"""Durable Task mailbox over the public G3 Unix-socket bridge.

The mailbox owns no recovery loop: it sends only an explicit in-process message
handle, and a new mailbox instance never reloads or replays stored messages.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
import hmac
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import stat
from threading import Condition, Lock, RLock, Thread
import time
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4, uuid5

from workbench.contracts.v1 import (
    ActorRole,
    ControlEnvelope,
    MessageEvent,
    MessageKind,
    new_identifier,
)
from workbench.ipc.bridge_g3.protocol import DeliveryLedger, DeliveryState
from workbench.tasks.repository import TaskRepository


MAX_FRAME_BYTES = 1_048_576
MAX_RETAINED_EVENTS = 4096
MAX_TOOL_IDENTIFIER = 256
TOOL_RESULT_WRITE_TIMEOUT = 5.0


class MailboxError(RuntimeError):
    """Raised when a mailbox operation cannot safely proceed."""


class BridgeTimeout(MailboxError):
    """Raised when the public G3 bridge does not answer within its deadline."""


class _ToolResultUndelivered(Exception):
    """A tool result did not go out to the session that asked for it."""


class BridgeDisconnected(MailboxError):
    """Raised when the bound OMP session disconnects during an operation."""


class BridgeBoundMismatch(BridgeDisconnected):
    """A bound request failed before any bytes were submitted to OMP."""


class MailboxStatus(StrEnum):
    DEFERRED = "deferred"
    REJECTED = "rejected"
    API_RETURNED = "api_returned"
    OMP_PROCESSED = "omp_processed"
    UNKNOWN = "unknown"


def _role(value: ActorRole | str) -> ActorRole:
    try:
        role = value if isinstance(value, ActorRole) else ActorRole(value)
    except ValueError as exc:
        raise MailboxError("role must be manager or worker") from exc
    if role not in {ActorRole.MANAGER, ActorRole.WORKER}:
        raise MailboxError("role must be manager or worker")
    return role


def _uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise MailboxError(f"{field} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise MailboxError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise MailboxError(f"{field} must be a canonical UUID")
    return value


def _direction(sender: ActorRole, target: ActorRole, kind: MessageKind) -> bool:
    if sender is ActorRole.MANAGER and target is ActorRole.WORKER:
        return kind in {MessageKind.TASK, MessageKind.QUESTION}
    if sender is ActorRole.WORKER and target is ActorRole.MANAGER:
        return kind in {MessageKind.ANSWER, MessageKind.REPORT}
    return False


@dataclass(frozen=True, slots=True)
class BridgePeer:
    role: ActorRole
    session_id: str
    generation: int
    pid: int


# CW-18 handoff tools: (authenticated peer, normalized request) -> result object.
# The request's role/session/generation come from the peer's hello, never the frame.
ToolHandler = Callable[[BridgePeer, dict[str, Any]], Mapping[str, Any]]


@dataclass(slots=True)
class _LivePeer:
    public: BridgePeer
    connection: socket.socket
    write_lock: Lock
    state: dict[str, Any] | None = None


class _G3UnixServer(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, socket_path: str, mailbox_bridge: G3BridgeServer):
        self.mailbox_bridge = mailbox_bridge
        super().__init__(socket_path, _G3RequestHandler)


class _G3RequestHandler(socketserver.StreamRequestHandler):
    server: _G3UnixServer

    def _read_frame(self) -> dict[str, Any] | None:
        raw = self.rfile.readline(MAX_FRAME_BYTES + 1)
        if not raw or len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
            return None
        try:
            frame = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return frame if isinstance(frame, dict) else None

    def handle(self) -> None:
        frame = self._read_frame()
        if frame is None:
            return
        peer = self.server.mailbox_bridge._register_peer(self.request, frame)
        if peer is None:
            return
        try:
            self.wfile.write((json.dumps({
                "kind": "ready",
                "sessionId": peer.public.session_id,
                "generation": peer.public.generation,
            }) + "\n").encode())
            self.wfile.flush()
            while True:
                frame = self._read_frame()
                if frame is None:
                    return
                self.server.mailbox_bridge._receive(peer, frame)
        except (BrokenPipeError, ConnectionError, OSError):
            return
        finally:
            self.server.mailbox_bridge._unregister_peer(peer)


class G3BridgeServer:
    """Authenticated manager/worker transport for the existing G3 extension."""

    def __init__(self, socket_path: str | Path, tokens: Mapping[str, str]):
        self.socket_path = Path(socket_path)
        normalized: dict[ActorRole, str] = {}
        for key, token in tokens.items():
            role = _role(key)
            if not isinstance(token, str) or not token:
                raise ValueError("each bridge role requires a non-empty token")
            normalized[role] = token
        if set(normalized) != {ActorRole.MANAGER, ActorRole.WORKER}:
            raise ValueError("bridge tokens are required for manager and worker")
        self._tokens = normalized
        self._condition = Condition(RLock())
        self._peers: dict[ActorRole, _LivePeer] = {}
        self._pending_acks: dict[str, tuple[_LivePeer, dict[str, Any] | None]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_RETAINED_EVENTS)
        self._event_sequence = 0
        self._server: _G3UnixServer | None = None
        self._thread: Thread | None = None
        self._closed = False
        self._tool_handler: ToolHandler | None = None
        self._tool_undelivered: Callable[[BridgePeer, dict[str, Any]], None] | None = None
        self._peer_gone: Callable[[BridgePeer], None] | None = None
        # CW-18: one serialized delivery path per target OMP. Every TaskMailbox on this
        # bridge (workflow stage deliveries and handoff outbox lanes) delivers under it.
        self._delivery_locks = {role: Lock() for role in (ActorRole.MANAGER, ActorRole.WORKER)}

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            raise FileExistsError(f"refusing to replace existing bridge socket path: {self.socket_path}")
        self._server = _G3UnixServer(str(self.socket_path), self)
        created_socket = self.socket_path.lstat()
        self._socket_identity = (created_socket.st_dev, created_socket.st_ino)
        os.chmod(self.socket_path, 0o600)

    def start(self) -> None:
        if self._closed:
            raise MailboxError("bridge server is closed")
        if self._thread is not None:
            return
        assert self._server is not None
        self._thread = Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            peers = list(self._peers.values())
            self._peers.clear()
            self._pending_acks.clear()
            self._condition.notify_all()
        for peer in peers:
            try:
                peer.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                peer.connection.close()
            except OSError:
                pass
        if self._server is not None:
            if self._thread is not None:
                self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if (stat.S_ISSOCK(current.st_mode)
                and (current.st_dev, current.st_ino) == self._socket_identity):
            self.socket_path.unlink()

    def __enter__(self) -> G3BridgeServer:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _register_peer(self, connection: socket.socket, hello: dict[str, Any]) -> _LivePeer | None:
        if hello.get("kind") != "hello" or hello.get("protocolVersion") != 1:
            return None
        try:
            role = _role(hello.get("role"))
            session_id = _uuid(hello.get("ompSessionId"), "sessionId")
        except MailboxError:
            return None
        generation, pid, token = hello.get("generation"), hello.get("pid"), hello.get("token")
        if type(generation) is not int or generation < 1 or type(pid) is not int or pid < 1:
            return None
        if not isinstance(token, str) or not hmac.compare_digest(token, self._tokens[role]):
            return None
        peer = _LivePeer(
            public=BridgePeer(role, session_id, generation, pid),
            connection=connection,
            write_lock=Lock(),
        )
        old: _LivePeer | None
        with self._condition:
            if self._closed:
                return None
            old = self._peers.get(role)
            self._peers[role] = peer
            self._condition.notify_all()
        if old is not None and old is not peer:
            try:
                old.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                old.connection.close()
            except OSError:
                pass
        return peer

    def delivery_lock(self, role: ActorRole | str) -> Lock:
        """The lock every delivery to ``role``'s OMP holds from its state probe to its outcome."""
        return self._delivery_locks[_role(role)]

    def set_tool_handler(self, handler: ToolHandler | None, *,
                         undelivered: Callable[[BridgePeer, dict[str, Any]], None] | None = None,
                         peer_gone: Callable[[BridgePeer], None] | None = None) -> None:
        """Route ``tool_request`` frames (CW-18 ``to_worker``/``to_manager``) to ``handler``.

        ``undelivered(peer, request)`` (p27-cd68-fix-01) is told when the
        handler's result could not be written to the session that asked (the
        session was replaced, the socket closed or the write timed out): the
        caller never got it. ``peer_gone(peer)`` (p27-cd68-fix-03) is told when
        a session's connection ended (closed or replaced by a new hello).
        """
        with self._condition:
            self._tool_handler = handler
            self._tool_undelivered = undelivered
            self._peer_gone = peer_gone

    def _accept_tool_request(self, peer: _LivePeer, frame: dict[str, Any]) -> None:
        """Called under the condition lock; the handler runs on its own thread."""
        request_id, tool_call_id = frame.get("requestId"), frame.get("toolCallId")
        if (not isinstance(request_id, str) or not 0 < len(request_id) <= MAX_TOOL_IDENTIFIER
                or not isinstance(tool_call_id, str) or not 0 < len(tool_call_id) <= MAX_TOOL_IDENTIFIER):
            return  # nothing to answer to; the extension reports outcome_unknown
        handler = self._tool_handler
        result: dict[str, Any] | None = None
        if (frame.get("sessionId") != peer.public.session_id or type(frame.get("generation")) is not int
                or frame.get("generation") != peer.public.generation):
            result = {"status": "rejected", "reason": "session_mismatch"}
        elif handler is None:
            result = {"status": "rejected", "reason": "handoff_unavailable"}
        request = {
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "tool": frame.get("tool"),
            "args": frame.get("args"),
            "session_id": peer.public.session_id,
            "generation": peer.public.generation,
        }
        Thread(target=self._run_tool_request, args=(peer, request, handler, result, self._tool_undelivered),
               name="g3-tool-request", daemon=True).start()

    def _run_tool_request(self, peer: _LivePeer, request: dict[str, Any], handler: ToolHandler | None,
                          result: dict[str, Any] | None,
                          undelivered: Callable[[BridgePeer, dict[str, Any]], None] | None = None) -> None:
        handled = result is None and handler is not None
        sent = False
        try:
            self._answer_tool_request(peer, request, handler, result)
            sent = True
        except _ToolResultUndelivered:
            pass
        if handled and not sent and undelivered is not None:
            try:
                undelivered(peer.public, request)
            except Exception:
                pass  # the hook never changes the bridge

    def _answer_tool_request(self, peer: _LivePeer, request: dict[str, Any], handler: ToolHandler | None,
                             result: dict[str, Any] | None) -> None:
        """Run the handler and write its result; raises ``_ToolResultUndelivered`` when it did not go out."""
        if result is None and handler is not None:
            try:
                result = dict(handler(peer.public, request))
            except Exception:
                # The handler may have acted before failing: the outcome is not known.
                result = {"status": "outcome_unknown", "reason": "backend_error"}
        try:
            encoded = (json.dumps({"kind": "tool_result", "requestId": request["request_id"],
                                   "toolCallId": request["tool_call_id"], "result": result},
                                  separators=(",", ":"), allow_nan=False) + "\n").encode()
        except (TypeError, ValueError):
            encoded = (json.dumps({"kind": "tool_result", "requestId": request["request_id"],
                                   "toolCallId": request["tool_call_id"],
                                   "result": {"status": "outcome_unknown", "reason": "backend_error"}},
                                  separators=(",", ":")) + "\n").encode()
        deadline = time.monotonic() + TOOL_RESULT_WRITE_TIMEOUT
        if not peer.write_lock.acquire(timeout=TOOL_RESULT_WRITE_TIMEOUT):
            raise _ToolResultUndelivered
        try:
            with self._condition:
                if self._closed or self._peers.get(peer.public.role) is not peer:
                    raise _ToolResultUndelivered  # a replaced session never receives another session's result
            self._send_until(peer.connection, encoded, deadline)
        except (OSError, ValueError, MailboxError) as exc:
            # The extension times out to outcome_unknown and never resends.
            raise _ToolResultUndelivered from exc
        finally:
            peer.write_lock.release()

    def _unregister_peer(self, peer: _LivePeer) -> None:
        with self._condition:
            if self._peers.get(peer.public.role) is peer:
                self._peers.pop(peer.public.role, None)
            for request_id, (pending_peer, _) in tuple(self._pending_acks.items()):
                if pending_peer is peer:
                    self._pending_acks.pop(request_id, None)
            self._condition.notify_all()
            gone = self._peer_gone
        if gone is not None:
            try:
                gone(peer.public)
            except Exception:
                pass  # the hook never changes the bridge

    def _receive(self, peer: _LivePeer, frame: dict[str, Any]) -> None:
        with self._condition:
            if self._peers.get(peer.public.role) is not peer:
                return
            if frame.get("kind") == "state":
                if (frame.get("role") == peer.public.role.value
                        and frame.get("sessionId") == peer.public.session_id
                        and type(frame.get("generation")) is int
                        and frame.get("generation") == peer.public.generation):
                    peer.state = frame
            elif frame.get("kind") == "api_ack":
                request_id = frame.get("requestId")
                pending = self._pending_acks.get(request_id) if isinstance(request_id, str) else None
                if pending is not None and pending[0] is peer and pending[1] is None:
                    self._pending_acks[request_id] = (peer, frame)
            elif frame.get("kind") == "omp_event":
                if (frame.get("sessionId") == peer.public.session_id
                        and type(frame.get("generation")) is int
                        and frame.get("generation") == peer.public.generation
                        and isinstance(frame.get("name"), str)):
                    self._event_sequence += 1
                    self._events.append({
                        **frame,
                        "role": peer.public.role.value,
                        "bridgeSequence": self._event_sequence,
                    })
            elif frame.get("kind") == "tool_request":
                self._accept_tool_request(peer, frame)
            self._condition.notify_all()

    def peer(self, role: ActorRole | str, timeout: float = 0) -> BridgePeer:
        target = _role(role)
        deadline = time.monotonic() + max(timeout, 0)
        with self._condition:
            while target not in self._peers and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeDisconnected(f"no connected {target.value} OMP session")
                self._condition.wait(remaining)
            if self._closed:
                raise BridgeDisconnected("bridge server is closed")
            return self._peers[target].public

    def request(self, role: ActorRole | str, frame: Mapping[str, Any], timeout: float = 5,
                *, expected_peer: tuple[str, int] | None = None,
                expected_peers: Mapping[ActorRole | str, tuple[str, int]] | None = None,
                authority_token: object | None = None) -> dict[str, Any]:
        target = _role(role)
        if not isinstance(frame, Mapping) or "requestId" in frame:
            raise MailboxError("bridge requests must be objects without requestId")
        expected: dict[ActorRole, tuple[str, int]] = {}
        if expected_peers is not None:
            expected = {_role(peer_role): identity
                        for peer_role, identity in expected_peers.items()}
        if expected_peer is not None:
            if target in expected and expected[target] != expected_peer:
                raise MailboxError("conflicting expected peer identities")
            expected[target] = expected_peer
        for identity in expected.values():
            if (not isinstance(identity, tuple) or len(identity) != 2
                    or not isinstance(identity[0], str) or not identity[0]
                    or type(identity[1]) is not int or identity[1] < 1):
                raise MailboxError("expected peer must have sessionId and generation")
        current = None if authority_token is None else getattr(authority_token, "current", None)
        if authority_token is not None and not callable(current):
            raise MailboxError("authority token must expose current()")
        try:
            claim = None if authority_token is None else getattr(authority_token, "claim", None)
        except Exception as exc:
            raise MailboxError("authority claim is malformed") from exc
        if claim is not None and not callable(claim):
            raise MailboxError("authority claim must be callable")
        deadline = time.monotonic() + max(timeout, 0)
        request_id = new_identifier()
        encoded = (json.dumps({**dict(frame), "requestId": request_id}, separators=(",", ":")) + "\n").encode()
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeTimeout(f"{target.value} OMP bridge request timed out")
            try:
                public = self.peer(target, timeout=remaining)
            except BridgeDisconnected as exc:
                with self._condition:
                    if self._closed:
                        raise
                if time.monotonic() >= deadline:
                    raise BridgeTimeout(f"{target.value} OMP bridge request timed out") from exc
                raise
            if deadline - time.monotonic() <= 0:
                raise BridgeTimeout(f"{target.value} OMP bridge request timed out")
            live = self._live_peer(target, public)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not live.write_lock.acquire(timeout=max(remaining, 0)):
                raise BridgeTimeout(f"{target.value} OMP bridge request timed out")
            try:
                with self._condition:
                    if self._closed:
                        raise BridgeDisconnected("bridge server is closed")
                    if self._peers.get(target) is not live:
                        error = (BridgeBoundMismatch if expected or current is not None
                                 else BridgeDisconnected)
                        raise error("OMP session changed before bridge request")
                    if any((self._peers.get(peer_role) is None
                            or (self._peers[peer_role].public.session_id,
                                self._peers[peer_role].public.generation) != identity)
                           for peer_role, identity in expected.items()):
                        raise BridgeBoundMismatch("bound OMP peer changed before submission")
                    if current is not None:
                        try:
                            valid = current()
                        except Exception:
                            valid = False
                        if valid is not True:
                            raise BridgeBoundMismatch("authority changed before submission")
                    self._pending_acks[request_id] = (live, None)
                # write_lock keeps this request next at the socket. The claim
                # linearizes a concurrent revoke before any frame bytes leave.
                if claim is not None:
                    try:
                        claimed = claim()
                    except Exception:
                        claimed = False
                    if claimed is not True:
                        raise BridgeBoundMismatch("authority claim rejected before submission")
                self._send_until(live.connection, encoded, deadline)
            except (OSError, ValueError) as exc:
                raise BridgeDisconnected(f"{target.value} OMP bridge connection closed") from exc
            finally:
                live.write_lock.release()

            with self._condition:
                while True:
                    if self._closed:
                        raise BridgeDisconnected("bridge server is closed")
                    if self._peers.get(target) is not live:
                        raise BridgeDisconnected("OMP session changed before bridge acknowledgement")
                    pending = self._pending_acks.get(request_id)
                    if pending is None or pending[0] is not live:
                        raise BridgeDisconnected("bridge acknowledgement is no longer pending")
                    if pending[1] is not None:
                        ack = pending[1]
                        self._pending_acks.pop(request_id, None)
                        return dict(ack)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BridgeTimeout(f"{target.value} OMP bridge request timed out")
                    self._condition.wait(remaining)
        finally:
            with self._condition:
                self._pending_acks.pop(request_id, None)

    @staticmethod
    def _send_until(connection: socket.socket, payload: bytes, deadline: float) -> None:
        sent = 0
        view = memoryview(payload)
        while sent < len(view):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeTimeout("OMP bridge request timed out while sending")
            try:
                _, writable, exceptional = select.select([], [connection], [connection], remaining)
            except InterruptedError:
                continue
            if exceptional:
                raise ConnectionError("OMP bridge connection is not writable")
            if not writable:
                raise BridgeTimeout("OMP bridge request timed out while sending")
            try:
                count = connection.send(view[sent:], socket.MSG_DONTWAIT)
            except (BlockingIOError, InterruptedError):
                continue
            if count <= 0:
                raise ConnectionError("OMP bridge connection closed while sending")
            sent += count

    def _live_peer(self, role: ActorRole, public: BridgePeer) -> _LivePeer:
        with self._condition:
            live = self._peers.get(role)
            if live is None or live.public != public:
                raise BridgeDisconnected(f"{role.value} OMP session is no longer current")
            return live

    def probe(self, role: ActorRole | str, timeout: float = 5) -> dict[str, Any]:
        ack = self.request(role, {"kind": "probe"}, timeout)
        state = ack.get("state")
        if ack.get("status") != "state" or not isinstance(state, dict):
            raise MailboxError("OMP did not return a current public state snapshot")
        return state

    def event_cursor(self) -> int:
        with self._condition:
            return self._event_sequence

    def events_since(self, role: ActorRole | str, names: tuple[str, ...], after_sequence: int) -> tuple[int, int]:
        """(current cursor, how many retained events of ``role`` named ``names`` came after ``after_sequence``);
        never waits (C-D70 (1): the watchdog sees a turn it did not probe)."""
        target = _role(role).value
        wanted = frozenset(names)
        with self._condition:
            count = sum(1 for event in self._events if event.get("bridgeSequence", 0) > after_sequence
                        and event.get("role") == target and event.get("name") in wanted)
            return self._event_sequence, count

    def events_after(self, names: tuple[str, ...], after_sequence: int) -> tuple[int, list[dict[str, Any]]]:
        """(current cursor, retained events named ``names`` of either role after ``after_sequence``); never waits
        (CW-19: the backend reads ``model_turn_result``)."""
        wanted = frozenset(names)
        with self._condition:
            return self._event_sequence, [dict(event) for event in self._events
                                          if event.get("bridgeSequence", 0) > after_sequence
                                          and event.get("name") in wanted]

    def wait_event(
        self,
        role: ActorRole | str,
        name: str,
        expected: Mapping[str, Any],
        *,
        after_sequence: int = 0,
        timeout: float = 15,
    ) -> dict[str, Any]:
        return self.wait_any_event(
            role, (name,), expected, after_sequence=after_sequence, timeout=timeout,
        )

    def wait_any_event(
        self,
        role: ActorRole | str,
        names: tuple[str, ...],
        expected: Mapping[str, Any],
        *,
        after_sequence: int = 0,
        timeout: float = 15,
    ) -> dict[str, Any]:
        target = _role(role)
        allowed_names = frozenset(names)
        if not allowed_names or any(not isinstance(name, str) or not name for name in allowed_names):
            raise ValueError("at least one non-empty event name is required")
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self._events:
                    if (event.get("bridgeSequence", 0) > after_sequence
                            and event.get("role") == target.value
                            and event.get("name") in allowed_names
                            and all(event.get(key) == value for key, value in expected.items())):
                        return dict(event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeTimeout(
                        f"OMP event {','.join(sorted(allowed_names))} was not observed for {target.value}"
                    )
                self._condition.wait(remaining)


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    """A process-owned handle for one persisted logical Task message."""

    message_id: str
    task_id: str
    revision: int
    revision_id: str
    run_id: str
    sender_role: ActorRole
    target_role: ActorRole
    session_id: str
    session_generation: int
    kind: MessageKind
    payload_json: str
    in_reply_to_message_id: str | None

    @property
    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise MailboxError("stored logical message payload is not an object")
        return value

    def envelope(self, attempt_id: str) -> ControlEnvelope:
        return ControlEnvelope(
            message_id=self.message_id,
            delivery_attempt_id=attempt_id,
            sender_role=self.sender_role,
            session_id=self.session_id,
            session_generation=self.session_generation,
            task_id=self.task_id,
            revision_id=self.revision_id,
            run_id=self.run_id,
            event=MessageEvent(self.kind, self.payload, self.in_reply_to_message_id),
        )


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    message_id: str
    delivery_attempt_id: str | None
    target_role: ActorRole
    session_id: str
    session_generation: int
    status: MailboxStatus
    details: Mapping[str, Any]


_FALLBACK_DELIVERY_LOCKS: dict[tuple[int, ActorRole], Lock] = {}
_FALLBACK_DELIVERY_GUARD = Lock()


def _delivery_lock(bridge: Any, role: ActorRole) -> Lock:
    """The bridge's per-target delivery lock (a shared per-object lock for other bridges)."""
    getter = getattr(bridge, "delivery_lock", None)
    if callable(getter):
        return getter(role)
    with _FALLBACK_DELIVERY_GUARD:
        return _FALLBACK_DELIVERY_LOCKS.setdefault((id(bridge), role), Lock())


def state_blockers(state: Mapping[str, Any]) -> list[str]:
    """Why an OMP state snapshot is not safe for a delivery (short machine names, never editor text)."""
    blockers = []
    if state.get("idle") is not True:
        blockers.append("busy")
    if state.get("pending") is not False:
        blockers.append("pending_messages")
    if state.get("approvalPending") is not False:
        blockers.append("approval_pending")
    if state.get("editorKnown") is not True:
        blockers.append("editor_unknown")
    elif state.get("editorEmpty") is not True:
        blockers.append("editor_not_empty")
    if type(state.get("inFlightToolCount")) is not int or state.get("inFlightToolCount") != 0:
        blockers.append("tool_running")
    if state.get("paused") is not False:
        blockers.append("paused")
    return blockers


class TaskMailbox:
    """Persists logical messages and one-way delivery evidence through CW-09 APIs.

    Deliveries to one target OMP are serialized across every mailbox on the same
    bridge (``G3BridgeServer.delivery_lock``): two messages never race into the
    same OMP turn, whichever component (workflow stage or handoff outbox) sends.
    """

    def __init__(self, repository: TaskRepository, bridge: G3BridgeServer):
        self._repository = repository
        self._bridge = bridge
        self._owner = new_identifier()
        self._messages: dict[str, MailboxMessage] = {}
        self._ledgers: dict[tuple[ActorRole, str, int], DeliveryLedger] = {}
        self._receipts: dict[str, DeliveryReceipt] = {}
        self._lock = RLock()

    def create_message(
        self,
        task_id: str,
        revision: int,
        run_id: str,
        sender_role: ActorRole | str,
        target_role: ActorRole | str,
        kind: MessageKind | str,
        payload: Mapping[str, Any],
        *,
        in_reply_to_message_id: str | None = None,
    ) -> MailboxMessage:
        sender, target = _role(sender_role), _role(target_role)
        try:
            message_kind = kind if isinstance(kind, MessageKind) else MessageKind(kind)
        except ValueError as exc:
            raise MailboxError("unsupported mailbox message kind") from exc
        if not _direction(sender, target, message_kind):
            raise MailboxError("message kind is not valid for this sender/target role direction")
        _uuid(task_id, "task_id")
        _uuid(run_id, "run_id")
        if type(revision) is not int or revision < 1:
            raise MailboxError("revision must be a positive integer")
        run = self._repository.get_run(run_id)
        if (run["task_id"], run["revision"]) != (task_id, revision):
            raise MailboxError("run does not belong to the requested Task revision")
        revision_id = str(uuid5(UUID(task_id), f"task-spec-revision:{revision}"))

        try:
            json_payload = json.dumps(
                dict(payload), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise MailboxError("message payload must be JSON serializable") from exc
        decoded_payload = json.loads(json_payload)
        if not isinstance(decoded_payload, dict):
            raise MailboxError("message payload must be a JSON object")
        if in_reply_to_message_id is not None:
            _uuid(in_reply_to_message_id, "in_reply_to_message_id")
            prior = self._repository.get_message(in_reply_to_message_id)
            content = prior["content"]
            if (prior["task_id"], prior["revision"], prior["run_id"]) != (task_id, revision, run_id):
                raise MailboxError("reply must reference a message from the same Task/revision/run")
            expected_kind = MessageKind.QUESTION if message_kind is MessageKind.ANSWER else MessageKind.TASK
            if (content.get("sender_role") != ActorRole.MANAGER.value
                    or content.get("target_role") != ActorRole.WORKER.value
                    or content.get("kind") != expected_kind.value):
                raise MailboxError("answer/report must reply to the matching manager question/task")
        elif message_kind in {MessageKind.ANSWER, MessageKind.REPORT}:
            raise MailboxError("worker answer/report must reference its question/task message")

        peer = self._bridge.peer(target)
        message_id = new_identifier()
        message = MailboxMessage(
            message_id=message_id,
            task_id=task_id,
            revision=revision,
            revision_id=revision_id,
            run_id=run_id,
            sender_role=sender,
            target_role=target,
            session_id=peer.session_id,
            session_generation=peer.generation,
            kind=message_kind,
            payload_json=json_payload,
            in_reply_to_message_id=in_reply_to_message_id,
        )
        # Validate the full cross-language envelope before the durable insert.
        envelope = message.envelope(new_identifier())
        content = {
            "schema_version": 1,
            "sender_role": sender.value,
            "target_role": target.value,
            "kind": message_kind.value,
            "task_id": task_id,
            "revision": revision,
            "revision_id": revision_id,
            "run_id": run_id,
            "target_session_id": peer.session_id,
            "target_session_generation": peer.generation,
            "in_reply_to_message_id": in_reply_to_message_id,
            "payload": envelope.event.payload,
        }
        self._repository.create_message(task_id, revision, run_id, content, message_id=message_id)
        with self._lock:
            self._messages[message_id] = message
        return message

    def deliver(self, message: MailboxMessage, *, timeout: float = 20,
                expected_peers: Mapping[ActorRole | str, tuple[str, int]] | None = None,
                authority_token: object | None = None,
                on_submitted: Callable[[], None] | None = None) -> DeliveryReceipt:
        """Deliver ``message`` once; the receipt comes after the target's turn (or ``timeout``).

        ``on_submitted`` (CW-18 R1) is called on this thread as soon as the target
        OMP accepted the message into its session (``api_accepted``), before the
        turn's outcome is observed. It is never called for a deferred, rejected or
        unknown submission. Its failure never changes the receipt.
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        with self._lock:
            if self._messages.get(getattr(message, "message_id", "")) is not message:
                raise MailboxError("message handle is not owned by this live mailbox; replay after reopen is forbidden")
            target_lock = _delivery_lock(self._bridge, message.target_role)
        with self._lock, target_lock:
            return self._deliver_serialized(message, timeout, expected_peers, authority_token, on_submitted)

    def _deliver_serialized(self, message: MailboxMessage, timeout: float,
                            expected_peers: Mapping[ActorRole | str, tuple[str, int]] | None,
                            authority_token: object | None,
                            on_submitted: Callable[[], None] | None = None) -> DeliveryReceipt:
        # Held: self._lock and the target's delivery lock.
        if self._messages.get(getattr(message, "message_id", "")) is not message:
            raise MailboxError("message handle is not owned by this live mailbox; replay after reopen is forbidden")
        prior = self._receipts.get(message.message_id)
        if prior is not None and prior.status is not MailboxStatus.DEFERRED:
            return prior
        if authority_token is not None and not authority_token.current():
            receipt = self._receipt(message, None, MailboxStatus.REJECTED, {
                "reason": "authority_changed_before_delivery",
                "api_called": False, "omp_processed": False,
            })
            self._receipts[message.message_id] = receipt
            return receipt
        peer = self._bridge.peer(message.target_role)
        if (peer.session_id, peer.generation) != (message.session_id, message.session_generation):
            receipt = self._receipt(message, None, MailboxStatus.REJECTED, {
                "reason": "target_session_changed",
                "api_called": False,
                "omp_processed": False,
            })
            self._receipts[message.message_id] = receipt
            return receipt

        try:
            state = self._bridge.probe(message.target_role, timeout=min(timeout, 5))
        except (BridgeTimeout, BridgeDisconnected, MailboxError) as exc:
            receipt = self._receipt(message, None, MailboxStatus.DEFERRED, {
                "reason": type(exc).__name__,
                "api_called": False,
                "omp_processed": False,
            })
            return receipt
        if not self._ready_state(message, state):
            return self._receipt(message, None, MailboxStatus.DEFERRED, {
                "reason": "current_omp_state_not_safe",
                # C-D70 (2): which conditions held it (e.g. the manager's composer is not empty)
                "blockers": state_blockers(state),
                "api_called": False,
                "omp_processed": False,
            })

        attempt_id = new_identifier()
        envelope = message.envelope(attempt_id)
        key = (message.target_role, message.session_id, message.session_generation)
        ledger = self._ledgers.setdefault(
            key, DeliveryLedger(message.target_role, message.session_id, message.session_generation)
        )
        try:
            decision, candidate = ledger.begin(
                envelope.to_json(),
                idle=state.get("idle") is True,
                has_pending_messages=state.get("pending") is not False,
                approval_pending=state.get("approvalPending") is not False,
                editor_text="" if state.get("editorKnown") is True and state.get("editorEmpty") is True else None,
                paused=state.get("paused") is not False,
            )
        except ValueError as exc:
            return self._receipt(message, None, MailboxStatus.REJECTED, {
                "reason": "public_contract_rejected_envelope",
                "api_called": False,
                "detail": str(exc),
            })
        if decision is DeliveryState.API_ACCEPTED:
            return self._receipts.get(message.message_id) or self._receipt(
                message, None, MailboxStatus.API_RETURNED, {"deduplicated": True}
            )
        if decision is DeliveryState.UNKNOWN:
            return self._receipts.get(message.message_id) or self._receipt(
                message, None, MailboxStatus.UNKNOWN, {"reason": "prior_attempt_unknown_no_replay"}
            )
        if decision is DeliveryState.DEFERRED or candidate is None:
            return self._receipt(message, None, MailboxStatus.DEFERRED, {
                "reason": "public_delivery_gate_deferred",
                "api_called": False,
                "omp_processed": False,
            })

        try:
            self._repository.create_delivery_attempt(message.message_id, attempt_id=attempt_id)
        except Exception as exc:
            ledger.finish(candidate, api_accepted=False)
            receipt = self._receipt(message, attempt_id, MailboxStatus.REJECTED, {
                "reason": "delivery_attempt_persistence_failed",
                "error_type": type(exc).__name__,
                "api_called": False,
            })
            self._receipts[message.message_id] = receipt
            return receipt

        cursor = self._bridge.event_cursor()
        try:
            frame = {"kind": "deliver", "envelope": candidate.to_json()}
            if isinstance(self._bridge, G3BridgeServer):
                ack = self._bridge.request(
                    message.target_role, frame, timeout=timeout,
                    expected_peer=(message.session_id, message.session_generation),
                    expected_peers=expected_peers, authority_token=authority_token,
                )
            elif authority_token is None and expected_peers is None:
                ack = self._bridge.request(message.target_role, frame, timeout=timeout)
            else:
                raise BridgeBoundMismatch("bound delivery requires G3BridgeServer")
        except BridgeBoundMismatch as exc:
            ledger.finish(candidate, api_accepted=False)
            receipt = self._receipt(message, attempt_id, MailboxStatus.REJECTED, {
                "reason": type(exc).__name__, "api_called": False,
                "omp_processed": False, "automatic_replay": False,
            })
            self._record(attempt_id, "failed", receipt.details)
            self._receipts[message.message_id] = receipt
            return receipt
        except (BridgeTimeout, BridgeDisconnected, OSError) as exc:
            ledger.finish(candidate, api_accepted=None)
            receipt = self._receipt(message, attempt_id, MailboxStatus.UNKNOWN, {
                "reason": type(exc).__name__,
                "stage": "api_return",
                "api_called": True,
                "automatic_replay": False,
            })
            self._record(attempt_id, "unknown", receipt.details)
            self._receipts[message.message_id] = receipt
            return receipt

        if authority_token is not None and not authority_token.current():
            ledger.finish(candidate, api_accepted=None)
            receipt = self._receipt(message, attempt_id, MailboxStatus.UNKNOWN, {
                "reason": "authority_changed_after_submission",
                "api_called": True, "automatic_replay": False,
            })
            self._record(attempt_id, "unknown", receipt.details)
            self._receipts[message.message_id] = receipt
            return receipt

        ack_status = ack.get("status")
        ack_details = {
            "method": "pi.sendUserMessage",
            "api_status": ack_status,
            "request_id": ack.get("requestId"),
            "message_id": message.message_id,
            "delivery_attempt_id": attempt_id,
            "target_role": message.target_role.value,
            "session_id": message.session_id,
            "session_generation": message.session_generation,
            "model_processed": False,
        }
        if ack_status != "api_accepted":
            if ack_status == "deferred":
                ledger.finish(candidate, api_accepted=False)
                self._record(attempt_id, "failed", {**ack_details, "reason": ack.get("reason")})
                return self._receipt(message, attempt_id, MailboxStatus.DEFERRED, {
                    **ack_details,
                    "reason": ack.get("reason", "public_extension_rechecked_state"),
                    "api_called": False,
                    "omp_processed": False,
                })
            if ack_status in {"unknown_no_replay", "duplicate_api_accepted"}:
                ledger.finish(candidate, api_accepted=None)
                receipt = self._receipt(message, attempt_id, MailboxStatus.UNKNOWN, {
                    **ack_details,
                    "reason": ack.get("reason", "extension_refused_replay"),
                    "automatic_replay": False,
                })
                self._record(attempt_id, "unknown", receipt.details)
                self._receipts[message.message_id] = receipt
                return receipt
            ledger.finish(candidate, api_accepted=False)
            receipt = self._receipt(message, attempt_id, MailboxStatus.REJECTED, {
                **ack_details,
                "reason": ack.get("reason", "extension_rejected_delivery"),
                "api_called": False,
                "omp_processed": False,
            })
            self._record(attempt_id, "failed", receipt.details)
            self._receipts[message.message_id] = receipt
            return receipt

        ledger.finish(candidate, api_accepted=True)
        if on_submitted is not None:
            try:
                on_submitted()
            except Exception:
                pass  # the caller's bookkeeping never changes the delivery evidence
        try:
            self._record(attempt_id, "api_returned", ack_details)
        except Exception as exc:
            receipt = self._receipt(message, attempt_id, MailboxStatus.UNKNOWN, {
                **ack_details,
                "reason": "api_return_persistence_failed",
                "error_type": type(exc).__name__,
                "automatic_replay": False,
            })
            self._receipts[message.message_id] = receipt
            return receipt

        expected = {
            "messageId": message.message_id,
            "deliveryAttemptId": attempt_id,
            "taskId": message.task_id,
            "revisionId": message.revision_id,
            "runId": message.run_id,
            "sessionId": message.session_id,
            "generation": message.session_generation,
        }
        try:
            event = self._bridge.wait_any_event(
                message.target_role,
                ("delivery_omp_processed", "delivery_processing_unknown"),
                expected,
                after_sequence=cursor,
                timeout=timeout,
            )
        except (BridgeTimeout, BridgeDisconnected) as exc:
            receipt = self._receipt(message, attempt_id, MailboxStatus.UNKNOWN, {
                **ack_details,
                "reason": type(exc).__name__,
                "stage": "omp_processing_observation",
                "automatic_replay": False,
                "task_completed": False,
            })
            self._record(attempt_id, "unknown", receipt.details)
            self._receipts[message.message_id] = receipt
            return receipt

        if event.get("name") == "delivery_processing_unknown":
            receipt = self._receipt(message, attempt_id, MailboxStatus.UNKNOWN, {
                **ack_details,
                "processing_event": event.get("name"),
                "provider_request_matched": event.get("providerRequestMatched") is True,
                "provider_response_observed": event.get("providerResponseObserved") is True,
                "agent_end_observed": event.get("agentEndObserved") is True,
                "reason": event.get("reason", "omp_processing_unknown"),
                "automatic_replay": False,
                "task_completed": False,
            })
            self._record(attempt_id, "unknown", receipt.details)
            self._receipts[message.message_id] = receipt
            return receipt

        evidence = {
            **ack_details,
            "processing_event": event.get("name"),
            "provider_request_matched": event.get("providerRequestMatched") is True,
            "provider_response_observed": event.get("providerResponseObserved") is True,
            "agent_end_observed": event.get("agentEndObserved") is True,
            "task_completed": False,
        }
        if not all((evidence["provider_request_matched"], evidence["provider_response_observed"], evidence["agent_end_observed"])):
            receipt = self._receipt(message, attempt_id, MailboxStatus.UNKNOWN, {
                **evidence,
                "reason": "processing_event_incomplete",
                "automatic_replay": False,
            })
            self._record(attempt_id, "unknown", receipt.details)
        else:
            receipt = self._receipt(message, attempt_id, MailboxStatus.OMP_PROCESSED, evidence)
            self._record(attempt_id, "omp_processed", evidence)
        self._receipts[message.message_id] = receipt
        return receipt

    def _ready_state(self, message: MailboxMessage, state: Mapping[str, Any]) -> bool:
        peer = self._bridge.peer(message.target_role)
        return (
            peer.session_id == message.session_id
            and peer.generation == message.session_generation
            and state.get("role") == message.target_role.value
            and state.get("sessionId") == message.session_id
            and type(state.get("generation")) is int
            and state.get("generation") == message.session_generation
            and state.get("idle") is True
            and state.get("pending") is False
            and state.get("approvalPending") is False
            and state.get("editorKnown") is True
            and state.get("editorEmpty") is True
            and type(state.get("inFlightToolCount")) is int
            and state.get("inFlightToolCount") == 0
            and state.get("paused") is False
        )

    def _receipt(
        self,
        message: MailboxMessage,
        attempt_id: str | None,
        status: MailboxStatus,
        details: Mapping[str, Any],
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            message_id=message.message_id,
            delivery_attempt_id=attempt_id,
            target_role=message.target_role,
            session_id=message.session_id,
            session_generation=message.session_generation,
            status=status,
            details=dict(details),
        )

    def _record(self, attempt_id: str, status: str, details: Mapping[str, Any]) -> None:
        self._repository.record_delivery_status(attempt_id, status, dict(details))


__all__ = [
    "BridgeDisconnected",
    "BridgePeer",
    "BridgeTimeout",
    "DeliveryReceipt",
    "G3BridgeServer",
    "MailboxError",
    "MailboxMessage",
    "MailboxStatus",
    "TaskMailbox",
    "state_blockers",
]
