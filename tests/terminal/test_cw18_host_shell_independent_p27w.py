"""CW-18 independent verification (p27-cw18-test-01): experiment runs on the REAL product host shell.

A real backend ``ShellPane`` (bash, rcfile hooks) pumped like the backend loop, ``HostShellPort`` and the
real ``TaskWorkflow`` under ``TaskFlow`` (fake public mailbox, no OMP, no provider, no network).

Expectations (C-D65 (2), C-D66 (3), C-D58, CW-18 U2/U4 required behaviour):
- Workbench types a run's start only when the shell is user-owned at a clean prompt with no job; a typed
  partial line, a background or suspended job, a foreground program, a manager-owned shell or a held
  handoff keep it busy (``held:host_terminal_busy``) and the user's keystrokes are never overwritten;
- while Workbench holds the shell for the start, user input is refused and afterwards accepted again;
- two consecutive runs on the same shell; the shell is given back to the user after each run;
- a cancel while the host command runs never kills it: it finishes, then the Task closes as cancelled;
- C-D58: a held wb-handoff never strands the user, and after the user cleans up the shell is usable for
  the next automatic run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

from workbench.backend.flow import HandoffService
from workbench.backend.flow_tasks import ExperimentPorts, TaskFlow
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.contracts.ui_v1 import Reason
from workbench.contracts.v1 import ActorRole
from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.workflow.run import TaskWorkflow

AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=15,
                          check=True).stdout.strip()


class PublicMailbox:
    def __init__(self, repository):
        self.repository = repository

    def create_message(self, task_id, revision, run_id, sender, target, kind, payload, *, in_reply_to_message_id=None):
        return SimpleNamespace(message_id=str(uuid4()), task_id=task_id, revision=revision, run_id=run_id,
                               sender=sender, target=target, kind=kind, payload=payload)

    def deliver(self, message, **_):
        return SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)


class NullHandoffMailbox:
    def create_message(self, *args, **kwargs):
        return SimpleNamespace(message_id=str(uuid4()), session_id=str(uuid4()), session_generation=1)

    def deliver(self, message, **_):
        return SimpleNamespace(status=MailboxStatus.OMP_PROCESSED, details={})


class HostShellHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p27w-host-", dir="/tmp")
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.email", "p27w@example.invalid")
        git(self.source, "config", "user.name", "p27w")
        (self.source / "base.txt").write_text("base\n")
        git(self.source, "add", "base.txt")
        git(self.source, "commit", "-qm", "base")
        self.commit = git(self.source, "rev-parse", "HEAD")
        for name in ("workflow", "worktrees", "runs"):
            (self.root / name).mkdir(mode=0o700)
        self.db = self.root / "tasks.sqlite3"
        TaskRepository(self.db).close()
        self.pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "LANG": "C.UTF-8", "TERM": "xterm"})
        self.ui = bytearray()
        self.ui_lock = threading.Lock()
        self.stop = threading.Event()
        self.loop = threading.Thread(target=self._pump, daemon=True)
        self.loop.start()
        self.flow = None
        self.service = None
        self.started_pids: list[int] = []
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"), "first prompt")

    def _pump(self):
        while not self.stop.is_set():
            for chunk in self.pane.pump():
                with self.ui_lock:
                    self.ui.extend(chunk.data)
            time.sleep(0.01)

    def tearDown(self):
        if self.flow is not None:
            self.flow.close()
        if self.service is not None:
            self.service.close()
        self.stop.set()
        self.loop.join(5)
        self.pane.close()
        for pid in self.started_pids:  # only processes this test started (identity: our own pid files)
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------------------------
    def state(self):
        return self.pane.state

    def until(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return bool(predicate())

    def type(self, data: bytes):
        refused = self.pane.admit(data)
        self.assertIsNone(refused, (data, refused))

    def screen(self) -> bytes:
        with self.ui_lock:
            return bytes(self.ui)

    def port(self):
        port = HostShellPort(self.pane, lambda: self.pane)
        self.addCleanup(port.detach)
        return port

    def start_flow(self):
        self.service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=NullHandoffMailbox())
        ports = ExperimentPorts(host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
                                make_workflow=lambda repository: TaskWorkflow(repository, PublicMailbox(repository)),
                                automation=lambda: AUTOMATION, environment_names=lambda: {"PATH"},
                                worktrees_root=self.root / "worktrees", artifacts_root=self.root / "runs")
        self.flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl",
                             repository_factory=lambda: TaskRepository(self.db), handoffs=self.service,
                             omp_idle=lambda role: True, experiment=ports, poll_interval=0.05, collect_slice=0.2)
        self.service.configure(policy=self.flow, active_task=self.flow.active_task)
        self.service.start()
        self.flow.start()

    def experiment(self, command, call_id, task_id=None, run=False, out="outcome.txt"):
        args = {"kind": "experiment", "message": f"run {call_id}"}
        if task_id is None:
            args["spec"] = {"goal": "fixture", "paths": [out], "execution": {
                "source": str(self.source), "commit": self.commit, "command": command,
                "criteria": {"log_contains": "PASS", "result_file": out, "result_contains": "PASS"},
                "environment": ["PATH"], "shell": "bash"}}
        else:
            args.update(task_id=task_id, run=run)
        return self.service.handle(ActorRole.MANAGER, {
            "request_id": str(uuid4()), "tool_call_id": call_id, "tool": "to_worker", "args": args,
            "session_id": str(uuid4()), "generation": 1})

    def view(self):
        return self.flow.task_view() or {}

    def current_run(self, task_id):
        with TaskRepository(self.db) as repository:
            return repository.get_current_run(task_id)

    def given_back(self):
        state = self.state()
        return state["input_owner"] == "user" and state["parent_mode"] == "manual_prompt" \
            and self.pane.automation_hold is None


class IdleOnlyTests(HostShellHarness):
    def test_partial_line_keeps_the_shell_busy_and_is_never_overwritten(self):
        self.start_flow()
        self.type(b"echo p27w-typed-by-user")
        port = self.port()
        self.assertTrue(self.until(lambda: port.busy() is not None), "a typed line must keep the shell busy")
        result = self.experiment("printf 'PASS\\n'; printf PASS > outcome.txt", "m-1")
        self.assertEqual(result["status"], "dispatched")
        self.assertTrue(self.until(lambda: self.view().get("held_reason") == "host_terminal_busy"), self.view())
        time.sleep(0.5)
        self.assertIsNone(self.current_run(result["task_id"]), "nothing starts while the user types")
        self.assertIsNone(self.pane.automation_hold)
        self.type(b"\r")  # the user submits their own line
        self.assertTrue(self.until(lambda: self.view().get("status") == "finished", 30), self.view())
        self.assertIn(b"p27w-typed-by-user", self.screen())
        # the user's command really ran (it was not replaced by Workbench's command)
        self.assertTrue(self.until(lambda: self.screen().count(b"p27w-typed-by-user") >= 2), self.screen()[-400:])
        self.assertEqual(self.pane.dropped_input_bytes, 0)
        self.assertTrue(self.until(self.given_back), self.state())

    def background_job(self, seconds):
        pid_file = self.root / f"bg{seconds}.pid"
        self.type(f"sleep {seconds} & echo $! > {pid_file}\r".encode())
        self.assertTrue(self.until(lambda: pid_file.exists() and pid_file.read_text().strip().isdecimal()))
        self.started_pids.append(int(pid_file.read_text()))
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"))
        time.sleep(0.5)
        return self.started_pids[-1]

    def test_background_job_at_a_plain_prompt_keeps_the_shell_busy(self):
        # C-D65 (2) / C-D66 (3): Workbench starts a run only when there is NO job in the user's shell.
        port = self.port()
        self.background_job(617)
        self.assertTrue(self.until(lambda: port.busy() is not None, 3),
                        f"a user background job must keep the host shell busy; held={self.state()['held_reasons']}")
        self.assertIsNotNone(port.hold("run"))
        self.assertIsNone(self.pane.automation_hold)

    def test_experiment_never_starts_while_a_user_background_job_runs(self):
        self.start_flow()
        self.background_job(621)
        before = len(self.screen())
        result = self.experiment("printf 'PASS\\n'; printf PASS > outcome.txt", "m-bg")
        self.assertEqual(result["status"], "dispatched")
        time.sleep(2.0)
        typed = self.screen()[before:]
        self.assertNotIn(b"worktrees", typed, f"Workbench typed into the user's shell while a job runs: {typed!r}")
        self.assertNotIn(b"wb-handoff", typed)
        self.assertEqual((self.state()["input_owner"], self.state()["parent_mode"]), ("user", "manual_prompt"),
                         f"the user's shell was left in {self.state()['parent_mode']} {self.state()['held_reasons']}")
        self.assertIsNone(self.current_run(result["task_id"]))
        self.assertEqual((self.view().get("status"), self.view().get("held_reason"), self.view().get("runs_started")),
                         ("dispatched", "host_terminal_busy", 0), self.view())

    def test_suspended_job_keeps_the_shell_busy(self):
        port = self.port()
        self.type(b"sleep 618\r")
        time.sleep(0.5)
        self.assertIsNotNone(port.busy(), "a foreground program keeps it busy")
        self.type(b"\x1a")  # Ctrl-Z: a suspended job at the prompt
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"))
        time.sleep(0.5)
        busy = port.busy()
        self.type(b"kill -9 %1; wait\r")
        self.assertIsNotNone(busy, f"a suspended job must keep the host shell busy; held={self.state()['held_reasons']}")

    def test_foreground_program_keeps_the_shell_busy_and_idle_after(self):
        port = self.port()
        self.type(b"sleep 1.5\r")
        time.sleep(0.4)
        self.assertIsNotNone(port.busy(), "a foreground program keeps it busy")
        self.assertIsNotNone(port.hold("run"))
        self.assertTrue(self.until(lambda: port.busy() is None, 10), port.busy())

    def test_manager_owned_shell_and_held_handoff_keep_it_busy(self):
        port = self.port()
        self.type(b"wb-handoff\r")
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "control_wait"))
        self.assertIsNotNone(port.busy(), "a handoff in progress keeps it busy")
        self.pane.handoff()
        self.assertEqual(self.state()["input_owner"], "manager")
        self.assertIsNotNone(port.busy())
        self.assertIn("manager", port.busy())
        self.pane.request_takeover()
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"))
        self.pane.confirm_takeover()
        self.assertTrue(self.until(lambda: port.busy() is None, 10), port.busy())

    def test_user_input_refused_while_held_and_accepted_after(self):
        port = self.port()
        self.assertTrue(self.until(lambda: port.busy() is None))
        self.assertIsNone(port.hold("Workbench is starting the run"))
        refused = self.pane.admit(b"echo should-not-run\r")
        self.assertEqual(refused[0], Reason.HOST_SHELL_AUTOMATION)
        with self.assertRaises(Exception):
            self.pane.handoff()  # no wb-handoff/manager claim while Workbench holds the shell
        self.assertIsNotNone(port.hold("second"), "the hold is exclusive")
        port.release_hold()
        self.type(b"echo p27w-after-hold\r")
        self.assertTrue(self.until(lambda: b"p27w-after-hold" in self.screen().split(b"echo p27w-after-hold", 1)[-1]))
        self.assertNotIn(b"should-not-run", self.screen())


class ConsecutiveRunsTests(HostShellHarness):
    def test_two_runs_on_the_same_shell_with_user_activity_between(self):
        self.start_flow()
        first = self.experiment("printf 'PASS one\\n'; printf PASS > outcome.txt", "m-1")
        self.assertTrue(self.until(lambda: self.view().get("status") == "finished", 30), self.view())
        self.assertTrue(self.until(self.given_back, 10), self.state())
        run_one = self.view()["last_result"]["run_id"]
        record = json.loads((self.root / "runs" / run_one / "run.json").read_text())
        self.assertEqual(record["parent_pid"], self.pane.pid)
        # the user types a partial line before the manager re-runs
        self.type(b"echo p27w-between")
        again = self.experiment(None, "m-2", task_id=first["task_id"], run=True)
        self.assertEqual(again["status"], "dispatched", again)
        self.assertTrue(self.until(lambda: self.view().get("held_reason") == "host_terminal_busy"), self.view())
        self.type(b"\r")
        self.assertTrue(self.until(lambda: self.view().get("status") == "finished"
                                   and self.view()["last_result"]["run_id"] != run_one, 30), self.view())
        result = self.view()["last_result"]
        self.assertEqual((result["judgment"], result["run_closed"]), ("success", True), result)
        record = json.loads((self.root / "runs" / result["run_id"] / "run.json").read_text())
        self.assertEqual(record["parent_pid"], self.pane.pid, "the same product shell ran the second run")
        self.assertTrue(self.until(self.given_back, 10))
        self.assertEqual(self.pane.dropped_input_bytes, 0)
        self.assertFalse(self.pane.exited())

    def test_the_users_working_directory_is_unchanged_after_a_run(self):
        # The shell is returned to the user: Workbench's own "cd <worktree>" must not stay behind.
        self.start_flow()
        self.type(b"cd /tmp\r")
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"))
        time.sleep(0.3)
        self.experiment("printf 'PASS\\n'; printf PASS > outcome.txt", "m-cwd")
        self.assertTrue(self.until(lambda: self.view().get("status") == "finished", 30), self.view())
        self.assertTrue(self.until(self.given_back, 10))
        before = len(self.screen())
        self.type(b"echo CWD=$(pwd)\r")
        self.assertTrue(self.until(lambda: b"CWD=/" in self.screen()[before:].split(b"\n", 1)[-1]))
        line = self.screen()[before:].split(b"\n", 1)[-1]
        cwd = line.split(b"CWD=", 1)[1].split(b"\r", 1)[0]
        self.assertEqual(cwd, b"/tmp", "the user's shell was left in the Workbench worktree")

    def test_cancel_during_the_host_command_never_kills_it(self):
        self.start_flow()
        marker = "finished-by-itself.txt"
        result = self.experiment(f"sleep 2; printf done > {marker}; printf 'PASS\\n'; printf PASS > outcome.txt", "m-1")
        task_id = result["task_id"]
        self.assertTrue(self.until(lambda: self.view().get("status") == "running", 20), self.view())
        cancel = self.service.handle(ActorRole.MANAGER, {
            "request_id": str(uuid4()), "tool_call_id": "m-cancel", "tool": "to_worker",
            "args": {"kind": "experiment", "message": "stop", "task_id": task_id, "cancel": True},
            "session_id": str(uuid4()), "generation": 1})
        self.assertEqual(cancel["status"], "cancel_requested", cancel)
        self.assertTrue(self.until(lambda: self.view().get("status") == "closed", 30), self.view())
        self.assertEqual(self.view()["closed_reason"], "cancelled")
        worktree = next((self.root / "worktrees").iterdir())
        self.assertEqual((worktree / marker).read_text(), "done", "the host command was not killed")
        self.assertIsNone(self.current_run(task_id))
        self.assertNotIn("judgment", self.view()["last_result"] or {})
        self.assertTrue(self.until(self.given_back, 10), self.state())


class HeldHandoffTests(HostShellHarness):
    def test_held_handoff_user_recovers_and_the_next_run_can_start(self):
        """C-D58 + root-adjudication-p27-cw18-needs-review, at the product pane."""
        port = self.port()
        pid_file = self.root / "job.pid"
        self.type(f"sleep 619 & echo $! > {pid_file}\r".encode())
        self.assertTrue(self.until(lambda: pid_file.exists() and pid_file.read_text().strip().isdecimal()))
        self.started_pids.append(int(pid_file.read_text()))
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"))
        self.type(b"wb-handoff\r")
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "control_wait"
                                   and "manual_jobs" in self.state()["held_reasons"]), self.state())
        with self.assertRaises(Exception):
            self.pane.handoff()  # automatic acceptance stays held
        self.assertIsNotNone(port.hold("run"))
        # not a dead end: the user takes the prompt back, confirms, and types
        self.pane.request_takeover()
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"), self.state())
        confirmed = self.pane.confirm_takeover()
        self.assertEqual(confirmed["input_owner"], "user")
        self.type(b"kill %1; wait\r")
        self.assertTrue(self.until(lambda: not Path(f"/proc/{self.started_pids[-1]}").exists()))
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"))
        # Root adjudication p27-cw18-needs-review: the job-caused latch is cleared only by a fresh verified
        # wb-handoff with an empty jobs probe; until then automation stays held (fail-safe, documented).
        self.assertIsNotNone(port.busy())
        self.type(b"wb-handoff\r")
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "control_wait"
                                   and "manual_jobs" not in self.state()["held_reasons"]), self.state())
        self.assertEqual(self.pane.handoff()["input_owner"], "manager")
        # the user takes the shell back; now it is idle for the next automatic run
        self.pane.request_takeover()
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"))
        self.pane.confirm_takeover()
        self.assertTrue(self.until(lambda: port.busy() is None, 10),
                        f"still busy after clean-up: {port.busy()} {self.state()['held_reasons']}")
        self.assertEqual(self.pane.dropped_input_bytes, 0)

    def test_unknown_residue_latch_is_not_cleared_by_cleanup(self):
        port = self.port()
        self.type(b"wb-handoffx\x7f\r")  # an edited line: unknown residue, never clean
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "control_wait"), self.state())
        with self.assertRaises(Exception):
            self.pane.handoff()
        self.pane.request_takeover()
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"))
        self.pane.confirm_takeover()
        marker = self.root / "typed"
        self.type(f"printf ok > {marker}\r".encode())  # not a dead end: manual input works
        self.assertTrue(self.until(lambda: marker.exists() and self.state()["parent_mode"] == "manual_prompt"))
        self.type(b"wb-handoff\r")
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "control_wait"), self.state())
        self.assertEqual(self.pane.handoff()["input_owner"], "manager")


if __name__ == "__main__":
    unittest.main()
