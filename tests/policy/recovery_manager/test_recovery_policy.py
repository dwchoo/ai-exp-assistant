"""Fixture contract for CW-13 Manager second-level policy."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
import unittest
from uuid import uuid4

from workbench.policy.recovery_manager import (
    Artifact, CompletionCriteria, ForceTarget, OrdinaryStopProof,
    RecoveryCoordinator, RecoveryFacts,
    RecoveryRequest, RetryAttempt, RunIdentity, RunObservation, TargetLiveness,
    WorkerJudgment,
)


def digest(content: bytes) -> str:
    return sha256(content).hexdigest()


class FixturePort:
    def __init__(self, facts: RecoveryFacts):
        self.facts = facts
        self.reservations: set[str] = set()

    def read(self, identity: RunIdentity) -> RecoveryFacts:
        return self.facts

    def reserve_recovery(self, *, task_id: str, decision_id: str, expected_used: int,
                         expected_authority_version: int,
                         expected_settings_revision: int, maximum: int) -> bool:
        facts = self.facts
        if (task_id != facts.identity.task_id or decision_id in self.reservations
                or expected_used != facts.retries_used or expected_used >= maximum
                or expected_authority_version != facts.authority_version
                or expected_settings_revision != facts.settings_revision):
            return False
        self.reservations.add(decision_id)
        self.facts = replace(facts, attempts=facts.attempts +
                             (RetryAttempt(task_id, decision_id, "recovery"),))
        return True


class RacingCASPort(FixturePort):
    """Both callers read one snapshot; the retry ledger remains atomic."""

    def __init__(self, facts: RecoveryFacts):
        super().__init__(facts)
        self.read_barrier = Barrier(2)
        self.reserve_lock = Lock()
        self.read_lock = Lock()
        self.read_count = 0

    def read(self, identity: RunIdentity) -> RecoveryFacts:
        facts = self.facts
        with self.read_lock:
            self.read_count += 1
            initial_read = self.read_count <= 2
        if initial_read:
            self.read_barrier.wait(timeout=5)
        return facts

    def reserve_recovery(self, **kwargs) -> bool:
        with self.reserve_lock:
            return super().reserve_recovery(**kwargs)


class RecoveryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = RunIdentity(str(uuid4()), 1, str(uuid4()))
        self.criteria = CompletionCriteria("RUN OK", "src/result.txt", "PASS")
        self.log = b"start\nRUN OK\n"
        self.result_bytes = b"PASS\n"
        self.facts = self.make_facts()
        self.port = FixturePort(self.facts)
        self.coordinator = RecoveryCoordinator(self.port)

    def make_worker(self, *, identity: RunIdentity | None = None,
                    status: int | None = 0, judgment: str = "success",
                    reasons: tuple[str, ...] = ("criteria_met",),
                    log: bytes | None = None, result: bytes | None = None,
                    unknowns: tuple[str, ...] = (), errors: tuple[dict[str, str], ...] = (),
                    criteria: CompletionCriteria | None = None) -> WorkerJudgment:
        log = self.log if log is None else log
        result = self.result_bytes if result is None else result
        chosen = criteria or self.criteria
        report = {
            "task_id": (identity or self.identity).task_id,
            "revision": (identity or self.identity).revision,
            "run_id": (identity or self.identity).run_id,
            "criteria": {"log_contains": chosen.log_contains,
                         "result_file": chosen.result_file,
                         "result_contains": chosen.result_contains},
            "judgment": judgment, "reasons": list(reasons),
            "unknowns": list(unknowns), "evidence_errors": list(errors),
            "exit_status": status, "exit_confirmed": True,
            "raw_log": "/records/raw.log", "raw_log_sha256": digest(log),
            "result_file": "/worktree/src/result.txt", "result_sha256": digest(result),
            "requires_code_change": False, "code_change_reason": "",
        }
        return WorkerJudgment.from_cw11_report(
            report, report_id=str(uuid4()), provenance="durable_worker_report")

    def make_facts(self, *, status: int | None = 0, judgment: str = "success",
                   reasons: tuple[str, ...] = ("criteria_met",),
                   log: bytes | None = None, result: bytes | None = None,
                   unknowns: tuple[str, ...] = (), errors: tuple[dict[str, str], ...] = (),
                   attempts: tuple[RetryAttempt, ...] = ()) -> RecoveryFacts:
        log = self.log if log is None else log
        result = self.result_bytes if result is None else result
        observation = RunObservation(self.identity, 3, "exited", status, True, "ended",
                                     True, True, True, False)
        return RecoveryFacts(
            identity=self.identity, current_identity=self.identity,
            approval_hash="a" * 64, current_approval_hash="a" * 64,
            criteria=self.criteria, approved_paths=("src/result.txt",),
            automation_state={"portVersion": 2, "kind": "AutomationState", "payload": {
                "paused": False, "cancelled": False, "metadataHealthy": True,
                "approvalValid": True}},
            revoked=False, metadata_healthy=True, terminal_owner="workbench",
            settings_revision=1, authority_version=1, observation=observation,
            worker=self.make_worker(status=status, judgment=judgment, reasons=reasons,
                                    log=log, result=result, unknowns=unknowns,
                                    errors=errors),
            raw_log=Artifact("raw_log", "/records/raw.log", log, digest(log)),
            result=Artifact("result_file", "/worktree/src/result.txt", result,
                            digest(result)), worktree_root="/worktree",
            raw_log_path="/records/raw.log", attempts=attempts,
        )

    def request(self, kind: str = "evaluate", **changes) -> RecoveryRequest:
        return RecoveryRequest(self.identity, str(uuid4()), kind,
                               expected_settings_revision=self.port.facts.settings_revision,
                               **changes)

    def test_second_level_confirms_only_complete_criteria_bound_evidence(self):
        decision = self.coordinator.decide(self.request())
        self.assertEqual(decision.outcome, "confirmed_completion")
        self.assertEqual(decision.steps, ())
        self.assertIn("criteria_met", decision.report.reasons)
        self.assertEqual(len(decision.report.sources), 5)
        self.assertEqual(decision.report.remaining_problems, ())
        self.port.facts = self.make_facts(status=7, judgment="failure",
                                          reasons=("nonzero_exit",))
        failure = self.coordinator.decide(self.request())
        self.assertEqual(failure.outcome, "confirmed_failure")
        self.assertEqual(failure.report.remaining_problems, ("nonzero_exit",))
        self.port.facts = self.make_facts(
            judgment="failure", reasons=("exit_zero_criteria_failed",),
            result=b"FAIL\n")
        failure = self.coordinator.decide(self.request())
        self.assertEqual(failure.outcome, "confirmed_failure")
        self.assertEqual(failure.report.remaining_problems,
                         ("exit_zero_criteria_failed",))
        self.port.facts = self.make_facts()
        self.port.facts = replace(self.port.facts, worker=replace(
            self.port.facts.worker, reasons=("exit_zero_criteria_failed",)))
        self.assertEqual(self.coordinator.decide(self.request()).outcome,
                         "additional_investigation")

    def test_missing_changed_or_unresolved_artifacts_never_confirm(self):
        for source in ("raw_log", "result"):
            with self.subTest(source=source):
                facts = self.make_facts()
                artifact = getattr(facts, source)
                self.port.facts = replace(facts, **{
                    source: replace(artifact, content=None, error="NotFound")})
                decision = self.coordinator.decide(self.request())
                self.assertEqual(decision.outcome, "additional_investigation")
                self.assertTrue(decision.report.evidence_errors)
                self.port.facts = replace(facts, **{
                    source: replace(artifact, collected_sha256="0" * 64)})
                self.assertEqual(self.coordinator.decide(self.request()).outcome,
                                 "additional_investigation")
        self.port.facts = self.make_facts(
            judgment="indeterminate", reasons=("insufficient_raw_log",),
            unknowns=("raw log unavailable",),
            errors=({"source": "raw_log", "path": "/records/raw.log",
                     "error": "NotFound"},))
        decision = self.coordinator.decide(self.request())
        self.assertEqual(decision.outcome, "additional_investigation")
        self.assertIn("raw log unavailable", decision.report.unknowns)
        facts = self.make_facts()
        self.port.facts = replace(facts, worktree_root="/other-worktree")
        self.assertEqual(self.coordinator.decide(self.request()).report.remaining_problems,
                         ("artifact_missing_or_changed",))
        self.port.facts = replace(facts, raw_log_path="/other/raw.log")
        self.assertEqual(self.coordinator.decide(self.request()).outcome,
                         "additional_investigation")

    def test_identity_criteria_and_terminal_unknown_fail_closed(self):
        facts = self.make_facts()
        other = RunIdentity(str(uuid4()), 1, str(uuid4()))
        self.port.facts = replace(facts, current_identity=other)
        self.assertEqual(self.coordinator.decide(self.request()).report.remaining_problems,
                         ("identity_mismatch",))
        self.port.facts = replace(facts, worker=replace(
            facts.worker, criteria=CompletionCriteria("OTHER", "src/result.txt", "PASS")))
        self.assertEqual(self.coordinator.decide(self.request()).outcome,
                         "additional_investigation")
        for observation in (
            replace(facts.observation, shell_state="unknown", lifetime="unknown",
                    exit_confirmed=False),
            replace(facts.observation, shell_state="exited", lifetime="running",
                    descendants_clear=False),
            replace(facts.observation, control_returned=False),
            replace(facts.observation, input_returned=False),
        ):
            with self.subTest(observation=observation):
                self.port.facts = replace(facts, observation=observation)
                decision = self.coordinator.decide(self.request("recover",
                    mutation_paths=("src/result.txt",)))
                self.assertEqual(decision.outcome, "blocked_unresolved")
                self.assertEqual(decision.steps, ())
        self.port.facts = self.make_facts(status=None, judgment="failure",
                                          reasons=("nonzero_exit",))
        self.assertEqual(self.coordinator.decide(self.request()).outcome,
                         "additional_investigation")

    def test_task_wide_retry_three_then_fourth_blocked_without_time_cutoff(self):
        self.port.facts = self.make_facts(
            status=1, judgment="failure", reasons=("nonzero_exit",),
            attempts=(RetryAttempt(self.identity.task_id, "initial-1", "initial"),
                      RetryAttempt(self.identity.task_id, "improve-1", "normal_improvement")))
        for ordinal in (1, 2, 3):
            decision = self.coordinator.decide(self.request("recover",
                mutation_paths=("src/result.txt",)))
            self.assertEqual(decision.outcome, "bounded_recovery_retry")
            self.assertEqual(decision.retry_ordinal, ordinal)
            self.assertEqual(decision.report.attempts_used, ordinal)
            self.assertEqual(decision.steps,
                             ("mutate_approved_sources", "send_new_worker_instruction", "relaunch"))
        blocked = self.coordinator.decide(self.request("recover",
            mutation_paths=("src/result.txt",)))
        self.assertEqual(blocked.outcome, "blocked_unresolved")
        self.assertEqual(blocked.report.remaining_problems, ("recovery_retry_limit_reached",))
        self.assertEqual(blocked.steps, ())

    def test_retry_reservation_is_one_shot_and_persists_across_revision_settings_commit_name(self):
        self.port.facts = self.make_facts(status=1, judgment="failure",
                                          reasons=("nonzero_exit",))
        request = self.request("recover", mutation_paths=("src/result.txt",))
        first = self.coordinator.decide(request)
        self.assertEqual(first.retry_ordinal, 1)
        self.assertEqual(self.coordinator.decide(request).outcome, "blocked_unresolved")
        new_identity = RunIdentity(self.identity.task_id, 2, str(uuid4()))
        facts = self.port.facts
        self.identity = new_identity
        self.port.facts = replace(
            facts, identity=new_identity, current_identity=new_identity,
            observation=replace(facts.observation, identity=new_identity),
            worker=replace(facts.worker, identity=new_identity),
            settings_revision=2, source_commit="b" * 40, task_name="renamed")
        decision = self.coordinator.decide(self.request("recover",
            mutation_paths=("src/result.txt",)))
        self.assertEqual(decision.retry_ordinal, 2)
        self.assertEqual(decision.report.settings_revision, 2)
        self.assertEqual(decision.report.attempts_used, 2)

    def test_stop_and_revision_apply_wait_for_full_termination(self):
        facts = self.make_facts()
        running = replace(facts.observation, shell_state="running", lifetime="running",
                          exit_confirmed=False, descendants_clear=False,
                          input_returned=False, control_returned=False)
        self.port.facts = replace(facts, observation=running)
        next_run = self.coordinator.decide(self.request("apply_revision", target_revision=2))
        self.assertEqual(next_run.outcome, "deferred_next_run")
        self.assertEqual(next_run.steps, ())
        current = self.coordinator.decide(self.request(
            "apply_revision", target_revision=2, apply_current_run=True,
            mutation_paths=("src/result.txt",)))
        self.assertEqual(current.outcome, "termination_required")
        self.assertEqual(current.steps, ("request_stop", "wait_full_termination"))
        self.port.facts = replace(facts, observation=replace(running,
            stop_requested=True, shell_state="exited", exit_confirmed=True,
            lifetime="unknown", descendants_clear=None))
        current = self.coordinator.decide(self.request(
            "apply_revision", target_revision=2, apply_current_run=True,
            mutation_paths=("src/result.txt",)))
        self.assertEqual(current.steps, ("wait_full_termination",))
        self.port.facts = facts
        current = self.coordinator.decide(self.request(
            "apply_revision", target_revision=2, apply_current_run=True,
            mutation_paths=("src/result.txt",)))
        self.assertEqual(current.outcome, "revision_relaunch")
        self.assertEqual(current.steps[-1], "relaunch")
        no_mutation = self.coordinator.decide(self.request(
            "apply_revision", target_revision=2, apply_current_run=True))
        self.assertEqual(no_mutation.steps,
                         ("apply_new_revision", "send_new_worker_instruction", "relaunch"))

    def test_recovery_without_source_mutation_still_consumes_one_task_attempt(self):
        self.port.facts = self.make_facts(status=1, judgment="failure",
                                          reasons=("nonzero_exit",))
        decision = self.coordinator.decide(self.request("recover"))
        self.assertEqual(decision.outcome, "bounded_recovery_retry")
        self.assertEqual(decision.steps, ("send_new_worker_instruction", "relaunch"))
        self.assertEqual(self.port.facts.retries_used, 1)

    def test_pause_cancel_revocation_metadata_and_owner_gate_actions(self):
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))
        for payload_change in ({"paused": True}, {"cancelled": True},
                               {"metadataHealthy": False}, {"approvalValid": False}):
            with self.subTest(payload_change=payload_change):
                port = dict(facts.automation_state)
                port["payload"] = {**port["payload"], **payload_change}
                self.port.facts = replace(facts, automation_state=port)
                decision = self.coordinator.decide(self.request("recover",
                    mutation_paths=("src/result.txt",)))
                self.assertEqual(decision.outcome, "blocked_unresolved")
                self.assertEqual(decision.steps, ())
        self.port.facts = replace(facts, metadata_healthy=False)
        self.assertEqual(self.coordinator.decide(self.request()).outcome,
                         "blocked_unresolved")
        self.port.facts = replace(facts, terminal_owner="user")
        self.assertEqual(self.coordinator.decide(self.request("recover",
            mutation_paths=("src/result.txt",))).steps, ())
        self.port.facts = replace(facts, revoked=True,
            observation=replace(facts.observation, shell_state="running",
                                lifetime="running", exit_confirmed=False))
        revoked = self.coordinator.decide(self.request("apply_revision", target_revision=2))
        self.assertEqual(revoked.outcome, "termination_required")
        self.assertEqual(revoked.steps, ("request_stop", "wait_full_termination"))

    def test_force_needs_delegation_exact_owner_and_fresh_post_force_exit(self):
        facts = self.make_facts()
        target = ForceTarget(self.identity.run_id, 321, 987, 2)
        other = ForceTarget(self.identity.run_id, 322, 987, 2)
        running = replace(facts.observation, shell_state="running", lifetime="running",
                          exit_confirmed=False, descendants_clear=False,
                          observed_target=target, owned_target_confirmed=True)
        self.port.facts = replace(facts, observation=running)
        request = self.request("force", force_target=target)
        self.assertEqual(self.coordinator.decide(request).outcome,
                         "force_confirmation_required")
        self.port.facts = replace(self.port.facts, delegated_force_target=other)
        self.assertEqual(self.coordinator.decide(request).steps, ())
        self.port.facts = replace(self.port.facts, delegated_force_target=target)
        force = self.coordinator.decide(request)
        self.assertEqual(force.outcome, "termination_required")
        self.assertEqual(force.steps,
                         ("request_stop", "observe_after_stop"))
        proof = OrdinaryStopProof(self.identity, target, str(uuid4()), 3)
        stopped = replace(running, stop_requested=True, stop_proof=proof)
        self.port.facts = replace(self.port.facts, observation=stopped)
        waiting = self.coordinator.decide(request)
        self.assertEqual(waiting.outcome, "termination_required")
        self.assertEqual(waiting.steps, ("observe_after_stop",))
        later = replace(stopped, sequence=4,
                        target_liveness=TargetLiveness(target, 4, True))
        self.port.facts = replace(self.port.facts, observation=later)
        force = self.coordinator.decide(request)
        self.assertEqual(force.outcome, "force_requested")
        self.assertEqual(force.steps,
                         ("force_exact_owned_target", "observe_full_termination"))
        stale = replace(facts.observation, observed_target=target,
                        owned_target_confirmed=True, last_force_sequence=3)
        self.port.facts = replace(self.port.facts, observation=stale)
        self.assertEqual(self.coordinator.decide(request).report.remaining_problems,
                         ("post_force_observation_required",))
        blocked = self.coordinator.decide(self.request("recover",
            mutation_paths=("src/result.txt",)))
        self.assertEqual(blocked.outcome, "blocked_unresolved")
        self.port.facts = replace(self.port.facts, observation=replace(stale, sequence=4))
        self.assertEqual(self.coordinator.decide(self.request()).outcome,
                         "confirmed_completion")

    def test_force_stop_proof_order_target_and_authority_are_fail_closed(self):
        facts = self.make_facts()
        target = ForceTarget(self.identity.run_id, 321, 987, 2)
        other = ForceTarget(self.identity.run_id, 322, 987, 2)
        proof = OrdinaryStopProof(self.identity, target, str(uuid4()), 3)
        running = replace(facts.observation, shell_state="running", lifetime="running",
                          exit_confirmed=False, descendants_clear=False,
                          stop_requested=True, stop_proof=proof,
                          observed_target=target, owned_target_confirmed=True)
        base = replace(facts, observation=running, delegated_force_target=target)
        request = RecoveryRequest(self.identity, str(uuid4()), "force", force_target=target)
        cases = (
            (base, "termination_required", ("observe_after_stop",)),
            (replace(base, observation=replace(running, sequence=2)),
             "termination_required", ("observe_after_stop",)),
            (replace(base, observation=replace(running, stop_proof=None)),
             "force_confirmation_required", ()),
            (replace(base, observation=replace(running, sequence=4,
                                               stop_proof=OrdinaryStopProof(
                                                   self.identity, other, str(uuid4()), 3))),
             "force_confirmation_required", ()),
            (replace(base, observation=replace(running, sequence=4,
                                               observed_target=other)),
             "force_confirmation_required", ()),
            (replace(base, observation=replace(running, sequence=4,
                                               lifetime="unknown", descendants_clear=None)),
             "force_confirmation_required", ()),
            (replace(base, observation=replace(running, sequence=4,
                                               shell_state="exited", exit_confirmed=True,
                                               lifetime="ended", descendants_clear=True)),
             "blocked_unresolved", ()),
        )
        for altered, outcome, steps in cases:
            with self.subTest(outcome=outcome, observation=altered.observation):
                self.port.facts = altered
                decision = self.coordinator.decide(request)
                self.assertEqual(decision.outcome, outcome)
                self.assertEqual(decision.steps, steps)
                self.assertNotIn("force_exact_owned_target", decision.steps)
        paused = {**base.automation_state,
                  "payload": {**base.automation_state["payload"], "paused": True}}
        self.port.facts = replace(base, observation=replace(running, sequence=4),
                                  automation_state=paused)
        self.assertEqual(self.coordinator.decide(request).outcome, "blocked_unresolved")
        self.port.facts = replace(base, observation=replace(running, sequence=4),
                                  revoked=True)
        revoked = self.coordinator.decide(request)
        self.assertEqual(revoked.outcome, "termination_required")
        self.assertNotIn("force_exact_owned_target", revoked.steps)
        with self.assertRaises(ValueError):
            OrdinaryStopProof(self.identity, target, "not-a-uuid", 3)
        with self.assertRaises(ValueError):
            OrdinaryStopProof(self.identity, target, str(uuid4()), 0)
        with self.assertRaises(ValueError):
            replace(running, stop_requested=False)

    def test_force_requires_confirmed_still_running_state_after_stop(self):
        facts = self.make_facts()
        target = ForceTarget(self.identity.run_id, 321, 987, 2)
        proof = OrdinaryStopProof(self.identity, target, str(uuid4()), 3)
        running = replace(facts.observation, sequence=4, shell_state="running",
                          lifetime="running", exit_confirmed=False,
                          descendants_clear=False, stop_requested=True,
                          stop_proof=proof, observed_target=target,
                          owned_target_confirmed=True,
                          target_liveness=TargetLiveness(target, 4, True))
        request = RecoveryRequest(self.identity, str(uuid4()), "force", force_target=target)
        for observation in (
            replace(running, shell_state="unknown"),
            replace(running, shell_state="exited"),
            replace(running, exit_confirmed=True),
        ):
            with self.subTest(observation=observation):
                self.port.facts = replace(facts, observation=observation,
                                          delegated_force_target=target)
                decision = self.coordinator.decide(request)
                self.assertNotEqual(decision.outcome, "force_requested")
                self.assertNotIn("force_exact_owned_target", decision.steps)

    def test_force_requires_fresh_exact_target_liveness_after_stop(self):
        facts = self.make_facts()
        target = ForceTarget(self.identity.run_id, 321, 987, 2)
        other = ForceTarget(self.identity.run_id, 322, 987, 2)
        proof = OrdinaryStopProof(self.identity, target, str(uuid4()), 3)
        running = replace(facts.observation, sequence=4, shell_state="running",
                          lifetime="running", exit_confirmed=False,
                          descendants_clear=False, stop_requested=True,
                          stop_proof=proof, observed_target=target,
                          owned_target_confirmed=True)
        request = self.request("force", force_target=target)
        for liveness in (
            None,
            TargetLiveness(target, 4, None),
            TargetLiveness(target, 4, False),
            TargetLiveness(target, 3, True),
            TargetLiveness(other, 4, True),
        ):
            with self.subTest(liveness=liveness):
                self.port.facts = replace(
                    facts, observation=replace(running, target_liveness=liveness),
                    delegated_force_target=target)
                decision = self.coordinator.decide(request)
                self.assertEqual(decision.outcome, "force_confirmation_required")
                self.assertEqual(decision.report.remaining_problems,
                                 ("target_liveness_unconfirmed",))
                self.assertNotIn("force_exact_owned_target", decision.steps)
        self.port.facts = replace(
            facts,
            observation=replace(running, target_liveness=TargetLiveness(target, 4, True)),
            delegated_force_target=target)
        self.assertEqual(self.coordinator.decide(request).outcome, "force_requested")

    def test_live_target_cannot_override_delegation_or_owner_drift(self):
        facts = self.make_facts()
        target = ForceTarget(self.identity.run_id, 321, 987, 2)
        other = ForceTarget(self.identity.run_id, 322, 987, 2)
        proof = OrdinaryStopProof(self.identity, target, str(uuid4()), 3)
        observation = replace(
            facts.observation, sequence=4, shell_state="running", lifetime="running",
            exit_confirmed=False, descendants_clear=False, stop_requested=True,
            stop_proof=proof, observed_target=target, owned_target_confirmed=True,
            target_liveness=TargetLiveness(target, 4, True))
        base = replace(facts, observation=observation, delegated_force_target=target)
        request = RecoveryRequest(self.identity, str(uuid4()), "force", force_target=target)
        for altered in (
            replace(base, delegated_force_target=other),
            replace(base, observation=replace(observation, observed_target=other)),
            replace(base, observation=replace(observation, owned_target_confirmed=False)),
            replace(base, terminal_owner="user"),
        ):
            with self.subTest(altered=altered):
                port = FixturePort(altered)
                decision = RecoveryCoordinator(port).decide(request)
                self.assertNotEqual(decision.outcome, "force_requested")
                self.assertNotIn("force_exact_owned_target", decision.steps)

    def test_target_liveness_schema_requires_exact_owned_observation(self):
        target = ForceTarget(self.identity.run_id, 321, 987, 2)
        proof = TargetLiveness(target, 4, True)
        self.assertEqual(proof.source, "owned_terminal_observation")
        for changes in (
            {"target": None},
            {"observed_sequence": 0},
            {"observed_sequence": True},
            {"alive": 1},
            {"source": "worker_report"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(proof, **changes)

    def test_ordinary_stop_proof_schema_exact_binding_and_trusted_source(self):
        target = ForceTarget(self.identity.run_id, 321, 987, 2)
        proof = OrdinaryStopProof(self.identity, target, str(uuid4()), 3)
        self.assertEqual(proof.source, "owned_terminal_control")
        other_run = RunIdentity(self.identity.task_id, 1, str(uuid4()))
        for changes in (
            {"identity": other_run},
            {"target": ForceTarget(other_run.run_id, 321, 987, 2)},
            {"request_id": "uppercase-or-noncanonical"},
            {"request_sequence": 0},
            {"request_sequence": True},
            {"source": "worker_report"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(proof, **changes)
        with self.assertRaises(ValueError):
            replace(self.facts.observation, stop_requested=True,
                    stop_proof=OrdinaryStopProof(
                        other_run, ForceTarget(other_run.run_id, 321, 987, 2),
                        str(uuid4()), 3))

    def test_force_requires_further_full_termination_before_recovery_or_revision(self):
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))
        target = ForceTarget(self.identity.run_id, 321, 987, 2)
        proof = OrdinaryStopProof(self.identity, target, str(uuid4()), 3)
        after_stop = replace(facts.observation, sequence=4, shell_state="running",
                             lifetime="running", exit_confirmed=False,
                             descendants_clear=False, stop_requested=True,
                             stop_proof=proof, observed_target=target,
                             owned_target_confirmed=True,
                             target_liveness=TargetLiveness(target, 4, True))
        self.port.facts = replace(facts, observation=after_stop,
                                  delegated_force_target=target)
        self.assertEqual(after_stop.exit_status, 1)
        force = self.coordinator.decide(self.request("force", force_target=target))
        self.assertEqual(force.outcome, "force_requested")
        self.assertEqual(force.steps,
                         ("force_exact_owned_target", "observe_full_termination"))
        for observation in (
            replace(after_stop, last_force_sequence=4),
            replace(after_stop, sequence=5, last_force_sequence=4,
                    shell_state="exited", exit_confirmed=True, lifetime="ended"),
            replace(after_stop, sequence=5, last_force_sequence=4,
                    shell_state="exited", exit_confirmed=True, lifetime="ended",
                    descendants_clear=True, input_returned=False),
            replace(after_stop, sequence=5, last_force_sequence=4,
                    shell_state="exited", exit_confirmed=True, lifetime="ended",
                    descendants_clear=True, control_returned=False),
        ):
            with self.subTest(observation=observation):
                self.port.facts = replace(self.port.facts, observation=observation)
                for kind in ("recover", "apply_revision"):
                    decision = self.coordinator.decide(self.request(
                        kind, mutation_paths=("src/result.txt",),
                        target_revision=2 if kind == "apply_revision" else None,
                        apply_current_run=kind == "apply_revision"))
                    self.assertNotIn("mutate_approved_sources", decision.steps)
                    self.assertNotIn("relaunch", decision.steps)
        terminated = replace(after_stop, sequence=5, last_force_sequence=4,
                             shell_state="exited", exit_confirmed=True,
                             lifetime="ended", descendants_clear=True)
        self.port.facts = replace(self.port.facts, observation=terminated)
        decision = self.coordinator.decide(self.request(
            "recover", mutation_paths=("src/result.txt",)))
        self.assertEqual(decision.outcome, "bounded_recovery_retry")
        self.assertEqual(decision.retry_ordinal, 1)

    def test_malformed_report_and_scope_or_reservation_failure_block(self):
        with self.assertRaises(ValueError):
            RunIdentity("not-a-task-uuid", 1, self.identity.run_id)
        with self.assertRaises(ValueError):
            CompletionCriteria("RUN OK", "src/result.txt/", "PASS")
        with self.assertRaises(ValueError):
            WorkerJudgment.from_cw11_report({"judgment": "success"}, report_id="r",
                                            provenance="durable_worker_report")
        report = {
            "task_id": self.identity.task_id, "revision": 1, "run_id": self.identity.run_id,
            "criteria": {"log_contains": "RUN OK", "result_file": "src/result.txt",
                         "result_contains": "PASS"},
            "judgment": "success", "reasons": ["criteria_met"], "unknowns": [],
            "evidence_errors": [], "exit_status": 0, "exit_confirmed": True,
            "raw_log": "/records/raw.log", "raw_log_sha256": digest(self.log),
            "result_file": "/worktree/src/result.txt",
            "result_sha256": digest(self.result_bytes),
            "requires_code_change": False, "code_change_reason": "",
        }
        with self.assertRaises(ValueError):
            WorkerJudgment.from_cw11_report(report, report_id="r",
                                            provenance="omp_assistant_response")
        report["worker_response"] = {
            "source": "omp_assistant_response", "decision": "success",
            "task_id": self.identity.task_id, "revision": 1,
            "run_id": str(uuid4()), "stage": "analysis"}
        with self.assertRaises(ValueError):
            WorkerJudgment.from_cw11_report(report, report_id="r",
                                            provenance="omp_assistant_response")
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))
        self.port.facts = facts
        self.assertEqual(self.coordinator.decide(self.request("recover",
            mutation_paths=("outside.txt",))).report.remaining_problems,
                         ("mutation_outside_approved_scope",))
        self.port.facts = replace(facts, current_approval_hash="b" * 64)
        self.assertEqual(self.coordinator.decide(self.request()).report.remaining_problems,
                         ("approval_changed",))
        self.port.facts = facts
        self.port.reservations.add("already-reserved")
        request = RecoveryRequest(self.identity, "already-reserved", "recover",
                                  mutation_paths=("src/result.txt",))
        self.assertEqual(self.coordinator.decide(request).report.remaining_problems,
                         ("recovery_reservation_unconfirmed",))

    def test_concurrent_recovery_cas_admits_only_one_retry(self):
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))
        port = RacingCASPort(facts)
        coordinator = RecoveryCoordinator(port)
        requests = (RecoveryRequest(self.identity, str(uuid4()), "recover",
                                    mutation_paths=("src/result.txt",)) for _ in range(2))
        with ThreadPoolExecutor(max_workers=2) as executor:
            decisions = list(executor.map(coordinator.decide, requests))
        self.assertCountEqual((item.outcome for item in decisions),
                              ("bounded_recovery_retry", "blocked_unresolved"))
        self.assertEqual(port.facts.retries_used, 1)
        self.assertEqual(len(port.reservations), 1)
        admitted = next(item for item in decisions if item.outcome == "bounded_recovery_retry")
        self.assertEqual(admitted.retry_ordinal, 1)
        self.assertEqual(admitted.report.attempts_used, 1)
        self.assertEqual(admitted.steps,
                         ("mutate_approved_sources", "send_new_worker_instruction", "relaunch"))
        denied = next(item for item in decisions if item.outcome == "blocked_unresolved")
        self.assertEqual(denied.report.remaining_problems,
                         ("recovery_reservation_unconfirmed",))
        self.assertEqual(denied.steps, ())

    def test_stale_cas_authority_or_settings_never_returns_retry_plan(self):
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))

        class ChangedBeforeReserve(FixturePort):
            def __init__(self, source: RecoveryFacts, field: str):
                super().__init__(source)
                self.field = field

            def reserve_recovery(self, **kwargs) -> bool:
                self.facts = replace(self.facts, **{
                    self.field: getattr(self.facts, self.field) + 1})
                return super().reserve_recovery(**kwargs)

        for field in ("authority_version", "settings_revision"):
            with self.subTest(field=field):
                port = ChangedBeforeReserve(facts, field)
                decision = RecoveryCoordinator(port).decide(RecoveryRequest(
                    self.identity, str(uuid4()), "recover",
                    mutation_paths=("src/result.txt",)))
                self.assertEqual(decision.outcome, "blocked_unresolved")
                self.assertEqual(decision.steps, ())
                self.assertEqual(decision.report.remaining_problems,
                                 ("recovery_reservation_unconfirmed",))
                self.assertEqual(port.facts.retries_used, 0)

    def test_inconsistent_reservation_result_never_authorizes_retry(self):
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))

        class InconsistentPort(FixturePort):
            response = None

            def reserve_recovery(self, **kwargs):
                return self.response  # No durable attempt was recorded.

        for response in (1, True):
            with self.subTest(response=response):
                port = InconsistentPort(facts)
                port.response = response
                decision = RecoveryCoordinator(port).decide(RecoveryRequest(
                    self.identity, str(uuid4()), "recover",
                    mutation_paths=("src/result.txt",)))
                self.assertEqual(decision.outcome, "blocked_unresolved")
                self.assertEqual(decision.steps, ())
                self.assertEqual(port.facts.retries_used, 0)

    def test_post_reservation_history_and_authority_drift_block_retry(self):
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))
        task_id = self.identity.task_id
        paused = {**facts.automation_state,
                  "payload": {**facts.automation_state["payload"], "paused": True}}
        changes = (
            lambda current: replace(current, attempts=()),
            lambda current: replace(current, attempts=(
                RetryAttempt(task_id, "wrong-id", "recovery"),)),
            lambda current: replace(current, attempts=current.attempts +
                                    (RetryAttempt(task_id, "extra", "recovery"),)),
            lambda current: replace(current, attempts=(RetryAttempt(
                task_id, current.attempts[-1].attempt_id, "initial"),)),
            lambda current: replace(current, attempts=(RetryAttempt(
                str(uuid4()), current.attempts[-1].attempt_id, "recovery"),)),
            lambda current: replace(current, current_identity=RunIdentity(
                task_id, 2, str(uuid4()))),
            lambda current: replace(current, identity=RunIdentity(
                task_id, 2, str(uuid4()))),
            lambda current: replace(current, approval_hash="b" * 64),
            lambda current: replace(current, current_approval_hash="b" * 64),
            lambda current: replace(current, criteria=CompletionCriteria(
                "OTHER", "src/result.txt", "PASS")),
            lambda current: replace(current, approved_paths=("other.txt",)),
            lambda current: replace(current, authority_version=2),
            lambda current: replace(current, settings_revision=2),
            lambda current: replace(current, automation_state=paused),
            lambda current: replace(current, metadata_healthy=False),
            lambda current: replace(current, revoked=True),
            lambda current: replace(current, terminal_owner="user"),
            lambda current: replace(current, observation=replace(
                current.observation, sequence=4)),
            lambda current: replace(current, worker=replace(
                current.worker, report_id="changed-report")),
            lambda current: replace(current, raw_log=replace(
                current.raw_log, content=b"changed log", collected_sha256=digest(b"changed log"))),
            lambda current: replace(current, result=replace(
                current.result, content=b"changed result",
                collected_sha256=digest(b"changed result"))),
            lambda current: replace(current, raw_log_path="/records/other.log"),
            lambda current: replace(current, worktree_root="/other-worktree"),
            lambda current: replace(current, source_commit="b" * 40),
            lambda current: replace(current, task_name="renamed"),
        )

        class DriftingPort(FixturePort):
            def __init__(self, source: RecoveryFacts, change):
                super().__init__(source)
                self.change = change

            def reserve_recovery(self, **kwargs) -> bool:
                reserved = super().reserve_recovery(**kwargs)
                if reserved:
                    self.facts = self.change(self.facts)
                return reserved

        for change in changes:
            with self.subTest(change=change):
                port = DriftingPort(facts, change)
                decision = RecoveryCoordinator(port).decide(RecoveryRequest(
                    self.identity, str(uuid4()), "recover",
                    mutation_paths=("src/result.txt",)))
                self.assertEqual(decision.outcome, "blocked_unresolved")
                self.assertEqual(decision.steps, ())
                self.assertEqual(decision.report.remaining_problems,
                                 ("recovery_reservation_unconfirmed",))
        prior = replace(facts, attempts=(RetryAttempt(task_id, "prior", "recovery"),))
        port = DriftingPort(prior, lambda current: replace(current, attempts=(
            RetryAttempt(task_id, "replaced-prior", "recovery"), current.attempts[-1])))
        decision = RecoveryCoordinator(port).decide(RecoveryRequest(
            self.identity, str(uuid4()), "recover",
            mutation_paths=("src/result.txt",)))
        self.assertEqual(decision.outcome, "blocked_unresolved")
        self.assertEqual(decision.steps, ())

    def test_valid_cas_appends_exact_attempt_preserving_all_prior_history(self):
        task_id = self.identity.task_id
        prior = (RetryAttempt(task_id, "initial", "initial"),
                 RetryAttempt(task_id, "improve", "normal_improvement"),
                 RetryAttempt(str(uuid4()), "other-task", "recovery"),
                 RetryAttempt(task_id, "prior-retry", "recovery"))
        port = FixturePort(self.make_facts(status=1, judgment="failure",
                                           reasons=("nonzero_exit",), attempts=prior))
        decision_id = str(uuid4())
        decision = RecoveryCoordinator(port).decide(RecoveryRequest(
            self.identity, decision_id, "recover", mutation_paths=("src/result.txt",)))
        self.assertEqual(port.facts.attempts,
                         prior + (RetryAttempt(task_id, decision_id, "recovery"),))
        self.assertEqual(decision.outcome, "bounded_recovery_retry")
        self.assertEqual(decision.retry_ordinal, 2)
        self.assertEqual(decision.report.attempts_used, 2)
        self.assertIn(f"recovery_attempt:{decision_id}", decision.report.sources)

    def test_in_place_authority_drift_after_cas_cannot_return_actions(self):
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))

        class InPlaceDriftPort(FixturePort):
            def reserve_recovery(self, **kwargs) -> bool:
                reserved = super().reserve_recovery(**kwargs)
                if reserved:
                    self.facts.automation_state["payload"]["paused"] = True
                return reserved

        port = InPlaceDriftPort(facts)
        decision = RecoveryCoordinator(port).decide(RecoveryRequest(
            self.identity, str(uuid4()), "recover", mutation_paths=("src/result.txt",)))
        self.assertEqual(decision.outcome, "blocked_unresolved")
        self.assertEqual(decision.steps, ())
        self.assertEqual(decision.report.remaining_problems,
                         ("recovery_reservation_unconfirmed",))
        self.assertEqual(port.facts.retries_used, 1)

    def test_post_reservation_read_failure_never_replays_or_authorizes(self):
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))

        class FailedPostRead(FixturePort):
            reads = 0

            def read(self, identity: RunIdentity) -> RecoveryFacts:
                self.reads += 1
                if self.reads == 2:
                    raise RuntimeError("durable read unavailable")
                return super().read(identity)

        port = FailedPostRead(facts)
        request = RecoveryRequest(self.identity, str(uuid4()), "recover",
                                  mutation_paths=("src/result.txt",))
        decision = RecoveryCoordinator(port).decide(request)
        self.assertEqual(decision.outcome, "blocked_unresolved")
        self.assertEqual(decision.steps, ())
        self.assertEqual(port.facts.retries_used, 1)
        self.assertEqual(len(port.reservations), 1)
        replay = RecoveryCoordinator(port).decide(request)
        self.assertEqual(replay.outcome, "blocked_unresolved")
        self.assertEqual(replay.steps, ())
        self.assertEqual(port.facts.retries_used, 1)
        self.assertEqual(len(port.reservations), 1)

        class MalformedPostRead(FixturePort):
            reads = 0

            def read(self, identity: RunIdentity):
                self.reads += 1
                return super().read(identity) if self.reads == 1 else {"invalid": True}

        malformed = MalformedPostRead(facts)
        decision = RecoveryCoordinator(malformed).decide(RecoveryRequest(
            self.identity, str(uuid4()), "recover",
            mutation_paths=("src/result.txt",)))
        self.assertEqual(decision.outcome, "blocked_unresolved")
        self.assertEqual(decision.steps, ())

    def test_empty_artifacts_or_cross_run_worker_cannot_reserve(self):
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))
        other = RunIdentity(self.identity.task_id, 2, str(uuid4()))
        cases = (
            replace(facts, worker=None),
            replace(facts, worker=replace(facts.worker, identity=other)),
            replace(facts, raw_log=Artifact("raw_log", "/records/raw.log", b"", digest(b""))),
            replace(facts, result=Artifact("result_file", "/worktree/src/result.txt",
                                           b"", digest(b""))),
            replace(facts, worker=replace(facts.worker, raw_log_sha256="0" * 64)),
        )
        for altered in cases:
            with self.subTest(altered=altered):
                port = FixturePort(altered)
                decision = RecoveryCoordinator(port).decide(RecoveryRequest(
                    self.identity, str(uuid4()), "recover",
                    mutation_paths=("src/result.txt",)))
                self.assertNotEqual(decision.outcome, "bounded_recovery_retry")
                self.assertEqual(decision.steps, ())
                self.assertEqual(port.facts.retries_used, 0)

    def test_force_wrong_or_ambiguous_target_and_post_force_descendants_block(self):
        facts = self.make_facts(status=1, judgment="failure", reasons=("nonzero_exit",))
        target = ForceTarget(self.identity.run_id, 321, 987, 2)
        wrong = ForceTarget(self.identity.run_id, 322, 987, 2)
        running = replace(facts.observation, shell_state="running", lifetime="running",
                          exit_confirmed=False, descendants_clear=False,
                          observed_target=target, owned_target_confirmed=True)
        cases = (
            replace(facts, observation=running, delegated_force_target=None),
            replace(facts, observation=replace(running, observed_target=None),
                    delegated_force_target=target),
            replace(facts, observation=replace(running, owned_target_confirmed=False),
                    delegated_force_target=target),
            replace(facts, observation=replace(running, observed_target=wrong),
                    delegated_force_target=target),
        )
        for altered in cases:
            with self.subTest(altered=altered):
                port = FixturePort(altered)
                decision = RecoveryCoordinator(port).decide(RecoveryRequest(
                    self.identity, str(uuid4()), "force", force_target=target))
                self.assertEqual(decision.outcome, "force_confirmation_required")
                self.assertEqual(decision.steps, ())
        after_force = replace(facts.observation, sequence=4, last_force_sequence=3,
                              descendants_clear=False, observed_target=target,
                              owned_target_confirmed=True)
        port = FixturePort(replace(facts, observation=after_force,
                                   delegated_force_target=target))
        for kind in ("evaluate", "recover", "apply_revision"):
            with self.subTest(kind=kind):
                request = RecoveryRequest(self.identity, str(uuid4()), kind,
                                          mutation_paths=("src/result.txt",),
                                          target_revision=2 if kind == "apply_revision" else None,
                                          apply_current_run=kind == "apply_revision")
                decision = RecoveryCoordinator(port).decide(request)
                self.assertNotIn("mutate_approved_sources", decision.steps)
                self.assertNotIn("relaunch", decision.steps)
                self.assertEqual(port.facts.retries_used, 0)


if __name__ == "__main__":
    unittest.main()
