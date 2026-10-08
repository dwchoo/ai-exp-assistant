"""L-CW17-START runtime: real entrypoint shell selection, guidance and reuse.

Real Bash 5 and dash (as sh), a zsh login-shell environment (SHELL=zsh), a PATH
without Bash/sh, and a second/concurrent start that must reuse the backend.
Every case starts real OMP 18.2.10 processes with the bridge extension.
"""
import os
from pathlib import Path
import subprocess
import time
import unittest

from live_harness import LiveBackend, ZSH_DEFAULT, find_omp, kill_exact
from workbench.backend.client import UiClient
from workbench.contracts.v1 import PaneId

OMP = find_omp()


def wait_status(live, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    snapshot = None
    while time.monotonic() < deadline:
        snapshot = live.status()
        if snapshot is not None and predicate(snapshot):
            return snapshot
        time.sleep(0.1)
    raise AssertionError(f"status predicate timeout: {snapshot}")


@unittest.skipUnless(OMP, "real OMP is required for L-CW17-START (version recorded, not pinned: C-D72 (2))")
class LiveStartTests(unittest.TestCase):
    def live(self, **kwargs) -> LiveBackend:
        live = LiveBackend(OMP, **kwargs)

        def finish():
            residue = live.cleanup()
            self.assertEqual({k: v for k, v in residue.items() if v}, {}, "owned process/socket leak")
        self.addCleanup(finish)
        return live

    def bindir(self, live: LiveBackend, **links) -> str:
        path = live.root / "bin"
        path.mkdir()
        for name, target in links.items():
            (path / name).symlink_to(target)
        return str(path)

    def assert_clean_shutdown(self, live: LiveBackend):
        result = live.shutdown()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"verified": true', result.stdout)
        deadline = time.monotonic() + 10
        while any(live.leaks().values()) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual({k: v for k, v in live.leaks().items() if v}, {})

    def test_bash_first_zsh_login_shell_ignored_and_second_start_reuses_backend(self):
        live = self.live(path="/usr/bin:/bin")
        self.assertEqual(live.env["SHELL"], ZSH_DEFAULT)
        # Two concurrent starts on a fresh data dir: exactly one backend wins the lock.
        first = subprocess.Popen(live.argv(*live.start_args("--no-attach")), env=live.env, cwd=live.project,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(live.argv(*live.start_args("--no-attach")), env=live.env, cwd=live.project,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        outputs = [process.communicate(timeout=180) for process in (first, second)]
        self.assertEqual([first.returncode, second.returncode], [0, 0], outputs)
        snapshot = live.status()
        live.remember(snapshot)
        backend_pid = snapshot["backend"]["pid"]
        self.assertEqual(live.backend_processes(), [backend_pid])
        self.assertEqual(snapshot["phase"], "ready", snapshot.get("reason"))
        # Detached from the frontend: its own session, not a child of any start process.
        self.assertNotEqual(snapshot["backend"]["session_id"], os.getsid(0))
        parent = int(Path(f"/proc/{backend_pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()[1])
        self.assertNotIn(parent, {first.pid, second.pid, os.getpid()})
        shell = snapshot["panes"]["host_shell"]["shell"]
        self.assertEqual((shell["kind"], shell["executable"]), ("bash", "/usr/bin/bash"))
        self.assertEqual(os.readlink(f"/proc/{shell['parent']['pid']}/exe"), "/usr/bin/bash")
        self.assertEqual(Path(f"/proc/{shell['parent']['pid']}/environ").read_bytes().count(b"SHELL=" + ZSH_DEFAULT.encode()), 1)
        for role in ("manager", "worker"):
            self.assertTrue(snapshot["bridge"][role]["pid_matches_pane"], snapshot["bridge"])
        self.assertEqual(oct(os.stat(live.data).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(live.data / "ui.sock").st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(live.data / "bridge.sock").st_mode & 0o777), "0o600")
        # A later start attaches (plain PTY) instead of spawning.
        run = live.pty(*live.start_args("--plain"))
        run.wait_output(b"attaching instead of starting", 60)
        run.wait_output(b"attached to", 30)
        run.send(b"\x1dd")
        self.assertEqual(run.wait(30), 0)
        self.assertIn(b"backend keeps running", run.output)
        # An abruptly killed attached client is only a disconnect.
        killed = live.pty("attach", "--plain", "--data-dir", str(live.data))
        killed.wait_output(b"attached to", 30)
        self.assertTrue(kill_exact(killed.pid, killed.ticks))
        self.assertEqual(killed.wait(10), -9)
        again = wait_status(live, lambda s: s["attached"] is False)
        self.assertEqual(again["backend"]["process"], snapshot["backend"]["process"])
        self.assertEqual({k: p["process"] for k, p in again["panes"].items()},
                         {k: p["process"] for k, p in snapshot["panes"].items()})
        self.assertTrue(all(p["alive"] for p in again["panes"].values()))
        self.assertEqual(live.backend_processes(), [backend_pid])
        log = (live.data / "backend.log").read_text()
        self.assertEqual(log.count("starting in"), 1, log)
        self.assertEqual(log.count("another backend holds this data dir"), 1, log)
        self.assertEqual(live.provider.requests, 0)
        self.assert_clean_shutdown(live)

    def test_sh_only_path_selects_dash_and_accepts_manual_input(self):
        live = self.live()
        live.env["PATH"] = self.bindir(live, sh="/usr/bin/dash", omp=OMP)
        result = live.cli(*live.start_args("--no-attach"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("shell sh (/usr/bin/dash)", result.stdout)
        snapshot = live.status()
        live.remember(snapshot)
        shell = snapshot["panes"]["host_shell"]["shell"]
        self.assertEqual((shell["kind"], shell["executable"]), ("sh", "/usr/bin/dash"))
        self.assertEqual(os.readlink(f"/proc/{shell['parent']['pid']}/exe"), "/usr/bin/dash")
        marker = live.project / "dash-marker"
        with UiClient(live.data / "ui.sock") as client:
            self.assertTrue(client.attach((30, 100))["ok"])
            accepted = client.paste(PaneId.HOST_SHELL, f"printf '%s' \"$$\" > {marker}\r".encode())
            self.assertTrue(accepted["ok"], accepted)
            deadline = time.monotonic() + 10
            while not (marker.exists() and marker.read_text()) and time.monotonic() < deadline:
                client.pump(0.05)
            self.assertEqual(marker.read_text(), str(shell["parent"]["pid"]))
            client.detach()
        self.assert_clean_shutdown(live)

    def test_path_without_bash_or_sh_prints_requirements_and_starts_nothing(self):
        live = self.live(seed_provider=False)  # gap-01 wiring: the data dir must stay untouched before start
        live.env["PATH"] = self.bindir(live, omp=OMP)
        result = live.cli(*live.start_args("--no-attach"), timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("needs Bash or a POSIX sh", result.stderr)
        self.assertIn("No backend was started", result.stderr)
        self.assertIn("zsh", result.stderr)
        self.assertEqual(live.backend_processes(), [])
        for name in ("ui.sock", "bridge.sock", "backend.json", "backend.lock", "backend.log"):
            self.assertFalse((live.data / name).exists(), name)
        self.assertEqual(live.status(), None)


if __name__ == "__main__":
    unittest.main()
