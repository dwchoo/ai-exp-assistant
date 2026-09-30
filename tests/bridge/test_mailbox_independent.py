"""Independent CW-08 mailbox boundary and durability regressions."""
from __future__ import annotations

from pathlib import Path
import json
import socket
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageKind
from workbench.ipc.bridge_g3.mailbox import (BridgeBoundMismatch, BridgeDisconnected, BridgePeer, BridgeTimeout,
                                             G3BridgeServer, MailboxError, MailboxStatus, TaskMailbox)
from workbench.observation.worker_review import ReviewAdmissionFence
from workbench.tasks.repository import TaskRepository


class BoundaryBridge:
    def __init__(self):
        self.peers = {role: BridgePeer(role, str(uuid4()), 1, 100 + index)
                      for index, role in enumerate((ActorRole.MANAGER, ActorRole.WORKER))}
        self.states = {role: self.ready(peer) for role, peer in self.peers.items()}
        self.requests = []
        self.ack_mode = "api_accepted"
        self.processing_name = "delivery_omp_processed"
        self.processing_flags = (True, True, True)

    @staticmethod
    def ready(peer):
        return {"role": peer.role.value, "sessionId": peer.session_id, "generation": peer.generation,
                "idle": True, "pending": False, "approvalPending": False,
                "editorKnown": True, "editorEmpty": True, "inFlightToolCount": 0, "paused": False}

    def peer(self, role, timeout=0):
        return self.peers[ActorRole(role)]

    def probe(self, role, timeout=5):
        return dict(self.states[ActorRole(role)])

    def event_cursor(self):
        return len(self.requests)

    def request(self, role, frame, timeout=5):
        self.requests.append((ActorRole(role), dict(frame)))
        if self.ack_mode == "lost":
            raise BridgeTimeout("ACK lost after send")
        return {"status": self.ack_mode, "requestId": str(uuid4()), "reason": "state_changed"}

    def wait_any_event(self, role, names, expected, *, after_sequence=0, timeout=15):
        assert len(self.requests) > after_sequence
        return {"role": ActorRole(role).value, "name": self.processing_name, **expected,
                "providerRequestMatched": self.processing_flags[0],
                "providerResponseObserved": self.processing_flags[1],
                "agentEndObserved": self.processing_flags[2],
                "reason": "provider_failed"}


class MailboxIndependentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cw08-independent-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "metadata.sqlite3"
        self.repo = TaskRepository(self.path)
        self.addCleanup(lambda: self.repo.close())
        self.bridge = BoundaryBridge()
        self.mailbox = TaskMailbox(self.repo, self.bridge)
        self.task = self.repo.create_task({"goal": "bounded two-role task"})
        self.repo.approve_scope(self.task, 1, {"paths": []})
        self.repo.proceed(self.task, 1, "start")
        self.run = self.repo.start_run(self.task, 1)

    def create(self, kind=MessageKind.TASK, *, reply=None):
        sender, target = (("manager", "worker") if kind in (MessageKind.TASK, MessageKind.QUESTION)
                          else ("worker", "manager"))
        return self.mailbox.create_message(self.task, 1, self.run, sender, target, kind,
                                           {"instruction": "bounded"}, in_reply_to_message_id=reply)

    def test_missing_or_wrong_typed_readiness_never_creates_attempt_then_recovery_uses_same_message(self):
        unsafe = (("idle", 1), ("pending", None), ("approvalPending", "false"),
                  ("editorKnown", None), ("editorEmpty", ""),
                  ("inFlightToolCount", False), ("paused", 0),
                  ("role", "manager"), ("sessionId", str(uuid4())),
                  ("generation", 2), ("generation", True))
        for key, value in unsafe:
            with self.subTest(key=key):
                message = self.create()
                self.bridge.states[ActorRole.WORKER] = self.bridge.ready(self.bridge.peer("worker"))
                self.bridge.states[ActorRole.WORKER][key] = value
                request_count = len(self.bridge.requests)
                receipt = self.mailbox.deliver(message)
                self.assertEqual(receipt.status, MailboxStatus.DEFERRED)
                self.assertIsNone(receipt.delivery_attempt_id)
                self.assertEqual(len(self.bridge.requests), request_count)
                self.bridge.states[ActorRole.WORKER] = self.bridge.ready(self.bridge.peer("worker"))
                recovered = self.mailbox.deliver(message)
                self.assertEqual(recovered.status, MailboxStatus.OMP_PROCESSED)
                self.assertEqual(len(self.bridge.requests), request_count + 1)
                self.assertEqual([e["status"] for e in self.repo.get_delivery_history(recovered.delivery_attempt_id)],
                                 ["attempted", "api_returned", "omp_processed"])

    def test_current_session_change_and_extension_last_moment_deferral_do_not_claim_delivery(self):
        old = self.bridge.peer("worker")
        for switched in (BridgePeer(old.role, str(uuid4()), old.generation, old.pid),
                         BridgePeer(old.role, old.session_id, old.generation + 1, old.pid)):
            with self.subTest(switched=switched):
                stale = self.create()
                self.bridge.peers[ActorRole.WORKER] = switched
                self.bridge.states[ActorRole.WORKER] = self.bridge.ready(switched)
                rejected = self.mailbox.deliver(stale)
                self.assertEqual(rejected.status, MailboxStatus.REJECTED)
                self.assertIsNone(rejected.delivery_attempt_id)
                self.assertFalse(self.bridge.requests)
                self.bridge.peers[ActorRole.WORKER] = old
                self.bridge.states[ActorRole.WORKER] = self.bridge.ready(old)
                self.assertEqual(self.mailbox.deliver(stale), rejected)
                self.assertFalse(self.bridge.requests)

        message = self.create()
        self.bridge.ack_mode = "deferred"  # extension rechecked after mailbox probe
        receipt = self.mailbox.deliver(message)
        self.assertEqual(receipt.status, MailboxStatus.DEFERRED)
        self.assertEqual(len(self.bridge.requests), 1)
        self.assertEqual([e["status"] for e in self.repo.get_delivery_history(receipt.delivery_attempt_id)],
                         ["attempted", "failed"])
        self.bridge.ack_mode = "api_accepted"
        recovered = self.mailbox.deliver(message)
        self.assertEqual(recovered.status, MailboxStatus.OMP_PROCESSED)
        self.assertNotEqual(recovered.delivery_attempt_id, receipt.delivery_attempt_id)

    def test_api_acceptance_or_incomplete_provider_evidence_never_means_work_completed(self):
        for flags in ((False, True, True), (True, False, True), (True, True, False)):
            with self.subTest(flags=flags):
                self.bridge.processing_flags = flags
                message = self.create()
                receipt = self.mailbox.deliver(message)
                self.assertEqual(receipt.status, MailboxStatus.UNKNOWN)
                self.assertFalse(receipt.details["task_completed"] if "task_completed" in receipt.details else False)
                history = self.repo.get_delivery_history(receipt.delivery_attempt_id)
                self.assertEqual([e["status"] for e in history], ["attempted", "api_returned", "unknown"])
                self.assertFalse(history[1]["details"]["model_processed"])
                self.assertEqual(self.mailbox.deliver(message), receipt)
        self.bridge.processing_name = "delivery_processing_unknown"
        self.bridge.processing_flags = (True, False, True)
        asynchronous = self.create()
        unknown = self.mailbox.deliver(asynchronous)
        self.assertEqual(unknown.status, MailboxStatus.UNKNOWN)
        self.assertEqual(unknown.details["reason"], "provider_failed")
        self.assertEqual([e["status"] for e in self.repo.get_delivery_history(unknown.delivery_attempt_id)],
                         ["attempted", "api_returned", "unknown"])
        self.assertEqual(self.mailbox.deliver(asynchronous), unknown)
        self.bridge.processing_name = "delivery_omp_processed"
        self.bridge.processing_flags = (True, True, True)
        message = self.create()
        receipt = self.mailbox.deliver(message)
        self.assertEqual(receipt.status, MailboxStatus.OMP_PROCESSED)
        self.assertFalse(receipt.details["task_completed"])

    def test_lost_ack_unknown_is_durable_and_never_replayed_by_new_mailbox_or_generation(self):
        message = self.create()
        self.bridge.ack_mode = "lost"
        receipt = self.mailbox.deliver(message)
        self.assertEqual(receipt.status, MailboxStatus.UNKNOWN)
        self.assertEqual(len(self.bridge.requests), 1)
        self.assertEqual(self.mailbox.deliver(message), receipt)
        self.repo.close()
        self.repo = TaskRepository(self.path)
        self.assertEqual(self.repo.get_delivery_history(receipt.delivery_attempt_id)[-1]["status"], "unknown")
        peer = self.bridge.peer("worker")
        self.bridge.peers[ActorRole.WORKER] = BridgePeer(peer.role, str(uuid4()), 2, peer.pid + 1)
        fresh = TaskMailbox(self.repo, self.bridge)
        with self.assertRaises(MailboxError):
            fresh.deliver(message)
        self.assertEqual(len(self.bridge.requests), 1)


class BridgeServerIndependentRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cw08-independent-socket-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bridge.sock"
        self.tokens = {"manager": str(uuid4()), "worker": str(uuid4())}
        self.server = G3BridgeServer(self.path, self.tokens)
        self.server.start()
        self.addCleanup(self.server.close)

    def connect(self, *, generation=1, role="worker"):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1)
        self.addCleanup(client.close)
        client.connect(str(self.path))
        session = str(uuid4())
        self.write(client, {"kind": "hello", "protocolVersion": 1, "role": role,
                            "ompSessionId": session, "generation": generation,
                            "pid": 12345, "token": self.tokens[role]})
        return client, session

    @staticmethod
    def write(client, frame):
        client.sendall((json.dumps(frame) + "\n").encode())

    @staticmethod
    def line(client):
        buffer = bytearray()
        while not buffer.endswith(b"\n"):
            buffer.extend(client.recv(4096))
        return json.loads(buffer)

    def test_backpressured_request_has_deadline_and_close_can_progress(self):
        client, _ = self.connect()
        self.assertEqual(self.line(client)["kind"], "ready")
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        start = time.monotonic()
        with self.assertRaises(BridgeTimeout):
            self.server.request("worker", {"kind": "deliver", "payload": "x" * 4_000_000}, timeout=.12)
        self.assertLess(time.monotonic() - start, .55, "backpressured send exceeded its deadline")
        self.assertEqual(len(self.server._pending_acks), 0)
        outcome = []
        def request():
            try:
                self.server.request("worker", {"kind": "deliver", "payload": "x" * 4_000_000}, timeout=1.2)
            except (BridgeTimeout, BridgeDisconnected):
                outcome.append("held")
            else:
                outcome.append("unexpected_ack")
        sender = Thread(target=request, daemon=True)
        sender.start()
        time.sleep(.08)
        self.assertTrue(sender.is_alive(), "non-reading peer did not backpressure a large request")
        closer = Thread(target=self.server.close, daemon=True)
        closer.start()
        closer.join(timeout=.6)
        self.assertFalse(closer.is_alive(), "close was blocked by a backpressured send")
        sender.join(timeout=1.5)
        self.assertFalse(sender.is_alive(), "request ignored its bounded deadline after close")
        self.assertEqual(outcome, ["held"])
        self.assertFalse(self.path.exists())

    def test_bound_peer_switch_while_write_lock_waits_sends_no_replacement_bytes(self):
        old_client, old_session = self.connect(role="worker")
        self.assertEqual(self.line(old_client)["kind"], "ready")
        live = self.server._peers[ActorRole.WORKER]
        live.write_lock.acquire()
        selected = Event()
        original_live = self.server._live_peer
        results = []

        def selected_live(role, public):
            peer = original_live(role, public)
            selected.set()
            return peer

        def send():
            try:
                self.server.request("worker", {"kind": "pause"}, timeout=.8,
                                    expected_peer=(old_session, 1))
            except BridgeBoundMismatch:
                results.append("rejected")

        with patch.object(self.server, "_live_peer", side_effect=selected_live):
            thread = Thread(target=send)
            thread.start()
            self.assertTrue(selected.wait(.2))
            replacement, _ = self.connect(role="worker")
            self.assertEqual(self.line(replacement)["kind"], "ready")
            live.write_lock.release()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["rejected"])
        self.assertEqual(self.server._pending_acks, {})
        replacement.settimeout(.1)
        with self.assertRaises(socket.timeout):
            replacement.recv(1)

    def test_newer_fence_close_rejects_resume_waiting_on_write_lock(self):
        client, session = self.connect(role="worker")
        self.assertEqual(self.line(client)["kind"], "ready")
        fence = ReviewAdmissionFence()
        bound, _ = fence.bind(None, 0, fence.token())
        opened = fence.open(bound)
        self.assertIsNotNone(opened)
        live = self.server._peers[ActorRole.WORKER]
        live.write_lock.acquire()
        selected = Event()
        original_live = self.server._live_peer
        results = []

        def selected_live(role, public):
            peer = original_live(role, public)
            selected.set()
            return peer

        def resume():
            try:
                self.server.request("worker", {"kind": "resume", "reconciled": True},
                                    timeout=.8, expected_peer=(session, 1),
                                    authority_token=opened)
            except BridgeBoundMismatch:
                results.append("stale")

        with patch.object(self.server, "_live_peer", side_effect=selected_live):
            thread = Thread(target=resume)
            thread.start()
            self.assertTrue(selected.wait(.2))
            fence.close("newer_pause")
            live.write_lock.release()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["stale"])
        self.assertEqual(self.server._pending_acks, {})
        client.settimeout(.1)
        with self.assertRaises(socket.timeout):
            client.recv(1)

    def test_bound_peer_pair_rejects_manager_switch_before_worker_submission(self):
        worker, worker_session = self.connect(role="worker")
        manager, manager_session = self.connect(role="manager")
        self.assertEqual(self.line(worker)["kind"], "ready")
        self.assertEqual(self.line(manager)["kind"], "ready")
        live = self.server._peers[ActorRole.WORKER]
        live.write_lock.acquire()
        selected = Event()
        original_live = self.server._live_peer
        results = []

        def selected_live(role, public):
            peer = original_live(role, public)
            selected.set()
            return peer

        def send():
            try:
                self.server.request("worker", {"kind": "deliver"}, timeout=.8,
                                    expected_peers={
                                        ActorRole.MANAGER: (manager_session, 1),
                                        ActorRole.WORKER: (worker_session, 1),
                                    })
            except BridgeBoundMismatch:
                results.append("pair_changed")

        with patch.object(self.server, "_live_peer", side_effect=selected_live):
            thread = Thread(target=send)
            thread.start()
            self.assertTrue(selected.wait(.2))
            replacement, _ = self.connect(role="manager")
            self.assertEqual(self.line(replacement)["kind"], "ready")
            live.write_lock.release()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["pair_changed"])
        self.assertEqual(self.server._pending_acks, {})
        worker.settimeout(.1)
        with self.assertRaises(socket.timeout):
            worker.recv(1)

    def test_switch_after_pending_registration_never_reselects_replacement(self):
        old_client, old_session = self.connect(role="worker")
        self.assertEqual(self.line(old_client)["kind"], "ready")
        replacements = []
        sends = []

        def switch_before_first_send(connection, payload, deadline):
            sends.append(True)
            self.assertEqual(len(self.server._pending_acks), 1)
            replacement, _ = self.connect(role="worker")
            self.assertEqual(self.line(replacement)["kind"], "ready")
            replacements.append(replacement)
            raise OSError("old selected peer disconnected")

        with patch.object(self.server, "_send_until", side_effect=switch_before_first_send):
            with self.assertRaises(BridgeDisconnected):
                self.server.request("worker", {"kind": "pause"}, timeout=.5,
                                    expected_peer=(old_session, 1))
        self.assertEqual(sends, [True], "request was retried or reselected")
        self.assertEqual(self.server._pending_acks, {})
        replacements[0].settimeout(.1)
        with self.assertRaises(socket.timeout):
            replacements[0].recv(1)

    def test_boolean_generation_cannot_register_update_state_or_emit_event(self):
        bad, _ = self.connect(generation=True)
        self.assertEqual(bad.recv(1), b"")
        with self.assertRaises(BridgeDisconnected):
            self.server.peer("worker", timeout=.05)
        client, session = self.connect()
        self.assertEqual(self.line(client)["kind"], "ready")
        peer = self.server.peer("worker", timeout=.2)
        cursor = self.server.event_cursor()
        self.write(client, {"kind": "state", "role": "worker", "sessionId": session,
                            "generation": True, "idle": True})
        self.write(client, {"kind": "omp_event", "name": "identity_check",
                            "sessionId": session, "generation": True})
        with self.assertRaises(BridgeTimeout):
            self.server.wait_event("worker", "identity_check", {}, after_sequence=cursor, timeout=.05)
        self.assertIsNone(self.server._peers[ActorRole.WORKER].state,
                          "malformed state frame replaced the current peer state")
        self.write(client, {"kind": "state", "role": "worker", "sessionId": session,
                            "generation": 1, "idle": True})
        self.write(client, {"kind": "omp_event", "name": "identity_check",
                            "sessionId": session, "generation": 1})
        accepted = self.server.wait_event("worker", "identity_check", {},
                                          after_sequence=cursor, timeout=.3)
        self.assertEqual(accepted["generation"], peer.generation)
        self.assertEqual(self.server._peers[ActorRole.WORKER].state["generation"], 1)

    def test_unsolicited_and_late_acks_are_not_retained_or_reused(self):
        client, session = self.connect()
        self.assertEqual(self.line(client)["kind"], "ready")
        cursor = self.server.event_cursor()
        for _ in range(128):
            self.write(client, {"kind": "api_ack", "requestId": str(uuid4()),
                                "status": "api_accepted"})
        self.write(client, {"kind": "omp_event", "name": "ack_batch_complete",
                            "sessionId": session, "generation": 1})
        self.server.wait_event("worker", "ack_batch_complete", {},
                               after_sequence=cursor, timeout=.5)
        self.assertEqual(len(self.server._pending_acks), 0)

        outcome = []
        def timeout_request():
            try:
                self.server.request("worker", {"kind": "probe"}, timeout=.08)
            except BridgeTimeout:
                outcome.append("timed_out")
        sender = Thread(target=timeout_request, daemon=True)
        sender.start()
        request = self.line(client)
        sender.join(timeout=.5)
        self.assertFalse(sender.is_alive())
        self.assertEqual(outcome, ["timed_out"])
        self.write(client, {"kind": "api_ack", "requestId": request["requestId"],
                            "status": "api_accepted"})
        cursor = self.server.event_cursor()
        self.write(client, {"kind": "omp_event", "name": "late_ack_complete",
                            "sessionId": session, "generation": 1})
        self.server.wait_event("worker", "late_ack_complete", {}, after_sequence=cursor, timeout=.5)
        self.assertEqual(len(self.server._pending_acks), 0)
        with self.assertRaises(BridgeTimeout):
            self.server.request("worker", {"kind": "probe"}, timeout=.08)

    def test_close_after_ack_pending_before_first_send_converges_to_durable_unknown(self):
        client, _ = self.connect()
        self.assertEqual(self.line(client)["kind"], "ready")
        repository = TaskRepository(Path(self.directory.name) / "metadata.sqlite3")
        self.addCleanup(repository.close)
        task = repository.create_task({"goal": "close race"})
        repository.approve_scope(task, 1, {"paths": []})
        repository.proceed(task, 1, "bounded")
        run = repository.start_run(task, 1)
        mailbox = TaskMailbox(repository, self.server)
        peer = self.server.peer("worker")
        message = mailbox.create_message(task, 1, run, "manager", "worker", MessageKind.TASK,
                                         {"instruction": "must not replay"})
        first_send_reached = []
        original_send = self.server._send_until

        def close_before_first_select(connection, payload, deadline):
            self.assertEqual(len(self.server._pending_acks), 1,
                             "the request was not yet registered as pending")
            self.assertEqual(first_send_reached, [], "send hook unexpectedly repeated")
            first_send_reached.append(True)
            self.server.close()
            return original_send(connection, payload, deadline)

        start = time.monotonic()
        with patch.object(self.server, "probe", return_value=BoundaryBridge.ready(peer)):
            with patch.object(self.server, "_send_until", side_effect=close_before_first_select):
                receipt = mailbox.deliver(message, timeout=.5)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(first_send_reached, [True])
        self.assertEqual(receipt.status, MailboxStatus.UNKNOWN)
        self.assertEqual(receipt.details["automatic_replay"], False)
        self.assertEqual([event["status"] for event in repository.get_delivery_history(
            receipt.delivery_attempt_id)], ["attempted", "unknown"])
        self.assertEqual(mailbox.deliver(message), receipt)
        self.assertEqual(self.server._pending_acks, {})
        self.assertFalse(self.path.exists())
        self.assertEqual(client.recv(1), b"", "request bytes were sent after close")

if __name__ == "__main__":
    unittest.main()
