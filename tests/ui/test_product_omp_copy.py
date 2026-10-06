"""C-D69 (1): OMP ``/copy`` inside a Workbench pane reaches the outer terminal clipboard.

OMP 18.6.1 ``/copy`` writes ``ESC ] 52 ; c ; <base64> BEL`` to its stdout (the pane PTY). The product UI forwards
OSC 52 clipboard WRITES of the manager/worker OMP panes through the drag-copy path (selection ``c``, plus the tmux
passthrough form inside tmux); never from the host shell pane, never reads (``?``), never invalid base64 or data
over MAX_COPY_BYTES, and only for live output (attach replay, catch-up tails and replaced sessions never copy).
"""
import base64
import os
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, FixtureServer, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product.model import CATCHUP_BACKLOG_BYTES, MAX_COPY_BYTES, ProductModel  # noqa: E402

SRC = str(Path(__file__).resolve().parents[2] / "src")
CODE = ("import sys; from pathlib import Path; from workbench.ui.product import run_product; "
        "raise SystemExit(run_product(Path(sys.argv[1])))")
OMP_PANES = (PaneId.MANAGER_OMP, PaneId.WORKER_OMP)


def b64(data):
    return base64.b64encode(data if isinstance(data, bytes) else data.encode())


def omp_copy(text):
    """The exact bytes OMP 18.6.1 Ol() writes: pb(`\\x1B]52;c;${Buffer.from(e).toString("base64")}\\x07`)."""
    return b"\x1b]52;c;" + b64(text) + b"\x07"


