"""Metadata write-health latch kept separate from raw-log storage health."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class MetadataAdmissionStatus:
    """Immutable view of whether a new automatic run may be admitted."""

    automatic_runs_allowed: bool
    failure: str | None
    failed_at: str | None
    recovered_at: str | None
    generation: int


class MetadataAdmissionGate:
    """Latch metadata failures until an explicit durable-success observation.

    Callers report only outcomes from their metadata port. Raw-log success or
    failure is intentionally not connected to this object.
    """

    def __init__(self, *, initially_healthy: bool = True):
        if not isinstance(initially_healthy, bool):
            raise TypeError("initially_healthy must be bool")
        self._lock = RLock()
        self._allowed = initially_healthy
        self._failure: str | None = None if initially_healthy else "metadata health is unknown"
        self._failed_at: str | None = None if initially_healthy else _now()
        self._recovered_at: str | None = None
        self._generation = 0

    @property
    def automatic_runs_allowed(self) -> bool:
        with self._lock:
            return self._allowed

    def snapshot(self) -> MetadataAdmissionStatus:
        with self._lock:
            return MetadataAdmissionStatus(
                self._allowed, self._failure, self._failed_at,
                self._recovered_at, self._generation,
            )

    def record_metadata_failure(self, reason: str) -> MetadataAdmissionStatus:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("metadata failure reason must be a non-empty string")
        with self._lock:
            self._allowed = False
            self._failure = reason.strip()[:240]
            self._failed_at = _now()
            self._recovered_at = None
            self._generation += 1
            return self.snapshot()

    def record_durable_metadata_success(self) -> MetadataAdmissionStatus:
        """Clear the latch only after a caller confirms a durable metadata write."""
        with self._lock:
            if not self._allowed:
                self._allowed = True
                self._failure = None
                self._recovered_at = _now()
                self._generation += 1
            return self.snapshot()
