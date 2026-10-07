"""CW-18 Task flow under the user's standing delegation (C-D60, C-D64, C-D65, C-D66).

``TaskFlow`` is the backend's ``HandoffPolicy`` for ``to_worker``/``to_manager``
and the owner of the single active Task:

- Standing delegation (C-D66): the user delegates handing work to the worker to
  the manager. A ``to_worker`` without ``task_id`` while the worker is idle
  creates the Task (``experiment`` or ``work``), records ``approve_scope`` +
  ``proceed`` with ``actor: user_standing_delegation`` and ``authority: C-D66``
  (no UI approval, no derived scope check) and dispatches at once: a free-work
  instruction goes to the worker, an experiment run starts on the product host
  shell under the idle-only rule (else the Task shows
  ``held:host_terminal_busy`` and the start is tried again). The user's host
  input is held only from the moment Workbench types (R5): the TASK delivery,
  the worker's decision and the worktree preparation run unheld; just before
  the first keystroke the shell is checked idle again and held (if the user
  started a line meanwhile nothing is typed and the start fails as
  ``start_failed:host_terminal_busy``).
- Paths (CW-18 smoke D2): Task paths are repo-relative. With ``project_dir`` an
  absolute path inside the project becomes relative to it; the project root, a
  path outside it or any other invalid path is ``rejected:invalid_paths`` with
  ``errors`` naming ``spec.paths[i]`` and the expected form.
- One Task at a time: while a Task is active (dispatched, starting, running,
  held, waiting for the worker's report, cancelling) a new ``to_worker``
  answers ``worker_busy`` with ``{task_id, kind, summary, status, since}``.
  Nothing is queued.
- Follow-ups: a ``to_worker`` with the active Task's ``task_id`` is a message to
  the worker for the same Task (no new approval). An experiment re-run
  (``run: true`` or a changed spec) of the current Task is a new run under the
  same delegation, limited to ``RETRY_LIMIT`` (CW-13) re-runs per Task.
- Completion frees the worker (CW-18 R1): free work ends when the worker's
  ``done`` (closes the Task) or ``blocked`` (the Task stays open for a
  follow-up) report is accepted into the manager OMP's session (the mailbox's
  ``api_accepted``), not when the manager's turn ends: a ``to_worker`` in the
  turn that reads the report is accepted. An experiment Task is ``finished``
  once its report was accepted the same way (its run closes then; it can still
  be re-run until a new Task supersedes it). A ``to_worker`` that races the
  report's ack waits for it at most ``REPORT_SETTLE_WAIT``. A report whose
  delivery ended unknown or rejected before any acceptance closes the Task as
  ``report_outcome_unknown`` / ``report_not_delivered`` (nothing is resent) and
  the manager learns it from the notices of its next ``to_worker`` result. A
  done/blocked report and a free-work TASK are kept across a pause (never
  submitted, so delivering them after the resume is not a replay; R3).
- Judgment unavailable (smoke-04 G2/G3): when the worker's staged analysis was rejected, its outcome is
  unknown, or the worker stays busy past ``ANALYSIS_IDLE_LIMIT`` after the exit, the run closes as
  ``indeterminate`` (never success) with the machine reason, the Task is ``finished`` (the worker is free), the
  automation for the run ends and the manager gets one Workbench notice; nothing is resent.
- Attribution (R2): a ``to_manager`` without ``task_id`` belongs to the active
  Task only once that Task's TASK reached the worker; before that it is
  ``rejected:task_not_delivered``.
- Cancel (``to_worker {task_id, cancel: true}``): every not yet submitted
  message of the Task is withdrawn from the outbox first (R2: the worker never
  gets a cancelled TASK after its cancel; a TASK it never got needs no cancel
  notice). An unstarted Task closes at once; a free-work run is cancelled in
  the repository and the worker is told;
  a host command in flight is never killed by Workbench (no CW-13 forced stop):
  the Task is ``cancelling`` until the command exits, then the run is cancelled
  without a judgment and the Task closes. The manager learns the end from a
  notice in its next ``to_worker`` result.
- Automation (CW-18 U3): an optional ``lifecycle`` hook (the backend's
  ``AutomationController``) learns each run's start (``experiment_started`` with
  the live ``WorkflowRun`` on the runner thread, ``work_started`` once the
  free-work TASK message exists) and its close (``run_ended``). It is called
  outside the flow lock; its failure never stops the flow.
- Host shell owner (C-D68): an experiment run holds the shared ``HostGate``
  for its whole run (and a pending directory restore for its ``cd``); the
  worker's ``terminal`` command holds it until its shell was given back, so
  the two never start into each other (``held:host_terminal_busy``, or
  ``held:host_terminal_busy:worker_terminal_command`` while the worker's own
  command holds it). Every
  accepted ``to_worker`` result (``dispatched``, ``queued``) carries
  ``manager_rule``: the worker does the Task, the manager waits for its report.
- New worker session (C-D70 (4)): each work Task remembers the worker OMP
  session that accepted its TASK (``worker_session``). A follow-up
  ``to_worker`` on that Task while the worker is another session that never got
  it carries the full TASK again (message, goal, paths, analysis level, the
  commands with the run-as-given rule), the commands already run from the
  terminal journal (command, exit, duration, log path) and then the follow-up
  text; nothing else is resent. The Task's command list keeps being enforced.
- Persistence: ``workflow/tasks-flow.jsonl`` (0600, fsync per record) keeps the
  Task state and every decision with its ids. After a backend restart nothing
  is started or resent: an unstarted Task is closed, a Task with a current run
  is held (``backend_restarted``) until its report or a cancel. Pending UI
  approvals from before C-D66 are retired once (never dispatched).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterator, Mapping
from contextlib import contextmanager
from uuid import uuid4

from workbench.backend.flow import (
    ANALYSIS_RULES, DEFAULT_ANALYSIS, ActiveTask, HandoffDecision, HandoffJournal, HandoffRequest, OutboundMessage, accepts_keyword, held, rejected,
    experiment_command_error,
)
from workbench.backend.flow_terminal import RETURN_WAIT, HostGate
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.workflow.run import WorkerJudgmentUnavailable, WorkflowHeld, classify_worker_request
from workbench.workflow.worker_port import WorkerResponseRejected

FLOW_LEDGER_NAME = "tasks-flow.jsonl"  # under DataLayout.workflow
WORKER_TERMINAL_HELD = "host_terminal_busy:worker_terminal_command"
RETRY_LIMIT = 3  # CW-13: re-runs of one experiment Task
SUMMARY_MAX = 1024
NOTICE_LIMIT = 16
HOLD_REASON = "Workbench is starting the manager's experiment run"
RETURN_REASON = "Workbench is returning the host shell to your directory"
# F2: how long a give-back waits for the shell to be idle before it retries the cd later.
RESTORE_IDLE_WAIT = 5.0
STANDING_DELEGATION = {"actor": "user_standing_delegation", "authority": "C-D66"}
# Task statuses (ui_v1 TASK_STATUSES). Inactive ones free the worker.
ACTIVE_STATUSES = ("dispatched", "starting", "running", "waiting_report", "held", "cancelling")
INACTIVE_STATUSES = ("finished", "blocked", "closed")
BUSY_DETAIL = ("The worker does one task at a time and is busy with this Task. Wait for its to_manager "
               "report (done/blocked), send a follow-up with this task_id, or cancel it with "
               "{task_id, cancel: true}.")
UNKNOWN_TASK_DETAIL = ("task_id: no such Task. For a follow-up, cancel or re-run use the task_id from your "
                       "dispatched result; for a new Task send task_id null.")
TASK_NOT_DELIVERED_DETAIL = ("The active Task's instruction has not reached you yet; a report without task_id "
                             "cannot belong to it. Finish the turn; the Task's TASK message follows.")
# R1: how long a to_worker (to_manager) waits for a report (TASK) the lane is submitting right now.
REPORT_SETTLE_WAIT = 2.0
# smoke-04 G2: after the host command exited, the staged analysis waits for an idle worker at most this long
# (a periodic review turn whose outcome is unknown must not keep the Task waiting forever; a pause does not count).
ANALYSIS_IDLE_LIMIT = 180.0
# C-D68 (3): every accepted to_worker result says who does the work.
def _analysis_fields(level: object) -> dict[str, str]:
    """The TASK message lines for the analysis level (C-D69 (2)); null is ``summary``."""
    level = level if level in ANALYSIS_RULES else DEFAULT_ANALYSIS
    return {"analysis": level, "analysis_rule": ANALYSIS_RULES[level]}


COMMANDS_RULE = ("Run these commands exactly as given with the terminal tool, one per call, in order (number 1 first); "
                 "use an alternative entry only as the message says. Do not write, change, split or combine "
                 "commands: while this Task is active Workbench refuses any other command. Then report a short "
                 "summary of the results (key values, failures); Workbench attaches the list of commands run, so "
                 "do not paste commands or scripts back.")
COMMANDS_RUN_NOTE = ("Recorded by Workbench from the terminal journal: the commands the worker ran for this Task "
                     "(command shortened for display, exit code or signal, duration, full output in log_path).")
# C-D70 (4): what a new worker session reads first in a re-sent Task.
RESEND_NOTE = ("The Task is re-sent in full because the current worker session may not have it. Below is the full Task "
               "as first sent, then commands_already_run (the commands already run for this Task, from the "
               "Workbench terminal journal), then the manager's follow_up. Do not run a command listed in "
               "commands_already_run again unless the follow-up asks for it; continue as the follow-up says and "
               "report with to_manager.")


def _commands_fields(commands: list[str] | None) -> dict[str, Any]:
    """C-D69 (6): the TASK/follow-up lines of a Task's exact commands (numbered, with the rule)."""
    if not commands:
        return {}
    return {"commands": [{"number": index, "command": command} for index, command in enumerate(commands, 1)],
            "commands_rule": COMMANDS_RULE}


MANAGER_RULE = ("The worker does this Task; do not do it yourself (no commands, edits or checks for it). End "
                "your turn and wait for the worker's to_manager report.")
CANCEL_WAIT_DETAIL = ("The experiment's host command is still running; Workbench never kills it. The Task "
                      "closes as cancelled when the command exits (the user can stop it in the host shell); "
                      "no judgment is made.")


def _under(path: str, root: Path | str) -> bool:
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([os.path.realpath(path), root]) == root
    except ValueError:
        return False


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _valid_paths(paths: list[str]) -> bool:
    if not paths:
        return True
    try:
        classify_worker_request({"paths": list(paths)}, None)
    except ValueError:
        return False
    return True


RELATIVE_RULE = ("use a repo-relative path (relative to the project root, without '.', '..', empty segments or "
                 "backslashes), such as src/app/ or work/hello.txt")


