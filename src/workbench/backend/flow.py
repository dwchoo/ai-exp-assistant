"""CW-18 handoff service: the backend side of the bridge tools (C-D60, C-D64, C-D65).

The manager OMP's ``to_worker`` and the worker OMP's ``to_manager`` reach the
backend as ``tool_request`` frames on the authenticated G3 bridge socket.
``HandoffService.handle`` decides each one and returns at once; it never waits
for the other OMP. The worker's ``terminal`` tool (C-D68) is not handled here:
the backend routes it to ``flow_terminal.TerminalService``, which journals
through ``record``. Its contract:

- The role is the caller's: the bridge passes the role of the peer that
  authenticated with its hello token. A tool of the other role is rejected.
- Idempotence: ``(role, session_id, generation, tool_call_id)`` is processed
  once; the same key returns the first result again (also after a restart,
  from the journal).
- Journal: every request and result goes to ``workflow/handoffs.jsonl``
  (0600, fsync per record). Arguments carrying an environment variable value
  are rejected and journaled only as field names.
- Strict schemas (CW-18 smoke D1): a provider with strict tool schemas makes the
  model send every field; ``normalize_arguments`` drops optional fields that are
  null, blank, an empty list/object or ``false`` for a flag before validation,
  so the policy and the journal see only what was meant. A rejection's
  ``errors`` name the field and the expected value, never the value sent.
- Pause: while paused every request is ``held:paused``; nothing is queued and
  nothing is sent after resume (the manager decides again). A queued message
  that reaches the lane while paused is dropped (``held_paused``), except one
  queued with ``keep_across_pause`` (a free-work TASK, a worker's done/blocked
  report): it was never submitted, so it stays pending and is delivered after
  the resume (not a replay; CW-18 R3).
- Outbox: ``handle`` only records a queued handoff (bound to the target
  peer's current session); the target's outbox lane thread creates it through
  ``TaskMailbox.create_message`` and delivers it through ``TaskMailbox.deliver``
  (bridge ``deliver``). ``handle`` never waits on the mailbox: ``deliver``
  holds the mailbox lock until the target's turn ends, and the calling OMP's
  tool must answer within 10 s. Only a ``DEFERRED`` delivery (or a target not
  connected before any submission) is tried again; unknown or rejected never is.
  A listener sees ``delivering`` (the lane took the message), ``deferred`` (back
  to pending, nothing submitted) and ``submitted`` (CW-18 R1: the target OMP
  accepted it into its session; ``TaskMailbox.deliver(on_submitted=...)``),
  before the terminal state that comes when the target's turn ended (or the
  receipt window ran out: ``unknown`` after a submission changes nothing that
  was decided at ``submitted``).
- ``withdraw(task_id)`` (CW-18 R2) drops every message of a Task that was not
  submitted yet (pending, deferred or never created); a message the lane holds
  at that moment is dropped at its next step and never retried.
  There is one lane per target OMP, so a report to the manager never waits
  behind a worker's long turn. ``TaskRepository`` connections are bound to their
  thread, so the backend passes ``mailbox_factory``: each lane opens its own
  repository connection and ``TaskMailbox`` on the same database and bridge.
  Every ``TaskMailbox`` on one bridge delivers to one target under the bridge's
  per-target delivery lock, so handoffs and workflow stage deliveries never race
  into the same OMP turn (CW-18 U2).
- ``enqueue`` queues a backend-originated message (an approved Task's first
  instruction) through the same lanes; a ``listener`` sees ``created`` and the
  terminal state of a queued message.

What a request means (Tasks, runs) is the ``HandoffPolicy``'s. The backend
binds ``flow_tasks.TaskFlow``: under the user's standing delegation (C-D66) a
``to_worker`` becomes a Task at once, one Task at a time (``worker_busy``
otherwise), and ``{task_id, cancel: true}`` cancels it. ``PlaceholderPolicy`` is
the U1 stub used only by unit tests of this service (its ``approval_pending``
is never shown to the user); U3 supplies the real pause source.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable
from uuid import UUID, uuid4

from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected, BridgePeer, MailboxError, MailboxStatus

HANDOFF_JOURNAL_NAME = "handoffs.jsonl"  # under DataLayout.workflow
# p27-cd68-fix-03 (smoke-01 M2): "not yet processed" was read as "send it again".
QUEUED_DETAIL = ("Accepted by Workbench; it is delivered to the other OMP once, in order. Do not send it again; "
                 "a reply, if any, arrives as a new message.")
TOOL_ROLES: dict[str, ActorRole] = {"to_worker": ActorRole.MANAGER, "to_manager": ActorRole.WORKER}
TO_WORKER_KINDS = ("experiment", "work")
TO_MANAGER_KINDS = ("answer", "progress", "done", "blocked", "report")
MESSAGE_MAX = 8192
SHORT_MAX = 1024
LIST_MAX = 64
IDENTIFIER_MAX = 256
SENSITIVE_MIN_LENGTH = 8
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SECRET_NAME = re.compile(r"(?i)(token|secret|passw(?:or)?d|api_?key|access_?key|private_?key|credential"
                         r"|(?:^|_)auth(?:$|_))")
_ASSIGNMENT = re.compile(r"(?<![A-Za-z0-9_$])([A-Za-z_][A-Za-z0-9_]*)=(\S+)")
_TO_WORKER_KEYS = frozenset({"task_id", "kind", "message", "spec", "run", "cancel"})
_TO_MANAGER_KEYS = frozenset({"kind", "message", "task_id", "in_reply_to", "requires_code_change", "reason",
                              "request"})
_SPEC_KEYS = frozenset({"goal", "paths", "instructions", "execution"})
_EXECUTION_KEYS = frozenset({"source", "commit", "command", "criteria", "environment", "shell"})
_CRITERIA_KEYS = frozenset({"log_contains", "result_file", "result_contains"})

RequestKey = tuple[str, str, int, str]


# -- request, task and decision types (the interface U2/U3 build on) -------------
@dataclass(frozen=True, slots=True)
class HandoffRequest:
    """One validated tool call; role/session/generation are the authenticated peer's."""

    role: ActorRole
    session_id: str
    generation: int
    tool_call_id: str
    request_id: str
    tool: str
    args: Mapping[str, Any]

    @property
    def key(self) -> RequestKey:
        return (self.role.value, self.session_id, self.generation, self.tool_call_id)

    def key_dict(self) -> dict[str, Any]:
        return _key_dict(self.key)


