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
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pyte  # noqa: E402
from support import FixtureServer, snapshot  # noqa: E402

SRC = str(Path(__file__).resolve().parents[2] / "src")
CODE = ("import sys; from pathlib import Path; from workbench.ui.product import run_product; "
        "raise SystemExit(run_product(Path(sys.argv[1])))")
P = b"\x1d"
MOUSE_ON = (b"\x1b[?1000h", b"\x1b[?1006h")
MOUSE_OFF = (b"\x1b[?1000l", b"\x1b[?1006l")


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
        self.screen = pyte.Screen(size[1], size[0])
        self.stream = pyte.ByteStream(self.screen)
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
        ui.wait_exit()

    # -- direct scrolling: mouse wheel / Shift+PgUp, no mode ------------------------------------------------
    def test_mouse_reporting_is_enabled_at_start_with_sgr_and_only_the_basic_modes(self):
        server, ui = self.start()
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()))
        ui.drain(0.3)
        raw = bytes(ui.raw)
        for mode in MOUSE_ON:
            self.assertIn(mode, raw)
        self.assertGreater(raw.index(MOUSE_ON[0]), raw.index(b"\x1b[?2004h"))
        for mode in (b"?1002h", b"?1003h", b"?1015h"):  # no drag/any-motion reporting
            self.assertNotIn(b"\x1b[" + mode, raw)
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        ui.send(P + b"d")
        self.assertEqual(0, ui.wait_exit())
        assert_mouse_restored(self, bytes(ui.raw))

    def test_mouse_reporting_stays_off_at_exit_after_toggling_off(self):
        server, ui = self.start()
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()))
        ui.send(P + b"m")
        self.assertTrue(ui.until(lambda: "마우스 캡처 꺼짐" in ui.text()), ui.text())
        ui.send(P + b"d")
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
        ui.send(P + b"d")
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
        raw_before = bytes(ui.raw)
        self.assertNotIn(b"\x1b[?1002h", raw_before)
        server.frames.clear()
        ui.send(sgr(0, 60, 8))  # manager's right border
        self.assertTrue(ui.until(lambda: b"\x1b[?1002h" in bytes(ui.raw)), "drag reporting not enabled on press")
        ui.send(sgr(32, 45, 8) + sgr(32, 41, 8))
        ui.drain(0.3)
        ui.send(sgr(0, 41, 8, True))
        self.assertTrue(ui.until(lambda: b"\x1b[?1002l" in bytes(ui.raw)))
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
        ui.send(P + b"d")
        self.assertEqual(0, ui.wait_exit())
        assert_mouse_restored(self, bytes(ui.raw))
        raw = bytes(ui.raw)
        self.assertGreater(raw.rfind(b"\x1b[?1002l"), raw.rfind(b"\x1b[?1002h"))

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
        ui.send(P + b"d")
        self.assertEqual(0, ui.wait_exit())

    def test_stored_layout_is_used_on_attach(self):
        stored = json.dumps({"version": 1, "col_ratio": 0.25, "row_ratio": 0.6, "zoom": False}).encode()
        server, ui = self.start(stored)
        self.assertTrue(server.wait_for(lambda: len(server.received("resize")) >= 3))
        sent = {(f.header["pane"], f.header["rows"], f.header["cols"]) for f in server.received("resize")}
        for want in (("manager_omp", 14, 28), ("worker_omp", 14, 88), ("host_shell", 9, 118)):
            self.assertIn(want, sent)
        self.assertTrue(ui.until(lambda: ui.screen.display[2].find("WORKER OMP") in range(29, 34)), ui.text())
        ui.send(P + b"d")
        self.assertEqual(0, ui.wait_exit())

    def test_stored_zoom_survives_a_restart(self):
        stored = json.dumps({"version": 1, "col_ratio": None, "row_ratio": None, "zoom": True}).encode()
        server, ui = self.start(stored)
        self.assertTrue(ui.until(lambda: "[ZOOM]" in ui.text() and "WORKER OMP" not in ui.text()), ui.text())
        self.assertIn(("manager_omp", 25, 118), [(f.header["pane"], f.header["rows"], f.header["cols"])
                                                  for f in server.received("resize")])
        ui.send(P + b"d")
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
                ui.send(P + b"d")
                self.assertEqual(0, ui.wait_exit())


if __name__ == "__main__":
    unittest.main()
