"""Only the launch handshake is bounded; a running experiment never expires.

F-P27-DEADLINE: the former 5 s / opt-in main-return deadline contradicted
BRIEF "실험 실행 시간 상한 없음" and C-AC-07, so the legacy ``return_timeout``
keyword is accepted but never becomes a wire field or a runtime bound.
"""
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
        self.assertNotIn("start_timeout", normalize_run("/bin/bash", ":", "x"))
        self.assertNotIn("start_timeout", normalize_run("/bin/bash", ["/bin/true"], "x"))
        self.assertEqual(normalize_run("/bin/bash", ":", "x", start_timeout=120)["start_timeout"], 120)
        for value in (True, 0, -1, float("inf"), float("nan"), 301):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_run("/bin/bash", ":", "x", start_timeout=value)
        self.assertNotIn("return_timeout", normalize_run("/bin/bash", ":", "x", return_timeout=120))

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

    def test_long_main_and_legacy_short_bound_both_complete(self):
        normal = self.run_case(5.2, 8)
        self.assertFalse(normal.unknown)
        self.assertEqual(normal.main_exit, 0)
        self.assertTrue(normal.input_barrier)
        legacy = self.run_case(1, .1)  # formerly expired into unknown by elapsed time
        self.assertFalse(legacy.unknown)
        self.assertEqual(legacy.main_exit, 0)
        self.assertTrue(legacy.input_barrier)
