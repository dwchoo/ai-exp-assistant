"""Version 1 backend<->UI contract over a private Unix stream socket.

This contract is separate from :mod:`workbench.contracts.v1` and ``ports_v2``;
it reuses their :class:`~workbench.contracts.v1.PaneId` and
:class:`~workbench.contracts.v1.DisplayChunk` unchanged.

Framing
-------
Every frame is ``MAGIC(4) | header_len(u32 BE) | payload_len(u32 BE) | header |
payload``. The header is a UTF-8 JSON object carrying ``"v"`` (the negotiated
contract version) and ``"type"``. The payload carries raw bytes only: PTY
display bytes on ``display`` frames and user bytes on ``input``/``paste``
frames. Raw PTY bytes are never embedded in JSON.

Limits: headers are at most :data:`MAX_HEADER_BYTES`. Payloads up to
:data:`MAX_BUFFERED_PAYLOAD_BYTES` are read into memory; larger payloads up to
:data:`MAX_DISCARD_PAYLOAD_BYTES` are consumed without being retained so that
the receiver can still answer the frame with a reason. Anything larger, a bad
magic, or an invalid header is a protocol error that closes only that
connection. Input and paste frames larger than :data:`MAX_PASTE_BYTES`, or larger
than the target pane's current free queue space, are rejected as a whole; no
prefix of such a frame is delivered.

Session
-------
The first client frame must be ``hello`` with ``versions`` (a list of integers).
The server answers ``welcome`` with the chosen version, or ``reject`` with
``version_mismatch`` and closes. After ``welcome`` every frame must carry the
negotiated ``"v"``; a mismatch is rejected and the connection is closed.
Client requests carry an ``id`` and receive exactly one ``result`` frame.

``attach`` makes the connection the single display/input client: the server
replays each pane's retained display tail (``replay: true``) and then streams
live ``display`` frames and ``state`` pushes. ``detach`` or closing the
connection ends only the attachment; it never stops the backend, a PTY, or a
running command. Focus, input ownership and control mode are backend state and
survive detach/reattach.

``restart_pane`` (attached client, ``pane`` field) starts a new session in an
exited pane: an exited ``manager_omp``/``worker_omp`` gets a new OMP session with
its start-up command (C-D62); an exited ``host_shell`` gets a new persistent
shell started like the first one (same shell, rcfile/hooks, environment and
cwd) with input owner ``user`` and a fresh control/handoff/takeover state
(C-D63). It is refused with ``pane_alive`` for a running pane,
``restart_in_progress`` while a restart (or, for the host shell, a kill) is
still being carried out, ``backend_shutdown`` during shutdown and
``restart_failed`` when the new process cannot be spawned (the pane stays
exited). The restarted pane streams under a new ``session_id``/``generation``.
``pane_not_restartable`` is kept as a value but no pane is refused with it now.

``kill_pane`` (attached client, ``pane`` field) force-kills the host shell at
once (C-D63; the UI asks the user to confirm before sending it): the parent
shell and every process in its session (its jobs and their process groups) get
SIGHUP/SIGTERM and then SIGKILL after a bounded wait; a process that moved to
another session (``setsid``) is not signalled. It is accepted whatever the
input owner is; the result reports ``input_owner``, ``manager_owned``,
``manager_command_in_flight`` and ``manager_command`` (a manager request in
flight is closed as ``outcome: unknown``, never a success), ``exit_status``,
``signalled``, ``survivors`` and ``left_session``. Each survivor is
``{pid, reason}``: a member that refuses the signal (for example a process now
running as another user under sudo/su/pkexec) is skipped and reported with
``permission_denied`` while the others are still signalled. A ``restart_pane``
result reports the members of the old session it could not end the same way
in ``survivors``. The pane is exited afterwards
and ``restart_pane`` starts a new shell. It is refused with
``pane_not_killable`` for an OMP pane (an OMP ends through the OMP itself),
``pane_exited`` when the host shell has already exited, ``kill_in_progress``
while a host shell kill or restart is being carried out, ``kill_failed`` when
the shell cannot be proven, does not end or the kill fails with an OS error,
and ``backend_shutdown`` during shutdown. Any request whose handler fails
unexpectedly is answered with ``internal_error``; the backend keeps serving. A successful ``restart_pane`` or ``kill_pane`` result is followed by a
``state`` push.

The ``host_shell`` pane in a snapshot carries ``alive``, ``exit_status``,
``input_owner``, ``manager_command_in_flight``, ``manager_command`` (the last
manager request closed by an exit or kill), ``restart`` and ``kill`` (the last
force-kill result) next to the ``shell`` control state. ``automation_hold`` is
a reason while Workbench types an experiment run's start into the idle host shell
(user input is then refused with ``host_shell_automation``), else null.
``operated_by`` (additive, C-D68) is ``"worker"`` while the worker OMP's
``terminal`` command holds the host shell (its start through its give-back),
else null; a client shows the worker as the host terminal's user then (the
shell itself is claimed on the manager side, so ``input_owner`` stays
``manager``). Experiment runs leave it null.

Task and worker (CW-18, C-D66; additive)
----------------------------------------
The manager's ``to_worker`` is the user's standing delegation (C-D66): there is
no UI approval. (``approval_decide`` and the ``approvals`` list of p2.7h were
removed before any release; a client sending ``approval_decide`` gets
``unsupported_type``.) A snapshot carries:

- ``task``: the current Task, else the most recent one, else null:
  ``{task_id, kind, status, summary, since, active, revision, run_id,
  runs_started, retry_limit, held_reason, closed_reason, cancel_requested,
  last_result}``. ``kind`` is one of :data:`TASK_KINDS`; ``status`` one of
  :data:`TASK_STATUSES` (``dispatched`` until its run starts, ``starting``,
  ``running``, ``waiting_report`` (an experiment's judgment and report),
  ``held`` (its run needs a report or a cancel), ``cancelling`` (waits for the
  host command to exit), ``finished`` (an experiment whose report reached the
  manager; it can be re-run), ``blocked`` (the worker reported blocked),
  ``closed`` with ``closed_reason`` such as ``done``, ``cancelled``,
  ``superseded_by_new_task``). ``held_reason`` says why a start or step waits,
  e.g. ``host_terminal_busy``. ``since`` is when ``status`` last changed.
- ``worker``: ``{state, task_id}``; ``state`` is one of :data:`WORKER_STATES`
  (``busy`` while a Task is active, then ``task_id`` names it).

Backend restart and reboot (CW-19, C-D71; additive)
--------------------------------------------------
- ``boot``: ``{boot_id, recorded_boot_id, confirmed_boot_id,
  confirmation_required, confirmed, reason, persisted}``. While
  ``confirmation_required`` is true (``reason`` ``reboot``,
  ``boot_marker_unknown``, ``boot_record_unreadable`` or ``reconcile_failed``)
  Workbench starts no automatic work; ``confirm_boot`` (the CLI
  ``confirm-boot``) needs the current ``boot_id`` and answers
  ``boot_confirmation_not_saved`` when the confirmation could not be stored
  (the hold stays). The product UI only shows the wait (C-D58).
- ``startup``: null or ``{classification, at, previous, run, probed,
  processes, outbox_lost, outbox_lost_count, survivors, survivor_stops, notice,
  notes}``. ``classification`` is ``fresh``, ``same_boot_clean_stop``,
  ``same_boot_unverified_stop``, ``same_boot_crash``, ``reboot`` or
  ``boot_unknown``. ``survivors`` are processes the previous backend left
  running: ``{survivor_id, name, pid, start_ticks, comm, stoppable, identity
  (verified|unverified), why_not, state (alive|ended|stopped|stop_unconfirmed),
  stop}``; Workbench never ends them by itself.
- ``holds``: ``[{reason, since, detail}]`` with reasons
  ``boot_confirmation_required``, ``metadata_unavailable``,
  ``model_hold:<role>``, ``shutdown_closing``.
- ``faults``: ``{metadata, record_error, model, raw_log}``: a metadata write
  failure (``{state, sources, since}``), the model state per role
  (``{state: error|ok, since, ...}``) and an experiment raw log that was not
  fully stored (``{run_id, stored_bytes, missing_bytes, cap_source,
  storage_error, text}``). Execution and observation continue in each case.

Recovery (C-D70; additive)
--------------------------
- ``recovery``: null (no watchdog) or ``{report_wait, watch}``. ``report_wait``
  is null or ``{count, reason, since}`` while worker reports to the manager
  have waited at least 30 s because the manager OMP's composer is not empty
  (``reason`` ``manager_editor_not_empty``; ``since`` is the wall-clock time
  the wait began), or, at once, while a report is queued with no manager OMP
  connected (``reason`` ``manager_session_not_connected``; it goes to the next
  manager session; with both kinds waiting this reason is reported). A client
  that does not know a ``reason`` treats it as the editor wait. It is null
  again once the reports were delivered. ``watch`` is null
  or ``{task_id, checks_sent, max_checks, stalled_notified, idle_since}`` for
  the open work Task the Workbench watchdog checks.
- A manager/worker pane's ``restart`` (and the snapshot record of restarts)
  also carries ``cause`` (``user_restart`` from ``restart_pane``,
  ``restart_worker`` from the manager's tool), ``requester`` (``user`` or
  ``manager``) and ``reason`` (the manager's text for ``restart_worker``, else
  null). A ``restart_worker`` restarts a live worker OMP; ``restart_pane``
  still restarts only an exited pane.

``pause`` (attached client, no fields) holds new automatic work. ``resume``
(attached client, ``reconciled`` boolean) resumes only with ``reconciled:
true``; ``false`` is refused with ``resume_not_reconciled``. Both answer with
``automation`` at once; the pause/resume itself finishes in the background and
the result arrives in a later ``state`` push.

Automation state (CW-18 U3; additive)
-------------------------------------
A snapshot's ``automation`` is ``{state, source, detail, paused, transition,
run, tick, review, interruption, resume, retry_limit, persistence_error}``:

- ``state`` is one of :data:`AUTOMATION_STATES`: ``idle`` (no run), ``active``,
  ``held`` (an experiment run's automatic work is held; ``detail`` and
  ``tick.problems`` say why, e.g. ``user_owner_or_control_hold``), ``pausing``,
  ``paused``, ``resuming``. ``paused`` is true from the pause request until a
  resume succeeds; while it is true no new automatic work starts, a
  ``to_worker`` is answered ``held:paused`` and host log/process collection
  goes on.
- ``run``: null or ``{kind, task_id, revision, run_id, bound, error}``.
- ``tick``: null or ``{outcome, problems, at}`` of the last lifecycle tick
  (``outcome`` ``admitted``|``held``|``idle``).
- ``review`` (the 60 s worker review): ``{applies, interval_seconds, status,
  reason, review_count, last_review_at, next_due_in_seconds, pending,
  coalesced_count, exit}``. ``applies`` is false for a free-work run (no host
  run). ``status`` is the scheduler's (``waiting``, ``dispatched``, ``delayed``
  with ``reason`` ``worker_busy_or_unknown`` or ``user_priority``, ``paused``,
  ``exited``, ...); ``coalesced_count`` counts reviews merged into the pending
  one while the worker was busy. ``exit`` is the observed run exit, or null.
- ``interruption`` (the manager turn stop requested on pause): ``{state,
  pause_id, requested_at, confirmed_at, manager_ack, worker_ack,
  unknown_tool_call_ids, error}``; ``state`` is one of
  :data:`INTERRUPTION_STATES` (``requested``, ``confirmed`` once the turn stop
  was observed, ``unknown``; ``not_needed`` without a bound run or when the
  manager was idle; ``requesting`` while the pause is being carried out;
  ``none`` when not paused).
- ``resume``: null or ``{outcome, reason, at}`` of the last resume
  (``outcome`` ``resumed``|``refused``; a refused resume stays paused and
  replays nothing).
- ``retry_limit``: re-runs allowed per Task (CW-13, 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import struct
from typing import Any

from .v1 import ContractError, DisplayChunk, PaneId


CONTRACT_NAME = "workbench.ui"
VERSION = 1
SUPPORTED_VERSIONS = (VERSION,)

MAGIC = b"WBUI"
_PREFIX = struct.Struct(">4sII")
PREFIX_BYTES = _PREFIX.size
MAX_HEADER_BYTES = 64 * 1024
MAX_PASTE_BYTES = 2 * 1024 * 1024
MAX_BUFFERED_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_DISCARD_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_ID_LENGTH = 64
MAX_TERMINAL_ROWS = 1000
MAX_TERMINAL_COLUMNS = 1000
AUTOMATION_STATES = ("idle", "active", "held", "pausing", "paused", "resuming")
INTERRUPTION_STATES = ("none", "not_needed", "requesting", "requested", "confirmed", "request_failed", "unknown")
WORKER_STATES = ("idle", "busy")
TASK_KINDS = ("experiment", "work")
TASK_STATUSES = ("dispatched", "starting", "running", "waiting_report", "held", "cancelling", "finished", "blocked",
                 "closed")


class ClientType(StrEnum):
    HELLO = "hello"
    ATTACH = "attach"
    DETACH = "detach"
    SNAPSHOT = "snapshot"
    INPUT = "input"
    PASTE = "paste"
    RESIZE = "resize"
    FOCUS = "focus"
    TAKEOVER_REQUEST = "takeover_request"
    TAKEOVER_CONFIRM = "takeover_confirm"
    HANDOFF = "handoff"
    SHUTDOWN_REQUEST = "shutdown_request"
    SHUTDOWN_CONFIRM = "shutdown_confirm"
    CONFIRM_BOOT = "confirm_boot"
    RESTART_PANE = "restart_pane"
    KILL_PANE = "kill_pane"
    PAUSE = "pause"
    RESUME = "resume"


class ServerType(StrEnum):
    WELCOME = "welcome"
    REJECT = "reject"
    RESULT = "result"
    DISPLAY = "display"
    STATE = "state"
    CLOSING = "closing"


class Reason(StrEnum):
    VERSION_MISMATCH = "version_mismatch"
    PROTOCOL_ERROR = "protocol_error"
    HELLO_REQUIRED = "hello_required"
    FRAME_TOO_LARGE = "frame_too_large"
    PASTE_TOO_LARGE = "paste_too_large"
    QUEUE_FULL = "queue_full"
    INVALID_MESSAGE = "invalid_message"
    UNSUPPORTED_TYPE = "unsupported_type"
    NOT_ATTACHED = "not_attached"
    ATTACHED_ELSEWHERE = "attached_elsewhere"
    PANE_UNAVAILABLE = "pane_unavailable"
    INPUT_OWNER_MANAGER = "input_owner_manager"
    INPUT_TARGET_UNKNOWN = "input_target_unknown"
    TAKEOVER_HELD = "takeover_held"
    HANDOFF_HELD = "handoff_held"
    SHUTDOWN_NOT_REQUESTED = "shutdown_not_requested"
    SHUTDOWN_TOKEN_MISMATCH = "shutdown_token_mismatch"
    BOOT_CONFIRMATION_NOT_REQUIRED = "boot_confirmation_not_required"
    BOOT_ID_MISMATCH = "boot_id_mismatch"
    BOOT_CONFIRMATION_NOT_SAVED = "boot_confirmation_not_saved"  # CW-19: the hold stays until it is durable
    BACKEND_NOT_READY = "backend_not_ready"
    SLOW_CLIENT = "slow_client"
    BACKEND_SHUTDOWN = "backend_shutdown"
    PANE_ALIVE = "pane_alive"
    PANE_NOT_RESTARTABLE = "pane_not_restartable"
    RESTART_IN_PROGRESS = "restart_in_progress"
    RESTART_FAILED = "restart_failed"
    PANE_NOT_KILLABLE = "pane_not_killable"
    PANE_EXITED = "pane_exited"
    KILL_IN_PROGRESS = "kill_in_progress"
    KILL_FAILED = "kill_failed"
    INTERNAL_ERROR = "internal_error"
    HOST_SHELL_AUTOMATION = "host_shell_automation"
    RESUME_NOT_RECONCILED = "resume_not_reconciled"


# Requests whose ``id`` is mandatory. ``hello`` is answered by welcome/reject.
_REQUEST_TYPES = frozenset(ClientType) - {ClientType.HELLO}
_PAYLOAD_TYPES = frozenset({ClientType.INPUT, ClientType.PASTE})


class ProtocolError(ContractError):
    """The byte stream cannot be trusted any more; close the connection."""


@dataclass(frozen=True, slots=True)
class Frame:
    header: dict[str, Any]
    payload: bytes
    # Size of a consumed payload that was too large to retain. When non-zero the
    # payload field is empty and the receiver must reject the frame as a whole.
    discarded_bytes: int = 0


def encode_frame(header: dict[str, Any], payload: bytes = b"") -> bytes:
    if not isinstance(header, dict) or not isinstance(header.get("type"), str):
        raise ContractError("frame header must be an object with a type")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ContractError("frame payload must be raw bytes")
    try:
        encoded = json.dumps(header, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("frame header must be JSON-compatible") from exc
    payload = bytes(payload)
    if len(encoded) > MAX_HEADER_BYTES:
        raise ContractError("frame header exceeds its bound")
    if len(payload) > MAX_DISCARD_PAYLOAD_BYTES:
        raise ContractError("frame payload exceeds its bound")
    return _PREFIX.pack(MAGIC, len(encoded), len(payload)) + encoded + payload


class FrameDecoder:
    """Incremental decoder. Oversized payloads are consumed, not retained."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._header: dict[str, Any] | None = None
        self._payload_length = 0
        self._discard_remaining = 0
        self._discard_total = 0
        self.failed = False

    def feed(self, data: bytes) -> list[Frame]:
        if self.failed:
            raise ProtocolError("decoder already failed")
        self._buffer.extend(data)
        frames: list[Frame] = []
        try:
            while True:
                frame = self._next()
                if frame is None:
                    return frames
                frames.append(frame)
        except ProtocolError:
            self.failed = True
            self._buffer.clear()
            raise

    def _next(self) -> Frame | None:
        if self._discard_remaining:
            take = min(self._discard_remaining, len(self._buffer))
            del self._buffer[:take]
            self._discard_remaining -= take
            if self._discard_remaining:
                return None
            header, self._header = self._header, None
            assert header is not None
            return Frame(header, b"", self._discard_total)
        if self._header is None:
            if len(self._buffer) < PREFIX_BYTES:
                return None
            magic, header_length, payload_length = _PREFIX.unpack_from(self._buffer)
            if magic != MAGIC:
                raise ProtocolError("bad frame magic")
            if header_length < 2 or header_length > MAX_HEADER_BYTES:
                raise ProtocolError("frame header length is out of bounds")
            if payload_length > MAX_DISCARD_PAYLOAD_BYTES:
                raise ProtocolError("frame payload length is out of bounds")
            if len(self._buffer) < PREFIX_BYTES + header_length:
                return None
            raw = bytes(self._buffer[PREFIX_BYTES:PREFIX_BYTES + header_length])
            del self._buffer[:PREFIX_BYTES + header_length]
            try:
                header = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ProtocolError("frame header is not UTF-8 JSON") from exc
            if not isinstance(header, dict) or not isinstance(header.get("type"), str):
                raise ProtocolError("frame header must be an object with a type")
            self._header, self._payload_length = header, payload_length
            if payload_length > MAX_BUFFERED_PAYLOAD_BYTES:
                self._discard_remaining = self._discard_total = payload_length
                return self._next()
        if len(self._buffer) < self._payload_length:
            return None
        payload = bytes(self._buffer[:self._payload_length])
        del self._buffer[:self._payload_length]
        header, self._header = self._header, None
        return Frame(header, payload)


