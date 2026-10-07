"""C-D70 (1)/(2)/(4)/(5): the Workbench watchdog with fake ports and a fake clock (no OMP, no provider).

Status checks to an idle worker (timing, limit, reset, pause, terminal running, outbox pending, retry without a
duplicate), the worker_stalled notice, worker/manager new-session notices, the unknown-report notice and the
editor-wait view.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from uuid import uuid4

from workbench.backend.flow_recovery import (
    IDLE_LIMIT, MAX_CHECKS, PROBE_INTERVAL, WatchPorts, Watchdog, validate_restart_arguments,
    validate_status_arguments,
)
from workbench.contracts.v1 import ActorRole

IDLE_STATE = {"idle": True, "pending": False, "approvalPending": False, "inFlightToolCount": 0}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class World:
    """The fake backend the watchdog reads: one open work Task in the worker's current session."""

    def __init__(self):
        self.task_id = str(uuid4())
        self.worker = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=111)
        self.manager = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=222)
        self.task: dict | None = {"task_id": self.task_id, "kind": "work", "status": "running", "run_id": "r1",
                                  "worker_session": [self.worker.session_id, 1]}
        self.state = dict(IDLE_STATE)
        self.terminal = {"running": False, "notice_pending": False,
                         "last": {"command_id": "c1", "status": "exited", "exit_code": 0}}
        self.busy = False
        self.paused = False
        self.events = 0  # worker turn events seen by the bridge
        self.sent: list[tuple[str, dict]] = []
        self.outcome = {"worker": "delivered", "manager": "delivered"}
        self.reports: list[dict] = []
        self.requeued = 0
        self.probes = 0
        self.journal: list[dict] = []

    def ports(self):
        def turns(cursor):
            return self.events, self.events > cursor

        def probe(role):
            self.probes += 1
            return dict(self.state)

        def notify(role, notice):
            self.sent.append((role.value, dict(notice)))
            return self.outcome[role.value]

        return WatchPorts(
            task=lambda: None if self.task is None else dict(self.task),
            task_summary=lambda: None if self.task is None else {**self.task, "summary": "s", "active": True},
            peer=lambda role: self.worker if role is ActorRole.WORKER else self.manager,
            probe=probe, turns=turns, terminal=lambda: dict(self.terminal), outbox_busy=lambda role: self.busy,
            reports=lambda: [dict(item) for item in self.reports], requeue=lambda: self.requeued,
            paused=lambda: self.paused, notify=notify,
            restart_cause=lambda peer: {"cause": "restart_worker", "reason": "stuck", "requester": "manager"},
            journal=self.journal.append, worker_state=lambda: {"state": "busy", "task_id": self.task_id})

    def of(self, role, kind=None):
        return [n for r, n in self.sent if r == role and (kind is None or n["type"] == kind)]


