"""LIVE C-D63 host terminal force-kill and Enter restart (p27-shell-test-01), ZERO model calls. Opt-in: ``WB_LIVE_SHELL=1``.

Run (repo root, short scratch TMPDIR)::

    WB_LIVE_SHELL=1 PYTHONPATH=src:tests/ui TMPDIR=/tmp/wbp27s /tmp/cw02-g1-venv/bin/python -m unittest \\
        tests.ui.live_shell_kill_independent_p27s -v

A backend is started with the real entrypoint (``start --no-attach``) and the real OMP 18.4.4 in a scratch data dir,
a scratch project dir and a FAKE ``HOME`` (the user's ~/.omp ~/.agents ~/.claude ~/.codex are never read or written)
with a fake API key string. Nothing is ever typed into an OMP pane, so no prompt is submitted. The real product UI
(``attach``) runs on a PTY this probe owns and drives the real Bash host shell:

background job + TERM/HUP-ignoring background job + a setsid daemon + a long foreground job -> ``Ctrl-] k`` -> the
confirmation is drawn -> ``k`` -> every job PID is gone, the daemon survives (this probe kills it afterwards by exact
identity) -> the pane shows 'host terminal 종료됨' -> Enter -> a new shell (new pid, generation 2, owner user, same cwd)
takes input, ``wb-handoff`` + ``Ctrl-] h`` hand it to the manager -> OMPs and backend unchanged -> detach -> confirmed
shutdown -> nothing naming the scratch root survives (exact-identity cleanup; survivors are failures).
Evidence: JSON lines on stderr.
"""
from __future__ import annotations

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
LIVE = os.environ.get("WB_LIVE_SHELL") == "1"
OMP = shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
PREFIX = b"\x1d"
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"
FAKE_KEY = "sk-ant-fake-p27s-never-used"
KILL_TEXT = "host terminal을 강제 종료합니다"


def note(**record) -> None:
    print(json.dumps(record, ensure_ascii=False, default=str), file=sys.stderr)


