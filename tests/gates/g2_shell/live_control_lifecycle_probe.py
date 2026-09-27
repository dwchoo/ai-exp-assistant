"""Bash/dash runtime evidence for the bounded managed lifecycle transport."""

from __future__ import annotations

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

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.gates.g2_shell import live_combined_boundary_probe as combined
from workbench.terminal.shell_g2.lifecycle import ManagedLifecycleProbe
from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState


def rejects(action) -> None:
    try:
        action()
    except UnsafeShellState:
        return
    raise AssertionError("unknown/incomplete lifecycle granted an operation")


def case(shell: str, fault: str) -> dict:
    source = combined.controller_source(shell).replace(
        "                    __b_emit START", "                    __b_emit ACCEPT\n                    __b_emit START"
    )
    identities = []
    with patch.object(combined.boundary.prototype, "_bash_control_init", return_value=source), \
         patch.object(combined.boundary.prototype, "_sh_control_init", return_value=source), \
         tempfile.TemporaryDirectory(prefix="cw03-lifecycle-") as directory, \
         ManagedLifecycleProbe(ShellChoice("bash" if Path(shell).name == "bash" else "sh", shell)) as session, \
         combined.cleanup_owned(identities):
        combined.wait(session, "READY", 0)
        marker = Path(directory) / "stale-input"
        before = Path(directory) / "traps-before"
        after = Path(directory) / "traps-after"
        session._write_all(
            (f"export BOUNDARY_PREPARED=kept BOUNDARY_TRAPS={shlex.quote(str(before))} "
             f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(after))}; wb-handoff\n").encode()
        )
        combined.wait(session, f"WAIT:{session.pid}:", 0)
        since = len(session.events)
        if fault == "request_fd":
            os.close(session._request_fd)
            try:
                session.dispatch_managed("one", ":")
            except OSError:
                pass
            else:
                raise AssertionError("closed request FD accepted a request")
        else:
            script = ("#WB_START_HOLD\n:" if fault == "supervisor_start" else "#WB_TREE_FIXTURE\n"
                      f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(combined.__file__).resolve()))} --fork-tree"
                      if fault in {"none", "supervisor_start"} else ":")
            session.dispatch_managed("one", script)
            sup = combined.wait(session, "SUPERVISOR:", since)
            supervisor_pid = int(sup.split(":")[1])
            identities.append((supervisor_pid, combined.proc_fields(supervisor_pid)[2]))
            rejects(lambda: session.dispatch_managed("replay", ":"))
            rejects(lambda: session.release_control())
            rejects(lambda: session.send_user(b"unsafe\n"))
            if fault == "none":
                descendant = combined.wait(session, "DESCENDANT:", since)
                _, raw_pid, raw_start, _ = descendant.split(":")
                identities.append((int(raw_pid), int(raw_start)))
                combined.wait(session, "LIFETIME_ACTIVE:", since)
                assert session.lifecycle.main_exit == 17
                assert session.lifecycle.lifetime == "active"
                rejects(session.release_input)
                os.kill(supervisor_pid, signal.SIGUSR2)
            if fault == "supervisor_start":
                combined.wait(session, "START_BARRIER", since)
                os.kill(supervisor_pid, signal.SIGKILL)
                combined.wait(session, "RETURN:", since)
            else:
                combined.wait(session, "INPUT_BARRIER", since)
                session._write_all(f": > {shlex.quote(str(marker))}\n".encode())
            if fault == "supervisor_start":
                assert not session.lifecycle.experiment_started
                assert session.lifecycle.lifetime == "unknown"
            elif fault == "flush":
                with patch("workbench.terminal.shell_g2.lifecycle.termios.tcflush",
                           side_effect=OSError("fixture flush failure")):
                    try:
                        session.release_input()
                    except OSError:
                        pass
                    else:
                        raise AssertionError("flush failure released input")
            elif fault == "supervisor":
                os.kill(supervisor_pid, signal.SIGKILL)
                combined.wait(session, "RETURN:", since)
            elif fault == "event_fd":
                os.close(session._control_fd)
                try:
                    session._drain(0)
                except OSError:
                    pass
            else:
                session.release_input()
                combined.wait(session, "INPUT_RELEASED", since)
                combined.wait(session, "RETURN:", since)
                combined.wait(session, f"WAIT:{session.pid}:", since)
                assert session.lifecycle.returned, session.lifecycle
                if fault == "control_eof":
                    os.close(session._request_fd)
                    combined.wait(session, "CONTROL_LOST", since)
                else:
                    session.release_control()
                    combined.wait(session, "TAKEOVER_ACK:", since)
                    combined.wait(session, "READY", since)
                    session._write_all(b"__b_emit NEW_INPUT\n")
                    combined.wait(session, "NEW_INPUT", since)
                    ordered = ["ACCEPT", "SUPERVISOR:", "CHILD:", "EXPERIMENT_START:",
                               "MAIN_RETURN:", "LIFETIME_ACTIVE:", "WAIT_EMPTY:",
                               "LIFETIME_DONE:", "INPUT_BARRIER", "INPUT_RELEASED",
                               "RETURN:", "WAIT:"]
                    events = session.events[since:]
                    indices = [next(i for i, event in enumerate(events) if event.startswith(kind))
                               for kind in ordered]
                    assert indices == sorted(indices), events
        if fault != "none":
            assert session.lifecycle.unknown, session.lifecycle
            assert not session.lifecycle.returned
            for action in (session.release_input, session.release_control,
                           lambda: session.dispatch_managed("replay", ":")):
                # Closed event FD can raise OSError before the explicit hold.
                try:
                    rejects(action)
                except OSError:
                    assert session.lifecycle.unknown
            assert "INPUT_RELEASED" not in session.events[since:] or fault == "control_eof"
        deadline = time.monotonic() + 0.08
        while fault != "event_fd" and time.monotonic() < deadline:
            session._drain(0.01)
        assert not marker.exists(), "stale input escaped the held/flush boundary"
        return {"shell": Path(shell).name, "fault": fault,
                "events": session.events[since:], "unknown": session.lifecycle.unknown,
                "lifetime": session.lifecycle.lifetime,
                "input_returned": session.lifecycle.input_returned,
                "control_returned": session.lifecycle.control_returned,
                "returned": session.lifecycle.returned, "stale_marker": False}


def main() -> int:
    results = []
    for shell in (shutil.which("bash"), shutil.which("dash")):
        if shell is None:
            print(json.dumps({"result": "inconclusive", "reason": "Bash/dash unavailable"}))
            return 2
        for fault in ("none", "request_fd", "flush", "supervisor", "supervisor_start", "event_fd", "control_eof"):
            try:
                results.append(case(shell, fault))
            except Exception as exc:
                print(json.dumps({"result": "failed", "cases": results, "fault": fault,
                                  "shell": shell, "error": f"{type(exc).__name__}: {exc}"}))
                return 1
    print(json.dumps({"result": "observed", "cases": results}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
