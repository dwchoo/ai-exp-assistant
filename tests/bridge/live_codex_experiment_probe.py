"""CW-18 smoke-02 E1/E3 live probe: an experiment with two real OMPs on a local openai-codex-shaped provider, no model.

The real-model smoke-02 (openai-codex, OMP 18.4.5) never started an experiment: the bridge looked for the
delivery only in ``payload.messages`` while the codex provider sends Responses ``input`` items (over the Codex
WebSocket only the delta after ``previous_response_id``), never emits ``after_provider_response``, and a
reasoning model's message starts with an empty thinking block. This probe runs the U3 automation setup
(``tests/backend/live_automation_probe.py``: product G3 bridge, HandoffService/TaskFlow, TaskWorkflow with
TaskMailbox and G3WorkerResponsePort, AutomationController with the review scheduler, a real host ShellPane)
but each OMP's only model is a local scripted server speaking the ``openai-codex-responses`` API: Responses
``input`` in, Responses events out (a reasoning item with encrypted content before every message or function
call), over the Codex WebSocket by default (``PI_CODEX_WEBSOCKET=1``; ``--sse`` uses the HTTP SSE transport).
The fake key is a local JWT-shaped string with a fake account id; no credential exists in the fake HOMEs and every
other HTTP(S) request goes to a closed proxy port.

Stages:
  H  manager to_worker experiment whose worker execute decision is ``hold`` -> the TASK is confirmed processed
     (identity in the Responses payload) -> run_start_failed -> the manager receives one Workbench notice
     (smoke-02 E3) -> the worker is idle and the Task finished.
  E  manager to_worker experiment -> worker execute decision (staged marker, OMP_PROCESSED) -> the command runs
     in the host shell -> periodic worker review(s) -> collect -> worker analysis -> report to the manager ->
     judged success, run closed, automation idle.

smoke-04 G1/G2 (p27-cw18-smoke-fix-04): the response contract no longer asks the model for ``response_id`` (the
bridge generates it). Between H and E:
  R  manager to_worker experiment -> the command runs -> the worker's analysis marker carries a malformed
     ``message_id`` (last group 11 chars) -> ``invalid_assistant_response:bad_marker`` -> the run closes as
     indeterminate with that reason, the manager receives one Workbench notice, the worker is idle, automation
     idle, and nothing is resent.
In E the worker's analysis marker additionally carries a model-made malformed ``response_id`` (the smoke-04
failure): it is ignored and the run is still judged success.

C-D67 (smoke-03): ``--visible-reasoning`` gives every reasoning item a visible summary (the GPT-5.5 reasoning
summary shape: ``response.reasoning_summary_*`` events, ``summary: [{type: "summary_text", text}]``), so each
assistant message, including the worker's staged marker reply, starts with a thinking item carrying visible text.
The run must still start and finish, and the summary text must not reach any Workbench record.

Usage: PYTHONPATH=src python tests/bridge/live_codex_experiment_probe.py [omp] [interval_seconds] [--sse]
       [--visible-reasoning]
Prints one JSON object; exit 0 only when every check holds.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


U3 = _load(ROOT / "tests/backend/live_automation_probe.py", "cw18_u3_live_probe_for_codex")
U2, U1 = U3.U2, U3.U1
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _b64(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


# A JWT-shaped local string so that OMP's codex provider finds an account id; it authorises nothing anywhere.
FAKE_KEY = _b64({"alg": "none"}) + "." + _b64({"https://api.openai.com/auth": {
    "chatgpt_account_id": "local-probe-account"}, "exp": 4102444800}) + ".local"


MALFORMED_UUID = "0d4c8a3e-5b2f-4c1a-9e7d-2a6b4f8c1d3"  # smoke-04: a model-made UUID whose last group has 11 chars


def frame_from_contract(observed: dict[str, Any], decision: str, *, malformed: str | None = None,
                        model_response_id: str | None = None) -> str | None:
    """The fixture model obeys the delivered contract (smoke-04 G1: nothing to generate; every field but the
    decision is given). ``malformed`` corrupts that identity field (drops its last char); ``model_response_id``
    adds a response_id the bridge must ignore."""
    contract = observed.get("response_contract")
    if not isinstance(contract, dict) or contract.get("version") != 1:
        return None
    fields, order = contract.get("fields"), contract.get("field_order")
    if (not isinstance(fields, list) or not isinstance(order, list)
            or [item.get("name") for item in fields if isinstance(item, dict)] != order):
        return None
    response: dict[str, Any] = {}
    for field in fields:
        if "generate" in field:
            return None  # the model must not be asked to invent a value any more
        if field.get("type") == "enum_string":
            if decision not in (field.get("allowed") or []):
                return None
            if model_response_id is not None:
                response["response_id"] = model_response_id
            response[field["name"]] = decision
        elif "value" in field:
            value = field["value"]
            response[field["name"]] = value[:-1] if field["name"] == malformed else value
        else:
            return None
    return str(contract.get("marker")) + json.dumps(response, separators=(",", ":"))


U2.CW10._frame_from_contract = frame_from_contract  # U2's scripted worker answers execute/analysis through this


class CodexModel(U2.Model):
    """U2's scripted model (tool calls / staged markers); the worker holds an execute whose commit is ``hold``."""

    def __init__(self, role: str, experiment_spec: dict[str, Any], hold_commit: str):
        super().__init__(role, experiment_spec)
        self.hold_commit = hold_commit
        self.shapes: list[dict[str, Any]] = []
        self.corrupt_analyses = 1  # stage R: the first analysis marker carries a malformed message_id
        self.analyses: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []  # Workbench notices the manager received (codes only)

    def respond(self, request: dict[str, Any]) -> dict[str, Any]:
        messages = request.get("messages") or []
        last = messages[-1] if messages else {}
        blocks = U1._text_blocks(last.get("content"))
        text = blocks[-1].strip() if blocks else ""
        try:
            injected = json.loads(text) if last.get("role") == "user" else None
        except ValueError:
            injected = None
        payload = injected.get("payload") if isinstance(injected, dict) else None
        if self.role == "manager" and isinstance(payload, dict) and payload.get("handoff") == "workbench_notice":
            with self.lock:
                self.notices.append({"notice": payload.get("notice"), "reason": payload.get("reason"),
                                     "error": payload.get("error"), "judgment": payload.get("judgment"),
                                     "not_judged": payload.get("not_judged"), "run_id": injected.get("run_id")})
        if (self.role == "worker" and isinstance(payload, dict) and payload.get("stage") == "analysis"
                and "response_contract" in injected):
            with self.lock:
                self.requests += 1
                corrupt = self.corrupt_analyses > 0
                self.corrupt_analyses -= 1 if corrupt else 0
                self.analyses.append({"run_id": injected.get("run_id"), "corrupt": corrupt})
                self.injected.append({"kind": injected.get("kind"), "stage": "analysis",
                                      "message_id": injected.get("workbench_message_id"),
                                      "run_id": injected.get("run_id")})
            facts = payload.get("facts") or {}
            ok = (facts.get("exit_status") == 0 and "PASS" in (facts.get("raw_log_excerpt") or "")
                  and "PASS" in (facts.get("result_excerpt") or ""))
            frame = (frame_from_contract(injected, "success" if ok else "failure", malformed="message_id")
                     if corrupt else
                     frame_from_contract(injected, "success" if ok else "failure", model_response_id=MALFORMED_UUID))
            return {"content": frame or "invalid"}
        if self.role == "manager" and text == "stage-hold":
            with self.lock:
                self.requests += 1
            spec = json.loads(json.dumps(self.experiment_spec))
            spec["execution"]["commit"] = self.hold_commit
            return self._call("to_worker", {"kind": "experiment", "message": "probe experiment to hold", "spec": spec})
        if self.role == "worker" and last.get("role") == "user":
            if (isinstance(payload, dict) and payload.get("stage") == "execute"
                    and payload.get("commit") == self.hold_commit and "response_contract" in injected):
                with self.lock:
                    self.requests += 1
                    self.injected.append({"kind": injected.get("kind"), "stage": "execute", "decision": "hold",
                                          "message_id": injected.get("workbench_message_id")})
                return {"content": U2.CW10._frame_from_contract(injected, "hold") or "invalid"}
        return super().respond(request)


