"""Independent adversarial checks for CW-11 review scheduling and evidence policy."""
from __future__ import annotations

import unittest
from threading import Event, Thread

from workbench.observation.worker_review import (
    ActiveRunRef, UsageLedger, UsageValue, SerializedReviewAdmission,
    WorkerReviewScheduler as _WorkerReviewScheduler, investigate_hang,
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


def authority(**changes):
    return {"portVersion": 2, "kind": "AutomationState", "payload": {
        "paused": False, "cancelled": False, "metadataHealthy": True,
        "approvalValid": True, **changes,
    }}


def run_ref(**changes):
    return ActiveRunRef(**{
        "task_id": "task", "revision_id": "revision", "revision": 1,
        "run_id": "run", "session_id": "session", "session_generation": 1,
        **changes,
    })


def exit_identity(run):
    return {
        "task_id": run.task_id, "revision_id": run.revision_id,
        "revision": run.revision, "run_id": run.run_id,
        "session_id": run.session_id,
        "session_generation": run.session_generation,
    }


def ready(run):
    return {"role": "worker", "sessionId": run.session_id,
            "generation": run.session_generation, "idle": True,
            "pending": False, "approvalPending": False,
            "editorKnown": True, "editorEmpty": True,
            "inFlightToolCount": 0, "paused": False}


class Harness:
    def __init__(self):
        self.now = 0.0
        self.run = run_ref()
        self.automation = authority()
        self.worker = ready(self.run)
        self.priority = False
        self.facts = {"phase": "running", "process_alive": True,
                      "exit_confirmed": False, "raw_log_excerpt": "PRIVATE_RAW_LOG",
                      "unknowns": ["safe_unknown", "PRIVATE USER NOTE"]}
        self.collected = []
        self.dispatched = []
        self.exits = []
        self.delivery = {"status": "omp_processed"}
        self.scheduler = WorkerReviewScheduler(
            automation_state=lambda: self.automation,
            active_run=lambda: self.run,
            worker_state=lambda _run: self.worker,
            collect_non_model=self.collect,
            dispatch_review=self.dispatch,
            user_priority=lambda _run: self.priority,
            on_exit=self.exits.append,
            clock=lambda: self.now,
        )

    def collect(self, run):
        self.collected.append((run.run_id, self.now))
        return self.facts

    def dispatch(self, request):
        self.dispatched.append(request)
        if isinstance(self.delivery, Exception):
            raise self.delivery
        return self.delivery

    def tick(self, now):
        self.now = now
        return self.scheduler.tick()


class IndependentReviewTests(unittest.TestCase):
    def test_last_active_run_callback_cannot_invalidate_authority_then_dispatch(self):
        for change in ("pause", "cancel", "approval_loss", "metadata_loss"):
            with self.subTest(change=change):
                h = Harness()
                calls = []

                def active_run():
                    calls.append(h.now)
                    if len(calls) == 4:  # second due-tick guard, after readiness
                        h.automation = authority(**{
                            "pause": {"paused": True},
                            "cancel": {"cancelled": True},
                            "approval_loss": {"approvalValid": False},
                            "metadata_loss": {"metadataHealthy": False},
                        }[change])
                    return h.run

                h.scheduler = WorkerReviewScheduler(
                    automation_state=lambda: h.automation,
                    active_run=active_run,
                    worker_state=lambda _run: h.worker,
                    collect_non_model=h.collect,
                    dispatch_review=h.dispatch,
                    user_priority=lambda _run: False,
                    on_exit=h.exits.append,
                    clock=lambda: h.now,
                )
                self.assertEqual(h.tick(0).status, "waiting")
                stopped = h.tick(60)
                self.assertEqual(len(calls), 4)
                self.assertEqual(stopped.status, "paused" if change == "pause" else "held")
                self.assertEqual(h.dispatched, [])

    def test_late_exit_requires_exact_full_identity_and_never_fences_new_session(self):
        h = Harness()
        old = h.run
        self.assertEqual(h.tick(0).status, "waiting")
        h.run = run_ref(session_id="new-session", session_generation=2)
        h.worker = ready(h.run)
        self.assertEqual(h.tick(0).status, "waiting")
        old_event = {**exit_identity(old), "exit_confirmed": True, "exit_status": 7}
        new_event = {**exit_identity(h.run), "exit_confirmed": True, "exit_status": 7}
        invalid = (
            (old_event, old),
            (old_event, None),
            ({"run_id": h.run.run_id, "exit_confirmed": True, "exit_status": 7}, h.run),
            ({key: value for key, value in new_event.items() if key != "revision_id"}, h.run),
            ({**new_event, "session_generation": True}, h.run),
            ({**new_event, "task_id": "spoofed-task"}, h.run),
            ({**new_event, "session_id": old.session_id}, h.run),
        )
        for event, ref in invalid:
            with self.subTest(event=event, ref=ref):
                self.assertFalse(h.scheduler.notify_exit_event(event, run=ref))
        self.assertEqual(h.exits, [])
        self.assertEqual(h.tick(60).status, "dispatched")
        self.assertEqual(h.dispatched[0].run.identity, h.run.identity)

    def test_failed_exit_surface_retries_once_with_or_without_active_run(self):
        for loses_active in (False, True):
            with self.subTest(loses_active=loses_active):
                h = Harness()
                attempts = []
                successful = []

                def on_exit(event):
                    attempts.append(dict(event))
                    if len(attempts) == 1:
                        raise RuntimeError("PRIVATE_CALLBACK_FAILURE")
                    successful.append(dict(event))

                h.scheduler = WorkerReviewScheduler(
                    automation_state=lambda: h.automation,
                    active_run=lambda: h.run,
                    worker_state=lambda _run: h.worker,
                    collect_non_model=h.collect,
                    dispatch_review=h.dispatch,
                    user_priority=lambda _run: False,
                    on_exit=on_exit, clock=lambda: h.now,
                )
                self.assertEqual(h.tick(0).status, "waiting")
                event = {**exit_identity(h.run), "exit_confirmed": True,
                         "exit_status": 5, "private": "PRIVATE_EXIT_DATA"}
                source = h.scheduler.bind_exit_source(h.run)
                self.assertFalse(h.scheduler.notify_exit_event(event, source=source, run=h.run))
                if loses_active:
                    h.run = None
                outcome = h.tick(60)
                self.assertEqual(outcome.status, "inactive" if loses_active else "exited")
                self.assertEqual(len(attempts), 2)
                self.assertEqual(len(successful), 1)
                self.assertNotIn("PRIVATE_EXIT_DATA", repr(successful))
                self.assertFalse(h.scheduler.notify_exit_event(event))
                self.assertEqual(len(successful), 1)
                self.assertEqual(h.dispatched, [])

    def test_concurrent_and_reentrant_exit_duplicates_do_not_consume_retry(self):
        h = Harness()
        event = {**exit_identity(h.run), "exit_confirmed": True, "exit_status": 0}
        entered, release = Event(), Event()
        attempts, surfaced, reentrant, first_result = [], [], [], []
        scheduler_ref = []
        source_ref = []

        def on_exit(public):
            attempts.append(dict(public))
            reentrant.append(scheduler_ref[0].notify_exit_event(event, source=source_ref[0], run=h.run))
            if len(attempts) == 1:
                entered.set()
                release.wait(1)
                raise RuntimeError("transient display failure")
            surfaced.append(dict(public))

        h.scheduler = WorkerReviewScheduler(
            automation_state=lambda: h.automation,
            active_run=lambda: h.run,
            worker_state=lambda _run: h.worker,
            collect_non_model=h.collect, dispatch_review=h.dispatch,
            user_priority=lambda _run: False, on_exit=on_exit,
            clock=lambda: h.now,
        )
        scheduler_ref.append(h.scheduler)
        h.tick(0)
        source_ref.append(h.scheduler.bind_exit_source(h.run))
        worker = Thread(target=lambda: first_result.append(
            h.scheduler.notify_exit_event(event, source=source_ref[0], run=h.run)))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(h.scheduler.notify_exit_event(event, source=source_ref[0], run=h.run))
        finally:
            release.set()
            worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first_result, [False])
        self.assertTrue(h.scheduler.notify_exit_event(event, source=source_ref[0], run=h.run))
        self.assertFalse(h.scheduler.notify_exit_event(event, source=source_ref[0], run=h.run))
        self.assertEqual(reentrant, [False, False])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(surfaced), 1)
        self.assertEqual(h.tick(60).status, "exited")
        self.assertEqual(h.dispatched, [])

    def test_all_callback_boundaries_recheck_authority_and_run_before_dispatch(self):
        for site in ("facts", "priority", "readiness"):
            for change in ("pause", "cancel", "approval_loss", "run_replaced"):
                with self.subTest(site=site, change=change):
                    h = Harness()
                    injected = [False]

                    def mutate():
                        if injected[0]:
                            return
                        injected[0] = True
                        if change == "pause":
                            h.automation = authority(paused=True)
                        elif change == "cancel":
                            h.automation = authority(cancelled=True)
                        elif change == "approval_loss":
                            h.automation = authority(approvalValid=False)
                        else:
                            h.run = run_ref(run_id="replacement", session_id="next-session")

                    def collect(run):
                        facts = h.collect(run)
                        if site == "facts" and h.now == 60:
                            mutate()
                        return facts

                    def priority(_run):
                        if site == "priority":
                            mutate()
                        return False

                    def worker(_run):
                        if site == "readiness":
                            mutate()
                        return h.worker

                    h.scheduler = WorkerReviewScheduler(
                        automation_state=lambda: h.automation,
                        active_run=lambda: h.run,
                        worker_state=worker,
                        collect_non_model=collect,
                        dispatch_review=h.dispatch,
                        user_priority=priority,
                        on_exit=h.exits.append,
                        clock=lambda: h.now,
                    )
                    self.assertEqual(h.tick(0).status, "waiting")
                    stopped = h.tick(60)
                    self.assertEqual(stopped.status, {
                        "pause": "paused", "cancel": "held",
                        "approval_loss": "held", "run_replaced": "held",
                    }[change])
                    self.assertEqual(h.dispatched, [])
                    if change == "run_replaced":
                        h.worker = ready(h.run)
                        self.assertEqual(h.tick(60).status, "waiting")
                        self.assertEqual(h.tick(120).status, "dispatched")
                        self.assertEqual(h.dispatched[0].run.run_id, "replacement")
                    elif change != "pause":
                        h.automation = authority()
                        self.assertEqual(h.tick(60).status, "waiting")
                        self.assertEqual(h.dispatched, [])

    def test_confirmed_exit_fences_stale_facts_but_not_a_new_run_identity(self):
        h = Harness()
        self.assertEqual(h.tick(0).status, "waiting")
        source = h.scheduler.bind_exit_source(h.run)
        self.assertTrue(h.scheduler.notify_exit_event({
            **exit_identity(h.run), "exit_confirmed": True, "exit_status": 0,
            "event_sequence": 8, "private": "PRIVATE_EXIT"}, source=source, run=h.run))
        for now in (60, 120, 600, 3600):
            with self.subTest(now=now):
                result = h.tick(now)
                self.assertEqual(result.status, "exited")
                self.assertFalse(result.pending)
                self.assertEqual(result.exit_event["event_sequence"], 8)
                self.assertEqual(h.dispatched, [])
                self.assertNotIn("PRIVATE_EXIT", repr(result))
        self.assertEqual(len(h.exits), 1)
        h.run = run_ref(session_id="new-session", session_generation=2)
        h.worker = ready(h.run)
        self.assertEqual(h.tick(3600).status, "waiting")
        self.assertEqual(h.tick(3660).status, "dispatched")
        self.assertEqual(h.dispatched[-1].run.session_id, "new-session")

    def test_scheduler_integrates_bounded_hang_evidence_and_strips_private_fields(self):
        h = Harness()
        secret = "CW11_PRIVATE_INVESTIGATION"
        h.facts = {"phase": "running", "last_output_age_seconds": 1_000_000,
                   "raw_log_excerpt": secret, "exit_confirmed": False}
        evidence = [{"silence_seconds": 1_000_000,
                     "lifecycle_evidence": {"phase": "running"}},
                    {"silence_seconds": 1_000_000,
                     "lifecycle_evidence": {"phase": "running", "exit_confirmed": False,
                                            "private": secret},
                     "process_evidence": [{"pid": 41, "alive": True, "stalled": True,
                                           "blocked_on": "io", "state": "D",
                                           "command_line": secret}],
                     "unknowns": ["safe", secret]}]
        calls = []

        def investigate(run, budget):
            calls.append((run.run_id, budget))
            return evidence[min(len(calls) - 1, 1)]

        h.scheduler = WorkerReviewScheduler(
            automation_state=lambda: h.automation, active_run=lambda: h.run,
            worker_state=lambda _run: h.worker, collect_non_model=h.collect,
            investigate_non_model=investigate, investigation_timeout_seconds=0.2,
            dispatch_review=h.dispatch, user_priority=lambda _run: False,
            on_exit=h.exits.append, clock=lambda: h.now,
        )
        first = h.tick(0)
        self.assertEqual(first.hang_investigation["classification"], "unknown")
        self.assertEqual(first.hang_investigation["silence_seconds"], 1_000_000)
        second = h.tick(60)
        self.assertEqual(second.status, "dispatched")
        self.assertEqual(second.hang_investigation["classification"], "suspected_hang")
        forwarded = h.dispatched[0].facts["hang_investigation"]
        self.assertEqual(forwarded["classification"], "suspected_hang")
        self.assertEqual(forwarded["source"], "bounded_non_model_investigator")
        self.assertNotIn(secret, repr(first))
        self.assertNotIn(secret, repr(second))
        self.assertNotIn(secret, repr(h.dispatched[0]))
        self.assertEqual(calls, [("run", 0.2), ("run", 0.2)])

    def test_investigation_timeout_single_flight_and_exception_stay_unknown(self):
        h = Harness()
        release = Event()
        finished = Event()
        calls = []

        def stalled(_run, _budget):
            calls.append(1)
            release.wait(2)
            finished.set()
            return {"phase": "running", "process_evidence": [{
                "pid": 99, "alive": True, "stalled": True, "blocked_on": "io"}]}

        h.scheduler = WorkerReviewScheduler(
            automation_state=lambda: h.automation, active_run=lambda: h.run,
            worker_state=lambda _run: h.worker, collect_non_model=h.collect,
            investigate_non_model=stalled, investigation_timeout_seconds=0.01,
            dispatch_review=h.dispatch, user_priority=lambda _run: False,
            on_exit=h.exits.append, clock=lambda: h.now,
        )
        try:
            timed_out = h.tick(0)
            self.assertEqual(timed_out.hang_investigation["classification"], "unknown")
            self.assertEqual(timed_out.hang_investigation["investigation_status"], "timeout")
            concurrent = h.tick(60)
            self.assertEqual(concurrent.hang_investigation["classification"], "unknown")
            self.assertEqual(concurrent.hang_investigation["investigation_status"], "in_flight")
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

        def broken(_run, _budget):
            raise RuntimeError("PRIVATE_EXCEPTION")

        h.scheduler = WorkerReviewScheduler(
            automation_state=lambda: h.automation, active_run=lambda: h.run,
            worker_state=lambda _run: h.worker, collect_non_model=h.collect,
            investigate_non_model=broken, investigation_timeout_seconds=0.1,
            dispatch_review=h.dispatch, user_priority=lambda _run: False,
            on_exit=h.exits.append, clock=lambda: h.now,
        )
        failed = h.tick(120)
        self.assertEqual(failed.hang_investigation["classification"], "unknown")
        self.assertEqual(failed.hang_investigation["investigation_status"], "failed")
        self.assertNotIn("PRIVATE_EXCEPTION", repr(failed))

    def test_scheduler_usage_provenance_survives_dispatch_and_resets_for_new_identity(self):
        h = Harness()
        secret = "CW11_PRIVATE_USAGE"
        values = iter(({"kind": "observed", "value": 7, "private": secret},
                       {"kind": "estimated", "value": 3, "private": secret},
                       {"kind": "unknown", "value": None},
                       {"kind": "observed", "value": True}))
        h.scheduler = WorkerReviewScheduler(
            automation_state=lambda: h.automation, active_run=lambda: h.run,
            worker_state=lambda _run: h.worker, collect_non_model=h.collect,
            collect_usage=lambda _run: next(values),
            dispatch_review=h.dispatch, user_priority=lambda _run: False,
            on_exit=h.exits.append, clock=lambda: h.now,
        )
        first = h.tick(0)
        self.assertEqual(first.usage["tokens_observed"], 7)
        self.assertEqual(first.usage["tokens_estimated"], "unknown")
        second = h.tick(60)
        self.assertEqual(second.usage["tokens_observed"], 7)
        self.assertEqual(second.usage["tokens_estimated"], 3)
        self.assertEqual(h.dispatched[0].facts["usage"]["tokens_estimated"], 3)
        third = h.tick(120)
        self.assertEqual(third.usage["tokens_unknown"], "unknown")
        self.assertEqual(third.usage["unknown_samples"], 1)
        invalid = h.tick(180)
        self.assertEqual(invalid.usage["unknown_samples"], 2)
        self.assertNotIn(secret, repr(h.dispatched))
        h.run = run_ref(run_id="next-run")
        h.worker = ready(h.run)
        reset = h.tick(180)  # exhausted source is unknown, not zero
        self.assertEqual(reset.status, "waiting")
        self.assertEqual(reset.usage["tokens_observed"], "unknown")
        self.assertEqual(reset.usage["tokens_estimated"], "unknown")
        self.assertEqual(reset.usage["unknown_samples"], 1)

    def test_clock_edges_large_jump_identity_and_121st_review(self):
        h = Harness()
        self.assertEqual(h.tick(0).status, "waiting")
        self.assertEqual(h.tick(59.999).status, "waiting")
        self.assertEqual(len(h.dispatched), 0)
        self.assertEqual(h.tick(60).status, "dispatched")
        self.assertEqual(h.dispatched[0].due_at, 60)
        self.assertEqual(h.tick(60).status, "waiting")
        h.worker["idle"] = False
        delayed = h.tick(600)
        self.assertEqual(delayed.status, "delayed")
        self.assertEqual(delayed.coalesced_count, 8)
        self.assertEqual(delayed.next_due_at, 660)
        h.worker = ready(h.run)
        self.assertEqual(h.tick(600).status, "dispatched")
        self.assertEqual(h.dispatched[-1].due_at, 600)
        self.assertEqual(h.dispatched[-1].coalesced_count, 8)
        self.assertEqual(h.tick(540).status, "waiting")  # backwards time never duplicates
        for ordinal in range(3, 122):
            result = h.tick(600 + 60 * (ordinal - 2))
            self.assertEqual(result.status, "dispatched", ordinal)
        self.assertEqual(result.review_count, 121)
        self.assertEqual(len(h.dispatched), 121)
        h.run = run_ref(run_id="replacement", session_id="new-session")
        h.worker = ready(h.run)
        replaced = h.tick(h.now)
        self.assertEqual(replaced.status, "waiting")
        self.assertEqual(replaced.review_count, 0)
        self.assertEqual(h.dispatched[-1].run.run_id, "run")
        h.run = run_ref(active=False)
        self.assertEqual(h.tick(h.now + 60).status, "inactive")
        for bad in (True, -1, float("nan"), float("inf")):
            h.now = bad
            with self.subTest(clock=bad), self.assertRaises(ValueError):
                h.scheduler.tick()

    def test_busy_ambiguity_user_priority_and_unknown_delivery_do_not_replay(self):
        busy_cases = {
            "wrong_session": {"sessionId": "elsewhere"},
            "wrong_generation": {"generation": 2},
            "bool_generation": {"generation": True},
            "float_generation": {"generation": 1.0},
            "pending": {"pending": True},
            "approval": {"approvalPending": True},
            "editor_unknown": {"editorKnown": False},
            "editor_text": {"editorEmpty": False},
            "tool": {"inFlightToolCount": 1},
            "bool_tool_count": {"inFlightToolCount": False},
            "paused_worker": {"paused": True},
            "not_idle": {"idle": False},
        }
        for label, changes in busy_cases.items():
            with self.subTest(label=label):
                h = Harness()
                h.tick(0)
                h.worker.update(changes)
                self.assertEqual(h.tick(60).status, "delayed")
                self.assertEqual(h.dispatched, [])
                h.priority = True
                self.assertEqual(h.tick(180).reason, "user_priority")
                self.assertEqual(h.dispatched, [])
                h.priority = False
                h.worker = ready(h.run)
                self.assertEqual(h.tick(180).status, "dispatched")
                self.assertEqual(len(h.dispatched), 1)
                self.assertEqual(h.dispatched[0].due_at, 180)
                self.assertEqual(h.dispatched[0].coalesced_count, 2)
        for delivery in ({"status": "unknown"}, None, RuntimeError("send outcome unknown")):
            with self.subTest(delivery=delivery):
                h = Harness()
                h.delivery = delivery
                h.tick(0)
                outcome = h.tick(60)
                self.assertEqual(outcome.status, "unknown")
                self.assertFalse(outcome.pending)
                self.assertEqual(h.tick(60).status, "waiting")
                self.assertEqual(len(h.dispatched), 1)
        h = Harness()
        h.delivery = {"status": "deferred"}
        h.tick(0)
        self.assertEqual(h.tick(60).status, "delayed")
        self.assertEqual(h.tick(120).coalesced_count, 1)
        h.delivery = {"status": "omp_processed"}
        self.assertEqual(h.tick(120).status, "dispatched")
        self.assertEqual(len(h.dispatched), 3)
        self.assertEqual(h.dispatched[-1].due_at, 120)

    def test_pause_authority_revocation_and_exit_keep_observation_without_model_work(self):
        h = Harness()
        h.tick(0)
        h.automation = authority(paused=True)
        self.assertEqual(h.tick(60).status, "paused")
        self.assertEqual(h.tick(180).status, "paused")
        self.assertEqual(len(h.collected), 3)
        self.assertEqual(h.dispatched, [])
        for changes in ({"cancelled": True}, {"approvalValid": False},
                        {"metadataHealthy": False}):
            h.automation = authority(**changes)
            result = h.tick(h.now)
            self.assertEqual(result.status, "held")
            self.assertFalse(result.pending)
            self.assertEqual(h.dispatched, [])
        h.automation = authority()
        self.assertEqual(h.tick(180).status, "waiting")
        source = h.scheduler.bind_exit_source(h.run)
        self.assertFalse(h.scheduler.notify_exit_event({
            "run_id": "run", "exit_confirmed": True, "exit_status": True}))
        self.assertFalse(h.scheduler.notify_exit_event({
            "run_id": "run", "exit_confirmed": True, "exit_status": 9}))
        self.assertTrue(h.scheduler.notify_exit_event({
            **exit_identity(h.run), "exit_confirmed": True, "exit_status": 9,
            "event_sequence": 3, "private": "PRIVATE_MODEL"}, source=source, run=h.run))
        self.assertFalse(h.scheduler.notify_exit_event({
            "run_id": "run", "exit_confirmed": True, "exit_status": 9}, run=h.run))
        self.assertEqual(len(h.exits), 1)
        self.assertEqual(dict(h.exits[0]), {
            **exit_identity(h.run), "exit_confirmed": True,
            "exit_status": 9, "event_sequence": 3,
            "run_incarnation": h.scheduler.admission.run_incarnation(),
        })
        h.facts = {"exit_confirmed": True, "exit_status": 9, "phase": "exited"}
        self.assertEqual(h.tick(240).status, "exited")
        self.assertEqual(len(h.exits), 1)
        self.assertEqual(h.dispatched, [])

    def test_hang_requires_process_and_lifecycle_evidence_and_usage_never_conflates(self):
        private = "PRIVATE_MODEL_THOUGHT"
        process = {"pid": 73, "alive": True, "stalled": True,
                   "blocked_on": "io", "state": "D", "private": private}
        lifecycle = {"phase": "running", "exit_confirmed": False, "private": private}
        for changed_process, changed_lifecycle in (
            ([], lifecycle),
            ([{**process, "stalled": False}], lifecycle),
            ([{**process, "alive": False}], lifecycle),
            ([{**process, "blocked_on": "unknown"}], lifecycle),
            ([process], {**lifecycle, "phase": "unknown"}),
            ([{**process, "stalled": 1}], lifecycle),
        ):
            with self.subTest(process=changed_process, lifecycle=changed_lifecycle):
                report = investigate_hang("run", process_evidence=changed_process,
                                          lifecycle_evidence=changed_lifecycle,
                                          silence_seconds=1_000_000,
                                          unknowns=["safe", private + " private"])
                self.assertEqual(report["classification"], "unknown")
                self.assertNotIn(private, repr(report))
        suspected = investigate_hang("run", process_evidence=[process],
                                     lifecycle_evidence=lifecycle, silence_seconds=0)
        self.assertEqual(suspected["classification"], "suspected_hang")
        self.assertNotIn(private, repr(suspected))
        self.assertEqual(investigate_hang("run", process_evidence=[process],
                         lifecycle_evidence={"phase": "exited", "exit_confirmed": True,
                                             "lifetime_ended": True})["classification"],
                         "exit_observed")
        ledger = UsageLedger()
        self.assertEqual(ledger.snapshot().as_dict()["tokens_observed"], "unknown")
        for _ in range(121):
            ledger.record(UsageValue(2**64, "observed"))
        for _ in range(13):
            ledger.record(UsageValue(3, "estimated"))
        ledger.record(UsageValue(None, "unknown"))
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.observed, 121 * 2**64)
        self.assertEqual(snapshot.estimated, 39)
        self.assertEqual(snapshot.unknown_samples, 1)
        self.assertEqual(snapshot.as_dict()["tokens_unknown"], "unknown")
        for value, kind in ((True, "observed"), (0, "unknown")):
            with self.assertRaises(ValueError):
                UsageValue(value, kind)


if __name__ == "__main__":
    unittest.main()
