"""Explicit worker-role response boundary for a delivered OMP message.

The host supplies identity and evidence, but only an injected observer of the
worker assistant response may supply an execution decision or judgment. The
observer must use a public OMP response surface; mailbox processing is not a
decision. No model text is returned or persisted through this port.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol
from uuid import UUID
import time

from workbench.contracts.v1 import MAX_SAFE_INTEGER
from workbench.contracts.v1 import ActorRole
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, MailboxStatus


class WorkerRolePort(Protocol):
    def arm(self, stage: str, message: Any) -> None:
        """Capture the public response cursor before this message is delivered."""

    def observe(self, stage: str, message: Any, receipt: Any) -> Mapping[str, Any]:
        """Return parsed identity/decision only after a real assistant response."""


class G3WorkerResponsePort:
    """One-shot worker decision from allowlisted public G3 OMP events only."""

    _fields = frozenset({"stage", "kind", "task_id", "revision_id", "revision", "run_id",
                         "message_id", "delivery_attempt_id", "session_id",
                         "session_generation", "response_id", "decision"})

    def __init__(self, bridge: G3BridgeServer, *, timeout: float = 45):
        if timeout <= 0:
            raise ValueError("worker response timeout must be positive")
        self._bridge = bridge
        self._timeout = timeout
        self._armed: dict[tuple[str, str], tuple[int, float]] = {}
        self._response_ids: set[str] = set()

    def arm(self, stage: str, message: Any) -> None:
        if stage not in {"execute", "analysis"}:
            raise ValueError("unsupported worker response stage")
        key = (stage, _uuid(message.message_id, "message_id"))
        if key in self._armed:
            raise ValueError("worker response already armed")
        self._armed[key] = (self._bridge.event_cursor(), time.monotonic() + self._timeout)

    def observe(self, stage: str, message: Any, receipt: Any) -> Mapping[str, Any]:
        key = (stage, _uuid(message.message_id, "message_id"))
        try:
            cursor, deadline = self._armed.pop(key)
        except KeyError as exc:
            raise ValueError("worker response was not armed or was already observed") from exc
        if receipt.status is not MailboxStatus.OMP_PROCESSED or receipt.message_id != message.message_id:
            raise ValueError("worker message was not processed by OMP")
        expected = {"messageId": message.message_id,
                    "deliveryAttemptId": receipt.delivery_attempt_id,
                    "taskId": message.task_id, "revisionId": message.revision_id,
                    "runId": message.run_id, "sessionId": receipt.session_id,
                    "generation": receipt.session_generation}

        def next_event(names: tuple[str, ...], after: int) -> dict[str, Any]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("public worker response deadline expired")
            return self._bridge.wait_any_event(ActorRole.WORKER, names, expected,
                                               after_sequence=after, timeout=remaining)

        first = next_event(("assistant_message_end", "worker_response_rejected",
                            "delivery_processing_unknown", "delivery_omp_processed"), cursor)
        if first.get("name") != "assistant_message_end":
            raise ValueError("no validated public worker assistant response")
        response = first.get("workerResponse")
        if not isinstance(response, dict) or set(response) != self._fields:
            raise ValueError("public worker response fields are invalid")
        if response.get("kind") != ("task" if stage == "execute" else "question"):
            raise ValueError("public worker response kind mismatch")
        if response.get("revision_id") != message.revision_id:
            raise ValueError("public worker response revision mismatch")
        processed = next_event(("delivery_omp_processed", "delivery_processing_unknown",
                                "worker_response_rejected", "assistant_message_end"),
                               first["bridgeSequence"])
        if processed.get("name") != "delivery_omp_processed" or processed.get("workerResponseId") != response.get("response_id"):
            raise ValueError("public worker response has no linked delivery completion")
        verified = verified_worker_response(stage, message, receipt, {
            **response, "source": "omp_assistant_response",
            "assistant_event_sequence": first["bridgeSequence"],
            "delivery_event_sequence": processed["bridgeSequence"],
        })
        if verified["response_id"] in self._response_ids:
            raise ValueError("public worker response ID was already observed")
        self._response_ids.add(verified["response_id"])
        return verified


def _uuid(value: Any, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be a canonical UUID string")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical UUID string") from exc
    return value


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_SAFE_INTEGER:
        raise ValueError(f"{field} must be a positive safe integer")
    return value


def verified_worker_response(stage: str, message: Any, receipt: Any,
                             response: Mapping[str, Any]) -> dict[str, Any]:
    """Validate exact Task/revision/run/message/session binding; return no text."""
    if type(stage) is not str or stage not in {"execute", "analysis"} or not isinstance(response, Mapping):
        raise ValueError("unsupported or absent worker response")
    expected = {
        "stage": stage,
        "task_id": _uuid(message.task_id, "task_id"),
        "revision": _positive_integer(message.revision, "revision"),
        "run_id": _uuid(message.run_id, "run_id"),
        "message_id": _uuid(message.message_id, "message_id"),
        "delivery_attempt_id": _uuid(receipt.delivery_attempt_id, "delivery_attempt_id"),
        "session_id": _uuid(receipt.session_id, "session_id"),
        "session_generation": _positive_integer(receipt.session_generation, "session_generation"),
        "source": "omp_assistant_response",
    }
    for field in ("task_id", "run_id", "message_id", "delivery_attempt_id", "session_id"):
        _uuid(response.get(field), field)
    for field in ("revision", "session_generation"):
        _positive_integer(response.get(field), field)
    if (type(response.get("stage")) is not str or type(response.get("source")) is not str
            or any(response.get(key) != value for key, value in expected.items())):
        raise ValueError("worker response identity does not match delivered message")
    response_id = _uuid(response.get("response_id"), "response_id")
    sequence = _positive_integer(response.get("assistant_event_sequence"), "assistant_event_sequence")
    delivery_sequence = _positive_integer(response.get("delivery_event_sequence"), "delivery_event_sequence")
    if delivery_sequence <= sequence:
        raise ValueError("worker response must precede its exact public delivery event")
    decision = response.get("decision")
    allowed = {"execute", "hold"} if stage == "execute" else {"success", "failure", "indeterminate"}
    if type(decision) is not str or decision not in allowed:
        raise ValueError("worker response decision is unsupported")
    return {**expected, "response_id": response_id,
            "assistant_event_sequence": sequence,
            "delivery_event_sequence": delivery_sequence, "decision": decision}
