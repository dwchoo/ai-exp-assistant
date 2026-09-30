"""Fixture ui_v1 server and helpers for product UI tests (no real OMP, no credentials)."""
from __future__ import annotations

import os
from pathlib import Path
import select
import shutil
import socket
import tempfile
import threading
import time

import uuid

from workbench.contracts import ui_v1
from workbench.contracts.v1 import DisplayChunk, PaneId


def snapshot(*, owner="user", mode="user_control", phase="ready", focus="manager_omp", automation="idle",
             alive=("manager_omp", "worker_omp", "host_shell")):
    panes = {}
    for name in ("manager_omp", "worker_omp", "host_shell"):
        info = {"pane": name, "alive": name in alive, "exit_status": None if name in alive else 1,
                "input_owner": "user"}
        if name == "host_shell":
            info["input_owner"] = owner
            info["shell"] = {"kind": "bash", "input_owner": owner, "parent_mode": mode, "phase": "ready"}
        panes[name] = info
    return {"contract": {"name": "workbench.ui", "version": 1}, "phase": phase, "focus": focus, "panes": panes,
            "bridge": {"manager": {"connected": True}, "worker": {"connected": False}},
            "automation": {"state": automation}, "backend": {"pid": 1, "data_dir": "/x"}}


SID = str(uuid.uuid4())


class FixtureServer:
    """One-connection ui_v1 server on a private UDS in an owned temp dir."""

    def __init__(self, *, replay: dict[str, bytes] | None = None, reject: dict[str, tuple[str, str]] | None = None,
                 snap=None):
        self.root = Path(tempfile.mkdtemp(prefix="cw06-ui-", dir="/tmp"))
        self.path = self.root / "ui.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(1)
        self.replay = replay or {}
        self.reject = reject or {}
        self.snap = snap or snapshot()
        self.frames: list[ui_v1.Frame] = []
        self.conn: socket.socket | None = None
        self.attached = threading.Event()
        self.disconnected = threading.Event()
        self._stop = False
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _send(self, header, payload=b""):
        with self._lock:
            if self.conn is not None:
                try:
                    self.conn.sendall(ui_v1.encode_frame(header, payload))
                except OSError:
                    pass

    def _serve(self):
        decoder = ui_v1.FrameDecoder()
        listener = self.listener
        while not self._stop:
            try:
                if not select.select([listener], [], [], 0.05)[0]:
                    continue
                conn, _ = listener.accept()
            except (OSError, ValueError):
                self.disconnected.set()
                return
            self.conn = conn
            break
        while not self._stop and self.conn is not None:
            if not select.select([self.conn], [], [], 0.05)[0]:
                continue
            try:
                data = self.conn.recv(1 << 20)
            except OSError:
                data = b""
            if not data:
                break
            for frame in decoder.feed(data):
                self.frames.append(frame)
                self._answer(frame)
        self.disconnected.set()

    def _answer(self, frame):
        header = frame.header
        kind = header.get("type")
        if kind == "hello":
            self._send({"v": 1, "type": "welcome", "contract": "workbench.ui", "version": 1, "backend_pid": 1})
            return
        rid = header.get("id")
        if kind in self.reject:
            reason, detail = self.reject[kind]
            self._send(ui_v1.result(rid, False, reason=ui_v1.Reason(reason), detail=detail))
            return
        if kind == "attach":
            self._send(ui_v1.result(rid, True, snapshot=self.snap))
            for pane, data in self.replay.items():
                with self._lock:
                    self.conn.sendall(ui_v1.encode_display(
                        DisplayChunk(session_id=SID, session_generation=1, pane_id=PaneId(pane), sequence=1,
                                     data=data), replay=True))
            self.attached.set()
        elif kind == "snapshot":
            self._send(ui_v1.result(rid, True, snapshot=self.snap))
        elif kind == "detach":
            self._send(ui_v1.result(rid, True, detached=True))
        elif kind == "takeover_request":
            self._send(ui_v1.result(rid, True, shell={"input_owner": "user", "parent_mode": "user_control"}))
        else:
            self._send(ui_v1.result(rid, True))

    def display(self, pane: str, data: bytes, seq=2):
        self._send(dict(v=1, type="display", pane=pane, session_id=SID, generation=1, sequence=seq), data)

    def state(self, snap):
        self._send({"v": 1, "type": "state", "snapshot": snap})

    def closing(self, reason="backend_shutdown"):
        self._send({"v": 1, "type": "closing", "reason": reason})

    def received(self, kind):
        return [f for f in list(self.frames) if f.header.get("type") == kind]

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def close(self):
        self._stop = True
        for sock in (self.conn, self.listener):
            try:
                if sock:
                    sock.close()
            except OSError:
                pass
        self.thread.join(2)
        shutil.rmtree(self.root, ignore_errors=True)


