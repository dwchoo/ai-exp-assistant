"""p27-overprint-01: a pane whose height shrinks keeps its cursor on the line it was on.

pyte 0.8.2 ``Screen.resize`` drops rows from the top on a height shrink but restores the cursor at its old
row, so a full host pane (cursor on the last row) left the cursor below the visible screen: the next typed
line (``$ echo X_42``) went to an invisible row and the following output was drawn over the visible prompt
line. The pane screen now shrinks like tmux/xterm: rows below the cursor go first, then top rows scroll into
the history, and the cursor moves up with its line.
"""

from __future__ import annotations

import unittest

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


def full_pane(rows: int = 14, cols: int = 50) -> tuple[TerminalScreen, object]:
    screen = TerminalScreen(cols, rows, history=500)
    stream = make_stream(screen)
    stream.feed(b"".join(b"%d\r\n" % i for i in range(1, 31)) + b"Hardware inspection completed.\r\n$ ")
    return screen, stream


def lines(screen: TerminalScreen) -> list[str]:
    return [line.rstrip() for line in screen.display]


def history_text(screen: TerminalScreen) -> list[str]:
    return ["".join(row[x].data for x in sorted(row)).rstrip() for row in screen.history.top]


class ShrinkTests(unittest.TestCase):
    def test_a_full_pane_shrunk_then_a_follow_up_command_is_not_overprinted(self):
        screen, stream = full_pane()
        self.assertEqual((screen.cursor.y, screen.cursor.x), (13, 2))
        screen.resize(lines=8, columns=50)
        self.assertEqual((screen.cursor.y, screen.cursor.x), (7, 2), "the cursor stays on the prompt line")
        self.assertEqual(lines(screen)[-2:], ["Hardware inspection completed.", "$"])
        # bash redraws its prompt after SIGWINCH, the user types, the command prints, a new prompt follows
        stream.feed(b"\r$ echo X_$((6*7))")
        self.assertEqual(lines(screen)[-1], "$ echo X_$((6*7))", "the typed line is visible")
        stream.feed(b"\r\nX_42\r\n$ ")
        self.assertEqual(lines(screen)[-4:], ["Hardware inspection completed.", "$ echo X_$((6*7))", "X_42", "$"])
        self.assertEqual((screen.cursor.y, screen.cursor.x), (7, 2))

    def test_the_rows_that_leave_the_top_go_to_the_history_in_order(self):
        screen, _ = full_pane()
        before = history_text(screen)
        visible = lines(screen)
        screen.resize(lines=8)
        self.assertEqual(history_text(screen), before + visible[:6])
        self.assertEqual(lines(screen), visible[6:])

    def test_rows_below_the_cursor_go_first_and_nothing_scrolls_when_the_cursor_still_fits(self):
        screen = TerminalScreen(40, 10, history=50)
        stream = make_stream(screen)
        stream.feed(b"one\r\ntwo\r\n$ ")
        screen.resize(lines=4)
        self.assertEqual(lines(screen), ["one", "two", "$", ""])
        self.assertEqual((screen.cursor.y, screen.cursor.x), (2, 2))
        self.assertEqual(len(screen.history.top), 0)
        stream.feed(b"ls\r\nout\r\n$ ")
        self.assertEqual(lines(screen), ["two", "$ ls", "out", "$"])

    def test_a_shrink_by_several_steps_matches_one_shrink(self):
        stepped, _ = full_pane()
        for rows in (12, 10, 7, 5):
            stepped.resize(lines=rows)
        once, _ = full_pane()
        once.resize(lines=5)
        self.assertEqual(lines(stepped), lines(once))
        self.assertEqual(history_text(stepped), history_text(once))
        self.assertEqual((stepped.cursor.y, stepped.cursor.x), (once.cursor.y, once.cursor.x), (4, 2))

    def test_growing_and_column_changes_keep_pyte_behaviour(self):
        screen, stream = full_pane()
        screen.resize(lines=20, columns=40)
        self.assertEqual((screen.lines, screen.columns), (20, 40))
        self.assertEqual((screen.cursor.y, screen.cursor.x), (13, 2))
        self.assertEqual(lines(screen)[12:14], ["Hardware inspection completed.", "$"])
        screen.resize(lines=14, columns=20)
        self.assertEqual(lines(screen)[12:14], ["Hardware inspection", "$"])
        stream.feed(b"x\r\ny")
        self.assertEqual(lines(screen)[-2:], ["$ x", "y"])

    def test_a_scroll_region_is_reset_and_still_works_after_a_shrink(self):
        screen, stream = full_pane()
        stream.feed(b"\x1b[2;5r")  # DECSTBM; the cursor goes home
        stream.feed(b"\x1b[14;3H")  # back to the prompt
        screen.resize(lines=6)
        self.assertIsNone(screen.margins)
        self.assertEqual(screen.cursor.y, 5)
        stream.feed(b"\r\nnext")
        self.assertEqual(lines(screen)[-1], "next")

    def test_leaving_the_alternate_screen_after_a_shrink_keeps_the_shell_prompt_line(self):
        screen, stream = full_pane()
        stream.feed(b"less file\r\n\x1b[?1049h\x1b[Hpager page")
        screen.resize(lines=8)
        self.assertEqual(lines(screen)[0], "pager page")
        stream.feed(b"\x1b[?1049l")
        self.assertEqual(screen.lines, 8)
        self.assertLess(screen.cursor.y, 8, "the restored primary cursor is on the visible screen")
        stream.feed(b"\r$ echo X_42\r\nX_42\r\n$ ")
        self.assertEqual(lines(screen)[-3:], ["$ echo X_42", "X_42", "$"])
        self.assertIn("Hardware inspection completed.", lines(screen) + history_text(screen))

    def test_the_alternate_screen_itself_keeps_its_cursor_visible_and_no_history(self):
        screen, stream = full_pane()
        stream.feed(b"\x1b[?1049h" + b"".join(b"alt %d\r\n" % i for i in range(13)) + b"alt end")
        pushed = len(screen.history.top)
        screen.resize(lines=6)
        self.assertEqual(screen.cursor.y, 5)
        self.assertEqual(lines(screen)[-1], "alt end")
        self.assertEqual(len(screen.history.top), pushed, "alternate-screen rows never enter a history")


if __name__ == "__main__":
    unittest.main()
