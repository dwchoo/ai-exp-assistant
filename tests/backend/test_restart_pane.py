"""C-D62 (1): an exited manager/worker OMP pane restarts through ui_v1 as a new OMP session.

A fake OMP (a Python script on a real PTY) records its argv/env/cwd, registers
with the real G3 bridge using the injected role token, then waits for "exit" on
its terminal. The isolation check is replaced by a recorder (no RPC process).
"""
from pathlib import Path
import contextlib
import errno
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from workbench.backend.launcher import LaunchPlan
from workbench.backend.panes import OmpPane
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.service import Backend
from workbench.backend.ui_server import Held
from workbench.contracts.ui_v1 import Reason
from workbench.contracts.v1 import PaneId
from workbench.terminal.shell_g2.prototype import ShellChoice

FAKE_OMP = r'''#!{python}
import json, os, socket, sys, uuid
argv = sys.argv[1:]
if argv[:2] == ["config", "get"]:
    print("[]")
    sys.exit(0)
with open(os.environ["FAKE_PANE_RECORD"], "a") as stream:
    stream.write(json.dumps({{"argv": argv, "pid": os.getpid(), "cwd": os.getcwd(),
                              "auto_qa": "PI_AUTO_QA" in os.environ,
                              "bridge": {{k: v for k, v in os.environ.items() if k.startswith("WORKBENCH_G3_")}}}}) + "\n")
sock = socket.socket(socket.AF_UNIX)
sock.connect(os.environ["WORKBENCH_G3_BRIDGE_SOCKET"])
hello = {{"kind": "hello", "protocolVersion": 1, "token": os.environ["WORKBENCH_G3_TOKEN"],
          "role": os.environ["WORKBENCH_G3_ROLE"], "ompSessionId": str(uuid.uuid4()),
          "generation": int(os.environ["WORKBENCH_G3_GENERATION"]), "pid": os.getpid()}}
sock.sendall((json.dumps(hello) + "\n").encode())
ready = json.loads(sock.makefile("rb").readline())
print("fake omp", os.environ["WORKBENCH_G3_ROLE"], ready["kind"], flush=True)
for line in sys.stdin:
    if line.strip() == "exit":
        break
    if line.startswith("spawn "):
        # A member of this OMP session that ignores TERM/HUP, so it outlives the OMP.
        import subprocess
        child = subprocess.Popen(["sh", "-c", "trap '' TERM HUP; exec sleep 6091"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(line.split(" ", 1)[1].strip(), "w") as stream:
            stream.write(str(child.pid))
'''

OK = {"state": "ok", "ok": True, "leaks": [], "warnings": [], "error": None}


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] not in "ZX"
    except OSError:
        return False


def pidfd_pid(fd: int) -> int | None:
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
        if line.startswith("Pid:"):
            return int(line.split()[1])
    return None


@contextlib.contextmanager
def deny_signals(pids):
    """pidfd_send_signal raises EPERM for ``pids`` (a member now owned by another user, e.g. under sudo)."""
    real, denied = signal.pidfd_send_signal, []

    def send(fd, signum, *args):
        pid = pidfd_pid(fd)
        if pid in pids:
            denied.append((pid, signum))
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real(fd, signum, *args)
    with mock.patch("workbench.backend.panes.signal.pidfd_send_signal", send):
        yield denied


class RestartPaneTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw17-rst-")
        self.addCleanup(self._dir.cleanup)
        self.root = root = Path(self._dir.name)
        self.project, home = root / "p", root / "h"
        self.project.mkdir()
        home.mkdir()
        fake = root / "omp"
        fake.write_text(FAKE_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        self.record = root / "panes.jsonl"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
               "FAKE_PANE_RECORD": str(self.record), "PI_AUTO_QA": "1"}
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), str(fake), "omp/18.4.4", "/x/bridge.ts",
                          ("--plan-arg",))
        self.checks: list[tuple[str, list[str], dict[str, str]]] = []
        self.gate = threading.Event()
        self.gate.set()
        patcher = mock.patch("workbench.backend.service.check_isolation", self.fake_check)
        patcher.start()
        self.addCleanup(patcher.stop)
        layout = DataLayout(root / "d")
        ensure_private_dir(layout.root)
        self.backend = Backend(layout, plan, project_dir=str(self.project), environment=env)
        self.started: list[int] = []
        self.addCleanup(self.close)
        self.backend._open()
        self.wait(lambda: self.backend.phase == "ready", "both fake OMPs registered")
        self.wait(lambda: self.backend.omp_isolation["state"] == "ok", "start-up isolation result")

    def fake_check(self, command, *, cwd, environment, role, allowed_skills, omp_version, cancel):
        self.checks.append((role, list(command), dict(environment)))
        self.gate.wait(10)
        return {"role": role, **OK}

    def close(self):
        self.gate.set()
        pids = [entry["pid"] for entry in self.invocations()]
        self.backend._close()
        for pid in pids:
            self.assertFalse(alive(pid), f"fake OMP {pid} survived close")

    def invocations(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines() if line.strip()]

    def wait(self, predicate, what, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.backend._tick(0.02)
            if predicate():
                return
        self.fail(f"timed out waiting for {what}; phase={self.backend.phase} reason={self.backend.reason}")

    def exit_pane(self, pane_id):
        pane = self.backend.panes[pane_id]
        self.assertIsNone(self.backend.admit(pane_id, b"exit\n", "input"))
        self.wait(lambda: pane.info()["alive"] is False, f"{pane_id.value} to exit")
        return pane

    def spawn_member(self, pane_id):
        """A process left in the OMP's own session; ended by the test through a pidfd."""
        path = self.root / f"member-{pane_id.value}"
        self.assertIsNone(self.backend.admit(pane_id, f"spawn {path}\n".encode(), "input"))
        self.wait(lambda: path.exists() and path.read_text().strip().isdecimal(), "the spawned member")
        pid = int(path.read_text())
        fd = os.pidfd_open(pid)
        deadline = time.monotonic() + 5
        while b"6091" not in Path(f"/proc/{pid}/cmdline").read_bytes() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(b"6091", Path(f"/proc/{pid}/cmdline").read_bytes())

        def end():
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(fd)
            limit = time.monotonic() + 3
            while alive(pid) and time.monotonic() < limit:
                time.sleep(0.02)
        self.addCleanup(end)
        return pid

    # -- C-D63 review R1: an unsignallable member never fails a restart or the shutdown --
    def test_restart_with_an_unsignallable_member_left_in_the_old_session_starts_the_new_omp(self):
        worker, shell = self.backend.panes[PaneId.WORKER_OMP], self.backend.shell
        member = self.spawn_member(PaneId.MANAGER_OMP)
        old = self.exit_pane(PaneId.MANAGER_OMP)
        self.assertTrue(alive(member))
        with deny_signals({member}) as calls:
            result = self.backend.restart_pane(PaneId.MANAGER_OMP)
        self.assertTrue(calls, "the leftover member was never tried")
        self.assertTrue(result["restarted"])
        self.assertEqual(result["survivors"], [{"pid": member, "reason": "permission_denied"}])
        new = self.backend.panes[PaneId.MANAGER_OMP]
        self.assertIsNot(new, old)
        self.assertEqual(new.restart["previous"]["survivors"], result["survivors"])
        self.assertTrue(alive(member))
        self.assertTrue(alive(worker.pid) and shell.alive())
        self.wait(lambda: self.backend.phase == "ready", "ready after the restart")

    def test_shutdown_with_an_unsignallable_member_and_a_refused_group_signal_raises_nothing(self):
        manager, worker = self.backend.panes[PaneId.MANAGER_OMP], self.backend.panes[PaneId.WORKER_OMP]
        member = self.spawn_member(PaneId.MANAGER_OMP)
        real_killpg = os.killpg

        def killpg(pgid, signum):
            if pgid == worker.pid:
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return real_killpg(pgid, signum)
        with deny_signals({member}), mock.patch("workbench.backend.panes.os.killpg", killpg):
            result = self.backend._close()
        panes = {item["pane"]: item for item in result["panes"]}
        for item in panes.values():
            self.assertNotIn("error", item)
        self.assertEqual(panes["manager_omp"]["survivors"], [{"pid": member, "reason": "permission_denied"}])
        self.assertEqual(panes["worker_omp"]["survivors"], [])
        self.assertFalse(alive(manager.pid))
        self.assertFalse(alive(worker.pid), "a refused group TERM left the worker OMP running")
        self.assertTrue(alive(member))

    def test_exited_pane_restarts_with_start_argv_env_cwd_and_reregisters(self):
        first_manager, first_worker = self.invocations()[0], self.invocations()[1]
        if first_manager["bridge"]["WORKBENCH_G3_ROLE"] != "manager":
            first_manager, first_worker = first_worker, first_manager
        worker = self.backend.panes[PaneId.WORKER_OMP]
        old = self.exit_pane(PaneId.MANAGER_OMP)
        self.wait(lambda: self.backend.phase == "degraded", "degraded after the exit")
        self.assertEqual(self.backend.reason, "pane_exited:manager_omp")
        checks_before = len(self.checks)

        result = self.backend.restart_pane(PaneId.MANAGER_OMP)

        new = self.backend.panes[PaneId.MANAGER_OMP]
        self.assertIsInstance(new, OmpPane)
        self.assertIsNot(new, old)
        self.assertIs(self.backend.panes[PaneId.WORKER_OMP], worker, "the other OMP pane is untouched")
        self.assertTrue(alive(worker.pid))
        self.assertNotEqual(new.pid, old.pid)
        self.assertNotEqual(new.session_id, old.session_id)
        self.assertEqual(new.generation, old.generation + 1)
        self.assertEqual(new.size, old.size)
        self.assertEqual((result["pane"], result["restarted"], result["session_id"], result["generation"]),
                         ("manager_omp", True, new.session_id, new.generation))
        self.assertEqual(result["process"]["pid"], new.pid)
        self.wait(lambda: self.backend.phase == "ready", "ready after the new OMP registered")
        self.assertIsNone(self.backend.reason)
        bridge = self.backend.bridge_state()["manager"]
        self.assertEqual((bridge["pid"], bridge["pid_matches_pane"]), (new.pid, True))
        self.assertTrue(self.backend.bridge_state()["worker"]["pid_matches_pane"])

        again = self.invocations()[-1]
        self.assertEqual(again["pid"], new.pid)
        self.assertEqual(again["argv"], first_manager["argv"])
        self.assertEqual(again["bridge"], first_manager["bridge"], "same role token, socket and generation env")
        self.assertNotEqual(again["bridge"]["WORKBENCH_G3_TOKEN"], first_worker["bridge"]["WORKBENCH_G3_TOKEN"])
        self.assertEqual(again["cwd"], str(self.project))
        self.assertFalse(again["auto_qa"] or first_manager["auto_qa"], "PI_AUTO_QA is stripped")
        self.assertIn("--plan-arg", again["argv"])
        self.assertFalse({"--resume", "-r", "--continue", "-c"} & set(again["argv"]), "a new session, no resume")
        overlay = again["argv"][again["argv"].index("--no-extensions") - 1]
        self.assertTrue(overlay.endswith("omp-isolation-manager.yml") and Path(overlay).exists())

        self.wait(lambda: len(self.checks) > checks_before and self.backend.omp_isolation["state"] == "ok",
                  "isolation re-check for the restarted role")
        role, command, _env = self.checks[-1]
        self.assertEqual(role, "manager")
        self.assertEqual(command, [self.backend.plan.omp, *first_manager["argv"]])
        self.assertEqual(sorted(self.backend.omp_isolation["roles"]), ["manager", "worker"])

        snapshot = self.backend.snapshot()
        self.assertEqual(snapshot["panes"]["manager_omp"]["session_id"], new.session_id)
        self.assertTrue(snapshot["panes"]["manager_omp"]["alive"])
        self.assertEqual(snapshot["panes"]["manager_omp"]["restart"]["previous"]["process"]["pid"], old.pid)
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual(record["phase"], "ready")
        self.assertEqual(record["processes"]["manager_omp"]["pid"], new.pid)
        self.assertEqual(record["restarts"][-1]["previous"]["process"]["pid"], old.pid)
        self.assertEqual(record["restarts"][-1]["process"]["pid"], new.pid)
        self.assertFalse(alive(old.pid))

    def test_restarted_pane_shows_isolation_pending_until_its_recheck_finishes(self):
        self.exit_pane(PaneId.WORKER_OMP)
        self.gate.clear()
        self.backend.restart_pane(PaneId.WORKER_OMP)
        self.wait(lambda: self.checks[-1][0] == "worker" and len(self.checks) == 3, "re-check started")
        isolation = self.backend.snapshot()["omp_isolation"]
        self.assertEqual((isolation["state"], isolation["checked"], isolation["rechecking"]),
                         ("pending", False, ["worker"]))
        self.assertIn("manager", isolation["roles"])
        # While the re-check runs a second exit of the same pane cannot start another restart.
        self.exit_pane(PaneId.WORKER_OMP)
        with self.assertRaises(Held) as held:
            self.backend.restart_pane(PaneId.WORKER_OMP)
        self.assertEqual(held.exception.reason, Reason.RESTART_IN_PROGRESS)
        self.gate.set()
        self.wait(lambda: self.backend.omp_isolation["state"] == "ok", "re-check finished")
        self.assertNotIn("rechecking", self.backend.omp_isolation)
        self.backend.restart_pane(PaneId.WORKER_OMP)
        self.wait(lambda: self.backend.phase == "ready", "ready after the second restart")

    def test_refusals_never_touch_a_live_omp_the_host_shell_or_shutdown(self):
        manager, worker, shell = (self.backend.panes[p] for p in PaneId)
        cases = [(PaneId.MANAGER_OMP, Reason.PANE_ALIVE), (PaneId.WORKER_OMP, Reason.PANE_ALIVE),
                 (PaneId.HOST_SHELL, Reason.PANE_ALIVE)]  # C-D63: only an exited host shell restarts
        for pane_id, reason in cases:
            with self.subTest(pane=pane_id.value), self.assertRaises(Held) as held:
                self.backend.restart_pane(pane_id)
            self.assertEqual(held.exception.reason, reason)
            self.assertTrue(held.exception.detail)
        self.assertEqual([self.backend.panes[p] for p in PaneId], [manager, worker, shell])
        self.assertTrue(alive(manager.pid) and alive(worker.pid) and shell.alive())
        self.assertEqual(self.backend.phase, "ready")
        self.assertEqual(len(self.invocations()), 2)

        self.exit_pane(PaneId.MANAGER_OMP)
        self.backend._shutdown_confirmed = True
        with self.assertRaises(Held) as held:
            self.backend.restart_pane(PaneId.MANAGER_OMP)
        self.assertEqual(held.exception.reason, Reason.BACKEND_SHUTDOWN)
        self.backend._shutdown_confirmed = False
        self.assertIs(self.backend.panes[PaneId.MANAGER_OMP], manager)
        self.assertEqual(len(self.invocations()), 2)

    def test_unopened_backend_has_no_pane_to_restart(self):
        with tempfile.TemporaryDirectory(prefix="cw17-rsu-") as directory:
            plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.4", "/x/bridge.ts")
            backend = Backend(DataLayout(Path(directory)), plan, project_dir=directory, environment={})
            with self.assertRaises(Held) as held:
                backend.restart_pane(PaneId.MANAGER_OMP)
            self.assertEqual(held.exception.reason, Reason.PANE_UNAVAILABLE)

    def test_spawn_failure_leaves_the_pane_exited_with_a_reason(self):
        old = self.exit_pane(PaneId.MANAGER_OMP)
        self.wait(lambda: self.backend.phase == "degraded", "degraded after the exit")
        with mock.patch("workbench.backend.panes.pty.fork", side_effect=OSError(24, "out of ptys")):
            with self.assertRaises(Held) as held:
                self.backend.restart_pane(PaneId.MANAGER_OMP)
        self.assertEqual(held.exception.reason, Reason.RESTART_FAILED)
        self.assertIn("out of ptys", held.exception.detail)
        self.assertIs(self.backend.panes[PaneId.MANAGER_OMP], old)
        info = self.backend.snapshot()["panes"]["manager_omp"]
        self.assertFalse(info["alive"])
        self.assertEqual(info["restart"]["state"], "failed")
        self.assertIn("out of ptys", info["restart"]["error"])
        self.assertEqual(self.backend.phase, "degraded")
        self.assertEqual(self.backend.admit(PaneId.MANAGER_OMP, b"x", "input")[0], Reason.PANE_UNAVAILABLE)
        self.assertNotIn("manager", self.backend.omp_isolation.get("rechecking") or [])
        # The failure is not sticky: the next request restarts the pane.
        self.backend.restart_pane(PaneId.MANAGER_OMP)
        self.wait(lambda: self.backend.phase == "ready", "ready after the retry")


if __name__ == "__main__":
    unittest.main()
