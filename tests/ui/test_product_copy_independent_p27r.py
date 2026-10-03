"""Independent C-D62 (2) drag-to-copy checks (p27-copy-test-01).

Expectations were written from DECISIONS.md C-D62 (2) and the worker packet (required_behavior + facts) before the
implementation was read:

- A left drag in a pane that does not track the mouse selects text of THAT pane only (clamped to its inner area, never
  a border or another pane); release copies. A click without movement is only a focus click. Divider drags, the wheel,
  click-to-focus and forwarding to panes whose app enabled 1000/1002/1003 are unchanged (no Workbench selection there).
- The selection lives in pane content coordinates (scrollback included): scrolling or new output does not change what
  is selected; dragging past the top/bottom inner edge auto-scrolls. Resize, alternate screen, zoom, focus change and a
  reset/replaced/caught-up pane end it. Content that disappeared must never be copied as something else.
- Extraction: rows in order joined with ``\\n``; wide characters once whichever half the selection starts/ends on;
  combining characters kept; tabs are the spaces they rendered as; trailing spaces trimmed per row; empty -> nothing;
  more than 1 MiB of UTF-8 -> refused with a notice, exactly 1 MiB -> copied.
- Copy = ``ESC ] 52 ; c ; <base64> BEL`` queued by the model and written by the app loop to the UI's own stdout; inside
  tmux (TMUX set) also ``ESC P tmux; <same with ESC doubled> ESC \\``. No subprocess/tmux/clipboard program is run,
  nothing is written during terminal restore, a frame being drawn is never split.

Run: PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/ui -p test_product_copy_independent_p27r.py
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import unicodedata
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from support import FakeSender, FixtureServer, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product import model as model_module  # noqa: E402
from workbench.ui.product.model import ProductModel, pane_boxes  # noqa: E402

MGR, WRK, HOST = PaneId.MANAGER_OMP, PaneId.WORKER_OMP, PaneId.HOST_SHELL
PREFIX = b"\x1d"
OSC_PLAIN = re.compile(rb"\x1b\]52;c;([A-Za-z0-9+/]*={0,2})\x07")
PRODUCT_SRC = Path(model_module.__file__).resolve().parent


def sgr(button: int, x: int, y: int, release: bool = False) -> bytes:
    return b"\x1b[<%d;%d;%d%s" % (button, x, y, b"m" if release else b"M")


def osc52(text: str, tmux: bool = False) -> bytes:
    plain = b"\x1b]52;c;" + base64.b64encode(text.encode("utf-8")) + b"\x07"
    return plain + (b"\x1bPtmux;" + plain.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\" if tmux else b"")


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


class Base(unittest.TestCase):
    rows, cols = 30, 120

    def setUp(self) -> None:
        self.clock = Clock()
        self.sender = FakeSender()
        self.model = self.new_model()

    def new_model(self, rows=None, cols=None, environ=None) -> ProductModel:
        model = ProductModel(self.sender, rows or self.rows, cols or self.cols, clock=lambda: 1000.0,
                             monotonic=self.clock, environ={} if environ is None else environ)
        model.apply_snapshot(snapshot())
        return model

    # -- helpers -------------------------------------------------------------------------------------------------------
    def feed(self, pane: PaneId, data, gen: int = 1, model=None) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        (model or self.model).on_display(ui_v1.Frame({"pane": pane.value, "session_id": "s", "generation": gen}, data))

    def at(self, pane: PaneId, col: int, row: int, model=None) -> tuple[int, int]:
        """1-based terminal cell of the pane-local 0-based (col, row) in the CURRENT layout (may be outside the pane)."""
        m = model or self.model
        top, left, _, _ = pane_boxes(m.rows, m.cols, m.layout, m.zoom)[pane]
        return left + 2 + col, top + 2 + row

    def mouse(self, pane, button, col, row, release=False, model=None) -> None:
        x, y = self.at(pane, col, row, model)
        (model or self.model).handle_input(sgr(button, x, y, release))

    def press(self, pane, col, row, model=None):
        self.mouse(pane, 0, col, row, model=model)

    def move(self, pane, col, row, model=None):
        self.mouse(pane, 32, col, row, model=model)

    def release(self, pane, col, row, model=None):
        self.mouse(pane, 0, col, row, release=True, model=model)

    def drag(self, pane, start, end, model=None) -> None:
        self.press(pane, *start, model=model)
        self.move(pane, *end, model=model)
        self.release(pane, *end, model=model)

    def copied(self, model=None) -> str | None:
        """The text of the queued OSC 52 (checked for the exact plain form); None when nothing was queued."""
        out = (model or self.model).take_output()
        if not out:
            return None
        match = OSC_PLAIN.match(out)
        self.assertIsNotNone(match, out[:80])
        self.assertEqual(out[:match.end()], osc52(base64.b64decode(match.group(1), validate=True).decode()))
        return base64.b64decode(match.group(1), validate=True).decode("utf-8")

    def spans(self, pane, model=None):
        m = model or self.model
        return m.selection_spans(pane, m.sizes[pane][0])

    def visible(self, pane, model=None) -> list[str]:
        m = model or self.model
        screen = m.panes[pane].screen
        out = []
        for line in m.pane_lines(pane, m.sizes[pane][0]):
            out.append("".join(line.get(x, screen.default_char).data for x in range(screen.columns)).rstrip(" "))
        return out


# ---------------------------------------------------------------------------------------------------------------------
class ExtractionTests(Base):
    def test_single_row_substring_is_exact(self):
        self.feed(MGR, "alpha beta gamma")
        self.drag(MGR, (6, 0), (9, 0))
        self.assertEqual("beta", self.copied())
        self.assertEqual("복사됨: 4자", self.model.notice)  # no tmux hint outside tmux

    def test_rows_in_order_trailing_spaces_trimmed_interior_kept_empty_rows_kept(self):
        self.feed(HOST, "a  b   \r\n\r\n  c d  \r\nlast")
        self.drag(HOST, (0, 0), (3, 3))
        self.assertEqual("a  b\n\n  c d\nlast", self.copied())

    def test_trailing_spaces_are_trimmed_even_when_written_with_colour(self):
        self.feed(HOST, "word\x1b[41m    \x1b[0m\r\nnext")
        self.drag(HOST, (0, 0), (3, 1))
        self.assertEqual("word\nnext", self.copied())

    def test_wide_characters_once_whichever_half_the_selection_starts_or_ends_on(self):
        self.feed(MGR, "가나다ab")  # 가 0-1, 나 2-3, 다 4-5, a 6, b 7
        cases = {((2, 0), (3, 0)): "나", ((3, 0), (4, 0)): "나다", ((3, 0), (5, 0)): "나다",
                 ((0, 0), (2, 0)): "가나", ((1, 0), (6, 0)): "가나다a", ((5, 0), (7, 0)): "다ab"}
        for (start, end), want in cases.items():
            with self.subTest(start=start, end=end):
                self.drag(MGR, start, end)
                self.assertEqual(want, self.copied())
                self.drag(MGR, end, start)  # backward drag: the same text
                self.assertEqual(want, self.copied())

    def test_wide_character_on_a_row_boundary_of_a_multi_row_selection(self):
        self.feed(HOST, "xx한글\r\n글자yy")
        self.drag(HOST, (3, 0), (2, 1))  # starts on the trailing half of 한, ends on the first half of 자
        self.assertEqual("한글\n글자", self.copied())

    def test_combining_characters_are_kept(self):
        self.feed(HOST, "e\u0301te\u0308 x")
        self.drag(HOST, (0, 0), (2, 0))
        self.assertEqual(unicodedata.normalize("NFC", "e\u0301te\u0308"), unicodedata.normalize("NFC", self.copied()))

    def test_emoji(self):
        self.feed(WRK, "\U0001F600ok")
        self.drag(WRK, (0, 0), (3, 0))
        self.assertEqual("\U0001F600ok", self.copied())
        self.drag(WRK, (1, 0), (2, 0))  # starts on the trailing half of the emoji
        self.assertEqual("\U0001F600o", self.copied())

    def test_tabs_are_the_spaces_they_rendered_as(self):
        self.feed(HOST, "a\tb\r\n\tc")
        self.drag(HOST, (0, 0), (8, 1))
        self.assertEqual("a" + " " * 7 + "b\n" + " " * 8 + "c", self.copied())

    def test_empty_selection_copies_nothing(self):
        self.feed(HOST, "top")
        self.drag(HOST, (10, 3), (30, 5))
        self.assertIsNone(self.copied())
        self.assertNotIn("복사됨", self.model.notice)

    def test_notice_counts_characters_not_bytes(self):
        self.feed(MGR, "한글 ok")
        self.drag(MGR, (0, 0), (6, 0))
        self.assertEqual("한글 ok", self.copied())
        self.assertEqual("복사됨: 5자", self.model.notice)


class CapBoundaryTests(Base):
    """The real MAX_COPY_BYTES (1 MiB): a selection of exactly 1 MiB copies, one byte more is refused with a notice."""

    def test_exactly_one_mebibyte_copies_and_one_byte_more_is_refused(self):
        self.assertEqual(1024 * 1024, model_module.MAX_COPY_BYTES)
        model = self.new_model(rows=2210, cols=1026)  # host inner area: 1101 rows x 1024 columns
        self.assertGreaterEqual(model.sizes[HOST][0], 1048)
        lines = [f"{i:05d}" + "x" * 995 for i in range(1048)]  # 1000 cells each
        self.feed(HOST, "\r\n".join(lines), model=model)
        # rows 0..1046 whole (1000 bytes + \n) + 529 bytes of row 1047 = 1047 * 1001 + 529 = 1 048 576
        self.drag(HOST, (0, 0), (528, 1047), model=model)
        out = model.take_output()
        self.assertTrue(out.startswith(b"\x1b]52;c;") and out.endswith(b"\x07"), model.notice)
        text = base64.b64decode(out[7:-1], validate=True).decode()
        self.assertEqual(1024 * 1024, len(text.encode()))
        self.assertEqual("\n".join(lines[:1047] + [lines[1047][:529]]), text)
        self.drag(HOST, (0, 0), (529, 1047), model=model)  # one byte more
        self.assertEqual(b"", model.take_output())
        self.assertIn("1 MiB", model.notice)
        self.assertNotIn("복사됨", model.notice)


# ---------------------------------------------------------------------------------------------------------------------
class SelectionModelTests(Base):
    def setUp(self) -> None:
        super().setUp()
        for pane in (MGR, WRK, HOST):
            self.feed(pane, f"{pane.value}-row0\r\n{pane.value}-row1\r\n{pane.value}-row2")
        self.sender.sent.clear()

    def test_drag_leaving_the_pane_into_other_panes_is_clamped_to_the_start_pane(self):
        self.press(WRK, 3, 1)
        for pane, col, row in ((MGR, 5, 1), (HOST, 5, 1), (MGR, 1, 0)):  # over the manager, the host, the borders
            x, y = self.at(pane, col, row)
            self.model.handle_input(sgr(32, x, y))
            self.assertEqual({}, self.spans(MGR))
            self.assertEqual({}, self.spans(HOST))
        x, y = self.at(HOST, 4, 2)
        self.model.handle_input(sgr(0, x, y, release=True))
        text = self.copied()  # released below the worker: clamped to its bottom row (empty rows = empty lines)
        rows = self.model.sizes[WRK][0]
        self.assertEqual("ker_omp-row1\nworker_omp-row2" + "\n" * (rows - 3), text)
        self.assertNotIn("manager", text)
        self.assertNotIn("host", text)
        self.assertEqual([], [s for s in self.sender.sent if s[0] == "input"])  # nothing reached any pane

    def test_drag_from_the_host_up_over_the_divider_stays_in_the_host(self):
        self.press(HOST, 12, 2)
        x, y = self.at(MGR, 2, 0)  # inside the manager pane, straight above the host
        for _ in range(3):
            self.clock.now += 1
            self.model.handle_input(sgr(32, x, y))
        self.assertEqual({}, self.spans(MGR))
        self.model.handle_input(sgr(0, x, y, release=True))
        self.assertEqual("st_shell-row0\nhost_shell-row1\nhost_shell-ro", self.copied())
        self.assertEqual(self.model.layout, model_module.DEFAULT_LAYOUT)  # never a divider drag

    def test_click_without_move_is_only_a_focus_click(self):
        self.press(HOST, 4, 1)
        self.release(HOST, 4, 1)
        self.assertIs(HOST, self.model.focus)
        self.assertEqual(["focus"], [s[0] for s in self.sender.sent])
        self.assertIsNone(self.copied())
        self.assertEqual({}, self.spans(HOST))
        self.assertNotIn("복사", self.model.notice)

    def test_motion_that_returns_to_the_press_cell_copies_nothing(self):
        self.press(MGR, 4, 1)
        self.move(MGR, 8, 1)
        self.move(MGR, 4, 1)
        self.release(MGR, 4, 1)
        self.assertIsNone(self.copied())

    def test_divider_drag_moves_the_divider_and_selects_nothing(self):
        top, left, height, width = pane_boxes(self.model.rows, self.model.cols)[MGR]
        x, y = left + width, top + 3  # the manager's right border (1-based) = the vertical divider
        self.model.handle_input(sgr(0, x, y))
        self.model.handle_input(sgr(32, x + 5, y))
        self.model.handle_input(sgr(0, x + 5, y, release=True))
        self.assertNotEqual(model_module.DEFAULT_LAYOUT, self.model.layout)
        self.assertIsNone(self.copied())
        for pane in (MGR, WRK, HOST):
            self.assertEqual({}, self.spans(pane))

    def test_wheel_scrolls_as_before(self):
        self.feed(HOST, "".join(f"\r\nmore {i}" for i in range(60)))
        self.mouse(HOST, 64, 3, 3)
        self.assertEqual(3, self.model.scroll_offset(HOST))
        self.assertIsNone(self.copied())

    def test_x10_mode_1000_pane_gets_press_and_release_but_no_motion_and_no_selection(self):
        self.feed(WRK, "\x1b[?1000h")
        self.model.set_focus(WRK)
        self.sender.sent.clear()
        self.drag(WRK, (0, 0), (5, 1))
        got = [s[2] for s in self.sender.sent if s[0] == "input"]
        self.assertEqual([b"\x1b[M" + bytes((32, 33, 33)), b"\x1b[M" + bytes((35, 38, 34))], got)
        self.assertIsNone(self.copied())
        self.assertEqual({}, self.spans(WRK))

    def test_any_motion_1003_sgr_pane_gets_every_report_and_no_selection(self):
        self.feed(MGR, "\x1b[?1003h\x1b[?1006h")
        self.drag(MGR, (0, 0), (5, 1))
        got = [s[2] for s in self.sender.sent if s[0] == "input"]
        self.assertEqual([b"\x1b[<0;1;1M", b"\x1b[<32;6;2M", b"\x1b[<0;6;2m"], got)
        self.assertIsNone(self.copied())
        self.assertEqual({}, self.spans(MGR))

    def test_tracking_turned_off_again_gives_the_selection_back(self):
        self.feed(MGR, "\x1b[?1002h\x1b[?1006h\x1b[?1002l")
        self.drag(MGR, (0, 0), (10, 0))
        self.assertEqual("manager_omp", self.copied())

    def test_autoscroll_from_the_loop_tick_reaches_the_oldest_line_and_stops_there(self):
        self.feed(MGR, "".join(f"\r\nhist {i:03d}" for i in range(40)))
        self.press(MGR, 0, 3)
        x, y = self.at(MGR, 4, -1)  # the pane's top border
        self.model.handle_input(sgr(32, x, y))
        steps = 0
        for _ in range(200):
            self.clock.now += 1
            steps += bool(self.model.autoscroll_tick())
        history = len(self.model.panes[MGR].screen.history.top)
        self.assertEqual(history, self.model.scroll_offset(MGR))
        self.model.handle_input(sgr(0, x, y, release=True))
        text = self.copied()
        self.assertTrue(text.startswith("ger_omp-row0\nmanager_omp-row1\n"), text[:40])
        self.assertIn("hist 000", text)
        self.assertEqual(text.count("\n"), history + 3)

    def test_scrolled_back_selection_is_anchored_while_new_output_arrives(self):
        self.feed(MGR, "".join(f"\r\nold {i:03d}" for i in range(80)))
        for _ in range(5):
            self.mouse(MGR, 64, 2, 2)  # 15 lines back
        before = self.visible(MGR)
        self.press(MGR, 0, 1)
        self.move(MGR, 6, 3)
        self.feed(MGR, "".join(f"\r\nnew {i:03d}" for i in range(50)))  # arrives mid-drag
        self.assertEqual(before, self.visible(MGR))  # the scrolled view stays where it was
        self.release(MGR, 6, 3)
        self.assertEqual("\n".join([before[1], before[2], before[3][:7]]), self.copied())
        self.feed(MGR, "".join(f"\r\nlater {i:03d}" for i in range(5)))
        self.assertEqual({1: (0, self.model.sizes[MGR][1] - 1), 2: (0, self.model.sizes[MGR][1] - 1), 3: (0, 6)},
                         self.spans(MGR))

    def test_kept_highlight_follows_its_text_when_live_output_scrolls(self):
        pane = HOST
        self.drag(pane, (0, 1), (8, 1))
        self.assertEqual("host_shel", self.copied())
        self.assertEqual({1: (0, 8)}, self.spans(pane))
        rows = self.model.sizes[pane][0]
        self.feed(pane, "\r\n" * (rows - 1))  # the text moves up and out of the live view
        self.assertEqual({}, self.spans(pane))
        self.mouse(pane, 64, 1, 1)
        self.mouse(pane, 64, 1, 1)
        where = [y for y, text in enumerate(self.visible(pane)) if text.startswith("host_shell-row1")]
        self.assertEqual({where[0]: (0, 8)}, self.spans(pane))

    def test_evicted_history_under_a_selection_is_never_copied_as_other_text(self):
        """History trimmed (1000-line OMP scrollback) under a scrolled-back drag: copy the old text or nothing."""
        self.feed(MGR, "".join(f"\r\nold {i:04d}" for i in range(1000)))
        self.model.scroll_by(10_000, MGR)  # to the oldest line
        selected = self.visible(MGR)[0:2]
        self.press(MGR, 0, 0)
        self.move(MGR, 7, 1)
        self.feed(MGR, "".join(f"\r\nflood {i:04d}" for i in range(600)))  # evicts the selected lines
        self.release(MGR, 7, 1)
        text = self.copied()
        self.assertIn(text, (None, "\n".join(selected)), f"copied {text!r} instead of {selected!r}")

    def test_catch_up_during_a_drag_ends_the_selection(self):
        self.press(HOST, 0, 0)
        self.move(HOST, 6, 1)
        chunk = b"y\r\n" * (model_module.CATCHUP_BACKLOG_BYTES // 3 + 10)
        self.model.enqueue_display(ui_v1.Frame({"pane": "host_shell", "session_id": "s", "generation": 1}, chunk))
        while self.model.feed_pending(max_bytes=1 << 30, max_seconds=30):
            pass
        self.assertEqual({}, self.spans(HOST))
        self.release(HOST, 6, 1)
        self.assertIsNone(self.copied())

    def test_replaced_session_replay_ends_the_selection(self):
        self.press(WRK, 0, 0)
        self.move(WRK, 6, 1)
        frame = ui_v1.Frame({"pane": "worker_omp", "session_id": "s", "generation": 2, "replay": True}, b"replayed text")
        self.model.on_display(frame)
        self.release(WRK, 6, 1)
        self.assertIsNone(self.copied())
        self.assertEqual({}, self.spans(WRK))

    def test_terminal_reset_by_the_pane_app_ends_the_highlight(self):
        """RIS (ESC c) wipes the pane's content and history: the highlight must not stay on the blank screen."""
        self.drag(HOST, (0, 0), (8, 1))
        self.assertTrue(self.copied())
        self.feed(HOST, "\x1bc")
        self.assertEqual({}, self.spans(HOST))

    def test_resize_during_a_drag_ends_it(self):
        self.press(MGR, 0, 0)
        self.move(MGR, 6, 1)
        self.model.resize(34, 126)
        self.assertEqual({}, self.spans(MGR))
        self.release(MGR, 6, 1)
        self.assertIsNone(self.copied())

    def test_zoom_ends_a_kept_highlight_and_a_drag(self):
        self.drag(MGR, (0, 0), (6, 1))
        self.assertTrue(self.copied())
        self.model.handle_input(PREFIX + b"z")
        self.assertEqual({}, self.spans(MGR))
        self.press(MGR, 0, 0)
        self.move(MGR, 6, 1)
        self.model.toggle_zoom()  # not a key: only the layout changes
        self.release(MGR, 6, 1)
        self.assertIsNone(self.copied())

    def test_alternate_screen_entered_during_a_drag_ends_it(self):
        for mode in (47, 1047, 1049):
            with self.subTest(mode=mode):
                self.press(HOST, 0, 0)
                self.move(HOST, 6, 1)
                self.feed(HOST, f"\x1b[?{mode}hALT")
                self.release(HOST, 6, 1)
                self.assertIsNone(self.copied())
                self.assertEqual({}, self.spans(HOST))
                self.feed(HOST, f"\x1b[?{mode}l")

    def test_backend_focus_change_ends_the_highlight(self):
        self.drag(MGR, (0, 0), (6, 1))
        self.assertTrue(self.copied())
        self.model.apply_snapshot(snapshot(focus="worker_omp"))
        self.assertIs(WRK, self.model.focus)
        self.assertEqual({}, self.spans(MGR))

    def test_click_in_another_pane_ends_the_highlight_and_starts_nothing(self):
        self.drag(MGR, (0, 0), (6, 1))
        self.assertTrue(self.copied())
        self.press(WRK, 2, 0)
        self.release(WRK, 2, 0)
        self.assertEqual({}, self.spans(MGR))
        self.assertEqual({}, self.spans(WRK))
        self.assertIsNone(self.copied())


