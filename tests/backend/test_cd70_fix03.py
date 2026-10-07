"""p27-cd70-fix-03 (smoke-02 D1): a worker done/blocked report made while no manager OMP is connected.

C-D70 (4)/(5): worker reports that could not reach the previous manager session go to the new one. A ``to_manager``
done/blocked report while the manager OMP is gone is queued unbound (no target session) and delivered once to the
next manager session; ``manager_recovery`` counts it in ``reports_resent`` and ``workbench_status`` lists it while it
waits. Other handoffs keep ``held:target_not_connected``. The watchdog does not nag the worker meanwhile.

The real TaskFlow inside the real HandoffService (and the real Watchdog for the end-to-end case) with a fake mailbox
and fake bridge peers whose manager session can disappear and come back as a new one (no OMP, no provider).
"""

from __future__ import annotations

import time
import unittest
from uuid import uuid4

from workbench.backend.flow_recovery import Watchdog, WatchPorts
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected, DeliveryReceipt, MailboxStatus

import test_cd70_task_resend as rs
import test_task_flow as fx

NEW_MANAGER = str(uuid4())
WAITING = "manager_session"


class Peer:
    def __init__(self, session):
        self.session_id, self.generation, self.pid = session[0], session[1], 1


class PeersMailbox(rs.SessionMailbox):
    """TaskMailbox.create_message binds a message to the target's current session; none -> BridgeDisconnected."""

    def create_message(self, *args, **kwargs):
        target = ActorRole(args[4] if len(args) > 4 else kwargs["target_role"])
        if target is ActorRole.MANAGER and self.test.manager_session is None:
            raise BridgeDisconnected("no connected manager OMP session")
        message = super().create_message(*args, **kwargs)
        if target is ActorRole.MANAGER:
            message.session_id, message.session_generation = self.test.manager_session
        return message


class Fixture(rs.ResendFixture):
    def setUp(self):
        self.manager_session: tuple[str, int] | None = (fx.MANAGER_SESSION, 1)
        self.mailbox = None
        super().setUp()

    def open(self, *, start=True):
        if not isinstance(self.mailbox, PeersMailbox):
            self.mailbox = PeersMailbox(self)
        flow = super().open(start=start)
        self.service._peer_lookup = self.peer
        return flow

    def peer(self, role):
        session = self.manager_session if role is ActorRole.MANAGER else self.worker_session
        return None if session is None else Peer(session)

    def manager_reports(self):
        return [m for m in self.mailbox.to(ActorRole.MANAGER) if m.kind is MessageKind.REPORT]

    def outbox_states(self):
        return [r.get("state") for r in self.handoff_records() if r.get("type") == "outbox"]

    def handoff_records(self):
        import json
        path = self.root / "workflow" / "handoffs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def report_while_manager_gone(self, kind="done", message="all 3 commands ran; cpu: 8 cores"):
        task_id = self.work()
        self.manager_session = None  # the manager OMP exited (bridge not connected)
        args = {"kind": kind, "message": message, "task_id": task_id}
        if kind == "blocked":
            args["reason"] = "disk full"
        result = self.to_manager(args)
        return task_id, result


