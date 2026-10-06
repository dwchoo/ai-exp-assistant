"""p27-cd68-o1-fix-01: string control sequences never become pane text (smoke-03 O1).

Inside tmux, OMP 18.6.1 wraps its OSC 777 turn notification in a tmux DCS
passthrough (``ESC P tmux; <payload with every ESC doubled> ESC \\``, sometimes
followed by a bare BEL; ``packages/tui/src/tmux.ts``). pyte 0.8.2 has no DCS
state, so the pane showed ``tmux;]777;notify;…``. The pane VT stream now
swallows DCS/SOS/PM/APC strings (also split over reads); plain OSC 777/9/99
were already swallowed by pyte and stay so.
"""

from __future__ import annotations

import json
import unittest

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream

PAYLOAD = json.dumps({"event": "stop", "query": "사용자 입력 원문", "response": "manager reply", "agent": "omp"},
                     ensure_ascii=False)
OSC777 = f"\x1b]777;notify;warp://cli-agent;{PAYLOAD}\x07".encode()
# OMP 18.6.1 Yz(): `\x1BPtmux;${e.replaceAll("\x1B", "\x1B\x1B")}\x1B\\`, plus a BEL for some events.
TMUX_WRAPPED = b"\x1bPtmux;" + OSC777.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"


def render(*chunks: bytes, cols: int = 200) -> list[str]:
    screen = TerminalScreen(cols, 6)
    stream = make_stream(screen)
    for chunk in chunks:
        stream.feed(chunk)
    return [line.rstrip() for line in screen.display if line.strip()]


class StringSequenceTests(unittest.TestCase):
    def test_the_tmux_wrapped_omp_notification_is_not_text(self):
        for tail in (b"", b"\x07"):
            with self.subTest(trailing_bel=bool(tail)):
                self.assertEqual(render(b"before ", TMUX_WRAPPED + tail, "after 한글".encode()), ["before after 한글"])

    def test_split_at_every_byte_boundary(self):
        data = b"A" + TMUX_WRAPPED + b"\x07B"
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                self.assertEqual(render(data[:cut], data[cut:]), ["AB"])
        self.assertEqual(render(*[data[i:i + 1] for i in range(len(data))]), ["AB"], "one byte per read")

    def test_plain_notifications_stay_invisible(self):
        for sequence in (OSC777, OSC777[:-1] + b"\x1b\\", b"\x1b]9;hello\x07", b"\x1b]99;i=1:d=0;hello\x1b\\",
                         b"\x1b]99;;body\x07"):
            with self.subTest(sequence=sequence[:12]):
                data = b"A" + sequence + b"B"
                self.assertEqual(render(data), ["AB"])
                self.assertEqual(render(data[:5], data[5:]), ["AB"])

    def test_any_dcs_sos_pm_apc_string_is_swallowed(self):
        for intro in (b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_"):
            with self.subTest(intro=intro):
                self.assertEqual(render(b"A" + intro + b"q#0;2;0;0;0#0~~@@vv\x1b\\B"), ["AB"])
        self.assertEqual(render(b"A\x1bPtmux;\x1b\x1b[31mred\x1b\\B"), ["AB"], "a wrapped CSI is not applied either")

    def test_can_or_sub_abort_a_string(self):
        for abort in (b"\x18", b"\x1a"):
            with self.subTest(abort=abort):
                self.assertEqual(render(b"A\x1bPnever closed" + abort + b"B"), ["AB"])

    def test_ordinary_output_is_unchanged(self):
        self.assertEqual(render(b"\x1b[1mbold\x1b[0m P ok \x1b]0;title\x07end"), ["bold P ok end"])
        screen = TerminalScreen(40, 4)
        stream = make_stream(screen)
        stream.feed("가나".encode()[:4])
        stream.feed("가나".encode()[4:] + b"\x1b[2;1Hx\x1b")
        stream.feed(b"[1mY")
        self.assertEqual(screen.display[0].rstrip(), "가나")
        self.assertEqual(screen.display[1].rstrip(), "xY", "a chunk ending in ESC continues in the next read")
        self.assertTrue(screen.buffer[1][1].bold)



class StringBoundsTests(unittest.TestCase):
    """p27-cd68-o1-fix-02 (review-01 / test-01): a string never hides a pane for good."""

    def test_can_or_sub_right_after_esc_abort(self):
        for abort in (b"\x18", b"\x1a"):
            with self.subTest(abort=abort):
                self.assertEqual(render(b"A\x1bPxx\x1b" + abort + b"B"), ["AB"])
                self.assertEqual(render(b"A\x1bPxx\x1b", abort + b"B"), ["AB"], "split after the ESC")

    def test_esc_with_another_byte_ends_the_string_and_acts(self):
        self.assertEqual(render(b"A\x1bPlost st \x1b[1mbold"), ["Abold"], "the CSI after a lost ST applies")
        self.assertEqual(render(b"old\x1bPlost \x1bcB"), ["B"], "RIS inside an unterminated string still resets")
        self.assertEqual(render(b"A\x1bPone \x1bPtwo\x1b\\B"), ["AB"], "a new DCS restarts the string")
        self.assertEqual(render(b"A\x1bPtmux;\x1b\x1b]777;x\x07\x1b\\B"), ["AB"], "tmux doubling still holds")

    def test_a_string_is_capped(self):
        from workbench.terminal.vt_g1.screen import STRING_MAX
        self.assertEqual(STRING_MAX, 8 << 20)
        screen = TerminalScreen(40, 4)
        stream = make_stream(screen)
        stream.feed(b"A\x1bP")
        for _ in range(STRING_MAX // 65536):
            stream.feed(b"x" * 65536)
        stream.feed(b"\r\nvisible again")
        self.assertEqual(screen.display[-1].rstrip() or screen.display[1].rstrip(), "visible again")

    def test_esc_esc_p_starts_a_dcs(self):
        self.assertEqual(render(b"A\x1b\x1bPtmux;x\x1b\\B"), ["AB"])
        self.assertEqual(render(b"A\x1b", b"\x1bPtmux;x\x1b\\B"), ["AB"])
        self.assertEqual(render(b"A\x1b\x1b[1mB"), ["AB"])

    def test_large_output_stays_fast(self):
        import time
        data = (b"plain text line with \x1b[32mcolor\x1b[0m\r\n" * 120000)[:5_000_000]
        stream = make_stream(TerminalScreen(80, 24))
        started = time.perf_counter()
        stream._drop_strings(data)
        stream._drop_strings(b"\x1bP" + b"y" * 5_000_000)
        self.assertLess(time.perf_counter() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
