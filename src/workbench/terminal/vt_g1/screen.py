"""Small pyte-backed VT screen used by the CW-02 feasibility prototype."""
from __future__ import annotations

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


class TerminalByteStream(pyte.ByteStream):
    """pyte ByteStream that also dispatches CSI S (SU) and CSI T (SD)."""

    csi = {**pyte.ByteStream.csi, "S": "scroll_up", "T": "scroll_down"}
    events = frozenset(pyte.ByteStream.events | {"scroll_up", "scroll_down"})


def make_stream(screen: TerminalScreen) -> pyte.ByteStream:
    return TerminalByteStream(screen)
