from __future__ import annotations

import unittest
from dataclasses import replace

from workbench.contracts.v1 import ActorRole, ContractError, ControlEnvelope, MessageEvent, MessageKind, new_identifier
from workbench.ipc.bridge_g3.protocol import (
    DeliveryLedger,
    DeliveryState,
)


def message(
    *,
    session_id: str,
    generation: int,
    sender: ActorRole = ActorRole.MANAGER,
    kind: MessageKind = MessageKind.TASK,
    message_id: str | None = None,
    attempt_id: str | None = None,
) -> ControlEnvelope:
    return ControlEnvelope(
        message_id=message_id or new_identifier(),
        delivery_attempt_id=attempt_id or new_identifier(),
        sender_role=sender,
        session_id=session_id,
        session_generation=generation,
        task_id=new_identifier(),
        revision_id=new_identifier(),
        run_id=new_identifier(),
        event=MessageEvent(kind, {"text": "fixture"}),
    )


class DeliveryLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = new_identifier()
        self.ledger = DeliveryLedger(ActorRole.WORKER, self.session_id, 3)
        self.envelope = message(session_id=self.session_id, generation=3)

    def begin(self, envelope: ControlEnvelope, **state: object):
        inputs = dict(
            encoded_envelope=envelope.to_json(),
            idle=state.get("idle", True),
            has_pending_messages=state.get("pending", False),
            approval_pending=state.get("approval", False),
            editor_text=state.get("editor", ""),
        )
        if "paused" in state:
            inputs["paused"] = state["paused"]
        return self.ledger.begin(**inputs)

    def test_accepted_message_id_is_not_replayed_on_new_attempt(self) -> None:
        state, accepted = self.begin(self.envelope)
        self.assertEqual(state, DeliveryState.DELIVERING)
        self.assertIsNotNone(accepted)
        self.assertEqual(self.ledger.finish(self.envelope, api_accepted=True), DeliveryState.API_ACCEPTED)

        retry = replace(self.envelope, delivery_attempt_id=new_identifier())
        state, replay = self.begin(retry)
        self.assertEqual(state, DeliveryState.API_ACCEPTED)
        self.assertIsNone(replay)

    def test_unknown_ack_result_is_not_replayed(self) -> None:
        self.begin(self.envelope)
        self.assertEqual(self.ledger.finish(self.envelope, api_accepted=None), DeliveryState.UNKNOWN)

        retry = replace(self.envelope, delivery_attempt_id=new_identifier())
        state, replay = self.begin(retry)
        self.assertEqual(state, DeliveryState.UNKNOWN)
        self.assertIsNone(replay)

    def test_session_generation_and_message_direction_are_checked(self) -> None:
        wrong_generation = replace(self.envelope, session_generation=4)
        with self.assertRaises(ContractError):
            self.begin(wrong_generation)

        wrong_direction = message(
            session_id=self.session_id,
            generation=3,
            sender=ActorRole.WORKER,
            kind=MessageKind.ANSWER,
        )
        with self.assertRaises(ContractError):
            self.begin(wrong_direction)

    def test_unknown_busy_pending_approval_and_composer_state_fail_closed(self) -> None:
        states = (
            {"idle": False},
            {"pending": True},
            {"approval": True},
            {"editor": None},
            {"editor": "draft"},
        )
        for state in states:
            with self.subTest(state=state):
                envelope = message(session_id=self.session_id, generation=3)
                delivery, candidate = self.begin(envelope, **state)
                self.assertEqual(delivery, DeliveryState.DEFERRED)
                self.assertIsNone(candidate)

    def test_paused_automatic_delivery_is_deferred_even_when_omp_is_idle(self) -> None:
        delivery, candidate = self.begin(self.envelope, paused=True)
        self.assertEqual(delivery, DeliveryState.DEFERRED)
        self.assertIsNone(candidate)

    def test_a_paused_request_has_no_automatic_replay_after_resume(self) -> None:
        delivery, candidate = self.begin(self.envelope, paused=True)
        self.assertEqual(delivery, DeliveryState.DEFERRED)
        self.assertIsNone(candidate)
        retry = replace(self.envelope, delivery_attempt_id=new_identifier())
        delivery, candidate = self.begin(retry, paused=False)
        self.assertEqual(delivery, DeliveryState.UNKNOWN)
        self.assertIsNone(candidate)

    def test_a_pre_pause_deferred_request_is_not_replayed_after_resume(self) -> None:
        delivery, candidate = self.begin(self.envelope, idle=False)
        self.assertEqual(delivery, DeliveryState.DEFERRED)
        self.assertIsNone(candidate)
        self.ledger.pause()
        retry = replace(self.envelope, delivery_attempt_id=new_identifier())
        delivery, candidate = self.begin(retry, paused=False)
        self.assertEqual(delivery, DeliveryState.UNKNOWN)
        self.assertIsNone(candidate)


if __name__ == "__main__":
    unittest.main()
