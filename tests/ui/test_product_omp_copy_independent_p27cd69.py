"""Independent adversarial tests for C-D69 (1) in the product UI (p27-cd69-test-01).

Expectations come from DECISIONS.md C-D69 (1) and C-D62 (drag copy), not from the implementation:
- an OSC 52 clipboard WRITE from the manager/worker OMP pane (OMP ``/copy``: ``ESC ] 52 ; c ; <base64> BEL``, also
  tmux-wrapped, also split across display frames) reaches the outer terminal through the drag-copy path: plain
  ``ESC ] 52 ; c ; <b64> BEL`` and, when the UI runs inside tmux (non-empty TMUX), that plus the tmux passthrough form;
- the host shell pane never forwards; a clipboard READ (``?``) is never forwarded; invalid base64 / data over the
  size cap is never forwarded (no truncated or stale clipboard);
- only live output copies: attach replay, a reattach, catch-up (dropped or tail output) and a replaced session never
  copy; each live sequence copies once and nothing re-sends it later; other OSC stay as before.
The last part runs the real curses UI on a PTY (fixture ui_v1 server; no backend, no OMP, no tmux server).
"""
from __future__ import annotations

import base64
import random
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, FixtureServer, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product.model import CATCHUP_BACKLOG_BYTES, MAX_COPY_BYTES, ProductModel  # noqa: E402

OMP_PANES = (PaneId.MANAGER_OMP, PaneId.WORKER_OMP)


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


def clipboards(out: bytes) -> list[bytes]:
    """Decoded data of every plain OSC 52 write in ``out`` (outside the passthrough copy)."""
    found, at = [], 0
    while True:
        at = out.find(b"\x1b]52;", at)
        if at < 0:
            return found
        if at > 0 and out[at - 1:at] == b"\x1b":  # doubled ESC: the passthrough copy
            at += 1
            continue
        end = out.find(b"\x07", at)
        body = out[at + 5:end]
        found.append(base64.b64decode(body.split(b";", 1)[1]))
        at = end


class Base(unittest.TestCase):
    def make(self, environ=None):
        model = ProductModel(FakeSender(), 30, 120, clock=lambda: 1000.0, environ={} if environ is None else environ)
        model.apply_snapshot(snapshot())
        return model

    def drain(self, model):
        while model.feed_pending():
            pass


class ForwardingTests(Base):
    def test_every_form_from_both_omp_panes_reaches_the_outer_terminal_exactly(self):
        forms = {"bel": omp_copy("결과 /copy"), "st": omp_copy("결과 /copy", end=b"\x1b\\"),
                 "tmux-bel": tmux_wrap(omp_copy("결과 /copy")),
                 "tmux-st": tmux_wrap(omp_copy("결과 /copy", end=b"\x1b\\"))}
        for pane in OMP_PANES:
            for name, data in forms.items():
                for environ, tmux in (({}, False), ({"TMUX": ""}, False), ({"TMUX": "/tmp/t,1,0"}, True)):
                    with self.subTest(pane=pane.value, form=name, tmux=environ.get("TMUX")):
                        model = self.make(environ)
                        model.on_display(frame(pane, b"a" + data + b"b"))
                        self.assertEqual(expected_outer("결과 /copy", tmux), model.take_output())
                        self.assertEqual("ab", model.panes[pane].screen.display[0].rstrip())

    def test_random_chunking_through_the_queued_path_interleaved_across_both_panes(self):
        rng = random.Random(69)
        manager = b"m1" + omp_copy("from manager") + b"m2"
        worker = b"w1" + tmux_wrap(omp_copy("from worker")) + b"w2"
        for trial in range(60):
            model = self.make()
            chunks = []
            for pane, data in ((PaneId.MANAGER_OMP, manager), (PaneId.WORKER_OMP, worker)):
                cuts = sorted(rng.sample(range(1, len(data)), rng.randint(1, 8)))
                chunks.append([(pane, data[a:b]) for a, b in zip([0] + cuts, cuts + [len(data)])])
            seqs = {PaneId.MANAGER_OMP: 0, PaneId.WORKER_OMP: 0}
            outputs = []
            while chunks[0] or chunks[1]:
                side = chunks[rng.randrange(2)] or chunks[0] or chunks[1]
                pane, part = side.pop(0)
                seqs[pane] += 1
                model.enqueue_display(frame(pane, part, seq=seqs[pane]))
                if rng.random() < 0.5:
                    self.drain(model)
                    outputs.append(model.take_output())
            self.drain(model)
            outputs.append(model.take_output())
            copied = [c for out in outputs for c in clipboards(out)]
            with self.subTest(trial=trial):
                # each of the two live sequences is forwarded at most once and the outer clipboard ends on one of them
                self.assertLessEqual(copied.count(b"from manager"), 1, copied)
                self.assertLessEqual(copied.count(b"from worker"), 1, copied)
                self.assertTrue(copied, "no copy forwarded")
                self.assertTrue(set(copied) <= {b"from manager", b"from worker"}, copied)

    def test_each_live_sequence_is_forwarded_and_nothing_resends_it(self):
        model = self.make()
        model.on_display(frame(PaneId.MANAGER_OMP, omp_copy("one"), seq=1))
        self.assertEqual(expected_outer("one"), model.take_output())
        # later output, state pushes, snapshots, resizes, focus/zoom changes: never the same copy again
        model.on_display(frame(PaneId.MANAGER_OMP, b"more text\r\n", seq=2))
        model.apply_snapshot(snapshot())
        model.on_state(snapshot(focus="worker_omp"))
        model.feed_pending()
        self.assertEqual(b"", model.take_output())
        # the same bytes written again are a new copy
        model.on_display(frame(PaneId.MANAGER_OMP, omp_copy("one"), seq=3))
        self.assertEqual(expected_outer("one"), model.take_output())

    def test_two_writes_in_one_frame_leave_the_outer_clipboard_on_the_last(self):
        model = self.make()
        model.on_display(frame(PaneId.WORKER_OMP, omp_copy("first") + b"x" + omp_copy("second")))
        copied = clipboards(model.take_output())
        self.assertTrue(copied, "nothing forwarded")
        self.assertEqual(b"second", copied[-1])
        self.assertLessEqual(len(copied), 2)

    def test_a_hidden_omp_pane_still_forwards(self):
        model = self.make()
        model.on_state(snapshot(focus="host_shell"))
        model.on_display(frame(PaneId.WORKER_OMP, omp_copy("while unfocused")))
        self.assertEqual(expected_outer("while unfocused"), model.take_output())


