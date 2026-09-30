"""Independent ui_v1 codec checks derived from the contract docstring (p27-cw17-test-01)."""
import json
import struct
import unittest

from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, Frame, FrameDecoder, ProtocolError
from workbench.contracts.v1 import ContractError, DisplayChunk, PaneId, new_identifier

PREFIX = struct.Struct(">4sII")


def raw_frame(header: dict, payload_length: int) -> bytes:
    encoded = json.dumps(header).encode()
    return PREFIX.pack(ui_v1.MAGIC, len(encoded), payload_length) + encoded


class IndependentFramingTests(unittest.TestCase):
    def test_byte_by_byte_feed_yields_identical_frames(self):
        frames = [({"type": "hello", "versions": [1]}, b""), ({"v": 1, "type": "paste", "id": "a", "pane": "host_shell"},
                                                             bytes(range(256)) * 3)]
        stream = b"".join(ui_v1.encode_frame(h, p) for h, p in frames)
        decoder = FrameDecoder()
        out = []
        for i in range(len(stream)):
            out.extend(decoder.feed(stream[i:i + 1]))
        self.assertEqual([(f.header, f.payload, f.discarded_bytes) for f in out], [(h, p, 0) for h, p in frames])

    def test_header_bound_is_inclusive_and_one_more_is_a_protocol_error(self):
        filler = "x" * (ui_v1.MAX_HEADER_BYTES - len(json.dumps({"type": "t", "p": ""}, separators=(",", ":"))))
        header = {"type": "t", "p": filler}
        encoded = ui_v1.encode_frame(header)
        self.assertEqual(PREFIX.unpack_from(encoded)[1], ui_v1.MAX_HEADER_BYTES)
        self.assertEqual(FrameDecoder().feed(encoded)[0].header, header)
        with self.assertRaises(ContractError):
            ui_v1.encode_frame({"type": "t", "p": filler + "x"})
        with self.assertRaises(ProtocolError):
            FrameDecoder().feed(PREFIX.pack(ui_v1.MAGIC, ui_v1.MAX_HEADER_BYTES + 1, 0))

    def test_buffered_bound_is_retained_and_one_more_is_discarded_with_size(self):
        header = {"v": 1, "type": "paste", "id": "p", "pane": "worker_omp"}
        for size, discarded in ((ui_v1.MAX_BUFFERED_PAYLOAD_BYTES, 0), (ui_v1.MAX_BUFFERED_PAYLOAD_BYTES + 1,
                                                                       ui_v1.MAX_BUFFERED_PAYLOAD_BYTES + 1)):
            with self.subTest(size=size):
                decoder = FrameDecoder()
                frames = decoder.feed(raw_frame(header, size))
                chunk = b"z" * 65536
                sent = 0
                while sent < size:
                    piece = chunk[:min(len(chunk), size - sent)]
                    frames.extend(decoder.feed(piece))
                    sent += len(piece)
                frames.extend(decoder.feed(ui_v1.encode_frame({"type": "after"})))
                self.assertEqual([f.header["type"] for f in frames], ["paste", "after"])
                self.assertEqual(frames[0].discarded_bytes, discarded)
                self.assertEqual(len(frames[0].payload), 0 if discarded else size)

    def test_discard_bound_is_inclusive_and_one_more_fails_before_any_payload(self):
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(raw_frame({"type": "paste"}, ui_v1.MAX_DISCARD_PAYLOAD_BYTES)), [])
        with self.assertRaises(ProtocolError):
            FrameDecoder().feed(raw_frame({"type": "paste"}, ui_v1.MAX_DISCARD_PAYLOAD_BYTES + 1))

    def test_failed_decoder_stays_failed(self):
        decoder = FrameDecoder()
        with self.assertRaises(ProtocolError):
            decoder.feed(b"NOPE" + b"\0" * 8)
        self.assertTrue(decoder.failed)
        with self.assertRaises(ProtocolError):
            decoder.feed(ui_v1.encode_frame({"type": "hello"}))


class IndependentSessionContractTests(unittest.TestCase):
    def test_negotiation_rejects_malformed_version_lists(self):
        self.assertEqual(ui_v1.negotiate([3, 1, 2]), 1)
        for bad in ([], None, "1", [1.0], [True], [False, 1], list(range(17)), {"1": 1}):
            with self.subTest(bad=bad):
                self.assertIsNone(ui_v1.negotiate(bad))

    def test_every_post_welcome_frame_needs_exact_integer_version(self):
        for value in (None, "1", 1.0, True, 2, 0):
            header = {"type": "snapshot", "id": "a"}
            if value is not None:
                header["v"] = value
            with self.subTest(v=value), self.assertRaises(ContractError):
                ui_v1.parse_client_frame(Frame(header, b""), 1)

    def test_payload_only_on_input_and_paste_and_discarded_frames_keep_their_size(self):
        for kind in ClientType:
            if kind is ClientType.HELLO:
                continue
            header = {"v": 1, "type": kind.value, "id": "i", "pane": "host_shell", "rows": 5, "cols": 5,
                      "token": "t", "boot_id": "b"}
            with self.subTest(kind=kind.value):
                if kind in {ClientType.INPUT, ClientType.PASTE}:
                    message = ui_v1.parse_client_frame(Frame(header, b"", 5 * 1024 * 1024), 1)
                    self.assertEqual((message.payload, message.discarded_bytes), (b"", 5 * 1024 * 1024))
                else:
                    with self.assertRaises(ContractError):
                        ui_v1.parse_client_frame(Frame(header, b"x"), 1)
                    with self.assertRaises(ContractError):
                        ui_v1.parse_client_frame(Frame(header, b"", 10), 1)

    def test_attach_size_shape_and_bounds(self):
        base = {"v": 1, "type": "attach", "id": "a"}
        good = ui_v1.parse_client_frame(Frame({**base, "size": {"rows": 1000, "cols": 1}}, b""), 1)
        self.assertEqual(good.fields["size"], (1000, 1))
        for size in ({"rows": 1}, {"rows": 1, "cols": 1, "x": 1}, {"rows": 1001, "cols": 1}, {"rows": "1", "cols": 1},
                     [1, 1]):
            with self.subTest(size=size), self.assertRaises(ContractError):
                ui_v1.parse_client_frame(Frame({**base, "size": size}, b""), 1)

    def test_display_frames_carry_raw_bytes_and_identity(self):
        chunk = DisplayChunk(new_identifier(), 3, PaneId.WORKER_OMP, 42, b"\x1b[31m\xff\x00raw")
        frame = FrameDecoder().feed(ui_v1.encode_display(chunk, replay=True))[0]
        self.assertTrue(frame.header["replay"])
        self.assertNotIn("data", frame.header)
        self.assertEqual(ui_v1.decode_display(frame), chunk)
        with self.assertRaises(ContractError):
            ui_v1.decode_display(Frame(frame.header, b"", 7))

    def test_reject_and_result_are_self_describing(self):
        reject = ui_v1.reject(ui_v1.Reason.VERSION_MISMATCH, "no")
        self.assertEqual((reject["type"], reject["reason"], reject["supported"]), ("reject", "version_mismatch", [1]))
        refused = ui_v1.result("r", False, reason=ui_v1.Reason.QUEUE_FULL, detail="d")
        self.assertEqual((refused["v"], refused["ok"], refused["reason"], refused["detail"]), (1, False, "queue_full", "d"))


if __name__ == "__main__":
    unittest.main()
