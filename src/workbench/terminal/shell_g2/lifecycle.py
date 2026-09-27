"""One-request lifecycle observations for the bounded managed-shell probe.

Events describe process/control facts, never experiment success. The transport
still owns admission, PTY writers and identity checks; this is not an adapter.
"""
from dataclasses import dataclass, field
import base64
import fcntl
import os
import signal
import termios

from .prototype import ShellChoice, ShellProcess, UnsafeShellState


@dataclass
class ManagedLifecycle:
    parent_pid: int
    request_id: str | None = None
    accepted: bool = False
    supervisor_pid: int | None = None
    child_pid: int | None = None
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
                if self.supervisor_pid <= 0:
                    raise ValueError
            elif kind == "CHILD":
                if self.supervisor_pid is None or self.child_pid is not None:
                    raise ValueError
                self.child_pid = int(value.split(":")[0])
                if self.child_pid <= 0 or self.child_pid in {self.parent_pid, self.supervisor_pid}:
                    raise ValueError
            elif kind == "EXPERIMENT_START":
                if self.child_pid is None or int(value) != self.child_pid or self.experiment_started:
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

    def __init__(self, choice: ShellChoice) -> None:
        super().__init__(choice, control_wait=True)
        self.lifecycle = ManagedLifecycle(self.pid)
        self.control_wait_seen = False
        self._supervisor_start: str | None = None
        self._takeover_ack = False
        self.manual_prompt_confirmed = False

    def _on_control_event(self, event: str) -> None:
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

    def dispatch_managed(self, request_id: str, script: str) -> None:
        with self.boundary.lock:
            self._drain(0)
            if not self.control_wait_seen or not any(
                e == f"HANDOFF:{self.pid}" for e in self.events
            ):
                raise UnsafeShellState("no same-parent handoff and control wait")
            self.lifecycle.begin(request_id)
            payload = base64.b64encode(script.encode()).decode("ascii")
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

    def release_input(self) -> None:
        with self.boundary.lock:
            self._drain(0)
            self.lifecycle.require_input_release()
            if self.lifecycle.input_returned:
                raise UnsafeShellState("input release already observed; no replay")
            try:
                with open(f"/proc/{self.lifecycle.supervisor_pid}/stat",
                          encoding="ascii") as stream:
                    fields = stream.read().split(") ", 1)[1].split()
                if fields[19] != self._supervisor_start or int(fields[1]) != self.pid:
                    raise OSError("supervisor identity changed")
                slave = fcntl.ioctl(self.master_fd, 0x5441,
                                    os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
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
                not self.lifecycle.returned or self.lifecycle.unknown
                or not self.manual_prompt_confirmed
            ):
                raise UnsafeShellState("managed lifecycle holds user input")
            super().send_user(data)

    def release_control(self) -> None:
        with self.boundary.lock:
            self._drain(0)
            if not self.lifecycle.returned:
                raise UnsafeShellState("lifecycle has no confirmed control return")
            try:
                # Bytes can arrive after the first flush and before control return.
                slave = fcntl.ioctl(self.master_fd, 0x5441,
                                    os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
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
