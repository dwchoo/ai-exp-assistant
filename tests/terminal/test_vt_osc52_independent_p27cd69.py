"""Independent adversarial tests for C-D69 (1) at the pane VT stream (p27-cd69-test-01).

Expectations come from DECISIONS.md C-D69 (1), not from the implementation: OMP ``/copy`` writes a clipboard WRITE
signal (OSC 52, ``ESC ] 52 ; Pc ; Pd`` ended by BEL or ST; inside tmux OMP may wrap it in ``ESC P tmux; ... ESC \\``
with every ESC doubled). The pane stream must hand exactly one body per complete live sequence to its clipboard
callback, whatever the read boundaries are, never draw the body as pane text, never report a sequence fed (even
partly) as replay/skipped output (``quiet``), never report an aborted one (CAN/SUB, ESC + another byte), refuse an
oversized one without cutting it, and leave every other OSC (titles, hyperlinks, notifications, OSC 5/520/52x)
exactly as before.
"""

from __future__ import annotations

import base64
import random
import unittest

import pyte

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


def b64(data) -> bytes:
    return base64.b64encode(data if isinstance(data, bytes) else data.encode())


def osc52(text, pc: bytes = b"c", end: bytes = b"\x07") -> bytes:
    return b"\x1b]52;" + pc + b";" + b64(text) + end


def tmux_wrap(inner: bytes) -> bytes:
    return b"\x1bPtmux;" + inner.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"


FORMS = {
    "plain-bel": lambda t: osc52(t),
    "plain-st": lambda t: osc52(t, end=b"\x1b\\"),
    "tmux-bel": lambda t: tmux_wrap(osc52(t)),
    "tmux-st": lambda t: tmux_wrap(osc52(t, end=b"\x1b\\")),
}


class Pane:
    def __init__(self, cols=80, rows=6, clipboard=True, clipboard_max=None):
        self.reports: list = []
        self.screen = TerminalScreen(cols, rows)
        kwargs = {}
        if clipboard_max is not None:
            kwargs["clipboard_max"] = clipboard_max
        self.stream = make_stream(self.screen, clipboard=self.reports.append if clipboard else None, **kwargs)

    def feed(self, *chunks, quiet=False):
        for chunk in chunks:
            self.stream.quiet = quiet
            try:
                self.stream.feed(chunk)
            finally:
                self.stream.quiet = False
        return self

    def lines(self):
        return [line.rstrip() for line in self.screen.display if line.strip()]


def reference_lines(data: bytes, cols=80, rows=6):
    """What plain pyte draws for ``data`` (no DCS in it): the pre-C-D69 display of OSC that are not 52."""
    screen = pyte.Screen(cols, rows)
    pyte.ByteStream(screen).feed(data)
    return [line.rstrip() for line in screen.display if line.strip()]


class EveryBoundaryTests(unittest.TestCase):
    def test_every_two_way_split_reports_the_body_once_and_never_draws_it(self):
        for name, form in FORMS.items():
            data = b"before" + form("/copy 결과 ok") + b"after"
            for cut in range(len(data) + 1):
                with self.subTest(form=name, cut=cut):
                    pane = Pane().feed(data[:cut], data[cut:])
                    self.assertEqual([b"c;" + b64("/copy 결과 ok")], pane.reports)
                    self.assertEqual(["beforeafter"], pane.lines())

    def test_byte_by_byte_and_random_chunking(self):
        rng = random.Random(6909)
        for name, form in FORMS.items():
            data = b"A" + form("x" * 300) + b"B" + form("second") + b"C"
            expected = [b"c;" + b64("x" * 300), b"c;" + b64("second")]
            with self.subTest(form=name, mode="bytes"):
                pane = Pane().feed(*[data[i:i + 1] for i in range(len(data))])
                self.assertEqual(expected, pane.reports)
                self.assertEqual(["ABC"], pane.lines())
            for trial in range(40):
                cuts = sorted(rng.sample(range(1, len(data)), rng.randint(1, 12)))
                chunks = [data[a:b] for a, b in zip([0] + cuts, cuts + [len(data)])]
                with self.subTest(form=name, trial=trial):
                    pane = Pane().feed(*chunks)
                    self.assertEqual(expected, pane.reports)
                    self.assertEqual(["ABC"], pane.lines())

    def test_selection_parameter_is_passed_through_and_reads_are_not_turned_into_writes(self):
        pane = Pane().feed(b"\x1b]52;p;" + b64("p sel") + b"\x07", b"\x1b]52;c;?\x07", b"\x1b]52;;?\x1b\\")
        # The stream only reports bodies; whether ``?`` is forwarded is the owner's decision (see the UI tests),
        # but a read must never be reported as anything other than its own ``?`` body.
        for body in pane.reports:
            self.assertTrue(body == b"p;" + b64("p sel") or body.endswith(b";?"), body)
        self.assertEqual([], pane.lines())


