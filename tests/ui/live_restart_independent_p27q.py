"""LIVE C-D62 (1) OMP pane restart with real OMP 18.4.4 (p27-restart-test-01), ZERO model calls. Opt-in: ``WB_LIVE_RESTART=1``.

Run (repo root, short scratch TMPDIR)::

    WB_LIVE_RESTART=1 PYTHONPATH=src:tests/ui TMPDIR=/tmp/wbp27q /tmp/cw02-g1-venv/bin/python -m unittest \\
        tests.ui.live_restart_independent_p27q -v

A backend is started with the real entrypoint (``start --no-attach``) in a scratch data dir, a scratch project dir and a
FAKE ``HOME`` (the user's ~/.omp ~/.agents ~/.claude ~/.codex are never read or written) with a fake API key string (no
prompt is ever submitted: Enter is only pressed while the manager pane is exited, i.e. it is the restart key, and the
text typed into the restarted OMP composer is never submitted). The real product UI (``attach``) runs on a PTY this
probe owns, rendered with pyte (which answers terminal queries like a real terminal).

Steps: record manager/worker/shell/backend identities -> SIGKILL the manager OMP PID (exact identity via pidfd) -> the
UI shows the '종료됨' notice -> Enter on the manager pane -> a NEW manager OMP pid (same start argv, role token hash,
no PI_AUTO_QA, no --resume/--continue), bridge peer pid matches, phase ready, isolation re-check finished (ok) -> the
pane redraws a fresh OMP and takes typed input -> worker OMP, host shell and backend identities unchanged -> detach ->
confirmed shutdown -> nothing naming the scratch root survives (exact-identity cleanup; survivors are failures).
Evidence: JSON lines on stderr (identities, booleans, digests; never OMP screens beyond short markers).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from live_mux_independent_p27n import OuterTerminal, kill_identity, owned_processes, ticks  # noqa: E402

REPO = HERE.parents[1]
SRC = str(REPO / "src")
LIVE = os.environ.get("WB_LIVE_RESTART") == "1"
OMP = shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
PREFIX = b"\x1d"
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"
FAKE_KEY = "sk-ant-fake-p27q-never-used"
EVIDENCE: list[dict] = []


def note(**record) -> None:
    EVIDENCE.append(record)
    print(json.dumps(record, ensure_ascii=False, default=str), file=sys.stderr)


def cmdline(pid: int) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")[:-1]
    except OSError:
        return []


def environ(pid: int) -> dict[str, str]:
    try:
        blob = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    return dict(item.decode(errors="replace").split("=", 1) for item in blob.split(b"\0") if b"=" in item)


def rchar(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/io").read_text().splitlines():
            if line.startswith("rchar:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def digest(value: str | None) -> str | None:
    return None if value is None else hashlib.sha256(value.encode()).hexdigest()[:12]


@unittest.skipUnless(LIVE, "set WB_LIVE_RESTART=1 to run the live OMP restart check")
class LiveRestartP27q(unittest.TestCase):
    def setUp(self):
        if not os.access(OMP, os.X_OK):
            self.skipTest("omp not found")
        version = subprocess.run([OMP, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        if "18.4.4" not in version:
            self.skipTest(f"omp version {version!r} is not 18.4.4")
        self.root = Path(tempfile.mkdtemp(prefix="wbp27q-", dir=os.environ.get("TMPDIR") or "/tmp"))
        self.home, self.project, self.data = self.root / "home", self.root / "proj", self.root / "d"
        (self.home / ".omp" / "agent").mkdir(parents=True)
        if os.environ.get("P27Q_WIZARD") != "1":  # the fake HOME would open OMP's first-run setup wizard
            (self.home / ".omp" / "agent" / "config.yml").write_text("startup:\n  setupWizard: false\n")
        self.project.mkdir()
        tmp = self.root / "tmp"
        tmp.mkdir()
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home), "LANG": "C.UTF-8",
                    "TERM": "xterm-256color", "SHELL": "/bin/bash", "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1",
                    "TMPDIR": str(tmp), "ANTHROPIC_API_KEY": FAKE_KEY, "PI_AUTO_QA": "1"}
        self.term: OuterTerminal | None = None

    def cli(self, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", MAIN, *args], env=self.env, cwd=self.project, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def status(self) -> dict:
        out = self.cli("status", "--data-dir", str(self.data), "--json", timeout=30)
        value = json.loads(out.stdout)
        return value.get("snapshot") or {}

    def wait_status(self, predicate, timeout: float = 90.0) -> dict:
        deadline, snap = time.monotonic() + timeout, {}
        while time.monotonic() < deadline:
            snap = self.status()
            if snap and predicate(snap):
                return snap
            if self.term is not None:
                self.term.pump(0.3)
            else:
                time.sleep(0.3)
        self.fail(f"status predicate not met: phase={snap.get('phase')} reason={snap.get('reason')} "
                  f"isolation={(snap.get('omp_isolation') or {}).get('state')}")

    def tearDown(self):
        residue: dict = {}
        try:
            if self.term is not None and self.term.proc.poll() is None:
                kill_identity(self.term.proc.pid, self.term.start, signal.SIGKILL)
                self.term.proc.wait(5)
            if self.term is not None:
                os.close(self.term.fd)
            if (self.data / "ui.sock").exists():
                self.cli("shutdown", "--data-dir", str(self.data), "--yes", "--json", timeout=90)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and owned_processes(self.root):
                time.sleep(0.2)
            residue = owned_processes(self.root)
            for pid, start in residue.items():
                kill_identity(pid, start, signal.SIGKILL)
        finally:
            shutil.rmtree(self.root, ignore_errors=True)
        note(step="teardown", residue=sorted(residue))
        self.assertEqual({}, residue, "processes of this probe survived the shutdown")

    @staticmethod
    def ident(pane: dict) -> tuple:
        process = pane.get("process") or {}
        return process.get("pid"), process.get("start_ticks")

    def test_sigkill_manager_then_enter_restarts_a_fresh_isolated_omp(self):
        started = self.cli("start", "--data-dir", str(self.data), "--omp", OMP, "--no-attach", "--timeout", "150")
        self.assertEqual(0, started.returncode, (started.stdout + started.stderr)[-800:])
        snap = self.wait_status(lambda s: s.get("phase") == "ready"
                                and (s.get("omp_isolation") or {}).get("state") not in (None, "pending"))
        panes = snap["panes"]
        manager0, worker0 = self.ident(panes["manager_omp"]), self.ident(panes["worker_omp"])
        shell0, backend0 = self.ident(panes["host_shell"]), (snap["backend"]["process"]["pid"],
                                                             snap["backend"]["process"]["start_ticks"])
        argv0, env0 = cmdline(manager0[0]), environ(manager0[0])
        note(step="started", phase=snap["phase"], isolation=snap["omp_isolation"]["state"], manager=manager0,
             worker=worker0, shell=shell0, backend=backend0, generation=panes["manager_omp"].get("generation"),
             bridge_manager_pid=snap["bridge"]["manager"]["pid"], token=digest(env0.get("WORKBENCH_G3_TOKEN")),
             argv_len=len(argv0))
        self.assertEqual(snap["omp_isolation"]["state"], "ok", snap["omp_isolation"])
        self.assertNotIn("PI_AUTO_QA", env0)
        self.assertEqual("manager", env0.get("WORKBENCH_G3_ROLE"))

        self.term = term = OuterTerminal([sys.executable, "-c", MAIN, "attach", "--data-dir", str(self.data)],
                                         self.env, str(self.project))
        self.assertTrue(term.until(lambda: "MANAGER OMP" in term.text() and "WORKER OMP" in term.text(), 40),
                        term.text()[-600:])
        term.send(PREFIX + b"1")  # focus the manager pane
        term.pump(1.5)
        # control: the ORIGINAL manager OMP shows typed text (never submitted), then it is erased again
        control = "p27qctl"
        term.send(control.encode())
        control_ok = term.until(lambda: control in term.text(), 20)
        term.send(b"\x7f" * len(control))
        term.until(lambda: control not in term.text(), 10)
        note(step="control_typing", ok=control_ok)
        if os.environ.get("P27Q_DEBUG"):
            print("\n".join(term.text().splitlines()[:30]), file=sys.stderr)
        self.assertTrue(control_ok, "typed text never showed in the original OMP (probe baseline)")

        # -- SIGKILL the manager OMP this probe's backend started (exact identity)
        kill_identity(manager0[0], str(manager0[1]), signal.SIGKILL)
        self.assertTrue(term.until(lambda: "OMP 종료됨" in term.text(), 20), term.text()[-800:])
        exited = self.wait_status(lambda s: s["panes"]["manager_omp"].get("alive") is False, 20)
        note(step="killed", phase=exited["phase"], reason=exited.get("reason"),
             exit_status=exited["panes"]["manager_omp"].get("exit_status"),
             notice_title="종료됨" in term.text())
        self.assertEqual("degraded", exited["phase"])
        # Only now (the pane is exited in the UI and in the backend) Enter is the restart key.
        term.send(b"\r")
        restarted = self.wait_status(
            lambda s: s["panes"]["manager_omp"].get("alive") is True and s.get("phase") == "ready"
            and s["bridge"]["manager"].get("pid_matches_pane") is True
            and (s.get("omp_isolation") or {}).get("state") not in (None, "pending")
            and not (s.get("omp_isolation") or {}).get("rechecking"), 150)
        pane = restarted["panes"]["manager_omp"]
        manager1 = self.ident(pane)
        argv1, env1 = cmdline(manager1[0]), environ(manager1[0])
        note(step="restarted", manager=manager1, generation=pane.get("generation"), session_changed=pane.get(
            "session_id") != panes["manager_omp"].get("session_id"), bridge_pid=restarted["bridge"]["manager"]["pid"],
             bridge_generation=restarted["bridge"]["manager"].get("generation"),
             isolation=restarted["omp_isolation"]["state"], restart=pane.get("restart", {}).get("state"),
             restart_count=pane.get("restart", {}).get("count"), token_same=digest(env1.get("WORKBENCH_G3_TOKEN")) ==
             digest(env0.get("WORKBENCH_G3_TOKEN")), argv_same=argv1 == argv0)
        self.assertNotEqual(manager0[0], manager1[0])
        self.assertEqual(2, pane.get("generation"))
        self.assertEqual(restarted["bridge"]["manager"]["pid"], manager1[0])
        self.assertEqual("ok", restarted["omp_isolation"]["state"], restarted["omp_isolation"])
        self.assertEqual(argv0, argv1, "the restarted OMP runs the start-up argv")
        self.assertFalse({"--resume", "-r", "--continue", "-c"} & set(argv1))
        for flag in ("--no-extensions", "--no-title", "--append-system-prompt"):
            self.assertIn(flag, argv1)
        self.assertEqual(env0.get("WORKBENCH_G3_TOKEN"), env1.get("WORKBENCH_G3_TOKEN"))
        self.assertEqual({k: v for k, v in env0.items()}, env1, "the restarted OMP has the start-up environment")
        self.assertNotIn("PI_AUTO_QA", env1)
        self.assertEqual(self.project.resolve(), Path(os.readlink(f"/proc/{manager1[0]}/cwd")).resolve())
        self.assertNotEqual(str(manager0[1]), ticks(manager0[0]), "the killed OMP is still alive")

        # -- the pane redraws a fresh OMP, the notice is gone, typed text reaches the new composer (never submitted)
        self.assertTrue(term.until(lambda: "OMP 종료됨" not in term.text() and "다시 시작 중" not in term.text(), 20),
                        term.text()[-800:])
        # wait until the new OMP has drawn (keys typed while OMP is still starting are its own business)
        rows = lambda: [ln[1:74] for ln in term.text().splitlines()[3:24]]  # the manager pane interior
        drawn = term.until(lambda: sum(bool(r.strip()) for r in rows()) >= 3, 30)
        term.pump(3.0)
        note(step="fresh_omp_drawn", drawn=drawn, nonblank_rows=sum(bool(r.strip()) for r in rows()))
        self.assertTrue(drawn, "the restarted OMP never drew into the manager pane")
        marker = "p27qtyped"
        reads_before = rchar(manager1[0])
        term.send(marker.encode())
        typed = term.until(lambda: marker in term.text(), 20)
        note(step="typed_reads", rchar_before=reads_before, rchar_after=rchar(manager1[0]),
             queued=self.status()["panes"]["manager_omp"].get("queued_input_bytes"))
        if os.environ.get("P27Q_DEBUG"):
            print("\n".join(term.text().splitlines()[:30]), file=sys.stderr)
        note(step="redraw", notice_gone=True, typed_echo=typed)
        self.assertTrue(typed, "the restarted OMP did not show typed input")

        # -- the others are untouched
        final = self.status()
        self.assertEqual(worker0, self.ident(final["panes"]["worker_omp"]))
        self.assertEqual(shell0, self.ident(final["panes"]["host_shell"]))
        self.assertEqual(backend0, (final["backend"]["process"]["pid"], final["backend"]["process"]["start_ticks"]))
        for pid, start in (worker0, shell0, backend0):
            self.assertEqual(str(start), ticks(pid), f"{pid} changed")
        self.assertTrue(final["bridge"]["worker"]["pid_matches_pane"])
        note(step="others_unchanged", worker=worker0, shell=shell0, backend=backend0, phase=final["phase"])

        # -- detach, then confirmed shutdown; nothing survives
        term.send(PREFIX + b"q")
        deadline = time.monotonic() + 15
        while term.proc.poll() is None and time.monotonic() < deadline:
            term.pump(0.1)
        self.assertEqual(0, term.proc.poll())
        down = self.cli("shutdown", "--data-dir", str(self.data), "--yes", "--json", timeout=90)
        note(step="shutdown", code=down.returncode, out=down.stdout.strip()[-300:])
        self.assertEqual(0, down.returncode, down.stdout + down.stderr)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and owned_processes(self.root):
            time.sleep(0.2)
        self.assertEqual({}, owned_processes(self.root))
        for pid, start in (manager1, worker0, shell0, backend0):
            self.assertNotEqual(str(start), ticks(pid))


if __name__ == "__main__":
    unittest.main()
