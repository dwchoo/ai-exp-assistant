from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import (
    BridgePeer,
    BridgeTimeout,
    MailboxError,
    MailboxStatus,
    TaskMailbox,
)
from workbench.tasks.repository import TaskRepository


class FakeBridge:
    def __init__(self):
        self.peers = {
            ActorRole.MANAGER: BridgePeer(ActorRole.MANAGER, str(uuid4()), 1, 101),
            ActorRole.WORKER: BridgePeer(ActorRole.WORKER, str(uuid4()), 1, 102),
        }
        self.states = {role: self._ready_state(peer) for role, peer in self.peers.items()}
        self.requests = []
        self.events = []
        self.ack = {"status": "api_accepted", "requestId": str(uuid4())}
        self.request_error = None
        self.event_error = None
        self.emit_processing = False

    @staticmethod
    def _ready_state(peer):
        return {
            "role": peer.role.value,
            "sessionId": peer.session_id,
            "generation": peer.generation,
            "idle": True,
            "pending": False,
            "approvalPending": False,
            "editorKnown": True,
            "editorEmpty": True,
            "inFlightToolCount": 0,
            "paused": False,
        }

    def peer(self, role, timeout=0):
        return self.peers[role if isinstance(role, ActorRole) else ActorRole(role)]

    def probe(self, role, timeout=5):
        role = role if isinstance(role, ActorRole) else ActorRole(role)
        return dict(self.states[role])

    def event_cursor(self):
        return len(self.events)

    def request(self, role, frame, timeout=5):
        self.requests.append((role, dict(frame)))
        if self.request_error:
            raise self.request_error
        if self.emit_processing and frame.get("kind") == "deliver":
            from workbench.contracts.v1 import ControlEnvelope

            envelope = ControlEnvelope.from_json(frame["envelope"])
            peer = self.peer(role)
            self.events.append({
                "role": peer.role.value,
                "name": "delivery_omp_processed",
                "messageId": envelope.message_id,
                "deliveryAttemptId": envelope.delivery_attempt_id,
                "taskId": envelope.task_id,
                "revisionId": envelope.revision_id,
                "runId": envelope.run_id,
                "sessionId": peer.session_id,
                "generation": peer.generation,
                "providerRequestMatched": True,
                "providerResponseObserved": True,
                "agentEndObserved": True,
            })
        return dict(self.ack)

    def wait_any_event(self, role, names, expected, *, after_sequence=0, timeout=15):
        if self.event_error:
            raise self.event_error
        for event in self.events[after_sequence:]:
            if event.get("role") == ActorRole(role).value and event.get("name") in names:
                if all(event.get(key) == value for key, value in expected.items()):
                    return dict(event)
        raise BridgeTimeout("event not configured")


class TaskMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = TaskRepository(Path(self.temp_dir.name) / "metadata.sqlite3")
        self.bridge = FakeBridge()
        self.task_id = self.repository.create_task({"goal": "mailbox test", "allowed_changes": []})
        self.repository.approve_scope(self.task_id, 1, {"paths": [], "commands": []})
        self.repository.proceed(self.task_id, 1, "run mailbox test")
        self.run_id = self.repository.start_run(self.task_id, 1)
        self.mailbox = TaskMailbox(self.repository, self.bridge)

    def tearDown(self):
        self.repository.close()
        self.temp_dir.cleanup()

    def create(self, kind=MessageKind.TASK, *, sender="manager", target="worker", reply_to=None):
        return self.mailbox.create_message(
            self.task_id, 1, self.run_id, sender, target, kind,
            {"note": f"{kind.value} payload"}, in_reply_to_message_id=reply_to,
        )

    def test_all_kinds_use_correct_roles_and_durable_task_run_links(self):
        task = self.create(MessageKind.TASK)
        question = self.create(MessageKind.QUESTION)
        answer = self.create(MessageKind.ANSWER, sender="worker", target="manager", reply_to=question.message_id)
        report = self.create(MessageKind.REPORT, sender="worker", target="manager", reply_to=task.message_id)

        self.assertEqual(
            [(message.kind, message.sender_role, message.target_role) for message in (task, question, answer, report)],
            [
                (MessageKind.TASK, ActorRole.MANAGER, ActorRole.WORKER),
                (MessageKind.QUESTION, ActorRole.MANAGER, ActorRole.WORKER),
                (MessageKind.ANSWER, ActorRole.WORKER, ActorRole.MANAGER),
                (MessageKind.REPORT, ActorRole.WORKER, ActorRole.MANAGER),
            ],
        )
        for message in (task, question, answer, report):
            stored = self.repository.get_message(message.message_id)
            self.assertEqual((stored["task_id"], stored["revision"], stored["run_id"]),
                             (self.task_id, 1, self.run_id))
            self.assertEqual(stored["content"]["target_session_generation"], message.session_generation)

    def test_rejects_wrong_direction_and_reply_parent(self):
        with self.assertRaises(MailboxError):
            self.create(MessageKind.TASK, sender="worker", target="manager")
        with self.assertRaises(MailboxError):
            self.create(MessageKind.ANSWER, sender="worker", target="manager")
        task = self.create()
        with self.assertRaises(MailboxError):
            self.create(MessageKind.ANSWER, sender="worker", target="manager", reply_to=task.message_id)

    def test_readiness_gates_are_fail_closed_before_attempt_or_injection(self):
        fields = {
            "pending": True,
            "approvalPending": True,
            "editorEmpty": False,
            "idle": False,
            "editorKnown": False,
            "inFlightToolCount": 1,
            "paused": True,
        }
        for field, value in fields.items():
            with self.subTest(field=field):
                self.bridge.requests.clear()
                self.bridge.events.clear()
                self.bridge.states[ActorRole.WORKER] = FakeBridge._ready_state(self.bridge.peer("worker"))
                self.bridge.states[ActorRole.WORKER][field] = value
                message = self.create()
                receipt = self.mailbox.deliver(message)
                self.assertEqual(receipt.status, MailboxStatus.DEFERRED)
                self.assertFalse(self.bridge.requests)
                self.assertIsNone(receipt.delivery_attempt_id)

    def test_session_generation_change_rejects_without_replay(self):
        message = self.create()
        old = self.bridge.peer("worker")
        self.bridge.peers[ActorRole.WORKER] = BridgePeer(ActorRole.WORKER, old.session_id, old.generation + 1, old.pid)
        receipt = self.mailbox.deliver(message)
        self.assertEqual(receipt.status, MailboxStatus.REJECTED)
        self.assertEqual(receipt.details["reason"], "target_session_changed")
        self.assertFalse(self.bridge.requests)

    def test_api_return_is_not_processed_and_matching_event_records_separate_status(self):
        message = self.create()
        self.bridge.emit_processing = True
        receipt = self.mailbox.deliver(message)
        self.assertEqual(receipt.status, MailboxStatus.OMP_PROCESSED)
        self.assertFalse(receipt.details["task_completed"])
        history = self.repository.get_delivery_history(receipt.delivery_attempt_id)
        self.assertEqual([event["status"] for event in history], ["attempted", "api_returned", "omp_processed"])
        self.assertFalse(history[1]["details"]["model_processed"])

    def test_api_return_without_processing_observation_is_unknown(self):
        message = self.create()
        receipt = self.mailbox.deliver(message)
        self.assertEqual(receipt.status, MailboxStatus.UNKNOWN)
        self.assertFalse(receipt.details["task_completed"])
        self.assertEqual(
            [event["status"] for event in self.repository.get_delivery_history(receipt.delivery_attempt_id)],
            ["attempted", "api_returned", "unknown"],
        )

    def test_async_processing_error_is_persisted_unknown_and_not_replayed(self):
        message = self.create()
        request = self.bridge.requests
        self.bridge.ack = {"status": "api_accepted", "requestId": str(uuid4())}
        self.bridge.events.append({
            "role": "worker", "name": "delivery_processing_unknown",
            "messageId": message.message_id,
            "deliveryAttemptId": "not-matching-yet",
        })
        # Supply the exact identity after the bridge request has decoded its new attempt ID.
        original_request = self.bridge.request

        def request_with_async_error(role, frame, timeout=5):
            ack = original_request(role, frame, timeout)
            from workbench.contracts.v1 import ControlEnvelope
            envelope = ControlEnvelope.from_json(frame["envelope"])
            self.bridge.events.append({
                "role": "worker", "name": "delivery_processing_unknown",
                "messageId": message.message_id,
                "deliveryAttemptId": envelope.delivery_attempt_id,
                "taskId": message.task_id,
                "revisionId": message.revision_id,
                "runId": message.run_id,
                "sessionId": message.session_id,
                "generation": message.session_generation,
                "providerRequestMatched": True,
                "providerResponseObserved": False,
                "agentEndObserved": True,
                "reason": "assistant_message_ended_with_error_or_abort",
            })
            return ack

        self.bridge.request = request_with_async_error
        receipt = self.mailbox.deliver(message)
        self.assertEqual(receipt.status, MailboxStatus.UNKNOWN)
        self.assertEqual(receipt.details["reason"], "assistant_message_ended_with_error_or_abort")
        self.assertFalse(receipt.details["automatic_replay"])
        self.assertEqual(self.mailbox.deliver(message), receipt)
        self.assertEqual(len(self.bridge.requests), 1)
        history = self.repository.get_delivery_history(receipt.delivery_attempt_id)
        self.assertEqual([event["status"] for event in history], ["attempted", "api_returned", "unknown"])

    def test_lost_ack_is_unknown_and_never_replayed(self):
        message = self.create()
        self.bridge.request_error = BridgeTimeout("ack lost")
        first = self.mailbox.deliver(message)
        second = self.mailbox.deliver(message)
        self.assertEqual(first.status, MailboxStatus.UNKNOWN)
        self.assertEqual(first, second)
        self.assertEqual(len(self.bridge.requests), 1)
        self.assertEqual(self.repository.get_delivery_history(first.delivery_attempt_id)[-1]["status"], "unknown")

    def test_new_mailbox_cannot_deliver_prior_process_handle(self):
        message = self.create()
        reopened = TaskMailbox(self.repository, self.bridge)
        with self.assertRaisesRegex(MailboxError, "replay after reopen is forbidden"):
            reopened.deliver(message)
        self.assertFalse(self.bridge.requests)

    def test_successful_message_handle_is_deduplicated_on_live_mailbox(self):
        message = self.create()
        self.bridge.emit_processing = True
        first = self.mailbox.deliver(message)
        second = self.mailbox.deliver(message)
        self.assertEqual(first.status, MailboxStatus.OMP_PROCESSED)
        self.assertEqual(second, first)
        self.assertEqual(len(self.bridge.requests), 1)
        self.assertEqual(len(self.repository.get_delivery_history(first.delivery_attempt_id)), 3)


if __name__ == "__main__":
    unittest.main()
