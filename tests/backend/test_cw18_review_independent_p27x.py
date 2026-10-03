"""CW-18 re-verification (p27-cw18-test-03): the review findings R1, R2, R3, R5 and R6, adversarially.

Expectations come from ``result-p27-cw18-review-01-agent.json`` (findings and suggestions), the Root
adjudication ``root-adjudication-p27-cw18-r1.json`` and C-D66, not from the implementation:

- R1 (P1): the worker is free once the manager OMP *accepted* the done/blocked report into its session
  (``on_submitted``), not when the manager's turn ends. So the manager can call ``to_worker`` inside the
  turn that reads the report, and a manager turn longer than ``deliver_timeout`` leaves no busy Task. A
  report whose delivery is unknown/rejected *before* acceptance closes the Task as
  ``report_outcome_unknown`` / ``report_not_delivered``, the manager learns it from the notices of its next
  ``to_worker`` result, and nothing is resent.
- R2 (P2): a cancelled Task's not-yet-delivered TASK is never delivered to the worker; a later Task's
  untagged report is attributed only once that Task's own TASK reached the worker.
- R3 (P2): a done/blocked report (and a free-work TASK) that was queued but never submitted survives a
  pause and is delivered exactly once after the resume.
- R5 (P3): the host shell is held only from the moment Workbench types (``before_shell_input``), not
  during the TASK delivery / worker decision / worktree preparation.
- R6 (P3): a free-work run cancelled before its automation bind leaves AutomationController idle.

No OMP, no provider, no network: fakes from the p27w harness, a mailbox that models "accepted, manager
turn still running" and "target in a turn (deferred)", and the U3 automation fixture for R6.
"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))

import tempfile  # noqa: E402

from test_cw18_flow_independent_p27w import (  # noqa: E402
    AUTOMATION, M_SESSION, W_SESSION, Gates, Harness, Lifecycle, Port, Run, Workflow, eventually,
)
from workbench.backend.flow import HandoffService  # noqa: E402
from workbench.backend.flow_tasks import ExperimentPorts, TaskFlow  # noqa: E402
from workbench.contracts.v1 import ActorRole, MessageKind  # noqa: E402
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus  # noqa: E402
from workbench.tasks.repository import TaskRepository  # noqa: E402
from workbench.workflow.run import WorkflowHeld  # noqa: E402


class TurnMailbox:
    """Fake TaskMailbox: ``on_submitted`` at acceptance, then the target's turn, then the receipt."""

    def __init__(self):
        self.lock = threading.Lock()
        self.created: list[SimpleNamespace] = []
        self.delivered: list[str] = []  # message ids, at submission
        self.attempts: list[tuple[str, str]] = []  # (message_id, "deferred" | "submitted")
        self.turn = threading.Event()  # set: the manager's turn is over (a report's receipt returns at once)
        self.turn.set()
        self.defer: set[ActorRole] = set()  # targets that are in a turn: deliver answers DEFERRED
        self.status = MailboxStatus.OMP_PROCESSED

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

    def deliver(self, message, *, timeout=20, on_submitted=None):
        def receipt(status):
            return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1,
                                   status, {})
        with self.lock:
            if message.target_role in self.defer:
                self.attempts.append((message.message_id, "deferred"))
                return receipt(MailboxStatus.DEFERRED)
            self.delivered.append(message.message_id)
            self.attempts.append((message.message_id, "submitted"))
            status = self.status
        if status is MailboxStatus.OMP_PROCESSED and on_submitted is not None:
            on_submitted()  # api_accepted: the OMP took the message into its session
        if message.target_role is ActorRole.MANAGER and not self.turn.wait(timeout):
            return receipt(MailboxStatus.UNKNOWN)  # the manager's turn outlived the delivery timeout
        return receipt(status)

    # -- observations ------------------------------------------------------------------------------
    def to(self, role):
        with self.lock:
            return [m for m in self.created if m.target_role is role]

    def delivered_to(self, role, kind=None, task_id=None):
        with self.lock:
            ids = set(self.delivered)
            return [m for m in self.created if m.message_id in ids and m.target_role is role
                    and (kind is None or m.kind is kind) and (task_id is None or m.task_id == task_id)]

    def submitted_count(self, message_id):
        with self.lock:
            return sum(1 for mid, what in self.attempts if mid == message_id and what == "submitted")