@dataclass(frozen=True, slots=True)
class ActiveTask:
    """The single active Task (C-D65), as the policy sees it. Supplied by U2."""

    task_id: str
    revision: int
    kind: str  # "experiment" | "work"
    approved: bool
    run_id: str | None = None
    # The manager's task message a worker report replies to (mailbox reply rule).
    task_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """A message the outbox creates and delivers through ``TaskMailbox``."""

    task_id: str
    revision: int
    run_id: str
    sender_role: ActorRole
    target_role: ActorRole
    kind: MessageKind
    payload: Mapping[str, Any]
    in_reply_to_message_id: str | None = None


OutboxListener = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class HandoffDecision:
    """A policy's answer: the tool result, plus an optional message or approval request.

    ``listener`` (optional) is told ``created`` and the terminal outbox state of
    ``message``, with the entry snapshot.
    """

    result: Mapping[str, Any]
    message: OutboundMessage | None = None
    approval: Mapping[str, Any] | None = None
    listener: OutboxListener | None = None
    keep_across_pause: bool = False


@runtime_checkable
class HandoffPolicy(Protocol):
    def decide(self, request: HandoffRequest, active: ActiveTask | None) -> HandoffDecision: ...


def held(reason: str) -> dict[str, Any]:
    return {"status": "held", "reason": reason}


def rejected(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, **extra}


class PlaceholderPolicy:
    """U1 policy: no approvals or runs yet, only the safe placeholders.

    - ``to_worker`` without an approved active Task, or with a spec/run for it,
      answers ``approval_pending`` with a stub approval record (U2 replaces it
      with the real approval and the deterministic scope check).
    - ``to_manager`` without an active Task is ``rejected:no_active_task``.
    - Free text inside the approved active Task with a run is queued.
    """

    def decide(self, request: HandoffRequest, active: ActiveTask | None) -> HandoffDecision:
        if request.tool == "to_worker":
            return self._to_worker(request, active)
        return self._to_manager(request, active)

    @staticmethod
    def _approval(request: HandoffRequest, task_id: str | None) -> HandoffDecision:
        approval_id = str(uuid4())
        args = request.args
        approval = {
            "approval_id": approval_id, "state": "pending", "stub": True, "task_id": task_id,
            "requested_at": _now(), "request_key": request.key_dict(), "kind": args["kind"],
            "message": args["message"], "spec": args.get("spec"), "run": args.get("run", False),
        }
        return HandoffDecision({"status": "approval_pending", "approval_id": approval_id, "task_id": task_id,
                                "detail": "waiting for the user's approval in Workbench"}, approval=approval)

    def _to_worker(self, request: HandoffRequest, active: ActiveTask | None) -> HandoffDecision:
        args = request.args
        task_id = args.get("task_id")
        if active is None:
            if task_id is not None:
                return HandoffDecision(rejected("unknown_task"))
            return self._approval(request, None)
        if task_id is not None and task_id != active.task_id:
            return HandoffDecision(held("another_task_active"))
        if not active.approved:
            return self._approval(request, active.task_id)
        if "spec" in args or args.get("run") is True:
            if task_id is None:
                return HandoffDecision(held("another_task_active"))
            return self._approval(request, active.task_id)
        if active.run_id is None:
            return HandoffDecision(held("no_active_run"))
        return HandoffDecision({"status": "queued", "task_id": active.task_id}, message=OutboundMessage(
            active.task_id, active.revision, active.run_id, ActorRole.MANAGER, ActorRole.WORKER,
            MessageKind.QUESTION, {"handoff": "to_worker", "kind": args["kind"], "message": args["message"]}))

    @staticmethod
    def _to_manager(request: HandoffRequest, active: ActiveTask | None) -> HandoffDecision:
        args = request.args
        if active is None or not active.approved:
            return HandoffDecision(rejected("no_active_task"))
        if args.get("task_id") not in (None, active.task_id):
            return HandoffDecision(rejected("unknown_task"))
        if active.run_id is None:
            return HandoffDecision(held("no_active_run"))
        payload: dict[str, Any] = {"handoff": "to_manager", "kind": args["kind"], "message": args["message"]}
        for name in ("requires_code_change", "reason", "request"):
            if name in args:
                payload[name] = args[name]
        if args["kind"] == "answer":
            if args.get("in_reply_to") is None:
                return HandoffDecision(rejected("in_reply_to_required"))
            kind, reply_to = MessageKind.ANSWER, args["in_reply_to"]
        else:
            kind, reply_to = MessageKind.REPORT, args.get("in_reply_to") or active.task_message_id
            if reply_to is None:
                return HandoffDecision(rejected("no_task_message"))
        return HandoffDecision({"status": "queued", "task_id": active.task_id}, message=OutboundMessage(
            active.task_id, active.revision, active.run_id, ActorRole.WORKER, ActorRole.MANAGER, kind, payload,
            in_reply_to_message_id=reply_to))


