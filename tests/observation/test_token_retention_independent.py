"""Bounded token ownership during long CW-11 observation and source churn."""
from __future__ import annotations

from copy import copy
import gc
import unittest
from unittest.mock import patch
from weakref import ref

import workbench.observation.worker_review as review_module
from workbench.observation.worker_review import (
    ActiveRunRef, SerializedReviewAdmission, WorkerReviewScheduler,
)
from workbench.observation.workflow_binding import WorkflowObservationBinding
from tests.observation.test_workflow_binding import fixture


RUN = ActiveRunRef("task", "revision", 1, "run", "session", 1)
AUTHORITY = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True,
    "approvalValid": True,
}}


class IndependentRetentionTests(unittest.TestCase):
    def test_twelve_thousand_active_no_review_ticks_reuse_one_internal_token(self):
        owner = SerializedReviewAdmission(RUN, AUTHORITY)
        now = [0.0]
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: AUTHORITY,
            active_run=lambda: RUN, worker_state=lambda _: None,
            collect_non_model=lambda _: {"phase": "running"},
            dispatch_review=lambda _: self.fail("no review is due"),
            user_priority=lambda _: False, on_exit=lambda _: None,
            clock=lambda: now[0],
        )
        self.assertEqual(scheduler.tick().status, "waiting")
        token = owner.bind_exit_source(RUN)
        self.assertIsNotNone(token)
        sizes = []
        for ordinal in range(1, 12_001):
            result = scheduler.tick()
            self.assertEqual(result.status, "waiting")
            if ordinal in {1, 100, 1_000, 12_000}:
                sizes.append((len(owner._exit_sources), len(owner._tickets),
                              len(owner._external_exit_sources)))
                self.assertIs(owner.bind_exit_source(RUN), token)
        self.assertEqual(sizes, [(1, 0, 0)] * 4)
        self.assertIs(owner._internal_exit_source, token)
        self.assertEqual(scheduler.tick().next_due_at, 60.0)

    def test_weak_external_registry_tracks_live_bindings_during_source_churn(self):
        workflow, _ = fixture()
        run = WorkflowObservationBinding.resolve_run(workflow)
        owner = SerializedReviewAdmission(None, AUTHORITY)
        retained = []
        for ordinal in range(500):
            source = copy(workflow)
            token = owner.activate_run_source(run, source)
            self.assertIsNotNone(token)
            self.assertEqual(len(owner._exit_sources), 1)
            if ordinal % 50 == 0:
                retained.append(WorkflowObservationBinding.attach(source))
            owner.set_active_run(None)
            self.assertEqual(len(owner._exit_sources), 0)
            self.assertIsNone(owner._active_external_source)
        del source
        gc.collect()
        self.assertEqual(len(owner._external_exit_sources), len(retained))
        for binding in retained:
            self.assertIsNone(owner.activate_run_source(run, binding.workflow_run))
        del binding
        retained.clear()
        gc.collect()
        self.assertEqual(len(owner._external_exit_sources), 0)

    def test_stale_object_id_entry_cannot_authorize_a_new_source(self):
        workflow, _ = fixture()
        run = WorkflowObservationBinding.resolve_run(workflow)
        owner = SerializedReviewAdmission(None, AUTHORITY)
        with patch.object(review_module, "id", lambda _source: 12345, create=True):
            first = copy(workflow)
            first_ref = ref(first)
            old = owner.activate_run_source(run, first)
            self.assertIsNotNone(old)
            owner.set_active_run(None)
            self.assertIsNone(owner.activate_run_source(run, first))
            other = copy(workflow)
            self.assertIsNone(owner.activate_run_source(run, other))
            del first
            gc.collect()
            self.assertIsNone(first_ref())
            self.assertEqual(len(owner._external_exit_sources), 0)
            fresh = owner.activate_run_source(run, other)
            self.assertIsNotNone(fresh)
            self.assertNotEqual(fresh._run_incarnation, old._run_incarnation)
            self.assertFalse(owner.fence_exit(old))

    def test_exit_clears_current_tokens_but_retired_source_stays_rejected(self):
        workflow, _ = fixture()
        run = WorkflowObservationBinding.resolve_run(workflow)
        owner = SerializedReviewAdmission(None, AUTHORITY)
        external = owner.activate_run_source(run, workflow)
        internal = owner.bind_exit_source(run)
        self.assertEqual(len(owner._exit_sources), 2)
        self.assertTrue(owner.fence_exit(external))
        self.assertEqual(len(owner._exit_sources), 0)
        self.assertIsNone(owner._internal_exit_source)
        self.assertIsNone(owner._active_external_source)
        self.assertIsNone(owner.bind_exit_source(run))
        self.assertFalse(owner.fence_exit(internal))
        owner.set_active_run(None)
        self.assertIsNone(owner.activate_run_source(run, workflow))
        replacement = copy(workflow)
        fresh = owner.activate_run_source(run, replacement)
        self.assertIsNotNone(fresh)
        self.assertGreater(fresh._run_incarnation, external._run_incarnation)


if __name__ == "__main__":
    unittest.main()
