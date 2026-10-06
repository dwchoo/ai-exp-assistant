"""Independent tests for the C-D69 (1) corrections in the product UI model (p27-cd69-test-02).

Expectations come from DECISIONS.md C-D69 (1) and the review findings (p27-cd69-review-01 P2-2, test-01 P3), not
from the implementation:
- an OMP pane's OSC 52 write (plain or tmux-wrapped) up to the 1 MiB cap (300 KB, ~1 MiB) reaches the outer
  terminal whole, however it arrives (one frame, 4 KiB..256 KiB frames, all queued before any feeding), without a
  UI catch-up and without its base64 ever drawn in the pane (screen or scrollback);
- over the cap: nothing forwarded, a refusal notice, no base64 drawn, and the next small copy still works;
- a catch-up whose kept tail starts inside a copy body never draws base64 (also a body resumed live afterwards)
  and never copies;
- two or more OSC 52 writes of one feed are all forwarded, in order.
No tmux server, no OMP, no provider: the model only.
"""
from __future__ import annotations

import base64
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product.model import (  # noqa: E402
    CATCHUP_BACKLOG_BYTES, CATCHUP_KEEP_BYTES, MAX_COPY_BYTES, ProductModel,
)

OMP_PANES = (PaneId.MANAGER_OMP, PaneId.WORKER_OMP)
TMUX = {"TMUX": "/tmp/not-a-real-tmux-p27cd69b,1,0"}
BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{16,}")


def b64(data) -> bytes:
    return base64.b64encode(data if isinstance(data, bytes) else data.encode())


def omp_copy(text, end=b"\x07") -> bytes:
    return b"\x1b]52;c;" + b64(text) + end


def tmux_wrap(inner: bytes) -> bytes:
    return b"\x1bPtmux;" + inner.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"


def expected_outer(data, tmux=False) -> bytes:
    plain = b"\x1b]52;c;" + b64(data) + b"\x07"
    return plain + (tmux_wrap(plain) if tmux else b"")


def frame(pane, data, *, replay=False, session="s1", gen=1, seq=1):
    header = {"type": "display", "pane": pane.value, "session_id": session, "generation": gen, "sequence": seq}
    if replay:
        header["replay"] = True
    return ui_v1.Frame(header, data)


def payload(size: int, label: str) -> bytes:
    """Deterministic, non-repeating-looking text so a wrong or cut delivery cannot compare equal."""
    unit = (label + " 복사 line %08d the quick brown fox\n").encode()
    out, i = bytearray(), 0
    while len(out) < size:
        out += unit % i
        i += 1
    return bytes(out[:size])


class Base(unittest.TestCase):
    def make(self, environ=None):
        model = ProductModel(FakeSender(), 30, 120, clock=lambda: 1000.0, environ={} if environ is None else environ)
        model.apply_snapshot(snapshot())
        return model

    def drain(self, model):
        while model.feed_pending():
            pass

    def push(self, model, pane, data, step, seq0=1):
        """Queue ``data`` in ``step``-byte frames without feeding anything in between (worst-case backlog)."""
        for n, at in enumerate(range(0, len(data), step)):
            model.enqueue_display(frame(pane, data[at:at + step], seq=seq0 + n))
        return seq0 + len(range(0, len(data), step))

    def drawn(self, model, pane):
        screen = model.panes[pane].screen
        rows = list(screen.display)
        for part in (screen.history.top, screen.history.bottom):
            for line in part:
                rows.append("".join(line[x].data for x in sorted(line)))
        return rows

    def assertNoBase64Drawn(self, model, pane):
        for row in self.drawn(model, pane):
            self.assertIsNone(BASE64_RUN.search(row), f"base64 drawn in the pane: {row[:80]!r}")


