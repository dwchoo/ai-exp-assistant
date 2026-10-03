"""CW-18 review corrections (p27-cw18-review-01 R1, R2, R3, R5, R6): no OMP, no provider.

- R1: the worker is free once its done/blocked (work) or experiment report is
  accepted into the manager OMP's session (the mailbox's ``api_accepted``), not
  when the manager's turn ends: a ``to_worker`` in the report turn is accepted,
  a long manager turn never leaves the Task busy, and a report whose delivery
  outcome is unknown closes the Task as ``report_outcome_unknown`` (no resend;
  the manager is told in the next tool result's notices).
- R2: a cancel withdraws the Task's not yet sent messages; the worker never gets
  a cancelled TASK after its cancel, and an untagged report is never attributed
  to a Task whose TASK the worker has not received.
- R3: a done/blocked report waiting for a busy manager survives a pause and is
  delivered after the resume (never sent before, so not a replay).
- R5: the user's host input is held only from the moment Workbench types.
- R6: a cancel before the automation bind leaves automation idle.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

from workbench.backend.flow import HandoffService, OutboundMessage
from workbench.backend.flow_tasks import ExperimentPorts, TaskFlow
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageKind
from workbench.ipc.bridge_g3.mailbox import (
    BridgePeer, BridgeTimeout, DeliveryReceipt, MailboxStatus, TaskMailbox,
)
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.workflow.run import TaskWorkflow

from test_task_flow import (
    AUTOMATION, FakeMailbox, FakePort, FakeWorkflow, FlowFixture, Gates, PublicMailboxFixture, execution, git,
    request, wait_until,
)


def receipt(message, status):
    return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1, status, {})


class TurnMailbox(FakeMailbox):
    """Deliveries modelled on ``TaskMailbox``: ``on_submitted`` at the api ack, the receipt after the turn.

    - ``busy[role]`` set: the target is in another turn (DEFERRED, nothing submitted);
    - ``ack[role]`` closed: the ack has not arrived yet (the lane is inside ``deliver``);
    - manager turns end when ``turn_end`` is set (else UNKNOWN after ``timeout``, as the real mailbox);
    - ``fail_manager`` (a status): the manager delivery ends so without any submission.
    """

    def __init__(self):
        super().__init__()
        self.busy = {ActorRole.MANAGER: threading.Event(), ActorRole.WORKER: threading.Event()}
        self.ack = {ActorRole.MANAGER: threading.Event(), ActorRole.WORKER: threading.Event()}
        for event in self.ack.values():
            event.set()
        self.turn_end = threading.Event()
        self.turn_end.set()
        self.in_turn = threading.Event()
        self.fail_manager: MailboxStatus | None = None
        self.lock = threading.Lock()

    def deliver(self, message, *, timeout=20, on_submitted=None):
        role = message.target_role
        if self.busy[role].is_set():
            return DeliveryReceipt(message.message_id, None, role, message.session_id, 1, MailboxStatus.DEFERRED,
                                   {"reason": "current_omp_state_not_safe"})
        if role is ActorRole.MANAGER and self.fail_manager is not None:
            return receipt(message, self.fail_manager)
        self.ack[role].wait(10)
        with self.lock:
            self.delivered.append(message.message_id)
        if on_submitted is not None:
            on_submitted()
        if role is ActorRole.MANAGER:
            self.in_turn.set()
            ended = self.turn_end.wait(timeout)
            return receipt(message, MailboxStatus.OMP_PROCESSED if ended else MailboxStatus.UNKNOWN)
        return receipt(message, MailboxStatus.OMP_PROCESSED)

    def sent_to(self, role):
        with self.lock:
            delivered = set(self.delivered)
        return [m for m in self.created if m.target_role is role and m.message_id in delivered]


class CorrectionFixture(FlowFixture):
    def setUp(self):
        super().setUp()
        for flow in self.flows:
            flow.close()
        for service in self.services:
            service.close()
        self.flows.clear()
        self.services.clear()
        self.mailbox = TurnMailbox()
        self.open()

    def tearDown(self):
        self.mailbox.turn_end.set()
        for event in (*self.mailbox.ack.values(),):
            event.set()
        for event in self.mailbox.busy.values():
            event.clear()
        super().tearDown()

    def work_args(self, goal="A"):
        return {"kind": "work", "message": f"do {goal}", "spec": {"goal": goal, "paths": ["src/"]}}

    def running(self, goal="A"):
        result = self.to_worker(self.work_args(goal))
        self.assertEqual(result["status"], "dispatched", result)
        task_id = result["task_id"]
        self.assertTrue(wait_until(lambda: any(m.payload.get("task_id") == task_id
                                               for m in self.mailbox.sent_to(ActorRole.WORKER))))
        self.assertTrue(wait_until(lambda: self.flow.tasks[task_id].status == "running"))
        return task_id


# -- R1 ------------------------------------------------------------------------------------------
class ReportFreesTheWorkerOnSubmissionTests(CorrectionFixture):
    def test_to_worker_in_the_turn_that_reads_the_done_report_is_accepted(self):
        task_a = self.running("A")
        self.mailbox.turn_end.clear()  # the manager's report turn runs on
        self.assertEqual(self.to_manager({"kind": "done", "message": "A done"})["status"], "queued")
        self.assertTrue(self.mailbox.in_turn.wait(5))
        nxt = self.to_worker(self.work_args("B"))  # issued inside the report turn
        self.assertEqual(nxt["status"], "dispatched", nxt)
        self.assertEqual((self.flow.tasks[task_a].status, self.flow.tasks[task_a].closed_reason), ("closed", "done"))
        self.assertIsNone(self.current_run(task_a))
        self.mailbox.turn_end.set()
        time.sleep(0.2)
        self.assertEqual(self.flow.worker_view(), {"state": "busy", "task_id": nxt["task_id"]})
        self.assertEqual(self.flow.tasks[task_a].closed_reason, "done")
        self.assertEqual(len(self.mailbox.sent_to(ActorRole.MANAGER)), 1)

    def test_blocked_report_frees_the_worker_inside_the_report_turn(self):
        task_a = self.running("A")
        self.mailbox.turn_end.clear()
        self.to_manager({"kind": "blocked", "message": "need input"})
        self.assertTrue(self.mailbox.in_turn.wait(5))
        self.assertTrue(wait_until(lambda: self.flow.tasks[task_a].status == "blocked"))
        follow = self.to_worker({"kind": "work", "message": "input is X", "task_id": task_a})
        self.assertEqual(follow["status"], "queued", follow)
        self.mailbox.turn_end.set()
        time.sleep(0.2)
        self.assertEqual(self.flow.tasks[task_a].status, "running", "the follow-up is not undone by the turn end")

    def test_a_manager_turn_longer_than_the_deliver_timeout_never_leaves_the_task_busy(self):
        self.service._deliver_timeout = 0.3
        task_a = self.running("A")
        self.mailbox.turn_end.clear()  # the turn outlasts the 0.3 s receipt window -> UNKNOWN receipt
        self.to_manager({"kind": "done", "message": "A done"})
        self.assertTrue(wait_until(lambda: any(e["state"] == "unknown" for e in self.service.outbox_snapshot())))
        task = self.flow.tasks[task_a]
        self.assertEqual((task.status, task.closed_reason), ("closed", "done"))
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        self.assertEqual(self.to_worker(self.work_args("B"))["status"], "dispatched")
        self.assertEqual(len(self.mailbox.sent_to(ActorRole.MANAGER)), 1, "nothing is resent")

    def test_an_unknown_report_outcome_closes_the_task_without_resend_and_tells_the_manager(self):
        for kind in ("done", "blocked"):
            with self.subTest(kind=kind):
                task_id = self.running(kind)
                run_id = self.flow.tasks[task_id].run_id
                self.mailbox.fail_manager = MailboxStatus.UNKNOWN
                self.to_manager({"kind": kind, "message": f"{kind} report"})
                self.assertTrue(wait_until(lambda: self.flow.tasks[task_id].status == "closed"),
                                self.flow.tasks[task_id])
                self.assertEqual(self.flow.tasks[task_id].closed_reason, "report_outcome_unknown")
                self.assertEqual(self.flow.worker_view()["state"], "idle")
                self.assertIsNone(self.current_run(task_id))
                self.mailbox.fail_manager = None
                time.sleep(0.2)
                self.assertFalse(any(m.payload.get("message") == f"{kind} report"
                                     for m in self.mailbox.sent_to(ActorRole.MANAGER)), "never resent")
                nxt = self.to_worker(self.work_args(f"after {kind}"))
                self.assertEqual(nxt["status"], "dispatched", nxt)
                notice = [n for n in nxt.get("notices", []) if n["kind"] == "report_outcome_unknown"]
                self.assertEqual(len(notice), 1, nxt)
                self.assertEqual((notice[0]["task_id"], notice[0]["run_id"], notice[0]["report_kind"]),
                                 (task_id, run_id, kind))
                self.to_worker({"kind": "work", "message": "stop", "task_id": nxt["task_id"], "cancel": True})

    def test_a_to_worker_racing_the_report_ack_waits_for_it(self):
        task_a = self.running("A")
        self.mailbox.ack[ActorRole.MANAGER].clear()  # the lane is inside deliver, the ack not yet seen
        self.to_manager({"kind": "done", "message": "A done"})
        time.sleep(0.1)
        results = []
        thread = threading.Thread(target=lambda: results.append(self.to_worker(self.work_args("B"))))
        thread.start()
        time.sleep(0.3)
        self.mailbox.ack[ActorRole.MANAGER].set()
        thread.join(10)
        self.assertEqual(results[0]["status"], "dispatched", results)
        self.assertEqual(self.flow.tasks[task_a].closed_reason, "done")


class ExperimentReportGates:
    def __init__(self):
        self.turn_end = threading.Event()
        self.turn_end.set()
        self.in_turn = threading.Event()
        self.report_status = "omp_processed"
        self.submit = True


class HookedRun:
    """A run whose judge() delivers its report the way WorkflowRun does with ``on_report``."""

    def __init__(self, repository, run_id, gates):
        self.repository, self.run_id, self.gates = repository, run_id, gates
        self.task_message_id = str(uuid4())
        self._record = {}

    def collect(self, *, timeout=1, paused=False):
        return {"shell_state": "exited", "exit_confirmed": True}

    def judge(self, *, on_report=None):
        record = {"worker_judgment": {"judgment": "success"}, "report": {"status": "delivery_unknown"}}
        if on_report is not None:
            on_report("sending", dict(record))
        if self.gates.submit:
            self.repository.complete_run(self.run_id, {"judgment": "success"})
            record["report"]["status"] = "api_returned"
            if on_report is not None:
                on_report("submitted", dict(record))
            self.gates.in_turn.set()
            ended = self.gates.turn_end.wait(10)
            record["report"]["status"] = "omp_processed" if ended else "unknown"
        else:
            record["report"]["status"] = self.gates.report_status
        return record

    def close(self):
        pass


class HookedWorkflow(FakeWorkflow):
    def __init__(self, repository, log, gates):
        super().__init__(repository, log)
        self.report_gates = gates

    def start(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell):
        run_id = self.repository.start_run(task_id, revision, inputs={})
        self.log.append({"task_id": task_id, "revision": revision, "run_id": run_id})
        return HookedRun(self.repository, run_id, self.report_gates)


class ExperimentReportTests(FlowFixture):
    def setUp(self):
        self.report_gates = ExperimentReportGates()
        super().setUp()

    def tearDown(self):
        self.report_gates.turn_end.set()
        super().tearDown()

    def open(self, *, start=True):
        flow = super().open(start=False)
        flow._experiment.make_workflow = lambda repository: HookedWorkflow(repository, self.runs, self.report_gates)
        if start:
            flow.start()
        return flow

    def test_rerun_in_the_turn_that_reads_the_experiment_report_is_accepted(self):
        self.report_gates.turn_end.clear()
        first = self.new_experiment()
        self.assertTrue(self.report_gates.in_turn.wait(5))
        task = self.flow.tasks[first["task_id"]]
        self.assertTrue(wait_until(lambda: task.status == "finished"), task)
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        again = self.to_worker({"kind": "experiment", "message": "again", "task_id": first["task_id"],
                                "run": True})
        self.assertEqual((again["status"], again["retry"]), ("dispatched", 1), again)
        self.report_gates.turn_end.set()
        self.assertTrue(wait_until(lambda: len(self.runs) == 2 and task.status == "finished"), task)
        self.assertEqual(task.last_result["run_id"], self.runs[1]["run_id"])

    def test_a_new_task_in_the_report_turn_is_accepted(self):
        self.report_gates.turn_end.clear()
        first = self.new_experiment()
        self.assertTrue(self.report_gates.in_turn.wait(5))
        self.assertTrue(wait_until(lambda: self.flow.tasks[first["task_id"]].status == "finished"))
        nxt = self.new_work()
        self.assertEqual(nxt["status"], "dispatched", nxt)
        self.report_gates.turn_end.set()
        time.sleep(0.2)
        self.assertEqual(self.flow.tasks[first["task_id"]].closed_reason, "superseded_by_new_task")
        self.assertEqual(self.flow.worker_view(), {"state": "busy", "task_id": nxt["task_id"]})

    def test_an_unknown_experiment_report_closes_the_task_and_the_run(self):
        self.report_gates.submit = False
        self.report_gates.report_status = "unknown"
        first = self.new_experiment()
        task = self.flow.tasks[first["task_id"]]
        self.assertTrue(wait_until(lambda: task.status == "closed"), task)
        self.assertEqual(task.closed_reason, "report_outcome_unknown")
        self.assertIsNone(self.current_run(first["task_id"]))
        self.assertEqual(self.flow.worker_view()["state"], "idle")
        nxt = self.new_work()
        self.assertIn("report_outcome_unknown", [n["kind"] for n in nxt.get("notices", [])], nxt)


# -- R2 ------------------------------------------------------------------------------------------
class CancelWithdrawsTests(CorrectionFixture):
    def test_cancel_withdraws_a_deferred_task_and_the_worker_never_gets_it(self):
        self.mailbox.busy[ActorRole.WORKER].set()  # the worker OMP is in a turn: the TASK waits
        task_a = self.to_worker(self.work_args("A"))["task_id"]
        self.assertTrue(wait_until(lambda: any(m.payload.get("task_id") == task_a for m in self.mailbox.created)))
        time.sleep(0.1)
        cancel = self.to_worker({"kind": "work", "message": "stop A", "task_id": task_a, "cancel": True})
        self.assertEqual(cancel["status"], "cancelled", cancel)
        task_b = self.to_worker(self.work_args("B"))["task_id"]
        self.assertTrue(wait_until(lambda: self.flow.tasks[task_b].status == "running"))
        # The worker, still in its turn, reports without task_id: B's TASK has not reached it.
        early = self.to_manager({"kind": "done", "message": "finished (old work)"})
        self.assertEqual((early["status"], early["reason"]), ("rejected", "task_not_delivered"), early)
        self.assertEqual(self.flow.tasks[task_b].status, "running")
        self.mailbox.busy[ActorRole.WORKER].clear()
        self.assertTrue(wait_until(lambda: any(m.payload.get("task_id") == task_b
                                               for m in self.mailbox.sent_to(ActorRole.WORKER))))
        time.sleep(0.2)
        sent = [(m.payload.get("task_id"), m.kind) for m in self.mailbox.sent_to(ActorRole.WORKER)]
        self.assertEqual(sent, [(task_b, MessageKind.TASK)], "no cancelled TASK, nothing for A")
        withdrawn = [e for e in self.service.outbox_snapshot() if e["state"] == "withdrawn"]
        self.assertEqual(len(withdrawn), 1)
        done = self.to_manager({"kind": "done", "message": "B done"})
        self.assertEqual((done["status"], done["task_id"]), ("queued", task_b), done)

    def test_cancel_withdraws_queued_follow_ups_before_the_cancel_notice(self):
        task_a = self.running("A")
        self.mailbox.busy[ActorRole.WORKER].set()
        self.assertEqual(self.to_worker({"kind": "work", "message": "also X", "task_id": task_a})["status"], "queued")
        time.sleep(0.1)
        self.to_worker({"kind": "work", "message": "stop", "task_id": task_a, "cancel": True})
        self.mailbox.busy[ActorRole.WORKER].clear()
        self.assertTrue(wait_until(lambda: len(self.mailbox.sent_to(ActorRole.WORKER)) == 2))
        time.sleep(0.2)
        sent = [m.payload for m in self.mailbox.sent_to(ActorRole.WORKER)]
        self.assertEqual([p.get("cancel", False) for p in sent], [False, True], sent)
        self.assertNotIn("also X", [p.get("message") for p in sent])


# -- R3 ------------------------------------------------------------------------------------------
class PauseKeepsReportTests(CorrectionFixture):
    def test_a_deferred_done_report_is_delivered_after_resume(self):
        task_a = self.running("A")
        self.mailbox.busy[ActorRole.MANAGER].set()  # the manager is in a turn: the report waits
        self.assertEqual(self.to_manager({"kind": "done", "message": "A done"})["status"], "queued")
        time.sleep(0.1)
        self.controller.pause()
        time.sleep(0.3)
        self.controller.resume(True)
        self.mailbox.busy[ActorRole.MANAGER].clear()
        self.assertTrue(wait_until(lambda: self.flow.tasks[task_a].status == "closed"), self.flow.tasks[task_a])
        self.assertEqual(self.flow.tasks[task_a].closed_reason, "done")
        reports = [m for m in self.mailbox.sent_to(ActorRole.MANAGER) if m.payload.get("message") == "A done"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(self.to_worker(self.work_args("B"))["status"], "dispatched")

    def test_a_backend_message_kept_across_pause_is_sent_once_after_resume(self):
        paused = [False]
        service = HandoffService(self.root / "workflow" / "keep.jsonl", mailbox=FakeMailbox(),
                                 paused=lambda: paused[0], retry_interval=0.01)
        self.services.append(service)
        service.start()
        with TaskRepository(self.db) as repository:
            task = repository.create_task({"goal": "g"})
        outbound = OutboundMessage(task, 1, str(uuid4()), ActorRole.MANAGER, ActorRole.WORKER, MessageKind.TASK,
                                   {"handoff": "to_worker"})
        paused[0] = True
        self.assertEqual(service.enqueue(outbound, origin="t", keep_across_pause=True)["status"], "queued")
        dropped = service.enqueue(outbound, origin="t")
        self.assertEqual(dropped["status"], "queued")
        self.assertTrue(wait_until(lambda: any(e["state"] == "held_paused" for e in service.outbox_snapshot())))
        time.sleep(0.2)
        self.assertEqual(service._mailbox.created, [])
        paused[0] = False
        self.assertTrue(wait_until(lambda: len(service._mailbox.delivered) == 1))
        time.sleep(0.2)
        self.assertEqual(len(service._mailbox.delivered), 1, "the dropped one is not sent after resume")


# -- R1 with the real TaskMailbox --------------------------------------------------------------
class TurnBridge:
    """A bridge for real TaskMailboxes: ``api_accepted`` at once, the turn's end event when released."""

    def __init__(self):
        self.peers = {ActorRole.MANAGER: BridgePeer(ActorRole.MANAGER, str(uuid4()), 1, 101),
                      ActorRole.WORKER: BridgePeer(ActorRole.WORKER, str(uuid4()), 1, 102)}
        self.cv = threading.Condition()
        self.events = []
        self.turn_end = {ActorRole.MANAGER: threading.Event(), ActorRole.WORKER: threading.Event()}
        for event in self.turn_end.values():
            event.set()
        self.in_turn = threading.Event()

    def peer(self, role, timeout=0):
        return self.peers[ActorRole(role)]

    def probe(self, role, timeout=5):
        peer = self.peers[ActorRole(role)]
        return {"role": peer.role.value, "sessionId": peer.session_id, "generation": 1, "idle": True,
                "pending": False, "approvalPending": False, "editorKnown": True, "editorEmpty": True,
                "inFlightToolCount": 0, "paused": False}

    def event_cursor(self):
        with self.cv:
            return len(self.events)

    def request(self, role, frame, timeout=5):
        role = ActorRole(role)
        envelope = ControlEnvelope.from_json(frame["envelope"])
        peer = self.peers[role]

        def finish():
            self.turn_end[role].wait(30)
            with self.cv:
                self.events.append({"role": role.value, "name": "delivery_omp_processed",
                                    "messageId": envelope.message_id,
                                    "deliveryAttemptId": envelope.delivery_attempt_id,
                                    "taskId": envelope.task_id, "revisionId": envelope.revision_id,
                                    "runId": envelope.run_id, "sessionId": peer.session_id, "generation": 1,
                                    "providerRequestMatched": True, "providerResponseObserved": True,
                                    "agentEndObserved": True})
                self.cv.notify_all()

        if role is ActorRole.MANAGER:
            self.in_turn.set()
        threading.Thread(target=finish, daemon=True).start()
        return {"status": "api_accepted", "requestId": frame.get("requestId", "r")}

    def wait_any_event(self, role, names, expected, *, after_sequence=0, timeout=15):
        deadline = time.monotonic() + timeout
        with self.cv:
            while True:
                for event in self.events[after_sequence:]:
                    if event["role"] == ActorRole(role).value and event["name"] in names and all(
                            event.get(key) == value for key, value in expected.items()):
                        return dict(event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeTimeout("no processing event")
                self.cv.wait(remaining)


class RealMailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cw18-r1-real-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "workflow").mkdir(mode=0o700)
        self.db = self.root / "tasks.sqlite3"
        with TaskRepository(self.db):
            pass
        self.bridge = TurnBridge()
        self.addCleanup(lambda: [event.set() for event in self.bridge.turn_end.values()])

        def factory():
            repository = TaskRepository(self.db)
            return TaskMailbox(repository, self.bridge), repository.close

        self.service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox_factory=factory,
                                      peer_lookup=lambda role: self.bridge.peers[role], deliver_timeout=1.0,
                                      retry_interval=0.01)
        self.flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl",
                             repository_factory=lambda: TaskRepository(self.db), handoffs=self.service,
                             omp_idle=lambda role: True, poll_interval=0.02)
        self.service.configure(policy=self.flow, active_task=self.flow.active_task)
        self.service.start()
        self.flow.start()
        self.addCleanup(self.service.close)
        self.addCleanup(self.flow.close)
        self.calls = 0

    def handle(self, tool, args):
        self.calls += 1
        role = ActorRole.MANAGER if tool == "to_worker" else ActorRole.WORKER
        args = dict(args)
        payload = request(tool, args, f"c-{self.calls}")
        payload["session_id"] = self.bridge.peers[role].session_id
        return self.service.handle(role, payload)

    def test_tasks_mailbox_api_ack_frees_the_worker_inside_the_report_turn(self):
        first = self.handle("to_worker", {"kind": "work", "message": "do A",
                                          "spec": {"goal": "A", "paths": ["src/"]}})
        self.assertEqual(first["status"], "dispatched", first)
        task_a = first["task_id"]
        self.assertTrue(wait_until(lambda: self.flow.tasks[task_a].status == "running"))
        self.bridge.turn_end[ActorRole.MANAGER].clear()
        self.assertEqual(self.handle("to_manager", {"kind": "done", "message": "A done"})["status"], "queued")
        self.assertTrue(self.bridge.in_turn.wait(5))
        nxt = self.handle("to_worker", {"kind": "work", "message": "do B", "spec": {"goal": "B", "paths": ["src/"]}})
        self.assertEqual(nxt["status"], "dispatched", nxt)
        self.assertEqual(self.flow.tasks[task_a].closed_reason, "done")
        # The manager's turn outlasts the 1 s receipt window: the receipt is unknown, the Task stays done.
        self.assertTrue(wait_until(lambda: any(e["state"] == "unknown" for e in self.service.outbox_snapshot()), 5))
        self.assertEqual(self.flow.worker_view(), {"state": "busy", "task_id": nxt["task_id"]})
        with TaskRepository(self.db) as repository:
            self.assertIsNone(repository.get_current_run(task_a))


