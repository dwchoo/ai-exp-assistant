"""C-D69 (1): the pane VT stream hands OSC 52 clipboard writes to a callback instead of swallowing them.

OMP 18.6.1 ``/copy`` (``clipboard.ts`` copyToClipboard) writes ``ESC ] 52 ; c ; <base64> BEL`` to stdout when it
is a TTY. pyte 0.8.2 drops every OSC but the titles, so nothing reached the outer terminal. The stream now
collects an OSC 52 (plain, or wrapped in a tmux passthrough DCS), also split over reads, never gives it to pyte,
and reports its ``Pc;Pd`` body once it ends with BEL or ST. Output fed while ``quiet`` never reports.
"""

from __future__ import annotations

import base64
import unittest

from workbench.terminal.vt_g1.screen import STRING_MAX, TerminalScreen, make_stream

TEXT = "hello /copy 한글"
B64 = base64.b64encode(TEXT.encode())
BODY = b"c;" + B64
# OMP 18.6.1 Ol(): pb(`\x1B]52;c;${Buffer.from(e).toString("base64")}\x07`)
OMP_COPY = b"\x1b]52;c;" + B64 + b"\x07"
OSC52_ST = b"\x1b]52;c;" + B64 + b"\x1b\\"
TMUX_WRAPPED = b"\x1bPtmux;" + OMP_COPY.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
TMUX_WRAPPED_ST = b"\x1bPtmux;" + OSC52_ST.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"


class Fixture:
    def __init__(self, *, clipboard=True, clipboard_max=None):
        self.reports: list[bytes | None] = []
        self.screen = TerminalScreen(80, 4)
        kwargs = {} if clipboard_max is None else {"clipboard_max": clipboard_max}
        self.stream = make_stream(self.screen, clipboard=self.reports.append if clipboard else None, **kwargs)

    def feed(self, *chunks: bytes, quiet: bool = False) -> "Fixture":
        self.stream.quiet = quiet
        for chunk in chunks:
            self.stream.feed(chunk)
        self.stream.quiet = False
        return self

    def text(self) -> list[str]:
        return [line.rstrip() for line in self.screen.display if line.strip()]


def splits(data: bytes):
    for cut in range(1, len(data)):
        yield data[:cut], data[cut:]


