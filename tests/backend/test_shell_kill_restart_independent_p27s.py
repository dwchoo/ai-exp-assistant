"""Independent C-D63 backend checks (p27-shell-test-01): host shell force-kill (kill_pane) and restart.

Expectations were drafted from DECISIONS C-D63, CW-17 'Host shell 재시작·강제 종료' and the senior/UI worker
required_behavior before the implementation was read:

* kill_pane{host_shell} ends the shell and every process of its session: foreground job, background jobs, stopped
  jobs, nested subshell pipelines, process groups made by job control or by the processes themselves, and members
  that ignore SIGHUP/SIGTERM (escalation to SIGKILL, bounded) or fork new members when signalled;
* a process that left the session (setsid) is never signalled; one still below the shell is reported;
* signals reach only proven members (pidfds), never a bare PID; an unprovable shell identity signals nothing;
* a manager command in flight is closed as unknown, never a success; after the restart the owner is the user and
  control/takeover/handoff state is fresh;
* refusals (OMP pane, exited, in progress, shutdown, not attached) carry reasons and touch nothing;
* restart only after the exit; the new shell is started like the first one (rcfile/hook, wb-handoff, cwd, env) and
  inherits nothing the user changed in the old one;
* repeated kill/exit/restart cycles leak no descriptor, child, zombie or thread;
* the same over the real UiServer socket, across detach/attach, pipelined requests and shutdown.

The two OMP panes are the fake OMP of the p27q independent suite (no real OMP, no model). Every process a test starts
is identified by pidfd + start ticks + a cmdline marker and is killed in cleanup only through that pidfd.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import select
import signal
import socket
import sys
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_restart_pane_independent_p27q import _BackendCase, alive, fd_targets, own_children  # noqa: E402

from workbench.backend.ui_server import Held  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.ui_v1 import ClientType, Reason  # noqa: E402
from workbench.contracts.v1 import ContractError, PaneId  # noqa: E402

PY = sys.executable

RESPAWNER = r'''
import os, signal, sys, time
out = sys.argv[1]
def spawn(*_):
    pid = os.fork()
    if pid == 0:
        os.execv("/bin/sleep", ["sleep", "7106"])
    with open(out, "a") as stream:
        stream.write("%d\n" % pid)
for signum in (signal.SIGHUP, signal.SIGTERM):
    signal.signal(signum, spawn)
with open(sys.argv[2], "w") as stream:
    stream.write(str(os.getpid()))
while True:
    time.sleep(0.05)
'''

NEWGROUP = r'''
import os, sys, time
os.setpgid(0, 0)
open(sys.argv[1], "w").write(str(os.getpid()))
time.sleep(7109)
'''


def stat(pid: int) -> list[str] | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def live_procs() -> dict[int, list[str]]:
    out = {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            fields = stat(int(name))
            if fields is not None and fields[0] not in "ZX":
                out[int(name)] = fields
    return out


def session_members(sid: int) -> list[int]:
    return sorted(pid for pid, fields in live_procs().items() if int(fields[3]) == sid)


def group_members(pgids: set[int]) -> list[int]:
    return sorted(pid for pid, fields in live_procs().items() if int(fields[2]) in pgids)


def cmdline(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


def live_with_marker(pid: int, marker: str) -> bool:
    return alive(pid) and marker.encode() in cmdline(pid)


class Owned:
    """Processes a test started: pidfd + start ticks + cmdline marker; cleanup signals only those pidfds."""

    def __init__(self, case: unittest.TestCase):
        self.case = case
        self.items: dict[int, tuple[int, str]] = {}
        self.expected: list[tuple[Path, str]] = []  # pid files a test asked for, adopted late if a test failed early

    def expect(self, path: Path, marker: str) -> None:
        self.expected.append((path, marker))

    def adopt(self, pid: int, marker: str) -> int:
        fd = os.pidfd_open(pid)
        deadline = time.monotonic() + 5
        while marker.encode() not in cmdline(pid) and time.monotonic() < deadline:
            time.sleep(0.01)  # forked by the shell, not exec'd yet
        if marker.encode() not in cmdline(pid):
            os.close(fd)
            self.case.fail(f"pid {pid} is not the process this test started: {cmdline(pid)!r}")
        fields = stat(pid)
        self.items[pid] = (fd, fields[19] if fields else "")
        return pid

    def cleanup(self) -> list[int]:
        for path, marker in self.expected:
            try:
                pid = int(path.read_text().strip())
            except (OSError, ValueError):
                continue
            if pid not in self.items and alive(pid) and marker.encode() in cmdline(pid):
                try:
                    fd = os.pidfd_open(pid)
                except OSError:
                    continue
                fields = stat(pid)
                if fields is not None and marker.encode() in cmdline(pid):
                    self.items[pid] = (fd, fields[19])
                else:
                    os.close(fd)
        killed = []
        for pid, (fd, ticks) in self.items.items():
            fields = stat(pid)
            if fields is not None and fields[0] not in "ZX" and fields[19] == ticks:
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                    killed.append(pid)
                except ProcessLookupError:
                    pass
            os.close(fd)
        deadline = time.monotonic() + 5
        while any(alive(pid) for pid in killed) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.items.clear()
        return killed


class _ShellCase(_BackendCase):
    """The p27q backend fixture plus host-shell helpers and owned shell-started processes."""

    def setUp(self):
        self.start_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.start_cwd)  # restart_pane chdirs the backend (this process) to its start cwd
        super().setUp()
        self.owned = Owned(self)
        self.addCleanup(self._cleanup_owned)
        self.wait(lambda: self.mode() == "manual_prompt", "first host prompt")

    def _cleanup_owned(self):
        # Runs before the backend close (cleanups are LIFO): processes we started are gone either way.
        self.expected_residue = self.owned.cleanup()

    def mode(self) -> str:
        return self.backend.shell.state["parent_mode"]

    def type(self, line: str) -> None:
        refused = self.backend.admit(PaneId.HOST_SHELL, (line + "\r").encode(), "input")
        self.assertIsNone(refused, f"{line!r} refused: {refused}")

    def prompt(self, what="prompt") -> None:
        self.wait(lambda: self.mode() == "manual_prompt", what)

    def pid_from(self, name: str, marker: str) -> int:
        path = self.root / name
        self.owned.expect(path, marker)
        self.wait(lambda: path.exists() and path.read_text().strip().isdecimal(), f"pid file {name}")
        return self.owned.adopt(int(path.read_text().strip()), marker)

    def omps(self):
        return {p: (self.backend.panes[p], self.backend.panes[p].pid) for p in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP)}

    def assert_omps_untouched(self, before):
        for pane_id, (pane, pid) in before.items():
            self.assertIs(self.backend.panes[pane_id], pane, f"{pane_id.value} replaced")
            self.assertTrue(alive(pid), f"{pane_id.value} was touched")

    def held(self, call) -> Held:
        with self.assertRaises(Held) as caught:
            call()
        self.assertTrue(caught.exception.detail)
        return caught.exception

    def control_ports(self):
        shell = self.backend.shell.shell
        control = {"portVersion": 2, "kind": "ShellControl", "payload": {
            "parentPid": shell.parent_pid, "generation": 1, "ownerEpoch": shell.snapshot()["owner_epoch"],
            "requestId": str(uuid4()), "approvalHash": "b" * 64, "phase": "accepted"}}
        automation = {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}
        return control, automation

    def hand_to_manager(self):
        self.type("wb-handoff")
        self.wait(lambda: self.mode() == "control_wait", "control wait after wb-handoff")
        self.assertEqual(self.backend.handoff()["input_owner"], "manager")

    def probe(self, name: str) -> dict:
        """What a fresh shell must look like; written by the shell itself."""
        out = self.root / name
        self.type(f"{{ printf 'cwd=%s\\n' \"$PWD\"; printf 'home=%s\\n' \"$HOME\"; printf 'leak=%s\\n' \"${{P27S_LEAK-unset}}\"; "
                  f"printf 'hook=%s\\n' \"$(type -t wb-handoff)\"; printf 'alias=%s\\n' \"$(alias p27s_alias 2>&1 >/dev/null | wc -l)\"; "
                  f"printf 'noclobber=%s\\n' \"$(set -o | grep -c 'noclobber.*on')\"; "
                  f"printf 'funcs=%s\\n' \"$(declare -F | awk '{{print $3}}' | sort | tr '\\n' ,)\"; "
                  f"printf 'envnames=%s\\n' \"$(env | cut -d= -f1 | grep -v '^_$' | sort | tr '\\n' ,)\"; "
                  f"printf 'done\\n'; }} > {out}.tmp && mv {out}.tmp {out}")
        self.wait(lambda: out.exists() and out.read_text().endswith("done\n"), f"probe {name}")
        return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


class KillScopeTests(_ShellCase):
    def test_kill_ends_every_job_kind_and_group_including_signal_ignoring_and_respawning_members(self):
        root, omps, old = self.root, self.omps(), self.backend.shell
        (root / "respawn.py").write_text(RESPAWNER)
        (root / "newgroup.py").write_text(NEWGROUP)
        self.type(f"sleep 7101 & echo $! > {root}/bg")
        bg = self.pid_from("bg", "7101")
        self.prompt()
        self.type(f"( ( sh -c 'echo $$ > {root}/n1; exec sleep 7102' | cat ) | "
                  f"( sh -c 'echo $$ > {root}/n2; exec sleep 7103' | cat ) ) &")
        n1, n2 = self.pid_from("n1", "7102"), self.pid_from("n2", "7103")
        self.prompt()
        self.type(f"sh -c 'trap \"\" HUP TERM; echo $$ > {root}/ign; exec sleep 7104' &")
        ign = self.pid_from("ign", "7104")
        self.prompt()
        self.type(f"{PY} {root}/respawn.py {root}/respawned {root}/resp &")
        resp = self.pid_from("resp", "respawn.py")
        self.prompt()
        self.type(f"( {PY} {root}/newgroup.py {root}/ng; true ) &")
        ng = self.pid_from("ng", "newgroup.py")
        self.prompt()
        self.type(f"sh -c 'echo $$ > {root}/stop2; exec sleep 7108' & sleep 0.3; kill -STOP $!")
        stop2 = self.pid_from("stop2", "7108")
        self.prompt()
        self.wait(lambda: (stat(stop2) or ["?"])[0] == "T", "kill -STOP'd background job")
        # A job stopped by the terminal (Ctrl-Z) through job control.
        self.type(f"sh -c 'echo $$ > {root}/tstp; exec sleep 7107'")
        tstp = self.pid_from("tstp", "7107")
        self.wait(lambda: self.mode() == "manual_foreground", "foreground job to stop")
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"\x1a", "input"))
        self.wait(lambda: (stat(tstp) or ["?"])[0] == "T", "Ctrl-Z stopped job")
        self.prompt("prompt after Ctrl-Z")
        self.type(f"sh -c 'echo $$ > {root}/fg; exec sleep 7110'")
        fg = self.pid_from("fg", "7110")
        self.wait(lambda: self.mode() == "manual_foreground", "a foreground job")

        mine = {"bg": bg, "n1": n1, "n2": n2, "ign": ign, "resp": resp, "ng": ng, "stop2": stop2, "tstp": tstp,
                "fg": fg}
        pgids = {int(stat(pid)[2]) for pid in mine.values()}
        in_session = session_members(old.pid)
        for name, pid in mine.items():
            self.assertIn(pid, in_session, f"{name} is not in the shell session (fixture problem)")
        self.assertNotEqual(int(stat(ng)[2]), int(stat(ng)[3]), "newgroup.py did not get its own group")
        self.assertGreater(len(pgids), 3, f"expected several process groups, got {pgids}")
        session_before = set(in_session)

        started = time.monotonic()
        result = self.backend.kill_pane(PaneId.HOST_SHELL)
        elapsed = time.monotonic() - started

        self.assertEqual((result["pane"], result["killed"], result["survivors"]), ("host_shell", True, []))
        self.assertLess(elapsed, 8.0, "the escalation is not bounded")
        for name, pid in mine.items():
            self.assertFalse(alive(pid), f"{name} ({pid}) survived the kill")
        self.assertFalse(alive(old.pid))
        respawned_path = root / "respawned"
        respawned = [int(x) for x in respawned_path.read_text().split()] if respawned_path.exists() else []
        for pid in respawned:
            self.assertFalse(live_with_marker(pid, "7106"), f"member {pid} forked during the kill survived")
        self.assertEqual(session_members(old.pid), [], "a member of the shell session survived")
        self.assertEqual(group_members(pgids), [], "a member of a job process group survived")
        self.assertTrue(session_before <= set(result["signalled"]) | set(respawned),
                        f"not every session member was signalled: {session_before - set(result['signalled'])}")
        self.assertIsNone(stat(old.pid), "the killed shell is left a zombie")
        self.assert_omps_untouched(omps)
        self.assertEqual(self.backend.panes[PaneId.HOST_SHELL], old)
        self.assertFalse(self.backend.snapshot()["panes"]["host_shell"]["alive"])

    def test_setsid_processes_are_never_signalled_and_a_descendant_one_is_reported(self):
        root, old = self.root, self.backend.shell
        self.type(f"setsid -w sh -c 'echo $$ > {root}/d1; exec sleep 7201' & echo $! > {root}/d1w")
        d1 = self.pid_from("d1", "7201")
        waiter = self.pid_from("d1w", "setsid")
        self.prompt()
        self.type(f"setsid -f sh -c 'echo $$ > {root}/d2; exec sleep 7202' </dev/null >/dev/null 2>&1")
        d2 = self.pid_from("d2", "7202")
        self.prompt()
        self.type(f"sleep 7203 & echo $! > {root}/plain")
        plain = self.pid_from("plain", "7203")
        self.prompt()
        for pid in (d1, d2):
            self.assertEqual(int(stat(pid)[3]), pid, "fixture: setsid did not make its own session")
        self.assertEqual(int(stat(waiter)[3]), old.pid)

        result = self.backend.kill_pane(PaneId.HOST_SHELL)

        self.assertTrue(alive(d1) and alive(d2), "a process in its own session was killed")
        for pid in (d1, d2):
            self.assertNotIn(pid, result["signalled"])
            self.assertEqual(int(stat(pid)[3]), pid)
        self.assertFalse(alive(waiter), "the in-session setsid -w waiter survived")
        self.assertFalse(alive(plain))
        self.assertIn({"pid": d1, "session": d1}, result["left_session"],
                      "a setsid descendant of the shell is not reported")
        self.assertEqual(result["survivors"], [])
        record = json.loads(self.backend.layout.record.read_text())
        self.assertIn({"pid": d1, "session": d1}, record["kills"][-1]["left_session"])
        self.assertEqual(session_members(old.pid), [])
        # A later restart and backend close must not touch them either (cleanup kills them by pidfd).
        self.backend.restart_pane(PaneId.HOST_SHELL)
        self.prompt("fresh prompt")
        self.assertTrue(alive(d1) and alive(d2), "restart touched a setsid process")


class SignalIdentityTests(_ShellCase):
    def test_only_pidfds_of_proven_session_members_are_signalled_never_a_bare_pid(self):
        root, old = self.root, self.backend.shell
        self.type(f"sleep 7301 & echo $! > {root}/bg")
        bg = self.pid_from("bg", "7301")
        self.prompt()
        self.type(f"setsid -f sh -c 'echo $$ > {root}/dmn; exec sleep 7302' </dev/null >/dev/null 2>&1")
        daemon = self.pid_from("dmn", "7302")
        self.prompt()
        raw, via_pidfd = [], []
        real_kill, real_killpg, real_pidfd = os.kill, os.killpg, signal.pidfd_send_signal

        def kill(pid, sig):
            raw.append(("kill", pid, sig))
            return real_kill(pid, sig)

        def killpg(pgid, sig):
            raw.append(("killpg", pgid, sig))
            return real_killpg(pgid, sig)

        def pidfd_send(fd, sig, *args):
            try:
                pid = int(next(line.split()[1] for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
                               if line.startswith("Pid:")))
            except (OSError, StopIteration, ValueError):
                pid = None
            fields = stat(pid) if pid and pid > 0 else None
            via_pidfd.append((pid, None if fields is None else int(fields[3]), sig))
            return real_pidfd(fd, sig, *args)

        with mock.patch.object(os, "kill", kill), mock.patch.object(os, "killpg", killpg), \
                mock.patch.object(signal, "pidfd_send_signal", pidfd_send):
            result = self.backend.kill_pane(PaneId.HOST_SHELL)
        self.assertEqual([c for c in raw if c[2] != 0], [], "a signal was sent by bare PID/PGID")
        self.assertTrue(via_pidfd, "nothing was signalled through a pidfd")
        for pid, session, sig in via_pidfd:
            self.assertNotEqual(pid, daemon)
            if pid not in (None, -1, 0) and session is not None:
                self.assertEqual(session, old.pid, f"signal {sig} to {pid} in session {session}")
        self.assertIn(old.pid, {pid for pid, _, _ in via_pidfd})
        self.assertIn(bg, {pid for pid, _, _ in via_pidfd})
        self.assertTrue(alive(daemon))
        self.assertEqual(result["survivors"], [])

    def test_an_unprovable_shell_identity_refuses_the_kill_and_signals_nothing(self):
        root, shell = self.root, self.backend.shell
        self.type(f"sleep 7311 & echo $! > {root}/bg")
        bg = self.pid_from("bg", "7311")
        self.prompt()
        real = shell.ref
        sent = []
        real_pidfd = signal.pidfd_send_signal
        # Same pid number, different start time: what a recycled PID looks like to the backend.
        shell.ref = dataclasses.replace(real, start_ticks=real.start_ticks + 1)
        try:
            with mock.patch.object(signal, "pidfd_send_signal", lambda *a: (sent.append(a), real_pidfd(*a))[1]), \
                    mock.patch.object(os, "kill", side_effect=AssertionError("bare kill")), \
                    mock.patch.object(os, "killpg", side_effect=AssertionError("bare killpg")):
                refused = self.held(lambda: self.backend.kill_pane(PaneId.HOST_SHELL))
        finally:
            shell.ref = real
        self.assertEqual(refused.reason, Reason.KILL_FAILED)
        self.assertEqual(sent, [], "an unprovable identity was signalled")
        self.assertTrue(alive(shell.pid) and alive(bg))
        self.assertIs(self.backend.shell, shell)
        self.assertEqual(self.backend.kills, [])
        # Once the identity is proven again the kill works.
        self.assertEqual(self.backend.kill_pane(PaneId.HOST_SHELL)["survivors"], [])
        self.assertFalse(alive(bg))


class ManagerStateTests(_ShellCase):
    def test_manager_owned_idle_shell_is_killed_and_restarts_fresh_for_the_user(self):
        self.hand_to_manager()
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual((info["input_owner"], info["manager_command_in_flight"]), ("manager", False))

        result = self.backend.kill_pane(PaneId.HOST_SHELL)

        self.assertEqual((result["input_owner"], result["manager_owned"], result["manager_command_in_flight"],
                          result["manager_command"]), ("manager", True, False, None))
        for action in (self.backend.takeover_request, self.backend.takeover_confirm, self.backend.handoff):
            self.assertEqual(self.held(action).reason, Reason.PANE_UNAVAILABLE)
        restarted = self.backend.restart_pane(PaneId.HOST_SHELL)
        self.assertEqual(restarted["input_owner"], "user")
        self.prompt("fresh prompt")
        state = self.backend.shell.shell_state()
        self.assertEqual((state["input_owner"], state["takeover_requested"], state["takeover_confirmed"],
                          state["request_id"], state["parent_mode"]), ("user", False, False, None, "manual_prompt"))
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"true\r", "input"))
        # The manager can be handed the fresh shell again (control/handoff work from scratch).
        self.prompt()
        self.hand_to_manager()

    def test_a_pending_takeover_does_not_survive_kill_and_restart(self):
        self.hand_to_manager()
        self.backend.takeover_request()
        self.assertTrue(self.backend.shell.shell_state()["takeover_requested"])
        self.backend.kill_pane(PaneId.HOST_SHELL)
        self.backend.restart_pane(PaneId.HOST_SHELL)
        self.prompt("fresh prompt")
        state = self.backend.shell.shell_state()
        self.assertEqual((state["input_owner"], state["takeover_requested"], state["takeover_confirmed"]),
                         ("user", False, False))
        self.assertEqual(self.held(self.backend.takeover_confirm).reason, Reason.TAKEOVER_HELD)

    def test_a_manager_command_killed_right_after_its_launch_is_unknown_never_a_success(self):
        root = self.root
        self.hand_to_manager()
        shell = self.backend.shell
        control, automation = self.control_ports()
        shell.shell.submit(control, f"echo $$ > {root}/run; exec sleep 7401", automation)
        run = self.pid_from("run", "7401")  # killed before the test waits for experiment_started
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertTrue(info["manager_command_in_flight"])

        result = self.backend.kill_pane(PaneId.HOST_SHELL)

        self.assertTrue(result["manager_owned"] and result["manager_command_in_flight"])
        command = result["manager_command"]
        self.assertEqual((command["request_id"], command["outcome"], command["task_success"]),
                         (control["payload"]["requestId"], "unknown", None))
        self.assertFalse(alive(run))
        for _ in range(50):  # late control/result traffic must not turn it into a success
            self.backend._tick(0.01)
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual(info["manager_command"]["outcome"], "unknown")
        self.assertIsNone(info["manager_command"]["task_success"])
        self.assertIsNot(shell.state.get("task_success"), True)
        self.assertNotEqual(info["shell"]["phase"], "control_returned")
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual(record["kills"][-1]["manager_command"]["outcome"], "unknown")
        self.backend.restart_pane(PaneId.HOST_SHELL)
        info = self.backend.snapshot()["panes"]["host_shell"]
        self.assertEqual((info["input_owner"], info["manager_command"], info["manager_command_in_flight"]),
                         ("user", None, False))
        self.assertEqual(info["restart"]["previous"]["manager_command"]["outcome"], "unknown")


class RefusalTests(_ShellCase):
    def test_refusals_carry_reasons_and_touch_nothing(self):
        root, omps, shell = self.root, self.omps(), self.backend.shell
        self.type(f"sleep 7501 & echo $! > {root}/bg")
        bg = self.pid_from("bg", "7501")
        self.prompt()

        def untouched():
            self.assertIs(self.backend.shell, shell)
            self.assertTrue(alive(shell.pid) and alive(bg))
            self.assertEqual(self.backend.kills, [])
            self.assert_omps_untouched(omps)

        for pane_id in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP):
            self.assertEqual(self.held(lambda: self.backend.kill_pane(pane_id)).reason, Reason.PANE_NOT_KILLABLE)
        self.assertEqual(self.held(lambda: self.backend.restart_pane(PaneId.HOST_SHELL)).reason, Reason.PANE_ALIVE)
        for attr, value in (("_shutdown_confirmed", True), ("_stop_signal", signal.SIGTERM)):
            setattr(self.backend, attr, value)
            try:
                self.assertEqual(self.held(lambda: self.backend.kill_pane(PaneId.HOST_SHELL)).reason,
                                 Reason.BACKEND_SHUTDOWN)
            finally:
                setattr(self.backend, attr, False if attr == "_shutdown_confirmed" else None)
        untouched()

    def test_requests_during_a_running_kill_are_refused_as_in_progress(self):
        root, shell = self.root, self.backend.shell
        # A TERM/HUP-ignoring member keeps the kill busy for the whole grace period.
        self.type(f"sh -c 'trap \"\" HUP TERM; echo $$ > {root}/ign; exec sleep 7511' &")
        ign = self.pid_from("ign", "7511")
        self.prompt()
        out = {}

        def run():
            try:
                out["result"] = self.backend.kill_pane(PaneId.HOST_SHELL)
            except Exception as exc:  # reported below
                out["error"] = exc
        thread = threading.Thread(target=run)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not self.backend._shell_lock.locked() and time.monotonic() < deadline:
                time.sleep(0.001)
            time.sleep(0.1)
            self.assertTrue(thread.is_alive(), "the kill finished before the in-progress checks")
            self.assertEqual(self.held(lambda: self.backend.kill_pane(PaneId.HOST_SHELL)).reason,
                             Reason.KILL_IN_PROGRESS)
            self.assertEqual(self.held(lambda: self.backend.restart_pane(PaneId.HOST_SHELL)).reason,
                             Reason.RESTART_IN_PROGRESS)
        finally:
            thread.join(15)
        self.assertNotIn("error", out, out)
        self.assertEqual(out["result"]["survivors"], [])
        self.assertFalse(alive(ign))
        self.assertIs(self.backend.shell, shell)
        self.assertEqual(len(self.backend.kills), 1)
        self.assertEqual(self.held(lambda: self.backend.kill_pane(PaneId.HOST_SHELL)).reason, Reason.PANE_EXITED)


class FreshShellTests(_ShellCase):
    def test_restarted_shell_is_started_like_the_first_and_inherits_nothing_from_the_old_one(self):
        first = self.probe("probe1")
        self.assertIn(first["hook"], ("alias", "function"), "fixture: wb-handoff missing in the first shell")
        self.assertEqual(first["leak"], "unset")
        self.type(f"cd {self.root}; export P27S_LEAK=1; alias p27s_alias=true; set -o noclobber; "
                  "p27s_fn() { :; }")
        self.prompt()
        changed = self.probe("probe-changed")
        self.assertEqual((changed["cwd"], changed["leak"], changed["noclobber"]), (str(self.root), "1", "1"))
        self.backend.kill_pane(PaneId.HOST_SHELL)
        os.chdir(self.root)  # the backend's own cwd moved meanwhile: the new shell still starts where the first did
        self.backend.restart_pane(PaneId.HOST_SHELL)
        self.prompt("fresh prompt")
        second = self.probe("probe2")
        self.assertEqual(second, first, "the restarted shell differs from the first start")
        # wb-handoff of the new shell works end to end.
        self.prompt()
        self.hand_to_manager()

    def test_restart_is_refused_while_alive_and_accepted_after_any_kind_of_exit(self):
        for how in ("exit 7", "kill"):
            with self.subTest(how=how):
                shell = self.backend.shell
                self.assertEqual(self.held(lambda: self.backend.restart_pane(PaneId.HOST_SHELL)).reason,
                                 Reason.PANE_ALIVE)
                if how == "kill":
                    self.backend.kill_pane(PaneId.HOST_SHELL)
                else:
                    self.type(how)
                    self.wait(lambda: shell.exited(), "shell exit")
                    for _ in range(20):
                        self.backend._tick(0.01)
                    self.assertIs(self.backend.shell, shell, "the backend restarted the shell by itself")
                    self.assertEqual(self.backend.snapshot()["panes"]["host_shell"]["exit_status"], 7)
                result = self.backend.restart_pane(PaneId.HOST_SHELL)
                self.assertEqual((result["generation"], result["input_owner"]), (shell.generation + 1, "user"))
                self.assertNotEqual(result["session_id"], shell.session_id)
                self.prompt("fresh prompt")
                self.wait(lambda: self.backend.phase == "ready", "ready again")


class CycleLeakTests(_ShellCase):
    def test_kill_exit_restart_cycles_leak_no_fd_child_zombie_or_thread(self):
        def threads():
            return len(os.listdir("/proc/self/task"))

        def cycle(index):
            shell = self.backend.shell
            self.type(f"sleep {7600 + index} & sh -c 'trap \"\" HUP TERM; exec sleep {7650 + index}' &")
            self.wait(lambda: len(session_members(shell.pid)) >= 3, "background jobs")
            if index % 2:
                self.type("exit")
                self.wait(lambda: shell.exited(), "exit")
            else:
                self.backend.kill_pane(PaneId.HOST_SHELL)
            self.backend.restart_pane(PaneId.HOST_SHELL)
            self.prompt()
            self.assertEqual(session_members(shell.pid), [], f"cycle {index}: old session members left")
            self.assertEqual(shell._pins, {})
            self.assertFalse(Path(shell.shell._transport._init_dir.name).exists())
            return shell

        cycle(0)
        cycle(1)
        self.wait(lambda: self.backend.phase == "ready", "ready")
        fds, kids, nthreads = len(fd_targets()), own_children(), threads()
        olds = [cycle(i) for i in range(2, 8)]
        self.wait(lambda: self.backend.phase == "ready", "ready")
        for _ in range(10):
            self.backend._tick(0.01)
        self.assertEqual(len(fd_targets()), fds, "descriptor leak")
        self.assertEqual(own_children()["zombies"], [], "zombie leak")
        self.assertEqual(len(own_children()["live"]), len(kids["live"]), "child leak")
        self.assertEqual(threads(), nthreads, "thread leak")
        self.assertTrue(all(not alive(o.pid) for o in olds))
        self.assertEqual(self.backend.shell.generation, olds[-1].generation + 1)


class UiSocketShellTests(_ShellCase):
    """kill_pane / restart_pane through the real UiServer socket, with the backend loop in its own thread."""

    def setUp(self):
        super().setUp()
        self.start_ticker()
        self.sock = None
        self.connect()

    def connect(self):
        if self.sock is not None:
            self.sock.close()
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(str(self.backend.layout.ui_socket))
        self.addCleanup(self.sock.close)
        self.decoder, self.frames, self.counter = ui_v1.FrameDecoder(), [], getattr(self, "counter", 0)
        self.sock.sendall(ui_v1.encode_frame(ui_v1.hello("p27s")))
        self.assertEqual(self.next(lambda f: f.header.get("type") in ("welcome", "reject")).header["type"], "welcome")

    def pump(self, timeout=0.05):
        if select.select([self.sock], [], [], timeout)[0]:
            data = self.sock.recv(1 << 20)
            if data:
                self.frames.extend(self.decoder.feed(data))

    def next(self, predicate, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for i, frame in enumerate(self.frames):
                if predicate(frame):
                    return self.frames.pop(i)
            self.pump()
        self.fail("no matching frame")

    def request(self, kind, payload=b"", **fields) -> str:
        self.counter += 1
        rid = f"s{self.counter}"
        self.sock.sendall(ui_v1.encode_frame(ui_v1.request(kind, rid, **fields), payload))
        return rid

    def result(self, rid):
        return self.next(lambda f: f.header.get("type") == "result" and f.header.get("id") == rid).header

    def call(self, kind, payload=b"", **fields):
        return self.result(self.request(kind, payload, **fields))

    def host_state(self, predicate):
        return self.next(lambda f: f.header.get("type") == "state"
                         and predicate(f.header["snapshot"]["panes"]["host_shell"])).header["snapshot"]

    def test_contract_refusals_detach_attach_and_restart_over_the_socket(self):
        root, shell = self.root, self.backend.shell
        refused = self.call(ClientType.KILL_PANE, pane="host_shell")
        self.assertEqual((refused["ok"], refused["reason"]), (False, "not_attached"))
        self.assertTrue(alive(shell.pid))
        self.assertTrue(self.call(ClientType.ATTACH, size={"rows": 30, "cols": 100})["ok"])
        for pane in ("manager_omp", "worker_omp"):
            header = self.call(ClientType.KILL_PANE, pane=pane)
            self.assertEqual((header["ok"], header["reason"]), (False, "pane_not_killable"), header)
        for bad in ({"pane": "bogus"}, {}, {"pane": 3}):
            header = self.call(ClientType.KILL_PANE, **bad)
            self.assertEqual((header["ok"], header["reason"]), (False, "invalid_message"), header)
        header = self.call(ClientType.KILL_PANE, b"y", pane="host_shell")
        self.assertEqual((header["ok"], header["reason"]), (False, "invalid_message"), header)
        self.assertTrue(alive(shell.pid))
        self.assertTrue(self.call(ClientType.INPUT, f"sleep 7701 & echo $! > {root}/bg\r".encode(),
                                  pane="host_shell")["ok"])
        bg = self.pid_from("bg", "7701")

        ok = self.call(ClientType.KILL_PANE, pane="host_shell")

        self.assertTrue(ok["ok"], ok)
        self.assertEqual((ok["pane"], ok["killed"], ok["input_owner"], ok["manager_owned"], ok["survivors"]),
                         ("host_shell", True, "user", False, []))
        self.assertEqual(ok["process"]["pid"], shell.pid)
        self.assertFalse(alive(bg) or alive(shell.pid))
        state = self.host_state(lambda host: host.get("alive") is False)
        self.assertIsNotNone(state["panes"]["host_shell"]["kill"])
        refused = self.call(ClientType.INPUT, b"echo hi\r", pane="host_shell")
        self.assertEqual((refused["ok"], refused["reason"]), (False, "pane_unavailable"))
        again = self.call(ClientType.KILL_PANE, pane="host_shell")
        self.assertEqual((again["ok"], again["reason"]), (False, "pane_exited"))

        # detach, reconnect, attach: the exited state is kept and nothing restarted it meanwhile
        self.assertTrue(self.call(ClientType.DETACH)["ok"])
        self.connect()
        attached = self.call(ClientType.ATTACH, size={"rows": 30, "cols": 100})
        host = attached["snapshot"]["panes"]["host_shell"]
        self.assertEqual((host["alive"], host["generation"]), (False, shell.generation))
        self.assertIn(shell.pid, host["kill"]["signalled"])
        self.assertIs(self.backend.shell, shell)

        restarted = self.call(ClientType.RESTART_PANE, pane="host_shell")
        self.assertTrue(restarted["ok"], restarted)
        new = self.backend.shell
        self.assertEqual((restarted["generation"], restarted["input_owner"], restarted["session_id"]),
                         (shell.generation + 1, "user", new.session_id))
        self.host_state(lambda host: host.get("alive") is True and host.get("generation") == new.generation)
        self.next(lambda f: f.header.get("type") == "display" and f.header.get("pane") == "host_shell"
                  and f.header.get("session_id") == new.session_id)
        self.wait(lambda: self.mode() == "manual_prompt", "fresh prompt")
        self.assertTrue(self.call(ClientType.INPUT, f"echo $$ > {root}/newpid\r".encode(), pane="host_shell")["ok"])
        self.wait(lambda: (root / "newpid").exists() and (root / "newpid").read_text().strip() == str(new.pid),
                  "input reaches the new shell")

    def test_pipelined_kill_restart_and_kill_kill_are_handled_in_order(self):
        self.assertTrue(self.call(ClientType.ATTACH, size={"rows": 30, "cols": 100})["ok"])
        first = self.backend.shell
        self.counter += 1
        k1, r1 = f"s{self.counter}a", f"s{self.counter}b"
        self.sock.sendall(ui_v1.encode_frame(ui_v1.request(ClientType.KILL_PANE, k1, pane="host_shell"))
                          + ui_v1.encode_frame(ui_v1.request(ClientType.RESTART_PANE, r1, pane="host_shell")))
        kill, restart = self.result(k1), self.result(r1)
        self.assertTrue(kill["ok"], kill)
        self.assertTrue(restart["ok"], restart)
        second = self.backend.shell
        self.assertEqual(second.generation, first.generation + 1)
        self.assertFalse(alive(first.pid))
        self.wait(lambda: self.mode() == "manual_prompt", "fresh prompt")
        k2, k3 = "dup-a", "dup-b"
        self.sock.sendall(ui_v1.encode_frame(ui_v1.request(ClientType.KILL_PANE, k2, pane="host_shell"))
                          + ui_v1.encode_frame(ui_v1.request(ClientType.KILL_PANE, k3, pane="host_shell")))
        a, b = self.result(k2), self.result(k3)
        self.assertTrue(a["ok"], a)
        self.assertEqual((b["ok"], b["reason"]), (False, "pane_exited"), b)
        self.assertEqual(len(self.backend.kills), 2)
        self.assertIs(self.backend.shell, second)

    def test_shutdown_refuses_kill_and_restart_and_closes_a_restarted_shell_and_its_jobs(self):
        root = self.root
        self.assertTrue(self.call(ClientType.ATTACH, size={"rows": 30, "cols": 100})["ok"])
        self.assertTrue(self.call(ClientType.KILL_PANE, pane="host_shell")["ok"])
        self.assertTrue(self.call(ClientType.RESTART_PANE, pane="host_shell")["ok"])
        new = self.backend.shell
        self.wait(lambda: self.mode() == "manual_prompt", "fresh prompt")
        self.assertTrue(self.call(ClientType.INPUT, f"sleep 7801 & echo $! > {root}/bg\r".encode(),
                                  pane="host_shell")["ok"])
        bg = self.pid_from("bg", "7801")
        token = self.call(ClientType.SHUTDOWN_REQUEST)["token"]
        self.stop.set()  # the test drives the loop from here on
        self.ticker.join(5)
        self.ticker = None

        def ticked_result(rid):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                self.backend._tick(0.02)
                self.pump(0)
                for frame in self.frames:
                    if frame.header.get("type") == "result" and frame.header.get("id") == rid:
                        return frame.header
            self.fail(f"no result for {rid}")

        self.assertTrue(ticked_result(self.request(ClientType.SHUTDOWN_CONFIRM, token=token))["ok"])
        self.assertTrue(self.backend._shutdown_confirmed)
        for kind in (ClientType.KILL_PANE, ClientType.RESTART_PANE):
            header = ticked_result(self.request(kind, pane="host_shell"))
            self.assertEqual((header["ok"], header["reason"]), (False, "backend_shutdown"), header)
        self.assertTrue(alive(new.pid) and alive(bg))
        result = self.backend._close()
        self.backend._close = lambda: result  # closed here; the fixture teardown must not close twice
        self.assertTrue(result["verified"], result)
        self.assertFalse(alive(new.pid) or alive(bg))
        self.assertEqual(session_members(new.pid), [])


class ConcurrencyTests(_ShellCase):
    def test_many_concurrent_kills_then_restarts_yield_exactly_one_each(self):
        def race(call, n=6):
            barrier = threading.Barrier(n)
            out, lock = [], threading.Lock()

            def run():
                barrier.wait(5)
                try:
                    value = ("ok", call())
                except Held as held:
                    value = ("held", held.reason)
                with lock:
                    out.append(value)
            threads = [threading.Thread(target=run) for _ in range(n)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertFalse(any(t.is_alive() for t in threads))
            return out

        first = self.backend.shell
        kills = race(lambda: self.backend.kill_pane(PaneId.HOST_SHELL))
        self.assertEqual(sum(1 for kind, _ in kills if kind == "ok"), 1, kills)
        self.assertTrue({v for k, v in kills if k == "held"} <= {Reason.KILL_IN_PROGRESS, Reason.PANE_EXITED}, kills)
        self.assertEqual(len(self.backend.kills), 1)
        restarts = race(lambda: self.backend.restart_pane(PaneId.HOST_SHELL))
        self.assertEqual(sum(1 for kind, _ in restarts if kind == "ok"), 1, restarts)
        self.assertTrue({v for k, v in restarts if k == "held"} <= {Reason.RESTART_IN_PROGRESS, Reason.PANE_ALIVE},
                        restarts)
        second = self.backend.shell
        self.assertEqual(second.generation, first.generation + 1)
        self.prompt("fresh prompt")
        self.assertFalse(alive(first.pid))
        self.assertNotIn(first.pid, own_children()["live"])
        self.assertEqual(own_children()["zombies"], [])


if __name__ == "__main__":
    unittest.main()
