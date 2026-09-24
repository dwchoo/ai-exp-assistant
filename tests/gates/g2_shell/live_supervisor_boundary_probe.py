"""Host-only PTY feasibility probe for a foreground supervisor boundary.

This is an experiment, not a production adapter or a G2 acceptance test.  It
uses the existing PTY harness only for descriptor setup, draining and cleanup.
The persistent shell never evaluates the RUN payload: a fixed Python helper
starts a separate shell with the payload as data.

Run: python tests/gates/g2_shell/live_supervisor_boundary_probe.py
"""

from __future__ import annotations

import array
import base64
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from workbench.terminal.shell_g2 import prototype  # noqa: E402


def emit(event: str) -> None:
    os.write(9, (event + "\n").encode("ascii"))


def supervisor(shell: str, payload: str) -> int:
    """Run one child interpreter in this foreground group and wait for it."""
    script = base64.b64decode(payload, validate=True).decode("utf-8")
    interrupts = 0

    def on_interrupt(_signum: int, _frame: object) -> None:
        nonlocal interrupts
        interrupts += 1

    signal.signal(signal.SIGINT, on_interrupt)
    emit(f"SUPERVISOR:{os.getpid()}:{os.getpgrp()}")
    child = subprocess.Popen([shell, "-c", script], close_fds=True)
    emit(f"CHILD:{child.pid}:{os.getpgid(child.pid)}")
    status = child.wait()
    emit(f"SUPERVISOR_DONE:{status}:{interrupts}")
    return 128 + -status if status < 0 else status


def controller_init(shell: str) -> str:
    helper = shlex.quote(str(Path(__file__).resolve()))
    python = shlex.quote(sys.executable)
    target = shlex.quote(shell)
    # RUN accepts only a base64 token.  The value is expanded as one argument
    # to a fixed command; no eval, command substitution or shell parsing of it.
    return f'''__b_emit() {{ printf '%s\\n' "$1" >&9; }}
__b_loop() {{
    __b_emit "WAIT:$$:$PWD:$BOUNDARY_PREPARED"
    while IFS= read -r __b_line <&8; do
        case $__b_line in
            RUN:*)
                __b_payload=${{__b_line#RUN:}}
                __b_emit START
                {python} {helper} --supervisor {target} "$__b_payload"
                __b_status=$?
                __b_emit "RETURN:$__b_status"
                __b_emit "WAIT:$$:$PWD:$BOUNDARY_PREPARED"
                ;;
            TAKEOVER) __b_emit "TAKEOVER_ACK:$$"; return;;
            *) __b_emit BAD_REQUEST;;
        esac
    done
    __b_emit CONTROL_LOST
    while :; do sleep 1; done
}}
''' + ("PROMPT_COMMAND='__b_emit READY'\nPS1='$ '\n" if Path(shell).name == "bash" else
         "PS1='$( __b_emit READY; printf \"$ \" )'\n")


