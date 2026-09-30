"""Independent adversarial checks for incarnation-bound exit sources."""
from __future__ import annotations

from copy import copy
from threading import Event, Thread
import unittest

from workbench.observation.worker_review import (
    ActiveRunRef, ExitSourceToken, SerializedReviewAdmission,
    WorkerReviewScheduler,
)
from workbench.observation.workflow_binding import WorkflowObservationBinding
from tests.observation.test_workflow_binding import fixture


RUN = ActiveRunRef("task", "revision", 1, "run", "session", 1)
AUTHORITY = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True,
    "approvalValid": True,
}}
EXIT = {"task_id": RUN.task_id, "revision_id": RUN.revision_id,
        "revision": RUN.revision, "run_id": RUN.run_id,
        "session_id": RUN.session_id,
        "session_generation": RUN.session_generation,
        "exit_confirmed": True, "exit_status": 0}


def scheduler_for(owner, now, delivered, exits, on_exit=None):
    return WorkerReviewScheduler(
        admission=owner, automation_state=lambda: AUTHORITY,
        active_run=lambda: RUN,
        worker_state=lambda _: {"role": "worker", "sessionId": RUN.session_id,
                                "generation": RUN.session_generation, "idle": True,
                                "pending": False, "approvalPending": False,
                                "editorKnown": True, "editorEmpty": True,
                                "inFlightToolCount": 0, "paused": False},
        collect_non_model=lambda _: {"phase": "running"},
        dispatch_review=lambda request: (delivered.append(request),
                                         {"status": "omp_processed"})[1],
        user_priority=lambda _: False,
        on_exit=on_exit or (lambda event: exits.append(dict(event))),
        clock=lambda: now[0],
    )


