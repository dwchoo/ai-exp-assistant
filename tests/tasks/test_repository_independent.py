"""Independent authorization, durability and cross-link invariant regressions."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from workbench.tasks.repository import (AuthorizationError, InvalidRevisionError,
                                        InvalidTransitionError, RepositoryClosedError,
                                        TaskRepository)


class RepositoryInvariantTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cw09-independent-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.repo = TaskRepository(self.path)
        self.addCleanup(lambda: self.repo.close())
        self.task = self.repo.create_task({"goal": "bounded operation"}, task_id="independent-task")

    def authorize(self, task=None, revision=1, retries=0):
        task = self.task if task is None else task
        self.repo.approve_scope(task, revision, {"paths": ["allowed.py"]}, retry_count_used=retries)
        return self.repo.proceed(task, revision, "proceed", retry_count_used=retries)

    def reopen(self):
        self.repo.close()
        self.repo = TaskRepository(self.path)

    def test_proceed_before_approval_wrong_revision_and_consumed_decision_stay_denied_after_reopen(self):
        before = self.repo._durable_counts()
        with self.assertRaises(AuthorizationError): self.repo.proceed(self.task, 1, "too early")
        self.assertEqual(self.repo._durable_counts(), before)
        self.repo.approve_scope(self.task, 1, {"paths": ["allowed.py"]})
        new_revision = self.repo.revise_task(self.task, {"goal": "next run"})
        with self.assertRaises(AuthorizationError): self.repo.proceed(self.task, new_revision, "scope mismatch")
        with self.assertRaises(InvalidRevisionError): self.repo.start_run(self.task, 99)
        self.repo.proceed(self.task, 1, "valid older approved input")
        run = self.repo.start_run(self.task, 1)
        self.repo.complete_run(run)
        self.reopen()
        with self.assertRaises(AuthorizationError): self.repo.start_run(self.task, 1)
        self.assertEqual([e["kind"] for e in self.repo.get_run_history(run)], ["started", "completed"])

    def test_transaction_denial_at_each_write_and_commit_preserves_unused_authorization(self):
        for boundary in ("runs", "authorization_consumption", "active_runs", "COMMIT"):
            with self.subTest(boundary=boundary):
                task = self.repo.create_task({"goal": boundary})
                proceed = self.authorize(task)
                before = self.repo._durable_counts()
                def deny(action, arg1, _arg2, _db, _trigger):
                    if (action == sqlite3.SQLITE_INSERT and arg1 == boundary) or (
                            action == sqlite3.SQLITE_TRANSACTION and arg1 == boundary):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                self.repo._connection.set_authorizer(deny)
                try:
                    with self.assertRaises(sqlite3.DatabaseError): self.repo.start_run(task, 1)
                finally:
                    self.repo._connection.set_authorizer(None)
                self.assertEqual(self.repo._durable_counts(), before)
                self.assertFalse(self.repo._connection.in_transaction)
                self.reopen()
                self.assertEqual(self.repo.get_decision(proceed)["kind"], "proceed")
                self.assertIsNone(self.repo.get_current_run(task))
                run = self.repo.start_run(task, 1)
                self.assertEqual(self.repo.get_current_run(task)["run_id"], run)
                self.repo.complete_run(run)

    def test_cancel_and_revoke_transaction_failure_does_not_partially_remove_current_run(self):
        self.authorize()
        run = self.repo.start_run(self.task, 1)
        original = self.repo.get_run(run)
        original_history = self.repo.get_run_history(run)
        for action in (lambda: self.repo.cancel_run(run, "stop"),
                       lambda: self.repo.revoke_authority(self.task, 1, "withdraw")):
            def deny(code, table, *_args):
                return sqlite3.SQLITE_DENY if code == sqlite3.SQLITE_DELETE and table == "active_runs" else sqlite3.SQLITE_OK
            self.repo._connection.set_authorizer(deny)
            try:
                with self.assertRaises(sqlite3.DatabaseError): action()
            finally:
                self.repo._connection.set_authorizer(None)
            self.reopen()
            self.assertEqual(self.repo.get_current_run(self.task), original)
            self.assertEqual(self.repo.get_run_history(run), original_history)
        self.repo.cancel_run(run, "confirmed stop")
        self.reopen()
        for action in (lambda: self.repo.approve_scope(self.task, 1, {"paths": ["allowed.py"]}),
                       lambda: self.repo.proceed(self.task, 1, "replay"),
                       lambda: self.repo.start_run(self.task, 1)):
            with self.assertRaises(AuthorizationError): action()

    def test_cross_task_message_decision_and_delivery_attempt_reuse_never_overwrite(self):
        self.authorize()
        run = self.repo.start_run(self.task, 1)
        other = self.repo.create_task({"goal": "other"})
        self.authorize(other)
        other_run = self.repo.start_run(other, 1)
        original = self.repo.get_run(run)
        for action in (lambda: self.repo.create_message(other, 1, run, {"text": "wrong run"}),
                       lambda: self.repo.record_decision(other, 1, run, "retry", retry_count_used=1)):
            with self.assertRaises(InvalidTransitionError): action()
        message = self.repo.create_message(self.task, 1, run, {"text": "logical"}, message_id="stable-id")
        other_message = self.repo.create_message(other, 1, other_run, {"text": "other"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create_message(other, 1, other_run, {"text": "overwrite"}, message_id=message)
        attempt = self.repo.create_delivery_attempt(message, attempt_id="attempt-id")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create_delivery_attempt(other_message, attempt_id=attempt)
        second = self.repo.create_delivery_attempt(message)
        self.repo.record_delivery_status(attempt, "api_returned", {"request_id": "ack"})
        self.assertEqual([e["status"] for e in self.repo.get_delivery_history(second)], ["attempted"])
        self.assertEqual([e["status"] for e in self.repo.get_delivery_history(attempt)], ["attempted", "api_returned"])
        self.assertEqual(self.repo.get_run(run), original)
        self.reopen()
        self.assertEqual(self.repo.get_message(message)["content"], {"text": "logical"})
        self.assertEqual(self.repo._connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_settings_and_revision_change_preserve_active_run_and_prior_retry_history(self):
        self.authorize(retries=2)
        run = self.repo.start_run(self.task, 1)
        initial = self.repo.get_run(run)
        revision = self.repo.revise_task(self.task, {"goal": "updated specification"})
        settings = self.repo.set_session_settings({"max_retries": 7})
        decision = self.repo.record_decision(self.task, 1, run, "retry", retry_count_used=3)
        self.assertEqual(self.repo.get_decision(decision)["settings_revision"], settings)
        self.assertEqual(self.repo.get_current_run(self.task), initial)
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_decision(self.task, revision, run, "continue", retry_count_used=3)
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_decision(self.task, 1, run, "continue", retry_count_used=2)
        self.repo.fail_run(run)
        self.reopen()
        self.assertEqual(self.repo.get_run(run), initial)
        self.assertEqual(self.repo.get_run_history(run)[-1]["retry_count_used"], 3)
        # A distinct normal run can start at zero; that must not reset the
        # existing run's immutable snapshot or its consumed retry history.
        self.repo.approve_scope(self.task, revision, {"paths": ["allowed.py"]}, retry_count_used=3)
        self.repo.proceed(self.task, revision, "distinct normal run", retry_count_used=0)
        next_run = self.repo.start_run(self.task, revision, inputs={"purpose": "normal improvement"})
        self.assertEqual(self.repo.get_run(next_run)["retry_count_used"], 0)
        self.assertEqual(self.repo.get_run(next_run)["settings_revision"], settings)
        self.assertEqual(self.repo.get_run_history(run)[-1]["retry_count_used"], 3)
        self.assertEqual(self.repo.get_decision(decision)["retry_count_used"], 3)

    def test_database_invalidation_cannot_reference_another_tasks_authority_decision(self):
        other = self.repo.create_task({"goal": "other authority"})
        # Use an otherwise-unconsumed decision; a duplicate decision_id would
        # test only UNIQUE, masking the missing Task/revision authority link.
        decision = self.repo.approve_scope(other, 1, {"paths": ["other.py"]})
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo._connection.execute(
                "INSERT INTO invalidated_revisions(task_id,revision,decision_id,reason,created_at) VALUES(?,?,?,?,?)",
                (self.task, 1, decision, "cross-task corruption", "now"))

    def test_insert_or_replace_cannot_overwrite_existing_task_spec_revision(self):
        connection = self.repo._connection
        original = tuple(connection.execute(
            "SELECT task_id,revision,spec_json,created_at FROM task_specs WHERE task_id=? AND revision=1",
            (self.task,)).fetchone())
        rejected = None
        try:
            # Keep the repository's real pragma settings: enabling recursive
            # triggers here would hide a failure in its connection setup.
            connection.execute(
                "INSERT OR REPLACE INTO task_specs(task_id,revision,spec_json,created_at) VALUES(?,?,?,?)",
                (self.task, 1, '{"goal":"unauthorized replacement"}', "replacement-time"))
        except sqlite3.IntegrityError as error:
            rejected = error
        self.reopen()
        retained = tuple(self.repo._connection.execute(
            "SELECT task_id,revision,spec_json,created_at FROM task_specs WHERE task_id=? AND revision=1",
            (self.task,)).fetchone())
        self.assertEqual(retained, original, "INSERT OR REPLACE overwrote immutable TaskSpec revision")
        self.assertIsInstance(rejected, sqlite3.IntegrityError, "replacement must be rejected, not silently ignored")

    def test_v1_migration_rejects_scope_approval_invalidation_atomically(self):
        decision = self.repo.approve_scope(self.task, 1, {"paths": ["allowed.py"]})
        self.repo.close()
        legacy = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(legacy.close)
        # Build the historical v1 invalidation boundary, whose single-column
        # decision FK permitted this same-Task but wrong-kind authority row.
        legacy.executescript("""
            DROP TRIGGER invalidated_revisions_immutable_update;
            DROP TRIGGER invalidated_revisions_immutable_delete;
            DROP TRIGGER invalidated_revisions_requires_authority_decision;
            DROP TABLE invalidated_revisions;
            CREATE TABLE invalidated_revisions (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                decision_id TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (task_id, revision),
                FOREIGN KEY (task_id, revision) REFERENCES task_specs(task_id, revision),
                FOREIGN KEY (decision_id) REFERENCES task_decisions(decision_id)
            );
            PRAGMA user_version = 1;
        """)
        legacy.execute("PRAGMA foreign_keys = ON")
        legacy.execute("INSERT INTO invalidated_revisions VALUES(?,?,?,?,?)",
                       (self.task, 1, decision, "legacy wrong-kind authority", "legacy-time"))
        self.assertEqual(legacy.execute("PRAGMA foreign_key_check").fetchall(), [])
        before_schema = legacy.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        before_rows = {
            table: legacy.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for (table,) in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        legacy.close()
        rejected = None
        try:
            migrated = TaskRepository(self.path)
        except sqlite3.DatabaseError as error:
            rejected = error
        else:
            migrated.close()
        verification = sqlite3.connect(self.path)
        self.addCleanup(verification.close)
        self.assertEqual(verification.execute("PRAGMA user_version").fetchone()[0], 1,
                         "wrong-kind v1 invalidation was accepted and migration committed")
        self.assertEqual(verification.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall(), before_schema)
        for table, rows in before_rows.items():
            with self.subTest(table=table):
                self.assertEqual(verification.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall(), rows)
        self.assertIsInstance(rejected, sqlite3.DatabaseError, "invalid legacy authority must fail migration")

    def test_takeover_confirmation_and_shell_history_are_append_only_and_reopen_in_order(self):
        self.authorize()
        run = self.repo.start_run(self.task, 1)
        original = self.repo.get_run(run)
        for kind in ("sent", "accepted", "started", "ended"):
            self.repo.record_shell_event(run, kind, {"command_id": "same-command"})
        request = self.repo.request_takeover(run, {"owner_epoch": 2})
        confirmed = self.repo.confirm_takeover(request, {"owner_epoch": 3})
        history = self.repo.get_takeover_history(run)
        with self.assertRaises(sqlite3.IntegrityError): self.repo.confirm_takeover(request)
        with self.assertRaises(InvalidTransitionError): self.repo.confirm_takeover(confirmed)
        self.reopen()
        self.assertEqual(self.repo.get_takeover_history(run), history)
        self.assertEqual([e["kind"] for e in self.repo.get_shell_history(run)], ["sent", "accepted", "started", "ended"])
        self.assertEqual(self.repo.get_current_run(self.task), original)

    def test_all_persisted_fact_tables_reject_update_and_delete_not_only_run_events(self):
        self.authorize()
        run = self.repo.start_run(self.task, 1)
        message = self.repo.create_message(self.task, 1, run, {"text": "immutable"})
        attempt = self.repo.create_delivery_attempt(message)
        self.repo.record_delivery_status(attempt, "api_returned")
        self.repo.record_shell_event(run, "sent")
        request = self.repo.request_takeover(run)
        self.repo.confirm_takeover(request)
        self.repo.cancel_run(run, "retain all facts")
        tables = ("tasks", "task_specs", "session_settings", "runs", "task_decisions",
                  "authorization_consumption", "run_events", "invalidated_revisions", "messages",
                  "delivery_attempts", "delivery_status_events", "shell_events", "takeover_events")
        for table in tables:
            with self.subTest(table=table):
                self.assertGreater(self.repo._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
                for query in (f"UPDATE {table} SET rowid=rowid", f"DELETE FROM {table}"):
                    with self.assertRaises(sqlite3.IntegrityError): self.repo._connection.execute(query)
        self.reopen()
        self.assertEqual(self.repo.get_message(message)["content"], {"text": "immutable"})
        self.assertEqual(self.repo._connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_closed_reads_and_writes_never_return_success_or_create_new_history(self):
        self.authorize()
        run = self.repo.start_run(self.task, 1)
        message = self.repo.create_message(self.task, 1, run, {"text": "pending"})
        attempt = self.repo.create_delivery_attempt(message)
        self.repo.close()
        for action in (lambda: self.repo.start_run(self.task, 1), lambda: self.repo.get_run(run),
                       lambda: self.repo.set_session_settings({"new": True}),
                       lambda: self.repo.record_delivery_status(attempt, "omp_processed"),
                       lambda: self.repo.record_shell_event(run, "started"),
                       lambda: self.repo.request_takeover(run), lambda: self.repo.complete_run(run)):
            with self.assertRaises(RepositoryClosedError): action()
        self.reopen()
        self.assertEqual([e["status"] for e in self.repo.get_delivery_history(attempt)], ["attempted"])
        self.assertEqual(self.repo.get_run_history(run)[-1]["kind"], "started")


if __name__ == "__main__": unittest.main()
