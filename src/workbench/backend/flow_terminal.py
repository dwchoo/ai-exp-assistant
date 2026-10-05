"""C-D68 (1): the worker OMP's ``terminal`` tool, its only way to run a command.

The worker's bridge tool ``terminal`` reaches the backend as a ``tool_request``
frame on the authenticated G3 bridge socket; the backend routes it here, not
through ``HandoffService.handle`` (which answers at once): this call may wait.

- Where it runs: in the product host shell (the persistent shell in the host
  terminal pane), visible to the user, through the managed dispatch an
  experiment run uses: the shell must be a clean user-owned prompt with no job,
  no typed line and no request in flight (C-D65 (2)); then user input is held,
  ``wb-handoff`` puts the parent into its control wait, the manager claims it
  and the command is submitted to a child interpreter (``<shell> -c <command>``)
  that inherits the parent's cwd and exported environment. The user sees the
  output in the host pane (the pane keeps every byte; the service only reads a
  copy).
- What the user sees (p27-cd68-fix-03, smoke-01 P1): the child prints
  ``[worker] $ <command>`` into the host pane before the command runs; the
  child is ``<shell> -c 'printf ...; exec "$0" -c "$1"' <shell> <command>``, so
  the command itself still runs exactly as ``<shell> -c <command>`` (same PID,
  ``$0``, no positional parameters) and nothing is typed into the parent
  shell or its history. While the command holds the host shell,
  ``host_operator()`` is ``worker`` (the UI shows the worker as the user of
  the host terminal; smoke-01 P2).
- Where (C-D68 (7)): the host shell's current directory, where the user last
  cd'd. Nothing changes the parent shell's directory (no ``cd`` is typed; a
  ``cd`` inside the command stays in the child), so nothing is restored
  afterwards. The directory used is the parent's cwd read from /proc (never
  typed) before anything is typed and again just before the submit (input is
  held in between); the result and the journal record it.
- After the exit the shell is given back to the user (as ``TaskFlow._give_back``
  does).
- Refusals type nothing: a busy, typing, job-holding, foreign-owned or missing
  host shell, an experiment run that is active or starting, or a pending
  directory restore of an experiment are ``host_terminal_busy``; a paused
  Workbench is ``paused`` for a new command only (a command already running
  continues and ``command: null`` still returns its state); a new command while
  one runs is ``terminal_command_running``. No Task is needed (the journal then
  has ``task_id: null``).
- One command at a time. The tool call waits up to ``WAIT_SECONDS`` (120 s, fixed;
  C-D68 (9): the worker sets no wait): ``exited`` gives the exit code, a bounded output tail and
  the full log file; otherwise ``running``: the command keeps running and the
  worker ends its turn (C-D68 (8); no re-wait loop). A call with ``command:
  null`` never waits (C-D68 (10)): it returns at once the running state (the
  output not yet returned, elapsed time) or the finished result, which then
  counts as received; it does not hold back the checks. Aborting
  the tool call stops only the waiting (the bridge answers it, the command
  continues) and the bridge sends ``terminal_wait_abandoned`` so that call does
  not count as having received the result. A call abandoned before its command
  was submitted runs nothing (``aborted``; checked before anything is typed and
  again just before the submit, the shell is given back). A result the bridge
  server could not write to the session that asked (session replaced, socket
  closed) is reported through ``undelivered`` and counts the same as an abandon
  (p27-cd68-fix-01).
- Notices (C-D68 (8)) through ``notify`` (the backend's bridge ``notice`` frame
  to the worker, under the worker's delivery lock, so never into the same turn
  as a mailbox delivery): while a command runs and no ``terminal`` call waits
  for it, a ``terminal_check`` every ``CHECK_INTERVAL`` (60 s; counted from the
  start, the last check sent or the last waiting call's return) with the
  command, cwd, elapsed time, the output since the last check (bounded) and the
  log path. A worker that is busy (``deferred``) keeps one pending check; checks
  due meanwhile are merged into it (``coalesced_count``) and it carries the
  latest output when it is sent. Paused: no check (skipped). On the exit, when
  no live ``terminal`` call waits for it (or got it), one ``terminal_done``
  notice (command, cwd, status, exit code or signal, output tail, log path,
  duration): busy -> retried until delivered, paused -> held until the resume,
  ``unknown``/``rejected`` -> never resent; never twice. The notice of an
  earlier command survives a new command. ``tick`` (the notifier thread, or a
  test with a fake ``clock``) does all of it.
- Experiment runs and terminal commands exclude each other through one
  ``HostGate`` (the experiment's whole run vs. the command from its idle check
  to its give-back); the shell's own automation hold makes the last step
  atomic.
- Journal: every request, refusal, start, end and result goes to the handoff
  journal (``workflow/handoffs.jsonl``) with the command text, times, exit code,
  log path and refusal reason, each check (``terminal_check``: sent, merged,
  deferred, skipped_paused, unknown, rejected) and the completion notice
  (``terminal_notice``: pending, deferred, held_paused, sent, unknown,
  rejected, not_needed); output is only in the log file
  (``workflow/terminal/<command_id>.log``, 0600, at most ``LOG_MAX`` bytes) and
  no environment value is stored (a command carrying one is refused).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from workbench.backend.flow import (
    HandoffService, environment_value_findings, rejected,
)
from workbench.contracts.v1 import ActorRole

TERMINAL_TOOL = "terminal"
ABANDON_TOOL = "terminal_wait_abandoned"  # the bridge: a terminal call stopped waiting (abort or its timeout)
WAIT_SECONDS = 120  # C-D68 (9): the first wait, fixed (no worker-supplied timeout)
DEFAULT_TIMEOUT = WAIT_SECONDS  # the earlier name
COMMAND_MAX = 8192
TAIL_BYTES = 8192
TAIL_LINES = 200
LOG_MAX = 64 * 1024 * 1024  # the per-run raw log limit of the operating contract
PREPARE_WAIT = 3.0  # the control wait, as in an experiment start
HOLD_REASON = "Workbench is running the worker's terminal command"
TERMINAL_DIRECTORY = "terminal"  # under DataLayout.workflow
CHECK_INTERVAL = 60  # C-D68 (8): 1 minute
NOTICE_RETRY = 2.0  # a busy or unreachable worker: the next delivery attempt
NOTIFIER_TICK = 0.5
CALLS_KEPT = 256  # terminal calls remembered for a late abandon signal
_KEYS = frozenset({"command"})
# The child prints the command line for the user, then becomes ``<shell> -c <command>`` (same PID).
ECHO_SCRIPT = 'printf \'[worker] $ %s\\n\' "$1"; exec "$0" -c "$1"'
_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[P^_][^\x1b]*\x1b\\|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

RUNNING_DETAIL = ("The command keeps running in the host terminal. End your turn now with one short text line (for "
                  "example 'Waiting for the Workbench notice.'), never an empty reply. Do not use the wait tool or "
                  "repeated terminal calls to wait for it, and do not start another command (a new one is refused "
                  "until it ends). Do not send progress reports about it unless the user or the manager asks or a "
                  "check shows a problem: Workbench sends you a check every 60 s while it runs and a completion "
                  "notice when it exits. The user can stop it in the host terminal.")
CHECK_INSTRUCTION = ("Workbench check (automatic, every 60 s while your terminal command runs). Look at the new "
                     "output for errors or a hang. Do not start another command and do not wait for it. If it is "
                     "useful, report to the manager with to_manager (with no Task, tell the user in your reply); "
                     "then end your turn with one short text line (never an empty reply). The completion notice "
                     "follows when the command exits.")
DONE_INSTRUCTION = ("Workbench notice: your terminal command ended. Read the result (the full output is in "
                    "log_path) and continue your work.")
BUSY_DETAIL = ("Nothing was run. The host terminal runs a command only when it is free (the user's idle prompt, "
               "no job, no typed line, no experiment run). Do not work around it; try again later or tell the "
               "manager with to_manager.")
ABORTED_DETAIL = "The call was aborted before the command started: nothing was run."
PAUSED_DETAIL = ("Nothing was run: the user paused Workbench automation. A command already running continues. "
                 "Wait for the user's resume.")


class _CallAbandoned(RuntimeError):
    """The bridge stopped waiting for this call before its command was submitted."""


class HostGate:
    """One Workbench owner of the product host shell at a time: an experiment run or a terminal command.

    Not re-entrant. ``check`` runs under the gate lock (it may take the flow
    lock; the flow never holds its lock while it acquires the gate).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.owner: str | None = None

    def acquire(self, owner: str, check: Callable[[], str | None] | None = None) -> str | None:
        """None when ``owner`` now holds the gate, else why not (nothing held)."""
        with self._lock:
            if self.owner is not None:
                return self.owner
            if check is not None:
                try:
                    reason = check()
                except Exception as exc:  # an unknown state never lets a command start
                    reason = f"check_failed:{type(exc).__name__}"
                if reason is not None:
                    return reason
            self.owner = owner
            return None

    def release(self, owner: str) -> None:
        with self._lock:
            if self.owner == owner:
                self.owner = None


