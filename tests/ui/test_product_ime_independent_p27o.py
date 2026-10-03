"""Independent CW-06 tests of the IME-neutral prefix commands (p27-ime-test-01).

Expectations were derived BEFORE the implementation was read, from the user requirement (a Korean IME on Mac/Windows,
also over SSH, must not break ``Ctrl-]`` + command; no 1:1 jamo mapping) and from byte-level facts about what such a
client actually sends:

- Ctrl combos (C0 bytes), digits, ASCII punctuation, Space, arrows (CSI/SS3), Tab/Enter/Esc pass through a Korean IME;
  ordinary letters become Hangul (UTF-8 syllables/jamo, often committed late or split across reads) or stay in preedit.
- So every prefix command must be reachable WITHOUT a letter key: a Ctrl alias (a C0 byte other than Backspace/Tab/LF/
  Enter/Esc, the prefix itself and Ctrl-c, which must stay "unknown") and the Ctrl-] Space menu (arrows/Enter/digits).
- Hangul right after the prefix is never guessed and never forwarded to a pane; a hint is shown; the prefix is spent.
- The menu forwards nothing to a pane; Esc / the prefix close it. English letters, resize repeat, direct scroll,
  mouse and paste keep working.

The tests deliberately do not read a key table: aliases and digits are DISCOVERED by comparing observable effects
(``effect``) of all C0 bytes / all menu digits with the effect of the plain letter, and the on-screen labels of the menu
are checked against what the keys really do.

Adapted by p27-jamo-test-01 for the user's later decisions (ur-ux follow_ups 2026-09-30 '자모 자동 변환 + Space' and
C-D61 (1)): detach is prefix q / Ctrl-q (prefix d / Ctrl-d only show a notice), a single 2-set compatibility jamo after
the prefix runs the command of its QWERTY key (syllables and longer runs are still hinted), and a raw Ctrl-d to an OMP
pane is delivered only after a second Ctrl-d within the confirmation window. Each adapted check keeps the original
invariant (nothing reaches a pane, splits/late bytes behave like one read, every other key is unchanged). Run:
``PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/ui -p test_product_ime_independent_p27o.py``
"""
from __future__ import annotations

import itertools
from pathlib import Path
import re
import shutil
import tempfile
import termios
import time
import unittest

from independent_support_cw06 import SID, RecordingSender, ScriptedServer, UiPty, modes_restored, snap
from workbench.contracts import ui_v1
from workbench.contracts.v1 import DisplayChunk, PaneId
from workbench.ui.product import model as model_module
from workbench.ui.product.input import PARTIAL_HOLD_SECONDS, PREFIX
from workbench.ui.product.model import HELP_LINES, ProductModel, pane_boxes

P = bytes([PREFIX])
HOLD = PARTIAL_HOLD_SECONDS + 0.001
LETTERS = "qtchrmz"  # every letter command of the documented prefix set (C-D61: detach d -> q)
FORBIDDEN_CTRL = {0x08: "Backspace/Ctrl-h", 0x09: "Tab/Ctrl-i", 0x0A: "LF/Ctrl-j", 0x0D: "Enter/Ctrl-m",
                  0x1B: "Esc/Ctrl-[", 0x03: "Ctrl-c (must stay unknown)", 0x1D: "the prefix itself"}
HANGUL = {"syllable": "한", "syllable_2": "글"}  # syllables: still hinted, never guessed
# single compatibility jamo: now read as the 2-set QWERTY key (user decision '자모 자동 변환 + Space')
JAMO = {"jamo_consonant": ("ㅇ", "d"), "jamo_vowel": ("ㅏ", "k"), "jamo_b": ("ㅠ", "b"), "jamo_q": ("ㅂ", "q"),
        "jamo_z": ("ㅋ", "z")}
UP, DOWN, ENTER, ESC = b"\x1b[A", b"\x1b[B", b"\r", b"\x1b"


def make(rows: int = 40, cols: int = 150):
    sender = RecordingSender()
    return ProductModel(sender, rows, cols, clock=lambda: 1000.0), sender


def feed(m, data: bytes, t: float = 0.0) -> float:
    """One read of ``data`` followed by the idle flush a real loop does when no more bytes arrive."""
    m.handle_input(data, now=t)
    m.flush_input(now=t + HOLD)
    return t + 1.0


def feed_parts(m, parts, t: float = 0.0, gap: float = 0.0) -> float:
    for part in parts:
        m.handle_input(part, now=t)
        t += gap
        if gap:
            m.flush_input(now=t)
    m.flush_input(now=t + 1.0)
    return t + 2.0


def effect(m, sender) -> tuple:
    """Everything a command can change that a user or the backend can observe, minus ids."""
    frames = tuple((kind, tuple(sorted(fields.items())), payload) for kind, fields, payload, _ in sender.sent)
    return (m.quit, m.mouse_capture, m.zoom, m.scroll_pane, m.help_open, m.layout, frames)


