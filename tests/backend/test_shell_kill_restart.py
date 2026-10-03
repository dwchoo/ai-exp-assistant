"""C-D63: force-kill of the host shell (kill_pane) and restart of an exited host shell (restart_pane).

The backend runs with the real PersistentShell (Bash, plus a dash pane-level
case) in a temp dir; the two OMP panes are the fake OMP of test_restart_pane and
the isolation check is a recorder. Every process a test starts is recorded with
its start ticks and, if still alive at cleanup, ended through a pidfd.
"""
from pathlib import Path
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))

from test_restart_pane import FAKE_OMP, OK, alive, deny_signals  # noqa: E402

from workbench.backend.launcher import LaunchPlan  # noqa: E402
from workbench.backend.panes import ShellPane  # noqa: E402
from workbench.backend.paths import DataLayout, ensure_private_dir  # noqa: E402
from workbench.backend.service import Backend  # noqa: E402
from workbench.backend.ui_server import Held  # noqa: E402
from workbench.contracts.ui_v1 import Reason  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.terminal.shell_g2.prototype import ShellChoice  # noqa: E402

BASH = ShellChoice("bash", "/usr/bin/bash")
DASH = ShellChoice("sh", "/usr/bin/dash")

FORK_SETSID = (
    "import os, sys, time\n"
    "if os.fork() == 0:\n"
    "    os.setsid(); open(sys.argv[2], 'w').write(str(os.getpid())); time.sleep(int(sys.argv[3]))\n"
    "else:\n"
    "    open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(int(sys.argv[3]))\n"
)


