"""Guards for evidence that must originate after the live resize."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import live_normal_resize_probe as normal
import live_three_pane_resize_probe as three
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


class ResizeEvidenceTests(unittest.TestCase):
    def test_old_screen_content_with_no_post_resize_bytes_is_not_redraw(self) -> None:
        screen = TerminalScreen(84, 24)
        make_stream(screen).feed(("π >\n" + normal.DRAFT).encode())
        with (
            patch.object(normal, "pump", return_value=(False, 0)),
            patch.object(normal, "child_tty_state", return_value=SimpleNamespace(size=(84, 24))),
        ):
            seen, count = normal.pump_surface(
                SimpleNamespace(pid=123), object(), screen, normal.DRAFT, "composer"
            )
        self.assertFalse(seen)
        self.assertEqual(count, 0)

    def test_provider_must_receive_original_draft_and_suffix_in_one_user_message(self) -> None:
        def body(messages: list[dict[str, str]]) -> bytes:
            return json.dumps({"messages": messages}, ensure_ascii=False).encode()

        self.assertTrue(normal.request_contains_full_draft(body([
            {"role": "user", "content": normal.DRAFT + "_INPUT_OK"},
        ])))
        self.assertFalse(normal.request_contains_full_draft(body([
            {"role": "user", "content": "_INPUT_OK"},
        ])))
        self.assertFalse(normal.request_contains_full_draft(body([
            {"role": "system", "content": normal.DRAFT + "_INPUT_OK"},
        ])))

    def test_three_pane_requires_three_stable_distinct_pids(self) -> None:
        stages = [{"child_pids": [11, 12, 13]} for _ in range(4)]
        self.assertTrue(three.stable_child_pids(stages))
        stages[-1] = {"child_pids": [11, 12, 14]}
        self.assertFalse(three.stable_child_pids(stages))
        stages[-1] = {"child_pids": [11, None, 13]}
        self.assertFalse(three.stable_child_pids(stages))
        self.assertFalse(three.stable_child_pids([{"child_pids": [11, 11, 13]}] * 4))


if __name__ == "__main__":
    unittest.main()
