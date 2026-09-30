"""Independent CW-06 re-verification (p27-cw06-rerun-test-03): flood, connection loss, terminal restore, input framing.

Expectations come from the review finding p27-review-cw06-01 and root-adjudication-p27-review-cw06-01
(P2-1 flood latency / catch-up, P2-1c connection loss, P3-1..P3-4), not from the worker's tests.
Runs the real product UI loop on a PTY this test owns against a scripted ui_v1 fixture server; no OMP, no model.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import socket
import struct
import tempfile
import threading
import time
import unittest

from independent_support_cw06 import ScriptedServer, UiPty, modes_restored, snap
from workbench.ui.product.input import PREFIX
from workbench.ui.product.model import ProductModel
from independent_support_cw06 import RecordingSender

START, END = b"\x1b[200~", b"\x1b[201~"
P = bytes([PREFIX])
ENABLE_2004 = b"\x1b[?2004h"
FLOOD_BYTES = 8 * 1024 * 1024


def flood_chunks(total: int, tail: bytes):
    """Coloured, Korean-mixed lines; the last chunk ends with ``tail``."""
    n, sent = 0, 0
    while sent < total:
        lines = []
        size = 0
        while size < 32768:
            n += 1
            line = (f"\x1b[3{n % 7 + 1}m행 {n:07d} 한글 플러드 line\x1b[0m\r\n".encode()
                    if n % 2 else f"plain flood line {n:07d} xxxxxxxxxxxxxxxxxxxxxxxxxxxx\r\n".encode())
            lines.append(line)
            size += len(line)
        chunk = b"".join(lines)
        sent += len(chunk)
        yield chunk
    yield tail


class Harness:
    def __init__(self, *, focus="host_shell", replay=None, answers=None):
        self.server = ScriptedServer(snapshot=snap(focus=focus), replay=replay or {"host_shell": ENABLE_2004 + b"$ "},
                                     answers=answers)
        self.work = Path(tempfile.mkdtemp(prefix="cw06-p27f-", dir="/tmp"))
        self.ui = UiPty(self.server.path, self.work, rows=30, cols=120)

    def start(self):
        assert self.server.attached.wait(10), self.ui.screen_text()
        assert self.ui.wait_text("focus:", 10), self.ui.screen_text()  # first frame drawn: the loop is running
        self.ui.drain(0.3)
        return self

    def close(self):
        self.ui.close()
        self.server.close()
        shutil.rmtree(self.work, ignore_errors=True)


class FloodLatencyTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness().start()
        self.addCleanup(self.h.close)

    def test_8mib_flood_key_latency_under_300ms_catchup_visible_and_tail_shown(self):
        server, ui = self.h.server, self.h.ui
        stop = threading.Event()
        state = {"done": False, "sent": 0}

        def pump():
            for chunk in flood_chunks(FLOOD_BYTES, b"\r\nTAILMARK-END-OF-FLOOD\r\n$ "):
                if stop.is_set():
                    return
                server.display("host_shell", chunk)
                state["sent"] += len(chunk)
            state["done"] = True

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(15)))
        latencies = []
        for i in range(3):
            self.assertTrue(ui.wait_for(lambda: state["sent"] > (i + 1) * 512 * 1024, 20), "flood did not progress")
            self.assertFalse(state["done"], "flood ended before the key was sent (vacuous)")
            before = len(server.payloads("input", "host_shell"))
            marker = f"k{i}".encode()
            t0 = time.monotonic()
            ui.send(marker)
            ok = ui.wait_for(lambda: server.payloads("input", "host_shell")[before:].startswith(marker), 5)
            latencies.append(time.monotonic() - t0)
            self.assertTrue(ok, "key never reached the backend during the flood")
            self.assertFalse(state["done"], "flood finished before the key arrived (latency not measured under flood)")
        self.assertLess(max(latencies), 0.3, f"key latency under flood: {latencies}")
        thread.join(60)
        self.assertTrue(state["done"], "fixture flood did not finish (UI stopped reading?)")
        self.assertTrue(ui.wait_for(lambda: "TAILMARK-END-OF-FLOOD" in ui.screen_text(), 15), ui.screen_text())
        self.assertIn("따라잡음".encode(), bytes(ui.output), "catch-up indicator was never shown")
        self.assertNotIn(b"Traceback", bytes(ui.output))
        self.assertIsNone(ui.status(), "UI exited during flood")
        # still usable: a further key reaches the backend
        ui.send(b"z")
        self.assertTrue(ui.wait_for(lambda: server.payloads("input", "host_shell").endswith(b"z"), 3))


class ConnectionLossTests(unittest.TestCase):
    """P2-1c: EPIPE/ECONNRESET/backend-closed after a send shows a reason and exits cleanly (status 1, no traceback)."""

    def _finish(self, h, expect_reason=None):
        ui = h.ui
        self.assertTrue(ui.ui_done(15), ui.screen_text())
        ui.drain(0.2)
        out = bytes(ui.output)
        self.assertNotIn(b"Traceback", out)
        self.assertEqual(ui.status(), 1)
        self.assertTrue(modes_restored(out) == {"alt_screen": True, "bracketed_paste": True, "cursor_visible": True},
                        modes_restored(out))
        text = out.decode("utf-8", "replace")
        self.assertIn("[workbench]", text)
        tail = text[text.rindex("[workbench]"):]
        if expect_reason:
            self.assertIn(expect_reason, tail)
        return tail

    def test_closing_reason_then_drop_then_key(self):
        h = Harness().start()
        self.addCleanup(h.close)
        h.server.closing("slow_client")
        h.server.drop()
        h.ui.send(b"x")
        self._finish(h, "slow_client")

    def test_drop_without_closing_then_keys_is_clean_with_message(self):
        h = Harness().start()
        self.addCleanup(h.close)
        h.server.drop()
        for _ in range(5):
            h.ui.send(b"x")
            time.sleep(0.05)
        tail = self._finish(h)
        self.assertNotIn("Traceback", tail)

    def test_reset_by_peer_then_paste_is_clean(self):
        h = Harness().start()
        self.addCleanup(h.close)
        conn = h.server.conn
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close() sends RST
        h.server.closing("slow_client")
        conn.close()
        for _ in range(3):
            h.ui.send(START + b"paste" * 100 + END)
            time.sleep(0.05)
        self._finish(h)


class TerminalRestoreTests(unittest.TestCase):
    """P3-2: SIGTERM and SIGHUP on the UI process restore 2004l / rmcup / termios."""

    def _kill_and_check(self, sig):
        h = Harness().start()
        self.addCleanup(h.close)
        ui = h.ui
        ui.wait_for(lambda: b"\x1b[?2004h" in bytes(ui.output) and b"\x1b[?1049h" in bytes(ui.output), 5)
        self.assertIn(b"\x1b[?2004h", bytes(ui.output))
        pid = ui.ui_pid()
        self.assertIsNotNone(pid)
        os.kill(pid, sig)
        self.assertTrue(ui.ui_done(10), "UI did not exit after the signal")
        ui.drain(0.2)
        out = bytes(ui.output)
        self.assertNotIn(b"Traceback", out)
        self.assertTrue(all(modes_restored(out).values()), modes_restored(out))
        self.assertGreater(out.rfind(b"\x1b[?2004l"), out.rfind(b"\x1b[?2004h"))
        self.assertGreater(out.rfind(b"\x1b[?1049l"), out.rfind(b"\x1b[?1049h"))
        before, after = h.ui.before_file.read_text().strip(), h.ui.after_file.read_text().strip()
        self.assertTrue(before and before == after, f"termios not restored: {before!r} vs {after!r}")

    def test_sigterm_restores_terminal(self):
        self._kill_and_check(signal.SIGTERM)

    def test_sighup_restores_terminal(self):
        self._kill_and_check(signal.SIGHUP)


def last_cursor_state(out: bytes):
    """True/False for the last cnorm/civis-style show/hide in ``out`` (None if none)."""
    found = [(m.start(), m.group(1) == b"h") for m in re.finditer(rb"\x1b\[\?25([hl])", out)]
    return found[-1][1] if found else None


class CursorAfterHelpTests(unittest.TestCase):
    def test_cursor_visible_on_focused_pane_and_after_help_closed(self):
        h = Harness().start()
        self.addCleanup(h.close)
        ui = h.ui
        ui.drain(0.3)
        self.assertIsNot(last_cursor_state(bytes(ui.output)), False, "cursor hidden with a live focused pane")
        for closer in (b"q", b" ", b"\x1b"):  # any key closes the overlay (Esc is a held key: released by the flush timeout)
            ui.send(P + b"?")
            self.assertTrue(ui.wait_for(lambda: last_cursor_state(bytes(ui.output)) is False, 3), "help did not open")
            ui.drain(0.3)
            ui.send(closer)
            restored = ui.wait_for(lambda: last_cursor_state(bytes(ui.output)) is True, 1.0)
            self.assertTrue(restored, f"cursor still hidden 1 s after help closed by {closer!r}")
        # and the UI is still driving input
        before = len(h.server.payloads("input", "host_shell"))
        ui.send(b"k")
        self.assertTrue(h.server.wait(lambda: len(h.server.payloads("input", "host_shell")) > before, 3))


class HelpEscRedrawTests(unittest.TestCase):
    """P3-1 residual: closing help with a (held) lone Esc must redraw/restore the cursor promptly.

    Fixed pacing (no observation waits) as a typist would: prefix ?, look for a moment, Esc.
    """

    def test_esc_closes_help_and_cursor_restored_within_1s(self):
        h = Harness().start()
        self.addCleanup(h.close)
        ui = h.ui
        ui.send(P + b"?")
        time.sleep(0.05)
        ui.drain(1.0)
        self.assertIs(last_cursor_state(bytes(ui.output)), False, "help overlay not shown (cursor not hidden)")
        ui.send(b"\x1b")
        t0 = time.monotonic()
        restored = ui.wait_for(lambda: last_cursor_state(bytes(ui.output)) is True, 1.0)
        self.assertTrue(restored, f"help overlay/hidden cursor still stale {time.monotonic() - t0:.2f}s after Esc")


class PrefixSequenceTests(unittest.TestCase):
    """P3-3: prefix followed by Up / F5 / Alt-x is consumed whole; nothing reaches any pane."""

    def test_prefix_then_up_f5_altx_sends_nothing_and_sentinel_follows(self):
        h = Harness().start()
        self.addCleanup(h.close)
        ui, server = h.ui, h.server
        base = {p: server.payloads("input", p) for p in ("manager_omp", "worker_omp", "host_shell")}
        for seq in (b"\x1b[A", b"\x1b[15~", b"\x1bx", b"\x1bOP", b"\x1b[1;5A"):
            ui.send(P + seq)
            time.sleep(0.12)  # longer than every hold timeout: nothing may leak out afterwards
            ui.drain(0.1)
        ui.send(b"Z")
        self.assertTrue(server.wait(lambda: server.payloads("input", "host_shell").endswith(b"Z"), 3))
        time.sleep(0.3)
        self.assertEqual(server.payloads("input", "host_shell"), base["host_shell"] + b"Z")
        self.assertEqual(server.payloads("input", "manager_omp"), base["manager_omp"])
        self.assertEqual(server.payloads("input", "worker_omp"), base["worker_omp"])
        self.assertEqual(server.of("paste"), [])

    def test_model_level_prefix_sequences(self):
        for seq in (b"\x1b[A", b"\x1b[15~", b"\x1bx", b"\x1bOQ", b"\x1b[3;5~", b"\x1b\xc3\xa9"):
            with self.subTest(seq=seq):
                sender = RecordingSender()
                model = ProductModel(sender, 40, 150, clock=lambda: 1000.0)
                model.handle_input(P + seq, now=0)
                model.flush_input(now=5)
                self.assertEqual(sender.payloads("input"), b"", seq)
                model.handle_input(b"Z", now=6)
                model.flush_input(now=7)
                self.assertEqual(sender.payloads("input"), b"Z")


class SplitPasteIntroducerTests(unittest.TestCase):
    """P3-4: 'ESC [ 2', 'ESC [ 2 0', 'ESC [ 2 0 0' arriving >50 ms before the rest still frame one paste."""

    BODY = "가나 abc\r\nline2".encode()

    def test_pty_split_introducer_gap_over_50ms(self):
        h = Harness().start()
        self.addCleanup(h.close)
        ui, server = h.ui, h.server
        frame = START + self.BODY + END
        for cut in (3, 4, 5):  # ESC [ 2 / ESC [ 2 0 / ESC [ 2 0 0
            before = len(server.of("paste"))
            inputs_before = server.payloads("input", "host_shell")
            ui.send(frame[:cut])
            ui.drain(0.2)  # > 50 ms
            time.sleep(0.1)
            ui.send(frame[cut:])
            self.assertTrue(server.wait(lambda: len(server.of("paste")) > before, 3), f"cut={cut}: no paste frame")
            time.sleep(0.3)
            pastes = server.of("paste")
            self.assertEqual(len(pastes) - before, 1, cut)
            self.assertEqual(pastes[-1].payload, frame, cut)
            self.assertEqual(server.payloads("input", "host_shell"), inputs_before, f"cut={cut}: leaked as keys")

    def test_model_level_gap_and_lone_esc_still_released(self):
        frame = START + self.BODY + END
        for cut in (3, 4, 5):
            with self.subTest(cut=cut):
                sender = RecordingSender()
                model = ProductModel(sender, 40, 150, clock=lambda: 1000.0)
                model.on_display(_display(b"\x1b[?2004h"))
                model.handle_input(frame[:cut], now=0.0)
                model.flush_input(now=0.2)  # >50 ms gap
                model.handle_input(frame[cut:], now=0.25)
                model.flush_input(now=5)
                self.assertEqual([p for k, _, p, _ in sender.sent if k == "paste"], [frame])
                self.assertEqual(sender.payloads("input"), b"")
        # a real lone Esc is still delivered eventually
        sender = RecordingSender()
        model = ProductModel(sender, 40, 150, clock=lambda: 1000.0)
        model.handle_input(b"\x1b", now=0.0)
        model.flush_input(now=2.0)
        self.assertEqual(sender.payloads("input"), b"\x1b")


def _display(data: bytes, pane="manager_omp"):
    from independent_support_cw06 import SID
    from workbench.contracts import ui_v1
    from workbench.contracts.v1 import DisplayChunk, PaneId
    raw = ui_v1.encode_display(DisplayChunk(session_id=SID, session_generation=1, pane_id=PaneId(pane), sequence=1,
                                            data=data))
    return ui_v1.FrameDecoder().feed(raw)[0]


if __name__ == "__main__":
    unittest.main()
