"""CW-18 R1: ``TaskMailbox.deliver(on_submitted=...)`` is told the api ack before the turn's outcome."""

from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageKind
from workbench.ipc.bridge_g3.mailbox import BridgePeer, BridgeTimeout, MailboxStatus, TaskMailbox
from workbench.tasks.repository import TaskRepository


class AckBridge:
    """``ack`` is the extension's answer; the processed event appears when ``turn_end`` is set."""

    def __init__(self, ack="api_accepted"):
        self.ack = ack
        self.peers = {ActorRole.MANAGER: BridgePeer(ActorRole.MANAGER, str(uuid4()), 1, 101),
                      ActorRole.WORKER: BridgePeer(ActorRole.WORKER, str(uuid4()), 1, 102)}
        self.cv = threading.Condition()
        self.events = []
        self.turn_end = threading.Event()
        self.requests = 0

    def peer(self, role, timeout=0):
        return self.peers[ActorRole(role)]

    def probe(self, role, timeout=5):
        peer = self.peers[ActorRole(role)]
        return {"role": peer.role.value, "sessionId": peer.session_id, "generation": 1, "idle": True,
                "pending": False, "approvalPending": False, "editorKnown": True, "editorEmpty": True,
                "inFlightToolCount": 0, "paused": False}

    def event_cursor(self):
        with self.cv:
            return len(self.events)

    def request(self, role, frame, timeout=5):
        self.requests += 1
        role = ActorRole(role)
        envelope = ControlEnvelope.from_json(frame["envelope"])
        peer = self.peers[role]

        def finish():
            if self.turn_end.wait(10):
                with self.cv:
                    self.events.append({"role": role.value, "name": "delivery_omp_processed",
                                        "messageId": envelope.message_id,
                                        "deliveryAttemptId": envelope.delivery_attempt_id,
                                        "taskId": envelope.task_id, "revisionId": envelope.revision_id,
                                        "runId": envelope.run_id, "sessionId": peer.session_id, "generation": 1,
                                        "providerRequestMatched": True, "providerResponseObserved": True,
                                        "agentEndObserved": True})
                    self.cv.notify_all()

        if self.ack == "api_accepted":
            threading.Thread(target=finish, daemon=True).start()
        return {"status": self.ack, "requestId": frame.get("requestId", "r")}

    def wait_any_event(self, role, names, expected, *, after_sequence=0, timeout=15):
        deadline = time.monotonic() + timeout
        with self.cv:
            while True:
                for event in self.events[after_sequence:]:
                    if event["name"] in names and event.get("messageId") == expected.get("messageId"):
                        return dict(event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeTimeout("no processing event")
                self.cv.wait(remaining)


class SubmittedHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cw18-r1-submitted-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.repository = TaskRepository(Path(self.tmp.name) / "tasks.sqlite3")
        self.addCleanup(self.repository.close)
        self.task_id = self.repository.create_task({"goal": "submitted"})
        self.repository.approve_scope(self.task_id, 1, {"paths": []})
        self.repository.proceed(self.task_id, 1, "go")
        self.run_id = self.repository.start_run(self.task_id, 1)

    def deliver(self, bridge, timeout):
        mailbox = TaskMailbox(self.repository, bridge)
        message = mailbox.create_message(self.task_id, 1, self.run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                         MessageKind.TASK, {"n": 1})
        calls = []
        receipt = mailbox.deliver(message, timeout=timeout,
                                  on_submitted=lambda: calls.append(bridge.turn_end.is_set()))
        return receipt, calls

    def test_on_submitted_is_called_at_the_ack_before_the_turn_ends(self):
        bridge = AckBridge()
        threading.Timer(0.3, bridge.turn_end.set).start()
        receipt, calls = self.deliver(bridge, 5)
        self.assertEqual(receipt.status, MailboxStatus.OMP_PROCESSED)
        self.assertEqual(calls, [False], "told once, while the turn still ran")

    def test_a_turn_longer_than_the_timeout_is_unknown_but_was_submitted(self):
        bridge = AckBridge()
        self.addCleanup(bridge.turn_end.set)
        receipt, calls = self.deliver(bridge, 0.3)
        self.assertEqual(receipt.status, MailboxStatus.UNKNOWN)
        self.assertEqual(calls, [False])

    def test_no_submission_means_no_call(self):
        for ack, status in (("deferred", MailboxStatus.DEFERRED), ("unknown_no_replay", MailboxStatus.UNKNOWN),
                            ("rejected", MailboxStatus.REJECTED)):
            with self.subTest(ack=ack):
                receipt, calls = self.deliver(AckBridge(ack), 1)
                self.assertEqual((receipt.status, calls), (status, []))

    def test_a_failing_callback_never_changes_the_receipt(self):
        bridge = AckBridge()
        bridge.turn_end.set()
        mailbox = TaskMailbox(self.repository, bridge)
        message = mailbox.create_message(self.task_id, 1, self.run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                         MessageKind.TASK, {"n": 1})

        def boom():
            raise RuntimeError("caller bookkeeping failed")

        self.assertEqual(mailbox.deliver(message, timeout=5, on_submitted=boom).status, MailboxStatus.OMP_PROCESSED)


if __name__ == "__main__":
    unittest.main()