class WatchdogFixture(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.clock = Clock()
        self.dog = Watchdog(self.world.ports(), clock=self.clock, wall=lambda: 5000.0)

    def run_for(self, seconds, step=1.0):
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.dog.tick()
            self.clock.now += step
        self.dog.tick()

    def checks(self):
        return self.world.of("worker", "status_check")


class StatusCheckTests(WatchdogFixture):
    def test_no_check_before_60_s_idle_then_one_check_with_the_task_and_terminal_state(self):
        self.run_for(IDLE_LIMIT - PROBE_INTERVAL - 1)
        self.assertEqual(self.checks(), [])
        self.run_for(PROBE_INTERVAL + 2)
        checks = self.checks()
        self.assertEqual(len(checks), 1)
        notice = checks[0]
        self.assertEqual(notice["task_id"], self.world.task_id)
        self.assertGreaterEqual(notice["idle_seconds"], IDLE_LIMIT)
        self.assertEqual(notice["terminal"]["last"], {"command_id": "c1", "status": "exited", "exit_code": 0})
        for needle in ("report done", "blocked", "commands remain", "end your turn"):
            self.assertIn(needle, notice["instruction"])
        self.assertTrue(self.world.of("worker"), "status checks only go to the worker")
        self.assertEqual(self.world.of("manager"), [])

    def test_probes_the_worker_at_most_every_5_s(self):
        self.run_for(30, step=0.25)
        self.assertLessEqual(self.world.probes, 30 / PROBE_INTERVAL + 1)
        self.assertGreaterEqual(self.world.probes, 5)

    def test_at_most_two_checks_then_one_stalled_notice_to_the_manager(self):
        self.run_for(IDLE_LIMIT * 6)
        self.assertEqual(len(self.checks()), MAX_CHECKS)
        stalled = self.world.of("manager", "worker_stalled")
        self.assertEqual(len(stalled), 1, "never more than one stalled notice per Task")
        notice = stalled[0]
        self.assertEqual(notice["task_id"], self.world.task_id)
        self.assertEqual(notice["checks_sent"], 2)
        self.assertGreaterEqual(notice["idle_seconds"], IDLE_LIMIT)
        self.assertEqual(notice["terminal"]["last"]["command_id"], "c1")
        for needle in ("workbench_status", "follow-up to_worker", "cancel", "restart_worker"):
            self.assertIn(needle, notice["instruction"])
        # the stalled notice comes only after the 2nd check plus another 60 s
        times = [r for r in self.world.journal if r.get("type") == "workbench_notice" and r["outcome"] == "sent"]
        self.assertEqual([r["notice_type"] for r in times], ["status_check", "status_check", "worker_stalled"])

    def test_the_stalled_notice_needs_another_60_s_after_the_second_check(self):
        self.run_for(IDLE_LIMIT * 2 + PROBE_INTERVAL + 2)
        self.assertEqual(len(self.checks()), 2)
        self.assertEqual(self.world.of("manager", "worker_stalled"), [])
        self.run_for(IDLE_LIMIT + 1)
        self.assertEqual(len(self.world.of("manager", "worker_stalled")), 1)

    def test_a_worker_terminal_call_or_report_resets_the_count_and_allows_a_new_stalled_notice(self):
        self.run_for(IDLE_LIMIT * 4)
        self.assertEqual(len(self.checks()), 2)
        self.assertEqual(len(self.world.of("manager", "worker_stalled")), 1)
        self.dog.worker_acted("terminal")
        self.run_for(IDLE_LIMIT * 4)
        self.assertEqual(len(self.checks()), 4, "the count started over")
        self.assertEqual(len(self.world.of("manager", "worker_stalled")), 2, "a new stall after the worker acted")
        self.assertIn("watchdog_reset", [r["type"] for r in self.world.journal])

    def test_a_worker_turn_restarts_the_idle_clock(self):
        for _ in range(4):
            self.run_for(IDLE_LIMIT - 10)
            self.world.events += 1  # the worker took a turn (e.g. answered the user)
        self.assertEqual(self.checks(), [])

    def test_nothing_while_paused_terminal_running_notice_pending_outbox_busy_or_worker_busy(self):
        cases = {
            "paused": lambda w: setattr(w, "paused", True),
            "terminal running": lambda w: w.terminal.update(running=True),
            "terminal notice pending": lambda w: w.terminal.update(notice_pending=True),
            "terminal unknown": lambda w: setattr(w, "terminal", {}),
            "outbox pending": lambda w: setattr(w, "busy", True),
            "worker busy": lambda w: w.state.update(idle=False),
            "worker has pending messages": lambda w: w.state.update(pending=True),
            "approval pending": lambda w: w.state.update(approvalPending=True),
            "tool running": lambda w: w.state.update(inFlightToolCount=1),
            "probe fails": lambda w: setattr(w, "state", None),
            "experiment task": lambda w: w.task.update(kind="experiment"),
            "task not running": lambda w: w.task.update(status="starting"),
            "task message not in this worker session": lambda w: w.task.update(worker_session=["other", 1]),
            "no task": lambda w: setattr(w, "task", None),
        }
        for name, apply in cases.items():
            with self.subTest(name):
                self.setUp()
                apply(self.world)
                self.run_for(IDLE_LIMIT * 4)
                self.assertEqual(self.checks(), [], name)
                self.assertEqual(self.world.of("manager"), [], name)

    def test_a_busy_period_in_between_restarts_the_60_s(self):
        self.run_for(IDLE_LIMIT - 5)
        self.world.terminal["running"] = True
        self.run_for(3)
        self.world.terminal["running"] = False
        self.run_for(IDLE_LIMIT - 10)
        self.assertEqual(self.checks(), [])
        self.run_for(20)
        self.assertEqual(len(self.checks()), 1)

    def test_a_deferred_check_is_retried_with_the_same_notice_id_never_duplicated(self):
        self.world.outcome["worker"] = "deferred"
        self.run_for(IDLE_LIMIT + PROBE_INTERVAL + 10)
        attempts = self.checks()
        self.assertGreater(len(attempts), 1)
        self.assertEqual(len({n["notice_id"] for n in attempts}), 1, "a retry keeps its notice_id")
        deferred = [r for r in self.world.journal if r.get("outcome") == "deferred"]
        self.assertEqual(len(deferred), 1, "a deferral is journaled once")
        self.world.outcome["worker"] = "delivered"
        self.run_for(3)
        ids = [n["notice_id"] for n in self.checks()]
        self.assertEqual(len(set(ids)), 1, "the delivered check is the deferred one")
        self.run_for(10)
        self.assertEqual(len(set(n["notice_id"] for n in self.checks())), 1, "and nothing more within 60 s")

    def test_an_unknown_outcome_is_never_resent(self):
        self.world.outcome["worker"] = "unknown"
        self.run_for(IDLE_LIMIT + PROBE_INTERVAL + 20)
        self.assertEqual(len(self.checks()), 1)

    def test_a_new_task_starts_its_own_count(self):
        self.run_for(IDLE_LIMIT * 4)
        self.assertEqual(len(self.world.of("manager", "worker_stalled")), 1)
        self.world.task = {**self.world.task, "task_id": str(uuid4())}
        self.run_for(IDLE_LIMIT * 4)
        self.assertEqual(len(self.checks()), 4)
        self.assertEqual(len(self.world.of("manager", "worker_stalled")), 2)

    def test_the_view_has_the_watch_of_the_open_task(self):
        self.run_for(IDLE_LIMIT + PROBE_INTERVAL + 2)
        view = self.dog.view()
        self.assertEqual(view["watch"]["task_id"], self.world.task_id)
        self.assertEqual(view["watch"]["checks_sent"], 1)
        self.assertIsNone(view["report_wait"])


class SessionChangeTests(WatchdogFixture):
    def test_a_new_worker_session_with_an_open_task_tells_the_manager_once(self):
        self.dog.tick()
        self.world.worker = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=333)
        self.run_for(3)
        notices = self.world.of("manager", "worker_restarted")
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual(notice["task_id"], self.world.task_id)
        self.assertEqual((notice["cause"], notice["reason"], notice["requester"]), ("restart_worker", "stuck", "manager"))
        self.assertEqual(notice["session_id"], self.world.worker.session_id)
        self.assertEqual(notice["terminal"]["last"]["command_id"], "c1")
        self.assertIn("follow-up to_worker on this task_id", notice["instruction"])
        # the new session has not received the Task: no status check to it
        self.run_for(IDLE_LIMIT * 3)
        self.assertEqual(self.checks(), [])

    def test_a_new_worker_session_without_a_task_sends_nothing(self):
        self.world.task = None
        self.dog.tick()
        self.world.worker = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=333)
        self.run_for(3)
        self.assertEqual(self.world.sent, [])

    def test_a_new_manager_session_gets_one_recovery_notice_with_counts_and_requeues_reports(self):
        unknown_id = str(uuid4())
        self.world.reports = [
            {"handoff_id": "h1", "origin": "worker", "state": "unknown", "submitted": False, "task_id": self.world.task_id,
             "message_id": unknown_id, "report_kind": "done"},
            {"handoff_id": "h2", "origin": "worker", "state": "pending", "submitted": False,
             "task_id": self.world.task_id, "message_id": None, "report_kind": "progress"}]
        self.dog._noticed_unknown.add("h1")  # (already told before the restart)
        self.dog.tick()
        self.world.requeued = 1
        self.world.manager = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=444)
        self.run_for(2)
        notices = self.world.of("manager", "manager_recovery")
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual(notice["reports_resent"], 1)
        self.assertEqual(notice["reports_unknown"], 1)
        self.assertEqual(notice["unknown_reports"], [{"task_id": self.world.task_id, "message_id": unknown_id,
                                                      "report_kind": "done"}])
        self.assertNotIn("message", notice["unknown_reports"][0], "no report content")
        self.assertEqual(notice["task"]["task_id"], self.world.task_id)
        self.assertEqual(notice["worker"]["state"], "busy")
        self.assertIn("workbench_status", notice["instruction"])

    def test_the_first_registration_is_not_a_restart(self):
        self.run_for(3)
        self.assertEqual(self.world.of("manager"), [])

    def test_manager_notices_keep_their_order_and_wait_for_a_deliverable_manager(self):
        self.world.outcome["manager"] = "deferred"
        self.dog.tick()
        self.world.worker = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=333)
        self.run_for(10)
        first = self.world.of("manager")
        self.assertTrue(first)
        self.assertEqual({n["notice_id"] for n in first}, {first[0]["notice_id"]}, "only the head is tried")
        self.world.outcome["manager"] = "delivered"
        self.run_for(3)
        self.assertEqual(len({n["notice_id"] for n in self.world.of("manager")}), 1)


