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


# C-D69 (1): OSC 52 clipboard writes (``ESC ] 52 ; Pc ; Pd`` ended by BEL or ST), plain or wrapped in a tmux
# passthrough DCS (``ESC P tmux; ESC ESC ] 52 ; ... ESC \\``), are taken out of the stream and handed to a callback.
_OSC52 = b"52;"
_TMUX_OSC52 = b"tmux;\x1b]52;"
_OSC_STOP = re.compile(rb"[\x07\x1b\x18\x1a]")
# default body cap (Pc ; Pd): base64 of 1 MiB plus room for the selection parameter
CLIPBOARD_MAX = 4 * -(-(1 << 20) // 3) + 16


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
    starts a DCS, as in a real terminal).

    C-D69 (1): the body of an OSC 52 (plain, or the only content of a
    ``tmux;`` DCS) never reaches pyte (a plain one reaches it as ``ESC ] 52 ;``
    + BEL, which pyte ignores); when it ends with BEL or ST its ``Pc;Pd`` body
    goes to ``clipboard`` (``None`` when longer than ``clipboard_max``).
    CAN/SUB or ESC + another byte abort it (nothing reported). Output fed while
    ``quiet`` is set (replay, catch-up tail) never reports, also when such an
    OSC 52 only began or ended there. Other OSCs reach pyte as before.
    """

    csi = {**pyte.ByteStream.csi, "S": "scroll_up", "T": "scroll_down"}
    events = frozenset(pyte.ByteStream.events | {"scroll_up", "scroll_down"})

    def __init__(self, *args: object, clipboard: Callable[[bytes | None], None] | None = None,
                 clipboard_max: int = CLIPBOARD_MAX, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._in_string = False  # inside a DCS/SOS/PM/APC string
        self._string_bytes = 0  # payload bytes of the current string so far
        self._pending_esc = False  # the last read ended in ESC (outside or inside a string)
        self.quiet = False  # set by the owner around replayed/skipped-ahead output: no clipboard reports
        self._clipboard = clipboard
        self._clipboard_max = clipboard_max
        self._osc_head: bytes | None = None  # bytes after ESC ] (forwarded) while they still match ``52;``
        self._osc52: bytearray | None = None  # body of a plain OSC 52 being collected
        self._inner: bytearray | None = None  # un-doubled payload of a DCS that may be a tmux-wrapped OSC 52
        self._inner_head = b""  # its first bytes, compared with ``tmux;`` ESC ] 52 ;
        self._clip_bytes = 0  # body bytes of the current OSC 52 (also those not kept)
        self._clip_over = False  # the current OSC 52 body is longer than clipboard_max
        self._clip_quiet = False  # part of the current OSC 52 was fed while quiet
        self._seq_quiet = False  # the current ESC ] / ESC P sequence began (or continued) in quiet output
        self._pending_quiet = False  # the ESC carried over from the last read was fed while quiet

    @property
    def in_clipboard_body(self) -> bool:
        """True while bytes fed now are held back as string or OSC 52 payload (never drawn).

        p27-cd69-review-01 P2-2: the owner skips the rest of an OSC 52 body this stream did not see begin
        (it started over inside it, e.g. after a catch-up), so that body is never drawn as text.
        """
        return self._osc52 is not None or self._in_string

    def feed(self, data: bytes) -> None:  # type: ignore[override]
        if self.quiet:
            self._clip_quiet = self._seq_quiet = True
        super().feed(self._drop_strings(bytes(data)))

    def _clip_start(self) -> bytearray:
        self._clip_bytes, self._clip_over, self._clip_quiet = 0, False, self._seq_quiet
        return bytearray()

    def _clip_add(self, buf: bytearray, data: bytes, slack: int = 0) -> None:
        self._clip_bytes += len(data)
        if self._clip_over:
            return
        if len(buf) + len(data) > self._clipboard_max + slack:
            self._clip_over = True
            buf.clear()
        else:
            buf += data

    def _clip_report(self, body: bytes) -> None:
        if self._clipboard is not None and not self._clip_quiet:
            self._clipboard(None if self._clip_over else bytes(body))

    def _inner_add(self, data: bytes) -> None:
        """Payload of a DCS: kept only while it can still be ``tmux;`` + OSC 52."""
        head = self._inner_head
        if len(head) < len(_TMUX_OSC52):
            take = data[:len(_TMUX_OSC52) - len(head)]
            head += take
            if not _TMUX_OSC52.startswith(head):
                self._inner = None
                return
            self._inner_head = head
            data = data[len(take):]
        if data:
            self._clip_add(self._inner, data, slack=2)  # the wrapped OSC 52's own BEL or ST

    def _inner_done(self) -> None:
        """The DCS ended with ST: report its OSC 52 when it was ``tmux;`` + a terminated OSC 52."""
        inner, self._inner = self._inner, None
        if inner is None or self._inner_head != _TMUX_OSC52:
            return
        if self._clip_over:
            self._clip_report(b"")
            return
        stop = _OSC_STOP.search(inner)
        if stop is None:
            return  # the wrapped OSC 52 has no terminator: no copy
        at = stop.start()
        if inner[at] == 0x07 or inner[at:at + 2] == b"\x1b\\":
            self._clip_over = at > self._clipboard_max
            self._clip_report(inner[:at])

    def _drop_strings(self, data: bytes) -> bytes:
        out = bytearray()
        carried = self._pending_esc  # data[0] is an ESC from the last read
        if carried:
            self._pending_esc = False
            data = b"\x1b" + data
        carried_quiet = carried and self._pending_quiet

        def quiet_at(at: int) -> bool:
            """The ESC at ``at`` was fed while quiet (a carried-over ESC: in the last read)."""
            return self.quiet or (carried_quiet and at == 0)

        i, n = 0, len(data)
        while i < n:
            if self._in_string:
                match = _STRING_STOP.search(data, i)
                stop = n if match is None else match.start()
                self._string_bytes += stop - i
                if self._string_bytes > STRING_MAX:  # abandoned: the rest is shown again
                    self._in_string, self._inner = False, None
                    i = max(i, stop - (self._string_bytes - STRING_MAX))
                    continue
                if self._inner is not None:
                    self._inner_add(data[i:stop])
                if match is None:
                    break
                i = stop
                if data[i] in _STRING_ABORT:
                    self._in_string, self._inner = False, None
                    i += 1
                elif i + 1 == n:
                    self._pending_esc, self._pending_quiet = True, quiet_at(i)
                    break
                elif data[i + 1] == 0x5C:  # ESC \ (ST): the string ends
                    self._in_string = False
                    self._inner_done()
                    i += 2
                elif data[i + 1] == _ESC:  # tmux doubling: one payload ESC
                    self._string_bytes += 2
                    if self._inner is not None:
                        self._inner_add(b"\x1b")
                    i += 2
                elif data[i + 1] in _STRING_ABORT:
                    self._in_string, self._inner = False, None
                    i += 2
                else:  # ESC + another byte: the string ended without ST; parse it again
                    self._in_string, self._inner = False, None
                continue
            if self._osc_head is not None:  # ESC ] forwarded: is it ESC ] 52 ; ?
                head = self._osc_head
                if data[i] == _OSC52[len(head)]:
                    head += data[i:i + 1]
                    out += data[i:i + 1]  # pyte sees ESC ] 52 ; (and later a BEL): an OSC it ignores
                    i += 1
                    if len(head) == len(_OSC52):
                        self._osc_head, self._osc52 = None, self._clip_start()
                    else:
                        self._osc_head = head
                else:  # another OSC: pyte parses it as before
                    self._osc_head = None
                continue
            if self._osc52 is not None:
                match = _OSC_STOP.search(data, i)
                stop = n if match is None else match.start()
                self._clip_add(self._osc52, data[i:stop])
                if self._clip_bytes > STRING_MAX:  # a lost terminator: abandoned, the rest is shown again
                    self._osc52 = None
                    out += b"\x07"  # ends pyte's OSC
                    i = max(i, stop - (self._clip_bytes - STRING_MAX))
                    continue
                if match is None:
                    break
                i = stop
                if not (data[i] == 0x1B and i + 1 == n):
                    out += b"\x07"  # whatever ends the OSC 52 here (BEL, ST, CAN/SUB, ESC + byte) ends pyte's OSC
                if data[i] == 0x07:  # BEL
                    body, self._osc52 = self._osc52, None
                    self._clip_report(body)
                    i += 1
                elif data[i] in _STRING_ABORT:
                    self._osc52 = None
                    i += 1
                elif i + 1 == n:
                    self._pending_esc, self._pending_quiet = True, quiet_at(i)
                    break
                elif data[i + 1] == 0x5C:  # ESC \ (ST)
                    body, self._osc52 = self._osc52, None
                    self._clip_report(body)
                    i += 2
                else:  # ESC + another byte: aborted (no copy); the ESC is parsed again
                    self._osc52 = None
                continue
            esc = data.find(b"\x1b", i)
            if esc < 0:
                out += data[i:]
                break
            out += data[i:esc]
            if esc + 1 == n:
                self._pending_esc, self._pending_quiet = True, quiet_at(esc)
                break
            follower = data[esc + 1]
            self._seq_quiet = quiet_at(esc)
            if follower == _ESC:  # the first ESC is cancelled; the second starts a sequence
                i = esc + 1
            elif follower in _STRING_INTRODUCERS:
                self._in_string, self._string_bytes = True, 0
                if follower == 0x50 and self._clipboard is not None:  # DCS: maybe a tmux-wrapped OSC 52
                    self._inner, self._inner_head = self._clip_start(), b""
                i = esc + 2
            elif follower == 0x5D:  # OSC: forwarded as before; only an OSC 52 body is held back
                out += b"\x1b]"
                self._osc_head = b""
                i = esc + 2
            else:
                out += data[esc:esc + 2]
                i = esc + 2
        return bytes(out)


def make_stream(screen: TerminalScreen, *, clipboard: Callable[[bytes | None], None] | None = None,
                clipboard_max: int = CLIPBOARD_MAX) -> pyte.ByteStream:
    return TerminalByteStream(screen, clipboard=clipboard, clipboard_max=clipboard_max)
