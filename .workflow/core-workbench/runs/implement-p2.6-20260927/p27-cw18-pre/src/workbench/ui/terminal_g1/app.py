"""Three-PTY, three-pane terminal feasibility UI for CW-02 (G1)."""
from __future__ import annotations

import argparse
import curses
import os
import selectors
import shlex
import signal
import sys
import termios
import time
from collections import deque
from pathlib import Path

from wcwidth import wcswidth

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.terminal.vt_g1.session import PtySession
from workbench.ui.terminal_g1.input_router import BracketedPaste, InputRouter, PasteRejected


DEFAULT_OMP = "omp --no-session --no-tools --no-pty --no-extensions --no-skills --no-rules --no-title"
DEFAULT_HOST = "/bin/bash --noprofile --norc -i"
MAX_APP_OUTBOUND_BYTES = 64 * 1024
MAX_QUERY_REPLY_BYTES_PER_OUTPUT_BYTE = 32
MAX_SOURCE_READ_BYTES = 65536
_resize_pending = False


def _on_resize(_signum: int, _frame: object) -> None:
    global _resize_pending
    _resize_pending = True


def _outer_size(fd: int) -> tuple[int, int]:
    import fcntl
    import struct

    rows, columns, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    return max(1, rows), max(1, columns)


def _color_index(value: str, colors: int) -> int:
    named = {
        "black": 0,
        "red": 1,
        "green": 2,
        "brown": 3,
        "blue": 4,
        "magenta": 5,
        "cyan": 6,
        "white": 7,
        "brightblack": 8,
        "brightred": 9,
        "brightgreen": 10,
        "brightyellow": 11,
        "brightblue": 12,
        "brightmagenta": 13,
        "brightcyan": 14,
        "brightwhite": 15,
    }
    lowered = value.lower()
    if lowered in named:
        index = named[lowered]
    elif lowered.isdigit():
        index = int(lowered)
    elif len(lowered) == 6 and all(char in "0123456789abcdef" for char in lowered):
        rgb = tuple(int(lowered[offset : offset + 2], 16) for offset in (0, 2, 4))
        cube = tuple(min(5, round(component / 255 * 5)) for component in rgb)
        index = 16 + 36 * cube[0] + 6 * cube[1] + cube[2]
    else:
        return -1
    if colors < 16:
        return index % max(1, colors)
    return min(colors - 1, max(0, index))


class _ColorPairs:
    def __init__(self) -> None:
        self.enabled = bool(curses.has_colors())
        self.colors = 0
        self.pair_limit = 0
        self.pairs: dict[tuple[int, int], int] = {}
        self.next_pair = 1
        if self.enabled:
            try:
                curses.start_color()
                curses.use_default_colors()
                self.colors = max(8, curses.COLORS)
                self.pair_limit = curses.COLOR_PAIRS
            except curses.error:
                self.enabled = False

    def pair(self, fg: str, bg: str) -> int:
        if not self.enabled:
            return 0
        foreground = _color_index(fg, self.colors) if fg != "default" else -1
        background = _color_index(bg, self.colors) if bg != "default" else -1
        key = (foreground, background)
        if key in self.pairs:
            return self.pairs[key]
        if self.next_pair >= self.pair_limit:
            return 0
        pair_number = self.next_pair
        self.next_pair += 1
        try:
            curses.init_pair(pair_number, foreground, background)
        except curses.error:
            return 0
        self.pairs[key] = pair_number
        return pair_number


def _attributes(cell: object, colors: _ColorPairs) -> int:
    attrs = curses.color_pair(colors.pair(cell.fg, cell.bg))
    if cell.bold:
        attrs |= curses.A_BOLD
    if cell.underscore:
        attrs |= curses.A_UNDERLINE
    if cell.reverse:
        attrs |= curses.A_REVERSE
    if cell.blink:
        attrs |= curses.A_BLINK
    if cell.italics and hasattr(curses, "A_ITALIC"):
        attrs |= curses.A_ITALIC
    if cell.strikethrough and hasattr(curses, "A_STRIKEOUT"):
        attrs |= curses.A_STRIKEOUT
    return attrs


def _rgb(value: str) -> bool:
    return len(value) == 6 and all(char in "0123456789abcdef" for char in value.lower())