class LargeCopyDeliveryTests(Base):
    def test_300kb_and_the_cap_are_delivered_without_catch_up_in_every_arrival_shape(self):
        for size in (300 * 1024, MAX_COPY_BYTES):
            data = payload(size, f"big{size}")
            for pane in OMP_PANES:
                for wrap in (False, True):
                    for environ, tmux in (({}, False), (TMUX, True)):
                        for step in (None, 4096, 65536, 262144):
                            name = dict(size=size, pane=pane.value, wrap=wrap, tmux=tmux, step=step)
                            with self.subTest(**name):
                                model = self.make(environ)
                                seq = omp_copy(data)
                                seq = tmux_wrap(seq) if wrap else seq
                                stream = b"<" + seq + b">"
                                if step is None:
                                    model.on_display(frame(pane, stream))
                                else:
                                    self.push(model, pane, stream, step)
                                    self.drain(model)
                                self.assertEqual(0, model.panes[pane].skipped_bytes, "a catch-up dropped output")
                                self.assertNotIn("따라잡음", model.notice)
                                self.assertNotIn("복사 건너뜀", model.notice)
                                out = model.take_output()
                                self.assertEqual(expected_outer(data, tmux), out, "clipboard not delivered whole")
                                self.assertNotIn("거부", model.notice)
                                self.assertEqual("<>", model.panes[pane].screen.display[0].rstrip())
                                self.assertNoBase64Drawn(model, pane)

    def test_a_big_copy_amid_normal_output_keeps_the_surrounding_text_and_the_next_copy_works(self):
        data = payload(300 * 1024, "amid")
        model = self.make()
        pane = PaneId.MANAGER_OMP
        stream = b"before\r\n" + omp_copy(data) + b"after\r\n" + omp_copy("small next")
        self.push(model, pane, stream, 8192)
        self.drain(model)
        self.assertEqual(expected_outer(data) + expected_outer("small next"), model.take_output())
        rows = [row.rstrip() for row in model.panes[pane].screen.display]
        self.assertEqual(["before", "after"], [row for row in rows if row])
        self.assertEqual(0, model.panes[pane].skipped_bytes)

    def test_a_big_copy_does_not_stop_catch_up_for_real_flood_output_around_it(self):
        # the body is excluded from the backlog accounting, but ordinary output still triggers a catch-up
        flood = b"".join(b"row %07d ................................\r\n" % i for i in range(9000))  # > 256 KiB
        self.assertGreater(len(flood), CATCHUP_BACKLOG_BYTES)
        data = payload(300 * 1024, "flood")
        model = self.make()
        pane = PaneId.WORKER_OMP
        self.push(model, pane, flood, 16384)
        self.drain(model)
        self.assertGreater(model.panes[pane].skipped_bytes, 0, "ordinary flood output was never caught up")
        self.assertEqual(b"", model.take_output())
        self.push(model, pane, omp_copy(data), 16384, seq0=1000)
        self.drain(model)
        self.assertEqual(expected_outer(data), model.take_output())
        self.assertNoBase64Drawn(model, pane)


class OverCapTests(Base):
    def test_over_the_cap_is_refused_with_a_notice_and_no_base64_in_any_arrival_shape(self):
        for size in (MAX_COPY_BYTES + 1, MAX_COPY_BYTES + 100_000, 3 * MAX_COPY_BYTES):
            data = payload(size, f"over{size}")
            for pane in OMP_PANES:
                for environ in ({}, TMUX):
                    for step in (None, 4096, 65536, 262144):
                        with self.subTest(size=size, pane=pane.value, tmux=bool(environ), step=step):
                            model = self.make(environ)
                            stream = b"<" + omp_copy(data) + b">"
                            if step is None:
                                model.on_display(frame(pane, stream))
                            else:
                                self.push(model, pane, stream, step)
                                self.drain(model)
                            self.assertEqual(b"", model.take_output(), "an over-cap copy was forwarded")
                            self.assertIn("거부", model.notice)
                            self.assertEqual("<>", model.panes[pane].screen.display[0].rstrip())
                            self.assertNoBase64Drawn(model, pane)
                            model.on_display(frame(pane, omp_copy("small after"), seq=9999))
                            self.assertEqual(expected_outer("small after", bool(environ)), model.take_output())

    def test_over_the_cap_tmux_wrapped_is_refused_without_a_pty_leak(self):
        data = payload(MAX_COPY_BYTES + 5000, "overwrap")
        model = self.make(TMUX)
        self.push(model, PaneId.MANAGER_OMP, b"x" + tmux_wrap(omp_copy(data)) + b"y", 32768)
        self.drain(model)
        self.assertEqual(b"", model.take_output())
        self.assertIn("거부", model.notice)
        self.assertEqual("xy", model.panes[PaneId.MANAGER_OMP].screen.display[0].rstrip())
        self.assertNoBase64Drawn(model, PaneId.MANAGER_OMP)


