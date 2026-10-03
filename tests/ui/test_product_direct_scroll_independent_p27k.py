"""Independent CW-06 UR-UX direct-scroll tests (p27-urux-scroll2-test-01).

Expectations come from the user follow-up "스크롤 모드로 들어가서 하기 보다는 바로 되면 좋겠는데" (answer "둘 다": mouse wheel AND
Shift+PgUp/PgDn), the Root-specified contract and the xterm mouse protocol - written before the implementation was read:

- Wheel over a pane scrolls that pane's view (about 3 lines per notch) and sends nothing to the backend; Shift+PgUp/PgDn
  (CSI 5;2~ / 6;2~) scroll the focused pane by a page. Neither is forwarded. The pane title keeps a SCROLL indicator.
- Typing or pasting into a scrolled pane first returns it to live and is then delivered exactly once, unchanged.
  Other panes keep their scroll position.
- Panes whose app enabled mouse tracking (DECSET 1000/1002/1003, SGR 1006 or legacy X10) get wheel/click translated to
  pane-local 1-based coordinates. Alt-screen panes without tracking: the wheel becomes Up/Down arrows (DECCKM aware).
  A left click focuses a pane without tracking. Borders/header/footer are ignored. Prefix m toggles the capture.
- Mouse sequences (also split across reads) never leak to a pane as text.
- The outer terminal gets ?1000h ?1006h at start; every exit path restores ?1000l ?1006l ?2004l and termios.
Real runtime: the product UI loop on an owned PTY (scripted ui_v1 fixture) and the real ``workbench attach``
entrypoint against a real backend with a stub OMP (zero model turns, no provider, no credentials).
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from independent_support_cw06 import SID, RecordingSender, ScriptedServer, UiPty, modes_restored, snap
from workbench.contracts import ui_v1
from workbench.contracts.v1 import DisplayChunk, PaneId
from workbench.ui.product.input import PARTIAL_HOLD_SECONDS, PREFIX
from workbench.ui.product.model import HELP_LINES, ProductModel, pane_boxes

P = bytes([PREFIX])
SHIFT_PGUP, SHIFT_PGDN = b"\x1b[5;2~", b"\x1b[6;2~"
PGUP = b"\x1b[5~"
NUM = re.compile(r"(?:manager|worker|host)-(\d{4})")
ROWS, COLS = 40, 150
ALL = tuple(PaneId)
TAG = {PaneId.MANAGER_OMP: "manager", PaneId.WORKER_OMP: "worker", PaneId.HOST_SHELL: "host"}


def sgr(button: int, x: int, y: int, release: bool = False) -> bytes:
    return f"\x1b[<{button};{x};{y}{'m' if release else 'M'}".encode()


def numbered(pane: PaneId, first: int, last: int) -> bytes:
    return b"".join(f"{TAG[pane]}-{i:04d}\r\n".encode() for i in range(first, last + 1))


def cell(pane: PaneId, col: int, row: int, rows: int = ROWS, cols: int = COLS) -> tuple[int, int]:
    """1-based outer-terminal (x, y) of the 1-based pane-local interior cell (col, row), from the documented layout."""
    top, left, _, _ = pane_boxes(rows, cols)[pane]
    return left + 1 + col, top + 1 + row


class M:
    """A pure ProductModel fed like the backend would, with helpers to read what a user would see."""

    def __init__(self, focus: str = "manager_omp", lines: int | None = 300, rows: int = ROWS, cols: int = COLS):
        self.sender = RecordingSender()
        self.model = ProductModel(self.sender, rows, cols, clock=lambda: 1000.0)
        self.model.attach_done(snap(focus=focus))
        self.model.after_attach()
        self.rows, self.cols = rows, cols
        self.seq, self.now = 0, 1.0
        if lines:
            for pane in ALL:
                self.feed(pane, numbered(pane, 1, lines))
        self.base = len(self.sender.sent)

    def feed(self, pane: PaneId, data: bytes, *, generation: int = 1, replay: bool = False) -> None:
        self.seq += 1
        raw = ui_v1.encode_display(DisplayChunk(session_id=SID, session_generation=generation, pane_id=pane,
                                                sequence=self.seq, data=data), replay=replay)
        self.model.on_display(ui_v1.FrameDecoder().feed(raw)[0])
        while self.model.has_backlog():
            self.model.feed_pending()

    def keys(self, data: bytes) -> None:
        self.now += 1
        self.model.handle_input(data, now=self.now)
        self.model.flush_input(now=self.now + PARTIAL_HOLD_SECONDS + 0.01)

    def wheel(self, pane: PaneId, up: bool = True, n: int = 1, col: int = 5, row: int = 3) -> None:
        x, y = cell(pane, col, row, self.rows, self.cols)
        self.keys(sgr(64 if up else 65, x, y) * n)

    def new(self) -> list[tuple]:
        return self.sender.sent[self.base:]

    def new_of(self, kind: str) -> list[tuple]:
        return [f for f in self.new() if f[0] == kind]

    def view(self, pane: PaneId) -> list[str]:
        rows, cols = self.model.sizes[pane]
        return ["".join(getattr(line.get(x), "data", " ") or " " for x in range(cols)).rstrip()
                for line in self.model.pane_lines(pane, rows)]

    def live(self, pane: PaneId) -> list[str]:
        return [ln.rstrip() for ln in self.model.panes[pane].screen.display]

    def nums(self, pane: PaneId) -> list[int]:
        return [int(m.group(1)) for ln in self.view(pane) if (m := NUM.fullmatch(ln))]

    def first(self, pane: PaneId) -> int:
        return self.nums(pane)[0]

    def title(self, pane: PaneId) -> str:
        return self.model.pane_title(pane)

    def is_live(self, pane: PaneId) -> bool:
        return self.view(pane) == self.live(pane) and "SCROLL" not in self.title(pane)

    def contiguous(self, test: unittest.TestCase, pane: PaneId) -> list[int]:
        nums = self.nums(pane)
        test.assertTrue(nums, f"nothing visible in {pane.value}: {self.view(pane)}")
        test.assertEqual(nums, list(range(nums[0], nums[0] + len(nums))), f"{pane.value} lines out of order/missing")
        return nums


class WheelTests(unittest.TestCase):
    def test_wheel_scrolls_only_the_pane_under_the_pointer_and_sends_nothing(self):
        for target in ALL:
            with self.subTest(target=target.value):
                m = M()
                live_first = {p: m.first(p) for p in ALL}
                m.wheel(target)
                moved = live_first[target] - m.first(target)
                self.assertIn(moved, range(1, 7), f"one notch should move about 3 lines, moved {moved}")
                m.contiguous(self, target)
                self.assertIn("SCROLL", m.title(target))
                for other in ALL:
                    if other is not target:
                        self.assertTrue(m.is_live(other), f"{other.value} moved when the wheel was over {target.value}")
                self.assertEqual(m.new(), [], "a wheel scroll sent something to the backend")
                self.assertIs(m.model.focus, PaneId.MANAGER_OMP, "the wheel changed focus")
                m.wheel(target, n=4)
                self.assertGreater(live_first[target] - m.first(target), moved, "more notches did not scroll further")
                m.wheel(target, up=False, n=10)  # back down (more than enough): exactly live again
                self.assertTrue(m.is_live(target), "scrolling back down did not end at the live screen")
                self.assertEqual(m.new(), [])

    def test_wheel_up_stops_at_the_oldest_line_and_down_at_live_is_a_noop(self):
        for pane in ALL:
            with self.subTest(pane=pane.value):
                m = M()
                m.wheel(pane, up=False, n=3)  # at live: nothing
                self.assertTrue(m.is_live(pane))
                m.wheel(pane, n=200)
                self.assertEqual(m.contiguous(self, pane)[0], 1, "the oldest retained line is not reachable")
                m.wheel(pane, n=5)
                self.assertEqual(m.contiguous(self, pane)[0], 1, "scrolled past the oldest line")
                self.assertEqual(m.new(), [])

    def test_wheel_over_a_pane_without_history_does_nothing(self):
        m = M(lines=None)
        for pane in ALL:
            m.feed(pane, b"only\r\nthree\r\nlines\r\n")
        m.base = len(m.sender.sent)
        for pane in ALL:
            m.wheel(pane, n=3)
            self.assertTrue(m.is_live(pane))
        self.assertEqual(m.new(), [])

    def test_output_arriving_while_scrolled_by_the_wheel_does_not_move_the_view(self):
        m = M()
        pane = PaneId.HOST_SHELL
        m.wheel(pane, n=8)
        before = m.view(pane)
        m.feed(pane, numbered(pane, 301, 380))
        self.assertEqual(m.view(pane), before, "viewed lines moved when output arrived")
        self.assertIn("SCROLL", m.title(pane))

    def test_wheel_on_borders_header_footer_and_outside_the_panes_is_ignored(self):
        boxes = pane_boxes(ROWS, COLS)
        spots = [(1, 1), (5, 2), (COLS, 1), (10, ROWS), (COLS, ROWS), (9999, 9999)]
        for pane, (top, left, height, width) in boxes.items():
            spots += [(left + 6, top + 1), (left + 6, top + height),  # top / bottom border (1-based)
                      (left + 1, top + 4), (left + width, top + 4)]  # left / right border
        for name, button, release in (("wheel_up", 64, False), ("wheel_down", 65, False), ("click", 0, False),
                                      ("release", 0, True)):
            m = M()
            m.wheel(PaneId.WORKER_OMP, n=2)  # a scrolled pane must not move either
            before = {p: m.view(p) for p in ALL}
            for x, y in spots:
                with self.subTest(event=name, at=(x, y)):
                    m.keys(sgr(button, x, y, release))
            self.assertEqual({p: m.view(p) for p in ALL}, before, f"{name} on a non-pane cell changed a view")
            self.assertEqual(m.new(), [], f"{name} on a non-pane cell reached the backend")
            self.assertIs(m.model.focus, PaneId.MANAGER_OMP)

    def test_wheel_in_scroll_mode_or_with_the_help_overlay_never_leaks(self):
        m = M()
        m.keys(P + b"[")
        m.wheel(PaneId.MANAGER_OMP, n=3)
        m.keys(b"q")
        m.keys(P + b"?")
        m.wheel(PaneId.HOST_SHELL, n=3)
        m.keys(b" ")
        self.assertEqual(m.new_of("input") + m.new_of("paste"), [])

    def test_the_footer_and_help_say_selection_needs_shift_drag(self):
        m = M()
        self.assertTrue(m.model.mouse_capture)
        self.assertIn("Shift", m.model.footer(), "footer does not mention Shift+drag while mouse capture is on")
        text = "\n".join(HELP_LINES)
        self.assertIn("Shift+드래그", text)
        self.assertIn("Shift+PgUp", text)


class ShiftPageTests(unittest.TestCase):
    def test_shift_pgup_pgdn_page_scrolls_only_the_focused_pane(self):
        for focus in ALL:
            with self.subTest(focus=focus.value):
                m = M(focus=focus.value)
                rows = m.model.sizes[focus][0]
                live_first = m.first(focus)
                m.keys(SHIFT_PGUP)
                page = live_first - m.first(focus)
                self.assertTrue(rows // 2 <= page <= rows, f"a page is about {rows} rows, moved {page}")
                m.contiguous(self, focus)
                self.assertIn("SCROLL", m.title(focus))
                for other in ALL:
                    if other is not focus:
                        self.assertTrue(m.is_live(other))
                m.keys(SHIFT_PGUP)
                self.assertEqual(live_first - m.first(focus), 2 * page, "the second page is not the same size")
                m.keys(SHIFT_PGDN * 2)
                self.assertTrue(m.is_live(focus), "two pages down after two pages up is not live")
                self.assertEqual(m.new(), [], "Shift+PgUp/PgDn reached the backend")

    def test_shift_pgup_stops_at_the_oldest_line_and_pgdn_at_live_is_a_noop(self):
        m = M(focus="host_shell")
        m.keys(SHIFT_PGDN * 3)
        self.assertTrue(m.is_live(PaneId.HOST_SHELL))
        m.keys(SHIFT_PGUP * 60)
        self.assertEqual(m.contiguous(self, PaneId.HOST_SHELL)[0], 1)
        self.assertEqual(m.new(), [])

    def test_plain_and_ctrl_pgup_are_still_original_keys_for_the_pane(self):
        for key in (PGUP, b"\x1b[6~", b"\x1b[5;5~", b"\x1b[6;3~"):
            with self.subTest(key=key):
                m = M(focus="worker_omp")
                m.keys(key)
                self.assertTrue(m.is_live(PaneId.WORKER_OMP), "a non-Shift page key scrolled the view")
                self.assertEqual(m.sender.payloads("input", "worker_omp"), key)
                self.assertEqual(len(m.new_of("input")), 1)

    def test_split_shift_pgup_never_leaks_at_any_boundary(self):
        for cut in range(1, len(SHIFT_PGUP)):
            with self.subTest(cut=cut):
                m = M(focus="host_shell")
                m.model.handle_input(SHIFT_PGUP[:cut], now=1.0)
                m.model.handle_input(SHIFT_PGUP[cut:], now=1.001)
                m.model.flush_input(now=5.0)
                self.assertEqual(m.new(), [], "part of Shift+PgUp reached the backend")
                self.assertIn("SCROLL", m.title(PaneId.HOST_SHELL))
        m = M(focus="host_shell")
        for i, byte in enumerate(SHIFT_PGUP):
            m.model.handle_input(bytes([byte]), now=1.0 + i * 0.001)
        m.model.flush_input(now=5.0)
        self.assertEqual(m.new(), [])
        self.assertIn("SCROLL", m.title(PaneId.HOST_SHELL))


class AutoReturnTests(unittest.TestCase):
    KEYS = (b"a", b"\r", b"\x03", b"\x1b[A", "한".encode(), PGUP, b"\x7f", b"/help\r")

    def scroll_everything(self, m: M, focus: PaneId) -> dict[PaneId, list[str]]:
        for i, pane in enumerate(ALL):
            m.wheel(pane, n=4 + 3 * i)
        return {p: m.view(p) for p in ALL if p is not focus}

    def test_typing_into_a_scrolled_pane_returns_to_live_and_is_delivered_once_unchanged(self):
        for focus in ALL:
            for key in self.KEYS:
                with self.subTest(pane=focus.value, key=key):
                    m = M(focus=focus.value)
                    others = self.scroll_everything(m, focus)
                    self.assertFalse(m.is_live(focus))
                    m.keys(key)
                    self.assertTrue(m.is_live(focus), "the typed key did not return the pane to live")
                    self.assertEqual([(f[1]["pane"], f[2]) for f in m.new_of("input")], [(focus.value, key)],
                                     "not delivered exactly once, unchanged")
                    self.assertEqual({p: m.view(p) for p in ALL if p is not focus}, others,
                                     "another pane lost its scroll position")

    def test_typing_delivery_is_one_frame_with_the_exact_bytes_including_following_keys(self):
        m = M(focus="host_shell")
        self.scroll_everything(m, PaneId.HOST_SHELL)
        m.keys(b"ls -l\r")
        self.assertEqual([(f[1]["pane"], f[2]) for f in m.new_of("input")], [("host_shell", b"ls -l\r")])
        m.keys(b"more")
        self.assertEqual(m.sender.payloads("input", "host_shell"), b"ls -l\rmore")

    def test_a_key_for_the_focused_pane_leaves_other_scrolled_panes_where_they_are(self):
        m = M(focus="host_shell")
        m.wheel(PaneId.MANAGER_OMP, n=6)
        m.wheel(PaneId.WORKER_OMP, n=9)
        before = {p: m.view(p) for p in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP)}
        m.keys(b"x")
        self.assertEqual({p: m.view(p) for p in before}, before)
        self.assertTrue(m.is_live(PaneId.HOST_SHELL))

    def test_paste_into_a_scrolled_pane_returns_to_live_and_is_delivered_exactly_once(self):
        for focus in ALL:
            with self.subTest(pane=focus.value):
                m = M(focus=focus.value)
                others = self.scroll_everything(m, focus)
                m.keys(b"\x1b[200~pasted text\x1b[201~")
                self.assertTrue(m.is_live(focus), "a paste did not return the pane to live")
                pastes = m.new_of("paste")
                self.assertEqual(len(pastes), 1)
                self.assertEqual(pastes[0][1]["pane"], focus.value)
                self.assertEqual(pastes[0][2].count(b"pasted text"), 1)
                self.assertEqual(m.new_of("input"), [], "the pasted body was also typed")
                self.assertEqual({p: m.view(p) for p in ALL if p is not focus}, others)

    def test_prefix_focus_and_click_keep_every_panes_scroll_position(self):
        m = M(focus="manager_omp")
        m.wheel(PaneId.MANAGER_OMP, n=5)
        m.wheel(PaneId.HOST_SHELL, n=7)
        before = {p: m.view(p) for p in ALL}
        m.keys(P + b"2")
        self.assertEqual({p: m.view(p) for p in ALL}, before, "prefix focus change moved a view")
        x, y = cell(PaneId.MANAGER_OMP, 3, 3)
        m.keys(sgr(0, x, y) + sgr(0, x, y, True))  # click focuses the scrolled manager pane
        self.assertIs(m.model.focus, PaneId.MANAGER_OMP)
        self.assertEqual({p: m.view(p) for p in ALL}, before, "a click moved a view")
        m.keys(b"z")  # typing now returns only the focused (manager) pane
        self.assertTrue(m.is_live(PaneId.MANAGER_OMP))
        self.assertEqual(m.view(PaneId.HOST_SHELL), before[PaneId.HOST_SHELL])


class AltScreenTests(unittest.TestCase):
    ENTER = {"1049": b"\x1b[?1049h", "1047": b"\x1b[?1047h", "47": b"\x1b[?47h"}

    def test_wheel_in_the_alternate_screen_becomes_arrow_keys(self):
        for name, enter in self.ENTER.items():
            for pane in ALL:
                with self.subTest(mode=name, pane=pane.value):
                    m = M()
                    m.feed(pane, enter + b"vim-like app")
                    m.base = len(m.sender.sent)
                    m.wheel(pane, up=True)
                    up = m.sender.payloads("input", pane.value)
                    self.assertRegex(up, rb"^(\x1b\[A)+$")
                    self.assertIn(up.count(b"\x1b[A"), range(1, 7), "about 3 arrows per notch")
                    m.wheel(pane, up=False)
                    down = m.sender.payloads("input", pane.value)[len(up):]
                    self.assertRegex(down, rb"^(\x1b\[B)+$")
                    self.assertEqual(m.sender.payloads("input", None), m.sender.payloads("input", pane.value))
                    self.assertNotIn("SCROLL", m.title(pane), "the alt screen has no history to scroll")
                    self.assertEqual(m.new_of("paste"), [])
                    for other in ALL:
                        if other is not pane:
                            self.assertTrue(m.is_live(other))

    def test_application_cursor_keys_mode_selects_the_ss3_arrows(self):
        m = M()
        pane = PaneId.HOST_SHELL
        m.feed(pane, b"\x1b[?1049h\x1b[?1h")
        m.base = len(m.sender.sent)
        m.wheel(pane, up=True)
        m.wheel(pane, up=False)
        payload = m.sender.payloads("input", "host_shell")
        self.assertRegex(payload, rb"^(\x1bOA)+(\x1bOB)+$")
        m.feed(pane, b"\x1b[?1l")  # DECCKM reset: back to CSI arrows
        m.base = len(m.sender.sent)
        m.wheel(pane, up=True)
        self.assertRegex(m.sender.payloads("input", "host_shell")[len(payload):], rb"^(\x1b\[A)+$")

    def test_decckm_alone_does_not_turn_the_wheel_into_arrows(self):
        m = M()
        m.feed(PaneId.HOST_SHELL, b"\x1b[?1h")
        m.base = len(m.sender.sent)
        m.wheel(PaneId.HOST_SHELL)
        self.assertIn("SCROLL", m.title(PaneId.HOST_SHELL))
        self.assertEqual(m.new(), [])

    def test_alternate_screen_with_app_mouse_tracking_forwards_the_wheel_not_arrows(self):
        m = M()
        pane = PaneId.WORKER_OMP
        m.feed(pane, b"\x1b[?1049h\x1b[?1000h\x1b[?1006h")
        m.base = len(m.sender.sent)
        x, y = cell(pane, 6, 2)
        m.keys(sgr(64, x, y))
        self.assertEqual(m.sender.payloads("input", "worker_omp"), b"\x1b[<64;6;2M")

    def test_leaving_the_alternate_screen_makes_the_wheel_scroll_history_again(self):
        m = M()
        pane = PaneId.HOST_SHELL
        m.feed(pane, b"\x1b[?1049hfull screen app\x1b[?1049l")
        m.base = len(m.sender.sent)
        m.wheel(pane, n=2)
        self.assertEqual(m.new(), [], "after leaving the alt screen the wheel must not send arrows")
        self.assertIn("SCROLL", m.title(pane))
        self.assertLess(m.contiguous(self, pane)[0], 300 - m.model.sizes[pane][0] + 1, "view is not behind live")


class TrackingTests(unittest.TestCase):
    def test_forwarded_translated_for_each_tracking_mode_with_sgr(self):
        for mode in (b"1000", b"1002", b"1003"):
            for pane in ALL:
                with self.subTest(mode=mode, pane=pane.value):
                    m = M()
                    m.feed(pane, b"\x1b[?" + mode + b"h\x1b[?1006h")
                    m.base = len(m.sender.sent)
                    for button, release, want in ((64, False, b"\x1b[<64;9;4M"), (65, False, b"\x1b[<65;9;4M"),
                                                  (0, False, b"\x1b[<0;9;4M"), (0, True, b"\x1b[<0;9;4m")):
                        before = len(m.sender.payloads("input", pane.value))
                        x, y = cell(pane, 9, 4)
                        m.keys(sgr(button, x, y, release))
                        self.assertEqual(m.sender.payloads("input", pane.value)[before:], want)
                    self.assertEqual(m.sender.payloads("input", None), m.sender.payloads("input", pane.value))
                    self.assertTrue(m.is_live(pane), "a tracked pane must not be scrolled by the UI")
                    self.assertIs(m.model.focus, PaneId.MANAGER_OMP, "a click on a tracked pane changed focus")
                    self.assertEqual(m.new_of("focus"), [])

    def test_legacy_x10_encoding_without_1006(self):
        for pane in ALL:
            with self.subTest(pane=pane.value):
                m = M()
                m.feed(pane, b"\x1b[?1000h")
                m.base = len(m.sender.sent)
                x, y = cell(pane, 12, 5)
                m.keys(sgr(64, x, y))
                m.keys(sgr(0, x, y))
                m.keys(sgr(0, x, y, True))
                head = b"\x1b[M"
                want = (head + bytes([32 + 64, 32 + 12, 32 + 5]) + head + bytes([32 + 0, 32 + 12, 32 + 5])
                        + head + bytes([32 + 3, 32 + 12, 32 + 5]))
                self.assertEqual(m.sender.payloads("input", pane.value), want)

    def test_tracking_is_per_pane_and_ends_when_the_app_disables_it(self):
        m = M()
        m.feed(PaneId.WORKER_OMP, b"\x1b[?1000;1006h")  # combined parameter list
        m.base = len(m.sender.sent)
        m.wheel(PaneId.MANAGER_OMP)  # manager did not ask: the view scrolls
        self.assertIn("SCROLL", m.title(PaneId.MANAGER_OMP))
        self.assertEqual(m.new(), [])
        m.wheel(PaneId.WORKER_OMP, col=5, row=3)
        self.assertEqual(m.sender.payloads("input", "worker_omp"), b"\x1b[<64;5;3M")
        m.feed(PaneId.WORKER_OMP, b"\x1b[?1000l")
        m.base = len(m.sender.sent)
        m.wheel(PaneId.WORKER_OMP)
        self.assertIn("SCROLL", m.title(PaneId.WORKER_OMP))
        self.assertEqual(m.new(), [])

    def test_the_enable_sequence_split_across_display_chunks_is_still_seen(self):
        for cut in range(1, len(b"\x1b[?1000h")):
            with self.subTest(cut=cut):
                m = M()
                seq = b"\x1b[?1000h\x1b[?1006h"
                m.feed(PaneId.HOST_SHELL, seq[:cut])
                m.feed(PaneId.HOST_SHELL, seq[cut:])
                m.base = len(m.sender.sent)
                m.wheel(PaneId.HOST_SHELL, col=4, row=2)
                self.assertEqual(m.sender.payloads("input", "host_shell"), b"\x1b[<64;4;2M")

    def test_tracking_seen_in_attach_replay_counts(self):
        m = M(lines=None)
        m.feed(PaneId.HOST_SHELL, numbered(PaneId.HOST_SHELL, 1, 100) + b"\x1b[?1000h\x1b[?1006h", replay=True)
        m.base = len(m.sender.sent)
        m.wheel(PaneId.HOST_SHELL, col=2, row=2)
        self.assertEqual(m.sender.payloads("input", "host_shell"), b"\x1b[<64;2;2M")


class ClickFocusTests(unittest.TestCase):
    def test_left_click_focuses_a_pane_without_tracking_like_prefix_1_2_3(self):
        for start in ALL:
            for target in ALL:
                if target is start:
                    continue
                with self.subTest(start=start.value, target=target.value):
                    m = M(focus=start.value)
                    x, y = cell(target, 8, 3)
                    m.keys(sgr(0, x, y))
                    m.keys(sgr(0, x, y, True))
                    self.assertIs(m.model.focus, target)
                    self.assertEqual([f[1]["pane"] for f in m.new_of("focus")], [target.value])
                    self.assertEqual(m.new_of("input") + m.new_of("paste"), [], "a click was typed into a pane")
                    for pane in ALL:
                        self.assertTrue(m.is_live(pane), "a click scrolled a pane")
                    m.keys(b"k")
                    self.assertEqual([(f[1]["pane"], f[2]) for f in m.new_of("input")], [(target.value, b"k")])

    def test_click_on_the_focused_pane_and_non_left_buttons_send_no_input(self):
        m = M(focus="host_shell")
        x, y = cell(PaneId.HOST_SHELL, 8, 3)
        for button in (0, 1, 2, 32, 34, 35):
            m.keys(sgr(button, x, y) + sgr(button, x, y, True))
        for target in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP):
            x, y = cell(target, 8, 3)
            for button in (1, 2, 32, 34, 35):
                m.keys(sgr(button, x, y) + sgr(button, x, y, True))
        self.assertEqual(m.new_of("input") + m.new_of("paste"), [])
        self.assertIs(m.model.focus, PaneId.HOST_SHELL)


class ToggleTests(unittest.TestCase):
    def test_prefix_m_toggles_mouse_capture_with_a_notice_and_sends_nothing(self):
        m = M()
        self.assertTrue(m.model.mouse_capture)
        m.keys(P + b"m")
        self.assertFalse(m.model.mouse_capture)
        off_notice = m.model.notice
        self.assertTrue(off_notice, "no notice when capture was switched off")
        m.keys(P + b"m")
        self.assertTrue(m.model.mouse_capture)
        self.assertTrue(m.model.notice)
        self.assertNotEqual(m.model.notice, off_notice)
        m.keys(P + b"M")  # upper case works like the other commands
        self.assertFalse(m.model.mouse_capture)
        self.assertEqual(m.new(), [])

    def test_a_late_mouse_report_while_capture_is_off_does_nothing_and_never_leaks(self):
        m = M()
        m.keys(P + b"m")
        x, y = cell(PaneId.WORKER_OMP, 4, 3)
        m.keys(sgr(64, x, y) * 3 + sgr(0, x, y) + sgr(0, x, y, True))
        self.assertEqual(m.new(), [], "a mouse report was acted on / leaked while capture is off")
        for pane in ALL:
            self.assertTrue(m.is_live(pane))
        self.assertIs(m.model.focus, PaneId.MANAGER_OMP)
        m.keys(P + b"m")
        m.wheel(PaneId.WORKER_OMP)
        self.assertIn("SCROLL", m.title(PaneId.WORKER_OMP), "wheel scrolling did not come back after re-enabling")

    def test_prefix_m_does_not_disturb_scroll_positions(self):
        m = M()
        m.wheel(PaneId.HOST_SHELL, n=5)
        before = m.view(PaneId.HOST_SHELL)
        m.keys(P + b"m")
        m.keys(P + b"m")
        self.assertEqual(m.view(PaneId.HOST_SHELL), before)


class LeakTests(unittest.TestCase):
    def test_a_wheel_report_split_at_every_boundary_never_leaks_and_scrolls_once(self):
        x, y = cell(PaneId.HOST_SHELL, 5, 3)
        report = sgr(64, x, y)
        ref = M()
        ref.model.handle_input(report, now=1.0)
        want = ref.view(PaneId.HOST_SHELL)
        self.assertIn("SCROLL", ref.title(PaneId.HOST_SHELL))
        for cut in range(1, len(report)):
            with self.subTest(cut=cut):
                m = M()
                m.model.handle_input(report[:cut], now=1.0)
                m.model.handle_input(report[cut:], now=1.0 + PARTIAL_HOLD_SECONDS / 4)
                m.model.flush_input(now=5.0)
                self.assertEqual(m.new(), [], "a fragment of the mouse report reached the backend")
                self.assertEqual(m.view(PaneId.HOST_SHELL), want, "split report scrolled differently than the whole")
        m = M()
        for i, byte in enumerate(report * 2):
            m.model.handle_input(bytes([byte]), now=1.0 + i * 0.001)
        m.model.flush_input(now=5.0)
        self.assertEqual(m.new(), [])
        self.assertLess(m.first(PaneId.HOST_SHELL), ref.first(PaneId.HOST_SHELL), "two reports scroll further than one")

    def test_mouse_reports_mixed_into_typing_never_add_or_drop_bytes(self):
        m = M(focus="host_shell")
        x, y = cell(PaneId.HOST_SHELL, 5, 3)
        stream = b"ab" + sgr(64, x, y) + b"cd" + SHIFT_PGUP + b"ef" + sgr(0, x, y) + sgr(0, x, y, True) + b"gh"
        for i, byte in enumerate(stream):
            m.model.handle_input(bytes([byte]), now=1.0 + i * 0.001)
        m.model.flush_input(now=9.0)
        self.assertEqual(m.sender.payloads("input", "host_shell"), b"abcdefgh")
        self.assertEqual(m.new_of("paste"), [])

    def test_a_burst_of_wheel_reports_in_one_read_is_handled(self):
        m = M()
        x, y = cell(PaneId.WORKER_OMP, 5, 3)
        m.keys(sgr(64, x, y) * 500)
        self.assertEqual(m.new(), [])
        self.assertEqual(m.contiguous(self, PaneId.WORKER_OMP)[0], 1)
        m.keys(sgr(65, x, y) * 500)
        self.assertTrue(m.is_live(PaneId.WORKER_OMP))

    def test_modified_horizontal_motion_and_odd_reports_never_leak(self):
        codes = (4, 8, 16, 20, 32, 35, 66, 67, 68, 72, 80, 96, 97, 128, 129)
        for pane in ALL:
            for code in codes:
                for release in (False, True):
                    with self.subTest(pane=pane.value, button=code, release=release):
                        m = M()
                        x, y = cell(pane, 6, 3)
                        m.keys(sgr(code, x, y, release))
                        m.keys(sgr(code, 12345, 54321, release))
                        # a modified/extra-button press may legitimately focus (a focus frame is not text)
                        self.assertEqual(m.new_of("input") + m.new_of("paste"), [], "a mouse report reached the pane as text")
                        self.assertEqual({f[0] for f in m.new()} - {"focus"}, set())

    def test_a_wheel_between_prefix_and_its_key_leaks_nothing(self):
        m = M()
        x, y = cell(PaneId.HOST_SHELL, 5, 3)
        m.keys(P + sgr(64, x, y) + b"2")
        for _, _, payload, _ in m.new():
            self.assertNotIn(b"<64", payload)
            self.assertNotIn(b"\x1b[<", payload)
        self.assertEqual(m.new_of("paste"), [])


class DeltaFixTests(unittest.TestCase):
    def test_f2_entering_scroll_mode_on_an_already_scrolled_pane_keeps_the_place(self):
        m = M()
        pane = PaneId.MANAGER_OMP
        live_first = m.first(pane)
        m.wheel(pane, n=6)
        before, before_first = m.view(pane), m.first(pane)
        m.keys(P + b"[")
        self.assertEqual(m.view(pane), before, "prefix [ reset the scroll anchor")
        m.keys(P + PGUP)  # prefix PgUp while already scrolled: one more page further back, not a reset to live+page
        page = m.model.sizes[pane][0]
        self.assertLessEqual(m.first(pane), before_first - page // 2, "prefix PgUp did not continue from the place")
        self.assertGreater(live_first - m.first(pane), live_first - before_first)

    def test_f3_history_wiped_by_a_replaced_session_returns_to_live_with_a_notice(self):
        m = M()
        pane = PaneId.HOST_SHELL
        m.wheel(pane, n=6)
        self.assertIn("SCROLL", m.title(pane))
        m.model.notice = ""
        m.feed(pane, b"fresh session\r\n", generation=2)
        self.assertTrue(m.is_live(pane), "a replaced session left a dangling scrolled view")
        self.assertTrue(m.model.notice, "no notice when the scrolled history disappeared")

    def test_f4_backend_focus_change_leaves_scroll_mode(self):
        m = M()
        m.keys(P + b"[")
        m.wheel(PaneId.MANAGER_OMP, n=2)
        m.model.on_state(snap(focus="worker_omp"))
        self.assertIs(m.model.focus, PaneId.WORKER_OMP)
        m.keys(b"x")
        self.assertEqual([(f[1]["pane"], f[2]) for f in m.new_of("input")], [("worker_omp", b"x")],
                         "after a backend focus change keys are still swallowed by scroll mode")


# --- real product UI loop on an owned PTY (scripted ui_v1 fixture) ------------------------------------------------
def boxes(screen_lines: list[str]) -> list[tuple[int, int, int, int]]:
    """(row0, col0, height, width) of manager, worker, host interiors from the UI's own ACS border rows."""
    top = next(i for i, ln in enumerate(screen_lines) if ln.startswith("lq"))
    mid = next(i for i, ln in enumerate(screen_lines) if i > top and ln.startswith("mq"))
    host_top = next(i for i, ln in enumerate(screen_lines) if i > mid and ln.startswith("lq"))
    host_bottom = next(i for i, ln in enumerate(screen_lines) if i > host_top and ln.startswith("mq"))
    out = [(top + 1, m.start() + 1, mid - top - 1, len(m.group(1))) for m in re.finditer(r"m(q+)j", screen_lines[mid])]
    out += [(host_top + 1, m.start() + 1, host_bottom - host_top - 1, len(m.group(1)))
            for m in re.finditer(r"m(q+)j", screen_lines[host_bottom])]
    return out


