"""Bounded same-interpreter control-wait experiment for CW-03.

This probe intentionally supports one outstanding request. It is not the
production shell adapter or a general command transport.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import select
import signal
import termios
import time

from .prototype import ShellChoice, ShellProcess, UnsafeShellState


@dataclass(frozen=True, slots=True)
class ForegroundTarget:
    """Host-observed target; this is separate from the shell's takeover ACK."""

    command_id: str
    process_group: int


class CleanupIncomplete(RuntimeError):
    """One or more probe cgroups remain after bounded cleanup."""


class ControlWaitProbe(ShellProcess):
    def __init__(self, choice: ShellChoice | None = None) -> None:
        super().__init__(choice, control_wait=True)
        self.waiting = False
        self.takeover_requested = False
        self.sent_id: str | None = None
        self.confirmed_target: ForegroundTarget | None = None
        self.manual_cgroup: Path | None = None
        self._cgroup_setup_failed = False
        self.run_cgroup: Path | None = None
        self._return_status: int | None = None
        self._awaiting_wait = False
        self._shell_left_run = False
        self._local_done = False
        self._local_active_reported = False
        self._takeover_sent = False
        self._takeover_ack_seen = False
        self._recovery_epoch: int | None = None
        self._recovery_requested = False
        self._recovery_ack_seen = False
        self.manual_prompt_confirmed = False
        self.cleanup_residuals: tuple[Path, ...] = ()
        self._pending_events: list[str] = []
        self._setup_manual_cgroup()

    def wait_event(self, expected: str, timeout: float = 2.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for index, event in enumerate(self._pending_events):
                if event == expected or event.startswith(expected):
                    return self._pending_events.pop(index)
            self._drain(timeout=min(0.03, deadline - time.monotonic()))
            while self._event_cursor < len(self._events):
                event = self._events[self._event_cursor]
                self._event_cursor += 1
                if event == expected or event.startswith(expected):
                    return event
                self._pending_events.append(event)
        self.boundary.fail_closed()
        raise TimeoutError(f"control event not received: {expected}")

    def _pid_cgroup(self, pid: int) -> Path:
        raw = Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
        return Path("/sys/fs/cgroup") / raw.lstrip("/")

    def _move_shell(self, destination: Path) -> None:
        (destination / "cgroup.procs").write_text(str(self.pid))
        if self._pid_cgroup(self.pid) != destination:
            raise OSError("shell cgroup move could not be verified")

    def _populated(self, group: Path) -> bool:
        for line in (group / "cgroup.events").read_text().splitlines():
            if line.startswith("populated "):
                return line == "populated 1"
        raise OSError("cgroup populated state unavailable")

    def _setup_manual_cgroup(self) -> None:
        try:
            base = self._pid_cgroup(os.getpid())
            if not os.access(base / "cgroup.procs", os.W_OK):
                return
            manual = base / f"wb-cw03-manual-{os.getpid()}-{self.pid}"
            manual.mkdir()
            self.manual_cgroup = manual
            self._move_shell(manual)
        except OSError:
            self._cgroup_setup_failed = True
            self.boundary.fail_closed()
            if self.manual_cgroup is not None:
                rolled_back = False
                try:
                    self._move_shell(base)
                    rolled_back = True
                except OSError:
                    pass
                if rolled_back:
                    try:
                        self.manual_cgroup.rmdir()
                    except OSError:
                        pass  # close() retains and reports this residual.
                    else:
                        self.manual_cgroup = None

    @property
    def delegated(self) -> bool:
        return self.manual_cgroup is not None and not self._cgroup_setup_failed

    def _emit_lifecycle(self, event: str) -> None:
        self._events.append(event)
        self.boundary.observe_event(event)

    def _on_control_event(self, event: str) -> None:
        if event == f"HANDOFF:{self.pid}":
            self.manual_prompt_confirmed = False
            self._takeover_ack_seen = False
            self._recovery_requested = False
            self._recovery_ack_seen = False
            self._recovery_epoch = None
        elif event.startswith("RETURN:"):
            parts = event.split(":", 2)
            if len(parts) != 3 or not parts[2].lstrip("-").isdecimal():
                self.boundary.fail_closed()
                return
            self._return_status = int(parts[2])
            self._awaiting_wait = True
            self._emit_lifecycle(f"EVAL_RETURNED:{parts[1]}:{parts[2]}")
            if self.boundary._job_pids:
                self._emit_lifecycle(f"ACTIVE_JOBS:{parts[1]}")
                self.boundary.fail_closed()
            if self.run_cgroup is None:
                self._emit_lifecycle(f"LIFETIME_UNKNOWN:{parts[1]}")
                self.boundary.fail_closed()
        elif event == f"WAIT:{self.pid}" and self._awaiting_wait:
            self._awaiting_wait = False
            if self.run_cgroup is not None and self.manual_cgroup is not None:
                try:
                    self._move_shell(self.manual_cgroup)
                    self._shell_left_run = True
                    self._poll_local_lifetime()
                except OSError:
                    self._emit_lifecycle(f"LIFETIME_UNKNOWN:{self.sent_id}")
                    self.boundary.fail_closed()
                    return
            self.waiting = True
            self._send_takeover_if_safe()
        elif event == f"WAIT:{self.pid}" and self.sent_id is None:
            self.waiting = True
            self._send_takeover_if_safe()
        elif event == f"TAKEOVER_ACK:{self.pid}":
            self._takeover_ack_seen = True
        elif event == "READY":
            if self._recovery_requested:
                self.manual_prompt_confirmed = self._recovery_ack_seen
            elif self._takeover_ack_seen:
                self.manual_prompt_confirmed = True
        elif event.startswith("RECOVERY_WAIT:"):
            raw_epoch = event.partition(":")[2]
            if raw_epoch.isdecimal() and int(raw_epoch) > 0:
                self._recovery_epoch = int(raw_epoch)
            else:
                self.boundary.fail_closed()
        elif event.startswith("RECOVERY_ACK:"):
            self._recovery_ack_seen = (
                self._recovery_requested
                and event == f"RECOVERY_ACK:{self._recovery_epoch}:{self.pid}"
            )
            if not self._recovery_ack_seen:
                self.boundary.fail_closed()
        elif event in {"CONTROL_LOST", "CONTROL_INTERRUPTED", "CONTROL_STOPPED"}:
            if event == "CONTROL_INTERRUPTED" and self.waiting and self.sent_id is None:
                # Bash may restart read(8) after its trap; the empty record
                # moves it to the controller boundary without executing work.
                try:
                    os.write(self._request_fd, b"\n")
                except OSError:
                    self.boundary.fail_closed()
            if event == "CONTROL_STOPPED" and self.run_cgroup and not self._shell_left_run:
                try:
                    self._move_shell(self.manual_cgroup)
                    self._shell_left_run = True
                    self._poll_local_lifetime()
                except (OSError, TypeError):
                    self._emit_lifecycle(f"LIFETIME_UNKNOWN:{self.sent_id}")
            self.waiting = False
            self.confirmed_target = None
            self.boundary.fail_closed()

    def recover_manual_prompt(self, timeout: float = 2.0) -> None:
        """Release a stopped controller only after its own safe boundary ACK."""
        with self.boundary.lock:
            self._drain(timeout=0)
            if (
                "CONTROL_STOPPED" not in self.events
                or self._recovery_epoch is None
                or self._recovery_requested
                or self.manual_prompt_confirmed
                or self._awaiting_wait
                or self._current_foreground_child_group() is not None
                or self.boundary._job_pids
                or (self.sent_id is not None and self._return_status is None)
                or (self.run_cgroup is not None and not self._shell_left_run)
            ):
                self.boundary.fail_closed()
                raise UnsafeShellState("stopped controller has no verified recovery boundary")
            self._flush_slave_input()
            self._pending_events = [e for e in self._pending_events if e != "READY"]
            self._event_cursor = len(self._events)
            start = len(self._events)
            self._recovery_requested = True
            request = f"RELEASE:{self._recovery_epoch}:{self._recovery_token}\n".encode()
            try:
                if os.write(self._recovery_fd, request) != len(request):
                    raise OSError("partial recovery request")
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    self._drain(timeout=min(0.03, deadline - time.monotonic()))
                    fresh = self._events[start:]
                    if self._recovery_ack_seen and "READY" in fresh:
                        return
                    if "READY" in fresh and not self._recovery_ack_seen:
                        break
            except OSError:
                self.boundary.fail_closed()
                raise
            self.boundary.fail_closed()
            raise TimeoutError("recovery ACK and fresh prompt were not confirmed")

    def _flush_slave_input(self) -> None:
        # Linux TIOCGPTPEER opens this PTY's slave without relying on a path.
        slave = fcntl.ioctl(self.master_fd, 0x5441, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            termios.tcflush(slave, termios.TCIFLUSH)
        finally:
            os.close(slave)

    def _send_takeover_if_safe(self) -> None:
        if not self.takeover_requested or self._takeover_sent or not self.waiting:
            return
        if self.run_cgroup is not None and not self._shell_left_run:
            return
        # No prompt can resume until stale PTY input has been discarded.
        self._takeover_sent = True  # A failed write is unknown; never retry it.
        try:
            self._flush_slave_input()
            os.write(self._request_fd, b"\nTAKEOVER\n")
        except OSError:
            self.boundary.fail_closed()
            # Keep parsing events already drained from FD 9. In particular,
            # CONTROL_STOPPED may be in the same read after a closed FD 8.

    def _drain(self, timeout: float) -> None:
        with self.boundary.lock:
            super()._drain(timeout)
            self._poll_local_lifetime()

    def _poll_local_lifetime(self) -> None:
        if self.run_cgroup is None or not self._shell_left_run or self._local_done:
            return
        try:
            populated = self._populated(self.run_cgroup)
        except OSError:
            self._emit_lifecycle(f"LIFETIME_UNKNOWN:{self.sent_id}")
            self.boundary.fail_closed()
            self._local_done = True
            return
        if populated:
            if not self._local_active_reported:
                self._emit_lifecycle(f"LOCAL_WORK_ACTIVE:{self.sent_id}")
                self._local_active_reported = True
            return
        self._local_done = True
        self._emit_lifecycle(f"LOCAL_LIFETIME_DONE:{self.sent_id}")

    def wait_control(self, timeout: float = 2.0) -> None:
        self.wait_event(f"WAIT:{self.pid}", timeout)
        self.waiting = True

    def send_user(self, data: bytes) -> None:
        with self.boundary.lock:
            self._drain(timeout=0)
            if self.boundary._handoff_seen and not self.manual_prompt_confirmed:
                raise UnsafeShellState("managed control wait has no manual PTY target")
            super().send_user(data)

    def dispatch_control(
        self, command_id: str, script: str, *, expected_epoch: int, generation: int
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", command_id):
            raise ValueError("invalid command ID")
        if "\n" in script or "\r" in script:
            raise ValueError("control probe accepts one physical line")
        with self.boundary.lock:
            self._drain(timeout=0)
            if (
                not self.waiting
                or self.takeover_requested
                or self.sent_id is not None
                or generation != self.generation
                or expected_epoch != self.owner_epoch
                or self.boundary.owner != "user"
                or not self.boundary._handoff_seen
                or not self.boundary._handoff_line
                or self.boundary.submitted_lines != 1
                or self.boundary.pending_line
                or self.boundary.uncertain
                or self.boundary.needs_review
            ):
                raise UnsafeShellState("control wait has no verified execution right")
            request = f"RUN:{command_id}:{script}:__CW_END__\n".encode()
            if len(request) > 4096:
                raise ValueError("control probe request is too large")
            if self.manual_cgroup is not None:
                run = self.manual_cgroup.parent / f"wb-cw03-run-{os.getpid()}-{self.pid}"
                run.mkdir()
                self.run_cgroup = run
                try:
                    self._move_shell(run)
                    if self._populated(self.manual_cgroup):
                        raise UnsafeShellState("manual-phase process remains active")
                except (OSError, UnsafeShellState):
                    self.boundary.fail_closed()
                    try:
                        self._move_shell(self.manual_cgroup)
                        run.rmdir()
                        self.run_cgroup = None
                    except OSError:
                        pass  # close() still owns and kills this run cgroup.
                    raise
            self.sent_id = command_id  # A failed or partial write is unknown, never retried.
            self.waiting = False
            written = os.write(self._request_fd, request)
            if written != len(request):
                raise OSError("partial control request; outcome unknown")

    def request_takeover(self) -> str:
        with self.boundary.lock:
            self.takeover_requested = True
            if self.sent_id is None:
                state = "not_sent"
            else:
                state = "unknown"
            self._send_takeover_if_safe()
            return state

    def _current_foreground_child_group(self) -> int | None:
        foreground = self._foreground_group()
        try:
            shell_group = os.getpgid(self.pid)
        except ProcessLookupError:
            return None
        if foreground is None or foreground == shell_group:
            return None
        for pid in self._descendant_pids():
            try:
                with open(f"/proc/{pid}/stat", encoding="ascii") as stream:
                    fields = stream.read().split(") ", 1)[1].split()
                if int(fields[2]) == foreground and int(fields[3]) == self.pid:
                    return foreground
            except (OSError, ValueError, IndexError):
                continue
        return None

    def confirm_foreground_takeover(self) -> ForegroundTarget:
        with self.boundary.lock:
            self._drain(timeout=0)
            if not self.takeover_requested or self.sent_id is None:
                raise UnsafeShellState("takeover has no sent request")
            if f"START:{self.sent_id}" not in self.events or any(
                event.startswith((f"DONE:{self.sent_id}:", f"ACTIVE_JOBS:{self.sent_id}"))
                for event in self.events
            ):
                raise UnsafeShellState("no running foreground command")
            group = self._current_foreground_child_group()
            if group is None:
                raise UnsafeShellState("foreground child target is not confirmed")
            target = ForegroundTarget(self.sent_id, group)
            self.confirmed_target = target
            return target

    def send_confirmed_foreground(self, data: bytes) -> None:
        with self.boundary.lock:
            self._drain(timeout=0)
            target = self.confirmed_target
            if (
                target is None
                or not self.takeover_requested
                or target.command_id != self.sent_id
                or self._current_foreground_child_group() != target.process_group
                or any(event.startswith((f"DONE:{target.command_id}:",
                                         f"ACTIVE_JOBS:{target.command_id}"))
                       for event in self.events)
            ):
                raise UnsafeShellState("confirmed foreground target changed")
            self.boundary.observe_user_bytes(data)
            view = memoryview(data)
            offset = 0
            deadline = time.monotonic() + 0.25
            while offset < len(view):
                try:
                    written = os.write(self.master_fd, view[offset:])
                except BlockingIOError:
                    written = 0
                except OSError:
                    self.boundary.fail_closed()
                    raise
                if written:
                    offset += written
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [self.master_fd], [], remaining)[1]:
                    self.boundary.fail_closed()
                    raise TimeoutError("partial foreground PTY write; remaining bytes withheld")

    def close(self) -> None:
        if self._closed:
            return
        groups = [group for group in (self.run_cgroup, self.manual_cgroup) if group]
        for group in groups:
            try:
                (group / "cgroup.kill").write_text("1")
            except OSError:
                try:
                    for raw_pid in (group / "cgroup.procs").read_text().split():
                        os.kill(int(raw_pid), signal.SIGKILL)
                except OSError:
                    pass
        super().close()
        deadline = time.monotonic() + 1.0
        residuals: list[Path] = []
        for group in groups:
            while time.monotonic() < deadline:
                try:
                    if not self._populated(group):
                        group.rmdir()
                        break
                except OSError:
                    break
                time.sleep(0.01)
            if group.exists():
                residuals.append(group)
        self.cleanup_residuals = tuple(residuals)
        if residuals:
            raise CleanupIncomplete(f"probe cgroups remain: {residuals}")
