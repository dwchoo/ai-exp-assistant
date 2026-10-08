"""CW-16 fix-02 (review-01 P2-1, P2-2; no OMP, no provider).

- P2-1: a cancel while paused queues the worker's cancel notice through the pause (it was never submitted, so the
  delivery after the resume is not a replay); the result says the worker is told after the resume, and exactly
  one notice reaches the worker once Workbench resumes.
- P3-1: the host shell's exported names come from its own prompt hook (a private names file): nothing is typed,
  ``$?`` and history are untouched, a user's keystroke is never refused for it, nothing is asked again per command.
- P3-2: the usage source is the real WorkerReviewScheduler.
- P2-2: the resume of a run whose host command ended while paused keeps the run bound until ``run_ended``: a new
  pause reaches both OMPs again, a second resume reconciles again and the shutdown stop fence still covers it.
"""
from __future__ import annotations

from pathlib import Path
import sys
import os
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from test_automation_loop import AutomationFixture  # noqa: E402
from test_task_flow import FlowFixture, wait_until  # noqa: E402

from workbench.backend.panes import ShellPane  # noqa: E402
from workbench.contracts.v1 import ActorRole  # noqa: E402
from workbench.terminal.shell_g2.prototype import ShellChoice  # noqa: E402


class CancelNoticeWhilePausedTests(FlowFixture):
    def notices(self, task_id):
        return [m for m in self.mailbox.to(ActorRole.WORKER)
                if m.task_id == task_id and m.payload.get("cancel") is True]

    def delivered(self, task_id):
        return [m for m in self.notices(task_id) if m.message_id in self.mailbox.delivered]

    def test_pause_cancel_resume_delivers_exactly_one_notice(self):
        task_id = self.running_work()["task_id"]
        self.controller.automation = {"state": "paused"}
        result = self.to_worker({"task_id": task_id, "kind": "work", "cancel": True, "message": "stop it"})
        self.assertEqual(result["status"], "cancelled", result)
        self.assertIs(result["worker_notified"], False, "not told yet while paused")
        self.assertEqual(result["worker_notice"], "after_resume", result)
        self.assertIn("after the resume", result["detail"])
        time.sleep(0.3)
        self.assertEqual(self.delivered(task_id), [], "nothing reaches the worker while paused")
        self.assertEqual(self.flow.task_view()["status"], "closed")
        self.controller.automation = {"state": "active"}
        self.assertTrue(wait_until(lambda: len(self.delivered(task_id)) == 1), self.notices(task_id))
        time.sleep(0.3)
        self.assertEqual(len(self.delivered(task_id)), 1, "exactly one notice")
        self.assertEqual(len(self.notices(task_id)), 1, "created once (no replay)")

    def test_a_cancel_while_not_paused_is_notified_at_once(self):
        task_id = self.running_work()["task_id"]
        result = self.to_worker({"task_id": task_id, "kind": "work", "cancel": True, "message": "stop it"})
        self.assertEqual((result["status"], result["worker_notified"]), ("cancelled", True), result)
        self.assertNotIn("worker_notice", result)
        self.assertTrue(wait_until(lambda: len(self.delivered(task_id)) == 1))


class ResumeAfterFinishBindingTests(AutomationFixture):
    def finished_while_paused(self):
        run = self.start_experiment(command="printf HOST_STARTED; sleep 1; printf PASS > outcome.txt")
        self.controller.experiment_started(run)
        self.tick()
        self.controller.request_pause()
        self.assertTrue(self.controller.wait_idle(15))
        deadline = time.monotonic() + 20
        record = {}
        while time.monotonic() < deadline:
            record = run.collect(timeout=0.3, paused=True)
            if record.get("shell_state") == "exited" and record.get("exit_confirmed"):
                break
        self.assertEqual((record.get("shell_state"), record.get("exit_confirmed")), ("exited", True), record)
        return run

    def resume(self):
        self.controller.request_resume()
        self.assertTrue(self.controller.wait_idle(20))
        status = self.status()
        self.assertEqual(status["resume"]["outcome"], "resumed", (status["resume"], self.logs[-5:]))
        self.assertIn("run_finished_while_paused", status["resume"]["reason"])
        self.assertFalse(status["paused"])

    def test_the_run_stays_bound_and_a_new_pause_reaches_both_omps(self):
        run = self.finished_while_paused()
        self.resume()
        self.assertEqual(self.repository.get_current_run(run.task_id)["run_id"], run.run_id, "still current")
        bound = self.controller._bound
        self.assertIsNotNone(bound, "the run stays bound until run_ended")
        self.assertEqual(bound.run_id, run.run_id)
        self.assertEqual(self.status()["run"]["run_id"], run.run_id)
        before = (self.manager.frames.count("pause"), self.worker.frames.count("pause"))
        self.assertTrue(self.manager.paused is False and self.worker.paused is False)

        self.controller.request_pause()  # e.g. during the worker's analysis turn after the resume
        self.assertTrue(self.controller.wait_idle(15))
        self.assertTrue(self.status()["paused"])
        after = (self.manager.frames.count("pause"), self.worker.frames.count("pause"))
        self.assertEqual((after[0] - before[0], after[1] - before[1]), (1, 1), "the pause reached both OMPs")
        self.assertTrue(self.manager.paused and self.worker.paused)

        self.resume()  # reconciled again (the run is still finished and current)
        self.assertTrue(self.manager.paused is False and self.worker.paused is False)
        self.assertEqual(self.worker.reviews(), [], "no review for the finished run")

        fence = self.controller.shutdown_bound(timeout=1.0)
        self.assertIsNotNone(fence, "the shutdown stop fence still covers the run")
        self.assertEqual(fence["run_id"], run.run_id)

        self.controller.run_ended(run.run_id)  # the report was accepted: only now it is unbound
        self.assertIsNone(self.controller._bound)




