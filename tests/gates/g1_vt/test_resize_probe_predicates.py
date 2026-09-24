"""Requirement-derived guards against optimistic live resize probe results."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import live_normal_resize_probe as normal
import live_three_pane_resize_probe as three
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


class NormalResizeEvidenceTests(unittest.TestCase):
    def test_fresh_unrelated_status_text_is_not_native_approval_redraw(self) -> None:
        screen = TerminalScreen(84, 24)
        status = f"status: approve {normal.RESULT_NAME}\r\n".encode()

        def emit_status(_session: object, _stream: object, _seconds: float,
                        _predicate: object, observe: object) -> tuple[bool, int]:
            observe(status)
            return False, len(status)

        with (
            patch.object(normal, "pump", side_effect=emit_status),
            patch.object(normal, "child_tty_state", return_value=SimpleNamespace(size=(84, 24))),
        ):
            redraw, drained = normal.pump_surface(
                SimpleNamespace(pid=123), object(), screen, normal.RESULT_NAME, "approval"
            )

        self.assertEqual(drained, len(status))
        self.assertFalse(redraw)

    def test_failed_setup_cannot_serve_as_a_negative_resize_control(self) -> None:
        failed_control = {
            "startup_ready": False,
            "draft_resize_passed": False,
            "approval_resize_passed": False,
            "child_cleaned": True,
        }
        normal_case = {key: True for key in (
            "draft_resize_passed", "approval_resize_passed", "input_after_resize",
            "artifact_absent_after_resize", "tool_output_visible", "final_visible",
            "provider_followup", "child_cleaned",
        )}
        with (
            patch.object(normal.shutil, "which", return_value="/bin/true"),
            patch.object(normal.subprocess, "run", return_value=SimpleNamespace(stdout="omp/18.2.10")),
            patch.object(normal, "run_case", side_effect=[failed_control, failed_control, normal_case]),
        ):
            result = normal.run()

        self.assertFalse(result["normal_resize_confirmed"])

    def test_transcript_marker_does_not_prove_current_composer_draft(self) -> None:
        observations = []
        for columns, lines in ((84, 24), (100, 32)):
            screen = TerminalScreen(columns, lines)
            make_stream(screen).feed(
                (normal.DRAFT + f"\x1b[{lines};1Hπ >").encode()
            )
            with patch.object(normal, "child_tty_state", return_value=SimpleNamespace(size=(columns, lines))):
                observed = normal.state(screen, SimpleNamespace(pid=123), normal.DRAFT)
            self.assertEqual(observed["marker_rows"], [0])
            self.assertTrue(observed["composer_visible"])
            observations.append(observed)

        self.assertFalse(normal.resize_surface_passed(*observations, surface="composer"))

    def test_composer_text_mentioning_approval_does_not_prove_native_approval(self) -> None:
        screen = TerminalScreen(84, 24)
        make_stream(screen).feed(f"π > approve {normal.RESULT_NAME}".encode())
        with patch.object(normal, "child_tty_state", return_value=SimpleNamespace(size=(84, 24))):
            observed = normal.state(screen, SimpleNamespace(pid=123), normal.RESULT_NAME)

        self.assertFalse(observed["approval_visible"])


class ThreePaneEvidenceTests(unittest.TestCase):
    def _scripted_result(
        self, *, child_pids: tuple[int, int, int] = (99999997, 99999998, 99999999),
        initial_cross_route: bool = False,
        initial_outer_size: tuple[int, int] = (240, 45),
        initial_child_size: tuple[int, int] = (78, 41),
    ) -> dict[str, object]:
        class FakeSession:
            pid = 123

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.quitting = False

            def resize(self, *_args: object) -> None:
                pass

            def write(self, data: bytes) -> None:
                self.quitting |= data == three.QUIT

            def poll(self) -> int | None:
                return 0 if self.quitting else None

            def read_available(self) -> bytes:
                return b""

            def close(self) -> None:
                pass

        class FakeScreen:
            def __init__(self, columns: int, lines: int, **_kwargs: object) -> None:
                self.columns, self.lines = columns, lines

            def resize(self, *, lines: int, columns: int) -> None:
                self.columns, self.lines = columns, lines

        def stage(size: tuple[int, int], outer: tuple[int, int], host: bool,
                  cross_route: bool = False) -> dict[str, object]:
            return {
                "outer_size": list(outer),
                "pane_title_visible": [True] * 3,
                "marker_in_target": [True, True, host],
                "marker_in_other_panes": [cross_route, False, False],
                "child_sizes": [list(size) for _ in range(3)],
                "child_pids": list(child_pids),
            }

        snapshots = iter((
            stage(initial_child_size, initial_outer_size, False, initial_cross_route),
            stage((68, 35), (210, 39), False),
            stage((68, 35), (210, 39), True),
            stage((78, 41), (240, 45), True),
        ))
        with (
            patch.object(three.shutil, "which", return_value="/bin/true"),
            patch.object(three.subprocess, "run", return_value=SimpleNamespace(stdout="omp/18.2.10")),
            patch.object(three, "PtySession", FakeSession),
            patch.object(three, "TerminalScreen", FakeScreen),
            patch.object(three, "make_stream", return_value=object()),
            patch.object(three, "pump", return_value=(True, 0)),
            patch.object(three, "panes", return_value=["π >", "π >", "bash-"]),
            patch.object(three, "observe", side_effect=lambda *_args: next(snapshots)),
        ):
            return three.run()

    def test_clean_geometry_and_routing_fixture_is_accepted(self) -> None:
        result = self._scripted_result()

        self.assertTrue(result["children_cleaned"])
        self.assertTrue(result["three_pane_resize_confirmed"])

    def test_live_success_requires_every_reported_child_to_be_cleaned(self) -> None:
        result = self._scripted_result(child_pids=(os.getpid(), 99999998, 99999999))

        self.assertTrue(three.stable_child_pids([
            result[key] for key in ("initial", "small", "small_routed", "restored_routed")
        ]))
        self.assertTrue(result["outer_cleaned"])
        self.assertFalse(result["children_cleaned"])
        self.assertFalse(result["three_pane_resize_confirmed"])

    def test_initial_cross_routing_cannot_be_erased_by_later_clean_frames(self) -> None:
        result = self._scripted_result(initial_cross_route=True)

        self.assertTrue(result["initial"]["marker_in_other_panes"][0])
        self.assertTrue(result["children_cleaned"])
        self.assertFalse(result["three_pane_resize_confirmed"])

    def test_wrong_initial_outer_or_child_geometry_is_rejected(self) -> None:
        for change in (
            {"initial_outer_size": (239, 45)},
            {"initial_child_size": (77, 41)},
        ):
            with self.subTest(change=change):
                result = self._scripted_result(**change)
                self.assertTrue(result["children_cleaned"])
                self.assertFalse(result["three_pane_resize_confirmed"])


if __name__ == "__main__":
    unittest.main()
