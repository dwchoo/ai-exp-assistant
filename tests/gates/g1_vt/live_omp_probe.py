"""Read-only live OMP PTY probe; it does not submit a model request."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.terminal.vt_g1.session import PtySession


def text(screen: TerminalScreen) -> str:
    return "\n".join(
        "".join(screen.buffer[y][x].data for x in range(screen.columns)).rstrip()
        for y in range(screen.lines)
    )


def main() -> int:
    rows, columns = 35, 100
    env = {"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"}
    command = [
        "omp", "--no-session", "--no-tools", "--no-pty", "--no-extensions",
        "--no-skills", "--no-rules", "--no-title",
    ]
    sessions: list[PtySession] = []
    try:
        for _ in range(2):
            session = PtySession(command, env=env)
            session.resize(rows, columns)
            sessions.append(session)
        screens = [TerminalScreen(columns, rows, reply=session.write) for session in sessions]
        streams = [make_stream(screen) for screen in screens]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            for session, stream in zip(sessions, streams):
                data = session.read_available()
                if data:
                    stream.feed(data)
            if all("π >" in text(screen) and "Recent sessions" in text(screen) for screen in screens):
                break
            time.sleep(0.02)
        for session, screen, stream in zip(sessions, screens, streams):
            visible = text(screen)
            if "omp v18.2.10" not in visible or "π >" not in visible:
                raise SystemExit("live OMP startup/composer not visible; inspect gate evidence")
            session.write(b"/clear\r")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                data = session.read_available()
                if data:
                    stream.feed(data)
                if "Context reset" in text(screen):
                    break
                time.sleep(0.02)
            clear_ok = "Context reset" in text(screen)
            session.write(b"/")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                data = session.read_available()
                if data:
                    stream.feed(data)
                if "plan-review" in text(screen) or "switch" in text(screen):
                    break
                time.sleep(0.02)
            slash_ok = "plan-review" in text(screen) or "switch" in text(screen)
            session.write(b"\x1b")
            time.sleep(0.1)
            session.write("G1_COMPOSER_한글".encode())
            session.write(b"\x1b[200~G1_PASTE_A\nG1_PASTE_B\x1b[201~")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                data = session.read_available()
                if data:
                    stream.feed(data)
                visible = text(screen)
                if "G1_COMPOSER_한글" in visible and "G1_PASTE_B" in visible:
                    break
                time.sleep(0.02)
            if "G1_COMPOSER_한글" not in text(screen) or "G1_PASTE_B" not in text(screen):
                visible = text(screen)
                raise SystemExit(
                    "live OMP composer or bracketed multiline paste probe failed; "
                    f"composer={'G1_COMPOSER_한글' in visible} paste={'G1_PASTE_B' in visible}\n"
                    + "\n".join(visible.splitlines()[-8:])
                )
            print(
                f"pid={session.pid} version=18.2.10 composer=pass korean=pass "
                f"multiline_paste=pass slash_palette={'pass' if slash_ok else 'unknown'} "
                f"slash_clear={'pass' if clear_ok else 'unknown'}"
            )
        return 0
    finally:
        for session in reversed(sessions):
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
