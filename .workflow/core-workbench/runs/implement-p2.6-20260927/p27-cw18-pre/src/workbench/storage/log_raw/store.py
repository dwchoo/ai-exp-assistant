"""Quota-limited, append-only raw logs with fail-closed path handling."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
from threading import RLock
from typing import Iterator


DEFAULT_PER_RUN_LIMIT = 64 * 1024 * 1024
DEFAULT_PROJECT_LIMIT = 512 * 1024 * 1024
_MARKER = ".workbench-log-store"
_LOCK = ".quota.lock"
_STATUS = ".status"
_MARKER_HEADER = b"workbench-raw-log-store-v1"
_STATUS_VERSION = 1
_MAX_STATUS_BYTES = 4096
_ERROR_LIMIT = 240


class StoreIntegrityError(RuntimeError):
    """The owned raw-log tree is unsafe or its quota state is inconsistent."""


@dataclass(frozen=True, slots=True)
class RawLogStatus:
    """Immutable per-run accounting; raw content is never included."""

    run_id: str
    stored_bytes: int = 0
    observed_bytes: int = 0
    dropped_bytes: int = 0
    truncated: bool = False
    missing_bytes: int = 0
    cap_source: str | None = None
    first_observed_at: str | None = None
    last_observed_at: str | None = None
    last_persisted_at: str | None = None
    storage_error: str | None = None

    @property
    def experiment_continues(self) -> bool:
        """Raw-log status is observational and never requests experiment stop."""
        return True

    @property
    def observation_continues(self) -> bool:
        """Callers may continue lifecycle/display observations after a log fault."""
        return True


class _PartialWriteError(OSError):
    def __init__(self, written: int, cause: OSError):
        super().__init__(cause.errno, cause.strerror or "raw log write failed")
        self.written = written


@dataclass(frozen=True, slots=True)
class _StoredStatus:
    snapshot: RawLogStatus
    raw_identity: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class _PendingFailure:
    """Only this store's failed, unstored observations since a known raw base."""

    base: _StoredStatus | None
    bytes: int
    events: int
    first_observed_at: str
    last_observed_at: str
    storage_error: str
    applied_visible: _StoredStatus | None = None


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
    else:
        raise TypeError("observed_at must be datetime, ISO-8601 string, or None")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _valid_run_id(run_id: object) -> str:
    if (not isinstance(run_id, str) or not run_id or run_id != run_id.strip()
            or run_id in {".", ".."} or "/" in run_id or "\\" in run_id
            or "\x00" in run_id or any(ord(ch) < 32 for ch in run_id)):
        raise ValueError("run_id must be a non-empty path-safe identifier")
    if len(run_id.encode("utf-8")) > 160 or run_id.startswith("."):
        raise ValueError("run_id is outside the supported identifier bounds")
    return run_id


