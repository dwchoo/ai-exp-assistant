"""L-CW17-CONTRACT fixture: ui_v1 server version/framing/limits/paste/disconnect."""
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest

from workbench.backend.client import ClientError, UiClient
from workbench.backend.ui_server import Held, UiServer
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, Reason
from workbench.contracts.v1 import DisplayChunk, PaneId, new_identifier

MiB = 1024 * 1024


class FakeController:
    """Bounded per-pane queues; delivered bytes are only ever whole frames."""

    def __init__(self, capacity=2 * MiB):
        self.capacity = capacity
        self.queued = {pane: bytearray() for pane in PaneId}
        self.frames = {pane: [] for pane in PaneId}
        self.focus = PaneId.MANAGER_OMP
        self.attaches, self.detaches = [], 0
        self.session = new_identifier()
        self.sequence = 0
        self.shell_owner = "user"
        self.shutdown_token = None
        self.shutdown_confirmed = False

    def snapshot(self):
        return {"focus": self.focus.value, "owner": self.shell_owner,
                "queued": {p.value: len(q) for p, q in self.queued.items()}}

    def chunk(self, data):
        self.sequence += 1
        return DisplayChunk(self.session, 1, PaneId.HOST_SHELL, self.sequence, data)

    def replay(self):
        return [DisplayChunk(self.session, 1, PaneId.HOST_SHELL, 1, b"retained")]

    def on_attach(self, size):
        self.attaches.append(size)

    def on_detach(self):
        self.detaches += 1

    def admit(self, pane, data, kind):
        if pane is PaneId.HOST_SHELL and self.shell_owner != "user":
            return Reason.INPUT_OWNER_MANAGER, "manager owns input"
        if len(data) > self.capacity - len(self.queued[pane]):
            return Reason.QUEUE_FULL, "no space"
        self.queued[pane].extend(data)
        self.frames[pane].append(bytes(data))
        return None

    def resize(self, pane, rows, cols):
        self.size = (pane, rows, cols)

    def set_focus(self, pane):
        self.focus = pane

    def takeover_request(self):
        return {"takeover_requested": True}

    def takeover_confirm(self):
        raise Held(Reason.TAKEOVER_HELD, "target unknown")

    def handoff(self):
        raise Held(Reason.HANDOFF_HELD, "no handoff")

    def shutdown_request(self):
        self.shutdown_token = "tok"
        return {"token": "tok", "active": []}

    def shutdown_confirm(self, token):
        if token != self.shutdown_token:
            raise Held(Reason.SHUTDOWN_TOKEN_MISMATCH, "stale")
        self.shutdown_confirmed = True
        return {"shutting_down": True}

    def confirm_boot(self, boot_id):
        raise Held(Reason.BOOT_CONFIRMATION_NOT_REQUIRED, "none")


class ServerFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw17-ui-")
        self.path = Path(self._dir.name) / "ui.sock"
        self.controller = FakeController()
        self.server = UiServer(self.path, self.controller, max_outbound=8 * MiB)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            with self.lock:
                self.server.poll(0.01)

    def tearDown(self):
        self.stop.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.server.close(flush_timeout=0.5)
        self.assertFalse(self.path.exists())
        self._dir.cleanup()

    def client(self, **kwargs):
        client = UiClient(self.path, timeout=10, **kwargs)
        self.addCleanup(client.close)
        return client

    def raw(self):
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(5)
        sock.connect(str(self.path))
        self.addCleanup(sock.close)
        return sock

    def read_frames(self, sock, count=1, timeout=5):
        decoder, frames = ui_v1.FrameDecoder(), []
        deadline = time.monotonic() + timeout
        while len(frames) < count and time.monotonic() < deadline:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            frames.extend(decoder.feed(data))
        return frames

    def assert_server_alive(self):
        with self.client() as probe:
            self.assertIn("focus", probe.snapshot())