# -- argument validation and environment values ---------------------------------
def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _text(value: object, limit: int = MESSAGE_MAX) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _paths(value: object) -> bool:
    return (isinstance(value, list) and len(value) <= LIST_MAX
            and all(_text(item, SHORT_MAX) for item in value))


_FLAG_KEYS = frozenset({"run", "cancel", "requires_code_change"})  # false means "not used"
EXECUTION_SHAPE = ("{source, commit, command, criteria: {log_contains, result_file, result_contains}, "
                   "environment: [variable NAMES], shell: bash|sh}")
# Smoke-02 E2: the CW-10 judge uses all three (success = exit 0, log_contains in the raw log, result_file
# written by this run and non-empty, result_contains in it), so none of them is optional.
CRITERIA_RULE = ("spec.execution.criteria: all three are required and non-empty: log_contains (text the command "
                 "prints to its output, the raw log), result_file (repo-relative path of a file the command writes "
                 "during this run; a file the run does not write, such as the script itself, makes the result "
                 "indeterminate) and result_contains (text that file must contain); do not invent a condition the "
                 "user did not ask for: ask the user, or make the command write a result file")
PATHS_RULE = (f"a list (at most {LIST_MAX}) of repo-relative path strings such as \"src/app/\" or "
              "\"work/hello.txt\"")