def negotiate(versions: object) -> int | None:
    """Return the highest mutually supported version, or None."""
    if (not isinstance(versions, list) or not versions or len(versions) > 16
            or any(type(item) is not int for item in versions)):
        return None
    common = set(versions) & set(SUPPORTED_VERSIONS)
    return max(common) if common else None


def version_matches(header: dict[str, Any], version: int) -> bool:
    value = header.get("v")
    return type(value) is int and value == version


def hello(client: str, versions: tuple[int, ...] = SUPPORTED_VERSIONS) -> dict[str, Any]:
    return {"type": ClientType.HELLO.value, "versions": list(versions), "client": client}


@dataclass(frozen=True, slots=True)
class ClientMessage:
    type: ClientType
    id: str | None
    fields: dict[str, Any]
    payload: bytes
    discarded_bytes: int = 0


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LENGTH or not value.isprintable():
        raise ContractError("request id must be a short printable string")
    return value


def _pane(value: object) -> PaneId:
    try:
        return PaneId(value)
    except ValueError as exc:
        raise ContractError("pane must be a supported PaneId") from exc


def _dimension(value: object, field: str, limit: int) -> int:
    if type(value) is not int or value < 1 or value > limit:
        raise ContractError(f"{field} must be an integer between 1 and {limit}")
    return value


