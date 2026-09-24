"""Bounded OMP v18.2.10 probe for the product G3 write wrapper.

The local scripted provider requests one write into a temporary cwd. PTY
output is drained without retention; only lifecycle and file facts are printed.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import BridgeHarness, _drain_pty, _stop_omps


REPO = Path(__file__).resolve().parents[3]
EXTENSION = REPO / "omp_bridge" / "g3" / "bridge.ts"


def _provider(target: Path) -> ThreadingHTTPServer:
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
                        "index": 0,
                        "id": "product-write-A",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps({"path": str(target), "content": "PRODUCT_G3_OK"}),
                        },
                    }],
                }
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "Product write probe complete."}
                finish = "stop"
            frames = [
                {"id": "g3-product", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "g3-product", "object": "chat.completion.chunk", "created": 0,
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


def _run(auto_approve: bool) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="cw04-product-g3-") as temporary:
        root = Path(temporary)
        cwd = root / "cwd"
        cwd.mkdir()
        session_dir = root / "sessions"
        session_dir.mkdir()
        profile = root / "agent"
        profile.mkdir()
        config = root / "config.yml"
        config.write_text("# Probe-only overlay\n")
        target = cwd / "A.txt"
        provider = _provider(target)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "providers:\n"
            "  g3-product:\n"
            f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
            "    api: openai-completions\n"
            "    auth: none\n"
            "    models:\n"
            "      - id: scripted\n"
            "        name: Scripted product G3 probe\n"
            "        contextWindow: 32768\n"
            "        maxTokens: 1024\n"
        )
        token = str(uuid4())
        server = BridgeHarness(root / "bridge.sock", {"worker": token})
        os.chmod(root / "bridge.sock", 0o600)
        bridge_thread = threading.Thread(target=server.serve_forever, daemon=True)
        bridge_thread.start()
        children: list[dict[str, object]] = []
        drains: list[tuple[threading.Thread, threading.Event, dict[str, object]]] = []
        observations: dict[str, object] = {"mode": "auto_approve" if auto_approve else "native_approval"}
        started = time.monotonic()
        try:
            omp = shutil.which("omp")
            if omp is None:
                raise RuntimeError("OMP executable unavailable")
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(cwd)
                env = dict(os.environ)
                env.update({
                    "TERM": "xterm-256color",
                    "PI_CODING_AGENT_DIR": str(profile),
                    "WORKBENCH_G3_BRIDGE_SOCKET": str(root / "bridge.sock"),
                    "WORKBENCH_G3_ROLE": "worker",
                    "WORKBENCH_G3_TOKEN": token,
                    "WORKBENCH_G3_GENERATION": "1",
                    "WORKBENCH_G3_EXPECTED_OMP_VERSION": "omp/18.2.10",
                })
                args = [omp, "--print", "--model", "g3-product/scripted", "--no-session", "--no-pty",
                        "--no-skills", "--no-rules", "--no-title", "--no-extensions", "--extension",
                        str(EXTENSION), "--tools=write", "--max-time=30", "--cwd", str(cwd),
                        "--session-dir", str(session_dir), "--config", str(config),
                        "Run the scripted product G3 write probe."]
                if auto_approve:
                    args.insert(args.index("--max-time=30"), "--auto-approve")
                else:
                    args.insert(args.index("--max-time=30"), "--approval-mode=always-ask")
                os.execvpe(omp, args, env)
            child = {"pid": pid, "fd": fd, "role": "worker"}
            children.append(child)
            stop = threading.Event()
            drain_result: dict[str, object] = {"role": "worker"}
            drain = threading.Thread(target=_drain_pty, args=(fd, stop, drain_result), daemon=True)
            drain.start()
            drains.append((drain, stop, drain_result))

            peer = server.peer("worker", timeout=10)
            observations["omp_pid"] = peer["pid"]
            observations["session_generation"] = peer["generation"]
            if not auto_approve:
                try:
                    server.wait_event("worker", "tool_approval_requested", timeout=15)
                    observations["approval_requested"] = True
                    observations["file_before_approval"] = target.exists()
                    ack = server.request("worker", {"kind": "probe"}, timeout=3)
                    state = ack.get("state")
                    observations["approval_pending_state"] = (
                        isinstance(state, dict) and state.get("approvalPending") is True
                    )
                    # The TUI is attached to this disposable PTY. Approve only this
                    # scripted write to the path under the temporary cwd.
                    os.write(fd, b"y")
                    observations["approval_key_sent"] = "y"
                except TimeoutError:
                    observations["approval_requested"] = False
            try:
                server.wait_event("worker", "agent_end", timeout=max(0.1, 35 - (time.monotonic() - started)))
                observations["agent_end"] = True
            except TimeoutError:
                observations["agent_end"] = False
            observations["provider_requests"] = provider.RequestHandlerClass.requests
            observations["approval_resolved"] = server.event_count("worker", "tool_approval_resolved") > 0
            observations["execution_started"] = server.event_count("worker", "tool_execution_start") > 0
            observations["execution_ended"] = server.event_count("worker", "tool_execution_end") > 0
            observations["file_content_exact"] = target.exists() and target.read_text() == "PRODUCT_G3_OK"
            with server.condition:
                observations["lifecycle"] = [
                    {key: event.get(key) for key in ("name", "toolName", "approved") if event.get(key) is not None}
                    for event in server.events
                    if event.get("name") in (
                        "tool_approval_requested", "tool_approval_resolved",
                        "tool_execution_start", "tool_execution_end", "agent_end",
                    )
                ]
            observations["pty_bytes_drained"] = drain_result.get("bytes", 0)
            observations["elapsed_seconds"] = round(time.monotonic() - started, 2)
            observations["passed"] = (
                observations["agent_end"] is True
                and observations["provider_requests"] == 2
                and observations["execution_started"] is True
                and observations["execution_ended"] is True
                and observations["file_content_exact"] is True
                and (auto_approve or (
                    observations.get("approval_requested") is True
                    and observations.get("file_before_approval") is False
                    and observations.get("approval_pending_state") is True
                    and observations["approval_resolved"] is True
                ))
            )
            observations["result"] = "passed" if observations["passed"] else "inconclusive"
            return observations
        finally:
            _stop_omps(children)
            for drain, stop, _ in drains:
                stop.set()
                drain.join(timeout=1)
            for child in children:
                os.close(int(child["fd"]))
            server.shutdown()
            server.server_close()
            bridge_thread.join(timeout=2)
            provider.shutdown()
            provider.server_close()
            provider_thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-approval", action="store_true")
    args = parser.parse_args()
    omp = shutil.which("omp")
    version = subprocess.run([omp, "--version"], text=True, capture_output=True, timeout=10).stdout.strip() if omp else "missing"
    if version != "omp/18.2.10":
        print(json.dumps({"result": "inconclusive", "omp_version": version}))
        return 2
    observations = _run(auto_approve=not args.native_approval)
    print(json.dumps({"omp_version": version, **observations}))
    return 0 if observations["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
