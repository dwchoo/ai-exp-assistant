"""Admission and negative controls for the input/jobs feasibility unit."""
from __future__ import annotations

import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.gates.g2_shell import live_input_jobs_probe as probe


class InputJobsPredicatesTest(unittest.TestCase):
    def control_boundary(self, data: bytes = b"wb-handoff\n"):
        boundary = probe.prototype.InputBoundary(1)
        boundary.shell_pid = 123
        boundary.observe_event("READY")
        boundary.owner_change("user")
        boundary.observe_user_bytes(data)
        boundary.observe_event("HANDOFF:123")
        boundary.observe_event("HOOK_OK:HANDOFF")
        return boundary

    def test_control_ready_requires_completed_job_probe_and_current_parent(self) -> None:
        boundary = self.control_boundary()
        boundary.observe_event("CONTROL_READY:123")
        self.assertFalse(boundary.ready)
        boundary.observe_event("JOBS_BEGIN:HANDOFF")
        boundary.observe_event("JOBS_END:HANDOFF")
        boundary.observe_event("CONTROL_READY:122")
        self.assertFalse(boundary.ready)
        boundary.observe_event("CONTROL_READY:123")
        epoch = boundary.owner_change("manager")
        self.assertTrue(boundary.can_dispatch(generation=1, owner_epoch=epoch))

    def test_control_ready_cannot_clear_jobs_or_pasted_and_partial_input(self) -> None:
        for data, job in ((b"wb-handoff\n", "456"),
                          (b"wb-handoff\necho stale\n", None),
                          (b"wb-handoff\necho pending", None),
                          (b"wb-handoff\n\x1b[0n", None)):
            with self.subTest(data=data, job=job):
                boundary = self.control_boundary(data)
                boundary.observe_event("JOBS_BEGIN:HANDOFF")
                if job:
                    boundary.observe_event(job)
                boundary.observe_event("JOBS_END:HANDOFF")
                boundary.observe_event("CONTROL_READY:123")
                epoch = boundary.owner_change("manager")
                self.assertFalse(boundary.can_dispatch(generation=1, owner_epoch=epoch))

    def test_invalid_or_incomplete_completion_cannot_clear_the_active_command(self) -> None:
        prefix = ("START", "LIFETIME_DONE:INTERRUPTS=0", "INPUT_BARRIER", "INPUT_RELEASED")
        sequences = (
            prefix + ("RETURN:7", "WAIT:123:/tmp:kept"),
            prefix + ("RETURN:bad", "WAIT:123:/tmp:kept"),
            prefix + ("RETURN:0", "WAIT:122:/tmp:kept"),
            prefix + ("RETURN:0",),
            ("START", "INPUT_BARRIER", "INPUT_RELEASED", "RETURN:0", "WAIT:123:/tmp:kept"),
            ("START", "LIFETIME_DONE:bad", "INPUT_BARRIER", "INPUT_RELEASED",
             "RETURN:0", "WAIT:123:/tmp:kept"),
        )
        for events in sequences:
            with self.subTest(events=events):
                boundary = probe.prototype.InputBoundary(1)
                boundary.active_command = "input-jobs"
                session = SimpleNamespace(boundary=boundary, pid=123, _completion_index=0,
                                          _completion_events=probe.InputJobsSession._completion_events)
                for event in events:
                    boundary.observe_event(event)
                    probe.InputJobsSession._on_control_event(session, event)
                self.assertEqual(boundary.active_command, "input-jobs")
                boundary.observe_event("READY")
                self.assertTrue(boundary.needs_review)
                self.assertFalse(boundary.can_dispatch(generation=1, owner_epoch=1))

    def test_missing_or_reordered_completion_stays_closed_after_fresh_handoff(self) -> None:
        complete = ("START", "LIFETIME_DONE:INTERRUPTS=0", "INPUT_BARRIER",
                    "INPUT_RELEASED", "RETURN:0", "WAIT:123:/tmp:kept")
        sequences = [complete[:index] + complete[index + 1:]
                     for index in range(len(complete))]
        for index in range(len(complete) - 1):
            reordered = list(complete)
            reordered[index], reordered[index + 1] = reordered[index + 1], reordered[index]
            sequences.append(tuple(reordered))
        for events in sequences:
            with self.subTest(events=events):
                boundary = probe.prototype.InputBoundary(1)
                boundary.shell_pid = 123
                boundary.active_command = "input-jobs"
                session = SimpleNamespace(boundary=boundary, pid=123, _completion_index=0,
                                          _completion_events=probe.InputJobsSession._completion_events)
                for event in events:
                    boundary.observe_event(event)
                    probe.InputJobsSession._on_control_event(session, event)
                self.assertEqual(boundary.active_command, "input-jobs")
                boundary.observe_event("READY")
                boundary.owner_change("user")
                boundary.observe_user_bytes(b"wb-handoff\n")
                for event in ("HANDOFF:123", "HOOK_OK:HANDOFF", "JOBS_BEGIN:HANDOFF",
                              "JOBS_END:HANDOFF", "CONTROL_READY:123"):
                    boundary.observe_event(event)
                epoch = boundary.owner_change("manager")
                self.assertTrue(boundary.needs_review)
                self.assertFalse(boundary.can_dispatch(generation=1, owner_epoch=epoch))

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("bash") and shutil.which("dash"),
                         "Linux/Bash/dash required")
    def test_all_deny_candidate_fails_normal_admission_control(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                with patch.object(probe.prototype.InputBoundary, "can_dispatch", return_value=False):
                    with self.assertRaises(probe.prototype.UnsafeShellState):
                        probe.case(shell, "clean")

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("bash") and shutil.which("dash"),
                         "Linux/Bash/dash required")
    def test_missing_flush_exposes_the_rejected_handoff_trailing_line(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                with patch.object(probe.combined.boundary, "flush_queued_pty"):
                    with self.assertRaisesRegex(AssertionError, "trailing line escaped"):
                        probe.case(shell, "handoff_trailing")

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("bash") and shutil.which("dash"),
                         "Linux/Bash/dash required")
    def test_second_handoff_and_dispatch_use_the_same_parent_after_input_return(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                result = probe.case(shell, "clean")
                self.assertEqual(result["successful_dispatches"], 2)
                self.assertTrue(result["parent_prompt_and_new_input"])

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("bash") and shutil.which("dash"),
                         "Linux/Bash/dash required")
    def test_second_dispatch_cannot_bypass_a_rejected_guard(self) -> None:
        original = probe.prototype.InputBoundary.can_dispatch
        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                admitted = []

                def reject_second(boundary, *, generation, owner_epoch):
                    allowed = original(boundary, generation=generation, owner_epoch=owner_epoch)
                    if allowed:
                        admitted.append(boundary.shell_pid)
                    return allowed and len(admitted) < 2

                with patch.object(probe.prototype.InputBoundary, "can_dispatch", reject_second):
                    with self.assertRaises(probe.prototype.UnsafeShellState):
                        probe.case(shell, "clean")
                self.assertEqual(len(admitted), 2)
                self.assertEqual(admitted[0], admitted[1])


if __name__ == "__main__":
    unittest.main()
