"""CW-18 U2b (C-D66): immediate dispatch, one Task at a time, follow-ups, cancel (no OMP, no provider).

The policy runs inside the real HandoffService with a fake mailbox; experiment
runs use a fake workflow (gated collect/judge) or the real TaskWorkflow on a
real host ShellPane through HostShellPort (idle-only start, tee, give-back,
two consecutive runs on the same shell).
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
from unittest import mock
from uuid import uuid4

from workbench.backend import flow as flow_module
from workbench.backend.flow import ANALYSIS_RULES, HandoffService
from workbench.backend.flow_tasks import HOLD_REASON as HOLD_REASON_FLOW, RETRY_LIMIT, ExperimentPorts, TaskFlow
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.backend.service import Backend
from workbench.backend.client import UiClient
from workbench.backend.ui_server import UiServer
from workbench.contracts.ui_v1 import ClientType, Reason
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.workflow.run import TaskWorkflow, WorkflowHeld

MANAGER_SESSION = str(uuid4())
WORKER_SESSION = str(uuid4())
AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
STANDING = ("user_standing_delegation", "C-D66")


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeMailbox:
    def __init__(self):
        self.created, self.delivered = [], []

    def create_message(self, task_id, revision, run_id, sender_role, target_role, kind, payload, *,
                       in_reply_to_message_id=None):
        target = ActorRole(target_role)
        session = WORKER_SESSION if target is ActorRole.WORKER else MANAGER_SESSION
        message = SimpleNamespace(message_id=str(uuid4()), session_id=session, session_generation=1,
                                  task_id=task_id, revision=revision, run_id=run_id,
                                  sender_role=ActorRole(sender_role), target_role=target, kind=MessageKind(kind),
                                  payload=dict(payload), in_reply_to_message_id=in_reply_to_message_id)
        self.created.append(message)
        return message

    def deliver(self, message, *, timeout=20):
        self.delivered.append(message.message_id)
        return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1,
                               MailboxStatus.OMP_PROCESSED, {})

    def to(self, role):
        return [message for message in self.created if message.target_role is role]


def request(tool, args, call):
    session = MANAGER_SESSION if tool == "to_worker" else WORKER_SESSION
    return {"request_id": str(uuid4()), "tool_call_id": call, "tool": tool, "args": args,
            "session_id": session, "generation": 1}


def execution(**overrides):
    value = {"source": "/src/repo", "commit": COMMIT_A, "command": "./run.sh",
             "criteria": {"log_contains": "PASS", "result_file": "out.txt", "result_contains": "PASS"},
             "environment": ["PATH"], "shell": "bash"}
    value.update(overrides)
    return value


class FakePort:
    def __init__(self):
        self.busy_reason: str | None = None
        self.held = False
        self.holds = 0
        self.detached = 0

    def busy(self):
        return self.busy_reason

    def hold(self, reason):
        if self.busy_reason is not None:
            return self.busy_reason
        self.held, self.holds = True, self.holds + 1
        return None

    def release_hold(self):
        self.held = False

    def snapshot(self):
        return {"input_owner": "user", "lifecycle": {}}

    def request_takeover(self):
        return {}

    def detach(self):
        self.detached += 1


class Gates:
    """Open by default; a test closes one to keep a run in its host command or its judgment."""

    def __init__(self):
        self.exit = threading.Event()
        self.exit.set()
        self.judge = threading.Event()
        self.judge.set()
        self.judged = 0


class FakeRun:
    def __init__(self, repository, run_id, gates):
        self.repository, self.run_id, self.task_message_id = repository, run_id, str(uuid4())
        self._record = {}
        self.gates = gates

    def collect(self, *, timeout=1, paused=False):
        if self.gates.exit.wait(timeout):
            return {"shell_state": "exited", "exit_confirmed": True}
        return {"shell_state": "running", "exit_confirmed": False}

    def judge(self):
        self.gates.judge.wait(10)
        self.gates.judged += 1
        self.repository.complete_run(self.run_id, {"judgment": "success"})
        return {"report": {"status": "omp_processed"}, "worker_judgment": {"judgment": "success"}}

    def close(self):
        pass


class FakeWorkflow:
    """Starts a run exactly as run.py authorizes it (latest scope_approved execution + unused proceed)."""

    def __init__(self, repository, log, gates=None):
        self.repository, self.log, self.gates = repository, log, gates or Gates()

    def start(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell):
        task = self.repository.get_task_spec(task_id, revision)
        approvals = [d for d in self.repository.get_decisions(task_id, revision) if d["kind"] == "scope_approved"]
        assert approvals and approvals[-1]["details"]["execution"] == task["spec"]["execution"]
        run_id = self.repository.start_run(task_id, revision, inputs={})
        self.log.append({"task_id": task_id, "revision": revision, "run_id": run_id,
                         "scope": approvals[-1]["details"], "shell": shell})
        return FakeRun(self.repository, run_id, self.gates)


class FlowController:
    """The ui_v1 controller surface of Backend for the Task view and pause/resume (Backend's own methods)."""

    pause = Backend.pause
    resume = Backend.resume
    _default_pause = Backend._default_pause
    _default_resume = Backend._default_resume

    def __init__(self):
        self.flow = None
        self.automation = {"state": "idle"}
        self.pause_hook, self.resume_hook = self._default_pause, self._default_resume

    def snapshot(self):
        return {"task": self.flow.task_view(), "worker": self.flow.worker_view(),
                "automation": dict(self.automation)}

    def replay(self):
        return []

    def on_attach(self, size):
        pass

    def on_detach(self):
        pass


class FlowFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cw18-u2b-flow-", dir="/tmp")
        self.root = Path(self.tmp.name)
        (self.root / "workflow").mkdir(mode=0o700)
        self.db = self.root / "tasks.sqlite3"
        self.mailbox = FakeMailbox()
        self.controller = FlowController()
        self.port = FakePort()
        self.gates = Gates()
        self.runs: list[dict] = []
        self.calls = 0
        self.flows: list[TaskFlow] = []
        self.services: list[HandoffService] = []
        self.open()

    def tearDown(self):
        self.gates.exit.set()
        self.gates.judge.set()
        for flow in self.flows:
            flow.close()
        for service in self.services:
            service.close()
        self.tmp.cleanup()

    def paused(self):
        return self.controller.automation.get("state") == "paused"

    def open(self, *, start=True):
        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=self.mailbox,
                                 paused=self.paused, retry_interval=0.01)
        ports = ExperimentPorts(host_shell=lambda: self.port,
                                make_workflow=lambda repository: FakeWorkflow(repository, self.runs, self.gates),
                                automation=lambda: AUTOMATION, environment_names=lambda: {"PATH"},
                                worktrees_root=self.root, artifacts_root=self.root)
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl",
                        repository_factory=lambda: TaskRepository(self.db), handoffs=service,
                        omp_idle=lambda role: True, paused=self.paused, experiment=ports,
                        poll_interval=0.02, collect_slice=0.05)
        service.configure(policy=flow, active_task=flow.active_task)
        service.start()
        if start:
            flow.start()
        self.service, self.flow = service, flow
        self.controller.flow = flow
        self.services.append(service)
        self.flows.append(flow)
        return flow

    def restart(self, *, start=True):
        self.flow.close()
        self.service.close()
        return self.open(start=start)

    def to_worker(self, args, call=None):
        self.calls += 1
        return self.service.handle(ActorRole.MANAGER, request("to_worker", args, call or f"m-{self.calls}"))

    def to_manager(self, args, call=None):
        self.calls += 1
        return self.service.handle(ActorRole.WORKER, request("to_manager", args, call or f"w-{self.calls}"))

    def decisions(self, task_id, revision):
        with TaskRepository(self.db) as repository:
            return repository.get_decisions(task_id, revision)

    def current_run(self, task_id):
        with TaskRepository(self.db) as repository:
            return repository.get_current_run(task_id)

    def run_history(self, run_id):
        with TaskRepository(self.db) as repository:
            return repository.get_run_history(run_id)

    def ledger(self):
        path = self.root / "workflow" / "tasks-flow.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def new_experiment(self, message="run the parser benchmark", **spec):
        args = {"kind": "experiment", "message": message,
                "spec": {"goal": "faster parser", "paths": ["src/"], "execution": execution(), **spec}}
        return self.to_worker(args)

    def finished_experiment(self):
        result = self.new_experiment()
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: len(self.runs) == 1 and self.flow.task_view()["status"] == "finished"))
        return result["task_id"]

    def new_work(self, message="refactor the parser module"):
        return self.to_worker({"kind": "work", "message": message,
                               "spec": {"goal": "clean parser", "paths": ["src/parser/"]}})

    def running_work(self, message="refactor the parser module"):
        result = self.new_work(message)
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"
                                   and len(self.mailbox.delivered) >= 1))
        return result

    def assert_worker_busy(self, result, task_id, kind, status):
        self.assertEqual(result["status"], "worker_busy", result)
        task = result["task"]
        self.assertEqual(set(task) >= {"task_id", "kind", "summary", "status", "since"}, True, task)
        self.assertEqual((task["task_id"], task["kind"], task["status"]), (task_id, kind, status))
        self.assertTrue(task["since"])
        self.assertIn("to_manager", result["detail"])


