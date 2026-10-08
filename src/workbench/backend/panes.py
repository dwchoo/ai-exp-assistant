"""Backend-owned PTY panes with bounded, all-or-nothing input admission.

Each pane keeps a monotonically increasing display sequence and a bounded
retained tail for reattach replay. Admission either queues a whole input frame
or rejects it with a reason; a rejected frame never reaches the PTY.
"""

from __future__ import annotations

from collections import deque
import errno
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import stat
import struct
import termios
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from workbench.contracts.ui_v1 import MAX_PASTE_BYTES, Reason
from workbench.contracts.v1 import DisplayChunk, PaneId, new_identifier
from workbench.runtime.process_evidence import LinuxProcessProbe, ProcessRef
from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState
from workbench.terminal.shell_persistent.adapter import PersistentShell

INPUT_QUEUE_BYTES = MAX_PASTE_BYTES
REPLAY_BYTES = 256 * 1024
READ_CHUNK_BYTES = 65536
SHELL_WRITE_CHUNK_BYTES = 1024
# One pump may spend at most this long writing queued host-shell input, so a
# 2 MiB paste never stalls OMP output, control drain, UI requests or SIGTERM.
SHELL_FLUSH_SLICE_SECONDS = 0.02
# Upper bound on pidfds an OMP pane keeps for its own session members.
SESSION_PIN_LIMIT = 256
# C-D63 force-kill: SIGHUP/SIGTERM grace before SIGKILL of the host shell session.
SHELL_KILL_GRACE = 1.0
DEFAULT_SIZE = (30, 100)
ENV_PROBE_SECONDS = 0.5  # CW-16 D-B2-1: re-reading a names file the next prompt is rewriting
# CW-16 B3 F1: detached children of a Workbench OMP (e.g. OMP 18.8.0's daemon broker, which calls setsid and
# outlives its parent for ~1-3 s): rescanned while the OMP lives, waited for after it ends, then ended.
DETACHED_SCAN_SECONDS = 2.0
DETACHED_WAIT_SECONDS = 5.0
DETACHED_TERM_GRACE = 2.0
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_NAMES_MAX = 1 << 20