def responses_to_chat(payload: dict[str, Any]) -> dict[str, Any]:
    """The Responses ``input`` (full or delta) as the chat ``messages`` view the scripted model reads."""
    messages: list[dict[str, Any]] = []
    for item in payload.get("input") or []:
        if not isinstance(item, dict):
            continue
        kind, role = item.get("type"), item.get("role")
        if kind == "function_call_output":
            output = item.get("output")
            text = output if isinstance(output, str) else "".join(U1._text_blocks(output))
            messages.append({"role": "tool", "tool_call_id": item.get("call_id"), "content": text})
        elif kind == "function_call":
            messages.append({"role": "assistant", "tool_calls": [{"id": item.get("call_id")}]})
        elif kind in (None, "message") and role in ("user", "assistant", "developer", "system"):
            content = item.get("content")
            blocks = [{"type": "text", "text": text} for text in U1._text_blocks(content)]
            messages.append({"role": role, "content": blocks})
    return {"messages": messages, "tools": [{"function": {"name": tool.get("name")}}
                                            for tool in payload.get("tools") or [] if isinstance(tool, dict)]}


VISIBLE_REASONING = "WB_PROBE_VISIBLE_REASONING_SENTINEL deciding how to answer"


def responses_events(answer: dict[str, Any], number: int, visible_reasoning: bool = False) -> list[dict[str, Any]]:
    summary = [{"type": "summary_text", "text": VISIBLE_REASONING}] if visible_reasoning else []
    reasoning = {"type": "reasoning", "id": f"rs_{number}", "summary": summary, "encrypted_content": "local-opaque"}
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"id": f"resp_{number}", "object": "response",
                                                  "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "reasoning", "id": reasoning["id"], "summary": []}},
    ]
    if visible_reasoning:
        where = {"item_id": reasoning["id"], "output_index": 0, "summary_index": 0}
        events += [
            {"type": "response.reasoning_summary_part.added", **where, "part": {"type": "summary_text", "text": ""}},
            {"type": "response.reasoning_summary_text.delta", **where, "delta": VISIBLE_REASONING},
            {"type": "response.reasoning_summary_text.done", **where, "text": VISIBLE_REASONING},
            {"type": "response.reasoning_summary_part.done", **where, "part": summary[0]},
        ]
    events.append({"type": "response.output_item.done", "output_index": 0, "item": reasoning})
    output: list[dict[str, Any]] = [reasoning]
    if "tool_calls" in answer:
        for offset, (name, args, call_id) in enumerate(answer["tool_calls"], start=1):
            arguments = json.dumps(args)
            item = {"type": "function_call", "id": f"fc_{number}_{offset}", "call_id": call_id, "name": name,
                    "arguments": arguments, "status": "completed"}
            events += [
                {"type": "response.output_item.added", "output_index": offset,
                 "item": {**item, "arguments": "", "status": "in_progress"}},
                {"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": offset,
                 "delta": arguments},
                {"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": offset,
                 "arguments": arguments},
                {"type": "response.output_item.done", "output_index": offset, "item": item},
            ]
            output.append(item)
    else:
        text = answer["content"]
        message = {"type": "message", "id": f"msg_{number}", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": text, "annotations": []}]}
        part = {"item_id": message["id"], "output_index": 1, "content_index": 0}
        events += [
            {"type": "response.output_item.added", "output_index": 1,
             "item": {**message, "status": "in_progress", "content": []}},
            {"type": "response.content_part.added", **part, "part": {"type": "output_text", "text": "",
                                                                     "annotations": []}},
            {"type": "response.output_text.delta", **part, "delta": text},
            {"type": "response.output_text.done", **part, "text": text},
            {"type": "response.content_part.done", **part, "part": message["content"][0]},
            {"type": "response.output_item.done", "output_index": 1, "item": message},
        ]
        output.append(message)
    events.append({"type": "response.completed", "response": {
        "id": f"resp_{number}", "object": "response", "status": "completed", "output": output,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}})
    return events


def _shape(payload: dict[str, Any], transport: str) -> dict[str, Any]:
    items = payload.get("input") if isinstance(payload.get("input"), list) else []
    last = items[-1] if items and isinstance(items[-1], dict) else {}
    content = last.get("content") if isinstance(last.get("content"), list) else []
    return {"transport": transport, "messages": "messages" in payload, "input_items": len(items),
            "previous_response_id": isinstance(payload.get("previous_response_id"), str),
            "last_role": last.get("role"), "last_type": last.get("type"),
            "last_block_types": sorted({b.get("type") for b in content if isinstance(b, dict)})}


def codex_server(model: CodexModel, visible_reasoning: bool = False) -> ThreadingHTTPServer:
    counter = {"n": 0}
    lock = threading.Lock()

    def answer(payload: dict[str, Any], transport: str) -> list[dict[str, Any]]:
        with lock:
            counter["n"] += 1
            number = counter["n"]
        model.shapes.append(_shape(payload, transport))
        try:
            reply = model.respond(responses_to_chat(payload))
        except Exception as exc:  # keep the stream well formed; the checks see the missing effect
            reply = {"content": f"probe model error {type(exc).__name__}"}
        return responses_events(reply, number, visible_reasoning)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
            body = b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()
                            for e in answer(payload, "sse"))
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:
            if self.headers.get("Upgrade", "").lower() != "websocket":
                self.send_error(404)
                return
            accept = base64.b64encode(hashlib.sha1(
                (self.headers["Sec-WebSocket-Key"] + WS_GUID).encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            message = b""
            try:
                while True:
                    opcode, fin, data = self._read_frame()
                    if opcode == 8:
                        self._send(8, data[:2])
                        return
                    if opcode == 9:
                        self._send(10, data)
                        continue
                    if opcode in (1, 0):
                        message += data
                        if not fin:
                            continue
                        text, message = message.decode("utf-8", "replace"), b""
                        try:
                            payload = json.loads(text)
                        except ValueError:
                            continue
                        if payload.get("type") != "response.create":
                            continue
                        for event in answer(payload, "websocket"):
                            self._send(1, json.dumps(event).encode())
            except (ConnectionError, OSError, struct.error, EOFError):
                return

        def _read_exact(self, size: int) -> bytes:
            data = self.rfile.read(size)
            if len(data) != size:
                raise EOFError
            return data

        def _read_frame(self) -> tuple[int, bool, bytes]:
            first, second = self._read_exact(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if second & 0x80 else b"\0\0\0\0"
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read_exact(length)))
            return first & 0x0F, bool(first & 0x80), data

        def _send(self, opcode: int, data: bytes) -> None:
            header = bytes([0x80 | opcode])
            if len(data) < 126:
                header += bytes([len(data)])
            elif len(data) < 65536:
                header += bytes([126]) + struct.pack(">H", len(data))
            else:
                header += bytes([127]) + struct.pack(">Q", len(data))
            self.wfile.write(header + data)
            self.wfile.flush()

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server


class CodexRpcOmp(U1.RpcOmp):
    """U1's RPC OMP, but its fake OMP home's only model speaks openai-codex-responses on 127.0.0.1 (with a session)."""

    def __init__(self, omp: str, role: str, token: str, root: Path, socket_path: Path, port: int, websocket: bool):
        home = root / f"home-{role}"
        agent, config, cwd = home / "agent", home / "config", root / f"cwd-{role}"
        for directory in (home, agent, config, cwd):
            directory.mkdir(parents=True)
        (agent / "models.yml").write_text(
            "providers:\n  wbcodex:\n"
            f"    baseUrl: http://127.0.0.1:{port}\n"
            "    api: openai-codex-responses\n"
            f"    apiKey: {FAKE_KEY}\n"
            "    models:\n      - id: gpt-5.5\n        name: gpt-5.5\n        contextWindow: 32768\n"
            "        maxTokens: 1024\n" + ("        preferWebsockets: true\n" if websocket else ""))
        overlay = root / "overlay.yml"
        if not overlay.exists():
            overlay.write_text("startup:\n  setupWizard: false\n")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TERM": "dumb", "LANG": "C.UTF-8",
               "PI_CODING_AGENT_DIR": str(agent), "PI_CONFIG_DIR": str(config),
               "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
               "XDG_STATE_HOME": str(home / ".local/state"), "XDG_CACHE_HOME": str(home / ".cache"),
               **{key: U1.BLOCKED_PROXY for key in U1.PROXY_KEYS}, "NO_PROXY": "127.0.0.1",
               "no_proxy": "127.0.0.1", "PI_CODEX_WEBSOCKET": "1" if websocket else "0",
               "WORKBENCH_G3_BRIDGE_SOCKET": str(socket_path), "WORKBENCH_G3_ROLE": role,
               "WORKBENCH_G3_TOKEN": token, "WORKBENCH_G3_GENERATION": "1"}
        argv = [omp, "--mode", "rpc", "--no-title", "--no-extensions", "--no-skills", "--no-rules",
                "--config", str(overlay), "--extension", str(U1.EXTENSION), "--model", "wbcodex/gpt-5.5",
                "--cwd", str(cwd)]
        self.role = role
        self.stderr = (root / f"stderr-{role}.log").open("wb")
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, start_new_session=True)
        self.frames: list[dict[str, Any]] = []
        self.cond = threading.Condition()
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()