# ---------------------------------------------------------------------------------------------------------------------
class Osc52BytesTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.feed(HOST, "첫 줄 line\r\n둘째\tend")

    def test_exact_plain_sequence_for_multi_row_utf8(self):
        self.drag(HOST, (0, 0), (15, 1))
        out = self.model.take_output()
        want = "첫 줄 line\n둘째" + " " * 4 + "end"
        self.assertEqual(osc52(want), out)
        b64 = out[len(b"\x1b]52;c;"):-1]
        self.assertNotIn(b"\n", b64)
        self.assertEqual(want, base64.b64decode(b64, validate=True).decode())

    def test_tmux_passthrough_only_with_a_non_empty_tmux_and_every_inner_esc_doubled(self):
        for environ, tmux in (({}, False), ({"TMUX": ""}, False), ({"TMUX": "/tmp/tmux-1/x,9,0"}, True)):
            with self.subTest(environ=environ):
                model = self.new_model(environ=environ)
                self.feed(HOST, "copy me", model=model)
                self.drag(HOST, (0, 0), (6, 0), model=model)
                out = model.take_output()
                self.assertEqual(osc52("copy me", tmux), out)
                if tmux:
                    inner = out[out.index(b"\x1bPtmux;") + len(b"\x1bPtmux;"):-2]
                    self.assertNotRegex(inner.replace(b"\x1b\x1b", b""), rb"\x1b")
                    self.assertTrue(out.endswith(b"\x1b\\"))
                    self.assertIn("set-clipboard on", model.notice)
                else:
                    self.assertNotIn("tmux", model.notice)

    def test_output_is_taken_once(self):
        self.drag(HOST, (0, 0), (3, 0))
        self.assertTrue(self.model.take_output())
        self.assertEqual(b"", self.model.take_output())

    def test_copy_runs_no_program_and_the_model_writes_no_fd(self):
        def boom(*_a, **_k):
            raise AssertionError("a process was started or a fd written during a copy")

        model = self.new_model(environ={"TMUX": "/tmp/t,1,0"})
        self.feed(HOST, "no programs", model=model)
        patches = [mock.patch.object(subprocess, "Popen", boom), mock.patch.object(os, "system", boom),
                   mock.patch.object(os, "fork", boom), mock.patch.object(os, "write", boom),
                   mock.patch.object(os, "execv", boom), mock.patch.object(os, "execvp", boom),
                   mock.patch.object(os, "execve", boom), mock.patch.object(os, "posix_spawn", boom),
                   mock.patch.object(os, "posix_spawnp", boom), mock.patch.object(os, "popen", boom)]
        for patch in patches:
            patch.start()
        try:
            self.drag(HOST, (0, 0), (10, 0), model=model)
            out = model.take_output()
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(osc52("no programs", True), out)

    def test_product_ui_sources_name_no_clipboard_program_and_no_subprocess(self):
        for path in sorted(PRODUCT_SRC.glob("*.py")):
            source = path.read_text()
            with self.subTest(path=path.name):
                self.assertNotRegex(source, r"\bimport subprocess\b|\bfrom subprocess\b|os\.system|os\.popen|posix_spawn")
                self.assertNotRegex(source, r"xclip|xsel|wl-copy|pbcopy|load-buffer|set-buffer|clip\.exe")