class DispatchTests(FlowFixture):
    def test_work_to_worker_dispatches_at_once_under_the_standing_delegation(self):
        result = self.new_work()
        self.assertEqual((result["status"], result["kind"], result["revision"]), ("dispatched", "work", 1))
        self.assertNotIn("approval_id", result)
        decisions = self.decisions(result["task_id"], 1)
        self.assertEqual([d["kind"] for d in decisions], ["scope_approved", "proceed"])
        for decision in decisions:
            self.assertEqual((decision["details"]["actor"], decision["details"]["authority"]), STANDING)
            self.assertEqual(decision["details"]["tool_call_id"], "m-1")
            self.assertNotIn("derived_from", decision["details"])
        self.assertEqual(decisions[0]["details"]["paths"], ["src/parser/"])
        self.assertTrue(wait_until(lambda: len(self.mailbox.delivered) == 1))
        task_message = self.mailbox.created[0]
        self.assertEqual((task_message.kind, task_message.target_role), (MessageKind.TASK, ActorRole.WORKER))
        self.assertEqual(task_message.payload, {"handoff": "to_worker", "kind": "work", "task_id": result["task_id"],
                                                "revision": 1, "goal": "clean parser", "paths": ["src/parser/"],
                                                "message": "refactor the parser module",
                                                "analysis": "summary",
                                                "analysis_rule": ANALYSIS_RULES["summary"]})
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        self.assertEqual(self.current_run(result["task_id"])["run_id"], self.flow.task_view()["run_id"])
        self.assertEqual(self.flow.worker_view(), {"state": "busy", "task_id": result["task_id"]})
        self.assertFalse(hasattr(self.flow, "approvals"))
        self.assertNotIn("approval", {r["type"] for r in self.ledger()})
        delegated = [r for r in self.ledger() if r["type"] == "delegated"]
        self.assertEqual((delegated[0]["actor"], delegated[0]["authority"]), STANDING)
        self.assertEqual(os.stat(self.root / "workflow" / "tasks-flow.jsonl").st_mode & 0o777, 0o600)

    def test_the_first_task_carries_the_full_manager_message_and_the_summary_stays_short(self):
        # C-D69 (2): the procedure goes in `message`; the worker must get all of it (<= 8192), not the summary.
        procedure = "\n".join(f"step {index}: run `make check-{index}` and record the exit code"
                              for index in range(1, 200))[:8192]
        self.assertGreater(len(procedure), 1024)
        result = self.new_work(procedure)
        self.assertTrue(wait_until(lambda: len(self.mailbox.delivered) == 1))
        self.assertEqual(self.mailbox.created[0].payload["message"], procedure)
        self.assertEqual(self.flow.task_view()["summary"], procedure[:1024])
        busy = self.new_work("another")
        self.assertEqual(busy["task"]["summary"], procedure[:1024])
        self.assertNotIn("message", self.flow.task_view())
        self.assertEqual(result["status"], "dispatched")

    def test_an_experiment_command_too_long_for_the_host_shell_is_rejected_at_task_creation(self):
        # C-D69 (5)(b): the same early size check as terminal; nothing is created or typed
        for command in ("echo " + "x" * 3500, ["/bin/sh", "-c", "echo " + "y" * 3500], "echo " + "한" * 600):
            with self.subTest(kind=type(command).__name__, size=len(str(command))):
                result = self.new_experiment(execution=execution(command=command))
                self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
                errors = [e for e in result["errors"] if e.startswith("spec.execution.command")]
                self.assertTrue(errors, result)
                self.assertIn("script file", errors[0])
                self.assertIn("4096", errors[0])
        self.assertIsNone(self.flow.active_task())
        self.assertEqual(self.mailbox.created, [])
        self.assertEqual(self.new_experiment(execution=execution(command="echo " + "z" * 1500))["status"],
                         "dispatched")

    def test_experiment_to_worker_starts_the_run_at_once_and_finishing_frees_the_worker(self):
        result = self.new_experiment()
        self.assertEqual((result["status"], result["kind"]), ("dispatched", "experiment"))
        self.assertTrue(wait_until(lambda: len(self.runs) == 1))
        self.assertIs(self.runs[0]["shell"], self.port, "the product host shell is injected")
        self.assertEqual((self.runs[0]["scope"]["actor"], self.runs[0]["scope"]["authority"]), STANDING)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "finished"))
        task = self.flow.task_view()
        self.assertEqual((task["last_result"]["judgment"], task["active"], task["run_id"]), ("success", False, None))
        self.assertEqual(self.flow.worker_view(), {"state": "idle", "task_id": None})
        self.assertFalse(self.port.held)

    def test_spec_is_required_for_a_new_task_and_paths_must_be_relative(self):
        self.assertEqual(self.to_worker({"kind": "work", "message": "x"})["reason"], "spec_required")
        self.assertEqual(self.to_worker({"kind": "work", "message": "x",
                                         "spec": {"goal": "g", "paths": ["/etc"]}})["reason"], "invalid_paths")
        self.assertEqual(self.to_worker({"kind": "work", "message": "x", "task_id": str(uuid4())})["reason"],
                         "unknown_task")
        self.assertIsNone(self.flow.task_view())
        self.assertEqual(self.flow.worker_view()["state"], "idle")


