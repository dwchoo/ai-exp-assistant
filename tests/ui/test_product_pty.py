"""Run the real product UI (curses) on a PTY against a fixture ui_v1 server."""
import fcntl
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import pyte  # noqa: E402
from support import FixtureServer, OuterMouse, rep_screen_classes, snapshot  # noqa: E402

SRC = str(Path(__file__).resolve().parents[2] / "src")
CODE = ("import sys; from pathlib import Path; from workbench.ui.product import run_product; "
        "raise SystemExit(run_product(Path(sys.argv[1])))")
P = b"\x1d"
MOUSE_ON = (b"\x1b[?1000h", b"\x1b[?1002h", b"\x1b[?1006h")
MOUSE_OFF = (b"\x1b[?1000l", b"\x1b[?1002l", b"\x1b[?1006l")


def sgr(button, x, y, release=False):
    return b"\x1b[<%d;%d;%d%s" % (button, x, y, b"m" if release else b"M")


def assert_mouse_restored(test, raw):
    """Mouse reporting was enabled (SGR) and every exit path switched it off again, last."""
    for on, off in zip(MOUSE_ON, MOUSE_OFF):
        test.assertIn(on, raw)
        test.assertGreater(raw.rfind(off), raw.rfind(on), f"{off!r} missing after {on!r}")
    test.assertGreater(raw.rfind(MOUSE_OFF[0]), raw.rfind(b"\x1b[?2004h"))


class UiProcess:
    def __init__(self, sock_path, size=(30, 120)):
        master, slave = os.openpty()
        self.set_size(master, size)
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "TERM": "xterm-256color",
               "LANG": "C.UTF-8"}
        self.proc = subprocess.Popen([sys.executable, "-c", CODE, str(sock_path)], stdin=slave, stdout=slave,
                                     stderr=slave, env=env, start_new_session=True, close_fds=True)
        os.close(slave)
        self.fd = master
        screen_class, stream_class = rep_screen_classes()  # ncurses uses REP; plain pyte would miss it
        self.screen = screen_class(size[1], size[0])
        self.stream = stream_class(self.screen)
        self.raw = bytearray()

    @staticmethod
    def set_size(fd, size):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))

    def drain(self, timeout=0.05):
        try:
            if select.select([self.fd], [], [], timeout)[0]:
                data = os.read(self.fd, 65536)
                self.raw.extend(data)
                self.stream.feed(data)
        except OSError:
            pass

    def until(self, predicate, timeout=6.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return True
        return False

    def text(self):
        return "\n".join(self.screen.display)

    def wait_exit(self, timeout=6.0):
        deadline = time.monotonic() + timeout
        while self.proc.poll() is None and time.monotonic() < deadline:
            self.drain()
        self.drain(0.1)
        return self.proc.poll()

    def send(self, data):
        os.write(self.fd, data)

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)  # our own child in its own session
            self.proc.wait(5)
        os.close(self.fd)


