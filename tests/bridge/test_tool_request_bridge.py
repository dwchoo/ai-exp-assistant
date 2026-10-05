"""CW-18 U1: G3BridgeServer accepts tool_request frames only from authenticated peers.

A fake extension peer speaks the real socket protocol; no OMP, no provider.
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

from workbench.contracts.v1 import ActorRole
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer


class FakePeer:
    def __init__(self, path: Path, role: str, token: str, *, claimed_role: str | None = None):
        self.session_id = str(uuid4())
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.sock.settimeout(3)
        self.buffer = b""
        self.send({"kind": "hello", "protocolVersion": 1, "token": token, "role": claimed_role or role,
                   "ompSessionId": self.session_id, "generation": 1, "pid": 4242})

    def send(self, frame: dict) -> None:
        self.sock.sendall((json.dumps(frame) + "\n").encode())

    def read(self, timeout: float = 3) -> dict | None:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                return None
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def read_kind(self, kind: str, timeout: float = 3) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.read(max(deadline - time.monotonic(), 0.01))
            if frame is None:
                return None
            if frame.get("kind") == kind:
                return frame
        return None

    def tool_request(self, tool: str, args: dict, *, tool_call_id: str = "call-1", **overrides) -> str:
        request_id = str(uuid4())
        frame = {"kind": "tool_request", "requestId": request_id, "toolCallId": tool_call_id, "tool": tool,
                 "args": args, "sessionId": self.session_id, "generation": 1}
        frame.update(overrides)
        self.send(frame)
        return request_id

    def close(self) -> None:
        self.sock.close()


class ToolRequestBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cw18-u1-bridge-")
        self.path = Path(self.tmp.name) / "bridge.sock"
        self.tokens = {"manager": str(uuid4()), "worker": str(uuid4())}
        self.bridge = G3BridgeServer(self.path, self.tokens)
        self.bridge.start()
        self.calls: list[tuple] = []
        self.peers: list[FakePeer] = []

    def tearDown(self):
        for peer in self.peers:
            peer.close()
        self.bridge.close()
        self.tmp.cleanup()

    def connect(self, role: str, **kwargs) -> FakePeer:
        peer = FakePeer(self.path, role, kwargs.pop("token", self.tokens[role]), **kwargs)
        self.peers.append(peer)
        return peer

    def handler(self, peer, request):
        self.calls.append((peer, dict(request), threading.current_thread().name))
        return {"status": "queued", "message_id": "m-1", "seen_role": peer.role.value}

    def test_authenticated_tool_request_reaches_handler_with_peer_identity_and_result_returns(self):
        self.bridge.set_tool_handler(self.handler)
        peer = self.connect("worker")
        self.assertIsNotNone(peer.read_kind("ready"))
        # Frame fields that try to claim another identity are not used.
        request_id = peer.tool_request("to_manager", {"kind": "progress", "message": "half"}, role="manager")
        result = peer.read_kind("tool_result")
        self.assertEqual(result, {"kind": "tool_result", "requestId": request_id, "toolCallId": "call-1",
                                  "result": {"status": "queued", "message_id": "m-1", "seen_role": "worker"}})
        self.assertEqual(len(self.calls), 1)
        bridge_peer, request, _ = self.calls[0]
        self.assertEqual(bridge_peer.role, ActorRole.WORKER)
        self.assertEqual(request, {"request_id": request_id, "tool_call_id": "call-1", "tool": "to_manager",
                                   "args": {"kind": "progress", "message": "half"},
                                   "session_id": peer.session_id, "generation": 1})

    def test_hello_role_comes_from_the_token_not_the_claim(self):
        self.bridge.set_tool_handler(self.handler)
        # The worker token cannot authenticate a manager claim: no session, no handler call.
        impostor = self.connect("worker", claimed_role="manager")
        self.assertIsNone(impostor.read_kind("ready", timeout=0.5))
        bad = self.connect("manager", token=str(uuid4()))
        self.assertIsNone(bad.read_kind("ready", timeout=0.5))
        try:
            impostor.tool_request("to_worker", {"kind": "work", "message": "x"})
        except OSError:
            pass
        time.sleep(0.2)
        self.assertEqual(self.calls, [])

    def test_session_mismatch_is_rejected_without_calling_the_handler(self):
        self.bridge.set_tool_handler(self.handler)
        peer = self.connect("manager")
        self.assertIsNotNone(peer.read_kind("ready"))
        peer.tool_request("to_worker", {"kind": "work", "message": "x"}, sessionId=str(uuid4()))
        self.assertEqual(peer.read_kind("tool_result")["result"],
                         {"status": "rejected", "reason": "session_mismatch"})
        peer.tool_request("to_worker", {"kind": "work", "message": "x"}, tool_call_id="c2", generation=2)
        self.assertEqual(peer.read_kind("tool_result")["result"]["reason"], "session_mismatch")
        self.assertEqual(self.calls, [])

    def test_without_handler_the_request_is_rejected(self):
        peer = self.connect("manager")
        self.assertIsNotNone(peer.read_kind("ready"))
        peer.tool_request("to_worker", {"kind": "work", "message": "x"})
        self.assertEqual(peer.read_kind("tool_result")["result"],
                         {"status": "rejected", "reason": "handoff_unavailable"})

    def test_malformed_identifiers_get_no_result_and_no_handler_call(self):
        self.bridge.set_tool_handler(self.handler)
        peer = self.connect("manager")
        self.assertIsNotNone(peer.read_kind("ready"))
        peer.send({"kind": "tool_request", "requestId": 7, "toolCallId": "c", "tool": "to_worker", "args": {},
                   "sessionId": peer.session_id, "generation": 1})
        peer.tool_request("to_worker", {}, tool_call_id="")
        self.assertIsNone(peer.read_kind("tool_result", timeout=0.4))
        self.assertEqual(self.calls, [])

    def test_handler_failure_answers_outcome_unknown_and_slow_handler_does_not_block_state(self):
        gate = threading.Event()

        def slow(peer, request):
            if request["tool_call_id"] == "boom":
                raise RuntimeError("handler failed")
            gate.wait(3)
            return {"status": "held", "reason": "paused"}

        self.bridge.set_tool_handler(slow)
        peer = self.connect("worker")
        self.assertIsNotNone(peer.read_kind("ready"))
        peer.tool_request("to_manager", {"kind": "done", "message": "x"}, tool_call_id="slow")
        # The connection's reader keeps processing frames while the handler runs.
        peer.send({"kind": "state", "role": "worker", "sessionId": peer.session_id, "generation": 1, "idle": True})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.bridge._peers[ActorRole.WORKER].state is None:
            time.sleep(0.01)
        self.assertIsNotNone(self.bridge._peers[ActorRole.WORKER].state)
        peer.tool_request("to_manager", {"kind": "done", "message": "x"}, tool_call_id="boom")
        failed = peer.read_kind("tool_result")
        self.assertEqual(failed["toolCallId"], "boom")
        self.assertEqual(failed["result"], {"status": "outcome_unknown", "reason": "backend_error"})
        gate.set()
        self.assertEqual(peer.read_kind("tool_result")["result"], {"status": "held", "reason": "paused"})


    # p27-cd68-review-01 P2-1: a result that cannot reach its caller is reported, so it is not "consumed".
    def test_a_result_for_a_replaced_session_is_reported_undelivered(self):
        release, undelivered = threading.Event(), []

        def slow(peer, request):
            release.wait(5)
            return {"status": "exited", "exit_code": 0}

        self.bridge.set_tool_handler(slow, undelivered=lambda peer, request: undelivered.append(
            (peer.session_id, request["tool_call_id"])))
        first = self.connect("worker")
        self.assertIsNotNone(first.read_kind("ready"))
        first.tool_request("terminal", {"command": "make", "timeout_seconds": 30}, tool_call_id="call-wait")
        time.sleep(0.2)
        second = self.connect("worker")  # the bridge reconnected / the worker OMP was respawned
        self.assertIsNotNone(second.read_kind("ready"))
        release.set()
        deadline = time.monotonic() + 3
        while not undelivered and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(undelivered, [(first.session_id, "call-wait")])
        self.assertIsNone(second.read_kind("tool_result", timeout=0.3), "never sent to another session")

    def test_a_delivered_result_is_not_reported_undelivered(self):
        undelivered = []
        self.bridge.set_tool_handler(self.handler, undelivered=lambda peer, request: undelivered.append(1))
        peer = self.connect("worker")
        self.assertIsNotNone(peer.read_kind("ready"))
        peer.tool_request("terminal", {"command": None, "timeout_seconds": 1})
        self.assertIsNotNone(peer.read_kind("tool_result"))
        time.sleep(0.1)
        self.assertEqual(undelivered, [])


    # p27-cd68-fix-03 (review-02 P3 (3)): a closed or replaced session is reported, so its waiters are dropped.
    def test_a_gone_peer_is_reported(self):
        gone = []
        self.bridge.set_tool_handler(self.handler, peer_gone=lambda peer: gone.append(peer.session_id))
        first = self.connect("worker")
        self.assertIsNotNone(first.read_kind("ready"))
        second = self.connect("worker")  # replaces the first
        self.assertIsNotNone(second.read_kind("ready"))
        deadline = time.monotonic() + 3
        while len(gone) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(gone, [first.session_id])
        second.close()
        deadline = time.monotonic() + 3
        while len(gone) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(gone, [first.session_id, second.session_id])


if __name__ == "__main__":
    unittest.main()
