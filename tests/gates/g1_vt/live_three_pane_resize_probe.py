"""Live outer-PTY resize and focus routing through two OMP panes and a host shell."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from live_runtime_probe import child_tty_state, pump, visible
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.terminal.vt_g1.session import PtySession


APP = Path(__file__).resolve().parents[3] / "src/workbench/ui/terminal_g1/app.py"
FOCUS = b"\x1b[17~"
QUIT = b"\x1b[21~"


def panes(screen: TerminalScreen) -> list[str]:
    width = screen.columns // 3
    lines = visible(screen).splitlines()
    return ["\n".join(line[start:end] for line in lines)
            for start, end in ((0, width), (width, width * 2), (width * 2, screen.columns))]


def observe(screen: TerminalScreen, expected: list[str]) -> dict[str, object]:
    texts = panes(screen)
    pids = [re.search(r"pid=(\d+)", text) for text in texts]
    return {
        "outer_size": [screen.columns, screen.lines],
        "pane_title_visible": [title in text for title, text in zip(
            ("MANAGER OMP", "WORKER OMP", "HOST SHELL"), texts)],
        "marker_in_target": [marker in text for marker, text in zip(expected, texts)],
        "marker_in_other_panes": [any(marker in other for j, other in enumerate(texts) if j != i)
                                  for i, marker in enumerate(expected)],
        "child_sizes": [list(state.size) if (match and (state := child_tty_state(int(match[1]))))
                        else None for match in pids],
        "child_pids": [int(match[1]) if match else None for match in pids],
    }


def stable_child_pids(stages: list[dict[str, object]]) -> bool:
    first = stages[0]["child_pids"]
    return bool(
        len(first) == 3 and all(isinstance(pid, int) and pid > 0 for pid in first)
        and len(set(first)) == 3
        and all(stage["child_pids"] == first for stage in stages[1:])
    )


def run() -> dict[str, object]:
    omp = shutil.which("omp")
    version = subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=10).stdout.strip() if omp else "missing"
    if version != "omp/18.2.10":
        return {"omp_version": version, "result": "inconclusive_version"}
    with tempfile.TemporaryDirectory(prefix="cw02-g1-three-pane-") as temporary:
        root = Path(temporary)
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        commands = []
        for name in ("manager", "worker"):
            cwd, sessions = root / f"{name}-cwd", root / f"{name}-sessions"
            cwd.mkdir()
            sessions.mkdir()
            commands.append(shlex.join([
                omp, "--no-session", "--no-tools", "--no-pty", "--no-extensions",
                "--no-skills", "--no-rules", "--no-title", "--cwd", str(cwd),
                "--session-dir", str(sessions), "--config", str(config),
            ]))
        command = [sys.executable, str(APP), "--manager-cmd", commands[0],
                   "--worker-cmd", commands[1]]
        env = {"TERM": "xterm-256color", "COLORTERM": "truecolor",
               "LANG": "en_US.UTF-8", "PI_CODING_AGENT_DIR": str(profile)}
        session = None
        result: dict[str, object] = {"omp_version": version}
        observed_pids: set[int] = set()

        def snapshot(screen: TerminalScreen, expected: list[str]) -> dict[str, object]:
            observation = observe(screen, expected)
            observed_pids.update(pid for pid in observation["child_pids"] if pid is not None)
            return observation

        try:
            session = PtySession(command, env=env)
            session.resize(45, 240)
            screen = TerminalScreen(240, 45, reply=session.write)
            stream = make_stream(screen)
            ready, count = pump(session, stream, 12, lambda: all(
                label in text for label, text in zip(
                    ("π >", "π >", "bash-"), panes(screen))))
            result["startup_ready"] = ready
            if not ready:
                result["initial"] = snapshot(screen, ["MGR_한글", "WRK_한글", "HOST_G1"])
                return result
            session.write("MGR_한글".encode())
            _, drained = pump(session, stream, 3, lambda: "MGR_한글" in panes(screen)[0])
            count += drained
            session.write(FOCUS + "WRK_한글".encode())
            _, drained = pump(session, stream, 3, lambda: "WRK_한글" in panes(screen)[1])
            count += drained
            result["initial"] = snapshot(screen, ["MGR_한글", "WRK_한글", "HOST_G1"])
            screen.resize(lines=39, columns=210)
            session.resize(39, 210)
            _, drained = pump(session, stream, 3, lambda: False)
            count += drained
            result["small"] = snapshot(screen, ["MGR_한글", "WRK_한글", "HOST_G1"])
            session.write(b"_W")
            _, drained = pump(session, stream, 3, lambda: "WRK_한글_W" in panes(screen)[1])
            count += drained
            session.write(FOCUS + b"printf 'HOST_%s\\n' G1\r")
            _, drained = pump(session, stream, 3, lambda: "HOST_G1" in panes(screen)[2])
            count += drained
            result["small_routed"] = snapshot(screen, ["MGR_한글", "WRK_한글_W", "HOST_G1"])
            screen.resize(lines=45, columns=240)
            session.resize(45, 240)
            _, drained = pump(session, stream, 3, lambda: False)
            count += drained
            session.write(FOCUS + b"_M")
            _, drained = pump(session, stream, 3, lambda: "MGR_한글_M" in panes(screen)[0])
            count += drained
            result["restored_routed"] = snapshot(screen, ["MGR_한글_M", "WRK_한글_W", "HOST_G1"])
            result["pty_bytes_drained"] = count
            expected_sizes = ([78, 41], [68, 35], [78, 41])
            stages = [result[key] for key in ("small_routed", "restored_routed")]
            result["three_pane_resize_confirmed"] = bool(
                stable_child_pids([result[key] for key in (
                    "initial", "small", "small_routed", "restored_routed")])
                and result["initial"]["outer_size"] == [240, 45]
                and result["initial"]["child_sizes"] == [expected_sizes[0]] * 3
                and all(result["initial"]["pane_title_visible"])
                and result["initial"]["marker_in_target"][:2] == [True, True]
                and result["initial"]["marker_in_target"][2] is False
                and not any(result["initial"]["marker_in_other_panes"])
                and all(result["small"]["pane_title_visible"])
                and result["small"]["outer_size"] == [210, 39]
                and result["small"]["child_sizes"] == [[68, 35]] * 3
                and not any(result["small"]["marker_in_other_panes"])
                and all(
                all(stage["pane_title_visible"])
                and stage["outer_size"] == outer_size
                and all(stage["marker_in_target"])
                and not any(stage["marker_in_other_panes"])
                and stage["child_sizes"] == [size] * 3
                for stage, size, outer_size in zip(
                    stages, expected_sizes[1:], ([210, 39], [240, 45])
                )
                )
            )
            return result
        finally:
            if session is not None:
                session.write(QUIT)
                deadline = time.monotonic() + 2
                while session.poll() is None and time.monotonic() < deadline:
                    session.read_available()
                    time.sleep(0.02)
                session.close()
                result["outer_cleaned"] = session.poll() is not None
            result["observed_child_count"] = len(observed_pids)
            result["children_cleaned"] = bool(observed_pids) and all(
                not Path(f"/proc/{pid}").exists() for pid in observed_pids
            )
            result["three_pane_resize_confirmed"] = bool(
                result.get("three_pane_resize_confirmed")
                and result.get("outer_cleaned") and result["children_cleaned"]
            )


if __name__ == "__main__":
    observations = run()
    print(json.dumps(observations, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if observations.get("three_pane_resize_confirmed") else 1)
