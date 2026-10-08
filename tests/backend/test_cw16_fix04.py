"""p27-cw16-fix-04: D1 pane colours (C-D73 256-colour approximation, no hex misread as a decimal index) and D3
(every OMP process Workbench starts runs with umask 077; the host shell keeps the user's umask). No OMP, no model."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from workbench.backend import launcher
from workbench.backend.panes import OmpPane, ShellPane
from workbench.contracts.v1 import PaneId
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.ui.terminal_g1.app import _color_index

# xterm palette as pyte writes it (pyte.graphics.FG_BG_256), kept here so the expectation is independent.
CUBE = (0, 95, 135, 175, 215, 255)


def xterm_hex(index: int) -> str:
    if index >= 232:
        level = 8 + 10 * (index - 232)
        return f"{level:02x}" * 3
    index -= 16
    return "".join(f"{CUBE[part]:02x}" for part in (index // 36, index // 6 % 6, index % 6))


class ColorIndex(unittest.TestCase):
    def test_decimal_looking_hex_is_an_rgb_not_an_index(self):
        # D1: '303030' was read as decimal 303030 and clamped to 255 (near white).
        self.assertEqual(_color_index("303030", 256), 236)
        self.assertEqual(_color_index("808080", 256), 244)
        self.assertEqual(_color_index("585858", 256), 240)
        self.assertEqual(_color_index("111111", 256), 233)
        self.assertEqual(_color_index("123456", 256), 23)  # cube 005f5f is nearer than any grey

    def test_every_cube_and_grey_colour_maps_back_to_its_own_index(self):
        for index in range(16, 256):
            with self.subTest(index=index):
                self.assertEqual(_color_index(xterm_hex(index), 256), index)

    def test_system_colours_keep_their_index_when_their_rgb_is_unique(self):
        for index, value in ((1, "cd0000"), (4, "0000ee"), (8, "7f7f7f"), (12, "5c5cff")):
            self.assertEqual(_color_index(value, 256), index)

    def test_rgb_takes_the_nearer_of_cube_and_grey(self):
        self.assertEqual(_color_index("ff8000", 256), 208)  # ff8700
        self.assertEqual(_color_index("c81e8c", 256), 162)  # d70087
        self.assertEqual(_color_index("777777", 256), 243)  # grey 767676 beats cube 5f5f5f/878787
        self.assertEqual(_color_index("5f5f5f", 256), 59)  # exact cube wins the tie-free case

    def test_index_strings_and_names_unchanged(self):
        self.assertEqual(_color_index("196", 256), 196)
        self.assertEqual(_color_index("7", 256), 7)
        self.assertEqual(_color_index("brightred", 256), 9)
        self.assertEqual(_color_index("nonsense", 256), -1)


def wait_file(path: Path, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.02)
    raise AssertionError(f"{path} not written")


class OmpUmask(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="cw16-fix04-", dir="/tmp"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, True))
        self.previous = os.umask(0o002)  # a permissive user umask, as in the gap run
        self.addCleanup(os.umask, self.previous)

    def test_omp_pane_child_runs_with_umask_077(self):
        out = self.directory / "umask"
        pane = OmpPane(PaneId.WORKER_OMP, "worker", ["/bin/sh", "-c", f"umask > {out}; exec sleep 30"],
                       {"PATH": "/usr/bin:/bin"})
        try:
            self.assertEqual(wait_file(out), "0077")
        finally:
            pane.close(grace=0.5)

    def test_omp_helper_processes_run_with_umask_077(self):
        outputs = launcher._bounded_outputs([["/bin/sh", "-c", "umask"]], cwd=self.directory,
                                            environment={"PATH": "/usr/bin:/bin"}, deadline=time.monotonic() + 5)
        self.assertEqual(outputs[0][1].strip(), b"0077")
        out = self.directory / "iso-umask"
        launcher.check_isolation(["/bin/sh", "-c", f"umask > {out}", "x"], cwd=self.directory,
                                 environment={"PATH": "/usr/bin:/bin"}, role="worker", allowed_skills=(),
                                 timeout=5)
        self.assertEqual(wait_file(out), "0077")

    def test_host_shell_keeps_the_user_umask(self):
        out = self.directory / "shell-umask"
        pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                         {"PATH": "/usr/bin:/bin", "HOME": str(self.directory), "LANG": "C.UTF-8"})
        try:
            self.assertIsNone(pane.admit(f"umask > {out}\r".encode()))
            deadline = time.monotonic() + 5
            while not (out.exists() and out.read_text().strip()) and time.monotonic() < deadline:
                pane.pump()
                time.sleep(0.02)
            self.assertEqual(out.read_text().strip(), "0002")
        finally:
            pane.close()


if __name__ == "__main__":
    unittest.main()
