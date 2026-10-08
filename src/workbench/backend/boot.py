"""CW-19: the data dir's boot marker (C-AC-23, C-D58, C-D71 (3)).

``/proc/sys/kernel/random/boot_id`` is the boot marker. ``<data>/boot.json``
(0600, atomic and fsynced) keeps the boot the data dir was last confirmed
under and whether a confirmation is pending. ``BootStore.begin`` runs at
backend start, before anything is served or started:

- no earlier record and no earlier backend: ``fresh`` (nothing to confirm);
- the recorded boot differs from the current one: confirmation required
  (``reboot``);
- the current marker cannot be read, or an earlier start left no marker:
  confirmation required (``boot_marker_unknown``; fail closed, never "same
  boot");
- ``boot.json`` exists but is unreadable or unsafe: confirmation required
  (``boot_record_unreadable``);
- a confirmation still pending from an earlier start stays pending.

The pending state is written before the backend serves anything, so a crash
before ``confirm_boot`` keeps it; ``confirm`` writes the confirmed boot first
and only then lifts the hold.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import time
from typing import Any, Callable, Mapping

from workbench.backend.paths import read_private_json, write_private_json

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
BOOT_RECORD_VERSION = 1
HISTORY_LIMIT = 20
_MAX_RECORD = 64 * 1024

# Start-up classifications (reported in the snapshot ``startup`` and the manager notice).
FRESH = "fresh"
SAME_BOOT_CLEAN_STOP = "same_boot_clean_stop"
SAME_BOOT_UNVERIFIED_STOP = "same_boot_unverified_stop"
SAME_BOOT_CRASH = "same_boot_crash"
REBOOT = "reboot"
BOOT_UNKNOWN = "boot_unknown"
CLASSIFICATIONS = (FRESH, SAME_BOOT_CLEAN_STOP, SAME_BOOT_UNVERIFIED_STOP, SAME_BOOT_CRASH, REBOOT, BOOT_UNKNOWN)


def read_boot_id(path: Path = BOOT_ID_PATH) -> str | None:
    """The current boot marker, or None when it cannot be read (callers fail closed)."""
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value if value and len(value) <= 64 else None


class BootRecordError(RuntimeError):
    """``boot.json`` exists but cannot be trusted."""


@dataclass(frozen=True, slots=True)
class BootState:
    current: str | None
    recorded: str | None
    confirmed: str | None
    pending: bool
    reason: str | None  # reboot | boot_marker_unknown | boot_record_unreadable (when pending)
    persisted: bool = True  # False: the state could not be written (it still holds in memory)
    confirmed_here: bool = False  # the user confirmed this boot while this backend runs

    def view(self) -> dict[str, Any]:
        """The ui_v1 ``boot`` fields; ``confirmed`` is null when no confirmation was needed."""
        return {"boot_id": self.current, "recorded_boot_id": self.recorded, "confirmed_boot_id": self.confirmed,
                "confirmation_required": self.pending,
                "confirmed": False if self.pending else (True if self.confirmed_here else None),
                "reason": self.reason, "persisted": self.persisted}


def classify(current: str | None, previous_record: Mapping[str, Any] | None, *, history: bool) -> str:
    """How the previous backend incarnation ended, as far as the boot and its record tell.

    ``previous_record`` is the previous ``backend.json`` (read before it is
    overwritten); ``history`` says whether the data dir was used before.
    """
    if current is None:
        return BOOT_UNKNOWN
    if previous_record is None:
        return FRESH if not history else BOOT_UNKNOWN
    previous_boot = previous_record.get("boot_id")
    if not isinstance(previous_boot, str) or not previous_boot:
        return BOOT_UNKNOWN
    if previous_boot != current:
        return REBOOT
    if previous_record.get("phase") == "stopped":
        shutdown = previous_record.get("shutdown")
        verified = isinstance(shutdown, Mapping) and shutdown.get("verified") is True
        return SAME_BOOT_CLEAN_STOP if verified else SAME_BOOT_UNVERIFIED_STOP
    return SAME_BOOT_CRASH


class BootStore:
    """``<data>/boot.json``; see the module docstring."""

    def __init__(self, path: str | Path, *, boot_source: Callable[[], str | None] = read_boot_id,
                 wall: Callable[[], float] = time.time):
        self.path = Path(path)
        self._boot_source = boot_source
        self._wall = wall
        self.state: BootState | None = None
        self._history: list[dict[str, Any]] = []

    def current_boot(self) -> str | None:
        try:
            value = self._boot_source()
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

    def read(self) -> dict[str, Any] | None:
        """The stored record; None when absent; ``BootRecordError`` when present but not trustworthy."""
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BootRecordError(f"boot record unreadable: {type(exc).__name__}") from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > _MAX_RECORD):
            raise BootRecordError("boot record is not a private regular file")
        value = read_private_json(self.path, _MAX_RECORD)
        if (not isinstance(value, dict) or value.get("version") != BOOT_RECORD_VERSION
                or type(value.get("pending")) is not bool
                or any(value.get(key) is not None and not isinstance(value.get(key), str)
                       for key in ("recorded_boot_id", "confirmed_boot_id", "pending_boot_id", "reason"))):
            raise BootRecordError("boot record is corrupt")
        return value

    def _write(self, state: BootState, event: str) -> bool:
        self._history = (self._history + [{"at": self._wall(), "event": event, "boot_id": state.current,
                                           "pending": state.pending, "reason": state.reason}])[-HISTORY_LIMIT:]
        record = {"version": BOOT_RECORD_VERSION, "recorded_boot_id": state.recorded,
                  "confirmed_boot_id": state.confirmed, "pending": state.pending,
                  "pending_boot_id": state.current if state.pending else None, "reason": state.reason,
                  "history": list(self._history)}
        try:
            write_private_json(self.path, record)
        except OSError:
            return False
        return True

    def begin(self, previous_backend: Mapping[str, Any] | None) -> BootState:
        """Decide (and persist) whether this start needs a boot confirmation; see the module docstring."""
        current = self.current_boot()
        unreadable = False
        try:
            stored = self.read()
        except BootRecordError:
            stored, unreadable = None, True
        if stored is not None and isinstance(stored.get("history"), list):
            self._history = [item for item in stored["history"] if isinstance(item, dict)][-HISTORY_LIMIT:]
        history = stored is not None or previous_backend is not None or unreadable
        recorded = stored.get("recorded_boot_id") if stored is not None else None
        if recorded is None and isinstance((previous_backend or {}).get("boot_id"), str):
            recorded = previous_backend["boot_id"]  # a data dir from before boot.json existed
        confirmed = stored.get("confirmed_boot_id") if stored is not None else None
        reason: str | None = None
        if current is None:
            reason = "boot_marker_unknown"
        elif unreadable:
            reason = "boot_record_unreadable"
        elif stored is not None and stored.get("pending") is True:
            reason = stored.get("reason") if stored.get("reason") in (
                "reboot", "boot_marker_unknown", "boot_record_unreadable", "reconcile_failed") else "reboot"
        elif not history:
            reason = None
        elif recorded is None:
            reason = "boot_marker_unknown"
        elif recorded != current:
            reason = "reboot"
        pending = reason is not None
        state = BootState(current, recorded if pending else current, confirmed, pending, reason)
        persisted = self._write(state, "start_pending" if pending else "start")
        self.state = BootState(state.current, state.recorded, state.confirmed, state.pending, state.reason,
                               persisted)
        return self.state

    def rewrite(self) -> bool:
        """Write the current state again (after a failed write); True when it is durable now."""
        if self.state is None:
            return False
        ok = self._write(self.state, "rewrite")
        if ok and not self.state.persisted:
            self.state = BootState(self.state.current, self.state.recorded, self.state.confirmed, self.state.pending,
                                   self.state.reason, True, self.state.confirmed_here)
        return ok

    def confirm(self, boot_id: str) -> BootState:
        """Persist the confirmation for ``boot_id`` (the caller checked it is pending and current).

        Raises OSError when it cannot be written: the hold then stays.
        """
        state = self.state
        if state is None or not state.pending:
            raise ValueError("no boot confirmation is pending")
        if boot_id != state.current or boot_id != self.current_boot():
            raise ValueError("boot id does not match the current boot")
        confirmed = BootState(boot_id, boot_id, boot_id, False, None, True, True)
        if not self._write(confirmed, "confirmed"):
            raise OSError("the boot confirmation could not be written")
        self.state = confirmed
        return confirmed


__all__ = ["BOOT_UNKNOWN", "BootRecordError", "BootState", "BootStore", "CLASSIFICATIONS", "FRESH", "REBOOT",
           "SAME_BOOT_CLEAN_STOP", "SAME_BOOT_CRASH", "SAME_BOOT_UNVERIFIED_STOP", "classify", "read_boot_id"]
