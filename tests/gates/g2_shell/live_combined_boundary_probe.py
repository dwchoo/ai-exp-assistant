"""Bounded CW-03 PTY, signal, subreaper and input-return discriminator.

Run: python tests/gates/g2_shell/live_combined_boundary_probe.py
This is a feasibility probe for one fixed supervisor, not a production adapter.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
import errno
import json
import os
from pathlib import Path
import resource
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.gates.g2_shell import live_supervisor_boundary_probe as boundary
from tests.gates.g2_shell.live_subreaper_lifetime_probe import proc_fields


TIMEOUT = 5.0


class Inconclusive(RuntimeError):
    """A native shell behavior could not be proved by this fixture."""


def signal_snapshot(pid: int) -> dict:
    """Record disposition and job-control identity without inferring suspension."""
    status = dict(line.split(":", 1) for line in
                  Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)
    ignored = int(status["SigIgn"].strip(), 16)
    caught = int(status["SigCgt"].strip(), 16)
    blocked = int(status["SigBlk"].strip(), 16)
    bit = 1 << (signal.SIGTSTP - 1)
    ppid = proc_fields(pid)[0]
    return {"pid": pid, "state": proc_fields(pid)[3], "ppid": ppid,
            "pgid": os.getpgid(pid), "sid": os.getsid(pid),
            "parent_pgid": os.getpgid(ppid), "parent_sid": os.getsid(ppid),
            "tstp_ignored": bool(ignored & bit), "tstp_caught": bool(caught & bit),
            "tstp_blocked": bool(blocked & bit)}


def emit(event: str) -> None:
    os.write(9, (event + "\n").encode("ascii"))


def enable_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                           ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    value = ctypes.c_int(-1)
    if libc.prctl(37, ctypes.addressof(value), 0, 0, 0) != 0 or value.value != 1:
        raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER")


def fork_tree() -> None:
    release_fd = int(os.environ["WB_RELEASE_FD"])
    middle = os.fork()
    if middle == 0:
        child = os.fork()
        if child == 0:
            os.setsid()
            pid = os.getpid()
            emit(f"DESCENDANT:{pid}:{proc_fields(pid)[2]}:{os.getsid(0)}")
            ready, _, _ = select.select([release_fd], [], [], TIMEOUT)
            os._exit(23 if ready and os.read(release_fd, 1) == b"R" else 70)
        emit(f"MIDDLE_FORKED:{os.getpid()}:{child}")
        os._exit(19)
    os.close(release_fd)
    _, status = os.waitpid(middle, 0)
    emit(f"MIDDLE_REAPED:{middle}:{os.waitstatus_to_exitcode(status)}")
    os._exit(17)


def blocking_child() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {
        signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP, signal.SIGCONT,
    })
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    emit(f"SCRIPT_READY:{os.getpid()}")
    signal.pause()


def orphan_child(delayed: bool) -> None:
    child = os.fork()
    if child == 0:
        os.setsid()
        if delayed:
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR2})
        pid = os.getpid()
        emit(f"ORPHAN_READY:{pid}:{proc_fields(pid)[2]}")
        if delayed and signal.sigtimedwait({signal.SIGUSR2}, TIMEOUT) is None:
            os._exit(70)
        os._exit(29)
    os._exit(17)


def await_release(signum: signal.Signals) -> None:
    deadline = time.monotonic() + TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or signal.sigtimedwait({signum}, remaining) is None:
            raise TimeoutError(f"release signal not received: {signum.name}")
        return


def supervisor(shell: str, payload: str) -> int:
    enable_subreaper()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if signal.getsignal(signal.SIGCHLD) is not signal.SIG_DFL:
        raise RuntimeError("SIGCHLD is not default")
    interrupts = 0

    def on_interrupt(_signum: int, _frame: object) -> None:
        nonlocal interrupts
        interrupts += 1
        emit("SIGNAL_INT")

    def on_quit(_signum: int, _frame: object) -> None:
        emit("SIGNAL_QUIT")

    def on_tstp(_signum: int, _frame: object) -> None:
        emit("SIGNAL_TSTP")
        # The marker alone is not suspension evidence. Re-deliver native TSTP
        # with its default disposition; do not replace it with SIGSTOP.
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)
        signal.signal(signal.SIGTSTP, on_tstp)

    def on_cont(_signum: int, _frame: object) -> None:
        emit("SIGNAL_CONT")

    signal.signal(signal.SIGINT, on_interrupt)
    signal.signal(signal.SIGQUIT, on_quit)
    signal.signal(signal.SIGTSTP, on_tstp)
    signal.signal(signal.SIGCONT, on_cont)
    read_fd, write_fd = os.pipe()
    env = os.environ.copy()
    env["WB_RELEASE_FD"] = str(read_fd)
    script = base64.b64decode(payload, validate=True).decode("utf-8")
    start_hold = script.startswith("#WB_START_HOLD\n")
    if start_hold:
        script = script.removeprefix("#WB_START_HOLD\n")
    identity_hold = script.startswith("#WB_CHILD_ID_HOLD\n")
    if identity_hold:
        script = script.removeprefix("#WB_CHILD_ID_HOLD\n")
    substitute = None
    if script.startswith("#WB_SUBSTITUTE_EXEC:"):
        directive, _, script = script.partition("\n")
        substitute = directive.partition(":")[2]
    tree_fixture = script.startswith("#WB_TREE_FIXTURE\n")
    emit(f"SUPERVISOR:{os.getpid()}:{os.getpgrp()}:SUBREAPER=1:SIGCHLD=DFL")
    if start_hold:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR2})
        emit("START_BARRIER")
        await_release(signal.SIGUSR2)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    if identity_hold:
        previous_identity_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR2})
    if script.startswith("#WB_ARGV_JSON\n"):
        argv = json.loads(script.removeprefix("#WB_ARGV_JSON\n"))
        if not isinstance(argv, list) or not argv or not all(
            isinstance(arg, str) for arg in argv
        ):
            raise ValueError("argv fixture requires a nonempty string list")
        emit(f"LAUNCH:argv:{os.path.realpath(argv[0])}")
        child = subprocess.Popen(argv, env=env, pass_fds=(9, read_fd))
        argv_launch = True
    else:
        argv_launch = False
        emit(f"LAUNCH:script:{os.path.realpath(shell)}")
        if identity_hold:
            script = ('printf "CHILD_READY:%s\\n" "$$" >&9; '
                      'IFS= read -r __wb_identity_release <&"$WB_RELEASE_FD"; '
                      '[ "$__wb_identity_release" = R ] || exit 72; '
                      'printf "EXPERIMENT_START:%s\\n" "$$" >&9; ' + script)
        else:
            script = ('IFS= read -r __wb_start_release <&"$WB_RELEASE_FD"; '
                      '[ "$__wb_start_release" = R ] || exit 72; '
                      'printf "EXPERIMENT_START:%s\\n" "$$" >&9; ' + script)
        child = subprocess.Popen([substitute or shell, "-c", script],
                                 env=env, pass_fds=(9, read_fd))
    os.close(read_fd)
    emit(f"CHILD:{child.pid}:{os.getpgid(child.pid)}")
    if argv_launch:
        # Popen confirms executable invocation, not completion or success.
        emit(f"EXPERIMENT_START:{child.pid}")
    elif not identity_hold:
        os.write(write_fd, b"R\n")
    if identity_hold:
        await_release(signal.SIGUSR2)
        os.write(write_fd, b"R\n")
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_identity_mask)
    signal.pthread_sigmask(signal.SIG_BLOCK, {
        signal.SIGCHLD, signal.SIGUSR1, signal.SIGUSR2,
    })
    deadline = time.monotonic() + TIMEOUT
    while True:
        pid, result = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
        if pid:
            if os.WIFSTOPPED(result):
                emit(f"MAIN_STOPPED:{os.WSTOPSIG(result)}")
            elif os.WIFCONTINUED(result):
                emit("MAIN_CONTINUED")
            else:
                status = os.waitstatus_to_exitcode(result)
                break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("main program did not return")
        signal.sigtimedwait({signal.SIGCHLD}, remaining)
    emit(f"MAIN_RETURN:{status}")
    released_tree = False
    active_reported = False
    deadline = time.monotonic() + TIMEOUT
    while True:
        try:
            pid, result = os.waitpid(-1, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
        except ChildProcessError as exc:
            if exc.errno != errno.ECHILD:
                raise
            emit("WAIT_EMPTY:ECHILD")
            break
        if pid:
            if os.WIFSTOPPED(result):
                emit(f"DESCENDANT_STOPPED:{pid}:{os.WSTOPSIG(result)}")
            elif os.WIFCONTINUED(result):
                emit(f"DESCENDANT_CONTINUED:{pid}")
            else:
                emit(f"DESCENDANT_REAPED:{pid}:{os.waitstatus_to_exitcode(result)}")
            continue
        if not active_reported:
            emit("LIFETIME_ACTIVE:WNOHANG=0")
            active_reported = True
        if tree_fixture and not released_tree:
            await_release(signal.SIGUSR2)
            os.write(write_fd, b"R")
            released_tree = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("managed descendant remained active")
        signal.sigtimedwait({signal.SIGCHLD}, remaining)
    os.close(write_fd)
    emit(f"LIFETIME_DONE:INTERRUPTS={interrupts}")
    emit("INPUT_BARRIER")
    await_release(signal.SIGUSR1)
    emit("INPUT_RELEASED")
    return 128 - status if status < 0 else status


def wait(session: boundary.prototype.ShellProcess, prefix: str, since: int) -> str:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        session._drain(min(0.03, deadline - time.monotonic()))
        session.display_bytes()
        for event in session.events[since:]:
            if event.startswith(prefix):
                return event
    raise TimeoutError(f"missing event {prefix}; observed={session.events[since:]}")


@contextmanager
def cleanup_owned(identities: list[tuple[int, int]]):
    try:
        yield
    finally:
        for pid, started in reversed(identities):
            try:
                if proc_fields(pid)[2] == started:
                    os.kill(pid, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass


def controller_source(shell: str) -> str:
    source = boundary.controller_init(shell)
    original_path = shlex.quote(str(Path(boundary.__file__).resolve()))
    source = source.replace(original_path, shlex.quote(str(Path(__file__).resolve())))
    venv_ok = ('[ -n "$VIRTUAL_ENV" ] && [ -x "$VIRTUAL_ENV/bin/python" ] && '
               '[ -n "$VIRTUAL_ENV_PROMPT" ]')
    prompt_ok = (
        '{ [ "$PROMPT_COMMAND" = "__b_emit READY" ] || '
        '[ "$PROMPT_COMMAND" = "__b_emit PRIOR_HOOK; __b_emit READY" ]; } && '
        '{ [ "$PS1" = \'$ \' ] || '
        f'{{ {venv_ok} && [ "$PS1" = "$VIRTUAL_ENV_PROMPT"\'$ \' ]; }}; }}'
        if Path(shell).name == "bash" else
        '{ [ "$PS1" = \'$( __b_emit READY; printf "$ " )\' ] || '
        '[ "$PS1" = \'$( __b_emit PRIOR_HOOK; __b_emit READY; printf "$ " )\' ] || '
        f'{{ {venv_ok} && '
        '{ [ "$PS1" = "$VIRTUAL_ENV_PROMPT"\'$( __b_emit READY; printf "$ " )\' ] || '
        '[ "$PS1" = "$VIRTUAL_ENV_PROMPT"\'$( __b_emit PRIOR_HOOK; __b_emit READY; printf "$ " )\' ]; }; }; }'
    )
    return source + f'''
__b_hook_compatible() {{
    {prompt_ok} || return 1
    case "$(cat "$BOUNDARY_TRAPS")" in *CHLD*) return 1;; esac
    return 0
}}
__b_loop() {{
    trap > "$BOUNDARY_TRAPS"
    __b_interrupted=0
    trap '__b_interrupted=1; __b_emit PARENT_INT' INT
    __b_emit "WAIT:$$:$PWD:$BOUNDARY_PREPARED"
    while :; do
        __b_interrupted=0
        if IFS= read -r __b_line <&8; then
            case $__b_line in
                RUN:*)
                    __b_payload=${{__b_line#RUN:}}
                    if ! __b_hook_compatible; then
                        __b_emit HOOK_LOST
                        __b_emit HOOK_REJECTED
                        continue
                    fi
                    __b_emit START
                    if {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} --supervisor {shlex.quote(shell)} "$__b_payload"; then
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
        elif [ "$__b_interrupted" -eq 1 ]; then
            __b_emit WAIT_INTERRUPTED
        else
            __b_emit CONTROL_LOST
            while :; do sleep 1; done
        fi
    done
}}
__b_handoff() {{ __b_emit "HANDOFF:$$"; __b_loop; }}
alias wb-handoff=__b_handoff
'''


class PreviousWriter:
    """One pending host write from the owner that is being returned."""

    def __init__(self, pending: bytes) -> None:
        self.pending = pending
        self.closed = False

    def close_and_discard(self) -> None:
        self.closed = True
        self.pending = b""

    def drain(self, session: boundary.prototype.ShellProcess) -> bool:
        if self.closed or not self.pending:
            return False
        session._write_all(self.pending)
        self.pending = b""
        return True


def case(shell: str, *, negative: bool) -> dict:
    script = ("#WB_TREE_FIXTURE\n"
              f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} --fork-tree")
    source = controller_source(shell)
    identities: list[tuple[int, int]] = []
    with patch.object(boundary.prototype, "_bash_control_init", return_value=source), \
         patch.object(boundary.prototype, "_sh_control_init", return_value=source), \
         tempfile.TemporaryDirectory(prefix="cw03-combined-") as directory, \
         boundary.prototype.ShellProcess(
             boundary.prototype.ShellChoice("bash" if Path(shell).name == "bash" else "sh", shell),
             control_wait=True,
         ) as session, cleanup_owned(identities):
        wait(session, "READY", 0)
        parent = session.pid
        marker = Path(directory) / "spill"
        queue_marker = Path(directory) / "queued-spill"
        previous_writer = PreviousWriter(b"\n")
        following = Path(directory) / "handoff-following"
        traps_before = Path(directory) / "traps-before"
        traps_after = Path(directory) / "traps-after"
        prepared = (f"cd {shlex.quote(directory)}; export BOUNDARY_PREPARED=kept "
                    f"BOUNDARY_TRAPS={shlex.quote(str(traps_before))} "
                    f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(traps_after))}; wb-handoff\n")
        if not negative:
            prepared += f": > {shlex.quote(str(following))}\n"
        session._write_all(prepared.encode())
        if not negative:
            wait(session, f"HANDOFF:{parent}", 0)
        first = wait(session, "WAIT:", 0)
        assert first == f"WAIT:{parent}:{directory}:kept", first
        assert not following.exists(), "following pasted line executed before control wait"
        since = len(session.events)
        payload = base64.b64encode(script.encode()).decode("ascii")
        os.write(session._request_fd, f"RUN:{payload}\n".encode())
        wait(session, "START", since)
        sup = wait(session, "SUPERVISOR:", since)
        supervisor_pid = int(sup.split(":")[1])
        identities.append((supervisor_pid, proc_fields(supervisor_pid)[2]))
        child = wait(session, "CHILD:", since)
        child_pid = int(child.split(":")[1])
        descendant = wait(session, "DESCENDANT:", since)
        _, raw_pid, raw_start, raw_session = descendant.split(":")
        descendant_pid = int(raw_pid)
        identities.append((descendant_pid, int(raw_start)))
        wait(session, "MIDDLE_REAPED:", since)
        main = wait(session, "MAIN_RETURN:", since)
        active = wait(session, "LIFETIME_ACTIVE:", since)
        adopted = proc_fields(descendant_pid)
        assert adopted[0] == supervisor_pid and adopted[2] == int(raw_start)
        assert adopted[1] == descendant_pid == int(raw_session)
        assert main == "MAIN_RETURN:17" and active == "LIFETIME_ACTIVE:WNOHANG=0"
        assert "LIFETIME_DONE:INTERRUPTS=0" not in session.events[since:]
        assert boundary.foreground_group(session) == int(sup.split(":")[2])
        assert int(sup.split(":")[2]) != os.getpgid(parent)
        if not negative:
            session._write_all(b"\x03")
            wait(session, "SIGNAL_INT", since)
        session._write_all(f": > {shlex.quote(str(marker))}\n".encode())
        session._write_all(f": > {shlex.quote(str(queue_marker))}".encode())
        os.kill(supervisor_pid, signal.SIGUSR2)
        reaped = wait(session, "DESCENDANT_REAPED:", since)
        empty = wait(session, "WAIT_EMPTY:", since)
        lifetime = wait(session, "LIFETIME_DONE:", since)
        wait(session, "INPUT_BARRIER", since)
        assert reaped == f"DESCENDANT_REAPED:{descendant_pid}:23", reaped
        assert empty == "WAIT_EMPTY:ECHILD"
        assert lifetime == f"LIFETIME_DONE:INTERRUPTS={0 if negative else 1}"
        assert "READY" not in session.events[since:]
        if not negative:
            previous_writer.close_and_discard()
            boundary.flush_queued_pty(session)
        os.kill(supervisor_pid, signal.SIGUSR1)
        wait(session, "INPUT_RELEASED", since)
        returned = wait(session, "RETURN:", since)
        back = wait(session, "WAIT:", since)
        assert returned == "RETURN:17" and back == first
        assert session.pid == parent and os.readlink(f"/proc/{parent}/cwd") == directory
        assert boundary.foreground_group(session) == os.getpgid(parent)
        assert not any(Path(f"/proc/{pid}").exists() for pid in (supervisor_pid, child_pid, descendant_pid))
        if not negative:
            boundary.flush_queued_pty(session)
        os.write(session._request_fd, b"TAKEOVER\n")
        wait(session, "TAKEOVER_ACK:", since)
        wait(session, "READY", since)
        assert traps_before.read_bytes() == traps_after.read_bytes(), "signal trap not restored"
        assert previous_writer.drain(session) == negative
        if negative:
            deadline = time.monotonic() + TIMEOUT
            while not queue_marker.exists() and time.monotonic() < deadline:
                session._drain(min(0.03, deadline - time.monotonic()))
                session.display_bytes()
            assert queue_marker.exists(), "queued marker did not execute in negative control"
        elif marker.exists() or following.exists() or queue_marker.exists():
            raise AssertionError("input control mismatch: stale marker reached prompt")
        session._write_all(b"__b_emit NEW_INPUT\n")
        try:
            wait(session, "NEW_INPUT", since)
        except TimeoutError as exc:
            raise AssertionError("input control mismatch: new input was not processed") from exc
        session._drain(0.05)
        session.display_bytes()
        spilled = marker.exists()
        assert spilled == negative, f"input control mismatch: negative={negative}, spilled={spilled}"
        assert queue_marker.exists() == negative, "prior writer queue leaked or negative missed"
        assert not following.exists(), "following pasted line escaped the input-return barrier"
        later = session.events[since:]
        ordered = ["START", sup, child, main, active]
        if not negative:
            ordered.append("SIGNAL_INT")
        ordered += [reaped, empty, lifetime, "INPUT_BARRIER", "INPUT_RELEASED",
                    returned, back, f"TAKEOVER_ACK:{parent}", "READY", "NEW_INPUT"]
        assert [later.index(event) for event in ordered] == sorted(
            later.index(event) for event in ordered
        ), later
        assert later.index(sup) < later.index(descendant) < later.index(reaped), later
        return {"shell": Path(shell).name, "negative": negative,
                "barriers": ["main_return", "descendant_live",
                             "signal_observed" if not negative else "no_signal_control",
                             "descendant_reaped", "wait_empty", "input_barrier",
                             "input_released", "parent_wait", "prompt", "new_input"],
                "marker_spilled": spilled, "parent_pid_preserved": session.pid == parent,
                "following_line_suppressed": not following.exists(),
                "queued_marker_spilled": queue_marker.exists(),
                "trap_restored": True,
                "owned_pids": [parent, supervisor_pid, child_pid, descendant_pid]}


def simple_case(shell: str, name: str) -> dict:
    source = controller_source(shell)
    identities: list[tuple[int, int]] = []
    with patch.object(boundary.prototype, "_bash_control_init", return_value=source), \
         patch.object(boundary.prototype, "_sh_control_init", return_value=source), \
         tempfile.TemporaryDirectory(prefix="cw03-simple-") as directory, \
         boundary.prototype.ShellProcess(
             boundary.prototype.ShellChoice("bash" if Path(shell).name == "bash" else "sh", shell),
             control_wait=True,
         ) as session, cleanup_owned(identities):
        wait(session, "READY", 0)
        parent = session.pid
        tail = Path(directory) / "tail"
        spill = Path(directory) / "spill"
        traps_before = Path(directory) / "traps-before"
        traps_after = Path(directory) / "traps-after"
        prior_trap = "trap '__b_emit PRIOR_INT' INT; " if name == "parent_wait_prior" else ""
        errexit = "set -e; " if name in {"errexit", "errexit_interrupt"} else ""
        session._write_all(
            (f"cd {shlex.quote(directory)}; export BOUNDARY_PREPARED=kept "
             f"BOUNDARY_TRAPS={shlex.quote(str(traps_before))} "
             f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(traps_after))}; "
             f"{prior_trap}{errexit}wb-handoff\n").encode()
        )
        wait(session, f"HANDOFF:{parent}", 0)
        first = wait(session, "WAIT:", 0)
        assert first == f"WAIT:{parent}:{directory}:kept"
        since = len(session.events)
        script = ("cd /; export BOUNDARY_PREPARED=changed; return" if name == "return"
                  else "exec true" if name == "exec"
                  else "exit 37" if name == "errexit"
                  else "#WB_START_HOLD\n:" if name == "start"
                  else ":" if name.startswith("parent_wait")
                  else (f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
                        f"--{name.replace('_', '-')}") if name in {"orphan_immediate", "orphan_delayed"}
                  else (f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
                        f"--blocking-child; : > {shlex.quote(str(tail))}"))
        payload = base64.b64encode(script.encode()).decode("ascii")
        os.write(session._request_fd, f"RUN:{payload}\n".encode())
        wait(session, "START", since)
        sup = wait(session, "SUPERVISOR:", since)
        supervisor_pid = int(sup.split(":")[1])
        identities.append((supervisor_pid, proc_fields(supervisor_pid)[2]))
        if name == "start":
            wait(session, "START_BARRIER", since)
            assert boundary.foreground_group(session) == int(sup.split(":")[2])
            session._write_all(b"\x1a")
            wait(session, "SIGNAL_TSTP", since)
            assert not any(event.startswith(("CHILD:", "MAIN_RETURN:",
                                             "INPUT_BARRIER")) for event in session.events[since:])
            os.killpg(int(sup.split(":")[2]), signal.SIGCONT)
            wait(session, "SIGNAL_CONT", since)
            assert not any(event.startswith(("CHILD:", "MAIN_RETURN:",
                                             "INPUT_BARRIER")) for event in session.events[since:])
            os.kill(supervisor_pid, signal.SIGUSR2)
        child = wait(session, "CHILD:", since)
        child_pid = int(child.split(":")[1])
        blocking_pid: int | None = None
        if name in {"interrupt", "quit", "suspend", "errexit_interrupt"}:
            ready = wait(session, "SCRIPT_READY:", since)
            blocking_pid = int(ready.split(":")[1])
            blocking_parent, _, blocking_start, blocking_state = proc_fields(blocking_pid)
            assert blocking_parent == child_pid and blocking_state not in {"Z", "X"}
            identities.append((blocking_pid, blocking_start))
            assert boundary.foreground_group(session) == int(sup.split(":")[2])
            if name == "suspend":
                session._write_all(b"\x1a")
                wait(session, "SIGNAL_TSTP", since)
                deadline = time.monotonic() + TIMEOUT
                while proc_fields(blocking_pid)[3] != "T" and time.monotonic() < deadline:
                    session._drain(min(0.03, deadline - time.monotonic()))
                    session.display_bytes()
                assert proc_fields(blocking_pid)[3] == "T", "child did not stop"
                assert not any(event.startswith(("MAIN_RETURN:", "LIFETIME_DONE:",
                                                 "INPUT_BARRIER")) for event in session.events[since:])
                os.killpg(int(sup.split(":")[2]), signal.SIGCONT)
                wait(session, "SIGNAL_CONT", since)
                deadline = time.monotonic() + TIMEOUT
                while proc_fields(blocking_pid)[3] == "T" and time.monotonic() < deadline:
                    session._drain(min(0.03, deadline - time.monotonic()))
                    session.display_bytes()
                assert proc_fields(blocking_pid)[3] != "T", "child did not resume"
                assert boundary.foreground_group(session) == int(sup.split(":")[2])
                session._write_all(b"\x03")
                wait(session, "SIGNAL_INT", since)
            else:
                session._write_all(b"\x03" if name in {"interrupt", "errexit_interrupt"} else b"\x1c")
                wait(session, "SIGNAL_INT" if name in {"interrupt", "errexit_interrupt"} else "SIGNAL_QUIT", since)
        main = wait(session, "MAIN_RETURN:", since)
        if name in {"orphan_immediate", "orphan_delayed"}:
            orphan = wait(session, "ORPHAN_READY:", since)
            _, raw_pid, raw_start = orphan.split(":")
            orphan_pid = int(raw_pid)
            identities.append((orphan_pid, int(raw_start)))
            assert main == "MAIN_RETURN:17", main
            if name == "orphan_delayed":
                wait(session, "LIFETIME_ACTIVE:WNOHANG=0", since)
                adopted = proc_fields(orphan_pid)
                assert adopted[0] == supervisor_pid and adopted[2] == int(raw_start)
                os.kill(orphan_pid, signal.SIGUSR2)
            assert wait(session, "DESCENDANT_REAPED:", since) == (
                f"DESCENDANT_REAPED:{orphan_pid}:29"
            )
        wait(session, "WAIT_EMPTY:ECHILD", since)
        lifetime = wait(session, "LIFETIME_DONE:", since)
        wait(session, "INPUT_BARRIER", since)
        assert lifetime == f"LIFETIME_DONE:INTERRUPTS={int(name in {'interrupt', 'suspend', 'errexit_interrupt'})}"
        if name in {"interrupt", "suspend", "errexit_interrupt"}:
            assert main == "MAIN_RETURN:-2", main
            assert not tail.exists(), "Ctrl-C script tail executed"
        elif name == "quit":
            expected = ("MAIN_RETURN:0", True) if Path(shell).name == "bash" else (
                "MAIN_RETURN:-3", False
            )
            assert (main, tail.exists()) == expected, (main, tail.exists())
        elif name == "errexit":
            assert main == "MAIN_RETURN:37", main
            assert not tail.exists()
        else:
            assert main.startswith("MAIN_RETURN:")
            assert not tail.exists()
        assert "READY" not in session.events[since:]
        session._write_all(f": > {shlex.quote(str(spill))}\n".encode())
        boundary.flush_queued_pty(session)
        os.kill(supervisor_pid, signal.SIGUSR1)
        wait(session, "INPUT_RELEASED", since)
        returned = wait(session, "RETURN:", since)
        assert int(returned.split(":")[1]) == (128 - int(main.split(":")[1])
                                                   if int(main.split(":")[1]) < 0
                                                   else int(main.split(":")[1]))
        assert wait(session, "WAIT:", since) == first
        if name in {"errexit", "errexit_interrupt"}:
            assert returned == ("RETURN:37" if name == "errexit" else "RETURN:130")
            os.write(session._request_fd, b"PING\n")
            wait(session, "BAD_REQUEST", since)
            assert "READY" not in session.events[since:]
        if name.startswith("parent_wait"):
            assert boundary.foreground_group(session) == os.getpgid(parent)
            signal_since = len(session.events)
            key = {"parent_wait": b"\x03", "parent_wait_prior": b"\x03",
                   "parent_wait_quit": b"\x1c",
                   "parent_wait_suspend": b"\x1a"}[name]
            session._write_all(key)
            stopped = False
            if name == "parent_wait_suspend":
                deadline = time.monotonic() + TIMEOUT
                while time.monotonic() < deadline:
                    session._drain(min(0.03, deadline - time.monotonic()))
                    session.display_bytes()
                    stopped = proc_fields(parent)[3] == "T"
                    if stopped:
                        break
                if not stopped:
                    observation = signal_snapshot(parent)
                    os.killpg(os.getpgid(parent), signal.SIGCONT)
                    raise Inconclusive("parent WAIT TSTP produced no observed stopped state; "
                                       + json.dumps(observation, sort_keys=True))
                os.write(session._request_fd, b"PING\n")
                quiet_until = time.monotonic() + 0.1
                while time.monotonic() < quiet_until:
                    session._drain(min(0.03, quiet_until - time.monotonic()))
                    session.display_bytes()
                    assert proc_fields(parent)[3] == "T", "parent resumed before SIGCONT"
                    assert not any(event in {"READY", "BAD_REQUEST"} or
                                   event.startswith(("TAKEOVER_ACK:", "RETURN:", "WAIT:"))
                                   for event in session.events[signal_since:]), "control returned while stopped"
                os.killpg(os.getpgid(parent), signal.SIGCONT)
            else:
                os.write(session._request_fd, b"PING\n")
            deadline = time.monotonic() + TIMEOUT
            while time.monotonic() < deadline:
                session._drain(min(0.03, deadline - time.monotonic()))
                session.display_bytes()
                if "READY" in session.events[since:]:
                    raise AssertionError(f"parent left control wait after {name}")
                if "BAD_REQUEST" in session.events[since:]:
                    break
            else:
                raise TimeoutError(f"parent did not process control PING after {name}")
            if name in {"parent_wait", "parent_wait_prior"}:
                assert "PARENT_INT" in session.events[since:], "Ctrl-C trap was not observed"
        assert session.pid == parent and os.readlink(f"/proc/{parent}/cwd") == directory
        assert boundary.foreground_group(session) == os.getpgid(parent)
        assert not Path(f"/proc/{supervisor_pid}").exists()
        assert not Path(f"/proc/{child_pid}").exists()
        if blocking_pid is not None:
            assert not Path(f"/proc/{blocking_pid}").exists()
        if name in {"orphan_immediate", "orphan_delayed"}:
            assert not Path(f"/proc/{orphan_pid}").exists()
        boundary.flush_queued_pty(session)
        os.write(session._request_fd, b"TAKEOVER\n")
        wait(session, "TAKEOVER_ACK:", since)
        wait(session, "READY", since)
        assert traps_before.read_bytes() == traps_after.read_bytes(), "signal trap not restored"
        errexit_flags = None
        if name in {"errexit", "errexit_interrupt"}:
            flags_since = len(session.events)
            session._write_all(b'__b_emit "FLAGS:$-"\n')
            flags = wait(session, "FLAGS:", flags_since)
            assert "e" in flags.split(":", 1)[1], f"errexit lost: {flags}"
            errexit_flags = flags
        if name == "parent_wait":
            after_takeover = len(session.events)
            session._write_all(b"\x03")
            wait(session, "READY", after_takeover)
            assert "PARENT_INT" not in session.events[after_takeover:], "control trap leaked"
        after_takeover = len(session.events)
        session._write_all(
            b"kill -INT $$; __b_emit NEW_INPUT\n" if name == "parent_wait_prior"
            else b"__b_emit NEW_INPUT\n"
        )
        if name == "parent_wait_prior":
            wait(session, "PRIOR_INT", after_takeover)
            assert "PARENT_INT" not in session.events[after_takeover:], "control trap leaked"
        wait(session, "NEW_INPUT", since)
        session._drain(0.05)
        session.display_bytes()
        assert not spill.exists()
        return {"shell": Path(shell).name, "case": name, "main_return": main,
                "parent_return": returned, "tail_executed": tail.exists(),
                "marker_spilled": spill.exists(),
                "trap_restored": True,
                "errexit_flags": errexit_flags,
                "parent_stopped": stopped if name == "parent_wait_suspend" else None,
                "owned_pids": [pid for pid in (parent, supervisor_pid, child_pid, blocking_pid,
                                               orphan_pid if name.startswith("orphan_") else None)
                               if pid is not None]}


def signal_stage_case(shell: str, stage: str, signame: str) -> dict:
    """Inject one signal at a known uncreated or returned main-program barrier."""
    assert stage in {"start", "return"}
    assert signame in {"INT", "QUIT", "TSTP", "CONT"}
    source = controller_source(shell)
    identities: list[tuple[int, int]] = []
    with patch.object(boundary.prototype, "_bash_control_init", return_value=source), \
         patch.object(boundary.prototype, "_sh_control_init", return_value=source), \
         tempfile.TemporaryDirectory(prefix="cw03-signal-") as directory, \
         boundary.prototype.ShellProcess(
             boundary.prototype.ShellChoice("bash" if Path(shell).name == "bash" else "sh", shell),
             control_wait=True,
         ) as session, cleanup_owned(identities):
        wait(session, "READY", 0)
        parent = session.pid
        marker = Path(directory) / "stale-input"
        traps_before = Path(directory) / "traps-before"
        traps_after = Path(directory) / "traps-after"
        session._write_all(
            (f"cd {shlex.quote(directory)}; export BOUNDARY_PREPARED=kept "
             f"BOUNDARY_TRAPS={shlex.quote(str(traps_before))} "
             f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(traps_after))}; wb-handoff\n").encode()
        )
        wait(session, f"HANDOFF:{parent}", 0)
        first = wait(session, "WAIT:", 0)
        assert first == f"WAIT:{parent}:{directory}:kept"
        since = len(session.events)
        if stage == "start":
            script = "#WB_START_HOLD\n:"
        else:
            script = ("#WB_TREE_FIXTURE\n"
                      f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} --fork-tree")
        os.write(session._request_fd, f"RUN:{base64.b64encode(script.encode()).decode()}\n".encode())
        wait(session, "START", since)
        sup = wait(session, "SUPERVISOR:", since)
        supervisor_pid, supervisor_group = map(int, sup.split(":")[1:3])
        identities.append((supervisor_pid, proc_fields(supervisor_pid)[2]))
        child_pid = None
        descendant_pid = None
        if stage == "start":
            wait(session, "START_BARRIER", since)
            assert not any(event.startswith(("CHILD:", "MAIN_RETURN:", "INPUT_BARRIER"))
                           for event in session.events[since:])
        else:
            child_pid = int(wait(session, "CHILD:", since).split(":")[1])
            descendant = wait(session, "DESCENDANT:", since)
            _, raw_pid, raw_start, raw_session = descendant.split(":")
            descendant_pid = int(raw_pid)
            identities.append((descendant_pid, int(raw_start)))
            assert wait(session, "MAIN_RETURN:", since) == "MAIN_RETURN:17"
            assert wait(session, "LIFETIME_ACTIVE:", since) == "LIFETIME_ACTIVE:WNOHANG=0"
            adopted = proc_fields(descendant_pid)
            assert adopted[0] == supervisor_pid and adopted[2] == int(raw_start)
            assert adopted[1] == descendant_pid == int(raw_session)
            assert adopted[3] not in {"Z", "X"}
        assert boundary.foreground_group(session) == supervisor_group
        assert supervisor_group != os.getpgid(parent)
        session._write_all(f": > {shlex.quote(str(marker))}\n".encode())
        signal_since = len(session.events)
        if signame == "CONT":
            os.killpg(supervisor_group, signal.SIGCONT)
        else:
            session._write_all({"INT": b"\x03", "QUIT": b"\x1c", "TSTP": b"\x1a"}[signame])
        wait(session, f"SIGNAL_{signame}", signal_since)
        stage_events = session.events[since:]
        stage_barrier = ("START_BARRIER" if stage == "start"
                         else "LIFETIME_ACTIVE:WNOHANG=0")
        observed_signals = [event for event in stage_events if event.startswith("SIGNAL_")]
        assert observed_signals == [f"SIGNAL_{signame}"], (
            f"signal observation mismatch at {stage}: {observed_signals}"
        )
        assert stage_barrier in stage_events, f"missing {stage} barrier event"
        assert stage_events.index(stage_barrier) < stage_events.index(observed_signals[0]), (
            f"signal preceded {stage} barrier"
        )
        assert "INPUT_BARRIER" not in stage_events
        if stage == "return":
            assert stage_events.index("MAIN_RETURN:17") < stage_events.index(stage_barrier)
            assert not any(event.startswith("DESCENDANT_REAPED:")
                           for event in stage_events), "signal arrived after descendant reap"
        if "CONTROL_LOST" in session.events[since:]:
            raise Inconclusive("parent control FD lost at signal barrier")
        assert proc_fields(supervisor_pid)[3] not in {"Z", "X"}
        stopped = proc_fields(supervisor_pid)[3] == "T"
        if signame == "TSTP" and stopped:
            if stage == "start":
                assert not any(event.startswith(("CHILD:", "MAIN_RETURN:", "INPUT_BARRIER"))
                               for event in session.events[signal_since:])
            else:
                assert "INPUT_BARRIER" not in session.events[signal_since:]
            os.killpg(supervisor_group, signal.SIGCONT)
            wait(session, "SIGNAL_CONT", signal_since)
        assert "READY" not in session.events[since:], (
            "native signal returned parent to prompt before lifetime/input return; "
            f"parent={signal_snapshot(parent)}; supervisor={signal_snapshot(supervisor_pid)}; "
            f"events={session.events[since:]}"
        )
        assert "INPUT_BARRIER" not in session.events[since:]
        assert not marker.exists(), "signal released stale input before descendant drain"
        if stage == "start":
            assert not any(event.startswith(("CHILD:", "MAIN_RETURN:"))
                           for event in session.events[since:]), "signal started main program"
            os.kill(supervisor_pid, signal.SIGUSR2)
            child_pid = int(wait(session, "CHILD:", since).split(":")[1])
            main = wait(session, "MAIN_RETURN:", since)
            assert main == "MAIN_RETURN:0", main
        else:
            assert proc_fields(descendant_pid)[0] == supervisor_pid
            assert "DESCENDANT_REAPED:" not in " ".join(session.events[since:])
            os.kill(supervisor_pid, signal.SIGUSR2)
            assert wait(session, "DESCENDANT_REAPED:", since) == (
                f"DESCENDANT_REAPED:{descendant_pid}:23"
            )
        assert wait(session, "WAIT_EMPTY:", since) == "WAIT_EMPTY:ECHILD"
        assert wait(session, "LIFETIME_DONE:", since) == (
            f"LIFETIME_DONE:INTERRUPTS={int(signame == 'INT')}"
        )
        wait(session, "INPUT_BARRIER", since)
        if "CONTROL_LOST" in session.events[since:]:
            raise Inconclusive("parent control FD lost before input return")
        assert "READY" not in session.events[since:]
        assert not marker.exists(), "stale input escaped before input barrier"
        boundary.flush_queued_pty(session)
        os.kill(supervisor_pid, signal.SIGUSR1)
        wait(session, "INPUT_RELEASED", since)
        assert wait(session, "RETURN:", since) == (
            "RETURN:0" if stage == "start" else "RETURN:17"
        )
        assert wait(session, "WAIT:", since) == first
        assert session.pid == parent and os.readlink(f"/proc/{parent}/cwd") == directory
        assert not Path(f"/proc/{supervisor_pid}").exists()
        assert not Path(f"/proc/{child_pid}").exists()
        if descendant_pid is not None:
            assert not Path(f"/proc/{descendant_pid}").exists()
        boundary.flush_queued_pty(session)
        os.write(session._request_fd, b"TAKEOVER\n")
        wait(session, "TAKEOVER_ACK:", since)
        wait(session, "READY", since)
        assert traps_before.read_bytes() == traps_after.read_bytes()
        new_since = len(session.events)
        session._write_all(b"__b_emit NEW_INPUT\n")
        wait(session, "NEW_INPUT", new_since)
        assert not marker.exists(), "stale input escaped before return"
        expected_signals = [f"SIGNAL_{signame}"]
        if signame == "TSTP" and stopped:
            expected_signals.append("SIGNAL_CONT")
        assert [event for event in session.events[since:] if event.startswith("SIGNAL_")] == (
            expected_signals
        ), "signal event was duplicated or replayed"
        return {"shell": Path(shell).name, "stage": stage, "signal": signame,
                "classification": "unknown_no_stop" if signame == "TSTP" and not stopped
                else "stopped_continued" if signame == "TSTP"
                else "native_noop" if signame == "CONT" else "observed",
                "at_injection": "main_not_created" if stage == "start"
                else "main_returned_descendant_active",
                "main_return": main if stage == "start" else "MAIN_RETURN:17",
                "observer_alive": True, "parent_control_returned": True,
                "owned_pids": [parent, supervisor_pid, child_pid, descendant_pid]}


def legacy_main() -> int:
    shells = [shutil.which("bash"), shutil.which("dash")]
    if sys.platform != "linux" or any(shell is None for shell in shells):
        print(json.dumps({"result": "inconclusive", "reason": "Linux, Bash and dash required"}))
        return 2
    results = []
    unknowns = []
    signal_unknowns = []
    for shell in shells:
        assert shell is not None
        for negative in (True, False):
            try:
                results.append(case(shell, negative=negative))
            except Exception as exc:
                print(json.dumps({"result": "failed", "cases": results,
                                  "shell": shell, "negative": negative,
                                  "error": f"{type(exc).__name__}: {exc}"}))
                return 1
        for name in ("return", "interrupt", "quit", "suspend", "start",
                     "orphan_immediate", "orphan_delayed", "parent_wait", "parent_wait_prior",
                     "parent_wait_quit", "errexit", "errexit_interrupt", "parent_wait_suspend"):
            try:
                results.append(simple_case(shell, name))
            except Inconclusive as exc:
                unknowns.append({"shell": shell, "case": name, "reason": str(exc)})
            except Exception as exc:
                print(json.dumps({"result": "failed", "cases": results,
                                  "shell": shell, "case": name,
                                  "error": f"{type(exc).__name__}: {exc}"}))
                return 1
        for stage in ("start", "return"):
            for signame in ("INT", "QUIT", "TSTP", "CONT"):
                try:
                    result = signal_stage_case(shell, stage, signame)
                    allowed = ({"unknown_no_stop", "stopped_continued"}
                               if signame == "TSTP" else {"native_noop"}
                               if signame == "CONT" else {"observed"})
                    assert result["classification"] in allowed, (
                        f"unsupported {signame} classification: {result['classification']}"
                    )
                    results.append(result)
                    if result["classification"] == "unknown_no_stop":
                        signal_unknowns.append({"shell": shell, "stage": stage,
                                                "signal": signame,
                                                "reason": result["classification"]})
                except Exception as exc:
                    print(json.dumps({"result": "failed", "cases": results,
                                      "shell": shell, "stage": stage, "signal": signame,
                                      "error": f"{type(exc).__name__}: {exc}"}))
                    return 1
    if unknowns or signal_unknowns:
        print(json.dumps({"result": "inconclusive", "cases": results,
                          "unknowns": unknowns, "signal_unknowns": signal_unknowns}))
        return 2
    print(json.dumps({"result": "observed", "cases": results}))
    return 0


def prepared_split_child(shell: str, script: str, ready_fd: int, go_fd: int) -> None:
    """Install native dispositions in PG_E before allowing any payload code."""
    for signum in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP, signal.SIGCONT):
        signal.signal(signum, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_SETMASK, set())
    emit(f"PREPARED:{os.getpid()}:{os.getpgrp()}:{os.getsid(0)}")
    os.write(ready_fd, b"P")
    os.close(ready_fd)
    if os.read(go_fd, 1) != b"G":
        os._exit(72)
    os.close(go_fd)
    os.execv(shell, [shell, "-c", script])


def split_running_child() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    signal.signal(signal.SIGCONT, lambda *_: emit(f"RESUMED_ACK:{os.getpid()}"))
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR2})
    emit(f"SPLIT_RUNNING_READY:{os.getpid()}")
    await_release(signal.SIGUSR2)
    fork_tree()


def restore_foreground(group: int) -> None:
    """Only the same-session supervisor restores ownership before SIGCONT."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    try:
        os.tcsetpgrp(0, group)
        if os.tcgetpgrp(0) != group:
            raise RuntimeError("foreground restore did not select experiment PG")
        emit(f"FOREGROUND_VERIFIED:{group}")
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def continue_experiment(group: int) -> None:
    restore_foreground(group)
    os.killpg(group, signal.SIGCONT)
    emit(f"CONT_SENT:{group}")


