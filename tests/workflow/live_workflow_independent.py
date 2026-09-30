"""Standalone bounded CW-10 host-shell and detached-worktree evidence."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from uuid import uuid4

from test_run_independent import WorkflowIndependentTests, git


def run_case(label, command, shell, expected_judgment, expected_exit):
    fixture = WorkflowIndependentTests("test_explicit_commit_and_canonical_target_preserve_dirty_user_work")
    fixture.setUp()
    parent_pid = None
    try:
        task = fixture.task(fixture.execution(command, shell=shell))
        run = fixture.start(task, "execution-" + label)
        parent_pid = run.shell.parent_pid
        observation = run.collect(timeout=8)
        assert observation["exit_confirmed"] and observation["exit_status"] == expected_exit
        report = run.judge()
        evidence = report["worker_judgment"]
        assert evidence["judgment"] == expected_judgment
        assert evidence["exit_status"] == expected_exit
        assert Path(evidence["raw_log"]).is_file()
        assert Path(run.result_path).is_file()
        assert git(run.worktree.path, "rev-parse", "HEAD") == fixture.commit
        assert run.worktree.path != fixture.source
        assert git(fixture.source, "status", "--porcelain=v1", "--untracked-files=all") == fixture.status
        assert (fixture.source / "untracked.cfg").read_text() == "user untracked\n"
        assert [event["kind"] for event in fixture.repo.get_shell_history(run.run_id)] == [
            "sent", "accepted", "started", "ended"]
        output = {"case": label, "shell": shell, "exit_confirmed": True,
                  "exit_status": expected_exit, "judgment": evidence["judgment"],
                  "reason_codes": evidence["reasons"], "source_status_unchanged": True,
                  "explicit_commit_matched": True, "raw_log_location_recorded": True,
                  "result_location_recorded": True, "shell_event_order": [
                      event["kind"] for event in fixture.repo.get_shell_history(run.run_id)]}
    finally:
        fixture.doCleanups()
    output["parent_pid_residue"] = parent_pid is not None and Path(f"/proc/{parent_pid}").exists()
    assert not output["parent_pid_residue"]
    return output


def run_actual_two_omp():
    """Exercise public OMP events and independently assert both response bindings."""
    repository_root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repository_root / "tests/workflow"),
                    str(repository_root / "tests/bridge"),
                    str(repository_root / "tests/gates/g3_omp")]
    from live_workflow_probe import _worker_provider
    from live_mailbox_probe import _ready, _semantic_provider
    from live_omp_probe import OMP_VERSION, _omp_version, _start_omp, _stop_omps
    from live_tui_draft_probe import _drain_visible
    from live_pause_abort_probe import cwd_processes
    from workbench.contracts.v1 import ActorRole
    from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, TaskMailbox
    from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
    from workbench.workflow import G3WorkerResponsePort, TaskWorkflow

    omp = shutil.which("omp")
    if not omp or _omp_version(omp) != OMP_VERSION:
        return {"status": "inconclusive", "reason": "OMP 18.2.10 unavailable"}
    fixture = WorkflowIndependentTests("test_explicit_commit_and_canonical_target_preserve_dirty_user_work")
    fixture.setUp()
    root = fixture.root
    roles = (ActorRole.MANAGER, ActorRole.WORKER)
    config = root / "config.yml"
    config.write_text("startup:\n  setupWizard: false\n")
    profile = root / "agent"
    profile.mkdir()
    sentinel = "CW10_INDEPENDENT_SECRET_" + uuid4().hex
    providers = {ActorRole.MANAGER: _semantic_provider(),
                 ActorRole.WORKER: _worker_provider()}
    provider_threads = {role: threading.Thread(target=server.serve_forever, daemon=True)
                        for role, server in providers.items()}
    for thread in provider_threads.values():
        thread.start()
    lines = ["providers:"]
    for role in roles:
        lines.extend((f"  cw10-independent-{role.value}:",
                      f"    baseUrl: http://127.0.0.1:{providers[role].server_port}/v1",
                      "    api: openai-completions", "    auth: none", "    models:",
                      "      - id: scripted", f"        name: Independent {role.value}",
                      "        contextWindow: 32768", "        maxTokens: 1024"))
    (profile / "models.yml").write_text("\n".join(lines) + "\n")
    tokens = {role.value: str(uuid4()) for role in roles}
    socket_path = root / "bridge.sock"
    bridge = G3BridgeServer(socket_path, tokens)
    bridge.start()
    mailbox = TaskMailbox(fixture.repo, bridge)
    children, drains, screens = [], [], {}
    active = None
    output = {"status": "inconclusive", "omp_version": _omp_version(omp)}
    try:
        for role in roles:
            child = _start_omp(omp, role.value, tokens[role.value], root,
                               socket_path, config, profile=profile,
                               model=f"cw10-independent-{role.value}/scripted", max_time="60")
            children.append(child)
            fd = int(child["fd"])
            screen = TerminalScreen(100, 30, reply=lambda data, fd=fd: os.write(fd, data))
            stream, lock = make_stream(screen), threading.Lock()
            screens[role] = (screen, lock)
            stop = threading.Event()
            drained = {"role": role.value}
            thread = threading.Thread(target=_drain_visible,
                                      args=(fd, stop, drained, stream, lock), daemon=True)
            thread.start()
            drains.append((thread, stop))
        peers = {role: bridge.peer(role, timeout=15) for role in roles}
        if not _ready(bridge, screens, timeout=15):
            raise RuntimeError("OMP peers did not become ready")
        execution = fixture.strict_execution()
        task = fixture.task(execution)
        port = G3WorkerResponsePort(bridge)
        automation = {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}
        workflow = TaskWorkflow(fixture.repo, mailbox, worker_port=port,
                                automation_source=lambda: automation)
        active = workflow.start(task, 1, worktree_path=root / "actual-execution",
                                artifacts_root=fixture.artifacts, automation=automation,
                                environment_values=fixture.transient_environment(sentinel))
        collected = active.collect(timeout=8)
        judged = active.judge()
        execute, analysis = judged["worker_execution_decision"], judged["worker_analysis_response"]
        assert execute["response_id"] != analysis["response_id"]
        assert execute["stage"] == "execute" and execute["decision"] == "execute"
        assert analysis["stage"] == "analysis" and analysis["decision"] == "success"
        for response, key in ((execute, "worker_request"), (analysis, "worker_analysis_request")):
            message = fixture.repo.get_message(judged[key]["message_id"])
            assert response["task_id"] == message["task_id"] == task
            assert response["revision"] == message["revision"] == 1
            assert response["run_id"] == message["run_id"] == active.run_id
            assert response["message_id"] == message["message_id"]
            assert response["assistant_event_sequence"] < response["delivery_event_sequence"]
            assert response["source"] == "omp_assistant_response"
            assert response["delivery_attempt_id"] and response["session_id"]
            assert type(response["session_generation"]) is int
        assert judged["worker_execution_decision"] == execute
        assert judged["worker_analysis_response"] == analysis
        report = fixture.repo.get_message(judged["report"]["message_id"])
        assert report["content"]["payload"]["worker_response"] == analysis
        assert collected["exit_confirmed"] and collected["exit_status"] == 0
        assert judged["worker_judgment"]["judgment"] == "success"
        assert judged["report"]["status"] == "omp_processed"
        assert git(active.worktree.path, "rev-parse", "HEAD") == fixture.commit
        assert git(fixture.source, "status", "--porcelain=v1", "--untracked-files=all") == fixture.status
        assert [item["kind"] for item in fixture.repo.get_shell_history(active.run_id)] == [
            "sent", "accepted", "started", "ended"]
        assert all(sentinel.encode() not in path.read_bytes()
                   for path in root.rglob("*") if path.is_file())
        output.update({"status": "passed", "task_id": task, "run_id": active.run_id,
                       "omp_pids": {role.value: peers[role].pid for role in roles},
                       "responses": [execute, analysis], "response_ids_distinct": True,
                       "secret_absent_from_all_files": True,
                       "shell_events": ["sent", "accepted", "started", "ended"],
                       "source_status_unchanged": True, "commit_verified": True,
                       "judgment": "success", "exit_status": 0})
    except Exception as exc:
        output.update({"status": "failed", "error_type": type(exc).__name__,
                       "error": str(exc)})
    finally:
        if active is not None:
            active.close()
        _stop_omps(children)
        output["omp_children_remaining"] = sum(Path(f"/proc/{child['pid']}").exists()
                                                for child in children)
        output["omp_cwd_processes_remaining"] = {
            role.value: cwd_processes(root / f"cwd-{role.value}") for role in roles
            if (root / f"cwd-{role.value}").exists()}
        for thread, stop in drains:
            stop.set()
            thread.join(timeout=1)
        for child in children:
            try:
                os.close(int(child["fd"]))
            except OSError:
                pass
        bridge.close()
        output["bridge_socket_removed"] = not socket_path.exists()
        for role in roles:
            providers[role].shutdown()
            providers[role].server_close()
            provider_threads[role].join(timeout=2)
        output["provider_threads_remaining"] = sum(thread.is_alive()
                                                    for thread in provider_threads.values())
        fixture.doCleanups()
    return output


if __name__ == "__main__":
    if "--actual-omp" in sys.argv:
        evidence = run_actual_two_omp()
        print(json.dumps(evidence, sort_keys=True))
        raise SystemExit(0 if evidence["status"] == "passed"
                         and evidence["omp_children_remaining"] == 0
                         and evidence["provider_threads_remaining"] == 0
                         and evidence["bridge_socket_removed"]
                         and all(not pids for pids in evidence["omp_cwd_processes_remaining"].values()) else 1)
    cases = [run_case("success", "printf 'PASS\\n'; printf PASS > result.txt", "bash", "success", 0),
             run_case("nonzero", "printf 'ERROR\\n'; printf BAD > result.txt; exit 9", "bash", "failure", 9),
             run_case("exit_zero_wrong_result", "printf 'PASS\\n'; printf BAD > result.txt", "sh", "failure", 0),
             run_case("no_log", "printf PASS > result.txt", "sh", "indeterminate", 0)]
    print(json.dumps({"status": "passed", "cases": cases}, sort_keys=True))
