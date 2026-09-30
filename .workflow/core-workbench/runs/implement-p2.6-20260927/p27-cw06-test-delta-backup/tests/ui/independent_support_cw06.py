"""Independent CW-06 test support (p27-cw06-test-01): no real OMP, no credentials.

- ``RecordingSender``: captures what the pure model sends.
- ``ScriptedServer``: a threaded ui_v1 server on an owned private UDS that
  records every frame, answers requests from a per-type script, and lets a
  test push display/state/closing frames or drop the connection.
- ``UiPty``: runs ``run_product`` (the real product UI loop) on a PTY this
  test owns, under ``/bin/sh`` so the outer terminal state after the UI exits
  can be captured with ``stty -g``; output is drained continuously and can be
  rendered with the repo's VT screen to check what the user actually sees.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from typing import Any, Callable
import uuid

from workbench.contracts import ui_v1
from workbench.contracts.v1 import DisplayChunk, PaneId
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
SID = str(uuid.uuid4())


def snap(*, owner: str = "user", mode: str = "manual_prompt", focus: str = "manager_omp",
         phase: str = "ready", automation: str = "idle") -> dict[str, Any]:
    panes: dict[str, Any] = {}
    for name in ("manager_omp", "worker_omp", "host_shell"):
        panes[name] = {"pane": name, "alive": True, "exit_status": None, "input_owner": "user"}
    panes["host_shell"]["input_owner"] = owner
    panes["host_shell"]["shell"] = {"kind": "bash", "input_owner": owner, "parent_mode": mode, "phase": "idle"}
    return {"contract": {"name": "workbench.ui", "version": 1}, "phase": phase, "focus": focus, "panes": panes,
            "bridge": {"manager": {"connected": True}, "worker": {"connected": True}},
            "automation": {"state": automation}, "backend": {"pid": 4242, "data_dir": "/fixture"}}


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], bytes, str]] = []
        self._n = 0

    def send(self, kind, payload: bytes = b"", **fields: Any) -> str:
        self._n += 1
        rid = f"q{self._n}"
        self.sent.append((kind.value, dict(fields), bytes(payload), rid))
        return rid

    def of(self, kind: str) -> list[tuple[str, dict[str, Any], bytes, str]]:
        return [item for item in self.sent if item[0] == kind]

    def payloads(self, kind: str = "input", pane: str | None = None) -> bytes:
        return b"".join(p for k, f, p, _ in self.sent if k == kind and (pane is None or f.get("pane") == pane))


Answer = Callable[[dict[str, Any], bytes], dict[str, Any] | None]


class ScriptedServer:
    """Single-connection ui_v1 fixture server in an owned 0700 temp dir."""

    def __init__(self, *, snapshot: dict[str, Any] | None = None, replay: dict[str, bytes] | None = None,
                 answers: dict[str, Answer] | None = None):
        self.root = Path(tempfile.mkdtemp(prefix="cw06-indep-", dir="/tmp"))
        os.chmod(self.root, 0o700)
        self.path = self.root / "ui.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(2)
        self.snapshot = snapshot or snap()
        self.replay = replay or {}
        self.answers = answers or {}
        self.frames: list[ui_v1.Frame] = []
        self.conn: socket.socket | None = None
        self.attached = threading.Event()
        self.gone = threading.Event()
        self._stop = False
        self._lock = threading.Lock()
        self._seq = 100
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    # -- sending ---------------------------------------------------------
    def send(self, header: dict[str, Any], payload: bytes = b"") -> None:
        with self._lock:
            if self.conn is not None:
                try:
                    self.conn.sendall(ui_v1.encode_frame(header, payload))
                except OSError:
                    pass

    def display(self, pane: str, data: bytes, *, replay: bool = False) -> None:
        self._seq += 1
        chunk = DisplayChunk(session_id=SID, session_generation=1, pane_id=PaneId(pane), sequence=self._seq,
                             data=data)
        with self._lock:
            if self.conn is not None:
                try:
                    self.conn.sendall(ui_v1.encode_display(chunk, replay=replay))
                except OSError:
                    pass

    def state(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        self.send({"v": 1, "type": "state", "snapshot": snapshot})

    def closing(self, reason: str = "backend_shutdown") -> None:
        self.send({"v": 1, "type": "closing", "reason": reason})

    def drop(self) -> None:
        with self._lock:
            if self.conn is not None:
                try:
                    self.conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    # -- serving ---------------------------------------------------------
    def _serve(self) -> None:
        decoder = ui_v1.FrameDecoder()
        while not self._stop:
            try:
                if not select.select([self.listener], [], [], 0.05)[0]:
                    continue
                conn, _ = self.listener.accept()
            except (OSError, ValueError):
                self.gone.set()
                return
            self.conn = conn
            break
        while not self._stop and self.conn is not None:
            try:
                if not select.select([self.conn], [], [], 0.05)[0]:
                    continue
                data = self.conn.recv(1 << 20)
            except (OSError, ValueError):
                data = b""
            if not data:
                break
            for frame in decoder.feed(data):
                self.frames.append(frame)
                self._answer(frame)
        self.gone.set()

    def _answer(self, frame: ui_v1.Frame) -> None:
        header = frame.header
        kind = header.get("type")
        if kind == "hello":
            self.send({"v": 1, "type": "welcome", "contract": "workbench.ui", "version": 1, "backend_pid": 4242})
            return
        rid = header.get("id")
        custom = self.answers.get(kind)
        if custom is not None:
            fields = custom(header, frame.payload)
            if fields is not None:
                ok = fields.pop("ok", True)
                reason = fields.pop("reason", None)
                self.send(ui_v1.result(rid, ok, reason=ui_v1.Reason(reason) if reason else None, **fields))
                return
        if kind == "attach":
            self.send(ui_v1.result(rid, True, snapshot=self.snapshot))
            for pane, data in self.replay.items():
                self.display(pane, data, replay=True)
            self.attached.set()
        elif kind == "snapshot":
            self.send(ui_v1.result(rid, True, snapshot=self.snapshot))
        elif kind == "detach":
            self.send(ui_v1.result(rid, True, detached=True))
        elif kind in {"input", "paste"}:
            self.send(ui_v1.result(rid, True, pane=header.get("pane"), accepted_bytes=len(frame.payload)))
        elif kind == "focus":
            self.snapshot = dict(self.snapshot, focus=header.get("pane"))
            self.send(ui_v1.result(rid, True, focus=header.get("pane")))
        else:
            self.send(ui_v1.result(rid, True))

    # -- inspection ------------------------------------------------------
    def of(self, kind: str) -> list[ui_v1.Frame]:
        return [f for f in list(self.frames) if f.header.get("type") == kind]

    def payloads(self, kind: str = "input", pane: str | None = None) -> bytes:
        return b"".join(f.payload for f in self.of(kind) if pane is None or f.header.get("pane") == pane)

    def wait(self, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def close(self) -> None:
        self._stop = True
        for sock in (self.conn, self.listener):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
        self.thread.join(2)
        shutil.rmtree(self.root, ignore_errors=True)


_UI_BOOT = ("import sys; sys.path.insert(0, {src!r}); from pathlib import Path; "
            "from workbench.ui.product import run_product; raise SystemExit(run_product(Path({sock!r})))")


def _set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class UiPty:
    """The real product UI loop on an owned PTY, wrapped by /bin/sh to capture post-exit ``stty -g``."""

    def __init__(self, sock: Path, workdir: Path, *, rows: int = 30, cols: int = 120):
        self.rows, self.cols = rows, cols
        self.before_file = workdir / "stty-before"
        self.after_file = workdir / "stty-after"
        self.status_file = workdir / "ui-status"
        boot = _UI_BOOT.format(src=str(SRC), sock=str(sock))
        script = (f'stty -g > "{self.before_file}"; "{sys.executable}" -c "$WB_BOOT"; '
                  f'echo $? > "{self.status_file}"; stty -g > "{self.after_file}"; printf "\\n__SH_DONE__\\n"; '
                  'exec sleep 30')
        master, slave = os.openpty()
        _set_size(slave, rows, cols)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TMUX", "HERDR_", "WORKBENCH_"))}
        env.update(TERM="xterm-256color", LANG="C.UTF-8", WB_BOOT=boot, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        self.process = subprocess.Popen(["/usr/bin/setsid", "--ctty", "/bin/sh", "-c", script], stdin=slave,
                                        stdout=slave, stderr=slave, close_fds=True, env=env, cwd=str(workdir))
        os.close(slave)
        self.fd = master
        self.output = bytearray()

    def drain(self, timeout: float = 0.05) -> None:
        if self.fd < 0:
            return
        try:
            while select.select([self.fd], [], [], timeout)[0]:
                data = os.read(self.fd, 65536)
                if not data:
                    break
                self.output.extend(data)
                timeout = 0
        except OSError:
            pass

    def send(self, data: bytes, chunk: int = 4096) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.fd, view[:chunk])
            except BlockingIOError:
                written = 0
            view = view[written:]
            self.drain(0)

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain(0.03)
            if predicate():
                return True
        self.drain(0)
        return predicate()

    def wait_text(self, needle: str, timeout: float = 5.0) -> bool:
        return self.wait_for(lambda: needle in self.screen_text(), timeout)

    def ui_done(self, timeout: float = 10.0) -> bool:
        return self.wait_for(lambda: b"__SH_DONE__" in self.output, timeout)

    def ui_pid(self) -> int | None:
        """The python child of the wrapper shell (the product UI process)."""
        try:
            children = Path(f"/proc/{self.process.pid}/task/{self.process.pid}/children").read_text().split()
        except OSError:
            return None
        return int(children[0]) if children else None

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        _set_size(self.fd, rows, cols)  # the kernel sends SIGWINCH to the foreground process group

    def screen_text(self) -> str:
        screen = TerminalScreen(self.cols, self.rows)
        stream = make_stream(screen)
        stream.feed(bytes(self.output))
        return "\n".join(screen.display)

    def status(self) -> int | None:
        try:
            return int(self.status_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)  # own session (setsid) only
            except OSError:
                pass
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                pass
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def modes_restored(output: bytes) -> dict[str, bool]:
    """Private modes that a clean exit must turn off again after turning them on."""
    def last(seq: bytes) -> int:
        return output.rfind(seq)
    checks = {}
    for name, on, off in (("alt_screen", b"\x1b[?1049h", b"\x1b[?1049l"),
                          ("bracketed_paste", b"\x1b[?2004h", b"\x1b[?2004l")):
        checks[name] = last(on) < 0 or last(off) > last(on)
    hidden, shown = last(b"\x1b[?25l"), last(b"\x1b[?25h")
    checks["cursor_visible"] = hidden < 0 or shown > hidden
    return checks
