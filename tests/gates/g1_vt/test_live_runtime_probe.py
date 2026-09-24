from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from live_runtime_probe import (
    ChildTtyState,
    MARKER,
    RESULT_NAME,
    approval_allowed,
    approval_surface_present_after_resize,
    approval_tool_scenario_passed,
    drain_until_quiet,
    normal_resize_distinguished,
    query_reply_tty_drained,
    tool_output_confirmed,
)


class LiveRuntimePredicateTests(unittest.TestCase):
    def test_approval_preview_is_not_tool_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result_file = Path(temporary) / RESULT_NAME
            self.assertFalse(tool_output_confirmed(f"Approve printf '{MARKER}'", result_file))
            result_file.write_text(MARKER + "\n")
            self.assertFalse(tool_output_confirmed("Tool finished without visible output", result_file))
            self.assertTrue(tool_output_confirmed(MARKER, result_file))

    def test_approval_surface_observation_excludes_composer(self) -> None:
        before = f"Approve command writing {RESULT_NAME}"
        after = f"Allow command writing {RESULT_NAME}"
        evidence = dict(pre_resize_quiet=True, redraw_anchor_seen=True,
                        screen_size=(84, 24), child_size=(84, 24))
        self.assertTrue(approval_surface_present_after_resize(before, after, **evidence))
        self.assertFalse(approval_surface_present_after_resize(before, "π >", **evidence))
        self.assertFalse(approval_surface_present_after_resize(before, after, **{**evidence, "redraw_anchor_seen": False}))

    def test_stale_pre_resize_output_cannot_count_as_child_redraw(self) -> None:
        approval = f"Approve command writing {RESULT_NAME}"
        self.assertFalse(approval_surface_present_after_resize(
            approval, approval, pre_resize_quiet=False, redraw_anchor_seen=True,
            screen_size=(84, 24), child_size=(84, 24),
        ))
        self.assertFalse(approval_surface_present_after_resize(
            approval, approval, pre_resize_quiet=True, redraw_anchor_seen=True,
            screen_size=(84, 24), child_size=(100, 32),
        ))

    def test_no_resize_stop_continue_control_invalidates_redraw_claim(self) -> None:
        self.assertFalse(normal_resize_distinguished(True, True))
        self.assertFalse(normal_resize_distinguished(False, False))

    def test_cli_success_only_describes_approval_tool_roundtrip(self) -> None:
        observed = {key: True for key in (
            "composer_ready", "approval_visible", "artifact_absent_on_approval_ui",
            "artifact_absent_before_approval", "artifact_present_after_approval",
            "tool_output_visible", "provider_followup_observed", "final_visible",
        )}
        observed.update(normal_resize_confirmed=False, query_reply_omp_interpretation_confirmed=False)
        self.assertTrue(approval_tool_scenario_passed(observed))
        observed["artifact_absent_before_approval"] = False
        self.assertFalse(approval_tool_scenario_passed(observed))

    def test_pre_resize_drain_feeds_stale_output_before_resize(self) -> None:
        class Session:
            pending_write_bytes = 0

            def __init__(self) -> None:
                self.chunks = [b"stale", b""]

            def read_available(self) -> bytes:
                return self.chunks.pop(0) if self.chunks else b""

            def flush_writes(self) -> int:
                return 0

            def poll(self) -> None:
                return None

        class Stream:
            def __init__(self) -> None:
                self.fed: list[bytes] = []

            def feed(self, data: bytes) -> None:
                self.fed.append(data)

        session = Session()
        stream = Stream()
        quiet, count = drain_until_quiet(session, stream, timeout=0.1, quiet=0.01)
        self.assertTrue(quiet)
        self.assertEqual(count, 5)
        self.assertEqual(stream.fed, [b"stale"])
        self.assertEqual(session.read_available(), b"")

    def test_zero_input_queue_only_counts_with_noncanonical_tty(self) -> None:
        raw = ChildTtyState(0, False, (100, 32))
        canonical = ChildTtyState(0, True, (100, 32))
        self.assertTrue(query_reply_tty_drained(1, 5, 5, 0, raw))
        self.assertFalse(query_reply_tty_drained(1, 5, 5, 0, canonical))
        self.assertFalse(query_reply_tty_drained(1, 5, 5, 0, None))

    def test_approval_requires_artifact_absent_at_send_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result_file = Path(temporary) / RESULT_NAME
            self.assertTrue(approval_allowed(True, result_file))
            result_file.write_text(MARKER + "\n")
            self.assertFalse(approval_allowed(True, result_file))


if __name__ == "__main__":
    unittest.main()
