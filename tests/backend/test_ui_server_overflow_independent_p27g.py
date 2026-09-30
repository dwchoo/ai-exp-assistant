"""Independent CW-17 overflow test (p27-cw06-rerun-test-04).

Expectations derive from ui_v1 semantics only: a client that stops reading and makes the backend's outbound
queue overflow is closed with ``CLOSING slow_client`` -- connection-only; the byte stream it receives must be a
sequence of *whole, valid* frames ending in that CLOSING frame and carrying nothing after it; the frame that was
partially written when the overflow happened is completed, never truncated; the backend/controller are
unaffected and a new client can attach afterwards.

Uses only the public UiServer API (poll/broadcast/push_state/attached), a private socket in a temp dir and raw
client sockets owned by the test. No processes are spawned.
"""
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from workbench.backend.ui_server import UiServer
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import DisplayChunk, PaneId

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ui_server import FakeController  # noqa: E402  (shared fake controller; not production code)

KiB = 1024
MAX_OUT = 512 * KiB


def payload_for(seq: int, size: int) -> bytes:
    unit = f"<{seq:06d}>".encode()
    return (unit * (size // len(unit) + 1))[:size]


class OverflowIndependent(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw17-ovf-")
        self.path = Path(self._dir.name) / "ui.sock"
        self.controller = FakeController()
        self.server = UiServer(self.path, self.controller, max_outbound=MAX_OUT)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.socks = []

    def _serve(self):
        while not self.stop.is_set():
            with self.lock:
                self.server.poll(0)
            time.sleep(0.001)  # yield: threading.Lock is unfair, a busy poller would starve the flooding thread

    def tearDown(self):
        self.stop.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        for sock in self.socks:
            sock.close()
        self.server.close(flush_timeout=0.5)
        self.assertFalse(self.path.exists())
        self._dir.cleanup()

    # -- helpers -------------------------------------------------------------
    def connect(self, rcvbuf=None):
        sock = socket.socket(socket.AF_UNIX)
        if rcvbuf:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        sock.settimeout(5)
        sock.connect(str(self.path))
        self.socks.append(sock)
        return sock

    def attach_raw(self, sock, rid="a1"):
        """hello + attach, read until the attach result; returns (frames, decoder)."""
        sock.sendall(ui_v1.encode_frame(ui_v1.hello("ovf-test")))
        decoder, frames = ui_v1.FrameDecoder(), []
        self._read_until(sock, decoder, frames, lambda f: f.header.get("type") == "welcome")
        sock.sendall(ui_v1.encode_frame(ui_v1.request(ClientType.ATTACH, rid, rows=24, cols=80)))
        self._read_until(sock, decoder, frames,
                         lambda f: f.header.get("type") == "result" and f.header.get("id") == rid)
        result = [f for f in frames if f.header.get("type") == "result"][-1]
        self.assertTrue(result.header["ok"], result.header)
        return frames, decoder

    def _read_until(self, sock, decoder, frames, pred, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(pred(f) for f in frames):
                return
            data = sock.recv(65536)
            self.assertTrue(data, "connection closed early")
            frames.extend(decoder.feed(data))
        self.fail("timeout waiting for frame")

    def drain_to_eof(self, sock, decoder, timeout=15):
        """Read everything until EOF; returns (frames, total_bytes)."""
        frames, total = [], 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                self.fail("no EOF after CLOSING: connection not closed by backend")
            if not data:
                return frames, total
            total += len(data)
            frames.extend(decoder.feed(data))  # ProtocolError (bad magic etc.) would raise here -> test error
        self.fail("timeout draining")

    def flood(self, chunk_size, limit_seq=400):
        """Broadcast whole chunks while the client is not reading until the server detaches the client.
        Then keep sending a little more (state pushes + displays) to prove nothing is queued after CLOSING."""
        sent = 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and sent < limit_seq:
            with self.lock:
                if self.server.attached is None:
                    break
                sent += 1
                self.server.broadcast(DisplayChunk(self.controller.session, 1, PaneId.HOST_SHELL, sent,
                                                   payload_for(sent, chunk_size)))
            time.sleep(0.001)
        with self.lock:
            attached_after = self.server.attached
        self.assertIsNone(attached_after, f"backend never overflowed within {sent} chunks of {chunk_size}")
        for extra in range(5):
            with self.lock:
                self.server.broadcast(DisplayChunk(self.controller.session, 1, PaneId.HOST_SHELL, 10_000 + extra,
                                                   b"AFTER-CLOSING"))
                self.server.push_state({"after": "closing"})
        return sent

    def assert_stream(self, frames, chunk_size, sent):
        types = [f.header["type"] for f in frames]
        self.assertEqual(types[-1], "closing", types[-3:])
        self.assertEqual(types.count("closing"), 1)
        self.assertEqual(frames[-1].header["reason"], "slow_client")
        self.assertEqual(frames[-1].payload, b"")
        # nothing after CLOSING: nothing beyond it was decoded, and no post-closing marker leaked in
        for frame in frames:
            self.assertNotIn(b"AFTER-CLOSING", frame.payload)
            self.assertNotEqual(frame.header.get("snapshot"), {"after": "closing"})
        live = [f for f in frames[:-1] if f.header["type"] == "display" and not f.header.get("replay")]
        self.assertTrue(live, "no live display frame delivered before overflow")
        # Every delivered display frame is whole (full payload, expected bytes) and they are a gap-free prefix:
        # whole unsent frames may be dropped only from the tail.
        for index, frame in enumerate(live, start=1):
            chunk = ui_v1.decode_display(frame)
            self.assertEqual(chunk.sequence, index, "delivered display frames are not a contiguous prefix")
            self.assertEqual(chunk.data, payload_for(index, chunk_size), f"frame {index} truncated/corrupt")
        self.assertLess(len(live), sent, "overflow should have dropped at least the last whole frame")

    # -- tests ---------------------------------------------------------------
    def test_reader_that_resumes_gets_valid_stream_ending_in_closing_slow_client(self):
        for chunk_size in (7_777, 65_536, 150_001, 400_000):
            with self.subTest(chunk_size=chunk_size):
                self._one_case(chunk_size)

    def _one_case(self, chunk_size):
        sock = self.connect(rcvbuf=4096)
        frames, decoder = self.attach_raw(sock)
        sent = self.flood(chunk_size)
        time.sleep(0.2)  # client still not reading; let the server settle
        rest, _ = self.drain_to_eof(sock, decoder)
        frames.extend(rest)
        if hasattr(decoder, "_buffer"):  # EOF must land on a frame boundary
            self.assertEqual(len(decoder._buffer), 0, "leftover bytes: stream ended mid-frame")
        # attach-time replay frames (retained tail) precede live frames; only the live ones are checked below
        self.assert_stream(frames, chunk_size, sent)
        # backend / controller unaffected: only this attachment was ended
        self.assertEqual(self.controller.detaches, 1)
        self.assertFalse(self.stop.is_set())
        sock.close()
        self.attach_again_and_cleanup()
        self.controller.detaches = 0

    def attach_again_and_cleanup(self):
        """A new client can attach afterwards and gets an ordinary stream; then detaches cleanly."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.lock:
                if self.server.attached is None:
                    break
            time.sleep(0.01)
        sock = self.connect()
        frames, decoder = self.attach_raw(sock, rid="again")
        with self.lock:
            self.server.broadcast(DisplayChunk(self.controller.session, 1, PaneId.HOST_SHELL, 1, b"fresh"))
        self._read_until(sock, decoder, frames,
                         lambda f: f.header.get("type") == "display" and not f.header.get("replay"))
        live = [f for f in frames if f.header.get("type") == "display" and not f.header.get("replay")]
        self.assertEqual(ui_v1.decode_display(live[0]).data, b"fresh")
        sock.sendall(ui_v1.encode_frame(ui_v1.request(ClientType.DETACH, "d1")))
        self._read_until(sock, decoder, frames,
                         lambda f: f.header.get("type") == "result" and f.header.get("id") == "d1")
        sock.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.lock:
                if self.server.attached is None:
                    return
            time.sleep(0.01)
        self.fail("second client did not detach")

    def test_other_connections_and_requests_survive_overflow_of_the_attached_client(self):
        watcher = self.connect()
        watcher.sendall(ui_v1.encode_frame(ui_v1.hello("watcher")))
        w_dec, w_frames = ui_v1.FrameDecoder(), []
        self._read_until(watcher, w_dec, w_frames, lambda f: f.header.get("type") == "welcome")
        slow = self.connect(rcvbuf=4096)
        _, slow_dec = self.attach_raw(slow)
        self.flood(100_000)
        # the non-attached connection still gets ordinary request/result service (not closed, not affected)
        watcher.sendall(ui_v1.encode_frame(ui_v1.request(ClientType.SNAPSHOT, "s1")))
        self._read_until(watcher, w_dec, w_frames,
                         lambda f: f.header.get("type") == "result" and f.header.get("id") == "s1")
        self.assertNotIn("closing", [f.header["type"] for f in w_frames])
        # and the slow one still ends properly
        frames, total = self.drain_to_eof(slow, slow_dec)
        self.assertEqual(frames[-1].header["type"], "closing")


if __name__ == "__main__":
    unittest.main()
