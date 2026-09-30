"""One-request lifecycle observations for the bounded managed-shell probe.

Events describe process/control facts, never experiment success. The transport
still owns admission, PTY writers and identity checks; this is not an adapter.
"""
from dataclasses import dataclass, field
import base64
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import resource
import select
import shlex
import shutil
import signal
import stat
import sys
import termios
import time

if not __package__:  # Fixed helper invocation by absolute path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "workbench.terminal.shell_g2"

from .prototype import ShellChoice, ShellProcess, UnsafeShellState


@dataclass
class ManagedLifecycle:
    parent_pid: int
    request_id: str | None = None
    accepted: bool = False
    supervisor_pid: int | None = None
    supervisor_group: int | None = None
    child_pid: int | None = None
    child_prepared: bool = False
    child_group: int | None = None
    foreground_verified: bool = False
    exec_ready: bool = False
    main_stopped: bool = False
    experiment_started: bool = False
    main_exit: int | None = None
    lifetime: str = "pending"
    wait_empty: bool = False
    input_barrier: bool = False
    input_returned: bool = False
    controller_exit: int | None = None
    control_returned: bool = False
    unknown: list[str] = field(default_factory=list)

    def begin(self, request_id: str) -> None:
        if self.request_id is not None or self.unknown:
            raise UnsafeShellState("outstanding or unknown lifecycle; no replay")
        self.request_id = request_id

    def fail_unknown(self, reason: str) -> None:
        if reason not in self.unknown:
            self.unknown.append(reason)
        if self.lifetime != "ended":
            self.lifetime = "unknown"

    @property
    def returned(self) -> bool:
        return (not self.unknown and self.lifetime == "ended"
                and self.input_returned and self.control_returned)

    def require_input_release(self) -> None:
        if self.unknown or self.lifetime != "ended" or not self.input_barrier:
            raise UnsafeShellState("input release has no confirmed lifetime boundary")

    def observe(self, event: str) -> None:
        if self.request_id is None:
            return
        kind, _, value = event.partition(":")
        try:
            if kind == "ACCEPT":
                if event != "ACCEPT" or self.accepted:
                    raise ValueError
                self.accepted = True
            elif kind == "SUPERVISOR":
                parts = value.split(":")
                if (not self.accepted or self.supervisor_pid is not None
                        or len(parts) != 4 or parts[2:] != ["SUBREAPER=1", "SIGCHLD=DFL"]):
                    raise ValueError
                self.supervisor_pid = int(parts[0])
                self.supervisor_group = int(parts[1])
                if self.supervisor_pid <= 0:
                    raise ValueError
            elif kind == "CHILD":
                if self.supervisor_pid is None or not self.exec_ready:
                    raise ValueError
                if int(value.split(":")[0]) != self.child_pid:
                    raise ValueError
            elif kind == "CHILD_PREPARED":
                pid, group, session = map(int, value.split(":"))
                if (self.supervisor_pid is None or self.child_prepared or pid <= 0
                        or pid in {self.parent_pid, self.supervisor_pid}
                        or group != pid or group == self.supervisor_group or session != self.parent_pid):
                    raise ValueError
                self.child_pid, self.child_group = pid, group
                self.child_prepared = True
            elif kind == "FOREGROUND_VERIFIED":
                if self.child_prepared and int(value) == self.child_group:
                    self.foreground_verified = True
                elif self.main_exit is None or int(value) != self.supervisor_group:
                    raise ValueError
            elif kind == "EXEC_READY":
                if (not self.child_prepared or not self.foreground_verified
                        or int(value) != self.child_pid or self.exec_ready):
                    raise ValueError
                self.exec_ready = True
            elif kind in {"EXEC_ERROR", "UNKNOWN", "CLEANUP_UNKNOWN"}:
                self.fail_unknown(f"{kind.lower()}:{value}")
            elif kind == "MAIN_STOPPED":
                if not self.experiment_started or self.main_exit is not None:
                    raise ValueError
                self.main_stopped = True
            elif kind == "MAIN_CONTINUED":
                if not self.main_stopped or self.main_exit is not None:
                    raise ValueError
                self.main_stopped = False
            elif kind == "EXPERIMENT_START":
                if (not self.exec_ready or int(value) != self.child_pid
                        or self.experiment_started):
                    raise ValueError
                self.experiment_started = True
            elif kind == "MAIN_RETURN":
                if not self.experiment_started or self.main_exit is not None:
                    raise ValueError
                self.main_exit = int(value)
            elif kind == "LIFETIME_ACTIVE":
                if self.main_exit is None or value != "WNOHANG=0" or self.wait_empty:
                    raise ValueError
                if not self.unknown:
                    self.lifetime = "active"
            elif kind == "WAIT_EMPTY":
                if self.main_exit is None or value != "ECHILD":
                    raise ValueError
                self.wait_empty = True
            elif kind == "LIFETIME_DONE":
                if (not self.wait_empty or not value.startswith("INTERRUPTS=")
                        or not value.removeprefix("INTERRUPTS=").isdecimal()):
                    raise ValueError
                if not self.unknown:
                    self.lifetime = "ended"
            elif kind == "INPUT_BARRIER":
                if event != "INPUT_BARRIER" or self.lifetime != "ended":
                    raise ValueError
                self.input_barrier = True
            elif kind == "INPUT_RELEASED":
                self.require_input_release()
                if event != "INPUT_RELEASED":
                    raise ValueError
                self.input_returned = True
            elif kind == "RETURN":
                if self.main_exit is None or not self.input_returned:
                    raise ValueError
                expected = 128 - self.main_exit if self.main_exit < 0 else self.main_exit
                if int(value) != expected:
                    raise ValueError
                self.controller_exit = int(value)
            elif kind == "WAIT":
                if (not value.startswith(f"{self.parent_pid}:")
                        or self.controller_exit is None or not self.input_returned):
                    raise ValueError
                if not self.unknown:
                    self.control_returned = True
            elif kind in {"CONTROL_LOST", "HOOK_LOST"}:
                self.fail_unknown(kind.lower())
        except (ValueError, IndexError, UnsafeShellState):
            self.fail_unknown(f"invalid_or_incomplete:{kind}")


