"""Bounded two-process OMP TUI draft and identified delivery feasibility probe."""

from __future__ import annotations

import argparse
import json
import os
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import select
import shutil
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import (
    BridgeHarness, OMP_VERSION, _omp_version,
    _start_omp, _stop_omps,
)
from live_rpc_pause_probe import semantic_provider

from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageEvent, MessageKind
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


def _screen_state(
    screen: TerminalScreen, lock: threading.Lock, draft: str = "", error_marker: str = "",
) -> dict[str, object]:
    with lock:
        lines = [
            "".join(screen.buffer[row][column].data for column in range(screen.columns))
            for row in range(screen.lines)
        ]
    composer_rows = [row for row, line in enumerate(lines) if "π >" in line]
    draft_rows = [row for row, line in enumerate(lines) if draft and draft in line]
    setup_visible = any("Setup" in line or "setup" in line for line in lines)
    # A wizard can reuse the prompt glyph. Only the last composer is current.
    current_composer = max(composer_rows) if composer_rows and not setup_visible else None
    return {
        "version_visible": any("omp v18.2.10" in line for line in lines),
        "composer_visible": current_composer is not None,
        "recent_sessions_visible": any("Recent sessions" in line for line in lines),
        "setup_visible": setup_visible,
        "error_visible": any("Error" in line or "error" in line for line in lines),
        "error_marker_visible": bool(error_marker) and any(error_marker in line for line in lines),
        "draft_visible": bool(draft_rows),
        "draft_in_composer": current_composer is not None and current_composer + 1 in draft_rows,
    }


def _screen_command_feedback(screen: TerminalScreen, lock: threading.Lock) -> dict[str, bool]:
    with lock:
        lines = [
            "".join(screen.buffer[row][column].data for column in range(screen.columns))
            for row in range(screen.lines)
        ]
    return {
        "new_session_started": any("New session started" in line for line in lines),
        "unknown_command": any("Unknown command" in line for line in lines),
        "session_error": any("session" in line.lower() and "error" in line.lower() for line in lines),
    }


def _wait_fresh_event(server: BridgeHarness, role: str, name: str, start_index: int, timeout: float) -> bool:
    """Ignore already recorded events, including an unconsumed older turn."""
    deadline = time.monotonic() + timeout
    previously_returned: set[int] = set()
    while time.monotonic() < deadline:
        try:
            event = server.wait_event(role, name, timeout=max(0.01, deadline - time.monotonic()))
        except TimeoutError:
            return False
        if event.get("name") == name and any(event is item for item in server.events[start_index:]):
            return True
        # A test double may return the same stale event forever; real harness consumes it.
        if id(event) in previously_returned:
            return False
        previously_returned.add(id(event))
    return False


def _failing_provider() -> ThreadingHTTPServer:
    """Reject one ID-checked request only after the caller releases its API ACK."""
    class Handler(BaseHTTPRequestHandler):
        requests = 0

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            type(self).requests += 1
            self.server.request_received_at = time.monotonic()
            try:
                messages = json.loads(body).get("messages", [])
                for message in messages:
                    if not isinstance(message, dict) or message.get("role") != "user":
                        continue
                    content = message.get("content")
                    parts = [content] if isinstance(content, str) else [
                        part.get("text") for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    ] if isinstance(content, list) else []
                    for part in parts:
                        try:
                            payload = json.loads(part)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(payload, dict):
                            continue
                        message_id = payload.get("workbench_message_id")
                        expected = self.server.semantic_expected.get(message_id)
                        if expected is not None:
                            self.server.semantic_seen[message_id] = {
                                "matched": all(payload.get(key) == value for key, value in expected.items())
                            }
            except (TypeError, ValueError):
                pass
            self.server.error_released_by_ack = self.server.release_error.wait(timeout=5)
            response = json.dumps({"error": {
                "message": self.server.error_marker,
                "type": "invalid_request_error",
            }}).encode()
            try:
                self.send_response(422)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                self.server.error_sent_at = time.monotonic()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    provider.semantic_expected = {}
    provider.semantic_seen = {}
    provider.release_error = threading.Event()
    provider.error_marker = ""
    provider.error_released_by_ack = False
    provider.request_received_at = None
    provider.error_sent_at = None
    return provider


