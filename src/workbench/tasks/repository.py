"""Transactional, append-only task metadata backed by SQLite."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from threading import local
from typing import Any
import uuid

from workbench.storage.metadata_sqlite.schema import (
    APPEND_ONLY_TABLES,
    MIGRATION_1_TO_2,
    SCHEMA,
    SCHEMA_VERSION,
)


class RepositoryClosedError(RuntimeError):
    """Raised when the repository is used after close()."""


class InvalidRevisionError(ValueError):
    """Raised when a Task or TaskSpec revision does not exist."""


class AuthorizationError(RuntimeError):
    """Raised when persisted approval and proceed are insufficient."""


class ActiveRunError(RuntimeError):
    """Raised when a Task already has a current run."""


class InvalidTransitionError(ValueError):
    """Raised when a requested metadata transition violates its contract."""


_run_guard_local = local()


@contextmanager
def persisted_run_guard(path: str):
    """Serialize active-run retirement with automatic dispatch opening.

    SQLite's write lock cannot span transport because the mailbox persists its
    attempt before sending. This inode lock also works across processes.
    """
    if path == ":memory:":
        yield
        return
    key = os.path.realpath(path)
    held = getattr(_run_guard_local, "held", None)
    if held is None:
        held = _run_guard_local.held = {}
    if key in held:
        held[key][1] += 1
        try:
            yield
        finally:
            held[key][1] -= 1
        return
    descriptor = os.open(key, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        held[key] = [descriptor, 1]
        try:
            yield
        finally:
            del held[key]
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(value: str | None, *, name: str) -> str:
    if value is None:
        return str(uuid.uuid4())
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InvalidTransitionError(f"{name} must be a non-empty string")
    return value


def _positive_integer(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidTransitionError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidTransitionError(f"{name} must be a non-negative integer")
    return value


def _json(value: Mapping[str, Any], *, name: str) -> str:
    if not isinstance(value, Mapping):
        raise InvalidTransitionError(f"{name} must be a mapping")
    try:
        return json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise InvalidTransitionError(f"{name} must be JSON serializable") from exc


def _decoded(value: str) -> Any:
    return json.loads(value)


def _dict(row: sqlite3.Row | None, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if row is None:
        return None
    return {field: row[field] for field in fields}


class TaskRepository:
    """A single-connection SQLite repository with explicit transactions.

    History tables reject UPDATE and DELETE at the database level. Mutable state is
    limited to the active-run pointer; durable run and decision facts are append-only.
    SQLite errors propagate to the caller, and a failed transaction is rolled back.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        # SQLite's REPLACE conflict algorithm deletes the conflicting row before
        # inserting its replacement. Delete triggers only run for that implicit
        # delete when recursive triggers are enabled, so append-only guards would
        # otherwise be bypassable with INSERT OR REPLACE.
        self._connection.execute("PRAGMA recursive_triggers = ON")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA synchronous = FULL")
        user_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if user_version not in (0, 1, SCHEMA_VERSION):
            self._connection.close()
            self._closed = True
            raise InvalidTransitionError(f"unsupported metadata schema version: {user_version}")
        try:
            if user_version == 1:
                self._connection.executescript(MIGRATION_1_TO_2)
            self._connection.executescript(SCHEMA)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            self._connection.close()
            self._closed = True
            raise
        for table in APPEND_ONLY_TABLES:
            self._connection.executescript(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_update
                    BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS {table}_immutable_delete
                    BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"""
            )
        self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._closed = False
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO session_settings(revision, settings_json, created_at) VALUES (1, '{}', ?)",
                (_now(),),
            )

    def __enter__(self) -> TaskRepository:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RepositoryClosedError("TaskRepository is closed")

    @contextmanager
    def _transaction(self):
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def _require_spec(self, connection: sqlite3.Connection, task_id: str, revision: int) -> None:
        if not isinstance(task_id, str) or not task_id.strip() or task_id != task_id.strip():
            raise InvalidRevisionError("task_id must be a non-empty string")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise InvalidRevisionError("revision must be a positive integer")
        row = connection.execute(
            "SELECT 1 FROM task_specs WHERE task_id = ? AND revision = ?",
            (task_id, revision),
        ).fetchone()
        if row is None:
            raise InvalidRevisionError(f"unknown TaskSpec revision: {task_id}@{revision}")

    def _require_run(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise InvalidTransitionError(f"unknown run: {run_id}")
        return row

    @staticmethod
    def _settings_revision(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT MAX(revision) FROM session_settings").fetchone()[0])

    @staticmethod
    def _insert_decision(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        revision: int,
        kind: str,
        settings_revision: int,
        retry_count_used: int,
        details_json: str,
        run_id: str | None = None,
    ) -> str:
        decision_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO task_decisions(
                decision_id, task_id, revision, run_id, settings_revision,
                retry_count_used, kind, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id,
                task_id,
                revision,
                run_id,
                settings_revision,
                retry_count_used,
                kind,
                details_json,
                _now(),
            ),
        )
        return decision_id

    @staticmethod
    def _latest_retry_count(connection: sqlite3.Connection, run: sqlite3.Row) -> int:
        row = connection.execute(
            "SELECT MAX(retry_count_used) FROM task_decisions WHERE run_id = ?",
            (run["run_id"],),
        ).fetchone()
        return max(int(run["retry_count_used"]), int(row[0] or 0))

    def create_task(self, spec: Mapping[str, Any], *, task_id: str | None = None) -> str:
        task_id = _identifier(task_id, name="task_id")
        spec_json = _json(spec, name="TaskSpec")
        now = _now()
        with self._transaction() as connection:
            connection.execute("INSERT INTO tasks(task_id, created_at) VALUES (?, ?)", (task_id, now))
            connection.execute(
                "INSERT INTO task_specs(task_id, revision, spec_json, created_at) VALUES (?, 1, ?, ?)",
                (task_id, spec_json, now),
            )
        return task_id

    def revise_task(self, task_id: str, spec: Mapping[str, Any]) -> int:
        spec_json = _json(spec, name="TaskSpec")
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is None:
                raise InvalidRevisionError(f"unknown Task: {task_id}")
            revision = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM task_specs WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO task_specs(task_id, revision, spec_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, revision, spec_json, _now()),
            )
        return revision

    def get_task_spec(self, task_id: str, revision: int | None = None) -> dict[str, Any]:
        self._ensure_open()
        if revision is not None:
            try:
                _positive_integer(revision, name="revision")
            except InvalidTransitionError as exc:
                raise InvalidRevisionError(str(exc)) from exc
        if revision is None:
            row = self._connection.execute(
                "SELECT * FROM task_specs WHERE task_id = ? ORDER BY revision DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT * FROM task_specs WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone()
        if row is None:
            raise InvalidRevisionError(f"unknown TaskSpec revision: {task_id}@{revision}")
        return {
            "task_id": row["task_id"],
            "revision": row["revision"],
            "spec": _decoded(row["spec_json"]),
            "created_at": row["created_at"],
        }

    def approve_scope(
        self,
        task_id: str,
        revision: int,
        scope: Mapping[str, Any],
        *,
        retry_count_used: int = 0,
    ) -> str:
        scope_json = _json(scope, name="delegation scope")
        if not scope:
            raise InvalidTransitionError("delegation scope approval must not be empty")
        retry_count_used = _nonnegative_integer(retry_count_used, name="retry_count_used")
        with self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            if connection.execute(
                "SELECT 1 FROM invalidated_revisions WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone():
                raise AuthorizationError("an invalidated TaskSpec revision cannot be re-approved")
            return self._insert_decision(
                connection,
                task_id=task_id,
                revision=revision,
                kind="scope_approved",
                settings_revision=self._settings_revision(connection),
                retry_count_used=retry_count_used,
                details_json=scope_json,
            )

    def proceed(
        self,
        task_id: str,
        revision: int,
        instruction: str | Mapping[str, Any],
        *,
        retry_count_used: int = 0,
    ) -> str:
        if isinstance(instruction, str):
            if not instruction.strip():
                raise InvalidTransitionError("proceed instruction must not be empty")
            details = {"instruction": instruction}
        elif isinstance(instruction, Mapping):
            details = dict(instruction)
            if not details:
                raise InvalidTransitionError("proceed instruction must not be empty")
        else:
            raise InvalidTransitionError("proceed instruction must be text or a mapping")
        details_json = _json(details, name="proceed instruction")
        retry_count_used = _nonnegative_integer(retry_count_used, name="retry_count_used")
        with self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            if connection.execute(
                "SELECT 1 FROM invalidated_revisions WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone():
                raise AuthorizationError("an invalidated TaskSpec revision cannot authorize a run")
            approved = connection.execute(
                """SELECT 1 FROM task_decisions
                   WHERE task_id = ? AND revision = ? AND kind = 'scope_approved'
                   LIMIT 1""",
                (task_id, revision),
            ).fetchone()
            if approved is None:
                raise AuthorizationError("delegation scope must be persisted before proceed")
            return self._insert_decision(
                connection,
                task_id=task_id,
                revision=revision,
                kind="proceed",
                settings_revision=self._settings_revision(connection),
                retry_count_used=retry_count_used,
                details_json=details_json,
            )

    def start_run(
        self,
        task_id: str,
        revision: int,
        *,
        inputs: Mapping[str, Any] | None = None,
    ) -> str:
        inputs_json = _json({} if inputs is None else inputs, name="run inputs")
        with self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            if connection.execute(
                "SELECT 1 FROM invalidated_revisions WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone():
                raise AuthorizationError("an invalidated TaskSpec revision cannot start a run")
            if connection.execute("SELECT 1 FROM active_runs WHERE task_id = ?", (task_id,)).fetchone():
                raise ActiveRunError(f"Task already has a current run: {task_id}")
            approved = connection.execute(
                """SELECT 1 FROM task_decisions
                   WHERE task_id = ? AND revision = ? AND kind = 'scope_approved'
                   LIMIT 1""",
                (task_id, revision),
            ).fetchone()
            if approved is None:
                raise AuthorizationError("persisted delegation scope approval is required")
            proceed = connection.execute(
                """SELECT decision_id, retry_count_used FROM task_decisions d
                   WHERE d.task_id = ? AND d.revision = ? AND d.kind = 'proceed'
                     AND NOT EXISTS (
                         SELECT 1 FROM authorization_consumption c
                         WHERE c.decision_id = d.decision_id
                     )
                   ORDER BY d.rowid DESC LIMIT 1""",
                (task_id, revision),
            ).fetchone()
            if proceed is None:
                raise AuthorizationError("a persisted, unused proceed instruction is required")

            settings_revision = self._settings_revision(connection)
            retry_count_used = int(proceed["retry_count_used"])
            run_id = str(uuid.uuid4())
            now = _now()
            connection.execute(
                """INSERT INTO runs(
                    run_id, task_id, revision, settings_revision,
                    retry_count_used, inputs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, task_id, revision, settings_revision, retry_count_used, inputs_json, now),
            )
            connection.execute(
                """INSERT INTO authorization_consumption(
                    decision_id, task_id, revision, run_id, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (proceed["decision_id"], task_id, revision, run_id, now),
            )
            connection.execute(
                """INSERT INTO run_events(
                    event_id, run_id, kind, settings_revision,
                    retry_count_used, details_json, created_at
                ) VALUES (?, ?, 'started', ?, ?, '{}', ?)""",
                (str(uuid.uuid4()), run_id, settings_revision, retry_count_used, now),
            )
            connection.execute(
                "INSERT INTO active_runs(task_id, run_id) VALUES (?, ?)", (task_id, run_id)
            )
            return run_id

    def get_run(self, run_id: str) -> dict[str, Any]:
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise InvalidTransitionError(f"unknown run: {run_id}")
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "revision": row["revision"],
            "settings_revision": row["settings_revision"],
            "retry_count_used": row["retry_count_used"],
            "inputs": _decoded(row["inputs_json"]),
            "created_at": row["created_at"],
        }

    def get_current_run(self, task_id: str) -> dict[str, Any] | None:
        self._ensure_open()
        row = self._connection.execute(
            """SELECT r.* FROM active_runs a JOIN runs r ON r.run_id = a.run_id
               WHERE a.task_id = ?""",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "revision": row["revision"],
            "settings_revision": row["settings_revision"],
            "retry_count_used": row["retry_count_used"],
            "inputs": _decoded(row["inputs_json"]),
            "created_at": row["created_at"],
        }

    def get_run_history(self, run_id: str) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "settings_revision": row["settings_revision"],
                "retry_count_used": row["retry_count_used"],
                "details": _decoded(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _terminal_run(self, run_id: str, kind: str, details: Mapping[str, Any]) -> None:
        details_json = _json(details, name="run event details")
        with persisted_run_guard(self.path), self._transaction() as connection:
            run = self._require_run(connection, run_id)
            active = connection.execute(
                "SELECT 1 FROM active_runs WHERE task_id = ? AND run_id = ?",
                (run["task_id"], run_id),
            ).fetchone()
            if active is None:
                raise InvalidTransitionError(f"run is not current: {run_id}")
            retry_count_used = self._latest_retry_count(connection, run)
            settings_revision = self._settings_revision(connection)
            decision_kind = "cancelled" if kind == "cancelled" else None
            decision_id = None
            if decision_kind:
                decision_id = self._insert_decision(
                    connection,
                    task_id=run["task_id"],
                    revision=run["revision"],
                    run_id=run_id,
                    kind=decision_kind,
                    settings_revision=settings_revision,
                    retry_count_used=retry_count_used,
                    details_json=details_json,
                )
            connection.execute(
                """INSERT INTO run_events(
                    event_id, run_id, kind, settings_revision,
                    retry_count_used, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()), run_id, kind, settings_revision,
                    retry_count_used, details_json, _now(),
                ),
            )
            if decision_id is not None:
                connection.execute(
                    """INSERT INTO invalidated_revisions(
                        task_id, revision, decision_id, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (run["task_id"], run["revision"], decision_id, str(details.get("reason", kind)), _now()),
                )
            connection.execute("DELETE FROM active_runs WHERE task_id = ?", (run["task_id"],))

    def cancel_run(self, run_id: str, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidTransitionError("cancellation reason must not be empty")
        self._terminal_run(run_id, "cancelled", {"reason": reason})

    def complete_run(self, run_id: str, result: Mapping[str, Any] | None = None) -> None:
        self._terminal_run(run_id, "completed", {} if result is None else result)

    def fail_run(self, run_id: str, failure: Mapping[str, Any] | None = None) -> None:
        self._terminal_run(run_id, "failed", {} if failure is None else failure)

    def revoke_authority(self, task_id: str, revision: int, reason: str) -> str:
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidTransitionError("revocation reason must not be empty")
        details_json = _json({"reason": reason}, name="revocation details")
        with persisted_run_guard(self.path), self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            if connection.execute(
                "SELECT 1 FROM invalidated_revisions WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone():
                raise AuthorizationError("TaskSpec revision authority is already invalidated")
            active = connection.execute(
                """SELECT r.* FROM active_runs a JOIN runs r ON r.run_id = a.run_id
                   WHERE a.task_id = ? AND r.revision = ?""",
                (task_id, revision),
            ).fetchone()
            settings_revision = self._settings_revision(connection)
            retry_count_used = 0 if active is None else self._latest_retry_count(connection, active)
            decision_id = self._insert_decision(
                connection,
                task_id=task_id,
                revision=revision,
                run_id=None if active is None else active["run_id"],
                kind="revoked",
                settings_revision=settings_revision,
                retry_count_used=retry_count_used,
                details_json=details_json,
            )
            connection.execute(
                """INSERT INTO invalidated_revisions(
                    task_id, revision, decision_id, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (task_id, revision, decision_id, reason, _now()),
            )
            if active is not None:
                connection.execute(
                    """INSERT INTO run_events(
                        event_id, run_id, kind, settings_revision,
                        retry_count_used, details_json, created_at
                    ) VALUES (?, ?, 'revoked', ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()), active["run_id"], settings_revision,
                        retry_count_used, details_json, _now(),
                    ),
                )
                connection.execute("DELETE FROM active_runs WHERE task_id = ?", (task_id,))
            return decision_id

    def set_session_settings(self, settings: Mapping[str, Any]) -> int:
        settings_json = _json(settings, name="session settings")
        with self._transaction() as connection:
            revision = int(
                connection.execute("SELECT MAX(revision) + 1 FROM session_settings").fetchone()[0]
            )
            connection.execute(
                "INSERT INTO session_settings(revision, settings_json, created_at) VALUES (?, ?, ?)",
                (revision, settings_json, _now()),
            )
            return revision

    def get_session_settings(self, revision: int | None = None) -> dict[str, Any]:
        self._ensure_open()
        if revision is None:
            row = self._connection.execute(
                "SELECT * FROM session_settings ORDER BY revision DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT * FROM session_settings WHERE revision = ?", (revision,)
            ).fetchone()
        if row is None:
            raise InvalidTransitionError(f"unknown session settings revision: {revision}")
        return {
            "revision": row["revision"],
            "settings": _decoded(row["settings_json"]),
            "created_at": row["created_at"],
        }

    def record_decision(
        self,
        task_id: str,
        revision: int,
        run_id: str,
        kind: str,
        *,
        retry_count_used: int,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        if kind not in {"retry", "continue", "blocked"}:
            raise InvalidTransitionError(f"unsupported run decision kind: {kind}")
        retry_count_used = _nonnegative_integer(retry_count_used, name="retry_count_used")
        details_json = _json({} if details is None else details, name="decision details")
        with self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            run = self._require_run(connection, run_id)
            if (run["task_id"], run["revision"]) != (task_id, revision):
                raise InvalidTransitionError("decision Task/revision must match its run")
            active = connection.execute(
                "SELECT 1 FROM active_runs WHERE task_id = ? AND run_id = ?",
                (task_id, run_id),
            ).fetchone()
            if active is None:
                raise InvalidTransitionError("decisions can only be linked to the current run")
            previous_count = self._latest_retry_count(connection, run)
            if retry_count_used < previous_count:
                raise InvalidTransitionError("retry count cannot be reset during a run")
            if kind == "retry" and retry_count_used <= previous_count:
                raise InvalidTransitionError("a retry decision must increase the used retry count")
            return self._insert_decision(
                connection,
                task_id=task_id,
                revision=revision,
                run_id=run_id,
                kind=kind,
                settings_revision=self._settings_revision(connection),
                retry_count_used=retry_count_used,
                details_json=details_json,
            )

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM task_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise InvalidTransitionError(f"unknown decision: {decision_id}")
        return {
            "decision_id": row["decision_id"],
            "task_id": row["task_id"],
            "revision": row["revision"],
            "run_id": row["run_id"],
            "settings_revision": row["settings_revision"],
            "retry_count_used": row["retry_count_used"],
            "kind": row["kind"],
            "details": _decoded(row["details_json"]),
            "created_at": row["created_at"],
        }

    def get_decisions(self, task_id: str, revision: int) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT decision_id FROM task_decisions WHERE task_id = ? AND revision = ? ORDER BY rowid",
            (task_id, revision),
        ).fetchall()
        return [self.get_decision(row["decision_id"]) for row in rows]

    def create_message(
        self,
        task_id: str,
        revision: int,
        run_id: str,
        content: Mapping[str, Any],
        *,
        message_id: str | None = None,
    ) -> str:
        message_id = _identifier(message_id, name="message_id")
        content_json = _json(content, name="logical message content")
        with self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            run = self._require_run(connection, run_id)
            if (run["task_id"], run["revision"]) != (task_id, revision):
                raise InvalidTransitionError("message Task/revision must match its run")
            connection.execute(
                """INSERT INTO messages(
                    message_id, task_id, revision, run_id, content_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (message_id, task_id, revision, run_id, content_json, _now()),
            )
        return message_id

    def get_message(self, message_id: str) -> dict[str, Any]:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise InvalidTransitionError(f"unknown message: {message_id}")
        return {
            "message_id": row["message_id"],
            "task_id": row["task_id"],
            "revision": row["revision"],
            "run_id": row["run_id"],
            "content": _decoded(row["content_json"]),
            "created_at": row["created_at"],
        }

    def create_delivery_attempt(self, message_id: str, *, attempt_id: str | None = None) -> str:
        attempt_id = _identifier(attempt_id, name="attempt_id")
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone() is None:
                raise InvalidTransitionError(f"unknown message: {message_id}")
            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM delivery_attempts WHERE message_id = ?",
                    (message_id,),
                ).fetchone()[0]
            )
            now = _now()
            connection.execute(
                """INSERT INTO delivery_attempts(
                    attempt_id, message_id, attempt_number, created_at
                ) VALUES (?, ?, ?, ?)""",
                (attempt_id, message_id, number, now),
            )
            connection.execute(
                """INSERT INTO delivery_status_events(
                    attempt_id, sequence, status, details_json, created_at
                ) VALUES (?, 1, 'attempted', '{}', ?)""",
                (attempt_id, now),
            )
        return attempt_id

    def record_delivery_status(
        self,
        attempt_id: str,
        status: str,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        if status not in {"api_returned", "omp_processed", "failed", "unknown"}:
            raise InvalidTransitionError(f"unsupported delivery status: {status}")
        details_json = _json({} if details is None else details, name="delivery event details")
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM delivery_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone() is None:
                raise InvalidTransitionError(f"unknown delivery attempt: {attempt_id}")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM delivery_status_events WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """INSERT INTO delivery_status_events(
                    attempt_id, sequence, status, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (attempt_id, sequence, status, details_json, _now()),
            )
            return int(cursor.lastrowid)

    def get_delivery_history(self, attempt_id: str) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self._connection.execute(
            """SELECT * FROM delivery_status_events WHERE attempt_id = ? ORDER BY sequence""",
            (attempt_id,),
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "attempt_id": row["attempt_id"],
                "sequence": row["sequence"],
                "status": row["status"],
                "details": _decoded(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_shell_event(
        self, run_id: str, kind: str, details: Mapping[str, Any] | None = None
    ) -> str:
        if kind not in {"sent", "accepted", "started", "ended", "failed"}:
            raise InvalidTransitionError(f"unsupported shell lifecycle event: {kind}")
        details_json = _json({} if details is None else details, name="shell event details")
        event_id = str(uuid.uuid4())
        with self._transaction() as connection:
            self._require_run(connection, run_id)
            connection.execute(
                """INSERT INTO shell_events(event_id, run_id, kind, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, run_id, kind, details_json, _now()),
            )
        return event_id

    def get_shell_history(self, run_id: str) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT * FROM shell_events WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "details": _decoded(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def request_takeover(
        self, run_id: str, details: Mapping[str, Any] | None = None
    ) -> str:
        details_json = _json({} if details is None else details, name="takeover request details")
        event_id = str(uuid.uuid4())
        with self._transaction() as connection:
            self._require_run(connection, run_id)
            connection.execute(
                """INSERT INTO takeover_events(
                    event_id, run_id, request_id, kind, details_json, created_at
                ) VALUES (?, ?, NULL, 'requested', ?, ?)""",
                (event_id, run_id, details_json, _now()),
            )
        return event_id

    def confirm_takeover(
        self, request_id: str, details: Mapping[str, Any] | None = None
    ) -> str:
        details_json = _json({} if details is None else details, name="takeover confirmation details")
        event_id = str(uuid.uuid4())
        with self._transaction() as connection:
            request = connection.execute(
                "SELECT * FROM takeover_events WHERE event_id = ?", (request_id,)
            ).fetchone()
            if request is None or request["kind"] != "requested":
                raise InvalidTransitionError("takeover confirmation requires a request event")
            connection.execute(
                """INSERT INTO takeover_events(
                    event_id, run_id, request_id, kind, details_json, created_at
                ) VALUES (?, ?, ?, 'confirmed', ?, ?)""",
                (event_id, request["run_id"], request_id, details_json, _now()),
            )
        return event_id

    def get_takeover_history(self, run_id: str) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT * FROM takeover_events WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "request_id": row["request_id"],
                "kind": row["kind"],
                "details": _decoded(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _durable_counts(self) -> dict[str, int]:
        """Private, test-only read helper for rollback assertions."""
        self._ensure_open()
        tables = (
            "tasks", "task_specs", "session_settings", "runs", "task_decisions",
            "authorization_consumption", "active_runs", "run_events",
            "invalidated_revisions", "messages", "delivery_attempts",
            "delivery_status_events", "shell_events", "takeover_events",
        )
        return {
            table: int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
