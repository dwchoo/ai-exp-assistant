"""Independent CW-06 mux-scrolling tests (p27-cd59-test-02, unit V-CW-06-p2.7-resize).

Expectations come from the review finding R2 (p27-cd59-review-01) and the user's requirements, not from the fix:

- the user runs the product UI inside tmux or herdr and needs scrolling (wheel + keyboard fallback) to keep working;
  the real-world failure was "after the first divider drag the wheel is dead" because the UI switched ``?1002`` on for
  the drag and ``?1002l`` off again, and tmux / xterm / herdr treat ``?1000 / ?1002 / ?1003`` as ONE tracking mode, so
  the reset turned ALL mouse reporting off (the UI kept believing capture was on).
- therefore the outer terminal is modelled with a single tracking mode (``OuterMouse``: the last of 1000/1002/1003 set
  wins, a reset of ANY of them turns tracking off; ``?1006`` SGR is independent). After N divider drags (any kind of
  drag end: release, lost release, prefix command, too-small window) and any number of prefix m off/on cycles the
  model must still report wheel and clicks whenever capture is on, and everything must be off after capture-off and
  after every exit path; the UI must actually react to the reported wheel and clicks.
- keyboard fallbacks Shift+PgUp/PgDn, prefix PgUp and prefix [ scroll without any mouse reporting.
- the backend's OMP isolation warning is visible in the header (two header rows) when the check found a problem.

Runtime: the real product UI loop on an owned PTY against a scripted ui_v1 fixture (no OMP, no credentials, no
multiplexer). Live tmux/herdr counterparts are opt-in: ``tests/ui/live_mux_independent_p27n.py``.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from independent_support_cw06 import ScriptedServer, UiPty, snap  # noqa: E402
from test_product_resize_independent_p27l import (  # noqa: E402
    MOTION, PRESS, OuterMouse, screen_boxes, sgr)
from workbench.ui.product.input import PREFIX  # noqa: E402

P = bytes([PREFIX])
SHIFT_PGUP, SHIFT_PGDN, PGUP = b"\x1b[5;2~", b"\x1b[6;2~", b"\x1b[5~"
WHEEL_UP, WHEEL_DOWN = 64, 65
LINES = b"".join(b"line %03d\r\n" % i for i in range(80))


def drawn(ui: UiPty) -> list[tuple[int, int, int, int]]:
    return screen_boxes(ui.screen_text().split("\n"))


def settled(ui: UiPty, predicate, timeout: float = 10.0) -> bool:
    return ui.wait_for(lambda: predicate(drawn(ui)), timeout)


def model_of(ui: UiPty) -> OuterMouse:
    return OuterMouse.of(bytes(ui.output))


class OuterMouseModelSelfTests(unittest.TestCase):
    """The model must reproduce the observed tmux 3.4 behaviour, otherwise the PTY tests below prove nothing."""

    ON = b"\x1b[?1000h\x1b[?1002h\x1b[?1006h"

    def test_the_old_drag_toggle_kills_reporting_the_new_contract_does_not(self):
        old = self.ON + b"\x1b[?1002l"  # divider release under the superseded contract
        model = OuterMouse.of(old)
        self.assertIsNone(model.mode)
        self.assertFalse(model.clicks or model.wheel or model.motion)
        for kept in (self.ON, self.ON + b"\x1b[?25h", b"\x1b[?1000h\x1b[?1006h"):
            self.assertTrue(OuterMouse.of(kept).clicks and OuterMouse.of(kept).wheel, kept)

    def test_single_tracking_mode_semantics(self):
        self.assertEqual(OuterMouse.of(b"\x1b[?1000h\x1b[?1002h").mode, "button")  # 1002 replaces 1000
        self.assertEqual(OuterMouse.of(b"\x1b[?1002h\x1b[?1000h").mode, "press")  # ... and 1000 replaces 1002
        self.assertIsNone(OuterMouse.of(b"\x1b[?1002h\x1b[?1000l").mode)  # any reset clears the single mode
        self.assertIsNone(OuterMouse.of(b"\x1b[?1000h\x1b[?1003l").mode)
        self.assertFalse(OuterMouse.of(b"\x1b[?1000h").clicks, "no SGR encoding, no usable reports")
        self.assertTrue(OuterMouse.of(b"\x1b[?1002h\x1b[?1006h").motion)
        self.assertFalse(OuterMouse.of(b"\x1b[?1000h\x1b[?1006h").motion, "press tracking sends no drag motion")
        self.assertEqual(OuterMouse.of(self.ON + b"\x1b[?1006l\x1b[?1002l\x1b[?1000l").sgr, False)


class MuxBase(unittest.TestCase):
    ROWS, COLS = 40, 140

    def setUp(self):
        self.server = ScriptedServer(replay={"manager_omp": LINES, "worker_omp": LINES.replace(b"line", b"work"),
                                             "host_shell": LINES.replace(b"line", b"host")})
        self.work = Path(tempfile.mkdtemp(prefix="cw06-p27n-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.addCleanup(self.server.close)

    def start_ui(self) -> UiPty:
        ui = UiPty(self.server.path, self.work, rows=self.ROWS, cols=self.COLS)
        self.addCleanup(ui.close)
        self.assertTrue(self.server.attached.wait(10), ui.screen_text())
        self.assertTrue(ui.wait_text("focus:", 10), ui.screen_text())
        self.assertTrue(settled(ui, lambda b: len(b) == 3), ui.screen_text())
        self.assertTrue(ui.wait_text("line 079", 10), ui.screen_text())
        ui.drain(0.3)
        return ui

    @staticmethod
    def points(ui: UiPty):
        boxes = drawn(ui)
        (mt, ml, mh, mw), (wt, wl, wh, ww), (ht, hl, hh, hw) = boxes
        return {"vx": ml + mw, "vy": mt + 1 + mh // 2, "hx": hl + hw // 2, "hy": ht + 1, "boxes": boxes,
                "worker": (wt + 3, wl + 8), "manager": (mt + 3, ml + 8)}

    def assert_reports(self, ui: UiPty, msg: str = "") -> None:
        model = model_of(ui)
        self.assertTrue(model.clicks and model.wheel,
                        f"outer terminal reports neither clicks nor wheel (mode={model.mode}, sgr={model.sgr}) {msg}")

    def assert_off(self, ui: UiPty, msg: str = "") -> None:
        model = model_of(ui)
        self.assertEqual((model.mode, model.sgr), (None, False), f"mouse modes left on {msg}")

    def wheel_scrolls(self, ui: UiPty, msg: str = "") -> None:
        """The UI reacts to the wheel: a wheel-up over the worker pane shows a scrolled worker view, wheel-down returns."""
        pt = self.points(ui)["worker"]
        ui.send(sgr(WHEEL_UP, pt[1], pt[0]) * 2)
        self.assertTrue(ui.wait_for(lambda: "WORKER OMP [SCROLL" in ui.screen_text(), 5),
                        f"the wheel did not scroll the pane {msg}\n" + ui.screen_text())
        ui.send(sgr(WHEEL_DOWN, pt[1], pt[0]) * 64)
        self.assertTrue(ui.wait_for(lambda: "[SCROLL" not in ui.screen_text(), 5),
                        f"wheel-down did not return to live {msg}\n" + ui.screen_text())

    def click_focuses(self, ui: UiPty, pane: str, msg: str = "") -> None:
        pts = self.points(ui)
        y, x = pts["worker" if pane == "worker_omp" else "manager"]
        before = len(self.server.of("focus"))
        ui.send(sgr(PRESS, x, y) + sgr(PRESS, x, y, release=True))
        self.assertTrue(self.server.wait(lambda: len(self.server.of("focus")) > before), f"click did not focus {msg}")
        self.assertEqual(self.server.of("focus")[-1].header.get("pane"), pane)

    def drag(self, ui: UiPty, axis: str, delta: int, *, release: bool = True) -> None:
        pts = self.points(ui)
        before = pts["boxes"]
        if axis == "v":
            x0, y0, x1, y1 = pts["vx"], pts["vy"], pts["vx"] + delta, pts["vy"]
            expect = lambda b: len(b) == 3 and b[0][3] == before[0][3] + delta  # noqa: E731
        else:
            x0, y0, x1, y1 = pts["hx"], pts["hy"], pts["hx"], pts["hy"] + delta
            expect = lambda b: len(b) == 3 and b[0][2] == before[0][2] + delta  # noqa: E731
        ui.send(sgr(PRESS, x0, y0))
        steps = max(abs(delta), 1)
        for i in range(1, steps + 1):
            ui.send(sgr(MOTION, x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps))
            time.sleep(0.01)
        if release:
            ui.send(sgr(PRESS, x1, y1, release=True))
        self.assertTrue(settled(ui, expect), f"{axis}-drag by {delta} did not move the divider\n" + ui.screen_text())


class OuterTerminalAfterDragsTests(MuxBase):
    def test_tracking_is_enabled_once_at_start_in_a_fallback_friendly_order(self):
        ui = self.start_ui()
        out = bytes(ui.output)
        first = {n: out.find(b"\x1b[?%dh" % n) for n in (1000, 1002, 1006)}
        for number, at in first.items():
            self.assertGreaterEqual(at, 0, f"?{number}h never enabled")
        self.assertLess(first[1000], first[1002], "?1000h must come first so a press-only terminal keeps reporting")
        model = model_of(ui)
        self.assertEqual((model.mode, model.sgr, model.motion), ("button", True, True))
        for number in (1000, 1002, 1006):
            self.assertEqual(out.count(b"\x1b[?%dh" % number), 1, f"?{number}h written more than once at start")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())

    def test_after_many_divider_drags_the_outer_terminal_still_reports_wheel_and_clicks(self):
        ui = self.start_ui()
        mark = len(ui.output)
        for i, (axis, delta) in enumerate((("v", -12), ("h", 4), ("v", 20), ("h", -3), ("v", -8), ("h", 5), ("v", 6),
                                           ("h", -6))):
            self.drag(ui, axis, delta)
            ui.drain(0.1)
            self.assert_reports(ui, f"after drag #{i + 1} ({axis} {delta})")
        self.assertEqual(model_of(ui).changes_after(mark), [], "mouse modes were switched during the drags")
        self.wheel_scrolls(ui, "after 8 drags")
        self.click_focuses(ui, "worker_omp", "after 8 drags")
        self.click_focuses(ui, "manager_omp", "after 8 drags")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())
        ui.drain(0.3)
        self.assert_off(ui, "after detach")
        self.assertEqual(ui.status(), 0)

    def test_every_way_a_drag_can_end_keeps_the_terminal_reporting(self):
        ui = self.start_ui()
        mark = len(ui.output)
        # 1. press+motion, then the release report is lost and a new press arrives
        pts = self.points(ui)
        ui.send(sgr(PRESS, pts["vx"], pts["vy"]) + sgr(MOTION, pts["vx"] - 5, pts["vy"]))
        ui.send(sgr(PRESS, pts["vx"] - 5, pts["vy"] + 2))  # second press without a release
        ui.drain(0.3)
        self.assert_reports(ui, "after a lost release")
        # 2. a window that becomes too small in the middle of a drag, then grows back
        self.assertTrue(settled(ui, lambda b: len(b) == 3), ui.screen_text())
        pts = self.points(ui)
        ui.send(sgr(PRESS, pts["vx"], pts["vy"]) + sgr(MOTION, pts["vx"] - 3, pts["vy"]))
        ui.resize(6, 20)
        ui.drain(0.5)
        ui.resize(self.ROWS, self.COLS)
        ui.drain(0.5)
        self.assertTrue(settled(ui, lambda b: len(b) == 3), ui.screen_text())
        self.assert_reports(ui, "after a too-small excursion mid-drag")
        # 3. a prefix command in the middle of a drag (layout key), the drag ends, reporting stays
        pts = self.points(ui)
        ui.send(sgr(PRESS, pts["vx"], pts["vy"]) + sgr(MOTION, pts["vx"] - 3, pts["vy"]) + P + b"=")
        ui.drain(0.4)
        self.assert_reports(ui, "after a prefix command mid-drag")
        # 4. a normal drag still works afterwards, and the wheel still scrolls
        self.assertTrue(settled(ui, lambda b: len(b) == 3), ui.screen_text())
        self.drag(ui, "v", -7)
        self.assert_reports(ui, "after the final drag")
        changes = [c for c in model_of(ui).changes_after(mark) if not c[2]]
        self.assertEqual(changes, [], "a mouse mode was reset while capture stayed on")
        self.wheel_scrolls(ui, "after every drag-ending path")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())
        ui.drain(0.3)
        self.assert_off(ui, "after detach")

    def test_window_resizes_keep_the_terminal_reporting_and_drags_working(self):
        ui = self.start_ui()
        for rows, cols in ((30, 100), (36, 120), (self.ROWS, self.COLS)):  # never larger than the replaying screen
            ui.resize(rows, cols)
            ui.drain(0.5)
            self.assert_reports(ui, f"after resize to {rows}x{cols}")
        # the replaying VT screen cannot follow window-size history, so the drag is judged by the resize frames the
        # backend receives (the drawn inner sizes) instead of by screen geometry
        ui.resize(self.ROWS, self.COLS)
        ui.drain(1.0)

        def manager():
            frames = [f for f in self.server.of("resize") if f.header["pane"] == "manager_omp"]
            return (frames[-1].header["rows"], frames[-1].header["cols"]) if frames else None

        self.assertTrue(self.server.wait(lambda: manager() is not None and manager()[1] > 20), manager())
        time.sleep(0.5)
        rows, cols = manager()
        x, y = cols + 2, 4 + rows // 2  # 1-based divider column and a row inside the top pane row
        ui.send(sgr(PRESS, x, y))
        for i in range(1, 10):
            ui.send(sgr(MOTION, x - i, y))
            time.sleep(0.01)
        ui.send(sgr(PRESS, x - 9, y, release=True))
        self.assertTrue(self.server.wait(lambda: manager() == (rows, cols - 9)),
                        f"the divider drag after window resizes did not resize the manager pane: {manager()} vs "
                        f"{(rows, cols - 9)}")
        self.assert_reports(ui, "after resizes and a drag")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())


class CaptureToggleTests(MuxBase):
    def test_prefix_m_off_and_on_turns_everything_off_then_fully_back_on_repeatedly(self):
        ui = self.start_ui()
        for cycle in range(3):
            ui.send(P + b"m")
            self.assertTrue(ui.wait_for(lambda: model_of(ui).mode is None, 5),
                            f"cycle {cycle}: capture off did not stop tracking")
            self.assert_off(ui, f"cycle {cycle} after prefix m (off)")
            self.assertFalse(model_of(ui).wheel and model_of(ui).clicks)
            ui.send(P + b"m")
            self.assertTrue(ui.wait_for(lambda: model_of(ui).clicks, 5),
                            f"cycle {cycle}: capture on did not restore reporting")
            self.assert_reports(ui, f"cycle {cycle} after prefix m (on)")
            self.assertEqual(model_of(ui).mode, "button", "after re-enable a divider drag needs motion tracking")
            self.drag(ui, "v", -4 if cycle % 2 == 0 else 4)
            self.assert_reports(ui, f"cycle {cycle} after a drag")
        self.wheel_scrolls(ui, "after 3 capture toggles")
        self.click_focuses(ui, "worker_omp", "after 3 capture toggles")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())
        ui.drain(0.3)
        self.assert_off(ui, "after detach")

    def test_capture_off_ignores_wheel_clicks_and_drags_and_switches_no_mode_on(self):
        ui = self.start_ui()
        ui.send(P + b"m")
        self.assertTrue(ui.wait_for(lambda: model_of(ui).mode is None, 5))
        mark = len(ui.output)
        pts = self.points(ui)
        before = pts["boxes"]
        focus_frames = len(self.server.of("focus"))
        ui.send(sgr(PRESS, pts["vx"], pts["vy"]) + sgr(MOTION, pts["vx"] + 9, pts["vy"]) +
                sgr(PRESS, pts["vx"] + 9, pts["vy"], release=True))
        ui.send(sgr(WHEEL_UP, pts["worker"][1], pts["worker"][0]) * 2)
        ui.send(sgr(PRESS, pts["worker"][1], pts["worker"][0]) + sgr(PRESS, pts["worker"][1], pts["worker"][0], True))
        ui.drain(0.6)
        self.assertEqual(drawn(ui), before, "a divider moved although capture was off")
        self.assertNotIn("[SCROLL", ui.screen_text())
        self.assertEqual(len(self.server.of("focus")), focus_frames)
        self.assertEqual([c for c in model_of(ui).changes_after(mark) if c[2]], [])
        self.assert_off(ui, "while capture is off")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())

    def test_every_exit_path_leaves_all_mouse_modes_off_after_drags(self):
        for action in ("detach", "sigterm", "sighup", "closing", "drop"):
            with self.subTest(exit=action):
                self.setUp()
                ui = self.start_ui()
                self.drag(ui, "v", -6)
                self.drag(ui, "h", 3)
                self.assert_reports(ui, "before the exit")
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
                else:
                    self.server.drop()
                self.assertTrue(ui.ui_done(15), ui.screen_text())
                ui.drain(0.4)
                self.assert_off(ui, f"after {action}")


class KeyboardFallbackTests(MuxBase):
    """Scrolling without a mouse: Shift+PgUp/PgDn, prefix PgUp and prefix [ (also while capture is off / after drags)."""

    def test_shift_pgup_pgdn_scroll_the_focused_pane_and_nothing_reaches_it(self):
        ui = self.start_ui()
        self.drag(ui, "v", -10)
        ui.send(SHIFT_PGUP)
        self.assertTrue(ui.wait_for(lambda: "MANAGER OMP [SCROLL" in ui.screen_text(), 5), ui.screen_text())
        ui.send(SHIFT_PGDN * 20)
        self.assertTrue(ui.wait_for(lambda: "[SCROLL" not in ui.screen_text(), 5), ui.screen_text())
        ui.drain(0.2)
        self.assertEqual(self.server.payloads("input"), b"", "scroll keys were typed into a pane")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())

    def test_fallbacks_work_with_mouse_capture_off(self):
        ui = self.start_ui()
        ui.send(P + b"m")
        self.assertTrue(ui.wait_for(lambda: model_of(ui).mode is None, 5))
        ui.send(SHIFT_PGUP)
        self.assertTrue(ui.wait_for(lambda: "[SCROLL" in ui.screen_text(), 5), ui.screen_text())
        ui.send(SHIFT_PGDN * 20)
        self.assertTrue(ui.wait_for(lambda: "[SCROLL" not in ui.screen_text(), 5), ui.screen_text())
        ui.send(P + PGUP)
        self.assertTrue(ui.wait_for(lambda: "[SCROLL" in ui.screen_text(), 5), ui.screen_text())
        ui.send(b"q")
        self.assertTrue(ui.wait_for(lambda: "[SCROLL" not in ui.screen_text(), 5), ui.screen_text())
        ui.send(P + b"[")
        self.assertTrue(ui.wait_for(lambda: "[SCROLL" in ui.screen_text(), 5), ui.screen_text())
        ui.send(b"q")
        self.assertTrue(ui.wait_for(lambda: "[SCROLL" not in ui.screen_text(), 5), ui.screen_text())
        ui.drain(0.2)
        self.assertEqual(self.server.payloads("input"), b"", "scroll keys were typed into a pane")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())

    def test_prefix_pgup_and_prefix_bracket_after_drags(self):
        ui = self.start_ui()
        self.drag(ui, "v", 8)
        self.drag(ui, "h", -2)
        for entry in (P + PGUP, P + b"["):
            ui.send(entry)
            self.assertTrue(ui.wait_for(lambda: "[SCROLL" in ui.screen_text(), 5), (entry, ui.screen_text()))
            ui.send(b"q")
            self.assertTrue(ui.wait_for(lambda: "[SCROLL" not in ui.screen_text(), 5), (entry, ui.screen_text()))
        self.assertEqual(self.server.payloads("input"), b"")
        ui.send(P + b"d")
        self.assertTrue(ui.ui_done(), ui.screen_text())


class IsolationWarningHeaderTests(unittest.TestCase):
    """R5 (UI part): a leak / failed / warning isolation check is visible in the header, ok / unchecked is not."""

    def header(self, isolation) -> str:
        snapshot = snap()
        if isolation is not None:
            snapshot["omp_isolation"] = isolation
        server = ScriptedServer(snapshot=snapshot)
        work = Path(tempfile.mkdtemp(prefix="cw06-p27n-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, work, True)
        self.addCleanup(server.close)
        ui = UiPty(server.path, work, rows=30, cols=140)
        self.addCleanup(ui.close)
        self.assertTrue(server.attached.wait(10), ui.screen_text())
        self.assertTrue(ui.wait_text("focus:", 10), ui.screen_text())
        ui.drain(0.4)
        head = "\n".join(ui.screen_text().split("\n")[:2])
        ui.send(P + b"d")
        ui.ui_done()
        return head

    def test_leak_failed_and_warning_states_are_shown(self):
        cases = {
            "leak": "ambient configuration loaded: manager:append_system",
            "failed": "OMP isolation check failed: timeout p27n",
            "warning": "OMP keeps its default personality and reads PERSONALITY.md: manager:personality:/x/PERSONALITY.md",
        }
        for state, text in cases.items():
            with self.subTest(state=state):
                head = self.header({"checked": True, "state": state, "ok": state == "warning", "warning": text,
                                    "leaks": [], "warnings": []})
                self.assertIn(text[:40], head)

    def test_ok_pending_and_absent_isolation_show_no_warning(self):
        for isolation in ({"checked": True, "state": "ok", "ok": True, "warning": None},
                          {"checked": False, "state": "pending", "ok": None, "warning": "SHOULD-NOT-SHOW-p27n"}, None):
            with self.subTest(isolation=isolation):
                head = self.header(isolation)
                self.assertNotIn("SHOULD-NOT-SHOW-p27n", head)
                self.assertNotIn("경고", head)


if __name__ == "__main__":
    unittest.main()