def _blank(value: object) -> bool:
    """A strict-schema placeholder: null, a blank string, or an empty (or all-blank) list/object."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return all(_blank(item) for item in value)
    if isinstance(value, dict):
        return all(_blank(item) for item in value.values())
    return False


def _blank_execution(value: object) -> bool:
    """An execution placeholder: every field blank (``shell`` is an enum, so it never is)."""
    return _blank(value) or (isinstance(value, dict) and all(
        _blank(item) for name, item in value.items() if name != "shell"))


def _absent(name: str, value: object) -> bool:
    return _blank(value) or (name in _FLAG_KEYS and value is False)


def normalize_arguments(tool: str, args: object) -> object:
    """Drop optional fields a strict-schema model filled with placeholders (D1).

    Optional fields that are null, blank, an empty list/object, or ``false`` for a flag are ABSENT. A spec whose
    fields are all blank is absent; inside a spec a blank ``instructions`` and a clearly empty ``execution`` are
    absent, ``paths`` loses blank items (null means none). A ``request`` without any path is no scope request.
    Required fields (``kind``, ``message``) and unknown keys are kept as sent, so validation still names them.
    """
    if not isinstance(args, dict):
        return args
    optional = (_TO_WORKER_KEYS if tool == "to_worker" else _TO_MANAGER_KEYS) - {"kind", "message"}
    result: dict[str, Any] = {}
    for name, value in args.items():
        if name in optional and _absent(name, value):
            continue
        if tool == "to_worker" and name == "spec" and isinstance(value, dict):
            value = _normalize_spec(value)
            if value is None:
                continue
        if tool == "to_manager" and name == "request" and isinstance(value, dict):
            value = _normalize_request(value)
            if value is None:
                continue
        result[name] = value
    return result


def _clean_paths(value: object) -> object:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if not (item is None or isinstance(item, str) and not item.strip())]
    return value


def _normalize_spec(spec: dict[str, Any]) -> dict[str, Any] | None:
    if all(_blank_execution(item) if name == "execution" else _blank(item) for name, item in spec.items()):
        return None
    result: dict[str, Any] = {}
    for name, value in spec.items():
        if name == "instructions" and _blank(value):
            continue
        if name == "execution" and _blank_execution(value):
            continue
        if name == "goal" and value is None:
            continue
        result[name] = _clean_paths(value) if name == "paths" else value
    return result


def _normalize_request(request: dict[str, Any]) -> dict[str, Any] | None:
    if "paths" not in request or not set(request) <= {"goal", "paths"}:
        return request  # a malformed request is named by validation
    paths = _clean_paths(request["paths"])
    if isinstance(paths, list) and not paths:
        return None  # a scope request needs at least one path; the message carries any text
    return {**request, "paths": paths}


def _check_execution(execution: object, errors: list[str]) -> None:
    if not isinstance(execution, dict) or set(execution) != _EXECUTION_KEYS:
        errors.append(f"spec.execution: must be an object with exactly {EXECUTION_SHAPE}")
        return
    for name in ("source", "commit"):
        if not _text(execution[name], SHORT_MAX):
            errors.append(f"spec.execution.{name}: must be a non-empty string (at most {SHORT_MAX} characters)")
    command = execution["command"]
    if not (_text(command) or isinstance(command, list) and 0 < len(command) <= LIST_MAX
            and all(_text(item) for item in command)):
        errors.append("spec.execution.command: must be shell text or a non-empty argv list of strings")
    criteria = execution["criteria"]
    if (not isinstance(criteria, dict) or set(criteria) != _CRITERIA_KEYS
            or not all(_text(criteria[name], SHORT_MAX) for name in _CRITERIA_KEYS)):
        errors.append(CRITERIA_RULE)
    environment = execution["environment"]
    if (not isinstance(environment, list) or len(environment) > LIST_MAX
            or not all(isinstance(name, str) and len(name) <= 128 and ENV_NAME.fullmatch(name)
                       for name in environment)
            or len(set(environment)) != len(environment)):
        errors.append("spec.execution.environment: must be a list of unique variable names (never values), "
                      "[] when none")
    if execution["shell"] not in ("bash", "sh"):
        errors.append("spec.execution.shell: must be bash or sh")


def validate_arguments(tool: str, args: object) -> list[str]:
    """Schema errors (field paths, rules and the expected value; never the values sent).

    The arguments are normalised first (``normalize_arguments``): placeholders of unused fields are absent.
    """
    args = normalize_arguments(tool, args)
    if not isinstance(args, dict):
        return ["arguments: must be an object"]
    errors: list[str] = []
    allowed = _TO_WORKER_KEYS if tool == "to_worker" else _TO_MANAGER_KEYS
    errors += [f"{name}: unknown argument (allowed: {', '.join(sorted(allowed))})"
               for name in sorted(set(args) - allowed)]
    kinds = TO_WORKER_KINDS if tool == "to_worker" else TO_MANAGER_KINDS
    if args.get("kind") not in kinds:
        errors.append(f"kind: must be one of {', '.join(kinds)}")
    if not _text(args.get("message")):
        errors.append(f"message: must be a non-empty string of at most {MESSAGE_MAX} characters")
    if "task_id" in args and not _is_uuid(args["task_id"]):
        errors.append("task_id: must be the canonical UUID of the active Task from a to_worker result, "
                      "or null for a new Task" if tool == "to_worker"
                      else "task_id: must be the canonical UUID of your active Task, or null")
    if tool == "to_worker":
        if "run" in args and not isinstance(args["run"], bool):
            errors.append("run: must be true (re-run the experiment), false or null")
        if "cancel" in args:
            if not isinstance(args["cancel"], bool):
                errors.append("cancel: must be true (cancel the Task), false or null")
            elif args["cancel"] and ("spec" in args or args.get("run") is True):
                errors.append("cancel: with cancel true, spec and run must be null")
        if "spec" in args:
            spec = args["spec"]
            if not isinstance(spec, dict):
                errors.append("spec: must be an object {goal, paths, instructions, execution}, or null for a "
                              "follow-up")
            else:
                errors += [f"spec.{name}: unknown field (allowed: goal, paths, instructions, execution)"
                           for name in sorted(set(spec) - _SPEC_KEYS)]
                if not _text(spec.get("goal")):
                    errors.append(f"spec.goal: must be a non-empty string (at most {MESSAGE_MAX} characters)")
                if not _paths(spec.get("paths")):
                    errors.append(f"spec.paths: must be {PATHS_RULE}; [] when the worker changes nothing")
                if "instructions" in spec and not _text(spec["instructions"]):
                    errors.append("spec.instructions: must be a non-empty string, or null")
                if "execution" in spec:
                    if args.get("kind") != "experiment":
                        errors.append("spec.execution: must be null for kind work (only an experiment Task has "
                                      "an execution); send spec.execution: null, or kind experiment to run it")
                    else:
                        _check_execution(spec["execution"], errors)
                elif args.get("kind") == "experiment":
                    errors.append(f"spec.execution: kind experiment needs an execution object {EXECUTION_SHAPE}")
    else:
        if "in_reply_to" in args and not _is_uuid(args["in_reply_to"]):
            errors.append("in_reply_to: must be the workbench_message_id (UUID) of the manager message you "
                          "answer, or null")
        if "requires_code_change" in args and not isinstance(args["requires_code_change"], bool):
            errors.append("requires_code_change: must be true, false or null")
        if "reason" in args and not _text(args["reason"]):
            errors.append("reason: must be a non-empty string, or null")
        if "request" in args:
            request = args["request"]
            if (not isinstance(request, dict) or set(request) != {"goal", "paths"}
                    or not _text(request.get("goal")) or not _paths(request.get("paths"))):
                errors.append(f"request: must be null, or exactly {{goal: non-empty string, paths: {PATHS_RULE}}}")
    return errors


def _strings(value: object, path: str) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def environment_value_findings(args: Mapping[str, Any], sensitive: Iterable[str]) -> list[str]:
    """Where ``args`` carries an environment variable value (field paths and names only).

    A finding is a known sensitive value anywhere in the text, or an assignment
    ``NAME=value`` to a declared Task environment name or a secret-like name.
    ``$NAME`` references are fine: values reach a run only transiently.
    """
    values = [value for value in dict.fromkeys(sensitive) if isinstance(value, str)
              and len(value) >= SENSITIVE_MIN_LENGTH]
    declared: set[str] = set()
    spec = args.get("spec")
    if isinstance(spec, dict) and isinstance(spec.get("execution"), dict):
        environment = spec["execution"].get("environment")
        if isinstance(environment, list):
            declared = {name for name in environment if isinstance(name, str)}
    findings: list[str] = []
    for path, text in _strings(args, ""):
        if any(value in text for value in values):
            findings.append(f"{path}:sensitive_value")
        for match in _ASSIGNMENT.finditer(text):
            name, value = match.group(1), match.group(2).strip("'\"")
            if value and not value.startswith("$") and (name in declared or SECRET_NAME.search(name)):
                findings.append(f"{path}:assignment:{name}")
    return list(dict.fromkeys(findings))


def sensitive_environment_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Values of secret-like variables (the backend never prints or stores them)."""
    return tuple(value for name, value in environment.items()
                 if SECRET_NAME.search(name) and isinstance(value, str) and len(value) >= SENSITIVE_MIN_LENGTH)


