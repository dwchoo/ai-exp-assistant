"""F-CW17-INPUT-01 regressions: manual input follows the observed PTY target.

C-AC-08: manual input goes to the confirmed current target and is held only
while that target is unknown. C-AC-26: line-edit uncertainty keeps holding
automatic dispatch and manager return, and only an authenticated control event
(fresh READY / verified CONTROL_READY) clears it, never a timer or output.
"""
import os
from pathlib import Path
import shlex
import tempfile
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

from workbench.backend.panes import ShellPane
from workbench.contracts.ui_v1 import Reason
from workbench.terminal.shell_g2.lifecycle import ManagedLifecycle
from workbench.terminal.shell_g2.prototype import InputBoundary, ShellChoice, UnsafeShellState
from workbench.terminal.shell_persistent.adapter import PersistentShell, _Transport

SHELLS = (ShellChoice("bash", "/usr/bin/bash"), ShellChoice("sh", "/usr/bin/dash"))
HANDOFF_EVENTS = ("HANDOFF:{pid}", "HOOK_OK:HANDOFF", "JOBS_BEGIN:HANDOFF", "JOBS_END:HANDOFF",
                  "CONTROL_READY:{pid}")


def user_boundary(pid=42):
    boundary = InputBoundary(1)
    boundary.shell_pid = pid
    boundary.observe_event("READY")
    boundary.owner_change("user")
    return boundary


def verified_handoff(boundary, pid=42):
    for event in HANDOFF_EVENTS:
        boundary.observe_event(event.format(pid=pid))
    return boundary._handoff_ready


class ManualLineUncertaintyTests(unittest.TestCase):
    def test_edited_line_holds_automation_until_its_fresh_ready(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"printf A\x1b[D\x1b[C", shell_foreground=True)
        self.assertTrue(boundary.uncertain)
        boundary.observe_event("READY")  # no counted line consumed; line still open
        self.assertTrue(boundary.uncertain)
        boundary.observe_manual_bytes(b"\r", shell_foreground=True)
        self.assertTrue(boundary.uncertain)
        boundary.observe_event("READY")
        self.assertFalse(boundary.uncertain)
        self.assertEqual(boundary.submitted_lines, 0)

    def test_editing_keys_then_clean_handoff_is_accepted(self):
        boundary = user_boundary()
        for data in (b"\x1b[A", b"\x15", b"typo\x7f\x7f\r"):
            boundary.observe_manual_bytes(data, shell_foreground=True)
        boundary.observe_event("READY")
        boundary.observe_manual_bytes(b"wb-handoff\r", shell_foreground=True)
        self.assertTrue(verified_handoff(boundary))
        epoch = boundary.owner_change("manager")
        self.assertTrue(boundary.can_dispatch(generation=1, owner_epoch=epoch))

    def test_an_edited_handoff_line_is_never_trusted(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"x\x15wb-handoff\r", shell_foreground=True)
        self.assertFalse(verified_handoff(boundary))
        epoch = boundary.owner_change("manager")
        self.assertFalse(boundary.can_dispatch(generation=1, owner_epoch=epoch))

    def test_ctrl_c_at_idle_prompt_expects_exactly_its_redisplayed_prompt(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"printf NO\x03", shell_foreground=True)
        self.assertEqual((boundary.uncertain, bytes(boundary.pending_line), boundary.submitted_lines),
                         (True, b"", 1))
        boundary.observe_event("READY")
        self.assertFalse(boundary.uncertain)
        # Ctrl-C while a counted line is outstanding (PS2 continuation) is
        # covered by that line's own READY and is not counted twice.
        boundary.observe_manual_bytes(b"echo 'a\r\x03", shell_foreground=True)
        self.assertEqual(boundary.submitted_lines, 1)
        boundary.observe_event("READY")
        self.assertFalse(boundary.uncertain)

    def test_stale_ready_cannot_clear_a_later_interrupt(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"\x03", shell_foreground=True)
        boundary.observe_manual_bytes(b"; evil\x01\x1b\r", shell_foreground=True)
        boundary.observe_event("READY")  # the interrupt's prompt, not the edited line
        self.assertTrue(boundary.uncertain)

    def test_program_input_is_not_a_shell_line_and_resets_on_fresh_prompt(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"cat > out\r", shell_foreground=True)
        boundary.observe_manual_bytes(b"abc\n\x1b[A\x04", shell_foreground=False)
        self.assertTrue(boundary.uncertain)
        self.assertEqual(boundary.submitted_lines, 1)
        boundary.observe_manual_bytes(b"\n\x04", shell_foreground=False)
        boundary.observe_event("READY")
        self.assertFalse(boundary.uncertain)

    def test_unterminated_program_tail_taints_the_next_shell_line(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"sleep 5\r", shell_foreground=True)
        boundary.observe_manual_bytes(b"; evil\x01", shell_foreground=False)
        boundary.observe_event("READY")
        self.assertTrue(boundary.uncertain)
        boundary.observe_manual_bytes(b"wb-handoff\r", shell_foreground=True)
        self.assertFalse(verified_handoff(boundary))

    def test_program_input_behind_counted_handoff_typeahead_keeps_hold(self):
        boundary = user_boundary()
        boundary.observe_manual_bytes(b"sleep 1\rwb-handoff\r", shell_foreground=True)
        boundary.observe_manual_bytes(b"evil\n", shell_foreground=False)
        boundary.observe_event("READY")
        self.assertTrue(boundary.uncertain)
        self.assertFalse(verified_handoff(boundary))

    def test_target_unknown_observation_keeps_the_session_latch(self):
        boundary = user_boundary()
        boundary.observe_user_bytes(b"\x1b[A\r")
        boundary.observe_event("READY")
        self.assertTrue(boundary.uncertain)


