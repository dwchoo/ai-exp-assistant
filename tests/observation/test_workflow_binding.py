"""Persisted TASK identity and same-WorkflowRun exit binding checks."""
from __future__ import annotations

from copy import copy, deepcopy
import gc
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4, uuid5
from weakref import ref

from workbench.observation.worker_review import SerializedReviewAdmission, WorkerReviewScheduler
from workbench.observation.workflow_binding import WorkflowObservationBinding
from workbench.workflow.run import WorkflowRun


AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True,
}}


class Repository:
    def __init__(self, message):
        self.message = message
        self.current_peer = ("new-peer", 99)

    def get_message(self, _message_id):
        return deepcopy(self.message)


def fixture():
    task_id, run_id, message_id, session_id = (str(uuid4()) for _ in range(4))
    revision_id = str(uuid5(UUID(task_id), "task-spec-revision:1"))
    content = {"task_id": task_id, "revision": 1, "run_id": run_id,
               "revision_id": revision_id, "sender_role": "manager",
               "target_role": "worker", "kind": "task",
               "target_session_id": session_id, "target_session_generation": 3}
    message = {"message_id": message_id, "task_id": task_id, "revision": 1,
               "run_id": run_id, "content": content}
    workflow = object.__new__(WorkflowRun)
    workflow.repository = Repository(message)
    workflow.task_id, workflow.revision, workflow.run_id = task_id, 1, run_id
    workflow.task_message_id = message_id
    workflow._record = {"task_id": task_id, "revision": 1, "run_id": run_id,
                        "shell_state": "exited", "exit_confirmed": True, "exit_status": 0}
    workflow._terminal = True
    workflow._observed = {"sent", "ended"}
    lifecycle = {"control_returned": True, "input_returned": True, "lifetime": "ended"}
    workflow.shell = SimpleNamespace(snapshot=lambda: {"lifecycle": lifecycle})
    workflow.collect = lambda **_kwargs: dict(workflow._record)
    return workflow, lifecycle


def scheduler_for(run, owner=None):
    exits = []
    scheduler = WorkerReviewScheduler(
        admission=owner or SerializedReviewAdmission(None, AUTOMATION),
        automation_state=lambda: AUTOMATION, active_run=lambda: run,
        worker_state=lambda _: None, collect_non_model=lambda _: {},
        dispatch_review=lambda _: {"status": "omp_processed"},
        user_priority=lambda _: False, on_exit=lambda event: exits.append(dict(event)),
        clock=lambda: 0,
    )
    return scheduler, exits