class QueuedWhileManagerGoneTests(Fixture):
    def test_a_done_report_is_queued_unbound_and_reaches_the_next_manager_session_once(self):
        task_id, result = self.report_while_manager_gone()
        self.assertEqual(result["status"], "queued", result)
        self.assertEqual(result.get("waiting_for"), WAITING)
        self.assertIn("handoff_id", result)
        self.assertIn("next manager session", result["detail"])
        self.assertIn("Do not send it again", result["detail"])
        self.assertIn("end your turn", result["detail"])
        # it waits: nothing created, listed with its text, the worker's lane is busy, the Task waits for it
        self.assertTrue(fx.wait_until(lambda: "deferred" in self.outbox_states()))
        self.assertEqual(self.manager_reports(), [])
        entry = self.service.report_entries()[0]
        self.assertEqual((entry["state"], entry["origin"], entry["report_kind"]), ("pending", "worker", "done"))
        self.assertEqual(entry["text"], "all 3 commands ran; cpu: 8 cores")
        self.assertEqual(entry["reason"], "target_not_connected")
        self.assertTrue(self.service.lane_busy(ActorRole.WORKER))
        view = self.flow.task_view()
        self.assertEqual((view["status"], view["held_reason"]), ("running", "done_report_pending"))
        # a new manager session registers
        self.manager_session = (NEW_MANAGER, 1)
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 1)
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "delivered"))
        self.assertEqual([m.session_id for m in self.manager_reports()], [NEW_MANAGER])
        self.assertEqual(self.manager_reports()[0].payload["message"], "all 3 commands ran; cpu: 8 cores")
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["status"] == "closed"))
        self.assertEqual(self.flow.task_view()["closed_reason"], "done")
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 0, "counted once")
        self.assertFalse(self.service.lane_busy(ActorRole.WORKER))

    def test_a_blocked_report_waits_too(self):
        _, result = self.report_while_manager_gone("blocked", "cannot write: disk full")
        self.assertEqual((result["status"], result.get("waiting_for")), ("queued", WAITING))
        self.manager_session = (NEW_MANAGER, 1)
        self.assertTrue(fx.wait_until(lambda: len(self.manager_reports()) == 1))
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["status"] == "blocked"))

    def test_the_count_includes_a_report_the_lane_delivered_before_the_recovery_notice(self):
        self.report_while_manager_gone()
        self.manager_session = (NEW_MANAGER, 1)
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "delivered"))
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 1)
        self.assertEqual(len(self.manager_reports()), 1)

    def test_progress_and_answers_are_still_held_when_the_manager_is_gone(self):
        task_id = self.work()
        self.manager_session = None
        for args in ({"kind": "progress", "message": "1 of 3", "task_id": task_id},
                     {"kind": "report", "message": "fyi", "task_id": task_id},
                     {"kind": "answer", "message": "yes", "task_id": task_id, "in_reply_to": str(uuid4())}):
            result = self.to_manager(args)
            self.assertEqual((result["status"], result.get("reason")), ("held", "target_not_connected"), args)
            self.assertNotIn("waiting_for", result)
        self.assertEqual(self.service.report_entries(), [])

    def test_to_worker_is_still_held_when_the_worker_is_gone(self):
        task_id = self.work()
        self.worker_session = None
        result = self.to_worker({"kind": "work", "message": "continue", "task_id": task_id})
        self.assertEqual((result["status"], result.get("reason")), ("held", "target_not_connected"), result)
        self.assertNotIn("waiting_for", result)

    def test_a_manager_back_in_the_same_session_gets_it_and_a_later_new_session_does_not_count_it(self):
        self.report_while_manager_gone()
        self.manager_session = (fx.MANAGER_SESSION, 1)  # the bridge reconnected, same OMP session
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "delivered"))
        self.assertEqual([m.session_id for m in self.manager_reports()], [fx.MANAGER_SESSION])
        self.manager_session = (NEW_MANAGER, 1)
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 0)
        self.assertEqual(len(self.manager_reports()), 1)

    def test_an_unknown_delivery_to_the_new_session_is_never_resent_nor_counted(self):
        self.report_while_manager_gone()
        original = self.mailbox.deliver

        def unknown(message, *, timeout=20):
            if message.target_role is ActorRole.MANAGER:
                self.mailbox.delivered.append(message.message_id)
                return DeliveryReceipt(message.message_id, None, message.target_role, message.session_id, 1,
                                       MailboxStatus.UNKNOWN, {"reason": "x"})
            return original(message, timeout=timeout)
        self.mailbox.deliver = unknown
        self.manager_session = (NEW_MANAGER, 1)
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "unknown"))
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 0)
        time.sleep(0.1)
        self.assertEqual(len(self.manager_reports()), 1, "an unknown delivery is never sent again")

    def test_a_withdrawn_report_is_neither_sent_nor_counted(self):
        task_id, _ = self.report_while_manager_gone()
        self.assertEqual(self.service.withdraw(task_id), ["report"])
        self.manager_session = (NEW_MANAGER, 1)
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 0)
        time.sleep(0.1)
        self.assertEqual(self.manager_reports(), [])

    def test_pause_keeps_it_and_resume_delivers_it_once(self):
        self.report_while_manager_gone()
        self.controller.automation = {**self.controller.automation, "state": "paused"}
        self.manager_session = (NEW_MANAGER, 1)
        self.assertTrue(fx.wait_until(lambda: "kept_paused" in self.outbox_states()))
        self.assertEqual(self.manager_reports(), [])
        self.assertEqual(self.service.report_entries()[0]["state"], "pending")
        self.controller.automation = {**self.controller.automation, "state": "active"}
        self.assertTrue(fx.wait_until(lambda: len(self.manager_reports()) == 1))
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "delivered"))

    def test_a_report_during_pause_is_still_held_paused(self):
        task_id = self.work()
        self.manager_session = None
        self.controller.automation = {**self.controller.automation, "state": "paused"}
        result = self.to_manager({"kind": "done", "message": "x", "task_id": task_id})
        self.assertEqual((result["status"], result.get("reason")), ("held", "paused"))

    def test_the_old_session_path_still_requeues(self):
        """A report already created for the old session (the manager OMP exits while it waits) goes to the new one."""
        task_id = self.work()
        original = self.mailbox.deliver
        busy = [True]

        def deliver(message, *, timeout=20):
            if message.target_role is ActorRole.MANAGER and busy[0]:
                self.mailbox.delivered.append(message.message_id)
                return DeliveryReceipt(message.message_id, None, message.target_role, message.session_id, 1,
                                       MailboxStatus.DEFERRED, {"reason": "current_omp_state_not_safe",
                                                                "blockers": ["busy"]})
            return original(message, timeout=timeout)
        self.mailbox.deliver = deliver
        result = self.to_manager({"kind": "done", "message": "finished", "task_id": task_id})
        self.assertEqual(result["status"], "queued")
        self.assertNotIn("waiting_for", result)
        self.assertTrue(fx.wait_until(lambda: len(self.manager_reports()) == 1
                                      and self.service.report_entries()[0]["status"] == "deferred"))
        self.manager_session = None  # the manager OMP exits
        time.sleep(0.05)
        self.manager_session = (NEW_MANAGER, 1)
        busy[0] = False
        self.assertTrue(fx.wait_until(lambda: self.service.requeue_for_new_session(ActorRole.MANAGER) == 1))
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "delivered"))
        self.assertEqual([m.session_id for m in self.manager_reports()], [fx.MANAGER_SESSION, NEW_MANAGER])
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["closed_reason"] == "done"))


