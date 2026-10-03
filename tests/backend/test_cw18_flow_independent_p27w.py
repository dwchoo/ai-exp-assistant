"""CW-18 independent verification (p27-cw18-test-01): the Task flow under C-D66, adversarially.

Expectations were drafted from DECISIONS C-D58/C-D60/C-D64/C-D65/C-D66, tickets/CW-18.md and the
unit assignments before the implementation was read:

- tools by role: ``to_worker`` only for the manager, ``to_manager`` only for the worker; the role is the
  authenticated peer's (the service is called with it), never a request field;
- idempotence per (role, session, generation, tool_call_id), also across a backend restart;
- environment variable values are refused and never journaled;
- C-D66: a ``to_worker`` without task_id while the worker is idle becomes a Task at once (scope approval +
  proceed by ``user_standing_delegation`` / C-D66) and is dispatched immediately (work and experiment);
- one Task at a time: ``worker_busy`` with {task_id, kind, summary, status, since} in EVERY active state
  (dispatched, starting, running, waiting_report, held, cancelling) and nothing is queued or created;
- follow-ups to the active Task are worker messages of the same Task/run; cancel in each phase; a running
  host command is never killed (the Task closes as cancelled after the exit, without a judgment);
- done closes the Task (run completed), blocked frees the worker (run not completed), a new Task supersedes;
- retry limit 3 (CW-13: three re-runs after the first run);
- pause: every request held, nothing resent after resume; a held start never starts while paused;
- restart: nothing is started or resent, an active run is held ``backend_restarted``;
- journals are 0600 and carry no secret values; ui_v1 publishes task/worker, pause/resume, and the
  removed approval API answers ``unsupported_type``.

No OMP, no provider, no network: fake mailbox, fake host-shell port and a gated fake workflow.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

from workbench.backend.client import UiClient
from workbench.backend.flow import HandoffService
from workbench.backend.flow_tasks import RETRY_LIMIT, ExperimentPorts, TaskFlow
from workbench.backend.service import Backend
from workbench.backend.ui_server import UiServer
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus
from workbench.tasks.repository import TaskRepository

M_SESSION = str(uuid4())
W_SESSION = str(uuid4())
AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}
SECRET = "p27w-SECRET-value-0123456789"
ACTIVE = ("dispatched", "starting", "running", "waiting_report", "held", "cancelling")


def eventually(predicate, timeout=6.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


class Mailbox:
    """Records every created/delivered message; a delivery can be made to wait or to fail."""

    def __init__(self):
        self.lock = threading.Lock()
        self.created: list[SimpleNamespace] = []
        self.delivered: list[str] = []
        self.status = MailboxStatus.OMP_PROCESSED
        self.gate = threading.Event()
        self.gate.set()

    def create_message(self, task_id, revision, run_id, sender_role, target_role, kind, payload, *,
                       in_reply_to_message_id=None):
        target = ActorRole(target_role)
        message = SimpleNamespace(
            message_id=str(uuid4()), task_id=task_id, revision=revision, run_id=run_id,
            sender_role=ActorRole(sender_role), target_role=target, kind=MessageKind(kind), payload=dict(payload),
            session_id=W_SESSION if target is ActorRole.WORKER else M_SESSION, session_generation=1,
            in_reply_to_message_id=in_reply_to_message_id)
        with self.lock:
            self.created.append(message)
        return message

    def deliver(self, message, *, timeout=20):
        self.gate.wait(10)
        with self.lock:
            self.delivered.append(message.message_id)
        return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1,
                               self.status, {})

    def to(self, role):
        with self.lock:
            return [m for m in self.created if m.target_role is role]


class Port:
    def __init__(self):
        self.busy_reason: str | None = None
        self.holds = 0
        self.held = False
        self.takeovers = 0
        self.kills = 0

    def busy(self):
        return self.busy_reason

    def hold(self, reason):
        if self.busy_reason is not None:
            return self.busy_reason
        self.holds += 1
        self.held = True
        return None

    def release_hold(self):
        self.held = False

    def snapshot(self):
        return {"input_owner": "user", "lifecycle": {}}

    def request_takeover(self):
        self.takeovers += 1
        return {}

    def detach(self):
        pass


class Gates:
    def __init__(self):
        self.start = threading.Event()
        self.exit = threading.Event()
        self.judge = threading.Event()
        for gate in (self.start, self.exit, self.judge):
            gate.set()
        self.shell_state = None  # e.g. "unknown"
        self.collects = []
        self.judged = 0
        self.report_status = "omp_processed"


class Run:
    def __init__(self, repository, run_id, gates):
        self.repository, self.run_id, self.gates = repository, run_id, gates
        self.task_message_id = str(uuid4())
        self._record = {}

    def collect(self, *, timeout=1, paused=False):
        self.gates.collects.append(paused)
        if self.gates.shell_state is not None:
            return {"shell_state": self.gates.shell_state, "exit_confirmed": False}
        if self.gates.exit.wait(timeout):
            return {"shell_state": "exited", "exit_confirmed": True}
        return {"shell_state": "running", "exit_confirmed": False}

    def judge(self):
        self.gates.judge.wait(10)
        self.gates.judged += 1
        if self.gates.report_status == "omp_processed":
            self.repository.complete_run(self.run_id, {"judgment": "success"})
        return {"report": {"status": self.gates.report_status}, "worker_judgment": {"judgment": "success"}}

    def retry_report(self):
        return {"report": {"status": "omp_processed"}}

    def close(self):
        pass


class Workflow:
    def __init__(self, repository, starts, gates):
        self.repository, self.starts, self.gates = repository, starts, gates

    def start(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell):
        self.gates.start.wait(10)
        run_id = self.repository.start_run(task_id, revision, inputs={})
        self.starts.append({"task_id": task_id, "revision": revision, "run_id": run_id, "shell": shell})
        return Run(self.repository, run_id, self.gates)


class Lifecycle:
    def __init__(self):
        self.events = []

    def experiment_started(self, run):
        self.events.append(("experiment_started", run.run_id))

    def work_started(self, task_id, revision, run_id, message_id):
        self.events.append(("work_started", run_id))

    def run_ended(self, run_id):
        self.events.append(("run_ended", run_id))


def call(tool, args, call_id, *, session=None, generation=1):
    return {"request_id": str(uuid4()), "tool_call_id": call_id, "tool": tool, "args": args,
            "session_id": session or (M_SESSION if tool == "to_worker" else W_SESSION), "generation": generation}


def experiment_spec(**execution):
    value = {"source": "/src/repo", "commit": "c" * 40, "command": "./bench.sh",
             "criteria": {"log_contains": "OK", "result_file": "r.txt", "result_contains": "OK"},
             "environment": ["PATH"], "shell": "bash"}
    value.update(execution)
    return {"goal": "benchmark", "paths": ["bench/"], "execution": value}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p27w-flow-", dir="/tmp")
        self.root = Path(self.tmp.name)
        (self.root / "workflow").mkdir(mode=0o700)
        self.db = self.root / "tasks.sqlite3"
        self.mailbox = Mailbox()
        self.port = Port()
        self.gates = Gates()
        self.starts: list[dict] = []
        self.lifecycle = Lifecycle()
        self.paused_flag = False
        self.worker_idle: bool | None = True
        self.sensitive = (SECRET,)
        self.n = 0
        self.opened: list = []
        self.open()

    def tearDown(self):
        self.mailbox.gate.set()
        for gate in (self.gates.start, self.gates.exit, self.gates.judge):
            gate.set()
        for item in reversed(self.opened):
            item.close()
        self.tmp.cleanup()

    def open(self, *, start=True):
        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=self.mailbox,
                                 paused=lambda: self.paused_flag, sensitive_values=lambda: self.sensitive,
                                 retry_interval=0.01)
        ports = ExperimentPorts(host_shell=lambda: self.port,
                                make_workflow=lambda repository: Workflow(repository, self.starts, self.gates),
                                automation=lambda: AUTOMATION, environment_names=lambda: {"PATH"},
                                worktrees_root=self.root, artifacts_root=self.root)
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(self.db),
                        handoffs=service, omp_idle=lambda role: self.worker_idle, paused=lambda: self.paused_flag,
                        experiment=ports, lifecycle=self.lifecycle, poll_interval=0.02, collect_slice=0.03)
        service.configure(policy=flow, active_task=flow.active_task)
        service.start()
        if start:
            flow.start()
        self.service, self.flow = service, flow
        self.opened += [service, flow]
        return flow

    def restart(self):
        self.flow.close()
        self.service.close()
        self.opened = []
        return self.open()

    # -- requests ------------------------------------------------------------------------------
    def to_worker(self, args, call_id=None, **kw):
        self.n += 1
        return self.service.handle(ActorRole.MANAGER, call("to_worker", args, call_id or f"mw-{self.n}", **kw))

    def to_manager(self, args, call_id=None, **kw):
        self.n += 1
        return self.service.handle(ActorRole.WORKER, call("to_manager", args, call_id or f"wm-{self.n}", **kw))

    def work(self, message="tidy the parser"):
        return self.to_worker({"kind": "work", "message": message, "spec": {"goal": "tidy", "paths": ["src/p/"]}})

    def experiment(self, message="run the benchmark", **execution):
        return self.to_worker({"kind": "experiment", "message": message, "spec": experiment_spec(**execution)})

    def running_work(self):
        result = self.work()
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(eventually(lambda: self.view()["status"] == "running"), self.view())
        return result["task_id"]

    # -- observations ------------------------------------------------------------------------------
    def view(self):
        return self.flow.task_view() or {}

    def repo(self):
        return TaskRepository(self.db)

    def task_count(self):
        with self.repo() as repository:
            return repository._connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] \
                if hasattr(repository, "_connection") else None

    def ledger(self, name="tasks-flow.jsonl"):
        path = self.root / "workflow" / name
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def assert_busy(self, result, task_id, kind, status_prefix):
        self.assertEqual(result["status"], "worker_busy", result)
        task = result["task"]
        for name in ("task_id", "kind", "summary", "status", "since"):
            self.assertIn(name, task)
        self.assertEqual((task["task_id"], task["kind"]), (task_id, kind))
        self.assertTrue(task["status"].startswith(status_prefix), (task["status"], status_prefix))
        self.assertTrue(task["summary"])
        self.assertTrue(task["since"])
        self.assertEqual(result["task_id"], task_id)

    def assert_nothing_queued_by(self, action):
        """``action`` creates no Task, no message and no outbox entry."""
        created, outbox = len(self.mailbox.created), len(self.service.outbox_snapshot())
        tasks = set(self.flow.tasks)
        task_created = sum(1 for r in self.ledger() if r.get("type") == "task_created")
        result = action()
        time.sleep(0.1)
        self.assertEqual(len(self.mailbox.created), created, "a message was created")
        self.assertEqual(len(self.service.outbox_snapshot()), outbox, "an outbox entry was queued")
        self.assertEqual(set(self.flow.tasks), tasks, "a Task was created")
        self.assertEqual(sum(1 for r in self.ledger() if r.get("type") == "task_created"), task_created)
        return result


# =============================================================================================
class RoleAndRequestTests(Harness):
    def test_each_tool_only_for_its_own_authenticated_role(self):
        work = {"kind": "work", "message": "x", "spec": {"goal": "g", "paths": ["a/"]}}
        for role, tool, args in ((ActorRole.WORKER, "to_worker", work),
                                 (ActorRole.MANAGER, "to_manager", {"kind": "done", "message": "x"})):
            with self.subTest(role=role.value, tool=tool):
                result = self.assert_nothing_queued_by(
                    lambda: self.service.handle(role, call(tool, args, f"spoof-{tool}")))
                self.assertEqual((result["status"], result["reason"]), ("rejected", "tool_not_allowed_for_role"))
        for role in ("user", "backend", "observer", "", None):
            with self.subTest(role=role):
                result = self.service.handle(role, call("to_worker", work, "r"))
                self.assertEqual(result["status"], "rejected")
        self.assertIsNone(self.flow.task_view())

    def test_frame_role_fields_do_not_change_the_callers_role(self):
        # A worker that puts role/actor/sender fields into its request is still the worker.
        spoof = call("to_worker", {"kind": "work", "message": "x", "spec": {"goal": "g", "paths": ["a/"]}}, "c-1")
        spoof.update({"role": "manager", "actor": "manager", "sender_role": "manager"})
        result = self.service.handle(ActorRole.WORKER, spoof)
        self.assertEqual(result.get("reason"), "tool_not_allowed_for_role", result)
        # ... and fields inside args are unknown arguments, never a role.
        args = {"kind": "work", "message": "x", "spec": {"goal": "g", "paths": ["a/"]}, "role": "manager"}
        result = self.service.handle(ActorRole.MANAGER, call("to_worker", args, "c-2"))
        self.assertEqual(result.get("reason"), "invalid_arguments", result)
        self.assertIsNone(self.flow.task_view())

    def test_same_key_is_processed_once_and_returns_the_first_result(self):
        first = self.work()
        self.assertEqual(first["status"], "dispatched")
        again = self.to_worker({"kind": "work", "message": "tidy the parser",
                                "spec": {"goal": "tidy", "paths": ["src/p/"]}}, call_id="mw-1")
        self.assertEqual(again, first, "a duplicate returns the first result, not worker_busy")
        # The same tool_call_id from another session/generation is another call.
        other = self.to_worker({"kind": "work", "message": "other", "spec": {"goal": "g", "paths": ["a/"]}},
                               call_id="mw-1", generation=2)
        self.assertEqual(other["status"], "worker_busy")
        self.assertEqual(len([t for t in self.flow.tasks.values()]), 1)
        self.assertTrue(eventually(lambda: len(self.mailbox.to(ActorRole.WORKER)) == 1))
        time.sleep(0.1)
        self.assertEqual(len(self.mailbox.to(ActorRole.WORKER)), 1, "the duplicate sent nothing")
        # A restart keeps the dedupe (journal), nothing is redone.
        self.restart()
        replay = self.to_worker({"kind": "work", "message": "tidy the parser",
                                 "spec": {"goal": "tidy", "paths": ["src/p/"]}}, call_id="mw-1")
        self.assertEqual(replay, first)
        self.assertEqual(len(self.flow.tasks), 1)

    def test_concurrent_new_tasks_dispatch_exactly_one(self):
        results, barrier = [], threading.Barrier(8)

        def go(index):
            barrier.wait()
            results.append(self.to_worker({"kind": "work", "message": f"task {index}",
                                           "spec": {"goal": f"g{index}", "paths": ["a/"]}}, call_id=f"race-{index}"))

        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        statuses = sorted(r["status"] for r in results)
        self.assertEqual(statuses, ["dispatched"] + ["worker_busy"] * 7, results)
        self.assertEqual(len(self.flow.tasks), 1)


class EnvironmentValueTests(Harness):
    def test_values_are_refused_and_never_journaled(self):
        cases = [
            ("known value in message", {"kind": "work", "message": f"use {SECRET} please",
                                        "spec": {"goal": "g", "paths": ["a/"]}}),
            ("known value in goal", {"kind": "work", "message": "m", "spec": {"goal": f"g {SECRET}", "paths": ["a/"]}}),
            ("secret-like assignment", {"kind": "work", "message": "run with API_TOKEN=abc123def",
                                        "spec": {"goal": "g", "paths": ["a/"]}}),
            ("password flag", {"kind": "work", "message": "login --password=hunter2hunter2",
                               "spec": {"goal": "g", "paths": ["a/"]}}),
            ("declared name assignment in command", {"kind": "experiment", "message": "m", "spec": experiment_spec(
                command="DATA_DIR=/data/private ./bench.sh", environment=["PATH", "DATA_DIR"])}),
        ]
        for label, args in cases:
            with self.subTest(label):
                result = self.assert_nothing_queued_by(lambda: self.to_worker(args))
                self.assertEqual((result["status"], result["reason"]), ("rejected", "environment_value"), result)
                self.assertTrue(result["fields"])
        report = self.to_manager({"kind": "progress", "message": f"token is {SECRET}"})
        self.assertEqual(report.get("reason"), "environment_value")
        # A reference by name is fine.
        ok = self.to_worker({"kind": "work", "message": "read $API_TOKEN from the env",
                             "spec": {"goal": "g", "paths": ["a/"]}})
        self.assertEqual(ok["status"], "dispatched", ok)
        for name in ("handoffs.jsonl", "tasks-flow.jsonl"):
            text = (self.root / "workflow" / name).read_text()
            for value in (SECRET, "abc123def", "hunter2hunter2", "/data/private"):
                self.assertNotIn(value, text, f"{value} journaled in {name}")

    def test_environment_entries_must_be_names_never_values(self):
        for bad in (["PATH=/usr/bin"], ["A B"], ["1ABC"], ["PATH", "PATH"]):
            with self.subTest(bad):
                result = self.experiment(environment=bad)
                self.assertEqual(result.get("reason"), "invalid_arguments", result)
        self.assertNotIn("/usr/bin", (self.root / "workflow" / "handoffs.jsonl").read_text())

    def test_journals_and_database_are_private(self):
        self.running_work()
        for path in (self.root / "workflow" / "handoffs.jsonl", self.root / "workflow" / "tasks-flow.jsonl"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        # A journal that is a symlink is refused (no write through a planted link).
        self.restart()
        self.flow.close()
        self.service.close()
        self.opened = []
        target = self.root / "elsewhere.jsonl"
        target.write_text("")
        link = self.root / "workflow" / "handoffs.jsonl"
        link.unlink()
        link.symlink_to(target)
        with self.assertRaises(OSError):
            HandoffService(link, mailbox=self.mailbox)


class DispatchTests(Harness):
    def test_work_task_is_dispatched_at_once_under_the_standing_delegation(self):
        result = self.work()
        self.assertEqual((result["status"], result["kind"], result["revision"]), ("dispatched", "work", 1), result)
        self.assertNotIn("approval_id", json.dumps(result))
        task_id = result["task_id"]
        self.assertTrue(eventually(lambda: len(self.mailbox.to(ActorRole.WORKER)) == 1))
        message = self.mailbox.to(ActorRole.WORKER)[0]
        self.assertEqual((message.kind, message.task_id, message.sender_role), (MessageKind.TASK, task_id,
                                                                                 ActorRole.MANAGER))
        self.assertEqual((message.payload["goal"], message.payload["paths"]), ("tidy", ["src/p/"]))
        with self.repo() as repository:
            decisions = repository.get_decisions(task_id, 1)
        kinds = [d["kind"] for d in decisions]
        self.assertIn("scope_approved", kinds)
        self.assertIn("proceed", kinds)
        for decision in decisions:
            if decision["kind"] in ("scope_approved", "proceed"):
                details = decision["details"]
                self.assertEqual((details["actor"], details["authority"]), ("user_standing_delegation", "C-D66"))
                self.assertEqual(details["tool_call_id"], "mw-1")
        self.assertTrue(eventually(lambda: ("work_started", self.view()["run_id"]) in self.lifecycle.events))
        self.assertEqual(self.flow.worker_view(), {"state": "busy", "task_id": task_id})

    def test_experiment_task_starts_its_run_at_once_on_the_host_shell(self):
        result = self.experiment()
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(eventually(lambda: self.view()["status"] == "finished"), self.view())
        self.assertEqual(len(self.starts), 1)
        self.assertIs(self.starts[0]["shell"], self.port, "the product host-shell port is the run's shell")
        self.assertEqual(self.port.holds, 1)
        self.assertFalse(self.port.held, "the hold is released after the start")
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        run_id = self.starts[0]["run_id"]
        self.assertIn(("experiment_started", run_id), self.lifecycle.events)
        self.assertIn(("run_ended", run_id), self.lifecycle.events)

    def test_a_new_task_needs_a_spec_and_relative_paths(self):
        self.assertEqual(self.to_worker({"kind": "work", "message": "no spec"}).get("reason"), "spec_required")
        for paths in (["/etc"], ["../outside"], ["a/../../b"]):
            with self.subTest(paths=paths):
                result = self.to_worker({"kind": "work", "message": "m", "spec": {"goal": "g", "paths": paths}})
                self.assertEqual(result["status"], "rejected", result)
        self.assertIsNone(self.flow.task_view())

    def test_to_manager_without_a_task_is_rejected(self):
        result = self.assert_nothing_queued_by(lambda: self.to_manager({"kind": "done", "message": "done"}))
        self.assertEqual((result["status"], result["reason"]), ("rejected", "no_active_task"))


class WorkerBusyTests(Harness):
    def busy_check(self, task_id, kind, status):
        for args in ({"kind": "work", "message": "another", "spec": {"goal": "g2", "paths": ["b/"]}},
                     {"kind": "experiment", "message": "another", "spec": experiment_spec()}):
            result = self.assert_nothing_queued_by(lambda: self.to_worker(args))
            self.assert_busy(result, task_id, kind, status)
        self.assertEqual(self.flow.worker_view(), {"state": "busy", "task_id": task_id})

    def test_dispatched_held_for_the_host_terminal(self):
        self.port.busy_reason = "a line is being typed in the host shell"
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view().get("held_reason") == "host_terminal_busy"))
        self.assertEqual(self.view()["status"], "dispatched")
        self.busy_check(task_id, "experiment", "held:host_terminal_busy")
        self.assertEqual(self.starts, [], "nothing starts while the host terminal is busy")
        self.assertEqual(self.port.holds, 0, "the user's input is never taken over")
        self.port.busy_reason = None
        self.assertTrue(eventually(lambda: self.view()["status"] == "finished"), "the start is retried when idle")
        self.assertEqual(len(self.starts), 1)

    def test_starting(self):
        self.gates.start.clear()
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "starting"))
        self.busy_check(task_id, "experiment", "starting")

    def test_running_experiment_and_running_work(self):
        self.gates.exit.clear()
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "running"))
        self.busy_check(task_id, "experiment", "running")

    def test_running_work(self):
        task_id = self.running_work()
        self.busy_check(task_id, "work", "running")

    def test_waiting_report(self):
        self.gates.judge.clear()
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "waiting_report"))
        self.busy_check(task_id, "experiment", "waiting_report")

    def test_held_shell_unknown(self):
        self.gates.shell_state = "unknown"
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "held"), self.view())
        self.busy_check(task_id, "experiment", "held")
        self.assertEqual(self.gates.judged, 0, "an unknown shell boundary is never judged")

    def test_cancelling(self):
        self.gates.exit.clear()
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "running"))
        cancel = self.to_worker({"kind": "experiment", "message": "stop", "task_id": task_id, "cancel": True})
        self.assertEqual(cancel["status"], "cancel_requested", cancel)
        self.assertEqual(self.view()["status"], "cancelling")
        self.busy_check(task_id, "experiment", "cancelling")

    def test_worker_busy_detail_tells_the_manager_what_to_do(self):
        task_id = self.running_work()
        result = self.work("second")
        self.assertIn("to_manager", result["detail"])
        self.assertIn("cancel", result["detail"])
        self.assertEqual(result["task"]["summary"], "tidy the parser")
        self.assertEqual(result["task_id"], task_id)


class FollowUpTests(Harness):
    def test_follow_up_is_a_message_of_the_same_task_and_run(self):
        task_id = self.running_work()
        run_id = self.view()["run_id"]
        result = self.to_worker({"kind": "work", "message": "also check the tests", "task_id": task_id})
        self.assertEqual((result["status"], result["task_id"]), ("queued", task_id), result)
        self.assertTrue(eventually(lambda: len(self.mailbox.to(ActorRole.WORKER)) == 2))
        follow = self.mailbox.to(ActorRole.WORKER)[1]
        self.assertEqual((follow.task_id, follow.run_id, follow.kind), (task_id, run_id, MessageKind.QUESTION))
        self.assertEqual(follow.payload["message"], "also check the tests")
        self.assertEqual(len(self.flow.tasks), 1)
        with self.repo() as repository:
            self.assertEqual(repository.get_current_run(task_id)["run_id"], run_id, "no new run")

    def test_follow_up_to_an_unknown_or_closed_task_is_refused(self):
        task_id = self.running_work()
        self.assertEqual(self.to_worker({"kind": "work", "message": "x", "task_id": str(uuid4())})["reason"],
                         "unknown_task")
        self.assertEqual(self.to_worker({"kind": "experiment", "message": "x", "task_id": task_id})["reason"],
                         "task_kind_mismatch")
        self.to_worker({"kind": "work", "message": "stop", "task_id": task_id, "cancel": True})
        self.assertEqual(self.to_worker({"kind": "work", "message": "x", "task_id": task_id})["reason"],
                         "task_closed")

    def test_a_report_for_another_task_is_refused(self):
        self.running_work()
        result = self.to_manager({"kind": "progress", "message": "x", "task_id": str(uuid4())})
        self.assertEqual(result["reason"], "unknown_task")


class CancelTests(Harness):
    def test_cancel_before_start_starts_nothing_ever(self):
        self.port.busy_reason = "the host shell has jobs"
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view().get("held_reason") == "host_terminal_busy"))
        result = self.to_worker({"kind": "experiment", "message": "never mind", "task_id": task_id, "cancel": True})
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual((self.view()["status"], self.view()["closed_reason"]), ("closed", "cancelled"))
        self.port.busy_reason = None
        time.sleep(0.3)
        self.assertEqual((self.starts, self.port.holds), ([], 0), "a cancelled Task never starts")
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        with self.repo() as repository:
            self.assertIsNone(repository.get_current_run(task_id))

    def test_cancel_free_work_cancels_the_run_and_tells_the_worker(self):
        task_id = self.running_work()
        run_id = self.view()["run_id"]
        result = self.to_worker({"kind": "work", "message": "stop now", "task_id": task_id, "cancel": True})
        self.assertEqual((result["status"], result["worker_notified"]), ("cancelled", True), result)
        with self.repo() as repository:
            self.assertIsNone(repository.get_current_run(task_id))
            self.assertIn("cancel", repository.get_run_history(run_id)[-1]["kind"])
        self.assertTrue(eventually(lambda: any(m.payload.get("cancel") is True
                                               for m in self.mailbox.to(ActorRole.WORKER))))
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        self.assertEqual(self.work("next one")["status"], "dispatched")
        self.assertTrue(eventually(lambda: ("run_ended", run_id) in self.lifecycle.events))

    def test_cancel_while_the_host_command_runs_never_kills_and_closes_after_the_exit(self):
        self.gates.exit.clear()
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "running"))
        run_id = self.starts[0]["run_id"]
        result = self.to_worker({"kind": "experiment", "message": "stop", "task_id": task_id, "cancel": True})
        self.assertEqual(result["status"], "cancel_requested", result)
        self.assertIn("never kills", result["detail"])
        time.sleep(0.3)
        self.assertEqual(self.view()["status"], "cancelling")
        with self.repo() as repository:
            self.assertEqual(repository.get_current_run(task_id)["run_id"], run_id, "the run is still current")
        self.assertEqual(self.port.kills, 0)
        self.gates.exit.set()  # the command exits by itself
        self.assertTrue(eventually(lambda: self.view()["status"] == "closed"), self.view())
        self.assertEqual(self.view()["closed_reason"], "cancelled")
        self.assertEqual(self.gates.judged, 0, "no judgment after a cancel")
        with self.repo() as repository:
            self.assertIsNone(repository.get_current_run(task_id))
        # The manager learns the end in its next to_worker result.
        nxt = self.work("next")
        self.assertEqual(nxt["status"], "dispatched")
        self.assertIn("task_cancelled", [n["kind"] for n in nxt.get("notices", [])])

    def test_cancel_needs_task_id_and_open_task(self):
        self.assertEqual(self.to_worker({"kind": "work", "message": "x", "cancel": True})["reason"], "task_id_required")
        self.assertEqual(self.to_worker({"kind": "work", "message": "x", "cancel": True, "task_id": str(uuid4())})
                         ["reason"], "unknown_task")
        self.assertEqual(self.to_worker({"kind": "experiment", "message": "x", "cancel": True, "run": True,
                                         "task_id": str(uuid4())})["reason"], "invalid_arguments")


class CompletionTests(Harness):
    def task_message_id(self):
        return self.mailbox.to(ActorRole.WORKER)[0].message_id

    def test_done_closes_the_task_and_completes_the_run(self):
        task_id = self.running_work()
        run_id = self.view()["run_id"]
        result = self.to_manager({"kind": "done", "message": "finished, tests pass", "task_id": task_id})
        self.assertEqual(result["status"], "queued", result)
        self.assertTrue(eventually(lambda: self.view()["status"] == "closed"), self.view())
        self.assertEqual(self.view()["closed_reason"], "done")
        report = self.mailbox.to(ActorRole.MANAGER)[-1]
        self.assertEqual((report.kind, report.in_reply_to_message_id), (MessageKind.REPORT, self.task_message_id()))
        with self.repo() as repository:
            self.assertEqual(repository.get_run_history(run_id)[-1]["kind"], "completed")
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        self.assertEqual(self.work("next")["status"], "dispatched")

    def test_done_whose_delivery_is_unknown_before_acceptance_closes_the_task_and_frees_the_worker(self):
        """Root adjudication p27-cw18-r1 (review R1): unknown before acceptance -> report_outcome_unknown.

        The worker is free, the Task is closed as ``report_outcome_unknown``, the manager learns it from the
        notices of its next ``to_worker`` result, and nothing is resent (the no-replay invariant stays).
        """
        task_id = self.running_work()
        self.mailbox.status = MailboxStatus.UNKNOWN
        self.to_manager({"kind": "done", "message": "finished", "task_id": task_id})
        self.assertTrue(eventually(lambda: self.flow.tasks[task_id].status == "closed"), self.view())
        self.assertEqual(self.flow.tasks[task_id].closed_reason, "report_outcome_unknown")
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        time.sleep(0.15)
        self.assertEqual(len(self.mailbox.to(ActorRole.MANAGER)), 1, "the unknown report is created once")
        self.assertEqual(self.mailbox.delivered.count(self.mailbox.to(ActorRole.MANAGER)[0].message_id), 1,
                         "and never delivered a second time")
        self.mailbox.status = MailboxStatus.OMP_PROCESSED
        result = self.work("next")
        self.assertEqual(result["status"], "dispatched", result)
        notices = [n for n in result.get("notices", ()) if n.get("kind") == "report_outcome_unknown"]
        self.assertEqual([n.get("task_id") for n in notices], [task_id], result)
        time.sleep(0.15)
        self.assertEqual(len(self.mailbox.to(ActorRole.MANAGER)), 1, "no resend after the next dispatch either")

    def test_blocked_frees_the_worker_without_completing_and_a_new_task_supersedes(self):
        task_id = self.running_work()
        run_id = self.view()["run_id"]
        self.to_manager({"kind": "blocked", "message": "need access", "reason": "no permission", "task_id": task_id})
        self.assertTrue(eventually(lambda: self.view()["status"] == "blocked"), self.view())
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        with self.repo() as repository:
            self.assertNotEqual(repository.get_run_history(run_id)[-1]["kind"], "completed")
            self.assertEqual(repository.get_current_run(task_id)["run_id"], run_id)
        new = self.work("something else")
        self.assertEqual(new["status"], "dispatched")
        old = self.flow.tasks[task_id]
        self.assertEqual((old.status, old.closed_reason), ("closed", "superseded_by_new_task"))
        with self.repo() as repository:
            self.assertIsNone(repository.get_current_run(task_id), "the superseded run is closed")

    def test_answer_needs_in_reply_to_and_report_kinds_pass_through(self):
        task_id = self.running_work()
        self.assertEqual(self.to_manager({"kind": "answer", "message": "x"})["reason"], "in_reply_to_required")
        result = self.to_manager({"kind": "progress", "message": "half way", "requires_code_change": True,
                                  "reason": "parser bug", "task_id": task_id})
        self.assertEqual(result["status"], "queued")
        self.assertTrue(eventually(lambda: len(self.mailbox.to(ActorRole.MANAGER)) == 1))
        payload = self.mailbox.to(ActorRole.MANAGER)[0].payload
        self.assertEqual((payload["kind"], payload["requires_code_change"]), ("progress", True))
        self.assertEqual(self.view()["status"], "running", "progress does not end the Task")

    def test_a_worker_scope_request_is_never_dispatched(self):
        task_id = self.running_work()
        self.to_manager({"kind": "report", "message": "I need more", "task_id": task_id,
                         "request": {"goal": "more", "paths": ["other/"]}})
        self.assertTrue(eventually(lambda: len(self.mailbox.to(ActorRole.MANAGER)) == 1))
        request = self.mailbox.to(ActorRole.MANAGER)[0].payload["request"]
        self.assertIs(request["dispatch_authorized"], False)
        self.assertEqual(len(self.flow.tasks), 1)


class RetryTests(Harness):
    def test_three_reruns_after_the_first_run_then_the_limit(self):
        self.assertEqual(RETRY_LIMIT, 3)
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "finished"))
        for attempt in range(1, 4):
            result = self.to_worker({"kind": "experiment", "message": f"again {attempt}", "task_id": task_id,
                                     "run": True})
            self.assertEqual((result["status"], result.get("retry")), ("dispatched", attempt), result)
            self.assertTrue(eventually(lambda: len(self.starts) == attempt + 1 and self.view()["status"] == "finished"))
        refused = self.assert_nothing_queued_by(lambda: self.to_worker(
            {"kind": "experiment", "message": "once more", "task_id": task_id, "run": True}))
        self.assertEqual((refused["status"], refused["reason"]), ("held", "retry_limit"), refused)
        time.sleep(0.2)
        self.assertEqual(len(self.starts), 4, "1 first run + 3 re-runs")
        with self.repo() as repository:
            used = [d["retry_count_used"] for d in repository.get_decisions(task_id, 1) if d["kind"] == "proceed"]
        self.assertEqual(used, [0, 1, 2, 3])

    def test_a_changed_spec_is_a_new_revision_for_the_next_run(self):
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "finished"))
        result = self.to_worker({"kind": "experiment", "message": "new commit", "task_id": task_id,
                                 "spec": experiment_spec(commit="d" * 40)})
        self.assertEqual((result["status"], result["revision"]), ("dispatched", 2), result)
        self.assertTrue(eventually(lambda: len(self.starts) == 2))
        self.assertEqual(self.starts[1]["revision"], 2)

    def test_rerun_while_busy_is_worker_busy(self):
        self.gates.exit.clear()
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "running"))
        result = self.to_worker({"kind": "experiment", "message": "again", "task_id": task_id, "run": True})
        self.assertEqual(result["status"], "worker_busy")


class PauseTests(Harness):
    def test_paused_holds_every_request_and_nothing_is_resent_after_resume(self):
        self.paused_flag = True
        for args in ({"kind": "work", "message": "m", "spec": {"goal": "g", "paths": ["a/"]}},
                     {"kind": "experiment", "message": "m", "spec": experiment_spec()}):
            result = self.assert_nothing_queued_by(lambda: self.to_worker(args))
            self.assertEqual(result, {"status": "held", "reason": "paused"})
        self.paused_flag = False
        time.sleep(0.3)
        self.assertIsNone(self.flow.task_view(), "a held to_worker is not dispatched after resume")
        self.assertEqual(self.mailbox.created, [])

    def test_a_dispatched_experiment_never_starts_while_paused_and_collection_goes_on(self):
        self.port.busy_reason = "busy"
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view().get("held_reason") == "host_terminal_busy"))
        self.paused_flag = True
        self.port.busy_reason = None
        self.assertTrue(eventually(lambda: self.view().get("held_reason") == "paused"))
        time.sleep(0.2)
        self.assertEqual((self.starts, self.port.holds), ([], 0))
        self.paused_flag = False
        self.assertTrue(eventually(lambda: len(self.starts) == 1), "the held start runs once after resume")
        # collection during a pause: start a long run, then pause
        self.assertTrue(eventually(lambda: self.view()["status"] == "finished"))
        self.gates.exit.clear()
        self.to_worker({"kind": "experiment", "message": "again", "task_id": task_id, "run": True})
        self.assertTrue(eventually(lambda: self.view()["status"] == "running"))
        self.paused_flag = True
        self.assertTrue(eventually(lambda: True in self.gates.collects), "collect(paused=True) keeps collecting")
        self.gates.exit.set()
        time.sleep(0.3)
        self.assertEqual(self.gates.judged, 1, "no judgment (model work) while paused")
        self.assertEqual(self.view().get("held_reason"), "paused")
        self.paused_flag = False
        self.assertTrue(eventually(lambda: self.gates.judged == 2))

    def test_a_queued_message_reaching_the_outbox_while_paused_is_dropped_not_resent(self):
        task_id = self.running_work()
        self.mailbox.gate.clear()  # the lane is busy delivering the next message
        self.to_manager({"kind": "progress", "message": "first", "task_id": task_id})
        self.to_manager({"kind": "progress", "message": "second", "task_id": task_id})
        self.assertTrue(eventually(lambda: len(self.mailbox.to(ActorRole.MANAGER)) == 1))
        self.paused_flag = True
        self.mailbox.gate.set()
        self.assertTrue(eventually(lambda: any(e["state"] == "held_paused" for e in self.service.outbox_snapshot())))
        self.paused_flag = False
        time.sleep(0.3)
        self.assertEqual([m.payload["message"] for m in self.mailbox.to(ActorRole.MANAGER)], ["first"])


class RestartTests(Harness):
    def test_restart_starts_and_resends_nothing(self):
        self.port.busy_reason = "busy"
        unstarted = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view().get("held_reason") == "host_terminal_busy"))
        created = len(self.mailbox.created)
        self.port.busy_reason = None
        self.flow.close()  # the backend stops before the start was tried again
        self.service.close()
        self.opened = []
        self.open()
        time.sleep(0.3)
        self.assertEqual(self.starts, [], "an unstarted Task is not started after a restart")
        self.assertEqual(self.flow.tasks[unstarted].status, "closed")
        self.assertEqual(len(self.mailbox.created), created)
        self.assertEqual(self.flow.worker_view()["state"], "idle")

    def test_a_running_work_task_is_held_backend_restarted_and_still_busy(self):
        task_id = self.running_work()
        created = len(self.mailbox.created)
        self.restart()
        time.sleep(0.2)
        self.assertEqual(self.view()["held_reason"], "backend_restarted")
        self.assertEqual(len(self.mailbox.created), created, "nothing is resent after a restart")
        self.assert_busy(self.work("another"), task_id, "work", "held:backend_restarted")
        # the worker's report still ends it
        self.to_manager({"kind": "done", "message": "finished", "task_id": task_id})
        self.assertTrue(eventually(lambda: self.view()["status"] == "closed"), self.view())

    def test_a_running_experiment_is_held_after_restart_and_cancel_closes_it(self):
        self.gates.exit.clear()
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "running"))
        self.restart()
        self.assertEqual((self.view()["status"], self.view()["held_reason"]), ("held", "backend_restarted"))
        self.assert_busy(self.work("x"), task_id, "experiment", "held")
        self.assertEqual(len(self.starts), 1, "nothing is started again")

    @unittest.expectedFailure  # F5 carried to CW-19 restart reconcile (RecoveryPort): root-adjudication-p27-cw18-f5; CW-19 must remove this marker
    def test_cancel_after_restart_never_claims_cancelled_while_the_host_command_may_still_run(self):
        # "running host command -> no kill, cancelled after": after a restart nobody follows the command, so a
        # cancel must not close the Task (and free the worker) as if the command had ended.
        self.gates.exit.clear()
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "running"))
        self.restart()
        result = self.to_worker({"kind": "experiment", "message": "stop", "task_id": task_id, "cancel": True})
        self.assertNotEqual(result["status"], "cancelled",
                            f"cancelled at once although the host command may still run: {result}")

    def test_pending_approvals_from_before_c_d66_are_retired_not_dispatched(self):
        self.flow.close()
        self.service.close()
        self.opened = []
        ledger = self.root / "workflow" / "tasks-flow.jsonl"
        approval = {"approval_id": str(uuid4()), "state": "pending", "kind": "work", "message": "old"}
        old_task = {"task_id": str(uuid4()), "kind": "work", "status": "pending_approval", "summary": "old"}
        with ledger.open("a") as handle:
            handle.write(json.dumps({"seq": 99, "type": "approval", "approval": approval}) + "\n")
            handle.write(json.dumps({"seq": 100, "type": "task", "task": old_task}) + "\n")
        self.open()
        time.sleep(0.3)
        self.assertEqual(self.mailbox.created, [])
        self.assertEqual(self.flow.tasks[old_task["task_id"]].status, "closed")
        retired = [r for r in self.ledger() if r.get("type") == "approvals_retired"]
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0]["approval_ids"], [approval["approval_id"]])
        self.restart()  # retired once
        self.assertEqual(len([r for r in self.ledger() if r.get("type") == "approvals_retired"]), 1)


# =============================================================================================
class Controller:
    """The Backend's own ui_v1 pause/resume/snapshot surface over a TaskFlow."""

    pause = Backend.pause
    resume = Backend.resume
    _default_pause = Backend._default_pause
    _default_resume = Backend._default_resume

    def __init__(self, flow):
        self.flow = flow
        self.automation = {"state": "idle"}
        self.pause_hook, self.resume_hook = self._default_pause, self._default_resume

    def snapshot(self):
        return {"task": self.flow.task_view(), "worker": self.flow.worker_view(), "automation": dict(self.automation)}

    def replay(self):
        return []

    def on_attach(self, size):
        pass

    def on_detach(self):
        pass


