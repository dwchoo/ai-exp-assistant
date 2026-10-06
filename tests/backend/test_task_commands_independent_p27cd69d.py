"""C-D69 (6) independent (p27-cd69-cmds-test-01): manager-authored Task ``commands`` and the done report.

Derived from DECISIONS.md C-D69 (6), not from the implementation:
(a) the manager writes the exact commands in ``to_worker`` (kind work), alternatives as more entries; bounded,
    no NUL, no environment values; not for experiments or a cancel; a follow-up with commands replaces the list,
    null keeps it.
(b) the worker's Task message shows the commands (numbered) and the rule (run as given, in order, summarise);
    while the Task is active only those run; no Task: no restriction.
(d) the worker's done/blocked report gets the list of commands the harness really ran (command, exit code or
    signal, duration, log path) from the record; refused attempts are not in it; the worker's text is kept.
Real TaskFlow/HandoffService (fake mailbox) and a real TerminalService; the report check uses a real bash
ShellPane. No OMP, no provider; temp files under /tmp only.
"""

from __future__ import annotations

import gc
import re
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from workbench.backend import flow as flow_module
from workbench.backend import flow_terminal
from workbench.backend.flow_terminal import TerminalService
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.contracts.v1 import ActorRole
from workbench.terminal.shell_g2.prototype import ShellChoice

import test_flow_terminal as tf
import test_task_flow as fx

WORK = {"kind": "work", "message": "collect facts with these commands; stop on the first failure",
        "spec": {"goal": "facts", "paths": []}}


def errors_for(args):
    return flow_module.validate_arguments("to_worker", flow_module.normalize_arguments("to_worker", args))


class _CommandsFixture(fx.FlowFixture):
    def setUp(self):
        super().setUp()
        self.port_for_terminal = tf.FakeHostPort()
        self.terminal = TerminalService(handoffs=self.service, host_shell=lambda: self.port_for_terminal,
                                        gate=self.flow._host_gate, log_root=self.root / "terminal",
                                        automation=lambda: tf.AUTOMATION,
                                        activity=self.flow.experiment_host_activity,
                                        active_task=self.flow.active_task, task_commands=self.flow.active_commands,
                                        poll_interval=0.02)
        self.flow.terminal_runs = self.terminal.runs_for_task
        self.terminal_calls = 0

    def tearDown(self):
        self.terminal.close()
        super().tearDown()

    def work(self, commands, **extra):
        return self.to_worker({**WORK, "commands": commands, **extra})

    def running(self, commands, **extra):
        result = self.work(commands, **extra)
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(tf.wait_until(lambda: self.flow.task_view()["status"] == "running"), self.flow.task_view())
        return result["task_id"]

    def terminal_call(self, command, wait=5):
        self.terminal_calls += 1
        self.terminal._wait_seconds = wait
        return self.terminal.handle(ActorRole.WORKER, tf.call({"command": command}, f"t-{self.terminal_calls}"))

    def refused(self, command):
        return self.terminal_call(command).get("status") == "not_in_task_commands"

    def task_messages(self):
        return [m.payload for m in self.mailbox.created if m.payload.get("handoff") != "to_manager"]

    def reports(self):
        return [m.payload for m in self.mailbox.created if m.payload.get("handoff") == "to_manager"]