def _drain_visible(
    fd: int, stop: threading.Event, result: dict[str, object],
    stream: object, lock: threading.Lock,
) -> None:
    """Drain PTY into an in-memory VT screen, answering terminal status queries."""
    result["bytes"] = 0
    while not stop.is_set():
        try:
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            result["bytes"] = int(result["bytes"]) + len(chunk)
            with lock:
                stream.feed(chunk)
        except OSError as exc:
            if exc.errno != errno.EIO:
                result["error"] = exc.errno
            return


def _persistent_omp_launcher(root: Path, omp: str) -> str:
    """Use the shared PTY harness with a temporary persistent session directory."""
    launcher = root / "persistent-omp"
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"binary = {omp!r}\n"
        "os.execv(binary, [binary, *(arg for arg in sys.argv[1:] if arg != '--no-session')])\n"
    )
    launcher.chmod(0o700)
    return str(launcher)


def _event_surface_diagnostic(server: BridgeHarness, start_index: int, ack_at: float) -> dict[str, object]:
    """Keep only structured identity comparisons and event order for this turn."""
    names = {
        "delivery_user_message_end_probe", "delivery_assistant_message_end_probe",
        "provider_request_started", "provider_request_identity_probe",
        "provider_response_received", "agent_start", "agent_end",
    }
    with server.condition:
        events = [dict(event) for event in server.events[start_index:] if event.get("name") in names]
    observed = []
    for event in events:
        item = {
            "name": event["name"], "role": event["role"],
            "sessionId": event["sessionId"], "generation": event["generation"],
            "afterApiAck": event["observedAt"] >= ack_at,
            "elapsedFromAckMs": round((event["observedAt"] - ack_at) * 1000),
        }
        if event["name"] == "delivery_user_message_end_probe":
            item["identityFieldsPresent"] = event["identityFieldsPresent"]
            item["matches"] = event["matches"]
            item["roleMatched"] = event["roleMatched"]
            item["sessionMatched"] = event["sessionMatched"]
            item["generationMatched"] = event["generationMatched"]
        if event["name"] == "delivery_assistant_message_end_probe":
            item["stopReason"] = event["stopReason"]
            item["errorMessagePresent"] = event["errorMessagePresent"]
            item["willContinue"] = event["willContinue"]
        if event["name"] == "provider_request_identity_probe":
            for key in ("eventObject", "payloadShape", "payloadKeys", "messagesArray",
                        "lastMessageRole", "lastContentShape", "textBlockCount", "structuredBlockCount",
                        "identityFieldsPresent", "matches", "roleMatched", "sessionMatched",
                        "generationMatched"):
                item[key] = event[key]
        observed.append(item)
    users = [item for item in observed if item["name"] == "delivery_user_message_end_probe"]
    return {
        "events": observed,
        "userIdentityExact": len(users) == 1 and users[0]["identityFieldsPresent"] is True
            and all(value is True for value in users[0]["matches"].values())
            and len(users[0]["matches"]) == 6
            and users[0]["roleMatched"] is True
            and users[0]["sessionMatched"] is True
            and users[0]["generationMatched"] is True,
        "identitySpecificOutcome": "unknown",
    }


