"""Compare real OMP bytes with their actual Workbench outer-PTY rendering.

The replay boundary makes text, theme, geometry and input bytes identical; only
the renderer differs. Neither a static fixture nor parser-only success suffices.
Raw output is disposable and is never included in the result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from live_runtime_probe import pump, visible, drain_until_quiet
from live_three_pane_resize_probe import APP, QUIT, panes
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.terminal.vt_g1.session import PtySession


def is_rgb(value: str) -> bool:
    return len(value) == 6 and all(char in "0123456789abcdef" for char in value.lower())


def compare(source: TerminalScreen, rendered: TerminalScreen) -> dict[str, object]:
    checked = []
    for y in range(source.lines):
        for x in range(source.columns):
            cell = source.buffer[y][x]
            if cell.data.strip() and (is_rgb(cell.fg) or is_rgb(cell.bg)):
                outer = rendered.buffer[y + 2][x + 1]
                checked.append((cell, outer))
    colors = sorted({cell.fg for cell, _ in checked if is_rgb(cell.fg)})
    rgb_text_cells = len(checked)
    styles = {}
    for marker in ("G1_COLOR_NORMAL", "G1_COLOR_BOLD", "G1_COLOR_ITALIC"):
        for y in range(source.lines):
            for x in range(source.columns - len(marker) + 1):
                if "".join(source.buffer[y][x + offset].data for offset in range(len(marker))) == marker:
                    cell = source.buffer[y][x]
                    styles[marker] = {key: getattr(cell, key) for key in
                                      ("fg", "bg", "bold", "italics", "underscore")}
                    checked.extend((source.buffer[y][x + offset], rendered.buffer[y + 2][x + 1 + offset])
                                   for offset in range(len(marker)))
    emphasis = (len(styles) == 3
                and styles["G1_COLOR_NORMAL"] != styles["G1_COLOR_BOLD"])
    mismatch = sum(any(getattr(cell, key) != getattr(outer, key) for key in
                       ("data", "fg", "bg", "bold", "italics", "underscore", "reverse"))
                   for cell, outer in checked)
    mismatch_keys = {key: sum(getattr(cell, key) != getattr(outer, key) for cell, outer in checked)
                     for key in ("data", "fg", "bg", "bold", "italics", "underscore", "reverse")}
    mismatch_samples = [{"source": {key: getattr(cell, key) for key in ("data", "fg", "bg")},
                         "rendered": {key: getattr(outer, key) for key in ("data", "fg", "bg")}}
                        for cell, outer in checked if cell.fg != outer.fg or cell.data != outer.data][:8]
    return {"rgb_text_cells": rgb_text_cells, "distinct_rgb_foregrounds": colors,
            "diagnostic_text_styles": styles, "rgb_emphasis_distinguishable": emphasis,
            "mismatched_cells": mismatch,
            "mismatch_attributes": mismatch_keys,
            "mismatch_samples": mismatch_samples,
            "exact_rgb_and_emphasis": len(colors) >= 2 and emphasis
            and bool(checked) and mismatch == 0}


def color_passed(result: dict[str, object]) -> bool:
    return all(result.get(key) for key in (
        "live_omp_ready", "same_output_replayed", "real_renderer_ready",
        "matched_geometry", "rgb_sgr_in_original_bytes", "rgb_sgr_in_rendered_bytes",
        "exact_rgb_and_emphasis", "cleaned",
    ))


def provider() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        requests = 0

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            type(self).requests += 1
            payload = {"id": "g1-color", "object": "chat.completion.chunk", "created": 0,
                       "model": "scripted", "choices": [{"index": 0, "delta": {
                           "role": "assistant", "content": "G1_COLOR_NORMAL **G1_COLOR_BOLD** _G1_COLOR_ITALIC_"},
                           "finish_reason": None}]}
            end = {"id": "g1-color", "object": "chat.completion.chunk", "created": 0,
                   "model": "scripted", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 1}}
            data = (f"data: {json.dumps(payload)}\n\ndata: {json.dumps(end)}\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def run() -> dict[str, object]:
    omp = shutil.which("omp")
    version = subprocess.run([omp, "--version"], capture_output=True, text=True,
                             timeout=5).stdout.strip() if omp else "missing"
    result: dict[str, object] = {"omp_version": version, "result": "unknown",
                                "theme": "same captured live OMP frame",
                                "TERM": "xterm-256color", "COLORTERM": "truecolor",
                                "FORCE_COLOR": "3", "NO_COLOR": "removed for original OMP"}
    if version != "omp/18.2.10":
        return result
    sessions = []
    children: set[int] = set()
    with tempfile.TemporaryDirectory(prefix="cw02-g1-color-") as temporary:
        root = Path(temporary)
        config = root / "omp.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        server = provider()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (profile / "models.yml").write_text(
            f"providers:\n  g1-color:\n    baseUrl: http://127.0.0.1:{server.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: G1 color diagnostic\n"
            "        contextWindow: 32768\n        maxTokens: 1024\n"
        )
        environment = {"TERM": "xterm-256color", "COLORTERM": "truecolor",
                       "LANG": "C.UTF-8", "PI_CODING_AGENT_DIR": str(profile), "FORCE_COLOR": "3"}
        clean_argv = ["/usr/bin/env", "-u", "NO_COLOR"]
        stale_context = sorted(key for key in os.environ if key.startswith("HERDR_"))
        for key in stale_context:
            clean_argv.extend(["-u", key])
        result["stale_herdr_context_removed"] = stale_context
        try:
            original = PtySession([*clean_argv, omp, "--no-session", "--no-tools", "--no-pty",
                                   "--no-extensions", "--no-skills", "--no-rules",
                                   "--no-title", "--model", "g1-color/scripted",
                                   "--config", str(config)], env=environment)
            sessions.append(original)
            original.resize(41, 78)
            source = TerminalScreen(78, 41, reply=original.write)
            raw = bytearray()
            class RecordingStream:
                def feed(self, data: bytes) -> None:
                    raw.extend(data)
                    source_stream.feed(data)
            source_stream = make_stream(source)
            recorder = RecordingStream()
            ready, _ = pump(original, recorder, 15, lambda: "π >" in visible(source))
            original.write(b"Render diagnostic formatting. Do not use tools.\r")
            formatted, _ = pump(original, recorder, 15, lambda: "G1_COLOR_ITALIC" in visible(source))
            pump(original, recorder, 1, lambda: False)
            os.kill(original.pid, signal.SIGSTOP)
            quiet, _ = drain_until_quiet(original, recorder, timeout=3)
            result["live_omp_ready"] = ready and formatted and quiet and original.poll() is None
            result["provider_requests"] = server.RequestHandlerClass.requests
            result["original_output_bytes"] = len(raw)
            result["original_output_sha256"] = hashlib.sha256(raw).hexdigest()
            result["rgb_sgr_in_original_bytes"] = b";2;" in raw
            captured = root / "omp-output.bin"
            captured.write_bytes(raw)
            replay = shlex.join([sys.executable, str(Path(__file__).resolve()), "--replay", str(captured)])
            idle = shlex.join([sys.executable, "-c", "import time; time.sleep(60)"])
            renderer = PtySession([*clean_argv, sys.executable, str(APP), "--manager-cmd", replay,
                                   "--worker-cmd", idle, "--host-cmd", idle], env=environment)
            sessions.append(renderer)
            renderer.resize(45, 240)
            rendered = TerminalScreen(240, 45, reply=renderer.write)
            rendered_raw = bytearray()
            ready, _ = pump(renderer, make_stream(rendered), 12,
                            lambda: compare(source, rendered)["exact_rgb_and_emphasis"], observe=rendered_raw.extend)
            result["real_renderer_ready"] = ready and renderer.poll() is None
            result["same_output_replayed"] = captured.read_bytes() == bytes(raw)
            result["matched_geometry"] = source.columns == 78 and source.lines == 41
            result["rgb_sgr_in_rendered_bytes"] = b";2;" in rendered_raw
            result.update(compare(source, rendered))
            import re
            children.update(int(pid) for pid in re.findall(r"pid=(\d+)", visible(rendered)))
            result["pids"] = {"original_omp": original.pid, "workbench": renderer.pid,
                              "replay_and_idle_children": sorted(children)}
        finally:
            for session in reversed(sessions):
                session.write(QUIT if session is sessions[-1] and len(sessions) == 2 else b"\x03")
                if session is sessions[-1] and len(sessions) == 2:
                    deadline = time.monotonic() + 3
                    while session.poll() is None and time.monotonic() < deadline:
                        session.read_available()
                        time.sleep(0.02)
                else:
                    os.kill(session.pid, signal.SIGCONT)
                session.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            result["cleaned"] = bool(sessions) and all(session.poll() is not None for session in sessions) and all(
                not Path(f"/proc/{pid}").exists() for pid in children)
    result["result"] = "passed" if color_passed(result) else "unknown"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    if args.replay:
        time.sleep(0.5)  # Parent completes the prepared pane geometry before replay.
        data = args.replay.read_bytes()
        while data:
            data = data[os.write(1, data[:65536]):]
        time.sleep(60)
    else:
        observation = run()
        print(json.dumps(observation, sort_keys=True))
        raise SystemExit(0 if observation["result"] == "passed" else 1)
