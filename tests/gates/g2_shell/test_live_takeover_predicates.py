"""Runtime regressions and negative controls for combined takeover evidence."""
import shutil
import sys
import unittest
from unittest.mock import patch

from tests.gates.g2_shell import live_takeover_probe as probe


@unittest.skipUnless(sys.platform == "linux" and shutil.which("bash") and shutil.which("dash"),
                     "Linux/Bash/dash required")
class TakeoverPredicatesTest(unittest.TestCase):
    def test_zero_progress_retry_withholds_input_after_foreground_exit(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for name in ("eagain_race", "zero_race"):
                with self.subTest(shell=shell, name=name):
                    result = probe.case(shell, name)
                    self.assertTrue(result["marker_absent"])
                    self.assertEqual(result["run_writes"], 1)

    def test_missing_retry_revalidation_leaks_after_first_eagain_or_zero_write(self) -> None:
        original = probe.TakeoverSession.send_confirmed_foreground

        def validate_once_then_retry(session, data):
            original(session, b"")
            session.boundary.observe_user_bytes(data)
            session._write_all(data)

        for shell in (shutil.which("bash"), shutil.which("dash")):
            for name in ("eagain_race", "zero_race"):
                with self.subTest(shell=shell, name=name):
                    with patch.object(probe.TakeoverSession, "send_confirmed_foreground",
                                      validate_once_then_retry):
                        result = probe.case(shell, name, partial_negative=True)
                    self.assertFalse(result["marker_absent"])

    def test_general_input_bypass_leaks_without_ack_confirmation(self) -> None:
        original_send = probe.TakeoverSession.send_user
        original_reject = probe.reject_user_without_write
        bypass_writes = []

        def bypass_after_completion(session, data):
            if session.main_returned and not session.manual_prompt_confirmed:
                probe.prototype.ShellProcess.send_user(session, data)
                bypass_writes.append(session.pid)
            else:
                original_send(session, data)

        def expose_real_bypass(session, data):
            if not session.main_returned:
                original_reject(session, data)
                return
            session.send_user(data)
            if "ACK_DROPPED" in session.events:
                since = len(session.events)
                session._write_all(b"__b_emit BYPASS_PROCESSED\n")
                probe.combined.wait(session, "BYPASS_PROCESSED", since)

        for shell in (shutil.which("bash"), shutil.which("dash")):
            for name in ("ack_lost", "ack_delayed"):
                with self.subTest(shell=shell, name=name):
                    before = len(bypass_writes)
                    with patch.object(probe.TakeoverSession, "send_user", bypass_after_completion), \
                         patch.object(probe, "reject_user_without_write", expose_real_bypass):
                        with self.assertRaisesRegex(AssertionError, "stale input escaped"):
                            probe.case(shell, name)
                    self.assertEqual(len(bypass_writes), before + 1)

    def test_partial_write_withholds_tail_after_observed_foreground_exit(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                result = probe.case(shell, "partial_race")
                self.assertTrue(result["marker_absent"])
                self.assertEqual(result["run_writes"], 1)

    def test_missing_per_chunk_revalidation_leaks_tail_at_the_parent_prompt(self) -> None:
        original = probe.TakeoverSession.send_confirmed_foreground

        def validate_only_first_chunk(session, data):
            # Preserve the initial confirmation and first write, but omit the
            # foreground revalidation before the remaining chunk.
            original(session, data[:6])
            session._write_all(data[6:])

        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                with patch.object(probe.TakeoverSession, "send_confirmed_foreground",
                                  validate_only_first_chunk):
                    result = probe.case(shell, "partial_race", partial_negative=True)
                self.assertFalse(result["marker_absent"])
                self.assertTrue(result["manual_prompt_confirmed"])
                self.assertEqual(result["run_writes"], 1)

    def test_lost_and_delayed_ack_do_not_replay_the_request(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for name in ("ack_lost", "ack_delayed"):
                with self.subTest(shell=shell, name=name):
                    result = probe.case(shell, name)
                    self.assertEqual(result["request_state"], "unknown")
                    self.assertEqual(result["takeover_writes"], 1)
                    self.assertEqual(result["manual_prompt_confirmed"], name == "ack_delayed")

    def test_missing_input_flush_exposes_a_stale_marker_at_the_parent_prompt(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                with patch.object(probe.combined.boundary, "flush_queued_pty"):
                    with self.assertRaisesRegex(AssertionError, "stale input escaped"):
                        probe.case(shell, "foreground")

    def test_ready_without_ack_is_rejected_as_a_confirmation(self) -> None:
        original = probe.TakeoverSession._on_control_event

        def wrongly_confirm_ready(session, event):
            original(session, event)
            if event == "READY" and session.takeover_requested:
                session.manual_prompt_confirmed = True

        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                with patch.object(probe.TakeoverSession, "_on_control_event", wrongly_confirm_ready):
                    with self.assertRaises(AssertionError):
                        probe.case(shell, "ack_lost")


if __name__ == "__main__":
    unittest.main()