@unittest.skipUnless(LIVE, "live probe: set WB_LIVE_SHELL=1")
class LiveShellKillP27s(unittest.TestCase):
    def setUp(self):
        if not os.access(OMP, os.X_OK):
            self.skipTest("omp not found")
        version = subprocess.run([OMP, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        if "18.4." not in version:  # the OMP panes are only bystanders here; any 18.4.x starts the same way
            self.skipTest(f"omp version {version!r} is not 18.4.x")
        self.omp_version = version
        self.root = Path(tempfile.mkdtemp(prefix="wbp27s-", dir=os.environ.get("TMPDIR") or "/tmp"))
        self.home, self.project, self.data = self.root / "home", self.root / "proj", self.root / "d"
        self.marks = self.root / "m"
        (self.home / ".omp" / "agent").mkdir(parents=True)
        (self.home / ".omp" / "agent" / "config.yml").write_text("startup:\n  setupWizard: false\n")
        self.project.mkdir()
        self.marks.mkdir()
        tmp = self.root / "tmp"
        tmp.mkdir()
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home), "LANG": "C.UTF-8",
                    "TERM": "xterm-256color", "SHELL": "/bin/bash", "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1",
                    "TMPDIR": str(tmp), "ANTHROPIC_API_KEY": FAKE_KEY}
        self.term: OuterTerminal | None = None
        self.mine: dict[int, str] = {}  # pid -> start ticks of processes this probe made the shell start

    def cli(self, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", MAIN, *args], env=self.env, cwd=self.project, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def status(self) -> dict:
        out = self.cli("status", "--data-dir", str(self.data), "--json", timeout=30)
        return json.loads(out.stdout).get("snapshot") or {}

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
        self.fail(f"status predicate not met: phase={snap.get('phase')} reason={snap.get('reason')}")

    def tearDown(self):
        residue: dict = {}
        try:
            for pid, start in self.mine.items():
                kill_identity(pid, start, signal.SIGKILL)
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

    def type_host(self, line: str) -> None:
        self.term.send(line.encode() + b"\r")
        self.term.pump(0.3)

    def pid_file(self, name: str, marker: str, timeout: float = 20) -> int:
        path = self.marks / name
        ok = self.term.until(lambda: path.exists() and path.read_text().strip().isdecimal(), timeout)
        self.assertTrue(ok, f"pid file {name} never written; screen:\n{self.term.text()[-600:]}")
        pid = int(path.read_text().strip())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        self.assertIn(marker.encode(), cmd)
        self.mine[pid] = ticks(pid)
        return pid

    def test_confirmed_kill_ends_jobs_keeps_the_daemon_and_enter_restarts_a_fresh_shell(self):
        m = self.marks
        started = self.cli("start", "--data-dir", str(self.data), "--omp", OMP, "--no-attach", "--timeout", "150")
        self.assertEqual(0, started.returncode, (started.stdout + started.stderr)[-800:])
        snap = self.wait_status(lambda s: s.get("phase") == "ready"
                                and (s.get("omp_isolation") or {}).get("state") not in (None, "pending"))
        panes = snap["panes"]
        manager0, worker0, shell0 = (self.ident(panes[p]) for p in ("manager_omp", "worker_omp", "host_shell"))
        backend0 = (snap["backend"]["process"]["pid"], snap["backend"]["process"]["start_ticks"])
        note(step="started", omp=self.omp_version, isolation=(snap.get("omp_isolation") or {}).get("state"),
             manager=manager0, worker=worker0, shell=shell0, backend=backend0)

        self.term = term = OuterTerminal([sys.executable, "-c", MAIN, "attach", "--data-dir", str(self.data)],
                                         self.env, str(self.project))
        self.assertTrue(term.until(lambda: "MANAGER OMP" in term.text() and "WORKER OMP" in term.text(), 40),
                        term.text()[-600:])
        term.send(PREFIX + b"3")
        term.pump(1.0)
        self.type_host(f"pwd > {m}/cwd1")
        self.type_host(f"sleep 7901 & echo $! > {m}/bg")
        bg = self.pid_file("bg", "7901")
        self.type_host(f"sh -c 'trap \"\" HUP TERM; echo $$ > {m}/ign; exec sleep 7902' &")
        ign = self.pid_file("ign", "7902")
        self.type_host(f"setsid -f sh -c 'echo $$ > {m}/dmn; exec sleep 7903' </dev/null >/dev/null 2>&1")
        daemon = self.pid_file("dmn", "7903")
        self.type_host(f"sh -c 'echo $$ > {m}/fg; exec sleep 7904'")
        fg = self.pid_file("fg", "7904")
        self.wait_status(lambda s: (s["panes"]["host_shell"].get("shell") or {}).get("parent_mode")
                         == "manual_foreground", 20)
        jobs = {"bg": bg, "ign": ign, "fg": fg}
        note(step="jobs", jobs=jobs, daemon=daemon,
             sessions={k: int(Path(f"/proc/{p}/stat").read_text().rsplit(")", 1)[1].split()[3]) for k, p in
                       {**jobs, "daemon": daemon}.items()})

        # -- Ctrl-] k: the confirmation is drawn; x cancels; Ctrl-] k then k confirms
        term.send(PREFIX + b"k")
        self.assertTrue(term.until(lambda: KILL_TEXT in term.text(), 10), term.text()[-800:])
        term.send(b"x")
        self.assertTrue(term.until(lambda: KILL_TEXT not in term.text(), 10))
        term.pump(1.0)
        self.assertTrue(all(ticks(p) == self.mine[p] for p in jobs.values()), "the cancelled confirmation killed")
        self.assertTrue(self.status()["panes"]["host_shell"]["alive"])
        term.send(PREFIX + b"k")
        self.assertTrue(term.until(lambda: KILL_TEXT in term.text(), 10))
        t0 = time.monotonic()
        term.send(b"k")
        exited = self.wait_status(lambda s: s["panes"]["host_shell"].get("alive") is False, 30)
        host = exited["panes"]["host_shell"]
        note(step="killed", seconds=round(time.monotonic() - t0, 2), exit_status=host.get("exit_status"),
             kill=host.get("kill"), phase=exited["phase"])
        for name, pid in jobs.items():
            self.assertNotEqual(self.mine[pid], ticks(pid), f"{name} ({pid}) survived the kill")
        self.assertNotEqual(str(shell0[1]), ticks(shell0[0]), "the host shell survived")
        self.assertEqual(self.mine[daemon], ticks(daemon), "the setsid daemon was killed")
        self.assertEqual(host["kill"]["survivors"], [])
        self.assertNotIn(daemon, host["kill"]["signalled"])
        self.assertTrue(term.until(lambda: "host terminal 종료됨" in term.text(), 10), term.text()[-800:])
        kill_identity(daemon, self.mine[daemon], signal.SIGKILL)  # the probe's own daemon, by exact identity
        deadline = time.monotonic() + 5
        while ticks(daemon) == self.mine[daemon] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertNotEqual(self.mine[daemon], ticks(daemon))

        # -- Enter restarts a fresh shell
        term.send(b"\r")
        fresh = self.wait_status(lambda s: s["panes"]["host_shell"].get("alive") is True
                                 and (s["panes"]["host_shell"].get("shell") or {}).get("parent_mode") == "manual_prompt",
                                 30)
        host = fresh["panes"]["host_shell"]
        shell1 = self.ident(host)
        note(step="restarted", shell=shell1, generation=host.get("generation"), owner=host.get("input_owner"),
             restart=(host.get("restart") or {}).get("state"))
        self.assertNotEqual(shell0[0], shell1[0])
        self.assertEqual((host.get("generation"), host.get("input_owner")), (2, "user"))
        self.assertTrue(term.until(lambda: "host terminal 종료됨" not in term.text(), 10))
        self.type_host(f"pwd > {m}/cwd2; type wb-handoff > {m}/hook 2>&1; echo $$ > {m}/newpid")
        self.assertTrue(term.until(lambda: (m / "newpid").exists() and (m / "newpid").read_text().strip(), 20))
        self.assertEqual(int((m / "newpid").read_text()), shell1[0], "typed input did not reach the new shell")
        self.assertEqual((m / "cwd2").read_text(), (m / "cwd1").read_text())
        self.assertIn("wb-handoff", (m / "hook").read_text())
        self.type_host("wb-handoff")
        self.wait_status(lambda s: (s["panes"]["host_shell"].get("shell") or {}).get("parent_mode") == "control_wait",
                         20)
        term.send(PREFIX + b"h")
        handed = self.wait_status(lambda s: s["panes"]["host_shell"].get("input_owner") == "manager", 20)
        note(step="handoff", owner=handed["panes"]["host_shell"]["input_owner"])

        # -- OMPs and backend untouched
        for name, (pid, start) in (("manager", manager0), ("worker", worker0), ("backend", backend0)):
            self.assertEqual(str(start), ticks(pid), f"{name} changed")
        self.assertEqual(manager0, self.ident(handed["panes"]["manager_omp"]))
        self.assertEqual(worker0, self.ident(handed["panes"]["worker_omp"]))

        # -- detach, confirmed shutdown, nothing survives
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
        for pid, start in (manager0, worker0, shell1, backend0):
            self.assertNotEqual(str(start), ticks(pid))


if __name__ == "__main__":
    unittest.main()
