"""Independent G1 boundary regressions, not additional gate success flags."""
from __future__ import annotations

import copy
import io
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import live_outer_compat_probe as outer
import test_closure_predicates as predicates
from test_g1_candidate import (_RecordingWindow, _ScriptedSelector, _ScriptedSession,
                               _run_fake_ui)
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.ui.terminal_g1 import app


class PrivateQueryRegressionTests(unittest.TestCase):
    def test_private_dsr_keeps_public_reply_fifo_under_partial_writes(self):
        expected = b"\x1b[?4;9R\x1b[0n\x1b[4;9R\x1b[?6c"
        manager = _ScriptedSession(101, output=b"\x1b[4;9H\x1b[?6n\x1b[?996n\x1b[5n\x1b[6n\x1b[c",
                                   acceptances=[0, 1, 0, 2, 3])
        selector = _ScriptedSelector(manager, [], exit_when=lambda: bytes(manager.received) == expected)
        _run_fake_ui(_RecordingWindow(), [manager, _ScriptedSession(102), _ScriptedSession(103)],
                     selector, lambda _fd, _limit: selector.chunks.pop(0))
        self.assertEqual(manager.received, expected)
        self.assertEqual(selector.chunks, [])

    def test_byte_fragmented_private_cpr_origin_and_resize_do_not_invent_996_reply(self):
        replies = []
        screen = TerminalScreen(80, 24, reply=replies.append)
        stream = make_stream(screen)
        for byte in b"\x1b[3;20r\x1b[?6h\x1b[4;9H\x1b[?996n\x1b[?6n\x1b[5n":
            stream.feed(bytes([byte]))
        self.assertEqual(replies, [b"\x1b[?4;9R", b"\x1b[0n"])
        screen.resize(lines=30, columns=90)
        stream.feed(b"\x1b[?6l\x1b[30;90H\x1b[?996n\x1b[?6n")
        self.assertEqual(replies[-1], b"\x1b[?30;90R")
        self.assertEqual(len(replies), 3)


class RgbDrawRegressionTests(unittest.TestCase):
    def test_rgb_overlay_preserves_wide_text_styles_and_cursor_on_redraw(self):
        source = TerminalScreen(28, 20)
        make_stream(source).feed("\x1b[2;3H\x1b[1;3;4;7;38;2;17;29;43;48;2;51;67;83m가XY".encode())
        outer_screen = TerminalScreen(90, 24)
        outer_stream = make_stream(outer_screen)
        window = _RecordingWindow()
        session = SimpleNamespace(pid=987654, poll=lambda: None, resize=lambda *_args: None)
        panes = [("MANAGER OMP", session, source),
                 ("WORKER OMP", session, TerminalScreen(28, 20)),
                 ("HOST SHELL", session, TerminalScreen(28, 20))]
        colors = SimpleNamespace(pair=lambda *_args: 0)
        for redraw in range(2):
            output = io.StringIO()
            with patch.dict(os.environ, {"COLORTERM": "truecolor"}), \
                    patch.object(app.sys, "stdout", output), \
                    patch.object(app.curses, "color_pair", return_value=0):
                with patch.multiple(app.curses, ACS_ULCORNER=0, ACS_LLCORNER=0, ACS_URCORNER=0,
                                    ACS_LRCORNER=0, ACS_HLINE=0, ACS_VLINE=0, create=True):
                    app._draw(window, panes, 0, colors)
            # The cursor established by curses must survive the RGB overlay.
            outer_stream.feed(b"\x1b[10;11H" + output.getvalue().encode())
            self.assertEqual((outer_screen.cursor.y, outer_screen.cursor.x), (9, 10))
            for x in (2, 3, 4, 5):
                expected, actual = source.buffer[1][x], outer_screen.buffer[3][x + 1]
                for key in ("data", "fg", "bg", "bold", "italics", "underscore", "reverse"):
                    # A wide glyph's empty continuation is governed by its lead cell.
                    if not expected.data and key != "data":
                        continue
                    self.assertEqual(getattr(actual, key), getattr(expected, key), (redraw, x, key))
            # Same position, changed data/style: redraw must not reuse stale SGR.
            make_stream(source).feed(b"\x1b[2;3H\x1b[0;38;2;91;103;117mABCD")


class OuterBoundaryRegressionTests(unittest.TestCase):
    def positive(self):
        result = predicates.OuterEvidenceTests().positive()
        for item, geometry in zip(result["sizes"], ([210, 39], [240, 45])):
            item["outer"] = geometry
            item["app"] = geometry
            item["children"] = [[geometry[0] // 3 - 2, geometry[1] - 4]] * 3
        result["mode"] = "plain"
        result["initial"]["app"] = [240, 45]
        result["initial"]["child_sizes"] = [[78, 41]] * 3
        return result

    def test_invalid_missing_or_extra_child_pid_cannot_prove_three_live_panes(self):
        positive = self.positive()
        self.assertTrue(outer.passed(positive))
        for pids in ([0, 2, 3], [-1, 2, 3], [None, 2, 3], [True, 2, 3], [1, 2, 3, 3]):
            with self.subTest(pids=pids):
                result = copy.deepcopy(positive)
                result["initial"]["child_pids"] = pids
                self.assertFalse(outer.passed(result))

    def test_self_consistent_children_do_not_hide_wrong_plain_outer_geometry(self):
        positive = self.positive()
        self.assertTrue(outer.passed(positive))
        result = copy.deepcopy(positive)
        result["sizes"][0]["app"] = [180, 30]
        result["sizes"][0]["children"] = [[58, 26]] * 3
        self.assertFalse(outer.passed(result))

    def test_arbitrary_inherited_herdr_selector_is_removed_before_version_preflight(self):
        seen = []

        def preflight(argv, environment):
            seen.append(environment)
            return SimpleNamespace(stdout="unsupported version")

        with patch.dict(os.environ, {"HERDR_CONFIG_PATH": "/never-use-user-config",
                                    "HERDR_FUTURE_SELECTOR": "stale"}), \
                patch.object(outer.shutil, "which", return_value="/test/omp"), \
                patch.object(outer, "call", preflight):
            outer.run(herdr_mode=True)
        self.assertEqual(len(seen), 1)
        self.assertFalse(any(key.startswith("HERDR_") for key in seen[0]))


if __name__ == "__main__":
    unittest.main()