def perturbed():
    """A model whose divider was moved (so ``=`` has something to reset) and whose frames are not recorded."""
    m, s = make()
    feed(m, P + b"\x1b[D")
    s.sent.clear()
    return m, s


def effect_of(keys: bytes, *, setup=make, split: bool = False) -> tuple:
    m, s = setup()
    if split:
        feed_parts(m, [bytes([b]) for b in keys], gap=0.01)
    else:
        feed(m, keys)
    return effect(m, s)


def display(m, pane, data, seq=1):
    raw = ui_v1.encode_display(DisplayChunk(session_id=SID, session_generation=1, pane_id=pane, sequence=seq,
                                            data=data), replay=False)
    m.on_display(ui_v1.FrameDecoder().feed(raw)[0])


def cell(pane, col, row):
    top, left, _, _ = pane_boxes(40, 150)[pane]
    return left + 1 + col, top + 1 + row


def sgr(button, x, y, release=False):
    return f"\x1b[<{button};{x};{y}{'m' if release else 'M'}".encode()


def ctrl(letter: str) -> bytes:
    return bytes([ord(letter) - 96])


def discovered_aliases() -> dict[str, list[int]]:
    """For each letter command: the C0 bytes (after the prefix) whose observable effect equals the letter's."""
    found = {}
    for letter in LETTERS:
        want = effect_of(P + letter.encode())
        found[letter] = [b for b in range(1, 32) if effect_of(P + bytes([b])) == want]
    return found


class AliasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aliases = discovered_aliases()

    def test_every_letter_command_has_an_effect_and_a_distinct_ctrl_alias(self):
        idle = effect_of(b"")
        seen = {}
        for letter in LETTERS:
            self.assertNotEqual(effect_of(P + letter.encode()), idle, f"prefix {letter} does nothing")
            aliases = self.aliases[letter]
            self.assertTrue(aliases, f"no Ctrl alias runs the same command as prefix {letter!r}")
            for byte in aliases:
                self.assertNotIn(byte, seen, f"Ctrl byte {byte:#x} runs two different commands")
                seen[byte] = letter

    def test_ctrl_aliases_never_use_backspace_tab_lf_enter_esc_prefix_or_ctrl_c(self):
        for letter, aliases in self.aliases.items():
            for byte in aliases:
                self.assertNotIn(byte, FORBIDDEN_CTRL, f"{letter}: alias {byte:#x} = {FORBIDDEN_CTRL.get(byte)}")
        # ...and no such byte runs ANY letter command, whatever the alias map is
        for byte, why in FORBIDDEN_CTRL.items():
            for letter in LETTERS:
                self.assertNotEqual(effect_of(P + bytes([byte])), effect_of(P + letter.encode()),
                                    f"{why} after the prefix runs the {letter!r} command")

    def test_prefix_ctrl_c_is_unknown_sends_nothing_and_confirms_nothing(self):
        m, s = make()
        feed(m, P + b"\x03")
        self.assertEqual(s.sent, [])
        self.assertFalse(m.quit)
        self.assertIn("prefix", m.notice)
        self.assertFalse(m.parser.prefix_active)

    def test_held_ctrl_released_and_byte_by_byte_all_reach_the_same_command(self):
        for letter, aliases in self.aliases.items():
            want = effect_of(P + letter.encode())
            for byte in aliases:
                with self.subTest(letter=letter, alias=f"{byte:#04x}"):
                    self.assertEqual(effect_of(P + bytes([byte])), want, "held Ctrl (one read)")
                    m, s = make()
                    t = feed(m, P)
                    self.assertTrue(m.parser.prefix_active)
                    feed(m, bytes([byte]), t + 5.0)  # prefix released, alias pressed much later
                    self.assertEqual(effect(m, s), want, "released prefix")
                    self.assertEqual(effect_of(P + bytes([byte]), split=True), want, "byte-by-byte")

    def test_plain_english_letters_still_run_their_commands_in_both_cases(self):
        for letter in "qtchrm":
            want = effect_of(P + letter.encode())
            self.assertEqual(effect_of(P + letter.upper().encode()), want, f"prefix {letter.upper()}")
        m, s = make()
        feed(m, P + b"q")
        self.assertTrue(m.quit)
        for old in (b"d", b"D", b"\x04"):  # C-D61: the old detach keys no longer detach and send nothing
            m, s = make()
            feed(m, P + old)
            self.assertFalse(m.quit, old)
            self.assertEqual(s.sent, [], old)
            self.assertIn("q", m.notice, old)

    def test_aliases_work_from_every_focused_pane_and_never_leak_a_byte_to_a_pane(self):
        for pane_key in b"123":
            for letter, aliases in self.aliases.items():
                for byte in aliases:
                    with self.subTest(pane=chr(pane_key), letter=letter):
                        m, s = make()
                        t = feed(m, P + bytes([pane_key]))
                        s.sent.clear()
                        feed(m, P + bytes([byte]), t)
                        self.assertEqual(s.of("input") + s.of("paste"), [], "alias bytes reached a pane")

    def test_documented_ctrl_forms_are_the_real_ones(self):
        """A ``(... prefix Ctrl-x)`` hint on a help line must really run the command that line documents."""
        for letter in LETTERS:
            line = next((ln for ln in HELP_LINES if re.search(rf"prefix {letter}\b", ln)), "")
            if letter == "z":
                line = next(ln for ln in HELP_LINES if "prefix z" in ln)
            for char in re.findall(r"prefix Ctrl-([a-z])(?![a-z])", line) + re.findall(r"z\(Ctrl-([a-z])\)", line):
                self.assertEqual(effect_of(P + ctrl(char)), effect_of(P + letter.encode()),
                                 f"help says Ctrl-{char} runs prefix {letter}: {line.strip()}")
        text = "\n".join(HELP_LINES)
        m, _ = make()
        m.menu_open = True
        text += "\n" + "\n".join(m.menu_lines())
        for letter in LETTERS:  # every letter command's alias appears somewhere in the visible documentation
            self.assertTrue(any(f"Ctrl-{chr(b + 96)}" in text for b in self.aliases[letter]),
                            f"no Ctrl form of prefix {letter} is documented in help/menu")

    def test_help_and_footer_mention_the_menu_and_the_ctrl_forms(self):
        help_text = "\n".join(HELP_LINES)
        self.assertIn("Space", help_text)
        self.assertIn("Ctrl", help_text)
        m, _ = make()
        m.handle_input(P, now=0.0)
        self.assertIn("Space", m.footer())
        self.assertIn("Ctrl", m.footer())
        idle, _ = make()
        self.assertIn("Space", idle.footer())


