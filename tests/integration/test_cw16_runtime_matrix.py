"""CW-16의 재실행 가능한 Linux shell 및 outer-terminal runtime matrix.

C-D72 (2): versions are recorded, never pinned. The outer matrix now runs through the product path
(``tests/integration/live_cw16_compat.py``: ``python -m workbench start``/``attach`` in plain PTY, isolated tmux
and isolated Herdr x host Bash/dash, scripted local provider). Historical: the p2.6 outer matrix ran the
G1 feasibility probes ``tests/gates/g1_vt/live_outer_compat_probe.py`` / ``live_outer_tmux_probe.py``, which
refuse anything but OMP 18.2.10 and Herdr 0.9.1 and drive ``terminal_g1`` (not the product UI); those probes
and their p2.6 evidence (candidate 6dc51f) stay as historical records only and are no longer run here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import shutil
import signal
import stat
import string
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

from workbench.terminal.shell_g2.prototype import ShellUnavailable, select_shell


ROOT = Path(__file__).resolve().parents[2]
_OUTPUT_LIMIT = 8 * 1024 * 1024


def _environment() -> dict[str, str]:
    # The session running this may live inside the user's tmux/Herdr: never hand their context to a probe.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("TMUX", "HERDR_", "WORKBENCH_"))}
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "src"})
    return environment


def _limit_child_files() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (_OUTPUT_LIMIT, _OUTPUT_LIMIT))


def _process_identity(pid: int) -> tuple[int, int] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        return int(fields[1]), int(fields[19])
    except (FileNotFoundError, IndexError, PermissionError, ValueError):
        return None


def _descendants(root_pid: int) -> dict[int, int]:
    processes: dict[int, tuple[int, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or (identity := _process_identity(int(entry.name))) is None:
            continue
        processes[int(entry.name)] = identity
    selected = {root_pid}
    while True:
        expanded = selected | {pid for pid, (parent, _) in processes.items() if parent in selected}
        if expanded == selected:
            break
        selected = expanded
    return {pid: processes[pid][1] for pid in selected if pid in processes}


def _owned_processes(owner_token: str) -> dict[int, int]:
    """Processes carrying ``owner_token``: in their cmdline (any process), or in their environ for this test
    process's own descendants only -- the environ of a process this test did not start is never read."""
    marker = owner_token.encode()
    owned: dict[int, int] = {}
    mine = _descendants(os.getpid())
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        identity = _process_identity(pid)
        if identity is None:
            continue
        try:
            evidence = entry.joinpath("cmdline").read_bytes()
            if pid in mine and mine[pid] == identity[1]:
                evidence += entry.joinpath("environ").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if marker in evidence:
            owned[pid] = identity[1]
    return owned


def _signal_exact(identities: dict[int, int], signum: int) -> None:
    for pid, started in identities.items():
        try:
            pidfd = os.pidfd_open(pid)
        except (OSError, ProcessLookupError):
            continue
        try:
            identity = _process_identity(pid)
            if identity is None or identity[1] != started:
                continue
            signal.pidfd_send_signal(pidfd, signum)
        except ProcessLookupError:
            pass
        finally:
            os.close(pidfd)


def _terminate_probe(
    process: subprocess.Popen[str], owner_token: str, root_started: int | None
) -> None:
    current = _process_identity(process.pid)
    identities = (
        _descendants(process.pid)
        if current is not None and current[1] == root_started
        else {}
    )
    identities.update(_owned_processes(owner_token))
    _signal_exact(identities, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    identities.update(_owned_processes(owner_token))
    _signal_exact(identities, signal.SIGKILL)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _short_owned_root() -> tuple[Path, tuple[int, int]]:
    # One path component keeps Herdr's nested UDS below sockaddr_un length.
    # mkdir is the ownership claim; existing user paths are never reused.
    for name in string.ascii_letters + string.digits:
        root = Path("/tmp") / name
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            continue
        created = root.stat(follow_symlinks=False)
        return root, (created.st_dev, created.st_ino)
    raise RuntimeError("no short private TMPDIR is available")


def _same_owned_root(root: Path, identity: tuple[int, int]) -> bool:
    try:
        current = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and (current.st_dev, current.st_ino) == identity
    )


