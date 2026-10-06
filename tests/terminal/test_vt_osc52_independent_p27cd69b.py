"""Independent tests for the C-D69 (1) corrections at the pane VT stream (p27-cd69-test-02).

Expectations come from DECISIONS.md C-D69 (1) and p27-cd69-review-01 P2-2, not from the implementation:
- a body up to 1 MiB of data (base64 ~1.4 MB) is reported whole to the clipboard callback whatever the feed size
  (feeds of 1 KiB up to the whole stream), and never drawn;
- a body over the owner's cap is reported as ``None`` (refused, never cut) and never drawn;
- two or more OSC 52 writes of one feed are all reported, in order (plain, tmux-wrapped, mixed);
- ``in_clipboard_body`` is True exactly while an unterminated body is held back, so an owner that starts a new stream
  inside a body can recognise (and skip) the rest instead of drawing it as text.
"""
from __future__ import annotations

import base64
import random
import re
import unittest

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream

MAX_DATA = 1024 * 1024
CAP = 4 * -(-MAX_DATA // 3) + 16  # what the product model hands the stream as ``clipboard_max``
BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{16,}")


def b64(data) -> bytes:
    return base64.b64encode(data if isinstance(data, bytes) else data.encode())


def osc52(data, end=b"\x07") -> bytes:
    return b"\x1b]52;c;" + b64(data) + end


def tmux_wrap(inner: bytes) -> bytes:
    return b"\x1bPtmux;" + inner.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"


def blob(size: int, label: str) -> bytes:
    unit = (label + " 데이터 %08d lorem ipsum\n").encode()
    out, i = bytearray(), 0
    while len(out) < size:
        out += unit % i
        i += 1
    return bytes(out[:size])


class Pane:
    def __init__(self, cap=CAP):
        self.reports: list = []
        self.screen = TerminalScreen(80, 6)
        self.stream = make_stream(self.screen, clipboard=self.reports.append, clipboard_max=cap)

    def feed_in(self, data: bytes, step: int | None):
        for at in range(0, len(data), step or len(data) or 1):
            self.stream.feed(data[at:at + (step or len(data))])
        return self

    def rows(self):
        lines = list(self.screen.display)
        for part in (self.screen.history.top, self.screen.history.bottom):
            lines += ["".join(line[x].data for x in sorted(line)) for line in part]
        return lines

    def text(self):
        return [row.rstrip() for row in self.screen.display if row.strip()]


class BigBodyTests(unittest.TestCase):
    def test_up_to_the_cap_is_reported_whole_and_never_drawn(self):
        for size in (300 * 1024, MAX_DATA):
            data = blob(size, f"b{size}")
            for name, wrap in (("plain", lambda s: s), ("tmux", tmux_wrap)):
                for step in (None, 1024, 65536, 1 << 20):
                    with self.subTest(size=size, form=name, step=step):
                        pane = Pane().feed_in(b"<" + wrap(osc52(data)) + b">", step)
                        self.assertEqual([b"c;" + b64(data)], pane.reports)
                        self.assertEqual(["<>"], pane.text())
                        for row in pane.rows():
                            self.assertIsNone(BASE64_RUN.search(row))

    def test_over_the_cap_is_reported_as_refused_never_cut_and_never_drawn(self):
        data = blob(MAX_DATA + 1000, "over")  # base64 beyond the stream cap (the owner also checks the decoded size)
        for name, wrap in (("plain", lambda s: s), ("tmux", tmux_wrap)):
            for step in (None, 4096, 1 << 20):
                with self.subTest(form=name, step=step):
                    pane = Pane().feed_in(b"<" + wrap(osc52(data)) + b">", step)
                    self.assertEqual([None], pane.reports)
                    self.assertEqual(["<>"], pane.text())
                    for row in pane.rows():
                        self.assertIsNone(BASE64_RUN.search(row))

    def test_a_cap_boundary_pair_decides_by_the_base64_length(self):
        small = Pane(cap=100).feed_in(osc52(b"x" * 60), None)  # 80 base64 chars + "c;" fits
        large = Pane(cap=100).feed_in(osc52(b"x" * 90), None)  # 120 base64 chars does not
        self.assertEqual([b"c;" + b64(b"x" * 60)], small.reports)
        self.assertEqual([None], large.reports)


class MultipleWritesTests(unittest.TestCase):
    def test_all_writes_of_one_feed_are_reported_in_order(self):
        cases = {
            "plain": [osc52("one"), osc52("two"), osc52("three")],
            "st": [osc52("one", end=b"\x1b\\"), osc52("two", end=b"\x1b\\")],
            "tmux": [tmux_wrap(osc52("one")), tmux_wrap(osc52("two")), tmux_wrap(osc52("three"))],
            "mixed": [osc52("one"), tmux_wrap(osc52("two")), osc52("three", end=b"\x1b\\")],
        }
        for name, parts in cases.items():
            for glue in (b"", b" ", b"\r\n"):
                with self.subTest(case=name, glue=glue):
                    pane = Pane().feed_in(glue.join(parts), None)
                    expected = [b"c;" + b64(w) for w in ("one", "two", "three")[:len(parts)]]
                    self.assertEqual(expected, pane.reports)
                    self.assertEqual([], [r for r in pane.rows() if BASE64_RUN.search(r)])

    def test_random_chunking_of_many_writes_keeps_order_and_count(self):
        rng = random.Random(692)
        names = [f"w{i}" for i in range(6)]
        data = b"".join(osc52(n) if i % 2 else tmux_wrap(osc52(n)) for i, n in enumerate(names)) + b"END"
        for trial in range(80):
            cuts = sorted(rng.sample(range(1, len(data)), rng.randint(1, 15)))
            pane = Pane()
            for a, b in zip([0] + cuts, cuts + [len(data)]):
                pane.stream.feed(data[a:b])
            with self.subTest(trial=trial):
                self.assertEqual([b"c;" + b64(n) for n in names], pane.reports)
                self.assertEqual(["END"], pane.text())

    def test_a_big_write_between_two_small_ones_keeps_order(self):
        big = blob(500 * 1024, "mid")
        pane = Pane().feed_in(osc52("head") + osc52(big) + osc52("tail"), 1 << 16)
        self.assertEqual([b"c;" + b64("head"), b"c;" + b64(big), b"c;" + b64("tail")], pane.reports)


class InClipboardBodyTests(unittest.TestCase):
    def test_true_only_while_a_body_is_open(self):
        for form, wrap in (("plain", lambda s: s), ("tmux", tmux_wrap)):
            data = wrap(osc52(blob(5000, "flag")))
            with self.subTest(form=form):
                pane = Pane()
                self.assertFalse(pane.stream.in_clipboard_body)
                pane.stream.feed(b"hello")
                self.assertFalse(pane.stream.in_clipboard_body)
                cut = len(data) // 2
                pane.stream.feed(data[:cut])
                self.assertTrue(pane.stream.in_clipboard_body, "half a body is open")
                pane.stream.feed(data[cut:])
                self.assertFalse(pane.stream.in_clipboard_body, "closed after the terminator")
                self.assertEqual(1, len(pane.reports))
                pane.stream.feed(b"after")
                self.assertFalse(pane.stream.in_clipboard_body)

    def test_a_fresh_stream_fed_the_middle_of_a_body_does_not_know_it_is_inside_one(self):
        # the contract the owner relies on: it must track that itself and skip those bytes (never feed them)
        fresh = Pane()
        self.assertFalse(fresh.stream.in_clipboard_body)
        fresh.stream.feed(b64(blob(3000, "mid")))  # mid-body bytes fed to a fresh stream are plain text
        self.assertTrue([r for r in fresh.rows() if BASE64_RUN.search(r)], "(documents why the owner must skip them)")

    def test_an_aborted_body_is_not_open_and_not_reported(self):
        for abort in (b"\x18", b"\x1a", b"\x1bZ"):
            with self.subTest(abort=abort):
                pane = Pane()
                pane.stream.feed(b"\x1b]52;c;" + b64("partial"))
                self.assertTrue(pane.stream.in_clipboard_body)
                pane.stream.feed(abort + b"ok")
                self.assertFalse(pane.stream.in_clipboard_body)
                self.assertEqual([], pane.reports)


if __name__ == "__main__":
    unittest.main()