def contiguous(nums: list[int]) -> bool:
    return bool(nums) and nums == list(range(nums[0], nums[0] + len(nums)))


def mode_state(output: bytes, mode: str) -> tuple[int, int]:
    """(index of the last ``h``, index of the last ``l``) of DECSET ``mode`` in the outer terminal output."""
    return output.rfind(f"\x1b[?{mode}h".encode()), output.rfind(f"\x1b[?{mode}l".encode())


def proc_start(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


class PtyBase(unittest.TestCase):
    def setUp(self):
        self.server = ScriptedServer()
        self.work = Path(tempfile.mkdtemp(prefix="cw06-p27k-", dir=os.environ.get("TMPDIR", "/tmp")))
        self.ui = UiPty(self.server.path, self.work, rows=ROWS, cols=COLS)
        self.addCleanup(shutil.rmtree, self.work, True)
        self.addCleanup(self.server.close)
        self.addCleanup(self.ui.close)
        self.assertTrue(self.server.attached.wait(10), self.ui.screen_text())
        self.assertTrue(self.ui.wait_text("focus:", 10), self.ui.screen_text())
        self.ui.drain(0.3)

    def lines(self, index: int) -> list[str]:
        screen = self.ui.screen_text().split("\n")
        row0, col0, height, width = boxes(screen)[index]
        return [screen[row0 + r][col0:col0 + width].rstrip() for r in range(height)]

    def nums(self, index: int) -> list[int]:
        return [int(m.group(1)) for ln in self.lines(index) if (m := NUM.fullmatch(ln))]

    def title_line(self, index: int) -> str:
        screen = self.ui.screen_text().split("\n")
        row0, col0, _, width = boxes(screen)[index]
        return screen[row0 - 1][col0 - 1:col0 + width + 1]  # only this pane's part of the border row

    def at(self, index: int, col: int = 10, row: int = 3) -> tuple[int, int]:
        row0, col0, _, _ = boxes(self.ui.screen_text().split("\n"))[index]
        return col0 + col + 1, row0 + row + 1  # 1-based outer terminal cell of the 0-based interior cell (pane-local col+1,row+1)

    def fill(self, lines: int = 300) -> None:
        for index, pane in enumerate(ALL):
            self.server.display(pane.value, numbered(pane, 1, lines))
        self.assertTrue(self.ui.wait_for(lambda: all(lines in self.nums(i) for i in range(3)), 20), self.ui.screen_text())
        self.ui.drain(0.3)

    def wait_settled(self, index: int, predicate) -> bool:
        # a curses repaint can be caught half way: wait for a settled (contiguous) picture, then judge it
        return self.ui.wait_for(lambda: contiguous(self.nums(index)) and predicate(self.nums(index)), 10)

    def focus_host(self) -> None:
        self.ui.send(P + b"3")
        self.assertTrue(self.ui.wait_text("focus: HOST SHELL", 10), self.ui.screen_text())


class PtyDirectScrollTests(PtyBase):
    def test_mouse_reporting_is_enabled_at_start(self):
        out = bytes(self.ui.output)
        self.assertGreater(out.rfind(b"\x1b[?1000h"), out.rfind(b"\x1b[?1000l"))
        self.assertGreater(out.rfind(b"\x1b[?1006h"), out.rfind(b"\x1b[?1006l"))
        self.assertGreater(out.rfind(b"\x1b[?2004h"), out.rfind(b"\x1b[?2004l"))

    def test_wheel_over_each_pane_scrolls_only_that_pane_and_sends_nothing(self):
        self.fill()
        base_in, base_paste = len(self.server.of("input")), len(self.server.of("paste"))
        for index in range(3):
            with self.subTest(index=index):
                before = [self.lines(i) for i in range(3)]
                x, y = self.at(index)
                self.ui.send(sgr(64, x, y) * 5)
                self.assertTrue(self.wait_settled(index, lambda n, b=self.nums(index)[0]: n[0] < b), self.ui.screen_text())
                self.assertIn("SCROLL", self.title_line(index))
                for other in range(3):
                    if other != index:
                        self.assertEqual(self.lines(other), before[other], f"pane {other} changed")
                        self.assertNotIn("SCROLL", self.title_line(other))
                self.ui.send(sgr(65, x, y) * 20)
                self.assertTrue(self.ui.wait_for(lambda: self.lines(index) == before[index], 10), self.ui.screen_text())
                self.assertNotIn("SCROLL", self.title_line(index))
        self.assertEqual(len(self.server.of("input")), base_in, "a wheel reached the backend as input")
        self.assertEqual(len(self.server.of("paste")), base_paste)
        self.assertIsNone(self.ui.status())

    def test_shift_pgup_and_pgdn_page_scroll_the_focused_pane(self):
        self.fill()
        self.focus_host()
        base = len(self.server.of("input"))
        live, others = self.lines(2), [self.lines(0), self.lines(1)]
        self.ui.send(SHIFT_PGUP)
        self.assertTrue(self.wait_settled(2, lambda n: n[-1] < live_last(live)), self.ui.screen_text())
        self.assertIn("SCROLL", self.title_line(2))
        self.assertEqual([self.lines(0), self.lines(1)], others, "another pane scrolled")
        self.ui.send(SHIFT_PGDN)
        self.assertTrue(self.ui.wait_for(lambda: self.lines(2) == live, 10), self.ui.screen_text())
        self.assertEqual(len(self.server.of("input")), base, "Shift+PgUp/PgDn reached the backend")

    def test_typing_returns_the_scrolled_pane_to_live_and_is_delivered_once(self):
        self.fill()
        self.focus_host()
        live = self.lines(2)
        x, y = self.at(2)
        self.ui.send(sgr(64, x, y) * 6)
        self.assertTrue(self.wait_settled(2, lambda n: n[-1] < 300), self.ui.screen_text())
        base = len(self.server.of("input"))
        self.ui.send(b"x")
        self.assertTrue(self.ui.wait_for(lambda: self.lines(2) == live, 10), self.ui.screen_text())
        self.assertTrue(self.server.wait(lambda: len(self.server.of("input")) > base))
        self.ui.drain(0.3)
        got = self.server.of("input")[base:]
        self.assertEqual([(f.header["pane"], f.payload) for f in got], [("host_shell", b"x")])
        # paste after scrolling again: live again, one paste frame
        self.ui.send(sgr(64, x, y) * 6)
        self.assertTrue(self.wait_settled(2, lambda n: n[-1] < 300), self.ui.screen_text())
        self.ui.send(b"\x1b[200~pasted body\x1b[201~")
        self.assertTrue(self.ui.wait_for(lambda: self.lines(2) == live, 10), self.ui.screen_text())
        self.assertTrue(self.server.wait(lambda: len(self.server.of("paste")) == 1))
        self.assertEqual(self.server.of("paste")[0].payload.count(b"pasted body"), 1)
        self.assertEqual(len(self.server.of("input")), base + 1)

    def test_click_focuses_a_pane_and_wheel_over_others_keeps_focus(self):
        self.fill(60)
        x, y = self.at(1)
        self.ui.send(sgr(64, x, y))  # wheel: no focus change
        self.ui.drain(0.5)
        self.assertEqual(self.server.of("focus"), [])
        self.ui.send(sgr(0, x, y) + sgr(0, x, y, True))
        self.assertTrue(self.server.wait(lambda: any(f.header["pane"] == "worker_omp" for f in self.server.of("focus"))))
        self.assertTrue(self.ui.wait_text("focus: WORKER OMP", 10), self.ui.screen_text())
        x, y = self.at(2)
        self.ui.send(sgr(0, x, y) + sgr(0, x, y, True))
        self.assertTrue(self.server.wait(lambda: self.server.of("focus")[-1].header["pane"] == "host_shell"))
        self.assertTrue(self.ui.wait_text("focus: HOST SHELL", 10), self.ui.screen_text())
        self.assertEqual(self.server.of("input"), [], "a click was typed into a pane")

    def test_split_mouse_reports_never_leak_through_the_real_pty(self):
        self.fill(60)
        x, y = self.at(2)
        report = sgr(64, x, y) * 3 + sgr(0, x, y) + sgr(0, x, y, True)
        for i in range(0, len(report), 3):
            self.ui.send(report[i:i + 3])
            time.sleep(0.01)
        self.ui.drain(0.8)
        self.assertEqual(self.server.of("input"), [])
        self.assertEqual(self.server.of("paste"), [])
        self.assertNotIn("<64", self.ui.screen_text())

    def test_tracking_pane_gets_translated_reports_and_others_still_scroll(self):
        self.fill()
        self.server.display("worker_omp", b"\x1b[?1000h\x1b[?1006h")
        time.sleep(0.3)
        x, y = self.at(1, col=6, row=3)  # 0-based interior cell (6, 3) = pane-local 1-based (7, 4)
        self.ui.send(sgr(64, x, y))
        self.assertTrue(self.server.wait(lambda: self.server.payloads("input", "worker_omp") == b"\x1b[<64;7;4M"),
                        self.server.payloads("input"))
        self.assertNotIn("SCROLL", self.title_line(1))
        x, y = self.at(0)
        self.ui.send(sgr(64, x, y) * 3)
        self.assertTrue(self.ui.wait_for(lambda: "SCROLL" in self.title_line(0), 10), self.ui.screen_text())
        self.assertEqual(self.server.payloads("input", "manager_omp"), b"")

    def test_alt_screen_wheel_sends_arrow_keys(self):
        self.fill(60)
        self.server.display("host_shell", b"\x1b[?1049h\x1b[?1happ")
        time.sleep(0.3)
        x, y = self.at(2)
        self.ui.send(sgr(64, x, y) + sgr(65, x, y))
        self.assertTrue(self.server.wait(lambda: re.fullmatch(rb"(\x1bOA)+(\x1bOB)+", self.server.payloads("input", "host_shell"))
                                         is not None), self.server.payloads("input"))

    def test_prefix_m_toggles_terminal_mouse_reporting_and_stops_events(self):
        self.fill(60)
        mark = len(self.ui.output)
        self.ui.send(P + b"m")
        self.assertTrue(self.ui.wait_for(lambda: b"\x1b[?1000l" in bytes(self.ui.output[mark:])
                                         and b"\x1b[?1006l" in bytes(self.ui.output[mark:]), 10),
                        bytes(self.ui.output[mark:])[-300:])
        self.assertNotIn(b"\x1b[?1000h", bytes(self.ui.output[mark:]))
        x, y = self.at(2)
        self.ui.send(sgr(64, x, y) * 3)  # a late report: ignored
        self.ui.drain(0.6)
        self.assertNotIn("SCROLL", self.title_line(2))
        self.assertEqual(self.server.of("input"), [])
        mark = len(self.ui.output)
        self.ui.send(P + b"m")
        self.assertTrue(self.ui.wait_for(lambda: b"\x1b[?1000h" in bytes(self.ui.output[mark:])
                                         and b"\x1b[?1006h" in bytes(self.ui.output[mark:]), 10),
                        bytes(self.ui.output[mark:])[-300:])
        self.ui.send(sgr(64, x, y) * 3)
        self.assertTrue(self.ui.wait_for(lambda: "SCROLL" in self.title_line(2), 10), self.ui.screen_text())
        self.ui.send(P + b"m")  # leave capture off, then exit normally: still restored
        self.ui.send(P + b"q")
        self.assertTrue(self.ui.ui_done(15), self.ui.screen_text())
        self.assert_restored()

    # -- every exit path restores mouse reporting, bracketed paste and termios ---------------------------------
    def assert_restored(self):
        self.ui.drain(0.3)
        out = bytes(self.ui.output)
        for mode in ("1000", "1006", "2004"):
            on, off = mode_state(out, mode)
            self.assertGreaterEqual(on, 0, f"?{mode}h never enabled")
            self.assertGreater(off, on, f"?{mode}l missing after the last ?{mode}h")
        self.assertEqual(self.ui.after_file.read_text(), self.ui.before_file.read_text(), "outer termios not restored")
        self.assertTrue(all(modes_restored(out).values()), modes_restored(out))
        self.assertNotIn(b"Traceback", out)

    def signal_ui(self, signum: int) -> None:
        pid = self.ui.ui_pid()
        self.assertIsNotNone(pid)
        start = proc_start(pid)
        self.assertIn(b"workbench", Path(f"/proc/{pid}/cmdline").read_bytes() + b"workbench")
        fd = os.pidfd_open(pid)
        try:
            self.assertEqual(proc_start(pid), start)  # identity: pid + start time
            signal.pidfd_send_signal(fd, signum)
        finally:
            os.close(fd)

    def test_exit_normal_detach_restores(self):
        self.fill(60)
        self.ui.send(P + b"q")
        self.assertTrue(self.ui.ui_done(15), self.ui.screen_text())
        self.assertEqual(self.ui.status(), 0)
        self.assert_restored()

    def test_exit_sigterm_restores(self):
        self.fill(60)
        self.signal_ui(signal.SIGTERM)
        self.assertTrue(self.ui.ui_done(15), self.ui.screen_text())
        self.assertEqual(self.ui.status(), 1)
        self.assert_restored()

    def test_exit_sighup_restores(self):
        self.fill(60)
        self.signal_ui(signal.SIGHUP)
        self.assertTrue(self.ui.ui_done(15), self.ui.screen_text())
        self.assertEqual(self.ui.status(), 1)
        self.assert_restored()

    def test_exit_backend_closing_restores(self):
        self.fill(60)
        self.server.closing()
        self.assertTrue(self.ui.ui_done(15), self.ui.screen_text())
        self.assertEqual(self.ui.status(), 1)
        self.assert_restored()

    def test_exit_backend_connection_drop_restores(self):
        self.fill(60)
        self.server.drop()
        self.assertTrue(self.ui.ui_done(15), self.ui.screen_text())
        self.assertEqual(self.ui.status(), 1)
        self.assert_restored()

    def test_exit_while_scrolled_restores(self):
        self.fill(60)
        x, y = self.at(2)
        self.ui.send(sgr(64, x, y) * 3)
        self.assertTrue(self.ui.wait_for(lambda: "SCROLL" in self.title_line(2), 10), self.ui.screen_text())
        self.signal_ui(signal.SIGTERM)
        self.assertTrue(self.ui.ui_done(15), self.ui.screen_text())
        self.assert_restored()


def live_last(lines: list[str]) -> int:
    return max(int(m.group(1)) for ln in lines if (m := NUM.fullmatch(ln)))


# --- real entrypoint + real backend + stub OMP (zero model turns) -------------------------------------------------
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
SRC = str(Path(__file__).resolve().parents[2] / "src")
ENV = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
       "TERM": "xterm-256color"}
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"


