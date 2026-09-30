"""Real-shell independent CW-07 adapter regressions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from workbench.terminal.shell_g2.prototype import ShellChoice, ShellProcess, UnsafeShellState
from workbench.terminal.shell_persistent.adapter import PersistentShell


class PersistentShellIndependentRuntimeTests(unittest.TestCase):
    def test_partial_manual_write_rechecks_unknown_before_remaining_bytes(self):
        payload = b"not-a-complete-command\n"
        for kind, executable in (("bash", "/bin/bash"), ("sh", "/bin/sh")):
            with self.subTest(shell=kind):
                with PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "TERM": "xterm"},
                                     choice=ShellChoice(kind, executable)) as shell:
                    initial = shell.snapshot()
                    self.assertEqual(initial["input_owner"], "user")
                    self.assertEqual(initial["parent_mode"], "manual_prompt")
                    self.assertNotEqual(initial["phase"], "unknown")
                    transport = shell._transport
                    emitted = bytearray()
                    calls = []

                    def short_write(fd, data):
                        self.assertEqual(fd, transport.master_fd)
                        offered = bytes(data)
                        calls.append(offered)
                        if len(calls) == 1:
                            emitted.extend(offered[:1])
                            transport.boundary.fail_closed()
                            return 1
                        emitted.extend(offered)
                        return len(offered)

                    refused = False
                    with patch("workbench.terminal.shell_g2.prototype.os.write", side_effect=short_write):
                        try:
                            shell.send_user(payload)
                        except UnsafeShellState:
                            refused = True
                    self.assertEqual(shell.snapshot()["phase"], "unknown")
                    self.assertEqual(calls, [payload], "remaining bytes reached a second PTY write")
                    self.assertEqual(bytes(emitted), payload[:1], "more than the unavoidable prefix was emitted")
                    self.assertTrue(refused, "partial-write boundary loss must refuse the rest")

    def test_unknown_manual_input_is_not_written_in_live_bash_and_sh(self):
        probe = Path(__file__).with_name("live_persistent_shell_independent.py")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        for kind in ("bash", "sh"):
            with self.subTest(shell=kind):
                result = subprocess.run([sys.executable, str(probe), "--unknown", kind], env=env,
                                        capture_output=True, text=True, timeout=15, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                evidence = json.loads(result.stdout)["results"][0]
                self.assertTrue(evidence["refused"])
                self.assertFalse(evidence["marker_seen"])
                self.assertEqual(evidence["residue"], [])

    def test_unknown_user_owned_prompt_never_calls_pty_writer(self):
        for kind, executable in (("bash", "/bin/bash"), ("sh", "/bin/sh")):
            with self.subTest(shell=kind):
                with PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "TERM": "xterm"},
                                     choice=ShellChoice(kind, executable)) as shell:
                    shell.send_user(b"exec 9>&-\n")
                    for _ in range(200):
                        state = shell.poll(.01)
                        shell.display_bytes()
                        if state["phase"] == "unknown":
                            break
                    else:
                        self.fail("control loss did not reach unknown")
                    self.assertEqual(state["input_owner"], "user")
                    self.assertIn("unknown_or_manual_residue", state["held_reasons"])
                    with patch.object(ShellProcess, "send_user",
                                      side_effect=AssertionError("PTY writer was invoked")) as writer:
                        with self.assertRaises(UnsafeShellState):
                            shell.send_user(b"must-not-write\n")
                        writer.assert_not_called()

    def test_bash_and_sh_takeover_environment_and_fail_closed(self):
        probe = Path(__file__).with_name("live_persistent_shell_independent.py")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        result = subprocess.run([sys.executable, str(probe)], env=env,
                                capture_output=True, text=True, timeout=45, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual([row["shell"] for row in evidence["results"]], ["bash", "sh"])
        for row in evidence["results"]:
            self.assertEqual(row["residue"], [])
            self.assertEqual(len(row["cases"]), 6)


if __name__ == "__main__":
    unittest.main()