def _read_env_names(path: Path) -> set[str] | None:
    """The names the prompt hook wrote (``WBENV1 L``: one per line, ``WBENV1 Z``: NUL-ended), or None when the
    file is missing, too large or not complete (no end marker yet)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > _ENV_NAMES_MAX:
            return None
        data = os.read(fd, _ENV_NAMES_MAX + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    end = b"\x01END\n"
    if not data.endswith(end):
        return None
    if data.startswith(b"WBENV1 L\n"):
        items = data[len(b"WBENV1 L\n"):-len(end)].split(b"\n")
    elif data.startswith(b"WBENV1 Z\n"):
        items = data[len(b"WBENV1 Z\n"):-len(end)].split(b"\0")
    else:
        return None
    return {item.decode("ascii") for item in items if _ENV_NAME.fullmatch(item.decode("ascii", "replace"))}


def process_ref(role: str, pid: int) -> ProcessRef | None:
    try:
        ticks = LinuxProcessProbe.start_ticks(pid)
    except OSError:
        return None
    return ProcessRef(role, pid, ticks, 1) if ticks else None


def ref_dict(ref: ProcessRef | None) -> dict[str, Any] | None:
    return None if ref is None else {"pid": ref.pid, "start_ticks": ref.start_ticks}


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _stat(pid: int) -> list[bytes] | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            return stream.read().rsplit(b") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def _live(fields: list[bytes] | None) -> bool:
    return fields is not None and fields[0] not in {b"Z", b"X"}


def _pidfd_exited(fd: int) -> bool:
    """A pidfd polls readable once its process has exited (zombie or reaped)."""
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(0))


def _signal_failure(exc: OSError) -> str:
    """Why a pinned member could not be signalled (it stays pinned and is reported)."""
    if isinstance(exc, PermissionError):
        return "permission_denied"  # e.g. sudo/su/pkexec: the member now runs as another user
    return f"signal_failed:{errno.errorcode.get(exc.errno, exc.errno)}"


def _pin_member(pid: int, session: int) -> tuple[int, bytes] | None:
    """(pidfd, start) for a live member of ``session``; identity re-read after opening.

    Returns None unless the process at ``pid`` is the same live process in
    ``session`` before and after ``pidfd_open`` and the pidfd has not exited, so
    the pidfd provably names that member (C-D45).
    """
    before = _stat(pid)
    try:
        if not _live(before) or int(before[3]) != session:
            return None
        fd = os.pidfd_open(pid)
    except (OSError, ValueError, IndexError):
        return None
    after = _stat(pid)
    if not _live(after) or after[3] != before[3] or after[19] != before[19] or _pidfd_exited(fd):
        os.close(fd)
        return None
    return fd, before[19]


class _OwnedSession:
    """Pinned ownership of the session a backend child leads (C-D45).

    The owner provides ``pid`` (the session leader, a child of this backend),
    ``ref`` (its start ticks), ``_reaped``, ``_pins``, ``_unsignallable`` and
    ``_drain_quietly``. Signals go only through pidfds that provably name a
    member of that session. A member that refuses a signal (EPERM: it now runs
    as another user, e.g. under sudo/su/pkexec) is skipped, never raised, and
    reported as a survivor with the reason.
    """

    pid: int
    ref: ProcessRef | None
    _reaped: bool
    _pins: dict[int, tuple[int, bytes]]
    # pid -> reason for pinned members whose signal failed (never retried, reported as survivors).
    _unsignallable: dict[int, str]

    def _drain_quietly(self, timeout: float) -> None:
        raise NotImplementedError

    def _leader_exited(self) -> bool:
        """Whether the leader child exited; never reaps, so a zombie keeps its numbers."""
        if self._reaped:
            return True
        try:
            return os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            self._reaped = True  # not ours to reap any more; its numbers prove nothing
            return True

    def _leader_proven(self) -> bool:
        """The leader child is unreaped and still ours: ppid is the backend, start ticks match."""
        if self._reaped or self.ref is None:
            return False
        fields = _stat(self.pid)
        return (fields is not None and int(fields[1]) == os.getpid()
                and int(fields[19]) == self.ref.start_ticks)

    def _pin_holds(self, pid: int) -> bool:
        """A pinned member is the same live process and still in this session."""
        fd, start = self._pins[pid]
        fields = _stat(pid)
        return (_live(fields) and fields[19] == start and int(fields[3]) == self.pid
                and not _pidfd_exited(fd))

    def _drop_stale_pins(self) -> None:
        for pid in [pid for pid in self._pins if not self._pin_holds(pid)]:
            os.close(self._pins.pop(pid)[0])
            self._unsignallable.pop(pid, None)

    def _release_pins(self) -> None:
        for fd, _ in self._pins.values():
            os.close(fd)
        self._pins.clear()
        self._unsignallable.clear()

    def _send(self, pid: int, signum: int) -> bool:
        """Signal one pinned member; False when it is gone or refuses (then recorded, never raised)."""
        try:
            signal.pidfd_send_signal(self._pins[pid][0], signum)
        except ProcessLookupError:
            return False
        except OSError as exc:
            self._unsignallable[pid] = _signal_failure(exc)
            return False
        return True

    def _survivors(self) -> list[dict[str, Any]]:
        """Pinned members still alive in this session, each with why it was not ended."""
        self._drop_stale_pins()
        return [{"pid": pid, "reason": self._unsignallable.get(pid, "did_not_exit")} for pid in sorted(self._pins)]

    def _pin_session(self) -> None:
        """Pin every live member of this session while the number is provably ours.

        Proof is the unreaped leader, or a pinned member that is live and in
        the session both before and after the scan: a process never rejoins a
        session it left, and a session number cannot be reallocated while any
        member uses it, so every member seen in between is ours.
        """
        self._drop_stale_pins()
        anchors = list(self._pins)
        if not self._leader_proven() and not anchors:
            return
        found: dict[int, tuple[int, bytes]] = {}
        try:
            names = os.listdir("/proc")
        except OSError:
            names = []
        for name in names:
            if len(self._pins) + len(found) >= SESSION_PIN_LIMIT:
                break
            if not name.isdecimal() or int(name) in self._pins:
                continue
            pinned = _pin_member(int(name), self.pid)
            if pinned is not None:
                found[int(name)] = pinned
        if self._leader_proven() or any(self._pin_holds(pid) for pid in anchors):
            self._pins.update(found)
        else:
            for fd, _ in found.values():
                os.close(fd)

    def _kill_owned_session(self) -> set[int]:
        """KILL remaining members of this own session; skip when unprovable.

        Signals go only through pinned pidfds. The unreaped leader keeps the
        session provable across rounds; once it is reaped, one pinned member is
        spared per round as the anchor that proves the next scan (an
        unsignallable pinned member serves as that anchor when there is one),
        and is killed last when it is the only member left. A member that
        refuses SIGKILL is skipped and stays pinned. Returns the pids sent SIGKILL.
        """
        killed: set[int] = set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self._pin_session()
            signallable = [pid for pid in self._pins if pid not in self._unsignallable]
            if not signallable:
                break
            keep = None
            if not self._leader_proven() and len(signallable) > 1 and len(signallable) == len(self._pins):
                keep = signallable[0]
            sent = [pid for pid in signallable if pid != keep and self._send(pid, signal.SIGKILL)]
            killed.update(sent)
            targets = [self._pins[pid][0] for pid in sent]
            settle = min(deadline, time.monotonic() + 0.5)
            while any(not _pidfd_exited(fd) for fd in targets) and time.monotonic() < settle:
                self._drain_quietly(0.01)
        return killed


def _pin_child(pid: int, parent: int, session: int) -> tuple[int, bytes] | None:
    """(pidfd, start) for a live direct child of ``parent`` outside ``session``; identity re-read after opening."""
    before = _stat(pid)
    try:
        if not _live(before) or int(before[1]) != parent or int(before[3]) == session:
            return None
        fd = os.pidfd_open(pid)
    except (OSError, ValueError, IndexError):
        return None
    after = _stat(pid)
    if (not _live(after) or after[19] != before[19] or int(after[1]) != parent
            or _pidfd_exited(fd)):
        os.close(fd)
        return None
    return fd, before[19]


class DetachedChildren:
    """CW-16 B3 F1 (C-AC-22): processes a Workbench OMP started in their own session, by exact identity.

    Each entry is a pidfd opened while the process was provably a direct
    child of the live OMP this backend started (never a name or command line
    match, so a user's own OMP broker is never a candidate). ``finish`` waits
    a bounded time for them to end by themselves, then signals only these
    pidfds (TERM, then KILL) and reports what is still alive.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[int, int], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def add(self, entries: Sequence[dict[str, Any]]) -> None:
        with self._lock:
            for entry in entries:
                key = (entry["pid"], int(entry["start_ticks"]))
                if key in self._entries:
                    os.close(entry.pop("fd"))
                else:
                    self._entries[key] = entry
            for key in [k for k, e in self._entries.items() if e.get("ended") is None and _pidfd_exited(e["fd"])]:
                os.close(self._entries.pop(key)["fd"])  # ended by itself before the shutdown: nothing to show

    def alive(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._view(e) for e in self._entries.values() if not _pidfd_exited(e["fd"])]

    @staticmethod
    def _view(entry: Mapping[str, Any]) -> dict[str, Any]:
        return {name: entry.get(name) for name in ("pid", "start_ticks", "parent", "parent_pid", "ended")}

    def finish(self, wait: float = DETACHED_WAIT_SECONDS, grace: float = DETACHED_TERM_GRACE) -> dict[str, Any]:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()

        def pending() -> list[dict[str, Any]]:
            for entry in entries:
                if entry.get("ended") is None and _pidfd_exited(entry["fd"]):
                    entry["ended"] = entry.get("signalled") or "by_itself"
            return [entry for entry in entries if entry.get("ended") is None]

        deadline = time.monotonic() + max(wait, 0.0)
        while pending() and time.monotonic() < deadline:
            time.sleep(0.05)
        for signum, label, settle in ((signal.SIGTERM, "terminated", grace), (signal.SIGKILL, "killed", 1.0)):
            left = pending()
            if not left:
                break
            for entry in left:
                try:
                    signal.pidfd_send_signal(entry["fd"], signum)
                    entry["signalled"] = label
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    entry["why_not"] = _signal_failure(exc)
            deadline = time.monotonic() + settle
            while pending() and time.monotonic() < deadline:
                time.sleep(0.02)
        alive = pending()
        for entry in alive:
            entry["ended"] = None
        result = {"observed": [self._view(e) for e in entries],
                  "alive": [dict(self._view(e), why_not=e.get("why_not", "did_not_exit")) for e in alive]}
        for entry in entries:
            os.close(entry["fd"])
        return result


class Pane:
    """Common sequence/replay/queue bookkeeping."""

    def __init__(self, pane_id: PaneId, size: tuple[int, int]):
        self.pane_id = pane_id
        self.session_id = new_identifier()
        self.generation = 1
        self.sequence = 0
        self.size = size
        self._replay: deque[DisplayChunk] = deque()
        self._replay_bytes = 0
        self.dropped_input_bytes = 0
        self.last_input_problem: str | None = None

    # -- display ---------------------------------------------------------
    def _chunk(self, data: bytes) -> DisplayChunk:
        self.sequence += 1
        chunk = DisplayChunk(self.session_id, self.generation, self.pane_id, self.sequence, data)
        self._replay.append(chunk)
        self._replay_bytes += len(data)
        while self._replay_bytes > REPLAY_BYTES and len(self._replay) > 1:
            self._replay_bytes -= len(self._replay.popleft().data)
        return chunk

    def replay(self) -> list[DisplayChunk]:
        return list(self._replay)

    # -- interface -------------------------------------------------------
    def fds(self) -> list[int]:
        raise NotImplementedError

    def pump(self) -> list[DisplayChunk]:
        raise NotImplementedError

    def admit(self, data: bytes) -> tuple[Reason, str] | None:
        raise NotImplementedError

    def resize(self, rows: int, cols: int) -> None:
        raise NotImplementedError

    def info(self) -> dict[str, Any]:
        return {"pane": self.pane_id.value, "session_id": self.session_id, "generation": self.generation,
                "sequence": self.sequence, "rows": self.size[0], "cols": self.size[1],
                "dropped_input_bytes": self.dropped_input_bytes,
                "last_input_problem": self.last_input_problem}

    def close(self) -> dict[str, Any]:
        raise NotImplementedError


class OmpPane(_OwnedSession, Pane):
    """One OMP process on its own PTY and session; the backend is its parent."""

    def __init__(self, pane_id: PaneId, role: str, argv: Sequence[str], env: Mapping[str, str],
                 *, cwd: str | None = None, size: tuple[int, int] = DEFAULT_SIZE, generation: int = 1):
        super().__init__(pane_id, size)
        # A restarted pane (C-D62) streams under a new session id and a higher generation.
        self.generation = generation
        self.role = role
        # Set by the backend: the last restart of this pane slot (None until one happens).
        self.restart: dict[str, Any] | None = None
        self.returncode: int | None = None
        self._pending = bytearray()
        rows, cols = size
        argv, env = list(argv), dict(env)
        pid, master = pty.fork()
        if pid == 0:  # pragma: no cover - child side
            try:
                if cwd is not None:
                    os.chdir(cwd)
                _set_winsize(0, rows, cols)
                # Python ignores SIGPIPE/SIGXFSZ; OMP starts with defaults.
                signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
                os.execvpe(argv[0], argv, env)
            except BaseException as exc:
                try:
                    os.write(2, f"workbench: cannot start {role} OMP: {exc}\n".encode("utf-8", "replace"))
                finally:
                    os._exit(127)
        self.pid, self.master_fd = pid, master
        self._eof = False
        self._reaped = False
        # pid -> (pidfd, start ticks) of own session members, pinned while proven.
        self._pins: dict[int, tuple[int, bytes]] = {}
        self._unsignallable: dict[int, str] = {}
        os.set_blocking(master, False)
        # The unreaped child is ours; its start ticks prove session ownership later.
        self.ref = process_ref(f"{role}_omp", pid)
        # CW-16 B3 F1: (pid, start ticks) -> pidfd of this OMP's direct children in another session.
        self._detached: dict[tuple[int, bytes], int] = {}
        self._detached_scan_at = 0.0

    def fds(self) -> list[int]:
        # After EOF/EIO a PTY master stays readable; stop selecting on it.
        return [self.master_fd] if self.master_fd >= 0 and not self._eof else []

    def poll(self) -> int | None:
        if self.returncode is not None or self._reaped:
            return self.returncode
        if not self._leader_exited():
            return None
        if self._reaped:
            return self.returncode
        # The unreaped zombie still holds the session number: pin the members
        # of this session now, because after the reap a bare number proves nothing.
        self._pin_session()
        _, status = os.waitpid(self.pid, 0)
        self._reaped = True
        self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def pump(self) -> list[DisplayChunk]:
        chunks = []
        if self.master_fd < 0:
            return chunks
        self._flush()
        try:
            data = os.read(self.master_fd, READ_CHUNK_BYTES)
        except BlockingIOError:
            data = None
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b""
        if data:
            chunks.append(self._chunk(data))
        elif data == b"":
            self._eof = True
        if time.monotonic() >= getattr(self, "_detached_scan_at", 0.0):
            self._detached_scan_at = time.monotonic() + DETACHED_SCAN_SECONDS
            self._scan_detached()
        self.poll()
        return chunks

    def scan_detached(self) -> None:
        """Pin detached children now (the backend calls it before anything at a full shutdown signals an OMP)."""
        self._scan_detached()

    def _scan_detached(self) -> None:
        """CW-16 B3 F1: pin this OMP's direct children that left its session (e.g. its daemon broker).

        Only while the OMP is the unexited child this backend started (its pid
        cannot name another process then); a child is pinned by pidfd with its
        parent re-read after opening. They are reparented when the OMP ends, so
        this runs before ``close`` signals the OMP and every few seconds.
        """
        if not hasattr(self, "_detached"):  # an instance built without __init__ (tests)
            self._detached = {}
        for key in [key for key, fd in self._detached.items() if _pidfd_exited(fd)]:
            os.close(self._detached.pop(key))
        if self._leader_exited() or not self._leader_proven():
            return
        try:
            names = os.listdir("/proc")
        except OSError:
            return
        known = {pid for pid, _ in self._detached}
        for name in names:
            if len(self._detached) >= SESSION_PIN_LIMIT:
                break
            if not name.isdecimal() or int(name) in known or int(name) == self.pid:
                continue
            pinned = _pin_child(int(name), self.pid, self.pid)
            if pinned is None:
                continue
            if self._leader_exited() or not self._leader_proven():
                os.close(pinned[0])  # the parent ended meanwhile: its pid proves nothing any more
                return
            self._detached[(int(name), pinned[1])] = pinned[0]

    def take_detached(self) -> list[dict[str, Any]]:
        """Hand the pinned detached children (pidfds included) to the backend's ``DetachedChildren``."""
        taken: list[dict[str, Any]] = []
        for (pid, start), fd in getattr(self, "_detached", {}).items():
            taken.append({"pid": pid, "start_ticks": int(start), "fd": fd, "parent": f"{self.role}_omp",
                          "parent_pid": self.pid, "ended": None})
        getattr(self, "_detached", {}).clear()
        return taken

    def _flush(self) -> None:
        if not self._pending or self.master_fd < 0:
            return
        try:
            written = os.write(self.master_fd, self._pending)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno not in {errno.EIO, errno.EBADF}:
                raise
            self.dropped_input_bytes += len(self._pending)
            self.last_input_problem = "pane_exited"
            self._pending.clear()
            return
        del self._pending[:written]

    def wants_write(self) -> bool:
        return bool(self._pending)

    def admit(self, data: bytes) -> tuple[Reason, str] | None:
        if self.poll() is not None or self.master_fd < 0:
            return Reason.PANE_UNAVAILABLE, "OMP process has exited"
        if len(data) > INPUT_QUEUE_BYTES:
            return Reason.PASTE_TOO_LARGE, f"{len(data)} bytes exceeds {INPUT_QUEUE_BYTES}"
        self._flush()
        free = INPUT_QUEUE_BYTES - len(self._pending)
        if len(data) > free:
            return Reason.QUEUE_FULL, f"{len(data)} bytes exceeds free queue space {free}"
        self._pending.extend(data)
        self._flush()
        return None

    def resize(self, rows: int, cols: int) -> None:
        if self.master_fd >= 0 and self.poll() is None:
            _set_winsize(self.master_fd, rows, cols)
        self.size = (rows, cols)

    def info(self) -> dict[str, Any]:
        alive = self.poll() is None
        return {**super().info(), "role": self.role, "process": ref_dict(self.ref), "alive": alive,
                "exit_status": self.returncode, "input_owner": "user",
                "queued_input_bytes": len(self._pending), "input_capacity": INPUT_QUEUE_BYTES,
                "restart": None if self.restart is None else dict(self.restart)}

    def terminate(self) -> bool:
        """C-D70 (3): ask this OMP to end: SIGTERM to its own process group, only while its unreaped child is
        proven ours (never a pattern or another process). True when the signal was sent; ``close`` KILLs later."""
        if self._leader_exited() or not self._leader_proven():
            return False
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except OSError:
            return False
        return True

    def close(self, grace: float = 3.0) -> dict[str, Any]:
        """TERM the proven OMP group, then KILL every proven member left in its session.

        Never raises for a member that cannot be signalled: it is reported in ``survivors``.
        Its detached children (CW-16 B3 F1) are pinned first; ``take_detached`` hands them to the backend.
        """
        self._scan_detached()
        if not self._leader_exited() and self._leader_proven():
            # Unreaped child of this backend: its group number is still ours.
            try:
                os.killpg(self.pid, signal.SIGTERM)
                terminated = True
            except ProcessLookupError:
                terminated = True
            except OSError:
                terminated = False  # EPERM: no group member took it; the pinned KILL below decides
            deadline = time.monotonic() + (grace if terminated else 0.0)
            while not self._leader_exited() and time.monotonic() < deadline:
                self._drain_quietly(0.05)
        self._kill_owned_session()
        deadline = time.monotonic() + 2.0
        while self.poll() is None and time.monotonic() < deadline:
            self._drain_quietly(0.02)
        survivors = self._survivors()
        self._release_pins()
        if self.master_fd >= 0:
            os.close(self.master_fd)
            self.master_fd = -1
        self._pending.clear()
        return {"pane": self.pane_id.value, "exit_status": self.returncode, "survivors": survivors}

    def _drain_quietly(self, timeout: float) -> None:
        try:
            if select.select([self.master_fd], [], [], timeout)[0]:
                os.read(self.master_fd, READ_CHUNK_BYTES)
        except OSError:
            time.sleep(timeout)


class ShellPane(_OwnedSession, Pane):
    """Backend ownership of the CW-07 PersistentShell; input goes through its boundary.

    The parent shell is a child of this backend leading its own PTY session. Its
    exit is observed without reaping first, so the zombie keeps the session
    number while the members still in that session are pinned (C-D45); then it
    is reaped. A force-kill (C-D63) signals only pinned members of that session:
    a process that called setsid is in another session and is never targeted.
    """

    def __init__(self, choice: ShellChoice, environment: dict[str, str],
                 *, size: tuple[int, int] = DEFAULT_SIZE, generation: int = 1):
        super().__init__(PaneId.HOST_SHELL, size)
        # CW-18: the backend loop and an approved run (HostShellPort, another thread)
        # share this shell; every use of ``self.shell`` holds this lock.
        self.io_lock = threading.RLock()
        # Display bytes drained outside ``pump`` (for a run's raw log) wait here for the UI.
        self._display_backlog = bytearray()
        self._taps: list[bytearray] = []
        # Set while Workbench types an approved run's start into the shell: user input is held.
        self.automation_hold: str | None = None
        # A restarted host shell (C-D63) streams under a new session id and a higher generation.
        self.generation = generation
        self.choice = choice
        self.shell = PersistentShell(user_environment=environment, choice=choice)
        self._pending = bytearray()
        self.pid = self.shell.parent_pid
        self.ref = process_ref("host_shell", self.pid)
        self._supervisor_ref: ProcessRef | None = None
        self.state: dict[str, Any] = self.shell.snapshot()
        self.error: str | None = None
        self.returncode: int | None = None
        self._exited = False
        self._reaped = False
        self._released = False
        self._final = b""
        self._drained_after_exit = False
        # pid -> (pidfd, start ticks) of own session members, pinned while proven.
        self._pins: dict[int, tuple[int, bytes]] = {}
        self._unsignallable: dict[int, str] = {}
        # Set by the backend: the last restart of this pane slot (None until one happens).
        self.restart: dict[str, Any] | None = None
        # The last force-kill of this shell and a manager request it closed (never a success).
        self.kill_result: dict[str, Any] | None = None
        self.closed_request: dict[str, Any] | None = None
        self.resize(*size)

    @property
    def _master_fd(self) -> int:
        return self.shell._transport.master_fd

    # -- exit and reaping ------------------------------------------------
    def poll(self) -> int | None:
        """Exit status once the parent shell exited; reaps it after pinning its session."""
        if self._exited:
            return self.returncode
        if not self._leader_exited():
            return None
        if not self._reaped:
            # The unreaped zombie still holds the session number: pin what is left now.
            self._pin_session()
            try:
                _, status = os.waitpid(self.pid, 0)
                self.returncode = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                pass
            self._reaped = True
        self._exited = True
        self._close_request("host_shell_exited")
        return self.returncode

    def exited(self) -> bool:
        self.poll()
        return self._exited

    def alive(self) -> bool:
        return (not self.exited() and self.ref is not None
                and LinuxProcessProbe().observe(self.ref).state == "alive")

    def request_in_flight(self) -> bool:
        """A manager request was sent to this shell and has not fully returned."""
        life = self.shell._transport.lifecycle
        return not self._exited and life.request_id is not None and not life.returned

    def _close_request(self, reason: str) -> None:
        """An unreturned manager request ends as unknown, never as a success."""
        life = self.shell._transport.lifecycle
        if life.request_id is None or life.returned or self.closed_request is not None:
            return
        self.closed_request = {"request_id": life.request_id, "outcome": "unknown", "task_success": None,
                               "reason": reason, "phase": self.state.get("phase"), "at": time.time()}
        life.fail_unknown(reason)

    # -- I/O -------------------------------------------------------------
    def fds(self) -> list[int]:
        transport = self.shell._transport
        if transport._closed or self._exited:
            return []
        return [transport.master_fd, transport._control_fd]

    def pump(self) -> list[DisplayChunk]:
        with self.io_lock:
            return self._pump_locked()

    def _take_display(self) -> bytes:
        """Drain the shell's display bytes once; copies go to every open tap (caller holds io_lock)."""
        data = self.shell.display_bytes()
        if data:
            for tap in self._taps:
                tap.extend(data)
        return data

    def _pump_locked(self) -> list[DisplayChunk]:
        if self._released or self.shell._transport._closed:
            final, self._final = self._final, b""
            final = bytes(self._display_backlog) + final
            self._display_backlog.clear()
            return [self._chunk(final)] if final else []
        if not self._exited:
            self.poll()
        elif self._drained_after_exit:
            return []
        # An exited shell's last output is drained once, then it leaves the select set.
        self._drained_after_exit = self._exited
        try:
            self.state = self.shell.poll(0)
            self._flush()
            data = self._take_display()
        except (OSError, UnsafeShellState) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = self.shell.snapshot()
            data = b""
        if self._display_backlog:
            data = bytes(self._display_backlog) + data
            self._display_backlog.clear()
        supervisor = self.state["lifecycle"].get("supervisor_pid")
        if supervisor and (self._supervisor_ref is None or self._supervisor_ref.pid != supervisor):
            self._supervisor_ref = process_ref("supervisor", supervisor)
        return [self._chunk(data)] if data else []

    def _accepts_manual_input(self) -> tuple[Reason, str] | None:
        state = self.state
        if self.automation_hold is not None:
            return Reason.HOST_SHELL_AUTOMATION, self.automation_hold
        if state["input_owner"] != "user":
            return Reason.INPUT_OWNER_MANAGER, "manager owns shell input; request takeover first"
        if self.error is not None:
            return Reason.INPUT_TARGET_UNKNOWN, "shell boundary unknown; input held"
        # The adapter decides the current manual target. Line-edit uncertainty
        # (phase "unknown") holds automation only and stays visible in state.
        held = self.shell.manual_input_hold()
        if held is not None:
            return Reason.INPUT_TARGET_UNKNOWN, f"{held} (mode {state['parent_mode']})"
        return None

    def admit(self, data: bytes) -> tuple[Reason, str] | None:
        with self.io_lock:
            return self._admit_locked(data)

    def _admit_locked(self, data: bytes) -> tuple[Reason, str] | None:
        if self._released or self.exited():
            return Reason.PANE_UNAVAILABLE, "host shell has exited; restart it first"
        if len(data) > INPUT_QUEUE_BYTES:
            return Reason.PASTE_TOO_LARGE, f"{len(data)} bytes exceeds {INPUT_QUEUE_BYTES}"
        try:
            self.state = self.shell.poll(0)
        except (OSError, UnsafeShellState) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        held = self._accepts_manual_input()
        if held is not None:
            return held
        free = INPUT_QUEUE_BYTES - len(self._pending)
        if len(data) > free:
            return Reason.QUEUE_FULL, f"{len(data)} bytes exceeds free queue space {free}"
        self._pending.extend(data)
        self._flush()
        return None

    def wants_write(self) -> bool:
        return bool(self._pending)

    def _flush(self) -> None:
        # Bounded slice per call; the rest waits for the next pump, in order.
        deadline = time.monotonic() + SHELL_FLUSH_SLICE_SECONDS
        while self._pending and time.monotonic() < deadline:
            try:
                if not select.select([], [self._master_fd], [], 0)[1]:
                    return
            except OSError:
                return
            chunk = bytes(self._pending[:SHELL_WRITE_CHUNK_BYTES])
            try:
                self.shell.send_user(chunk)
            except (OSError, UnsafeShellState) as exc:
                # The shell boundary decided; queued bytes are withheld, never retried.
                self.dropped_input_bytes += len(self._pending)
                self.last_input_problem = f"{type(exc).__name__}: {exc}"
                self._pending.clear()
                return
            del self._pending[:len(chunk)]

    def resize(self, rows: int, cols: int) -> None:
        transport = self.shell._transport
        if not transport._closed:
            _set_winsize(transport.master_fd, rows, cols)
        self.size = (rows, cols)

    def shell_state(self) -> dict[str, Any]:
        state = self.state
        life = state["lifecycle"]
        return {"kind": self.choice.kind, "executable": self.choice.executable,
                "parent": ref_dict(self.ref), "generation": state["generation"],
                "input_owner": state["input_owner"], "owner_epoch": state["owner_epoch"],
                "parent_mode": state["parent_mode"], "phase": state["phase"],
                "takeover_requested": state["takeover_requested"],
                "takeover_confirmed": state["takeover_confirmed"],
                "held_reasons": list(state["held_reasons"]), "request_id": life.get("request_id"),
                "supervisor": ref_dict(self._supervisor_ref) if life.get("supervisor_pid") else None,
                "unknown": list(life.get("unknown") or ()), "error": self.error}

    def info(self) -> dict[str, Any]:
        alive = self.alive()
        return {**super().info(), "process": ref_dict(self.ref), "alive": alive,
                "exit_status": self.returncode, "input_owner": self.state["input_owner"],
                "queued_input_bytes": len(self._pending), "input_capacity": INPUT_QUEUE_BYTES,
                "manager_command_in_flight": self.request_in_flight(),
                "manager_command": None if self.closed_request is None else dict(self.closed_request),
                "restart": None if self.restart is None else dict(self.restart),
                "kill": None if self.kill_result is None else dict(self.kill_result),
                "automation_hold": self.automation_hold,
                "shell": self.shell_state()}

    def request_takeover(self) -> dict[str, Any]:
        with self.io_lock:
            self.state = self.shell.request_takeover()
            return self.shell_state()

    def confirm_takeover(self) -> dict[str, Any]:
        with self.io_lock:
            self.state = self.shell.confirm_takeover()
            return self.shell_state()

    def handoff(self) -> dict[str, Any]:
        with self.io_lock:
            if self.automation_hold is not None:
                raise UnsafeShellState(self.automation_hold)
            self._pending.clear()
            self.state = self.shell.claim_manager()
            return self.shell_state()

    # -- CW-18 approved runs (C-D65): idle-only start through HostShellPort --------
    def automation_busy(self, own_hold: str | None = None) -> str | None:
        """None when the shell is user-owned at a clean prompt with no jobs, else why not.

        Caller holds ``io_lock``. Queued user bytes, a typed but unsubmitted line,
        a job, a foreground program, a request in flight or any unknown keep it busy.
        A job is any other live (running or stopped) member of the shell's own
        session, read from /proc while the unreaped shell proves the session
        number (CW-18 F1): nothing is typed into the shell to find out. A process
        that called setsid has left the session and is not the shell's job.
        ``own_hold`` is the caller's own automation hold (it does not count).
        """
        if self._released or self.exited():
            return "host shell has exited"
        if self.automation_hold is not None and self.automation_hold != own_hold:
            return self.automation_hold
        if self.error is not None:
            return "host shell boundary unknown"
        try:
            self.state = self.shell.poll(0)
        except (OSError, UnsafeShellState) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return "host shell boundary unknown"
        state, life = self.state, self.state["lifecycle"]
        if state["input_owner"] != "user":
            return "manager owns the host shell"
        if self._pending:
            return "user input is queued for the host shell"
        if state["parent_mode"] != "manual_prompt":
            return f"host shell is not at a clean prompt (mode {state['parent_mode']})"
        busy = {"manual_jobs": "the host shell has jobs",
                "unsubmitted_or_unconsumed_input": "a line is being typed in the host shell",
                "unknown_or_manual_residue": "host shell state is unknown",
                "unsupported_hook_or_trap": "host shell hook is unsupported"}
        for reason in state["held_reasons"]:
            if reason in busy:
                return busy[reason]
        # The snapshot lifecycle is a plain dict without ``returned``: compute it like the
        # workflow's give-back (lifetime ended, input and control returned, nothing unknown).
        returned = (not life.get("unknown") and life.get("lifetime") == "ended"
                    and life.get("input_returned") is True and life.get("control_returned") is True)
        if life.get("request_id") is not None and not returned:
            return "a managed request is still in flight"
        members = self._session_members()
        if members is None:
            return "host shell identity is not provable"
        if members:
            return "the host shell has jobs (a running or stopped process in its session)"
        return None

    def _session_members(self) -> list[int] | None:
        """Live members of the shell's session other than the shell; None when unprovable.

        The unreaped shell leads the session, so its number cannot be reused
        while it is proven before and after the scan. Zombies are not jobs.
        """
        if not self._leader_proven():
            return None
        try:
            names = os.listdir("/proc")
        except OSError:
            return None
        members = []
        for name in names:
            if not name.isdecimal() or int(name) == self.pid:
                continue
            fields = _stat(int(name))
            try:
                if _live(fields) and int(fields[3]) == self.pid:
                    members.append(int(name))
            except (IndexError, ValueError):
                continue
        return members if self._leader_proven() else None

    def exported_names(self, names: Sequence[str], *, timeout: float = ENV_PROBE_SECONDS) -> set[str] | None:
        """CW-16 D-B2-1 (C-D55): which of ``names`` the shell exports NOW (the user may export after start).

        fix-02 (P3-1): nothing is typed. The managed shell's own prompt hook writes the names (never values) it
        exports at every prompt to a private file in its 0700 init directory before it emits READY
        (``managed_controller_source``), so at a user-owned idle prompt the file is the environment of that
        prompt; nothing else can change it before the next prompt. The user's terminal, ``$?``, history and
        keystrokes are untouched. A partial file (the next prompt is being drawn) is read again within
        ``timeout``. None when it cannot be observed now (the shell is not at its idle prompt, no complete file).
        """
        wanted = tuple(sorted(set(names)))
        if not wanted:
            return set()
        if any(not _ENV_NAME.fullmatch(name) for name in wanted):
            return None  # not a shell variable name: never reported as exported
        path = getattr(self.shell._transport, "env_names_path", None)
        if path is None:
            return None
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            with self.io_lock:
                if self._released or self._exited:
                    return None
                events = self.shell._transport.events
                at_prompt = (self.state.get("parent_mode") == "manual_prompt" and bool(events)
                             and events[-1] == "READY")
                present = _read_env_names(path) if at_prompt else None
            if present is not None:
                return present & set(wanted)
            if not at_prompt or time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

    def cwd(self) -> str | None:
        """The shell's current directory from /proc (never typed); None when unprovable."""
        if self._released or self.exited() or not self._leader_proven():
            return None
        try:
            path = os.readlink(f"/proc/{self.pid}/cwd")
        except OSError:
            return None
        if not os.path.isabs(path) or path.endswith(" (deleted)") or not self._leader_proven():
            return None
        return path

    def open_tap(self) -> bytearray:
        with self.io_lock:
            tap = bytearray()
            self._taps.append(tap)
            return tap

    def close_tap(self, tap: bytearray) -> None:
        with self.io_lock:
            self._taps = [item for item in self._taps if item is not tap]

    def drain_tap(self, tap: bytearray) -> bytes:
        """Bytes shown since the last call; the UI still gets every byte (backlog)."""
        with self.io_lock:
            if not (self._released or self.shell._transport._closed):
                try:
                    self._display_backlog.extend(self._take_display())
                except (OSError, UnsafeShellState):
                    pass
            value = bytes(tap)
            tap.clear()
            return value

    # -- force-kill (C-D63) ----------------------------------------------
    def _drain_quietly(self, timeout: float) -> None:
        started = time.monotonic()
        transport = self.shell._transport
        if not transport._closed:
            try:
                transport._drain(timeout)
            except (OSError, ValueError, UnsafeShellState):
                pass
        rest = timeout - (time.monotonic() - started)
        if rest > 0:
            time.sleep(rest)  # an EIO-readable master after the exit must not spin

    def _left_session(self) -> list[dict[str, Any]]:
        """Descendants of the live parent shell that moved to another session (never signalled)."""
        left = []
        for pid in sorted(self.shell._transport._descendant_pids()):
            fields = _stat(pid)
            if _live(fields) and int(fields[3]) != self.pid:
                left.append({"pid": pid, "session": int(fields[3])})
        return left

    def _signal_pins(self, pids: Sequence[int], signums: Sequence[int]) -> set[int]:
        """Send ``signums`` to each pinned member; a gone or refusing member is skipped (never raises)."""
        delivered: set[int] = set()
        for pid in pids:
            for signum in signums:
                if not self._send(pid, signum):
                    break
                delivered.add(pid)
        return delivered

    def kill(self, grace: float = SHELL_KILL_GRACE) -> dict[str, Any]:
        with self.io_lock:
            return self._kill_locked(grace)

    def _kill_locked(self, grace: float = SHELL_KILL_GRACE) -> dict[str, Any]:
        """End the parent shell and every member of its session: HUP/TERM, then KILL.

        Only pidfds pinned while the unreaped parent proves the session are
        signalled, so neither a recycled PID nor a setsid daemon is targeted.
        A member that refuses a signal (EPERM) is skipped and reported in
        ``survivors`` with its reason; the others are still signalled.
        Raises UnsafeShellState if the shell already exited, cannot be proven or
        does not end.
        """
        if self._released or self.exited():
            raise UnsafeShellState("host shell has already exited")
        if not self._leader_proven():
            raise UnsafeShellState("host shell identity is not provable; nothing signalled")
        try:
            self.state = self.shell.poll(0)
        except (OSError, UnsafeShellState) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        owner = self.state["input_owner"]
        in_flight = self.request_in_flight()
        self._unsignallable.clear()  # a retried kill tries every member again
        left = self._left_session()
        self._pending.clear()
        # The manager's command dies with the shell: it is closed as unknown before any signal.
        self._close_request("host_shell_killed")
        signalled: set[int] = set()
        tried: set[int] = set()
        deadline = time.monotonic() + grace
        while True:
            # Members forked meanwhile are still in the proven session: they get the same signals.
            self._pin_session()
            fresh = [pid for pid in self._pins if pid not in tried]
            signalled |= self._signal_pins(fresh, (signal.SIGHUP, signal.SIGTERM, signal.SIGCONT))
            tried.update(fresh)
            pending = [pid for pid in self._pins if pid not in self._unsignallable]
            if (not pending and self._leader_exited()) or time.monotonic() >= deadline:
                break
            self._drain_quietly(0.02)
        signalled |= self._kill_owned_session()  # the unreaped parent keeps every scan provable
        deadline = time.monotonic() + 1.0
        while not self._leader_exited() and time.monotonic() < deadline:
            self._drain_quietly(0.02)
        if not self._leader_exited():
            why = self._unsignallable.get(self.pid)
            raise UnsafeShellState("host shell did not end after SIGKILL"
                                   + (f" (it cannot be signalled: {why})" if why else ""))
        survivors = self._survivors()
        try:
            self.state = self.shell.snapshot()
        except OSError:
            pass
        self.poll()
        self._release_transport()
        self._release_pins()
        self.kill_result = {"at": time.time(), "input_owner": owner, "manager_owned": owner == "manager",
                            "manager_command_in_flight": in_flight,
                            "manager_command": None if self.closed_request is None else dict(self.closed_request),
                            "exit_status": self.returncode, "signalled": sorted(signalled), "survivors": survivors,
                            "left_session": left}
        return {"pane": self.pane_id.value, "killed": True, "process": ref_dict(self.ref),
                "session_id": self.session_id, "generation": self.generation, **self.kill_result}

    def _release_transport(self) -> None:
        """Close the old shell's PTY and control descriptors; never signals by number."""
        transport = self.shell._transport
        if transport._closed:
            self._released = True
            return
        transport._closed = True
        transport.boundary.fail_closed()
        transport.manual_prompt_confirmed = False
        tail = bytes(transport._display_tail)
        transport._display_tail.clear()
        if tail:
            self._final = tail
        for fd in (transport.master_fd, transport._control_fd, transport._request_fd, transport._recovery_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        transport._init_dir.cleanup()
        self._released = True

    def close(self) -> dict[str, Any]:
        with self.io_lock:
            return self._close_locked()

    def _close_locked(self) -> dict[str, Any]:
        """End the shell and its session through pinned identities, then release its PTY.

        Never raises for a member that cannot be signalled: it is reported in ``survivors``.
        """
        self._pending.clear()
        survivors: list[dict[str, Any]] = []
        if not self._released:
            if not self.exited() and self._leader_proven():
                try:
                    survivors = self.kill()["survivors"]
                except (UnsafeShellState, OSError):
                    pass  # the pinned members still get SIGKILL below
            if not self._released:
                # Exited shell: members left in its session were pinned at the exit.
                self._kill_owned_session()
                self.poll()
                survivors = self._survivors()
                self._release_transport()
        self._release_pins()
        return {"pane": self.pane_id.value, "exit_status": self.returncode, "survivors": survivors}


class HostShellPort:
    """The product host shell as ``TaskWorkflow.start(shell=...)`` (CW-18 U2, C-D65).

    It exposes the ``PersistentShell`` surface the workflow uses, under the
    pane's ``io_lock``, so the backend loop and the run never use the shell at
    the same time. Display bytes are tee'd: the run's raw log gets a copy and
    the UI still receives every byte. ``hold`` starts a run only when the shell
    is user-owned at a clean prompt with no jobs and then refuses user input
    until ``release_hold`` (typed keystrokes are never overwritten or mixed).
    The port never closes the shell; ``detach`` only drops its display tap.
    """

    def __init__(self, pane: ShellPane, current: Callable[[], ShellPane | None]):
        self._pane = pane
        self._current = current
        self.choice = pane.choice
        self._tap: bytearray | None = pane.open_tap()

    def _check(self) -> None:
        if self._current() is not self._pane:
            raise UnsafeShellState("host shell was replaced")
        if self._pane.exited():
            raise UnsafeShellState("host shell has exited")

    def busy(self) -> str | None:
        with self._pane.io_lock:
            if self._current() is not self._pane:
                return "host shell was replaced"
            return self._pane.automation_busy()

    def hold(self, reason: str) -> str | None:
        """Atomically: None and input held when idle, else the busy reason (nothing held)."""
        with self._pane.io_lock:
            busy = self.busy()
            if busy is None:
                self._pane.automation_hold = reason
            return busy

    def release_hold(self, reason: str | None = None) -> None:
        """Release the hold (only when it is ``reason``, if given)."""
        with self._pane.io_lock:
            if reason is None or self._pane.automation_hold == reason:
                self._pane.automation_hold = None

    def hold_return(self, reason: str) -> bool:
        """Hold user input while a finished run's shell comes back (no busy check: the manager owns it)."""
        with self._pane.io_lock:
            if self._current() is not self._pane or self._pane.exited() or self._pane.automation_hold is not None:
                return False
            self._pane.automation_hold = reason
            return True

    def cwd(self) -> str | None:
        with self._pane.io_lock:
            if self._current() is not self._pane:
                return None
            return self._pane.cwd()

    def restore_cwd(self, target: str, inside: Path | str, *, reason: str, idle_wait: float = 0.0,
                    confirm_wait: float = 3.0) -> str:
        """Type ``cd -- <target>`` back into the user's idle shell when a run left it in ``inside``.

        CW-18 F2: only while the shell's current directory (from /proc) is still
        under ``inside`` (the Workbench worktrees); a user who already moved is
        never moved again. It types only at an idle user-owned prompt with no
        job (the same rule as a run's start) and holds user input until the
        shell's directory is ``target`` and the prompt is back. Returns
        ``restored``, ``unchanged``, ``user_moved``, ``target_missing``,
        ``shell_gone`` (final) or ``busy``, ``unknown``, ``unconfirmed`` (try
        again later). The hold ``reason`` (the caller's own, possibly already
        set) is released here; no other hold is touched.
        """
        pane, inside = self._pane, os.path.realpath(inside)
        deadline = time.monotonic() + max(idle_wait, 0.0)
        typed_at: float | None = None
        try:
            while True:
                with pane.io_lock:
                    if self._current() is not pane or pane.exited():
                        return "shell_gone"
                    current = pane.cwd()
                    if typed_at is not None:
                        pane.state = pane.shell.poll(0)
                        if current == target and pane.state["parent_mode"] == "manual_prompt":
                            return "restored"
                        if time.monotonic() >= typed_at + confirm_wait:
                            return "unconfirmed"
                    elif current is None:
                        return "unknown"
                    elif current == target:
                        return "unchanged"
                    elif os.path.commonpath([current, inside]) != inside:
                        return "user_moved"
                    elif not os.path.isdir(target):
                        return "target_missing"
                    elif pane.automation_busy(own_hold=reason) is None:
                        pane.automation_hold = reason
                        builtin = "builtin " if self.choice.kind == "bash" else ""
                        # A leading space keeps it out of a HISTCONTROL=ignorespace history.
                        pane.shell.send_user(f" {builtin}cd -- {shlex.quote(target)}\n".encode())
                        typed_at = time.monotonic()
                    elif time.monotonic() >= deadline:
                        return "busy"
                time.sleep(0.02)
        except (OSError, UnsafeShellState):
            return "unconfirmed" if typed_at is not None else "unknown"
        finally:
            self.release_hold(reason)

    def send_user(self, data: bytes) -> None:
        with self._pane.io_lock:
            self._check()
            if self._pane.automation_hold is None or self._pane._pending:
                raise UnsafeShellState("host shell input is not held for this run")
            self._pane.shell.send_user(data)

    def poll(self, timeout: float = 0) -> dict[str, Any]:
        if timeout > 0:
            time.sleep(timeout)  # never block the backend loop on the shell lock
        with self._pane.io_lock:
            self._check()
            self._pane.state = self._pane.shell.poll(0)
            return self._pane.state

    def snapshot(self) -> dict[str, Any]:
        with self._pane.io_lock:
            return self._pane.shell.snapshot()

    def claim_manager(self) -> dict[str, Any]:
        with self._pane.io_lock:
            self._check()
            self._pane.state = self._pane.shell.claim_manager()
            return self._pane.state

    def submit(self, control: dict, command: str | list[str], automation: dict, **kwargs: Any) -> dict[str, Any]:
        with self._pane.io_lock:
            self._check()
            self._pane.state = self._pane.shell.submit(control, command, automation, **kwargs)
            return self._pane.state

    def release_input(self) -> None:
        with self._pane.io_lock:
            self._check()
            self._pane.shell.release_input()

    def request_takeover(self) -> dict[str, Any]:
        """Give the shell back to the user after the run returned (the UI's takeover request)."""
        with self._pane.io_lock:
            self._check()
            return self._pane.request_takeover()

    def display_bytes(self) -> bytes:
        if self._tap is None:
            return b""
        return self._pane.drain_tap(self._tap)

    def detach(self) -> None:
        if self._tap is not None:
            self._pane.close_tap(self._tap)
            self._tap = None