def run(omp: str, *, async_error: bool = False, session_switch: bool = False,
        event_surface: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "result": "inconclusive",
        "mode": "two-real-tui-session-switch" if session_switch else (
            "two-real-tui-async-error" if async_error else "two-real-tui"
        ),
    }
    with tempfile.TemporaryDirectory(prefix="cw04-g3-tui-draft-") as temporary:
        root = Path(temporary)
        socket_path = root / "bridge.sock"
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        provider = _failing_provider() if async_error else semantic_provider(manager=False)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "providers:\n"
            "  g3-tui-probe:\n"
            f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
            "    api: openai-completions\n"
            "    auth: none\n"
            "    models:\n"
            "      - id: scripted\n"
            "        name: Scripted G3 TUI probe\n"
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
        launched_omp = _persistent_omp_launcher(root, omp) if session_switch else omp
        try:
            for role in roles:
                child = _start_omp(
                    launched_omp, role, tokens[role], root, socket_path, config,
                    profile=profile, model="g3-tui-probe/scripted", max_time="45",
                    expected_marker="Pair probe response." if role == "worker" else None,
                    event_surface_probe=event_surface,
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
            peers = {role: server.peer(role) for role in roles}
            result["pids"] = [int(peers[role]["pid"]) for role in roles]
            if len(set(result["pids"])) != 2:
                raise RuntimeError("two distinct OMP processes were not observed")

            target = "worker"
            ready_deadline = time.monotonic() + 8
            while time.monotonic() < ready_deadline:
                manager_screen = _screen_state(*screens[0])
                worker_screen = _screen_state(*screens[1])
                if manager_screen["composer_visible"] and worker_screen["composer_visible"]:
                    break
                time.sleep(0.05)
            result["initial_screens"] = {"manager": manager_screen, "worker": worker_screen}
            if not (manager_screen["composer_visible"] and worker_screen["composer_visible"]):
                result["result"] = "tui_composer_not_ready"
                return result
            initial = server.request(target, {"kind": "probe"})["state"]
            result["initial_state"] = {
                key: initial.get(key) for key in ("idle", "pending", "editorKnown", "editorEmpty", "editorLength")
            }
            draft = f"draft-{uuid4().hex}"
            for char in draft.encode():
                os.write(int(children[1]["fd"]), bytes((char,)))
                time.sleep(0.01)
            draft_state = None
            draft_screen = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = server.request(target, {"kind": "probe"})["state"]
                draft_screen = _screen_state(*screens[1], draft)
                if draft_screen["draft_in_composer"] and state.get("editorLength") == len(draft):
                    draft_state = state
                    break
                time.sleep(0.1)
            result["draft_state"] = {
                key: state.get(key) for key in ("idle", "pending", "editorKnown", "editorEmpty", "editorLength")
            }
            result["draft_visible_to_extension"] = draft_state is not None
            result["draft_screen"] = draft_screen
            result["provider_requests_with_draft"] = provider.RequestHandlerClass.requests
            if draft_state is None:
                result["result"] = (
                    "composer_api_draft_unobservable" if draft_screen["draft_in_composer"]
                    else "tui_input_path_unknown"
                )
                return result

            message_id = str(uuid4())
            if async_error:
                provider.error_marker = f"G3_ASYNC_ERROR_{uuid4().hex}"
            peer = peers[target]
            envelope = ControlEnvelope(
                message_id=message_id,
                delivery_attempt_id=str(uuid4()),
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
            held = server.request(target, {"kind": "deliver", "envelope": envelope.to_json()})
            result["draft_delivery_status"] = held.get("status")
            result["draft_length_after_deliver"] = server.request(target, {"kind": "probe"})["state"].get("editorLength")
            result["draft_screen_after_deliver"] = _screen_state(*screens[1], draft)
            result["provider_requests_while_held"] = provider.RequestHandlerClass.requests
            if held.get("status") != "deferred" or result["draft_length_after_deliver"] != len(draft):
                result["result"] = "draft_not_preserved"
                return result

            if session_switch:
                os.write(int(children[1]["fd"]), b"\x7f" * len(draft))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = server.request(target, {"kind": "probe"})["state"]
                    cleared_screen = _screen_state(*screens[1], draft)
                    if (state.get("editorKnown") and state.get("editorEmpty")
                            and state.get("idle") and not state.get("pending")
                            and not cleared_screen["draft_visible"]):
                        break
                    time.sleep(0.1)
                result["draft_cleared_before_switch"] = (
                    state.get("editorKnown") and state.get("editorEmpty")
                    and state.get("idle") and not state.get("pending")
                    and not cleared_screen["draft_visible"]
                )
                if not result["draft_cleared_before_switch"]:
                    result["result"] = "draft_clear_unknown"
                    return result

                # /new is the pinned OMP TUI command that emits session_switch.
                for char in b"/new":
                    os.write(int(children[1]["fd"]), bytes((char,)))
                    time.sleep(0.02)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    command_state = server.request(target, {"kind": "probe"})["state"]
                    command_screen = _screen_state(*screens[1], "/new")
                    if command_state.get("editorLength") == 4 and command_screen["draft_in_composer"]:
                        break
                    time.sleep(0.1)
                result["new_command_in_composer"] = (
                    command_state.get("editorLength") == 4 and command_screen["draft_in_composer"]
                )
                if not result["new_command_in_composer"]:
                    result["result"] = "tui_new_command_input_unknown"
                    return result
                os.write(int(children[1]["fd"]), b"\r")
                deadline = time.monotonic() + 8
                new_peer = None
                candidate = peer
                while time.monotonic() < deadline:
                    try:
                        candidate = server.peer(target, timeout=0.2)
                    except TimeoutError:
                        continue
                    if (candidate is not peer and candidate["pid"] == peer["pid"]
                            and candidate["session_id"] != peer["session_id"]
                            and candidate["generation"] == peer["generation"] + 1):
                        new_peer = candidate
                        break
                    time.sleep(0.05)
                result["session_transition"] = {
                    "same_pid": bool(new_peer and new_peer["pid"] == peer["pid"]),
                    "new_connection": bool(new_peer and new_peer["socket"] is not peer["socket"]),
                    "new_session_id": bool(new_peer and new_peer["session_id"] != peer["session_id"]),
                    "generation_incremented": bool(new_peer and new_peer["generation"] == peer["generation"] + 1),
                }
                if new_peer is None:
                    state = server.request(target, {"kind": "probe"})["state"]
                    result["command_feedback"] = _screen_command_feedback(*screens[1])
                    result["observed_after_command"] = {
                        "same_peer": candidate is peer,
                        "same_pid": candidate["pid"] == peer["pid"],
                        "same_session_id": candidate["session_id"] == peer["session_id"],
                        "generation_delta": candidate["generation"] - peer["generation"],
                    }
                    result["command_state"] = {
                        key: state.get(key) for key in ("editorKnown", "editorEmpty", "editorLength")
                    }
                    result["command_screen"] = _screen_state(*screens[1], "/new")
                    result["provider_requests_after_command"] = provider.RequestHandlerClass.requests
                    result["result"] = "tui_session_switch_not_observed"
                    return result

                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    new_state = server.request(target, {"kind": "probe"})["state"]
                    if (new_state.get("sessionId") == new_peer["session_id"]
                            and new_state.get("generation") == new_peer["generation"]
                            and new_state.get("idle") and not new_state.get("pending")
                            and new_state.get("editorKnown") and new_state.get("editorEmpty")
                            and _screen_state(*screens[1])["composer_visible"]):
                        break
                    time.sleep(0.1)
                result["new_session_ready"] = (
                    new_state.get("sessionId") == new_peer["session_id"]
                    and new_state.get("generation") == new_peer["generation"]
                    and new_state.get("idle") and not new_state.get("pending")
                    and new_state.get("editorKnown") and new_state.get("editorEmpty")
                    and _screen_state(*screens[1])["composer_visible"]
                )
                if not result["new_session_ready"]:
                    result["result"] = "new_session_composer_unknown"
                    return result

                result["old_envelope_after_switch"] = server.request(
                    target, {"kind": "deliver", "envelope": envelope.to_json()}
                ).get("status")
                result["provider_requests_before_new_message"] = provider.RequestHandlerClass.requests
                new_message_id = str(uuid4())
                new_envelope = ControlEnvelope(
                    message_id=new_message_id, delivery_attempt_id=str(uuid4()),
                    sender_role=ActorRole.MANAGER,
                    session_id=str(new_peer["session_id"]),
                    session_generation=int(new_peer["generation"]),
                    task_id=str(uuid4()), revision_id=str(uuid4()), run_id=str(uuid4()),
                    event=MessageEvent(MessageKind.TASK, {"text": "Reply briefly. Do not use tools."}),
                )
                provider.semantic_expected[new_message_id] = {
                    "workbench_message_id": new_message_id, "kind": "task",
                    "task_id": new_envelope.task_id, "revision_id": new_envelope.revision_id,
                    "run_id": new_envelope.run_id,
                }
                events_before_new = len(server.events)
                result["new_delivery_status"] = server.request(
                    target, {"kind": "deliver", "envelope": new_envelope.to_json()}, timeout=8,
                ).get("status")
                result["new_agent_end_observed"] = _wait_fresh_event(
                    server, target, "agent_end", events_before_new, timeout=20,
                )
                result["provider_requests_after_new_message"] = provider.RequestHandlerClass.requests
                result["new_provider_identity_matched"] = (
                    provider.semantic_seen.get(new_message_id, {}).get("matched") is True
                )
                result["old_provider_identity_seen"] = message_id in provider.semantic_seen
                result["result"] = "passed_pair_tui_session_switch" if (
                    all(result["session_transition"].values())
                    and result["old_envelope_after_switch"] == "rejected"
                    and result["provider_requests_before_new_message"] == 0
                    and result["new_delivery_status"] == "api_accepted"
                    and result["new_agent_end_observed"]
                    and result["provider_requests_after_new_message"] == 1
                    and result["new_provider_identity_matched"]
                    and not result["old_provider_identity_seen"]
                ) else "session_switch_isolation_unknown"
                return result

            os.write(int(children[1]["fd"]), b"\x7f" * len(draft))
            deadline = time.monotonic() + 5
            cleared = None
            while time.monotonic() < deadline:
                state = server.request(target, {"kind": "probe"})["state"]
                cleared_screen = _screen_state(*screens[1], draft)
                if (state.get("editorKnown") and state.get("editorEmpty")
                        and state.get("idle") and not state.get("pending")
                        and not cleared_screen["draft_visible"]):
                    cleared = state
                    break
                time.sleep(0.1)
            result["draft_cleared_safely"] = cleared is not None
            result["draft_screen_after_clear"] = cleared_screen
            if cleared is None:
                result["result"] = "draft_clear_unknown"
                return result

            events_before_send = len(server.events)
            provider_requests_before_send = provider.RequestHandlerClass.requests
            start = time.monotonic()
            try:
                response = server.request(target, {"kind": "deliver", "envelope": envelope.to_json()}, timeout=8)
                api_ack_received_at = time.monotonic()
            except TimeoutError:
                result["result"] = "api_return_unknown"
                return result
            finally:
                if async_error:
                    provider.release_error.set()
            result["api_return_status"] = response.get("status")
            result["api_model_processed_flag"] = response.get("modelProcessed")
            result["api_return_elapsed_ms"] = round((time.monotonic() - start) * 1000)
            if async_error:
                result["provider_identity_matched"] = False
                deadline = time.monotonic() + 12
                error_screen = None
                while time.monotonic() < deadline:
                    error_screen = _screen_state(*screens[1], error_marker=provider.error_marker)
                    if error_screen["error_marker_visible"]:
                        break
                    time.sleep(0.1)
                result["provider_requests"] = provider.RequestHandlerClass.requests
                result["provider_identity_matched"] = (
                    provider.semantic_seen.get(message_id, {}).get("matched") is True
                )
                result["provider_error_released_by_api_ack"] = provider.error_released_by_ack
                result["provider_error_sent"] = provider.error_sent_at is not None
                result["provider_error_after_api_ack"] = (
                    provider.error_sent_at is not None
                    and provider.error_sent_at >= api_ack_received_at
                )
                result["error_marker_visible"] = bool(error_screen and error_screen["error_marker_visible"])
                turn_deadline = time.monotonic() + 5
                while True:
                    result["fresh_agent_end_observed"] = any(
                        event.get("role") == target and event.get("name") == "agent_end"
                        for event in server.events[events_before_send:]
                    )
                    if result["fresh_agent_end_observed"] or time.monotonic() >= turn_deadline:
                        break
                    time.sleep(0.05)
                result["fresh_assistant_marker_matched"] = any(
                    event.get("role") == target and event.get("name") == "assistant_message_end"
                    and event.get("responseMarkerMatched") is True
                    for event in server.events[events_before_send:]
                )
                result["harness_delivery_attempts_after_clear"] = 1
                result["result"] = "observed_async_error" if (
                    result["api_return_status"] == "api_accepted"
                    and result["api_model_processed_flag"] is False
                    and result["provider_requests"] == provider_requests_before_send + 1
                    and result["provider_identity_matched"]
                    and result["provider_error_released_by_api_ack"]
                    and result["provider_error_sent"]
                    and result["provider_error_after_api_ack"]
                    and result["error_marker_visible"]
                    and result["fresh_agent_end_observed"]
                    and not result["fresh_assistant_marker_matched"]
                ) else "async_error_observation_unknown"
                if event_surface:
                    result["event_surface"] = _event_surface_diagnostic(server, events_before_send, api_ack_received_at)
                return result
            result["agent_end_observed"] = _wait_fresh_event(
                server, target, "agent_end", events_before_send, timeout=20,
            )
            result["provider_requests"] = provider.RequestHandlerClass.requests
            result["provider_identity_matched"] = provider.semantic_seen.get(message_id, {}).get("matched") is True
            result["assistant_marker_matched"] = any(
                event.get("role") == target and event.get("name") == "assistant_message_end"
                and event.get("responseMarkerMatched") is True
                for event in server.events[events_before_send:]
            )
            result["result"] = "passed_pair_tui_draft_delivery" if (
                result["api_return_status"] == "api_accepted"
                and result["provider_requests_with_draft"] == 0
                and result["provider_requests_while_held"] == 0
                and result["provider_requests"] == provider_requests_before_send + 1
                and result["provider_identity_matched"]
                and result["assistant_marker_matched"]
                and result["agent_end_observed"]
            ) else "inconclusive"
            if event_surface:
                result["event_surface"] = _event_surface_diagnostic(server, events_before_send, api_ack_received_at)
            return result
        finally:
            _stop_omps(children)
            result["omp_children_remaining"] = sum(Path(f"/proc/{child['pid']}").exists() for child in children)
            for thread, stop, drained in drains:
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--async-error-probe", action="store_true")
    parser.add_argument("--session-switch-probe", action="store_true")
    arguments = parser.parse_args()
    if arguments.async_error_probe and arguments.session_switch_probe:
        parser.error("choose only one probe mode")
    omp = shutil.which("omp")
    if not omp or _omp_version(omp) != OMP_VERSION:
        raise SystemExit("requires OMP 18.2.10")
    outcome = run(
        omp, async_error=arguments.async_error_probe,
        session_switch=arguments.session_switch_probe,
    )
    print(json.dumps(outcome, sort_keys=True))
    raise SystemExit(0 if outcome["result"] in {
        "passed_pair_tui_draft_delivery", "observed_async_error",
        "passed_pair_tui_session_switch",
    } else 1)
