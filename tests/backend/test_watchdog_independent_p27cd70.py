"""C-D70 (1)/(2)/(4)/(5) independent checks of the Workbench watchdog (p27-cd70-test-01).

Expectations come from DECISIONS.md C-D70 and the assignment p27-cd70-01 (1)/(2)/(4)/(5) and the root adjudication
p27-cd70-02 Q2, not from the implementation's tests. The real ``Watchdog`` runs on a fake clock against fake ports
(an in-memory world: the open Task, the worker OMP probe, the terminal, the outbox, pause, the reports, the
bridge peers). Each notice "sent" is recorded with its role. No OMP, no provider.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

from workbench.backend.flow_recovery import WatchPorts, Watchdog
from workbench.contracts.v1 import ActorRole

IDLE_PROBE = {"idle": True, "pending": False, "approvalPending": False, "inFlightToolCount": 0}
SECRET = "REPORT-BODY-must-not-appear-7f3a"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class World:
    """Everything the watchdog may read; one instance per test."""

    def __init__(self):
        self.worker_peer = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=4242)
        self.manager_peer = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=4343)
        self.task_id = str(uuid4())
        self.task = {"task_id": self.task_id, "kind": "work", "status": "running", "run_id": str(uuid4()),
                     "worker_session": [self.worker_peer.session_id, 1]}
        self.probe = dict(IDLE_PROBE)
        self.probes = 0
        self.terminal = {"running": False, "notice_pending": False,
                         "last": {"command_id": "c-1", "command": "lscpu", "state": "exited", "exit_code": 0,
                                  "log_path": "/tmp/x/c-1.log"}}
        self.outbox_busy = False
        self.paused = False
        self.reports: list[dict] = []
        self.requeued = 0
        self.outcome = "delivered"
        self.sent: list[tuple[str, dict]] = []
        self.journal: list[dict] = []
        self.on_notify = None
        self.turns = (0, False)

    def ports(self) -> WatchPorts:
        def notify(role, notice):
            self.sent.append((role.value, dict(notice)))
            if self.on_notify is not None:
                self.on_notify(role, notice)
            return self.outcome if not callable(self.outcome) else self.outcome(role, notice)

        def peer(role):
            return self.worker_peer if role is ActorRole.WORKER else self.manager_peer

        def probe(role):
            self.probes += 1
            return None if self.probe is None else dict(self.probe)

        def requeue():
            return self.requeued

        return WatchPorts(task=lambda: None if self.task is None else dict(self.task),
                          task_summary=lambda: None if self.task is None else {**self.task, "summary": "s"},
                          peer=peer, probe=probe, turns=lambda cursor: self.turns,
                          terminal=lambda: dict(self.terminal), outbox_busy=lambda role: self.outbox_busy,
                          reports=lambda: [dict(r) for r in self.reports], requeue=requeue,
                          paused=lambda: self.paused, notify=notify,
                          restart_cause=lambda p: {"cause": "restart_worker", "requester": "manager",
                                                   "reason": "stuck"},
                          journal=self.journal.append, worker_state=lambda: {"state": "busy"})

    def of(self, role, notice_type=None):
        return [n for r, n in self.sent if r == role and (notice_type is None or n.get("type") == notice_type)]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.world = World()
        self.dog = Watchdog(self.world.ports(), clock=self.clock, wall=lambda: 1_800_000_000.0)

    def run_for(self, seconds, step=1.0):
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.dog.tick()
            self.clock.now += step
        self.dog.tick()

    def checks(self):
        return self.world.of("worker", "status_check")

    def stalled(self):
        return self.world.of("manager", "worker_stalled")


class StatusCheckTimingTests(Fixture):
    def test_first_check_only_after_60_s_of_continuous_idleness(self):
        self.run_for(55)
        self.assertEqual(self.checks(), [], "no check before 60 s idle")
        self.run_for(10)
        self.assertEqual(len(self.checks()), 1)
        check = self.checks()[0]
        self.assertEqual(check["task_id"], self.world.task_id)
        self.assertGreaterEqual(check["idle_seconds"], 60)
        dumped = json.dumps(check)
        self.assertIn("c-1", dumped, "the last terminal command id is in the check")
        text = check.get("instruction", "")
        for word in ("done", "blocked", "to_manager", "terminal"):
            self.assertIn(word, text, f"the check tells the worker what to do ({word})")

    def test_two_checks_then_exactly_one_stalled_notice_however_long_it_stays_idle(self):
        self.run_for(60 * 20)
        self.assertEqual(len(self.checks()), 2, "at most two checks in a row per Task")
        self.assertEqual(len(self.stalled()), 1, "one worker_stalled, never repeated")
        stalled = self.stalled()[0]
        self.assertEqual(stalled["task_id"], self.world.task_id)
        self.assertEqual(stalled.get("checks_sent"), 2)
        self.assertGreaterEqual(stalled.get("idle_seconds", 0), 60)
        self.assertIn("restart_worker", stalled.get("instruction", ""), "the hint names the manager's choices")
        self.assertEqual(self.world.of("worker", "worker_stalled"), [], "the stalled notice goes to the manager only")
        self.assertEqual(self.world.of("manager", "status_check"), [])

    def test_the_stalled_notice_needs_another_60_s_after_the_second_check(self):
        self.run_for(61)
        self.run_for(61)
        self.assertEqual(len(self.checks()), 2)
        second_at = self.clock.now
        self.run_for(50)
        self.assertEqual(self.stalled(), [], f"not before 60 s after the 2nd check (at {second_at})")
        self.run_for(15)
        self.assertEqual(len(self.stalled()), 1)

    def test_checks_are_spaced_by_the_idle_limit_not_sent_on_every_tick(self):
        self.run_for(61)
        self.assertEqual(len(self.checks()), 1)
        self.run_for(45)
        self.assertEqual(len(self.checks()), 1, "the 2nd check needs another full 60 s")

    def test_the_worker_probe_is_not_polled_more_often_than_every_5_s(self):
        self.run_for(120, step=0.5)
        self.assertLessEqual(self.world.probes, 120 / 5 + 2, f"{self.world.probes} probes in 120 s")
        self.assertGreater(self.world.probes, 0)


class BlockingConditionTests(Fixture):
    """Each condition of C-D70 (1) alone keeps the watchdog quiet for a long time."""

    CASES = {
        "worker busy (responding)": lambda w: w.probe.update(idle=False),
        "worker has pending messages": lambda w: w.probe.update(pending=True),
        "approval pending": lambda w: w.probe.update(approvalPending=True),
        "a tool is running": lambda w: w.probe.update(inFlightToolCount=1),
        "worker state unknown (probe failed)": lambda w: setattr(w, "probe", None),
        "worker terminal command running": lambda w: w.terminal.update(running=True),
        "terminal completion notice still to be sent": lambda w: w.terminal.update(notice_pending=True),
        "outbox message to/from the worker pending": lambda w: setattr(w, "outbox_busy", True),
        "automation paused": lambda w: setattr(w, "paused", True),
        "no open Task": lambda w: setattr(w, "task", None),
        "the Task is held (not running)": lambda w: w.task.update(status="held"),
        "an experiment Task (not work)": lambda w: w.task.update(kind="experiment"),
        "the TASK never reached the current worker session": lambda w: w.task.update(
            worker_session=[str(uuid4()), 1]),
        "worker not connected": lambda w: setattr(w, "worker_peer", None),
    }

    def test_each_condition_alone_blocks_every_notice(self):
        for name, apply in self.CASES.items():
            with self.subTest(name):
                self.setUp()
                apply(self.world)
                self.run_for(600)
                self.assertEqual(self.world.sent, [], f"{name}: nothing is sent")

    def test_a_port_that_raises_counts_as_not_idle(self):
        def boom(*_args):
            raise RuntimeError("port down")
        for port in ("paused", "outbox_busy", "terminal", "probe"):
            with self.subTest(port):
                self.setUp()
                ports = self.world.ports()
                setattr(ports, port, boom)
                dog = Watchdog(ports, clock=self.clock)
                end = self.clock.now + 400
                while self.clock.now < end:
                    dog.tick()
                    self.clock.now += 1
                self.assertEqual(self.world.of("worker", "status_check"), [], f"{port} failing: no check")

    def test_a_short_busy_blip_restarts_the_60_s(self):
        self.run_for(50)
        self.world.probe["idle"] = False
        self.run_for(6)
        self.world.probe["idle"] = True
        self.run_for(50)
        self.assertEqual(self.checks(), [], "the 60 s are continuous; a busy period starts them over")
        self.run_for(20)
        self.assertEqual(len(self.checks()), 1)

    def test_a_terminal_command_or_pending_message_in_between_restarts_the_60_s(self):
        for name, on, off in (("terminal", lambda w: w.terminal.update(running=True),
                               lambda w: w.terminal.update(running=False)),
                              ("outbox", lambda w: setattr(w, "outbox_busy", True),
                               lambda w: setattr(w, "outbox_busy", False)),
                              ("pause", lambda w: setattr(w, "paused", True), lambda w: setattr(w, "paused", False))):
            with self.subTest(name):
                self.setUp()
                self.run_for(55)
                on(self.world)
                self.run_for(2)
                off(self.world)
                self.run_for(30)
                self.assertEqual(self.checks(), [], f"{name}: the idle time starts over")

    def test_a_worker_turn_event_between_probes_restarts_the_idle_time(self):
        self.run_for(58)
        self.world.turns = (1, True)  # the worker took a turn that the 5 s probe did not see
        self.dog.tick()
        self.world.turns = (1, False)
        self.clock.now += 1
        self.run_for(30)
        self.assertEqual(self.checks(), [])


class ResetTests(Fixture):
    def test_a_worker_terminal_call_or_report_starts_the_count_over_and_re_arms_the_stalled_notice(self):
        for tool in ("terminal", "to_manager", "manager_follow_up"):
            with self.subTest(tool):
                self.setUp()
                self.run_for(60 * 5)
                self.assertEqual((len(self.checks()), len(self.stalled())), (2, 1))
                self.dog.worker_acted(tool)
                self.run_for(60 * 5)
                self.assertEqual(len(self.checks()), 4, f"{tool}: two new checks after the reset")
                self.assertEqual(len(self.stalled()), 2, f"{tool}: one more stalled notice after the worker acted")

    def test_a_worker_turn_alone_does_not_reset_the_count(self):
        """Only a report or a terminal call (or an accepted manager follow-up) count as acting (C-D70 (1))."""
        self.run_for(61)
        self.assertEqual(len(self.checks()), 1)
        self.world.turns = (5, True)  # the worker answered the check with a text turn only
        self.dog.tick()
        self.world.turns = (5, False)
        self.run_for(60 * 5)
        self.assertEqual(len(self.checks()), 2, "a text turn does not give two more checks")
        self.assertEqual(len(self.stalled()), 1)

    def test_the_worker_acting_while_a_check_is_being_sent_does_not_count_that_check(self):
        """Race: worker_acted() lands while the notify call is in flight (another thread)."""
        acted = threading.Event()

        def act(role, notice):
            if notice.get("type") == "status_check" and not acted.is_set():
                acted.set()
                thread = threading.Thread(target=self.dog.worker_acted, args=("terminal",))
                thread.start()
                thread.join(2)
        self.world.on_notify = act
        self.run_for(61)
        self.assertTrue(acted.is_set())
        self.run_for(60 * 2 + 15)
        self.assertEqual(len(self.checks()), 3, "the overtaken check is not counted: two more checks follow")
        self.assertEqual(len(self.stalled()), 0)
        self.run_for(75)
        self.assertEqual(len(self.stalled()), 1)

    def test_a_new_task_has_its_own_count(self):
        self.run_for(60 * 5)
        self.assertEqual(len(self.stalled()), 1)
        self.world.task = {**self.world.task, "task_id": str(uuid4())}
        self.run_for(60 * 5)
        self.assertEqual(len(self.checks()), 4)
        self.assertEqual(len(self.stalled()), 2)
        self.assertEqual(self.stalled()[1]["task_id"], self.world.task["task_id"])

    def test_concurrent_worker_acted_calls_and_ticks_never_raise_or_duplicate(self):
        stop = threading.Event()
        errors: list[BaseException] = []

        def hammer():
            while not stop.is_set():
                try:
                    self.dog.worker_acted("terminal")
                except BaseException as exc:  # pragma: no cover - reported below
                    errors.append(exc)
        thread = threading.Thread(target=hammer)
        thread.start()
        try:
            self.run_for(400)
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(errors, [])
        ids = [n["notice_id"] for _, n in self.world.sent]
        self.assertEqual(len(ids), len(set(ids)), "no notice_id is sent twice after a successful delivery")


class DeliveryTests(Fixture):
    def test_a_deferred_check_is_retried_with_the_same_notice_id_and_counted_once(self):
        outcomes = iter(["deferred", "paused", "not_connected", "delivered"])
        self.world.outcome = lambda role, notice: next(outcomes, "delivered")
        self.run_for(75)
        ids = [n["notice_id"] for n in self.checks()]
        self.assertGreaterEqual(len(ids), 4, "a deferred check is tried again")
        self.assertEqual(len(set(ids[:4])), 1, "the retries carry the same notice_id (the bridge drops a 2nd copy)")
        self.assertEqual(self.dog.view()["watch"]["checks_sent"], 1, "one check counted")

    def test_an_unknown_or_rejected_check_is_never_resent(self):
        for outcome in ("unknown", "rejected"):
            with self.subTest(outcome):
                self.setUp()
                self.world.outcome = outcome
                self.run_for(62)
                first = [n["notice_id"] for n in self.checks()]
                self.assertEqual(len(first), 1)
                self.run_for(30)
                self.assertEqual([n["notice_id"] for n in self.checks()], first)

    def test_a_deferred_manager_notice_is_retried_never_duplicated(self):
        state = {"n": 0}

        def outcome(role, notice):
            if role is ActorRole.MANAGER:
                state["n"] += 1
                return "deferred" if state["n"] < 5 else "delivered"
            return "delivered"
        self.world.outcome = outcome
        self.run_for(60 * 6)
        stalled = self.stalled()
        self.assertEqual(len({n["notice_id"] for n in stalled}), 1, "one stalled notice, retried under one id")
        self.assertGreaterEqual(len(stalled), 5)
        self.run_for(60 * 3)
        self.assertEqual(len({n["notice_id"] for n in self.stalled()}), 1)


class ReportDeliveryTests(Fixture):
    def report(self, **fields):
        entry = {"handoff_id": str(uuid4()), "origin": "worker", "task_id": self.world.task_id,
                 "message_id": str(uuid4()), "report_kind": "done", "text": SECRET, "state": "pending",
                 "submitted": False, "editor_since": None}
        entry.update(fields)
        self.world.reports.append(entry)
        return entry

    def test_an_unknown_report_is_told_once_without_its_content(self):
        self.world.task = None  # independent of the watch
        entry = self.report(state="unknown")
        self.run_for(30)
        notices = self.world.of("manager", "report_delivery_unknown")
        self.assertEqual(len(notices), 1)
        self.assertEqual((notices[0]["task_id"], notices[0]["message_id"]), (entry["task_id"], entry["message_id"]))
        self.assertNotIn(SECRET, json.dumps(notices[0]), "never the report content")
        self.run_for(300)
        self.assertEqual(len(self.world.of("manager", "report_delivery_unknown")), 1, "once")
        self.assertEqual(self.world.of("worker"), [])

    def test_two_unknown_reports_get_one_notice_each(self):
        self.world.task = None
        a, b = self.report(state="unknown"), self.report(state="unknown")
        self.run_for(10)
        ids = sorted(n["message_id"] for n in self.world.of("manager", "report_delivery_unknown"))
        self.assertEqual(ids, sorted([a["message_id"], b["message_id"]]))

    def test_a_pending_report_is_not_unknown(self):
        self.world.task = None
        self.report(state="pending")
        self.report(state="delivered", submitted=True)
        self.run_for(60)
        self.assertEqual(self.world.of("manager", "report_delivery_unknown"), [])

    def test_editor_wait_is_shown_after_30_s_and_cleared_on_delivery(self):
        self.world.task = None
        entry = self.report(state="pending", status="deferred", editor_since=self.clock.now)
        self.run_for(25)
        self.assertIsNone(self.dog.view()["report_wait"], "not before 30 s")
        self.run_for(10)
        wait = self.dog.view()["report_wait"]
        self.assertIsNotNone(wait, "after 30 s the editor wait is exposed")
        self.assertEqual(wait["count"], 1)
        self.assertNotIn(SECRET, json.dumps(wait))
        entry.update(state="delivered", submitted=True, editor_since=None)
        self.world.reports = [entry]
        self.assertIsNone(self.dog.view()["report_wait"], "cleared when delivered")


class SessionTests(Fixture):
    def test_the_first_registration_is_not_a_restart(self):
        self.world.task = {**self.world.task, "status": "running"}
        self.run_for(5)
        self.assertEqual(self.world.of("manager", "worker_restarted"), [])
        self.assertEqual(self.world.of("manager", "manager_recovery"), [])

    def test_a_new_worker_session_with_an_open_task_tells_the_manager_once(self):
        self.run_for(3)
        old = self.world.worker_peer.session_id
        self.world.worker_peer = None  # the worker OMP is being restarted
        self.run_for(3)
        self.world.worker_peer = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=999)
        self.run_for(200)
        notices = self.world.of("manager", "worker_restarted")
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual(notice["task_id"], self.world.task_id)
        self.assertEqual(notice.get("cause"), "restart_worker")
        self.assertIn("terminal", notice, "the terminal state is in the notice")
        self.assertNotEqual(notice.get("session_id"), old)
        self.assertEqual(self.checks(), [], "the new session never got the Task: no status check to it")

    def test_a_reconnect_of_the_same_session_is_not_a_restart(self):
        self.run_for(3)
        peer = self.world.worker_peer
        self.world.worker_peer = None
        self.run_for(3)
        self.world.worker_peer = peer
        self.run_for(3)
        self.assertEqual(self.world.of("manager", "worker_restarted"), [])

    def test_a_new_worker_session_without_an_open_task_sends_nothing(self):
        self.world.task = None
        self.run_for(3)
        self.world.worker_peer = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=999)
        self.run_for(10)
        self.assertEqual(self.world.sent, [])

    def test_two_worker_restarts_give_two_notices(self):
        self.run_for(3)
        for _ in range(2):
            self.world.worker_peer = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=999)
            self.run_for(5)
        self.assertEqual(len(self.world.of("manager", "worker_restarted")), 2)

    def test_a_new_manager_session_gets_one_recovery_notice_listing_unknown_reports_without_content(self):
        self.world.task = None
        unknown = {"handoff_id": str(uuid4()), "origin": "worker", "task_id": self.world.task_id,
                   "message_id": str(uuid4()), "report_kind": "done", "text": SECRET, "state": "unknown",
                   "submitted": False}
        self.world.reports.append(unknown)
        self.world.outcome = lambda role, notice: "deferred" if role is ActorRole.MANAGER else "delivered"
        self.run_for(3)  # the old manager never takes the unknown notice (composer busy)
        self.world.requeued = 2
        switched = len(self.world.sent)
        self.world.manager_peer = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=5555)
        self.world.outcome = "delivered"
        self.run_for(20)
        recovery = self.world.of("manager", "manager_recovery")
        self.assertEqual(len(recovery), 1)
        notice = recovery[0]
        self.assertEqual(notice.get("reports_resent"), 2)
        self.assertEqual(notice.get("reports_unknown"), 1)
        self.assertNotIn(SECRET, json.dumps(notice), "unknown reports are listed without content")
        self.assertIn(unknown["message_id"], json.dumps(notice))
        delivered = [n for r, n in self.world.sent[switched:] if r == "manager"]
        self.assertEqual(delivered[0]["type"] if delivered else None, "manager_recovery",
                         "the recovery notice is the first one the new manager session gets")
        self.assertLessEqual(len({n["notice_id"] for n in self.world.of("manager", "report_delivery_unknown")}), 1,
                             "the unknown report is never told twice")


class LifetimeTests(unittest.TestCase):
    def test_the_watchdog_thread_ticks_and_stops_on_close(self):
        world = World()
        dog = Watchdog(world.ports(), idle_limit=0.2, probe_interval=0.0, tick_interval=0.02, notice_retry=0.05)
        before = {t.ident for t in threading.enumerate()}
        dog.start()
        dog.start()  # a second start is harmless
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not world.of("manager", "worker_stalled"):
            time.sleep(0.02)
        self.assertEqual(len(world.of("worker", "status_check")), 2)
        self.assertEqual(len(world.of("manager", "worker_stalled")), 1)
        dog.close(timeout=2)
        new = [t for t in threading.enumerate() if t.ident not in before and t.is_alive()]
        self.assertEqual([t.name for t in new], [], "no watchdog thread left after close")
        count = len(world.sent)
        time.sleep(0.2)
        self.assertEqual(len(world.sent), count, "nothing is sent after close")

    def test_a_tick_that_raises_inside_a_port_never_kills_the_thread(self):
        world = World()
        ports = world.ports()
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] % 2:
                raise RuntimeError("flaky")
            return dict(world.task)
        ports.task = flaky
        dog = Watchdog(ports, idle_limit=0.1, probe_interval=0.0, tick_interval=0.01)
        dog.start()
        try:
            time.sleep(0.5)
            self.assertTrue(dog._thread is not None and dog._thread.is_alive())
        finally:
            dog.close(timeout=2)


if __name__ == "__main__":
    unittest.main()
