from pathlib import Path
import sqlite3
import tempfile
import unittest

from workbench.tasks.repository import (
    ActiveRunError,
    AuthorizationError,
    InvalidRevisionError,
    InvalidTransitionError,
    RepositoryClosedError,
    TaskRepository,
)


class TaskRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "metadata.sqlite3"
        self.repository = TaskRepository(self.database)

    def tearDown(self):
        self.repository.close()
        self.temp_dir.cleanup()

    def make_task(self, *, task_id="task-1"):
        created_id = self.repository.create_task(
            {"goal": "inspect", "allowed_changes": ["src/example.py"]},
            task_id=task_id,
        )
        return created_id

    def authorize(self, task_id="task-1", revision=1, *, retry_count_used=0):
        self.repository.approve_scope(
            task_id, revision, {"paths": ["src/example.py"], "commands": []}
        )
        return self.repository.proceed(
            task_id,
            revision,
            "run approved task",
            retry_count_used=retry_count_used,
        )

    def test_task_identity_spec_revisions_and_run_snapshot_survive_reopen(self):
        task_id = self.make_task()
        old_spec = self.repository.get_task_spec(task_id, 1)
        old_proceed = self.authorize(task_id)
        run_id = self.repository.start_run(task_id, 1)

        revision = self.repository.revise_task(
            task_id,
            {"goal": "inspect more", "allowed_changes": ["src/example.py", "tests/"]},
        )
        self.assertEqual(revision, 2)
        self.assertEqual(self.repository.get_task_spec(task_id, 1), old_spec)
        self.assertEqual(self.repository.get_run(run_id)["revision"], 1)
        self.assertEqual(self.repository.get_run(run_id)["task_id"], task_id)
        self.assertEqual(self.repository.get_task_spec(task_id, 2)["spec"]["goal"], "inspect more")
        self.assertEqual(self.repository.get_run(run_id)["retry_count_used"], 0)

        self.repository.close()
        self.repository = TaskRepository(self.database)
        self.assertEqual(self.repository.get_task_spec(task_id, 1), old_spec)
        self.assertEqual(self.repository.get_run(run_id)["task_id"], task_id)
        self.assertEqual(self.repository.get_current_run(task_id)["run_id"], run_id)
        self.assertEqual(self.repository.get_decision(old_proceed)["kind"], "proceed")
        self.repository.complete_run(run_id, {"status": "done"})
        self.repository.approve_scope(task_id, revision, {"paths": ["src/", "tests/"]})
        self.repository.proceed(task_id, revision, "use revised specification")
        later_run = self.repository.start_run(task_id, revision)
        self.assertEqual(self.repository.get_run(later_run)["task_id"], task_id)
        self.assertEqual(self.repository.get_run(later_run)["revision"], revision)

    def test_both_scope_approval_and_fresh_proceed_are_required(self):
        task_id = self.make_task()
        with self.assertRaises(AuthorizationError):
            self.repository.start_run(task_id, 1)

        self.repository.approve_scope(task_id, 1, {"paths": ["src/"]})
        with self.assertRaises(AuthorizationError):
            self.repository.start_run(task_id, 1)

        proceed_id = self.repository.proceed(task_id, 1, "continue")
        run_id = self.repository.start_run(task_id, 1)
        self.assertEqual(self.repository.get_run(run_id)["revision"], 1)
        self.assertEqual(self.repository.get_decision(proceed_id)["kind"], "proceed")
        self.repository.complete_run(run_id, {"status": "done"})
        with self.assertRaises(AuthorizationError):
            self.repository.start_run(task_id, 1)

    def test_cancel_and_revoke_are_append_only_and_invalidate_the_revision(self):
        task_id = self.make_task()
        self.authorize(task_id)
        run_id = self.repository.start_run(task_id, 1)
        self.repository.cancel_run(run_id, "user stopped")
        self.assertIsNone(self.repository.get_current_run(task_id))
        self.assertEqual(
            [event["kind"] for event in self.repository.get_run_history(run_id)],
            ["started", "cancelled"],
        )
        self.assertEqual(self.repository.get_decisions(task_id, 1)[-1]["kind"], "cancelled")
        with self.assertRaises(AuthorizationError):
            self.repository.proceed(task_id, 1, "replay old revision")
        with self.assertRaises(AuthorizationError):
            self.repository.start_run(task_id, 1)

        revision = self.repository.revise_task(task_id, {"goal": "new scope"})
        self.repository.approve_scope(task_id, revision, {"paths": ["src/"]})
        self.repository.proceed(task_id, revision, "new approved task")
        new_run = self.repository.start_run(task_id, revision)
        self.repository.revoke_authority(task_id, revision, "scope withdrawn")
        self.assertIsNone(self.repository.get_current_run(task_id))
        self.assertEqual(
            self.repository.get_run_history(new_run)[-1]["kind"], "revoked"
        )
        with self.assertRaises(AuthorizationError):
            self.repository.start_run(task_id, revision)

    def test_active_run_is_not_replaced_by_spec_or_settings_change(self):
        task_id = self.make_task()
        self.authorize(task_id, retry_count_used=2)
        run_id = self.repository.start_run(task_id, 1)
        initial = self.repository.get_run(run_id)

        settings_revision = self.repository.set_session_settings(
            {"max_retries": 5, "parallelism": 2}
        )
        self.assertEqual(settings_revision, 2)
        self.assertEqual(self.repository.get_current_run(task_id)["run_id"], run_id)
        self.assertEqual(self.repository.get_run(run_id)["settings_revision"], 1)
        self.assertEqual(self.repository.get_run(run_id)["retry_count_used"], 2)
        with self.assertRaises(ActiveRunError):
            self.repository.start_run(task_id, 1)

        decision_id = self.repository.record_decision(
            task_id,
            1,
            run_id,
            "retry",
            retry_count_used=3,
            details={"reason": "transient"},
        )
        decision = self.repository.get_decision(decision_id)
        self.assertEqual(decision["settings_revision"], settings_revision)
        self.assertEqual(decision["retry_count_used"], 3)
        self.assertEqual(self.repository.get_run(run_id), initial)
        with self.assertRaises(InvalidTransitionError):
            self.repository.record_decision(
                task_id,
                1,
                run_id,
                "continue",
                retry_count_used=2,
                details={"reason": "must not reset"},
            )

    def test_new_run_uses_new_settings_and_failed_write_rolls_back_all_run_state(self):
        task_id = self.make_task()
        self.repository.approve_scope(task_id, 1, {"paths": ["src/"]})
        first_proceed = self.repository.proceed(task_id, 1, "first run")
        prior_run = self.repository.start_run(task_id, 1)
        self.repository.complete_run(prior_run, {"status": "done"})
        proceed_id = self.repository.proceed(task_id, 1, "run after completed task", retry_count_used=1)
        settings_revision = self.repository.set_session_settings({"max_retries": 7})

        self.repository._connection.execute(
            """CREATE TRIGGER fail_started_event BEFORE INSERT ON run_events
               WHEN NEW.kind = 'started'
               BEGIN SELECT RAISE(ABORT, 'injected metadata write failure'); END"""
        )
        before = self.repository._durable_counts()
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.start_run(task_id, 1)
        self.assertEqual(self.repository._durable_counts(), before)
        self.assertIsNone(self.repository.get_current_run(task_id))
        self.assertEqual(
            [event["kind"] for event in self.repository.get_run_history(prior_run)],
            ["started", "completed"],
        )
        self.assertEqual(self.repository.get_decision(first_proceed)["kind"], "proceed")

        self.repository._connection.execute("DROP TRIGGER fail_started_event")
        run_id = self.repository.start_run(task_id, 1)
        run = self.repository.get_run(run_id)
        self.assertEqual(run["settings_revision"], settings_revision)
        self.assertEqual(run["retry_count_used"], 1)
        self.assertEqual(self.repository.get_decision(proceed_id)["kind"], "proceed")

    def test_settings_revisions_and_run_decisions_are_durable(self):
        task_id = self.make_task()
        self.authorize(task_id)
        run_id = self.repository.start_run(task_id, 1)
        self.repository.record_decision(
            task_id, 1, run_id, "continue", retry_count_used=0, details={"step": 1}
        )
        self.repository.set_session_settings({"max_retries": 4})
        next_decision_id = self.repository.record_decision(
            task_id,
            1,
            run_id,
            "retry",
            retry_count_used=1,
            details={"step": 2},
        )

        self.repository.close()
        self.repository = TaskRepository(self.database)
        decision = self.repository.get_decision(next_decision_id)
        self.assertEqual(decision["settings_revision"], 2)
        self.assertEqual(decision["retry_count_used"], 1)
        self.assertEqual(self.repository.get_current_run(task_id)["run_id"], run_id)

    def test_message_delivery_attempts_shell_and_takeover_are_linked_append_only(self):
        task_id = self.make_task()
        self.authorize(task_id)
        run_id = self.repository.start_run(task_id, 1)
        message_id = self.repository.create_message(
            task_id,
            1,
            run_id,
            {"role": "assistant", "text": "hello"},
            message_id="stable-message-1",
        )
        attempt_id = self.repository.create_delivery_attempt(message_id)
        self.repository.record_delivery_status(attempt_id, "api_returned", {"request_id": "r1"})
        statuses = self.repository.get_delivery_history(attempt_id)
        self.assertEqual([event["status"] for event in statuses], ["attempted", "api_returned"])
        self.assertNotIn("omp_processed", [event["status"] for event in statuses])
        self.repository.record_delivery_status(attempt_id, "omp_processed", {"session_id": "s1"})
        self.assertEqual(
            [event["status"] for event in self.repository.get_delivery_history(attempt_id)],
            ["attempted", "api_returned", "omp_processed"],
        )

        self.repository.record_shell_event(run_id, "sent", {"operation": "run"})
        self.repository.record_shell_event(run_id, "accepted", {"job_id": "j1"})
        request_id = self.repository.request_takeover(run_id, {"reason": "inspect"})
        confirmation_id = self.repository.confirm_takeover(request_id, {"owner": "user"})
        history = self.repository.get_takeover_history(run_id)
        self.assertEqual([event["kind"] for event in history], ["requested", "confirmed"])
        self.assertEqual(history[1]["event_id"], confirmation_id)
        self.assertEqual(history[1]["request_id"], request_id)

        self.repository.close()
        self.repository = TaskRepository(self.database)
        saved_message = self.repository.get_message(message_id)
        self.assertEqual(saved_message["task_id"], task_id)
        self.assertEqual(saved_message["run_id"], run_id)
        self.assertEqual(saved_message["content"]["text"], "hello")
        self.assertEqual(self.repository.get_shell_history(run_id)[1]["kind"], "accepted")
        self.assertEqual(len(self.repository.get_delivery_history(attempt_id)), 3)
        self.assertEqual(self.repository._connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_revision_and_lifecycle_history_rows_reject_mutation(self):
        task_id = self.make_task()
        self.authorize(task_id)
        run_id = self.repository.start_run(task_id, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(
                "UPDATE task_specs SET spec_json = '{}' WHERE task_id = ? AND revision = 1",
                (task_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(
                "DELETE FROM run_events WHERE run_id = ?", (run_id,)
            )

    def test_invalidation_requires_its_own_task_revision_and_a_terminal_authority_decision(self):
        task_id = self.make_task()
        other_task = self.repository.create_task({"goal": "different authority"}, task_id="task-2")
        # A valid invalidation decision from another Task keeps the kind guard
        # from masking the composite Task/revision foreign-key invariant.
        other_decision = "cross-task-revocation"
        self.repository._connection.execute(
            """INSERT INTO task_decisions(
                decision_id, task_id, revision, run_id, settings_revision,
                retry_count_used, kind, details_json, created_at
            ) VALUES (?, ?, 1, NULL, 1, 0, 'revoked', '{}', 'now')""",
            (other_decision, other_task),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(
                """INSERT INTO invalidated_revisions(
                    task_id, revision, decision_id, reason, created_at
                ) VALUES (?, 1, ?, 'cross-task', 'now')""",
                (task_id, other_decision),
            )

        self.authorize(task_id)
        run_id = self.repository.start_run(task_id, 1)
        ordinary_decision = self.repository.record_decision(
            task_id, 1, run_id, "continue", retry_count_used=0
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(
                """INSERT INTO invalidated_revisions(
                    task_id, revision, decision_id, reason, created_at
                ) VALUES (?, 1, ?, 'wrong decision kind', 'now')""",
                (task_id, ordinary_decision),
            )
        self.assertIsNone(self.repository._connection.execute(
            "SELECT 1 FROM invalidated_revisions WHERE task_id = ?", (task_id,)
        ).fetchone())

    def test_schema_v1_migration_preserves_cancellation_invalidation_history(self):
        task_id = self.make_task()
        self.authorize(task_id)
        run_id = self.repository.start_run(task_id, 1)
        self.repository.cancel_run(run_id, "migrate existing record")
        prior_history = self.repository.get_run_history(run_id)
        self.repository.close()

        legacy = sqlite3.connect(self.database, isolation_level=None)
        legacy.execute("PRAGMA foreign_keys = OFF")
        legacy.execute("DROP TRIGGER invalidated_revisions_immutable_update")
        legacy.execute("DROP TRIGGER invalidated_revisions_immutable_delete")
        legacy.execute("DROP TRIGGER invalidated_revisions_requires_authority_decision")
        legacy.execute("ALTER TABLE invalidated_revisions RENAME TO invalidated_revisions_v2_test")
        legacy.execute(
            """CREATE TABLE invalidated_revisions (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                decision_id TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (task_id, revision),
                FOREIGN KEY (task_id, revision) REFERENCES task_specs(task_id, revision),
                FOREIGN KEY (decision_id) REFERENCES task_decisions(decision_id)
            )"""
        )
        legacy.execute(
            """INSERT INTO invalidated_revisions(task_id, revision, decision_id, reason, created_at)
               SELECT task_id, revision, decision_id, reason, created_at
               FROM invalidated_revisions_v2_test"""
        )
        legacy.execute("DROP TABLE invalidated_revisions_v2_test")
        legacy.execute("PRAGMA user_version = 1")
        legacy.close()

        self.repository = TaskRepository(self.database)
        self.assertEqual(self.repository._connection.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.repository.get_run_history(run_id), prior_history)
        self.assertIsNone(self.repository.get_current_run(task_id))
        with self.assertRaises(AuthorizationError):
            self.repository.start_run(task_id, 1)

    def test_invalid_references_and_closed_repository_fail_explicitly(self):
        task_id = self.make_task()
        with self.assertRaises(InvalidRevisionError):
            self.repository.approve_scope(task_id, 3, {"paths": []})
        with self.assertRaises(InvalidRevisionError):
            self.repository.get_task_spec(task_id, True)
        with self.assertRaises(InvalidTransitionError):
            self.repository.approve_scope(task_id, 1, {})
        with self.assertRaises(InvalidTransitionError):
            self.repository.proceed(task_id, 1, "continue", retry_count_used=True)

        self.repository.close()
        with self.assertRaises(RepositoryClosedError):
            self.repository.get_task_spec(task_id)


if __name__ == "__main__":
    unittest.main()
