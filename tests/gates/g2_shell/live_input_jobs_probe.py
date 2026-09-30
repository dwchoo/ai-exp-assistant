"""Bounded input/job admission on the combined supervisor candidate.

Run: python tests/gates/g2_shell/live_input_jobs_probe.py
This fixture connects InputBoundary to control WAIT, not a production adapter.
Manual daemons and external writers remain outside this fixture's observation.
"""
from __future__ import annotations

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
from workbench.terminal.shell_g2.lifecycle import ManagedLifecycleProbe, managed_controller_source

prototype = combined.boundary.prototype


def controller_source(shell: str) -> str:
    source = managed_controller_source(shell)
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


class InputJobsSession(ManagedLifecycleProbe):
    """One bounded control writer using the existing owner/line guard."""

    _completion_events = ("START", "LIFETIME_DONE", "INPUT_BARRIER",
                          "INPUT_RELEASED", "RETURN", "WAIT")

    def __init__(self, *args, **kwargs) -> None:
        self._completion_index: int | None = None
        self._request_sequence = 0
        kwargs.pop("control_wait", None)
        super().__init__(*args, **kwargs)

    def _control_init_source(self) -> str:
        return controller_source(self.choice.executable)

    def dispatch_control(self, script: str, *, generation: int, owner_epoch: int) -> None:
        with self.boundary.lock:
            self._drain(0)
            if not self.boundary.can_dispatch(generation=generation, owner_epoch=owner_epoch):
                raise prototype.UnsafeShellState("input/job boundary is not clean")
            if self.lifecycle.request_id is not None:
                self.rearm_after_handoff()
            self._request_sequence += 1
            self.dispatch_managed(f"input-jobs-{self._request_sequence}", script)
            self.boundary.ready = False
            self.boundary.dirty = True
            self.boundary.active_command = "input-jobs"
            self._completion_index = 0

    def _on_control_event(self, event: str) -> None:
        if isinstance(self, ManagedLifecycleProbe):
            super()._on_control_event(event)
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
            if isinstance(self, ManagedLifecycleProbe) and not self.lifecycle.returned:
                self.boundary.fail_closed()
                self._completion_index = None
                return
            # RETURN alone proves neither input release nor parent control return.
            self.boundary.observe_event("DONE:input-jobs:0")
            self._completion_index = None


def assert_canonical_run(session: ManagedLifecycleProbe, since: int) -> tuple[int, int]:
    sup = combined.wait(session, "SUPERVISOR:", since)
    prepared = combined.wait(session, "CHILD_PREPARED:", since)
    _, child, group, sid = prepared.split(":")
    child = int(child)
    assert int(group) == child and int(sid) == session.pid
    assert int(sup.split(":")[2]) != child
    executed = combined.wait(session, f"EXEC_READY:{child}", since)
    started = combined.wait(session, f"EXPERIMENT_START:{child}", since)
    events = session.events[since:]
    assert events.index(prepared) < events.index(f"FOREGROUND_VERIFIED:{child}") < events.index(executed) < events.index(started)
    assert session.lifecycle.exec_ready and session.lifecycle.experiment_started and not session.lifecycle.unknown
    return int(sup.split(":")[1]), child


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
    supervisor, child = assert_canonical_run(session, run_since)
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
    pids: list[int] = []
    job_pid = None
    in_wait = False
    with tempfile.TemporaryDirectory(prefix="cw03-input-jobs-") as directory, \
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
                deadline = combined.time.monotonic() + combined.TIMEOUT
                while Path(f"/proc/{job_pid}").exists() and combined.time.monotonic() < deadline:
                    session._drain(0.01)
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
            result = input_return_case(shell, negative=negative)
            assert not any(Path(f"/proc/{pid}").exists() for pid in result["owned_pids"])
            results.append(result)
    return results


