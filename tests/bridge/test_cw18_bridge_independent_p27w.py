"""CW-18 independent verification (p27-cw18-test-01): the tools over the real G3 bridge socket.

Real ``G3BridgeServer`` + real ``HandoffService`` + real ``TaskFlow`` + real ``TaskMailbox``/``TaskRepository``.
Fake extension peers speak the socket protocol (hello, state, probe answers, api_ack, omp_event,
tool_request); no OMP, no provider, no network.

Expectations (criteria, drafted before reading the implementation):
- the role comes from the hello token; a role/actor field in a tool_request frame is ignored;
- a work Task is dispatched immediately; the worker's ``to_manager`` done reaches the manager as a report
  and frees the worker; while busy, ``to_worker`` answers ``worker_busy`` and nothing reaches the worker;
- a repeated tool call is answered with the first result and delivers nothing again;
- one delivery path per target: a second message never enters the worker while the first turn is
  still being processed (no two deliveries into one OMP turn);
- a handoff queued for one worker session is never delivered into a replacement session.
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from workbench.backend.flow import HandoffService
from workbench.backend.flow_tasks import TaskFlow
from workbench.contracts.v1 import ActorRole, ControlEnvelope
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, TaskMailbox
from workbench.tasks.repository import TaskRepository


class Peer:
    """A fake bridge extension; ``turn`` seconds pass between a deliver frame and its processed event."""

    def __init__(self, path: Path, role: str, token: str, *, turn: float = 0.0, claimed_role: str | None = None):
        self.role, self.session_id, self.turn = role, str(uuid4()), turn
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.write_lock = threading.Lock()
        self.cond = threading.Condition()
        self.results: dict[str, dict] = {}
        self.injected: list[dict] = []
        self.timeline: list[tuple[str, float, str]] = []  # (event, time, message id)
        self.in_turn = 0
        self.max_in_turn = 0
        self.send({"kind": "hello", "protocolVersion": 1, "token": token, "role": claimed_role or role,
                   "ompSessionId": self.session_id, "generation": 1, "pid": 4747})
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def state(self):
        return {"kind": "state", "role": self.role, "sessionId": self.session_id, "generation": 1,
                "idle": self.in_turn == 0, "pending": False, "approvalPending": False, "inFlightToolCount": 0,
                "editorKnown": True, "editorEmpty": True, "paused": False}

    def send(self, frame):
        with self.write_lock:
            try:
                self.sock.sendall((json.dumps(frame) + "\n").encode())
            except OSError:
                pass  # this peer was closed (replaced session)

    def _read(self):
        buffer = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self._on(json.loads(line))

    def _on(self, frame):
        kind = frame.get("kind")
        if kind == "probe":
            self.send({"kind": "api_ack", "requestId": frame["requestId"], "status": "state", "state": self.state()})
        elif kind == "deliver":
            envelope = ControlEnvelope.from_json(frame["envelope"])
            with self.cond:
                self.in_turn += 1
                self.max_in_turn = max(self.max_in_turn, self.in_turn)
                self.timeline.append(("deliver", time.monotonic(), envelope.message_id))
                self.injected.append({"kind": envelope.event.message_kind.value, "payload": envelope.event.payload,
                                      "in_reply_to": envelope.event.in_reply_to_message_id,
                                      "message_id": envelope.message_id, "task_id": envelope.task_id})
                self.cond.notify_all()
            self.send({"kind": "api_ack", "requestId": frame["requestId"], "status": "api_accepted",
                       "messageId": envelope.message_id})
            threading.Thread(target=self._finish_turn, args=(envelope,), daemon=True).start()
        elif kind == "tool_result":
            with self.cond:
                self.results[frame["toolCallId"]] = frame["result"]
                self.cond.notify_all()

    def _finish_turn(self, envelope):
        time.sleep(self.turn)
        with self.cond:
            self.in_turn -= 1
            self.timeline.append(("processed", time.monotonic(), envelope.message_id))
        self.send({"kind": "omp_event", "name": "delivery_omp_processed", "sessionId": self.session_id,
                   "generation": 1, "messageId": envelope.message_id,
                   "deliveryAttemptId": envelope.delivery_attempt_id, "taskId": envelope.task_id,
                   "revisionId": envelope.revision_id, "runId": envelope.run_id, "providerRequestMatched": True,
                   "providerResponseObserved": True, "agentEndObserved": True})
        self.send(self.state())

    def call(self, tool, args, call_id, timeout=10, **extra):
        frame = {"kind": "tool_request", "requestId": str(uuid4()), "toolCallId": call_id, "tool": tool,
                 "args": args, "sessionId": self.session_id, "generation": 1}
        frame.update(extra)
        self.send(frame)
        deadline = time.monotonic() + timeout
        with self.cond:
            while call_id not in self.results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(call_id)
                self.cond.wait(remaining)
            return self.results.pop(call_id)

    def wait_injected(self, count, timeout=10):
        deadline = time.monotonic() + timeout
        with self.cond:
            while len(self.injected) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{self.role}: {len(self.injected)} < {count}")
                self.cond.wait(remaining)
            return list(self.injected)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def eventually(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class BridgeFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p27w-bridge-", dir="/tmp")
        self.root = Path(self.tmp.name)
        self.socket = self.root / "bridge.sock"
        self.tokens = {"manager": str(uuid4()), "worker": str(uuid4())}
        self.bridge = G3BridgeServer(self.socket, self.tokens)
        self.bridge.start()
        self.db = self.root / "tasks.sqlite3"
        TaskRepository(self.db).close()
        self.peers: list[Peer] = []

        def lane_mailbox():
            repository = TaskRepository(self.db)
            return TaskMailbox(repository, self.bridge), repository.close

        self.handoffs = HandoffService(self.root / "handoffs.jsonl", mailbox_factory=lane_mailbox,
                                       peer_lookup=self.lookup, retry_interval=0.05)
        self.flow = TaskFlow(self.root / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(self.db),
                             handoffs=self.handoffs, omp_idle=self.idle, poll_interval=0.05)
        self.handoffs.configure(policy=self.flow, active_task=self.flow.active_task)
        self.handoffs.start()
        self.flow.start()
        self.bridge.set_tool_handler(lambda peer, request: self.handoffs.handle(peer.role, request))

    def lookup(self, role):
        try:
            return self.bridge.peer(role, 0)
        except Exception:
            return None

    def idle(self, role):
        try:
            return self.bridge.probe(role, timeout=1.0).get("idle") is True
        except Exception:
            return None

    def tearDown(self):
        for peer in self.peers:
            peer.close()
        self.bridge.set_tool_handler(None)
        self.flow.close()
        self.bridge.close()
        self.handoffs.close()
        self.tmp.cleanup()

    def connect(self, role, **kwargs):
        peer = Peer(self.socket, role, kwargs.pop("token", self.tokens[role]), **kwargs)
        self.peers.append(peer)
        return peer

    def both(self, worker_turn=0.0):
        manager, worker = self.connect("manager"), self.connect("worker", turn=worker_turn)
        self.bridge.peer("manager", timeout=3)
        self.bridge.peer("worker", timeout=3)
        for peer in (manager, worker):
            peer.send(peer.state())
        return manager, worker

    def test_free_work_round_trip_and_worker_busy_over_the_socket(self):
        manager, worker = self.both()
        work = {"kind": "work", "message": "write notes.txt", "spec": {"goal": "notes", "paths": ["notes/"]}}
        first = manager.call("to_worker", work, "m-1")
        self.assertEqual(first["status"], "dispatched", first)
        task_message = worker.wait_injected(1)[0]
        self.assertEqual((task_message["kind"], task_message["task_id"]), ("task", first["task_id"]))
        self.assertEqual(task_message["payload"]["paths"], ["notes/"])
        self.assertTrue(eventually(lambda: self.flow.task_view()["status"] == "running"))
        busy = manager.call("to_worker", {"kind": "work", "message": "other", "spec": {"goal": "o", "paths": ["o/"]}},
                            "m-2")
        self.assertEqual((busy["status"], busy["task"]["task_id"]), ("worker_busy", first["task_id"]), busy)
        # a duplicate of the first call: the first result, nothing delivered again
        self.assertEqual(manager.call("to_worker", work, "m-1"), first)
        done = worker.call("to_manager", {"kind": "done", "message": "notes written", "task_id": first["task_id"]},
                           "w-1")
        self.assertEqual(done["status"], "queued", done)
        report = manager.wait_injected(1)[0]
        self.assertEqual((report["kind"], report["in_reply_to"]), ("report", task_message["message_id"]))
        self.assertEqual(report["payload"]["kind"], "done")
        self.assertTrue(eventually(lambda: self.flow.task_view()["status"] == "closed"))
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        time.sleep(0.2)
        self.assertEqual(len(worker.injected), 1, "worker_busy and the duplicate delivered nothing")
        nxt = manager.call("to_worker", {"kind": "work", "message": "next", "spec": {"goal": "n", "paths": ["n/"]}},
                           "m-3")
        self.assertEqual(nxt["status"], "dispatched")

    def test_frame_role_claims_are_ignored_and_impostor_hello_is_refused(self):
        manager, worker = self.both()
        spoof = worker.call("to_worker", {"kind": "work", "message": "x", "spec": {"goal": "g", "paths": ["a/"]}},
                            "w-spoof", role="manager", senderRole="manager", actor="manager")
        self.assertEqual(spoof, {"status": "rejected", "reason": "tool_not_allowed_for_role"})
        spoof = manager.call("to_manager", {"kind": "done", "message": "x"}, "m-spoof", role="worker")
        self.assertEqual(spoof, {"status": "rejected", "reason": "tool_not_allowed_for_role"})
        self.assertIsNone(self.flow.task_view())
        # an authenticated manager cannot act for another session
        result = manager.call("to_worker", {"kind": "work", "message": "x", "spec": {"goal": "g", "paths": ["a/"]}},
                              "m-other", sessionId=worker.session_id)
        self.assertEqual(result["reason"], "session_mismatch")
        self.assertIsNone(self.flow.task_view())

    def test_no_two_deliveries_enter_the_worker_in_one_turn(self):
        manager, worker = self.both(worker_turn=0.6)
        first = manager.call("to_worker", {"kind": "work", "message": "first", "spec": {"goal": "g", "paths": ["a/"]}},
                             "m-1")
        worker.wait_injected(1)
        self.assertTrue(eventually(lambda: self.flow.task_view()["run_id"] is not None
                                   and self.flow.task_view()["status"] == "running"))
        # A follow-up through the handoff lane and a direct TaskMailbox delivery (the workflow's path) at once.
        follow = manager.call("to_worker", {"kind": "work", "message": "follow-up", "task_id": first["task_id"]}, "m-2")
        self.assertEqual(follow["status"], "queued", follow)
        view = self.flow.task_view()
        repository = TaskRepository(self.db)
        try:
            mailbox = TaskMailbox(repository, self.bridge)
            direct = mailbox.create_message(first["task_id"], view["revision"], view["run_id"], "manager", "worker",
                                            "question", {"stage": "direct"})
            receipt = mailbox.deliver(direct, timeout=10)
        finally:
            repository.close()
        self.assertIn(receipt.status.value, ("omp_processed", "api_returned", "deferred"), receipt)
        self.assertTrue(eventually(lambda: len(worker.injected) >= 2 + (receipt.status.value != "deferred"), 10))
        time.sleep(0.8)
        self.assertEqual(worker.max_in_turn, 1, f"overlapping deliveries: {worker.timeline}")

    def test_a_handoff_queued_for_one_session_is_not_delivered_into_a_new_session(self):
        manager, worker = self.both(worker_turn=0.8)
        first = manager.call("to_worker", {"kind": "work", "message": "first", "spec": {"goal": "g", "paths": ["a/"]}},
                             "m-1")
        worker.wait_injected(1)
        self.assertTrue(eventually(lambda: self.flow.task_view()["status"] == "running"))
        follow = manager.call("to_worker", {"kind": "work", "message": "follow-up", "task_id": first["task_id"]}, "m-2")
        self.assertEqual(follow["status"], "queued")
        worker.close()  # the worker OMP is replaced while the follow-up waits behind the first turn
        replacement = self.connect("worker")
        self.assertTrue(eventually(lambda: self.lookup("worker") is not None
                                   and self.lookup("worker").session_id == replacement.session_id))
        replacement.send(replacement.state())
        time.sleep(1.5)
        self.assertEqual([m for m in replacement.injected if m["payload"].get("message") == "follow-up"], [],
                         "a handoff for the old session reached the new one")


if __name__ == "__main__":
    unittest.main()