class ManualTargetDecisionTests(unittest.TestCase):
    def transport(self, foreground):
        t = Mock(pid=42, boundary=user_boundary(), lifecycle=ManagedLifecycle(42), manual_prompt_confirmed=True,
                 control_wait_seen=False, takeover_requested=False, confirmed_target=None, _closed=False,
                 _parent_start="start", master_fd=-1, events=(), generation=1)
        t._proc.return_value = ["S", str(os.getpid()), "42", "42"] + ["0"] * 15 + ["start"]
        t._foreground_group.return_value = foreground
        t._manual_prompt_is_current.side_effect = lambda: _Transport._manual_prompt_is_current(t)
        t._manual_foreground_group.side_effect = lambda: _Transport._manual_foreground_group(t)
        shell = PersistentShell.__new__(PersistentShell)
        shell._transport, shell._seen, shell._takeover_sent = t, set(), False
        return shell, t

    def test_latched_boundary_does_not_hold_the_prompt_target(self):
        shell, t = self.transport(42)
        t.boundary.uncertain = t.boundary.needs_review = True
        self.assertIsNone(shell.manual_input_hold())
        shell.send_user(b"\x1b[A")
        t.send_manual_parent.assert_called_once_with(b"\x1b[A")

    def test_foreground_outside_the_session_or_unobservable_is_held_with_reason(self):
        for foreground in (None, os.getpgrp()):
            with self.subTest(foreground=foreground):
                shell, t = self.transport(foreground)
                self.assertIn("foreground process group", shell.manual_input_hold())
                with self.assertRaises(UnsafeShellState):
                    shell.send_user(b"x")
                t.send_manual_foreground.assert_not_called()

    def test_control_loss_and_in_flight_request_hold_manual_input(self):
        shell, t = self.transport(42)
        t.boundary.control_lost = True
        self.assertEqual(shell.manual_input_hold(), "shell control channel lost")
        shell, t = self.transport(42)
        t.lifecycle.begin("req")
        self.assertIn("managed request in flight", shell.manual_input_hold())
        with self.assertRaises(UnsafeShellState):
            shell.send_user(b"x")


