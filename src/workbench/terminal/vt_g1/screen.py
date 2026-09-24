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

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        control = self._terminal_control
        if lines:
            control["lines"] = lines
        if columns:
            control["columns"] = columns
        super().resize(lines=lines, columns=columns)


def make_stream(screen: TerminalScreen) -> pyte.ByteStream:
    return pyte.ByteStream(screen)
