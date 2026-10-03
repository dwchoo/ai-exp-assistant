"""C-D58 / Root adjudication p27-cw18-needs-review: the job-caused CW-07 latch.

A wb-handoff held only because of the user's background/suspended jobs is
recoverable: after the user takes the prompt back and cleans the jobs, a
fresh verified handoff whose own jobs probe is empty clears that latch and
is accepted. Latches from unknown/manual residue, lost hooks, failed or
unfinished commands, automation jobs or fail_closed are never cleared, and a
job latch is never cleared by anything but an accepted fresh handoff.
"""
import unittest

from workbench.terminal.shell_g2.prototype import InputBoundary

PID = 4242


def user_boundary() -> InputBoundary:
    boundary = InputBoundary(1)
    boundary.shell_pid = PID
    boundary.observe_event("READY")
    boundary.owner_change("user")
    return boundary


def typed(boundary: InputBoundary, line: bytes) -> None:
    boundary.observe_manual_bytes(line, shell_foreground=True)


def handoff(boundary: InputBoundary, jobs=(), pid: int = PID) -> bool:
    """Managed controller order: HANDOFF, hook check, jobs probe, CONTROL_READY."""
    typed(boundary, b"wb-handoff\r")
    for event in (f"HANDOFF:{pid}", "HOOK_OK:HANDOFF", "JOBS_BEGIN:HANDOFF",
                  *(str(job) for job in jobs), "JOBS_END:HANDOFF", f"CONTROL_READY:{pid}"):
        boundary.observe_event(event)
    return boundary._handoff_ready


def take_back_and_clean(boundary: InputBoundary) -> None:
    # TAKEOVER returns the idle control wait to the prompt: one READY for the
    # held wb-handoff line. The user then kills the job.
    boundary.observe_event("READY")
    typed(boundary, b"kill %1; wait\r")
    boundary.observe_event("READY")


def dispatchable_after_claim(boundary: InputBoundary) -> bool:
    epoch = boundary.owner_change("manager")
    return boundary.can_dispatch(generation=1, owner_epoch=epoch)


class JobLatchClearedByFreshCleanHandoff(unittest.TestCase):
    def test_job_held_then_clean_handoff_is_accepted(self):
        boundary = user_boundary()
        self.assertFalse(handoff(boundary, jobs=(555,)))
        self.assertTrue(boundary.needs_review)
        take_back_and_clean(boundary)
        self.assertTrue(boundary.needs_review)  # cleaning alone never clears it
        self.assertTrue(handoff(boundary))
        self.assertFalse(boundary.needs_review)
        self.assertTrue(dispatchable_after_claim(boundary))

    def test_repeated_job_cycles_each_recover(self):
        boundary = user_boundary()
        for cycle in range(3):
            with self.subTest(cycle=cycle):
                self.assertFalse(handoff(boundary, jobs=(600 + cycle,)))
                take_back_and_clean(boundary)
                self.assertTrue(handoff(boundary))
                self.assertTrue(dispatchable_after_claim(boundary))
                boundary.owner_change("user")
                boundary.observe_event("READY")  # the prompt given back to the user

    def test_handoff_still_seeing_jobs_stays_held(self):
        boundary = user_boundary()
        self.assertFalse(handoff(boundary, jobs=(555,)))
        boundary.observe_event("READY")  # taken back, job not cleaned
        self.assertFalse(handoff(boundary, jobs=(555,)))
        self.assertTrue(boundary.needs_review)
        self.assertFalse(dispatchable_after_claim(boundary))

    def test_unaccepted_handoff_does_not_clear_the_job_latch(self):
        cases = {
            "other_pid": lambda b: handoff(b, pid=PID + 1),
            "residue_line": lambda b: (typed(b, b"x\x7f"), handoff(b))[1],
        }
        for name, attempt in cases.items():
            with self.subTest(case=name):
                boundary = user_boundary()
                self.assertFalse(handoff(boundary, jobs=(555,)))
                take_back_and_clean(boundary)
                self.assertFalse(attempt(boundary))
                self.assertTrue(boundary.needs_review)
                self.assertFalse(dispatchable_after_claim(boundary))

    def test_prompt_probe_job_latch_clears_on_prototype_handoff_path(self):
        # CW-03 prototype order: jobs probe before the hook check, then the
        # prompt READY and its own jobs probe complete the handoff.
        boundary = user_boundary()
        typed(boundary, b"sleep 30 &\r")
        for event in ("READY", "JOBS_BEGIN:READY", "777", "JOBS_END:READY"):
            boundary.observe_event(event)
        self.assertTrue(boundary.needs_review)
        typed(boundary, b"kill %1; wait\r")
        for event in ("READY", "JOBS_BEGIN:READY", "JOBS_END:READY"):
            boundary.observe_event(event)
        typed(boundary, b"wb-handoff\r")
        for event in (f"HANDOFF:{PID}", "JOBS_BEGIN:HANDOFF", "JOBS_END:HANDOFF", "HOOK_OK:HANDOFF",
                      "READY", "JOBS_BEGIN:READY", "JOBS_END:READY"):
            boundary.observe_event(event)
        self.assertTrue(boundary._handoff_ready)
        self.assertTrue(dispatchable_after_claim(boundary))

    def test_prototype_handoff_with_job_back_at_the_prompt_stays_held(self):
        boundary = user_boundary()
        typed(boundary, b"sleep 30 &\r")
        for event in ("READY", "JOBS_BEGIN:READY", "888", "JOBS_END:READY"):
            boundary.observe_event(event)
        typed(boundary, b"wb-handoff\r")
        for event in (f"HANDOFF:{PID}", "JOBS_BEGIN:HANDOFF", "JOBS_END:HANDOFF", "HOOK_OK:HANDOFF",
                      "READY", "JOBS_BEGIN:READY", "888", "JOBS_END:READY"):
            boundary.observe_event(event)
        self.assertFalse(boundary._handoff_ready)
        self.assertTrue(boundary.needs_review)


