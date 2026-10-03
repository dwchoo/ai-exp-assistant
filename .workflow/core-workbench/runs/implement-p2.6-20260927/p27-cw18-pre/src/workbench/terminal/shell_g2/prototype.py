"""Small real-PTY shell control prototype for the CW-03 G2 gate.

This is a feasibility harness, not the production terminal adapter. It keeps
display bytes and lifecycle events on separate channels and serializes every
write to the PTY master through one owner gate.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import os
import pty
import array
import select
import secrets
import shlex
import shutil
import signal
import tempfile
import threading
import time
from typing import Literal


ShellKind = Literal["bash", "sh"]
InputOwner = Literal["manager", "user"]


class ShellUnavailable(RuntimeError):
    """Neither supported shell can be selected from the supplied PATH."""


class UnsafeShellState(RuntimeError):
    """The shell is not at a verified clean boundary for automatic input."""


@dataclass(frozen=True, slots=True)
class ShellChoice:
    kind: ShellKind
    executable: str


def select_shell(path: str | None = None) -> ShellChoice:
    """Choose Bash first, then sh; a returned choice never changes in-session."""
    bash = shutil.which("bash", path=path)
    if bash:
        return ShellChoice("bash", os.path.realpath(bash))
    shell = shutil.which("sh", path=path)
    if shell:
        return ShellChoice("sh", os.path.realpath(shell))
    raise ShellUnavailable("Workbench needs Bash or sh on PATH")


class InputBoundary:
    """Conservative owner/dirty-line guard around one PTY master writer.

    User input is only trustworthy here when it is routed through this object.
    Unknown editing controls and paste framing latch the state dirty. A manual
    line is considered consumed only after the shell's control channel reports
    a fresh ready event for it.

    ``uncertain`` only holds automatic dispatch and manager return; it never
    decides whether manual bytes may reach the PTY. Bytes observed through
    ``observe_user_bytes`` (target unknown) keep the latch for the session.
    Bytes observed through ``observe_manual_bytes`` carry the write-time PTY
    foreground target, so their line-edit uncertainty is cleared by the next
    control-channel READY that consumes every counted line with no pending,
    edited or foreground-program tail left. Timers and output never clear it.
    """

    def __init__(self, generation: int) -> None:
        if generation < 1:
            raise ValueError("generation must be positive")
        self.generation = generation
        self.owner: InputOwner = "manager"
        self.owner_epoch = 1
        self.input_sequence = 0
        self.ready = False
        self.dirty = True
        self.pending_line = bytearray()
        self.submitted_lines = 0
        self.uncertain = False
        # Line-scoped sources of ``uncertain`` (see observe_manual_bytes).
        self._line_uncertain = False
        self._residue_open = False
        self._sticky_uncertain = False
        self.active_command: str | None = None
        self.needs_review = False
        # The control FD reached EOF: shell state is no longer observable.
        self.control_lost = False
        self.last_exit_code: int | None = None
        self._job_probe = False
        self._job_pids: list[int] = []
        self._handoff_required = False
        self._handoff_line = False
        self._handoff_seen = False
        self._handoff_hook_checked = False
        self._handoff_jobs_checked = False
        self._handoff_ready = False
        self.shell_pid: int | None = None
        self.lock = threading.RLock()

    def owner_change(self, owner: InputOwner) -> int:
        with self.lock:
            if owner != self.owner:
                self.owner = owner
                self.owner_epoch += 1
                if owner == "user":
                    self._handoff_required = True
                    self._handoff_line = False
                    self._handoff_seen = False
                    self._handoff_hook_checked = False
                    self._handoff_jobs_checked = False
                    self._handoff_ready = False
            if owner == "manager":
                self._reconcile_clean()
            return self.owner_epoch

    def observe_user_bytes(self, data: bytes) -> int:
        with self.lock:
            if self.owner != "user":
                raise UnsafeShellState("user does not own terminal input")
            if not data:
                return self.input_sequence
            self.input_sequence += 1
            self.dirty = True
            self.ready = False
            for byte in data:
                if byte in (0x0A, 0x0D):
                    self._handoff_line = bytes(self.pending_line).strip() == b"wb-handoff"
                    self._handoff_seen = False
                    self._handoff_hook_checked = False
                    self._handoff_jobs_checked = False
                    self._handoff_ready = False
                    self.pending_line.clear()
                    self.submitted_lines += 1
                elif byte in (0x08, 0x7F, 0x15, 0x17):
                    # Readline, canonical erase and word/line kill semantics
                    # vary. Keep the latch dirty until an explicit safe event.
                    self.uncertain = self._sticky_uncertain = True
                    if byte == 0x15:
                        self.pending_line.clear()
                    elif self.pending_line:
                        self.pending_line.pop()
                elif byte == 0x1B or byte < 0x20:
                    self.uncertain = self._sticky_uncertain = True
                    self.pending_line.append(byte)
                else:
                    self.pending_line.append(byte)
                if byte not in (0x0A, 0x0D):
                    self._handoff_ready = False
            return self.input_sequence

    def observe_manual_bytes(self, data: bytes, *, shell_foreground: bool) -> int:
        """Account bytes already written to an observed manual PTY target.

        ``shell_foreground`` is the write-time foreground process group: true
        when it was the parent shell itself (prompt/line editor), false when it
        was a program the user started. Program input is never counted as a
        shell line; it only holds automation until a later fresh READY, and an
        unterminated tail may still prefix the shell's next line.
        """
        with self.lock:
            if self.owner != "user":
                raise UnsafeShellState("user does not own terminal input")
            if not data:
                return self.input_sequence
            self.input_sequence += 1
            self.dirty = True
            self.ready = False
            self._handoff_ready = False
            if not shell_foreground:
                self.uncertain = True
                for byte in data:
                    if byte in (0x0A, 0x0D, 0x03, 0x1A, 0x1C):
                        # Line end, or an ISIG key that flushes the tty queue.
                        self._residue_open = False
                    elif byte != 0x04:
                        # EOF keeps the prior state: it ends an empty canonical
                        # read, but pushes a partial line without a newline.
                        self._residue_open = True
                return self.input_sequence
            if self._residue_open:
                self._line_uncertain = self.uncertain = True
                self._residue_open = False
            for byte in data:
                if byte in (0x0A, 0x0D):
                    self._handoff_line = (bytes(self.pending_line).strip() == b"wb-handoff"
                                          and not self._line_uncertain)
                    self._handoff_seen = False
                    self._handoff_hook_checked = False
                    self._handoff_jobs_checked = False
                    self.pending_line.clear()
                    self.submitted_lines += 1
                    self._line_uncertain = False
                elif byte == 0x03:
                    # SIGINT to the foreground shell discards its current line
                    # and redisplays the prompt (one READY). While counted lines
                    # are outstanding, their own READY covers the interrupt.
                    self.uncertain = True
                    self._handoff_line = False
                    self._line_uncertain = False
                    self.pending_line.clear()
                    if self.submitted_lines == 0:
                        self.submitted_lines = 1
                elif byte in (0x08, 0x7F, 0x15, 0x17):
                    self.uncertain = self._line_uncertain = True
                    if byte == 0x15:
                        self.pending_line.clear()
                    elif self.pending_line:
                        self.pending_line.pop()
                elif byte == 0x1B or byte < 0x20:
                    self.uncertain = self._line_uncertain = True
                    self.pending_line.append(byte)
                else:
                    self.pending_line.append(byte)
            return self.input_sequence

    def observe_run_bytes(self, data: bytes) -> int:
        """Account bytes written to a confirmed automation foreground run.

        They are the experiment's input, not parent line edits: the parent is
        in control wait and the supervisor's input barrier plus the takeover
        flush discard any unread remainder before the parent reads its PTY.
        """
        with self.lock:
            if self.owner != "user":
                raise UnsafeShellState("user does not own terminal input")
            if not data:
                return self.input_sequence
            self.input_sequence += 1
            self.dirty = True
            self._handoff_ready = False
            return self.input_sequence

    def observe_event(self, event: str) -> None:
        with self.lock:
            if event.startswith("CONTROL_READY:"):
                # A verified handoff remains inside the parent controller;
                # there is deliberately no interactive prompt READY here.
                if (
                    event == f"CONTROL_READY:{self.shell_pid}"
                    and self._handoff_seen
                    and self._handoff_hook_checked
                    and self._handoff_jobs_checked
                    and self.submitted_lines == 1
                    and not self.pending_line
                    and not self.uncertain
                    and not self.needs_review
                    and self.active_command is None
                ):
                    self.submitted_lines = 0
                    self.ready = True
                    self._handoff_ready = True
                    self._reconcile_clean()
                return
            if event == "READY":
                self.ready = True
                if self.submitted_lines:
                    self.submitted_lines -= 1
                if self.active_command is not None:
                    # A prompt returned before the command lifecycle finished.
                    self.needs_review = True
                    self.active_command = None
                if (
                    self.uncertain
                    and not self._sticky_uncertain
                    and self.submitted_lines == 0
                    and not self.pending_line
                    and not self._line_uncertain
                    and not self._residue_open
                ):
                    # Fresh prompt after every counted line with nothing typed
                    # since: the edited/interrupted lines were consumed.
                    self.uncertain = False
                self._reconcile_clean()
                return
            if event.startswith("START:"):
                self.active_command = event.partition(":")[2]
                self.ready = False
                return
            if event.startswith("JOBS_BEGIN:"):
                self._job_probe = True
                self._job_pids.clear()
                return
            if event == "HOOK_OK:HANDOFF" and self._handoff_seen:
                self._handoff_hook_checked = True
                return
            if event.startswith("JOBS_END:"):
                if self._job_probe and self._job_pids:
                    self.needs_review = True
                if self._job_probe and event == "JOBS_END:HANDOFF" and self._handoff_seen:
                    self._handoff_jobs_checked = True
                if self._job_probe and event == "JOBS_END:READY" and self._handoff_seen:
                    if (
                        self.ready
                        and self._handoff_hook_checked
                        and self._handoff_jobs_checked
                        and not self.needs_review
                    ):
                        self._handoff_ready = True
                        self._reconcile_clean()
                self._job_probe = False
                return
            if self._job_probe and event.isdecimal():
                self._job_pids.append(int(event))
                return
            if event.startswith("DONE:"):
                parts = event.split(":", 2)
                if len(parts) == 3 and parts[2].lstrip("-").isdecimal():
                    self.last_exit_code = int(parts[2])
                    self.active_command = None
                    if self.last_exit_code != 0:
                        self.needs_review = True
                return
            if event.startswith("HANDOFF:"):
                reported_pid = event.partition(":")[2]
                if (
                    self.owner == "user"
                    and self._handoff_line
                    and not self.pending_line
                    and not self.uncertain
                    and self.shell_pid is not None
                    and reported_pid == str(self.shell_pid)
                ):
                    self._handoff_seen = True
                return
            if event in {"SHELL_EXIT", "HOOK_LOST"}:
                self.needs_review = True
                self.ready = False
                return

    def fail_closed(self) -> None:
        with self.lock:
            self.needs_review = True
            self.ready = False

    def can_dispatch(self, *, generation: int, owner_epoch: int) -> bool:
        with self.lock:
            return (
                generation == self.generation
                and owner_epoch == self.owner_epoch
                and self.owner == "manager"
                and self.ready
                and not self.dirty
                and not self.pending_line
                and self.submitted_lines == 0
                and not self.uncertain
                and self.active_command is None
                and not self.needs_review
                and (not self._handoff_required or self._handoff_ready)
            )

    def _reconcile_clean(self) -> None:
        if (
            self.owner == "manager"
            and self.ready
            and not self.pending_line
            and self.submitted_lines == 0
            and not self.uncertain
            and self.active_command is None
            and not self.needs_review
            and (not self._handoff_required or self._handoff_ready)
        ):
            self.dirty = False


class ShellProcess:
    """Interactive shell child controlled through one PTY master and FD 9."""

    CONTROL_FD = 9

    def __init__(self, choice: ShellChoice | None = None, *, control_wait: bool = False,
                 environment: dict[str, str] | None = None) -> None:
        if not sys_platform_linux():
            raise OSError("CW-03 shell prototype requires Linux PTY semantics")
        self.choice = choice or select_shell()
        self.control_wait = control_wait
        self.generation = 1
        self.boundary = InputBoundary(self.generation)
        self.pid: int
        self.master_fd: int
        self._control_fd: int
        self._control_tail = bytearray()
        self._events: list[str] = []
        self._event_cursor = 0
        self._display_tail = bytearray()
        self._closed = False
        self._job_pids: list[int] = []
        self._recovery_token = secrets.token_hex(16) if control_wait else ""
        self._init_dir = tempfile.TemporaryDirectory(prefix="cw03-g2-")
        self._init_path = os.path.join(self._init_dir.name, "shell-init")
        with open(self._init_path, "w", encoding="utf-8") as init_file:
            init_file.write((
                self._control_init_source()
                if control_wait else (_bash_init() if self.choice.kind == "bash" else _sh_init())
            ).replace("__CW_RECOVERY_TOKEN__", self._recovery_token))
        read_fd, write_fd = os.pipe()
        request_read, request_write = os.pipe()
        recovery_read, recovery_write = os.pipe()
        pid, master = pty.fork()
        if pid == 0:  # pragma: no cover - exercised by the parent-side tests
            try:
                # Source descriptors can themselves be 7, 8 or 9 when the
                # caller has other files open. Duplicate first, then assign.
                request_source = fcntl.fcntl(request_read, fcntl.F_DUPFD_CLOEXEC, 11)
                recovery_source = fcntl.fcntl(recovery_read, fcntl.F_DUPFD_CLOEXEC, 11)
                control_source = fcntl.fcntl(write_fd, fcntl.F_DUPFD_CLOEXEC, 11)
                for fd in (read_fd, write_fd, request_read, request_write,
                           recovery_read, recovery_write):
                    os.close(fd)
                os.dup2(request_source, 8)
                os.close(request_source)
                os.set_inheritable(8, True)
                os.dup2(recovery_source, 7)
                os.close(recovery_source)
                os.set_inheritable(7, True)
                os.dup2(control_source, self.CONTROL_FD)
                os.close(control_source)
                os.set_inheritable(self.CONTROL_FD, True)
                child_env = os.environ.copy() if environment is None else dict(environment)
                child_env["TERM"] = child_env.get("TERM", "dumb")
                if self.choice.kind == "bash":
                    argv = [self.choice.executable, "--noprofile", "--rcfile", self._init_path, "-i"]
                else:
                    child_env["ENV"] = self._init_path
                    argv = [self.choice.executable, "-i"]
                # Python ignores SIGPIPE/SIGXFSZ; the user shell starts with defaults.
                signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
                os.execve(self.choice.executable, argv, child_env)
            except BaseException as exc:
                try:
                    os.write(self.CONTROL_FD, f"EXEC_ERROR:{type(exc).__name__}\n".encode())
                finally:
                    os._exit(127)
        os.close(write_fd)
        os.close(request_read)
        os.close(recovery_read)
        self.pid, self.master_fd, self._control_fd = pid, master, read_fd
        self._request_fd = request_write
        self._recovery_fd = recovery_write
        self.boundary.shell_pid = pid
        os.set_blocking(self.master_fd, False)
        os.set_blocking(self._control_fd, False)

    @property
    def owner_epoch(self) -> int:
        return self.boundary.owner_epoch

    def _control_init_source(self) -> str:
        return _bash_control_init() if self.choice.kind == "bash" else _sh_control_init()

    @property
    def events(self) -> tuple[str, ...]:
        return tuple(self._events)

    def wait_event(self, expected: str, timeout: float = 2.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain(timeout=min(0.03, deadline - time.monotonic()))
            while self._event_cursor < len(self._events):
                event = self._events[self._event_cursor]
                self._event_cursor += 1
                if event == expected or event.startswith(expected):
                    return event
        self.boundary.fail_closed()
        raise TimeoutError(f"control event not received: {expected}")

    def wait_ready(self, timeout: float = 2.0) -> None:
        self.wait_event("READY", timeout)
        if self.boundary._handoff_seen and self.boundary._handoff_hook_checked:
            self.wait_event("JOBS_END:READY", timeout)

    def wait_foreground_child(self, timeout: float = 2.0) -> None:
        shell_group = os.getpgid(self.pid)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                foreground = array.array("i", [0])
                fcntl.ioctl(self.master_fd, 0x540F, foreground, True)  # TIOCGPGRP
                if foreground[0] != shell_group:
                    return
            except OSError:
                break
            self._drain(timeout=0.01)
        raise TimeoutError("no foreground child process group appeared")

    def take_user_control(self) -> int:
        return self.boundary.owner_change("user")

    def send_user(self, data: bytes) -> None:
        with self.boundary.lock:
            self._drain(timeout=0)
            self.boundary.observe_user_bytes(data)
            self._write_all(data)

    def return_to_manager(self) -> int:
        self._drain(timeout=0)
        self._detect_nested_shell()
        return self.boundary.owner_change("manager")

    def dispatch(self, command_id: str, script: str, *, expected_epoch: int) -> None:
        if not command_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in command_id):
            raise ValueError("command_id must be a simple stable token")
        with self.boundary.lock:
            if not self.boundary.can_dispatch(generation=self.generation, owner_epoch=expected_epoch):
                raise UnsafeShellState("automatic input is not at a verified clean boundary")
            line = f"__wb_run {command_id} {shlex.quote(script)}\n".encode()
            self._write_all(line)
            self.boundary.dirty = True
            self.boundary.ready = False
            self.boundary.active_command = command_id

    def wait_command(self, command_id: str, timeout: float = 2.0) -> tuple[str, ...]:
        done = f"DONE:{command_id}:"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain(timeout=min(0.03, deadline - time.monotonic()))
            if any(event.startswith(done) for event in self._events):
                self.wait_event("READY", max(0.01, deadline - time.monotonic()))
                return tuple(self._events)
            self._detect_nested_shell()
        self.boundary.fail_closed()
        raise TimeoutError(f"command lifecycle incomplete: {command_id}")

    def display_bytes(self) -> bytes:
        self._drain(timeout=0)
        value = bytes(self._display_tail)
        self._display_tail.clear()
        return value

    def _write_all(self, data: bytes) -> None:
        view = memoryview(data)
        offset = 0
        try:
            while offset < len(view):
                try:
                    offset += os.write(self.master_fd, view[offset:])
                except BlockingIOError:
                    select.select([], [self.master_fd], [], 0.1)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        self.boundary.fail_closed()
                    raise
        finally:
            view.release()

    def _drain(self, timeout: float) -> None:
        ready, _, _ = select.select([self.master_fd, self._control_fd], [], [], max(0.0, timeout))
        if self.master_fd in ready:
            try:
                data = os.read(self.master_fd, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                data = b""
            if data:
                self._display_tail.extend(data)
        if self._control_fd in ready:
            try:
                data = os.read(self._control_fd, 65536)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
                data = b""
            if not data:
                self.boundary.control_lost = True
                self.boundary.observe_event("HOOK_LOST")
                return
            self._control_tail.extend(data)
            while b"\n" in self._control_tail:
                raw, _, rest = self._control_tail.partition(b"\n")
                self._control_tail[:] = rest
                event = raw.decode("utf-8", "replace")
                self._events.append(event)
                self.boundary.observe_event(event)
                if self.control_wait:
                    self._on_control_event(event)

    def _on_control_event(self, event: str) -> None:
        """Control-wait probes can classify lifecycle after receiving an event."""

    def _detect_nested_shell(self) -> None:
        foreground_group = self._foreground_group()
        if foreground_group is None:
            return
        for pid in self._descendant_pids():
            try:
                with open(f"/proc/{pid}/stat", encoding="ascii") as stream:
                    stat = stream.read().split(") ", 1)[1].split()
                process_group = int(stat[2])
                session_id = int(stat[3])
                executable = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
            except (OSError, ValueError, IndexError):
                continue
            if session_id == self.pid and process_group == foreground_group and executable in {"bash", "dash", "sh"}:
                self.boundary.fail_closed()

    def _foreground_group(self) -> int | None:
        try:
            foreground = array.array("i", [0])
            fcntl.ioctl(self.master_fd, 0x540F, foreground, True)  # TIOCGPGRP
        except OSError:
            return None
        return foreground[0]

    def _descendant_pids(self) -> set[int]:
        descendants: set[int] = set()
        pending = [self.pid]
        while pending:
            parent = pending.pop()
            try:
                with open(f"/proc/{parent}/task/{parent}/children", encoding="ascii") as stream:
                    children = [int(item) for item in stream.read().split()]
            except (OSError, ValueError):
                continue
            for child in children:
                if child not in descendants:
                    descendants.add(child)
                    pending.append(child)
        return descendants

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Kill each process group in this PTY session, including shell jobs.
        groups: set[int] = set()
        try:
            names = os.listdir("/proc")
        except OSError:
            names = []
        for name in names:
            if not name.isdecimal():
                continue
            try:
                with open(f"/proc/{name}/stat", encoding="ascii") as stream:
                    fields = stream.read().split(") ", 1)[1].split()
                process_group, session_id = int(fields[2]), int(fields[3])
            except (OSError, ValueError, IndexError):
                continue
            if session_id == self.pid:
                groups.add(process_group)
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                waited, _ = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                break
            if waited == self.pid:
                break
            time.sleep(0.01)
        for fd in (self.master_fd, self._control_fd, self._request_fd, self._recovery_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        self._init_dir.cleanup()

    def __enter__(self) -> ShellProcess:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def sys_platform_linux() -> bool:
    return os.name == "posix" and os.path.isdir("/proc") and os.path.exists("/dev/ptmx")


def _bash_init() -> str:
    return r'''__wb_emit() {
    if [[ $1 == READY ]] && { [[ $(declare -p PROMPT_COMMAND) != declare\ --\ PROMPT_COMMAND=* ]] || [[ $PROMPT_COMMAND != '__wb_emit READY; [[ $PROMPT_COMMAND == "$__wb_prompt_value" && $PS1 == \$\  ]] || builtin printf "HOOK_LOST\n" >&9; builtin printf "JOBS_BEGIN:READY\n" >&9; builtin jobs -p >&9; builtin printf "JOBS_END:READY\n" >&9' ]]; }; then
        printf '%s\n' HOOK_LOST >&9
    fi
    printf '%s\n' "$*" >&9
}
PROMPT_COMMAND='__wb_emit READY; [[ $PROMPT_COMMAND == "$__wb_prompt_value" && $PS1 == \$\  ]] || builtin printf "HOOK_LOST\n" >&9; builtin printf "JOBS_BEGIN:READY\n" >&9; builtin jobs -p >&9; builtin printf "JOBS_END:READY\n" >&9'
__wb_prompt_value=$PROMPT_COMMAND
PS1='$ '
trap '__wb_emit SHELL_EXIT' EXIT
wb-handoff() {
    read -r __wb_handoff_pid __wb_handoff_tail < /proc/self/stat || return 1
    __wb_emit "HANDOFF:$__wb_handoff_pid"
    __wb_emit JOBS_BEGIN:HANDOFF
    jobs -p >&9
    __wb_emit JOBS_END:HANDOFF
    [[ $(declare -p PROMPT_COMMAND) == declare\ --\ PROMPT_COMMAND=* && $PROMPT_COMMAND == '__wb_emit READY; [[ $PROMPT_COMMAND == "$__wb_prompt_value" && $PS1 == \$\  ]] || builtin printf "HOOK_LOST\n" >&9; builtin printf "JOBS_BEGIN:READY\n" >&9; builtin jobs -p >&9; builtin printf "JOBS_END:READY\n" >&9' && $PS1 == '$ ' ]] || return 1
    __wb_emit HOOK_OK:HANDOFF
}
__wb_run() {
    local __wb_id=$1 __wb_script=$2 __wb_status
    __wb_emit "START:$__wb_id"
    eval "$__wb_script"
    __wb_status=$?
    __wb_emit "JOBS_BEGIN:$__wb_id"
    jobs -p >&9
    __wb_emit "JOBS_END:$__wb_id"
    __wb_emit "DONE:$__wb_id:$__wb_status"
    return "$__wb_status"
}
'''


def _sh_init() -> str:
    return r'''__wb_emit() { printf '%s\n' "$*" >&9; }
__wb_ready() {
    [ "$PS1" = '$( __wb_ready; command printf "JOBS_BEGIN:READY\n" >&9; jobs -p >&9; command printf "JOBS_END:READY\n" >&9; printf "$ " )' ] || __wb_emit HOOK_LOST
    __wb_emit READY
}
PS1='$( __wb_ready; command printf "JOBS_BEGIN:READY\n" >&9; jobs -p >&9; command printf "JOBS_END:READY\n" >&9; printf "$ " )'
trap '__wb_emit SHELL_EXIT' EXIT
alias wb-handoff='__wb_handoff'
__wb_handoff() {
    read -r __wb_handoff_pid __wb_handoff_tail < /proc/self/stat || return 1
    __wb_emit "HANDOFF:$__wb_handoff_pid"
    __wb_emit JOBS_BEGIN:HANDOFF
    jobs -p >&9
    __wb_emit JOBS_END:HANDOFF
    [ "$PS1" = '$( __wb_ready; command printf "JOBS_BEGIN:READY\n" >&9; jobs -p >&9; command printf "JOBS_END:READY\n" >&9; printf "$ " )' ] || return 1
    __wb_emit HOOK_OK:HANDOFF
}
__wb_run() {
    __wb_id=$1
    __wb_script=$2
    __wb_emit "START:$__wb_id"
    eval "$__wb_script"
    __wb_status=$?
    __wb_emit "JOBS_BEGIN:$__wb_id"
    jobs -p >&9
    __wb_emit "JOBS_END:$__wb_id"
    __wb_emit "DONE:$__wb_id:$__wb_status"
    return "$__wb_status"
}
'''


def _bash_control_init() -> str:
    return r'''__cw_emit() { builtin printf '%s\n' "$1" >&9; }
__cw_ready() { __cw_emit READY; }
__cw_recovery_token=__CW_RECOVERY_TOKEN__
__cw_recovery_epoch=0
__cw_hold() {
    trap '' INT
    __cw_recovery_epoch=$((__cw_recovery_epoch + 1))
    __cw_emit "RECOVERY_WAIT:$__cw_recovery_epoch"
    __cw_emit CONTROL_STOPPED
    while IFS= read -r __cw_recovery <&7; do
        if [[ $__cw_recovery == "RELEASE:$__cw_recovery_epoch:$__cw_recovery_token" ]]; then
            __cw_emit "RECOVERY_ACK:$__cw_recovery_epoch:$$"
            trap - INT
            return
        fi
    done
    while :; do sleep 1; done
}
PROMPT_COMMAND=__cw_ready
PS1='$ '
wb-handoff() {
    __cw_interrupted=0
    trap '__cw_interrupted=1; __cw_emit CONTROL_INTERRUPTED' INT
    __cw_emit "HANDOFF:$$"
    __cw_emit JOBS_BEGIN:HANDOFF
    builtin jobs -p >&9
    __cw_emit JOBS_END:HANDOFF
    __cw_emit "WAIT:$$"
    while IFS= read -r __cw_line <&8; do
        if [[ $__cw_interrupted == 1 ]]; then
            __cw_hold
            return
        fi
        case $__cw_line in
            RUN:*:__CW_END__)
                __cw_payload=${__cw_line#RUN:}
                __cw_id=${__cw_payload%%:*}
                __cw_script=${__cw_payload#*:}
                __cw_script=${__cw_script%:__CW_END__}
                case $__cw_id in ''|*[!a-zA-Z0-9_-]*) __cw_emit BAD_REQUEST; continue;; esac
                __cw_emit "ACCEPT:$__cw_id"
                __cw_emit "START:$__cw_id"
                eval "$__cw_script"
                __cw_status=$?
                __cw_emit "JOBS_BEGIN:$__cw_id"
                builtin jobs -p >&9
                __cw_emit "JOBS_END:$__cw_id"
                __cw_emit "RETURN:$__cw_id:$__cw_status"
                if [[ $__cw_interrupted == 1 ]]; then
                    __cw_hold
                    return
                fi
                __cw_emit "WAIT:$$"
                ;;
            TAKEOVER) trap - INT; __cw_emit "TAKEOVER_ACK:$$"; return;;
            '') :;;
            *) __cw_emit BAD_REQUEST;;
        esac
    done
    if [[ $__cw_interrupted != 1 ]]; then __cw_emit CONTROL_LOST; fi
    __cw_hold
}
'''


def _sh_control_init() -> str:
    return r'''__cw_emit() { command printf '%s\n' "$1" >&9; }
__cw_ready() { __cw_emit READY; }
__cw_recovery_token=__CW_RECOVERY_TOKEN__
__cw_recovery_epoch=0
__cw_hold() {
    trap '' INT
    __cw_recovery_epoch=$((__cw_recovery_epoch + 1))
    __cw_emit "RECOVERY_WAIT:$__cw_recovery_epoch"
    __cw_emit CONTROL_STOPPED
    while IFS= read -r __cw_recovery <&7; do
        if [ "$__cw_recovery" = "RELEASE:$__cw_recovery_epoch:$__cw_recovery_token" ]; then
            __cw_emit "RECOVERY_ACK:$__cw_recovery_epoch:$$"
            trap - INT
            return
        fi
    done
    while :; do sleep 1; done
}
PS1='$( __cw_ready; printf "$ " )'
alias wb-handoff='__cw_handoff'
__cw_handoff() {
    __cw_interrupted=0
    trap '__cw_interrupted=1; __cw_emit CONTROL_INTERRUPTED' INT
    __cw_emit "HANDOFF:$$"
    __cw_emit JOBS_BEGIN:HANDOFF
    jobs -p >&9
    __cw_emit JOBS_END:HANDOFF
    __cw_emit "WAIT:$$"
    while IFS= read -r __cw_line <&8; do
        if [ "$__cw_interrupted" = 1 ]; then
            __cw_hold
            return
        fi
        case $__cw_line in
            RUN:*:__CW_END__)
                __cw_payload=${__cw_line#RUN:}
                __cw_id=${__cw_payload%%:*}
                __cw_script=${__cw_payload#*:}
                __cw_script=${__cw_script%:__CW_END__}
                case $__cw_id in ''|*[!a-zA-Z0-9_-]*) __cw_emit BAD_REQUEST; continue;; esac
                __cw_emit "ACCEPT:$__cw_id"
                __cw_emit "START:$__cw_id"
                eval "$__cw_script"
                __cw_status=$?
                __cw_emit "JOBS_BEGIN:$__cw_id"
                jobs -p >&9
                __cw_emit "JOBS_END:$__cw_id"
                __cw_emit "RETURN:$__cw_id:$__cw_status"
                if [ "$__cw_interrupted" = 1 ]; then
                    __cw_hold
                    return
                fi
                __cw_emit "WAIT:$$"
                ;;
            TAKEOVER) trap - INT; __cw_emit "TAKEOVER_ACK:$$"; return;;
            '') :;;
            *) __cw_emit BAD_REQUEST;;
        esac
    done
    if [ "$__cw_interrupted" != 1 ]; then __cw_emit CONTROL_LOST; fi
    __cw_hold
}
'''