class ProductPtyTests(unittest.TestCase):
    def start(self, **kwargs):
        server = FixtureServer(**kwargs)
        self.addCleanup(server.close)
        ui = UiProcess(server.path)
        self.addCleanup(ui.close)
        self.assertTrue(server.wait_for(server.attached.is_set), "attach not received")
        # keys sent before curses raw mode would hit the cooked tty (Ctrl-z -> SIGTSTP): wait for the first draw
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()), ui.text())
        return server, ui

    def test_renders_three_panes_focus_and_detach_restores_terminal(self):
        server, ui = self.start(replay={"manager_omp": b"MANAGER-REPLAY\r\n", "worker_omp": "워커 화면".encode(),
                                        "host_shell": b"user@host$ "})
        self.assertTrue(ui.until(lambda: "MANAGER-REPLAY" in ui.text() and "워커 화면" in ui.text()
                                 and "user@host$" in ui.text()), ui.text())
        text = ui.text()
        for needle in ("MANAGER OMP", "WORKER OMP", "HOST SHELL", "focus: MANAGER OMP", "host 입력 owner: user",
                       "마지막 확인"):
            self.assertIn(needle, text)
        attach = server.received("attach")[0].header
        self.assertIn("size", attach)
        # per-pane resize with real sizes, nothing unusual before user action
        self.assertGreaterEqual(len(server.received("resize")), 3)
        # focus switch
        ui.send(P + b"3")
        self.assertTrue(server.wait_for(lambda: server.received("focus")))
        self.assertEqual("host_shell", server.received("focus")[-1].header["pane"])
        self.assertTrue(ui.until(lambda: "focus: HOST SHELL" in ui.text()))
        self.assertIn("host 입력 owner: user", ui.text())
        # keys forwarded unchanged
        ui.send(b"ls\r")
        self.assertTrue(server.wait_for(lambda: any(f.payload == b"ls\r" for f in server.received("input"))))
        # help overlay
        ui.send(P + b"?")
        self.assertTrue(ui.until(lambda: "prefix" in ui.text() and "detach" in ui.text() and "임시" in ui.text()))
        ui.send(b" ")
        # detach
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())
        self.assertIn(b"detached; backend keeps running", bytes(ui.raw))
        self.assertTrue(server.wait_for(lambda: server.received("detach")))
        self.assertTrue(server.disconnected.wait(3))
        self.assertEqual([], [f for f in server.frames if str(f.header.get("type")).startswith("shutdown")])
        self.assertIn(b"\x1b[?1049l", bytes(ui.raw))  # alt screen left (endwin)
        self.assertIn(b"\x1b[?2004l", bytes(ui.raw))  # bracketed paste off
        assert_mouse_restored(self, bytes(ui.raw))
        attrs = termios.tcgetattr(ui.fd)  # slave cooked mode restored: ECHO + ICANON back on
        self.assertTrue(attrs[3] & termios.ICANON and attrs[3] & termios.ECHO)

    def test_paste_rejection_notice_and_korean_paste_whole(self):
        server, ui = self.start(reject={"paste": ("paste_too_large", "too big")})
        body = b"\x1b[200~" + "여러\n줄".encode() + b"\x1b[201~"
        ui.send(body)
        self.assertTrue(server.wait_for(lambda: server.received("paste")))
        self.assertEqual(body, server.received("paste")[0].payload)
        self.assertTrue(ui.until(lambda: "paste_too_large" in ui.text()), ui.text())
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())

    def test_resize_sends_new_per_pane_sizes(self):
        server, ui = self.start()
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()))  # curses loop (SIGWINCH handler) is up
        before = len(server.received("resize"))
        UiProcess.set_size(ui.fd, (40, 150))
        os.killpg(ui.proc.pid, signal.SIGWINCH)
        self.assertTrue(server.wait_for(lambda: len(server.received("resize")) >= before + 3))
        latest = {f.header["pane"]: (f.header["rows"], f.header["cols"]) for f in server.received("resize")[-3:]}
        self.assertEqual({"manager_omp": (17, 73), "worker_omp": (17, 73), "host_shell": (16, 148)}, latest)
        ui.send(P + b"q")
        ui.wait_exit()

    def test_backend_close_shows_notice_and_exits_nonzero(self):
        server, ui = self.start()
        server.closing("backend_shutdown")
        self.assertEqual(1, ui.wait_exit())
        self.assertIn(b"backend_shutdown", bytes(ui.raw))
        self.assertIn(b"\x1b[?1049l", bytes(ui.raw))
        assert_mouse_restored(self, bytes(ui.raw))

    def test_connection_lost_exits_nonzero_and_restores(self):
        server, ui = self.start()
        server.conn.close()
        self.assertEqual(1, ui.wait_exit())
        self.assertIn(b"\x1b[?2004l", bytes(ui.raw))
        assert_mouse_restored(self, bytes(ui.raw))

    def test_owner_and_state_push_update_status(self):
        server, ui = self.start()
        server.state(snapshot(owner="manager", mode="manager_control", automation="running", phase="ready"))
        self.assertTrue(ui.until(lambda: "host 입력 owner: manager" in ui.text() and "자동화: running" in ui.text()),
                        ui.text())
        self.assertIn("focus: MANAGER OMP", ui.text())
        ui.send(P + b"q")
        ui.wait_exit()

    def test_no_tty_returns_2(self):
        server = FixtureServer()
        self.addCleanup(server.close)
        result = subprocess.run([sys.executable, "-c", CODE, str(server.path)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, env={"PYTHONPATH": SRC, "PATH": "/usr/bin"})
        self.assertEqual(2, result.returncode)
        self.assertIn("status --json", result.stderr)

    # -- P2-1: high-volume output ------------------------------------------------
    def flood(self, server, total, pane="host_shell", chunk_size=32768, pause=0.008):
        line = b"\x1b[31mline\x1b[0m " + b"x" * 60 + b"\r\n"
        chunk = line * (chunk_size // len(line))
        marker = b"FLOOD-TAIL-MARKER\r\n"
        result = {}

        def run():
            start, sent = time.monotonic(), 0
            while sent < total:
                server.display(pane, chunk, seq=10 + sent // chunk_size)
                sent += len(chunk)
                time.sleep(pause)
            server.display(pane, marker, seq=10_000_000)
            result["seconds"] = time.monotonic() - start
            result["sent"] = sent

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result

    def test_last_fed_chunk_is_drawn_promptly_without_further_input(self):
        """Output that stops while the backlog is being fed (draws throttled) must still reach the screen."""
        server, ui = self.start()
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()), ui.text())
        line = b"burst " + b"y" * 60 + b"\r\n"
        for index in range(48):  # ~200 KiB: several throttled feed slices, then the final marker chunk
            server.display("manager_omp", line * 64, seq=10 + index)
        server.display("manager_omp", b"BURST-LAST-MARK", seq=1000)
        started = time.monotonic()
        self.assertTrue(ui.until(lambda: "BURST-LAST-MARK" in ui.text(), 3.0), ui.text())
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIsNone(ui.proc.poll())

    def test_flood_keeps_keys_responsive_drains_socket_and_shows_tail(self):
        server, ui = self.start()
        ui.send(P + b"3")
        server.wait_for(lambda: server.received("focus"))
        server.frames.clear()
        server.conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)  # bounded outbound buffer
        thread, result = self.flood(server, 8 * 1024 * 1024)
        latencies = []
        try:
            for index in range(5):
                ui.until(lambda: False, 0.4)  # keep reading the PTY like a terminal emulator does
                key = bytes([ord("a") + index])
                started = time.monotonic()
                ui.send(key)
                self.assertTrue(ui.until(lambda: any(f.payload == key for f in server.received("input")), 3.0),
                                f"key {key!r} not delivered during flood")
                latencies.append(time.monotonic() - started)
        finally:
            ui.until(lambda: not thread.is_alive(), 30)
            thread.join(1)
        print(f"\nflood key latencies (s): {[round(x, 3) for x in latencies]} max={max(latencies):.3f}; "
              f"server flood send {result.get('seconds', -1):.2f}s")
        self.assertFalse(thread.is_alive(), "server outbound stalled: the UI did not drain the socket")
        self.assertLess(max(latencies), 0.3)
        self.assertGreater(result.get("seconds", 0), 1.5, "flood finished before the keys were typed")
        self.assertTrue(ui.until(lambda: "FLOOD-TAIL-MARKER" in ui.text(), 15.0), ui.text())
        self.assertTrue(ui.until(lambda: "따라잡음" in ui.text(), 5.0), ui.text())
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())

    def test_sustained_yes_flood_intake_keeps_up_and_ctrl_c_is_prompt(self):
        """Worst pyte content (`yes`), produced as fast as the UI accepts it for 4 s through a bounded socket
        buffer: the UI must drain far faster than it can render (intake is not starved by feeding/drawing)
        and a mid-flood Ctrl-C reaches the backend within 1 s."""
        server, ui = self.start()
        ui.send(P + b"3")
        server.wait_for(lambda: server.received("focus"))
        server.frames.clear()
        server.conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        chunk = b"y\r\n" * 21845  # 64 KiB, like one backend PTY read
        stop, result = threading.Event(), {"sent": 0}

        def run():
            start, seq = time.monotonic(), 10
            while not stop.is_set() and time.monotonic() - start < 4.0:
                server.display("host_shell", chunk, seq=seq)  # blocking sendall: paced by the UI's intake
                seq += 1
                result["sent"] += len(chunk)
            result["seconds"] = time.monotonic() - start

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            ui.until(lambda: False, 2.0)
            started = time.monotonic()
            ui.send(b"\x03")
            delivered = ui.until(lambda: any(f.payload == b"\x03" for f in server.received("input")), 3.0)
            ctrl_c = time.monotonic() - started
            ui.until(lambda: not thread.is_alive(), 10)
        finally:
            stop.set()
            thread.join(5)
        rate = result["sent"] / max(result.get("seconds", 1), 1e-6)
        print(f"\nsustained yes flood: UI intake {rate / 1e6:.1f} MB/s over {result.get('seconds', -1):.1f}s; "
              f"Ctrl-C {ctrl_c:.3f}s")
        self.assertTrue(delivered, "Ctrl-C not delivered during the flood")
        self.assertLess(ctrl_c, 1.0)
        self.assertGreater(rate, 20e6, "UI socket intake starved (backend would detach it as slow_client)")
        self.assertIsNone(ui.proc.poll())
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit(10))

    def test_send_failure_shows_reason_and_no_traceback(self):
        server, ui = self.start()
        server.closing("slow_client")
        server.conn.shutdown(2)
        ui.send(b"typing after the backend dropped us")
        self.assertEqual(1, ui.wait_exit())
        raw = bytes(ui.raw)
        self.assertIn(b"slow_client", raw)
        self.assertNotIn(b"Traceback", raw)
        self.assertNotIn(b"BrokenPipe", raw)
        self.assertIn(b"\x1b[?2004l", raw)
        assert_mouse_restored(self, raw)

    # -- P3-1: cursor ------------------------------------------------------------
    def test_cursor_visible_again_after_help_closes(self):
        server, ui = self.start()
        ui.send(P + b"?")
        self.assertTrue(ui.until(lambda: "임시" in ui.text()))
        ui.send(b" ")
        self.assertTrue(ui.until(lambda: "임시" not in ui.text()))
        ui.drain(0.3)
        raw = bytes(ui.raw)
        self.assertGreater(raw.rfind(b"\x1b[?25h"), raw.rfind(b"\x1b[?25l"), "cursor left hidden after help")
        ui.send(P + b"q")
        ui.wait_exit()

    # -- C-D58: layout and host shell scrollback -----------------------------------------------------------
    def test_layout_omp_panes_side_by_side_on_top_host_full_width_below(self):
        server, ui = self.start(replay={"manager_omp": b"MGR-TEXT", "worker_omp": b"WRK-TEXT",
                                        "host_shell": b"user@host$ "})
        self.assertTrue(ui.until(lambda: "MGR-TEXT" in ui.text() and "WRK-TEXT" in ui.text()
                                 and "user@host$" in ui.text()), ui.text())
        rows = ui.screen.display
        row_of = {name: next(i for i, line in enumerate(rows) if name in line)
                  for name in ("MANAGER OMP *FOCUS*", "WORKER OMP", "HOST SHELL", "MGR-TEXT", "WRK-TEXT", "user@host$")}
        self.assertEqual(row_of["MANAGER OMP *FOCUS*"], row_of["WORKER OMP"])
        self.assertEqual(row_of["MGR-TEXT"], row_of["WRK-TEXT"])
        self.assertLess(row_of["MGR-TEXT"], row_of["HOST SHELL"])
        self.assertLess(row_of["HOST SHELL"], row_of["user@host$"])
        top_line = rows[row_of["WORKER OMP"]]
        self.assertEqual(60, top_line.index("WORKER OMP") - 3)  # worker box starts at half of 120 columns (corner + space)
        # host frame spans the whole width: its bottom border is one unbroken run right above the footer
        bottom_border = next(line for line in reversed(rows[:-1]) if line.strip())
        self.assertEqual(120, len(bottom_border.rstrip()))
        sizes = {f.header["pane"]: (f.header["rows"], f.header["cols"]) for f in server.received("resize")}
        self.assertEqual({"manager_omp": (12, 58), "worker_omp": (12, 58), "host_shell": (11, 118)}, sizes)
        ui.send(P + b"q")
        ui.wait_exit()

    def test_host_scrollback_via_prefix_bracket_keys_not_forwarded_and_exit_to_live(self):
        lines = b"".join(b"history line %04d\r\n" % i for i in range(150))
        server, ui = self.start(replay={"host_shell": lines + b"user@host$ "}, snap=snapshot(focus="host_shell"))
        ui.send(P + b"3")
        self.assertTrue(ui.until(lambda: "user@host$" in ui.text() and "history line 0149" in ui.text()), ui.text())
        self.assertNotIn("history line 0000", ui.text())
        live_text = ui.text()
        ui.send(P + b"[")
        self.assertTrue(ui.until(lambda: "[SCROLL" in ui.text()), ui.text())
        ui.send(b"g")  # top of history
        self.assertTrue(ui.until(lambda: "history line 0000" in ui.text() and "user@host$" not in ui.text()), ui.text())
        self.assertIn("SCROLL live보다", ui.text())
        self.assertIn("history line 0001", ui.text())
        ui.send(b"\x1b[6~\x1b[6~")  # PgDn x2, then typed keys must not reach the pane
        ui.send(b"ls\r\x03")
        server.display("host_shell", b"NEW-OUTPUT-WHILE-SCROLLED\r\n", seq=3)
        self.assertTrue(ui.until(lambda: "history line 0020" in ui.text() and "history line 0000" not in ui.text()),
                        ui.text())
        ui.drain(0.3)
        self.assertNotIn("NEW-OUTPUT-WHILE-SCROLLED", ui.text())  # the held view did not jump to live
        self.assertEqual([], server.received("input"))
        ui.send(b"q")
        self.assertTrue(ui.until(lambda: "[SCROLL" not in ui.text() and "NEW-OUTPUT-WHILE-SCROLLED" in ui.text()),
                        ui.text())
        self.assertIn("user@host$", ui.text())
        ui.send(b"ls\r")
        self.assertTrue(server.wait_for(lambda: any(f.payload == b"ls\r" for f in server.received("input"))))
        self.assertEqual([b"ls\r"], [f.payload for f in server.received("input")])
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())
        self.assertNotEqual("", live_text)

    def test_prefix_pgup_enters_scroll_and_lone_esc_leaves_it(self):
        server, ui = self.start(replay={"manager_omp": b"".join(b"m %03d\r\n" % i for i in range(60))})
        self.assertTrue(ui.until(lambda: "m 059" in ui.text()), ui.text())
        ui.send(P + b"\x1b[5~")
        self.assertTrue(ui.until(lambda: "[SCROLL" in ui.text()), ui.text())
        ui.send(b"\x1b")
        self.assertTrue(ui.until(lambda: "[SCROLL" not in ui.text()), ui.text())
        self.assertEqual([], server.received("input"))
        ui.send(P + b"q")
        ui.wait_exit()

    # -- direct scrolling: mouse wheel / Shift+PgUp, no mode ------------------------------------------------
    def test_mouse_reporting_is_enabled_at_start_with_sgr_and_button_event_tracking(self):
        server, ui = self.start()
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()))
        ui.drain(0.3)
        raw = bytes(ui.raw)
        for mode in MOUSE_ON:
            self.assertIn(mode, raw)
        self.assertGreater(raw.index(MOUSE_ON[0]), raw.index(b"\x1b[?2004h"))
        for mode in (b"?1003h", b"?1015h"):  # no any-motion / urxvt reporting
            self.assertNotIn(b"\x1b[" + mode, raw)
        tracking = OuterMouse()
        tracking.feed(raw)
        self.assertEqual((1002, True), (tracking.tracking, tracking.sgr))
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())
        assert_mouse_restored(self, bytes(ui.raw))

    def test_wheel_scrolls_the_pane_under_the_pointer_and_typing_returns_to_live(self):
        lines = b"".join(b"m %03d\r\n" % i for i in range(60))
        server, ui = self.start(replay={"manager_omp": lines, "worker_omp": lines.replace(b"m ", b"w ")})
        self.assertTrue(ui.until(lambda: "m 059" in ui.text() and "w 059" in ui.text()), ui.text())
        ui.send(sgr(64, 90, 8) + sgr(64, 90, 8))  # wheel up over the WORKER pane (right half)
        self.assertTrue(ui.until(lambda: "WORKER OMP [SCROLL live보다 6줄" in ui.text() and "w 043" in ui.text()),
                        ui.text())  # older worker lines are shown again
        self.assertNotIn("MANAGER OMP [SCROLL", ui.text())
        self.assertIn("m 059", ui.text())  # the manager pane still shows its live screen
        server.display("worker_omp", b"NEW-WHILE-SCROLLED\r\n", seq=3)
        ui.drain(0.3)
        self.assertNotIn("NEW-WHILE-SCROLLED", ui.text())  # the held view does not jump
        ui.send(sgr(65, 90, 8))
        self.assertTrue(ui.until(lambda: "live보다 4줄" in ui.text()), ui.text())  # 6 + 1 new line - 3
        # typing goes to the focused (manager) pane; the scrolled worker view is left alone
        ui.send(b"ls\r")
        self.assertTrue(server.wait_for(lambda: server.received("input")))
        ui.drain(0.3)
        self.assertEqual([b"ls\r"], [f.payload for f in server.received("input")])
        self.assertEqual({"manager_omp"}, {f.header["pane"] for f in server.received("input")})
        self.assertIn("WORKER OMP [SCROLL", ui.text())
        # focus the scrolled pane: the next key returns it to live and arrives exactly once
        ui.send(P + b"2")
        self.assertTrue(server.wait_for(lambda: server.received("focus")))
        ui.send(b"x")
        self.assertTrue(ui.until(lambda: "[SCROLL" not in ui.text() and "NEW-WHILE-SCROLLED" in ui.text()), ui.text())
        self.assertTrue(server.wait_for(lambda: len(server.received("input")) == 2))
        self.assertEqual([b"ls\r", b"x"], [f.payload for f in server.received("input")])
        self.assertNotIn(b"\x1b[<", b"".join(f.payload for f in server.received("input")))  # reports never leak
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())

    def test_shift_pgup_pgdn_scroll_the_focused_pane_without_reaching_it(self):
        lines = b"".join(b"m %03d\r\n" % i for i in range(80))
        server, ui = self.start(replay={"manager_omp": lines})
        self.assertTrue(ui.until(lambda: "m 079" in ui.text()), ui.text())
        ui.send(b"\x1b[5;2~")
        self.assertTrue(ui.until(lambda: "MANAGER OMP [SCROLL live보다 11줄" in ui.text()), ui.text())
        ui.send(b"\x1b[6;2~")
        self.assertTrue(ui.until(lambda: "[SCROLL" not in ui.text()), ui.text())
        ui.drain(0.2)
        self.assertEqual([], server.received("input"))
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())

    def test_prefix_m_toggles_mouse_reporting_and_a_click_focuses(self):
        server, ui = self.start()
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()))
        ui.send(sgr(0, 90, 8))  # left click into the worker pane
        self.assertTrue(server.wait_for(lambda: server.received("focus")))
        self.assertEqual("worker_omp", server.received("focus")[-1].header["pane"])
        self.assertTrue(ui.until(lambda: "focus: WORKER OMP" in ui.text()))
        mark = len(ui.raw)
        ui.send(P + b"m")
        self.assertTrue(ui.until(lambda: "마우스 캡처 꺼짐" in ui.text()), ui.text())
        self.assertTrue(ui.until(lambda: all(m in bytes(ui.raw[mark:]) for m in MOUSE_OFF)))
        self.assertTrue(all(m not in bytes(ui.raw[mark:]) for m in MOUSE_ON))
        mark = len(ui.raw)
        ui.send(P + b"m")
        self.assertTrue(ui.until(lambda: "마우스 캡처 켜짐" in ui.text()), ui.text())
        self.assertTrue(ui.until(lambda: all(m in bytes(ui.raw[mark:]) for m in MOUSE_ON)))
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())
        assert_mouse_restored(self, bytes(ui.raw))

    def test_drag_in_the_host_pane_copies_the_selected_text_as_osc52_on_the_ui_stdout(self):
        import base64
        from workbench.ui.product.model import pane_boxes
        from workbench.contracts.v1 import PaneId
        server, ui = self.start(replay={"host_shell": "hello copy 한글\r\nsecond".encode()})
        self.assertTrue(ui.until(lambda: "hello copy" in ui.text()), ui.text())
        top, left, _, _ = pane_boxes(30, 120)[PaneId.HOST_SHELL]
        x, y = left + 2, top + 2
        mark = len(ui.raw)
        ui.send(sgr(0, x, y))
        ui.send(sgr(32, x + 8, y))
        ui.send(sgr(32, x + 12, y))
        ui.send(sgr(0, x + 12, y, release=True))
        expected = b"\x1b]52;c;" + base64.b64encode("hello copy 한".encode()) + b"\x07"
        self.assertTrue(ui.until(lambda: expected in bytes(ui.raw[mark:])), bytes(ui.raw[mark:])[-300:])
        self.assertTrue(ui.until(lambda: "복사됨: 12자" in ui.text()), ui.text())
        row = ui.screen.buffer[y - 1]  # the selected cells stay highlighted (reverse video) after the copy
        self.assertTrue(all(row[c].reverse for c in range(x - 1, x + 12)), [row[c].reverse for c in range(x - 1, x + 14)])
        self.assertFalse(row[x + 14].reverse)
        self.assertNotIn(b"\x1bPtmux;", bytes(ui.raw[mark:]))  # TMUX is not set in the child environment
        self.assertEqual([], server.received("input"))  # nothing typed into any pane
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())
        raw = bytes(ui.raw)
        assert_mouse_restored(self, raw)
        self.assertEqual(1, raw.count(expected))  # written once, and nothing during terminal restore
        self.assertLess(raw.rfind(expected), raw.rfind(MOUSE_OFF[0]))

    def test_mouse_reporting_stays_off_at_exit_after_toggling_off(self):
        server, ui = self.start()
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()))
        ui.send(P + b"m")
        self.assertTrue(ui.until(lambda: "마우스 캡처 꺼짐" in ui.text()), ui.text())
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())
        raw = bytes(ui.raw)
        self.assertGreater(raw.rfind(MOUSE_OFF[0]), raw.rfind(MOUSE_ON[0]))

    # -- P3-2: signals -----------------------------------------------------------
    def check_signal_restore(self, signum):
        server, ui = self.start()
        # The signal must land in the running loop: before the first frame is drawn the UI has neither entered the
        # alternate screen nor enabled bracketed paste, so there would be nothing to restore (test race, not a bug).
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()), ui.text())
        os.kill(ui.proc.pid, signum)
        self.assertEqual(1, ui.wait_exit())
        raw = bytes(ui.raw)
        self.assertIn(b"\x1b[?2004l", raw)
        self.assertIn(b"\x1b[?1049l", raw)
        assert_mouse_restored(self, raw)
        self.assertNotIn(b"Traceback", raw)
        attrs = termios.tcgetattr(ui.fd)
        self.assertTrue(attrs[3] & termios.ICANON and attrs[3] & termios.ECHO, "tty left raw")
        self.assertTrue(server.disconnected.wait(3))

    def test_sigterm_restores_terminal(self):
        self.check_signal_restore(signal.SIGTERM)

    def test_sighup_restores_terminal(self):
        self.check_signal_restore(signal.SIGHUP)

    # -- P3-3: prefix + sequence -------------------------------------------------
    def test_prefix_then_arrow_or_function_key_sends_nothing_to_pane(self):
        server, ui = self.start()
        for seq in (b"\x1b[A", b"\x1b[15~", b"\x1bOP", b"\x1bx"):
            ui.send(P + seq)
        ui.send(b"Z")
        self.assertTrue(server.wait_for(lambda: any(f.payload == b"Z" for f in server.received("input"))))
        self.assertEqual([b"Z"], [f.payload for f in server.received("input")])
        ui.send(P + b"q")
        ui.wait_exit()


