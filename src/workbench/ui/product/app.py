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
from workbench.ui.product.layout import LAYOUT_FILE, load_layout, save_layout
from workbench.ui.product.model import ProductModel
from workbench.ui.product.view import draw
from workbench.ui.terminal_g1.app import _ColorPairs

# Real terminals and multiplexers (xterm, VTE, tmux, herdr) keep ONE mouse-tracking mode for 1000/1002/1003:
# setting one replaces the mode, resetting any of them turns reporting off. So the mode is chosen once, at start:
# 1002 (button-event: press/release, wheel and motion while a button is held, for divider drags; 1000 first for
# terminals without 1002) with SGR encoding, and never switched while running. Only exit / prefix m reset it.
MOUSE_ON = b"\x1b[?1000h\x1b[?1002h\x1b[?1006h"
MOUSE_OFF = b"\x1b[?1006l\x1b[?1002l\x1b[?1000l"
SNAPSHOT_INTERVAL = 5.0
FRAME_INTERVAL = 0.03
BACKLOG_FRAME_INTERVAL = 0.05  # <= 20 fps while pane output is still queued: drawing must not starve intake
INTAKE_SECONDS = 0.02  # max time spent draining the socket per loop iteration (own budget, not shared with feed)


class ConnectionLost(Exception):
    """A send to the backend failed (EPIPE/ECONNRESET/timeout): the backend dropped this UI."""


class Terminate(BaseException):
    """SIGTERM/SIGHUP: unwind so the terminal is restored on the way out."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class ClientSender:
    def __init__(self, client: UiClient):
        self.client = client

    def send(self, kind: ClientType, payload: bytes = b"", **fields: Any) -> str:
        request_id = uuid4().hex[:16]
        try:
            self.client.send_frame(ui_v1.request(kind, request_id, **fields), payload)
        except OSError as exc:
            raise ConnectionLost(str(exc) or type(exc).__name__) from exc
        return request_id


def _safe_write(fd: int, data: bytes) -> None:
    try:
        _write_all(fd, data)
    except OSError:
        pass  # terminal already gone (SIGHUP)


def drain_socket(client: UiClient) -> bool:
    """Read what the backend already sent (bounded time) so its outbound buffer never fills. False on EOF."""
    deadline = time.monotonic() + INTAKE_SECONDS
    while True:
        if not client.pump(0):
            return False
        if time.monotonic() >= deadline or not select.select([client.sock], [], [], 0)[0]:
            return True


def connection_lost(client: UiClient, model: ProductModel, error: str) -> None:
    """After a failed send: surface the backend's CLOSING reason (e.g. slow_client) if it is readable."""
    end = time.monotonic() + 1.0
    while not model.closed_reason and time.monotonic() < end:
        try:
            alive = client.pump(0.1)
        except OSError:
            break
        dispatch_frames(client, model)
        if not alive:
            break
    if not model.closed_reason:
        model.closed_reason = "connection_lost"
        model.notice = f"backend 연결이 끊김 ({error})"


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


def persist_layout(model: ProductModel, layout_path: Path | None) -> None:
    """Write the split ratios / zoom next to the UI socket once a change is complete (not while dragging)."""
    if layout_path is not None and model.layout_dirty and not model.drag_active:
        save_layout(layout_path, *model.take_layout_state())


