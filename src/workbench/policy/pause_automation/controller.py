"""Pause/resume policy over the existing Task, review admission, and G3 ports."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import select
import sqlite3
import stat
import subprocess
from threading import Condition, Lock, RLock, local
import time
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4, uuid5

from workbench.contracts.ports_v2 import dispatch_allowed, parse_port, resume_allowed
from workbench.contracts.v1 import ActorRole
from workbench.ipc.bridge_g3.mailbox import (
    DeliveryReceipt, G3BridgeServer, TaskMailbox, MailboxMessage,
)
from workbench.observation.worker_review import (
    ActiveRunRef, ReviewAdmissionFence, ReviewFenceToken, SerializedReviewAdmission,
)
from workbench.tasks.repository import persisted_run_guard
from .journal import PauseBinding, PauseJournal, PauseJournalError


class PausePolicyError(RuntimeError):
    """A pause or resume cannot be safely bound to current approved state."""


@dataclass(frozen=True, slots=True)
class PauseStatus:
    binding: PauseBinding | None
    paused: bool
    cancelled: bool
    pause_id: str | None
    requested_at: str | None
    manager_ack_status: str | None
    worker_ack_status: str | None
    abort_status: str
    stop_observed_at: str | None
    unknown_tool_call_ids: tuple[str, ...]
    last_checked_at: str | None
    persistence_error: str | None
    manager_unknown_tool_call_ids: tuple[str, ...] = ()
    worker_unknown_tool_call_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": None if self.binding is None else self.binding.to_dict(),
            "paused": self.paused, "cancelled": self.cancelled, "pause_id": self.pause_id,
            "requested_at": self.requested_at, "manager_ack_status": self.manager_ack_status,
            "worker_ack_status": self.worker_ack_status, "abort_status": self.abort_status,
            "stop_observed_at": self.stop_observed_at,
            "unknown_tool_call_ids": list(self.unknown_tool_call_ids),
            "manager_unknown_tool_call_ids": list(self.manager_unknown_tool_call_ids),
            "worker_unknown_tool_call_ids": list(self.worker_unknown_tool_call_ids),
            "last_checked_at": self.last_checked_at,
            "persistence_error": self.persistence_error,
        }


class _CombinedAuthority:
    """Retain both CW-12 and caller authority at the final G3 send boundary."""

    def __init__(self, pause: object, lifecycle: object):
        if not callable(getattr(lifecycle, "current", None)):
            raise TypeError("authority token must expose current()")
        self._pause = pause
        self._lifecycle = lifecycle

    def current(self) -> bool:
        try:
            return (self._lifecycle.current() is True
                    and self._pause.current() is True)
        except Exception:
            return False

    def claim(self) -> bool:
        try:
            claim = getattr(self._lifecycle, "claim", None)
            return (callable(claim) and self._pause.current() is True
                    and claim() is True and self._pause.current() is True)
        except Exception:
            return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _safe_tool_ids(*sources: object) -> tuple[str, ...]:
    values: list[str] = []
    for source in sources:
        if isinstance(source, (list, tuple)):
            values.extend(value for value in source if isinstance(value, str) and value)
    return tuple(dict.fromkeys(values))


def _role_state(bridge: object, role: ActorRole, timeout: float) -> dict[str, Any]:
    result = bridge.probe(role, timeout=timeout)
    if not isinstance(result, dict):
        raise PausePolicyError("OMP returned an invalid public state snapshot")
    return result


_MAX_OBSERVED_FILES = 2048
_MAX_OBSERVED_BYTES = 64 * 1024 * 1024
_MAX_GIT_NAME_BYTES = 8 * 1024 * 1024
_MAX_GIT_RECORDS = 100_000
_MAX_TRACKED_MODE_FILES = 10_000
_MAX_TRACKED_MODE_NAME_BYTES = 8 * 1024 * 1024
_MAX_DIRECTORY_ENTRIES = 10_000
_MAX_DIRECTORY_DEPTH = 128


def _file_digest(path: Path, remaining_bytes: int) -> tuple[str, int]:
    """Hash one regular file without reading beyond the observation budget."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        digest, total, after = _digest_fd(fd, remaining_bytes)
        path_after = path.lstat()
        if not stat.S_ISREG(path_after.st_mode) or not _same_file(after, path_after):
            raise PausePolicyError("file changed while it was observed")
        return digest, total
    finally:
        os.close(fd)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return ((left.st_dev, left.st_ino, left.st_mode, left.st_size,
             left.st_mtime_ns, left.st_ctime_ns)
            == (right.st_dev, right.st_ino, right.st_mode, right.st_size,
                right.st_mtime_ns, right.st_ctime_ns))


def _directory_identity(info: os.stat_result) -> str:
    return f"v4:{info.st_mode:06o}:{info.st_dev}:{info.st_ino}"


def _approved_relative_path(name: str, approved_paths: tuple[str, ...]) -> bool:
    if not name:
        return False  # A worktree/peer root is never an approved file delta.
    relative = name.rstrip("/")
    return any(relative == approved.rstrip("/") or
               approved.endswith("/") and relative.startswith(approved)
               for approved in approved_paths)


def _digest_fd(fd: int, remaining_bytes: int) -> tuple[str, int, os.stat_result]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > remaining_bytes:
        raise PausePolicyError("file observation is unsafe or exceeds its bound")
    digest = sha256()
    total = 0
    while True:
        chunk = os.read(fd, min(64 * 1024, remaining_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > remaining_bytes:
            raise PausePolicyError("file observation exceeds its byte budget")
        digest.update(chunk)
    after = os.fstat(fd)
    if total != before.st_size or not _same_file(before, after):
        raise PausePolicyError("file changed while it was observed")
    return f"v2:{after.st_mode:06o}:{digest.hexdigest()}", total, after


def _changed_path_digest(root_fd: int, name: str, remaining_bytes: int) -> tuple[str, int]:
    """Open each Git-reported component relative to a pinned worktree fd."""
    parent_fd = root_fd
    opened: list[tuple[int, int, str, os.stat_result]] = []
    parts = Path(name).parts
    try:
        missing = False
        for part in parts[:-1]:
            try:
                before = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                missing = True
                break
            if not stat.S_ISDIR(before.st_mode):
                raise PausePolicyError("changed file has an unsafe directory component")
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         dir_fd=parent_fd)
            if not _same_file(before, os.fstat(fd)):
                os.close(fd)
                raise PausePolicyError("changed directory moved during observation")
            opened.append((fd, parent_fd, part, before))
            parent_fd = fd
        result = ("v2:missing", 0)
        if not missing:
            try:
                before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(before.st_mode):
                    raise PausePolicyError("changed path is not a regular file")
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent_fd)
                try:
                    digest, size, after = _digest_fd(fd, remaining_bytes)
                    path_after = os.stat(parts[-1], dir_fd=parent_fd,
                                         follow_symlinks=False)
                    if not _same_file(before, after) or not _same_file(after, path_after):
                        raise PausePolicyError("changed file moved during observation")
                    result = (digest, size)
                finally:
                    os.close(fd)
        for fd, parent, part, before in reversed(opened):
            if (not _same_file(before, os.fstat(fd))
                    or not _same_file(before, os.stat(part, dir_fd=parent,
                                                      follow_symlinks=False))):
                raise PausePolicyError("changed directory moved during observation")
        return result
    finally:
        for fd, _, _, _ in reversed(opened):
            os.close(fd)


def _tracked_path_mode(root_fd: int, name: str, *, sparse_missing: bool) -> str:
    """Stat a tracked file through pinned, non-symlink directory descriptors."""
    parent_fd = root_fd
    opened: list[tuple[int, int, str, os.stat_result]] = []
    parts = Path(name).parts
    try:
        missing_parent = False
        for part in parts[:-1]:
            try:
                before = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not sparse_missing:
                    raise PausePolicyError("tracked directory is missing") from None
                missing_parent = True
                break
            if not stat.S_ISDIR(before.st_mode):
                raise PausePolicyError("tracked path has an unsafe directory component")
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         dir_fd=parent_fd)
            if not _same_file(before, os.fstat(fd)):
                os.close(fd)
                raise PausePolicyError("tracked directory moved during observation")
            opened.append((fd, parent_fd, part, before))
            parent_fd = fd
        if missing_parent:
            result = "v3:missing"
        else:
            try:
                before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not sparse_missing:
                    raise PausePolicyError("tracked file is missing") from None
                result = "v3:missing"
            else:
                if not stat.S_ISREG(before.st_mode):
                    raise PausePolicyError("tracked path is not a regular file")
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent_fd)
                try:
                    after = os.fstat(fd)
                    path_after = os.stat(parts[-1], dir_fd=parent_fd,
                                         follow_symlinks=False)
                    if not _same_file(before, after) or not _same_file(after, path_after):
                        raise PausePolicyError("tracked file moved during observation")
                    result = f"v3:{after.st_mode:06o}"
                finally:
                    os.close(fd)
        for fd, parent, part, before in reversed(opened):
            if (not _same_file(before, os.fstat(fd))
                    or not _same_file(before, os.stat(part, dir_fd=parent,
                                                      follow_symlinks=False))):
                raise PausePolicyError("tracked directory moved during observation")
        return result
    except OSError as exc:
        raise PausePolicyError("tracked mode observation is unavailable") from exc
    finally:
        for fd, _, _, _ in reversed(opened):
            os.close(fd)


