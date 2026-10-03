"""CW-18 U2b live probe (C-D66): immediate dispatch, worker_busy and Task runs with two real OMPs, no model.

Two OMP processes (RPC mode, the product bridge extension) run in a fake HOME
with a fake OMP home whose only model is a scripted OpenAI-compatible server on
127.0.0.1 (every other HTTP(S) request goes to a closed proxy port; no
credentials exist). The backend parts are the product ones: G3BridgeServer,
HandoffService with the TaskFlow policy, a real host ShellPane (pumped by a
loop thread like the backend loop), TaskWorkflow with TaskMailbox and
G3WorkerResponsePort, and a UiServer whose pause/resume handlers are the
Backend's own methods; the ui_v1 snapshot carries ``task`` and ``worker``.
There is no UI approval (the user's standing delegation, C-D66).

Stages:
  W  free work: manager to_worker -> dispatched at once -> the Task message
     reaches the worker (which stays on it) -> a second manager to_worker is
     answered worker_busy with the current Task -> the worker's to_manager done
     reaches the manager -> the run is completed, the Task closed, the worker
     idle -> the next manager to_worker is dispatched.
  E  experiment twice on the same host shell: manager to_worker with an
     execution -> dispatched -> worker execute decision (staged marker) -> the
     command runs in the host shell -> collect -> worker analysis -> report to
     the manager -> finished; the shell is given back; then the manager's
     re-run (task_id, run: true) runs again in the same host shell.

Usage: PYTHONPATH=src python tests/backend/live_task_flow_probe.py [omp]
Prints one JSON object; exit 0 only when every check holds.
"""

from __future__ import annotations

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

from workbench.backend.client import UiClient  # noqa: E402
from workbench.backend.flow import HandoffService  # noqa: E402
from workbench.backend.flow_tasks import ExperimentPorts, TaskFlow  # noqa: E402
from workbench.backend.panes import HostShellPort, ShellPane  # noqa: E402
from workbench.backend.service import Backend  # noqa: E402
from workbench.backend.ui_server import UiServer  # noqa: E402
from workbench.contracts.v1 import ActorRole  # noqa: E402
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, MailboxError, TaskMailbox  # noqa: E402
from workbench.tasks.repository import TaskRepository  # noqa: E402
from workbench.terminal.shell_g2.prototype import ShellChoice  # noqa: E402
from workbench.workflow import G3WorkerResponsePort, TaskWorkflow  # noqa: E402


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


U1 = _load(ROOT / "tests/bridge/live_handoff_tools_probe.py", "cw18_u1_live_probe")
CW10 = _load(ROOT / "tests/workflow/live_workflow_probe.py", "cw10_live_workflow_probe")
AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}


