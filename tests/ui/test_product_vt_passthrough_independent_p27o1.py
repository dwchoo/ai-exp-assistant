"""Independent product-model tests for p27-cd68-o1-fix-01: tmux-wrapped OMP notifications in all three panes.

Drives the real ProductModel (all pane streams) with ui_v1 display frames: synchronous ``on_display``, the decoupled
``enqueue_display``/``feed_pending`` slices (frames cut anywhere, incl. at ESC), attach replay, and the session
replacement path. Nothing is forwarded to the outer terminal (``take_output`` stays empty); OSC 52 copy is unaffected.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product.model import ProductModel, _align  # noqa: E402

PANES = (PaneId.MANAGER_OMP, PaneId.WORKER_OMP, PaneId.HOST_SHELL)
PAYLOAD = json.dumps({"event": "stop", "query": "사용자 입력 원문", "response": "manager 응답 원문"}, ensure_ascii=False)
OSC777 = f"\x1b]777;notify;warp://cli-agent;{PAYLOAD}\x07".encode()
WRAPPED = b"\x1bPtmux;" + OSC777.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
LEAKS = ("tmux", "777", "notify", "warp://", "사용자 입력", "응답 원문", "cli-agent", "\x1b")


def model() -> ProductModel:
    m = ProductModel(FakeSender(), 30, 200, clock=lambda: 1000.0)
    m.apply_snapshot(snapshot())
    return m


def frame(pane: PaneId, data: bytes, *, session="s1", generation=1, replay=False) -> ui_v1.Frame:
    header = {"pane": pane.value, "session_id": session, "generation": generation}
    if replay:
        header["replay"] = True
    return ui_v1.Frame(header, data)


def lines(m: ProductModel, pane: PaneId) -> list[str]:
    return [line.rstrip() for line in m.panes[pane].screen.display if line.strip()]


def drain(m: ProductModel) -> None:
    for _ in range(10000):
        if not m.feed_pending(max_bytes=1 << 20, max_seconds=5.0):
            return
    raise AssertionError("backlog never drained")


class ProductPaneTests(unittest.TestCase):
    def assertClean(self, shown: list[str]) -> None:
        joined = "".join(shown)
        for leaked in LEAKS:
            self.assertNotIn(leaked, joined)

    def test_all_three_panes_hide_the_wrapped_notification_with_and_without_trailing_bel(self):
        for pane in PANES:
            for tail in (b"", b"\x07"):
                with self.subTest(pane=pane.value, bel=bool(tail)):
                    m = model()
                    m.on_display(frame(pane, b"before " + WRAPPED + tail + "after 한글\r\n".encode()))
                    self.assertEqual(lines(m, pane), ["before after 한글"])
                    self.assertEqual(m.take_output(), b"", "notifications are never forwarded to the outer terminal")

    def test_enqueued_frames_cut_at_every_offset_incl_at_esc(self):
        data = b"before " + WRAPPED + "\x07after 한글\r\n".encode()
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                m = model()
                m.enqueue_display(frame(PaneId.MANAGER_OMP, data[:cut]))
                m.enqueue_display(frame(PaneId.MANAGER_OMP, data[cut:]))
                drain(m)
                self.assertEqual(lines(m, PaneId.MANAGER_OMP), ["before after 한글"])

    def test_random_frame_splits_over_three_panes_interleaved(self):
        data = b"\x1b[32mgo\x1b[0m " + WRAPPED + "\x07 한글\r\n".encode() + WRAPPED + b"done\r\n"
        for seed in range(60):
            rng = random.Random(seed)
            m = model()
            offsets = {pane: 0 for pane in PANES}
            while any(offsets[p] < len(data) for p in PANES):
                pane = rng.choice([p for p in PANES if offsets[p] < len(data)])
                step = rng.choice((1, 2, 7, 30))
                chunk = data[offsets[pane]:offsets[pane] + step]
                offsets[pane] += step
                m.enqueue_display(frame(pane, chunk))
                if rng.random() < 0.3:
                    m.feed_pending(max_bytes=rng.choice((1, 5, 50)), max_seconds=5.0)
            drain(m)
            with self.subTest(seed=seed):
                for pane in PANES:
                    self.assertEqual(lines(m, pane), ["go  한글", "done"], pane.value)

    def test_attach_replay_frames_with_notification_are_clean(self):
        m = model()
        m.on_display(frame(PaneId.WORKER_OMP, b"history " + WRAPPED + b"\x07line\r\n", replay=True))
        m.on_display(frame(PaneId.WORKER_OMP, b"live " + WRAPPED + b"\r\n"))
        self.assertEqual(lines(m, PaneId.WORKER_OMP), ["history line", "live"])

    def test_osc52_copy_output_is_independent_of_pane_strings(self):
        m = model()
        m.on_display(frame(PaneId.HOST_SHELL, WRAPPED + b"copy me\r\n"))
        self.assertEqual(m.take_output(), b"")
        sent = b"\x1b]52;c;" + base64.b64encode("복사".encode()) + b"\x07"
        m.on_display(frame(PaneId.HOST_SHELL, sent + b"x"))  # an OSC 52 *from the pane* is swallowed, not echoed
        self.assertEqual(m.take_output(), b"")
        self.assertEqual(lines(m, PaneId.HOST_SHELL)[-1], "x")

    def test_unterminated_string_does_not_blank_a_replaced_session(self):
        """A pane process that dies inside a string must not hide the next session of that pane (C-D62 restart)."""
        for pane in PANES:
            for how, new_id in (("session", dict(session="s2", generation=1)), ("generation", dict(session="s1", generation=2)),
                                ("both", dict(session="s2", generation=2))):
                with self.subTest(pane=pane.value, changed=how):
                    m = model()
                    m.on_display(frame(pane, b"old\r\n\x1bPtmux;\x1b\x1b]777;notify;partial", session="s1", generation=1))
                    m.on_display(frame(pane, b"new session text\r\n", **new_id))
                    self.assertEqual(lines(m, pane), ["new session text"])

    def test_restart_mid_string_also_through_the_enqueued_path_and_split_frames(self):
        for pane in PANES:
            for kind in (b"\x1bPtmux;partial", b"\x1b_apc", b"\x1bX", b"\x1b^pm\x1b", b"\x1bPtmux;\x1b\x1b]777;n;"):
                with self.subTest(pane=pane.value, kind=kind):
                    m = model()
                    m.enqueue_display(frame(pane, b"old\r\n" + kind, session="s1", generation=1))
                    m.enqueue_display(frame(pane, b"fresh ", session="s2", generation=2))
                    m.enqueue_display(frame(pane, "한글\r\n".encode(), session="s2", generation=2))
                    drain(m)
                    self.assertEqual(lines(m, pane), ["fresh 한글"])

    def test_pending_esc_of_a_dead_session_does_not_corrupt_the_next_session(self):
        for pane in PANES:
            with self.subTest(pane=pane.value):
                m = model()
                m.on_display(frame(pane, b"old\x1b", session="s1", generation=1))
                m.on_display(frame(pane, b"[31mnew", session="s2", generation=2))
                self.assertEqual(lines(m, pane), ["[31mnew"])

    def test_pending_esc_inside_a_dead_string_does_not_corrupt_the_next_session(self):
        m = model()
        m.on_display(frame(PaneId.WORKER_OMP, b"old\x1bPtmux;x\x1b", session="s1", generation=1))
        m.on_display(frame(PaneId.WORKER_OMP, b"\\still hidden? no\r\n", session="s2", generation=2))
        self.assertEqual(lines(m, PaneId.WORKER_OMP), ["\\still hidden? no"])

    def test_lost_st_does_not_blank_the_pane_in_any_pane(self):
        for pane in PANES:
            with self.subTest(pane=pane.value, case="csi"):
                m = model()
                m.on_display(frame(pane, b"A\x1bPtmux;\x1b\x1b]777;n;lost"))
                m.on_display(frame(pane, b"\x1b[31mvisible\x1b[0m\r\nnext\r\n"))
                self.assertEqual(lines(m, pane), ["Avisible", "next"])
            with self.subTest(pane=pane.value, case="ris"):
                m = model()
                m.on_display(frame(pane, b"junk\r\nmore\x1b_apc lost"))
                m.on_display(frame(pane, b"\x1bcclean\r\n"))
                self.assertEqual(lines(m, pane), ["clean"])
            with self.subTest(pane=pane.value, case="split-enqueued"):
                m = model()
                for part in (b"A\x1bPlost", b"\x1b", b"[1mbold", b"\x1b", b"c", b"after\r\n"):
                    m.enqueue_display(frame(pane, part))
                drain(m)
                self.assertEqual(lines(m, pane), ["after"])

    def test_ris_after_an_aborted_string_works_in_the_model(self):
        for abort in (b"\x18", b"\x1a", b"\x1b\x18", b"\x1b\\"):
            with self.subTest(abort=abort):
                m = model()
                m.on_display(frame(PaneId.HOST_SHELL, b"junk\r\n\x1bPx" + abort + b"\x1bcfine\r\n"))
                self.assertEqual(lines(m, PaneId.HOST_SHELL), ["fine"])

    def test_replay_tail_cut_inside_a_wrapped_notification_does_not_leak_payload(self):
        """was a pinned known edge: the retained tail aligned by ``_align`` must hide a notification it cut into."""
        for cut in range(1, len(WRAPPED)):
            m = model()
            m.on_display(frame(PaneId.MANAGER_OMP, _align(WRAPPED[cut:]), replay=True))
            self.assertEqual(lines(m, PaneId.MANAGER_OMP), [], f"cut={cut}")
            m = model()
            m.on_display(frame(PaneId.MANAGER_OMP, _align(b"x" + WRAPPED[cut:] + b"AFTER"), replay=True))
            self.assertEqual(lines(m, PaneId.MANAGER_OMP)[-1:] and lines(m, PaneId.MANAGER_OMP)[-1].endswith("AFTER"), True,
                             f"cut={cut}")
            for line in lines(m, PaneId.MANAGER_OMP):
                for leaked in ("777", "notify", "warp://", "사용자 입력", "응답 원문", "cli-agent", "tmux"):
                    self.assertNotIn(leaked, line, f"cut={cut}")

    def test_catch_up_cut_inside_a_big_wrapped_notification_shows_no_payload(self):
        m = model()
        big = b"\x1bPtmux;\x1b\x1b]777;notify;" + b"a" * 90000 + b"\x07\x1b\\"
        for i in range(4):
            m.enqueue_display(frame(PaneId.WORKER_OMP, b"filler line %d\r\n" % i * 20000))
        m.enqueue_display(frame(PaneId.WORKER_OMP, big + b"TAIL"))
        while m.feed_pending():
            pass
        self.assertGreater(m.panes[PaneId.WORKER_OMP].skipped_bytes, 0, "the catch-up path was taken")
        shown = lines(m, PaneId.WORKER_OMP)
        self.assertEqual(shown, ["TAIL"])


if __name__ == "__main__":
    unittest.main()
