"""Bounded real two-TUI ACK-loss and same-process reconnect probe.

Only the host bridge socket is closed. The worker OMP process and session stay
alive; provider bodies are checked in memory and never retained in the result.
"""

from __future__ import annotations

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
from live_rpc_pause_probe import semantic_provider
from live_rpc_reconnect_probe import accepted_ack_was_discarded, wait_reconnected
from live_tui_draft_probe import _drain_visible, _screen_state
from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageEvent, MessageKind
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


def _wait_composer(screen: TerminalScreen, lock: threading.Lock, timeout: float = 8) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _screen_state(screen, lock)["composer_visible"]:
            return True
        time.sleep(0.05)
    return False


def _wait_editor(
    server: BridgeHarness, screen: TerminalScreen, lock: threading.Lock,
    expected: str, timeout: float = 5, stale_text: str = "",
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = server.request("worker", {"kind": "probe"}, timeout=1)["state"]
        except TimeoutError:
            time.sleep(0.05)
            continue
        visible = _screen_state(screen, lock, expected or stale_text)
        if (state.get("editorKnown") is True
                and state.get("editorLength") == len(expected)
                and state.get("editorEmpty") is (expected == "")
                and (visible["draft_in_composer"] if expected else
                     visible["composer_visible"] and not visible["draft_visible"])):
            return True
        time.sleep(0.05)
    return False


def run(omp: str) -> dict[str, object]:
    result: dict[str, object] = {"result": "inconclusive", "mode": "two-real-tui-reconnect"}
    with tempfile.TemporaryDirectory(prefix="cw04-g3-tui-reconnect-") as temporary:
        root = Path(temporary)
        socket_path = root / "bridge.sock"
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        provider = semantic_provider(manager=False)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "providers:\n"
            "  g3-tui-reconnect:\n"
            f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
            "    api: openai-completions\n"
            "    auth: none\n"
            "    models:\n"
            "      - id: scripted\n"
            "        name: Scripted G3 TUI reconnect probe\n"
            "        contextWindow: 32768\n"
            "        maxTokens: 1024\n"
        )
        roles = ("manager", "worker")
        tokens = {role: str(uuid4()) for role in roles}
        server = BridgeHarness(socket_path, tokens)
        os.chmod(socket_path, 0o600)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        children: list[dict[str, object]] = []
        drains: list[tuple[threading.Thread, threading.Event, dict[str, object]]] = []
        screens: list[tuple[TerminalScreen, threading.Lock]] = []
        try:
            for role in roles:
                child = _start_omp(
                    omp, role, tokens[role], root, socket_path, config,
                    profile=profile, model="g3-tui-reconnect/scripted", max_time="45",
                    expected_marker="Pair probe response." if role == "worker" else None,
                )
                children.append(child)
                stop = threading.Event()
                drained: dict[str, object] = {"role": role}
                screen = TerminalScreen(100, 30, reply=lambda data, fd=int(child["fd"]): os.write(fd, data))
                stream = make_stream(screen)
                lock = threading.Lock()
                screens.append((screen, lock))
                thread = threading.Thread(
                    target=_drain_visible,
                    args=(int(child["fd"]), stop, drained, stream, lock), daemon=True,
                )
                thread.start()
                drains.append((thread, stop, drained))

            peers = {role: server.peer(role, timeout=10) for role in roles}
            result["pids"] = [int(peers[role]["pid"]) for role in roles]
            result["two_distinct_omp_pids"] = len(set(result["pids"])) == 2
            result["initial_composers_visible"] = all(_wait_composer(*screen) for screen in screens)
            states = {role: server.request(role, {"kind": "probe"})["state"] for role in roles}
            result["initial_public_states_ready"] = all(
                state.get("idle") and not state.get("pending")
                and state.get("editorKnown") and state.get("editorEmpty")
                for state in states.values()
            )
            if not all(result[key] for key in (
                "two_distinct_omp_pids", "initial_composers_visible", "initial_public_states_ready"
            )):
                result["result"] = "tui_initial_state_unknown"
                return result

            peer = peers["worker"]
            message_id = str(uuid4())
            first_attempt_id = str(uuid4())
            envelope = ControlEnvelope(
                message_id=message_id, delivery_attempt_id=first_attempt_id,
                sender_role=ActorRole.MANAGER,
                session_id=str(peer["session_id"]),
                session_generation=int(peer["generation"]),
                task_id=str(uuid4()), revision_id=str(uuid4()), run_id=str(uuid4()),
                event=MessageEvent(MessageKind.TASK, {"text": "Reply briefly. Do not use tools."}),
            )
            provider.semantic_expected[message_id] = {
                "workbench_message_id": message_id, "kind": "task",
                "task_id": envelope.task_id, "revision_id": envelope.revision_id,
                "run_id": envelope.run_id,
            }
            result["provider_requests_before_send"] = provider.RequestHandlerClass.requests
            result["agent_ends_before_send"] = server.event_count("worker", "agent_end")
            first_request_id = str(uuid4())
            server.dropped_ack_ids.add(first_request_id)
            frame = {"kind": "deliver", "requestId": first_request_id, "envelope": envelope.to_json()}
            with peer["write_lock"]:
                peer["socket"].sendall((json.dumps(frame) + "\n").encode())
            server.wait_event("worker", "agent_end", timeout=20)
            result["provider_requests_before_reconnect"] = provider.RequestHandlerClass.requests
            result["agent_ends_before_reconnect"] = server.event_count("worker", "agent_end")
            result["provider_identity_matched"] = (
                provider.semantic_seen.get(message_id, {}).get("matched") is True
            )
            with server.condition:
                result["first_ack_received_and_discarded"] = accepted_ack_was_discarded(
                    server.discarded_ack_metadata, first_request_id, server.acks,
                )
                result["first_ack_available_to_host"] = first_request_id in server.acks
            result["worker_alive_before_disconnect"] = Path(f"/proc/{peer['pid']}").exists()
            try:
                peer["socket"].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer["socket"].close()
            new_peer = wait_reconnected(server, peer, timeout=8)
            result["new_connection"] = new_peer["socket"] is not peer["socket"]
            result["same_worker_pid"] = new_peer["pid"] == peer["pid"] == children[1]["pid"]
            result["same_session_id"] = new_peer["session_id"] == peer["session_id"]
            result["same_generation"] = new_peer["generation"] == peer["generation"]
            result["worker_alive_after_reconnect"] = Path(f"/proc/{new_peer['pid']}").exists()
            result["composer_visible_after_reconnect"] = _wait_composer(*screens[1])
            result["public_state_after_reconnect"] = server.request("worker", {"kind": "probe"})["state"]
            state = result["public_state_after_reconnect"]
            result["public_state_ready_after_reconnect"] = (
                state.get("sessionId") == new_peer["session_id"]
                and state.get("generation") == new_peer["generation"]
                and state.get("idle") and not state.get("pending")
                and state.get("editorKnown") and state.get("editorEmpty")
            )

            # This duplicate is sent after both agent_end and the new hello, not after
            # an arbitrary sleep. Only the delivery attempt changes.
            retry = ControlEnvelope(
                message_id=message_id, delivery_attempt_id=str(uuid4()),
                sender_role=ActorRole.MANAGER,
                session_id=envelope.session_id, session_generation=envelope.session_generation,
                task_id=envelope.task_id, revision_id=envelope.revision_id, run_id=envelope.run_id,
                event=envelope.event,
            )
            result["new_delivery_attempt_id"] = retry.delivery_attempt_id != first_attempt_id
            result["retry_status"] = server.request(
                "worker", {"kind": "deliver", "envelope": retry.to_json()}, timeout=5,
            ).get("status")
            # Public state round trip after duplicate ACK establishes a later bridge boundary.
            result["state_after_retry"] = server.request("worker", {"kind": "probe"})["state"]
            result["provider_requests_after_retry"] = provider.RequestHandlerClass.requests
            result["agent_ends_after_retry"] = server.event_count("worker", "agent_end")

            draft = f"reconnected-{uuid4().hex}"
            for char in draft.encode():
                os.write(int(children[1]["fd"]), bytes((char,)))
                time.sleep(0.01)
            result["post_reconnect_draft_visible_and_public"] = _wait_editor(
                server, *screens[1], draft,
            )
            os.write(int(children[1]["fd"]), b"\x7f" * len(draft))
            result["post_reconnect_draft_cleared"] = _wait_editor(
                server, *screens[1], "", stale_text=draft,
            )
            result["provider_requests_after_composer"] = provider.RequestHandlerClass.requests
            result["agent_ends_after_composer"] = server.event_count("worker", "agent_end")
            result["manager_composer_still_visible"] = _screen_state(*screens[0])["composer_visible"]
            result["manager_public_state_alive"] = server.request("manager", {"kind": "probe"})["state"].get("editorKnown")
            result["result"] = "passed_pair_tui_reconnect" if (
                result["provider_requests_before_send"] == 0
                and result["agent_ends_before_send"] == 0
                and result["provider_requests_before_reconnect"] == 1
                and result["agent_ends_before_reconnect"] == 1
                and result["provider_identity_matched"]
                and result["first_ack_received_and_discarded"]
                and result["first_ack_available_to_host"] is False
                and all(result[key] for key in (
                    "worker_alive_before_disconnect", "new_connection", "same_worker_pid",
                    "same_session_id", "same_generation", "worker_alive_after_reconnect",
                    "composer_visible_after_reconnect", "public_state_ready_after_reconnect",
                    "new_delivery_attempt_id", "post_reconnect_draft_visible_and_public",
                    "post_reconnect_draft_cleared", "manager_composer_still_visible",
                    "manager_public_state_alive",
                ))
                and result["retry_status"] == "duplicate_api_accepted"
                and result["provider_requests_after_retry"] == 1
                and result["agent_ends_after_retry"] == 1
                and result["provider_requests_after_composer"] == 1
                and result["agent_ends_after_composer"] == 1
            ) else "tui_reconnect_observation_unknown"
            return result
        except (TimeoutError, OSError, ValueError) as error:
            result["reason"] = type(error).__name__
            return result
        finally:
            _stop_omps(children)
            result["omp_children_remaining"] = sum(
                Path(f"/proc/{child['pid']}").exists() for child in children
            )
            for thread, stop, _ in drains:
                stop.set()
                thread.join(timeout=1)
            result["pty_drain"] = [drained for _, _, drained in drains]
            for child in children:
                os.close(int(child["fd"]))
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            provider.shutdown()
            provider.server_close()
            provider_thread.join(timeout=2)


if __name__ == "__main__":
    binary = shutil.which("omp")
    if not binary or _omp_version(binary) != OMP_VERSION:
        raise SystemExit("requires OMP 18.2.10")
    observation = run(binary)
    print(json.dumps(observation, sort_keys=True))
    raise SystemExit(0 if observation["result"] == "passed_pair_tui_reconnect"
                     and observation["omp_children_remaining"] == 0 else 1)
