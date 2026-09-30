"""Independent CW-06 resizable-splits tests (p27-cd59-test-01, unit V-CW-06-p2.7-resize).

Expectations come from C-D59 (user request: "manager, worker, 터미널 간에 좌우 창 길이를 조절할 수 있으면 좋겠어") and the
Root-specified contract, written before layout.py / model.py were read:

- two adjustable splits: manager|worker (vertical divider, top row) and top row|host terminal (horizontal divider);
  each keeps every pane at >= 10 inner columns / >= 3 inner rows (when the window is big enough), follows window resizes
  proportionally; the panes always tile the body exactly.
- mouse (while capture is on): press on a divider line, motion, release moves it by the mouse delta; divider clicks never
  reach a pane and never change focus. MOUSE-MODE CONTRACT (C-D59 correction p27-cd59-test-02, supersedes the earlier
  "?1002 only while a divider is held" wording, which killed all mouse reporting in tmux/xterm/herdr because
  1000/1002/1003 are ONE tracking mode there): ``?1000h ?1002h ?1006h`` are enabled once at start, nothing is switched
  during a drag, and all of them are off after every exit path (detach, SIGTERM, SIGHUP, backend closing, connection
  loss, prefix m ...). Asserted on a single-tracking-mode outer-terminal model (``OuterMouse``), not on byte presence.
- keys: prefix Left/Right moves the vertical divider, prefix Up/Down the horizontal one; after a prefix+arrow bare arrows
  within about one second keep resizing (tmux-like), any other key ends the repeat and is handled normally (delivered);
  prefix z zooms the focused pane over the whole body and back, prefix = resets the splits.
- every effective layout change sends per-pane resize frames equal to the drawn inner sizes, debounced during drags, and
  never sends keys to panes.
- ``<data dir>/ui-layout.json`` (mode 0600, atomic) keeps ratios and zoom across detach/reattach; corrupt content is ignored.

Runtime: the pure model, the real product UI loop on an owned PTY (scripted ui_v1 fixture) and the real ``workbench
attach`` entrypoint against a real backend running a stub OMP (zero model turns, no provider, no credentials).

Red-run knob (test-only): ``P27L_SRC`` points at an alternative ``src`` tree for the subprocesses (the PYTHONPATH of the
test process itself must point at the same tree).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import independent_support_cw06 as support  # noqa: E402
from independent_support_cw06 import RecordingSender, ScriptedServer, UiPty, snap  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import DisplayChunk, PaneId  # noqa: E402
from workbench.ui.product.input import PREFIX  # noqa: E402
from workbench.ui.product.model import MIN_COLS, MIN_ROWS, ProductModel  # noqa: E402
from workbench.ui.product.view import pane_rects  # noqa: E402

if os.environ.get("P27L_SRC"):
    support.SRC = Path(os.environ["P27L_SRC"])
SRC = str(support.SRC)

P = bytes([PREFIX])
RIGHT, LEFT, UP, DOWN = b"\x1b[C", b"\x1b[D", b"\x1b[A", b"\x1b[B"
M, W, H = PaneId.MANAGER_OMP, PaneId.WORKER_OMP, PaneId.HOST_SHELL
DRAG_ON, DRAG_OFF = b"\x1b[?1002h", b"\x1b[?1002l"  # 1002 is part of the once-at-start / all-off-on-exit set now
HEADER, FOOTER = 2, 1  # C-D58 frame: two header rows, one footer row


class OuterMouse:
    """The outer terminal as tmux 3.4 / xterm / herdr see mouse modes: ONE tracking mode.

    ``?1000h`` / ``?1002h`` / ``?1003h`` select press / button-motion / any-motion tracking (the last set wins), and a
    reset of ANY of the three turns tracking OFF (tmux clears ALL_MOUSE_MODES); ``?1006`` (SGR encoding) is independent.
    """

    TRACK = {1000: "press", 1002: "button", 1003: "any"}

    def __init__(self):
        self.mode: str | None = None
        self.sgr = False
        self.changes: list[tuple[int, int, bool]] = []  # (byte offset, mode number, on)

    @classmethod
    def of(cls, output: bytes) -> "OuterMouse":
        model = cls()
        for match in re.finditer(rb"\x1b\[\?([0-9;]+)([hl])", output):
            on = match.group(2) == b"h"
            for number in match.group(1).split(b";"):
                model.set(int(number), on, match.start())
        return model

    def set(self, number: int, on: bool, offset: int = -1) -> None:
        if number in self.TRACK:
            self.mode = self.TRACK[number] if on else None
        elif number == 1006:
            self.sgr = on
        else:
            return
        self.changes.append((offset, number, on))

    @property
    def clicks(self) -> bool:
        return self.mode is not None and self.sgr

    wheel = clicks

    @property
    def motion(self) -> bool:
        return self.clicks and self.mode in ("button", "any")

    def changes_after(self, offset: int) -> list[tuple[int, int, bool]]:
        return [c for c in self.changes if c[0] >= offset]


def assert_mouse_reporting_on(test: unittest.TestCase, output: bytes, msg: str = "") -> None:
    model = OuterMouse.of(bytes(output))
    test.assertTrue(model.clicks and model.wheel, f"outer terminal no longer reports clicks/wheel (mode={model.mode} "
                                                  f"sgr={model.sgr}) {msg}")


def assert_mouse_off(test: unittest.TestCase, output: bytes, msg: str = "") -> None:
    model = OuterMouse.of(bytes(output))
    test.assertEqual((model.mode, model.sgr), (None, False), f"mouse modes left on {msg}")


# --------------------------------------------------------------------------------------------- pure-model helpers
def make_model(sender, rows, cols, clock):
    try:
        return ProductModel(sender, rows, cols, clock=clock, monotonic=clock)
    except TypeError:  # the pre-change model has no injectable monotonic clock
        return ProductModel(sender, rows, cols, clock=clock)


def rects(model) -> dict[PaneId, tuple[int, int, int, int]]:
    """(top, left, height, width) incl. border of the visible panes, exactly as the curses view draws them."""
    layout, zoom = getattr(model, "layout", None), getattr(model, "zoom", None)
    if layout is not None:
        try:
            return pane_rects(model.rows, model.cols, layout, zoom)
        except TypeError:
            pass
    return pane_rects(model.rows, model.cols)


def inner(rect) -> tuple[int, int]:
    return rect[2] - 2, rect[3] - 2


def sgr(button: int, x: int, y: int, release: bool = False) -> bytes:
    return f"\x1b[<{button};{x};{y}{'m' if release else 'M'}".encode()


PRESS, MOTION = 0, 32


class Sim:
    """A pure ProductModel wired to a recording sender and a manual clock (the app loop's calls are mimicked)."""

    def __init__(self, rows: int = 40, cols: int = 140, focus: str = "manager_omp"):
        self.t = 100.0
        self.sender = RecordingSender()
        self.model = make_model(self.sender, rows, cols, lambda: self.t)
        self.model.attach_done(snap(focus=focus))
        self.model.after_attach()
        self.seq = 0

    # -- driving
    def keys(self, data: bytes, dt: float = 0.0) -> None:
        self.t += dt
        self.model.handle_input(data, now=self.t)
        self.flush()

    def flush(self) -> None:
        flush = getattr(self.model, "flush_resizes", None)
        if flush is not None:
            flush()

    def wait(self, dt: float) -> None:
        self.t += dt
        self.model.flush_input(now=self.t)
        self.flush()

    def mouse(self, button: int, x: int, y: int, release: bool = False, dt: float = 0.0) -> None:
        self.keys(sgr(button, x, y, release), dt)

    def drag(self, x0: int, y0: int, x1: int, y1: int, steps: int = 6, dt: float = 0.2) -> None:
        self.mouse(PRESS, x0, y0)
        for i in range(1, steps + 1):
            self.mouse(MOTION, x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps, dt=dt)
        self.mouse(PRESS, x1, y1, release=True, dt=dt)
        self.wait(0.5)

    def feed(self, pane: PaneId, data: bytes) -> None:
        self.seq += 1
        raw = ui_v1.encode_display(DisplayChunk(session_id=support.SID, session_generation=1, pane_id=pane,
                                                sequence=self.seq, data=data), replay=False)
        self.model.on_display(ui_v1.FrameDecoder().feed(raw)[0])
        while self.model.has_backlog():
            self.model.feed_pending()

    # -- observation
    def mark(self) -> int:
        return len(self.sender.sent)

    def after(self, mark: int, kind: str) -> list:
        return [f for f in self.sender.sent[mark:] if f[0] == kind]

    def last_sent(self) -> dict[str, tuple[int, int]]:
        out = {}
        for kind, fields, _, _ in self.sender.sent:
            if kind == "resize":
                out[fields["pane"]] = (fields["rows"], fields["cols"])
        return out

    def dividers(self):
        """1-based grab cells: (vx_manager_border, vx_worker_border, vy, hy_host_top_border, hy_top_row_bottom, hx)."""
        r = rects(self.model)
        m, w, h = r[M], r[W], r[H]
        vy = m[0] + 1 + m[2] // 2
        return m[1] + m[3], w[1] + 1, vy, h[0] + 1, h[0], h[1] + h[3] // 2

    def widths(self) -> tuple[int, int]:
        r = rects(self.model)
        return r[M][3], r[W][3]

    def heights(self) -> tuple[int, int]:
        r = rects(self.model)
        return r[M][2], r[H][2]


def tiled(test: unittest.TestCase, model, msg: str = "") -> None:
    r = rects(model)
    rows, cols = model.rows, model.cols
    body = rows - HEADER - FOOTER
    m, w, h = r[M], r[W], r[H]
    test.assertEqual((m[0], w[0]), (HEADER, HEADER), msg)
    test.assertEqual(m[1], 0, msg)
    test.assertEqual(m[1] + m[3], w[1], f"gap/overlap between manager and worker {r} {msg}")
    test.assertEqual(w[1] + w[3], cols, f"worker does not reach the right edge {r} {msg}")
    test.assertEqual(m[2], w[2], msg)
    test.assertEqual((h[0], h[1], h[3]), (HEADER + m[2], 0, cols), f"host not directly below, full width {r} {msg}")
    test.assertEqual(h[0] + h[2], HEADER + body, f"host does not reach the footer {r} {msg}")


def sent_matches_drawn(test: unittest.TestCase, sim: Sim, msg: str = "") -> None:
    last = sim.last_sent()
    for pane, rect in rects(sim.model).items():
        test.assertEqual(last.get(pane.value), inner(rect), f"resize frame for {pane.value} != drawn area {msg}")


def min_inner_ok(test: unittest.TestCase, model, msg: str = "") -> None:
    for pane, rect in rects(model).items():
        rows, cols = inner(rect)
        test.assertGreaterEqual(cols, 10, f"{pane.value} inner cols {rows}x{cols} {msg}")
        test.assertGreaterEqual(rows, 3, f"{pane.value} inner rows {rows}x{cols} {msg}")


# ---------------------------------------------------------------------------------------------------- geometry
class SplitGeometryTests(unittest.TestCase):
    SIZES = [(24, 80), (25, 81), (30, 100), (31, 101), (40, 140), (41, 141), (50, 200), (60, 300)]

    def test_default_split_is_balanced_and_tiled(self):
        for rows, cols in self.SIZES + [(MIN_ROWS, MIN_COLS)]:
            with self.subTest(size=(rows, cols)):
                sim = Sim(rows, cols)
                tiled(self, sim.model)
                mw, ww = sim.widths()
                self.assertLessEqual(abs(mw - ww), 1, "default manager|worker split is not about half")
                top, bottom = sim.heights()
                self.assertLessEqual(abs(top - bottom), 2, "default top|host split is not about half")

    def test_extreme_drags_clamp_to_minimum_inner_size_at_many_sizes(self):
        for rows, cols in self.SIZES:
            for axis in ("v", "h"):
                for direction in (-1, +1):
                    with self.subTest(size=(rows, cols), axis=axis, direction=direction):
                        sim = Sim(rows, cols)
                        vx, _, vy, hy, _, hx = sim.dividers()
                        if axis == "v":
                            sim.drag(vx, vy, 1 if direction < 0 else cols + 50, vy, steps=4)
                        else:
                            sim.drag(hx, hy, hx, 1 if direction < 0 else rows + 50, steps=4)
                        tiled(self, sim.model)
                        min_inner_ok(self, sim.model, f"{rows}x{cols} {axis}{direction}")
                        sent_matches_drawn(self, sim)

    def test_extreme_keyboard_moves_clamp_at_many_sizes(self):
        for rows, cols in self.SIZES:
            for key in (LEFT, RIGHT, UP, DOWN):
                with self.subTest(size=(rows, cols), key=key):
                    sim = Sim(rows, cols)
                    sim.keys(P + key, 0.0)
                    for i in range(1, 90):  # bare arrows keep resizing inside the repeat window
                        sim.keys(key, 0.05)
                    tiled(self, sim.model)
                    min_inner_ok(self, sim.model)
                    sent_matches_drawn(self, sim)

    def test_smallest_supported_window_stays_valid(self):
        sim = Sim(MIN_ROWS, MIN_COLS)
        vx, _, vy, hy, _, hx = sim.dividers()
        sim.drag(vx, vy, 1, vy)
        sim.drag(hx, hy, hx, 1)
        sim.keys(P + LEFT)
        sim.keys(P + UP)
        tiled(self, sim.model)
        for rect in rects(sim.model).values():
            self.assertGreaterEqual(inner(rect)[0], 1)
            self.assertGreaterEqual(inner(rect)[1], 1)
        sent_matches_drawn(self, sim)

    def test_split_follows_window_resize_proportionally(self):
        sim = Sim(40, 140)
        vx, _, vy, hy, _, hx = sim.dividers()
        target = int(140 * 0.3)
        sim.drag(vx, vy, target + 1, vy)  # manager box ~30 % wide
        vx, _, vy, hy, _, hx = sim.dividers()
        sim.drag(hx, hy, hx, hy + 8)  # top row grows by 8 rows
        col_share = sim.widths()[0] / 140
        row_share = sim.heights()[0] / (40 - HEADER - FOOTER)
        self.assertAlmostEqual(col_share, 0.3, delta=0.03)
        for rows, cols in ((50, 200), (30, 100), (45, 260), (60, 90)):
            with self.subTest(size=(rows, cols)):
                sim.model.resize(rows, cols)
                sim.flush()
                tiled(self, sim.model)
                self.assertAlmostEqual(sim.widths()[0] / cols, col_share, delta=0.04, msg="manager share drifted")
                self.assertAlmostEqual(sim.heights()[0] / (rows - HEADER - FOOTER), row_share, delta=0.08)
                min_inner_ok(self, sim.model)
                sent_matches_drawn(self, sim)

    def test_default_layout_untouched_by_window_resizes(self):
        sim = Sim(40, 140)
        fresh = {}
        for rows, cols in ((30, 100), (55, 211), (24, 80)):
            sim.model.resize(rows, cols)
            fresh = Sim(rows, cols)
            self.assertEqual(rects(sim.model), rects(fresh.model), "an untouched layout must equal the default split")


# ------------------------------------------------------------------------------------------------- mouse drag
class DividerDragModelTests(unittest.TestCase):
    def test_vertical_drag_moves_divider_by_mouse_delta_from_either_border(self):
        for which in (0, 1):
            for delta in (-25, -7, 9, 30):
                with self.subTest(border=which, delta=delta):
                    sim = Sim(40, 140)
                    before = sim.widths()
                    vx = sim.dividers()[which]
                    vy = sim.dividers()[2]
                    sim.drag(vx, vy, vx + delta, vy)
                    self.assertEqual(sim.widths()[0], before[0] + delta)
                    self.assertEqual(sum(sim.widths()), 140)
                    tiled(self, sim.model)
                    sent_matches_drawn(self, sim)

    def test_horizontal_drag_moves_divider_by_mouse_delta_from_either_border(self):
        for which in (3, 4):
            for delta in (-9, -3, 4, 10):
                with self.subTest(border=which, delta=delta):
                    sim = Sim(40, 140)
                    before = sim.heights()
                    d = sim.dividers()
                    sim.drag(d[5], d[which], d[5], d[which] + delta)
                    self.assertEqual(sim.heights()[0], before[0] + delta)
                    tiled(self, sim.model)
                    sent_matches_drawn(self, sim)

    def test_divider_follows_the_mouse_during_the_drag(self):
        sim = Sim(40, 140)
        vx, _, vy, *_ = sim.dividers()
        start = sim.widths()[0]
        sim.mouse(PRESS, vx, vy)
        for step in (3, 8, 15, 6, -4):
            sim.mouse(MOTION, vx + step, vy, dt=0.01)
            self.assertEqual(sim.widths()[0], start + step, f"divider is not under the pointer at delta {step}")
        sim.mouse(PRESS, vx - 4, vy, release=True)

    def test_drag_only_touches_its_own_axis(self):
        sim = Sim(40, 140)
        vx, _, vy, *_ = sim.dividers()
        heights = sim.heights()
        sim.drag(vx, vy, vx + 20, vy + 5)  # vertical divider dragged diagonally: only the width moves
        self.assertEqual(sim.heights(), heights)
        sim = Sim(40, 140)
        d = sim.dividers()
        widths = sim.widths()
        sim.drag(d[5], d[3], d[5] + 30, d[3] + 3)
        self.assertEqual(sim.widths(), widths)

    def test_divider_clicks_never_reach_panes_or_change_focus(self):
        for pane_app_mouse in (False, True):
            with self.subTest(app_tracks_mouse=pane_app_mouse):
                sim = Sim(40, 140, focus="manager_omp")
                if pane_app_mouse:  # panes whose application tracks the mouse get clicks forwarded elsewhere
                    for pane in (M, W, H):
                        sim.feed(pane, b"\x1b[?1000h\x1b[?1006h")
                vx0, vx1, vy, hy0, hy1, hx = sim.dividers()
                mark = sim.mark()
                layout_before = rects(sim.model)
                for x, y in ((vx0, vy), (vx1, vy), (vx0, vy + 1), (hx, hy0), (hx, hy1), (hx + 5, hy0)):
                    sim.mouse(PRESS, x, y)
                    sim.mouse(PRESS, x, y, release=True)
                self.assertEqual(sim.after(mark, "input"), [], "a divider click was delivered to a pane")
                self.assertEqual(sim.after(mark, "paste"), [])
                self.assertEqual(sim.after(mark, "focus"), [], "a divider click changed focus")
                self.assertEqual(sim.model.focus, M)
                self.assertEqual(rects(sim.model), layout_before, "a click without motion moved a divider")
                self.assertEqual(sim.after(mark, "resize"), [], "a click without motion sent resize frames")

    def test_interior_click_still_focuses_so_the_click_test_is_meaningful(self):
        sim = Sim(40, 140, focus="manager_omp")
        w = rects(sim.model)[W]
        mark = sim.mark()
        sim.mouse(PRESS, w[1] + 5, w[0] + 4)
        sim.mouse(PRESS, w[1] + 5, w[0] + 4, release=True)
        self.assertEqual(sim.model.focus, W)
        self.assertTrue(sim.after(mark, "focus"))

    def test_drag_frames_reach_the_backend_never_keys(self):
        sim = Sim(40, 140)
        vx, _, vy, *_ = sim.dividers()
        mark = sim.mark()
        sim.drag(vx, vy, vx - 30, vy)
        self.assertEqual(sim.after(mark, "input"), [])
        self.assertEqual(sim.after(mark, "paste"), [])
        self.assertEqual(sim.after(mark, "focus"), [])
        panes = {f[1]["pane"] for f in sim.after(mark, "resize")}
        self.assertLessEqual({"manager_omp", "worker_omp"}, panes)
        sent_matches_drawn(self, sim)

    def test_drag_resize_frames_are_debounced_not_a_flood(self):
        sim = Sim(60, 260)
        vx, _, vy, *_ = sim.dividers()
        mark = sim.mark()
        sim.mouse(PRESS, vx, vy)
        for i in range(1, 121):  # 120 motion reports, 20 ms apart (2.4 s), every one moves the divider
            sim.mouse(MOTION, vx - i, vy, dt=0.02)
        sim.mouse(PRESS, vx - 120, vy, release=True)
        sim.wait(0.5)
        frames = [f for f in sim.after(mark, "resize") if f[1]["pane"] == "manager_omp"]
        self.assertGreaterEqual(len(frames), 2, "no intermediate/final resize frames at all")
        self.assertLessEqual(len(frames), 30, f"{len(frames)} manager resize frames for 120 motions: flooding")
        sent_matches_drawn(self, sim)
        # burst inside one instant: at most a couple of frames, the final one still exact
        sim = Sim(60, 260)
        vx, _, vy, *_ = sim.dividers()
        mark = sim.mark()
        sim.mouse(PRESS, vx, vy)
        for i in range(1, 101):
            sim.mouse(MOTION, vx + i % 40, vy, dt=0.0)
        sim.mouse(PRESS, vx + 40, vy, release=True)
        sim.wait(0.5)
        burst = [f for f in sim.after(mark, "resize") if f[1]["pane"] == "manager_omp"]
        self.assertLessEqual(len(burst), 3, f"{len(burst)} frames for a same-instant burst")
        sent_matches_drawn(self, sim)

    def test_non_left_buttons_and_wheel_do_not_drag(self):
        for button in (1, 2, 64, 65):
            with self.subTest(button=button):
                sim = Sim(40, 140)
                vx, _, vy, *_ = sim.dividers()
                before, mark = rects(sim.model), sim.mark()
                sim.mouse(button, vx, vy)
                sim.mouse(MOTION + button, vx + 20, vy)
                sim.mouse(button, vx + 20, vy, release=True)
                self.assertEqual(rects(sim.model), before)
                self.assertEqual(sim.after(mark, "resize"), [])

    def test_press_off_the_divider_never_drags(self):
        sim = Sim(40, 140)
        m = rects(sim.model)[M]
        before = rects(sim.model)
        sim.mouse(PRESS, m[1] + 6, m[0] + 3)  # inside the manager pane
        sim.mouse(MOTION, m[1] + 30, m[0] + 3)
        sim.mouse(PRESS, m[1] + 30, m[0] + 3, release=True)
        self.assertEqual(rects(sim.model), before)

    def test_no_drag_without_mouse_capture(self):
        sim = Sim(40, 140)
        sim.keys(P + b"m")  # capture off
        vx, _, vy, *_ = sim.dividers()
        before, mark = rects(sim.model), sim.mark()
        sim.drag(vx, vy, vx + 20, vy)
        self.assertEqual(rects(sim.model), before)
        self.assertEqual(sim.after(mark, "resize"), [])
        self.assertEqual(sim.after(mark, "input"), [])

    def test_lost_release_report_ends_the_drag(self):
        sim = Sim(40, 140)
        vx, _, vy, *_ = sim.dividers()
        sim.mouse(PRESS, vx, vy)
        sim.mouse(MOTION, vx + 10, vy)
        width = sim.widths()[0]
        sim.mouse(MOTION + 3, vx + 25, vy)  # motion with no button held: the release was lost
        sim.mouse(MOTION, vx + 30, vy)
        self.assertEqual(sim.widths()[0], width, "divider kept following the pointer after the button was gone")

    def test_keys_after_a_drag_go_to_the_pane_normally(self):
        sim = Sim(40, 140)
        vx, _, vy, *_ = sim.dividers()
        sim.drag(vx, vy, vx + 10, vy)
        mark = sim.mark()
        sim.keys(b"hello")
        self.assertEqual(b"".join(f[2] for f in sim.after(mark, "input")), b"hello")

    def test_prefix_command_during_a_drag_ends_it_and_is_handled(self):
        sim = Sim(40, 140)
        vx, _, vy, *_ = sim.dividers()
        sim.mouse(PRESS, vx, vy)
        sim.mouse(MOTION, vx + 6, vy)
        sim.keys(P + b"2")
        self.assertEqual(sim.model.focus, W)
        width = sim.widths()[0]
        sim.mouse(MOTION, vx + 20, vy)
        self.assertEqual(sim.widths()[0], width, "drag continued after a prefix command")

    def test_scroll_position_is_kept_across_layout_changes(self):
        sim = Sim(40, 140, focus="host_shell")
        sim.keys(P + b"3")
        sim.feed(H, b"".join(f"line {i:05d}\r\n".encode() for i in range(1, 401)))
        sim.keys(b"\x1b[5;2~")  # Shift+PgUp: direct scroll back
        sim.keys(b"\x1b[5;2~")

        def numbers():
            return [int(m.group(1)) for line in sim.model.pane_lines(H, inner(rects(sim.model)[H])[0])
                    if (m := re.fullmatch(r"line (\d{5})", "".join(getattr(line.get(x), "data", " ") or " "
                                                                   for x in range(140)).strip()))]

        before = numbers()
        self.assertTrue(before and 400 not in before, before)
        d = sim.dividers()
        sim.drag(d[5], d[3], d[5], d[3] - 5)
        after = numbers()
        self.assertTrue(after, "history view vanished after the layout change")
        self.assertEqual(after, list(range(after[0], after[0] + len(after))), "history lines out of order")
        self.assertNotIn(400, after, "the layout change snapped the scrolled view back to live")
        self.assertLess(abs(after[-1] - before[-1]), 12, (before[-1], after[-1]))
        self.assertEqual([f for f in sim.sender.of("input") if f[1].get("pane") == "host_shell"], [])


# ---------------------------------------------------------------------------------------------- keyboard
class KeyboardResizeTests(unittest.TestCase):
    def test_prefix_arrows_move_the_matching_divider_in_the_natural_direction(self):
        cases = [(RIGHT, "w", +1), (LEFT, "w", -1), (DOWN, "h", +1), (UP, "h", -1)]
        for key, axis, sign in cases:
            with self.subTest(key=key):
                sim = Sim(40, 140)
                widths, heights, mark = sim.widths(), sim.heights(), sim.mark()
                sim.keys(P + key)
                new_w, new_h = sim.widths(), sim.heights()
                if axis == "w":
                    self.assertGreater((new_w[0] - widths[0]) * sign, 0, f"{widths} -> {new_w}")
                    self.assertEqual(new_h, heights, "a horizontal key moved the vertical divider")
                else:
                    self.assertGreater((new_h[0] - heights[0]) * sign, 0, f"{heights} -> {new_h}")
                    self.assertEqual(new_w, widths, "a vertical key moved the horizontal divider")
                self.assertEqual(sim.after(mark, "input"), [], "the arrow was typed into a pane")
                tiled(self, sim.model)
                sent_matches_drawn(self, sim)

    def test_bare_arrows_repeat_inside_the_window_without_the_prefix(self):
        sim = Sim(40, 140)
        w0, mark = sim.widths()[0], sim.mark()
        sim.keys(P + RIGHT)
        w1 = sim.widths()[0]
        sim.keys(RIGHT, 0.4)
        w2 = sim.widths()[0]
        sim.keys(RIGHT, 0.4)
        w3 = sim.widths()[0]
        self.assertTrue(w0 < w1 < w2 < w3, (w0, w1, w2, w3))
        self.assertEqual(sim.after(mark, "input"), [], "a repeated arrow was delivered to the pane")
        sent_matches_drawn(self, sim)

    def test_repeat_window_is_renewed_by_each_bare_arrow(self):
        sim = Sim(40, 140)
        sim.keys(P + RIGHT)
        widths = [sim.widths()[0]]
        for _ in range(4):  # 0.7 s apart: each is within ~1 s of the previous one, total 2.8 s
            sim.keys(RIGHT, 0.7)
            widths.append(sim.widths()[0])
        self.assertEqual(widths, sorted(set(widths)), f"resize repeat stopped early: {widths}")

    def test_bare_arrow_after_the_window_is_a_normal_key(self):
        for late in (1.6, 3.0):
            with self.subTest(late=late):
                sim = Sim(40, 140)
                sim.keys(P + RIGHT)
                before, mark = rects(sim.model), sim.mark()
                sim.keys(RIGHT, late)
                self.assertEqual(rects(sim.model), before, "an arrow long after the repeat window still resized")
                self.assertEqual(b"".join(f[2] for f in sim.after(mark, "input")), RIGHT)

    def test_bare_arrow_without_any_prefix_is_a_normal_key(self):
        sim = Sim(40, 140)
        before, mark = rects(sim.model), sim.mark()
        for key in (LEFT, RIGHT, UP, DOWN):
            sim.keys(key)
        self.assertEqual(rects(sim.model), before)
        self.assertEqual(b"".join(f[2] for f in sim.after(mark, "input")), LEFT + RIGHT + UP + DOWN)

    def test_other_key_ends_the_repeat_and_is_delivered_normally(self):
        keys = {"letter": b"x", "digit": b"7", "enter": b"\r", "tab": b"\t", "ctrl-c": b"\x03", "space": b" ",
                "backspace": b"\x7f", "page-up": b"\x1b[5~", "home": b"\x1b[H", "f5": b"\x1b[15~"}
        for name, key in keys.items():
            with self.subTest(key=name):
                sim = Sim(40, 140)
                sim.keys(P + RIGHT)
                sim.keys(RIGHT, 0.1)
                mark = sim.mark()
                sim.keys(key, 0.1)
                self.assertEqual(b"".join(f[2] for f in sim.after(mark, "input")), key, "the ending key was swallowed")
                before = rects(sim.model)
                mark = sim.mark()
                sim.keys(RIGHT, 0.1)
                self.assertEqual(rects(sim.model), before, "the repeat survived a non-arrow key")
                self.assertEqual(b"".join(f[2] for f in sim.after(mark, "input")), RIGHT)

    def test_mixed_chunk_arrows_then_text_then_arrow(self):
        sim = Sim(40, 140)
        w0 = sim.widths()[0]
        sim.keys(P + RIGHT)
        mark = sim.mark()
        sim.keys(RIGHT + RIGHT + b"x" + RIGHT, 0.1)
        self.assertEqual(sim.widths()[0], w0 + 3 * 2, "two bare arrows in one read should each resize")
        self.assertEqual(b"".join(f[2] for f in sim.after(mark, "input")), b"x" + RIGHT)

    def test_lone_escape_ends_the_repeat(self):
        sim = Sim(40, 140)
        sim.keys(P + RIGHT)
        mark = sim.mark()
        sim.keys(b"\x1b", 0.1)
        sim.wait(0.2)  # a lone Esc is released after its hold time
        self.assertEqual(b"".join(f[2] for f in sim.after(mark, "input")), b"\x1b")
        before = rects(sim.model)
        sim.keys(RIGHT, 0.1)
        self.assertEqual(rects(sim.model), before)

    def test_prefix_command_after_prefix_arrow_is_handled_and_ends_the_repeat(self):
        sim = Sim(40, 140)
        sim.keys(P + RIGHT)
        sim.keys(P + b"2", 0.1)
        self.assertEqual(sim.model.focus, W)
        before, mark = rects(sim.model), sim.mark()
        sim.keys(RIGHT, 0.1)
        self.assertEqual(rects(sim.model), before)
        inputs = sim.after(mark, "input")
        self.assertEqual(b"".join(f[2] for f in inputs), RIGHT)
        self.assertEqual({f[1]["pane"] for f in inputs}, {"worker_omp"})

    def test_prefix_arrow_repeat_can_mix_axes(self):
        sim = Sim(40, 140)
        w0, h0 = sim.widths()[0], sim.heights()[0]
        sim.keys(P + RIGHT)
        sim.keys(DOWN, 0.2)
        self.assertGreater(sim.widths()[0], w0)
        self.assertGreater(sim.heights()[0], h0)

    def test_pane_clamp_stops_the_divider_and_sends_nothing_more(self):
        sim = Sim(40, 140)
        sim.keys(P + RIGHT)
        for _ in range(120):
            sim.keys(RIGHT, 0.05)
        w = sim.widths()
        self.assertGreaterEqual(inner(rects(sim.model)[W])[1], 10)
        mark = sim.mark()
        sim.keys(RIGHT, 0.05)
        self.assertEqual(sim.widths(), w)
        self.assertEqual(sim.after(mark, "resize"), [], "a clamped move still sent resize frames")
        self.assertEqual(sim.after(mark, "input"), [])

    def test_keyboard_changes_scroll_mode_untouched_and_scroll_mode_keys_are_not_resizes(self):
        sim = Sim(40, 140)
        sim.keys(P + b"[")  # scroll mode: arrows drive the view
        before, mark = rects(sim.model), sim.mark()
        sim.keys(RIGHT)
        self.assertEqual(rects(sim.model), before)
        self.assertEqual(sim.after(mark, "input"), [])


# ------------------------------------------------------------------------------------------- zoom and reset
class ZoomResetTests(unittest.TestCase):
    def test_zoom_shows_only_the_focused_pane_over_the_whole_body_and_restores(self):
        for focus, pane in (("manager_omp", M), ("worker_omp", W), ("host_shell", H)):
            with self.subTest(focus=focus):
                sim = Sim(40, 140, focus=focus)
                before = rects(sim.model)
                sim.keys(P + b"z")
                zoomed = rects(sim.model)
                self.assertEqual(set(zoomed), {pane}, "other panes must be hidden")
                self.assertEqual(zoomed[pane], (HEADER, 0, 40 - HEADER - FOOTER, 140))
                self.assertEqual(sim.last_sent()[pane.value], inner(zoomed[pane]), "no resize for the zoomed pane")
                mark = sim.mark()
                sim.keys(b"typed")
                self.assertEqual({f[1]["pane"] for f in sim.after(mark, "input")}, {pane.value})
                sim.keys(P + b"z")
                self.assertEqual(rects(sim.model), before, "unzoom did not restore the split")
                self.assertEqual(sim.last_sent()[pane.value], inner(before[pane]))

    def test_zoom_keeps_and_restores_custom_ratios(self):
        sim = Sim(40, 140)
        sim.keys(P + LEFT)
        for _ in range(8):
            sim.keys(LEFT, 0.1)
        sim.wait(1.5)
        sim.keys(P + DOWN)
        sim.wait(1.5)
        custom = rects(sim.model)
        self.assertNotEqual(custom, rects(Sim(40, 140).model))
        sim.keys(P + b"z")
        sim.keys(P + b"z")
        self.assertEqual(rects(sim.model), custom)
        sim.model.resize(50, 180)
        sim.keys(P + b"z")
        self.assertEqual(rects(sim.model)[M], (HEADER, 0, 50 - HEADER - FOOTER, 180))
        sim.keys(P + b"z")
        tiled(self, sim.model)
        min_inner_ok(self, sim.model)

    def test_focus_is_never_a_hidden_pane_while_zoomed(self):
        sim = Sim(40, 140)
        sim.keys(P + b"z")
        for key in (b"2", b"3", b"1", b"\t", b"\t"):
            sim.keys(P + key)
            self.assertIn(sim.model.focus, rects(sim.model), f"focus {sim.model.focus} is not visible after {key!r}")
            self.assertEqual(len(rects(sim.model)), 1)
        sim.keys(P + b"z")
        self.assertEqual(len(rects(sim.model)), 3)

    def test_zoomed_view_resizes_with_the_window_and_reports_sizes(self):
        sim = Sim(40, 140, focus="worker_omp")
        sim.keys(P + b"z")
        sim.model.resize(30, 100)
        sim.flush()
        self.assertEqual(rects(sim.model)[W], (HEADER, 0, 30 - HEADER - FOOTER, 100))
        self.assertEqual(sim.last_sent()["worker_omp"], (30 - HEADER - FOOTER - 2, 98))

    def test_no_divider_drag_while_zoomed_and_arrows_do_not_break_it(self):
        sim = Sim(40, 140)
        sim.keys(P + b"z")
        before = rects(sim.model)
        sim.drag(70, 20, 90, 20)
        sim.keys(P + RIGHT)
        self.assertEqual(rects(sim.model), before)
        self.assertEqual(len(before), 1)

    def test_reset_restores_the_default_split_sizes_and_frames(self):
        default = rects(Sim(40, 140).model)
        for zoomed in (False, True):
            with self.subTest(zoomed=zoomed):
                sim = Sim(40, 140)
                sim.keys(P + LEFT)
                sim.keys(LEFT, 0.1)
                sim.keys(P + UP, 1.5)
                sim.wait(1.5)
                if zoomed:
                    sim.keys(P + b"z")
                self.assertNotEqual(rects(sim.model), default)
                sim.keys(P + b"=")
                self.assertEqual(rects(sim.model), default)
                self.assertEqual(len(rects(sim.model)), 3, "reset must leave zoom too")
                sent_matches_drawn(self, sim)

    def test_reset_on_the_default_layout_is_harmless(self):
        sim = Sim(40, 140)
        default = rects(sim.model)
        mark = sim.mark()
        sim.keys(P + b"=")
        self.assertEqual(rects(sim.model), default)
        self.assertEqual(sim.after(mark, "input"), [])

    def test_layout_keys_never_reach_the_panes(self):
        sim = Sim(40, 140)
        mark = sim.mark()
        for chunk in (P + b"z", P + b"z", P + RIGHT, P + b"=", P + LEFT):
            sim.keys(chunk, 1.2)
        self.assertEqual(sim.after(mark, "input"), [])
        self.assertEqual(sim.after(mark, "paste"), [])

    def test_help_mentions_the_new_keys(self):
        from workbench.ui.product.model import HELP_LINES
        text = "\n".join(HELP_LINES)
        for needle in ("prefix z", "prefix ="):
            self.assertIn(needle, text)
        self.assertRegex(text, r"(?i)드래그|drag")


# ------------------------------------------------------------------------------------ rendered-screen helpers
def screen_boxes(lines: list[str]) -> list[tuple[int, int, int, int]]:
    """(top, left, height, width) of every drawn pane box (curses ACS corners l/k/m/j seen through the VT screen)."""
    found = []
    for bottom, line in enumerate(lines):
        for m in re.finditer(r"m(q+)j", line):
            left, width = m.start(), len(m.group(1)) + 2
            for top in range(bottom - 1, -1, -1):
                row = lines[top]
                if len(row) > left + width - 1 and row[left] == "l" and row[left + width - 1] == "k":
                    found.append((top, left, bottom - top + 1, width))
                    break
    return sorted(found)


def drawn(ui: UiPty) -> list[tuple[int, int, int, int]]:
    return screen_boxes(ui.screen_text().split("\n"))


def settled(ui: UiPty, predicate, timeout: float = 10.0) -> bool:
    return ui.wait_for(lambda: predicate(drawn(ui)), timeout)


def restored(output: bytes) -> list[str]:
    bad = []
    for mode in (b"1000", b"1006", b"1002"):
        on, off = output.rfind(b"\x1b[?" + mode + b"h"), output.rfind(b"\x1b[?" + mode + b"l")
        if on >= 0 and off < on:
            bad.append(f"?{mode.decode()} left on")
    on, off = output.rfind(b"\x1b[?2004h"), output.rfind(b"\x1b[?2004l")
    if on >= 0 and off < on:
        bad.append("?2004 left on")
    return bad


class PtyBase(unittest.TestCase):
    ROWS, COLS = 40, 140

    def setUp(self):
        self.server = ScriptedServer()
        self.work = Path(tempfile.mkdtemp(prefix="cw06-p27l-", dir="/tmp"))
        self.layout_file = self.server.root / "ui-layout.json"
        self.addCleanup(shutil.rmtree, self.work, True)
        self.addCleanup(self.server.close)

    def start_ui(self) -> UiPty:
        ui = UiPty(self.server.path, self.work, rows=self.ROWS, cols=self.COLS)
        self.addCleanup(ui.close)
        self.assertTrue(self.server.attached.wait(10), ui.screen_text())
        self.assertTrue(ui.wait_text("focus:", 10), ui.screen_text())
        self.assertTrue(settled(ui, lambda b: len(b) == 3), ui.screen_text())
        return ui

    def resize_frames(self) -> dict[str, tuple[int, int]]:
        out = {}
        for f in self.server.of("resize"):
            out[f.header["pane"]] = (f.header["rows"], f.header["cols"])
        return out

    def grab_points(self, ui: UiPty):
        boxes = drawn(ui)
        self.assertEqual(len(boxes), 3, ui.screen_text())
        (mt, ml, mh, mw), (wt, wl, wh, ww), (ht, hl, hh, hw) = boxes[0], boxes[1], boxes[2]
        if boxes[0][1] > boxes[1][1]:  # sorted by (top,left): manager then worker on the same top row
            raise AssertionError(boxes)
        return {"vx": ml + mw, "vy": mt + 1 + mh // 2, "hx": hl + hw // 2, "hy": ht + 1, "boxes": boxes}

    def server_matches_screen(self, ui: UiPty) -> None:
        names = ("manager_omp", "worker_omp", "host_shell")

        def state():
            boxes = drawn(ui)
            want = {n: (b[2] - 2, b[3] - 2) for n, b in zip(names, boxes)} if len(boxes) == 3 else None
            got = self.resize_frames()
            return want, {n: got.get(n) for n in names}

        ui.wait_for(lambda: state()[0] == state()[1], 5)  # a curses repaint / the debounced frame can lag a moment
        want, got = state()
        self.assertEqual(got, want, "backend was told sizes other than the drawn areas")


class PtyDragTests(PtyBase):
    def test_drag_moves_divider_on_screen_and_reports_exact_sizes(self):
        ui = self.start_ui()
        g = self.grab_points(ui)
        before = g["boxes"]
        ui.drain(0.3)
        start_model = OuterMouse.of(bytes(ui.output))
        self.assertTrue(start_model.clicks and start_model.wheel and start_model.motion,
                        f"mouse tracking must be on from the start (mode={start_model.mode} sgr={start_model.sgr})")
        mark = len(ui.output)
        ui.send(sgr(PRESS, g["vx"], g["vy"]))
        for i in range(1, 16):
            ui.send(sgr(MOTION, g["vx"] - i, g["vy"]))
            time.sleep(0.02)
        ui.send(sgr(PRESS, g["vx"] - 15, g["vy"], release=True))
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][3] == before[0][3] - 15), ui.screen_text())
        self.assertTrue(self.server.wait(lambda: self.resize_frames().get("manager_omp") == (before[0][2] - 2,
                                                                                            before[0][3] - 17)))
        self.server_matches_screen(ui)
        self.assertEqual(self.server.payloads("input"), b"", "the drag typed something into a pane")
        self.assertEqual(self.server.of("focus"), [], "the drag changed focus")
        # horizontal divider
        ui.send(sgr(PRESS, g["hx"], g["hy"]))
        for i in range(1, 6):
            ui.send(sgr(MOTION, g["hx"], g["hy"] + i))
            time.sleep(0.02)
        ui.send(sgr(PRESS, g["hx"], g["hy"] + 5, release=True))
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][2] == before[0][2] + 5), ui.screen_text())
        self.assertTrue(self.server.wait(lambda: self.resize_frames().get("host_shell") == (before[2][2] - 2 - 5,
                                                                                           before[2][3] - 2)))
        self.server_matches_screen(ui)
        ui.drain(0.3)
        self.assertEqual(OuterMouse.of(bytes(ui.output)).changes_after(mark), [],
                         "mouse modes were switched during/after divider drags")
        assert_mouse_reporting_on(self, ui.output, "after two divider drags")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())
        ui.drain(0.3)
        self.assertEqual(restored(bytes(ui.output)), [])
        assert_mouse_off(self, ui.output, "after detach")
        self.assertEqual(ui.status(), 0)

    def test_drag_modes_are_off_outside_a_drag_and_mouse_capture_stays_on(self):
        # renamed contract: tracking is on from the start and is NOT switched by anything but prefix m / exit
        ui = self.start_ui()
        g = self.grab_points(ui)
        ui.drain(0.3)
        mark = len(ui.output)
        assert_mouse_reporting_on(self, ui.output, "at start")
        ui.send(P + RIGHT + RIGHT + P + b"z" + P + b"z" + P + b"=")
        m = g["boxes"][0]
        ui.send(sgr(PRESS, m[1] + 6, m[0] + 3) + sgr(PRESS, m[1] + 6, m[0] + 3, release=True))  # interior click
        ui.send(sgr(64, m[1] + 6, m[0] + 3))  # wheel
        ui.send(sgr(2, g["vx"], g["vy"]) + sgr(2, g["vx"], g["vy"], release=True))  # right click on a divider
        ui.drain(0.5)
        self.assertEqual(OuterMouse.of(bytes(ui.output)).changes_after(mark), [],
                         "mouse modes switched by layout keys / clicks / wheel")
        assert_mouse_reporting_on(self, ui.output)
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())
        ui.drain(0.3)
        self.assertEqual(restored(bytes(ui.output)), [])
        assert_mouse_off(self, ui.output, "after detach")

    def test_no_drag_reporting_when_mouse_capture_is_off(self):
        ui = self.start_ui()
        g = self.grab_points(ui)
        ui.drain(0.3)
        assert_mouse_reporting_on(self, ui.output, "at start")
        ui.send(P + b"m")
        self.assertTrue(ui.wait_for(lambda: OuterMouse.of(bytes(ui.output)).mode is None, 5),
                        "prefix m did not turn mouse tracking off")
        assert_mouse_off(self, ui.output, "after prefix m")
        mark = len(ui.output)
        ui.send(sgr(PRESS, g["vx"], g["vy"]) + sgr(MOTION, g["vx"] + 9, g["vy"]) +
                sgr(PRESS, g["vx"] + 9, g["vy"], release=True))
        ui.drain(0.5)
        self.assertEqual([c for c in OuterMouse.of(bytes(ui.output)).changes_after(mark) if c[2]], [],
                         "a mouse mode was switched ON while capture is off")
        assert_mouse_off(self, ui.output, "a divider press while capture is off")
        self.assertEqual(drawn(ui), g["boxes"])
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())

    def test_flood_of_motion_reports_is_debounced_on_the_wire(self):
        ui = self.start_ui()
        g = self.grab_points(ui)
        before = self.resize_frames()
        count_before = len(self.server.of("resize"))
        ui.send(sgr(PRESS, g["vx"], g["vy"]))
        burst = b"".join(sgr(MOTION, g["vx"] - (i % 30), g["vy"]) for i in range(1, 400))
        ui.send(burst)
        ui.send(sgr(PRESS, g["vx"] - 20, g["vy"], release=True))
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][3] == g["boxes"][0][3] - 20), ui.screen_text())
        self.assertTrue(self.server.wait(lambda: self.resize_frames().get("manager_omp") is not None))
        ui.drain(0.6)
        manager = [f for f in self.server.of("resize")[count_before:] if f.header["pane"] == "manager_omp"]
        self.assertLessEqual(len(manager), 20, f"{len(manager)} manager resize frames for a 400-report flood")
        self.server_matches_screen(ui)
        self.assertNotEqual(before, {})
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())

    def _drag_then(self, action: str):
        ui = self.start_ui()
        g = self.grab_points(ui)
        ui.drain(0.3)
        assert_mouse_reporting_on(self, ui.output, "at start")
        ui.send(sgr(PRESS, g["vx"], g["vy"]) + sgr(MOTION, g["vx"] - 6, g["vy"]))
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][3] == g["boxes"][0][3] - 6),
                        "the drag never started (divider did not follow the mouse)")
        pid = ui.ui_pid()
        self.assertIsNotNone(pid)
        if action == "detach":
            ui.send(P + b"d")
        elif action == "sigterm":
            os.kill(pid, signal.SIGTERM)
        elif action == "sighup":
            os.kill(pid, signal.SIGHUP)
        elif action == "closing":
            self.server.closing("backend_shutdown")
        elif action == "drop":
            self.server.drop()
        elif action == "prefix-m":
            ui.send(P + b"m")
        return ui

    def assert_drag_mode_closed(self, ui: UiPty, *, done: bool = True, need_on: bool = True) -> None:
        if done:
            self.assertTrue(ui.ui_done(15), ui.screen_text())
        ui.drain(0.4)
        out = bytes(ui.output)
        assert_mouse_off(self, out, "on this exit path")
        self.assertEqual(restored(out), [], "terminal modes left on")

    def test_drag_mode_is_closed_on_every_exit_path(self):
        for action in ("detach", "sigterm", "sighup", "closing", "drop"):
            with self.subTest(exit=action):
                self.setUp()  # fresh server per path (cleanups accumulate; released at the end)
                ui = self._drag_then(action)
                self.assert_drag_mode_closed(ui)
                if action == "detach":
                    self.assertEqual(ui.status(), 0)

    def test_drag_mode_is_closed_when_capture_is_switched_off_mid_drag(self):
        ui = self._drag_then("prefix-m")
        self.assertTrue(ui.wait_for(lambda: OuterMouse.of(bytes(ui.output)).mode is None, 5),
                        "mouse tracking not turned off when capture was switched off mid-drag")
        assert_mouse_off(self, ui.output, "after prefix m mid-drag")
        ui.send(P + b"d")
        self.assert_drag_mode_closed(ui)

    def test_drag_mode_is_closed_when_the_window_becomes_too_small_mid_drag(self):
        ui = self._drag_then("none")
        ui.resize(6, 20)
        ui.drain(0.6)
        ui.resize(self.ROWS, self.COLS)
        ui.drain(0.6)
        # the drag ended with the too-small excursion; mouse capture itself is still on for the terminal
        assert_mouse_reporting_on(self, ui.output, "after a too-small excursion mid-drag")
        ui.send(P + b"d")
        self.assert_drag_mode_closed(ui)

    def test_second_press_without_release_does_not_leave_drag_mode_on(self):
        ui = self.start_ui()
        g = self.grab_points(ui)
        ui.send(sgr(PRESS, g["vx"], g["vy"]) + sgr(PRESS, g["vx"] + 3, g["vy"]) + sgr(MOTION + 3, g["vx"] + 5, g["vy"]))
        ui.send(P + b"d")
        self.assert_drag_mode_closed(ui, need_on=False)  # the whole sequence may be handled inside one read