class Model:
    """The scripted 'model' of one role: tool calls or a staged marker; records ids and payload fields only."""

    def __init__(self, role: str, experiment_spec: dict[str, Any]):
        self.role, self.experiment_spec = role, experiment_spec
        self.requests = 0
        self.tool_results: dict[str, Any] = {}
        self.injected: list[dict[str, Any]] = []
        self.calls = 0
        self.lock = threading.Lock()
        self.experiment_task_id: str | None = None  # learnt from the manager's first experiment tool result

    def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"tool_calls": [(name, {"i": "scripted probe intent", **args}, f"{self.role}-call-{self.calls}")]}

    def respond(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.requests += 1
            messages = request.get("messages") or []
            last = messages[-1] if messages else {}
            if last.get("role") == "tool":
                for message in reversed(messages):
                    if message.get("role") != "tool":
                        break
                    text = "".join(U1._text_blocks(message.get("content")))
                    try:
                        self.tool_results[str(message.get("tool_call_id"))] = json.loads(text)
                    except ValueError:
                        self.tool_results[str(message.get("tool_call_id"))] = {"non_json": text[:160]}
                return {"content": "ok"}
            blocks = U1._text_blocks(last.get("content"))
            text = blocks[-1].strip() if blocks else ""
            try:
                injected = json.loads(text)
            except ValueError:
                injected = None
            if isinstance(injected, dict) and isinstance(injected.get("workbench_message_id"), str):
                payload = injected.get("payload") if isinstance(injected.get("payload"), dict) else {}
                self.injected.append({"kind": injected.get("kind"), "message_id": injected["workbench_message_id"],
                                      "in_reply_to": injected.get("in_reply_to_message_id"),
                                      "run_id": injected.get("run_id"),
                                      "payload_keys": sorted(payload), "handoff": payload.get("handoff"),
                                      "payload_kind": payload.get("kind"), "stage": payload.get("stage"),
                                      "judgment": payload.get("judgment"),
                                      "has_response_contract": "response_contract" in injected})
                if self.role == "worker" and "response_contract" in injected:
                    stage = payload.get("stage")
                    if stage == "execute":
                        decision = "execute"
                    else:
                        facts = payload.get("facts") or {}
                        ok = (facts.get("exit_status") == 0 and "PASS" in (facts.get("raw_log_excerpt") or "")
                              and "PASS" in (facts.get("result_excerpt") or ""))
                        decision = "success" if ok else "failure"
                    frame = CW10._frame_from_contract(injected, decision)
                    return {"content": frame or "invalid"}
                if (self.role == "worker" and payload.get("handoff") == "to_worker" and payload.get("kind") == "work"
                        and payload.get("message") == "probe next work"):
                    return self._call("to_manager", {"kind": "done", "message": "probe next work done"})
                return {"content": "ok"}  # the first free-work Task stays on the worker until "report-done"
            if self.role == "worker" and text == "report-done":
                return self._call("to_manager", {"kind": "done", "message": "probe free work done"})
            if self.role == "manager" and text == "stage-work":
                return self._call("to_worker", {"kind": "work", "message": "probe free work",
                                                 "spec": {"goal": "probe free work", "paths": ["notes/"]}})
            if self.role == "manager" and text == "stage-work-busy":
                return self._call("to_worker", {"kind": "work", "message": "probe second task",
                                                 "spec": {"goal": "probe second task", "paths": ["notes/"]}})
            if self.role == "manager" and text == "stage-work-next":
                return self._call("to_worker", {"kind": "work", "message": "probe next work",
                                                 "spec": {"goal": "probe next work", "paths": ["notes/"]}})
            if self.role == "manager" and text == "stage-exp":
                return self._call("to_worker", {"kind": "experiment", "message": "probe experiment",
                                                 "spec": self.experiment_spec})
            if self.role == "manager" and text == "stage-exp-rerun" and self.experiment_task_id:
                return self._call("to_worker", {"kind": "experiment", "message": "probe experiment again",
                                                 "task_id": self.experiment_task_id, "run": True})
            return {"content": "ok"}


def _server(model: Model) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                answer = model.respond(json.loads(raw))
            except ValueError:
                answer = {"content": "ok"}
            if "tool_calls" in answer:
                delta = {"role": "assistant", "tool_calls": [
                    {"index": index, "id": call_id, "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}
                    for index, (name, args, call_id) in enumerate(answer["tool_calls"])]}
                finish = "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": answer["content"]}, "stop"
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


class Controller:
    """ui_v1 controller: the Backend's own pause/resume methods and the Task/worker view of the probe's TaskFlow."""

    pause = Backend.pause
    resume = Backend.resume
    _default_pause = Backend._default_pause
    _default_resume = Backend._default_resume

    def __init__(self, flow: TaskFlow):
        self.flow = flow
        self.automation = {"state": "idle"}
        self.pause_hook, self.resume_hook = self._default_pause, self._default_resume

    def snapshot(self) -> dict[str, Any]:
        return {"task": self.flow.task_view(), "worker": self.flow.worker_view(),
                "automation": dict(self.automation)}

    def replay(self) -> list:
        return []

    def on_attach(self, size: Any) -> None:
        pass

    def on_detach(self) -> None:
        pass


def wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=15,
                          check=True).stdout.strip()


