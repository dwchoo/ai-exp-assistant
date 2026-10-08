"""Non-blocking UDS server for the ui_v1 contract.

The server owns only connections. Pane, shell, bridge and shutdown decisions
belong to the controller. Closing, detaching or misbehaving clients never stop
the backend: a protocol error closes only that connection, and a request whose
handler fails unexpectedly is answered with ``internal_error``.
"""

from __future__ import annotations

from collections import deque
import errno
import os
from pathlib import Path
import select
import socket
import stat
import sys
import time
import traceback
from typing import Any, Iterable, Protocol

from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, Reason, ServerType
from workbench.contracts.v1 import ContractError, DisplayChunk, PaneId

MAX_OUTBOUND_BYTES = 16 * 1024 * 1024
RECV_BYTES = 256 * 1024
MAX_CONNECTIONS = 16


class Held(Exception):
    """A controller refusal carrying a contract reason for the client."""

    def __init__(self, reason: Reason, detail: str):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


class Controller(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def replay(self) -> list[DisplayChunk]: ...
    def on_attach(self, size: tuple[int, int] | None) -> None: ...
    def on_detach(self) -> None: ...
    def admit(self, pane: PaneId, data: bytes, kind: str) -> tuple[Reason, str] | None: ...
    def resize(self, pane: PaneId | None, rows: int, cols: int) -> None: ...
    def set_focus(self, pane: PaneId) -> None: ...
    def takeover_request(self) -> dict[str, Any]: ...
    def takeover_confirm(self) -> dict[str, Any]: ...
    def handoff(self) -> dict[str, Any]: ...
    def shutdown_request(self) -> dict[str, Any]: ...
    def shutdown_confirm(self, token: str) -> dict[str, Any]: ...
    def confirm_boot(self, boot_id: str) -> dict[str, Any]: ...
    def restart_pane(self, pane: PaneId) -> dict[str, Any]: ...
    def kill_pane(self, pane: PaneId) -> dict[str, Any]: ...
    def pause(self) -> dict[str, Any]: ...
    def resume(self, reconciled: bool) -> dict[str, Any]: ...


class _Connection:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.decoder = ui_v1.FrameDecoder()
        self.version: int | None = None
        self.client = ""
        self.outbound = bytearray()
        # Frame boundaries of ``outbound``: lengths of the queued frames (head first) and how many bytes of
        # the head frame are already written. Updated per send/write, amortised O(1) per frame.
        self.frame_lengths: deque[int] = deque()
        self.head_sent = 0
        self.closing = False

    def fileno(self) -> int:
        return self.sock.fileno()


class UiServer:
    def __init__(self, path: Path, controller: Controller, *, max_outbound: int = MAX_OUTBOUND_BYTES):
        self.path = Path(path)
        self.controller = controller
        self.max_outbound = max_outbound
        self.connections: list[_Connection] = []
        self.attached: _Connection | None = None
        self.stats = {"accepted": 0, "closed": 0, "protocol_errors": 0, "version_rejects": 0,
                      "attaches": 0, "detaches": 0, "handler_errors": 0}
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            listener.bind(str(self.path))
        except BaseException:
            listener.close()
            raise
        finally:
            os.umask(previous)
        os.chmod(self.path, 0o600)
        created = self.path.lstat()
        self._identity = (created.st_dev, created.st_ino)
        listener.listen(8)
        listener.setblocking(False)
        self.listener: socket.socket | None = listener

    # -- event loop ------------------------------------------------------
    def poll(self, timeout: float, *, extra_read: Iterable[int] = (),
             extra_write: Iterable[int] = ()) -> set[int]:
        reads: list[Any] = [*extra_read]
        writes: list[Any] = [*extra_write]
        if self.listener is not None:
            reads.append(self.listener)
        for connection in self.connections:
            if not connection.closing:
                reads.append(connection)
            if connection.outbound:
                writes.append(connection)
        try:
            readable, writable, _ = select.select(reads, writes, [], max(0.0, timeout))
        except InterruptedError:
            return set()
        ready: set[int] = set()
        for item in readable:
            if item is self.listener:
                self._accept()
            elif isinstance(item, _Connection):
                self._read(item)
            else:
                ready.add(item)
        for item in writable:
            if isinstance(item, _Connection):
                self._write(item)
            else:
                ready.add(item)
        self._reap()
        return ready

    def _accept(self) -> None:
        assert self.listener is not None
        while True:
            try:
                sock, _ = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                if exc.errno in {errno.EMFILE, errno.ENFILE}:
                    return
                raise
            sock.setblocking(False)
            connection = _Connection(sock)
            self.stats["accepted"] += 1
            if len(self.connections) >= MAX_CONNECTIONS:
                self._fail(connection, Reason.PROTOCOL_ERROR, "too many connections")
            self.connections.append(connection)

    def _read(self, connection: _Connection) -> None:
        try:
            data = connection.sock.recv(RECV_BYTES)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            self._drop(connection)
            return
        try:
            frames = connection.decoder.feed(data)
        except ui_v1.ProtocolError as exc:
            self.stats["protocol_errors"] += 1
            self._fail(connection, Reason.PROTOCOL_ERROR, str(exc))
            return
        for frame in frames:
            if connection.closing:
                return
            self._handle(connection, frame)

    def _write(self, connection: _Connection) -> None:
        try:
            sent = connection.sock.send(connection.outbound)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(connection)
            return
        del connection.outbound[:sent]
        done = connection.head_sent + sent
        lengths = connection.frame_lengths
        while lengths and done >= lengths[0]:
            done -= lengths.popleft()
        connection.head_sent = done

    def _reap(self) -> None:
        for connection in list(self.connections):
            if connection.closing and not connection.outbound:
                self._drop(connection)

    # -- sending ---------------------------------------------------------
    def _send(self, connection: _Connection, data: bytes) -> None:
        """Queue one whole encoded frame. Nothing is queued once the connection is closing."""
        if connection.closing:
            return
        if len(connection.outbound) + len(data) > self.max_outbound:
            # A client that does not read cannot hold backend memory; it may reattach. Keep the unsent
            # rest of a partially written frame so the stream stays decodable, drop only whole unsent
            # frames, and end with CLOSING slow_client.
            lengths = connection.frame_lengths
            if connection.head_sent:
                del connection.outbound[lengths[0] - connection.head_sent:]
                connection.frame_lengths = deque([lengths[0]])
            else:
                connection.outbound.clear()
                lengths.clear()
            self._queue(connection, ui_v1.encode_frame(
                {"v": ui_v1.VERSION, "type": ServerType.CLOSING.value, "reason": Reason.SLOW_CLIENT.value}))
            connection.closing = True
            if connection is self.attached:
                self._detach(connection)
            return
        self._queue(connection, data)

    @staticmethod
    def _queue(connection: _Connection, frame: bytes) -> None:
        connection.outbound.extend(frame)
        connection.frame_lengths.append(len(frame))

    def _header(self, connection: _Connection, header: dict[str, Any], payload: bytes = b"") -> None:
        self._send(connection, ui_v1.encode_frame(header, payload))

    def _fail(self, connection: _Connection, reason: Reason, detail: str) -> None:
        self._header(connection, ui_v1.reject(reason, detail))
        connection.closing = True
        if connection is self.attached:
            self._detach(connection)

    def _drop(self, connection: _Connection) -> None:
        if connection is self.attached:
            self._detach(connection)
        if connection in self.connections:
            self.connections.remove(connection)
            self.stats["closed"] += 1
        try:
            connection.sock.close()
        except OSError:
            pass

    def _detach(self, connection: _Connection) -> None:
        if self.attached is connection:
            self.attached = None
            self.stats["detaches"] += 1
            self.controller.on_detach()

    def broadcast(self, chunk: DisplayChunk) -> None:
        if self.attached is not None and not self.attached.closing:
            self._send(self.attached, ui_v1.encode_display(chunk))

    def push_state(self, snapshot: dict[str, Any]) -> None:
        if self.attached is not None and not self.attached.closing:
            self._header(self.attached, {"v": ui_v1.VERSION, "type": ServerType.STATE.value,
                                         "snapshot": snapshot})

    # -- requests --------------------------------------------------------
    def _handle(self, connection: _Connection, frame: ui_v1.Frame) -> None:
        header = frame.header
        if connection.version is None:
            if header.get("type") != ClientType.HELLO.value:
                self._fail(connection, Reason.HELLO_REQUIRED, "first frame must be hello")
                return
            try:
                message = ui_v1.parse_client_frame(frame, None)
            except ContractError as exc:
                self._fail(connection, Reason.PROTOCOL_ERROR, str(exc))
                return
            version = ui_v1.negotiate(message.fields["versions"])
            if version is None:
                self.stats["version_rejects"] += 1
                self._fail(connection, Reason.VERSION_MISMATCH,
                           f"client versions {message.fields['versions']!r} not supported")
                return
            connection.version, connection.client = version, message.fields["client"]
            self._header(connection, {"v": version, "type": ServerType.WELCOME.value,
                                      "contract": ui_v1.CONTRACT_NAME, "version": version,
                                      "backend_pid": os.getpid()})
            return
        if not ui_v1.version_matches(header, connection.version):
            self.stats["version_rejects"] += 1
            self._fail(connection, Reason.VERSION_MISMATCH,
                       f"frame version {header.get('v')!r} != negotiated {connection.version}")
            return
        raw_id = header.get("id") if isinstance(header.get("id"), str) else None
        try:
            ClientType(header.get("type"))
        except ValueError:
            self._header(connection, ui_v1.result(raw_id, False, reason=Reason.UNSUPPORTED_TYPE,
                                                  detail=f"unsupported type {header.get('type')!r}"))
            return
        if frame.discarded_bytes and header.get("type") in {ClientType.INPUT.value, ClientType.PASTE.value}:
            # Consumed without retention: rejected as a whole, never partially delivered.
            self._header(connection, ui_v1.result(
                raw_id, False, reason=Reason.PASTE_TOO_LARGE,
                detail=f"{frame.discarded_bytes} bytes exceeds {ui_v1.MAX_PASTE_BYTES}",
                pane=header.get("pane"), size=frame.discarded_bytes))
            return
        try:
            message = ui_v1.parse_client_frame(frame, connection.version)
        except ContractError as exc:
            self._header(connection, ui_v1.result(raw_id, False, reason=Reason.INVALID_MESSAGE, detail=str(exc)))
            return
        try:
            fields = self._dispatch(connection, message)
        except Held as held:
            self._header(connection, ui_v1.result(message.id, False, reason=held.reason, detail=held.detail))
            return
        except Exception as exc:  # a handler failure answers this request; it never stops the backend
            self.stats["handler_errors"] += 1
            traceback.print_exc(file=sys.stderr)
            self._header(connection, ui_v1.result(message.id, False, reason=Reason.INTERNAL_ERROR,
                                                  detail=f"{message.type.value} failed: {type(exc).__name__}: {exc}"))
            return
        if fields is not None:
            self._header(connection, ui_v1.result(message.id, True, **fields))
            if message.type in {ClientType.RESTART_PANE, ClientType.KILL_PANE, ClientType.PAUSE,
                                ClientType.RESUME}:
                # The pane is live again / exited now: tell the UI at once, not on the next periodic push (C-D62/C-D63).
                try:
                    self.push_state(self.controller.snapshot())
                except Exception:  # the periodic push follows
                    self.stats["handler_errors"] += 1
                    traceback.print_exc(file=sys.stderr)

    def _require_attached(self, connection: _Connection) -> None:
        if self.attached is not connection:
            raise Held(Reason.NOT_ATTACHED, "attach before sending terminal or control input")

    def _dispatch(self, connection: _Connection, message: ui_v1.ClientMessage) -> dict[str, Any] | None:
        kind, fields, controller = message.type, message.fields, self.controller
        if kind is ClientType.SNAPSHOT:
            return {"snapshot": controller.snapshot()}
        if kind is ClientType.ATTACH:
            if self.attached is not None and self.attached is not connection:
                raise Held(Reason.ATTACHED_ELSEWHERE, "another UI client is attached")
            if self.attached is None:
                self.attached = connection
                self.stats["attaches"] += 1
                controller.on_attach(fields["size"])
            self._header(connection, ui_v1.result(message.id, True, snapshot=controller.snapshot()))
            for chunk in controller.replay():
                self._send(connection, ui_v1.encode_display(chunk, replay=True))
            return None
        if kind is ClientType.DETACH:
            self._require_attached(connection)
            self._detach(connection)
            return {"detached": True}
        if kind is ClientType.SHUTDOWN_REQUEST:
            return controller.shutdown_request()
        if kind is ClientType.SHUTDOWN_CONFIRM:
            return controller.shutdown_confirm(fields["token"])
        if kind is ClientType.CONFIRM_BOOT:
            return controller.confirm_boot(fields["boot_id"])
        self._require_attached(connection)
        if kind in {ClientType.INPUT, ClientType.PASTE}:
            data = message.payload
            if len(data) > ui_v1.MAX_PASTE_BYTES:
                raise Held(Reason.PASTE_TOO_LARGE, f"{len(data)} bytes exceeds {ui_v1.MAX_PASTE_BYTES}")
            refused = controller.admit(fields["pane"], data, kind.value)
            if refused is not None:
                raise Held(*refused)
            return {"pane": fields["pane"].value, "accepted_bytes": len(data)}
        if kind is ClientType.RESIZE:
            controller.resize(fields["pane"], fields["rows"], fields["cols"])
            return {}
        if kind is ClientType.FOCUS:
            controller.set_focus(fields["pane"])
            return {"focus": fields["pane"].value}
        if kind is ClientType.RESTART_PANE:
            return controller.restart_pane(fields["pane"])
        if kind is ClientType.KILL_PANE:
            return controller.kill_pane(fields["pane"])
        if kind is ClientType.TAKEOVER_REQUEST:
            return {"shell": controller.takeover_request()}
        if kind is ClientType.TAKEOVER_CONFIRM:
            return {"shell": controller.takeover_confirm()}
        if kind is ClientType.HANDOFF:
            return {"shell": controller.handoff()}
        if kind is ClientType.PAUSE:
            return controller.pause()
        if kind is ClientType.RESUME:
            return controller.resume(fields["reconciled"])
        raise Held(Reason.UNSUPPORTED_TYPE, f"unsupported type {kind.value}")

    # -- shutdown --------------------------------------------------------
    def stop_accepting(self) -> None:
        if self.listener is not None:
            self.listener.close()
            self.listener = None
            self._unlink()

    def flush(self, timeout: float) -> None:
        """Send what is already queued (e.g. the shutdown confirm's answer) without reading new requests."""
        deadline = time.monotonic() + timeout
        while True:
            pending = [c for c in self.connections if c.outbound]
            remaining = deadline - time.monotonic()
            if not pending or remaining <= 0:
                return
            try:
                writable = select.select([], pending, [], min(remaining, 0.05))[1]
            except InterruptedError:
                continue
            for connection in writable:
                self._write(connection)

    def close(self, final: dict[str, Any] | None = None, *, flush_timeout: float = 2.0) -> None:
        self.stop_accepting()
        header = {"v": ui_v1.VERSION, "type": ServerType.CLOSING.value, "reason": Reason.BACKEND_SHUTDOWN.value}
        if final is not None:
            header["result"] = final
        for connection in self.connections:
            if connection.version is not None and not connection.closing:
                self._header(connection, header)
            connection.closing = True
        deadline = time.monotonic() + flush_timeout
        while any(c.outbound for c in self.connections) and time.monotonic() < deadline:
            self.poll(0.02)
        for connection in list(self.connections):
            self._drop(connection)

    def _unlink(self) -> None:
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == self._identity:
            self.path.unlink()