class PtyLayoutKeysTests(PtyBase):
    def test_prefix_arrows_zoom_and_reset_on_the_real_screen(self):
        ui = self.start_ui()
        first = drawn(ui)
        ui.send(P + LEFT)
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][3] < first[0][3]), ui.screen_text())
        ui.send(LEFT + LEFT)
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][3] <= first[0][3] - 6), ui.screen_text())
        self.server_matches_screen(ui)
        ui.send(P + b"z")
        self.assertTrue(settled(ui, lambda b: b == [(2, 0, self.ROWS - 3, self.COLS)]), ui.screen_text())
        self.assertTrue(self.server.wait(lambda: self.resize_frames().get("manager_omp") ==
                                         (self.ROWS - 3 - 2, self.COLS - 2)))
        ui.send(P + b"z")
        self.assertTrue(settled(ui, lambda b: len(b) == 3), ui.screen_text())
        self.server_matches_screen(ui)
        ui.send(P + b"=")
        self.assertTrue(settled(ui, lambda b: b == first), ui.screen_text())
        self.server_matches_screen(ui)
        self.assertEqual(self.server.payloads("input"), b"")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())

    def test_window_resize_keeps_the_custom_ratio_on_the_real_screen(self):
        ui = self.start_ui()
        ui.send(P + LEFT + LEFT * 12)
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][3] < 66), ui.screen_text())
        ratio = drawn(ui)[0][3] / self.COLS
        ui.resize(50, 200)
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[2][3] == 200), ui.screen_text())
        self.assertAlmostEqual(drawn(ui)[0][3] / 200, ratio, delta=0.03)
        self.server_matches_screen(ui)
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())