class PauseCoordinator:
    """Fail-closed automation fence with explicit, non-replaying resume."""

    def __init__(self, *, bridge: object, admission: SerializedReviewAdmission,
                 automation_state: Callable[[], object], request_timeout: float = 3.0,
                 stop_timeout: float = 0.25, clock: Callable[[], str] = _utc_now):
        if not isinstance(admission, SerializedReviewAdmission):
            raise TypeError("SerializedReviewAdmission is required")
        if request_timeout <= 0 or stop_timeout < 0:
            raise ValueError("timeouts must be positive (stop_timeout may be zero)")
        self._bridge = bridge
        self._admission = admission
        self._source = automation_state
        self._request_timeout = request_timeout
        self._stop_timeout = stop_timeout
        self._clock = clock
        self._lock = RLock()
        self._transition_condition = Condition(Lock())
        self._transition_active = False
        self._transition_kind: str | None = None
        self._state_version = 0
        self._authority_fence = ReviewAdmissionFence()
        admission.install_authority_fence(self._authority_fence)
        self._dispatch_action = local()
        self._run: object | None = None
        self._binding: PauseBinding | None = None
        self._bound_active_run: ActiveRunRef | None = None
        self._bound_incarnation = 0
        self._journal: PauseJournal | None = None
        self._pause_override: bool | None = None
        self._cancelled = False
        self._dispatch_fenced = False
        self._pause_fence_pending = False
        self._cancel_fence_pending = False
        self._cancel_journaled = False
        self._pause_id: str | None = None
        self._requested_at: str | None = None
        self._manager_ack_status: str | None = None
        self._worker_ack_status: str | None = None
        self._abort_status = "none"
        self._stop_observed_at: str | None = None
        self._unknown_tool_call_ids: tuple[str, ...] = ()
        self._manager_unknown_tool_call_ids: tuple[str, ...] = ()
        self._worker_unknown_tool_call_ids: tuple[str, ...] = ()
        self._last_checked_at: str | None = None
        self._persistence_error: str | None = None
        self._base_cache = self._read_base_state()
        admission.install_peer_preflight(self._preflight_peer)

    def _read_base_state(self) -> dict[str, Any]:
        try:
            port = parse_port(self._source())
            if port["kind"] != "AutomationState":
                raise ValueError("AutomationState required")
            return port
        except Exception:
            return {"portVersion": 2, "kind": "AutomationState", "payload": {
                "paused": True, "cancelled": self._cancelled,
                "metadataHealthy": False, "approvalValid": False,
            }}

    def _refresh_base_state(self) -> None:
        observed = self._read_base_state()
        with self._lock:
            self._base_cache = observed

    def _base_state(self) -> dict[str, Any]:
        return dict(self._base_cache)

    @contextmanager
    def _transition(self, kind: str):
        # The condition is never held while reaching CW11, G3, or user code.
        # A callback already holding CW11 cannot wait on a transition that
        # itself needs the CW11 lock to finish.
        with self._transition_condition:
            if self._transition_active and self._admission.in_dispatch_callback():
                raise PausePolicyError("transition cannot wait inside a review callback")
            while self._transition_active:
                self._transition_condition.wait()
            self._transition_active = True
            self._transition_kind = kind
        try:
            yield
        finally:
            with self._transition_condition:
                self._transition_active = False
                self._transition_kind = None
                self._transition_condition.notify_all()

    def _state_snapshot(self, *, paused: bool | None = None,
                        cancelled: bool | None = None,
                        include_pending: bool = True) -> dict[str, Any]:
        """Build an AutomationState while holding the coordinator lock only."""
        state = self._base_state()
        state["payload"] = dict(state["payload"])
        if paused is not None:
            state["payload"]["paused"] = paused
        elif self._pause_override is not None:
            state["payload"]["paused"] = self._pause_override
        if include_pending and self._pause_fence_pending:
            state["payload"]["paused"] = True
        if paused is None and self._binding is not None and self._authority_fence.closed():
            state["payload"]["paused"] = True
        if cancelled is not None:
            state["payload"]["cancelled"] = cancelled
        else:
            state["payload"]["cancelled"] = (
                state["payload"].get("cancelled") is True or self._cancelled
                or include_pending and self._cancel_fence_pending
            )
        if self._persistence_error == "repository_state_unavailable":
            state["payload"]["metadataHealthy"] = False
            state["payload"]["approvalValid"] = False
        return state

    def automation_state(self) -> dict[str, Any]:
        """Current versioned port for TaskWorkflow and WorkerReviewScheduler."""
        self._refresh_base_state()
        if not getattr(self._dispatch_action, "active", False):
            self._sync_repository_cancellation()
        with self._lock:
            return self._state_snapshot()

    def dispatch_automatic(self, action: Callable[[], Any]) -> tuple[str, Any | None]:
        """Local admission only; external OMP delivery uses a bound adapter."""
        self._refresh_base_state()
        self._sync_repository_cancellation()
        if not self._preflight_peer():
            return "held", None
        with self._lock:
            binding, run, incarnation = (self._binding, self._bound_active_run,
                                         self._bound_incarnation)
            if (self._dispatch_fenced or self._pause_fence_pending or self._cancel_fence_pending
                    or binding is None or run is None
                    or not dispatch_allowed(self._state_snapshot())):
                return "held", None
            permit = self._authority_fence.permit(run, incarnation)
        if permit is None:
            return "held", None
        with self._persisted_run_permit(binding) as current:
            if not current or not self._permit_current(run, incarnation, binding, permit):
                return "held", None
            self._dispatch_action.active = True
            try:
                result = action()
            finally:
                self._dispatch_action.active = False
        return (("admitted", result) if self._permit_current(
            run, incarnation, binding, permit) else ("held", None))

    @staticmethod
    def _expected_peers(binding: PauseBinding) -> dict[ActorRole, tuple[str, int]]:
        return {
            ActorRole.MANAGER: (binding.manager_session_id, binding.manager_generation),
            ActorRole.WORKER: (binding.worker_session_id, binding.worker_generation),
        }

    def _preflight_peer(self) -> bool:
        """Unfenced local hint; G3 submission does the exact transport check."""
        token = self._authority_fence.token()
        if token.mode != "open":
            return False
        with self._lock:
            binding, run, incarnation = (self._binding, self._bound_active_run,
                                         self._bound_incarnation)
        active, current_incarnation = self._admission.active_run_snapshot()
        ready = (binding is not None and run is not None
                 and active == run and current_incarnation == incarnation
                 and self._active_matches_binding(run, incarnation, binding)
                 and self._peer_identity_current(binding))
        if not ready:
            self._authority_fence.close_if_binding(
                token, "reconciliation_needed", exact_generation=True)
            return False
        return token.current()

    @staticmethod
    def _active_matches_binding(run: ActiveRunRef | None, incarnation: int,
                                binding: PauseBinding | None) -> bool:
        if binding is None:
            return False
        try:
            revision_id = str(uuid5(UUID(binding.task_id),
                                    f"task-spec-revision:{binding.revision}"))
        except (TypeError, ValueError, AttributeError):
            return False
        return (isinstance(run, ActiveRunRef) and run.active
                and type(incarnation) is int and incarnation >= 1
                and (run.task_id, run.revision_id, run.revision, run.run_id,
                     run.session_id, run.session_generation) ==
                    (binding.task_id, revision_id, binding.revision, binding.run_id,
                     binding.worker_session_id, binding.worker_generation))

    def _permit_current(self, run: ActiveRunRef, incarnation: int,
                        binding: PauseBinding, permit: ReviewFenceToken) -> bool:
        active, current_incarnation = self._admission.active_run_snapshot()
        if active != run or current_incarnation != incarnation:
            return False
        with self._lock:
            return (self._binding is binding and self._bound_active_run == run
                    and self._bound_incarnation == incarnation
                    and self._authority_fence.current(permit))

    @contextmanager
    def _persisted_run_permit(self, binding: PauseBinding):
        """Serialize dispatch opening with terminal active_runs deletion."""
        with self._lock:
            repository = getattr(self._run, "repository", None)
            path = getattr(repository, "path", None)
        if not isinstance(path, str) or not path or path == ":memory:" or not Path(path).is_file():
            yield False
            return
        with persisted_run_guard(path):
            try:
                with closing(sqlite3.connect(path, timeout=5)) as connection:
                    current = self._exact_persisted_run(connection, binding)
            except sqlite3.Error:
                current = False
            yield current

    @staticmethod
    def _exact_persisted_run(connection: sqlite3.Connection,
                             binding: PauseBinding) -> bool:
        row = connection.execute(
            """SELECT r.task_id, r.revision, r.run_id FROM active_runs a
               JOIN runs r ON r.run_id = a.run_id WHERE a.task_id = ?""",
            (binding.task_id,),
        ).fetchone()
        return row is not None and tuple(row) == (
            binding.task_id, binding.revision, binding.run_id)

    def _bound_authority_current(self, binding: PauseBinding,
                                 token: ReviewFenceToken) -> bool:
        with self._lock:
            run, incarnation = self._bound_active_run, self._bound_incarnation
            same_binding = self._binding is binding
        active, current_incarnation = self._admission.active_run_snapshot()
        return (same_binding and active == run and current_incarnation == incarnation
                and self._active_matches_binding(run, incarnation, binding)
                and token.run_identity == run.identity
                and token.run_incarnation == incarnation
                and token.current())

    def _bound_request(self, role: ActorRole, frame: Mapping[str, Any],
                       binding: PauseBinding, token: object, *, pair: bool = False) -> dict[str, Any]:
        if isinstance(self._bridge, G3BridgeServer):
            peers = self._expected_peers(binding)
            return self._bridge.request(
                role, frame, self._request_timeout,
                expected_peer=peers[role],
                expected_peers=peers if pair else None,
                authority_token=token,
            )
        # Legacy fake bridges remain usable for local policy tests. Real OMP
        # safety is enforced only by the bound G3 transport path above.
        return self._bridge.request(role, frame, self._request_timeout)

    def dispatch_bound_automatic(self, mailbox: TaskMailbox, message: MailboxMessage,
                                 *, timeout: float = 20,
                                 authority_token: object | None = None) -> tuple[str, Any | None]:
        """Submit an automatic OMP delivery with exact run/peers/authority."""
        if not isinstance(mailbox, TaskMailbox):
            raise TypeError("TaskMailbox required for bound automatic delivery")
        self._refresh_base_state()
        self._sync_repository_cancellation()
        if not self._preflight_peer():
            return "held", None
        with self._lock:
            binding, run, incarnation = (self._binding, self._bound_active_run,
                                         self._bound_incarnation)
            if (binding is None or self._dispatch_fenced or self._pause_fence_pending
                    or self._cancel_fence_pending or run is None
                    or not dispatch_allowed(self._state_snapshot())
                    or (message.task_id, message.revision, message.run_id) !=
                    (binding.task_id, binding.revision, binding.run_id)):
                return "held", None
            permit = self._authority_fence.permit(run, incarnation)
        if permit is None:
            return "held", None
        combined = (permit if authority_token is None
                    else _CombinedAuthority(permit, authority_token))
        with self._persisted_run_permit(binding) as current:
            if (not current or not self._permit_current(run, incarnation, binding, permit)
                    or not combined.current()):
                return "held", None
            self._dispatch_action.active = True
            try:
                result = mailbox.deliver(
                    message, timeout=timeout,
                    expected_peers=self._expected_peers(binding), authority_token=combined,
                )
            finally:
                self._dispatch_action.active = False
        if (self._permit_current(run, incarnation, binding, permit)
                and combined.current()):
            return "admitted", result
        # A durable receipt may already prove submission. Do not report that as
        # cancelled merely because authority changed after mailbox.deliver.
        return "held", result if isinstance(result, DeliveryReceipt) else None

    def _sync_repository_cancellation(self) -> None:
        """Mirror cancellation of this exact persisted run before policy boundaries."""
        with self._lock:
            run, binding = self._run, self._binding
            if (run is None or binding is None or self._cancelled or self._cancel_fence_pending
                    or self._persistence_error == "binding_mismatch"):
                return
            fence_token = self._authority_fence.token()
        try:
            cancelled = self._repository_cancelled(
                run.repository, binding.task_id, binding.revision, binding.run_id)
        except Exception:
            if self._authority_fence.close_if_binding(
                    fence_token, "repository_state_unavailable") is None:
                return
            with self._lock:
                if not self._same_run(self._binding, binding):
                    return
                if self._persistence_error == "binding_mismatch":
                    return
                self._persistence_error = "repository_state_unavailable"
                self._dispatch_fenced = True
                self._state_version += 1
                state = self._state_snapshot(paused=True)
                version = self._state_version
            self._publish_admission_state(state, fail_closed=True, expected_version=version)
            return
        if not cancelled:
            return
        if self._authority_fence.close_if_binding(
                fence_token, "repository_cancelled") is None:
            return
        with self._lock:
            if (not self._same_run(self._binding, binding) or self._cancelled
                    or self._cancel_fence_pending):
                return
            self._cancel_fence_pending = True
            self._dispatch_fenced = True
            self._state_version += 1
            if self._pause_id is None:
                self._pause_id = str(uuid4())
                self._requested_at = self._clock()
            state = self._state_snapshot(cancelled=True)
            version = self._state_version
        # CW11 is updated without the coordinator lock. Its lock may be held by a
        # review callback that reads this coordinator, so the reverse order is unsafe.
        published = self._publish_admission_state(state, fail_closed=True,
                                                  expected_version=version)
        cancel_record = None
        with self._lock:
            if not self._same_run(self._binding, binding):
                return
            if not self._cancel_journaled:
                # Set before append: a write/fsync error after bytes reached disk must
                # not produce a duplicate cancellation event on the next boundary.
                self._cancel_journaled = True
                cancel_record = (self._journal, self._binding, self._pause_id)
            self._cancelled = True
            self._cancel_fence_pending = False
            self._dispatch_fenced = not published
            self._last_checked_at = self._clock()
            if not published and self._persistence_error is None:
                self._persistence_error = "admission_state_unavailable"
        if cancel_record is not None:
            journal, recorded_binding, pause_id = cancel_record
            try:
                journal.append(pause_id=pause_id, kind="cancelled",
                               occurred_at=self._clock(), binding=recorded_binding,
                               status="repository_cancelled")
            except PauseJournalError as exc:
                with self._lock:
                    if self._same_run(self._binding, recorded_binding):
                        self._persistence_error = type(exc).__name__

    @staticmethod
    def _repository_cancelled(repository: object, task_id: str, revision: int,
                              run_id: str) -> bool:
        persisted = repository.get_run(run_id)
        if (persisted.get("run_id") != run_id or persisted.get("task_id") != task_id
                or persisted.get("revision") != revision):
            raise PausePolicyError("persisted run identity differs from the bound Task")
        return any(event.get("kind") == "cancelled"
                   for event in repository.get_run_history(run_id))

    @staticmethod
    def _sessions(bridge: object, timeout: float) -> tuple[tuple[str | None, int | None], tuple[str | None, int | None]]:
        observed: list[tuple[str | None, int | None]] = []
        for role in (ActorRole.MANAGER, ActorRole.WORKER):
            try:
                state = _role_state(bridge, role, timeout)
                session_id, generation = state.get("sessionId"), state.get("generation")
                if not isinstance(session_id, str) or not session_id or type(generation) is not int or generation < 1:
                    observed.append((None, None))
                else:
                    observed.append((session_id, generation))
            except Exception:
                observed.append((None, None))
        return observed[0], observed[1]

    def _persisted_binding(self, run: object) -> PauseBinding:
        try:
            repository = run.repository
            task_id, revision, run_id = run.task_id, run.revision, run.run_id
            run_cancelled = self._repository_cancelled(repository, task_id, revision, run_id)
            current = repository.get_current_run(task_id)
            if (not run_cancelled and
                    (not isinstance(current, Mapping) or current.get("run_id") != run_id
                     or current.get("revision") != revision)):
                raise PausePolicyError("Task/revision/run is no longer the current run")
            task = repository.get_task_spec(task_id, revision)
            decisions = repository.get_decisions(task_id, revision)
            approvals = [item for item in decisions if item.get("kind") == "scope_approved"]
            if not approvals:
                raise PausePolicyError("persisted approved scope is missing")
            approval = approvals[-1]
            approval_hash = sha256(_canonical({"task": task, "approval": approval})).hexdigest()
            if approval_hash != run.approval_hash:
                raise PausePolicyError("persisted Task approval changed")
            scope = approval.get("details")
            if not isinstance(scope, Mapping) or not scope:
                raise PausePolicyError("persisted approved scope is invalid")
            paths = scope.get("paths", [])
            approved_paths = tuple(path for path in paths if isinstance(path, str) and path) if isinstance(paths, list) else ()
            manager, worker = self._sessions(self._bridge, min(self._request_timeout, 2.0))
            return PauseBinding(
                task_id=task_id, revision=revision, run_id=run_id,
                approval_hash=approval_hash,
                approved_scope_hash=sha256(_canonical(scope)).hexdigest(),
                approved_paths=approved_paths,
                manager_session_id=manager[0], manager_generation=manager[1],
                worker_session_id=worker[0], worker_generation=worker[1],
            )
        except PausePolicyError:
            raise
        except Exception as exc:
            raise PausePolicyError("current Task authority could not be verified") from exc

    def bind_run(self, run: object) -> PauseStatus:
        """Bind a live CW-10 run and restore any durable pause across restarts."""
        self._refresh_base_state()
        entry_token = self._authority_fence.token()
        with self._transition("bind"):
            active, incarnation = self._admission.active_run_snapshot()
            if (active is None or not active.active
                    or (active.task_id, active.revision, active.run_id) !=
                    (getattr(run, "task_id", None), getattr(run, "revision", None),
                     getattr(run, "run_id", None))):
                active = None
                incarnation = 0
            binding_token, uninterrupted = self._authority_fence.bind(
                active, incarnation, entry_token)
            self._bind_run(run)
            with self._lock:
                bound = self._binding
                needs_entry_baseline = (active is not None and self._pause_override is not True
                                        and not self._cancelled and self._persistence_error is None)
            if needs_entry_baseline and bound is not None:
                try:
                    self._ensure_entry_file_baseline(run, bound)
                except Exception:
                    with self._lock:
                        if self._run is run and self._binding is bound:
                            self._persistence_error = "entry_baseline_unavailable"
                            self._dispatch_fenced = True
                            self._state_version += 1
                    self._update_admission()
            current_active, current_incarnation = self._admission.active_run_snapshot()
            with self._lock:
                binding = self._binding
                matching = (current_active == active and current_incarnation == incarnation
                            and self._active_matches_binding(active, incarnation, binding))
                self._bound_active_run = active if matching else None
                self._bound_incarnation = incarnation if matching else 0
                eligible = (self._run is run and binding is not None
                            and matching
                            and self._pause_override is not True and not self._cancelled
                            and not self._pause_fence_pending
                            and self._persistence_error is None
                            and dispatch_allowed(self._base_state()))
            if (eligible and uninterrupted and self._peer_identity_current(binding)
                    and self._authority_fence.open(binding_token) is not None):
                self._update_admission()
            elif not matching:
                self._authority_fence.close_if_binding(
                    binding_token, "reconciliation_needed", exact_generation=True)
            return self.status()

    def _peer_identity_current(self, binding: PauseBinding) -> bool:
        for role, session_id, generation in (
            (ActorRole.MANAGER, binding.manager_session_id, binding.manager_generation),
            (ActorRole.WORKER, binding.worker_session_id, binding.worker_generation),
            (ActorRole.MANAGER, binding.manager_session_id, binding.manager_generation),
            (ActorRole.WORKER, binding.worker_session_id, binding.worker_generation),
        ):
            if (not isinstance(session_id, str) or not session_id
                    or type(generation) is not int or generation < 1):
                return False
            try:
                state = _role_state(self._bridge, role, min(self._request_timeout, 2.0))
            except Exception:
                return False
            if (state.get("role") != role.value or state.get("sessionId") != session_id
                    or type(state.get("generation")) is not int
                    or state["generation"] != generation):
                return False
        return True

    def _bind_run(self, run: object) -> PauseStatus:
        binding = self._persisted_binding(run)
        journal = PauseJournal(run.record_dir / "pause.jsonl")
        try:
            events = journal.read()
        except PauseJournalError as exc:
            with self._lock:
                self._reset_per_run_state()
                self._run, self._binding, self._journal = run, binding, journal
                self._pause_override = True
                self._persistence_error = type(exc).__name__
                self._dispatch_fenced = True
                self._state_version += 1
            self._update_admission()
            return self.status()
        latest = events[-1] if events else None
        if latest is not None and not self._same_scope(latest.binding, binding):
            with self._lock:
                self._reset_per_run_state()
                self._run, self._binding, self._journal = run, latest.binding, journal
                self._restore_events(events)
                self._pause_override = True
                self._persistence_error = "binding_mismatch"
                self._dispatch_fenced = True
                self._state_version += 1
            self._update_admission()
            return self.status()
        with self._lock:
            same_run = self._same_run(self._binding, binding)
            if same_run and not events and self._has_run_observations():
                self._run, self._binding, self._journal = run, binding, journal
                self._pause_override = True
                self._persistence_error = "journal_missing"
                self._dispatch_fenced = True
            else:
                self._reset_per_run_state()
                self._run, self._binding, self._journal = run, binding, journal
                if events:
                    self._restore_events(events)
            self._state_version += 1
        self._update_admission()
        return self.status()

    def _reset_per_run_state(self) -> None:
        self._bound_active_run = None
        self._bound_incarnation = 0
        self._pause_override = None
        self._cancelled = False
        self._dispatch_fenced = False
        self._pause_fence_pending = False
        self._cancel_fence_pending = False
        self._cancel_journaled = False
        self._pause_id = None
        self._requested_at = None
        self._manager_ack_status = None
        self._worker_ack_status = None
        self._abort_status = "none"
        self._stop_observed_at = None
        self._unknown_tool_call_ids = ()
        self._manager_unknown_tool_call_ids = ()
        self._worker_unknown_tool_call_ids = ()
        self._last_checked_at = None
        self._persistence_error = None

    @staticmethod
    def _same_run(left: PauseBinding | None, right: PauseBinding) -> bool:
        return left is not None and (left.task_id, left.revision, left.run_id) == (
            right.task_id, right.revision, right.run_id,
        )

    def _has_run_observations(self) -> bool:
        return bool(self._pause_id or self._cancelled or self._manager_ack_status
                    or self._worker_ack_status or self._stop_observed_at
                    or self._unknown_tool_call_ids or self._last_checked_at)

    def _restore_events(self, events: tuple[Any, ...]) -> None:
        pause_requests = [event for event in events if event.kind == "pause_requested"]
        transitions = [event for event in events if event.kind in {
            "pause_requested", "resumed", "resume_failed",
        }]
        active_pause_id = pause_requests[-1].pause_id if pause_requests else None
        latest_transition = transitions[-1] if transitions else None
        self._pause_override = (None if latest_transition is None
                                else latest_transition.kind != "resumed")
        self._cancelled = any(event.kind == "cancelled" for event in events)
        self._cancel_journaled = self._cancelled
        self._pause_id = (active_pause_id or next(
            (event.pause_id for event in reversed(events) if event.kind == "cancelled"), None,
        ))
        self._requested_at = pause_requests[-1].occurred_at if pause_requests else None
        self._unknown_tool_call_ids = tuple(dict.fromkeys(
            item for event in events for item in event.unknown_tool_call_ids
        ))
        self._manager_unknown_tool_call_ids = tuple(dict.fromkeys(
            item for event in events if event.kind in {"manager_pause_ack", "turn_stop_observed"}
            for item in event.unknown_tool_call_ids
        ))
        self._worker_unknown_tool_call_ids = tuple(dict.fromkeys(
            item for event in events if event.kind == "worker_pause_ack"
            for item in event.unknown_tool_call_ids
        ))
        self._abort_status = "none"
        self._manager_ack_status = None
        self._worker_ack_status = None
        self._stop_observed_at = None
        if active_pause_id is not None:
            self._abort_status = "unknown"
            for event in events:
                if event.pause_id != active_pause_id:
                    continue
                if event.kind == "manager_pause_ack":
                    self._manager_ack_status = event.status
                    self._abort_status = {
                        "paused": "not_needed", "abort_requested": "requested",
                        "abort_request_failed": "request_failed",
                    }.get(event.status, "unknown")
                elif event.kind == "worker_pause_ack":
                    self._worker_ack_status = event.status
                elif event.kind == "turn_stop_observed":
                    self._stop_observed_at = event.occurred_at
                    self._abort_status = "stop_observed"
        self._last_checked_at = events[-1].occurred_at if events else None

    @staticmethod
    def _same_scope(left: PauseBinding, right: PauseBinding) -> bool:
        return (left.task_id, left.revision, left.run_id, left.approval_hash,
                left.approved_scope_hash, left.approved_paths) == (
                    right.task_id, right.revision, right.run_id, right.approval_hash,
                    right.approved_scope_hash, right.approved_paths)

    def _append(self, kind: str, *, request_id: str | None = None, status: str | None = None,
                unknown_tool_call_ids: tuple[str, ...] = ()) -> None:
        journal, binding, pause_id = self._journal, self._binding, self._pause_id
        if journal is None or binding is None or pause_id is None:
            raise PausePolicyError("pause policy is not bound to a current run")
        journal.append(pause_id=pause_id, kind=kind, occurred_at=self._clock(), binding=binding,
                       request_id=request_id, status=status,
                       unknown_tool_call_ids=unknown_tool_call_ids)

    def _publish_admission_state(self, state: Mapping[str, Any], *,
                                 fail_closed: bool, expected_version: int | None = None) -> bool:
        # The authoritative fence, not this mirrored state, controls dispatch.
        # No coordinator/publication lock is held while entering CW11. An older
        # setter that finishes last repairs the mirror to the latest version.
        while True:
            with self._lock:
                if (expected_version is not None
                        and expected_version != self._state_version):
                    return False
            try:
                self._admission.set_automation_state(state)
                published = True
            except Exception:
                if not fail_closed:
                    return False
                payload = state.get("payload", {})
                fallback = {"portVersion": 2, "kind": "AutomationState", "payload": {
                    "paused": True, "cancelled": payload.get("cancelled") is True,
                    "metadataHealthy": False, "approvalValid": False,
                }}
                try:
                    self._admission.set_automation_state(fallback)
                    published = True
                except Exception:
                    return False
            with self._lock:
                if expected_version is None or expected_version == self._state_version:
                    return published
                expected_version = self._state_version
                state = self._state_snapshot()
                fail_closed = True

    def _update_admission(self) -> bool:
        self._refresh_base_state()
        with self._lock:
            state = self._state_snapshot()
            version = self._state_version
        published = self._publish_admission_state(state, fail_closed=True,
                                                  expected_version=version)
        if not published:
            with self._lock:
                if version != self._state_version:
                    return False
                self._dispatch_fenced = True
                if self._persistence_error is None:
                    self._persistence_error = "admission_state_unavailable"
        return published

    def _record(self, kind: str, *, request_id: str | None = None, status: str | None = None,
                unknown_tool_call_ids: tuple[str, ...] = ()) -> None:
        try:
            self._append(kind, request_id=request_id, status=status,
                         unknown_tool_call_ids=unknown_tool_call_ids)
        except PauseJournalError as exc:
            self._persistence_error = type(exc).__name__

    @staticmethod
    def _ack_status(response: object, allowed: frozenset[str]) -> str:
        if not isinstance(response, Mapping):
            return "unknown"
        status = response.get("status")
        return status if isinstance(status, str) and status in allowed else "unknown"

    def pause(self) -> PauseStatus:
        """Close local admission first, then request manager abort and pause both OMP peers."""
        with self._transition_condition:
            pause_already_running = (self._transition_active
                                     and self._transition_kind == "pause")
        if not pause_already_running:
            self._authority_fence.close()
        with self._transition("pause"):
            with self._lock:
                already_paused = self._pause_override is True
            if not already_paused:
                self._authority_fence.close()
            return self._pause()

    def _transition_current(self, run: object, binding: PauseBinding,
                            pause_id: str) -> bool:
        return (self._run is run and self._binding is binding
                and self._pause_id == pause_id)

    def _pause(self) -> PauseStatus:
        self._refresh_base_state()
        self._sync_repository_cancellation()
        pause_token = self._authority_fence.token()
        with self._lock:
            if self._run is None or self._binding is None or self._journal is None:
                raise PausePolicyError("bind a persisted current run before pausing")
            if self._cancelled or self._pause_override is True:
                already_paused = True
            else:
                already_paused = False
                if self._pause_fence_pending or self._cancel_fence_pending:
                    raise PausePolicyError("pause transition is already in progress")
                self._dispatch_fenced = True
                self._pause_fence_pending = True
                self._state_version += 1
                self._pause_id = str(uuid4())
                self._requested_at = self._clock()
                self._abort_status = "unknown"
                pause_binding = self._binding
                pause_run = self._run
                pause_id = self._pause_id
                paused_state = self._state_snapshot(paused=True)
                version = self._state_version
        if already_paused:
            return self.status()
        cursor = self._bridge.event_cursor()
        self._record("pause_requested")
        with self._lock:
            if not self._transition_current(pause_run, pause_binding, pause_id):
                raise PausePolicyError("pause run changed during durable pause request")
            self._pause_override = True
            self._pause_fence_pending = False
            self._dispatch_fenced = True
            self._last_checked_at = self._clock()

        manager_ack: Mapping[str, Any] | None = None
        manager_request_id: str | None = None
        try:
            value = self._bound_request(ActorRole.MANAGER, {"kind": "pause"},
                                        pause_binding, pause_token)
            manager_ack = value if isinstance(value, Mapping) else None
            if manager_ack and isinstance(manager_ack.get("requestId"), str):
                manager_request_id = manager_ack["requestId"]
            manager_status = self._ack_status(manager_ack, frozenset({"paused", "abort_requested", "abort_request_failed"}))
        except Exception:
            manager_status = "unknown"
        unknown = _safe_tool_ids(
            None if manager_ack is None else manager_ack.get("unconfirmedToolCallIds"),
            None if manager_ack is None else manager_ack.get("unknownOutcomeToolCallIds"),
        )
        with self._lock:
            if not self._transition_current(pause_run, pause_binding, pause_id):
                raise PausePolicyError("pause run changed during manager acknowledgement")
            self._manager_ack_status = manager_status
            self._unknown_tool_call_ids = tuple(dict.fromkeys((*self._unknown_tool_call_ids, *unknown)))
            self._manager_unknown_tool_call_ids = tuple(dict.fromkeys(
                (*self._manager_unknown_tool_call_ids, *unknown)
            ))
            self._abort_status = {
                "paused": "not_needed", "abort_requested": "requested",
                "abort_request_failed": "request_failed",
            }.get(manager_status, "unknown")
            self._last_checked_at = self._clock()
        self._record("manager_pause_ack", request_id=manager_request_id, status=manager_status,
                     unknown_tool_call_ids=unknown)

        manager_session_id = pause_binding.manager_session_id
        manager_generation = pause_binding.manager_generation
        if (manager_status == "abort_requested" and manager_request_id
                and isinstance(manager_session_id, str) and manager_session_id
                and type(manager_generation) is int and manager_generation >= 1):
            try:
                event = self._bridge.wait_event(
                    ActorRole.MANAGER, "turn_stop_observed", {
                        "abortRequestId": manager_request_id,
                        "sessionId": manager_session_id,
                        "generation": manager_generation,
                    },
                    after_sequence=cursor, timeout=self._stop_timeout,
                )
                if (isinstance(event, Mapping)
                        and event.get("role") == ActorRole.MANAGER.value
                        and event.get("name") == "turn_stop_observed"
                        and event.get("abortRequestId") == manager_request_id
                        and event.get("sessionId") == manager_session_id
                        and type(event.get("generation")) is int
                        and event["generation"] == manager_generation
                        and type(event.get("bridgeSequence")) is int
                        and event["bridgeSequence"] > cursor):
                    record_stop = False
                    with self._lock:
                        if (self._transition_current(pause_run, pause_binding, pause_id)
                                and self._pause_override is True):
                            self._abort_status = "stop_observed"
                            self._stop_observed_at = self._clock()
                            event_unknown = _safe_tool_ids(event.get("unconfirmedToolCallIds"))
                            self._unknown_tool_call_ids = tuple(dict.fromkeys((*self._unknown_tool_call_ids, *event_unknown)))
                            self._manager_unknown_tool_call_ids = tuple(dict.fromkeys(
                                (*self._manager_unknown_tool_call_ids, *event_unknown)
                            ))
                            record_stop = True
                    if record_stop:
                        self._record("turn_stop_observed", request_id=manager_request_id,
                                     status="stop_observed", unknown_tool_call_ids=event_unknown)
            except Exception:
                pass

        worker_ack: Mapping[str, Any] | None = None
        try:
            value = self._bound_request(ActorRole.WORKER, {"kind": "pause"},
                                        pause_binding, pause_token)
            worker_ack = value if isinstance(value, Mapping) else None
            worker_status = self._ack_status(worker_ack, frozenset({"paused", "abort_requested", "abort_request_failed"}))
        except Exception:
            worker_status = "unknown"
        worker_unknown = _safe_tool_ids(
            None if worker_ack is None else worker_ack.get("unconfirmedToolCallIds"),
            None if worker_ack is None else worker_ack.get("unknownOutcomeToolCallIds"),
        )
        with self._lock:
            if not self._transition_current(pause_run, pause_binding, pause_id):
                raise PausePolicyError("pause run changed during worker acknowledgement")
            self._worker_ack_status = worker_status
            self._unknown_tool_call_ids = tuple(dict.fromkeys((*self._unknown_tool_call_ids, *worker_unknown)))
            self._worker_unknown_tool_call_ids = tuple(dict.fromkeys(
                (*self._worker_unknown_tool_call_ids, *worker_unknown)
            ))
            self._last_checked_at = self._clock()
        self._record("worker_pause_ack", status=worker_status,
                     unknown_tool_call_ids=worker_unknown)
        published = self._publish_admission_state(paused_state, fail_closed=True,
                                                  expected_version=version)
        with self._lock:
            if self._transition_current(pause_run, pause_binding, pause_id):
                self._dispatch_fenced = not published
                if not published and version == self._state_version and self._persistence_error is None:
                    self._persistence_error = "admission_state_unavailable"
        try:
            self._persist_peer_file_baseline(pause_run, pause_binding, pause_id)
        except Exception:
            with self._lock:
                if self._transition_current(pause_run, pause_binding, pause_id):
                    self._persistence_error = "file_baseline_unavailable"
                    self._dispatch_fenced = True
        self.refresh()
        self._update_admission()
        return self.status()

    def refresh(self) -> PauseStatus:
        """Refresh only public state; logs/processes remain non-model observations."""
        observed: list[dict[str, Any] | None] = []
        for role in (ActorRole.MANAGER, ActorRole.WORKER):
            try:
                observed.append(_role_state(self._bridge, role, min(self._request_timeout, 2.0)))
            except Exception:
                observed.append(None)
        with self._lock:
            self._last_checked_at = self._clock()
            for role, state in zip(("manager", "worker"), observed):
                bound = self._binding
                expected = ((bound.manager_session_id, bound.manager_generation)
                            if role == "manager" else
                            (bound.worker_session_id, bound.worker_generation)) if bound else None
                if (state is not None and expected is not None
                        and expected[0] is not None and expected[1] is not None
                        and (state.get("sessionId"), state.get("generation")) == expected):
                    ids = _safe_tool_ids(state.get("unconfirmedToolCallIds"),
                                         state.get("unknownOutcomeToolCallIds"))
                    self._unknown_tool_call_ids = tuple(dict.fromkeys((*self._unknown_tool_call_ids, *ids)))
                    attribute = ("_manager_unknown_tool_call_ids" if role == "manager"
                                 else "_worker_unknown_tool_call_ids")
                    previous = getattr(self, attribute)
                    setattr(self, attribute, tuple(dict.fromkeys((*previous, *ids))))
            # The public snapshot has no abortRequestId. Even a matching
            # session cannot prove this stop belongs to our pause request;
            # only wait_event with the exact acknowledged request ID may do so.
        return self.status()

    def collect_non_model(self, run: object, *, timeout: float = 1.0) -> dict[str, Any]:
        """Keep the approved host run and its CW-10 log/process collection active."""
        paused = self.automation_state()["payload"]["paused"]
        result = run.collect(timeout=timeout, paused=paused)
        if not isinstance(result, dict):
            raise PausePolicyError("host observation returned an invalid result")
        return result

    def _validate_resume(self, run: object, evidence: object) -> tuple[dict[str, Any], dict[str, Any]]:
        current = self._persisted_binding(run)
        if self._repository_cancelled(
                run.repository, current.task_id, current.revision, current.run_id):
            raise PausePolicyError("a cancelled persisted run cannot be resumed")
        if self._binding is None or not self._same_scope(self._binding, current):
            raise PausePolicyError("current Task/revision/run/approval differs from paused scope")
        port = parse_port(evidence)
        if port["kind"] != "ResumeEvidence":
            raise PausePolicyError("ResumeEvidence is required")
        if hasattr(run, "worktree") and hasattr(run, "shell"):
            # Live WorkflowRun facts are owned by the coordinator. The caller
            # supplies intent and identity, never the reconciliation verdict.
            port = self._observed_resume_evidence(run, current, port)
        payload = port["payload"]
        if (payload["taskId"] != current.task_id or payload["runId"] != current.run_id
                or payload["approvalHash"] != current.approval_hash or not resume_allowed(port)):
            raise PausePolicyError("explicit resume and complete file/tool/process/task/approval reconciliation are required")
        checked_at = datetime.strptime(payload["checkedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.strptime(self._clock(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        requested = datetime.strptime(self._requested_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if self._requested_at else None
        if checked_at > now or requested is None or checked_at < requested:
            raise PausePolicyError("resume evidence is stale or not post-pause")
        return self._peers_reconciled(current)

    def _validate_finished_resume(self, run: object, _evidence: object) -> tuple[dict[str, Any], dict[str, Any]]:
        """CW-16 fix-02 (P2-2): the paused run's host command ended (exit confirmed, checked by the caller).

        Its process and cwd can no longer match the paused run, so only the parts that still apply are
        reconciled: the same current, uncancelled Task/revision/run/approval and both OMPs paused, idle and
        without in-flight tools in the bound sessions. Nothing of the run is replayed."""
        current = self._persisted_binding(run)
        if self._repository_cancelled(
                run.repository, current.task_id, current.revision, current.run_id):
            raise PausePolicyError("a cancelled persisted run cannot be resumed")
        if self._binding is None or not self._same_scope(self._binding, current):
            raise PausePolicyError("current Task/revision/run/approval differs from paused scope")
        return self._peers_reconciled(current)

    def _peers_reconciled(self, current: PauseBinding) -> tuple[dict[str, Any], dict[str, Any]]:
        manager = _role_state(self._bridge, ActorRole.MANAGER, min(self._request_timeout, 2.0))
        worker = _role_state(self._bridge, ActorRole.WORKER, min(self._request_timeout, 2.0))
        for role, state, session_id, generation in (
            ("manager", manager, current.manager_session_id, current.manager_generation),
            ("worker", worker, current.worker_session_id, current.worker_generation),
        ):
            bound_session = None
            if self._binding is not None:
                bound_session = ((self._binding.manager_session_id, self._binding.manager_generation)
                                 if role == "manager" else
                                 (self._binding.worker_session_id, self._binding.worker_generation))
            if (session_id is None or generation is None or bound_session != (session_id, generation)
                    or state.get("sessionId") != session_id
                    or state.get("generation") != generation or state.get("paused") is not True
                    or state.get("idle") is not True or state.get("pending") is not False
                    or state.get("approvalPending") is not False
                    or state.get("editorKnown") is not True or state.get("editorEmpty") is not True
                    or type(state.get("inFlightToolCount")) is not int
                    or state["inFlightToolCount"] != 0
                    or state.get("unresolvedPriorSessions") not in (None, [])):
                raise PausePolicyError(f"current {role} OMP state is not reconciled")
        if manager.get("abortStatus") not in {"none", "stop_observed"}:
            raise PausePolicyError("manager turn stop is not confirmed")
        return manager, worker

    @staticmethod
    def _bounded_git_records(command: tuple[str, ...], *,
                             max_bytes: int | None = None,
                             max_records: int | None = None) -> tuple[bytes, ...]:
        max_bytes = _MAX_GIT_NAME_BYTES if max_bytes is None else max_bytes
        max_records = _MAX_GIT_RECORDS if max_records is None else max_records
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            assert process.stdout is not None
            chunks = []
            total = 0
            deadline = time.monotonic() + 5
            try:
                while True:
                    wait = deadline - time.monotonic()
                    if wait <= 0 or not select.select([process.stdout], [], [], wait)[0]:
                        raise PausePolicyError("worktree file observation timed out")
                    chunk = os.read(process.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise PausePolicyError("worktree file names exceed their byte bound")
                    chunks.append(chunk)
                if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                    raise PausePolicyError("worktree file listing failed")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        raw = b"".join(chunks)
        if ((raw and not raw.endswith(b"\0"))
                or raw.count(b"\0") > max_records):
            raise PausePolicyError("Git file listing is malformed or exceeds its count bound")
        return tuple(name for name in raw.split(b"\0") if name)

    @staticmethod
    def _index_flags(root: Path) -> dict[str, str]:
        """Capture index flags that can suppress ordinary status reporting."""
        listings: list[dict[str, str]] = []
        for option in ("-v", "-t", "-f"):
            listing: dict[str, str] = {}
            for record in PauseCoordinator._bounded_git_records(
                    ("git", "-C", str(root), "ls-files", option, "-z")):
                if len(record) < 3 or record[1:2] != b" ":
                    raise PausePolicyError("Git index flag listing is invalid")
                tag = record[:1].decode("ascii")
                name = record[2:].decode("utf-8")
                if name in listing:
                    raise PausePolicyError("Git index contains ambiguous path stages")
                listing[name] = tag
            listings.append(listing)
        if any(set(listing) != set(listings[0]) for listing in listings[1:]):
            raise PausePolicyError("Git index changed during flag observation")
        flagged = {name: "|".join(listing[name] for listing in listings)
                   for name in listings[0]
                   if any(listing[name] != "H" for listing in listings)}
        if len(flagged) > _MAX_OBSERVED_FILES:
            raise PausePolicyError("Git flagged path count exceeds its bound")
        return flagged

    @staticmethod
    def _tracked_modes(root: Path, flagged_paths: Mapping[str, str]) -> dict[str, str]:
        """Capture modes for all tracked paths without reading clean file content."""
        names = PauseCoordinator._bounded_git_records(
            ("git", "-C", str(root), "ls-files", "--cached", "-z"),
            max_bytes=_MAX_TRACKED_MODE_NAME_BYTES,
            max_records=_MAX_TRACKED_MODE_FILES)
        observed: dict[str, str] = {}
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            before = os.fstat(root_fd)
            for raw in names:
                name = raw.decode("utf-8")
                relative = Path(name)
                if (not name or name in observed or relative.is_absolute()
                        or ".." in relative.parts or ".git" in relative.parts):
                    raise PausePolicyError("Git tracked path listing is unsafe")
                # A stable sparse skip-worktree absence is already represented by
                # the index flag snapshot; ordinary missing paths fail closed.
                sparse_missing = (flagged_paths.get(name, "").split("|")[1:2] == ["S"])
                observed[name] = _tracked_path_mode(root_fd, name,
                                                    sparse_missing=sparse_missing)
            after = os.fstat(root_fd)
            if not _same_file(before, after) or not _same_file(after, root.lstat()):
                raise PausePolicyError("worktree root changed during tracked mode observation")
        finally:
            os.close(root_fd)
        return observed

    @staticmethod
    def _changed_files(root: Path, flagged_paths: Mapping[str, str] | None = None) -> dict[str, str]:
        filemode = subprocess.run(
            ("git", "-C", str(root), "config", "--bool", "--get", "core.filemode"),
            capture_output=True, timeout=5, check=False)
        if filemode.returncode != 0 or filemode.stdout.strip() != b"true":
            raise PausePolicyError("Git file-mode observation is unavailable")
        paths: set[str] = set(flagged_paths or ())
        for command in (("git", "-C", str(root), "diff", "--name-only", "-z", "HEAD"),
                        ("git", "-C", str(root), "ls-files", "--others", "-z")):
            paths.update(name.decode("utf-8") for name in
                         PauseCoordinator._bounded_git_records(command))
            if len(paths) > _MAX_OBSERVED_FILES:
                raise PausePolicyError("worktree file observation exceeds its count bound")
        observed = {}
        remaining = _MAX_OBSERVED_BYTES
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            root_before = os.fstat(root_fd)
            for name in sorted(paths):
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
                    raise PausePolicyError("changed file escapes its worktree")
                observed[name], size = _changed_path_digest(root_fd, name, remaining)
                remaining -= size
            root_after = os.fstat(root_fd)
            if (not _same_file(root_before, root_after)
                    or not _same_file(root_after, root.lstat())):
                raise PausePolicyError("worktree root changed during observation")
        finally:
            os.close(root_fd)
        return observed

    @staticmethod
    def _directory_manifest(root: Path) -> dict[str, str]:
        """Bound a non-Git peer cwd scan; an oversized or unsafe tree is unknown."""
        observed = {}
        remaining = _MAX_OBSERVED_BYTES
        entries = 0
        directory_flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

        def visit(directory_fd: int, prefix: str) -> None:
            nonlocal entries, remaining
            with os.scandir(directory_fd) as children:
                for child in children:
                    before = os.stat(child.name, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISLNK(before.st_mode) or not (
                            stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
                        raise PausePolicyError("peer cwd contains an unsafe file")
                    if child.name == ".git" and stat.S_ISDIR(before.st_mode):
                        continue
                    entries += 1
                    if entries > _MAX_OBSERVED_FILES:
                        raise PausePolicyError("peer cwd file observation exceeds its count bound")
                    relative = f"{prefix}{child.name}"
                    flags = directory_flags if stat.S_ISDIR(before.st_mode) else file_flags
                    fd = os.open(child.name, flags, dir_fd=directory_fd)
                    try:
                        if not _same_file(before, os.fstat(fd)):
                            raise PausePolicyError("peer cwd entry changed during observation")
                        if stat.S_ISDIR(before.st_mode):
                            visit(fd, relative + "/")
                        else:
                            observed[relative], size, after = _digest_fd(fd, remaining)
                            remaining -= size
                            if not _same_file(before, after):
                                raise PausePolicyError("peer cwd file changed during observation")
                        after_path = os.stat(child.name, dir_fd=directory_fd,
                                             follow_symlinks=False)
                        after = os.fstat(fd)
                        if not _same_file(before, after) or not _same_file(after, after_path):
                            raise PausePolicyError("peer cwd entry changed during observation")
                    finally:
                        os.close(fd)

        root_fd = os.open(root, directory_flags)
        try:
            root_before = os.fstat(root_fd)
            visit(root_fd, "")
            root_after = os.fstat(root_fd)
            if (not _same_file(root_before, root_after)
                    or not _same_file(root_after, root.lstat())):
                raise PausePolicyError("peer cwd root changed during observation")
        finally:
            os.close(root_fd)
        return observed

    @staticmethod
    def _directory_modes(root: Path) -> dict[str, str]:
        """Bounded fd-relative directory metadata scan; file contents stay unread."""
        observed: dict[str, str] = {}
        entries = 0
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

        def visit(directory_fd: int, prefix: str, depth: int) -> None:
            nonlocal entries
            before = os.fstat(directory_fd)
            if (not stat.S_ISDIR(before.st_mode) or not before.st_mode & 0o444
                    or not before.st_mode & 0o111):
                raise PausePolicyError("directory observation is unreadable or unsafe")
            observed[prefix] = _directory_identity(before)
            with os.scandir(directory_fd) as children:
                for child in children:
                    child_before = os.stat(child.name, dir_fd=directory_fd,
                                           follow_symlinks=False)
                    if not prefix and child.name == ".git":
                        if stat.S_ISLNK(child_before.st_mode) or not (
                                stat.S_ISDIR(child_before.st_mode)
                                or stat.S_ISREG(child_before.st_mode)):
                            raise PausePolicyError("Git metadata path is unsafe")
                        continue
                    entries += 1
                    if entries > _MAX_DIRECTORY_ENTRIES:
                        raise PausePolicyError("directory observation exceeds its count bound")
                    if stat.S_ISDIR(child_before.st_mode):
                        if depth >= _MAX_DIRECTORY_DEPTH:
                            raise PausePolicyError("directory observation exceeds its depth bound")
                        fd = os.open(child.name, directory_flags, dir_fd=directory_fd)
                        try:
                            if not _same_file(child_before, os.fstat(fd)):
                                raise PausePolicyError("directory moved during observation")
                            visit(fd, prefix + child.name + "/", depth + 1)
                            child_after = os.stat(child.name, dir_fd=directory_fd,
                                                  follow_symlinks=False)
                            if not _same_file(child_before, os.fstat(fd)) or not _same_file(
                                    child_before, child_after):
                                raise PausePolicyError("directory moved during observation")
                        finally:
                            os.close(fd)
                    elif stat.S_ISREG(child_before.st_mode):
                        child_after = os.stat(child.name, dir_fd=directory_fd,
                                              follow_symlinks=False)
                        if not _same_file(child_before, child_after):
                            raise PausePolicyError("directory entry changed during observation")
                    else:
                        raise PausePolicyError("directory contains an unsafe entry")
            if not _same_file(before, os.fstat(directory_fd)):
                raise PausePolicyError("directory changed during observation")

        try:
            root_fd = os.open(root, directory_flags)
            try:
                before = os.fstat(root_fd)
                visit(root_fd, "", 0)
                if (not _same_file(before, os.fstat(root_fd))
                        or not _same_file(before, root.lstat())):
                    raise PausePolicyError("directory root changed during observation")
            finally:
                os.close(root_fd)
        except OSError as exc:
            raise PausePolicyError("directory observation is unavailable") from exc
        return observed

    def _peer_manifest(self, cwd: Path) -> dict[str, Any]:
        probe = subprocess.run(("git", "-C", str(cwd), "rev-parse", "--show-toplevel"),
                               capture_output=True, timeout=5, check=False)
        if probe.returncode != 0:
            for parent in (cwd, *cwd.parents):
                marker = parent / ".git"
                try:
                    mode = marker.lstat().st_mode
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(mode):
                    raise PausePolicyError("Git peer identity is unavailable")
                with os.scandir(marker) as entries:
                    for index, entry in enumerate(entries):
                        if index >= 64 or entry.name in {
                                "HEAD", "config", "objects", "refs", "commondir", "worktrees"}:
                            raise PausePolicyError("Git peer identity is unavailable")
            return {"scan_root": str(cwd), "git": None,
                    "directories": self._directory_modes(cwd),
                    "files": self._directory_manifest(cwd)}
        root = Path(os.fsdecode(probe.stdout.strip())).resolve(strict=True)
        if not cwd.is_relative_to(root) or not root.is_dir() or (root / ".git").is_symlink():
            raise PausePolicyError("Git peer worktree identity is unsafe")
        head_result = subprocess.run(("git", "-C", str(cwd), "rev-parse", "--verify", "HEAD"),
                                     capture_output=True, timeout=5, check=False)
        dir_result = subprocess.run(("git", "-C", str(cwd), "rev-parse", "--absolute-git-dir"),
                                    capture_output=True, timeout=5, check=False)
        if head_result.returncode != 0 or dir_result.returncode != 0:
            raise PausePolicyError("Git peer HEAD or git-dir is unavailable")
        head = os.fsdecode(head_result.stdout.strip())
        git_dir = Path(os.fsdecode(dir_result.stdout.strip()))
        resolved_git_dir = git_dir.resolve(strict=True)
        if (len(head) != 40 or any(ch not in "0123456789abcdef" for ch in head)
                or git_dir != resolved_git_dir or not git_dir.is_dir()):
            raise PausePolicyError("Git peer identity is invalid")
        root_stat, git_dir_stat = root.stat(), git_dir.stat()
        identity = {"root": str(root), "head": head, "git_dir": str(git_dir),
                    "root_device": root_stat.st_dev, "root_inode": root_stat.st_ino,
                    "git_dir_device": git_dir_stat.st_dev, "git_dir_inode": git_dir_stat.st_ino}
        flags = self._index_flags(root)
        return {"scan_root": str(root), "git": identity, "index_flags": flags,
                "directories": self._directory_modes(root),
                "tracked_modes": self._tracked_modes(root, flags),
                "files": self._changed_files(root, flags)}

    def _peer_file_state(self, binding: PauseBinding | None) -> dict[str, dict[str, Any]]:
        if not isinstance(self._bridge, G3BridgeServer):
            raise PausePolicyError("process-backed OMP peer observations are unavailable")
        observed = {}
        for role in (ActorRole.MANAGER, ActorRole.WORKER):
            peer = self._bridge.peer(role)
            if type(peer.pid) is not int or peer.pid < 1:
                raise PausePolicyError("OMP peer PID is unknown")
            if binding is not None:
                expected = (binding.manager_session_id, binding.manager_generation) \
                    if role is ActorRole.MANAGER else \
                    (binding.worker_session_id, binding.worker_generation)
                if (peer.session_id, peer.generation) != expected:
                    raise PausePolicyError("OMP peer changed since pause")
            os.kill(peer.pid, 0)
            reported = Path(os.readlink(f"/proc/{peer.pid}/cwd"))
            cwd = reported.resolve(strict=True)
            if reported != cwd or not cwd.is_dir():
                raise PausePolicyError("OMP peer cwd is unavailable")
            first = self._peer_manifest(cwd)
            second = self._peer_manifest(cwd)
            if first != second:
                raise PausePolicyError("OMP peer files changed during observation")
            if Path(os.readlink(f"/proc/{peer.pid}/cwd")).resolve() != cwd:
                raise PausePolicyError("OMP peer cwd changed during observation")
            observed[role.value] = {"pid": peer.pid, "cwd": str(cwd), **second}
        return observed

    def _peer_files_stable(self) -> bool:
        if not isinstance(self._bridge, G3BridgeServer):
            return True  # In-memory policy fakes have no process-backed peers.
        try:
            self._peer_file_state(None)
        except PausePolicyError as exc:
            if "changed during observation" in str(exc):
                return False
            raise
        return True

    @staticmethod
    def _file_baseline_path(run: object) -> Path:
        return Path(run.record_dir) / "pause-file-baseline.json"

    @staticmethod
    def _entry_baseline_path(run: object) -> Path:
        return Path(run.record_dir) / "entry-file-baseline.json"

    @staticmethod
    def _read_file_baseline(path: Path) -> dict[str, Any]:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PausePolicyError("peer file baseline is not regular")
            raw = os.read(fd, 1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise PausePolicyError("peer file baseline exceeds its byte bound")
        finally:
            os.close(fd)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise PausePolicyError("peer file baseline is invalid")
        return value

    @staticmethod
    def _baseline_matches(value: Mapping[str, Any], binding: PauseBinding) -> bool:
        return (value.get("version") == 4
                and (value.get("task_id"), value.get("revision"), value.get("run_id"))
                == (binding.task_id, binding.revision, binding.run_id)
                and (value.get("approval_hash"), value.get("approved_scope_hash"))
                == (binding.approval_hash, binding.approved_scope_hash)
                and isinstance(value.get("peers"), dict)
                and set(value["peers"]) == {"manager", "worker"}
                and isinstance(value.get("execution_index_flags"), dict)
                and isinstance(value.get("execution_flagged_files"), dict)
                and isinstance(value.get("execution_tracked_modes"), dict)
                and isinstance(value.get("execution_directory_modes"), dict)
                and set(value["execution_index_flags"])
                == set(value["execution_flagged_files"]))

    @staticmethod
    def _write_file_baseline(path: Path, value: Mapping[str, Any], *, exclusive: bool = False) -> None:
        temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
        payload = _canonical(value) + b"\n"
        if len(payload) > 1024 * 1024:
            raise PausePolicyError("peer file baseline exceeds its byte bound")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if exclusive:
                os.link(temporary, path, follow_symlinks=False)
            else:
                os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _ensure_entry_file_baseline(self, run: object, binding: PauseBinding) -> None:
        if not isinstance(self._bridge, G3BridgeServer):
            return
        path = self._entry_baseline_path(run)
        events = self._journal.read() if self._journal is not None else ()
        recorded = any(event.kind == "entry_baseline_recorded" for event in events)
        if path.exists() or path.is_symlink():
            value = self._read_file_baseline(path)
            if not self._baseline_matches(value, binding):
                raise PausePolicyError("entry peer file baseline does not match approval")
        else:
            if events:
                raise PausePolicyError("entry peer file baseline is missing for an existing run")
            execution_root = Path(run.worktree.path).resolve(strict=True)
            execution_flags = self._index_flags(execution_root)
            execution_directories = self._directory_modes(execution_root)
            execution_modes = self._tracked_modes(execution_root, execution_flags)
            execution_files = self._changed_files(execution_root, execution_flags)
            if (execution_flags != self._index_flags(execution_root)
                    or execution_directories != self._directory_modes(execution_root)
                    or execution_modes != self._tracked_modes(execution_root,
                                                              execution_flags)):
                raise PausePolicyError("execution paths changed during baseline")
            self._write_file_baseline(path, {
                "version": 4, "task_id": binding.task_id, "revision": binding.revision,
                "run_id": binding.run_id, "approval_hash": binding.approval_hash,
                "approved_scope_hash": binding.approved_scope_hash,
                "execution_index_flags": execution_flags,
                "execution_tracked_modes": execution_modes,
                "execution_directory_modes": execution_directories,
                "execution_flagged_files": {
                    name: execution_files[name] for name in execution_flags},
                "peers": self._peer_file_state(binding),
            }, exclusive=True)
        if not recorded:
            if self._journal is None:
                raise PausePolicyError("entry peer file baseline journal is unavailable")
            self._journal.append(pause_id=binding.run_id, kind="entry_baseline_recorded",
                                 occurred_at=self._clock(), binding=binding)

    def _persist_peer_file_baseline(self, run: object, binding: PauseBinding,
                                    pause_id: str) -> None:
        if not isinstance(self._bridge, G3BridgeServer):
            return
        entry = self._read_file_baseline(self._entry_baseline_path(run))
        if not self._baseline_matches(entry, binding):
            raise PausePolicyError("entry peer file baseline does not match approval")
        self._write_file_baseline(self._file_baseline_path(run),
                                  {**entry, "pause_id": pause_id})

    def _peer_files_reconciled(self, run: object, binding: PauseBinding,
                               execution_flags: Mapping[str, str] | None = None,
                               execution_files: Mapping[str, str] | None = None,
                               execution_modes: Mapping[str, str] | None = None,
                               execution_directories: Mapping[str, str] | None = None) -> bool:
        if not isinstance(self._bridge, G3BridgeServer):
            return True  # In-memory policy tests exercise the public port separately.
        baseline = self._read_file_baseline(self._file_baseline_path(run))
        entry = self._read_file_baseline(self._entry_baseline_path(run))
        if (not self._baseline_matches(baseline, binding)
                or not self._baseline_matches(entry, binding)
                or baseline.get("pause_id") != self._pause_id
                or baseline.get("peers") != entry.get("peers")
                or baseline.get("execution_index_flags") != entry.get("execution_index_flags")
                or baseline.get("execution_tracked_modes") != entry.get("execution_tracked_modes")
                or baseline.get("execution_directory_modes") != entry.get("execution_directory_modes")
                or baseline.get("execution_flagged_files") != entry.get("execution_flagged_files")):
            return False
        execution_root = Path(run.worktree.path).resolve(strict=True)
        current_flags = (dict(execution_flags) if execution_flags is not None
                         else self._index_flags(execution_root))
        if current_flags != baseline["execution_index_flags"]:
            return False
        current_files = (dict(execution_files) if execution_files is not None
                         else self._changed_files(execution_root, current_flags))
        if any(current_files.get(name) != digest for name, digest in
               baseline["execution_flagged_files"].items()):
            return False
        current_modes = (dict(execution_modes) if execution_modes is not None
                         else self._tracked_modes(execution_root, current_flags))
        prior_modes = baseline["execution_tracked_modes"]
        for name in prior_modes.keys() | current_modes.keys():
            if (prior_modes.get(name) != current_modes.get(name)
                    and not _approved_relative_path(name, binding.approved_paths)):
                return False
        current_directories = (dict(execution_directories)
                               if execution_directories is not None
                               else self._directory_modes(execution_root))
        prior_directories = baseline["execution_directory_modes"]
        if any(prior_directories.get(name) != current_directories.get(name)
               and not _approved_relative_path(name, binding.approved_paths)
               for name in prior_directories.keys() | current_directories.keys()):
            return False
        previous = baseline.get("peers")
        current = self._peer_file_state(binding)
        if not isinstance(previous, dict) or set(previous) != set(current):
            return False
        worktree = Path(run.worktree.path).resolve()
        for role, now in current.items():
            prior = previous[role]
            if (not isinstance(prior, dict) or prior.get("pid") != now["pid"]
                    or prior.get("cwd") != now["cwd"]
                    or prior.get("scan_root") != now["scan_root"]
                    or prior.get("git") != now["git"]
                    or prior.get("index_flags") != now.get("index_flags")
                    or not isinstance(prior.get("directories"), dict)
                    or (now.get("git") is not None
                        and not isinstance(prior.get("tracked_modes"), dict))
                    or not isinstance(prior.get("files"), dict)):
                return False
            before_files, now_files = prior["files"], now["files"]
            before_modes, now_modes = (prior.get("tracked_modes") or {},
                                       now.get("tracked_modes") or {})
            before_directories, now_directories = (prior["directories"],
                                                   now["directories"])
            if any(before_files.get(name) != now_files.get(name)
                   for name in (prior.get("index_flags") or {})):
                return False
            for name in (before_files.keys() | now_files.keys() |
                         before_modes.keys() | now_modes.keys() |
                         before_directories.keys() | now_directories.keys()):
                if (before_files.get(name) == now_files.get(name)
                        and before_modes.get(name) == now_modes.get(name)
                        and before_directories.get(name) == now_directories.get(name)):
                    continue
                if not isinstance(name, str):
                    return False
                if name == "":
                    return False
                relative_name = Path(name)
                if relative_name.is_absolute() or ".." in relative_name.parts:
                    return False
                path = (Path(now["scan_root"]) / relative_name).resolve(strict=False)
                if not path.is_relative_to(worktree):
                    return False
                relative = path.relative_to(worktree).as_posix()
                if not _approved_relative_path(relative, binding.approved_paths):
                    return False
        return True

    @staticmethod
    def _process_matches(run: object) -> bool:
        first = run.shell.snapshot()
        second = run.shell.snapshot()
        if first != second or not isinstance(first, Mapping):
            return False
        pid = first.get("parent_pid")
        lifecycle = first.get("lifecycle")
        if type(pid) is not int or pid < 1 or not isinstance(lifecycle, Mapping):
            return False
        try:
            configured_root = Path(run.worktree.path)
            root = configured_root.resolve(strict=True)
            if configured_root != root or not root.is_dir():
                return False
            processes = [pid]
            child = lifecycle.get("child_pid")
            if lifecycle.get("lifetime") != "ended" and type(child) is int and child > 0:
                processes.append(child)
            for index, process in enumerate(processes):
                os.kill(process, 0)
                reported = Path(os.readlink(f"/proc/{process}/cwd"))
                cwd = reported.resolve(strict=True)
                if (reported != cwd or not cwd.is_dir()
                        or (cwd != root if index == 0 else
                            cwd != root and not cwd.is_relative_to(root))):
                    return False
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _worktree_head_matches(run: object, root: Path) -> bool:
        commit = getattr(run.worktree, "commit", None)
        if not isinstance(commit, str) or len(commit) != 40:
            return False
        result = subprocess.run(("git", "-C", str(root), "rev-parse", "--verify", "HEAD"),
                                capture_output=True, timeout=5, check=False)
        return result.returncode == 0 and result.stdout.strip() == commit.encode("ascii")

    def _observed_resume_evidence(self, run: object, binding: PauseBinding,
                                  intent: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(intent["payload"])
        root = Path(run.worktree.path).resolve()
        try:
            first_flags = self._index_flags(root)
            first_directories = self._directory_modes(root)
            first_modes = self._tracked_modes(root, first_flags)
            first = self._changed_files(root, first_flags)
            second_flags = self._index_flags(root)
            second_directories = self._directory_modes(root)
            second_modes = self._tracked_modes(root, second_flags)
            second = self._changed_files(root, second_flags)
            files_match = (first_flags == second_flags
                           and first_directories == second_directories
                           and first_modes == second_modes
                           and self._worktree_head_matches(run, root)
                           and first == second and all(
                any(name == approved.rstrip("/") or
                    approved.endswith("/") and name.startswith(approved)
                    for approved in binding.approved_paths)
                for name in second if name not in second_flags
            ) and self._peer_files_reconciled(run, binding, second_flags, second,
                                             second_modes, second_directories))
            processes_match = self._process_matches(run)
            states = [_role_state(self._bridge, role, min(self._request_timeout, 2.0))
                      for role in (ActorRole.MANAGER, ActorRole.WORKER)]
            known = set(self._unknown_tool_call_ids)
            tools_match = all(
                state.get("paused") is True and state.get("inFlightToolCount") == 0
                and isinstance(state.get("unknownOutcomeToolCallIds"), list)
                and isinstance(state.get("unconfirmedToolCallIds"), list)
                and set(state["unknownOutcomeToolCallIds"]) <= known
                and set(state["unconfirmedToolCallIds"]) <= known
                for state in states
            )
            active = run.repository.get_current_run(binding.task_id)
            task_match = (isinstance(active, Mapping)
                          and (active.get("task_id"), active.get("revision"), active.get("run_id"))
                          == (binding.task_id, binding.revision, binding.run_id))
            approval_match = (binding.approval_hash == run.approval_hash
                              and self._binding is not None
                              and self._same_scope(self._binding, binding))
        except Exception:
            files_match = processes_match = tools_match = task_match = approval_match = False
        payload.update(filesMatch=files_match, processesMatch=processes_match,
                       toolsMatch=tools_match, taskMatch=task_match,
                       approvalMatch=approval_match, checkedAt=self._clock())
        return parse_port({"portVersion": 2, "kind": "ResumeEvidence", "payload": payload})

    def resume(self, run: object, evidence: object) -> PauseStatus:
        """Reconcile current facts and user intent; held bridge requests stay consumed."""
        resume_token = self._authority_fence.token()
        with self._transition("resume"):
            return self._resume(run, evidence, resume_token)

    def resume_finished(self, run: object) -> PauseStatus:
        """CW-16 fix-02 (P2-2): the user's reconciled resume of a bound run whose host command ended while
        paused (the caller observed the confirmed exit). The pause is lifted through the same transition as
        ``resume`` (both OMPs, journal ``resumed``/``finished_while_paused``), so the run stays bound and a later
        pause starts a new pause of both OMPs."""
        resume_token = self._authority_fence.token()
        with self._transition("resume"):
            return self._resume(run, None, resume_token, validate=self._validate_finished_resume,
                                resumed_status="finished_while_paused")

    def resume_observed(self, run: object, *, user_resume: bool) -> PauseStatus:
        """Production entry point: derive reconciliation from a live WorkflowRun."""
        if not hasattr(run, "worktree") or not hasattr(run, "shell"):
            raise PausePolicyError("live worktree and shell observations are required")
        evidence = {"portVersion": 2, "kind": "ResumeEvidence", "payload": {
            "userResume": user_resume, "taskId": run.task_id, "runId": run.run_id,
            "approvalHash": run.approval_hash, "checkedAt": self._clock(),
            "filesMatch": False, "processesMatch": False, "toolsMatch": False,
            "taskMatch": False, "approvalMatch": False, "unknowns": [],
        }}
        return self.resume(run, evidence)

    @staticmethod
    def _resumed_peer_ready(state: Mapping[str, Any], binding: PauseBinding,
                            role: ActorRole) -> bool:
        identity = ((binding.manager_session_id, binding.manager_generation)
                    if role is ActorRole.MANAGER else
                    (binding.worker_session_id, binding.worker_generation))
        return (isinstance(identity[0], str) and bool(identity[0])
                and type(identity[1]) is int and identity[1] >= 1
                and state.get("role") == role.value
                and state.get("sessionId") == identity[0]
                and type(state.get("generation")) is int
                and state["generation"] == identity[1]
                and state.get("paused") is False and state.get("idle") is True
                and state.get("pending") is False
                and state.get("approvalPending") is False
                and state.get("editorKnown") is True
                and state.get("editorEmpty") is True
                and type(state.get("inFlightToolCount")) is int
                and state["inFlightToolCount"] == 0
                and state.get("unresolvedPriorSessions") in (None, []))

    def _post_resume_ready(self, binding: PauseBinding) -> bool:
        for role in (ActorRole.MANAGER, ActorRole.WORKER):
            try:
                state = _role_state(self._bridge, role, min(self._request_timeout, 2.0))
            except Exception:
                return False
            if not self._resumed_peer_ready(state, binding, role):
                return False
        return True

    def _resume_failed(self, run: object, binding: PauseBinding,
                       pause_id: str, reason: str,
                       token: object | None = None) -> PauseStatus:
        if token is not None:
            self._authority_fence.close_if_binding(token, "resume_failed",
                                                   exact_generation=True)
        with self._lock:
            if not self._transition_current(run, binding, pause_id):
                raise PausePolicyError("resume run changed during transition")
            self._pause_override = True
            self._dispatch_fenced = True
            self._state_version += 1
        self._record("resume_failed", status=reason)
        self._update_admission()
        for role in (ActorRole.MANAGER, ActorRole.WORKER):
            try:
                self._bound_request(role, {"kind": "pause"}, binding,
                                    self._authority_fence.token())
            except Exception:
                pass
        return self.status()

    def _resume(self, run: object, evidence: object,
                resume_token: object, *, validate: Callable[[object, object], Any] | None = None,
                resumed_status: str = "reconciled") -> PauseStatus:
        self._refresh_base_state()
        self._sync_repository_cancellation()
        with self._lock:
            if self._pause_override is not True or self._cancelled:
                raise PausePolicyError("only an uncancelled paused run can be resumed")
        (validate or self._validate_resume)(run, evidence)
        with self._lock:
            binding, pause_id = self._binding, self._pause_id
            if (binding is None or pause_id is None or self._run is not run
                    or self._cancelled or self._pause_override is not True):
                raise PausePolicyError("resume run changed during validation")
            self._dispatch_fenced = True
        self._record("resume_requested")
        with self._lock:
            if self._persistence_error is not None:
                raise PauseJournalError("resume request could not be durably recorded")
        if not self._bound_authority_current(binding, resume_token):
            return self._resume_failed(run, binding, pause_id, "active_run_changed", resume_token)
        results: list[str] = []
        for role in (ActorRole.MANAGER, ActorRole.WORKER):
            try:
                response = self._bound_request(role, {"kind": "resume", "reconciled": True},
                                               binding, resume_token, pair=True)
                ack = response if isinstance(response, Mapping) else None
                status = self._ack_status(ack, frozenset({"resumed"}))
                if (status == "resumed" and (not isinstance(ack.get("requestId"), str)
                                             or not ack["requestId"]
                                             or not isinstance(ack.get("state"), Mapping)
                                             or not self._resumed_peer_ready(ack["state"], binding, role))):
                    status = "unknown"
            except Exception:
                ack, status = None, "unknown"
            results.append(status)
            with self._lock:
                if not self._transition_current(run, binding, pause_id):
                    raise PausePolicyError("resume run changed during acknowledgement")
            self._record("manager_resumed" if role is ActorRole.MANAGER else "worker_resumed",
                         request_id=None if ack is None else ack.get("requestId"), status=status)
        if any(status != "resumed" for status in results):
            return self._resume_failed(run, binding, pause_id, "unknown", resume_token)
        if not self._post_resume_ready(binding):
            return self._resume_failed(run, binding, pause_id, "peer_state_unavailable", resume_token)
        with self._lock:
            authority_changed = (not self._transition_current(run, binding, pause_id)
                                 or self._cancelled or self._cancel_fence_pending
                                 or self._persistence_error is not None)
        if not authority_changed:
            self._record("resumed", status=resumed_status)
        with self._lock:
            authority_changed = (authority_changed or not self._transition_current(run, binding, pause_id)
                                 or self._cancelled or self._cancel_fence_pending)
            durable = not authority_changed and self._persistence_error is None
            if durable:
                self._pause_override = False
                self._state_version += 1
                version = self._state_version
                unpaused_state = self._state_snapshot(paused=False, include_pending=False)
        if not durable:
            return self._resume_failed(run, binding, pause_id,
                                       "authority_changed" if authority_changed else "journal_unavailable",
                                       resume_token)
        if not self._publish_admission_state(unpaused_state, fail_closed=False,
                                             expected_version=version):
            return self._resume_failed(run, binding, pause_id, "admission_state_unavailable", resume_token)
        if not self._post_resume_ready(binding):
            return self._resume_failed(run, binding, pause_id, "peer_state_unavailable", resume_token)
        with self._lock:
            authority_changed = (not self._transition_current(run, binding, pause_id)
                                 or self._cancelled or self._cancel_fence_pending
                                 or self._persistence_error is not None
                                 or version != self._state_version)
            if not authority_changed:
                self._dispatch_fenced = False
        if authority_changed:
            return self._resume_failed(run, binding, pause_id, "authority_changed", resume_token)
        if (not self._bound_authority_current(binding, resume_token)
                or not self._peer_identity_current(binding)
                or self._authority_fence.open(resume_token) is None):
            return self._resume_failed(run, binding, pause_id, "peer_state_unavailable", resume_token)
        return self.status()

    def status(self) -> PauseStatus:
        self._refresh_base_state()
        if not getattr(self._dispatch_action, "active", False):
            self._sync_repository_cancellation()
        with self._lock:
            payload = self._state_snapshot(include_pending=False)["payload"]
            return PauseStatus(
                binding=self._binding, paused=payload["paused"], cancelled=payload["cancelled"],
                pause_id=self._pause_id, requested_at=self._requested_at,
                manager_ack_status=self._manager_ack_status,
                worker_ack_status=self._worker_ack_status, abort_status=self._abort_status,
                stop_observed_at=self._stop_observed_at,
                unknown_tool_call_ids=self._unknown_tool_call_ids,
                last_checked_at=self._last_checked_at, persistence_error=self._persistence_error,
                manager_unknown_tool_call_ids=self._manager_unknown_tool_call_ids,
                worker_unknown_tool_call_ids=self._worker_unknown_tool_call_ids,
            )
