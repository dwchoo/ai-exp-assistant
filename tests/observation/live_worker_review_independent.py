"""Independent real-clock CW-11 review and 13th peer wake against OMP 18.2.10."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from uuid import UUID, uuid4, uuid5

TESTS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TESTS / "bridge"), str(TESTS / "gates" / "g3_omp"),
                str(TESTS / "workflow")]
from live_omp_probe import OMP_VERSION, _drain_pty, _omp_version, _start_omp, _stop_omps  # noqa: E402
from live_workflow_probe import _worker_provider, git  # noqa: E402
from workbench.contracts.v1 import ActorRole, MessageKind  # noqa: E402
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, MailboxStatus, TaskMailbox  # noqa: E402
from workbench.observation.worker_review import (  # noqa: E402
    ActiveRunRef, SerializedReviewAdmission, WorkerReviewScheduler,
)
from workbench.observation.workflow_binding import WorkflowObservationBinding  # noqa: E402
from workbench.tasks.repository import TaskRepository  # noqa: E402
from workbench.workflow import G3WorkerResponsePort, TaskWorkflow  # noqa: E402


def run() -> dict[str, object]:
    omp = shutil.which("omp")
    result: dict[str, object] = {"status": "inconclusive", "omp_version": None}
    if not omp:
        result["reason"] = "omp_unavailable"
        return result
    result["omp_version"] = _omp_version(omp)
    if result["omp_version"] != OMP_VERSION:
        result["reason"] = "omp_version_mismatch"
        return result

    with tempfile.TemporaryDirectory(prefix="cw11-independent-") as temporary:
        root = Path(temporary)
        socket_path = root / "bridge.sock"
        (root / "config.yml").write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        provider = _worker_provider()
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "providers:\n  cw11-independent:\n"
            f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: Independent Review\n"
            "        contextWindow: 32768\n        maxTokens: 1024\n"
        )
        token = str(uuid4())
        bridge = G3BridgeServer(socket_path, {"manager": str(uuid4()), "worker": token})
        bridge.start()
        repo = TaskRepository(root / "tasks.sqlite3")
        children = []
        drains = []
        workflow_run = None
        secret = "CW11_PRIVATE_FACT_" + uuid4().hex
        stage = "setup"
        try:
            stage = "task_and_omp"
            task_id = repo.create_task({"goal": "independent review", "allowed_changes": []})
            repo.approve_scope(task_id, 1, {"paths": [], "commands": []})
            repo.proceed(task_id, 1, "bounded independent review")
            run_id = repo.start_run(task_id, 1)
            mailbox = TaskMailbox(repo, bridge)
            # OMP daemon state belongs to this disposable probe, including in
            # restricted workspaces where the user's global ~/.omp is read-only.
            config_dir = os.path.relpath(root / "omp-state", Path.home())
            previous_config = os.environ.get("PI_CONFIG_DIR")
            os.environ["PI_CONFIG_DIR"] = config_dir
            try:
                child = _start_omp(omp, "worker", token, root, socket_path,
                                   root / "config.yml", profile=profile,
                                   model="cw11-independent/scripted", max_time="180")
            finally:
                if previous_config is None:
                    os.environ.pop("PI_CONFIG_DIR", None)
                else:
                    os.environ["PI_CONFIG_DIR"] = previous_config
            children.append(child)
            stop = threading.Event()
            drain_state: dict[str, object] = {"role": "worker", "bytes": 0}
            drain = threading.Thread(target=_drain_pty,
                                     args=(int(child["fd"]), stop, drain_state), daemon=True)
            drain.start()
            drains.append((drain, stop, drain_state))
            peer = bridge.peer(ActorRole.WORKER, timeout=15)
            assert peer.pid == child["pid"]

            def wait_ready(timeout=15):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    state = bridge.probe(ActorRole.WORKER, timeout=2)
                    if (state.get("idle") is True and state.get("pending") is False
                            and state.get("approvalPending") is False
                            and state.get("editorKnown") is True and state.get("editorEmpty") is True
                            and type(state.get("inFlightToolCount")) is int
                            and state.get("inFlightToolCount") == 0 and state.get("paused") is False):
                        return state
                    time.sleep(0.05)
                raise AssertionError("worker never became idle-ready")

            wait_ready()
            stage = "periodic_review"
            ref = ActiveRunRef(task_id, str(uuid5(UUID(task_id), "task-spec-revision:1")),
                               1, run_id, peer.session_id, peer.generation)
            automation = {"portVersion": 2, "kind": "AutomationState", "payload": {
                "paused": False, "cancelled": False, "metadataHealthy": True,
                "approvalValid": True,
            }}
            collections = []
            deliveries = []

            def collect(_run):
                collections.append(time.monotonic())
                return {"phase": "running", "process_alive": True,
                        "exit_confirmed": False, "unknowns": ["process_tree_unobserved"],
                        "raw_log_excerpt": secret}

            def dispatch(request):
                assert secret not in repr(request.facts)
                assert request.facts["hang_investigation"]["classification"] == "unknown"
                assert request.facts["usage"]["tokens_observed"] == "unknown"
                assert request.facts["usage"]["tokens_estimated"] == "unknown"
                message = mailbox.create_message(
                    task_id, 1, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                    MessageKind.QUESTION,
                    {"stage": "periodic_review", "facts": dict(request.facts),
                     "instruction": "Review these public run facts without tools."},
                )
                receipt = mailbox.deliver(message, timeout=30)
                deliveries.append(receipt.status.value)
                return {"status": receipt.status.value}

            admission = SerializedReviewAdmission(ref, automation)
            exits = []
            scheduler = WorkerReviewScheduler(
                admission=admission,
                automation_state=lambda: automation, active_run=lambda: ref,
                worker_state=lambda _run: bridge.probe(ActorRole.WORKER, timeout=2),
                collect_non_model=collect, dispatch_review=dispatch,
                investigate_non_model=lambda _run, _budget: {
                    "process_evidence": [],
                    "lifecycle_evidence": {"phase": "running", "exit_confirmed": False},
                    "silence_seconds": 86_400,
                    "unknowns": ["process_tree_unobserved"],
                },
                collect_usage=lambda _run: None,
                user_priority=lambda _run: False, on_exit=lambda event: exits.append(dict(event)),
                clock=time.monotonic,
            )
            started = time.monotonic()
            first = scheduler.tick()
            assert first.status == "waiting" and first.next_due_at is not None
            while time.monotonic() < first.next_due_at - 0.05:
                time.sleep(min(1, first.next_due_at - 0.05 - time.monotonic()))
            before = scheduler.tick()
            assert before.status == "waiting" and not deliveries
            while time.monotonic() < first.next_due_at:
                time.sleep(min(0.02, first.next_due_at - time.monotonic()))
            review = scheduler.tick()
            assert review.status == "dispatched" and review.review_count == 1
            assert deliveries == [MailboxStatus.OMP_PROCESSED.value]
            assert review.last_review_at is not None
            assert review.hang_investigation["classification"] == "unknown"
            assert review.usage["tokens_unknown"] == "unknown"
            wait_ready()
            stage = "peer_wakes"
            wakes = []
            for ordinal in range(1, 14):
                message = mailbox.create_message(
                    task_id, 1, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                    MessageKind.QUESTION,
                    {"stage": "normal_peer_wake", "ordinal": ordinal,
                     "instruction": "Acknowledge the wake briefly without tools."},
                )
                receipt = mailbox.deliver(message, timeout=30)
                wakes.append(receipt.status.value)
                assert receipt.status is MailboxStatus.OMP_PROCESSED, ordinal
                wait_ready()
            assert provider.request_count == 14
            stage = "workflow_start"
            source = root / "source"
            source.mkdir()
            git(source, "init", "-q")
            git(source, "config", "user.email", "cw11@example.invalid")
            git(source, "config", "user.name", "CW11 Probe")
            (source / "tracked.txt").write_text("base\n")
            git(source, "add", "tracked.txt")
            git(source, "commit", "-qm", "probe baseline")
            commit = git(source, "rev-parse", "HEAD")
            (root / "artifacts").mkdir()
            execution = {
                "source": str(source), "commit": commit,
                "command": "printf 'PASS\\n'; printf PASS > outcome.txt",
                "criteria": {"log_contains": "PASS", "result_file": "outcome.txt",
                             "result_contains": "PASS"},
                "environment": ["PATH", "TERM"], "shell": "bash",
            }
            workflow_task = repo.create_task({"goal": "confirmed CW11 exit", "execution": execution})
            repo.approve_scope(workflow_task, 1, {"execution": execution, "paths": ["outcome.txt"]})
            repo.proceed(workflow_task, 1, "approved live observation")
            workflow = TaskWorkflow(repo, mailbox, worker_port=G3WorkerResponsePort(bridge),
                                    automation_source=lambda: automation)
            workflow_run = workflow.start(
                workflow_task, 1, worktree_path=root / "execution",
                artifacts_root=root / "artifacts", automation=automation,
                environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm"},
            )
            ref = WorkflowObservationBinding.resolve_run(workflow_run)
            stage = "workflow_collect"
            assert ref.session_id == peer.session_id
            exit_source = scheduler.activate_run_source(ref, workflow_run)
            assert exit_source is not None
            binding = WorkflowObservationBinding.attach(workflow_run)
            assert binding.bind(scheduler, exit_source)
            exit_started = time.monotonic()
            exit_record, notified = binding.collect_and_notify(scheduler, timeout=8)
            exit_elapsed = time.monotonic() - exit_started
            assert notified and exit_record["exit_confirmed"] is True
            assert exit_elapsed < 60 and len(exits) == 1
            assert exits[0]["session_id"] == binding.run.session_id
            assert exits[0]["run_id"] == workflow_run.run_id
            assert scheduler.tick().status == "exited"
            assert all(secret.encode() not in path.read_bytes()
                       for path in root.rglob("*") if path.is_file())
            result.update({"status": "passed", "real_elapsed_before_review":
                           round(review.last_review_at - started, 3),
                           "review_count": review.review_count,
                           "thirteenth_wake": wakes[-1], "peer_wakes": len(wakes),
                           "provider_requests": provider.request_count,
                           "non_model_collections": len(collections),
                           "silence_only_hang": review.hang_investigation["classification"],
                           "usage_without_provider_metrics": review.usage["tokens_unknown"],
                           "workflow_exit_notified": notified,
                           "workflow_exit_elapsed": round(exit_elapsed, 3),
                           "workflow_exit_identity": exits[0]["session_id"] == peer.session_id,
                           "secret_absent": True, "worker_pid": peer.pid})
        except Exception as exc:
            result.update({"status": "failed", "error_type": type(exc).__name__,
                           "error": str(exc), "stage": stage,
                           "provider_requests": provider.request_count})
        finally:
            if workflow_run is not None:
                workflow_run.close()
            _stop_omps(children)
            result["omp_children_remaining"] = sum(
                Path(f"/proc/{child['pid']}").exists() for child in children)
            for thread, stop, _state in drains:
                stop.set()
                thread.join(timeout=1)
            result["pty_drain_bytes"] = sum(int(state.get("bytes", 0))
                                            for _, _, state in drains)
            for child in children:
                try:
                    os.close(int(child["fd"]))
                except OSError:
                    pass
            bridge.close()
            repo.close()
            provider.shutdown()
            provider.server_close()
            provider_thread.join(timeout=2)
            result["provider_thread_remaining"] = provider_thread.is_alive()
            result["bridge_socket_remaining"] = socket_path.exists()
    return result


if __name__ == "__main__":
    evidence = run()
    print(json.dumps(evidence, sort_keys=True))
    raise SystemExit(0 if evidence.get("status") == "passed"
                     and evidence.get("omp_children_remaining") == 0
                     and evidence.get("provider_thread_remaining") is False
                     and evidence.get("bridge_socket_remaining") is False else 1)
