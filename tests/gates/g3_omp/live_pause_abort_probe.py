"""Bounded actual OMP 18.2.10 probe of the product G3 bridge pause boundary.

A local scripted provider asks native bash to write only in a temporary cwd.
No model credentials or response body are retained. This probe is one manager
turn; it does not establish the full two-process G3 gate.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import BridgeHarness, _drain_pty, _stop_omps

REPO = Path(__file__).resolve().parents[3]
EXTENSION = REPO / "omp_bridge/g3/bridge.ts"
VERSION = "omp/18.2.10"


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
                        "index": 0, "id": "pause-probe-bash", "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({
                            "command": "printf STARTED > started.txt; sleep 8; printf FINISHED > result.txt"
                        })},
                    }],
                }
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "Probe turn finished."}
                finish = "stop"
            frames = [
                {"id": "pause-probe", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "pause-probe", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            payload = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def group_members(pgid: int) -> list[int]:
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.getpgid(int(entry.name)) == pgid:
                members.append(int(entry.name))
        except (ProcessLookupError, PermissionError):
            pass
    return sorted(members)


def cwd_processes(cwd: Path) -> list[int]:
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "cwd").resolve() == cwd:
                members.append(int(entry.name))
        except (OSError, PermissionError):
            pass
    return sorted(members)


def run(*, interactive: bool = False) -> dict[str, object]:
    omp = shutil.which("omp")
    if not omp:
        return {"result": "inconclusive", "reason": "omp unavailable"}
    version = subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    if version != VERSION:
        return {"result": "inconclusive", "reason": "version mismatch", "version": version}
    with tempfile.TemporaryDirectory(prefix="cw04-g3-abort-") as temp:
        root = Path(temp)
        cwd = root / "cwd"
        cwd.mkdir()
        profile = root / "agent"
        profile.mkdir()
        (root / "sessions").mkdir()
        config = root / "config.yml"
        config.write_text("# Probe-only settings overlay.\n")
        scripted = provider()
        provider_thread = threading.Thread(target=scripted.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "providers:\n"
            "  g3-abort-probe:\n"
            f"    baseUrl: http://127.0.0.1:{scripted.server_port}/v1\n"
            "    api: openai-completions\n"
            "    auth: none\n"
            "    models:\n"
            "      - id: scripted\n"
            "        name: Scripted G3 abort probe\n"
            "        contextWindow: 32768\n"
            "        maxTokens: 1024\n"
        )
        socket_path = root / "bridge.sock"
        token = str(uuid4())
        server = BridgeHarness(socket_path, {"manager": token})
        os.chmod(socket_path, 0o600)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        children: list[dict[str, object]] = []
        drain_stop = threading.Event()
        drain_result: dict[str, object] = {}
        drain: threading.Thread | None = None
        evidence: dict[str, object] = {
            "result": "inconclusive", "version": version,
            "mode": "interactive" if interactive else "print",
        }
        try:
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(cwd)
                env = dict(os.environ)
                env.update({
                    "TERM": "xterm-256color", "PI_CODING_AGENT_DIR": str(profile),
                    "WORKBENCH_G3_BRIDGE_SOCKET": str(socket_path),
                    "WORKBENCH_G3_ROLE": "manager", "WORKBENCH_G3_TOKEN": token,
                    "WORKBENCH_G3_GENERATION": "1",
                    "WORKBENCH_G3_EXPECTED_OMP_VERSION": VERSION,
                })
                os.execvpe(omp, [
                    omp, *([] if interactive else ["--print"]),
                    "--model", "g3-abort-probe/scripted",
                    "--no-session", "--no-pty", "--no-skills", "--no-rules", "--no-title",
                    "--no-extensions", "--extension", str(EXTENSION),
                    "--tools=bash", "--auto-approve", "--max-time=25",
                    "--cwd", str(cwd), "--session-dir", str(root / "sessions"),
                    "--config", str(config), "Run the local scripted pause probe.",
                ], env)
            children.append({"pid": pid, "fd": fd})
            os.set_blocking(fd, False)
            drain = threading.Thread(target=_drain_pty, args=(fd, drain_stop, drain_result), daemon=True)
            drain.start()
            peer = server.peer("manager", timeout=10)
            evidence["omp_pid"] = peer["pid"]
            try:
                server.wait_event("manager", "tool_execution_start", timeout=10)
            except TimeoutError:
                evidence["reason"] = "native bash tool execution did not start"
                return evidence
            start_deadline = time.monotonic() + 2
            while not (cwd / "started.txt").exists() and time.monotonic() < start_deadline:
                time.sleep(0.02)
            evidence["started_file_observed_before_pause"] = (cwd / "started.txt").read_text() == "STARTED" if (cwd / "started.txt").exists() else False
            evidence["processes_before_pause"] = group_members(pid)
            evidence["cwd_processes_before_pause"] = cwd_processes(cwd)
            pause_started = time.monotonic()
            pause = server.request("manager", {"kind": "pause"}, timeout=3)
            evidence["pause_ack"] = pause.get("status")
            evidence["pause_ack_seconds"] = round(time.monotonic() - pause_started, 3)
            evidence["pause_ack_unknown_tool_ids"] = pause.get("unconfirmedToolCallIds")
            evidence["tool_end_seen_at_ack"] = server.event_count("manager", "tool_execution_end") > 0
            peer_session = str(peer["session_id"])
            envelope = {
                "schemaVersion": 1, "messageId": str(uuid4()), "deliveryAttemptId": str(uuid4()),
                "senderRole": "worker", "sessionId": peer_session,
                "sessionGeneration": int(peer["generation"]),
                "taskId": str(uuid4()), "revisionId": str(uuid4()), "runId": str(uuid4()),
                "event": {"type": "message", "messageKind": "report", "payload": {"text": "held probe"}},
            }
            evidence["paused_delivery_status"] = server.request("manager", {
                "kind": "deliver", "envelope": json.dumps(envelope)
            }, timeout=3).get("status")
            try:
                server.wait_event("manager", "turn_stop_observed", timeout=8)
                evidence["turn_stop_observed"] = True
            except TimeoutError:
                evidence["turn_stop_observed"] = False
            evidence["agent_end_observed"] = server.event_count("manager", "agent_end") > 0
            try:
                state = server.request("manager", {"kind": "probe"}, timeout=1).get("state")
            except (TimeoutError, OSError):
                state = None
            evidence["post_stop_state"] = {
                key: state.get(key) for key in ("paused", "abortStatus", "unconfirmedToolCallIds", "inFlightToolCount")
            } if isinstance(state, dict) else None
            evidence["started_file_exists"] = (cwd / "started.txt").exists()
            evidence["result_file_exists"] = (cwd / "result.txt").exists()
            evidence["result_file_exact"] = (cwd / "result.txt").read_text() == "FINISHED" if (cwd / "result.txt").exists() else False
            evidence["processes_after_stop"] = group_members(pid)
            evidence["cwd_processes_after_stop"] = cwd_processes(cwd)
            try:
                recon = server.request("manager", {"kind": "resume"}, timeout=1)
                evidence["resume_check_status"] = recon.get("status")
                evidence["resume_check_paused"] = recon.get("state", {}).get("paused") if isinstance(recon.get("state"), dict) else None
                evidence["resume_ack"] = server.request("manager", {"kind": "resume", "reconciled": True}, timeout=1).get("status")
                envelope["deliveryAttemptId"] = str(uuid4())
                evidence["stale_retry_status"] = server.request("manager", {
                    "kind": "deliver", "envelope": json.dumps(envelope)
                }, timeout=1).get("status")
            except (TimeoutError, OSError):
                evidence["resume_unverified"] = "bridge socket closed after print-mode turn"
            evidence["provider_requests"] = scripted.RequestHandlerClass.requests
            evidence["event_names"] = [event.get("name") for event in server.events if event.get("role") == "manager"]
            evidence["result"] = "passed_full_probe" if (
                evidence["started_file_observed_before_pause"]
                and evidence["pause_ack"] == "abort_requested"
                and evidence["pause_ack_seconds"] < 2
                and not evidence["tool_end_seen_at_ack"]
                and evidence["turn_stop_observed"]
                and evidence["paused_delivery_status"] == "deferred"
                and evidence.get("resume_check_status") == "reconciliation_required"
                and evidence.get("resume_check_paused") is True
                and evidence.get("resume_ack") == "resumed"
                and evidence.get("stale_retry_status") == "unknown_no_replay"
            ) else "passed_abort_only" if (
                evidence["started_file_observed_before_pause"]
                and evidence["pause_ack"] == "abort_requested"
                and evidence["pause_ack_seconds"] < 2
                and not evidence["tool_end_seen_at_ack"]
                and evidence["turn_stop_observed"]
                and evidence["paused_delivery_status"] == "deferred"
            ) else "inconclusive"
            return evidence
        finally:
            _stop_omps(children)
            # Native bash may have created a different process group. Only stop
            # processes still bound to this unique temporary working directory.
            for remaining_pid in cwd_processes(cwd):
                try:
                    os.kill(remaining_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            stop_deadline = time.monotonic() + 1
            while cwd_processes(cwd) and time.monotonic() < stop_deadline:
                time.sleep(0.02)
            for remaining_pid in cwd_processes(cwd):
                try:
                    os.kill(remaining_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if children:
                evidence["processes_after_cleanup"] = group_members(int(children[0]["pid"]))
                evidence["cwd_processes_after_cleanup"] = cwd_processes(cwd)
            drain_stop.set()
            if drain:
                drain.join(timeout=1)
            for child in children:
                try:
                    os.close(int(child["fd"]))
                except OSError:
                    pass
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            scripted.shutdown()
            scripted.server_close()
            provider_thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interactive", action="store_true", help="probe the persistent TUI session")
    result = run(interactive=parser.parse_args().interactive)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["result"] in {"passed_abort_only", "passed_full_probe"} else 1)
