"""Bounded two-TUI pause/reconcile probe against actual OMP 18.2.10.

All task and approval records below are test-owned fixtures, not Workbench
backend records. Only metadata, counts, hashes and predicates are reported.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import shutil
import signal
import tempfile
import threading
import time
from uuid import uuid4

from live_omp_probe import BridgeHarness, EXTENSION, OMP_VERSION, _omp_version, _stop_omps
from live_pause_abort_probe import cwd_processes
from live_rpc_pause_probe import (
    file_manifest, fixture_digest, reconciliation_checks, semantic_provider,
)
from live_tui_contention_probe import _wait_until
from live_tui_draft_probe import _drain_visible, _screen_state
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


ROLES = ("manager", "worker")


def _start(omp: str, root: Path, role: str, token: str) -> dict[str, object]:
    cwd = root / f"cwd-{role}"
    cwd.mkdir()
    sessions = root / f"sessions-{role}"
    sessions.mkdir()
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        env = dict(os.environ)
        env.update({
            "TERM": "xterm-256color", "LANG": "C.UTF-8", "COLORTERM": "truecolor",
            "PI_CODING_AGENT_DIR": str(root / f"profile-{role}"),
            "WORKBENCH_G3_BRIDGE_SOCKET": str(root / "bridge.sock"),
            "WORKBENCH_G3_ROLE": role, "WORKBENCH_G3_TOKEN": token,
            "WORKBENCH_G3_GENERATION": "1",
            "WORKBENCH_G3_EXPECTED_OMP_VERSION": OMP_VERSION,
        })
        args = [
            omp, "--no-session", "--no-pty", "--no-skills", "--no-rules",
            "--no-title", "--no-extensions", "--extension", str(EXTENSION),
            "--model", "g3-tui-pause/scripted",
            "--tools=bash" if role == "manager" else "--no-tools",
            "--approval-mode=always-ask" if role == "manager" else "--max-time=45",
            "--max-time=45" if role == "manager" else "--config",
        ]
        if role == "manager":
            args.extend(("--config", str(root / "config.yml")))
        else:
            args.append(str(root / "config.yml"))
        args.extend(("--cwd", str(cwd), "--session-dir", str(sessions)))
        os.execvpe(omp, args, env)
    os.set_blocking(fd, False)
    return {"pid": pid, "fd": fd, "role": role}


def _state(server: BridgeHarness, role: str) -> dict[str, object]:
    state = server.request(role, {"kind": "probe"}, timeout=3).get("state")
    if not isinstance(state, dict):
        raise ValueError("bridge returned no state")
    return state


def _envelope(role: str, peer: dict[str, object], *, message_id: str | None = None) -> dict[str, object]:
    return {
        "schemaVersion": 1, "messageId": message_id or str(uuid4()),
        "deliveryAttemptId": str(uuid4()),
        "senderRole": "worker" if role == "manager" else "manager",
        "sessionId": str(peer["session_id"]), "sessionGeneration": int(peer["generation"]),
        "taskId": str(uuid4()), "revisionId": str(uuid4()), "runId": str(uuid4()),
        "event": {"type": "message", "messageKind": "report" if role == "manager" else "task",
                  "payload": {"text": "Test-owned pause probe; no tool needed."}},
    }


def _send(server: BridgeHarness, role: str, envelope: dict[str, object]) -> dict[str, object]:
    return server.request(role, {"kind": "deliver", "envelope": json.dumps(envelope)}, timeout=3)


def _files(cwd: Path) -> dict[str, object]:
    return file_manifest(cwd)


def _reconcile(
    server: BridgeHarness, root: Path, peer: dict[str, object], held: dict[str, object],
    paused_delivery: str, post_files: dict[str, object], post_processes: list[int],
) -> tuple[dict[str, object], bool]:
    cwd = root / "cwd-manager"
    task = {
        "task_id": held["taskId"], "revision_id": held["revisionId"],
        "run_id": held["runId"], "held_message_id": held["messageId"],
        "delivery_status": "deferred", "paused": True,
    }
    approval = {
        "task_id": held["taskId"], "run_id": held["runId"],
        "tool": "bash", "allowed_cwd": str(cwd),
        "allowed_files": ["started.txt", "result.txt"],
        "native_approval_before_pause": True,
    }
    expected = {
        "files": post_files, "omp_pid": peer["pid"],
        "cwd_processes": post_processes, "task_run": task,
        "approval_scope_digest": fixture_digest(approval),
    }
    current = _state(server, "manager")
    observed = {
        "files": _files(cwd), "omp_state": current,
        "omp_pid": peer["pid"], "omp_alive": Path(f"/proc/{peer['pid']}").exists(),
        "cwd_processes": cwd_processes(cwd),
        "task_run": {**task, "delivery_status": paused_delivery,
                     "paused": current.get("paused")},
        "approval_scope_digest": fixture_digest(approval),
    }
    positive = reconciliation_checks(expected, observed)
    changed_task = {**observed, "task_run": {**observed["task_run"], "run_id": str(uuid4())}}
    changed_approval = {**observed, "approval_scope_digest": fixture_digest({**approval, "tool": "write"})}
    changed_process = {**observed, "cwd_processes": [*observed["cwd_processes"], 1_000_000 + int(peer["pid"])]}
    negative_dir = root / "negative-scope"
    negative_dir.mkdir()
    for name in ("started.txt", "result.txt"):
        source = cwd / name
        if source.is_file() and not source.is_symlink():
            (negative_dir / name).write_bytes(source.read_bytes())
    (negative_dir / "out-of-scope.txt").write_text("DIFFERENT")
    changed_file = {**observed, "files": _files(negative_dir)}
    (negative_dir / "out-of-scope.txt").unlink()
    (negative_dir / "result.txt").unlink(missing_ok=True)
    (negative_dir / "result.txt").symlink_to(negative_dir / "started.txt")
    changed_symlink = {**observed, "files": _files(negative_dir)}
    negatives = {
        "task": reconciliation_checks(expected, changed_task),
        "approval": reconciliation_checks(expected, changed_approval),
        "process": reconciliation_checks(expected, changed_process),
        "file": reconciliation_checks(expected, changed_file),
        "symlink": reconciliation_checks(expected, changed_symlink),
    }
    negative_withheld = all(not all(checks.values()) for checks in negatives.values())
    scope = positive["files"] and observed["files"].get("scope_valid") is True
    resume_allowed = all(positive.values()) and negative_withheld and scope
    return ({
        "checks": positive, "negative_withheld": {name: not all(checks.values()) for name, checks in negatives.items()},
        "expected_files_sha256": fixture_digest(post_files),
        "observed_files_sha256": fixture_digest(observed["files"]),
        "approval_scope_sha256": expected["approval_scope_digest"],
        "omp_pid": peer["pid"], "cwd_process_count": len(observed["cwd_processes"]),
    }, resume_allowed)


def run(omp: str) -> dict[str, object]:
    result: dict[str, object] = {"result": "inconclusive", "version": OMP_VERSION, "mode": "two_tui"}
    with tempfile.TemporaryDirectory(prefix="cw04-g3-tui-pause-") as temporary:
        root = Path(temporary)
        (root / "config.yml").write_text("startup:\n  setupWizard: false\n")
        providers = {role: semantic_provider(manager=role == "manager") for role in ROLES}
        provider_threads = {role: threading.Thread(target=provider.serve_forever, daemon=True)
                            for role, provider in providers.items()}
        for thread in provider_threads.values():
            thread.start()
        for role in ROLES:
            profile = root / f"profile-{role}"
            profile.mkdir()
            (profile / "models.yml").write_text(
                "providers:\n  g3-tui-pause:\n"
                f"    baseUrl: http://127.0.0.1:{providers[role].server_port}/v1\n"
                "    api: openai-completions\n    auth: none\n    models:\n"
                "      - id: scripted\n        name: Scripted TUI pause\n"
                "        contextWindow: 32768\n        maxTokens: 1024\n"
            )
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
                child = _start(omp, root, role, tokens[role])
                children.append(child)
                fd = int(child["fd"])
                screen = TerminalScreen(100, 30, reply=lambda data, fd=fd: os.write(fd, data))
                lock = threading.Lock()
                screens[role] = (screen, lock)
                stop = threading.Event()
                drained: dict[str, object] = {"role": role}
                thread = threading.Thread(target=_drain_visible,
                                          args=(fd, stop, drained, make_stream(screen), lock), daemon=True)
                thread.start()
                drains.append((thread, stop, drained))
            peers = {role: server.peer(role, timeout=12) for role in ROLES}
            result["pids"] = {role: peers[role]["pid"] for role in ROLES}
            result["distinct_pids"] = len(set(result["pids"].values())) == 2

            def ready(role: str) -> bool:
                state = _state(server, role)
                return (
                    state.get("sessionId") == peers[role]["session_id"]
                    and state.get("generation") == peers[role]["generation"]
                    and state.get("idle") is True and state.get("pending") is False
                    and state.get("editorKnown") is True and state.get("editorEmpty") is True
                    and _screen_state(*screens[role])["composer_visible"]
                )

            result["initial_ready"] = _wait_until(lambda: all(ready(role) for role in ROLES), 10)
            result["initial_provider_requests"] = {
                role: providers[role].RequestHandlerClass.requests for role in ROLES
            }
            if not result["initial_ready"] or result["initial_provider_requests"] != {"manager": 0, "worker": 0}:
                result["reason"] = "initial_tui_state_unknown"
                return result

            manager_fd = int(children[0]["fd"])
            for byte in b"Run the local scripted pause probe.":
                os.write(manager_fd, bytes((byte,)))
                time.sleep(0.005)
            os.write(manager_fd, b"\r")
            server.wait_event("manager", "tool_approval_requested", timeout=12)
            result["native_approval_pending_before_pause"] = _state(server, "manager").get("approvalPending") is True
            os.write(manager_fd, b"y")
            time.sleep(0.1)
            os.write(manager_fd, b"\r")
            approval = server.wait_event("manager", "tool_approval_resolved", timeout=8)
            result["native_approval_resolved_before_pause"] = approval.get("approved") is True
            server.wait_event("manager", "tool_execution_start", timeout=8)
            started = root / "cwd-manager" / "started.txt"
            result["started_before_pause"] = _wait_until(started.exists, 2) and started.read_text() == "STARTED"
            result["result_absent_before_pause"] = not (root / "cwd-manager" / "result.txt").exists()
            result["tool_in_flight_before_pause"] = _state(server, "manager").get("inFlightToolCount") == 1
            if not all(result[key] for key in (
                "native_approval_pending_before_pause", "native_approval_resolved_before_pause",
                "started_before_pause", "result_absent_before_pause", "tool_in_flight_before_pause",
            )):
                result["reason"] = "native_tool_boundary_unknown"
                return result

            before_provider = {role: providers[role].RequestHandlerClass.requests for role in ROLES}
            before_start = {role: server.event_count(role, "agent_start") for role in ROLES}
            pause_ack = server.request("manager", {"kind": "pause"}, timeout=3)
            result["manager_pause_ack"] = pause_ack.get("status")
            result["manager_unknown_at_ack"] = pause_ack.get("unconfirmedToolCallIds")
            result["tool_end_at_ack"] = server.event_count("manager", "tool_execution_end") > 0
            worker_ack = server.request("worker", {"kind": "pause"}, timeout=3)
            result["worker_pause_ack"] = worker_ack.get("status")
            result["turn_stop_observed"] = _wait_until(
                lambda: server.event_count("manager", "turn_stop_observed") == 1, 10)
            result["manager_agent_end_observed"] = server.event_count("manager", "agent_end") == 1
            paused_state = {role: _state(server, role) for role in ROLES}
            result["paused_state"] = {
                role: {key: paused_state[role].get(key) for key in
                       ("paused", "abortStatus", "unknownOutcomeToolCallIds", "inFlightToolCount")}
                for role in ROLES
            }
            result["result_file_after_stop"] = (root / "cwd-manager" / "result.txt").exists()
            held = {role: _envelope(role, peers[role]) for role in ROLES}
            for role in ROLES:
                providers[role].semantic_expected[held[role]["messageId"]] = {
                    "workbench_message_id": held[role]["messageId"],
                    "kind": "report" if role == "manager" else "task",
                    "task_id": held[role]["taskId"],
                    "revision_id": held[role]["revisionId"],
                    "run_id": held[role]["runId"],
                }
            result["held_ids"] = {role: held[role]["messageId"] for role in ROLES}
            held_acks = {role: _send(server, role, held[role]) for role in ROLES}
            result["paused_delivery"] = {role: held_acks[role].get("status") for role in ROLES}
            result["resume_without_reconciliation"] = {
                role: server.request(role, {"kind": "resume"}, timeout=3).get("status")
                for role in ROLES
            }
            time.sleep(0.5)
            result["provider_delta_while_paused"] = {
                role: providers[role].RequestHandlerClass.requests - before_provider[role] for role in ROLES
            }
            result["agent_start_delta_while_paused"] = {
                role: server.event_count(role, "agent_start") - before_start[role] for role in ROLES
            }
            result["held_ids_absent_from_provider"] = {
                role: held[role]["messageId"] not in providers[role].semantic_seen for role in ROLES
            }
            post_files = _files(root / "cwd-manager")
            post_processes = cwd_processes(root / "cwd-manager")
            recon, allowed = _reconcile(
                server, root, peers["manager"], held["manager"],
                str(result["paused_delivery"]["manager"]), post_files, post_processes,
            )
            result["reconciliation"] = recon
            worker_state = _state(server, "worker")
            # The worker OMP process itself owns cwd-worker. No native tool is allowed.
            worker_safe = (
                worker_state.get("paused") is True
                and worker_state.get("abortStatus") == "none"
                and worker_state.get("unknownOutcomeToolCallIds") == []
                and cwd_processes(root / "cwd-worker") == [int(peers["worker"]["pid"])]
                and not any((root / "cwd-worker").iterdir())
            )
            result["worker_reconciled"] = worker_safe
            safe_to_resume = (
                allowed and worker_safe and result["turn_stop_observed"]
                and result["manager_pause_ack"] == "abort_requested"
                and result["manager_unknown_at_ack"] == ["pause-probe-bash"]
                and not result["tool_end_at_ack"]
                and result["worker_pause_ack"] == "paused"
                and result["paused_delivery"] == {"manager": "deferred", "worker": "deferred"}
                and result["resume_without_reconciliation"] == {
                    "manager": "reconciliation_required", "worker": "reconciliation_required"
                }
                and result["provider_delta_while_paused"] == {"manager": 0, "worker": 0}
                and result["agent_start_delta_while_paused"] == {"manager": 0, "worker": 0}
                and all(result["held_ids_absent_from_provider"].values())
            )
            result["explicit_resume_allowed"] = safe_to_resume
            if not safe_to_resume:
                result["reason"] = "reconciliation_or_pause_evidence_incomplete"
                return result
            result["resume_ack"] = {
                role: server.request(role, {"kind": "resume", "reconciled": True}, timeout=3).get("status")
                for role in ROLES
            }
            stale = {}
            for role in ROLES:
                held[role]["deliveryAttemptId"] = str(uuid4())
                stale[role] = _send(server, role, held[role]).get("status")
            result["held_retry_after_resume"] = stale
            result["ready_after_resume"] = _wait_until(lambda: all(ready(role) for role in ROLES), 8)
            if not result["ready_after_resume"]:
                result["reason"] = "post_resume_tui_state_unknown"
                return result
            fresh = _envelope("manager", peers["manager"])
            providers["manager"].semantic_expected[fresh["messageId"]] = {
                "workbench_message_id": fresh["messageId"], "kind": "report",
                "task_id": fresh["taskId"], "revision_id": fresh["revisionId"], "run_id": fresh["runId"],
            }
            prior_end = server.event_count("manager", "agent_end")
            event_start = len(server.events)
            prior_requests = providers["manager"].RequestHandlerClass.requests
            fresh_ack = _send(server, "manager", fresh)
            result["fresh_ack"] = fresh_ack.get("status")
            result["fresh_model_processed_at_ack"] = fresh_ack.get("modelProcessed")
            result["fresh_turn_end"] = _wait_until(
                lambda: server.event_count("manager", "agent_end") >= prior_end + 1, 10)
            fresh_ends = [event for event in server.events[event_start:]
                          if event.get("role") == "manager" and event.get("name") == "agent_end"]
            result["fresh_end_current_session"] = (
                len(fresh_ends) == 1
                and fresh_ends[0].get("sessionId") == peers["manager"]["session_id"]
                and fresh_ends[0].get("generation") == peers["manager"]["generation"]
                and server.event_count("manager", "agent_end") == prior_end + 1
            )
            result["fresh_provider_match"] = (
                providers["manager"].semantic_seen.get(fresh["messageId"], {}).get("matched") is True
            )
            result["fresh_provider_delta"] = providers["manager"].RequestHandlerClass.requests - prior_requests
            result["worker_provider_requests_final"] = providers["worker"].RequestHandlerClass.requests
            result["held_ids_still_absent"] = all(
                held[role]["messageId"] not in providers[role].semantic_seen for role in ROLES
            )
            result["post_resume_state"] = {
                role: {key: _state(server, role).get(key) for key in ("paused", "unknownOutcomeToolCallIds")}
                for role in ROLES
            }
            result["result"] = "passed_pair_tui" if (
                result["distinct_pids"] and result["manager_agent_end_observed"]
                and result["resume_ack"] == {"manager": "resumed", "worker": "resumed"}
                and result["held_retry_after_resume"] == {"manager": "unknown_no_replay", "worker": "unknown_no_replay"}
                and result["fresh_ack"] == "api_accepted"
                and result["fresh_model_processed_at_ack"] is False
                and result["fresh_turn_end"] and result["fresh_end_current_session"]
                and result["fresh_provider_match"]
                and result["fresh_provider_delta"] == 1
                and result["worker_provider_requests_final"] == 0
                and result["held_ids_still_absent"]
                and result["post_resume_state"]["manager"]["unknownOutcomeToolCallIds"] == ["pause-probe-bash"]
                and all(result["post_resume_state"][role]["paused"] is False for role in ROLES)
            ) else "inconclusive"
            return result
        except (TimeoutError, OSError, ValueError, AssertionError) as error:
            result["reason"] = type(error).__name__
            return result
        finally:
            _stop_omps(children)
            descendant_pids: set[int] = set()
            for role in ROLES:
                cwd = root / f"cwd-{role}"
                if cwd.exists():
                    for pid in cwd_processes(cwd):
                        descendant_pids.add(pid)
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
            deadline = time.monotonic() + 1
            while any(Path(f"/proc/{pid}").exists() for pid in descendant_pids) and time.monotonic() < deadline:
                time.sleep(0.02)
            for pid in descendant_pids:
                if Path(f"/proc/{pid}").exists():
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            result["omp_children_remaining"] = sum(
                Path(f"/proc/{child['pid']}").exists() for child in children
            )
            result["cwd_processes_remaining"] = {
                role: cwd_processes(root / f"cwd-{role}") for role in ROLES
            }
            for thread, stop, drained in drains:
                stop.set()
                thread.join(timeout=1)
            result["pty_drain"] = [drained for _, _, drained in drains]
            for child in children:
                os.close(int(child["fd"]))
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            for role in ROLES:
                providers[role].shutdown()
                providers[role].server_close()
                provider_threads[role].join(timeout=2)


if __name__ == "__main__":
    binary = shutil.which("omp")
    if binary is None or _omp_version(binary) != OMP_VERSION:
        raise SystemExit("requires OMP 18.2.10")
    observation = run(binary)
    print(json.dumps(observation, sort_keys=True))
    raise SystemExit(0 if observation["result"] == "passed_pair_tui"
                     and observation["omp_children_remaining"] == 0
                     and all(not pids for pids in observation["cwd_processes_remaining"].values()) else 1)