class MenuTests(unittest.TestCase):
    NEEDED = ("q", "t", "c", "h", "r", "m", "z", "[", "?")

    @staticmethod
    def open_menu():
        m, s = make()
        feed(m, P + b" ")
        return m, s

    def menu_effect(self, keys: bytes, *, setup=make, split=False) -> tuple:
        m, s = setup()
        t = feed(m, P + b" ")
        self.assertTrue(m.menu_open, "prefix Space did not open the menu")
        if split:
            feed_parts(m, [bytes([b]) for b in keys], t, gap=0.01)
        else:
            feed(m, keys, t)
        return effect(m, s)

    def test_prefix_space_opens_a_menu_and_sends_nothing(self):
        m, s = self.open_menu()
        self.assertTrue(m.menu_open)
        self.assertEqual(s.sent, [])
        self.assertFalse(m.parser.prefix_active)
        lines = m.menu_lines()
        for digit in "1234567890":
            self.assertTrue(any(re.match(rf"^[> ] {digit}\s", ln) for ln in lines), f"digit {digit} not listed")
        self.assertIn("Space", m.footer() + " ".join(HELP_LINES))

    def test_space_without_the_prefix_is_ordinary_input(self):
        m, s = make()
        feed(m, b" ")
        self.assertFalse(m.menu_open)
        self.assertEqual(s.payloads("input"), b" ")

    def test_menu_digits_reach_every_documented_command(self):
        reached = {}
        for digit in "1234567890":
            reached[digit] = self.menu_effect(digit.encode())
        self.assertEqual(len({str(v) for v in reached.values()}), 10, "two digits run the same thing / one does nothing")
        for key in self.NEEDED:
            want = effect_of(P + key.encode())
            self.assertIn(want, reached.values(), f"no menu digit runs prefix {key!r}")
        want = effect_of(P + b"=", setup=perturbed)  # layout reset needs a moved divider to be observable
        got = [self.menu_effect(d.encode(), setup=perturbed) for d in "1234567890"]
        self.assertIn(want, got, "no menu digit runs the layout reset (prefix =)")

    def test_menu_labels_tell_the_truth(self):
        """Each digit's on-screen ``[Ctrl-] x / Ctrl-] Ctrl-y]`` keys must run exactly what the digit runs."""
        m, _ = self.open_menu()
        checked = 0
        for digit in "1234567890":
            line = next(ln for ln in m.menu_lines() if re.match(rf"^[> ] {digit}\s", ln))
            keys = re.findall(r"Ctrl-\] (Ctrl-[a-z]|\S)", re.search(r"\[(Ctrl-\] .*)\]\s*$", line).group(1))
            self.assertTrue(keys, f"digit {digit}: no key listed: {line!r}")
            for key in keys:
                raw = ctrl(key[-1]) if key.startswith("Ctrl-") else key.encode()
                self.assertEqual(effect_of(P + raw), self.menu_effect(digit.encode()),
                                 f"menu line {line!r}: key {key!r} does not do what digit {digit} does")
                checked += 1
        self.assertGreaterEqual(checked, 14)

    def test_arrows_and_enter_select_like_the_digits_including_wrap(self):
        for index, digit in enumerate("1234567890"):
            want = self.menu_effect(digit.encode())
            for name, down, enter in (("csi+CR", DOWN, b"\r"), ("ss3+LF", b"\x1bOB", b"\n")):
                with self.subTest(digit=digit, style=name):
                    self.assertEqual(self.menu_effect(down * index + enter), want)
        last = self.menu_effect(b"0")
        self.assertEqual(self.menu_effect(UP + ENTER), last, "Up from the first item wraps to the last")
        self.assertEqual(self.menu_effect(DOWN * 10 + ENTER), self.menu_effect(ENTER), "Down wraps around")
        self.assertEqual(self.menu_effect(b"\x1bOA" + ENTER), last)
        self.assertEqual(self.menu_effect(DOWN + DOWN + ENTER, split=True), self.menu_effect(b"3"))
        # arrow split across reads (ESC | [ | B) must be one arrow, not a close
        m, s = self.open_menu()
        feed_parts(m, [b"\x1b", b"[", b"B", b"\r"])
        self.assertEqual(effect(m, s), self.menu_effect(b"2"))

    def test_the_selection_marker_follows_the_arrows(self):
        m, _ = self.open_menu()
        self.assertTrue(m.menu_lines()[2].startswith(">"))
        feed(m, DOWN + DOWN, 5.0)
        marked = [ln for ln in m.menu_lines() if ln.startswith(">")]
        self.assertEqual(len(marked), 1)
        self.assertTrue(marked[0].startswith("> 3"), marked)

    def test_no_key_reaches_a_pane_or_runs_a_command_while_the_menu_is_open(self):
        junk = [b"abcdefghijklmnopqrstuvwxyzABC", "한글ㅇㅏㅠ안녕".encode(), b"\t", b"\x7f", b"\x08", b"\x03", b"\x1a",
                b"\x04", b"\x14", b"\x19", b"\x0f", b"\x12", b"\x05", b" ", b"[=?", b"\x1b[5~", b"\x1b[24~", b"\x1b[C",
                b"\x1b[D", b"\x1b[5;2~", b"\x1b[6;2~", b"\x1b[I", b"\x1bb", sgr(64, *cell(PaneId.WORKER_OMP, 3, 2)),
                sgr(0, *cell(PaneId.HOST_SHELL, 3, 2)), sgr(0, *cell(PaneId.HOST_SHELL, 3, 2), release=True),
                b"\x1b[200~pasted text\x1b[201~", b"\x1b[200~" + "붙여넣기 ㅇ".encode() + b"\x1b[201~"]
        for item in junk:
            with self.subTest(item=item[:20]):
                m, s = self.open_menu()
                display(m, PaneId.WORKER_OMP, b"\x1b[?1000h\x1b[?1006h")  # even a mouse-tracking pane gets nothing
                before, focus = effect(m, s), m.focus
                feed(m, item, 5.0)
                self.assertEqual(s.of("input") + s.of("paste"), [], "the menu forwarded a key to a pane")
                self.assertEqual(effect(m, s), before, "an ignored key had an observable effect")
                self.assertIs(m.focus, focus)
                self.assertFalse(m.quit)

    def test_hangul_in_the_menu_is_swallowed_even_when_split_byte_by_byte(self):
        m, s = self.open_menu()
        for byte in "한글ㅇ".encode():
            feed_parts(m, [bytes([byte])], 5.0 + byte, gap=0.2)
        self.assertEqual(s.sent, [])
        self.assertTrue(m.menu_open)
        feed(m, ESC, 500.0)
        self.assertFalse(m.menu_open)
        self.assertEqual(s.sent, [])

    def test_esc_closes_the_menu_and_keys_go_to_the_pane_again(self):
        for name, before in (("esc right away", b""), ("esc after navigation", DOWN + DOWN + UP)):
            with self.subTest(name):
                m, s = self.open_menu()
                t = feed(m, before, 5.0) if before else 5.0
                feed(m, ESC, t)
                self.assertFalse(m.menu_open, "Esc did not close the menu")
                self.assertEqual(s.sent, [])
                feed(m, b"d ", 9.0)  # plain d is text for the pane, not detach; Space is text, not a menu
                self.assertFalse(m.quit)
                self.assertFalse(m.menu_open)
                self.assertEqual(s.payloads("input"), b"d ")

    def test_the_prefix_key_alone_closes_the_menu_as_its_own_label_promises(self):
        """The menu says ``Esc / Ctrl-] 취소``: a lone Ctrl-] must not leave the menu on screen until another key."""
        m, s = self.open_menu()
        feed(m, P, 5.0)
        self.assertFalse(m.menu_open, "Ctrl-] alone left the menu open (it closes only when a further key arrives)")
        self.assertEqual(s.of("input") + s.of("paste"), [])

    def test_the_prefix_closes_the_menu_without_forwarding_anything(self):
        for name, keys in (("prefix prefix", P + P), ("prefix + Esc", P + ESC), ("prefix + hangul", P + "한".encode()),
                           ("prefix + Space", P + b" "), ("prefix + 1", P + b"1")):
            with self.subTest(name):
                m, s = self.open_menu()
                feed(m, keys, 5.0)
                self.assertEqual(s.of("input") + s.of("paste"), [], "closing the menu leaked bytes to a pane")
                if name in ("prefix prefix", "prefix + Esc"):
                    self.assertFalse(m.menu_open)
                self.assertFalse(m.parser.prefix_active)

    def test_after_the_menu_ran_a_command_it_is_closed_and_input_flows_to_the_pane(self):
        m, s = self.open_menu()
        feed(m, b"3", 5.0)  # a non-destructive item
        self.assertFalse(m.menu_open)
        self.assertEqual([k for k, *_ in s.sent], ["takeover_request"])
        s.sent.clear()
        feed(m, b"xyz", 9.0)
        self.assertEqual(s.payloads("input"), b"xyz")

    def test_menu_does_not_disturb_the_resize_repeat_window(self):
        m, s = make()
        feed(m, P + b"\x1b[D")
        widths = m.layout
        feed(m, P + b" ", 0.5)  # a menu opened inside the repeat window: arrows now navigate, they do not resize
        self.assertTrue(m.menu_open)
        feed(m, DOWN + UP + b"\x1b[D", 0.6)
        self.assertEqual(m.layout, widths)
        self.assertEqual(s.of("input"), [])

    def test_opening_the_menu_does_not_change_scroll_mouse_or_focus_state(self):
        m, s = make()
        m.mouse_capture = True
        feed(m, P + b" ", 0.0)
        feed(m, ESC, 1.0)
        self.assertTrue(m.mouse_capture)
        self.assertIsNone(m.scroll_pane)
        self.assertIs(m.focus, PaneId.MANAGER_OMP)

    # -- observations recorded for Root, not asserted as failures ---------------------------------------
    def test_observation_trailing_bytes_after_a_selection_in_the_same_read(self):
        m2, s2 = self.open_menu()
        feed(m2, b"3xyz", 5.0)  # digit and ordinary text arriving in one read
        print("OBSERVATION bytes_after_menu_selection_in_same_read_forwarded=", s2.payloads("input"))
        self.assertNotIn(b"3", s2.payloads("input"))


