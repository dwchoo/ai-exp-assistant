"""C-D70: the Workbench watchdog, recovery notices and the manager's recovery tools.

Workbench watches the worker itself instead of making the manager poll it
(turn and token cost). Everything here runs on one ``workbench-watchdog``
thread (``Watchdog.tick``; a test drives ``tick`` with a fake clock) and only
reads in-memory state, except one bridge ``probe`` of the worker at most every
``PROBE_INTERVAL`` seconds and the notices it sends.

- Status checks (C-D70 (1)): while a work Task is open and running, its TASK
  reached the worker's current OMP session, the worker OMP is idle (probe:
  idle, nothing pending, no approval, no tool running; no turn event since the
  last tick), no terminal command the worker started runs or waits for its
  completion notice, no outbox message to or from the worker is pending or
  being delivered, and automation is not paused, for ``IDLE_LIMIT`` (60 s)
  without a break, the worker gets one ``status_check`` Workbench notice (the
  bridge ``notice`` frame, sent only into an idle, unpaused OMP with an empty
  composer). At most ``MAX_CHECKS`` (2) in a row per Task. A worker
  ``terminal`` or ``to_manager`` call, or a manager follow-up the worker OMP
  accepted (p27-cd70-02 Q2), (``worker_acted``) starts the count again. After the last check and another ``IDLE_LIMIT`` of idleness the
  manager gets one ``worker_stalled`` notice for that Task; there is no second
  one until the worker acts again.
- Restarts (C-D70 (4)/(5)): a new worker OMP session (any cause) while a Task
  is open -> one ``worker_restarted`` notice to the manager. A new manager OMP
  session -> worker reports that were never submitted are addressed to it
  again (``requeue``) and the manager gets one ``manager_recovery`` notice
  (open Task, worker, terminal, how many reports go to it again and the
  reports whose delivery is unknown, listed without content).
- Report delivery (C-D70 (2)): a worker report whose delivery ended unknown
  before any acceptance -> one ``report_delivery_unknown`` notice to the
  manager (Task id, message id, report kind; never the content). A report
  deferred longer than ``EDITOR_WAIT`` (30 s) because the manager's composer
  is not empty is shown in the UI status line (``report_wait``), and so is a
  report queued while no manager OMP is connected (``waiting_for:
  manager_session``; reason ``manager_session_not_connected``, shown at once,
  cleared when it is delivered).
- Notices are sent once each: a deferred, paused or not connected attempt is
  retried with the same ``notice_id`` (the bridge refuses a second copy); a
  delivered, unknown or rejected one is never sent again. Every outcome is
  journaled (``workbench_notice``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from workbench.contracts.v1 import ActorRole

IDLE_LIMIT = 60.0  # C-D70: the user's choice "60초"
MAX_CHECKS = 2  # status checks in a row per Task before the manager is told
PROBE_INTERVAL = 5.0  # the worker OMP is probed at most this often
EDITOR_WAIT = 30.0  # a worker report waiting this long on the manager's composer is shown in the UI
NOTICE_RETRY = 2.0  # a deferred notice is tried again after this long
WATCH_TICK = 1.0
REASON_MAX = 500  # restart_worker reason
STATUS_TEXT_MAX = 2000  # one report's text in workbench_status
STATUS_REPORTS_MAX = 10
RESTARTS_SHOWN = 5

RESTART_WORKER_TOOL = "restart_worker"
STATUS_TOOL = "workbench_status"
STOP_SURVIVOR_TOOL = "stop_survivor"  # C-D71 (1): end one proven process the previous backend left running
MANAGER_TOOLS = (RESTART_WORKER_TOOL, STATUS_TOOL, STOP_SURVIVOR_TOOL)
WORKER_NOTICE_TYPES = ("terminal_check", "terminal_done", "status_check")
MANAGER_NOTICE_TYPES = ("worker_stalled", "report_delivery_unknown", "worker_restarted", "manager_recovery",
                        "worker_terminal_done",  # p27-cd70-02 Q3: a previous worker session's command ended
                        "backend_restarted")  # C-D71 (2): the backend restarted with an open Task
SURVIVOR_ID_MAX = 16

STATUS_CHECK_INSTRUCTION = (
    "Workbench status check (automatic): your Task is open and you have been idle for {idle} s with no terminal "
    "command running and no message on its way. If the Task is finished, report done with to_manager; if you are "
    "blocked, report blocked with to_manager (say why); if listed commands remain, run the next one with the "
    "terminal tool; otherwise end your turn with one short text line (never an empty reply).")
STALLED_HINT = (
    "Workbench notice: the worker sent no report and ran no command after {checks} automatic status checks. Use the "
    "workbench-recovery skill: call workbench_status, then decide: a follow-up to_worker on this task_id (continue or "
    "clarify), cancel the Task, or restart_worker with a reason if the worker is stuck or not responding. Do not run "
    "the worker's commands yourself. Tell the user briefly.")
RESTARTED_HINT = (
    "Workbench notice: the worker OMP is a new session and has no memory of the open Task (it is not cancelled). "
    "This notice is the cue to continue: send exactly one follow-up to_worker on this task_id now (Workbench re-sends "
    "the full Task and the commands already run), or cancel the Task; if you already sent that follow-up after "
    "this restart, just end your turn. Use the workbench-recovery skill (workbench_status first if unsure). Tell "
    "the user briefly.")
UNKNOWN_HINT = (
    "Workbench notice: a worker report for this Task may not have reached you (its delivery outcome is unknown and "
    "it is not re-sent). Call workbench_status to read it, then continue as the workbench-recovery skill says.")
RECOVERY_HINT = (
    "Workbench notice: your OMP session is new. Use the workbench-recovery skill: call workbench_status for the open "
    "Task, the worker, the terminal and the reports. Reports that never reached your old session, or that the worker "
    "sent while your OMP was down, arrive as messages (reports_resent); wait for them before you cancel the Task. "
    "Reports whose delivery is unknown are only listed (read them with workbench_status). Tell the user briefly "
    "what happened.")
RESTART_DETAIL = (
    "The worker OMP was restarted as a new session; it has no memory. The open Task was not cancelled and a host "
    "terminal command the worker started keeps running. Do not send a follow-up now: end your turn. Workbench sends "
    "a worker_restarted notice once the new worker is ready; then send exactly one follow-up to_worker on the same "
    "task_id (Workbench re-sends the full Task and the commands already run), or cancel the Task.")
RESTART_PENDING_DETAIL = (
    "The worker restart is still being carried out (the new worker is starting). Do not send a follow-up now: end "
    "your turn. Workbench sends a worker_restarted notice once the new worker is ready; then send exactly one "
    "follow-up to_worker on the same task_id, or cancel the Task.")
# review-02 P3-W: without an open Task no worker_restarted notice comes and there is nothing to re-send.
RESTART_NO_TASK_DETAIL = (
    "The worker OMP was restarted as a new session; it has no memory. There is no open Task, so no worker_restarted "
    "notice follows and nothing is re-sent; a host terminal command the worker started keeps running. Send a new "
    "Task with to_worker only when there is new work.")
RESTART_NO_TASK_PENDING_DETAIL = (
    "The worker restart is still being carried out (the new worker is starting); it will have no memory. There is "
    "no open Task, so no worker_restarted notice follows and nothing is re-sent. End your turn; send a new Task with "
    "to_worker only when there is new work.")


BACKEND_RESTARTED_HINT = (
    "Workbench notice: the Workbench backend restarted (this OMP session is new and has no memory). The open Task "
    "was not cancelled and nothing was re-sent or re-run; a run that was going on has an unknown outcome. "
    "survivors lists processes the previous backend left running (Workbench never ends them by itself). Use the "
    "workbench-recovery skill: call workbench_status, then decide per survivor (stop_survivor with a reason, or "
    "leave it) and continue the Task (one follow-up to_worker on its task_id, run: true to re-run an experiment, "
    "or cancel). Tell the user briefly what happened.")


def restart_detail(open_task: bool, ready: bool) -> str:
    """The restart_worker result text: true with and without an open Task (review-02 P3-W)."""
    if open_task:
        return RESTART_DETAIL if ready else RESTART_PENDING_DETAIL
    return RESTART_NO_TASK_DETAIL if ready else RESTART_NO_TASK_PENDING_DETAIL


def validate_restart_arguments(args: object) -> list[str]:
    """Errors naming the field and the expected value (never the value sent)."""
    if not isinstance(args, dict):
        return ["arguments: must be an object {reason}"]
    errors = [f"{name}: unknown argument (allowed: reason)" for name in sorted(set(args) - {"reason"})]
    reason = args.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_MAX or "\x00" in reason:
        errors.append(f"reason: required, a non-blank string of at most {REASON_MAX} characters (why the worker "
                      "is restarted; it is recorded and shown to the user)")
    return errors


def validate_stop_survivor_arguments(args: object) -> list[str]:
    """C-D71 (1): ``{survivor_id, reason}``; the errors name the field, never the value."""
    if not isinstance(args, Mapping):
        return ["arguments: an object {survivor_id, reason} is required"]
    errors = [f"{name}: not a stop_survivor field" for name in args if name not in ("survivor_id", "reason")]
    survivor = args.get("survivor_id")
    if not isinstance(survivor, str) or not survivor.strip() or len(survivor) > SURVIVOR_ID_MAX:
        errors.append(f"survivor_id: a survivor id from workbench_status (at most {SURVIVOR_ID_MAX} characters)")
    reason = args.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_MAX:
        errors.append(f"reason: a non-empty text of at most {REASON_MAX} characters is required")
    return errors


def validate_status_arguments(args: object) -> tuple[str | None, list[str]]:
    """(task_id or None, errors); a blank task_id (strict-schema placeholder) means none."""
    if args is None:
        return None, []
    if not isinstance(args, dict):
        return None, ["arguments: must be an object {task_id}"]
    errors = [f"{name}: unknown argument (allowed: task_id)" for name in sorted(set(args) - {"task_id"})]
    task_id = args.get("task_id")
    if task_id is None or isinstance(task_id, str) and not task_id.strip():
        return None, errors
    if not isinstance(task_id, str) or len(task_id) != 36:
        errors.append("task_id: the canonical UUID of a Task, or null for the current one")
        return None, errors
    return task_id, errors


def bounded(text: object, limit: int = STATUS_TEXT_MAX) -> tuple[str | None, bool]:
    """(text cut to ``limit`` characters, whether it was cut)."""
    if not isinstance(text, str):
        return None, False
    return text[:limit], len(text) > limit


def _wall(clock_now: float, at: float | None, wall_now: float) -> float | None:
    return None if at is None else round(wall_now - (clock_now - at), 3)


@dataclass
class _TaskWatch:
    task_id: str
    checks: int = 0
    stalled: bool = False
    idle_since: float | None = None
    last_probe: float | None = None
    pending: dict[str, Any] | None = None  # the status check waiting for a deliverable worker
    retry_at: float = 0.0
    tries: int = 0  # attempts of the pending check


@dataclass
class _Queued:
    role: ActorRole
    notice: dict[str, Any]
    retry_at: float = 0.0
    attempts: int = 0
    handoff_id: str | None = None  # the report a report_delivery_unknown notice is about
    # p27-cw19-fix-01 (VM F1): the notice's fields built again right before each attempt (the current state);
    # None from it means the notice no longer applies and it is dropped unsent
    build: Callable[[], Mapping[str, Any] | None] | None = None


@dataclass
class WatchPorts:
    """What the watchdog reads and does; every port may fail (it then counts as "not known")."""

    task: Callable[[], Mapping[str, Any] | None]  # TaskFlow.watch_view: the open Task, else None
    task_summary: Callable[[], Mapping[str, Any] | None] = lambda: None  # TaskFlow.task_view
    peer: Callable[[ActorRole], Any] = lambda role: None  # BridgePeer (session_id, generation, pid) or None
    probe: Callable[[ActorRole], Mapping[str, Any] | None] = lambda role: None  # the OMP's bridge state
    turns: Callable[[int], tuple[int, bool]] = lambda cursor: (cursor, False)  # worker turn events since cursor
    terminal: Callable[[], Mapping[str, Any]] = lambda: {}  # TerminalService.watch_state
    outbox_busy: Callable[[ActorRole], bool] = lambda role: False  # HandoffService.lane_busy
    reports: Callable[[], list[Mapping[str, Any]]] = lambda: []  # HandoffService.report_entries
    requeue: Callable[[], int] = lambda: 0  # HandoffService.requeue_for_new_session(MANAGER)
    paused: Callable[[], bool] = lambda: False
    notify: Callable[[ActorRole, Mapping[str, Any]], str] | None = None  # the backend's bridge notice frame
    restart_cause: Callable[[Any], Mapping[str, Any] | None] = lambda peer: None
    journal: Callable[[Mapping[str, Any]], None] = lambda record: None
    worker_state: Callable[[], Mapping[str, Any] | None] = lambda: None  # TaskFlow.worker_view


class Watchdog:
    """See the module docstring."""

    def __init__(self, ports: WatchPorts, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, idle_limit: float = IDLE_LIMIT,
                 max_checks: int = MAX_CHECKS, probe_interval: float = PROBE_INTERVAL,
                 editor_wait: float = EDITOR_WAIT, notice_retry: float = NOTICE_RETRY,
                 tick_interval: float = WATCH_TICK):
        self._ports = ports
        self._clock, self._wall = clock, wall
        self.idle_limit, self.max_checks = idle_limit, max_checks
        self.probe_interval, self.editor_wait = probe_interval, editor_wait
        self.notice_retry, self.tick_interval = notice_retry, tick_interval
        self._lock = threading.Lock()  # state shared with worker_acted() and the views
        self._tick_lock = threading.Lock()
        self._watch: _TaskWatch | None = None
        self._sessions: dict[ActorRole, tuple[str, int]] = {}
        self._queue: list[_Queued] = []
        self._noticed_unknown: set[str] = set()
        self._cursor = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            return
        self._thread = threading.Thread(target=self._loop, name="workbench-watchdog", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 7.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_interval):
            try:
                self.tick()
            except Exception:
                pass  # the next tick tries again; nothing here changes a Task

    # -- events from the backend ----------------------------------------------------------
    def worker_acted(self, tool: str = "") -> None:
        """The worker called ``terminal`` or ``to_manager``, or a manager follow-up reached it: the check count and
        the stalled notice start over."""
        with self._lock:
            watch = self._watch
            if watch is not None:
                if watch.checks or watch.stalled:
                    self._record({"type": "watchdog_reset", "task_id": watch.task_id, "tool": tool,
                                  "checks": watch.checks, "stalled": watch.stalled})
                watch.checks, watch.stalled, watch.idle_since, watch.pending = 0, False, None, None

    # -- views ------------------------------------------------------------------------------
    def view(self) -> dict[str, Any]:
        """ui_v1 ``recovery``: the editor wait of worker reports and the watch of the open Task (no counters
        that change every tick, so an unchanged state is not pushed again)."""
        now, wall = self._clock(), self._wall()
        with self._lock:
            watch = self._watch
            watching = None if watch is None else {
                "task_id": watch.task_id, "checks_sent": watch.checks, "max_checks": self.max_checks,
                "stalled_notified": watch.stalled, "idle_since": _wall(now, watch.idle_since, wall)}
        return {"report_wait": self.report_wait(), "watch": watching}

    def report_wait(self) -> dict[str, Any] | None:
        """Worker reports deferred at least ``editor_wait`` because the manager's composer is not empty."""
        now, wall = self._clock(), self._wall()
        waiting, no_manager = [], False
        for entry in self._reports():
            if entry.get("state") not in ("pending", "delivering") or entry.get("submitted"):
                continue
            since = entry.get("editor_since")
            queued = entry.get("waiting_since")
            if entry.get("waiting_for") == "manager_session" and isinstance(queued, (int, float)):
                # p27-cd70-ui-01: queued while no manager OMP is connected; shown at once (the OMP is gone)
                waiting.append(queued)
                no_manager = True
            elif isinstance(since, (int, float)) and now - since >= self.editor_wait:
                waiting.append(since)
        if not waiting:
            return None
        reason = "manager_session_not_connected" if no_manager else "manager_editor_not_empty"
        return {"count": len(waiting), "reason": reason, "since": _wall(now, min(waiting), wall)}

    # -- the tick ---------------------------------------------------------------------------
    def tick(self) -> None:
        with self._tick_lock:
            now = self._clock()
            self._sessions_step(now)
            self._unknown_reports_step(now)
            self._worker_step(now)
            self._send_queue(now)

    def _call(self, port: Callable[..., Any], *args: Any, default: Any = None) -> Any:
        try:
            return port(*args)
        except Exception:
            return default

    def _record(self, record: Mapping[str, Any]) -> None:
        try:
            self._ports.journal(record)
        except Exception:
            pass

    def _reports(self) -> list[Mapping[str, Any]]:
        reports = self._call(self._ports.reports, default=[])
        return [item for item in reports or [] if isinstance(item, Mapping)]

    def _terminal(self) -> Mapping[str, Any]:
        state = self._call(self._ports.terminal, default=None)
        return state if isinstance(state, Mapping) else {"running": None}

    def _terminal_summary(self) -> dict[str, Any]:
        state = self._terminal()
        last = state.get("last") if isinstance(state.get("last"), Mapping) else None
        return {"running": state.get("running"), "last": None if last is None else dict(last)}

    def _enqueue(self, role: ActorRole, notice_type: str, fields: Mapping[str, Any], *, first: bool = False,
                 handoff_id: str | None = None,
                 build: Callable[[], Mapping[str, Any] | None] | None = None) -> str:
        notice = {"notice_id": str(uuid4()), "type": notice_type, **fields}
        item = _Queued(role, notice, handoff_id=handoff_id, build=build)
        if first:
            self._queue.insert(0, item)
        else:
            self._queue.append(item)
        self._record({"type": "workbench_notice", "role": role.value, "notice_id": notice["notice_id"],
                      "notice_type": notice_type, "task_id": fields.get("task_id"), "outcome": "queued"})
        return notice["notice_id"]

    # sessions (C-D70 (4)/(5)) -------------------------------------------------------------
    def _sessions_step(self, now: float) -> None:
        for role in (ActorRole.WORKER, ActorRole.MANAGER):
            peer = self._call(self._ports.peer, role)
            if peer is None:
                continue
            key = (getattr(peer, "session_id", None), getattr(peer, "generation", None))
            previous = self._sessions.get(role)
            self._sessions[role] = key
            if previous is None or previous == key:
                continue
            if role is ActorRole.WORKER:
                self._worker_new_session(previous, key, peer)
            else:
                self._manager_new_session(previous, key)

    def _worker_new_session(self, previous: tuple[str, int], key: tuple[str, int], peer: Any) -> None:
        with self._lock:
            self._watch = None  # the new session never got the Task: no status check until it does
        task = self._call(self._ports.task)
        if not isinstance(task, Mapping):  # a blocked Task is open for a follow-up too (the worker is free)
            summary = self._call(self._ports.task_summary)
            task = summary if isinstance(summary, Mapping) and summary.get("status") == "blocked" else None
        if not isinstance(task, Mapping):
            self._record({"type": "worker_new_session", "session_id": key[0], "generation": key[1],
                          "task_id": None})
            return
        cause = self._call(self._ports.restart_cause, peer)
        cause = dict(cause) if isinstance(cause, Mapping) else {"cause": "unknown"}
        self._enqueue(ActorRole.MANAGER, "worker_restarted", {
            "task_id": task.get("task_id"), "task_kind": task.get("kind"), "task_status": task.get("status"),
            "cause": cause.get("cause"), "reason": cause.get("reason"), "requester": cause.get("requester"),
            "previous_session_id": previous[0], "session_id": key[0], "generation": key[1],
            "terminal": self._terminal_summary(), "instruction": RESTARTED_HINT})

    def _manager_new_session(self, previous: tuple[str, int], key: tuple[str, int]) -> None:
        requeued = self._call(self._ports.requeue, default=0)
        unknown = [entry for entry in self._reports()
                   if entry.get("state") == "unknown" and not entry.get("submitted")]
        summary = self._call(self._ports.task_summary)
        task = None
        if isinstance(summary, Mapping):
            task = {name: summary.get(name) for name in ("task_id", "kind", "status", "summary", "held_reason",
                                                         "closed_reason", "active")}
        worker = self._call(self._ports.worker_state)
        self._enqueue(ActorRole.MANAGER, "manager_recovery", {
            "task_id": None if task is None else task.get("task_id"), "task": task,
            "worker": dict(worker) if isinstance(worker, Mapping) else None, "terminal": self._terminal_summary(),
            "previous_session_id": previous[0], "session_id": key[0], "generation": key[1],
            "reports_resent": requeued if isinstance(requeued, int) else 0, "reports_unknown": len(unknown),
            "unknown_reports": [{"task_id": entry.get("task_id"), "message_id": entry.get("message_id"),
                                 "report_kind": entry.get("report_kind")} for entry in unknown[:STATUS_REPORTS_MAX]],
            "instruction": RECOVERY_HINT}, first=True)
        listed = {entry.get("handoff_id") for entry in unknown[:STATUS_REPORTS_MAX]
                  if isinstance(entry.get("handoff_id"), str)}
        for entry in unknown:  # listed here: no separate unknown notice for them
            if isinstance(entry.get("handoff_id"), str):
                self._noticed_unknown.add(entry["handoff_id"])
        # test O1: an unknown notice still waiting (for the old session) is dropped for a report listed here
        dropped = [item for item in self._queue if item.notice.get("type") == "report_delivery_unknown"
                   and item.handoff_id in listed]
        for item in dropped:
            self._queue.remove(item)
            self._record({"type": "workbench_notice", "role": item.role.value, "notice_id": item.notice["notice_id"],
                          "notice_type": "report_delivery_unknown", "task_id": item.notice.get("task_id"),
                          "outcome": "dropped_listed_in_manager_recovery"})

    # report delivery (C-D70 (2)) ----------------------------------------------------------
    def _unknown_reports_step(self, now: float) -> None:
        for entry in self._reports():
            handoff_id = entry.get("handoff_id")
            if (entry.get("origin") != "worker" or entry.get("state") != "unknown" or entry.get("submitted")
                    or not isinstance(handoff_id, str) or handoff_id in self._noticed_unknown):
                continue
            self._noticed_unknown.add(handoff_id)
            self._enqueue(ActorRole.MANAGER, "report_delivery_unknown", {
                "task_id": entry.get("task_id"), "message_id": entry.get("message_id"),
                "report_kind": entry.get("report_kind"), "instruction": UNKNOWN_HINT}, handoff_id=handoff_id)

    # the worker watch (C-D70 (1)) -----------------------------------------------------------
    def _not_idle_reason(self, task: Mapping[str, Any] | None) -> str | None:
        if not isinstance(task, Mapping) or task.get("kind") != "work" or task.get("status") != "running" \
                or task.get("run_id") is None:
            return "no_open_task"
        peer = self._call(self._ports.peer, ActorRole.WORKER)
        current = None if peer is None else [getattr(peer, "session_id", None), getattr(peer, "generation", None)]
        session = task.get("worker_session")
        if current is None or session is None or list(session) != current:
            return "task_not_in_worker_session"
        if self._call(self._ports.paused, default=True) is not False:
            return "paused"
        terminal = self._terminal()
        if terminal.get("running") is not False or terminal.get("notice_pending"):
            return "terminal_command"
        if self._call(self._ports.outbox_busy, ActorRole.WORKER, default=True) is not False:
            return "message_pending"
        return None

    def _worker_step(self, now: float) -> None:
        task = self._call(self._ports.task)
        task_id = task.get("task_id") if isinstance(task, Mapping) else None
        with self._lock:
            if task_id is None:
                self._watch = None
            elif self._watch is None or self._watch.task_id != task_id:
                self._watch = _TaskWatch(task_id)
        cursor = self._call(self._ports.turns, self._cursor, default=None)
        turned = False
        if isinstance(cursor, tuple) and len(cursor) == 2:
            self._cursor, turned = cursor[0], bool(cursor[1])
        if task_id is None:
            return
        reason = self._not_idle_reason(task) or ("worker_turn" if turned else None)
        with self._lock:
            watch = self._watch
            if watch is None:
                return
            if reason is not None:
                watch.idle_since, watch.pending = None, None
                return
            probe_due = watch.last_probe is None or now - watch.last_probe >= self.probe_interval
        if probe_due:
            state = self._call(self._ports.probe, ActorRole.WORKER)
            idle = (isinstance(state, Mapping) and state.get("idle") is True and state.get("pending") is False
                    and state.get("approvalPending") is False and state.get("inFlightToolCount") == 0)
            with self._lock:
                if self._watch is not watch:
                    return
                watch.last_probe = now
                if not idle:
                    watch.idle_since, watch.pending = None, None
                    return
                if watch.idle_since is None:
                    watch.idle_since = now
        with self._lock:
            if self._watch is not watch or watch.idle_since is None or now - watch.idle_since < self.idle_limit:
                return
            if watch.checks >= self.max_checks:
                if not watch.stalled:
                    watch.stalled = True
                    idle_for = round(now - watch.idle_since)
                    self._enqueue(ActorRole.MANAGER, "worker_stalled", {
                        "task_id": task_id, "idle_seconds": idle_for, "checks_sent": watch.checks,
                        "terminal": self._terminal_summary(), "instruction": STALLED_HINT.format(checks=watch.checks)})
                return
            if watch.pending is None:
                watch.pending, watch.retry_at, watch.tries = {"notice_id": str(uuid4()), "type": "status_check"}, now, 0
            if now < watch.retry_at:
                return
            idle_for = round(now - watch.idle_since)
            notice = {**watch.pending, "task_id": task_id, "idle_seconds": idle_for, "check": watch.checks + 1,
                      "max_checks": self.max_checks, "terminal": self._terminal_summary(),
                      "instruction": STATUS_CHECK_INSTRUCTION.format(idle=idle_for)}
            retry = watch.tries > 0
            watch.tries += 1
        outcome = self._send(ActorRole.WORKER, notice, retry=retry)
        with self._lock:
            if self._watch is not watch or watch.pending is None or watch.pending["notice_id"] != notice["notice_id"]:
                return  # the worker acted or the Task changed meanwhile
            if outcome in ("deferred", "paused", "not_connected"):
                watch.retry_at = now + self.notice_retry
                return
            watch.pending = None
            watch.checks += 1
            watch.idle_since = now  # the next step counts from this check

    # sending -----------------------------------------------------------------------------
    def _send(self, role: ActorRole, notice: Mapping[str, Any], *, retry: bool = False) -> str:
        """One attempt; a deferral is journaled only on the first attempt of a notice."""
        notify = self._ports.notify
        if notify is None:
            return "rejected"
        try:
            outcome = notify(role, dict(notice))
        except Exception:
            outcome = "unknown"  # it may have reached the OMP: never resent
        if outcome not in ("delivered", "deferred", "paused", "not_connected", "unknown", "rejected"):
            outcome = "unknown"
        if not (retry and outcome in ("deferred", "paused", "not_connected")):
            self._record({"type": "workbench_notice", "role": role.value, "notice_id": notice.get("notice_id"),
                          "notice_type": notice.get("type"), "task_id": notice.get("task_id"),
                          "outcome": {"delivered": "sent"}.get(outcome, outcome)})
        return outcome

    def _send_queue(self, now: float) -> None:
        while self._queue:
            item = self._queue[0]
            if now < item.retry_at:
                return
            if item.build is not None:
                try:
                    fields = item.build()
                except Exception:
                    fields = item.notice  # the state is not readable now: the content queued is kept
                if fields is None:  # no longer applies (e.g. its Task closed meanwhile): never sent
                    self._queue.pop(0)
                    self._record({"type": "workbench_notice", "role": item.role.value,
                                  "notice_id": item.notice.get("notice_id"), "notice_type": item.notice.get("type"),
                                  "task_id": item.notice.get("task_id"), "outcome": "dropped_not_applicable"})
                    continue
                item.notice = {**dict(fields), "notice_id": item.notice["notice_id"], "type": item.notice["type"]}
            outcome = self._send(item.role, item.notice, retry=item.attempts > 0)
            item.attempts += 1
            if outcome in ("deferred", "paused", "not_connected"):
                item.retry_at = now + self.notice_retry
                return  # notices to one OMP keep their order
            self._queue.pop(0)

    def enqueue_notice(self, role: ActorRole, notice_type: str, fields: Mapping[str, Any], *,
                       build: Callable[[], Mapping[str, Any] | None] | None = None) -> str:
        """A backend notice sent once through this queue (C-D71 (2): ``backend_restarted``); like the others it
        waits while the target OMP is paused, held or not connected and is never sent twice. Returns its id.

        ``build`` (VM F1) makes the fields again right before each attempt; None drops the notice unsent."""
        with self._tick_lock:
            return self._enqueue(role, notice_type, fields, first=True, build=build)

    def queued(self) -> list[dict[str, Any]]:
        """The notices waiting for a deliverable OMP (tests and workbench_status)."""
        return [{"role": item.role.value, **item.notice} for item in self._queue]


__all__ = ["BACKEND_RESTARTED_HINT", "STOP_SURVIVOR_TOOL", "validate_stop_survivor_arguments", "EDITOR_WAIT", "IDLE_LIMIT", "MANAGER_NOTICE_TYPES", "MANAGER_TOOLS", "MAX_CHECKS", "PROBE_INTERVAL",
           "RESTART_DETAIL", "RESTART_NO_TASK_DETAIL", "RESTART_NO_TASK_PENDING_DETAIL", "RESTART_PENDING_DETAIL", "restart_detail", "RESTART_WORKER_TOOL", "STATUS_TOOL", "WORKER_NOTICE_TYPES", "WatchPorts", "Watchdog",
           "bounded", "validate_restart_arguments", "validate_status_arguments"]
