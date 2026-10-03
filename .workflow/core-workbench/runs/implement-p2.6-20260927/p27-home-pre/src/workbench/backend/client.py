"""Minimal ui_v1 client for tests and pre-CW-06 use; not the product UI.

``UiClient`` is a blocking request/response helper. ``run_attach`` is a plain
raw-terminal passthrough of one focused pane: ``Ctrl-]`` followed by ``d``
detaches, ``1``/``2``/``3`` switch focus, ``t``/``c``/``h`` request takeover,
confirm takeover and hand the shell back, and a second ``Ctrl-]`` sends it.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty
import fcntl
from typing import Any, Callable
from uuid import uuid4

from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, ServerType
from workbench.contracts.v1 import PaneId

ESCAPE = 0x1D  # Ctrl-]
PANE_KEYS = {ord("1"): PaneId.MANAGER_OMP, ord("2"): PaneId.WORKER_OMP, ord("3"): PaneId.HOST_SHELL}


class ClientError(RuntimeError):
    def __init__(self, message: str, header: dict[str, Any] | None = None):
        super().__init__(message)
        self.header = header or {}


class NotRunning(ClientError):
    """No backend is listening on this data dir's socket."""


class UiClient:
    def __init__(self, path: Path, *, name: str = "workbench-min-client",
                 versions: tuple[int, ...] = ui_v1.SUPPORTED_VERSIONS, timeout: float = 10.0):
        self.path = Path(path)
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(timeout)
            self.sock.connect(str(self.path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            self.sock.close()
            raise NotRunning(f"no backend listening at {self.path}") from exc
        except OSError:
            self.sock.close()
            raise
        self.decoder = ui_v1.FrameDecoder()
        self.pending: list[ui_v1.Frame] = []
        self.displays: list[ui_v1.Frame] = []
        self.states: list[dict[str, Any]] = []
        self.closing: dict[str, Any] | None = None
        self.on_display: Callable[[ui_v1.Frame], None] | None = None
        self.welcome: dict[str, Any] | None = None
        self.send_frame(ui_v1.hello(name, versions))
        frame = self.next_frame(timeout)
        if frame.header.get("type") != ServerType.WELCOME.value:
            self.close()
            raise ClientError(f"backend rejected hello: {frame.header.get('reason')}", frame.header)
        self.welcome = frame.header
        self.version = frame.header["version"]

    # -- low level -------------------------------------------------------
    def send_frame(self, header: dict[str, Any], payload: bytes = b"") -> None:
        self.send_raw(ui_v1.encode_frame(header, payload))

    def send_raw(self, data: bytes) -> None:
        self.sock.settimeout(self.timeout)
        self.sock.sendall(data)

    def fileno(self) -> int:
        return self.sock.fileno()

    def pump(self, timeout: float) -> bool:
        """Read once; False on EOF."""
        ready = select.select([self.sock], [], [], max(0.0, timeout))[0]
        if not ready:
            return True
        try:
            data = self.sock.recv(1 << 20)
        except (BlockingIOError, InterruptedError, socket.timeout):
            return True
        except OSError:
            data = b""
        if not data:
            return False
        for frame in self.decoder.feed(data):
            kind = frame.header.get("type")
            if kind == ServerType.DISPLAY.value:
                if self.on_display is not None:
                    self.on_display(frame)
                else:
                    self.displays.append(frame)
            elif kind == ServerType.STATE.value:
                self.states.append(frame.header.get("snapshot", {}))
            elif kind == ServerType.CLOSING.value:
                self.closing = frame.header
                self.pending.append(frame)
            else:
                self.pending.append(frame)
        return True

    def next_frame(self, timeout: float | None = None) -> ui_v1.Frame:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while not self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no frame from backend")
            if not self.pump(remaining):
                if self.pending:
                    break
                raise ClientError("backend closed the connection", self.closing)
        return self.pending.pop(0)

    def request(self, kind: ClientType, payload: bytes = b"", *, timeout: float | None = None,
                **fields: Any) -> dict[str, Any]:
        request_id = uuid4().hex[:16]
        self.send_frame({"v": self.version, "type": kind.value, "id": request_id, **fields}, payload)
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            frame = self.next_frame(max(0.0, deadline - time.monotonic()))
            if frame.header.get("type") == ServerType.RESULT.value and frame.header.get("id") == request_id:
                return frame.header
            if frame.header.get("type") in {ServerType.REJECT.value, ServerType.CLOSING.value}:
                raise ClientError(f"backend closed: {frame.header.get('reason')}", frame.header)

    # -- helpers ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return self.request(ClientType.SNAPSHOT)["snapshot"]

    def attach(self, size: tuple[int, int] | None = None) -> dict[str, Any]:
        fields = {} if size is None else {"size": {"rows": size[0], "cols": size[1]}}
        return self.request(ClientType.ATTACH, **fields)

    def detach(self) -> dict[str, Any]:
        return self.request(ClientType.DETACH)

    def paste(self, pane: PaneId, data: bytes) -> dict[str, Any]:
        return self.request(ClientType.PASTE, data, pane=pane.value)

    def input(self, pane: PaneId, data: bytes) -> dict[str, Any]:
        return self.request(ClientType.INPUT, data, pane=pane.value)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> UiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _terminal_size(fd: int) -> tuple[int, int]:
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
        if rows and cols:
            return min(rows, ui_v1.MAX_TERMINAL_ROWS), min(cols, ui_v1.MAX_TERMINAL_COLUMNS)
    except OSError:
        pass
    return 30, 100


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            select.select([], [fd], [], 0.1)
            continue
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise
        view = view[written:]


def run_attach(path: Path, *, stdin_fd: int = 0, stdout_fd: int = 1) -> int:
    """Attach one terminal. Returns 0 on detach, 1 when the backend closed."""
    if not os.isatty(stdin_fd) or not os.isatty(stdout_fd):
        print("attach needs an interactive terminal (stdin/stdout must be a TTY)", file=sys.stderr)
        return 2
    client = UiClient(path)
    rows, cols = _terminal_size(stdout_fd)
    try:
        attached = client.attach((rows, cols))
    except ClientError as exc:
        client.close()
        print(f"attach refused: {exc.header.get('reason', exc)}", file=sys.stderr)
        return 1
    if not attached.get("ok"):
        client.close()
        print(f"attach refused: {attached.get('reason')}: {attached.get('detail')}", file=sys.stderr)
        return 1
    focus = PaneId(attached["snapshot"].get("focus", PaneId.MANAGER_OMP.value))

    def show(frame: ui_v1.Frame) -> None:
        if frame.header.get("pane") == focus.value:
            _write_all(stdout_fd, frame.payload)

    def notice(text: str) -> None:
        _write_all(stdout_fd, f"\r\n[workbench] {text}\r\n".encode())

    client.on_display = show
    for frame in client.displays:
        show(frame)
    client.displays.clear()
    resized = {"flag": False}
    previous_winch = signal.signal(signal.SIGWINCH, lambda *_: resized.__setitem__("flag", True))
    saved = termios.tcgetattr(stdin_fd)
    notice(f"attached to {focus.value}; Ctrl-] d detaches, Ctrl-] 1/2/3 switches pane")
    status, escape = 0, False
    try:
        tty.setraw(stdin_fd)
        while True:
            if resized["flag"]:
                resized["flag"] = False
                rows, cols = _terminal_size(stdout_fd)
                client.send_frame(ui_v1.request(ClientType.RESIZE, uuid4().hex[:16], rows=rows, cols=cols))
            try:
                ready = select.select([stdin_fd, client.sock], [], [], 0.25)[0]
            except InterruptedError:
                continue
            if client.sock in ready and not client.pump(0):
                status = 1
                break
            for frame in list(client.pending):
                client.pending.remove(frame)
                header = frame.header
                if header.get("type") == ServerType.RESULT.value and not header.get("ok"):
                    notice(f"refused: {header.get('reason')}: {header.get('detail')}")
                elif header.get("type") in {ServerType.CLOSING.value, ServerType.REJECT.value}:
                    notice(f"backend closed the connection: {header.get('reason')}")
                    status = 1
            if status:
                break
            if stdin_fd not in ready:
                continue
            data = os.read(stdin_fd, 65536)
            if not data:
                break
            forward = bytearray()
            detach = False
            for byte in data:
                if escape:
                    escape = False
                    if byte == ESCAPE:
                        forward.append(byte)
                    elif byte in (ord("d"), ord("D")):
                        detach = True
                        break
                    elif byte in PANE_KEYS:
                        if forward:
                            client.send_frame(ui_v1.request(ClientType.INPUT, uuid4().hex[:16],
                                                            pane=focus.value), bytes(forward))
                            forward.clear()
                        focus = PANE_KEYS[byte]
                        client.send_frame(ui_v1.request(ClientType.FOCUS, uuid4().hex[:16], pane=focus.value))
                        _write_all(stdout_fd, b"\x1b[2J\x1b[H")
                        # Ask the application to redraw by nudging its window size.
                        client.send_frame(ui_v1.request(ClientType.RESIZE, uuid4().hex[:16], pane=focus.value,
                                                        rows=max(1, rows - 1), cols=cols))
                        client.send_frame(ui_v1.request(ClientType.RESIZE, uuid4().hex[:16], pane=focus.value,
                                                        rows=rows, cols=cols))
                    elif byte in (ord("t"), ord("c"), ord("h")):
                        kind = {ord("t"): ClientType.TAKEOVER_REQUEST, ord("c"): ClientType.TAKEOVER_CONFIRM,
                                ord("h"): ClientType.HANDOFF}[byte]
                        client.send_frame(ui_v1.request(kind, uuid4().hex[:16]))
                elif byte == ESCAPE:
                    escape = True
                else:
                    forward.append(byte)
            if forward:
                client.send_frame(ui_v1.request(ClientType.INPUT, uuid4().hex[:16], pane=focus.value),
                                  bytes(forward))
            if detach:
                try:
                    client.request(ClientType.DETACH, timeout=5)
                except (ClientError, TimeoutError):
                    pass
                break
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        signal.signal(signal.SIGWINCH, previous_winch)
        client.close()
    _write_all(stdout_fd, b"\r\n[workbench] detached; backend keeps running\r\n" if status == 0
               else b"\r\n[workbench] disconnected\r\n")
    return status