class HangulAfterPrefixTests(unittest.TestCase):
    def splits(self, data: bytes):
        """Every way to cut ``data`` into 1..3 reads."""
        yield [data]
        for i in range(1, len(data)):
            yield [data[:i], data[i:]]
        for i, j in itertools.combinations(range(1, len(data)), 2):
            yield [data[:i], data[i:j], data[j:]]

    def check_spent(self, m, s, label):
        self.assertEqual(s.of("input") + s.of("paste"), [], f"{label}: Hangul reached a pane")
        self.assertFalse(m.parser.prefix_active, f"{label}: prefix still pending")
        self.assertFalse(m.menu_open)
        self.assertIn("한글", m.notice, f"{label}: no Hangul hint")
        self.assertIn("Ctrl", m.notice)
        self.assertIn("Space", m.notice)
        self.assertFalse(m.quit)
        self.assertEqual([k for k, *_ in s.sent], [], f"{label}: a command ran")

    def test_single_hangul_syllable_after_the_prefix_is_hinted_never_forwarded_never_guessed(self):
        for name, text in HANGUL.items():
            data = text.encode()
            for gap in (0.0, 0.2):
                for parts in self.splits(data):
                    with self.subTest(key=name, parts=parts, gap=gap):
                        m, s = make()
                        feed_parts(m, [P] + parts, gap=gap)
                        self.check_spent(m, s, name)

    def test_single_jamo_after_the_prefix_runs_its_2set_key_never_forwarded_in_every_split(self):
        """Adapted: a lone jamo is now converted (was: hinted). Still never forwarded, split reads = one read."""
        for name, (jamo, key) in JAMO.items():
            want_m, want_s = make()
            feed(want_m, P + key.encode())
            want = (effect(want_m, want_s), want_m.notice)
            data = jamo.encode()
            for gap in (0.0, 0.2):
                for parts in self.splits(data):
                    with self.subTest(key=name, parts=parts, gap=gap):
                        m, s = make()
                        feed_parts(m, [P] + parts, gap=gap)
                        self.assertEqual((effect(m, s), m.notice), want, f"prefix {jamo} != prefix {key}")
                        self.assertEqual(s.of("input") + s.of("paste"), [], f"{name}: the jamo reached a pane")
                        self.assertFalse(m.parser.prefix_active)
                        self.assertFalse(m.menu_open)

    def test_alt_hangul_after_the_prefix_is_hinted_too(self):
        m, s = make()
        feed(m, P + b"\x1b" + "한".encode())
        self.check_spent(m, s, "alt+hangul")

    def test_state_is_clean_afterwards_english_and_commands_work_normally(self):
        for text in list(HANGUL.values()) + [jamo for jamo, _ in JAMO.values() if jamo != "ㅂ"]:
            m, s = make()
            t = feed(m, P + text.encode())
            self.assertFalse(m.parser.prefix_active)
            if text == "ㅏ":
                # Adapted for C-D63: prefix ㅏ is prefix k = the host terminal kill confirmation. The next plain key
                # only cancels it (never kills, never reaches a pane); after that the state is clean as for the others.
                self.assertTrue(m.kill_confirm_open)
                t = feed(m, b"d", t)
                self.assertFalse(m.kill_confirm_open)
                self.assertEqual((s.of("kill_pane"), s.payloads("input"), s.payloads("paste")), ([], b"", b""))
            feed(m, b"d", t)  # a plain d is now just text for the pane, not a command
            self.assertFalse(m.quit)
            self.assertEqual(s.payloads("input"), b"d")
            s.sent.clear()
            t = feed(m, P + b"2", t + 1)
            self.assertIs(m.focus, PaneId.WORKER_OMP)
            feed(m, P + ctrl("q"), t)  # the IME-neutral route right after the hint (C-D61: Ctrl-q)
            self.assertTrue(m.quit)

    def test_split_utf8_continuation_arriving_after_the_flush_is_still_swallowed(self):
        data = "한".encode()
        m, s = make()
        m.handle_input(P + data[:1], now=0.0)
        m.flush_input(now=1.0)  # a long pause between the lead byte and its continuation
        m.handle_input(data[1:], now=2.0)
        m.flush_input(now=3.0)
        self.check_spent(m, s, "late continuation")
        feed(m, b"ok", 5.0)
        self.assertEqual(s.payloads("input"), b"ok")

    def test_hangul_typed_without_the_prefix_reaches_the_pane_unchanged(self):
        text = "안녕하세요 ㅇㅏㅠ world 한글\r".encode()
        for parts in list(self.splits(text))[:60]:
            m, s = make()
            feed_parts(m, parts)
            self.assertEqual(s.payloads("input"), text, parts)
            self.assertEqual(m.notice, "")

    def test_hangul_then_prefix_command_in_the_same_read(self):
        m, s = make()
        feed(m, "안".encode() + P + ctrl("q"))
        self.assertEqual(s.payloads("input"), "안".encode())
        self.assertTrue(m.quit)

    def test_footer_shows_the_hint_after_the_hangul_key(self):
        m, _ = make()
        feed(m, P + "한".encode())
        self.assertIn("한글", m.footer())
        m, _ = make()  # adapted: ㅇ is now d, whose footer notice points at the new detach key
        feed(m, P + "ㅇ".encode())
        self.assertIn("q", m.footer())
        self.assertFalse(m.quit)

    def test_prefix_prefix_still_sends_the_literal_byte_and_hangul_after_it_is_plain_text(self):
        m, s = make()
        feed(m, P + P + "한".encode())
        self.assertEqual(s.payloads("input"), P + "한".encode())

    def test_observation_multi_syllable_commit_after_the_prefix(self):
        """A Korean IME may commit several syllables at once; report (not assert) what happens to the rest."""
        m, s = make()
        feed(m, P + "안녕".encode())
        forwarded = s.payloads("input")
        print("OBSERVATION prefix+안녕 forwarded_to_pane=", forwarded, "notice=", m.notice[:20])
        self.assertNotIn("안".encode(), forwarded, "the first Hangul key after the prefix reached the pane")
        self.assertIn("한글", m.notice)