def event_after(session: prototype.ShellProcess, prefix: str, index: int,
                timeout: float = 2.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        session._drain(min(0.03, end - time.monotonic()))
        for event in session.events[index:]:
            if event.startswith(prefix):
                return event
    raise TimeoutError(f"missing {prefix}; observed={session.events[index:]}")


def drain_for(session: prototype.ShellProcess, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        session._drain(min(0.03, end - time.monotonic()))
        session.display_bytes()  # Drain display without recording user output.


def foreground_group(session: prototype.ShellProcess) -> int:
    value = array.array("i", [0])
    fcntl.ioctl(session.master_fd, termios.TIOCGPGRP, value, True)
    return value[0]


def flush_queued_pty(session: prototype.ShellProcess) -> None:
    slave = fcntl.ioctl(session.master_fd, 0x5441, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        termios.tcflush(slave, termios.TCIFLUSH)
    finally:
        os.close(slave)


def one_case(shell: str, name: str, script: str) -> dict:
    init = controller_init(shell)
    # The existing harness owns PTY/session cleanup even after a failed probe.
    with patch.object(prototype, "_bash_control_init", return_value=init), \
         patch.object(prototype, "_sh_control_init", return_value=init), \
         tempfile.TemporaryDirectory(prefix="cw03-boundary-") as directory, \
         prototype.ShellProcess(prototype.ShellChoice(Path(shell).name, shell),
                                control_wait=True) as session:
        session.wait_event("READY")
        original_pid = session.pid
        tail_path = Path(directory) / "tail-executed"
        prepared = (f"cd {shlex.quote(directory)}; export BOUNDARY_PREPARED=kept; "
                    f"export BOUNDARY_TAIL_PATH={shlex.quote(str(tail_path))}; __b_loop\n")
        os.write(session.master_fd, prepared.encode())
        first_wait = event_after(session, "WAIT:", 0)
        if first_wait != f"WAIT:{original_pid}:{directory}:kept":
            raise AssertionError(f"parent preparation lost: {first_wait}")
        start_at = len(session.events)
        payload = base64.b64encode(script.encode()).decode("ascii")
        os.write(session._request_fd, f"RUN:{payload}\n".encode())
        event_after(session, "START", start_at)
        reported = event_after(session, "SUPERVISOR:", start_at)
        _, supervisor_pid, supervisor_pgid = reported.split(":")
        child_event = event_after(session, "CHILD:", start_at)
        _, child_pid, child_pgid = child_event.split(":")
        supervisor_pid, supervisor_pgid = int(supervisor_pid), int(supervisor_pgid)
        child_pid, child_pgid = int(child_pid), int(child_pgid)
        if supervisor_pgid == os.getpgid(original_pid) or child_pgid != supervisor_pgid:
            raise AssertionError("supervisor and child did not share a distinct foreground group")
        if name == "interrupt":
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and foreground_group(session) != supervisor_pgid:
                drain_for(session, 0.01)
            if foreground_group(session) != supervisor_pgid:
                raise AssertionError("supervisor did not own foreground PTY group")
            os.write(session.master_fd, b"\x03")
        # The marker is a potential stale terminal line, never a RUN request.
        # Send while the shell is still in its controller function.
        os.write(session.master_fd, b"__b_emit SPILL_EXECUTED\n")
        done = event_after(session, "SUPERVISOR_DONE:", start_at, timeout=2)
        returned = event_after(session, "RETURN:", start_at, timeout=2)
        wait = event_after(session, "WAIT:", start_at, timeout=2)
        later = session.events[start_at:]
        if not (later.index("START") < later.index(reported) < later.index(child_event)
                < later.index(done) < later.index(returned) < later.index(wait)):
            raise AssertionError("lifecycle event order was not preserved")
        if name == "interrupt":
            if done != "SUPERVISOR_DONE:-2:1" or returned != "RETURN:130":
                raise AssertionError(f"Ctrl-C signal/status mismatch: {done}, {returned}")
            if tail_path.exists():
                raise AssertionError("script tail ran after Ctrl-C")
        elif name == "exec" and (done != "SUPERVISOR_DONE:0:0" or returned != "RETURN:0"):
            raise AssertionError(f"exec status mismatch: {done}, {returned}")
        if wait != first_wait:
            raise AssertionError(f"parent shell state changed: {wait}")
        if os.readlink(f"/proc/{original_pid}/cwd") != directory:
            raise AssertionError("parent cwd changed")
        if foreground_group(session) != os.getpgid(original_pid):
            raise AssertionError("foreground did not return to parent group")
        if Path(f"/proc/{supervisor_pid}").exists() or Path(f"/proc/{child_pid}").exists():
            raise AssertionError("supervisor or child was not reaped before shell WAIT")
        before_takeover = tuple(session.events)
        if "READY" in before_takeover[start_at:] or "SPILL_EXECUTED" in before_takeover:
            raise AssertionError("premature prompt or queued input execution")
        flush_queued_pty(session)
        os.write(session._request_fd, b"TAKEOVER\n")
        event_after(session, "TAKEOVER_ACK:", start_at)
        event_after(session, "READY", start_at)
        drain_for(session, 0.15)
        if "SPILL_EXECUTED" in session.events:
            raise AssertionError("queued PTY line executed after takeover")
        if session.events[start_at:].count("START") != 1:
            raise AssertionError("RUN was replayed")
        os.write(session.master_fd, b"__b_emit MANUAL_RECOVERED\n")
        event_after(session, "MANUAL_RECOVERED", start_at)
        drain_for(session, 0.05)
        return {
            "shell": Path(shell).name,
            "case": name,
            "parent_pid": original_pid,
            "parent_pid_preserved": session.pid == original_pid,
            "parent_state_preserved": wait == first_wait,
            "supervisor_pid": supervisor_pid,
            "supervisor_pgid": supervisor_pgid,
            "child_pid": child_pid,
            "child_pgid": child_pgid,
            "supervisor_done": done,
            "parent_return": returned,
            "supervisor_and_child_reaped": True,
            "script_tail_suppressed": not tail_path.exists() if name == "interrupt" else None,
            "wait_before_takeover": True,
            "queued_marker_suppressed": True,
            "manual_recovered": True,
            "no_replay": True,
        }


def main() -> int:
    if sys.platform != "linux":
        print(json.dumps({"result": "inconclusive", "reason": "Linux PTY required"}))
        return 2
    shells = [shutil.which("bash"), shutil.which("dash")]
    if any(shell is None for shell in shells):
        print(json.dumps({"result": "inconclusive", "reason": "Bash and dash required"}))
        return 2
    results = []
    for shell in shells:
        assert shell is not None
        for name, script in (
            ("interrupt", "sleep 30; : > \"$BOUNDARY_TAIL_PATH\""),
            ("return", "cd /; export BOUNDARY_PREPARED=changed; return"),
            ("exec", "exec sleep 0.2"),
        ):
            try:
                results.append(one_case(shell, name, script))
            except Exception as exc:
                print(json.dumps({"result": "failed", "shell": shell, "case": name,
                                  "error": f"{type(exc).__name__}: {exc}", "cases": results}))
                return 1
    print(json.dumps({"result": "observed", "cases": results}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--supervisor":
        if len(sys.argv) != 4:
            raise SystemExit(64)
        raise SystemExit(supervisor(sys.argv[2], sys.argv[3]))
    raise SystemExit(main())
