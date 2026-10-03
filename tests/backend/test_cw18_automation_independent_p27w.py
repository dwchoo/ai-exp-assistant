"""CW-18 independent verification (p27-cw18-test-01): the automation loop (60 s review, pause/resume).

Uses the U3 fixture (real G3 bridge with scripted in-process peers, real ShellPane/HostShellPort,
real TaskWorkflow, real CW-11/12/15 parts behind ``AutomationController``, injected clock) from
``test_automation_loop`` and adds independent expectations:

- the 60 s review is due at exactly 60 s of the injected clock (not at 59.9 s), never while paused,
  and never after the run ended;
- the controller's paused flag is the ONE source: HandoffService and TaskFlow hold at once on pause,
  nothing held is replayed after a reconciled resume;
- a pause never strands the user: after any pause the user can get back to an unpaused Workbench.
"""

from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))

from test_automation_loop import AutomationFixture  # noqa: E402

from workbench.backend.flow import HandoffService  # noqa: E402
from workbench.contracts.v1 import ActorRole  # noqa: E402


class FakeMailbox:
    def __init__(self):
        self.created = []

    def create_message(self, *args, **kwargs):
        raise AssertionError("nothing may be created while paused")

    def deliver(self, *args, **kwargs):
        raise AssertionError("nothing may be delivered while paused")


class ReviewTimingTests(AutomationFixture):
    def test_review_is_due_at_60_seconds_exactly_and_stops_with_the_run(self):
        run = self.start_experiment()
        self.controller.experiment_started(run)
        self.assertEqual(self.tick()["outcome"], "admitted", self.logs)
        self.tick(59.9)
        self.assertEqual(self.worker.reviews(), [], "no review before 60 s")
        self.assertEqual(self.status()["review"]["next_due_in_seconds"], 0.1)
        self.tick(0.1)
        self.assertEqual(len(self.worker.reviews()), 1, "due at 60 s")
        review = self.worker.reviews()[0]
        self.assertEqual(review["runId"], run.run_id)
        self.assertEqual(review["taskId"], run.task_id)
        # many ticks inside one interval: still one review
        for _ in range(5):
            self.tick(10.0)
        self.assertEqual(len(self.worker.reviews()), 1)
        self.tick(10.0)
        self.assertEqual(len(self.worker.reviews()), 2)
        self.controller.run_ended(run.run_id)
        self.tick(600.0)
        self.assertEqual(len(self.worker.reviews()), 2, "no review after the run ended")
        self.assertEqual(self.status()["state"], "idle")


class SinglePausedSourceTests(AutomationFixture):
    def test_pause_holds_handoffs_at_once_and_resume_replays_nothing(self):
        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=FakeMailbox(),
                                 paused=self.controller.paused)
        self.addCleanup(service.close)
        service.start()
        self.controller.request_pause()
        args = {"kind": "work", "message": "m", "spec": {"goal": "g", "paths": ["a/"]}}
        result = service.handle(ActorRole.MANAGER, {"request_id": "r", "tool_call_id": "c-1", "tool": "to_worker",
                                                    "args": args, "session_id": str(uuid4()), "generation": 1})
        self.assertEqual(result, {"status": "held", "reason": "paused"}, "held from the moment of the request")
        self.assertTrue(self.controller.wait_idle(15))
        self.assertEqual(self.status()["interruption"]["state"], "not_needed")
        self.controller.request_resume()
        self.assertTrue(self.controller.wait_idle(15))
        self.assertFalse(self.controller.paused())
        time.sleep(0.2)
        self.assertEqual(service.outbox_snapshot(), [], "nothing was queued or replayed")


class PauseNeverStrandsTests(AutomationFixture):
    def test_a_run_that_closes_during_a_pause_does_not_leave_workbench_paused_forever(self):
        run = self.start_experiment(command="printf HOST_STARTED; sleep 1; printf PASS > outcome.txt")
        self.controller.experiment_started(run)
        self.tick()
        self.controller.request_pause()
        self.assertTrue(self.controller.wait_idle(15))
        # the run closes while paused (e.g. its report was already being delivered when the pause came)
        self.repository.cancel_run(run.run_id, "closed_during_pause")
        self.controller.run_ended(run.run_id)
        for _ in range(2):
            self.controller.request_resume()
            self.assertTrue(self.controller.wait_idle(20))
        status = self.status()
        # Observed: resume refused ("only an uncancelled paused run can be resumed") on every try, so every
        # to_worker stays held:paused and no Task can start until the backend restarts.
        self.assertFalse(status["paused"], (status["resume"], status["state"]))


if __name__ == "__main__":
    unittest.main()
