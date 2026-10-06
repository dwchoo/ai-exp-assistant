"""Small pyte-backed VT screen used by the CW-02 feasibility prototype."""
from __future__ import annotations

import re
from collections.abc import Callable

try:
    import pyte
except ImportError as exc:  # pragma: no cover - exercised by the entry point
    raise RuntimeError("G1 prototype needs pyte==0.8.2; see gates/G1.md") from exc


class TerminalScreen(pyte.HistoryScreen):
    """VT screen with a bounded scrollback and DEC alternate-screen support."""

    _ALT_MODES = {47, 1047, 1049}

    def __init__(
        self,
        columns: int,
        lines: int,
        *,
        history: int = 1000,
        reply: Callable[[bytes], None] | None = None,
    ) -> None:
        self._terminal_control = {
            "reply": reply,
            "history_size": history,
            "columns": columns,
            "lines": lines,
            "primary": None,
            "alternate": None,
            "using_alternate": False,
        }
        super().__init__(columns, lines, history=history)

    def write_process_input(self, data: str) -> None:
        callback = self._terminal_control["reply"]
        if callback is not None:
            callback(data.encode("utf-8"))

    def report_device_status(self, mode: int, **kwargs: object) -> None:
        if not kwargs.get("private"):
            super().report_device_status(mode)
        elif mode == 6:
            # DEC CPR is private; pyte's public DSR handler rejects that keyword.
            row = self.cursor.y + 1
            if pyte.modes.DECOM in self.mode and self.margins is not None:
                row -= self.margins.top
            self.write_process_input(f"\x1b[?{row};{self.cursor.x + 1}R")
        # Unknown private DSRs make no unsupported capability claim.

    def _capture(self) -> dict[str, object]:
        return {key: value for key, value in self.__dict__.items() if key != "_terminal_control"}

    def _restore(self, state: dict[str, object]) -> None:
        control = self._terminal_control
        self.__dict__.clear()
        self.__dict__["_terminal_control"] = control
        self.__dict__.update(state)

    def _blank_state(self) -> dict[str, object]:
        screen = pyte.HistoryScreen(
            self._terminal_control["columns"],
            self._terminal_control["lines"],
            history=self._terminal_control["history_size"],
        )
        return screen.__dict__.copy()

    def _enter_alternate(self, *, clear: bool) -> None:
        control = self._terminal_control
        if control["using_alternate"]:
            if clear:
                control["alternate"] = self._blank_state()
                self._restore(control["alternate"])
            return

        control["primary"] = self._capture()
        if clear or control["alternate"] is None:
            control["alternate"] = self._blank_state()
        self._restore(control["alternate"])
        control["using_alternate"] = True

    def _leave_alternate(self) -> None:
        control = self._terminal_control
        if not control["using_alternate"]:
            return
        control["alternate"] = self._capture()
        primary = control["primary"]
        if primary is not None:
            self._restore(primary)
        control["primary"] = None
        control["using_alternate"] = False
        super().resize(
            lines=control["lines"],
            columns=control["columns"],
        )

    def set_mode(self, *modes: int, **kwargs: object) -> None:
        if kwargs.get("private"):
            handled: set[int] = set()
            for mode in modes:
                if mode in self._ALT_MODES:
                    self._enter_alternate(clear=mode in {1047, 1049})
                    handled.add(mode)
            remaining = tuple(mode for mode in modes if mode not in handled)
            if remaining:
                super().set_mode(*remaining, **kwargs)
            return
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs: object) -> None:
        if kwargs.get("private"):
            handled: set[int] = set()
            for mode in modes:
                if mode in self._ALT_MODES:
                    self._leave_alternate()
                    handled.add(mode)
            remaining = tuple(mode for mode in modes if mode not in handled)
            if remaining:
                super().reset_mode(*remaining, **kwargs)
            return
        super().reset_mode(*modes, **kwargs)

    def _blank_scrolled_line(self, y: int) -> None:
        """Replace row ``y`` with a blank line carrying the erase attributes."""
        self.buffer.pop(y, None)
        attrs = self.cursor.attrs
        if attrs != self.default_char:
            self.buffer[y].default = attrs._replace(data=" ")

    def _scroll_region(self, count: int | None, *, up: bool) -> None:
        """xterm SU/SD: scroll the DECSTBM region; the cursor does not move."""
        top, bottom = self.margins or pyte.screens.Margins(0, self.lines - 1)
        height = bottom - top + 1
        if height <= 0:
            return
        count = min(max(count or 1, 1), height)
        self.dirty.update(range(top, bottom + 1))
        history = self.history
        if up:
            for y in range(top, top + count):
                history.top.append(self.buffer[y])
            for y in range(top, bottom + 1 - count):
                self.buffer[y] = self.buffer[y + count]
            for y in range(bottom + 1 - count, bottom + 1):
                self._blank_scrolled_line(y)
        else:
            for y in range(bottom, bottom - count, -1):
                history.bottom.append(self.buffer[y])
            for y in range(bottom, top + count - 1, -1):
                self.buffer[y] = self.buffer[y - count]
            for y in range(top, top + count):
                self._blank_scrolled_line(y)

    def scroll_up(self, *params: int, **kwargs: object) -> None:
        """CSI Ps S (SU). Private and multi-parameter forms are not SU."""
        if kwargs.get("private") or len(params) > 1:
            return
        self._scroll_region(params[0] if params else None, up=True)

    def scroll_down(self, *params: int, **kwargs: object) -> None:
        """CSI Ps T (SD). Multi-parameter forms are xterm mouse tracking, not SD."""
        if kwargs.get("private") or len(params) > 1:
            return
        self._scroll_region(params[0] if params else None, up=False)

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        control = self._terminal_control
        if lines:
            control["lines"] = lines
        if columns:
            control["columns"] = columns
        super().resize(lines=lines, columns=columns)


