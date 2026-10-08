"""CW-19 U2: the SQLite RecoveryPort, exact-reference stop callbacks and the termination observation."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from support import Owned, ticks

from workbench.backend.automation import _signal_exact, approval_hash
from workbench.policy.recovery_manager import (
    RecoveryCoordinator, RecoveryRequest, RunIdentity, RunObservation,
)
from workbench.policy.recovery_manager.port_sqlite import RecoveryFactsUnavailable, RunFacts, SqliteRecoveryPort
from workbench.runtime.process_evidence import ProcessRef
from workbench.tasks.repository import TaskRepository

AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}


class PortFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw19-port-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db = self.root / "tasks.sqlite3"
        self.repository = TaskRepository(self.db)
        self.addCleanup(self.repository.close)
        spec = {"goal": "g", "paths": ["out.txt"], "execution": {
            "command": "true", "criteria": {"log_contains": "PASS", "result_file": "out.txt",
                                            "result_contains": "PASS"}}}
        self.task = self.repository.create_task(spec)
        self.repository.approve_scope(self.task, 1, {"paths": ["out.txt"], "execution": spec["execution"]})
        self.repository.proceed(self.task, 1, "start")
        self.run_id = self.repository.start_run(self.task, 1, inputs={
            "approval_hash": approval_hash(self.repository, self.task, 1)})  # as TaskWorkflow.start records it
        self.identity = RunIdentity(self.task, 1, self.run_id)
        self.facts_calls = 0
        self.stop_requested = False
        self.automation = AUTOMATION
        self.port = SqliteRecoveryPort(self.db, self.root / "recovery.sqlite3", self.run_facts)

    def run_facts(self, identity):
        self.facts_calls += 1
        observation = RunObservation(identity, self.facts_calls, "running", None, False, "running", False, False,
                                     False, self.stop_requested)
        return RunFacts(observation=observation, automation_state=self.automation, terminal_owner="workbench",
                        metadata_healthy=True, worktree_root=str(self.root), raw_log_path=str(self.root / "raw.log"))


class RecoveryPortTests(PortFixture):
    def test_facts_from_the_metadata_and_the_live_run(self):
        facts = self.port.read(self.identity)
        self.assertEqual((facts.identity, facts.current_identity), (self.identity, self.identity))
        self.assertEqual(facts.approval_hash, approval_hash(self.repository, self.task, 1))
        self.assertEqual(facts.approved_paths, ("out.txt",))
        self.assertEqual(facts.criteria.log_contains, "PASS")
        self.assertFalse(facts.revoked)
        self.assertEqual([(a.attempt_id, a.kind) for a in facts.attempts], [(self.run_id, "initial")])
        self.assertEqual(oct(os.stat(self.root / "recovery.sqlite3").st_mode & 0o777), "0o600")

    def test_identity_mismatch_and_gaps_fail_closed(self):
        other = RunIdentity(self.task, 1, "00000000-0000-4000-8000-000000000000")
        with self.assertRaises(RecoveryFactsUnavailable):
            self.port.read(other)
        coordinator = RecoveryCoordinator(self.port)
        with self.assertRaises(RecoveryFactsUnavailable):
            coordinator.decide(RecoveryRequest(other, "d1", kind="stop"))

    def test_stop_is_planned_only_with_current_facts(self):
        coordinator = RecoveryCoordinator(self.port)
        decision = coordinator.decide(RecoveryRequest(self.identity, "d1", kind="stop"))
        self.assertIn("request_stop", decision.steps)
        self.automation = {**AUTOMATION, "payload": {**AUTOMATION["payload"], "paused": True}}
        decision = coordinator.decide(RecoveryRequest(self.identity, "d2", kind="stop"))
        self.assertNotIn("request_stop", decision.steps)  # paused automation: not authorized
        self.repository.approve_scope(self.task, 1, {"paths": ["other.txt"]})  # approval changed
        self.automation = AUTOMATION
        decision = coordinator.decide(RecoveryRequest(self.identity, "d3", kind="stop"))
        self.assertEqual(decision.report.remaining_problems, ("approval_changed",))

    def test_revoked_authority_asks_for_the_stop_once(self):
        coordinator = RecoveryCoordinator(self.port)
        self.repository.revoke_authority(self.task, 1, "user_full_shutdown")
        with self.assertRaises(RecoveryFactsUnavailable):  # the run is no longer current: no facts
            coordinator.decide(RecoveryRequest(self.identity, "d1", kind="stop"))

    def test_reservation_is_an_atomic_task_wide_cas(self):
        facts = self.port.read(self.identity)
        args = dict(task_id=self.task, expected_authority_version=facts.authority_version,
                    expected_settings_revision=facts.settings_revision, maximum=2)
        self.assertTrue(self.port.reserve_recovery(decision_id="r1", expected_used=0, **args))
        self.assertFalse(self.port.reserve_recovery(decision_id="r1", expected_used=1, **args), "duplicate id")
        self.assertFalse(self.port.reserve_recovery(decision_id="r2", expected_used=0, **args), "stale count")
        self.assertFalse(self.port.reserve_recovery(decision_id="r2", expected_used=1,
                                                    **{**args, "expected_authority_version": 99}), "stale authority")
        self.assertTrue(self.port.reserve_recovery(decision_id="r2", expected_used=1, **args))
        self.assertFalse(self.port.reserve_recovery(decision_id="r3", expected_used=2, **args), "limit")
        kinds = [a.kind for a in self.port.read(self.identity).attempts]
        self.assertEqual(kinds, ["initial", "recovery", "recovery"])

    def test_concurrent_reservations_one_wins(self):
        facts = self.port.read(self.identity)
        barrier = threading.Barrier(8)
        results = []

        def reserve(index):
            port = SqliteRecoveryPort(self.db, self.root / "recovery.sqlite3", self.run_facts)
            barrier.wait()
            results.append(port.reserve_recovery(
                task_id=self.task, decision_id=f"c{index}", expected_used=0,
                expected_authority_version=facts.authority_version,
                expected_settings_revision=facts.settings_revision, maximum=3))

        threads = [threading.Thread(target=reserve, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(results.count(True), 1)


class ExactSignalTests(unittest.TestCase):
    def test_a_reused_pid_is_never_signalled(self):
        owned = Owned()
        self.addCleanup(owned.cleanup)
        child = owned.spawn("exec sleep 60")
        wrong = ProcessRef("shell", child.pid, ticks(child.pid) + 5, 1)
        with mock.patch("signal.pidfd_send_signal") as send:
            self.assertFalse(_signal_exact(wrong, (15,)))
        send.assert_not_called()
        self.assertTrue(_signal_exact(ProcessRef("shell", child.pid, ticks(child.pid), 1), (15,)))
        self.assertEqual(child.wait(5), -15)
        self.assertFalse(_signal_exact(ProcessRef("shell", child.pid, 1234, 1), (15,)))


class ObservationTests(unittest.TestCase):
    def test_sequence_is_monotonic_and_follows_the_record(self):
        from types import SimpleNamespace
        from workbench.backend.automation import AutomationController, _Bound
        controller = AutomationController.__new__(AutomationController)
        import threading as _threading
        controller._lock = _threading.RLock()
        controller._observation_sequence, controller._stop_requested = {}, set()
        task, run = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
        life = {"lifetime": "running", "input_returned": False, "control_returned": False}
        record = {"shell_state": "running", "exit_status": None, "exit_confirmed": False}
        workflow = SimpleNamespace(_record=record, shell=SimpleNamespace(snapshot=lambda: {"lifecycle": life}))
        controller._bound = _Bound("experiment", task, 1, run, object(), None, workflow)
        first = controller._observation(run)
        self.assertEqual((first.sequence, first.shell_state, first.fully_terminated), (1, "running", False))
        record.update(shell_state="exited", exit_status=0, exit_confirmed=True)
        life.update(lifetime="ended", input_returned=True, control_returned=True)
        second = controller._observation(run)
        self.assertEqual(second.sequence, 2)
        self.assertTrue(second.fully_terminated)
        self.assertIsNone(controller._observation("33333333-3333-4333-8333-333333333333"))


if __name__ == "__main__":
    unittest.main()
