"""ui_v1 framing, size limits, negotiation and message validation (fixture)."""
import json
import struct
import unittest

from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, Reason
from workbench.contracts.v1 import ContractError, DisplayChunk, PaneId, new_identifier


def raw_frame(header: bytes, payload_length: int, magic=ui_v1.MAGIC) -> bytes:
    return struct.pack(">4sII", magic, len(header), payload_length) + header


class FramingTests(unittest.TestCase):
    def test_roundtrip_is_byte_exact_and_incremental(self):
        payload = bytes(range(256)) * 10 + b"\x1b[31m\x00\xff"
        data = ui_v1.encode_frame({"v": 1, "type": "paste", "id": "a", "pane": "host_shell"}, payload)
        data += ui_v1.encode_frame({"v": 1, "type": "snapshot", "id": "b"})
        decoder = ui_v1.FrameDecoder()
        frames = []
        for index in range(len(data)):  # one byte at a time
            frames.extend(decoder.feed(data[index:index + 1]))
        self.assertEqual([f.header["id"] for f in frames], ["a", "b"])
        self.assertEqual(frames[0].payload, payload)
        self.assertEqual(frames[1].payload, b"")

    def test_bad_magic_header_bounds_and_json_are_protocol_errors(self):
        cases = [
            raw_frame(b"{}", 0, magic=b"NOPE"),
            struct.pack(">4sII", ui_v1.MAGIC, ui_v1.MAX_HEADER_BYTES + 1, 0),
            struct.pack(">4sII", ui_v1.MAGIC, 1, 0),
            raw_frame(b"not json", 0),
            raw_frame(b'["list"]', 0),
            raw_frame(b'{"no":"type"}', 0),
            struct.pack(">4sII", ui_v1.MAGIC, 20, ui_v1.MAX_DISCARD_PAYLOAD_BYTES + 1),
        ]
        for data in cases:
            decoder = ui_v1.FrameDecoder()
            with self.assertRaises(ui_v1.ProtocolError):
                decoder.feed(data)
            self.assertTrue(decoder.failed)
            with self.assertRaises(ui_v1.ProtocolError):
                decoder.feed(b"")

    def test_oversized_payload_is_consumed_not_retained_and_stream_resyncs(self):
        size = ui_v1.MAX_BUFFERED_PAYLOAD_BYTES + 5
        header = json.dumps({"v": 1, "type": "paste", "id": "big", "pane": "manager_omp"}).encode()
        decoder = ui_v1.FrameDecoder()
        frames = decoder.feed(raw_frame(header, size))
        self.assertEqual(frames, [])
        block = b"z" * 65536
        remaining = size
        while remaining:
            take = min(len(block), remaining)
            frames.extend(decoder.feed(block[:take]))
            remaining -= take
        frames.extend(decoder.feed(ui_v1.encode_frame({"v": 1, "type": "snapshot", "id": "next"})))
        self.assertEqual(len(frames), 2)
        self.assertEqual((frames[0].payload, frames[0].discarded_bytes), (b"", size))
        self.assertEqual(frames[1].header["id"], "next")
        self.assertLessEqual(len(decoder._buffer), 65536)

    def test_paste_limit_boundary_is_buffered_up_to_the_buffer_bound(self):
        exact = ui_v1.encode_frame({"v": 1, "type": "paste", "id": "x", "pane": "host_shell"},
                                   b"a" * ui_v1.MAX_PASTE_BYTES)
        frame, = ui_v1.FrameDecoder().feed(exact)
        self.assertEqual(len(frame.payload), ui_v1.MAX_PASTE_BYTES)
        self.assertEqual(frame.discarded_bytes, 0)
        self.assertGreater(ui_v1.MAX_BUFFERED_PAYLOAD_BYTES, ui_v1.MAX_PASTE_BYTES)

    def test_encoder_rejects_unbounded_or_non_json_headers(self):
        with self.assertRaises(ContractError):
            ui_v1.encode_frame({"type": "x", "pad": "a" * ui_v1.MAX_HEADER_BYTES})
        with self.assertRaises(ContractError):
            ui_v1.encode_frame({"type": "x", "bad": float("nan")})
        with self.assertRaises(ContractError):
            ui_v1.encode_frame({"no": "type"})
        with self.assertRaises(ContractError):
            ui_v1.encode_frame({"type": "x"}, "text")


