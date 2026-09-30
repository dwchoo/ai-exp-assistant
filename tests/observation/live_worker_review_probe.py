"""Bounded periodic-review and 13th peer-wake probe against a real OMP TUI.

The scripted local provider retains request counts and booleans only. It does
not retain prompts, model responses, account state, or provider credentials.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from uuid import uuid5, UUID

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS / "gates" / "g3_omp"))
sys.path.insert(0, str(_TESTS / "bridge"))

from live_omp_probe import OMP_VERSION, _drain_pty, _omp_version, _start_omp, _stop_omps  # noqa: E402
from live_mailbox_probe import _semantic_provider  # noqa: E402
from workbench.contracts.v1 import ActorRole, MessageKind  # noqa: E402
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, MailboxStatus, TaskMailbox  # noqa: E402
from workbench.observation.worker_review import (  # noqa: E402
    ActiveRunRef,
    SerializedReviewAdmission,
    WorkerReviewScheduler,
)
from workbench.tasks.repository import TaskRepository  # noqa: E402


AUTOMATION = {
    "portVersion": 2,
    "kind": "AutomationState",
    "payload": {
        "paused": False,
        "cancelled": False,
        "metadataHealthy": True,
        "approvalValid": True,
    },
}


def _worker_ready(bridge: G3BridgeServer, timeout: float = 15.0) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = bridge.probe(ActorRole.WORKER, timeout=2)
        except Exception:
            time.sleep(0.05)
            continue
        if (
            state.get("idle") is True
            and state.get("pending") is False
            and state.get("approvalPending") is False
            and state.get("editorKnown") is True
            and state.get("editorEmpty") is True
            and type(state.get("inFlightToolCount")) is int
            and state.get("inFlightToolCount") == 0
            and state.get("paused") is False
        ):
            return state
        time.sleep(0.05)
    return None


def run(omp: str | None = None) -> dict[str, object]:
    omp = omp or shutil.which("omp")
    result: dict[str, object] = {
        "result": "inconclusive",
        "probe": "cw11-periodic-review-and-13th-peer-wake",
        "omp_version": "unknown",
        "periodic_review_status": "not_started",
        "normal_peer_wakes_processed": 0,
        "provider_request_count": 0,
        "omp_children_remaining": None,
        "pty_drain_bytes": 0,
    }
    if not omp:
        result.update({"result": "blocked", "reason": "omp_not_found"})
        return result

    version = _omp_version(omp)
    result["omp_version"] = version
    if version != OMP_VERSION:
        result.update({"result": "blocked", "reason": "unsupported_omp_version"})
        return result

    with tempfile.TemporaryDirectory(prefix="cw11-worker-review-") as temporary:
        root = Path(temporary)
        socket_path = root / "bridge.sock"
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n", encoding="utf-8")
        profile = root / "agent"
        profile.mkdir()
        provider = _semantic_provider()
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "\n".join((
                "providers:",
                "  cw11-review-worker:",
                f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1",
                "    api: openai-completions",
                "    auth: none",
                "    models:",
                "      - id: scripted",
                "        name: CW-11 scripted worker",
                "        contextWindow: 32768",
                "        maxTokens: 1024",
                "",
            )),
            encoding="utf-8",
        )

        token = "cw11-worker-review-token"
        bridge = G3BridgeServer(socket_path, {
            "manager": "cw11-manager-unused-token",
            "worker": token,
        })
        bridge.start()
        children: list[dict[str, object]] = []
        drains: list[tuple[threading.Thread, threading.Event, dict[str, object]]] = []
        repository = TaskRepository(root / "tasks.sqlite3")
        try:
            task_id = repository.create_task({"goal": "bounded CW-11 review probe", "allowed_changes": []})
            repository.approve_scope(task_id, 1, {"paths": [], "commands": []})
            repository.proceed(task_id, 1, "run isolated OMP review probe")
            run_id = repository.start_run(task_id, 1)
            mailbox = TaskMailbox(repository, bridge)

            config_dir = os.path.relpath(root / "omp-state", Path.home())
            previous_config = os.environ.get("PI_CONFIG_DIR")
            os.environ["PI_CONFIG_DIR"] = config_dir
            try:
                child = _start_omp(
                    omp,
                    "worker",
                    token,
                    root,
                    socket_path,
                    config,
                    profile=profile,
                    model="cw11-review-worker/scripted",
                    max_time="240",
                )
            finally:
                if previous_config is None:
                    os.environ.pop("PI_CONFIG_DIR", None)
                else:
                    os.environ["PI_CONFIG_DIR"] = previous_config
            children.append(child)
            stop = threading.Event()
            drained: dict[str, object] = {"role": "worker", "bytes": 0}
            drain = threading.Thread(
                target=_drain_pty,
                args=(int(child["fd"]), stop, drained),
                daemon=True,
            )
            drain.start()
            drains.append((drain, stop, drained))

            peer = bridge.peer(ActorRole.WORKER, timeout=20)
            if peer.pid != int(child["pid"]):
                result.update({"result": "failed", "reason": "worker_peer_pid_mismatch"})
                return result
            if _worker_ready(bridge) is None:
                result.update({"result": "failed", "reason": "worker_not_idle_ready"})
                return result

            active_ref = ActiveRunRef(
                task_id=task_id,
                revision_id=str(uuid5(UUID(task_id), "task-spec-revision:1")),
                revision=1,
                run_id=run_id,
                session_id=peer.session_id,
                session_generation=peer.generation,
            )
            fake_now = [100.0]
            deliveries: list[str] = []

            def dispatch_review(request) -> dict[str, str]:
                message = mailbox.create_message(
                    task_id,
                    1,
                    run_id,
                    ActorRole.MANAGER,
                    ActorRole.WORKER,
                    MessageKind.QUESTION,
                    {
                        "stage": "periodic_review",
                        "instruction": "Review the active run using only the listed process/lifecycle facts. Do not use tools.",
                        "facts": dict(request.facts),
                    },
                )
                receipt = mailbox.deliver(message, timeout=30)
                deliveries.append(receipt.status.value)
                return {"status": receipt.status.value}

            scheduler = WorkerReviewScheduler(
                admission=SerializedReviewAdmission(active_ref, AUTOMATION),
                automation_state=lambda: AUTOMATION,
                active_run=lambda: active_ref,
                worker_state=lambda _run: bridge.probe(ActorRole.WORKER, timeout=3),
                collect_non_model=lambda _run: {
                    "phase": "running" if repository.get_current_run(task_id) is not None else "unknown",
                    "exit_confirmed": False,
                    "unknowns": ["shell_process_not_attached_to_runtime_probe"],
                },
                dispatch_review=dispatch_review,
                user_priority=lambda _run: False,
                on_exit=lambda _event: None,
                clock=lambda: fake_now[0],
            )
            first_tick = scheduler.tick()
            fake_now[0] += 60
            review_tick = scheduler.tick()
            result["first_tick_status"] = first_tick.status
            result["periodic_review_status"] = deliveries[0] if deliveries else "not_processed"
            result["scheduler_review_status"] = review_tick.status
            result["active_task_run_observed"] = repository.get_current_run(task_id) is not None
            result["worker_session_generation"] = peer.generation
            result["review_used_sanitized_facts"] = deliveries[:1] == [MailboxStatus.OMP_PROCESSED.value]
            if review_tick.status != "dispatched" or not deliveries or deliveries[0] != MailboxStatus.OMP_PROCESSED.value:
                result.update({"result": "failed", "reason": "periodic_review_not_omp_processed"})
                return result
            if _worker_ready(bridge) is None:
                result.update({"result": "failed", "reason": "worker_not_ready_after_periodic_review"})
                return result

            normal_wakes: list[str] = []
            for ordinal in range(1, 14):
                message = mailbox.create_message(
                    task_id,
                    1,
                    run_id,
                    ActorRole.MANAGER,
                    ActorRole.WORKER,
                    MessageKind.QUESTION,
                    {
                        "stage": "normal_peer_wake",
                        "ordinal": ordinal,
                        "instruction": "Acknowledge this normal peer wake briefly. Do not use tools.",
                    },
                )
                receipt = mailbox.deliver(message, timeout=30)
                normal_wakes.append(receipt.status.value)
                if receipt.status is not MailboxStatus.OMP_PROCESSED:
                    result.update({
                        "result": "failed",
                        "reason": f"normal_peer_wake_{ordinal}_not_processed",
                        "failed_status": receipt.status.value,
                    })
                    return result
                if _worker_ready(bridge) is None:
                    result.update({"result": "failed", "reason": f"worker_not_ready_after_peer_wake_{ordinal}"})
                    return result

            result["normal_peer_wakes_processed"] = len(normal_wakes)
            result["thirteenth_normal_peer_wake_status"] = normal_wakes[-1]
            result["review_count"] = review_tick.review_count
            result["provider_request_count"] = provider.request_count
            if len(normal_wakes) != 13 or normal_wakes[-1] != MailboxStatus.OMP_PROCESSED.value:
                result.update({"result": "failed", "reason": "thirteenth_peer_wake_not_processed"})
            else:
                result["result"] = "passed_actual_omp_18_2_10_review_and_13_peer_wakes"
        except Exception as error:
            result.update({"result": "runtime_exception", "error_type": type(error).__name__})
        finally:
            _stop_omps(children)
            result["omp_children_remaining"] = sum(
                (Path("/proc") / str(child["pid"])).exists() for child in children
            )
            for thread, stop, _state in drains:
                stop.set()
                thread.join(timeout=1)
            result["pty_drain_bytes"] = sum(int(state.get("bytes", 0)) for _, _, state in drains)
            for child in children:
                try:
                    os.close(int(child["fd"]))
                except OSError:
                    pass
            bridge.close()
            repository.close()
            provider.shutdown()
            provider.server_close()
            provider_thread.join(timeout=1)
            result["provider_request_count"] = provider.request_count
            result["provider_stopped"] = not provider_thread.is_alive()
    return result


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
