"""C-D69 (2): the nullable ``analysis`` field of to_worker kind work (validation, normalisation, TASK message)."""
from __future__ import annotations

import unittest

from workbench.backend import flow
from workbench.contracts.v1 import ActorRole

from test_task_flow import FlowFixture, wait_until


def check(args):
    return flow.validate_arguments("to_worker", args)


class AnalysisValidationTests(unittest.TestCase):
    base = {"kind": "work", "message": "run it", "spec": {"goal": "g", "paths": []}}

    def test_summary_detailed_and_null_are_accepted_for_work(self):
        for value in ("summary", "detailed", None, ""):
            self.assertEqual(check({**self.base, "analysis": value}), [], value)
        self.assertEqual(check(self.base), [])

    def test_other_values_are_rejected_naming_the_field(self):
        errors = check({**self.base, "analysis": "deep"})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("analysis:"), errors)

    def test_null_placeholder_is_dropped_and_a_value_is_kept(self):
        self.assertNotIn("analysis", flow.normalize_arguments("to_worker", {**self.base, "analysis": None}))
        self.assertEqual(flow.normalize_arguments("to_worker", {**self.base, "analysis": "detailed"})["analysis"],
                         "detailed")

    def test_an_experiment_must_not_set_analysis(self):
        errors = check({"kind": "experiment", "message": "m", "analysis": "summary",
                        "spec": {"goal": "g", "paths": [], "execution": None}})
        self.assertTrue(any(error.startswith("analysis: must be null for kind experiment") for error in errors), errors)


class AnalysisTaskMessageTests(FlowFixture):
    def dispatched_payload(self, **extra):
        result = self.to_worker({"kind": "work", "message": "collect specs",
                                 "spec": {"goal": "specs", "paths": []}, **extra})
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 1))
        return result, self.mailbox.created[0].payload

    def test_default_is_summary_and_the_task_message_says_so(self):
        _, payload = self.dispatched_payload(analysis=None)
        self.assertEqual(payload["analysis"], "summary")
        self.assertRegex(payload["analysis_rule"], r"^Analysis: summary - .*do not investigate")

    def test_detailed_is_shown_and_kept_for_follow_ups(self):
        result, payload = self.dispatched_payload(analysis="detailed")
        self.assertEqual(payload["analysis"], "detailed")
        self.assertRegex(payload["analysis_rule"], r"^Analysis: detailed")
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        self.to_worker({"kind": "work", "message": "and the swap", "task_id": result["task_id"]})
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 2))
        self.assertEqual(self.mailbox.created[1].payload["analysis"], "detailed")

    def test_a_bad_value_is_rejected_and_nothing_is_dispatched(self):
        result = self.to_worker({"kind": "work", "message": "m", "analysis": "deep",
                                 "spec": {"goal": "specs", "paths": []}})
        self.assertEqual(result["status"], "rejected", result)
        self.assertEqual(self.mailbox.created, [])


if __name__ == "__main__":
    unittest.main()