def parse_client_frame(frame: Frame, version: int | None) -> ClientMessage:
    """Validate one client frame. ``version`` is None before ``welcome``."""
    header = frame.header
    try:
        kind = ClientType(header.get("type"))
    except ValueError as exc:
        raise ContractError(f"unsupported client message type: {header.get('type')!r}") from exc
    if kind is ClientType.HELLO:
        if version is not None:
            raise ContractError("hello is only valid once")
        if frame.payload or frame.discarded_bytes:
            raise ContractError("hello carries no payload")
        if not isinstance(header.get("client", ""), str) or len(header.get("client", "")) > MAX_ID_LENGTH:
            raise ContractError("client name must be a short string")
        return ClientMessage(kind, None, {"versions": header.get("versions"),
                                          "client": header.get("client", "")}, b"")
    if version is None:
        raise ContractError("hello required")
    if not version_matches(header, version):
        raise ContractError("frame version does not match the negotiated version")
    request_id = _request_id(header.get("id"))
    if kind not in _PAYLOAD_TYPES and (frame.payload or frame.discarded_bytes):
        raise ContractError(f"{kind.value} carries no payload")
    fields: dict[str, Any] = {}
    if kind in _PAYLOAD_TYPES:
        fields["pane"] = _pane(header.get("pane"))
    elif kind is ClientType.RESIZE:
        fields["pane"] = _pane(header.get("pane")) if header.get("pane") is not None else None
        fields["rows"] = _dimension(header.get("rows"), "rows", MAX_TERMINAL_ROWS)
        fields["cols"] = _dimension(header.get("cols"), "cols", MAX_TERMINAL_COLUMNS)
    elif kind in {ClientType.FOCUS, ClientType.RESTART_PANE, ClientType.KILL_PANE}:
        fields["pane"] = _pane(header.get("pane"))
    elif kind is ClientType.ATTACH:
        size = header.get("size")
        if size is not None:
            if not isinstance(size, dict) or set(size) != {"rows", "cols"}:
                raise ContractError("attach size must be {rows, cols}")
            fields["size"] = (_dimension(size["rows"], "rows", MAX_TERMINAL_ROWS),
                              _dimension(size["cols"], "cols", MAX_TERMINAL_COLUMNS))
        else:
            fields["size"] = None
    elif kind is ClientType.SHUTDOWN_CONFIRM:
        fields["token"] = _request_id(header.get("token"))
    elif kind is ClientType.CONFIRM_BOOT:
        boot_id = header.get("boot_id")
        if not isinstance(boot_id, str) or not boot_id or len(boot_id) > 64:
            raise ContractError("boot_id must be a short string")
        fields["boot_id"] = boot_id
    elif kind is ClientType.RESUME:
        if type(header.get("reconciled")) is not bool:
            raise ContractError("resume needs reconciled (a boolean)")
        fields["reconciled"] = header["reconciled"]
    return ClientMessage(kind, request_id, fields, frame.payload, frame.discarded_bytes)