class UnaffectedBehaviourTests(unittest.TestCase):
    def test_resize_repeat_after_a_prefix_arrow_still_works_and_alias_ends_it(self):
        m, s = make()
        base = m.layout
        t = feed(m, P + b"\x1b[D")
        first = m.layout
        self.assertNotEqual(first, base)
        m.handle_input(b"\x1b[D", now=0.3)  # bare arrow inside the repeat window
        self.assertNotEqual(m.layout, first)
        self.assertEqual(s.of("input"), [])
        second = m.layout
        m.handle_input(P + ctrl("z"), now=0.4)  # an alias ends the repeat window and zooms
        m.flush_input(now=0.5)
        self.assertIsNotNone(m.zoom)
        m.handle_input(b"\x1b[D", now=0.6)
        m.flush_input(now=0.7)
        self.assertEqual(m.layout, second)
        self.assertEqual(s.payloads("input"), b"\x1b[D")  # after the window, arrows are ordinary keys again

    def test_direct_scroll_wheel_and_shift_pgup_are_unchanged(self):
        m, s = make()
        display(m, PaneId.MANAGER_OMP, b"".join(b"line %03d\r\n" % i for i in range(200)))
        m.feed_pending()
        feed(m, b"\x1b[5;2~")
        self.assertTrue(m.scrolled(PaneId.MANAGER_OMP))
        self.assertEqual(s.of("input"), [])
        feed(m, b"\x1b[6;2~" * 20, 5.0)
        self.assertFalse(m.scrolled(PaneId.MANAGER_OMP))
        x, y = cell(PaneId.MANAGER_OMP, 4, 3)
        feed(m, sgr(64, x, y), 9.0)
        self.assertTrue(m.scrolled(PaneId.MANAGER_OMP))
        self.assertEqual(s.of("input"), [])

    def test_mouse_wheel_still_translates_for_a_tracking_pane(self):
        m, s = make()
        display(m, PaneId.WORKER_OMP, b"\x1b[?1000h\x1b[?1006h")
        x, y = cell(PaneId.WORKER_OMP, 7, 3)
        feed(m, sgr(64, x, y))
        self.assertEqual(s.payloads("input", "worker_omp"), b"\x1b[<64;7;3M")

    def test_bracketed_paste_after_the_prefix_and_alias_bytes_inside_a_paste(self):
        body = b"echo hi \x04\x14\x19\x0f\x12\x05\x1a " + P + b" " + "한글".encode()
        m, s = make()
        feed(m, P + b"\x1b[200~" + body + b"\x1b[201~")
        self.assertEqual([f for k, f, p, _ in s.sent if k == "paste"], [{"pane": "manager_omp"}])
        self.assertEqual(s.payloads("paste"), b"\x1b[200~" + body + b"\x1b[201~")
        self.assertFalse(m.quit)
        self.assertFalse(m.menu_open)
        self.assertEqual([k for k, *_ in s.sent], ["paste"], "a paste body ran a command")
        self.assertFalse(m.parser.prefix_active, "a paste must cancel the pending prefix")

    def test_mouse_between_the_prefix_and_an_alias_does_not_cancel_the_alias(self):
        m, s = make()
        x, y = cell(PaneId.WORKER_OMP, 3, 2)
        feed(m, P + sgr(64, x, y) + ctrl("q"))
        self.assertTrue(m.quit)

    def test_direct_esc_and_ctrl_bytes_reach_the_pane_without_the_prefix(self):
        keys = b"\x04\x14\x19\x0f\x12\x05\x1a\x11\x03\x08\x09\x0a\r\x1b[A "
        m, s = make()
        feed(m, P + b"3")  # host shell: every byte immediately, Ctrl-d included
        feed(m, keys, 2.0)
        self.assertEqual(s.payloads("input"), keys)
        self.assertFalse(m.quit)
        self.assertFalse(m.menu_open)
        # an OMP pane: identical bytes once Ctrl-d is confirmed by a second Ctrl-d (C-D61); nothing else changes
        m, s = make()
        feed(m, b"\x04" + keys)
        self.assertEqual(s.payloads("input"), keys)
        self.assertFalse(m.quit)