def repo_relative_paths(paths: list[str], project_dir: str | Path | None,
                        field_name: str = "spec.paths") -> tuple[list[str], list[str]]:
    """(normalised paths, errors) for Task paths (D2).

    A leading ``./`` is dropped; an absolute path inside ``project_dir`` becomes relative to it (a trailing
    ``/`` is kept); the project root itself, a path outside it, or any other invalid path is an error naming
    ``field_name[index]`` and the expected form (never the value).
    """
    roots: list[str] = []
    if project_dir is not None:
        for root in (os.path.normpath(os.path.abspath(project_dir)), os.path.realpath(project_dir)):
            if root not in roots:
                roots.append(root)
    result: list[str] = []
    errors: list[str] = []
    for index, path in enumerate(paths):
        where = f"{field_name}[{index}]"
        directory = path.endswith("/")
        if path.startswith("/"):
            if not roots:
                errors.append(f"{where}: absolute paths are not accepted; {RELATIVE_RULE}")
                continue
            relative = None
            for candidate in dict.fromkeys((os.path.normpath(path), os.path.realpath(path))):
                for root in roots:
                    if candidate == root:
                        relative = ""
                        break
                    if os.path.commonpath([candidate, root]) == root:
                        relative = os.path.relpath(candidate, root)
                        break
                if relative is not None:
                    break
            if relative is None:
                errors.append(f"{where}: absolute path outside the project directory; {RELATIVE_RULE}")
                continue
            if relative == "":
                errors.append(f"{where}: is the project root itself; list the repo-relative files or directories "
                              "the worker may change (e.g. src/)")
                continue
            path = relative + ("/" if directory else "")
        while path.startswith("./"):
            path = path[2:]
        if not _valid_paths([path]):
            errors.append(f"{where}: invalid path; {RELATIVE_RULE}")
            continue
        result.append(path)
    return result, errors


# -- state ------------------------------------------------------------------------
class _HandoffWatch:
    """Notes the moment an experiment start types wb-handoff into the host shell port (the same port object is
    handed to the workflow; only its ``send_user`` is wrapped until ``close``)."""

    def __init__(self, port: Any) -> None:
        self._port = port
        self.handoff_typed = False
        self._original = getattr(port, "send_user", None)
        self._own = "send_user" in getattr(port, "__dict__", {})  # an instance attribute is put back on close
        self._installed = False
        if callable(self._original):
            try:
                port.send_user = self._send
                self._installed = True
            except (AttributeError, TypeError):
                pass

    def _send(self, data: bytes) -> Any:
        if bytes(data).strip() == b"wb-handoff":
            self.handoff_typed = True
        return self._original(data)

    def close(self) -> None:
        if self._installed:
            self._installed = False
            try:
                if self._own:
                    self._port.send_user = self._original
                else:
                    del self._port.send_user
            except AttributeError:
                pass


@dataclass
class FlowTask:
    task_id: str
    kind: str
    status: str = "dispatched"  # see ACTIVE_STATUSES / INACTIVE_STATUSES
    revision: int = 1
    spec: dict[str, Any] = field(default_factory=dict)  # the spec of ``revision`` (delegated)
    summary: str = ""  # the first SUMMARY_MAX chars of ``message``, for status views only
    message: str = ""  # the manager's full to_worker message (<= MESSAGE_MAX); the first TASK carries it
    commands: list[str] | None = None  # C-D69 (6): the exact commands of a work Task (terminal accepts only these)
    since: str = ""
    scope_decision_id: str | None = None
    run_id: str | None = None
    run_revision: int | None = None
    task_message_id: str | None = None
    runs_started: int = 0  # start attempts (each consumed one proceed)
    pending_start: dict[str, Any] | None = None  # {revision, origin}
    held_reason: str | None = None
    closed_reason: str | None = None
    last_result: dict[str, Any] | None = None
    cancel_requested: dict[str, Any] | None = None
    worker_session: list[Any] | None = None  # C-D70 (4): [session_id, generation] of the worker that got the TASK

    def set_status(self, status: str) -> None:
        if status != self.status or not self.since:
            self.status, self.since = status, _now()

    def busy(self) -> bool:
        """True while this Task occupies the worker (C-D66: one Task at a time)."""
        return self.status not in INACTIVE_STATUSES

    def shown_status(self) -> str:
        if self.status in ("dispatched", "starting", "running", "held") and self.held_reason:
            return f"held:{self.held_reason}"
        return self.status

    def busy_view(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "kind": self.kind, "summary": self.summary,
                "status": self.shown_status(), "since": self.since, "run_id": self.run_id,
                "held_reason": self.held_reason}

    def view(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "kind": self.kind, "status": self.status, "summary": self.summary,
                "since": self.since, "active": self.busy(), "revision": self.revision, "run_id": self.run_id,
                "runs_started": self.runs_started, "retry_limit": RETRY_LIMIT, "held_reason": self.held_reason,
                "closed_reason": self.closed_reason, "cancel_requested": self.cancel_requested is not None,
                "last_result": None if self.last_result is None else dict(self.last_result)}


_TASK_FIELDS = frozenset(item.name for item in fields(FlowTask))


@dataclass
class ExperimentPorts:
    """What the runner needs for an experiment run (the backend supplies product parts)."""

    host_shell: Callable[[], Any]  # -> HostShellPort | None
    make_workflow: Callable[[Any], Any]  # (repository on the runner thread) -> TaskWorkflow
    automation: Callable[[], Mapping[str, Any]]  # AutomationState port
    environment_names: Callable[[], set[str]]  # names in the host shell's start-up environment
    worktrees_root: Path
    artifacts_root: Path