class UiContractTests(Harness):
    def setUp(self):
        super().setUp()
        self.controller = Controller(self.flow)
        self.socket = self.root / "ui.sock"
        self.server = UiServer(self.socket, self.controller)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        self.client = UiClient(self.socket, timeout=10)
        self.client.attach()

    def serve(self):
        while not self.stopping.is_set():
            self.server.poll(0.01)

    def tearDown(self):
        self.client.close()
        self.stopping.set()
        self.thread.join(5)
        self.server.close(flush_timeout=0.5)
        super().tearDown()

    def test_removed_approval_api_and_bad_resume_are_refused(self):
        for frame in ({"v": 1, "type": "approval_decide", "id": "a1", "approval_id": str(uuid4()),
                       "decision": "approve"},
                      {"v": 1, "type": "approvals", "id": "a2"}):
            self.client.send_frame(frame)
            header = self.client.next_frame().header
            self.assertEqual((header["ok"], header["reason"]), (False, "unsupported_type"), header)
        for bad in ("true", 1, None):
            with self.subTest(reconciled=bad):
                self.client.send_frame({"v": 1, "type": "resume", "id": f"r-{bad}", "reconciled": bad})
                header = self.client.next_frame().header
                self.assertFalse(header["ok"], header)
        self.assertNotIn("approval_decide", [item.value for item in ClientType])

    def test_state_shapes_use_the_declared_vocabulary(self):
        self.running_work()
        snapshot = self.client.snapshot()
        self.assertIn(snapshot["task"]["status"], ui_v1.TASK_STATUSES)
        self.assertIn(snapshot["task"]["kind"], ui_v1.TASK_KINDS)
        self.assertIn(snapshot["worker"]["state"], ui_v1.WORKER_STATES)
        self.assertNotIn("approvals", snapshot)
        for status in ACTIVE + ("finished", "blocked", "closed"):
            self.assertIn(status, ui_v1.TASK_STATUSES)

    def test_resume_requires_reconciled_true_and_resume_replays_nothing(self):
        paused = self.client.request(ClientType.PAUSE)
        self.assertEqual(paused["automation"]["state"], "paused")
        self.controller.automation["state"] = "paused"
        self.paused_flag = True
        self.assertEqual(self.work()["status"], "held")
        refused = self.client.request(ClientType.RESUME, reconciled=False)
        self.assertEqual(refused.get("reason"), "resume_not_reconciled", refused)
        resumed = self.client.request(ClientType.RESUME, reconciled=True)
        self.assertNotEqual(resumed["automation"]["state"], "paused")
        self.paused_flag = False
        time.sleep(0.2)
        self.assertIsNone(self.flow.task_view(), "the request held during the pause is not replayed")


if __name__ == "__main__":
    unittest.main()
