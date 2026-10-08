"""p27-polish-01 (overprint review P3-2): a height shrink also moves a DECSC-saved cursor with its line.

``_resize_screen`` scrolls ``drop`` top rows away and moves the cursor up by ``drop``; a cursor saved with
DECSC (``ESC 7``) before the shrink pointed at a row that moved too, so ``ESC 8`` after the shrink must land on
the same text line (clamped to the screen), not ``drop`` rows lower.
"""

from __future__ import annotations

import unittest

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


def filled(rows: int = 10, cols: int = 40) -> tuple[TerminalScreen, object]:
    screen = TerminalScreen(cols, rows, history=100)
    stream = make_stream(screen)
    stream.feed(b"\r\n".join(b"row%d" % i for i in range(rows)))  # cursor on the last row
    return screen, stream


def row_text(screen: TerminalScreen, y: int) -> str:
    return screen.display[y].rstrip()


class SavedCursorShrinkTests(unittest.TestCase):
    def test_a_saved_cursor_moves_up_with_its_line(self):
        screen, stream = filled()
        stream.feed(b"\x1b[6;3H\x1b7")  # save at row5, column 3
        stream.feed(b"\x1b[10;1H")  # back to the last row (the prompt)
        screen.resize(lines=6)  # drop = 10 - 6 = 4 rows scroll into the history
        self.assertEqual(row_text(screen, 1), "row5")
        stream.feed(b"\x1b8")
        self.assertEqual((screen.cursor.y, screen.cursor.x), (1, 2), "ESC 8 lands on row5 again")
        stream.feed(b"X")
        self.assertEqual(row_text(screen, 1), "roX5")

    def test_a_saved_cursor_on_a_row_that_left_the_screen_is_clamped_to_the_top(self):
        screen, stream = filled()
        stream.feed(b"\x1b[2;1H\x1b7\x1b[10;1H")  # saved on row1, which goes to the history
        screen.resize(lines=6)
        stream.feed(b"\x1b8")
        self.assertEqual(screen.cursor.y, 0)

    def test_nothing_moves_when_no_row_scrolls_away(self):
        screen = TerminalScreen(40, 10, history=50)
        stream = make_stream(screen)
        stream.feed(b"one\r\ntwo\x1b7\r\nthree")
        screen.resize(lines=5)  # only empty rows below the cursor go
        stream.feed(b"\x1b8")
        self.assertEqual((screen.cursor.y, screen.cursor.x), (1, 3))

    def test_every_saved_cursor_on_the_stack_moves(self):
        screen, stream = filled()
        stream.feed(b"\x1b[7;1H\x1b7\x1b[9;2H\x1b7\x1b[10;1H")  # row6 then row8
        screen.resize(lines=6)
        stream.feed(b"\x1b8")
        self.assertEqual((screen.cursor.y, screen.cursor.x), (4, 1), "row8")
        stream.feed(b"\x1b8")
        self.assertEqual((screen.cursor.y, screen.cursor.x), (2, 0), "row6")

    def test_the_primary_saved_cursor_moves_when_the_shrink_happened_in_the_alternate_screen(self):
        screen, stream = filled()
        stream.feed(b"\x1b[6;3H\x1b7\x1b[10;1H")  # primary: saved on row5, cursor on the last row
        stream.feed(b"\x1b[?1049h")
        screen.resize(lines=6)
        stream.feed(b"\x1b[?1049l")
        self.assertEqual(row_text(screen, 1), "row5")
        stream.feed(b"\x1b8")
        self.assertEqual(screen.cursor.y, 1)


if __name__ == "__main__":
    unittest.main()
