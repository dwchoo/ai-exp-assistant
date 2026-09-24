"""Bounded actual OMP RPC checks for busy and native approval delivery contention.

Each mode uses the product bridge, a local scripted provider and disposable cwd.
No model or RPC response body is retained. A pass covers only the named mode.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import BridgeHarness
from live_pause_abort_probe import EXTENSION, VERSION
from live_rpc_pause_probe import RpcCapture, semantic_provider, stop_process
from product_bridge_probe import _provider as write_provider


def report_envelope(peer: dict[str, object]) -> dict[str, object]:
    return {
        "schemaVersion": 1, "messageId": str(uuid4()), "deliveryAttemptId": str(uuid4()),
        "senderRole": "worker", "sessionId": str(peer["session_id"]),
        "sessionGeneration": int(peer["generation"]),
        "taskId": str(uuid4()), "revisionId": str(uuid4()), "runId": str(uuid4()),
        "event": {"type": "message", "messageKind": "report", "payload": {"text": "contention probe"}},
    }


def _run_once(mode: str) -> dict[str, object]:
    omp = shutil.which("omp")
    if not omp:
        return {"result": "inconclusive", "reason": "omp unavailable", "mode": mode}
    version = subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    if version != VERSION:
        return {"result": "inconclusive", "reason": "version mismatch", "version": version, "mode": mode}
    with tempfile.TemporaryDirectory(prefix=f"cw04-g3-{mode}-") as temporary:
        root = Path(temporary)
        cwd = root / "cwd"
        cwd.mkdir()
        profile = root / "agent"
        profile.mkdir()
        (root / "sessions").mkdir()
        config = root / "config.yml"
        config.write_text("# Isolated contention probe overlay.\n")
        provider = semantic_provider(manager=True) if mode == "busy" else write_provider(cwd / "approval.txt")
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "providers:\n"
            "  g3-contention-probe:\n"
            f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
            "    api: openai-completions\n"
            "    auth: none\n"
            "    models:\n"
            "      - id: scripted\n"
            "        name: Scripted G3 contention probe\n"
            "        contextWindow: 32768\n"
            "        maxTokens: 1024\n"
        )
        token = str(uuid4())
        server = BridgeHarness(root / "bridge.sock", {"manager": token})
        os.chmod(root / "bridge.sock", 0o600)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        env = dict(os.environ)
        env.update({
            "PI_CODING_AGENT_DIR": str(profile),
            "WORKBENCH_G3_BRIDGE_SOCKET": str(root / "bridge.sock"),
            "WORKBENCH_G3_ROLE": "manager", "WORKBENCH_G3_TOKEN": token,
            "WORKBENCH_G3_GENERATION": "1", "WORKBENCH_G3_EXPECTED_OMP_VERSION": VERSION,
        })
        args = [
            omp, "--mode", "rpc", "--model", "g3-contention-probe/scripted",
            "--no-session", "--no-pty", "--no-skills", "--no-rules", "--no-title",
            "--no-extensions", "--extension", str(EXTENSION),
            "--tools=bash" if mode == "busy" else "--tools=write",
            "--auto-approve" if mode == "busy" else "--approval-mode=always-ask",
            "--max-time=25", "--cwd", str(cwd),
            "--session-dir", str(root / "sessions"), "--config", str(config),
        ]
        process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        capture = RpcCapture(process)
        evidence: dict[str, object] = {"result": "inconclusive", "mode": mode,
                                       "version": version, "omp_pid": process.pid}
        deadline = time.monotonic() + 28
        def left(limit: float) -> float:
            return max(0.1, min(limit, deadline - time.monotonic()))
        try:
            capture.wait(lambda item: item.get("type") == "ready", timeout=left(10))
            peer = server.peer("manager", timeout=left(10))
            capture.send({"id": "contention-prompt", "type": "prompt",
                          "message": "Run the fixed local contention probe."})
            lifecycle = "tool_execution_start" if mode == "busy" else "tool_approval_requested"
            try:
                server.wait_event("manager", lifecycle, timeout=left(12))
            except TimeoutError:
                evidence["reason"] = f"{lifecycle} not observed"
                return evidence
            evidence["target_lifecycle_observed"] = True
            state = server.request("manager", {"kind": "probe"}, timeout=left(3)).get("state")
            evidence["approval_pending"] = state.get("approvalPending") if isinstance(state, dict) else None
            evidence["target_idle"] = state.get("idle") if isinstance(state, dict) else None
            evidence["target_inflight_tool_count"] = state.get("inFlightToolCount") if isinstance(state, dict) else None
            evidence["provider_requests_before_deliver"] = provider.RequestHandlerClass.requests
            evidence["agent_starts_before_deliver"] = server.event_count("manager", "agent_start")
            if mode == "busy":
                started = cwd / "started.txt"
                start_deadline = time.monotonic() + left(2)
                while not started.exists() and time.monotonic() < start_deadline:
                    time.sleep(0.02)
                evidence["native_tool_wrote_started_file"] = started.is_file() and started.read_text() == "STARTED"
            else:
                evidence["approval_file_absent"] = not (cwd / "approval.txt").exists()
            delivery_state = server.request("manager", {"kind": "probe"}, timeout=left(3)).get("state")
            evidence["idle_at_delivery"] = delivery_state.get("idle") if isinstance(delivery_state, dict) else None
            evidence["inflight_tools_at_delivery"] = delivery_state.get("inFlightToolCount") if isinstance(delivery_state, dict) else None
            evidence["approval_pending_at_delivery"] = delivery_state.get("approvalPending") if isinstance(delivery_state, dict) else None
            evidence["tool_end_before_delivery"] = server.event_count("manager", "tool_execution_end") > 0
            message = report_envelope(peer)
            ack = server.request("manager", {"kind": "deliver", "envelope": json.dumps(message)},
                                 timeout=left(3))
            evidence["deliver_ack"] = ack.get("status")
            evidence["modelProcessedAtAck"] = ack.get("modelProcessed")
            time.sleep(0.2)
            evidence["provider_requests_after_deliver"] = provider.RequestHandlerClass.requests
            evidence["agent_starts_after_deliver"] = server.event_count("manager", "agent_start")
            evidence["new_model_turn_observed"] = (
                evidence["provider_requests_after_deliver"] > evidence["provider_requests_before_deliver"]
                or evidence["agent_starts_after_deliver"] > evidence["agent_starts_before_deliver"]
            )
            if mode == "approval":
                after_state = server.request("manager", {"kind": "probe"}, timeout=left(3)).get("state")
                evidence["approval_pending_after_window"] = after_state.get("approvalPending") if isinstance(after_state, dict) else None
                evidence["approval_file_absent_after_window"] = not (cwd / "approval.txt").exists()
            evidence["rpc_stdout_bytes_drained"] = capture.bytes
            evidence["result"] = ("passed_busy" if mode == "busy" else "passed_approval") if (
                evidence["target_lifecycle_observed"]
                and evidence["deliver_ack"] == "deferred"
                and evidence["new_model_turn_observed"] is False
                and evidence["provider_requests_before_deliver"] == 1
                and evidence["agent_starts_before_deliver"] == 1
                and (evidence.get("native_tool_wrote_started_file") is True
                     and evidence["idle_at_delivery"] is False
                     and isinstance(evidence["inflight_tools_at_delivery"], int)
                     and evidence["inflight_tools_at_delivery"] >= 1
                     and evidence["tool_end_before_delivery"] is False
                     if mode == "busy" else
                     evidence["approval_pending"] is True
                     and evidence["approval_pending_at_delivery"] is True
                     and evidence["approval_pending_after_window"] is True
                     and evidence["approval_file_absent"] is True
                     and evidence["approval_file_absent_after_window"] is True)
            ) else "inconclusive"
            return evidence
        except (TimeoutError, OSError, BrokenPipeError) as error:
            evidence["reason"] = type(error).__name__
            return evidence
        finally:
            evidence.update(stop_process(process, cwd))
            capture.thread.join(timeout=2)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            provider.shutdown()
            provider.server_close()
            provider_thread.join(timeout=2)


def run(mode: str) -> dict[str, object]:
    evidence = _run_once(mode)
    if (evidence.get("result") != f"passed_{mode}"
            or evidence.get("omp_exit") != 0
            or evidence.get("cwd_processes_after_cleanup") != []):
        evidence["result"] = "inconclusive"
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, choices=("busy", "approval"))
    scenario = parser.parse_args().scenario
    observation = run(scenario)
    print(json.dumps(observation, ensure_ascii=False))
    raise SystemExit(0 if observation["result"] == f"passed_{scenario}" else 1)
