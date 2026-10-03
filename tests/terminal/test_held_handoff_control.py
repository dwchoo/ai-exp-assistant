"""C-D58 (CW-18 U4): a held wb-handoff never strands the user.

While a handoff is held (user background/suspended job, unknown line residue)
automatic dispatch and manager return stay held, but the user can take the
parent prompt back from the idle control wait, confirm it, clean up and run
wb-handoff again. One TAKEOVER per held episode: never a double send, and a
stale takeover request never releases a later clean handoff by itself.
A job seen by a handoff sets the CW-07 needs_review latch; per the Root
adjudication (p27-cw18-needs-review) only that job-caused latch is cleared by
a fresh verified wb-handoff whose jobs probe is empty, so after cleaning up
the handoff is accepted. Unknown/manual residue latches stay.
"""
from pathlib import Path
import tempfile
import time
import unittest
from uuid import uuid4

from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState
from workbench.terminal.shell_persistent.adapter import PersistentShell

SHELLS = (ShellChoice("bash", "/usr/bin/bash"), ShellChoice("sh", "/usr/bin/dash"))


def until(shell, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = shell.poll(0.03)
        if predicate(state):
            return state
    raise AssertionError(f"state not reached: {state['parent_mode']} {state['held_reasons']}")


def stays(shell, predicate, seconds=0.6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = shell.poll(0.03)
        if not predicate(state):
            raise AssertionError(f"state left: {state['parent_mode']} {state['held_reasons']}")
    return state


def children(shell, name):
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            if int(fields[3]) == shell.parent_pid and (entry / "comm").read_text().strip() == name:
                found.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            pass
    return found


def ports(shell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "d" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


def acks(shell):
    return sum(1 for event in shell._transport.events if event.startswith("TAKEOVER_ACK:"))


class HeldHandoffUserControlTests(unittest.TestCase):
    def open(self, choice):
        directory = tempfile.mkdtemp(prefix="cw18-u4-", dir="/tmp")
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "TERM": "xterm", "HOME": directory},
                                choice=choice)
        self.addCleanup(shell.close)
        return shell, Path(directory)

    def assert_automation_held(self, shell):
        control, automation = ports(shell)
        with self.assertRaises(UnsafeShellState):
            shell.submit(control, ":", automation)
        with self.assertRaises(UnsafeShellState):
            shell.claim_manager()

    def hold_with_job(self, shell):
        shell.send_user(b"sleep 60 &\n")
        until(shell, lambda s: children(shell, "sleep") and s["parent_mode"] == "manual_prompt")
        shell.send_user(b"wb-handoff\n")
        until(shell, lambda s: "manual_jobs" in s["held_reasons"] and s["parent_mode"] == "control_wait")
        self.assertIn("control wait", shell.manual_input_hold())
        self.assert_automation_held(shell)

    def take_back_and_clean(self, shell):
        before = acks(shell)
        requested = shell.request_takeover()
        self.assertTrue(requested["takeover_requested"])
        until(shell, lambda s: s["parent_mode"] == "manual_prompt")
        self.assertEqual(acks(shell), before + 1)
        self.assertEqual(shell.confirm_takeover()["parent_mode"], "manual_prompt")
        self.assertEqual(shell.snapshot()["input_owner"], "user")
        self.assertIsNone(shell.manual_input_hold())
        self.assert_automation_held(shell)  # takeover never clears the hold
        shell.send_user(b"kill %1; wait\n")
        until(shell, lambda s: not children(shell, "sleep") and s["parent_mode"] == "manual_prompt")

    def handoff_cleanly(self, shell):
        shell.send_user(b"wb-handoff\n")
        until(shell, lambda s: s["parent_mode"] == "control_wait" and "manual_jobs" not in s["held_reasons"])
        # No leftover TAKEOVER and no stale request releases a clean handoff.
        stays(shell, lambda s: s["parent_mode"] == "control_wait")
        self.assertEqual(shell.claim_manager()["input_owner"], "manager")

    def hold_with_residue(self, shell):
        # An erase key makes the typed handoff line unknown residue (the
        # shell's line editor still runs wb-handoff), so it is not clean.
        shell.send_user(b"wb-handoffx\x7f\r")
        until(shell, lambda s: s["parent_mode"] == "control_wait")
        self.assertIn("control wait", shell.manual_input_hold())
        self.assert_automation_held(shell)

    def run_and_give_back(self, shell, out):
        control, automation = ports(shell)
        shell.submit(control, f"printf ok > {out}", automation)
        until(shell, lambda s: s["lifecycle"]["input_barrier"])
        shell.release_input()
        until(shell, lambda s: s["lifecycle"]["control_returned"])
        self.assertEqual(out.read_text(), "ok")
        shell.request_takeover()
        until(shell, lambda s: s["parent_mode"] == "manual_prompt" and s["input_owner"] == "user")

    def test_repeated_held_handoff_returns_the_prompt_each_time(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, _ = self.open(choice)
                for _ in range(2):
                    self.hold_with_job(shell)
                    self.take_back_and_clean(shell)
                # Cleaned up: the fresh wb-handoff probes no jobs, so the
                # job-caused latch clears and the handoff is accepted.
                self.handoff_cleanly(shell)
                self.assertEqual(shell.snapshot()["phase"], "not_sent")

    def test_job_clean_retry_cycles_run_automation_each_time(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, directory = self.open(choice)
                for cycle in range(3):
                    self.hold_with_job(shell)
                    self.take_back_and_clean(shell)
                    self.handoff_cleanly(shell)
                    self.run_and_give_back(shell, directory / f"ran{cycle}")

    def test_unknown_residue_latch_stays_after_clean_handoff(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, _ = self.open(choice)
                self.hold_with_job(shell)
                self.take_back_and_clean(shell)
                shell._transport.boundary.fail_closed()  # unknown residue latch
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait" and "manual_jobs" not in s["held_reasons"])
                stays(shell, lambda s: s["parent_mode"] == "control_wait", 0.3)
                self.assert_automation_held(shell)
                self.assertIn("unknown_or_manual_residue", shell.snapshot()["held_reasons"])
                # Still recoverable for the user.
                shell.request_takeover()
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                self.assertEqual(shell.confirm_takeover()["input_owner"], "user")
                self.assertIsNone(shell.manual_input_hold())

    def test_handoff_with_job_left_running_stays_held(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, _ = self.open(choice)
                self.hold_with_job(shell)
                shell.request_takeover()
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                shell.confirm_takeover()
                shell.send_user(b"wb-handoff\n")  # the job was not cleaned
                until(shell, lambda s: s["parent_mode"] == "control_wait" and "manual_jobs" in s["held_reasons"])
                self.assert_automation_held(shell)
                shell.request_takeover()
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                shell.send_user(b"kill %1; wait\n")
                until(shell, lambda s: not children(shell, "sleep") and s["parent_mode"] == "manual_prompt")
                self.handoff_cleanly(shell)

    def test_repeated_residue_hold_then_clean_handoff(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, directory = self.open(choice)
                for index in range(2):
                    self.hold_with_residue(shell)
                    shell.request_takeover()
                    until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                    self.assertEqual(shell.confirm_takeover()["input_owner"], "user")
                    marker = directory / f"typed{index}"
                    shell.send_user(f"printf ok > {marker}\n".encode())
                    until(shell, lambda s: marker.exists() and s["phase"] != "unknown"
                          and s["parent_mode"] == "manual_prompt")
                self.handoff_cleanly(shell)

    def test_held_handoff_after_a_returned_run_and_give_back(self):
        # U2 path: run returned, HostShellPort gave the shell back through
        # request_takeover; a later held handoff is still recoverable.
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, directory = self.open(choice)
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                shell.claim_manager()
                out = directory / "ran"
                control, automation = ports(shell)
                shell.submit(control, f"printf ok > {out}", automation)
                until(shell, lambda s: s["lifecycle"]["input_barrier"])
                shell.release_input()
                until(shell, lambda s: s["lifecycle"]["control_returned"])
                shell.request_takeover()
                until(shell, lambda s: s["parent_mode"] == "manual_prompt" and s["input_owner"] == "user")
                self.assertEqual(out.read_text(), "ok")
                self.hold_with_residue(shell)
                shell.request_takeover()
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                self.assertEqual(shell.confirm_takeover()["input_owner"], "user")
                until(shell, lambda s: s["phase"] != "unknown")
                self.handoff_cleanly(shell)
                fresh, automation = ports(shell)
                self.assertTrue(shell.submit(fresh, ":", automation)["lifecycle"]["request_id"])
                until(shell, lambda s: s["lifecycle"]["input_barrier"])
                shell.release_input()
                until(shell, lambda s: s["lifecycle"]["control_returned"])

    def test_one_takeover_per_held_episode(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, _ = self.open(choice)
                self.hold_with_job(shell)
                before = acks(shell)
                shell.request_takeover()
                shell.request_takeover()  # before the ack: no second TAKEOVER
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                shell.request_takeover()
                stays(shell, lambda s: s["parent_mode"] == "manual_prompt", 0.3)
                self.assertEqual(acks(shell), before + 1)
                shell.send_user(b"kill %1; wait\n")
                until(shell, lambda s: not children(shell, "sleep") and s["parent_mode"] == "manual_prompt")
                # No TAKEOVER is left in the request pipe: the next handoff
                # stays in control wait instead of returning by itself.
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                stays(shell, lambda s: s["parent_mode"] == "control_wait")
                self.assertEqual(acks(shell), before + 1)


if __name__ == "__main__":
    unittest.main()
