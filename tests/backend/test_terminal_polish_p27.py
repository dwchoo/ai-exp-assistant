"""p27-polish-01: the worker ``terminal`` carried polish (no OMP, no provider, fake clock).

(2) a call that stops before the command was submitted (a pause or a moved shell between the hold and the
    submit -> ``start_failed``) leaves no empty 0600 log file; a script that may have reached the shell stays.
(4) a ``terminal`` call whose worker session is gone (bridge ``peer_gone``) stops waiting at once; a call the
    bridge already answered (``terminal_wait_abandoned``) still gets the backend's answer as before. When either
    ends it does not act as a call that saw the output: the pending check is not dropped, the check window does
    not restart and the output not yet returned is kept for the next call.
"""

from __future__ import annotations

import threading
import time
import unittest

from workbench.backend.flow_terminal import ABANDON_TOOL
from workbench.contracts.v1 import ActorRole

import test_flow_terminal_notices as base


class StartFailedLogTests(base.NoticeFixture):
    def log_files(self):
        root = self.root / "workflow" / "terminal"
        return sorted(p.name for p in root.iterdir()) if root.exists() else []

    def test_a_pause_before_the_submit_leaves_no_log_file(self):
        def pause():
            self.paused = True
        self.port.on_claim = pause
        result = self.run_tool({"command": "make test", "wait": 30})
        self.assertEqual(result["status"], "start_failed", result)
        self.assertEqual(self.port.submitted, [], "nothing was submitted")
        self.assertEqual(self.log_files(), [], "no empty log is left")
        self.assertTrue(any(r["type"] == "terminal_start_failed" for r in self.journal()))

    def test_a_moved_shell_before_the_submit_leaves_no_log_file(self):
        def move():
            self.port._cwd = "/tmp/somewhere-else"
        self.port.on_claim = move
        result = self.run_tool({"command": "make test", "wait": 30})
        self.assertEqual(result["status"], "start_failed", result)
        self.assertEqual(self.log_files(), [])

    def test_a_failed_submit_keeps_a_script_the_shell_may_run_but_not_the_empty_log(self):
        def broken(_control, command, _automation):
            self.port.submitted.append(command)
            raise OSError("pipe closed after the write")
        self.port.submit = broken
        result = self.run_tool({"command": "echo " + "x" * 4000, "wait": 30})  # too long: run from a script
        self.assertEqual(result["status"], "start_failed", result)
        self.assertEqual(len(self.port.submitted), 1)
        names = self.log_files()
        self.assertFalse([n for n in names if n.endswith(".log")], names)
        self.assertEqual(len([n for n in names if n.endswith(".sh")]), 1, "the shell may still run it")

    def test_a_started_command_keeps_its_log(self):
        result = self.start_running()
        self.assertIn(result["command_id"] + ".log", self.log_files())
        self.finish()


class DeadWaiterTests(base.NoticeFixture):
    def wait_in_thread(self, call_id, wait=30):
        box = {}

        def run():
            box["result"] = self.run_tool({"command": "make long", "wait": wait}, call_id)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(base.wait_until(lambda: self.terminal._waiting_now()))
        return thread, box

    def test_a_waiter_of_a_gone_session_returns_at_once(self):
        thread, box = self.wait_in_thread("call-gone")
        started = time.monotonic()
        self.terminal.peer_gone(ActorRole.WORKER, base.WORKER_SESSION, 1)
        thread.join(3)
        self.assertFalse(thread.is_alive(), "the dead call does not wait out its 30 s")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(box["result"]["status"], "running")
        self.finish()

    def test_a_call_the_bridge_answered_itself_still_gets_the_backend_answer(self):
        thread, box = self.wait_in_thread("call-abandoned")
        self.terminal.abandon(ActorRole.WORKER, base.call({"tool_call_id": "call-abandoned"}, tool=ABANDON_TOOL))
        time.sleep(0.3)
        self.assertTrue(thread.is_alive(), "only a gone session releases the call early")
        self.finish(4)
        thread.join(3)
        self.assertEqual(box["result"]["status"], "exited")

    def test_an_abandoned_waiter_that_ends_at_its_deadline_does_not_drop_the_pending_check(self):
        thread, _ = self.wait_in_thread("call-abandoned-late", wait=1)
        self.terminal.abandon(ActorRole.WORKER, base.call({"tool_call_id": "call-abandoned-late"},
                                                          tool=ABANDON_TOOL))
        self.notices.outcome = "deferred"  # the worker is busy
        self.emit(b"late output\n")
        self.advance(60)
        self.assertEqual(len(self.notices.attempts), 1)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.notices.outcome = "delivered"
        self.advance(2)
        checks = self.notices.of("terminal_check")
        self.assertEqual(len(checks), 1, "the abandoned call's end did not drop the check")
        self.assertIn("late output", checks[0]["new_output"])
        self.finish()

    def test_a_dead_waiter_does_not_drop_the_pending_check_or_take_the_output(self):
        thread, _ = self.wait_in_thread("call-dead", wait=1)
        self.terminal.peer_gone(ActorRole.WORKER, base.WORKER_SESSION, 1)
        self.notices.outcome = "not_connected"  # the worker OMP is restarting
        self.emit(b"output while nobody waits\n")
        self.advance(60)
        self.assertEqual(len(self.notices.attempts), 1, "the check is due and held for the worker")
        thread.join(5)  # the dead call has ended (its own wait was 1 s)
        self.assertFalse(thread.is_alive())
        self.notices.outcome = "delivered"
        self.advance(2)  # the retry
        checks = self.notices.of("terminal_check")
        self.assertEqual(len(checks), 1, "the pending check was not dropped by the dead call")
        self.assertIn("output while nobody waits", checks[0]["new_output"])
        self.assertNotIn("superseded", [r["outcome"] for r in self.journal("terminal_check")])
        fetched = self.run_tool({"command": None})
        self.assertIn("output while nobody waits", fetched["output_tail"], "the next call still gets it")
        self.finish()

    def test_a_live_waiter_still_ends_at_its_deadline_and_moves_the_check_window(self):
        thread, box = self.wait_in_thread("call-live", wait=0.3)
        thread.join(5)
        self.assertEqual(box["result"]["status"], "running")
        self.advance(59)
        self.assertEqual(self.notices.attempts, [], "the window restarted when the call returned")
        self.advance(1)
        self.assertEqual(len(self.notices.of("terminal_check")), 1)
        self.finish()

    def test_the_completion_still_wakes_a_waiter_at_once(self):
        thread, box = self.wait_in_thread("call-done", wait=30)
        started = time.monotonic()
        self.finish(3)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((box["result"]["status"], box["result"]["exit_code"]), ("exited", 3))
        self.terminal.tick()
        self.assertEqual(self.notices.of("terminal_done"), [], "the waiting call got the result")


if __name__ == "__main__":
    unittest.main()
