"""Independent real-shell checks for CW-03 control transport boundaries."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workbench.terminal.shell_g2.control_probe import CleanupIncomplete, ControlWaitProbe
from workbench.terminal.shell_g2.prototype import UnsafeShellState
from tests.gates.g2_shell.test_control_probe import real_shells


class ControlBoundaryTests(unittest.TestCase):
    def enter_wait(self, session: ControlWaitProbe) -> None:
        session.wait_ready()
        session.take_user_control()
        session.send_user(b"wb-handoff\n")
        session.wait_control()

    def test_stale_identity_is_rejected_but_current_request_runs(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                epoch = session.owner_epoch
                generation = session.generation
                for command_id, stale_epoch, stale_generation in (
                    ("old-owner", epoch - 1, generation),
                    ("old-shell", epoch, generation - 1),
                ):
                    with self.assertRaises(UnsafeShellState):
                        session.dispatch_control(
                            command_id, ":", expected_epoch=stale_epoch,
                            generation=stale_generation,
                        )
                    self.assertNotIn(f"ACCEPT:{command_id}", session.events)

                session.dispatch_control(
                    "current", ":", expected_epoch=epoch, generation=generation,
                )
                self.assertEqual(session.wait_event("ACCEPT:current"), "ACCEPT:current")
                self.assertEqual(
                    session.wait_event("EVAL_RETURNED:current:"),
                    "EVAL_RETURNED:current:0",
                )
                session.wait_control()
                if session.delegated:
                    session.wait_event("LOCAL_LIFETIME_DONE:current")
                else:
                    session.wait_event("LIFETIME_UNKNOWN:current")
                self.assertFalse(any(e.startswith("DONE:current:") for e in session.events))

    def test_partial_request_cannot_be_completed_by_takeover(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                # Fault injection: the sender has written only part of a RUN line.
                os.write(session._request_fd, b"RUN:partial:__cw_emit PARTIAL_EXECUTED")
                self.assertEqual(session.request_takeover(), "not_sent")
                try:
                    session.wait_event(f"TAKEOVER_ACK:{session.pid}", timeout=0.5)
                except TimeoutError as exc:
                    self.fail(f"{exc}; observed {session.events}")
                self.assertNotIn("ACCEPT:partial", session.events)

    def test_broken_control_pipe_blocks_new_automatic_request(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                os.close(session._request_fd)
                self.assertEqual(session.wait_event("CONTROL_LOST"), "CONTROL_LOST")
                with self.assertRaises((UnsafeShellState, OSError)):
                    session.dispatch_control(
                        "after-loss", ":", expected_epoch=session.owner_epoch,
                        generation=session.generation,
                    )
                self.assertNotIn("ACCEPT:after-loss", session.events)
                with self.assertRaises((UnsafeShellState, OSError)):
                    session.dispatch_control(
                        "after-loss", ":", expected_epoch=session.owner_epoch,
                        generation=session.generation,
                    )

    def test_failed_initial_cgroup_move_keeps_rollback_residual_visible(self) -> None:
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory:
                # Keep the real shell in its original cgroup; inject only a fake path.
                with patch.object(ControlWaitProbe, "_setup_manual_cgroup"):
                    session = ControlWaitProbe(choice)
                residual = Path(directory) / f"wb-cw03-manual-{os.getpid()}-{session.pid}"
                try:
                    with (
                        patch.object(session, "_pid_cgroup", return_value=Path(directory)),
                        patch.object(session, "_move_shell", side_effect=OSError("move failed")) as move,
                        patch("workbench.terminal.shell_g2.control_probe.os.access", return_value=True),
                    ):
                        session._setup_manual_cgroup()
                    self.assertEqual(move.call_count, 2)
                    self.assertTrue(residual.exists())
                    try:
                        session.close()
                    except CleanupIncomplete:
                        self.assertIn(residual, session.cleanup_residuals)
                    else:
                        self.assertFalse(residual.exists(), "close silently lost rollback residual")
                finally:
                    session.close()


if __name__ == "__main__":
    unittest.main()