def failure_split_tree() -> None:
    """Hold both ancestors alive after setsid, before host registration."""
    middle = os.fork()
    if middle == 0:
        child = os.fork()
        if child == 0:
            os.setsid()
            emit(f"FAILURE_UNREGISTERED:{os.getpid()}:{proc_fields(os.getpid())[2]}")
            os.write(int(os.environ["WB_FAILURE_READY_FD"]), b"F")
        signal.pause()
        os._exit(70)
    signal.pause()
    os._exit(70)


def reap_split_failure() -> None:
    """Rediscover adopted children until ECHILD, with a bounded deadline."""
    deadline = time.monotonic() + 2.0
    children_path = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    while True:
        discovered = children_path.read_text().split()
        if discovered:
            emit(f"CLEANUP_SCAN:{','.join(discovered)}")
        for raw_pid in discovered:
            try:
                os.kill(int(raw_pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
            except ChildProcessError:
                if children_path.read_text().strip():
                    raise RuntimeError("ECHILD disagrees with owned child inventory")
                emit("CLEANUP_EMPTY:ECHILD")
                return
            if not pid:
                break
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                emit(f"CLEANUP_REAPED:{pid}")
        if time.monotonic() >= deadline:
            emit("CLEANUP_UNKNOWN:DEADLINE")
            raise RuntimeError("bounded split cleanup retained children")
        time.sleep(0.005)


def split_supervisor(shell: str, payload: str) -> int:
    enable_subreaper()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if signal.getsignal(signal.SIGCHLD) is not signal.SIG_DFL:
        raise RuntimeError("SIGCHLD is not default")
    signal.pthread_sigmask(signal.SIG_BLOCK, {
        signal.SIGCHLD, signal.SIGUSR1, signal.SIGUSR2,
    })
    target: int | None = None
    stage = "start"
    interrupts = 0

    def observe_signal(signum: int, _frame: object) -> None:
        nonlocal interrupts
        if signum == signal.SIGINT:
            interrupts += 1
        emit(f"SIGNAL_{signal.Signals(signum).name.removeprefix('SIG')}")
        if target is None:
            emit(f"NO_TARGET:{stage}:{signal.Signals(signum).name}")

    def no_target(signum: int, _frame: object) -> None:
        if target is not None:
            raise RuntimeError("signal addressed supervisor while experiment target exists")
        emit(f"NO_TARGET:{stage}:{signal.Signals(signum).name}")

    def terminate(_signum: int, _frame: object) -> None:
        raise SystemExit(73)

    for signum in (signal.SIGTSTP, signal.SIGCONT):
        signal.signal(signum, no_target)
    for signum in (signal.SIGINT, signal.SIGQUIT):
        signal.signal(signum, observe_signal)
    signal.signal(signal.SIGWINCH, lambda *_: emit(f"SUPERVISOR_PING:{stage}"))
    signal.signal(signal.SIGTERM, terminate)
    config = json.loads(base64.b64decode(payload, validate=True))
    emit(f"SUPERVISOR:{os.getpid()}:{os.getpgrp()}:SUBREAPER=1:SIGCHLD=DFL")
    emit(f"SPLIT_SESSION:{os.getsid(0)}")
    if config["hold_start"]:
        emit("START_BARRIER")
        await_release(signal.SIGUSR2)
    release_read, release_write = os.pipe()
    ready_read, ready_write = os.pipe()
    go_read, go_write = os.pipe()
    failure_read, failure_write = os.pipe()
    child = None
    try:
        env = os.environ.copy()
        env["WB_RELEASE_FD"] = str(release_read)
        env["WB_FAILURE_READY_FD"] = str(failure_write)
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--prepared-split-child",
             shell, config["script"], str(ready_write), str(go_read)],
            env=env, pass_fds=(9, release_read, ready_write, go_read, failure_write), process_group=0,
        )
        os.close(ready_write)
        ready_write = -1
        os.close(go_read)
        go_read = -1
        emit(f"CHILD:{child.pid}:{os.getpgid(child.pid)}")
        ready, _, _ = select.select([ready_read], [], [], TIMEOUT)
        if not ready or os.read(ready_read, 1) != b"P":
            raise TimeoutError("experiment child preparation barrier missing")
        target = child.pid
        if os.getpgid(target) == os.getpgrp() or os.getsid(target) != os.getsid(0):
            raise RuntimeError("split PG or same-session invariant violated")
        emit("CHILD_BARRIER")
        await_release(signal.SIGUSR2)
        restore_foreground(target)
        stage = "running"
        emit(f"EXPERIMENT_START:{target}")
        os.write(go_write, b"G")
        if config.get("failure"):
            ready, _, _ = select.select([failure_read], [], [], TIMEOUT)
            if not ready or os.read(failure_read, 1) != b"F":
                raise TimeoutError("setsid failure barrier missing")
            emit("FAILURE_ARMED:HOST_UNREGISTERED")
            await_release(signal.SIGUSR2)
            emit("UNKNOWN:INJECTED_FAILURE")
            raise RuntimeError("injected failure before host descendant registration")
        stopped = False
        deadline = time.monotonic() + TIMEOUT
        while True:
            pid, result = os.waitpid(target, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
            if pid:
                if os.WIFSTOPPED(result):
                    stopped = True
                    emit(f"MAIN_STOPPED:{os.WSTOPSIG(result)}")
                elif os.WIFCONTINUED(result):
                    stopped = False
                    emit("MAIN_CONTINUED")
                else:
                    status = os.waitstatus_to_exitcode(result)
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("split main program did not return")
            received = signal.sigtimedwait({signal.SIGCHLD, signal.SIGUSR2}, remaining)
            if received is not None and received.si_signo == signal.SIGUSR2:
                if not stopped:
                    raise RuntimeError("resume requested without observed stopped target")
                continue_experiment(target)
        target = None
        stage = "return"
        restore_foreground(os.getpgrp())
        emit(f"MAIN_RETURN:{status}")
        active = False
        deadline = time.monotonic() + TIMEOUT
        while True:
            try:
                pid, result = os.waitpid(-1, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
            except ChildProcessError:
                emit("WAIT_EMPTY:ECHILD")
                break
            if pid:
                if os.WIFSTOPPED(result) or os.WIFCONTINUED(result):
                    emit(f"DESCENDANT_LIVE:{pid}")
                else:
                    emit(f"DESCENDANT_REAPED:{pid}:{os.waitstatus_to_exitcode(result)}")
                continue
            if not active:
                emit("LIFETIME_ACTIVE:WNOHANG=0")
                active = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("split descendant remained active")
            received = signal.sigtimedwait({signal.SIGCHLD, signal.SIGUSR2}, remaining)
            if received is not None and received.si_signo == signal.SIGUSR2:
                os.write(release_write, b"R")
        emit(f"LIFETIME_DONE:INTERRUPTS={interrupts}")
        emit("INPUT_BARRIER")
        await_release(signal.SIGUSR1)
        emit("INPUT_RELEASED")
        return status if status >= 0 else 128 - status
    finally:
        # Every descendant is either live in the child PG or adopted by this
        # single subreaper. Failure cannot leave a bounded fixture running.
        reap_split_failure()
        for fd in (release_read, release_write, ready_read, ready_write, go_read, go_write,
                   failure_read, failure_write):
            if fd >= 0:
                os.close(fd)


@contextmanager
def cleanup_split(identities: list[tuple[int, int]], groups: list[tuple[int, int]]):
    try:
        yield
    finally:
        # SIGTERM leaves the single reaper alive to kill and reap its children.
        if identities:
            supervisor_pid, started = identities[0]
            try:
                if proc_fields(supervisor_pid)[2] == started:
                    os.kill(supervisor_pid, signal.SIGTERM)
            except (FileNotFoundError, ProcessLookupError):
                pass
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                live = []
                for pid, birth in identities:
                    try:
                        if proc_fields(pid)[2] == birth:
                            live.append(pid)
                    except (FileNotFoundError, ProcessLookupError):
                        pass
                if not live:
                    break
                time.sleep(0.01)
        for group, started in groups:
            try:
                if proc_fields(group)[2] == started:
                    os.killpg(group, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass
        for pid, started in reversed(identities):
            try:
                if proc_fields(pid)[2] == started:
                    os.kill(pid, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass


def split_case(shell: str, name: str) -> dict:
    """Differential split-PG candidate, keeping legacy helpers unchanged."""
    assert name in {"suspend", "start", "exit130", "exit131", "exit148", "parent_wait",
                    "int_start", "quit_start", "int_return", "quit_return",
                    "int_running", "quit_running"}
    signal_name = "INT" if name.startswith("int_") else "QUIT" if name.startswith("quit_") else None
    hold_start = name == "start" or name.endswith("_start")
    tree_case = name in {"suspend", "start"} or name.endswith(("_start", "_return"))
    running_signal = name.endswith("_running")
    source = controller_source(shell).replace(" --supervisor ", " --split-supervisor ")
    identities: list[tuple[int, int]] = []
    groups: list[tuple[int, int]] = []
    result = {}
    try:
        with patch.object(boundary.prototype, "_bash_control_init", return_value=source), \
             patch.object(boundary.prototype, "_sh_control_init", return_value=source), \
             tempfile.TemporaryDirectory(prefix="cw03-split-") as directory, \
             boundary.prototype.ShellProcess(
                 boundary.prototype.ShellChoice("bash" if Path(shell).name == "bash" else "sh", shell),
                 control_wait=True,
             ) as session, cleanup_split(identities, groups):
            wait(session, "READY", 0)
            parent = session.pid
            marker = Path(directory) / "spill"
            following = Path(directory) / "following"
            traps = Path(directory) / "traps-before"
            after = Path(directory) / "traps-after"
            session._write_all((f"cd {shlex.quote(directory)}; export BOUNDARY_PREPARED=kept "
                                f"BOUNDARY_TRAPS={shlex.quote(str(traps))} "
                                f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(after))}; wb-handoff\n"
                                f": > {shlex.quote(str(following))}\n").encode())
            wait(session, f"HANDOFF:{parent}", 0)
            first = wait(session, "WAIT:", 0)
            since = len(session.events)
            helper = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
            script = (f"exec {helper} --split-running-child" if name == "suspend" or running_signal else
                      f"{helper} --fork-tree" if tree_case else
                      f"exit {name.removeprefix('exit')}" if name.startswith("exit") else ":")
            payload = base64.b64encode(json.dumps({"script": script,
                                                   "hold_start": hold_start}).encode()).decode()
            os.write(session._request_fd, f"RUN:{payload}\n".encode())
            wait(session, "START", since)
            sup = wait(session, "SUPERVISOR:", since)
            sup_pid, sup_pg = map(int, sup.split(":")[1:3])
            identities.append((sup_pid, proc_fields(sup_pid)[2]))

            def control_held() -> None:
                events = session.events[since:]
                assert not any(event == "READY" or event.startswith(("RETURN:", "WAIT:",
                                                                      "TAKEOVER_ACK:"))
                               for event in events), events
                assert not marker.exists() and not following.exists()

            def ping(stage: str) -> None:
                ping_since = len(session.events)
                os.kill(sup_pid, signal.SIGWINCH)
                wait(session, f"SUPERVISOR_PING:{stage}", ping_since)
                control_held()

            if hold_start:
                wait(session, "START_BARRIER", since)
                assert not any(event.startswith("CHILD:") for event in session.events[since:])
                session._write_all(b"\x03" if signal_name == "INT" else
                                   b"\x1c" if signal_name == "QUIT" else b"\x1a")
                wait(session, f"NO_TARGET:start:SIG{signal_name or 'TSTP'}", since)
                if signal_name is None:
                    os.killpg(sup_pg, signal.SIGCONT)
                    wait(session, "NO_TARGET:start:SIGCONT", since)
                ping("start")
                os.kill(sup_pid, signal.SIGUSR2)
            child = wait(session, "CHILD:", since)
            child_pid, child_pg = map(int, child.split(":")[1:])
            started = proc_fields(child_pid)[2]
            identities.append((child_pid, started))
            groups.append((child_pg, started))
            prepared = wait(session, "PREPARED:", since)
            assert prepared == f"PREPARED:{child_pid}:{child_pg}:{os.getsid(parent)}"
            assert child_pg != sup_pg and os.getsid(sup_pid) == os.getsid(parent)
            wait(session, "CHILD_BARRIER", since)
            assert not any(event.startswith("EXPERIMENT_START:") for event in session.events[since:])
            os.kill(sup_pid, signal.SIGUSR2)
            wait(session, f"EXPERIMENT_START:{child_pid}", since)
            cycles = []
            if running_signal:
                assert wait(session, "SPLIT_RUNNING_READY:", since) == f"SPLIT_RUNNING_READY:{child_pid}"
                assert boundary.foreground_group(session) == child_pg
                session._write_all(b"\x03" if signal_name == "INT" else b"\x1c")
            if name == "suspend":
                assert wait(session, "SPLIT_RUNNING_READY:", since) == f"SPLIT_RUNNING_READY:{child_pid}"
                for cycle in range(2):
                    cycle_since = len(session.events)
                    assert boundary.foreground_group(session) == child_pg
                    session._write_all(b"\x1a")
                    wait(session, f"MAIN_STOPPED:{signal.SIGTSTP}", cycle_since)
                    assert proc_fields(child_pid)[3] == "T"
                    ping("running")
                    assert not any(event.startswith(("MAIN_RETURN:", "LIFETIME_DONE:",
                                                     "INPUT_RELEASED")) for event in session.events[since:])
                    os.kill(sup_pid, signal.SIGUSR2)
                    foreground = wait(session, f"FOREGROUND_VERIFIED:{child_pg}", cycle_since)
                    sent = wait(session, f"CONT_SENT:{child_pg}", cycle_since)
                    wait(session, "MAIN_CONTINUED", cycle_since)
                    ack = wait(session, f"RESUMED_ACK:{child_pid}", cycle_since)
                    events = session.events[cycle_since:]
                    assert events.index(foreground) < events.index(sent)
                    assert events.index(sent) < events.index("MAIN_CONTINUED")
                    assert events.index(foreground) < events.index(ack)
                    assert proc_fields(child_pid)[3] != "T"
                    assert boundary.foreground_group(session) == child_pg
                    control_held()
                    cycles.append("native_stop_foreground_restore_continue_ack")
                os.kill(child_pid, signal.SIGUSR2)
            descendant_pid = None
            if tree_case:
                descendant = wait(session, "DESCENDANT:", since)
                _, pid, birth, sid = descendant.split(":")
                descendant_pid = int(pid)
                identities.append((descendant_pid, int(birth)))
                main = wait(session, "MAIN_RETURN:17", since)
                wait(session, "LIFETIME_ACTIVE:WNOHANG=0", since)
                adopted = proc_fields(descendant_pid)
                assert adopted[0] == sup_pid and adopted[2] == int(birth)
                assert adopted[1] == descendant_pid == int(sid)
                assert boundary.foreground_group(session) == sup_pg
                if name.endswith("_return"):
                    session._write_all(b"\x03" if signal_name == "INT" else b"\x1c")
                    wait(session, f"NO_TARGET:return:SIG{signal_name}", since)
                    assert proc_fields(descendant_pid)[3] not in {"Z", "X"}
                elif signal_name is None:
                    session._write_all(b"\x1a")
                    wait(session, "NO_TARGET:return:SIGTSTP", since)
                    os.killpg(sup_pg, signal.SIGCONT)
                    wait(session, "NO_TARGET:return:SIGCONT", since)
                ping("return")
                assert "INPUT_BARRIER" not in session.events[since:]
                session._write_all(f": > {shlex.quote(str(marker))}\n".encode())
                os.kill(sup_pid, signal.SIGUSR2)
                wait(session, f"DESCENDANT_REAPED:{descendant_pid}:23", since)
            else:
                main = wait(session, "MAIN_RETURN:", since)
            expected_status = (int(name.removeprefix("exit")) if name.startswith("exit") else
                               -2 if name == "int_running" else -3 if name == "quit_running" else
                               0 if name == "parent_wait" else 17)
            assert main == f"MAIN_RETURN:{expected_status}"
            wait(session, "WAIT_EMPTY:ECHILD", since)
            wait(session, f"LIFETIME_DONE:INTERRUPTS={int(name in {'int_start', 'int_return'})}", since)
            wait(session, "INPUT_BARRIER", since)
            control_held()
            writer = PreviousWriter(f": > {shlex.quote(str(marker))}\n".encode())
            session._write_all(f": > {shlex.quote(str(marker))}".encode())
            writer.close_and_discard()
            boundary.flush_queued_pty(session)
            os.kill(sup_pid, signal.SIGUSR1)
            wait(session, "INPUT_RELEASED", since)
            returned = wait(session, "RETURN:", since)
            assert returned == f"RETURN:{expected_status if expected_status >= 0 else 128 - expected_status}"
            assert wait(session, "WAIT:", since) == first
            parent_disposition = None
            if name == "parent_wait":
                parent_disposition = signal_snapshot(parent)
                signal_since = len(session.events)
                session._write_all(b"\x1a")
                session._write_all(f": > {shlex.quote(str(marker))}\n".encode())
                boundary.drain_for(session, 0.05)
                if proc_fields(parent)[3] == "T":
                    raise Inconclusive("parent WAIT unexpectedly stopped; native ignore experiment unavailable")
                os.write(session._request_fd, b"PING\n")
                wait(session, "BAD_REQUEST", signal_since)
                if not parent_disposition["tstp_ignored"]:
                    raise Inconclusive("parent WAIT native TSTP disposition is not proven ignored")
                assert not any(event == "READY" or event.startswith(("RETURN:", "WAIT:",
                                                                      "TAKEOVER_ACK:"))
                               for event in session.events[signal_since:])
                assert not marker.exists() and not following.exists()
            boundary.flush_queued_pty(session)
            os.write(session._request_fd, b"TAKEOVER\n")
            wait(session, "TAKEOVER_ACK:", since)
            wait(session, "READY", since)
            assert not writer.drain(session)
            assert traps.read_bytes() == after.read_bytes()
            session._write_all(b"__b_emit NEW_INPUT\n")
            wait(session, "NEW_INPUT", since)
            assert not marker.exists() and not following.exists()
            assert session.pid == parent and os.readlink(f"/proc/{parent}/cwd") == directory
            assert boundary.foreground_group(session) == os.getpgid(parent)
            result = {"shell": Path(shell).name, "case": name, "mode": "split_pg",
                      "main_return": main, "parent_return": returned,
                      "classification": "native_ignore_noop" if name == "parent_wait" else "observed",
                      "stop_continue_cycles": cycles, "parent_disposition": parent_disposition,
                      "signal_events": [event for event in session.events[since:]
                                        if event.startswith(("SIGNAL_", "NO_TARGET:"))],
                      "owned_pids": [parent, sup_pid, child_pid] +
                                    ([descendant_pid] if descendant_pid is not None else [])}
    finally:
        # Include PG_E even on a failed assertion while it is stopped.
        for group, started in groups:
            try:
                if proc_fields(group)[2] == started:
                    os.killpg(group, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass
    for pid in result["owned_pids"]:
        assert not Path(f"/proc/{pid}").exists(), f"owned process remained: {pid}"
    owned_groups = [sup_pg, child_pg] + ([descendant_pid] if descendant_pid is not None else [])
    for group in owned_groups:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"owned process group remained: {group}")
    result["owned_groups"] = owned_groups
    return result


def split_failure_case(shell: str) -> dict:
    """Inject observer failure after setsid but before host registration."""
    source = controller_source(shell).replace(" --supervisor ", " --split-supervisor ")
    identities = []
    groups = []
    with patch.object(boundary.prototype, "_bash_control_init", return_value=source), \
         patch.object(boundary.prototype, "_sh_control_init", return_value=source), \
         tempfile.TemporaryDirectory(prefix="cw03-split-failure-") as directory, \
         boundary.prototype.ShellProcess(
             boundary.prototype.ShellChoice("bash" if Path(shell).name == "bash" else "sh", shell),
             control_wait=True,
         ) as session, cleanup_split(identities, groups):
        wait(session, "READY", 0)
        parent = session.pid
        traps = Path(directory) / "traps"
        after = Path(directory) / "after"
        session._write_all((f"cd {shlex.quote(directory)}; export BOUNDARY_PREPARED=kept "
                            f"BOUNDARY_TRAPS={shlex.quote(str(traps))} "
                            f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(after))}; wb-handoff\n").encode())
        first = wait(session, "WAIT:", 0)
        since = len(session.events)
        helper = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
        payload = base64.b64encode(json.dumps({"script": f"exec {helper} --failure-split-tree",
                                               "hold_start": False, "failure": True}).encode()).decode()
        os.write(session._request_fd, f"RUN:{payload}\n".encode())
        sup = wait(session, "SUPERVISOR:", since)
        sup_pid, sup_pg = map(int, sup.split(":")[1:3])
        identities.append((sup_pid, proc_fields(sup_pid)[2]))
        child_pid, child_pg = map(int, wait(session, "CHILD:", since).split(":")[1:])
        birth = proc_fields(child_pid)[2]
        identities.append((child_pid, birth))
        groups.append((child_pg, birth))
        wait(session, "PREPARED:", since)
        wait(session, "CHILD_BARRIER", since)
        os.kill(sup_pid, signal.SIGUSR2)
        wait(session, "FAILURE_ARMED:HOST_UNREGISTERED", since)
        unregistered = wait(session, "FAILURE_UNREGISTERED:", since)
        descendant_pid, descendant_birth = map(int, unregistered.split(":")[1:])
        middle_pid, descendant_sid, observed_birth, state = proc_fields(descendant_pid)
        assert descendant_sid == descendant_pid and observed_birth == descendant_birth
        assert state not in {"Z", "X"}
        assert middle_pid != sup_pid and proc_fields(middle_pid)[0] == child_pid
        assert descendant_pid not in [pid for pid, _ in identities]
        # The host knows only diagnostic identity here; it does not add the
        # descendant to the cleanup list. The supervisor must find adoption.
        os.kill(sup_pid, signal.SIGUSR2)
        wait(session, "UNKNOWN:INJECTED_FAILURE", since)
        wait(session, f"CLEANUP_REAPED:{descendant_pid}", since)
        wait(session, "CLEANUP_EMPTY:ECHILD", since)
        assert wait(session, "RETURN:", since) == "RETURN:1"
        assert wait(session, "WAIT:", since) == first
        events = session.events[since:]
        assert not any(event.startswith(("MAIN_RETURN:", "LIFETIME_DONE:",
                                         "INPUT_BARRIER", "INPUT_RELEASED", "READY")) for event in events)
        assert events.index("UNKNOWN:INJECTED_FAILURE") < events.index(f"CLEANUP_REAPED:{descendant_pid}")
        assert events.index("CLEANUP_EMPTY:ECHILD") < events.index("RETURN:1")
        scans = [set(map(int, event.split(":")[1].split(","))) for event in events
                 if event.startswith("CLEANUP_SCAN:")]
        assert scans[0] == {child_pid}, scans
        assert descendant_pid not in scans[0] and any(descendant_pid in scan for scan in scans[1:])
        all_pids = [parent, sup_pid, child_pid, middle_pid, descendant_pid]
        for pid in all_pids[1:]:
            assert not Path(f"/proc/{pid}").exists(), f"failure cleanup retained {pid}"
        boundary.flush_queued_pty(session)
        os.write(session._request_fd, b"TAKEOVER\n")
        wait(session, "TAKEOVER_ACK:", since)
        wait(session, "READY", since)
        session._write_all(b"__b_emit NEW_INPUT\n")
        wait(session, "NEW_INPUT", since)
    for pid in all_pids:
        assert not Path(f"/proc/{pid}").exists(), f"failure cleanup retained {pid}"
    return {"shell": Path(shell).name, "mode": "split_pg", "case": "unregistered_failure",
            "classification": "explicit_unknown_cleanup_empty", "events": events,
            "owned_pids": all_pids}


def shared_pg_negative(shell: str, stage: str) -> dict:
    """Require the known native job-control escape, never call it a pass."""
    observed = {}
    original_wait = wait

    def retain(session: object, prefix: str, since: int) -> str:
        observed["session"] = session
        if prefix == "START":
            observed["since"] = since
        return original_wait(session, prefix, since)

    with patch(__name__ + ".wait", side_effect=retain):
        try:
            signal_stage_case(shell, stage, "TSTP")
        except AssertionError:
            session = observed["session"]
            events = session.events[observed["since"]:]
            assert "SIGNAL_TSTP" in events
            before_release = events[:events.index("INPUT_RELEASED")] if "INPUT_RELEASED" in events else events
            assert "READY" in before_release or "RETURN:148" in before_release, events
        else:
            raise AssertionError("shared-PG negative control did not expose parent escape")
    pids = [session.pid] + [int(event.split(":")[1]) for event in events
                          if event.startswith(("SUPERVISOR:", "CHILD:", "DESCENDANT:"))]
    assert not any(Path(f"/proc/{pid}").exists() for pid in pids), pids
    return {"shell": Path(shell).name, "mode": "shared_pg_negative", "stage": stage,
            "classification": "parent_escape_before_input_release", "events": events,
            "owned_pids": pids}


def main() -> int:
    shells = [shutil.which("bash"), shutil.which("dash")]
    if sys.platform != "linux" or any(shell is None for shell in shells):
        print(json.dumps({"result": "inconclusive", "reason": "Linux, Bash and dash required"}))
        return 2
    results = []
    for shell in shells:
        for stage in ("start", "return"):
            try:
                results.append(shared_pg_negative(shell, stage))
            except Exception as exc:
                print(json.dumps({"result": "failed", "cases": results,
                                  "shell": shell, "stage": stage,
                                  "error": f"{type(exc).__name__}: {exc}"}))
                return 1
        for name in ("suspend", "start", "exit148", "parent_wait", "int_start", "quit_start",
                     "int_return", "quit_return", "int_running", "quit_running", "exit130", "exit131",
                     "unregistered_failure"):
            try:
                results.append(split_failure_case(shell) if name == "unregistered_failure"
                               else split_case(shell, name))
            except Exception as exc:
                print(json.dumps({"result": "inconclusive" if isinstance(exc, Inconclusive) else "failed",
                                  "cases": results, "shell": shell, "case": name,
                                  "error": f"{type(exc).__name__}: {exc}"}))
                return 2 if isinstance(exc, Inconclusive) else 1
    print(json.dumps({"result": "observed", "cases": results}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--fork-tree":
        fork_tree()
    elif len(sys.argv) == 2 and sys.argv[1] == "--blocking-child":
        blocking_child()
    elif len(sys.argv) == 2 and sys.argv[1] == "--split-running-child":
        split_running_child()
    elif len(sys.argv) == 2 and sys.argv[1] == "--failure-split-tree":
        failure_split_tree()
    elif len(sys.argv) == 6 and sys.argv[1] == "--prepared-split-child":
        prepared_split_child(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
    elif len(sys.argv) == 4 and sys.argv[1] == "--split-supervisor":
        raise SystemExit(split_supervisor(sys.argv[2], sys.argv[3]))
    elif len(sys.argv) == 2 and sys.argv[1] in {"--orphan-immediate", "--orphan-delayed"}:
        orphan_child(sys.argv[1] == "--orphan-delayed")
    elif len(sys.argv) == 4 and sys.argv[1] == "--supervisor":
        raise SystemExit(supervisor(sys.argv[2], sys.argv[3]))
    else:
        raise SystemExit(main())