class CommandsSchemaTests(_CommandsFixture):
    def test_count_size_nul_and_type_bounds(self):
        self.assertEqual(errors_for({**WORK, "commands": ["true"] * 32}), [])
        self.assertEqual(errors_for({**WORK, "commands": ["x" * 8192]}), [])
        for bad in (["true"] * 33, ["x" * 8193], ["ls\x00"], ["ls", "a\x00b"], [3], [["ls"]], [{"c": "ls"}],
                    "ls", {"0": "ls"}, True):
            with self.subTest(bad=repr(bad)[:30]):
                errors = errors_for({**WORK, "commands": bad})
                self.assertTrue([e for e in errors if e.startswith("commands")], errors)
        before = len(self.mailbox.created)
        result = self.work(["true"] * 33)
        self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
        self.assertIsNone(self.flow.active(), "nothing was created")
        self.assertEqual(len(self.mailbox.created), before)

    def test_environment_values_are_refused_in_commands(self):
        result = self.work(["echo ok", "curl -H x https://example.invalid API_TOKEN=abc123def456"])
        self.assertEqual((result["status"], result["reason"]), ("rejected", "environment_value"), result)
        self.assertIn("commands[1]:assignment:API_TOKEN", result["fields"])
        self.assertIsNone(self.flow.active())
        self.assertEqual(self.work(["echo \"$API_TOKEN\" | wc -c"])["status"], "dispatched", "a $NAME reference is fine")
        findings = flow_module.environment_value_findings({"commands": ["ls", "echo s3cret-value-123456"]},
                                                          ("s3cret-value-123456",))
        self.assertEqual(findings, ["commands[1]:sensitive_value"])

    def test_experiment_and_cancel_reject_commands(self):
        args = {"kind": "experiment", "message": "run it", "commands": ["./run.sh"],
                "spec": {"goal": "g", "paths": ["out.txt"], "execution": fx.execution()}}
        result = self.to_worker(args)
        self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
        self.assertTrue([e for e in result["errors"] if e.startswith("commands: must be null for kind experiment")])
        self.assertIsNone(self.flow.active())
        task_id = self.running(["echo kept"])
        cancel = self.to_worker({"kind": "work", "message": "stop", "task_id": task_id, "cancel": True,
                                 "commands": ["echo other"]})
        self.assertEqual((cancel["status"], cancel["reason"]), ("rejected", "invalid_arguments"), cancel)
        self.assertEqual(self.flow.task_view()["status"], "running", "the rejected cancel cancelled nothing")
        self.assertEqual(self.flow.active_commands(), ["echo kept"], "and replaced nothing")

    def test_blank_placeholders_mean_null(self):
        for blank in ([], [""], ["  ", None], None):
            with self.subTest(blank=blank):
                self.assertNotIn("commands", flow_module.normalize_arguments("to_worker", {**WORK, "commands": blank}))
        self.assertEqual(flow_module.normalize_arguments("to_worker", {**WORK, "commands": ["", "ls ", None]})
                         ["commands"], ["ls "], "only blank entries are dropped; the rest is kept verbatim")
        task_id = self.running(["", "   "])
        self.assertIsNone(self.flow.active_commands(), "all-blank commands are no commands")
        self.assertNotIn("commands", self.task_messages()[0])
        self.to_manager({"kind": "done", "message": "nothing"})
        self.assertTrue(tf.wait_until(lambda: self.flow.worker_view()["state"] == "idle"))
        task_id = self.running(["echo a"])
        follow = self.to_worker({"kind": "work", "message": "again", "task_id": task_id, "commands": ["", " "]})
        self.assertEqual(follow["status"], "queued", follow)
        self.assertEqual(self.flow.active_commands(), ["echo a"], "a blank follow-up list keeps the list")
        self.assertNotIn("commands_replaced", [r["type"] for r in self.ledger()])