class ProductLayoutPtyTests(unittest.TestCase):
    """C-D59: adjustable splits through the real curses loop on a PTY."""

    def start(self, layout_file=None, size=(30, 120)):
        server = FixtureServer()
        self.addCleanup(server.close)
        if layout_file is not None:
            (server.root / "ui-layout.json").write_bytes(layout_file)
        ui = UiProcess(server.path, size)
        self.addCleanup(ui.close)
        self.assertTrue(server.wait_for(server.attached.is_set), "attach not received")
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()))
        return server, ui

    @staticmethod
    def sizes(server, count=3):
        return {f.header["pane"]: (f.header["rows"], f.header["cols"]) for f in server.received("resize")[-count:]}

    def layout_file(self, server):
        return json.loads((server.root / "ui-layout.json").read_text())

    def test_mouse_drag_moves_the_divider_and_persists_it_0600(self):
        server, ui = self.start()
        self.assertTrue(ui.until(lambda: b"\x1b[?1002h" in bytes(ui.raw)), "motion-while-pressed not on from start")
        server.frames.clear()
        mark = len(ui.raw)
        ui.send(sgr(0, 60, 8))  # manager's right border
        ui.send(sgr(32, 45, 8) + sgr(32, 41, 8))
        ui.drain(0.3)
        ui.send(sgr(0, 41, 8, True))
        self.assertTrue(server.wait_for(lambda: server.received("resize")
                                        and self.sizes(server, 2) == {"manager_omp": (12, 39),
                                                                      "worker_omp": (12, 77)}))
        self.assertLessEqual(len(server.received("resize")), 6)  # debounced: not one frame per motion report
        self.assertEqual([], server.received("focus") + server.received("input"))
        self.assertTrue(ui.until(lambda: ui.screen.display[2].find("WORKER OMP") in range(40, 46)), ui.text())
        self.assertTrue(server.wait_for(lambda: (server.root / "ui-layout.json").exists()))
        path = server.root / "ui-layout.json"
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        data = self.layout_file(server)
        self.assertEqual(1, data["version"])
        self.assertAlmostEqual(41 / 120, data["col_ratio"], places=6)
        self.assertFalse(data["zoom"])
        self.assertEqual([], [p.name for p in server.root.iterdir() if p.name.endswith(".tmp")])
        during = bytes(ui.raw[mark:])
        for mode in (b"\x1b[?1000l", b"\x1b[?1002l", b"\x1b[?1003h", b"\x1b[?1000h"):  # never switched by a drag
            self.assertNotIn(mode, during)
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())
        assert_mouse_restored(self, bytes(ui.raw))

    def test_prefix_arrows_repeat_and_zoom_reset_persist(self):
        server, ui = self.start()
        server.frames.clear()
        ui.send(P + b"\x1b[D")
        ui.send(b"\x1b[D")  # repeat: no prefix needed
        self.assertTrue(server.wait_for(lambda: server.received("resize")
                                        and self.sizes(server, 2) == {"manager_omp": (12, 54),
                                                                      "worker_omp": (12, 62)}))
        self.assertEqual([], server.received("input"))
        ui.send(P + b"z")
        self.assertTrue(server.wait_for(lambda: self.sizes(server, 1) == {"manager_omp": (25, 118)}))
        self.assertTrue(ui.until(lambda: "WORKER OMP" not in ui.text() and "[ZOOM]" in ui.text()), ui.text())
        self.assertTrue(server.wait_for(lambda: (server.root / "ui-layout.json").exists()
                                        and self.layout_file(server)["zoom"] is True))
        ui.send(P + b"=")
        self.assertTrue(ui.until(lambda: "WORKER OMP" in ui.text() and "[ZOOM]" not in ui.text()), ui.text())
        self.assertTrue(server.wait_for(lambda: self.sizes(server, 2) == {  # host shell kept its size all along
            "manager_omp": (12, 58), "worker_omp": (12, 58)}))
        self.assertTrue(server.wait_for(lambda: self.layout_file(server) == {
            "version": 1, "col_ratio": None, "row_ratio": None, "zoom": False}))
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())

    def test_stored_layout_is_used_on_attach(self):
        stored = json.dumps({"version": 1, "col_ratio": 0.25, "row_ratio": 0.6, "zoom": False}).encode()
        server, ui = self.start(stored)
        self.assertTrue(server.wait_for(lambda: len(server.received("resize")) >= 3))
        sent = {(f.header["pane"], f.header["rows"], f.header["cols"]) for f in server.received("resize")}
        for want in (("manager_omp", 14, 28), ("worker_omp", 14, 88), ("host_shell", 9, 118)):
            self.assertIn(want, sent)
        self.assertTrue(ui.until(lambda: ui.screen.display[2].find("WORKER OMP") in range(29, 34)), ui.text())
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())

    def test_stored_zoom_survives_a_restart(self):
        stored = json.dumps({"version": 1, "col_ratio": None, "row_ratio": None, "zoom": True}).encode()
        server, ui = self.start(stored)
        self.assertTrue(ui.until(lambda: "[ZOOM]" in ui.text() and "WORKER OMP" not in ui.text()), ui.text())
        self.assertIn(("manager_omp", 25, 118), [(f.header["pane"], f.header["rows"], f.header["cols"])
                                                  for f in server.received("resize")])
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())
        self.assertTrue(self.layout_file(server)["zoom"])  # still stored

    def test_corrupt_layout_file_is_ignored(self):
        for raw in (b"\xff\x00garbage", b'{"version": 1, "col_ratio": 7}', b"[]"):
            with self.subTest(raw=raw):
                server, ui = self.start(raw)
                self.assertTrue(server.wait_for(lambda: len(server.received("resize")) >= 3))
                sent = {(f.header["pane"], f.header["rows"], f.header["cols"]) for f in server.received("resize")}
                self.assertIn(("manager_omp", 12, 58), sent)
                self.assertIn(("host_shell", 11, 118), sent)
                self.assertNotIn("Traceback", ui.text())
                ui.send(P + b"q")
                self.assertEqual(0, ui.wait_exit())