# -- R5 ------------------------------------------------------------------------------------------
class HoldStateMailbox(PublicMailboxFixture):
    """Records the host pane's hold while the TASK is delivered; ``on_task`` runs then (the user's typing)."""

    def __init__(self, repository, pane, seen, on_task=None):
        super().__init__(repository)
        self.pane, self.seen, self.on_task = pane, seen, on_task

    def deliver(self, message, **_):
        if message.kind is MessageKind.TASK:
            self.seen.append(self.pane.automation_hold)
            if self.on_task is not None:
                self.on_task()
        return SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)


class HostInputHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cw18-r5-host-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
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
        self.loop = threading.Thread(target=self._loop, daemon=True)
        self.loop.start()
        self.addCleanup(self._teardown)
        self.seen: list = []
        self.on_task = None

    def _loop(self):
        while not self.stop.is_set():
            for chunk in self.pane.pump():
                self.ui.extend(chunk.data)
            time.sleep(0.01)

    def _teardown(self):
        self.stop.set()
        self.loop.join(5)
        self.pane.close()

    def run_flow(self):
        with TaskRepository(self.db):
            pass
        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=FakeMailbox())
        ports = ExperimentPorts(
            host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
            make_workflow=lambda repository: TaskWorkflow(
                repository, HoldStateMailbox(repository, self.pane, self.seen, self.on_task)),
            automation=lambda: AUTOMATION, environment_names=lambda: {"PATH"},
            worktrees_root=self.root / "worktrees", artifacts_root=self.root / "runs")
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(self.db),
                        handoffs=service, omp_idle=lambda role: True, experiment=ports, poll_interval=0.05,
                        collect_slice=0.2)
        service.configure(policy=flow)
        service.start()
        flow.start()
        self.addCleanup(service.close)
        self.addCleanup(flow.close)
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt"))
        spec = {"goal": "fixture run", "paths": ["outcome.txt"], "execution": {
            "source": str(self.source), "commit": self.commit,
            "command": "printf 'PASS from host\\n'; printf PASS > outcome.txt",
            "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
            "environment": ["PATH"], "shell": "bash"}}
        first = service.handle(ActorRole.MANAGER, request("to_worker", {
            "kind": "experiment", "message": "run the fixture", "spec": spec}, "m-1"))
        self.assertEqual(first["status"], "dispatched", first)
        return flow, first["task_id"]

    def test_user_input_is_not_held_while_the_task_is_delivered(self):
        flow, task_id = self.run_flow()
        self.assertTrue(wait_until(lambda: flow.task_view()["status"] == "finished", 30), flow.task_view())
        self.assertEqual(self.seen, [None], "no hold while the TASK is delivered / the worker decides")
        self.assertEqual(flow.task_view()["last_result"]["judgment"], "success")
        self.assertTrue(wait_until(lambda: self.pane.automation_hold is None))

    def test_typing_during_the_task_delivery_means_nothing_is_typed(self):
        self.on_task = lambda: self.pane.admit(b"echo user-line")  # the user starts a line meanwhile
        flow, task_id = self.run_flow()
        self.assertTrue(wait_until(lambda: flow.task_view()["status"] == "finished", 30), flow.task_view())
        task = flow.task_view()
        self.assertEqual(task["held_reason"], "start_failed:host_terminal_busy", task)
        self.assertEqual(self.seen, [None])
        time.sleep(0.3)
        self.assertNotIn(b"cd ", bytes(self.ui), "Workbench typed nothing into the user's line")
        self.assertIsNone(self.pane.automation_hold)
        self.assertIsNone(self.pane.admit(b"\r"))
        self.assertTrue(wait_until(lambda: bytes(self.ui).count(b"user-line") >= 2, 5))


