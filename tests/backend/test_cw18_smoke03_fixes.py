"""CW-18 smoke-03 F2 (p27-cw18-smoke-fix-03), no OMP and no provider.

A staged worker response the bridge rejected ends the start with the bridge's short machine reason (for example
``invalid_assistant_response:extra_text``) in the Task ``last_result``, its ``held_reason``, the flow record and
the manager notice, instead of the generic ``ValueError``. No response text is involved: the reason is a code.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

from workbench.workflow.worker_port import WorkerResponseRejected

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SMOKE02 = _load("cw18_smoke02_fixes_for_smoke03", ROOT / "tests/backend/test_cw18_smoke02_fixes.py")
FLOW = SMOKE02.FLOW


class RejectedResponse(WorkerResponseRejected):
    def __init__(self, _text):  # FailingWorkflow passes its own message; the bridge reason is fixed here
        super().__init__("invalid_assistant_response:extra_text")


class RejectedWorkerResponseStartTests(FLOW.FlowFixture):
    # The smoke-02 harness helpers only (its tests run from their own module).
    swap_workflow = SMOKE02.StartFailureNoticeTests.swap_workflow
    use = SMOKE02.StartFailureNoticeTests.use
    failed_start = SMOKE02.StartFailureNoticeTests.failed_start
    notices_to_manager = SMOKE02.StartFailureNoticeTests.notices_to_manager

    def test_rejected_worker_response_reason_reaches_task_record_and_manager(self):
        self.use(error=RejectedResponse)
        task_id = self.failed_start()
        reason = "invalid_assistant_response:extra_text"
        view = self.flow.task_view()
        self.assertEqual((view["status"], view["held_reason"]), ("finished", f"start_failed:{reason}"))
        self.assertEqual(view["last_result"]["outcome"], "start_failed")
        self.assertEqual(view["last_result"]["error"], reason)
        self.assertNotIn("ValueError", repr(view))
        records = [r for r in self.ledger() if r.get("type") == "run_start_failed"]
        self.assertEqual([r["error"] for r in records], [reason])
        self.assertTrue(FLOW.wait_until(lambda: len(self.notices_to_manager()) == 1))
        payload = self.notices_to_manager()[0].payload
        self.assertEqual((payload["task_id"], payload["error"]), (task_id, reason))
        self.assertIn(reason, payload["message"])

    def test_other_start_failures_keep_the_exception_type(self):
        self.use(error=ValueError)
        self.failed_start()
        self.assertEqual(self.flow.task_view()["held_reason"], "start_failed:ValueError")

if __name__ == "__main__":
    unittest.main()