class EndToEndTests(Fixture):
    """The watchdog wired to the real TaskFlow and HandoffService as service.py wires it (fake clock)."""

    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.notices: list[tuple[ActorRole, dict]] = []
        self.dog = Watchdog(WatchPorts(
            task=self.flow.watch_view, task_summary=self.flow.task_view, peer=self.peer,
            probe=lambda role: {"idle": True, "pending": False, "approvalPending": False, "inFlightToolCount": 0},
            turns=lambda cursor: (cursor, False), terminal=lambda: {"running": False},
            outbox_busy=self.service.lane_busy, reports=self.service.report_entries,
            requeue=lambda: self.service.requeue_for_new_session(ActorRole.MANAGER), paused=self.paused,
            notify=self.notify, worker_state=self.flow.worker_view), clock=lambda: self.now, wall=lambda: 5000.0)

    def notify(self, role, notice):
        if role is ActorRole.MANAGER and self.manager_session is None:
            return "not_connected"
        self.notices.append((role, dict(notice)))
        return "delivered"

    def run_for(self, seconds, step=5.0):
        end = self.now + seconds
        while self.now < end:
            self.now += step
            self.dog.tick()

    def test_manager_gone_then_a_new_session_registers(self):
        task_id = self.work()
        self.dog.tick()  # the watchdog knows the first manager session
        self.manager_session = None
        result = self.to_manager({"kind": "done", "message": "cpu 8, mem 32G, disk 1T", "task_id": task_id})
        self.assertEqual((result["status"], result.get("waiting_for")), ("queued", WAITING))
        self.assertTrue(fx.wait_until(lambda: "deferred" in self.outbox_states()))
        self.run_for(400)  # far beyond IDLE_LIMIT * (MAX_CHECKS + 1)
        self.assertEqual([n for n in self.notices if n[0] is ActorRole.WORKER], [],
                         "no status_check while the worker's report waits for the manager")
        self.assertEqual(self.notices, [])
        self.manager_session = (NEW_MANAGER, 1)
        self.dog.tick()
        recovery = [n for role, n in self.notices if n["type"] == "manager_recovery"]
        self.assertEqual(len(recovery), 1)
        self.assertEqual((recovery[0]["reports_resent"], recovery[0]["reports_unknown"]), (1, 0))
        self.assertEqual(recovery[0]["session_id"], NEW_MANAGER)
        self.assertTrue(fx.wait_until(lambda: len(self.manager_reports()) == 1
                                      and self.flow.task_view()["status"] == "closed"))
        self.assertEqual(self.manager_reports()[0].session_id, NEW_MANAGER)
        self.assertEqual(self.flow.task_view()["closed_reason"], "done")
        self.run_for(200)
        self.assertEqual(len(self.manager_reports()), 1, "no duplicate")
        self.assertEqual([n["type"] for _, n in self.notices], ["manager_recovery"])


if __name__ == "__main__":
    unittest.main()
