"""Exit sources and UI state must never cross a same-identity run replacement."""
from __future__ import annotations

from threading import Event, Thread
import unittest

from workbench.observation.worker_review import (
    ActiveRunRef, SerializedReviewAdmission, WorkerReviewScheduler,
)


RUN = ActiveRunRef("task", "revision", 1, "run", "session", 1)
AUTHORITY = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True,
}}
EXIT = {"task_id": RUN.task_id, "revision_id": RUN.revision_id,
        "revision": RUN.revision, "run_id": RUN.run_id,
        "session_id": RUN.session_id, "session_generation": RUN.session_generation,
        "exit_confirmed": True, "exit_status": 0}


def ready(run):
    return {"role": "worker", "sessionId": run.session_id,
            "generation": run.session_generation, "idle": True,
            "pending": False, "approvalPending": False,
            "editorKnown": True, "editorEmpty": True,
            "inFlightToolCount": 0, "paused": False}


class ExitIncarnationTests(unittest.TestCase):
    def test_constructor_and_first_activation_incarnations_are_positive_and_monotonic(self):
        empty = SerializedReviewAdmission(None, AUTHORITY)
        self.assertEqual(empty.run_incarnation(), 0)
        empty.set_active_run(RUN)
        self.assertEqual(empty.run_incarnation(), 1)
        epoch = empty.snapshot()
        self.assertEqual(empty.set_active_run(RUN), epoch)
        self.assertEqual(empty.run_incarnation(), 1)
        empty.set_active_run(None)
        empty.set_active_run(RUN)
        self.assertEqual(empty.run_incarnation(), 3)

        active = SerializedReviewAdmission(RUN, AUTHORITY)
        self.assertEqual(active.run_incarnation(), 1)
        self.assertEqual(active.set_active_run(RUN), active.snapshot())
        self.assertEqual(active.run_incarnation(), 1)
        active.set_active_run(None)
        active.set_active_run(RUN)
        self.assertEqual(active.run_incarnation(), 3)

        inactive = SerializedReviewAdmission(ActiveRunRef(*RUN.identity, active=False), AUTHORITY)
        self.assertEqual(inactive.run_incarnation(), 0)
        inactive.set_active_run(RUN)
        self.assertEqual(inactive.run_incarnation(), 1)

    def test_internal_source_is_reused_for_1500_no_review_ticks(self):
        owner, scheduler, _, _, _, _, surfaced = self.harness()
        first = scheduler.bind_exit_source(RUN)
        self.assertIsNotNone(first)
        for _ in range(1500):
            self.assertEqual(scheduler.tick().status, "waiting")
            self.assertIs(scheduler.bind_exit_source(RUN), first)
            self.assertEqual(len(owner._exit_sources), 1)
        owner.set_automation_state(AUTHORITY)
        self.assertIs(scheduler.bind_exit_source(RUN), first)
        self.assertEqual(len(owner._exit_sources), 1)
        self.assertTrue(scheduler.notify_exit_event(EXIT, source=first, run=RUN))
        self.assertEqual(len(owner._exit_sources), 0)
        self.assertEqual(len(surfaced), 1)
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        fresh = scheduler.bind_exit_source(RUN)
        self.assertIsNot(fresh, first)
        self.assertEqual(len(owner._exit_sources), 1)

    def harness(self, *, on_exit=None, collect=None, active_run=None):
        owner = SerializedReviewAdmission(RUN, AUTHORITY)
        now = [0.0]
        current = [RUN]
        priority = [False]
        delivered = []
        surfaced = []
        scheduler = WorkerReviewScheduler(
            admission=owner,
            automation_state=lambda: AUTHORITY,
            active_run=active_run or (lambda: current[0]),
            worker_state=ready,
            collect_non_model=collect or (lambda _: {"phase": "running"}),
            dispatch_review=lambda request: delivered.append(request) or {"status": "omp_processed"},
            user_priority=lambda _: priority[0],
            on_exit=on_exit or surfaced.append,
            clock=lambda: now[0],
        )
        return owner, scheduler, now, current, priority, delivered, surfaced

    def test_exit_then_same_identity_new_incarnation_has_own_review_and_exit(self):
        owner, scheduler, now, current, _, delivered, surfaced = self.harness()
        self.assertEqual(scheduler.tick().status, "waiting")
        old = scheduler.bind_exit_source(RUN)
        self.assertIsNotNone(old)
        incarnation = owner.run_incarnation()
        epoch = owner.snapshot()
        self.assertEqual(owner.set_active_run(RUN), epoch)  # idempotent A
        self.assertEqual(owner.run_incarnation(), incarnation)
        self.assertTrue(scheduler.notify_exit_event(EXIT, source=old, run=RUN))
        self.assertEqual(scheduler.tick().status, "exited")

        owner.set_active_run(None)
        current[0] = None
        self.assertEqual(scheduler.tick().status, "inactive")
        owner.set_active_run(RUN)
        current[0] = RUN
        self.assertFalse(scheduler.notify_exit_event(EXIT, source=old, run=RUN))
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 59.999
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 60.0
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)
        fresh = scheduler.bind_exit_source(RUN)
        self.assertIsNot(fresh, old)
        self.assertTrue(scheduler.notify_exit_event(EXIT, source=fresh, run=RUN))
        self.assertEqual(len(surfaced), 2)
        self.assertEqual([event["run_incarnation"] for event in surfaced],
                         [old._run_incarnation, fresh._run_incarnation])

    def test_old_external_callback_racing_replacement_cannot_fence_new_run(self):
        entered, release = Event(), Event()
        blocking = [True]

        def active():
            if blocking[0]:
                entered.set()
                release.wait(2)
            return RUN

        owner, scheduler, now, _, _, delivered, surfaced = self.harness(active_run=active)
        old = scheduler.bind_exit_source(RUN)
        outcomes = []
        thread = Thread(target=lambda: outcomes.append(
            scheduler.notify_exit_event(EXIT, source=old, run=RUN)))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            owner.set_active_run(None)
            owner.set_active_run(RUN)
            blocking[0] = False
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [False])
        self.assertEqual(surfaced, [])
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 60
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)

    def test_old_pending_ui_retry_does_not_clear_new_pending_review(self):
        attempts = []
        entered, release = Event(), Event()

        def on_exit(event):
            attempts.append(dict(event))
            if len(attempts) == 1:
                entered.set()
                release.wait(2)
                raise RuntimeError("display unavailable")

        owner, scheduler, now, _, priority, delivered, _ = self.harness(on_exit=on_exit)
        scheduler.tick()
        old = scheduler.bind_exit_source(RUN)
        outcome = []
        thread = Thread(target=lambda: outcome.append(
            scheduler.notify_exit_event(EXIT, source=old, run=RUN)))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            owner.set_active_run(None)
            owner.set_active_run(RUN)
            self.assertEqual(scheduler.tick().status, "waiting")
            now[0] = 60
            priority[0] = True
            self.assertTrue(scheduler.tick().pending)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [False])
        self.assertTrue(scheduler.notify_exit_event(EXIT, source=old, run=RUN))
        self.assertEqual(len(attempts), 2)
        priority[0] = False
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)

    def test_old_pending_ui_is_retried_automatically_after_replacement(self):
        attempts = []

        def on_exit(event):
            attempts.append(dict(event))
            if len(attempts) == 1:
                raise RuntimeError("display unavailable")

        owner, scheduler, now, _, _, delivered, _ = self.harness(on_exit=on_exit)
        scheduler.tick()
        old = scheduler.bind_exit_source(RUN)
        self.assertFalse(scheduler.notify_exit_event(EXIT, source=old, run=RUN))
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        self.assertEqual(scheduler.tick().status, "waiting")
        self.assertEqual(len(attempts), 2)
        self.assertEqual([event["run_incarnation"] for event in attempts],
                         [old._run_incarnation, old._run_incarnation])
        self.assertFalse(scheduler.notify_exit_event(EXIT, source=old, run=RUN))
        now[0] = 60
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)

    def test_internal_facts_captured_before_blocking_collector_cannot_exit_new_run(self):
        entered, release = Event(), Event()
        calls = []

        def collect(_):
            calls.append(None)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
                return {"exit_confirmed": True, "exit_status": 0}
            return {"phase": "running"}

        owner, scheduler, now, _, _, delivered, surfaced = self.harness(collect=collect)
        results = []
        thread = Thread(target=lambda: results.append(scheduler.tick()))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            owner.set_active_run(None)
            owner.set_active_run(RUN)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].reason, "stale_exit_event")
        self.assertEqual(surfaced, [])
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 60
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)


if __name__ == "__main__":
    unittest.main()
