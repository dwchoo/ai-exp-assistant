"""Bounded G1 observations against a real OMP TUI and a local scripted provider.

Only boolean screen observations and byte counts leave the disposable PTY.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.terminal.vt_g1.session import PtySession


MARKER = "G1_SCRIPTED_TOOL_OUTPUT_42"
RESULT_NAME = "g1-tool-result.txt"
# A bounded OMP 18.2.10 no-resize STOP/CONT control also redrew this anchor.
NO_RESIZE_CONTROL_REDRAW_ANCHOR_SEEN = True


class ChildTtyState(NamedTuple):
    pending_input_bytes: int
    canonical: bool
    size: tuple[int, int]


def tool_output_confirmed(screen_text: str, result_file: Path) -> bool:
    """Require a real shell side effect as well as rendered output."""
    return result_file.is_file() and result_file.read_text() == MARKER + "\n" and MARKER in screen_text


def approval_allowed(approval_visible: bool, result_file: Path) -> bool:
    return approval_visible and not result_file.exists()


def approval_surface_present_after_resize(
    before: str, after: str, *, pre_resize_quiet: bool,
    redraw_anchor_seen: bool, screen_size: tuple[int, int], child_size: tuple[int, int] | None,
) -> bool:
    """Record the visible modal and geometry; this does not prove resize caused redraw."""
    anchor = RESULT_NAME
    permission = ("approve", "allow", "permission")
    return (
        anchor in before and anchor in after
        and any(word in before.lower() for word in permission)
        and any(word in after.lower() for word in permission)
        and pre_resize_quiet and redraw_anchor_seen
        and screen_size == (84, 24) and child_size == (84, 24)
    )


def normal_resize_distinguished(post_continue_anchor: bool, no_resize_control_anchor: bool) -> bool:
    """A redraw shared with STOP/CONT control cannot establish resize restoration."""
    return post_continue_anchor and not no_resize_control_anchor


def approval_tool_scenario_passed(result: dict[str, object]) -> bool:
    """Limit CLI success to the live approval, tool, and provider roundtrip."""
    return all(result.get(key) for key in (
        "composer_ready", "approval_visible", "artifact_absent_on_approval_ui",
        "artifact_absent_before_approval", "artifact_present_after_approval",
        "tool_output_visible", "provider_followup_observed", "final_visible",
    ))


def child_tty_state(pid: int) -> ChildTtyState | None:
    """Inspect the isolated child's slave TTY without consuming input."""
    try:
        slave = os.readlink(f"/proc/{pid}/fd/0")
        fd = os.open(slave, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        try:
            pending = struct.unpack("I", fcntl.ioctl(fd, termios.FIONREAD, struct.pack("I", 0)))[0]
            canonical = bool(termios.tcgetattr(fd)[3] & termios.ICANON)
            rows, columns, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
            return ChildTtyState(pending, canonical, (columns, rows))
        finally:
            os.close(fd)
    except OSError:
        return None


def query_reply_tty_drained(
    reply_count: int, accepted: int, generated: int, queued: int, state: ChildTtyState | None,
) -> bool:
    """Limit the TTY drain claim to a noncanonical slave input queue."""
    return bool(
        reply_count > 0 and accepted == generated and queued == 0
        and state is not None and not state.canonical and state.pending_input_bytes == 0
    )


def child_stopped(pid: int) -> bool:
    try:
        return any(
            line.split()[1] in {"T", "t"}
            for line in Path(f"/proc/{pid}/status").read_text().splitlines()
            if line.startswith("State:")
        )
    except (OSError, IndexError):
        return False


def visible(screen: TerminalScreen) -> str:
    return "\n".join(
        "".join(screen.buffer[y][x].data for x in range(screen.columns)).rstrip()
        for y in range(screen.lines)
    )


def provider() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        requests = 0

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            type(self).requests += 1
            if self.requests == 1:
                encoded = "".join(f"\\{ord(char):03o}" for char in MARKER)
                command = f"printf '%b\\n' '{encoded}' > {RESULT_NAME} && cat {RESULT_NAME}"
                delta = {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0, "id": "g1-scripted-write", "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({"command": command})},
                    }],
                }
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "G1 scripted probe complete."}
                finish = "stop"
            frames = [
                {"id": "g1-probe", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "g1-probe", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            payload = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def pump(
    session: PtySession, stream: object, seconds: float, predicate: Callable[[], bool],
    observe: Callable[[bytes], None] | None = None,
) -> tuple[bool, int]:
    deadline = time.monotonic() + seconds
    count = 0
    while time.monotonic() < deadline:
        data = session.read_available()
        if data:
            count += len(data)
            if observe is not None:
                observe(data)
            stream.feed(data)
        session.flush_writes()
        if predicate():
            return True, count
        if session.poll() is not None:
            break
        time.sleep(0.01)
    return bool(predicate()), count


def drain_until_quiet(session: PtySession, stream: object, *, timeout: float = 3, quiet: float = 0.2) -> tuple[bool, int]:
    """Establish an empty PTY output interval before changing child geometry."""
    deadline = time.monotonic() + timeout
    quiet_since = time.monotonic()
    count = 0
    while time.monotonic() < deadline:
        data = session.read_available()
        if data:
            count += len(data)
            stream.feed(data)
            quiet_since = time.monotonic()
        session.flush_writes()
        if session.poll() is not None:
            return False, count
        if session.pending_write_bytes == 0 and time.monotonic() - quiet_since >= quiet:
            final_data = session.read_available()
            if final_data:
                count += len(final_data)
                stream.feed(final_data)
                quiet_since = time.monotonic()
            else:
                return True, count
        time.sleep(0.01)
    return False, count


def run() -> dict[str, object]:
    omp = shutil.which("omp")
    version = subprocess.run([omp, "--version"], text=True, capture_output=True, timeout=10).stdout.strip() if omp else "missing"
    if version != "omp/18.2.10":
        return {
            "result": "inconclusive_version", "omp_version": version,
            "scenario": "approval_tool_roundtrip", "scenario_passed": False,
            "gate_status": "pending",
        }
    with tempfile.TemporaryDirectory(prefix="cw02-g1-runtime-") as temporary:
        root = Path(temporary)
        cwd = root / "cwd"
        cwd.mkdir()
        sessions = root / "sessions"
        sessions.mkdir()
        profile = root / "agent"
        profile.mkdir()
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        server = provider()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
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
        env = {
            "TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8",
            "PI_CODING_AGENT_DIR": str(profile),
        }
        session: PtySession | None = None
        result_file = cwd / RESULT_NAME
        try:
            session = PtySession(command, env=env)
            session.resize(32, 100)
            reply_count = 0
            reply_bytes_generated = 0
            reply_bytes_accepted = 0

            def reply(data: bytes) -> None:
                nonlocal reply_count, reply_bytes_generated, reply_bytes_accepted
                reply_count += 1
                reply_bytes_generated += len(data)
                reply_bytes_accepted += session.write(data)

            screen = TerminalScreen(100, 32, reply=reply)
            stream = make_stream(screen)
            composer_ready, startup_bytes = pump(session, stream, 8, lambda: "π >" in visible(screen))
            startup_tty = child_tty_state(session.pid)
            startup_query_consumed = query_reply_tty_drained(
                reply_count, reply_bytes_accepted, reply_bytes_generated,
                session.pending_write_bytes, startup_tty,
            )
            clear_ok = False
            if composer_ready:
                session.write(b"/clear\r")
                clear_ok, extra_bytes = pump(session, stream, 3, lambda: "Context reset" in visible(screen))
                startup_bytes += extra_bytes
            input_visible = False
            input_bytes = 0
            if clear_ok:
                session.write(b"Run the local scripted G1 tool probe.")
                input_visible, input_bytes = pump(
                    session, stream, 3,
                    lambda: "Run the local scripted G1 tool probe." in visible(screen),
                )
                if input_visible:
                    session.write(b"\r")
            requested, n1 = pump(session, stream, 12, lambda: server.RequestHandlerClass.requests > 0)
            if not requested:
                after_submit = visible(screen).lower()
                hints = (
                    "recent sessions", "context reset", "select", "trust", "continue",
                    "press enter", "update", "model", "provider", "api key", "permission",
                )
                return {
                    "omp_version": version, "composer_ready": composer_ready,
                    "clear_ack": clear_ok,
                    "input_visible_before_submit": input_visible,
                    "input_still_visible_after_submit": "run the local scripted g1 tool probe." in after_submit,
                    "ui_error_visible": any(token in after_submit for token in ("error", "failed", "invalid", "unknown model")),
                    "ui_hints": [token for token in hints if token in after_submit],
                    "child_exit_before_cleanup": session.poll(),
                    "provider_requests": server.RequestHandlerClass.requests,
                    "pty_bytes_drained": startup_bytes + input_bytes + n1,
                    "query_replies_total": reply_count,
                    "query_reply_bytes_accepted": reply_bytes_accepted,
                    "query_reply_queue_drained": session.pending_write_bytes == 0,
                    "query_reply_tty_pending_bytes": startup_tty.pending_input_bytes if startup_tty else None,
                    "query_reply_tty_canonical": startup_tty.canonical if startup_tty else None,
                    "query_reply_tty_input_drained": startup_query_consumed,
                    "query_reply_consumption_level": "tty_input_drained" if startup_query_consumed else "unconfirmed",
                    "query_reply_omp_interpretation_confirmed": False,
                    "result": "inconclusive_probe_no_request",
                    "scenario": "approval_tool_roundtrip",
                    "scenario_passed": False,
                    "gate_status": "pending",
                    "child_pid": session.pid,
                }
            before, n1_extra = pump(session, stream, 5, lambda: "approve" in visible(screen).lower())
            n1 += n1_extra
            stop_requested = False
            pre_resize_child_stopped = False
            pre_resize_quiet = False
            pre_resize_drained = 0
            resized_tty = None
            try:
                os.killpg(session.pid, signal.SIGSTOP)
                stop_requested = True
                pre_resize_child_stopped, stop_bytes = pump(
                    session, stream, 2, lambda: child_stopped(session.pid)
                )
                n1 += stop_bytes
                if pre_resize_child_stopped:
                    pre_resize_quiet, pre_resize_drained = drain_until_quiet(session, stream)
                n1 += pre_resize_drained
                approval_screen = visible(screen)
                approval_visible = before and any(
                    token in approval_screen.lower() for token in ("approve", "allow", "permission")
                )
                artifact_absent_on_approval_ui = not result_file.exists()
                query_before = reply_count
                # With the child stopped and the old PTY output drained, change both sizes.
                screen.resize(lines=24, columns=84)
                session.resize(24, 84)
                resized_tty = child_tty_state(session.pid)
            finally:
                if stop_requested:
                    try:
                        os.killpg(session.pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
            redraw_anchor_seen = False
            redraw_tail = b""
            anchor = RESULT_NAME.encode()

            def observe_resize(data: bytes) -> None:
                nonlocal redraw_anchor_seen, redraw_tail
                redraw_anchor_seen |= anchor in redraw_tail + data
                redraw_tail = data[-(len(anchor) - 1):]

            _, n2 = pump(session, stream, 3, lambda: False, observe_resize)
            approval_surface_after_resize = approval_surface_present_after_resize(
                approval_screen, visible(screen),
                pre_resize_quiet=pre_resize_child_stopped and pre_resize_quiet,
                redraw_anchor_seen=redraw_anchor_seen,
                screen_size=(screen.columns, screen.lines),
                child_size=resized_tty.size if resized_tty else None,
            )
            normal_resize_confirmed = normal_resize_distinguished(
                redraw_anchor_seen, NO_RESIZE_CONTROL_REDRAW_ANCHOR_SEEN,
            )
            artifact_absent_before_approval = not result_file.exists()
            if approval_allowed(approval_visible, result_file):
                session.write(b"\r")
            output_ok, n3 = pump(
                session, stream, 12,
                lambda: tool_output_confirmed(visible(screen), result_file),
            )
            final_ok, n4 = pump(session, stream, 12, lambda: "G1 scripted probe complete." in visible(screen))
            final_tty = child_tty_state(session.pid)
            query_consumed = startup_query_consumed and query_reply_tty_drained(
                reply_count, reply_bytes_accepted, reply_bytes_generated,
                session.pending_write_bytes, final_tty,
            )
            observations = {
                "omp_version": version,
                "composer_ready": composer_ready,
                "provider_requests": server.RequestHandlerClass.requests,
                "approval_visible": approval_visible,
                "artifact_absent_on_approval_ui": artifact_absent_on_approval_ui,
                "artifact_absent_before_approval": artifact_absent_before_approval,
                "artifact_present_after_approval": result_file.is_file(),
                "approval_surface_present_after_resize": approval_surface_after_resize,
                "normal_resize_confirmed": normal_resize_confirmed,
                "normal_resize_result": "inconclusive_no_resize_control_redrew",
                "pre_resize_child_stopped": pre_resize_child_stopped,
                "pre_resize_output_quiet": pre_resize_quiet,
                "pre_resize_pty_bytes_drained": pre_resize_drained,
                "post_continue_pty_bytes_drained": n2,
                "post_continue_redraw_anchor_seen": redraw_anchor_seen,
                "no_resize_control_redraw_anchor_seen": NO_RESIZE_CONTROL_REDRAW_ANCHOR_SEEN,
                "resize_child_columns": resized_tty.size[0] if resized_tty else None,
                "resize_child_rows": resized_tty.size[1] if resized_tty else None,
                "query_replies_before_resize": query_before,
                "query_replies_total": reply_count,
                "query_reply_bytes_accepted": reply_bytes_accepted,
                "query_reply_queue_drained": session.pending_write_bytes == 0,
                "query_reply_tty_pending_bytes": final_tty.pending_input_bytes if final_tty else None,
                "query_reply_tty_canonical": final_tty.canonical if final_tty else None,
                "query_reply_tty_input_drained": query_consumed,
                "query_reply_consumption_level": "tty_input_drained" if query_consumed else "unconfirmed",
                "query_reply_omp_interpretation_confirmed": False,
                "tool_output_visible": output_ok,
                "final_visible": final_ok,
                "provider_followup_observed": server.RequestHandlerClass.requests >= 2,
                "pty_bytes_drained": startup_bytes + input_bytes + n1 + n2 + n3 + n4,
                "child_pid": session.pid,
                "child_exit_before_cleanup": session.poll(),
            }
            observations["scenario"] = "approval_tool_roundtrip"
            observations["scenario_passed"] = approval_tool_scenario_passed(observations)
            observations["result"] = "approval_tool_scenario_passed" if observations["scenario_passed"] else "approval_tool_scenario_failed"
            observations["gate_status"] = "pending"
            return observations
        finally:
            if session is not None:
                session.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result.get("scenario_passed") else 1)
