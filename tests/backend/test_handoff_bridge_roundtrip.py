"""CW-18 U1: to_worker/to_manager round trip through the real bridge, HandoffService and TaskMailbox.

Fake extension peers speak the G3 socket protocol (hello, state, api_ack,
omp_event, tool_request); no OMP and no provider.
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

from workbench.backend.flow import ActiveTask, HandoffService
from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageKind
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, TaskMailbox
from workbench.tasks.repository import TaskRepository


class ExtensionPeer:
    """Answers probe/deliver like the bridge extension and records injected messages."""

    def __init__(self, path: Path, role: str, token: str):
        self.role, self.session_id = role, str(uuid4())
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.lock = threading.Lock()
        self.results: dict[str, dict] = {}
        self.injected: list[dict] = []
        self.cond = threading.Condition()
        self.send({"kind": "hello", "protocolVersion": 1, "token": token, "role": role,
                   "ompSessionId": self.session_id, "generation": 1, "pid": 4343})
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def state(self) -> dict:
        return {"kind": "state", "role": self.role, "sessionId": self.session_id, "generation": 1, "idle": True,
                "pending": False, "approvalPending": False, "inFlightToolCount": 0, "editorKnown": True,
                "editorEmpty": True, "paused": False}

    def send(self, frame: dict) -> None:
        with self.lock:
            self.sock.sendall((json.dumps(frame) + "\n").encode())

    def _read(self) -> None:
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

    def _on(self, frame: dict) -> None:
        kind = frame.get("kind")
        if kind == "probe":
            self.send({"kind": "api_ack", "requestId": frame["requestId"], "status": "state", "state": self.state()})
        elif kind == "deliver":
            envelope = ControlEnvelope.from_json(frame["envelope"])
            with self.cond:
                self.injected.append({"kind": envelope.event.message_kind.value,
                                      "payload": envelope.event.payload,
                                      "in_reply_to": envelope.event.in_reply_to_message_id,
                                      "message_id": envelope.message_id})
                self.cond.notify_all()
            self.send({"kind": "api_ack", "requestId": frame["requestId"], "status": "api_accepted",
                       "messageId": envelope.message_id})
            self.send({"kind": "omp_event", "name": "delivery_omp_processed", "sessionId": self.session_id,
                       "generation": 1, "messageId": envelope.message_id,
                       "deliveryAttemptId": envelope.delivery_attempt_id, "taskId": envelope.task_id,
                       "revisionId": envelope.revision_id, "runId": envelope.run_id,
                       "providerRequestMatched": True, "providerResponseObserved": True, "agentEndObserved": True})
        elif kind == "tool_result":
            with self.cond:
                self.results[frame["toolCallId"]] = frame["result"]
                self.cond.notify_all()

    def call(self, tool: str, args: dict, call: str, timeout: float = 10) -> dict:
        self.send({"kind": "tool_request", "requestId": str(uuid4()), "toolCallId": call, "tool": tool,
                   "args": args, "sessionId": self.session_id, "generation": 1})
        deadline = time.monotonic() + timeout
        with self.cond:
            while call not in self.results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(call)
                self.cond.wait(remaining)
            return self.results[call]

    def wait_injected(self, count: int, timeout: float = 10) -> list[dict]:
        deadline = time.monotonic() + timeout
        with self.cond:
            while len(self.injected) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("injected")
                self.cond.wait(remaining)
            return list(self.injected)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class HandoffBridgeRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cw18-u1-rt-")
        root = Path(self.tmp.name)
        tokens = {"manager": str(uuid4()), "worker": str(uuid4())}
        self.bridge = G3BridgeServer(root / "bridge.sock", tokens)
        self.bridge.start()
        self.repository = TaskRepository(root / "tasks.sqlite3")
        self.mailbox = TaskMailbox(self.repository, self.bridge)
        self.active: ActiveTask | None = None
        def outbox_mailbox():  # the outbox thread's own connection (SQLite is thread-bound)
            repository = TaskRepository(root / "tasks.sqlite3")
            return TaskMailbox(repository, self.bridge), repository.close

        self.handoffs = HandoffService(root / "handoffs.jsonl", mailbox_factory=outbox_mailbox,
                                       active_task=lambda: self.active,
                                       peer_lookup=lambda role: self.bridge.peer(role, 0), retry_interval=0.05)
        self.handoffs.start()
        self.bridge.set_tool_handler(lambda peer, request: self.handoffs.handle(peer.role, request))
        self.manager = ExtensionPeer(root / "bridge.sock", "manager", tokens["manager"])
        self.worker = ExtensionPeer(root / "bridge.sock", "worker", tokens["worker"])
        self.bridge.peer("manager", timeout=3)
        self.bridge.peer("worker", timeout=3)
        for peer in (self.manager, self.worker):
            peer.send(peer.state())

    def tearDown(self):
        self.manager.close()
        self.worker.close()
        self.bridge.set_tool_handler(None)
        self.bridge.close()
        self.handoffs.close()
        self.repository.close()
        self.tmp.cleanup()

    def start_task(self) -> ActiveTask:
        task_id = self.repository.create_task({"goal": "round trip", "allowed_changes": []})
        self.repository.approve_scope(task_id, 1, {"paths": [], "commands": []})
        self.repository.proceed(task_id, 1, "round trip")
        run_id = self.repository.start_run(task_id, 1)
        task_message = self.mailbox.create_message(task_id, 1, run_id, "manager", "worker", MessageKind.TASK,
                                                   {"note": "first task message"})
        return ActiveTask(task_id, 1, "work", True, run_id, task_message.message_id)

    def test_tool_calls_round_trip_through_the_backend(self):
        # No Task: the manager's instruction becomes an approval request; the worker cannot report.
        pending = self.manager.call("to_worker", {"kind": "work", "message": "plan the change"}, "m-1")
        self.assertEqual(pending["status"], "approval_pending")
        self.assertEqual(self.worker.call("to_manager", {"kind": "done", "message": "x"}, "w-1"),
                         {"status": "rejected", "reason": "no_active_task"})
        # A worker cannot use the manager's tool (role from its token).
        self.assertEqual(self.worker.call("to_worker", {"kind": "work", "message": "x"}, "w-2"),
                         {"status": "rejected", "reason": "tool_not_allowed_for_role"})
        self.assertEqual(self.worker.injected, [])

        self.active = self.start_task()
        queued = self.manager.call("to_worker", {"kind": "work", "message": "check the parser"}, "m-2")
        self.assertEqual(queued["status"], "queued")
        injected = self.worker.wait_injected(1)[0]
        self.assertEqual((injected["kind"], injected["payload"]),
                         ("question", {"handoff": "to_worker", "kind": "work", "message": "check the parser"}))
        report = self.worker.call("to_manager", {"kind": "report", "message": "parser ok",
                                                 "requires_code_change": False}, "w-3")
        self.assertEqual(report["status"], "queued")
        delivered = self.manager.wait_injected(1)[0]
        self.assertEqual(delivered["kind"], "report")
        self.assertEqual(delivered["in_reply_to"], self.active.task_message_id)
        self.assertEqual(delivered["payload"]["message"], "parser ok")
        # A repeated tool call id returns the first result and sends nothing again.
        self.assertEqual(self.manager.call("to_worker", {"kind": "work", "message": "again"}, "m-2"), queued)
        time.sleep(0.3)
        self.assertEqual(len(self.worker.injected), 1)
        self.assertTrue(all(entry["state"] == "delivered" for entry in self.handoffs.outbox_snapshot()))


if __name__ == "__main__":
    unittest.main()
