"""Independent, bounded public-adapter runtime checks for Bash and sh."""
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import time
from uuid import uuid4

from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState
from workbench.terminal.shell_persistent.adapter import PersistentShell


def until(shell, condition, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = shell.poll(.02)
        shell.display_bytes()
        if condition(state):
            return state
    raise AssertionError({"timeout": shell.snapshot()})


def denied(action):
    try:
        action()
    except UnsafeShellState:
        return
    raise AssertionError("unsafe operation was accepted")


def ports(shell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"],
        "ownerEpoch": state["owner_epoch"], "requestId": str(uuid4()),
        "approvalHash": "b" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True,
            "approvalValid": True}})


def proc_identity(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def run(kind, executable):
    cases, owned = {}, {}
    with tempfile.TemporaryDirectory(prefix="cw07-independent-") as temp:
        root = Path(temp)
        supplied = {"PATH": "/usr/bin:/bin", "TERM": "xterm-256color",
                    "CW07_PREPARED": "visible-to-child"}
        prior_app_secret = os.environ.get("CW07_APP_SECRET")
        os.environ["CW07_APP_SECRET"] = "not-for-shell"
        try:
            with PersistentShell(user_environment=supplied, choice=ShellChoice(kind, executable)) as shell:
                owned[shell.parent_pid] = proc_identity(shell.parent_pid)
                shell.send_user((f"cd {shlex.quote(temp)}; export CW07_PREPARED=parent; "
                                 f"export PATH={shlex.quote(temp + ':/usr/bin:/bin')}; "
                                 "printf '%s' $$ > parent.pid\n").encode())
                until(shell, lambda _: (root / "parent.pid").exists())
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                shell.claim_manager()
                first, auto = ports(shell)
                script = ("printf '%s\\n' \"$PWD\" \"$PATH\" \"$CW07_PREPARED\" "
                          "\"${CW07_APP_SECRET-unset}\" > inheritance; "
                          "cd /; export CW07_PREPARED=child-only; "
                          "read -r reply; printf '%s' \"$reply\" > " + shlex.quote(str(root / "reply")))
                shell.submit(first, script, auto)
                started = until(shell, lambda s: s["lifecycle"]["experiment_started"])
                child = started["lifecycle"]["child_pid"]
                for pid in (child, started["lifecycle"]["supervisor_pid"]):
                    owned[pid] = proc_identity(pid)
                requested = shell.request_takeover()
                assert requested["takeover_requested"] and not requested["takeover_confirmed"]
                second, _ = ports(shell)
                denied(lambda: shell.submit(second, "touch forbidden", auto))
                denied(lambda: shell.send_user(b"unconfirmed\n"))
                # A live but stopped foreground process cannot be a confirmed
                # input target. Resume it and confirm before it exits.
                os.kill(child, signal.SIGSTOP)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    stat = Path(f"/proc/{child}/stat").read_text().rsplit(") ", 1)[1].split()
                    if stat[0] in ("T", "t"):
                        break
                    time.sleep(.01)
                else:
                    raise AssertionError("child did not stop")
                denied(shell.confirm_takeover)
                denied(lambda: shell.send_user(b"stopped\n"))
                os.kill(child, signal.SIGCONT)
                until(shell, lambda s: s["lifecycle"]["main_exit"] is None and
                      s["lifecycle"]["experiment_started"])
                confirmed = shell.confirm_takeover()
                assert confirmed["takeover_confirmed"] and confirmed["lifecycle"]["main_exit"] is None
                shell.send_user(b"answer-from-user\n")
                until(shell, lambda s: s["lifecycle"]["input_barrier"])
                shell.release_input()
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                assert (root / "reply").read_text() == "answer-from-user"
                assert (root / "inheritance").read_text().splitlines() == [temp, temp + ":/usr/bin:/bin", "parent", "unset"]
                assert not (root / "forbidden").exists()
                shell.send_user(b"printf '%s\\n' \"$PWD\" \"$CW07_PREPARED\" \"$$\" > after\n")
                until(shell, lambda _: (root / "after").exists())
                assert (root / "after").read_text().splitlines() == [temp, "parent", str(shell.parent_pid)]
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                shell.claim_manager()
                denied(lambda: shell.submit(first, "touch replay", auto))
                assert not (root / "replay").exists()
                cases["stopped_identity_takeover_environment_no_replay"] = "passed"
            for label, input_bytes, expected in (
                ("unsubmitted", b"unfinished-input", "unsubmitted_or_unconsumed_input"),
                ("manual_job", ("sleep 20 & printf '%s' \"$!\" > " +
                                shlex.quote(str(root / "manual-job.pid")) + "\n").encode(), "manual_jobs"),
                ("hook", b"PROMPT_COMMAND=':'\n" if kind == "bash" else b"PS1='unsupported'\n",
                 "unsupported_hook_or_trap"),
                ("trap", b"trap ':' CHLD\n", "unsupported_hook_or_trap"),
                ("control_fd_loss", b"exec 9>&-\n", "unknown_or_manual_residue"),
            ):
                with PersistentShell(user_environment=supplied, choice=ShellChoice(kind, executable)) as held:
                    owned[held.parent_pid] = proc_identity(held.parent_pid)
                    held.send_user(input_bytes)
                    if label == "unsubmitted":
                        state = until(held, lambda s: expected in s["held_reasons"])
                    elif label == "control_fd_loss":
                        state = until(held, lambda s: s["phase"] == "unknown")
                    else:
                        if label == "manual_job":
                            until(held, lambda s: (root / "manual-job.pid").exists() and
                                  s["parent_mode"] == "manual_prompt")
                            job_pid = int((root / "manual-job.pid").read_text())
                            owned[job_pid] = proc_identity(job_pid)
                        held.send_user(b"wb-handoff\n")
                        state = until(held, lambda s: expected in s["held_reasons"])
                    assert expected in state["held_reasons"]
                    denied(held.claim_manager)
                    control, auto = ports(held)
                    denied(lambda: held.submit(control, ":", auto))
                    cases[label] = state["phase"]
        finally:
            if prior_app_secret is None:
                os.environ.pop("CW07_APP_SECRET", None)
            else:
                os.environ["CW07_APP_SECRET"] = prior_app_secret
    residue = [pid for pid, start in owned.items() if start is not None and proc_identity(pid) == start]
    assert not residue, {"residue": residue}
    return {"shell": kind, "cases": cases, "owned_pids": sorted(owned), "residue": residue}


def run_unknown_input(kind, executable):
    with tempfile.TemporaryDirectory(prefix="cw07-independent-") as temp:
        marker = Path(temp) / "unknown-input-reached-shell"
        with PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "TERM": "xterm"},
                             choice=ShellChoice(kind, executable)) as shell:
            parent_pid, parent_start = shell.parent_pid, proc_identity(shell.parent_pid)
            shell.send_user(b"exec 9>&-\n")
            state = until(shell, lambda s: s["phase"] == "unknown")
            assert state["input_owner"] == "user"
            assert "unknown_or_manual_residue" in state["held_reasons"]
            refused = False
            try:
                shell.send_user(("printf reached > " + shlex.quote(str(marker)) + "\n").encode())
            except UnsafeShellState:
                refused = True
            for _ in range(5):
                shell.poll(.02)
                shell.display_bytes()
            marker_seen = marker.exists()
        residue = parent_start is not None and proc_identity(parent_pid) == parent_start
        assert not residue, "owned parent PID survived close"
        assert not marker_seen, "unknown-state input reached shell PTY"
        assert refused, "unknown-state send_user accepted PTY input"
        return {"shell": kind, "phase": state["phase"], "held_reasons": state["held_reasons"],
                "refused": refused, "marker_seen": marker_seen, "residue": []}


if __name__ == "__main__":
    choices = {"bash": "/bin/bash", "sh": "/bin/sh"}
    unknown = sys.argv[1:2] == ["--unknown"]
    selected = sys.argv[2:] if unknown else sys.argv[1:]
    selected = selected or list(choices)
    function = run_unknown_input if unknown else run
    print(json.dumps({"results": [function(kind, choices[kind]) for kind in selected]}, sort_keys=True))
