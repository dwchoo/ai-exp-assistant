"""Independent negative controls for the external-tmux G1 observation."""

from __future__ import annotations

import errno
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import live_outer_tmux_probe as outer


class OuterTmuxPredicateTests(unittest.TestCase):
    """Keep host process and tmux boundaries synthetic; exercise the probe's verdict."""

    def _result(
        self, *, cross_routed: bool = False, paste: bool = True,
        sizes: tuple[tuple[int, int], ...] = ((78, 40), (68, 34), (78, 40)),
        pid_sets: tuple[tuple[int, int, int], ...] = ((99999991, 99999992, 99999993),) * 3,
        alternate_before: bool = True, alternate_after: bool = False,
        drain_status: str = "eof", sentinel_after: bool = True,
        stale_panes: bool = False,
    ) -> dict[str, object]:
        state: dict[str, object] = {"sentinel": "", "observations": 0, "writes": []}

        class FakeSession:
            pid = 99999990

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.quitting = False

            def resize(self, *_args: object) -> None:
                pass

            def write(self, data: bytes) -> None:
                state["writes"].append(data)
                if data == outer.QUIT:
                    self.quitting = True

            def poll(self) -> int | None:
                return 0 if self.quitting else None

            def close(self) -> None:
                pass

        class FakeScreen:
            def __init__(self, _columns: int, _lines: int, **_kwargs: object) -> None:
                self._terminal_control = {"using_alternate": alternate_before}
                self.size = (240, 45)

            def resize(self, *, lines: int, columns: int) -> None:
                self.size = (columns, lines)

        class FakeStream:
            def feed(self, data: bytes) -> None:
                if data.startswith(b"G1_LOCAL_"):
                    state["sentinel"] = data.decode().strip()

        screen_holder: list[FakeScreen] = []

        def screen_factory(columns: int, lines: int, **kwargs: object) -> FakeScreen:
            screen = FakeScreen(columns, lines, **kwargs)
            screen_holder.append(screen)
            return screen

        def pane_snapshot(_screen: object, markers: tuple[str, str, str]) -> dict[str, object]:
            index = int(state["observations"])
            state["observations"] = index + 1
            texts = pane_text(_screen)
            return {
                "titles": [True] * 3,
                "target": [marker in text for marker, text in zip(markers, texts)],
                "cross_routed": [cross_routed, False, False],
                "child_pids": list(pid_sets[index]),
                "child_sizes": [list(sizes[index]) for _ in range(3)],
            }

        def pane_text(_screen: object) -> list[str]:
            writes = state["writes"]
            manager = "π >"
            worker = "π >"
            host = "bash-"
            if stale_panes or b"MGR_TMUX" in writes:
                manager += " MGR_TMUX"
            if stale_panes or outer.FOCUS + b"WRK_TMUX" in writes:
                worker += " WRK_TMUX"
            if stale_panes or outer.FOCUS + b"printf 'HOST_%s\\n' TMUX\r" in writes:
                host += " HOST_TMUX"
            if stale_panes or outer.FOCUS + b"_M" in writes:
                manager += "_M"
            if stale_panes or b"_W" in writes:
                worker += "_W"
            if stale_panes or any(data.startswith(b"\x1b[200~") for data in writes):
                manager += " PASTE_TMUX_A"
                if paste:
                    manager += " PASTE_TMUX_B"
            return [manager, worker, host]

        def tmux_value(_tmux: str, _socket: Path, expression: str) -> str:
            if expression == "#{pid}":
                return "99999994"
            if expression == "#{pane_pid}":
                return "99999995"
            self.assertEqual(expression, "#{window_width}x#{window_height}")
            columns, lines = screen_holder[0].size
            return f"{columns}x{lines - 1}"

        def drain(_session: object, _stream: object) -> tuple[str, int]:
            screen_holder[0]._terminal_control["using_alternate"] = alternate_after
            return drain_status, 12

        def displayed(_screen: object) -> str:
            return str(state["sentinel"]) if sentinel_after else "blank primary screen"

        def command(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            if argv[-1] == "-V":
                return SimpleNamespace(stdout="tmux 3.4", returncode=0)
            if argv[-1] == "--version":
                return SimpleNamespace(stdout="omp/18.2.10", returncode=0)
            return SimpleNamespace(stdout="", returncode=0)

        with (
            patch.object(outer.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.object(outer.subprocess, "run", side_effect=command),
            patch.object(outer, "tmux_value", side_effect=tmux_value),
            patch.object(outer, "process_term", return_value="xterm-256color"),
            patch.object(outer, "PtySession", FakeSession),
            patch.object(outer, "TerminalScreen", side_effect=screen_factory),
            patch.object(outer, "make_stream", return_value=FakeStream()),
            patch.object(outer, "pump", return_value=(True, 10)),
            patch.object(outer, "panes", side_effect=pane_text),
            patch.object(outer, "pane_observation", side_effect=pane_snapshot),
            patch.object(outer, "drain_exited_client", side_effect=drain),
            patch.object(outer, "visible", side_effect=displayed),
        ):
            result = outer.run()
        if stale_panes:
            self.assertIn(b"MGR_TMUX", state["writes"])
            self.assertIn(b"\x1b[200~PASTE_TMUX_A\nPASTE_TMUX_B\x1b[201~", state["writes"])
        return result

    def test_complete_boundary_observation_is_accepted(self) -> None:
        result = self._result()
        self.assertEqual(result["result"], "observed")

    def test_cross_pane_marker_rejects_success(self) -> None:
        result = self._result(cross_routed=True)
        self.assertFalse(result["input_routed"])
        self.assertEqual(result["result"], "inconclusive_predicates")

    def test_incomplete_paste_rejects_success(self) -> None:
        result = self._result(paste=False)
        self.assertFalse(result["paste_visible"])
        self.assertEqual(result["result"], "inconclusive_predicates")

    def test_wrong_child_geometry_and_changed_pids_reject_success(self) -> None:
        for changes in (
            {"sizes": ((78, 40), (67, 34), (78, 40))},
            {"pid_sets": ((99999991, 99999992, 99999993),
                          (99999991, 99999992, 99999993),
                          (99999991, 99999992, 99999996))},
            {"pid_sets": ((99999991, 99999991, 99999993),) * 3},
        ):
            with self.subTest(changes=changes):
                result = self._result(**changes)
                self.assertFalse(result["resize_observed"])
                self.assertEqual(result["result"], "inconclusive_predicates")

    def test_local_restore_requires_real_alternate_lifecycle_and_eof(self) -> None:
        for changes in (
            {"alternate_before": False},
            {"alternate_after": True},
            {"drain_status": "timeout"},
            {"sentinel_after": False},
        ):
            with self.subTest(changes=changes):
                result = self._result(**changes)
                self.assertFalse(result["local_vt_restored"])
                self.assertEqual(result["result"], "inconclusive_cleanup_or_restore")

    def test_stale_pane_text_cannot_prove_fresh_routing_or_paste(self) -> None:
        # The synthetic screen never changes in response to the recorded writes.
        result = self._result(stale_panes=True)
        self.assertNotEqual(result["result"], "observed")


class PostExitDrainTests(unittest.TestCase):
    def test_queued_exit_bytes_are_fed_before_eof_is_reported(self) -> None:
        queued = b"\x1b[?1049lRESTORED"
        stream = SimpleNamespace(feed=unittest.mock.Mock())
        reads = [queued, OSError(errno.EIO, "PTY closed")]

        def read(_fd: int, _size: int) -> bytes:
            next_item = reads.pop(0)
            if isinstance(next_item, OSError):
                raise next_item
            return next_item

        with patch.object(outer.os, "read", side_effect=read):
            status, length = outer.drain_exited_client(SimpleNamespace(master_fd=17), stream)

        self.assertEqual((status, length), ("eof", len(queued)))
        stream.feed.assert_called_once_with(queued)


if __name__ == "__main__":
    unittest.main()
