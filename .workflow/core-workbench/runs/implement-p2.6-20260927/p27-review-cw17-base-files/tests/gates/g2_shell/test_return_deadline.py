"""The G4 caller opts in; every legacy common-RUN keeps its 5-second bound."""
import tempfile
from pathlib import Path
import shlex
import time
import unittest
from copy import deepcopy

from workbench.terminal.shell_g2.lifecycle import ManagedLifecycleProbe, normalize_run
from workbench.terminal.shell_g2.prototype import ShellChoice


class ReturnDeadlineTests(unittest.TestCase):
    def test_default_wire_and_invalid_bounds(self):
        self.assertNotIn("return_timeout", normalize_run("/bin/bash", ":", "x"))
        self.assertNotIn("return_timeout", normalize_run("/bin/bash", ["/bin/true"], "x"))
        self.assertEqual(normalize_run("/bin/bash", ":", "x", return_timeout=120)["return_timeout"], 120)
        for value in (True, 0, -1, float("inf"), float("nan"), 301):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_run("/bin/bash", ":", "x", return_timeout=value)

    def run_case(self, seconds, bound):
        with tempfile.TemporaryDirectory(prefix="cw05-deadline-") as directory, ManagedLifecycleProbe(ShellChoice("bash", "/bin/bash")) as shell:
            shell.wait_ready()
            root = Path(directory)
            shell._write_all((f"export BOUNDARY_PREPARED=deadline BOUNDARY_TRAPS={shlex.quote(str(root/'before'))} BOUNDARY_TRAPS_AFTER={shlex.quote(str(root/'after'))}; wb-handoff\n").encode())
            deadline = time.monotonic() + 3
            while not shell.control_wait_seen and time.monotonic() < deadline: shell._drain(.02)
            shell.dispatch_managed("deadline", ["/bin/sleep", str(seconds)], return_timeout=bound)
            deadline = time.monotonic() + seconds + 3
            while not shell.lifecycle.input_barrier and not shell.lifecycle.unknown and time.monotonic() < deadline: shell._drain(.02)
            # Capture the observed return boundary before owned-process cleanup.
            return deepcopy(shell.lifecycle)

    def test_explicit_long_main_and_expiry_unknown(self):
        normal = self.run_case(5.2, 8)
        self.assertFalse(normal.unknown)
        self.assertEqual(normal.main_exit, 0)
        self.assertTrue(normal.input_barrier)
        expired = self.run_case(1, .1)
        self.assertTrue(expired.unknown)
        self.assertFalse(expired.input_barrier)
