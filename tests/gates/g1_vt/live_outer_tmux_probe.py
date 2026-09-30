"""Bounded host probe for the G1 three-pane candidate inside external tmux.

The tmux server, profile, and OMP sessions live under one disposable directory.
Only predicates and process identifiers are reported; PTY contents are not retained.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from live_runtime_probe import child_tty_state, pump, visible
from live_three_pane_resize_probe import APP, FOCUS, QUIT, panes
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.terminal.vt_g1.session import PtySession


def tmux_value(tmux: str, socket: Path, expression: str) -> str | None:
    completed = subprocess.run(
        [tmux, "-S", str(socket), "display-message", "-p", expression],
        text=True, capture_output=True, timeout=3, check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def tmux_capture_flags(tmux: str, socket: Path) -> dict[str, bool] | None:
    completed = subprocess.run(
        [tmux, "-S", str(socket), "capture-pane", "-p", "-t", "g1-outer:0.0"],
        text=True, capture_output=True, timeout=3, check=False,
    )
    if completed.returncode:
        return None
    return startup_flags(completed.stdout)


def startup_flags(text: str) -> dict[str, bool]:
    lowered = text.lower()
    return {
        "omp_version": "18.2.10" in text,
        "composer": "π >" in text,
        "setup": "setup" in lowered,
        "provider": "provider" in lowered,
        "select": "select" in lowered,
        "error": "error" in lowered,
        "welcome": "welcome" in lowered,
    }


def process_term(pid: int | None) -> str | None:
    if pid is None:
        return None
    try:
        environment = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    return next((entry[5:].decode("ascii", "replace") for entry in environment.split(b"\0")
                 if entry.startswith(b"TERM=")), None)


def drain_exited_client(session: PtySession, stream: object, seconds: float = 1.0) -> tuple[str, int]:
    """Feed bytes still queued on a closed client's PTY before checking VT state."""
    deadline = time.monotonic() + seconds
    drained = 0
    while time.monotonic() < deadline:
        try:
            data = os.read(session.master_fd, 65536)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        except OSError as exc:
            if exc.errno == errno.EIO:
                return "eof", drained
            raise
        if not data:
            return "eof", drained
        drained += len(data)
        stream.feed(data)
    return "timeout", drained


def pane_observation(screen: TerminalScreen, expected: tuple[str, str, str]) -> dict[str, object]:
    texts = panes(screen)
    matches = [re.search(r"pid=(\d+)", text) for text in texts]
    pids = [int(match[1]) if match else None for match in matches]
    return {
        "titles": [title in text for title, text in zip(
            ("MANAGER OMP", "WORKER OMP", "HOST SHELL"), texts)],
        "target": [marker in text for marker, text in zip(expected, texts)],
        "cross_routed": [any(marker in other for j, other in enumerate(texts) if j != i)
                         for i, marker in enumerate(expected)],
        "child_pids": pids,
        "child_sizes": [list(state.size) if (pid and (state := child_tty_state(pid)))
                        else None for pid in pids],
    }


def stage_observation(session: PtySession, stream: object, screen: TerminalScreen,
                      expected: tuple[str, str, str], required_targets: int,
                      child_size: list[int], previous_pids: list[int] | None = None
                      ) -> tuple[dict[str, object], dict[str, object], bool, int]:
    """Wait for one complete same-size frame, not just the last input marker.

    tmux can deliver a host echo before the resized OMP redraw is complete.
    Missing title/marker/geometry evidence still fails after the bounded wait.
    """
    first = observation = pane_observation(screen, expected)

    def complete() -> bool:
        nonlocal observation
        observation = pane_observation(screen, expected)
        pids = observation["child_pids"]
        return bool(all(observation["titles"])
                    and all(observation["target"][:required_targets])
                    and not any(observation["cross_routed"])
                    and observation["child_sizes"] == [child_size] * 3
                    and len(pids) == 3 and all(type(pid) is int and pid > 0 for pid in pids)
                    and len(set(pids)) == 3
                    and (previous_pids is None or pids == previous_pids))

    settled, drained = pump(session, stream, 3, complete)
    return first, observation, settled, drained


