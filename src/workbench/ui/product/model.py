"""Pure product-UI state: pane screens, backend state, key commands. No curses, no sockets."""
from __future__ import annotations

import base64
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import os
import re
import time
from typing import Any, Protocol

from wcwidth import wcswidth

from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import PaneId
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.ui.product.input import (PASTE_END, PREFIX, PASTE_START, Command, InputParser, Mouse, PageScroll, Paste,
                                       PasteRejected, Passthrough, _sequence_length)
from workbench.ui.product.layout import (DEFAULT_LAYOUT, Layout, clamp_height, clamp_width, left_width,
                                         top_height)

_PRIVATE_MODE = re.compile(rb"\x1b\[\?([0-9;]*)([hl])")
_PARTIAL_MODE = re.compile(rb"\x1b(?:\[(?:\?[0-9;]*)?)?$")

PANES = (PaneId.MANAGER_OMP, PaneId.WORKER_OMP, PaneId.HOST_SHELL)
TITLES = {PaneId.MANAGER_OMP: "MANAGER OMP", PaneId.WORKER_OMP: "WORKER OMP", PaneId.HOST_SHELL: "HOST SHELL"}
HEADER_ROWS, FOOTER_ROWS = 2, 1
MIN_ROWS, MIN_COLS = 11, 30  # 11 rows: 8 body rows = two 4-row panes (borders + 2 text rows) top and bottom
SCROLLBACK = 1000  # OMP panes
HOST_SCROLLBACK = 5000  # host shell history lines kept for scroll mode (C-D58)
FEED_CHUNK_BYTES = 1024  # one pyte feed step: ~10 ms for `yes` output, so the slice time budget holds
FEED_SLICE_BYTES = 64 * 1024  # pyte work per loop iteration (bytes) ...
FEED_SLICE_SECONDS = 0.02  # ... or seconds, whichever comes first
CATCHUP_BACKLOG_BYTES = 256 * 1024  # unfed backlog per pane that triggers UI catch-up
CATCHUP_KEEP_BYTES = 64 * 1024  # newest bytes kept (and fed) when catching up
CATCHUP_NOTE_SECONDS = 10.0
WHEEL_LINES = 3  # history lines (or alternate-screen arrow keys) per mouse-wheel notch
MAX_COPY_BYTES = 1024 * 1024  # drag-to-copy: larger selections are refused (C-D62)
AUTOSCROLL_SECONDS = 0.05  # while a drag is held outside the pane, its history scrolls one line per this interval
TMUX_HINT = " (tmux는 set-clipboard on 또는 allow-passthrough on 필요)"
ALT_SCREEN_MODES = {47, 1047, 1049}
MOUSE_TRACKING_MODES = {1000, 1002, 1003}  # one tracking-mode state, like a real terminal (see _track_bracketed)
MOTION_MODES = {1002, 1003}  # the pane's app asked for motion reports (1002: while pressed, 1003: any)
_TRACKED_MODES = {1, 1006} | MOUSE_TRACKING_MODES | ALT_SCREEN_MODES
RESIZE_STEP_COLS, RESIZE_STEP_ROWS = 2, 1  # prefix + arrow moves a divider by this many cells (C-D59)
RESIZE_REPEAT_SECONDS = 1.0  # bare arrows keep resizing this long after the last prefix+arrow / repeated arrow
DRAG_RESIZE_DEBOUNCE = 0.1  # while a divider is dragged, resize frames go out at most this often (+ one on release)
_ARROWS = {"\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down",
           "\x1b[C": "right", "\x1bOC": "right", "\x1b[D": "left", "\x1bOD": "left"}
# IME-neutral prefix commands: Korean IMEs hold letters in a preedit buffer, but Ctrl combos, digits, punctuation,
# arrows, Tab/Enter/Esc/Space pass through. Ctrl-h/i/j/m/[ collide with Backspace/Tab/LF/Enter/Esc, so handoff and
# mouse use Ctrl-o / Ctrl-e instead; confirm uses Ctrl-y (yes), so Ctrl-c stays an unknown key (an interrupt reflex
# must not confirm a takeover). The plain letters keep working in English mode.
CTRL_ALIASES = {"\x11": "q", "\x14": "t", "\x19": "c", "\x0f": "h", "\x12": "r", "\x05": "m", "\x1a": "z",
                "\x0b": "k",  # Ctrl-k: host terminal kill (C-D63); 0x0b collides with no other key here
                "\x10": "p"}  # Ctrl-p: automation pause/resume (CW-18 U5); 0x10 collides with no other key here
HANGUL_HINT = "한글 입력 상태: Ctrl-] 뒤 자모 + Space (예: ㅂ Space = q detach), Ctrl 조합(Ctrl-] Ctrl-q), Ctrl-] Space 메뉴"
DETACH_MOVED = "detach는 이제 Ctrl-] q (Ctrl-q) — d / Ctrl-d 는 더 이상 detach가 아닙니다"
CTRL_D = 0x04
CTRL_D_CONFIRM_SECONDS = 2.0  # a second Ctrl-d to the same OMP pane within this time is delivered (OMP exits on Ctrl-d)
SELECTION_EVICTED_NOTICE = "선택한 내용이 스크롤 기록에서 밀려나 복사하지 않았습니다"
RESTARTABLE = PANES  # panes that Enter restarts once exited: both OMP panes (C-D62) and the host shell (C-D63)
RESTARTING_NOTICE = "다시 시작 중…"
EXITED_KEYS_NOTICE = "OMP 종료됨 — Enter로 새 세션을 시작합니다 (다른 입력은 전달되지 않음)"
HOST_EXITED_KEYS_NOTICE = "host terminal 종료됨 — Enter로 새 shell을 시작합니다 (다른 입력은 전달되지 않음)"
# host terminal force kill (C-D63): prefix k / Ctrl-k / jamo ㅏ opens a confirmation; only the k forms confirm
KILL_KEYS = ("k", "K")  # the command key after CTRL_ALIASES (Ctrl-k -> k; ㅏ -> k via the jamo table)
KILL_CONFIRM_LINES = ("host terminal 강제 종료 확인", "",
                      "host terminal을 강제 종료합니다. 실행 중인 process가 모두 종료됩니다.",
                      "k: 종료 · Esc/다른 키: 취소")
KILL_MANAGER_WARNING = "manager가 사용 중입니다 — 진행 중인 명령은 결과 불명으로 남습니다"
KILL_CONFIRM_FOOTER = "host terminal 강제 종료 확인: k(Ctrl-k·ㅏ) = 종료 · Esc/다른 키 = 취소 · 키는 pane으로 전달되지 않음"
KILL_CANCELLED_NOTICE = "확인이 취소되었습니다 — k 한 번만 눌러 확인"  # extra bytes with the confirm key (auto-repeat, typing, paste)
KILLING_NOTICE ="host terminal 강제 종료 중…"
PAUSE_CANCELLED_NOTICE = "확인이 취소되었습니다 — p 한 번만 눌러 확인"
PAUSING_NOTICE = "자동화 일시정지 요청 중…"
RESUMING_NOTICE = "자동화 재개 요청 중… (대조 완료로 전달)"
AUTOMATION_PENDING_NOTICE = "자동화 일시정지/재개 요청 처리 중…"
KILLED_NOTICE = "host terminal 강제 종료됨 — Enter로 새 shell"
# shown below the numbered command menu: the menu cycle (digits 1-9,0, arrows) stays the 10 items above, so this row is
# reached by Ctrl-k (the same key as after the prefix) instead of a digit
KILL_MENU_ROW = ("host terminal 강제 종료 (확인 후 실행)", "k", "Ctrl-k")
# automation pause/resume (CW-18 U5, C-D66): prefix p / Ctrl-p / jamo ㅔ opens a confirmation; only the p forms confirm
PAUSE_KEYS = ("p", "P")  # the command key after CTRL_ALIASES (Ctrl-p -> p; ㅔ -> p via the jamo table)
PAUSE_CONFIRM_LINES = ("자동화 일시정지 확인", "", "자동화를 일시정지합니다 (p: 확인)",
                       "새 자동 작업이 시작되지 않고 manager 턴 중단을 요청합니다.", "p: 확인 · Esc/다른 키: 취소")
RESUME_CONFIRM_LINES = ("자동화 재개 확인", "", "대조 후 재개합니다 (p: 확인)",
                        "일시정지 중 바뀐 상태를 확인(대조)한 뒤에만 확인하세요.", "p: 확인 · Esc/다른 키: 취소")
PAUSE_CONFIRM_FOOTER = "자동화 일시정지 확인: p(Ctrl-p·ㅔ) = 확인 · Esc/다른 키 = 취소 · 키는 pane으로 전달되지 않음"
RESUME_CONFIRM_FOOTER = "자동화 재개(대조 후) 확인: p(Ctrl-p·ㅔ) = 확인 · Esc/다른 키 = 취소 · 키는 pane으로 전달되지 않음"
PAUSE_MENU_ROW = ("자동화 일시정지 / 재개 (확인 후 실행)", "p", "Ctrl-p")
SUMMARY_MAX_COLS = 40  # the Task summary in the status line is shortened to at most this display width
SUMMARY_MIN_COLS = 8
WORKER_TEXT = {"idle": "대기", "busy": "작업 중"}
TASK_KIND_TEXT = {"experiment": "실험", "work": "작업"}
# "finished" with a run that failed to start is shown as 시작 실패 (smoke-02 E4; see task_text)
TASK_STATUS_TEXT = {"dispatched": "전달됨", "starting": "시작 중", "running": "실행 중", "waiting_report": "보고 대기",
                    "held": "보류", "cancelling": "취소 중", "finished": "보고 완료", "blocked": "막힘", "closed": "종료"}
TASK_HELD_TEXT = {"host_terminal_busy": "host terminal 사용 중",
                  # p27-cd68-fix-03 (review-02 P3 (1)): the worker's own terminal command; nothing for the user to clear
                  "host_terminal_busy:worker_terminal_command": "worker 명령 실행 중 (끝나면 시작)"}
# host-shell held reasons that mean user jobs (or a state left unknown by them) keep the handoff / automation held
JOB_HELD_REASONS = ("manual_jobs", "unknown_or_manual_residue")
JOB_HELD_HINT = ("해제: prefix t,c로 인수 → jobs 종료(fg/kill) → host shell에서 wb-handoff 다시 실행 → "
                 "prefix h로 manager에 반환 → prefix t,c로 다시 인수(빈 prompt)")
JOB_HELD_TASK_NOTE = "jobs 정리: prefix t,c 인수 → wb-handoff → prefix h → prefix t,c 재인수"
TASK_CLOSED_TEXT = {"done": "완료", "cancelled": "취소됨", "superseded_by_new_task": "새 작업으로 대체"}
INTERRUPTION_TEXT = {"requesting": "중단 요청 중", "requested": "중단 요청됨", "confirmed": "중단 확인됨",
                     "unknown": "중단 확인 불명", "request_failed": "중단 요청 실패"}
CTRL_D_NOTICE = "Ctrl-d는 OMP를 종료할 수 있습니다 — 2초 안에 Ctrl-d를 한 번 더 누르면 전달"
# (command key, label, letter form, Ctrl form); menu digits are 1..9 then 0 in this order
MENU_ITEMS = (
    ("[", "scroll 모드 (지나간 출력 보기)", "[", ""),
    ("z", "focus pane 확대/복원", "z", "Ctrl-z"),
    ("t", "host shell 인수 요청", "t", "Ctrl-t"),
    ("c", "인수 확인 (요청 후)", "c", "Ctrl-y"),
    ("h", "host shell을 manager에게 되돌리기(handoff)", "h", "Ctrl-o"),
    ("r", "focus pane 다시 그리기", "r", "Ctrl-r"),
    ("m", "마우스 캡처 켜기/끄기", "m", "Ctrl-e"),
    ("=", "배치 초기화", "=", ""),
    ("?", "도움말", "?", ""),
    ("q", "detach (backend와 PTY는 계속 실행)", "q", "Ctrl-q"),
)
MENU_DIGITS = "1234567890"
CATCHUP_NUDGE_SECONDS = 0.5  # repaint nudges while catch-ups repeat under a sustained flood: at most this often