class WorkerBusyTests(FlowFixture):
    def test_a_second_task_while_free_work_runs_is_answered_worker_busy_and_nothing_is_queued(self):
        work = self.running_work()
        created = len(self.mailbox.created)
        busy = self.new_experiment("another task")
        self.assert_worker_busy(busy, work["task_id"], "work", "running")
        self.assertEqual(busy["task"]["summary"], "refactor the parser module")
        busy_work = self.new_work("yet another")
        self.assert_worker_busy(busy_work, work["task_id"], "work", "running")
        time.sleep(0.1)
        self.assertEqual(len(self.mailbox.created), created, "nothing queued")
        self.assertEqual(list(self.flow.tasks), [work["task_id"]], "no Task created")
        self.assertEqual(self.runs, [])

    def test_worker_busy_while_an_experiment_is_held_for_the_host_terminal(self):
        self.port.busy_reason = "a line is being typed in the host shell"
        pending = self.new_experiment()
        self.assertEqual(pending["status"], "dispatched")
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"] == "host_terminal_busy"))
        busy = self.new_work()
        self.assert_worker_busy(busy, pending["task_id"], "experiment", "held:host_terminal_busy")
        time.sleep(0.1)
        self.assertEqual((self.runs, self.port.holds), ([], 0))
        self.assertIsNone(self.current_run(pending["task_id"]))
        self.port.busy_reason = None
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "finished"))
        self.assertEqual(len(self.runs), 1)

    def test_worker_busy_while_waiting_for_the_report(self):
        self.gates.judge.clear()
        pending = self.new_experiment()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "waiting_report"))
        busy = self.new_work()
        self.assert_worker_busy(busy, pending["task_id"], "experiment", "waiting_report")
        rerun = self.to_worker({"kind": "experiment", "message": "again", "task_id": pending["task_id"],
                                "run": True})
        self.assert_worker_busy(rerun, pending["task_id"], "experiment", "waiting_report")
        self.gates.judge.set()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "finished"))
        self.assertEqual(self.new_work()["status"], "dispatched", "the next Task is accepted once it finished")

    def test_missing_environment_names_or_a_busy_worker_hold_the_start(self):
        busy = {"worker": False}
        self.flow._omp_idle = lambda role: busy["worker"] if role is ActorRole.WORKER else True
        pending = self.new_experiment()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"] == "worker_busy"))
        self.flow._experiment.environment_names = lambda: set()
        busy["worker"] = True
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"] == "environment_missing:PATH"))
        self.assertEqual(self.runs, [])
        self.assert_worker_busy(self.new_work(), pending["task_id"], "experiment", "held:environment_missing:PATH")


class FollowUpTests(FlowFixture):
    def test_follow_ups_for_the_active_free_work_task_are_messages_to_the_worker(self):
        work = self.running_work()
        task_message = self.mailbox.created[0]
        further = self.to_worker({"kind": "work", "message": "also rename foo", "task_id": work["task_id"],
                                  "spec": {"goal": "clean parser", "paths": ["docs/"]}})
        self.assertEqual((further["status"], further["task_id"]), ("queued", work["task_id"]))
        self.assertTrue(wait_until(lambda: len(self.mailbox.delivered) == 2))
        question = self.mailbox.created[1]
        self.assertEqual((question.kind, question.target_role, question.run_id),
                         (MessageKind.QUESTION, ActorRole.WORKER, task_message.run_id))
        self.assertEqual((question.payload["message"], question.payload["paths"]), ("also rename foo", ["docs/"]))
        self.assertEqual([d["kind"] for d in self.decisions(work["task_id"], 1)], ["scope_approved", "proceed"],
                         "a follow-up is no new approval")
        follow = [r for r in self.ledger() if r["type"] == "follow_up"]
        self.assertEqual((follow[0]["actor"], follow[0]["authority"], follow[0]["tool_call_id"]),
                         (*STANDING, "m-2"))
        progress = self.to_manager({"kind": "progress", "message": "half done"})
        self.assertEqual(progress["status"], "queued")
        self.assertTrue(wait_until(lambda: len(self.mailbox.delivered) == 3))
        report = self.mailbox.created[2]
        self.assertEqual((report.kind, report.target_role, report.in_reply_to_message_id),
                         (MessageKind.REPORT, ActorRole.MANAGER, task_message.message_id))
        answer = self.to_manager({"kind": "answer", "message": "renamed", "in_reply_to": question.message_id})
        self.assertEqual(answer["status"], "queued")
        self.assertEqual(self.flow.worker_view()["state"], "busy", "progress and answers keep the Task active")

    def test_a_follow_up_to_another_task_id_or_kind_is_refused(self):
        work = self.running_work()
        self.assertEqual(self.to_worker({"kind": "work", "message": "x", "task_id": str(uuid4())})["reason"],
                         "unknown_task")
        self.assertEqual(self.to_worker({"kind": "experiment", "message": "x", "task_id": work["task_id"],
                                         "run": True})["reason"], "task_kind_mismatch")


