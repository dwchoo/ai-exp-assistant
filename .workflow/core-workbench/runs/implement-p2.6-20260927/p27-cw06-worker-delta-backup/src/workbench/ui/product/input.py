"""Host input parser: prefix commands, whole bracketed pastes, literal passthrough.

Everything except the prefix key is forwarded byte-for-byte, so original OMP
keys (Esc, Ctrl-C, Tab, arrows, slash commands, approval keys) are untouched.
The prefix pressed twice sends the literal prefix byte.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

PREFIX = 0x1D  # Ctrl-]  (not used by OMP or bash line editing)
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
MAX_PASTE_BYTES = 2 * 1024 * 1024
PARTIAL_HOLD_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class Passthrough:
    data: bytes


@dataclass(frozen=True, slots=True)
class Paste:
    data: bytes  # complete frame including the bracketed-paste markers


@dataclass(frozen=True, slots=True)
class PasteRejected:
    reason: str


@dataclass(frozen=True, slots=True)
class Command:
    key: str  # the character pressed after the prefix


Event = Passthrough | Paste | PasteRejected | Command


class InputParser:
    def __init__(self) -> None:
        self._pending = bytearray()
        self._pending_since = 0.0
        self._prefix = False
        self._in_paste = False
        self._paste = bytearray()
        self._discard = False

    @property
    def prefix_active(self) -> bool:
        return self._prefix

    def feed(self, data: bytes, *, now: float | None = None) -> list[Event]:
        now = time.monotonic() if now is None else now
        if data:
            if not self._pending:
                self._pending_since = now
            self._pending.extend(data)
        return self._drain(now, force=False)

    def flush(self, *, now: float | None = None) -> list[Event]:
        """Release a lone partial paste introducer (e.g. a real Esc key) after a short hold."""
        now = time.monotonic() if now is None else now
        return self._drain(now, force=bool(self._pending) and not self._in_paste
                           and now - self._pending_since >= PARTIAL_HOLD_SECONDS)

    def _drain(self, now: float, *, force: bool) -> list[Event]:
        events: list[Event] = []
        literal = bytearray()

        def out() -> None:
            if literal:
                events.append(Passthrough(bytes(literal)))
                literal.clear()

        buf = self._pending
        while buf:
            if self._in_paste:
                index = buf.find(PASTE_END)
                if index >= 0:
                    end = index + len(PASTE_END)
                    self._take_paste(bytes(buf[:end]))
                    del buf[:end]
                    events.append(PasteRejected("paste exceeds the 2 MiB limit") if self._discard
                                  else Paste(bytes(self._paste)))
                    self._paste.clear()
                    self._discard = self._in_paste = False
                    continue
                keep = 0
                for length in range(min(len(buf), len(PASTE_END) - 1), 0, -1):
                    if bytes(buf[-length:]) == PASTE_END[:length]:
                        keep = length
                        break
                self._take_paste(bytes(buf[:len(buf) - keep]))
                del buf[:len(buf) - keep]
                break
            byte = buf[0]
            if self._prefix:
                self._prefix = False
                del buf[0]
                if byte == PREFIX:
                    literal.append(byte)
                else:
                    out()
                    events.append(Command(chr(byte) if byte < 128 else "�"))
                continue
            if byte == PREFIX:
                out()
                self._prefix = True
                del buf[0]
                continue
            if byte == 0x1B:
                rest = bytes(buf)
                if rest.startswith(PASTE_START):
                    out()
                    del buf[:len(PASTE_START)]
                    self._in_paste = True
                    self._paste.extend(PASTE_START)
                    continue
                if PASTE_START.startswith(rest) and not force:
                    break  # maybe a split introducer; wait briefly
            literal.append(byte)
            del buf[0]
        out()
        return events

    def _take_paste(self, segment: bytes) -> None:
        if self._discard:
            return
        if len(self._paste) + len(segment) > MAX_PASTE_BYTES:
            self._paste.clear()
            self._discard = True
            return
        self._paste.extend(segment)