def until(shell, condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = shell.poll(.02)
        shell.display_bytes()
        if condition(state):
            return state
    raise AssertionError({"timeout": shell.snapshot()})


def children(shell, name):
    found = []
    for pid in shell._transport._descendant_pids():
        try:
            if Path(f"/proc/{pid}/comm").read_text().strip() == name:
                found.append(pid)
        except OSError:
            pass
    return found


def ports(shell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "c" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


class LiveManualInputTests(unittest.TestCase):
    def open(self, choice, directory):
        shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "TERM": "xterm", "HOME": directory},
                                choice=choice)
        self.addCleanup(shell.close)
        return shell

    def assert_automation_held(self, shell):
        control, automation = ports(shell)
        with self.assertRaises(UnsafeShellState):
            shell.submit(control, ":", automation)
        with self.assertRaises(UnsafeShellState):
            shell.claim_manager()

    def test_editing_keys_and_ctrl_c_keep_input_live_and_handoff_recoverable(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="cw07-edit-") as directory:
                shell = self.open(choice, directory)
                marker = Path(directory) / "m"
                shell.send_user(f"printf A >> {marker}".encode())
                shell.send_user(b"\x1b[D")
                shell.send_user(b"\x7f")
                self.assertEqual(shell.snapshot()["phase"], "unknown")
                self.assert_automation_held(shell)
                shell.send_user(b"\x03")  # discard the edited line
                until(shell, lambda s: s["phase"] != "unknown")
                shell.send_user(b"\x1b[A\x15")
                shell.send_user(f"printf B >> {marker}\r".encode())
                until(shell, lambda s: marker.exists() and s["phase"] != "unknown")
                self.assertEqual(marker.read_text(), "B")
                shell.send_user(b"wb-handoff\r")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                self.assertEqual(shell.claim_manager()["input_owner"], "manager")

    def test_user_started_foreground_programs_receive_data_and_control_keys(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="cw07-fg-") as directory:
                shell = self.open(choice, directory)
                out = Path(directory) / "cat-out"
                shell.send_user(f"cat > {shlex.quote(str(out))}\r".encode())
                until(shell, lambda s: s["parent_mode"] == "manual_foreground" and children(shell, "cat"))
                self.assertIsNone(shell.manual_input_hold())
                shell.send_user(b"abc\n")
                until(shell, lambda _: out.exists() and out.read_text() == "abc\n")
                self.assert_automation_held(shell)
                shell.send_user(b"\x04")
                until(shell, lambda s: not children(shell, "cat") and s["parent_mode"] == "manual_prompt")
                shell.send_user(b"sleep 30\r")
                until(shell, lambda _: children(shell, "sleep"))
                shell.send_user(b"\x03")
                state = until(shell, lambda s: not children(shell, "sleep") and s["parent_mode"] == "manual_prompt"
                              and s["phase"] != "unknown")
                self.assertEqual(state["input_owner"], "user")
                shell.send_user(b"wb-handoff\r")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                shell.claim_manager()

    def test_earlier_review_latch_holds_automation_but_not_manual_repair(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="cw07-job-") as directory:
                shell = self.open(choice, directory)
                shell.send_user(b"sleep 60 &\n")
                until(shell, lambda s: children(shell, "sleep") and s["parent_mode"] == "manual_prompt")
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: "manual_jobs" in s["held_reasons"] and s["parent_mode"] == "control_wait")
                self.assertIn("control wait", shell.manual_input_hold())
                with self.assertRaises(UnsafeShellState):
                    shell.claim_manager()
                shell.request_takeover()
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                self.assertTrue(shell._transport.boundary.needs_review)
                shell.send_user(b"kill %1\n")
                until(shell, lambda _: not children(shell, "sleep"))
                self.assert_automation_held(shell)

    def test_editing_after_completed_request_keeps_lifecycle_and_fresh_handoff(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="cw07-post-") as directory:
                shell = self.open(choice, directory)
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                shell.claim_manager()
                control, automation = ports(shell)
                shell.submit(control, ":", automation)
                until(shell, lambda s: s["lifecycle"]["input_barrier"])
                shell.release_input()
                until(shell, lambda s: s["lifecycle"]["control_returned"])
                shell.request_takeover()
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                shell.send_user(b"true\x1b[D\x1b[C\x7fe\r")
                until(shell, lambda s: s["phase"] != "unknown" and s["parent_mode"] == "manual_prompt")
                self.assertEqual(shell.snapshot()["lifecycle"]["unknown"], [])
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                shell.claim_manager()
                fresh, automation = ports(shell)
                self.assertTrue(shell.submit(fresh, ":", automation)["lifecycle"]["request_id"])
                until(shell, lambda s: s["lifecycle"]["input_barrier"])
                shell.release_input()
                until(shell, lambda s: s["lifecycle"]["control_returned"])


    def test_rejected_handoff_returns_input_to_the_reported_prompt(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="cw07-reject-") as directory:
                shell = self.open(choice, directory)
                shell.send_user(b"trap ':' CHLD\n")
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                shell.send_user(b"wb-handoff\n")
                state = until(shell, lambda s: "unsupported_hook_or_trap" in s["held_reasons"]
                              and s["parent_mode"] == "manual_prompt")
                self.assertEqual(state["input_owner"], "user")
                self.assertIsNone(shell.manual_input_hold())
                self.assert_automation_held(shell)
                marker = Path(directory) / "fixed"
                shell.send_user(f"trap - CHLD; printf ok > {marker}\n".encode())
                until(shell, lambda s: marker.exists() and s["parent_mode"] == "manual_prompt")
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                self.assertEqual(shell.claim_manager()["input_owner"], "manager")

    def test_rejection_outside_control_loop_itself_returns_input_without_ready(self):
        # Root ruling (p27-deadline-fix item 4): removing the prompt hook means
        # no READY ever comes; HOOK_REJECTED before any WAIT is the parent's own
        # authenticated statement that wb-handoff returned to the prompt.
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="cw07-noready-") as directory:
                shell = self.open(choice, directory)
                shell.send_user(b"PROMPT_COMMAND=\n" if choice.kind == "bash" else b"PS1='$ '\n")
                until(shell, lambda s: s["parent_mode"] != "unknown")
                shell.send_user(b"wb-handoff\n")
                state = until(shell, lambda s: "unsupported_hook_or_trap" in s["held_reasons"])
                self.assertNotIn("READY", shell._transport.events[shell._transport.events.index("HOOK_REJECTED"):])
                self.assertEqual(state["input_owner"], "user")
                self.assertIsNone(shell.manual_input_hold())
                marker = Path(directory) / "typed"
                shell.send_user(f"printf ok > {marker}\n".encode())
                until(shell, lambda _: marker.exists())
                self.assertEqual(marker.read_text(), "ok")
                self.assert_automation_held(shell)

    def test_confirmed_run_takeover_input_is_experiment_input_not_line_editing(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="cw07-run-") as directory:
                shell = self.open(choice, directory)
                out = Path(directory) / "answers"
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                shell.claim_manager()
                control, automation = ports(shell)
                shell.submit(control, "read -r a; read -r b; printf '%s|%s' \"$a\" \"$b\" > "
                             + shlex.quote(str(out)), automation)
                until(shell, lambda s: s["lifecycle"]["experiment_started"])
                shell.request_takeover()
                confirmed = shell.confirm_takeover()
                self.assertIsNotNone(confirmed["foreground_target"])
                boundary = shell._transport.boundary
                lines = boundary.submitted_lines
                shell.send_user(b"x\x1b[D\x7fy\x15first\n")
                state = until(shell, lambda s: True)
                self.assertEqual(state["lifecycle"]["unknown"], [])
                self.assertIsNotNone(state["foreground_target"])
                self.assertEqual((boundary.uncertain, boundary.submitted_lines), (False, lines))
                self.assert_automation_held(shell)
                shell.send_user(b"second\n")
                until(shell, lambda s: s["lifecycle"]["input_barrier"])
                self.assertEqual(out.read_text(), "first|second")
                shell.release_input()
                until(shell, lambda s: s["lifecycle"]["control_returned"] and s["parent_mode"] == "manual_prompt")
                self.assertEqual(shell.snapshot()["lifecycle"]["unknown"], [])
                self.assert_automation_held(shell)
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                shell.claim_manager()
                fresh, automation = ports(shell)
                shell.submit(fresh, ":", automation)
                until(shell, lambda s: s["lifecycle"]["input_barrier"])
                shell.release_input()
                until(shell, lambda s: s["lifecycle"]["control_returned"])


