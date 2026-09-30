"""Negative predicates for the bounded color and outer compatibility evidence."""
from __future__ import annotations

import copy
import unittest

import live_color_probe as color
import live_outer_compat_probe as outer
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.ui.terminal_g1 import app


class ColorEvidenceTests(unittest.TestCase):
    def screens(self):
        source, rendered = TerminalScreen(30, 3), TerminalScreen(100, 8)
        lines = (b"\x1b[38;2;53;175;234mG1_COLOR_NORMAL", b"\x1b[38;2;156;163;176m\x1b[1mG1_COLOR_BOLD",
                 b"\x1b[0;3;38;2;0;180;255mG1_COLOR_ITALIC")
        for index, data in enumerate(lines):
            make_stream(source).feed(f"\x1b[{index + 1};1H".encode() + data)
            make_stream(rendered).feed(f"\x1b[{index + 3};2H".encode() + data)
        return source, rendered

    def test_exact_color_and_emphasis_mapping(self):
        source, rendered = self.screens()
        self.assertTrue(color.compare(source, rendered)["exact_rgb_and_emphasis"])
        for key, value, y in (("fg", "cyan", 2), ("bold", False, 3), ("italics", False, 4), ("data", "X", 2)):
            with self.subTest(key=key):
                source, rendered = self.screens()
                rendered.buffer[y][1] = rendered.buffer[y][1]._replace(**{key: value})
                self.assertFalse(color.compare(source, rendered)["exact_rgb_and_emphasis"])

    def test_static_parser_only_unmatched_and_dirty_results_cannot_pass(self):
        keys = ("live_omp_ready", "same_output_replayed", "real_renderer_ready", "matched_geometry",
                "rgb_sgr_in_original_bytes", "rgb_sgr_in_rendered_bytes", "exact_rgb_and_emphasis", "cleaned")
        positive = dict.fromkeys(keys, True)
        self.assertTrue(color.color_passed(positive))
        for key in keys:
            with self.subTest(key=key):
                self.assertFalse(color.color_passed({**positive, key: False}))

    def test_rgb_style_keeps_foreground_background_and_emphasis(self):
        source, _ = self.screens()
        cell = source.buffer[1][0]._replace(bg="102030", italics=True, underscore=True)
        style = app._rgb_cell_style(cell)
        self.assertIn("38;2;156;163;176", style)
        self.assertIn("48;2;16;32;48", style)
        self.assertTrue(style.startswith("\x1b[0;1;3;4;"))

    def test_private_cpr_and_unknown_private_dsr_are_not_public_claims(self):
        replies = []
        screen = TerminalScreen(80, 24, reply=replies.append)
        make_stream(screen).feed(b"\x1b[4;9H\x1b[?6n\x1b[?996n\x1b[5n")
        self.assertEqual(replies, [b"\x1b[?4;9R", b"\x1b[0n"])


class OuterEvidenceTests(unittest.TestCase):
    def positive(self):
        keys = ("startup_ready", "fresh_input", "three_pane_routing", "paste_fresh_and_routed",
                "resize_propagated", "local_alternate_restored", "children_cleaned", "outer_cleaned",
                "isolated_session_paths", "non_nested_attach", "default_state_unchanged", "session_deleted")
        return {**dict.fromkeys(keys, True), "mode": "herdr", "fresh_before": [True] * 3,
                "fresh_after": [True] * 3, "paste_checks": [True] * 3,
                "initial": {"target": [True] * 3, "titles": [True] * 3,
                            "cross_routed": [False] * 3, "child_pids": [1, 2, 3],
                            "outer": [240, 45], "app": [240, 44], "child_sizes": [[78, 40]] * 3},
                "sizes": [{"stable_pids": True, "cross_routed": False, "propagated": True,
                           "outer": [210, 39], "app": [210, 38], "children": [[68, 34]] * 3},
                          {"stable_pids": True, "cross_routed": False, "propagated": True,
                           "outer": [240, 45], "app": [240, 44], "children": [[78, 40]] * 3}]}

    def test_freshness_cross_route_geometry_restore_and_cleanup_required(self):
        positive = self.positive()
        self.assertTrue(outer.passed(positive))
        for key in ("startup_ready", "fresh_input", "three_pane_routing", "paste_fresh_and_routed",
                    "resize_propagated", "local_alternate_restored", "children_cleaned", "outer_cleaned",
                    "isolated_session_paths", "non_nested_attach", "default_state_unchanged", "session_deleted"):
            with self.subTest(key=key):
                self.assertFalse(outer.passed({**positive, key: False}))
        for key in ("fresh_before", "fresh_after", "paste_checks"):
            self.assertFalse(outer.passed({**positive, key: [True, False, True]}))
        for key, value in (("target", [True, False, True]), ("cross_routed", [False, True, False]),
                           ("child_pids", [1, 1, 3])):
            result = copy.deepcopy(positive)
            result["initial"][key] = value
            self.assertFalse(outer.passed(result))
        for key, value in (("stable_pids", False), ("cross_routed", True)):
            result = copy.deepcopy(positive)
            result["sizes"][0][key] = value
            self.assertFalse(outer.passed(result))
        result = copy.deepcopy(positive)
        result["sizes"][0]["children"] = [[68, 35]] * 3
        self.assertFalse(outer.passed(result))

    def test_herdr_outer_geometry_and_initial_restored_geometry_required(self):
        positive = self.positive()
        positive["initial"]["child_sizes"] = [[78, 40]] * 3
        self.assertTrue(outer.passed(positive))
        for index in range(2):
            for geometry in (None, [180, 30]):
                with self.subTest(index=index, geometry=geometry):
                    result = copy.deepcopy(positive)
                    result["sizes"][index]["outer"] = geometry
                    self.assertFalse(outer.passed(result))
        result = copy.deepcopy(positive)
        result["initial"]["child_sizes"] = [[78, 41]] * 3
        self.assertFalse(outer.passed(result))
        for key in ("outer", "app", "child_sizes"):
            with self.subTest(missing_initial=key):
                result = copy.deepcopy(positive)
                del result["initial"][key]
                self.assertFalse(outer.passed(result))
        for key in ("outer", "app"):
            result = copy.deepcopy(positive)
            result["initial"][key] = [180, 30]
            self.assertFalse(outer.passed(result))


if __name__ == "__main__":
    unittest.main()