class CatchUpInsideBodyTests(Base):
    FILLER = b"".join(b"row %07d ................................\r\n" % i for i in range(1500))

    def flood_until_catch_up_is_due(self, model, pane):
        seq = 0
        while model.panes[pane].backlog_bytes <= CATCHUP_BACKLOG_BYTES + 70_000:
            seq += 1
            model.enqueue_display(frame(pane, omp_copy(f"old {seq}") + self.FILLER, seq=seq))
        return seq

    def test_a_kept_tail_that_starts_inside_a_body_never_draws_base64_or_copies(self):
        for pane in OMP_PANES:
            for wrap in (False, True):
                for body_size in (90_000, 200_000, 600_000):
                    for offset in (0, 10, 5000, 60_000):
                        with self.subTest(pane=pane.value, wrap=wrap, body=body_size, offset=offset):
                            model = self.make(TMUX if wrap else None)
                            seq = self.flood_until_catch_up_is_due(model, pane)
                            big = omp_copy(payload(body_size, "tail"))
                            big = tmux_wrap(big) if wrap else big
                            cut = len(big) - offset
                            model.enqueue_display(frame(pane, big[:cut], seq=seq + 1))
                            model.enqueue_display(frame(pane, big[cut:] + b"\r\nTAILEND", seq=seq + 2))
                            self.drain(model)
                            self.assertGreater(model.panes[pane].skipped_bytes, 0, "no catch-up happened")
                            self.assertEqual(b"", model.take_output(), "a copy was forwarded from a catch-up")
                            self.assertNoBase64Drawn(model, pane)
                            self.assertIn("TAILEND", "\n".join(model.panes[pane].screen.display))
                            model.enqueue_display(frame(pane, omp_copy("live again"), seq=seq + 3))
                            self.drain(model)
                            self.assertEqual(expected_outer("live again", wrap), model.take_output())

    def test_a_body_that_began_before_a_catch_up_and_resumes_live_is_skipped_never_drawn(self):
        for pane in OMP_PANES:
            for wrap in (False, True):
                with self.subTest(pane=pane.value, wrap=wrap):
                    model = self.make(TMUX if wrap else None)
                    seq = self.flood_until_catch_up_is_due(model, pane)
                    big = omp_copy(payload(400_000, "resume"))
                    big = tmux_wrap(big) if wrap else big
                    model.enqueue_display(frame(pane, big[:150_000], seq=seq + 1))  # the body is still open here
                    self.drain(model)
                    self.assertGreater(model.panes[pane].skipped_bytes, 0, "no catch-up happened")
                    mark = model.panes[pane].skipped_bytes
                    # the rest of the body arrives live afterwards, in small frames, then ordinary output
                    self.push(model, pane, big[150_000:] + b"\r\nRESUMED", 4096, seq0=seq + 2)
                    self.drain(model)
                    self.assertEqual(mark, model.panes[pane].skipped_bytes, "the live rest was dropped too")
                    self.assertEqual(b"", model.take_output())
                    self.assertNoBase64Drawn(model, pane)
                    self.assertIn("RESUMED", "\n".join(model.panes[pane].screen.display))
                    model.enqueue_display(frame(pane, omp_copy("clean"), seq=seq + 999))
                    self.drain(model)
                    self.assertEqual(expected_outer("clean", wrap), model.take_output())

    def test_a_catch_up_that_cut_a_copy_says_so_and_one_that_cut_none_does_not(self):
        pane = PaneId.MANAGER_OMP
        cut = self.make()
        seq = self.flood_until_catch_up_is_due(cut, pane)  # queued bodies are dropped by the catch-up
        self.drain(cut)
        self.assertIn("따라잡음", cut.notice)
        self.assertIn("복사 건너뜀", cut.notice)
        plain = self.make()
        flood = b"".join(b"row %07d ................................\r\n" % i for i in range(9000))
        self.push(plain, pane, flood, 16384)
        self.drain(plain)
        self.assertIn("따라잡음", plain.notice)
        self.assertNotIn("복사 건너뜀", plain.notice)
        self.assertEqual(seq > 0, True)


