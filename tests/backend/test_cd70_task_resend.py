"""C-D70 (4): a follow-up to a new worker session carries the full Task, the commands already run and the follow-up.

The real TaskFlow inside the real HandoffService with a fake mailbox whose worker session can change (no OMP).
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from workbench.backend.flow_tasks import COMMANDS_RULE, RESEND_NOTE
from workbench.backend.flow_terminal import CommandRuns
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus

import test_task_flow as fx

COMMANDS = ["lscpu", "free -h", "df -h"]
RUN = {"command": "lscpu", "status": "exited", "exit_code": 0, "signal": None, "duration_seconds": 0.2,
       "log_path": "/tmp/x/terminal/c1.log"}


def rs_wait(predicate, timeout=5.0):
    return fx.wait_until(predicate, timeout)


class SessionMailbox(fx.FakeMailbox):
    """Messages to the worker are bound to its current session (as TaskMailbox.create_message does)."""

    def __init__(self, test):
        super().__init__()
        self.test = test

    def create_message(self, *args, **kwargs):
        message = super().create_message(*args, **kwargs)
        if message.target_role is ActorRole.WORKER:
            message.session_id, message.session_generation = self.test.worker_session or (fx.WORKER_SESSION, 1)
        return message


class ResendFixture(fx.FlowFixture):
    def setUp(self):
        self.worker_session = (fx.WORKER_SESSION, 1)
        self.runs_listed: list = [dict(RUN)]
        super().setUp()

    def open(self, *, start=True):
        self.mailbox = SessionMailbox(self) if not isinstance(self.mailbox, SessionMailbox) else self.mailbox
        flow = super().open(start=start)
        flow._worker_session = lambda: self.worker_session
        flow.terminal_runs = lambda task_id: CommandRuns([dict(item) for item in self.runs_listed],
                                                         ["Runs from before a backend restart were rebuilt."])
        return flow

    def work(self, **extra):
        result = self.to_worker({"kind": "work", "message": "collect the hardware facts, stop on errors",
                                 "spec": {"goal": "hardware facts", "paths": ["out/"], "instructions": "be brief"},
                                 "commands": list(COMMANDS), "analysis": "detailed", **extra})
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["status"] == "running"
                                      and len(self.mailbox.delivered) == 1))
        self.assertTrue(fx.wait_until(lambda: self.flow.watch_view()["worker_session"] is not None))
        return result["task_id"]

    def to_workers(self):
        return self.mailbox.to(ActorRole.WORKER)

    def follow_up(self, task_id, message="continue with the remaining commands", **extra):
        before = len(self.to_workers())
        result = self.to_worker({"kind": "work", "message": message, "task_id": task_id, **extra})
        self.assertEqual(result["status"], "queued", result)
        self.assertTrue(fx.wait_until(lambda: len(self.to_workers()) == before + 1
                                      and len(self.mailbox.delivered) >= before + 1))
        return result, self.to_workers()[-1]


class ResendTests(ResendFixture):
    def test_the_task_remembers_the_worker_session_that_got_it(self):
        task_id = self.work()
        self.assertEqual(self.flow.watch_view()["worker_session"], [fx.WORKER_SESSION, 1])
        status = self.flow.status_view(task_id)
        self.assertEqual(status["worker_session"], [fx.WORKER_SESSION, 1])
        self.assertEqual(status["message"], "collect the hardware facts, stop on errors")
        self.assertEqual((status["commands"], status["analysis"]), (COMMANDS, "detailed"))
        self.assertIn("task_worker_session", [r["type"] for r in self.ledger()])

    def test_same_session_follow_up_is_unchanged(self):
        task_id = self.work()
        result, message = self.follow_up(task_id)
        self.assertNotIn("resent_task", result)
        self.assertNotIn("resent_task", message.payload)
        self.assertEqual(message.payload["message"], "continue with the remaining commands")

    def test_a_new_worker_session_gets_the_full_task_the_commands_run_and_the_follow_up(self):
        task_id = self.work()
        first = self.to_workers()[0].payload
        self.worker_session = (str(uuid4()), 1)  # the worker OMP was restarted
        result, message = self.follow_up(task_id)
        self.assertTrue(result["resent_task"])
        self.assertEqual(message.kind, MessageKind.QUESTION)
        payload = message.payload
        for name in ("task_id", "goal", "paths", "instructions", "message", "analysis", "analysis_rule", "commands",
                     "commands_rule", "revision"):
            self.assertEqual(payload[name], first[name], name)
        self.assertEqual(payload["commands_rule"], COMMANDS_RULE)
        self.assertEqual(payload["commands"][0], {"number": 1, "command": "lscpu"})
        self.assertEqual(payload["commands_already_run"], [RUN])
        self.assertIn("rebuilt", payload["commands_already_run_note"])
        self.assertEqual(payload["follow_up"], "continue with the remaining commands")
        self.assertEqual(payload["resend_note"], RESEND_NOTE)
        self.assertTrue(fx.wait_until(lambda: self.flow.watch_view()["worker_session"] == list(self.worker_session)))
        self.assertIn("task_resent", [r["type"] for r in self.ledger()])
        # the new session has it now: the next follow-up is a plain one
        _, again = self.follow_up(task_id, "and the disk sizes")
        self.assertNotIn("resent_task", again.payload)

    def test_the_task_commands_are_still_enforced_and_a_resend_can_replace_them(self):
        task_id = self.work()
        self.worker_session = (str(uuid4()), 1)
        self.assertEqual(self.flow.active_commands(), COMMANDS)
        _, message = self.follow_up(task_id, "run only this", commands=["uname -a"])
        self.assertEqual(message.payload["commands"], [{"number": 1, "command": "uname -a"}])
        self.assertTrue(fx.wait_until(lambda: self.flow.active_commands() == ["uname -a"]))

    def test_a_task_lost_after_its_creation_is_resent_as_a_question_replying_to_it(self):
        def lost(message, *, timeout=20):
            self.mailbox.delivered.append(message.message_id)
            status = MailboxStatus.REJECTED if message.kind is MessageKind.TASK else MailboxStatus.OMP_PROCESSED
            return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1,
                                   status, {"reason": "target_session_changed"})
        self.mailbox.deliver = lost
        task_id = self.to_worker({"kind": "work", "message": "collect", "spec": {"goal": "g", "paths": []},
                                  "commands": ["lscpu"]})["task_id"]
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["held_reason"] == "instruction_rejected"))
        original = self.flow.tasks[task_id].task_message_id
        self.assertIsNotNone(original)
        self.worker_session = (str(uuid4()), 1)
        self.assertEqual(self.to_manager({"kind": "progress", "message": "hi"})["reason"], "task_not_delivered")
        _, message = self.follow_up(task_id, "please start")
        self.assertEqual(message.kind, MessageKind.QUESTION)
        self.assertTrue(message.payload["resent_task"])
        self.assertEqual(self.flow.tasks[task_id].task_message_id, original, "reports keep replying to the TASK")
        self.assertTrue(fx.wait_until(lambda: self.flow.watch_view()["worker_session"] == list(self.worker_session)))
        self.assertEqual(self.to_manager({"kind": "progress", "message": "started"})["status"], "queued")

    def test_a_task_never_created_is_resent_as_the_task_message(self):
        from workbench.ipc.bridge_g3.mailbox import MailboxError
        create = self.mailbox.create_message
        failed = []

        def create_once_failing(*args, **kwargs):
            if not failed:
                failed.append(True)
                raise MailboxError("target session changed before creation")
            return create(*args, **kwargs)
        self.mailbox.create_message = create_once_failing
        task_id = self.to_worker({"kind": "work", "message": "collect", "spec": {"goal": "g", "paths": []},
                                  "commands": ["lscpu"]})["task_id"]
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["held_reason"] == "instruction_rejected"))
        self.assertIsNone(self.flow.tasks[task_id].task_message_id)
        self.worker_session = (str(uuid4()), 1)
        _, message = self.follow_up(task_id, "please start")
        self.assertEqual(message.kind, MessageKind.TASK, "the run's TASK message, so reports can reply to it")
        self.assertTrue(message.payload["resent_task"])
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["held_reason"] is None
                                      and self.flow.task_view()["status"] == "running"))
        self.assertEqual(self.flow.tasks[task_id].task_message_id, message.message_id)
        self.assertEqual(self.to_manager({"kind": "progress", "message": "started"})["status"], "queued")

    def test_no_resend_while_the_task_message_is_still_on_its_way(self):
        # review-02 P3-T: the TASK's last outbox event (delivered) is seen before the state is set by hand, so a late
        # event cannot overwrite it
        events: list[str] = []
        original = self.flow._task_message_listener

        def observed(task_id, run_id):
            inner = original(task_id, run_id)

            def listener(event, snapshot):
                inner(event, snapshot)
                events.append(event)
            return listener
        self.flow._task_message_listener = observed
        task_id = self.work()
        self.assertTrue(rs_wait(lambda: "delivered" in events), events)
        self.worker_session = (str(uuid4()), 1)
        self.flow._task_message_state[task_id] = "queued"
        _, message = self.follow_up(task_id)
        self.assertNotIn("resent_task", message.payload)

    def test_an_unknown_worker_session_or_an_experiment_task_never_resends(self):
        task_id = self.work()
        self.worker_session = None
        _, message = self.follow_up(task_id)
        self.assertNotIn("resent_task", message.payload)


if __name__ == "__main__":
    unittest.main()
