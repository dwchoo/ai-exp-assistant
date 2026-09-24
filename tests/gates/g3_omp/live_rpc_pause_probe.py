"""One bounded actual OMP 18.2.10 RPC manager pause/resume probe.

Uses the product bridge, a local scripted provider, one temporary cwd and UDS.
RPC stdout is drained to event metadata only; response bodies are discarded.
A successful result is partial G3 evidence, not the two-role gate.
"""

from __future__ import annotations

from hashlib import sha256
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import BridgeHarness
from live_pause_abort_probe import EXTENSION, VERSION, cwd_processes


def semantic_provider(*, manager: bool) -> ThreadingHTTPServer:
    """Serve fixed responses while checking only expected mailbox metadata."""

    class Handler(BaseHTTPRequestHandler):
        requests = 0

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            type(self).requests += 1
            payloads: list[dict[str, object]] = []
            user_messages = 0
            try:
                request = json.loads(body)
                messages = request.get("messages", []) if isinstance(request, dict) else []
                if not isinstance(messages, list):
                    messages = []
                for message in messages:
                    if not isinstance(message, dict) or message.get("role") != "user":
                        continue
                    user_messages += 1
                    content = message.get("content")
                    text_parts = [content] if isinstance(content, str) else [
                        part.get("text") for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    ] if isinstance(content, list) else []
                    for text_part in text_parts:
                        try:
                            candidate = json.loads(text_part)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(candidate, dict) and isinstance(candidate.get("workbench_message_id"), str):
                            payloads.append(candidate)
            except (UnicodeDecodeError, ValueError):
                pass
            # Only counts, booleans and digests survive this request handler.
            self.server.semantic_diagnostics.append({
                "request_index": self.requests, "user_message_count": user_messages,
                "structured_payload_count": len(payloads),
            })
            for payload in payloads:
                message_id = payload["workbench_message_id"]
                expected = self.server.semantic_expected.get(message_id)
                if expected is None:
                    continue
                fields = {key: payload.get(key) for key in (
                    "workbench_message_id", "kind", "task_id", "revision_id", "run_id",
                    "in_reply_to_message_id",
                )}
                matched = all(fields[key] == value for key, value in expected.items())
                if "in_reply_to_message_id" not in expected:
                    matched = matched and "in_reply_to_message_id" not in payload
                self.server.semantic_seen[message_id] = {
                    "matched": matched,
                    "fields_sha256": sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest(),
                }

            first_manager_request = manager and self.requests == 1
            if first_manager_request:
                delta = {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0, "id": "pause-probe-bash", "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({
                            "command": "printf STARTED > started.txt; sleep 8; printf FINISHED > result.txt"
                        })},
                    }],
                }
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "Pair probe response."}
                finish = "stop"
            frames = [
                {"id": "pair-probe", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "pair-probe", "object": "chat.completion.chunk", "created": 0,
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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.semantic_expected = {}
    server.semantic_seen = {}
    server.semantic_diagnostics = []
    return server


class RpcCapture:
    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.condition = threading.Condition()
        self.frames: list[dict[str, object]] = []
        self.bytes = 0
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            self.bytes += len(raw)
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            # Event payloads can contain message/tool/model text. Retain only
            # the top-level frame type needed for lifecycle classification.
            frame_type = frame.get("type")
            meta = {"type": frame_type if isinstance(frame_type, str) else "unknown"}
            with self.condition:
                self.frames.append(meta)
                self.condition.notify_all()

    def wait(self, predicate, timeout: float) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while time.monotonic() < deadline:
                match = next((frame for frame in self.frames if predicate(frame)), None)
                if match is not None:
                    return match
                self.condition.wait(deadline - time.monotonic())
        raise TimeoutError("RPC stdout event not observed")

    def send(self, frame: dict[str, object]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(frame) + "\n").encode())
        self.process.stdin.flush()


def stop_process(process: subprocess.Popen[bytes], cwd: Path) -> dict[str, object]:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
    for pid in cwd_processes(cwd):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 1
    while cwd_processes(cwd) and time.monotonic() < deadline:
        time.sleep(0.02)
    for pid in cwd_processes(cwd):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return {"omp_exit": process.returncode, "cwd_processes_after_cleanup": cwd_processes(cwd)}


def file_manifest(cwd: Path) -> dict[str, str | bool | None]:
    """Inspect two regular outputs and reject every other cwd entry."""
    result: dict[str, str | bool | None] = {}
    scope_valid = True
    for name in ("started.txt", "result.txt"):
        path = cwd / name
        if path.is_symlink():
            result[name] = "invalid_symlink"
            scope_valid = False
        elif path.exists() and not path.is_file():
            result[name] = "invalid_nonregular"
            scope_valid = False
        else:
            result[name] = sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    other_entries = sorted(
        path.name for path in cwd.iterdir()
        if path.name not in {"started.txt", "result.txt"}
    )
    result["other_entries_sha256"] = sha256(json.dumps(other_entries).encode()).hexdigest()
    result["scope_valid"] = scope_valid and not other_entries
    return result


def fixture_digest(fixture: dict[str, object]) -> str:
    return sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()


def reconciliation_checks(expected: dict[str, object], observed: dict[str, object]) -> dict[str, bool]:
    state = observed["omp_state"]
    assert isinstance(state, dict)
    return {
        "files": (
            observed["files"] == expected["files"]
            and expected["files"]["scope_valid"] is True
            and observed["files"]["scope_valid"] is True
        ),
        "tool_result_unknown": (
            state.get("paused") is True
            and state.get("abortStatus") == "stop_observed"
            and state.get("unknownOutcomeToolCallIds") == ["pause-probe-bash"]
        ),
        "terminal_process": (
            observed["omp_pid"] == expected["omp_pid"]
            and observed["omp_alive"] is True
            and expected["omp_pid"] in observed["cwd_processes"]
            and observed["cwd_processes"] == expected["cwd_processes"]
        ),
        "test_owned_task_run": observed["task_run"] == expected["task_run"],
        "test_owned_approval_scope": observed["approval_scope_digest"] == expected["approval_scope_digest"],
    }


def resume_if_reconciled(request, checks: dict[str, bool]) -> dict[str, object]:
    if not all(checks.values()):
        return {"sent": False, "status": "withheld"}
    ack = request({"kind": "resume", "reconciled": True})
    return {"sent": True, "status": ack.get("status")}


def _run_once(*, pair: bool = False) -> dict[str, object]:
    omp = shutil.which("omp")
    if not omp:
        return {"result": "inconclusive", "reason": "omp unavailable"}
    version = subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    if version != VERSION:
        return {"result": "inconclusive", "reason": "version mismatch", "version": version}
    with tempfile.TemporaryDirectory(prefix="cw04-g3-rpc-") as temporary:
        root = Path(temporary)
        cwd = root / "cwd"
        cwd.mkdir()
        profile = root / "agent"
        profile.mkdir()
        (root / "sessions").mkdir()
        config = root / "config.yml"
        config.write_text("# Isolated RPC probe overlay.\n")
        scripted = semantic_provider(manager=True)
        provider_thread = threading.Thread(target=scripted.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "providers:\n"
            "  g3-abort-probe:\n"
            f"    baseUrl: http://127.0.0.1:{scripted.server_port}/v1\n"
            "    api: openai-completions\n"
            "    auth: none\n"
            "    models:\n"
            "      - id: scripted\n"
            "        name: Scripted G3 abort probe\n"
            "        contextWindow: 32768\n"
            "        maxTokens: 1024\n"
        )
        token = str(uuid4())
        server = BridgeHarness(root / "bridge.sock", {"manager": token})
        os.chmod(root / "bridge.sock", 0o600)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        env = dict(os.environ)
        env.update({
            "PI_CODING_AGENT_DIR": str(profile),
            "WORKBENCH_G3_BRIDGE_SOCKET": str(root / "bridge.sock"),
            "WORKBENCH_G3_ROLE": "manager", "WORKBENCH_G3_TOKEN": token,
            "WORKBENCH_G3_GENERATION": "1", "WORKBENCH_G3_EXPECTED_OMP_VERSION": VERSION,
        })
        process = subprocess.Popen([
            omp, "--mode", "rpc", "--model", "g3-abort-probe/scripted",
            "--no-session", "--no-pty", "--no-skills", "--no-rules", "--no-title",
            "--no-extensions", "--extension", str(EXTENSION),
            "--tools=bash", "--auto-approve", "--max-time=30",
            "--cwd", str(cwd), "--session-dir", str(root / "sessions"),
            "--config", str(config),
        ], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
        capture = RpcCapture(process)
        worker_process: subprocess.Popen[bytes] | None = None
        worker_capture: RpcCapture | None = None
        worker_provider: ThreadingHTTPServer | None = None
        worker_provider_thread: threading.Thread | None = None
        worker_cwd = root / "cwd-worker"
        result: dict[str, object] = {"result": "inconclusive", "version": version, "mode": "rpc", "omp_pid": process.pid}
        try:
            capture.wait(lambda item: item.get("type") == "ready", timeout=10)
            peer = server.peer("manager", timeout=10)
            result["bridge_session_id"] = peer["session_id"]
            capture.send({"id": "prompt-1", "type": "prompt", "message": "Run the local scripted pause probe."})
            try:
                server.wait_event("manager", "tool_execution_start", timeout=12)
            except TimeoutError:
                result["reason"] = "native bash tool execution did not start"
                return result
            started_deadline = time.monotonic() + 2
            while not (cwd / "started.txt").exists() and time.monotonic() < started_deadline:
                time.sleep(0.02)
            result["started_file_before_pause"] = (cwd / "started.txt").read_text() == "STARTED" if (cwd / "started.txt").exists() else False
            result["cwd_processes_before_pause"] = cwd_processes(cwd)
            pause_started = time.monotonic()
            ack = server.request("manager", {"kind": "pause"}, timeout=3)
            result["pause_ack"] = ack.get("status")
            result["pause_ack_seconds"] = round(time.monotonic() - pause_started, 3)
            result["pause_unknown_tool_ids"] = ack.get("unconfirmedToolCallIds")
            result["tool_end_seen_at_ack"] = server.event_count("manager", "tool_execution_end") > 0
            envelope = {
                "schemaVersion": 1, "messageId": str(uuid4()), "deliveryAttemptId": str(uuid4()),
                "senderRole": "worker", "sessionId": str(peer["session_id"]),
                "sessionGeneration": int(peer["generation"]),
                "taskId": str(uuid4()), "revisionId": str(uuid4()), "runId": str(uuid4()),
                "event": {"type": "message", "messageKind": "report", "payload": {"text": "held probe"}},
            }
            # These task/run IDs and approval limits belong only to this gate
            # harness. They are not a Workbench backend approval record.
            task_fixture = {
                "task_id": envelope["taskId"], "revision_id": envelope["revisionId"],
                "run_id": envelope["runId"], "held_message_id": envelope["messageId"],
                "delivery_status": "deferred", "paused": True,
            }
            approval_fixture = {
                "task_id": envelope["taskId"], "run_id": envelope["runId"],
                "tool": "bash", "allowed_cwd": str(cwd),
                "allowed_files": ["started.txt", "result.txt"],
            }
            result["paused_delivery"] = server.request("manager", {
                "kind": "deliver", "envelope": json.dumps(envelope)
            }, timeout=3).get("status")
            try:
                server.wait_event("manager", "turn_stop_observed", timeout=10)
                result["turn_stop_observed"] = True
            except TimeoutError:
                result["turn_stop_observed"] = False
            result["agent_end_observed"] = server.event_count("manager", "agent_end") > 0
            post = server.request("manager", {"kind": "probe"}, timeout=3).get("state")
            result["post_stop_state"] = {
                key: post.get(key) for key in (
                    "paused", "abortStatus", "unconfirmedToolCallIds",
                    "unknownOutcomeToolCallIds", "editorKnown", "editorEmpty")
            } if isinstance(post, dict) else None
            result["started_file_exists"] = (cwd / "started.txt").exists()
            result["result_file_exists"] = (cwd / "result.txt").exists()
            result["cwd_processes_after_stop"] = cwd_processes(cwd)
            post_stop_files = file_manifest(cwd)
            check = server.request("manager", {"kind": "resume"}, timeout=3)
            result["resume_check"] = check.get("status")
            result["resume_check_paused"] = check.get("state", {}).get("paused") if isinstance(check.get("state"), dict) else None
            expected = {
                "files": post_stop_files,
                "omp_pid": process.pid,
                "cwd_processes": result["cwd_processes_after_stop"],
                "task_run": task_fixture,
                "approval_scope_digest": fixture_digest(approval_fixture),
            }
            current_state = server.request("manager", {"kind": "probe"}, timeout=3).get("state")
            observed = {
                "files": file_manifest(cwd),
                "omp_state": current_state,
                "omp_pid": peer["pid"],
                "omp_alive": process.poll() is None,
                "cwd_processes": cwd_processes(cwd),
                "task_run": {
                    **task_fixture,
                    "delivery_status": result["paused_delivery"],
                    "paused": current_state.get("paused") if isinstance(current_state, dict) else None,
                },
                "approval_scope_digest": fixture_digest(approval_fixture),
            }
            comparisons = reconciliation_checks(expected, observed)
            changed_approval = {**observed, "approval_scope_digest": fixture_digest({
                **approval_fixture, "tool": "write",
            })}
            changed_task = {**observed, "task_run": {
                **observed["task_run"], "run_id": str(uuid4()),
            }}
            negative_cwd = root / "negative-scope"
            negative_cwd.mkdir()
            for name in ("started.txt", "result.txt"):
                source = cwd / name
                if source.is_file() and not source.is_symlink():
                    (negative_cwd / name).write_bytes(source.read_bytes())
            extra_file = negative_cwd / "unapproved.txt"
            extra_file.write_text("OUT_OF_SCOPE")
            changed_extra_file = {**observed, "files": file_manifest(negative_cwd)}
            extra_file.unlink()
            symlink_name = negative_cwd / "result.txt"
            symlink_name.unlink(missing_ok=True)
            symlink_name.symlink_to(negative_cwd / "started.txt")
            changed_symlink = {**observed, "files": file_manifest(negative_cwd)}
            changed_processes = {**observed, "cwd_processes": [
                *observed["cwd_processes"], process.pid + 1_000_000,
            ]}

            def no_resume_allowed(_frame: dict[str, object]) -> dict[str, object]:
                raise AssertionError("negative reconciliation sent a resume signal")

            negative_approval = resume_if_reconciled(
                no_resume_allowed, reconciliation_checks(expected, changed_approval)
            )
            negative_task = resume_if_reconciled(
                no_resume_allowed, reconciliation_checks(expected, changed_task)
            )
            negative_extra_file = resume_if_reconciled(
                no_resume_allowed, reconciliation_checks(expected, changed_extra_file)
            )
            negative_symlink = resume_if_reconciled(
                no_resume_allowed, reconciliation_checks(expected, changed_symlink)
            )
            negative_process = resume_if_reconciled(
                no_resume_allowed, reconciliation_checks(expected, changed_processes)
            )
            explicit_resume = resume_if_reconciled(
                lambda frame: server.request("manager", frame, timeout=3), comparisons
            )
            result["resume_ack"] = explicit_resume["status"]
            result["reconciliation"] = {
                "checks": comparisons,
                "test_owned_task_id": envelope["taskId"],
                "test_owned_run_id": envelope["runId"],
                "test_owned_approval_scope_sha256": expected["approval_scope_digest"],
                "observed_files_sha256": fixture_digest(observed["files"]),
                "observed_omp_pid": observed["omp_pid"],
                "observed_unknown_tool_ids": current_state.get("unknownOutcomeToolCallIds") if isinstance(current_state, dict) else None,
                "negative_approval_withheld": negative_approval == {"sent": False, "status": "withheld"},
                "negative_task_withheld": negative_task == {"sent": False, "status": "withheld"},
                "negative_extra_file_withheld": negative_extra_file == {"sent": False, "status": "withheld"},
                "negative_symlink_withheld": negative_symlink == {"sent": False, "status": "withheld"},
                "negative_process_withheld": negative_process == {"sent": False, "status": "withheld"},
                "explicit_resume_sent": explicit_resume["sent"],
            }
            if explicit_resume["sent"] is not True:
                result["reason"] = "test-owned reconciliation did not match; resume withheld"
                return result
            envelope["deliveryAttemptId"] = str(uuid4())
            result["stale_retry"] = server.request("manager", {
                "kind": "deliver", "envelope": json.dumps(envelope)
            }, timeout=3).get("status")
            after = server.request("manager", {"kind": "probe"}, timeout=3).get("state")
            result["after_resume_state"] = {
                key: after.get(key) for key in ("paused", "unknownOutcomeToolCallIds", "editorKnown", "editorEmpty")
            } if isinstance(after, dict) else None
            result["provider_requests"] = scripted.RequestHandlerClass.requests
            result["rpc_frame_types"] = [frame.get("type") for frame in capture.frames]
            result["bridge_event_names"] = [event.get("name") for event in server.events]
            result["rpc_stdout_bytes_drained"] = capture.bytes
            result["result"] = "passed_manager_rpc" if (
                result["started_file_before_pause"]
                and result["pause_ack"] == "abort_requested"
                and result["pause_ack_seconds"] < 2
                and result["pause_unknown_tool_ids"] == ["pause-probe-bash"]
                and not result["tool_end_seen_at_ack"]
                and result["paused_delivery"] == "deferred"
                and result["turn_stop_observed"]
                and result["agent_end_observed"]
                and isinstance(result["post_stop_state"], dict)
                and result["post_stop_state"].get("paused") is True
                and result["post_stop_state"].get("abortStatus") == "stop_observed"
                and result["post_stop_state"].get("unknownOutcomeToolCallIds") == ["pause-probe-bash"]
                and result["resume_check"] == "reconciliation_required"
                and result["resume_check_paused"] is True
                and all(result["reconciliation"]["checks"].values())
                and result["reconciliation"]["negative_approval_withheld"]
                and result["reconciliation"]["negative_task_withheld"]
                and result["reconciliation"]["negative_extra_file_withheld"]
                and result["reconciliation"]["negative_symlink_withheld"]
                and result["reconciliation"]["negative_process_withheld"]
                and result["reconciliation"]["explicit_resume_sent"]
                and result["resume_ack"] == "resumed"
                and result["stale_retry"] == "unknown_no_replay"
                and isinstance(result["after_resume_state"], dict)
                and result["after_resume_state"].get("paused") is False
                and result["after_resume_state"].get("unknownOutcomeToolCallIds") == ["pause-probe-bash"]
            ) else "inconclusive"
            if pair and result["result"] == "passed_manager_rpc":
                worker_cwd.mkdir()
                worker_profile = root / "agent-worker"
                worker_profile.mkdir()
                (root / "sessions-worker").mkdir()
                worker_provider = semantic_provider(manager=False)
                worker_provider_thread = threading.Thread(target=worker_provider.serve_forever, daemon=True)
                worker_provider_thread.start()
                (worker_profile / "models.yml").write_text(
                    "providers:\n"
                    "  g3-pair-probe:\n"
                    f"    baseUrl: http://127.0.0.1:{worker_provider.server_port}/v1\n"
                    "    api: openai-completions\n"
                    "    auth: none\n"
                    "    models:\n"
                    "      - id: scripted\n"
                    "        name: Scripted G3 pair probe\n"
                    "        contextWindow: 32768\n"
                    "        maxTokens: 1024\n"
                )
                worker_token = str(uuid4())
                server.tokens["worker"] = worker_token
                worker_env = dict(os.environ)
                worker_env.update({
                    "PI_CODING_AGENT_DIR": str(worker_profile),
                    "WORKBENCH_G3_BRIDGE_SOCKET": str(root / "bridge.sock"),
                    "WORKBENCH_G3_ROLE": "worker", "WORKBENCH_G3_TOKEN": worker_token,
                    "WORKBENCH_G3_GENERATION": "1", "WORKBENCH_G3_EXPECTED_OMP_VERSION": VERSION,
                })
                worker_process = subprocess.Popen([
                    omp, "--mode", "rpc", "--model", "g3-pair-probe/scripted",
                    "--no-session", "--no-pty", "--no-tools", "--no-skills",
                    "--no-rules", "--no-title", "--no-extensions",
                    "--extension", str(EXTENSION), "--max-time=30",
                    "--cwd", str(worker_cwd), "--session-dir", str(root / "sessions-worker"),
                    "--config", str(config),
                ], cwd=worker_cwd, env=worker_env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
                worker_capture = RpcCapture(worker_process)
                worker_capture.wait(lambda item: item.get("type") == "ready", timeout=10)
                worker_peer = server.peer("worker", timeout=10)
                result["worker_pid"] = worker_process.pid
                result["independent_pids"] = worker_process.pid != process.pid
                worker_state = server.request("worker", {"kind": "probe"}, timeout=3).get("state")
                manager_state = server.request("manager", {"kind": "probe"}, timeout=3).get("state")
                result["pair_composer_ready"] = all(
                    isinstance(state, dict) and state.get("editorKnown") is True
                    and state.get("editorEmpty") is True
                    for state in (worker_state, manager_state)
                )
                if not result["pair_composer_ready"]:
                    result["pair_blocker"] = "RPC extension composer state unavailable"
                    return result

                def wait_agent_end(role: str, previous: int, timeout: float = 8) -> bool:
                    deadline = time.monotonic() + timeout
                    with server.condition:
                        while time.monotonic() < deadline:
                            if server.event_count(role, "agent_end") > previous:
                                return True
                            server.condition.wait(deadline - time.monotonic())
                    return False

                records: list[dict[str, object]] = []
                prior_ids: dict[str, str] = {}
                for kind, target, sender, target_peer in (
                    ("task", "worker", "manager", worker_peer),
                    ("question", "worker", "manager", worker_peer),
                    ("answer", "manager", "worker", peer),
                    ("report", "manager", "worker", peer),
                ):
                    message_id = str(uuid4())
                    message_event: dict[str, object] = {
                        "type": "message", "messageKind": kind,
                        "payload": {"text": f"RPC pair {kind} probe"},
                    }
                    if kind in ("answer", "report"):
                        message_event["inReplyToMessageId"] = prior_ids[
                            "question" if kind == "answer" else "task"
                        ]
                    envelope = {
                        "schemaVersion": 1, "messageId": message_id,
                        "deliveryAttemptId": str(uuid4()), "senderRole": sender,
                        "sessionId": str(target_peer["session_id"]),
                        "sessionGeneration": int(target_peer["generation"]),
                        "taskId": str(uuid4()), "revisionId": str(uuid4()), "runId": str(uuid4()),
                        "event": message_event,
                    }
                    expected_fields = {
                        "workbench_message_id": message_id,
                        "kind": kind,
                        "task_id": envelope["taskId"],
                        "revision_id": envelope["revisionId"],
                        "run_id": envelope["runId"],
                    }
                    if "inReplyToMessageId" in message_event:
                        expected_fields["in_reply_to_message_id"] = message_event["inReplyToMessageId"]
                    target_provider = worker_provider if target == "worker" else scripted
                    target_provider.semantic_expected[message_id] = expected_fields
                    previous_end = server.event_count(target, "agent_end")
                    ack = server.request(target, {"kind": "deliver", "envelope": json.dumps(envelope)}, timeout=3)
                    model_end = wait_agent_end(target, previous_end) if ack.get("status") == "api_accepted" else False
                    semantic = target_provider.semantic_seen.get(message_id)
                    records.append({
                        "kind": kind, "target": target, "ack": ack.get("status"),
                        "modelProcessedAtAck": ack.get("modelProcessed"),
                        "agent_end_after_send": model_end,
                        "workbench_message_id": message_id,
                        "provider_semantics_match": semantic is not None and semantic["matched"] is True,
                        "provider_fields_sha256": semantic["fields_sha256"] if semantic else None,
                    })
                    prior_ids[kind] = message_id
                    if not model_end:
                        break
                result["pair_records"] = records
                result["worker_provider_requests"] = worker_provider.RequestHandlerClass.requests
                result["manager_provider_requests"] = scripted.RequestHandlerClass.requests
                result["semantic_diagnostics"] = {
                    "worker": worker_provider.semantic_diagnostics,
                    "manager": scripted.semantic_diagnostics,
                }
                result["worker_rpc_stdout_bytes_drained"] = worker_capture.bytes
                result["result"] = "passed_pair_rpc" if (
                    result["independent_pids"]
                    and len(records) == 4
                    and all(record["ack"] == "api_accepted"
                            and record["modelProcessedAtAck"] is False
                            and record["agent_end_after_send"] is True
                            and record["provider_semantics_match"] is True
                            for record in records)
                    and result["worker_provider_requests"] >= 2
                    and result["manager_provider_requests"] >= 3
                ) else "inconclusive"
            return result
        except (TimeoutError, OSError, BrokenPipeError) as error:
            result["reason"] = type(error).__name__
            result["provider_requests"] = scripted.RequestHandlerClass.requests
            result["rpc_frame_types"] = [frame.get("type") for frame in capture.frames]
            result["bridge_event_names"] = [event.get("name") for event in server.events]
            return result
        finally:
            if worker_process is not None:
                result["worker_cleanup"] = stop_process(worker_process, worker_cwd)
            if worker_capture is not None:
                worker_capture.thread.join(timeout=2)
            if worker_provider is not None:
                worker_provider.shutdown()
                worker_provider.server_close()
            if worker_provider_thread is not None:
                worker_provider_thread.join(timeout=2)
            result.update(stop_process(process, cwd))
            capture.thread.join(timeout=2)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            scripted.shutdown()
            scripted.server_close()
            provider_thread.join(timeout=2)


def run(*, pair: bool = False) -> dict[str, object]:
    """Assign a pass verdict only after process and temp cwd cleanup is verified."""
    result = _run_once(pair=pair)
    expected = "passed_pair_rpc" if pair else "passed_manager_rpc"
    cleanup_ok = (
        result.get("omp_exit") == 0
        and result.get("cwd_processes_after_cleanup") == []
        and (not pair or (
            isinstance(result.get("worker_cleanup"), dict)
            and result["worker_cleanup"].get("omp_exit") == 0
            and result["worker_cleanup"].get("cwd_processes_after_cleanup") == []
        ))
    )
    if result.get("result") != expected or not cleanup_ok:
        result["result"] = "inconclusive"
        if not cleanup_ok:
            result["cleanup_verified"] = False
    else:
        result["cleanup_verified"] = True
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", action="store_true", help="also probe independent worker RPC delivery")
    pair = parser.parse_args().pair
    outcome = run(pair=pair)
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["result"] == ("passed_pair_rpc" if pair else "passed_manager_rpc") else 1)