class OtherLatchesStay(unittest.TestCase):
    OTHER = {
        "fail_closed": lambda b: b.fail_closed(),
        "hook_lost": lambda b: (b.observe_event("HOOK_LOST"), b.observe_event("READY")),
        "failed_command": lambda b: (b.observe_event("START:x"), b.observe_event("DONE:x:1")),
        "unfinished_command": lambda b: (b.observe_event("START:x"), b.observe_event("READY"),
                                         b.observe_event("READY")),
        "direct_latch": lambda b: setattr(b, "needs_review", True),
    }

    def test_unknown_residue_with_jobs_still_held_after_clean_handoff(self):
        for name, latch in self.OTHER.items():
            with self.subTest(cause=name):
                boundary = user_boundary()
                self.assertFalse(handoff(boundary, jobs=(555,)))
                take_back_and_clean(boundary)
                latch(boundary)
                self.assertFalse(handoff(boundary))
                self.assertTrue(boundary.needs_review)
                self.assertFalse(dispatchable_after_claim(boundary))

    def test_other_latch_alone_never_clears_on_clean_handoff(self):
        for name, latch in self.OTHER.items():
            with self.subTest(cause=name):
                boundary = user_boundary()
                latch(boundary)
                boundary.observe_event("READY")
                self.assertFalse(handoff(boundary))
                self.assertTrue(boundary.needs_review)

    def test_sticky_target_unknown_residue_stays(self):
        boundary = user_boundary()
        self.assertFalse(handoff(boundary, jobs=(555,)))
        take_back_and_clean(boundary)
        boundary.observe_user_bytes(b"\x1b[A\r")
        boundary.observe_event("READY")
        self.assertFalse(handoff(boundary))
        self.assertTrue(boundary.uncertain)
        self.assertFalse(dispatchable_after_claim(boundary))

    def test_automation_job_probe_is_not_user_job_latch(self):
        # Jobs left by a manager-dispatched command (its own probe, or any
        # probe while the manager owns input) are not the user's to clean.
        for probe, owner in (("JOBS_END:background", "manager"), ("JOBS_END:READY", "manager"),
                             ("JOBS_END:background", "user")):
            with self.subTest(probe=probe, owner=owner):
                boundary = InputBoundary(1)
                boundary.shell_pid = PID
                boundary.observe_event("READY")
                if owner == "user":
                    boundary.owner_change("user")
                for event in (f"JOBS_BEGIN:{probe.partition(':')[2]}", "999", probe):
                    boundary.observe_event(event)
                self.assertTrue(boundary.needs_review)
                boundary.owner_change("user")
                boundary.observe_event("READY")
                self.assertFalse(handoff(boundary))
                self.assertTrue(boundary.needs_review)

    def test_setting_false_resets_both_sources(self):
        boundary = user_boundary()
        handoff(boundary, jobs=(1,))
        boundary.fail_closed()
        boundary.needs_review = False
        self.assertFalse(boundary.needs_review)


if __name__ == "__main__":
    unittest.main()
