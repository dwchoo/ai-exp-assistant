"""Bounded G2 hook/environment and launch matrix on the combined supervisor."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.gates.g2_shell import live_combined_boundary_probe as combined
from tests.gates.g2_shell.live_subreaper_lifetime_probe import proc_fields as raw_proc_fields


@contextmanager
def controlled(shell: str, directory: str, preparation: str = ""):
    source = combined.controller_source(shell)
    prototype = combined.boundary.prototype
    choice = prototype.ShellChoice("bash" if Path(shell).name == "bash" else "sh", shell)
    traps_before = Path(directory) / "traps-before"
    traps_after = Path(directory) / "traps-after"
    with patch.object(prototype, "_bash_control_init", return_value=source), \
         patch.object(prototype, "_sh_control_init", return_value=source), \
         prototype.ShellProcess(choice, control_wait=True) as session:
        combined.wait(session, "READY", 0)
        parent = session.pid
        line = (f"cd {shlex.quote(directory)}; export BOUNDARY_PREPARED=kept "
                f"BOUNDARY_TRAPS={shlex.quote(str(traps_before))} "
                f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(traps_after))}; "
                f"{preparation}wb-handoff\n")
        session._write_all(line.encode())
        combined.wait(session, f"HANDOFF:{parent}", 0)
        first_wait = combined.wait(session, "WAIT:", 0)
        assert first_wait == f"WAIT:{parent}:{directory}:kept", first_wait
        yield session, parent, first_wait, traps_before, traps_after


def run_payload(session, parent: int, first_wait: str, traps_before: Path,
                traps_after: Path, payload: str, *, start_hold: bool = False,
                identity_expected: str | None = None,
                launch_expected: str | None = None):
    since = len(session.events)
    token = base64.b64encode(payload.encode()).decode("ascii")
    os.write(session._request_fd, f"RUN:{token}\n".encode())
    combined.wait(session, "START", since)
    sup = combined.wait(session, "SUPERVISOR:", since)
    sup_pid = int(sup.split(":")[1])
    assert os.path.realpath(f"/proc/{sup_pid}/exe") == os.path.realpath(sys.executable)
    if start_hold:
        combined.wait(session, "START_BARRIER", since)
        assert not any(event.startswith("CHILD:") for event in session.events[since:])
        os.kill(sup_pid, signal.SIGUSR2)
    child = combined.wait(session, "CHILD:", since)
    child_pid = int(child.split(":")[1])
    assert child_pid not in {parent, sup_pid}
    if launch_expected is not None:
        assert combined.wait(session, "LAUNCH:", since) == launch_expected
    actual_executable = None
    if identity_expected is not None:
        expected_ready = f"CHILD_READY:{child_pid}"
        assert combined.wait(session, "CHILD_READY:", since) == expected_ready
        assert not any(event.startswith("CHILD_READY:") for event in session.events[:since])
        assert [event for event in session.events[since:]
                if event.startswith("CHILD_READY:")] == [expected_ready], (
            "child readiness was duplicated, stale, or assigned to another PID"
        )
        stage_events = session.events[since:]
        assert stage_events.index("START_BARRIER") < stage_events.index(expected_ready)
        baseline = raw_proc_fields(child_pid)
        parent_pid, session_id, started, state = combined.proc_fields(child_pid)
        assert (parent_pid, session_id, started, state) == baseline, (
            "child process identity disagrees with /proc baseline"
        )
        assert parent_pid == sup_pid and session_id == parent
        assert os.getpgid(child_pid) == int(child.split(":")[2])
        assert started > 0 and state not in {"Z", "X"}, "child identity was not live"
        actual_executable = os.readlink(f"/proc/{child_pid}/exe")
        assert os.path.realpath(actual_executable) == os.path.realpath(identity_expected), (
            f"child executable mismatch: {actual_executable} != {identity_expected}"
        )
        assert not any(event.startswith(("MAIN_RETURN:", "INPUT_BARRIER"))
                       for event in session.events[since:])
        before_release = raw_proc_fields(child_pid)
        assert before_release[:3] == baseline[:3] and before_release[3] not in {"Z", "X"}, (
            "child PID/start time changed before identity release"
        )
        assert combined.proc_fields(child_pid) == before_release
        assert [event for event in session.events[since:]
                if event.startswith("CHILD_READY:")] == [expected_ready]
        os.kill(sup_pid, signal.SIGUSR2)
    main = combined.wait(session, "MAIN_RETURN:", since)
    if identity_expected is not None:
        assert [event for event in session.events[since:]
                if event.startswith("CHILD_READY:")] == [f"CHILD_READY:{child_pid}"], (
            "child readiness was replayed after release"
        )
    assert combined.wait(session, "WAIT_EMPTY:", since) == "WAIT_EMPTY:ECHILD"
    combined.wait(session, "LIFETIME_DONE:", since)
    combined.wait(session, "INPUT_BARRIER", since)
    assert "READY" not in session.events[since:]
    combined.boundary.flush_queued_pty(session)
    os.kill(sup_pid, signal.SIGUSR1)
    combined.wait(session, "INPUT_RELEASED", since)
    returned = combined.wait(session, "RETURN:", since)
    assert combined.wait(session, "WAIT:", since) == first_wait
    assert session.pid == parent
    assert os.readlink(f"/proc/{parent}/cwd") == str(traps_before.parent)
    assert not any(Path(f"/proc/{pid}").exists() for pid in (sup_pid, child_pid))
    combined.boundary.flush_queued_pty(session)
    os.write(session._request_fd, b"TAKEOVER\n")
    combined.wait(session, f"TAKEOVER_ACK:{parent}", since)
    combined.wait(session, "READY", since)
    assert traps_before.read_bytes() == traps_after.read_bytes()
    return since, main, returned, sup_pid, child_pid, actual_executable


def environment_case(shell: str, *, venv: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="cw03-env-") as directory:
        source_file = Path(directory) / "child-source"
        source_file.write_text("export CW03_SOURCE=child_only\n", encoding="ascii")
        venv_path = Path(directory) / "venv"
        if venv:
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv_path)],
                           check=True, timeout=15, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        if Path(shell).name == "bash":
            hook = "__b_emit PRIOR_HOOK; __b_emit READY"
            preparation = f"PROMPT_COMMAND={shlex.quote(hook)}; "
        else:
            hook = '$( __b_emit PRIOR_HOOK; __b_emit READY; printf "$ " )'
            preparation = f"PS1={shlex.quote(hook)}; "
        preparation += "export CW03_SCOPE=kept; trap '__b_emit PRIOR_INT' INT; "
        if venv:
            preparation += f". {shlex.quote(str(venv_path / 'bin' / 'activate'))}; "
        with controlled(shell, directory, preparation) as (
            session, parent, first_wait, traps_before, traps_after
        ):
            expected_venv = str(venv_path) if venv else "unset"
            script = (
                'printf "CHILD_BEFORE:%s:%s:%s:%s\\n" "$PWD" '
                '"$BOUNDARY_PREPARED" "$CW03_SCOPE" "${VIRTUAL_ENV-unset}" >&9\n'
                f"cd /; export BOUNDARY_PREPARED=changed CW03_SCOPE=changed; "
                f". {shlex.quote(str(source_file))}\n"
                'printf "CHILD_AFTER:%s:%s:%s\\n" "$PWD" "$CW03_SCOPE" '
                '"$CW03_SOURCE" >&9\n'
                f'printf complex | cat > {shlex.quote(str(Path(directory) / "child-pipeline"))}\n'
                'printf "CHILD_SUBST:%s\\n" "$(printf child)" >&9\n'
            )
            if venv:
                script += ('printf "CHILD_PYTHON:%s\\n" "$(command -v python)" >&9\n'
                           f". {shlex.quote(str(venv_path / 'bin' / 'activate'))}; "
                           'deactivate; printf "CHILD_DEACT:%s\\n" '
                           '"${VIRTUAL_ENV-unset}" >&9\n')
            since, main, returned, _, _, _ = run_payload(
                session, parent, first_wait, traps_before, traps_after,
                "#WB_START_HOLD\n" + script, start_hold=True,
            )
            assert main == "MAIN_RETURN:0" and returned == "RETURN:0"
            assert combined.wait(session, "CHILD_BEFORE:", since) == (
                f"CHILD_BEFORE:{directory}:kept:kept:{expected_venv}"
            )
            assert combined.wait(session, "CHILD_AFTER:", since) == (
                "CHILD_AFTER:/:changed:child_only"
            )
            assert combined.wait(session, "CHILD_SUBST:", since) == "CHILD_SUBST:child"
            assert (Path(directory) / "child-pipeline").read_text() == "complex"
            if venv:
                assert combined.wait(session, "CHILD_PYTHON:", since) == (
                    f"CHILD_PYTHON:{venv_path / 'bin' / 'python'}"
                )
                assert combined.wait(session, "CHILD_DEACT:", since) == "CHILD_DEACT:unset"
            assert combined.wait(session, "LAUNCH:script:", since) == (
                f"LAUNCH:script:{os.path.realpath(shell)}"
            )
            later = session.events[since:]
            ack = later.index(f"TAKEOVER_ACK:{parent}")
            assert ack < later.index("PRIOR_HOOK", ack) < later.index("READY", ack)
            assert b"PRIOR_INT" in traps_before.read_bytes()
            assert os.readlink(f"/proc/{parent}/cwd") == directory
            after = len(session.events)
            session._write_all(
                b'__b_emit "PARENT_ENV:$PWD:$BOUNDARY_PREPARED:$CW03_SCOPE:'
                b'${CW03_SOURCE-unset}:${VIRTUAL_ENV-unset}"\n'
            )
            assert combined.wait(session, "PARENT_ENV:", after) == (
                f"PARENT_ENV:{directory}:kept:kept:unset:{expected_venv}"
            )
            after = len(session.events)
            variable = "PROMPT_COMMAND" if Path(shell).name == "bash" else "PS1"
            session._write_all(f'__b_emit "PARENT_HOOK:${variable}"\n'.encode())
            expected_hook = (("(venv) " if venv and variable == "PS1" else "") + hook)
            assert combined.wait(session, "PARENT_HOOK:", after) == (
                f"PARENT_HOOK:{expected_hook}"
            )
            if venv:
                after = len(session.events)
                session._write_all(b'__b_emit "PARENT_PYTHON:$(command -v python)"\n')
                assert combined.wait(session, "PARENT_PYTHON:", after) == (
                    f"PARENT_PYTHON:{venv_path / 'bin' / 'python'}"
                )
                after = len(session.events)
                session._write_all(
                    b'deactivate; __b_emit "PARENT_DEACT:${VIRTUAL_ENV-unset}:'
                    b'$(command -v python)"\n'
                )
                deactivated = combined.wait(session, "PARENT_DEACT:", after)
                assert deactivated.startswith("PARENT_DEACT:unset:")
                assert not deactivated.endswith(f"{venv_path / 'bin' / 'python'}")
            return {"shell": Path(shell).name, "case": "venv" if venv else "env",
                    "child_inherited": True, "child_changes_isolated": True,
                    "parent_pid_preserved": session.pid == parent,
                    "compatible_trap_restored": True,
                    "venv_deactivated": True if venv else None}


def incompatible_case(shell: str, kind: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="cw03-hook-") as directory:
        if kind == "prompt":
            preparation = ("PROMPT_COMMAND=':'; " if Path(shell).name == "bash"
                           else "PS1='plain$ '; ")
        else:
            preparation = "trap ':' CHLD; "
        with controlled(shell, directory, preparation) as (
            session, parent, _, traps_before, traps_after
        ):
            since = len(session.events)
            token = base64.b64encode(b":").decode("ascii")
            os.write(session._request_fd, f"RUN:{token}\n".encode())
            combined.wait(session, "HOOK_LOST", since)
            combined.wait(session, "HOOK_REJECTED", since)
            assert not any(event == "START" or event.startswith(("SUPERVISOR:", "CHILD:"))
                           for event in session.events[since:])
            assert session.boundary.needs_review
            assert os.readlink(f"/proc/{parent}/cwd") == directory
            os.write(session._request_fd, b"TAKEOVER\n")
            combined.wait(session, f"TAKEOVER_ACK:{parent}", since)
            assert traps_before.read_bytes() == traps_after.read_bytes()
            return {"shell": Path(shell).name, "case": f"incompatible_{kind}",
                    "held_before_run": True, "failure_class": "HOOK_LOST",
                    "parent_pid_preserved": session.pid == parent}


def launch_case(choice, *, mode: str, substitute: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="cw03-launch-") as directory:
        marker = Path(directory) / "shell-reinterpreted"
        if mode == "argv":
            literal = f"$(touch {marker})"
            child_code = (
                'import os,sys; '
                'os.write(9, ("CHILD_READY:%s\\n" % os.getpid()).encode()); '
                'assert os.read(int(os.environ["WB_RELEASE_FD"]), 2) == b"R\\n"; '
                'os.write(9, ("ARGV_LITERAL:" + sys.argv[1] + "\\n").encode())'
            )
            script = "#WB_ARGV_JSON\n" + json.dumps([sys.executable, "-c", child_code, literal])
            expected_event = f"ARGV_LITERAL:{literal}"
        else:
            script = ('printf "SCRIPT_CHILD:%s\\n" "$(printf child)" >&9; '
                      f'printf done | cat > {shlex.quote(str(Path(directory) / "complex-output"))}')
            expected_event = "SCRIPT_CHILD:child"
        with controlled(choice.executable, directory) as (
            session, parent, first_wait, traps_before, traps_after
        ):
            assert os.path.realpath(f"/proc/{parent}/exe") == choice.executable
            target = os.path.realpath(sys.executable if mode == "argv" else choice.executable)
            payload = "#WB_START_HOLD\n#WB_CHILD_ID_HOLD\n"
            if substitute is not None:
                payload += f"#WB_SUBSTITUTE_EXEC:{substitute}\n"
            since, main, returned, _, _, actual_executable = run_payload(
                session, parent, first_wait, traps_before, traps_after,
                payload + script, start_hold=True, identity_expected=target,
                launch_expected=f"LAUNCH:{mode}:{target}",
            )
            assert main == "MAIN_RETURN:0" and returned == "RETURN:0"
            assert combined.wait(session, expected_event, since) == expected_event
            assert os.path.realpath(actual_executable) == target
            assert not marker.exists(), "argv was interpreted by a shell"
            if mode == "script":
                assert (Path(directory) / "complex-output").read_text() == "done"
            after = len(session.events)
            session._write_all(b"__b_emit NEW_INPUT\n")
            combined.wait(session, "NEW_INPUT", after)
            return {"choice": choice.kind, "executable": choice.executable,
                    "mode": mode, "fixed_supervisor": os.path.realpath(sys.executable),
                    "actual_child_executable": os.path.realpath(actual_executable),
                    "parent_pid_preserved": session.pid == parent,
                    "literal_argv": not marker.exists() if mode == "argv" else None}


def launch_selection() -> dict:
    prototype = combined.boundary.prototype
    bash = shutil.which("bash")
    sh = shutil.which("sh")
    assert bash is not None and sh is not None
    chosen = prototype.select_shell()
    assert chosen.kind == "bash" and chosen.executable == os.path.realpath(bash)
    with tempfile.TemporaryDirectory(prefix="cw03-path-") as path_dir:
        os.symlink(sh, Path(path_dir) / "sh")
        os.symlink(shutil.which("cat"), Path(path_dir) / "cat")
        with patch.dict(os.environ, {"PATH": path_dir}):
            assert shutil.which("bash") is None
            fallback = prototype.select_shell()
            assert fallback.kind == "sh" and fallback.executable == os.path.realpath(sh)
            fallback_cases = [launch_case(fallback, mode=mode) for mode in ("argv", "script")]
    with tempfile.TemporaryDirectory(prefix="cw03-empty-path-") as empty_path:
        try:
            prototype.select_shell(path=empty_path)
        except prototype.ShellUnavailable as exc:
            guidance = str(exc)
            assert "Bash or sh" in guidance
        else:
            raise AssertionError("missing Bash and sh was accepted")
    missing = "/nonexistent/cw03-shell"
    with prototype.ShellProcess(prototype.ShellChoice("sh", missing), control_wait=True) as session:
        failed_pid = session.pid
        combined.wait(session, "EXEC_ERROR:FileNotFoundError", 0)
        assert "READY" not in session.events
    assert not Path(f"/proc/{failed_pid}").exists(), "failed launch left a shell"
    with tempfile.TemporaryDirectory(prefix="cw03-failed-child-") as directory, \
         controlled(chosen.executable, directory) as (
             session, parent, first_wait, _, _
         ):
        since = len(session.events)
        payload = "#WB_ARGV_JSON\n" + json.dumps([missing])
        os.write(session._request_fd,
                 f"RUN:{base64.b64encode(payload.encode()).decode()}\n".encode())
        combined.wait(session, "START", since)
        supervisor_pid = int(combined.wait(session, "SUPERVISOR:", since).split(":")[1])
        assert combined.wait(session, "LAUNCH:argv:", since) == f"LAUNCH:argv:{missing}"
        assert combined.wait(session, "RETURN:", since) == "RETURN:1"
        assert combined.wait(session, "WAIT:", since) == first_wait
        assert not any(event.startswith(("CHILD:", "MAIN_RETURN:", "INPUT_BARRIER"))
                       for event in session.events[since:])
        assert "READY" not in session.events[since:]
        assert session.pid == parent and not Path(f"/proc/{supervisor_pid}").exists()
        assert os.path.realpath(f"/proc/{parent}/exe") == chosen.executable
        os.write(session._request_fd, b"TAKEOVER\n")
        combined.wait(session, f"TAKEOVER_ACK:{parent}", since)
        combined.wait(session, "READY", since)
        after = len(session.events)
        session._write_all(b"__b_emit NEW_INPUT\n")
        combined.wait(session, "NEW_INPUT", after)
    wrong_shell = shutil.which("dash")
    assert wrong_shell is not None and os.path.realpath(wrong_shell) != chosen.executable
    try:
        launch_case(chosen, mode="script", substitute=wrong_shell)
    except AssertionError as exc:
        assert "child executable mismatch" in str(exc), str(exc)
    else:
        raise AssertionError("substituted shell passed executable identity check")
    return {"bash_preferred": chosen.executable, "fallback": fallback.executable,
            "fallback_cases": fallback_cases, "missing_guidance": guidance,
            "failed_exec_no_shell": True, "failed_child_no_replacement": True,
            "wrong_shell_rejected": True}


def main() -> int:
    if sys.platform != "linux":
        print(json.dumps({"result": "inconclusive", "reason": "Linux PTY required"}))
        return 2
    results = []
    unavailable = []
    unknowns = []
    try:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            assert shell is not None
            results.append(environment_case(shell, venv=False))
            results.append(environment_case(shell, venv=True))
            for kind in ("prompt", "trap"):
                results.append(incompatible_case(shell, kind))
        if shutil.which("conda") is None:
            unavailable.append({"cell": "conda activation/deactivation",
                                "reason": "conda executable unavailable"})
        else:
            unknowns.append({"cell": "conda activation/deactivation",
                             "reason": "activation availability not verified by this probe"})
        launch = launch_selection()
        chosen = combined.boundary.prototype.select_shell()
        results.extend(launch_case(chosen, mode=mode) for mode in ("argv", "script"))
        print(json.dumps({"result": "inconclusive" if unknowns else "observed",
                          "cases": results, "launch": launch,
                          "unavailable": unavailable, "unknowns": unknowns}))
        return 2 if unknowns else 0
    except Exception as exc:
        print(json.dumps({"result": "failed", "cases": results,
                          "error": f"{type(exc).__name__}: {exc}",
                          "unavailable": unavailable, "unknowns": unknowns}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
