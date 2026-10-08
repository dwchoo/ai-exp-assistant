"""p27-cw16-fix-04/05: D1 pane colours (C-D73 256-colour approximation, no hex misread as a decimal index; every
SGR 30-37/90-97/40-47/100-107 keeps its index) and D3 (fix-05: OMP processes and the host shell all keep the user's
umask; the private data comes from the 0700 data dir / omp-root / agent). No OMP, no model."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest

import pyte

from workbench.backend import launcher, omp_home
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

    def test_every_sgr_30_37_90_97_40_47_100_107_keeps_its_index(self):
        # fix-05 (C17 P2-2): pyte names SGR 93/103 "brightbrown" (and spells SGR 105 "bfightmagenta").
        for first, base, attribute in ((30, 0, "fg"), (90, 8, "fg"), (40, 0, "bg"), (100, 8, "bg")):
            for step in range(8):
                sgr = first + step
                with self.subTest(sgr=sgr):
                    screen = pyte.Screen(4, 1)
                    pyte.Stream(screen).feed(f"\x1b[{sgr}mX")
                    name = getattr(screen.buffer[0][0], attribute)
                    self.assertNotEqual(name, "default")
                    self.assertEqual(_color_index(name, 256), base + step, name)
                    self.assertEqual(_color_index(name, 16), base + step, name)

    def test_pyte_bright_yellow_names_93_and_103(self):
        self.assertEqual(_color_index("brightbrown", 256), 11)
        self.assertEqual(_color_index("brightyellow", 256), 11)
        self.assertEqual(_color_index("bfightmagenta", 256), 13)


def wait_file(path: Path, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.02)
    raise AssertionError(f"{path} not written")


class OmpUmask(unittest.TestCase):
    """fix-05 (C17 P2-1): OMP's own tools write into the user's project, so OMP keeps the user's umask."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="cw16-fix04-", dir="/tmp"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, True))
        self.previous = os.umask(0o002)  # a permissive user umask, as in the gap run
        self.addCleanup(os.umask, self.previous)

    def test_omp_pane_child_keeps_the_user_umask(self):
        out = self.directory / "umask"
        pane = OmpPane(PaneId.WORKER_OMP, "worker", ["/bin/sh", "-c", f"umask > {out}; exec sleep 30"],
                       {"PATH": "/usr/bin:/bin"})
        try:
            self.assertEqual(wait_file(out), "0002")
        finally:
            pane.close(grace=0.5)

    def test_omp_project_files_keep_the_user_umask(self):
        out = self.directory / "project-file"
        pane = OmpPane(PaneId.WORKER_OMP, "worker", ["/bin/sh", "-c", f"touch {out}; mkdir {out}-dir; exec sleep 30"],
                       {"PATH": "/usr/bin:/bin"})
        try:
            deadline = time.monotonic() + 5
            while not Path(f"{out}-dir").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o664)
            self.assertEqual(stat.S_IMODE(os.stat(f"{out}-dir").st_mode), 0o775)
        finally:
            pane.close(grace=0.5)

    def test_omp_helper_processes_keep_the_user_umask(self):
        outputs = launcher._bounded_outputs([["/bin/sh", "-c", "umask"]], cwd=self.directory,
                                            environment={"PATH": "/usr/bin:/bin"}, deadline=time.monotonic() + 5)
        self.assertEqual(outputs[0][1].strip(), b"0002")
        out = self.directory / "iso-umask"
        launcher.check_isolation(["/bin/sh", "-c", f"umask > {out}", "x"], cwd=self.directory,
                                 environment={"PATH": "/usr/bin:/bin"}, role="worker", allowed_skills=(),
                                 timeout=5)
        self.assertEqual(wait_file(out), "0002")

    def test_workbench_omp_home_is_private_through_its_directories_not_the_umask(self):
        # The privacy of what OMP writes into its Workbench home comes from the 0700 directories.
        data = self.directory / "data"
        home = omp_home.prepare_omp_home(data, {"HOME": str(self.directory), "PATH": "/usr/bin:/bin"},
                                         skills_dir=self.directory, provider_ids=launcher.ISOLATION_PROVIDER_IDS)
        for directory in (omp_home.omp_root(data), home.agent_dir):
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700, directory)

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
