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
import pty
import select
import signal
import struct
import termios
import time
from typing import Any, Mapping, Sequence

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
DEFAULT_SIZE = (30, 100)


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


class OmpPane(Pane):
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
        os.set_blocking(master, False)
        # The unreaped child is ours; its start ticks prove session ownership later.
        self.ref = process_ref(f"{role}_omp", pid)

    def fds(self) -> list[int]:
        # After EOF/EIO a PTY master stays readable; stop selecting on it.
        return [self.master_fd] if self.master_fd >= 0 and not self._eof else []

    def _leader_exited(self) -> bool:
        """Whether the OMP child exited; never reaps, so a zombie keeps its numbers."""
        if self._reaped:
            return True
        try:
            return os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            self._reaped = True  # not ours to reap any more; its numbers prove nothing
            return True

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

    # -- session ownership (C-D45) ---------------------------------------
    def _leader_proven(self) -> bool:
        """The OMP child is unreaped and still ours: ppid is the backend, start ticks match."""
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

    def _release_pins(self) -> None:
        for fd, _ in self._pins.values():
            os.close(fd)
        self._pins.clear()

    def _pin_session(self) -> None:
        """Pin every live member of this OMP's session while the number is provably ours.

        Proof is the unreaped OMP leader, or a pinned member that is live and in
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
        self.poll()
        return chunks

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

    def close(self, grace: float = 3.0) -> dict[str, Any]:
        """TERM the proven OMP group, then KILL every proven member left in its session."""
        if not self._leader_exited() and self._leader_proven():
            # Unreaped child of this backend: its group number is still ours.
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + grace
            while not self._leader_exited() and time.monotonic() < deadline:
                self._drain_quietly(0.05)
        self._kill_owned_session()
        deadline = time.monotonic() + 2.0
        while self.poll() is None and time.monotonic() < deadline:
            self._drain_quietly(0.02)
        self._release_pins()
        if self.master_fd >= 0:
            os.close(self.master_fd)
            self.master_fd = -1
        self._pending.clear()
        return {"pane": self.pane_id.value, "exit_status": self.returncode}

    def _kill_owned_session(self) -> None:
        """KILL remaining members of this OMP's own session; skip when unprovable.

        Signals go only through pinned pidfds. The unreaped leader keeps the
        session provable across rounds; once it is reaped, one pinned member is
        spared per round as the anchor that proves the next scan, and is killed
        last when it is the only member left.
        """
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self._pin_session()
            if not self._pins:
                return
            keep = None
            if not self._leader_proven() and len(self._pins) > 1:
                keep = next(iter(self._pins))
            targets = [fd for pid, (fd, _) in self._pins.items() if pid != keep]
            for fd in targets:
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            settle = min(deadline, time.monotonic() + 0.5)
            while any(not _pidfd_exited(fd) for fd in targets) and time.monotonic() < settle:
                self._drain_quietly(0.01)

    def _drain_quietly(self, timeout: float) -> None:
        try:
            if select.select([self.master_fd], [], [], timeout)[0]:
                os.read(self.master_fd, READ_CHUNK_BYTES)
        except OSError:
            time.sleep(timeout)


class ShellPane(Pane):
    """Backend ownership of the CW-07 PersistentShell; input goes through its boundary."""

    def __init__(self, choice: ShellChoice, environment: dict[str, str],
                 *, size: tuple[int, int] = DEFAULT_SIZE):
        super().__init__(PaneId.HOST_SHELL, size)
        self.choice = choice
        self.shell = PersistentShell(user_environment=environment, choice=choice)
        self._pending = bytearray()
        self.ref = process_ref("host_shell", self.shell.parent_pid)
        self._supervisor_ref: ProcessRef | None = None
        self.state: dict[str, Any] = self.shell.snapshot()
        self.error: str | None = None
        self._exited = False
        self._last_alive_check = 0.0
        self.resize(*size)

    @property
    def _master_fd(self) -> int:
        return self.shell._transport.master_fd

    def alive(self) -> bool:
        return self.ref is not None and LinuxProcessProbe().observe(self.ref).state == "alive"

    def fds(self) -> list[int]:
        transport = self.shell._transport
        if transport._closed or self._exited:
            return []
        return [transport.master_fd, transport._control_fd]

    def pump(self) -> list[DisplayChunk]:
        if self.shell._transport._closed:
            return []
        now = time.monotonic()
        if not self._exited and now - self._last_alive_check >= 0.5:
            self._last_alive_check = now
            self._exited = not self.alive()
        try:
            self.state = self.shell.poll(0)
            self._flush()
            data = self.shell.display_bytes()
        except (OSError, UnsafeShellState) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = self.shell.snapshot()
            return []
        supervisor = self.state["lifecycle"].get("supervisor_pid")
        if supervisor and (self._supervisor_ref is None or self._supervisor_ref.pid != supervisor):
            self._supervisor_ref = process_ref("supervisor", supervisor)
        return [self._chunk(data)] if data else []

    def _accepts_manual_input(self) -> tuple[Reason, str] | None:
        state = self.state
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
        return {**super().info(), "process": ref_dict(self.ref), "alive": self.alive(), "exit_status": None,
                "input_owner": self.state["input_owner"], "queued_input_bytes": len(self._pending),
                "input_capacity": INPUT_QUEUE_BYTES, "shell": self.shell_state()}

    def request_takeover(self) -> dict[str, Any]:
        self.state = self.shell.request_takeover()
        return self.shell_state()

    def confirm_takeover(self) -> dict[str, Any]:
        self.state = self.shell.confirm_takeover()
        return self.shell_state()

    def handoff(self) -> dict[str, Any]:
        self._pending.clear()
        self.state = self.shell.claim_manager()
        return self.shell_state()

    def close(self) -> dict[str, Any]:
        self._pending.clear()
        self.shell.close()
        return {"pane": self.pane_id.value}
