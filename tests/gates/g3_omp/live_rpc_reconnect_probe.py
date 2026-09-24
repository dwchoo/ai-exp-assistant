"""Bounded actual OMP RPC ACK-loss and bridge reconnect probe.

Only the host-side bridge socket is disconnected. The OMP process/session stays
alive. Provider request bodies are inspected in memory for exact message IDs;
only booleans/digests and event counts are retained.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import BridgeHarness
from live_pause_abort_probe import EXTENSION, VERSION
from live_rpc_pause_probe import RpcCapture, semantic_provider, stop_process


def wait_reconnected(server: BridgeHarness, old_peer: dict[str, object], timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    with server.condition:
        while time.monotonic() < deadline:
            current = server.peers.get("worker")
            if current is not None and current is not old_peer:
                return current
            server.condition.wait(deadline - time.monotonic())
    raise TimeoutError("bridge did not reconnect with a new hello")


def accepted_ack_was_discarded(
    metadata: list[dict[str, object]], request_id: str, available_acks: dict[str, object]
) -> bool:
    return (metadata == [{"request_id": request_id, "status": "api_accepted"}]
            and request_id not in available_acks)


def _run_once() -> dict[str, object]:
    omp = shutil.which("omp")
    if not omp:
        return {"result": "inconclusive", "reason": "omp unavailable"}
    version = subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    if version != VERSION:
        return {"result": "inconclusive", "reason": "version mismatch", "version": version}
    with tempfile.TemporaryDirectory(prefix="cw04-g3-reconnect-") as temporary:
        root = Path(temporary)
        cwd = root / "cwd"
        cwd.mkdir()
        profile = root / "agent"
        profile.mkdir()
        (root / "sessions").mkdir()
        config = root / "config.yml"
        config.write_text("# Isolated reconnect probe overlay.\n")
        provider = semantic_provider(manager=False)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "providers:\n"
            "  g3-reconnect-probe:\n"
            f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
            "    api: openai-completions\n"
            "    auth: none\n"
            "    models:\n"
            "      - id: scripted\n"
            "        name: Scripted G3 reconnect probe\n"
            "        contextWindow: 32768\n"
            "        maxTokens: 1024\n"
        )
        token = str(uuid4())
        server = BridgeHarness(root / "bridge.sock", {"worker": token})
        os.chmod(root / "bridge.sock", 0o600)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        env = dict(os.environ)
        env.update({
            "PI_CODING_AGENT_DIR": str(profile),
            "WORKBENCH_G3_BRIDGE_SOCKET": str(root / "bridge.sock"),
            "WORKBENCH_G3_ROLE": "worker", "WORKBENCH_G3_TOKEN": token,
            "WORKBENCH_G3_GENERATION": "1", "WORKBENCH_G3_EXPECTED_OMP_VERSION": VERSION,
        })
        process = subprocess.Popen([
            omp, "--mode", "rpc", "--model", "g3-reconnect-probe/scripted",
            "--no-session", "--no-pty", "--no-tools", "--no-skills",
            "--no-rules", "--no-title", "--no-extensions", "--extension", str(EXTENSION),
            "--max-time=25", "--cwd", str(cwd),
            "--session-dir", str(root / "sessions"), "--config", str(config),
        ], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
        capture = RpcCapture(process)
        evidence: dict[str, object] = {"result": "inconclusive", "version": version,
                                       "omp_pid": process.pid}
        try:
            capture.wait(lambda frame: frame.get("type") == "ready", timeout=10)
            peer = server.peer("worker", timeout=10)
            message_id = str(uuid4())
            envelope = {
                "schemaVersion": 1, "messageId": message_id,
                "deliveryAttemptId": str(uuid4()), "senderRole": "manager",
                "sessionId": str(peer["session_id"]),
                "sessionGeneration": int(peer["generation"]),
                "taskId": str(uuid4()), "revisionId": str(uuid4()), "runId": str(uuid4()),
                "event": {"type": "message", "messageKind": "task",
                          "payload": {"text": "One local reconnect task"}},
            }
            provider.semantic_expected[message_id] = {
                "workbench_message_id": message_id, "kind": "task",
                "task_id": envelope["taskId"], "revision_id": envelope["revisionId"],
                "run_id": envelope["runId"],
            }
            first_request_id = str(uuid4())
            first_delivery_attempt_id = envelope["deliveryAttemptId"]
            server.dropped_ack_ids.add(first_request_id)
            first_frame = {"kind": "deliver", "requestId": first_request_id,
                           "envelope": json.dumps(envelope)}
            with peer["write_lock"]:
                peer["socket"].sendall((json.dumps(first_frame) + "\n").encode())
            server.wait_event("worker", "agent_end", timeout=12)
            semantic = provider.semantic_seen.get(message_id)
            evidence["message_id"] = message_id
            evidence["provider_semantics_match"] = semantic is not None and semantic["matched"] is True
            evidence["provider_fields_sha256"] = semantic["fields_sha256"] if semantic else None
            evidence["provider_requests_before_reconnect"] = provider.RequestHandlerClass.requests
            evidence["agent_ends_before_reconnect"] = server.event_count("worker", "agent_end")
            with server.condition:
                evidence["first_ack_available_to_host"] = first_request_id in server.acks
                evidence["first_accepted_ack_received_and_discarded"] = accepted_ack_was_discarded(
                    server.discarded_ack_metadata, first_request_id, server.acks)
                evidence["discarded_ack_count"] = len(server.discarded_ack_metadata)
            evidence["omp_alive_before_disconnect"] = process.poll() is None
            try:
                peer["socket"].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer["socket"].close()
            new_peer = wait_reconnected(server, peer, timeout=5)
            evidence["new_hello_observed"] = new_peer is not peer
            evidence["same_omp_pid"] = new_peer["pid"] == peer["pid"] == process.pid
            evidence["same_session_id"] = new_peer["session_id"] == peer["session_id"]
            evidence["same_session_generation"] = new_peer["generation"] == peer["generation"]
            evidence["omp_alive_after_reconnect"] = process.poll() is None
            envelope["deliveryAttemptId"] = str(uuid4())
            evidence["new_delivery_attempt_id"] = envelope["deliveryAttemptId"] != first_delivery_attempt_id
            retry = server.request("worker", {"kind": "deliver", "envelope": json.dumps(envelope)}, timeout=3)
            evidence["retry_status"] = retry.get("status")
            time.sleep(0.2)
            evidence["provider_requests_after_retry"] = provider.RequestHandlerClass.requests
            evidence["agent_ends_after_retry"] = server.event_count("worker", "agent_end")
            evidence["rpc_stdout_bytes_drained"] = capture.bytes
            evidence["semantic_diagnostics"] = provider.semantic_diagnostics
            evidence["result"] = "passed_ack_loss_reconnect" if (
                evidence["provider_semantics_match"]
                and evidence["provider_requests_before_reconnect"] == 1
                and evidence["agent_ends_before_reconnect"] == 1
                and evidence["first_accepted_ack_received_and_discarded"]
                and evidence["discarded_ack_count"] == 1
                and evidence["first_ack_available_to_host"] is False
                and evidence["omp_alive_before_disconnect"]
                and evidence["new_hello_observed"]
                and evidence["same_omp_pid"]
                and evidence["same_session_id"]
                and evidence["same_session_generation"]
                and evidence["omp_alive_after_reconnect"]
                and evidence["new_delivery_attempt_id"]
                and evidence["retry_status"] == "duplicate_api_accepted"
                and evidence["provider_requests_after_retry"] == 1
                and evidence["agent_ends_after_retry"] == 1
            ) else "inconclusive"
            return evidence
        except (TimeoutError, OSError, BrokenPipeError) as error:
            evidence["reason"] = type(error).__name__
            evidence["provider_requests"] = provider.RequestHandlerClass.requests
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


def run() -> dict[str, object]:
    evidence = _run_once()
    if (evidence.get("result") != "passed_ack_loss_reconnect"
            or evidence.get("omp_exit") != 0
            or evidence.get("cwd_processes_after_cleanup") != []):
        evidence["result"] = "inconclusive"
    return evidence


if __name__ == "__main__":
    observation = run()
    print(json.dumps(observation, ensure_ascii=False))
    raise SystemExit(0 if observation["result"] == "passed_ack_loss_reconnect" else 1)
