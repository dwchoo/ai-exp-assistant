"""Linearization tests for the CW-11 review admission owner."""
from __future__ import annotations

from threading import Event, Thread
import unittest

from workbench.observation.worker_review import (
    ActiveRunRef, ReviewAdmissionFence, ReviewRequest,
    SerializedReviewAdmission, WorkerReviewScheduler,
)


RUN = ActiveRunRef("task", "revision", 1, "run", "session", 1)
READY = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True,
}}


def state(**changes):
    return {"portVersion": 2, "kind": "AutomationState", "payload": {
        **READY["payload"], **changes,
    }}


def request(run=RUN):
    return ReviewRequest(run, 60.0, 0, {})


class AdmissionTests(unittest.TestCase):
    def test_fence_binding_nonce_and_incarnation_reject_a_b_a_review(self):
        owner = SerializedReviewAdmission(RUN, READY)
        fence = ReviewAdmissionFence()
        owner.install_authority_fence(fence)
        first, uninterrupted = fence.bind(RUN, owner.run_incarnation(), fence.token())
        self.assertTrue(uninterrupted)
        self.assertIsNotNone(fence.open(first))
        old_ticket = owner.issue_review(RUN, 60.0, 0, {})
        other = ActiveRunRef("other-task", "revision", 1, "other-run", "session", 1)
        second, _ = fence.bind(other, owner.run_incarnation(), fence.token())
        self.assertIsNotNone(fence.open(second))
        sent = []
        self.assertEqual(owner.admit_and_dispatch(
            old_ticket, dispatch_review=sent.append).status, "paused")
        self.assertIsNone(fence.open(first), "old A token reopened B")
        third, _ = fence.bind(RUN, owner.run_incarnation(), fence.token())
        self.assertIsNotNone(fence.open(third))
        self.assertIsNone(fence.open(first), "old A token reopened new A")
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        new_ticket = owner.issue_review(RUN, 61.0, 0, {})
        self.assertEqual(owner.admit_and_dispatch(
            new_ticket, dispatch_review=sent.append).status, "paused")
        self.assertEqual(sent, [])

    def test_late_deferred_completion_after_close_is_consumed_not_replayed(self):
        owner = SerializedReviewAdmission(RUN, READY)
        fence = ReviewAdmissionFence()
        owner.install_authority_fence(fence)
        bound, _ = fence.bind(RUN, owner.run_incarnation(), fence.token())
        self.assertIsNotNone(fence.open(bound))
        ticket = owner.issue_review(RUN, 60.0, 0, {})
        entered, release = Event(), Event()
        results = []

        def dispatch(_request):
            entered.set()
            self.assertTrue(release.wait(2))
            return {"status": "deferred"}

        worker = Thread(target=lambda: results.append(owner.admit_and_dispatch(
            ticket, dispatch_review=dispatch).status))
        worker.start()
        self.assertTrue(entered.wait(1))
        fence.close()
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, ["deferred"])
        owner.set_automation_state(READY)
        self.assertIsNotNone(fence.open(fence.token()))
        replacement = owner.issue_review(RUN, 60.0, 0, {})
        self.assertEqual(owner.admit_and_dispatch(
            replacement, dispatch_review=lambda request: {"status": "omp_processed"}
        ).status, "consumed")

    def test_run_change_during_ticket_issuance_discards_old_due(self):
        class ReplacingOwner(SerializedReviewAdmission):
            def __init__(self):
                super().__init__(RUN, READY)
                self.replace_once = True

            def issue_review(self, run, due_at, coalesced_count, facts):
                if self.replace_once:
                    self.replace_once = False
                    self.set_active_run(None)
                    self.set_active_run(RUN)
                return super().issue_review(run, due_at, coalesced_count, facts)

        owner = ReplacingOwner()
        now = [0.0]
        delivered = []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: READY, active_run=lambda: RUN,
            worker_state=lambda _: {"role": "worker", "sessionId": RUN.session_id,
                                    "generation": RUN.session_generation, "idle": True,
                                    "pending": False, "approvalPending": False,
                                    "editorKnown": True, "editorEmpty": True,
                                    "inFlightToolCount": 0, "paused": False},
            collect_non_model=lambda _: {},
            dispatch_review=lambda item: (delivered.append(item),
                                          {"status": "omp_processed"})[1],
            user_priority=lambda _: False, on_exit=lambda _: None, clock=lambda: now[0],
        )
        self.assertEqual(scheduler.tick().next_due_at, 60.0)
        now[0] = 60.0
        changed = scheduler.tick()
        self.assertEqual(changed.status, "waiting")
        self.assertEqual(changed.next_due_at, 120.0)
        self.assertEqual(delivered, [])
        now[0] = 119.999
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 120.0
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)

    def test_review_count_resets_with_new_incarnation(self):
        owner = SerializedReviewAdmission(RUN, READY)
        now = [0.0]
        delivered = []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: READY, active_run=lambda: RUN,
            worker_state=lambda _: {"role": "worker", "sessionId": RUN.session_id,
                                    "generation": RUN.session_generation, "idle": True,
                                    "pending": False, "approvalPending": False,
                                    "editorKnown": True, "editorEmpty": True,
                                    "inFlightToolCount": 0, "paused": False},
            collect_non_model=lambda _: {},
            dispatch_review=lambda item: (delivered.append(item),
                                          {"status": "omp_processed"})[1],
            user_priority=lambda _: False, on_exit=lambda _: None, clock=lambda: now[0],
        )
        scheduler.tick()
        now[0] = 60.0
        self.assertEqual(scheduler.tick().review_count, 1)
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        now[0] = 60.1
        restarted = scheduler.tick()
        self.assertEqual(restarted.status, "waiting")
        self.assertEqual(restarted.review_count, 0)
        self.assertEqual(restarted.next_due_at, 120.1)
        now[0] = 120.1
        self.assertEqual(scheduler.tick().review_count, 1)
        self.assertEqual(len(delivered), 2)

    def test_same_identity_new_incarnation_starts_its_own_sixty_second_baseline(self):
        owner = SerializedReviewAdmission(RUN, READY)
        now = [0.0]
        automation = [READY]
        delivered = []
        ready = {"role": "worker", "sessionId": RUN.session_id,
                 "generation": RUN.session_generation, "idle": True,
                 "pending": False, "approvalPending": False, "editorKnown": True,
                 "editorEmpty": True, "inFlightToolCount": 0, "paused": False}
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: automation[0],
            active_run=lambda: RUN, worker_state=lambda _: ready,
            collect_non_model=lambda _: {"phase": "running"},
            dispatch_review=lambda item: (delivered.append(item),
                                          {"status": "omp_processed"})[1],
            user_priority=lambda _: False, on_exit=lambda _: None,
            clock=lambda: now[0],
        )
        self.assertEqual(scheduler.tick().next_due_at, 60.0)
        now[0] = 60.0
        automation[0] = state(paused=True)
        owner.set_automation_state(automation[0])
        self.assertEqual(scheduler.tick().status, "paused")
        first_incarnation = owner.run_incarnation()
        now[0] = 61.0
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        automation[0] = READY
        owner.set_automation_state(READY)
        self.assertEqual(owner.run_incarnation(), first_incarnation + 2)
        observed = scheduler.tick()
        self.assertEqual(observed.status, "waiting")
        self.assertEqual(observed.next_due_at, 121.0)
        self.assertFalse(observed.pending)
        now[0] = 120.999
        self.assertEqual(scheduler.tick().status, "waiting")
        self.assertEqual(delivered, [])
        now[0] = 121.0
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].due_at, 121.0)
        now[0] = 121.001
        self.assertEqual(scheduler.tick().status, "waiting")
        self.assertEqual(len(delivered), 1)

    def test_idempotent_run_observation_and_authority_only_pause_preserve_cadence(self):
        owner = SerializedReviewAdmission(RUN, READY)
        initial_epoch = owner.snapshot()
        incarnation = owner.run_incarnation()
        self.assertEqual(owner.set_active_run(RUN), initial_epoch)
        self.assertEqual(owner.run_incarnation(), incarnation)
        owner.set_automation_state(state(paused=True))
        owner.set_automation_state(READY)
        self.assertEqual(owner.run_incarnation(), incarnation)
        now = [0.0]
        delivered = []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: READY, active_run=lambda: RUN,
            worker_state=lambda _: {"role": "worker", "sessionId": RUN.session_id,
                                    "generation": RUN.session_generation, "idle": True,
                                    "pending": False, "approvalPending": False,
                                    "editorKnown": True, "editorEmpty": True,
                                    "inFlightToolCount": 0, "paused": False},
            collect_non_model=lambda _: {},
            dispatch_review=lambda item: (delivered.append(item),
                                          {"status": "omp_processed"})[1],
            user_priority=lambda _: False, on_exit=lambda _: None, clock=lambda: now[0],
        )
        self.assertEqual(scheduler.tick().next_due_at, 60.0)
        for moment in (30.0, 59.999):
            owner.set_active_run(RUN)
            now[0] = moment
            self.assertEqual(scheduler.tick().next_due_at, 60.0)
        now[0] = 60.0
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)

    def test_coalescing_invalidates_older_owner_ticket(self):
        owner = SerializedReviewAdmission(RUN, READY)
        now = [0.0]
        worker = {"role": "worker", "sessionId": RUN.session_id,
                  "generation": RUN.session_generation, "idle": False,
                  "pending": False, "approvalPending": False, "editorKnown": True,
                  "editorEmpty": True, "inFlightToolCount": 0, "paused": False}
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: READY, active_run=lambda: RUN,
            worker_state=lambda _: worker, collect_non_model=lambda _: {},
            dispatch_review=lambda _: self.fail("busy worker received review"),
            user_priority=lambda _: False, on_exit=lambda _: None, clock=lambda: now[0],
        )
        scheduler.tick()
        now[0] = 60
        self.assertEqual(scheduler.tick().status, "delayed")
        old = scheduler._pending_ticket
        now[0] = 120
        self.assertEqual(scheduler.tick().status, "delayed")
        self.assertFalse(owner.ticket_current(old))
        self.assertEqual(owner.admit_and_dispatch(
            old, dispatch_review=lambda _: self.fail("old ticket admitted")).status,
            "stale")
        self.assertTrue(owner.ticket_current(scheduler._pending_ticket))

    def test_scheduler_keeps_pause_latest_but_not_aba_stale_ticket(self):
        ready = {"role": "worker", "sessionId": RUN.session_id,
                 "generation": RUN.session_generation, "idle": True,
                 "pending": False, "approvalPending": False, "editorKnown": True,
                 "editorEmpty": True, "inFlightToolCount": 0, "paused": False}
        for scenario in ("pause_resume", "aba"):
            with self.subTest(scenario=scenario):
                owner = SerializedReviewAdmission(RUN, READY)
                now = [0.0]
                authority = [READY]
                busy = [scenario == "aba"]
                sent = []
                scheduler = WorkerReviewScheduler(
                    admission=owner, automation_state=lambda: authority[0],
                    active_run=lambda: RUN,
                    worker_state=lambda _: {**ready, "idle": not busy[0]},
                    collect_non_model=lambda _: {"phase": "running"},
                    dispatch_review=lambda item: (sent.append(item),
                                                  {"status": "omp_processed"})[1],
                    user_priority=lambda _: False, on_exit=lambda _: None,
                    clock=lambda: now[0],
                )
                self.assertEqual(scheduler.tick().status, "waiting")
                now[0] = 60
                if scenario == "pause_resume":
                    authority[0] = state(paused=True)
                    owner.set_automation_state(authority[0])
                    self.assertEqual(scheduler.tick().status, "paused")
                    authority[0] = READY
                    owner.set_automation_state(READY)
                    now[0] = 61
                    self.assertEqual(scheduler.tick().status, "dispatched")
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(sent[0].due_at, 60)
                else:
                    self.assertEqual(scheduler.tick().status, "delayed")
                    owner.set_active_run(None)
                    owner.set_active_run(RUN)
                    busy[0] = False
                    self.assertEqual(scheduler.tick().status, "waiting")
                    self.assertEqual(sent, [])

    def test_scheduler_uses_owner_writes_after_readiness_callback(self):
        ready = {"role": "worker", "sessionId": RUN.session_id,
                 "generation": RUN.session_generation, "idle": True,
                 "pending": False, "approvalPending": False, "editorKnown": True,
                 "editorEmpty": True, "inFlightToolCount": 0, "paused": False}
        for update in (
            lambda owner: owner.set_automation_state(state(paused=True)),
            lambda owner: owner.set_automation_state(state(cancelled=True)),
            lambda owner: owner.set_automation_state(state(approvalValid=False)),
            lambda owner: owner.set_active_run(ActiveRunRef("task", "revision", 1,
                                                            "run", "replacement", 2)),
        ):
            with self.subTest(update=update):
                owner = SerializedReviewAdmission(RUN, READY)
                now, delivered = [0.0], []

                def readiness(_run):
                    if now[0] == 60:
                        update(owner)
                    return ready

                scheduler = WorkerReviewScheduler(
                    admission=owner, automation_state=lambda: READY,
                    active_run=lambda: RUN, worker_state=readiness,
                    collect_non_model=lambda _: {"phase": "running"},
                    dispatch_review=lambda request: delivered.append(request),
                    user_priority=lambda _: False, on_exit=lambda _: None,
                    clock=lambda: now[0],
                )
                self.assertEqual(scheduler.tick().status, "waiting")
                now[0] = 60
                self.assertIn(scheduler.tick().status, {"paused", "held"})
                self.assertEqual(delivered, [])

    def test_writer_wins_pause_cancel_revoke_replacement_and_aba(self):
        for update in (
            lambda owner: owner.set_automation_state(state(paused=True)),
            lambda owner: owner.set_automation_state(state(cancelled=True)),
            lambda owner: owner.set_automation_state(state(approvalValid=False)),
            lambda owner: owner.set_automation_state(state(metadataHealthy=False)),
            lambda owner: owner.set_active_run(ActiveRunRef("task", "revision", 1, "run", "next", 2)),
            lambda owner: (owner.set_active_run(None), owner.set_active_run(RUN)),
        ):
            with self.subTest(update=update):
                owner = SerializedReviewAdmission(RUN, READY)
                ticket = owner.issue_review(RUN, 60.0, 0, {})
                updated = Event()
                writer = Thread(target=lambda: (update(owner), updated.set()))
                writer.start()
                self.assertTrue(updated.wait(1))
                writer.join(1)
                delivered = []
                outcome = owner.admit_and_dispatch(ticket,
                                                   dispatch_review=delivered.append)
                self.assertEqual(outcome.status, "stale")
                self.assertEqual(delivered, [])

    def test_dispatch_wins_but_writer_progresses_before_submission_returns(self):
        owner = SerializedReviewAdmission(RUN, READY)
        ticket = owner.issue_review(RUN, 60.0, 0, {})
        entered, release, changed = Event(), Event(), Event()
        results = []

        def dispatch(_request):
            entered.set()
            self.assertTrue(release.wait(2))
            return {"status": "omp_processed"}

        submit = Thread(target=lambda: results.append(owner.admit_and_dispatch(
            ticket, dispatch_review=dispatch)))
        submit.start()
        self.assertTrue(entered.wait(1))
        writer = Thread(target=lambda: (owner.set_automation_state(state(paused=True)), changed.set()))
        writer.start()
        self.assertTrue(changed.wait(1), "authority writer waited for external action")
        release.set()
        submit.join(1)
        writer.join(1)
        self.assertFalse(submit.is_alive())
        self.assertTrue(changed.is_set())
        self.assertEqual([item.status for item in results], ["omp_processed"])

    def test_reentrant_concurrent_and_unknown_delivery_are_not_replayed(self):
        owner = SerializedReviewAdmission(RUN, READY)
        ticket = owner.issue_review(RUN, 60.0, 0, {})
        entered, release = Event(), Event()
        calls, outcomes, nested = [], [], []

        def dispatch(_request):
            calls.append(1)
            nested.append(owner.admit_and_dispatch(ticket,
                                                   dispatch_review=lambda _: None).status)
            entered.set()
            release.wait(1)
            return {"status": "unknown"}

        first = Thread(target=lambda: outcomes.append(owner.admit_and_dispatch(
            ticket, dispatch_review=dispatch).status))
        first.start()
        self.assertTrue(entered.wait(1))
        second = Thread(target=lambda: outcomes.append(owner.admit_and_dispatch(
            ticket, dispatch_review=dispatch).status))
        second.start()
        release.set()
        first.join(1)
        second.join(1)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(calls, [1])
        self.assertEqual(nested, ["reentrant"])
        self.assertCountEqual(outcomes, ["unknown", "reentrant"])

    def test_ticket_forgery_other_owner_and_legacy_pairing_fail_closed(self):
        from workbench.observation.worker_review import ReviewTicket

        owner = SerializedReviewAdmission(RUN, READY)
        other = SerializedReviewAdmission(RUN, READY)
        ticket = owner.issue_review(RUN, 60.0, 0, {})
        forged = ReviewTicket(ticket.request, ticket._nonce, ticket._run_incarnation)
        calls = []
        for candidate, target in ((forged, owner), (ticket, other)):
            self.assertEqual(target.admit_and_dispatch(
                candidate, dispatch_review=calls.append).status, "stale")
        self.assertEqual(owner.admit_and_dispatch(
            ticket.request, observed_epoch=owner.snapshot(),
            dispatch_review=calls.append).status, "stale")
        self.assertEqual(calls, [])
        self.assertEqual(owner.admit_and_dispatch(
            ticket, dispatch_review=lambda item: (calls.append(item),
                                                  {"status": "omp_processed"})[1]).status,
            "omp_processed")
        self.assertEqual(owner.admit_and_dispatch(
            ticket, dispatch_review=calls.append).status, "stale")
        self.assertEqual(len(calls), 1)
        tampered = owner.issue_review(RUN, 120.0, 0, {})
        object.__setattr__(tampered, "request", request())
        self.assertEqual(owner.admit_and_dispatch(
            tampered, dispatch_review=calls.append).status, "stale")
        self.assertEqual(len(calls), 1)

    def test_exit_fence_precedes_blocking_or_failed_display(self):
        for raise_display in (False, True):
            with self.subTest(raise_display=raise_display):
                owner = SerializedReviewAdmission(RUN, READY)
                ticket = owner.issue_review(RUN, 60.0, 0, {})
                entered, release = Event(), Event()
                shown = []

                def on_exit(_event):
                    entered.set()
                    release.wait(1)
                    if raise_display and not shown:
                        shown.append("failed")
                        raise RuntimeError("display failed")
                    shown.append("shown")

                scheduler = WorkerReviewScheduler(
                    admission=owner, automation_state=lambda: READY, active_run=lambda: RUN,
                    worker_state=lambda _: None, collect_non_model=lambda _: {},
                    dispatch_review=lambda _: {"status": "omp_processed"},
                    user_priority=lambda _: False, on_exit=on_exit, clock=lambda: 0,
                )
                event = {"task_id": RUN.task_id, "revision_id": RUN.revision_id,
                         "revision": RUN.revision, "run_id": RUN.run_id,
                         "session_id": RUN.session_id,
                         "session_generation": RUN.session_generation,
                         "exit_confirmed": True, "exit_status": 0}
                source = scheduler.bind_exit_source(RUN)
                thread = Thread(target=lambda: scheduler.notify_exit_event(event, source=source, run=RUN))
                thread.start()
                self.assertTrue(entered.wait(1))
                self.assertEqual(owner.admit_and_dispatch(
                    ticket, dispatch_review=lambda _: self.fail("exit admitted a review")).status,
                    "stale")
                release.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                if raise_display:
                    self.assertTrue(scheduler.notify_exit_event(event, source=source, run=RUN))
                    self.assertEqual(shown, ["failed", "shown"])
                else:
                    self.assertEqual(shown, ["shown"])
