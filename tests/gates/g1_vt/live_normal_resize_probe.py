"""Bounded, live OMP resize experiment with two negative controls."""

from __future__ import annotations

import json
import io
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from live_runtime_probe import (
    RESULT_NAME, approval_allowed, child_tty_state, drain_until_quiet,
    provider, pump, tool_output_confirmed, visible,
)
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.terminal.vt_g1.session import PtySession


DRAFT = "G1_한글_초안_계속"


def native_approval_visible(lines: list[str], marker: str) -> bool:
    """Recognize the observed OMP 18.2.10 bash approval modal, not transcript text."""
    for title_row, line in enumerate(lines):
        if not line.lstrip().startswith("╭─ Allow tool: bash"):
            continue
        for approve_row in range(title_row + 2, min(title_row + 9, len(lines) - 1)):
            if not lines[approve_row].lstrip().startswith("│  ❯ Approve"):
                continue
            if not lines[approve_row + 1].lstrip().startswith("│    Deny"):
                continue
            if any(marker in command and command.lstrip().startswith("│")
                   for command in lines[title_row + 1:approve_row]):
                return True
    return False


def request_contains_full_draft(body: bytes) -> bool:
    try:
        messages = json.loads(body).get("messages", [])
        user_text = " ".join(
            str(message.get("content", "")) for message in messages
            if message.get("role") == "user"
        )
        return DRAFT + "_INPUT_OK" in user_text
    except (ValueError, TypeError, AttributeError):
        return False


def observing_provider():
    """Record only whether the first user request contains the complete draft."""
    server = provider()
    base_handler = server.RequestHandlerClass

    class Handler(base_handler):
        full_draft_in_request = False

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/v1/chat/completions" and self.requests == 0:
                type(self).full_draft_in_request = request_contains_full_draft(body)
            self.rfile = io.BytesIO(body)
            super().do_POST()

    server.RequestHandlerClass = Handler
    return server


def pump_surface(session: PtySession, stream: object, screen: TerminalScreen,
                 marker: str, surface: str) -> tuple[bool, int]:
    """Render only post-resize bytes on a blank screen to check fresh placement."""
    anchor = marker.encode("utf-8")
    tail = b""
    seen = False
    fresh_screen = TerminalScreen(screen.columns, screen.lines)
    fresh_stream = make_stream(fresh_screen)

    def observe(data: bytes) -> None:
        nonlocal tail, seen
        seen |= anchor in tail + data
        tail = data[-(len(anchor) - 1):]
        fresh_stream.feed(data)

    _, count = pump(session, stream, 2, lambda: False, observe)
    fresh = state(fresh_screen, session, marker)
    placed = fresh["marker_in_composer"] if surface == "composer" else fresh["approval_visible"]
    return bool(seen and placed), count


def state(screen: TerminalScreen, session: PtySession, marker: str) -> dict[str, object]:
    lines = visible(screen).splitlines()
    tty = child_tty_state(session.pid)
    composer_rows = [i for i, line in enumerate(lines) if "π >" in line]
    marker_rows = [i for i, line in enumerate(lines) if marker in line]
    composer_visible = bool(composer_rows)
    return {
        "screen_size": [screen.columns, screen.lines],
        "child_size": list(tty.size) if tty else None,
        "marker_visible": bool(marker_rows),
        "marker_rows": marker_rows,
        "composer_rows": composer_rows,
        "composer_visible": composer_visible,
        # OMP 18.2.10 renders the input line immediately below its π > label.
        "marker_in_composer": any(row == prompt + 1 for row in marker_rows for prompt in composer_rows),
        "approval_visible": not composer_visible and native_approval_visible(lines, marker),
    }


def resize_surface_passed(small: dict[str, object], restored: dict[str, object],
                          *, surface: str) -> bool:
    return all(
        item["marker_visible"] and item[f"{surface}_visible"]
        and item["screen_size"] == size and item["child_size"] == size
        and item.get("surface_redraw_seen")
        and (surface != "composer" or item["marker_in_composer"])
        for item, size in ((small, [84, 24]), (restored, [100, 32]))
    ) and small["marker_rows"] != restored["marker_rows"]


