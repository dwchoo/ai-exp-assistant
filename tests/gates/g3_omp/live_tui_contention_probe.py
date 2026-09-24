"""Bounded two-role OMP TUI contention probe with disposable native tools.

The bridge ACK, provider input, and agent_end are separate observations. No
terminal or model text is printed; only IDs, counts, and state predicates are.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import shutil
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import BridgeHarness, EXTENSION, OMP_VERSION, _omp_version, _stop_omps
from live_pause_abort_probe import cwd_processes
from live_tui_draft_probe import _drain_visible, _screen_state
from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageEvent, MessageKind
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


ROLES = ("manager", "worker")


def _provider(mode: str, cwd: Path) -> ThreadingHTTPServer:
    """Request one fixed native tool, then answer all later turns without tools."""
    class Handler(BaseHTTPRequestHandler):
        requests = 0

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            type(self).requests += 1
            try:
                messages = json.loads(raw).get("messages", [])
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
                            self.server.semantic_seen[message_id] = all(
                                payload.get(key) == value for key, value in expected.items()
                            )
            except (TypeError, ValueError):
                pass

            if self.requests == 1:
                if mode == "busy":
                    tool_name = "bash"
                    arguments = {"command": "printf STARTED > started.txt; sleep 8; printf FINISHED > result.txt"}
                else:
                    tool_name = "write"
                    arguments = {"path": str(cwd / "approval.txt"), "content": "APPROVED"}
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": f"g3-{mode}-tool", "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                }]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "Done."}
                finish = "stop"
            frames = [
                {"id": "g3-contention", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "g3-contention", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            response = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    provider.semantic_expected = {}
    provider.semantic_seen = {}
    return provider


def _start_tui(
    omp: str, role: str, token: str, root: Path, profile: Path,
    mode: str,
) -> dict[str, object]:
    cwd = root / f"cwd-{role}"
    sessions = root / f"sessions-{role}"
    cwd.mkdir()
    sessions.mkdir()
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        env = dict(os.environ)
        env.update({
            "TERM": "xterm-256color", "LANG": "C.UTF-8", "COLORTERM": "truecolor",
            "PI_CODING_AGENT_DIR": str(profile),
            "WORKBENCH_G3_BRIDGE_SOCKET": str(root / "bridge.sock"),
            "WORKBENCH_G3_ROLE": role, "WORKBENCH_G3_TOKEN": token,
            "WORKBENCH_G3_GENERATION": "1",
            "WORKBENCH_G3_EXPECTED_OMP_VERSION": OMP_VERSION,
        })
        args = [
            omp, "--no-session", "--no-pty", "--no-skills", "--no-rules",
            "--no-title", "--no-extensions", "--extension", str(EXTENSION),
            "--model", f"g3-tui-contention-{role}/scripted",
            "--tools=bash" if mode == "busy" else "--tools=write",
            "--auto-approve" if mode == "busy" else "--approval-mode=always-ask",
            "--max-time=45", "--cwd", str(cwd), "--session-dir", str(sessions),
            "--config", str(root / "config.yml"),
        ]
        os.execvpe(omp, args, env)
    os.set_blocking(fd, False)
    return {"pid": pid, "fd": fd, "role": role}


def _state(server: BridgeHarness, role: str) -> dict[str, object]:
    return server.request(role, {"kind": "probe"}, timeout=3)["state"]


def _wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _event_count(server: BridgeHarness, role: str, name: str, start: int) -> int:
    with server.condition:
        return sum(event.get("role") == role and event.get("name") == name
                   for event in server.events[start:])


def run(omp: str, mode: str) -> dict[str, object]:
    result: dict[str, object] = {"result": "inconclusive", "mode": mode, "version": OMP_VERSION}
    with tempfile.TemporaryDirectory(prefix=f"cw04-g3-tui-{mode}-") as temporary:
        root = Path(temporary)
        (root / "config.yml").write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        providers = {role: _provider(mode, root / f"cwd-{role}") for role in ROLES}
        provider_threads = {
            role: threading.Thread(target=provider.serve_forever, daemon=True)
            for role, provider in providers.items()
        }
        for thread in provider_threads.values():
            thread.start()
        model_lines = ["providers:"]
        for role in ROLES:
            model_lines.extend((
                f"  g3-tui-contention-{role}:",
                f"    baseUrl: http://127.0.0.1:{providers[role].server_port}/v1",
                "    api: openai-completions", "    auth: none", "    models:",
                "      - id: scripted", f"        name: Scripted TUI {role}",
                "        contextWindow: 32768", "        maxTokens: 1024",
            ))
        (profile / "models.yml").write_text("\n".join(model_lines) + "\n")
        tokens = {role: str(uuid4()) for role in ROLES}
        server = BridgeHarness(root / "bridge.sock", tokens)
        os.chmod(root / "bridge.sock", 0o600)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        children: list[dict[str, object]] = []
        drains: list[tuple[threading.Thread, threading.Event, dict[str, object]]] = []
        screens: dict[str, tuple[TerminalScreen, threading.Lock]] = {}
        try:
            for role in ROLES:
                child = _start_tui(omp, role, tokens[role], root, profile, mode)
                children.append(child)
                fd = int(child["fd"])
                screen = TerminalScreen(100, 30, reply=lambda data, fd=fd: os.write(fd, data))
                stream = make_stream(screen)
                lock = threading.Lock()
                screens[role] = (screen, lock)
                stop = threading.Event()
                drained: dict[str, object] = {"role": role}
                thread = threading.Thread(
                    target=_drain_visible, args=(fd, stop, drained, stream, lock), daemon=True,
                )
                thread.start()
                drains.append((thread, stop, drained))
            peers = {role: server.peer(role, timeout=10) for role in ROLES}
            result["pids"] = {role: peers[role]["pid"] for role in ROLES}
            result["distinct_pids"] = len(set(result["pids"].values())) == 2

            def ready() -> bool:
                return all(
                    (state := _state(server, role)).get("sessionId") == peers[role]["session_id"]
                    and state.get("generation") == peers[role]["generation"]
                    and state.get("idle") is True and state.get("pending") is False
                    and state.get("editorKnown") is True and state.get("editorEmpty") is True
                    and _screen_state(*screens[role])["composer_visible"]
                    for role in ROLES
                )

            result["initial_ready"] = _wait_until(ready, 8)
            result["initial_provider_requests"] = {
                role: providers[role].RequestHandlerClass.requests for role in ROLES
            }
            if not (result["distinct_pids"] and result["initial_ready"]
                    and result["initial_provider_requests"] == {"manager": 0, "worker": 0}):
                result["reason"] = "initial_tui_state_unknown"
                return result

            target = "manager" if mode == "busy" else "worker"
            other = "worker" if target == "manager" else "manager"
            fd = int(next(child["fd"] for child in children if child["role"] == target))
            for byte in b"Run the fixed local probe.":
                os.write(fd, bytes((byte,)))
                time.sleep(0.005)
            os.write(fd, b"\r")
            lifecycle = "tool_execution_start" if mode == "busy" else "tool_approval_requested"
            server.wait_event(target, lifecycle, timeout=12)
            result["lifecycle_entry"] = lifecycle
            started = root / f"cwd-{target}" / "started.txt"
            result["native_started"] = (
                _wait_until(started.exists, 2)
                and started.read_text() == "STARTED"
                if mode == "busy" else True
            )
            before = _state(server, target)
            result["state_at_delivery"] = {key: before.get(key) for key in (
                "sessionId", "generation", "idle", "pending", "approvalPending",
                "inFlightToolCount", "editorKnown", "editorEmpty", "editorLength",
            )}
            result["approval_file_absent_before"] = not (root / f"cwd-{target}" / "approval.txt").exists()
            result["screen_at_delivery"] = _screen_state(*screens[target])
            before_requests = {role: providers[role].RequestHandlerClass.requests for role in ROLES}
            before_starts = {role: server.event_count(role, "agent_start") for role in ROLES}
            with server.condition:
                event_index = len(server.events)

            peer = peers[target]
            envelope = ControlEnvelope(
                message_id=str(uuid4()), delivery_attempt_id=str(uuid4()),
                sender_role=ActorRole.WORKER if target == "manager" else ActorRole.MANAGER,
                session_id=str(peer["session_id"]), session_generation=int(peer["generation"]),
                task_id=str(uuid4()), revision_id=str(uuid4()), run_id=str(uuid4()),
                event=MessageEvent(
                    MessageKind.REPORT if target == "manager" else MessageKind.TASK,
                    {"text": "Reply briefly. Do not use tools."},
                ),
            )
            providers[target].semantic_expected[envelope.message_id] = {
                "workbench_message_id": envelope.message_id,
                "kind": "report" if target == "manager" else "task",
                "task_id": envelope.task_id,
                "revision_id": envelope.revision_id,
                "run_id": envelope.run_id,
            }
            result["message_id"] = envelope.message_id
            ack = server.request(target, {"kind": "deliver", "envelope": envelope.to_json()})
            result["deferred_ack"] = ack.get("status")
            result["model_processed_at_deferred_ack"] = ack.get("modelProcessed")
            # A full second includes multiple public state polls and exceeds the
            # prior RPC probe's 200 ms window. The native tool remains held.
            hold_samples = []
            for _ in range(6):
                time.sleep(0.2)
                state = _state(server, target)
                hold_samples.append({
                    "busy_or_approval": state.get("inFlightToolCount", 0) >= 1
                    if mode == "busy" else state.get("approvalPending") is True,
                    "session_current": state.get("sessionId") == peer["session_id"]
                    and state.get("generation") == peer["generation"],
                    "editor_length_unchanged": state.get("editorLength") == before.get("editorLength"),
                    "provider_delta": providers[target].RequestHandlerClass.requests - before_requests[target],
                    "start_delta": server.event_count(target, "agent_start") - before_starts[target],
                    "other_provider_delta": providers[other].RequestHandlerClass.requests - before_requests[other],
                    "other_start_delta": server.event_count(other, "agent_start") - before_starts[other],
                })
            result["hold_samples"] = hold_samples
            result["screen_after_hold"] = _screen_state(*screens[target])
            result["approval_file_absent_during_hold"] = not (root / f"cwd-{target}" / "approval.txt").exists()
            result["deferred_identity_absent_during_hold"] = envelope.message_id not in providers[target].semantic_seen
            result["tool_end_during_hold"] = _event_count(server, target, "tool_execution_end", event_index)

            if mode == "approval":
                os.write(fd, b"y")
                time.sleep(0.1)
                os.write(fd, b"\r")
                result["native_approval_key"] = "y_then_enter"
                released = _wait_until(lambda: server.event_count(target, "tool_approval_resolved") >= 1, 8)
                with server.condition:
                    resolutions = [event for event in server.events
                                   if event.get("role") == target and event.get("name") == "tool_approval_resolved"]
                result["approval_resolved_approved"] = released and len(resolutions) == 1 and resolutions[0].get("approved") is True
                result["native_write_exact"] = (
                    (root / f"cwd-{target}" / "approval.txt").read_text() == "APPROVED"
                    if (root / f"cwd-{target}" / "approval.txt").exists() else False
                )
            else:
                result["native_finished"] = _wait_until(
                    lambda: (root / f"cwd-{target}" / "result.txt").exists(), 12
                )
                result["native_result_exact"] = (
                    (root / f"cwd-{target}" / "result.txt").read_text() == "FINISHED"
                    if result["native_finished"] else False
                )
            result["first_turn_finished"] = _wait_until(
                lambda: server.event_count(target, "agent_end") == 1, 8
            )
            result["safe_after_release"] = _wait_until(ready, 8)
            if not result["safe_after_release"]:
                result["reason"] = "native_release_or_composer_state_unknown"
                return result

            # Retry only after observing the safe state. A deferred request has
            # not been processed; this is a fresh attempt for the same message.
            envelope = ControlEnvelope(
                message_id=envelope.message_id, delivery_attempt_id=str(uuid4()),
                sender_role=envelope.sender_role, session_id=envelope.session_id,
                session_generation=envelope.session_generation, task_id=envelope.task_id,
                revision_id=envelope.revision_id, run_id=envelope.run_id, event=envelope.event,
            )
            retry = server.request(target, {"kind": "deliver", "envelope": envelope.to_json()})
            result["retry_ack"] = retry.get("status")
            result["model_processed_at_retry_ack"] = retry.get("modelProcessed")
            result["second_turn_finished"] = _wait_until(
                lambda: server.event_count(target, "agent_end") == 2, 12
            )
            result["safe_after_delivery"] = _wait_until(ready, 8)
            after_requests = {role: providers[role].RequestHandlerClass.requests for role in ROLES}
            after_starts = {role: server.event_count(role, "agent_start") for role in ROLES}
            result["provider_id_match"] = providers[target].semantic_seen.get(envelope.message_id) is True
            result["target_provider_delta_after_retry"] = after_requests[target] - before_requests[target]
            result["other_provider_delta"] = after_requests[other] - before_requests[other]
            result["target_start_delta_after_retry"] = after_starts[target] - before_starts[target]
            result["other_start_delta"] = after_starts[other] - before_starts[other]
            result["target_end_delta"] = _event_count(server, target, "agent_end", event_index)
            result["other_end_delta"] = _event_count(server, other, "agent_end", event_index)
            with server.condition:
                new_ends = [event for event in server.events[event_index:]
                            if event.get("role") == target and event.get("name") == "agent_end"]
            result["new_ends_current_session"] = all(
                event.get("sessionId") == peer["session_id"]
                and event.get("generation") == peer["generation"] for event in new_ends
            ) and len(new_ends) == 2
            duplicate = server.request(target, {"kind": "deliver", "envelope": envelope.to_json()})
            result["duplicate_ack"] = duplicate.get("status")
            time.sleep(0.4)
            result["no_replay_after_duplicate"] = (
                providers[target].RequestHandlerClass.requests == after_requests[target]
                and server.event_count(target, "agent_end") == 2
            )
            result["result"] = f"passed_{mode}" if (
                result["native_started"] is True
                and result["deferred_ack"] == "deferred"
                and result["model_processed_at_deferred_ack"] is not True
                and before_requests[target] == 1 and before_starts[target] == 1
                and all(
                    sample["busy_or_approval"] and sample["session_current"]
                    and sample["editor_length_unchanged"]
                    and sample["provider_delta"] == 0 and sample["start_delta"] == 0
                    and sample["other_provider_delta"] == 0 and sample["other_start_delta"] == 0
                    for sample in hold_samples
                )
                and result["screen_at_delivery"]["composer_visible"] == result["screen_after_hold"]["composer_visible"]
                and result["approval_file_absent_before"]
                and result["approval_file_absent_during_hold"]
                and result["deferred_identity_absent_during_hold"]
                and result["tool_end_during_hold"] == 0
                and before.get("sessionId") == peer["session_id"]
                and before.get("generation") == peer["generation"]
                and before.get("editorKnown") is True
                and before.get("editorEmpty") is True
                and (before.get("idle") is False and before.get("inFlightToolCount", 0) >= 1
                     if mode == "busy" else before.get("approvalPending") is True)
                and (result.get("native_result_exact") is True if mode == "busy" else
                     result.get("approval_resolved_approved") is True and result.get("native_write_exact") is True)
                and result["first_turn_finished"] and result["safe_after_release"]
                and result["retry_ack"] == "api_accepted"
                and result["model_processed_at_retry_ack"] is False
                and result["second_turn_finished"] and result["safe_after_delivery"]
                and result["provider_id_match"]
                and result["target_provider_delta_after_retry"] == 2
                and result["other_provider_delta"] == 0
                and result["target_start_delta_after_retry"] == 1
                and result["other_start_delta"] == 0
                and result["target_end_delta"] == 2 and result["other_end_delta"] == 0
                and result["new_ends_current_session"]
                and result["duplicate_ack"] == "duplicate_api_accepted"
                and result["no_replay_after_duplicate"]
            ) else "inconclusive"
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, choices=("busy", "approval"))
    scenario = parser.parse_args().scenario
    binary = shutil.which("omp")
    if not binary or _omp_version(binary) != OMP_VERSION:
        raise SystemExit("requires OMP 18.2.10")
    observation = run(binary, scenario)
    print(json.dumps(observation, sort_keys=True))
    raise SystemExit(0 if observation["result"] == f"passed_{scenario}"
                     and observation["omp_children_remaining"] == 0
                     and all(not pids for pids in observation["cwd_processes_remaining"].values()) else 1)
