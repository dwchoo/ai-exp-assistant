"""CW-18 U3 live probe: the automation loop with two real OMPs, no model.

The same isolated setup as the U2 probe (``live_task_flow_probe``): two OMP
processes in RPC mode with the product bridge extension, each in its own fake
HOME whose only model is a scripted OpenAI-compatible server on 127.0.0.1
(other HTTP(S) goes to a closed proxy port; no credentials exist), a real host
ShellPane pumped like the backend loop, the product HandoffService/TaskFlow and
a UiServer whose pause/resume handlers are the Backend's own methods.
New here: the product ``AutomationController`` (lifecycle record +
``bind_production``, 1 s tick thread, ``WorkerReviewScheduler`` at the real
60 s interval by default, ``PauseCoordinator`` pause/resume) wired as the
Backend wires it.

Stage E: manager to_worker experiment -> dispatched (C-D66) -> the host run (a
counter that prints every second) -> two periodic reviews reach the worker ->
the manager is put in a slow turn -> ui_v1 pause -> the turn stop is
requested/confirmed, no review while paused for longer than an interval, host
collection goes on -> ui_v1 resume reconciled:false is refused, reconciled:true
resumes after reconciliation -> the held review runs once -> the run ends,
is judged and reported; automation returns to idle.

Usage: PYTHONPATH=src python tests/backend/live_automation_probe.py [omp] [interval_seconds]
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

from workbench.app.lifecycle import LifecycleJournal  # noqa: E402
from workbench.backend.automation import AutomationController  # noqa: E402
from workbench.backend.client import UiClient  # noqa: E402
from workbench.backend.flow import HandoffService  # noqa: E402
from workbench.backend.flow_tasks import ExperimentPorts, TaskFlow  # noqa: E402
from workbench.backend.panes import HostShellPort, ShellPane  # noqa: E402
from workbench.backend.service import Backend  # noqa: E402
from workbench.backend.ui_server import UiServer  # noqa: E402
from workbench.contracts.ui_v1 import ClientType  # noqa: E402
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, MailboxError, TaskMailbox  # noqa: E402
from workbench.storage.log_raw import RawLogStore  # noqa: E402
from workbench.tasks.repository import TaskRepository  # noqa: E402
from workbench.terminal.shell_g2.prototype import ShellChoice  # noqa: E402
from workbench.workflow import G3WorkerResponsePort, TaskWorkflow  # noqa: E402


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


U2 = _load(ROOT / "tests/backend/live_task_flow_probe.py", "cw18_u2_live_probe")
U1 = U2.U1
SLOW_TURN_SECONDS = 40.0


class Model(U2.Model):
    """U2's scripted model; the manager's ``stage-slow`` prompt keeps a turn in flight."""

    def __init__(self, role: str, experiment_spec: dict[str, Any]):
        super().__init__(role, experiment_spec)
        self.slow_started = threading.Event()

    def respond(self, request: dict[str, Any]) -> dict[str, Any]:
        messages = request.get("messages") or []
        last = messages[-1] if messages else {}
        blocks = U1._text_blocks(last.get("content"))
        if self.role == "manager" and blocks and blocks[-1].strip() == "stage-slow":
            with self.lock:
                self.requests += 1
            self.slow_started.set()
            time.sleep(SLOW_TURN_SECONDS)  # aborted by the pause long before this returns
            return {"content": "slow turn finished"}
        return super().respond(request)


class Controller:
    """ui_v1 controller with the Backend's own pause/resume and automation view."""

    pause = Backend.pause
    resume = Backend.resume
    _automation_view = Backend._automation_view
    _automation_paused = Backend._automation_paused

    def __init__(self, flow: TaskFlow, loop: AutomationController):
        self.flow, self.automation_loop = flow, loop
        self.automation = {"state": "not_configured"}
        self.pause_hook, self.resume_hook = loop.request_pause, loop.request_resume

    def snapshot(self) -> dict[str, Any]:
        return {"task": self.flow.task_view(), "worker": self.flow.worker_view(),
                "automation": self._automation_view()}

    def replay(self) -> list:
        return []

    def on_attach(self, size: Any) -> None:
        pass

    def on_detach(self) -> None:
        pass