def stat(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def session_members(sid):
    members = []
    for name in os.listdir("/proc"):
        if name.isdecimal():
            fields = stat(int(name))
            if fields is not None and fields[0] not in "ZX" and int(fields[3]) == sid:
                members.append(int(name))
    return members


def ports(shell):
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": 1, "ownerEpoch": shell.snapshot()["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "a" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


class Owned:
    """Processes a test started, identified by pid + start ticks + a cmdline marker."""

    def __init__(self, case):
        self.case = case
        self.fds = {}

    def adopt(self, pid, marker):
        fd = os.pidfd_open(pid)  # names this process across its exec
        deadline = time.monotonic() + 5
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        while marker.encode() not in cmdline and time.monotonic() < deadline:
            time.sleep(0.01)  # forked by the shell, not yet exec'd
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        if marker.encode() not in cmdline:
            os.close(fd)
            self.case.fail(f"pid {pid} is not the process started by the test: {cmdline!r}")
        self.fds[pid] = fd
        return pid

    def cleanup(self):
        for pid, fd in self.fds.items():
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(fd)
        deadline = time.monotonic() + 3
        while any(alive(pid) for pid in self.fds) and time.monotonic() < deadline:
            time.sleep(0.02)


class ShellKillRestartTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw17-shk-")
        self.addCleanup(self._dir.cleanup)
        self.root = root = Path(self._dir.name)
        self.project, home = root / "p", root / "h"
        self.project.mkdir()
        home.mkdir()
        fake = root / "omp"
        fake.write_text(FAKE_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        self.record = root / "panes.jsonl"
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
                    "FAKE_PANE_RECORD": str(self.record), "CW63_MARK": "start-env"}
        plan = LaunchPlan(BASH, str(fake), "omp/18.4.4", "/x/bridge.ts", ())
        patcher = mock.patch("workbench.backend.service.check_isolation",
                             lambda command, **kw: {"role": kw["role"], **OK})
        patcher.start()
        self.addCleanup(patcher.stop)
        layout = DataLayout(root / "d")
        ensure_private_dir(layout.root)
        self.backend = Backend(layout, plan, project_dir=str(self.project), environment=self.env)
        self.owned = Owned(self)
        self.addCleanup(self.close)
        self.backend._open()
        self.wait(lambda: self.backend.phase == "ready", "both fake OMPs registered")
        self.wait(lambda: self.mode() == "manual_prompt", "first prompt")

    def close(self):
        pids = [getattr(pane, "pid", None) for pane in self.backend.panes.values()]
        try:
            self.backend._close()
        finally:
            self.owned.cleanup()
        for pid in filter(None, pids):
            self.assertFalse(alive(pid), f"{pid} survived close")
            self.assertEqual(session_members(pid), [])

    # -- helpers ---------------------------------------------------------
    def wait(self, predicate, what, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.backend._tick(0.02)
            if predicate():
                return
        self.fail(f"timed out waiting for {what}; phase={self.backend.phase} reason={self.backend.reason} "
                  f"shell={self.backend.shell.shell_state()}")

    def mode(self):
        return self.backend.shell.state["parent_mode"]

    def type(self, line):
        refused = self.backend.admit(PaneId.HOST_SHELL, (line + "\r").encode(), "input")
        self.assertIsNone(refused, line)

    def pid_from(self, path, marker):
        self.wait(lambda: path.exists() and path.read_text().strip().isdecimal(), f"pid file {path.name}")
        return self.owned.adopt(int(path.read_text()), marker)

    def omps(self):
        return {p: (self.backend.panes[p], self.backend.panes[p].pid) for p in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP)}

    def assert_omps_untouched(self, before):
        for pane_id, (pane, pid) in before.items():
            self.assertIs(self.backend.panes[pane_id], pane)
            self.assertTrue(alive(pid), f"{pane_id.value} was touched")

    # -- tests -----------------------------------------------------------
    def test_kill_ends_jobs_and_foreground_but_not_setsid_processes_then_restart_is_fresh(self):
        root, omps = self.root, self.omps()
        old = self.backend.shell
        self.type(f"pwd > {root}/cwd1; printf %s \"$CW63_MARK\" > {root}/env1")
        self.type(f"sleep 6001 & echo $! > {root}/bg")
        bg = self.pid_from(root / "bg", "6001")
        self.type(f"(sleep 6002; true) & echo $! > {root}/sub")
        sub = self.pid_from(root / "sub", "")
        self.type(f"setsid sh -c 'echo $$ > {root}/daemon; exec sleep 6003' >/dev/null 2>&1 &")
        daemon = self.pid_from(root / "daemon", "6003")
        (root / "fork.py").write_text(FORK_SETSID)
        self.type(f"{sys.executable} {root}/fork.py {root}/pyparent {root}/pychild 6004 &")
        pyparent = self.pid_from(root / "pyparent", "6004")
        pychild = self.pid_from(root / "pychild", "6004")
        self.wait(lambda: self.mode() == "manual_prompt", "prompt after the background jobs")
        self.type(f"sh -c 'echo $$ > {root}/fg; exec sleep 6005'")
        fg = self.pid_from(root / "fg", "6005")
        self.wait(lambda: self.mode() == "manual_foreground", "a foreground job")
        self.assertEqual(int(stat(daemon)[3]), daemon)
        self.assertEqual(int(stat(pychild)[3]), pychild)
        in_session = session_members(old.pid)
        for pid in (old.pid, bg, sub, pyparent, fg):
            self.assertIn(pid, in_session)

        result = self.backend.kill_pane(PaneId.HOST_SHELL)

        self.assertEqual((result["pane"], result["killed"], result["input_owner"], result["manager_owned"],
                          result["manager_command_in_flight"], result["manager_command"]),
                         ("host_shell", True, "user", False, False, None))
        self.assertEqual(result["process"]["pid"], old.pid)
        self.assertEqual(result["survivors"], [])
        for pid in (old.pid, bg, sub, pyparent, fg):
            self.assertFalse(alive(pid), f"{pid} survived the kill")
            self.assertIn(pid, result["signalled"])
        self.assertEqual(session_members(old.pid), [], "a member of the shell session survived")
        self.assertTrue(alive(daemon) and alive(pychild), "a setsid process was signalled")
        self.assertNotIn(daemon, result["signalled"])
        self.assertNotIn(pychild, result["signalled"])
        self.assertIn({"pid": pychild, "session": pychild}, result["left_session"])
        self.assert_omps_untouched(omps)
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual((info["alive"], info["exit_status"]), (False, result["exit_status"]))
        self.assertIsNotNone(info["exit_status"])
        self.assertEqual(info["kill"]["signalled"], result["signalled"])
        self.assertEqual(self.backend.phase, "degraded")
        self.assertIn("host_shell", self.backend.reason)
        self.assertEqual(self.backend.admit(PaneId.HOST_SHELL, b"x", "input")[0], Reason.PANE_UNAVAILABLE)
        for action in (self.backend.takeover_request, self.backend.handoff):
            with self.assertRaises(Held) as held:
                action()
            self.assertEqual(held.exception.reason, Reason.PANE_UNAVAILABLE)
        with self.assertRaises(Held) as held:
            self.backend.kill_pane(PaneId.HOST_SHELL)
        self.assertEqual(held.exception.reason, Reason.PANE_EXITED)
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual(record["kills"][-1]["process"]["pid"], old.pid)
        for _ in range(10):
            self.backend._tick(0.01)  # the loop keeps running on the exited pane

        restarted = self.backend.restart_pane(PaneId.HOST_SHELL)

        new = self.backend.shell
        self.assertIsNot(new, old)
        self.assertIs(self.backend.panes[PaneId.HOST_SHELL], new)
        self.assertEqual((restarted["pane"], restarted["restarted"], restarted["input_owner"]),
                         ("host_shell", True, "user"))
        self.assertEqual((restarted["session_id"], restarted["generation"]), (new.session_id, old.generation + 1))
        self.assertNotEqual(new.session_id, old.session_id)
        self.assertNotEqual(new.pid, old.pid)
        self.assertEqual(int(stat(new.pid)[3]), new.pid, "the new shell leads its own session")
        self.assertEqual(int(stat(new.pid)[1]), os.getpid(), "the backend is the new shell's parent")
        self.wait(lambda: self.mode() == "manual_prompt", "a fresh prompt")
        self.wait(lambda: self.backend.phase == "ready", "ready again")
        state = new.shell_state()
        self.assertEqual((state["input_owner"], state["takeover_requested"], state["takeover_confirmed"],
                          state["request_id"], state["unknown"]), ("user", False, False, None, []))
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual((info["alive"], info["exit_status"], info["manager_command_in_flight"],
                          info["manager_command"], info["kill"]), (True, None, False, None, None))
        self.assertEqual(info["restart"]["state"], "restarted")
        self.assertEqual(info["restart"]["previous"]["process"]["pid"], old.pid)
        self.assertEqual(info["generation"], old.generation + 1)
        self.type(f"pwd > {root}/cwd2; printf %s \"$CW63_MARK\" > {root}/env2; "
                  f"type wb-handoff >/dev/null && echo yes > {root}/hook2")
        self.wait(lambda: all((root / n).exists() and (root / n).read_text() for n in ("cwd2", "env2", "hook2")),
                  "the new shell's cwd, env and hook")
        self.assertEqual((root / "cwd2").read_text(), (root / "cwd1").read_text())
        self.assertEqual((root / "env2").read_text(), "start-env")
        # The wb-handoff hook works in the new shell: the manager can take it over.
        self.type("wb-handoff")
        self.wait(lambda: self.mode() == "control_wait", "control wait in the new shell")
        self.assertEqual(self.backend.handoff()["input_owner"], "manager")
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual(record["restarts"][-1]["pane"], "host_shell")
        self.assertEqual(record["restarts"][-1]["process"]["pid"], new.pid)
        self.assertEqual(record["processes"]["host_shell"]["pid"], new.pid)
        with self.assertRaises(Held) as held:
            self.backend.restart_pane(PaneId.HOST_SHELL)
        self.assertEqual(held.exception.reason, Reason.PANE_ALIVE)
        self.assertFalse(Path(old.shell._transport._init_dir.name).exists(), "old shell init dir leaked")
        self.assert_omps_untouched(omps)

    def test_manager_owned_shell_with_a_command_in_flight_is_killed_and_the_command_is_not_a_success(self):
        root = self.root
        self.type("wb-handoff")
        self.wait(lambda: self.mode() == "control_wait", "control wait")
        self.assertEqual(self.backend.handoff()["input_owner"], "manager")
        shell = self.backend.shell
        control, automation = ports(shell.shell)
        shell.shell.submit(control, f"echo $$ > {root}/run; exec sleep 6011", automation)
        run = self.pid_from(root / "run", "6011")
        self.wait(lambda: shell.state["lifecycle"]["experiment_started"], "the managed run started")
        supervisor = shell.state["lifecycle"]["supervisor_pid"]
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual((info["input_owner"], info["manager_command_in_flight"]), ("manager", True))
        # A user takeover request is not needed: the confirmed kill proceeds whatever the owner is.
        result = self.backend.kill_pane(PaneId.HOST_SHELL)

        self.assertEqual((result["input_owner"], result["manager_owned"], result["manager_command_in_flight"]),
                         ("manager", True, True))
        command = result["manager_command"]
        self.assertEqual((command["request_id"], command["outcome"], command["task_success"], command["reason"]),
                         (control["payload"]["requestId"], "unknown", None, "host_shell_killed"))
        for pid in (shell.pid, supervisor, run):
            self.assertFalse(alive(pid), f"{pid} survived")
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual((info["alive"], info["manager_command_in_flight"]), (False, False))
        self.assertEqual(info["manager_command"]["outcome"], "unknown")
        self.assertIn("host_shell_killed", info["shell"]["unknown"])
        self.assertEqual(info["shell"]["phase"], "unknown")
        self.assertIsNone(shell.state["task_success"])
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual(record["kills"][-1]["manager_command"]["outcome"], "unknown")

        restarted = self.backend.restart_pane(PaneId.HOST_SHELL)
        self.assertEqual(restarted["input_owner"], "user")
        self.wait(lambda: self.mode() == "manual_prompt", "a fresh prompt")
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual((info["input_owner"], info["manager_command_in_flight"], info["manager_command"]),
                         ("user", False, None))
        self.assertEqual((info["shell"]["request_id"], info["shell"]["takeover_requested"],
                          info["shell"]["parent_mode"]), (None, False, "manual_prompt"))
        self.assertEqual(info["restart"]["previous"]["manager_command"]["outcome"], "unknown")
        self.assertEqual(info["restart"]["previous"]["input_owner"], "manager")
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"true\r", "input"))

    def test_shell_exit_is_reaped_refused_for_input_and_restart_ends_the_members_it_left(self):
        root, omps = self.root, self.omps()
        old = self.backend.shell
        self.type(f"sleep 6021 & echo $! > {root}/left")
        left = self.pid_from(root / "left", "6021")
        self.wait(lambda: self.mode() == "manual_prompt", "prompt")
        self.type("exit 3")
        self.wait(lambda: old._exited, "the shell exit")
        self.wait(lambda: self.backend.phase == "degraded", "degraded")
        self.assertIn("host_shell", self.backend.reason)
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual((info["alive"], info["exit_status"], info["kill"]), (False, 3, None))
        self.assertIsNone(stat(old.pid), "the exited shell is reaped, not left a zombie")
        self.assertEqual(self.backend.admit(PaneId.HOST_SHELL, b"x", "input")[0], Reason.PANE_UNAVAILABLE)
        with self.assertRaises(Held) as held:
            self.backend.kill_pane(PaneId.HOST_SHELL)
        self.assertEqual(held.exception.reason, Reason.PANE_EXITED)
        self.assertTrue(alive(left), "the exit itself does not end background jobs")

        self.backend.restart_pane(PaneId.HOST_SHELL)

        self.assertFalse(alive(left), "a member left in the exited shell's session survived the restart")
        self.assertEqual(self.backend.shell.restart["previous"]["exit_status"], 3)
        self.wait(lambda: self.mode() == "manual_prompt", "a fresh prompt")
        self.wait(lambda: self.backend.phase == "ready", "ready again")
        self.assert_omps_untouched(omps)

    def test_refusals_omp_panes_shutdown_and_spawn_failure(self):
        omps, shell = self.omps(), self.backend.shell
        for pane_id in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP):
            with self.assertRaises(Held) as held:
                self.backend.kill_pane(pane_id)
            self.assertEqual(held.exception.reason, Reason.PANE_NOT_KILLABLE)
            self.assertTrue(held.exception.detail)
        with self.assertRaises(Held) as held:
            self.backend.restart_pane(PaneId.HOST_SHELL)
        self.assertEqual(held.exception.reason, Reason.PANE_ALIVE)
        self.assert_omps_untouched(omps)
        self.assertIs(self.backend.shell, shell)
        self.assertTrue(shell.alive())

        self.backend._shutdown_confirmed = True
        try:
            with self.assertRaises(Held) as held:
                self.backend.kill_pane(PaneId.HOST_SHELL)
            self.assertEqual(held.exception.reason, Reason.BACKEND_SHUTDOWN)
        finally:
            self.backend._shutdown_confirmed = False
        self.assertTrue(shell.alive())

        self.backend.kill_pane(PaneId.HOST_SHELL)
        self.backend._stop_signal = signal.SIGTERM
        try:
            with self.assertRaises(Held) as held:
                self.backend.restart_pane(PaneId.HOST_SHELL)
            self.assertEqual(held.exception.reason, Reason.BACKEND_SHUTDOWN)
        finally:
            self.backend._stop_signal = None
        self.assertIs(self.backend.shell, shell)

        with mock.patch("workbench.backend.service.ShellPane", side_effect=OSError(24, "out of ptys")):
            with self.assertRaises(Held) as held:
                self.backend.restart_pane(PaneId.HOST_SHELL)
        self.assertEqual(held.exception.reason, Reason.RESTART_FAILED)
        self.assertIn("out of ptys", held.exception.detail)
        self.assertIs(self.backend.shell, shell)
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual((info["alive"], info["restart"]["state"]), (False, "failed"))
        self.assertEqual(self.backend.phase, "degraded")
        self.backend.restart_pane(PaneId.HOST_SHELL)  # the failure is not sticky
        self.wait(lambda: self.backend.phase == "ready", "ready after the retry")
        self.assertEqual(self.backend.shell.restart["count"], 1)
        self.assert_omps_untouched(omps)

    def test_concurrent_kill_and_restart_requests_are_serialised(self):
        def race(calls):
            barrier = threading.Barrier(len(calls))
            out = [None] * len(calls)

            def run(index, call):
                barrier.wait()
                try:
                    out[index] = ("ok", call())
                except Held as held:
                    out[index] = ("held", held.reason)
            threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertFalse(any(t.is_alive() for t in threads))
            return out

        first = self.backend.shell
        kills = race([lambda: self.backend.kill_pane(PaneId.HOST_SHELL)] * 2)
        self.assertEqual(sorted(kind for kind, _ in kills), ["held", "ok"], kills)
        self.assertIn([v for k, v in kills if k == "held"][0], {Reason.KILL_IN_PROGRESS, Reason.PANE_EXITED})
        self.assertFalse(alive(first.pid))
        restarts = race([lambda: self.backend.restart_pane(PaneId.HOST_SHELL)] * 2)
        self.assertEqual(sorted(kind for kind, _ in restarts), ["held", "ok"], restarts)
        self.assertIn([v for k, v in restarts if k == "held"][0], {Reason.RESTART_IN_PROGRESS, Reason.PANE_ALIVE})
        second = self.backend.shell
        self.assertEqual(second.generation, first.generation + 1)
        self.wait(lambda: self.mode() == "manual_prompt", "prompt")
        mixed = race([lambda: self.backend.kill_pane(PaneId.HOST_SHELL),
                      lambda: self.backend.restart_pane(PaneId.HOST_SHELL)])
        self.assertEqual(mixed[0][0], "ok", mixed)
        self.assertEqual(mixed[1], ("held", mixed[1][1]))
        self.assertIn(mixed[1][1], {Reason.PANE_ALIVE, Reason.RESTART_IN_PROGRESS})
        self.assertIs(self.backend.shell, second)
        self.assertFalse(alive(second.pid))

    def test_repeated_kill_restart_cycles_leak_no_descriptor_or_process(self):
        def children():
            found = set()
            for task in os.listdir("/proc/self/task"):
                try:
                    found.update(int(p) for p in Path(f"/proc/self/task/{task}/children").read_text().split())
                except OSError:
                    pass
            return found

        def cycle(index):
            shell = self.backend.shell
            self.type(f"sleep {6040 + index} &")
            self.wait(lambda: len(session_members(shell.pid)) >= 2, "a background job")
            self.backend.kill_pane(PaneId.HOST_SHELL)
            self.assertEqual(session_members(shell.pid), [])
            self.backend.restart_pane(PaneId.HOST_SHELL)
            self.wait(lambda: self.mode() == "manual_prompt", "prompt")
            self.assertFalse(Path(shell.shell._transport._init_dir.name).exists())
            return shell

        cycle(0)  # warm-up: lazy imports and first-use descriptors
        self.wait(lambda: self.backend.phase == "ready", "ready")
        fds, kids = len(os.listdir("/proc/self/fd")), len(children())
        olds = [cycle(index) for index in range(1, 6)]
        self.wait(lambda: self.backend.phase == "ready", "ready")
        self.assertEqual(len(os.listdir("/proc/self/fd")), fds, "descriptor leak across kill/restart cycles")
        self.assertEqual(len(children()), kids, "child process leak across kill/restart cycles")
        self.assertEqual(self.backend.shell.generation, olds[0].generation + 5)
        self.assertTrue(all(not alive(old.pid) for old in olds))
        self.assertEqual(len(json.loads(self.backend.layout.record.read_text())["restarts"]), 6)

    # -- C-D63 review R1/R4: unsignallable members never stop the backend --
    def test_kill_with_a_member_that_cannot_be_signalled_reports_it_and_keeps_everything_else(self):
        # The unsignallable members below ignore HUP/TERM, so neither bash nor the kernel ends them for us.
        root, omps = self.root, self.omps()
        old = self.backend.shell
        self.type(f"sh -c 'trap \"\" HUP TERM; echo $$ > {root}/denied; exec sleep 6081' &")
        denied = self.pid_from(root / "denied", "6081")
        self.type(f"sleep 6082 & echo $! > {root}/other")
        other = self.pid_from(root / "other", "6082")
        self.wait(lambda: self.mode() == "manual_prompt", "prompt after the jobs")
        with deny_signals({denied}) as calls:
            started = time.monotonic()
            result = self.backend.kill_pane(PaneId.HOST_SHELL)
            took = time.monotonic() - started
        self.assertTrue(calls, "the unsignallable member was never tried")
        self.assertTrue(result["killed"])
        self.assertEqual(result["survivors"], [{"pid": denied, "reason": "permission_denied"}])
        self.assertNotIn(denied, result["signalled"])
        self.assertFalse(alive(old.pid), "the parent shell survived")
        self.assertFalse(alive(other), "a member after the unsignallable one was not signalled")
        self.assertTrue(alive(denied))
        self.assertLess(took, 5.0)
        self.assert_omps_untouched(omps)
        for _ in range(5):
            self.backend._tick(0.01)  # the loop keeps serving
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual((info["alive"], info["kill"]["survivors"]), (False, result["survivors"]))
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual(record["kills"][-1]["survivors"], result["survivors"])
        restarted = self.backend.restart_pane(PaneId.HOST_SHELL)
        self.assertTrue(restarted["restarted"])
        self.wait(lambda: self.mode() == "manual_prompt", "a fresh prompt")
        self.assert_omps_untouched(omps)

    def test_restart_of_an_exited_shell_with_an_unsignallable_leftover_starts_the_new_shell(self):
        root, omps = self.root, self.omps()
        old = self.backend.shell
        self.type(f"sh -c 'trap \"\" HUP TERM; echo $$ > {root}/left; exec sleep 6083' &")
        left = self.pid_from(root / "left", "6083")
        self.wait(lambda: self.mode() == "manual_prompt", "prompt")
        self.type("exit 0")
        self.wait(lambda: old._exited, "the shell exit")
        with deny_signals({left}):
            restarted = self.backend.restart_pane(PaneId.HOST_SHELL)
            with self.assertRaises(Held) as held:
                self.backend.restart_pane(PaneId.HOST_SHELL)
        self.assertEqual(held.exception.reason, Reason.PANE_ALIVE, "restart did not take effect")
        self.assertTrue(restarted["restarted"])
        self.assertIsNot(self.backend.shell, old)
        self.assertEqual(restarted["survivors"], [{"pid": left, "reason": "permission_denied"}])
        self.assertEqual(self.backend.shell.restart["previous"]["survivors"], restarted["survivors"])
        self.assertTrue(alive(left))
        self.wait(lambda: self.mode() == "manual_prompt", "a fresh prompt")
        self.wait(lambda: self.backend.phase == "ready", "ready again")
        self.assert_omps_untouched(omps)

    def test_shutdown_with_an_unsignallable_shell_member_closes_every_pane_without_error(self):
        root = self.root
        shell, omps = self.backend.shell, self.omps()
        self.type(f"sh -c 'trap \"\" HUP TERM; echo $$ > {root}/denied; exec sleep 6084' &")
        denied = self.pid_from(root / "denied", "6084")
        self.type(f"sleep 6085 & echo $! > {root}/other")
        other = self.pid_from(root / "other", "6085")
        self.wait(lambda: self.mode() == "manual_prompt", "prompt")
        with deny_signals({denied}):
            result = self.backend._close()
        panes = {item["pane"]: item for item in result["panes"]}
        for item in panes.values():
            self.assertNotIn("error", item)
        self.assertEqual(panes["host_shell"]["survivors"], [{"pid": denied, "reason": "permission_denied"}])
        self.assertFalse(alive(shell.pid))
        self.assertFalse(alive(other))
        for _, pid in omps.values():
            self.assertFalse(alive(pid))

    def test_unexpected_os_error_in_kill_is_kill_failed_and_the_shell_keeps_running(self):
        shell = self.backend.shell
        with mock.patch.object(ShellPane, "_left_session", side_effect=OSError(5, "Input/output error")):
            with self.assertRaises(Held) as held:
                self.backend.kill_pane(PaneId.HOST_SHELL)
        self.assertEqual(held.exception.reason, Reason.KILL_FAILED)
        self.assertIn("Input/output error", held.exception.detail)
        self.assertTrue(shell.alive())
        self.backend.kill_pane(PaneId.HOST_SHELL)  # not sticky
        self.assertFalse(alive(shell.pid))

    def test_shutdown_request_after_a_kill_lists_no_shell_work(self):
        self.type("wb-handoff")
        self.wait(lambda: self.mode() == "control_wait", "control wait")
        self.backend.handoff()
        shell = self.backend.shell
        control, automation = ports(shell.shell)
        shell.shell.submit(control, f"echo $$ > {self.root}/run; exec sleep 6086", automation)
        self.pid_from(self.root / "run", "6086")
        self.wait(lambda: shell.state["lifecycle"]["experiment_started"], "the managed run started")
        kinds = [item["kind"] for item in self.backend.shutdown_request()["active"]]
        self.assertIn("shell_request", kinds)
        self.backend.kill_pane(PaneId.HOST_SHELL)
        for _ in range(5):
            self.backend._tick(0.01)
        active = self.backend.shutdown_request()["active"]
        self.assertEqual([item for item in active if item["kind"].startswith("shell")], [], active)

    def test_shutdown_request_after_a_shell_exit_with_a_request_in_flight_lists_no_shell_work(self):
        self.type("wb-handoff")
        self.wait(lambda: self.mode() == "control_wait", "control wait")
        self.backend.handoff()
        shell = self.backend.shell
        control, automation = ports(shell.shell)
        shell.shell.submit(control, f"echo $$ > {self.root}/run; exec sleep 6087", automation)
        run = self.pid_from(self.root / "run", "6087")
        self.wait(lambda: shell.state["lifecycle"]["experiment_started"], "the managed run started")
        os.kill(shell.pid, signal.SIGKILL)  # the parent shell dies on its own (not through kill_pane)
        self.wait(lambda: shell._exited, "the shell exit")
        active = self.backend.shutdown_request()["active"]
        self.assertEqual([item for item in active if item["kind"].startswith("shell")], [], active)
        self.assertEqual(shell.closed_request["outcome"], "unknown")
        del run


