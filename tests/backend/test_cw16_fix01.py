"""CW-16 fix-01 (B2 self-run defects; no OMP, no provider).

- D-B2-1: the experiment admission asks the live host shell for the declared names (a name exported after the
  backend started counts); a start held for a reason other than the host shell never blocks the worker
  ``terminal`` (no gate held while it waits, no "starting" activity).
- D-B2-2: a run that finishes while paused can be resumed (the finish is recorded, nothing replayed, no review),
  and a cancel goes while paused (the deadlock path: finished while paused, cancel, closed).
- D-B2-3 (C-AC-21): the usage view (runs/retries, reviews, model usage unknown unless reported).
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from test_automation_loop import AutomationFixture  # noqa: E402
from test_task_flow import FlowFixture, wait_until  # noqa: E402

from workbench.backend import cli  # noqa: E402
from workbench.backend.panes import ShellPane  # noqa: E402
from workbench.backend.service import Backend  # noqa: E402
from workbench.terminal.shell_g2.prototype import ShellChoice  # noqa: E402


class LiveEnvironmentAdmissionTests(FlowFixture):
    def test_the_names_are_asked_of_the_live_shell_and_a_late_export_starts_the_run(self):
        exported: set[str] = set()
        asked: list[list[str]] = []

        def live(names):
            asked.append(list(names))
            return set(exported) & set(names)

        self.flow._experiment.environment_names = live
        result = self.new_experiment()
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"] == "environment_missing:PATH"))
        self.assertEqual(self.runs, [])
        exported.add("PATH")  # the user exports it in the host terminal after the start
        self.assertTrue(wait_until(lambda: len(self.runs) == 1 and self.flow.task_view()["status"] == "finished"))
        self.assertIn(["PATH"], asked)

    def test_an_unobservable_environment_holds_as_unverified(self):
        self.flow._experiment.environment_names = lambda names: None
        self.new_experiment()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"] == "environment_unverified"))
        self.assertEqual(self.runs, [])

    def test_a_start_held_for_its_environment_leaves_the_host_terminal_free(self):
        self.flow._experiment.environment_names = lambda names: set()
        self.new_experiment()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"] == "environment_missing:PATH"))
        self.assertIsNone(self.flow.experiment_host_activity(), "a held start reported as host activity")
        samples = []
        for _ in range(60):
            samples.append(self.flow._host_gate.owner)
            time.sleep(0.005)
        self.assertGreaterEqual(samples.count(None), 54, f"the held start keeps the host gate: {samples}")
        owner = self.flow._host_gate.acquire("terminal", self.flow.experiment_host_activity)
        if owner is not None:  # the start's check may own it for a moment: try once more
            time.sleep(0.01)
            owner = self.flow._host_gate.acquire("terminal", self.flow.experiment_host_activity)
        self.assertIsNone(owner)
        self.flow._host_gate.release("terminal")

    def test_a_start_waiting_for_the_host_shell_still_reports_activity(self):
        self.port.busy_reason = "a line is being typed in the host shell"
        self.new_experiment()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"] == "host_terminal_busy"))
        self.assertIsNotNone(self.flow.experiment_host_activity())


class CancelWhilePausedTests(FlowFixture):
    def pause(self):
        self.controller.automation = {"state": "paused"}

    def test_a_run_finished_while_paused_can_be_cancelled(self):
        self.gates.exit.clear()
        result = self.new_experiment()
        task_id = result["task_id"]
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        self.pause()
        self.gates.exit.set()  # the host command ends while paused
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "waiting_report"
                                   and self.flow.task_view()["held_reason"] == "paused"))
        cancelled = self.to_worker({"task_id": task_id, "kind": "experiment", "cancel": True, "message": "stop it"})
        self.assertNotEqual(cancelled.get("reason"), "paused", cancelled)
        self.assertIn(cancelled["status"], ("cancel_requested", "cancelled"), cancelled)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "closed"))
        view = self.flow.task_view()
        self.assertEqual(view["closed_reason"], "cancelled", view)
        self.assertEqual(self.gates.judged, 0, "no analysis started while paused")
        self.assertIsNone(self.current_run(task_id), "the run was closed by the cancel")

    def test_other_requests_stay_held_while_paused(self):
        self.pause()
        self.assertEqual(self.new_work(), {"status": "held", "reason": "paused"})


class ResumeAfterFinishTests(AutomationFixture):
    def test_a_run_that_finished_while_paused_resumes_without_replay(self):
        run = self.start_experiment(command="printf HOST_STARTED; sleep 1; printf PASS > outcome.txt")
        self.controller.experiment_started(run)
        self.tick()
        self.controller.request_pause()
        self.assertTrue(self.controller.wait_idle(15))
        self.assertTrue(self.status()["paused"])
        deadline = time.monotonic() + 20
        record = {}
        while time.monotonic() < deadline:  # TaskFlow keeps collecting while paused
            record = run.collect(timeout=0.3, paused=True)
            if record.get("shell_state") == "exited" and record.get("exit_confirmed"):
                break
        self.assertEqual((record.get("shell_state"), record.get("exit_confirmed")), ("exited", True), record)
        self.assertEqual(self.repository.get_current_run(run.task_id)["run_id"], run.run_id, "still current")
        self.controller.request_resume()
        self.assertTrue(self.controller.wait_idle(20))
        status = self.status()
        self.assertEqual(status["resume"]["outcome"], "resumed", (status["resume"], self.logs[-5:]))
        self.assertIn("run_finished_while_paused", status["resume"]["reason"])
        self.assertFalse(status["paused"])
        self.assertIn("resume", self.manager.frames)
        self.assertIn("resume", self.worker.frames)
        self.assertNotEqual(self.tick(120.0)["outcome"], "admitted")
        self.assertEqual(self.worker.reviews(), [], "no review for the finished run")
        lines = (Path(run.record_dir) / "resume-reconcile.jsonl").read_text().splitlines()
        entry = json.loads(lines[-1])
        self.assertEqual((entry["event"], entry["run_id"], entry["exit_status"], entry["replayed"]),
                         ("resumed_after_finish", run.run_id, 0, False))


class LiveShellProbeTests(unittest.TestCase):
    def check(self, kind, executable):
        if not Path(executable).exists():
            self.skipTest(f"{executable} missing")
        pane = ShellPane(ShellChoice(kind, executable), {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"})
        self.addCleanup(pane.close)
        stop = time.monotonic() + 10
        while pane.state["parent_mode"] != "manual_prompt" and time.monotonic() < stop:
            pane.pump()
            time.sleep(0.02)
        self.assertEqual(pane.exported_names(["PATH", "WB_LATE"]), {"PATH"})
        events = len(pane.shell._transport.events)
        self.assertEqual(pane.exported_names(["WB_LATE"]), set())
        self.assertEqual(len(pane.shell._transport.events), events, "an unchanged shell is not asked again")
        self.assertIsNone(pane.admit(b"export WB_LATE=1; WB_LOCAL=2\r"))
        stop = time.monotonic() + 5
        while time.monotonic() < stop:
            pane.pump()
            if pane.exported_names(["WB_LATE", "WB_LOCAL"]) == {"WB_LATE"}:
                break
            time.sleep(0.05)
        self.assertEqual(pane.exported_names(["WB_LATE", "WB_LOCAL"]), {"WB_LATE"}, "a local is not exported")
        self.assertIsNone(pane.exported_names(["NOT;A NAME"]), "never typed")
        self.assertIsNone(pane.automation_hold, "the probe released its input hold")

    def test_bash(self):
        self.check("bash", "/usr/bin/bash")

    def test_dash(self):
        self.check("sh", "/usr/bin/dash")


class UsageViewTests(unittest.TestCase):
    def backend(self, usage=None):
        backend = Backend.__new__(Backend)
        backend.automation_loop = None if usage is None else type("Loop", (), {"usage": lambda self: usage})()
        return backend

    def test_counts_and_unknown_model_usage(self):
        task = {"task_id": "t1", "runs_started": 3, "retry_limit": 3}
        automation = {"review": {"applies": True, "review_count": 2}}
        view = self.backend()._usage_view(task, automation)
        self.assertEqual(view, {"task_id": "t1", "runs_started": 3, "retries_used": 2, "retry_limit": 3,
                                "review_count": 2,
                                "model": {"tokens_observed": "unknown", "tokens_estimated": "unknown"},
                                "model_known": False})
        known = self.backend({"tokens_observed": 1234, "tokens_estimated": "unknown"})._usage_view(task, automation)
        self.assertEqual((known["model"]["tokens_observed"], known["model_known"]), (1234, True))

    def test_no_task_and_no_review(self):
        view = self.backend()._usage_view(None, {"review": {"applies": False, "review_count": 0}})
        self.assertEqual((view["runs_started"], view["retries_used"], view["review_count"]), (None, None, None))

    def test_the_snapshot_and_status_text_carry_it(self):
        source = Path(cli.__file__).with_name("service.py").read_text()
        self.assertIn('"usage": self._usage_view(task, automation)', source)
        import io
        stream = io.StringIO()
        cli._print_summary({"backend": {"pid": 1, "data_dir": "/d"}, "phase": "ready", "panes": {},
                            "usage": {"task_id": "t1", "runs_started": 2, "retries_used": 1, "retry_limit": 3,
                                      "review_count": 4, "model": {"tokens_observed": "unknown",
                                                                   "tokens_estimated": "unknown"}}}, stream)
        self.assertIn("사용량: Task t1 run 2 (재시도 1/3) · 60s 점검 4 · 모델 미확인", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
