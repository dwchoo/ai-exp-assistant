"""p27-cd68-test-03 independent tests for the fix-03 behaviours of the worker ``terminal`` tool.

Expectations come from DECISIONS.md C-D68 (9) and the smoke-01 / review-02 findings recorded in
result-p27-cd68-fix-03.md, not from flow_terminal.py:

- smoke-01 P1: the host pane shows ``[worker] $ <command>`` before the output; the worker's result, the log file and
  the periodic check / completion notice do not contain that line; exit code and process identity are those of
  ``<shell> -c <command>`` (a direct child of the parent shell, no extra arguments, signal exit unchanged).
- smoke-01 P2: ``host_operator()`` / the ui_v1 host pane ``operated_by`` is ``worker`` from the command start to the
  shell's give-back, never for an experiment run, and the UI shows ``owner: worker`` only then.
- smoke-01 M2: a ``queued`` handoff result says it was accepted and must not be sent again.
- smoke-01 M3: the ``running`` result tells the worker to end its turn and send no progress reports.
- review-02 P3: an abort before the submit leaves no (empty) log; ``peer_gone`` drops the waiters of a lost worker
  session so the 60 s checks resume and the completion notice is sent.

Real ShellPane + HostShellPort (bash), fake clock; no OMP, no model, no network.
"""

from __future__ import annotations

from pathlib import Path
import re
import shlex
import sys
import threading
import time
import unittest
from uuid import uuid4

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "ui"))

import test_task_flow as flow_fixtures  # noqa: E402
import test_terminal_independent_p27cd68 as base  # noqa: E402
from workbench.backend.panes import HostShellPort  # noqa: E402
from workbench.backend.service import Backend  # noqa: E402
from workbench.contracts.v1 import ActorRole  # noqa: E402
from workbench.ui.product.model import ProductModel  # noqa: E402

import independent_support_cw06 as ui_support  # noqa: E402
from support import FakeSender  # noqa: E402

wait_until = base.wait_until
MARK = "[worker] $ "


class _AbortAtClaim:
    """The real host port; fires ``hook`` right before the command would be submitted (after the log exists)."""

    def __init__(self, real, hook):
        self._real, self._hook = real, hook

    def claim_manager(self, *args, **kwargs):
        self._hook()
        return self._real.claim_manager(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class PaneDisplay(base.RealShellFixture):
    def test_pane_shows_the_command_first_while_result_log_and_exit_code_exclude_it(self):
        parent_pid = self.user_sees('echo "PARENT=$$"', "PARENT")
        command = "echo \"args=$# zero=${0##*/} self=$$\"; echo 'quote \"ok\" $HOME'; exit 4"
        before = len(self.screen())
        result = self.tool({"command": command, "wait": 20})
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 4), result)
        # C-D68 (9) / smoke-01 P1: identical to `<shell> -c <command>`, a direct child of the parent shell
        self.assertIn("args=0 zero=bash", result["output_tail"])
        # the process is a new child (as before fix-03: the supervisor runs it), never the parent shell itself
        self.assertNotIn(f"self={parent_pid}", result["output_tail"])
        self.assertRegex(result["output_tail"], r"self=\d+")
        self.assertIn('quote "ok" $HOME', result["output_tail"])
        self.assertNotIn(MARK, result["output_tail"])
        self.assertNotIn(MARK.encode(), Path(result["log_path"]).read_bytes())
        shown = self.screen()[before:].decode("utf-8", "replace")
        line = MARK + command
        self.assertEqual(shown.count(line), 1, shown)
        self.assertLess(shown.index(line), shown.index("args=0"), "the command line must come before its output")
        self.assertTrue(wait_until(self.idle, 10), "the shell was not given back")
        self.assertEqual(self.user_sees('echo "BACK=$?"', "BACK"), "0")

    def test_signal_exit_semantics_are_unchanged_by_the_display_line(self):
        result = self.tool({"command": "echo before; kill -KILL $$", "wait": 20})
        self.assertEqual((result["status"], result["exit_code"], result.get("signal")), ("exited", 137, 9), result)
        self.assertNotIn(MARK, result["output_tail"])
        self.assertTrue(wait_until(self.idle, 10))

    def test_the_display_line_is_not_typed_into_the_parent_shell_history(self):
        marker = f"nohist-{uuid4().hex[:8]}"
        self.tool({"command": f"echo {marker}", "wait": 20})
        self.assertTrue(wait_until(self.idle, 10))
        pattern = marker[:-1] + "[" + marker[-1] + "]"  # the typed check line itself does not match its own regex
        history = self.user_sees(f'echo "H=$(history | grep -c {pattern})"', "H")
        self.assertEqual(history, "0", "the worker's command went into the parent's history")