class UsageSourceTests(AutomationFixture):
    """P3-2: the controller reads usage from its real WorkerReviewScheduler (no fake loop)."""

    def test_unknown_until_reported_then_the_observed_total(self):
        from workbench.observation.worker_review import UsageValue, WorkerReviewScheduler
        self.assertTrue(hasattr(WorkerReviewScheduler, "usage_snapshot"))
        self.assertIsInstance(self.controller.review, WorkerReviewScheduler)
        self.assertEqual(self.controller.usage(), {"tokens_observed": "unknown", "tokens_estimated": "unknown"})
        self.controller.review._usage.record(UsageValue(1234, "observed"))
        self.assertEqual(self.controller.usage(), {"tokens_observed": 1234, "tokens_estimated": "unknown"})


class PromptHookEnvironmentTests(unittest.TestCase):
    def open(self, kind, executable):
        if not Path(executable).exists():
            self.skipTest(f"{executable} missing")
        previous = os.umask(0o002)  # a typical user umask (0664 files) must not widen the names file
        self.addCleanup(os.umask, previous)
        home = Path("/tmp/cw16fix02-home")
        home.mkdir(exist_ok=True)
        pane = ShellPane(ShellChoice(kind, executable), {"PATH": "/usr/bin:/bin", "HOME": str(home),
                                                         "LANG": "C.UTF-8", "WB_START": "1"})
        self.addCleanup(pane.close)
        self.output = bytearray()
        self.user_lines: list[bytes] = []
        self.pane = pane
        self.settle()
        typed: list[bytes] = []
        original = pane.shell.send_user
        pane.shell.send_user = lambda data, *a, **k: (typed.append(data), original(data, *a, **k))[1]
        self.typed = typed
        return pane

    def pump(self, seconds):
        stop = time.monotonic() + seconds
        while time.monotonic() < stop:
            for chunk in self.pane.pump():
                self.output.extend(chunk.data)
            time.sleep(0.02)

    def settle(self, timeout=10):
        stop = time.monotonic() + timeout
        while time.monotonic() < stop:
            self.pump(0.05)
            events = self.pane.shell._transport.events
            if self.pane.state["parent_mode"] == "manual_prompt" and events and events[-1] == "READY":
                return
        self.fail(f"no idle prompt: {self.pane.state['parent_mode']}")

    def type(self, line):
        self.user_lines.append(line.encode() + b"\r")
        self.assertIsNone(self.pane.admit(line.encode() + b"\r"), "a user keystroke was refused")
        self.pump(0.3)
        self.settle()

    def check(self, kind, executable):
        pane = self.open(kind, executable)
        path = pane.shell._transport.env_names_path
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600", "review-02 P3: never widened by the umask")
        self.assertEqual(pane.exported_names(["PATH", "WB_START", "WB_LATE"]), {"PATH", "WB_START"})
        self.assertNotIn(b"/tmp/cw16fix02-home", path.read_bytes(), "names only, never values")
        self.type("false")
        events = len(pane.shell._transport.events)
        for _ in range(5):  # asked repeatedly while held: nothing is typed or emitted
            self.assertEqual(pane.exported_names(["WB_LATE"]), set())
        self.assertEqual(len(pane.shell._transport.events), events)
        self.assertIsNone(pane.automation_hold)
        self.type("echo RC=$?")
        self.assertIn(b"RC=1", bytes(self.output), "the user's $? is untouched")
        self.type("export WB_LATE=secret-value; WB_LOCAL=2")
        self.assertEqual(pane.exported_names(["WB_LATE", "WB_LOCAL"]), {"WB_LATE"}, "a local is not exported")
        self.assertNotIn(b"secret-value", path.read_bytes())
        self.type("unset WB_LATE")
        self.assertEqual(pane.exported_names(["WB_LATE"]), set())
        self.assertIsNone(pane.exported_names(["NOT;A NAME"]))
        if kind == "bash":
            self.type("history")
        self.assertEqual(b"".join(self.typed), b"".join(self.user_lines),
                         "only the user's keystrokes reached the terminal (the backend typed nothing)")
        self.assertNotIn(b"__b_", bytes(self.output), "no probe line in the pane or the history")
        self.assertNotIn(b"WBENV1", bytes(self.output))
        self.type("set -C")  # the user's noclobber does not break the hook
        self.type("export WB_C=1")
        self.assertEqual(pane.exported_names(["WB_C"]), {"WB_C"})

    def test_bash(self):
        self.check("bash", "/usr/bin/bash")

    def test_dash(self):
        self.check("sh", "/usr/bin/dash")

    def test_not_at_the_idle_prompt_is_unobservable(self):
        pane = self.open("bash", "/usr/bin/bash")
        self.assertIsNone(pane.admit(b"sleep 2\r"))
        self.pump(0.4)
        self.assertNotEqual(pane.state["parent_mode"], "manual_prompt")
        self.assertIsNone(pane.exported_names(["PATH"], timeout=0.1))
        self.settle()
        self.assertEqual(pane.exported_names(["PATH"]), {"PATH"})


if __name__ == "__main__":
    unittest.main()