class TaskCommandsTests(FlowFixture):
    """C-D69 (6): the manager writes the exact commands; the worker runs them and summarises."""

    COMMANDS = ["lscpu", "free -h", "lspci | grep -i vga || echo 'no lspci'"]

    def work_with_commands(self, commands=None, **extra):
        return self.to_worker({"kind": "work", "message": "collect the hardware facts; stop on the first error",
                               "spec": {"goal": "hardware facts", "paths": []},
                               "commands": list(commands or self.COMMANDS), **extra})

    def test_the_commands_are_validated_and_normalised(self):
        base = {"kind": "work", "message": "m", "spec": {"goal": "g", "paths": []}}
        self.assertEqual(flow_module.validate_arguments("to_worker", {**base, "commands": ["ls"]}), [])
        self.assertEqual(flow_module.normalize_arguments("to_worker", {**base, "commands": ["ls", " ", ""]})["commands"],
                         ["ls"])
        self.assertNotIn("commands", flow_module.normalize_arguments("to_worker", {**base, "commands": [""]}))
        self.assertNotIn("commands", flow_module.normalize_arguments("to_worker", {**base, "commands": None}))
        for bad in (["ls\x00"], ["x" * 8193], ["ls"] * 33, "ls", [3]):
            with self.subTest(bad=str(bad)[:20]):
                errors = flow_module.validate_arguments("to_worker", {**base, "commands": bad})
                self.assertTrue([e for e in errors if e.startswith("commands")], errors)
        experiment = {"kind": "experiment", "message": "m", "commands": ["ls"],
                      "spec": {"goal": "g", "paths": ["out.txt"], "execution": execution()}}
        self.assertTrue([e for e in flow_module.validate_arguments("to_worker", experiment)
                         if e.startswith("commands: must be null for kind experiment")])
        self.assertIn("commands", flow_module._TO_WORKER_KEYS)

    def test_the_task_message_shows_the_commands_numbered_with_the_rule(self):
        result = self.work_with_commands()
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 1))
        payload = self.mailbox.created[0].payload
        self.assertEqual(payload["commands"], [{"number": 1, "command": "lscpu"}, {"number": 2, "command": "free -h"},
                                               {"number": 3, "command": self.COMMANDS[2]}])
        rule = payload["commands_rule"]
        for needle in ("exactly as given", "in order", "Do not write, change", "refuses any other command",
                       "summary of the results", "do not paste commands"):
            self.assertIn(needle, rule)
        decisions = self.decisions(result["task_id"], 1)
        self.assertEqual(decisions[0]["details"]["commands"], self.COMMANDS, "the commands are part of the scope")
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        self.assertEqual(self.flow.active_commands(), self.COMMANDS)

    def test_a_task_without_commands_shows_none_and_restricts_nothing(self):
        self.running_work()
        self.assertNotIn("commands", self.mailbox.created[0].payload)
        self.assertNotIn("commands_rule", self.mailbox.created[0].payload)
        self.assertIsNone(self.flow.active_commands())

    def test_a_follow_up_with_commands_replaces_the_list_and_null_keeps_it(self):
        result = self.work_with_commands()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"
                                   and len(self.mailbox.created) == 1))
        kept = self.to_worker({"kind": "work", "message": "also tell me the disk sizes", "task_id": result["task_id"]})
        self.assertEqual(kept["status"], "queued", kept)
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 2))
        self.assertNotIn("commands", self.mailbox.created[1].payload)
        self.assertEqual(self.flow.active_commands(), self.COMMANDS)
        replaced = self.to_worker({"kind": "work", "message": "run this instead", "task_id": result["task_id"],
                                   "commands": ["df -h"]})
        self.assertEqual(replaced["status"], "queued", replaced)
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 3))
        self.assertEqual(self.mailbox.created[2].payload["commands"], [{"number": 1, "command": "df -h"}])
        self.assertIn("commands_rule", self.mailbox.created[2].payload)
        self.assertTrue(wait_until(lambda: self.flow.active_commands() == ["df -h"]), "once the worker has it")
        self.assertIn("commands_replaced", [r["type"] for r in self.ledger()])

    def test_a_follow_up_that_is_not_delivered_keeps_the_old_commands(self):
        result = self.work_with_commands()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"
                                   and len(self.mailbox.delivered) == 1))
        original = self.mailbox.deliver
        for status in (MailboxStatus.REJECTED, MailboxStatus.UNKNOWN):
            with self.subTest(status=status.value):
                before = len(self.mailbox.created)

                def failing(message, *, timeout=20, _status=status):
                    self.mailbox.delivered.append(message.message_id)
                    return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role,
                                           message.session_id, 1, _status, {"reason": "test"})
                self.mailbox.deliver = failing
                queued = self.to_worker({"kind": "work", "message": "use these instead", "commands": ["df -h"],
                                         "task_id": result["task_id"]})
                self.assertEqual(queued["status"], "queued", queued)
                self.assertTrue(wait_until(lambda: len(self.mailbox.created) == before + 1))
                self.assertTrue(wait_until(lambda: len(self.mailbox.delivered) == before + 1))
                time.sleep(0.2)
                self.assertEqual(self.flow.active_commands(), self.COMMANDS, "the old list stays")
                self.assertNotIn("commands_replaced", [r["type"] for r in self.ledger()])
        self.mailbox.deliver = original
        queued = self.to_worker({"kind": "work", "message": "use these now", "commands": ["df -h"],
                                 "task_id": result["task_id"]})
        self.assertEqual(queued["status"], "queued", queued)
        self.assertTrue(wait_until(lambda: self.flow.active_commands() == ["df -h"]), "delivered: replaced")
        self.assertIn("commands_replaced", [r["type"] for r in self.ledger()])

    def test_blank_only_commands_mean_no_commands(self):
        base = {"kind": "work", "message": "m", "spec": {"goal": "g", "paths": []}}
        for blank in ([" "], ["", "  ", "\t\n"], [None]):
            with self.subTest(blank=blank):
                normalized = flow_module.normalize_arguments("to_worker", {**base, "commands": blank})
                self.assertNotIn("commands", normalized)
                self.assertEqual(flow_module.validate_arguments("to_worker", normalized), [])
        result = self.to_worker({**base, "commands": [" ", ""]})
        self.assertEqual(result["status"], "dispatched", result)
        self.assertIsNone(self.flow.active_commands())

    def test_the_report_note_carries_the_run_list_notes(self):
        from workbench.backend.flow_terminal import CommandRuns
        runs = CommandRuns([{"command": "lscpu", "status": "exited", "exit_code": 0, "signal": None,
                             "duration_seconds": 0.1, "log_path": "/x/1.log"}],
                           ["3 older runs omitted: only the last 64 runs are listed."])
        self.flow.terminal_runs = lambda task_id: runs
        self.work_with_commands()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        self.to_manager({"kind": "done", "message": "done"})
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 2))
        payload = self.mailbox.created[1].payload
        self.assertEqual(payload["commands_run"], list(runs))
        self.assertIsInstance(payload["commands_run"], list)
        self.assertTrue(payload["commands_run_note"].startswith("Recorded by Workbench"))
        self.assertIn("3 older runs omitted", payload["commands_run_note"])

    def test_the_restriction_ends_with_the_task(self):
        self.work_with_commands()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        self.to_manager({"kind": "done", "message": "facts collected"})
        self.assertTrue(wait_until(lambda: self.flow.worker_view()["state"] == "idle"))
        self.assertIsNone(self.flow.active_commands(), "no Task: the user's direct requests are not restricted")

    def test_the_done_report_carries_the_commands_run_from_the_terminal_record(self):
        runs = [{"command": "lscpu", "status": "exited", "exit_code": 0, "signal": None, "duration_seconds": 0.1,
                 "log_path": "/x/1.log"}]
        asked = []
        self.flow.terminal_runs = lambda task_id: asked.append(task_id) or runs
        result = self.work_with_commands()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        self.to_manager({"kind": "done", "message": "CPU: 8 cores; RAM 16 GiB; no GPU"})
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 2))
        payload = self.mailbox.created[1].payload
        self.assertEqual(payload["message"], "CPU: 8 cores; RAM 16 GiB; no GPU", "the worker's text is unchanged")
        self.assertEqual(payload["commands_run"], runs)
        self.assertIn("Recorded by Workbench", payload["commands_run_note"])
        self.assertEqual(asked, [result["task_id"]])
        self.flow.terminal_runs = lambda task_id: []
        self.running_work()
        self.to_manager({"kind": "progress", "message": "half way"})
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 4))
        self.assertNotIn("commands_run", self.mailbox.created[3].payload, "only done/blocked reports carry it")


class StartFailureHoldTests(FlowFixture):
    """stuck-review-02 P3: a failed experiment start keeps its hold until the shell is given back, and only a
    wb-handoff the start really typed counts (it may reach the control wait late)."""

    def failing_start(self, type_handoff):
        typed, seen = [], {}
        self.port.send_user = lambda data: typed.append(data)
        original = TaskFlow._give_back
        settled = []

        def give_back(flow, port, ports=None, *, after_failure=False, hold_reason=None):
            seen.update(after_failure=after_failure, hold_reason=hold_reason, held=port.held)
            return original(flow, port, ports, after_failure=after_failure, hold_reason=hold_reason)

        def start(workflow, task_id, revision, *, worktree_path, artifacts_root, automation, shell):
            self.assertIs(shell, self.port, "the workflow gets the product port itself")
            shell.send_user(b"cd /somewhere && pwd -P > /x\n")
            if type_handoff:
                shell.send_user(b"wb-handoff\n")
            raise WorkflowHeld("forced start failure")

        with mock.patch.object(FakeWorkflow, "start", start), \
                mock.patch.object(TaskFlow, "_give_back", give_back), \
                mock.patch.object(TaskFlow, "_await_settled", staticmethod(lambda port: settled.append(1))):
            result = self.new_experiment()
            self.assertEqual(result["status"], "dispatched", result)
            self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "finished"), self.flow.task_view())
        self.assertFalse(self.port.held, "released after the give-back")
        self.assertEqual(self.port.send_user.__name__, "<lambda>", "the port's own send_user is put back")
        return seen, settled, typed

    def test_a_failure_before_wb_handoff_is_not_treated_as_a_handoff(self):
        seen, settled, typed = self.failing_start(type_handoff=False)
        self.assertEqual(seen, {"after_failure": False, "hold_reason": HOLD_REASON_FLOW, "held": True})
        self.assertEqual(settled, [], "no wait for a control wait that cannot come")
        self.assertEqual(len(typed), 1)

    def test_a_failure_after_wb_handoff_waits_for_it_under_the_hold(self):
        seen, settled, typed = self.failing_start(type_handoff=True)
        self.assertEqual(seen, {"after_failure": True, "hold_reason": HOLD_REASON_FLOW, "held": True})
        self.assertEqual(settled, [1])
        self.assertEqual(typed[-1], b"wb-handoff\n")


