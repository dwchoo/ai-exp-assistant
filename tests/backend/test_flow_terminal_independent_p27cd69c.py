"""C-D69 (5) independent (p27-cd69-stuck-test-01), updated for C-D69 (6)(c) (p27-cd69-cmds-test-01).

Derived from DECISIONS.md C-D69 (5): (a) after any start failure the host shell returns to the user and the
result text matches the real state. C-D69 (6)(c) replaces the (5)(b) refusal: a command too long for one host
shell request is no longer refused (``command_too_long``) but written by the harness to a script file and run
(``script_path``); the request limit itself stays (the shell still refuses an oversize request, which is why the
size check exists). Real backend ShellPane through HostShellPort, bash and dash. No OMP, no provider.
"""

from __future__ import annotations

import re
import time
import threading
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from workbench.backend import flow_terminal
from workbench.backend.flow_terminal import TerminalService
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.terminal.shell_g2.prototype import ShellChoice

import test_flow_terminal as tf

LIMIT = 4096


class _RealShell(tf.ServiceFixture):
    CHOICE: ShellChoice

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.pane = ShellPane(self.CHOICE, {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8"})
        self.ui = bytearray()
        self.stop = threading.Event()
        self.loop = threading.Thread(target=self._loop, daemon=True)
        self.loop.start()
        self.terminal.close()
        self.terminal = self.make_terminal(lambda: HostShellPort(self.pane, lambda: self.pane))
        self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        self.type(f"cd {self.home}\r".encode())
        self.assertTrue(tf.wait_until(lambda: self.pane.cwd() == str(self.home), 5))
        self.assertTrue(tf.wait_until(self.idle, 5))

    def _loop(self):
        while not self.stop.is_set():
            for chunk in self.pane.pump():
                self.ui.extend(chunk.data)
            time.sleep(0.01)

    def tearDown(self):
        self.terminal.close()
        self.stop.set()
        self.loop.join(5)
        self.pane.close()
        super().tearDown()

    def type(self, data):
        self.assertIsNone(self.pane.admit(data))

    def idle(self):
        port = HostShellPort(self.pane, lambda: self.pane)
        try:
            return port.busy() is None
        finally:
            port.detach()

    def records(self, kind):
        return [r for r in tf.journal(self.root) if r["type"] == kind]

    def assert_returned(self):
        """Input owner user at a clean prompt, no hold, gate free; a manual and a terminal command both work."""
        self.assertTrue(tf.wait_until(lambda: self.pane.state["input_owner"] == "user"
                                      and self.pane.state["parent_mode"] == "manual_prompt", 10), self.pane.state)
        self.assertIsNone(self.pane.automation_hold)
        self.assertIsNone(self.gate.owner)
        self.assertEqual(self.pane.cwd(), str(self.home))
        marker = f"manual-{uuid4().hex[:6]}"
        self.type(f"echo {marker}-$((6*7))\r".encode())
        self.assertTrue(tf.wait_until(lambda: f"{marker}-42".encode() in self.ui, 5), "a manual command runs")
        self.assertTrue(tf.wait_until(self.idle, 5))
        after = self.run_tool({"command": "echo follow-$((1+1))", "wait": 30})
        self.assertEqual((after.get("status"), after.get("exit_code")), ("exited", 0), after)
        self.assertIn("follow-2", after["output_tail"])
        self.assertTrue(tf.wait_until(lambda: self.pane.state["input_owner"] == "user"
                                      and self.pane.state["parent_mode"] == "manual_prompt", 10), self.pane.state)

    def room(self):
        """The longest plain ASCII command that still runs inline (no script file), found on the service."""
        return flow_terminal.command_room(self.CHOICE.executable)

    # -- C-D69 (6)(c) replaces (5)(b): oversize runs from a script file -------------------------------
    def test_oversize_runs_from_a_script_file_instead_of_being_refused(self):
        for command, marker in (("echo " + "q" * 5000, "q" * 100), ("printf '%s\\n' " + "한" * 800, "한" * 50),
                                ("echo 'a\"b'\n" * 400, 'a"b')):
            with self.subTest(size=len(command)):
                before = len(self.ui)
                result = self.run_tool({"command": command, "wait": 30})
                self.assertEqual((result.get("status"), result.get("exit_code")), ("exited", 0), result)
                self.assertNotEqual(result.get("status"), "command_too_long")
                self.assertIn(marker, result["output_tail"])
                script = Path(result["script_path"])
                self.assertEqual(script.read_text(encoding="utf-8").rstrip("\n"), command.rstrip("\n"))
                shown = bytes(self.ui[before:]).decode("utf-8", "replace")
                self.assertIn(f"(script {script})", shown)
                self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        self.assertEqual({r["status"] for r in self.records("terminal_refused")}, set(), "nothing refused")
        if self.CHOICE.kind == "bash":
            before = len(self.ui)
            self.type(b"history 5\r")
            self.assertTrue(tf.wait_until(lambda: b"history 5" in self.ui[before:], 5))
            time.sleep(0.3)
            self.assertNotIn(b"qqqq", bytes(self.ui[before:]), "the long command is not in the user's history")
        self.assert_returned()

    def test_the_room_is_the_exact_inline_boundary_on_the_real_shell(self):
        room = self.room()
        self.assertGreater(room, 2500)
        fits = "echo " + "a" * (room - 5)
        over = fits + "a"
        ran = self.run_tool({"command": fits, "wait": 30})
        self.assertEqual((ran.get("status"), ran.get("exit_code")), ("exited", 0), ran)
        self.assertNotIn("script_path", ran, "the room still runs inline")
        self.assertIn("a" * 200, ran["output_tail"])
        self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        spilled = self.run_tool({"command": over, "wait": 30})
        self.assertEqual((spilled.get("status"), spilled.get("exit_code")), ("exited", 0), spilled)
        self.assertIn("script_path", spilled, "room + 1 runs from a script file")
        self.assertIn("a" * 200, spilled["output_tail"])
        self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        # the size check is necessary: without it the shell itself refuses room + 1 (the limit stays)
        with mock.patch.object(flow_terminal, "command_request_bytes", lambda executable, command: 0):
            failed = self.run_tool({"command": over, "wait": 10})
        self.assertEqual(failed["status"], "start_failed", failed)
        self.assertIn("pipe atomic write", failed["reason"])
        self.assert_returned()

    def test_multibyte_boundary(self):
        size = flow_terminal.command_request_bytes
        exe = self.CHOICE.executable
        n = 1
        while size(exe, "printf '%s\\n' " + "가" * (n + 1)) <= LIMIT:
            n += 1
        self.assertLess(n, 700)
        ran = self.run_tool({"command": "printf '%s\\n' " + "가" * n, "wait": 30})
        self.assertEqual((ran.get("status"), ran.get("exit_code")), ("exited", 0), ran)
        self.assertNotIn("script_path", ran)
        self.assertIn("가" * 50, ran["output_tail"])
        self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        over = self.run_tool({"command": "printf '%s\\n' " + "가" * (n + 1), "wait": 30})
        self.assertEqual((over.get("status"), over.get("exit_code")), ("exited", 0), over)
        self.assertIn("script_path", over)
        self.assertIn("가" * (n + 1), over["output_tail"])
        self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        with mock.patch.object(flow_terminal, "command_request_bytes", lambda executable, command: 0):
            failed = self.run_tool({"command": "printf '%s\\n' " + "가" * (n + 1), "wait": 10})
        self.assertEqual(failed["status"], "start_failed", failed)
        self.assert_returned()

    # -- (a) every start-failure branch -------------------------------------------------------------
    def assert_start_failed_and_returned(self, result, reason_part):
        self.assertEqual(result["status"], "start_failed", result)
        self.assertIn(reason_part, result["reason"])
        self.assertEqual(result["host_terminal"], {"input_owner": "user", "parent_mode": "manual_prompt"})
        self.assertIn("the host terminal is the user's again", result["detail"])
        failed = self.records("terminal_start_failed")[-1]
        self.assertIs(failed["returned_to_user"], True)
        self.assert_returned()

    def test_pipe_refusal_after_the_handoff(self):
        with mock.patch.object(flow_terminal, "command_request_bytes", lambda executable, command: 0):
            result = self.run_tool({"command": "echo " + "p" * 5000, "wait": 10})
        self.assert_start_failed_and_returned(result, "pipe atomic write")
        self.assertNotIn(b"pppppppp", bytes(self.ui))

    def test_claim_failure(self):
        with mock.patch.object(HostShellPort, "claim_manager", side_effect=RuntimeError("forced claim failure")):
            result = self.run_tool({"command": "echo never-runs", "wait": 10})
        self.assert_start_failed_and_returned(result, "forced claim failure")
        self.assertNotIn(b"never-runs\r\n", bytes(self.ui).replace(b"echo never-runs", b""))

    def test_submit_failure_after_the_claim(self):
        # the managed request channel fails (e.g. a broken control pipe): nothing was sent
        with mock.patch.object(HostShellPort, "submit", side_effect=OSError("control pipe broken")):
            result = self.run_tool({"command": "echo never-runs", "wait": 10})
        self.assert_start_failed_and_returned(result, "control pipe broken")

    def test_pause_during_start(self):
        calls = []

        def paused():
            calls.append(1)
            return len(calls) >= 3  # first two checks pass; the one after the claim sees the pause

        self.terminal.close()
        self.terminal = TerminalService(handoffs=self.handoffs,
                                        host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
                                        gate=self.gate, log_root=self.root / "workflow" / "terminal",
                                        automation=lambda: tf.AUTOMATION, paused=paused,
                                        activity=lambda: None, sensitive_values=lambda: (), poll_interval=0.02)
        result = self.run_tool({"command": "echo never-runs", "wait": 10})
        self.assertGreaterEqual(len(calls), 3)
        self.terminal._paused = lambda: False  # resumed: the follow-up commands may run
        self.assert_start_failed_and_returned(result, "paused")

    def test_cancel_during_start(self):
        original = TerminalService._abandoned_now
        seen = []

        def abandoned(service, key):
            seen.append(1)
            return len(seen) == 3 or original(service, key)  # after wb-handoff and the claim

        with mock.patch.object(TerminalService, "_abandoned_now", abandoned):
            result = self.run_tool({"command": "echo never-runs", "wait": 10})
        self.assertEqual(result["status"], "aborted", result)
        self.assertIn("nothing was run", result["detail"])
        aborted = self.records("terminal_aborted")[-1]
        self.assertIs(aborted["returned_to_user"], True)
        self.assert_returned()

    def test_unconfirmed_return_is_never_reported_as_returned(self):
        # truthfulness: if the takeover does nothing, the result must not say the user has the shell
        with mock.patch.object(flow_terminal, "command_request_bytes", lambda executable, command: 0), \
                mock.patch.object(HostShellPort, "request_takeover", lambda self: {}):
            result = self.run_tool({"command": "echo " + "u" * 5000, "wait": 10})
        self.assertEqual(result["status"], "start_failed", result)
        self.assertNotIn("user's again", result["detail"])
        self.assertIn("could not confirm", result["detail"])
        self.assertEqual(result["host_terminal"]["input_owner"], "manager")
        self.assertIs(self.records("terminal_start_failed")[-1]["returned_to_user"], False)
        port = HostShellPort(self.pane, lambda: self.pane)  # the recovery the detail names works
        try:
            port.request_takeover()
        finally:
            port.detach()
        self.assert_returned()

    def test_claim_before_the_control_wait_is_reported_truthfully(self):
        # a shell slower than the preparation wait: the claim fails before wb-handoff is processed
        with mock.patch.object(flow_terminal, "PREPARE_WAIT", 0.0):
            result = self.run_tool({"command": "echo never-runs", "wait": 10})
        self.assertEqual(result["status"], "start_failed", result)
        failed = self.records("terminal_start_failed")[-1]
        if result["host_terminal"] == {"input_owner": "user", "parent_mode": "manual_prompt"}:
            self.assertIn("user's again", result["detail"])
            self.assertIs(failed["returned_to_user"], True)
        else:
            self.assertNotIn("user's again", result["detail"])
            self.assertIn("could not confirm", result["detail"])
            self.assertIn("prefix t", result["detail"])
            self.assertIs(failed["returned_to_user"], False)
            self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "control_wait", 5))
            self.assertEqual(self.pane.state["input_owner"], "user", "the user can take it back (C-D58)")
            self.pane.request_takeover()  # the recovery the detail names
        self.assert_returned()

    # -- C-D58: a handoff the user typed is not taken away --------------------------------------------
    def test_user_initiated_handoff_is_left_alone(self):
        self.type(b"wb-handoff\r")
        self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "control_wait", 5), self.pane.state)
        result = self.run_tool({"command": "echo never-runs", "wait": 10})
        self.assertEqual(result["status"], "host_terminal_busy", result)
        too_long = self.run_tool({"command": "echo " + "x" * 5000})  # C-D69 (6)(c): busy, no script left
        self.assertEqual(too_long["status"], "host_terminal_busy", too_long)
        logs = self.root / "workflow" / "terminal"
        self.assertEqual(list(logs.glob("*.sh")) if logs.exists() else [], [], "no script file for a refusal")
        time.sleep(1.0)
        state = self.pane.state
        self.assertEqual((state["input_owner"], state["parent_mode"]), ("user", "control_wait"), state)
        self.assertFalse(state["takeover_requested"], "Workbench did not take the user's handoff back")
        self.assertEqual(self.records("terminal_start_failed"), [])


class BashTerminalTests(_RealShell):
    CHOICE = ShellChoice("bash", "/usr/bin/bash")


class DashTerminalTests(_RealShell):
    CHOICE = ShellChoice("sh", "/usr/bin/dash")


del _RealShell

if __name__ == "__main__":
    unittest.main()