class ShellPaneKillTests(unittest.TestCase):
    def test_dash_and_bash_kill_end_the_parent_jobs_and_release_the_pty(self):
        for choice in (DASH, BASH):
            with self.subTest(choice.kind), tempfile.TemporaryDirectory(prefix="cw17-shp-") as directory:
                root = Path(directory)
                pane = ShellPane(choice, {"PATH": "/usr/bin:/bin", "HOME": directory, "LANG": "C.UTF-8"})
                owned = Owned(self)
                try:
                    def pump_until(predicate, timeout=10):
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            pane.pump()
                            if predicate():
                                return
                            time.sleep(0.02)
                        self.fail(f"timeout: {pane.shell_state()}")
                    self.assertIsNone(pane.admit(f"sleep 6051 & echo $! > {root}/bg\r".encode()))
                    pump_until(lambda: (root / "bg").exists() and (root / "bg").read_text().strip())
                    bg = owned.adopt(int((root / "bg").read_text()), "6051")
                    pump_until(lambda: pane.state["parent_mode"] == "manual_prompt")
                    self.assertIsNone(pane.admit(f"sh -c 'echo $$ > {root}/fg; exec sleep 6052'\r".encode()))
                    pump_until(lambda: (root / "fg").exists() and (root / "fg").read_text().strip())
                    fg = owned.adopt(int((root / "fg").read_text()), "6052")
                    result = pane.kill(grace=0.5)
                    self.assertEqual(result["survivors"], [])
                    for pid in (pane.pid, bg, fg):
                        self.assertFalse(alive(pid), pid)
                    self.assertEqual(session_members(pane.pid), [])
                    self.assertTrue(pane.exited() and pane._released)
                    self.assertEqual(pane.fds(), [])
                    self.assertTrue(pane.shell._transport._closed)
                    self.assertFalse(Path(pane.shell._transport._init_dir.name).exists())
                    self.assertEqual(pane.admit(b"x")[0], Reason.PANE_UNAVAILABLE)
                    self.assertEqual(pane._pins, {})
                finally:
                    pane.close()
                    owned.cleanup()


if __name__ == "__main__":
    unittest.main()
