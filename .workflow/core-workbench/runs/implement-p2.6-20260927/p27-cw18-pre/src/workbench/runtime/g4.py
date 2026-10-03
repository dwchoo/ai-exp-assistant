"""Bounded G4 lifetime ports; not a production scheduler or recovery store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time


class MetadataPort:
    """Fixture append log: dispatch requires a successful fsync, not a flag."""
    def __init__(self, path: Path):
        self.path = path
        self.fail = False

    def persist(self, record: dict) -> None:
        if self.fail:
            raise OSError("injected metadata failure")
        created = not self.path.exists()
        with self.path.open("ab") as stream:
            stream.write((json.dumps(record, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        if created:
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


class WakePort:
    """One approved timer fixture, consumed even when held; never replayed."""
    def __init__(self, metadata: MetadataPort, deliver):
        self.metadata, self.deliver = metadata, deliver
        self.paused = False
        self.due: float | None = None
        self.consumed = False
        self.ticks = 0
        self.deliveries = 0
        self.status = "unarmed"
        self.approval: dict | None = None

    def arm(self, approval: dict, *, now: float | None = None) -> None:
        if self.due is not None or self.consumed:
            raise ValueError("one timer only; no replay")
        if approval.get("approved") is not True or approval.get("scope") != "one-no-tools-worker-wake":
            raise ValueError("bounded approval required")
        self.approval = dict(approval)
        self.due = (time.monotonic() if now is None else now) + 60
        self.status = "armed"

    def tick(self, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.due is None or now < self.due or self.consumed:
            return False
        self.consumed = True
        self.ticks += 1
        if self.paused:
            self.status = "held_paused"
            return False
        try:
            self.metadata.persist({"kind": "wake", "approval": self.approval})
        except OSError:
            self.status = "held_metadata"
            return False
        # A lost/failed acknowledgement is unknown, never permission to retry.
        self.status = "unknown"
        self.deliveries += 1
        self.deliver(self.approval)
        self.status = "api_returned"
        return True


class FrontendLease:
    """A display-client lease has no process/control ownership authority."""
    def __init__(self):
        self.attached = False
        self.owner = "user"
        self.owner_epoch = 1

    def attach(self) -> None:
        if self.attached:
            raise ValueError("duplicate frontend")
        self.attached = True

    def detach(self) -> None:
        self.attached = False