class IndependentExitIncarnationTests(unittest.TestCase):
    def test_initial_and_transition_incarnations_are_positive_and_monotonic(self):
        empty = SerializedReviewAdmission(None, AUTHORITY)
        active = SerializedReviewAdmission(RUN, AUTHORITY)
        self.assertEqual(empty.run_incarnation(), 0)
        self.assertEqual(active.run_incarnation(), 1)
        first = active.bind_exit_source(RUN)
        self.assertEqual(first._run_incarnation, 1)
        epoch = active.snapshot()
        self.assertEqual(active.set_active_run(RUN), epoch)
        self.assertEqual(active.run_incarnation(), 1)
        active.set_active_run(None)
        active.set_active_run(RUN)
        self.assertEqual(active.run_incarnation(), 3)
        self.assertFalse(active.fence_exit(first))
        current = active.bind_exit_source(RUN)
        self.assertEqual(current._run_incarnation, 3)

        workflow, _ = fixture()
        run = WorkflowObservationBinding.resolve_run(workflow)
        activated = empty.activate_run_source(run, workflow)
        self.assertEqual(activated._run_incarnation, 1)
        self.assertEqual(empty.run_incarnation(), 1)
        self.assertIs(empty.activate_run_source(run, workflow), activated)
        self.assertEqual(empty.run_incarnation(), 1)

    def test_delayed_first_bind_requires_original_activation_token(self):
        workflow, _ = fixture()
        old_binding = WorkflowObservationBinding.attach(workflow)
        owner = SerializedReviewAdmission(None, AUTHORITY)
        public_events = []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: AUTHORITY,
            active_run=lambda: old_binding.run, worker_state=lambda _: None,
            collect_non_model=lambda _: {},
            dispatch_review=lambda _: {"status": "omp_processed"},
            user_priority=lambda _: False,
            on_exit=lambda event: public_events.append(dict(event)), clock=lambda: 0,
        )
        self.assertFalse(old_binding.bind(scheduler, None))
        with self.assertRaises(TypeError):
            old_binding.bind(scheduler)
        self.assertEqual(owner.run_incarnation(), 0)
        old_token = scheduler.activate_run_source(old_binding.run, workflow)
        self.assertIsNotNone(old_token)
        self.assertIsNone(scheduler.activate_run_source(old_binding.run, copy(workflow)))

        owner.set_active_run(None)
        replacement = copy(workflow)
        fresh_token = scheduler.activate_run_source(old_binding.run, replacement)
        self.assertIsNotNone(fresh_token)
        self.assertIsNone(scheduler.activate_run_source(old_binding.run, workflow))
        self.assertFalse(old_binding.bind(scheduler, old_token))
        self.assertFalse(old_binding.bind(scheduler, fresh_token))
        with self.assertRaises(RuntimeError):
            old_binding.collect_and_notify(scheduler)

        foreign_owner = SerializedReviewAdmission(None, AUTHORITY)
        foreign_token = foreign_owner.activate_run_source(old_binding.run, replacement)
        new_binding = WorkflowObservationBinding.attach(replacement)
        self.assertFalse(new_binding.bind(scheduler, old_token))
        self.assertFalse(new_binding.bind(scheduler, foreign_token))
        self.assertTrue(new_binding.bind(scheduler, fresh_token))
        self.assertTrue(new_binding.bind(scheduler, fresh_token))
        self.assertFalse(new_binding.bind(scheduler, foreign_token))
        self.assertTrue(new_binding.collect_and_notify(scheduler)[1])
        self.assertEqual(public_events[0]["run_incarnation"],
                         fresh_token._run_incarnation)
        self.assertGreater(public_events[0]["run_incarnation"], 0)

    def test_forged_foreign_and_rebound_sources_never_fence_new_incarnation(self):
        workflow, _ = fixture()
        run = WorkflowObservationBinding.resolve_run(workflow)
        owner = SerializedReviewAdmission(None, AUTHORITY)
        other = SerializedReviewAdmission(None, AUTHORITY)
        old = owner.activate_run_source(run, workflow)
        self.assertIsNotNone(old)
        self.assertIs(owner.activate_run_source(run, workflow), old)
        epoch, incarnation = owner.snapshot(), owner.run_incarnation()
        self.assertEqual(owner.set_active_run(run), epoch)
        self.assertEqual(owner.run_incarnation(), incarnation)
        self.assertTrue(owner.exit_source_current(old))
        forged = ExitSourceToken(old.run, old._nonce, old._run_incarnation)
        for target, token in ((owner, forged), (other, old)):
            self.assertFalse(target.fence_exit(token))
        self.assertFalse(owner.fence_exit("run"))
        self.assertTrue(owner.exit_source_current(old))

        owner.set_active_run(None)
        replacement = copy(workflow)
        fresh = owner.activate_run_source(run, replacement)
        self.assertFalse(owner.exit_source_current(old))
        self.assertIsNone(owner.activate_run_source(run, workflow))
        self.assertFalse(owner.fence_exit(old))
        self.assertIsNotNone(fresh)
        self.assertIsNot(fresh, old)
        self.assertTrue(owner.fence_exit(fresh))
        self.assertIsNone(owner.issue_review(run, 60.0, 0, {}))

    def test_old_successful_ui_callback_cannot_clear_new_due_review(self):
        owner = SerializedReviewAdmission(RUN, AUTHORITY)
        now = [0.0]
        delivered, exits = [], []
        entered, release = Event(), Event()
        nested, outcomes = [], []
        scheduler = None

        def on_exit(event):
            nested.append(scheduler.notify_exit_event(EXIT, source=old, run=RUN))
            entered.set()
            self.assertTrue(release.wait(2))
            exits.append(dict(event))

        scheduler = scheduler_for(owner, now, delivered, exits, on_exit=on_exit)
        self.assertEqual(scheduler.tick().status, "waiting")
        old = scheduler.bind_exit_source(RUN)
        first = Thread(target=lambda: outcomes.append(
            scheduler.notify_exit_event(EXIT, source=old, run=RUN)))
        first.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(nested, [False])
        self.assertFalse(scheduler.notify_exit_event(EXIT, source=old, run=RUN))
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 60.0
        release.set()
        first.join(2)
        self.assertFalse(first.is_alive())
        self.assertEqual(outcomes, [True])
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["run_incarnation"], old._run_incarnation)
        self.assertGreater(exits[0]["run_incarnation"], 0)
        self.assertFalse(scheduler.notify_exit_event(EXIT, source=old, run=RUN))
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)
        fresh = scheduler.bind_exit_source(RUN)
        self.assertTrue(scheduler.notify_exit_event(EXIT, source=fresh, run=RUN))
        self.assertEqual(len(exits), 2)
        self.assertEqual(exits[1]["run_incarnation"], fresh._run_incarnation)
        self.assertNotEqual(exits[0]["run_incarnation"], exits[1]["run_incarnation"])

    def test_failed_old_ui_retry_finishes_once_without_fencing_new_run(self):
        owner = SerializedReviewAdmission(RUN, AUTHORITY)
        now = [0.0]
        delivered, exits = [], []
        attempts = []

        def on_exit(event):
            attempts.append(dict(event))
            if len(attempts) == 1:
                raise RuntimeError("UI unavailable")
            exits.append(dict(event))

        scheduler = scheduler_for(owner, now, delivered, exits, on_exit=on_exit)
        self.assertEqual(scheduler.tick().status, "waiting")
        old = scheduler.bind_exit_source(RUN)
        self.assertFalse(scheduler.notify_exit_event(EXIT, source=old, run=RUN))
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        self.assertEqual(scheduler.tick().status, "waiting")
        self.assertEqual(len(attempts), 2)
        self.assertEqual([event["run_incarnation"] for event in attempts],
                         [old._run_incarnation, old._run_incarnation])
        self.assertGreater(old._run_incarnation, 0)
        self.assertFalse(scheduler.notify_exit_event(EXIT, source=old, run=RUN))
        now[0] = 60.0
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)

    def test_old_workflow_binding_cannot_rebind_after_same_identity_replacement(self):
        workflow, _ = fixture()
        binding = WorkflowObservationBinding.attach(workflow)
        owner = SerializedReviewAdmission(None, AUTHORITY)
        exits = []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: AUTHORITY,
            active_run=lambda: binding.run, worker_state=lambda _: None,
            collect_non_model=lambda _: {},
            dispatch_review=lambda _: {"status": "omp_processed"},
            user_priority=lambda _: False,
            on_exit=lambda event: exits.append(dict(event)), clock=lambda: 0,
        )
        source = scheduler.activate_run_source(binding.run, workflow)
        self.assertIsNotNone(source)
        self.assertTrue(binding.bind(scheduler, source))
        owner.set_active_run(None)
        replacement = copy(workflow)
        fresh = scheduler.activate_run_source(binding.run, replacement)
        self.assertIsNotNone(fresh)
        self.assertFalse(binding.bind(scheduler, source))
        self.assertFalse(binding.bind(scheduler, fresh))
        self.assertFalse(binding.collect_and_notify(scheduler)[1])
        self.assertFalse(WorkflowObservationBinding.attach(workflow).bind(scheduler, fresh))
        self.assertEqual(exits, [])
        self.assertIsNotNone(owner.issue_review(binding.run, 60.0, 0, {}))


if __name__ == "__main__":
    unittest.main()