class PtyTests(unittest.TestCase):
    """The real product loop on an owned PTY: kernel line discipline included."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="wbp27o-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.work, True)

    def start(self, **kwargs):
        server = ScriptedServer(**kwargs)
        self.addCleanup(server.close)
        ui = UiPty(server.path, self.work)
        self.addCleanup(ui.close)
        self.assertTrue(server.attached.wait(15), bytes(ui.output[-2000:]))
        self.assertTrue(ui.wait_text("HOST SHELL", 10), ui.screen_text())
        return server, ui

    def finish(self, ui):
        self.assertTrue(ui.ui_done(15), bytes(ui.output[-3000:]))
        self.assertEqual(ui.status(), 0)
        self.assertEqual(ui.after_file.read_text(), ui.before_file.read_text(), "outer termios not restored")
        self.assertEqual(modes_restored(bytes(ui.output)), {"alt_screen": True, "bracketed_paste": True,
                                                            "cursor_visible": True})

    def test_the_tty_is_raw_so_no_alias_byte_is_a_signal_flow_control_or_line_edit_key(self):
        server, ui = self.start()
        flags = termios.tcgetattr(ui.fd)  # a Linux pty master shows the slave's line discipline
        self.assertFalse(flags[3] & (termios.ISIG | termios.ICANON | termios.IEXTEN), "cooked/signal/extended bits set")
        self.assertFalse(flags[0] & termios.IXON, "XON/XOFF would eat Ctrl-s/q")
        ui.send(P + ctrl("q"))
        self.finish(ui)

    def test_ctrl_prefix_ctrl_q_detaches_exit_0_and_restores_the_terminal(self):
        server, ui = self.start()
        ui.send(P + ctrl("q"))  # both bytes in one write: Ctrl held for both keys
        self.finish(ui)
        self.assertEqual(len(server.of("detach")), 1)
        self.assertEqual(server.of("input"), [])
        self.assertIn("backend keeps running", ui.screen_text())

    def test_released_prefix_then_ctrl_q_detaches(self):
        server, ui = self.start()
        ui.send(P)
        time.sleep(0.4)
        ui.send(ctrl("q"))
        self.finish(ui)
        self.assertEqual(len(server.of("detach")), 1)

    def test_ctrl_aliases_run_takeover_confirm_handoff_and_mouse_toggle_through_the_real_loop(self):
        server, ui = self.start(snapshot=snap(owner="user", mode="manual_prompt"))
        ui.send(P + ctrl("t"))
        self.assertTrue(server.wait(lambda: server.of("takeover_request")), "Ctrl-] Ctrl-t")
        ui.send(P + ctrl("y"))
        self.assertTrue(server.wait(lambda: server.of("takeover_confirm")), "Ctrl-] Ctrl-y")
        ui.send(P + ctrl("o"))
        self.assertTrue(server.wait(lambda: server.of("handoff")), "Ctrl-] Ctrl-o")
        ui.send(P + ctrl("e"))
        self.assertTrue(ui.wait_text("마우스 캡처 꺼짐", 5), ui.screen_text())
        n = len(server.of("resize"))
        ui.send(P + ctrl("z"))  # a real tty must deliver Ctrl-z as a byte, not SIGTSTP
        self.assertTrue(server.wait(lambda: len(server.of("resize")) > n), "Ctrl-] Ctrl-z zoom")
        self.assertIsNone(ui.status())
        ui.send(P + ctrl("r"))
        time.sleep(0.3)
        self.assertEqual(server.of("input"), [], "an alias reached a pane")
        ui.send(P + ctrl("q"))
        self.finish(ui)

    def test_menu_by_space_then_detach_digit_read_from_the_screen(self):
        server, ui = self.start()
        ui.send(P + b" ")
        self.assertTrue(ui.wait_text("명령 메뉴", 5), ui.screen_text())
        screen = ui.screen_text()
        row = next(ln for ln in screen.splitlines() if "detach" in ln and "Ctrl-]" in ln)
        digit = re.search(r"\b(\d)\s", row).group(1)
        ui.send(b"\x1b[B\x1b[A")  # arrows are harmless
        time.sleep(0.2)
        self.assertEqual(server.of("input"), [])
        ui.send(digit.encode())
        self.finish(ui)
        self.assertEqual(len(server.of("detach")), 1)

    def test_menu_esc_closes_and_arrow_enter_selects_through_the_real_loop(self):
        server, ui = self.start()
        ui.send(P + b" ")
        self.assertTrue(ui.wait_text("명령 메뉴", 5), ui.screen_text())
        ui.send("한글 abc".encode() + b"\t\x7f")
        ui.send(b"\x1b")
        self.assertTrue(ui.wait_for(lambda: "명령 메뉴" not in ui.screen_text(), 5), ui.screen_text())
        time.sleep(0.2)
        self.assertEqual(server.of("input"), [], "menu keys reached a pane")
        ui.send(b"hi")
        self.assertTrue(server.wait(lambda: server.payloads("input") == b"hi"), server.payloads("input"))
        ui.send(P + b" " + b"\x1b[B" * 9 + b"\x1b[A" * 0 + b"\r")  # last item is detach
        self.finish(ui)

    def test_hangul_after_the_prefix_shows_the_hint_and_reaches_no_pane(self):
        server, ui = self.start()
        for chunk in (P, "한".encode()[:1], "한".encode()[1:]):
            ui.send(chunk)
            time.sleep(0.15)
        self.assertTrue(ui.wait_text("한글 입력 상태", 5), ui.screen_text())
        time.sleep(0.2)
        self.assertEqual(server.of("input"), [])
        ui.send(b"d")
        self.assertTrue(server.wait(lambda: server.payloads("input") == b"d"), server.payloads("input"))
        self.assertIsNone(ui.status(), "a plain d after the hint must not detach")
        ui.send("안녕".encode())
        self.assertTrue(server.wait(lambda: server.payloads("input") == b"d" + "안녕".encode()))
        ui.send(P + ctrl("q"))
        self.finish(ui)


if __name__ == "__main__":
    unittest.main()
