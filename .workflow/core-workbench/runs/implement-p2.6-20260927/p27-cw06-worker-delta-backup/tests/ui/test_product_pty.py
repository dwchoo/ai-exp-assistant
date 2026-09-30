"""Run the real product UI (curses) on a PTY against a fixture ui_v1 server."""
import fcntl
import os
import select
import signal
import struct
import subprocess
import sys
import termios
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
        self.assertIn("--plain", result.stderr)


if __name__ == "__main__":
    unittest.main()