class NeverForwardTests(Base):
    def test_host_shell_never_forwards_any_form_or_split(self):
        data = (b"$ " + omp_copy("host one") + tmux_wrap(omp_copy("host two")) + omp_copy("three", end=b"\x1b\\")
                + b"done")
        for cut in range(len(data) + 1):
            with self.subTest(cut=cut):
                model = self.make({"TMUX": "/tmp/t,1,0"})
                model.enqueue_display(frame(PaneId.HOST_SHELL, data[:cut], seq=1))
                model.enqueue_display(frame(PaneId.HOST_SHELL, data[cut:], seq=2))
                self.drain(model)
                self.assertEqual(b"", model.take_output())
                self.assertNotIn("복사", model.notice)
                self.assertEqual("$ done", model.panes[PaneId.HOST_SHELL].screen.display[0].rstrip())

    def test_clipboard_reads_are_never_forwarded(self):
        for body in (b"c;?", b";?", b"p;?", b"s;?", b"0;?", b"cp;?"):
            for wrap in (False, True):
                with self.subTest(body=body, tmux_wrapped=wrap):
                    seq = b"\x1b]52;" + body + b"\x07"
                    model = self.make({"TMUX": "/tmp/t,1,0"})
                    model.on_display(frame(PaneId.MANAGER_OMP, tmux_wrap(seq) if wrap else seq))
                    out = model.take_output()
                    self.assertEqual(b"", out)
                    self.assertNotIn(b"?", out)

    def test_invalid_base64_never_forwards_and_never_resends_the_previous_clipboard(self):
        model = self.make()
        model.on_display(frame(PaneId.WORKER_OMP, omp_copy("valid before"), seq=1))
        self.assertEqual(expected_outer("valid before"), model.take_output())
        for i, bad in enumerate((b"%%%%", b"QQ=Q", b"Q", b"QUJD\x80", b"QU JD", b"-_-_")):
            with self.subTest(bad=bad):
                model.on_display(frame(PaneId.WORKER_OMP, b"\x1b]52;c;" + bad + b"\x07ok", seq=2 + i))
                self.assertEqual(b"", model.take_output())

    def test_oversize_is_refused_whole_split_or_not_and_the_next_copy_works(self):
        for size in (MAX_COPY_BYTES + 1, 3 * MAX_COPY_BYTES):
            for split in (False, True):
                with self.subTest(size=size, split=split):
                    model = self.make()
                    data = b"<" + omp_copy(b"q" * size) + b">"
                    if split:
                        step = 65537
                        for n, at in enumerate(range(0, len(data), step)):
                            model.enqueue_display(frame(PaneId.MANAGER_OMP, data[at:at + step], seq=n + 1))
                            self.drain(model)  # fed as it arrives: no catch-up
                    else:
                        model.on_display(frame(PaneId.MANAGER_OMP, data))
                    self.assertEqual(b"", model.take_output())
                    self.assertIn("거부", model.notice)
                    self.assertEqual("<>", model.panes[PaneId.MANAGER_OMP].screen.display[0].rstrip())
                    model.on_display(frame(PaneId.MANAGER_OMP, omp_copy("small after"), seq=999))
                    self.assertEqual(expected_outer("small after"), model.take_output())

    def test_exactly_the_cap_is_forwarded(self):
        model = self.make()
        model.on_display(frame(PaneId.MANAGER_OMP, omp_copy(b"k" * MAX_COPY_BYTES)))
        self.assertEqual(expected_outer(b"k" * MAX_COPY_BYTES), model.take_output())

    def test_other_osc_on_omp_panes_never_produce_outer_output(self):
        data = (b"\x1b]0;title\x07\x1b]8;;https://e.invalid\x07link\x1b]8;;\x07"
                b"\x1b]777;notify;t;b\x07\x1b]9;n\x07\x1b]520;c;" + b64("x") + b"\x07"
                + tmux_wrap(b"\x1b]777;notify;t;b\x07") + b"end")
        for pane in OMP_PANES:
            with self.subTest(pane=pane.value):
                model = self.make({"TMUX": "/tmp/t,1,0"})
                model.on_display(frame(pane, data))
                self.assertEqual(b"", model.take_output())
                self.assertEqual("linkend", model.panes[pane].screen.display[0].rstrip())