class ReportTests(WatchdogFixture):
    def test_an_unknown_report_is_told_once_without_content(self):
        message_id = str(uuid4())
        self.world.reports = [{"handoff_id": "h1", "origin": "worker", "state": "unknown", "submitted": False,
                               "task_id": self.world.task_id, "message_id": message_id, "report_kind": "done",
                               "text": "secret result text"}]
        self.run_for(5)
        notices = self.world.of("manager", "report_delivery_unknown")
        self.assertEqual(len(notices), 1)
        self.assertEqual((notices[0]["task_id"], notices[0]["message_id"], notices[0]["report_kind"]),
                         (self.world.task_id, message_id, "done"))
        self.assertNotIn("secret result text", repr(notices[0]))

    def test_a_submitted_or_backend_report_or_another_state_is_not_told(self):
        self.world.reports = [
            {"handoff_id": "a", "origin": "worker", "state": "unknown", "submitted": True},
            {"handoff_id": "b", "origin": "backend", "state": "unknown", "submitted": False},
            {"handoff_id": "c", "origin": "worker", "state": "delivered", "submitted": True},
            {"handoff_id": "d", "origin": "worker", "state": "rejected", "submitted": False}]
        self.run_for(5)
        self.assertEqual(self.world.of("manager", "report_delivery_unknown"), [])

    def test_editor_wait_shows_after_30_s_and_clears(self):
        self.world.reports = [{"handoff_id": "h", "origin": "worker", "state": "pending", "submitted": False,
                               "editor_since": self.clock.now}]
        self.clock.now += 29
        self.assertIsNone(self.dog.report_wait())
        self.clock.now += 2
        wait = self.dog.report_wait()
        self.assertEqual((wait["count"], wait["reason"]), (1, "manager_editor_not_empty"))
        self.assertEqual(wait["since"], 5000.0 - 31)
        self.world.reports[0].update(state="delivered", submitted=True)
        self.assertIsNone(self.dog.report_wait())
        self.world.reports[0].update(state="pending", submitted=False, editor_since=None)
        self.assertIsNone(self.dog.report_wait(), "waiting for another reason is not shown")