class TaskMessageTests(_CommandsFixture):
    COMMANDS = ["  ls -la /tmp  ", "printf '%s\\n' \"한글 ✓\" 'it''s'",
                "cat <<'EOF'\nline $HOME\nEOF", "make test || make check"]

    def test_the_task_message_shows_the_exact_commands_numbered_with_the_rule(self):
        task_id = self.running(self.COMMANDS)
        self.assertTrue(tf.wait_until(lambda: len(self.task_messages()) == 1))
        payload = self.task_messages()[0]
        self.assertEqual(payload["commands"], [{"number": i, "command": c} for i, c in enumerate(self.COMMANDS, 1)],
                         "verbatim, in the manager's order")
        self.assertEqual(payload["message"], WORK["message"], "the manager's message is unchanged")
        rule = payload["commands_rule"]
        for needle in ("exactly as given", "in order", "summary", "refuses any other command"):
            self.assertIn(needle, rule)
        self.assertRegex(rule, r"(?i)do not (write|paste)")
        self.assertEqual(self.flow.active_commands(), self.COMMANDS)
        decisions = self.decisions(task_id, 1)
        self.assertEqual(decisions[0]["details"]["commands"], self.COMMANDS, "part of the delegated scope")
        saved = [r["task"] for r in self.ledger() if r["type"] == "task" and r["task"]["task_id"] == task_id]
        self.assertEqual(saved[-1]["commands"], self.COMMANDS)

    def test_follow_ups_replace_or_keep_and_the_old_list_is_refused(self):
        task_id = self.running(["echo old-1", "echo old-2"])
        self.assertTrue(self.refused("echo new-1"))
        self.assertFalse(self.refused("echo old-1"))
        kept = self.to_worker({"kind": "work", "message": "also report the time", "task_id": task_id,
                               "commands": None})
        self.assertEqual(kept["status"], "queued", kept)
        self.assertTrue(tf.wait_until(lambda: len(self.task_messages()) == 2))
        self.assertNotIn("commands", self.task_messages()[1])
        self.assertEqual(self.flow.active_commands(), ["echo old-1", "echo old-2"])
        replaced = self.to_worker({"kind": "work", "message": "use these instead", "task_id": task_id,
                                   "commands": ["echo new-1"]})
        self.assertEqual(replaced["status"], "queued", replaced)
        self.assertTrue(tf.wait_until(lambda: len(self.task_messages()) == 3))
        self.assertEqual(self.task_messages()[2]["commands"], [{"number": 1, "command": "echo new-1"}])
        self.assertIn("commands_rule", self.task_messages()[2])
        # C-D69 (6) / review-01 P3-5: the replacement takes effect once the worker's OMP accepted the message
        self.assertTrue(tf.wait_until(lambda: self.flow.active_commands() == ["echo new-1"]),
                        "replaced after delivery")
        self.assertTrue(self.refused("echo old-1"), "the replaced list no longer runs")
        self.assertTrue(self.refused("echo old-2"))
        self.assertFalse(self.refused("echo new-1"))
        saved = [r["task"] for r in self.ledger() if r["type"] == "task" and r["task"]["task_id"] == task_id]
        self.assertEqual(saved[-1]["commands"], ["echo new-1"], "the replacement is saved with the Task")
        experiment = {"kind": "experiment", "message": "x", "task_id": task_id, "commands": ["echo e"]}
        self.assertEqual(self.to_worker(experiment)["status"], "rejected")
        self.assertEqual(self.flow.active_commands(), ["echo new-1"])

    def test_a_follow_up_whose_delivery_fails_keeps_the_old_list(self):
        """review-01 P3-5 (fixed in fix-01), C-D69 (6): a follow-up the worker never received must not change the
        list the worker's terminal accepts; a later delivered one does."""
        task_id = self.running(["echo old-1", "echo old-2"])
        self.assertTrue(tf.wait_until(lambda: len(self.mailbox.delivered) == 1))
        original = self.mailbox.deliver
        for status in (fx.MailboxStatus.REJECTED, fx.MailboxStatus.UNKNOWN):
            with self.subTest(status=status.value):
                before = len(self.mailbox.created)

                def failing(message, *, timeout=20, _status=status):
                    self.mailbox.delivered.append(message.message_id)
                    return fx.DeliveryReceipt(message.message_id, str(fx.uuid4()), message.target_role,
                                              message.session_id, 1, _status, {"reason": "test"})
                self.mailbox.deliver = failing
                queued = self.to_worker({"kind": "work", "message": "use these instead", "task_id": task_id,
                                         "commands": ["echo new-1"]})
                self.assertEqual(queued["status"], "queued", queued)
                self.assertTrue(tf.wait_until(lambda: len(self.mailbox.created) == before + 1))
                self.assertTrue(tf.wait_until(lambda: len(self.mailbox.delivered) == before + 1))
                time.sleep(0.3)  # the late listener events, if any, have run
                self.assertEqual(self.flow.active_commands(), ["echo old-1", "echo old-2"], "the old list stays")
                self.assertFalse(self.refused("echo old-1"))
                self.assertTrue(self.refused("echo new-1"), "the list that never arrived is not accepted")
                self.assertNotIn("commands_replaced", [r["type"] for r in self.ledger()])
                saved = [r["task"] for r in self.ledger() if r["type"] == "task" and r["task"]["task_id"] == task_id]
                self.assertEqual(saved[-1]["commands"], ["echo old-1", "echo old-2"])
        self.mailbox.deliver = original
        queued = self.to_worker({"kind": "work", "message": "now these", "task_id": task_id,
                                 "commands": ["echo new-2"]})
        self.assertEqual(queued["status"], "queued", queued)
        self.assertTrue(tf.wait_until(lambda: self.flow.active_commands() == ["echo new-2"]), "delivered: replaced")
        self.assertTrue(self.refused("echo old-1"))


