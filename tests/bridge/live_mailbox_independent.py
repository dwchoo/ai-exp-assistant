"""Independent two-OMP CW-08 runtime: exact role/provider and durable evidence."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import OMP_VERSION, _omp_version, _start_omp, _stop_omps
from live_pause_abort_probe import cwd_processes
from live_tui_draft_probe import _drain_visible
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, MailboxStatus, TaskMailbox
from workbench.tasks.repository import TaskRepository
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


def provider():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = json.loads(body)
            self.server.calls += 1
            for message in request.get("messages", []):
                if message.get("role") != "user":
                    continue
                content = message.get("content")
                fragments = ([content] if isinstance(content, str) else
                             [part.get("text") for part in content if isinstance(part, dict)]
                             if isinstance(content, list) else [])
                for fragment in fragments:
                    try:
                        value = json.loads(fragment)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(value, dict) and isinstance(value.get("workbench_message_id"), str):
                        self.server.seen.add(value["workbench_message_id"])
            frames = [
                {"id": "independent", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": {"role": "assistant",
                 "content": "Acknowledged."}, "finish_reason": None}]},
                {"id": "independent", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            payload = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls = 0
    server.seen = set()
    return server


def ready(bridge, roles, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            states = [bridge.probe(role, timeout=1) for role in roles]
        except Exception:
            time.sleep(.05)
            continue
        if all(state.get("idle") is True and state.get("pending") is False and
               state.get("approvalPending") is False and state.get("editorKnown") is True and
               state.get("editorEmpty") is True and state.get("inFlightToolCount") == 0 and
               state.get("paused") is False for state in states):
            return True
        time.sleep(.05)
    return False


def run():
    roles = (ActorRole.MANAGER, ActorRole.WORKER)
    result = {"status": "inconclusive", "omp_version": OMP_VERSION, "cases": []}
    with tempfile.TemporaryDirectory(prefix="cw08-independent-live-") as temporary:
        root = Path(temporary)
        socket_path = root / "bridge.sock"
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        providers = {role: provider() for role in roles}
        provider_threads = {role: threading.Thread(target=providers[role].serve_forever, daemon=True)
                            for role in roles}
        for thread in provider_threads.values(): thread.start()
        models = ["providers:"]
        for role in roles:
            models.extend((f"  cw08-independent-{role.value}:",
                           f"    baseUrl: http://127.0.0.1:{providers[role].server_port}/v1",
                           "    api: openai-completions", "    auth: none", "    models:",
                           "      - id: scripted", "        name: CW08 independent",
                           "        contextWindow: 32768", "        maxTokens: 1024"))
        (profile / "models.yml").write_text("\n".join(models) + "\n")
        tokens = {role.value: str(uuid4()) for role in roles}
        bridge = G3BridgeServer(socket_path, tokens)
        bridge.start()
        repo = TaskRepository(root / "metadata.sqlite3")
        task = repo.create_task({"goal": "independent four-message exchange"})
        repo.approve_scope(task, 1, {"paths": []})
        repo.proceed(task, 1, "bounded")
        run_id = repo.start_run(task, 1)
        mailbox = TaskMailbox(repo, bridge)
        children, drains = [], []
        try:
            binary = shutil.which("omp")
            assert binary and _omp_version(binary) == OMP_VERSION
            for role in roles:
                child = _start_omp(binary, role.value, tokens[role.value], root, socket_path,
                                   config, profile=profile,
                                   model=f"cw08-independent-{role.value}/scripted", max_time="60")
                children.append(child)
                fd = int(child["fd"])
                screen = TerminalScreen(100, 30, reply=lambda data, fd=fd: os.write(fd, data))
                stream, lock = make_stream(screen), threading.Lock()
                stop = threading.Event()
                observed = {"role": role.value}
                thread = threading.Thread(target=_drain_visible,
                                          args=(fd, stop, observed, stream, lock), daemon=True)
                thread.start()
                drains.append((thread, stop, observed))
            peers = {role: bridge.peer(role, timeout=15) for role in roles}
            assert len({peer.pid for peer in peers.values()}) == 2
            assert ready(bridge, roles), "two OMP instances not initially ready"
            saved = {}
            for kind, sender, target, parent in (
                (MessageKind.TASK, ActorRole.MANAGER, ActorRole.WORKER, None),
                (MessageKind.QUESTION, ActorRole.MANAGER, ActorRole.WORKER, None),
                (MessageKind.ANSWER, ActorRole.WORKER, ActorRole.MANAGER, MessageKind.QUESTION),
                (MessageKind.REPORT, ActorRole.WORKER, ActorRole.MANAGER, MessageKind.TASK),
            ):
                parent_id = saved.get(parent)
                message = mailbox.create_message(task, 1, run_id, sender, target, kind,
                                                 {"instruction": "Give a short no-tools reply."},
                                                 in_reply_to_message_id=parent_id)
                before = {role: providers[role].calls for role in roles}
                receipt = mailbox.deliver(message, timeout=25)
                repeat = mailbox.deliver(message, timeout=5)
                after = {role: providers[role].calls for role in roles}
                other = ActorRole.MANAGER if target is ActorRole.WORKER else ActorRole.WORKER
                history = repo.get_delivery_history(receipt.delivery_attempt_id)
                case = {"kind": kind.value, "sender": sender.value, "target": target.value,
                        "message_id": message.message_id, "reply_to": parent_id,
                        "status": receipt.status.value, "same_receipt": repeat == receipt,
                        "target_delta": after[target] - before[target],
                        "other_delta": after[other] - before[other],
                        "provider_exact_id_seen": message.message_id in providers[target].seen,
                        "history": [event["status"] for event in history],
                        "task_completed": receipt.details.get("task_completed")}
                result["cases"].append(case)
                assert (receipt.status is MailboxStatus.OMP_PROCESSED and
                        case["same_receipt"] and case["target_delta"] == 1 and
                        case["other_delta"] == 0 and case["provider_exact_id_seen"] and
                        case["history"] == ["attempted", "api_returned", "omp_processed"] and
                        case["task_completed"] is False), case
                saved[kind] = message.message_id
                assert ready(bridge, roles), "OMP not ready for next kind"
            result["status"] = "passed"
        except Exception as error:
            result["status"] = "failed"
            result["error_type"] = type(error).__name__
            result["error_message"] = str(error)[:300]
        finally:
            _stop_omps(children)
            result["child_residue"] = [int(child["pid"]) for child in children
                                       if Path(f"/proc/{child['pid']}").exists()]
            result["cwd_process_residue"] = {role.value: cwd_processes(root / f"cwd-{role.value}")
                                             for role in roles if (root / f"cwd-{role.value}").exists()}
            for thread, stop, _observed in drains:
                stop.set()
                thread.join(timeout=1)
            result["drain_threads_remaining"] = sum(thread.is_alive() for thread, _, _ in drains)
            for child in children:
                try: os.close(int(child["fd"]))
                except OSError: pass
            bridge.close()
            result["socket_residue"] = socket_path.exists()
            repo.close()
            for role in roles:
                providers[role].shutdown()
                providers[role].server_close()
                provider_threads[role].join(timeout=2)
            result["provider_threads_remaining"] = sum(thread.is_alive()
                                                       for thread in provider_threads.values())
    return result


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, sort_keys=True))
    raise SystemExit(0 if outcome["status"] == "passed" and not outcome["child_residue"] and
                     all(not pids for pids in outcome["cwd_process_residue"].values()) and
                     not outcome["socket_residue"] and not outcome["provider_threads_remaining"]
                     and not outcome["drain_threads_remaining"] else 1)
