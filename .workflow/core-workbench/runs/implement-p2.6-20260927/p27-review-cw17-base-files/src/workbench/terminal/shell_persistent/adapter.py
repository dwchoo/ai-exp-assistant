"""Persistent shell adapter over the verified G2 transport, not a Task runner.

Callers supply the user environment and validated approval/automation ports.
This adapter never approves a Task, infers business success, or replays a request.
Linux Bash/sh only; unsupported exec tracing remains UNKNOWN in G2.
"""
from __future__ import annotations

from dataclasses import asdict
import fcntl
import os
from pathlib import Path
import select
import shlex
import termios
import time

from workbench.contracts.ports_v2 import dispatch_allowed, parse_port
from workbench.terminal.shell_g2.control_probe import ControlWaitProbe, ForegroundTarget
from workbench.terminal.shell_g2.lifecycle import ManagedLifecycleProbe, managed_controller_source
from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState, select_shell


class _Transport(ManagedLifecycleProbe):
    # Reuse the bounded partial-write/foreground revalidation implementation.
    send_confirmed_foreground = ControlWaitProbe.send_confirmed_foreground

    def __init__(self, choice, environment):
        self.takeover_requested = False
        self.sent_id = None
        self.confirmed_target = None
        self._child_start = None
        self._parent_start = None
        super().__init__(choice, environment=environment)

    def _control_init_source(self):
        # Private control paths are non-exported shell locals, not user env.
        root = Path(self._init_dir.name)
        setup = ("unset BOUNDARY_TRAPS BOUNDARY_TRAPS_AFTER BOUNDARY_PREPARED\n"
                 f"BOUNDARY_TRAPS={shlex.quote(str(root / 'before'))}\n"
                 f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(root / 'after'))}\n"
                 "BOUNDARY_PREPARED=persistent\n")
        return setup + managed_controller_source(self.choice.executable, handoff_checks=True)

    def _on_control_event(self, event):
        super()._on_control_event(event)
        if event.startswith("EXEC_READY:") and not self.lifecycle.unknown:
            try:
                self._child_start = self._proc(self.lifecycle.child_pid)[19]
            except (OSError, ValueError, IndexError):
                # An already-reaped short command is a valid G2 completion.
                # A live foreground target still cannot be confirmed without
                # this identity, so input remains held closed.
                self._child_start = None
        if event.startswith(("MAIN_RETURN:", "MAIN_STOPPED:")):
            self.confirmed_target = None
        if event in {"CONTROL_LOST", "HOOK_LOST", "HOOK_REJECTED", "SHELL_EXIT"}:
            self.confirmed_target = None
            self.manual_prompt_confirmed = False

    @staticmethod
    def _proc(pid):
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()

    def _current_foreground_child_group(self):
        life = self.lifecycle
        if (not life.experiment_started or life.main_exit is not None or life.main_stopped
                or life.unknown or self.boundary.owner != "user"
                or self.boundary.uncertain or self.boundary.needs_review):
            return None
        try:
            fields = self._proc(life.child_pid)
            if (fields[19] == self._child_start and fields[0] not in {"X", "Z", "T", "t"}
                    and int(fields[1]) == life.supervisor_pid
                    and int(fields[3]) == self.pid and int(fields[2]) == life.child_group
                    and self._foreground_group() == life.child_group):
                return life.child_group
        except (OSError, ValueError, IndexError, TypeError):
            pass
        return None

    def _manual_prompt_is_current(self):
        life, boundary = self.lifecycle, self.boundary
        if (self._closed or not self.manual_prompt_confirmed or boundary.owner != "user"
                or boundary.uncertain or boundary.needs_review or life.unknown
                or (life.request_id is not None and not life.returned)
                or self._foreground_group() != self.pid):
            return False
        try:
            fields = self._proc(self.pid)
            return (fields[19] == self._parent_start and fields[0] not in {"X", "Z", "T", "t"}
                    and int(fields[1]) == os.getpid() and int(fields[2]) == self.pid
                    and int(fields[3]) == self.pid)
        except (OSError, ValueError, IndexError, TypeError):
            return False

    def send_manual_parent(self, data):
        """Write only to the still-verified parent, with a bounded retry window."""
        with self.boundary.lock:
            view = memoryview(data)
            offset = 0
            deadline = time.monotonic() + 0.25
            try:
                while offset < len(view):
                    if time.monotonic() >= deadline:
                        self.boundary.fail_closed()
                        raise TimeoutError("partial manual PTY write; remaining bytes withheld")
                    try:
                        self._drain(0)
                    except OSError as exc:
                        self.lifecycle.fail_unknown("control_observation_lost")
                        self.boundary.fail_closed()
                        raise UnsafeShellState("manual input boundary unavailable") from exc
                    if not self._manual_prompt_is_current():
                        if offset:
                            self.boundary.fail_closed()
                        raise UnsafeShellState("manual prompt target changed")
                    try:
                        written = os.write(self.master_fd, view[offset:])
                    except (InterruptedError, BlockingIOError):
                        written = 0
                    except OSError:
                        self.boundary.fail_closed()
                        raise
                    if written:
                        self.boundary.observe_user_bytes(bytes(view[offset:offset + written]))
                        offset += written
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.boundary.fail_closed()
                        raise TimeoutError("partial manual PTY write; remaining bytes withheld")
                    try:
                        ready = select.select([], [self.master_fd], [], remaining)[1]
                    except InterruptedError:
                        continue
                    except OSError:
                        self.boundary.fail_closed()
                        raise
                    if not ready:
                        self.boundary.fail_closed()
                        raise TimeoutError("partial manual PTY write; remaining bytes withheld")
            finally:
                view.release()

    def release_idle(self):
        if self.lifecycle.request_id is not None or not self.control_wait_seen:
            raise UnsafeShellState("not an idle parent control wait")
        self._flush_takeover()

    def _flush_takeover(self):
        fd = fcntl.ioctl(self.master_fd, 0x5441, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        finally:
            os.close(fd)
        if os.write(self._request_fd, b"TAKEOVER\n") != len(b"TAKEOVER\n"):
            self.lifecycle.fail_unknown("takeover_write_failure")
            raise OSError("partial takeover; no retry")


class PersistentShell:
    """One serialized writer, one outstanding request, explicit takeover state."""
    def __init__(self, *, user_environment: dict[str, str], choice: ShellChoice | None = None):
        if not isinstance(user_environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in user_environment.items()):
            raise ValueError("explicit string user environment required")
        selected = choice or select_shell(user_environment.get("PATH", ""))
        if selected.kind not in {"bash", "sh"} or Path(os.path.realpath(selected.executable)).name not in {"bash", "sh", "dash"}:
            raise ValueError("only verified Bash/sh supported")
        self._transport = _Transport(selected, dict(user_environment))
        self._seen = set()
        self._takeover_sent = False
        self._approval_hash = None
        try:
            self._transport.wait_ready()
            try:
                self._transport._parent_start = self._transport._proc(self._transport.pid)[19]
            except (OSError, ValueError, IndexError) as exc:
                raise UnsafeShellState("parent shell identity unconfirmed") from exc
            self._transport.take_user_control()
            self._transport.manual_prompt_confirmed = True
        except BaseException:
            self.close()
            raise

    @property
    def parent_pid(self):
        return self._transport.pid

    def poll(self, timeout=0):
        t = self._transport
        with t.boundary.lock:
            t._drain(timeout)
            if t.confirmed_target is not None and t._current_foreground_child_group() != t.confirmed_target.process_group:
                t.confirmed_target = None
            if t.takeover_requested and not self._takeover_sent and t.control_wait_seen and (
                    t.lifecycle.request_id is None or t.lifecycle.returned):
                self._takeover_sent = True  # A failed send is never retried.
                try:
                    t.release_idle() if t.lifecycle.request_id is None else t.release_control()
                except OSError:
                    t.lifecycle.fail_unknown("takeover_write_failure")
                    t.boundary.fail_closed()
                    raise
            return self.snapshot()

    def snapshot(self):
        t, life = self._transport, self._transport.lifecycle
        phase = ("unknown" if life.unknown or t.boundary.uncertain or t.boundary.needs_review else "control_returned" if life.control_returned else
                 "input_returned" if life.input_returned else "lifetime_ended" if life.lifetime == "ended" else
                 "main_returned" if life.main_exit is not None else "experiment_started" if life.experiment_started else
                 "supervisor_started" if life.supervisor_pid else "accepted" if life.accepted else "not_sent")
        reasons = []
        if t.takeover_requested: reasons.append("takeover_requested")
        if t.boundary.owner != "manager": reasons.append("user_owner")
        if not t.control_wait_seen: reasons.append("parent_not_control_wait")
        if t.boundary.pending_line or t.boundary.submitted_lines: reasons.append("unsubmitted_or_unconsumed_input")
        if t.boundary._job_pids: reasons.append("manual_jobs")
        if t.boundary.uncertain or t.boundary.needs_review or life.unknown: reasons.append("unknown_or_manual_residue")
        if "HOOK_REJECTED" in t.events: reasons.append("unsupported_hook_or_trap")
        if life.request_id is not None: reasons.append("outstanding_request")
        foreground = t._foreground_group()
        mode = ("control_wait" if t.control_wait_seen and not t.manual_prompt_confirmed else
                "manual_foreground" if t.manual_prompt_confirmed and foreground != t.pid else
                "manual_prompt" if t.manual_prompt_confirmed and t.boundary.ready and not t.boundary.submitted_lines and not t.boundary.pending_line else
                "manual_input" if t.manual_prompt_confirmed else "unknown")
        return {"parent_pid": t.pid, "generation": t.generation, "input_owner": t.boundary.owner,
                "owner_epoch": t.owner_epoch, "parent_mode": mode, "foreground_group": foreground,
                "takeover_requested": t.takeover_requested, "takeover_confirmed": t.confirmed_target is not None or (t.takeover_requested and mode == "manual_prompt" and phase != "unknown"),
                "foreground_target": asdict(t.confirmed_target) if t.confirmed_target else None,
                "phase": phase, "lifecycle": asdict(life), "held_reasons": reasons, "task_success": None}

    def request_takeover(self):
        t = self._transport
        with t.boundary.lock:
            self.poll()
            if t.lifecycle.request_id is None and t.boundary.owner == "user" and t.manual_prompt_confirmed:
                return self.snapshot()
            t.takeover_requested = True
            t.take_user_control()
            self.poll()
            return self.snapshot()

    def confirm_takeover(self):
        t = self._transport
        with t.boundary.lock:
            self.poll()
            if not t.takeover_requested:
                raise UnsafeShellState("no takeover request")
            if self.snapshot()["parent_mode"] == "manual_prompt":
                return self.snapshot()
            group = t._current_foreground_child_group()
            if group is None or t.sent_id is None:
                raise UnsafeShellState("foreground target unknown; input held")
            t.confirmed_target = ForegroundTarget(t.sent_id, group)
            return self.snapshot()

    def send_user(self, data: bytes):
        t = self._transport
        with t.boundary.lock:
            try:
                self.poll()
            except OSError as exc:
                t.lifecycle.fail_unknown("control_observation_lost")
                t.boundary.fail_closed()
                raise UnsafeShellState("manual input boundary unavailable") from exc
            if (t.boundary.owner != "user" or t.boundary.uncertain
                    or t.boundary.needs_review or t.lifecycle.unknown):
                raise UnsafeShellState("manual input boundary unknown")
            if t.confirmed_target is not None:
                t.send_confirmed_foreground(data)
            elif t._manual_prompt_is_current():
                t.send_manual_parent(data)
            else:
                raise UnsafeShellState("no confirmed manual input target")

    def claim_manager(self):
        t = self._transport
        with t.boundary.lock:
            self.poll()
            if not t.control_wait_seen or not t.boundary._handoff_ready or t.boundary.uncertain or t.boundary.needs_review:
                raise UnsafeShellState("no clean direct same-parent handoff")
            if t.lifecycle.request_id is not None:
                t.rearm_after_handoff()
            t.return_to_manager()
            t.takeover_requested = False
            t.confirmed_target = None
            t.sent_id = None
            self._takeover_sent = False
            return self.snapshot()

    def submit(self, control: dict, command: str | list[str], automation: dict, *, return_timeout=5.0):
        value = parse_port(control)
        if value["kind"] != "ShellControl" or value["payload"]["phase"] != "accepted" or not dispatch_allowed(automation):
            raise UnsafeShellState("approval/automation port held")
        p, t = value["payload"], self._transport
        with t.boundary.lock:
            self.poll()
            if (t.takeover_requested or p["requestId"] in self._seen or p["parentPid"] != t.pid
                    or not t.boundary.can_dispatch(generation=p["generation"], owner_epoch=p["ownerEpoch"])
                    or t.lifecycle.request_id is not None):
                raise UnsafeShellState("stale, outstanding, unknown or dirty shell; no replay")
            # Use literal argv even for scripts; do not expose fixture directives.
            argv = [t.choice.executable, "-c", command] if isinstance(command, str) else command
            self._seen.add(p["requestId"])
            t.sent_id = p["requestId"]
            self._approval_hash = p["approvalHash"]
            t.dispatch_managed(p["requestId"], argv, return_timeout=return_timeout)
            return self.snapshot()

    def release_input(self):
        self._transport.release_input()

    def display_bytes(self):
        return self._transport.display_bytes()

    def close(self):
        self._transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