class LifecycleTests(_CommandsFixture):
    def test_the_restriction_follows_the_task_and_never_leaks_to_the_next(self):
        task_a = self.running(["echo a"])
        self.assertTrue(self.refused("echo b"))
        self.to_manager({"kind": "blocked", "message": "a is not enough", "reason": "missing_information"})
        self.assertTrue(tf.wait_until(lambda: self.flow.task_view()["status"] == "blocked"), self.flow.task_view())
        self.assertIsNone(self.flow.active_commands(), "a blocked Task does not occupy the worker")
        self.assertFalse(self.refused("echo free-while-blocked"))
        follow = self.to_worker({"kind": "work", "message": "try once more", "task_id": task_a})
        self.assertIn(follow["status"], {"queued", "dispatched"}, follow)
        self.assertTrue(tf.wait_until(lambda: self.flow.task_view()["status"] == "running"), self.flow.task_view())
        self.assertEqual(self.flow.active_commands(), ["echo a"], "resumed with its list")
        self.assertTrue(self.refused("echo b"))
        cancel = self.to_worker({"kind": "work", "message": "stop", "task_id": task_a, "cancel": True})
        self.assertNotEqual(cancel["status"], "rejected", cancel)
        self.assertTrue(tf.wait_until(lambda: self.flow.active_commands() is None), self.flow.task_view())
        self.assertFalse(self.refused("echo b"), "a cancelled Task restricts nothing")
        self.assertTrue(tf.wait_until(lambda: self.flow.worker_view()["state"] == "idle"))
        task_b = self.running(None)
        self.assertNotEqual(task_b, task_a)
        self.assertIsNone(self.flow.active_commands(), "a Task without commands restricts nothing")
        self.assertFalse(self.refused("echo a-leftover"))
        self.to_manager({"kind": "done", "message": "b done"})
        self.assertTrue(tf.wait_until(lambda: self.flow.worker_view()["state"] == "idle"))
        self.running(["echo c"])
        self.assertTrue(self.refused("echo a"), "only the current Task's list counts")
        self.assertFalse(self.refused("echo c"))

    def test_a_task_that_survives_a_restart_keeps_its_commands(self):
        task_id = self.running(["echo only"])
        self.terminal.close()
        self.restart()
        view = self.flow.task_view()
        if view and view.get("task_id") == task_id and view.get("status") not in {"finished", "blocked", "closed"}:
            self.assertEqual(self.flow.active_commands(), ["echo only"], "restored from the saved Task")
            terminal = TerminalService(handoffs=self.service, host_shell=lambda: self.port_for_terminal,
                                       gate=self.flow._host_gate, log_root=self.root / "terminal2",
                                       automation=lambda: tf.AUTOMATION, active_task=self.flow.active_task,
                                       task_commands=self.flow.active_commands)
            self.addCleanup(terminal.close)
            refused = terminal.handle(ActorRole.WORKER, tf.call({"command": "echo other"}, "after-restart"))
            self.assertEqual(refused["status"], "not_in_task_commands", refused)
        else:
            self.assertIsNone(self.flow.active_commands(), view)