class TurnRun(Run):
    """An experiment run whose report is accepted, after which the manager's turn lasts until ``gates.judge``."""

    def judge(self, *, paused=False, requires_code_change=False, on_report=None):
        record = {"worker_judgment": {"judgment": "success"}, "report": {"status": "omp_processed"}}
        if on_report is not None:
            on_report("sending", {})
            on_report("submitted", dict(record))
        self.gates.judge.wait(10)  # the manager's turn that reads the report
        self.gates.judged += 1
        self.repository.complete_run(self.run_id, {"judgment": "success"})
        return record


class TurnWorkflow(Workflow):
    def start(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell):
        self.gates.start.wait(10)
        run_id = self.repository.start_run(task_id, revision, inputs={})
        self.starts.append({"task_id": task_id, "revision": revision, "run_id": run_id, "shell": shell})
        return TurnRun(self.repository, run_id, self.gates)


class LateHoldWorkflow:
    """A workflow that says when it types (``before_shell_input``): its earlier phases run unheld (R5)."""

    def __init__(self, repository, starts, gates, port):
        self.repository, self.starts, self.gates, self.port = repository, starts, gates, port
        self.entered = threading.Event()
        self.phase_gate = threading.Event()  # TASK delivery + worker decision + worktree preparation
        self.held_in_phase: list[bool] = []
        self.held_when_typing: list[bool] = []
        self.typed = False

    def start(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell, before_shell_input=None):
        self.entered.set()
        self.held_in_phase.append(self.port.held)
        self.phase_gate.wait(10)
        self.held_in_phase.append(self.port.held)
        if before_shell_input is not None:
            before_shell_input()  # raises WorkflowHeld when the shell is no longer idle
        self.held_when_typing.append(self.port.held)
        self.typed = True
        run_id = self.repository.start_run(task_id, revision, inputs={})
        self.starts.append({"task_id": task_id, "revision": revision, "run_id": run_id, "shell": shell})
        return Run(self.repository, run_id, self.gates)


