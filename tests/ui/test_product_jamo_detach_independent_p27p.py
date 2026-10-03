"""Independent adversarial CW-06 tests: jamo conversion, detach q, Ctrl-d confirmation (p27-jamo-test-01).

Expectations were drafted from the criteria BEFORE the implementation was read:

- ur-ux follow_ups 2026-09-30 ('자모 자동 변환 + Space'): a Mac 2-set Korean IME holds a lone jamo in preedit and sends
  it only on commit (Space/Enter), possibly seconds after the prefix and split across reads. After ``Ctrl-]`` a single
  Hangul compatibility jamo is read as its 2-set (dubeolsik) QWERTY key and runs that command; the commit key right after
  it is swallowed exactly once. Syllables / multi-character runs keep the hint; Hangul without the prefix is unchanged.
  Scroll mode reads its letter keys the same way (ㅓ j, ㅏ k, ㅎ g, ㅂ q).
- DECISIONS C-D61 (1): the detach key is ``q`` (``Ctrl-q``, menu item 0 kept); the old ``d`` / ``Ctrl-d`` after the
  prefix detach no more and send nothing (notice). A raw Ctrl-d going to manager_omp / worker_omp is not delivered at
  once: a second Ctrl-d to the same pane within 2 s delivers exactly one; any other key, a focus change, a paste, a
  command or the timeout cancels; bytes around it in the same read are unaffected; host_shell gets it immediately and
  pastes are unchanged.

The 2-set table below is written from the criteria, not imported. The real-loop test runs the real backend with a stub
OMP that logs every byte it reads to a file (no provider, no credentials, no network) and exits on Ctrl-d like OMP.
Run: ``PYTHONPATH=src:tests/ui /tmp/cw02-g1-venv/bin/python -m unittest tests.ui.test_product_jamo_detach_independent_p27p``
"""
from __future__ import annotations

import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from independent_support_cw06 import SRC, RecordingSender, ScriptedServer, UiPty, modes_restored, snap  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import DisplayChunk, PaneId  # noqa: E402
from workbench.ui.product.input import PARTIAL_HOLD_SECONDS, PREFIX  # noqa: E402
from workbench.ui.product.model import HELP_LINES, ProductModel, pane_boxes  # noqa: E402

P = bytes([PREFIX])
HOLD = PARTIAL_HOLD_SECONDS + 0.001
CD = b"\x04"
WINDOW = 2.0  # C-D61 / worker-02 packet: confirmation window in seconds
# standard 2-set (dubeolsik) layout, from the criteria: compatibility jamo -> QWERTY key
TWO_SET = dict(zip("ㅂㅈㄷㄱㅅㅛㅕㅑㅐㅔㅁㄴㅇㄹㅎㅗㅓㅏㅣㅋㅌㅊㅍㅠㅜㅡ" "ㅃㅉㄸㄲㅆㅒㅖ",
                   "qwertyuiopasdfghjklzxcvbnm" "QWERTOP"))
# compatibility jamo that are NOT typed by one 2-set key (compound finals / compound vowels / archaic): hint only
NOT_ONE_KEY = "ㄳㄵㄶㄺㄻㄼㄽㄾㄿㅀㅄㅘㅙㅚㅝㅞㅟㅢㅥㅿㆆ"
COMMITS = {"space": b" ", "cr": b"\r", "lf": b"\n", "tab": b"\t"}
OMP = (PaneId.MANAGER_OMP, PaneId.WORKER_OMP)


def make(rows: int = 40, cols: int = 150):
    sender = RecordingSender()
    return ProductModel(sender, rows, cols, clock=lambda: 1000.0), sender


def feed(m, data: bytes, t: float = 0.0) -> float:
    m.handle_input(data, now=t)
    m.flush_input(now=t + HOLD)
    return t + 0.1


def effect(m, s) -> tuple:
    frames = tuple((kind, tuple(sorted(fields.items())), payload) for kind, fields, payload, _ in s.sent)
    return (m.quit, m.notice, m.menu_open, m.help_open, m.focus, m.mouse_capture, m.zoom, m.scroll_pane, m.layout,
            frames)


def effect_of(data: bytes) -> tuple:
    m, s = make()
    feed(m, data)
    return effect(m, s)


def display(m, pane, data, seq=1):
    raw = ui_v1.encode_display(DisplayChunk(session_id="00000000-0000-4000-8000-000000000001", session_generation=1,
                                            pane_id=pane, sequence=seq, data=data), replay=False)
    m.on_display(ui_v1.FrameDecoder().feed(raw)[0])
    while m.has_backlog():
        m.feed_pending()


def cell(pane, col=4, row=2):
    top, left, _, _ = pane_boxes(40, 150)[pane]
    return left + 1 + col, top + 1 + row


def sgr(button, x, y, release=False):
    return f"\x1b[<{button};{x};{y}{'m' if release else 'M'}".encode()


def pane_bytes(s, pane: PaneId) -> bytes:
    return s.payloads("input", pane.value)


def reach_panes(s) -> list:
    return s.of("input") + s.of("paste")


PANE_KEY = {PaneId.MANAGER_OMP: b"1", PaneId.WORKER_OMP: b"2", PaneId.HOST_SHELL: b"3"}