class ShellPaneManualAdmissionTests(unittest.TestCase):
    def test_pane_admits_editing_and_foreground_program_input_and_reports_holds(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="cw07-pane-") as directory:
                pane = ShellPane(choice, {"PATH": "/usr/bin:/bin", "HOME": directory, "TERM": "xterm"})
                try:
                    out = Path(directory) / "out"
                    self.assertIsNone(pane.admit(b"echo x\x1b[D\x7f"))
                    pane.pump()
                    self.assertEqual(pane.state["phase"], "unknown")
                    self.assertIsNone(pane.admit(b"\x03"))
                    self.assertIsNone(pane.admit(f"cat > {shlex.quote(str(out))}\r".encode()))
                    until(pane.shell, lambda s: s["parent_mode"] == "manual_foreground")
                    self.assertIsNone(pane.admit(b"line\n\x04"))
                    until(pane.shell, lambda s: s["parent_mode"] == "manual_prompt" and s["phase"] != "unknown")
                    self.assertEqual(out.read_text(), "line\n")
                    self.assertEqual((pane.dropped_input_bytes, pane.last_input_problem), (0, None))
                    self.assertIsNone(pane.admit(b"wb-handoff\r"))
                    until(pane.shell, lambda s: s["parent_mode"] == "control_wait")
                    pane.pump()
                    refused = pane.admit(b"x")
                    self.assertEqual(refused[0], Reason.INPUT_TARGET_UNKNOWN)
                    self.assertIn("control wait", refused[1])
                finally:
                    pane.close()


if __name__ == "__main__":
    unittest.main()
