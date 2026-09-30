"""Independent L-CW17-CONTRACT check against the real backend (p27-cw17-test-01).

Expected outcomes derived from CW-17.md, SPEC "Paste" row, C-AC-19 and the
ui_v1 contract docstring before reading the implementation:
- ``hello`` with no common version -> ``reject``/``version_mismatch`` and close;
  after ``welcome`` every frame must carry the negotiated ``v`` or the
  connection is rejected and closed.
- Bad magic, out-of-bound header length, a payload length above the discard
  bound, or an invalid header is a protocol error that closes only that
  connection; the backend, the PTYs and other clients are unaffected.
- Input/paste larger than 2 MiB, or larger than the pane's free queue space,
  is rejected as a whole with a reason in the result; no prefix is delivered;
  ordinary input keeps working afterwards.
- A client that disconnects mid-frame or is SIGKILLed mid-stream only ends its
  connection/attachment; the backend and its PTYs keep their identity.

Unlike the worker fixture (FakeController), every case here runs against a
backend started through the real entrypoint with real Bash and OMP 18.2.10.
"""
import json
import os
import signal
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import unittest

from independent_support import (
    LiveBackend, PaneId, UiClient, finish, find_omp, kill_exact, settle, shell_view, start_no_attach, ticks,
    wait_client, wait_file,
)
from workbench.backend.client import ClientError
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType

OMP = find_omp()
MiB = 1024 * 1024
PREFIX = struct.Struct(">4sII")