class FakeSender:
    def __init__(self):
        self.sent = []
        self.count = 0

    def send(self, kind, payload=b"", **fields):
        self.count += 1
        self.sent.append((kind.value, dict(fields), payload))
        return f"r{self.count}"

    def of(self, kind):
        return [s for s in self.sent if s[0] == kind]


class OuterMouse:
    """What a real outer terminal (xterm/VTE/tmux/herdr) does with the UI's mouse DECSET/DECRST.

    1000/1002/1003 are ONE tracking-mode state there: setting any of them replaces the current mode and
    resetting any of them turns tracking off (xterm ``send_mouse_pos``, tmux ``ALL_MOUSE_MODES``). 1006 (SGR)
    is a separate encoding flag. ``report`` returns the bytes such a terminal would send for a mouse action,
    or None when it would send nothing (the event never reaches the UI).
    """

    _MODE = __import__("re").compile(rb"\x1b\[\?([0-9;]*)([hl])")

    def __init__(self):
        self.tracking = None  # None, 1000 (press/release/wheel), 1002 (+ motion while pressed), 1003 (+ any motion)
        self.sgr = False
        self._tail = b""

    def feed(self, data: bytes) -> None:
        data = self._tail + bytes(data)
        for match in self._MODE.finditer(data):
            enable = match.group(2) == b"h"
            for param in match.group(1).split(b";"):
                if param in (b"1000", b"1002", b"1003"):
                    self.tracking = int(param) if enable else None
                elif param == b"1006":
                    self.sgr = enable
        at = data.rfind(b"\x1b")
        self._tail = data[at:] if at >= 0 and len(data) - at < 16 and not self._MODE.match(data, at) else b""

    def report(self, button: int, x: int, y: int, release: bool = False):
        if self.tracking is None:
            return None
        if button & 32:  # motion
            if self.tracking == 1000 or (button & 3 == 3 and self.tracking != 1003):
                return None
        if self.sgr:
            return b"\x1b[<%d;%d;%d%s" % (button, x, y, b"m" if release else b"M")
        code = 3 if release and not button & 64 else button
        return b"\x1b[M" + bytes((32 + code, 32 + x, 32 + y))


def rep_screen_classes():
    """pyte Screen/ByteStream that also apply what ncurses sends for xterm-256color and plain pyte ignores:
    REP (CSI Ps b: repeat the last graphic char) and SU/SD (CSI Ps S / CSI Ps T: scroll the region). Without
    them pyte keeps stale cells, so a test would read a screen the real terminal never shows."""
    import pyte

    class RepScreen(pyte.Screen):
        _last = " "

        def draw(self, data):
            if data:
                self._last = data[-1]
            super().draw(data)

        def repeat_last(self, count=1, *args, **kwargs):
            super().draw(self._last * max(1, count))

        def _region(self):
            return (self.margins.top, self.margins.bottom) if self.margins else (0, self.lines - 1)

        def scroll_region_up(self, count=1, *args, **kwargs):
            top, bottom = self._region()
            for _ in range(max(1, count)):
                for y in range(top, bottom):
                    self.buffer[y] = self.buffer[y + 1]
                self.buffer.pop(bottom, None)
            self.dirty.update(range(self.lines))

        def scroll_region_down(self, count=1, *args, **kwargs):
            top, bottom = self._region()
            for _ in range(max(1, count)):
                for y in range(bottom, top, -1):
                    self.buffer[y] = self.buffer[y - 1]
                self.buffer.pop(top, None)
            self.dirty.update(range(self.lines))

    class RepStream(pyte.ByteStream):
        csi = {**pyte.ByteStream.csi, "b": "repeat_last", "S": "scroll_region_up", "T": "scroll_region_down"}

    return RepScreen, RepStream