def loop(win: "curses.window", client: UiClient, model: ProductModel, stdin_fd: int, stdout_fd: int = 1,
         layout_path: Path | None = None) -> int:
    curses.noecho()
    curses.raw()
    win.keypad(False)
    win.nodelay(True)
    resized = {"flag": False}
    previous = signal.signal(signal.SIGWINCH, lambda *_: resized.__setitem__("flag", True))
    colors = _ColorPairs()
    dirty, last_draw, last_snapshot = True, 0.0, time.monotonic()
    status = 0
    client.on_display = model.enqueue_display  # cheap: pyte is fed in bounded slices below
    drag_on = False  # a divider drag is in progress (redraw on change; the mouse mode is never switched for it)
    try:
        # Enabled inside the protected region: a SIGTERM/SIGHUP (Terminate) landing right after the write still
        # unwinds through the ``?2004l`` below, so bracketed paste is never left on in the user's terminal.
        _safe_write(stdout_fd, b"\x1b[?2004h")
        mouse_on = model.mouse_capture
        if mouse_on:
            _safe_write(stdout_fd, MOUSE_ON)
        while True:
            try:
                if resized["flag"]:
                    resized["flag"] = False
                    rows, cols = _terminal_size(stdout_fd)
                    try:
                        curses.resizeterm(rows, cols)
                    except curses.error:
                        pass
                    win.clear()
                    model.resize(rows, cols)
                    if mouse_on:  # re-assert: a multiplexer (re)attach or a reset may have changed the mode
                        _safe_write(stdout_fd, MOUSE_ON)
                    dirty = True
                try:
                    ready = select.select([stdin_fd, client.sock], [], [], 0 if model.has_backlog() else 0.05)[0]
                except InterruptedError:
                    continue
                if client.sock in ready:
                    try:
                        alive = drain_socket(client)
                    except ui_v1.ProtocolError as exc:  # corrupt stream: report it, never a traceback
                        model.closed_reason = "protocol_error"
                        model.notice = f"backend 스트림 오류로 연결을 닫음 ({exc}) — 다시 attach"
                        alive = False
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
                    try:
                        data = os.read(stdin_fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    model.handle_input(data)
                    dirty = True
                elif model.flush_input():
                    dirty = True  # e.g. a lone Esc that closed the help overlay
                if model.mouse_capture != mouse_on:  # prefix m
                    mouse_on = model.mouse_capture
                    _safe_write(stdout_fd, MOUSE_ON if mouse_on else MOUSE_OFF)
                elif model.take_mouse_reassert() and mouse_on:  # prefix r: also repair the outer mouse mode
                    _safe_write(stdout_fd, MOUSE_ON)
                model.flush_resizes()  # trailing resize frames of a divider drag (debounced)
                if model.drag_active != drag_on:
                    drag_on = model.drag_active
                    dirty = True
                persist_layout(model, layout_path)
                if model.quit:
                    break
                if model.has_backlog():
                    model.feed_pending()
                    dirty = True
                now = time.monotonic()
                if now - last_snapshot >= SNAPSHOT_INTERVAL:
                    last_snapshot = now
                    model._send(ClientType.SNAPSHOT)
                    dirty = True  # refresh "마지막 확인" age
                interval = BACKLOG_FRAME_INTERVAL if model.has_backlog() else FRAME_INTERVAL
                if dirty and now - last_draw >= interval:
                    draw(win, model, colors)
                    dirty, last_draw = False, now
            except ConnectionLost as exc:
                connection_lost(client, model, str(exc))
                status = 1
                break
    finally:
        # every exit path (detach, signal, connection loss, exception): mouse reporting and bracketed paste off
        _safe_write(stdout_fd, MOUSE_OFF + b"\x1b[?2004l")
        signal.signal(signal.SIGWINCH, previous)
        model.abort_drag()
        persist_layout(model, layout_path)
    return status


def run_product(path: Path, *, stdin_fd: int = 0, stdout_fd: int = 1) -> int:
    """Attach the product UI. 0 on detach, 1 when the backend closed/rejected/error, 2 without a TTY."""
    if not os.isatty(stdin_fd) or not os.isatty(stdout_fd):
        print("attach needs an interactive terminal (stdin/stdout must be a TTY); "
              "scripts can use `status --json`", file=sys.stderr)
        return 2
    client = UiClient(path, name="workbench-ui")
    rows, cols = _terminal_size(stdout_fd)
    model = ProductModel(ClientSender(client), rows, cols)
    layout_path = path.parent / LAYOUT_FILE  # per data dir: next to the UI socket
    model.set_layout_state(*load_layout(layout_path))
    status, message = 0, ""
    saved = termios.tcgetattr(stdin_fd)
    try:
        try:
            attached = client.attach(model.sizes[model.focus] if not model.too_small() else None)
        except ClientError as exc:
            print(f"attach refused: {exc.header.get('reason', exc)}", file=sys.stderr)
            return 1
        if not attached.get("ok"):
            print(f"attach refused: {attached.get('reason')}: {attached.get('detail')}", file=sys.stderr)
            return 1
        model.attach_done(attached["snapshot"])
        dispatch_frames(client, model)  # replay that arrived with the result
        client.on_display = model.on_display
        try:
            model.after_attach()
        except ConnectionLost as exc:
            connection_lost(client, model, str(exc))
            print(f"[workbench] {model.notice}", file=sys.stderr)
            return 1

        def on_signal(signum: int, _frame: Any) -> None:
            raise Terminate(signum)

        previous_handlers = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGHUP)}
        try:
            status = curses.wrapper(loop, client, model, stdin_fd, stdout_fd, layout_path)
        except Terminate as term:
            status, message = 1, f"signal {signal.Signals(term.signum).name}로 종료됨 (backend는 계속 실행)"
        except ConnectionLost as exc:  # a send failed outside the loop body
            connection_lost(client, model, str(exc))
            status = 1
        except Exception:
            status, message = 1, "ui error:\n" + traceback.format_exc()
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
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
        _safe_write(stdout_fd, b"[workbench] detached; backend keeps running\r\n")
    else:
        _safe_write(stdout_fd, f"[workbench] {message}\r\n".encode())
    return status