def validate_terminal_arguments(args: object) -> list[str]:
    """Errors naming the field and the expected value (never the value sent)."""
    if not isinstance(args, dict):
        return ["arguments: must be an object {command}"]
    errors = [f"{name}: unknown field; only command" for name in sorted(set(args) - _KEYS)]
    command = args.get("command")
    if command is not None and (not isinstance(command, str) or len(command) > COMMAND_MAX or "\x00" in command):
        errors.append(f"command: a shell command string (at most {COMMAND_MAX} characters, no NUL), or null to "
                      "wait for the running command")
    return errors


def output_tail(data: bytes) -> tuple[str, bool]:
    """The readable end of the output: at most ``TAIL_LINES`` lines and ``TAIL_BYTES`` bytes."""
    text = _ANSI.sub(b"", data).decode("utf-8", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    lines = text.split("\n")
    truncated = len(lines) > TAIL_LINES
    text = "\n".join(lines[-TAIL_LINES:])
    encoded = text.encode("utf-8")
    if len(encoded) > TAIL_BYTES:
        truncated = True
        text = encoded[-TAIL_BYTES:].decode("utf-8", "ignore")
    return text, truncated


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(eq=False)
class _Command:
    command_id: str
    command: str
    key: dict[str, Any]
    task_id: str | None
    log_path: Path
    cwd: str = ""  # the parent shell's directory the child inherits (observed at the submit)
    started_at: str = ""
    started: float = 0.0
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None  # the final result (exited | unknown)
    recent: bytearray = field(default_factory=bytearray)  # the last output bytes, for the tail
    log_bytes: int = 0
    log_truncated: bool = False
    log_error: str | None = None
    echo: bytes = b""  # the command line the child prints first, still to be skipped (as the PTY shows it)
    # C-D68 (8) notices; ``since`` is guarded by ``lock``, the rest by the service lock.
    lock: threading.Lock = field(default_factory=threading.Lock)
    since: bytearray = field(default_factory=bytearray)  # output since the last check (or waiting call)
    since_dropped: bool = False
    unseen: bytearray = field(default_factory=bytearray)  # output not yet returned by a terminal call (C-D68 (10))
    unseen_dropped: bool = False
    waiters: set = field(default_factory=set)  # keys of terminal calls waiting for it now
    consumed: set = field(default_factory=set)  # keys of terminal calls that got its final result
    check_base: float = 0.0  # the clock time the next check counts from
    check_pending: str | None = None  # the notice_id of the check waiting for an idle worker
    check_coalesced: int = 0
    check_retry_at: float = 0.0
    check_deferred_logged: bool = False
    notice: str = "none"  # none | not_needed | pending | sending | sent | unknown | rejected
    notice_id: str = ""
    notice_due: float = 0.0
    notice_logged: set = field(default_factory=set)

    def write(self, data: bytes) -> None:
        if data and self.echo:  # the "[worker] $ <command>" line is for the user's pane, not the output
            n = 0
            while n < min(len(data), len(self.echo)) and data[n] == self.echo[n]:
                n += 1
            if n == len(data):
                self.echo = self.echo[n:]
                return
            data, self.echo = (data[n:] if n == len(self.echo) else data), b""
        if not data:
            return
        self.recent.extend(data)
        if len(self.recent) > 4 * TAIL_BYTES:
            del self.recent[:-4 * TAIL_BYTES]
        with self.lock:
            self.since.extend(data)
            if len(self.since) > 4 * TAIL_BYTES:
                del self.since[:-4 * TAIL_BYTES]
                self.since_dropped = True
            self.unseen.extend(data)
            if len(self.unseen) > 4 * TAIL_BYTES:
                del self.unseen[:-4 * TAIL_BYTES]
                self.unseen_dropped = True
        if self.log_error is not None:
            return
        room = LOG_MAX - self.log_bytes
        chunk = data[:max(room, 0)]
        if len(chunk) < len(data):
            self.log_truncated = True
        if not chunk:
            return
        try:
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                view = memoryview(chunk)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                os.close(fd)
            self.log_bytes += len(chunk)
        except OSError as exc:  # the command and its observation continue (C-D46)
            self.log_error = type(exc).__name__

    def tail(self) -> dict[str, Any]:
        text, truncated = output_tail(bytes(self.recent))
        return {"output_tail": text, "output_tail_truncated": truncated or self.log_bytes > len(self.recent)}

    def take_unseen(self) -> tuple[bytes, bool]:
        with self.lock:
            data, dropped = bytes(self.unseen), self.unseen_dropped
            self.unseen, self.unseen_dropped = bytearray(), False
        return data, dropped

    def take_since(self) -> tuple[bytes, bool]:
        with self.lock:
            data, dropped = bytes(self.since), self.since_dropped
            self.since, self.since_dropped = bytearray(), False
        return data, dropped

    def put_back_since(self, data: bytes, dropped: bool) -> None:
        """A check that was not delivered: its output goes before what came meanwhile."""
        with self.lock:
            merged = bytearray(data) + self.since
            self.since_dropped = self.since_dropped or dropped or len(merged) > 4 * TAIL_BYTES
            self.since = merged[-4 * TAIL_BYTES:]


class TerminalService:
    """The backend side of the worker's ``terminal`` tool; see the module docstring."""

    def __init__(self, *, handoffs: HandoffService, host_shell: Callable[[], Any], gate: HostGate,
                 log_root: str | Path,
                 automation: Callable[[], Mapping[str, Any]],
                 paused: Callable[[], bool] = lambda: False,
                 activity: Callable[[], str | None] = lambda: None,
                 sensitive_values: Callable[[], Iterable[str]] = lambda: (),
                 active_task: Callable[[], Any] = lambda: None,
                 poll_interval: float = 0.03,
                 notify: Callable[[Mapping[str, Any]], str] | None = None,
                 clock: Callable[[], float] = time.monotonic, check_interval: float = CHECK_INTERVAL,
                 notice_retry: float = NOTICE_RETRY, tick_interval: float = NOTIFIER_TICK,
                 wait_seconds: float = WAIT_SECONDS):
        self._handoffs = handoffs
        self._host_shell = host_shell
        self._gate = gate
        self._log_root = Path(log_root)
        self._automation = automation
        self._paused = paused
        self._activity = activity
        self._sensitive_values = sensitive_values
        self._active_task = active_task
        self._poll = poll_interval
        self._lock = threading.Lock()
        self._results: dict[tuple, dict[str, Any]] = {}
        self._inflight: set[tuple] = set()
        self._current: _Command | None = None
        self._starting = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # C-D68 (8): checks and the completion notice (module docstring).
        self._notify = notify
        self._wait_seconds = wait_seconds
        self._clock = clock
        self._check_interval = check_interval
        self._notice_retry = notice_retry
        self._tick_interval = tick_interval
        self._tick_lock = threading.Lock()
        self._wake = threading.Event()
        self._notifier: threading.Thread | None = None
        self._calls: dict[tuple, _Command] = {}  # terminal call key -> the command it waited for
        self._abandoned: dict[tuple, None] = {}  # keys of calls the bridge stopped waiting for
        self._noticing: list[_Command] = []  # commands whose completion notice is pending

    # -- the journal ----------------------------------------------------------------
    def _journal(self, record: Mapping[str, Any]) -> None:
        try:
            self._handoffs.record(record)
        except (OSError, TypeError, ValueError):
            pass  # the effect stands; the tool result still says what happened

    def start(self) -> None:
        """The notifier thread (C-D68 (8)); without a ``notify`` port there are no notices."""
        with self._lock:
            if self._notify is None or self._notifier is not None or self._stop.is_set():
                return
            self._notifier = threading.Thread(target=self._notifier_loop, name="terminal-notifier", daemon=True)
            self._notifier.start()

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        for thread in list(self._threads) + ([self._notifier] if self._notifier is not None else []):
            thread.join(timeout)

    def current(self) -> dict[str, Any] | None:
        """The latest command (for a UI or a test): id, command, log path, running or its result."""
        command = self._current
        if command is None:
            return None
        view = {"command_id": command.command_id, "command": command.command, "log_path": str(command.log_path),
                "started_at": command.started_at, "running": not command.done.is_set()}
        if command.result is not None:
            view["status"] = command.result.get("status")
        return view

    # -- requests ---------------------------------------------------------------------
    def handle(self, role: ActorRole | str, request: Mapping[str, Any]) -> dict[str, Any]:
        """Decide and (if allowed) run one ``terminal`` call; waits at most its timeout; never raises."""
        parsed = HandoffService._parse(role, request)
        if isinstance(parsed, dict):
            self._journal({"type": "terminal_invalid", "reason": parsed["reason"]})
            return parsed
        key, key_dict = parsed.key, parsed.key_dict()
        with self._lock:
            if key in self._results:
                self._journal({"type": "terminal_duplicate", "key": key_dict, "request_id": parsed.request_id})
                return dict(self._results[key])
            if key in self._inflight:
                return {"status": "outcome_unknown", "reason": "duplicate_in_flight"}
            self._inflight.add(key)
        try:
            result = self._handle(parsed, key_dict, key)
        except Exception as exc:  # the command may have started: its outcome is not known here
            result = {"status": "outcome_unknown", "reason": f"backend_error:{type(exc).__name__}"}
        with self._lock:
            self._inflight.discard(key)
            self._results[key] = result
        self._journal({"type": "terminal_result", "key": key_dict, "result": _journal_result(result)})
        return dict(result)

    def _handle(self, parsed: Any, key_dict: dict[str, Any], key: tuple) -> dict[str, Any]:
        if parsed.tool != TERMINAL_TOOL:
            self._journal({"type": "terminal_request", "key": key_dict, "redacted": "unknown_tool"})
            return rejected("unknown_tool")
        if parsed.role is not ActorRole.WORKER:
            self._journal({"type": "terminal_request", "key": key_dict, "redacted": "tool_not_allowed_for_role"})
            return rejected("tool_not_allowed_for_role")
        args = parsed.args
        errors = validate_terminal_arguments(args)
        if errors:
            self._journal({"type": "terminal_request", "key": key_dict, "redacted": "invalid_arguments",
                           "errors": errors})
            return rejected("invalid_arguments", errors=errors)
        command = args.get("command")
        if isinstance(command, str) and not command.strip():
            command = None  # strict schema: a blank optional field means "not used" (D1)
        timeout = self._wait_seconds
        try:
            sensitive = tuple(self._sensitive_values())
        except Exception:
            self._journal({"type": "terminal_request", "key": key_dict, "redacted": "environment_check_unavailable"})
            return rejected("environment_check_unavailable")
        findings = environment_value_findings({"command": command or ""}, sensitive)
        if findings:
            self._journal({"type": "terminal_request", "key": key_dict, "redacted": "environment_value",
                           "fields": findings})
            return rejected("environment_value", fields=findings,
                            detail="Never put environment variable values in a command; use $NAME references.")
        task = self._task_id()
        self._journal({"type": "terminal_request", "key": key_dict, "request_id": parsed.request_id,
                       "command": command, "wait_seconds": timeout if command is not None else 0, "task_id": task})
        deadline = time.monotonic() + timeout
        if command is None:
            return self._fetch(key_dict, key)
        started = self._start(command, key_dict, task, key)
        if isinstance(started, dict):
            return started
        return self._await(started, deadline, key)

    def _task_id(self) -> str | None:
        try:
            active = self._active_task()
        except Exception:
            return None
        return getattr(active, "task_id", None)

    def _refuse(self, key_dict: dict[str, Any], status: str, reason: str, detail: str,
                **extra: Any) -> dict[str, Any]:
        self._journal({"type": "terminal_refused", "key": key_dict, "status": status, "reason": reason})
        return {"status": status, "reason": reason, "detail": detail, **extra}

    def _fetch(self, key_dict: dict[str, Any], key: tuple) -> dict[str, Any]:
        """C-D68 (10): ``command: null`` never waits; it returns the current state at once.

        Running: the output not yet returned by a terminal call (bounded), the
        elapsed time and the running detail; it is not a waiting call, so the
        checks go on. Ended: the result, which counts as received (no
        ``terminal_done`` is sent afterwards).
        """
        command = self._current
        if command is None:
            return self._refuse(key_dict, "rejected", "no_terminal_command",
                                "No command was run; give a command to run one.")
        self._journal({"type": "terminal_fetch", "key": key_dict, "command_id": command.command_id})
        with self._lock:
            self._calls[key] = command
            while len(self._calls) > CALLS_KEPT:
                del self._calls[next(iter(self._calls))]
            final = command.done.is_set() and command.result is not None
            if final:
                command.consumed.add(key)
                self._refresh_notice(command)
        if final:
            return {**command.result, **command.tail()}
        data, dropped = command.take_unseen()
        text, truncated = output_tail(data)
        return {"status": "running", "command_id": command.command_id, "command": command.command,
                "log_path": str(command.log_path), "elapsed_seconds": round(time.monotonic() - command.started, 3),
                "output_tail": text, "output_tail_truncated": truncated or dropped, "detail": RUNNING_DETAIL}

    def _await(self, command: _Command, deadline: float, key: tuple) -> dict[str, Any]:
        with self._lock:
            command.waiters.add(key)
            self._calls[key] = command
            while len(self._calls) > CALLS_KEPT:
                del self._calls[next(iter(self._calls))]
        final = False
        try:
            while not command.done.wait(max(min(deadline - time.monotonic(), 1.0), 0)):
                if time.monotonic() >= deadline or self._stop.is_set():
                    break
        finally:
            with self._lock:
                command.waiters.discard(key)
                final = command.done.is_set() and command.result is not None
                if final:
                    command.consumed.add(key)
                else:  # the call saw the output so far: the next check counts from now
                    command.check_base = self._clock()
                    command.take_since()
                    command.take_unseen()  # the running result below returns the output so far
                    self._drop_check(command, "superseded")
                self._refresh_notice(command)
        if final:
            return {**command.result, **command.tail()}
        return {"status": "running", "command_id": command.command_id, "command": command.command,
                "log_path": str(command.log_path), "elapsed_seconds": round(time.monotonic() - command.started, 3),
                **command.tail(), "detail": RUNNING_DETAIL}

    # -- C-D68 (8): the bridge's abandon signal ----------------------------------------------
    def abandon(self, role: ActorRole | str, request: Mapping[str, Any]) -> dict[str, Any]:
        """``terminal_wait_abandoned``: the bridge answered a terminal call itself (abort or its own timeout).

        That call no longer counts as having received the command's result, so
        the completion notice is sent (once) when no other call got it. A signal
        that arrives before the call registered still counts.
        """
        parsed = HandoffService._parse(role, request)
        if isinstance(parsed, dict):
            return parsed
        if parsed.role is not ActorRole.WORKER:
            return rejected("tool_not_allowed_for_role")
        args = parsed.args
        original = args.get("tool_call_id") if isinstance(args, dict) else None
        if (not isinstance(args, dict) or set(args) != {"tool_call_id"} or not isinstance(original, str)
                or not 0 < len(original) <= 256):
            return rejected("invalid_arguments", errors=["tool_call_id: the id of the terminal call that stopped "
                                                         "waiting (1-256 characters)"])
        self._mark_abandoned((parsed.role.value, parsed.session_id, parsed.generation, original),
                             "terminal_wait_abandoned")
        return {"status": "recorded"}

    def undelivered(self, role: ActorRole | str, request: Mapping[str, Any]) -> None:
        """The bridge server could not write this ``terminal`` call's result (p27-cd68-fix-01 P2-1)."""
        parsed = HandoffService._parse(role, request)
        if isinstance(parsed, dict) or parsed.role is not ActorRole.WORKER:
            return
        self._mark_abandoned(parsed.key, "terminal_result_undelivered")

    def peer_gone(self, role: ActorRole | str, session_id: str, generation: int) -> None:
        """The bridge connection of a session ended (closed or replaced; review-02 P3 (3)).

        Its waiting ``terminal`` calls can no longer get a result: they are
        dropped as abandoned, so checks resume and the completion notice is sent.
        """
        role_value = getattr(role, "value", role)
        if role_value != ActorRole.WORKER.value:
            return
        with self._lock:
            keys = [key for key, command in self._calls.items()
                    if key[:3] == (role_value, session_id, generation) and key in command.waiters]
        if keys:
            self._journal({"type": "terminal_peer_gone", "session_id": session_id, "generation": generation,
                           "calls": len(keys)})
        for key in keys:
            self._mark_abandoned(key, "terminal_wait_abandoned")

    def host_operator(self) -> str | None:
        """``worker`` while the worker's terminal command holds the host shell (smoke-01 P2), else None."""
        return "worker" if self._gate.owner == "terminal" else None

    def _mark_abandoned(self, key: tuple, kind: str) -> None:
        with self._lock:
            self._abandoned[key] = None
            while len(self._abandoned) > CALLS_KEPT:
                del self._abandoned[next(iter(self._abandoned))]
            command = self._calls.get(key)
            if command is not None:
                self._refresh_notice(command)
        self._journal({"type": kind, "key": {"role": key[0], "session_id": key[1], "generation": key[2],
                                             "tool_call_id": key[3]},
                       "command_id": None if command is None else command.command_id})

    def _abandoned_now(self, key: tuple) -> bool:
        with self._lock:
            return key in self._abandoned

    # -- C-D68 (8): checks and the completion notice ------------------------------------------
    def _waiting_now(self) -> bool:
        """Whether a live terminal call waits for the current command (no check is sent then)."""
        with self._lock:
            command = self._current
            return command is not None and self._live_waiters(command)

    def _live_waiters(self, command: _Command) -> bool:
        return any(key not in self._abandoned for key in command.waiters)

    def _notice_record(self, command: _Command, kind: str, outcome: str, **extra: Any) -> None:
        self._journal({"type": kind, "command_id": command.command_id, "outcome": outcome, "at": _now(), **extra})

    def _drop_check(self, command: _Command, outcome: str) -> None:
        """Under the service lock: forget the pending check."""
        if command.check_pending is not None:
            self._notice_record(command, "terminal_check", outcome, notice_id=command.check_pending)
            command.check_pending = None

    def _refresh_notice(self, command: _Command) -> None:
        """Under the service lock: is the completion notice needed (the command ended, no live call got it)?"""
        if not command.done.is_set() or command.result is None or command.notice not in ("none", "not_needed",
                                                                                          "pending"):
            return
        live = any(key not in self._abandoned for key in command.waiters | command.consumed)
        if not live and command.notice != "pending":
            command.notice = "pending"
            command.notice_id = command.notice_id or str(uuid4())
            command.notice_due = self._clock()
            if command not in self._noticing:
                self._noticing.append(command)
            self._notice_record(command, "terminal_notice", "pending", notice_id=command.notice_id)
            self._wake.set()
        elif live and command.notice != "not_needed":
            command.notice = "not_needed"
            self._notice_record(command, "terminal_notice", "not_needed")

    def _notifier_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._tick_interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.tick()
            except Exception:
                pass  # the next tick tries again; the command itself is never affected

    def tick(self) -> None:
        """One notifier step: pending completion notices, then the running command's check."""
        if self._notify is None:
            return
        with self._tick_lock:
            now = self._clock()
            paused = self._paused_now()
            with self._lock:
                self._noticing = [item for item in self._noticing if item.notice in ("pending", "sending")]
                noticing = list(self._noticing)
                current = self._current
            for command in noticing:
                self._notice_step(command, now, paused)
            if current is not None and not current.done.is_set():
                self._check_step(current, now, paused)

    def _send(self, notice: Mapping[str, Any]) -> str:
        try:
            outcome = self._notify(notice)  # type: ignore[misc]
        except Exception:
            return "unknown"  # it may have reached the worker: never resent
        return outcome if outcome in ("delivered", "deferred", "not_connected", "paused", "unknown",
                                      "rejected") else "unknown"

    def _check_step(self, command: _Command, now: float, paused: bool) -> None:
        with self._lock:
            if command.done.is_set() or self._live_waiters(command):
                return  # a waiting call sees the output itself
            if now >= command.check_base + self._check_interval:
                command.check_base = now
                if paused:
                    self._drop_check(command, "skipped_paused")
                    self._notice_record(command, "terminal_check", "skipped_paused")
                    return
                if command.check_pending is None:
                    command.check_pending, command.check_coalesced = str(uuid4()), 0
                    command.check_retry_at, command.check_deferred_logged = now, False
                else:  # the worker is still busy: one pending check, the latest
                    command.check_coalesced += 1
                    self._notice_record(command, "terminal_check", "merged", notice_id=command.check_pending,
                                        coalesced_count=command.check_coalesced)
            if command.check_pending is None or now < command.check_retry_at:
                return
            if paused:
                self._drop_check(command, "skipped_paused")
                return
            notice_id, coalesced = command.check_pending, command.check_coalesced
        data, dropped = command.take_since()
        text, truncated = output_tail(data)
        notice = {"notice_id": notice_id, "type": "terminal_check", "command_id": command.command_id,
                  "command": command.command, "cwd": command.cwd, "task_id": command.task_id, "state": "running",
                  "exit_code": None, "elapsed_seconds": round(time.monotonic() - command.started, 1),
                  "new_output": text, "new_output_truncated": truncated or dropped,
                  "log_path": str(command.log_path), "coalesced_count": coalesced,
                  "instruction": CHECK_INSTRUCTION}
        outcome = self._send(notice)
        with self._lock:
            if outcome != "delivered" and outcome != "unknown":
                command.put_back_since(data, dropped)
            if command.check_pending != notice_id:
                return  # ended or superseded meanwhile
            if outcome in ("deferred", "not_connected"):
                command.check_retry_at = now + self._notice_retry
                if not command.check_deferred_logged:
                    command.check_deferred_logged = True
                    self._notice_record(command, "terminal_check", "deferred", notice_id=notice_id,
                                        reason=outcome)
                return
            command.check_pending = None
            self._notice_record(command, "terminal_check", {"delivered": "sent"}.get(outcome, outcome),
                                notice_id=notice_id, coalesced_count=coalesced, new_output_bytes=len(data))

    def _notice_step(self, command: _Command, now: float, paused: bool) -> None:
        with self._lock:
            if command.notice != "pending" or now < command.notice_due:
                return
            if paused:
                if "held_paused" not in command.notice_logged:
                    command.notice_logged.add("held_paused")
                    self._notice_record(command, "terminal_notice", "held_paused", notice_id=command.notice_id)
                return
            command.notice = "sending"
        result = command.result or {}
        notice = {"notice_id": command.notice_id, "type": "terminal_done", "command_id": command.command_id,
                  "command": command.command, "cwd": command.cwd, "task_id": command.task_id,
                  "status": result.get("status"), "exit_code": result.get("exit_code"),
                  "signal": result.get("signal"), "reason": result.get("reason"), **command.tail(),
                  "log_path": str(command.log_path), "duration_seconds": result.get("duration_seconds"),
                  "instruction": DONE_INSTRUCTION}
        outcome = self._send(notice)
        with self._lock:
            if outcome in ("deferred", "not_connected", "paused"):
                command.notice = "pending"
                command.notice_due = now + self._notice_retry
                state = "held_paused" if outcome == "paused" else "deferred"
                if state not in command.notice_logged:
                    command.notice_logged.add(state)
                    self._notice_record(command, "terminal_notice", state, notice_id=command.notice_id,
                                        reason=outcome)
                self._refresh_notice(command)  # a call may have got the result meanwhile
                return
            command.notice = {"delivered": "sent"}.get(outcome, outcome)
            self._notice_record(command, "terminal_notice", command.notice, notice_id=command.notice_id)

    # -- starting a command -------------------------------------------------------------
    def _start(self, text: str, key_dict: dict[str, Any], task: str | None,
               key: tuple = ()) -> _Command | dict[str, Any]:
        with self._lock:
            current = self._current
            if self._starting or (current is not None and not current.done.is_set()):
                running = current if current is not None and not current.done.is_set() else None
                extra = {} if running is None else {"command_id": running.command_id, "command": running.command,
                                                     "log_path": str(running.log_path)}
                return self._refuse(key_dict, "terminal_command_running", "terminal_command_running",
                                    "One command at a time: do not start another until Workbench notifies you that it ended.",
                                    **extra)
            self._starting = True
        try:
            return self._start_unlocked(text, key_dict, task, key)
        finally:
            with self._lock:
                self._starting = False

    def _paused_now(self) -> bool:
        try:
            return self._paused() is not False
        except Exception:
            return True  # an unknown pause state holds

    def _start_unlocked(self, text: str, key_dict: dict[str, Any], task: str | None,
                        key: tuple = ()) -> _Command | dict[str, Any]:
        if self._abandoned_now(key):  # P3-4: the bridge already answered this call: nothing is typed
            self._journal({"type": "terminal_aborted", "key": key_dict, "stage": "before_start"})
            return {"status": "aborted", "reason": "call_abandoned", "detail": ABORTED_DETAIL}
        if self._paused_now():
            return self._refuse(key_dict, "paused", "paused", PAUSED_DETAIL)
        busy = self._gate.acquire("terminal", self._activity)
        if busy is not None:
            return self._refuse(key_dict, "host_terminal_busy", _busy_reason(busy), BUSY_DETAIL)
        port = None
        handed_over = False
        try:
            try:
                port = self._host_shell()
            except Exception:
                port = None
            if port is None:
                return self._refuse(key_dict, "host_terminal_busy", "host terminal is not running", BUSY_DETAIL)
            try:
                reason = port.busy() or port.hold(HOLD_REASON)
            except Exception as exc:
                reason = f"host shell state unknown ({type(exc).__name__})"
            if reason is not None:
                return self._refuse(key_dict, "host_terminal_busy", str(reason), BUSY_DETAIL)
            if self._paused_now():  # a pause between the first check and the hold: nothing typed
                port.release_hold(HOLD_REASON)
                return self._refuse(key_dict, "paused", "paused", PAUSED_DETAIL)
            command = self._new_command(text, key_dict, task)
            if isinstance(command, dict):
                port.release_hold(HOLD_REASON)
                return command
            try:
                self._dispatch(port, command, key)
            except _CallAbandoned:
                port.release_hold(HOLD_REASON)
                self._give_back(port, after_failure=True)
                try:
                    command.log_path.unlink()  # nothing ran: no empty log is left (review-02 P3 (2))
                except OSError:
                    pass
                self._journal({"type": "terminal_aborted", "key": key_dict, "stage": "before_submit",
                               "command_id": command.command_id})
                return {"status": "aborted", "reason": "call_abandoned", "command_id": command.command_id,
                        "detail": ABORTED_DETAIL}
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:300]
                port.release_hold(HOLD_REASON)
                self._give_back(port, after_failure=True)
                self._journal({"type": "terminal_start_failed", "command_id": command.command_id,
                               "key": key_dict, "reason": error, "cwd": command.cwd or None})
                return {"status": "start_failed", "reason": error, "command_id": command.command_id,
                        "detail": "The command was not started; the host terminal is the user's again."}
            port.release_hold(HOLD_REASON)
            with self._lock:
                self._current = command
            self._journal({"type": "terminal_started", "command_id": command.command_id, "key": key_dict,
                           "task_id": task, "command": command.command, "cwd": command.cwd,
                           "started_at": command.started_at, "log_path": str(command.log_path)})
            thread = threading.Thread(target=self._follow, args=(port, command), daemon=True,
                                      name=f"terminal-{command.command_id[:8]}")
            self._threads = [item for item in self._threads if item.is_alive()] + [thread]
            handed_over = True
            thread.start()
            return command
        finally:
            if not handed_over:
                if port is not None:
                    try:
                        port.detach()
                    except Exception:
                        pass
                self._gate.release("terminal")

    def _new_command(self, text: str, key_dict: dict[str, Any], task: str | None) -> _Command | dict[str, Any]:
        command_id = str(uuid4())
        try:
            self._log_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            log_path = self._log_root / f"{command_id}.log"
            os.close(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
        except OSError as exc:  # no durable log: nothing is run (C-D46)
            return self._refuse(key_dict, "rejected", "log_unavailable",
                                f"The command log could not be created ({type(exc).__name__}); nothing was run.")
        return _Command(command_id, text, key_dict, task, log_path)

    def _dispatch(self, port: Any, command: _Command, key: tuple = ()) -> None:
        """The experiment start's managed dispatch for one command (input is held by the caller).

        The command runs where the user's shell is (C-D68 (7)): no directory is
        typed; the child inherits the parent's cwd, read from /proc.
        """
        where = port.cwd()
        if where is None:
            raise RuntimeError("host shell directory unknown; no command sent")
        command.cwd = where
        if self._abandoned_now(key):
            raise _CallAbandoned
        port.send_user(b"wb-handoff\n")
        deadline = time.monotonic() + PREPARE_WAIT
        while time.monotonic() < deadline and port.poll(0.02)["parent_mode"] != "control_wait":
            port.display_bytes()
        port.claim_manager()
        port.display_bytes()
        state = port.snapshot()
        approval = sha256(json.dumps({"terminal": command.command_id, "command": command.command},
                                     sort_keys=True).encode()).hexdigest()
        control = {"portVersion": 2, "kind": "ShellControl", "payload": {
            "parentPid": state["parent_pid"], "generation": state["generation"],
            "ownerEpoch": state["owner_epoch"], "requestId": command.command_id,
            "approvalHash": approval, "phase": "accepted"}}
        if self._paused_now():
            raise RuntimeError("paused before the command was sent")
        if port.cwd() != command.cwd:  # input was held: only a moved or replaced shell gets here
            raise RuntimeError("host shell directory changed before the command was sent; no command sent")
        if self._abandoned_now(key):  # the last point before the command is typed
            raise _CallAbandoned
        command.started, command.started_at = time.monotonic(), _now()
        command.check_base = self._clock()
        executable = getattr(getattr(port, "choice", None), "executable", None)
        if not isinstance(executable, str) or not executable:
            raise RuntimeError("host shell executable unknown; no command sent")
        command.echo = f"[worker] $ {command.command}\n".encode().replace(b"\n", b"\r\n")
        port.submit(control, [executable, "-c", ECHO_SCRIPT, executable, command.command],
                    dict(self._automation()))

    # -- following a command ----------------------------------------------------------------
    def _follow(self, port: Any, command: _Command) -> None:
        result: dict[str, Any]
        try:
            result = self._collect(port, command)
        except Exception as exc:  # replaced, exited or unreadable shell: no exit is claimed
            command.write(_safe_drain(port))
            result = {"status": "unknown", "reason": f"{type(exc).__name__}: {exc}"[:300]}
        try:
            if result["status"] == "exited":
                self._give_back(port)
        finally:
            try:
                port.detach()
            except Exception:
                pass
            self._gate.release("terminal")
        duration = round(time.monotonic() - command.started, 3)
        result.update({"command_id": command.command_id, "command": command.command, "cwd": command.cwd,
                       "log_path": str(command.log_path), "duration_seconds": duration})
        if command.log_truncated:
            result["log_truncated"] = True
        if command.log_error is not None:
            result["log_error"] = command.log_error
        self._journal({"type": "terminal_ended", "command_id": command.command_id, "status": result["status"],
                       "exit_code": result.get("exit_code"), "signal": result.get("signal"),
                       "reason": result.get("reason"), "started_at": command.started_at, "ended_at": _now(),
                       "duration_seconds": duration, "log_path": str(command.log_path),
                       "log_bytes": command.log_bytes, "log_truncated": command.log_truncated,
                       "cwd": command.cwd})
        command.result = result
        command.done.set()
        with self._lock:
            self._drop_check(command, "superseded")  # the completion notice replaces it
            self._refresh_notice(command)

    def _collect(self, port: Any, command: _Command) -> dict[str, Any]:
        while True:
            state = port.poll(self._poll)
            command.write(port.display_bytes())
            life = state["lifecycle"]
            if life["input_barrier"] and not life["input_returned"]:
                port.release_input()
                continue
            if state["phase"] == "unknown":
                return {"status": "unknown", "reason": "host shell state unknown: "
                        + ", ".join(life.get("unknown") or state.get("held_reasons") or [])}
            if life["control_returned"] and life["input_returned"] and life["lifetime"] == "ended":
                command.write(port.display_bytes())
                exit_status = life["main_exit"]
                if not isinstance(exit_status, int):
                    return {"status": "unknown", "reason": "exit status not observed"}
                if exit_status < 0:
                    return {"status": "exited", "exit_code": 128 - exit_status, "signal": -exit_status}
                return {"status": "exited", "exit_code": exit_status}
            if self._stop.is_set():
                return {"status": "unknown", "reason": "backend stopping"}

    def _give_back(self, port: Any, *, after_failure: bool = False) -> None:
        """Return the shell to the user (``TaskFlow._give_back``); its directory was never changed."""
        try:
            state = port.snapshot()
            life = state.get("lifecycle") or {}
            returned = (not life.get("unknown") and life.get("lifetime") == "ended"
                        and life.get("input_returned") and life.get("control_returned"))
            idle_wait = (life.get("request_id") is None and after_failure
                         and state.get("parent_mode") == "control_wait")  # wb-handoff typed, never claimed
            if (state.get("input_owner") == "manager" and (life.get("request_id") is None or returned)) or idle_wait:
                port.request_takeover()
        except Exception:
            pass


def _busy_reason(owner: str) -> str:
    return {"experiment": "an experiment run uses the host terminal",
            "terminal": "a terminal command is starting"}.get(owner, owner)


def _safe_drain(port: Any) -> bytes:
    try:
        return port.display_bytes()
    except Exception:
        return b""


def _journal_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """The result as journaled: no output (only the log file has it)."""
    return {name: value for name, value in result.items() if name not in ("output_tail", "detail")}