def _run_bounded(
    argv: list[str], timeout: float, *, limit_child_files: bool = True
) -> subprocess.CompletedProcess[str]:
    # limit_child_files=False: RLIMIT_FSIZE is inherited by every descendant; the live product matrix must
    # not run under it (OMP 18.8.0 extracts its native addon, > 8 MiB, on first start -> degraded). The
    # captured output is still checked against _OUTPUT_LIMIT afterwards.
    owner_token = "cw16-owner-" + uuid.uuid4().hex
    sandbox, sandbox_identity = _short_owned_root()
    environment = _environment()
    environment["CW16_PROBE_OWNER"] = owner_token
    environment["TMPDIR"] = str(sandbox)
    stdout_file = tempfile.TemporaryFile(mode="w+b", dir=sandbox)
    stderr_file = tempfile.TemporaryFile(mode="w+b", dir=sandbox)
    try:
        process = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=environment,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
            preexec_fn=_limit_child_files if limit_child_files else None,
        )
    except BaseException:
        stdout_file.close()
        stderr_file.close()
        if _same_owned_root(sandbox, sandbox_identity):
            sandbox.rmdir()
        raise
    root_identity = _process_identity(process.pid)
    root_started = root_identity[1] if root_identity is not None else None
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_probe(process, owner_token, root_started)
    stdout_file.flush()
    stderr_file.flush()
    output_overflow = (
        os.fstat(stdout_file.fileno()).st_size >= _OUTPUT_LIMIT
        or os.fstat(stderr_file.fileno()).st_size >= _OUTPUT_LIMIT
    )
    stdout_file.seek(0)
    stderr_file.seek(0)
    stdout = stdout_file.read(_OUTPUT_LIMIT + 1).decode(errors="replace")
    stderr = stderr_file.read(_OUTPUT_LIMIT + 1).decode(errors="replace")
    stdout_file.close()
    stderr_file.close()
    owned_before_cleanup = _owned_processes(owner_token)
    if not _same_owned_root(sandbox, sandbox_identity):
        _signal_exact(owned_before_cleanup, signal.SIGKILL)
        raise AssertionError("owned TMPDIR identity changed; refusing path cleanup")
    residue = sorted(str(path.relative_to(sandbox)) for path in sandbox.rglob("*"))
    _signal_exact(owned_before_cleanup, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while _owned_processes(owner_token) and time.monotonic() < deadline:
        time.sleep(0.02)
    owned_after_cleanup = _owned_processes(owner_token)
    if sandbox.parent != Path("/tmp") or len(sandbox.name) != 1:
        raise AssertionError(f"refusing to remove unexpected sandbox {sandbox}")
    if not _same_owned_root(sandbox, sandbox_identity):
        raise AssertionError("owned TMPDIR identity changed before removal")
    shutil.rmtree(sandbox)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
    if output_overflow:
        raise AssertionError(f"probe output reached {_OUTPUT_LIMIT}-byte file limit")
    if owned_before_cleanup or residue or owned_after_cleanup:
        raise AssertionError(
            f"probe leaked owned resources: processes={sorted(owned_before_cleanup)} "
            f"residue={residue} remaining={sorted(owned_after_cleanup)}"
        )
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


@unittest.skipUnless(
    sys.platform == "linux"
    and shutil.which("bash")
    and shutil.which("dash")
    and shutil.which("omp")
    and shutil.which("tmux")
    and shutil.which("herdr"),
    "Linux, Bash, dash, OMP, tmux, and Herdr are required",
)
class Cw16RuntimeMatrixTests(unittest.TestCase):
    maxDiff = None

    def test_actual_bash_and_dash_canonical_matrix(self) -> None:
        completed = _run_bounded(
            [
                sys.executable,
                "-m",
                "unittest",
                "tests.gates.g2_shell.test_canonical_matrix_integration",
                "-v",
            ],
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Ran 3 tests", completed.stderr)

    def test_runtime_versions_are_recorded_not_pinned(self) -> None:
        # C-D72 (2): record what actually runs (OMP / tmux / Herdr / Bash / dash); no version is required.
        sys.path.insert(0, str(ROOT / "tests" / "integration"))
        import cw16_harness

        versions = cw16_harness.tool_versions()
        print(f"\n[cw16-versions] {json.dumps(versions, sort_keys=True)}", file=sys.stderr)
        target = os.environ.get("WB_CW16_VERSION_RECORD")
        if target:
            Path(target).write_text(json.dumps(versions, indent=1, sort_keys=True) + "\n")
        self.assertTrue(versions["omp"].startswith("omp/"), versions)
        self.assertTrue(versions["tmux"].startswith("tmux "), versions)
        self.assertTrue(versions["herdr"].startswith("herdr "), versions)
        self.assertIn("GNU bash, version 5.", versions["bash"])
        self.assertTrue(versions["dash"].startswith("dash "), versions)

    @unittest.skipUnless(
        os.environ.get("WB_LIVE_CW16") == "1",
        "product-path outer matrix (~25 min, scripted provider): set WB_LIVE_CW16=1",
    )
    def test_product_path_outer_matrix(self) -> None:
        # Replaces the historical G1-probe outer matrix (OMP 18.2.10 / Herdr 0.9.1 pins, terminal_g1).
        run_id = "cw16-matrix-" + uuid.uuid4().hex[:8]
        report_dir = Path(os.environ.get("WB_CW16_REPORT_DIR") or "/tmp/wb-cw16-reports")
        with mock.patch.dict(os.environ, {"WB_CW16_RUN_ID": run_id, "WB_CW16_REPORT_DIR": str(report_dir)}):
            completed = _run_bounded(
                [sys.executable, "-m", "unittest", "-v", "tests/integration/live_cw16_compat.py"],
                timeout=3000,
                limit_child_files=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stdout[-4000:] + completed.stderr[-8000:])
        matrix = json.loads((report_dir / run_id / "compat-matrix.json").read_text())
        expected_cells = {f"{outer}/{shell}" for outer in ("plain", "tmux", "herdr") for shell in ("bash", "dash")}
        self.assertEqual(set(matrix["cells"]), expected_cells, matrix)
        for cell, steps in matrix["cells"].items():
            for step, status in steps.items():
                allowed = ("pass", "n/a") if (cell.startswith("plain/") and step == "C6") else ("pass",)
                self.assertIn(status, allowed, (cell, step, matrix["cells"][cell]))
        self.assertEqual(matrix["selection"], {"sel-1": "pass", "sel-2": "pass", "sel-3": "pass"})
        for tool in ("omp", "tmux", "herdr", "bash", "dash"):
            self.assertNotIn(matrix["versions"][tool], ("missing", ""), tool)

    def test_timeout_kills_detached_owned_descendants(self) -> None:
        marker = "cw16-timeout-" + uuid.uuid4().hex
        unrelated = Path("/tmp") / (marker + "-unrelated")
        unrelated.write_text("preserve")
        source = (
            "import os,subprocess,sys,time; "
            "os.symlink(sys.argv[2], os.path.join(os.environ['TMPDIR'], 'external-link')); "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', sys.argv[1]], "
            "start_new_session=True); time.sleep(30)"
        )
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_bounded(
                    [sys.executable, "-c", source, marker, str(unrelated)], timeout=0.2
                )
            remaining = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if marker.encode() in entry.joinpath("cmdline").read_bytes():
                        remaining.append(int(entry.name))
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
            self.assertEqual(remaining, [])
            self.assertEqual(unrelated.read_text(), "preserve")
        finally:
            unrelated.unlink(missing_ok=True)

    def test_timeout_is_bounded_when_pipe_holder_drops_owner_token(self) -> None:
        marker = "cw16-unowned-pipe-" + uuid.uuid4().hex
        source = (
            "import os,subprocess,sys,time; "
            "environment={k:v for k,v in os.environ.items() if k != 'CW16_PROBE_OWNER'}; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.8)', sys.argv[1]], "
            "env=environment, start_new_session=True); time.sleep(30)"
        )
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_bounded([sys.executable, "-c", source, marker], timeout=0.2)
        self.assertLess(time.monotonic() - started, 2.0)
        deadline = time.monotonic() + 2
        remaining: list[int] = []
        while time.monotonic() < deadline:
            remaining = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if marker.encode() in entry.joinpath("cmdline").read_bytes():
                        remaining.append(int(entry.name))
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
            if not remaining:
                break
            time.sleep(0.02)
        self.assertEqual(remaining, [])

    def test_output_capture_has_a_hard_file_and_memory_bound(self) -> None:
        source = f"import os; os.write(1, b'x' * ({_OUTPUT_LIMIT} + 1024))"
        with self.assertRaisesRegex(AssertionError, "output reached"):
            _run_bounded([sys.executable, "-c", source], timeout=5)

    def test_shell_selection_ignores_login_shell_and_fails_with_guidance(self) -> None:
        bash = os.path.realpath(shutil.which("bash"))
        shell = os.path.realpath(shutil.which("sh"))
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}), mock.patch(
            "workbench.terminal.shell_g2.prototype.shutil.which",
            side_effect=lambda name, path=None: bash if name == "bash" else shell,
        ):
            self.assertEqual(select_shell().kind, "bash")
        with mock.patch(
            "workbench.terminal.shell_g2.prototype.shutil.which",
            side_effect=lambda name, path=None: None if name == "bash" else shell,
        ):
            self.assertEqual(select_shell().kind, "sh")
        with mock.patch("workbench.terminal.shell_g2.prototype.shutil.which", return_value=None):
            with self.assertRaisesRegex(ShellUnavailable, "Bash or sh"):
                select_shell()


if __name__ == "__main__":
    unittest.main()
