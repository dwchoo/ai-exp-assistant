"""Independent CW-06 key-collision / prefix / Esc-timing tests (p27-cw06-test-01).

Expectations (derived from CW-06.md, BRIEF C-AC-14, before the implementation was read):
- Every original OMP/shell key reaches the focused pane byte-for-byte; the UI
  prefix is the only intercepted key, and prefix-prefix sends the literal byte.
- A lone Esc is delivered promptly (not held until the next key) and is not
  merged into a different key; Ctrl-C/Ctrl-Z/Ctrl-\\ are bytes, not UI signals.
- A UI command never leaks stray bytes into the pane.
"""
import unittest

from independent_support_cw06 import RecordingSender
from workbench.contracts.v1 import PaneId
from workbench.ui.product.input import PARTIAL_HOLD_SECONDS, PREFIX
from workbench.ui.product.model import ProductModel

OMP_KEYS = {
    "enter": b"\r", "ctrl_j_newline": b"\n", "tab": b"\t", "shift_tab": b"\x1b[Z", "backspace": b"\x7f",
    "ctrl_h": b"\x08", "ctrl_c": b"\x03", "ctrl_d": b"\x04", "ctrl_z": b"\x1a", "ctrl_backslash": b"\x1c",
    "ctrl_l": b"\x0c", "ctrl_o": b"\x0f", "ctrl_p": b"\x10", "ctrl_t": b"\x14", "ctrl_underscore": b"\x1f",
    "up": b"\x1b[A", "down": b"\x1b[B", "left_ss3": b"\x1bOD", "home": b"\x1b[H", "end": b"\x1b[F",
    "pgup": b"\x1b[5~", "delete": b"\x1b[3~", "f9": b"\x1b[20~", "f10": b"\x1b[21~", "f12": b"\x1b[24~",
    "alt_enter": b"\x1b\r", "alt_b": b"\x1bb", "alt_bracket": b"\x1b[", "ctrl_left": b"\x1b[1;5D",
    "shift_enter_csi_u": b"\x1b[13;2u", "kitty_esc": b"\x1b[27u", "mouse_sgr": b"\x1b[<0;10;5M",
    "focus_in": b"\x1b[I", "slash_help": b"/help\r", "korean": "안녕 세계".encode(),
    "esc_esc": b"\x1b\x1b",
}


def model(**kw):
    sender = RecordingSender()
    return ProductModel(sender, 40, 150, clock=lambda: 1000.0), sender


def feed_and_settle(m, data, t=0.0):
    m.handle_input(data, now=t)
    m.flush_input(now=t + PARTIAL_HOLD_SECONDS + 0.001)


