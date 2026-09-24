"""Bounded two-TUI bridge outage and extension recovery observation.

The disposable host listener is removed during the fault. No message is sent
while its status is unknown. Provider bodies and terminal output stay in memory.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import BridgeHarness, OMP_VERSION, _omp_version, _start_omp, _stop_omps
from live_pause_abort_probe import cwd_processes
from live_rpc_pause_probe import semantic_provider
from live_tui_draft_probe import _drain_visible, _screen_state
from live_tui_message_kinds_probe import _new_end, _ready
from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageEvent, MessageKind
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


ROLES = ("manager", "worker")


def _wait_screen(
    screen: TerminalScreen, lock: threading.Lock, draft: str, *, present: bool,
    timeout: float = 5,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _screen_state(screen, lock, draft)
        if state["composer_visible"] and (
            state["draft_in_composer"] if present else not state["draft_visible"]
        ):
            return True
        time.sleep(0.05)
    return False


def _deliver(
    server: BridgeHarness, providers: dict[str, object], peers: dict[str, dict[str, object]],
    role: str, kind: MessageKind, sender: ActorRole, reply_to: str | None = None,
) -> dict[str, object]:
    peer = peers[role]
    envelope = ControlEnvelope(
        message_id=str(uuid4()), delivery_attempt_id=str(uuid4()),
        sender_role=sender, session_id=str(peer["session_id"]),
        session_generation=int(peer["generation"]),
        task_id=str(uuid4()), revision_id=str(uuid4()), run_id=str(uuid4()),
        event=MessageEvent(kind, {"text": "Reply briefly. Do not use tools."}, reply_to),
    )
    expected = {
        "workbench_message_id": envelope.message_id,
        "kind": kind.value,
        "task_id": envelope.task_id,
        "revision_id": envelope.revision_id,
        "run_id": envelope.run_id,
    }
    if reply_to is not None:
        expected["in_reply_to_message_id"] = reply_to
    providers[role].semantic_expected[envelope.message_id] = expected
    before_requests = {
        item: providers[item].RequestHandlerClass.requests for item in ROLES
    }
    before_ends = {item: server.event_count(item, "agent_end") for item in ROLES}
    with server.condition:
        start_index = len(server.events)
    ack = server.request(role, {"kind": "deliver", "envelope": envelope.to_json()}, timeout=5)
    end_count, end_session_match = _new_end(server, role, peer, start_index, timeout=15)
    after_requests = {
        item: providers[item].RequestHandlerClass.requests for item in ROLES
    }
    after_ends = {item: server.event_count(item, "agent_end") for item in ROLES}
    other = "manager" if role == "worker" else "worker"
    record = {
        "message_id": envelope.message_id,
        "ack_status": ack.get("status"),
        "model_processed_at_ack": ack.get("modelProcessed"),
        "provider_fields_match": providers[role].semantic_seen.get(envelope.message_id, {}).get("matched") is True,
        "target_provider_delta": after_requests[role] - before_requests[role],
        "other_provider_delta": after_requests[other] - before_requests[other],
        "target_agent_end_delta": after_ends[role] - before_ends[role],
        "other_agent_end_delta": after_ends[other] - before_ends[other],
        "end_session_match": end_count == 1 and end_session_match,
    }
    record["passed"] = (
        record["ack_status"] == "api_accepted"
        and record["model_processed_at_ack"] is False
        and record["provider_fields_match"]
        and record["target_provider_delta"] == 1
        and record["other_provider_delta"] == 0
        and record["target_agent_end_delta"] == 1
        and record["other_agent_end_delta"] == 0
        and record["end_session_match"]
    )
    return record


def _malformed_frame(
    server: BridgeHarness, peers: dict[str, dict[str, object]],
    providers: dict[str, object], screens: dict[str, tuple[TerminalScreen, threading.Lock]],
    children: list[dict[str, object]], result: dict[str, object],
) -> None:
    """Send one unparsable host frame to worker, then test the same connection."""
    worker = peers["worker"]
    malformed_id = str(uuid4())
    # A trailing comma makes this invalid JSON. The request ID is retained only
    # for checking that no API ACK is falsely attributed to the bad frame.
    bad_frame = f'{{"kind":"deliver","requestId":"{malformed_id}",}}\n'.encode()
    with worker["write_lock"]:
        worker["socket"].sendall(bad_frame)
    # A valid probe on the same socket is a processing boundary after the bad
    # frame; it also proves the extension can still answer host requests.
    worker_state = server.request("worker", {"kind": "probe"}, timeout=5)["state"]
    manager_state = server.request("manager", {"kind": "probe"}, timeout=5)["state"]
    with server.condition:
        result["malformed_frame_no_api_ack"] = malformed_id not in server.acks
    result["same_worker_connection_after_malformed"] = (
        server.peer("worker")["socket"] is worker["socket"]
        and worker_state.get("sessionId") == worker["session_id"]
        and worker_state.get("generation") == worker["generation"]
    )
    result["manager_binding_after_malformed"] = (
        server.peer("manager")["socket"] is peers["manager"]["socket"]
        and manager_state.get("sessionId") == peers["manager"]["session_id"]
        and manager_state.get("generation") == peers["manager"]["generation"]
    )
    draft = f"malformed-{uuid4().hex}"
    for char in draft.encode():
        os.write(int(children[1]["fd"]), bytes((char,)))
        time.sleep(0.01)
    result["worker_composer_usable_after_malformed"] = _wait_screen(
        *screens["worker"], draft, present=True,
    )
    os.write(int(children[1]["fd"]), b"\x7f" * len(draft))
    result["worker_composer_cleared_after_malformed"] = _wait_screen(
        *screens["worker"], draft, present=False,
    )
    result["roles_ready_after_malformed"] = _ready(server, screens, peers)
    result["provider_counts_after_malformed"] = {
        role: providers[role].RequestHandlerClass.requests for role in ROLES
    }
    result["agent_ends_after_malformed"] = {
        role: server.event_count(role, "agent_end") for role in ROLES
    }
    result["no_processing_or_replay_for_malformed"] = (
        result["provider_counts_after_malformed"] == result["baseline_counts"]
        and result["agent_ends_after_malformed"] == result["baseline_agent_ends"]
    )
    if not all(result[key] for key in (
        "malformed_frame_no_api_ack", "same_worker_connection_after_malformed",
        "manager_binding_after_malformed", "worker_composer_usable_after_malformed",
        "worker_composer_cleared_after_malformed", "roles_ready_after_malformed",
        "no_processing_or_replay_for_malformed",
    )):
        result["result"] = "malformed_frame_observation_unknown"
        return
    fresh = _deliver(server, providers, peers, "worker", MessageKind.TASK, ActorRole.MANAGER)
    result["fresh_worker_delivery_after_malformed"] = bool(fresh["passed"])
    result["final_provider_counts"] = {
        role: providers[role].RequestHandlerClass.requests for role in ROLES
    }
    result["final_agent_ends"] = {
        role: server.event_count(role, "agent_end") for role in ROLES
    }
    result["result"] = "passed_pair_tui_malformed_frame" if (
        result["fresh_worker_delivery_after_malformed"]
        and result["final_provider_counts"] == {"manager": 1, "worker": 2}
        and result["final_agent_ends"] == {"manager": 1, "worker": 2}
        and _ready(server, screens, peers)
    ) else "post_malformed_delivery_unknown"


def run(omp: str, *, malformed_frame: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "result": "inconclusive",
        "mode": "two-real-tui-malformed-frame" if malformed_frame else "two-real-tui-host-outage",
    }
    with tempfile.TemporaryDirectory(prefix="cw04-g3-tui-extension-fault-") as temporary:
        root = Path(temporary)
        socket_path = root / "bridge.sock"
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        providers = {role: semantic_provider(manager=False) for role in ROLES}
        provider_threads = {
            role: threading.Thread(target=providers[role].serve_forever, daemon=True)
            for role in ROLES
        }
        for thread in provider_threads.values():
            thread.start()
        models = ["providers:"]
        for role in ROLES:
            models.extend((
                f"  g3-tui-fault-{role}:",
                f"    baseUrl: http://127.0.0.1:{providers[role].server_port}/v1",
                "    api: openai-completions",
                "    auth: none",
                "    models:",
                "      - id: scripted",
                f"        name: Scripted G3 TUI {role} fault probe",
                "        contextWindow: 32768",
                "        maxTokens: 1024",
            ))
        (profile / "models.yml").write_text("\n".join(models) + "\n")
        tokens = {role: str(uuid4()) for role in ROLES}
        server = BridgeHarness(socket_path, tokens)
        os.chmod(socket_path, 0o600)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        servers = [(server, server_thread)]
        children: list[dict[str, object]] = []
        drains: list[tuple[threading.Thread, threading.Event, dict[str, object]]] = []
        screens: dict[str, tuple[TerminalScreen, threading.Lock]] = {}
        try:
            for role in ROLES:
                child = _start_omp(
                    omp, role, tokens[role], root, socket_path, config,
                    profile=profile, model=f"g3-tui-fault-{role}/scripted", max_time="45",
                )
                children.append(child)
                fd = int(child["fd"])
                stop = threading.Event()
                drained: dict[str, object] = {"role": role}
                screen = TerminalScreen(100, 30, reply=lambda data, fd=fd: os.write(fd, data))
                stream = make_stream(screen)
                lock = threading.Lock()
                screens[role] = (screen, lock)
                thread = threading.Thread(
                    target=_drain_visible, args=(fd, stop, drained, stream, lock), daemon=True,
                )
                thread.start()
                drains.append((thread, stop, drained))

            peers = {role: server.peer(role, timeout=10) for role in ROLES}
            result["pids"] = {role: peers[role]["pid"] for role in ROLES}
            result["distinct_pids_sessions"] = (
                len({peers[role]["pid"] for role in ROLES}) == 2
                and len({peers[role]["session_id"] for role in ROLES}) == 2
                and all(peers[role]["generation"] == 1 for role in ROLES)
            )
            result["initial_ready"] = _ready(server, screens, peers)
            result["initial_provider_counts"] = {
                role: providers[role].RequestHandlerClass.requests for role in ROLES
            }
            if not (result["distinct_pids_sessions"] and result["initial_ready"]
                    and result["initial_provider_counts"] == {"manager": 0, "worker": 0}):
                result["result"] = "initial_tui_state_unknown"
                return result

            worker = _deliver(server, providers, peers, "worker", MessageKind.TASK, ActorRole.MANAGER)
            manager = _deliver(
                server, providers, peers, "manager", MessageKind.ANSWER,
                ActorRole.WORKER, worker["message_id"],
            )
            result["separate_provider_handling"] = bool(worker["passed"] and manager["passed"])
            result["baseline_acks"] = {
                role: {"status": record["ack_status"], "modelProcessed": record["model_processed_at_ack"]}
                for role, record in (("worker", worker), ("manager", manager))
            }
            result["baseline_counts"] = {
                role: providers[role].RequestHandlerClass.requests for role in ROLES
            }
            result["baseline_agent_ends"] = {
                role: server.event_count(role, "agent_end") for role in ROLES
            }
            result["ready_before_fault"] = _ready(server, screens, peers)
            if not (result["separate_provider_handling"] and result["ready_before_fault"]
                    and result["baseline_counts"] == {"manager": 1, "worker": 1}
                    and result["baseline_agent_ends"] == {"manager": 1, "worker": 1}):
                result["result"] = "baseline_delivery_unknown"
                return result
            if malformed_frame:
                _malformed_frame(server, peers, providers, screens, children, result)
                return result

            # Stop the disposable host listener before cutting its accepted peers.
            # The bridge then has no reachable host until this probe restores it.
            server.shutdown()
            server.server_close()
            socket_path.unlink(missing_ok=True)
            for peer in peers.values():
                try:
                    peer["socket"].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                peer["socket"].close()
            deadline = time.monotonic() + 3
            while server.peers and time.monotonic() < deadline:
                time.sleep(0.05)
            result["host_unreachable_during_fault"] = not socket_path.exists() and not server.peers
            draft = f"fault-{uuid4().hex}"
            for char in draft.encode():
                os.write(int(children[1]["fd"]), bytes((char,)))
                time.sleep(0.01)
            result["worker_composer_usable_during_fault"] = _wait_screen(
                *screens["worker"], draft, present=True,
            )
            os.write(int(children[1]["fd"]), b"\x7f" * len(draft))
            result["worker_composer_cleared_during_fault"] = _wait_screen(
                *screens["worker"], draft, present=False,
            )
            result["original_tuis_alive_during_fault"] = all(
                Path(f"/proc/{peers[role]['pid']}").exists() for role in ROLES
            )
            result["delivery_withheld_during_unknown_host_status"] = result["host_unreachable_during_fault"]
            result["provider_counts_during_fault"] = {
                role: providers[role].RequestHandlerClass.requests for role in ROLES
            }
            result["agent_ends_during_fault"] = {
                role: server.event_count(role, "agent_end") for role in ROLES
            }
            result["no_replay_during_fault"] = (
                result["provider_counts_during_fault"] == result["baseline_counts"]
                and result["agent_ends_during_fault"] == result["baseline_agent_ends"]
            )
            if not (result["host_unreachable_during_fault"]
                    and result["worker_composer_usable_during_fault"]
                    and result["worker_composer_cleared_during_fault"]
                    and result["original_tuis_alive_during_fault"]
                    and result["no_replay_during_fault"]):
                result["result"] = "outage_observation_unknown"
                return result

            recovered = BridgeHarness(socket_path, tokens)
            os.chmod(socket_path, 0o600)
            recovered_thread = threading.Thread(target=recovered.serve_forever, daemon=True)
            recovered_thread.start()
            servers.append((recovered, recovered_thread))
            new_peers = {role: recovered.peer(role, timeout=8) for role in ROLES}
            result["recovery_same_pid_session_generation"] = all(
                new_peers[role]["pid"] == peers[role]["pid"]
                and new_peers[role]["session_id"] == peers[role]["session_id"]
                and new_peers[role]["generation"] == peers[role]["generation"]
                and new_peers[role]["socket"] is not peers[role]["socket"]
                for role in ROLES
            )
            result["recovered_public_states_ready"] = _ready(recovered, screens, new_peers)
            result["provider_counts_after_reconnect"] = {
                role: providers[role].RequestHandlerClass.requests for role in ROLES
            }
            result["no_replay_after_reconnect"] = (
                result["provider_counts_after_reconnect"] == result["baseline_counts"]
                and all(recovered.event_count(role, "agent_end") == 0 for role in ROLES)
            )
            if not (result["recovery_same_pid_session_generation"]
                    and result["recovered_public_states_ready"]
                    and result["no_replay_after_reconnect"]):
                result["result"] = "recovery_observation_unknown"
                return result

            fresh = _deliver(
                recovered, providers, new_peers, "worker", MessageKind.TASK, ActorRole.MANAGER,
            )
            result["fresh_delivery_after_recovery"] = bool(fresh["passed"])
            result["final_provider_counts"] = {
                role: providers[role].RequestHandlerClass.requests for role in ROLES
            }
            result["result"] = "passed_pair_tui_extension_fault" if (
                result["fresh_delivery_after_recovery"]
                and result["final_provider_counts"] == {"manager": 1, "worker": 2}
                and _ready(recovered, screens, new_peers)
            ) else "post_recovery_delivery_unknown"
            return result
        except (TimeoutError, OSError, ValueError) as error:
            result["reason"] = type(error).__name__
            return result
        finally:
            _stop_omps(children)
            result["omp_children_remaining"] = sum(
                Path(f"/proc/{child['pid']}").exists() for child in children
            )
            result["cwd_processes_remaining"] = {
                role: cwd_processes(root / f"cwd-{role}") for role in ROLES
                if (root / f"cwd-{role}").exists()
            }
            for thread, stop, _ in drains:
                stop.set()
                thread.join(timeout=1)
            result["pty_drain"] = [drained for _, _, drained in drains]
            for child in children:
                os.close(int(child["fd"]))
            for bridge, thread in servers:
                bridge.shutdown()
                bridge.server_close()
                thread.join(timeout=2)
            for role, provider in providers.items():
                provider.shutdown()
                provider.server_close()
                provider_threads[role].join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--malformed-frame", action="store_true")
    args = parser.parse_args()
    binary = shutil.which("omp")
    if not binary or _omp_version(binary) != OMP_VERSION:
        raise SystemExit("requires OMP 18.2.10")
    observation = run(binary, malformed_frame=args.malformed_frame)
    print(json.dumps(observation, sort_keys=True))
    expected_result = "passed_pair_tui_malformed_frame" if args.malformed_frame else "passed_pair_tui_extension_fault"
    raise SystemExit(0 if observation["result"] == expected_result
                     and observation["omp_children_remaining"] == 0
                     and all(not pids for pids in observation["cwd_processes_remaining"].values()) else 1)