class PtyLayoutFileTests(PtyBase):
    BAD = {
        "empty": b"",
        "binary": b"\x00\xff\xfegarbage\x00",
        "truncated": b'{"version": 1, "col_ratio": 0.2',
        "array": b"[]",
        "scalar": b"42",
        "null": b"null",
        "wrong-version": b'{"version": 99, "col_ratio": 0.2, "row_ratio": 0.3, "zoom": false}',
        "no-version": b'{"col_ratio": 0.2, "row_ratio": 0.3, "zoom": false}',
        "ratio-string": b'{"version": 1, "col_ratio": "0.2", "row_ratio": 0.3, "zoom": false}',
        "ratio-zero": b'{"version": 1, "col_ratio": 0, "row_ratio": 0.3, "zoom": false}',
        "ratio-one": b'{"version": 1, "col_ratio": 1, "row_ratio": 0.3, "zoom": false}',
        "ratio-huge": b'{"version": 1, "col_ratio": 5000, "row_ratio": 0.3, "zoom": false}',
        "ratio-negative": b'{"version": 1, "col_ratio": -0.4, "row_ratio": 0.3, "zoom": false}',
        "ratio-nan": b'{"version": 1, "col_ratio": NaN, "row_ratio": 0.3, "zoom": false}',
        "ratio-infinity": b'{"version": 1, "col_ratio": Infinity, "row_ratio": 0.3, "zoom": false}',
        "ratio-bool": b'{"version": 1, "col_ratio": true, "row_ratio": 0.3, "zoom": false}',
        "zoom-string": b'{"version": 1, "col_ratio": 0.2, "row_ratio": 0.3, "zoom": "yes"}',
        "deep-nesting": b"[" * 5000 + b"]" * 5000,
        "oversized": b'{"version": 1, "pad": "' + b"x" * (1 << 20) + b'"}',
    }

    def test_corrupt_or_unknown_layout_files_are_ignored(self):
        default = None
        for name, content in self.BAD.items():
            with self.subTest(file=name):
                self.setUp()
                self.layout_file.write_bytes(content)
                ui = self.start_ui()
                boxes = drawn(ui)
                if default is None:
                    default = boxes
                self.assertEqual(len(boxes), 3, f"{name}: UI did not come up with the three panes")
                self.assertEqual(boxes, default, f"{name}: a bad layout file changed the geometry")
                self.server_matches_screen(ui)
                ui.send(P + b"d")
                self.assertTrue(ui.ui_done(), ui.screen_text())
                self.assertEqual(ui.status(), 0, f"{name}: UI exited non-zero")

    def test_valid_file_is_applied_on_attach(self):
        self.layout_file.write_text(json.dumps({"version": 1, "col_ratio": 0.3, "row_ratio": 0.6, "zoom": False}))
        ui = self.start_ui()
        boxes = drawn(ui)
        self.assertAlmostEqual(boxes[0][3] / self.COLS, 0.3, delta=0.03)
        self.assertAlmostEqual(boxes[0][2] / (self.ROWS - 3), 0.6, delta=0.06)
        self.server_matches_screen(ui)
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())

    def test_symlink_layout_file_is_not_followed_for_reading_or_writing(self):
        victim = self.work / "victim.json"
        victim.write_text(json.dumps({"version": 1, "col_ratio": 0.25, "row_ratio": 0.5, "zoom": False}))
        victim_bytes = victim.read_bytes()
        os.symlink(victim, self.layout_file)
        ui = self.start_ui()
        default = drawn(ui)
        self.assertAlmostEqual(default[0][3] / self.COLS, 0.5, delta=0.02, msg="a symlinked layout file was read")
        ui.send(P + LEFT + LEFT)
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][3] < default[0][3]), ui.screen_text())
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())
        self.assertEqual(victim.read_bytes(), victim_bytes, "the layout was written through a symlink")

    def test_layout_change_is_persisted_atomically_with_mode_0600(self):
        ui = self.start_ui()
        ui.send(P + LEFT + LEFT * 6)
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][3] <= 60), ui.screen_text())
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())
        self.assertTrue(self.layout_file.exists(), "no ui-layout.json next to the UI socket")
        mode = stat.S_IMODE(self.layout_file.stat().st_mode)
        self.assertEqual(mode, 0o600)
        data = json.loads(self.layout_file.read_text())
        self.assertIsInstance(data, dict)
        self.assertEqual([p.name for p in self.server.root.iterdir() if p.name not in {"ui.sock", "ui-layout.json"}],
                         [], "temp files left next to the layout file")
        # second run: the stored layout is applied
        server2 = ScriptedServer()
        self.addCleanup(server2.close)
        shutil.copy(self.layout_file, server2.root / "ui-layout.json")
        ui2 = UiPty(server2.path, self.work, rows=self.ROWS, cols=self.COLS)
        self.addCleanup(ui2.close)
        self.assertTrue(server2.attached.wait(10))
        self.assertTrue(settled(ui2, lambda b: len(b) == 3 and b[0][3] <= 60), ui2.screen_text())
        ui2.send(P + b"d")

    def test_reset_is_persisted_as_the_default(self):
        ui = self.start_ui()
        default = drawn(ui)
        ui.send(P + LEFT + LEFT * 6)
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][3] < default[0][3]))
        ui.send(P + b"=")
        self.assertTrue(settled(ui, lambda b: b == default))
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())
        server2 = ScriptedServer()
        self.addCleanup(server2.close)
        if self.layout_file.exists():
            shutil.copy(self.layout_file, server2.root / "ui-layout.json")
        ui2 = UiPty(server2.path, self.work, rows=self.ROWS, cols=self.COLS)
        self.addCleanup(ui2.close)
        self.assertTrue(server2.attached.wait(10))
        self.assertTrue(settled(ui2, lambda b: b == default), ui2.screen_text())
        ui2.send(P + b"d")