class Osc52StreamTests(unittest.TestCase):
    def test_the_exact_omp_copy_bytes_are_reported_and_not_drawn(self):
        f = Fixture().feed(b"before" + OMP_COPY + b"after")
        self.assertEqual([BODY], f.reports)
        self.assertEqual(["beforeafter"], f.text())

    def test_st_terminated_form(self):
        f = Fixture().feed(b"a" + OSC52_ST + b"b")
        self.assertEqual([BODY], f.reports)
        self.assertEqual(["ab"], f.text())

    def test_split_across_reads_at_every_byte(self):
        for data in (OMP_COPY, OSC52_ST, TMUX_WRAPPED, TMUX_WRAPPED_ST):
            for head, tail in splits(b"x" + data + b"y"):
                with self.subTest(data=data[:12], cut=len(head)):
                    f = Fixture().feed(head, tail)
                    self.assertEqual([BODY], f.reports)
                    self.assertEqual(["xy"], f.text())

    def test_byte_by_byte(self):
        data = b"x" + TMUX_WRAPPED + OMP_COPY + b"y"
        f = Fixture().feed(*(data[i:i + 1] for i in range(len(data))))
        self.assertEqual([BODY, BODY], f.reports)
        self.assertEqual(["xy"], f.text())

    def test_tmux_passthrough_wrapped_form(self):
        for data in (TMUX_WRAPPED, TMUX_WRAPPED_ST):
            with self.subTest(data=data[:20]):
                f = Fixture().feed(b"a" + data + b"b")
                self.assertEqual([BODY], f.reports)
                self.assertEqual(["ab"], f.text())

    def test_other_selections_and_reads_are_reported_raw_for_the_owner_to_judge(self):
        f = Fixture().feed(b"\x1b]52;p;" + B64 + b"\x07\x1b]52;c;?\x07\x1b]52;;" + B64 + b"\x07")
        self.assertEqual([b"p;" + B64, b"c;?", b";" + B64], f.reports)

    def test_quiet_output_never_reports(self):
        f = Fixture().feed(OMP_COPY, TMUX_WRAPPED, quiet=True)
        self.assertEqual([], f.reports)
        f.feed(OMP_COPY)  # live output afterwards still copies, once
        self.assertEqual([BODY], f.reports)

    def test_an_osc52_that_began_or_ended_in_quiet_output_never_reports(self):
        for data in (OMP_COPY, TMUX_WRAPPED):
            for head, tail in splits(data):
                with self.subTest(data=data[:10], cut=len(head)):
                    began = Fixture().feed(head, quiet=True).feed(tail)
                    ended = Fixture().feed(head).feed(tail, quiet=True)
                    self.assertEqual([], began.reports)
                    self.assertEqual([], ended.reports)

    def test_oversized_body_is_reported_as_none_and_never_truncated(self):
        f = Fixture(clipboard_max=len(BODY) - 1).feed(OMP_COPY, TMUX_WRAPPED, b"after")
        self.assertEqual([None, None], f.reports)
        self.assertEqual(["after"], f.text())
        f = Fixture(clipboard_max=len(BODY)).feed(OMP_COPY, TMUX_WRAPPED)
        self.assertEqual([BODY, BODY], f.reports)

    def test_can_sub_or_another_escape_abort_without_a_report(self):
        for data in (b"\x1b]52;c;" + B64 + b"\x18", b"\x1b]52;c;" + B64 + b"\x1a",
                     b"\x1b]52;c;" + B64 + b"\x1b[1mBOLD",
                     b"\x1bPtmux;\x1b\x1b]52;c;" + B64 + b"\x18",
                     b"\x1bPtmux;\x1b\x1b]52;c;" + B64 + b"\x1b[1mBOLD",
                     b"\x1bPtmux;\x1b\x1b]52;c;" + B64 + b"\x1b\\"):  # wrapped OSC 52 without its own BEL/ST
            with self.subTest(data=data[-12:]):
                f = Fixture().feed(data, b"after")
                self.assertEqual([], f.reports)
                self.assertNotIn("52;", "".join(f.text()))
                self.assertTrue(f.text()[0].endswith("after"), f.text())

    def test_lost_terminator_is_abandoned_after_string_max_and_output_shows_again(self):
        f = Fixture().feed(b"\x1b]52;c;" + b"A" * (STRING_MAX - 2), b"visible")
        self.assertEqual([], f.reports)
        self.assertEqual(["visible"], f.text())  # "c;" counts: abandoned at STRING_MAX

    def test_other_osc_and_tmux_strings_are_unchanged(self):
        f = Fixture().feed(b"\x1b]2;my title\x07", b"\x1b]5", b"1;x\x07t", b"\x1bPtmux;\x1b\x1b]777;notify;x\x07\x1b\\")
        self.assertEqual("my title", f.screen.title)
        self.assertEqual([], f.reports)
        self.assertEqual(["t"], f.text())
        f = Fixture().feed(b"\x1b]2;", b"t2\x07")
        self.assertEqual("t2", f.screen.title)

    def test_without_a_callback_osc52_is_still_swallowed(self):
        f = Fixture(clipboard=False).feed(b"a" + OMP_COPY + TMUX_WRAPPED + b"b")
        self.assertEqual(["ab"], f.text())


    def test_in_clipboard_body_is_true_only_inside_a_held_back_body(self):
        # p27-cd69-review-01 P2-2: the UI skips the rest of a body its stream did not see begin
        for form, cut in ((OMP_COPY, 10), (OSC52_ST, 10), (TMUX_WRAPPED, 16)):
            with self.subTest(form=form[:3]):
                f = Fixture()
                self.assertFalse(f.stream.in_clipboard_body)
                f.feed(b"x" + form[:cut])
                self.assertTrue(f.stream.in_clipboard_body)
                f.feed(form[cut:])
                self.assertFalse(f.stream.in_clipboard_body)
                self.assertEqual([BODY], f.reports)
        f = Fixture().feed(b"text \x1b]0;title")
        self.assertFalse(f.stream.in_clipboard_body)

if __name__ == "__main__":
    unittest.main()