class ArgumentTests(unittest.TestCase):
    def test_restart_reason_is_required_non_blank_and_bounded(self):
        self.assertEqual(validate_restart_arguments({"reason": "stuck"}), [])
        for bad in ({}, {"reason": ""}, {"reason": "   "}, {"reason": None}, {"reason": 3}, {"reason": "x" * 501},
                    {"reason": "a\x00b"}, "x"):
            with self.subTest(bad=str(bad)[:30]):
                errors = validate_restart_arguments(bad)
                self.assertTrue(errors)
                self.assertNotIn("x" * 50, " ".join(errors), "the value is never echoed")
        self.assertTrue(validate_restart_arguments({"reason": "ok", "force": True}))

    def test_status_task_id_is_optional(self):
        task_id = str(uuid4())
        self.assertEqual(validate_status_arguments({}), (None, []))
        self.assertEqual(validate_status_arguments(None), (None, []))
        self.assertEqual(validate_status_arguments({"task_id": None}), (None, []))
        self.assertEqual(validate_status_arguments({"task_id": " "}), (None, []))
        self.assertEqual(validate_status_arguments({"task_id": task_id}), (task_id, []))
        self.assertTrue(validate_status_arguments({"task_id": "nope"})[1])
        self.assertTrue(validate_status_arguments({"other": 1})[1])


if __name__ == "__main__":
    unittest.main()