def outer(data, tmux=False):
    """What the drag-copy path writes for ``data`` (C-D62): selection c, plus the passthrough copy inside tmux."""
    plain = b"\x1b]52;c;" + b64(data) + b"\x07"
    return plain + (b"\x1bPtmux;" + plain.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\" if tmux else b"")


def frame(pane, data, *, replay=False, session="s", gen=1, seq=1):
    header = {"type": "display", "pane": pane.value, "session_id": session, "generation": gen, "sequence": seq}
    if replay:
        header["replay"] = True
    return ui_v1.Frame(header, data)


class OmpCopyModelTests(unittest.TestCase):
    def make(self, environ=None):
        model = ProductModel(FakeSender(), 30, 120, clock=lambda: 1000.0, environ={} if environ is None else environ)
        model.apply_snapshot(snapshot())
        return model

    def test_the_exact_omp_copy_bytes_of_manager_and_worker_reach_the_outer_terminal(self):
        text = "hello /copy 한글"
        for pane in OMP_PANES:
            with self.subTest(pane=pane):
                model = self.make()
                model.on_display(frame(pane, b"before" + omp_copy(text) + b"after"))
                self.assertEqual(outer(text), model.take_output())
                self.assertEqual(b"", model.take_output())  # once
                self.assertIn("복사됨: 14자", model.notice)
                self.assertEqual("beforeafter", model.panes[pane].screen.display[0].rstrip())

    def test_inside_tmux_the_same_passthrough_form_as_drag_copy(self):
        model = self.make({"TMUX": "/tmp/x,1,0"})
        model.on_display(frame(PaneId.WORKER_OMP, omp_copy("abc")))
        self.assertEqual(outer("abc", tmux=True), model.take_output())
        self.assertIn("set-clipboard on", model.notice)

    def test_tmux_wrapped_osc52_from_the_pane_is_forwarded(self):
        wrapped = b"\x1bPtmux;" + omp_copy("wrapped").replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
        model = self.make()
        model.on_display(frame(PaneId.MANAGER_OMP, b"a" + wrapped + b"b"))
        self.assertEqual(outer("wrapped"), model.take_output())

    def test_host_shell_pane_never_copies(self):
        wrapped = b"\x1bPtmux;" + omp_copy("x").replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
        model = self.make()
        model.on_display(frame(PaneId.HOST_SHELL, b"$ " + omp_copy("secret") + wrapped + b"ok"))
        self.assertEqual(b"", model.take_output())
        self.assertEqual("", model.notice)
        self.assertEqual("$ ok", model.panes[PaneId.HOST_SHELL].screen.display[0].rstrip())

    def test_selection_parameter_is_normalised_to_c(self):
        for pc in (b"p", b"s", b"", b"cs", b"0"):
            with self.subTest(pc=pc):
                model = self.make()
                model.on_display(frame(PaneId.MANAGER_OMP, b"\x1b]52;" + pc + b";" + b64("sel") + b"\x07"))
                self.assertEqual(outer("sel"), model.take_output())

    def test_reads_invalid_and_empty_are_ignored(self):
        for body in (b"c;?", b"c;not*base64!", b"c;QQ=Q", b"c;", b"c", b"x;" + b64("bad selection"),
                     b"c;" + b64("a") + b"\n"):
            with self.subTest(body=body):
                model = self.make()
                model.on_display(frame(PaneId.MANAGER_OMP, b"\x1b]52;" + body + b"\x07"))
                self.assertEqual(b"", model.take_output())
                self.assertNotIn("복사됨", model.notice)

    def test_unpadded_base64_is_accepted(self):
        model = self.make()
        model.on_display(frame(PaneId.MANAGER_OMP, b"\x1b]52;c;" + b64("ab").rstrip(b"=") + b"\x07"))
        self.assertEqual(outer("ab"), model.take_output())

    def test_size_cap_matches_drag_copy(self):
        model = self.make()
        model.on_display(frame(PaneId.MANAGER_OMP, omp_copy(b"x" * MAX_COPY_BYTES)))
        self.assertEqual(outer(b"x" * MAX_COPY_BYTES), model.take_output())
        for data in (b"x" * (MAX_COPY_BYTES + 1), b"x" * (MAX_COPY_BYTES * 2)):
            with self.subTest(size=len(data)):
                model = self.make()
                model.on_display(frame(PaneId.MANAGER_OMP, omp_copy(data) + b"after"))
                self.assertEqual(b"", model.take_output())  # refused, never cut into a wrong clipboard
                self.assertIn("복사 거부", model.notice)
                self.assertEqual("after", model.panes[PaneId.MANAGER_OMP].screen.display[0].rstrip())

    def test_split_across_live_frames_copies_once(self):
        data = b"x" + omp_copy("split") + b"y"
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                model = self.make()
                model.enqueue_display(frame(PaneId.MANAGER_OMP, data[:cut], seq=1))
                model.enqueue_display(frame(PaneId.MANAGER_OMP, data[cut:], seq=2))
                while model.feed_pending():
                    pass
                self.assertEqual(outer("split"), model.take_output())
                self.assertEqual(b"", model.take_output())

    def test_attach_replay_never_copies_and_live_output_after_it_does(self):
        model = self.make()
        model.on_display(frame(PaneId.MANAGER_OMP, omp_copy("old"), replay=True))
        model.enqueue_display(frame(PaneId.WORKER_OMP, omp_copy("old too"), replay=True))
        while model.feed_pending():
            pass
        self.assertEqual(b"", model.take_output())
        self.assertEqual("", model.notice)
        model.enqueue_display(frame(PaneId.WORKER_OMP, omp_copy("new"), seq=2))
        model.feed_pending()
        self.assertEqual(outer("new"), model.take_output())

    def test_an_osc52_begun_in_replay_and_ended_live_never_copies(self):
        data = omp_copy("straddles attach")
        for cut in (1, 2, 3, 5, len(data) - 1):
            with self.subTest(cut=cut):
                model = self.make()
                model.on_display(frame(PaneId.MANAGER_OMP, data[:cut], replay=True))
                model.on_display(frame(PaneId.MANAGER_OMP, data[cut:] + omp_copy("live"), seq=2))
                self.assertEqual(outer("live"), model.take_output())

    def test_catch_up_never_copies_dropped_or_tail_output(self):
        model = self.make()
        line = b"".join(b"line %06d\r\n" % i for i in range(2000))
        model.enqueue_display(frame(PaneId.MANAGER_OMP, omp_copy("dropped") + line, seq=0))
        for i in range(1, 30):  # > CATCHUP_BACKLOG_BYTES unfed
            model.enqueue_display(frame(PaneId.MANAGER_OMP, line, seq=i))
        model.enqueue_display(frame(PaneId.MANAGER_OMP, omp_copy("in the tail") + b"END\r\n", seq=99))
        self.assertGreater(model.panes[PaneId.MANAGER_OMP].backlog_bytes, CATCHUP_BACKLOG_BYTES)
        while model.feed_pending():
            pass
        self.assertGreater(model.panes[PaneId.MANAGER_OMP].skipped_bytes, 0)
        self.assertEqual(b"", model.take_output())
        model.enqueue_display(frame(PaneId.MANAGER_OMP, omp_copy("after catch-up"), seq=100))
        model.feed_pending()
        self.assertEqual(outer("after catch-up"), model.take_output())

    def test_a_replaced_session_drops_a_half_received_copy(self):
        data = omp_copy("old session")
        model = self.make()
        model.on_display(frame(PaneId.MANAGER_OMP, data[:12]))
        model.on_display(frame(PaneId.MANAGER_OMP, data[12:], gen=2))  # restarted: the rest is new-session text
        self.assertEqual(b"", model.take_output())

    def test_drag_copy_still_works(self):
        model = self.make()
        model.on_display(frame(PaneId.HOST_SHELL, b"hello world"))
        model._copy(b"hello", "복사됨: 5자")
        self.assertEqual(outer("hello"), model.take_output())


def tmux_wrap(inner):
    return b"\x1bPtmux;" + inner.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"


def shown(model, pane):
    """Every character the pane shows or keeps in its scrollback."""
    view = model.panes[pane]
    history = ["".join(line[x].data for x in sorted(line)) for line in view.screen.history.top]
    return "\n".join(history + list(view.screen.display))


def enqueue_chunks(model, pane, data, *, step=4096, seq=1):
    for n, at in enumerate(range(0, len(data), step)):
        model.enqueue_display(frame(pane, data[at:at + step], seq=seq + n))
    return seq + -(-len(data) // step)


def drain(model):
    while model.feed_pending():
        pass


class LargeOmpCopyTests(unittest.TestCase):
    """p27-cd69-review-01 P2-2: a /copy body larger than the catch-up threshold is still delivered (1 MiB cap);
    a catch-up never draws an OSC 52 body as base64; beyond the cap: no write, a notice, no base64."""

    def make(self):
        model = ProductModel(FakeSender(), 30, 120, clock=lambda: 1000.0, environ={})
        model.apply_snapshot(snapshot())
        return model

    def test_a_copy_larger_than_the_catch_up_threshold_is_delivered_without_catch_up(self):
        for size in (300_000, MAX_COPY_BYTES):
            for wrap in (False, True):
                with self.subTest(size=size, wrap=wrap):
                    text = (b"0123456789abcdef" * (size // 16 + 1))[:size]
                    seq_ = omp_copy(text)
                    self.assertGreater(len(seq_), CATCHUP_BACKLOG_BYTES)
                    model = self.make()
                    data = b"before\r\n" + (tmux_wrap(seq_) if wrap else seq_) + b"after"
                    enqueue_chunks(model, PaneId.WORKER_OMP, data)  # all queued before any feed: worst case
                    drain(model)
                    self.assertEqual(outer(text), model.take_output())
                    view = model.panes[PaneId.WORKER_OMP]
                    self.assertEqual(0, view.skipped_bytes, model.notice)
                    self.assertNotIn("따라잡음", model.notice)
                    self.assertEqual(["before", "after"], [line.rstrip() for line in view.screen.display[:2]])
                    self.assertFalse("MDEy" in shown(model, PaneId.WORKER_OMP), "base64 drawn")

    def test_a_split_copy_prefix_is_still_found_on_receipt(self):
        text = b"z" * 300_000
        for wrap in (False, True):
            seq_ = tmux_wrap(omp_copy(text)) if wrap else omp_copy(text)
            data = b"ab" + seq_ + b"cd"
            for cut in range(1, 18):
                with self.subTest(wrap=wrap, cut=cut):
                    model = self.make()
                    model.enqueue_display(frame(PaneId.MANAGER_OMP, data[:cut], seq=1))
                    for n, at in enumerate(range(cut, len(data), 3)):
                        if at > 40:
                            enqueue_chunks(model, PaneId.MANAGER_OMP, data[at:], seq=n + 2, step=8192)
                            break
                        model.enqueue_display(frame(PaneId.MANAGER_OMP, data[at:at + 3], seq=n + 2))
                    self.assertLessEqual(model.panes[PaneId.MANAGER_OMP].backlog_bytes, 64, "body counted")
                    drain(model)
                    self.assertEqual(outer(text), model.take_output())
                    self.assertEqual("abcd", model.panes[PaneId.MANAGER_OMP].screen.display[0].rstrip())

    def test_beyond_the_cap_nothing_is_written_a_notice_is_shown_and_no_base64_is_drawn(self):
        for size in (MAX_COPY_BYTES + 1, 3 * MAX_COPY_BYTES):
            with self.subTest(size=size):
                model = self.make()
                data = b"<" + omp_copy(b"q" * size) + b">"
                enqueue_chunks(model, PaneId.MANAGER_OMP, data, step=65536)
                drain(model)
                self.assertEqual(b"", model.take_output())
                self.assertIn("복사 거부", model.notice)
                self.assertFalse("cXFx" in shown(model, PaneId.MANAGER_OMP), "base64 drawn")
                self.assertEqual("<>", model.panes[PaneId.MANAGER_OMP].screen.display[0].rstrip())

    def test_a_catch_up_tail_starting_inside_a_copy_never_draws_base64(self):
        filler = b"".join(b"row %07d ................................\r\n" % i for i in range(1500))
        big = omp_copy(b"t" * 90_000)
        forms = {"bel": big, "st": big[:-1] + b"\x1b\\", "tmux": tmux_wrap(big)}
        for name, body in forms.items():
            for cut in (1, 30_000, 60_000):
                with self.subTest(form=name, cut=cut):
                    model = self.make()
                    seq_ = enqueue_chunks(model, PaneId.WORKER_OMP, filler * 6, step=65536)
                    # the kept 64 KiB tail starts inside the body
                    model.enqueue_display(frame(PaneId.WORKER_OMP, body[:len(body) - cut], seq=seq_))
                    model.enqueue_display(frame(PaneId.WORKER_OMP, body[len(body) - cut:] + b"TAILEND", seq=seq_ + 1))
                    drain(model)
                    view = model.panes[PaneId.WORKER_OMP]
                    self.assertGreater(view.skipped_bytes, 0, "no catch-up happened")
                    self.assertEqual(b"", model.take_output())
                    self.assertFalse("dHR0" in shown(model, PaneId.WORKER_OMP), "base64 drawn")
                    self.assertTrue("TAILEND" in shown(model, PaneId.WORKER_OMP), "output after the copy is shown")
                    self.assertIn("복사", model.notice)

    def test_a_copy_cut_by_catch_up_and_continued_live_never_draws_base64(self):
        filler = b"".join(b"row %07d ................................\r\n" % i for i in range(1500))
        big = omp_copy(b"t" * 200_000)
        model = self.make()
        seq_ = enqueue_chunks(model, PaneId.WORKER_OMP, filler * 6 + big[:100_000], step=65536)
        model.feed_pending()  # catch-up: the stream starts over inside the body
        self.assertGreater(model.panes[PaneId.WORKER_OMP].skipped_bytes, 0)
        enqueue_chunks(model, PaneId.WORKER_OMP, big[100_000:] + b"END", seq=seq_)
        drain(model)
        self.assertEqual(b"", model.take_output())
        self.assertFalse("dHR0" in shown(model, PaneId.WORKER_OMP), "base64 drawn")
        self.assertTrue("END" in shown(model, PaneId.WORKER_OMP), "output after the copy is shown")
        model.enqueue_display(frame(PaneId.WORKER_OMP, omp_copy("next"), seq=10_000))
        drain(model)
        self.assertEqual(outer("next"), model.take_output())

    def test_repeated_huge_bodies_cannot_pile_up_outside_the_catch_up_accounting(self):
        # p27-cd69-review-02 P3: ESC]52;c; + 8 MiB + BEL repeated faster than fed must not grow without bound
        from workbench.ui.product.model import CLIP_BACKLOG_MAX
        from workbench.terminal.vt_g1.screen import STRING_MAX
        self.assertLessEqual(CLIP_BACKLOG_MAX, 2 * STRING_MAX)
        model = self.make()
        view = model.panes[PaneId.WORKER_OMP]
        huge = b"\x1b]52;c;" + b"A" * (STRING_MAX - 16) + b"\x07"
        seq_ = 1
        for _ in range(4):
            seq_ = enqueue_chunks(model, PaneId.WORKER_OMP, huge, step=1 << 20, seq=seq_)
            self.assertLessEqual(view.clip_bytes, CLIP_BACKLOG_MAX)
        self.assertGreater(view.backlog_bytes, CATCHUP_BACKLOG_BYTES, "the excess counts toward catch-up")
        model.feed_pending()
        self.assertGreater(view.skipped_bytes, 0, "no catch-up")
        self.assertLessEqual(sum(len(item[2]) for item in view.backlog), 64 * 1024)
        self.assertIn("복사 건너뜀", model.notice)
        drain(model)
        self.assertEqual(b"", model.take_output())
        self.assertFalse("AAAA" in shown(model, PaneId.WORKER_OMP), "body drawn")
        model.enqueue_display(frame(PaneId.WORKER_OMP, omp_copy("next"), seq=seq_))
        drain(model)
        self.assertEqual(outer("next"), model.take_output())

    def test_every_copy_of_one_feed_is_queued_in_order(self):
        # p27-cd69-test-01 P3: two writes in one feed were overwritten (clipboard history tools missed the first)
        model = self.make()
        model.on_display(frame(PaneId.WORKER_OMP, omp_copy("first") + b"x" + omp_copy("second")))
        self.assertEqual(outer("first") + outer("second"), model.take_output())
        model.enqueue_display(frame(PaneId.MANAGER_OMP, omp_copy("a"), seq=1))
        model.enqueue_display(frame(PaneId.WORKER_OMP, omp_copy("b"), seq=2))
        drain(model)
        self.assertEqual(outer("a") + outer("b"), model.take_output())


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class OmpCopyTmuxPtyTests(unittest.TestCase):
    """The real product UI inside a private ``tmux -L`` server (own socket dir, own conf, own client PTY).

    The fixture server plays the backend; the manager/worker panes receive the exact OMP ``/copy`` bytes as live
    display frames. tmux is the UI's outer terminal: ``set-clipboard on`` puts a received OSC 52 into its buffer,
    ``allow-passthrough on`` hands the passthrough copy to its client, whose PTY this test reads.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wbcd69-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.sock = f"wbcd69-{secrets.token_hex(4)}"
        conf = self.root / "tmux.conf"
        conf.write_text("set -g status off\nset -s set-clipboard on\nset -g allow-passthrough on\n"
                        "set -s escape-time 0\n")
        self.env = {"PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "LANG": "C.UTF-8", "HOME": str(self.root),
                    "TMUX_TMPDIR": str(self.root)}
        self.base = ["tmux", "-L", self.sock, "-f", str(conf)]
        self.server_pid = None
        self.client = None
        self.addCleanup(self.stop_tmux)

    def tmux(self, *args):
        return subprocess.run(self.base + list(args), env=self.env, capture_output=True, text=True, timeout=20)

    def stop_tmux(self):
        if self.client is not None:
            proc, fd = self.client
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)  # our own client, in its own session
            proc.wait(5)
            os.close(fd)
        self.tmux("kill-server")
        if self.server_pid is not None:  # only the private server we started
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and Path(f"/proc/{self.server_pid}").exists():
                time.sleep(0.05)
            if Path(f"/proc/{self.server_pid}").exists():
                os.kill(self.server_pid, signal.SIGKILL)

    def read_client(self, raw, timeout=0.05):
        fd = self.client[1]
        try:
            while select.select([fd], [], [], timeout)[0]:
                data = os.read(fd, 65536)
                if not data:
                    break
                raw.extend(data)
                timeout = 0
        except OSError:
            pass

    def until(self, predicate, raw, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read_client(raw)
            if predicate():
                return True
        return False

    def buffer(self):
        result = self.tmux("show-buffer")
        return result.stdout if result.returncode == 0 else None

    def test_omp_copy_reaches_tmux_and_its_client_only_from_omp_panes_and_only_live(self):
        server = FixtureServer(replay={"manager_omp": b"REPLAYED " + omp_copy("replayed copy"),
                                       "host_shell": b"$ "})
        self.addCleanup(server.close)
        pane_out = self.root / "pane.out"
        command = (f"env PYTHONPATH={SRC} PYTHONDONTWRITEBYTECODE=1 {sys.executable} -c '{CODE}' {server.path}")
        subprocess.run(self.base + ["new-session", "-d", "-s", "wb", "-x", "120", "-y", "30", command],
                       env=self.env, check=True, timeout=20, cwd=self.root)
        self.server_pid = int(self.tmux("display", "-p", "#{pid}").stdout)
        self.assertEqual(0, self.tmux("pipe-pane", "-t", "wb", f"cat >> {pane_out}").returncode)
        master, slave = os.openpty()
        import fcntl, struct, termios
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        proc = subprocess.Popen(self.base + ["attach", "-t", "wb"], stdin=slave, stdout=slave, stderr=slave,
                                env=self.env, start_new_session=True, close_fds=True)
        os.close(slave)
        self.client = (proc, master)
        raw = bytearray()
        self.assertTrue(server.wait_for(server.attached.is_set, timeout=15), "attach not received")
        self.assertTrue(self.until(lambda: "MANAGER OMP" in self.tmux("capture-pane", "-p", "-t", "wb").stdout
                                   and "REPLAYED" in self.tmux("capture-pane", "-p", "-t", "wb").stdout, raw),
                        self.tmux("capture-pane", "-p", "-t", "wb").stdout)
        self.read_client(raw, 0.5)
        self.assertIsNone(self.buffer())  # the replayed copy was not forwarded
        self.assertNotIn(b64("replayed copy"), bytes(raw))

        for seq, (pane, text) in enumerate((("manager_omp", "매니저 /copy 결과"), ("worker_omp", "worker copy 2"))):
            with self.subTest(pane=pane):
                mark = len(raw)
                server.display(pane, b"before " + omp_copy(text) + b" after", seq=10 + seq)
                self.assertTrue(self.until(lambda: self.buffer() == text, raw), self.buffer())
                self.assertTrue(self.until(lambda: omp_copy(text) in bytes(raw[mark:]), raw),
                                bytes(raw[mark:])[-200:])
        # the UI itself wrote the drag-copy form, plain + tmux passthrough (TMUX is set inside tmux)
        written = pane_out.read_bytes()
        for text in ("매니저 /copy 결과", "worker copy 2"):
            self.assertEqual(1, written.count(outer(text, tmux=True)), written[-400:])
        self.assertNotIn(b64("replayed copy"), written)

        mark = len(raw)
        server.display("host_shell", b"$ " + omp_copy("host secret"), seq=20)
        server.display("worker_omp", b"\x1b]52;c;?\x07read", seq=21)
        server.display("worker_omp", b"\x1b]52;c;@@@@\x07bad", seq=22)
        self.assertTrue(self.until(lambda: "bad" in self.tmux("capture-pane", "-p", "-t", "wb").stdout, raw))
        self.read_client(raw, 0.5)
        self.assertEqual("worker copy 2", self.buffer())
        self.assertNotIn(b"\x1b]52;", bytes(raw[mark:]))
        written = pane_out.read_bytes()
        self.assertNotIn(b64("host secret"), written)
        self.assertNotIn(b"52;c;?", written)
        self.assertEqual(2, written.count(b"\x1bPtmux;"))  # only the two forwarded copies


if __name__ == "__main__":
    unittest.main()