def run(omp: str, interval: int) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": "two-real-omp-rpc-scripted-provider+host-shellpane+automation-loop",
                              "interval_seconds": interval, "checks": {}}
    checks = result["checks"]
    version = subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=30,
                             env={"PATH": U1.os.environ.get("PATH", ""), "HOME": "/nonexistent"})
    result["omp"] = (version.stdout or version.stderr).strip().splitlines()[0]
    root = Path(tempfile.mkdtemp(prefix="wb-p27-cw18-u3-live-", dir="/tmp"))
    source = root / "source"
    source.mkdir()
    U2.git(source, "init", "-q")
    U2.git(source, "config", "user.email", "probe@example.invalid")
    U2.git(source, "config", "user.name", "CW18 U3 probe")
    pause_hold = interval + 5
    duration = 2 * interval + pause_hold + 45
    (source / "run.sh").write_text(
        "#!/bin/sh\nprintf 'PASS from the host shell\\n'\n"
        f"i=0; while [ $i -lt {duration} ]; do i=$((i+1)); printf 'tick %s\\n' $i; sleep 1; done\n"
        "printf PASS > outcome.txt\nprintf 'PASS from the host shell\\n'\n")
    (source / "run.sh").chmod(0o755)
    U2.git(source, "add", "run.sh")
    U2.git(source, "commit", "-qm", "probe")
    commit = U2.git(source, "rev-parse", "HEAD")
    experiment_spec = {"goal": "probe experiment", "paths": ["outcome.txt"], "execution": {
        "source": str(source), "commit": commit, "command": "./run.sh",
        "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
        "environment": ["PATH"], "shell": "bash"}}
    for name in ("workflow", "worktrees", "runs", "shell-home", "raw"):
        (root / name).mkdir(mode=0o700)
    models = {role: Model(role, experiment_spec) for role in U1.ROLES}
    servers = {role: U2._server(models[role]) for role in U1.ROLES}
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
    stop = threading.Event()

    def backend_loop():
        while not stop.is_set():
            pane.pump()
            time.sleep(0.01)

    loop_thread = threading.Thread(target=backend_loop, daemon=True)
    loop_thread.start()
    raw = RawLogStore(root / "raw")
    logs: list[str] = []
    automation = AutomationController(
        bridge=bridge, database=db, journal=LifecycleJournal(root / "lifecycle.json"), raw=raw,
        shell_pane=lambda: pane, project_dir=root / "cwd-worker", artifacts_root=root / "runs",
        boot_marker=lambda: Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        interval_seconds=interval, log=lambda line: logs.append(f"{time.monotonic():.1f} {line}"))

    def automation_port():
        return {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": automation.paused(), "cancelled": False, "metadataHealthy": True, "approvalValid": True}}

    handoffs = HandoffService(root / "workflow" / "handoffs.jsonl", mailbox_factory=lane_mailbox,
                              paused=automation.paused, peer_lookup=lambda role: bridge.peer(role, 0),
                              retry_interval=0.2)
    ports = ExperimentPorts(
        host_shell=lambda: HostShellPort(pane, lambda: pane),
        make_workflow=lambda repository: TaskWorkflow(repository, TaskMailbox(repository, bridge),
                                                      worker_port=G3WorkerResponsePort(bridge),
                                                      automation_source=automation_port),
        automation=automation_port, environment_names=lambda: set(shell_env),
        worktrees_root=root / "worktrees", artifacts_root=root / "runs")
    flow = TaskFlow(root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(db),
                    handoffs=handoffs, omp_idle=omp_idle, paused=automation.paused, experiment=ports,
                    lifecycle=automation)
    controller = Controller(flow, automation)
    handoffs.configure(policy=flow, active_task=flow.active_task)
    handoffs.start()
    flow.start()
    automation.start()
    bridge.set_tool_handler(lambda peer, request: handoffs.handle(peer.role, request))
    ui_socket = root / "ui.sock"
    ui = UiServer(ui_socket, controller)
    ui_stop = threading.Event()

    def serve_ui():
        while not ui_stop.is_set():
            ui.poll(0.02)

    ui_thread = threading.Thread(target=serve_ui, daemon=True)
    ui_thread.start()
    client = UiClient(ui_socket, timeout=30)
    client.attach()
    omps: dict[str, Any] = {}

    def reviews() -> list[dict[str, Any]]:
        return [item for item in models["worker"].injected if item.get("stage") == "periodic_review"]

    def state() -> dict[str, Any]:
        return automation.status()

    try:
        for role in U1.ROLES:
            omps[role] = U1.RpcOmp(omp, role, tokens[role], root, socket_path, servers[role].server_port)
        peers = {role: bridge.peer(role, timeout=40) for role in U1.ROLES}
        checks["both_omps_connected"] = len({peer.pid for peer in peers.values()}) == 2
        result["initial_automation"] = client.snapshot()["automation"]["state"]
        checks["idle_before_any_run"] = result["initial_automation"] == "idle"

        omps["manager"].send({"id": "e", "type": "prompt", "message": "stage-exp"})
        checks["dispatched_without_ui_approval"] = U2.wait_for(
            lambda: (client.snapshot().get("worker") or {}).get("state") == "busy", 40)
        checks["run_bound"] = U2.wait_for(lambda: (state().get("run") or {}).get("bound") is True, 60)
        run_info = state().get("run") or {}
        result["run"] = run_info
        record = automation.journal.read()
        checks["first_lifecycle_record"] = (record is not None and record.run_id == run_info.get("run_id")
                                            and record.generation == 1 and record.shell.pid == pane.pid
                                            and record.manager.process.pid == peers["manager"].pid
                                            and record.worker.process.pid == peers["worker"].pid)
        bound_at = time.monotonic()
        checks["review_scheduled"] = U2.wait_for(lambda: (state()["review"]["status"] == "waiting"
                                                          and state()["review"]["next_due_in_seconds"]), 10)
        checks["no_review_before_interval"] = U2.wait_for(lambda: False, interval - 10) or reviews() == []
        checks["two_reviews"] = U2.wait_for(lambda: len(reviews()) >= 2, 2 * interval + 30)
        checks["reviews_processed"] = U2.wait_for(lambda: state()["review"]["review_count"] >= 2, 30) and all(
            "omp_processed" in line for line in logs if "periodic review" in line)
        result["review_times_after_bind"] = [round(float(line.split()[0]) - bound_at, 1)
                                             for line in logs if "periodic review" in line]
        result["review_status"] = state()["review"]

        # A slow manager turn is in flight when the user pauses.
        omps["manager"].wait(lambda f: f.get("type") == "agent_end", 30)
        omps["manager"].send({"id": "slow", "type": "prompt", "message": "stage-slow"})
        checks["manager_in_slow_turn"] = models["manager"].slow_started.wait(30)
        before_pause = {role: models[role].requests for role in U1.ROLES}
        paused = client.request(ClientType.PAUSE)
        checks["pause_accepted_at_once"] = paused.get("ok") is True and paused["automation"]["paused"] is True
        checks["paused"] = U2.wait_for(lambda: state()["state"] == "paused", 30)
        interruption = state()["interruption"]
        result["interruption"] = interruption
        checks["interruption_requested_and_confirmed"] = (interruption["manager_ack"] == "abort_requested"
                                                         and interruption["state"] in {"requested", "confirmed"})
        result["interruption_confirmed"] = interruption["state"] == "confirmed"
        run_id = run_info.get("run_id")
        raw_log = root / "runs" / str(run_id) / "raw.log"
        size_at_pause, reviews_at_pause = raw_log.stat().st_size, len(reviews())
        time.sleep(pause_hold)
        checks["no_review_while_paused"] = len(reviews()) == reviews_at_pause
        checks["collection_continues_while_paused"] = raw_log.stat().st_size > size_at_pause
        checks["no_provider_request_while_paused"] = {role: models[role].requests for role in U1.ROLES} \
            == before_pause
        result["tick_while_paused"] = state()["tick"]
        refused = client.request(ClientType.RESUME, reconciled=False)
        checks["resume_unreconciled_refused"] = (refused.get("ok") is False
                                                 and refused.get("reason") == "resume_not_reconciled"
                                                 and state()["paused"] is True)
        resumed = client.request(ClientType.RESUME, reconciled=True)
        checks["resume_accepted"] = resumed.get("ok") is True
        checks["resumed_after_reconciliation"] = U2.wait_for(
            lambda: state()["paused"] is False and (state()["resume"] or {}).get("outcome") == "resumed", 60)
        result["resume"] = state()["resume"]
        checks["slow_turn_not_replayed"] = U2.wait_for(lambda: True, 3) and \
            models["manager"].requests == before_pause["manager"]
        checks["held_review_runs_once_after_resume"] = U2.wait_for(
            lambda: len(reviews()) == reviews_at_pause + 1, 20)
        time.sleep(3)
        checks["held_review_not_duplicated"] = len(reviews()) == reviews_at_pause + 1

        checks["run_reported"] = U2.wait_for(
            lambda: ((flow.task_view() or {}).get("last_result") or {}).get("outcome") == "reported", duration + 60)
        last = (flow.task_view() or {}).get("last_result") or {}
        result["last_result"] = last
        checks["judged_success_and_closed"] = (last.get("judgment"), last.get("run_closed")) == ("success", True)
        checks["automation_idle_after_run"] = U2.wait_for(lambda: state()["state"] == "idle", 15)
        result["final_automation"] = state()
        result["provider_requests"] = {role: models[role].requests for role in U1.ROLES}
    finally:
        result["log"] = logs[-40:]
        result["omp_stop"] = {role: process.stop() for role, process in omps.items()}
        client.close()
        ui_stop.set()
        ui_thread.join(5)
        ui.close(flush_timeout=0.5)
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
    outcome = run(sys.argv[1] if len(sys.argv) > 1 else shutil.which("omp") or "omp",
                  int(sys.argv[2]) if len(sys.argv) > 2 else 60)
    print(json.dumps(outcome, indent=1, sort_keys=True, default=str))
    sys.exit(0 if outcome["ok"] else 1)
