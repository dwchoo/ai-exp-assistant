from __future__ import annotations

import unittest
from threading import Event, Thread
import time

from workbench.observation.worker_review import (
    ActiveRunRef,
    UsageLedger,
    UsageValue,
    SerializedReviewAdmission,
    WorkerReviewScheduler as _WorkerReviewScheduler,
    investigate_hang,
)


class _SingleThreadHintAdmission(SerializedReviewAdmission):
    """Keep the original callback-focused unit fixtures single-threaded."""

    def reconcile_observation(self, run, state, *, observed_epoch):
        if self.snapshot() != observed_epoch:
            return False
        if self._run != run:
            self.set_active_run(run)
        if self._state != self._validated_state(state):
            self.set_automation_state(state)
        return True

    def bind_exit_source(self, run):
        if self._run != run:
            self.set_active_run(run)
        return super().bind_exit_source(run)


def WorkerReviewScheduler(**kwargs):
    return _WorkerReviewScheduler(admission=_SingleThreadHintAdmission(), **kwargs)


AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False,
    "metadataHealthy": True, "approvalValid": True,
}}


def run_ref(*, run_id: str = "run-1", session_id: str = "worker-session-1",
            generation: int = 1, active: bool = True) -> ActiveRunRef:
    return ActiveRunRef("task-1", "revision-1", 1, run_id, session_id, generation, active)


def exit_identity(run: ActiveRunRef) -> dict[str, object]:
    return {
        "task_id": run.task_id,
        "revision_id": run.revision_id,
        "revision": run.revision,
        "run_id": run.run_id,
        "session_id": run.session_id,
        "session_generation": run.session_generation,
    }


def ready_state(run: ActiveRunRef) -> dict[str, object]:
    return {
        "role": "worker", "sessionId": run.session_id,
        "generation": run.session_generation, "idle": True,
        "pending": False, "approvalPending": False,
        "editorKnown": True, "editorEmpty": True,
        "inFlightToolCount": 0, "paused": False,
    }


