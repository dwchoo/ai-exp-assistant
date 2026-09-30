"""Adversarial schedules for the CW-11 admission boundary."""
from __future__ import annotations

from threading import Event, Thread
import unittest

from workbench.observation.worker_review import (
    ActiveRunRef, ReviewRequest, ReviewTicket, SerializedReviewAdmission,
    WorkerReviewScheduler,
)


RUN = ActiveRunRef("task", "revision", 1, "run", "session", 1)
AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True,
    "approvalValid": True,
}}


def authority(**changes):
    return {"portVersion": 2, "kind": "AutomationState",
            "payload": {**AUTOMATION["payload"], **changes}}


def request():
    return ReviewRequest(RUN, 60.0, 0, {})


def ready_worker():
    return {"role": "worker", "sessionId": RUN.session_id,
            "generation": RUN.session_generation, "idle": True,
            "pending": False, "approvalPending": False,
            "editorKnown": True, "editorEmpty": True,
            "inFlightToolCount": 0, "paused": False}


class IndependentAdmissionTests(unittest.TestCase):
    def test_late_review_ack_remains_historical_after_authority_changes(self):
        writers = {
            "pause": lambda owner: owner.set_automation_state(authority(paused=True)),
            "replace": lambda owner: owner.set_active_run(None),
            "exit": lambda owner: owner.fence_exit(owner.bind_exit_source(RUN)),
        }
        for name, write in writers.items():
            with self.subTest(writer=name):
                owner = SerializedReviewAdmission(RUN, AUTOMATION)
                ticket = owner.issue_review(RUN, 60.0, 0, {})
                entered, release = Event(), Event()
                results = []

                def dispatch(_request):
                    entered.set()
                    self.assertTrue(release.wait(2))
                    return {"status": "omp_processed"}

                thread = Thread(target=lambda: results.append(
                    owner.admit_and_dispatch(ticket, dispatch_review=dispatch)))
                thread.start()
                self.assertTrue(entered.wait(2))
                write(owner)
                release.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0].status, "omp_processed")
                self.assertFalse(results[0].current)

    def test_late_review_ack_does_not_increment_current_tick_review_count(self):
        writers = {
            "pause": lambda owner, state: (
                state.__setitem__(0, authority(paused=True)),
                owner.set_automation_state(state[0])),
            "rebind": lambda owner, _state: (
                owner.set_active_run(None), owner.set_active_run(RUN)),
            "exit": lambda owner, _state: owner.fence_exit(owner.bind_exit_source(RUN)),
        }
        for name, write in writers.items():
            with self.subTest(writer=name):
                owner = SerializedReviewAdmission(RUN, AUTOMATION)
                now = [0.0]
                state = [AUTOMATION]
                entered, release = Event(), Event()
                results = []

                def dispatch(_request):
                    entered.set()
                    self.assertTrue(release.wait(2))
                    return {"status": "omp_processed"}

                scheduler = WorkerReviewScheduler(
                    admission=owner, automation_state=lambda: state[0],
                    active_run=lambda: RUN, worker_state=lambda _: ready_worker(),
                    collect_non_model=lambda _: {}, dispatch_review=dispatch,
                    user_priority=lambda _: False, on_exit=lambda _: None,
                    clock=lambda: now[0],
                )
                self.assertEqual(scheduler.tick().status, "waiting")
                now[0] = 60.0
                thread = Thread(target=lambda: results.append(scheduler.tick()))
                thread.start()
                self.assertTrue(entered.wait(2))
                write(owner, state)
                release.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0].status, "held")
                self.assertEqual(results[0].reason, "admission_authority_changed")
                self.assertFalse(results[0].dispatched)
                self.assertEqual(results[0].review_count, 0)

    def test_scheduler_coalescing_discards_superseded_ticket(self):
        owner = SerializedReviewAdmission(RUN, AUTOMATION)
        now = [0.0]
        idle = [False]
        delivered = []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: AUTOMATION,
            active_run=lambda: RUN,
            worker_state=lambda _: {**ready_worker(), "idle": idle[0]},
            collect_non_model=lambda _: {"phase": "running"},
            dispatch_review=lambda item: (delivered.append(item),
                                          {"status": "omp_processed"})[1],
            user_priority=lambda _: False, on_exit=lambda _: None,
            clock=lambda: now[0],
        )
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 60.0
        self.assertEqual(scheduler.tick().status, "delayed")
        first = scheduler._pending_ticket
        now[0] = 120.0
        self.assertEqual(scheduler.tick().status, "delayed")
        self.assertFalse(owner.ticket_current(first))
        self.assertEqual(owner.admit_and_dispatch(
            first, dispatch_review=lambda item: delivered.append(item)).status,
            "stale")
        idle[0] = True
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].due_at, 120.0)
        self.assertEqual(delivered[0].coalesced_count, 1)

    def test_pause_held_review_resumes_in_same_run_incarnation(self):
        owner = SerializedReviewAdmission(RUN, AUTOMATION)
        now = [0.0]
        state = [AUTOMATION]
        delivered = []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: state[0],
            active_run=lambda: RUN, worker_state=lambda _: ready_worker(),
            collect_non_model=lambda _: {"phase": "running"},
            dispatch_review=lambda item: (delivered.append(item),
                                          {"status": "omp_processed"})[1],
            user_priority=lambda _: False, on_exit=lambda _: None,
            clock=lambda: now[0],
        )
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 60.0
        state[0] = authority(paused=True)
        owner.set_automation_state(state[0])
        self.assertEqual(scheduler.tick().status, "paused")
        state[0] = AUTOMATION
        owner.set_automation_state(state[0])
        now[0] = 61.0
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].due_at, 60.0)

    def test_owner_ticket_cannot_be_forged_reused_or_repaired_with_an_epoch(self):
        owner = SerializedReviewAdmission(RUN, AUTOMATION)
        foreign = SerializedReviewAdmission(RUN, AUTOMATION)
        ticket = owner.issue_review(RUN, 60.0, 0, {})
        delivered = []
        forged = ReviewTicket(ticket.request, ticket._nonce, ticket._run_incarnation)
        for target, presented in ((owner, forged), (foreign, ticket)):
            self.assertEqual(target.admit_and_dispatch(
                presented, dispatch_review=lambda item: delivered.append(item)).status,
                "stale")
        self.assertEqual(owner.admit_and_dispatch(
            ticket.request, observed_epoch=owner.snapshot(),
            dispatch_review=lambda item: delivered.append(item)).status, "stale")
        self.assertEqual(delivered, [])

        owner.discard_ticket(ticket)
        self.assertEqual(owner.admit_and_dispatch(
            ticket, dispatch_review=lambda item: delivered.append(item)).status, "stale")
        ticket = owner.issue_review(RUN, 60.0, 0, {})
        owner.set_automation_state(AUTOMATION)
        self.assertEqual(owner.admit_and_dispatch(
            ticket, dispatch_review=lambda item: delivered.append(item)).status, "stale")
        fresh = owner.issue_review(RUN, 60.0, 0, {})
        self.assertEqual(owner.admit_and_dispatch(
            fresh, dispatch_review=lambda item: (delivered.append(item),
                                                 {"status": "omp_processed"})[1]).status,
            "omp_processed")
        self.assertEqual(owner.admit_and_dispatch(
            fresh, dispatch_review=lambda item: delivered.append(item)).status, "stale")
        self.assertEqual(len(delivered), 1)

    def test_pause_held_due_review_does_not_cross_same_identity_new_incarnation(self):
        owner = SerializedReviewAdmission(RUN, AUTOMATION)
        now = [0.0]
        state = [AUTOMATION]
        delivered = []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: state[0],
            active_run=lambda: RUN, worker_state=lambda _: ready_worker(),
            collect_non_model=lambda _: {"phase": "running"},
            dispatch_review=lambda item: (delivered.append(item),
                                          {"status": "omp_processed"})[1],
            user_priority=lambda _: False, on_exit=lambda _: None,
            clock=lambda: now[0],
        )
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 60.0
        state[0] = authority(paused=True)
        owner.set_automation_state(state[0])
        self.assertEqual(scheduler.tick().status, "paused")
        held = scheduler._pending_ticket
        self.assertTrue(owner.ticket_current(held))

        # The owner changes run incarnation at t=61, even though the six
        # public identity fields remain equal. The next tick is only 59s later.
        now[0] = 61.0
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        state[0] = AUTOMATION
        owner.set_automation_state(state[0])
        now[0] = 120.0
        result = scheduler.tick()
        self.assertFalse(owner.ticket_current(held))
        self.assertNotEqual(result.status, "dispatched")
        self.assertEqual(delivered, [])
        self.assertFalse(result.pending)
        self.assertEqual(result.review_count, 0)
        self.assertEqual(result.next_due_at, 180.0)
        now[0] = 179.999
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 180.0
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].due_at, 180.0)
        self.assertEqual(delivered[0].coalesced_count, 0)

    def test_new_incarnation_observed_immediately_has_exact_new_boundary(self):
        owner = SerializedReviewAdmission(RUN, AUTOMATION)
        now = [0.0]
        delivered = []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: AUTOMATION,
            active_run=lambda: RUN, worker_state=lambda _: ready_worker(),
            collect_non_model=lambda _: {"phase": "running"},
            dispatch_review=lambda item: (delivered.append(item),
                                          {"status": "omp_processed"})[1],
            user_priority=lambda _: False, on_exit=lambda _: None,
            clock=lambda: now[0],
        )
        self.assertEqual(scheduler.tick().next_due_at, 60.0)
        now[0] = 60.0
        self.assertEqual(scheduler.tick().review_count, 1)
        first_ticket = owner.issue_review(RUN, 120.0, 0, {})
        now[0] = 61.0
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        restarted = scheduler.tick()
        self.assertEqual(restarted.status, "waiting")
        self.assertEqual(restarted.next_due_at, 121.0)
        self.assertEqual(restarted.review_count, 0)
        self.assertFalse(restarted.pending)
        self.assertFalse(owner.ticket_current(first_ticket))
        now[0] = 120.999
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 121.0
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 2)
        self.assertEqual(delivered[-1].due_at, 121.0)
        self.assertEqual(delivered[-1].coalesced_count, 0)

    def test_idempotent_active_run_observation_preserves_pending_ticket(self):
        owner = SerializedReviewAdmission(RUN, AUTOMATION)
        ticket = owner.issue_review(RUN, 60.0, 0, {})
        epoch, incarnation = owner.snapshot(), owner.run_incarnation()
        self.assertEqual(owner.set_active_run(RUN), epoch)
        self.assertEqual(owner.run_incarnation(), incarnation)
        self.assertTrue(owner.ticket_current(ticket))
        delivered = []
        self.assertEqual(owner.admit_and_dispatch(
            ticket, dispatch_review=lambda item: (delivered.append(item),
                                                  {"status": "omp_processed"})[1]).status,
            "omp_processed")
        self.assertEqual(len(delivered), 1)

    def test_scheduler_has_no_unchecked_dispatch_when_owner_denies(self):
        with self.assertRaises(TypeError):
            WorkerReviewScheduler(
                admission=None, automation_state=lambda: AUTOMATION,
                active_run=lambda: RUN, worker_state=lambda _: ready_worker(),
                collect_non_model=lambda _: {}, dispatch_review=lambda _: None,
                user_priority=lambda _: False, on_exit=lambda _: None, clock=lambda: 0,
            )
        owner = SerializedReviewAdmission(RUN, authority(paused=True))
        clock = [0.0]
        delivered = []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: AUTOMATION,
            active_run=lambda: RUN, worker_state=lambda _: ready_worker(),
            collect_non_model=lambda _: {},
            dispatch_review=lambda item: delivered.append(item),
            user_priority=lambda _: False, on_exit=lambda _: None,
            clock=lambda: clock[0],
        )
        self.assertEqual(scheduler.tick().status, "waiting")
        clock[0] = 60.0
        self.assertIn(scheduler.tick().status, {"held", "paused"})
        self.assertEqual(delivered, [])

    def test_concurrent_and_reentrant_ticks_submit_once(self):
        owner = SerializedReviewAdmission(RUN, AUTOMATION)
        clock = [0.0]
        entered, release = Event(), Event()
        delivered, nested, results = [], [], []
        scheduler = None

        def dispatch(item):
            delivered.append(item)
            nested.append(scheduler.tick().status)
            entered.set()
            self.assertTrue(release.wait(2))
            return {"status": "omp_processed"}

        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: AUTOMATION,
            active_run=lambda: RUN, worker_state=lambda _: ready_worker(),
            collect_non_model=lambda _: {}, dispatch_review=dispatch,
            user_priority=lambda _: False, on_exit=lambda _: None,
            clock=lambda: clock[0],
        )
        self.assertEqual(scheduler.tick().status, "waiting")
        clock[0] = 60.0
        first = Thread(target=lambda: results.append(scheduler.tick().status))
        first.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(scheduler.tick().status, "busy")
        release.set()
        first.join(2)
        self.assertFalse(first.is_alive())
        self.assertEqual(nested, ["busy"])
        self.assertEqual(results, ["dispatched"])
        self.assertEqual(len(delivered), 1)

    def test_run_replacement_while_ticket_is_issued_resets_schedule(self):
        entering, release = Event(), Event()

        class DelayedOwner(SerializedReviewAdmission):
            def issue_review(self, run, due_at, coalesced_count, facts):
                entering.set()
                if not release.wait(2):
                    raise AssertionError("ticket issuance timed out")
                return super().issue_review(run, due_at, coalesced_count, facts)

        owner = DelayedOwner(RUN, AUTOMATION)
        now = [0.0]
        delivered, outcomes = [], []
        scheduler = WorkerReviewScheduler(
            admission=owner, automation_state=lambda: AUTOMATION,
            active_run=lambda: RUN, worker_state=lambda _: ready_worker(),
            collect_non_model=lambda _: {"phase": "running"},
            dispatch_review=lambda item: (delivered.append(item),
                                          {"status": "omp_processed"})[1],
            user_priority=lambda _: False, on_exit=lambda _: None,
            clock=lambda: now[0],
        )
        self.assertEqual(scheduler.tick().next_due_at, 60.0)
        now[0] = 60.0
        ticking = Thread(target=lambda: outcomes.append(scheduler.tick()))
        ticking.start()
        self.assertTrue(entering.wait(2))
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        release.set()
        ticking.join(2)
        self.assertFalse(ticking.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, "waiting")
        self.assertEqual(outcomes[0].next_due_at, 120.0)
        self.assertFalse(outcomes[0].pending)
        self.assertEqual(delivered, [])
        now[0] = 119.999
        self.assertEqual(scheduler.tick().status, "waiting")
        now[0] = 120.0
        self.assertEqual(scheduler.tick().status, "dispatched")
        self.assertEqual(len(delivered), 1)

    def test_writer_wins_before_admission_for_every_authority_writer(self):
        replacement = ActiveRunRef("task", "revision", 1, "run", "new-session", 2)
        writers = {
            "pause": lambda owner: owner.set_automation_state(authority(paused=True)),
            "cancel": lambda owner: owner.set_automation_state(authority(cancelled=True)),
            "revoke": lambda owner: owner.set_automation_state(authority(approvalValid=False)),
            "replace": lambda owner: owner.set_active_run(replacement),
            "aba": lambda owner: (owner.set_active_run(None), owner.set_active_run(RUN)),
            "exit": lambda owner: owner.fence_exit(owner.bind_exit_source(RUN)),
        }
        for name, write in writers.items():
            with self.subTest(writer=name):
                owner = SerializedReviewAdmission(RUN, AUTOMATION)
                ticket = owner.issue_review(RUN, 60.0, 0, {})
                begin, done = Event(), Event()
                delivered, outcomes = [], []

                def submit():
                    self.assertTrue(begin.wait(2))
                    outcomes.append(owner.admit_and_dispatch(
                        ticket,
                        dispatch_review=lambda item: delivered.append(item)).status)
                    done.set()

                thread = Thread(target=submit)
                thread.start()
                write(owner)
                begin.set()
                self.assertTrue(done.wait(2))
                thread.join(2)
                self.assertEqual(outcomes, ["stale"])
                self.assertEqual(delivered, [])

    def test_dispatch_wins_but_each_writer_progresses_during_started_submission(self):
        replacement = ActiveRunRef("task", "revision", 1, "run", "new-session", 2)
        writers = {
            "pause": lambda owner: owner.set_automation_state(authority(paused=True)),
            "cancel": lambda owner: owner.set_automation_state(authority(cancelled=True)),
            "revoke": lambda owner: owner.set_automation_state(authority(approvalValid=False)),
            "replace": lambda owner: owner.set_active_run(replacement),
            "aba": lambda owner: (owner.set_active_run(None), owner.set_active_run(RUN)),
            "exit": lambda owner: owner.fence_exit(owner.bind_exit_source(RUN)),
        }
        for name, write in writers.items():
            with self.subTest(writer=name):
                owner = SerializedReviewAdmission(RUN, AUTOMATION)
                ticket = owner.issue_review(RUN, 60.0, 0, {})
                entered, release, writer_done = Event(), Event(), Event()
                deliveries, outcomes = [], []

                def dispatch(item):
                    deliveries.append(item)
                    entered.set()
                    self.assertTrue(release.wait(2))
                    return {"status": "omp_processed"}

                submitter = Thread(target=lambda: outcomes.append(
                    owner.admit_and_dispatch(ticket,
                                             dispatch_review=dispatch).status))
                submitter.start()
                self.assertTrue(entered.wait(2))
                writer = Thread(target=lambda: (write(owner), writer_done.set()))
                writer.start()
                self.assertTrue(writer_done.wait(1),
                                f"{name} authority writer waited for external action")
                release.set()
                submitter.join(2)
                writer.join(2)
                self.assertFalse(submitter.is_alive() or writer.is_alive())
                self.assertTrue(writer_done.is_set())
                self.assertEqual(outcomes, ["omp_processed"])
                self.assertEqual(len(deliveries), 1)

    def test_aba_does_not_reauthorize_a_request_from_before_replacement(self):
        owner = SerializedReviewAdmission(RUN, AUTOMATION)
        old_request = request()
        old_ticket = owner.issue_review(RUN, 60.0, 0, {})
        old_epoch = owner.snapshot()
        owner.set_active_run(None)
        owner.set_active_run(RUN)
        delivered = []
        self.assertEqual(owner.admit_and_dispatch(
            old_ticket, dispatch_review=lambda item: delivered.append(item)).status, "stale")
        self.assertEqual(owner.admit_and_dispatch(
            old_request, observed_epoch=old_epoch,
            dispatch_review=lambda item: delivered.append(item)).status, "stale")
        # A caller cannot turn a retained request into new work by attaching a
        # current epoch after the same identity was detached and attached again.
        self.assertEqual(owner.admit_and_dispatch(
            old_request, observed_epoch=owner.snapshot(),
            dispatch_review=lambda item: delivered.append(item)).status, "stale")
        self.assertEqual(delivered, [])

    def test_malformed_authority_and_unknown_delivery_fail_closed(self):
        owner = SerializedReviewAdmission(RUN, {"portVersion": 2, "kind": "AutomationState",
                                                  "payload": {**AUTOMATION["payload"],
                                                              "paused": "false"}})
        delivered = []
        denied_ticket = owner.issue_review(RUN, 60.0, 0, {})
        self.assertEqual(owner.admit_and_dispatch(
            denied_ticket,
            dispatch_review=lambda item: delivered.append(item)).status, "authority_invalid")
        self.assertEqual(delivered, [])
        owner.set_automation_state(AUTOMATION)
        ticket = owner.issue_review(RUN, 60.0, 0, {})
        calls = []

        def raises(item):
            calls.append(item)
            raise RuntimeError("ambiguous submission")

        self.assertEqual(owner.admit_and_dispatch(
            ticket, dispatch_review=raises).status,
            "unknown")
        self.assertEqual(owner.admit_and_dispatch(
            ticket, dispatch_review=raises).status,
            "stale")
        self.assertEqual(len(calls), 1)

    def test_callback_hints_cannot_change_authoritative_owner(self):
        owner = SerializedReviewAdmission(RUN, AUTOMATION)
        epoch = owner.snapshot()
        replacement = ActiveRunRef("task", "revision", 1, "run", "other", 2)
        self.assertFalse(owner.reconcile_observation(replacement, AUTOMATION,
                                                     observed_epoch=epoch))
        self.assertFalse(owner.reconcile_observation(RUN, authority(paused=True),
                                                     observed_epoch=epoch))
        self.assertEqual(owner.snapshot(), epoch)
        delivered = []
        ticket = owner.issue_review(RUN, 60.0, 0, {})
        self.assertEqual(owner.admit_and_dispatch(
            ticket,
            dispatch_review=lambda item: (delivered.append(item),
                                          {"status": "omp_processed"})[1]).status,
            "omp_processed")
        self.assertEqual(len(delivered), 1)


if __name__ == "__main__":
    unittest.main()