class OuterTerminalMouseTests(unittest.TestCase):
    """The UI's mouse DECSET/DECRST as a real terminal or multiplexer applies them (``support.OuterMouse``).

    Regression for the herdr/tmux report: ``?1002l`` after a divider drag turned the single tracking mode off, so
    no wheel report reached the UI any more.
    """

    def start(self):
        lines = b"".join(b"L%04d\r\n" % i for i in range(1, 201))
        server = FixtureServer(replay={"host_shell": lines})
        self.addCleanup(server.close)
        ui = UiProcess(server.path)
        self.addCleanup(ui.close)
        self.assertTrue(server.wait_for(server.attached.is_set), "attach not received")
        self.assertTrue(ui.until(lambda: "L0200" in ui.text()), ui.text())
        return server, ui

    @staticmethod
    def outer(ui):
        term = OuterMouse()
        term.feed(bytes(ui.raw))
        return term

    def act(self, ui, button, x, y, release=False):
        """Send what the outer terminal would send; False when it would send nothing."""
        ui.drain(0.05)
        data = self.outer(ui).report(button, x, y, release)
        if data is not None:
            ui.send(data)
        return data is not None

    @staticmethod
    def host_row(ui):
        return next(i for i, line in enumerate(ui.screen.display) if "HOST SHELL" in line) + 1  # 1-based

    def wheel_scrolls_host(self, ui):
        y = self.host_row(ui) + 3
        self.assertTrue(self.act(ui, 64, 10, y), "the outer terminal no longer reports the wheel")
        self.assertTrue(ui.until(lambda: "HOST SHELL [SCROLL" in ui.text()), ui.text())
        self.assertTrue(self.act(ui, 65, 10, y))  # wheel down: back to live
        self.assertTrue(ui.until(lambda: "[SCROLL" not in ui.text()), ui.text())

    def test_wheel_still_reaches_the_ui_after_divider_drags(self):
        server, ui = self.start()
        ui.drain(0.3)
        self.assertEqual(1002, self.outer(ui).tracking, "press/release, wheel and motion-while-pressed from start")
        self.assertTrue(self.outer(ui).sgr)
        self.wheel_scrolls_host(ui)
        server.frames.clear()
        # vertical divider (manager | worker), then the host divider, each dragged through the outer terminal
        for press, moves in (((60, 8), ((45, 8), (41, 8))), ((10, self.host_row(ui)), ((10, 16), (10, 14)))):
            self.assertTrue(self.act(ui, 0, *press))
            for x, y in moves:
                self.assertTrue(self.act(ui, 32, x, y), "motion while pressed is not reported")
            self.assertTrue(self.act(ui, 0, *moves[-1], release=True))
            self.assertTrue(server.wait_for(lambda: server.received("resize")), "the drag did not resize")
            server.frames.clear()
            ui.drain(0.3)
            self.assertEqual(1002, self.outer(ui).tracking, "mouse tracking changed by a divider drag")
            self.wheel_scrolls_host(ui)
        self.assertEqual(14, self.host_row(ui))
        self.assertEqual([], server.received("focus"))
        ui.send(P + b"m")  # capture off: nothing is reported
        self.assertTrue(ui.until(lambda: "마우스 캡처 꺼짐" in ui.text()), ui.text())
        self.assertIsNone(self.outer(ui).tracking)
        self.assertFalse(self.outer(ui).sgr)
        ui.send(P + b"m")
        self.assertTrue(ui.until(lambda: "마우스 캡처 켜짐" in ui.text()), ui.text())
        ui.drain(0.2)
        self.assertEqual(1002, self.outer(ui).tracking)
        self.wheel_scrolls_host(ui)
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())
        after = self.outer(ui)
        self.assertEqual((None, False), (after.tracking, after.sgr), "mouse reporting left on after detach")

    def test_mode_is_reasserted_when_the_window_changes(self):
        server, ui = self.start()
        ui.drain(0.3)
        ui.raw.extend(b"\x1b[?1000l")  # as if a multiplexer had reset the pane's mouse mode
        self.assertIsNone(self.outer(ui).tracking)
        UiProcess.set_size(ui.fd, (32, 120))
        os.kill(ui.proc.pid, signal.SIGWINCH)
        self.assertTrue(ui.until(lambda: self.outer(ui).tracking == 1002), "not re-asserted after a resize")
        ui.raw.extend(b"\x1b[?1002l")
        ui.send(P + b"r")  # redraw request re-asserts too
        self.assertTrue(ui.until(lambda: self.outer(ui).tracking == 1002), "not re-asserted after prefix r")
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())