def _rgb_cell_style(cell: object) -> str:
    """Preserve exact RGB after curses' palette-only layout refresh."""
    codes = ["0"]
    for name, code in (("bold", 1), ("italics", 3), ("underscore", 4),
                       ("blink", 5), ("reverse", 7), ("strikethrough", 9)):
        if getattr(cell, name):
            codes.append(str(code))
    for value, base, default in ((cell.fg, 38, 39), (cell.bg, 48, 49)):
        if value == "default":
            codes.append(str(default))
        elif _rgb(value):
            rgb = ";".join(str(int(value[offset:offset + 2], 16)) for offset in (0, 2, 4))
            codes.append(f"{base};2;{rgb}")
        else:
            codes.append(f"{base};5;{_color_index(value, 256)}")
    return "\x1b[" + ";".join(codes) + "m"


def _draw(
    stdscr: curses.window,
    panes: list[tuple[str, PtySession, TerminalScreen]],
    focus: int,
    colors: _ColorPairs,
    notice: str = "",
) -> None:
    rows, columns = stdscr.getmaxyx()
    stdscr.erase()
    header = "OMP Workbench G1 | F6 focus  F7/F8 scrollback  F10 exit"
    stdscr.addnstr(0, 0, header, max(1, columns - 1), curses.A_BOLD)
    content_top = 1
    content_height = max(1, rows - 2)
    base_width = max(1, columns // 3)
    starts = (0, base_width, base_width * 2)
    widths = (base_width, base_width, max(1, columns - base_width * 2))
    rgb_output: list[str] = []
    rgb_last: tuple[int, int] | None = None
    rgb_style = ""

    for index, ((title, session, screen), left, width) in enumerate(zip(panes, starts, widths)):
        height = content_height
        inner_rows = max(1, height - 2)
        inner_columns = max(1, width - 2)
        status = session.poll()
        label = f" {title}{' *' if index == focus else ''} pid={session.pid} {'running' if status is None else 'exit='+str(status)} "
        if height >= 2 and width >= 2:
            try:
                stdscr.addch(content_top, left, curses.ACS_ULCORNER)
                stdscr.addch(content_top + height - 1, left, curses.ACS_LLCORNER)
                stdscr.addch(content_top, left + width - 1, curses.ACS_URCORNER)
                stdscr.addch(content_top + height - 1, left + width - 1, curses.ACS_LRCORNER)
                for column in range(left + 1, left + width - 1):
                    stdscr.addch(content_top, column, curses.ACS_HLINE)
                    stdscr.addch(content_top + height - 1, column, curses.ACS_HLINE)
                for row in range(content_top + 1, content_top + height - 1):
                    stdscr.addch(row, left, curses.ACS_VLINE)
                    stdscr.addch(row, left + width - 1, curses.ACS_VLINE)
                stdscr.addnstr(content_top, left + 2, label, max(0, width - 4), curses.A_BOLD)
            except curses.error:
                pass

        for y in range(min(inner_rows, screen.lines)):
            line = screen.buffer.get(y, {})
            for x in range(min(inner_columns, screen.columns)):
                cell = line.get(x, screen.default_char)
                if not cell.data:  # second cell of a wide glyph
                    continue
                cell_width = wcswidth(cell.data)
                if cell_width < 0 or x + cell_width > inner_columns:
                    continue
                try:
                    stdscr.addstr(content_top + 1 + y, left + 1 + x, cell.data, _attributes(cell, colors))
                except curses.error:
                    pass
                row, column = content_top + 1 + y, left + 1 + x
                if (os.environ.get("COLORTERM") == "truecolor" and row < rows and column < columns
                        and (_rgb(cell.fg) or _rgb(cell.bg))):
                    style = _rgb_cell_style(cell)
                    if rgb_last != (row, column - 1):
                        rgb_output.append(f"\x1b[{row + 1};{column + 1}H")
                    if style != rgb_style:
                        rgb_output.append(style)
                        rgb_style = style
                    rgb_output.append(cell.data)
                    rgb_last = row, column + cell_width - 1

        if index == focus and not screen.cursor.hidden and width > 2 and height > 2:
            cursor_x = min(inner_columns - 1, max(0, screen.cursor.x))
            cursor_y = min(inner_rows - 1, max(0, screen.cursor.y))
            try:
                stdscr.move(content_top + 1 + cursor_y, left + 1 + cursor_x)
            except curses.error:
                pass
        if screen.columns != inner_columns or screen.lines != inner_rows:
            screen.resize(lines=inner_rows, columns=inner_columns)
            session.resize(inner_rows, inner_columns)

    footer = notice or "PTY bytes are rendered per pane; focus does not change process lifetime."
    try:
        stdscr.addnstr(rows - 1, 0, footer, max(1, columns - 1))
        stdscr.refresh()
        if rgb_output:
            # DEC save/restore keeps curses' physical cursor position intact.
            sys.stdout.write("\x1b7" + "".join(rgb_output) + "\x1b[0m\x1b8")
            sys.stdout.flush()
    except curses.error:
        pass


def _run_ui(stdscr: curses.window, args: argparse.Namespace, sessions: list[PtySession]) -> None:
    global _resize_pending
    curses.noecho()
    curses.raw()
    stdscr.keypad(False)
    stdscr.nodelay(True)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    try:
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()
        router = InputRouter()
        focus = 0
        notice = ""
        titles = ("MANAGER OMP", "WORKER OMP", "HOST SHELL")
        rows, columns = stdscr.getmaxyx()
        content_height = max(1, rows - 4)
        base_width = max(1, columns // 3)
        widths = (base_width, base_width, max(1, columns - base_width * 2))
        outbound: list[deque[bytes]] = [deque() for _ in sessions]
        outbound_bytes = [0 for _ in sessions]

        def enqueue_outbound(index: int, data: bytes) -> bool:
            if not data:
                return True
            if outbound_bytes[index] + len(data) > MAX_APP_OUTBOUND_BYTES:
                return False
            outbound[index].append(bytes(data))
            outbound_bytes[index] += len(data)
            return True

        def enqueue_query_reply(index: int, data: bytes) -> None:
            if not enqueue_outbound(index, data):
                raise BufferError("query reply outbox budget invariant failed; refusing silent loss")

        panes: list[tuple[str, PtySession, TerminalScreen]] = []
        for index, session in enumerate(sessions):
            pane_columns = max(1, widths[index] - 2)
            screen = TerminalScreen(
                pane_columns,
                content_height,
                history=args.scrollback,
                reply=lambda data, index=index: enqueue_query_reply(index, data),
            )
            panes.append((titles[index], session, screen))
            session.resize(content_height, pane_columns)
        streams = [make_stream(screen) for _, _, screen in panes]
        colors = _ColorPairs()
        selectors = selectors_module.DefaultSelector()
        source_fd = sys.stdin.fileno()
        selectors.register(source_fd, selectors_module.EVENT_READ, "input")
        pty_registered: set[int] = set()
        dead_ptys: set[int] = set()
        for _, session, _ in panes:
            selectors.register(session.master_fd, selectors_module.EVENT_READ, "pty")
            pty_registered.add(session.master_fd)
        _draw(stdscr, panes, focus, colors, notice)

        def drain_outbound(index: int) -> int:
            session = panes[index][1]
            sent_total = 0
            while outbound[index]:
                data = outbound[index][0]
                try:
                    accepted = session.write(data)
                except (BlockingIOError, BufferError):
                    accepted = 0
                if accepted is None:
                    accepted = len(data)
                accepted = max(0, min(len(data), accepted))
                if accepted == 0:
                    break
                outbound_bytes[index] -= accepted
                sent_total += accepted
                if accepted == len(data):
                    outbound[index].popleft()
                else:
                    outbound[index][0] = data[accepted:]
                    break
            return sent_total

        def set_notice(message: str) -> None:
            nonlocal notice
            notice = message

        def reject_paste(index: int, reason: str) -> None:
            set_notice(f"Paste rejected: {reason}")

        def accept_paste(index: int, frame: BracketedPaste) -> None:
            # Older app input and VT replies are ordered before a paste. Move
            # them into the PTY queue first; if any remain, reject this frame.
            drain_outbound(index)
            if outbound[index]:
                reject_paste(index, "earlier pane input is still pending")
                return
            writer = getattr(panes[index][1], "write_frame", None)
            if writer is None:
                reject_paste(index, "the destination cannot reserve a whole frame")
                return
            try:
                accepted = writer(frame)
            except (BlockingIOError, BufferError):
                accepted = False
            if not accepted:
                reject_paste(index, "the complete frame does not fit in the PTY queue")

        def route_input(events: list[bytes | str | PasteRejected]) -> bool:
            nonlocal focus, notice
            prior_notice = notice
            changed = False
            for event in events:
                if isinstance(event, PasteRejected):
                    reject_paste(focus, event.reason)
                    continue
                if isinstance(event, BracketedPaste):
                    accept_paste(focus, event)
                    continue
                if event == "focus_next":
                    focus = (focus + 1) % len(panes)
                    changed = True
                elif event == "scroll_up":
                    panes[focus][2].prev_page()
                    changed = True
                elif event == "scroll_down":
                    panes[focus][2].next_page()
                    changed = True
                elif event == "quit":
                    if changed or notice != prior_notice:
                        _draw(stdscr, panes, focus, colors, notice)
                    return False
                elif isinstance(event, bytes):
                    index = focus
                    drain_outbound(index)
                    if enqueue_outbound(index, event):
                        drain_outbound(index)
                        if outbound_bytes[index]:
                            set_notice(
                                f"Input buffered: {outbound_bytes[index]} bytes waiting for {panes[index][0]} PTY space."
                            )
                        elif notice.startswith("Input buffered:"):
                            set_notice("")
                    else:
                        set_notice(
                            f"Input rejected: {panes[index][0]} outbound buffer is full; retry after it drains."
                        )
            if changed or notice != prior_notice:
                _draw(stdscr, panes, focus, colors, notice)
            return True

        def update_pty_registration() -> None:
            for index, (_, session, _) in enumerate(panes):
                fd = session.master_fd
                if fd in dead_ptys:
                    continue
                reply_space = MAX_APP_OUTBOUND_BYTES - outbound_bytes[index]
                can_read = reply_space >= MAX_QUERY_REPLY_BYTES_PER_OUTPUT_BYTE
                if can_read and fd not in pty_registered:
                    selectors.register(fd, selectors_module.EVENT_READ, "pty")
                    pty_registered.add(fd)
                elif not can_read and fd in pty_registered:
                    try:
                        selectors.unregister(fd)
                    except (KeyError, ValueError):
                        pass
                    pty_registered.discard(fd)

        while True:
            changed = False
            for index in range(len(panes)):
                drain_outbound(index)
            update_pty_registration()
            if _resize_pending:
                _resize_pending = False
                try:
                    new_rows, new_columns = _outer_size(sys.stdin.fileno())
                    curses.resizeterm(new_rows, new_columns)
                except (OSError, curses.error):
                    pass
                changed = True

            ready = selectors.select(0.025)
            ready.sort(key=lambda item: item[0].data == "input")
            for key, _ in ready:
                if key.data == "input":
                    try:
                        incoming = os.read(source_fd, MAX_SOURCE_READ_BYTES)
                    except BlockingIOError:
                        incoming = b""
                    if not route_input(router.feed(incoming)):
                        return
                else:
                    for index, (_, session, _) in enumerate(panes):
                        if session.master_fd == key.fd:
                            reply_space = MAX_APP_OUTBOUND_BYTES - outbound_bytes[index]
                            read_limit = min(
                                262144,
                                reply_space // MAX_QUERY_REPLY_BYTES_PER_OUTPUT_BYTE,
                            )
                            if read_limit <= 0:
                                break
                            if isinstance(session, PtySession):
                                data = session.read_available(read_limit)
                            else:
                                data = session.read_available()
                            if data:
                                streams[index].feed(data)
                                changed = True
                            elif session.poll() is not None:
                                try:
                                    selectors.unregister(session.master_fd)
                                except (KeyError, ValueError):
                                    pass
                                pty_registered.discard(session.master_fd)
                                dead_ptys.add(session.master_fd)
                            break

            if not route_input(router.flush()):
                return
            for index, (_, session, _) in enumerate(panes):
                flush_writes = getattr(session, "flush_writes", None)
                if flush_writes is not None:
                    flush_writes()
                drain_outbound(index)
                session.poll()
            update_pty_registration()
            if changed:
                _draw(stdscr, panes, focus, colors, notice)
    finally:
        try:
            sys.stdout.write("\x1b[?2004l\x1b[0m\x1b[?25h")
            sys.stdout.flush()
        except OSError:
            pass


selectors_module = selectors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-cmd", default=DEFAULT_OMP, help="manager OMP argv, parsed with shlex; default disables tools")
    parser.add_argument("--worker-cmd", default=DEFAULT_OMP, help="worker OMP argv, parsed with shlex; default disables tools")
    parser.add_argument("--host-cmd", default=DEFAULT_HOST, help="host shell argv, parsed with shlex")
    parser.add_argument("--scrollback", type=int, default=1000)
    args = parser.parse_args()
    commands = [shlex.split(args.manager_cmd), shlex.split(args.worker_cmd), shlex.split(args.host_cmd)]
    if any(not command for command in commands):
        parser.error("all three pane commands must be non-empty")
    child_term = os.environ.get("TERM", "xterm-256color")
    env = {"TERM": child_term, "COLORTERM": os.environ.get("COLORTERM", "truecolor"), "LANG": os.environ.get("LANG", "C.UTF-8")}
    sessions: list[PtySession] = []
    try:
        for command in commands:
            sessions.append(PtySession(command, env=env))
        signal.signal(signal.SIGWINCH, _on_resize)
        curses.wrapper(_run_ui, args, sessions)
    finally:
        for session in reversed(sessions):
            session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
