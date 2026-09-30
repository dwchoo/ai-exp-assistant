"""Actual OMP 18.2.10 pause/reconcile/resume through production runtime ports.

The provider is scripted, but OMP peers, G3, Task/run persistence, host shell,
pause policy, CW11 admission, and status projection are the product objects.
Only bounded predicates and counts are emitted; no model or terminal text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[3]
TESTS = REPO / "tests"
sys.path[:0] = [str(REPO / "src"), str(TESTS / "gates/g3_omp"),
                str(TESTS / "workflow"), str(TESTS / "bridge")]

from live_omp_probe import OMP_VERSION, _drain_pty, _omp_version, _stop_omps  # noqa: E402
from live_pause_abort_probe import cwd_processes  # noqa: E402
from live_rpc_pause_probe import file_manifest, semantic_provider  # noqa: E402
from live_tui_pause_reconcile_probe import _start as start_tui  # noqa: E402
from live_workflow_probe import _worker_provider, git  # noqa: E402
from workbench.contracts.v1 import ActorRole, MessageKind, new_identifier  # noqa: E402
from workbench.ipc.bridge_g3.mailbox import (  # noqa: E402
    BridgeTimeout, G3BridgeServer, TaskMailbox,
)
from workbench.observation.worker_review import SerializedReviewAdmission  # noqa: E402
from workbench.observation.workflow_binding import WorkflowObservationBinding  # noqa: E402
from workbench.policy.pause_automation import (  # noqa: E402
    PauseCoordinator, PauseJournal, PausePolicyError,
)
from workbench.tasks.repository import TaskRepository  # noqa: E402
from workbench.ui.status_workbench import (  # noqa: E402
    AreaObservation, ConfirmedFileChange, HostObservation, RunningProcess,
    project_three_area_status,
)
from workbench.workflow import G3WorkerResponsePort, TaskWorkflow  # noqa: E402


ROLES = (ActorRole.MANAGER, ActorRole.WORKER)
AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True,
}}
PUBLIC_RESULT_KEYS = frozenset({
    "abort_event", "abort_journal_request_id", "abort_request_correlated",
    "automatic_held", "binding_identity", "bridge_socket_removed",
    "bridge_thread_remaining", "cw11_admission", "cwd_processes",
    "cwd_processes_remaining", "distinct_peers", "drain_threads_remaining",
    "fresh_agent_end", "fresh_agent_start", "fresh_delivery",
    "fresh_provider_delta", "held_ack", "held_retry_ack", "host_collection",
    "host_experiment_preserved", "host_parent_remaining", "host_process_count",
    "host_processes_remaining", "initial_ready", "manager_files",
    "native_approval_pending", "native_approval_resolved", "no_paused_agent_start",
    "no_paused_provider_requests", "omp_children_remaining", "omp_version",
    "pause", "paused_provider_delta", "paused_state_identity", "peer_identity",
    "persisted_binding", "persisted_scope", "provider_threads_remaining",
    "reconciliation", "result", "result_absent_before_pause", "resumed",
    "stage", "stage_at_cleanup", "status_identity", "task_identity",
    "three_area_identity", "three_area_paused", "tool_started", "zero_residue",
    "status_file_changes", "status_running_processes", "status_unknown_rows",
    "manual_activity", "host_child_pid",
    "bound_automatic_transport",
    "out_of_scope_rejected", "removed_out_of_scope_file",
    "reconciliation_after_cleanup",
    "directory_mode_rejected", "directory_mode_restored",
})


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _ready(bridge: G3BridgeServer, role: ActorRole) -> bool:
    state = bridge.probe(role, timeout=2)
    return (state.get("idle") is True and state.get("pending") is False
            and state.get("approvalPending") is False
            and state.get("editorKnown") is True and state.get("editorEmpty") is True
            and state.get("inFlightToolCount") == 0 and state.get("paused") is False)


def _area(state: dict, action: str, observed: str) -> AreaObservation:
    return AreaObservation(
        last_action=action, last_action_at=observed, observed_at=observed,
        session_id=state.get("sessionId"), session_generation=state.get("generation"),
        idle=state.get("idle"), pending=state.get("pending"), paused=state.get("paused"),
        abort_status=state.get("abortStatus"),
        unknown_tool_results=tuple(state.get("unknownOutcomeToolCallIds") or ()),
    )


def run(omp: str) -> dict[str, object]:
    result: dict[str, object] = {"result": "inconclusive", "omp_version": _omp_version(omp),
                                 "stage": "setup"}
    if result["omp_version"] != OMP_VERSION:
        result["reason"] = "omp_version_mismatch"
        return result
    with tempfile.TemporaryDirectory(prefix="cw12-runtime-") as temporary:
        root = Path(temporary)
        socket_path = root / "bridge.sock"
        (root / "config.yml").write_text("startup:\n  setupWizard: false\n")
        source = root / "source"
        source.mkdir()
        git(source, "init", "-q")
        git(source, "config", "user.email", "cw12@example.invalid")
        git(source, "config", "user.name", "CW12 Fixture")
        (source / "tracked.txt").write_text("base\n")
        git(source, "add", "tracked.txt")
        git(source, "commit", "-qm", "fixture baseline")
        commit = git(source, "rev-parse", "HEAD")
        (root / "artifacts").mkdir()
        providers = {ActorRole.MANAGER: semantic_provider(manager=True),
                     ActorRole.WORKER: _worker_provider()}
        provider_threads = {role: threading.Thread(target=provider.serve_forever, daemon=True)
                            for role, provider in providers.items()}
        for thread in provider_threads.values():
            thread.start()
        for role in ROLES:
            profile = root / f"profile-{role.value}"
            profile.mkdir()
            (profile / "models.yml").write_text(
                "providers:\n  g3-tui-pause:\n"
                f"    baseUrl: http://127.0.0.1:{providers[role].server_port}/v1\n"
                "    api: openai-completions\n    auth: none\n    models:\n"
                "      - id: scripted\n        name: CW12 runtime\n"
                "        contextWindow: 32768\n        maxTokens: 1024\n")
        tokens = {role.value: str(uuid4()) for role in ROLES}
        bridge = G3BridgeServer(socket_path, tokens)
        bridge.start()
        repo = TaskRepository(root / "metadata.sqlite3")
        mailbox = TaskMailbox(repo, bridge)
        admission = SerializedReviewAdmission(automation_state=lambda: AUTOMATION)
        coordinator = PauseCoordinator(bridge=bridge, admission=admission,
                                       automation_state=lambda: AUTOMATION,
                                       request_timeout=5, stop_timeout=4)
        children: list[dict[str, object]] = []
        drains: list[tuple[threading.Thread, threading.Event]] = []
        active = None
        try:
            result["stage"] = "peers"
            previous_config = os.environ.get("PI_CONFIG_DIR")
            os.environ["PI_CONFIG_DIR"] = os.path.relpath(root / "omp-state", Path.home())
            try:
                for role in ROLES:
                    child = start_tui(omp, root, role.value, tokens[role.value])
                    children.append(child)
                    stop = threading.Event()
                    drain = threading.Thread(target=_drain_pty,
                                             args=(int(child["fd"]), stop, {}), daemon=True)
                    drain.start()
                    drains.append((drain, stop))
            finally:
                if previous_config is None:
                    os.environ.pop("PI_CONFIG_DIR", None)
                else:
                    os.environ["PI_CONFIG_DIR"] = previous_config
            peers = {role: bridge.peer(role, timeout=15) for role in ROLES}
            result["distinct_peers"] = len({peer.pid for peer in peers.values()}) == 2
            result["peer_identity"] = {
                role.value: {"session_id": peer.session_id,
                             "generation": peer.generation, "pid": peer.pid}
                for role, peer in peers.items()
            }
            result["initial_ready"] = _until(lambda: all(_ready(bridge, role) for role in ROLES), 15)
            if not result["initial_ready"]:
                raise RuntimeError("initial OMP state not ready")

            result["stage"] = "persisted_run"
            execution = {"source": str(source), "commit": commit,
                         "command": "printf HOST_STARTED; sleep 30; printf HOST_DONE > outcome.txt",
                         "criteria": {"log_contains": "HOST_STARTED", "result_file": "outcome.txt",
                                      "result_contains": "HOST_DONE"},
                         "environment": ["PATH", "TERM"], "shell": "bash"}
            task_id = repo.create_task({"goal": "approved CW12 pause runtime", "execution": execution})
            repo.approve_scope(task_id, 1, {"execution": execution, "paths": ["outcome.txt"]})
            repo.proceed(task_id, 1, "approved runtime probe")
            workflow = TaskWorkflow(repo, mailbox, worker_port=G3WorkerResponsePort(bridge),
                                    automation_source=coordinator.automation_state)
            active = workflow.start(task_id, 1, worktree_path=root / "execution",
                                    artifacts_root=root / "artifacts", automation=AUTOMATION,
                                    environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm"})
            ref = WorkflowObservationBinding.resolve_run(active)
            admission.set_active_run(ref)
            bound = coordinator.bind_run(active)
            binding = bound.binding
            result["task_identity"] = {
                "task_id": task_id, "revision": 1, "run_id": active.run_id,
                "approval_hash": active.approval_hash,
            }
            result["binding_identity"] = None if binding is None else {
                "task_id": binding.task_id, "revision": binding.revision,
                "run_id": binding.run_id, "approval_hash": binding.approval_hash,
                "approved_scope_hash": binding.approved_scope_hash,
                "approved_paths": list(binding.approved_paths),
                "manager_session_id": binding.manager_session_id,
                "manager_generation": binding.manager_generation,
                "worker_session_id": binding.worker_session_id,
                "worker_generation": binding.worker_generation,
            }
            result["persisted_binding"] = (bound.binding is not None
                                            and bound.binding.task_id == task_id
                                            and bound.binding.run_id == active.run_id)
            if not result["persisted_binding"]:
                raise RuntimeError("persisted binding failed")

            result["stage"] = "manager_tool"
            manager_fd = int(children[0]["fd"])
            for byte in b"Run the local scripted pause probe.":
                os.write(manager_fd, bytes((byte,)))
                time.sleep(0.005)
            os.write(manager_fd, b"\r")
            bridge.wait_event(ActorRole.MANAGER, "tool_approval_requested", {}, timeout=15)
            result["native_approval_pending"] = bridge.probe(ActorRole.MANAGER).get("approvalPending") is True
            os.write(manager_fd, b"y")
            time.sleep(0.1)
            os.write(manager_fd, b"\r")
            approved = bridge.wait_event(ActorRole.MANAGER, "tool_approval_resolved", {}, timeout=8)
            bridge.wait_event(ActorRole.MANAGER, "tool_execution_start", {}, timeout=8)
            result["native_approval_resolved"] = approved.get("approved") is True
            manager_cwd = root / "cwd-manager"
            result["tool_started"] = _until(lambda: (manager_cwd / "started.txt").exists(), 2)
            result["result_absent_before_pause"] = not (manager_cwd / "result.txt").exists()
            if not all(result[key] for key in ("native_approval_pending", "native_approval_resolved",
                                               "tool_started", "result_absent_before_pause")):
                raise RuntimeError("native tool boundary not established")

            result["stage"] = "pause"
            before_provider = {"manager": providers[ActorRole.MANAGER].RequestHandlerClass.requests,
                               "worker": providers[ActorRole.WORKER].request_count}
            cursor = bridge.event_cursor()
            paused = coordinator.pause()
            result["pause"] = {"paused": paused.paused, "manager_ack": paused.manager_ack_status,
                               "worker_ack": paused.worker_ack_status, "abort": paused.abort_status,
                               "stop_observed": paused.stop_observed_at is not None,
                               "unknown_tools": list(paused.unknown_tool_call_ids)}
            pause_events = PauseJournal(active.record_dir / "pause.jsonl").read()
            abort_requests = [event.request_id for event in pause_events
                              if event.kind == "manager_pause_ack"]
            stop_requests = [event.request_id for event in pause_events
                             if event.kind == "turn_stop_observed"]
            result["abort_request_correlated"] = (len(abort_requests) == 1
                                                  and len(stop_requests) == 1
                                                  and abort_requests[0] is not None
                                                  and abort_requests == stop_requests)
            stop_event = None
            if result["abort_request_correlated"]:
                try:
                    stop_event = bridge.wait_event(
                        ActorRole.MANAGER, "turn_stop_observed",
                        {"abortRequestId": abort_requests[0]},
                        after_sequence=cursor, timeout=0.2,
                    )
                except BridgeTimeout:
                    pass
            result["abort_event"] = None if stop_event is None else {
                "request_id": stop_event.get("abortRequestId"),
                "session_id": stop_event.get("sessionId"),
                "generation": stop_event.get("generation"),
                "role": stop_event.get("role"),
            }
            result["abort_journal_request_id"] = abort_requests[0] if len(abort_requests) == 1 else None
            if not paused.paused:
                raise RuntimeError("pause fence did not close")
            held_called = []
            held_status, _ = coordinator.dispatch_automatic(lambda: held_called.append(True))
            ticket = admission.issue_review(ref, time.monotonic(), 0, {"source": "cw12"})
            admission_result = admission.admit_and_dispatch(ticket, dispatch_review=lambda _: held_called.append(True))
            result["automatic_held"] = held_status == "held" and not held_called
            result["cw11_admission"] = admission_result.status
            collected = coordinator.collect_non_model(active, timeout=0.1)
            result["host_collection"] = {"paused": collected.get("paused_at_collection"),
                                          "shell_state": collected.get("shell_state"),
                                          "run_id_matches": collected.get("run_id") == active.run_id}
            held = mailbox.create_message(task_id, 1, active.run_id, ActorRole.WORKER,
                                          ActorRole.MANAGER, MessageKind.REPORT,
                                          {"stage": "held"},
                                          in_reply_to_message_id=active.task_message_id)
            held_ack = bridge.request(ActorRole.MANAGER,
                                      {"kind": "deliver", "envelope": held.envelope(new_identifier()).to_json()},
                                      timeout=5)
            result["held_ack"] = held_ack.get("status")

            result["stage"] = "reconcile"
            settled = _until(lambda: all(
                (state := bridge.probe(role, timeout=2)).get("paused") is True
                and state.get("idle") is True and state.get("pending") is False
                and state.get("approvalPending") is False
                and state.get("editorKnown") is True and state.get("editorEmpty") is True
                and state.get("inFlightToolCount") == 0
                for role in ROLES), 10)
            manager_state = bridge.probe(ActorRole.MANAGER)
            worker_state = bridge.probe(ActorRole.WORKER)
            result["paused_state_identity"] = {
                role.value: {"session_id": state.get("sessionId"),
                             "generation": state.get("generation")}
                for role, state in ((ActorRole.MANAGER, manager_state),
                                    (ActorRole.WORKER, worker_state))
            }
            manifest_first = file_manifest(manager_cwd)
            time.sleep(0.3)
            manifest_second = file_manifest(manager_cwd)
            processes = {role.value: cwd_processes(root / f"cwd-{role.value}") for role in ROLES}
            host_processes = cwd_processes(root / "execution")
            shell_snapshot = active.shell.snapshot()
            host_parent = shell_snapshot.get("parent_pid")
            host_child = shell_snapshot.get("lifecycle", {}).get("child_pid")
            current = repo.get_current_run(task_id)
            task = repo.get_task_spec(task_id, 1)
            approvals = [decision for decision in repo.get_decisions(task_id, 1)
                         if decision["kind"] == "scope_approved"]
            approved_scope = approvals[-1]["details"] if approvals else None
            result["persisted_scope"] = None if approved_scope is None else {
                "hash": sha256(json.dumps(approved_scope, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest(),
                "paths": approved_scope.get("paths"),
            }
            pause_binding = coordinator.status().binding
            checks = {
                "filesMatch": manifest_first == manifest_second
                    and manifest_second.get("scope_valid") is True
                    and coordinator._peer_files_reconciled(active, pause_binding),
                "toolsMatch": settled and manager_state.get("inFlightToolCount") == 0
                    and worker_state.get("inFlightToolCount") == 0
                    and "pause-probe-bash" in coordinator.status().unknown_tool_call_ids,
                "processesMatch": processes["manager"] == [peers[ActorRole.MANAGER].pid]
                    and processes["worker"] == [peers[ActorRole.WORKER].pid]
                    and host_parent in host_processes
                    and host_child in host_processes,
                "taskMatch": current is not None and current.get("run_id") == active.run_id
                    and task.get("revision") == 1 and len(repo.get_run_history(active.run_id)) > 0,
                "approvalMatch": bool(approvals) and pause_binding is not None
                    and pause_binding.approval_hash == active.approval_hash,
            }
            result["manager_files"] = manifest_second
            result["cwd_processes"] = processes
            result["host_process_count"] = len(host_processes)
            result["host_child_pid"] = host_child
            result["host_experiment_preserved"] = collected.get("shell_state") == "running"
            result["no_paused_provider_requests"] = before_provider == {
                "manager": providers[ActorRole.MANAGER].RequestHandlerClass.requests,
                "worker": providers[ActorRole.WORKER].request_count,
            }
            result["paused_provider_delta"] = {
                "manager": providers[ActorRole.MANAGER].RequestHandlerClass.requests - before_provider["manager"],
                "worker": providers[ActorRole.WORKER].request_count - before_provider["worker"],
            }
            result["no_paused_agent_start"] = all(
                _no_agent_start(bridge, role, cursor) for role in ROLES)
            observed = _stamp()
            confirmed_file = ConfirmedFileChange(
                "cwd-manager/started.txt", "created", observed,
                manifest_second.get("started.txt"),
            )
            running_host = RunningProcess(host_child, execution["command"], observed)
            projection = project_three_area_status(
                pause=coordinator.status(), manager=_area(manager_state, "pause", observed),
                worker=_area(worker_state, "pause", observed),
                host=HostObservation(task_id, 1, active.run_id, "collect", observed, observed,
                                     (confirmed_file,), (running_host,),
                                     phase=collected.get("shell_state"),
                                     exit_confirmed=collected.get("exit_confirmed"),
                                     exit_status=collected.get("exit_status")),
            ).to_dict()
            result["status_file_changes"] = projection["host"]["confirmed_file_changes"]
            result["status_running_processes"] = projection["host"]["running_processes"]
            result["status_unknown_rows"] = {
                area: projection[area]["unknown_tool_results"]
                for area in ("manager", "worker", "host")
            }
            result["three_area_paused"] = all(projection[area]["paused"] is True
                                               for area in ("manager", "worker", "host"))
            result["three_area_identity"] = projection["host"]["task"]["run_id"] == active.run_id
            result["status_identity"] = {
                "manager": {"session_id": projection["manager"]["session_id"],
                            "generation": projection["manager"]["session_generation"]},
                "worker": {"session_id": projection["worker"]["session_id"],
                           "generation": projection["worker"]["session_generation"]},
                "host": projection["host"]["task"],
            }
            if checks["filesMatch"] or not all(
                    value for key, value in checks.items() if key != "filesMatch"):
                raise RuntimeError("out-of-scope peer output was not isolated")

            result["stage"] = "manual_while_paused"
            manual_cursor = bridge.event_cursor()
            worker_provider_before = providers[ActorRole.WORKER].request_count
            worker_fd = int(children[1]["fd"])
            for byte in b"Manual operator check while automation is paused.":
                os.write(worker_fd, bytes((byte,)))
                time.sleep(0.005)
            os.write(worker_fd, b"\r")
            manual_start = bridge.wait_event(
                ActorRole.WORKER, "agent_start", {}, after_sequence=manual_cursor, timeout=10)
            manual_end = bridge.wait_event(
                ActorRole.WORKER, "agent_end", {}, after_sequence=manual_cursor, timeout=10)
            manual_held, _ = coordinator.dispatch_automatic(lambda: held_called.append(True))
            manual_state = bridge.probe(ActorRole.WORKER)
            result["manual_activity"] = {
                "provider_delta": providers[ActorRole.WORKER].request_count - worker_provider_before,
                "start_session_matches": manual_start.get("sessionId") == peers[ActorRole.WORKER].session_id,
                "end_session_matches": manual_end.get("sessionId") == peers[ActorRole.WORKER].session_id,
                "omp_remains_paused": manual_state.get("paused") is True,
                "coordinator_remains_paused": coordinator.status().paused,
                "automatic_held": manual_held == "held" and not held_called,
            }

            result["stage"] = "resume"
            evidence = {"portVersion": 2, "kind": "ResumeEvidence", "payload": {
                "userResume": True, "taskId": task_id, "runId": active.run_id,
                "approvalHash": active.approval_hash, **checks,
                "checkedAt": _stamp(), "unknowns": [],
            }}
            # The manager tool wrote outside the approved execution worktree.
            # A durable bind-time baseline must reject that change even though
            # the earlier observation could describe it in the UI.
            result["out_of_scope_rejected"] = False
            if not checks["filesMatch"]:
                try:
                    coordinator.resume(active, evidence)
                except PausePolicyError:
                    result["out_of_scope_rejected"] = coordinator.status().paused
            if not result["out_of_scope_rejected"]:
                raise RuntimeError("out-of-scope peer output was not rejected")
            (manager_cwd / "started.txt").unlink()
            result["removed_out_of_scope_file"] = not (manager_cwd / "started.txt").exists()
            result["reconciliation_after_cleanup"] = coordinator._peer_files_reconciled(
                active, binding)
            if not (result["removed_out_of_scope_file"]
                    and result["reconciliation_after_cleanup"]):
                raise RuntimeError("test-owned peer output cleanup did not reconcile")
            original_mode = manager_cwd.stat().st_mode & 0o7777
            if original_mode == 0o555:
                raise RuntimeError("manager cwd is already read-only")
            manager_cwd.chmod(0o555)
            try:
                result["directory_mode_rejected"] = False
                if not coordinator._peer_files_reconciled(active, binding):
                    try:
                        coordinator.resume(active, evidence)
                    except PausePolicyError:
                        result["directory_mode_rejected"] = coordinator.status().paused
            finally:
                manager_cwd.chmod(original_mode)
            result["directory_mode_restored"] = (
                (manager_cwd.stat().st_mode & 0o7777) == original_mode
                and coordinator._peer_files_reconciled(active, binding))
            if not (result["directory_mode_rejected"]
                    and result["directory_mode_restored"]):
                raise RuntimeError("directory mode did not reject and reconcile")
            checks["filesMatch"] = result["directory_mode_restored"]
            result["reconciliation"] = dict(checks)
            resumed = coordinator.resume(active, evidence)
            result["resumed"] = not resumed.paused and not resumed.cancelled
            retry_ack = bridge.request(ActorRole.MANAGER,
                                       {"kind": "deliver", "envelope": held.envelope(new_identifier()).to_json()},
                                       timeout=5)
            result["held_retry_ack"] = retry_ack.get("status")
            fresh = mailbox.create_message(task_id, 1, active.run_id, ActorRole.WORKER,
                                           ActorRole.MANAGER, MessageKind.REPORT,
                                           {"stage": "fresh"},
                                           in_reply_to_message_id=active.task_message_id)
            fresh_cursor = bridge.event_cursor()
            fresh_provider_before = providers[ActorRole.MANAGER].RequestHandlerClass.requests
            expected_peers = {
                ActorRole.MANAGER: (binding.manager_session_id, binding.manager_generation),
                ActorRole.WORKER: (binding.worker_session_id, binding.worker_generation),
            }
            bound_calls = []
            original_request = bridge.request

            def observed_bound_request(role, frame, timeout=5, **kwargs):
                if frame.get("kind") == "deliver":
                    token = kwargs.get("authority_token")
                    bound_calls.append(
                        role is ActorRole.MANAGER
                        and kwargs.get("expected_peer") == expected_peers[ActorRole.MANAGER]
                        and kwargs.get("expected_peers") == expected_peers
                        and token is not None and token.current()
                    )
                return original_request(role, frame, timeout, **kwargs)

            bridge.request = observed_bound_request
            try:
                dispatch_state, receipt = coordinator.dispatch_bound_automatic(
                    mailbox, fresh, timeout=20)
            finally:
                bridge.request = original_request
            result["bound_automatic_transport"] = bound_calls == [True]
            result["fresh_delivery"] = {"dispatch": dispatch_state,
                                        "status": None if receipt is None else receipt.status.value}
            result["fresh_provider_delta"] = (
                providers[ActorRole.MANAGER].RequestHandlerClass.requests - fresh_provider_before
            )
            result["fresh_agent_start"] = _event_seen(
                bridge, ActorRole.MANAGER, "agent_start", fresh_cursor)
            result["fresh_agent_end"] = _event_seen(
                bridge, ActorRole.MANAGER, "agent_end", fresh_cursor)
        except Exception as exc:
            result["result"] = "runtime_exception"
            result["error_type"] = type(exc).__name__
        finally:
            result["stage_at_cleanup"] = result["stage"]
            if active is not None:
                active.close()
            result["host_parent_remaining"] = (active is not None
                                               and Path(f"/proc/{active.shell.snapshot().get('parent_pid')}").exists())
            _stop_omps(children)
            result["omp_children_remaining"] = sum(Path(f"/proc/{child['pid']}").exists()
                                                    for child in children)
            result["cwd_processes_remaining"] = {
                role.value: cwd_processes(root / f"cwd-{role.value}")
                for role in ROLES if (root / f"cwd-{role.value}").exists()
            }
            result["host_processes_remaining"] = cwd_processes(root / "execution")
            for drain, stop in drains:
                stop.set()
                drain.join(timeout=1)
            result["drain_threads_remaining"] = sum(drain.is_alive() for drain, _ in drains)
            for child in children:
                try:
                    os.close(int(child["fd"]))
                except OSError:
                    pass
            bridge.close()
            result["bridge_socket_removed"] = not socket_path.exists()
            result["bridge_thread_remaining"] = bridge._thread is not None and bridge._thread.is_alive()
            repo.close()
            for role in ROLES:
                providers[role].shutdown()
                providers[role].server_close()
                provider_threads[role].join(timeout=2)
            result["provider_threads_remaining"] = sum(thread.is_alive()
                                                       for thread in provider_threads.values())
            result["zero_residue"] = (result["omp_children_remaining"] == 0
                                      and all(not pids for pids in result["cwd_processes_remaining"].values())
                                      and not result["host_processes_remaining"]
                                      and not result["host_parent_remaining"]
                                      and result["drain_threads_remaining"] == 0
                                      and result["bridge_socket_removed"]
                                      and not result["bridge_thread_remaining"]
                                      and result["provider_threads_remaining"] == 0)
            if result["result"] != "runtime_exception":
                result["result"] = "passed" if qualifies(result) else "assertion_failed"
    return result


def _no_agent_start(bridge: G3BridgeServer, role: ActorRole, cursor: int) -> bool:
    try:
        bridge.wait_event(role, "agent_start", {}, after_sequence=cursor, timeout=0.2)
    except BridgeTimeout:
        return True
    return False


def _event_seen(bridge: G3BridgeServer, role: ActorRole, name: str,
                cursor: int) -> bool:
    try:
        bridge.wait_event(role, name, {}, after_sequence=cursor, timeout=0.2)
    except BridgeTimeout:
        return False
    return True


def qualifies(result: dict[str, object]) -> bool:
    """Require independent identity, activity, replay, reconciliation and cleanup facts."""
    try:
        if not set(result).issubset(PUBLIC_RESULT_KEYS):
            return False
        peers = result["peer_identity"]
        binding = result["binding_identity"]
        task = result["task_identity"]
        scope = result["persisted_scope"]
        paused = result["paused_state_identity"]
        status = result["status_identity"]
        event = result["abort_event"]
        pause = result["pause"]
        checks = result["reconciliation"]
        host = result["host_collection"]
        fresh = result["fresh_delivery"]
        files = result["manager_files"]
        file_rows = result["status_file_changes"]
        process_rows = result["status_running_processes"]
        unknown_rows = result["status_unknown_rows"]
        manual = result["manual_activity"]
        for role in ("manager", "worker"):
            if (paused[role] != {"session_id": peers[role]["session_id"],
                                 "generation": peers[role]["generation"]}
                    or status[role] != paused[role]
                    or binding[f"{role}_session_id"] != peers[role]["session_id"]
                    or binding[f"{role}_generation"] != peers[role]["generation"]):
                return False
        return bool(
            result["omp_version"] == OMP_VERSION and result["initial_ready"]
            and result["distinct_peers"] and result["persisted_binding"]
            and all(binding[key] == task[key] for key in
                    ("task_id", "revision", "run_id", "approval_hash"))
            and binding["approved_scope_hash"] == scope["hash"]
            and binding["approved_paths"] == scope["paths"]
            and status["host"]["task_id"] == task["task_id"]
            and status["host"]["revision"] == task["revision"]
            and status["host"]["run_id"] == task["run_id"]
            and result["abort_request_correlated"]
            and event == {"request_id": result["abort_journal_request_id"],
                          "session_id": peers["manager"]["session_id"],
                          "generation": peers["manager"]["generation"],
                          "role": "manager"}
            and pause["paused"] and pause["manager_ack"] == "abort_requested"
            and pause["worker_ack"] == "paused"
            and pause["abort"] == "stop_observed" and pause["stop_observed"]
            and "pause-probe-bash" in pause["unknown_tools"]
            and result["native_approval_pending"] and result["native_approval_resolved"]
            and result["tool_started"] and result["result_absent_before_pause"]
            and result["automatic_held"] and result["cw11_admission"] == "paused"
            and host["paused"] is True and host["run_id_matches"] is True
            and host["shell_state"] == "running"
            and result["host_experiment_preserved"]
            and files["scope_valid"] is True
            and files["started.txt"] == sha256(b"STARTED").hexdigest()
            and files["result.txt"] is None
            and files["other_entries_sha256"] == sha256(b"[]").hexdigest()
            and len(file_rows) == 1
            and type(file_rows[0]["confirmed_at"]) is str and bool(file_rows[0]["confirmed_at"])
            and file_rows == [{"path": "cwd-manager/started.txt", "change": "created",
                               "confirmed_at": file_rows[0]["confirmed_at"],
                               "content_hash": files["started.txt"]}]
            and len(process_rows) == 1
            and type(process_rows[0]["observed_at"]) is str and bool(process_rows[0]["observed_at"])
            and process_rows == [{"pid": result["host_child_pid"],
                                  "command": "printf HOST_STARTED; sleep 30; printf HOST_DONE > outcome.txt",
                                  "observed_at": process_rows[0]["observed_at"],
                                  "state": "running"}]
            and type(result["host_child_pid"]) is int and result["host_child_pid"] > 0
            and type(result["host_process_count"]) is int and result["host_process_count"] >= 2
            and result["cwd_processes"] == {
                role: [peers[role]["pid"]] for role in ("manager", "worker")
            }
            and unknown_rows == {"manager": ["pause-probe-bash"],
                                 "worker": [], "host": ["pause-probe-bash"]}
            and result["paused_provider_delta"] == {"manager": 0, "worker": 0}
            and result["no_paused_provider_requests"] and result["no_paused_agent_start"]
            and result["three_area_paused"] and result["three_area_identity"]
            and manual == {"provider_delta": 1,
                           "start_session_matches": True, "end_session_matches": True,
                           "omp_remains_paused": True, "coordinator_remains_paused": True,
                           "automatic_held": True}
            and all(checks[key] is True for key in
                    ("filesMatch", "toolsMatch", "processesMatch", "taskMatch", "approvalMatch"))
            and result["held_ack"] == "deferred" and result["resumed"]
            and result["out_of_scope_rejected"]
            and result["removed_out_of_scope_file"]
            and result["reconciliation_after_cleanup"]
            and result["directory_mode_rejected"]
            and result["directory_mode_restored"]
            and result["held_retry_ack"] == "unknown_no_replay"
            and result["bound_automatic_transport"] is True
            and fresh == {"dispatch": "admitted", "status": "omp_processed"}
            and result["fresh_provider_delta"] >= 1
            and result["fresh_agent_start"] and result["fresh_agent_end"]
            and result["zero_residue"]
            and result["omp_children_remaining"] == 0
            and all(not pids for pids in result["cwd_processes_remaining"].values())
            and not result["host_processes_remaining"]
            and result["host_parent_remaining"] is False
            and result["drain_threads_remaining"] == 0
            and result["bridge_socket_removed"] is True
            and result["bridge_thread_remaining"] is False
            and result["provider_threads_remaining"] == 0
        )
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return False


if __name__ == "__main__":
    binary = shutil.which("omp")
    if not binary:
        raise SystemExit("omp unavailable")
    evidence = run(binary)
    print(json.dumps(evidence, sort_keys=True))
    raise SystemExit(0 if evidence["result"] == "passed" and evidence["zero_residue"] else 1)
