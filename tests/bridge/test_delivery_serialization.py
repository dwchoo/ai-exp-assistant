"""CW-18 U2: one serialized delivery path per target OMP across every TaskMailbox on a bridge."""

from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageKind
from workbench.ipc.bridge_g3.mailbox import BridgePeer, G3BridgeServer, MailboxStatus, TaskMailbox
from workbench.tasks.repository import TaskRepository


class SlowBridge:
    """A bridge whose deliver frame takes ``hold`` seconds; records overlap per target."""

    def __init__(self, hold=0.15):
        self.hold = hold
        self.peers = {ActorRole.MANAGER: BridgePeer(ActorRole.MANAGER, str(uuid4()), 1, 101),
                      ActorRole.WORKER: BridgePeer(ActorRole.WORKER, str(uuid4()), 1, 102)}
        self.events = []
        self.lock = threading.Lock()
        self.in_flight = {ActorRole.MANAGER: 0, ActorRole.WORKER: 0}
        self.max_in_flight = {ActorRole.MANAGER: 0, ActorRole.WORKER: 0}
        self.max_total = 0
        self.order = []

    def peer(self, role, timeout=0):
        return self.peers[ActorRole(role)]

    def probe(self, role, timeout=5):
        peer = self.peers[ActorRole(role)]
        return {"role": peer.role.value, "sessionId": peer.session_id, "generation": 1, "idle": True,
                "pending": False, "approvalPending": False, "editorKnown": True, "editorEmpty": True,
                "inFlightToolCount": 0, "paused": False}

    def event_cursor(self):
        with self.lock:
            return len(self.events)

    def request(self, role, frame, timeout=5):
        role = ActorRole(role)
        envelope = ControlEnvelope.from_json(frame["envelope"])
        with self.lock:
            self.in_flight[role] += 1
            self.max_in_flight[role] = max(self.max_in_flight[role], self.in_flight[role])
            self.max_total = max(self.max_total, sum(self.in_flight.values()))
            self.order.append(("start", role.value, envelope.message_id))
        time.sleep(self.hold)  # the OMP turn
        peer = self.peers[role]
        with self.lock:
            self.in_flight[role] -= 1
            self.order.append(("end", role.value, envelope.message_id))
            self.events.append({"role": role.value, "name": "delivery_omp_processed",
                                "messageId": envelope.message_id, "deliveryAttemptId": envelope.delivery_attempt_id,
                                "taskId": envelope.task_id, "revisionId": envelope.revision_id,
                                "runId": envelope.run_id, "sessionId": peer.session_id, "generation": 1,
                                "providerRequestMatched": True, "providerResponseObserved": True,
                                "agentEndObserved": True})
        return {"status": "api_accepted", "requestId": frame.get("requestId", "r")}

    def wait_any_event(self, role, names, expected, *, after_sequence=0, timeout=15):
        with self.lock:
            for event in self.events[after_sequence:]:
                if event["role"] == ActorRole(role).value and event["name"] in names and all(
                        event.get(key) == value for key, value in expected.items()):
                    return dict(event)
        raise AssertionError("processing event missing")


class DeliverySerializationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cw18-u2-delivery-")
        self.db = Path(self.tmp.name) / "tasks.sqlite3"
        with TaskRepository(self.db) as repository:
            self.task_id = repository.create_task({"goal": "serialization"})
            repository.approve_scope(self.task_id, 1, {"paths": []})
            repository.proceed(self.task_id, 1, "go")
            self.run_id = repository.start_run(self.task_id, 1)

    def tearDown(self):
        self.tmp.cleanup()

    def deliver_from_own_mailbox(self, bridge, sender, target, kind, count, results):
        """One component (its own thread, repository connection and TaskMailbox), like a lane or the runner."""
        with TaskRepository(self.db) as repository:
            mailbox = TaskMailbox(repository, bridge)
            for _ in range(count):
                message = mailbox.create_message(self.task_id, 1, self.run_id, sender, target, kind, {"n": 1})
                results.append(mailbox.deliver(message, timeout=5).status)

    def run_components(self, bridge, specs):
        results: list[MailboxStatus] = []
        threads = [threading.Thread(target=self.deliver_from_own_mailbox, args=(bridge, *spec, results))
                   for spec in specs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        return results

    def test_two_mailboxes_never_deliver_into_the_same_omp_at_once(self):
        bridge = SlowBridge()
        results = self.run_components(bridge, [
            ("manager", "worker", MessageKind.TASK, 2),  # workflow stage deliveries
            ("manager", "worker", MessageKind.QUESTION, 2),  # handoff outbox lane
        ])
        self.assertEqual(results, [MailboxStatus.OMP_PROCESSED] * 4)
        self.assertEqual(bridge.max_in_flight[ActorRole.WORKER], 1, bridge.order)
        # Strictly alternating start/end: each turn ends before the next message enters the OMP.
        kinds = [entry[0] for entry in bridge.order]
        self.assertEqual(kinds, ["start", "end"] * 4)

    def test_different_targets_are_independent_lanes(self):
        bridge = SlowBridge(hold=0.3)
        with TaskRepository(self.db) as repository:
            task = TaskMailbox(repository, bridge).create_message(
                self.task_id, 1, self.run_id, "manager", "worker", MessageKind.TASK, {})
        results: list[MailboxStatus] = []

        def to_manager():
            with TaskRepository(self.db) as repository:
                mailbox = TaskMailbox(repository, bridge)
                report = mailbox.create_message(self.task_id, 1, self.run_id, "worker", "manager",
                                                MessageKind.REPORT, {}, in_reply_to_message_id=task.message_id)
                results.append(mailbox.deliver(report, timeout=5).status)

        threads = [threading.Thread(target=self.deliver_from_own_mailbox,
                                    args=(bridge, "manager", "worker", MessageKind.TASK, 1, results)),
                   threading.Thread(target=to_manager)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(results, [MailboxStatus.OMP_PROCESSED] * 2)
        self.assertEqual(bridge.max_total, 2, "manager and worker deliveries may overlap")
        self.assertLess(time.monotonic() - started, 0.55)

    def test_g3_bridge_exposes_one_delivery_lock_per_target(self):
        socket = Path(self.tmp.name) / "b.sock"
        bridge = G3BridgeServer(socket, {"manager": "m-token", "worker": "w-token"})
        try:
            self.assertIs(bridge.delivery_lock("worker"), bridge.delivery_lock(ActorRole.WORKER))
            self.assertIsNot(bridge.delivery_lock("worker"), bridge.delivery_lock("manager"))
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