def focus(m, pane: PaneId, t: float = 0.0) -> float:
    return feed(m, P + PANE_KEY[pane], t)


# ======================================================================================================================
class JamoTableTests(unittest.TestCase):
    def test_table_under_test_is_the_33_key_2set_layout(self):
        self.assertEqual(len(TWO_SET), 33)
        self.assertEqual(len(set(TWO_SET.values())), 33)

    def test_every_mapped_jamo_plus_commit_equals_its_ascii_key_and_reaches_no_pane(self):
        for jamo, key in TWO_SET.items():
            want = effect_of(P + key.encode())
            for name, commit in COMMITS.items():
                with self.subTest(jamo=jamo, key=key, commit=name):
                    m, s = make()
                    feed(m, P + jamo.encode() + commit)
                    self.assertEqual(effect(m, s), want, f"prefix {jamo}{name} != prefix {key}")
                    self.assertEqual(reach_panes(s), [], "the jamo or its commit key reached a pane")
                    self.assertFalse(m.parser.prefix_active)

    def test_shifted_jamo_run_the_uppercase_key_not_the_lowercase_one(self):
        for jamo, key in (("ㅃ", "Q"), ("ㅉ", "W"), ("ㄸ", "E"), ("ㄲ", "R"), ("ㅆ", "T"), ("ㅒ", "O"), ("ㅖ", "P")):
            with self.subTest(jamo=jamo):
                self.assertEqual(effect_of(P + jamo.encode() + b" "), effect_of(P + key.encode()))
        self.assertTrue(effect_of(P + "ㅃ ".encode())[0], "ㅃ = Shift-q = Q must detach like prefix Q")

    def test_jamo_that_no_single_2set_key_types_only_hint(self):
        for jamo in NOT_ONE_KEY:
            with self.subTest(jamo=jamo):
                m, s = make()
                feed(m, P + jamo.encode() + b" ")
                self.assertEqual(s.sent, [], "a compound jamo ran a command or reached a pane")
                self.assertIn("한글", m.notice)
                self.assertFalse(m.quit)

    def test_syllables_and_multi_character_runs_keep_the_hint_even_if_they_start_with_a_command_jamo(self):
        for text in ("안", "한글", "ㅂㅂ", "ㅂ녕", "ㅇㅇ", "ㅋㅋㅋ", "ㅂㅏ", "ㅃ안"):
            for split in (False, True):
                with self.subTest(text=text, split=split):
                    m, s = make()
                    data = text.encode() + b" "
                    if split:  # the whole run in one read after the prefix, cut at a byte boundary inside a char
                        m.handle_input(P + data[:2], now=0.0)
                        m.handle_input(data[2:], now=0.01)
                        m.flush_input(now=0.2)
                    else:
                        feed(m, P + data)
                    self.assertEqual(s.sent, [], f"prefix {text!r} ran a command or reached a pane")
                    self.assertFalse(m.quit, f"prefix {text!r} detached")
                    self.assertIn("한글", m.notice)

    def test_hangul_without_the_prefix_reaches_the_pane_unchanged_including_every_jamo(self):
        text = ("".join(TWO_SET) + " " + NOT_ONE_KEY + " 안녕 ㅂ ㅇ\r").encode()
        for pane in PaneId:
            for split in (1, 2, 3, 7):
                with self.subTest(pane=pane.value, split=split):
                    m, s = make()
                    t = focus(m, pane)
                    s.sent.clear()
                    for i in range(0, len(text), split):
                        m.handle_input(text[i:i + split], now=t)
                        t += 0.01
                    m.flush_input(now=t + 1)
                    self.assertEqual(pane_bytes(s, pane), text)
                    self.assertFalse(m.quit)
                    self.assertEqual(m.notice, "")