class LiveOnlyTests(Base):
    def test_attach_replay_never_copies_in_either_intake_path(self):
        for pane in OMP_PANES:
            with self.subTest(pane=pane.value):
                model = self.make()
                model.on_display(frame(pane, omp_copy("replayed a"), replay=True))
                model.enqueue_display(frame(pane, tmux_wrap(omp_copy("replayed b")), replay=True, seq=2))
                self.drain(model)
                self.assertEqual(b"", model.take_output())

    def test_a_reattached_ui_does_not_copy_the_replayed_history_again(self):
        history = b"prompt> /copy\r\n" + omp_copy("copied before detach") + b"\r\nnext"
        first = self.make()
        first.on_display(frame(PaneId.MANAGER_OMP, history))
        self.assertEqual(expected_outer("copied before detach"), first.take_output())
        second = self.make()  # a new UI attaches: the backend replays its retained tail
        for at in range(0, len(history), 7):
            second.enqueue_display(frame(PaneId.MANAGER_OMP, history[at:at + 7], replay=True, seq=at + 1))
        self.drain(second)
        self.assertEqual(b"", second.take_output())
        second.enqueue_display(frame(PaneId.MANAGER_OMP, omp_copy("after reattach"), seq=10_000))
        self.drain(second)
        self.assertEqual(expected_outer("after reattach"), second.take_output())

    def test_every_replay_live_boundary_inside_a_sequence_never_copies(self):
        data = tmux_wrap(omp_copy("straddle"))
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                model = self.make()
                model.enqueue_display(frame(PaneId.WORKER_OMP, data[:cut], replay=True, seq=1))
                model.enqueue_display(frame(PaneId.WORKER_OMP, data[cut:], seq=2))
                self.drain(model)
                self.assertEqual(b"", model.take_output())

    def test_catch_up_never_copies_even_when_the_kept_tail_starts_inside_a_sequence(self):
        filler = b"".join(b"row %07d ................................\r\n" % i for i in range(1500))
        for offset in (0, 10, 5000, 60_000):
            with self.subTest(offset=offset):
                model = self.make()
                seq = 0
                while model.panes[PaneId.WORKER_OMP].backlog_bytes <= CATCHUP_BACKLOG_BYTES + 70_000:
                    seq += 1
                    model.enqueue_display(frame(PaneId.WORKER_OMP, omp_copy(f"old {seq}") + filler, seq=seq))
                big = omp_copy(b"t" * 90_000)  # longer than the kept tail: the cut lands inside it
                model.enqueue_display(frame(PaneId.WORKER_OMP, big[:len(big) - offset], seq=seq + 1))
                model.enqueue_display(frame(PaneId.WORKER_OMP, big[len(big) - offset:] + b"TAILEND", seq=seq + 2))
                self.drain(model)
                self.assertGreater(model.panes[PaneId.WORKER_OMP].skipped_bytes, 0, "no catch-up happened")
                self.assertEqual(b"", model.take_output())
                model.enqueue_display(frame(PaneId.WORKER_OMP, omp_copy("live again"), seq=seq + 3))
                self.drain(model)
                self.assertEqual(expected_outer("live again"), model.take_output())

    def test_a_restarted_session_never_completes_the_old_sessions_copy(self):
        data = omp_copy("old session copy")
        for cut in range(1, len(data)):
            for change in ({"gen": 2}, {"session": "s2"}):
                with self.subTest(cut=cut, change=change):
                    model = self.make()
                    model.on_display(frame(PaneId.MANAGER_OMP, data[:cut]))
                    model.on_display(frame(PaneId.MANAGER_OMP, data[cut:] + b"!", seq=1, **change))
                    self.assertEqual(b"", model.take_output())
                    model.on_display(frame(PaneId.MANAGER_OMP, omp_copy("new session copy"), seq=2, **change))
                    self.assertEqual(expected_outer("new session copy"), model.take_output())


