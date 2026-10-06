"""C-D68 (1)-(3) independent checks (p27-cd68-test-01) of what the models are told.

- (3) the manager keeps its freedom, but work handed over with ``to_worker`` is not done by the manager: it waits
  for the worker's report. The rule is in the ``to_worker`` description (bridge; see
  ``tests/bridge/bridge_terminal_independent_p27cd68.test.ts``), the ``to-worker`` skill and every accepted
  ``to_worker`` result (real TaskFlow + HandoffService, U2b fakes).
- (1)/(2) the worker skill sends every command through ``terminal`` and uses only the Workbench subagents.
No OMP, no provider.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))

import test_task_flow as flow_fixtures  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "omp_bridge" / "skills"
RULE = re.compile(r"do not do it yourself", re.I)
WAIT = re.compile(r"wait for the worker'?s `?to_manager`? report", re.I)


def body(name: str) -> str:
    text = (SKILLS / name / "SKILL.md").read_text()
    return text.split("\n---\n", 1)[1]


class SkillTextTests(unittest.TestCase):
    def test_to_worker_skill_carries_the_manager_rule_and_keeps_the_managers_freedom(self):
        text = body("to-worker")
        self.assertRegex(text, RULE)
        self.assertRegex(text, WAIT)
        # C-D68 (3): only the delegated work is off-limits; everything else for the user stays the manager's.
        self.assertRegex(text, re.compile(r"other work for the user stays yours", re.I))
        self.assertNotRegex(text, re.compile(r"never use (bash|eval)|you have no (bash|eval)", re.I),
                            "the manager keeps OMP's tools")

    def test_to_manager_skill_sends_every_command_through_terminal(self):
        text = body("to-manager")
        self.assertIn("`terminal`", text)
        self.assertRegex(text, re.compile(r"every shell command[^.]*`terminal`", re.I))
        self.assertRegex(text, re.compile(r"no other way to (run|execute) commands", re.I))
        self.assertNotRegex(text, re.compile(r"\buse (the )?`?bash`?\b|\buse (the )?`?eval`?\b", re.I))
        # (7)/(8): the decided behaviour, in the worker's words
        # C-D68 (9): the wait is fixed at 120 s; the worker's wait input (and the 1800 s maximum) is gone.
        for needle in ("120", "terminal_check", "terminal_done", "host_terminal_busy", "End your turn"):
            self.assertIn(needle, text)
        self.assertNotRegex(text, re.compile(r"call `?terminal`? (again )?with `?command`?:? null to wait again", re.I),
                            "C-D68 (8): no re-wait loop")
        self.assertNotIn("1800", text, "C-D68 (9): no 1800 s maximum")
        self.assertNotIn("timeout_seconds", text, "C-D68 (9): the worker sets no wait")
        # C-D68 (9) / smoke M2, M3: end the turn and send no progress reports while it runs; a queued handoff is not resent
        self.assertRegex(text, re.compile(r"end your turn[^.]*\.[^.]*do not send progress", re.I | re.S))
        self.assertRegex(text, re.compile(r"never send the same report again|do not send it again", re.I))

    def test_to_manager_skill_names_only_the_workbench_subagents(self):
        text = body("to-manager")
        self.assertIn("`explorer`", text)
        # C-D69 (3): the worker's analyst subagent is removed; the skill must not offer it
        self.assertNotRegex(text, re.compile(r"\banalyst\b", re.I))
        for bundled in ("scout", "reviewer", "sonic", "security-reviewer"):
            self.assertNotRegex(text, re.compile(rf"`{bundled}`"), f"bundled agent {bundled} offered to the worker")


class ManagerRuleResultTests(flow_fixtures.FlowFixture):
    def assert_rule(self, result):
        rule = result.get("manager_rule")
        self.assertIsInstance(rule, str, result)
        self.assertRegex(rule, RULE)
        self.assertRegex(rule, re.compile(r"to_manager report", re.I))

    def test_new_work_task_result(self):
        result = self.new_work()
        self.assertEqual(result["status"], "dispatched", result)
        self.assert_rule(result)

    def test_new_experiment_and_rerun_results(self):
        result = self.new_experiment()
        self.assertEqual(result["status"], "dispatched", result)
        self.assert_rule(result)
        self.assertTrue(flow_fixtures.wait_until(lambda: len(self.runs) == 1
                                                 and self.flow.task_view()["status"] == "finished"))
        rerun = self.to_worker({"kind": "experiment", "message": "again", "task_id": result["task_id"], "run": True})
        self.assertEqual(rerun["status"], "dispatched", rerun)
        self.assert_rule(rerun)

    def test_follow_up_result(self):
        work = self.running_work()
        follow = self.to_worker({"kind": "work", "message": "also the tests", "task_id": work["task_id"]})
        self.assertEqual(follow["status"], "queued", follow)
        self.assert_rule(follow)


if __name__ == "__main__":
    unittest.main()
