"""Runtime discriminators for the production common RUN boundary."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.gates.g2_shell import live_combined_boundary_probe as combined
from tests.gates.g2_shell.live_env_launch_probe import controlled
from tests.gates.g2_shell.test_control_probe import real_shells
from workbench.terminal.shell_g2.prototype import UnsafeShellState


class CommonRunProtocolTests(unittest.TestCase):
    def finish(self, session, status, since=0):
        combined.wait(session, "INPUT_BARRIER", since)
        barrier = next(index for index in range(since, len(session.events))
                       if session.events[index] == "INPUT_BARRIER")
        self.assertEqual(session.lifecycle.main_exit, status)
        self.assertFalse(session.lifecycle.returned)
        session.release_input()
        combined.wait(session, f"WAIT:{session.pid}:", barrier)
        self.assertTrue(session.lifecycle.returned, session.lifecycle)
        session.release_control()
        combined.wait(session, "READY", barrier)
        self.assertTrue(session.manual_prompt_confirmed)
        self.assertFalse(session.lifecycle.unknown)
        self.assertFalse(Path(f"/proc/{session.lifecycle.child_pid}").exists())
        self.assertFalse(Path(f"/proc/{session.lifecycle.supervisor_pid}").exists())

    def test_new_request_needs_return_manual_ack_and_fresh_observed_wait(self):
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory, \
                    controlled(choice.executable, directory) as (session, *_):
                session.dispatch_managed("first", ":")
                self.finish(session, 0)
                with self.assertRaises(UnsafeShellState):
                    session.rearm_after_handoff()
                original = session._on_control_event
                since = len(session.events)
                def lose_wait(event):
                    if not event.startswith(f"WAIT:{session.pid}:"):
                        original(event)
                with patch.object(session, "_on_control_event", side_effect=lose_wait):
                    session._write_all(b"wb-handoff\n")
                    wait = combined.wait(session, f"WAIT:{session.pid}:", since)
                self.assertTrue(session._fresh_handoff)
                self.assertFalse(session.control_wait_seen)
                with self.assertRaises(UnsafeShellState):
                    session.rearm_after_handoff()
                original(wait)
                session.rearm_after_handoff()
                second = len(session.events)
                session.dispatch_managed("second", ":")
                self.finish(session, 0, second)
                third = len(session.events)
                session._write_all(b"wb-handoff\n")
                combined.wait(session, f"WAIT:{session.pid}:", third)
                self.assertTrue(session._fresh_handoff and session.control_wait_seen)
                session.lifecycle.fail_unknown("fixture_observation_lost")
                with self.assertRaises(UnsafeShellState):
                    session.rearm_after_handoff()

    def prepared(self, session):
        prepared = combined.wait(session, "CHILD_PREPARED:", 0)
        _, pid, group, sid = prepared.split(":")
        pid = int(pid)
        self.assertEqual(int(group), pid)
        self.assertEqual(int(sid), session.pid)
        self.assertNotEqual(pid, session.lifecycle.supervisor_group)
        ready = combined.wait(session, f"EXEC_READY:{pid}", 0)
        self.assertLess(session.events.index(prepared),
                        session.events.index(f"FOREGROUND_VERIFIED:{pid}"))
        self.assertLess(session.events.index(f"FOREGROUND_VERIFIED:{pid}"),
                        session.events.index(ready))
        self.assertTrue(session.lifecycle.exec_ready)
        return pid

    def test_native_stop_continue_does_not_complete_or_restore_parent(self):
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory, \
                    controlled(choice.executable, directory) as (session, *_):
                session.dispatch_managed("native-stop", [sys.executable, str(Path(combined.__file__).resolve()),
                                                        "--split-running-child"])
                pid = self.prepared(session)
                combined.wait(session, f"SPLIT_RUNNING_READY:{pid}", 0)
                since = len(session.events)
                session._write_all(b"\x1a")
                self.assertEqual(combined.wait(session, "MAIN_STOPPED:", since),
                                 f"MAIN_STOPPED:{signal.SIGTSTP}")
                self.assertEqual(combined.proc_fields(pid)[3], "T")
                self.assertTrue(session.lifecycle.main_stopped)
                supervisor = session.lifecycle.supervisor_pid
                os.kill(supervisor, signal.SIGWINCH)
                combined.wait(session, "SUPERVISOR_PING:running", since)
                self.assertFalse(any(e == "READY" or e.startswith(("RETURN:", "WAIT:", "MAIN_RETURN:"))
                                     for e in session.events[since:]))
                self.assertFalse(session.lifecycle.returned)
                with self.assertRaises(UnsafeShellState):
                    session.release_input()
                resumed = len(session.events)
                os.kill(supervisor, signal.SIGUSR2)
                combined.wait(session, "MAIN_CONTINUED", resumed)
                combined.wait(session, f"RESUMED_ACK:{pid}", resumed)
                self.assertFalse(session.lifecycle.main_stopped)
                self.assertIn(f"FOREGROUND_VERIFIED:{pid}", session.events[resumed:])
                self.assertEqual(os.tcgetpgrp(session.master_fd), pid)
                self.assertIsNone(session.lifecycle.main_exit)
                os.kill(pid, signal.SIGUSR2)
                combined.wait(session, "LIFETIME_ACTIVE:", resumed)
                os.kill(supervisor, signal.SIGUSR2)
                self.finish(session, 17)

    def test_signal_exit_is_not_numeric_shell_exit(self):
        for choice in real_shells():
            for status in (130, 131, 148, -signal.SIGINT, -signal.SIGQUIT):
                with self.subTest(shell=choice.kind, status=status), tempfile.TemporaryDirectory() as directory, \
                        controlled(choice.executable, directory) as (session, *_):
                    if status < 0:
                        session.dispatch_managed("signal", [sys.executable,
                            str(Path(combined.__file__).resolve()), "--blocking-child"])
                        pid = self.prepared(session)
                        combined.wait(session, f"SCRIPT_READY:{pid}", 0)
                        session._write_all(b"\x03" if status == -signal.SIGINT else b"\x1c")
                    else:
                        session.dispatch_managed("numeric", f"exit {status}")
                        self.prepared(session)
                    self.finish(session, status)
                    self.assertEqual(session.lifecycle.controller_exit, status if status >= 0 else 128 - status)
                    self.assertFalse(any(e.startswith("MAIN_STOPPED:") for e in session.events))

    def test_literal_argv_and_child_script_exec(self):
        for choice in real_shells():
            for kind in ("argv", "script"):
                with self.subTest(shell=choice.kind, kind=kind), tempfile.TemporaryDirectory() as directory, \
                        controlled(choice.executable, directory) as (session, *_):
                    marker = Path(directory) / "must-not-exist"
                    literal = f"$(touch {marker}); spaced argument"
                    if kind == "argv":
                        payload = [sys.executable, "-c",
                                   "import os,sys; os.write(9, ('LITERAL:'+sys.argv[1]+'\\n').encode())", literal]
                    else:
                        payload = "exec " + sys.executable + " -c 'import os; os.write(9,b\"SCRIPT_EXEC\\n\")'"
                    session.dispatch_managed("literal", payload)
                    self.prepared(session)
                    combined.wait(session, "LITERAL:" + literal if kind == "argv" else "SCRIPT_EXEC", 0)
                    self.finish(session, 0)
                    self.assertFalse(marker.exists())

    def test_failed_exec_has_no_start_or_replacement(self):
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory, \
                    controlled(choice.executable, directory) as (session, *_):
                session.dispatch_managed("missing", [str(Path(directory) / "missing-executable")])
                combined.wait(session, "EXEC_ERROR:", 0)
                combined.wait(session, "RETURN:", 0)
                self.assertTrue(session.lifecycle.child_prepared)
                self.assertFalse(session.lifecycle.exec_ready)
                self.assertFalse(session.lifecycle.experiment_started)
                self.assertIsNone(session.lifecycle.main_exit)
                self.assertTrue(session.lifecycle.unknown)
                self.assertFalse(Path(f"/proc/{session.lifecycle.child_pid}").exists())
                self.assertFalse(Path(f"/proc/{session.lifecycle.supervisor_pid}").exists())
                self.assertFalse(any(e.startswith(("CHILD:", "EXPERIMENT_START:", "LIFETIME_DONE:"))
                                     for e in session.events))
                for action in (session.release_input, session.release_control,
                               lambda: session.dispatch_managed("retry", ":")):
                    with self.assertRaises(UnsafeShellState):
                        action()


if __name__ == "__main__":
    unittest.main()
