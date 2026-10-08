"""p27-polish-01 (1): a handoff the target OMP accepted whose turn outlasts the receipt window is not ``unknown``.

Smokes cd70-01..04 recorded the first TASK (and reports) as ``unknown / BridgeTimeout`` although the target OMP
had it at once: ``TaskMailbox.deliver`` waits for the end of the target's turn only ``deliver_timeout`` (20 s)
and a worker turn with a 120 s terminal wait (or a manager turn that reads a report) is longer. The outbox now
records such a delivery as ``delivered`` (``api_returned``, reason ``receipt_window_ended``: submitted, the
turn's outcome pending) — never resent, never announced as lost. Unknown stays unknown when the submission is
not known (C-D70 ``report_delivery_unknown`` semantics) or the target went away during the turn.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from uuid import uuid4

from workbench.backend.flow import RECEIPT_WINDOW_ENDED, HandoffService, OutboundMessage
from workbench.backend.flow_tasks import TaskFlow
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus, TaskMailbox
from workbench.tasks.repository import TaskRepository

from test_cw18_review_corrections import TurnBridge
from test_task_flow import FakeMailbox, request, wait_until


def journal(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class RealMailboxReceiptWindowTests(unittest.TestCase):
    WINDOW = 0.3

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p27-polish-recv-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "workflow").mkdir(mode=0o700)
        self.db = self.root / "tasks.sqlite3"
        with TaskRepository(self.db):
            pass
        self.bridge = TurnBridge()
        self.addCleanup(lambda: [event.set() for event in self.bridge.turn_end.values()])
        self.requests: list = []
        original = self.bridge.request

        def counted(role, frame, timeout=5):
            self.requests.append((ActorRole(role), frame))
            return original(role, frame, timeout)
        self.bridge.request = counted

        def factory():
            repository = TaskRepository(self.db)
            return TaskMailbox(repository, self.bridge), repository.close

        self.journal_path = self.root / "workflow" / "handoffs.jsonl"
        self.service = HandoffService(self.journal_path, mailbox_factory=factory,
                                      peer_lookup=lambda role: self.bridge.peers[role],
                                      deliver_timeout=self.WINDOW, retry_interval=0.01)
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
        payload = request(tool, dict(args), f"c-{self.calls}")
        payload["session_id"] = self.bridge.peers[role].session_id
        return self.service.handle(role, payload)

    def entry_to(self, role):
        entries = [e for e in self.service.outbox_snapshot() if e["target_role"] == role.value]
        return entries[-1] if entries else None

    def ended(self, role):
        entry = self.entry_to(role)
        return entry is not None and entry["state"] not in ("pending", "delivering")

    def outbox_states(self, handoff_id):
        return [r["state"] for r in journal(self.journal_path)
                if r.get("type") == "outbox" and r.get("handoff_id") == handoff_id]

    def test_a_first_task_whose_worker_turn_outlasts_the_window_is_submitted_not_unknown(self):
        self.bridge.turn_end[ActorRole.WORKER].clear()  # the worker's first turn runs a long terminal wait
        first = self.handle("to_worker", {"kind": "work", "message": "do A", "spec": {"goal": "A", "paths": ["src/"]}})
        self.assertEqual(first["status"], "dispatched", first)
        self.assertTrue(wait_until(lambda: self.ended(ActorRole.WORKER), 5))
        entry = self.entry_to(ActorRole.WORKER)
        self.assertEqual((entry["state"], entry["status"], entry["reason"]),
                         ("delivered", "api_returned", RECEIPT_WINDOW_ENDED), entry)
        states = self.outbox_states(entry["handoff_id"])
        self.assertIn("submitted", states)
        self.assertNotIn("unknown", states, "the journal does not call an accepted TASK unknown")
        self.assertEqual(self.flow.tasks[first["task_id"]].status, "running")
        self.assertIsNone(self.flow.tasks[first["task_id"]].held_reason)
        self.bridge.turn_end[ActorRole.WORKER].set()
        time.sleep(self.WINDOW + 0.2)
        self.assertEqual(len([r for r, _ in self.requests if r is ActorRole.WORKER]), 1, "never resent")

    def test_a_report_whose_manager_turn_outlasts_the_window_is_submitted_not_unknown(self):
        first = self.handle("to_worker", {"kind": "work", "message": "do A", "spec": {"goal": "A", "paths": ["src/"]}})
        task_id = first["task_id"]
        self.assertTrue(wait_until(lambda: self.flow.tasks[task_id].status == "running"))
        self.assertTrue(wait_until(lambda: self.ended(ActorRole.WORKER), 5))
        self.bridge.turn_end[ActorRole.MANAGER].clear()
        self.assertEqual(self.handle("to_manager", {"kind": "done", "message": "A done"})["status"], "queued")
        self.assertTrue(wait_until(lambda: self.ended(ActorRole.MANAGER), 5))
        entry = self.entry_to(ActorRole.MANAGER)
        self.assertEqual((entry["state"], entry["reason"]), ("delivered", RECEIPT_WINDOW_ENDED), entry)
        self.assertNotIn("unknown", self.outbox_states(entry["handoff_id"]))
        task = self.flow.tasks[task_id]
        self.assertEqual((task.status, task.closed_reason), ("closed", "done"))
        reports = [e for e in self.service.report_entries() if e["state"] == "unknown"]
        self.assertEqual(reports, [], "nothing for a report_delivery_unknown notice")
        self.assertEqual(len([r for r, _ in self.requests if r is ActorRole.MANAGER]), 1)


class ScriptedMailbox(FakeMailbox):
    """``deliver`` acknowledges the submission (or not) and returns the scripted receipt."""

    def __init__(self, submit: bool, status: MailboxStatus, details: dict):
        super().__init__()
        self.submit, self.status, self.details = submit, status, details

    def deliver(self, message, *, timeout=20, on_submitted=None):
        self.delivered.append(message.message_id)
        if self.submit and on_submitted is not None:
            on_submitted()
        return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1,
                               self.status, dict(self.details))


class OutcomeMappingTests(unittest.TestCase):
    def run_one(self, mailbox):
        tmp = tempfile.TemporaryDirectory(prefix="p27-polish-map-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        service = HandoffService(Path(tmp.name) / "handoffs.jsonl", mailbox=mailbox, retry_interval=0.01)
        self.addCleanup(service.close)
        service.start()
        events: list[str] = []
        outbound = OutboundMessage(str(uuid4()), 1, str(uuid4()), ActorRole.WORKER, ActorRole.MANAGER,
                                   MessageKind.REPORT, {"kind": "done", "message": "x"})
        service.enqueue(outbound, origin="worker", listener=lambda event, _snapshot: events.append(event))
        self.assertTrue(wait_until(lambda: service.outbox_snapshot()[0]["state"] not in ("pending", "delivering")))
        time.sleep(0.05)
        return service.outbox_snapshot()[0], events

    def test_submitted_then_the_window_ended_is_delivered_with_the_outcome_pending(self):
        entry, events = self.run_one(ScriptedMailbox(True, MailboxStatus.UNKNOWN, {
            "reason": "BridgeTimeout", "stage": "omp_processing_observation"}))
        self.assertEqual((entry["state"], entry["status"], entry["reason"]),
                         ("delivered", "api_returned", RECEIPT_WINDOW_ENDED))
        self.assertEqual(events, ["delivering", "created", "submitted", "delivered"])

    def test_submitted_then_the_target_went_away_stays_unknown(self):
        entry, _ = self.run_one(ScriptedMailbox(True, MailboxStatus.UNKNOWN, {
            "reason": "BridgeDisconnected", "stage": "omp_processing_observation"}))
        self.assertEqual((entry["state"], entry["reason"]), ("unknown", "BridgeDisconnected"))

    def test_an_aborted_turn_stays_unknown(self):
        entry, _ = self.run_one(ScriptedMailbox(True, MailboxStatus.UNKNOWN, {
            "reason": "assistant_message_ended_with_error_or_abort"}))
        self.assertEqual(entry["state"], "unknown")

    def test_a_timeout_without_a_known_submission_stays_unknown(self):
        entry, events = self.run_one(ScriptedMailbox(False, MailboxStatus.UNKNOWN, {
            "reason": "BridgeTimeout", "stage": "api_return"}))
        self.assertEqual((entry["state"], entry["reason"]), ("unknown", "BridgeTimeout"))
        self.assertNotIn("submitted", events)


if __name__ == "__main__":
    unittest.main()
