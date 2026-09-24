"""Bounded real two-TUI session switch while delivery outcome is unknown.

The first provider reply is held locally. A deliver and pause are placed on the
same bridge connection without waiting between them, then /new is typed in the
worker TUI. The result is deliberately inconclusive if OMP refuses the command
until the pending turn ends. No model or terminal body is retained or printed.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import BridgeHarness, OMP_VERSION, _omp_version, _start_omp, _stop_omps
from live_pause_abort_probe import cwd_processes
from live_tui_draft_probe import _drain_visible, _persistent_omp_launcher, _screen_state
from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageEvent, MessageKind
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


def _provider() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                messages = json.loads(body).get("messages", [])
                found = []
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
                            candidate = json.loads(part)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(candidate, dict) and isinstance(candidate.get("workbench_message_id"), str):
                            found.append(candidate)
            except (UnicodeDecodeError, ValueError, AttributeError):
                found = []
            with self.server.lock:
                self.server.requests += 1
                index = self.server.requests
                self.server.request_message_ids[index] = [payload["workbench_message_id"] for payload in found]
                for payload in found:
                    identity = payload["workbench_message_id"]
                    expected = self.server.expected.get(identity)
                    if expected is not None:
                        self.server.seen.setdefault(identity, []).append(
                            all(payload.get(key) == value for key, value in expected.items())
                        )
                self.server.request_started.set()
            if index == 1:
                self.server.first_released_by_probe = self.server.release_first.wait(12)
            delta = {"role": "assistant", "content": "Pair probe response."}
            frames = [
                {"id": "g3-session-inflight", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "g3-session-inflight", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            response = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                with self.server.lock:
                    self.server.response_sent.add(index)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    provider.lock = threading.Lock()
    provider.requests = 0
    provider.expected = {}
    provider.seen = {}
    provider.request_message_ids = {}
    provider.response_sent = set()
    provider.request_started = threading.Event()
    provider.release_first = threading.Event()
    provider.first_released_by_probe = False
    return provider


def _wait_ack_pair(server: BridgeHarness, ids: tuple[str, str], timeout: float) -> tuple[object, object]:
    deadline = time.monotonic() + timeout
    with server.condition:
        while not all(request_id in server.acks for request_id in ids) and time.monotonic() < deadline:
            server.condition.wait(deadline - time.monotonic())
        return tuple(server.acks.pop(request_id, None) for request_id in ids)


def _wait_new_peer(server: BridgeHarness, old: dict[str, object], timeout: float) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            candidate = server.peer("worker", timeout=0.2)
        except TimeoutError:
            continue
        if (candidate is not old and candidate["socket"] is not old["socket"]
                and candidate["pid"] == old["pid"]
                and candidate["session_id"] != old["session_id"]
                and candidate["generation"] == old["generation"] + 1):
            return candidate
        time.sleep(0.05)
    return None


def run(omp: str) -> dict[str, object]:
    result: dict[str, object] = {"result": "inconclusive", "mode": "two-real-tui-session-inflight"}
    with tempfile.TemporaryDirectory(prefix="cw04-g3-tui-session-inflight-") as temporary:
        root = Path(temporary)
        socket_path = root / "bridge.sock"
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        provider = _provider()
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "providers:\n  g3-tui-session-inflight:\n"
            f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: Scripted TUI session probe\n"
            "        contextWindow: 32768\n        maxTokens: 1024\n"
        )
        roles = ("manager", "worker")
        tokens = {role: str(uuid4()) for role in roles}
        server = BridgeHarness(socket_path, tokens)
        os.chmod(socket_path, 0o600)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        children: list[dict[str, object]] = []
        drains: list[tuple[threading.Thread, threading.Event, dict[str, object]]] = []
        screens: dict[str, tuple[TerminalScreen, threading.Lock]] = {}
        launcher = _persistent_omp_launcher(root, omp)
        try:
            for role in roles:
                child = _start_omp(
                    launcher, role, tokens[role], root, socket_path, config,
                    profile=profile, model="g3-tui-session-inflight/scripted", max_time="45",
                )
                children.append(child)
                fd = int(child["fd"])
                screen = TerminalScreen(100, 30, reply=lambda data, fd=fd: os.write(fd, data))
                lock = threading.Lock()
                screens[role] = (screen, lock)
                stop = threading.Event()
                drained: dict[str, object] = {"role": role}
                thread = threading.Thread(
                    target=_drain_visible, args=(fd, stop, drained, make_stream(screen), lock), daemon=True,
                )
                thread.start()
                drains.append((thread, stop, drained))
            peers = {role: server.peer(role, timeout=10) for role in roles}
            result["pids"] = {role: peers[role]["pid"] for role in roles}
            result["two_distinct_pids"] = len(set(result["pids"].values())) == 2
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                states = {role: server.request(role, {"kind": "probe"})["state"] for role in roles}
                if all(
                    state.get("sessionId") == peers[role]["session_id"]
                    and state.get("generation") == peers[role]["generation"]
                    and state.get("idle") is True and state.get("pending") is False
                    and state.get("editorKnown") is True and state.get("editorEmpty") is True
                    and _screen_state(*screens[role])["composer_visible"]
                    for role, state in states.items()
                ):
                    break
                time.sleep(0.05)
            result["initial_ready"] = all(
                state.get("idle") is True and state.get("pending") is False
                and state.get("editorEmpty") is True
                and _screen_state(*screens[role])["composer_visible"]
                for role, state in states.items()
            )
            if not (result["two_distinct_pids"] and result["initial_ready"]):
                result["reason"] = "initial_tui_state_unknown"
                return result

            old = peers["worker"]
            old_id = str(uuid4())
            envelope = ControlEnvelope(
                message_id=old_id, delivery_attempt_id=str(uuid4()),
                sender_role=ActorRole.MANAGER,
                session_id=str(old["session_id"]), session_generation=int(old["generation"]),
                task_id=str(uuid4()), revision_id=str(uuid4()), run_id=str(uuid4()),
                event=MessageEvent(MessageKind.TASK, {"text": "Reply briefly. Do not use tools."}),
            )
            provider.expected[old_id] = {
                "workbench_message_id": old_id, "kind": "task",
                "task_id": envelope.task_id, "revision_id": envelope.revision_id,
                "run_id": envelope.run_id,
            }
            deliver_id, pause_id = str(uuid4()), str(uuid4())
            frames = (
                {"kind": "deliver", "requestId": deliver_id, "envelope": envelope.to_json()},
                {"kind": "pause", "requestId": pause_id},
            )
            with old["write_lock"]:
                old["socket"].sendall("".join(json.dumps(frame) + "\n" for frame in frames).encode())
            deliver_ack, pause_ack = _wait_ack_pair(server, (deliver_id, pause_id), timeout=5)
            result["deliver_ack"] = deliver_ack.get("status") if isinstance(deliver_ack, dict) else None
            result["deliver_model_processed_at_ack"] = (
                deliver_ack.get("modelProcessed") if isinstance(deliver_ack, dict) else None
            )
            result["pause_ack"] = pause_ack.get("status") if isinstance(pause_ack, dict) else None
            result["provider_started"] = provider.request_started.wait(5)
            old_state = server.request("worker", {"kind": "probe"})["state"]
            result["old_state_before_new"] = {key: old_state.get(key) for key in (
                "sessionId", "generation", "idle", "pending", "paused", "editorKnown", "editorEmpty",
            )}
            result["old_message_id"] = old_id
            if not (result["deliver_ack"] == "unknown_no_replay"
                    and result["pause_ack"] == "paused"
                    and result["provider_started"]):
                result["reason"] = "sending_or_unknown_window_not_established"
                return result

            fd = int(children[1]["fd"])
            for byte in b"/new":
                os.write(fd, bytes((byte,)))
                time.sleep(0.02)
            time.sleep(0.25)
            typed = server.request("worker", {"kind": "probe"})["state"]
            result["new_command_editor_length_during_pending"] = typed.get("editorLength")
            result["new_command_visible_during_pending"] = _screen_state(*screens["worker"], "/new")["draft_in_composer"]
            os.write(fd, b"\r")
            new_peer = _wait_new_peer(server, old, timeout=3)
            result["switch_accepted_while_provider_held"] = new_peer is not None and not provider.release_first.is_set()
            result["provider_requests_during_hold"] = provider.requests
            if new_peer is None:
                result["reason"] = "tui_new_not_accepted_during_pending_turn"
                return result
            result["new_session_id"] = str(new_peer["session_id"])
            result["new_generation"] = int(new_peer["generation"])
            result["same_worker_pid"] = new_peer["pid"] == old["pid"]
            result["old_session_id"] = str(old["session_id"])
            result["old_generation"] = int(old["generation"])
            provider.release_first.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and 1 not in provider.response_sent:
                time.sleep(0.05)
            result["old_provider_response_sent_after_switch"] = 1 in provider.response_sent
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                state = server.request("worker", {"kind": "probe"})["state"]
                if (state.get("sessionId") == new_peer["session_id"]
                        and state.get("generation") == new_peer["generation"]
                        and state.get("idle") is True and state.get("pending") is False
                        and state.get("editorKnown") and state.get("editorEmpty")
                        and _screen_state(*screens["worker"])["composer_visible"]):
                    break
                time.sleep(0.05)
            result["new_composer_visible"] = _screen_state(*screens["worker"])["composer_visible"]
            result["new_session_ready"] = (
                state.get("sessionId") == new_peer["session_id"]
                and state.get("generation") == new_peer["generation"]
                and state.get("idle") is True and state.get("pending") is False
                and state.get("editorKnown") and state.get("editorEmpty")
                and result["new_composer_visible"]
            )
            if not result["new_session_ready"]:
                result["reason"] = "new_session_state_unknown"
                return result
            result["old_retry_ack"] = server.request(
                "worker", {"kind": "deliver", "envelope": envelope.to_json()}
            ).get("status")
            result["new_paused_before_resume"] = state.get("paused")
            if state.get("paused") is True:
                result["resume_ack"] = server.request(
                    "worker", {"kind": "resume", "reconciled": True}
                ).get("status")
            else:
                result["resume_ack"] = "already_resumed"
            new_id = str(uuid4())
            new_envelope = ControlEnvelope(
                message_id=new_id, delivery_attempt_id=str(uuid4()),
                sender_role=ActorRole.MANAGER,
                session_id=str(new_peer["session_id"]), session_generation=int(new_peer["generation"]),
                task_id=str(uuid4()), revision_id=str(uuid4()), run_id=str(uuid4()),
                event=MessageEvent(MessageKind.TASK, {"text": "Reply briefly. Do not use tools."}),
            )
            provider.expected[new_id] = {
                "workbench_message_id": new_id, "kind": "task",
                "task_id": new_envelope.task_id, "revision_id": new_envelope.revision_id,
                "run_id": new_envelope.run_id,
            }
            result["new_message_id"] = new_id
            with server.condition:
                new_event_start = len(server.events)
            result["new_ends_before_delivery"] = server.event_count("worker", "agent_end")
            new_ack = server.request(
                "worker", {"kind": "deliver", "envelope": new_envelope.to_json()}
            )
            result["new_ack"] = new_ack.get("status")
            result["new_model_processed_at_ack"] = new_ack.get("modelProcessed")
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                with server.condition:
                    new_ends = [event for event in server.events[new_event_start:]
                                if event.get("role") == "worker" and event.get("name") == "agent_end"]
                if provider.seen.get(new_id) and new_ends:
                    break
                time.sleep(0.05)
            result["old_provider_identity_matched"] = provider.seen.get(old_id)
            result["new_provider_identity_matched"] = provider.seen.get(new_id)
            result["provider_request_count"] = provider.requests
            result["provider_request_identities"] = {
                str(index): [
                    "old" if identity == old_id else "new" if identity == new_id else "unexpected"
                    for identity in provider.request_message_ids.get(index, [])
                ] for index in sorted(provider.request_message_ids)
            }
            result["new_agent_end_count"] = len(new_ends)
            result["new_ends_current_session"] = (
                len(new_ends) == 1
                and new_ends[0].get("sessionId") == new_peer["session_id"]
                and new_ends[0].get("generation") == new_peer["generation"]
            )
            result["result"] = "observed_session_inflight_isolation" if (
                result["switch_accepted_while_provider_held"]
                and result["same_worker_pid"]
                and result["old_retry_ack"] == "rejected"
                and result["resume_ack"] in {"resumed", "already_resumed"}
                and result["new_ack"] == "api_accepted"
                and result["new_model_processed_at_ack"] is False
                and result["new_provider_identity_matched"] == [True]
                and result["old_provider_identity_matched"] == [True]
                and result["provider_request_count"] == 2
                and result["provider_request_identities"] == {"1": ["old"], "2": ["new"]}
                and result["new_ends_current_session"]
            ) else "inconclusive"
            return result
        except (TimeoutError, OSError, ValueError) as error:
            result["reason"] = type(error).__name__
            return result
        finally:
            provider.release_first.set()
            _stop_omps(children)
            result["omp_children_remaining"] = sum(
                Path(f"/proc/{child['pid']}").exists() for child in children
            )
            result["cwd_processes_remaining"] = {
                role: cwd_processes(root / f"cwd-{role}") for role in roles
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
            provider.shutdown()
            provider.server_close()
            provider_thread.join(timeout=2)


if __name__ == "__main__":
    binary = shutil.which("omp")
    if not binary or _omp_version(binary) != OMP_VERSION:
        raise SystemExit("requires OMP 18.2.10")
    observation = run(binary)
    print(json.dumps(observation, sort_keys=True))
    raise SystemExit(0 if observation["result"] == "observed_session_inflight_isolation"
                     and observation["omp_children_remaining"] == 0
                     and all(not pids for pids in observation["cwd_processes_remaining"].values()) else 1)
