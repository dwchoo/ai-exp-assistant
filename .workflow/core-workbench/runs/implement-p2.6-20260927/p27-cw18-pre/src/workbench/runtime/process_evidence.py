"""Fresh Linux process identity evidence for lifecycle reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class ProcessRef:
    role: str
    pid: int
    start_ticks: int
    owner_epoch: int

    def __post_init__(self) -> None:
        if (not isinstance(self.role, str) or not self.role
                or any(type(value) is not int or value < 1 for value in
                       (self.pid, self.start_ticks, self.owner_epoch))):
            raise ValueError("exact managed process reference required")


@dataclass(frozen=True, slots=True)
class ProcessEvidence:
    ref: ProcessRef
    state: str

    def __post_init__(self) -> None:
        if self.state not in {"alive", "dead", "unknown"}:
            raise ValueError("process evidence state is invalid")


class LinuxProcessProbe:
    """Compare PID plus proc start ticks; never infer ownership from PID alone."""

    @staticmethod
    def start_ticks(pid: int) -> int | None:
        if type(pid) is not int or pid < 1:
            raise ValueError("positive PID required")
        descriptor = os.open(
            f"/proc/{pid}/stat", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:
            raw = os.read(descriptor, 4096)
        finally:
            os.close(descriptor)
        marker = raw.rfind(b") ")
        if marker < 0 or raw[:marker].split(b" ", 1)[0] != str(pid).encode():
            return None
        fields = raw[marker + 2:].split()
        if len(fields) <= 19:
            return None
        try:
            return int(fields[19])
        except ValueError:
            return None

    def observe(self, ref: ProcessRef) -> ProcessEvidence:
        try:
            descriptor = os.open(
                f"/proc/{ref.pid}/stat", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            try:
                raw = os.read(descriptor, 4096)
            finally:
                os.close(descriptor)
        except FileNotFoundError:
            return ProcessEvidence(ref, "dead")
        except OSError:
            return ProcessEvidence(ref, "unknown")
        marker = raw.rfind(b") ")
        if marker < 0 or raw[:marker].split(b" ", 1)[0] != str(ref.pid).encode():
            return ProcessEvidence(ref, "unknown")
        fields = raw[marker + 2:].split()
        if len(fields) <= 19:
            return ProcessEvidence(ref, "unknown")
        try:
            start_ticks = int(fields[19])
        except ValueError:
            return ProcessEvidence(ref, "unknown")
        if start_ticks != ref.start_ticks:
            return ProcessEvidence(ref, "unknown")
        return ProcessEvidence(ref, "dead" if fields[0] in {b"Z", b"X", b"x"} else "alive")
