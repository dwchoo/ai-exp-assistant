"""Bounded G1 observations against a real OMP TUI and a local scripted provider.

Only boolean screen observations and byte counts leave the disposable PTY.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.terminal.vt_g1.session import PtySession


MARKER = "G1_SCRIPTED_TOOL_OUTPUT_42"


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
                delta = {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0, "id": "g1-scripted-write", "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({"command": f"printf '{MARKER}\\n'"})},
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


def pump(session: PtySession, stream: object, seconds: float, predicate: object) -> tuple[bool, int]:
    deadline = time.monotonic() + seconds
    count = 0
    while time.monotonic() < deadline:
        data = session.read_available()
        if data:
            count += len(data)
            stream.feed(data)
        session.flush_writes()
        if predicate():
            return True, count
        if session.poll() is not None:
            break
        time.sleep(0.01)
    return bool(predicate()), count


def run() -> dict[str, object]:
    omp = shutil.which("omp")
    version = subprocess.run([omp, "--version"], text=True, capture_output=True, timeout=10).stdout.strip() if omp else "missing"
    if version != "omp/18.2.10":
        return {"result": "inconclusive", "omp_version": version}
    with tempfile.TemporaryDirectory(prefix="cw02-g1-runtime-") as temporary:
        root = Path(temporary)
        cwd = root / "cwd"
        cwd.mkdir()
        sessions = root / "sessions"
        sessions.mkdir()
        profile = root / "agent"
        profile.mkdir()
        config = root / "config.yml"
        config.write_text("# G1 probe overlay\n")
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
        try:
            session = PtySession(command, env=env)
            session.resize(32, 100)
            replies: list[bytes] = []

            def reply(data: bytes) -> None:
                replies.append(data)
                session.write(data)

            screen = TerminalScreen(100, 32, reply=reply)
            stream = make_stream(screen)
            composer_ready, startup_bytes = pump(session, stream, 8, lambda: "π >" in visible(screen))
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
                    "query_replies_total": len(replies), "result": "inconclusive_probe_no_request",
                    "child_pid": session.pid,
                }
            before, n1_extra = pump(session, stream, 5, lambda: "approve" in visible(screen).lower())
            n1 += n1_extra
            approval_screen = visible(screen)
            approval_visible = before and any(
                token in approval_screen.lower() for token in ("approve", "allow", "permission")
            )
            query_before = len(replies)
            # The same live TUI survives a child PTY and VT-screen size change.
            screen.resize(lines=24, columns=84)
            session.resize(24, 84)
            resize_ok, n2 = pump(session, stream, 2, lambda: "π >" in visible(screen))
            if approval_visible:
                session.write(b"y")
            output_ok, n3 = pump(session, stream, 12, lambda: MARKER in visible(screen))
            final_ok, n4 = pump(session, stream, 3, lambda: "G1 scripted probe complete." in visible(screen))
            return {
                "omp_version": version,
                "composer_ready": composer_ready,
                "provider_requests": server.RequestHandlerClass.requests,
                "approval_visible": approval_visible,
                "resize_screen_restored": resize_ok,
                "query_replies_before_resize": query_before,
                "query_replies_total": len(replies),
                "tool_output_visible": output_ok,
                "final_visible": final_ok,
                "pty_bytes_drained": n1 + n2 + n3 + n4,
                "child_pid": session.pid,
                "child_exit_before_cleanup": session.poll(),
            }
        finally:
            if session is not None:
                session.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result.get("approval_visible") and result.get("tool_output_visible") and result.get("resize_screen_restored") else 1)
