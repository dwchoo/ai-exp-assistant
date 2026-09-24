from __future__ import annotations

import json
from pathlib import Path
import unittest
from datetime import datetime

from workbench.contracts.v1 import (
    ActorRole,
    CommandState,
    CommandStatusEvent,
    ContractError,
    ControlEnvelope,
    DisplayChunk,
    MessageEvent,
    MessageKind,
    PaneId,
    new_identifier,
)


FIXTURES = Path(__file__).parent / "fixtures"


class ContractV1Tests(unittest.TestCase):
    def test_golden_envelope_round_trips_and_checks_session_generation(self) -> None:
        raw = json.loads((FIXTURES / "control-envelope-v1.json").read_text())
        envelope = ControlEnvelope.from_dict(
            raw,
            expected_session_id="30000000-0000-4000-8000-000000000001",
            expected_session_generation=2,
        )

        self.assertEqual(envelope.to_dict(), raw)
        self.assertEqual(ControlEnvelope.from_json(envelope.to_json()), envelope)
        self.assertIsInstance(envelope.event, CommandStatusEvent)
        self.assertEqual(envelope.event.state, CommandState.COMPLETED)
        self.assertEqual(envelope.event.exit_code, 0)

    def test_all_command_states_remain_distinct(self) -> None:
        raw = json.loads((FIXTURES / "control-envelope-v1.json").read_text())
        for state in CommandState:
            candidate = {**raw, "event": {**raw["event"], "state": state.value}}
            if state is not CommandState.COMPLETED:
                candidate["event"].pop("exitCode")
            envelope = ControlEnvelope.from_dict(candidate)
            self.assertEqual(envelope.event.state, state)

    def test_rejects_unsupported_version_role_and_session_generation(self) -> None:
        raw = json.loads((FIXTURES / "control-envelope-v1.json").read_text())
        cases = [
            ({**raw, "schemaVersion": 2}, {}),
            ({**raw, "senderRole": "orchestrator"}, {}),
            (raw, {"expected_session_id": "30000000-0000-4000-8000-000000000002"}),
            (raw, {"expected_session_generation": 3}),
        ]
        for candidate, expected in cases:
            with self.subTest(candidate=candidate, expected=expected):
                with self.assertRaises(ContractError):
                    ControlEnvelope.from_dict(candidate, **expected)

    def test_rejects_invalid_identifiers_unknown_fields_and_invalid_state_data(self) -> None:
        raw = json.loads((FIXTURES / "control-envelope-v1.json").read_text())
        malformed = [
            {**raw, "messageId": "not-a-uuid"},
            {**raw, "unexpected": True},
            {**raw, "event": {**raw["event"], "state": "queued"}},
            {**raw, "event": {**raw["event"], "exitCode": True}},
        ]
        for candidate in malformed:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ContractError):
                    ControlEnvelope.from_dict(candidate)

    def test_message_id_is_logical_and_attempt_ids_are_per_delivery(self) -> None:
        raw_attempts = json.loads((FIXTURES / "delivery-attempts-v1.json").read_text())
        first, retry = [ControlEnvelope.from_dict(raw) for raw in raw_attempts]

        self.assertEqual(first.message_id, retry.message_id)
        self.assertNotEqual(first.delivery_attempt_id, retry.delivery_attempt_id)
        self.assertEqual(first.event, retry.event)

    def test_message_payload_and_reply_reference_round_trip(self) -> None:
        message_id = new_identifier()
        envelope = ControlEnvelope(
            message_id=message_id,
            delivery_attempt_id=new_identifier(),
            sender_role=ActorRole.WORKER,
            session_id=new_identifier(),
            session_generation=1,
            task_id=new_identifier(),
            event=MessageEvent(
                MessageKind.ANSWER,
                {"text": "확인", "details": [1, True, None]},
                in_reply_to_message_id=new_identifier(),
            ),
        )

        self.assertEqual(ControlEnvelope.from_json(envelope.to_json()), envelope)

    def test_rejects_non_json_values_nested_inside_a_message_payload(self) -> None:
        raw = json.loads((FIXTURES / "delivery-attempts-v1.json").read_text())[0]
        raw["event"]["payload"]["metadata"] = datetime(1970, 1, 1)

        with self.assertRaises(ContractError):
            ControlEnvelope.from_dict(raw)

    def test_rejects_explicit_null_for_optional_wire_fields(self) -> None:
        command = json.loads((FIXTURES / "control-envelope-v1.json").read_text())
        message = json.loads((FIXTURES / "delivery-attempts-v1.json").read_text())[0]
        cases = [
            ("taskId", {**command, "taskId": None}),
            ("exitCode", {**command, "event": {**command["event"], "exitCode": None}}),
            (
                "inReplyToMessageId",
                {**message, "event": {**message["event"], "inReplyToMessageId": None}},
            ),
        ]
        for field, candidate in cases:
            with self.subTest(field=field):
                with self.assertRaises(ContractError):
                    ControlEnvelope.from_dict(candidate)

    def test_session_generation_must_fit_shared_js_safe_integer_range(self) -> None:
        raw = json.loads((FIXTURES / "control-envelope-v1.json").read_text())
        safe = {**raw, "sessionGeneration": 2**53 - 1}
        self.assertEqual(ControlEnvelope.from_dict(safe).session_generation, 2**53 - 1)

        with self.assertRaises(ContractError):
            ControlEnvelope.from_dict({**raw, "sessionGeneration": 2**53})

    def test_command_exit_code_fits_shared_safe_integer_range(self) -> None:
        raw = json.loads((FIXTURES / "control-envelope-v1.json").read_text())
        maximum = 2**53 - 1
        for exit_code in (maximum, -maximum):
            with self.subTest(exit_code=exit_code):
                candidate = {**raw, "event": {**raw["event"], "exitCode": exit_code}}
                envelope = ControlEnvelope.from_dict(candidate)
                self.assertEqual(ControlEnvelope.from_json(envelope.to_json()).event.exit_code, exit_code)
        for exit_code in (2**53, -(2**53), 2**53 + 2):
            with self.subTest(exit_code=exit_code):
                candidate = {**raw, "event": {**raw["event"], "exitCode": exit_code}}
                with self.assertRaises(ContractError):
                    ControlEnvelope.from_dict(candidate)

    def test_nested_payload_numbers_preserve_shared_numeric_boundary(self) -> None:
        raw = json.loads((FIXTURES / "delivery-attempts-v1.json").read_text())[0]
        maximum = 2**53 - 1
        safe_numbers = {
            "positive": maximum,
            "negative": -maximum,
            "fraction": 1.25,
            "largeId": str(2**53 + 1),
        }
        candidate = {**raw, "event": {**raw["event"], "payload": {"numbers": safe_numbers}}}
        envelope = ControlEnvelope.from_dict(candidate)
        self.assertEqual(ControlEnvelope.from_json(envelope.to_json()).to_dict(), candidate)

        for unsafe in (2**53, -(2**53), 2**53 + 2, float(2**53)):
            with self.subTest(unsafe=unsafe):
                invalid = {**raw, "event": {**raw["event"], "payload": {"numbers": [1.25, unsafe]}}}
                with self.assertRaises(ContractError):
                    ControlEnvelope.from_dict(invalid)
                with self.assertRaises(ContractError):
                    ControlEnvelope.from_json(json.dumps(invalid))

    def test_display_counters_fit_shared_safe_integer_range(self) -> None:
        maximum = 2**53 - 1
        fields = {
            "session_id": new_identifier(),
            "session_generation": maximum,
            "pane_id": PaneId.HOST_SHELL,
            "sequence": maximum,
            "data": b"x",
        }
        chunk = DisplayChunk(**fields)
        self.assertEqual((chunk.session_generation, chunk.sequence), (maximum, maximum))
        for field in ("session_generation", "sequence"):
            with self.subTest(field=field):
                with self.assertRaises(ContractError):
                    DisplayChunk(**{**fields, field: 2**53})

    def test_exit_code_is_only_valid_after_completion(self) -> None:
        for state in (CommandState.ACCEPTED, CommandState.STARTED, CommandState.UNKNOWN):
            with self.subTest(state=state):
                with self.assertRaises(ContractError):
                    CommandStatusEvent(new_identifier(), state, exit_code=0)

    def test_display_bytes_are_a_distinct_raw_byte_type(self) -> None:
        chunk = DisplayChunk(
            session_id=new_identifier(),
            session_generation=1,
            pane_id=PaneId.HOST_SHELL,
            sequence=1,
            data=b"\x1b[31m\xff\x00",
        )

        self.assertEqual(chunk.data, b"\x1b[31m\xff\x00")
        with self.assertRaises(ContractError):
            DisplayChunk(new_identifier(), 1, PaneId.HOST_SHELL, 1, "text")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
