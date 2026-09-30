"""Independent adversarial checks of the p2.7 InputBoundary latch (p27-input-test-01).

C-AC-26 / root adjudication: line uncertainty holds automatic dispatch and
manager return; it is cleared only by the authenticated control events (a
fresh READY consuming every counted line with nothing left open, or a verified
CONTROL_READY of the same parent), never by other events, output-like strings,
or experiment input under a confirmed takeover.
"""
import unittest

from workbench.terminal.shell_g2.prototype import InputBoundary, UnsafeShellState

PID = 4242
HANDOFF = ("HANDOFF:{pid}", "HOOK_OK:HANDOFF", "JOBS_BEGIN:HANDOFF", "JOBS_END:HANDOFF", "CONTROL_READY:{pid}")


def user_boundary() -> InputBoundary:
    boundary = InputBoundary(1)
    boundary.shell_pid = PID
    boundary.observe_event("READY")
    boundary.owner_change("user")
    return boundary


def handoff(boundary: InputBoundary, pid: int = PID) -> bool:
    for event in HANDOFF:
        boundary.observe_event(event.format(pid=pid))
    return boundary._handoff_ready


def dispatchable_after_claim(boundary: InputBoundary) -> bool:
    epoch = boundary.owner_change("manager")
    return boundary.can_dispatch(generation=1, owner_epoch=epoch)


class LatchIsNotClearedByOtherEvents(unittest.TestCase):
    NON_CLEARING = ("START:x", "DONE:x:0", "JOBS_BEGIN:READY", "JOBS_END:READY", "HOOK_OK:HANDOFF",
                    f"WAIT:{PID}:/tmp:persistent", "ACCEPT", "RETURN:0", "READY ", " READY", "READY:1",
                    "$ ", "CONTROL_READY:1", f"CONTROL_READY:{PID}x", "PRIOR_HOOK", "")

    def test_uncertain_pending_line_survives_every_non_ready_event(self):
        for data in (b"echo a\x7f", b"echo \x1b[A", b"x\x15", b"x\x17", b"\t", b"\x1a", b"\x1c"):
            boundary = user_boundary()
            boundary.observe_manual_bytes(data, shell_foreground=True)
            for event in self.NON_CLEARING:
                with self.subTest(data=data, event=event):
                    boundary.observe_event(event)
                    self.assertTrue(boundary.uncertain)
            boundary.observe_event("READY")        # the line is still open: no consumption
            self.assertTrue(boundary.uncertain)
            self.assertFalse(dispatchable_after_claim(boundary))

    def test_session_latch_from_target_unknown_bytes_never_clears(self):
        boundary = user_boundary()
        boundary.observe_user_bytes(b"\x1b[A\r")
        for _ in range(3):
            boundary.observe_event("READY")
        boundary.observe_manual_bytes(b"wb-handoff\r", shell_foreground=True)
        boundary.observe_event("READY")
        self.assertTrue(boundary.uncertain)
        self.assertFalse(handoff(boundary))
        self.assertFalse(dispatchable_after_claim(boundary))


class HandoffIsNotTrustedFromUncertainInput(unittest.TestCase):
    def test_typeahead_to_a_program_that_becomes_wb_handoff(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"sleep 1\r", shell_foreground=True)
        boundary.observe_manual_bytes(b"wb-handoff\n", shell_foreground=False)
        boundary.observe_event("READY")            # sleep's prompt; shell then reads the typeahead
        self.assertFalse(handoff(boundary))
        self.assertFalse(dispatchable_after_claim(boundary))

    def test_handoff_typed_before_the_interrupt_prompt_arrived(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"\x03", shell_foreground=True)
        boundary.observe_manual_bytes(b"wb-handoff\r", shell_foreground=True)
        boundary.observe_event("READY")
        self.assertFalse(handoff(boundary))

    def test_control_ready_of_another_pid_or_without_handoff_is_ignored(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"wb-handoff\r", shell_foreground=True)
        self.assertFalse(handoff(boundary, pid=PID + 1))
        self.assertFalse(dispatchable_after_claim(boundary))
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"true\r", shell_foreground=True)
        boundary.observe_event(f"CONTROL_READY:{PID}")
        self.assertFalse(boundary._handoff_ready)

    def test_extra_typed_line_after_handoff_breaks_the_single_line_rule(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"wb-handoff\rtouch x\r", shell_foreground=True)
        self.assertFalse(handoff(boundary))

    def test_clean_handoff_after_consumed_edits_is_accepted(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"ls\x1b[D\x7f\x15echo hi\r", shell_foreground=True)
        boundary.observe_event("READY")
        self.assertFalse(boundary.uncertain)
        boundary.observe_manual_bytes(b"wb-handoff\r", shell_foreground=True)
        self.assertTrue(handoff(boundary))
        self.assertTrue(dispatchable_after_claim(boundary))

    def test_manual_bytes_require_the_user_owner(self):
        boundary = InputBoundary(1)
        for call in (lambda: boundary.observe_manual_bytes(b"x", shell_foreground=True),
                     lambda: boundary.observe_manual_bytes(b"x", shell_foreground=False),
                     lambda: boundary.observe_run_bytes(b"x")):
            with self.assertRaises(UnsafeShellState):
                call()


class ExperimentInputUnderConfirmedTakeover(unittest.TestCase):
    def test_run_bytes_do_not_touch_line_state_but_void_a_prior_handoff(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"wb-handoff\r", shell_foreground=True)
        self.assertTrue(handoff(boundary))
        before = (boundary.uncertain, boundary.submitted_lines, bytes(boundary.pending_line))
        boundary.observe_run_bytes(b"a\x1b[D\x7f\x15\x03\x04\x1a\n")
        self.assertEqual((boundary.uncertain, boundary.submitted_lines, bytes(boundary.pending_line)), before)
        self.assertFalse(boundary._handoff_ready)
        self.assertFalse(dispatchable_after_claim(boundary))

    def test_stale_owner_epoch_cannot_dispatch(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"wb-handoff\r", shell_foreground=True)
        self.assertTrue(handoff(boundary))
        epoch = boundary.owner_change("manager")
        self.assertFalse(boundary.can_dispatch(generation=1, owner_epoch=epoch - 1))
        self.assertFalse(boundary.can_dispatch(generation=2, owner_epoch=epoch))
        self.assertTrue(boundary.can_dispatch(generation=1, owner_epoch=epoch))


if __name__ == "__main__":
    unittest.main()