class Raw:
    """A deliberately low-level ui_v1 peer for malformed traffic."""

    def __init__(self, path: Path, timeout: float = 10.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.decoder = ui_v1.FrameDecoder()
        self.frames: list[ui_v1.Frame] = []
        self.eof = False

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def frame(self, header: dict, payload: bytes = b"") -> None:
        self.send(ui_v1.encode_frame(header, payload))

    def read(self, timeout: float = 10.0) -> ui_v1.Frame | None:
        deadline = time.monotonic() + timeout
        while not self.frames and not self.eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(1 << 20)
            except socket.timeout:
                return None
            except OSError:
                data = b""
            if not data:
                self.eof = True
                break
            self.frames.extend(self.decoder.feed(data))
        return self.frames.pop(0) if self.frames else None

    def closed(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while not self.eof and time.monotonic() < deadline:
            if self.read(max(0.0, deadline - time.monotonic())) is None and not self.eof:
                return False
        return self.eof

    def welcome(self) -> dict:
        self.frame(ui_v1.hello("independent-raw"))
        frame = self.read()
        assert frame is not None and frame.header["type"] == "welcome", frame
        return frame.header

    def result(self, timeout: float = 10.0) -> dict:
        while True:
            frame = self.read(timeout)
            assert frame is not None, "no result frame"
            if frame.header["type"] in {"result", "reject", "closing"}:
                return frame.header

    def close(self) -> None:
        self.sock.close()


def core_identity(snapshot: dict) -> dict:
    return {"backend": snapshot["backend"]["process"],
            "panes": {k: (p["process"], p["session_id"], p["generation"]) for k, p in snapshot["panes"].items()},
            "alive": {k: p["alive"] for k, p in snapshot["panes"].items()}}


@unittest.skipUnless(OMP, "real OMP 18.2.10 is required")
class IndependentLiveContractTests(unittest.TestCase):
    def open(self) -> tuple[LiveBackend, dict]:
        live = LiveBackend(OMP, path="/usr/bin:/bin")
        self.addCleanup(finish, self, live)
        snapshot = start_no_attach(self, live)
        self.sock_path = live.data / "ui.sock"
        return live, snapshot

    def raw(self) -> Raw:
        conn = Raw(self.sock_path)
        self.addCleanup(conn.close)
        return conn

    def attached_client(self) -> UiClient:
        client = UiClient(self.sock_path)
        self.addCleanup(client.close)
        self.assertTrue(client.attach((30, 100))["ok"])
        return client

    def assert_backend_intact(self, before: dict, client: UiClient | None = None) -> dict:
        if client is None:
            with UiClient(self.sock_path) as probe:
                after = probe.snapshot()
        else:
            after = client.snapshot()
            client.displays.clear()
        self.assertEqual(core_identity(after), core_identity(before))
        self.assertTrue(all(after["panes"][k]["alive"] for k in after["panes"]))
        return after

    # -- version ---------------------------------------------------------
    def test_version_mismatch_is_rejected_and_closes_only_that_connection(self):
        live, before = self.open()
        watcher = self.attached_client()
        cases = {
            "unsupported version": ui_v1.hello("x", (2,)),
            "empty list": {"type": "hello", "versions": []},
            "string version": {"type": "hello", "versions": ["1"]},
            "bool version": {"type": "hello", "versions": [True]},
        }
        for label, header in cases.items():
            with self.subTest(label):
                conn = self.raw()
                conn.frame(header)
                reply = conn.read()
                self.assertIsNotNone(reply)
                self.assertEqual((reply.header["type"], reply.header["reason"]), ("reject", "version_mismatch"))
                self.assertEqual(reply.header["supported"], [1])
                self.assertTrue(conn.closed())
        for label, v in {"frame v=2": 2, "frame without v": None, "frame v='1'": "1"}.items():
            with self.subTest(label):
                conn = self.raw()
                conn.welcome()
                header = {"type": "snapshot", "id": "a1"}
                if v is not None:
                    header["v"] = v
                conn.frame(header)
                reply = conn.result()
                self.assertEqual((reply["type"], reply["reason"]), ("reject", "version_mismatch"))
                self.assertTrue(conn.closed())
        with self.subTest("hello required first"):
            conn = self.raw()
            conn.frame(ui_v1.request(ClientType.SNAPSHOT, "a2"))
            reply = conn.result()
            self.assertEqual((reply["type"], reply["reason"]), ("reject", "hello_required"))
            self.assertTrue(conn.closed())
        with self.subTest("second hello"):
            conn = self.raw()
            conn.welcome()
            conn.frame({"v": 1, **ui_v1.hello("again"), "id": "h2"})
            reply = conn.result()
            self.assertEqual(reply["ok"], False)
            self.assertEqual(reply["reason"], "invalid_message")
        after = self.assert_backend_intact(before, watcher)
        self.assertTrue(after["attached"])
        self.assertGreaterEqual(after["ui"]["version_rejects"], 7)
        watcher.detach()

    # -- framing ---------------------------------------------------------
    def test_framing_errors_close_only_that_connection_and_invalid_fields_answer(self):
        live, before = self.open()
        watcher = self.attached_client()
        header = json.dumps({"type": "hello", "versions": [1]}).encode()
        broken = {
            "bad magic": PREFIX.pack(b"XXXX", len(header), 0) + header,
            "header too long": PREFIX.pack(b"WBUI", ui_v1.MAX_HEADER_BYTES + 1, 0),
            "header too short": PREFIX.pack(b"WBUI", 1, 0) + b"{",
            "payload beyond discard bound": PREFIX.pack(b"WBUI", len(header), ui_v1.MAX_DISCARD_PAYLOAD_BYTES + 1)
            + header,
            "non-UTF-8 header": PREFIX.pack(b"WBUI", 4, 0) + b"\xff\xfe{}",
            "JSON array header": PREFIX.pack(b"WBUI", 2, 0) + b"[]",
            "header without type": PREFIX.pack(b"WBUI", 2, 0) + b"{}",
            "hello with payload": ui_v1.encode_frame(ui_v1.hello("p"), b"x"),
        }
        for label, data in broken.items():
            with self.subTest(label):
                conn = self.raw()
                conn.send(data)
                reply = conn.result()
                self.assertEqual((reply["type"], reply["reason"]), ("reject", "protocol_error"), reply)
                self.assertTrue(conn.closed())
        for label, data in broken.items():
            if label.startswith("hello"):
                continue
            with self.subTest(f"after welcome: {label}"):
                conn = self.raw()
                conn.welcome()
                conn.send(data)
                reply = conn.result()
                self.assertEqual((reply["type"], reply["reason"]), ("reject", "protocol_error"), reply)
                self.assertTrue(conn.closed())
        conn = self.raw()
        conn.welcome()
        soft = {
            "unknown type": ({"v": 1, "type": "launch_missiles", "id": "s1"}, b"", "unsupported_type"),
            "missing id": ({"v": 1, "type": "snapshot"}, b"", "invalid_message"),
            "overlong id": ({"v": 1, "type": "snapshot", "id": "x" * 65}, b"", "invalid_message"),
            "snapshot with payload": ({"v": 1, "type": "snapshot", "id": "s2"}, b"zz", "invalid_message"),
            "resize rows 0": ({"v": 1, "type": "resize", "id": "s3", "rows": 0, "cols": 80}, b"", "invalid_message"),
            "resize cols 1001": ({"v": 1, "type": "resize", "id": "s4", "rows": 20, "cols": 1001}, b"",
                                 "invalid_message"),
            "input bad pane": ({"v": 1, "type": "input", "id": "s5", "pane": "nope"}, b"a", "invalid_message"),
            "focus bool pane": ({"v": 1, "type": "focus", "id": "s6", "pane": True}, b"", "invalid_message"),
            "input unattached": ({"v": 1, "type": "input", "id": "s7", "pane": "host_shell"}, b"a", "not_attached"),
            "attach while other attached": ({"v": 1, "type": "attach", "id": "s8"}, b"", "attached_elsewhere"),
            "shutdown_confirm without request": ({"v": 1, "type": "shutdown_confirm", "id": "s9", "token": "t"}, b"",
                                                 "shutdown_not_requested"),
            "confirm_boot not required": ({"v": 1, "type": "confirm_boot", "id": "sa", "boot_id": "b"}, b"",
                                          "boot_confirmation_not_required"),
        }
        for label, (hdr, payload, reason) in soft.items():
            with self.subTest(label):
                conn.frame(hdr, payload)
                reply = conn.result()
                self.assertEqual((reply["type"], reply.get("ok"), reply.get("reason")), ("result", False, reason),
                                 reply)
        conn.frame(ui_v1.request(ClientType.SNAPSHOT, "still-open"))
        reply = conn.result()
        self.assertEqual((reply["id"], reply["ok"]), ("still-open", True))
        after = self.assert_backend_intact(before, watcher)
        self.assertTrue(after["attached"])
        self.assertGreaterEqual(after["ui"]["protocol_errors"], 10)
        watcher.detach()

    # -- paste limits ----------------------------------------------------
    def test_paste_over_2mib_is_rejected_whole_with_reason_and_normal_input_survives(self):
        live, before = self.open()
        client = self.attached_client()
        marker = live.project / "m-big"
        command = f"printf P >> {marker}\r".encode()
        cases = {
            "host_shell 2MiB+1 (buffered path)": (PaneId.HOST_SHELL, ui_v1.MAX_PASTE_BYTES + 1),
            "host_shell 5MiB (discard path)": (PaneId.HOST_SHELL, 5 * MiB),
            "manager_omp 2MiB+1": (PaneId.MANAGER_OMP, ui_v1.MAX_PASTE_BYTES + 1),
            "worker_omp 5MiB": (PaneId.WORKER_OMP, 5 * MiB),
        }
        for label, (pane, size) in cases.items():
            for kind in (ClientType.PASTE, ClientType.INPUT):
                with self.subTest(label, kind=kind.value):
                    payload = command + b"#" * (size - len(command))
                    reply = client.request(kind, payload, pane=pane.value, timeout=30)
                    self.assertEqual((reply["ok"], reply["reason"]), (False, "paste_too_large"), reply)
                    self.assertIn(str(size), reply["detail"])
                    settle(client, 0.5)
                    view = client.snapshot()["panes"][pane.value]
                    self.assertEqual(view["queued_input_bytes"], 0)
                    self.assertEqual(view["dropped_input_bytes"], 0)
        settle(client, 1.5)
        self.assertFalse(marker.exists(), "a prefix of a rejected paste reached the shell")
        shell = shell_view(client.snapshot())
        self.assertNotEqual(shell["phase"], "unknown", shell)
        # Ordinary paste still works afterwards (same parent shell).
        ok = client.paste(PaneId.HOST_SHELL, command)
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(wait_file(marker, "P", 10, client), "P")
        self.assert_backend_intact(before, client)
        client.detach()

    def test_queue_full_on_a_stalled_real_pane_rejects_whole_frame_with_reason(self):
        live, before = self.open()
        client = self.attached_client()
        worker = before["panes"]["worker_omp"]["process"]
        descriptor = os.pidfd_open(worker["pid"])
        try:
            self.assertEqual(ticks(worker["pid"]), worker["start_ticks"])
            signal.pidfd_send_signal(descriptor, signal.SIGSTOP)  # owned OMP stops reading its PTY
        finally:
            os.close(descriptor)
        first = client.paste(PaneId.WORKER_OMP, b"a" * ui_v1.MAX_PASTE_BYTES)
        self.assertTrue(first["ok"], first)
        settle(client, 1.0)
        queued = client.snapshot()["panes"]["worker_omp"]["queued_input_bytes"]
        self.assertGreater(queued, ui_v1.MAX_PASTE_BYTES - 256 * 1024, "PTY absorbed an implausible amount")
        free = ui_v1.MAX_PASTE_BYTES - queued
        refused = client.paste(PaneId.WORKER_OMP, b"b" * (free + 1))
        self.assertEqual((refused["ok"], refused["reason"]), (False, "queue_full"), refused)
        self.assertIn(str(free + 1), refused["detail"])
        self.assertEqual(client.snapshot()["panes"]["worker_omp"]["queued_input_bytes"], queued,
                         "a rejected frame changed the queue")
        if free:
            fits = client.input(PaneId.WORKER_OMP, b"c" * free)
            self.assertTrue(fits["ok"], fits)
            self.assertEqual(client.snapshot()["panes"]["worker_omp"]["queued_input_bytes"], ui_v1.MAX_PASTE_BYTES)
        one = client.input(PaneId.WORKER_OMP, b"d")
        self.assertEqual((one["ok"], one["reason"]), (False, "queue_full"), one)
        # Other panes are unaffected by the stalled one.
        marker = live.project / "m-q"
        ok = client.input(PaneId.HOST_SHELL, f"printf Q >> {marker}\r".encode())
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(wait_file(marker, "Q", 10, client), "Q")
        client.detach()
        # The stopped worker is killed by the confirmed shutdown in finish().

    def test_accepted_multiline_paste_below_limit_is_delivered_completely_to_host_shell(self):
        live, before = self.open()
        client = self.attached_client()
        marker = live.project / "m-lines"
        line = f"printf L >> {marker}\r".encode()
        count = (256 * 1024) // len(line)
        payload = line * count
        reply = client.paste(PaneId.HOST_SHELL, payload)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["accepted_bytes"], len(payload))
        got = wait_file(marker, "L" * count, 90, client)
        view = shell_view(client.snapshot())
        self.assertEqual(len(got or ""), count,
                         f"accepted paste only partly delivered: {len(got or '')}/{count} lines; shell={view}")
        self.assertEqual(view["dropped"], 0, view)
        self.assertNotEqual(view["phase"], "unknown", view)
        client.detach()

    # -- disconnects -----------------------------------------------------
    def test_mid_stream_disconnect_and_sigkilled_client_keep_backend_and_ptys(self):
        live, before = self.open()
        marker = live.project / "m-cut"
        command = f"printf CUT >> {marker}\r".encode()

        # (a) half a prefix, then close.
        conn = self.raw()
        conn.welcome()
        conn.send(b"WBUI\x00\x00")
        conn.close()
        # (b) attached, 3 MiB paste announced, 1 MiB sent, then close.
        conn = self.raw()
        conn.welcome()
        conn.frame(ui_v1.request(ClientType.ATTACH, "at"))
        self.assertTrue(conn.result()["ok"])
        head = json.dumps({"v": 1, "type": "paste", "id": "p", "pane": "host_shell"}).encode()
        conn.send(PREFIX.pack(b"WBUI", len(head), 3 * MiB) + head + command + b"#" * (MiB - len(command)))
        conn.close()
        with UiClient(self.sock_path) as probe:
            wait_client(probe, lambda s: s["attached"] is False)
        # (c) a separate client process attached and streaming a large paste, SIGKILLed mid-frame.
        child_source = (
            "import sys, time, json, struct, socket\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2] / 'src')!r})\n"
            "from workbench.backend.client import UiClient\n"
            f"c = UiClient({str(self.sock_path)!r})\n"
            "assert c.attach((30, 100))['ok']\n"
            "head = json.dumps({'v': 1, 'type': 'paste', 'id': 'k', 'pane': 'host_shell'}).encode()\n"
            f"c.send_raw(struct.pack('>4sII', b'WBUI', len(head), 1024 * 1024) + head + {command!r})\n"
            "print('streaming', flush=True)\n"
            "while True:\n"
            "    c.send_raw(b'#' * 1024); time.sleep(0.05)\n"
        )
        child = subprocess.Popen([sys.executable, "-c", child_source], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
        child_ticks = ticks(child.pid)
        self.addCleanup(lambda: child.poll() is None and kill_exact(child.pid, child_ticks))
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.stderr.close)
        self.assertEqual(child.stdout.readline().strip(), b"streaming", child.stderr.read1() if child.poll() else b"")
        with UiClient(self.sock_path) as probe:
            self.assertTrue(wait_client(probe, lambda s: s["attached"] is True)["attached"])
        time.sleep(0.5)
        self.assertTrue(kill_exact(child.pid, child_ticks))
        self.assertEqual(child.wait(10), -9)

        with UiClient(self.sock_path) as probe:
            after = wait_client(probe, lambda s: s["attached"] is False)
        self.assertEqual(core_identity(after), core_identity(before))
        time.sleep(1.0)
        self.assertFalse(marker.exists(), "prefix of an unfinished frame was delivered")
        client = self.attached_client()
        view = shell_view(client.snapshot())
        self.assertEqual((view["queued"], view["dropped"]), (0, 0), view)
        self.assertNotEqual(view["phase"], "unknown", view)
        ok = client.input(PaneId.HOST_SHELL, command)
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(wait_file(marker, "CUT", 10, client), "CUT")
        self.assert_backend_intact(before, client)
        client.detach()

    def test_connection_cap_and_exclusive_attach_do_not_disturb_the_backend(self):
        live, before = self.open()
        holder = self.attached_client()
        other = UiClient(self.sock_path)
        self.addCleanup(other.close)
        refused = other.attach()
        self.assertEqual((refused["ok"], refused["reason"]), (False, "attached_elsewhere"))
        denied = other.input(PaneId.HOST_SHELL, b"echo no\r")
        self.assertEqual((denied["ok"], denied["reason"]), (False, "not_attached"))
        extra = []
        try:
            for _ in range(20):
                conn = Raw(self.sock_path)
                extra.append(conn)
            rejected = 0
            for conn in extra:
                try:
                    conn.frame(ui_v1.hello("cap"))
                except OSError:
                    pass
                frame = conn.read(5)
                if frame is not None and frame.header["type"] == "reject":
                    self.assertEqual(frame.header["reason"], "protocol_error")
                    rejected += 1
            self.assertGreater(rejected, 0, "no connection cap observed")
        finally:
            for conn in extra:
                conn.close()
        settle(holder, 0.5)
        after = self.assert_backend_intact(before, holder)
        self.assertTrue(after["attached"])
        with UiClient(self.sock_path) as late:
            self.assertEqual(late.snapshot()["backend"]["pid"], before["backend"]["pid"])
        holder.detach()
        try:
            other.detach()
        except ClientError:
            pass


if __name__ == "__main__":
    unittest.main()