class FakeClock:
    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class WorkerReviewTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.run = run_ref()
        self.automation = dict(AUTOMATION)
        self.worker_state = ready_state(self.run)
        self.user_priority = False
        self.collected = []
        self.dispatches = []
        self.exits = []

        def collect(active_run):
            self.collected.append(active_run.run_id)
            return {
                "phase": "running", "progress_count": len(self.collected),
                "output_bytes": 128, "process_count": 2, "process_alive": True,
                "exit_confirmed": False, "last_output_age_seconds": 240,
                "raw_log_excerpt": "PRIVATE_LOG_SENTINEL",
                "unknowns": [],
            }

        def dispatch(request):
            self.dispatches.append(request)
            # Public callers use only status; accidental model text is not retained.
            return {"status": "omp_processed", "model_text": "PRIVATE_MODEL_SENTINEL"}

        self.collect_callback = collect
        self.dispatch_callback = dispatch
        self.scheduler = WorkerReviewScheduler(
            automation_state=lambda: self.automation,
            active_run=lambda: self.run,
            worker_state=lambda _run: self.worker_state,
            collect_non_model=collect,
            dispatch_review=dispatch,
            user_priority=lambda _run: self.user_priority,
            on_exit=self.exits.append,
            clock=self.clock,
        )

    def test_exact_sixty_second_boundary_and_sanitized_request(self):
        first = self.scheduler.tick()
        self.assertEqual(first.status, "waiting")
        self.clock.advance(59.999)
        self.assertFalse(self.scheduler.tick().dispatched)
        self.clock.advance(0.001)
        result = self.scheduler.tick()
        self.assertEqual(result.status, "dispatched")
        self.assertEqual(len(self.dispatches), 1)
        request = self.dispatches[0]
        self.assertEqual(request.due_at, 160.0)
        self.assertEqual(request.facts["phase"], "running")
        self.assertNotIn("raw_log_excerpt", request.facts)
        self.assertNotIn("PRIVATE_LOG_SENTINEL", repr(request))
        self.assertNotIn("PRIVATE_MODEL_SENTINEL", repr(result))
        self.assertEqual(self.collected, ["run-1", "run-1", "run-1"])

    def test_one_hundred_twenty_first_periodic_review_runs_without_cap(self):
        self.scheduler.tick()
        for _ in range(121):
            self.clock.advance(60)
            result = self.scheduler.tick()
        self.assertEqual(result.status, "dispatched")
        self.assertEqual(result.review_count, 121)
        self.assertEqual(len(self.dispatches), 121)

    def test_busy_due_reviews_coalesce_to_only_the_latest(self):
        self.scheduler.tick()
        self.worker_state = {**ready_state(self.run), "idle": False}
        self.clock.advance(60)
        first = self.scheduler.tick()
        self.assertEqual(first.status, "delayed")
        self.assertTrue(first.pending)
        self.clock.advance(180)
        delayed = self.scheduler.tick()
        self.assertEqual(delayed.status, "delayed")
        self.assertEqual(delayed.coalesced_count, 3)
        self.assertEqual(self.dispatches, [])

        self.worker_state = ready_state(self.run)
        dispatched = self.scheduler.tick()
        self.assertEqual(dispatched.status, "dispatched")
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.dispatches[0].due_at, 340.0)
        self.assertEqual(self.dispatches[0].coalesced_count, 3)

    def test_user_composer_or_conversation_priority_delays_until_clear(self):
        self.scheduler.tick()
        self.clock.advance(60)
        self.user_priority = True
        delayed = self.scheduler.tick()
        self.assertEqual(delayed.status, "delayed")
        self.assertEqual(delayed.reason, "user_priority")
        self.assertEqual(self.dispatches, [])
        self.user_priority = False
        self.assertEqual(self.scheduler.tick().status, "dispatched")

    def test_pause_keeps_collecting_but_never_starts_model_review_until_resumed(self):
        self.scheduler.tick()
        self.automation = {**AUTOMATION, "payload": {
            **AUTOMATION["payload"], "paused": True,
        }}
        self.clock.advance(60)
        paused = self.scheduler.tick()
        self.assertEqual(paused.status, "paused")
        self.assertTrue(paused.pending)
        self.assertEqual(self.dispatches, [])
        self.assertEqual(len(self.collected), 2)
        self.clock.advance(120)
        self.assertEqual(self.scheduler.tick().status, "paused")
        self.assertEqual(len(self.collected), 3)
        self.automation = dict(AUTOMATION)
        resumed = self.scheduler.tick()
        self.assertEqual(resumed.status, "dispatched")
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.dispatches[0].due_at, 280.0)

    def test_cancel_or_invalid_authority_discards_pending_review_fail_closed(self):
        self.scheduler.tick()
        self.clock.advance(60)
        self.worker_state = {**ready_state(self.run), "idle": False}
        self.assertTrue(self.scheduler.tick().pending)
        self.automation = {**AUTOMATION, "payload": {
            **AUTOMATION["payload"], "cancelled": True,
        }}
        held = self.scheduler.tick()
        self.assertEqual(held.status, "held")
        self.assertFalse(held.pending)
        self.assertEqual(self.dispatches, [])
        self.assertEqual(len(self.collected), 3)

    def test_wrong_session_generation_is_busy_not_a_safe_review_target(self):
        self.scheduler.tick()
        self.clock.advance(60)
        self.worker_state = {**ready_state(self.run), "generation": 2}
        delayed = self.scheduler.tick()
        self.assertEqual(delayed.status, "delayed")
        self.assertEqual(delayed.reason, "worker_session_mismatch")
        self.assertEqual(self.dispatches, [])

    def test_boolean_in_flight_count_is_not_treated_as_zero(self):
        self.scheduler.tick()
        self.clock.advance(60)
        self.worker_state = {**ready_state(self.run), "inFlightToolCount": False}
        delayed = self.scheduler.tick()
        self.assertEqual(delayed.status, "delayed")
        self.assertEqual(delayed.reason, "worker_busy_or_unknown")
        self.assertEqual(self.dispatches, [])

    def test_only_confirmed_exit_is_surfaced_immediately_even_during_pause(self):
        self.assertFalse(self.scheduler.notify_exit_event({
            "run_id": "run-1", "exit_confirmed": False, "exit_status": 0,
        }))
        unbound = {"run_id": "run-1", "exit_confirmed": True, "exit_status": 7}
        self.assertFalse(self.scheduler.notify_exit_event(unbound))
        self.assertFalse(self.scheduler.notify_exit_event(unbound, run=self.run))
        event = {**exit_identity(self.run), "exit_confirmed": True,
                 "exit_status": 7, "event_sequence": 3,
                 "model_text": "PRIVATE_MODEL_SENTINEL"}
        source = self.scheduler.bind_exit_source(self.run)
        self.assertTrue(self.scheduler.notify_exit_event(event, source=source))
        self.assertEqual(len(self.exits), 1)
        self.assertEqual(self.exits[0]["exit_status"], 7)
        self.assertNotIn("model_text", self.exits[0])
        self.assertFalse(self.scheduler.notify_exit_event(event, source=source))
        self.assertEqual(len(self.exits), 1)

    def test_collection_reports_confirmed_exit_without_waiting_for_review_tick(self):
        def collect(_run):
            return {"exit_confirmed": True, "exit_status": 0}

        self.scheduler = WorkerReviewScheduler(
            automation_state=lambda: self.automation,
            active_run=lambda: self.run,
            worker_state=lambda _run: self.worker_state,
            collect_non_model=collect,
            dispatch_review=self.dispatch_callback,
            user_priority=lambda _run: False,
            on_exit=self.exits.append,
            clock=self.clock,
        )
        result = self.scheduler.tick()
        self.assertEqual(result.status, "exited")
        self.assertEqual(result.exit_event["exit_confirmed"], True)
        self.assertEqual(self.exits[0]["run_id"], "run-1")
        self.assertEqual(self.dispatches, [])

    def test_silence_or_elapsed_time_alone_never_classifies_hang_or_exit(self):
        quiet = investigate_hang(
            "run-1", process_evidence=[], lifecycle_evidence={"phase": "running"},
            silence_seconds=86_400, unknowns=["process_tree_not_observed"],
        )
        self.assertEqual(quiet["classification"], "unknown")
        self.assertEqual(quiet["unknowns"], ["process_tree_not_observed"])
        self.assertTrue(quiet["suggested_next_observations"])

        blocked = investigate_hang(
            "run-1",
            process_evidence=[{"pid": 42, "alive": True, "state": "D",
                               "stalled": True, "blocked_on": "io"}],
            lifecycle_evidence={"phase": "running", "exit_confirmed": False},
            silence_seconds=0,
        )
        self.assertEqual(blocked["classification"], "suspected_hang")
        self.assertIn("process evidence", blocked["basis"])

        confirmed = investigate_hang(
            "run-1", process_evidence=[{"pid": 42, "alive": False, "state": "X"}],
            lifecycle_evidence={"phase": "exited", "exit_confirmed": True,
                                "lifetime_ended": True},
            silence_seconds=10_000,
        )
        self.assertEqual(confirmed["classification"], "exit_observed")

    def test_usage_keeps_observed_estimated_and_unknown_distinct(self):
        usage = UsageLedger()
        usage.record(UsageValue(21, "observed"))
        usage.record(UsageValue(8, "estimated"))
        usage.record(UsageValue(None, "unknown"))
        summary = usage.snapshot()
        self.assertEqual(summary.observed, 21)
        self.assertEqual(summary.estimated, 8)
        self.assertEqual(summary.unknown_samples, 1)
        self.assertEqual(summary.as_dict()["tokens_unknown"], "unknown")

    def test_final_guard_rechecks_authority_after_priority_callback(self):
        for change, expected_status, expected_pending in (
            ({"paused": True}, "paused", True),
            ({"cancelled": True}, "held", False),
            ({"metadataHealthy": False}, "held", False),
            ({"approvalValid": False}, "held", False),
        ):
            with self.subTest(change=change):
                clock = FakeClock()
                run = run_ref()
                authority = [dict(AUTOMATION)]
                dispatches = []

                def priority(_run):
                    authority[0] = {**AUTOMATION, "payload": {
                        **AUTOMATION["payload"], **change,
                    }}
                    return False

                scheduler = WorkerReviewScheduler(
                    automation_state=lambda: authority[0],
                    active_run=lambda: run,
                    worker_state=lambda active: ready_state(active),
                    collect_non_model=lambda _run: {"phase": "running"},
                    dispatch_review=lambda request: dispatches.append(request) or {
                        "status": "omp_processed",
                    },
                    user_priority=priority,
                    on_exit=lambda _event: None,
                    clock=clock,
                )
                scheduler.tick()
                clock.advance(60)
                result = scheduler.tick()
                self.assertEqual(result.status, expected_status)
                self.assertEqual(result.pending, expected_pending)
                self.assertEqual(dispatches, [])

    def test_final_guard_rechecks_active_run_after_readiness_callback(self):
        clock = FakeClock()
        original = run_ref()
        replacement = run_ref(run_id="replacement", session_id="worker-session-2", generation=2)
        current = [original]
        dispatches = []

        def readiness(_run):
            current[0] = replacement
            return ready_state(original)

        scheduler = WorkerReviewScheduler(
            automation_state=lambda: AUTOMATION,
            active_run=lambda: current[0],
            worker_state=readiness,
            collect_non_model=lambda _run: {"phase": "running"},
            dispatch_review=lambda request: dispatches.append(request) or {
                "status": "omp_processed",
            },
            user_priority=lambda _run: False,
            on_exit=lambda _event: None,
            clock=clock,
        )
        scheduler.tick()
        clock.advance(60)
        result = scheduler.tick()
        self.assertEqual(result.status, "held")
        self.assertEqual(result.reason, "active_run_changed")
        self.assertFalse(result.pending)
        self.assertEqual(dispatches, [])

    def test_fact_callback_run_replacement_is_rechecked_before_dispatch(self):
        clock = FakeClock()
        original = run_ref()
        replacement = run_ref(run_id="replacement", session_id="worker-session-2", generation=2)
        current = [original]
        collections = [0]
        dispatches = []

        def collect(_run):
            collections[0] += 1
            if collections[0] == 2:
                current[0] = replacement
            return {"phase": "running"}

        scheduler = WorkerReviewScheduler(
            automation_state=lambda: AUTOMATION,
            active_run=lambda: current[0],
            worker_state=lambda run: ready_state(run),
            collect_non_model=collect,
            dispatch_review=lambda request: dispatches.append(request) or {
                "status": "omp_processed",
            },
            user_priority=lambda _run: False,
            on_exit=lambda _event: None,
            clock=clock,
        )
        scheduler.tick()
        clock.advance(60)
        result = scheduler.tick()
        self.assertEqual(result.status, "held")
        self.assertEqual(result.reason, "active_run_changed")
        self.assertEqual(dispatches, [])

    def test_final_active_run_callback_mutation_is_followed_by_authority_read(self):
        clock = FakeClock()
        run = run_ref()
        authority = [dict(AUTOMATION)]
        active_run_calls = [0]
        dispatches = []

        def active_run():
            active_run_calls[0] += 1
            if active_run_calls[0] == 3:
                authority[0] = {**AUTOMATION, "payload": {
                    **AUTOMATION["payload"], "cancelled": True,
                }}
            return run

        scheduler = WorkerReviewScheduler(
            automation_state=lambda: authority[0],
            active_run=active_run,
            worker_state=lambda active: ready_state(active),
            collect_non_model=lambda _run: {"phase": "running"},
            dispatch_review=lambda request: dispatches.append(request) or {
                "status": "omp_processed",
            },
            user_priority=lambda _run: False,
            on_exit=lambda _event: None,
            clock=clock,
        )
        scheduler.tick()
        clock.advance(60)
        result = scheduler.tick()
        self.assertEqual(active_run_calls[0], 3)
        self.assertEqual(result.status, "held")
        self.assertEqual(result.reason, "automation_not_authorized")
        self.assertEqual(dispatches, [])

    def test_late_old_session_exit_cannot_bind_to_new_same_run_id(self):
        old_run = run_ref()
        new_run = run_ref(session_id="worker-session-2", generation=2)
        current = [old_run]
        exits = []
        scheduler = WorkerReviewScheduler(
            automation_state=lambda: AUTOMATION,
            active_run=lambda: current[0],
            worker_state=lambda run: ready_state(run),
            collect_non_model=lambda _run: {"phase": "running"},
            dispatch_review=self.dispatch_callback,
            user_priority=lambda _run: False,
            on_exit=exits.append,
            clock=self.clock,
        )
        scheduler.tick()
        current[0] = new_run
        self.assertEqual(scheduler.tick().status, "waiting")
        old_exit = {**exit_identity(old_run), "exit_confirmed": True, "exit_status": 6}
        self.assertFalse(scheduler.notify_exit_event({
            "run_id": new_run.run_id, "exit_confirmed": True, "exit_status": 6,
        }, run=new_run))
        self.assertFalse(scheduler.notify_exit_event(old_exit, run=old_run))
        self.assertEqual(exits, [])
        self.clock.advance(60)
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(self.dispatches[-1].run.identity, new_run.identity)

    def test_exit_callback_exception_can_retry_but_success_is_once_only(self):
        run = run_ref()
        attempts = []
        surfaced = []

        def on_exit(event):
            attempts.append(dict(event))
            if len(attempts) == 1:
                raise RuntimeError("transient display failure")
            surfaced.append(dict(event))

        scheduler = WorkerReviewScheduler(
            automation_state=lambda: AUTOMATION,
            active_run=lambda: run,
            worker_state=lambda active: ready_state(active),
            collect_non_model=lambda _run: {"phase": "running"},
            dispatch_review=self.dispatch_callback,
            user_priority=lambda _run: False,
            on_exit=on_exit,
            clock=self.clock,
        )
        scheduler.tick()
        event = {**exit_identity(run), "exit_confirmed": True, "exit_status": 0}
        source = scheduler.bind_exit_source(run)
        self.assertFalse(scheduler.notify_exit_event(event, source=source, run=run))
        self.assertTrue(scheduler.notify_exit_event(event, source=source, run=run))
        self.assertFalse(scheduler.notify_exit_event(event, source=source, run=run))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(surfaced), 1)

    def test_pending_exit_retries_after_active_run_disappears(self):
        run = run_ref()
        current = [run]
        attempts = []

        def on_exit(event):
            attempts.append(dict(event))
            if len(attempts) == 1:
                raise RuntimeError("transient display failure")

        scheduler = WorkerReviewScheduler(
            automation_state=lambda: AUTOMATION,
            active_run=lambda: current[0],
            worker_state=lambda active: ready_state(active),
            collect_non_model=lambda _run: {"phase": "running"},
            dispatch_review=self.dispatch_callback,
            user_priority=lambda _run: False,
            on_exit=on_exit,
            clock=self.clock,
        )
        scheduler.tick()
        event = {**exit_identity(run), "exit_confirmed": True, "exit_status": 0}
        source = scheduler.bind_exit_source(run)
        self.assertFalse(scheduler.notify_exit_event(event, source=source, run=run))
        current[0] = None
        self.assertEqual(scheduler.tick().status, "inactive")
        self.assertEqual(len(attempts), 2)
        self.assertFalse(scheduler.notify_exit_event(event))

    def test_exit_callback_is_reentrant_and_concurrent_once_only(self):
        run = run_ref()
        event = {**exit_identity(run), "exit_confirmed": True, "exit_status": 0}
        entered = Event()
        release = Event()
        scheduler_ref = []
        callback_calls = []
        reentrant_results = []
        first_results = []
        source_ref = []

        def on_exit(public_event):
            callback_calls.append(dict(public_event))
            reentrant_results.append(
                scheduler_ref[0].notify_exit_event(event, source=source_ref[0], run=run)
            )
            entered.set()
            release.wait(1)

        scheduler = WorkerReviewScheduler(
            automation_state=lambda: AUTOMATION,
            active_run=lambda: run,
            worker_state=lambda active: ready_state(active),
            collect_non_model=lambda _run: {"phase": "running"},
            dispatch_review=self.dispatch_callback,
            user_priority=lambda _run: False,
            on_exit=on_exit,
            clock=self.clock,
        )
        scheduler_ref.append(scheduler)
        source_ref.append(scheduler.bind_exit_source(run))
        first = Thread(target=lambda: first_results.append(
            scheduler.notify_exit_event(event, source=source_ref[0], run=run)
        ))
        first.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(scheduler.notify_exit_event(event, source=source_ref[0], run=run))
        finally:
            release.set()
            first.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_results, [True])
        self.assertEqual(reentrant_results, [False])
        self.assertEqual(len(callback_calls), 1)
        self.assertFalse(scheduler.notify_exit_event(event, source=source_ref[0], run=run))
        self.assertEqual(len(callback_calls), 1)

    def test_confirmed_exit_fences_stale_running_identity_but_not_new_identity(self):
        current = [run_ref()]
        collect_calls = []
        dispatches = []
        scheduler = WorkerReviewScheduler(
            automation_state=lambda: AUTOMATION,
            active_run=lambda: current[0],
            worker_state=lambda run: ready_state(run),
            collect_non_model=lambda run: collect_calls.append(run.identity) or {
                "phase": "running", "exit_confirmed": False,
            },
            dispatch_review=lambda request: dispatches.append(request) or {
                "status": "omp_processed",
            },
            user_priority=lambda _run: False,
            on_exit=lambda _event: None,
            clock=self.clock,
        )
        scheduler.tick()
        self.clock.advance(60)
        scheduler.tick()
        self.assertEqual(len(dispatches), 1)
        source = scheduler.bind_exit_source(current[0])
        self.assertTrue(scheduler.notify_exit_event({
            **exit_identity(current[0]), "exit_confirmed": True, "exit_status": 0,
        }, source=source, run=current[0]))
        self.clock.advance(60)
        fenced = scheduler.tick()
        self.assertEqual(fenced.status, "exited")
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(len(collect_calls), 2)

        current[0] = run_ref(generation=2)
        independent = scheduler.tick()
        self.assertEqual(independent.status, "waiting")
        self.assertEqual(independent.review_count, 0)
        self.assertEqual(len(dispatches), 1)

    def test_bounded_hang_investigation_flows_to_tick_and_review_without_private_data(self):
        private = "PRIVATE_PROCESS_ARGUMENT_SENTINEL"
        calls = []

        def investigate(run, timeout_seconds):
            calls.append((run.identity, timeout_seconds))
            return {
                "process_evidence": [{
                    "pid": 42, "alive": True, "stalled": True,
                    "blocked_on": "io", "private_command": private,
                }],
                "lifecycle_evidence": {"phase": "running", "private": private},
                "silence_seconds": 300,
                "unknowns": ["process_tree_partial", private],
            }

        scheduler = WorkerReviewScheduler(
            automation_state=lambda: self.automation,
            active_run=lambda: self.run,
            worker_state=lambda run: ready_state(run),
            collect_non_model=lambda _run: {"phase": "running"},
            investigate_non_model=investigate,
            investigation_timeout_seconds=0.25,
            dispatch_review=self.dispatch_callback,
            user_priority=lambda _run: False,
            on_exit=self.exits.append,
            clock=self.clock,
        )
        first = scheduler.tick()
        self.assertEqual(first.hang_investigation["classification"], "suspected_hang")
        self.assertEqual(first.hang_investigation["source"], "bounded_non_model_investigator")
        self.assertEqual(first.hang_investigation["investigation_status"], "completed")
        self.assertEqual(calls[0][1], 0.25)
        self.assertNotIn(private, repr(first))
        self.clock.advance(60)
        review = scheduler.tick()
        self.assertTrue(review.dispatched)
        forwarded = self.dispatches[-1].facts["hang_investigation"]
        self.assertEqual(forwarded["classification"], "suspected_hang")
        self.assertIn("process_tree_partial", forwarded["unknowns"])
        self.assertIn("non_model_unknown_present", forwarded["unknowns"])
        self.assertNotIn(private, repr(self.dispatches[-1]))

    def test_hang_investigation_timeout_is_bounded_and_remains_unknown(self):
        release = Event()

        def slow_investigator(_run, _timeout_seconds):
            release.wait(1)
            return {"process_evidence": [], "lifecycle_evidence": {"phase": "running"}}

        scheduler = WorkerReviewScheduler(
            automation_state=lambda: self.automation,
            active_run=lambda: self.run,
            worker_state=lambda run: ready_state(run),
            collect_non_model=lambda _run: {"phase": "running"},
            investigate_non_model=slow_investigator,
            investigation_timeout_seconds=0.02,
            dispatch_review=self.dispatch_callback,
            user_priority=lambda _run: False,
            on_exit=self.exits.append,
            clock=self.clock,
        )
        started = time.monotonic()
        result = scheduler.tick()
        elapsed = time.monotonic() - started
        release.set()
        self.assertLess(elapsed, 0.5)
        self.assertEqual(result.hang_investigation["classification"], "unknown")
        self.assertEqual(result.hang_investigation["investigation_status"], "timeout")
        self.assertIn("investigation_timeout", result.hang_investigation["unknowns"])

    def test_confirmed_lifecycle_investigation_surfaces_exit_and_fences_review(self):
        scheduler = WorkerReviewScheduler(
            automation_state=lambda: self.automation,
            active_run=lambda: self.run,
            worker_state=lambda run: ready_state(run),
            collect_non_model=lambda _run: {"phase": "running", "exit_confirmed": False},
            investigate_non_model=lambda _run, _timeout: {
                "lifecycle_evidence": {
                    "phase": "exited", "exit_confirmed": True,
                    "lifetime_ended": True, "exit_status": 4,
                },
            },
            dispatch_review=self.dispatch_callback,
            user_priority=lambda _run: False,
            on_exit=self.exits.append,
            clock=self.clock,
        )
        result = scheduler.tick()
        self.assertEqual(result.status, "exited")
        self.assertEqual(result.exit_event["exit_status"], 4)
        self.assertEqual(result.hang_investigation["classification"], "exit_observed")
        self.assertEqual(self.exits[0]["run_id"], "run-1")
        self.assertEqual(self.dispatches, [])

    def test_scheduler_usage_path_preserves_provenance_and_missing_is_unknown(self):
        values = iter((
            {"value": 21, "kind": "observed", "unit": "tokens", "private": "SECRET"},
            {"value": 8, "kind": "estimated", "unit": "tokens"},
            {"value": None, "kind": "unknown", "unit": "tokens"},
        ))
        scheduler = WorkerReviewScheduler(
            automation_state=lambda: self.automation,
            active_run=lambda: self.run,
            worker_state=lambda run: ready_state(run),
            collect_non_model=lambda _run: {"phase": "running"},
            collect_usage=lambda _run: next(values),
            dispatch_review=self.dispatch_callback,
            user_priority=lambda _run: False,
            on_exit=self.exits.append,
            clock=self.clock,
        )
        first = scheduler.tick()
        self.assertEqual(first.usage["tokens_observed"], 21)
        self.assertEqual(first.usage["tokens_estimated"], "unknown")
        self.clock.advance(60)
        second = scheduler.tick()
        self.assertEqual(second.usage["tokens_observed"], 21)
        self.assertEqual(second.usage["tokens_estimated"], 8)
        self.assertEqual(self.dispatches[-1].facts["usage"]["tokens_estimated"], 8)
        self.assertNotIn("SECRET", repr(self.dispatches[-1]))
        self.clock.advance(60)
        third = scheduler.tick()
        self.assertEqual(third.usage["tokens_unknown"], "unknown")
        self.assertEqual(third.usage["unknown_samples"], 1)

        missing_scheduler = WorkerReviewScheduler(
            automation_state=lambda: self.automation,
            active_run=lambda: self.run,
            worker_state=lambda run: ready_state(run),
            collect_non_model=lambda _run: {"phase": "running"},
            dispatch_review=self.dispatch_callback,
            user_priority=lambda _run: False,
            on_exit=self.exits.append,
            clock=FakeClock(),
        )
        missing = missing_scheduler.tick()
        self.assertEqual(missing.usage["tokens_observed"], "unknown")
        self.assertEqual(missing.usage["tokens_estimated"], "unknown")
        self.assertEqual(missing.usage["tokens_unknown"], "unknown")
        self.assertEqual(missing.usage["unknown_samples"], 1)


if __name__ == "__main__":
    unittest.main()
