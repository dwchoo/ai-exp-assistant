"""Pure product-UI state: pane screens, backend state, key commands. No curses, no sockets."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import re
import time
from typing import Any, Protocol

from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import PaneId
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.ui.product.input import (PASTE_END, PASTE_START, Command, InputParser, Paste, PasteRejected,
                                       Passthrough)

_PRIVATE_MODE = re.compile(rb"\x1b\[\?([0-9;]*)([hl])")
_PARTIAL_MODE = re.compile(rb"\x1b(?:\[(?:\?[0-9;]*)?)?$")

PANES = (PaneId.MANAGER_OMP, PaneId.WORKER_OMP, PaneId.HOST_SHELL)
TITLES = {PaneId.MANAGER_OMP: "MANAGER OMP", PaneId.WORKER_OMP: "WORKER OMP", PaneId.HOST_SHELL: "HOST SHELL"}
HEADER_ROWS, FOOTER_ROWS = 2, 1
MIN_ROWS, MIN_COLS = 8, 30
SCROLLBACK = 1000
FEED_CHUNK_BYTES = 16 * 1024  # one pyte feed step
FEED_SLICE_BYTES = 64 * 1024  # pyte work per loop iteration (bytes) ...
FEED_SLICE_SECONDS = 0.02  # ... or seconds, whichever comes first
CATCHUP_BACKLOG_BYTES = 256 * 1024  # unfed backlog per pane that triggers UI catch-up
CATCHUP_KEEP_BYTES = 64 * 1024  # newest bytes kept (and fed) when catching up
CATCHUP_NOTE_SECONDS = 10.0

# Provisional key choices (UR-UX will review). Shown only in the help overlay.
HELP_LINES = (
    "OMP Workbench 키 (임시 배치 — 사용자 UX 검토 대상)",
    "",
    "prefix = Ctrl-]   (그 외 모든 키는 focus된 pane에 원본 그대로 전달)",
    "  prefix prefix   prefix 바이트(0x1d)를 그대로 전송",
    "  prefix 1/2/3    manager OMP / worker OMP / host shell로 focus",
    "  prefix Tab      다음 pane으로 focus",
    "  prefix t        host shell 사용자 인수 요청",
    "  prefix c        인수 확인 (요청 후)",
    "  prefix h        host shell을 manager에게 되돌리기(handoff)",
    "  prefix r        focus pane 다시 그리기 요청",
    "  prefix d        detach (backend와 PTY는 계속 실행)",
    "  prefix ?        이 도움말 (아무 키로 닫기)",
    "",
    "focus 변경은 host 입력 owner를 바꾸지 않는다.",
)


def _identity(frame: ui_v1.Frame) -> tuple[Any, Any]:
    return (frame.header.get("session_id"), frame.header.get("generation"))


class Sender(Protocol):
    def send(self, kind: ClientType, payload: bytes = b"", **fields: Any) -> str: ...


def pane_inner_sizes(rows: int, cols: int) -> dict[PaneId, tuple[int, int]]:
    """(rows, cols) of each pane's terminal area inside its border."""
    height = max(1, rows - HEADER_ROWS - FOOTER_ROWS - 2)
    base = max(1, cols // 3)
    widths = (base, base, max(1, cols - 2 * base))
    return {pane: (height, max(1, width - 2)) for pane, width in zip(PANES, widths)}


@dataclass
class PaneView:
    screen: TerminalScreen
    stream: Any
    identity: tuple[Any, Any] | None = None
    bracketed: bool | None = None  # last observed DECSET/DECRST 2004; None = never seen
    tail: bytes = b""  # possibly split private-mode sequence at the end of the previous chunk
    backlog: deque = field(default_factory=deque)  # (identity, replay, payload) received but not yet fed
    backlog_bytes: int = 0
    skipped_bytes: int = 0  # total bytes the UI skipped while catching up
    catchup_until: float = 0.0  # monotonic time until which the indicator is shown


class ProductModel:
    def __init__(self, sender: Sender, rows: int, cols: int, *, clock: Callable[[], float] = time.time):
        self.sender = sender
        self.clock = clock
        self.rows, self.cols = rows, cols
        self.focus = PaneId.MANAGER_OMP
        self.state: dict[str, Any] = {}
        self.last_seen: float | None = None
        self.notice = ""
        self.help_open = False
        self.quit = False
        self.closed_reason: str | None = None
        self.parser = InputParser()
        self.pending: dict[str, tuple[ClientType, PaneId | None]] = {}
        self._replaying = False
        self.sizes = pane_inner_sizes(rows, cols)
        self.panes: dict[PaneId, PaneView] = {}
        for pane in PANES:
            r, c = self.sizes[pane]
            screen = TerminalScreen(c, r, history=SCROLLBACK,
                                    reply=lambda data, pane=pane: self._query_reply(pane, data))
            self.panes[pane] = PaneView(screen, make_stream(screen))

    # -- sending ---------------------------------------------------------
    def _send(self, kind: ClientType, payload: bytes = b"", pane: PaneId | None = None, **fields: Any) -> str:
        if pane is not None:
            fields["pane"] = pane.value
        request_id = self.sender.send(kind, payload, **fields)
        self.pending[request_id] = (kind, pane)
        return request_id

    def _query_reply(self, pane: PaneId, data: bytes) -> None:
        # Terminal query answers (DSR/DA) go to the pane like a real terminal would.
        # Replayed history must not re-answer old queries (no duplicate input).
        if self._replaying or not data:
            return
        request_id = self._send(ClientType.INPUT, data, pane)
        self.pending[request_id] = (ClientType.INPUT, None)  # quiet: refusals are not shown

    # -- backend frames --------------------------------------------------
    def touch(self) -> None:
        self.last_seen = self.clock()

    def apply_snapshot(self, snapshot: dict[str, Any], *, adopt_focus: bool = False) -> None:
        self.state = snapshot
        self.touch()
        if adopt_focus or not any(k is ClientType.FOCUS for k, _ in self.pending.values()):
            try:
                self.focus = PaneId(snapshot.get("focus", self.focus.value))
            except ValueError:
                pass

    def attach_done(self, snapshot: dict[str, Any]) -> None:
        self.apply_snapshot(snapshot, adopt_focus=True)

    def on_display(self, frame: ui_v1.Frame) -> None:
        """Synchronous path (attach replay, tests): feed the frame now."""
        pane = self._display_pane(frame)
        if pane is None:
            return
        self._apply(self.panes[pane], _identity(frame), bool(frame.header.get("replay")), frame.payload)

    def _display_pane(self, frame: ui_v1.Frame) -> PaneId | None:
        self.touch()
        try:
            return PaneId(frame.header.get("pane"))
        except ValueError:
            return None

    def _apply(self, view: PaneView, identity: tuple[Any, Any], replay: bool, payload: bytes) -> None:
        if view.identity is not None and identity != view.identity:
            view.screen.reset()  # the pane's process/session was replaced
            view.bracketed, view.tail = None, b""
        view.identity = identity
        self._replaying = replay
        try:
            self._track_bracketed(view, payload)
            view.stream.feed(payload)
        finally:
            self._replaying = False

    # -- decoupled intake: drain the socket fast, feed pyte in bounded slices ----
    def enqueue_display(self, frame: ui_v1.Frame) -> None:
        pane = self._display_pane(frame)
        if pane is None or not frame.payload:
            return
        view = self.panes[pane]
        view.backlog.append((_identity(frame), bool(frame.header.get("replay")), frame.payload))
        view.backlog_bytes += len(frame.payload)

    def has_backlog(self) -> bool:
        return any(view.backlog for view in self.panes.values())

    def feed_pending(self, *, max_bytes: int = FEED_SLICE_BYTES, max_seconds: float = FEED_SLICE_SECONDS) -> bool:
        """Feed queued output for at most ``max_bytes``/``max_seconds``; True while a backlog remains."""
        for pane in PANES:
            if self.panes[pane].backlog_bytes > CATCHUP_BACKLOG_BYTES:
                self._catch_up(pane)
        deadline = time.monotonic() + max_seconds
        fed, progress = 0, True
        while progress and fed < max_bytes and time.monotonic() < deadline:
            progress = False
            for pane in PANES:
                view = self.panes[pane]
                if not view.backlog:
                    continue
                identity, replay, payload = view.backlog[0]
                chunk = payload[:FEED_CHUNK_BYTES]
                if len(payload) > FEED_CHUNK_BYTES:
                    view.backlog[0] = (identity, replay, payload[FEED_CHUNK_BYTES:])
                else:
                    view.backlog.popleft()
                view.backlog_bytes -= len(chunk)
                self._apply(view, identity, replay, chunk)
                fed += len(chunk)
                progress = True
        return self.has_backlog()

    def _catch_up(self, pane: PaneId) -> None:
        """Drop the older backlog, reset this pane's screen and feed only the newest tail.

        The display is a view: backend state and its retained tail are untouched. The pane
        application is asked to repaint through the usual resize nudge.
        """
        view = self.panes[pane]
        items = list(view.backlog)
        total = view.backlog_bytes
        start, previous = 0, view.identity
        for index, (identity, _, _) in enumerate(items):
            if previous is not None and identity != previous:
                start = index  # keep only what belongs to the newest session
            previous = identity
        for _, _, payload in items[:start]:
            self._track_bracketed(view, payload)
        segment = b"".join(payload for _, _, payload in items[start:])
        tail = segment[-CATCHUP_KEEP_BYTES:]
        while tail and tail[0] & 0xC0 == 0x80:
            tail = tail[1:]  # do not start in the middle of a UTF-8 character
        self._track_bracketed(view, segment[:len(segment) - len(tail)])
        view.tail = b""
        view.screen.reset()
        view.stream = make_stream(view.screen)
        view.backlog = deque([(items[-1][0], True, tail)])  # replay=True: stale queries are not answered
        view.backlog_bytes = len(tail)
        view.skipped_bytes += total - len(tail)
        view.catchup_until = time.monotonic() + CATCHUP_NOTE_SECONDS
        self.notice = f"[{TITLES[pane]}] 출력 따라잡음: {total - len(tail)} bytes 건너뜀"
        self.redraw(pane)

    def catching_up(self, pane: PaneId) -> bool:
        return time.monotonic() < self.panes[pane].catchup_until

    def on_state(self, snapshot: dict[str, Any]) -> None:
        self.apply_snapshot(snapshot)

    def on_result(self, header: dict[str, Any]) -> None:
        self.touch()
        kind, pane = self.pending.pop(header.get("id"), (None, None))
        if header.get("snapshot"):
            self.apply_snapshot(header["snapshot"])
        shell = header.get("shell")
        if header.get("ok") and isinstance(shell, dict):
            host = self.state.get("panes", {}).get(PaneId.HOST_SHELL.value)
            if isinstance(host, dict):
                host["shell"] = shell
                host["input_owner"] = shell.get("input_owner", host.get("input_owner"))
        if header.get("ok"):
            if kind is ClientType.TAKEOVER_REQUEST:
                if isinstance(shell, dict) and not shell.get("takeover_requested"):
                    owner = shell.get("input_owner") or self.input_owner()
                    self.notice = f"인수 요청이 기록되지 않음 — 이미 host 입력 owner={owner}"
                else:
                    self.notice = "인수 요청됨 — prefix c 로 확인"
            elif kind is ClientType.TAKEOVER_CONFIRM:
                self.notice = "인수 확인됨: host 입력 owner 변경"
            elif kind is ClientType.HANDOFF:
                self.notice = "host shell을 manager에게 되돌림"
            return
        if kind is ClientType.INPUT and pane is None:
            return  # terminal-query reply refused; not a user action
        what = {ClientType.PASTE: "붙여넣기", ClientType.INPUT: "입력"}.get(kind, str(kind.value if kind else "요청"))
        target = f" [{TITLES[pane]}]" if pane else ""
        text = f"{what} 거부{target}: {header.get('reason')}: {header.get('detail') or ''}".rstrip(": ")
        held = self._held_reasons(header)
        if held:
            text += f" (held: {', '.join(held)})"
        self.notice = text

    def _held_reasons(self, header: dict[str, Any]) -> list[str]:
        for source in (header.get("shell"), self.host_info().get("shell"), header):
            reasons = source.get("held_reasons") if isinstance(source, dict) else None
            if isinstance(reasons, (list, tuple)) and reasons:
                return [str(r) for r in reasons]
        return []

    def on_closing(self, header: dict[str, Any]) -> None:
        self.closed_reason = str(header.get("reason") or "closed")
        self.notice = f"backend가 연결을 닫음: {self.closed_reason}"

    # -- user input ------------------------------------------------------
    def handle_input(self, data: bytes, *, now: float | None = None) -> None:
        self._handle_events(self.parser.feed(data, now=now))

    def flush_input(self, *, now: float | None = None) -> None:
        self._handle_events(self.parser.flush(now=now))

    @staticmethod
    def _track_bracketed(view: PaneView, payload: bytes) -> None:
        data = view.tail + payload
        for match in _PRIVATE_MODE.finditer(data):
            if b"2004" in match.group(1).split(b";"):
                view.bracketed = match.group(2) == b"h"
        partial = _PARTIAL_MODE.search(data)
        view.tail = data[partial.start():] if partial else b""

    def _bracketed_paste(self, pane: PaneId) -> bool:
        # Strip the markers only once the pane's application has turned DECSET 2004 off;
        # a never-observed state keeps the whole frame (fail-safe for the backend paste contract).
        # The sh (dash) fallback host shell never supports it: strip unless 2004h was observed.
        bracketed = self.panes[pane].bracketed
        if pane is PaneId.HOST_SHELL and (self.host_info().get("shell") or {}).get("kind") == "sh":
            return bracketed is True
        return bracketed is not False

    def _handle_events(self, events: list) -> None:
        for event in events:
            if isinstance(event, Command):
                self.help_open = False
                self._command(event.key)
            elif isinstance(event, PasteRejected):
                self.help_open = False
                self.notice = f"붙여넣기 거부: paste_too_large: {event.reason}"
            elif isinstance(event, Paste):
                self.help_open = False  # the paste is delivered, not swallowed by the overlay
                data = event.data
                if not self._bracketed_paste(self.focus):
                    data = data[len(PASTE_START):-len(PASTE_END)]  # app did not enable DECSET 2004
                self._send(ClientType.PASTE, data, self.focus)
            elif self.help_open:
                self.help_open = False  # any other key just closes the overlay
            elif isinstance(event, Passthrough):
                self._send(ClientType.INPUT, event.data, self.focus)

    def _command(self, key: str) -> None:
        if key in "123":
            self.set_focus(PANES[int(key) - 1])
        elif key == "\t":
            self.set_focus(PANES[(PANES.index(self.focus) + 1) % len(PANES)])
        elif key in "dD":
            self.quit = True
        elif key in "tT":
            self._send(ClientType.TAKEOVER_REQUEST)
        elif key in "cC":
            self._send(ClientType.TAKEOVER_CONFIRM)
        elif key in "hH":
            self._send(ClientType.HANDOFF)
        elif key in "rR":
            self.redraw(self.focus)
        elif key == "?":
            self.help_open = True
        else:
            self.notice = f"알 수 없는 prefix 명령 {key!r} (prefix ? 도움말)"

    def set_focus(self, pane: PaneId) -> None:
        self.focus = pane
        self._send(ClientType.FOCUS, pane=pane)
        self.notice = ""

    # -- layout / resize -------------------------------------------------
    def too_small(self) -> bool:
        return self.rows < MIN_ROWS or self.cols < MIN_COLS

    def resize(self, rows: int, cols: int, *, force: bool = False) -> None:
        self.rows, self.cols = rows, cols
        if self.too_small():
            return
        sizes = pane_inner_sizes(rows, cols)
        for pane in PANES:
            r, c = sizes[pane]
            if force or sizes[pane] != self.sizes[pane]:
                self.panes[pane].screen.resize(lines=r, columns=c)
                self._send(ClientType.RESIZE, pane=pane, rows=r, cols=c)
        self.sizes = sizes

    def redraw(self, pane: PaneId) -> None:
        """Nudge the application's window size so it repaints at the real size."""
        r, c = self.sizes[pane]
        self._send(ClientType.RESIZE, pane=pane, rows=max(1, r - 1), cols=c)
        self._send(ClientType.RESIZE, pane=pane, rows=r, cols=c)

    def after_attach(self) -> None:
        for pane in PANES:
            r, c = self.sizes[pane]
            self._send(ClientType.RESIZE, pane=pane, rows=r, cols=c)
        for pane in PANES:
            self.redraw(pane)

    # -- status text -----------------------------------------------------
    def host_info(self) -> dict[str, Any]:
        return self.state.get("panes", {}).get(PaneId.HOST_SHELL.value) or {}

    def input_owner(self) -> str:
        owner = self.host_info().get("input_owner")
        return owner if owner in {"user", "manager"} else "unknown"

    def pane_status(self, pane: PaneId) -> str:
        info = self.state.get("panes", {}).get(pane.value)
        if not info:
            return "unknown"
        if info.get("alive"):
            return "alive"
        code = info.get("exit_status")
        return f"exited({code})" if code is not None else "exited"

    def pane_title(self, pane: PaneId) -> str:
        extra = ""
        if pane is PaneId.HOST_SHELL:
            extra = f" owner={self.input_owner()}"
        note = " 따라잡음" if self.catching_up(pane) else ""
        return f" {TITLES[pane]}{' *FOCUS*' if pane is self.focus else ''} {self.pane_status(pane)}{extra}{note} "

    def status_lines(self) -> tuple[str, str]:
        shell = self.host_info().get("shell") or {}
        mode = shell.get("parent_mode", "unknown")
        automation = (self.state.get("automation") or {}).get("state", "unknown")
        bridge = self.state.get("bridge") or {}

        def peer(role: str) -> str:
            value = bridge.get(role)
            return "?" if not isinstance(value, dict) else ("ok" if value.get("connected") else "down")

        seen = "없음"
        if self.last_seen is not None:
            age = max(0, int(self.clock() - self.last_seen))
            seen = f"{time.strftime('%H:%M:%S', time.localtime(self.last_seen))} ({age}s 전)"
        line1 = (f"focus: {TITLES[self.focus]} | host 입력 owner: {self.input_owner()} | "
                 f"shell mode: {mode} | 자동화: {automation}")
        line2 = (f"backend: {self.state.get('phase', 'unknown')} | bridge manager={peer('manager')} "
                 f"worker={peer('worker')} | 마지막 확인 {seen} | Ctrl-] ? 도움말")
        return line1, line2

    def footer(self) -> str:
        if self.closed_reason:
            return self.notice
        if self.parser.prefix_active:
            return "prefix… (1/2/3 Tab t c h r d ? , Ctrl-] 한 번 더 = 리터럴)"
        return self.notice or "원본 키는 focus pane으로 그대로 전달됩니다 · Ctrl-] d = detach"