def _validate_limit(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _error_name(error: BaseException) -> str:
    name = type(error).__name__
    number = getattr(error, "errno", None)
    detail = f"{name}:{number}" if isinstance(number, int) else name
    return detail[:_ERROR_LIMIT]


def _open_directory_path(path: str, *, create: bool) -> int:
    """Open every path component with O_NOFOLLOW and return the final dirfd."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in path.split("/")[1:]:
            if not component:
                continue
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _private_directory(info: os.stat_result, *, name: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise StoreIntegrityError(f"{name} is not a directory")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise StoreIntegrityError(f"{name} must be owned by this user and private")


def _private_regular(info: os.stat_result, *, name: str) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise StoreIntegrityError(f"{name} must be an owned single-link regular file")


def _write_all(descriptor: int, payload: bytes) -> int:
    written = 0
    view = memoryview(payload)
    while written < len(view):
        try:
            count = os.write(descriptor, view[written:])
        except OSError as exc:
            raise _PartialWriteError(written, exc) from exc
        if count <= 0:
            cause = OSError(errno.EIO, "write returned no progress")
            raise _PartialWriteError(written, cause) from cause
        written += count
    return written


class RawLogStore:
    """One project-owned raw-log root with durable run and project quotas.

    The root is dedicated to raw logs and must either be empty or already carry
    this store's marker. Metadata, TaskSpec, summary, worktree and experiment
    result paths stay outside this root and are never scanned or removed.
    """

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        per_run_limit: int = DEFAULT_PER_RUN_LIMIT,
        project_limit: int = DEFAULT_PROJECT_LIMIT,
    ):
        self.per_run_limit = _validate_limit(per_run_limit, name="per_run_limit")
        self.project_limit = _validate_limit(project_limit, name="project_limit")
        raw_path = os.fspath(project_root)
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("project_root must be a non-empty path")
        if ".." in Path(raw_path).parts:
            raise ValueError("project_root must not contain parent traversal")
        self.root_path = os.path.abspath(raw_path)
        if self.root_path == "/":
            raise ValueError("project_root must be a dedicated directory")
        self._thread_lock = RLock()
        self._pending: dict[str, _PendingFailure] = {}
        self._known_records: dict[str, _StoredStatus | None] = {}
        self._unconfirmed: dict[str, RawLogStatus] = {}
        self._root_fd: int | None = None
        self._status_fd: int | None = None
        self._lock_identity: tuple[int, int] | None = None
        self._status_identity: tuple[int, int] | None = None
        try:
            self._root_fd = _open_directory_path(self.root_path, create=True)
            _private_directory(os.fstat(self._root_fd), name="project raw-log root")
            self._initialize_root()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> RawLogStore:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._status_fd is not None:
            os.close(self._status_fd)
            self._status_fd = None
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def _ensure_open(self) -> tuple[int, int]:
        if self._root_fd is None or self._status_fd is None:
            raise RuntimeError("RawLogStore is closed")
        return self._root_fd, self._status_fd

    def _initialize_root(self) -> None:
        root_fd = self._root_fd
        if root_fd is None:
            raise RuntimeError("RawLogStore is closed")
        initial_names = set(os.listdir(root_fd))
        if _MARKER not in initial_names and initial_names and _LOCK not in initial_names:
            raise StoreIntegrityError("unmarked raw-log root is not empty")
        created_lock = False
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_EXCL
        try:
            lock_fd = os.open(_LOCK, flags, 0o600, dir_fd=root_fd)
            created_lock = True
        except FileExistsError:
            lock_named = os.stat(_LOCK, dir_fd=root_fd, follow_symlinks=False)
            _private_regular(lock_named, name="quota lock")
            lock_fd = os.open(
                _LOCK,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root_fd,
            )
        try:
            _private_regular(os.fstat(lock_fd), name="quota lock")
            self._lock_identity = _identity(os.fstat(lock_fd))
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._verify_named_file(root_fd, _LOCK, lock_fd)
            names = set(os.listdir(root_fd))
            marker_exists = _MARKER in names
            if not marker_exists:
                if not created_lock or names != {_LOCK}:
                    raise StoreIntegrityError("unmarked raw-log root is not empty")
                status_fd = self._create_status_directory(root_fd)
                self._status_fd = status_fd
                self._status_identity = _identity(os.fstat(status_fd))
                self._create_marker(root_fd)
            else:
                status_fd = self._open_status_directory(root_fd)
                self._status_fd = status_fd
                self._status_identity = _identity(os.fstat(status_fd))
                self._verify_marker(root_fd)
            self._verify_root_and_status()
            self._scan_project()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _create_status_directory(self, root_fd: int) -> int:
        os.mkdir(_STATUS, 0o700, dir_fd=root_fd)
        return self._open_status_directory(root_fd)

    def _open_status_directory(self, root_fd: int) -> int:
        descriptor = os.open(
            _STATUS,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            _private_directory(os.fstat(descriptor), name="status directory")
            named = os.stat(_STATUS, dir_fd=root_fd, follow_symlinks=False)
            if _identity(named) != _identity(os.fstat(descriptor)):
                raise StoreIntegrityError("status directory was replaced")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _create_marker(self, root_fd: int) -> None:
        if self._lock_identity is None or self._status_identity is None:
            raise RuntimeError("store lock/status identity is unavailable")
        content = self._marker_content(
            self._lock_identity, self._status_identity,
            self.per_run_limit, self.project_limit,
        )
        descriptor = os.open(
            _MARKER,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        try:
            _private_regular(os.fstat(descriptor), name="store marker")
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.fsync(root_fd)
        finally:
            os.close(descriptor)

    def _verify_marker(self, root_fd: int) -> None:
        if self._lock_identity is None or self._status_identity is None:
            raise StoreIntegrityError("store identities are unavailable")
        named = os.stat(_MARKER, dir_fd=root_fd, follow_symlinks=False)
        _private_regular(named, name="store marker")
        descriptor = os.open(
            _MARKER,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=root_fd,
        )
        try:
            info = os.fstat(descriptor)
            _private_regular(info, name="store marker")
            self._verify_named_file(root_fd, _MARKER, descriptor)
            expected = self._marker_content(
                self._lock_identity, self._status_identity,
                self.per_run_limit, self.project_limit,
            )
            if info.st_size > 128 or os.read(descriptor, 129) != expected:
                raise StoreIntegrityError("store marker is corrupt")
        finally:
            os.close(descriptor)

    @staticmethod
    def _marker_content(
        lock_identity: tuple[int, int],
        status_identity: tuple[int, int],
        per_run_limit: int,
        project_limit: int,
    ) -> bytes:
        return (
            _MARKER_HEADER + b"\n"
            + f"{lock_identity[0]}:{lock_identity[1]}\n".encode("ascii")
            + f"{status_identity[0]}:{status_identity[1]}\n".encode("ascii")
            + f"{per_run_limit}:{project_limit}\n".encode("ascii")
        )

    def _verify_named_file(self, parent_fd: int, name: str, descriptor: int) -> None:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
        _private_regular(named, name=name)
        _private_regular(opened, name=name)
        if _identity(named) != _identity(opened):
            raise StoreIntegrityError(f"{name} was replaced")

    def _verify_root_and_status(self) -> None:
        root_fd, status_fd = self._ensure_open()
        current_fd = _open_directory_path(self.root_path, create=False)
        try:
            if _identity(os.fstat(current_fd)) != _identity(os.fstat(root_fd)):
                raise StoreIntegrityError("project raw-log root was replaced")
        finally:
            os.close(current_fd)
        _private_directory(os.fstat(root_fd), name="project raw-log root")
        _private_directory(os.fstat(status_fd), name="status directory")
        if self._lock_identity is None or self._status_identity is None:
            raise StoreIntegrityError("store identities are unavailable")
        named_lock = os.stat(_LOCK, dir_fd=root_fd, follow_symlinks=False)
        if _identity(named_lock) != self._lock_identity:
            raise StoreIntegrityError("quota lock was replaced")
        named_status = os.stat(_STATUS, dir_fd=root_fd, follow_symlinks=False)
        if (_identity(named_status) != _identity(os.fstat(status_fd))
                or _identity(named_status) != self._status_identity):
            raise StoreIntegrityError("status directory was replaced")

    @contextmanager
    def _project_lock(self) -> Iterator[None]:
        root_fd, _ = self._ensure_open()
        descriptor = os.open(
            _LOCK,
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=root_fd,
        )
        try:
            _private_regular(os.fstat(descriptor), name="quota lock")
            if self._lock_identity is not None and _identity(os.fstat(descriptor)) != self._lock_identity:
                raise StoreIntegrityError("quota lock was replaced")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._verify_named_file(root_fd, _LOCK, descriptor)
            self._verify_root_and_status()
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _read_status(self, run_id: str) -> _StoredStatus | None:
        _, status_fd = self._ensure_open()
        filename = f"{run_id}.json"
        try:
            named = os.stat(filename, dir_fd=status_fd, follow_symlinks=False)
            _private_regular(named, name=f"status for {run_id}")
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=status_fd,
            )
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            _private_regular(info, name=f"status for {run_id}")
            named = os.stat(filename, dir_fd=status_fd, follow_symlinks=False)
            if _identity(named) != _identity(info):
                raise StoreIntegrityError(f"status for {run_id} was replaced")
            if info.st_size > _MAX_STATUS_BYTES:
                raise StoreIntegrityError(f"status for {run_id} exceeds its bound")
            payload = os.read(descriptor, _MAX_STATUS_BYTES + 1)
            if len(payload) > _MAX_STATUS_BYTES:
                raise StoreIntegrityError(f"status for {run_id} exceeds its bound")
        finally:
            os.close(descriptor)
        try:
            decoded = json.loads(payload)
            return self._decode_status(run_id, decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise StoreIntegrityError(f"status for {run_id} is corrupt") from exc

    @staticmethod
    def _decode_status(run_id: str, value: object) -> _StoredStatus:
        fields = {"version", "run_id", "status", "raw_identity"}
        if not isinstance(value, dict) or set(value) != fields or value["version"] != _STATUS_VERSION:
            raise ValueError("unsupported raw-log status")
        if value["run_id"] != run_id:
            raise ValueError("run id mismatch")
        status_value = value["status"]
        status_fields = {
            "run_id", "stored_bytes", "observed_bytes", "dropped_bytes", "truncated",
            "missing_bytes", "cap_source", "first_observed_at", "last_observed_at",
            "last_persisted_at", "storage_error",
        }
        if not isinstance(status_value, dict) or set(status_value) != status_fields:
            raise ValueError("invalid status snapshot")
        if status_value["run_id"] != run_id:
            raise ValueError("status run id mismatch")
        for key in ("stored_bytes", "observed_bytes", "dropped_bytes", "missing_bytes"):
            if isinstance(status_value[key], bool) or not isinstance(status_value[key], int) or status_value[key] < 0:
                raise ValueError("invalid byte count")
        if status_value["observed_bytes"] != status_value["stored_bytes"] + status_value["dropped_bytes"]:
            raise ValueError("byte accounting mismatch")
        if status_value["missing_bytes"] < status_value["dropped_bytes"]:
            raise ValueError("missing bytes cannot be less than dropped bytes")
        if not isinstance(status_value["truncated"], bool):
            raise ValueError("invalid truncation state")
        if status_value["truncated"] != (status_value["missing_bytes"] > 0):
            raise ValueError("truncation state mismatch")
        if status_value["cap_source"] not in {None, "run", "project", "run+project"}:
            raise ValueError("invalid cap source")
        for key in ("first_observed_at", "last_observed_at", "last_persisted_at"):
            stamp = status_value[key]
            if stamp is not None:
                if not isinstance(stamp, str):
                    raise ValueError("invalid timestamp")
                _timestamp(stamp)
        error = status_value["storage_error"]
        if error is not None and (not isinstance(error, str) or len(error) > _ERROR_LIMIT):
            raise ValueError("invalid storage error")
        if (status_value["first_observed_at"] is None) != (status_value["last_observed_at"] is None):
            raise ValueError("observation timestamp mismatch")
        identity = value["raw_identity"]
        if identity is not None:
            if (not isinstance(identity, list) or len(identity) != 2
                    or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in identity)):
                raise ValueError("invalid raw-log identity")
            raw_identity = (identity[0], identity[1])
        else:
            raw_identity = None
        status = RawLogStatus(
            run_id=run_id,
            stored_bytes=status_value["stored_bytes"],
            observed_bytes=status_value["observed_bytes"],
            dropped_bytes=status_value["dropped_bytes"],
            truncated=status_value["truncated"],
            missing_bytes=status_value["missing_bytes"],
            cap_source=status_value["cap_source"],
            first_observed_at=status_value["first_observed_at"],
            last_observed_at=status_value["last_observed_at"],
            last_persisted_at=status_value["last_persisted_at"],
            storage_error=error,
        )
        return _StoredStatus(status, raw_identity)

    def _scan_project(self) -> tuple[dict[str, _StoredStatus], dict[str, int], int]:
        root_fd, status_fd = self._ensure_open()
        self._verify_root_and_status()
        names = set(os.listdir(root_fd))
        required = {_MARKER, _LOCK, _STATUS}
        if not required <= names:
            raise StoreIntegrityError("raw-log root is missing a required store entry")
        self._verify_marker(root_fd)
        lock_info = os.stat(_LOCK, dir_fd=root_fd, follow_symlinks=False)
        _private_regular(lock_info, name="quota lock")
        if _identity(lock_info) != self._lock_identity:
            raise StoreIntegrityError("quota lock was replaced")
        status_info = os.stat(_STATUS, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(status_info.st_mode) or _identity(status_info) != _identity(os.fstat(status_fd)):
            raise StoreIntegrityError("status directory is invalid")

        raw_sizes: dict[str, int] = {}
        raw_identities: dict[str, tuple[int, int]] = {}
        for name in names - required:
            if not name.endswith(".log"):
                raise StoreIntegrityError(f"unexpected entry in raw-log root: {name[:80]}")
            try:
                run_id = _valid_run_id(name[:-4])
            except ValueError as exc:
                raise StoreIntegrityError("raw-log entry has an invalid run id") from exc
            named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            _private_regular(named, name=f"raw log for {run_id}")
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root_fd,
            )
            try:
                info = os.fstat(descriptor)
                _private_regular(info, name=f"raw log for {run_id}")
                named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if _identity(named) != _identity(info) or info.st_size < 0:
                    raise StoreIntegrityError(f"raw log for {run_id} was replaced")
                raw_sizes[run_id] = info.st_size
                raw_identities[run_id] = _identity(info)
            finally:
                os.close(descriptor)

        statuses: dict[str, _StoredStatus] = {}
        for filename in os.listdir(status_fd):
            if not filename.endswith(".json"):
                raise StoreIntegrityError(f"unexpected entry in status directory: {filename[:80]}")
            try:
                run_id = _valid_run_id(filename[:-5])
            except ValueError as exc:
                raise StoreIntegrityError("status entry has an invalid run id") from exc
            stored = self._read_status(run_id)
            if stored is None:
                raise StoreIntegrityError(f"status for {run_id} disappeared")
            current = stored.snapshot
            actual_size = raw_sizes.get(run_id, 0)
            if current.stored_bytes != actual_size:
                raise StoreIntegrityError(f"raw-log size differs from status for {run_id}")
            if stored.raw_identity != raw_identities.get(run_id):
                raise StoreIntegrityError(f"raw-log path identity differs from status for {run_id}")
            if current.stored_bytes > self.per_run_limit:
                raise StoreIntegrityError(f"per-run quota is corrupt for {run_id}")
            if current.stored_bytes == self.per_run_limit and current.cap_source is None:
                raise StoreIntegrityError(f"run-cap source is missing for {run_id}")
            statuses[run_id] = stored

        if set(raw_sizes) - set(statuses):
            raise StoreIntegrityError("a raw log has no durable status")
        total = sum(raw_sizes.values())
        if total > self.project_limit:
            raise StoreIntegrityError("project raw-log quota is corrupt")
        return statuses, raw_sizes, total

    def _atomic_status(
        self,
        status: RawLogStatus,
        raw_identity: tuple[int, int] | None,
    ) -> None:
        _, status_fd = self._ensure_open()
        filename = f"{status.run_id}.json"
        temp_name = f".tmp-{secrets.token_hex(12)}"
        payload = json.dumps(
            {"version": _STATUS_VERSION, "run_id": status.run_id,
             "status": asdict(status),
             "raw_identity": list(raw_identity) if raw_identity is not None else None},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > _MAX_STATUS_BYTES:
            raise StoreIntegrityError("serialized raw-log status exceeds its bound")
        try:
            try:
                target = os.stat(filename, dir_fd=status_fd, follow_symlinks=False)
            except FileNotFoundError:
                target = None
            if target is not None:
                _private_regular(target, name=f"status for {status.run_id}")
            descriptor = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=status_fd,
            )
            try:
                _private_regular(os.fstat(descriptor), name="temporary status")
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temp_name, filename, src_dir_fd=status_fd, dst_dir_fd=status_fd)
            os.fsync(status_fd)
            self._verify_root_and_status()
            current = os.stat(filename, dir_fd=status_fd, follow_symlinks=False)
            _private_regular(current, name=f"status for {status.run_id}")
        except BaseException:
            try:
                os.unlink(temp_name, dir_fd=status_fd)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _status_extends(base: RawLogStatus, candidate: RawLogStatus) -> bool:
        """Check a known status ancestor without weakening stored-prefix checks."""
        return (
            candidate.run_id == base.run_id
            and candidate.stored_bytes >= base.stored_bytes
            and candidate.observed_bytes >= base.observed_bytes
            and candidate.dropped_bytes >= base.dropped_bytes
            and candidate.missing_bytes >= base.missing_bytes
            and (not base.truncated or candidate.truncated)
            and (base.cap_source is None or candidate.cap_source == base.cap_source)
            and (base.first_observed_at is None
                 or candidate.first_observed_at == base.first_observed_at)
            and (base.last_persisted_at is None
                 or candidate.last_persisted_at is not None)
        )

    @classmethod
    def _validate_pending(
        cls, run_id: str, pending: _PendingFailure,
        durable_record: _StoredStatus | None, run_size: int,
    ) -> None:
        durable = durable_record.snapshot if durable_record is not None else RawLogStatus(run_id)
        identity = durable_record.raw_identity if durable_record is not None else None
        if durable.stored_bytes != run_size:
            raise StoreIntegrityError(f"raw-log size differs from status for {run_id}")
        for ancestor in (pending.base, pending.applied_visible):
            if ancestor is None:
                continue
            if (not cls._status_extends(ancestor.snapshot, durable)
                    or ancestor.raw_identity is not None
                    and ancestor.raw_identity != identity):
                raise StoreIntegrityError(f"pending failure for {run_id} has an unsafe durable base")

    @staticmethod
    def _compose_pending(
        run_id: str, durable: RawLogStatus, pending: _PendingFailure,
    ) -> RawLogStatus:
        first = min(filter(None, (durable.first_observed_at, pending.first_observed_at)))
        last = max(filter(None, (durable.last_observed_at, pending.last_observed_at)))
        missing = durable.missing_bytes + pending.bytes
        return RawLogStatus(
            run_id=run_id,
            stored_bytes=durable.stored_bytes,
            observed_bytes=durable.observed_bytes + pending.bytes,
            dropped_bytes=durable.dropped_bytes + pending.bytes,
            truncated=missing > 0,
            missing_bytes=missing,
            cap_source=durable.cap_source,
            first_observed_at=first,
            last_observed_at=last,
            last_persisted_at=durable.last_persisted_at,
            storage_error=pending.storage_error,
        )

    def _record_failure(
        self, run_id: str, amount: int, observed_at: str,
        error: BaseException, known_record: _StoredStatus | None,
    ) -> RawLogStatus:
        pending = self._pending.get(run_id)
        if pending is None:
            pending = _PendingFailure(
                base=known_record, bytes=0, events=0, first_observed_at=observed_at,
                last_observed_at=observed_at, storage_error=_error_name(error),
            )
        pending = replace(
            pending, bytes=pending.bytes + amount, events=pending.events + 1,
            last_observed_at=observed_at, storage_error=_error_name(error),
        )
        self._pending[run_id] = pending
        base = self._unconfirmed.get(run_id)
        if base is None:
            base = known_record.snapshot if known_record is not None else RawLogStatus(run_id)
        return self._compose_pending(run_id, base, pending)

    def append(
        self,
        run_id: str,
        data: bytes | bytearray | memoryview,
        *,
        observed_at: datetime | str | None = None,
    ) -> RawLogStatus:
        """Persist the fitting prefix and return status even when storage fails."""
        run_id = _valid_run_id(run_id)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("raw-log data must be bytes-like")
        payload = bytes(data)
        timestamp = _timestamp(observed_at)

        with self._thread_lock:
            known_record = self._known_records.get(run_id)
            try:
                with self._project_lock():
                    read_record = self._read_status(run_id)
                    if read_record is not None or known_record is None:
                        known_record = read_record
                    statuses, raw_sizes, project_size = self._scan_project()
                    previous_record = statuses.get(run_id)
                    known_record = previous_record
                    self._known_records[run_id] = previous_record
                    unconfirmed = self._unconfirmed.get(run_id)
                    if unconfirmed is not None:
                        current = (previous_record.snapshot if previous_record is not None
                                   else RawLogStatus(run_id))
                        if replace(current, storage_error=unconfirmed.storage_error) != unconfirmed:
                            raise StoreIntegrityError(
                                f"unconfirmed raw append for {run_id} differs from durable status"
                            )
                        os.fsync(self._ensure_open()[1])
                        self._unconfirmed.pop(run_id, None)

                    pending = self._pending.get(run_id)
                    if pending is not None:
                        self._validate_pending(
                            run_id, pending, previous_record, raw_sizes.get(run_id, 0)
                        )
                        if pending.applied_visible is not None:
                            os.fsync(self._ensure_open()[1])
                            pending = replace(
                                pending, base=previous_record, applied_visible=None
                            )
                            if pending.events == 0:
                                self._pending.pop(run_id, None)
                                pending = None
                            else:
                                self._pending[run_id] = pending
                    if pending is not None:
                        durable = (previous_record.snapshot if previous_record is not None
                                   else RawLogStatus(run_id))
                        merged = self._compose_pending(run_id, durable, pending)
                        if merged.cap_source is None:
                            run_hit = merged.stored_bytes >= self.per_run_limit
                            project_hit = project_size >= self.project_limit
                            if run_hit or project_hit:
                                cap_source = ("run+project" if run_hit and project_hit
                                              else "run" if run_hit else "project")
                                merged = replace(merged, cap_source=cap_source)
                        merged_record = _StoredStatus(
                            merged,
                            previous_record.raw_identity if previous_record is not None else None,
                        )
                        try:
                            self._atomic_status(
                                merged_record.snapshot, merged_record.raw_identity
                            )
                        except (OSError, StoreIntegrityError):
                            # Preserve the in-flight delta if rename was visible but
                            # directory durability could not be confirmed.
                            try:
                                visible = self._read_status(run_id)
                            except (OSError, StoreIntegrityError):
                                visible = None
                            if visible == merged_record:
                                self._pending[run_id] = replace(
                                    pending, base=visible, bytes=0, events=0,
                                    applied_visible=visible,
                                )
                                known_record = visible
                                self._known_records[run_id] = visible
                            raise
                        self._pending.pop(run_id, None)
                        previous_record = merged_record
                        known_record = merged_record
                        self._known_records[run_id] = merged_record
                    previous = (previous_record.snapshot if previous_record is not None
                                else RawLogStatus(run_id))
                    result = self._append_locked(
                        run_id, payload, timestamp, previous,
                        raw_sizes.get(run_id, 0), project_size,
                        previous_record.raw_identity if previous_record is not None else None,
                        run_id in raw_sizes,
                    )
                    try:
                        persisted = self._read_status(run_id)
                    except (OSError, StoreIntegrityError):
                        persisted = None
                    if persisted is not None and persisted.snapshot == result:
                        self._known_records[run_id] = persisted
                        self._unconfirmed.pop(run_id, None)
                    else:
                        self._unconfirmed[run_id] = result
                    return result
            except (OSError, StoreIntegrityError) as exc:
                if known_record is None:
                    try:
                        known_record = self._read_status(run_id)
                    except (OSError, StoreIntegrityError):
                        pass
                return self._record_failure(
                    run_id, len(payload), timestamp, exc, known_record
                )

    def _append_locked(
        self,
        run_id: str,
        payload: bytes,
        timestamp: str,
        previous: RawLogStatus,
        run_size: int,
        project_size: int,
        previous_identity: tuple[int, int] | None,
        run_exists: bool,
    ) -> RawLogStatus:
        observed = previous.observed_bytes + len(payload)
        first_observed = previous.first_observed_at or timestamp
        run_remaining = self.per_run_limit - run_size
        project_remaining = self.project_limit - project_size
        if run_remaining < 0 or project_remaining < 0:
            raise StoreIntegrityError("raw-log quota accounting became negative")
        accepted = min(len(payload), run_remaining, project_remaining)
        written = 0
        storage_error: str | None = None
        durable = True
        raw_identity = previous_identity
        root_fd, _ = self._ensure_open()

        if accepted:
            filename = f"{run_id}.log"
            descriptor: int | None = None
            try:
                flags = os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
                if run_exists:
                    named = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
                    _private_regular(named, name=f"raw log for {run_id}")
                    if previous_identity is not None and _identity(named) != previous_identity:
                        raise StoreIntegrityError(f"raw-log path identity changed for {run_id}")
                    descriptor = os.open(filename, flags, dir_fd=root_fd)
                else:
                    descriptor = os.open(
                        filename,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=root_fd,
                    )
                info = os.fstat(descriptor)
                _private_regular(info, name=f"raw log for {run_id}")
                if info.st_size != run_size:
                    raise StoreIntegrityError(f"raw-log size changed for {run_id}")
                raw_identity = _identity(info)
                if previous_identity is not None and raw_identity != previous_identity:
                    raise StoreIntegrityError(f"raw-log path identity changed for {run_id}")
                self._verify_named_file(root_fd, filename, descriptor)
                try:
                    written = _write_all(descriptor, payload[:accepted])
                except _PartialWriteError as exc:
                    written = exc.written
                    storage_error = _error_name(exc.__cause__ or exc)
                after = os.fstat(descriptor)
                if after.st_size != run_size + written:
                    raise StoreIntegrityError(f"raw-log append size is inconsistent for {run_id}")
                self._verify_named_file(root_fd, filename, descriptor)
                try:
                    os.fsync(descriptor)
                    if not run_exists:
                        os.fsync(root_fd)
                except OSError as exc:
                    durable = False
                    storage_error = storage_error or _error_name(exc)
            except (OSError, StoreIntegrityError) as exc:
                if descriptor is None:
                    written = 0
                elif isinstance(exc, _PartialWriteError):
                    written = exc.written
                if written:
                    durable = False
                storage_error = storage_error or _error_name(exc)
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError as exc:
                        durable = False
                        storage_error = storage_error or _error_name(exc)

        new_stored = run_size + written
        dropped_now = len(payload) - written
        missing_now = dropped_now + (written if accepted and not durable else 0)
        new_dropped = previous.dropped_bytes + dropped_now
        new_missing = previous.missing_bytes + missing_now
        total_after = project_size + written

        cap_source = previous.cap_source
        if cap_source is None:
            run_hit = new_stored >= self.per_run_limit
            project_hit = total_after >= self.project_limit
            if run_hit and project_hit:
                cap_source = "run+project"
            elif run_hit:
                cap_source = "run"
            elif project_hit:
                cap_source = "project"

        last_persisted = previous.last_persisted_at
        if written and durable:
            last_persisted = timestamp

        result = RawLogStatus(
            run_id=run_id,
            stored_bytes=new_stored,
            observed_bytes=observed,
            dropped_bytes=new_dropped,
            truncated=new_missing > 0,
            missing_bytes=new_missing,
            cap_source=cap_source,
            first_observed_at=first_observed,
            last_observed_at=timestamp,
            last_persisted_at=last_persisted,
            storage_error=storage_error,
        )
        try:
            self._atomic_status(result, raw_identity)
        except (OSError, StoreIntegrityError) as exc:
            result = RawLogStatus(
                run_id=result.run_id,
                stored_bytes=result.stored_bytes,
                observed_bytes=result.observed_bytes,
                dropped_bytes=result.dropped_bytes,
                truncated=result.truncated,
                missing_bytes=result.missing_bytes,
                cap_source=result.cap_source,
                first_observed_at=result.first_observed_at,
                last_observed_at=result.last_observed_at,
                last_persisted_at=result.last_persisted_at,
                storage_error=result.storage_error or _error_name(exc),
            )
        return result

    def status(self, run_id: str) -> RawLogStatus:
        """Return a bounded immutable snapshot; storage faults are status values."""
        run_id = _valid_run_id(run_id)
        with self._thread_lock:
            known_record = self._known_records.get(run_id)
            try:
                with self._project_lock():
                    read_record = self._read_status(run_id)
                    if read_record is not None or known_record is None:
                        known_record = read_record
                    statuses, _, _ = self._scan_project()
                    known_record = statuses.get(run_id)
                    self._known_records[run_id] = known_record
                    current = (known_record.snapshot if known_record is not None
                               else RawLogStatus(run_id))
                    unconfirmed = self._unconfirmed.get(run_id)
                    if unconfirmed is not None:
                        current = replace(current, storage_error=unconfirmed.storage_error)
                        if current != unconfirmed:
                            raise StoreIntegrityError(
                                f"unconfirmed raw append for {run_id} differs from durable status"
                            )
                    pending = self._pending.get(run_id)
                    if pending is not None:
                        self._validate_pending(run_id, pending, known_record,
                                               current.stored_bytes)
                        if pending.events:
                            return self._compose_pending(run_id, current, pending)
                        return replace(current, storage_error=pending.storage_error)
                    return current
            except (OSError, StoreIntegrityError) as exc:
                current = (self._unconfirmed.get(run_id)
                           or (known_record.snapshot if known_record is not None
                               else RawLogStatus(run_id)))
                pending = self._pending.get(run_id)
                if pending is not None and pending.events:
                    current = self._compose_pending(run_id, current, pending)
                return replace(current, storage_error=_error_name(exc))
