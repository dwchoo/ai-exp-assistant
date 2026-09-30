"""Full display-width clipping at the curses and exact-RGB pane boundary."""
from __future__ import annotations

import io
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.ui.terminal_g1 import app


class _Surface:
    def __init__(self):
        self.screen = TerminalScreen(90, 24)
        self.stream = make_stream(self.screen)
        self.writes = []

    def getmaxyx(self):
        return 24, 90

    def erase(self):
        self.stream.feed(b"\x1b[2J")

    def addstr(self, row, column, text, *_attrs):
        self.writes.append((row, column, text))
        self.stream.feed(f"\x1b[{row + 1};{column + 1}H{text}".encode())

    def addnstr(self, row, column, text, limit, *_attrs):
        self.addstr(row, column, text[:limit])

    def addch(self, row, column, char):
        self.addstr(row, column, chr(char))

    def move(self, row, column):
        self.stream.feed(f"\x1b[{row + 1};{column + 1}H".encode())

    def refresh(self):
        pass


class WideBoundaryTests(unittest.TestCase):
    def draw(self, data, column, truecolor):
        source = TerminalScreen(30, 20)
        make_stream(source).feed(f"\x1b[1;{column + 1}H\x1b[38;2;17;29;43m{data}".encode())
        if data == "가\u0301":
            # Model a combined wide cell explicitly: pyte may otherwise store
            # the combining mark on its empty continuation cell.
            source.buffer[0][column] = source.buffer[0][column]._replace(data=data)
            source.buffer[0][column + 1] = source.buffer[0][column + 1]._replace(data="")
        source.resize(lines=20, columns=28)
        surface, output = _Surface(), io.StringIO()
        session = SimpleNamespace(pid=1, poll=lambda: None, resize=lambda *_args: None)
        panes = [("MANAGER", session, source),
                 ("WORKER", session, TerminalScreen(28, 20)),
                 ("HOST", session, TerminalScreen(28, 20))]
        with patch.dict(os.environ, {"COLORTERM": "truecolor" if truecolor else ""}), \
                patch.object(app.sys, "stdout", output), \
                patch.object(app.curses, "color_pair", return_value=0), \
                patch.multiple(app.curses, ACS_ULCORNER=ord("+"), ACS_LLCORNER=ord("+"),
                               ACS_URCORNER=ord("+"), ACS_LRCORNER=ord("+"),
                               ACS_HLINE=ord("-"), ACS_VLINE=ord("|"), create=True):
            app._draw(surface, panes, 0, SimpleNamespace(pair=lambda *_args: 0))
        cursor = surface.screen.cursor.y, surface.screen.cursor.x
        surface.stream.feed(output.getvalue().encode())
        self.assertEqual((surface.screen.cursor.y, surface.screen.cursor.x), cursor)
        self.assertEqual(surface.screen.buffer[2][29].data, "|")
        return source, surface, output.getvalue()

    def test_resize_clipped_wide_lead_never_overwrites_border(self):
        for truecolor in (False, True):
            for text in ("가", "가\u0301"):
                with self.subTest(truecolor=truecolor, text=text):
                    source, surface, output = self.draw(text, 27, truecolor)
                    self.assertTrue(source.buffer[0][27].data.startswith("가"))
                    self.assertFalse(any(row == 2 and column == 28 and "가" in value
                                         for row, column, value in surface.writes))
                    self.assertNotIn("가", output)

    def test_fitting_wide_continuation_and_combining_cell_are_preserved(self):
        for truecolor in (False, True):
            for text, column in (("가\u0301", 26), ("e\u0301", 27)):
                with self.subTest(truecolor=truecolor, text=text):
                    source, surface, output = self.draw(text, column, truecolor)
                    rendered = surface.screen.buffer[2][column + 1].data
                    if column == 26:
                        # pyte stores this combining mark on the continuation;
                        # the emitted cell string is also asserted below.
                        rendered += surface.screen.buffer[2][column + 2].data
                    self.assertEqual(rendered, source.buffer[0][column].data)
                    self.assertIn((2, column + 1, source.buffer[0][column].data), surface.writes)
                    if column == 26:
                        self.assertEqual(source.buffer[0][27].data, "")
                        self.assertFalse(any(row == 2 and column == 28 for row, column, _ in surface.writes))
                    if truecolor:
                        self.assertEqual(surface.screen.buffer[2][column + 1].fg, "111d2b")
                        self.assertIn(source.buffer[0][column].data, output)


if __name__ == "__main__":
    unittest.main()
