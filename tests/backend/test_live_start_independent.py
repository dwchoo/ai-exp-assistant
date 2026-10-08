"""Independent L-CW17-START runtime and start-negative checks (p27-cw17-test-01).

Expected outcomes derived from CW-17.md / SPEC s2.7 / C-AC-20 before reading
the implementation:
- Bash is chosen when available, otherwise sh (dash here). A zsh login/default
  shell (SHELL=zsh) neither changes the choice nor is executed, and the user's
  SHELL setting is preserved in the host shell's environment.
- Without Bash and sh the entrypoint prints the start requirements, exits
  non-zero and leaves no backend (also in the default attach mode on a PTY).
- One backend per data dir: concurrent starts yield exactly one backend.
- A backend that died leaves a stale socket/lock; the next start must recover
  without killing any live process it does not own (e.g. a foreign process
  whose PID appears in stale records, or a live foreign lock holder).
- Data dir 0700 (tightened if pre-existing), sockets 0600, and no private file
  in the data dir is group/other accessible.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import unittest

from independent_support import (
    LiveBackend, PaneId, UiClient, finish, find_omp, kill_exact, residue, start_no_attach, stop_and_verify,
    ticks, wait_file, wait_status,
)

OMP = find_omp()


def fake_zsh(bindir: Path, sentinel: Path) -> Path:
    script = bindir / "zsh"
    script.write_text(f"#!/usr/bin/dash\necho ran >> {sentinel}\nexit 0\n")
    script.chmod(0o755)
    return script


def foreign_sleeper() -> tuple[subprocess.Popen, int]:
    process = subprocess.Popen(["/usr/bin/sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    return process, ticks(process.pid)


@unittest.skipUnless(OMP, "real OMP is required for L-CW17-START (version recorded, not pinned: C-D72 (2))")
class IndependentLiveStartTests(unittest.TestCase):
    def live(self, **kwargs) -> LiveBackend:
        live = LiveBackend(OMP, **kwargs)
        self.extra: list[tuple[int, int]] = []
        self.addCleanup(finish, self, live, self.extra)
        return live

    def bindir(self, live: LiveBackend, **links: str) -> Path:
        path = live.root / "bin"
        path.mkdir(exist_ok=True)
        for name, target in links.items():
            (path / name).symlink_to(target)
        return path

    @staticmethod
    def reap(process: subprocess.Popen, start: int) -> None:
        if process.poll() is None:
            kill_exact(process.pid, start)
        process.wait(10)

    def assert_shutdown_clean(self, live: LiveBackend) -> None:
        left, output = stop_and_verify(live)
        self.assertIn('"verified": true', output)
        self.assertEqual(left, {})

    # -- shell selection -------------------------------------------------
    def test_bash_selected_and_fake_zsh_login_shell_never_executed(self):
        live = self.live()
        sentinel = live.root / "zsh-ran"
        bindir = self.bindir(live, bash="/usr/bin/bash", sh="/usr/bin/dash", omp=OMP)
        zsh = fake_zsh(bindir, sentinel)
        live.env.update(PATH=str(bindir), SHELL=str(zsh))
        result = live.cli(*live.start_args("--no-attach"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("shell bash (/usr/bin/bash)", result.stdout)
        snapshot = wait_status(live, lambda s: s["phase"] != "starting")
        live.remember(snapshot)
        self.assertEqual(snapshot["phase"], "ready", snapshot.get("reason"))
        shell = snapshot["panes"]["host_shell"]["shell"]
        pid = shell["parent"]["pid"]
        self.assertEqual((shell["kind"], os.readlink(f"/proc/{pid}/exe")), ("bash", "/usr/bin/bash"))
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        self.assertIn(f"SHELL={zsh}".encode(), environ, "user's SHELL setting changed")
        marker = live.project / "m-bash"
        with UiClient(live.data / "ui.sock") as client:
            self.assertTrue(client.attach((30, 100))["ok"])
            ok = client.input(PaneId.HOST_SHELL, f"printf '%s' \"$BASH_VERSION:$$\" > {marker}\r".encode())
            self.assertTrue(ok["ok"], ok)
            deadline = time.monotonic() + 10
            while not (marker.exists() and marker.read_text()) and time.monotonic() < deadline:
                client.pump(0.05)
            client.detach()
        version, _, shell_pid = marker.read_text().partition(":")
        self.assertTrue(version.startswith("5."), version)
        self.assertEqual(int(shell_pid), pid)
        self.assert_shutdown_clean(live)
        self.assertFalse(sentinel.exists(), "the zsh login shell was executed")

    def test_dash_selected_without_bash_and_zsh_default_ignored(self):
        live = self.live()
        sentinel = live.root / "zsh-ran"
        bindir = self.bindir(live, sh="/usr/bin/dash", omp=OMP)
        zsh = fake_zsh(bindir, sentinel)
        live.env.update(PATH=str(bindir), SHELL=str(zsh))
        result = live.cli(*live.start_args("--no-attach"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("shell sh (/usr/bin/dash)", result.stdout)
        snapshot = wait_status(live, lambda s: s["phase"] != "starting")
        live.remember(snapshot)
        self.assertEqual(snapshot["phase"], "ready", snapshot.get("reason"))
        shell = snapshot["panes"]["host_shell"]["shell"]
        pid = shell["parent"]["pid"]
        self.assertEqual((shell["kind"], os.readlink(f"/proc/{pid}/exe")), ("sh", "/usr/bin/dash"))
        marker = live.project / "m-dash"
        with UiClient(live.data / "ui.sock") as client:
            self.assertTrue(client.attach((30, 100))["ok"])
            ok = client.input(PaneId.HOST_SHELL, f"printf '%s' \"$$:${{BASH_VERSION:-none}}\" > {marker}\r".encode())
            self.assertTrue(ok["ok"], ok)
            self.assertEqual(wait_file(marker, f"{pid}:none", 10, client), f"{pid}:none")
            client.detach()
        self.assert_shutdown_clean(live)
        self.assertFalse(sentinel.exists(), "the zsh login shell was executed")

    def test_no_bash_no_sh_on_plain_pty_prints_requirements_exits_nonzero_and_starts_nothing(self):
        live = self.live(seed_provider=False)  # gap-01 wiring: the data dir must stay untouched before start
        sentinel = live.root / "zsh-ran"
        bindir = self.bindir(live, omp=OMP)
        zsh = fake_zsh(bindir, sentinel)
        live.env.update(PATH=str(bindir), SHELL=str(zsh))
        run = live.pty(*live.start_args())  # default attach mode, real terminal
        status = run.wait(60)
        text = bytes(run.output).decode(errors="replace")
        self.assertEqual(status, 2, text)
        self.assertIn("Bash", text)
        self.assertIn("No backend was started", text)
        self.assertNotIn("attached to", text)
        self.assertEqual(live.backend_processes(), [])
        leftovers = sorted(p.name for p in live.data.iterdir()) if live.data.exists() else []
        self.assertEqual(leftovers, [], "start requirement failure left backend files")
        status_run = live.cli("status", "--data-dir", str(live.data), "--json", timeout=30)
        self.assertEqual(status_run.returncode, 3)
        self.assertFalse(sentinel.exists())

    # -- single instance, stale state -------------------------------------
    def test_concurrent_starts_yield_one_backend_and_private_modes(self):
        # p27-cw16-fix-05 (P3-1): seed_provider=False -> omp-root and agent are created by the product itself.
        live = self.live(seed_provider=False)
        live.data.mkdir(mode=0o755)
        os.chmod(live.data, 0o755)
        procs = [subprocess.Popen(live.argv(*live.start_args("--no-attach")), env=live.env, cwd=live.project,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(3)]
        outputs = [p.communicate(timeout=180) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0, 0], outputs)
        snapshot = wait_status(live, lambda s: s["phase"] != "starting")
        live.remember(snapshot)
        self.assertEqual(live.backend_processes(), [snapshot["backend"]["pid"]])
        log = (live.data / "backend.log").read_text()
        self.assertEqual(log.count("starting in"), 1, log)
        self.assertEqual(stat.S_IMODE(os.stat(live.data).st_mode), 0o700)
        for name in ("ui.sock", "bridge.sock"):
            info = os.lstat(live.data / name)
            self.assertTrue(stat.S_ISSOCK(info.st_mode), name)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600, name)
        # p27-cw16-fix-05 (Root-approved delta, P2-1): OMP keeps the user's umask, so privacy of what OMP writes comes
        # from the 0700 ancestors; the product creates omp-root and omp-root/agent as 0700.
        omp_root_dir = live.data / "omp-root"
        for directory in (omp_root_dir, omp_root_dir / "agent"):
            self.assertEqual(stat.S_IMODE(os.lstat(directory).st_mode), 0o700, str(directory))
        exposed = []
        for root, dirs, files in os.walk(live.data):
            if Path(root) == omp_root_dir:
                dirs[:] = []  # entries OMP itself creates under the 0700 omp-root: protected by that ancestor
            for name in dirs + files:
                path = Path(root) / name
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    continue  # p27-cw16-fix-04 (Root-approved delta): C-D64 agent.db symlink, lstat mode is always 0777
                if stat.S_IMODE(info.st_mode) & 0o077:
                    exposed.append((str(path.relative_to(live.data)), oct(stat.S_IMODE(info.st_mode))))
        self.assertEqual(exposed, [], "Workbench-written data-dir entries readable by group/other")
        self.assert_shutdown_clean(live)

    def test_symlinked_data_dir_is_refused_before_anything_starts(self):
        live = self.live(seed_provider=False)  # gap-01 wiring: nothing may be written through the symlink
        target = live.root / "elsewhere"
        target.mkdir(mode=0o700)
        live.data.symlink_to(target)
        result = live.cli(*live.start_args("--no-attach"), timeout=60)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("data dir", result.stderr)
        self.assertEqual(live.backend_processes(), [])
        self.assertEqual(list(target.iterdir()), [])

    def test_dead_backend_stale_socket_and_lock_restart_without_killing_foreign_processes(self):
        live = self.live()
        first = start_no_attach(self, live)
        old_backend = first["backend"]["process"]
        old_panes = {name: (p["process"]["pid"], p["process"]["start_ticks"]) for name, p in first["panes"].items()}
        foreign, foreign_ticks = foreign_sleeper()
        self.extra.append((foreign.pid, foreign_ticks))
        self.addCleanup(self.reap, foreign, foreign_ticks)
        # Make the stale record point at a live foreign process as well.
        record = json.loads((live.data / "backend.json").read_text())
        self.assertEqual(record["processes"]["backend"]["pid"], old_backend["pid"])

        self.assertTrue(kill_exact(old_backend["pid"], old_backend["start_ticks"]))
        deadline = time.monotonic() + 10
        while ticks(old_backend["pid"]) == old_backend["start_ticks"] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertNotEqual(ticks(old_backend["pid"]), old_backend["start_ticks"], "old backend still alive")
        self.assertTrue((live.data / "ui.sock").exists(), "no stale socket to exercise")
        self.assertTrue((live.data / "backend.lock").exists())
        time.sleep(2.0)
        orphans_before = {name: ident for name, ident in old_panes.items() if ticks(ident[0]) == ident[1]}

        status = live.cli("status", "--data-dir", str(live.data), "--json", timeout=30)
        self.assertEqual(status.returncode, 3, status.stdout + status.stderr)
        self.assertFalse(json.loads(status.stdout)["running"])

        second = live.cli(*live.start_args("--no-attach"))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        snapshot = wait_status(live, lambda s: s["phase"] != "starting")
        live.remember(snapshot)
        self.assertEqual(snapshot["phase"], "ready", snapshot.get("reason"))
        self.assertNotEqual(snapshot["backend"]["pid"], old_backend["pid"])
        self.assertEqual(live.backend_processes(), [snapshot["backend"]["pid"]])
        # (tmpfs may reuse the stale socket's inode number; the served snapshot is the evidence.)
        self.assertTrue(stat.S_ISSOCK(os.lstat(live.data / "ui.sock").st_mode))
        self.assertEqual(ticks(foreign.pid), foreign_ticks, "a live foreign process was killed")
        for name, (pid, start) in orphans_before.items():
            self.assertEqual(ticks(pid), start, f"new backend killed old {name} it does not own")
        self.assert_shutdown_clean_except(live, orphans_before)
        self.assertEqual(ticks(foreign.pid), foreign_ticks, "shutdown killed a foreign process")
        print(f"\n[observation] old pane processes alive after backend SIGKILL: "
              f"{sorted(orphans_before)}", file=sys.stderr)

    def assert_shutdown_clean_except(self, live: LiveBackend, orphans: dict) -> None:
        """Shut the new backend down; then remove the old backend's orphans by exact identity."""
        result = live.shutdown()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"verified": true', result.stdout)
        for pid, start in orphans.values():
            kill_exact(pid, start)
        deadline = time.monotonic() + 15
        while residue(live) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(residue(live), {})

    def test_live_foreign_lock_holder_blocks_start_and_is_not_killed(self):
        live = self.live()
        live.data.mkdir(mode=0o700)
        holder_source = (
            "import fcntl, os, sys, time\n"
            f"fd = os.open({str(live.data / 'backend.lock')!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(300)\n"
        )
        holder = subprocess.Popen([sys.executable, "-c", holder_source], stdout=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, start_new_session=True)
        holder_ticks = ticks(holder.pid)
        self.extra.append((holder.pid, holder_ticks))
        self.addCleanup(self.reap, holder, holder_ticks)
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), b"locked")
        started = time.monotonic()
        result = live.cli(*live.start_args("--no-attach", "--timeout", "12"), timeout=90)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("did not become available", result.stderr)
        self.assertEqual(ticks(holder.pid), holder_ticks, "live lock holder was killed")
        deadline = time.monotonic() + 10
        while live.backend_processes() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(live.backend_processes(), [])
        log = (live.data / "backend.log").read_text()
        self.assertIn("another backend holds this data dir", log)
        self.assertNotIn("starting in", log)
        self.assertFalse((live.data / "ui.sock").exists())
        print(f"\n[observation] start against a live foreign lock holder returned after {elapsed:.1f}s "
              f"(--timeout 12; default START_TIMEOUT is 120s)", file=sys.stderr)
        self.assertTrue(kill_exact(holder.pid, holder_ticks))


if __name__ == "__main__":
    unittest.main()
