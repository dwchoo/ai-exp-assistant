"""CW-18 U1: HandoffService (backend side of to_worker / to_manager). No OMP, no provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import UUID, uuid4

from workbench.backend import flow
from workbench.backend.flow import ActiveTask, HandoffService, PlaceholderPolicy
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected, DeliveryReceipt, MailboxStatus

MANAGER_SESSION = str(uuid4())
WORKER_SESSION = str(uuid4())
SECRET = "s3cr3t-value-for-test-0123456789"


class FakeMessage:
    def __init__(self, **fields):
        self.__dict__.update(fields)
        self.message_id = str(uuid4())


class FakeMailbox:
    def __init__(self):
        self.created: list[FakeMessage] = []
        self.delivered: list[str] = []
        self.statuses: list[MailboxStatus | Exception] = []
        self.create_errors: list[Exception] = []
        self.sessions = {ActorRole.MANAGER: (MANAGER_SESSION, 1), ActorRole.WORKER: (WORKER_SESSION, 1)}
        self.event = threading.Event()

    def create_message(self, task_id, revision, run_id, sender_role, target_role, kind, payload, *,
                       in_reply_to_message_id=None):
        if self.create_errors:
            raise self.create_errors.pop(0)
        session_id, generation = self.sessions[ActorRole(target_role)]
        message = FakeMessage(session_id=session_id, session_generation=generation, task_id=task_id, revision=revision, run_id=run_id, sender_role=ActorRole(sender_role),
                              target_role=ActorRole(target_role), kind=MessageKind(kind), payload=dict(payload),
                              in_reply_to_message_id=in_reply_to_message_id)
        self.created.append(message)
        return message

    def deliver(self, message, *, timeout=20):
        self.delivered.append(message.message_id)
        status = self.statuses.pop(0) if self.statuses else MailboxStatus.API_RETURNED
        self.event.set()
        if isinstance(status, Exception):
            raise status
        return DeliveryReceipt(message.message_id, None, message.target_role, "s", 1, status, {"reason": "fake"})


def request(tool, args, *, call="call-1", session=None, generation=1, request_id=None):
    session = session or (MANAGER_SESSION if tool == "to_worker" else WORKER_SESSION)
    return {"request_id": request_id or str(uuid4()), "tool_call_id": call, "tool": tool, "args": args,
            "session_id": session, "generation": generation}


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class HandoffServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cw18-u1-flow-")
        self.journal = Path(self.tmp.name) / "workflow" / "handoffs.jsonl"
        self.journal.parent.mkdir(mode=0o700)
        self.mailbox = FakeMailbox()
        self.active: ActiveTask | None = None
        self.paused = False
        self.services: list[HandoffService] = []

    def tearDown(self):
        for service in self.services:
            service.close()
        self.tmp.cleanup()

    def service(self, **kwargs) -> HandoffService:
        kwargs.setdefault("mailbox", self.mailbox)
        kwargs.setdefault("active_task", lambda: self.active)
        kwargs.setdefault("paused", lambda: self.paused)
        kwargs.setdefault("sensitive_values", lambda: (SECRET,))
        kwargs.setdefault("retry_interval", 0.01)
        service = HandoffService(self.journal, **kwargs)
        service.start()
        self.services.append(service)
        return service

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.journal.read_text().splitlines()]

    def active_task(self, **overrides) -> ActiveTask:
        fields = dict(task_id=str(uuid4()), revision=1, kind="work", approved=True, run_id=str(uuid4()),
                      task_message_id=str(uuid4()))
        fields.update(overrides)
        return ActiveTask(**fields)

    # -- authentication and role -------------------------------------------------
    def test_role_is_the_callers_and_tool_role_mismatch_or_unknown_tool_is_rejected(self):
        service = self.service()
        self.assertEqual(service.handle(ActorRole.WORKER, request("to_worker", {"kind": "work", "message": "x"},
                                                                  session=WORKER_SESSION)),
                         {"status": "rejected", "reason": "tool_not_allowed_for_role"})
        self.assertEqual(service.handle(ActorRole.MANAGER, request("to_manager", {"kind": "done", "message": "x"},
                                                                   session=MANAGER_SESSION, call="c2")),
                         {"status": "rejected", "reason": "tool_not_allowed_for_role"})
        self.assertEqual(service.handle("manager", request("rm_rf", {}, call="c3"))["reason"], "unknown_tool")
        self.assertEqual(self.mailbox.created, [])
        roles = {(r["type"], r.get("key", {}).get("role")) for r in self.records()}
        self.assertIn(("request", "worker"), roles)
        self.assertIn(("result", "manager"), roles)

    def test_invalid_role_or_identity_is_rejected(self):
        service = self.service()
        self.assertEqual(service.handle("system", request("to_worker", {"kind": "work", "message": "x"}))["reason"],
                         "invalid_request")
        bad = request("to_worker", {"kind": "work", "message": "x"})
        bad["session_id"] = "not-a-uuid"
        self.assertEqual(service.handle(ActorRole.MANAGER, bad)["reason"], "invalid_request")
        bad = request("to_worker", {"kind": "work", "message": "x"}, call="")
        self.assertEqual(service.handle(ActorRole.MANAGER, bad)["reason"], "invalid_request")

    # -- placeholder policy (U1) ----------------------------------------------------
    def test_to_worker_without_active_task_is_approval_pending_with_a_stub_record(self):
        service = self.service()
        spec = {"goal": "speed up the parser", "paths": ["src/parser"]}
        result = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "do it",
                                                                         "spec": spec}))
        self.assertEqual(result["status"], "approval_pending")
        UUID(result["approval_id"])
        self.assertIsNone(result["task_id"])
        approvals = service.pending_approvals()
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["approval_id"], result["approval_id"])
        self.assertEqual(approvals[0]["state"], "pending")
        self.assertIs(approvals[0]["stub"], True)
        self.assertEqual(approvals[0]["spec"], spec)
        self.assertEqual(approvals[0]["request_key"],
                         {"role": "manager", "session_id": MANAGER_SESSION, "generation": 1, "tool_call_id": "call-1"})
        self.assertEqual(self.mailbox.created, [])
        self.assertIn("approval", [r["type"] for r in self.records()])

    def test_to_worker_with_unknown_task_or_unapproved_task(self):
        service = self.service()
        self.assertEqual(service.handle(ActorRole.MANAGER, request(
            "to_worker", {"kind": "work", "message": "x", "task_id": str(uuid4())})),
            {"status": "rejected", "reason": "unknown_task"})
        self.active = self.active_task(approved=False, run_id=None)
        result = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}, call="c2"))
        self.assertEqual(result["status"], "approval_pending")
        self.assertEqual(result["task_id"], self.active.task_id)
        other = service.handle(ActorRole.MANAGER, request(
            "to_worker", {"kind": "work", "message": "x", "spec": {"goal": "new", "paths": []}}, call="c3"))
        self.assertEqual(other["status"], "approval_pending")
        self.active = self.active_task()
        self.assertEqual(service.handle(ActorRole.MANAGER, request(
            "to_worker", {"kind": "work", "message": "x", "spec": {"goal": "new", "paths": []}}, call="c4")),
            {"status": "held", "reason": "another_task_active"})
        self.assertEqual(self.mailbox.created, [])

    def test_to_manager_without_active_task_is_rejected(self):
        service = self.service()
        self.assertEqual(service.handle(ActorRole.WORKER, request("to_manager", {"kind": "done", "message": "ok"})),
                         {"status": "rejected", "reason": "no_active_task"})
        self.active = self.active_task(run_id=None)
        self.assertEqual(service.handle(ActorRole.WORKER, request("to_manager", {"kind": "done", "message": "ok"},
                                                                  call="c2")),
                         {"status": "held", "reason": "no_active_run"})

    def test_free_text_inside_an_active_task_is_queued_and_delivered_through_the_mailbox(self):
        service = self.service()
        self.active = self.active_task()
        result = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "check logs"}))
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["task_id"], self.active.task_id)
        UUID(result["handoff_id"])
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 1))
        created = self.mailbox.created[-1]
        self.assertEqual(service.outbox_snapshot()[0]["message_id"], created.message_id)
        self.assertEqual((created.sender_role, created.target_role, created.kind),
                         (ActorRole.MANAGER, ActorRole.WORKER, MessageKind.QUESTION))
        self.assertEqual((created.task_id, created.revision, created.run_id),
                         (self.active.task_id, 1, self.active.run_id))
        self.assertEqual(created.payload, {"handoff": "to_worker", "kind": "work", "message": "check logs"})
        self.assertNotIn("stage", created.payload, "a handoff must never look like a staged worker delivery")
        self.assertTrue(wait_until(lambda: self.mailbox.delivered == [created.message_id]))

        report = service.handle(ActorRole.WORKER, request(
            "to_manager", {"kind": "blocked", "message": "missing data", "requires_code_change": True,
                           "reason": "schema changed"}))
        self.assertEqual(report["status"], "queued")
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 2))
        created = self.mailbox.created[-1]
        self.assertEqual((created.sender_role, created.target_role, created.kind, created.in_reply_to_message_id),
                         (ActorRole.WORKER, ActorRole.MANAGER, MessageKind.REPORT, self.active.task_message_id))
        self.assertEqual(created.payload, {"handoff": "to_manager", "kind": "blocked", "message": "missing data",
                                           "requires_code_change": True, "reason": "schema changed"})
        question = str(uuid4())
        service.handle(ActorRole.WORKER, request("to_manager", {"kind": "answer", "message": "yes",
                                                                "in_reply_to": question}, call="c3"))
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 3))
        self.assertEqual((self.mailbox.created[-1].kind, self.mailbox.created[-1].in_reply_to_message_id),
                         (MessageKind.ANSWER, question))
        self.assertEqual(service.handle(ActorRole.WORKER, request("to_manager", {"kind": "answer", "message": "y"},
                                                                  call="c4")),
                         {"status": "rejected", "reason": "in_reply_to_required"})

    def test_spec_or_run_inside_the_active_task_waits_for_approval_in_u1(self):
        service = self.service()
        self.active = self.active_task(kind="experiment")
        result = service.handle(ActorRole.MANAGER, request(
            "to_worker", {"kind": "experiment", "message": "rerun", "task_id": self.active.task_id, "run": True}))
        self.assertEqual(result["status"], "approval_pending")
        self.assertEqual(self.mailbox.created, [])

    def test_target_not_connected_is_held_and_nothing_is_queued(self):
        peers = {}
        service = self.service(peer_lookup=lambda role: peers.get(role))
        self.active = self.active_task()
        result = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
        self.assertEqual((result["status"], result["reason"]), ("held", "target_not_connected"))
        self.assertNotIn("handoff_id", result)
        self.assertEqual(service.outbox_snapshot(), [])

    def test_handle_never_waits_on_the_mailbox(self):
        gate = threading.Event()
        original = self.mailbox.deliver

        def slow_deliver(message, *, timeout=20):
            if message.target_role is ActorRole.WORKER:
                gate.wait(5)  # TaskMailbox.deliver holds its lock until the target's turn ends
            return original(message, timeout=timeout)

        self.mailbox.deliver = slow_deliver
        self.mailbox.create_message = mock.Mock(wraps=self.mailbox.create_message)
        service = self.service()
        self.active = self.active_task()
        service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
        self.assertTrue(wait_until(lambda: self.mailbox.create_message.call_count == 1))
        started = time.monotonic()
        result = service.handle(ActorRole.WORKER, request("to_manager", {"kind": "progress", "message": "y"}))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result["status"], "queued")
        # U2: one lane per target; the manager's report does not wait behind the worker's turn.
        self.assertTrue(wait_until(lambda: [e["state"] for e in service.outbox_snapshot()] == ["delivering",
                                                                                              "delivered"]))
        self.assertEqual(self.mailbox.create_message.call_count, 2)
        gate.set()
        self.assertTrue(wait_until(lambda: [e["state"] for e in service.outbox_snapshot()] == ["delivered"] * 2))

    def test_a_new_target_session_does_not_receive_a_queued_handoff(self):
        worker = mock.Mock(session_id=WORKER_SESSION, generation=1)
        service = self.service(peer_lookup=lambda role: worker)
        self.active = self.active_task()
        self.paused = False
        self.mailbox.sessions[ActorRole.WORKER] = (str(uuid4()), 1)
        service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
        self.assertTrue(wait_until(lambda: service.outbox_snapshot()[0]["state"] == "rejected"))
        self.assertEqual(service.outbox_snapshot()[0]["reason"], "target_session_changed")
        self.assertEqual(self.mailbox.delivered, [])

    # -- pause hook ---------------------------------------------------------------
    def test_paused_holds_every_request_without_queue_or_approval(self):
        service = self.service()
        self.paused = True
        self.active = self.active_task()
        self.assertEqual(service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"})),
                         {"status": "held", "reason": "paused"})
        self.assertEqual(service.handle(ActorRole.WORKER, request("to_manager", {"kind": "done", "message": "x"})),
                         {"status": "held", "reason": "paused"})
        self.assertEqual((self.mailbox.created, service.pending_approvals()), ([], []))

    # -- idempotence ----------------------------------------------------------------
    def test_same_key_returns_the_first_result_and_is_processed_once(self):
        service = self.service()
        self.active = self.active_task()
        first = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "one"}))
        again = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "changed"}))
        self.assertEqual(again, first)
        self.assertEqual(len(service.outbox_snapshot()), 1)
        # Another session, generation or tool call is a different key.
        service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}, session=str(uuid4())))
        service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}, generation=2))
        service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}, call="call-2"))
        self.assertEqual(len(service.outbox_snapshot()), 4)
        self.assertEqual([r["type"] for r in self.records()].count("duplicate"), 1)
        # The same toolCallId from the other role is its own key, still checked by role.
        self.assertEqual(service.handle(ActorRole.WORKER, request("to_manager", {"kind": "done", "message": "x"}))
                         ["status"], "queued")

    def test_idempotence_survives_a_new_service_on_the_same_journal(self):
        first_service = self.service()
        first = first_service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
        first_service.close()
        second = self.service()
        self.active = self.active_task()
        self.assertEqual(second.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "y"})),
                         first)
        self.assertEqual(self.mailbox.created, [])

    def test_concurrent_duplicates_are_processed_once(self):
        service = self.service()
        self.active = self.active_task()
        results: list[dict] = []
        threads = [threading.Thread(target=lambda: results.append(service.handle(
            ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"})))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({json.dumps(r, sort_keys=True) for r in results}), 1)
        self.assertEqual(len(service.outbox_snapshot()), 1)
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 1))
        time.sleep(0.05)
        self.assertEqual(len(self.mailbox.created), 1)

    # -- journal --------------------------------------------------------------------
    def test_journal_is_private_fsynced_and_records_every_request_and_result(self):
        with mock.patch.object(flow.os, "fsync", wraps=os.fsync) as fsync:
            service = self.service()
            service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "first"}))
            service.handle(ActorRole.WORKER, request("to_manager", {"kind": "done", "message": "x"}))
        mode = stat.S_IMODE(self.journal.stat().st_mode)
        self.assertEqual(mode, 0o600)
        records = self.records()
        self.assertGreaterEqual(fsync.call_count, len(records))
        kinds = [r["type"] for r in records]
        self.assertEqual(kinds.count("request"), 2)
        self.assertEqual(kinds.count("result"), 2)
        request_record = next(r for r in records if r["type"] == "request")
        self.assertEqual(request_record["tool"], "to_worker")
        self.assertEqual(request_record["args"], {"kind": "work", "message": "first"})
        self.assertEqual([r["seq"] for r in records], list(range(1, len(records) + 1)))

    def test_journal_refuses_a_symlink(self):
        target = Path(self.tmp.name) / "elsewhere.jsonl"
        target.write_text("")
        self.journal.symlink_to(target)
        with self.assertRaises(OSError):
            HandoffService(self.journal, mailbox=self.mailbox)

    def test_torn_last_line_is_ignored_on_reload(self):
        service = self.service()
        first = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
        service.close()
        with self.journal.open("a") as handle:
            handle.write('{"type": "result", "key"')
        again = self.service()
        self.assertEqual(again.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"})),
                         first)

    # -- environment values -----------------------------------------------------------
    def test_environment_values_are_rejected_and_never_journaled(self):
        service = self.service()
        self.active = self.active_task()
        cases = [
            ("to_worker", {"kind": "work", "message": f"use {SECRET} to log in"}, "message"),
            ("to_manager", {"kind": "report", "message": "x", "reason": f"token is {SECRET}"}, "reason"),
            ("to_worker", {"kind": "work", "message": "export OPENAI_API_KEY=sk-abcdefgh then run"}, "message"),
            ("to_worker", {"kind": "experiment", "message": "x", "spec": {"goal": "g", "paths": [], "execution": {
                "source": "repo", "commit": "abc", "command": "DATA_ROOT=/mnt/data python train.py",
                "criteria": {"log_contains": "done", "result_file": "r.txt", "result_contains": "ok"},
                "environment": ["DATA_ROOT"], "shell": "bash"}}}, "spec.execution.command"),
        ]
        for index, (tool, args, field) in enumerate(cases):
            with self.subTest(field=field):
                role = ActorRole.MANAGER if tool == "to_worker" else ActorRole.WORKER
                result = service.handle(role, request(tool, args, call=f"env-{index}"))
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["reason"], "environment_value")
                self.assertTrue(any(item.startswith(field) for item in result["fields"]), result)
        text = self.journal.read_text()
        for secret in (SECRET, "sk-abcdefgh", "/mnt/data"):
            self.assertNotIn(secret, text)
        self.assertNotIn(SECRET, json.dumps(service.pending_approvals()))
        self.assertEqual(self.mailbox.created, [])
        # References and names are fine.
        ok = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work",
                                                                     "message": "read $OPENAI_API_KEY"}, call="ok"))
        self.assertEqual(ok["status"], "queued")

    def test_invalid_arguments_are_rejected_without_echoing_values(self):
        service = self.service()
        cases = [
            {"kind": "work"},
            {"kind": "nope", "message": "x"},
            {"kind": "work", "message": ""},
            {"kind": "work", "message": "x" * 8193},
            {"kind": "work", "message": "x", "extra": 1},
            {"kind": "work", "message": "x", "task_id": "123"},
            {"kind": "work", "message": "x", "run": "yes"},
            {"kind": "work", "message": "x", "spec": {"goal": "g"}},
            {"kind": "experiment", "message": "x", "spec": {"goal": "g", "paths": []}},
            {"kind": "experiment", "message": "x", "spec": {"goal": "g", "paths": [], "execution": {
                "source": "r", "commit": "c", "command": "python x.py",
                "criteria": {"log_contains": "a", "result_file": "b", "result_contains": "c"},
                "environment": {"HIDDEN_NAME": "hidden-value-xyz"}, "shell": "bash"}}},
            "not an object",
        ]
        for index, args in enumerate(cases):
            with self.subTest(index=index):
                result = service.handle(ActorRole.MANAGER, request("to_worker", args, call=f"bad-{index}"))
                self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
                self.assertTrue(result["errors"])
        self.assertNotIn("hidden-value-xyz", self.journal.read_text())
        bad = service.handle(ActorRole.WORKER, request("to_manager", {"kind": "done", "message": "x",
                                                                      "request": {"goal": "g"}}, call="w"))
        self.assertEqual(bad["reason"], "invalid_arguments")

    # -- outbox -----------------------------------------------------------------------
    def test_outbox_resends_only_deferred(self):
        service = self.service()
        self.active = self.active_task()
        self.mailbox.statuses = [MailboxStatus.DEFERRED, MailboxStatus.DEFERRED, MailboxStatus.API_RETURNED]
        result = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
        self.assertTrue(wait_until(lambda: service.outbox_snapshot()[0]["state"] == "delivered"))
        message_id = service.outbox_snapshot()[0]["message_id"]
        self.assertEqual(self.mailbox.delivered, [message_id] * 3)
        self.assertEqual(len(self.mailbox.created), 1, "a resend reuses the same message")
        for terminal, state in ((MailboxStatus.UNKNOWN, "unknown"), (MailboxStatus.REJECTED, "rejected"),
                                (RuntimeError("after submit"), "unknown")):
            with self.subTest(state=state):
                self.mailbox.delivered.clear()
                self.mailbox.statuses = [terminal, MailboxStatus.API_RETURNED]
                queued = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"},
                                                                   call=f"t-{state}-{len(service.outbox_snapshot())}"))
                self.assertTrue(wait_until(lambda: service.outbox_snapshot()[-1]["state"] == state))
                time.sleep(0.1)
                self.assertEqual(queued["status"], "queued")
                self.assertEqual(self.mailbox.delivered, [service.outbox_snapshot()[-1]["message_id"]],
                                 "never resent")
        records = [r for r in self.records() if r["type"] == "outbox"]
        self.assertIn("deferred", [r["state"] for r in records])
        self.assertIn("unknown", [r["state"] for r in records])

    def test_disconnected_target_before_submission_is_retried(self):
        service = self.service()
        self.active = self.active_task()
        self.mailbox.create_errors = [BridgeDisconnected("no connected worker OMP session")]
        self.mailbox.statuses = [BridgeDisconnected("no connected worker OMP session"), MailboxStatus.OMP_PROCESSED]
        service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
        self.assertTrue(wait_until(lambda: service.outbox_snapshot()[0]["state"] == "delivered"))
        self.assertEqual(len(self.mailbox.delivered), 2)
        self.assertEqual(len(self.mailbox.created), 1)

    def test_mailbox_factory_runs_on_the_outbox_thread_and_is_released(self):
        threads, released = [], []

        def factory():
            threads.append(threading.current_thread().name)
            return self.mailbox, lambda: released.append(True)

        service = self.service(mailbox=None, mailbox_factory=factory)
        self.active = self.active_task()
        service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
        self.assertTrue(wait_until(lambda: service.outbox_snapshot()[0]["state"] == "delivered"))
        service.close()
        self.assertEqual((threads, released), (["handoff-outbox-worker"], [True]))

    def test_mailbox_factory_failure_rejects_without_sending(self):
        def factory():
            raise RuntimeError("cannot open")

        service = self.service(mailbox=None, mailbox_factory=factory)
        self.active = self.active_task()
        self.assertEqual(service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
                         ["status"], "queued")
        self.assertTrue(wait_until(lambda: service.outbox_snapshot()[0]["state"] == "rejected"))
        self.assertEqual(service.outbox_snapshot()[0]["reason"], "mailbox_unavailable:RuntimeError")

    def test_pause_after_queueing_holds_and_never_sends_after_resume(self):
        service = self.service()
        self.active = self.active_task()
        self.mailbox.statuses = [MailboxStatus.DEFERRED, MailboxStatus.API_RETURNED]
        service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
        self.assertTrue(wait_until(lambda: len(self.mailbox.delivered) == 1))
        self.paused = True
        self.assertTrue(wait_until(lambda: service.outbox_snapshot()[0]["state"] == "held_paused"))
        self.paused = False
        time.sleep(0.1)
        self.assertEqual(len(self.mailbox.delivered), 1)
        self.assertIn("held_paused", [r.get("state") for r in self.records()])


class PlaceholderPolicyTests(unittest.TestCase):
    def test_policy_is_replaceable_through_the_documented_interface(self):
        calls = []

        class Policy:
            def decide(self, req, active):
                calls.append((req.role, req.tool, req.key, active))
                return flow.HandoffDecision({"status": "run_scheduled", "task_id": "t"})

        with tempfile.TemporaryDirectory(prefix="cw18-u1-policy-") as tmp:
            service = HandoffService(Path(tmp) / "handoffs.jsonl", mailbox=FakeMailbox(), policy=Policy())
            try:
                result = service.handle(ActorRole.MANAGER, request("to_worker", {"kind": "work", "message": "x"}))
            finally:
                service.close()
        self.assertEqual(result, {"status": "run_scheduled", "task_id": "t"})
        self.assertEqual(calls, [(ActorRole.MANAGER, "to_worker",
                                  ("manager", MANAGER_SESSION, 1, "call-1"), None)])
        self.assertIsInstance(PlaceholderPolicy(), flow.HandoffPolicy)


if __name__ == "__main__":
    unittest.main()