class JamoTimingTests(unittest.TestCase):
    def test_jamo_arriving_seconds_after_the_prefix_still_runs(self):
        for delay in (0.6, 2.5, 5.0, 30.0):
            with self.subTest(delay=delay):
                m, s = make()
                m.handle_input(P, now=0.0)
                for t in (0.1, 0.5, 1.0, delay / 2):  # the idle loop flushes many times meanwhile
                    m.flush_input(now=t)
                self.assertTrue(m.parser.prefix_active, "the prefix timed out while the IME held the jamo")
                feed(m, "ㅋ ".encode(), delay)
                self.assertIsNotNone(m.zoom)
                self.assertEqual(reach_panes(s), [])

    def test_jamo_utf8_split_byte_by_byte_with_long_pauses(self):
        for jamo, key in (("ㅂ", "q"), ("ㅋ", "z"), ("ㅆ", "T"), ("ㅇ", "d")):
            with self.subTest(jamo=jamo):
                want = effect_of(P + key.encode())
                m, s = make()
                t = 0.0
                for part in [P] + [bytes([b]) for b in jamo.encode()] + [b" "]:
                    m.handle_input(part, now=t)
                    m.flush_input(now=t + 0.3)
                    t += 0.4 if part != b" " else 0.0
                m.flush_input(now=t + 5)
                self.assertEqual(effect(m, s), want)
                self.assertEqual(reach_panes(s), [])

    def test_commit_key_swallowed_exactly_once(self):
        for name, commit in COMMITS.items():
            for split in (False, True):
                with self.subTest(commit=name, split=split):
                    m, s = make()
                    if split:
                        m.handle_input(P + "ㅋ".encode(), now=0.0)
                        m.handle_input(commit, now=0.02)
                        m.handle_input(commit + b"x", now=0.04)
                        m.flush_input(now=1.0)
                    else:
                        feed(m, P + "ㅋ".encode() + commit + commit + b"x")
                    self.assertIsNotNone(m.zoom, "ㅋ did not zoom")
                    self.assertEqual(s.payloads("input"), commit + b"x", "the commit key was not swallowed exactly once")
                    self.assertFalse(m.menu_open, "the committing Space opened the menu")

    def test_non_commit_key_after_the_jamo_is_ordinary_input(self):
        m, s = make()
        feed(m, P + "ㅋ".encode() + b"ab")
        self.assertIsNotNone(m.zoom)
        self.assertEqual(s.payloads("input"), b"ab")
        m, s = make()
        feed(m, P + "ㅋ ".encode() + P + b"1")  # a new prefix right after the commit is a new prefix, not swallowed
        self.assertEqual([k for k, *_ in s.sent if k == "focus"], ["focus"])

    def test_committing_space_after_the_jamo_never_opens_the_menu(self):
        for jamo in TWO_SET:
            with self.subTest(jamo=jamo):
                m, _ = make()
                feed(m, P + jamo.encode() + b" ")
                self.assertFalse(m.menu_open)

    def test_jamo_while_the_menu_is_open_is_swallowed_and_runs_nothing(self):
        m, s = make()
        feed(m, P + b" ")
        self.assertTrue(m.menu_open)
        feed(m, "ㅂ ".encode(), 1.0)
        feed(m, P + "ㅂ ".encode(), 2.0)
        self.assertFalse(m.quit)
        self.assertEqual(reach_panes(s), [])


class JamoScrollModeTests(unittest.TestCase):
    def setUp(self):
        self.m, self.s = make()
        display(self.m, PaneId.MANAGER_OMP, b"".join(b"line %03d\r\n" % i for i in range(300)))
        feed(self.m, P + b"[", 1.0)
        self.assertEqual(self.m.scroll_pane, PaneId.MANAGER_OMP)
        feed(self.m, b"\x1b[5~", 2.0)  # one page back so j/k both move
        self.s.sent.clear()

    def test_scroll_letters_by_jamo_equal_the_ascii_letters(self):
        m = self.m
        for jamo, ascii_key, delta in (("ㅏ", b"k", 1), ("ㅓ", b"j", -1)):
            with self.subTest(jamo=jamo):
                before = m.scroll_offset()
                feed(m, jamo.encode() + b" ", 3.0)
                self.assertEqual(m.scroll_offset(), before + delta)
                feed(m, ascii_key, 4.0)
                self.assertEqual(m.scroll_offset(), before + 2 * delta)
        feed(m, "ㅎ ".encode(), 5.0)
        self.assertEqual(m.scroll_offset(), m.scroll_history(), "ㅎ (g) did not go to the top")
        self.assertEqual(reach_panes(self.s), [])

    def test_jamo_q_leaves_scroll_mode_and_its_space_never_reaches_the_pane(self):
        for jamo in ("ㅂ", "ㅃ"):
            with self.subTest(jamo=jamo):
                self.setUp()
                feed(self.m, jamo.encode() + b" ", 3.0)
                self.assertIsNone(self.m.scroll_pane, f"{jamo} Space did not leave scroll mode")
                self.assertFalse(self.m.quit, "scroll-mode q must not detach")
                self.assertEqual(reach_panes(self.s), [], "the commit Space leaked to the pane after leaving scroll")
                feed(self.m, "ㅂ".encode(), 4.0)  # outside scroll mode a jamo is text again
                self.assertEqual(self.s.payloads("input"), "ㅂ".encode())

    def test_prefix_jamo_q_in_scroll_mode_detaches(self):
        feed(self.m, P + "ㅂ ".encode(), 3.0)
        self.assertTrue(self.m.quit)
        self.assertEqual(reach_panes(self.s), [])

    def test_split_jamo_and_syllables_in_scroll_mode_reach_no_pane(self):
        m = self.m
        before = m.scroll_offset()
        data = "ㅏ".encode()
        m.handle_input(data[:1], now=3.0)
        m.flush_input(now=3.4)
        m.handle_input(data[1:] + b" ", now=3.5)
        m.flush_input(now=4.0)
        self.assertEqual(m.scroll_offset(), before + 1)
        feed(m, "안녕 ㅋ ".encode(), 5.0)
        self.assertEqual(reach_panes(self.s), [])
        self.assertEqual(m.scroll_pane, PaneId.MANAGER_OMP)


