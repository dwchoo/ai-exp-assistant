"""CW-18 smoke-02 findings E2/E3 (p27-cw18-smoke-fix-02), no OMP and no provider.

- E2: the verified CW-10 contract judges an experiment with all three criteria (``log_contains`` in the raw
  log, ``result_file`` written by the run, ``result_contains`` in it), so all three stay required; the
  rejection, the ``to_worker`` tool description and the to-worker skill say what each means and how to choose
  ``result_file``.
- E3: an experiment run that fails to start (``run_start_failed``) is told to the manager once, as a Workbench
  notice through the existing delivery path (worker->manager REPORT replying to the run's TASK message, the
  same path the workflow's own preparation report uses), in addition to ``notices[]``. Nothing is sent when the
  workflow already reported to the manager, when the run never got a TASK message, or for a cancel. The Task and
  worker state stay as before (finished, worker idle, held ``start_failed:<error>``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys
import unittest
from uuid import uuid4

from workbench.backend.flow import validate_arguments
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.workflow.run import WorkflowHeld

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FLOW = _load("cw18_smoke02_task_flow_fixture", ROOT / "tests/backend/test_task_flow.py")


class CriteriaContractTests(unittest.TestCase):
    def experiment(self, criteria):
        return {"kind": "experiment", "message": "run it", "spec": {"goal": "g", "paths": ["out/"], "execution": {
            "source": "/src/repo", "commit": "a" * 40, "command": "./exp.sh", "criteria": criteria,
            "environment": [], "shell": "bash"}}}

    def test_incomplete_criteria_rejection_explains_all_three(self):
        for criteria in ({"log_contains": "RESULT=ok", "result_file": "", "result_contains": ""},
                         {"log_contains": "RESULT=ok"}):
            with self.subTest(criteria=criteria):
                errors = [e for e in validate_arguments("to_worker", self.experiment(criteria))
                          if e.startswith("spec.execution.criteria")]
                self.assertEqual(len(errors), 1, errors)
                error = errors[0]
                self.assertRegex(error, r"(?i)all three")
                for needle in ("log_contains", "result_file", "result_contains", "log", "writes"):
                    self.assertIn(needle, error)
                self.assertNotIn("RESULT=ok", error, "never echo the value sent")

    def test_complete_criteria_still_pass(self):
        errors = validate_arguments("to_worker", self.experiment(
            {"log_contains": "RESULT=ok", "result_file": "out/result.txt", "result_contains": "RESULT=ok"}))
        self.assertEqual(errors, [])

    def test_skill_explains_the_three_criteria_and_how_to_choose_result_file(self):
        text = (ROOT / "omp_bridge/skills/to-worker/SKILL.md").read_text()
        self.assertRegex(text, r"(?i)all three .*required")
        line = next(l for l in text.splitlines() if "result_file" in l and "writes" in l)
        self.assertRegex(line, r"(?i)indeterminate")
        self.assertRegex(text, r"(?i)do not invent")
        self.assertIn("log_contains", text)
        self.assertIn("result_contains", text)

    def test_tool_description_explains_the_three_criteria(self):
        source = (ROOT / "omp_bridge/g3/bridge.ts").read_text()
        block = source[source.index("criteria: {"):source.index("environment: {", source.index("criteria: {"))]
        self.assertRegex(block, r"(?i)all three")
        for name in ("log_contains", "result_file", "result_contains"):
            self.assertRegex(block, rf"{name}: \{{[^}}]*description")
        self.assertRegex(block, r"(?i)writes")


class FailingWorkflow:
    """Starts like FakeWorkflow, then fails as run.py does after the TASK delivery (E1-style unknown receipt)."""

    def __init__(self, repository, log, *, task_message=True, manager_report=None, error=WorkflowHeld):
        self.repository, self.log = repository, log
        self.task_message, self.manager_report, self.error = task_message, manager_report, error

    def start(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell):
        run_id = self.repository.start_run(task_id, revision, inputs={})
        message_id = str(uuid4()) if self.task_message else None
        self.log.append({"task_id": task_id, "run_id": run_id, "task_message_id": message_id})
        self.repository.fail_run(run_id, {"stage": "preparation"})
        exc = self.error("worker instruction was not confirmed processed; no execution or replay")
        exc.workbench_start_failure = {"run_id": run_id, "revision": revision, "task_message_id": message_id,
                                       "manager_report": self.manager_report}
        raise exc


class StartFailureNoticeTests(FLOW.FlowFixture):
    def swap_workflow(self, factory):
        """FlowFixture.open looks FakeWorkflow up when the runner starts a run: swap it for this test."""
        original = FLOW.FakeWorkflow
        FLOW.FakeWorkflow = factory
        self.addCleanup(setattr, FLOW, "FakeWorkflow", original)
        self.flow.close()
        self.service.close()
        self.open()

    def use(self, **options):
        self.swap_workflow(lambda repository, log, gates=None: FailingWorkflow(repository, log, **options))

    def failed_start(self):
        result = self.new_experiment()
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(FLOW.wait_until(lambda: self.flow.task_view()["status"] == "finished"))
        return result["task_id"]

    def notices_to_manager(self):
        return [m for m in self.mailbox.to(ActorRole.MANAGER)
                if (m.payload or {}).get("handoff") == "workbench_notice"]

    def test_run_start_failure_is_told_to_the_manager_once_with_notices_kept(self):
        self.use()
        task_id = self.failed_start()
        self.assertTrue(FLOW.wait_until(lambda: len(self.notices_to_manager()) == 1))
        message = self.notices_to_manager()[0]
        run = self.runs[0]
        self.assertEqual((message.sender_role, message.target_role, message.kind),
                         (ActorRole.WORKER, ActorRole.MANAGER, MessageKind.REPORT))
        self.assertEqual((message.task_id, message.run_id, message.in_reply_to_message_id),
                         (task_id, run["run_id"], run["task_message_id"]))
        payload = message.payload
        self.assertEqual(payload["notice"], "run_start_failed")
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(payload["error"], "WorkflowHeld")
        self.assertEqual(payload["source"], "workbench")
        self.assertRegex(payload["message"], r"(?i)did not start")
        self.assertIn("run: true", payload["message"])
        self.assertLessEqual(len(payload["message"]), 1024)
        self.assertTrue(FLOW.wait_until(lambda: message.message_id in self.mailbox.delivered))
        # Task and worker state are as before; notices[] still carries the failure for the next to_worker.
        view = self.flow.task_view()
        self.assertEqual((view["status"], view["held_reason"]), ("finished", "start_failed:WorkflowHeld"))
        self.assertEqual(self.flow.worker_view(), {"state": "idle", "task_id": None})
        follow = self.new_work("next task")
        self.assertEqual(follow["status"], "dispatched", follow)
        self.assertEqual([n["kind"] for n in follow.get("notices", [])], ["run_start_failed"])
        FLOW.time.sleep(0.2)
        self.assertEqual(len(self.notices_to_manager()), 1, "told once")
        self.assertTrue(any(r.get("type") == "start_failure_notice" for r in self.ledger()))

    def test_no_manager_notice_when_the_workflow_already_reported(self):
        self.use(manager_report="omp_processed")
        self.failed_start()
        FLOW.time.sleep(0.3)
        self.assertEqual(self.notices_to_manager(), [])

    def test_no_manager_notice_without_a_task_message(self):
        self.use(task_message=False)
        self.failed_start()
        FLOW.time.sleep(0.3)
        self.assertEqual(self.notices_to_manager(), [])
        self.assertEqual(self.flow.task_view()["held_reason"], "start_failed:WorkflowHeld")

    def test_a_failure_before_any_run_sends_nothing(self):
        class Early:
            def __init__(self, repository, log, gates=None):
                pass

            def start(self, *args, **kwargs):
                raise WorkflowHeld("automation is paused, cancelled, unhealthy, or unapproved")

        self.swap_workflow(Early)
        self.failed_start()
        FLOW.time.sleep(0.3)
        self.assertEqual(self.notices_to_manager(), [])


class WorkflowStartFailureIdentityTests(unittest.TestCase):
    """run.py attaches the failed run's identity to the start exception (what E3 needs)."""

    def setUp(self):
        self.workflow_tests = _load("cw18_smoke02_workflow_fixture", ROOT / "tests/workflow/test_run.py")

    def test_unknown_task_receipt_carries_run_and_task_message(self):
        case = self.workflow_tests.WorkflowRuntimeTests("test_four_exit_and_evidence_outcomes_on_real_host_shell")
        case.setUp()
        try:
            task_id = case.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
            original = case.mailbox.deliver
            case.mailbox.deliver = lambda message: (original(message), FLOW.SimpleNamespace(
                status=FLOW.MailboxStatus.UNKNOWN))[1]
            with self.assertRaises(WorkflowHeld) as raised:
                case.workflow.start(task_id, 1, worktree_path=case.root / "wt", artifacts_root=case.artifacts,
                                    automation=self.workflow_tests.AUTOMATION)
            failure = raised.exception.workbench_start_failure
            task_message = case.mailbox.messages[0]
            self.assertEqual(failure, {"run_id": task_message.run_id, "revision": 1,
                                       "task_message_id": task_message.message_id, "manager_report": None})
        finally:
            case.doCleanups()

    def test_preparation_report_is_named_so_nothing_is_told_twice(self):
        case = self.workflow_tests.WorkflowRuntimeTests("test_four_exit_and_evidence_outcomes_on_real_host_shell")
        case.setUp()
        try:
            task_id = case.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
            spec = case.repository.get_task_spec(task_id, 1)["spec"]
            self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", spec["execution"]["commit"]))
            with FLOW_PATCH("workbench.workflow.run.prepare_execution_worktree",
                            side_effect=self.workflow_tests.WorktreePreparationError("fixture", "prepared nothing")):
                with self.assertRaises(Exception) as raised:
                    case.workflow.start(task_id, 1, worktree_path=case.root / "wt2", artifacts_root=case.artifacts,
                                        automation=self.workflow_tests.AUTOMATION)
            failure = raised.exception.workbench_start_failure
            self.assertEqual(failure["manager_report"], "omp_processed")
            self.assertIsNotNone(failure["task_message_id"])
        finally:
            case.doCleanups()


from unittest.mock import patch as FLOW_PATCH  # noqa: E402


if __name__ == "__main__":
    unittest.main()