# Kinds of queued display output. Each is accounted separately so every loop decision is O(1).
REPLAY = "replay"  # attach replay (header replay=true): fed completely, never trimmed; blocks catch-up while pending
TAIL = "tail"  # the kept tail of a catch-up: fed quietly (no query replies); a later catch-up may replace it
LIVE = "live"  # live output: counted in ``backlog_bytes``; subject to catch-up

# Provisional key choices (UR-UX will review). Shown only in the help overlay.
HELP_LINES = (
    "OMP Workbench 키 (임시 배치 — 사용자 UX 검토 대상)",
    "",
    "prefix = Ctrl-]   (그 외 모든 키는 focus된 pane에 원본 그대로 전달)",
    "  prefix prefix   prefix 바이트(0x1d)를 그대로 전송",
    "  prefix 1/2/3    manager OMP / worker OMP / host shell로 focus",
    "  prefix Tab      다음 pane으로 focus",
    "  prefix t        host shell 사용자 인수 요청      (한글 입력 상태: prefix Ctrl-t)",
    "  prefix c        인수 확인 (요청 후)              (prefix Ctrl-y)",
    "  prefix h        host shell을 manager에게 되돌리기(handoff)  (prefix Ctrl-o)",
    "  prefix r        focus pane 다시 그리기 요청      (prefix Ctrl-r)",
    "  prefix [        scroll 모드: focus pane의 지나간 출력 보기 (prefix PgUp 도 진입+한 페이지 위)",
    "  prefix m        마우스 캡처 켜기/끄기 (켜져 있으면 드래그: 복사 · 터미널 자체 선택은 Shift+드래그)  (prefix Ctrl-e)",
    "  prefix q        detach (backend와 PTY는 계속 실행)  (prefix Ctrl-q)",
    "  OMP pane의 Ctrl-d는 종료 방지: 2초 안에 한 번 더 누르면 전달 (host shell은 즉시)",
    "  종료된 OMP pane(종료됨 안내): 그 pane에서 Enter = 새 OMP 세션으로 다시 시작 (이전 대화는 새 OMP에서 /resume)",
    "  host shell 종료 시: Enter = 새 shell 시작 · prefix k (Ctrl-k / ㅏ Space) = host terminal 강제 종료 (확인 필요)",
    "  prefix p        자동화 일시정지 / 재개(대조 후) (확인 필요)  (prefix Ctrl-p · 한글: ㅔ Space)",
    "  prefix ?        이 도움말 (아무 키로 닫기)",
    "  prefix Space    명령 메뉴: ↑/↓+Enter 또는 숫자 1-9/0 (Esc 취소) — 글자 키 없이 쓸 수 있음",
    "한글 IME: Ctrl-] 뒤 자모 + Space 로 영문 키 명령 (예: ㅂ Space = q detach, ㅋ = z; scroll 모드도 ㅓ/ㅏ/ㅎ/ㅂ),",
    "  또는 Ctrl을 누른 채 명령 키(Ctrl-] Ctrl-q), 숫자·Tab·[ = ? 방향키는 그대로, Ctrl-] Space 메뉴 (Ctrl-h/m/i/[ 는 제외)",
    "",
    "바로 scroll (모드 없음): 마우스 휠(해당 pane, 3줄) · Shift+PgUp/Shift+PgDn (focus pane 한 페이지)",
    "  스크롤한 pane에 입력/붙여넣기하면 자동으로 live 복귀 (다른 pane 위치는 유지)",
    "  앱이 마우스 추적을 켠 pane은 휠/클릭이 앱으로 전달, alt-screen 앱은 휠 → ↑/↓, 클릭 = focus",
    "  추적 안 하는 pane(OMP·shell)에서 드래그 = 선택, 놓으면 복사(OSC 52; 위/아래로 끌면 자동 스크롤)",
    "  tmux/herdr 안: 휠·Shift+PgUp/PgDn 그대로 동작 (tmux mouse on/off, herdr 설정 변경 불필요)",
    "  키가 가로채이면 prefix PgUp / prefix [ (Ctrl-]는 tmux·herdr prefix Ctrl-b와 겹치지 않음)",
    "scroll 모드 (키는 pane으로 전달되지 않음, 새 출력이 와도 보던 위치 유지):",
    "  PgUp/PgDn 한 페이지   ↑/↓ (k/j) 한 줄   Home/g 맨 위   End/G 맨 아래(live)   q/Esc 종료",

    "배치: 위 = manager OMP | worker OMP, 아래 = host shell 전체 폭.",
    "  경계 조절: 마우스로 pane 사이 경계선 드래그 · prefix ←/→ manager|worker 폭 · prefix ↑/↓ 위|host 높이",
    "  (prefix 방향키 뒤 1초 안의 방향키는 계속 조절)  prefix z(Ctrl-z) focus pane 확대/복원  prefix = 배치 초기화",
    "focus 변경은 host 입력 owner를 바꾸지 않는다.",
)

# scroll-mode keys (raw bytes typed while scroll mode is on); anything else is ignored, never forwarded
_SCROLL_KEYS = {
    b"\x1b[5~": "pgup", b"\x1b[6~": "pgdn", b"\x1b[A": "up", b"\x1bOA": "up", b"\x1b[B": "down",
    b"\x1bOB": "down", b"\x1b[H": "home", b"\x1b[1~": "home", b"\x1bOH": "home", b"\x1b[7~": "home",
    b"\x1b[F": "end", b"\x1b[4~": "end", b"\x1bOF": "end", b"\x1b[8~": "end",
    b"k": "up", b"j": "down", b"g": "home", b"G": "end", b"q": "quit", b"Q": "quit", b"\x1b": "quit",
}


