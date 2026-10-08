"""CW-19: the production CW-13 ``RecoveryPort`` on SQLite.

``read`` builds fresh ``RecoveryFacts`` for one exact run from the Task
metadata (``tasks.sqlite3``, read through its own connections: the Task, its
revision, the persisted scope approval, the current run, revocations, session
settings and every run of the Task) and from the run facts the backend observes
now (``RunFacts``: terminal observation, automation state, terminal owner,
metadata health, raw log and result artifacts, the persisted worker report).
Any gap raises, so ``RecoveryCoordinator`` fails closed.

``reserve_recovery`` is an atomic, Task-wide compare-and-set in the port's own
``recovery.sqlite3`` (0600; ``BEGIN IMMEDIATE``), so the Task repository's schema
is not touched. TaskFlow's own re-run limit is separate (R7): its runs count
here as ``initial`` / ``normal_improvement`` attempts and only reservations made
through this port count as ``recovery``.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Mapping

from workbench.workflow.run import _canonical

from .policy import (
    Artifact, CompletionCriteria, ForceTarget, RecoveryFacts, RetryAttempt, RunIdentity, RunObservation,
    WorkerJudgment,
)

_SCHEMA = """CREATE TABLE IF NOT EXISTS recovery_attempts (
    task_id TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    ordinal INTEGER NOT NULL,
    authority_version INTEGER NOT NULL,
    settings_revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (task_id, ordinal)
)"""


@dataclass(frozen=True, slots=True)
class RunFacts:
    """What the backend observes now for one run (no Task metadata here)."""

    observation: RunObservation
    automation_state: Mapping[str, Any]
    terminal_owner: str  # workbench | user | unknown
    metadata_healthy: bool
    worktree_root: str
    raw_log_path: str
    raw_log: Artifact | None = None
    result: Artifact | None = None
    worker_report: Mapping[str, Any] | None = None
    worker_report_id: str | None = None
    worker_provenance: str = "durable_worker_report"
    delegated_force_target: ForceTarget | None = None
    source_commit: str = ""
    task_name: str = ""


class RecoveryFactsUnavailable(RuntimeError):
    """The durable or live facts for a run cannot be read; the decision fails closed."""


def approval_hash_of(task: Mapping[str, Any], approval: Mapping[str, Any]) -> str:
    """The run authority hash exactly as ``TaskWorkflow.start`` computes it."""
    return sha256(_canonical({"task": task, "approval": approval})).hexdigest()


class SqliteRecoveryPort:
    """See the module docstring."""

    def __init__(self, tasks_db: str | Path, recovery_db: str | Path,
                 run_facts: Callable[[RunIdentity], RunFacts], *, timeout: float = 5.0):
        self.tasks_db = str(tasks_db)
        self.recovery_db = Path(recovery_db)
        self._run_facts = run_facts
        self._timeout = timeout
        self._initialize()

    # -- storage ----------------------------------------------------------------------------
    def _connect_recovery(self) -> sqlite3.Connection:
        created = not self.recovery_db.exists()
        connection = sqlite3.connect(str(self.recovery_db), timeout=self._timeout, isolation_level=None)
        if created:
            os.chmod(self.recovery_db, 0o600)
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
        return connection

    def _initialize(self) -> None:
        connection = self._connect_recovery()
        try:
            connection.execute(_SCHEMA)
        finally:
            connection.close()

    # -- Task metadata (read through TaskRepository's public getters on its own connection) -------
    def _repository(self) -> Any:
        from workbench.tasks.repository import TaskRepository
        if not Path(self.tasks_db).exists():
            raise RecoveryFactsUnavailable("task metadata store is missing")
        return TaskRepository(self.tasks_db)

    @staticmethod
    def _authority(repository: Any, task_id: str, revision: int) -> dict[str, Any]:
        task = repository.get_task_spec(task_id, revision)
        decisions = repository.get_decisions(task_id, revision)
        approvals = [item for item in decisions if item["kind"] == "scope_approved"]
        if not approvals:
            raise RecoveryFactsUnavailable("the run has no persisted scope approval")
        return {"task": task, "approval": approvals[-1], "hash": approval_hash_of(task, approvals[-1]),
                "revoked": any(item["kind"] == "revoked" for item in decisions),
                "version": max(1, len(decisions))}

    @staticmethod
    def _current(repository: Any, task_id: str) -> RunIdentity:
        run = repository.get_current_run(task_id)
        if run is None:
            raise RecoveryFactsUnavailable("the Task has no current run")
        return RunIdentity(task_id, run["revision"], run["run_id"])

    def _attempts(self, task_id: str) -> tuple[RetryAttempt, ...]:
        if not Path(self.tasks_db).exists():
            raise RecoveryFactsUnavailable("task metadata store is missing")
        tasks = sqlite3.connect(f"file:{self.tasks_db}?mode=ro", uri=True, timeout=self._timeout)
        try:
            runs = tasks.execute("SELECT run_id FROM runs WHERE task_id = ? ORDER BY rowid", (task_id,)).fetchall()
        finally:
            tasks.close()
        attempts = [RetryAttempt(task_id, row[0], "initial" if index == 0 else "normal_improvement")
                    for index, row in enumerate(runs)]
        recovery = self._connect_recovery()
        try:
            rows = recovery.execute("SELECT decision_id FROM recovery_attempts WHERE task_id = ? ORDER BY ordinal",
                                    (task_id,)).fetchall()
        finally:
            recovery.close()
        attempts.extend(RetryAttempt(task_id, row[0], "recovery") for row in rows)
        return tuple(attempts)

    # -- RecoveryPort -------------------------------------------------------------------------
    def read(self, identity: RunIdentity) -> RecoveryFacts:
        if not isinstance(identity, RunIdentity):
            raise TypeError("RunIdentity required")
        try:
            repository = self._repository()
            try:
                run = repository.get_run(identity.run_id)
                if (run["task_id"], run["revision"]) != (identity.task_id, identity.revision):
                    raise RecoveryFactsUnavailable("the run is not this Task revision's")
                current = self._current(repository, identity.task_id)
                authority = self._authority(repository, identity.task_id, identity.revision)
                current_authority = (authority if current.revision == identity.revision
                                     else self._authority(repository, identity.task_id, current.revision))
                settings = int(repository.get_session_settings()["revision"])
            finally:
                repository.close()
            attempts = self._attempts(identity.task_id)
        except RecoveryFactsUnavailable:
            raise
        except Exception as exc:  # sqlite3.Error, an unknown run/revision: no facts, fail closed
            raise RecoveryFactsUnavailable(f"task metadata unreadable: {type(exc).__name__}") from exc
        spec = authority["task"].get("spec") if isinstance(authority["task"], Mapping) else None
        task = spec if isinstance(spec, Mapping) else {}
        execution = task.get("execution") if isinstance(task.get("execution"), Mapping) else {}
        criteria = execution.get("criteria")
        if not isinstance(criteria, Mapping):
            raise RecoveryFactsUnavailable("the Task has no completion criteria (not an experiment run)")
        details = authority["approval"].get("details") or {}
        paths = details.get("paths") if isinstance(details, Mapping) else None
        facts = self._run_facts(identity)
        if not isinstance(facts, RunFacts):
            raise RecoveryFactsUnavailable("the run's live facts are unavailable")
        worker = None
        if facts.worker_report is not None and facts.worker_report_id:
            try:
                worker = WorkerJudgment.from_cw11_report(facts.worker_report, report_id=facts.worker_report_id,
                                                         provenance=facts.worker_provenance)
            except (ValueError, TypeError):
                worker = None  # a malformed report is not evidence
        # the authority the run started under (TaskWorkflow.start records it) against the authority now
        inputs = run.get("inputs") if isinstance(run.get("inputs"), Mapping) else {}
        started_hash = inputs.get("approval_hash") if isinstance(inputs.get("approval_hash"), str) else None
        return RecoveryFacts(
            identity=identity, current_identity=current, approval_hash=started_hash or authority["hash"],
            current_approval_hash=current_authority["hash"],
            criteria=CompletionCriteria(**{name: criteria.get(name) for name in
                                           ("log_contains", "result_file", "result_contains")}),
            approved_paths=tuple(paths or ()), automation_state=dict(facts.automation_state),
            revoked=bool(authority["revoked"]), metadata_healthy=facts.metadata_healthy is True,
            terminal_owner=facts.terminal_owner, settings_revision=settings,
            authority_version=current_authority["version"], observation=facts.observation, worker=worker,
            raw_log=facts.raw_log, result=facts.result, worktree_root=facts.worktree_root,
            raw_log_path=facts.raw_log_path, attempts=attempts,
            delegated_force_target=facts.delegated_force_target, source_commit=facts.source_commit,
            task_name=facts.task_name)

    def reserve_recovery(self, *, task_id: str, decision_id: str, expected_used: int,
                         expected_authority_version: int, expected_settings_revision: int, maximum: int) -> bool:
        """One reservation per decision; false on a duplicate, a stale count/authority/settings or the limit."""
        try:
            repository = self._repository()
            try:
                current = self._current(repository, task_id)
                version = self._authority(repository, task_id, current.revision)["version"]
                settings = int(repository.get_session_settings()["revision"])
            finally:
                repository.close()
        except Exception:
            return False
        if version != expected_authority_version or settings != expected_settings_revision:
            return False
        connection = self._connect_recovery()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                used = connection.execute("SELECT COUNT(*) FROM recovery_attempts WHERE task_id = ?",
                                          (task_id,)).fetchone()[0]
                if used != expected_used or used >= maximum:
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    "INSERT INTO recovery_attempts(task_id, decision_id, ordinal, authority_version, "
                    "settings_revision, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, decision_id, used + 1, version, settings, time.time()))
                connection.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                return False
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        except sqlite3.Error:
            return False
        finally:
            connection.close()


__all__ = ["RecoveryFactsUnavailable", "RunFacts", "SqliteRecoveryPort", "approval_hash_of"]
