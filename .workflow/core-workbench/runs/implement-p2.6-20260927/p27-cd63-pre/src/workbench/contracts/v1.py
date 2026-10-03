"""Version 1 control and terminal-display contracts.

IDs are canonical UUID strings. Producers create IDs with :func:`new_identifier`.
``message_id`` identifies one logical message and stays the same across retries;
``delivery_attempt_id`` identifies one physical delivery attempt and changes on
each retry. The contract does not implement delivery or deduplication.

PTY master bytes use :class:`DisplayChunk` and are never serialized inside a
control envelope. A command state is evidence about command execution, not proof
that the user's task is complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
from typing import TypeAlias
from uuid import UUID, uuid4


WIRE_VERSION = 1
MAX_SAFE_INTEGER = (1 << 53) - 1


class ContractError(ValueError):
    """Raised when data does not satisfy the supported wire contract."""


class ActorRole(StrEnum):
    MANAGER = "manager"
    WORKER = "worker"
    WORKBENCH = "workbench"
    HOST_SHELL = "host_shell"


class MessageKind(StrEnum):
    TASK = "task"
    QUESTION = "question"
    ANSWER = "answer"
    REPORT = "report"


class CommandState(StrEnum):
    ACCEPTED = "accepted"
    STARTED = "started"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class PaneId(StrEnum):
    MANAGER_OMP = "manager_omp"
    WORKER_OMP = "worker_omp"
    HOST_SHELL = "host_shell"


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
Identifier: TypeAlias = str


def new_identifier() -> Identifier:
    """Create a canonical UUIDv4 string for any contract ID field."""
    return str(uuid4())


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a UUID string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ContractError(f"{field} must be a UUID string") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ContractError(f"{field} must use canonical lowercase UUID form")
    return canonical


def _safe_integer(value: object, field: str) -> int:
    if type(value) is not int or abs(value) > MAX_SAFE_INTEGER:
        raise ContractError(f"{field} must be an integer within the shared safe range")
    return value


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value < 1 or value > MAX_SAFE_INTEGER:
        raise ContractError(f"{field} must be a positive integer")
    return value


def _json_value(value: object, field: str = "payload") -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return _safe_integer(value, field)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{field} must contain only finite JSON numbers")
        if value.is_integer() and abs(value) > MAX_SAFE_INTEGER:
            raise ContractError(f"{field} contains an integer-valued number outside the shared safe range")
        return value
    if isinstance(value, list):
        return [_json_value(item, field) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ContractError(f"{field} object keys must be strings")
        return {key: _json_value(item, field) for key, item in value.items()}
    raise ContractError(f"{field} must contain JSON-compatible values")


def _exact_keys(value: object, required: set[str], optional: set[str] = frozenset()) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ContractError("object expected")
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise ContractError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ContractError(f"unsupported fields: {', '.join(sorted(extra))}")
    return value


@dataclass(frozen=True, slots=True)
class MessageEvent:
    message_kind: MessageKind
    payload: dict[str, JsonValue]
    in_reply_to_message_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message_kind, MessageKind):
            raise ContractError("message_kind must be a supported message kind")
        object.__setattr__(self, "payload", _json_value(self.payload))
        if not isinstance(self.payload, dict):
            raise ContractError("payload must be a JSON object")
        if self.in_reply_to_message_id is not None:
            object.__setattr__(
                self,
                "in_reply_to_message_id",
                _identifier(self.in_reply_to_message_id, "inReplyToMessageId"),
            )

    def to_dict(self) -> dict[str, object]:
        payload = _json_value(self.payload)
        if not isinstance(payload, dict):
            raise ContractError("payload must be a JSON object")
        result: dict[str, object] = {
            "type": "message",
            "messageKind": self.message_kind.value,
            "payload": payload,
        }
        if self.in_reply_to_message_id is not None:
            result["inReplyToMessageId"] = self.in_reply_to_message_id
        return result


@dataclass(frozen=True, slots=True)
class CommandStatusEvent:
    command_id: str
    state: CommandState
    exit_code: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", _identifier(self.command_id, "commandId"))
        if not isinstance(self.state, CommandState):
            raise ContractError("state must be accepted, started, completed, or unknown")
        if self.exit_code is not None:
            if type(self.exit_code) is not int:
                raise ContractError("exit_code must be an integer")
            _safe_integer(self.exit_code, "exit_code")
            if self.state is not CommandState.COMPLETED:
                raise ContractError("exit_code is valid only for a completed command")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "type": "commandStatus",
            "commandId": self.command_id,
            "state": self.state.value,
        }
        if self.exit_code is not None:
            _safe_integer(self.exit_code, "exit_code")
            result["exitCode"] = self.exit_code
        return result


ControlEvent: TypeAlias = MessageEvent | CommandStatusEvent


@dataclass(frozen=True, slots=True)
class ControlEnvelope:
    message_id: str
    delivery_attempt_id: str
    sender_role: ActorRole
    session_id: str
    session_generation: int
    event: ControlEvent
    task_id: str | None = None
    revision_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("message_id", "delivery_attempt_id", "session_id"):
            object.__setattr__(self, field_name, _identifier(getattr(self, field_name), field_name))
        for field_name in ("task_id", "revision_id", "run_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _identifier(value, field_name))
        if not isinstance(self.sender_role, ActorRole):
            raise ContractError("sender_role must be a supported actor role")
        _positive_integer(self.session_generation, "sessionGeneration")
        if not isinstance(self.event, (MessageEvent, CommandStatusEvent)):
            raise ContractError("event must be a supported control event")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": WIRE_VERSION,
            "messageId": self.message_id,
            "deliveryAttemptId": self.delivery_attempt_id,
            "senderRole": self.sender_role.value,
            "sessionId": self.session_id,
            "sessionGeneration": self.session_generation,
            "event": self.event.to_dict(),
        }
        for field_name, wire_name in (
            ("task_id", "taskId"),
            ("revision_id", "revisionId"),
            ("run_id", "runId"),
        ):
            value = getattr(self, field_name)
            if value is not None:
                result[wire_name] = value
        return result

    def to_json(self) -> str:
        wire_value = _json_value(self.to_dict(), "control envelope")
        return json.dumps(wire_value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(
        cls,
        encoded: str | bytes,
        *,
        expected_session_id: str | None = None,
        expected_session_generation: int | None = None,
    ) -> ControlEnvelope:
        try:
            value = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise ContractError("invalid JSON control envelope") from exc
        return cls.from_dict(
            value,
            expected_session_id=expected_session_id,
            expected_session_generation=expected_session_generation,
        )

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        expected_session_id: str | None = None,
        expected_session_generation: int | None = None,
    ) -> ControlEnvelope:
        data = _exact_keys(
            value,
            {
                "schemaVersion",
                "messageId",
                "deliveryAttemptId",
                "senderRole",
                "sessionId",
                "sessionGeneration",
                "event",
            },
            {"taskId", "revisionId", "runId"},
        )
        version = data["schemaVersion"]
        if type(version) is not int or version != WIRE_VERSION:
            raise ContractError(f"unsupported schemaVersion: {version!r}")
        for field in ("taskId", "revisionId", "runId"):
            if field in data and data[field] is None:
                raise ContractError(f"{field} must be omitted instead of null")
        try:
            role = ActorRole(data["senderRole"])
        except (ValueError, TypeError) as exc:
            raise ContractError(f"unsupported senderRole: {data['senderRole']!r}") from exc
        session_id = _identifier(data["sessionId"], "sessionId")
        session_generation = _positive_integer(data["sessionGeneration"], "sessionGeneration")
        if expected_session_id is not None:
            expected_session_id = _identifier(expected_session_id, "expectedSessionId")
            if session_id != expected_session_id:
                raise ContractError("sessionId does not match the expected session")
        if expected_session_generation is not None:
            expected_session_generation = _positive_integer(
                expected_session_generation,
                "expectedSessionGeneration",
            )
            if session_generation != expected_session_generation:
                raise ContractError("sessionGeneration does not match the expected generation")

        event_data = data["event"]
        if not isinstance(event_data, dict):
            raise ContractError("event must be an object")
        event_type = event_data.get("type")
        if event_type == "message":
            event_fields = _exact_keys(
                event_data,
                {"type", "messageKind", "payload"},
                {"inReplyToMessageId"},
            )
            try:
                message_kind = MessageKind(event_fields["messageKind"])
            except (ValueError, TypeError) as exc:
                raise ContractError(f"unsupported messageKind: {event_fields['messageKind']!r}") from exc
            payload = _json_value(event_fields["payload"])
            if not isinstance(payload, dict):
                raise ContractError("payload must be a JSON object")
            if "inReplyToMessageId" in event_fields and event_fields["inReplyToMessageId"] is None:
                raise ContractError("inReplyToMessageId must be omitted instead of null")
            reply_to = event_fields.get("inReplyToMessageId")
            event: ControlEvent = MessageEvent(message_kind, payload, reply_to)
        elif event_type == "commandStatus":
            event_fields = _exact_keys(event_data, {"type", "commandId", "state"}, {"exitCode"})
            try:
                state = CommandState(event_fields["state"])
            except (ValueError, TypeError) as exc:
                raise ContractError(f"unsupported command state: {event_fields['state']!r}") from exc
            if "exitCode" in event_fields and event_fields["exitCode"] is None:
                raise ContractError("exitCode must be omitted instead of null")
            exit_code = event_fields.get("exitCode")
            event = CommandStatusEvent(event_fields["commandId"], state, exit_code)
        else:
            raise ContractError(f"unsupported event type: {event_type!r}")

        return cls(
            message_id=data["messageId"],
            delivery_attempt_id=data["deliveryAttemptId"],
            sender_role=role,
            session_id=session_id,
            session_generation=session_generation,
            event=event,
            task_id=data.get("taskId"),
            revision_id=data.get("revisionId"),
            run_id=data.get("runId"),
        )


@dataclass(frozen=True, slots=True)
class DisplayChunk:
    """Raw bytes read from one PTY master; never a control-envelope payload."""

    session_id: str
    session_generation: int
    pane_id: PaneId
    sequence: int
    data: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _identifier(self.session_id, "sessionId"))
        _positive_integer(self.session_generation, "sessionGeneration")
        if not isinstance(self.pane_id, PaneId):
            raise ContractError("pane_id must identify a supported PTY pane")
        _positive_integer(self.sequence, "sequence")
        if not isinstance(self.data, bytes):
            raise ContractError("display data must be raw bytes")