def request(kind: ClientType, request_id: str, **fields: Any) -> dict[str, Any]:
    return {"v": VERSION, "type": kind.value, "id": request_id, **fields}


def result(request_id: str | None, ok: bool, *, reason: Reason | None = None,
           detail: str | None = None, **fields: Any) -> dict[str, Any]:
    header: dict[str, Any] = {"v": VERSION, "type": ServerType.RESULT.value, "id": request_id, "ok": ok}
    if reason is not None:
        header["reason"] = reason.value
    if detail is not None:
        header["detail"] = detail
    header.update(fields)
    return header


def reject(reason: Reason, detail: str) -> dict[str, Any]:
    return {"type": ServerType.REJECT.value, "reason": reason.value, "detail": detail,
            "supported": list(SUPPORTED_VERSIONS)}


def encode_display(chunk: DisplayChunk, *, replay: bool = False) -> bytes:
    if not isinstance(chunk, DisplayChunk):
        raise ContractError("DisplayChunk required")
    header = {"v": VERSION, "type": ServerType.DISPLAY.value, "pane": chunk.pane_id.value,
              "session_id": chunk.session_id, "generation": chunk.session_generation,
              "sequence": chunk.sequence}
    if replay:
        header["replay"] = True
    return encode_frame(header, chunk.data)


def decode_display(frame: Frame) -> DisplayChunk:
    header = frame.header
    if header.get("type") != ServerType.DISPLAY.value or frame.discarded_bytes:
        raise ContractError("display frame expected")
    return DisplayChunk(session_id=header.get("session_id"), session_generation=header.get("generation"),
                        pane_id=_pane(header.get("pane")), sequence=header.get("sequence"),
                        data=frame.payload)


__all__ = [
    "CONTRACT_NAME", "VERSION", "SUPPORTED_VERSIONS", "MAGIC", "PREFIX_BYTES",
    "MAX_HEADER_BYTES", "MAX_PASTE_BYTES", "MAX_BUFFERED_PAYLOAD_BYTES",
    "MAX_DISCARD_PAYLOAD_BYTES", "AUTOMATION_STATES", "INTERRUPTION_STATES", "TASK_KINDS", "TASK_STATUSES", "WORKER_STATES", "version_matches", "ClientType", "ServerType", "Reason", "ProtocolError",
    "Frame", "FrameDecoder", "ClientMessage", "encode_frame", "negotiate", "hello",
    "parse_client_frame", "request", "result", "reject", "encode_display", "decode_display",
]
