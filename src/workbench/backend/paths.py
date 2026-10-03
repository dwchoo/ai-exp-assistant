"""Data-dir resolution, single-instance lock and the backend identity record."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping

DATA_DIR_ENV = "WORKBENCH_DATA_DIR"
APP_DIR_NAME = "omp-workbench"
# sockaddr_un.sun_path is 108 bytes including the terminating NUL.
MAX_SOCKET_PATH_BYTES = 107


class DataDirError(RuntimeError):
    """The data dir is unsafe or unusable; nothing was started."""


class BackendLocked(RuntimeError):
    """Another backend already holds this data dir."""


def resolve_data_dir(cli_value: str | os.PathLike[str] | None,
                     environment: Mapping[str, str]) -> Path:
    """CLI argument, then ``WORKBENCH_DATA_DIR``, then the XDG state default."""
    if cli_value:
        chosen = Path(os.fspath(cli_value)).expanduser()
    elif environment.get(DATA_DIR_ENV):
        chosen = Path(environment[DATA_DIR_ENV]).expanduser()
    else:
        state_home = environment.get("XDG_STATE_HOME", "")
        # XDG: a relative value is invalid and must be ignored.
        if state_home and os.path.isabs(state_home):
            base = Path(state_home)
        else:
            home = environment.get("HOME") or os.path.expanduser("~")
            base = Path(home) / ".local" / "state"
        chosen = base / APP_DIR_NAME
    return Path(os.path.abspath(chosen))


@dataclass(frozen=True, slots=True)
class DataLayout:
    root: Path

    @property
    def lock(self) -> Path: return self.root / "backend.lock"
    @property
    def record(self) -> Path: return self.root / "backend.json"
    @property
    def ui_socket(self) -> Path: return self.root / "ui.sock"
    @property
    def bridge_socket(self) -> Path: return self.root / "bridge.sock"
    @property
    def log(self) -> Path: return self.root / "backend.log"
    @property
    def tasks(self) -> Path: return self.root / "tasks.sqlite3"
    @property
    def raw_logs(self) -> Path: return self.root / "raw-logs"
    @property
    def pause_journal(self) -> Path: return self.root / "pause.jsonl"
    @property
    def lifecycle_journal(self) -> Path: return self.root / "lifecycle.json"
    @property
    def workflow(self) -> Path: return self.root / "workflow"

    def check_socket_paths(self) -> None:
        for path in (self.ui_socket, self.bridge_socket):
            if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
                raise DataDirError(f"socket path is too long for AF_UNIX: {path}")


def ensure_private_dir(path: Path) -> Path:
    """Create or tighten a user-owned 0700 directory; refuse symlinks/foreign owners."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise DataDirError(f"cannot create data dir {path}: {exc.strerror}") from exc
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise DataDirError(f"data dir is not a plain directory: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise DataDirError(f"data dir is not owned by the current user: {path}")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return path


class InstanceLock:
    """Non-blocking flock held for the backend lifetime; never inherited by children."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise DataDirError("backend lock file is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EWOULDBLOCK, errno.EAGAIN}:
                    raise BackendLocked(str(self.path)) from exc
                raise
        except BaseException:
            os.close(descriptor)
            raise
        self._fd = descriptor

    def held_elsewhere(self) -> bool:
        """Probe without keeping the lock (used by status/start checks)."""
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EWOULDBLOCK, errno.EAGAIN}:
                return True
            raise
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    """Atomic, fsynced 0600 JSON write inside an existing private directory."""
    write_private_bytes(path, json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def write_private_bytes(path: Path, payload: bytes) -> None:
    """Atomic, fsynced 0600 write inside an existing private directory (replaces, never follows ``path``)."""
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    name = f".{path.name}-{os.getpid()}-{os.urandom(6).hex()}"
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                             0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent_fd)


def read_private_json(path: Path, limit: int = 1 << 20) -> dict[str, Any] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > limit):
            return None
        raw = os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def unlink_stale_socket(path: Path) -> None:
    """Only called while holding the instance lock: a leftover socket is stale."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise DataDirError(f"refusing to replace a non-socket path: {path}")
    path.unlink()
