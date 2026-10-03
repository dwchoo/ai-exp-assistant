"""Raw-byte host input router; only its documented UI keys are consumed."""
from __future__ import annotations

import time
from dataclasses import dataclass


MAX_BRACKETED_PASTE_BYTES = 2 * 1024 * 1024


class BracketedPaste(bytes):
    """A complete paste frame that the UI must accept or reject atomically."""


@dataclass(frozen=True)
class PasteRejected:
    reason: str


class InputRouter:
    _paste_start = b"\x1b[200~"
    _paste_end = b"\x1b[201~"
    _keys = {
        b"\x1b[17~": "focus_next",  # xterm F6
        b"\x1b[18~": "scroll_up",  # xterm F7
        b"\x1b[19~": "scroll_down",  # xterm F8
        b"\x1b[21~": "quit",  # xterm F10
    }

    def __init__(self) -> None:
        self._pending = bytearray()
        self._pending_since = 0.0
        self._in_bracketed_paste = False
        self._paste_buffer = bytearray()
        self._discarding_paste = False

    def feed(self, data: bytes, *, now: float | None = None) -> list[bytes | str | PasteRejected]:
        if data:
            if not self._pending:
                self._pending_since = time.monotonic() if now is None else now
            self._pending.extend(data)
        events: list[bytes | str | PasteRejected] = []
        passthrough = bytearray()
        current_time = time.monotonic() if now is None else now

        def flush_passthrough() -> None:
            if passthrough:
                events.append(bytes(passthrough))
                passthrough.clear()

        def add_paste_bytes(segment: bytes) -> None:
            if self._discarding_paste:
                return
            if len(self._paste_buffer) + len(segment) > MAX_BRACKETED_PASTE_BYTES:
                self._paste_buffer.clear()
                self._discarding_paste = True
                return
            self._paste_buffer.extend(segment)

        while self._pending:
            if self._in_bracketed_paste:
                end_index = self._pending.find(self._paste_end)
                if end_index >= 0:
                    end = end_index + len(self._paste_end)
                    add_paste_bytes(bytes(self._pending[:end]))
                    del self._pending[:end]
                    if self._discarding_paste:
                        events.append(PasteRejected("frame exceeds the 2 MiB paste limit"))
                    else:
                        events.append(BracketedPaste(self._paste_buffer))
                    self._paste_buffer.clear()
                    self._discarding_paste = False
                    self._in_bracketed_paste = False
                    self._pending_since = current_time
                    continue

                # Keep only a possible split end marker. A timeout must not
                # expose its bytes to the UI hotkey parser.
                keep_length = 0
                for length in range(min(len(self._pending), len(self._paste_end) - 1), 0, -1):
                    if self._pending[-length:] == self._paste_end[:length]:
                        keep_length = length
                        break
                safe_length = len(self._pending) - keep_length
                if safe_length:
                    add_paste_bytes(bytes(self._pending[:safe_length]))
                    del self._pending[:safe_length]
                    self._pending_since = current_time
                break

            if self._pending.startswith(self._paste_start):
                flush_passthrough()
                self._paste_buffer.clear()
                self._paste_buffer.extend(self._paste_start)
                self._discarding_paste = False
                del self._pending[: len(self._paste_start)]
                self._in_bracketed_paste = True
                self._pending_since = current_time
                continue

            for sequence, action in self._keys.items():
                if self._pending.startswith(sequence):
                    flush_passthrough()
                    del self._pending[: len(sequence)]
                    events.append(action)
                    self._pending_since = current_time
                    break
            else:
                pending_bytes = bytes(self._pending)
                if self._paste_start.startswith(pending_bytes):
                    # PTY reads can split the paste introducer beyond the
                    # function-key timeout; wait for its terminating byte.
                    break
                if any(key.startswith(pending_bytes) for key in self._keys):
                    if current_time - self._pending_since < 0.06:
                        break
                passthrough.extend(self._pending[:1])
                del self._pending[:1]
                self._pending_since = current_time
        flush_passthrough()
        return events

    def flush(self, *, now: float | None = None) -> list[bytes | str | PasteRejected]:
        return self.feed(b"", now=now)
