"""Actual two-OMP mailbox plus Git worktree and persistent host-shell CW-10 probe."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from uuid import uuid4
from uuid import UUID

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests/bridge"), str(REPO / "tests/gates/g3_omp")]
from live_mailbox_probe import _ready, _semantic_provider  # noqa: E402
from live_omp_probe import OMP_VERSION, _omp_version, _start_omp, _stop_omps  # noqa: E402
from live_pause_abort_probe import cwd_processes  # noqa: E402
from live_tui_draft_probe import _drain_visible  # noqa: E402
from workbench.contracts.v1 import ActorRole  # noqa: E402
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, TaskMailbox  # noqa: E402
from workbench.tasks.repository import TaskRepository  # noqa: E402
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream  # noqa: E402
from workbench.workflow import G3WorkerResponsePort, TaskWorkflow  # noqa: E402


ROLES = (ActorRole.MANAGER, ActorRole.WORKER)
AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True,
}}

_CONTRACT_INSTRUCTION = (
    "Emit only the marker immediately followed by one compact flat JSON object in field_order; "
    "no prose, tools, thinking, markdown, whitespace, or additional messages."
)
_CONTRACT_RULES = {"exactly_one_frame": True, "no_prose": True, "no_tools": True,
                   "no_thinking": True, "no_markdown": True, "no_extra_content": True}


def _frame_from_contract(observed: dict, decision: str) -> str | None:
    """Fixture model obeys the delivered contract; it owns no response schema.

    Root adjudication p27-cw18-response-id: response_id is a technical identity the bridge generates, so the
    delivered contract carries only literal identity values and one decision choice. The fixture model never
    invents an identifier; a legacy ``generate`` descriptor is still understood only so older fixed contracts
    (tests/workflow/test_worker_port.py) keep parsing, and the live independent run asserts none is delivered."""
    contract = observed.get("response_contract")
    if (not isinstance(contract, dict)
            or set(contract) != {"version", "marker", "format", "field_order", "fields",
                                 "output_rules", "instruction"}
            or type(contract.get("version")) is not int or contract["version"] != 1):
        return None
    marker = contract.get("marker")
    fields = contract.get("fields")
    order = contract.get("field_order")
    if (not isinstance(marker, str) or not marker.endswith(":") or not marker.isascii()
            or len(marker) > 80 or any(char.isspace() for char in marker)
            or contract.get("format") != "marker_plus_compact_flat_json"
            or contract.get("output_rules") != _CONTRACT_RULES
            or contract.get("instruction") != _CONTRACT_INSTRUCTION
            or not isinstance(fields, list) or not isinstance(order, list)
            or not fields or len(fields) != len(order)
            or any(not isinstance(item, dict) for item in fields)
            or any(type(name) is not str or not name.isidentifier() for name in order)
            or len(set(order)) != len(order)
            or [item.get("name") for item in fields] != order):
        return None
    response = {}
    generated = 0
    choice = 0
    for field in fields:
        name, field_type = field.get("name"), field.get("type")
        if field_type == "canonical_uuid":
            if set(field) == {"name", "type", "generate"} and field.get("generate") == "canonical_uuid":
                value = str(uuid4())
                generated += 1
            elif set(field) == {"name", "type", "value"} and type(field.get("value")) is str:
                value = field["value"]
            else:
                return None
            try:
                if str(UUID(value)) != value:
                    return None
            except ValueError:
                return None
        elif field_type == "positive_safe_integer":
            value = field.get("value")
            if set(field) != {"name", "type", "value"} or type(value) is not int or not 1 <= value <= 2**53 - 1:
                return None
        elif field_type == "literal_string":
            value = field.get("value")
            if set(field) != {"name", "type", "value"} or type(value) is not str:
                return None
        elif field_type == "enum_string":
            allowed = field.get("allowed")
            if (set(field) != {"name", "type", "allowed"}
                    or not isinstance(allowed, list) or not allowed
                    or any(type(item) is not str for item in allowed)
                    or len(set(allowed)) != len(allowed) or decision not in allowed):
                return None
            value = decision
            choice += 1
        else:
            return None
        response[name] = value
    if choice != 1:
        return None
    return marker + json.dumps(response, separators=(",", ":"))


def _worker_provider() -> ThreadingHTTPServer:
    """Scripted OMP model: decide from delivered identity and experiment facts."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.server.request_count += 1
            observed = None
            try:
                request = json.loads(raw)
                for item in reversed(request.get("messages", [])):
                    if not isinstance(item, dict) or item.get("role") != "user":
                        continue
                    content = item.get("content")
                    texts = [content] if isinstance(content, str) else [
                        block.get("text") for block in content
                        if isinstance(block, dict) and isinstance(block.get("text"), str)
                    ] if isinstance(content, list) else []
                    for text in texts:
                        try:
                            candidate = json.loads(text)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(candidate, dict) and "workbench_message_id" in candidate:
                            observed = candidate
                            break
                    if observed is not None:
                        break
            except (UnicodeDecodeError, TypeError, ValueError):
                pass
            content = None
            if observed is not None:
                payload = observed.get("payload", {})
                stage = payload.get("stage") if isinstance(payload, dict) else None
                if stage in {"execute", "analysis"}:
                    if stage == "execute":
                        decision = "execute"
                    else:
                        facts = payload.get("facts", {})
                        criteria = facts.get("criteria", {}) if isinstance(facts, dict) else {}
                        if (not facts.get("raw_log_excerpt") or not facts.get("result_excerpt")
                                or facts.get("evidence_errors") or facts.get("unknowns")):
                            decision = "indeterminate"
                        elif facts.get("exit_status") != 0:
                            decision = "failure"
                        elif (criteria.get("log_contains", "") not in facts["raw_log_excerpt"]
                              or criteria.get("result_contains", "") not in facts["result_excerpt"]):
                            decision = "failure"
                        else:
                            decision = "success"
                    self.server.observed_contracts.append(observed.get("response_contract"))
                    content = _frame_from_contract(observed, decision)
            if content is None:
                content = "invalid"
            frames = [
                {"id": "cw10-worker", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0,
                 "delta": {"role": "assistant", "content": content}, "finish_reason": None}]},
                {"id": "cw10-worker", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
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
    server.observed_contracts = []  # every response_contract the scripted worker was actually delivered
    return server


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, timeout=10, check=True).stdout.strip()


