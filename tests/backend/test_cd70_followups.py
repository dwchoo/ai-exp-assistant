"""p27-cd70-02 (Root adjudication of p27-cd70-01's open questions; no OMP, no provider).

- Q2: a manager follow-up on the open Task that the worker OMP accepted re-arms the watchdog (count and the single
  worker_stalled notice start over).
- Q3: the completion of a terminal command a previous worker session started goes to the manager (information:
  command id, status, exit code, log path; no output) while the Task has not reached the current worker session,
  and to the worker as before once it has. The re-sent Task lists that command in commands_already_run.
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from workbench.backend.flow_recovery import IDLE_LIMIT, MANAGER_NOTICE_TYPES
from workbench.backend.flow_terminal import HostGate, TerminalService
from workbench.contracts.v1 import ActorRole

import test_cd70_task_resend as rs
import test_cd70_watchdog as wd
import test_flow_terminal_notices as tn


class FollowUpReArmsTheWatchdogTests(rs.ResendFixture):
    def setUp(self):
        super().setUp()
        self.told: list[str] = []
        self.flow.follow_up_submitted = self.told.append

    def test_a_submitted_follow_up_is_told_once(self):
        task_id = self.work()
        self.assertEqual(self.told, [], "the TASK itself is not a follow-up")
        self.follow_up(task_id)
        self.assertTrue(rs.fx.wait_until(lambda: self.told == [task_id]))
        self.follow_up(task_id, "and the disk sizes", commands=["df -h"])
        self.assertTrue(rs.fx.wait_until(lambda: self.told == [task_id, task_id]))

    def test_a_resent_task_is_a_follow_up_too(self):
        task_id = self.work()
        self.worker_session = (str(uuid4()), 1)
        self.follow_up(task_id)
        self.assertTrue(rs.fx.wait_until(lambda: self.told == [task_id]))

    def test_a_follow_up_that_never_reached_the_worker_is_not(self):
        task_id = self.work()
        original = self.mailbox.deliver

        def rejected(message, *, timeout=20):
            self.mailbox.delivered.append(message.message_id)
            return rs.DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1,
                                      rs.MailboxStatus.REJECTED, {"reason": "x"})
        self.mailbox.deliver = rejected
        self.follow_up(task_id)
        self.mailbox.deliver = original
        self.assertEqual(self.told, [])


class WatchdogReArmTests(wd.WatchdogFixture):
    def test_after_a_follow_up_the_checks_and_one_more_stalled_notice_can_follow(self):
        self.run_for(IDLE_LIMIT * 4)
        self.assertEqual(len(self.checks()), 2)
        self.assertEqual(len(self.world.of("manager", "worker_stalled")), 1)
        self.run_for(IDLE_LIMIT * 4)
        self.assertEqual(len(self.world.of("manager", "worker_stalled")), 1, "no second notice by itself")
        self.dog.worker_acted("manager_follow_up")
        self.run_for(IDLE_LIMIT * 4)
        self.assertEqual(len(self.checks()), 4)
        self.assertEqual(len(self.world.of("manager", "worker_stalled")), 2)
        resets = [r for r in self.world.journal if r["type"] == "watchdog_reset"]
        self.assertEqual(resets[-1]["tool"], "manager_follow_up")


class OldSessionCompletionTests(tn.NoticeFixture):
    def setUp(self):
        super().setUp()
        self.terminal.close()
        self.manager = tn.Notices()
        self.route = "worker"
        self.asked: list[dict] = []

        def target(started):
            self.asked.append(dict(started))
            return self.route
        self.terminal = TerminalService(
            handoffs=self.handoffs, host_shell=lambda: self.port, gate=HostGate(),
            log_root=self.root / "workflow" / "terminal", automation=lambda: tn.AUTOMATION,
            paused=lambda: self.paused, poll_interval=0.01, notify=self.notices, clock=self.clock,
            notify_manager=self.manager, done_target=target)

    def test_the_manager_gets_an_information_notice_without_output_once(self):
        first = self.start_running(b"building\n")
        self.route = "manager"  # the worker OMP was restarted and has not got the Task again
        self.finish(3, b"secret-ish output\n")
        self.advance(1)
        self.advance(60)
        notices = self.manager.of("worker_terminal_done")
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual((notice["command_id"], notice["status"], notice["exit_code"], notice["log_path"]),
                         (first["command_id"], "exited", 3, first["log_path"]))
        self.assertNotIn("output_tail", notice)
        self.assertNotIn("secret-ish", repr(notice))
        self.assertIn("Do not run it again", notice["instruction"])
        self.assertEqual(self.notices.of("terminal_done"), [], "not to the new worker session")
        self.assertEqual(self.asked[0]["session_id"], tn.WORKER_SESSION)
        self.assertEqual(self.asked[0]["generation"], 1)
        records = [r for r in self.journal("terminal_notice") if r["outcome"] == "sent"]
        self.assertEqual([r["target"] for r in records], ["manager"])
        self.assertIn("worker_terminal_done", MANAGER_NOTICE_TYPES)

    def test_after_re_delivery_the_worker_gets_it_as_before(self):
        self.start_running(b"building\n")
        self.route = "worker"
        self.finish(0)
        self.advance(1)
        self.assertEqual(len(self.notices.of("terminal_done")), 1)
        self.assertEqual(self.manager.sent, [])

    def test_a_deferred_manager_notice_goes_to_the_worker_once_the_task_was_re_delivered(self):
        self.start_running(b"building\n")
        self.route = "manager"
        self.manager.outcome = "deferred"  # the manager is busy
        self.finish(0)
        self.advance(1)
        self.advance(5)
        self.assertTrue(self.manager.attempts)
        self.route = "worker"  # meanwhile the Task reached the new worker session
        self.advance(5)
        self.advance(5)
        sent = self.notices.of("terminal_done")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["notice_id"], self.manager.attempts[0]["notice_id"], "the same notice, never twice")
        self.assertEqual(self.manager.sent, [])


class RoutingRuleTests(unittest.TestCase):
    """The backend's rule (service.Backend._done_target) on a stub backend."""

    def rule(self, current, started, task_session):
        from workbench.backend.service import Backend
        stub = Backend.__new__(Backend)
        stub._worker_session_key = lambda: current
        stub.flow = type("F", (), {"task_session": staticmethod(lambda task_id: task_session)})()
        return Backend._done_target(stub, started)

    def test_rule(self):
        old, new = ("s-old", 1), ("s-new", 1)
        started = {"session_id": old[0], "generation": old[1], "task_id": "t"}
        self.assertEqual(self.rule(old, started, None), "worker", "same session: as before")
        self.assertEqual(self.rule(None, started, None), "worker", "no worker connected: retried as before")
        self.assertEqual(self.rule(new, started, ["s-old", 1]), "manager", "Task not re-delivered")
        self.assertEqual(self.rule(new, started, None), "manager")
        self.assertEqual(self.rule(new, started, ["s-new", 1]), "worker", "re-delivered to the current session")
        self.assertEqual(self.rule(new, {**started, "task_id": None}, None), "manager", "no Task: the manager")


class ResentTaskListsTheOldSessionsCommandTests(rs.ResendFixture):
    def test_commands_already_run_comes_from_the_terminal_record(self):
        task_id = self.work()
        self.runs_listed = [{"command": "lscpu", "status": "exited", "exit_code": 3, "signal": None,
                             "duration_seconds": 1.5, "log_path": "/tmp/x/terminal/old.log"}]
        self.worker_session = (str(uuid4()), 1)
        _, message = self.follow_up(task_id)
        self.assertEqual(message.payload["commands_already_run"][0]["exit_code"], 3)
        self.assertEqual(message.payload["commands_already_run"][0]["log_path"], "/tmp/x/terminal/old.log")


if __name__ == "__main__":
    unittest.main()