class ContractServerTests(ServerFixture):
    def test_socket_is_private(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(stat.S_ISSOCK(self.path.lstat().st_mode))

    def test_version_mismatch_is_rejected_and_closed(self):
        with self.assertRaises(ClientError) as caught:
            self.client(versions=(2,))
        self.assertEqual(caught.exception.header["reason"], "version_mismatch")
        self.assertEqual(caught.exception.header["supported"], [1])
        sock = self.raw()
        sock.sendall(ui_v1.encode_frame(ui_v1.hello("raw")))
        welcome, = self.read_frames(sock)
        self.assertEqual(welcome.header["version"], 1)
        sock.sendall(ui_v1.encode_frame({"v": 2, "type": "snapshot", "id": "a"}))
        reject, = self.read_frames(sock)
        self.assertEqual(reject.header["reason"], "version_mismatch")
        self.assertEqual(sock.recv(10), b"")  # closed by the server
        self.assertGreaterEqual(self.server.stats["version_rejects"], 2)
        self.assert_server_alive()

    def test_hello_required_and_framing_errors_close_only_that_connection(self):
        sock = self.raw()
        sock.sendall(ui_v1.encode_frame({"v": 1, "type": "snapshot", "id": "a"}))
        reject, = self.read_frames(sock)
        self.assertEqual(reject.header["reason"], "hello_required")
        for garbage in (b"GARBAGE-GARBAGE", struct.pack(">4sII", ui_v1.MAGIC, 999999, 0),
                        struct.pack(">4sII", ui_v1.MAGIC, 2, ui_v1.MAX_DISCARD_PAYLOAD_BYTES + 1) + b"{}"):
            sock = self.raw()
            sock.sendall(garbage)
            reject, = self.read_frames(sock)
            self.assertEqual(reject.header["reason"], "protocol_error")
            self.assertEqual(sock.recv(10), b"")
        self.assert_server_alive()

    def test_unknown_type_and_invalid_fields_answer_without_closing(self):
        client = self.client()
        client.send_frame({"v": 1, "type": "teleport", "id": "t1"})
        result = client.next_frame()
        self.assertEqual((result.header["id"], result.header["reason"]), ("t1", "unsupported_type"))
        bad = client.request(ClientType.RESIZE, rows=0, cols=10)
        self.assertEqual(bad["reason"], "invalid_message")
        self.assertIn("focus", client.snapshot())

    def test_input_requires_attachment_and_single_attached_client(self):
        first, second = self.client(), self.client()
        refused = first.paste(PaneId.MANAGER_OMP, b"x")
        self.assertEqual(refused["reason"], "not_attached")
        attached = first.attach((24, 80))
        self.assertTrue(attached["ok"])
        self.assertEqual(self.controller.attaches, [(24, 80)])
        self.assertEqual(second.attach()["reason"], "attached_elsewhere")
        first.pump(0.2)
        replay = [f for f in first.displays if f.header.get("replay")]
        self.assertEqual([ui_v1.decode_display(f).data for f in replay], [b"retained"])
        self.assertEqual(first.detach()["detached"], True)
        self.assertTrue(second.attach()["ok"])

    def test_paste_boundary_and_oversize_are_all_or_nothing_with_reason(self):
        client = self.client()
        client.attach()
        exact = b"a" * (2 * MiB)
        self.assertTrue(client.paste(PaneId.WORKER_OMP, exact)["ok"])
        self.assertEqual(self.controller.frames[PaneId.WORKER_OMP], [exact])
        for size in (2 * MiB + 1, 3 * MiB, 5 * MiB):  # buffered and discarded paths
            result = client.request(ClientType.PASTE, b"b" * size, pane=PaneId.MANAGER_OMP.value, timeout=30)
            self.assertEqual((result["ok"], result["reason"]), (False, "paste_too_large"), size)
            self.assertIn(str(size), result["detail"])
        self.assertEqual(self.controller.frames[PaneId.MANAGER_OMP], [])
        self.assertEqual(len(self.controller.queued[PaneId.MANAGER_OMP]), 0)
        # Connection remains usable after the oversized frames.
        self.assertTrue(client.input(PaneId.MANAGER_OMP, b"ok")["ok"])

    def test_queue_space_shortfall_rejects_whole_frame(self):
        client = self.client()
        client.attach()
        self.assertTrue(client.paste(PaneId.HOST_SHELL, b"q" * (MiB + MiB // 2))["ok"])
        before = bytes(self.controller.queued[PaneId.HOST_SHELL])
        refused = client.paste(PaneId.HOST_SHELL, b"r" * MiB)
        self.assertEqual((refused["ok"], refused["reason"]), (False, "queue_full"))
        self.assertEqual(bytes(self.controller.queued[PaneId.HOST_SHELL]), before)
        self.assertNotIn(b"r", self.controller.queued[PaneId.HOST_SHELL])
        self.controller.shell_owner = "manager"
        held = client.input(PaneId.HOST_SHELL, b"x")
        self.assertEqual(held["reason"], "input_owner_manager")

    def test_client_disconnect_detaches_but_server_keeps_serving(self):
        client = self.client()
        client.attach()
        client.close()
        deadline = time.monotonic() + 3
        while self.controller.detaches == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.controller.detaches, 1)
        self.assertIsNone(self.server.attached)
        again = self.client()
        self.assertTrue(again.attach()["ok"])  # the lease was released, backend still serving
        # Abrupt close mid-frame (half a header) is also only a disconnect.
        sock = self.raw()
        sock.sendall(ui_v1.encode_frame(ui_v1.hello("half"))[:7])
        sock.close()
        self.assert_server_alive()

    def test_display_stream_goes_only_to_attached_client_in_order(self):
        watcher, attached = self.client(), self.client()
        attached.attach()
        with self.lock:
            for index in range(5):
                self.server.broadcast(self.controller.chunk(f"line{index}".encode()))
        deadline = time.monotonic() + 3
        while len([f for f in attached.displays if not f.header.get("replay")]) < 5 and time.monotonic() < deadline:
            attached.pump(0.05)
        live = [ui_v1.decode_display(f) for f in attached.displays if not f.header.get("replay")]
        self.assertEqual([c.sequence for c in live], [1, 2, 3, 4, 5])
        watcher.pump(0.2)
        self.assertEqual(watcher.displays, [])

    def test_takeover_handoff_shutdown_and_boot_passthrough(self):
        client = self.client()
        self.assertEqual(client.request(ClientType.TAKEOVER_REQUEST)["reason"], "not_attached")
        client.attach()
        self.assertTrue(client.request(ClientType.TAKEOVER_REQUEST)["shell"]["takeover_requested"])
        self.assertEqual(client.request(ClientType.TAKEOVER_CONFIRM)["reason"], "takeover_held")
        self.assertEqual(client.request(ClientType.HANDOFF)["reason"], "handoff_held")
        self.assertEqual(client.request(ClientType.FOCUS, pane="host_shell")["focus"], "host_shell")
        self.assertEqual(client.request(ClientType.CONFIRM_BOOT, boot_id="b")["reason"],
                         "boot_confirmation_not_required")
        self.assertEqual(client.request(ClientType.SHUTDOWN_CONFIRM, token="zzz")["reason"],
                         "shutdown_token_mismatch")
        token = client.request(ClientType.SHUTDOWN_REQUEST)["token"]
        self.assertFalse(self.controller.shutdown_confirmed)
        self.assertTrue(client.request(ClientType.SHUTDOWN_CONFIRM, token=token)["shutting_down"])

    def test_slow_client_is_cut_off_without_blocking_the_server(self):
        self.server.max_outbound = 256 * 1024
        slow = socket.socket(socket.AF_UNIX)
        slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        slow.connect(str(self.path))
        self.addCleanup(slow.close)
        slow.sendall(ui_v1.encode_frame(ui_v1.hello("slow")))
        slow.sendall(ui_v1.encode_frame(ui_v1.request(ClientType.ATTACH, "a")))
        deadline = time.monotonic() + 3
        while self.server.attached is None and time.monotonic() < deadline:
            time.sleep(0.01)
        with self.lock:
            for _ in range(64):
                self.server.broadcast(self.controller.chunk(b"x" * 16384))
        deadline = time.monotonic() + 3
        while self.server.attached is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(self.server.attached)
        self.assertEqual(self.controller.detaches, 1)
        self.assert_server_alive()

    def test_overflow_mid_frame_keeps_framing_and_delivers_closing_slow_client(self):
        """A partially written frame is completed, whole unsent frames are dropped, CLOSING slow_client is
        the last frame, and nothing is queued after it; a client that resumes reading decodes it cleanly."""
        self.server.max_outbound = 256 * 1024
        slow = socket.socket(socket.AF_UNIX)
        slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        slow.settimeout(5)
        slow.connect(str(self.path))
        self.addCleanup(slow.close)
        slow.sendall(ui_v1.encode_frame(ui_v1.hello("slow")))
        decoder, frames = ui_v1.FrameDecoder(), []
        while not frames:
            frames.extend(decoder.feed(slow.recv(65536)))
        self.assertEqual(frames[0].header["type"], "welcome")
        slow.sendall(ui_v1.encode_frame(ui_v1.request(ClientType.ATTACH, "a")))
        deadline = time.monotonic() + 3
        while self.server.attached is None and time.monotonic() < deadline:
            time.sleep(0.01)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.lock:
                connection = self.server.attached
                if connection is not None and not connection.outbound:
                    break
            time.sleep(0.01)
        payload = bytes(range(256)) * 39 + b"z"  # 9985 bytes
        with self.lock:  # the poll thread is held: this block alone drives writes
            self.assertIsNotNone(connection)
            self.assertEqual(len(connection.outbound), 0)
            connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            ends, total = set(), 0
            for _ in range(8):
                chunk = self.controller.chunk(payload)
                total += len(ui_v1.encode_display(chunk))
                ends.add(total)
                self.server.broadcast(chunk)
            self.server._write(connection)
            written = total - len(connection.outbound)
            self.assertTrue(0 < written and written not in ends,
                            f"precondition: a frame is partially written ({written} of {sorted(ends)})")
            while self.server.attached is not None:
                self.server.broadcast(self.controller.chunk(payload))
            self.assertTrue(connection.closing)
            closing_len = len(connection.outbound)
            # Nothing may be queued after CLOSING, whatever path tries.
            self.server._send(connection, ui_v1.encode_display(self.controller.chunk(b"late")))
            self.server._header(connection, ui_v1.result("x", True))
            self.assertEqual(len(connection.outbound), closing_len)
        received = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                data = slow.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            received.extend(decoder.feed(data))  # raises ProtocolError on corrupt framing
        types = [frame.header.get("type") for frame in received]
        self.assertEqual(types[-1], "closing", types[-5:])
        self.assertEqual(received[-1].header.get("reason"), "slow_client")
        self.assertEqual(types.count("closing"), 1)
        for frame in received:
            if frame.header.get("type") == "display" and not frame.header.get("replay"):
                self.assertEqual(frame.payload, payload)
        self.assertEqual(self.controller.detaches, 1)
        self.assert_server_alive()


if __name__ == "__main__":
    unittest.main()
