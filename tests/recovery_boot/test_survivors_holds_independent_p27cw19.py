"""Independent CW-19 checks (p27-cw19-test-01): survivors, stop_survivor, holds and the restart notice.

Derived from C-D71 (1)-(3) and the root-accepted implementer decisions 1, 2 and 4:

- ``stop_survivor`` signals only a survivor whose identity (same boot, pid, start ticks, owner) is proven again
  right before the signal; unknown ids, display-only entries, changed boots, recycled pids and ended processes
  are refused without any signal; TERM, then KILL after the grace; the request is recorded with its reason;
- the tool is manager-only, validates its arguments, never accepts environment values in the reason and is
  refused while the backend shuts down;
- the admission hold matrix before confirm-boot / during a metadata or model hold: to_worker (new work) held,
  cancel allowed; worker ``terminal`` held; notices held (answer ``paused`` without touching the bridge);
- the ``backend_restarted`` notice: queued once for a non-fresh start with an open Task, never for a fresh
  start or a closed Task, waits while held and is sent exactly once afterwards.

Only processes started by this test are signalled; temp files live under /tmp.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from workbench.app.recovery import (
    BOOT_HOLD, METADATA_HOLD, AdmissionHold, Survivor, SurvivorRegistry, model_hold,
)
from workbench.backend.flow import HandoffService
from workbench.backend.flow_recovery import WatchPorts, Watchdog
from workbench.backend.launcher import LaunchPlan
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.service import Backend
from workbench.contracts.v1 import ActorRole
from workbench.terminal.shell_g2.prototype import ShellChoice

BOOT_A = "aaaaaaaa-0000-4000-8000-0000000c0019"
BOOT_B = "bbbbbbbb-0000-4000-8000-0000000c0019"


def start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    return int(raw[raw.rfind(b") ") + 2:].split()[19])


def state(pid: int) -> bytes | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    return raw[raw.rfind(b") ") + 2:].split()[0]


def alive(pid: int, ticks: int | None = None) -> bool:
    return state(pid) not in (None, b"Z", b"X") and (ticks is None or start_ticks(pid) == ticks)


class Owned:
    def __init__(self):
        self.items: list[tuple[int, int, subprocess.Popen | None]] = []

    def spawn(self, script: str, *, session: bool = True) -> tuple[int, int]:
        child = subprocess.Popen(["/bin/sh", "-c", script], start_new_session=session, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while start_ticks(child.pid) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.items.append((child.pid, start_ticks(child.pid), child))
        return child.pid, start_ticks(child.pid)

    def adopt(self, pid: int) -> tuple[int, int]:
        self.items.append((pid, start_ticks(pid), None))
        return pid, start_ticks(pid)

    def reap(self, pid: int) -> None:
        for item_pid, _ticks, child in self.items:
            if item_pid == pid and child is not None:
                try:
                    child.wait(5)
                except subprocess.TimeoutExpired:
                    pass

    def cleanup(self):
        for pid, ticks, _child in self.items:
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            try:
                if start_ticks(pid) == ticks:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            except OSError:
                pass
            finally:
                os.close(fd)
        for _pid, _ticks, child in self.items:
            if child is not None:
                try:
                    child.wait(5)
                except subprocess.TimeoutExpired:
                    pass


def wait_member(leader: int, timeout: float = 5.0) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name in os.listdir("/proc"):
            if not name.isdigit() or int(name) == leader:
                continue
            try:
                raw = Path(f"/proc/{name}/stat").read_bytes()
            except OSError:
                continue
            fields = raw[raw.rfind(b") ") + 2:].split()
            if int(fields[3]) == leader and fields[0] not in (b"Z", b"X"):
                return int(name)
        time.sleep(0.02)
    return None


def gone(pid: int, ticks: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid, ticks):
            return True
        time.sleep(0.02)
    return not alive(pid, ticks)


class SurvivorStop(unittest.TestCase):
    def setUp(self):
        self.owned = Owned()
        self.addCleanup(self.owned.cleanup)
        self.boot = BOOT_A

    def registry(self, *survivors, grace=0.5, kill_wait=1.0):
        return SurvivorRegistry(list(survivors), lambda: self.boot, grace=grace, kill_wait=kill_wait)

    def survivor(self, pid, ticks, *, sid="s1", stoppable=True, leader=None, boot=BOOT_A, name="host_shell"):
        return Survivor(sid, name, pid, ticks, boot, stoppable, None if stoppable else "unverified",
                        leader=leader)

    def test_unknown_id_is_refused(self):
        result = self.registry().stop("s9", reason="r", requester="manager")
        self.assertEqual((result["status"], result["reason"]), ("refused", "unknown_survivor"))

    def test_display_only_entry_is_never_signalled(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        result = self.registry(self.survivor(pid, ticks, stoppable=False)).stop("s1", reason="r",
                                                                                 requester="manager")
        self.assertEqual((result["status"], result["reason"]), ("refused", "identity_unverified"))
        time.sleep(0.2)
        self.assertTrue(alive(pid, ticks))

    def test_changed_or_unknown_boot_is_refused(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        registry = self.registry(self.survivor(pid, ticks))
        for current in (BOOT_B, None, ""):
            self.boot = current
            result = registry.stop("s1", reason="r", requester="manager")
            self.assertEqual((result["status"], result["reason"]), ("refused", "boot_changed"), current)
        time.sleep(0.2)
        self.assertTrue(alive(pid, ticks))

    def test_boot_source_that_raises_is_refused(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        registry = SurvivorRegistry([self.survivor(pid, ticks)], lambda: (_ for _ in ()).throw(OSError()))
        self.assertEqual(registry.stop("s1", reason="r", requester="manager")["reason"], "boot_changed")
        self.assertTrue(alive(pid, ticks))

    def test_recycled_pid_is_never_signalled(self):
        # The survivor's pid now belongs to another process (same pid, other start ticks).
        pid, ticks = self.owned.spawn("exec sleep 60")
        registry = self.registry(self.survivor(pid, ticks + 3))
        result = registry.stop("s1", reason="r", requester="manager")
        self.assertIn(result["status"], ("refused", "already_ended"))
        self.assertEqual(result.get("signalled", []), [])
        time.sleep(0.2)
        self.assertTrue(alive(pid, ticks), "a process with another identity was signalled")

    def test_member_whose_leader_identity_changed_is_refused(self):
        leader, leader_ticks = self.owned.spawn("sleep 60 & wait")
        member = wait_member(leader)
        self.assertIsNotNone(member)
        member_ticks = self.owned.adopt(member)[1]
        registry = self.registry(self.survivor(member, member_ticks, name="session_member",
                                               leader=(leader, leader_ticks + 5)))
        result = registry.stop("s1", reason="r", requester="manager")
        self.assertEqual((result["status"], result["reason"]), ("refused", "identity_unverified"))
        time.sleep(0.2)
        self.assertTrue(alive(member, member_ticks))

    def test_stop_term_then_recorded_and_not_repeated(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        registry = self.registry(self.survivor(pid, ticks))
        result = registry.stop("s1", reason="old job no longer needed", requester="manager")
        self.owned.reap(pid)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["signalled"], [pid])
        self.assertTrue(gone(pid, ticks))
        view = registry.views()[0]
        self.assertEqual(view["stop"]["reason"], "old job no longer needed")
        self.assertEqual(view["stop"]["requester"], "manager")
        self.assertEqual(view["stop"]["outcome"], "stopped")
        self.assertFalse(view["stoppable"])
        again = registry.stop("s1", reason="again", requester="manager")
        self.assertEqual((again["status"], again["reason"]), ("refused", "not_alive"))
        self.assertEqual(registry.alive(), [])

    def test_term_ignoring_survivor_is_killed_after_the_grace(self):
        pid, ticks = self.owned.spawn("trap '' TERM HUP; while :; do sleep 0.1; done")
        registry = self.registry(self.survivor(pid, ticks), grace=0.3, kill_wait=2.0)
        started = time.monotonic()
        result = registry.stop("s1", reason="r", requester="manager")
        elapsed = time.monotonic() - started
        self.owned.reap(pid)
        self.assertEqual(result["status"], "stopped", result)
        self.assertGreaterEqual(elapsed, 0.25, "KILL must come only after the TERM grace")
        self.assertTrue(gone(pid, ticks))

    def test_session_leader_is_stopped_with_its_members(self):
        leader, leader_ticks = self.owned.spawn("trap '' HUP; sleep 60 & wait")
        member = wait_member(leader)
        self.assertIsNotNone(member)
        member_ticks = self.owned.adopt(member)[1]
        registry = self.registry(self.survivor(leader, leader_ticks))
        result = registry.stop("s1", reason="r", requester="manager")
        self.owned.reap(leader)
        self.assertEqual(result["status"], "stopped", result)
        self.assertIn(member, result["members"])
        self.assertTrue(gone(member, member_ticks))

    def test_concurrent_stops_of_one_survivor_signal_once(self):
        import threading
        pid, ticks = self.owned.spawn("exec sleep 60")
        registry = self.registry(self.survivor(pid, ticks), grace=0.5)
        results: list = []
        barrier = threading.Barrier(3)

        def call():
            barrier.wait()
            results.append(registry.stop("s1", reason="r", requester="manager"))
        threads = [threading.Thread(target=call) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.owned.reap(pid)
        statuses = sorted(item["status"] for item in results)
        self.assertEqual(statuses, ["refused", "refused", "stopped"], results)
        self.assertEqual(sum(len(item.get("signalled") or []) for item in results), 1)

    def test_stop_unconfirmed_survivor_that_ends_later_is_not_left_running(self):
        """A survivor whose KILL was not yet observed must not stay 'left running' once it has ended."""
        pid, ticks = self.owned.spawn("trap '' TERM HUP; while :; do sleep 0.1; done")
        registry = self.registry(self.survivor(pid, ticks), grace=0.0, kill_wait=0.0)
        result = registry.stop("s1", reason="r", requester="manager")
        self.owned.reap(pid)
        if result["status"] != "stop_unconfirmed":
            self.skipTest(f"the KILL was observed at once ({result['status']}); the race did not occur")
        self.assertTrue(gone(pid, ticks))
        self.assertEqual(registry.alive(), [],
                         "an ended survivor is still listed as alive (shutdown would be reported unverified)")


class BackendStub(unittest.TestCase):
    """A real Backend object (not opened): only the CW-19 methods under test are used."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw19-indep-be-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        layout = DataLayout(ensure_private_dir(self.root / "data"))
        ensure_private_dir(layout.workflow)
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/nonexistent/omp", "omp/0", "/x/bridge.ts", ())
        self.boot = BOOT_A
        self.backend = Backend(layout, plan, project_dir=str(self.root),
                               environment={"PATH": "/usr/bin:/bin", "SECRET_TOKEN": "s3cr3t-value-0123456789"},
                               boot_source=lambda: self.boot)
        self.journal: list[dict] = []
        self.backend._journal = self.journal.append
        self.backend._write_record = lambda: True
        self.owned = Owned()
        self.addCleanup(self.owned.cleanup)

    @staticmethod
    def request(tool, args):
        return {"tool_call_id": f"call-{uuid4().hex[:8]}", "request_id": uuid4().hex, "session_id": str(uuid4()),
                "generation": 1, "tool": tool, "args": args}