class NegotiationAndValidationTests(unittest.TestCase):
    def parse(self, header, payload=b"", version=1, discarded=0):
        return ui_v1.parse_client_frame(ui_v1.Frame(header, payload, discarded), version)

    def test_version_negotiation(self):
        self.assertEqual(ui_v1.negotiate([1]), 1)
        self.assertEqual(ui_v1.negotiate([0, 1, 7]), 1)
        for bad in ([2], [], "1", [1.0], None, [True], list(range(20))):
            self.assertIsNone(ui_v1.negotiate(bad), bad)

    def test_hello_first_and_once(self):
        message = self.parse(ui_v1.hello("t"), version=None)
        self.assertIs(message.type, ClientType.HELLO)
        with self.assertRaises(ContractError):
            self.parse({"v": 1, "type": "snapshot", "id": "a"}, version=None)
        with self.assertRaises(ContractError):
            self.parse(ui_v1.hello("t"), version=1)

    def test_negotiated_version_is_required_on_every_frame(self):
        for value in (None, 2, "1", True):
            with self.assertRaises(ContractError):
                self.parse({"v": value, "type": "snapshot", "id": "a"})

    def test_field_validation(self):
        good = self.parse({"v": 1, "type": "resize", "id": "r", "rows": 24, "cols": 80})
        self.assertEqual((good.fields["pane"], good.fields["rows"], good.fields["cols"]), (None, 24, 80))
        bad_headers = [
            {"v": 1, "type": "resize", "id": "r", "rows": 0, "cols": 80},
            {"v": 1, "type": "resize", "id": "r", "rows": 24, "cols": 1001},
            {"v": 1, "type": "resize", "id": "r", "rows": True, "cols": 80},
            {"v": 1, "type": "focus", "id": "f", "pane": "tmux"},
            {"v": 1, "type": "paste", "id": "p"},
            {"v": 1, "type": "snapshot"},
            {"v": 1, "type": "snapshot", "id": "x" * 65},
            {"v": 1, "type": "snapshot", "id": "\n"},
            {"v": 1, "type": "attach", "id": "a", "size": {"rows": 1}},
            {"v": 1, "type": "shutdown_confirm", "id": "s"},
            {"v": 1, "type": "confirm_boot", "id": "b", "boot_id": ""},
            {"v": 1, "type": "nope", "id": "n"},
        ]
        for header in bad_headers:
            with self.assertRaises(ContractError, msg=header):
                self.parse(header)
        with self.assertRaises(ContractError):
            self.parse({"v": 1, "type": "snapshot", "id": "a"}, payload=b"x")
        paste = self.parse({"v": 1, "type": "paste", "id": "p", "pane": "worker_omp"}, payload=b"hi")
        self.assertEqual((paste.fields["pane"], paste.payload), (PaneId.WORKER_OMP, b"hi"))

    def test_every_client_type_has_a_documented_request_shape(self):
        extras = {"input": {"pane": "host_shell"}, "paste": {"pane": "host_shell"},
                  "resize": {"rows": 1, "cols": 1}, "focus": {"pane": "manager_omp"},
                  "restart_pane": {"pane": "worker_omp"}, "kill_pane": {"pane": "host_shell"},
                  "shutdown_confirm": {"token": "t"}, "confirm_boot": {"boot_id": "b"},
                  "resume": {"reconciled": True}}
        for kind in ClientType:
            if kind is ClientType.HELLO:
                continue
            message = self.parse(ui_v1.request(kind, "id1", **extras.get(kind.value, {})))
            self.assertIs(message.type, kind)

    def test_restart_pane_names_one_pane_and_carries_no_payload(self):
        for pane in PaneId:
            message = self.parse({"v": 1, "type": "restart_pane", "id": "r", "pane": pane.value})
            self.assertIs(message.type, ClientType.RESTART_PANE)
            self.assertIs(message.fields["pane"], pane)
        for header in ({"v": 1, "type": "restart_pane", "id": "r"},
                       {"v": 1, "type": "restart_pane", "id": "r", "pane": None},
                       {"v": 1, "type": "restart_pane", "id": "r", "pane": "tmux"},
                       {"v": 1, "type": "restart_pane", "id": "r", "pane": 1},
                       {"v": 1, "type": "restart_pane", "pane": "manager_omp"}):
            with self.assertRaises(ContractError, msg=header):
                self.parse(header)
        with self.assertRaises(ContractError):
            self.parse({"v": 1, "type": "restart_pane", "id": "r", "pane": "manager_omp"}, payload=b"x")
        self.assertEqual(ui_v1.request(ClientType.RESTART_PANE, "r", pane="manager_omp")["type"], "restart_pane")

    def test_kill_pane_names_one_pane_and_carries_no_payload(self):
        # C-D63: the backend accepts only host_shell, but the contract parses every PaneId so an
        # OMP pane is refused with a reason (pane_not_killable) rather than as an invalid message.
        for pane in PaneId:
            message = self.parse({"v": 1, "type": "kill_pane", "id": "k", "pane": pane.value})
            self.assertIs(message.type, ClientType.KILL_PANE)
            self.assertIs(message.fields["pane"], pane)
            self.assertEqual(set(message.fields), {"pane"})
        for header in ({"v": 1, "type": "kill_pane", "id": "k"},
                       {"v": 1, "type": "kill_pane", "id": "k", "pane": None},
                       {"v": 1, "type": "kill_pane", "id": "k", "pane": "tmux"},
                       {"v": 1, "type": "kill_pane", "id": "k", "pane": 1},
                       {"v": 1, "type": "kill_pane", "pane": "host_shell"},
                       {"v": 2, "type": "kill_pane", "id": "k", "pane": "host_shell"}):
            with self.assertRaises(ContractError, msg=header):
                self.parse(header)
        with self.assertRaises(ContractError):
            self.parse({"v": 1, "type": "kill_pane", "id": "k", "pane": "host_shell"}, payload=b"x")
        self.assertEqual(ui_v1.request(ClientType.KILL_PANE, "k", pane="host_shell"),
                         {"v": 1, "type": "kill_pane", "id": "k", "pane": "host_shell"})

    def test_restart_pane_host_shell_parses(self):
        message = self.parse({"v": 1, "type": "restart_pane", "id": "r", "pane": "host_shell"})
        self.assertEqual((message.type, message.fields), (ClientType.RESTART_PANE, {"pane": PaneId.HOST_SHELL}))

    def test_kill_refusal_reasons_are_contract_values(self):
        for name, value in (("PANE_NOT_KILLABLE", "pane_not_killable"), ("PANE_EXITED", "pane_exited"),
                            ("KILL_IN_PROGRESS", "kill_in_progress"), ("KILL_FAILED", "kill_failed")):
            self.assertEqual(Reason[name].value, value)

    def test_restart_refusal_reasons_are_contract_values(self):
        for name, value in (("PANE_ALIVE", "pane_alive"), ("PANE_NOT_RESTARTABLE", "pane_not_restartable"),
                            ("RESTART_IN_PROGRESS", "restart_in_progress"), ("RESTART_FAILED", "restart_failed")):
            self.assertEqual(Reason[name].value, value)

    def test_approval_decide_is_removed_and_pause_resume_shapes_stay(self):
        # C-D66: the manager's to_worker is the user's standing delegation; there is no UI approval.
        self.assertNotIn("approval_decide", {kind.value for kind in ClientType})
        approval_id = "6f1c1f4e-7d55-4b49-9d1c-0f5d8a3c1b2a"
        for header in ({"v": 1, "type": "approval_decide", "id": "a", "approval_id": approval_id,
                        "decision": "approve"},
                       {"v": 1, "type": "resume", "id": "r"},
                       {"v": 1, "type": "resume", "id": "r", "reconciled": "true"},
                       {"v": 1, "type": "resume", "id": "r", "reconciled": 1}):
            with self.assertRaises(ContractError, msg=header):
                self.parse(header)
        self.assertEqual(self.parse({"v": 1, "type": "pause", "id": "p"}).fields, {})
        self.assertEqual(self.parse({"v": 1, "type": "resume", "id": "r", "reconciled": False}).fields,
                         {"reconciled": False})
        with self.assertRaises(ContractError):
            self.parse({"v": 1, "type": "pause", "id": "p"}, payload=b"x")
        self.assertFalse(hasattr(ui_v1, "APPROVAL_DECISIONS"))

    def test_automation_refusal_reasons_are_contract_values_and_approval_reasons_are_gone(self):
        for name, value in (("HOST_SHELL_AUTOMATION", "host_shell_automation"),
                            ("RESUME_NOT_RECONCILED", "resume_not_reconciled")):
            self.assertEqual(Reason[name].value, value)
        values = {reason.value for reason in Reason}
        for gone in ("approval_unknown", "approval_not_pending", "approval_failed", "automation_paused",
                     "automation_unavailable"):
            self.assertNotIn(gone, values)

    def test_task_and_worker_state_values_are_contract_values(self):
        # C-D66: the snapshot's task and worker (U5 renders these instead of approval cards).
        self.assertEqual(ui_v1.WORKER_STATES, ("idle", "busy"))
        self.assertEqual(ui_v1.TASK_STATUSES, ("dispatched", "starting", "running", "waiting_report", "held",
                                               "cancelling", "finished", "blocked", "closed"))
        self.assertEqual(ui_v1.TASK_KINDS, ("experiment", "work"))

    def test_automation_state_values_are_contract_values(self):
        # CW-18 U3: the published automation state and the manager turn interruption (U5 renders these).
        self.assertEqual(ui_v1.AUTOMATION_STATES, ("idle", "active", "held", "pausing", "paused", "resuming"))
        self.assertEqual(ui_v1.INTERRUPTION_STATES, ("none", "not_needed", "requesting", "requested", "confirmed",
                                                     "request_failed", "unknown"))
        self.assertNotIn("not_configured", ui_v1.AUTOMATION_STATES)

    def test_results_and_rejects_carry_reasons(self):
        result = ui_v1.result("x", False, reason=Reason.PASTE_TOO_LARGE, detail="d")
        self.assertEqual((result["ok"], result["reason"], result["detail"]), (False, "paste_too_large", "d"))
        reject = ui_v1.reject(Reason.VERSION_MISMATCH, "no")
        self.assertEqual((reject["type"], reject["supported"]), ("reject", [1]))


class DisplayTests(unittest.TestCase):
    def test_display_chunk_roundtrip_uses_raw_payload(self):
        chunk = DisplayChunk(new_identifier(), 1, PaneId.HOST_SHELL, 7, b"\x1b[2J\xff\x00")
        frame, = ui_v1.FrameDecoder().feed(ui_v1.encode_display(chunk, replay=True))
        self.assertTrue(frame.header["replay"])
        self.assertNotIn("data", frame.header)
        self.assertEqual(ui_v1.decode_display(frame), chunk)
        with self.assertRaises(ContractError):
            ui_v1.decode_display(ui_v1.Frame({"type": "display", "pane": "host_shell", "session_id": "x",
                                              "generation": 1, "sequence": 1}, b""))
        with self.assertRaises(ContractError):
            ui_v1.encode_display(b"raw")


if __name__ == "__main__":
    unittest.main()
