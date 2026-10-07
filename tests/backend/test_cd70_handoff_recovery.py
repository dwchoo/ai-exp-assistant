"""C-D70 (2)/(5): the outbox side of recovery (fake mailbox, no OMP).

- A worker->manager message that was never submitted follows a new manager session (created again for it);
  a message to the worker is still dropped as ``target_session_changed``.
- A deferral records why (``blockers``) and since when the manager's composer blocked it.
- ``report_entries`` / ``lane_busy`` for the watchdog and ``workbench_status``; the mailbox's ``state_blockers``
  and the bridge's non-blocking ``events_since``.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from workbench.backend.flow import REQUEUE_LIMIT
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import (
    DeliveryReceipt, G3BridgeServer, MailboxMessage, MailboxStatus, TaskMailbox, state_blockers,
)

import test_handoff_service as hs

NEW_MANAGER = str(uuid4())


class Peer:
    def __init__(self, session_id, generation=1):
        self.session_id, self.generation, self.pid = session_id, generation, 1


class Fixture(unittest.TestCase):
    """The HandoffServiceTests helpers without its tests."""

    setUp = hs.HandoffServiceTests.setUp
    tearDown = hs.HandoffServiceTests.tearDown
    service = hs.HandoffServiceTests.service
    records = hs.HandoffServiceTests.records
    active_task = hs.HandoffServiceTests.active_task


class RequeueTests(Fixture):
    def setUp(self):
        super().setUp()
        self.peers = {ActorRole.MANAGER: Peer(hs.MANAGER_SESSION), ActorRole.WORKER: Peer(hs.WORKER_SESSION)}
        self.events: list[str] = []

    def make(self):
        service = self.service(peer_lookup=lambda role: self.peers.get(role))
        self.active = self.active_task()
        return service

    def report(self, service, kind="done"):
        return service.handle(ActorRole.WORKER, hs.request("to_manager", {"kind": kind, "message": "all done: 42"},
                                                            call=f"w-{uuid4()}"))

    def test_a_report_queued_for_the_old_manager_session_goes_to_the_new_one(self):
        service = self.make()
        self.mailbox.sessions[ActorRole.MANAGER] = (NEW_MANAGER, 1)  # restarted before the lane created it
        self.assertEqual(self.report(service)["status"], "queued")
        self.assertTrue(hs.wait_until(lambda: service.outbox_snapshot()[0]["state"] == "delivered"))
        self.assertEqual(self.mailbox.created[0].session_id, NEW_MANAGER)
        self.assertEqual(len(self.mailbox.delivered), 1)
        states = [r["state"] for r in self.records() if r.get("type") == "outbox"]
        self.assertIn("requeued", states)
        self.assertNotIn("rejected", states)
        self.assertEqual(service.report_entries()[0]["requeued"], 1)

    def test_a_report_created_for_the_old_session_is_created_again_for_the_new_one(self):
        service = self.make()
        self.mailbox.statuses = [MailboxStatus.REJECTED]  # TaskMailbox: target_session_changed, nothing submitted
        original = self.mailbox.deliver

        def deliver(message, *, timeout=20):
            if self.mailbox.statuses:
                self.mailbox.statuses.pop(0)
                self.mailbox.sessions[ActorRole.MANAGER] = (NEW_MANAGER, 1)
                self.mailbox.delivered.append(message.message_id)
                return DeliveryReceipt(message.message_id, None, message.target_role, message.session_id, 1,
                                       MailboxStatus.REJECTED, {"reason": "target_session_changed",
                                                                "api_called": False})
            return original(message, timeout=timeout)

        self.mailbox.deliver = deliver
        self.report(service)
        self.assertTrue(hs.wait_until(lambda: service.outbox_snapshot()[0]["state"] == "delivered"))
        self.assertEqual([m.session_id for m in self.mailbox.created], [hs.MANAGER_SESSION, NEW_MANAGER])
        self.assertEqual(len(self.mailbox.delivered), 2)

    def test_the_report_listener_never_sees_a_loss_for_a_requeue(self):
        events = []
        service = self.make()
        self.mailbox.sessions[ActorRole.MANAGER] = (NEW_MANAGER, 1)
        from workbench.backend.flow import OutboundMessage
        outbound = OutboundMessage(self.active.task_id, 1, self.active.run_id, ActorRole.WORKER, ActorRole.MANAGER,
                                   MessageKind.REPORT, {"handoff": "to_manager", "kind": "done", "message": "m"},
                                   in_reply_to_message_id=self.active.task_message_id)
        service.enqueue(outbound, origin="test", listener=lambda event, snap: events.append(event))
        self.assertTrue(hs.wait_until(lambda: "delivered" in events))
        self.assertNotIn("rejected", events)

    def test_a_message_to_the_worker_is_still_dropped(self):
        service = self.make()
        self.mailbox.sessions[ActorRole.WORKER] = (str(uuid4()), 1)
        service.handle(ActorRole.MANAGER, hs.request("to_worker", {"kind": "work", "message": "x"}))
        self.assertTrue(hs.wait_until(lambda: service.outbox_snapshot()[0]["state"] == "rejected"))
        self.assertEqual(service.outbox_snapshot()[0]["reason"], "target_session_changed")
        self.assertEqual(self.mailbox.delivered, [])

    def test_a_submitted_report_is_never_requeued(self):
        service = self.make()
        self.mailbox.statuses = [MailboxStatus.UNKNOWN]
        self.report(service)
        self.assertTrue(hs.wait_until(lambda: service.outbox_snapshot()[0]["state"] == "unknown"))
        self.mailbox.sessions[ActorRole.MANAGER] = (NEW_MANAGER, 1)
        self.peers[ActorRole.MANAGER] = Peer(NEW_MANAGER)
        self.assertEqual(service.requeue_for_new_session(ActorRole.MANAGER), 0)
        self.assertEqual(len(self.mailbox.created), 1, "an unknown delivery is never sent again")
        entry = service.report_entries()[0]
        self.assertEqual((entry["state"], entry["origin"], entry["report_kind"]), ("unknown", "worker", "done"))
        self.assertEqual(entry["text"], "all done: 42")

    def test_requeue_for_new_session_moves_a_deferred_report_and_counts_it(self):
        service = self.make()
        self.mailbox.statuses = [MailboxStatus.DEFERRED] * 1000
        self.report(service)
        self.assertTrue(hs.wait_until(lambda: len(self.mailbox.delivered) >= 2))
        self.mailbox.sessions[ActorRole.MANAGER] = (NEW_MANAGER, 1)
        self.peers[ActorRole.MANAGER] = Peer(NEW_MANAGER)
        self.assertTrue(hs.wait_until(lambda: service.requeue_for_new_session(ActorRole.MANAGER) == 1))
        self.mailbox.statuses.clear()
        self.assertTrue(hs.wait_until(lambda: service.outbox_snapshot()[0]["state"] == "delivered"))
        self.assertEqual(self.mailbox.created[-1].session_id, NEW_MANAGER)
        self.assertEqual(service.requeue_for_new_session(ActorRole.WORKER), 0)

    def test_requeue_is_bounded(self):
        service = self.make()
        sessions = iter(str(uuid4()) for _ in range(100))

        def deliver(message, *, timeout=20):
            self.mailbox.sessions[ActorRole.MANAGER] = (next(sessions), 1)
            self.mailbox.delivered.append(message.message_id)
            return DeliveryReceipt(message.message_id, None, message.target_role, message.session_id, 1,
                                   MailboxStatus.REJECTED, {"reason": "target_session_changed"})

        self.mailbox.deliver = deliver
        self.report(service)
        self.assertTrue(hs.wait_until(lambda: service.outbox_snapshot()[0]["state"] == "rejected"))
        self.assertEqual(len(self.mailbox.delivered), REQUEUE_LIMIT + 1)


class BlockerTests(Fixture):
    def test_an_editor_deferral_is_recorded_and_cleared(self):
        service = self.service()
        self.active = self.active_task()
        gate = threading.Event()
        receipts = [("editor", ["editor_not_empty"]), ("editor", ["busy", "editor_not_empty"]), ("busy", ["busy"])]
        seen = []

        def deliver(message, *, timeout=20):
            if receipts:
                _, blockers = receipts.pop(0)
                result = DeliveryReceipt(message.message_id, None, message.target_role, "s", 1,
                                         MailboxStatus.DEFERRED, {"reason": "current_omp_state_not_safe",
                                                                  "blockers": blockers})
                seen.append(service.report_entries()[0]["editor_since"])
                return result
            gate.wait(5)
            return DeliveryReceipt(message.message_id, None, message.target_role, "s", 1, MailboxStatus.API_RETURNED, {})

        self.mailbox.deliver = deliver
        service.handle(ActorRole.WORKER, hs.request("to_manager", {"kind": "progress", "message": "p"}))
        self.assertTrue(hs.wait_until(lambda: len(seen) == 3))
        self.assertTrue(hs.wait_until(lambda: service.report_entries()[0]["state"] == "delivering"))
        entry = service.report_entries()[0]
        self.assertEqual(entry["blockers"], ["busy"])
        self.assertIsNone(entry["editor_since"], "a deferral for another reason clears the composer wait")
        self.assertIsNone(seen[0])
        self.assertIsNotNone(seen[2], "set at the first editor deferral and kept while it lasts")
        self.assertTrue(service.lane_busy(ActorRole.WORKER), "a message from the worker is on its way")
        gate.set()
        self.assertTrue(hs.wait_until(lambda: service.report_entries()[0]["state"] == "delivered"))
        self.assertFalse(service.lane_busy(ActorRole.WORKER))


class MailboxBlockerTests(unittest.TestCase):
    SAFE = {"idle": True, "pending": False, "approvalPending": False, "editorKnown": True, "editorEmpty": True,
            "inFlightToolCount": 0, "paused": False}

    def test_state_blockers_names_each_condition(self):
        self.assertEqual(state_blockers(self.SAFE), [])
        self.assertEqual(state_blockers({**self.SAFE, "editorEmpty": False}), ["editor_not_empty"])
        self.assertEqual(state_blockers({**self.SAFE, "editorKnown": False, "editorEmpty": False}), ["editor_unknown"])
        self.assertEqual(state_blockers({**self.SAFE, "idle": False, "inFlightToolCount": 2}), ["busy", "tool_running"])
        self.assertEqual(state_blockers({}), ["busy", "pending_messages", "approval_pending", "editor_unknown",
                                              "tool_running", "paused"])

    def test_a_deferred_delivery_says_the_manager_composer_is_not_empty(self):
        session = str(uuid4())

        class Bridge:
            def peer(self, role, timeout=0):
                return Peer(session)

            def probe(self, role, timeout=5):
                return {**MailboxBlockerTests.SAFE, "role": "manager", "sessionId": session, "generation": 1,
                        "editorEmpty": False}

            def delivery_lock(self, role):
                return threading.Lock()

        mailbox = TaskMailbox(None, Bridge())
        message = MailboxMessage(str(uuid4()), str(uuid4()), 1, str(uuid4()), str(uuid4()), ActorRole.WORKER,
                                 ActorRole.MANAGER, session, 1, MessageKind.REPORT, "{}", str(uuid4()))
        mailbox._messages[message.message_id] = message
        receipt = mailbox.deliver(message, timeout=1)
        self.assertEqual(receipt.status, MailboxStatus.DEFERRED)
        self.assertEqual(receipt.details["blockers"], ["editor_not_empty"])


class EventsSinceTests(unittest.TestCase):
    def test_counts_only_new_events_of_the_role_without_waiting(self):
        with tempfile.TemporaryDirectory(prefix="cd70-ev-", dir="/tmp") as directory:
            bridge = G3BridgeServer(Path(directory) / "b.sock", {"manager": "m" * 8, "worker": "w" * 8})
            try:
                for index, (role, name) in enumerate([("worker", "agent_start"), ("manager", "agent_start"),
                                                      ("worker", "provider_request_started"),
                                                      ("worker", "agent_end")], 1):
                    bridge._events.append({"role": role, "name": name, "bridgeSequence": index})
                bridge._event_sequence = 4
                names = ("agent_start", "agent_end")
                self.assertEqual(bridge.events_since(ActorRole.WORKER, names, 0), (4, 2))
                self.assertEqual(bridge.events_since(ActorRole.WORKER, names, 1), (4, 1))
                self.assertEqual(bridge.events_since(ActorRole.WORKER, names, 4), (4, 0))
                self.assertEqual(bridge.events_since(ActorRole.MANAGER, names, 0), (4, 1))
            finally:
                bridge.close()


if __name__ == "__main__":
    unittest.main()