# ======================================================================================================================
class DetachKeyTests(unittest.TestCase):
    NEW = {"prefix q": P + b"q", "prefix Q": P + b"Q", "prefix Ctrl-q": P + b"\x11", "prefix ㅂ Space": P + "ㅂ ".encode(),
           "prefix ㅂ Enter": P + "ㅂ\r".encode(), "prefix ㅃ Space": P + "ㅃ ".encode(), "menu 0": P + b" 0",
           "menu Up Enter": P + b" \x1b[A\r", "menu Down x9 Enter": P + b" " + b"\x1b[B" * 9 + b"\r"}
    OLD = {"prefix d": P + b"d", "prefix D": P + b"D", "prefix Ctrl-d": P + CD, "prefix ㅇ Space": P + "ㅇ ".encode(),
           "prefix ㅇ Enter": P + "ㅇ\r".encode(), "prefix ㅇ alone": P + "ㅇ".encode()}

    def test_every_new_detach_form_quits_and_sends_nothing(self):
        for pane in PaneId:
            for name, keys in self.NEW.items():
                with self.subTest(pane=pane.value, form=name):
                    m, s = make()
                    t = focus(m, pane)
                    s.sent.clear()
                    feed(m, keys, t)
                    self.assertTrue(m.quit, f"{name} did not detach")
                    self.assertEqual(s.sent, [], f"{name} sent a frame (shutdown/input) before detaching")

    def test_old_detach_forms_do_not_detach_send_nothing_and_say_where_detach_went(self):
        for pane in PaneId:
            for name, keys in self.OLD.items():
                with self.subTest(pane=pane.value, form=name):
                    m, s = make()
                    t = focus(m, pane)
                    s.sent.clear()
                    feed(m, keys, t)
                    self.assertFalse(m.quit, f"{name} still detaches")
                    self.assertEqual(s.sent, [], f"{name} sent something")
                    self.assertIn("q", m.footer(), f"{name}: the footer does not point at the new detach key")
                    self.assertIn("detach", m.footer())
                    feed(m, b"x", t + 1)  # the prefix is spent; typing goes on normally
                    self.assertEqual(pane_bytes(s, pane), b"x")

    def test_bare_ctrl_q_is_an_ordinary_key(self):
        for pane in PaneId:
            with self.subTest(pane=pane.value):
                m, s = make()
                t = focus(m, pane)
                feed(m, b"\x11", t)
                self.assertFalse(m.quit)
                self.assertEqual(pane_bytes(s, pane), b"\x11")

    def test_menu_items_only_item_0_detaches(self):
        for digit in "123456789":
            with self.subTest(digit=digit):
                m, _ = make()
                feed(m, P + b" " + digit.encode())
                self.assertFalse(m.quit, f"menu {digit} detached")
        m, _ = make()
        feed(m, P + b" ")
        row = next(ln for ln in m.menu_lines() if "detach" in ln)
        self.assertRegex(row, r"^[> ] 0\s")
        self.assertIn("Ctrl-] q", row)
        self.assertIn("Ctrl-q", row)
        self.assertNotIn("Ctrl-] d", row)

    def test_scroll_mode_q_exits_scroll_prefix_q_detaches(self):
        m, s = make()
        display(m, PaneId.MANAGER_OMP, b"".join(b"l%03d\r\n" % i for i in range(200)))
        feed(m, P + b"[", 1.0)
        feed(m, b"q", 2.0)
        self.assertIsNone(m.scroll_pane)
        self.assertFalse(m.quit)
        feed(m, P + b"[", 3.0)
        feed(m, P + b"q", 4.0)
        self.assertTrue(m.quit)
        self.assertEqual(reach_panes(s), [])

    def test_visible_texts_name_q_as_detach_and_never_d(self):
        m, _ = make()
        texts = list(HELP_LINES) + [m.footer()]
        m.handle_input(P, now=0.0)
        texts.append(m.footer())
        m2, _ = make()
        feed(m2, P + b" ")
        texts += m2.menu_lines()
        joined = "\n".join(texts)
        self.assertRegex(joined, r"prefix q\s+detach")
        for line in texts:
            if "detach" in line and ("prefix d" in line or "Ctrl-] d" in line or "prefix Ctrl-d" in line):
                self.fail(f"a visible text still names d as detach: {line!r}")