class ImeNeutralPrefixTests(unittest.TestCase):
    """Ctrl aliases, the prefix Space menu and the Hangul hint on the real curses UI (client IME cannot be switched)."""

    def start(self, **kwargs):
        server = FixtureServer(**kwargs)
        self.addCleanup(server.close)
        ui = UiProcess(server.path)
        self.addCleanup(ui.close)
        self.assertTrue(server.wait_for(server.attached.is_set), "attach not received")
        # keys sent before curses raw mode would hit the cooked tty (Ctrl-z -> SIGTSTP): wait for the first draw
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()), ui.text())
        return server, ui

    def test_ctrl_alias_zoom_then_ctrl_q_detach_in_one_held_sequence(self):
        server, ui = self.start()
        ui.send(P + b"\x1a")  # Ctrl-] Ctrl-z
        self.assertTrue(ui.until(lambda: "[ZOOM]" in ui.text() and "WORKER OMP" not in ui.text()), ui.text())
        ui.send(P + b"\x1a")
        self.assertTrue(ui.until(lambda: "[ZOOM]" not in ui.text() and "WORKER OMP" in ui.text()), ui.text())
        self.assertEqual([], server.received("input"))
        ui.send(P + b"\x11")  # Ctrl-] Ctrl-q
        self.assertEqual(0, ui.wait_exit())
        self.assertTrue(server.wait_for(lambda: server.received("detach")))
        self.assertEqual([], server.received("input"))
        assert_mouse_restored(self, bytes(ui.raw))

    def test_ctrl_alias_takeover_request_confirm_handoff_and_redraw(self):
        server, ui = self.start()
        ui.send(P + b"\x14")
        self.assertTrue(server.wait_for(lambda: server.received("takeover_request")))
        ui.send(P + b"\x19")
        self.assertTrue(server.wait_for(lambda: server.received("takeover_confirm")))
        ui.send(P + b"\x0f")
        self.assertTrue(server.wait_for(lambda: server.received("handoff")))
        self.assertTrue(server.wait_for(lambda: len(server.received("resize")) >= 3))
        ui.drain(0.3)
        before = len(server.received("resize"))
        ui.send(P + b"\x12")  # redraw nudge = two more resize frames for the focus pane
        self.assertTrue(server.wait_for(lambda: len(server.received("resize")) >= before + 2))
        self.assertEqual([], server.received("input"))
        ui.send(P + b"\x11")
        self.assertEqual(0, ui.wait_exit())

    def test_menu_arrows_enter_digits_and_esc(self):
        server, ui = self.start()
        ui.send(P + b" ")
        self.assertTrue(ui.until(lambda: "명령 메뉴" in ui.text() and "> 1" in ui.text()), ui.text())
        for needle in ("Ctrl-q", "Ctrl-z", "1/2/3", "Esc"):
            self.assertIn(needle, ui.text())
        ui.send(b"\x1b[B")  # down -> item 2 (zoom)
        self.assertTrue(ui.until(lambda: "> 2" in ui.text()), ui.text())
        ui.send(b"hello\x03")  # nothing reaches a pane while the menu is open
        ui.drain(0.3)
        self.assertEqual([], server.received("input"))
        ui.send(b"\r")
        self.assertTrue(ui.until(lambda: "[ZOOM]" in ui.text() and "명령 메뉴" not in ui.text()), ui.text())
        ui.send(P + b" ")
        self.assertTrue(ui.until(lambda: "명령 메뉴" in ui.text()), ui.text())
        ui.send(b"\x1b")  # lone Esc cancels
        self.assertTrue(ui.until(lambda: "명령 메뉴" not in ui.text()), ui.text())
        self.assertEqual([], server.received("input"))
        ui.send(P + b" ")
        self.assertTrue(ui.until(lambda: "명령 메뉴" in ui.text()), ui.text())
        ui.send(b"0")  # digit 0 = detach
        self.assertEqual(0, ui.wait_exit())
        self.assertTrue(server.wait_for(lambda: server.received("detach")))
        self.assertEqual([], server.received("input"))

    def test_syllable_after_prefix_shows_hint_and_sends_nothing(self):
        server, ui = self.start()
        ui.send(P + "안".encode())
        self.assertTrue(ui.until(lambda: "한글 입력 상태" in ui.text() and "Ctrl-] Space" in ui.text()), ui.text())
        ui.drain(0.3)
        self.assertEqual([], server.received("input"))
        ui.send(b"x")
        self.assertTrue(server.wait_for(lambda: any(f.payload == b"x" for f in server.received("input"))))
        ui.send(P + b"\x11")
        self.assertEqual(0, ui.wait_exit())

    def test_jamo_after_prefix_then_space_detaches_on_the_real_loop(self):
        server, ui = self.start()
        ui.send(P)
        ui.drain(0.6)  # the IME may hold the jamo for a while: the prefix stays armed
        ui.send("ㅂ".encode()[:2])  # UTF-8 split across two reads
        ui.drain(0.3)
        ui.send("ㅂ".encode()[2:] + b" ")  # ㅂ (= q) + commit Space
        self.assertEqual(0, ui.wait_exit())
        self.assertTrue(server.wait_for(lambda: server.received("detach")))
        self.assertEqual([], server.received("input"))
        assert_mouse_restored(self, bytes(ui.raw))

    def test_footer_and_help_list_the_ctrl_forms_and_menu(self):
        server, ui = self.start()
        ui.send(P)
        self.assertTrue(ui.until(lambda: "Space 메뉴" in ui.text() and "Ctrl+q" in ui.text()), ui.text())
        ui.send(b"?")
        self.assertTrue(ui.until(lambda: "Ctrl-t" in ui.text() and "prefix Space" in ui.text()), ui.text())
        ui.send(b" ")
        ui.send(P + b"\x11")
        self.assertEqual(0, ui.wait_exit())

    def test_old_detach_keys_do_not_detach_and_ctrl_d_to_omp_needs_a_second_press(self):
        server, ui = self.start()
        ui.send(P + b"d")  # old detach key: a notice, no detach, nothing sent
        self.assertTrue(ui.until(lambda: "detach는 이제" in ui.text()), ui.text())
        ui.send(P + b"\x04")
        ui.drain(0.3)
        self.assertEqual([], server.received("input"))
        self.assertEqual([], server.received("detach"))
        ui.send(b"\x04")  # Ctrl-d on the manager pane: held, never delivered to the fake OMP
        self.assertTrue(ui.until(lambda: "2초 안에 Ctrl-d" in ui.text()), ui.text())
        ui.drain(0.3)
        self.assertEqual([], server.received("input"))
        ui.send(b"\x04")  # second press within 2 s: exactly one 0x04 arrives
        self.assertTrue(server.wait_for(lambda: any(f.payload == b"\x04" for f in server.received("input"))))
        ui.drain(0.3)
        self.assertEqual([b"\x04"], [f.payload for f in server.received("input")])
        self.assertEqual("manager_omp", server.received("input")[0].header["pane"])
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())
        self.assertTrue(server.wait_for(lambda: server.received("detach")))


