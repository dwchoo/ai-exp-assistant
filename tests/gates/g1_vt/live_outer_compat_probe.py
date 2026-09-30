"""Bounded plain outer PTY and isolated, non-nested Herdr attach probes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from live_runtime_probe import child_tty_state, pump, visible
from live_three_pane_resize_probe import APP, FOCUS, QUIT, panes
from live_outer_tmux_probe import drain_exited_client, pane_observation
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.terminal.vt_g1.session import PtySession


def passed(result: dict[str, object]) -> bool:
    initial = result.get("initial", {})
    sizes = result.get("sizes", [])
    pids = initial.get("child_pids", [])
    mode = result.get("mode")
    # This probe uses a hidden Herdr sidebar with one outer chrome row.
    # Geometry flags cannot substitute for the actual outer/app relation.
    chrome_rows = 0 if mode == "plain" else 1
    facts = (result.get("fresh_before") == [True] * 3 and result.get("fresh_after") == [True] * 3
             and result.get("paste_checks") == [True] * 3
             and initial.get("target") == [True] * 3 and not any(initial.get("cross_routed", [True]))
             and initial.get("titles") == [True] * 3
             and isinstance(pids, list) and len(pids) == 3
             and all(type(pid) is int and pid > 0 for pid in pids) and len(set(pids)) == 3
             and mode in ("plain", "herdr")
             and len(sizes) == 2 and all(
                 item["stable_pids"] and not item["cross_routed"] and item["propagated"]
                 and item["app"] is not None
                 and item.get("outer") is not None
                 and item["app"] == [item["outer"][0], item["outer"][1] - chrome_rows]
                 and item["children"] == [[item["app"][0] // 3 - 2, item["app"][1] - 4]] * 3
                 for item in sizes)
             and initial.get("outer") == sizes[-1]["outer"]
             and initial.get("app") == sizes[-1]["app"]
             and initial.get("child_sizes") == sizes[-1]["children"])
    return facts and all(result.get(key) for key in (
        "startup_ready", "fresh_input", "three_pane_routing", "paste_fresh_and_routed",
        "resize_propagated", "local_alternate_restored", "children_cleaned", "outer_cleaned",
    )) and (result["mode"] == "plain" or all(result.get(key) for key in (
        "isolated_session_paths", "non_nested_attach", "default_state_unchanged", "session_deleted",
    )))


def call(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=5, check=False)


def run(*, herdr_mode: bool = False) -> dict[str, object]:
    omp, herdr = shutil.which("omp"), shutil.which("herdr")
    result: dict[str, object] = {"result": "unknown", "mode": "herdr" if herdr_mode else "plain"}
    clean = {key: value for key, value in os.environ.items() if not key.startswith("HERDR_")}
    result["stale_herdr_context_removed"] = sorted(key for key in os.environ if key.startswith("HERDR_"))
    result["omp_version"] = call([omp, "--version"], clean).stdout.strip() if omp else "missing"
    if result["omp_version"] != "omp/18.2.10":
        return result
    if herdr_mode:
        result["herdr_version"] = call([herdr, "--version"], clean).stdout.strip() if herdr else "missing"
        if result["herdr_version"] != "herdr 0.9.1":
            return result
        # Verify ancestry rather than interpreting inherited HERDR_ENV as nesting.
        pid = os.getpid()
        ancestors = []
        while pid > 1:
            proc = Path(f"/proc/{pid}")
            ancestors.append(proc.joinpath("comm").read_text().strip())
            pid = int(next(line.split()[1] for line in proc.joinpath("status").read_text().splitlines()
                           if line.startswith("PPid:")))
        result["non_nested_attach"] = not any(name.startswith("herdr") for name in ancestors)
        if not result["non_nested_attach"]:
            return result
        default_before = (call([herdr, "session", "list", "--json"], clean).stdout,
                          call([herdr, "status", "server"], clean).stdout)
    with tempfile.TemporaryDirectory(prefix="cw02-g1-outer-compat-") as temporary:
        root = Path(temporary)
        config = root / "omp.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        commands = []
        for role in ("manager", "worker"):
            cwd, saved = root / f"{role}-cwd", root / f"{role}-sessions"
            cwd.mkdir()
            saved.mkdir()
            commands.append(shlex.join([omp, "--no-session", "--no-tools", "--no-pty",
                                       "--no-extensions", "--no-skills", "--no-rules", "--no-title",
                                       "--cwd", str(cwd), "--session-dir", str(saved), "--config", str(config)]))
        app = [sys.executable, str(APP), "--manager-cmd", commands[0], "--worker-cmd", commands[1]]
        environment = {**clean, "TERM": "xterm-256color", "COLORTERM": "truecolor",
                       "LANG": "C.UTF-8", "PI_CODING_AGENT_DIR": str(profile)}
        clean_argv = ["/usr/bin/env"]
        for key in result["stale_herdr_context_removed"]:
            clean_argv.extend(["-u", key])
        session_name = "cw02-g1-" + secrets.token_hex(8)
        session = None
        children: set[int] = set()
        app_pid = None
        started = False
        try:
            if herdr_mode:
                runtime = root / "runtime"
                runtime.mkdir(mode=0o700)
                herdr_config = root / "herdr.toml"
                herdr_config.write_text(
                    'onboarding = false\n[terminal]\ndefault_shell = "/bin/bash"\nshell_mode = "non_login"\n'
                    '[ui]\nsidebar_start_collapsed = true\nsidebar_collapsed_mode = "hidden"\n'
                    '[update]\nversion_check = false\nmanifest_check = false\n'
                )
                environment.update({"HERDR_CONFIG_PATH": str(herdr_config),
                                    "XDG_CONFIG_HOME": str(root / "config-home"), "XDG_RUNTIME_DIR": str(runtime)})
                preflight = call([herdr, "session", "list", "--json"], environment)
                info = json.loads(preflight.stdout)
                result["isolated_session_paths"] = preflight.returncode == 0 and all(
                    Path(item[key]).is_relative_to(root) for item in info["sessions"]
                    for key in ("session_dir", "socket_path"))
                if not result["isolated_session_paths"]:
                    result["reason"] = "Herdr isolation preflight did not establish disposable paths"
                    return result
                # PtySession merges env: explicitly remove every stale context in
                # its exec child, then set only the disposable config selector.
                argv = [*clean_argv, f"HERDR_CONFIG_PATH={herdr_config}", herdr, "--session", session_name]
                session = PtySession(argv, env=environment)
                started = True
            else:
                session = PtySession(clean_argv + app, env=environment)
                app_pid = session.pid
            session.resize(45, 240)
            screen = TerminalScreen(240, 45, reply=session.write)
            parser_stream = make_stream(screen)
            queries: set[int] = set()
            class QueryStream:
                def feed(self, data: bytes) -> None:
                    queries.update(int(mode) for mode in re.findall(rb"\x1b\[\?(\d+)n", data))
                    parser_stream.feed(data)
            stream = QueryStream()
            sentinel = "G1_LOCAL_" + secrets.token_hex(8)
            stream.feed((sentinel + "\r\n").encode())
            if herdr_mode:
                deadline = time.monotonic() + 10
                pane = None
                while time.monotonic() < deadline:
                    pump(session, stream, 0.1, lambda: False)
                    response = call([herdr, "--session", session_name, "pane", "list"], environment)
                    if response.returncode == 0:
                        result["pane_list"] = json.loads(response.stdout)
                        # IDs are selected from the fresh isolated session, never inferred.
                        def find_pane(value: object) -> dict | None:
                            if isinstance(value, dict):
                                if isinstance(value.get("pane_id"), str):
                                    return value
                                for item in value.values():
                                    if found := find_pane(item):
                                        return found
                            if isinstance(value, list):
                                for item in value:
                                    if found := find_pane(item):
                                        return found
                            return None
                        pane = find_pane(result["pane_list"])
                        if pane:
                            break
                if not pane:
                    result["reason"] = "isolated Herdr pane unavailable"
                    return result
                launched = call([herdr, "--session", session_name, "pane", "run", pane["pane_id"],
                                 "exec " + shlex.join(app)], environment)
                result["pane_run_exit"] = launched.returncode
                if launched.returncode:
                    return result
            ready, _ = pump(session, stream, 25, lambda: all(marker in text for marker, text in
                              zip(("π >", "π >", "bash-"), panes(screen))))
            result["startup_ready"] = ready
            if not ready:
                result["reason"] = "three pane startup not established"
                return result
            pump(session, stream, 1, lambda: False)
            marker_base = secrets.token_hex(4)
            markers = ("M_" + marker_base, "W_" + marker_base, "H_" + marker_base)
            fresh = []
            after = []
            for index, marker in enumerate(markers):
                fresh.append(all(marker not in text for text in panes(screen)))
                if index:
                    session.write(FOCUS)
                    pump(session, stream, 0.15, lambda: False)
                for char in marker.encode():
                    session.write(bytes((char,)))
                    pump(session, stream, 0.02, lambda: False)
                found, _ = pump(session, stream, 3, lambda index=index, marker=marker:
                                marker in panes(screen)[index])
                after.append(found)
            initial = pane_observation(screen, markers)
            children.update(pid for pid in initial["child_pids"] if pid)
            result["fresh_input"] = all(fresh) and all(after)
            result["fresh_before"], result["fresh_after"] = fresh, after
            result["three_pane_routing"] = all(initial["titles"]) and all(initial["target"]) and not any(initial["cross_routed"])
            result["initial"] = initial
            if herdr_mode:
                # Child OMPs are PTY children of the actual Workbench process.
                app_pid = int(next(line.split()[1] for line in Path(
                    f"/proc/{initial['child_pids'][0]}/status").read_text().splitlines() if line.startswith("PPid:")))
            result["app_pid"] = app_pid
            app_initial = child_tty_state(app_pid)
            initial["outer"] = [240, 45]
            initial["app"] = list(app_initial.size) if app_initial else None
            chrome_rows = 45 - app_initial.size[1] if app_initial else None
            paste_ok = []
            for index in range(3):
                # Current focus is host. Cycle to each requested pane.
                session.write(FOCUS)
                a, b = f"PA_{marker_base}_{index}", f"PB_{marker_base}_{index}"
                absent = all(a not in text and b not in text for text in panes(screen))
                session.write(f"\x1b[200~{a}\n{b}\x1b[201~".encode())
                found, _ = pump(session, stream, 3, lambda index=index, a=a, b=b:
                                a in panes(screen)[index] and b in panes(screen)[index])
                paste_ok.append(absent and found and all(a not in text and b not in text
                                for other, text in enumerate(panes(screen)) if other != index))
            result["paste_fresh_and_routed"] = all(paste_ok)
            result["paste_checks"] = paste_ok
            sizes = []
            for rows, columns in ((39, 210), (45, 240)):
                screen.resize(lines=rows, columns=columns)
                session.resize(rows, columns)
                def size_match() -> bool:
                    state = child_tty_state(app_pid)
                    if state is None:
                        return False
                    expected = (state.size[0] // 3 - 2, state.size[1] - 4)
                    observation = pane_observation(screen, markers)
                    return (state.size == (columns, rows - chrome_rows)
                            and observation["child_pids"] == initial["child_pids"]
                            and observation["child_sizes"] == [list(expected)] * 3
                            and all((child := child_tty_state(pid)) and child.size == expected for pid in children))
                propagated, _ = pump(session, stream, 4, size_match)
                state = child_tty_state(app_pid)
                observation = pane_observation(screen, markers)
                sizes.append({"outer": [columns, rows], "app": list(state.size) if state else None,
                              "children": observation["child_sizes"], "propagated": propagated,
                              "stable_pids": observation["child_pids"] == initial["child_pids"],
                              "cross_routed": any(observation["cross_routed"])})
            result["sizes"] = sizes
            result["resize_propagated"] = all(item["propagated"] and item["stable_pids"] and not item["cross_routed"] for item in sizes)
            result["alternate_before_exit"] = screen._terminal_control["using_alternate"]
            session.write(QUIT)
            pump(session, stream, 3, lambda: not Path(f"/proc/{app_pid}").exists())
            if herdr_mode:
                session.write(b"\x02q")  # Explicit client detach; owned session stop follows.
            deadline = time.monotonic() + 4
            while session.poll() is None and time.monotonic() < deadline:
                pump(session, stream, 0.1, lambda: False)
            drain, _ = drain_exited_client(session, stream)
            result["local_alternate_restored"] = (result["alternate_before_exit"] and session.poll() == 0
                and drain == "eof" and not screen._terminal_control["using_alternate"] and sentinel in visible(screen))
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as error:
            result["reason"] = type(error).__name__
        finally:
            if session is not None:
                session.close()
                result["outer_cleaned"] = session.poll() is not None
            if herdr_mode:
                if started:
                    result["session_stop_exit"] = call([herdr, "session", "stop", session_name, "--json"], environment).returncode
                    result["session_delete_exit"] = call([herdr, "session", "delete", session_name, "--json"], environment).returncode
                listing = json.loads(call([herdr, "session", "list", "--json"], environment).stdout)
                result["session_deleted"] = all(item["name"] != session_name for item in listing["sessions"])
                default_after = (call([herdr, "session", "list", "--json"], clean).stdout,
                                 call([herdr, "status", "server"], clean).stdout)
                result["default_state_unchanged"] = default_before == default_after
            result["children_cleaned"] = len(children) == 3 and all(not Path(f"/proc/{pid}").exists() for pid in children)
            result["owned_child_pids"] = sorted(children)
            result["private_dsr_queries"] = sorted(queries) if session is not None else []
    result["result"] = "passed" if passed(result) else "unknown"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--herdr", action="store_true")
    args = parser.parse_args()
    outcome = run(herdr_mode=args.herdr)
    print(json.dumps(outcome, sort_keys=True))
    raise SystemExit(0 if outcome["result"] == "passed" else 1)
