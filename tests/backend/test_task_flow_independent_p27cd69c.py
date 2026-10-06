"""C-D69 (5) independent (p27-cd69-stuck-test-01): experiment Tasks and the host shell.

Derived from DECISIONS.md C-D69 (5): an experiment command too long for the host shell is refused when the
Task is created (nothing created, nothing typed) and the largest accepted one really runs; any experiment
start failure after Workbench typed ``wb-handoff`` gives the shell back to the user (a manual command and the
next run both work); a ``wb-handoff`` the user typed (C-D58) is not taken away. Real TaskWorkflow on a real
backend ShellPane (bash and dash) through HostShellPort; fake mailboxes, no OMP, no provider.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from workbench.backend import flow as flow_module
from workbench.backend.flow import HandoffService
from workbench.backend.flow_tasks import ExperimentPorts, TaskFlow
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.contracts.v1 import ActorRole
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.workflow.run import TaskWorkflow

import test_task_flow as fx

LIMIT = 4096
PAUSED = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": True, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}
CANCELLED = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": True, "metadataHealthy": True, "approvalValid": True}}


class _ExperimentShell(unittest.TestCase):
    CHOICE: ShellChoice

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p27cd69c-exp-", dir="/tmp")
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        fx.git(self.source, "init", "-q")
        fx.git(self.source, "config", "user.email", "fixture@example.invalid")
        fx.git(self.source, "config", "user.name", "CD69 Fixture")
        (self.source / "tracked.txt").write_text("base\n")
        fx.git(self.source, "add", "tracked.txt")
        fx.git(self.source, "commit", "-qm", "baseline")
        self.commit = fx.git(self.source, "rev-parse", "HEAD")
        for name in ("workflow", "worktrees", "runs", "home"):
            (self.root / name).mkdir(mode=0o700)
        self.home = self.root / "home"
        self.db = self.root / "tasks.sqlite3"
        self.pane = ShellPane(self.CHOICE, {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8"})
        self.ui = bytearray()
        self.stop = threading.Event()
        self.loop = threading.Thread(target=self._loop, daemon=True)
        self.loop.start()
        self.automation_now = fx.AUTOMATION
        with TaskRepository(self.db):
            pass
        self.service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=fx.FakeMailbox())
        ports = ExperimentPorts(host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
                                make_workflow=self.make_workflow, automation=lambda: fx.AUTOMATION,
                                environment_names=lambda: {"PATH"}, worktrees_root=self.root / "worktrees",
                                artifacts_root=self.root / "runs")
        self.flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl",
                             repository_factory=lambda: TaskRepository(self.db), handoffs=self.service,
                             omp_idle=lambda role: True, experiment=ports, poll_interval=0.05, collect_slice=0.2)
        self.service.configure(policy=self.flow)
        self.service.start()
        self.flow.start()
        self.assertTrue(fx.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        self.type(f"cd {self.home}\r".encode())
        self.assertTrue(fx.wait_until(lambda: self.pane.cwd() == str(self.home), 5))
        self.assertTrue(fx.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 5))

    def _loop(self):
        while not self.stop.is_set():
            for chunk in self.pane.pump():
                self.ui.extend(chunk.data)
            time.sleep(0.01)

    def tearDown(self):
        self.flow.close()
        self.service.close()
        self.stop.set()
        self.loop.join(5)
        self.pane.close()
        self.tmp.cleanup()

    def make_workflow(self, repository):
        return TaskWorkflow(repository, fx.PublicMailboxFixture(repository),
                            automation_source=lambda: self.automation_now)

    def type(self, data):
        self.assertIsNone(self.pane.admit(data))

    def spec(self, command):
        return {"goal": "fixture run", "paths": ["outcome.txt"], "execution": {
            "source": str(self.source), "commit": self.commit, "command": command,
            "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
            "environment": ["PATH"], "shell": self.CHOICE.kind}}

    def to_worker(self, command, call_id=None):
        return self.service.handle(ActorRole.MANAGER, fx.request("to_worker", {
            "kind": "experiment", "message": "run the fixture", "spec": self.spec(command)},
            call_id or f"m-{uuid4().hex[:6]}"))

    def finished(self):
        self.assertTrue(fx.wait_until(lambda: (self.flow.task_view() or {}).get("status") == "finished", 40),
                        self.flow.task_view())
        return self.flow.task_view()["last_result"]

    def assert_returned(self):
        self.assertTrue(fx.wait_until(lambda: self.pane.state["input_owner"] == "user"
                                      and self.pane.state["parent_mode"] == "manual_prompt", 10), self.pane.state)
        self.assertTrue(fx.wait_until(lambda: self.pane.automation_hold is None, 5))
        self.assertTrue(fx.wait_until(lambda: self.pane.cwd() == str(self.home), 5), "the user's directory is back")
        marker = f"manual-{uuid4().hex[:6]}"
        self.type(f"echo {marker}-$((6*7))\r".encode())
        self.assertTrue(fx.wait_until(lambda: f"{marker}-42".encode() in self.ui, 5), "a manual command runs")
        self.assertTrue(fx.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 5))

    def assert_next_run_succeeds(self):
        result = self.to_worker("printf 'PASS next\\n'; printf PASS > outcome.txt")
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(fx.wait_until(lambda: (self.flow.task_view() or {}).get("status") == "finished"
                                      and ((self.flow.task_view() or {}).get("last_result") or {}).get("judgment")
                                      == "success", 40), self.flow.task_view())
        self.assert_returned()

    # -- (b) checked at Task creation -------------------------------------------------------------
    def test_creation_boundary_largest_runs_one_more_is_rejected(self):
        prefix = "printf 'PASS boundary\\n'; printf PASS > outcome.txt; : "
        n = 1
        while flow_module.experiment_request_bytes(prefix + "b" * (n + 1)) <= LIMIT:
            n += 1
        before = len(self.ui)
        for over in (prefix + "b" * (n + 1), prefix + "가" * 700,
                     ["/bin/sh", "-c", prefix + "b" * (n + 200)]):
            with self.subTest(kind=type(over).__name__):
                result = self.to_worker(over)
                self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
                error = next(e for e in result["errors"] if e.startswith("spec.execution.command"))
                self.assertIn("script file", error)
                self.assertIn(str(LIMIT), error)
        self.assertIsNone(self.flow.active_task(), "no Task was created")
        time.sleep(0.3)
        self.assertNotIn(b"wb-handoff", bytes(self.ui[before:]), "nothing was typed")
        self.assertEqual((self.pane.state["input_owner"], self.pane.state["parent_mode"]), ("user", "manual_prompt"))
        result = self.to_worker(prefix + "b" * n)
        self.assertEqual(result["status"], "dispatched", result)
        self.assertEqual(self.finished().get("judgment"), "success", self.flow.task_view())
        self.assertIn(b"PASS boundary", bytes(self.ui))
        self.assert_returned()

    # -- (a) start failures after Workbench typed wb-handoff ------------------------------------------
    def check_failed_start(self, command="printf PASS > outcome.txt"):
        before = len(self.ui)
        result = self.to_worker(command)
        self.assertEqual(result["status"], "dispatched", result)
        outcome = self.finished()
        self.assertEqual(outcome["outcome"], "start_failed", self.flow.task_view())
        self.assertIn(b"wb-handoff", bytes(self.ui[before:]), "the failure came after Workbench typed wb-handoff")
        self.assert_returned()
        return outcome

    def test_pipe_refusal_after_handoff(self):
        with mock.patch.object(flow_module, "experiment_request_bytes", lambda command: 0):
            self.check_failed_start("printf PASS > outcome.txt; : " + "x" * 5000)
        self.assert_next_run_succeeds()

    def test_claim_failure(self):
        with mock.patch.object(HostShellPort, "claim_manager", side_effect=RuntimeError("forced claim failure")):
            self.check_failed_start()
        self.assert_next_run_succeeds()

    def test_submit_failure_after_claim(self):
        with mock.patch.object(HostShellPort, "submit", side_effect=OSError("control pipe broken")):
            self.check_failed_start()
        self.assert_next_run_succeeds()

    def test_pause_and_cancel_after_claim(self):
        original = HostShellPort.claim_manager
        for state in (PAUSED, CANCELLED):
            with self.subTest(paused=state["payload"]["paused"]):
                def claim(port, _state=state):
                    result = original(port)
                    self.automation_now = _state  # the user pauses / cancels while the start is in flight
                    return result

                with mock.patch.object(HostShellPort, "claim_manager", claim):
                    self.check_failed_start()
                self.automation_now = fx.AUTOMATION
        self.assert_next_run_succeeds()

    # -- C-D58 -------------------------------------------------------------------------------------
    def test_user_initiated_handoff_is_not_taken_away(self):
        self.type(b"wb-handoff\r")
        self.assertTrue(fx.wait_until(lambda: self.pane.state["parent_mode"] == "control_wait", 5), self.pane.state)
        result = self.to_worker("printf 'PASS late\\n'; printf PASS > outcome.txt")
        self.assertEqual(result["status"], "dispatched", result)
        time.sleep(2.0)
        state = self.pane.state
        self.assertEqual((state["input_owner"], state["parent_mode"]), ("user", "control_wait"), state)
        self.assertFalse(state["takeover_requested"], "Workbench did not take the user's handoff back")
        self.assertNotEqual((self.flow.task_view() or {}).get("status"), "finished", self.flow.task_view())
        self.assertNotIn(b"PASS late", bytes(self.ui))
        # the user takes the prompt back; then the held run starts on its own
        self.pane.request_takeover()
        self.assertTrue(fx.wait_until(lambda: (self.flow.task_view() or {}).get("status") == "finished", 40),
                        self.flow.task_view())
        self.assertEqual(self.flow.task_view()["last_result"].get("judgment"), "success", self.flow.task_view())
        self.assert_returned()


class BashExperimentTests(_ExperimentShell):
    CHOICE = ShellChoice("bash", "/usr/bin/bash")


class DashExperimentTests(_ExperimentShell):
    CHOICE = ShellChoice("sh", "/usr/bin/dash")


del _ExperimentShell

if __name__ == "__main__":
    unittest.main()