FAKE_OMP = r'''#!{python}
import json, os, socket, sys, uuid
argv = sys.argv[1:]
if argv[:2] == ["config", "get"]:
    print("[]")
    sys.exit(0)
with open(os.environ["FAKE_PANE_RECORD"], "a") as stream:
    stream.write(json.dumps({{"pid": os.getpid(), "role": os.environ["WORKBENCH_G3_ROLE"]}}) + "\n")
sock = socket.socket(socket.AF_UNIX)
sock.connect(os.environ["WORKBENCH_G3_BRIDGE_SOCKET"])
hello = {{"kind": "hello", "protocolVersion": 1, "token": os.environ["WORKBENCH_G3_TOKEN"],
          "role": os.environ["WORKBENCH_G3_ROLE"], "ompSessionId": str(uuid.uuid4()),
          "generation": int(os.environ["WORKBENCH_G3_GENERATION"]), "pid": os.getpid()}}
sock.sendall((json.dumps(hello) + "\n").encode())
json.loads(sock.makefile("rb").readline())
print("fake omp", os.environ["WORKBENCH_G3_ROLE"], "pid=%d" % os.getpid(), flush=True)
for line in sys.stdin:
    if line.strip() == "exit":
        break
    print("echo:" + line.strip(), flush=True)
'''