def _plain(value: Any) -> str:
    """Backend text made safe for a status line: no control characters, single spaces."""
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def _clip(text: str, width: int) -> str:
    """Shorten ``text`` to at most ``width`` display columns, ending in an ellipsis when shortened."""
    if wcswidth(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        size = max(wcswidth(ch), 0)
        if used + size > width - 1:
            break
        out.append(ch)
        used += size
    return "".join(out) + "…"


def _safe_tail(segment: bytes, keep: int) -> bytes:
    """Newest ``keep`` bytes, advanced to a boundary: after a newline, else at an ESC that starts a sequence."""
    if len(segment) <= keep:
        return bytes(segment)
    return _align(bytes(segment[-keep:]), before=bytes(segment[-keep - 1:-keep]))


def _align(tail: bytes, before: bytes | None = None) -> bytes:
    """``tail`` cut out of a longer stream, advanced to a safe start (see ``_safe_tail``).

    ``before`` is the byte just before the cut when known: a leading ``\\`` is
    dropped only when it is the second half of a split ``ESC \\`` (``before`` is
    ESC, or unknown). A bounded loop (o1-fix-03): any number of string
    terminators in the tail is fine.
    """
    newline = tail.find(b"\n")
    if newline >= 0:
        return tail[newline + 1:]
    if tail[:1] == b"\\" and before in (None, b"\x1b"):
        tail = tail[1:]  # the cut fell between the ESC and the \ of a string terminator
    at = tail.find(b"\x1b")
    if at >= 0 and tail[at + 1:at + 2] == b"\\":
        # ESC \ (ST) ends a string the cut started inside (e.g. a tmux-wrapped notification): start after it
        # (and after any terminators right behind it; a loop, so any number of them is fine).
        start = at + 2
        while tail[start:start + 2] == b"\x1b\\":
            start += 2
        return tail[start:]
    if at >= 0 and tail[at + 1:at + 2] == b"\x1b":
        at += 1  # a tmux-doubled ESC: the cut is inside a passthrough; its inner sequence starts here
    if at >= 0:
        return tail[at:]
    while tail and tail[0] & 0xC0 == 0x80:
        tail = tail[1:]  # no boundary: at least do not start in the middle of a UTF-8 character
    return tail


def _identity(frame: ui_v1.Frame) -> tuple[Any, Any]:
    return (frame.header.get("session_id"), frame.header.get("generation"))


class Sender(Protocol):
    def send(self, kind: ClientType, payload: bytes = b"", **fields: Any) -> str: ...


def _tiled_boxes(rows: int, cols: int, layout: Layout) -> dict[PaneId, tuple[int, int, int, int]]:
    body = max(2, rows - HEADER_ROWS - FOOTER_ROWS)
    top_h = top_height(body, layout.row_ratio)
    bottom_height = max(1, body - top_h)
    left_w = left_width(cols, layout.col_ratio)
    right_width = max(1, cols - left_w)
    bottom = HEADER_ROWS + top_h
    return {PaneId.MANAGER_OMP: (HEADER_ROWS, 0, top_h, left_w),
            PaneId.WORKER_OMP: (HEADER_ROWS, left_w, top_h, right_width),
            PaneId.HOST_SHELL: (bottom, 0, bottom_height, max(1, cols))}


def _zoom_box(rows: int, cols: int) -> tuple[int, int, int, int]:
    return (HEADER_ROWS, 0, max(2, rows - HEADER_ROWS - FOOTER_ROWS), max(1, cols))


def pane_boxes(rows: int, cols: int, layout: Layout = DEFAULT_LAYOUT,
               zoom: PaneId | None = None) -> dict[PaneId, tuple[int, int, int, int]]:
    """(top, left, height, width) of each *visible* pane including its border (C-D58 layout, C-D59 splits).

    Top row: manager OMP (left) and worker OMP (right), about half the width each by default. Bottom: host shell
    across the full width. The top row gets the extra row on an odd body height. ``layout`` moves the two
    dividers (clamped so every pane keeps a usable inner size); ``zoom`` shows only that pane over the whole body.
    """
    if zoom is not None:
        return {zoom: _zoom_box(rows, cols)}
    return _tiled_boxes(rows, cols, layout)


def pane_inner_sizes(rows: int, cols: int, layout: Layout = DEFAULT_LAYOUT,
                     zoom: PaneId | None = None) -> dict[PaneId, tuple[int, int]]:
    """(rows, cols) of each pane's terminal area inside its border.

    A hidden (zoomed-away) pane keeps the size it has in the un-zoomed layout, so un-zooming needs no resize for it.
    """
    boxes = _tiled_boxes(rows, cols, layout)
    if zoom is not None:
        boxes[zoom] = _zoom_box(rows, cols)
    return {pane: (max(1, height - 2), max(1, width - 2)) for pane, (_, _, height, width) in boxes.items()}


class _CountingDeque(deque):
    """History ``top`` queue that counts every line pushed, so a scrolled view can hold its place."""

    pushed = 0

    def append(self, item: Any) -> None:
        self.pushed += 1
        super().append(item)

    def extend(self, items: Any) -> None:
        for item in items:
            self.append(item)


class PaneScreen(TerminalScreen):
    """TerminalScreen whose history queue counts pushes (also after alternate-screen swaps)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._count_history()

    resets = 0  # full resets (RIS, session replacement) so far: a selection made before one is void

    def reset(self) -> None:
        self.resets += 1
        super().reset()

    def _restore(self, state: dict[str, object]) -> None:
        super()._restore(state)
        self._count_history()

    def _count_history(self) -> None:
        top = self.history.top
        if not isinstance(top, _CountingDeque):
            self.history = self.history._replace(top=_CountingDeque(top, maxlen=top.maxlen))


@dataclass
class _ScrollView:
    queue: Any  # the history queue the anchor refers to
    anchor: int  # absolute history line index at the top edge of the view (pushed - offset)


@dataclass
class _Selection:
    """A drag-to-copy selection in pane content coordinates: (absolute history line, column) pairs.

    The absolute line is ``history.pushed - offset + row``, so it keeps pointing at the same output while the
    view scrolls or new lines arrive. ``queue`` ties it to the history it was made on.
    """

    pane: PaneId
    queue: Any
    anchor: tuple[int, int]
    cursor: tuple[int, int]
    dragging: bool = True
    edge: int = 0  # -1 = pointer above the pane's inner area, 1 = below, 0 = inside
    column: int = 0  # last pointer column (clamped), used by the auto-scroll tick
    next_scroll: float = float("-inf")
    resets: int = 0  # the pane screen's reset count when the selection began

    @property
    def moved(self) -> bool:
        return self.anchor != self.cursor

    def span(self) -> tuple[tuple[int, int], tuple[int, int]]:
        return (self.anchor, self.cursor) if self.anchor <= self.cursor else (self.cursor, self.anchor)


@dataclass
class PaneView:
    screen: TerminalScreen
    stream: Any
    identity: tuple[Any, Any] | None = None
    # DECSET/DECRST 2004 is tracked on receipt (not on feed), so dropped output needs no re-scan.
    bracketed: bool | None = None  # last received DECSET/DECRST 2004; None = never seen
    modes: set = field(default_factory=set)  # DECSET modes seen on receipt: 1 (DECCKM), 1000/1002/1003/1006, 47/1047/1049
    tail: bytes = b""  # possibly split private-mode sequence at the end of the previous received chunk
    received_identity: tuple[Any, Any] | None = None  # identity of the last received frame (2004 tracking)
    backlog: deque = field(default_factory=deque)  # (identity, kind, memoryview) received but not yet fed
    backlog_bytes: int = 0  # unfed LIVE bytes
    replay_bytes: int = 0  # unfed REPLAY bytes
    tail_bytes: int = 0  # unfed TAIL bytes
    skipped_bytes: int = 0  # total bytes the UI skipped while catching up
    catchup_until: float = 0.0  # monotonic time until which the indicator is shown
    nudge_after: float = 0.0  # no repaint nudge before this monotonic time
    nudge_due: bool = False  # a catch-up still owes the pane a repaint nudge

    @property
    def mouse_tracking(self) -> bool:
        return bool(self.modes & MOUSE_TRACKING_MODES)

    @property
    def alt_screen(self) -> bool:
        return bool(self.modes & ALT_SCREEN_MODES)

    def count(self, kind: str, size: int) -> None:
        if kind is LIVE:
            self.backlog_bytes += size
        elif kind is REPLAY:
            self.replay_bytes += size
        else:
            self.tail_bytes += size


class ProductModel:
    def __init__(self, sender: Sender, rows: int, cols: int, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, environ: Mapping[str, str] | None = None):
        self.sender = sender
        self._environ = environ  # None: the process environment, read when a copy happens (TMUX)
        self._sel: _Selection | None = None  # drag-to-copy selection (dragging, or kept highlighted after the copy)
        self._out = b""  # terminal bytes the app loop writes (OSC 52); the model never touches the terminal
        self.clock = clock
        self._mono = monotonic
        self.rows, self.cols = rows, cols
        self.focus = PaneId.MANAGER_OMP
        self.state: dict[str, Any] = {}
        self.last_seen: float | None = None
        self.notice = ""
        self.help_open = False
        self.menu_open = False  # prefix Space command menu (IME-neutral: arrows/Enter/digits/Esc only)
        self.menu_index = 0
        self.kill_confirm_open = False  # host terminal kill confirmation (C-D63): modal like the menu
        self.pause_confirm: str | None = None  # automation "pause" / "resume" confirmation (CW-18 U5): same kind of modal
        self._mode: PaneId | None = None  # explicit scroll mode (prefix [): keys drive the view, never the pane
        self._views: dict[PaneId, _ScrollView] = {}  # panes whose view is scrolled back (wheel, Shift+PgUp, mode)
        self.mouse_capture = True  # outer-terminal mouse reporting wanted (prefix m toggles; the app loop applies it)
        self._mouse_reassert = False  # prefix r: the app loop re-sends the outer mouse mode once
        self.quit = False
        self.closed_reason: str | None = None
        self.parser = InputParser()
        self.pending: dict[str, tuple[ClientType, PaneId | None]] = {}
        self._replaying = False
        self.layout = DEFAULT_LAYOUT  # C-D59 divider positions (ratios); None ratios = the default split
        self.zoom: PaneId | None = None  # the zoomed pane (always the focus pane) or None
        self.layout_dirty = False  # the app persists the layout (ui-layout.json) when this is set
        self._drag: tuple[str, int] | None = None  # (axis "v"/"h", grab offset) of a divider drag in progress
        self._repeat_until: float | None = None  # bare arrows resize until then (tmux-like repeat)
        self._last_now = 0.0
        self._restarting: dict[PaneId, Any] = {}  # OMP pane -> its generation when the restart was requested (C-D62)
        self._ctrl_d: tuple[PaneId, float] | None = None  # (OMP pane, deadline) of a Ctrl-d held for confirmation
        self._unsent: set[PaneId] = set()  # panes whose current inner size was not sent to the backend yet
        self._last_resize_sent = float("-inf")
        self.sizes = pane_inner_sizes(rows, cols)
        self.panes: dict[PaneId, PaneView] = {}
        for pane in PANES:
            r, c = self.sizes[pane]
            screen = PaneScreen(c, r, history=HOST_SCROLLBACK if pane is PaneId.HOST_SHELL else SCROLLBACK,
                                reply=lambda data, pane=pane: self._query_reply(pane, data))
            self.panes[pane] = PaneView(screen, make_stream(screen))

    # -- sending ---------------------------------------------------------
    def _send(self, kind: ClientType, payload: bytes = b"", pane: PaneId | None = None, **fields: Any) -> str:
        if kind in (ClientType.INPUT, ClientType.PASTE) and pane is not None and self.pane_exited(pane):
            return ""  # nothing typed reaches an exited OMP pane; Enter restarts it instead (C-D62)
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
        if request_id:
            self.pending[request_id] = (ClientType.INPUT, None)  # quiet: refusals are not shown

    # -- backend frames --------------------------------------------------
    def touch(self) -> None:
        self.last_seen = self.clock()

    def apply_snapshot(self, snapshot: dict[str, Any], *, adopt_focus: bool = False) -> None:
        self.state = snapshot
        self.touch()
        if adopt_focus or not any(k is ClientType.FOCUS for k, _ in self.pending.values()):
            before = self.focus
            try:
                self.focus = PaneId(snapshot.get("focus", self.focus.value))
            except ValueError:
                pass
            if self.focus is not before:
                self._clear_selection()
        if self._ctrl_d is not None and self._ctrl_d[0] is not self.focus:
            self._cancel_ctrl_d()  # a backend-driven focus change drops a held Ctrl-d
        if self._mode is not None and self._mode is not self.focus:
            self.exit_scroll()  # a backend-driven focus change leaves scroll mode, like prefix 1/2/3
        self._sync_zoom()
        self._sync_restarts()

    def attach_done(self, snapshot: dict[str, Any]) -> None:
        self.apply_snapshot(snapshot, adopt_focus=True)

    def on_display(self, frame: ui_v1.Frame) -> None:
        """Synchronous path (attach replay, tests): feed the frame now."""
        pane = self._display_pane(frame)
        if pane is None:
            return
        view, identity = self.panes[pane], _identity(frame)
        self._note_new_session(pane, identity)
        self._receive(view, identity, frame.payload)
        self._apply(view, identity, bool(frame.header.get("replay")), frame.payload)

    def _display_pane(self, frame: ui_v1.Frame) -> PaneId | None:
        self.touch()
        try:
            return PaneId(frame.header.get("pane"))
        except ValueError:
            return None

    def _note_new_session(self, pane: PaneId, identity: tuple[Any, Any]) -> None:
        """Output of a newer session/generation than the one the last state calls exited: the pane was restarted
        (C-D62) and is live now, without waiting for the next periodic state push (its start-up queries and typed
        keys must not be dropped)."""
        if pane not in RESTARTABLE or identity[0] is None:
            return
        info = self.state.get("panes", {}).get(pane.value)
        if not isinstance(info, dict) or info.get("alive"):
            return
        session, generation = info.get("session_id"), info.get("generation")
        newer = generation is not None and isinstance(identity[1], int) and identity[1] > generation
        if newer or (session is not None and identity[0] != session):
            info["alive"] = True
            info["exit_status"] = None
            self._restarting.pop(pane, None)
            if self.notice == RESTARTING_NOTICE:
                self.notice = ""

    def _receive(self, view: PaneView, identity: tuple[Any, Any], payload: bytes) -> None:
        """Receipt-time bookkeeping, once per received byte: 2004 state of the pane's current session."""
        if view.received_identity is not None and identity != view.received_identity:
            view.bracketed, view.tail = None, b""  # the pane's process/session was replaced
            view.modes.clear()
        view.received_identity = identity
        self._track_bracketed(view, payload)

    def _apply(self, view: PaneView, identity: tuple[Any, Any], quiet: bool, payload: bytes) -> None:
        """Feed pyte. ``quiet``: replayed/skipped-ahead output must not answer (stale) terminal queries."""
        if view.identity is not None and identity != view.identity:
            view.screen.reset()  # the pane's process/session was replaced
            view.stream = make_stream(view.screen)  # o1-fix-02: no escape/string state carries over
            self._history_cleared(view)
        view.identity = identity
        self._replaying = quiet
        try:
            view.stream.feed(payload)
        finally:
            self._replaying = False
        self._sync_scroll()

    # -- decoupled intake: drain the socket fast, feed pyte in bounded slices ----
    def enqueue_display(self, frame: ui_v1.Frame) -> None:
        pane = self._display_pane(frame)
        if pane is None or not frame.payload:
            return
        view, identity = self.panes[pane], _identity(frame)
        self._note_new_session(pane, identity)
        self._receive(view, identity, frame.payload)
        # backend replay is bounded by its retained tail: never part of the catch-up accounting
        kind = REPLAY if frame.header.get("replay") else LIVE
        view.backlog.append((identity, kind, memoryview(frame.payload)))
        view.count(kind, len(frame.payload))

    def has_backlog(self) -> bool:
        return any(view.backlog for view in self.panes.values())

    def feed_pending(self, *, max_bytes: int = FEED_SLICE_BYTES, max_seconds: float = FEED_SLICE_SECONDS) -> bool:
        """Feed queued output for at most ``max_bytes``/``max_seconds``; True while a backlog remains."""
        for pane in PANES:  # O(1) per pane: counters only, no backlog scan
            view = self.panes[pane]
            # attach replay is fed completely first; live backlog (and an unfinished catch-up tail) may be dropped
            if view.backlog_bytes > CATCHUP_BACKLOG_BYTES and not view.replay_bytes:
                self._catch_up(pane)
        deadline = time.monotonic() + max_seconds
        fed, progress = 0, True
        while progress and fed < max_bytes and time.monotonic() < deadline:
            progress = False
            for pane in PANES:
                view = self.panes[pane]
                if not view.backlog:
                    continue
                identity, kind, payload = view.backlog[0]
                chunk = bytes(payload[:FEED_CHUNK_BYTES])
                if len(payload) > FEED_CHUNK_BYTES:
                    view.backlog[0] = (identity, kind, payload[FEED_CHUNK_BYTES:])  # memoryview: O(1) slice
                else:
                    view.backlog.popleft()
                view.count(kind, -len(chunk))
                self._apply(view, identity, kind is not LIVE, chunk)
                fed += len(chunk)
                progress = True
        for pane in PANES:  # a rate-limited nudge is sent later, at the latest once the pane's backlog is fed
            view = self.panes[pane]
            if view.nudge_due and (not view.backlog or time.monotonic() >= view.nudge_after):
                self._nudge(pane)
        return self.has_backlog()

    def _catch_up(self, pane: PaneId) -> None:
        """Drop the older backlog, reset this pane's screen and feed only the newest tail.

        Cost is bounded by the kept tail, not by the backlog: the tail is collected by walking from the
        newest item; older items are dropped without being joined (2004 state was tracked on receipt).
        A previous, unfinished catch-up tail is dropped like live output. The display is a view: backend
        state and its retained tail are untouched. The pane application is asked to repaint through the
        usual resize nudge (rate limited while catch-ups repeat).
        """
        view = self.panes[pane]
        pending = view.backlog_bytes + view.tail_bytes
        newest = view.backlog[-1][0]
        parts: list[memoryview] = []
        size, cut, before = 0, False, None
        for identity, _, payload in reversed(view.backlog):
            if identity != newest:
                break  # keep only what belongs to the newest session (its start is a natural boundary)
            room = CATCHUP_KEEP_BYTES - size
            if len(payload) > room:
                parts.append(payload[len(payload) - room:])
                before = bytes(payload[len(payload) - room - 1:len(payload) - room])
                cut = True  # older output of this session is dropped: start the tail at a safe boundary
                break
            parts.append(payload)
            size += len(payload)
        segment = b"".join(reversed(parts))
        tail = _align(segment, before=before) if cut else segment
        dropped = self._history_cleared(view)
        view.screen.reset()
        view.stream = make_stream(view.screen)
        view.backlog = deque([(newest, TAIL, memoryview(tail))] if tail else ())  # stale queries not answered
        view.backlog_bytes, view.tail_bytes = 0, len(tail)
        view.skipped_bytes += pending - len(tail)
        now = time.monotonic()
        view.catchup_until = now + CATCHUP_NOTE_SECONDS
        self.notice = (f"[{TITLES[pane]}] 출력 따라잡음: {pending - len(tail)} bytes 건너뜀"
                       + (" · scroll 보기는 live로 복귀" if dropped else ""))
        view.nudge_due = True
        if now >= view.nudge_after:
            self._nudge(pane)

    def _nudge(self, pane: PaneId) -> None:
        view = self.panes[pane]
        view.nudge_due = False
        view.nudge_after = time.monotonic() + CATCHUP_NUDGE_SECONDS
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
        if kind is ClientType.RESTART_PANE and not header.get("ok") and pane is not None:
            self._restarting.pop(pane, None)  # refused: Enter may try again
            if self.notice == RESTARTING_NOTICE:
                self.notice = ""
        if kind is ClientType.RESTART_PANE and header.get("ok") and pane is PaneId.HOST_SHELL:
            host = self.state.get("panes", {}).get(PaneId.HOST_SHELL.value)
            if isinstance(host, dict) and header.get("input_owner") in ("user", "manager"):
                host["input_owner"] = header["input_owner"]
        if header.get("ok"):
            if kind is ClientType.KILL_PANE:
                survivors = header.get("survivors")
                left = f" (종료되지 않은 process {len(survivors)}개)" if isinstance(survivors, (list, tuple)) and survivors else ""
                self.notice = KILLED_NOTICE + left
            elif kind is ClientType.TAKEOVER_REQUEST:
                if isinstance(shell, dict) and not shell.get("takeover_requested"):
                    owner = shell.get("input_owner") or self.input_owner()
                    self.notice = f"인수 요청이 기록되지 않음 — 이미 host 입력 owner={owner}"
                else:
                    self.notice = "인수 요청됨 — prefix c 로 확인"
            elif kind is ClientType.TAKEOVER_CONFIRM:
                self.notice = "인수 확인됨: host 입력 owner 변경"
            elif kind is ClientType.HANDOFF:
                self.notice = "host shell을 manager에게 되돌림"
            elif kind in (ClientType.PAUSE, ClientType.RESUME):
                self._automation_result(kind, header.get("automation"))
            return
        if kind is ClientType.INPUT and pane is None:
            return  # terminal-query reply refused; not a user action
        what = {ClientType.PASTE: "붙여넣기", ClientType.INPUT: "입력",
                                         ClientType.RESTART_PANE: "재시작", ClientType.KILL_PANE: "강제 종료",
                                         ClientType.PAUSE: "일시정지", ClientType.RESUME: "재개"}.get(kind, str(kind.value if kind else "요청"))
        target = f" [{TITLES[pane]}]" if pane else ""
        text = f"{what} 거부{target}: {header.get('reason')}: {header.get('detail') or ''}".rstrip(": ")
        held = self._held_reasons(header)
        if held:
            text += f" (held: {', '.join(held)})"
            if any(r in JOB_HELD_REASONS for r in held):
                text += f" · {JOB_HELD_HINT}"
        self.notice = text

    def _automation_result(self, kind: ClientType, automation: Any) -> None:
        """A pause/resume answer carries the automation state at once; the rest finishes in a later state push."""
        if isinstance(automation, dict):
            merged = dict(self._automation())
            merged.update(automation)
            self.state["automation"] = merged
        state = self._automation().get("state")
        if kind is ClientType.PAUSE:
            self.notice = "자동화 일시정지됨" if state == "paused" else "자동화 일시정지 요청됨 — 진행은 상태줄에서 확인"
        elif state == "resuming":
            self.notice = "자동화 재개 진행 중"
        elif state in ("paused", "pausing"):
            self.notice = "자동화 재개 요청됨 — 아직 일시정지 상태입니다"
        else:
            self.notice = "자동화 재개됨"

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
    def handle_input(self, data: bytes, *, now: float | None = None) -> bool:
        """Parse and act on typed bytes; True when an event was handled (the caller redraws)."""
        self._last_now = self._mono() if now is None else now
        self.parser.jamo_keys = self._mode is not None or self._confirm_open()  # a lone jamo is its 2-set letter key
        return self._handle_events(self.parser.feed(data, now=now))

    def flush_input(self, *, now: float | None = None) -> bool:
        """Release held partial input (e.g. a lone Esc); True when an event was handled (the caller redraws)."""
        self._last_now = self._mono() if now is None else now
        self.parser.jamo_keys = self._mode is not None or self._confirm_open()
        expired = self._repeat_until is not None and self._last_now >= self._repeat_until
        if expired:
            self._repeat_until = None  # the resize-repeat hint leaves the footer
        if self._ctrl_d is not None and self._last_now > self._ctrl_d[1]:
            self._cancel_ctrl_d()  # the confirmation window is over: the first Ctrl-d stays dropped
            expired = True
        return self._handle_events(self.parser.flush(now=now)) or expired

    @staticmethod
    def _track_bracketed(view: PaneView, payload: bytes) -> None:
        data = view.tail + payload if view.tail else payload
        for match in _PRIVATE_MODE.finditer(data):
            enable = match.group(2) == b"h"
            for param in match.group(1).split(b";"):
                if param == b"2004":
                    view.bracketed = enable
                elif param.isdigit() and int(param) in MOUSE_TRACKING_MODES:
                    view.modes -= MOUSE_TRACKING_MODES  # a set replaces the mode, any reset turns tracking off
                    if enable:
                        view.modes.add(int(param))
                elif param.isdigit() and int(param) in _TRACKED_MODES:
                    (view.modes.add if enable else view.modes.discard)(int(param))
        # a split sequence can only start at the last ESC (its remaining bytes contain none): O(suffix)
        at = data.rfind(b"\x1b")
        view.tail = bytes(data[at:]) if at >= 0 and _PARTIAL_MODE.match(data, at) else b""

    def _bracketed_paste(self, pane: PaneId) -> bool:
        # Strip the markers only once the pane's application has turned DECSET 2004 off;
        # a never-observed state keeps the whole frame (fail-safe for the backend paste contract).
        # The sh (dash) fallback host shell never supports it: strip unless 2004h was observed.
        bracketed = self.panes[pane].bracketed
        if pane is PaneId.HOST_SHELL and (self.host_info().get("shell") or {}).get("kind") == "sh":
            return bracketed is True
        return bracketed is not False

    def _handle_events(self, events: list) -> bool:
        handled = self._handle_event_list(events)
        if self.menu_open and self.parser.prefix_active:
            self.menu_open = False  # the prefix key alone closes the menu (its label says so)
            self.parser.cancel_prefix()  # ... and is spent: the next key is ordinary input again
            handled = True
        return handled

    def _handle_event_list(self, events: list) -> bool:
        kill_chunk = self._confirm_open()  # an open confirmation judges the whole chunk (C-D63 review R3)
        chunk_keys = self._confirm_keys()
        single = len(events) == 1
        for event in events:
            if self._repeat_until is not None:
                if self._last_now >= self._repeat_until or self._mode is not None:
                    self._repeat_until = None
                elif isinstance(event, Passthrough):
                    remaining = self._repeat_arrows(event.data)
                    if not remaining:
                        continue  # every key was a resize-repeat arrow: nothing goes to the pane
                    event = Passthrough(remaining)
                    self._repeat_until = None
                elif not isinstance(event, Mouse):
                    self._repeat_until = None
            if self._ctrl_d is not None and not self._continues_ctrl_d(event):
                self._cancel_ctrl_d()  # any other key, a paste or a command drops the held first Ctrl-d
            if not isinstance(event, Mouse):
                self._clear_selection()  # the next key (or paste, command, page scroll) ends the highlight
            if kill_chunk:
                if self._confirm_open():
                    self._confirm_event(event, single=single)
                elif not single and self._confirm_starts(event, chunk_keys):
                    self.notice = self._cancelled_notice(chunk_keys)
                continue  # the rest of a multi-event chunk after the decision is dropped: no pane sees it
            if self._confirm_open():
                self._confirm_event(event, single=True)
                continue
            if self.menu_open:
                self._menu_event(event)
                continue
            if isinstance(event, Command):
                self.help_open = False
                self._end_drag()
                self._repeat_until = None
                self._command(event.key)
            elif isinstance(event, Mouse):
                if not self.help_open:  # the overlay is closed by keys only
                    self._mouse(event)
            elif isinstance(event, PageScroll):
                self.help_open = False
                self.scroll_by(event.direction * self._page(self.focus), self.focus)
            elif isinstance(event, PasteRejected):
                self.help_open = False
                self.notice = f"붙여넣기 거부: paste_too_large: {event.reason}"
            elif isinstance(event, Paste) and self._mode is not None:
                self.help_open = False
                self.notice = "scroll 모드: 붙여넣기는 pane으로 전달되지 않음 (q로 종료)"
            elif isinstance(event, Paste) and self.pane_exited(self.focus):
                self.help_open = False
                self.notice = self._exited_keys_notice(self.focus)
            elif isinstance(event, Paste):
                self.help_open = False  # the paste is delivered, not swallowed by the overlay
                data = event.data
                if not self._bracketed_paste(self.focus):
                    data = data[len(PASTE_START):-len(PASTE_END)]  # app did not enable DECSET 2004
                self._live_for_input()
                self._send(ClientType.PASTE, data, self.focus)
            elif self.help_open:
                self.help_open = False  # any other key just closes the overlay
            elif isinstance(event, Passthrough) and self._mode is not None:
                self._scroll_input(event.data)  # scroll mode: keys drive the view, never the pane
            elif isinstance(event, Passthrough):
                self._send_input(event.data)
        return bool(events)

    def _continues_ctrl_d(self, event: Any) -> bool:
        """Events that leave a held Ctrl-d alone: mouse reports and keys that are about to reach the focus pane."""
        if isinstance(event, Mouse):
            return True
        return (isinstance(event, Passthrough) and not self.menu_open and not self.help_open and self._mode is None)

    def _cancel_ctrl_d(self) -> None:
        if self._ctrl_d is not None:
            self._ctrl_d = None
            if self.notice == CTRL_D_NOTICE:
                self.notice = ""

    def _send_input(self, data: bytes) -> None:
        """Send typed bytes to the focus pane. Ctrl-d to an OMP pane needs a second press (OMP exits on it)."""
        pane = self.focus
        if self.pane_exited(pane):
            self._exited_input(pane, data)
            return
        if pane is PaneId.HOST_SHELL or CTRL_D not in data:
            if self._ctrl_d is not None:
                self._cancel_ctrl_d()
            self._live_for_input()
            self._send(ClientType.INPUT, data, pane)
            return
        out = bytearray()
        i = 0
        while i < len(data):
            byte = data[i]
            # ESC 0x04 (Alt+Ctrl-d) is ONE key: held / delivered whole, never split into a lone ESC + Ctrl-d
            key = data[i:i + 2] if byte == 0x1B and data[i + 1:i + 2] == bytes((CTRL_D,)) else bytes((byte,))
            i += len(key)
            if key[-1] != CTRL_D:
                if self._ctrl_d is not None:
                    self._cancel_ctrl_d()  # the byte after a held Ctrl-d cancels it; the byte itself goes through
                out.append(byte)
                continue
            held, self._ctrl_d = self._ctrl_d, None
            if held is not None and held[0] is pane and self._last_now <= held[1]:
                out += key  # confirmed: exactly the key pressed second is delivered, whole
                if self.notice == CTRL_D_NOTICE:
                    self.notice = ""
            else:
                self._ctrl_d = (pane, self._last_now + CTRL_D_CONFIRM_SECONDS)
                self.notice = CTRL_D_NOTICE
        if out:
            self._live_for_input()
            self._send(ClientType.INPUT, bytes(out), pane)

    def _exited_keys_notice(self, pane: PaneId) -> str:
        if pane in self._restarting:
            return RESTARTING_NOTICE
        return HOST_EXITED_KEYS_NOTICE if pane is PaneId.HOST_SHELL else EXITED_KEYS_NOTICE

    def _exited_input(self, pane: PaneId, data: bytes) -> None:
        """Typed bytes for an exited pane: only Enter counts (one restart request); the rest is dropped."""
        self._cancel_ctrl_d()
        if pane in self._restarting:
            return  # a restart is already pending: no duplicate request
        if b"\r" not in data and b"\n" not in data:
            self.notice = self._exited_keys_notice(pane)
            return
        generation = (self.state.get("panes", {}).get(pane.value) or {}).get("generation")
        self._send(ClientType.RESTART_PANE, pane=pane)
        self._restarting[pane] = generation
        self.notice = RESTARTING_NOTICE

    def _sync_restarts(self) -> None:
        """A pending restart ends when the snapshot shows the pane alive or on a newer generation."""
        panes = self.state.get("panes", {})
        for pane, generation in list(self._restarting.items()):
            info = panes.get(pane.value)
            if isinstance(info, dict) and (info.get("alive") or info.get("generation") != generation):
                del self._restarting[pane]
                if self.notice == RESTARTING_NOTICE:
                    self.notice = ""

    def _repeat_arrows(self, data: bytes) -> bytes:
        """Consume leading arrow keys as divider moves during resize repeat; returns the rest (forwarded normally)."""
        while len(data) >= 3 and data[:3].decode("latin-1") in _ARROWS:
            self._resize_arrow(_ARROWS[data[:3].decode("latin-1")])
            self._repeat_until = self._last_now + RESIZE_REPEAT_SECONDS
            data = data[3:]
        return data

    def _menu_event(self, event: Any) -> None:
        """While the menu is open no event reaches a pane; prefix (plus its key) cancels, Esc cancels."""
        if isinstance(event, Command):
            self.menu_open = False  # prefix cancels; the key after it is not run
        elif isinstance(event, Passthrough):
            self._menu_input(event.data)
        elif isinstance(event, (Paste, PasteRejected)):
            self.notice = "명령 메뉴 열림: 붙여넣기는 pane으로 전달되지 않음 (Esc로 닫기)"

    def _menu_input(self, data: bytes) -> None:
        i = 0
        while i < len(data) and self.menu_open:
            byte = data[i]
            if byte == 0x1B:
                rest = data[i:]
                length = _sequence_length(rest) if len(rest) > 1 else 0
                if length == 0:
                    self.menu_open = False  # a lone Esc closes the menu
                    return
                seq = rest[:length]
                if seq in (b"\x1b[A", b"\x1bOA"):
                    self.menu_index = (self.menu_index - 1) % len(MENU_ITEMS)
                elif seq in (b"\x1b[B", b"\x1bOB"):
                    self.menu_index = (self.menu_index + 1) % len(MENU_ITEMS)
                i += length
                continue
            i += 1
            if byte in (0x0D, 0x0A):
                self._menu_run(self.menu_index)
            elif byte == PREFIX:
                self.menu_open = False
            elif byte == 0x0B:  # Ctrl-k: the kill row below the numbered list (IME-neutral, like the other Ctrl keys)
                self.menu_open = False
                self._command("k")
            elif byte == 0x10:  # Ctrl-p: the pause/resume row below it
                self.menu_open = False
                self._command("p")
            elif 0x30 <= byte <= 0x39 and chr(byte) in MENU_DIGITS:
                self._menu_run(MENU_DIGITS.index(chr(byte)))
            # anything else (letters, Hangul, Tab, Backspace, ...) is swallowed: the menu is arrows/Enter/digits/Esc

    def _menu_run(self, index: int) -> None:
        self.menu_open = False
        self._command(MENU_ITEMS[index][0])

    def menu_lines(self) -> list[str]:
        lines = ["명령 메뉴 (Ctrl-] Space)   ↑/↓ + Enter 또는 숫자 · Esc / Ctrl-] 취소", ""]
        for index, (_, label, letter, ctrl) in enumerate(MENU_ITEMS):
            keys = f"Ctrl-] {letter}" + (f" / Ctrl-] {ctrl}" if ctrl else "")
            lines.append(f"{'>' if index == self.menu_index else ' '} {MENU_DIGITS[index]}  {label}   [{keys}]")
        label, letter, ctrl = KILL_MENU_ROW
        lines.append(f"  Ctrl-k  {label}   [Ctrl-] {letter} / Ctrl-] {ctrl}]")
        label, letter, ctrl = PAUSE_MENU_ROW
        lines.append(f"  Ctrl-p  {label}   [Ctrl-] {letter} / Ctrl-] {ctrl}]")
        lines += ["", "focus: Ctrl-] 1/2/3 · Tab   경계 조절: Ctrl-] ←↑↓→   literal prefix: Ctrl-] Ctrl-]",
                  "한글 입력 상태에서는 글자 키 대신 Ctrl 조합이나 이 메뉴를 쓰세요"]
        return lines

    # -- host terminal force kill (C-D63) --------------------------------------------------------------
    def _manager_in_use(self) -> bool:
        info = self.host_info()
        return self.input_owner() == "manager" or bool(info.get("manager_command_in_flight"))

    def kill_confirm_lines(self) -> list[str]:
        lines = list(KILL_CONFIRM_LINES)
        if self._manager_in_use():
            lines.insert(3, KILL_MANAGER_WARNING)
        return lines

    def _pending_kind(self, kind: ClientType) -> bool:
        return any(pending_kind is kind for pending_kind, _ in self.pending.values())

    def _open_kill_confirm(self) -> None:
        """prefix k: ask before killing the host terminal; nothing reaches any pane meanwhile."""
        if self._pending_kind(ClientType.KILL_PANE):
            self.notice = KILLING_NOTICE
            return
        self.kill_confirm_open = True
        self.notice = ""
        self._cancel_ctrl_d()

    # -- confirmation modals (host terminal kill, automation pause/resume) ---------------------------------------
    @property
    def pause_confirm_open(self) -> bool:
        return self.pause_confirm is not None

    def _confirm_open(self) -> bool:
        return self.kill_confirm_open or self.pause_confirm is not None

    def _confirm_keys(self) -> tuple[str, ...]:
        """The command letters that confirm the modal now open (empty when none is)."""
        if self.kill_confirm_open:
            return KILL_KEYS
        return PAUSE_KEYS if self.pause_confirm is not None else ()

    @staticmethod
    def _confirm_starts(event: Any, keys: tuple[str, ...]) -> bool:
        """True when the event is (or starts with) a confirm form: the letter, its Ctrl form or (via the parser) its jamo."""
        if not keys:
            return False
        if isinstance(event, Command):
            return CTRL_ALIASES.get(event.key, event.key) in keys
        raw = tuple(k.encode() for k in keys) + tuple(c.encode() for c, k in CTRL_ALIASES.items() if k == keys[0])
        return isinstance(event, Passthrough) and event.data[:1] in raw

    @staticmethod
    def _cancelled_notice(keys: tuple[str, ...]) -> str:
        return KILL_CANCELLED_NOTICE if keys == KILL_KEYS else PAUSE_CANCELLED_NOTICE

    def _confirm_event(self, event: Any, *, single: bool = True) -> None:
        """While a confirmation is open no event reaches a pane: only one confirm key on its own confirms.

        ``single`` is False when the input chunk held more than that one event, and a Passthrough must be exactly one
        confirm key: 'kk' / 'kill' / 'k'+Enter (auto-repeat, typing, an unbracketed paste) cancel (C-D63 review R3).
        A lone prefix leaves it open (so ``Ctrl-] k`` pressed twice confirms); the key after it decides."""
        keys = self._confirm_keys()
        starts = self._confirm_starts(event, keys)
        if isinstance(event, Passthrough):
            confirmed = starts and len(event.data) == 1
        else:
            confirmed = starts  # a prefix command is one key; paste, mouse and page scroll never confirm
        confirmed = confirmed and single
        which = self.pause_confirm
        self.kill_confirm_open = False
        self.pause_confirm = None
        if confirmed and keys == KILL_KEYS:
            self._send(ClientType.KILL_PANE, pane=PaneId.HOST_SHELL)
            self.notice = KILLING_NOTICE
        elif confirmed and which == "resume":
            self._send(ClientType.RESUME, reconciled=True)
            self.notice = RESUMING_NOTICE
        elif confirmed:
            self._send(ClientType.PAUSE)
            self.notice = PAUSING_NOTICE
        else:
            self.notice = self._cancelled_notice(keys) if starts else ""

    # -- automation pause / resume (CW-18 U5) ---------------------------------------------------------------------
    def _automation(self) -> dict[str, Any]:
        value = self.state.get("automation")
        return value if isinstance(value, dict) else {}

    def pause_confirm_lines(self) -> list[str]:
        return list(RESUME_CONFIRM_LINES if self.pause_confirm == "resume" else PAUSE_CONFIRM_LINES)

    def _open_pause_confirm(self) -> None:
        """prefix p: ask before pausing (running) or resuming (paused, after reconciliation); nothing reaches a pane."""
        if self._pending_kind(ClientType.PAUSE) or self._pending_kind(ClientType.RESUME):
            self.notice = AUTOMATION_PENDING_NOTICE
            return
        automation = self._automation()
        state = automation.get("state")
        if state in ("pausing", "resuming"):
            self.notice = f"자동화 {'일시정지' if state == 'pausing' else '재개'} 진행 중 — 완료를 기다리세요"
            return
        if state == "paused" or automation.get("paused") is True:
            self.pause_confirm = "resume"
        elif isinstance(state, str) and state:
            self.pause_confirm = "pause"
        else:
            self.notice = "자동화 상태를 알 수 없어 일시정지/재개를 열 수 없습니다"
            return
        self.notice = ""
        self._cancel_ctrl_d()

    def _command(self, key: str) -> None:
        key = CTRL_ALIASES.get(key, key)
        if any(ord(ch) >= 128 for ch in key):
            self.notice = HANGUL_HINT  # IME text right after the prefix: never guessed, never forwarded
        elif key == " ":
            self.menu_open, self.menu_index, self.notice = True, 0, ""
        elif key in _ARROWS:
            self._resize_arrow(_ARROWS[key])
            self._repeat_until = self._last_now + RESIZE_REPEAT_SECONDS
        elif key == "z":
            self.toggle_zoom()
        elif key == "=":
            self.reset_layout()
        elif key in ("1", "2", "3"):
            self.set_focus(PANES[int(key) - 1])
        elif key == "\t":
            self.set_focus(PANES[(PANES.index(self.focus) + 1) % len(PANES)])
        elif key in "qQ" and len(key) == 1:
            self.quit = True
        elif key in ("d", "D", "\x04"):
            self.notice = DETACH_MOVED  # the old detach keys send nothing to any pane
        elif key in "tT" and len(key) == 1:
            self._send(ClientType.TAKEOVER_REQUEST)
        elif key in "cC" and len(key) == 1:
            self._send(ClientType.TAKEOVER_CONFIRM)
        elif key in "hH" and len(key) == 1:
            self._send(ClientType.HANDOFF)
        elif key in "rR" and len(key) == 1:
            self.redraw(self.focus)
            self._mouse_reassert = True
        elif key == "[":
            self.enter_scroll()
        elif key == "\x1b[5~":  # prefix PgUp: scroll mode + one page up
            self.enter_scroll()
            self.scroll_by(self._page())
        elif key in "mM" and len(key) == 1:
            self.mouse_capture = not self.mouse_capture
            self.notice = ("마우스 캡처 켜짐: 휠 스크롤 · 클릭 focus · 드래그: 복사 (터미널 자체 선택은 Shift+드래그)" if self.mouse_capture
                           else "마우스 캡처 꺼짐: 터미널 텍스트 선택 가능 (prefix m 으로 다시 켬)")
        elif key == "?":
            self.help_open = True
        elif key in KILL_KEYS:
            self._open_kill_confirm()
        elif key in PAUSE_KEYS:
            self._open_pause_confirm()
        else:
            self.notice = f"알 수 없는 prefix 명령 {key!r} (Ctrl-] Space 메뉴 · Ctrl-] ? 도움말)"

    def set_focus(self, pane: PaneId) -> None:
        self.exit_scroll()  # leaves scroll mode; panes scrolled directly keep their position
        self._cancel_ctrl_d()
        self._clear_selection()
        self.focus = pane
        self._send(ClientType.FOCUS, pane=pane)
        self.notice = ""
        self._sync_zoom()

    # -- mouse (SGR reports from the outer terminal) ---------------------------------------------------
    def _pane_at(self, x: int, y: int) -> tuple[PaneId, int, int] | None:
        """(pane, column, row) with 1-based pane-local coordinates for a 1-based terminal cell; None off the panes."""
        if self.too_small():
            return None
        col, row = x - 1, y - 1
        for pane, (top, left, height, width) in pane_boxes(self.rows, self.cols, self.layout, self.zoom).items():
            if top < row < top + height - 1 and left < col < left + width - 1:
                return pane, col - left, row - top
        return None  # border, header or footer

    def _mouse(self, event: Mouse) -> None:
        if not self.mouse_capture:
            return  # late report after the user turned capture off
        if self._selection_mouse(event):
            return  # motion / release of a drag-to-copy selection (also over borders and other panes)
        if not event.button & (32 | 64) and not event.release:
            self._clear_selection()  # any press ends a kept highlight
        if self._divider_mouse(event):
            return  # divider press/drag/release: never delivered to a pane, never a focus change
        hit = self._pane_at(event.x, event.y)
        if hit is None:
            return
        pane, column, row = hit
        view, button = self.panes[pane], event.button
        if button & 64:  # wheel: 64 = up (older output), 65 = down; 66/67 = horizontal (ignored)
            if button & 3 > 1 or event.release:
                return
            direction = 1 if button & 3 == 0 else -1
            if pane in self._views:
                self.scroll_by(direction * WHEEL_LINES, pane)  # a scrolled-back view keeps the wheel until live
            elif view.mouse_tracking:
                self._forward_mouse(pane, event, column, row)
            elif view.alt_screen:  # xterm alternate-scroll: no history to scroll, the wheel becomes arrow keys
                arrow = (b"\x1bOA" if direction > 0 else b"\x1bOB") if 1 in view.modes else (
                    b"\x1b[A" if direction > 0 else b"\x1b[B")
                self._send(ClientType.INPUT, arrow * WHEEL_LINES, pane)
            else:
                self.scroll_by(direction * WHEEL_LINES, pane)
        elif view.mouse_tracking:
            if button & 32 and not self._wants_motion(view, button):
                return  # the outer terminal reports motion (1002); this app did not ask for it
            self._forward_mouse(pane, event, column, row)
        elif button & 3 == 0 and not button & 32 and not event.release:
            if pane is not self.focus:
                self.set_focus(pane)  # left click focuses (same as prefix 1/2/3)
            self._begin_selection(pane, column - 1, row - 1)  # ... and may start a drag-to-copy selection

    @staticmethod
    def _wants_motion(view: PaneView, button: int) -> bool:
        if not view.modes & MOTION_MODES:
            return False
        return 1003 in view.modes or button & 3 != 3  # 1002: motion only while a button is held

    def take_mouse_reassert(self) -> bool:
        """True once after prefix r: the app loop re-sends the outer mouse mode (a multiplexer may have reset it)."""
        wanted, self._mouse_reassert = self._mouse_reassert, False
        return wanted

    def _forward_mouse(self, pane: PaneId, event: Mouse, column: int, row: int) -> None:
        """Re-encode the report in pane-local coordinates the way the pane's application asked for it."""
        if 1006 in self.panes[pane].modes:
            data = f"\x1b[<{event.button};{column};{row}{'m' if event.release else 'M'}".encode()
        elif column > 223 or row > 223:
            return  # the legacy X10 encoding cannot express it
        else:
            code = (event.button | 3) if event.release else event.button
            data = b"\x1b[M" + bytes((32 + code, 32 + column, 32 + row))
        self._send(ClientType.INPUT, data, pane)

    # -- drag-to-copy (C-D62): selection in pane content coordinates, copy through OSC 52 --------------------------
    def _view_top(self, pane: PaneId) -> int:
        """Absolute history line shown on the first row of ``pane``'s inner area."""
        return self._history(pane).pushed - self.scroll_offset(pane)

    def _selection(self) -> _Selection | None:
        sel = self._sel
        if sel is not None and (self._history(sel.pane) is not sel.queue
                                or self.panes[sel.pane].screen.resets != sel.resets):
            self._sel = sel = None  # alternate-screen swap / reset (RIS): the content the selection pointed at is gone
        if sel is not None:
            sel = self._clamp_to_history(sel)
        return sel

    def _clamp_to_history(self, sel: _Selection) -> _Selection | None:
        """Rows trimmed out of the scrollback under a selection are dropped from it (never copied as other text);
        a selection with no row left is cancelled."""
        queue = self._history(sel.pane)
        base = queue.pushed - len(queue)  # the oldest absolute line that still exists
        if min(sel.anchor[0], sel.cursor[0]) >= base:
            return sel
        if max(sel.anchor[0], sel.cursor[0]) < base:
            self._sel = None
            if sel.moved:
                self.notice = SELECTION_EVICTED_NOTICE
            return None
        if sel.anchor[0] < base:
            sel.anchor = (base, 0)
        if sel.cursor[0] < base:
            sel.cursor = (base, 0)
        return sel

    def _clear_selection(self) -> None:
        self._sel = None

    def _begin_selection(self, pane: PaneId, column: int, row: int) -> None:
        cell = (self._view_top(pane) + row, column)
        self._sel = _Selection(pane, self._history(pane), cell, cell, column=column,
                               resets=self.panes[pane].screen.resets)

    def _selection_mouse(self, event: Mouse) -> bool:
        """Motion and release of a drag in progress. True when the event was consumed."""
        sel, button = self._selection(), event.button
        if sel is None or not sel.dragging or button & 64:
            return False
        if not event.release and not button & 32:
            return False  # another press: a fresh click (the old drag is dropped by the caller)
        if sel.pane not in pane_boxes(self.rows, self.cols, self.layout, self.zoom):
            self._sel = None
            return True
        self._follow(sel, event, scroll=not event.release)
        if event.release or button & 3:  # release, or motion with the left button no longer held (lost release)
            sel.dragging = False
            self._finish_selection(sel)
        return True

    def _follow(self, sel: _Selection, event: Mouse, *, scroll: bool) -> None:
        """Move the selection's end to the pointer, clamped to the pane's inner area; scroll past its edges."""
        top, left, height, width = pane_boxes(self.rows, self.cols, self.layout, self.zoom)[sel.pane]
        rows, cols = max(1, height - 2), max(1, width - 2)
        row = (event.y - 1) - top - 1
        sel.column = min(max((event.x - 1) - left - 1, 0), cols - 1)
        sel.edge = -1 if row < 0 else 1 if row >= rows else 0
        self._extend(sel, min(max(row, 0), rows - 1), rows, scroll=scroll)

    def _extend(self, sel: _Selection, row: int, rows: int, *, scroll: bool) -> None:
        now = self._mono()
        if sel.edge and scroll and now >= sel.next_scroll:
            sel.next_scroll = now + AUTOSCROLL_SECONDS
            self.scroll_by(-sel.edge, sel.pane)  # above: older output (+1), below: towards live (-1)
        if sel.edge:
            row = 0 if sel.edge < 0 else rows - 1
        sel.cursor = (self._view_top(sel.pane) + row, sel.column)

    def autoscroll_tick(self) -> bool:
        """The app loop calls this every iteration: a drag held outside its pane keeps scrolling. True on a step."""
        sel = self._selection()
        if sel is None or not sel.dragging or not sel.edge or self._mono() < sel.next_scroll:
            return False
        boxes = pane_boxes(self.rows, self.cols, self.layout, self.zoom)
        if sel.pane not in boxes:
            return False
        rows = max(1, boxes[sel.pane][2] - 2)
        self._extend(sel, 0, rows, scroll=True)
        return True

    def _finish_selection(self, sel: _Selection) -> None:
        if not sel.moved:
            self._sel = None  # a click without movement is only a focus click
            return
        text = self._selection_text(sel)
        if not text.strip():
            self.notice = "선택 영역이 비어 있어 복사하지 않음"
            return
        data = text.encode()
        if len(data) > MAX_COPY_BYTES:
            self.notice = f"복사 거부: 선택이 1 MiB를 넘음 ({len(data)} bytes)"
            return
        osc = b"\x1b]52;c;" + base64.b64encode(data) + b"\x07"
        environ = os.environ if self._environ is None else self._environ
        tmux = bool(environ.get("TMUX"))
        # inside tmux the plain sequence works with set-clipboard on; the passthrough copy with allow-passthrough on
        self._out = osc + (b"\x1bPtmux;" + osc.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\" if tmux else b"")
        self.notice = f"복사됨: {len(text)}자" + (TMUX_HINT if tmux else "")

    def take_output(self) -> bytes:
        """Terminal bytes queued for the app loop to write (OSC 52 copy); empty when there are none."""
        out, self._out = self._out, b""
        return out

    def _selection_text(self, sel: _Selection) -> str:
        pane = sel.pane
        queue = self._history(pane)
        history, pushed = list(queue), queue.pushed
        base = pushed - len(history)
        screen = self.panes[pane].screen
        (first, first_col), (last, last_col) = sel.span()
        rows = []
        for line_no in range(first, last + 1):
            if line_no < pushed:
                line = history[line_no - base] if 0 <= line_no - base < len(history) else {}
            else:
                line = screen.buffer.get(line_no - pushed, {}) if line_no - pushed < screen.lines else {}
            start = first_col if line_no == first else 0
            end = last_col if line_no == last else screen.columns - 1
            if start > 0 and start in line and not line[start].data and line.get(start - 1) is not None:
                start -= 1  # began on the trailing half of a wide character: include the character
            cells = (line.get(x, screen.default_char).data for x in range(start, end + 1))
            rows.append("".join(cells).rstrip(" "))
        return "\n".join(rows)

    def selection_spans(self, pane: PaneId, rows: int) -> dict[int, tuple[int, int]]:
        """Highlighted cells per visible row of ``pane``: {row: (first column, last column)} (inclusive)."""
        sel = self._selection()
        if sel is None or sel.pane is not pane or not sel.moved:
            return {}
        (first, first_col), (last, last_col) = sel.span()
        top, columns = self._view_top(pane), self.panes[pane].screen.columns
        spans = {}
        for y in range(rows):
            line_no = top + y
            if first <= line_no <= last:
                spans[y] = (first_col if line_no == first else 0, last_col if line_no == last else columns - 1)
        return spans

    # -- scrolling (view only: never mutates the live pyte screen or cursor) -------------------------------
    def _history(self, pane: PaneId) -> _CountingDeque:
        return self.panes[pane].screen.history.top

    @property
    def scroll_pane(self) -> PaneId | None:
        """The pane in explicit scroll mode (prefix [); panes scrolled directly are not 'in' the mode."""
        return self._mode

    def scrolled(self, pane: PaneId) -> bool:
        """True while ``pane``'s view is behind (or held apart from) its live screen."""
        self._sync_scroll()
        return pane in self._views

    def _live_for_input(self) -> None:
        """Typing/pasting into a directly scrolled pane first returns its view to live (no extra key needed)."""
        self._views.pop(self.focus, None)

    def _open_view(self, pane: PaneId) -> _ScrollView:
        queue = self._history(pane)
        view = self._views.get(pane)
        if view is None or view.queue is not queue:
            view = self._views[pane] = _ScrollView(queue, queue.pushed)  # offset 0: exactly the live screen
        return view

    def enter_scroll(self, pane: PaneId | None = None) -> None:
        pane = pane or self.focus
        if self._mode is not None and self._mode is not pane:
            self.exit_scroll()
        self._open_view(pane)  # an already scrolled pane keeps its place
        self._mode = pane
        self.notice = ""

    def exit_scroll(self) -> None:
        """Leave scroll mode; that pane shows its live screen again. Directly scrolled panes are untouched."""
        if self._mode is not None:
            self._views.pop(self._mode, None)
        self._mode = None

    def _history_cleared(self, view: PaneView) -> bool:
        """The pane's history was wiped (catch-up or a replaced session): its scrolled view returns to live."""
        pane = next(p for p, v in self.panes.items() if v is view)
        if self._sel is not None and self._sel.pane is pane:
            self._sel = None  # its content is gone
        if pane not in self._views:
            return False
        self._views.pop(pane)
        if self._mode is pane:
            self._mode = None
        self.notice = f"[{TITLES[pane]}] 출력 기록이 초기화되어 live로 복귀"
        return True

    def _sync_scroll(self) -> None:
        # alternate-screen switches replace the history queue: the view no longer refers to anything
        for pane, view in list(self._views.items()):
            if self._history(pane) is not view.queue:
                del self._views[pane]
                if self._mode is pane:
                    self._mode = None

    def scroll_offset(self, pane: PaneId | None = None) -> int:
        """Lines the view is behind the live screen (0 = live), clamped to the history that still exists."""
        self._sync_scroll()
        pane = pane or self._mode or self.focus
        view = self._views.get(pane)
        if view is None:
            return 0
        queue = view.queue
        offset = min(max(queue.pushed - view.anchor, 0), len(queue))
        view.anchor = queue.pushed - offset  # new output does not move the view; evicted lines clamp it
        return offset

    def scroll_history(self, pane: PaneId | None = None) -> int:
        self._sync_scroll()
        pane = pane or self._mode or self.focus
        return len(self._history(pane)) if pane in self._views else 0

    def scroll_by(self, lines: int, pane: PaneId | None = None) -> None:
        """Positive = further back in history, negative = towards live. Reaching live ends a direct scroll."""
        pane = pane or self._mode or self.focus
        self._sync_scroll()
        if pane not in self._views:
            if lines <= 0 or not len(self._history(pane)):
                return
            self._open_view(pane)
        view = self._views[pane]
        target = min(max(self.scroll_offset(pane) + lines, 0), len(view.queue))
        view.anchor = view.queue.pushed - target
        if not target and pane is not self._mode:
            del self._views[pane]

    def _page(self, pane: PaneId | None = None) -> int:
        pane = pane or self._mode or self.focus
        return max(1, self.sizes[pane][0] - 1)

    def _scroll_input(self, data: bytes) -> None:
        index = 0
        while index < len(data):
            length = 1
            if data[index] == 0x1B and index + 1 < len(data):
                length = _sequence_length(data[index:]) or len(data) - index
            key = _SCROLL_KEYS.get(bytes(data[index:index + length]))
            index += length
            if key == "quit":
                self.exit_scroll()
                return
            if key == "pgup":
                self.scroll_by(self._page())
            elif key == "pgdn":
                self.scroll_by(-self._page())
            elif key == "up":
                self.scroll_by(1)
            elif key == "down":
                self.scroll_by(-1)
            elif key == "home":
                self.scroll_by(self.scroll_history())
            elif key == "end":
                self.scroll_by(-self.scroll_history())

    def pane_lines(self, pane: PaneId, rows: int) -> list[Any]:
        """The ``rows`` lines to draw for ``pane``: cell mappings with ``.get(x, default_char)``.

        Live: the screen buffer. While ``pane`` is scrolled: a window over history + the live screen,
        built from copies of references (the screen itself is only read).
        """
        screen = self.panes[pane].screen
        offset = self.scroll_offset(pane) if pane in self._views else 0
        if not offset:
            return [screen.buffer.get(y, {}) for y in range(min(rows, screen.lines))]
        history = list(self._history(pane))
        start, lines = len(history) - offset, []
        for index in range(start, start + rows):
            if index < len(history):
                lines.append(history[index])
            else:
                row = index - len(history)
                lines.append(screen.buffer.get(row, {}) if row < screen.lines else {})
        return lines

    # -- layout / resize -------------------------------------------------
    def too_small(self) -> bool:
        return self.rows < MIN_ROWS or self.cols < MIN_COLS

    def resize(self, rows: int, cols: int, *, force: bool = False) -> None:
        self.rows, self.cols = rows, cols
        self.pause_confirm = None  # ... and the automation pause/resume one (CW-18 U5)
        self.kill_confirm_open = False  # a resize cancels the host-shell kill confirmation (C-D63): like any non-confirm input
        if self.too_small():
            self._end_drag()
            return
        self._relayout(force=force)

    def _relayout(self, *, force: bool = False, defer: bool = False, send: bool = True) -> None:
        """Recompute the pane sizes (window resize, split move, zoom); resize the screens; send per-pane frames.

        ``defer`` (a divider drag) sends at most once per ``DRAG_RESIZE_DEBOUNCE``; ``flush_resizes`` sends the rest.
        Scroll positions are kept: a scrolled view is clamped to the history that still exists on the next read.
        """
        if self.too_small():
            return
        sizes = pane_inner_sizes(self.rows, self.cols, self.layout, self.zoom)
        for pane in PANES:
            r, c = sizes[pane]
            if force or sizes[pane] != self.sizes[pane]:
                self._sel = None  # cell positions change with the size
                self.panes[pane].screen.resize(lines=r, columns=c)
                if send:
                    self._unsent.add(pane)
        self.sizes = sizes
        self._sync_scroll()
        if send:
            self.flush_resizes(final=not defer)

    def flush_resizes(self, *, final: bool = False) -> None:
        """Send the pending per-pane resize frames (actual inner sizes). Rate limited while a divider is dragged."""
        if not self._unsent or self.too_small():
            return
        now = self._mono()
        if not final and self._drag is not None and now - self._last_resize_sent < DRAG_RESIZE_DEBOUNCE:
            return
        for pane in PANES:
            if pane in self._unsent:
                r, c = self.sizes[pane]
                self._send(ClientType.RESIZE, pane=pane, rows=r, cols=c)
        self._unsent.clear()
        self._last_resize_sent = now

    # -- adjustable splits (C-D59) ---------------------------------------------------------------------
    @property
    def drag_active(self) -> bool:
        return self._drag is not None

    @property
    def resize_repeat_active(self) -> bool:
        return self._repeat_until is not None and self._last_now < self._repeat_until

    def set_layout_state(self, layout: Layout, zoom: bool) -> None:
        """Adopt a stored layout before attach: sizes only, nothing is sent (``after_attach`` sends every pane)."""
        self.layout = layout
        self.zoom = self.focus if zoom else None
        self._relayout(send=False)

    def take_layout_state(self) -> tuple[Layout, bool]:
        self.layout_dirty = False
        return self.layout, self.zoom is not None

    def _divider_state(self) -> tuple[int, int, int, int]:
        """(body top row, top-row box height, body height, manager box width) of the un-zoomed layout."""
        body = max(2, self.rows - HEADER_ROWS - FOOTER_ROWS)
        return HEADER_ROWS, top_height(body, self.layout.row_ratio), body, left_width(self.cols, self.layout.col_ratio)

    def _set_split(self, axis: str, size: int, *, defer: bool = False) -> bool:
        """Move a divider to ``size`` (manager box width / top-row box height), clamped. True when it moved."""
        _, top_h, body, left_w = self._divider_state()
        if axis == "v":
            size = clamp_width(self.cols, size)
            if size == left_w:
                return False
            layout = Layout(size / self.cols, self.layout.row_ratio)
        else:
            size = clamp_height(body, size)
            if size == top_h:
                return False
            layout = Layout(self.layout.col_ratio, size / body)
        self.layout, self.layout_dirty = layout, True
        self._relayout(defer=defer)
        _, top_h, body, left_w = self._divider_state()
        self.notice = (f"분할: manager {left_w * 100 // max(1, self.cols)}% | worker "
                       f"{100 - left_w * 100 // max(1, self.cols)}% · 위 {top_h * 100 // body}% / host "
                       f"{100 - top_h * 100 // body}%")
        return True

    def _resize_arrow(self, arrow: str) -> None:
        if self.too_small():
            return
        if self.zoom is not None:
            self.notice = "확대 중: prefix z 로 복원한 뒤 경계를 조절하세요"
            return
        _, top_h, _, left_w = self._divider_state()
        if arrow in ("left", "right"):
            moved = self._set_split("v", left_w + (RESIZE_STEP_COLS if arrow == "right" else -RESIZE_STEP_COLS))
        else:
            moved = self._set_split("h", top_h + (RESIZE_STEP_ROWS if arrow == "down" else -RESIZE_STEP_ROWS))
        if not moved:
            self.notice = "더 이상 조절할 수 없음: pane 최소 크기"

    def toggle_zoom(self) -> None:
        """Show only the focused pane over the whole body (others hidden), or restore the split."""
        self.zoom = None if self.zoom is not None else self.focus
        self.layout_dirty = True
        self._relayout()
        self.notice = f"[{TITLES[self.zoom]}] 확대 — prefix z 로 복원" if self.zoom is not None else "확대 해제"

    def reset_layout(self) -> None:
        """prefix =: default split, no zoom."""
        self.layout, self.zoom, self.layout_dirty = DEFAULT_LAYOUT, None, True
        self._relayout()
        self.notice = "배치 초기화"

    def _sync_zoom(self) -> None:
        """The zoomed pane follows the focus, so a hidden pane is never the focused one."""
        if self.zoom is not None and self.zoom is not self.focus:
            self.zoom = self.focus
            self.layout_dirty = True
            self._relayout()

    def _divider_hit(self, x: int, y: int) -> tuple[str, int] | None:
        """(axis, grab offset) when the 1-based cell lies on a divider line: the two borders facing each other."""
        if self.zoom is not None or self.too_small():
            return None
        col, row = x - 1, y - 1
        top, top_h, _, left_w = self._divider_state()
        bottom = top + top_h  # first row of the host shell box
        if row in (bottom - 1, bottom) and 0 <= col < self.cols:
            return "h", row - bottom
        if top <= row < bottom - 1 and col in (left_w - 1, left_w):
            return "v", col - left_w
        return None

    def _drag_to(self, x: int, y: int, *, defer: bool) -> None:
        axis, offset = self._drag
        if axis == "v":
            self._set_split("v", (x - 1) - offset, defer=defer)
        else:
            self._set_split("h", (y - 1) - offset - HEADER_ROWS, defer=defer)

    def abort_drag(self) -> None:
        """Forget a drag in progress without sending anything (the UI is leaving)."""
        self._drag = None

    def _end_drag(self) -> None:
        if self._drag is not None:
            self._drag = None
            self.flush_resizes(final=True)

    def _divider_mouse(self, event: Mouse) -> bool:
        """Divider press starts a drag, motion moves it, release ends it. True when the event was consumed."""
        button = event.button
        if button & 64:
            return False  # wheel: normal pane handling (borders are not panes)
        if self._drag is not None:
            if event.release:
                self._drag_to(event.x, event.y, defer=True)
                self._end_drag()
                return True
            if button & 32:  # motion; left button still held?
                if button & 3 == 0:
                    self._drag_to(event.x, event.y, defer=True)
                else:
                    self._end_drag()  # the release report was lost
                return True
            self._end_drag()  # another press: handled below as a fresh event
        hit = self._divider_hit(event.x, event.y)
        if hit is None:
            return False
        if button & 3 == 0 and not button & 32 and not event.release:
            self._drag = hit
        return True

    def redraw(self, pane: PaneId) -> None:
        """Nudge the application's window size so it repaints at the real size."""
        r, c = self.sizes[pane]
        self._send(ClientType.RESIZE, pane=pane, rows=max(1, r - 1), cols=c)
        self._send(ClientType.RESIZE, pane=pane, rows=r, cols=c)

    def after_attach(self) -> None:
        self._unsent.clear()
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

    def shown_owner(self) -> str:
        """The host owner as shown: ``worker`` while the worker's terminal command runs (smoke-01 P2).

        The shell is claimed by the manager side then (managed dispatch), so
        ``input_owner`` (used for takeover logic) stays ``manager``.
        """
        owner = self.input_owner()
        if owner == "manager" and self.host_info().get("operated_by") == "worker":
            return "worker"
        return owner

    def pane_status(self, pane: PaneId) -> str:
        info = self.state.get("panes", {}).get(pane.value)
        if not info:
            return "unknown"
        if info.get("alive"):
            return "alive"
        code = info.get("exit_status")
        return f"exited({code})" if code is not None else "exited"

    def pane_exited(self, pane: PaneId) -> bool:
        """A pane whose process the backend reports as not alive (OMP: C-D62, host shell: C-D63)."""
        if pane not in RESTARTABLE:
            return False
        info = self.state.get("panes", {}).get(pane.value)
        return isinstance(info, dict) and not info.get("alive")

    def restart_notice(self, pane: PaneId) -> str | None:
        """Text drawn inside an exited pane (OMP: C-D62, host shell: C-D63); None for a live pane."""
        if not self.pane_exited(pane):
            return None
        if pane in self._restarting:
            return RESTARTING_NOTICE
        code = self.state["panes"][pane.value].get("exit_status")
        if pane is PaneId.HOST_SHELL:
            return f"host terminal 종료됨 (exit {'?' if code is None else code}) — Enter: 새 shell 시작"
        return (f"OMP 종료됨 (exit {'?' if code is None else code}) — Enter: 새 세션으로 다시 시작 · "
                "이전 대화는 새 OMP에서 /resume")

    def restart_notice_lines(self, pane: PaneId, cols: int) -> list[str]:
        """``restart_notice`` word-wrapped to ``cols`` display cells (long words are cut); empty when none."""
        text = self.restart_notice(pane)
        if text is None:
            return []
        cols, lines, current = max(1, cols), [], ""
        for word in text.split():
            while wcswidth(word) > cols:  # a word wider than the pane is cut
                cut = len(word)
                while cut > 1 and wcswidth(word[:cut]) > cols:
                    cut -= 1
                if current:
                    lines.append(current)
                    current = ""
                lines.append(word[:cut])
                word = word[cut:]
            joined = f"{current} {word}" if current else word
            if wcswidth(joined) <= cols:
                current = joined
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    def pane_title(self, pane: PaneId) -> str:
        extra = ""
        if pane is PaneId.HOST_SHELL:
            extra = f" owner={self.shown_owner()}"
        note = " 따라잡음" if self.catching_up(pane) else ""
        if self.pane_exited(pane):
            note += " 다시 시작 중…" if pane in self._restarting else " 종료됨 (Enter: 다시 시작)"
        scroll = ""
        if self.scrolled(pane):
            offset = self.scroll_offset(pane)
            history = self.scroll_history(pane)
            where = f"live보다 {offset}줄 위/{history}" if offset else f"live 끝/{history}"
            scroll = f" [SCROLL {where}]"
        zoom = " [ZOOM]" if pane is self.zoom else ""
        return (f" {TITLES[pane]}{zoom}{scroll}{' *FOCUS*' if pane is self.focus else ''} "
                f"{self.pane_status(pane)}{extra}{note} ")

    def status_lines(self) -> tuple[str, str]:
        shell = self.host_info().get("shell") or {}
        mode = shell.get("parent_mode", "unknown")
        automation = _plain(self._automation().get("state")) or "unknown"
        bridge = self.state.get("bridge")
        bridge = bridge if isinstance(bridge, dict) else {}

        def peer(role: str) -> str:
            value = bridge.get(role)
            return "?" if not isinstance(value, dict) else ("ok" if value.get("connected") else "down")

        seen = "없음"
        if self.last_seen is not None:
            age = max(0, int(self.clock() - self.last_seen))
            seen = f"{time.strftime('%H:%M:%S', time.localtime(self.last_seen))} ({age}s 전)"
        worker = self.worker_text()
        line1 = (f"focus: {TITLES[self.focus]} | host 입력 owner: {self.shown_owner()} | "
                 f"shell mode: {mode}" + (f" | worker: {worker}" if worker else "") + f" | 자동화: {automation}")
        line2 = (f"backend: {self.state.get('phase', 'unknown')} | bridge manager={peer('manager')} "
                 f"worker={peer('worker')} | 마지막 확인 {seen} | Ctrl-] ? 도움말")
        extra = " | ".join(part for part in (self.task_text(), self.automation_text()) if part)
        if extra:
            line2 = f"{extra} | {line2}"
        warning = self.isolation_warning()
        if warning:
            line2 = f"{warning} | {line2}"
        return line1, line2

    # -- CW-18 U5: current Task / worker / automation (all fields optional; unknown values are shown as sent) --------
    def worker_text(self) -> str:
        """대기 / 작업 중 from the snapshot ``worker``; empty when an older backend sends none."""
        worker = self.state.get("worker")
        if not isinstance(worker, dict):
            return ""
        state = _plain(worker.get("state"))
        return WORKER_TEXT.get(state) or state or "?"

    def task_text(self) -> str:
        """``작업: 실험 "summary" [실행 중 · host terminal 사용 중]`` for the snapshot ``task`` (current, else last)."""
        task = self.state.get("task")
        if not isinstance(task, dict):
            return ""
        kind = _plain(task.get("kind"))
        status = _plain(task.get("status"))
        status_text = TASK_STATUS_TEXT.get(status) or status or "?"
        if status == "closed":
            reason = _plain(task.get("closed_reason"))
            status_text = f"종료({TASK_CLOSED_TEXT.get(reason) or reason})" if reason else status_text
        notes = []
        held = _plain(task.get("held_reason"))
        last = task.get("last_result") if isinstance(task.get("last_result"), dict) else {}
        if status == "finished" and (held.startswith("start_failed:")
                                     or _plain(last.get("outcome")) in ("start_failed", "not_started")):
            # CW-18 smoke-02 E4: the run never started, so nothing was reported.
            status_text = "시작 실패"
            held = held.removeprefix("start_failed:") or _plain(last.get("error")) or _plain(last.get("reason"))
        if held:
            notes.append(TASK_HELD_TEXT.get(held) or held)
            if held == "host_terminal_busy" and any(r in JOB_HELD_REASONS for r in self._held_reasons({})):
                notes.append(JOB_HELD_TASK_NOTE)
        if task.get("cancel_requested") is True and status != "cancelling":
            notes.append("취소 요청됨")
        tail = f" [{' · '.join([status_text] + notes)}]"
        head = f"작업: {TASK_KIND_TEXT.get(kind) or kind or '?'}"
        summary = _plain(task.get("summary"))
        if summary:
            summary = _clip(summary, max(SUMMARY_MIN_COLS, min(SUMMARY_MAX_COLS, self.cols // 3)))
            head += f' "{summary}"'
        return head + tail

    def automation_text(self) -> str:
        """Review timer / held reason / interruption / refused resume of the snapshot ``automation``."""
        automation = self._automation()
        state = automation.get("state")
        paused = state in ("paused", "pausing") or automation.get("paused") is True
        parts: list[str] = []
        if state == "held":
            tick = automation.get("tick")
            problems = tick.get("problems") if isinstance(tick, dict) else None
            why = _plain(automation.get("detail")) or (
                _plain(", ".join(str(p) for p in problems)) if isinstance(problems, (list, tuple)) else "")
            parts.append(f"보류: {why}" if why else "보류")
        if state == "pausing":
            parts.append("일시정지 중")
        elif state == "resuming":
            parts.append("재개 중")
        review = automation.get("review")
        if not paused and isinstance(review, dict) and review.get("applies") is True:
            if review.get("status") == "delayed":
                why = _plain(review.get("reason"))
                count = review.get("coalesced_count")
                merged = f" (합침 {count})" if isinstance(count, int) and not isinstance(count, bool) and count > 0 else ""
                parts.append(f"대조 지연: {why or '?'}{merged}")
            else:
                due = review.get("next_due_in_seconds")
                interval = review.get("interval_seconds")
                if isinstance(due, (int, float)) and not isinstance(due, bool):
                    every = f"{int(interval)}s " if isinstance(interval, (int, float)) and not isinstance(interval, bool) else ""
                    parts.append(f"{every}대조 {int(max(0, due))}s 후")
        if paused:
            if state == "paused":
                parts.append("일시정지됨")
            interruption = automation.get("interruption")
            if isinstance(interruption, dict):
                text = INTERRUPTION_TEXT.get(_plain(interruption.get("state")))
                if text:
                    parts.append(text)
            resume = automation.get("resume")
            if isinstance(resume, dict) and resume.get("outcome") == "refused":
                parts.append(f"재개 거부: {_plain(resume.get('reason')) or '?'}")
        if automation.get("persistence_error"):
            parts.append("저장 오류")
        return " · ".join(parts)

    def isolation_warning(self) -> str | None:
        """The backend's OMP isolation warning (snapshot ``omp_isolation``) once its check found a problem."""
        isolation = self.state.get("omp_isolation")
        if not isinstance(isolation, dict) or not isolation.get("checked") or isolation.get("state") == "ok":
            return None
        state = isolation.get("state") or "unknown"
        detail = isolation.get("warning") or "no detail"
        return f"경고 OMP 격리 {state}: {detail}"

    def footer(self) -> str:
        if self.closed_reason:
            return self.notice
        if self.kill_confirm_open:
            return KILL_CONFIRM_FOOTER
        if self.pause_confirm is not None:
            return RESUME_CONFIRM_FOOTER if self.pause_confirm == "resume" else PAUSE_CONFIRM_FOOTER
        if self.menu_open:
            return self.notice or "명령 메뉴: ↑/↓ + Enter · 숫자 1-9/0 · Esc 취소 · 키는 pane으로 전달되지 않음"
        if self.parser.prefix_active:
            return ("prefix… Space 메뉴 · 한글 IME는 Ctrl+q/t/y(확인)/o(handoff)/r/e(mouse)/z · 1/2/3 Tab [ = ? · "
                    "영문 q t c h r m z · ←↑↓→ 분할 · Ctrl-] 한 번 더 = 리터럴")
        if self.resize_repeat_active:
            return (f"{self.notice} · " if self.notice else "") + "←↑↓→ 계속 조절 · 다른 키는 종료 후 그대로 처리"
        if self._mode is not None:
            return self.notice or ("SCROLL: PgUp/PgDn ↑/↓ Home/End (g/G) 이동 · q/Esc 종료 · "
                                   "키는 pane으로 전달되지 않음")
        if self.scrolled(self.focus):
            return self.notice or "SCROLL: 휠/Shift+PgUp·PgDn 이동 · 입력하면 live로 복귀"
        default = "원본 키는 focus pane으로 그대로 전달됩니다 · Ctrl-] Space 메뉴 · Ctrl-] q/Ctrl-q = detach"
        if self.mouse_capture:
            default += " · 휠/Shift+PgUp 스크롤 · 드래그: 복사 · Shift+드래그: 터미널 선택"
        return self.notice or default
