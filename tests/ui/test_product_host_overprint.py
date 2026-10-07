"""p27-overprint-01: the host pane after a full pane, a follow-up command and a window shrink (real curses UI).

Frames are read with the REP/SU/SD-aware pyte of ``support`` (what ncurses sends for xterm-256color): a plain
pyte ignores ``CSI Ps S``, which ncurses uses to scroll a full host pane, and then shows the follow-up output
over the previous last line with the prompt gone; a real terminal (tmux) shows the same bytes correctly.
"""
import os
import signal
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import FixtureServer  # noqa: E402
from test_product_pty import P, UiProcess  # noqa: E402

FILLED = b"".join(b"%d\r\n" % i for i in range(1, 61)) + b"Hardware inspection completed.\r\n$ "


class HostOverprintTests(unittest.TestCase):
    def start(self, size=(40, 170)):
        server = FixtureServer(replay={"manager_omp": b"MGR\r\n", "worker_omp": b"WRK\r\n", "host_shell": b"$ "})
        self.addCleanup(server.close)
        ui = UiProcess(server.path, size=size)
        self.addCleanup(ui.close)
        self.assertTrue(server.wait_for(server.attached.is_set), "attach not received")
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()), ui.text())
        ui.send(P + b"3")
        self.assertTrue(ui.until(lambda: "focus: HOST SHELL" in ui.text()), ui.text())
        self.seq = 2
        return server, ui

    def display(self, server, ui, data):
        server.display("host_shell", data, seq=self.seq)
        self.seq += 1
        ui.until(lambda: False, 0.15)

    @staticmethod
    def host_lines(ui):
        """Inner text of the host pane rows, top to bottom (bottom border excluded)."""
        rows = ui.screen.display
        top = next((i for i, line in enumerate(rows) if "HOST SHELL" in line and line[:1] in ("l", "┌")), None)
        if top is None:  # mid-redraw
            return []
        bottom = next((i for i in range(top + 1, len(rows)) if rows[i][:1] in ("m", "└")), len(rows))
        return [line[1:-1].rstrip() for line in rows[top + 1:bottom]]

    def follow_up(self, server, ui):
        for ch in b"echo X_$((6*7))":
            self.display(server, ui, bytes([ch]))
        self.display(server, ui, b"\r\nX_42\r\n$ ")
        expected = ["Hardware inspection completed.", "$ echo X_$((6*7))", "X_42", "$"]
        self.assertTrue(ui.until(lambda: self.host_lines(ui)[-4:] == expected, 3), self.host_lines(ui))

    def test_a_follow_up_in_a_full_host_pane_is_drawn_below_its_command(self):
        server, ui = self.start()
        self.display(server, ui, FILLED)
        self.assertTrue(ui.until(lambda: self.host_lines(ui)[-1] == "$", 3), self.host_lines(ui))
        self.follow_up(server, ui)

    def test_after_a_window_shrink_the_typed_line_and_its_output_stay_visible_and_in_order(self):
        server, ui = self.start()
        self.display(server, ui, FILLED)
        before = len(self.host_lines(ui))
        UiProcess.set_size(ui.fd, (30, 150))
        ui.screen.resize(30, 150)
        os.kill(ui.proc.pid, signal.SIGWINCH)
        self.assertTrue(ui.until(lambda: len(self.host_lines(ui)) < before
                                 and self.host_lines(ui)[-2:] == ["Hardware inspection completed.", "$"], 3),
                        self.host_lines(ui))
        self.display(server, ui, b"\r$ ")  # bash redraws its prompt line after SIGWINCH
        self.follow_up(server, ui)


if __name__ == "__main__":
    unittest.main()
