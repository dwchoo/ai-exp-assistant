"""Unprivileged Linux host probe for one subreaper-owned experiment lifetime.

Run: python tests/gates/g2_shell/live_subreaper_lifetime_probe.py

This is a bounded feasibility experiment, not product integration or a G2 pass.
It deliberately does not exercise Docker, a shell, PTY, signals, or manual takeover.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
TIMEOUT = 5.0


def emit(**event: object) -> None:
    os.write(1, (json.dumps(event, sort_keys=True) + "\n").encode())


def proc_fields(pid: int) -> tuple[int, int, int, str]:
    """Return PPid, session, start time and state from Linux procfs."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    fields = stat[stat.rfind(")") + 2:].split()
    return int(fields[1]), int(fields[3]), int(fields[19]), fields[0]


def exit_code(status: int) -> int:
    return os.waitstatus_to_exitcode(status)


def enable_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                           ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    value = ctypes.c_int(-1)
    if libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.addressof(value), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER")
    if value.value != 1:
        raise AssertionError(f"subreaper get returned {value.value}")


def descendant(release_fd: int) -> None:
    os.setsid()
    pid = os.getpid()
    emit(event="descendant", pid=pid, session=os.getsid(0),
         start_time=proc_fields(pid)[2])
    ready, _, _ = select.select([release_fd], [], [], TIMEOUT)
    if not ready or os.read(release_fd, 1) != b"R":
        os._exit(70)
    os._exit(23)


def main_child(release_fd: int) -> None:
    middle = os.fork()
    if middle == 0:
        detached = os.fork()
        if detached == 0:
            descendant(release_fd)
        emit(event="middle_forked", pid=os.getpid(), descendant=detached)
        os._exit(19)
    os.close(release_fd)
    _, status = os.waitpid(middle, 0)
    emit(event="middle_reaped", pid=middle, exit=exit_code(status))
    os._exit(17)


def supervisor(release_fd: int) -> int:
    enable_subreaper()
    owner = os.getpid()
    emit(event="subreaper", pid=owner, get=1)
    main = os.fork()
    if main == 0:
        main_child(release_fd)
    os.close(release_fd)
    emit(event="main_started", pid=main)
    _, main_status = os.waitpid(main, 0)
    emit(event="main_reaped", pid=main, exit=exit_code(main_status))
    # The detached child is the only remaining child. WNOHANG=0 is *alive*,
    # not ECHILD and not a completion signal.
    pending, _ = os.waitpid(-1, os.WNOHANG)
    emit(event="live_wait", result=pending)
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        pid, status = os.waitpid(-1, os.WNOHANG)
        if pid:
            emit(event="descendant_reaped", pid=pid, exit=exit_code(status))
            try:
                os.waitpid(-1, os.WNOHANG)
            except ChildProcessError as exc:
                if exc.errno != errno.ECHILD:
                    raise
                emit(event="empty_wait", errno="ECHILD")
                return 0
            raise AssertionError("unexpected child remained after descendant reap")
        time.sleep(0.01)
    emit(event="timeout")
    return 1


def events_until(fd: int, wanted: set[str], deadline: float) -> list[dict]:
    events: list[dict] = []
    pending = b""
    while wanted:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"missing events: {sorted(wanted)}")
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            raise TimeoutError(f"missing events: {sorted(wanted)}")
        data = os.read(fd, 4096)
        if not data:
            raise EOFError(f"missing events: {sorted(wanted)}")
        pending += data
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            event = json.loads(line)
            events.append(event)
            wanted.discard(event["event"])
    return events


def run() -> int:
    if sys.platform != "linux" or not Path("/proc/self/stat").exists():
        print(json.dumps({"result": "inconclusive", "reason": "Linux procfs required"}))
        return 2
    release_read, release_write = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    events: list[dict] = []
    descendant_identity: tuple[int, int] | None = None
    try:
        process = subprocess.Popen(
            [sys.executable, __file__, "--supervisor", str(release_read)],
            pass_fds=(release_read,), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        os.close(release_read)
        release_read = -1
        assert process.stdout is not None
        deadline = time.monotonic() + TIMEOUT
        events += events_until(process.stdout.fileno(),
                               {"subreaper", "main_started", "middle_forked",
                                "middle_reaped", "descendant", "main_reaped",
                                "live_wait"}, deadline)
        by_name = {event["event"]: event for event in events}
        detached = by_name["descendant"]
        descendant_identity = detached["pid"], detached["start_time"]
        owner = by_name["subreaper"]["pid"]
        ppid, session, start_time, state = proc_fields(detached["pid"])
        checks = {
            "subreaper_get": by_name["subreaper"]["get"] == 1,
            "main_exit": by_name["main_reaped"]["exit"] == 17,
            "middle_exit": by_name["middle_reaped"]["exit"] == 19,
            "double_fork": by_name["middle_forked"]["descendant"] == detached["pid"],
            "adopted_live_child": ppid == owner and state not in {"Z", "X"},
            "detached_session": session == detached["pid"] == detached["session"],
            "same_child_identity": start_time == detached["start_time"],
            "no_false_completion": by_name["live_wait"]["result"] == 0,
        }
        os.write(release_write, b"R")
        os.close(release_write)
        release_write = -1
        events += events_until(process.stdout.fileno(),
                               {"descendant_reaped", "empty_wait"}, deadline)
        by_name = {event["event"]: event for event in events}
        checks["descendant_exit"] = (by_name["descendant_reaped"]["pid"] == detached["pid"]
                                     and by_name["descendant_reaped"]["exit"] == 23)
        checks["empty_after_reap"] = by_name["empty_wait"]["errno"] == "ECHILD"
        returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        checks["supervisor_exit"] = returncode == 0
        owned_pids = (owner, by_name["main_started"]["pid"],
                      by_name["middle_reaped"]["pid"], detached["pid"])
        checks["no_residual_processes"] = all(
            not Path(f"/proc/{pid}").exists() for pid in owned_pids
        )
        result = "observed" if all(checks.values()) else "failed"
        print(json.dumps({"result": result, "checks": checks,
                          "pids": {"supervisor": owner, "main": by_name["main_started"]["pid"],
                                   "middle": by_name["middle_reaped"]["pid"],
                                   "descendant": detached["pid"]},
                          "relationship": {"adopted_ppid": ppid, "session": session},
                          "exits": {"main": by_name["main_reaped"]["exit"],
                                    "middle": by_name["middle_reaped"]["exit"],
                                    "descendant": by_name["descendant_reaped"]["exit"]}},
                         sort_keys=True))
        return 0 if result == "observed" else 1
    except Exception as exc:
        print(json.dumps({"result": "inconclusive", "error": f"{type(exc).__name__}: {exc}",
                          "events": events}, sort_keys=True))
        return 2
    finally:
        # Releasing the pipe makes the detached child exit even after a failed
        # observation. Retain its identity to avoid signalling a reused PID.
        if release_read >= 0:
            os.close(release_read)
        if release_write >= 0:
            os.close(release_write)
        if process is not None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        if descendant_identity is not None:
            pid, start_time = descendant_identity
            try:
                if proc_fields(pid)[2] == start_time:
                    os.kill(pid, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--supervisor":
        try:
            raise SystemExit(supervisor(int(sys.argv[2])))
        except Exception as exc:
            emit(event="supervisor_error", error=f"{type(exc).__name__}: {exc}")
            raise SystemExit(1)
    raise SystemExit(run())