def task_automation_snapshot(*, state, paused, task_status="running", worker="busy"):
    """A fake backend push: a busy worker with an experiment Task and the given automation state (ui_v1, C-D66)."""
    snap = snapshot()
    snap["task"] = {"task_id": "t1", "kind": "experiment", "status": task_status, "summary": "lr sweep 3 runs",
                    "since": 1.0, "active": True, "revision": 1, "run_id": "run-1", "runs_started": 1, "retry_limit": 3,
                    "held_reason": None, "closed_reason": None, "cancel_requested": False, "last_result": None}
    snap["worker"] = {"state": worker, "task_id": "t1" if worker == "busy" else None}
    snap["automation"] = {"state": state, "source": "user", "detail": None, "paused": paused, "transition": None,
                          "run": None, "tick": None, "interruption": {"state": "confirmed" if paused else "none"},
                          "review": None if paused else {"applies": True, "interval_seconds": 60, "status": "waiting",
                                                         "reason": None, "next_due_in_seconds": 41},
                          "resume": None, "retry_limit": 3, "persistence_error": None}
    return snap


class TaskWorkerAutomationPtyTests(unittest.TestCase):
    """CW-18 U5 on the real curses UI: a pushed busy worker / paused automation is shown; p opens the confirmation."""

    def start(self):
        server = FixtureServer()
        self.addCleanup(server.close)
        ui = UiProcess(server.path)
        self.addCleanup(ui.close)
        self.assertTrue(server.wait_for(server.attached.is_set), "attach not received")
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()), ui.text())
        return server, ui

    def test_busy_worker_and_paused_automation_are_drawn_and_p_confirm_resumes_reconciled(self):
        server, ui = self.start()
        server.state(task_automation_snapshot(state="paused", paused=True))
        self.assertTrue(ui.until(lambda: "worker: 작업 중" in ui.text() and "자동화: paused" in ui.text()
                                  and "일시정지됨" in ui.text()), ui.text())
        text = ui.text()
        self.assertIn("lr sweep 3 runs", text)
        self.assertIn("실험", text)
        self.assertIn("실행 중", text)
        self.assertIn("일시정지됨", text)
        self.assertNotIn("승인", text)
        ui.send(P + b"\x10")  # Ctrl-] Ctrl-p
        self.assertTrue(ui.until(lambda: "대조 후 재개합니다 (p: 확인)" in ui.text()), ui.text())
        self.assertEqual([], server.received("resume"))
        ui.send(b"\x1b")  # lone Esc cancels
        self.assertTrue(ui.until(lambda: "대조 후 재개합니다" not in ui.text()), ui.text())
        ui.drain(0.2)
        self.assertEqual([], server.received("resume") + server.received("pause") + server.received("input"))
        ui.send(P + b"p")
        self.assertTrue(ui.until(lambda: "대조 후 재개합니다 (p: 확인)" in ui.text()), ui.text())
        ui.send(b"p")
        self.assertTrue(server.wait_for(lambda: server.received("resume")), "resume not sent")
        self.assertIs(server.received("resume")[0].header["reconciled"], True)
        self.assertEqual([], server.received("pause") + server.received("input"))
        server.state(task_automation_snapshot(state="idle", paused=False, task_status="waiting_report", worker="idle"))
        self.assertTrue(ui.until(lambda: "worker: 대기" in ui.text() and "60s 대조 41s 후" in ui.text()), ui.text())
        self.assertIn("60s 대조 41s 후", ui.text())
        ui.send(P + b"p")
        self.assertTrue(ui.until(lambda: "자동화를 일시정지합니다 (p: 확인)" in ui.text()), ui.text())
        ui.send("ㅔ".encode())  # a lone jamo confirms (Hangul IME left on)
        self.assertTrue(server.wait_for(lambda: server.received("pause")), "pause not sent")
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())


def _alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] not in "ZX"
    except OSError:
        return False