def proc_identity(pid: int) -> tuple[int, str] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[1].split()
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


def find_key(node, key):
    if isinstance(node, dict):
        if key in node and not isinstance(node[key], (dict, list)):
            return node[key]
        for value in node.values():
            found = find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_key(value, key)
            if found is not None:
                return found
    return None


class RealEntrypointRuntime(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw06-p27k-rt-", dir=os.environ.get("TMPDIR", "/tmp")))
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
        self.leaked = [p for p, t in self.seen.items() if proc_identity(p) == (p, t)]
        for pid in self.leaked:  # exact identity (pid + starttime) only
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            try:
                if proc_identity(pid) == (pid, self.seen[pid]):
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)

    def attach_ui(self) -> UiPty:
        ui = UiPty.__new__(UiPty)
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        ui.rows, ui.cols = ROWS, COLS
        ui.process = subprocess.Popen(["/usr/bin/setsid", "--ctty", sys.executable, "-c", MAIN, "attach",
                                       "--data-dir", str(self.data)],
                                      stdin=slave, stdout=slave, stderr=slave, env=ENV, close_fds=True)
        os.close(slave)
        ui.fd, ui.output = master, bytearray()
        ui.before_file = ui.after_file = ui.status_file = self.root / "unused"
        self.addCleanup(ui.close)
        return ui

    def focus_now(self) -> str | None:
        result = self.cli("status", "--data-dir", str(self.data), "--json")
        if result.returncode != 0:
            return None
        try:
            return find_key(json.loads(result.stdout), "focus")
        except ValueError:
            return None

    def test_direct_scroll_in_the_real_entrypoint(self):
        started = self.cli("start", "--data-dir", str(self.data), "--omp", str(self.omp), "--no-attach", "--timeout", "3")
        self.assertIn("starting backend", started.stdout)
        ui = self.attach_ui()
        self.assertTrue(ui.wait_for(lambda: "STUB-OMP manager ready" in ui.screen_text()
                                    and "STUB-OMP worker ready" in ui.screen_text(), 30), ui.screen_text())
        self.seen.update(procs_naming(self.root))
        ui.drain(0.3)
        out = bytes(ui.output)
        self.assertGreater(out.rfind(b"\x1b[?1000h"), out.rfind(b"\x1b[?1000l"), "mouse tracking not enabled")
        self.assertGreater(out.rfind(b"\x1b[?1006h"), out.rfind(b"\x1b[?1006l"))
        geo = boxes(ui.screen_text().split("\n"))

        def host() -> list[str]:
            lines = ui.screen_text().split("\n")
            row0, col0, height, width = boxes(lines)[2]
            return [lines[row0 + r][col0:col0 + width].rstrip() for r in range(height)]

        def numbers() -> list[int]:
            return [int(ln) for ln in host() if re.fullmatch(r"\d{1,3}", ln)]

        def host_title() -> str:
            lines = ui.screen_text().split("\n")
            return lines[boxes(lines)[2][0] - 1]

        def cell_of(index: int, col: int = 10, row: int = 3) -> tuple[int, int]:
            row0, col0, _, _ = boxes(ui.screen_text().split("\n"))[index]
            return col0 + col + 1, row0 + row + 1

        # host shell: 400 lines, then wheel over the host pane
        ui.send(P + b"3")
        self.assertTrue(ui.wait_text("focus: HOST SHELL", 15), ui.screen_text())
        ui.send(b"seq 1 400\r")
        self.assertTrue(ui.wait_for(lambda: 400 in numbers(), 30), "\n".join(host()))
        live = host()
        x, y = cell_of(2)
        ui.send(sgr(64, x, y) * 10)
        self.assertTrue(ui.wait_for(lambda: contiguous(numbers()) and 400 not in numbers(), 15), "\n".join(host()))
        self.assertLess(max(numbers()), 400, "earlier lines are not visible")
        self.assertIn("SCROLL", host_title())
        # the wheel never went to bash: both OMP stubs and the shell saw nothing
        screen = ui.screen_text()
        self.assertIsNone(re.search(r"\[(manager|worker) got", screen), "a wheel reached a stub OMP")
        self.assertNotIn("<64", screen)
        # Shift+PgUp scrolls further back, Shift+PgDn towards live
        top = min(numbers())
        ui.send(b"\x1b[5;2~")
        self.assertTrue(ui.wait_for(lambda: contiguous(numbers()) and min(numbers()) < top, 15), "\n".join(host()))
        ui.send(b"\x1b[6;2~")
        self.assertTrue(ui.wait_for(lambda: contiguous(numbers()) and min(numbers()) >= top, 15), "\n".join(host()))
        # typing returns to live and reaches bash once
        ui.send(b"echo KEY-$((6*7))\r")
        self.assertTrue(ui.wait_for(lambda: "KEY-42" in host(), 20), "\n".join(host()))
        self.assertTrue(ui.wait_for(lambda: "SCROLL" not in host_title(), 10), host_title())
        self.assertEqual(host().count("KEY-42"), 1, "\n".join(host()))
        self.assertTrue(any(ln.startswith("echo KEY-$((6*7))") or "KEY-$((6*7))" in ln for ln in host()), "\n".join(host()))
        self.assertNotIn("<64", "\n".join(host()))
        self.assertNotIn("5;2~", "\n".join(host()))
        # wheel over the stub OMP panes (no history, no tracking): nothing reaches them
        for index in (0, 1):
            mx, my = cell_of(index)
            ui.send(sgr(64, mx, my) * 3 + sgr(65, mx, my) * 3)
        ui.drain(0.8)
        self.assertIsNone(re.search(r"\[(manager|worker) got", ui.screen_text()), "a wheel reached a stub OMP")
        # click: worker pane takes focus (status --json), then manager, then host
        self.assertEqual(self.focus_now(), "host_shell")
        wx, wy = cell_of(1)
        ui.send(sgr(0, wx, wy) + sgr(0, wx, wy, True))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.focus_now() != "worker_omp":
            ui.drain(0.2)
        self.assertEqual(self.focus_now(), "worker_omp", "a click on the worker pane did not focus it")
        self.assertTrue(ui.wait_text("focus: WORKER OMP", 10), ui.screen_text())
        ui.drain(0.5)
        self.assertIsNone(re.search(r"\[(manager|worker) got", ui.screen_text()), "a click was typed into a stub OMP")
        hx, hy = cell_of(2)
        ui.send(sgr(0, hx, hy) + sgr(0, hx, hy, True))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.focus_now() != "host_shell":
            ui.drain(0.2)
        self.assertEqual(self.focus_now(), "host_shell")
        # shell still alive and clean, then detach: mouse reporting restored
        ui.send(b"echo ALIVE-$((6*7))\r")
        self.assertTrue(ui.wait_for(lambda: "ALIVE-42" in host(), 15), "\n".join(host()))
        ui.send(P + b"q")
        self.assertTrue(ui.wait_for(lambda: b"detached; backend keeps running" in bytes(ui.output), 10))
        self.assertEqual(ui.process.wait(15), 0, "attach process did not exit cleanly after detach")
        ui.drain(0.2)
        out = bytes(ui.output)
        for mode in ("1000", "1006", "2004"):
            on, off = mode_state(out, mode)
            self.assertGreater(off, on, f"?{mode}l not left in the output after detach")
        self.assertEqual(len(geo), 3)
        self.assertEqual(self.cli("status", "--data-dir", str(self.data), "--json").returncode, 0)
        self.stop_backend()
        self.assertEqual(self.leaked, [], f"leaked processes: {self.leaked}")


if __name__ == "__main__":
    unittest.main()