# -------------------------------------------------------------------- real entrypoint + real backend + stub OMP
STUB = '''#!/usr/bin/env python3
import os, sys, tty
if "--version" in sys.argv:
    print("omp/18.2.10"); sys.exit(0)
role = os.environ.get("WORKBENCH_G3_ROLE", "?")
tty.setraw(0)
sys.stdout.write(f"STUB-OMP {role} ready\\r\\n> "); sys.stdout.flush()
while True:
    data = os.read(0, 4096)
    if not data:
        break
    sys.stdout.write(f"[{role} got {data!r}]\\r\\n> "); sys.stdout.flush()
'''
ENV = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
       "TERM": "xterm-256color"}
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"


def proc_identity(pid: int) -> tuple[int, str] | None:
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat_line.rsplit(")", 1)[1].split()
    return None if fields[0] in "ZX" else (pid, fields[19])


def procs_naming(root: Path) -> dict[int, str]:
    needle, found = str(root).encode(), {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            blob = (entry / "cmdline").read_bytes() + (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in blob and (ident := proc_identity(int(entry.name))):
            found[ident[0]] = ident[1]
    return found


class RealEntrypointBase(unittest.TestCase):
    ROWS, COLS = 40, 140

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw06-p27l-rt-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.omp = self.root / "omp"
        self.omp.write_text(STUB)
        self.omp.chmod(0o755)
        self.data = self.root / "d"
        self.seen: dict[int, str] = {}
        self.leaked: list[int] = []
        self.addCleanup(self.stop_backend)

    def cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, "-c", MAIN, *args], env=ENV, cwd=self.root, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def stop_backend(self):
        self.seen.update(procs_naming(self.root))
        self.cli("shutdown", "--data-dir", str(self.data), "--yes")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(proc_identity(p) == (p, t) for p, t in self.seen.items()):
            time.sleep(0.1)
        for pid, start in self.seen.items():  # exact identity (pid + starttime) only
            if proc_identity(pid) != (pid, start):
                continue
            self.leaked.append(pid)
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            try:
                if proc_identity(pid) == (pid, start):
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)

    def attach_ui(self) -> UiPty:
        ui = UiPty.__new__(UiPty)
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", self.ROWS, self.COLS, 0, 0))
        ui.rows, ui.cols = self.ROWS, self.COLS
        ui.process = subprocess.Popen(["/usr/bin/setsid", "--ctty", sys.executable, "-c", MAIN, "attach",
                                       "--data-dir", str(self.data)], stdin=slave, stdout=slave, stderr=slave,
                                      env=ENV, close_fds=True)
        os.close(slave)
        ui.fd, ui.output = master, bytearray()
        ui.before_file = ui.after_file = ui.status_file = self.root / "unused"
        self.addCleanup(ui.close)
        return ui

    def ready(self, ui: UiPty) -> None:
        self.assertTrue(ui.wait_for(lambda: "STUB-OMP manager ready" in ui.screen_text()
                                    and "STUB-OMP worker ready" in ui.screen_text(), 30), ui.screen_text())
        self.seen.update(procs_naming(self.root))

    def detach(self, ui: UiPty) -> None:
        ui.send(P + b"d")
        self.assertTrue(ui.wait_for(lambda: b"detached; backend keeps running" in bytes(ui.output), 10))
        self.assertEqual(ui.process.wait(15), 0, "attach process did not exit cleanly after detach")
        ui.drain(0.2)
        self.assertEqual(restored(bytes(ui.output)), [], "terminal modes left on after detach")