class TurnHarness(Harness):
    DELIVER_TIMEOUT = 20.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p27x-flow-", dir="/tmp")
        self.root = Path(self.tmp.name)
        (self.root / "workflow").mkdir(mode=0o700)
        self.db = self.root / "tasks.sqlite3"
        self.mailbox = TurnMailbox()
        self.port = Port()
        self.gates = Gates()
        self.starts: list[dict] = []
        self.lifecycle = Lifecycle()
        self.paused_flag = False
        self.worker_idle: bool | None = True
        self.sensitive = ("p27x-SECRET-value-0123456789",)
        self.n = 0
        self.opened: list = []
        self.workflows: list = []
        self.open()

    def tearDown(self):
        self.mailbox.turn.set()
        self.mailbox.defer.clear()
        for gate in (self.gates.start, self.gates.exit, self.gates.judge):
            gate.set()
        for item in self.workflows:
            item.phase_gate.set()
        for item in reversed(self.opened):
            item.close()
        self.tmp.cleanup()

    def make_workflow(self, repository):
        return TurnWorkflow(repository, self.starts, self.gates)

    def open(self, *, start=True):
        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=self.mailbox,
                                 paused=lambda: self.paused_flag, sensitive_values=lambda: self.sensitive,
                                 retry_interval=0.01, deliver_timeout=self.DELIVER_TIMEOUT)
        ports = ExperimentPorts(host_shell=lambda: self.port, make_workflow=self.make_workflow,
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

    # -- helpers ------------------------------------------------------------------------------------
    def cancel(self, task_id, kind="work"):
        return self.to_worker({"kind": kind, "message": "stop", "task_id": task_id, "cancel": True})

    def report_message_ids(self):
        return [m.message_id for m in self.mailbox.to(ActorRole.MANAGER)]

    def in_manager_turn(self):
        """The manager's turn is running: it accepts a report but its receipt only comes at the turn's end."""
        self.mailbox.turn.clear()

    def wait_accepted(self, count=1):
        self.assertTrue(eventually(lambda: len(self.mailbox.delivered_to(ActorRole.MANAGER)) >= count),
                        "the report was accepted by the manager OMP")


# =============================================================================================
class R1ReportAcceptedFreesTheWorker(TurnHarness):
    def test_to_worker_in_the_turn_that_reads_the_done_report_is_accepted(self):
        task_id = self.running_work()
        self.in_manager_turn()
        self.to_manager({"kind": "done", "message": "finished", "task_id": task_id})
        self.wait_accepted()
        # the manager's turn is still running (the receipt has not returned): it dispatches the next Task now
        result = self.work("the next step")
        self.assertEqual(result["status"], "dispatched", f"same-turn to_worker must not be worker_busy: {result}")
        self.assertNotEqual(result["task_id"], task_id)
        old = self.flow.tasks[task_id]
        self.assertEqual((old.status, old.closed_reason), ("closed", "done"))
        with self.repo() as repository:
            self.assertEqual(repository.get_run_history(old.last_result["run_id"])[-1]["kind"], "completed")
        self.assertTrue(eventually(lambda: self.view()["task_id"] == result["task_id"]
                                   and self.view()["status"] == "running"), self.view())
        self.assertEqual(len(self.report_message_ids()), 1, "the report was created and submitted once")

    def test_to_worker_in_the_turn_that_reads_a_blocked_report_is_accepted(self):
        task_id = self.running_work()
        self.in_manager_turn()
        self.to_manager({"kind": "blocked", "message": "need access", "reason": "permission", "task_id": task_id})
        self.wait_accepted()
        self.assertTrue(eventually(lambda: self.view()["status"] == "blocked"), self.view())
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        result = self.work("something else")
        self.assertEqual(result["status"], "dispatched", result)
        self.assertEqual(self.flow.tasks[task_id].closed_reason, "superseded_by_new_task")

    def test_a_manager_turn_longer_than_the_delivery_timeout_does_not_leave_the_task_busy(self):
        self.DELIVER_TIMEOUT = 0.3
        self.restart()
        task_id = self.running_work()
        self.in_manager_turn()  # the turn never ends within the test's delivery timeout
        self.to_manager({"kind": "done", "message": "finished", "task_id": task_id})
        self.wait_accepted()
        time.sleep(0.8)  # well past deliver_timeout: the receipt is UNKNOWN after the acceptance
        task = self.flow.tasks[task_id]
        self.assertEqual((task.status, task.closed_reason), ("closed", "done"), self.view())
        self.assertEqual(self.flow.worker_view()["state"], "idle", "the Task is not stuck busy/held")
        result = self.work("next")
        self.assertEqual(result["status"], "dispatched", result)
        self.assertEqual([n for n in result.get("notices", ()) if n.get("kind", "").startswith("report_")], [],
                         "an accepted report whose turn timed out is not announced as lost")
        self.assertEqual(len(self.mailbox.to(ActorRole.MANAGER)), 1)
        self.assertEqual(self.mailbox.submitted_count(self.report_message_ids()[0]), 1, "never resent")

    def test_the_experiment_report_accepted_in_the_turn_frees_the_worker_for_a_new_task(self):
        self.gates.judge.clear()  # the manager's turn that reads the experiment report lasts
        self.assertEqual(self.experiment()["status"], "dispatched")
        self.assertTrue(eventually(lambda: self.view()["status"] == "finished"), self.view())
        self.assertEqual(self.gates.judged, 0, "the manager's turn has not ended")
        result = self.work("a different job")
        self.assertEqual(result["status"], "dispatched", result)

    def test_an_experiment_rerun_requested_in_the_report_turn_is_accepted(self):
        self.gates.judge.clear()
        task_id = self.experiment()["task_id"]
        self.assertTrue(eventually(lambda: self.view()["status"] == "finished"), self.view())
        self.assertEqual(self.gates.judged, 0)
        result = self.to_worker({"kind": "experiment", "message": "again", "task_id": task_id, "run": True})
        self.assertEqual((result["status"], result.get("retry")), ("dispatched", 1), result)
        self.gates.judge.set()  # the first turn ends; the re-run then starts
        self.assertTrue(eventually(lambda: len(self.starts) == 2 and self.view()["status"] == "finished"),
                        (self.starts, self.view()))

    def test_unknown_before_acceptance_closes_as_report_outcome_unknown_and_is_announced_once(self):
        task_id = self.running_work()
        self.mailbox.status = MailboxStatus.UNKNOWN
        self.to_manager({"kind": "done", "message": "finished", "task_id": task_id})
        self.assertTrue(eventually(lambda: self.flow.tasks[task_id].status == "closed"), self.view())
        self.assertEqual(self.flow.tasks[task_id].closed_reason, "report_outcome_unknown")
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        self.mailbox.status = MailboxStatus.OMP_PROCESSED
        first = self.work("next")
        self.assertEqual(first["status"], "dispatched", first)
        notices = [n for n in first.get("notices", ()) if n.get("kind") == "report_outcome_unknown"]
        self.assertEqual(len(notices), 1, first)
        self.assertEqual((notices[0]["task_id"], notices[0]["report_kind"]), (task_id, "done"))
        time.sleep(0.15)
        self.assertEqual(len(self.mailbox.to(ActorRole.MANAGER)), 1, "no second report was created")
        self.assertEqual(self.mailbox.submitted_count(self.report_message_ids()[0]), 1, "and none was resent")
        # a notice is delivered once; the next to_worker (worker_busy answers carry notices too) sees none
        second = self.work("and again")
        self.assertEqual(second["status"], "worker_busy", second)
        self.assertEqual([n for n in second.get("notices", ()) if n.get("kind") == "report_outcome_unknown"], [])

    def test_a_rejected_blocked_report_closes_as_report_not_delivered_without_resend(self):
        task_id = self.running_work()
        self.mailbox.status = MailboxStatus.REJECTED
        self.to_manager({"kind": "blocked", "message": "stuck", "reason": "x", "task_id": task_id})
        self.assertTrue(eventually(lambda: self.flow.tasks[task_id].status == "closed"), self.view())
        self.assertEqual(self.flow.tasks[task_id].closed_reason, "report_not_delivered")
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        self.mailbox.status = MailboxStatus.OMP_PROCESSED
        result = self.work("next")
        self.assertEqual(result["status"], "dispatched", result)
        self.assertEqual([n["kind"] for n in result["notices"] if n.get("task_id") == task_id],
                         ["report_not_delivered"])
        time.sleep(0.15)
        self.assertEqual(len(self.mailbox.to(ActorRole.MANAGER)), 1)
        self.assertEqual(self.mailbox.submitted_count(self.report_message_ids()[0]), 1)


# =============================================================================================
class R2CancelWithdrawsTheUndeliveredTask(TurnHarness):
    def test_a_cancelled_task_is_never_delivered_to_the_worker_and_the_next_task_is(self):
        self.mailbox.defer.add(ActorRole.WORKER)  # the worker OMP is still in a turn: the TASK is deferred
        a = self.work("task A")
        self.assertEqual(a["status"], "dispatched", a)
        a_id = a["task_id"]
        self.assertTrue(eventually(lambda: len(self.mailbox.to(ActorRole.WORKER)) == 1), "A's TASK was created")
        self.assertEqual(self.cancel(a_id)["status"], "cancelled")
        b = self.work("task B")
        self.assertEqual(b["status"], "dispatched", b)
        b_id = b["task_id"]
        self.mailbox.defer.discard(ActorRole.WORKER)  # the worker becomes idle
        self.assertTrue(eventually(lambda: self.mailbox.delivered_to(ActorRole.WORKER, MessageKind.TASK, b_id)),
                        "B's TASK reaches the worker")
        time.sleep(0.3)
        self.assertEqual(self.mailbox.delivered_to(ActorRole.WORKER, task_id=a_id), [],
                         "nothing of the cancelled Task A (neither TASK nor anything else) reached the worker "
                         "before or after its cancel")
        self.assertEqual(self.mailbox.submitted_count(self.mailbox.to(ActorRole.WORKER)[0].message_id), 0)
        self.assertEqual(self.flow.tasks[a_id].closed_reason, "cancelled")
        a_attempts = [m.message_id for m in self.mailbox.to(ActorRole.WORKER) if m.task_id == a_id]
        for message_id in a_attempts:
            self.assertEqual(self.mailbox.submitted_count(message_id), 0)

    def test_an_untagged_report_is_not_attributed_to_a_task_whose_instruction_has_not_reached_the_worker(self):
        self.mailbox.defer.add(ActorRole.WORKER)
        a = self.work("task A")
        self.assertTrue(eventually(lambda: len(self.mailbox.to(ActorRole.WORKER)) == 1))
        self.cancel(a["task_id"])
        b = self.work("task B")
        b_id = b["task_id"]
        self.assertTrue(eventually(lambda: len(self.mailbox.to(ActorRole.WORKER)) == 2))
        # the worker (still working on whatever it had) reports done without a task_id: B has no TASK there
        refused = self.to_manager({"kind": "done", "message": "all done"})
        self.assertEqual(refused["status"], "rejected", refused)
        self.assertEqual(refused["reason"], "task_not_delivered", refused)
        time.sleep(0.1)
        self.assertNotEqual(self.flow.tasks[b_id].status, "closed", "B was not completed by a report that is not its own")
        self.assertEqual(self.mailbox.to(ActorRole.MANAGER), [], "nothing was created for the manager")
        # once B's own TASK reached the worker an untagged report belongs to B
        self.mailbox.defer.clear()
        self.assertTrue(eventually(lambda: self.mailbox.delivered_to(ActorRole.WORKER, MessageKind.TASK, b_id)))
        accepted = self.to_manager({"kind": "done", "message": "B done"})
        self.assertEqual(accepted["status"], "queued", accepted)
        self.assertTrue(eventually(lambda: self.flow.tasks[b_id].closed_reason == "done"), self.view())

    def test_cancel_after_the_task_was_delivered_still_tells_the_worker(self):
        task_id = self.running_work()
        self.assertTrue(eventually(lambda: self.mailbox.delivered_to(ActorRole.WORKER, MessageKind.TASK, task_id)))
        result = self.cancel(task_id)
        self.assertEqual((result["status"], result["worker_notified"]), ("cancelled", True), result)
        self.assertTrue(eventually(lambda: len(self.mailbox.delivered_to(ActorRole.WORKER, task_id=task_id)) == 2),
                        "TASK then the cancel notice")


# =============================================================================================
class R3PauseKeepsAQueuedReport(TurnHarness):
    def pause_until_the_lane_has_seen_it(self):
        self.paused_flag = True
        # the lane retries every ~10 ms: give it several passes while paused
        time.sleep(0.4)

    def test_a_done_report_queued_while_the_manager_is_in_a_turn_survives_a_pause_and_is_delivered_once(self):
        task_id = self.running_work()
        self.mailbox.defer.add(ActorRole.MANAGER)  # the manager is in a turn: the report is deferred
        self.to_manager({"kind": "done", "message": "finished", "task_id": task_id})
        self.assertTrue(eventually(lambda: any(w == "deferred" for _, w in self.mailbox.attempts)), "deferred")
        self.pause_until_the_lane_has_seen_it()
        self.assertEqual(self.mailbox.delivered_to(ActorRole.MANAGER), [], "nothing is delivered while paused")
        self.assertNotEqual(self.flow.tasks[task_id].status, "closed")
        # resume (reconciled) and the manager's turn ends
        self.paused_flag = False
        self.mailbox.defer.clear()
        self.assertTrue(eventually(lambda: len(self.mailbox.delivered_to(ActorRole.MANAGER)) == 1),
                        "the report reaches the manager after the resume")
        self.assertTrue(eventually(lambda: self.flow.tasks[task_id].closed_reason == "done"), self.view())
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        time.sleep(0.3)
        self.assertEqual(len(self.mailbox.to(ActorRole.MANAGER)), 1, "one report message")
        self.assertEqual(self.mailbox.submitted_count(self.report_message_ids()[0]), 1, "delivered exactly once")
        self.assertEqual(self.work("next")["status"], "dispatched")

    def test_a_blocked_report_queued_across_a_pause_is_delivered_once_after_the_resume(self):
        task_id = self.running_work()
        self.mailbox.defer.add(ActorRole.MANAGER)
        self.to_manager({"kind": "blocked", "message": "stuck", "reason": "x", "task_id": task_id})
        self.assertTrue(eventually(lambda: any(w == "deferred" for _, w in self.mailbox.attempts)))
        self.pause_until_the_lane_has_seen_it()
        self.assertEqual(self.mailbox.delivered_to(ActorRole.MANAGER), [])
        self.paused_flag = False
        self.mailbox.defer.clear()
        self.assertTrue(eventually(lambda: self.view()["status"] == "blocked"), self.view())
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        time.sleep(0.2)
        self.assertEqual(self.mailbox.submitted_count(self.report_message_ids()[0]), 1)
        self.assertEqual(len(self.mailbox.to(ActorRole.MANAGER)), 1)

    def test_a_free_work_task_queued_when_the_pause_comes_is_delivered_once_after_the_resume(self):
        self.mailbox.defer.add(ActorRole.WORKER)
        result = self.work("task A")
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(eventually(lambda: any(w == "deferred" for _, w in self.mailbox.attempts)))
        self.pause_until_the_lane_has_seen_it()
        self.assertEqual(self.mailbox.delivered_to(ActorRole.WORKER), [])
        self.paused_flag = False
        self.mailbox.defer.clear()
        self.assertTrue(eventually(lambda: len(self.mailbox.delivered_to(ActorRole.WORKER, MessageKind.TASK)) == 1),
                        "the TASK reaches the worker after the resume")
        time.sleep(0.3)
        worker_messages = self.mailbox.to(ActorRole.WORKER)
        self.assertEqual(len(worker_messages), 1)
        self.assertEqual(self.mailbox.submitted_count(worker_messages[0].message_id), 1)
        self.assertTrue(eventually(lambda: self.view()["status"] == "running"))
        # the worker can now report: no no_task_message, and the Task closes
        self.assertEqual(self.to_manager({"kind": "done", "message": "ok", "task_id": result["task_id"]})["status"],
                         "queued")
        self.assertTrue(eventually(lambda: self.flow.tasks[result["task_id"]].closed_reason == "done"))


# =============================================================================================
class R5HostShellHeldOnlyFromTyping(TurnHarness):
    def make_workflow(self, repository):
        workflow = LateHoldWorkflow(repository, self.starts, self.gates, self.port)
        self.workflows.append(workflow)
        return workflow

    def test_the_host_shell_is_not_held_during_the_task_delivery_and_worker_decision(self):
        self.assertEqual(self.experiment()["status"], "dispatched")
        workflow_ready = lambda: bool(self.workflows) and self.workflows[0].entered.is_set()  # noqa: E731
        self.assertTrue(eventually(workflow_ready), "the start is in its TASK-delivery/worker-decision phase")
        time.sleep(0.2)
        workflow = self.workflows[0]
        self.assertFalse(self.port.held, "user input must not be refused before Workbench types")
        self.assertEqual(self.port.holds, 0)
        self.assertEqual(workflow.held_in_phase, [False])
        self.assertFalse(workflow.typed)
        workflow.phase_gate.set()  # the worker decided; the first keystroke is next
        self.assertTrue(eventually(lambda: workflow.typed and self.view()["status"] == "finished"), self.view())
        self.assertEqual(workflow.held_when_typing, [True], "held from the typing")
        self.assertEqual(self.port.holds, 1)
        self.assertFalse(self.port.held, "the hold is released once the start returned")
        self.assertEqual(len(self.starts), 1)

    def test_a_line_the_user_started_during_the_phase_stops_the_start_and_nothing_is_typed(self):
        self.assertEqual(self.experiment()["status"], "dispatched")
        self.assertTrue(eventually(lambda: bool(self.workflows) and self.workflows[0].entered.is_set()))
        workflow = self.workflows[0]
        self.assertFalse(self.port.held, "the user could type until now")
        self.port.busy_reason = "user_partial_line"  # the user started typing a command meanwhile
        workflow.phase_gate.set()
        self.assertTrue(eventually(lambda: (self.flow.tasks[next(iter(self.flow.tasks))].held_reason or "")
                                   .startswith("start_failed")), self.view())
        task = next(iter(self.flow.tasks.values()))
        self.assertEqual(task.held_reason, "start_failed:host_terminal_busy")
        self.assertFalse(workflow.typed, "nothing was typed into the user's shell")
        self.assertEqual(self.starts, [])
        self.assertFalse(self.port.held, "the hold is never left behind")
        self.assertEqual(self.flow.worker_view()["state"], "idle", "the worker is free; the manager may start again")


# =============================================================================================
try:
    from test_automation_loop import AutomationFixture, PersistingMailbox
except Exception:  # pragma: no cover - the fixture needs the bridge stack
    AutomationFixture = None


if AutomationFixture is not None:
    class R6QuickCancelLeavesAutomationIdle(AutomationFixture):
        def _work_run(self):
            task = self.approve({"goal": "clean parser", "paths": ["src/"]}, ["src/"])
            run_id = self.repository.start_run(task, 1, inputs={"kind": "work"})
            message = PersistingMailbox(self.repository, self.bridge).create_message(
                task, 1, run_id, ActorRole.MANAGER, ActorRole.WORKER, MessageKind.TASK, {"handoff": "to_worker"})
            return task, run_id, message

        def test_cancel_before_the_bind_leaves_automation_idle_and_unbound(self):
            task, run_id, message = self._work_run()
            self.repository.cancel_run(run_id, "cancelled_by_manager")
            self.controller.run_ended(run_id)  # run_ended finds nothing bound yet
            self.controller.work_started(task, 1, run_id, message.message_id)  # the TASK event arrives late
            status = self.status()
            self.assertEqual(status["state"], "idle", (status, self.logs))
            self.assertFalse((status.get("run") or {}).get("bound"), status)
            self.assertEqual(self.tick(120.0)["outcome"], "idle", "no run is bound: nothing is reviewed for a closed run")
            self.assertEqual(self.worker.reviews(), [])

        def test_cancel_after_the_bind_leaves_automation_idle(self):
            task, run_id, message = self._work_run()
            self.controller.work_started(task, 1, run_id, message.message_id)
            self.assertEqual(self.status()["state"], "active", self.logs)
            self.repository.cancel_run(run_id, "cancelled_by_manager")
            self.controller.run_ended(run_id)
            self.assertEqual(self.status()["state"], "idle", self.status())

        def test_a_cancelled_runs_ended_mark_does_not_suppress_the_next_runs_bind(self):
            task, run_id, message = self._work_run()
            self.repository.cancel_run(run_id, "cancelled_by_manager")
            self.controller.run_ended(run_id)
            self.controller.work_started(task, 1, run_id, message.message_id)
            self.assertEqual(self.status()["state"], "idle")
            second_task, second_run, second_message = self._work_run()
            self.controller.work_started(second_task, 1, second_run, second_message.message_id)
            status = self.status()
            self.assertEqual((status["state"], status["run"]["run_id"], status["run"]["bound"]),
                             ("active", second_run, True), (status, self.logs))


if __name__ == "__main__":
    unittest.main()
