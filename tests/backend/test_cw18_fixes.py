"""CW-18 corrections F3/F7 (p27-cw18-fix-01).

F3: a resume always leaves the paused state when the run bound at pause time is no longer current (closed
while paused): it reconciles to idle (no run), lifts the OMPs' pause and says so; an active run keeps the
reconciled-only resume (an unconfirmed manager stop still refuses).
F7: ``role_skill_allowlist`` fails closed for an unknown role.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from test_automation_loop import AutomationFixture  # noqa: E402

from workbench.backend import launcher  # noqa: E402


class ResumeAfterCloseTests(AutomationFixture):
    def pause_running(self):
        run = self.start_experiment(command="printf HOST_STARTED; sleep 1; printf PASS > outcome.txt")
        self.controller.experiment_started(run)
        self.tick()
        self.controller.request_pause()
        self.assertTrue(self.controller.wait_idle(15))
        self.assertTrue(self.status()["paused"])
        self.assertIn("pause", self.worker.frames)
        return run

    def resume(self):
        self.controller.request_resume()
        self.assertTrue(self.controller.wait_idle(20))
        return self.status()

    def test_closed_run_resume_reconciles_to_idle_and_lifts_the_omp_pause(self):
        run = self.pause_running()
        self.repository.cancel_run(run.run_id, "closed_during_pause")
        self.controller.run_ended(run.run_id)
        self.assertEqual(self.status()["state"], "paused")
        status = self.resume()
        self.assertFalse(status["paused"], status)
        self.assertFalse(self.controller.paused())
        self.assertEqual(status["resume"]["outcome"], "resumed")
        self.assertIn("run_closed_while_paused", status["resume"]["reason"])
        self.assertEqual((status["state"], status["run"]), ("idle", None), status)
        self.assertIn("resume", self.manager.frames)
        self.assertIn("resume", self.worker.frames)
        self.assertFalse(self.manager.paused or self.worker.paused)
        self.assertEqual(self.tick(120.0)["outcome"], "idle")
        self.assertEqual(self.worker.reviews(), [], "nothing replayed for the closed run")

    def test_run_no_longer_current_without_run_ended_also_reconciles(self):
        run = self.pause_running()
        self.repository.cancel_run(run.run_id, "closed_without_notice")
        status = self.resume()
        self.assertFalse(status["paused"], status)
        self.assertIn("run_closed_while_paused", status["resume"]["reason"])
        self.assertEqual(status["state"], "idle")

    def test_closed_run_with_an_unlifted_omp_pause_refuses_explicitly_then_succeeds(self):
        run = self.pause_running()
        self.repository.cancel_run(run.run_id, "closed_during_pause")
        self.controller.run_ended(run.run_id)
        original = self.controller.bridge.request
        answers = iter([{"status": "abort_pending"}])

        def flaky(role, frame, timeout=5, **kwargs):
            if frame.get("kind") == "resume" and str(getattr(role, "value", role)) == "manager":
                answer = next(answers, None)
                if answer is not None:
                    return answer
            return original(role, frame, timeout, **kwargs)

        self.controller.bridge.request = flaky
        try:
            status = self.resume()
            self.assertTrue(status["paused"])
            self.assertEqual(status["resume"]["outcome"], "refused")
            self.assertIn("manager=abort_pending", status["resume"]["reason"])
            status = self.resume()
        finally:
            self.controller.bridge.request = original
        self.assertFalse(status["paused"], status)
        self.assertEqual(status["state"], "idle")

    def test_active_run_keeps_the_reconciled_only_resume(self):
        run = self.start_experiment()
        self.controller.experiment_started(run)
        self.tick()
        self.manager.confirm_stop = False
        self.controller.request_pause()
        self.assertTrue(self.controller.wait_idle(15))
        status = self.resume()
        self.assertEqual(status["resume"]["outcome"], "refused", status)
        self.assertTrue(status["paused"])
        self.assertNotIn("run_closed_while_paused", status["resume"]["reason"] or "")
        self.assertNotIn("resume", self.manager.frames)


class SkillAllowlistTests(unittest.TestCase):
    def test_unknown_role_gets_no_skill(self):
        for role in ("", "admin", "Manager", "worker ", None, 3):
            self.assertEqual(launcher.role_skill_allowlist(role), (), role)

    def test_known_roles_unchanged(self):
        self.assertEqual(launcher.role_skill_allowlist("manager"), ("to-worker",))
        self.assertEqual(launcher.role_skill_allowlist("worker"), ("to-manager",))


if __name__ == "__main__":
    unittest.main()