class ManagedLifecycleProbe(ShellProcess):
    """Bounded transport for the fixed supervisor feasibility fixture.

    The controller emits untagged events and therefore admits one request only.
    A missing event never grants completion or permits replay.
    """

    def __init__(self, choice: ShellChoice, *, environment: dict[str, str] | None = None) -> None:
        super().__init__(choice, control_wait=True, environment=environment)
        self.lifecycle = ManagedLifecycle(self.pid)
        self.control_wait_seen = False
        self._supervisor_start: str | None = None
        self._takeover_ack = False
        self.manual_prompt_confirmed = False
        self._fresh_handoff = False

    def _control_init_source(self) -> str:
        return managed_controller_source(self.choice.executable)

    def _on_control_event(self, event: str) -> None:
        if event == f"HANDOFF:{self.pid}":
            self._fresh_handoff = self.lifecycle.returned and self.manual_prompt_confirmed
            self.control_wait_seen = False
            self._takeover_ack = False
            self.manual_prompt_confirmed = False
        if event.startswith(f"WAIT:{self.pid}:"):
            self.control_wait_seen = True
        if event == f"TAKEOVER_ACK:{self.pid}":
            self._takeover_ack = True
        elif event == "READY" and self._takeover_ack:
            self.manual_prompt_confirmed = True
        self.lifecycle.observe(event)
        if event.startswith("SUPERVISOR:") and not self.lifecycle.unknown:
            try:
                with open(f"/proc/{self.lifecycle.supervisor_pid}/stat",
                          encoding="ascii") as stream:
                    fields = stream.read().split(") ", 1)[1].split()
                if int(fields[1]) != self.pid:
                    raise ValueError("supervisor is not a child of the controller")
                self._supervisor_start = fields[19]
            except (OSError, ValueError, IndexError):
                self.lifecycle.fail_unknown("supervisor_identity_unconfirmed")
        if event.startswith("RETURN:") and not self.lifecycle.returned:
            # RETURN alone is not the lifetime or input-return boundary.
            if self.lifecycle.controller_exit is None:
                self.lifecycle.fail_unknown("supervisor_return_without_boundary")
        if self.lifecycle.unknown:
            self.boundary.fail_closed()

    def _drain(self, timeout: float) -> None:
        try:
            super()._drain(timeout)
            if self.lifecycle.request_id is not None:
                if self.boundary.uncertain or self.boundary.needs_review:
                    self.lifecycle.fail_unknown("control_observation_lost")
                if (self.lifecycle.supervisor_pid is not None
                        and self.lifecycle.lifetime != "ended"
                        and not os.path.exists(f"/proc/{self.lifecycle.supervisor_pid}")):
                    self.lifecycle.fail_unknown("supervisor_disappeared")
                if self.lifecycle.unknown:
                    self.boundary.fail_closed()
        except OSError:
            self.lifecycle.fail_unknown("control_fd_failure")
            self.boundary.fail_closed()
            raise

    def dispatch_managed(self, request_id: str, script: str | list[str], *, return_timeout: float = 5.0) -> None:
        with self.boundary.lock:
            self._drain(0)
            if not self.control_wait_seen or not any(
                e == f"HANDOFF:{self.pid}" for e in self.events
            ):
                raise UnsafeShellState("no same-parent handoff and control wait")
            payload = encode_run(normalize_run(self.choice.executable, script, request_id,
                                               return_timeout=return_timeout))
            self.lifecycle.begin(request_id)
            request = f"RUN:{payload}\n".encode()
            if len(request) > 4096:
                self.lifecycle.fail_unknown("request_too_large")
                raise ValueError("bounded request exceeds pipe atomic write")
            try:
                if os.write(self._request_fd, request) != len(request):
                    raise OSError("partial managed request")
            except OSError:
                self.lifecycle.fail_unknown("request_write_failure")
                self.boundary.fail_closed()
                raise

    def rearm_after_handoff(self) -> None:
        """A new request, never a retry, after verified manual/control return."""
        if not self._fresh_handoff or not self.control_wait_seen or not self.lifecycle.returned:
            raise UnsafeShellState("no verified fresh handoff after completed request")
        self.lifecycle = ManagedLifecycle(self.pid)
        self._supervisor_start = None
        self._fresh_handoff = False

    def close(self) -> None:
        if not self._closed:
            try:
                try:
                    self._drain(0)
                except OSError:
                    self.lifecycle.fail_unknown("close_observation_lost")
                pid = self.lifecycle.supervisor_pid
                if pid is not None and self._supervisor_start is not None:
                    fields = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
                    if fields[19] == self._supervisor_start and int(fields[1]) == self.pid:
                        os.kill(pid, signal.SIGTERM)
                        deadline = time.monotonic() + 2.5
                        while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
                            try:
                                self._drain(0.01)
                            except OSError:
                                time.sleep(0.01)
            except FileNotFoundError:
                pass  # A normally returned supervisor was already reaped.
            except (OSError, ValueError, IndexError):
                self.lifecycle.fail_unknown("close_observation_lost")
        super().close()

    def release_input(self) -> None:
        with self.boundary.lock:
            self._drain(0)
            self.lifecycle.require_input_release()
            if self.lifecycle.input_returned:
                raise UnsafeShellState("input release already observed; no replay")
            try:
                with open(f"/proc/{self.lifecycle.supervisor_pid}/stat", encoding="ascii") as stream:
                    fields = stream.read().split(") ", 1)[1].split()
                if fields[19] != self._supervisor_start or int(fields[1]) != self.pid:
                    raise OSError("supervisor identity changed")
                slave = fcntl.ioctl(self.master_fd, 0x5441, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
                try:
                    termios.tcflush(slave, termios.TCIFLUSH)
                finally:
                    os.close(slave)
                os.kill(self.lifecycle.supervisor_pid, signal.SIGUSR1)
            except (OSError, TypeError, IndexError, ValueError):
                self.lifecycle.fail_unknown("input_flush_or_release_failure")
                self.boundary.fail_closed()
                raise

    def send_user(self, data: bytes) -> None:
        with self.boundary.lock:
            self._drain(0)
            if self.lifecycle.request_id is not None and (
                not self.lifecycle.returned or self.lifecycle.unknown or not self.manual_prompt_confirmed
            ):
                raise UnsafeShellState("managed lifecycle holds user input")
            super().send_user(data)

    def release_control(self) -> None:
        with self.boundary.lock:
            self._drain(0)
            if not self.lifecycle.returned:
                raise UnsafeShellState("lifecycle has no confirmed control return")
            try:
                slave = fcntl.ioctl(self.master_fd, 0x5441, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
                try:
                    termios.tcflush(slave, termios.TCIFLUSH)
                finally:
                    os.close(slave)
                if os.write(self._request_fd, b"TAKEOVER\n") != len(b"TAKEOVER\n"):
                    raise OSError("partial takeover")
            except OSError:
                self.lifecycle.fail_unknown("control_release_failure")
                self.boundary.fail_closed()
                raise


def normalize_run(shell: str, script: str | list[str], request_id: str, *, return_timeout: float = 5.0) -> dict:
    """One bounded wire representation; payload parsing stays off the parent."""
    timeout_fields = {}
    if type(return_timeout) not in (int, float) or not 0 < return_timeout <= 300:
        raise ValueError("return_timeout must be finite, positive, and at most 300 seconds")
    if return_timeout != 5.0:
        timeout_fields["return_timeout"] = return_timeout
    if isinstance(script, list):
        if not script or not all(isinstance(arg, str) for arg in script):
            raise ValueError("argv must be a nonempty string list")
        return {"version": 1, "request_id": request_id, "kind": "argv", "argv": script,
                "selected": script[0], "hold_start": False, "hold_identity": False, **timeout_fields}
    if not isinstance(script, str):
        raise ValueError("RUN requires script text or a string argv list")
    hold_start = script.startswith("#WB_START_HOLD\n")
    if hold_start:
        script = script.removeprefix("#WB_START_HOLD\n")
    hold_identity = script.startswith("#WB_CHILD_ID_HOLD\n")
    if hold_identity:
        script = script.removeprefix("#WB_CHILD_ID_HOLD\n")
    executable = shell
    if script.startswith("#WB_SUBSTITUTE_EXEC:"):
        directive, _, script = script.partition("\n")
        executable = directive.partition(":")[2]
    if script.startswith("#WB_ARGV_JSON\n"):
        argv = json.loads(script.removeprefix("#WB_ARGV_JSON\n"))
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("argv must be a nonempty string list")
        kind, selected = "argv", os.path.realpath(argv[0])
    else:
        if hold_identity:
            script = ('printf "CHILD_READY:%s\\n" "$$" >&9; '
                      'IFS= read -r __wb_identity <&"$WB_RELEASE_FD"; '
                      '[ "$__wb_identity" = R ] || exit 72; ' + script)
        argv = [executable, "-c", script]
        kind, selected = "script", os.path.realpath(shell)
    return {"version": 1, "request_id": request_id, "kind": kind, "argv": argv,
            "selected": selected, "hold_start": hold_start, "hold_identity": hold_identity, **timeout_fields}


def encode_run(request: dict) -> str:
    return base64.b64encode(json.dumps(request, separators=(",", ":")).encode()).decode("ascii")


def managed_controller_source(shell: str, *, handoff_checks: bool = False) -> str:
    """Persistent control shell launches a fixed helper with one opaque token."""
    helper = shlex.quote(str(Path(__file__).resolve()))
    bash = Path(shell).name == "bash"
    prompt = ("PROMPT_COMMAND='__b_emit READY'\nPS1='$ '\n" if bash else
              "PS1='$( __b_emit READY; printf \"$ \" )'\n")
    prompt_check = (
        '[ "$PROMPT_COMMAND" = "__b_emit READY" ] || '
        '[ "$PROMPT_COMMAND" = "__b_emit PRIOR_HOOK; __b_emit READY" ]'
        if bash else
        '[ "$PS1" = \'$( __b_emit READY; printf "$ " )\' ] || '
        '[ "$PS1" = \'$( __b_emit PRIOR_HOOK; __b_emit READY; printf "$ " )\' ] || '
        '[ "$PS1" = "$VIRTUAL_ENV_PROMPT"\'$( __b_emit READY; printf "$ " )\' ] || '
        '[ "$PS1" = "$VIRTUAL_ENV_PROMPT"\'$( __b_emit PRIOR_HOOK; __b_emit READY; printf "$ " )\' ]'
    )
    ps1_check = ('[ "$PS1" = \'$ \' ] || { [ -n "$VIRTUAL_ENV" ] && '
                 '[ "$PS1" = "$VIRTUAL_ENV_PROMPT"\'$ \' ]; }' if bash else ":")
    handoff = '__b_emit "HANDOFF:$$"; __b_loop'
    if handoff_checks:
        handoff = '''__b_emit "HANDOFF:$$"
    trap > "$BOUNDARY_TRAPS"
    if ! __b_hook_compatible; then
        __b_emit HOOK_REJECTED
        return 1
    fi
    __b_emit HOOK_OK:HANDOFF
    __b_emit JOBS_BEGIN:HANDOFF
    jobs -p >&9
    __b_emit JOBS_END:HANDOFF
    __b_emit "CONTROL_READY:$$"
    __b_loop'''
    return f'''__b_emit() {{ printf '%s\\n' "$1" >&9; }}
{prompt}
__b_hook_compatible() {{
    {{ {prompt_check}; }} || return 1
    {{ {ps1_check}; }} || return 1
    case "$(cat "$BOUNDARY_TRAPS")" in *CHLD*) return 1;; esac
}}
__b_loop() {{
    trap > "$BOUNDARY_TRAPS"
    trap '__b_emit PARENT_INT' INT
    __b_emit "WAIT:$$:$PWD:$BOUNDARY_PREPARED"
    while :; do
        if IFS= read -r __b_line <&8; then
            case $__b_line in
                RUN:*)
                    __b_payload=${{__b_line#RUN:}}
                    if ! __b_hook_compatible; then
                        __b_emit HOOK_LOST
                        __b_emit HOOK_REJECTED
                        continue
                    fi
                    __b_emit ACCEPT
                    __b_emit START
                    if {shlex.quote(sys.executable)} {helper} --supervisor {shlex.quote(shell)} "$__b_payload"; then
                        __b_status=0
                    else
                        __b_status=$?
                    fi
                    __b_emit "RETURN:$__b_status"
                    __b_emit "WAIT:$$:$PWD:$BOUNDARY_PREPARED"
                    ;;
                TAKEOVER)
                    trap - INT
                    eval "$(cat "$BOUNDARY_TRAPS")"
                    trap > "$BOUNDARY_TRAPS_AFTER"
                    __b_emit "TAKEOVER_ACK:$$"
                    return
                    ;;
                *) __b_emit BAD_REQUEST;;
            esac
        else
            __b_emit CONTROL_LOST
            while :; do sleep 1; done
        fi
    done
}}
__b_handoff() {{ {handoff}; }}
alias wb-handoff=__b_handoff
'''


def _emit(event: str) -> None:
    os.write(9, (event + "\n").encode("ascii"))


def _await_release(signum: signal.Signals, timeout: float = 5.0) -> None:
    if signal.sigtimedwait({signum}, timeout) is None:
        raise TimeoutError("managed release deadline")


def _foreground(group: int) -> None:
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    try:
        os.tcsetpgrp(0, group)
        if os.tcgetpgrp(0) != group:
            raise RuntimeError("foreground ownership mismatch")
        _emit(f"FOREGROUND_VERIFIED:{group}")
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _cleanup_children() -> None:
    children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    deadline = time.monotonic() + 2.0
    while True:
        for raw_pid in children.read_text().split():
            try:
                os.kill(int(raw_pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                _emit("CLEANUP_EMPTY:ECHILD")
                return
            if not pid:
                break
        if time.monotonic() >= deadline:
            _emit("CLEANUP_UNKNOWN:DEADLINE")
            raise RuntimeError("managed child cleanup deadline")
        time.sleep(0.005)


def _close_child_fds(keep: set[int]) -> None:
    """Linux child allowlist: no persistent host request/recovery channels."""
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        if fd not in keep:
            try:
                os.close(fd)
            except OSError as exc:
                # listdir's transient directory descriptor is already closed.
                if exc.errno != errno.EBADF:
                    raise


def run_supervisor(shell: str, token: str) -> int:
    """Same-session split-PG launch, exec acknowledgement and single reaper."""
    request = json.loads(base64.b64decode(token, validate=True))
    return_timeout = request.get("return_timeout", 5.0)
    if type(return_timeout) not in (int, float) or not 0 < return_timeout <= 300:
        raise ValueError("invalid bounded return deadline")
    argv = request.get("argv")
    if (request.get("version") != 1 or request.get("kind") not in {"script", "argv"}
            or not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv)
            or not all(isinstance(request.get(key), bool) for key in ("hold_start", "hold_identity"))):
        raise ValueError("invalid canonical RUN")
    if request["kind"] == "script" and request["selected"] != os.path.realpath(shell):
        raise ValueError("script shell selection changed")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.restype = ctypes.c_long

    def trace(operation: int, pid: int, data: int = 0) -> None:
        if libc.ptrace(ctypes.c_uint(operation), ctypes.c_uint(pid),
                       ctypes.c_void_p(), ctypes.c_void_p(data)) == -1:
            raise OSError(ctypes.get_errno(), "exec-transition ptrace failed")

    def await_exec_status(pid: int) -> int:
        deadline = time.monotonic() + return_timeout
        while True:
            found, status = os.waitpid(pid, os.WNOHANG | os.WUNTRACED)
            if found:
                return status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("exec-transition acknowledgement unavailable")
            signal.sigtimedwait({signal.SIGCHLD}, remaining)

    def require_unprivileged(path: str) -> None:
        # Tracing suppresses privilege gains; never silently change those runs.
        if os.stat(path).st_mode & (stat.S_ISUID | stat.S_ISGID):
            raise PermissionError("PRIVILEGED_EXEC_UNSUPPORTED")
        try:
            capabilities = os.getxattr(path, "security.capability")
        except OSError as exc:
            if exc.errno not in {errno.ENODATA, errno.ENOTSUP}:
                raise
        else:
            if capabilities:
                raise PermissionError("PRIVILEGED_EXEC_UNSUPPORTED")

    enabled = ctypes.c_int()
    if libc.prctl(36, 1, 0, 0, 0) != 0 or libc.prctl(37, ctypes.byref(enabled), 0, 0, 0) != 0 or enabled.value != 1:
        raise OSError("subreaper unavailable")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if signal.getsignal(signal.SIGCHLD) is not signal.SIG_DFL:
        raise RuntimeError("SIGCHLD disposition changed")
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGCHLD, signal.SIGUSR1, signal.SIGUSR2})
    stage = "start"
    target = None
    interrupts = 0

    def observe(signum: int, _frame: object) -> None:
        nonlocal interrupts
        if signum == signal.SIGINT:
            interrupts += 1
        _emit(f"SIGNAL_{signal.Signals(signum).name.removeprefix('SIG')}")
        if target is None:
            _emit(f"NO_TARGET:{stage}:{signal.Signals(signum).name}")
        else:
            _emit(f"SUPERVISOR_SIGNAL:{stage}:{target}:{signal.Signals(signum).name}")

    def terminate(_signum: int, _frame: object) -> None:
        raise SystemExit(73)

    for signum in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP, signal.SIGCONT):
        signal.signal(signum, observe)
    signal.signal(signal.SIGWINCH, lambda *_: _emit(f"SUPERVISOR_PING:{stage}"))
    signal.signal(signal.SIGTERM, terminate)
    _emit(f"SUPERVISOR:{os.getpid()}:{os.getpgrp()}:SUBREAPER=1:SIGCHLD=DFL")
    fds = []
    try:
        if request["hold_start"]:
            _emit("START_BARRIER")
            _await_release(signal.SIGUSR2)
        release_read, release_write = os.pipe()
        prepared_read, prepared_write = os.pipe()
        go_read, go_write = os.pipe()
        error_read, error_write = os.pipe()  # CLOEXEC: EOF alone is not exec evidence.
        fds = [release_read, release_write, prepared_read, prepared_write,
               go_read, go_write, error_read, error_write]
        _emit(f"LAUNCH:{request['kind']}:{request['selected']}")
        child = os.fork()
        if child == 0:
            try:
                # FD9 and release_read are explicit bounded payload-fixture
                # channels, not the persistent shell request/recovery readers.
                payload_fds = {0, 1, 2, 9, release_read}
                _close_child_fds(payload_fds | {prepared_write, go_read, error_write})
                os.setpgid(0, 0)
                for signum in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP, signal.SIGCONT, signal.SIGTERM):
                    signal.signal(signum, signal.SIG_DFL)
                signal.pthread_sigmask(signal.SIG_SETMASK, set())
                os.set_inheritable(release_read, True)
                env = os.environ.copy()
                env["WB_RELEASE_FD"] = str(release_read)
                os.write(prepared_write, b"P")
                os.close(prepared_write)
                if os.read(go_read, 1) != b"G":
                    os._exit(72)
                os.close(go_read)
                selected = shutil.which(argv[0], path=env.get("PATH"))
                if selected is not None:
                    require_unprivileged(selected)
                # Trace only this launch transition. The exec event stops the
                # new image before its first instruction, including fast exits.
                trace(0, 0)  # PTRACE_TRACEME; direct parent, no extra privilege.
                os.kill(os.getpid(), signal.SIGSTOP)
                _close_child_fds(payload_fds | {error_write})
                os.execvpe(argv[0], argv, env)
            except BaseException as exc:
                os.write(error_write, f"{type(exc).__name__}:{exc}".encode("ascii", "backslashreplace")[:256])
                os._exit(127)
        for fd in (prepared_write, go_read, error_write):
            os.close(fd)
            fds.remove(fd)
        ready, _, _ = select.select([prepared_read], [], [], 5.0)
        if not ready or os.read(prepared_read, 1) != b"P":
            raise TimeoutError("child preparation unavailable")
        if os.getpgid(child) != child or child == os.getpgrp() or os.getsid(child) != os.getsid(0):
            raise RuntimeError("child split-PG identity mismatch")
        _emit(f"CHILD_PREPARED:{child}:{child}:{os.getsid(child)}")
        target = child
        _foreground(child)
        os.write(go_write, b"G")
        status = await_exec_status(child)
        if not os.WIFSTOPPED(status) or os.WSTOPSIG(status) != signal.SIGSTOP:
            ready, _, _ = select.select([error_read], [], [], 0)
            error = os.read(error_read, 256) if ready else b""
            reason = error.decode("ascii") if error else f"PRE_EXEC_STATUS:{status}"
            _emit(f"EXEC_ERROR:{child}:{reason}")
            _emit("UNKNOWN:EXEC_TRANSITION_UNCONFIRMED")
            _foreground(os.getpgrp())
            return 1
        trace(0x4200, child, 0x10)  # PTRACE_SETOPTIONS, PTRACE_O_TRACEEXEC.
        trace(7, child)  # PTRACE_CONT; prepared foreground ownership is verified.
        status = await_exec_status(child)
        if (not os.WIFSTOPPED(status) or os.WSTOPSIG(status) != signal.SIGTRAP
                or status >> 16 != 4):  # PTRACE_EVENT_EXEC, not an ordinary trap.
            _emit(f"EXEC_ERROR:{child}:PRE_EXEC_STATUS:{status}")
            _emit("UNKNOWN:EXEC_TRANSITION_UNCONFIRMED")
            _foreground(os.getpgrp())
            return 1
        ready, _, _ = select.select([error_read], [], [], 5.0)
        if not ready:
            raise TimeoutError("exec acknowledgement unavailable")
        error = os.read(error_read, 256)
        if error:
            _emit(f"EXEC_ERROR:{child}:{error.decode('ascii')}")
            _emit("UNKNOWN:EXEC_FAILED")
            _foreground(os.getpgrp())
            return 1
        try:
            require_unprivileged(f"/proc/{child}/exe")
        except PermissionError:
            _emit(f"EXEC_ERROR:{child}:PRIVILEGED_EXEC_UNSUPPORTED")
            _emit("UNKNOWN:PRIVILEGED_EXEC_UNSUPPORTED")
            _foreground(os.getpgrp())
            return 1
        _emit(f"EXEC_READY:{child}")
        _emit(f"CHILD:{child}:{child}")
        trace(17, child)  # PTRACE_DETACH; restore native stop/continue semantics.
        if request["hold_identity"]:
            _await_release(signal.SIGUSR2)
            os.write(release_write, b"R\n")
        stage = "running"
        _emit(f"EXPERIMENT_START:{child}")
        stopped = False
        deadline = time.monotonic() + return_timeout
        while True:
            pid, status = os.waitpid(child, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
            if pid:
                if os.WIFSTOPPED(status):
                    stopped = True
                    _emit(f"MAIN_STOPPED:{os.WSTOPSIG(status)}")
                elif os.WIFCONTINUED(status):
                    stopped = False
                    _emit("MAIN_CONTINUED")
                else:
                    status = os.waitstatus_to_exitcode(status)
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("main return deadline")
            received = signal.sigtimedwait({signal.SIGCHLD, signal.SIGUSR2}, remaining)
            if received is not None and received.si_signo == signal.SIGUSR2:
                if not stopped:
                    raise RuntimeError("continue requested for nonstopped child")
                _foreground(child)
                os.killpg(child, signal.SIGCONT)
        stage = "return"
        target = None
        _emit(f"MAIN_RETURN:{status}")
        _foreground(os.getpgrp())
        deadline = time.monotonic() + 5.0
        active = False
        while True:
            try:
                pid, result = os.waitpid(-1, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
            except ChildProcessError:
                _emit("WAIT_EMPTY:ECHILD")
                break
            if pid:
                if os.WIFEXITED(result) or os.WIFSIGNALED(result):
                    _emit(f"DESCENDANT_REAPED:{pid}:{os.waitstatus_to_exitcode(result)}")
                else:
                    _emit(f"DESCENDANT_LIVE:{pid}")
                continue
            if not active:
                _emit("LIFETIME_ACTIVE:WNOHANG=0")
                active = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("descendant return deadline")
            received = signal.sigtimedwait({signal.SIGCHLD, signal.SIGUSR2}, remaining)
            if received is not None and received.si_signo == signal.SIGUSR2:
                os.write(release_write, b"R")
        _emit(f"LIFETIME_DONE:INTERRUPTS={interrupts}")
        _emit("INPUT_BARRIER")
        _await_release(signal.SIGUSR1)
        _emit("INPUT_RELEASED")
        return status if status >= 0 else 128 - status
    except BaseException:
        _emit("UNKNOWN:SUPERVISOR_FAILURE")
        raise
    finally:
        _cleanup_children()
        for fd in fds:
            os.close(fd)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--supervisor":
        raise SystemExit(2)
    raise SystemExit(run_supervisor(sys.argv[2], sys.argv[3]))
