"""p27-cd70-fix-01: corrections from review-01 and test-01 (no OMP, no provider).

- P2-1/P3-3: restart_worker never tells the manager to follow up now; worker_restarted is the one cue; a second
  follow-up while a full re-send is on its way is a plain follow-up; commands_already_run is read when the lane
  creates the message.
- P3-1: a first TASK whose delivery ended unknown counts as in that worker session; RESEND_NOTE is neutral.
- P3-2: a re-send held at queue time is retried by the next follow-up.
- P3-4/O3: restart_worker racing the shutdown answers backend_shutdown.
- test P3-1: reports_resent counts reports the lane re-queued before the notice was built.
- test O1: manager_recovery drops a waiting report_delivery_unknown notice for a report it lists.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from uuid import uuid4

from workbench.backend.flow import OutboundMessage
from workbench.backend.flow_recovery import (
    RESTART_DETAIL, RESTART_NO_TASK_DETAIL, RESTART_NO_TASK_PENDING_DETAIL, RESTART_PENDING_DETAIL, RESTARTED_HINT,
    restart_detail,
)
from workbench.backend.flow_tasks import RESEND_NOTE
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus

import test_cd70_handoff_recovery as hr
import test_cd70_restart_worker as rw
import test_cd70_task_resend as rs
import test_cd70_watchdog as wd


class WordingTests(unittest.TestCase):
    def test_restart_result_says_wait_for_the_notice_and_the_notice_is_the_cue(self):
        for text in (RESTART_DETAIL, RESTART_PENDING_DETAIL):
            self.assertIn("Do not send a follow-up now", text)
            self.assertIn("worker_restarted", text)
            self.assertIn("exactly one follow-up", text)
        self.assertIn("This notice is the cue", RESTARTED_HINT)
        # review-02 P3-W: without an open Task no notice comes and nothing is re-sent
        for text in (RESTART_NO_TASK_DETAIL, RESTART_NO_TASK_PENDING_DETAIL):
            self.assertIn("no open Task", text)
            self.assertIn("no worker_restarted notice follows", text)
            self.assertIn("nothing is re-sent", text)
            self.assertIn("no memory", text)
            self.assertNotIn("follow-up", text)
        self.assertEqual([restart_detail(True, True), restart_detail(True, False), restart_detail(False, True),
                          restart_detail(False, False)],
                         [RESTART_DETAIL, RESTART_PENDING_DETAIL, RESTART_NO_TASK_DETAIL, RESTART_NO_TASK_PENDING_DETAIL])
        self.assertIn("if you already sent that follow-up", RESTARTED_HINT)

    def test_resend_note_is_neutral(self):
        self.assertIn("may not have it", RESEND_NOTE)
        for claim in ("ended", "restart", "you are a new session"):
            self.assertNotIn(claim, RESEND_NOTE)


class ResendOnceTests(rs.ResendFixture):
    def hold_worker_deliveries(self):
        gate = threading.Event()
        original = self.mailbox.deliver

        def deliver(message, *, timeout=20):
            if message.target_role is ActorRole.WORKER:
                gate.wait(10)
            return original(message, timeout=timeout)
        self.mailbox.deliver = deliver
        self.addCleanup(gate.set)
        return gate

    def test_a_second_follow_up_while_the_resend_is_on_its_way_is_plain(self):
        task_id = self.work()
        self.worker_session = (str(uuid4()), 1)
        gate = self.hold_worker_deliveries()
        first = self.to_worker({"kind": "work", "message": "continue", "task_id": task_id})
        second = self.to_worker({"kind": "work", "message": "and then report", "task_id": task_id})
        self.assertTrue(first.get("resent_task"))
        self.assertNotIn("resent_task", second)
        gate.set()
        self.assertTrue(rs.fx.wait_until(lambda: len(self.to_workers()) == 3))
        resent = [m for m in self.to_workers() if m.payload.get("resent_task")]
        self.assertEqual(len(resent), 1, "one full re-send only")
        self.assertEqual(self.to_workers()[-1].payload["message"], "and then report")

    def test_commands_already_run_is_read_when_the_lane_creates_the_message(self):
        task_id = self.work()
        gate = self.hold_worker_deliveries()  # the lane is busy with a plain follow-up ...
        self.to_worker({"kind": "work", "message": "plain one", "task_id": task_id})
        self.assertTrue(rs.fx.wait_until(lambda: len(self.to_workers()) == 2))
        self.worker_session = (str(uuid4()), 1)
        resent = self.to_worker({"kind": "work", "message": "continue", "task_id": task_id})  # ... queued behind it
        self.assertTrue(resent.get("resent_task"))
        later = {"command": "free -h", "status": "exited", "exit_code": 0, "signal": None, "duration_seconds": 0.1,
                 "log_path": "/tmp/x/terminal/c2.log"}
        self.runs_listed = [dict(rs.RUN), later]  # ran after the manager's call, before the lane created it
        gate.set()
        self.assertTrue(rs.fx.wait_until(lambda: len(self.to_workers()) == 3))
        self.assertEqual(self.to_workers()[-1].payload["commands_already_run"], [rs.RUN, later])

    def test_after_the_resend_was_rejected_the_next_follow_up_resends_again(self):
        task_id = self.work()
        self.worker_session = (str(uuid4()), 1)
        original = self.mailbox.deliver

        def reject_once(message, *, timeout=20):
            self.mailbox.deliver = original
            self.mailbox.delivered.append(message.message_id)
            return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1,
                                   MailboxStatus.REJECTED, {"reason": "x"})
        self.mailbox.deliver = reject_once
        self.follow_up(task_id)
        _, again = self.follow_up(task_id, "try again")
        self.assertTrue(again.payload.get("resent_task"))


class UnknownTaskTests(rs.ResendFixture):
    def test_an_unknown_first_task_counts_as_in_that_session(self):
        original = self.mailbox.deliver

        def unknown(message, *, timeout=20):
            self.mailbox.deliver = original
            self.mailbox.delivered.append(message.message_id)
            return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1,
                                   MailboxStatus.UNKNOWN, {"reason": "x"})
        self.mailbox.deliver = unknown
        task_id = self.to_worker({"kind": "work", "message": "collect", "spec": {"goal": "g", "paths": []},
                                  "commands": ["lscpu"]})["task_id"]
        self.assertTrue(rs.fx.wait_until(lambda: self.flow.task_view()["held_reason"] == "instruction_unknown"))
        self.assertEqual(self.flow.watch_view()["worker_session"], list(self.worker_session),
                         "the watchdog checks it (the worker may have it)")
        _, message = self.follow_up(task_id)
        self.assertNotIn("resent_task", message.payload, "no false re-send to the same session")


class HeldAtQueueTests(rs.ResendFixture):
    def test_a_resend_held_at_queue_is_retried_by_the_next_follow_up(self):
        task_id = self.work()
        self.worker_session = (str(uuid4()), 1)
        self.service._peer_lookup = lambda role: None  # the worker drops off between decide and queue
        held = self.to_worker({"kind": "work", "message": "continue", "task_id": task_id})
        self.assertEqual((held["status"], held["reason"]), ("held", "target_not_connected"), held)
        self.service._peer_lookup = None
        _, message = self.follow_up(task_id, "continue now")
        self.assertTrue(message.payload.get("resent_task"))
        self.assertIn("task_resend_not_queued", [r["type"] for r in self.ledger()])

    def test_a_first_task_resend_held_at_queue_restores_the_task_state(self):
        from workbench.ipc.bridge_g3.mailbox import MailboxError
        create, failed = self.mailbox.create_message, []

        def create_once_failing(*args, **kwargs):
            if not failed:
                failed.append(True)
                raise MailboxError("x")
            return create(*args, **kwargs)
        self.mailbox.create_message = create_once_failing
        task_id = self.to_worker({"kind": "work", "message": "collect", "spec": {"goal": "g", "paths": []}})["task_id"]
        self.assertTrue(rs.fx.wait_until(lambda: self.flow.task_view()["held_reason"] == "instruction_rejected"))
        self.worker_session = (str(uuid4()), 1)
        self.service._peer_lookup = lambda role: None
        self.to_worker({"kind": "work", "message": "start", "task_id": task_id})
        self.assertEqual(self.flow._task_message_state.get(task_id), "not_sent", "not stuck at queued")
        self.service._peer_lookup = None
        _, message = self.follow_up(task_id, "start now")
        self.assertEqual(message.kind, MessageKind.TASK)


class ShutdownRaceTests(rw.RestartWorkerFixture):
    def test_a_restart_after_the_jobs_were_aborted_is_refused_truthfully(self):
        self.backend._abort_restart_jobs("closing")  # _close ran it; the tool thread passed its first check
        started = time.monotonic()
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "late"})
        self.assertEqual((result["status"], result["reason"]), ("refused", "backend_shutdown"))
        self.assertLess(time.monotonic() - started, 3.0, "no 8 s wait")
        self.assertTrue(self.backend._restart_lock.acquire(blocking=False), "the lock was released")
        self.backend._restart_lock.release()
        self.assertEqual(self.backend._restart_jobs, [])

    def test_registered_false_result_says_wait_for_the_notice(self):
        self.open_task()
        real = self.backend._bridge_peer
        self.backend._bridge_peer = lambda role: None if role is ActorRole.WORKER else real(role)
        with mock.patch.object(rw.service_module, "RESTART_TOOL_BUDGET", 8.5):
            result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "stuck"})
        self.backend._bridge_peer = real
        self.assertEqual(result["status"], "restarted", result)
        self.assertFalse(result["worker"]["registered"])
        self.assertEqual(result["detail"], RESTART_PENDING_DETAIL)


class RestartDetailTests(rw.RestartWorkerFixture):
    """review-02 P3-W: the restart_worker result is true with and without an open Task."""

    def test_with_an_open_task_wait_for_the_notice_then_one_follow_up(self):
        task = self.open_task()
        self.backend.watchdog.tick()
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "stuck"})
        self.assertEqual(result["detail"], RESTART_DETAIL)
        self.wait(lambda: any(n["notice"]["type"] == "worker_restarted" and n["notice"]["task_id"] == task.task_id
                              for n in self.received()), "the worker_restarted notice the result announces")

    def test_with_a_blocked_task_the_notice_comes_too(self):
        task = self.open_task()
        task.status = "blocked"
        self.backend.watchdog.tick()
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "stuck"})
        self.assertEqual(result["detail"], RESTART_DETAIL)
        self.wait(lambda: any(n["notice"]["type"] == "worker_restarted" for n in self.received()),
                  "the worker_restarted notice for the blocked Task")

    def test_without_an_open_task_no_notice_and_nothing_to_resend(self):
        self.backend.watchdog.tick()
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "fresh start"})
        self.assertEqual(result["status"], "restarted", result)
        self.assertEqual(result["detail"], RESTART_NO_TASK_DETAIL)
        self.wait(lambda: self.backend.bridge.peer(ActorRole.WORKER).pid == self.backend.panes[rw.PaneId.WORKER_OMP].pid,
                  "the new worker")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.backend._tick(0.05)
        self.assertEqual([n for n in self.received() if n["notice"]["type"] == "worker_restarted"], [],
                         "no notice, as the result said")


class RequeueCountTests(hr.Fixture):
    def test_a_report_the_lane_already_re_sent_is_counted_once(self):
        peers = {ActorRole.MANAGER: hr.Peer(hr.hs.MANAGER_SESSION)}
        service = self.service(peer_lookup=lambda role: peers.get(role))
        self.active = self.active_task()
        self.mailbox.sessions[ActorRole.MANAGER] = (hr.NEW_MANAGER, 1)  # the lane meets the new session first
        service.handle(ActorRole.WORKER, hr.hs.request("to_manager", {"kind": "progress", "message": "m"}))
        self.assertTrue(hr.hs.wait_until(lambda: service.report_entries()[0]["state"] == "delivered"))
        peers[ActorRole.MANAGER] = hr.Peer(hr.NEW_MANAGER)
        self.assertEqual(service.requeue_for_new_session(ActorRole.MANAGER), 1)
        self.assertEqual(service.requeue_for_new_session(ActorRole.MANAGER), 0, "counted once")

    def test_a_report_queued_after_the_new_session_registered_is_not_counted(self):
        peers = {ActorRole.MANAGER: hr.Peer(hr.NEW_MANAGER)}
        service = self.service(peer_lookup=lambda role: peers.get(role))
        self.active = self.active_task()
        self.mailbox.sessions[ActorRole.MANAGER] = (hr.NEW_MANAGER, 1)
        self.mailbox.statuses = [MailboxStatus.DEFERRED] * 100
        service.handle(ActorRole.WORKER, hr.hs.request("to_manager", {"kind": "progress", "message": "m"}))
        self.assertTrue(hr.hs.wait_until(lambda: len(self.mailbox.delivered) >= 1))
        self.assertEqual(service.requeue_for_new_session(ActorRole.MANAGER), 0)
        self.mailbox.statuses.clear()


class RecoveryDropsUnknownTests(wd.WatchdogFixture):
    def test_a_waiting_unknown_notice_for_a_listed_report_is_dropped(self):
        self.world.reports = [{"handoff_id": "h1", "origin": "worker", "state": "unknown", "submitted": False,
                               "task_id": self.world.task_id, "message_id": str(uuid4()), "report_kind": "done"}]
        self.world.outcome["manager"] = "deferred"  # the old manager session is busy
        self.run_for(3)
        self.assertEqual(len(self.dog.queued()), 1)
        self.world.manager = SimpleNamespace(session_id=str(uuid4()), generation=1, pid=9)
        self.world.outcome["manager"] = "delivered"
        self.run_for(5)
        sent = [n["type"] for r, n in self.world.sent if r == "manager" and self.world.outcome]
        delivered = self.world.of("manager")
        self.assertEqual(delivered[-1]["type"], "manager_recovery")
        self.assertEqual(delivered[-1]["reports_unknown"], 1)
        self.assertEqual(self.dog.queued(), [], "the unknown notice was dropped, not sent after the recovery")
        later = [n for n in delivered if n["type"] == "report_delivery_unknown"]
        self.assertTrue(all(n["notice_id"] == later[0]["notice_id"] for n in later), "only the deferred attempts")
        self.assertIn("dropped_listed_in_manager_recovery", [r.get("outcome") for r in self.world.journal])
        self.assertTrue(sent)


if __name__ == "__main__":
    unittest.main()