class EnforcementEdgeTests(_CommandsFixture):
    def test_trailing_ascii_whitespace_is_the_only_slack(self):
        self.running(["echo listed-ok"])
        for same in ("echo listed-ok ", "echo listed-ok\t", "echo listed-ok\n", "echo listed-ok \n\t "):
            with self.subTest(command=repr(same)):
                self.assertFalse(self.refused(same))
        for other in (" echo listed-ok", "echo listed-ok\\", "echo listed-ok;", "echo listed-ok\u200b",
                      "echo listed-ok\x00"):
            with self.subTest(command=repr(other)):
                result = self.terminal_call(other)
                self.assertIn(result.get("status"), {"not_in_task_commands", "rejected"}, result)
        self.assertEqual(self.port_for_terminal.sent, [], "a refusal types nothing")

    def test_trailing_characters_the_shell_does_not_ignore_are_refused(self):
        """C-D69 (6) / test-01 P3-1 (fixed in fix-01): the comparison ignores only trailing space, tab and newline;
        \\r, \\v, \\f, U+001C..U+001F, U+0085, U+00A0, U+2028, U+3000 stay in the last word of the shell."""
        self.running(["echo listed-ok"])
        for char in ("\r", "\x0b", "\x0c", "\x1c", "\x1f", "\x85", "\xa0", "\u2028", "\u3000"):
            with self.subTest(char=repr(char)):
                self.assertTrue(self.refused("echo listed-ok" + char))