class TaskFlow:
    """The CW-18 policy and Task runner under the standing delegation; see the module docstring."""

    def __init__(self, ledger_path: str | Path, *, repository_factory: Callable[[], Any],
                 handoffs: Any, omp_idle: Callable[[ActorRole], bool | None],
                 paused: Callable[[], bool] = lambda: False,
                 experiment: ExperimentPorts | None = None, lifecycle: Any | None = None,
                 poll_interval: float = 0.5, collect_slice: float = 1.0,
                 project_dir: str | Path | None = None, analysis_idle_limit: float = ANALYSIS_IDLE_LIMIT,
                 host_gate: HostGate | None = None,
                 worker_session: Callable[[], tuple[str, int] | None] = lambda: None):
        self._repository_factory = repository_factory
        # C-D70 (4): the worker OMP's current bridge session (session_id, generation), None when not connected.
        self._worker_session = worker_session
        # C-D68: one Workbench owner of the host shell (an experiment run or the worker's terminal command).
        self._host_gate = host_gate or HostGate()
        self._analysis_idle_limit = analysis_idle_limit
        self._project_dir = project_dir  # D2: absolute Task paths inside it become repo-relative
        self._handoffs = handoffs
        self._omp_idle = omp_idle
        self._paused = paused
        self._experiment = experiment
        self._lifecycle = lifecycle
        self._poll_interval = poll_interval
        self._collect_slice = collect_slice
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._stop = False
        self._thread: threading.Thread | None = None
        self._following: str | None = None  # the Task whose experiment run the runner thread is driving
        # F2: the user's host shell directory before Workbench typed a run's ``cd`` (restored after the run).
        self._home_cwd: str | None = None
        # C-D69 (6): the terminal service's record of the commands run for a Task (set by the backend)
        self.terminal_runs: Callable[[str], list[dict[str, Any]]] | None = None
        # C-D70 (Q2, p27-cd70-02): told when a manager follow-up of a Task was submitted to the worker (the watchdog
        # starts its status checks and the worker_stalled notice over: a new instruction is a new chance)
        self.follow_up_submitted: Callable[[str], None] | None = None
        # R1/R2 (in memory): Tasks whose report the lane/runner is submitting right now, and each free-work
        # Task's TASK message state (queued | delivering | sent | not_sent).
        self._report_inflight: set[str] = set()
        self._task_message_state: dict[str, str] = {}
        # p27-cd70-fix-01: a full re-send queued or being delivered, per Task: the worker session it goes to
        self._resend_pending: dict[str, list[Any]] = {}
        self._ledger = HandoffJournal(ledger_path)
        self.tasks: dict[str, FlowTask] = {}
        self.notices: list[dict[str, Any]] = []
        self._load(self._ledger.records)
        self._ledger.records = []

    # -- persistence ------------------------------------------------------------
    def _load(self, records: list[dict[str, Any]]) -> None:
        pending_approvals: dict[str, Any] = {}
        retired: set[str] = set()
        for record in records:
            kind = record.get("type")
            if kind == "task" and isinstance(record.get("task"), dict):
                data = {name: value for name, value in record["task"].items() if name in _TASK_FIELDS}
                try:
                    self.tasks[data["task_id"]] = FlowTask(**data)
                except (KeyError, TypeError):
                    continue
            elif kind == "approval" and isinstance(record.get("approval"), dict):  # before C-D66
                approval = record["approval"]
                approval_id = str(approval.get("approval_id"))
                if approval.get("state") == "pending":
                    pending_approvals[approval_id] = approval
                else:
                    pending_approvals.pop(approval_id, None)
            elif kind == "approvals_retired":
                retired.update(str(item) for item in record.get("approval_ids") or ())
            elif kind == "notice" and isinstance(record.get("notice"), dict):
                self.notices.append(dict(record["notice"]))
            elif kind == "notices_delivered":
                done = set(record.get("notice_ids") or ())
                self.notices = [n for n in self.notices if n.get("notice_id") not in done]
        # C-D66: pending UI approvals are retired, never turned into a dispatch.
        stale = sorted(set(pending_approvals) - retired)
        if stale:
            self._record({"type": "approvals_retired", "approval_ids": stale, "authority": "C-D66"})
        # A restart starts nothing and resends nothing: the manager acts again.
        for task in list(self.tasks.values()):
            if task.status == "closed":
                continue
            if task.status == "pending_approval":  # a Task of the old flow that was never approved
                self._close(task, "approval_retired_c_d66")
            elif task.run_id is not None:  # its report or a cancel ends it; nothing is resent
                task.pending_start = None
                task.held_reason = "backend_restarted"
                if task.kind == "experiment":
                    task.set_status("held")  # no runner follows the run any more
                self._save_task(task)
            elif task.kind == "experiment" and task.runs_started > 0:
                if task.pending_start is not None or task.status != "finished":
                    task.pending_start = None
                    task.held_reason = "backend_restarted" if task.status != "finished" else task.held_reason
                    task.set_status("finished")  # a re-run needs the manager's run: true again
                    self._save_task(task)
            elif task.status != "blocked":
                self._close(task, "not_started_before_restart")

    def _notify_lifecycle(self, name: str, *args: Any) -> None:
        """Tell the automation controller about a run start/close (never under the flow lock)."""
        hook = getattr(self._lifecycle, name, None)
        if hook is None:
            return
        try:
            hook(*args)
        except Exception:
            pass  # automation for the run stays held; the flow itself goes on

    def _notify_lifecycle_later(self, name: str, *args: Any) -> None:
        """The lifecycle hook from a mailbox callback (never under the mailbox's or the flow's lock)."""
        threading.Thread(target=self._notify_lifecycle, args=(name, *args), daemon=True).start()

    def _settle(self, waiting: Callable[[], bool]) -> None:
        """Wait (flow lock released) at most ``REPORT_SETTLE_WAIT`` while ``waiting()`` (under the flow lock)."""
        deadline = time.monotonic() + REPORT_SETTLE_WAIT
        while not self._stop and waiting():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._wake.wait(remaining)

    def _record(self, record: Mapping[str, Any]) -> None:
        try:
            self._ledger.append(record)
        except OSError:
            if not self._stop:  # after close() a late runner record is dropped, never raised
                raise

    def _save_task(self, task: FlowTask) -> None:
        self._record({"type": "task", "task": asdict(task)})

    @contextmanager
    def _repository(self) -> Iterator[Any]:
        repository = self._repository_factory()
        try:
            yield repository
        finally:
            repository.close()

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._stop:
                return
            self._thread = threading.Thread(target=self._runner, name="task-flow-runner", daemon=True)
            self._thread.start()

    def close(self, timeout: float = 10.0) -> None:
        with self._wake:
            self._stop = True
            self._wake.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._ledger.close()

    # -- views (ui_v1 state and the HandoffService hook) ---------------------------
    def active(self) -> FlowTask | None:
        """The current Task: the latest one not closed (it may be inactive: finished or blocked)."""
        with self._lock:
            live = [task for task in self.tasks.values() if task.status != "closed"]
            return live[-1] if live else None

    def active_task(self) -> ActiveTask | None:
        task = self.active()
        if task is None:
            return None
        return ActiveTask(task.task_id, task.run_revision or task.revision, task.kind, True, task.run_id,
                          task.task_message_id)

    def active_commands(self) -> list[str] | None:
        """C-D69 (6): the exact commands the worker's terminal accepts now: those of the active (busy) work Task
        that has them; None means no restriction (no Task, or a Task without commands)."""
        with self._lock:
            task = self.active()
            if task is None or not task.busy() or task.kind != "work" or not task.commands:
                return None
            return list(task.commands)

    def experiment_host_activity(self) -> str | None:
        """C-D68: why an experiment needs the host shell now (a run starting or running, a directory restore
        pending), else None. Called by the terminal service under the HostGate lock; takes only the flow lock."""
        with self._lock:
            if self._following is not None:
                return "an experiment run uses the host terminal"
            if any(task.kind == "experiment" and task.status != "closed" and task.pending_start
                   for task in self.tasks.values()):
                return "an experiment run is starting"
            if self._home_cwd is not None:
                return "the host shell directory is being restored after an experiment run"
        return None

    def task_view(self) -> dict[str, Any] | None:
        """ui_v1 ``task``: the current Task, else the most recent one (closed), else None."""
        with self._lock:
            task = self.active() or (list(self.tasks.values())[-1] if self.tasks else None)
            return None if task is None else task.view()

    def watch_view(self) -> dict[str, Any] | None:
        """C-D70 (1): the open (busy) Task as the watchdog sees it, else None."""
        with self._lock:
            task = self.active()
            if task is None or not task.busy():
                return None
            return {"task_id": task.task_id, "kind": task.kind, "status": task.status, "run_id": task.run_id,
                    "held_reason": task.held_reason, "summary": task.summary,
                    "message_state": self._task_message_state.get(task.task_id),
                    "worker_session": None if task.worker_session is None else list(task.worker_session)}

    def task_session(self, task_id: str) -> list[Any] | None:
        """The worker session ``[session_id, generation]`` that has this Task's TASK (C-D70 (4)), or None."""
        with self._lock:
            task = self.tasks.get(task_id)
            return None if task is None or task.worker_session is None else list(task.worker_session)

    def status_view(self, task_id: str | None = None) -> dict[str, Any] | None:
        """C-D70 (6): a Task for the manager's ``workbench_status`` (the current, else the last one), with its full
        message, analysis level and commands; None when there is no such Task."""
        with self._lock:
            if task_id is None:
                task = self.active() or (list(self.tasks.values())[-1] if self.tasks else None)
            else:
                task = self.tasks.get(task_id)
            if task is None:
                return None
            spec = task.spec or {}
            view = task.view()
            view.update({"message": task.message or task.summary, "goal": spec.get("goal"),
                         "paths": list(spec.get("paths") or []), "analysis": spec.get("analysis") or (
                             DEFAULT_ANALYSIS if task.kind == "work" else None),
                         "commands": None if task.commands is None else list(task.commands),
                         "task_message_id": task.task_message_id,
                         "worker_session": None if task.worker_session is None else list(task.worker_session)})
            return view

    def worker_view(self) -> dict[str, Any]:
        """ui_v1 ``worker``: busy while a Task occupies it (C-D66)."""
        with self._lock:
            task = self.active()
            busy = task is not None and task.busy()
            return {"state": "busy" if busy else "idle", "task_id": task.task_id if busy else None}

    # -- the HandoffPolicy -----------------------------------------------------------
    def decide(self, request: HandoffRequest, _active: ActiveTask | None) -> HandoffDecision:
        # the host shell is asked before the flow lock (its port takes the pane lock)
        size_error = self._host_shell_size_error(request.args) if request.tool == "to_worker" else None
        with self._lock:
            if request.tool == "to_worker":
                decision = (HandoffDecision(rejected("invalid_arguments", errors=[size_error]))
                            if size_error is not None else self._to_worker(request))
                notices = self._take_notices()
                if notices:
                    decision = replace(decision, result={**decision.result, "notices": notices})
                return decision
            return self._to_manager(request)

    def _host_shell_size_error(self, args: Mapping[str, Any]) -> str | None:
        """C-D69 (5)(b) (stuck-review-01 P3-1): an experiment command checked with the current host shell's path
        at Task creation, so creation never passes what the start refuses on this shell."""
        spec = args.get("spec") if args.get("kind") == "experiment" else None
        execution = spec.get("execution") if isinstance(spec, Mapping) else None
        if not isinstance(execution, Mapping) or self._experiment is None or args.get("cancel") is True:
            return None
        return experiment_command_error(execution.get("command"), self._host_shell_path())

    def _host_shell_path(self) -> str | None:
        port = None
        try:
            port = self._experiment.host_shell()
            executable = getattr(getattr(port, "choice", None), "executable", None)
            return executable if isinstance(executable, str) and executable else None
        except Exception:
            return None
        finally:
            if port is not None:
                try:
                    port.detach()
                except Exception:
                    pass

    def _take_notices(self) -> list[dict[str, Any]]:
        if not self.notices:
            return []
        taken, self.notices = self.notices[:NOTICE_LIMIT], self.notices[NOTICE_LIMIT:]
        self._record({"type": "notices_delivered", "notice_ids": [n["notice_id"] for n in taken]})
        return taken

    def _notice(self, **values: Any) -> None:
        notice = {"notice_id": str(uuid4()), "at": _now(), **values}
        self.notices.append(notice)
        self._record({"type": "notice", "notice": notice})

    def _spec_document(self, args: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """(the Task spec document, path errors); paths are repo-relative (D2)."""
        spec = dict(args["spec"])
        paths, errors = repo_relative_paths(list(spec["paths"]), self._project_dir)
        document: dict[str, Any] = {"kind": args["kind"], "goal": spec["goal"], "paths": paths}
        if "instructions" in spec:
            document["instructions"] = spec["instructions"]
        if "execution" in spec:
            document["execution"] = dict(spec["execution"])
        if args.get("analysis") is not None:  # C-D69 (2): kept with the Task for later follow-ups
            document["analysis"] = args["analysis"]
        if args.get("commands"):  # C-D69 (6): part of the delegated spec/scope
            document["commands"] = list(args["commands"])
        document["source"] = "manager_to_worker"
        return document, errors

    @staticmethod
    def _invalid_paths(errors: list[str]) -> HandoffDecision:
        return HandoffDecision(rejected("invalid_paths", errors=errors,
                                        detail="nothing was dispatched; fix spec.paths and send again"))

    @staticmethod
    def _worker_busy(task: FlowTask, **extra: Any) -> HandoffDecision:
        return HandoffDecision({"status": "worker_busy", "task_id": task.task_id, "task": task.busy_view(),
                                "detail": BUSY_DETAIL, **extra})

    @staticmethod
    def _standing(request: HandoffRequest) -> dict[str, Any]:
        return {**STANDING_DELEGATION, "tool_call_id": request.tool_call_id, "request_key": request.key_dict()}

    def _delegate(self, task: FlowTask, request: HandoffRequest, revision: int, spec: Mapping[str, Any]) -> None:
        """Record scope + proceed for ``revision`` under the standing delegation (C-D66) and schedule it."""
        scope: dict[str, Any] = {"kind": task.kind, "goal": spec["goal"], "paths": list(spec["paths"]),
                                 "commit_policy": "manager_local_commits", "retry_limit": RETRY_LIMIT,
                                 **self._standing(request)}
        for name in ("execution", "instructions", "analysis", "commands"):
            if name in spec:
                scope[name] = spec[name]
        with self._repository() as repository:
            scope_id = repository.approve_scope(task.task_id, revision, scope, retry_count_used=task.runs_started)
            proceed_id = repository.proceed(task.task_id, revision, {
                "source": "manager_to_worker", "instruction": request.args["message"][:SUMMARY_MAX],
                **self._standing(request)}, retry_count_used=task.runs_started)
        task.revision, task.spec, task.scope_decision_id = revision, dict(spec), scope_id
        self._record({"type": "delegated", "task_id": task.task_id, "revision": revision,
                      "scope_decision_id": scope_id, "proceed_decision_id": proceed_id,
                      **self._standing(request)})
        task.set_status("dispatched")
        self._schedule(task, revision, f"tool_call:{request.tool_call_id}")

    def _to_worker(self, request: HandoffRequest) -> HandoffDecision:
        args = request.args
        if args.get("cancel") is True:
            return self._cancel(request)
        # R1: a report the lane is submitting right now decides whether the worker is free (bounded wait).
        self._settle(lambda: (current := self.active()) is not None and current.busy()
                     and current.task_id in self._report_inflight)
        task_id = args.get("task_id")
        active = self.active()
        if task_id is None:
            if active is not None and active.busy():
                return self._worker_busy(active)
            if "spec" not in args:
                return HandoffDecision(rejected("spec_required", errors=[
                    "spec: a new Task (task_id null) needs spec {goal, paths, instructions, execution}; "
                    "for a follow-up give the active Task's task_id"]))
            spec, path_errors = self._spec_document(args)
            if path_errors:
                return self._invalid_paths(path_errors)
            if active is not None:  # a finished or blocked Task is ended by a new Task
                self._end_inactive(active, "superseded_by_new_task")
            with self._repository() as repository:
                new_id = repository.create_task(spec)
            task = FlowTask(new_id, args["kind"], spec=spec, summary=args["message"][:SUMMARY_MAX],
                            message=args["message"], commands=list(args["commands"]) if args.get("commands") else None)
            task.set_status("dispatched")
            self.tasks[new_id] = task
            self._record({"type": "task_created", "task_id": new_id, "request_key": request.key_dict()})
            self._delegate(task, request, 1, spec)
            return HandoffDecision({"status": "dispatched", "task_id": new_id, "kind": task.kind, "revision": 1,
                                    "detail": "dispatched to the worker under the user's standing delegation "
                                              "(C-D66); it reports back with to_manager",
                                    "manager_rule": MANAGER_RULE})
        task = self.tasks.get(task_id)
        if task is None:
            return HandoffDecision(rejected("unknown_task", detail=UNKNOWN_TASK_DETAIL))
        if task.status == "closed":
            return HandoffDecision(rejected("task_closed") | {"closed_reason": task.closed_reason})
        if active is not task:  # only the latest Task is open; kept for safety
            return self._worker_busy(active) if active is not None and active.busy() \
                else HandoffDecision(rejected("task_closed"))
        if args["kind"] != task.kind:
            return HandoffDecision(rejected("task_kind_mismatch") | {"task_kind": task.kind})
        spec = None
        if "spec" in args:
            spec, path_errors = self._spec_document(args)
            if path_errors:
                return self._invalid_paths(path_errors)
        if task.kind == "experiment":
            return self._experiment_follow_up(task, request, spec)
        return self._work_follow_up(task, request, spec)

    def _experiment_follow_up(self, task: FlowTask, request: HandoffRequest,
                              spec: dict[str, Any] | None) -> HandoffDecision:
        wants_run = request.args.get("run") is True or (spec is not None and spec != task.spec)
        if not wants_run:
            if task.run_id is None:
                return HandoffDecision(held("no_active_run") | {
                    "task_id": task.task_id, "detail": "no run is in progress; send run: true to re-run this "
                                                       "experiment or a new Task without task_id"})
            self._record({"type": "follow_up", "task_id": task.task_id, "run_id": task.run_id,
                          **self._standing(request)})
            return self._question(task, request, None)
        if task.busy():
            return self._worker_busy(task)
        if task.runs_started > RETRY_LIMIT:
            return HandoffDecision(held("retry_limit") | {"task_id": task.task_id, "retry_limit": RETRY_LIMIT,
                                                          "runs_started": task.runs_started})
        base = dict(spec) if spec is not None else dict(task.spec)
        if base == task.spec:
            revision = task.revision  # the same delegated spec runs again on its revision
        else:
            with self._repository() as repository:
                revision = repository.revise_task(task.task_id, base)
        self._delegate(task, request, revision, base)
        return HandoffDecision({"status": "dispatched", "task_id": task.task_id, "kind": task.kind,
                                "revision": revision, "retry": task.runs_started, "retry_limit": RETRY_LIMIT,
                                "manager_rule": MANAGER_RULE})

    def _work_follow_up(self, task: FlowTask, request: HandoffRequest,
                        spec: dict[str, Any] | None) -> HandoffDecision:
        if task.run_id is None:
            return HandoffDecision(held("run_starting") | {"task_id": task.task_id})
        self._record({"type": "follow_up", "task_id": task.task_id, "run_id": task.run_id,
                      **self._standing(request)})
        if task.status == "blocked":
            task.held_reason = None
            task.set_status("running")
            self._save_task(task)
        return self._question(task, request, spec)

    def _current_worker_session(self) -> list[Any] | None:
        try:
            current = self._worker_session()
        except Exception:
            return None
        return None if current is None else [current[0], current[1]]

    def _needs_resend(self, task: FlowTask) -> list[Any] | None:
        """C-D70 (4): the worker session to re-send the full Task to, or None (it has the Task, or it is unknown
        or the TASK itself is still on its way to it)."""
        if task.kind != "work":
            return None
        current = self._current_worker_session()
        if current is None or task.worker_session == current:
            return None
        if self._task_message_state.get(task.task_id) in ("queued", "delivering"):
            return None  # its TASK still goes to this session
        if self._resend_pending.get(task.task_id) == current:
            return None  # a full re-send to this session is on its way: a further follow-up is a plain one
        return current

    def _resend(self, task: FlowTask, request: HandoffRequest, spec: dict[str, Any] | None,
                session: list[Any]) -> HandoffDecision:
        """C-D70 (4): the full TASK, the commands already run and the follow-up, to a new worker session."""
        commands = list(request.args["commands"]) if request.args.get("commands") else task.commands
        analysis = request.args.get("analysis") or (task.spec or {}).get("analysis")
        payload = self._task_payload(task, task.run_revision or task.revision, commands=commands, analysis=analysis)
        if spec is not None:
            payload["paths"] = list(spec["paths"])
        payload.update({"resent_task": True, "resent_reason": "worker_session_may_not_have_it",
                        "resend_note": RESEND_NOTE, "follow_up": request.args["message"]})
        first = task.task_message_id is None  # the TASK never reached any worker: this message is the TASK
        self._record({"type": "task_resent", "task_id": task.task_id, "run_id": task.run_id,
                      "worker_session": list(session), "as_task_message": first})
        previous_state = self._task_message_state.get(task.task_id)
        if first:
            self._task_message_state[task.task_id] = "queued"
        self._resend_pending[task.task_id] = list(session)
        task_id = task.task_id

        def not_queued(result: Mapping[str, Any]) -> None:  # held at queue time: the next follow-up retries it
            with self._lock:
                if self._resend_pending.get(task_id) == list(session):
                    del self._resend_pending[task_id]
                if first and self._task_message_state.get(task_id) == "queued":
                    if previous_state is None:
                        self._task_message_state.pop(task_id, None)
                    else:
                        self._task_message_state[task_id] = previous_state
                self._record({"type": "task_resend_not_queued", "task_id": task_id,
                              "reason": result.get("reason")})

        return HandoffDecision({"status": "queued", "task_id": task.task_id, "manager_rule": MANAGER_RULE,
                                "resent_task": True,
                                "detail": "the worker is a new session: Workbench re-sends the full Task, the "
                                          "commands already run and this follow-up"},
                               message=OutboundMessage(task.task_id, task.run_revision, task.run_id, ActorRole.MANAGER,
                                                       ActorRole.WORKER,
                                                       MessageKind.TASK if first else MessageKind.QUESTION, payload,
                                                       prepare=self._commands_run_section(task.task_id)),
                               listener=self._follow_up_listener(task.task_id, self._resend_listener(
                                   task.task_id, task.run_id,
                                   list(commands) if request.args.get("commands") else None, first, list(session))),
                               on_not_queued=not_queued)

    def _commands_run_section(self, task_id: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        """p27-cd70-fix-01: ``commands_already_run`` read from the terminal journal when the outbox lane creates the
        re-sent message (so it lists what ran up to the delivery, not up to the manager's call)."""
        def prepare(payload: dict[str, Any]) -> dict[str, Any]:
            runs: Any = None
            if self.terminal_runs is not None:
                try:
                    runs = self.terminal_runs(task_id)
                except Exception:
                    runs = None
            payload["commands_already_run"] = [] if runs is None else list(runs)
            payload["commands_already_run_note"] = (" ".join([COMMANDS_RUN_NOTE, *(
                note for note in getattr(runs, "notes", ()) if isinstance(note, str))]) if runs is not None
                else "The terminal journal could not be read; it does not show that nothing ran.")
            return payload
        return prepare

    def _resend_listener(self, task_id: str, run_id: str | None, commands: list[str] | None,
                         first: bool, session: list[Any] | None = None) -> Callable:
        replace_commands = None if commands is None else self._commands_listener(task_id, run_id, commands)
        state_listener = self._task_message_listener(task_id, run_id) if first and run_id is not None else None

        def listener(event: str, snapshot: Mapping[str, Any]) -> None:
            if event in ("submitted", "delivered", "unknown", "rejected", "held_paused", "withdrawn"):
                with self._lock:  # the re-send is no longer on its way (worker_session says who has it)
                    if self._resend_pending.get(task_id) == session:
                        del self._resend_pending[task_id]
            if event == "unknown" and state_listener is None:
                self._took_task(task_id, run_id, snapshot)  # the worker may have it (review P3-1)
            if state_listener is not None:
                state_listener(event, snapshot)  # the TASK message of the run (its id, running, worker_session)
            elif event in ("submitted", "delivered"):
                self._took_task(task_id, run_id, snapshot)
            if replace_commands is not None:
                replace_commands(event, snapshot)
        return listener

    def _took_task(self, task_id: str, run_id: str | None, snapshot: Mapping[str, Any]) -> None:
        """A worker OMP session accepted this Task's TASK (or its re-send): it has the Task now."""
        session, generation = snapshot.get("session_id"), snapshot.get("session_generation")
        if not isinstance(session, str) or type(generation) is not int:
            return
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.run_id != run_id or task.status == "closed":
                return
            self._task_message_state[task_id] = "sent"
            if task.worker_session != [session, generation]:
                task.worker_session = [session, generation]
                self._record({"type": "task_worker_session", "task_id": task_id, "session_id": session,
                              "generation": generation})
                self._save_task(task)
            self._wake.notify_all()

    def _question(self, task: FlowTask, request: HandoffRequest, spec: dict[str, Any] | None) -> HandoffDecision:
        session = self._needs_resend(task)
        if session is not None:
            return self._resend(task, request, spec, session)
        payload: dict[str, Any] = {"handoff": "to_worker", "kind": task.kind, "message": request.args["message"],
                                   "task_id": task.task_id}
        listener = None
        if spec is not None:
            payload["paths"] = list(spec["paths"])
        if task.kind == "work":
            payload.update(_analysis_fields(request.args.get("analysis") or (task.spec or {}).get("analysis")))
            if request.args.get("commands"):  # C-D69 (6): a follow-up's commands replace the Task's list ...
                replacement = list(request.args["commands"])
                payload.update(_commands_fields(replacement))
                listener = self._commands_listener(task.task_id, task.run_id, replacement)  # ... once delivered
        return HandoffDecision({"status": "queued", "task_id": task.task_id, "manager_rule": MANAGER_RULE},
                               message=OutboundMessage(
            task.task_id, task.run_revision, task.run_id, ActorRole.MANAGER, ActorRole.WORKER,
            MessageKind.QUESTION, payload), listener=self._follow_up_listener(task.task_id, listener))

    def _follow_up_listener(self, task_id: str, inner: Callable | None) -> Callable:
        """A manager follow-up's outbox events: ``inner`` (if any) first, then ``follow_up_submitted`` once the
        worker's OMP accepted it (p27-cd70-02 Q2)."""
        told = [False]

        def listener(event: str, snapshot: Mapping[str, Any]) -> None:
            if inner is not None:
                inner(event, snapshot)
            if event in ("submitted", "delivered") and not told[0]:
                told[0] = True
                hook = self.follow_up_submitted
                if hook is not None:
                    try:
                        hook(task_id)
                    except Exception:
                        pass  # the watchdog never changes the flow
        return listener

    def _commands_listener(self, task_id: str, run_id: str | None, commands: list[str]) -> Callable:
        """The follow-up's new commands take effect when the worker's OMP accepted the message (a failed or
        unconfirmed delivery keeps the old list, which the message that did not arrive never replaced)."""
        def listener(event: str, snapshot: Mapping[str, Any]) -> None:
            if event not in ("submitted", "delivered"):
                return
            with self._lock:
                task = self.tasks.get(task_id)
                if task is None or task.run_id != run_id or task.status == "closed" or task.commands == commands:
                    return
                task.commands = list(commands)
                self._record({"type": "commands_replaced", "task_id": task_id, "count": len(commands)})
                self._save_task(task)
        return listener

    # -- cancel ---------------------------------------------------------------------
    def _cancel(self, request: HandoffRequest) -> HandoffDecision:
        task_id = request.args.get("task_id")
        if task_id is None:
            return HandoffDecision(rejected("task_id_required", detail="task_id: cancel true needs the task_id of "
                                                                       "the Task to cancel"))
        task = self.tasks.get(task_id)
        if task is None:
            return HandoffDecision(rejected("unknown_task", detail=UNKNOWN_TASK_DETAIL))
        if task.status == "closed":
            return HandoffDecision(rejected("task_closed") | {"closed_reason": task.closed_reason})
        record = {"by": "manager", "message": request.args["message"][:SUMMARY_MAX], "at": _now(),
                  **self._standing(request)}
        self._record({"type": "cancel_requested", "task_id": task_id, **record})
        if self._following == task_id:  # the runner drives a host run: it ends the Task after the exit
            self._withdraw(task_id)
            task.cancel_requested = record
            task.set_status("cancelling")
            task.held_reason = "cancel_waiting_for_host_exit"
            self._save_task(task)
            self._wake.notify_all()
            return HandoffDecision({"status": "cancel_requested", "task_id": task_id, "detail": CANCEL_WAIT_DETAIL})
        task.cancel_requested = record
        notified = self._finish_cancel(task, task.run_id)
        return HandoffDecision({"status": "cancelled", "task_id": task_id, "worker_notified": notified})

    def _finish_cancel(self, task: FlowTask, run_id: str | None) -> bool:
        """Cancel the current run (if any), close the Task and tell the worker; under the flow lock."""
        record = task.cancel_requested or {}
        cancelled_run = None
        if run_id is not None:
            with self._repository() as repository:
                current = repository.get_current_run(task.task_id)
                if current is not None and current.get("run_id") == run_id:
                    repository.cancel_run(run_id, "cancelled_by_manager")
                cancelled_run = repository.get_run(run_id)
        task.last_result = {"run_id": run_id, "outcome": "cancelled"}
        revision = task.run_revision
        self._close(task, "cancelled")
        # R2: nothing of the cancelled Task that was not submitted yet goes out, before the cancel notice.
        withdrawn = self._withdraw(task.task_id)
        never_sent = (MessageKind.TASK.value in withdrawn
                      or self._task_message_state.get(task.task_id) in ("queued", "not_sent"))
        self._notice(kind="task_cancelled", task_id=task.task_id, run_id=run_id)
        notified = False
        if cancelled_run is not None and not never_sent:
            payload = {"handoff": "to_worker", "kind": task.kind, "task_id": task.task_id, "cancel": True,
                       "message": record.get("message") or "cancelled by the manager"}
            queued = self._handoffs.enqueue(OutboundMessage(
                task.task_id, revision or cancelled_run["revision"], run_id, ActorRole.MANAGER, ActorRole.WORKER,
                MessageKind.QUESTION, payload), origin=f"cancel:{task.task_id}")
            notified = queued.get("status") == "queued"
        if run_id is not None:
            threading.Thread(target=self._notify_lifecycle, args=("run_ended", run_id), daemon=True).start()
        return notified

    def _end_inactive(self, task: FlowTask, reason: str) -> None:
        """A finished experiment or blocked free-work Task ends when a new Task arrives."""
        run_id = task.run_id
        if run_id is not None:
            with self._repository() as repository:
                current = repository.get_current_run(task.task_id)
                if current is not None and current.get("run_id") == run_id:
                    repository.cancel_run(run_id, reason)
            threading.Thread(target=self._notify_lifecycle, args=("run_ended", run_id), daemon=True).start()
        self._close(task, reason)
        self._withdraw(task.task_id)

    def _withdraw(self, task_id: str) -> list[str]:
        withdraw = getattr(self._handoffs, "withdraw", None)
        if withdraw is None:
            return []
        try:
            return list(withdraw(task_id))
        except Exception:
            return []

    # -- worker reports -------------------------------------------------------------
    def _to_manager(self, request: HandoffRequest) -> HandoffDecision:
        args = request.args
        task = self.active()
        if task is None:
            return HandoffDecision(rejected("no_active_task"))
        if args.get("task_id") not in (None, task.task_id):
            return HandoffDecision(rejected("unknown_task"))
        if task.run_id is None:
            return HandoffDecision(held("no_active_run"))
        if args.get("task_id") is None and task.kind == "work":
            # R2: an untagged report belongs to the active Task only once its TASK reached the worker.
            self._settle(lambda: self._task_message_state.get(task.task_id) == "delivering")
            if self._task_message_state.get(task.task_id) in ("queued", "not_sent"):
                return HandoffDecision(rejected("task_not_delivered") | {"task_id": task.task_id,
                                                                         "detail": TASK_NOT_DELIVERED_DETAIL})
            if task.status == "closed" or task.run_id is None:
                return HandoffDecision(rejected("no_active_task"))
        payload: dict[str, Any] = {"handoff": "to_manager", "kind": args["kind"], "message": args["message"]}
        for name in ("requires_code_change", "reason"):
            if name in args:
                payload[name] = args[name]
        if "request" in args:  # a worker's own request: classified only, never dispatched
            request_paths, path_errors = repo_relative_paths(list(args["request"]["paths"]), self._project_dir,
                                                             "request.paths")
            try:
                if path_errors:
                    raise ValueError(path_errors[0])
                category = classify_worker_request({**args["request"], "paths": request_paths},
                                                   list(task.spec.get("paths") or []))
            except ValueError:
                category, request_paths = "invalid_paths", list(args["request"]["paths"])
            payload["request"] = {**args["request"], "paths": request_paths, "classification": category,
                                  "dispatch_authorized": False}
        if args["kind"] == "answer":
            if args.get("in_reply_to") is None:
                return HandoffDecision(rejected("in_reply_to_required"))
            kind, reply_to = MessageKind.ANSWER, args["in_reply_to"]
        else:
            if task.task_message_id is None:
                return HandoffDecision(rejected("no_task_message"))
            if args.get("in_reply_to") is not None:
                payload["in_reply_to"] = args["in_reply_to"]
            kind, reply_to = MessageKind.REPORT, task.task_message_id
        listener = None
        report = task.kind == "work" and args["kind"] in {"done", "blocked"}
        if report and self.terminal_runs is not None:  # C-D69 (6)(d): the worker need not repeat its commands
            try:
                runs = self.terminal_runs(task.task_id)
            except Exception:
                runs = None
            if runs is not None:
                payload["commands_run"] = list(runs)
                payload["commands_run_note"] = " ".join(
                    [COMMANDS_RUN_NOTE, *(note for note in getattr(runs, "notes", ()) if isinstance(note, str))])
        if report:
            listener = self._report_listener(task.task_id, task.run_id, args["kind"], request.tool_call_id)
            task.held_reason = f"{args['kind']}_report_pending"
            self._save_task(task)
        return HandoffDecision({"status": "queued", "task_id": task.task_id}, message=OutboundMessage(
            task.task_id, task.run_revision, task.run_id, ActorRole.WORKER, ActorRole.MANAGER, kind, payload,
            in_reply_to_message_id=reply_to), listener=listener, keep_across_pause=report)

    def _report_listener(self, task_id: str, run_id: str, kind: str, tool_call_id: str) -> Callable:
        """The done/blocked report's outbox events (R1): accepted -> the worker is free; lost -> the Task closes."""
        settled = [False]  # decided once: at the submission, else at the terminal state

        def listener(event: str, snapshot: Mapping[str, Any]) -> None:
            if event == "created":
                return
            ended = False
            with self._lock:
                if event == "delivering":
                    self._report_inflight.add(task_id)
                    self._wake.notify_all()
                    return
                self._report_inflight.discard(task_id)
                self._wake.notify_all()
                if event == "deferred" or settled[0]:
                    return
                task = self.tasks.get(task_id)
                if task is None or task.run_id != run_id or task.status == "closed":
                    return  # cancelled/superseded (its messages were withdrawn) or restarted
                settled[0] = True
                if event in ("submitted", "delivered"):
                    ended = self._report_accepted(task, run_id, kind, tool_call_id, snapshot)
                else:  # unknown / rejected / held_paused before any acceptance: never resent
                    ended = self._report_lost(task, run_id, kind, event)
            if ended:
                self._notify_lifecycle_later("run_ended", run_id)
        return listener

    def _report_accepted(self, task: FlowTask, run_id: str, kind: str, tool_call_id: str,
                         snapshot: Mapping[str, Any]) -> bool:
        """The manager OMP accepted the report: the worker is free (under the flow lock); True when the run ended."""
        task_id = task.task_id
        with self._repository() as repository:
            details = {"source": "worker_to_manager", "kind": kind, "tool_call_id": tool_call_id,
                       "report_message_id": snapshot.get("message_id"), "report": "accepted_by_manager"}
            if kind == "done":
                repository.complete_run(run_id, details)
            else:
                run = repository.get_run(run_id)
                repository.record_decision(task_id, run["revision"], run_id, "blocked",
                                           retry_count_used=run["retry_count_used"], details=details)
        if kind == "done":
            task.last_result = {"run_id": run_id, "outcome": "done"}
            self._record({"type": "run_ended", "task_id": task_id, "run_id": run_id, "outcome": "done"})
            self._close(task, "done")
            return True
        # the worker is free; the Task stays open for a follow-up or ends with a new Task
        task.last_result = {"run_id": run_id, "outcome": "blocked"}
        task.held_reason = "worker_blocked"
        task.set_status("blocked")
        self._save_task(task)
        return False

    def _report_lost(self, task: FlowTask, run_id: str, kind: str, event: str) -> bool:
        """The report never reached (or may not have reached) the manager: close, never resend (R1)."""
        reason = "report_outcome_unknown" if event == "unknown" else "report_not_delivered"
        with self._repository() as repository:
            current = repository.get_current_run(task.task_id)
            if current is not None and current.get("run_id") == run_id:
                details = {"source": "worker_to_manager", "kind": kind, "report_outcome": event}
                if kind == "done" and event == "unknown":
                    repository.complete_run(run_id, details)
                elif kind == "done":
                    repository.fail_run(run_id, details)
                else:
                    repository.cancel_run(run_id, reason)
        task.last_result = {"run_id": run_id, "outcome": reason, "report_kind": kind}
        self._record({"type": "run_ended", "task_id": task.task_id, "run_id": run_id, "outcome": reason})
        self._close(task, reason)
        self._notice(kind=reason, task_id=task.task_id, run_id=run_id, report_kind=kind)
        return True

    def _close(self, task: FlowTask, reason: str) -> None:
        task.closed_reason, task.pending_start, task.run_id = reason, None, None
        task.held_reason = None
        task.set_status("closed")
        self._record({"type": "task_closed", "task_id": task.task_id, "reason": reason})
        self._save_task(task)

    def _paused_now(self) -> bool:
        try:
            return self._paused() is not False
        except Exception:
            return True

    def _schedule(self, task: FlowTask, revision: int, origin: str) -> None:
        task.pending_start = {"revision": revision, "origin": origin}
        task.held_reason = None
        self._save_task(task)
        self._wake.notify_all()

    # -- the runner thread ------------------------------------------------------------------
    def _runner(self) -> None:
        while True:
            with self._wake:
                if self._stop:
                    return
                task = next((t for t in self.tasks.values() if t.status != "closed" and t.pending_start), None)
                if task is None:
                    self._wake.wait(self._poll_interval)
                    retry = self._home_cwd is not None and not self._stop
                else:
                    job = (task.task_id, task.kind, dict(task.pending_start))
            if task is None:
                if retry:
                    self._retry_home_cwd()
                continue
            try:
                if job[1] == "work":
                    self._start_work(job[0], job[2])
                else:
                    self._run_experiment(job[0], job[2])
            except Exception as exc:  # never let the runner die silently
                with self._lock:
                    self._following = None
                    task = self.tasks.get(job[0])
                    if task is not None and task.status != "closed":
                        reason = getattr(exc, "reason", None)  # G3: a short machine reason when the error has one
                        self._runner_failed(task, f"runner_error:{type(exc).__name__}"
                                                  + (f":{reason}" if isinstance(reason, str) and reason else ""))
            with self._wake:
                if not self._stop:
                    self._wake.wait(0.01)

    def _runner_failed(self, task: FlowTask, reason: str) -> None:
        """Nothing started: the worker is free again and the manager learns why (under the flow lock)."""
        task.pending_start = None
        if task.run_id is not None:
            task.held_reason = reason  # a run exists: its report or a cancel ends the Task
            self._save_task(task)
            return
        task.last_result = {"outcome": "not_started", "reason": reason}
        self._notice(kind="task_not_started", task_id=task.task_id, reason=reason)
        if task.kind == "experiment" and task.runs_started > 0:
            task.held_reason = reason
            task.set_status("finished")
            self._save_task(task)
        else:
            self._close(task, reason)

    def _set_held(self, task_id: str, reason: str | None) -> None:
        with self._lock:
            task = self.tasks.get(task_id)
            if task is not None and task.status != "closed" and task.held_reason != reason:
                task.held_reason = reason
                self._save_task(task)

    def _wait(self, seconds: float) -> bool:
        """Sleep unless stopping; False when the flow is stopping."""
        with self._wake:
            if not self._stop:
                self._wake.wait(seconds)
            return not self._stop

    def _start_work(self, task_id: str, job: Mapping[str, Any]) -> None:
        if self._paused_now():
            self._set_held(task_id, "paused")
            self._wait(self._poll_interval)
            return
        if self._omp_idle(ActorRole.WORKER) is None:
            self._set_held(task_id, "worker_not_connected")
            self._wait(self._poll_interval)
            return
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.pending_start != job or task.status == "closed":
                return
            revision = job["revision"]
            with self._repository() as repository:
                run_id = repository.start_run(task_id, revision, inputs={"kind": "work", "origin": job["origin"]})
            task.runs_started += 1
            task.pending_start, task.run_id, task.run_revision = None, run_id, revision
            task.held_reason, task.task_message_id = None, None
            task.set_status("starting")
            self._save_task(task)
            self._record({"type": "run_started", "task_id": task_id, "run_id": run_id, "revision": revision})
            payload = self._task_payload(task, revision)
            outbound = OutboundMessage(task_id, revision, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                       MessageKind.TASK, payload)
            self._task_message_state[task_id] = "queued"
        queued = self._handoffs.enqueue(outbound, origin=str(job["origin"]),
                                        listener=self._task_message_listener(task_id, run_id),
                                        keep_across_pause=True)  # R3: never submitted -> sent after a resume
        if queued.get("status") != "queued":
            with self._lock:
                self._task_message_state[task_id] = "not_sent"
            self._set_held(task_id, f"instruction_{queued.get('reason', 'not_queued')}")

    @staticmethod
    def _task_payload(task: FlowTask, revision: int, *, commands: list[str] | None = None,
                      analysis: Any = None) -> dict[str, Any]:
        """The first TASK message of a work Task (also re-sent to a new worker session, C-D70 (4))."""
        spec = task.spec or {}
        payload: dict[str, Any] = {"handoff": "to_worker", "kind": "work", "task_id": task.task_id,
                                   "revision": revision, "goal": spec.get("goal"),
                                   "paths": list(spec.get("paths") or []),
                                   "message": task.message or task.summary}  # older records: summary
        if "instructions" in spec:
            payload["instructions"] = spec["instructions"]
        payload.update(_analysis_fields(analysis or spec.get("analysis")))
        payload.update(_commands_fields(task.commands if commands is None else commands))
        return payload

    _TASK_MESSAGE_STATES = {"delivering": "delivering", "deferred": "queued", "submitted": "sent",
                            "delivered": "sent", "unknown": "sent", "rejected": "not_sent",
                            "held_paused": "not_sent", "withdrawn": "not_sent"}

    def _task_message_listener(self, task_id: str, run_id: str) -> Callable:
        submitted = [False]

        def listener(event: str, snapshot: Mapping[str, Any]) -> None:
            with self._lock:
                state = self._TASK_MESSAGE_STATES.get(event)
                if state is not None and not (submitted[0] and state != "sent"):
                    self._task_message_state[task_id] = state  # "unknown": the worker may have it
                    self._wake.notify_all()
                if event == "submitted":
                    submitted[0] = True
                task = self.tasks.get(task_id)
                if task is None or task.run_id != run_id or task.status == "closed":
                    return
                if event == "created":
                    task.task_message_id = snapshot.get("message_id")
                    if task.held_reason is not None and task.held_reason.startswith("instruction_"):
                        task.held_reason = None  # C-D70 (4): a re-sent TASK after a lost one
                    task.set_status("running")
                elif event in ("rejected", "held_paused") or event == "unknown" and not submitted[0]:
                    task.held_reason = f"instruction_{event}"
                    session, generation = snapshot.get("session_id"), snapshot.get("session_generation")
                    if event == "unknown" and isinstance(session, str) and type(generation) is int:
                        # review P3-1: the worker session may have it (watchdog, no false re-send)
                        task.worker_session = [session, generation]
                elif event in ("submitted", "delivered"):
                    session, generation = snapshot.get("session_id"), snapshot.get("session_generation")
                    if not isinstance(session, str) or type(generation) is not int \
                            or task.worker_session == [session, generation]:
                        return
                    task.worker_session = [session, generation]  # C-D70 (4): this worker session has the Task
                    self._record({"type": "task_worker_session", "task_id": task_id, "session_id": session,
                                  "generation": generation})
                else:
                    return
                self._save_task(task)
                revision, message_id = task.run_revision, task.task_message_id
            if event == "created" and message_id is not None:
                self._notify_lifecycle("work_started", task_id, revision, run_id, message_id)
        return listener

    # -- experiment runs ------------------------------------------------------------------------
    def _experiment_blocker(self, task: FlowTask, port: Any) -> str | None:
        if self._paused_now():
            return "paused"
        if port is None:
            return "host_terminal_unavailable"
        names = set((task.spec or {}).get("execution", {}).get("environment") or [])
        try:
            missing = sorted(names - set(self._experiment.environment_names()))
        except Exception:
            missing = sorted(names)
        if missing:
            return "environment_missing:" + ",".join(missing)
        if self._omp_idle(ActorRole.WORKER) is not True:
            return "worker_busy"
        busy = port.busy()
        if busy is not None:
            return "host_terminal_busy"
        return None

    def _run_experiment(self, task_id: str, job: Mapping[str, Any]) -> None:
        ports = self._experiment
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.pending_start != job or task.status == "closed":
                return
            if ports is None:
                self._runner_failed(task, "experiment_runner_unavailable")
                return
        # C-D68: a terminal command of the worker owns the host shell until it ended and was given back
        # (p27-cd68-fix-01 P3-5: its own reason, so the manager waits instead of sending the user to clear it).
        owner = self._host_gate.acquire("experiment")
        if owner is not None:
            self._set_held(task_id, WORKER_TERMINAL_HELD if owner == "terminal" else "host_terminal_busy")
            self._wait(self._poll_interval)
            return
        port = None
        try:
            port = ports.host_shell()
            blocker = self._experiment_blocker(task, port)  # the idle-only rule (nothing is held yet)
            if blocker is not None:
                self._set_held(task_id, blocker)
                self._wait(self._poll_interval)
                return
            self._execute(task, job, port, ports)
        finally:
            self._host_gate.release("experiment")
            with self._lock:
                self._following = None
            if port is not None:
                try:
                    port.release_hold()
                except Exception:
                    pass
                port.detach()

    def _typing_hold(self, task: FlowTask, port: Any, ports: ExperimentPorts, refused: list[str]) -> Callable:
        """R5: called by the workflow just before its first keystroke into the host shell.

        The shell must still be idle and is held from here until the start returns;
        else nothing is typed (``WorkflowHeld``) and ``refused`` names why.
        """
        def before_shell_input() -> None:
            reason = None
            if self._cancelled(task):
                reason = "cancelled"
            elif self._paused_now():
                reason = "paused"
            else:
                try:
                    if port.busy() is not None or port.hold(HOLD_REASON) is not None:
                        reason = "host_terminal_busy"
                except Exception:
                    reason = "host_terminal_busy"
            if reason is not None:
                refused.append(reason)
                raise WorkflowHeld(f"{reason}: nothing was typed into the host shell")
            self._remember_home_cwd(port, ports)
        return before_shell_input

    def _execute(self, task: FlowTask, job: Mapping[str, Any], port: Any, ports: ExperimentPorts) -> None:
        revision = job["revision"]
        repository = self._repository_factory()
        run = None
        try:
            workflow = ports.make_workflow(repository)
            late_hold = accepts_keyword(workflow.start, "before_shell_input")
            if not late_hold and port.hold(HOLD_REASON) is not None:
                # A workflow that cannot say when it types is held for its whole start (fail-safe).
                self._set_held(task.task_id, "host_terminal_busy")  # changed since the idle check
                self._wait(self._poll_interval)
                return
            with self._lock:
                if task.pending_start != job or task.status == "closed":  # cancelled meanwhile
                    return
                task.pending_start, task.held_reason = None, None
                task.set_status("starting")
                task.runs_started += 1
                self._following = task.task_id
                self._save_task(task)
            refused: list[str] = []
            # stuck-review-02 P3-2: set right before the start types wb-handoff (like the terminal's handoff_typed)
            watch = _HandoffWatch(port)
            options: dict[str, Any] = {}
            if late_hold:
                options["before_shell_input"] = self._typing_hold(task, port, ports, refused)
            else:
                self._remember_home_cwd(port, ports)
            slot = ports.worktrees_root / f"{task.task_id}-r{revision}-{uuid4().hex[:8]}"
            try:
                executable = getattr(getattr(port, "choice", None), "executable", None)
                too_long = experiment_command_error((task.spec or {}).get("execution", {}).get("command") or "",
                                                    executable if isinstance(executable, str) else None)
                if too_long is not None:  # C-D69 (5)(b): this shell's real path, before anything is typed
                    refused.append("command_too_long")
                    raise WorkflowHeld(too_long)
                run = workflow.start(task.task_id, revision, worktree_path=slot,
                                     artifacts_root=ports.artifacts_root, automation=ports.automation(), shell=port,
                                     **options)
            except Exception as exc:
                watch.close()
                if watch.handoff_typed:  # its wb-handoff may reach the control wait late (input still held)
                    self._await_settled(port)
                # stuck-review-02 P3-1: the start's hold stays until the shell is given back (as in terminal)
                self._give_back(port, ports, after_failure=watch.handoff_typed, hold_reason=HOLD_REASON)
                port.release_hold()
                # F2: a rejected worker response keeps the bridge's machine reason, not just "ValueError".
                error = (refused[0] if refused else exc.reason if isinstance(exc, WorkerResponseRejected)
                         else type(exc).__name__)
                tell_manager = False
                with self._lock:
                    task.last_result = {"outcome": "start_failed", "error": error,
                                        "detail": str(exc)[:SUMMARY_MAX]}
                    self._record({"type": "run_start_failed", "task_id": task.task_id, "revision": revision,
                                  "error": error})
                    if task.cancel_requested is not None:
                        self._finish_cancel(task, None)
                    else:
                        task.held_reason = f"start_failed:{error}"
                        task.set_status("finished")  # the worker is free; the manager may re-run or move on
                        self._save_task(task)
                        self._notice(kind="run_start_failed", task_id=task.task_id, error=error)
                        tell_manager = True
                if tell_manager:
                    self._tell_manager_start_failed(task.task_id, revision, error, str(exc),
                                                    getattr(exc, "workbench_start_failure", None))
                return
            watch.close()
            port.release_hold()
            with self._lock:
                task.run_id, task.run_revision, task.task_message_id = run.run_id, revision, run.task_message_id
                if task.status != "cancelling":
                    task.set_status("running")
                self._save_task(task)
                self._record({"type": "run_started", "task_id": task.task_id, "run_id": run.run_id,
                              "revision": revision})
            self._notify_lifecycle("experiment_started", run)
            self._follow(task, run, port)
        finally:
            if run is not None:
                run.close()
            repository.close()

    def _tell_manager_start_failed(self, task_id: str, revision: int, error: str, detail: str,
                                   failure: Any) -> None:
        """Smoke-02 E3: tell the manager once that the experiment run did not start (outside the flow lock).

        Besides ``notices[]`` (seen only with the next ``to_worker`` result), the manager gets one Workbench
        notice through the existing delivery path: a worker->manager REPORT replying to the failed run's TASK
        message (the path the workflow's own preparation report uses), delivered by the manager lane when
        the manager OMP is idle and never resent. Nothing is sent when the run never got a TASK message (the
        mailbox can address only a reply to it), or when the workflow already reported to the manager.
        """
        if not isinstance(failure, Mapping):
            return
        run_id, message_id = failure.get("run_id"), failure.get("task_message_id")
        if (not isinstance(run_id, str) or not isinstance(message_id, str)
                or failure.get("manager_report") not in (None, "not_created")):
            return
        text = (f"Workbench notice: the experiment run for Task {task_id} did not start ({error}: "
                f"{detail[:300]}). The worker is free. Tell the user; after the cause is fixed re-run it "
                "(to_worker with this task_id and run: true) or send a new Task.")
        self._tell_manager(task_id, failure.get("revision") or revision, run_id, message_id,
                           {"notice": "run_start_failed", "error": error}, text, f"start_failed:{task_id}",
                           record_type="start_failure_notice")

    def _tell_manager(self, task_id: str, revision: int | None, run_id: str, message_id: Any,
                      fields: Mapping[str, Any], text: str, origin: str, *,
                      record_type: str = "workbench_notice") -> None:
        """One Workbench notice to the manager (outside the flow lock), never resent: a worker->manager REPORT
        replying to the run's TASK message, delivered by the manager lane when the manager OMP is idle."""
        if not isinstance(message_id, str):
            with self._lock:
                self._record({"type": record_type, "task_id": task_id, "run_id": run_id, "state": "not_sent",
                              "reason": "no_task_message"})
            return
        payload = {"handoff": "workbench_notice", "source": "workbench", **fields,
                   "task_id": task_id, "message": text[:SUMMARY_MAX]}

        def listener(event: str, snapshot: Mapping[str, Any]) -> None:
            if event in ("submitted", "delivered", "unknown", "rejected", "held_paused", "withdrawn"):
                with self._lock:
                    self._record({"type": record_type, "task_id": task_id, "run_id": run_id,
                                  "state": event, "message_id": snapshot.get("message_id")})

        try:
            queued = self._handoffs.enqueue(OutboundMessage(
                task_id, revision, run_id, ActorRole.WORKER, ActorRole.MANAGER,
                MessageKind.REPORT, payload, in_reply_to_message_id=message_id),
                origin=origin, listener=listener)
        except Exception as exc:  # the notices[] entry still tells the manager
            queued = {"status": "held", "reason": type(exc).__name__}
        with self._lock:
            self._record({"type": record_type, "task_id": task_id, "run_id": run_id,
                          "state": queued.get("status"), "reason": queued.get("reason")})

    @staticmethod
    def _await_settled(port: Any) -> None:
        """Until the shell shows the user's prompt or a control wait with no request (bounded by RETURN_WAIT).

        A wb-handoff typed by the start (stuck-test-01 P3-1) may be processed after the claim gave up; its
        control wait is then given back by ``_give_back``. Nothing else can type meanwhile: input is held.
        """
        deadline = time.monotonic() + RETURN_WAIT
        while time.monotonic() < deadline:
            try:
                state = port.poll(0.02)
            except Exception:
                return
            life = state.get("lifecycle") or {}
            if state.get("parent_mode") == "manual_prompt" or (
                    state.get("parent_mode") == "control_wait" and life.get("request_id") is None):
                return

    def _give_back(self, port: Any, ports: ExperimentPorts | None = None, *, after_failure: bool = False,
                   hold_reason: str | None = None) -> None:
        """Return the host shell to the user when the manager holds it with nothing in flight.

        CW-18 F2: then the user's directory from before the run is restored. User
        input stays held from the takeover until that ``cd`` is done, so nothing
        the user types mixes with it. ``hold_reason``: the caller's own hold, kept through the takeover and the
        restore and released at the end (a failed start).
        """
        held = hold_reason is not None
        reason = hold_reason or RETURN_REASON
        try:
            state = port.snapshot()
            life = state.get("lifecycle") or {}
            returned = (not life.get("unknown") and life.get("lifetime") == "ended"
                        and life.get("input_returned") and life.get("control_returned"))
            # C-D69 (5)(a): also a start that failed after its wb-handoff but before the manager claim (idle wait)
            idle_wait = (after_failure and life.get("request_id") is None
                         and state.get("parent_mode") == "control_wait")
            if (state.get("input_owner") == "manager" and (life.get("request_id") is None or returned)) or idle_wait:
                if not held and self._home_cwd is not None and ports is not None:
                    try:
                        held = port.hold_return(RETURN_REASON) is True
                    except Exception:
                        held = False
                port.request_takeover()
        except Exception:
            pass
        try:
            if ports is not None:
                self._restore_home_cwd(port, ports, RESTORE_IDLE_WAIT, reason=reason)
        finally:
            if held:
                try:
                    port.release_hold(reason)
                except Exception:
                    pass

    # -- F2: the user's host shell directory --------------------------------------------------
    def _remember_home_cwd(self, port: Any, ports: ExperimentPorts) -> None:
        """Before a run types its ``cd``: the shell's directory (a pending one from an earlier run wins)."""
        try:
            current = port.cwd()
        except Exception:
            current = None
        if current is None:
            return
        if not _under(current, ports.worktrees_root):
            self._home_cwd = current  # the user's own directory
        # else the shell is still in an earlier run's worktree: the pending directory stays the target

    def _restore_home_cwd(self, port: Any, ports: ExperimentPorts, idle_wait: float,
                          reason: str = RETURN_REASON) -> str | None:
        target = self._home_cwd
        if target is None:
            return None
        try:
            outcome = port.restore_cwd(target, ports.worktrees_root, reason=reason, idle_wait=idle_wait)
        except Exception as exc:
            outcome = f"error:{type(exc).__name__}"
        if outcome in {"restored", "unchanged", "user_moved", "target_missing", "shell_gone"}:
            self._home_cwd = None
            if outcome != "unchanged":
                with self._lock:
                    self._record({"type": "host_cwd_restore", "outcome": outcome})
        return outcome

    def _retry_home_cwd(self) -> None:
        """A restore that found the shell busy is tried again while no Task start is pending."""
        ports = self._experiment
        if ports is None:
            self._home_cwd = None
            return
        if self._host_gate.acquire("experiment") is not None:
            return  # a terminal command owns the host shell; tried again later
        port = None
        try:
            port = ports.host_shell()
            if port is not None:
                self._restore_home_cwd(port, ports, 0.0)
        except Exception:
            pass
        finally:
            self._host_gate.release("experiment")
            if port is not None:
                try:
                    port.detach()
                except Exception:
                    pass

    def _cancelled(self, task: FlowTask) -> bool:
        with self._lock:
            return task.cancel_requested is not None

    def _end_cancelled_run(self, task: FlowTask, run: Any) -> None:
        with self._lock:
            self._finish_cancel(task, run.run_id)

    def _follow(self, task: FlowTask, run: Any, port: Any) -> None:
        # The host command is never interrupted (also not on cancel): collect until it exits.
        while True:
            record = run.collect(timeout=self._collect_slice, paused=self._paused_now())
            if record.get("shell_state") == "exited" and record.get("exit_confirmed"):
                break
            if record.get("shell_state") == "unknown":
                self._set_held(task.task_id, "shell_unknown")  # CW-10: no judgment, no replay
                with self._lock:
                    if task.status != "cancelling":
                        task.set_status("held")
                        self._save_task(task)
                return
            if not self._wait(0):
                return
        self._give_back(port, self._experiment)
        if self._cancelled(task):
            self._end_cancelled_run(task, run)
            return
        with self._lock:
            task.set_status("waiting_report")
            self._save_task(task)
        accepted: list[bool] = []
        on_report = self._report_hook(task, run, accepted)
        # G2: the worker has been busy (not idle for the analysis) since then; fix-05 P3a: one continuous busy
        # period, so an idle observation starts a new one. ``deferred_since``: analysis deliveries deferred
        # while the worker looked idle in between (no busy observation), which must not wait forever either.
        busy_since: float | None = None
        deferred_since: float | None = None
        try:
            while True:  # judge only when not paused and the worker can take the staged analysis
                if not self._wait(0):
                    return
                if self._cancelled(task):
                    self._end_cancelled_run(task, run)
                    return
                if self._paused_now():
                    busy_since = deferred_since = None
                    self._set_held(task.task_id, "paused")
                    self._wait(self._poll_interval)
                    continue
                if self._omp_idle(ActorRole.WORKER) is not True:
                    deferred_since = None
                    busy_since = time.monotonic() if busy_since is None else busy_since
                    if time.monotonic() - busy_since >= self._analysis_idle_limit:
                        self._judgment_unavailable(task, run, self._worker_not_idle_reason(), None)
                        return
                    self._set_held(task.task_id, "worker_busy")
                    self._wait(self._poll_interval)
                    continue
                busy_since = None  # P3a: the worker is idle; a later busy observation starts a new period
                self._set_held(task.task_id, None)
                try:
                    record = self._call_with_report_hook(run.judge, on_report)
                    break
                except WorkerJudgmentUnavailable as exc:  # G2: rejected or unknown worker analysis
                    self._judgment_unavailable(task, run, exc.reason, exc.host_evidence or None)
                    return
                except WorkflowHeld as exc:
                    request = run._record.get("worker_analysis_request") or {}
                    if request.get("status") == MailboxStatus.DEFERRED.value:
                        now = time.monotonic()
                        busy_since = now if busy_since is None else busy_since
                        deferred_since = now if deferred_since is None else deferred_since
                        if now - min(busy_since, deferred_since) >= self._analysis_idle_limit:
                            self._judgment_unavailable(task, run, self._worker_not_idle_reason(), None)
                            return
                        self._set_held(task.task_id, "worker_busy")  # nothing was sent: ask again
                        self._wait(self._poll_interval)
                        continue
                    self._finish_run(task, run, "held", {"reason": str(exc)[:SUMMARY_MAX]})
                    return
                except Exception as exc:  # fix-05 P2: any other failure of the analysis stage or the report
                    self._run_error(task, run, accepted, on_report, exc)
                    return
            while not accepted and (record.get("report") or {}).get("status") == MailboxStatus.DEFERRED.value:
                self._set_held(task.task_id, "manager_busy")
                if not self._wait(self._poll_interval):
                    return
                if self._paused_now() or self._omp_idle(ActorRole.MANAGER) is not True:
                    continue
                try:
                    record = self._call_with_report_hook(run.retry_report, on_report)
                except Exception as exc:  # fix-05: the report may have reached the manager; never resent
                    self._run_error(task, run, accepted, on_report, exc)
                    return
        finally:
            with self._lock:
                self._report_inflight.discard(task.task_id)
                self._wake.notify_all()
        report = (record.get("report") or {}).get("status")
        if accepted:  # the worker was freed at the acceptance (R1); only the final receipt is recorded
            with self._lock:
                if task.last_result is not None and task.last_result.get("run_id") == run.run_id:
                    task.last_result["report"] = report
                    self._save_task(task)
                self._record({"type": "report_receipt", "task_id": task.task_id, "run_id": run.run_id,
                              "report": report})
            return
        judgment = (record.get("worker_judgment") or {}).get("judgment")
        self._finish_run(task, run, "reported", {"judgment": judgment, "report": report})

    def _run_error(self, task: FlowTask, run: Any, accepted: list[bool], on_report: Callable,
                   exc: Exception) -> None:
        """fix-05 P2: an unexpected exception from the run's analysis or report stage (runner thread).

        Before any report exists it ends like a rejected analysis (``analysis_error:<Type>``: indeterminate,
        run closed, worker free, automation ended, one manager notice, nothing resent), or keeps the CW-10 hold
        while paused. Once a report was created it never contradicts it: a report the manager accepted keeps
        its closed run and the error is only recorded; a report whose delivery outcome is unknown closes as
        ``report_outcome_unknown`` (R1, never resent).
        """
        record = getattr(run, "_record", None)
        record = record if isinstance(record, dict) else {}
        report = record.get("report") if isinstance(record.get("report"), dict) else None
        created = report is not None and report.get("status") != "not_sent"
        reason = f"{'report' if created else 'analysis'}_error:{type(exc).__name__}"
        if created and not accepted and report.get("accepted_by_manager") is True:
            on_report("submitted", dict(record))  # the manager has it: the worker is free (R1)
        if accepted:
            self._close_accepted_run(task, run, record, reason)
            with self._lock:
                if task.last_result is not None and task.last_result.get("run_id") == run.run_id:
                    task.last_result["error"] = reason
                    self._save_task(task)
                self._record({"type": "run_error", "task_id": task.task_id, "run_id": run.run_id,
                              "stage": "after_report_accepted", "error": reason})
            return
        if created:
            judgment = (record.get("worker_judgment") or {}).get("judgment")
            self._finish_run(task, run, "reported", {"judgment": judgment, "error": reason,
                                                     "report": report.get("status") or "delivery_unknown"})
            return
        if self._paused_now() and not self._cancelled(task):  # CW-10: a pause keeps the hold, no notice
            self._finish_run(task, run, "held", {"reason": f"paused:{reason}"})
            return
        rejected = record.get("worker_analysis_rejected")
        host = rejected.get("host_evidence") if isinstance(rejected, dict) else None
        self._judgment_unavailable(task, run, reason, host if isinstance(host, Mapping) and host else None)

    def _close_accepted_run(self, task: FlowTask, run: Any, record: Mapping[str, Any], reason: str) -> None:
        """The report reached the manager: its run is closed once (when the run itself could not close it)."""
        try:
            with self._repository() as repository:
                current = repository.get_current_run(task.task_id)
                if current is None or current.get("run_id") != run.run_id:
                    return
                details = {"judgment": (record.get("worker_judgment") or {}).get("judgment"),
                           "report": record.get("report"), "error": reason}
                if record.get("instruction_ended") is True:
                    repository.fail_run(run.run_id, {"requires_code_change": True, **details})
                else:
                    repository.complete_run(run.run_id, details)
        except Exception:
            pass

    def _worker_not_idle_reason(self) -> str:
        """G2/G3: why the analysis never started, with the last periodic review's outcome when it was not processed."""
        reason = "worker_not_idle_for_analysis"
        try:
            review = (self._lifecycle.status() or {}).get("review") or {}
        except Exception:
            review = {}
        status, detail = review.get("status"), review.get("reason")
        if isinstance(status, str) and status not in ("dispatched", "") and isinstance(detail, str) and detail:
            reason += f":review_{status}:{detail}"
        return reason[:200]

    def _judgment_unavailable(self, task: FlowTask, run: Any, reason: str,
                              host_evidence: Mapping[str, Any] | None) -> None:
        """smoke-04 G2/G3: the worker analysis was rejected or its outcome is unknown (runner thread).

        CW-10: the result is indeterminate (never success) and nothing is replayed (no second analysis, no
        report). The run closes, the worker is free (the Task is ``finished``; the manager may re-run or move
        on), the automation for the run ends, and the manager is told once with the reason and what was and was
        not judged. ``reason`` is a short machine code, never response text.
        """
        record = getattr(run, "_record", None)
        record = record if isinstance(record, dict) else {}
        if "worker_analysis_rejected" not in record:  # the run itself did not get to record it
            record["worker_analysis_rejected"] = {"stage": "analysis", "reason": reason, "host_evidence": host_evidence}
            try:
                run._persist()
            except Exception:
                pass
        judged = {"exit_status": record.get("exit_status"), "exit_confirmed": record.get("exit_confirmed") is True,
                  "raw_log_collected": isinstance(record.get("raw_log_collected"), dict)
                  and "error" not in record["raw_log_collected"],
                  "result_collected": isinstance(record.get("result_collected"), dict)
                  and "error" not in record["result_collected"],
                  "host_evidence": None if host_evidence is None else dict(host_evidence)}
        outcome = {"judgment": "indeterminate", "reasons": ["worker_analysis_unavailable"], "reason": reason,
                   "judged": judged, "not_judged": ["worker_analysis"], "report": "not_sent"}
        with self._lock:
            with self._repository() as repository:
                current = repository.get_current_run(task.task_id)
                if current is not None and current.get("run_id") == run.run_id:
                    repository.complete_run(run.run_id, outcome)
            if self._following == task.task_id:
                self._following = None
            task.last_result = {"run_id": run.run_id, "outcome": "judgment_unavailable", **outcome,
                                "run_closed": True}
            self._record({"type": "run_ended", "task_id": task.task_id, **task.last_result})
            revision, message_id = task.run_revision, task.task_message_id or getattr(run, "task_message_id", None)
            if task.cancel_requested is not None:
                self._finish_cancel(task, run.run_id)  # the cancel closes the Task and ends the automation
                return
            task.run_id, task.held_reason = None, f"judgment_unavailable:{reason}"
            task.set_status("finished")  # the worker is free (C-D66)
            self._save_task(task)
            self._notice(kind="run_judgment_unavailable", task_id=task.task_id, run_id=run.run_id, reason=reason)
        self._notify_lifecycle("run_ended", run.run_id)
        host = "not checked" if host_evidence is None else (
            f"{host_evidence.get('judgment')} ({', '.join(map(str, host_evidence.get('reasons') or []))})")
        text = (f"Workbench notice: the experiment run of Task {task.task_id} ended (exit status "
                f"{judged['exit_status']}, exit confirmed: {judged['exit_confirmed']}), but the worker's analysis "
                f"was not obtained ({reason}). Workbench's own evidence check: {host}. The worker's judgment is "
                "missing, so the result is indeterminate (not success); nothing was resent. The worker is free. "
                "Tell the user; re-run it (to_worker with this task_id and run: true) or send a new Task.")
        self._tell_manager(task.task_id, revision or getattr(run, "revision", None), run.run_id, message_id,
                           {"notice": "run_judgment_unavailable", "reason": reason, "judgment": "indeterminate",
                            "judged": judged, "not_judged": ["worker_analysis"]}, text, f"judgment_unavailable:{task.task_id}")

    @staticmethod
    def _call_with_report_hook(method: Callable, on_report: Callable) -> dict[str, Any]:
        if accepts_keyword(method, "on_report"):
            return method(on_report=on_report)
        return method()

    def _report_hook(self, task: FlowTask, run: Any, accepted: list[bool]) -> Callable:
        """R1: the experiment report's ``sending``/``submitted`` events from ``WorkflowRun`` (runner thread)."""
        def on_report(event: str, record: Mapping[str, Any]) -> None:
            if event == "sending":
                with self._lock:
                    self._report_inflight.add(task.task_id)
                    self._wake.notify_all()
                return
            if event != "submitted" or accepted:
                return
            accepted.append(True)
            with self._lock:
                self._report_inflight.discard(task.task_id)
                self._wake.notify_all()
                if self._following == task.task_id:
                    self._following = None  # nothing of this run is followed any more
                judgment = (record.get("worker_judgment") or {}).get("judgment")
                task.last_result = {"run_id": run.run_id, "outcome": "reported", "judgment": judgment,
                                    "report": "accepted_by_manager", "run_closed": True}
                self._record({"type": "run_ended", "task_id": task.task_id, "run_id": run.run_id,
                              **task.last_result})
                if task.status == "closed":
                    return
                if task.cancel_requested is not None:
                    self._finish_cancel(task, run.run_id)  # the report was delivered; then it closes
                    return
                task.run_id, task.held_reason = None, None
                task.set_status("finished")  # the worker is free (C-D66)
                self._save_task(task)
            self._notify_lifecycle_later("run_ended", run.run_id)
        return on_report

    def _finish_run(self, task: FlowTask, run: Any, outcome: str, details: Mapping[str, Any]) -> None:
        lost = None
        with self._lock:
            with self._repository() as repository:
                current = repository.get_current_run(task.task_id)
                closed = current is None or current.get("run_id") != run.run_id
                if not closed and outcome == "reported":
                    # R1: the report never reached (or may not have reached) the manager: the run and the
                    # Task close without a resend; the manager learns it from the next tool result.
                    report = details.get("report")
                    lost = "report_not_delivered" if report == MailboxStatus.REJECTED.value \
                        else "report_outcome_unknown"
                    closing = {"judgment": details.get("judgment"), "report_outcome": report}
                    if lost == "report_outcome_unknown":
                        repository.complete_run(run.run_id, closing)
                    else:
                        repository.fail_run(run.run_id, closing)
                    closed = True
            task.last_result = {"run_id": run.run_id, "outcome": lost or outcome, **details, "run_closed": closed}
            self._record({"type": "run_ended" if closed else "run_held", "task_id": task.task_id,
                          "run_id": run.run_id, **task.last_result})
            if task.cancel_requested is not None:
                self._finish_cancel(task, run.run_id)  # the report (if any) was delivered; then it closes
                return
            if lost is not None:
                self._close(task, lost)
                self._notice(kind=lost, task_id=task.task_id, run_id=run.run_id, report_kind="experiment")
            elif closed:  # the report reached the manager: the worker is free (C-D66)
                task.run_id, task.held_reason = None, None
                task.set_status("finished")
            else:  # held judgment: the run stays current until a cancel; nothing is replayed
                task.held_reason = f"run_{outcome}"
                task.set_status("held")
            self._save_task(task)
        if closed:
            self._notify_lifecycle("run_ended", run.run_id)


__all__ = ["ACTIVE_STATUSES", "FLOW_LEDGER_NAME", "INACTIVE_STATUSES", "RETRY_LIMIT", "STANDING_DELEGATION",
           "ExperimentPorts", "FlowTask", "TaskFlow"]