def run(omp: str) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": "two-real-omp-rpc-scripted-provider+host-shellpane", "checks": {}}
    checks = result["checks"]
    version = subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=30,
                             env={"PATH": U1.os.environ.get("PATH", ""), "HOME": "/nonexistent"})
    result["omp"] = (version.stdout or version.stderr).strip().splitlines()[0]
    root = Path(tempfile.mkdtemp(prefix="wb-p27-cw18-u2-live-", dir="/tmp"))
    source = root / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.email", "probe@example.invalid")
    git(source, "config", "user.name", "CW18 U2 probe")
    (source / "run.sh").write_text("#!/bin/sh\nprintf 'PASS from the host shell\\n'\nsleep 1\nprintf PASS > outcome.txt\n")
    (source / "run.sh").chmod(0o755)
    git(source, "add", "run.sh")
    git(source, "commit", "-qm", "probe")
    commit = git(source, "rev-parse", "HEAD")
    experiment_spec = {"goal": "probe experiment", "paths": ["outcome.txt"], "execution": {
        "source": str(source), "commit": commit, "command": "./run.sh",
        "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
        "environment": ["PATH"], "shell": "bash"}}
    for name in ("workflow", "worktrees", "runs", "shell-home"):
        (root / name).mkdir(mode=0o700)
    models = {role: Model(role, experiment_spec) for role in U1.ROLES}
    servers = {role: _server(models[role]) for role in U1.ROLES}
    for server in servers.values():
        threading.Thread(target=server.serve_forever, daemon=True).start()
    tokens = {role: str(uuid4()) for role in U1.ROLES}
    socket_path = root / "bridge.sock"
    bridge = G3BridgeServer(socket_path, tokens)
    bridge.start()
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

    shell_env = {"PATH": U1.os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root / "shell-home"),
                 "LANG": "C.UTF-8", "TERM": "xterm-256color"}
    pane = ShellPane(ShellChoice("bash", shutil.which("bash") or "/usr/bin/bash"), dict(shell_env))
    ui_bytes = bytearray()
    stop = threading.Event()

    def backend_loop():
        while not stop.is_set():
            for chunk in pane.pump():
                ui_bytes.extend(chunk.data)
            time.sleep(0.01)

    loop = threading.Thread(target=backend_loop, daemon=True)
    loop.start()
    controller_ref: dict[str, Controller] = {}
    handoffs = HandoffService(root / "workflow" / "handoffs.jsonl", mailbox_factory=lane_mailbox,
                              paused=lambda: controller_ref["c"].automation.get("state") == "paused",
                              peer_lookup=lambda role: bridge.peer(role, 0), retry_interval=0.2)
    ports = ExperimentPorts(
        host_shell=lambda: HostShellPort(pane, lambda: pane),
        make_workflow=lambda repository: TaskWorkflow(repository, TaskMailbox(repository, bridge),
                                                      worker_port=G3WorkerResponsePort(bridge),
                                                      automation_source=lambda: AUTOMATION),
        automation=lambda: AUTOMATION, environment_names=lambda: set(shell_env),
        worktrees_root=root / "worktrees", artifacts_root=root / "runs")
    flow = TaskFlow(root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(db),
                    handoffs=handoffs, omp_idle=omp_idle,
                    paused=lambda: controller_ref["c"].automation.get("state") == "paused", experiment=ports)
    controller_ref["c"] = Controller(flow)
    handoffs.configure(policy=flow, active_task=flow.active_task)
    handoffs.start()
    flow.start()
    bridge.set_tool_handler(lambda peer, request: handoffs.handle(peer.role, request))
    ui_socket = root / "ui.sock"
    ui = UiServer(ui_socket, controller_ref["c"])
    ui_stop = threading.Event()

    def serve_ui():
        while not ui_stop.is_set():
            ui.poll(0.02)

    ui_thread = threading.Thread(target=serve_ui, daemon=True)
    ui_thread.start()
    client = UiClient(ui_socket, timeout=15)
    client.attach()
    omps: dict[str, Any] = {}
    try:
        for role in U1.ROLES:
            omps[role] = U1.RpcOmp(omp, role, tokens[role], root, socket_path, servers[role].server_port)
        peers = {role: bridge.peer(role, timeout=40) for role in U1.ROLES}
        result["pids_distinct"] = len({peer.pid for peer in peers.values()}) == 2
        checks["both_omps_connected"] = result["pids_distinct"]

        def tool_result(role: str, call: str) -> dict[str, Any]:
            return models[role].tool_results.get(call, {})

        def manager_turn(message: str, call: str) -> dict[str, Any]:
            wait_for(lambda: omp_idle("manager") is True, 30)  # a prompt is sent only between turns
            omps["manager"].send({"id": call, "type": "prompt", "message": message})
            wait_for(lambda: bool(tool_result("manager", call)), 40)
            return tool_result("manager", call)

        # Stage W: free work, one Task at a time.
        omps["manager"].send({"id": "w", "type": "prompt", "message": "stage-work"})
        checks["w_dispatched"] = wait_for(lambda: tool_result("manager", "manager-call-1").get("status")
                                          == "dispatched", 40)
        w_first = tool_result("manager", "manager-call-1")
        result["w_to_worker_result"] = w_first
        checks["w_worker_got_task"] = wait_for(lambda: any(
            item["kind"] == "task" and item["handoff"] == "to_worker" and not item["has_response_contract"]
            for item in models["worker"].injected), 40)
        snapshot = client.snapshot()
        result["w_ui_task"], result["w_ui_worker"] = snapshot.get("task"), snapshot.get("worker")
        checks["w_ui_worker_busy"] = (snapshot.get("worker") == {"state": "busy", "task_id": w_first.get("task_id")}
                                      and "approvals" not in snapshot
                                      and (snapshot.get("task") or {}).get("status") == "running")
        busy = manager_turn("stage-work-busy", "manager-call-2")
        result["w_busy_result"] = busy
        checks["w_second_to_worker_worker_busy"] = (
            busy.get("status") == "worker_busy" and (busy.get("task") or {}).get("task_id") == w_first.get("task_id")
            and (busy.get("task") or {}).get("summary") == "probe free work"
            and {"task_id", "kind", "summary", "status", "since"} <= set(busy.get("task") or {}))
        time.sleep(0.5)
        checks["w_busy_queued_nothing"] = (len(flow.tasks) == 1 and sum(
            1 for item in models["worker"].injected if item["kind"] in ("task", "question")) == 1)
        wait_for(lambda: omp_idle("worker") is True, 30)
        omps["worker"].send({"id": "d", "type": "prompt", "message": "report-done"})
        checks["w_manager_got_done"] = wait_for(lambda: any(
            item["kind"] == "report" and item["handoff"] == "to_manager" and item["payload_kind"] == "done"
            for item in models["manager"].injected), 40)
        checks["w_worker_idle_after_done"] = wait_for(lambda: flow.worker_view()["state"] == "idle", 20)
        w_task = flow.tasks[w_first["task_id"]]
        checks["w_task_closed_done"] = (w_task.status, w_task.closed_reason) == ("closed", "done")
        with TaskRepository(db) as repository:
            w_decisions = repository.get_decisions(w_task.task_id, 1)
            w_history = repository.get_run_history(w_task.last_result["run_id"])
        result["w_decisions"] = [(d["kind"], d["details"].get("actor"), d["details"].get("authority"))
                                 for d in w_decisions]
        result["w_run_events"] = [e["kind"] for e in w_history]
        checks["w_standing_delegation_decisions"] = [d["kind"] for d in w_decisions] == ["scope_approved", "proceed"] \
            and all((d["details"].get("actor"), d["details"].get("authority")) == ("user_standing_delegation", "C-D66")
                    for d in w_decisions)
        checks["w_run_completed_by_done"] = [e["kind"] for e in w_history] == ["started", "completed"]
        nxt = manager_turn("stage-work-next", "manager-call-3")
        result["w_next_result"] = nxt
        checks["w_next_to_worker_dispatched"] = nxt.get("status") == "dispatched"
        checks["w_next_done"] = wait_for(lambda: (flow.tasks.get(nxt.get("task_id")) is not None
                                                  and flow.tasks[nxt["task_id"]].closed_reason == "done"), 40)
        checks["w_worker_idle_again"] = wait_for(lambda: flow.worker_view()["state"] == "idle", 20)

        # Stage E: two consecutive experiment runs in the same host shell.
        e_first = manager_turn("stage-exp", "manager-call-4")
        result["e_to_worker_result"] = e_first
        checks["e_dispatched"] = e_first.get("status") == "dispatched"
        models["manager"].experiment_task_id = e_first.get("task_id")
        e_runs: list[dict[str, Any]] = []
        for index in (1, 2):
            if index == 2:
                rerun = manager_turn("stage-exp-rerun", "manager-call-5")
                result["e_rerun_result"] = rerun
                checks["e_rerun_dispatched"] = (rerun.get("status"), rerun.get("retry")) == ("dispatched", 1)
                checks["e2_new_run"] = wait_for(lambda: (flow.task_view() or {}).get("runs_started") == 2, 60)
            checks[f"e{index}_finished"] = wait_for(
                lambda: (flow.task_view() or {}).get("status") == "finished"
                and (flow.task_view()["last_result"] or {}).get("run_id") not in [r.get("run_id") for r in e_runs],
                120)
            last = dict((flow.task_view() or {}).get("last_result") or {})
            e_runs.append(last)
            checks[f"e{index}_judged_success_report_processed"] = (
                last.get("judgment"), last.get("report"), last.get("run_closed")) == ("success", "omp_processed", True)
            run_id = last.get("run_id")
            if run_id:
                with TaskRepository(db) as repository:
                    shell_events = [e["kind"] for e in repository.get_shell_history(run_id)]
                    run_events = [e["kind"] for e in repository.get_run_history(run_id)]
                record = json.loads((root / "runs" / run_id / "run.json").read_text())
                raw = (root / "runs" / run_id / "raw.log").read_bytes()
                result[f"e{index}_shell_events"], result[f"e{index}_run_events"] = shell_events, run_events
                checks[f"e{index}_shell_events"] = shell_events == ["sent", "accepted", "started", "ended"]
                checks[f"e{index}_run_completed"] = run_events[-1] == "completed"
                checks[f"e{index}_ran_in_product_host_shell"] = (record.get("shell_source") == "injected_host_shell"
                                                                 and record.get("parent_pid") == pane.pid)
                checks[f"e{index}_raw_log_has_output"] = b"PASS from the host shell" in raw
                checks[f"e{index}_worker_decisions"] = (
                    record.get("worker_execution_decision", {}).get("decision") == "execute"
                    and record.get("worker_analysis_response", {}).get("decision") == "success")
            checks[f"e{index}_manager_got_report"] = wait_for(lambda: any(
                item["kind"] == "report" and item["run_id"] == run_id and item["judgment"] == "success"
                for item in models["manager"].injected), 20)
            checks[f"e{index}_shell_given_back_to_user"] = wait_for(
                lambda: pane.state["input_owner"] == "user" and pane.state["parent_mode"] == "manual_prompt", 15)
            checks[f"e{index}_no_hold_left"] = pane.automation_hold is None
            checks[f"e{index}_worker_idle"] = flow.worker_view()["state"] == "idle"
        result["e_task"] = flow.task_view()
        checks["e_two_runs_same_shell_distinct_runs"] = (len({r.get("run_id") for r in e_runs}) == 2
                                                         and not pane.exited())
        checks["e_ui_saw_output"] = b"PASS from the host shell" in bytes(ui_bytes)
        result["worker_injected"] = [{k: v for k, v in item.items() if k != "payload_keys"}
                                     for item in models["worker"].injected]
        result["manager_injected"] = [{k: v for k, v in item.items() if k != "payload_keys"}
                                      for item in models["manager"].injected]
        result["outbox"] = [{"target": e["target_role"], "state": e["state"]} for e in handoffs.outbox_snapshot()]
        checks["outbox_all_delivered"] = all(e["state"] == "delivered" for e in handoffs.outbox_snapshot())
        result["provider_requests"] = {role: models[role].requests for role in U1.ROLES}
    finally:
        result["omp_stop"] = {role: process.stop() for role, process in omps.items()}
        client.close()
        ui_stop.set()
        ui_thread.join(5)
        ui.close(flush_timeout=0.5)
        bridge.set_tool_handler(None)
        flow.close()
        handoffs.close()
        bridge.close()
        stop.set()
        loop.join(5)
        result["shell_close"] = pane.close()
        for server in servers.values():
            server.shutdown()
            server.server_close()
        try:
            git(source, "worktree", "prune")
        except subprocess.CalledProcessError:
            pass
        shutil.rmtree(root, ignore_errors=True)
        result["temp_removed"] = not root.exists()
    result["ok"] = bool(checks) and all(checks.values()) and result["temp_removed"]
    return result


if __name__ == "__main__":
    outcome = run(sys.argv[1] if len(sys.argv) > 1 else shutil.which("omp") or "omp")
    print(json.dumps(outcome, indent=1, sort_keys=True))
    sys.exit(0 if outcome["ok"] else 1)