class RealBackendCase(unittest.TestCase):
    """A real Backend (fake OMP panes, real bash host shell) with the product UI on a PTY."""

    def setUp(self):
        from workbench.backend.launcher import LaunchPlan
        from workbench.backend.paths import DataLayout, ensure_private_dir
        from workbench.backend.service import Backend
        from workbench.terminal.shell_g2.prototype import ShellChoice

        self._dir = tempfile.TemporaryDirectory(prefix="rs27-", dir="/tmp")
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        project, home = root / "p", root / "h"
        project.mkdir()
        home.mkdir()
        fake = root / "omp"
        fake.write_text(FAKE_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        self.record = root / "panes.jsonl"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "FAKE_PANE_RECORD": str(self.record)}
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), str(fake), "omp/18.4.4", "/x/bridge.ts", ("--plan-arg",))
        patcher = mock.patch("workbench.backend.service.check_isolation",
                             lambda *a, role, **k: {"role": role, "state": "ok", "ok": True, "leaks": [],
                                                    "warnings": [], "error": None})
        patcher.start()
        self.addCleanup(patcher.stop)
        layout = DataLayout(root / "d")
        ensure_private_dir(layout.root)
        self.backend = Backend(layout, plan, project_dir=str(project), environment=env)
        self.stop = threading.Event()
        self.ticker = None
        self.addCleanup(self._close_backend)
        self.backend._open()
        deadline = time.monotonic() + 15
        while self.backend.phase != "ready" and time.monotonic() < deadline:
            self.backend._tick(0.02)
        self.assertEqual("ready", self.backend.phase)
        self.ticker = threading.Thread(target=self._tick_loop, daemon=True)
        self.ticker.start()
        self.ui = UiProcess(layout.ui_socket)
        self.addCleanup(self.ui.close)

    def _tick_loop(self):
        while not self.stop.is_set():
            self.backend._tick(0.02)

    def _close_backend(self):
        self.stop.set()
        if self.ticker is not None:
            self.ticker.join(5)
        lines = self.record.read_text().splitlines() if self.record.exists() else []
        pids = [json.loads(line)["pid"] for line in lines if line.strip()]
        self.backend._close()
        for pid in pids:
            self.assertFalse(_alive(pid), f"fake OMP {pid} survived the backend close")


class RestartExitedOmpPtyTests(RealBackendCase):
    """C-D62 (1) on a real backend: the manager fake OMP exits, the UI shows the notice, Enter starts a new session."""

    def test_exited_manager_shows_the_notice_and_enter_starts_a_new_session_that_takes_input(self):
        ui = self.ui
        old = json.loads(self.record.read_text().splitlines()[0])
        self.assertEqual("manager", old["role"])  # the manager registered first: it is spawned before the worker
        self.assertTrue(ui.until(lambda: f"fake omp manager pid={old['pid']}" in ui.text()), ui.text())
        ui.send(b"exit\r")  # the fake OMP exits on this line
        self.assertTrue(ui.until(lambda: "OMP 종료됨" in ui.text() and "/resume" in ui.text()), ui.text())
        self.assertIn(f"pid={old['pid']}", ui.text(), "the last screen stays visible")
        ui.send(b"stray")  # other keys go nowhere
        self.assertTrue(ui.until(lambda: "Enter로 새 세션" in ui.text()), ui.text())
        ui.send(b"\r")
        self.assertTrue(ui.until(lambda: "fake omp manager pid=" in ui.text() and f"pid={old['pid']}" not in ui.text()),
                        ui.text())  # the new generation cleared the old screen
        self.assertTrue(ui.until(lambda: "MANAGER OMP *FOCUS* alive" in ui.text() and "다시 시작 중" not in ui.text()),
                        ui.text())  # the snapshot shows the pane alive again: notice gone, input flows
        self.assertNotIn("OMP 종료됨", ui.text())
        ui.send(b"hello\r")
        self.assertTrue(ui.until(lambda: "echo:hello" in ui.text()), ui.text())
        self.assertNotIn("echo:stray", ui.text())
        invocations = [json.loads(line) for line in self.record.read_text().splitlines() if line.strip()]
        self.assertEqual(3, len(invocations), "manager, worker and exactly one restarted manager")
        self.assertEqual("manager", invocations[-1]["role"])
        self.assertNotEqual(old["pid"], invocations[-1]["pid"])
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())


def _session_members(sid, comm=None):
    """PIDs of the processes in session ``sid`` (optionally only those whose command name is ``comm``)."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        name, rest = stat[stat.index("(") + 1:stat.rindex(")")], stat[stat.rindex(")") + 2:].split()
        if rest[0] not in "ZX" and int(rest[3]) == sid and (comm is None or name == comm):
            found.append(int(entry.name))
    return found


class KillRestartHostShellPtyTests(RealBackendCase):
    """C-D63 on a real backend: Ctrl-] k then k kills the host shell and its jobs; Enter starts a fresh shell."""

    def test_ctrl_bracket_k_confirm_kills_shell_and_jobs_and_enter_starts_a_fresh_shell(self):
        import re
        ui = self.ui
        self.assertTrue(ui.until(lambda: "HOST SHELL" in ui.text()), ui.text())
        ui.send(P + b"3")  # focus the host shell
        ui.send(b"echo SHPID=$$\r")
        self.assertTrue(ui.until(lambda: re.search(r"SHPID=\d+", ui.text())), ui.text())
        shell = int(re.search(r"SHPID=(\d+)", ui.text()).group(1))
        sleeps = []

        def cleanup_sleeps():  # only the sleeps found in this test's own shell session
            for pid in set(sleeps) | set(_session_members(shell, "sleep")):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

        self.addCleanup(cleanup_sleeps)
        ui.send(b"sleep 1000 &\r")  # a background job ...
        ui.send(b"sleep 1000\r")  # ... and a foreground one
        self.assertTrue(ui.until(lambda: len(_session_members(shell, "sleep")) == 2), _session_members(shell))
        sleeps.extend(_session_members(shell, "sleep"))
        ui.send(P + b"k")
        self.assertTrue(ui.until(lambda: "강제 종료합니다" in ui.text() and "k: 종료" in ui.text()), ui.text())
        self.assertTrue(all(_alive(pid) for pid in sleeps + [shell]), "the confirmation alone kills nothing")
        ui.send(b"k")
        self.assertTrue(ui.until(lambda: "host terminal 종료됨" in ui.text() and "exited" in ui.text()), ui.text())
        self.assertNotIn("강제 종료합니다", ui.text())
        self.assertTrue(ui.until(lambda: not any(_alive(pid) for pid in sleeps + [shell]), 10), [_alive(p) for p in sleeps])
        self.assertEqual([], _session_members(shell))
        ui.send(b"typed")  # nothing reaches the exited pane
        self.assertTrue(ui.until(lambda: "Enter로 새 shell" in ui.text()), ui.text())
        ui.send(b"\r")  # Enter: a fresh shell
        self.assertTrue(ui.until(lambda: "host terminal 종료됨" not in ui.text() and "HOST SHELL" in ui.text()), ui.text())
        ui.send(b"echo NEWPID=$$ FRESH$((20+22))\r")
        self.assertTrue(ui.until(lambda: re.search(r"NEWPID=\d+ FRESH42", ui.text())), ui.text())
        fresh = int(re.search(r"NEWPID=(\d+) FRESH42", ui.text()).group(1))
        self.assertNotEqual(shell, fresh)
        self.assertTrue(_alive(fresh))
        self.assertNotIn("echo:typed", ui.text())
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())


if __name__ == "__main__":
    unittest.main()