class ReportAppendixRealShellTests(fx.FlowFixture):
    """(d) on a real bash host shell: the list in the report is what really ran, in order, nothing else."""

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8"})
        self.stop = threading.Event()
        self.loop = threading.Thread(target=self._loop, daemon=True)
        self.loop.start()
        self.terminal = TerminalService(handoffs=self.service,
                                        host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
                                        gate=self.flow._host_gate, log_root=self.root / "terminal",
                                        automation=lambda: tf.AUTOMATION,
                                        activity=self.flow.experiment_host_activity,
                                        active_task=self.flow.active_task, task_commands=self.flow.active_commands,
                                        poll_interval=0.02)
        self.flow.terminal_runs = self.terminal.runs_for_task
        self.n = 0
        self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        self.assertIsNone(self.pane.admit(f"cd {self.home}\r".encode()))
        self.assertTrue(tf.wait_until(lambda: self.pane.cwd() == str(self.home), 5))

    def _loop(self):
        while not self.stop.is_set():
            self.pane.pump()
            time.sleep(0.01)

    def tearDown(self):
        self.terminal.close()
        self.stop.set()
        self.loop.join(5)
        self.pane.close()
        super().tearDown()

    def run_cmd(self, command, wait=30):
        self.n += 1
        self.terminal._wait_seconds = wait
        result = self.terminal.handle(ActorRole.WORKER, tf.call({"command": command}, f"rt-{self.n}"))
        if result.get("status") == "exited":
            self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt"
                                          and self.flow._host_gate.owner is None, 10))
        return result

    def reports(self):
        return [m.payload for m in self.mailbox.created if m.payload.get("handoff") == "to_manager"]

    def start(self, commands):
        result = self.to_worker({**WORK, "commands": commands})
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(tf.wait_until(lambda: self.flow.task_view()["status"] == "running"), self.flow.task_view())
        return result["task_id"]

    def test_done_and_blocked_reports_list_exactly_the_runs_of_their_task(self):
        long_script = "printf 'long-start\\n'\n" + "\n".join(f": pad {i:03d} {'y' * 40}" for i in range(100)) \
            + "\nprintf 'long-end\\n'\nexit 3"
        a = ["echo a-one", long_script, "printf 'x\\n'; kill -TERM $$"]
        self.start(a)
        self.assertEqual(self.run_cmd("echo hacked")["status"], "not_in_task_commands")
        r1 = self.run_cmd("echo a-one")
        self.assertEqual(self.run_cmd("echo a-one; echo extra")["status"], "not_in_task_commands")
        r2 = self.run_cmd(long_script)
        r3 = self.run_cmd(a[2])
        self.assertEqual([(r["status"], r.get("exit_code")) for r in (r1, r2, r3)],
                         [("exited", 0), ("exited", 3), ("exited", 143)], (r1, r2, r3))
        self.to_manager({"kind": "done", "message": "a-one printed; long script exit 3; last one killed"})
        self.assertTrue(tf.wait_until(lambda: len(self.reports()) == 1))
        report = self.reports()[0]
        self.assertEqual(report["message"], "a-one printed; long script exit 3; last one killed")
        listed = report["commands_run"]
        self.assertEqual([e["command"] for e in listed], ["echo a-one", "printf 'long-start\\n' …", a[2]],
                         "in start order; the refused attempts are not listed")
        for entry, result in zip(listed, (r1, r2, r3)):
            self.assertEqual((entry["status"], entry["exit_code"], entry["signal"], entry["log_path"],
                              entry["duration_seconds"]),
                             (result["status"], result["exit_code"], result.get("signal"), result["log_path"],
                              result["duration_seconds"]))
        self.assertEqual(listed[2]["signal"], 15)
        self.assertEqual(listed[1]["script_path"], r2["script_path"])
        self.assertNotIn("script_path", listed[0])
        self.assertIn("log_path", report["commands_run_note"])
        self.assertNotIn("pad 050", str(report), "the script body is not pasted into the report")
        self.assertTrue(tf.wait_until(lambda: self.flow.worker_view()["state"] == "idle"))
        # a second Task: its blocked report lists only its own runs, a still-running one as running
        self.start(["sleep 2; echo b-late", "echo b-one"])
        self.assertEqual(self.run_cmd("echo a-one")["status"], "not_in_task_commands", "Task A's list is gone")
        b1 = self.run_cmd("echo b-one")
        b2 = self.run_cmd("sleep 2; echo b-late", wait=0.3)
        self.assertEqual(b2["status"], "running", b2)
        self.to_manager({"kind": "blocked", "message": "b-late still running", "reason": "missing_information"})
        self.assertTrue(tf.wait_until(lambda: len(self.reports()) == 2))
        listed = self.reports()[1]["commands_run"]
        self.assertEqual([(e["command"], e["status"], e["exit_code"]) for e in listed],
                         [("echo b-one", "exited", 0), ("sleep 2; echo b-late", "running", None)])
        self.assertEqual(listed[0]["log_path"], b1["log_path"])
        self.assertTrue(tf.wait_until(lambda: self.flow._host_gate.owner is None, 10), "the late command ends")


    def test_after_a_restart_the_report_does_not_present_a_partial_list_as_complete(self):
        """C-D69 (6)(d) / test-01 P3-2 and review-01 P2-2 (fixed in fix-01): after a backend restart the done report
        is rebuilt from the terminal journal (the run is listed), never an empty list presented as complete."""
        self.start(["echo before-restart"])
        self.assertEqual(self.run_cmd("echo before-restart")["status"], "exited")
        self.terminal.close()
        self.restart()
        self.terminal = TerminalService(handoffs=self.service,
                                        host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
                                        gate=self.flow._host_gate, log_root=self.root / "terminal",
                                        automation=lambda: tf.AUTOMATION, active_task=self.flow.active_task,
                                        task_commands=self.flow.active_commands, poll_interval=0.02)
        self.flow.terminal_runs = self.terminal.runs_for_task
        self.assertEqual(self.to_manager({"kind": "done", "message": "after restart"})["status"], "queued")
        self.assertTrue(tf.wait_until(lambda: len(self.reports()) == 1))
        report = self.reports()[0]
        self.assertTrue(any(r.get("type") == "terminal_started" for r in tf.journal(self.root)))
        listed = [entry["command"] for entry in report.get("commands_run") or []]
        self.assertEqual(listed, ["echo before-restart"], report)
        self.assertEqual(report["commands_run"][0]["status"], "exited")
        self.assertEqual(report["commands_run"][0]["exit_code"], 0)
        self.assertRegex(report.get("commands_run_note", ""), r"(?i)rebuilt|before a backend restart")


class ReportUnreadableJournalTests(_CommandsFixture):
    def test_the_done_report_carries_the_unreadable_note_not_a_bare_empty_list(self):
        self.running(["echo a"])
        with mock.patch.object(self.service, "read_records", side_effect=OSError("journal gone")):
            self.to_manager({"kind": "done", "message": "finished"})
            self.assertTrue(tf.wait_until(lambda: len(self.reports()) == 1))
        report = self.reports()[0]
        self.assertEqual(report["commands_run"], [])
        self.assertRegex(report["commands_run_note"], r"(?i)could not be read.*does not show that nothing ran")
        self.assertEqual(report["message"], "finished")