def input_return_case(shell: str, *, negative: bool) -> dict:
    """Same marker/previous-writer control through the canonical supervisor."""
    identities = []
    with tempfile.TemporaryDirectory(prefix="cw03-canonical-return-") as directory, \
         InputJobsSession(prototype.ShellChoice("bash" if Path(shell).name == "bash" else "sh", shell)) as session, \
         combined.cleanup_owned(identities):
        combined.wait(session, "READY", 0)
        parent = session.pid
        marker = Path(directory) / "spill"
        queue_marker = Path(directory) / "queued-spill"
        following = Path(directory) / "handoff-following"
        previous_writer = combined.PreviousWriter(b"\n")
        before, after = Path(directory) / "before", Path(directory) / "after"
        prepared = (f"cd {shlex.quote(directory)}; export BOUNDARY_PREPARED=kept "
                    f"BOUNDARY_TRAPS={shlex.quote(str(before))} BOUNDARY_TRAPS_AFTER={shlex.quote(str(after))}; wb-handoff\n")
        if not negative:
            prepared += f": > {shlex.quote(str(following))}\n"
        session._write_all(prepared.encode())
        first = combined.wait(session, "WAIT:", 0)
        assert first == f"WAIT:{parent}:{directory}:kept" and not following.exists()
        since = len(session.events)
        session.dispatch_managed("input-return", f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(combined.__file__).resolve()))} --fork-tree")
        supervisor, child = assert_canonical_run(session, since)
        identities.append((supervisor, combined.proc_fields(supervisor)[2]))
        descendant = combined.wait(session, "DESCENDANT:", since)
        _, raw_pid, raw_start, raw_sid = descendant.split(":")
        pid = int(raw_pid)
        identities.append((pid, int(raw_start)))
        combined.wait(session, "MIDDLE_REAPED:", since)
        main = combined.wait(session, "MAIN_RETURN:", since)
        active = combined.wait(session, "LIFETIME_ACTIVE:", since)
        fields = combined.proc_fields(pid)
        assert fields[0] == supervisor and fields[2] == int(raw_start)
        assert fields[1] == pid == int(raw_sid)
        assert main == "MAIN_RETURN:17" and active == "LIFETIME_ACTIVE:WNOHANG=0"
        assert not session.lifecycle.returned and session.lifecycle.lifetime == "active"
        assert session._foreground_group() == session.lifecycle.supervisor_group != os.getpgid(parent)
        if not negative:
            session._write_all(b"\x03")
            combined.wait(session, "SIGNAL_INT", since)
        session._write_all(f": > {shlex.quote(str(marker))}\n".encode())
        session._write_all(f": > {shlex.quote(str(queue_marker))}".encode())
        os.kill(supervisor, signal.SIGUSR2)
        reaped = combined.wait(session, "DESCENDANT_REAPED:", since)
        empty = combined.wait(session, "WAIT_EMPTY:", since)
        lifetime = combined.wait(session, "LIFETIME_DONE:", since)
        combined.wait(session, "INPUT_BARRIER", since)
        assert reaped == f"DESCENDANT_REAPED:{pid}:23" and empty == "WAIT_EMPTY:ECHILD"
        assert lifetime == f"LIFETIME_DONE:INTERRUPTS={0 if negative else 1}"
        assert "READY" not in session.events[since:]
        if not negative:
            previous_writer.close_and_discard()
            combined.boundary.flush_queued_pty(session)
        os.kill(supervisor, signal.SIGUSR1)
        combined.wait(session, "INPUT_RELEASED", since)
        returned = combined.wait(session, "RETURN:", since)
        back = combined.wait(session, "WAIT:", since)
        assert returned == "RETURN:17" and back == first and session.lifecycle.returned
        assert session.pid == parent and os.readlink(f"/proc/{parent}/cwd") == directory
        assert session._foreground_group() == os.getpgid(parent)
        assert not any(Path(f"/proc/{owned}").exists() for owned in (supervisor, child, pid))
        if not negative:
            combined.boundary.flush_queued_pty(session)
        os.write(session._request_fd, b"TAKEOVER\n")
        combined.wait(session, "TAKEOVER_ACK:", since)
        combined.wait(session, "READY", since)
        assert before.read_bytes() == after.read_bytes(), "signal trap not restored"
        assert previous_writer.drain(session) == negative
        if negative:
            deadline = combined.time.monotonic() + combined.TIMEOUT
            while not queue_marker.exists() and combined.time.monotonic() < deadline:
                session._drain(0.03)
            assert queue_marker.exists(), "queued marker did not execute in negative control"
        else:
            assert not any(path.exists() for path in (marker, following, queue_marker)), "stale marker reached prompt"
        session._write_all(b"__b_emit NEW_INPUT\n")
        combined.wait(session, "NEW_INPUT", since)
        session._drain(0.05)
        assert marker.exists() == negative and queue_marker.exists() == negative
        assert not following.exists()
        ordered = ["START", f"SUPERVISOR:{supervisor}:{session.lifecycle.supervisor_group}:SUBREAPER=1:SIGCHLD=DFL",
                   f"CHILD:{child}:{child}", main, active]
        if not negative:
            ordered.append("SIGNAL_INT")
        ordered += [reaped, empty, lifetime, "INPUT_BARRIER", "INPUT_RELEASED", returned, back,
                    f"TAKEOVER_ACK:{parent}", "READY", "NEW_INPUT"]
        later = session.events[since:]
        assert [later.index(event) for event in ordered] == sorted(later.index(event) for event in ordered)
        assert later.index(ordered[1]) < later.index(descendant) < later.index(reaped)
        return {"shell": Path(shell).name, "negative": negative, "marker_spilled": marker.exists(),
                "barriers": ["main_return", "descendant_live", "signal_observed" if not negative else "no_signal_control",
                             "descendant_reaped", "wait_empty", "input_barrier", "input_released", "parent_wait", "prompt", "new_input"],
                "queued_marker_spilled": queue_marker.exists(), "following_line_suppressed": not following.exists(),
                "parent_pid_preserved": session.pid == parent, "trap_restored": True,
                "owned_pids": [parent, supervisor, child, pid]}


if __name__ == "__main__":
    print(json.dumps({"results": run(), "unknown": [
        "external writers bypassing the owner gate", "pre-supervisor manual daemons",
        "production adapter integration",
    ]}, indent=2))