class MultipleWritesTests(Base):
    def test_two_and_three_writes_in_one_feed_are_all_forwarded_in_order(self):
        cases = {
            "plain": omp_copy("first") + b"x" + omp_copy("second"),
            "adjacent": omp_copy("first") + omp_copy("second"),
            "st-then-bel": omp_copy("first", end=b"\x1b\\") + b"--" + omp_copy("second"),
            "wrapped": tmux_wrap(omp_copy("first")) + tmux_wrap(omp_copy("second")),
            "mixed": omp_copy("first") + tmux_wrap(omp_copy("second")),
            "three": omp_copy("first") + omp_copy("second") + omp_copy("third"),
        }
        for pane in OMP_PANES:
            for name, data in cases.items():
                for environ, tmux in (({}, False), (TMUX, True)):
                    for path in ("direct", "queued"):
                        with self.subTest(pane=pane.value, case=name, tmux=tmux, path=path):
                            model = self.make(environ)
                            if path == "direct":
                                model.on_display(frame(pane, b"<" + data + b">"))
                            else:
                                model.enqueue_display(frame(pane, b"<" + data + b">"))
                                self.drain(model)
                            names = ["first", "second", "third"][:3 if name == "three" else 2]
                            self.assertEqual(b"".join(expected_outer(n, tmux) for n in names), model.take_output())
                            self.assertEqual("<" + {"plain": "x", "st-then-bel": "--"}.get(name, "") + ">",
                                             model.panes[pane].screen.display[0].rstrip())

    def test_two_writes_inside_one_feed_with_a_big_one_between_keep_order(self):
        big = payload(300 * 1024, "mid")
        model = self.make()
        model.on_display(frame(PaneId.WORKER_OMP, omp_copy("head") + omp_copy(big) + omp_copy("tail")))
        self.assertEqual(expected_outer("head") + expected_outer(big) + expected_outer("tail"), model.take_output())

    def test_writes_split_across_frames_in_one_drain_all_arrive_in_order(self):
        data = omp_copy("alpha") + b"." + tmux_wrap(omp_copy("beta")) + b"." + omp_copy("gamma", end=b"\x1b\\")
        for cut_a in range(1, len(data) - 1, 3):
            with self.subTest(cut=cut_a):
                model = self.make()
                model.enqueue_display(frame(PaneId.MANAGER_OMP, data[:cut_a], seq=1))
                model.enqueue_display(frame(PaneId.MANAGER_OMP, data[cut_a:], seq=2))
                self.drain(model)
                self.assertEqual(expected_outer("alpha") + expected_outer("beta") + expected_outer("gamma"),
                                 model.take_output())

    def test_a_refused_write_between_two_good_ones_does_not_drop_either(self):
        over = payload(MAX_COPY_BYTES + 1, "refused")
        model = self.make()
        model.on_display(frame(PaneId.MANAGER_OMP, omp_copy("good one") + omp_copy(over) + omp_copy("good two")))
        self.assertEqual(expected_outer("good one") + expected_outer("good two"), model.take_output())

    def test_host_shell_still_never_forwards_two_writes(self):
        model = self.make()
        model.on_display(frame(PaneId.HOST_SHELL, omp_copy("a") + omp_copy("b") + b"done"))
        self.assertEqual(b"", model.take_output())


if __name__ == "__main__":
    unittest.main()
