"""Bounded TaskMailbox exercise against two real OMP 18.2.10 TUIs."""

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

from live_omp_probe import OMP_VERSION, _omp_version, _start_omp, _stop_omps
from live_pause_abort_probe import cwd_processes
from live_tui_draft_probe import _drain_visible, _screen_state
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, MailboxStatus, TaskMailbox
from workbench.tasks.repository import TaskRepository
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


ROLES = (ActorRole.MANAGER, ActorRole.WORKER)
CASES = (
    (MessageKind.TASK, ActorRole.MANAGER, ActorRole.WORKER, None),
    (MessageKind.QUESTION, ActorRole.MANAGER, ActorRole.WORKER, None),
    (MessageKind.ANSWER, ActorRole.WORKER, ActorRole.MANAGER, MessageKind.QUESTION),
    (MessageKind.REPORT, ActorRole.WORKER, ActorRole.MANAGER, MessageKind.TASK),
)


def _semantic_provider() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.server.request_count += 1
            parsed: list[dict[str, object]] = []
            try:
                request = json.loads(raw)
                messages = request.get("messages", []) if isinstance(request, dict) else []
                for item in messages if isinstance(messages, list) else []:
                    if not isinstance(item, dict) or item.get("role") != "user":
                        continue
                    content = item.get("content")
                    parts = [content] if isinstance(content, str) else [
                        block.get("text") for block in content
                        if isinstance(block, dict) and isinstance(block.get("text"), str)
                    ] if isinstance(content, list) else []
                    for part in parts:
                        try:
                            value = json.loads(part)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(value, dict) and isinstance(value.get("workbench_message_id"), str):
                            parsed.append(value)
            except (UnicodeDecodeError, TypeError, ValueError):
                pass
            # Only identity booleans and an ID survive; no prompt or response text is retained.
            for value in parsed:
                message_id = value["workbench_message_id"]
                expected = self.server.semantic_expected.get(message_id)
                if expected is not None:
                    self.server.semantic_seen[message_id] = all(
                        value.get(key) == expected_value for key, expected_value in expected.items()
                    )

            frames = [
                {"id": "mailbox-probe", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0,
                 "delta": {"role": "assistant", "content": "Mailbox probe response."},
                 "finish_reason": None}]},
                {"id": "mailbox-probe", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": {},
                 "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            body = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.request_count = 0
    server.semantic_expected = {}
    server.semantic_seen = {}
    return server


def _ready(bridge: G3BridgeServer, screens, timeout: float = 12) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            states = {role: bridge.probe(role, timeout=2) for role in ROLES}
        except Exception:
            time.sleep(0.05)
            continue
        if all(
            states[role].get("idle") is True
            and states[role].get("pending") is False
            and states[role].get("approvalPending") is False
            and states[role].get("editorKnown") is True
            and states[role].get("editorEmpty") is True
            and states[role].get("inFlightToolCount") == 0
            and _screen_state(*screens[role])["composer_visible"]
            for role in ROLES
        ):
            return True
        time.sleep(0.05)
    return False


def run(omp: str) -> dict[str, object]:
    result: dict[str, object] = {"result": "inconclusive", "mode": "actual-two-omp-task-mailbox"}
    with tempfile.TemporaryDirectory(prefix="cw08-task-mailbox-") as temporary:
        root = Path(temporary)
        socket_path = root / "bridge.sock"
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n", encoding="utf-8")
        profile = root / "agent"
        profile.mkdir()
        providers = {role: _semantic_provider() for role in ROLES}
        provider_threads = {
            role: threading.Thread(target=provider.serve_forever, daemon=True)
            for role, provider in providers.items()
        }
        for thread in provider_threads.values():
            thread.start()
        model_lines = ["providers:"]
        for role in ROLES:
            model_lines.extend((
                f"  cw08-mailbox-{role.value}:",
                f"    baseUrl: http://127.0.0.1:{providers[role].server_port}/v1",
                "    api: openai-completions",
                "    auth: none",
                "    models:",
                "      - id: scripted",
                f"        name: CW-08 scripted {role.value}",
                "        contextWindow: 32768",
                "        maxTokens: 1024",
            ))
        (profile / "models.yml").write_text("\n".join(model_lines) + "\n", encoding="utf-8")

        tokens = {role.value: str(uuid4()) for role in ROLES}
        bridge = G3BridgeServer(socket_path, tokens)
        bridge.start()
        repository = TaskRepository(root / "metadata.sqlite3")
        task_id = repository.create_task({"goal": "four-kind mailbox runtime", "allowed_changes": []})
        repository.approve_scope(task_id, 1, {"paths": [], "commands": []})
        repository.proceed(task_id, 1, "bounded integration probe")
        run_id = repository.start_run(task_id, 1)
        mailbox = TaskMailbox(repository, bridge)

        children: list[dict[str, object]] = []
        drains: list[tuple[threading.Thread, threading.Event, dict[str, object]]] = []
        screens = {}
        try:
            for role in ROLES:
                child = _start_omp(
                    omp, role.value, tokens[role.value], root, socket_path, config,
                    profile=profile, model=f"cw08-mailbox-{role.value}/scripted", max_time="60",
                )
                children.append(child)
                fd = int(child["fd"])
                screen = TerminalScreen(100, 30, reply=lambda data, fd=fd: os.write(fd, data))
                stream, lock = make_stream(screen), threading.Lock()
                screens[role] = (screen, lock)
                stop = threading.Event()
                drained: dict[str, object] = {"role": role.value}
                thread = threading.Thread(
                    target=_drain_visible, args=(fd, stop, drained, stream, lock), daemon=True,
                )
                thread.start()
                drains.append((thread, stop, drained))

            peers = {role: bridge.peer(role, timeout=15) for role in ROLES}
            result["omp_versions"] = {role.value: _omp_version(omp) for role in ROLES}
            result["pids"] = {role.value: peers[role].pid for role in ROLES}
            result["distinct_omp_pids"] = len({peer.pid for peer in peers.values()}) == 2
            result["initial_ready"] = _ready(bridge, screens)
            result["initial_provider_requests"] = {
                role.value: providers[role].request_count for role in ROLES
            }
            if not result["distinct_omp_pids"] or not result["initial_ready"]:
                result["result"] = "initial_two_omp_state_not_ready"
                return result

            prior: dict[MessageKind, str] = {}
            records: list[dict[str, object]] = []
            result["deliveries"] = records
            for kind, sender, target, parent_kind in CASES:
                reply_to = prior.get(parent_kind) if parent_kind is not None else None
                message = mailbox.create_message(
                    task_id, 1, run_id, sender, target, kind,
                    {"instruction": "Do not use tools; return a short reply."},
                    in_reply_to_message_id=reply_to,
                )
                expected = {
                    "workbench_message_id": message.message_id,
                    "kind": message.kind.value,
                    "task_id": message.task_id,
                    "revision_id": message.revision_id,
                    "run_id": message.run_id,
                }
                if reply_to is not None:
                    expected["in_reply_to_message_id"] = reply_to
                providers[target].semantic_expected[message.message_id] = expected
                before = {role: providers[role].request_count for role in ROLES}
                receipt = mailbox.deliver(message, timeout=30)
                after = {role: providers[role].request_count for role in ROLES}
                provider_matched = providers[target].semantic_seen.get(message.message_id) is True
                other = ActorRole.MANAGER if target is ActorRole.WORKER else ActorRole.WORKER
                duplicate = mailbox.deliver(message, timeout=5)
                records.append({
                    "kind": kind.value,
                    "sender_role": sender.value,
                    "target_role": target.value,
                    "message_id": message.message_id,
                    "delivery_attempt_id": receipt.delivery_attempt_id,
                    "in_reply_to_message_id": reply_to,
                    "status": receipt.status.value,
                    "provider_exact_message_fields_match": provider_matched,
                    "target_provider_request_delta": after[target] - before[target],
                    "other_provider_request_delta": after[other] - before[other],
                    "duplicate_same_receipt": duplicate == receipt,
                    "task_completed_claim": receipt.details.get("task_completed", False),
                })
                prior[kind] = message.message_id
                if not (
                    receipt.status is MailboxStatus.OMP_PROCESSED
                    and receipt.delivery_attempt_id is not None
                    and provider_matched
                    and after[target] - before[target] == 1
                    and after[other] - before[other] == 0
                    and duplicate == receipt
                    and receipt.details.get("task_completed") is False
                ):
                    result["result"] = "delivery_did_not_meet_runtime_assertions"
                    result["failed_status"] = receipt.status.value
                    result["failed_details"] = dict(receipt.details)
                    return result
                if not _ready(bridge, screens, timeout=15):
                    result["result"] = "post_delivery_omp_state_not_ready"
                    return result

            result["provider_request_totals"] = {
                role.value: providers[role].request_count for role in ROLES
            }
            result["result"] = "passed_actual_two_omp_four_kind_mailbox"
            return result
        except Exception as error:
            result["result"] = "runtime_exception"
            result["error_type"] = type(error).__name__
            return result
        finally:
            _stop_omps(children)
            result["omp_children_remaining"] = sum(
                Path(f"/proc/{child['pid']}").exists() for child in children
            )
            result["cwd_processes_remaining"] = {
                role.value: cwd_processes(root / f"cwd-{role.value}")
                for role in ROLES if (root / f"cwd-{role.value}").exists()
            }
            for thread, stop, _drained in drains:
                stop.set()
                thread.join(timeout=1)
            result["pty_drains"] = [drained for _, _, drained in drains]
            for child in children:
                try:
                    os.close(int(child["fd"]))
                except OSError:
                    pass
            bridge.close()
            result["bridge_socket_removed"] = not socket_path.exists()
            repository.close()
            for role in ROLES:
                providers[role].shutdown()
                providers[role].server_close()
                provider_threads[role].join(timeout=2)
            result["provider_threads_remaining"] = sum(thread.is_alive() for thread in provider_threads.values())


if __name__ == "__main__":
    binary = shutil.which("omp")
    if not binary or _omp_version(binary) != OMP_VERSION:
        raise SystemExit("requires OMP 18.2.10")
    observation = run(binary)
    print(json.dumps(observation, sort_keys=True))
    raise SystemExit(0 if observation["result"] == "passed_actual_two_omp_four_kind_mailbox"
                     and observation["omp_children_remaining"] == 0
                     and observation["bridge_socket_removed"]
                     and observation["provider_threads_remaining"] == 0
                     and all(not pids for pids in observation["cwd_processes_remaining"].values()) else 1)
