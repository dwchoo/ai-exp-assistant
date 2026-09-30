"""Run the real product UI (curses) on a PTY against a fixture ui_v1 server."""
import fcntl
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
        self.assertEqual({"manager_omp": (35, 48), "worker_omp": (35, 48), "host_shell": (35, 48)}, latest)
        ui.send(P + b"d")
        ui.wait_exit()

    def test_backend_close_shows_notice_and_exits_nonzero(self):
        server, ui = self.start()
        server.closing("backend_shutdown")
        self.assertEqual(1, ui.wait_exit())
        self.assertIn(b"backend_shutdown", bytes(ui.raw))
        self.assertIn(b"\x1b[?1049l", bytes(ui.raw))

    def test_connection_lost_exits_nonzero_and_restores(self):
        server, ui = self.start()
        server.conn.close()
        self.assertEqual(1, ui.wait_exit())
        self.assertIn(b"\x1b[?2004l", bytes(ui.raw))

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


if __name__ == "__main__":
    unittest.main()