class OperatedBy(base.RealShellFixture):
    clock_driven = True

    def info(self):
        # the real ui_v1 host pane info as the backend builds it, with this fixture's real terminal service
        fixture = type("B", (), {"terminal": self.terminal})()
        return Backend._pane_info(fixture, self.pane, host=True)

    def test_worker_command_marks_the_host_pane_and_the_ui_shows_the_worker(self):
        self.assertIsNone(self.terminal.host_operator())
        self.assertIsNone(self.info()["operated_by"])
        flag = self.root / "go"
        result = self.tool({"command": f"while [ ! -e {shlex.quote(str(flag))} ]; do sleep 0.05; done", "wait": 1})
        self.assertEqual(result["status"], "running", result)
        self.assertEqual(self.terminal.host_operator(), "worker")
        info = self.info()
        self.assertEqual(info["operated_by"], "worker")
        self.assertEqual(info["pane"], "host_shell")
        snapshot = ui_support.snap(owner="manager", mode="control_wait")
        snapshot["panes"]["host_shell"]["operated_by"] = info["operated_by"]
        model = ProductModel(FakeSender(), 30, 120, clock=lambda: 1000.0)
        model.apply_snapshot(snapshot)
        self.assertIn("host 입력 owner: worker", model.status_lines()[0])
        flag.touch()
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        self.assertTrue(wait_until(lambda: self.terminal.host_operator() is None, 10), "worker shown after the end")
        self.assertIsNone(self.info()["operated_by"])

    def test_an_experiment_run_never_shows_the_worker_as_operator(self):
        self.assertIsNone(self.gate.acquire("experiment"))
        self.assertIsNone(self.terminal.host_operator())
        self.assertIsNone(self.info()["operated_by"])
        snapshot = ui_support.snap(owner="manager", mode="control_wait")
        snapshot["panes"]["host_shell"]["operated_by"] = self.info()["operated_by"]
        model = ProductModel(FakeSender(), 30, 120, clock=lambda: 1000.0)
        model.apply_snapshot(snapshot)
        self.assertIn("host 입력 owner: manager", model.status_lines()[0])
        self.assertNotIn("owner: worker", "\n".join(model.status_lines()))