class WorkflowBindingTests(unittest.TestCase):
    def test_attach_rejects_a_task_message_id_that_resolves_to_another_row(self):
        workflow, _ = fixture()
        workflow.task_message_id = str(uuid4())
        with self.assertRaises(ValueError):
            WorkflowObservationBinding.attach(workflow)

    def test_binding_cannot_notify_a_scheduler_owned_by_another_workflow_run(self):
        first, _ = fixture()
        second, _ = fixture()
        binding = WorkflowObservationBinding.attach(first)
        other_binding = WorkflowObservationBinding.attach(second)
        scheduler, exits = scheduler_for(other_binding.run)
        source = scheduler.activate_run_source(other_binding.run, second)
        self.assertIsNotNone(source)
        self.assertFalse(binding.bind(scheduler, source))
        with self.assertRaises(RuntimeError):
            binding.collect_and_notify(scheduler)
        self.assertEqual(exits, [])

    def test_persisted_task_freezes_old_session_after_peer_replacement(self):
        workflow, _ = fixture()
        binding = WorkflowObservationBinding.attach(workflow)
        original = binding.run.identity
        workflow.repository.current_peer = ("replacement", 100)
        self.assertEqual(binding.run.identity, original)
        scheduler, exits = scheduler_for(binding.run)
        source = scheduler.activate_run_source(binding.run, workflow)
        self.assertIsNotNone(source)
        self.assertTrue(binding.bind(scheduler, source))
        record, notified = binding.collect_and_notify(scheduler)
        self.assertTrue(notified)
        self.assertEqual(record["run_id"], original[3])
        self.assertEqual(exits[0]["session_id"], original[4])
        self.assertEqual(exits[0]["session_generation"], original[5])
        self.assertFalse(binding.collect_and_notify(scheduler)[1])

    def test_old_workflow_run_cannot_rebind_to_same_identity_new_incarnation(self):
        workflow, _ = fixture()
        binding = WorkflowObservationBinding.attach(workflow)
        scheduler, exits = scheduler_for(binding.run)
        source = scheduler.activate_run_source(binding.run, workflow)
        self.assertIsNotNone(source)
        self.assertTrue(binding.bind(scheduler, source))
        owner = scheduler.admission
        owner.set_active_run(None)
        owner.set_active_run(binding.run)
        self.assertFalse(binding.bind(scheduler, source))  # still holds the original token
        self.assertFalse(binding.collect_and_notify(scheduler)[1])
        self.assertEqual(exits, [])
        self.assertFalse(WorkflowObservationBinding.attach(workflow).bind(scheduler, source))
        self.assertIsNotNone(owner.issue_review(binding.run, 60.0, 0, {}))

    def test_source_activation_rejects_foreign_and_retired_workflow_run(self):
        workflow, _ = fixture()
        foreign, _ = fixture()
        run = WorkflowObservationBinding.resolve_run(workflow)
        scheduler, _ = scheduler_for(run)
        self.assertIsNone(scheduler.activate_run_source(run, foreign))
        self.assertEqual(scheduler.admission.run_incarnation(), 0)
        old = scheduler.activate_run_source(run, workflow)
        self.assertIsNotNone(old)
        self.assertIs(scheduler.activate_run_source(run, workflow), old)
        self.assertIsNone(scheduler.activate_run_source(run, copy(workflow)))
        scheduler.admission.set_active_run(None)
        self.assertIsNone(scheduler.activate_run_source(run, workflow))
        self.assertEqual(scheduler.admission.run_incarnation(), 2)

    def test_delayed_first_bind_cannot_use_old_or_foreign_token_after_replacement(self):
        workflow, _ = fixture()
        old_binding = WorkflowObservationBinding.attach(workflow)
        scheduler, exits = scheduler_for(old_binding.run)
        old = scheduler.activate_run_source(old_binding.run, workflow)
        self.assertIsNotNone(old)
        scheduler.admission.set_active_run(None)
        replacement = copy(workflow)
        fresh = scheduler.activate_run_source(old_binding.run, replacement)
        self.assertIsNotNone(fresh)
        self.assertNotEqual(old._run_incarnation, fresh._run_incarnation)
        self.assertFalse(old_binding.bind(scheduler, old))
        self.assertFalse(old_binding.bind(scheduler, fresh))
        with self.assertRaises(RuntimeError):
            old_binding.collect_and_notify(scheduler)
        new_binding = WorkflowObservationBinding.attach(replacement)
        self.assertTrue(new_binding.bind(scheduler, fresh))
        self.assertTrue(new_binding.collect_and_notify(scheduler)[1])
        self.assertEqual(exits[0]["run_incarnation"], fresh._run_incarnation)

    def test_1200_external_source_churn_retains_only_live_weak_registrations(self):
        workflow, _ = fixture()
        run = WorkflowObservationBinding.resolve_run(workflow)
        owner = SerializedReviewAdmission(None, AUTOMATION)
        scheduler, _ = scheduler_for(run, owner)
        retired_binding = None
        retired_source = None
        retired_token = None
        for ordinal in range(1200):
            source = copy(workflow)
            token = scheduler.activate_run_source(run, source)
            self.assertIsNotNone(token)
            self.assertEqual(len(owner._exit_sources), 1)
            self.assertLessEqual(len(owner._external_exit_sources), 2)
            if ordinal == 0:
                retired_source = source
                retired_token = token
                retired_binding = WorkflowObservationBinding.attach(source)
                self.assertTrue(retired_binding.bind(scheduler, token))
            owner.set_active_run(None)
            self.assertEqual(len(owner._exit_sources), 0)
            del source
        gc.collect()
        self.assertEqual(len(owner._external_exit_sources), 1)
        self.assertFalse(retired_binding.bind(scheduler, retired_token))
        self.assertIsNone(scheduler.activate_run_source(run, retired_source))
        old_ref = ref(retired_source)
        del retired_binding, retired_source
        gc.collect()
        self.assertIsNone(old_ref())
        self.assertEqual(len(owner._external_exit_sources), 0)

    def test_active_external_source_is_released_on_retirement(self):
        workflow, _ = fixture()
        run = WorkflowObservationBinding.resolve_run(workflow)
        owner = SerializedReviewAdmission(None, AUTOMATION)
        source = copy(workflow)
        source_ref = ref(source)
        self.assertIsNotNone(owner.activate_run_source(run, source))
        del source
        gc.collect()
        self.assertIsNotNone(source_ref())
        self.assertEqual(len(owner._external_exit_sources), 1)
        owner.set_active_run(None)
        gc.collect()
        self.assertIsNone(source_ref())
        self.assertEqual(len(owner._external_exit_sources), 0)

    def test_attach_rejects_wrong_row_content_role_kind_generation_and_revision(self):
        changes = (
            ("row task", lambda row: row.__setitem__("task_id", "wrong")),
            ("row run", lambda row: row.__setitem__("run_id", "wrong")),
            ("row revision", lambda row: row.__setitem__("revision", True)),
            ("content task", lambda row: row["content"].__setitem__("task_id", "wrong")),
            ("content run", lambda row: row["content"].__setitem__("run_id", "wrong")),
            ("content revision", lambda row: row["content"].__setitem__("revision", True)),
            ("revision ID", lambda row: row["content"].__setitem__("revision_id", str(uuid4()))),
            ("sender", lambda row: row["content"].__setitem__("sender_role", "worker")),
            ("target", lambda row: row["content"].__setitem__("target_role", "manager")),
            ("kind", lambda row: row["content"].__setitem__("kind", "question")),
            ("session", lambda row: row["content"].__setitem__("target_session_id", None)),
            ("generation", lambda row: row["content"].__setitem__("target_session_generation", True)),
        )
        for name, mutate in changes:
            with self.subTest(name=name):
                workflow, _ = fixture()
                mutate(workflow.repository.message)
                with self.assertRaises(ValueError):
                    WorkflowObservationBinding.attach(workflow)

    def test_unconfirmed_or_incomplete_or_different_run_never_notifies(self):
        for name, mutate in (
            ("unconfirmed", lambda workflow, life: workflow._record.__setitem__("exit_confirmed", False)),
            ("wrong run", lambda workflow, life: workflow._record.__setitem__("run_id", str(uuid4()))),
            ("different record", lambda workflow, life: setattr(
                workflow, "collect", lambda **_kwargs: {**workflow._record, "exit_status": 9})),
            ("not terminal", lambda workflow, life: setattr(workflow, "_terminal", False)),
            ("not observed", lambda workflow, life: workflow._observed.discard("ended")),
            ("control", lambda workflow, life: life.__setitem__("control_returned", False)),
            ("input", lambda workflow, life: life.__setitem__("input_returned", False)),
            ("lifetime", lambda workflow, life: life.__setitem__("lifetime", "running")),
        ):
            with self.subTest(name=name):
                workflow, lifecycle = fixture()
                binding = WorkflowObservationBinding.attach(workflow)
                scheduler, exits = scheduler_for(binding.run)
                source = scheduler.activate_run_source(binding.run, workflow)
                self.assertIsNotNone(source)
                self.assertTrue(binding.bind(scheduler, source))
                mutate(workflow, lifecycle)
                self.assertFalse(binding.collect_and_notify(scheduler)[1])
                self.assertEqual(exits, [])