# ---------------------------------------------------------------------------------------------------------------------
# The real curses UI on a PTY (outside tmux; TMUX set only in the UI's own environment, no tmux server is used).
from test_product_pty import UiProcess  # noqa: E402


class Ui(UiProcess):
    def __init__(self, sock_path, extra_env=None):
        real = subprocess.Popen

        def popen(argv, **kw):
            kw["env"] = {**kw["env"], **(extra_env or {})}
            return real(argv, **kw)

        with mock.patch.object(subprocess, "Popen", popen):
            super().__init__(sock_path, (30, 120))


class RealUiPtyTests(unittest.TestCase):
    def start(self, extra_env=None):
        server = FixtureServer(replay={"manager_omp": b"REPLAY " + omp_copy("replayed secret"),
                                       "worker_omp": tmux_wrap(omp_copy("replayed wrapped")),
                                       "host_shell": b"$ "})
        self.addCleanup(server.close)
        ui = Ui(server.path, extra_env)
        self.addCleanup(ui.close)
        self.assertTrue(server.wait_for(server.attached.is_set, timeout=15), "attach not received")
        self.assertTrue(ui.until(lambda: "REPLAY" in ui.text(), timeout=15), ui.text())
        ui.until(lambda: False, timeout=0.5)
        return server, ui

    def test_the_real_ui_writes_each_live_omp_copy_once_and_nothing_else(self):
        server, ui = self.start()
        self.assertNotIn(b"\x1b]52;", bytes(ui.raw), "a replayed copy reached the terminal")
        mark = len(ui.raw)
        data = b"w " + omp_copy("worker /copy 한글") + b" W"
        server.display("worker_omp", data[:5], seq=10)
        server.display("worker_omp", data[5:17], seq=11)
        server.display("worker_omp", data[17:], seq=12)
        self.assertTrue(ui.until(lambda: expected_outer("worker /copy 한글") in bytes(ui.raw[mark:]), timeout=10),
                        bytes(ui.raw[mark:])[-300:])
        server.display("manager_omp", tmux_wrap(omp_copy("manager wrapped")), seq=13)
        self.assertTrue(ui.until(lambda: expected_outer("manager wrapped") in bytes(ui.raw[mark:]), timeout=10))
        server.display("host_shell", omp_copy("host secret") + b"HOSTDONE", seq=14)
        server.display("worker_omp", b"\x1b]52;c;?\x07\x1b]52;c;!!\x07READDONE", seq=15)
        self.assertTrue(ui.until(lambda: "HOSTDONE" in ui.text() and "READDONE" in ui.text(), timeout=10), ui.text())
        ui.until(lambda: False, timeout=0.5)
        out = bytes(ui.raw[mark:])
        self.assertEqual(2, out.count(b"\x1b]52;"), out.count(b"\x1b]52;"))
        self.assertEqual(1, out.count(expected_outer("worker /copy 한글")))
        self.assertEqual(1, out.count(expected_outer("manager wrapped")))
        self.assertNotIn(b"\x1bPtmux;", out, "no passthrough copy outside tmux")
        self.assertNotIn(b64("host secret"), out)
        self.assertNotIn(b"52;c;?", out)

    def test_inside_tmux_the_ui_adds_the_passthrough_copy(self):
        server, ui = self.start({"TMUX": "/tmp/not-a-real-tmux-p27cd69,1,0"})
        mark = len(ui.raw)
        server.display("manager_omp", omp_copy("in tmux"), seq=10)
        self.assertTrue(ui.until(lambda: expected_outer("in tmux", tmux=True) in bytes(ui.raw[mark:]), timeout=10),
                        bytes(ui.raw[mark:])[-300:])
        ui.until(lambda: False, timeout=0.5)
        out = bytes(ui.raw[mark:])
        self.assertEqual(1, out.count(expected_outer("in tmux", tmux=True)))
        self.assertEqual(1, out.count(b"\x1bPtmux;"))


if __name__ == "__main__":
    unittest.main()
