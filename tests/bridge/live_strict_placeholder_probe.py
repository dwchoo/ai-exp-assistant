"""CW-18 smoke D1/D2 regression with two real OMPs and a scripted provider, no model (p27-cw18-smoke-fix-01).

The real-model smoke (openai-codex, GPT-5.5) showed the manager filling every optional ``to_worker`` field
(``task_id``, ``run``, ``cancel``, a placeholder ``spec.execution`` on a work Task) and the worker filling every
``to_manager`` field (``requires_code_change``, ``reason``, ``request: {goal: "none", paths: []}``). This probe
replays that shape through the product bridge extension (registered with ``strict: true`` and nullable
optional fields), OMP's own argument validation, the authenticated G3 bridge, ``HandoffService`` and the real
``TaskFlow`` (TaskRepository, TaskMailbox lanes):

  1. manager: a work Task with the smoke's real-looking placeholder execution -> ``rejected:invalid_arguments``
     whose error names ``spec.execution`` and the expected null (nothing dispatched);
  2. manager: the same Task the strict way (every unused field null, one absolute path inside the project) ->
     ``dispatched``; the worker's TASK carries repo-relative paths and no execution;
  3. worker: ``to_manager done`` with null/false/blank placeholders and the smoke's empty request -> ``queued``;
     the report the manager receives has no ``request``, ``requires_code_change`` or ``reason``.

Isolation as in ``live_handoff_tools_probe``: fake HOME and OMP home under one /tmp root, the only provider is
a scripted OpenAI-compatible server on 127.0.0.1, outbound HTTP(S) goes to a closed proxy port.

Usage: PYTHONPATH=src python tests/bridge/live_strict_placeholder_probe.py [omp]
Prints one JSON object; exit 0 only when every check holds.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from workbench.backend.flow import HandoffService  # noqa: E402
from workbench.backend.flow_tasks import TaskFlow  # noqa: E402
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, MailboxError, TaskMailbox  # noqa: E402
from workbench.tasks.repository import TaskRepository  # noqa: E402


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


U1 = _load(ROOT / "tests/bridge/live_handoff_tools_probe.py", "cw18_smoke_fix_u1")
SMOKE_EXECUTION = {"source": "unused", "commit": "unused", "command": "true",
                   "criteria": {"log_contains": "unused", "result_file": "unused", "result_contains": "unused"},
                   "environment": [], "shell": "bash"}


class Model:
    """The scripted 'model' of one role (smoke-shaped calls); records ids, statuses and payload fields only."""

    def __init__(self, role: str, project: Path):
        self.role, self.project = role, project
        self.requests = 0
        self.calls = 0
        self.lock = threading.Lock()
        self.results: dict[str, Any] = {}
        self.injected: list[dict[str, Any]] = []
        self.tool_wire: dict[str, Any] = {}

    def _call(self, name: str, args: dict[str, Any]) -> list[tuple[str, dict[str, Any], str]]:
        self.calls += 1
        return [(name, {"i": "scripted smoke-fix intent", **args}, f"{self.role}-sf-{self.calls}")]

    def strict_work(self, execution: Any, paths: list[str]) -> dict[str, Any]:
        return {"task_id": None, "kind": "work", "message": "Create work/hello.txt containing hello from worker.",
                "spec": {"goal": "create hello.txt", "paths": paths, "instructions": None, "execution": execution},
                "run": None, "cancel": None}

    def respond(self, request: dict[str, Any]) -> list[tuple[str, dict[str, Any], str]] | None:
        with self.lock:
            self.requests += 1
            for tool in request.get("tools") or []:
                function = tool.get("function") or {}
                if function.get("name") in ("to_worker", "to_manager") and function["name"] not in self.tool_wire:
                    parameters = function.get("parameters") or {}
                    self.tool_wire[function["name"]] = {"strict": function.get("strict"),
                                                        "required": sorted(parameters.get("required") or [])}
            messages = request.get("messages") or []
            last = messages[-1] if messages else {}
            if last.get("role") == "tool":
                body = "".join(U1._text_blocks(last.get("content")))
                try:
                    value = json.loads(body)
                except ValueError:
                    value = {"non_json": body[:300]}
                call_id = str(last.get("tool_call_id"))
                self.results[call_id] = value
                if self.role == "manager" and call_id == "manager-sf-1":
                    return self._call("to_worker", self.strict_work(
                        None, [str(self.project / "work" / "hello.txt"), "./work/notes/"]))
                return None
            blocks = U1._text_blocks(last.get("content"))
            text = blocks[-1].strip() if blocks else ""
            try:
                injected = json.loads(text)
            except ValueError:
                injected = None
            if isinstance(injected, dict) and isinstance(injected.get("workbench_message_id"), str):
                payload = injected.get("payload") if isinstance(injected.get("payload"), dict) else {}
                self.injected.append({"kind": injected.get("kind"), "payload": payload,
                                      "message_id": injected["workbench_message_id"]})
                if self.role == "worker" and injected.get("kind") == "task":
                    return self._call("to_manager", {
                        "kind": "done", "message": "wrote work/hello.txt", "task_id": payload.get("task_id"),
                        "in_reply_to": None, "requires_code_change": False, "reason": "",
                        "request": {"goal": "none", "paths": []}})
                return None
            if self.role == "manager" and text == "smoke-work":
                return self._call("to_worker", self.strict_work(SMOKE_EXECUTION, ["work/hello.txt"]))
            return None


def wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


def run(omp: str) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": "two-real-omp-rpc-scripted-provider", "checks": {}}
    checks = result["checks"]
    version = subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=30,
                             env={"PATH": U1.os.environ.get("PATH", ""), "HOME": "/nonexistent"})
    result["omp"] = (version.stdout or version.stderr).strip().splitlines()[0]
    root = Path(tempfile.mkdtemp(prefix="wb-p27-cw18-smoke-fix-live-", dir="/tmp"))
    project = root / "project"
    project.mkdir()
    models = {role: Model(role, project) for role in U1.ROLES}
    servers = {role: U1._server(models[role]) for role in U1.ROLES}
    for server in servers.values():
        threading.Thread(target=server.serve_forever, daemon=True).start()
    tokens = {role: str(uuid4()) for role in U1.ROLES}
    socket_path = root / "bridge.sock"
    bridge = G3BridgeServer(socket_path, tokens)
    bridge.start()
    (root / "workflow").mkdir(mode=0o700)
    db = root / "tasks.sqlite3"
    TaskRepository(db).close()

    def lane_mailbox():
        own = TaskRepository(db)
        return TaskMailbox(own, bridge), own.close

    def omp_idle(role):
        try:
            return bridge.probe(role, timeout=1.0).get("idle") is True
        except (MailboxError, OSError, TimeoutError):
            return None

    handoffs = HandoffService(root / "workflow" / "handoffs.jsonl", mailbox_factory=lane_mailbox,
                              peer_lookup=lambda role: bridge.peer(role, 0), retry_interval=0.2)
    flow = TaskFlow(root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(db),
                    handoffs=handoffs, omp_idle=omp_idle, project_dir=project)
    handoffs.configure(policy=flow, active_task=flow.active_task)
    handoffs.start()
    flow.start()
    bridge.set_tool_handler(lambda peer, request: handoffs.handle(peer.role, request))
    omps: dict[str, Any] = {}
    try:
        for role in U1.ROLES:
            omps[role] = U1.RpcOmp(omp, role, tokens[role], root, socket_path, servers[role].server_port)
        peers = {role: bridge.peer(role, timeout=40) for role in U1.ROLES}
        checks["both_omps_connected"] = len({peer.pid for peer in peers.values()}) == 2
        omps["manager"].send({"id": "smoke", "type": "prompt", "message": "smoke-work"})
        manager, worker = models["manager"], models["worker"]
        checks["manager_received_report"] = wait_for(
            lambda: any(item["kind"] == "report" for item in manager.injected), 90)
        first = manager.results.get("manager-sf-1", {})
        second = manager.results.get("manager-sf-2", {})
        result["first_result"] = first
        result["second_result"] = second
        errors = " | ".join(first.get("errors") or [])
        checks["placeholder_execution_rejected_naming_the_field"] = (
            first.get("status") == "rejected" and first.get("reason") == "invalid_arguments"
            and "spec.execution" in errors and "null" in errors and "work" in errors)
        checks["strict_null_work_task_dispatched"] = (second.get("status"), second.get("kind")) == (
            "dispatched", "work")
        task = next((item for item in worker.injected if item["kind"] == "task"), None)
        result["worker_task_payload_keys"] = sorted((task or {}).get("payload", {}))
        checks["worker_task_paths_repo_relative_no_execution"] = task is not None and (
            task["payload"].get("paths") == ["work/hello.txt", "work/notes/"]
            and "execution" not in task["payload"])
        worker_result = next(iter(worker.results.values()), {})
        result["worker_result"] = worker_result
        checks["worker_placeholder_report_queued"] = worker_result.get("status") == "queued"
        report = next((item for item in manager.injected if item["kind"] == "report"), {"payload": {}})
        result["report_payload"] = report["payload"]
        checks["report_has_no_placeholder_fields"] = (
            report["payload"].get("message") == "wrote work/hello.txt"
            and not {"request", "requires_code_change", "reason"} & set(report["payload"]))
        checks["one_task_created"] = len(flow.tasks) == 1
        result["provider_tool_wire"] = {role: models[role].tool_wire for role in U1.ROLES}
        result["provider_requests"] = {role: models[role].requests for role in U1.ROLES}
        omps["manager"].wait(lambda f: f.get("type") == "agent_end", 20)
    finally:
        result["omp_stop"] = {role: process.stop() for role, process in omps.items()}
        bridge.set_tool_handler(None)
        flow.close()
        handoffs.close()
        bridge.close()
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