_ESC = 0x1B
# ESC P (DCS), ESC X (SOS), ESC ^ (PM), ESC _ (APC): strings that end at ST (ESC \\); CAN/SUB abort them.
_STRING_INTRODUCERS = frozenset(b"PX^_")
_STRING_ABORT = frozenset(b"\x18\x1a")
_STRING_STOP = re.compile(rb"[\x1b\x18\x1a]")
# p27-cd68-o1-fix-02: a backstop for a lost ST in plain-text output (any later ESC sequence already ends the
# string): a string longer than this is abandoned and the pane shows output again. It is above realistic single
# strings (a sixel DCS or a long notification can be several MiB), whose payload must not leak as text.
STRING_MAX = 8 << 20


class TerminalByteStream(pyte.ByteStream):
    """pyte ByteStream that also dispatches CSI S (SU) and CSI T (SD).

    p27-cd68-o1-fix-01/-02: pyte 0.8.2 has no DCS/SOS/PM/APC state, so their
    payload was drawn as text (inside tmux, OMP wraps its OSC 777 notification
    in ``ESC P tmux; ... ESC \\`` and the pane showed ``tmux;]777;notify;...``).
    ``feed`` drops such strings before pyte sees them, also across reads.
    Inside a string: ``ESC \\`` ends it; ``ESC ESC`` is tmux passthrough
    escaping (payload); CAN/SUB (also right after an ESC) abort it; ESC with any
    other byte ends it and is parsed again as a normal sequence (a lost ST
    never hides later output, RIS still resets); after ``STRING_MAX`` bytes it
    is abandoned. Outside: ``ESC ESC`` cancels the first ESC (``ESC ESC P``
    starts a DCS, as in a real terminal). Nothing is forwarded anywhere.
    """

    csi = {**pyte.ByteStream.csi, "S": "scroll_up", "T": "scroll_down"}
    events = frozenset(pyte.ByteStream.events | {"scroll_up", "scroll_down"})

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._in_string = False  # inside a DCS/SOS/PM/APC string
        self._string_bytes = 0  # payload bytes of the current string so far
        self._pending_esc = False  # the last read ended in ESC (outside or inside a string)

    def feed(self, data: bytes) -> None:  # type: ignore[override]
        super().feed(self._drop_strings(bytes(data)))

    def _drop_strings(self, data: bytes) -> bytes:
        out = bytearray()
        if self._pending_esc:
            self._pending_esc = False
            data = b"\x1b" + data
        i, n = 0, len(data)
        while i < n:
            if self._in_string:
                match = _STRING_STOP.search(data, i)
                stop = n if match is None else match.start()
                self._string_bytes += stop - i
                if self._string_bytes > STRING_MAX:  # abandoned: the rest is shown again
                    self._in_string = False
                    i = max(i, stop - (self._string_bytes - STRING_MAX))
                    continue
                if match is None:
                    break
                i = stop
                if data[i] in _STRING_ABORT:
                    self._in_string = False
                    i += 1
                elif i + 1 == n:
                    self._pending_esc = True
                    break
                elif data[i + 1] == 0x5C:  # ESC \ (ST): the string ends
                    self._in_string = False
                    i += 2
                elif data[i + 1] == _ESC:  # tmux doubling: one payload ESC
                    self._string_bytes += 2
                    i += 2
                elif data[i + 1] in _STRING_ABORT:
                    self._in_string = False
                    i += 2
                else:  # ESC + another byte: the string ended without ST; parse it again
                    self._in_string = False
                continue
            esc = data.find(b"\x1b", i)
            if esc < 0:
                out += data[i:]
                break
            out += data[i:esc]
            if esc + 1 == n:
                self._pending_esc = True
                break
            follower = data[esc + 1]
            if follower == _ESC:  # the first ESC is cancelled; the second starts a sequence
                i = esc + 1
            elif follower in _STRING_INTRODUCERS:
                self._in_string, self._string_bytes = True, 0
                i = esc + 2
            else:
                out += data[esc:esc + 2]
                i = esc + 2
        return bytes(out)


def make_stream(screen: TerminalScreen) -> pyte.ByteStream:
    return TerminalByteStream(screen)