class CompletionTests(FlowFixture):
    def test_done_frees_the_worker_and_the_next_to_worker_is_accepted(self):
        work = self.running_work()
        run_id = self.flow.task_view()["run_id"]
        done = self.to_manager({"kind": "done", "message": "finished"})
        self.assertEqual(done["status"], "queued")
        self.assertTrue(wait_until(lambda: self.flow.worker_view()["state"] == "idle"))
        task = self.flow.task_view()
        self.assertEqual((task["task_id"], task["status"], task["closed_reason"], task["active"]),
                         (work["task_id"], "closed", "done", False))
        self.assertIsNone(self.current_run(work["task_id"]))
        self.assertEqual(self.run_history(run_id)[-1]["kind"], "completed")
        self.assertEqual(self.to_manager({"kind": "progress", "message": "late"})["reason"], "no_active_task")
        nxt = self.new_work("next change")
        self.assertEqual(nxt["status"], "dispatched")
        self.assertNotEqual(nxt["task_id"], work["task_id"])

    def test_blocked_frees_the_worker_a_follow_up_resumes_it_and_a_new_task_supersedes_it(self):
        work = self.running_work()
        run_id = self.flow.task_view()["run_id"]
        blocked = self.to_manager({"kind": "blocked", "message": "need the schema"})
        self.assertEqual(blocked["status"], "queued")
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "blocked"))
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        self.assertIn("blocked", [d["kind"] for d in self.decisions(work["task_id"], 1)])
        resumed = self.to_worker({"kind": "work", "message": "schema is in src/parser/schema.py",
                                  "task_id": work["task_id"]})
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual((self.flow.task_view()["status"], self.flow.worker_view()["state"]), ("running", "busy"))
        self.to_manager({"kind": "blocked", "message": "still blocked"})
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "blocked"))
        nxt = self.new_work("something else")
        self.assertEqual(nxt["status"], "dispatched")
        old = self.flow.tasks[work["task_id"]]
        self.assertEqual((old.status, old.closed_reason), ("closed", "superseded_by_new_task"))
        self.assertIsNone(self.current_run(work["task_id"]))
        self.assertEqual(self.run_history(run_id)[-1]["kind"], "cancelled")

    def test_a_finished_experiment_task_is_superseded_by_a_new_task(self):
        old = self.finished_experiment()
        result = self.new_work()
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual((self.flow.tasks[old].status, self.flow.tasks[old].closed_reason),
                         ("closed", "superseded_by_new_task"))
        self.assertEqual(self.to_worker({"kind": "experiment", "message": "again", "task_id": old,
                                         "run": True})["reason"], "task_closed")


class RetryTests(FlowFixture):
    def test_rerun_of_the_active_experiment_keeps_the_retry_limit(self):
        task_id = self.finished_experiment()
        for attempt in range(1, RETRY_LIMIT + 1):
            commit = COMMIT_B if attempt == 1 else None
            args = {"kind": "experiment", "message": f"rerun {attempt}", "task_id": task_id, "run": True}
            if commit:
                args["spec"] = {"goal": "faster parser", "paths": ["src/"], "execution": execution(commit=commit)}
            result = self.to_worker(args)
            self.assertEqual((result["status"], result["retry"], result["retry_limit"]),
                             ("dispatched", attempt, RETRY_LIMIT), result)
            self.assertTrue(wait_until(lambda: len(self.runs) == attempt + 1
                                       and self.flow.task_view()["status"] == "finished"))
        self.assertEqual(self.runs[1]["revision"], 2, "a changed spec is a new revision")
        self.assertEqual(self.runs[2]["revision"], 2, "the same spec reruns on its revision")
        limited = self.to_worker({"kind": "experiment", "message": "one more", "task_id": task_id, "run": True})
        self.assertEqual((limited["status"], limited["reason"]), ("held", "retry_limit"))
        proceeds = [d for revision in (1, 2) for d in self.decisions(task_id, revision) if d["kind"] == "proceed"]
        self.assertEqual([d["retry_count_used"] for d in proceeds], [0, 1, 2, 3])
        self.assertTrue(all((d["details"]["actor"], d["details"]["authority"]) == STANDING for d in proceeds))
        time.sleep(0.1)
        self.assertEqual(len(self.runs), RETRY_LIMIT + 1)

    def test_a_message_to_a_finished_experiment_needs_a_run(self):
        task_id = self.finished_experiment()
        self.assertEqual(self.to_worker({"kind": "experiment", "message": "how was it", "task_id": task_id})["reason"],
                         "no_active_run")


class CancelTests(FlowFixture):
    def cancel(self, task_id, message="stop this task"):
        args = {"kind": "work", "message": message, "cancel": True}
        if task_id is not None:
            args["task_id"] = task_id
        task = self.flow.tasks.get(task_id) if task_id else None
        if task is not None:
            args["kind"] = task.kind
        return self.to_worker(args)

    def test_cancel_running_free_work_closes_the_task_cancels_the_run_and_notifies_the_worker(self):
        work = self.running_work()
        run_id = self.flow.task_view()["run_id"]
        result = self.cancel(work["task_id"], "the user changed plans")
        self.assertEqual((result["status"], result["task_id"]), ("cancelled", work["task_id"]), result)
        task = self.flow.task_view()
        self.assertEqual((task["status"], task["closed_reason"], self.flow.worker_view()["state"]),
                         ("closed", "cancelled", "idle"))
        self.assertIsNone(self.current_run(work["task_id"]))
        self.assertEqual(self.run_history(run_id)[-1]["kind"], "cancelled")
        self.assertTrue(wait_until(lambda: len(self.mailbox.to(ActorRole.WORKER)) == 2))
        notice = self.mailbox.to(ActorRole.WORKER)[1]
        self.assertEqual((notice.kind, notice.run_id, notice.payload["cancel"], notice.payload["message"]),
                         (MessageKind.QUESTION, run_id, True, "the user changed plans"))
        self.assertEqual(self.to_manager({"kind": "done", "message": "late"})["reason"], "no_active_task")
        self.assertEqual(self.new_work("next")["status"], "dispatched")

    def test_cancel_an_experiment_held_for_the_host_terminal_starts_nothing(self):
        self.port.busy_reason = "a job is running"
        pending = self.new_experiment()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"] == "host_terminal_busy"))
        result = self.cancel(pending["task_id"])
        self.assertEqual(result["status"], "cancelled")
        self.port.busy_reason = None
        time.sleep(0.2)
        self.assertEqual(self.runs, [])
        self.assertEqual((self.flow.task_view()["closed_reason"], self.flow.worker_view()["state"]),
                         ("cancelled", "idle"))
        self.assertEqual(self.mailbox.created, [], "the worker never got anything, so nothing is sent")

    def test_cancel_during_the_host_command_waits_for_its_exit_and_skips_the_judgment(self):
        self.gates.exit.clear()
        pending = self.new_experiment()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        run_id = self.flow.task_view()["run_id"]
        result = self.cancel(pending["task_id"])
        self.assertEqual(result["status"], "cancel_requested", result)
        self.assertIn("host", result["detail"])
        self.assertEqual(self.flow.task_view()["status"], "cancelling")
        self.assert_worker_busy(self.new_work(), pending["task_id"], "experiment", "cancelling")
        self.gates.exit.set()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "closed"))
        self.assertEqual((self.flow.task_view()["closed_reason"], self.gates.judged), ("cancelled", 0))
        self.assertEqual(self.run_history(run_id)[-1]["kind"], "cancelled")
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        nxt = self.new_work()
        self.assertEqual(nxt["status"], "dispatched")
        self.assertIn("task_cancelled", [n["kind"] for n in nxt.get("notices", [])])

    def test_cancel_needs_the_task_id_of_an_open_task(self):
        self.assertEqual(self.cancel(None)["reason"], "task_id_required")
        self.assertEqual(self.cancel(str(uuid4()))["reason"], "unknown_task")
        work = self.running_work()
        self.cancel(work["task_id"])
        self.assertEqual(self.cancel(work["task_id"])["reason"], "task_closed")


