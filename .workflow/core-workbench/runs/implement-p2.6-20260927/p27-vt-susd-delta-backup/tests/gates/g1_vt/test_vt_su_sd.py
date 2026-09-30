"""CSI Ps S (SU) / CSI Ps T (SD) scroll semantics for the G1 VT screen."""
from __future__ import annotations

import unittest

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream

ROWS = 6


def _make(lines: int = ROWS, columns: int = 8, history: int = 100):
    screen = TerminalScreen(columns, lines, history=history)
    stream = make_stream(screen)
    return screen, stream


def _fill(stream, lines: int = ROWS) -> None:
    """Write row labels r0..rN-1 without scrolling; cursor ends on row 1, col 1."""
    for row in range(lines):
        stream.feed(f"\x1b[{row + 1};1Hr{row}".encode())
    stream.feed(b"\x1b[1;1H")


def _text(screen) -> list[str]:
    return [line.rstrip() for line in screen.display]


class ScrollUpDownTests(unittest.TestCase):
    def test_su_default_scrolls_one_line_full_screen(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[S")
        self.assertEqual(_text(screen), ["r1", "r2", "r3", "r4", "r5", ""])

    def test_sd_default_scrolls_one_line_full_screen(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[T")
        self.assertEqual(_text(screen), ["", "r0", "r1", "r2", "r3", "r4"])

    def test_explicit_count(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2S")
        self.assertEqual(_text(screen), ["r2", "r3", "r4", "r5", "", ""])
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2T")
        self.assertEqual(_text(screen), ["", "", "r0", "r1", "r2", "r3"])

    def test_zero_param_means_one(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[0S")
        self.assertEqual(_text(screen), ["r1", "r2", "r3", "r4", "r5", ""])

    def test_su_within_margins_leaves_outside_rows(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2;5r\x1b[S")
        self.assertEqual(_text(screen), ["r0", "r2", "r3", "r4", "", "r5"])

    def test_sd_within_margins_leaves_outside_rows(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2;5r\x1b[T")
        self.assertEqual(_text(screen), ["r0", "", "r1", "r2", "r3", "r5"])

    def test_count_larger_than_region_clears_region_only(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2;4r\x1b[99S")
        self.assertEqual(_text(screen), ["r0", "", "", "", "r4", "r5"])
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2;4r\x1b[99T")
        self.assertEqual(_text(screen), ["r0", "", "", "", "r4", "r5"])

    def test_count_equal_region_height_clears_region(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[3S")
        self.assertEqual(_text(screen), ["r3", "r4", "r5", "", "", ""])
        stream.feed(b"\x1b[6T")
        self.assertEqual(_text(screen), [""] * ROWS)

    def test_cursor_position_unchanged_even_outside_margins(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2;4r")  # DECSTBM homes the cursor
        stream.feed(b"\x1b[6;3H")  # row 6 is outside the region
        before = (screen.cursor.y, screen.cursor.x)
        stream.feed(b"\x1b[S")
        self.assertEqual((screen.cursor.y, screen.cursor.x), before)
        stream.feed(b"\x1b[2;5H\x1b[T")
        self.assertEqual((screen.cursor.y, screen.cursor.x), (1, 4))
        self.assertEqual(_text(screen)[0], "r0")
        self.assertEqual(_text(screen)[5], "r5")

    def test_scroll_ignores_cursor_when_outside_region(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2;4r\x1b[6;1H\x1b[S")
        self.assertEqual(_text(screen), ["r0", "r2", "r3", "", "r4", "r5"])

    def test_blank_lines_use_cursor_background(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[44m\x1b[S")
        blank_row = screen.buffer[ROWS - 1]
        for column in range(screen.columns):
            self.assertEqual(blank_row[column].bg, "blue")
            self.assertEqual(blank_row[column].data, " ")
        self.assertEqual(screen.buffer[0][0].bg, "default")

    def test_sd_blank_lines_use_cursor_background_within_margins(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2;4r\x1b[41m\x1b[2T")
        for row in (1, 2):
            for column in range(screen.columns):
                self.assertEqual(screen.buffer[row][column].bg, "red")
        self.assertEqual(screen.display[3].rstrip(), "r1")
        self.assertEqual(screen.buffer[0][0].bg, "default")
        self.assertEqual(screen.buffer[4][0].bg, "default")

    def test_blank_lines_default_attrs_when_cursor_default(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[S")
        for column in range(screen.columns):
            self.assertEqual(screen.buffer[ROWS - 1][column].bg, "default")

    def test_moved_lines_keep_their_attributes(self):
        screen, stream = _make()
        stream.feed(b"\x1b[3;1H\x1b[31mred\x1b[m")
        stream.feed(b"\x1b[S")
        self.assertEqual(screen.buffer[1][0].fg, "red")
        self.assertEqual(screen.buffer[1][0].data, "r")

    def test_su_su_ncurses_pattern(self):
        # ncurses hardware scroll: set region, scroll up, restore region.
        screen, stream = _make(lines=37, columns=20)
        for row in range(37):
            stream.feed(f"\x1b[{row + 1};1Hrow{row}".encode())
        stream.feed(b"\x1b[5;37r\x1b[2S\x1b[r")
        text = _text(screen)
        self.assertEqual(text[:4], ["row0", "row1", "row2", "row3"])
        self.assertEqual(text[4], "row6")
        self.assertEqual(text[34], "row36")
        self.assertEqual(text[35:], ["", ""])
        self.assertIsNone(screen.margins)

    def test_su_ncurses_pattern_single_feed_and_chunked(self):
        raw = b"\x1b[5;37r\x1b[2S\x1b[r"
        expected = None
        for chunks in ([raw], [raw[i:i + 1] for i in range(len(raw))]):
            screen, stream = _make(lines=37, columns=20)
            for row in range(37):
                stream.feed(f"\x1b[{row + 1};1Hrow{row}".encode())
            for chunk in chunks:
                stream.feed(chunk)
            text = _text(screen)
            if expected is None:
                expected = text
            self.assertEqual(text, expected)

    def test_dirty_rows_are_marked(self):
        screen, stream = _make()
        _fill(stream)
        screen.dirty.clear()
        stream.feed(b"\x1b[2;4r\x1b[S")
        self.assertTrue({1, 2, 3} <= screen.dirty)
        screen.dirty.clear()
        stream.feed(b"\x1b[T")
        self.assertTrue({1, 2, 3} <= screen.dirty)

    def test_history_consistent_with_index_and_reverse_index(self):
        ref, ref_stream = _make()
        _fill(ref_stream)
        ref_stream.feed(b"\x1b[6;1H\n\n")  # two IND at bottom margin
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2S")
        self.assertEqual(_text(screen), _text(ref))
        self.assertEqual(
            ["".join(c.data for c in line.values()).rstrip() for line in screen.history.top],
            ["".join(c.data for c in line.values()).rstrip() for line in ref.history.top],
        )
        ref, ref_stream = _make()
        _fill(ref_stream)
        ref_stream.feed(b"\x1b[1;1H\x1bM\x1bM")
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2T")
        self.assertEqual(_text(screen), _text(ref))
        self.assertEqual(len(screen.history.bottom), len(ref.history.bottom))

    def test_private_and_multi_param_forms_are_ignored(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[?2S")  # graphics attribute query, not SU
        stream.feed(b"\x1b[1;2;3;4;5T")  # mouse highlight tracking, not SD
        self.assertEqual(_text(screen), ["r0", "r1", "r2", "r3", "r4", "r5"])

    def test_alternate_screen_scrolls_alternate_only(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[?1049h")
        for row in range(ROWS):
            stream.feed(f"\x1b[{row + 1};1Ha{row}".encode())
        stream.feed(b"\x1b[2;5r\x1b[S")
        self.assertEqual(_text(screen), ["a0", "a2", "a3", "a4", "", "a5"])
        stream.feed(b"\x1b[?1049l")
        self.assertEqual(_text(screen), ["r0", "r1", "r2", "r3", "r4", "r5"])
        self.assertIsNone(screen.margins)

    def test_alternate_screen_margins_do_not_leak_to_primary(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[?1049h\x1b[2;3r\x1b[?1049l\x1b[S")
        self.assertEqual(_text(screen), ["r1", "r2", "r3", "r4", "r5", ""])

    def test_sd_then_su_round_trip_restores_middle(self):
        screen, stream = _make()
        _fill(stream)
        stream.feed(b"\x1b[2T\x1b[2S")
        self.assertEqual(_text(screen), ["r0", "r1", "r2", "r3", "", ""])


if __name__ == "__main__":
    unittest.main()
