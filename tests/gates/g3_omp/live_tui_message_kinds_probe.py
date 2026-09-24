"""Bounded four-kind delivery probe using two real OMP 18.2.10 TUIs.

The local providers keep only ID match metadata. Method acceptance, provider
input, and a new target-session agent_end are separate observations.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import BridgeHarness, OMP_VERSION, _omp_version, _start_omp, _stop_omps
from live_pause_abort_probe import cwd_processes
from live_rpc_pause_probe import semantic_provider
from live_tui_draft_probe import _drain_visible, _screen_state
from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageEvent, MessageKind
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


ROLES = ("manager", "worker")
DELIVERIES = (
    (MessageKind.TASK, "worker", ActorRole.MANAGER),
    (MessageKind.QUESTION, "worker", ActorRole.MANAGER),
    (MessageKind.ANSWER, "manager", ActorRole.WORKER),
    (MessageKind.REPORT, "manager", ActorRole.WORKER),
)


def _ready(
    server: BridgeHarness,
    screens: dict[str, tuple[TerminalScreen, threading.Lock]],
    peers: dict[str, dict[str, object]],
    timeout: float = 8,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        states = {role: server.request(role, {"kind": "probe"})["state"] for role in ROLES}
        if all(
            states[role].get("sessionId") == peers[role]["session_id"]
            and states[role].get("generation") == peers[role]["generation"]
            and states[role].get("idle") is True
            and states[role].get("pending") is False
            and states[role].get("editorKnown") is True
            and states[role].get("editorEmpty") is True
            and _screen_state(*screens[role])["composer_visible"]
            for role in ROLES
        ):
            return True
        time.sleep(0.05)
    return False


def _new_end(
    server: BridgeHarness, role: str, peer: dict[str, object], start_index: int,
    timeout: float = 15,
) -> tuple[int, bool]:
    deadline = time.monotonic() + timeout
    with server.condition:
        while time.monotonic() < deadline:
            events = [event for event in server.events[start_index:]
                      if event.get("role") == role and event.get("name") == "agent_end"]
            if events:
                return len(events), all(
                    event.get("sessionId") == peer["session_id"]
                    and event.get("generation") == peer["generation"]
                    for event in events
                )
            server.condition.wait(deadline - time.monotonic())
    return 0, False


def run(omp: str) -> dict[str, object]:
    result: dict[str, object] = {"result": "inconclusive", "mode": "two-real-tui-four-kinds"}
    with tempfile.TemporaryDirectory(prefix="cw04-g3-tui-kinds-") as temporary:
        root = Path(temporary)
        socket_path = root / "bridge.sock"
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        providers = {role: semantic_provider(manager=False) for role in ROLES}
        provider_threads = {
            role: threading.Thread(target=provider.serve_forever, daemon=True)
            for role, provider in providers.items()
        }
        for thread in provider_threads.values():
            thread.start()
        model_lines = ["providers:"]
        for role in ROLES:
            model_lines.extend((
                f"  g3-tui-kinds-{role}:",
                f"    baseUrl: http://127.0.0.1:{providers[role].server_port}/v1",
                "    api: openai-completions",
                "    auth: none",
                "    models:",
                "      - id: scripted",
                f"        name: Scripted G3 TUI {role} probe",
                "        contextWindow: 32768",
                "        maxTokens: 1024",
            ))
        (profile / "models.yml").write_text("\n".join(model_lines) + "\n")

        tokens = {role: str(uuid4()) for role in ROLES}
        server = BridgeHarness(socket_path, tokens)
        os.chmod(socket_path, 0o600)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        children: list[dict[str, object]] = []
        drains: list[tuple[threading.Thread, threading.Event, dict[str, object]]] = []
        screens: dict[str, tuple[TerminalScreen, threading.Lock]] = {}
        try:
            for role in ROLES:
                child = _start_omp(
                    omp, role, tokens[role], root, socket_path, config,
                    profile=profile, model=f"g3-tui-kinds-{role}/scripted", max_time="45",
                )
                children.append(child)
                stop = threading.Event()
                drained: dict[str, object] = {"role": role}
                fd = int(child["fd"])
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
            result["distinct_pids"] = len(set(result["pids"].values())) == 2
            result["initial_ready"] = _ready(server, screens, peers)
            result["initial_provider_requests"] = {
                role: providers[role].RequestHandlerClass.requests for role in ROLES
            }
            if not (result["distinct_pids"] and result["initial_ready"]
                    and all(count == 0 for count in result["initial_provider_requests"].values())):
                result["result"] = "initial_tui_state_unknown"
                return result

            prior_ids: dict[str, str] = {}
            records: list[dict[str, object]] = []
            result["records"] = records
            for kind, target, sender in DELIVERIES:
                peer = peers[target]
                reply_to = prior_ids.get("question" if kind is MessageKind.ANSWER else "task") if kind in (
                    MessageKind.ANSWER, MessageKind.REPORT,
                ) else None
                envelope = ControlEnvelope(
                    message_id=str(uuid4()), delivery_attempt_id=str(uuid4()),
                    sender_role=sender, session_id=str(peer["session_id"]),
                    session_generation=int(peer["generation"]),
                    task_id=str(uuid4()), revision_id=str(uuid4()), run_id=str(uuid4()),
                    event=MessageEvent(kind, {"text": "Reply briefly. Do not use tools."}, reply_to),
                )
                expected = {
                    "workbench_message_id": envelope.message_id,
                    "kind": kind.value, "task_id": envelope.task_id,
                    "revision_id": envelope.revision_id, "run_id": envelope.run_id,
                }
                if reply_to is not None:
                    expected["in_reply_to_message_id"] = reply_to
                providers[target].semantic_expected[envelope.message_id] = expected
                before_requests = {
                    role: providers[role].RequestHandlerClass.requests for role in ROLES
                }
                before_ends = {role: server.event_count(role, "agent_end") for role in ROLES}
                with server.condition:
                    event_index = len(server.events)
                prior_seen = envelope.message_id in providers[target].semantic_seen
                ack = server.request(
                    target, {"kind": "deliver", "envelope": envelope.to_json()}, timeout=5,
                )
                end_count, end_session_match = _new_end(server, target, peer, event_index)
                settled = _ready(server, screens, peers) if end_count else False
                # The public state round trip occurs after the new agent_end; count
                # both providers and both roles again to reject cross-role activity.
                after_requests = {
                    role: providers[role].RequestHandlerClass.requests for role in ROLES
                }
                after_ends = {role: server.event_count(role, "agent_end") for role in ROLES}
                seen = providers[target].semantic_seen.get(envelope.message_id)
                other = "manager" if target == "worker" else "worker"
                record = {
                    "kind": kind.value, "target": target,
                    "message_id": envelope.message_id,
                    "delivery_attempt_id": envelope.delivery_attempt_id,
                    "session_id": envelope.session_id,
                    "generation": envelope.session_generation,
                    "task_id": envelope.task_id,
                    "revision_id": envelope.revision_id,
                    "run_id": envelope.run_id,
                    "reply_to_message_id": reply_to,
                    "ack": ack.get("status"),
                    "model_processed_at_ack": ack.get("modelProcessed"),
                    "new_provider_id_seen": not prior_seen and seen is not None,
                    "provider_fields_match": seen is not None and seen.get("matched") is True,
                    "target_provider_request_delta": after_requests[target] - before_requests[target],
                    "other_provider_request_delta": after_requests[other] - before_requests[other],
                    "target_agent_end_delta": after_ends[target] - before_ends[target],
                    "other_agent_end_delta": after_ends[other] - before_ends[other],
                    "new_end_session_match": end_session_match,
                    "ready_after_end": settled,
                }
                records.append(record)
                prior_ids[kind.value] = envelope.message_id
                if not (
                    record["ack"] == "api_accepted"
                    and record["model_processed_at_ack"] is False
                    and record["new_provider_id_seen"]
                    and record["provider_fields_match"]
                    and record["target_provider_request_delta"] == 1
                    and record["other_provider_request_delta"] == 0
                    and record["target_agent_end_delta"] == 1
                    and record["other_agent_end_delta"] == 0
                    and record["new_end_session_match"]
                    and record["ready_after_end"]
                ):
                    result["result"] = "delivery_observation_unknown"
                    return result

            result["provider_requests"] = {
                role: providers[role].RequestHandlerClass.requests for role in ROLES
            }
            result["agent_ends"] = {role: server.event_count(role, "agent_end") for role in ROLES}
            result["result"] = "passed_pair_tui_four_kinds" if (
                [record["kind"] for record in records] == [kind.value for kind, _, _ in DELIVERIES]
                and result["provider_requests"] == {"manager": 2, "worker": 2}
                and result["agent_ends"] == {"manager": 2, "worker": 2}
            ) else "delivery_observation_unknown"
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
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            for role, provider in providers.items():
                provider.shutdown()
                provider.server_close()
                provider_threads[role].join(timeout=2)


if __name__ == "__main__":
    binary = shutil.which("omp")
    if not binary or _omp_version(binary) != OMP_VERSION:
        raise SystemExit("requires OMP 18.2.10")
    observation = run(binary)
    print(json.dumps(observation, sort_keys=True))
    raise SystemExit(0 if observation["result"] == "passed_pair_tui_four_kinds"
                     and observation["omp_children_remaining"] == 0
                     and all(not pids for pids in observation["cwd_processes_remaining"].values()) else 1)