# -- R6 ------------------------------------------------------------------------------------------
try:
    from test_automation_loop import AutomationFixture
except Exception:  # pragma: no cover - the fixture needs the bridge stack
    AutomationFixture = None


if AutomationFixture is not None:
    class CancelBeforeBindTests(AutomationFixture):
        def test_a_run_ended_before_its_bind_leaves_automation_idle(self):
            from workbench.backend.automation import AutomationController  # noqa: F401 (fixture import)
            from workbench.contracts.v1 import MessageKind as Kind
            from test_automation_loop import PersistingMailbox

            task = self.approve({"goal": "clean parser", "paths": ["src/"]}, ["src/"])
            run_id = self.repository.start_run(task, 1, inputs={"kind": "work"})
            message = PersistingMailbox(self.repository, self.bridge).create_message(
                task, 1, run_id, ActorRole.MANAGER, ActorRole.WORKER, Kind.TASK, {"handoff": "to_worker"})
            self.repository.cancel_run(run_id, "cancelled_by_manager")
            self.controller.run_ended(run_id)  # the cancel's run_ended runs before work_started's bind
            self.controller.work_started(task, 1, run_id, message.message_id)
            status = self.status()
            self.assertEqual(status["state"], "idle", (status, self.logs))
            self.assertFalse((status.get("run") or {}).get("bound"), status)


if __name__ == "__main__":
    unittest.main()