# ======================================================================================================================
class CtrlDGuardTests(unittest.TestCase):
    def omp(self, pane=PaneId.MANAGER_OMP):
        m, s = make()
        t = focus(m, pane)
        s.sent.clear()
        return m, s, t + 1.0

    def test_single_ctrl_d_to_an_omp_pane_is_held_with_a_visible_notice(self):
        for pane in OMP:
            with self.subTest(pane=pane.value):
                m, s, t = self.omp(pane)
                feed(m, CD, t)
                self.assertEqual(reach_panes(s), [], "a single Ctrl-d reached an OMP pane")
                self.assertIn("Ctrl-d", m.footer())
                self.assertIn("2", m.footer())
                self.assertFalse(m.quit)

    def test_second_ctrl_d_within_the_window_delivers_exactly_one(self):
        for pane in OMP:
            for gap in (0.0, 0.05, 1.0, 1.9, WINDOW - 0.001):
                with self.subTest(pane=pane.value, gap=gap):
                    m, s, t = self.omp(pane)
                    if gap == 0.0:
                        feed(m, CD + CD, t)
                    else:
                        m.handle_input(CD, now=t)
                        m.flush_input(now=t + gap / 2)
                        m.handle_input(CD, now=t + gap)
                        m.flush_input(now=t + gap + HOLD)
                    self.assertEqual(pane_bytes(s, pane), CD)
                    self.assertEqual(s.payloads("input"), CD, "the Ctrl-d went to another pane")
                    self.assertNotIn("Ctrl-d는", m.footer(), "the confirmation notice stayed after delivery")

    def test_timeout_boundary(self):
        for flush_between in (False, True):
            with self.subTest(flush_between=flush_between):
                m, s, t = self.omp()
                m.handle_input(CD, now=t)
                if flush_between:
                    m.flush_input(now=t + WINDOW + 0.01)
                    self.assertNotIn("Ctrl-d는", m.footer(), "the notice outlived the window")
                m.handle_input(CD, now=t + WINDOW + 0.02)
                m.flush_input(now=t + WINDOW + 0.03)
                self.assertEqual(reach_panes(s), [], "a Ctrl-d after the window was delivered")
                # the late one is a new first press: a prompt second one is delivered
                feed(m, CD, t + WINDOW + 0.5)
                self.assertEqual(s.payloads("input"), CD)

    def test_three_and_four_in_one_read(self):
        for n, want in ((1, b""), (2, CD), (3, CD), (4, CD + CD), (5, CD + CD)):
            with self.subTest(n=n):
                m, s, t = self.omp()
                feed(m, CD * n, t)
                self.assertEqual(s.payloads("input"), want)

    def test_bytes_around_ctrl_d_in_the_same_read_are_unaffected(self):
        cases = {b"ab" + CD + b"cd": b"abcd", b"ab" + CD + CD + b"cd": b"ab" + CD + b"cd",
                 CD + b"\r": b"\r", b"x" + CD: b"x", "한".encode() + CD + "글".encode(): "한글".encode(),
                 b"\x1b[A" + CD + b"\x1b[B": b"\x1b[A\x1b[B", CD + b"a" + CD: b"a"}
        for typed, want in cases.items():
            with self.subTest(typed=typed):
                m, s, t = self.omp()
                feed(m, typed, t)
                self.assertEqual(s.payloads("input"), want)

    def test_alt_ctrl_d_is_one_key_never_split_into_a_lone_esc(self):
        """ESC 0x04 (Alt+Ctrl-d) is one key, not 'bytes around' a Ctrl-d. Held whole or delivered whole are both
        acceptable; a bare Esc reaching the OMP is not (Esc interrupts/cancels in OMP) and the key must stay reachable."""
        for presses in (1, 2):
            with self.subTest(presses=presses):
                m, s, t = self.omp()
                for i in range(presses):
                    feed(m, b"\x1b" + CD, t + i * 0.2)
                got = s.payloads("input")
                self.assertNotIn(got, (b"\x1b", b"\x1b\x1b"), "Alt+Ctrl-d reached the OMP as a lone Esc")
                if presses == 2:
                    self.assertIn(b"\x1b" + CD, got, "a confirmed Alt+Ctrl-d never reaches the OMP")

    def test_other_key_cancels(self):
        for other in (b"a", b"\r", b"\x03", b"\x1b[A", "한".encode(), b"\x11"):
            with self.subTest(other=other):
                m, s, t = self.omp()
                feed(m, CD, t)
                feed(m, other, t + 0.2)
                feed(m, CD, t + 0.4)
                self.assertEqual(s.payloads("input"), other, "the key in between did not cancel the held Ctrl-d")

    def cancelled_by(self, action) -> None:
        m, s, t = self.omp()
        feed(m, CD, t)
        action(m, t + 0.2)
        feed(m, CD, t + 0.6)
        self.assertNotIn(CD, s.payloads("input", "manager_omp"), "the held Ctrl-d survived")
        self.assertNotIn(CD, s.payloads("input"))

    def test_focus_change_by_key_mouse_and_backend_cancels(self):
        wx, wy = cell(PaneId.WORKER_OMP)
        mx, my = cell(PaneId.MANAGER_OMP)
        actions = {
            "prefix 2 then click back": lambda m, t: (feed(m, P + b"2", t), feed(m, sgr(0, mx, my), t + 0.1),
                                                      feed(m, sgr(0, mx, my, True), t + 0.15)),
            "click another pane and back": lambda m, t: [feed(m, x, t + i * 0.05) for i, x in enumerate(
                (sgr(0, wx, wy), sgr(0, wx, wy, True), sgr(0, mx, my), sgr(0, mx, my, True)))],
            # the focus request of setUp is answered first, so the model adopts backend-driven focus changes
            "backend focus change and back": lambda m, t: (m.pending.clear(), m.apply_snapshot(snap(focus="worker_omp")),
                                                           m.apply_snapshot(snap(focus="manager_omp"))),
        }
        for name, action in actions.items():
            with self.subTest(name):
                self.cancelled_by(action)

    def test_paste_menu_help_scroll_and_commands_cancel(self):
        actions = {
            "paste": lambda m, t: feed(m, b"\x1b[200~p\x1b[201~", t),
            "menu open + Esc": lambda m, t: (feed(m, P + b" ", t), feed(m, b"\x1b", t + 0.1)),
            "help open + key": lambda m, t: (feed(m, P + b"?", t), feed(m, b"x", t + 0.1)),
            "scroll mode + q": lambda m, t: (feed(m, P + b"[", t), feed(m, b"q", t + 0.1)),
            "Shift+PgUp": lambda m, t: feed(m, b"\x1b[5;2~", t),
            "prefix Ctrl-d (old detach)": lambda m, t: feed(m, P + CD, t),
            "prefix d": lambda m, t: feed(m, P + b"d", t),
            "prefix r": lambda m, t: feed(m, P + b"r", t),
            "prefix ㅋ Space twice": lambda m, t: (feed(m, P + "ㅋ ".encode(), t), feed(m, P + "ㅋ ".encode(), t + .1)),
        }
        for name, action in actions.items():
            with self.subTest(name):
                self.cancelled_by(action)

    def test_prefix_ctrl_d_never_confirms_a_held_ctrl_d_and_is_not_held_itself(self):
        m, s, t = self.omp()
        feed(m, CD, t)
        feed(m, P + CD, t + 0.1)  # the old detach habit right after a stray Ctrl-d
        self.assertEqual(s.sent, [])
        self.assertFalse(m.quit)
        feed(m, CD, t + 0.2)  # the prefix Ctrl-d was not a first press either
        self.assertEqual(s.sent, [])

    def test_held_on_one_omp_pane_is_not_confirmed_on_the_other(self):
        m, s, t = self.omp()
        feed(m, CD, t)
        wx, wy = cell(PaneId.WORKER_OMP)
        feed(m, sgr(0, wx, wy), t + 0.1)
        feed(m, sgr(0, wx, wy, True), t + 0.15)
        self.assertIs(m.focus, PaneId.WORKER_OMP)
        feed(m, CD, t + 0.2)
        self.assertEqual(s.payloads("input"), b"")
        feed(m, CD, t + 0.3)
        self.assertEqual(pane_bytes(s, PaneId.WORKER_OMP), CD)
        self.assertEqual(pane_bytes(s, PaneId.MANAGER_OMP), b"")

    def test_host_shell_gets_ctrl_d_immediately(self):
        m, s = make()
        t = focus(m, PaneId.HOST_SHELL)
        for typed in (CD, b"a" + CD + b"b", CD + CD):
            feed(m, typed, t)
            t += 0.5
        self.assertEqual(pane_bytes(s, PaneId.HOST_SHELL), CD + b"a" + CD + b"b" + CD + CD)
        self.assertNotIn("Ctrl-d는", m.footer())
        # a Ctrl-d held on the manager does not follow the focus to the host shell or delay it
        m, s, t = self.omp()
        feed(m, CD, t)
        feed(m, P + b"3", t + 0.1)
        feed(m, CD, t + 0.2)
        self.assertEqual(s.payloads("input"), CD)
        self.assertEqual(pane_bytes(s, PaneId.HOST_SHELL), CD)

    def test_ctrl_d_inside_a_paste_is_unchanged_and_arms_nothing(self):
        body = b"\x1b[200~a" + CD + b"b" + CD + CD + b"c\x1b[201~"
        for pane in PaneId:
            with self.subTest(pane=pane.value):
                m, s = make()
                t = focus(m, pane)
                s.sent.clear()
                feed(m, body, t)
                self.assertEqual(s.payloads("paste", pane.value), body)
                self.assertNotIn("Ctrl-d는", m.footer())
                feed(m, CD, t + 0.2)
                expected = CD if pane is PaneId.HOST_SHELL else b""
                self.assertEqual(s.payloads("input"), expected, "a Ctrl-d inside the paste counted as a first press")

    def test_mouse_wheel_and_same_pane_click_leave_the_hold_alone_and_never_leak(self):
        """Observed design (worker-02 'mouse report does not cancel'): a wheel / click on the same pane is not a key."""
        m, s, t = self.omp()
        display(m, PaneId.MANAGER_OMP, b"".join(b"w%03d\r\n" % i for i in range(200)))
        feed(m, CD, t)
        x, y = cell(PaneId.MANAGER_OMP)
        feed(m, sgr(64, x, y), t + 0.1)
        feed(m, CD, t + 0.2)
        self.assertEqual(s.payloads("input"), CD)  # wheel is not a key: the second press still confirms
        self.assertFalse(m.scrolled(PaneId.MANAGER_OMP), "typing Ctrl-d did not return the view to live")

    def test_resize_repeat_arrow_then_ctrl_d(self):
        m, s, t = self.omp()
        feed(m, P + b"\x1b[D", t)
        feed(m, CD, t + 0.2)
        self.assertEqual(s.payloads("input"), b"")
        feed(m, CD, t + 0.3)
        self.assertEqual(s.payloads("input"), CD)

    def test_notice_in_footer_under_zoom_and_direct_scroll(self):
        m, s, t = self.omp()
        feed(m, P + b"z", t)
        feed(m, CD, t + 0.1)
        self.assertIn("Ctrl-d", m.footer())
        m, s, t = self.omp()
        display(m, PaneId.MANAGER_OMP, b"".join(b"s%03d\r\n" % i for i in range(200)))
        feed(m, b"\x1b[5;2~", t)
        feed(m, CD, t + 0.1)
        self.assertIn("Ctrl-d", m.footer())

    def test_guard_never_loses_reorders_or_duplicates_other_keys(self):
        """Randomised: any mix of keys, Ctrl-d and read splits on an OMP pane equals a reference of the rule."""
        tokens = [b"a", b"Z", b"1", b" ", b"\r", b"\t", b"\x03", b"\x7f", CD, CD, CD, b"\x1b[A", "한".encode(),
                  "ㅂ".encode(), b"\x11"]

        def reference(stream: list[bytes]) -> bytes:
            out, held = bytearray(), False
            for tok in stream:
                if tok == CD:
                    if held:
                        out += CD
                    held = not held
                else:
                    held = False
                    out += tok
            return bytes(out)

        rng = random.Random(20260930)
        for case in range(400):
            stream = [rng.choice(tokens) for _ in range(rng.randint(1, 25))]
            data = b"".join(stream)
            cuts = sorted(rng.sample(range(1, len(data)), min(len(data) - 1, rng.randint(0, 6)))) if len(data) > 1 else []
            parts = [data[i:j] for i, j in zip([0] + cuts, cuts + [len(data)])]
            m, s, t = self.omp(rng.choice(OMP))
            for part in parts:
                m.handle_input(part, now=t)
                t += 0.01
            m.flush_input(now=t + 0.5)
            self.assertEqual(s.payloads("input"), reference(stream), f"case {case}: {stream!r} split {cuts}")
            self.assertEqual(s.of("paste"), [])
            self.assertFalse(m.quit)


