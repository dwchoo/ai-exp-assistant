"""Bounded input/job admission on the combined supervisor candidate.

Run: python tests/gates/g2_shell/live_input_jobs_probe.py
This fixture connects InputBoundary to control WAIT, not a production adapter.
Manual daemons and external writers remain outside this fixture's observation.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.gates.g2_shell import live_combined_boundary_probe as combined

prototype = combined.boundary.prototype


def controller_source(shell: str) -> str:
    source = combined.controller_source(shell)
    return source.replace(
        '__b_handoff() { __b_emit "HANDOFF:$$"; __b_loop; }',
        '''__b_handoff() {
    __b_emit "HANDOFF:$$"
    trap > "$BOUNDARY_TRAPS"
    __b_hook_compatible || { __b_emit HOOK_LOST; return 1; }
    __b_emit HOOK_OK:HANDOFF
    __b_emit JOBS_BEGIN:HANDOFF
    jobs -p >&9
    __b_emit JOBS_END:HANDOFF
    __b_emit "CONTROL_READY:$$"
    __b_loop
}''',
    )


class InputJobsSession(prototype.ShellProcess):
    """One bounded control writer using the existing owner/line guard."""

    _completion_events = ("START", "LIFETIME_DONE", "INPUT_BARRIER",
                          "INPUT_RELEASED", "RETURN", "WAIT")

    def __init__(self, *args, **kwargs) -> None:
        self._completion_index: int | None = None
        super().__init__(*args, **kwargs)

    def dispatch_control(self, script: str, *, generation: int, owner_epoch: int) -> None:
        with self.boundary.lock:
            self._drain(0)
            if not self.boundary.can_dispatch(generation=generation, owner_epoch=owner_epoch):
                raise prototype.UnsafeShellState("input/job boundary is not clean")
            payload = base64.b64encode(script.encode()).decode("ascii")
            os.write(self._request_fd, f"RUN:{payload}\n".encode())
            self.boundary.ready = False
            self.boundary.dirty = True
            self.boundary.active_command = "input-jobs"
            self._completion_index = 0

    def _on_control_event(self, event: str) -> None:
        if self._completion_index is None:
            return
        kind = event.partition(":")[0]
        if kind not in self._completion_events:
            return
        valid = (
            event.removeprefix("LIFETIME_DONE:INTERRUPTS=").isdecimal()
            if kind == "LIFETIME_DONE" else
            event == "RETURN:0" if kind == "RETURN" else
            event.startswith(f"WAIT:{self.pid}:") if kind == "WAIT" else
            event == kind
        )
        if kind != self._completion_events[self._completion_index] or not valid:
            self._completion_index = None
            self.boundary.fail_closed()
            return
        self._completion_index += 1
        if self._completion_index == len(self._completion_events):
            # RETURN alone proves neither input release nor parent control return.
            self.boundary.observe_event("DONE:input-jobs:0")
            self._completion_index = None


def blocked_without_write(session: InputJobsSession, generation: int, epoch: int) -> None:
    since = len(session.events)
    with patch.object(os, "write") as write:
        try:
            session.dispatch_control(":", generation=generation, owner_epoch=epoch)
        except prototype.UnsafeShellState:
            pass
        else:
            raise AssertionError("unsafe input/job state accepted")
        write.assert_not_called()
    assert "START" not in session.events[since:]


def complete_run(session: InputJobsSession, epoch: int, pids: list[int]) -> None:
    run_since = len(session.events)
    session.dispatch_control(":", generation=session.generation, owner_epoch=epoch)
    combined.wait(session, "START", run_since)
    supervisor = int(combined.wait(session, "SUPERVISOR:", run_since).split(":")[1])
    child = int(combined.wait(session, "CHILD:", run_since).split(":")[1])
    pids.extend((supervisor, child))
    combined.wait(session, "INPUT_BARRIER", run_since)
    assert "READY" not in session.events[run_since:]
    combined.boundary.flush_queued_pty(session)
    os.kill(supervisor, signal.SIGUSR1)
    combined.wait(session, "INPUT_RELEASED", run_since)
    combined.wait(session, "RETURN:0", run_since)
    combined.wait(session, "WAIT:", run_since)
    assert session.boundary.active_command is None, "completed run retained an active command"
    assert not session.boundary.needs_review, "normal control return latched unknown"


def case(shell: str, name: str) -> dict:
    source = controller_source(shell)
    pids: list[int] = []
    job_pid = None
    in_wait = False
    with patch.object(prototype, "_bash_control_init", return_value=source), \
         patch.object(prototype, "_sh_control_init", return_value=source), \
         tempfile.TemporaryDirectory(prefix="cw03-input-jobs-") as directory, \
         InputJobsSession(prototype.ShellChoice(
             "bash" if Path(shell).name == "bash" else "sh", shell
         ), control_wait=True) as session:
        pids.append(session.pid)
        combined.wait(session, "READY", 0)
        session.take_user_control()
        session.send_user((
            f"export BOUNDARY_TRAPS={shlex.quote(directory + '/traps')} "
            f"BOUNDARY_TRAPS_AFTER={shlex.quote(directory + '/traps-after')}\n"
        ).encode())
        combined.wait(session, "READY", 1)
        since = len(session.events)
        marker = Path(directory) / "spill"
        try:
            if name in {"background", "suspended"}:
                session.send_user(b'sleep 30 & __b_emit "JOB:$!"\n')
                job_pid = int(combined.wait(session, "JOB:", since).partition(":")[2])
                pids.append(job_pid)
                combined.wait(session, "READY", since)
                if name == "suspended":
                    os.kill(job_pid, signal.SIGSTOP)
                    deadline = combined.time.monotonic() + combined.TIMEOUT
                    while combined.proc_fields(job_pid)[3] not in {"T", "t"}:
                        assert combined.time.monotonic() < deadline, "job did not stop"
                        session._drain(0.01)
            if name == "unsubmitted":
                session.send_user(b"echo pending")
            elif name == "multiline":
                session.send_user(b"printf 'unfinished\nwb-handoff\n")
            elif name == "repl":
                repl = (
                    "import os,sys; "
                    "os.write(9, ('REPL_READY:%s\\n' % os.getpid()).encode()); "
                    "value=sys.stdin.readline(); "
                    "os.write(9, ('REPL_DONE:%s\\n' % value.strip()).encode())"
                )
                session.send_user(f"{shlex.quote(sys.executable)} -c {shlex.quote(repl)}\n".encode())
                repl_pid = int(combined.wait(session, "REPL_READY:", since).partition(":")[2])
                pids.append(repl_pid)
                session.wait_foreground_child()
                epoch = session.return_to_manager()
                blocked_without_write(session, session.generation, epoch)
                session.take_user_control()
                session.send_user(b"wb-handoff\n")
                assert combined.wait(session, "REPL_DONE", since) == "REPL_DONE:wb-handoff"
            else:
                line = b"wb-handoff\n"
                if name == "handoff_trailing":
                    line += f": > {shlex.quote(str(marker))}\n".encode()
                session.send_user(line)
                combined.wait(session, "WAIT:", since)
                in_wait = True
                stale_line = f": > {shlex.quote(str(marker))}\n".encode()
                if name == "partial_write":
                    session.send_user(stale_line[:4])
                    session.send_user(stale_line[4:])
                elif name == "paste":
                    session.send_user(b"\x1b[200~" + stale_line + b"\x1b[201~")
                elif name == "query_reply":
                    session.send_user(b"\x1b[0n")
            epoch = session.return_to_manager()
            if name == "clean":
                blocked_without_write(session, session.generation - 1, epoch)
                blocked_without_write(session, session.generation, epoch - 1)
                complete_run(session, epoch, pids)
            else:
                blocked_without_write(session, session.generation, epoch)
                assert not marker.exists(), "trailing line executed in control wait"
            if in_wait:
                combined.boundary.flush_queued_pty(session)
                takeover_since = len(session.events)
                os.write(session._request_fd, b"TAKEOVER\n")
                combined.wait(session, "TAKEOVER_ACK:", takeover_since)
                combined.wait(session, "READY", takeover_since)
                in_wait = False
                new_input_since = len(session.events)
                session._write_all(b"__b_emit NEW_INPUT\n")
                combined.wait(session, "NEW_INPUT", new_input_since)
                combined.wait(session, "READY", new_input_since)
                assert not marker.exists(), "trailing line escaped into the parent prompt"
                if name == "clean":
                    parent = session.pid
                    session.take_user_control()
                    second_since = len(session.events)
                    session.send_user(b"wb-handoff\n")
                    combined.wait(session, f"WAIT:{parent}:", second_since)
                    epoch = session.return_to_manager()
                    complete_run(session, epoch, pids)
                    assert session.pid == parent, "second dispatch replaced the parent shell"
                    combined.boundary.flush_queued_pty(session)
                    takeover_since = len(session.events)
                    os.write(session._request_fd, b"TAKEOVER\n")
                    combined.wait(session, "TAKEOVER_ACK:", takeover_since)
                    combined.wait(session, "READY", takeover_since)
                    second_input_since = len(session.events)
                    session._write_all(b"__b_emit SECOND_NEW_INPUT\n")
                    combined.wait(session, "SECOND_NEW_INPUT", second_input_since)
                    combined.wait(session, "READY", second_input_since)
                    assert not marker.exists(), "second input return leaked stale input"
            marker_absent = not marker.exists()
        finally:
            if job_pid is not None:
                try:
                    os.kill(job_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if in_wait:
                    takeover_since = len(session.events)
                    os.write(session._request_fd, b"TAKEOVER\n")
                    combined.wait(session, "TAKEOVER_ACK:", takeover_since)
                    combined.wait(session, "READY", takeover_since)
                reap_since = len(session.events)
                session._write_all(f"wait {job_pid}; __b_emit JOB_REAPED\n".encode())
                combined.wait(session, "JOB_REAPED", reap_since)
                assert not Path(f"/proc/{job_pid}").exists(), "manual job was not reaped"
    assert not any(Path(f"/proc/{pid}").exists() for pid in pids), pids
    return {"shell": Path(shell).name, "case": name,
            "admission": "accepted" if name == "clean" else "held",
            "stale_identity_held": name == "clean",
            "successful_dispatches": 2 if name == "clean" else 0,
            "parent_prompt_and_new_input": name not in {"unsubmitted", "multiline", "repl"},
            "marker_absent": marker_absent, "owned_pids_stopped": pids}


def run() -> list[dict]:
    results = []
    for shell in (shutil.which("bash"), shutil.which("dash")):
        if shell is None:
            raise RuntimeError("Bash and dash are required for this probe")
        for name in ("unsubmitted", "multiline", "repl", "background", "suspended",
                     "handoff_trailing", "partial_write", "paste", "query_reply", "clean"):
            results.append(case(shell, name))
        # Reuse the same candidate's full input-return and known-leak controls.
        for negative in (False, True):
            source = controller_source(shell)
            with patch.object(combined, "controller_source", return_value=source):
                result = combined.case(shell, negative=negative)
            assert not any(Path(f"/proc/{pid}").exists() for pid in result["owned_pids"])
            results.append(result)
    return results


if __name__ == "__main__":
    print(json.dumps({"results": run(), "unknown": [
        "external writers bypassing the owner gate", "pre-supervisor manual daemons",
        "production adapter integration",
    ]}, indent=2))
