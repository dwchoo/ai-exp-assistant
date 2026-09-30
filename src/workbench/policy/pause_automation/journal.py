"""Append-only, inspectable pause evidence scoped to one approved Task run."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from uuid import uuid4


class PauseJournalError(RuntimeError):
    """The durable pause history cannot be safely read or extended."""


@dataclass(frozen=True, slots=True)
class PauseBinding:
    task_id: str
    revision: int
    run_id: str
    approval_hash: str
    approved_scope_hash: str
    approved_paths: tuple[str, ...]
    manager_session_id: str | None
    manager_generation: int | None
    worker_session_id: str | None
    worker_generation: int | None

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in
                   (self.task_id, self.run_id, self.approval_hash, self.approved_scope_hash)):
            raise ValueError("pause binding identifiers and hashes must be non-empty")
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None
               for value in (self.approval_hash, self.approved_scope_hash)):
            raise ValueError("pause binding hashes must be SHA256")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if any(not isinstance(path, str) or not path for path in self.approved_paths):
            raise ValueError("approved paths must be non-empty strings")
        for name in ("manager_generation", "worker_generation"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer or unknown")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["approved_paths"] = list(self.approved_paths)
        return value


@dataclass(frozen=True, slots=True)
class PauseEvent:
    sequence: int
    event_id: str
    pause_id: str
    kind: str
    occurred_at: str
    binding: PauseBinding
    request_id: str | None = None
    status: str | None = None
    unknown_tool_call_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["binding"] = self.binding.to_dict()
        value["unknown_tool_call_ids"] = list(self.unknown_tool_call_ids)
        return value


class PauseJournal:
    """Small JSONL journal; every event is fsynced before it is reported durable."""

    _event_kinds = frozenset({
        "entry_baseline_recorded",
        "pause_requested", "manager_pause_ack", "worker_pause_ack",
        "turn_stop_observed", "resume_requested", "manager_resumed",
        "worker_resumed", "resumed", "resume_failed", "cancelled",
    })

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read_fd(self, fd: int) -> list[PauseEvent]:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        if raw and not raw.endswith(b"\n"):
            raise PauseJournalError("pause journal ends with a partial event")
        events: list[PauseEvent] = []
        for expected, line in enumerate(raw.splitlines(), start=1):
            try:
                data = json.loads(line)
                if set(data) != {
                    "sequence", "event_id", "pause_id", "kind", "occurred_at",
                    "binding", "request_id", "status", "unknown_tool_call_ids",
                }:
                    raise ValueError("unexpected event fields")
                binding_data = data["binding"]
                if set(binding_data) != {
                    "task_id", "revision", "run_id", "approval_hash", "approved_scope_hash",
                    "approved_paths", "manager_session_id", "manager_generation",
                    "worker_session_id", "worker_generation",
                }:
                    raise ValueError("unexpected binding fields")
                binding = PauseBinding(
                    **{**binding_data, "approved_paths": tuple(binding_data["approved_paths"])}
                )
                event = PauseEvent(
                    sequence=data["sequence"], event_id=data["event_id"], pause_id=data["pause_id"],
                    kind=data["kind"], occurred_at=data["occurred_at"], binding=binding,
                    request_id=data["request_id"], status=data["status"],
                    unknown_tool_call_ids=tuple(data["unknown_tool_call_ids"]),
                )
                if (event.sequence != expected or event.kind not in self._event_kinds
                        or not event.event_id or not event.pause_id or not event.occurred_at
                        or (event.request_id is not None and not isinstance(event.request_id, str))
                        or (event.status is not None and not isinstance(event.status, str))
                        or any(not isinstance(item, str) or not item
                               for item in event.unknown_tool_call_ids)):
                    raise ValueError("invalid event values or order")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PauseJournalError("pause journal contains invalid evidence") from exc
            events.append(event)
        return events

    def read(self) -> tuple[PauseEvent, ...]:
        try:
            fd = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise PauseJournalError("pause journal cannot be opened") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PauseJournalError("pause journal is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_SH)
            return tuple(self._read_fd(fd))
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def append(self, *, pause_id: str, kind: str, occurred_at: str, binding: PauseBinding,
               request_id: str | None = None, status: str | None = None,
               unknown_tool_call_ids: tuple[str, ...] = ()) -> PauseEvent:
        if kind not in self._event_kinds:
            raise ValueError("unsupported pause journal event")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PauseJournalError("pause journal parent cannot be created") from exc
        directory_flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                           | getattr(os, "O_NOFOLLOW", 0))
        directory_fd = None
        try:
            directory_fd = os.open(self.path.parent, directory_flags)
            if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                raise PauseJournalError("pause journal parent is not a directory")
        except (OSError, PauseJournalError) as exc:
            if directory_fd is not None:
                os.close(directory_fd)
            raise PauseJournalError("pause journal parent cannot be opened safely") from exc
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path.name, flags, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            os.close(directory_fd)
            raise PauseJournalError("pause journal cannot be written") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PauseJournalError("pause journal is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            events = self._read_fd(fd)
            event = PauseEvent(
                sequence=len(events) + 1, event_id=str(uuid4()), pause_id=pause_id,
                kind=kind, occurred_at=occurred_at, binding=binding,
                request_id=request_id, status=status,
                unknown_tool_call_ids=tuple(unknown_tool_call_ids),
            )
            payload = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise PauseJournalError("pause journal write made no progress")
                view = view[written:]
            os.fsync(fd)
            # File fsync alone does not durably publish a newly created name.
            # Syncing the checked containing directory on every append also
            # covers concurrent first creators without a racy existence test.
            os.fsync(directory_fd)
            return event
        except (OSError, ValueError) as exc:
            if isinstance(exc, PauseJournalError):
                raise
            raise PauseJournalError("pause journal append failed") from exc
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                os.close(directory_fd)
