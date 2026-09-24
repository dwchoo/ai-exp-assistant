"""A real POSIX PTY session for the CW-02 candidate."""
from __future__ import annotations

import errno
import fcntl
import os
import pty
import signal
import struct
import termios
import time
from collections.abc import Mapping, Sequence


class PtySession:
    MAX_PENDING_WRITE_BYTES = 2 * 1024 * 1024

    def __init__(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> None:
        if not argv:
            raise ValueError("PTY command must not be empty")
        pid, master_fd = pty.fork()
        if pid == 0:
            child_env = os.environ.copy()
            if env is not None:
                child_env.update(env)
            try:
                os.execvpe(argv[0], list(argv), child_env)
            except BaseException as exc:
                os.write(2, f"g1: exec failed: {exc}\n".encode("utf-8", "replace"))
                os._exit(127)

        self.pid = pid
        self.master_fd = master_fd
        self.returncode: int | None = None
        self._pending_write = bytearray()
        os.set_blocking(master_fd, False)

    def resize(self, rows: int, columns: int) -> None:
        rows, columns = max(1, rows), max(1, columns)
        fcntl.ioctl(
            self.master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, columns, 0, 0),
        )

    def read_available(self, limit: int = 262144) -> bytes:
        chunks: list[bytes] = []
        remaining = limit
        while remaining > 0:
            try:
                data = os.read(self.master_fd, min(65536, remaining))
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def write(self, data: bytes) -> int:
        """Accept as much input as fits in the bounded queue and return its length."""
        if not data:
            return 0

        self.flush_writes()
        available = self.MAX_PENDING_WRITE_BYTES - len(self._pending_write)
        accepted = min(len(data), max(0, available))
        if accepted == 0:
            return 0

        self._pending_write.extend(data[:accepted])
        self.flush_writes()
        return accepted

    def write_frame(self, data: bytes) -> bool:
        """Queue a complete input frame or leave the queue unchanged."""
        if not data:
            return True
        if len(data) > self.MAX_PENDING_WRITE_BYTES:
            return False

        self.flush_writes()
        if len(data) > self.MAX_PENDING_WRITE_BYTES - len(self._pending_write):
            return False
        self._pending_write.extend(data)
        self.flush_writes()
        return True

    @property
    def pending_write_bytes(self) -> int:
        return len(self._pending_write)

    def flush_writes(self) -> int:
        """Attempt queued input without waiting for the child to read."""
        if not self._pending_write:
            return 0
        view = memoryview(self._pending_write)
        written_total = 0
        try:
            while written_total < len(view):
                try:
                    written = os.write(self.master_fd, view[written_total:])
                except BlockingIOError:
                    break
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        break
                    raise
                if written <= 0:
                    break
                written_total += written
        finally:
            view.release()
        if written_total:
            del self._pending_write[:written_total]
        return written_total

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return self.returncode
        if pid == 0:
            return None
        if os.WIFEXITED(status):
            self.returncode = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self.returncode = -os.WTERMSIG(status)
        else:
            self.returncode = status
        return self.returncode

    def close(self, *, terminate: bool = True) -> None:
        if terminate and self.poll() is None:
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 0.8
            while self.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if self.poll() is None:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 0.8
                while self.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
        self._pending_write.clear()
        try:
            os.close(self.master_fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