class RestartTests(FlowFixture):
    def test_a_dispatched_but_unstarted_task_is_not_started_after_a_restart(self):
        self.restart(start=False)  # no runner: the dispatch is recorded but nothing starts
        result = self.new_work()
        self.assertEqual(result["status"], "dispatched")
        self.restart()
        time.sleep(0.2)
        self.assertEqual(self.mailbox.created, [], "no automatic start or resend after a restart")
        self.assertIsNone(self.current_run(result["task_id"]))
        task = self.flow.task_view()
        self.assertEqual((task["status"], task["closed_reason"]), ("closed", "not_started_before_restart"))
        self.assertEqual(self.flow.worker_view()["state"], "idle")

    def test_old_pending_approvals_are_retired_on_start_and_never_dispatched(self):
        self.flow.close()
        self.service.close()
        with TaskRepository(self.db) as repository:
            old_task = repository.create_task({"kind": "work", "goal": "old", "paths": ["src/"]})
        approval_id = str(uuid4())
        ledger = self.root / "workflow" / "tasks-flow.jsonl"
        records = [
            {"seq": 1, "type": "task_created", "task_id": old_task},
            {"seq": 2, "type": "approval", "approval": {
                "approval_id": approval_id, "task_id": old_task, "revision": 1, "kind": "work", "summary": "old",
                "spec": {"kind": "work", "goal": "old", "paths": ["src/"]}, "diff_vs_approved": None,
                "created_at": "2026-10-01T00:00:00Z", "state": "pending"}},
            {"seq": 3, "type": "task", "task": {
                "task_id": old_task, "kind": "work", "status": "pending_approval", "revision": 1,
                "spec": {"kind": "work", "goal": "old", "paths": ["src/"]}, "approved_revision": None,
                "approved_spec": None, "current_spec": None, "user_decision_id": None, "run_id": None,
                "run_revision": None, "task_message_id": None, "runs_started": 0, "pending_start": None,
                "pending_approval_id": approval_id, "held_reason": None, "closed_reason": None,
                "last_result": None}},
        ]
        ledger.write_text("".join(json.dumps(r) + "\n" for r in records))
        os.chmod(ledger, 0o600)
        self.open()
        time.sleep(0.2)
        self.assertEqual(self.mailbox.created, [])
        self.assertIsNone(self.current_run(old_task))
        self.assertEqual(self.decisions(old_task, 1), [], "an old approval is never turned into a dispatch")
        old = self.flow.tasks[old_task]
        self.assertEqual((old.status, old.closed_reason), ("closed", "approval_retired_c_d66"))
        retired = [r for r in self.ledger() if r["type"] == "approvals_retired"]
        self.assertEqual(retired[-1]["approval_ids"], [approval_id])
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        self.assertEqual(self.new_work()["status"], "dispatched")
        self.restart()
        self.assertEqual(len([r for r in self.ledger() if r["type"] == "approvals_retired"]), 1,
                         "retired once")

    def test_a_running_free_work_task_survives_a_restart_and_can_be_cancelled(self):
        work = self.running_work()
        self.restart()
        task = self.flow.task_view()
        self.assertEqual((task["status"], task["held_reason"]), ("running", "backend_restarted"))
        self.assert_worker_busy(self.new_work("other"), work["task_id"], "work", "held:backend_restarted")
        self.assertEqual(self.to_worker({"kind": "work", "message": "stop", "task_id": work["task_id"],
                                         "cancel": True})["status"], "cancelled")
        self.assertEqual(self.flow.worker_view()["state"], "idle")


class UiStateTests(FlowFixture):
    def setUp(self):
        super().setUp()
        self.socket = self.root / "ui.sock"
        self.server = UiServer(self.socket, self.controller)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.client = UiClient(self.socket, timeout=10)
        self.client.attach()

    def _serve(self):
        while not self.stop.is_set():
            self.server.poll(0.01)

    def tearDown(self):
        self.client.close()
        self.stop.set()
        self.thread.join(5)
        self.server.close(flush_timeout=0.5)
        super().tearDown()

    def test_snapshot_publishes_task_and_worker_and_approval_decide_is_gone(self):
        snapshot = self.client.snapshot()
        self.assertNotIn("approvals", snapshot)
        self.assertEqual((snapshot["task"], snapshot["worker"]), (None, {"state": "idle", "task_id": None}))
        work = self.running_work()
        snapshot = self.client.snapshot()
        task = snapshot["task"]
        self.assertTrue({"task_id", "kind", "status", "summary", "since", "run_id", "held_reason",
                         "last_result"} <= set(task), task)
        self.assertEqual((task["task_id"], task["kind"], task["status"], task["summary"]),
                         (work["task_id"], "work", "running", "refactor the parser module"))
        self.assertEqual(snapshot["worker"], {"state": "busy", "task_id": work["task_id"]})
        self.client.send_frame({"v": 1, "type": "approval_decide", "id": "a1", "approval_id": str(uuid4()),
                                "decision": "approve"})
        refused = self.client.next_frame().header
        self.assertEqual((refused["ok"], refused["reason"]), (False, "unsupported_type"))

    def test_pause_resume_are_accepted_and_paused_holds_to_worker(self):
        paused = self.client.request(ClientType.PAUSE)
        self.assertEqual(paused["automation"]["state"], "paused")
        self.assertEqual(self.new_work(), {"status": "held", "reason": "paused"})
        self.assertIsNone(self.flow.task_view())
        refused = self.client.request(ClientType.RESUME, reconciled=False)
        self.assertEqual(refused["reason"], "resume_not_reconciled")
        resumed = self.client.request(ClientType.RESUME, reconciled=True)
        self.assertEqual(resumed["automation"]["state"], "idle")
        self.assertEqual(self.new_work()["status"], "dispatched")


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=15,
                          check=True).stdout.strip()


class PublicMailboxFixture:
    def __init__(self, repository):
        self.repository, self.messages = repository, []

    def create_message(self, task_id, revision, run_id, sender, target, kind, payload, *,
                       in_reply_to_message_id=None):
        message = SimpleNamespace(message_id=str(uuid4()), task_id=task_id, revision=revision, run_id=run_id,
                                  sender=sender, target=target, kind=kind, payload=payload)
        self.messages.append(message)
        return message

    def deliver(self, message, **_):
        return SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)