def run_case(mode: str, omp: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="cw02-g1-normal-resize-") as temporary:
        root = Path(temporary)
        cwd, sessions, profile = (root / name for name in ("cwd", "sessions", "agent"))
        for directory in (cwd, sessions, profile):
            directory.mkdir()
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        server = observing_provider()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (profile / "models.yml").write_text(
            "providers:\n  g1-probe:\n"
            f"    baseUrl: http://127.0.0.1:{server.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: Scripted G1 probe\n"
            "        contextWindow: 32768\n        maxTokens: 1024\n"
        )
        command = [
            omp, "--model", "g1-probe/scripted", "--no-session", "--no-pty",
            "--no-extensions", "--no-skills", "--no-rules", "--no-title",
            "--tools=bash", "--approval-mode=always-ask", "--max-time=30",
            "--cwd", str(cwd), "--session-dir", str(sessions), "--config", str(config),
        ]
        env = {"TERM": "xterm-256color", "COLORTERM": "truecolor",
               "LANG": "en_US.UTF-8", "PI_CODING_AGENT_DIR": str(profile)}
        session = None
        result: dict[str, object] = {"mode": mode}
        try:
            session = PtySession(command, env=env)
            session.resize(32, 100)
            screen = TerminalScreen(100, 32, reply=session.write)
            stream = make_stream(screen)
            result["child_pid"] = session.pid
            ready, count = pump(session, stream, 8, lambda: "π >" in visible(screen))
            result["startup_ready"] = ready
            if not ready:
                return result
            session.write(b"/clear\r")
            clear, drained = pump(session, stream, 3, lambda: "Context reset" in visible(screen))
            count += drained
            result["clear_ack"] = clear
            if not clear:
                return result
            session.write(DRAFT.encode())
            typed, drained = pump(session, stream, 3, lambda: DRAFT in visible(screen))
            count += drained
            result["draft_typed"] = typed
            result["draft_before"] = state(screen, session, DRAFT)
            quiet, drained = drain_until_quiet(session, stream)
            count += drained
            result["draft_pre_quiet"] = quiet
            if mode != "no_resize":
                screen.resize(lines=24, columns=84)
                if mode == "normal":
                    session.resize(24, 84)
            redraw_seen, drained = pump_surface(session, stream, screen, DRAFT, "composer")
            count += drained
            result["draft_small"] = state(screen, session, DRAFT)
            result["draft_small"]["surface_redraw_seen"] = redraw_seen
            if mode != "no_resize":
                screen.resize(lines=32, columns=100)
                if mode == "normal":
                    session.resize(32, 100)
            redraw_seen, drained = pump_surface(session, stream, screen, DRAFT, "composer")
            count += drained
            result["draft_restored"] = state(screen, session, DRAFT)
            result["draft_restored"]["surface_redraw_seen"] = redraw_seen
            result["draft_resize_passed"] = resize_surface_passed(
                result["draft_small"], result["draft_restored"], surface="composer"
            )
            # Add more input after restoration to prove that focus still targets composer.
            session.write(b"_INPUT_OK")
            input_ok, drained = pump(session, stream, 3, lambda: "INPUT_OK" in visible(screen))
            count += drained
            result["input_after_resize"] = input_ok
            session.write(b"\r")
            requested, drained = pump(session, stream, 12, lambda: server.RequestHandlerClass.requests > 0)
            count += drained
            result["provider_requested"] = requested
            result["provider_received_full_draft"] = server.RequestHandlerClass.full_draft_in_request
            if not requested:
                return result
            approval, drained = pump(
                session, stream, 5,
                lambda: native_approval_visible(visible(screen).splitlines(), RESULT_NAME),
            )
            count += drained
            result["approval_visible"] = approval
            result_file = cwd / RESULT_NAME
            result["artifact_absent_before_approval"] = not result_file.exists()
            quiet, drained = drain_until_quiet(session, stream)
            count += drained
            result["approval_pre_quiet"] = quiet
            result["approval_before"] = state(screen, session, RESULT_NAME)
            if mode != "no_resize":
                screen.resize(lines=24, columns=84)
                if mode == "normal":
                    session.resize(24, 84)
            redraw_seen, drained = pump_surface(session, stream, screen, RESULT_NAME, "approval")
            count += drained
            result["approval_small"] = state(screen, session, RESULT_NAME)
            result["approval_small"]["surface_redraw_seen"] = redraw_seen
            if mode != "no_resize":
                screen.resize(lines=32, columns=100)
                if mode == "normal":
                    session.resize(32, 100)
            redraw_seen, drained = pump_surface(session, stream, screen, RESULT_NAME, "approval")
            count += drained
            result["approval_restored"] = state(screen, session, RESULT_NAME)
            result["approval_restored"]["surface_redraw_seen"] = redraw_seen
            result["artifact_absent_after_resize"] = not result_file.exists()
            result["approval_resize_passed"] = resize_surface_passed(
                result["approval_small"], result["approval_restored"], surface="approval"
            )
            if approval_allowed(approval, result_file):
                session.write(b"\r")
            output, drained = pump(session, stream, 12, lambda: tool_output_confirmed(visible(screen), result_file))
            count += drained
            final, drained = pump(session, stream, 12, lambda: "G1 scripted probe complete." in visible(screen))
            count += drained
            result.update(tool_output_visible=output, final_visible=final,
                          provider_followup=server.RequestHandlerClass.requests >= 2,
                          pty_bytes_drained=count)
            return result
        finally:
            if session is not None:
                session.close()
                result["child_cleaned"] = session.poll() is not None
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def run() -> dict[str, object]:
    omp = shutil.which("omp")
    version = subprocess.run([omp, "--version"], text=True, capture_output=True, timeout=10).stdout.strip() if omp else "missing"
    if version != "omp/18.2.10":
        return {"omp_version": version, "result": "inconclusive_version"}
    cases = [run_case(mode, omp) for mode in ("no_resize", "screen_only", "normal")]
    normal = cases[-1]
    completed = all(
        all(case.get(key) for key in (
            "startup_ready", "clear_ack", "draft_typed", "input_after_resize",
            "provider_requested", "approval_visible", "artifact_absent_before_approval",
            "artifact_absent_after_resize", "tool_output_visible", "final_visible",
            "provider_followup", "provider_received_full_draft", "child_cleaned",
        ))
        for case in cases
    )
    passed = bool(
        completed and normal.get("draft_resize_passed") and normal.get("approval_resize_passed")
        and normal.get("input_after_resize") and normal.get("artifact_absent_after_resize")
        and normal.get("tool_output_visible") and normal.get("final_visible")
        and normal.get("provider_followup")
        and not cases[0].get("draft_resize_passed")
        and not cases[1].get("draft_resize_passed")
        and not cases[0].get("approval_resize_passed")
        and not cases[1].get("approval_resize_passed")
        and all(case.get("child_cleaned") for case in cases)
    )
    return {"omp_version": version, "cases": cases, "normal_resize_confirmed": passed,
            "gate_status": "pending"}


if __name__ == "__main__":
    observations = run()
    print(json.dumps(observations, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if observations.get("normal_resize_confirmed") else 1)