class RunRecordBoundTests(unittest.TestCase):
    """C-D69 (6)(d) after fix-01: bounded memory per Task, an explicit note when older runs are left out."""

    @staticmethod
    def bare_service():
        service = TerminalService.__new__(TerminalService)
        service._runs_lock = threading.Lock()
        service._task_runs = {}
        return service

    @staticmethod
    def remember(service, task, index, **extra):
        command = flow_terminal._Command(f"id-{task}-{index}", f"echo run-{index}", {}, task,
                                         Path(f"/tmp/run-{task}-{index}.log"))
        service._remember_run(command)
        command.result = {"status": "exited", "exit_code": 0, "duration_seconds": 0.01}
        service._finish_run(command, command.result)
        return command

    def test_more_runs_than_kept_are_not_silently_dropped(self):
        """test-01 P3-2 / review-01 P2-2: past RUNS_KEPT (64) the report lists the last 64 and says, in
        ``notes``, how many older runs are omitted - never a silently shortened list."""
        service = self.bare_service()
        extra = 6
        for index in range(flow_terminal.RUNS_KEPT + extra):
            self.remember(service, "task-many", index)
        listed = service.runs_for_task("task-many")
        self.assertEqual(len(listed), flow_terminal.RUNS_KEPT)
        self.assertEqual(listed[0]["command"], f"echo run-{extra}", "the oldest are the ones left out")
        self.assertEqual(listed[-1]["command"], f"echo run-{flow_terminal.RUNS_KEPT + extra - 1}")
        self.assertTrue(any(re.search(rf"\b{extra} older runs omitted", note) for note in listed.notes), listed.notes)
        within = self.bare_service()
        for index in range(flow_terminal.RUNS_KEPT):
            self.remember(within, "task-fit", index)
        self.assertEqual(len(within.runs_for_task("task-fit")), flow_terminal.RUNS_KEPT)
        self.assertFalse([n for n in within.runs_for_task("task-fit").notes if "omitted" in n],
                         "no note when nothing is left out")

    def test_an_unreadable_journal_is_said_not_shown_as_nothing_ran(self):
        """review-01 P2-2 / test-01 P3-2: when the terminal journal cannot be read back, the list is the process's
        own record and ``notes`` say it may be incomplete (an empty list is not a statement that nothing ran)."""
        service = self.bare_service()
        self.remember(service, "task-j", 0)
        service._handoffs = SimpleNamespace(read_records=mock.Mock(side_effect=OSError("journal gone")))
        service._lock = threading.Lock()
        service._current = None
        listed = service.runs_for_task("task-j")
        self.assertEqual([e["command"] for e in listed], ["echo run-0"], "what the process recorded")
        self.assertTrue(any(re.search(r"(?i)could not be read", n) and re.search(r"(?i)does not show that nothing ran",
                                                                                 n) for n in listed.notes), listed.notes)
        empty = service.runs_for_task("task-none")
        self.assertEqual(list(empty), [])
        self.assertTrue(empty.notes, "an empty list from an unreadable journal still carries the note")

    def test_a_finished_light_record_frees_the_output_buffers(self):
        """review-01 P2-1: the per-Task record does not keep the command's output buffers (bounded memory)."""
        import weakref
        service = self.bare_service()
        command = flow_terminal._Command("id-w-1", "echo big", {}, "task-w", Path("/tmp/run-w.log"))
        service._remember_run(command)
        command.recent = bytearray(b"x" * 32768)
        ref = weakref.ref(command)
        service._finish_run(command, {"status": "exited", "exit_code": 0, "duration_seconds": 0.1})
        del command
        gc.collect()
        self.assertIsNone(ref(), "the Task's run record must not keep the _Command (and its buffers) alive")
        entry = service.runs_for_task("task-w")[0]
        self.assertEqual((entry["status"], entry["exit_code"]), ("exited", 0), "the light record has the result")
        for value in service._task_runs["task-w"].runs:
            self.assertNotIsInstance(value, flow_terminal._Command)
            self.assertFalse(any(isinstance(part, (bytes, bytearray)) and len(part) > 1024 for part in value))


del _CommandsFixture

if __name__ == "__main__":
    unittest.main()