class RealEntrypointLayoutTests(RealEntrypointBase):
    def start(self):
        out = self.cli("start", "--data-dir", str(self.data), "--omp", str(self.omp), "--no-attach", "--timeout", "3")
        self.assertIn("starting backend", out.stdout)

    def host_stty(self, ui: UiPty) -> tuple[int, int]:
        mark = len(ui.output)
        ui.send(b"stty size\r")
        pattern = re.compile(rb"(?<![\d;])(\d+) (\d+)\r?\n")
        self.assertTrue(ui.wait_for(lambda: pattern.search(bytes(ui.output[mark:]).replace(b"\x1b[K", b"")) is not None
                                    or self._stty_from_screen(ui) is not None, 20), ui.screen_text())
        return self._stty_from_screen(ui)

    @staticmethod
    def _stty_from_screen(ui: UiPty) -> tuple[int, int] | None:
        lines = ui.screen_text().split("\n")
        boxes = screen_boxes(lines)
        if len(boxes) != 3:
            return None
        top, left, height, width = boxes[2]
        for row in lines[top + 1:top + height - 1][::-1]:
            m = re.fullmatch(r"(\d+) (\d+)\s*", row[left + 1:left + width - 1])
            if m:
                return int(m.group(1)), int(m.group(2))
        return None

    def test_layout_survives_detach_and_reattach_and_reaches_the_real_shell(self):
        self.start()
        ui = self.attach_ui()
        self.ready(ui)
        default = drawn(ui)
        self.assertEqual(len(default), 3, ui.screen_text())
        # drag both dividers with the mouse
        vx, vy = default[0][1] + default[0][3], default[0][0] + 1 + default[0][2] // 2
        hx, hy = default[2][1] + default[2][3] // 2, default[2][0] + 1
        ui.send(sgr(PRESS, vx, vy))
        for i in range(1, 25):
            ui.send(sgr(MOTION, vx - i, vy))
            time.sleep(0.02)
        ui.send(sgr(PRESS, vx - 24, vy, release=True))
        ui.send(sgr(PRESS, hx, hy))
        for i in range(1, 5):
            ui.send(sgr(MOTION, hx, hy - i))
            time.sleep(0.02)
        ui.send(sgr(PRESS, hx, hy - 4, release=True))
        want_w, want_h = default[0][3] - 24, default[0][2] - 4
        self.assertTrue(settled(ui, lambda b: len(b) == 3 and b[0][3] == want_w and b[0][2] == want_h, 15),
                        ui.screen_text())
        moved = drawn(ui)
        ui.send(P + b"3")
        self.assertTrue(ui.wait_text("focus: HOST SHELL", 15), ui.screen_text())
        got = self.host_stty(ui)
        self.assertEqual(got, (moved[2][2] - 2, moved[2][3] - 2), "the real host shell size != drawn host pane")
        self.detach(ui)
        layout_file = self.data / "ui-layout.json"
        self.assertTrue(layout_file.exists(), "no <data dir>/ui-layout.json after detach")
        self.assertEqual(stat.S_IMODE(layout_file.stat().st_mode), 0o600)
        json.loads(layout_file.read_text())
        self.assertEqual([p.name for p in self.data.iterdir() if p.name.startswith(".ui-layout")], [])
        # reattach: same geometry, same real shell size
        ui2 = self.attach_ui()
        self.assertTrue(ui2.wait_for(lambda: len(drawn(ui2)) == 3, 30), ui2.screen_text())
        self.assertTrue(settled(ui2, lambda b: b == moved, 15), f"{drawn(ui2)} != {moved}\n{ui2.screen_text()}")
        ui2.send(P + b"3")
        self.assertTrue(ui2.wait_text("focus: HOST SHELL", 15), ui2.screen_text())
        got2 = self.host_stty(ui2)
        self.assertEqual(got2, (moved[2][2] - 2, moved[2][3] - 2), "reattached shell does not have the stored size")
        # zoom persists too
        ui2.send(P + b"z")
        self.assertTrue(settled(ui2, lambda b: len(b) == 1), ui2.screen_text())
        self.detach(ui2)
        ui3 = self.attach_ui()
        self.assertTrue(ui3.wait_for(lambda: len(drawn(ui3)) >= 1, 30), ui3.screen_text())
        self.assertTrue(settled(ui3, lambda b: len(b) == 1, 15), "zoom state was not restored on reattach")
        ui3.send(P + b"=")
        self.assertTrue(settled(ui3, lambda b: b == default, 15), f"{drawn(ui3)} != {default}")
        self.detach(ui3)
        ui4 = self.attach_ui()
        self.assertTrue(ui4.wait_for(lambda: len(drawn(ui4)) == 3, 30), ui4.screen_text())
        self.assertTrue(settled(ui4, lambda b: b == default, 15), "reset was not persisted")
        self.detach(ui4)
        self.assertEqual(self.cli("status", "--data-dir", str(self.data), "--json").returncode, 0)
        self.stop_backend()
        self.assertEqual(self.leaked, [], "leaked processes")

    def test_corrupt_layout_file_is_ignored_by_the_real_entrypoint(self):
        self.start()
        (self.data / "ui-layout.json").write_bytes(b"\x00{not json")
        ui = self.attach_ui()
        self.ready(ui)
        boxes = drawn(ui)
        self.assertEqual(len(boxes), 3, ui.screen_text())
        self.assertLessEqual(abs(boxes[0][3] - boxes[1][3]), 1, "corrupt file changed the default split")
        ui.send(P + b"3")
        self.assertTrue(ui.wait_text("focus: HOST SHELL", 15), ui.screen_text())
        self.assertEqual(self.host_stty(ui), (boxes[2][2] - 2, boxes[2][3] - 2))
        self.detach(ui)
        self.assertEqual(self.cli("status", "--data-dir", str(self.data), "--json").returncode, 0)
        self.stop_backend()
        self.assertEqual(self.leaked, [], "leaked processes")


if __name__ == "__main__":
    unittest.main()
