"""Independent Bash/dash lifecycle observations at the control takeover boundary."""

import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workbench.terminal.shell_g2.control_probe import ControlWaitProbe
from workbench.terminal.shell_g2.prototype import UnsafeShellState
from tests.gates.g2_shell.test_control_probe import real_shells


class ControlLifecycleTests(unittest.TestCase):
    def enter_wait(self, session: ControlWaitProbe) -> None:
        session.wait_ready()
        session.take_user_control()
        session.send_user(b"wb-handoff\n")
        session.wait_control()

    def recover_manual_prompt(self, session: ControlWaitProbe) -> None:
        recover = getattr(session, "recover_manual_prompt", None)
        self.assertTrue(callable(recover), "no independent manual recovery channel after hold")
        recover()
        session.wait_event("READY", timeout=1)
        self.assertTrue(session.manual_prompt_confirmed)

    def test_foreground_stays_controllable_before_shell_takeover_ack(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "manual-lines"
                with ControlWaitProbe(choice) as session:
                    self.enter_wait(session)
                    session.dispatch_control(
                        "two-lines", f"head -n 2 > {shlex.quote(str(output))}",
                        expected_epoch=session.owner_epoch, generation=session.generation,
                    )
                    session.wait_event("START:two-lines")
                    session.wait_foreground_child()
                    self.assertEqual(session.request_takeover(), "unknown")
                    target = session.confirm_foreground_takeover()
                    self.assertEqual(target.command_id, "two-lines")

                    session.send_confirmed_foreground(b"first\n")
                    session._drain(timeout=0.1)
                    self.assertNotIn(f"TAKEOVER_ACK:{session.pid}", session.events)
                    self.assertFalse(any(e.startswith("DONE:two-lines:") for e in session.events))
                    self.assertEqual(
                        session.confirm_foreground_takeover().process_group,
                        target.process_group,
                    )

                    session.send_confirmed_foreground(b"second\n")
                    self.assertEqual(
                        session.wait_event("EVAL_RETURNED:two-lines:"),
                        "EVAL_RETURNED:two-lines:0",
                    )
                    session.wait_event(f"TAKEOVER_ACK:{session.pid}")
                    if session.delegated:
                        session.wait_event("LOCAL_LIFETIME_DONE:two-lines")
                    else:
                        session.wait_event("LIFETIME_UNKNOWN:two-lines")
                    self.assertFalse(any(e.startswith("DONE:two-lines:") for e in session.events))
                    self.assertEqual(output.read_bytes(), b"first\nsecond\n")
                    with self.assertRaises(UnsafeShellState):
                        session.send_confirmed_foreground(b"late\n")

    def test_foreground_input_spill_never_executes_at_manual_prompt(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "dd-input"
                with ControlWaitProbe(choice) as session:
                    self.enter_wait(session)
                    session.dispatch_control(
                        "six-bytes", f"dd bs=1 count=6 > {shlex.quote(str(output))} 2>/dev/null",
                        expected_epoch=session.owner_epoch, generation=session.generation,
                    )
                    session.wait_event("START:six-bytes")
                    session.wait_foreground_child()
                    self.assertEqual(session.request_takeover(), "unknown")
                    session.confirm_foreground_takeover()
                    session.send_confirmed_foreground(b"first\n__cw_emit SPILL_EXECUTED\n")
                    session.wait_event("EVAL_RETURNED:six-bytes:0")
                    session.wait_event(f"TAKEOVER_ACK:{session.pid}")
                    session.wait_event("READY")
                    deadline = time.monotonic() + 0.5
                    while time.monotonic() < deadline and "SPILL_EXECUTED" not in session.events:
                        session._drain(timeout=0.03)
                    self.assertEqual(output.read_bytes(), b"first\n")
                    self.assertNotIn("SPILL_EXECUTED", session.events)

    def test_control_eof_cannot_release_queued_foreground_input(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "dd-input"
                with ControlWaitProbe(choice) as session:
                    self.enter_wait(session)
                    session.dispatch_control(
                        "eof-six", f"dd bs=1 count=6 > {shlex.quote(str(output))} 2>/dev/null",
                        expected_epoch=session.owner_epoch, generation=session.generation,
                    )
                    session.wait_event("START:eof-six")
                    session.wait_foreground_child()
                    self.assertEqual(session.request_takeover(), "unknown")
                    session.confirm_foreground_takeover()
                    session.send_confirmed_foreground(b"firs")
                    os.close(session._request_fd)
                    session.send_confirmed_foreground(b"t\n__cw_emit EOF_SPILL_EXECUTED\n")
                    deadline = time.monotonic() + 0.5
                    while time.monotonic() < deadline:
                        try:
                            session._drain(timeout=0.03)
                        except OSError:
                            # A queued takeover write may fail after the FD 8 fault.
                            pass
                        if "EOF_SPILL_EXECUTED" in session.events:
                            break
                    self.assertEqual(output.read_bytes(), b"first\n")
                    self.assertNotIn("EOF_SPILL_EXECUTED", session.events)
                    if "READY" in session.events[session.events.index(f"HANDOFF:{session.pid}") + 1:]:
                        self.assertTrue(session.boundary.needs_review)

    def test_ctrl_c_at_control_wait_cannot_silently_restore_prompt(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                before_interrupt = len(session.events)
                os.write(session.master_fd, b"__cw_emit INTERRUPT_SPILL_EXECUTED\n")
                os.write(session.master_fd, b"\x03")
                deadline = time.monotonic() + 0.4
                while time.monotonic() < deadline:
                    session._drain(timeout=0.03)
                later_events = session.events[before_interrupt:]
                self.assertNotIn("INTERRUPT_SPILL_EXECUTED", later_events)
                if "READY" in later_events:
                    self.assertTrue(
                        session.boundary.needs_review,
                        f"Ctrl-C returned to prompt without unknown state: {later_events}",
                    )
                    self.assertFalse(session.manual_prompt_confirmed)

    def test_fd8_eof_after_foreground_wait_can_recover_same_shell(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "dd-input"
                with ControlWaitProbe(choice) as session:
                    session.wait_ready()
                    session.take_user_control()
                    session.send_user(f"cd {shlex.quote(directory)}\n".encode())
                    session.wait_ready()
                    session.send_user(b"export CW03_RECOVERY_VALUE=kept\n")
                    session.wait_ready()
                    session.send_user(b"wb-handoff\n")
                    session.wait_control()
                    original_pid = session.pid

                    session.dispatch_control(
                        "eof-recover", f"dd bs=1 count=6 > {shlex.quote(str(output))} 2>/dev/null",
                        expected_epoch=session.owner_epoch, generation=session.generation,
                    )
                    session.wait_event("START:eof-recover")
                    session.wait_foreground_child()
                    self.assertEqual(session.request_takeover(), "unknown")
                    session.confirm_foreground_takeover()
                    session.send_confirmed_foreground(b"firs")
                    os.close(session._request_fd)
                    session.send_confirmed_foreground(b"t\n__cw_emit RECOVERY_SPILL_EXECUTED\n")
                    deadline = time.monotonic() + 1
                    while "CONTROL_STOPPED" not in session.events and time.monotonic() < deadline:
                        try:
                            session._drain(timeout=0.03)
                        except OSError:
                            # The already queued takeover may hit the closed FD 8 writer.
                            pass
                    self.assertIn("CONTROL_STOPPED", session.events)
                    self.assertIn("EVAL_RETURNED:eof-recover:0", session.events)
                    self.assertEqual(output.read_bytes(), b"first\n")
                    self.assertEqual(session.pid, original_pid)
                    self.assertEqual(os.readlink(f"/proc/{original_pid}/cwd"), directory)
                    self.assertNotIn("READY", session.events[session.events.index("CONTROL_STOPPED"):])
                    self.assertNotIn("RECOVERY_SPILL_EXECUTED", session.events)
                    self.assertFalse(any(e.startswith("DONE:eof-recover:") for e in session.events))

                    self.recover_manual_prompt(session)
                    session.send_user(b"__cw_emit RECOVERY_STATE:$$:$PWD:$CW03_RECOVERY_VALUE\n")
                    self.assertEqual(
                        session.wait_event("RECOVERY_STATE:"),
                        f"RECOVERY_STATE:{original_pid}:{directory}:kept",
                    )
                    self.assertNotIn("RECOVERY_SPILL_EXECUTED", session.events)
                    self.assertEqual(session.events.count("ACCEPT:eof-recover"), 1)

    def test_ctrl_c_hold_can_recover_same_shell_without_queued_input(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory:
                with ControlWaitProbe(choice) as session:
                    session.wait_ready()
                    session.take_user_control()
                    session.send_user(f"cd {shlex.quote(directory)}\n".encode())
                    session.wait_ready()
                    session.send_user(b"export CW03_RECOVERY_VALUE=kept\n")
                    session.wait_ready()
                    session.send_user(b"wb-handoff\n")
                    session.wait_control()
                    original_pid = session.pid

                    os.write(session.master_fd, b"__cw_emit INTERRUPT_RECOVERY_SPILL\n")
                    os.write(session.master_fd, b"\x03")
                    session.wait_event("CONTROL_STOPPED", timeout=1)
                    self.assertIn("CONTROL_INTERRUPTED", session.events)
                    self.assertEqual(os.readlink(f"/proc/{original_pid}/cwd"), directory)
                    self.assertNotIn("INTERRUPT_RECOVERY_SPILL", session.events)
                    self.assertNotIn("READY", session.events[session.events.index("CONTROL_STOPPED"):])

                    self.recover_manual_prompt(session)
                    session.send_user(b"__cw_emit RECOVERY_STATE:$$:$PWD:$CW03_RECOVERY_VALUE\n")
                    self.assertEqual(
                        session.wait_event("RECOVERY_STATE:"),
                        f"RECOVERY_STATE:{original_pid}:{directory}:kept",
                    )
                    self.assertNotIn("INTERRUPT_RECOVERY_SPILL", session.events)

    def test_stale_release_cannot_open_manual_prompt(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                os.close(session._request_fd)
                session.wait_event("CONTROL_STOPPED")
                epoch = session._recovery_epoch
                self.assertIsNotNone(epoch)

                # An older epoch with this session's token cannot release the hold.
                os.write(
                    session._recovery_fd,
                    f"RELEASE:{epoch - 1}:{session._recovery_token}\n".encode(),
                )
                deadline = time.monotonic() + 0.2
                while time.monotonic() < deadline:
                    session._drain(timeout=0.02)
                self.assertFalse(session.manual_prompt_confirmed)
                self.assertNotIn(f"RECOVERY_ACK:{epoch}:{session.pid}", session.events)
                self.assertNotIn("READY", session.events[session.events.index("CONTROL_STOPPED"):])
                with self.assertRaises(UnsafeShellState):
                    session.send_user(b"__cw_emit STALE_RELEASE_EXECUTED\n")
                self.assertNotIn("STALE_RELEASE_EXECUTED", session.events)

                self.recover_manual_prompt(session)

    def test_lost_recovery_ack_does_not_confirm_manual_prompt(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                os.close(session._request_fd)
                session.wait_event("CONTROL_STOPPED")
                drain = session._drain
                calls = 0

                def lose_ack_after_release(timeout: float) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        # The release was written to FD 7; now lose FD 9
                        # before the controller can observe its ACK.
                        os.close(session._control_fd)
                    drain(timeout)

                with patch.object(session, "_drain", side_effect=lose_ack_after_release):
                    with self.assertRaises((OSError, TimeoutError, UnsafeShellState)):
                        session.recover_manual_prompt(timeout=0.3)
                self.assertTrue(session._recovery_requested)
                self.assertFalse(session.manual_prompt_confirmed)
                self.assertTrue(session.boundary.needs_review)
                with self.assertRaises((OSError, UnsafeShellState)):
                    session.send_user(b"__cw_emit LOST_ACK_EXECUTED\n")
                self.assertNotIn("LOST_ACK_EXECUTED", session.events)
                self.assertFalse(any(event.startswith("DONE:") for event in session.events))

    def test_legacy_direct_eval_ctrl_c_negative_control(self) -> None:
        """Retain the rejected parent-eval candidate's Bash/dash counterexamples."""
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                session.dispatch_control(
                    "interrupt-run", "sleep 30; __cw_emit RUN_TAIL_EXECUTED",
                    expected_epoch=session.owner_epoch, generation=session.generation,
                )
                session.wait_event("START:interrupt-run")
                session.wait_foreground_child()
                os.write(session.master_fd, b"\x03")
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and "CONTROL_STOPPED" not in session.events:
                    session._drain(timeout=0.03)
                if choice.kind == "bash":
                    self.assertIn("READY", session.events[session.events.index("START:interrupt-run"):])
                else:
                    self.assertIn("CONTROL_STOPPED", session.events)
                    self.assertIn("RUN_TAIL_EXECUTED", session.events)
                self.assertFalse(any(e.startswith("DONE:interrupt-run:") for e in session.events))
                self.assertFalse(session.manual_prompt_confirmed)
                with self.assertRaises(UnsafeShellState):
                    session.dispatch_control(
                        "after-interrupt", ":", expected_epoch=session.owner_epoch,
                        generation=session.generation,
                    )

    def test_legacy_direct_eval_return_negative_control_and_exec_hold(self) -> None:
        """Parent eval's return leaks input; its exec hold is limited old evidence."""
        for choice in real_shells():
            for case, script in (("return", "return"), ("exec", "exec sleep 0.2")):
                with self.subTest(shell=choice.kind, case=case), ControlWaitProbe(choice) as session:
                    self.enter_wait(session)
                    session.dispatch_control(
                        case, script, expected_epoch=session.owner_epoch,
                        generation=session.generation,
                    )
                    session.wait_event(f"START:{case}")
                    os.write(session.master_fd, b"__cw_emit LOOP_ESCAPE_EXECUTED\n")
                    start = session.events.index(f"START:{case}")
                    deadline = time.monotonic() + 0.5
                    while time.monotonic() < deadline:
                        session._drain(timeout=0.03)
                    later = session.events[start + 1:]
                    if case == "return":
                        self.assertIn("LOOP_ESCAPE_EXECUTED", later)
                        self.assertIn("READY", later)
                    else:
                        self.assertNotIn("LOOP_ESCAPE_EXECUTED", later)
                        self.assertNotIn("READY", later)
                    self.assertFalse(any(e.startswith(f"DONE:{case}:") for e in later))
                    self.assertFalse(session.manual_prompt_confirmed)
                    with self.assertRaises(UnsafeShellState):
                        session.dispatch_control(
                            "after-exit", ":", expected_epoch=session.owner_epoch,
                            generation=session.generation,
                        )

    def test_background_work_survives_takeover_without_done(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "background-result"
                script = f"(sleep 0.4; printf finished > {shlex.quote(str(output))}) &"
                with ControlWaitProbe(choice) as session:
                    self.enter_wait(session)
                    session.dispatch_control(
                        "background-work", script,
                        expected_epoch=session.owner_epoch, generation=session.generation,
                    )
                    session.wait_event("START:background-work")
                    self.assertEqual(
                        session.wait_event("ACTIVE_JOBS:background-work"),
                        "ACTIVE_JOBS:background-work",
                    )
                    session.wait_event("EVAL_RETURNED:background-work:0")
                    self.assertEqual(session.request_takeover(), "unknown")
                    session.wait_event(f"TAKEOVER_ACK:{session.pid}")
                    self.assertFalse(any(e.startswith("DONE:background-work:") for e in session.events))
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        if output.exists() and output.read_text() == "finished":
                            break
                        session._drain(timeout=0.03)
                    self.assertEqual(output.read_text(), "finished")
                    if session.delegated:
                        session.wait_event("LOCAL_LIFETIME_DONE:background-work")
                    else:
                        session.wait_event("LIFETIME_UNKNOWN:background-work")
                    self.assertFalse(any(e.startswith("DONE:background-work:") for e in session.events))

    def test_reparented_daemon_is_not_reported_done_while_alive(self) -> None:
        if shutil.which("setsid") is None:
            self.skipTest("setsid unavailable")
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory:
                pid_file = Path(directory) / "escaped-pid"
                daemon_pid = None
                try:
                    with ControlWaitProbe(choice) as session:
                        self.enter_wait(session)
                        script = (
                            "setsid sh -c 'sleep 1.2 & echo $! > "
                            f"{shlex.quote(str(pid_file))}' >/dev/null 2>&1 & "
                            f"while [ ! -s {shlex.quote(str(pid_file))} ]; "
                            "do sleep 0.01; done; wait"
                        )
                        session.dispatch_control(
                            "escaped", script,
                            expected_epoch=session.owner_epoch, generation=session.generation,
                        )
                        session.wait_event("RETURN:escaped:")
                        self.assertTrue(pid_file.exists())
                        daemon_pid = int(pid_file.read_text().strip())
                        with open(f"/proc/{daemon_pid}/status", encoding="ascii") as stream:
                            status = stream.read()
                        self.assertNotIn("State:\tZ", status)
                        self.assertNotEqual(os.getsid(daemon_pid), session.pid)
                        self.assertFalse(
                            any(e.startswith("DONE:escaped:") for e in session.events),
                            f"escaped PID {daemon_pid} remains alive; events={session.events}",
                        )
                        session.wait_control()
                        if session.delegated:
                            self.assertTrue(session._populated(session.run_cgroup))
                            session.wait_event("LOCAL_WORK_ACTIVE:escaped")
                            session.wait_event("LOCAL_LIFETIME_DONE:escaped", timeout=2)
                            self.assertFalse(session._populated(session.run_cgroup))
                        else:
                            session.wait_event("LIFETIME_UNKNOWN:escaped")
                        self.assertFalse(
                            any(e.startswith("DONE:escaped:") for e in session.events),
                            f"async work must not become DONE; events={session.events}",
                        )
                finally:
                    if daemon_pid is not None:
                        try:
                            os.kill(daemon_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