def run() -> dict[str, object]:
    tmux = shutil.which("tmux")
    omp = shutil.which("omp")
    if not tmux or not omp:
        return {"result": "inconclusive_missing_binary", "tmux_found": bool(tmux), "omp_found": bool(omp)}
    versions = {
        "tmux": subprocess.run([tmux, "-V"], text=True, capture_output=True, timeout=3).stdout.strip(),
        "omp": subprocess.run([omp, "--version"], text=True, capture_output=True, timeout=5).stdout.strip(),
    }
    result: dict[str, object] = {"versions": versions, "result": "inconclusive"}
    if versions != {"tmux": "tmux 3.4", "omp": "omp/18.2.10"}:
        result["result"] = "inconclusive_version"
        return result

    with tempfile.TemporaryDirectory(prefix="cw02-g1-outer-tmux-") as temporary:
        root = Path(temporary)
        socket = root / "tmux.sock"
        tmux_config = root / "tmux.conf"
        tmux_config.write_text("")
        omp_config = root / "omp.yml"
        omp_config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        commands = []
        for name in ("manager", "worker"):
            cwd = root / f"{name}-cwd"
            sessions = root / f"{name}-sessions"
            cwd.mkdir()
            sessions.mkdir()
            commands.append(shlex.join([
                omp, "--no-session", "--no-tools", "--no-pty", "--no-extensions",
                "--no-skills", "--no-rules", "--no-title", "--cwd", str(cwd),
                "--session-dir", str(sessions), "--config", str(omp_config),
            ]))
        app_command = shlex.join([
            sys.executable, str(APP), "--manager-cmd", commands[0],
            "--worker-cmd", commands[1],
        ])
        environment = os.environ.copy()
        environment.update({
            "TERM": "xterm-256color", "COLORTERM": "truecolor",
            "LANG": "en_US.UTF-8", "PI_CODING_AGENT_DIR": str(profile),
        })
        session: PtySession | None = None
        observed_pids: set[int] = set()
        server_pid: int | None = None
        app_pid: int | None = None
        started = False
        restore_sentinel = "G1_LOCAL_" + secrets.token_hex(12)
        try:
            created = subprocess.run([
                tmux, "-f", str(tmux_config), "-S", str(socket),
                "new-session", "-d", "-s", "g1-outer", "-x", "240", "-y", "45",
                app_command,
            ], cwd=root, env=environment, capture_output=True, timeout=5, check=False)
            result["tmux_new_session_exit"] = created.returncode
            if created.returncode != 0:
                result["result"] = "inconclusive_tmux_start"
                return result
            started = True
            server_text = tmux_value(tmux, socket, "#{pid}")
            app_text = tmux_value(tmux, socket, "#{pane_pid}")
            server_pid = int(server_text) if server_text and server_text.isdigit() else None
            app_pid = int(app_text) if app_text and app_text.isdigit() else None
            result["tmux_server_pid"] = server_pid
            result["tmux_pane_pid"] = app_pid
            result["tmux_pane_term"] = process_term(app_pid)

            session = PtySession([tmux, "-S", str(socket), "attach-session", "-t", "g1-outer"], env=environment)
            session.resize(45, 240)
            screen = TerminalScreen(240, 45, reply=session.write)
            stream = make_stream(screen)
            stream.feed((restore_sentinel + "\r\n").encode())
            ready, byte_count = pump(session, stream, 18, lambda: all(
                marker in text for marker, text in zip(("π >", "π >", "bash-"), panes(screen))))
            result["startup_at_18s"] = [
                marker in text for marker, text in zip(("π >", "π >", "bash-"), panes(screen))]
            if not ready:
                later, drained = pump(session, stream, 12, lambda: all(
                    marker in text for marker, text in zip(("π >", "π >", "bash-"), panes(screen))))
                ready = later
                byte_count += drained
            result["startup_ready"] = ready
            result["tmux_window_initial"] = tmux_value(tmux, socket, "#{window_width}x#{window_height}")
            if not ready:
                startup = pane_observation(screen, ("MGR_TMUX", "WRK_TMUX", "HOST_TMUX"))
                observed_pids.update(pid for pid in startup["child_pids"] if pid)
                result["startup_titles"] = startup["titles"]
                result["startup_child_pids"] = startup["child_pids"]
                result["startup_composers"] = [
                    marker in text for marker, text in zip(("π >", "π >", "bash-"), panes(screen))]
                result["startup_outer_flags"] = startup_flags(visible(screen))
                result["startup_tmux_capture_flags"] = tmux_capture_flags(tmux, socket)
                result["startup_child_sizes"] = startup["child_sizes"]
                result["pty_bytes_drained"] = byte_count
                result["result"] = "inconclusive_startup"
                return result

            fresh_before = {"manager": "MGR_TMUX" not in panes(screen)[0]}
            session.write(b"MGR_TMUX")
            manager_visible, drained = pump(session, stream, 3, lambda: "MGR_TMUX" in panes(screen)[0])
            byte_count += drained
            fresh_before["worker"] = "WRK_TMUX" not in panes(screen)[1]
            session.write(FOCUS + b"WRK_TMUX")
            worker_visible, drained = pump(session, stream, 3, lambda: "WRK_TMUX" in panes(screen)[1])
            byte_count += drained
            initial_first, initial, initial_settled, drained = stage_observation(
                session, stream, screen, ("MGR_TMUX", "WRK_TMUX", "HOST_TMUX"), 2, [78, 40])
            byte_count += drained
            observed_pids.update(pid for pid in initial["child_pids"] if pid)

            screen.resize(lines=39, columns=210)
            session.resize(39, 210)
            _, drained = pump(session, stream, 3, lambda: tmux_value(
                tmux, socket, "#{window_width}x#{window_height}") == "210x38")
            byte_count += drained
            result["tmux_window_small"] = tmux_value(tmux, socket, "#{window_width}x#{window_height}")
            fresh_before["worker_append"] = "WRK_TMUX_W" not in panes(screen)[1]
            session.write(b"_W")
            worker_append_visible, drained = pump(session, stream, 3, lambda: "WRK_TMUX_W" in panes(screen)[1])
            byte_count += drained
            fresh_before["host"] = "HOST_TMUX" not in panes(screen)[2]
            session.write(FOCUS + b"printf 'HOST_%s\\n' TMUX\r")
            host_visible, drained = pump(session, stream, 3, lambda: "HOST_TMUX" in panes(screen)[2])
            byte_count += drained
            small_first, small, small_settled, drained = stage_observation(
                session, stream, screen, ("MGR_TMUX", "WRK_TMUX_W", "HOST_TMUX"),
                3, [68, 34], initial["child_pids"])
            byte_count += drained
            observed_pids.update(pid for pid in small["child_pids"] if pid)

            screen.resize(lines=45, columns=240)
            session.resize(45, 240)
            _, drained = pump(session, stream, 3, lambda: tmux_value(
                tmux, socket, "#{window_width}x#{window_height}") == "240x44")
            byte_count += drained
            result["tmux_window_restored"] = tmux_value(tmux, socket, "#{window_width}x#{window_height}")
            fresh_before["manager_append"] = "MGR_TMUX_M" not in panes(screen)[0]
            session.write(FOCUS + b"_M")
            manager_append_visible, drained = pump(session, stream, 3, lambda: "MGR_TMUX_M" in panes(screen)[0])
            byte_count += drained
            paste_markers = ("PASTE_TMUX_A", "PASTE_TMUX_B")
            paste_absent_before = all(marker not in panes(screen)[0] for marker in paste_markers)
            session.write(b"\x1b[200~PASTE_TMUX_A\nPASTE_TMUX_B\x1b[201~")
            paste_after, drained = pump(session, stream, 3, lambda: all(
                marker in panes(screen)[0] for marker in paste_markers))
            byte_count += drained
            restored_first, restored, restored_settled, drained = stage_observation(
                session, stream, screen, ("MGR_TMUX_M", "WRK_TMUX_W", "HOST_TMUX"),
                3, [78, 40], initial["child_pids"])
            byte_count += drained
            result["stages"] = {"initial": initial, "small": small, "restored": restored}
            result["stage_first"] = {"initial": initial_first, "small": small_first,
                                     "restored": restored_first}
            result["stage_settled"] = {"initial": initial_settled, "small": small_settled,
                                       "restored": restored_settled}
            observed_pids.update(pid for pid in restored["child_pids"] if pid)
            result["pty_bytes_drained"] = byte_count
            result["fresh_input_before"] = fresh_before
            result["fresh_input_after"] = {
                "manager": manager_visible, "worker": worker_visible,
                "worker_append": worker_append_visible, "host": host_visible,
                "manager_append": manager_append_visible,
            }
            result["paste_absent_before"] = paste_absent_before
            result["input_routed"] = bool(
                all(fresh_before.values()) and all(result["fresh_input_after"].values())
                and all(result["stage_settled"].values())
                and all(initial["titles"]) and initial["target"][:2] == [True, True]
                and all(small["titles"]) and all(small["target"])
                and all(restored["titles"]) and all(restored["target"])
                and not any(initial["cross_routed"] + small["cross_routed"] + restored["cross_routed"])
            )
            result["paste_visible"] = bool(
                paste_absent_before and paste_after
                and all(marker in panes(screen)[0] for marker in paste_markers))
            result["resize_observed"] = bool(
                (result["tmux_window_initial"], result["tmux_window_small"], result["tmux_window_restored"])
                == ("240x44", "210x38", "240x44")
                and initial["child_sizes"] == [[78, 40]] * 3
                and small["child_sizes"] == [[68, 34]] * 3
                and restored["child_sizes"] == [[78, 40]] * 3
                and initial["child_pids"] == small["child_pids"] == restored["child_pids"]
                and len(set(initial["child_pids"])) == 3
                and None not in initial["child_pids"]
            )
            result["child_pids"] = sorted(observed_pids)
            result["result"] = "observed" if all(result[key] for key in (
                "input_routed", "paste_visible", "resize_observed")) else "inconclusive_predicates"
            return result
        finally:
            if session is not None:
                alternate_before_quit = bool(
                    "screen" in locals() and screen._terminal_control["using_alternate"])
                session.write(QUIT)
                deadline = time.monotonic() + 2
                while session.poll() is None and time.monotonic() < deadline:
                    data = session.read_available()
                    if data and "stream" in locals():
                        stream.feed(data)
                    session.flush_writes()
                    time.sleep(0.02)
                if session.poll() is None:
                    session.write(b"\x02d")  # Detach only from this isolated tmux client.
                    pump(session, stream, 2, lambda: session.poll() is not None)
                if session.poll() is not None and "stream" in locals():
                    drain_status, post_exit_bytes = drain_exited_client(session, stream)
                else:
                    drain_status, post_exit_bytes = "client_running", 0
                result["post_exit_drain"] = drain_status
                result["post_exit_bytes"] = post_exit_bytes
                result["alternate_before_quit"] = alternate_before_quit
                if "screen" in locals():
                    result["local_vt_restored"] = bool(
                        alternate_before_quit and drain_status == "eof"
                        and not screen._terminal_control["using_alternate"]
                        and restore_sentinel in visible(screen))
                session.close()
                result["tmux_client_cleaned"] = session.poll() is not None
            if started:
                subprocess.run([tmux, "-S", str(socket), "kill-server"],
                               capture_output=True, timeout=3, check=False)
            deadline = time.monotonic() + 2
            checked = [pid for pid in (server_pid, app_pid, *observed_pids) if pid]
            while any(Path(f"/proc/{pid}").exists() for pid in checked) and time.monotonic() < deadline:
                time.sleep(0.02)
            result["processes_cleaned"] = all(not Path(f"/proc/{pid}").exists() for pid in checked)
            if result["processes_cleaned"]:
                socket.unlink(missing_ok=True)
            result["tmux_socket_removed"] = not socket.exists()
            if result.get("result") == "observed" and not all(result.get(key) for key in (
                "local_vt_restored", "tmux_client_cleaned", "processes_cleaned", "tmux_socket_removed")):
                result["result"] = "inconclusive_cleanup_or_restore"


if __name__ == "__main__":
    observation = run()
    print(json.dumps(observation, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if observation.get("result") == "observed" else 1)