class RealHostShellTests(unittest.TestCase):
    """The real TaskWorkflow on a real backend ShellPane through HostShellPort."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cw18-u2b-host-", dir="/tmp")
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.email", "fixture@example.invalid")
        git(self.source, "config", "user.name", "CW18 Fixture")
        (self.source / "tracked.txt").write_text("base\n")
        git(self.source, "add", "tracked.txt")
        git(self.source, "commit", "-qm", "baseline")
        self.commit = git(self.source, "rev-parse", "HEAD")
        for name in ("workflow", "worktrees", "runs"):
            (self.root / name).mkdir(mode=0o700)
        self.db = self.root / "tasks.sqlite3"
        self.pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "LANG": "C.UTF-8"})
        self.ui = bytearray()
        self.stop = threading.Event()
        self.loop = threading.Thread(target=self._backend_loop, daemon=True)
        self.loop.start()
        self.mailboxes: list[PublicMailboxFixture] = []

    def _backend_loop(self):
        while not self.stop.is_set():
            for chunk in self.pane.pump():
                self.ui.extend(chunk.data)
            time.sleep(0.01)

    def tearDown(self):
        self.stop.set()
        self.loop.join(5)
        self.pane.close()
        self.tmp.cleanup()

    def make_workflow(self, repository):
        mailbox = PublicMailboxFixture(repository)
        self.mailboxes.append(mailbox)
        return TaskWorkflow(repository, mailbox)

    def test_hold_refuses_user_input_and_a_typed_line_keeps_the_shell_busy(self):
        port = HostShellPort(self.pane, lambda: self.pane)
        try:
            self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt"))
            self.assertIsNone(self.pane.admit(b"echo typed-by-user"))
            self.assertTrue(wait_until(lambda: port.busy() is not None))
            self.assertIsNotNone(port.hold("x"), "a typed line is never overwritten")
            self.assertIsNone(self.pane.automation_hold)
            self.assertIsNone(self.pane.admit(b"\r"))
            self.assertTrue(wait_until(lambda: port.busy() is None, 5))
            self.assertIsNone(port.hold("Workbench is starting the approved run"))
            self.assertEqual(self.pane.admit(b"x")[0], Reason.HOST_SHELL_AUTOMATION)
            port.release_hold()
            self.assertIsNone(self.pane.admit(b"\x15"))
        finally:
            port.detach()

    def test_two_consecutive_experiment_runs_on_the_same_host_shell(self):
        with TaskRepository(self.db):
            pass
        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=FakeMailbox())
        ports = ExperimentPorts(host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
                                make_workflow=self.make_workflow, automation=lambda: AUTOMATION,
                                environment_names=lambda: {"PATH"}, worktrees_root=self.root / "worktrees",
                                artifacts_root=self.root / "runs")
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(self.db),
                        handoffs=service, omp_idle=lambda role: True, experiment=ports, poll_interval=0.05,
                        collect_slice=0.2)
        service.configure(policy=flow)
        service.start()
        flow.start()
        try:
            self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt"))
            self.assertIsNone(self.pane.admit(b"echo typed-by-user"))  # the user is typing
            spec = {"goal": "fixture run", "paths": ["outcome.txt"], "execution": {
                "source": str(self.source), "commit": self.commit,
                "command": "printf 'PASS from host\\n'; printf PASS > outcome.txt",
                "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
                "environment": ["PATH"], "shell": "bash"}}
            first = service.handle(ActorRole.MANAGER, request("to_worker", {
                "kind": "experiment", "message": "run the fixture", "spec": spec}, "m-1"))
            self.assertEqual(first["status"], "dispatched", first)
            self.assertTrue(wait_until(lambda: flow.task_view()["held_reason"] == "host_terminal_busy"))
            time.sleep(0.3)
            with TaskRepository(self.db) as repository:
                self.assertIsNone(repository.get_current_run(first["task_id"]), "nothing starts while busy")
            self.assertIsNone(self.pane.automation_hold)
            self.assertIsNone(self.pane.admit(b"\r"))  # the user finishes the line
            self.assertTrue(wait_until(lambda: flow.task_view()["status"] == "finished", 30), flow.task_view())
            result = flow.task_view()["last_result"]
            self.assertEqual((result["judgment"], result["report"], result["run_closed"]),
                             ("success", "omp_processed", True))
            run_id = result["run_id"]
            with TaskRepository(self.db) as repository:
                self.assertIsNone(repository.get_current_run(first["task_id"]))
                self.assertEqual([e["kind"] for e in repository.get_shell_history(run_id)],
                                 ["sent", "accepted", "started", "ended"])
                self.assertEqual(repository.get_run_history(run_id)[-1]["kind"], "completed")
            raw = (self.root / "runs" / run_id / "raw.log").read_bytes()
            self.assertIn(b"PASS from host", raw)
            record = json.loads((self.root / "runs" / run_id / "run.json").read_text())
            self.assertEqual(record["shell_source"], "injected_host_shell")
            self.assertEqual(record["parent_pid"], self.pane.pid, "the product host shell ran it")
            self.assertTrue(wait_until(lambda: b"PASS from host" in self.ui), "the UI still sees every byte")
            self.assertIn(b"typed-by-user", bytes(self.ui))
            self.assertTrue(wait_until(lambda: self.pane.state["input_owner"] == "user"
                                       and self.pane.state["parent_mode"] == "manual_prompt", 10),
                            "the shell is given back to the user")
            self.assertIsNone(self.pane.automation_hold)
            # U4 open 2: a returned manager request must not keep the shell busy for the next run.
            port = HostShellPort(self.pane, lambda: self.pane)
            try:
                self.assertTrue(wait_until(lambda: port.busy() is None, 10), port.busy())
            finally:
                port.detach()

            second = service.handle(ActorRole.MANAGER, request("to_worker", {
                "kind": "experiment", "message": "run it again", "task_id": first["task_id"], "run": True}, "m-2"))
            self.assertEqual((second["status"], second["retry"]), ("dispatched", 1), second)
            self.assertTrue(wait_until(lambda: flow.task_view()["status"] == "finished"
                                       and flow.task_view()["last_result"]["run_id"] != run_id, 30),
                            flow.task_view())
            again = flow.task_view()["last_result"]
            self.assertEqual((again["judgment"], again["run_closed"]), ("success", True))
            again_record = json.loads((self.root / "runs" / again["run_id"] / "run.json").read_text())
            self.assertEqual(again_record["parent_pid"], self.pane.pid, "the same host shell ran the second run")
            self.assertTrue(wait_until(lambda: self.pane.state["input_owner"] == "user"
                                       and self.pane.state["parent_mode"] == "manual_prompt", 10))
            self.assertFalse(self.pane.exited())
            self.assertIsNone(self.pane.admit(b"echo after-run\r"))
            self.assertTrue(wait_until(lambda: bytes(self.ui).count(b"after-run") >= 2, 5))
        finally:
            flow.close()
            service.close()


    def test_any_start_failure_after_the_handoff_gives_the_host_shell_back_and_the_next_run_works(self):
        # C-D69 (5)(a): p27-cd69-smoke-01 left the shell in control_wait (manager) after a refused request
        with TaskRepository(self.db):
            pass
        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=FakeMailbox())
        ports = ExperimentPorts(host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
                                make_workflow=self.make_workflow, automation=lambda: AUTOMATION,
                                environment_names=lambda: {"PATH"}, worktrees_root=self.root / "worktrees",
                                artifacts_root=self.root / "runs")
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(self.db),
                        handoffs=service, omp_idle=lambda role: True, experiment=ports, poll_interval=0.05,
                        collect_slice=0.2)
        service.configure(policy=flow)
        service.start()
        flow.start()

        def spec(command):
            return {"goal": "fixture run", "paths": ["outcome.txt"], "execution": {
                "source": str(self.source), "commit": self.commit, "command": command,
                "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
                "environment": ["PATH"], "shell": "bash"}}

        def user_owns_the_shell():
            self.assertTrue(wait_until(lambda: self.pane.state["input_owner"] == "user"
                                       and self.pane.state["parent_mode"] == "manual_prompt", 10), self.pane.state)
            self.assertTrue(wait_until(lambda: self.pane.automation_hold is None, 5))
            marker = f"user-{uuid4().hex[:6]}"
            self.assertIsNone(self.pane.admit(f"echo {marker}\r".encode()))
            self.assertTrue(wait_until(lambda: bytes(self.ui).count(marker.encode()) >= 2, 5), "the user types again")

        def failed_start(call_id, command):
            result = service.handle(ActorRole.MANAGER, request("to_worker", {
                "kind": "experiment", "message": "run the fixture", "spec": spec(command)}, call_id))
            self.assertEqual(result["status"], "dispatched", result)
            self.assertTrue(wait_until(lambda: flow.task_view()["status"] == "finished", 30), flow.task_view())
            self.assertEqual(flow.task_view()["last_result"]["outcome"], "start_failed", flow.task_view())
            user_owns_the_shell()

        try:
            self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt"))
            # 1. the shell refuses the request after wb-handoff and the manager claim (sizing check bypassed)
            with mock.patch.object(flow_module, "experiment_request_bytes", lambda command: 0):
                failed_start("m-1", "printf PASS > outcome.txt; echo " + "x" * 5000)
            # 2. the claim itself fails: the parent waits in control_wait, still owned by the user
            with mock.patch.object(HostShellPort, "claim_manager", side_effect=RuntimeError("forced claim failure")):
                failed_start("m-2", "printf PASS > outcome.txt")
            # 3. the next run uses the same shell normally
            result = service.handle(ActorRole.MANAGER, request("to_worker", {
                "kind": "experiment", "message": "run the fixture", "spec": spec(
                    "printf 'PASS from host\\n'; printf PASS > outcome.txt")}, "m-3"))
            self.assertEqual(result["status"], "dispatched", result)
            self.assertTrue(wait_until(lambda: flow.task_view()["status"] == "finished"
                                       and (flow.task_view()["last_result"] or {}).get("judgment") == "success", 30),
                            flow.task_view())
            user_owns_the_shell()
        finally:
            flow.close()
            service.close()

    def experiment_flow(self):
        with TaskRepository(self.db):
            pass
        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=FakeMailbox())
        ports = ExperimentPorts(host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
                                make_workflow=self.make_workflow, automation=lambda: AUTOMATION,
                                environment_names=lambda: {"PATH"}, worktrees_root=self.root / "worktrees",
                                artifacts_root=self.root / "runs")
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(self.db),
                        handoffs=service, omp_idle=lambda role: True, experiment=ports, poll_interval=0.05,
                        collect_slice=0.2)
        service.configure(policy=flow)
        service.start()
        flow.start()
        self.addCleanup(service.close)
        self.addCleanup(flow.close)
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt"))
        return service, flow

    def experiment_spec(self, command):
        return {"goal": "fixture run", "paths": ["outcome.txt"], "execution": {
            "source": str(self.source), "commit": self.commit, "command": command,
            "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
            "environment": ["PATH"], "shell": "bash"}}

    def test_a_late_control_wait_after_the_experiment_claim_failed_still_returns_the_shell(self):
        # p27-cd69-stuck-test-01 P3-1: the shell processes wb-handoff only after the 3 s wait (stopped shell)
        import signal
        service, flow = self.experiment_flow()
        original = HostShellPort.send_user
        resumed = []

        def slow_handoff(port, data):
            original(port, data)
            if data == b"wb-handoff\n":
                os.kill(self.pane.pid, signal.SIGSTOP)  # the test's own shell child
                timer = threading.Timer(3.6, lambda: (resume(), resumed.append(1)))
                timer.start()
                self.addCleanup(timer.cancel)

        pid = self.pane.pid

        def resume():
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        self.addCleanup(resume)
        with mock.patch.object(HostShellPort, "send_user", slow_handoff):
            result = service.handle(ActorRole.MANAGER, request("to_worker", {
                "kind": "experiment", "message": "run", "spec": self.experiment_spec("printf PASS > outcome.txt")},
                "m-late"))
            self.assertEqual(result["status"], "dispatched", result)
            self.assertTrue(wait_until(lambda: flow.task_view()["status"] == "finished", 30), flow.task_view())
        self.assertEqual(flow.task_view()["last_result"]["outcome"], "start_failed", flow.task_view())
        self.assertEqual(resumed, [1])
        self.assertTrue(wait_until(lambda: self.pane.state["input_owner"] == "user"
                                   and self.pane.state["parent_mode"] == "manual_prompt", 10), self.pane.state)
        self.assertTrue(wait_until(lambda: self.pane.automation_hold is None, 5))
        self.assertIsNone(self.pane.admit(b"echo after-late\r"))
        self.assertTrue(wait_until(lambda: bytes(self.ui).count(b"after-late") >= 2, 5))

    def test_the_experiment_size_check_uses_the_real_host_shell_path(self):
        # p27-cd69-stuck-review-01 P3-1: a host shell path longer than the generic estimate (e.g. a Nix store path)
        service, flow = self.experiment_flow()
        long_shell = ("/nix/store/" + "a" * 32 + "-bash-interactive-5.2p37/bin/").ljust(106, "x") + "/bash"
        prefix = "printf 'PASS\\n'; printf PASS > outcome.txt; : "
        n = 1
        while flow_module.experiment_request_bytes(prefix + "b" * (n + 1)) <= 4096:
            n += 1
        command = prefix + "b" * n  # the largest the generic check passes
        self.assertEqual(flow_module.validate_arguments("to_worker", {
            "kind": "experiment", "message": "m", "spec": self.experiment_spec(command)}), [])
        self.assertIsNotNone(flow_module.experiment_command_error(command, long_shell))
        self.assertIsNone(flow_module.experiment_command_error(command, "/usr/bin/bash"))
        self.assertIsNone(flow_module.experiment_command_error(["/bin/sh", "-c", command], long_shell),
                          "an argv list does not carry the shell path")
        before = len(self.ui)
        with mock.patch.object(self.pane, "choice", ShellChoice("bash", long_shell)):
            # 1. at Task creation, with the current host shell's path
            created = service.handle(ActorRole.MANAGER, request("to_worker", {
                "kind": "experiment", "message": "run", "spec": self.experiment_spec(command)}, "m-long"))
            self.assertEqual((created["status"], created["reason"]), ("rejected", "invalid_arguments"), created)
            self.assertTrue(any(e.startswith("spec.execution.command") and "script file" in e
                                for e in created["errors"]), created)
            self.assertIsNone(flow.active_task())
            # 2. the shell changed after creation: the start check refuses it before anything is typed
            with mock.patch.object(TaskFlow, "_host_shell_size_error", lambda self, args: None):
                created = service.handle(ActorRole.MANAGER, request("to_worker", {
                    "kind": "experiment", "message": "run", "spec": self.experiment_spec(command)}, "m-start"))
            self.assertEqual(created["status"], "dispatched", created)
            self.assertTrue(wait_until(lambda: flow.task_view()["status"] == "finished", 20), flow.task_view())
        self.assertEqual((flow.task_view()["last_result"]["outcome"], flow.task_view()["last_result"]["error"]),
                         ("start_failed", "command_too_long"), flow.task_view())
        time.sleep(0.3)
        self.assertNotIn(b"wb-handoff", bytes(self.ui)[before:], "nothing was typed")
        self.assertNotIn(b"cd ", bytes(self.ui)[before:], "nothing was typed")
        self.assertEqual((self.pane.state["input_owner"], self.pane.state["parent_mode"]), ("user", "manual_prompt"))
        # 3. with the real (short) path the same command runs
        result = service.handle(ActorRole.MANAGER, request("to_worker", {
            "kind": "experiment", "message": "run", "spec": self.experiment_spec(command)}, "m-short"))
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: flow.task_view()["status"] == "finished"
                                   and (flow.task_view()["last_result"] or {}).get("judgment") == "success", 30),
                        flow.task_view())

if __name__ == "__main__":
    unittest.main()