class QuietTests(unittest.TestCase):
    def test_a_sequence_with_any_quiet_part_is_never_reported(self):
        for name, form in FORMS.items():
            data = form("replayed")
            for cut in range(len(data) + 1):
                for quiet_first in (True, False):
                    with self.subTest(form=name, cut=cut, quiet_first=quiet_first):
                        pane = Pane()
                        if cut == 0 or cut == len(data):
                            pane.feed(data, quiet=True)
                        else:
                            pane.feed(data[:cut], quiet=quiet_first)
                            pane.feed(data[cut:], quiet=not quiet_first)
                        self.assertEqual([], pane.reports)

    def test_live_sequence_after_quiet_output_reports_again(self):
        for name, form in FORMS.items():
            with self.subTest(form=name):
                pane = Pane().feed(b"old" + form("old"), quiet=True)
                pane.feed(b"\r\nnew" + form("new"))
                self.assertEqual([b"c;" + b64("new")], pane.reports)
                pane.feed(b"x")  # nothing replays the last copy
                self.assertEqual([b"c;" + b64("new")], pane.reports)

    def test_quiet_trailing_escape_does_not_leak_into_live(self):
        # ESC was the last byte of a replayed read; the rest of the sequence arrives live: still no copy.
        data = osc52("esc split")
        pane = Pane().feed(data[:1], quiet=True).feed(data[1:])
        self.assertEqual([], pane.reports)
        st = osc52("st split", end=b"\x1b\\")
        pane = Pane().feed(st[:-1], quiet=True).feed(st[-1:])
        self.assertEqual([], pane.reports)


class AbortAndLimitTests(unittest.TestCase):
    def test_can_sub_and_esc_other_abort_without_a_report_and_later_text_shows(self):
        body = b"\x1b]52;c;" + b64("aborted")
        for name, abort in {"CAN": b"\x18", "SUB": b"\x1a", "ESC-[": b"\x1b[1m", "ESC-c": b"\x1bc"}.items():
            with self.subTest(abort=name):
                pane = Pane().feed(b"x" + body[:9], body[9:] + abort + b"visible" + osc52("after"))
                self.assertEqual([b"c;" + b64("after")], pane.reports)
                self.assertTrue(any("visible" in line for line in pane.lines()), pane.lines())
                self.assertFalse(any(b64("aborted").decode()[:6] in line for line in pane.lines()), pane.lines())

    def test_oversized_body_is_refused_whole_not_cut_and_the_next_one_works(self):
        for name, form in FORMS.items():
            with self.subTest(form=name):
                pane = Pane(clipboard_max=64)
                big = form("y" * 200)
                pane.feed(*[big[i:i + 7] for i in range(0, len(big), 7)])
                self.assertEqual(1, len(pane.reports), pane.reports)
                self.assertIn(pane.reports[0], (None, b""), "refused, never a truncated body")
                pane.feed(form("small"))
                self.assertEqual(b"c;" + b64("small"), pane.reports[-1])

    def test_body_exactly_at_the_limit_is_kept(self):
        body = b"c;" + b64("z" * 45)  # 2 + 60 bytes
        pane = Pane(clipboard_max=len(body)).feed(b"\x1b]52;" + body + b"\x07")
        self.assertEqual([body], pane.reports)


class OtherSequencesTests(unittest.TestCase):
    def test_other_osc_render_exactly_like_plain_pyte_and_never_report(self):
        samples = {
            "title-0": b"\x1b]0;my title\x07text",
            "title-2-st": b"\x1b]2;win\x1b\\text",
            "hyperlink": b"\x1b]8;;https://example.invalid\x07link\x1b]8;;\x07 end",
            "notify-777": b"\x1b]777;notify;title;body 52;c;QQ==\x07after",
            "osc-5": b"\x1b]5;0;red\x07five",
            "osc-520": b"\x1b]520;c;" + b64("no") + b"\x07fivetwenty",
            "osc-52-no-semicolon": b"\x1b]52\x07bare",
            "osc-525": b"\x1b]525;c;QQ==\x07x",
            "osc-5-esc": b"\x1b]5\x1b[1mbold",
            "color-query": b"\x1b]11;?\x07bg",
        }
        for name, data in samples.items():
            with self.subTest(sample=name):
                for clipboard in (True, False):
                    pane = Pane(clipboard=clipboard).feed(data)
                    self.assertEqual([], pane.reports)
                    self.assertEqual(reference_lines(data), pane.lines(), clipboard)

    def test_titles_still_reach_the_screen(self):
        pane = Pane().feed(b"\x1b]2;work title\x07", osc52("t"), b"\x1b]0;final\x1b\\")
        self.assertEqual("final", pane.screen.title)
        self.assertEqual([b"c;" + b64("t")], pane.reports)

    def test_dcs_that_is_not_a_tmux_wrapped_osc52_never_reports_or_draws(self):
        samples = [
            b"\x1bPtmux;\x1b\x1b]777;notify;x\x07\x1b\\",       # tmux-wrapped other OSC
            b"\x1bP$q\x1b\x1b]52;c;" + b64("q") + b"\x07\x1b\\",  # not tmux;
            b"\x1bPtmux:\x1b\x1b]52;c;" + b64("q") + b"\x07\x1b\\",  # tmux: (colon)
            b"\x1b_tmux;\x1b\x1b]52;c;" + b64("apc") + b"\x07\x1b\\",  # APC, not DCS
            b"\x1bPtmux;\x1b\x1b]52;c;" + b64("unterminated") + b"\x1b\\",  # inner OSC never ended
        ]
        for data in samples:
            with self.subTest(data=data[:12]):
                pane = Pane().feed(b"L" + data + b"R")
                self.assertEqual([], pane.reports, data)
                self.assertEqual(["LR"], pane.lines())

    def test_no_clipboard_callback_means_no_report_and_no_text(self):
        for name, form in FORMS.items():
            with self.subTest(form=name):
                pane = Pane(clipboard=False).feed(b"$ " + form("host secret") + b"ok")
                self.assertEqual([], pane.reports)
                self.assertEqual(["$ ok"], pane.lines())


if __name__ == "__main__":
    unittest.main()
