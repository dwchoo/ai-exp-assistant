"""Manual-residue feasibility evidence, with explicit observation-gap holds.

The escaped fixture's identity is an external test oracle, not discovery of
arbitrary manual daemons. Only owned fixture processes are explicitly cleaned.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.gates.g2_shell import live_env_launch_probe as env

combined = env.combined


def daemon_fixture() -> None:
    child = os.fork()
    if child:
        os._exit(0)
    os.setsid()
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    os.write(9, f"MANUAL_DAEMON:{os.getpid()}:{combined.proc_fields(os.getpid())[2]}\n".encode())
    # No further spawn is possible in this fixed fixture. This is not a
    # statement about arbitrary user code or an experiment supervisor.
    signum = signal.sigtimedwait({signal.SIGUSR1}, 10.0)
    os._exit(0 if signum else 70)


@contextmanager
def own_orphans():
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER")
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    identities = []
    try:
        yield identities
    finally:
        try:
            for pid, started in identities:
                try:
                    if combined.proc_fields(pid)[2] == started:
                        os.kill(pid, signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
        finally:
            if libc.prctl(36, previous.value, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "restore child subreaper")


def controller_source(shell: str) -> str:
    source = combined.controller_source(shell)
    jobs = "builtin jobs" if Path(shell).name == "bash" else "command jobs"
    source = source.replace(
        "                    __b_emit START",
        f'''                    {jobs} -p > "$WB_MANUAL_JOBS"
                    if [ -s "$WB_MANUAL_JOBS" ]; then
                        __b_emit MANUAL_ACTIVE
                        continue
                    fi
                    if [ "$WB_MANUAL_OBSERVATION" = unknown ]; then
                        __b_emit MANUAL_UNKNOWN
                        continue
                    fi
                    __b_emit START''',
    )
    return source.replace(
        "                TAKEOVER)",
        '''                CONFIRM_MANUAL)
                    __b_emit MANUAL_UNKNOWN
                    ;;
                FRESH_MANUAL_CHECK)
                    WB_MANUAL_OBSERVATION=clear
                    __b_emit FRESH_MANUAL_CHECK
                    ;;
                TAKEOVER)''',
    )


def run_request(session, script: str) -> int:
    since = len(session.events)
    token = base64.b64encode(script.encode()).decode("ascii")
    os.write(session._request_fd, f"RUN:{token}\n".encode())
    return since


def decision(session, since: int) -> str:
    deadline = time.monotonic() + combined.TIMEOUT
    while time.monotonic() < deadline:
        session._drain(0.01)
        session.display_bytes()
        for event in session.events[since:]:
            if event in {"START", "MANUAL_ACTIVE", "MANUAL_UNKNOWN"}:
                return event
    raise TimeoutError("no manual-residue admission decision")


def fresh_check_after_owned_cleanup(session, identity: tuple[int, int]) -> None:
    pid, _ = identity
    try:
        fields = combined.proc_fields(pid)
    except FileNotFoundError:
        fields = None
    if fields is not None:
        # A changed PID is not proof about this old manual scope either.
        raise combined.boundary.prototype.UnsafeShellState("cleanup has no verified process disappearance")
    since = len(session.events)
    os.write(session._request_fd, b"FRESH_MANUAL_CHECK\n")
    combined.wait(session, "FRESH_MANUAL_CHECK", since)


def case(shell: str, kind: str) -> dict:
    source = controller_source(shell)
    pids = []
    with own_orphans() as orphans, tempfile.TemporaryDirectory(prefix="cw03-manual-residue-") as directory:
        jobs_file = Path(directory) / "manual-jobs"
        traps_before = Path(directory) / "traps-before"
        traps_after = Path(directory) / "traps-after"
        preparation = (f"cd {shlex.quote(directory)}; export BOUNDARY_PREPARED=kept "
                       f"BOUNDARY_TRAPS={shlex.quote(str(traps_before))} "
                       f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(traps_after))} "
                       f"WB_MANUAL_JOBS={shlex.quote(str(jobs_file))} "
                       f"WB_MANUAL_OBSERVATION={'unknown' if kind == 'escaped' else 'clear'}; ")
        prototype = combined.boundary.prototype
        with patch.object(prototype, "_bash_control_init", return_value=source), \
             patch.object(prototype, "_sh_control_init", return_value=source), \
             prototype.ShellProcess(prototype.ShellChoice(
                 "bash" if Path(shell).name == "bash" else "sh", shell
             ), control_wait=True) as session:
            parent = session.pid
            pids.append(parent)
            combined.wait(session, "READY", 0)
            if kind == "background":
                manual = 'sleep 30 & __b_emit "MANUAL_JOB:$!"; wb-handoff\n'
            else:
                manual = (f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
                          "--daemon; wb-handoff\n")
            session._write_all((preparation + manual).encode())
            first_wait = combined.wait(session, f"WAIT:{parent}:", 0)
            event = combined.wait(session, "MANUAL_JOB:" if kind == "background" else "MANUAL_DAEMON:", 0)
            parts = event.split(":")
            pid = int(parts[1])
            started = combined.proc_fields(pid)[2]
            if kind == "escaped":
                assert int(parts[2]) == started, "external fixture start-time oracle disagreed"
            identity = (pid, started)
            pids.append(pid)
            if kind == "escaped":
                orphans.append(identity)
                deadline = time.monotonic() + combined.TIMEOUT
                while combined.proc_fields(pid)[0] != os.getpid():
                    assert time.monotonic() < deadline, "daemon was not adopted by the test oracle"
                    session._drain(0.01)
                assert pid not in session._descendant_pids()
            marker = Path(directory) / "experiment-started"
            since = run_request(session, f": > {shlex.quote(str(marker))}")
            expected = "MANUAL_ACTIVE" if kind == "background" else "MANUAL_UNKNOWN"
            assert decision(session, since) == expected, "manual residue was automatically accepted"
            assert not marker.exists() and "START" not in session.events[since:]
            if kind == "background":
                assert str(pid) in jobs_file.read_text().split()
            else:
                assert not jobs_file.read_text().strip(), "escaped daemon was a visible shell job"
                confirm_since = len(session.events)
                os.write(session._request_fd, b"CONFIRM_MANUAL\n")
                combined.wait(session, "MANUAL_UNKNOWN", confirm_since)
                since = run_request(session, ":")
                assert decision(session, since) == "MANUAL_UNKNOWN"
                assert combined.proc_fields(pid)[2] == started and combined.proc_fields(pid)[3] not in {"X", "Z"}
                with patch.object(os, "write") as control_write:
                    try:
                        fresh_check_after_owned_cleanup(session, identity)
                    except prototype.UnsafeShellState:
                        pass
                    else:
                        raise AssertionError("live manual daemon was cleared by a recheck")
                    control_write.assert_not_called()
            # Explicit user cleanup of a fixture-attributed PID, never backend
            # automatic termination or termination of an unattributed process.
            assert combined.proc_fields(pid)[2] == started
            if kind == "escaped":
                os.kill(pid, signal.SIGUSR1)
                assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0
                fresh_check_after_owned_cleanup(session, identity)
            takeover_since = len(session.events)
            os.write(session._request_fd, b"TAKEOVER\n")
            combined.wait(session, f"TAKEOVER_ACK:{parent}", takeover_since)
            combined.wait(session, "READY", takeover_since)
            if kind == "background":
                cleanup_since = len(session.events)
                session._write_all(f"kill {pid}; wait {pid}; __b_emit USER_CLEANED\n".encode())
                combined.wait(session, "USER_CLEANED", cleanup_since)
                combined.wait(session, "READY", cleanup_since)
                assert not Path(f"/proc/{pid}").exists()
            handoff_since = len(session.events)
            session._write_all(b"wb-handoff\n")
            combined.wait(session, f"HANDOFF:{parent}", handoff_since)
            combined.wait(session, f"WAIT:{parent}:", handoff_since)
            since = len(session.events)
            _, main, returned, sup, child, _ = env.run_payload(
                session, parent, first_wait, traps_before, traps_after, ":"
            )
            pids.extend((sup, child))
            assert main == "MAIN_RETURN:0" and returned == "RETURN:0"
            assert session.pid == parent and not marker.exists()
            assert "START" in session.events[since:]
            assert not Path(f"/proc/{pid}").exists()
            result = {"shell": Path(shell).name, "case": kind,
                      "initial_admission": expected, "same_parent_recheck_proceeded": True,
                      "prior_scope": "unknown" if kind == "escaped" else "observed_job",
                      "prior_scope_completed": False, "backend_auto_kills": 0,
                      "user_confirmation_completed": False, "owned_pids": pids}
    assert not any(Path(f"/proc/{pid}").exists() for pid in pids), pids
    return result


def run() -> list[dict]:
    results = []
    for shell in (shutil.which("bash"), shutil.which("dash")):
        if shell is None:
            raise RuntimeError("Bash and dash required")
        for kind in ("background", "escaped"):
            results.append(case(shell, kind))
    return results


if __name__ == "__main__":
    if sys.argv[1:] == ["--daemon"]:
        daemon_fixture()
    else:
        print(json.dumps({"results": run(), "unknown": [
            "arbitrary pre-supervisor daemon discovery", "prior escaped manual scope lifetime",
            "production adapter integration",
        ], "excluded": ["conda-specific validation", "delegated-cgroup prerequisite"]}, indent=2))
