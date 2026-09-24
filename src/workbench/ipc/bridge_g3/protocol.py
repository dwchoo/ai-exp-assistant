"""Fail-closed policy primitives for the fixed OMP v18.2.10 G3 probe.

This is a feasibility-gate implementation. Durable delivery state belongs to the
storage port selected by CW-05; this module intentionally keeps only process-local
gate state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock

from workbench.contracts.v1 import (
    ActorRole,
    ContractError,
    ControlEnvelope,
    MessageEvent,
    MessageKind,
)


class DeliveryState(StrEnum):
    DEFERRED = "deferred"
    DELIVERING = "delivering"
    API_ACCEPTED = "api_accepted"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


def _message_identity(envelope: ControlEnvelope) -> tuple[str, int, str]:
    return (envelope.session_id, envelope.session_generation, envelope.message_id)


def _valid_direction(envelope: ControlEnvelope, target_role: ActorRole) -> bool:
    if not isinstance(envelope.event, MessageEvent):
        return False
    kind = envelope.event.message_kind
    if target_role is ActorRole.WORKER:
        return envelope.sender_role is ActorRole.MANAGER and kind in {
            MessageKind.TASK,
            MessageKind.QUESTION,
        }
    if target_role is ActorRole.MANAGER:
        return envelope.sender_role is ActorRole.WORKER and kind in {
            MessageKind.ANSWER,
            MessageKind.REPORT,
        }
    return False


@dataclass(slots=True)
class DeliveryLedger:
    """Validates one target session and refuses replay after an uncertain ACK."""

    role: ActorRole
    session_id: str
    generation: int
    _states: dict[tuple[str, int, str], DeliveryState] = field(default_factory=dict)
    _paused: bool = False
    _lock: RLock = field(default_factory=RLock, repr=False)

    def begin(
        self,
        encoded_envelope: str | bytes,
        *,
        idle: bool,
        has_pending_messages: bool,
        approval_pending: bool,
        editor_text: str | None,
        paused: bool = False,
    ) -> tuple[DeliveryState, ControlEnvelope | None]:
        envelope = ControlEnvelope.from_json(
            encoded_envelope,
            expected_session_id=self.session_id,
            expected_session_generation=self.generation,
        )
        if not _valid_direction(envelope, self.role):
            raise ContractError("message role/kind is not valid for the target OMP role")
        key = _message_identity(envelope)
        with self._lock:
            if paused:
                self._mark_paused()
            previous = self._states.get(key)
            if previous is DeliveryState.API_ACCEPTED:
                return DeliveryState.API_ACCEPTED, None
            if previous is DeliveryState.UNKNOWN:
                return DeliveryState.UNKNOWN, None
            if previous is DeliveryState.DELIVERING:
                return DeliveryState.DELIVERING, None
            if self._paused:
                # The caller may have received a deferred response, but this
                # message ID must not be sent again after explicit resume.
                self._states[key] = DeliveryState.UNKNOWN
                return DeliveryState.DEFERRED, None
            if (
                not idle
                or has_pending_messages
                or approval_pending
                or editor_text is None
                or editor_text != ""
            ):
                self._states[key] = DeliveryState.DEFERRED
                return DeliveryState.DEFERRED, None
            self._states[key] = DeliveryState.DELIVERING
            return DeliveryState.DELIVERING, envelope

    def finish(self, envelope: ControlEnvelope, *, api_accepted: bool | None) -> DeliveryState:
        key = _message_identity(envelope)
        state = (
            DeliveryState.API_ACCEPTED
            if api_accepted is True
            else DeliveryState.REJECTED
            if api_accepted is False
            else DeliveryState.UNKNOWN
        )
        with self._lock:
            if self._states.get(key) is DeliveryState.UNKNOWN and api_accepted is not True:
                return DeliveryState.UNKNOWN
            self._states[key] = state
        return state

    def pause(self) -> None:
        with self._lock:
            self._mark_paused()

    def _mark_paused(self) -> None:
        self._paused = True
        for key, state in self._states.items():
            if state in {DeliveryState.DEFERRED, DeliveryState.DELIVERING}:
                self._states[key] = DeliveryState.UNKNOWN

    def resume(self, *, reconciled: bool) -> bool:
        """Only an explicit post-reconciliation decision reopens new delivery."""
        with self._lock:
            if not reconciled:
                return False
            self._paused = False
            return True

    def state_for(self, envelope: ControlEnvelope) -> DeliveryState | None:
        with self._lock:
            return self._states.get(_message_identity(envelope))


__all__ = [
    "DeliveryLedger",
    "DeliveryState",
]