# ---------------------------------------------------------------------------------------------------------------------
# The real curses loop on a PTY (fixture ui_v1 server, no backend, no OMP).
from test_product_pty import MOUSE_OFF, UiProcess  # noqa: E402


class Ui(UiProcess):
    def __init__(self, sock_path, size=(30, 120), extra_env=None):
        if extra_env:
            real = subprocess.Popen

            def popen(argv, **kw):
                kw["env"] = {**kw["env"], **extra_env}
                return real(argv, **kw)

            with mock.patch.object(subprocess, "Popen", popen):
                super().__init__(sock_path, size)
        else:
            super().__init__(sock_path, size)


class CopyPtyTests(unittest.TestCase):
    HOST_TEXT = ("plain \x1b[31mRED\x1b[0m \x1b[7mREV\x1b[0m 한글 end\r\n"
                 "second row here").encode()

    def start(self, extra_env=None):
        server = FixtureServer(replay={"host_shell": self.HOST_TEXT})
        self.addCleanup(server.close)
        ui = Ui(server.path, extra_env=extra_env)
        self.addCleanup(ui.close)
        self.assertTrue(server.wait_for(server.attached.is_set), "attach not received")
        self.assertTrue(ui.until(lambda: "second row here" in ui.text()), ui.text())
        top, left, _, _ = pane_boxes(30, 120)[HOST]
        self.x0, self.y0 = left + 2, top + 2  # 1-based cell of host (0, 0)
        return server, ui

    def drag(self, ui, c0, c1, row=0, extra=b""):
        y = self.y0 + row
        ui.send(sgr(0, self.x0 + c0, y) + sgr(32, self.x0 + c1, y) + sgr(0, self.x0 + c1, y, release=True) + extra)

    def test_highlight_on_colours_reverse_cells_and_wide_characters_then_a_key_ends_it(self):
        server, ui = self.start()
        row = self.y0 - 1
        before = {c: (ui.screen.buffer[row][c].reverse, ui.screen.buffer[row][c].fg) for c in range(self.x0 - 1, self.x0 + 25)}
        mark = len(ui.raw)
        self.drag(ui, 6, 17)  # "RED REV 한글" ends on the trailing half of 글
        want = "RED REV 한글"
        expected = osc52(want)
        self.assertTrue(ui.until(lambda: expected in bytes(ui.raw[mark:])), bytes(ui.raw[mark:])[-200:])
        self.assertTrue(ui.until(lambda: ui.screen.buffer[row][self.x0 - 1 + 6].reverse))
        line = ui.screen.buffer[row]
        for c in range(6, 18):
            cell, was = line[self.x0 - 1 + c], before[self.x0 - 1 + c]
            with self.subTest(col=c, data=cell.data):
                self.assertNotEqual(was[0], cell.reverse, "selected cell not visibly highlighted")
        self.assertEqual("red", line[self.x0 - 1 + 6].fg)  # the colour stays; only reverse video is toggled
        for c in (0, 5, 18, 19):
            self.assertEqual(before[self.x0 - 1 + c][0], line[self.x0 - 1 + c].reverse, f"col {c} highlighted")
        ui.send(b"x")  # the next key ends the highlight
        self.assertTrue(ui.until(lambda: not ui.screen.buffer[row][self.x0 - 1 + 6].reverse), "highlight stayed")
        self.assertTrue(server.wait_for(lambda: [f.payload for f in server.received("input")] == [b"x"]))

    def test_osc52_is_written_whole_between_frames_and_before_the_terminal_restore(self):
        server, ui = self.start()
        mark = len(ui.raw)
        self.drag(ui, 0, 4, row=1, extra=PREFIX + b"q")  # the release and the detach in one read
        self.assertEqual(0, ui.wait_exit())
        raw = bytes(ui.raw[mark:])
        expected = osc52("secon")
        self.assertEqual(1, raw.count(expected), raw[-400:])
        at = raw.index(expected)
        restore = raw.find(MOUSE_OFF[0], at)
        self.assertGreater(restore, at, "OSC 52 written after (or during) the terminal restore")
        prefix = bytes(ui.raw[:mark]) + raw[:at]
        # the byte stream before the OSC must not end inside a curses escape sequence (a frame is never split)
        self.assertNotRegex(prefix[-24:], rb"\x1b(\[[0-?]*[ -/]*|[()#]|\][^\x07\x1b]*|P[^\x1b]*)?$", prefix[-24:])

    def test_sigterm_right_after_a_copy_never_writes_osc52_after_the_restore(self):
        server, ui = self.start()
        mark = len(ui.raw)
        self.drag(ui, 0, 4, row=1)
        os.kill(ui.proc.pid, signal.SIGTERM)
        self.assertEqual(1, ui.wait_exit())
        raw = bytes(ui.raw[mark:])
        at = raw.find(osc52("secon"))
        if at >= 0:
            self.assertLess(at, raw.rfind(b"\x1b[?2004l"))
            self.assertLess(at, raw.rfind(MOUSE_OFF[0]))

    def test_tmux_env_in_the_ui_process_adds_the_passthrough_copy(self):
        server, ui = self.start(extra_env={"TMUX": "/tmp/wb-none/fake,1,0"})
        mark = len(ui.raw)
        self.drag(ui, 0, 4, row=1)
        expected = osc52("secon", tmux=True)
        self.assertTrue(ui.until(lambda: expected in bytes(ui.raw[mark:])), bytes(ui.raw[mark:])[-300:])
        self.assertTrue(ui.until(lambda: "allow-passthrough on" in ui.text()), ui.text()[-200:])
        self.assertEqual([], server.received("input"))


if __name__ == "__main__":
    unittest.main()
