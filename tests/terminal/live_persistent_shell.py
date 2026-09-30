"""Bounded actual Bash/sh CW07 adapter evidence, without gate probe imports."""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time

from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState
from workbench.terminal.shell_persistent.adapter import PersistentShell
from test_persistent_shell import ports


def wait(shell, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = shell.poll(.02)
        shell.display_bytes()  # continuously drain, do not retain user text
        if predicate(state): return state
    raise AssertionError({"timeout": shell.snapshot()})


def rejected(action):
    try: action()
    except UnsafeShellState: return True
    raise AssertionError("unsafe operation accepted")


def run(choice):
    cases, owned = {}, set()
    with tempfile.TemporaryDirectory(prefix="cw07-runtime-") as directory:
        root = Path(directory)
        (root / "bin").mkdir()
        prepared_path = str(root / "bin") + ":/usr/bin:/bin"
        env = {"PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "LANG": "C.UTF-8"}
        # Deliberately poison the app environment: it must not be copied.
        previous_app = os.environ.get("CW07_APP_ONLY")
        os.environ["CW07_APP_ONLY"] = "must-not-leak"
        shell = PersistentShell(user_environment=env, choice=choice)
        owned.add(shell.parent_pid)
        try:
            shell.send_user((f"cd {shlex.quote(directory)}; export CW07_USER=prepared; export PATH={shlex.quote(prepared_path)}; printf '%s' $$ > parent.pid\n").encode())
            wait(shell, lambda _: (root / "parent.pid").exists())
            shell.send_user(b"wb-handoff\n")
            wait(shell, lambda s: s["parent_mode"] == "control_wait")
            shell.claim_manager()
            first, auto = ports(shell)
            script = ("printf '%s\\n' \"$PWD\" \"$PATH\" \"$CW07_USER\" \"${CW07_APP_ONLY-unset}\" \"${BOUNDARY_TRAPS-unset}\" > inherited; "
                      "cd /; export CW07_USER=child-only; read -r answer; printf '%s' \"$answer\" > " + shlex.quote(str(root / "answer")))
            shell.submit(first, script, auto)
            started = wait(shell, lambda s: s["lifecycle"]["experiment_started"])
            owned.update((started["lifecycle"]["supervisor_pid"], started["lifecycle"]["child_pid"]))
            assert len(owned) == 3 and started["lifecycle"]["child_group"] != started["lifecycle"]["supervisor_group"]
            requested = shell.request_takeover()
            assert requested["takeover_requested"] and not requested["takeover_confirmed"]
            second, _ = ports(shell)
            assert rejected(lambda: shell.submit(second, "touch forbidden", auto))
            assert rejected(lambda: shell.send_user(b"premature\n"))
            confirmed = shell.confirm_takeover()
            assert confirmed["takeover_confirmed"] and confirmed["lifecycle"]["main_exit"] is None
            assert not confirmed["lifecycle"]["control_returned"] and confirmed["parent_mode"] != "manual_prompt"
            shell.send_user(b"manual-answer\n")
            barrier = wait(shell, lambda s: s["lifecycle"]["input_barrier"])
            assert not barrier["lifecycle"]["input_returned"] and not barrier["lifecycle"]["control_returned"]
            shell.release_input()
            wait(shell, lambda s: s["parent_mode"] == "manual_prompt")
            assert (root / "answer").read_text() == "manual-answer"
            inherited = (root / "inherited").read_text().splitlines()
            assert inherited == [directory, prepared_path, "prepared", "unset", "unset"], inherited
            assert not (root / "forbidden").exists()
            shell.send_user(b"printf '%s\\n' \"$PWD\" \"$CW07_USER\" \"$$\" > parent-after\n")
            wait(shell, lambda _: (root / "parent-after").exists())
            assert (root / "parent-after").read_text().splitlines() == [directory, "prepared", str(shell.parent_pid)]
            cases["takeover_and_environment"] = {"requested": requested, "confirmed": confirmed, "barrier": barrier, "inherited": inherited}
            shell.send_user(b"wb-handoff\n")
            wait(shell, lambda s: s["parent_mode"] == "control_wait")
            shell.claim_manager()
            assert rejected(lambda: shell.submit(first, ":", auto))  # no replay after rearm
            fresh, auto = ports(shell)
            shell.submit(fresh, ":", auto)
            end = wait(shell, lambda s: s["lifecycle"]["input_barrier"])
            owned.update((end["lifecycle"]["supervisor_pid"], end["lifecycle"]["child_pid"]))
            shell.release_input()
            wait(shell, lambda s: s["lifecycle"]["control_returned"])
            cases["fresh_handoff_no_replay"] = shell.snapshot()
        finally:
            shell.close()
            if previous_app is None:
                os.environ.pop("CW07_APP_ONLY", None)
            else:
                os.environ["CW07_APP_ONLY"] = previous_app
        for name, manual in {
            "unsubmitted": b"not-submitted", "repl": b"python3 -q\n",
            "job": b"sleep 30 &\n", "hook": b"PROMPT_COMMAND=':'\n" if choice.kind == "bash" else b"PS1='bad'\n",
            "trap": b"trap ':' CHLD\n",
            "unknown": b"exec 9>&-\n",
        }.items():
            with PersistentShell(user_environment=env, choice=choice) as held:
                owned.add(held.parent_pid)
                held.send_user(manual)
                if name == "repl":
                    wait(held, lambda s: s["parent_mode"] == "manual_foreground")
                elif name == "unknown":
                    wait(held, lambda s: s["phase"] == "unknown")
                elif name != "unsubmitted":
                    if name == "job":
                        wait(held, lambda _: len(held._transport._descendant_pids()) > 0 and held._transport.boundary.ready)
                    else:
                        for _ in range(5): held.poll(.02)
                    held.send_user(b"wb-handoff\n")
                    wait(held, lambda s: "manual_jobs" in s["held_reasons"] if name == "job" else "unsupported_hook_or_trap" in s["held_reasons"])
                assert rejected(held.claim_manager)
                control, automation = ports(held)
                assert rejected(lambda: held.submit(control, ":", automation))
                cases[name] = held.snapshot()
                owned.update(held._transport._descendant_pids())
    residue = [pid for pid in owned if Path(f"/proc/{pid}").exists()]
    return {"shell": choice.kind, "executable": choice.executable, "cases": cases,
            "owned_pids": sorted(owned), "residue": residue, "result": "passed" if not residue else "unknown"}


if __name__ == "__main__":
    results = [run(ShellChoice("bash", "/bin/bash")), run(ShellChoice("sh", "/bin/sh"))]
    print(json.dumps({"items": ["P-C-AC-08", "P-C-AC-26", "P-C-AC-32"], "results": results}, sort_keys=True))
    raise SystemExit(0 if all(r["result"] == "passed" for r in results) else 1)