def _assistant_shapes(frames: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Content item shapes of each assistant message_end (types, keys, thinking length); no text."""
    shapes = []
    for frame in frames:
        message = frame.get("message") if frame.get("type") == "message_end" else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        shapes.append([{"type": item.get("type"), "keys": sorted(item),
                        **({"thinking_chars": len(item.get("thinking") or "")} if item.get("type") == "thinking" else {})}
                       for item in message.get("content") or [] if isinstance(item, dict)])
    return shapes


def _files_containing(root: Path, needle: bytes) -> list[str]:
    """Workbench-owned files under the probe root holding ``needle`` (the OMP homes and their stderr excluded)."""
    found = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0].startswith(("home-", "cwd-", "stderr-")) or not path.is_file() or path.is_symlink():
            continue
        try:
            if needle in path.read_bytes():
                found.append(str(relative))
        except OSError:
            continue
    return found


def run(omp: str, interval: int, websocket: bool, visible_reasoning: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": "two-real-omp-rpc+local-openai-codex-responses+host-shellpane+automation",
                              "transport": "websocket" if websocket else "sse", "interval_seconds": interval,
                              "visible_reasoning": visible_reasoning, "checks": {}}
    checks = result["checks"]
    version = subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=30,
                             env={"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent"})
    result["omp"] = (version.stdout or version.stderr).strip().splitlines()[0]
    root = Path(tempfile.mkdtemp(prefix="wb-p27-cw18-codex-live-", dir="/tmp"))
    source = root / "source"
    source.mkdir()
    U2.git(source, "init", "-q")
    U2.git(source, "config", "user.email", "probe@example.invalid")
    U2.git(source, "config", "user.name", "CW18 codex probe")
    duration = interval + 20
    (source / "run.sh").write_text(
        "#!/bin/sh\nprintf 'PASS from the host shell\\n'\n"
        f"i=0; while [ $i -lt {duration} ]; do i=$((i+1)); printf 'tick %s\\n' $i; sleep 1; done\n"
        "printf PASS > outcome.txt\nprintf 'PASS from the host shell\\n'\n")
    (source / "run.sh").chmod(0o755)
    U2.git(source, "add", "run.sh")
    U2.git(source, "commit", "-qm", "probe")
    commit = U2.git(source, "rev-parse", "HEAD")
    (source / "hold.txt").write_text("hold\n")
    U2.git(source, "add", "hold.txt")
    U2.git(source, "commit", "-qm", "hold")
    hold_commit = U2.git(source, "rev-parse", "HEAD")
    experiment_spec = {"goal": "probe experiment", "paths": ["outcome.txt"], "execution": {
        "source": str(source), "commit": commit, "command": "./run.sh",
        "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
        "environment": ["PATH"], "shell": "bash"}}
    for name in ("workflow", "worktrees", "runs", "shell-home", "raw"):
        (root / name).mkdir(mode=0o700)
    models = {role: CodexModel(role, experiment_spec, hold_commit) for role in U1.ROLES}
    servers = {role: codex_server(models[role], visible_reasoning) for role in U1.ROLES}
    for server in servers.values():
        threading.Thread(target=server.serve_forever, daemon=True).start()
    tokens = {role: str(uuid4()) for role in U1.ROLES}
    socket_path = root / "bridge.sock"
    bridge = U3.G3BridgeServer(socket_path, tokens)
    bridge.start()
    db = root / "tasks.sqlite3"
    U3.TaskRepository(db).close()

    def lane_mailbox():
        own = U3.TaskRepository(db)
        return U3.TaskMailbox(own, bridge), own.close

    def omp_idle(role):
        try:
            return bridge.probe(role, timeout=1.0).get("idle") is True
        except (U3.MailboxError, OSError, TimeoutError):
            return None

    shell_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root / "shell-home"),
                 "LANG": "C.UTF-8", "TERM": "xterm-256color"}
    pane = U3.ShellPane(U3.ShellChoice("bash", shutil.which("bash") or "/usr/bin/bash"), dict(shell_env))
    stop = threading.Event()

    def backend_loop():
        while not stop.is_set():
            pane.pump()
            time.sleep(0.01)

    loop_thread = threading.Thread(target=backend_loop, daemon=True)
    loop_thread.start()
    raw = U3.RawLogStore(root / "raw")
    logs: list[str] = []
    automation = U3.AutomationController(
        bridge=bridge, database=db, journal=U3.LifecycleJournal(root / "lifecycle.json"), raw=raw,
        shell_pane=lambda: pane, project_dir=root / "cwd-worker", artifacts_root=root / "runs",
        boot_marker=lambda: Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        interval_seconds=interval, log=lambda line: logs.append(f"{time.monotonic():.1f} {line}"))

    def automation_port():
        return {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": automation.paused(), "cancelled": False, "metadataHealthy": True, "approvalValid": True}}

    handoffs = U3.HandoffService(root / "workflow" / "handoffs.jsonl", mailbox_factory=lane_mailbox,
                                 paused=automation.paused, peer_lookup=lambda role: bridge.peer(role, 0),
                                 retry_interval=0.2)
    ports = U3.ExperimentPorts(
        host_shell=lambda: U3.HostShellPort(pane, lambda: pane),
        make_workflow=lambda repository: U3.TaskWorkflow(repository, U3.TaskMailbox(repository, bridge),
                                                         worker_port=U3.G3WorkerResponsePort(bridge),
                                                         automation_source=automation_port),
        automation=automation_port, environment_names=lambda: set(shell_env),
        worktrees_root=root / "worktrees", artifacts_root=root / "runs")
    flow = U3.TaskFlow(root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: U3.TaskRepository(db),
                       handoffs=handoffs, omp_idle=omp_idle, paused=automation.paused, experiment=ports,
                       lifecycle=automation)
    handoffs.configure(policy=flow, active_task=flow.active_task)
    handoffs.start()
    flow.start()
    automation.start()
    bridge.set_tool_handler(lambda peer, request: handoffs.handle(peer.role, request))
    omps: dict[str, Any] = {}

    def state() -> dict[str, Any]:
        return automation.status()

    def notices() -> list[dict[str, Any]]:
        return [item for item in models["manager"].injected if item.get("handoff") == "workbench_notice"]

    def reviews() -> list[dict[str, Any]]:
        return [item for item in models["worker"].injected if item.get("stage") == "periodic_review"]

    def run_record(run_id: str | None) -> dict[str, Any]:
        try:
            return json.loads((root / "runs" / str(run_id) / "run.json").read_text())
        except (OSError, ValueError):
            return {}

    try:
        for role in U1.ROLES:
            omps[role] = CodexRpcOmp(omp, role, tokens[role], root, socket_path, servers[role].server_port, websocket)
        peers = {role: bridge.peer(role, timeout=40) for role in U1.ROLES}
        checks["both_omps_connected"] = len({peer.pid for peer in peers.values()}) == 2

        # Stage H: a held execute decision ends the start; the manager is told once (E3).
        omps["manager"].send({"id": "h", "type": "prompt", "message": "stage-hold"})
        checks["hold_task_finished_start_failed"] = U2.wait_for(
            lambda: ((flow.task_view() or {}).get("status"), (flow.task_view() or {}).get("held_reason"))
            == ("finished", "start_failed:WorkflowHeld"), 60)
        hold_task = flow.task_view() or {}
        result["hold_task"] = {k: hold_task.get(k) for k in ("task_id", "status", "held_reason", "last_result")}
        checks["hold_decision_reached_worker"] = any(item.get("decision") == "hold"
                                                     for item in models["worker"].injected)
        checks["manager_told_once"] = U2.wait_for(lambda: len(notices()) == 1, 30)
        time.sleep(2)
        checks["manager_notice_not_repeated"] = len(notices()) == 1
        result["manager_notice"] = notices()[:2]
        checks["worker_idle_after_hold"] = flow.worker_view().get("state") == "idle"

        # Stage R (smoke-04 G2/G3): the worker's analysis marker is rejected -> indeterminate, one notice, clean close.
        omps["manager"].wait(lambda f: f.get("type") == "agent_end", 30)
        omps["manager"].send({"id": "r", "type": "prompt", "message": "stage-exp"})
        checks["reject_dispatched"] = U2.wait_for(lambda: (flow.task_view() or {}).get("task_id")
                                                  not in (None, hold_task.get("task_id"))
                                                  and flow.worker_view().get("state") == "busy", 40)
        reject_task = flow.task_view() or {}
        reason = "invalid_assistant_response:bad_marker"
        checks["reject_task_finished_with_reason"] = U2.wait_for(
            lambda: ((flow.task_view() or {}).get("status"), (flow.task_view() or {}).get("held_reason"))
            == ("finished", f"judgment_unavailable:{reason}"), duration + 120)
        reject_view = flow.task_view() or {}
        reject_last = reject_view.get("last_result") or {}
        result["reject_task"] = {k: reject_view.get(k) for k in ("task_id", "status", "held_reason", "last_result")}
        checks["reject_same_task"] = reject_view.get("task_id") == reject_task.get("task_id")
        checks["reject_indeterminate_closed"] = (
            reject_last.get("outcome"), reject_last.get("judgment"), reject_last.get("reason"),
            reject_last.get("run_closed")) == ("judgment_unavailable", "indeterminate", reason, True)
        reject_record = run_record(reject_last.get("run_id"))
        result["reject_run_record"] = {"worker_analysis_rejected": reject_record.get("worker_analysis_rejected"),
                                       "worker_judgment": reject_record.get("worker_judgment"),
                                       "report": reject_record.get("report")}
        checks["reject_reason_in_run_record"] = (
            (reject_record.get("worker_analysis_rejected") or {}).get("reason") == reason
            and reject_record.get("worker_judgment") is None and "report" not in reject_record)
        checks["reject_worker_idle"] = flow.worker_view().get("state") == "idle"
        checks["reject_manager_told_once"] = U2.wait_for(lambda: [
            n for n in models["manager"].notices if n.get("notice") == "run_judgment_unavailable"] != [], 30)
        time.sleep(2)
        told = [n for n in models["manager"].notices if n.get("notice") == "run_judgment_unavailable"]
        result["reject_manager_notice"] = told
        checks["reject_manager_notice_once_with_reason"] = (
            len(told) == 1 and told[0].get("reason") == reason and told[0].get("judgment") == "indeterminate"
            and told[0].get("not_judged") == ["worker_analysis"])
        checks["reject_automation_idle"] = U2.wait_for(lambda: state()["state"] == "idle", 20)
        checks["reject_analysis_not_resent"] = [a["corrupt"] for a in models["worker"].analyses] == [True]

        # Stage E: the full experiment path.
        omps["manager"].wait(lambda f: f.get("type") == "agent_end", 30)
        omps["manager"].send({"id": "e", "type": "prompt", "message": "stage-exp"})
        checks["dispatched"] = U2.wait_for(lambda: (flow.task_view() or {}).get("task_id")
                                           not in (None, hold_task.get("task_id"), reject_task.get("task_id"))
                                           and flow.worker_view().get("state") == "busy", 40)
        checks["run_bound"] = U2.wait_for(lambda: (state().get("run") or {}).get("bound") is True, 60)
        run_info = state().get("run") or {}
        result["run"] = run_info
        record = run_record(run_info.get("run_id"))
        checks["execute_task_omp_processed"] = (record.get("worker_request") or {}).get("status") == "omp_processed"
        checks["worker_authorised_execution"] = (record.get("worker_execution_decision") or {}).get(
            "decision") == "execute"
        checks["host_shell_command_sent"] = record.get("shell_state") in ("sent", "exited")
        checks["review_processed"] = U2.wait_for(lambda: state()["review"]["review_count"] >= 1, interval + 40)
        checks["review_reached_worker"] = len(reviews()) >= 1
        result["review_status"] = state()["review"]
        checks["run_reported"] = U2.wait_for(
            lambda: ((flow.task_view() or {}).get("last_result") or {}).get("outcome") == "reported", duration + 90)
        last = (flow.task_view() or {}).get("last_result") or {}
        result["last_result"] = last
        checks["judged_success_and_closed"] = (last.get("judgment"), last.get("run_closed")) == ("success", True)
        final_record = run_record(run_info.get("run_id"))
        result["run_record"] = {k: final_record.get(k) for k in ("shell_state", "exit_status", "exit_confirmed")}
        result["run_record"]["worker_judgment"] = (final_record.get("worker_judgment") or {}).get("judgment")
        # smoke-04 G1: the model-made malformed response_id was ignored; the bridge's own id was recorded.
        recorded_id = ((final_record.get("worker_judgment") or {}).get("worker_response") or {}).get("response_id")
        result["run_record"]["response_id_bridge_generated"] = isinstance(recorded_id, str) and len(recorded_id) == 36
        checks["model_response_id_ignored"] = (isinstance(recorded_id, str) and recorded_id != MALFORMED_UUID
                                               and len(recorded_id) == 36)
        checks["success_analysis_once"] = [a["corrupt"] for a in models["worker"].analyses] == [True, False]
        checks["no_judgment_notice_for_success"] = len(
            [n for n in models["manager"].notices if n.get("notice") == "run_judgment_unavailable"]) == 1
        # The run closes when the manager OMP accepts the report; its provider request follows.
        checks["manager_received_report"] = U2.wait_for(lambda: any(
            item.get("kind") == "report" and item.get("judgment") == "success"
            for item in models["manager"].injected), 30)
        checks["automation_idle_after_run"] = U2.wait_for(lambda: state()["state"] == "idle", 20)
        # Every provider request was Responses-shaped (no chat messages); the identity was in the final input item.
        shapes = models["manager"].shapes + models["worker"].shapes
        result["shapes"] = {"count": len(shapes), "transports": sorted({s["transport"] for s in shapes}),
                            "chained": sum(s["previous_response_id"] for s in shapes),
                            "examples": shapes[:2] + shapes[-2:]}
        checks["responses_shape_only"] = bool(shapes) and not any(s["messages"] for s in shapes)
        checks["transport_as_requested"] = {s["transport"] for s in shapes} == {"websocket" if websocket else "sse"}
        if websocket:
            checks["websocket_delta_used"] = any(s["previous_response_id"] for s in shapes)
        result["provider_requests"] = {role: models[role].requests for role in U1.ROLES}
        with omps["worker"].cond:
            worker_shapes = _assistant_shapes(list(omps["worker"].frames))
        result["worker_assistant_shapes"] = worker_shapes[:3]
        if visible_reasoning:
            # C-D67: each staged worker reply was [thinking with visible text, marker text] and was accepted.
            checks["worker_replies_carry_visible_thinking"] = bool(worker_shapes) and all(
                shape and shape[0]["type"] == "thinking" and shape[0].get("thinking_chars", 0) > 0
                for shape in worker_shapes)
            checks["thinking_text_not_recorded"] = (
                not _files_containing(root, VISIBLE_REASONING.encode())
                and VISIBLE_REASONING not in json.dumps([flow.task_view(), flow.worker_view(), state(), logs],
                                                        default=str))
    finally:
        result["log"] = logs[-30:]
        result["omp_stop"] = {role: process.stop() for role, process in omps.items()}
        bridge.set_tool_handler(None)
        automation.close()
        flow.close()
        handoffs.close()
        bridge.close()
        stop.set()
        loop_thread.join(5)
        result["shell_close"] = pane.close()
        raw.close()
        for server in servers.values():
            server.shutdown()
            server.server_close()
        try:
            U2.git(source, "worktree", "prune")
        except subprocess.CalledProcessError:
            pass
        shutil.rmtree(root, ignore_errors=True)
        result["temp_removed"] = not root.exists()
    result["ok"] = bool(checks) and all(checks.values()) and result["temp_removed"]
    return result


if __name__ == "__main__":
    argv = [arg for arg in sys.argv[1:] if arg not in ("--sse", "--visible-reasoning")]
    outcome = run(argv[0] if argv else shutil.which("omp") or "omp",
                  int(argv[1]) if len(argv) > 1 else 60, "--sse" not in sys.argv[1:],
                  "--visible-reasoning" in sys.argv[1:])
    print(json.dumps(outcome, indent=1, sort_keys=True, default=str))
    sys.exit(0 if outcome["ok"] else 1)
