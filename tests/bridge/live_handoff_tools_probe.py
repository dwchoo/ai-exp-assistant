"""CW-18 U1 live probe: to_worker/to_manager in two real OMPs, no model.

Two OMP processes (RPC mode, the product bridge extension) run in a fake HOME
with a fake OMP home (PI_CODING_AGENT_DIR/PI_CONFIG_DIR under a /tmp directory)
whose only model is a scripted OpenAI-compatible server on 127.0.0.1; every
other HTTP(S) request goes to a closed proxy port. No credentials exist in the
fake home, so no real provider can be reached. The scripted server decides the
"model" turns: it calls the handoff tools and records the tool results and the
injected Workbench messages it receives (ids and payload fields only).

Stages:
  A  registration: each role's dumpTools and provider tool list; a call with
     no active Task (manager to_worker -> approval_pending, worker to_manager
     -> rejected:no_active_task); the other role's tool is not callable.
  C  round trip: with an approved active Task and run, manager to_worker is
     queued, delivered to the worker OMP through TaskMailbox; the worker's
     scripted turn answers with to_manager, which is delivered to the manager.

Usage: PYTHONPATH=src python tests/bridge/live_handoff_tools_probe.py [omp]
Prints one JSON object; exit 0 only when every check holds.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from workbench.backend.flow import ActiveTask, HandoffService  # noqa: E402
from workbench.contracts.v1 import ActorRole, MessageKind  # noqa: E402
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, TaskMailbox  # noqa: E402
from workbench.tasks.repository import TaskRepository  # noqa: E402

EXTENSION = ROOT / "omp_bridge/g3/bridge.ts"
BLOCKED_PROXY = "http://127.0.0.1:9"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
ROLES = ("manager", "worker")
OWN_TOOL = {"manager": "to_worker", "worker": "to_manager"}
OTHER_TOOL = {"manager": "to_manager", "worker": "to_worker"}


def _text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [block.get("text") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)]
    return []


class ScriptedModel:
    """The scripted 'model' of one role; records only ids, statuses and payload fields."""

    def __init__(self, role: str):
        self.role = role
        self.requests = 0
        self.tool_lists: list[list[str]] = []
        self.tool_results: dict[str, Any] = {}
        self.injected: list[dict[str, Any]] = []
        self.prompt_shapes: list[dict[str, Any]] = []
        self.calls = 0
        self.lock = threading.Lock()

    def _call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
        self.calls += 1
        return name, {"i": "scripted probe intent", **args}, f"{self.role}-call-{self.calls}"

    def respond(self, request: dict[str, Any]) -> list[tuple[str, dict[str, Any], str]] | None:
        with self.lock:
            self.requests += 1
            tools = sorted(t.get("function", {}).get("name", "") for t in request.get("tools") or [])
            self.tool_lists.append(tools)
            messages = request.get("messages") or []
            last = messages[-1] if messages else {}
            if last.get("role") == "tool":
                for message in reversed(messages):
                    if message.get("role") != "tool":
                        break
                    text = "".join(_text_blocks(message.get("content")))
                    try:
                        value = json.loads(text)
                    except ValueError:
                        value = {"non_json": text[:160]}
                    self.tool_results[str(message.get("tool_call_id"))] = value
                return None
            blocks = _text_blocks(last.get("content"))
            # OMP may prepend a context block to the first prompt; the prompt itself is the last block.
            text = blocks[-1].strip() if blocks else ""
            try:
                injected = json.loads(text)
            except ValueError:
                injected = None
            if isinstance(injected, dict) and isinstance(injected.get("workbench_message_id"), str):
                payload = injected.get("payload") if isinstance(injected.get("payload"), dict) else {}
                self.injected.append({"kind": injected.get("kind"), "message_id": injected["workbench_message_id"],
                                      "in_reply_to": injected.get("in_reply_to_message_id"),
                                      "payload": payload, "has_response_contract": "response_contract" in injected})
                if self.role == "worker" and payload.get("handoff") == "to_worker":
                    return [self._call("to_manager", {"kind": "report", "message": "worker probe report",
                                                      "requires_code_change": False})]
                return None
            self.prompt_shapes.append({"blocks": len(blocks), "last_block": text[:16]})
            if text == "stage-a":
                own = ({"kind": "work", "message": "probe instruction A"} if self.role == "manager"
                       else {"kind": "progress", "message": "probe progress A"})
                other = ({"kind": "done", "message": "not mine"} if self.role == "manager"
                         else {"kind": "work", "message": "not mine"})
                return [self._call(OWN_TOOL[self.role], own), self._call(OTHER_TOOL[self.role], other)]
            if text == "stage-c" and self.role == "manager":
                return [self._call("to_worker", {"kind": "work", "message": "probe instruction C"})]
            return None


def _server(model: ScriptedModel) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                calls = model.respond(json.loads(raw))
            except ValueError:
                calls = None
            if calls:
                delta = {"role": "assistant", "tool_calls": [
                    {"index": index, "id": call_id, "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}
                    for index, (name, args, call_id) in enumerate(calls)]}
                finish = "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": "ok"}, "stop"
            frames = [
                {"id": "p", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "p", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            body = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


class RpcOmp:
    def __init__(self, omp: str, role: str, token: str, root: Path, socket_path: Path, port: int):
        home = root / f"home-{role}"
        agent, config, cwd = home / "agent", home / "config", root / f"cwd-{role}"
        for directory in (home, agent, config, cwd):
            directory.mkdir(parents=True)
        (agent / "models.yml").write_text(
            "providers:\n  wbprobe:\n"
            f"    baseUrl: http://127.0.0.1:{port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
        overlay = root / "overlay.yml"
        if not overlay.exists():
            overlay.write_text("startup:\n  setupWizard: false\n")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TERM": "dumb", "LANG": "C.UTF-8",
               "PI_CODING_AGENT_DIR": str(agent), "PI_CONFIG_DIR": str(config),
               "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
               "XDG_STATE_HOME": str(home / ".local/state"), "XDG_CACHE_HOME": str(home / ".cache"),
               **{key: BLOCKED_PROXY for key in PROXY_KEYS}, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
               "WORKBENCH_G3_BRIDGE_SOCKET": str(socket_path), "WORKBENCH_G3_ROLE": role,
               "WORKBENCH_G3_TOKEN": token, "WORKBENCH_G3_GENERATION": "1"}
        argv = [omp, "--mode", "rpc", "--no-session", "--no-title", "--no-extensions", "--no-skills", "--no-rules",
                "--config", str(overlay), "--extension", str(EXTENSION), "--model", "wbprobe/scripted",
                "--cwd", str(cwd)]
        self.role = role
        self.stderr = (root / f"stderr-{role}.log").open("wb")
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, start_new_session=True)
        self.frames: list[dict[str, Any]] = []
        self.cond = threading.Condition()
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        buffer = b""
        fd = self.process.stdout.fileno()
        while True:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                if self.process.poll() is not None:
                    return
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    with self.cond:
                        self.frames.append(frame)
                        self.cond.notify_all()

    def send(self, frame: dict[str, Any]) -> None:
        self.process.stdin.write((json.dumps(frame) + "\n").encode())
        self.process.stdin.flush()

    def wait(self, predicate, timeout: float, after: int = 0) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for frame in self.frames[after:]:
                    if predicate(frame):
                        return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cond.wait(remaining)

    def tool_ends(self) -> dict[str, dict[str, Any]]:
        with self.cond:
            ends = [f for f in self.frames if f.get("type") == "tool_execution_end"]
        result = {}
        for frame in ends:
            content = (frame.get("result") or {}).get("content") or []
            text = "".join(_text_blocks(content))
            result[str(frame.get("toolCallId"))] = {"tool": frame.get("toolName"), "isError": frame.get("isError"),
                                                    "text": text[:200]}
        return result

    def stop(self) -> dict[str, Any]:
        outcome = {"pid": self.process.pid}
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(5)
        outcome["returncode"] = self.process.returncode
        self.stderr.close()
        return outcome


def run(omp: str) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": "two-real-omp-rpc-scripted-provider", "omp": None, "checks": {}}
    checks = result["checks"]
    version = subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=30,
                             env={"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent"})
    result["omp"] = (version.stdout or version.stderr).strip().splitlines()[0]
    root = Path(tempfile.mkdtemp(prefix="wb-p27-cw18-u1-live-", dir="/tmp"))
    models = {role: ScriptedModel(role) for role in ROLES}
    servers = {role: _server(models[role]) for role in ROLES}
    for server in servers.values():
        threading.Thread(target=server.serve_forever, daemon=True).start()
    tokens = {role: str(uuid4()) for role in ROLES}
    socket_path = root / "bridge.sock"
    bridge = G3BridgeServer(socket_path, tokens)
    bridge.start()
    repository = TaskRepository(root / "tasks.sqlite3")
    mailbox = TaskMailbox(repository, bridge)
    active: dict[str, ActiveTask | None] = {"task": None}

    def outbox_mailbox():
        own = TaskRepository(root / "tasks.sqlite3")
        return TaskMailbox(own, bridge), own.close

    handoffs = HandoffService(root / "workflow-handoffs.jsonl", mailbox_factory=outbox_mailbox,
                              active_task=lambda: active["task"], peer_lookup=lambda role: bridge.peer(role, 0),
                              retry_interval=0.2)
    handoffs.start()
    bridge.set_tool_handler(lambda peer, request: handoffs.handle(peer.role, request))
    omps: dict[str, RpcOmp] = {}
    try:
        for role in ROLES:
            omps[role] = RpcOmp(omp, role, tokens[role], root, socket_path, servers[role].server_port)
        for role in ROLES:
            omps[role].send({"id": f"state-{role}", "type": "get_state"})
        states = {role: omps[role].wait(lambda f, r=role: f.get("type") == "response"
                                        and f.get("id") == f"state-{r}", 40) for role in ROLES}
        peers = {role: bridge.peer(role, timeout=20) for role in ROLES}
        result["pids_distinct"] = len({peer.pid for peer in peers.values()}) == 2
        dump = {role: sorted(t.get("name") for t in ((states[role] or {}).get("data") or {}).get("dumpTools") or []
                             if t.get("name") in ("to_worker", "to_manager")) for role in ROLES}
        result["dump_tools"] = dump
        checks["registered_per_role"] = dump == {"manager": ["to_worker"], "worker": ["to_manager"]}

        # Stage A: no active Task.
        for role in ROLES:
            cursor = len(omps[role].frames)
            omps[role].send({"id": f"a-{role}", "type": "prompt", "message": "stage-a"})
            omps[role].wait(lambda f: f.get("type") == "agent_end", 40, after=cursor)
        result["provider_tools"] = {role: sorted({name for names in models[role].tool_lists for name in names
                                                  if name in ("to_worker", "to_manager")}) for role in ROLES}
        checks["provider_receives_only_own_tool"] = result["provider_tools"] == {
            "manager": ["to_worker"], "worker": ["to_manager"]}
        ends = {role: omps[role].tool_ends() for role in ROLES}
        result["stage_a_tool_ends"] = ends
        result["stage_a_results_seen_by_model"] = {role: dict(models[role].tool_results) for role in ROLES}
        manager_own = models["manager"].tool_results.get("manager-call-1", {})
        worker_own = models["worker"].tool_results.get("worker-call-1", {})
        checks["manager_to_worker_approval_pending"] = manager_own.get("status") == "approval_pending"
        checks["worker_to_manager_rejected_no_active_task"] = worker_own == {"status": "rejected",
                                                                             "reason": "no_active_task"}
        checks["other_role_tool_not_callable"] = all(
            ends[role].get(f"{role}-call-2", {}).get("isError") is True for role in ROLES)
        checks["stage_a_nothing_delivered"] = models["manager"].injected == [] and models["worker"].injected == []
        checks["approval_stub_recorded"] = len(handoffs.pending_approvals()) == 1

        # Stage C: approved active Task with a run.
        task_id = repository.create_task({"goal": "CW-18 U1 live probe", "allowed_changes": []})
        repository.approve_scope(task_id, 1, {"paths": [], "commands": []})
        repository.proceed(task_id, 1, "CW-18 U1 live probe")
        run_id = repository.start_run(task_id, 1)
        task_message = mailbox.create_message(task_id, 1, run_id, "manager", "worker", MessageKind.TASK,
                                              {"note": "probe task message (not delivered)"})
        active["task"] = ActiveTask(task_id, 1, "work", True, run_id, task_message.message_id)
        cursor = len(omps["manager"].frames)
        omps["manager"].send({"id": "c-manager", "type": "prompt", "message": "stage-c"})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not any(
                item["payload"].get("handoff") == "to_manager" for item in models["manager"].injected):
            time.sleep(0.1)
        time.sleep(1.0)
        manager_c = models["manager"].tool_results.get("manager-call-3", {})
        worker_report = next((value for key, value in models["worker"].tool_results.items()
                              if key != "worker-call-1" and key != "worker-call-2"), {})
        result["stage_c"] = {
            "manager_to_worker_result": manager_c,
            "worker_injected": models["worker"].injected,
            "worker_to_manager_result": worker_report,
            "manager_injected": models["manager"].injected,
            "outbox": handoffs.outbox_snapshot(),
        }
        worker_injected = models["worker"].injected
        manager_injected = models["manager"].injected
        checks["manager_to_worker_queued"] = manager_c.get("status") == "queued"
        checks["worker_received_question"] = (len(worker_injected) == 1 and worker_injected[0]["kind"] == "question"
                                              and worker_injected[0]["payload"] == {
                                                  "handoff": "to_worker", "kind": "work",
                                                  "message": "probe instruction C"}
                                              and worker_injected[0]["has_response_contract"] is False)
        checks["worker_to_manager_queued"] = worker_report.get("status") == "queued"
        checks["manager_received_report"] = (len(manager_injected) == 1 and manager_injected[0]["kind"] == "report"
                                             and manager_injected[0]["in_reply_to"] == task_message.message_id
                                             and manager_injected[0]["payload"].get("message")
                                             == "worker probe report")
        checks["outbox_delivered_once_each"] = [e["state"] for e in handoffs.outbox_snapshot()] == [
            "delivered", "delivered"]
        omps["manager"].wait(lambda f: f.get("type") == "agent_end", 20, after=cursor)
        result["provider_requests"] = {role: models[role].requests for role in ROLES}
        result["prompt_shapes"] = {role: models[role].prompt_shapes for role in ROLES}
        journal = [json.loads(line) for line in (root / "workflow-handoffs.jsonl").read_text().splitlines()]
        result["journal_types"] = {kind: sum(1 for r in journal if r["type"] == kind)
                                   for kind in sorted({r["type"] for r in journal})}
        checks["journal_has_every_request_and_result"] = (result["journal_types"].get("request")
                                                          == result["journal_types"].get("result") == 4)
    finally:
        result["omp_stop"] = {role: process.stop() for role, process in omps.items()}
        bridge.set_tool_handler(None)
        bridge.close()
        handoffs.close()
        repository.close()
        for server in servers.values():
            server.shutdown()
            server.server_close()
        shutil.rmtree(root, ignore_errors=True)
        result["temp_removed"] = not root.exists()
    result["ok"] = bool(checks) and all(checks.values()) and result["temp_removed"]
    return result


if __name__ == "__main__":
    outcome = run(sys.argv[1] if len(sys.argv) > 1 else shutil.which("omp") or "omp")
    print(json.dumps(outcome, indent=1, sort_keys=True))
    sys.exit(0 if outcome["ok"] else 1)