def run(omp: str) -> dict:
    observation = {"result": "inconclusive", "mode": "actual-two-omp-cw10-vertical",
                   "omp_version": _omp_version(omp)}
    with tempfile.TemporaryDirectory(prefix="cw10-live-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        git(source, "init", "-q")
        git(source, "config", "user.email", "cw10@example.invalid")
        git(source, "config", "user.name", "CW10 Fixture")
        (source / "tracked.txt").write_text("base\n")
        git(source, "add", "tracked.txt")
        git(source, "commit", "-qm", "fixture baseline")
        commit = git(source, "rev-parse", "HEAD")
        (source / "tracked.txt").write_text("user dirty\n")
        (source / "untracked.txt").write_text("preserve\n")
        source_status = git(source, "status", "--porcelain=v1", "--untracked-files=all")
        (root / "artifacts").mkdir()
        config = root / "config.yml"
        config.write_text("startup:\n  setupWizard: false\n")
        profile = root / "agent"
        profile.mkdir()
        secret_sentinel = "CW10_SECRET_" + uuid4().hex
        providers = {ActorRole.MANAGER: _semantic_provider(),
                     ActorRole.WORKER: _worker_provider()}
        provider_threads = {role: threading.Thread(target=provider.serve_forever, daemon=True)
                            for role, provider in providers.items()}
        for thread in provider_threads.values():
            thread.start()
        model_lines = ["providers:"]
        for role in ROLES:
            model_lines.extend((
                f"  cw10-{role.value}:",
                f"    baseUrl: http://127.0.0.1:{providers[role].server_port}/v1",
                "    api: openai-completions", "    auth: none", "    models:",
                "      - id: scripted", f"        name: CW-10 {role.value}",
                "        contextWindow: 32768", "        maxTokens: 1024",
            ))
        (profile / "models.yml").write_text("\n".join(model_lines) + "\n")
        tokens = {role.value: str(uuid4()) for role in ROLES}
        socket_path = root / "bridge.sock"
        bridge = G3BridgeServer(socket_path, tokens)
        bridge.start()
        repository = TaskRepository(root / "metadata.sqlite3")
        mailbox = TaskMailbox(repository, bridge)
        children, drains, screens = [], [], {}
        active = None
        try:
            for role in ROLES:
                child = _start_omp(omp, role.value, tokens[role.value], root,
                                   socket_path, config, profile=profile,
                                   model=f"cw10-{role.value}/scripted", max_time="60")
                children.append(child)
                fd = int(child["fd"])
                screen = TerminalScreen(100, 30, reply=lambda data, fd=fd: os.write(fd, data))
                stream, lock = make_stream(screen), threading.Lock()
                screens[role] = (screen, lock)
                stop = threading.Event()
                drained = {"role": role.value}
                thread = threading.Thread(target=_drain_visible,
                                          args=(fd, stop, drained, stream, lock), daemon=True)
                thread.start()
                drains.append((thread, stop, drained))
            peers = {role: bridge.peer(role, timeout=15) for role in ROLES}
            observation["omp_pids"] = {role.value: peers[role].pid for role in ROLES}
            observation["initial_ready"] = _ready(bridge, screens, timeout=15)
            if not observation["initial_ready"]:
                observation["result"] = "omp_not_ready"
                return observation
            execution = {"source": str(source), "commit": commit,
                         "command": "printf 'PASS\\n'; printf PASS > outcome.txt",
                         "criteria": {"log_contains": "PASS", "result_file": "outcome.txt",
                                      "result_contains": "PASS"},
                         "environment": ["PATH", "TERM", "CW10_SECRET_SENTINEL"],
                         "shell": "bash"}
            task_id = repository.create_task({"goal": "approved vertical runtime", "execution": execution})
            repository.approve_scope(task_id, 1, {"execution": execution, "paths": ["outcome.txt"]})
            repository.proceed(task_id, 1, "approved experiment")
            worker_port = G3WorkerResponsePort(bridge)
            workflow = TaskWorkflow(repository, mailbox, worker_port=worker_port,
                                    automation_source=lambda: AUTOMATION)
            active = workflow.start(task_id, 1, worktree_path=root / "execution",
                                    artifacts_root=root / "artifacts", automation=AUTOMATION,
                                    environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                        "CW10_SECRET_SENTINEL": secret_sentinel})
            collected = active.collect(timeout=8)
            judged = active.judge()
            durable_files = [path for path in root.rglob("*") if path.is_file()]
            secret_absent = all(secret_sentinel.encode() not in path.read_bytes() for path in durable_files)
            observation.update({
                "task_id": task_id, "run_id": active.run_id,
                "worker_request": judged.get("worker_request"), "worker_report": judged.get("report"),
                "cwd": judged["cwd"], "commit": judged["commit"],
                "exit_confirmed": collected["exit_confirmed"], "exit_status": collected["exit_status"],
                "judgment": judged["worker_judgment"]["judgment"],
                "log_sha256": judged["worker_judgment"]["raw_log_sha256"],
                "result_sha256": judged["worker_judgment"]["result_sha256"],
                "shell_events": [e["kind"] for e in repository.get_shell_history(active.run_id)],
                "run_events": [e["kind"] for e in repository.get_run_history(active.run_id)],
                "source_dirty_untracked_preserved":
                    git(source, "status", "--porcelain=v1", "--untracked-files=all") == source_status
                    and (source / "untracked.txt").read_text() == "preserve\n",
                "worktree_commit_verified": git(root / "execution", "rev-parse", "HEAD") == commit,
                "worktree_retained_at_observation": (root / "execution").is_dir(),
                "raw_log_exists": active.raw_log.is_file(),
                "result_record_exists": active.result_path.is_file(),
                "provider_requests": {role.value: providers[role].request_count for role in ROLES},
                "worker_execution_response": judged.get("worker_execution_decision"),
                "worker_analysis_response": judged.get("worker_analysis_response"),
                "secret_sentinel_absent_from_durable_files": secret_absent,
            })
            observation["result"] = "passed" if (
                judged["worker_request"]["status"] == "omp_processed"
                and judged["report"]["status"] == "omp_processed"
                and collected["exit_confirmed"] and collected["exit_status"] == 0
                and judged["worker_judgment"]["judgment"] == "success"
                and observation["source_dirty_untracked_preserved"]
                and observation["worktree_commit_verified"]
                and observation["raw_log_exists"] and observation["result_record_exists"]
                and observation["shell_events"] == ["sent", "accepted", "started", "ended"]
                and observation["worker_execution_response"]["decision"] == "execute"
                and observation["worker_analysis_response"]["decision"] == "success"
                and observation["secret_sentinel_absent_from_durable_files"]
                and all(count >= 1 for count in observation["provider_requests"].values())
            ) else "vertical_assertion_failed"
            return observation
        except Exception as exc:
            observation["result"] = "runtime_exception"
            observation["error_type"] = type(exc).__name__
            observation["error"] = str(exc)
            return observation
        finally:
            if active is not None:
                active.close()
            _stop_omps(children)
            observation["omp_children_remaining"] = sum(Path(f"/proc/{child['pid']}").exists() for child in children)
            observation["omp_cwd_processes_remaining"] = {
                role.value: cwd_processes(root / f"cwd-{role.value}") for role in ROLES
                if (root / f"cwd-{role.value}").exists()
            }
            for thread, stop, _drained in drains:
                stop.set()
                thread.join(timeout=1)
            for child in children:
                try:
                    os.close(int(child["fd"]))
                except OSError:
                    pass
            bridge.close()
            observation["bridge_socket_removed"] = not socket_path.exists()
            repository.close()
            for role in ROLES:
                providers[role].shutdown()
                providers[role].server_close()
                provider_threads[role].join(timeout=2)
            observation["provider_threads_remaining"] = sum(thread.is_alive() for thread in provider_threads.values())


if __name__ == "__main__":
    binary = shutil.which("omp")
    if not binary or _omp_version(binary) != OMP_VERSION:
        raise SystemExit("requires OMP 18.2.10")
    evidence = run(binary)
    print(json.dumps(evidence, sort_keys=True))
    raise SystemExit(0 if evidence["result"] == "passed"
                     and evidence["omp_children_remaining"] == 0
                     and evidence["bridge_socket_removed"]
                     and evidence["provider_threads_remaining"] == 0
                     and all(not pids for pids in evidence["omp_cwd_processes_remaining"].values()) else 1)
