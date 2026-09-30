"""Product UI entry: attach to the backend as a ui_v1 client and run the curses loop.

The UI owns no PTY and never signals or stops anything. Quitting sends
``detach`` and closes only this connection.
"""
from __future__ import annotations

import curses
import os
from pathlib import Path
import select
import signal
import sys
import termios
import time
import traceback
from typing import Any
from uuid import uuid4

from workbench.backend.client import ClientError, UiClient, _terminal_size, _write_all
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, ServerType
from workbench.ui.product.model import ProductModel, pane_inner_sizes
from workbench.ui.product.view import draw
from workbench.ui.terminal_g1.app import _ColorPairs

SNAPSHOT_INTERVAL = 5.0
FRAME_INTERVAL = 0.03


class ClientSender:
    def __init__(self, client: UiClient):
        self.client = client

    def send(self, kind: ClientType, payload: bytes = b"", **fields: Any) -> str:
        request_id = uuid4().hex[:16]
        self.client.send_frame(ui_v1.request(kind, request_id, **fields), payload)
        return request_id


def dispatch_frames(client: UiClient, model: ProductModel) -> None:
    """Move frames the client queued into the model."""
    for frame in client.displays:
        model.on_display(frame)
    client.displays.clear()
    for snapshot in client.states:
        model.on_state(snapshot)
    client.states.clear()
    for frame in list(client.pending):
        client.pending.remove(frame)
        kind = frame.header.get("type")
        if kind == ServerType.RESULT.value:
            model.on_result(frame.header)
        elif kind in {ServerType.CLOSING.value, ServerType.REJECT.value}:
            model.on_closing(frame.header)


def loop(win: "curses.window", client: UiClient, model: ProductModel, stdin_fd: int) -> int:
    curses.noecho()
    curses.raw()
    win.keypad(False)
    win.nodelay(True)
    resized = {"flag": False}
    previous = signal.signal(signal.SIGWINCH, lambda *_: resized.__setitem__("flag", True))
    colors = _ColorPairs()
    _write_all(1, b"\x1b[?2004h")
    dirty, last_draw, last_snapshot = True, 0.0, time.monotonic()
    status = 0
    try:
        while True:
            if resized["flag"]:
                resized["flag"] = False
                rows, cols = _terminal_size(1)
                try:
                    curses.resizeterm(rows, cols)
                except curses.error:
                    pass
                win.clear()
                model.resize(rows, cols)
                dirty = True
            try:
                ready = select.select([stdin_fd, client.sock], [], [], 0.05)[0]
            except InterruptedError:
                continue
            if client.sock in ready:
                alive = client.pump(0)
                dispatch_frames(client, model)
                dirty = True
                if not alive:
                    if not model.closed_reason:
                        model.closed_reason = "connection_closed"
                        model.notice = "backend 연결이 끊김"
                    status = 1
                    break
            dispatch_frames(client, model)
            if model.closed_reason:
                status = 1
                break
            if stdin_fd in ready:
                data = os.read(stdin_fd, 65536)
                if not data:
                    break
                model.handle_input(data)
                dirty = True
            else:
                model.flush_input()
            if model.quit:
                break
            now = time.monotonic()
            if now - last_snapshot >= SNAPSHOT_INTERVAL:
                last_snapshot = now
                model._send(ClientType.SNAPSHOT)
                dirty = True  # refresh "마지막 확인" age
            if dirty and now - last_draw >= FRAME_INTERVAL:
                draw(win, model, colors)
                dirty, last_draw = False, now
    finally:
        _write_all(1, b"\x1b[?2004l")
        signal.signal(signal.SIGWINCH, previous)
    return status


def run_product(path: Path, *, stdin_fd: int = 0, stdout_fd: int = 1) -> int:
    """Attach the product UI. 0 on detach, 1 when the backend closed/rejected/error, 2 without a TTY."""
    if not os.isatty(stdin_fd) or not os.isatty(stdout_fd):
        print("attach needs an interactive terminal (stdin/stdout must be a TTY); "
              "use --plain for the minimal client", file=sys.stderr)
        return 2
    client = UiClient(path, name="workbench-ui")
    rows, cols = _terminal_size(stdout_fd)
    model = ProductModel(ClientSender(client), rows, cols)
    status, message = 0, ""
    saved = termios.tcgetattr(stdin_fd)
    try:
        try:
            attached = client.attach(pane_inner_sizes(rows, cols)[model.focus] if not model.too_small() else None)
        except ClientError as exc:
            print(f"attach refused: {exc.header.get('reason', exc)}", file=sys.stderr)
            return 1
        if not attached.get("ok"):
            print(f"attach refused: {attached.get('reason')}: {attached.get('detail')}", file=sys.stderr)
            return 1
        model.attach_done(attached["snapshot"])
        dispatch_frames(client, model)  # replay that arrived with the result
        client.on_display = model.on_display
        model.after_attach()
        try:
            status = curses.wrapper(loop, client, model, stdin_fd)
        except Exception:
            status, message = 1, "ui error:\n" + traceback.format_exc()
        if model.quit and status == 0:
            try:
                client.request(ClientType.DETACH, timeout=5)
            except (ClientError, TimeoutError, OSError):
                pass
        elif status:
            message = message or (model.notice or "disconnected")
    finally:
        try:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        except termios.error:
            pass
        client.close()
    if status == 0:
        _write_all(stdout_fd, b"[workbench] detached; backend keeps running\r\n")
    else:
        _write_all(stdout_fd, f"[workbench] {message}\r\n".encode())
    return status