class StopSurvivorTool(BackendStub):
    def test_worker_cannot_call_it(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.backend.survivors = SurvivorRegistry([Survivor("s1", "host_shell", pid, ticks, BOOT_A, True)],
                                                  lambda: self.boot)
        peer = SimpleNamespace(role=ActorRole.WORKER)
        result = self.backend._recovery_tool(peer, self.request("stop_survivor", {"survivor_id": "s1",
                                                                                   "reason": "r"}))
        self.assertEqual((result["status"], result["reason"]), ("rejected", "tool_not_allowed_for_role"))
        self.assertTrue(alive(pid, ticks))

    def test_argument_validation(self):
        peer = SimpleNamespace(role=ActorRole.MANAGER)
        for args in (None, {}, {"survivor_id": "s1"}, {"reason": "x"}, {"survivor_id": "", "reason": "x"},
                     {"survivor_id": "s1", "reason": "   "}, {"survivor_id": "s" * 17, "reason": "x"},
                     {"survivor_id": "s1", "reason": "x", "pid": 1}, {"survivor_id": 1, "reason": "x"}):
            with self.subTest(args=args):
                result = self.backend._recovery_tool(peer, self.request("stop_survivor", args))
                self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"))

    def test_environment_value_in_reason_is_rejected_and_not_journaled(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.backend.survivors = SurvivorRegistry([Survivor("s1", "host_shell", pid, ticks, BOOT_A, True)],
                                                  lambda: self.boot)
        peer = SimpleNamespace(role=ActorRole.MANAGER)
        result = self.backend._recovery_tool(peer, self.request(
            "stop_survivor", {"survivor_id": "s1", "reason": "uses s3cr3t-value-0123456789"}))
        self.assertEqual((result["status"], result["reason"]), ("rejected", "environment_value"))
        self.assertNotIn("s3cr3t-value-0123456789", json.dumps(self.journal))
        self.assertTrue(alive(pid, ticks))

    def test_refused_while_shutting_down(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.backend.survivors = SurvivorRegistry([Survivor("s1", "host_shell", pid, ticks, BOOT_A, True)],
                                                  lambda: self.boot)
        self.backend._shutdown_confirmed = True
        result = self.backend._recovery_tool(SimpleNamespace(role=ActorRole.MANAGER),
                                             self.request("stop_survivor", {"survivor_id": "s1", "reason": "r"}))
        self.assertEqual((result["status"], result["reason"]), ("refused", "backend_shutdown"))
        self.assertTrue(alive(pid, ticks))

    def test_stop_is_recorded_once_per_call_key(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.backend.survivors = SurvivorRegistry([Survivor("s1", "host_shell", pid, ticks, BOOT_A, True)],
                                                  lambda: self.boot, grace=0.5)
        peer = SimpleNamespace(role=ActorRole.MANAGER)
        request = self.request("stop_survivor", {"survivor_id": "s1", "reason": "leftover experiment"})
        first = self.backend._recovery_tool(peer, request)
        self.owned.reap(pid)
        second = self.backend._recovery_tool(peer, dict(request))  # the same call again: the stored answer
        self.assertEqual(first["status"], "stopped")
        self.assertEqual(second, first)
        self.assertEqual(len(self.backend.survivor_stops), 1)
        entry = self.backend.survivor_stops[0]
        self.assertEqual((entry["reason"], entry["requester"], entry["outcome"]),
                         ("leftover experiment", "manager", "stopped"))
        kinds = [item.get("type") for item in self.journal]
        self.assertIn("stop_survivor_request", kinds)
        self.assertIn("stop_survivor_result", kinds)

    def test_status_lists_survivors_for_the_manager(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.backend.survivors = SurvivorRegistry(
            [Survivor("s1", "host_shell", pid, ticks, BOOT_A, True),
             Survivor("s2", "session_member", pid, ticks, BOOT_A, False, "its session leader is gone")],
            lambda: self.boot)
        self.backend.startup = {"classification": "same_boot_crash", "outbox_lost_count": 2, "run": None}
        backend = self.backend._workbench_status(SimpleNamespace(args={}), {})["backend"]
        self.assertEqual(backend["startup"], "same_boot_crash")
        self.assertEqual(backend["outbox_not_sent"], 2)
        by_id = {item["survivor_id"]: item for item in backend["survivors"]}
        self.assertTrue(by_id["s1"]["stoppable"])
        self.assertFalse(by_id["s2"]["stoppable"])
        self.assertEqual(by_id["s2"]["identity"], "unverified")


class HoldMatrix(BackendStub):
    """C-D71 (3): before confirm-boot every Workbench-originated automatic action waits; user actions do not."""

    def handoffs(self, hold):
        service = HandoffService(self.root / "handoffs.jsonl", mailbox=object(), hold=hold)
        return service

    def test_to_worker_held_cancel_allowed(self):
        for reason in (BOOT_HOLD, METADATA_HOLD, model_hold("worker")):
            with self.subTest(reason=reason):
                hold = AdmissionHold()
                hold.set(reason)
                service = self.handoffs(hold.reason_for)
                result = service.handle(ActorRole.MANAGER, self.request("to_worker", {"kind": "work",
                                                                                      "message": "do x"}))
                self.assertEqual(result, {"status": "held", "reason": reason})
                cancel = service.handle(ActorRole.MANAGER, self.request(
                    "to_worker", {"kind": "work", "message": "stop", "task_id": str(uuid4()), "cancel": True}))
                self.assertNotEqual(cancel.get("reason"), reason, "a cancel must not be held")

    def test_manager_model_hold_does_not_hold_to_worker(self):
        hold = AdmissionHold()
        hold.set(model_hold("manager"))
        service = self.handoffs(hold.reason_for)
        result = service.handle(ActorRole.MANAGER, self.request("to_worker", {"kind": "work", "message": "do x"}))
        self.assertNotEqual(result.get("reason"), model_hold("manager"))

    def test_hold_callable_that_raises_holds(self):
        def broken(_role):
            raise RuntimeError("x")
        service = self.handoffs(broken)
        result = service.handle(ActorRole.MANAGER, self.request("to_worker", {"kind": "work", "message": "do x"}))
        self.assertEqual(result["status"], "held")

    def test_notice_is_held_without_touching_the_bridge(self):
        class Untouchable:
            def __getattr__(self, name):
                raise AssertionError(f"the bridge was used while held ({name})")
        self.backend.bridge = Untouchable()
        for reason, role in ((BOOT_HOLD, ActorRole.MANAGER), (BOOT_HOLD, ActorRole.WORKER),
                             (METADATA_HOLD, ActorRole.MANAGER), (model_hold("manager"), ActorRole.MANAGER),
                             (model_hold("worker"), ActorRole.WORKER)):
            with self.subTest(reason=reason, role=role):
                self.backend.hold = AdmissionHold()
                self.backend.hold.set(reason)
                self.assertEqual(self.backend._notice(role, {"type": "status_check"}), "paused")

    def test_paused_notice_is_held(self):
        class Untouchable:
            def __getattr__(self, name):
                raise AssertionError(name)
        self.backend.bridge = Untouchable()
        self.backend.automation_loop = SimpleNamespace(paused=lambda: True)
        self.assertEqual(self.backend._notice(ActorRole.MANAGER, {"type": "backend_restarted"}), "paused")

    def test_terminal_tool_held(self):
        from workbench.backend.flow_terminal import TerminalService
        hold = AdmissionHold()
        hold.set(BOOT_HOLD)
        typed: list = []
        port = SimpleNamespace(type_text=typed.append)
        service = TerminalService(handoffs=SimpleNamespace(record=lambda *_a, **_k: None),
                                  host_shell=lambda: port, gate=SimpleNamespace(acquire=lambda *a: None),
                                  log_root=self.root / "term", automation=lambda: {}, paused=lambda: False,
                                  hold=lambda: hold.reason_for(ActorRole.WORKER))
        result = service._start_unlocked("echo hi", {"k": 1}, None)
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("status"), "held", result)
        self.assertEqual(result.get("reason"), BOOT_HOLD)
        self.assertEqual(typed, [])

    def test_confirm_boot_lifts_only_the_boot_hold_and_is_refused_when_not_pending(self):
        from workbench.backend.boot import BootStore
        from workbench.backend.ui_server import Held
        BootStore(self.backend.layout.root / "boot.json", boot_source=lambda: BOOT_A).begin(None)
        self.boot = BOOT_B
        self.backend._reconcile_startup()
        self.assertEqual(self.backend._hold_reason(ActorRole.WORKER), BOOT_HOLD)
        self.backend.hold.set(METADATA_HOLD)
        with self.assertRaises(Held):
            self.backend.confirm_boot(BOOT_A)  # the recorded (old) boot is not the current one
        self.assertTrue(self.backend.hold.has(BOOT_HOLD))
        answer = self.backend.confirm_boot(BOOT_B)
        self.assertFalse(answer["boot"]["confirmation_required"])
        self.assertFalse(self.backend.hold.has(BOOT_HOLD))
        self.assertTrue(self.backend.hold.has(METADATA_HOLD), "confirm-boot must not lift a metadata hold")
        with self.assertRaises(Held):
            self.backend.confirm_boot(BOOT_B)  # nothing pending any more


class RestartNotice(BackendStub):
    """C-D71 (2) + root decision 1: one backend_restarted notice; never while paused or held."""

    def wire(self, classification, task):
        queued: list = []
        self.backend.startup = {"classification": classification, "run": None, "outbox_lost_count": 0}
        self.backend.flow = SimpleNamespace(task_view=lambda: task)
        self.backend.watchdog = SimpleNamespace(
            enqueue_notice=lambda role, kind, fields: queued.append((role, kind, fields)) or "n-1")
        self.backend._queue_restart_notice()
        return queued

    def test_fresh_start_or_no_open_task_sends_nothing(self):
        task = {"task_id": str(uuid4()), "status": "running"}
        self.assertEqual(self.wire("fresh", task), [])
        self.assertEqual(self.wire("same_boot_crash", None), [])
        self.assertEqual(self.wire("same_boot_crash", {**task, "status": "closed"}), [])

    def test_open_task_queues_exactly_one_notice_with_survivors(self):
        owned_pid, ticks = self.owned.spawn("exec sleep 60")
        self.backend.survivors = SurvivorRegistry([Survivor("s1", "host_shell", owned_pid, ticks, BOOT_A, True)],
                                                  lambda: self.boot)
        task = {"task_id": str(uuid4()), "status": "held", "held_reason": "backend_restarted"}
        for classification in ("same_boot_crash", "same_boot_clean_stop", "reboot"):
            with self.subTest(classification):
                queued = self.wire(classification, task)
                self.assertEqual(len(queued), 1)
                role, kind, fields = queued[0]
                self.assertEqual((role, kind), (ActorRole.MANAGER, "backend_restarted"))
                self.assertEqual(fields["task_id"], task["task_id"])
                self.assertEqual([item["survivor_id"] for item in fields["survivors"]], ["s1"])

    def test_queued_notice_waits_while_held_and_is_sent_once(self):
        hold = AdmissionHold()
        hold.set(BOOT_HOLD)
        sent: list = []

        def notify(role, notice):
            if hold.reason_for(role) is not None:
                return "paused"
            sent.append(notice)
            return "delivered"
        now = [100.0]
        watchdog = Watchdog(WatchPorts(task=lambda: None, notify=notify), clock=lambda: now[0],
                            notice_retry=1.0)
        watchdog.enqueue_notice(ActorRole.MANAGER, "backend_restarted", {"task_id": "t"})
        for _ in range(5):
            watchdog.tick()
            now[0] += 2
        self.assertEqual(sent, [])
        hold.clear(BOOT_HOLD)
        hold.set(model_hold("manager"))  # a model hold of the manager also keeps it waiting
        watchdog.tick()
        now[0] += 2
        self.assertEqual(sent, [])
        hold.clear(model_hold("manager"))
        for _ in range(5):
            watchdog.tick()
            now[0] += 2
        self.assertEqual([item["type"] for item in sent], ["backend_restarted"])


if __name__ == "__main__":
    unittest.main()