# ======================================================================================================================
STUB = '''#!/usr/bin/env python3
import os, sys, tty
if "--version" in sys.argv:
    print("omp/18.2.10"); sys.exit(0)
role = os.environ.get("WORKBENCH_G3_ROLE", "?")
log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "read-" + role + ".bin"), "ab", buffering=0)
tty.setraw(0)
sys.stdout.write(f"STUB-OMP {role} ready\\r\\n> "); sys.stdout.flush()
while True:
    data = os.read(0, 4096)
    if not data:
        break
    log.write(data)
    sys.stdout.write(f"[{role} got {data!r}]\\r\\n> "); sys.stdout.flush()
    if b"\\x04" in data:  # like OMP: Ctrl-d ends the program
        sys.stdout.write(f"STUB-OMP {role} exit on ctrl-d\\r\\n"); sys.stdout.flush()
        sys.exit(0)
'''
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"


def _ticks(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


class RealLoopTests(unittest.TestCase):
    """The real product loop (``python -m workbench attach``) against the real backend running a stub OMP."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wbp27p-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "bin").mkdir()
        self.omp = self.root / "bin" / "omp"
        self.omp.write_text(STUB)
        self.omp.chmod(0o755)
        self.data = self.root / "d"
        self.env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1",
                    "LANG": "C.UTF-8", "TERM": "xterm-256color", "HOME": str(self.root)}
        # C-D64 (p27-home-test-01): start needs the user's OMP auth store; a fake regular file, never a credential
        (self.root / ".omp" / "agent").mkdir(parents=True)
        (self.root / ".omp" / "agent" / "agent.db").write_bytes(b"p27u fake auth store - not a credential\n")
        self.addCleanup(self.stop_backend)

    def cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, "-c", MAIN, *args], env=self.env, cwd=self.root, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def owned(self) -> dict[int, int]:
        found = {}
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit() and int(entry.name) != os.getpid():
                try:
                    cmdline = (entry / "cmdline").read_bytes()
                except OSError:
                    continue
                if str(self.root).encode() in cmdline:
                    ticks = _ticks(int(entry.name))
                    if ticks is not None:
                        found[int(entry.name)] = ticks
        return found

    def stop_backend(self):
        self.cli("shutdown", "--data-dir", str(self.data), "--yes")
        end = time.monotonic() + 10
        left = self.owned()
        while left and time.monotonic() < end:
            time.sleep(0.2)
            left = self.owned()
        for pid, ticks in left.items():  # exact identity (pid + start ticks) of processes naming our own root only
            if _ticks(pid) == ticks:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self.residue = sorted(left)

    def read_log(self, role: str) -> bytes:
        path = self.root / "bin" / f"read-{role}.bin"
        return path.read_bytes() if path.exists() else b""

    def test_first_ctrl_d_never_reaches_the_omp_second_does(self):
        started = self.cli("start", "--data-dir", str(self.data), "--omp", str(self.omp), "--no-attach", "--timeout", "3")
        self.assertIn("starting backend", started.stdout, started.stdout + started.stderr)
        ui = UiPty.__new__(UiPty)  # the UiPty helpers over a real `attach` process (not the scripted boot)
        ui.rows, ui.cols = 30, 120
        master, slave = os.openpty()
        import fcntl, struct, termios  # noqa: E401
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        ui.process = subprocess.Popen([sys.executable, "-c", MAIN, "attach", "--data-dir", str(self.data)],
                                      stdin=slave, stdout=slave, stderr=slave, env=self.env, cwd=self.root,
                                      start_new_session=True)
        os.close(slave)
        ui.fd, ui.output = master, bytearray()
        self.addCleanup(ui.close)
        self.assertTrue(ui.wait_for(lambda: "STUB-OMP manager ready" in ui.screen_text()
                                    and "STUB-OMP worker ready" in ui.screen_text(), 40), ui.screen_text())
        ui.send(b"a")
        self.assertTrue(ui.wait_for(lambda: self.read_log("manager") == b"a", 10), self.read_log("manager"))
        # 1. a single Ctrl-d: held, notice on screen, the OMP process never reads it
        ui.send(CD)
        self.assertTrue(ui.wait_text("Ctrl-d", 5), ui.screen_text())
        time.sleep(0.8)
        ui.drain(0)
        self.assertEqual(self.read_log("manager"), b"a", "the first Ctrl-d reached the OMP")
        # 2. another key cancels: the key arrives, the Ctrl-d does not
        ui.send(b"b")
        self.assertTrue(ui.wait_for(lambda: self.read_log("manager") == b"ab", 10), self.read_log("manager"))
        ui.send(CD)
        # 3. the window runs out: the late second Ctrl-d is only a new first press
        time.sleep(WINDOW + 0.6)
        ui.drain(0)
        self.assertNotIn("Ctrl-d는", ui.screen_text(), "the notice outlived the window")
        ui.send(CD)
        time.sleep(0.8)
        ui.drain(0)
        self.assertEqual(self.read_log("manager"), b"ab", "a Ctrl-d outside the window reached the OMP")
        self.assertEqual(self.read_log("worker"), b"", "bytes reached the worker OMP")
        # 4. the confirming press within the window: exactly one Ctrl-d, and the OMP exits on it
        ui.send(CD)
        self.assertTrue(ui.wait_for(lambda: self.read_log("manager") == b"ab" + CD, 10), self.read_log("manager"))
        self.assertTrue(ui.wait_text("exit on ctrl-d", 10), ui.screen_text())
        # 5. the UI is still attached; detach with q; the backend survives
        self.assertIsNone(ui.process.poll(), "the UI exited")
        ui.send(P + b"q")
        end = time.monotonic() + 15
        while ui.process.poll() is None and time.monotonic() < end:
            ui.drain(0.05)
        self.assertEqual(ui.process.returncode, 0)
        self.assertIn(b"backend keeps running", bytes(ui.output))
        self.assertEqual(self.cli("status", "--data-dir", str(self.data), "--json").returncode, 0)
        self.assertEqual(self.read_log("worker"), b"")
        self.assertEqual(modes_restored(bytes(ui.output)), {"alt_screen": True, "bracketed_paste": True,
                                                            "cursor_visible": True})


class ScriptedPtyTests(unittest.TestCase):
    """The real product loop on an owned PTY against the scripted ui_v1 server (host shell, jamo detach timing)."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="wbp27p-pty-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.work, True)

    def start(self, **kwargs):
        server = ScriptedServer(**kwargs)
        self.addCleanup(server.close)
        ui = UiPty(server.path, self.work)
        self.addCleanup(ui.close)
        self.assertTrue(server.attached.wait(15), bytes(ui.output[-2000:]))
        self.assertTrue(ui.wait_text("HOST SHELL", 10), ui.screen_text())
        return server, ui

    def finish(self, server, ui):
        self.assertTrue(ui.ui_done(15), bytes(ui.output[-3000:]))
        self.assertEqual(ui.status(), 0)
        self.assertEqual(len(server.of("detach")), 1)
        self.assertEqual(ui.after_file.read_text(), ui.before_file.read_text(), "outer termios not restored")

    def test_host_shell_ctrl_d_is_immediate_through_the_real_loop(self):
        server, ui = self.start()
        ui.send(P + b"3")
        self.assertTrue(server.wait(lambda: server.of("focus")))
        ui.send(CD)
        self.assertTrue(server.wait(lambda: server.payloads("input", "host_shell") == CD, 3), server.payloads("input"))
        ui.send(P + b"q")
        self.finish(server, ui)

    def test_ime_timing_prefix_then_jamo_seconds_later_split_then_space_detaches(self):
        server, ui = self.start()
        ui.send(P)
        time.sleep(1.5)  # the IME holds the jamo in preedit
        data = "ㅂ".encode()
        ui.send(data[:1])
        time.sleep(0.3)
        ui.send(data[1:] + b" ")
        self.finish(server, ui)
        self.assertEqual(server.of("input") + server.of("paste"), [])

    def test_old_prefix_ctrl_d_and_jamo_d_show_the_notice_and_keep_running(self):
        server, ui = self.start()
        ui.send(P + CD)
        self.assertTrue(ui.wait_text("detach는 이제", 5), ui.screen_text())
        ui.send(P + "ㅇ ".encode())
        time.sleep(0.5)
        self.assertIsNone(ui.status())
        self.assertEqual(server.of("input") + server.of("paste") + server.of("detach"), [])
        ui.send(P + b"\x11")
        self.finish(server, ui)


if __name__ == "__main__":
    unittest.main()
