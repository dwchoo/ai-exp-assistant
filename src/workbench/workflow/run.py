"""Approved Task to worktree, persistent host shell, and worker evidence report."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import time
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from workbench.contracts.ports_v2 import dispatch_allowed
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import MailboxStatus, TaskMailbox
from workbench.tasks.repository import AuthorizationError, TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell
from .worktree import PreparedWorktree, WorktreePreparationError, prepare_execution_worktree
from .worker_port import WorkerResponseRejected, WorkerRolePort, verified_worker_response


class WorkflowHeld(RuntimeError):
    """No further automatic action is authorized or safely observable."""


class WorkerJudgmentUnavailable(WorkflowHeld):
    """smoke-04 G2/G3: the staged worker analysis was rejected, or its outcome is unknown.

    No worker judgment exists and the run sent no report (CW-10: nothing is replayed). ``reason`` is a short
    machine code (never response text); ``host_evidence`` is what Workbench itself checked (``judgment`` and
    ``reasons`` of the evidence guard), so the caller can tell the manager what was and was not judged.
    """

    def __init__(self, reason: str, host_evidence: Mapping[str, Any]):
        self.reason = reason
        self.host_evidence = dict(host_evidence)
        super().__init__(f"worker analysis unavailable ({reason}): no worker judgment, no report or replay")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _execution(spec: Mapping[str, Any]) -> dict[str, Any]:
    execution = spec.get("execution")
    if not isinstance(execution, dict) or set(execution) != {"source", "commit", "command", "criteria", "environment", "shell"}:
        raise ValueError("TaskSpec.execution must contain source, commit, command, criteria, environment, shell")
    if not isinstance(execution["source"], str) or not isinstance(execution["commit"], str):
        raise ValueError("source and commit must be strings")
    command = execution["command"]
    if not (isinstance(command, str) or isinstance(command, list) and command and all(isinstance(v, str) for v in command)):
        raise ValueError("command must be shell text or nonempty argv")
    criteria = execution["criteria"]
    if (not isinstance(criteria, dict) or set(criteria) != {"log_contains", "result_file", "result_contains"}
            or any(not isinstance(criteria[key], str) or not criteria[key] for key in criteria)):
        raise ValueError("criteria must name required log text and result-file text")
    environment = execution["environment"]
    if isinstance(environment, list):
        if (any(not isinstance(name, str) or not name for name in environment)
                or len(set(environment)) != len(environment)):
            raise ValueError("environment names must be unique nonempty strings")
    elif not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()):
        raise ValueError("environment must contain names or a legacy string mapping")
    if execution["shell"] not in {"bash", "sh"}:
        raise ValueError("only Bash/sh execution is supported")
    return execution


def _current_authority(repository: TaskRepository, task_id: str, revision: int, run_id: str,
                       approval_hash: str, automation_source: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
    """Re-read public metadata and automation immediately before new work."""
    state = dict(automation_source())
    if not dispatch_allowed(state):
        raise WorkflowHeld("current automation state denies new work")
    active = repository.get_current_run(task_id)
    if active is None or active["run_id"] != run_id or active["revision"] != revision:
        raise WorkflowHeld("run is no longer the current approved run")
    decisions = repository.get_decisions(task_id, revision)
    approvals = [decision for decision in decisions if decision["kind"] == "scope_approved"]
    task = repository.get_task_spec(task_id, revision)
    if (not approvals or any(decision["kind"] == "revoked" for decision in decisions)
            or sha256(_canonical({"task": task, "approval": approvals[-1]})).hexdigest() != approval_hash):
        raise WorkflowHeld("Task revision authority changed")
    return state


def _deliver_report(mailbox: Any, message: Any, timeout: float | None,
                    on_submitted: Callable[[], None] | None) -> Any:
    """``mailbox.deliver`` with ``on_submitted`` when the mailbox supports it (``TaskMailbox`` does)."""
    options: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
    if on_submitted is not None:
        try:
            parameters = inspect.signature(mailbox.deliver).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        if any(p.name == "on_submitted" or p.kind is p.VAR_KEYWORD for p in parameters):
            options["on_submitted"] = on_submitted
    return mailbox.deliver(message, **options)


def classify_worker_request(request: Mapping[str, Any], approved_paths: list[str] | None) -> str:
    """Advisory classification only; never grants execution authority."""
    def valid(path: Any) -> bool:
        return (isinstance(path, str) and bool(path) and not path.startswith("/")
                and not path.startswith("./") and "\\" not in path
                and all(part not in {"", ".", ".."} for part in path.rstrip("/").split("/")))

    paths = request.get("paths")
    if not isinstance(paths, list) or not paths or any(not valid(path) for path in paths):
        raise ValueError("worker request paths must be explicit relative paths")
    if approved_paths is not None:
        if not isinstance(approved_paths, list) or any(not valid(path) for path in approved_paths):
            raise ValueError("approved paths must be explicit relative paths")
        def covered(path: str) -> bool:
            request_path = PurePosixPath(path)
            return any(request_path == PurePosixPath(approved.rstrip("/"))
                       or approved.endswith("/") and request_path.is_relative_to(PurePosixPath(approved.rstrip("/")))
                       for approved in approved_paths)
        return "existing_task_scope" if all(covered(path) for path in paths) else "scope_expansion_manager_confirmation"
    if request.get("bounded_small_spec") is True and isinstance(request.get("goal"), str) and request["goal"].strip():
        return "bounded_new_spec_requires_manager_approval"
    return "scope_expansion_manager_confirmation"


@dataclass
class WorkflowRun:
    repository: TaskRepository
    mailbox: TaskMailbox
    task_id: str
    revision: int
    run_id: str
    task_message_id: str
    worktree: PreparedWorktree
    shell: PersistentShell
    execution: dict[str, Any]
    record_dir: Path
    raw_log: Path
    result_path: Path
    result_before: str | None
    approval_hash: str
    _record: dict[str, Any]
    _observed: set[str]
    automation_source: Callable[[], Mapping[str, Any]]
    worker_port: WorkerRolePort | None
    _terminal: bool = False
    _reported: bool = False
    # False when the caller injected the shell (the product host shell): it is never closed here.
    owns_shell: bool = True
    _report_message: Any = None
    _report_requires_code_change: bool = False
    _report_judgment: str | None = None
    _run_closed: bool = False

    def _persist(self) -> None:
        _write_json(self.result_path, self._record)

    def _drain(self) -> None:
        data = self.shell.display_bytes()
        if data:
            with self.raw_log.open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())

    def collect(self, *, timeout: float = 5, paused: bool = False) -> dict[str, Any]:
        """Collect shell facts even while paused; never start worker analysis here."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.shell.poll(min(0.03, max(0, deadline - time.monotonic())))
            self._drain()
            life = state["lifecycle"]
            for name, observed, kind in (
                ("accepted", life["accepted"], "accepted"),
                ("started", life["experiment_started"], "started"),
            ):
                if observed and name not in self._observed:
                    self.repository.record_shell_event(self.run_id, kind, {
                        "parent_pid": state["parent_pid"], "child_pid": life["child_pid"],
                        "cwd": str(self.worktree.path), "commit": self.worktree.commit,
                        "raw_log": str(self.raw_log), "result": str(self.result_path),
                    })
                    self._observed.add(name)
            if life["input_barrier"] and not life["input_returned"]:
                self.shell.release_input()
                continue
            if state["phase"] == "unknown":
                self._record.update({"shell_state": "unknown", "exit_status": life["main_exit"],
                                     "exit_confirmed": False, "unknowns": life["unknown"] or state["held_reasons"]})
                if "unknown" not in self._observed:
                    self.repository.record_shell_event(self.run_id, "failed", dict(self._record))
                    self._observed.add("unknown")
                self._persist()
                return dict(self._record)
            if life["control_returned"] and life["input_returned"] and life["lifetime"] == "ended":
                self._drain()
                try:
                    collected_log = self.raw_log.read_bytes()
                    self._record["raw_log_collected"] = {
                        "size": len(collected_log), "sha256": sha256(collected_log).hexdigest(),
                    }
                except OSError as exc:
                    self._record["raw_log_collected"] = {
                        "path": str(self.raw_log), "error": type(exc).__name__,
                    }
                result_file = (self.worktree.path / self.execution["criteria"]["result_file"]).resolve()
                if not result_file.is_relative_to(self.worktree.path):
                    self._record["result_collected"] = {
                        "path": str(result_file), "error": "PathEscapedWorktree",
                    }
                else:
                    try:
                        collected_result = result_file.read_bytes()
                        self._record["result_collected"] = {
                            "path": str(result_file), "size": len(collected_result),
                            "sha256": sha256(collected_result).hexdigest(),
                        }
                    except OSError as exc:
                        self._record["result_collected"] = {
                            "path": str(result_file), "error": type(exc).__name__,
                        }
                self._record.update({"shell_state": "exited", "exit_status": life["main_exit"],
                                     "exit_confirmed": True, "paused_at_collection": paused})
                if "ended" not in self._observed:
                    self.repository.record_shell_event(self.run_id, "ended", dict(self._record))
                    self._observed.add("ended")
                self._terminal = True
                self._persist()
                return dict(self._record)
        self._record.update({"shell_state": "running", "exit_status": self.shell.snapshot()["lifecycle"]["main_exit"],
                             "exit_confirmed": False, "paused_at_collection": paused})
        self._persist()
        return dict(self._record)

    def judge(self, *, paused: bool = False, requires_code_change: bool = False,
              code_change_reason: str = "",
              on_report: Callable[[str, Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
        """Judge the collected evidence and report it to the manager (once; never replayed).

        ``on_report`` (CW-18 R1) is told ``sending`` just before the report is
        delivered and ``submitted`` once the manager OMP accepted it into its
        session (mailboxes with ``on_submitted``). With ``on_report`` the run is
        closed at that acceptance, not when the manager's turn ends: the report
        reached the manager, and a later unknown receipt changes nothing.
        """
        if paused:
            return {"status": "deferred_paused", "run_id": self.run_id, "raw_log": str(self.raw_log)}
        if not self._terminal or self._record.get("shell_state") != "exited":
            raise WorkflowHeld("confirmed exit and input return are required before worker judgment")
        if self._reported:
            raise WorkflowHeld("worker judgment was already reported; no replay")
        if requires_code_change and not code_change_reason.strip():
            raise ValueError("code change request needs an evidence-backed reason")
        host: dict[str, Any] = {}  # the evidence guard's judgment once known
        try:
            evidence, judgment = self._analyse(requires_code_change, code_change_reason, host)
            self._record["worker_judgment"] = evidence
            self._persist()
            message = self.mailbox.create_message(
                self.task_id, self.revision, self.run_id, ActorRole.WORKER, ActorRole.MANAGER,
                MessageKind.REPORT, evidence, in_reply_to_message_id=self.task_message_id,
            )
            self._reported = True  # The delivery attempt may succeed before its caller sees a receipt.
            self._report_message = message
            self._report_requires_code_change = requires_code_change
            self._report_judgment = judgment
            self._record["report"] = {"message_id": message.message_id, "status": "delivery_unknown"}
            self._persist()
        except WorkflowHeld:
            raise
        except Exception as exc:  # fix-05 P2: any other analysis-stage failure is no worker judgment either
            self._record["worker_judgment"] = None
            if isinstance(self._record.get("report"), dict):  # created, never delivered (nor replayed)
                self._record["report"]["status"] = "not_sent"
            self._analysis_unavailable(f"analysis_error:{type(exc).__name__}",
                                       host.get("judgment"), host.get("reasons") or [])
        if on_report is not None:
            on_report("sending", dict(self._record))
        try:
            receipt = _deliver_report(self.mailbox, message, None, self._report_submitted(on_report))
        except BaseException:
            self._persist()
            raise
        return self._finish_report(receipt)

    def _analyse(self, requires_code_change: bool, code_change_reason: str,
                 host: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The evidence guard and the staged worker analysis: (report evidence, final judgment)."""
        _current_authority(self.repository, self.task_id, self.revision, self.run_id,
                           self.approval_hash, self.automation_source)
        criteria = self.execution["criteria"]
        evidence_errors: list[dict[str, str]] = []
        try:
            log = self.raw_log.read_bytes()
        except OSError as exc:
            log = None
            evidence_errors.append({"source": "raw_log", "path": str(self.raw_log),
                                    "error": type(exc).__name__})
        collected_log = self._record.get("raw_log_collected")
        if isinstance(collected_log, dict):
            if "error" in collected_log:
                evidence_errors.append({"source": "raw_log", "path": str(self.raw_log),
                                        "error": str(collected_log["error"]) + "AtCollection"})
            elif log is not None and (len(log) != collected_log["size"]
                                      or sha256(log).hexdigest() != collected_log["sha256"]):
                evidence_errors.append({"source": "raw_log", "path": str(self.raw_log),
                                        "error": "ChangedSinceCollection"})
        result_file = (self.worktree.path / criteria["result_file"]).resolve()
        if not result_file.is_relative_to(self.worktree.path):
            result_bytes = None
            evidence_errors.append({"source": "result_file", "path": str(result_file),
                                    "error": "PathEscapedWorktree"})
        else:
            try:
                result_bytes = result_file.read_bytes()
            except OSError as exc:
                result_bytes = None
                evidence_errors.append({"source": "result_file", "path": str(result_file),
                                        "error": type(exc).__name__})
        collected_result = self._record.get("result_collected")
        if isinstance(collected_result, dict):
            if "error" in collected_result:
                evidence_errors.append({"source": "result_file", "path": str(collected_result["path"]),
                                        "error": str(collected_result["error"]) + "AtCollection"})
            elif result_bytes is not None and (str(result_file) != collected_result["path"]
                                               or len(result_bytes) != collected_result["size"]
                                               or sha256(result_bytes).hexdigest() != collected_result["sha256"]):
                evidence_errors.append({"source": "result_file", "path": str(result_file),
                                        "error": "ChangedSinceCollection"})
        result_hash = sha256(result_bytes).hexdigest() if result_bytes is not None else None
        unknowns = [f"{item['source']} {item['path']}: {item['error']}" for item in evidence_errors]
        if log is None or any(item["source"] == "raw_log" for item in evidence_errors) or not log:
            judgment, reasons = "indeterminate", ["insufficient_raw_log"]
            if log == b"":
                unknowns.append("experiment emitted no raw log bytes")
        elif result_bytes is None or not result_bytes or any(item["source"] == "result_file" for item in evidence_errors):
            judgment, reasons = "indeterminate", ["missing_or_empty_result_file"]
            if result_bytes == b"":
                unknowns.append("result file is empty")
        elif self._record["exit_status"] != 0:
            judgment = "failure"
            reasons = ["nonzero_exit"]
        elif self.result_before is not None and self.result_before == result_hash:
            judgment, reasons = "indeterminate", ["result_provenance_unconfirmed"]
            unknowns.append("result file is unchanged from before the run")
        elif criteria["log_contains"].encode() not in log or criteria["result_contains"].encode() not in result_bytes:
            judgment, reasons = "failure", ["exit_zero_criteria_failed"]
        else:
            judgment, reasons = "success", ["criteria_met"]
        evidence = {
            "task_id": self.task_id, "revision": self.revision, "run_id": self.run_id,
            "judgment": judgment, "reasons": reasons, "unknowns": unknowns,
            "evidence_errors": evidence_errors,
            "exit_status": self._record["exit_status"], "exit_confirmed": True,
            "raw_log": str(self.raw_log), "raw_log_sha256": None if log is None else sha256(log).hexdigest(),
            "raw_log_excerpt": None if log is None else log[-1024:].decode("utf-8", "replace"),
            "result_file": str(result_file), "result_sha256": result_hash,
            "result_excerpt": None if result_bytes is None else result_bytes[:1024].decode("utf-8", "replace"),
            "criteria": criteria, "requires_code_change": requires_code_change,
            "code_change_reason": code_change_reason,
        }
        host.update({"judgment": judgment, "reasons": list(reasons)})
        if self.worker_port is not None:
            question = self.mailbox.create_message(
                self.task_id, self.revision, self.run_id, ActorRole.MANAGER, ActorRole.WORKER,
                MessageKind.QUESTION,
                {"stage": "analysis", "revision": self.revision,
                 "facts": {key: value for key, value in evidence.items()
                           if key not in {"judgment", "reasons"}}},
            )
            self.worker_port.arm("analysis", question)
            _current_authority(self.repository, self.task_id, self.revision, self.run_id,
                               self.approval_hash, self.automation_source)
            try:
                question_receipt = self.mailbox.deliver(question)
            except Exception as exc:
                self._record["worker_analysis_request"] = {"message_id": question.message_id,
                                                           "status": "delivery_error"}
                self._analysis_unavailable(f"analysis_delivery_error:{type(exc).__name__}", judgment, reasons)
            self._record["worker_analysis_request"] = {
                "message_id": question.message_id, "status": question_receipt.status.value,
            }
            self._persist()
            if question_receipt.status is MailboxStatus.DEFERRED:  # nothing was submitted: the caller asks again
                raise WorkflowHeld("worker analysis delivery was not confirmed; no report or replay")
            if question_receipt.status is not MailboxStatus.OMP_PROCESSED:
                self._analysis_unavailable(f"analysis_delivery_{question_receipt.status.value}", judgment, reasons)
            try:
                response = verified_worker_response(
                    "analysis", question, question_receipt,
                    self.worker_port.observe("analysis", question, question_receipt),
                )
            except WorkflowHeld:
                raise
            except WorkerResponseRejected as exc:  # the bridge's machine reason (no response text)
                self._analysis_unavailable(exc.reason, judgment, reasons)
            except TimeoutError:
                self._analysis_unavailable("worker_response_timeout", judgment, reasons)
            except ValueError:  # identity/linkage of the public response did not verify
                self._analysis_unavailable("worker_response_unverified", judgment, reasons)
            if ((judgment == "indeterminate" and response["decision"] != "indeterminate")
                    or (judgment == "failure" and response["decision"] == "success")):
                self._record["worker_analysis_response"] = response
                self._persist()
                self._analysis_unavailable("worker_judgment_conflicts_with_evidence", judgment, reasons)
            judgment = response["decision"]
            evidence["judgment"] = judgment
            evidence["worker_response"] = response
            self._record["worker_analysis_response"] = response
            _current_authority(self.repository, self.task_id, self.revision, self.run_id,
                               self.approval_hash, self.automation_source)
        return evidence, judgment

    def _analysis_unavailable(self, reason: str, judgment: str | None, reasons: list[str]) -> None:
        """smoke-04 G2/G3: record why the worker analysis is unavailable and raise; nothing is reported here.

        A pause, cancel or revoked authority keeps the plain CW-10 hold (``WorkflowHeld``) instead. A failing
        ``run.json`` write does not hide the outcome (fix-05): it is noted and the caller still closes the run.
        ``judgment`` is None when the analysis failed before the evidence guard judged (no host evidence).
        """
        _current_authority(self.repository, self.task_id, self.revision, self.run_id,
                           self.approval_hash, self.automation_source)
        host = {} if judgment is None else {"judgment": judgment, "reasons": list(reasons)}
        rejected = {"stage": "analysis", "reason": reason, "host_evidence": host or None}
        self._record["worker_analysis_rejected"] = rejected
        try:
            self._persist()
        except Exception as exc:
            rejected["persist_error"] = type(exc).__name__
        raise WorkerJudgmentUnavailable(reason, host)

    def _close_reported_run(self) -> None:
        if self._run_closed:
            return
        self._run_closed = True
        if self._report_requires_code_change:
            self.repository.fail_run(self.run_id, {"requires_code_change": True, "report": self._record["report"]})
        else:
            self.repository.complete_run(self.run_id, {"judgment": self._report_judgment,
                                                       "report": self._record["report"]})

    def _report_submitted(self, on_report: Callable[[str, Mapping[str, Any]], None] | None
                          ) -> Callable[[], None] | None:
        if on_report is None:
            return None

        def submitted() -> None:  # on this thread, inside the mailbox delivery
            self._record["report"]["status"] = MailboxStatus.API_RETURNED.value
            self._record["report"]["accepted_by_manager"] = True
            self._record["instruction_ended"] = self._report_requires_code_change
            try:  # fix-05: the report reached the manager; a failing run.json write must not contradict it
                self._persist()
            except Exception as exc:
                self._record["report"]["persist_error"] = type(exc).__name__
            self._close_reported_run()
            on_report("submitted", dict(self._record))
        return submitted

    def _finish_report(self, receipt: Any) -> dict[str, Any]:
        self._record["report"]["status"] = receipt.status.value
        self._record["instruction_ended"] = self._report_requires_code_change
        self._persist()
        if self._report_requires_code_change or receipt.status is MailboxStatus.OMP_PROCESSED:
            self._close_reported_run()
        return dict(self._record)

    def retry_report(self, *, timeout: float = 20,
                     on_report: Callable[[str, Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
        """Deliver the same report again only while its last receipt was ``deferred``.

        A deferred receipt means nothing was submitted to the manager OMP (the
        mailbox resends only that state); any other state is never replayed.
        """
        report = self._record.get("report") or {}
        if self._report_message is None or report.get("status") != MailboxStatus.DEFERRED.value:
            raise WorkflowHeld("only a deferred (never submitted) report is delivered again; no replay")
        if on_report is not None:
            on_report("sending", dict(self._record))
        try:
            receipt = _deliver_report(self.mailbox, self._report_message, timeout, self._report_submitted(on_report))
        except BaseException:
            self._record["report"]["status"] = "delivery_unknown"
            self._persist()
            raise
        return self._finish_report(receipt)

    def close(self) -> None:
        _release_shell(self.shell, self.owns_shell)


def _complete_line(path: Path) -> str | None:
    """The first line of ``path`` once it is complete (ends with a newline), else None (missing or partial)."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    line, newline, _ = text.partition("\n")
    return line if newline and line else None


def _release_shell(shell: Any, owned: bool) -> None:
    """Close a shell the workflow created; only detach from an injected one."""
    if owned:
        shell.close()
        return
    detach = getattr(shell, "detach", None)
    if callable(detach):
        detach()


class TaskWorkflow:
    def __init__(self, repository: TaskRepository, mailbox: TaskMailbox,
                 *, worker_port: WorkerRolePort | None = None,
                 automation_source: Callable[[], Mapping[str, Any]] | None = None):
        self.repository = repository
        self.mailbox = mailbox
        self.worker_port = worker_port
        self.automation_source = automation_source
        self._public_mailbox = isinstance(mailbox, TaskMailbox)

    def route_worker_request(self, request: Mapping[str, Any], *, task_id: str | None = None,
                             revision: int | None = None) -> dict[str, Any]:
        """Bind a direct request to durable Task authority; never dispatch it here."""
        if (task_id is None) != (revision is None):
            raise ValueError("Task ID and revision must be supplied together")
        task = self.repository.get_task_spec(task_id, revision) if task_id is not None else None
        approvals = ([decision for decision in self.repository.get_decisions(task_id, revision)
                      if decision["kind"] == "scope_approved"] if task is not None else [])
        approved_paths = approvals[-1]["details"].get("paths") if approvals else None
        category = classify_worker_request(request, approved_paths if task is not None else None)
        paths = list(request["paths"])
        if category == "existing_task_scope" and task is not None:
            spec = dict(task["spec"])
            if (self._public_mailbox and isinstance(spec.get("execution"), Mapping)
                    and not isinstance(spec["execution"].get("environment"), list)):
                raise WorkflowHeld("routed TaskSpec may persist environment names but not values")
            spec["worker_request"] = {"goal": spec.get("goal", "existing Task scope"),
                                      "paths": paths, "classification": category}
            routed_revision = self.repository.revise_task(task_id, spec)
            return {"classification": category, "task_id": task_id, "revision": routed_revision,
                    "prior_approval_decision_id": approvals[-1]["decision_id"],
                    "approval_decision_id": None,
                    "manager_action": "approve_scope_and_proceed", "dispatch_authorized": False}
        goal = request.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise WorkflowHeld("new or expanded scope requires a stated goal")
        proposed = request.get("spec")
        if proposed is not None and not isinstance(proposed, Mapping):
            raise ValueError("proposed TaskSpec must be a mapping")
        spec = dict(proposed) if proposed is not None else ({"goal": goal} if task is None else dict(task["spec"]))
        if (self._public_mailbox and isinstance(spec.get("execution"), Mapping)
                and not isinstance(spec["execution"].get("environment"), list)):
            raise WorkflowHeld("routed TaskSpec may persist environment names but not values")
        spec["worker_request"] = {"goal": goal, "paths": paths,
                                  "classification": category}
        if task is None:
            routed_task_id = self.repository.create_task(spec)
            routed_revision = 1
        else:
            routed_task_id = task_id
            routed_revision = self.repository.revise_task(task_id, spec)
        return {"classification": category, "task_id": routed_task_id,
                "revision": routed_revision, "approval_decision_id": None,
                "manager_action": "approve_scope_and_proceed", "dispatch_authorized": False}

    def start(self, task_id: str, revision: int, *, worktree_path: str | Path,
              artifacts_root: str | Path, automation: Mapping[str, Any],
              environment_values: Mapping[str, str] | None = None,
              shell: Any | None = None,
              before_shell_input: Callable[[], None] | None = None) -> WorkflowRun:
        """Start one approved run.

        ``before_shell_input`` (CW-18 R5) is called right before the first
        keystroke into the shell (after the worker's decision and the worktree
        preparation); it may raise ``WorkflowHeld`` so that nothing is typed.

        ``shell`` (CW-18 delta) injects an existing persistent host shell, the
        product host pane, instead of a new ``PersistentShell``. The run then
        uses that shell's own environment: ``environment_values`` must be None,
        the approved names are recorded, and the shell kind must equal the
        approved ``execution.shell``. An injected shell is never closed by the
        workflow (only ``detach()`` is called when it has one). Without
        ``shell`` the behaviour is unchanged.
        """
        if self._public_mailbox and (self.worker_port is None or self.automation_source is None):
            raise WorkflowHeld("public OMP execution requires worker-response and current-automation ports")
        automation_source = self.automation_source or (lambda: automation)
        if not dispatch_allowed(dict(automation_source())):
            raise WorkflowHeld("automation is paused, cancelled, unhealthy, or unapproved")
        task = self.repository.get_task_spec(task_id, revision)
        execution = _execution(task["spec"])
        environment_spec = execution["environment"]
        if self._public_mailbox and not isinstance(environment_spec, list):
            raise WorkflowHeld("public OMP TaskSpec must contain environment names, never values")
        injected = shell is not None
        if injected:
            if environment_values is not None:
                raise ValueError("an injected host shell runs with its own environment; pass no values")
            if not isinstance(environment_spec, list):
                raise WorkflowHeld("an injected host shell runs only TaskSpecs with environment names")
            kind = getattr(getattr(shell, "choice", None), "kind", None)
            if kind != execution["shell"]:
                raise WorkflowHeld("the host shell kind differs from the approved execution shell")
            shell_environment = {name: "" for name in environment_spec}  # names only; values stay in the shell
        elif isinstance(environment_spec, list):
            if environment_values is None or set(environment_values) != set(environment_spec) or any(
                not isinstance(value, str) for value in environment_values.values()
            ):
                raise WorkflowHeld("transient environment values must match approved variable names")
            shell_environment = dict(environment_values)
        else:
            shell_environment = dict(environment_spec)
        approvals = [d for d in self.repository.get_decisions(task_id, revision) if d["kind"] == "scope_approved"]
        if not approvals or approvals[-1]["details"].get("execution") != execution:
            raise AuthorizationError("execution is not the persisted approved TaskSpec scope")
        approval_hash = sha256(_canonical({"task": task, "approval": approvals[-1]})).hexdigest()
        try:
            if str(UUID(task_id)) != task_id:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValueError("mailbox Task ID must be a canonical UUID") from exc
        root = Path(artifacts_root).resolve()
        if not root.is_dir():
            raise WorkflowHeld("existing artifacts root is required")
        run_id = self.repository.start_run(task_id, revision, inputs={
            "source": execution["source"], "commit": execution["commit"],
            "worktree_path": str(Path(worktree_path).resolve()), "approval_hash": approval_hash,
        })
        record_dir = root / run_id
        try:
            record_dir.mkdir(mode=0o700)
            raw_log, result_path = record_dir / "raw.log", record_dir / "run.json"
            fd = os.open(raw_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except OSError as exc:
            self.repository.fail_run(run_id, {"stage": "artifact_setup", "error": type(exc).__name__})
            raise WorkflowHeld("durable artifact directory could not be prepared") from exc
        record: dict[str, Any] = {
            "task_id": task_id, "revision": revision, "run_id": run_id,
            "source": execution["source"], "commit": execution["commit"],
            "cwd": str(Path(worktree_path).resolve()), "environment_names": sorted(shell_environment),
            "command": execution["command"], "raw_log": str(raw_log), "result": str(result_path),
            "shell_state": "not_sent", "exit_status": None, "exit_confirmed": False,
            "worker_judgment": None, "approval_hash": approval_hash,
        }
        if injected:
            record["shell_source"] = "injected_host_shell"
        try:
            _write_json(result_path, record)
        except BaseException as exc:
            self.repository.fail_run(run_id, {"stage": "artifact_setup", "error": type(exc).__name__})
            raise
        host_shell, shell = shell, None
        task_message = None
        try:
            task_message = self.mailbox.create_message(
                task_id, revision, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                MessageKind.TASK, {"stage": "execute", "revision": revision,
                                   "instruction": "prepare and run the approved execution",
                                   "commit": execution["commit"]},
            )
            if self.worker_port is not None:
                self.worker_port.arm("execute", task_message)
            receipt = self.mailbox.deliver(task_message)
            record["worker_request"] = {"message_id": task_message.message_id, "status": receipt.status.value}
            _write_json(result_path, record)
            if receipt.status is not MailboxStatus.OMP_PROCESSED:
                raise WorkflowHeld("worker instruction was not confirmed processed; no execution or replay")
            if self.worker_port is not None:
                worker_decision = verified_worker_response(
                    "execute", task_message, receipt,
                    self.worker_port.observe("execute", task_message, receipt),
                )
                record["worker_execution_decision"] = worker_decision
                _write_json(result_path, record)
                if worker_decision["decision"] != "execute":
                    raise WorkflowHeld("worker OMP did not authorize execution")
            _current_authority(self.repository, task_id, revision, run_id, approval_hash, automation_source)
            worktree = prepare_execution_worktree(execution["source"], execution["commit"], worktree_path)
            record.update({"cwd": str(worktree.path), "source_status_before": worktree.source_status_before,
                           "source_status_after": worktree.source_status_after})
            _write_json(result_path, record)
            _current_authority(self.repository, task_id, revision, run_id, approval_hash, automation_source)
            if before_shell_input is not None:
                before_shell_input()  # e.g. the product host shell is checked idle and held from here
            if injected:
                shell = host_shell
            else:
                executable = "/bin/bash" if execution["shell"] == "bash" else "/bin/sh"
                shell = PersistentShell(user_environment=shell_environment,
                                        choice=ShellChoice(execution["shell"], executable))
            pwd_file = record_dir / "parent-cwd.txt"
            shell.send_user((f"cd {shlex.quote(str(worktree.path))} && pwd -P > {shlex.quote(str(pwd_file))}\n").encode())
            deadline = time.monotonic() + 3
            # p27-cd69-stuck-03: the redirection creates the file before pwd writes it; only a complete
            # (newline-terminated) line is compared, within the same deadline
            reported = _complete_line(pwd_file)
            while reported is None and time.monotonic() < deadline:
                shell.poll(0.02)
                shell.display_bytes()  # Preparation output is not experiment evidence.
                reported = _complete_line(pwd_file)
            if reported is None or Path(reported).resolve() != worktree.path:
                raise WorkflowHeld("parent shell cwd could not be verified; no experiment sent")
            shell.send_user(b"wb-handoff\n")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and shell.poll(0.02)["parent_mode"] != "control_wait":
                shell.display_bytes()
            shell.claim_manager()
            shell.display_bytes()
            state = shell.snapshot()
            control = {"portVersion": 2, "kind": "ShellControl", "payload": {
                "parentPid": state["parent_pid"], "generation": state["generation"],
                "ownerEpoch": state["owner_epoch"], "requestId": str(uuid4()),
                "approvalHash": approval_hash, "phase": "accepted",
            }}
            result_file = (worktree.path / execution["criteria"]["result_file"]).resolve()
            if not result_file.is_relative_to(worktree.path):
                raise WorkflowHeld("result-file path escapes the execution worktree")
            before = sha256(result_file.read_bytes()).hexdigest() if result_file.is_file() else None
            current_automation = _current_authority(
                self.repository, task_id, revision, run_id, approval_hash, automation_source,
            )
            shell.submit(control, execution["command"], current_automation)
            self.repository.record_shell_event(run_id, "sent", {
                "cwd": str(worktree.path), "commit": worktree.commit,
                "environment_names": sorted(shell_environment),
                "raw_log": str(raw_log), "result": str(result_path), "parent_pid": state["parent_pid"],
            })
            record.update({"shell_state": "sent", "parent_pid": state["parent_pid"],
                           "shell_request_id": control["payload"]["requestId"]})
            _write_json(result_path, record)
            return WorkflowRun(self.repository, self.mailbox, task_id, revision, run_id,
                               task_message.message_id, worktree, shell, execution,
                               record_dir, raw_log, result_path, before, approval_hash, record, {"sent"},
                               automation_source, self.worker_port, owns_shell=not injected)
        except BaseException as exc:
            if shell is not None:
                _release_shell(shell, not injected)
            preparation_error = {"type": type(exc).__name__, "detail": str(exc)}
            if isinstance(exc, WorkerResponseRejected):  # F2: the bridge's machine reason (no response text)
                preparation_error["reason"] = exc.reason
            record.update({"shell_state": "unknown" if isinstance(exc, WorkflowHeld) else "preparation_failed",
                           "preparation_error": preparation_error})
            active = self.repository.get_current_run(task_id)
            still_authorized = active is not None and active["run_id"] == run_id
            if (still_authorized and task_message is not None
                    and dispatch_allowed(dict(automation_source()))
                    and record.get("worker_request", {}).get("status") == MailboxStatus.OMP_PROCESSED.value
                    and (not self._public_mailbox or record.get("worker_execution_decision", {}).get("decision") == "execute")):
                try:
                    report_payload: dict[str, Any] = {
                        "stage": "preparation", "judgment": "indeterminate",
                        "error": record["preparation_error"], "requires_manager_resolution": True,
                    }
                    if self.worker_port is not None:
                        question = self.mailbox.create_message(
                            task_id, revision, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                            MessageKind.QUESTION,
                            {"stage": "analysis", "revision": revision,
                             "facts": {"preparation_error": record["preparation_error"]}},
                        )
                        self.worker_port.arm("analysis", question)
                        _current_authority(self.repository, task_id, revision, run_id,
                                           approval_hash, automation_source)
                        question_receipt = self.mailbox.deliver(question)
                        record["preparation_analysis_request"] = {
                            "message_id": question.message_id, "status": question_receipt.status.value,
                        }
                        _write_json(result_path, record)
                        if question_receipt.status is not MailboxStatus.OMP_PROCESSED:
                            raise WorkflowHeld("worker preparation analysis was not confirmed")
                        response = verified_worker_response(
                            "analysis", question, question_receipt,
                            self.worker_port.observe("analysis", question, question_receipt),
                        )
                        if response["decision"] != "indeterminate":
                            raise WorkflowHeld("worker preparation judgment conflicts with missing experiment")
                        report_payload["worker_response"] = response
                        _current_authority(self.repository, task_id, revision, run_id,
                                           approval_hash, automation_source)
                    report = self.mailbox.create_message(
                        task_id, revision, run_id, ActorRole.WORKER, ActorRole.MANAGER,
                        MessageKind.REPORT, report_payload,
                        in_reply_to_message_id=task_message.message_id,
                    )
                    record["preparation_report"] = {"message_id": report.message_id, "status": "delivery_unknown"}
                    _write_json(result_path, record)
                    preparation_receipt = self.mailbox.deliver(report)
                    record["preparation_report"]["status"] = preparation_receipt.status.value
                except BaseException as report_exc:
                    failed = record.setdefault("preparation_report", {"status": "not_created"})
                    failed["error"] = type(report_exc).__name__
                    if isinstance(report_exc, WorkerResponseRejected):  # G3: the bridge's machine reason
                        failed["reason"] = report_exc.reason
            _write_json(result_path, record)
            self.repository.record_shell_event(run_id, "failed", dict(record))
            if still_authorized:
                self.repository.fail_run(run_id, {"stage": "preparation", "error": record["preparation_error"]})
            try:  # CW-18 smoke-02 E3: the caller may tell the manager once, unless this run already reported
                exc.workbench_start_failure = {
                    "run_id": run_id, "revision": revision,
                    "task_message_id": None if task_message is None else task_message.message_id,
                    "manager_report": (record.get("preparation_report") or {}).get("status"),
                }
            except AttributeError:
                pass
            raise
