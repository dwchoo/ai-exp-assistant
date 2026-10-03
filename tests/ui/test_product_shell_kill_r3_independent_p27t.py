"""Independent R3 variants for the C-D63 kill confirmation (p27-shell-test-03).

Expectations come from root-adjudication-p27-cd63-r3 and review R3 (result-p27-cd63-review-01), not from the worker's
tests: once the confirmation is open a chunk confirms the kill only when it is *exactly one confirm key* (k, K, Ctrl-k,
ㅏ with its single IME commit key, prefix+k), including the case where the opener and the confirm arrive together
(prefix + k + k). Any chunk with extra bytes (auto-repeat, typeahead, an unbracketed paste that starts with k, a second
Ctrl-k, ㅏㅏ) cancels: no kill_pane is sent and nothing reaches any pane.

Model-level, with a recording sender; no backend, no OMP, no model.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from test_product_shell_kill_independent_p27s import JAMO_K, OPEN_FORMS, P, feed, make, pane_frames  # noqa: E402

ALL_FOCUS = ("manager_omp", "worker_omp", "host_shell")

# One read each, delivered while the confirmation is open.
CANCEL_CHUNKS = {
    "kk": b"kk",
    "kkk": b"kkk",
    "kill": b"kill",
    "k_enter": b"k\r",
    "k_lf": b"k\n",
    "k_space_text": b"k ls",
    "K_extra": b"Kx",
    "unbracketed_paste_k": b"kubectl get pods\r",
    "ctrl_k_ctrl_k": b"\x0b\x0b",
    "ctrl_k_k": b"\x0bk",
    "k_ctrl_k": b"k\x0b",
    "jamo_jamo": JAMO_K + JAMO_K,
    "jamo_k": JAMO_K + b"k",
    "k_jamo": b"k" + JAMO_K,
    "jamo_space_space": JAMO_K + b"  ",
    "jamo_space_text": JAMO_K + b" ls",
    "k_then_esc_seq": b"k\x1b[A",
}
CONFIRM_CHUNKS = {"k": b"k", "K": b"K", "ctrl_k": b"\x0b", "jamo": JAMO_K, "jamo_space": JAMO_K + b" ",
                  "prefix_k": P + b"k", "prefix_ctrl_k": P + b"\x0b", "prefix_jamo": P + JAMO_K}


def opened(opener, focus: str):
    m, s = make(focus=focus)
    t = feed(m, opener)
    assert m.kill_confirm_open, f"fixture: {opener!r} did not open the confirmation"
    s.sent.clear()
    return m, s, t


class MultiByteChunkCancelsTests(unittest.TestCase):
    def test_every_chunk_with_extra_bytes_cancels_and_nothing_is_sent_from_any_opener_and_focus(self):
        for open_name, opener in OPEN_FORMS.items():
            for focus in ALL_FOCUS:
                for name, chunk in CANCEL_CHUNKS.items():
                    with self.subTest(open=open_name, focus=focus, chunk=name):
                        m, s, t = opened(opener, focus)
                        feed(m, chunk, t)
                        self.assertFalse(m.kill_confirm_open, "the confirmation stayed open")
                        self.assertEqual(s.of("kill_pane"), [], "a multi-byte chunk confirmed the kill")
                        self.assertEqual(s.sent, [], "something was sent after the cancelling chunk")
                        self.assertFalse(m.quit)

    def test_a_cancelled_chunk_leaves_a_clean_state_so_a_later_k_is_plain_text(self):
        for name, chunk in CANCEL_CHUNKS.items():
            with self.subTest(chunk=name):
                m, s, t = opened(P + b"k", "host_shell")
                t = feed(m, chunk, t)
                s.sent.clear()
                feed(m, b"k", t + 2)
                self.assertEqual(s.of("kill_pane"), [])
                self.assertEqual(s.payloads("input"), b"k")

    def test_the_opener_and_extra_bytes_in_one_read_also_cancel(self):
        for focus in ALL_FOCUS:
            for name, data in {"prefix_kkk": P + b"kkk", "prefix_kls": P + b"kls\r", "prefix_k_enter": P + b"k\r",
                               "prefix_ctrl_k_ctrl_k_k": P + b"\x0b\x0bk"}.items():
                with self.subTest(focus=focus, data=name):
                    m, s = make(focus=focus)
                    feed(m, data)
                    self.assertEqual(s.of("kill_pane"), [], "opener plus extra bytes confirmed the kill")
                    self.assertEqual(pane_frames(s), [], "bytes after the opener reached a pane")
                    self.assertFalse(m.kill_confirm_open)

    def test_the_chunk_is_judged_whole_not_after_a_partial_hold_flush(self):
        # The same bytes read in one chunk or fed one at a time through the same parser state must not differ in what
        # reaches a pane; a single-byte read of k is a confirm only when it really was alone.
        for name, chunk in CANCEL_CHUNKS.items():
            with self.subTest(chunk=name):
                m, s, t = opened(P + b"k", "manager_omp")
                m.handle_input(chunk, now=t)  # no flush yet: the hold timer has not run
                m.flush_input(now=t + 1.0)
                self.assertEqual(s.of("kill_pane"), [])
                self.assertEqual(pane_frames(s), [])


class SingleKeyConfirmsTests(unittest.TestCase):
    def test_exactly_one_confirm_key_confirms_once_with_no_pane_bytes(self):
        for open_name, opener in OPEN_FORMS.items():
            for focus in ALL_FOCUS:
                for name, chunk in CONFIRM_CHUNKS.items():
                    with self.subTest(open=open_name, focus=focus, confirm=name):
                        m, s, t = opened(opener, focus)
                        feed(m, chunk, t)
                        self.assertEqual([k[:3] for k in s.of("kill_pane")],
                                         [("kill_pane", {"pane": "host_shell"}, b"")])
                        self.assertEqual(pane_frames(s), [], "a key of the confirmation reached a pane")
                        self.assertFalse(m.kill_confirm_open)

    def test_split_arrival_of_the_opener_and_a_lone_k_confirms_and_two_separate_k_reads_do_not_double(self):
        m, s = make()
        for part in (P, b"k", b"k"):  # opener split into two reads, then a lone k read
            m.handle_input(part, now=0.0)
            m.flush_input(now=0.5)
        self.assertEqual(len(s.of("kill_pane")), 1)
        self.assertEqual(pane_frames(s), [])
        self.assertFalse(m.kill_confirm_open)
        # The modal is gone: a further k is ordinary input for the focus pane, not a second kill.
        s.sent.clear()
        feed(m, b"k", 5.0)
        self.assertEqual(s.of("kill_pane"), [])

    def test_prefix_then_k_after_the_modal_opened_confirms(self):
        m, s, t = opened(P + b"k", "worker_omp")
        feed(m, P, t)
        self.assertTrue(m.kill_confirm_open, "a lone prefix must keep the confirmation open")
        self.assertEqual(s.sent, [])
        feed(m, b"k", t + 1.0)
        self.assertEqual(len(s.of("kill_pane")), 1)
        self.assertEqual(pane_frames(s), [])


if __name__ == "__main__":
    unittest.main()