# -- journal --------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _key_dict(key: RequestKey) -> dict[str, Any]:
    return {"role": key[0], "session_id": key[1], "generation": key[2], "tool_call_id": key[3]}


def _key_tuple(value: object) -> RequestKey | None:
    if not isinstance(value, dict):
        return None
    role, session, generation, call = (value.get("role"), value.get("session_id"), value.get("generation"),
                                       value.get("tool_call_id"))
    if (isinstance(role, str) and isinstance(session, str) and type(generation) is int
            and isinstance(call, str)):
        return (role, session, generation, call)
    return None


class HandoffJournal:
    """Append-only JSONL, 0600, one fsync per record; refuses a symlink or foreign file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.records: list[dict[str, Any]] = self._read()
        created = not self.path.exists()
        self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                           0o600)
        try:
            info = os.fstat(self._fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError(f"handoff journal is not a private regular file: {self.path}")
            os.fchmod(self._fd, 0o600)
            if created:
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except Exception:
            os.close(self._fd)
            raise
        self._sequence = max((record.get("seq", 0) for record in self.records
                              if type(record.get("seq")) is int), default=0)

    def _read(self) -> list[dict[str, Any]]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError(f"handoff journal is not a regular file: {self.path}")
            data = handle.read()
        records = []
        for line in data.split(b"\n"):
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, ValueError):
                continue  # a torn last line (crash during append) is not a record
            if isinstance(record, dict):
                records.append(record)
        return records

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._fd < 0:
                raise OSError("handoff journal is closed")
            entry = {"seq": self._sequence + 1, "at": _now(), **record}
            data = (json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                               allow_nan=False) + "\n").encode()
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                view = view[written:]
            os.fsync(self._fd)
            self._sequence += 1
            return entry

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1


# -- outbox ---------------------------------------------------------------------
@dataclass(slots=True)
class _OutboxEntry:
    key: RequestKey
    handoff_id: str
    outbound: OutboundMessage
    target: tuple[str, int] | None  # target peer (session_id, generation) when queued
    message: Any = None  # the TaskMailbox message, once created by the outbox thread
    state: str = "pending"  # pending | delivering | delivered | unknown | rejected | held_paused
    attempts: int = 0
    due: float = 0.0
    status: str | None = None
    reason: str | None = None
    deferred_logged: bool = False
    listener: OutboxListener | None = None
    keep_across_pause: bool = False  # never submitted: kept pending through a pause (R3)
    submitted: bool = False  # the target OMP accepted it into its session (R1)
    withdrawn: bool = False  # its Task was cancelled/superseded before it was submitted (R2)
    paused_logged: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {"key": _key_dict(self.key), "handoff_id": self.handoff_id,
                "message_id": getattr(self.message, "message_id", None),
                "target_role": self.outbound.target_role.value, "state": self.state,
                "attempts": self.attempts, "status": self.status, "reason": self.reason}


_TERMINAL_STATES = {MailboxStatus.API_RETURNED: "delivered", MailboxStatus.OMP_PROCESSED: "delivered",
                    MailboxStatus.UNKNOWN: "unknown", MailboxStatus.REJECTED: "rejected"}


def accepts_keyword(function: Any, name: str) -> bool:
    """Whether ``function`` takes the keyword ``name`` (test doubles and older ports may not)."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name and p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
               or p.kind is p.VAR_KEYWORD for p in parameters)