class Notices(base.RealShellFixture):
    clock_driven = True

    def advance(self, seconds):
        self.clock.now += seconds
        self.terminal.tick()

    def start_slow(self, call_id=None):
        self.flag = self.root / "slow-go"
        command = (f"i=0; while [ ! -e {shlex.quote(str(self.flag))} ]; do i=$((i+1)); echo line-$i; sleep 0.1; done;"
                   " echo slow-done")
        result = self.tool({"command": command, "wait": 1}, call_id)
        self.assertEqual(result["status"], "running", result)
        return result

    def finish(self):
        self.flag.touch()
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))

    def test_running_result_says_end_your_turn_without_progress_reports(self):
        result = self.start_slow()
        detail = result["detail"]
        self.assertRegex(detail, re.compile(r"end your turn", re.I))
        self.assertRegex(detail, re.compile(r"do not send progress reports", re.I))
        self.assertRegex(detail, re.compile(r"unless the user or the manager asks|check shows a problem", re.I))
        self.finish()

    def test_check_and_completion_notice_exclude_the_display_line(self):
        self.start_slow()
        time.sleep(0.5)  # output keeps coming after the running result; the fake minute passes in one step
        self.advance(61)
        checks = self.notices.sent_of("terminal_check")
        self.assertEqual(len(checks), 1)
        self.assertNotIn(MARK, checks[0]["new_output"])
        self.assertNotIn(MARK, str(checks[0].get("output_tail", "")))
        self.assertIn("line-", checks[0]["new_output"])
        self.finish()
        self.advance(1)
        done = self.notices.sent_of("terminal_done")
        self.assertEqual(len(done), 1)
        self.assertNotIn(MARK, str(done[0].get("output_tail", "")))
        self.assertIn("slow-done", done[0]["output_tail"])
        self.assertNotIn(MARK.encode(), Path(done[0]["log_path"]).read_bytes())

    def test_peer_gone_clears_the_waiter_so_checks_and_the_notice_resume(self):
        self.start_slow()
        waiter = threading.Thread(target=lambda: self.tool({"command": None, "wait": 30}))
        waiter.start()
        self.addCleanup(waiter.join, 15)
        self.assertTrue(wait_until(lambda: self.terminal._waiting_now(), 5), "control: the null call waits")
        self.advance(61)
        self.assertEqual(self.notices.of("terminal_check"), [], "control: no check while a call waits")
        self.terminal.peer_gone(ActorRole.MANAGER, base.SESSION, 1)  # another role: nothing changes
        self.terminal.peer_gone(ActorRole.WORKER, str(uuid4()), 1)  # another session: nothing changes
        self.assertTrue(self.terminal._waiting_now())
        self.terminal.peer_gone(ActorRole.WORKER, base.SESSION, 1)  # the worker's bridge connection ended
        self.assertFalse(self.terminal._waiting_now())
        self.advance(60)
        self.assertEqual(len(self.notices.sent_of("terminal_check")), 1, "checks did not resume")
        self.finish()
        waiter.join(15)
        self.advance(1)
        self.assertEqual(len(self.notices.sent_of("terminal_done")), 1, "the lost call's result reached nobody")


class AbortBeforeSubmit(base.RealShellFixture):
    def test_no_empty_log_is_left_when_the_call_is_aborted_before_the_submit(self):
        sentinel = f"ABORT-NEVER-{uuid4().hex[:6]}"
        port = lambda: _AbortAtClaim(HostShellPort(self.pane, lambda: self.pane), lambda: self.terminal.abandon(
            ActorRole.WORKER, base.call({"tool_call_id": "call-nolog"}, "call-nolog") | {"tool": "terminal_wait_abandoned"}))
        self.terminal._host_shell = port  # the same service, the same pane; only the port is wrapped
        before = len(self.screen())
        result = self.tool({"command": f"echo {sentinel}", "wait": 5}, "call-nolog")
        self.assertEqual(result["status"], "aborted", result)
        self.assertEqual(self.logs(), [], "an empty log file was left behind")
        self.assertIsNone(self.terminal.current())
        self.assertNotIn(sentinel.encode(), self.screen()[before:], "the aborted command was typed")
        self.assertTrue(wait_until(self.idle, 10), "the shell was not given back")


class QueuedDetail(flow_fixtures.FlowFixture):
    def test_a_queued_handoff_result_says_do_not_send_it_again(self):
        work = self.running_work()
        for result in (self.to_manager({"kind": "progress", "message": "half done"}),
                       self.to_worker({"kind": "work", "message": "also rename foo", "task_id": work["task_id"],
                                       "spec": {"goal": "clean parser", "paths": ["docs/"]}})):
            self.assertEqual(result["status"], "queued", result)
            detail = result["detail"]
            self.assertRegex(detail, re.compile(r"accepted", re.I))
            self.assertRegex(detail, re.compile(r"do not send it again", re.I))
            self.assertNotRegex(detail, re.compile(r"not yet processed", re.I))


if __name__ == "__main__":
    unittest.main()