class PassthroughTests(unittest.TestCase):
    def test_each_original_key_reaches_every_focused_pane_unchanged(self):
        for pane_key, pane in (("1", PaneId.MANAGER_OMP), ("2", PaneId.WORKER_OMP), ("3", PaneId.HOST_SHELL)):
            for name, key in OMP_KEYS.items():
                with self.subTest(pane=pane.value, key=name):
                    m, s = model()
                    feed_and_settle(m, bytes([PREFIX]) + pane_key.encode())
                    self.assertIs(m.focus, pane)
                    feed_and_settle(m, key, t=1.0)
                    self.assertEqual(s.payloads("input", pane.value), key)
                    self.assertEqual(s.payloads("input", None), key, "bytes went to another pane")
                    self.assertEqual(s.of("paste"), [])

    def test_keys_split_byte_by_byte_still_arrive_in_order(self):
        m, s = model()
        stream = b"".join(OMP_KEYS.values())
        for i, byte in enumerate(stream):
            m.handle_input(bytes([byte]), now=i * 0.001)
        m.flush_input(now=10.0)
        self.assertEqual(s.payloads("input"), stream)

    def test_prefix_prefix_sends_one_literal_prefix_byte_and_stays_out_of_prefix_mode(self):
        m, s = model()
        feed_and_settle(m, bytes([PREFIX, PREFIX]) + b"x")
        self.assertEqual(s.payloads("input"), bytes([PREFIX]) + b"x")
        self.assertFalse(m.parser.prefix_active)

    def test_prefix_split_across_reads(self):
        m, s = model()
        m.handle_input(bytes([PREFIX]), now=0)
        self.assertTrue(m.parser.prefix_active)
        m.flush_input(now=5.0)  # a long pause does not cancel or leak the prefix
        self.assertEqual(s.of("input"), [])
        m.handle_input(b"2", now=6.0)
        self.assertIs(m.focus, PaneId.WORKER_OMP)
        self.assertEqual(s.of("input"), [])

    def test_unknown_prefix_command_sends_nothing_to_the_pane(self):
        for key in (b"x", b"Z", b"\x03", b"\r"):
            with self.subTest(key=key):
                m, s = model()
                feed_and_settle(m, bytes([PREFIX]) + key)
                self.assertEqual(s.of("input"), [])
                self.assertIn("prefix", m.notice)

    def test_prefix_then_non_ascii_key_leaks_no_utf8_continuation_bytes(self):
        m, s = model()
        feed_and_settle(m, bytes([PREFIX]) + "한".encode())
        self.assertEqual(s.payloads("input"), b"", "prefix + multibyte key sent partial UTF-8 to the pane")

    def test_prefix_then_paste_keeps_the_paste_framed(self):
        # A paste that arrives right after the prefix must not be typed into the pane as raw bytes.
        m, s = model()
        body = "line1\nline2\n".encode()
        feed_and_settle(m, bytes([PREFIX]) + b"\x1b[200~" + body + b"\x1b[201~")
        leaked = s.payloads("input")
        self.assertNotIn(body, leaked, "pasted text was delivered as typed input (no paste frame)")


class EscTimingTests(unittest.TestCase):
    def test_lone_esc_is_held_at_most_the_partial_hold_then_sent(self):
        m, s = model()
        m.handle_input(b"\x1b", now=0.0)
        m.flush_input(now=PARTIAL_HOLD_SECONDS / 2)
        self.assertEqual(s.payloads("input"), b"")
        m.flush_input(now=PARTIAL_HOLD_SECONDS + 0.001)
        self.assertEqual(s.payloads("input"), b"\x1b")
        self.assertLessEqual(PARTIAL_HOLD_SECONDS, 0.1, "Esc hold longer than 100 ms is user-visible")

    def test_esc_then_later_key_are_two_separate_keys(self):
        m, s = model()
        m.handle_input(b"\x1b", now=0.0)
        m.flush_input(now=0.2)
        m.handle_input(b"q", now=0.3)
        self.assertEqual([p for k, _, p, _ in s.sent if k == "input"], [b"\x1b", b"q"])

    def test_esc_bracket_split_is_not_swallowed(self):
        m, s = model()
        m.handle_input(b"\x1b", now=0.0)
        m.handle_input(b"[", now=0.01)
        m.handle_input(b"A", now=0.02)
        m.flush_input(now=1.0)
        self.assertEqual(s.payloads("input"), b"\x1b[A")

    def test_ctrl_c_after_pending_esc_keeps_order(self):
        m, s = model()
        m.handle_input(b"\x1b", now=0.0)
        m.handle_input(b"\x03", now=0.01)
        m.flush_input(now=1.0)
        self.assertEqual(s.payloads("input"), b"\x1b\x03")


class FocusCommandTests(unittest.TestCase):
    def test_focus_commands_send_only_focus_and_never_ownership_changes(self):
        m, s = model()
        for key in (b"1", b"2", b"3", b"\t", b"\t", b"\t"):
            feed_and_settle(m, bytes([PREFIX]) + key)
        kinds = {k for k, *_ in s.sent}
        self.assertEqual(kinds, {"focus"})
        self.assertEqual([f["pane"] for k, f, *_ in s.sent],
                         ["manager_omp", "worker_omp", "host_shell", "manager_omp", "worker_omp", "host_shell"])

    def test_detach_command_sets_quit_and_sends_no_shutdown(self):
        m, s = model()
        feed_and_settle(m, bytes([PREFIX]) + b"d")
        self.assertTrue(m.quit)
        self.assertEqual([k for k, *_ in s.sent if k.startswith("shutdown")], [])
        self.assertEqual(s.of("input"), [])


if __name__ == "__main__":
    unittest.main()