class HandoffService:
    """Backend handling of ``to_worker``/``to_manager``; see the module docstring."""

    def __init__(self, journal_path: str | Path, *, mailbox: Any | None = None,
                 mailbox_factory: Callable[[], tuple[Any, Callable[[], None]]] | None = None,
                 policy: HandoffPolicy | None = None,
                 active_task: Callable[[], ActiveTask | None] | None = None,
                 paused: Callable[[], bool] | None = None,
                 sensitive_values: Callable[[], Iterable[str]] | None = None,
                 peer_lookup: Callable[[ActorRole], BridgePeer | None] | None = None,
                 deliver_timeout: float = 20.0, retry_interval: float = 0.5):
        self._mailbox = mailbox
        self._mailbox_factory = mailbox_factory
        self._peer_lookup = peer_lookup
        self._policy: HandoffPolicy = policy or PlaceholderPolicy()
        self._active_task = active_task or (lambda: None)
        self._paused = paused or (lambda: False)
        self._sensitive_values = sensitive_values or (lambda: ())
        self._deliver_timeout = deliver_timeout
        self._retry_interval = retry_interval
        self._lock = threading.RLock()
        self._journal = HandoffJournal(journal_path)
        self._results: dict[RequestKey, dict[str, Any]] = {}
        for record in self._journal.records:
            key = _key_tuple(record.get("key"))
            if record.get("type") == "result" and key is not None and isinstance(record.get("result"), dict):
                self._results.setdefault(key, record["result"])
        self._journal.records = []  # only the results are kept
        self._approvals: dict[str, dict[str, Any]] = {}
        self._outbox: list[_OutboxEntry] = []
        self._outbox_cv = threading.Condition()
        self._stop = False
        self._threads: dict[ActorRole, threading.Thread] = {}

    # -- wiring for U2/U3 -------------------------------------------------------
    def configure(self, *, policy: HandoffPolicy | None = None,
                  active_task: Callable[[], ActiveTask | None] | None = None,
                  paused: Callable[[], bool] | None = None) -> None:
        with self._lock:
            if policy is not None:
                self._policy = policy
            if active_task is not None:
                self._active_task = active_task
            if paused is not None:
                self._paused = paused

    def start(self) -> None:
        with self._outbox_cv:
            if self._threads or self._stop:
                return
            for role in (ActorRole.WORKER, ActorRole.MANAGER):  # one delivery lane per target OMP
                thread = threading.Thread(target=self._outbox_loop, args=(role,),
                                          name=f"handoff-outbox-{role.value}", daemon=True)
                self._threads[role] = thread
                thread.start()

    def close(self) -> None:
        with self._outbox_cv:
            self._stop = True
            threads = list(self._threads.values())
            self._outbox_cv.notify_all()
        for thread in threads:
            thread.join(self._deliver_timeout + 5)
        self._journal.close()

    def enqueue(self, outbound: OutboundMessage, *, origin: str,
                listener: OutboxListener | None = None, keep_across_pause: bool = False) -> dict[str, Any]:
        """Queue a backend-originated message (not a tool call) through the target's lane."""
        key: RequestKey = ("backend", origin, 1, str(uuid4()))
        try:
            return self._queue(key, outbound, listener, keep_across_pause)
        except (OSError, TypeError, ValueError):
            return held("journal_unavailable")

    def withdraw(self, task_id: str) -> list[str]:
        """Drop every not yet submitted message of ``task_id`` (CW-18 R2); the kinds dropped.

        A pending (queued or deferred) message ends ``withdrawn`` at once. One the
        lane holds right now is marked and ends ``withdrawn`` at the lane's next
        step unless it was already submitted (then it reached the OMP before any
        message queued after this call).
        """
        dropped: list[_OutboxEntry] = []
        with self._outbox_cv:
            for entry in self._outbox:
                if entry.outbound.task_id != task_id or entry.submitted:
                    continue
                if entry.state == "pending":
                    entry.withdrawn = True
                    entry.state = "withdrawn"
                    entry.reason = "task_withdrawn"
                    dropped.append(entry)
                elif entry.state == "delivering":
                    entry.withdrawn = True
            self._outbox_cv.notify_all()
        for entry in dropped:
            self._log_outbox(entry, "withdrawn")
            self._notify(entry, "withdrawn")
        return [entry.outbound.kind.value for entry in dropped]

    def record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Append one record of another bridge tool (C-D68 ``terminal``) to this journal; may raise OSError."""
        return self._journal.append(record)

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._approvals.values() if item.get("state") == "pending"]

    def outbox_snapshot(self) -> list[dict[str, Any]]:
        with self._outbox_cv:
            return [entry.snapshot() for entry in self._outbox]

    def _is_paused(self) -> bool:
        try:
            return self._paused() is not False
        except Exception:
            return True  # an unknown pause state holds

    # -- requests ---------------------------------------------------------------
    def handle(self, role: ActorRole | str, request: Mapping[str, Any]) -> dict[str, Any]:
        """Decide one tool call from the authenticated ``role``; never raises for bad input."""
        parsed = self._parse(role, request)
        if isinstance(parsed, dict):
            self._journal.append({"type": "invalid", "reason": parsed["reason"]})
            return parsed
        # D1: the policy and the journal see the arguments without strict-schema placeholders.
        parsed = replace(parsed, args=normalize_arguments(parsed.tool, parsed.args))
        with self._lock:
            key = parsed.key
            if key in self._results:
                self._journal.append({"type": "duplicate", "key": parsed.key_dict(),
                                      "request_id": parsed.request_id})
                return dict(self._results[key])
            result, journal_args = self._screen(parsed)
            try:
                self._journal.append({"type": "request", "key": parsed.key_dict(), "request_id": parsed.request_id,
                                      "tool": parsed.tool, "args": journal_args})
            except (OSError, TypeError, ValueError):
                return rejected("journal_unavailable")  # nothing was decided or sent
            if result is None:
                result = self._decide(parsed)
            self._results[key] = result
            try:
                self._journal.append({"type": "result", "key": parsed.key_dict(), "result": result})
            except (OSError, TypeError, ValueError):
                pass  # the effect already happened; the in-memory result still dedupes
            return dict(result)

    @staticmethod
    def _parse(role: ActorRole | str, request: Mapping[str, Any]) -> HandoffRequest | dict[str, Any]:
        try:
            actor = role if isinstance(role, ActorRole) else ActorRole(role)
        except ValueError:
            return rejected("invalid_request")
        if actor not in (ActorRole.MANAGER, ActorRole.WORKER) or not isinstance(request, Mapping):
            return rejected("invalid_request")
        call, request_id = request.get("tool_call_id"), request.get("request_id")
        session, generation = request.get("session_id"), request.get("generation")
        if (not _text(call, IDENTIFIER_MAX) or not _text(request_id, IDENTIFIER_MAX) or not _is_uuid(session)
                or type(generation) is not int or generation < 1):
            return rejected("invalid_request")
        tool = request.get("tool")
        return HandoffRequest(actor, session, generation, call, request_id, tool if isinstance(tool, str) else "",
                              request.get("args"))

    def _screen(self, request: HandoffRequest) -> tuple[dict[str, Any] | None, Any]:
        """(early result or None, the arguments as journaled)."""
        if request.tool not in TOOL_ROLES:
            return rejected("unknown_tool"), {"redacted": "unknown_tool"}
        if TOOL_ROLES[request.tool] is not request.role:
            return rejected("tool_not_allowed_for_role"), {"redacted": "tool_not_allowed_for_role"}
        errors = validate_arguments(request.tool, request.args)
        if errors:
            return rejected("invalid_arguments", errors=errors), {"redacted": "invalid_arguments", "errors": errors}
        try:
            sensitive = tuple(self._sensitive_values())
        except Exception:
            return rejected("environment_check_unavailable"), {"redacted": "environment_check_unavailable"}
        findings = environment_value_findings(request.args, sensitive)
        if findings:
            return (rejected("environment_value", fields=findings),
                    {"redacted": "environment_value", "fields": findings})
        if self._is_paused():
            return held("paused"), request.args
        return None, request.args

    def _decide(self, request: HandoffRequest) -> dict[str, Any]:
        try:
            active = self._active_task()
            decision = self._policy.decide(request, active)
        except Exception as exc:
            return held(f"policy_error:{type(exc).__name__}")
        result = dict(decision.result)
        if decision.approval is not None:
            approval = dict(decision.approval)
            self._approvals[str(approval.get("approval_id"))] = approval
            try:
                self._journal.append({"type": "approval", "key": request.key_dict(), "approval": approval})
            except (OSError, TypeError, ValueError):
                pass  # kept in memory; the request record is already durable
        if decision.message is not None:
            result.update(self._queue(request.key, decision.message, decision.listener, decision.keep_across_pause))
        return result

    def _queue(self, key: RequestKey, outbound: OutboundMessage,
               listener: OutboxListener | None = None, keep_across_pause: bool = False) -> dict[str, Any]:
        if self._mailbox is None and self._mailbox_factory is None:
            return held("mailbox_unavailable")
        target: tuple[str, int] | None = None
        if self._peer_lookup is not None:
            try:
                peer = self._peer_lookup(outbound.target_role)
            except Exception:
                peer = None
            if peer is None:
                return held("target_not_connected")
            target = (peer.session_id, peer.generation)
        entry = _OutboxEntry(key, str(uuid4()), outbound, target, listener=listener,
                             keep_across_pause=keep_across_pause)
        self._journal.append({"type": "outbox", "key": _key_dict(key), "handoff_id": entry.handoff_id,
                              "target_role": outbound.target_role.value, "kind": outbound.kind.value,
                              "state": "pending", "attempts": 0})
        with self._outbox_cv:
            self._outbox.append(entry)
            self._outbox_cv.notify_all()
        return {"status": "queued", "handoff_id": entry.handoff_id, "detail": QUEUED_DETAIL}

    # -- outbox thread ------------------------------------------------------------
    def _next_due(self, role: ActorRole) -> tuple[_OutboxEntry | None, float | None]:
        now = time.monotonic()
        wait: float | None = None
        for entry in self._outbox:
            if entry.state != "pending" or entry.outbound.target_role is not role:
                continue
            if entry.due <= now:
                return entry, None
            wait = entry.due - now if wait is None else min(wait, entry.due - now)
        return None, wait

    def _outbox_loop(self, role: ActorRole) -> None:
        mailbox, release, failure, opened = self._mailbox, None, None, False
        try:
            while True:
                with self._outbox_cv:
                    while True:
                        if self._stop:
                            return
                        entry, wait = self._next_due(role)
                        if entry is not None:
                            entry.state = "delivering"
                            break
                        self._outbox_cv.wait(wait)
                if not opened:
                    opened = True  # this lane's own connection, opened on its first message
                    if mailbox is None and self._mailbox_factory is not None:
                        try:
                            mailbox, release = self._mailbox_factory()
                        except Exception as exc:
                            mailbox, failure = None, f"mailbox_unavailable:{type(exc).__name__}"
                if mailbox is None:
                    self._finish(entry, "rejected", None, failure or "mailbox_unavailable")  # nothing was sent
                    continue
                self._notify(entry, "delivering")
                self._deliver_once(mailbox, entry)
        finally:
            if release is not None:
                try:
                    release()
                except Exception:
                    pass

    def _create(self, mailbox: Any, entry: _OutboxEntry) -> str | None:
        """Create the mailbox message once; ``"deferred"``, a terminal state, or None when ready."""
        if entry.message is not None:
            return None
        outbound = entry.outbound
        try:
            message = mailbox.create_message(
                outbound.task_id, outbound.revision, outbound.run_id, outbound.sender_role, outbound.target_role,
                outbound.kind, dict(outbound.payload), in_reply_to_message_id=outbound.in_reply_to_message_id)
        except BridgeDisconnected:
            return "deferred"  # the target reconnects; nothing was created
        except (MailboxError, LookupError, ValueError, TypeError) as exc:
            entry.reason = type(exc).__name__
            return "rejected"
        except Exception as exc:  # the store may have written the row
            entry.reason = type(exc).__name__
            return "unknown"
        entry.message = message
        if entry.target is not None and (message.session_id, message.session_generation) != entry.target:
            # The target OMP is a new session: the handoff was for the one it was queued for.
            entry.reason = "target_session_changed"
            return "rejected"
        self._log_outbox(entry, "created")
        self._notify(entry, "created")
        return None

    def _withdrawn(self, entry: _OutboxEntry) -> bool:
        with self._outbox_cv:
            return entry.withdrawn

    def _deliver_once(self, mailbox: Any, entry: _OutboxEntry) -> None:
        if self._withdrawn(entry):
            self._finish(entry, "withdrawn", None, "task_withdrawn")
            return
        if self._is_paused():
            if entry.keep_across_pause:
                self._hold_paused(entry)  # never submitted: delivered after the resume (R3)
                return
            # Held, not resent after resume: the sender decides again (C-D65 pause).
            self._finish(entry, "held_paused", None, "paused")
            return
        created = self._create(mailbox, entry)
        if created == "deferred":
            self._defer(entry, MailboxStatus.DEFERRED, "target_not_connected")
            return
        if created is not None:
            self._finish(entry, created, None, entry.reason)
            return
        if self._withdrawn(entry):  # created, never submitted
            self._finish(entry, "withdrawn", None, "task_withdrawn")
            return
        try:
            if accepts_keyword(mailbox.deliver, "on_submitted"):
                receipt = mailbox.deliver(entry.message, timeout=self._deliver_timeout,
                                          on_submitted=lambda: self._submitted(entry))
            else:
                receipt = mailbox.deliver(entry.message, timeout=self._deliver_timeout)
            status, reason = receipt.status, (receipt.details or {}).get("reason")
        except BridgeDisconnected:
            status, reason = MailboxStatus.DEFERRED, "target_not_connected"  # before any submission
        except Exception as exc:
            status, reason = MailboxStatus.UNKNOWN, type(exc).__name__
        if status is MailboxStatus.DEFERRED and not entry.submitted:
            self._defer(entry, status, reason)
            return
        state = _TERMINAL_STATES.get(status, "unknown")
        if state == "delivered" and not entry.submitted:
            self._submitted(entry)  # a mailbox without the ack hook: its receipt is the submission
        self._finish(entry, state, status, reason)

    def _submitted(self, entry: _OutboxEntry) -> None:
        with self._outbox_cv:
            if entry.submitted:
                return
            entry.submitted = True
        self._log_outbox(entry, "submitted")
        self._notify(entry, "submitted")

    def _hold_paused(self, entry: _OutboxEntry) -> None:
        with self._outbox_cv:
            entry.state = "pending"
            entry.due = time.monotonic() + max(self._retry_interval, 0.05)
            first = not entry.paused_logged
            entry.paused_logged = True
            self._outbox_cv.notify_all()
        if first:
            self._log_outbox(entry, "kept_paused")
        self._notify(entry, "deferred")

    def _defer(self, entry: _OutboxEntry, status: MailboxStatus, reason: Any) -> None:
        if self._withdrawn(entry):
            self._finish(entry, "withdrawn", None, "task_withdrawn")
            return
        with self._outbox_cv:
            entry.attempts += 1
            entry.status, entry.reason = status.value, reason if isinstance(reason, str) else None
            delay = min(self._retry_interval * (2 ** min(entry.attempts - 1, 6)), max(self._retry_interval, 2.0))
            entry.due = time.monotonic() + delay
            entry.state = "pending"
            first = not entry.deferred_logged
            entry.deferred_logged = True
            self._outbox_cv.notify_all()
        if first:
            self._log_outbox(entry, "deferred")
        self._notify(entry, "deferred")

    def _finish(self, entry: _OutboxEntry, state: str, status: MailboxStatus | None, reason: Any) -> None:
        with self._outbox_cv:
            if status is not None:
                entry.attempts += 1
            entry.state = state
            entry.status = status.value if status is not None else None
            entry.reason = reason if isinstance(reason, str) else entry.reason
            self._outbox_cv.notify_all()
        self._log_outbox(entry, state)
        self._notify(entry, state)

    def _notify(self, entry: _OutboxEntry, event: str) -> None:
        if entry.listener is None:
            return
        try:
            entry.listener(event, entry.snapshot())
        except Exception:
            pass  # a listener never stops the lane

    def _log_outbox(self, entry: _OutboxEntry, state: str) -> None:
        try:
            self._journal.append({"type": "outbox", "key": _key_dict(entry.key), "handoff_id": entry.handoff_id,
                                  "message_id": getattr(entry.message, "message_id", None), "state": state,
                                  "status": entry.status, "reason": entry.reason, "attempts": entry.attempts})
        except (OSError, TypeError, ValueError):
            pass


__all__ = [
    "HANDOFF_JOURNAL_NAME", "ActiveTask", "HandoffDecision", "HandoffJournal", "HandoffPolicy", "HandoffRequest", "HandoffService",
    "OutboundMessage", "OutboxListener", "PlaceholderPolicy", "TOOL_ROLES", "accepts_keyword",
    "environment_value_findings", "held", "rejected",
    "sensitive_environment_values", "validate_arguments",
]
