"""CW-18 U3: the backend automation loop (C-D65 "자동화", L-CW18-POLICY).

``AutomationController`` binds the verified CW-11/CW-12/CW-15 parts to the
single active run and changes none of their policy:

- Run start (``experiment_started`` / ``work_started``, called by ``TaskFlow``):
  the run is made the active run of the one ``SerializedReviewAdmission``, the
  one ``PauseCoordinator`` binds it (``bind_run``), the run's first
  ``LifecycleRecord`` is committed to the backend's ``LifecycleJournal`` (the
  next generation of the previous record) and ``bind_production`` returns the
  run's ``LifecycleCoordinator``.
- Loop: ``tick`` (about every second on the controller's thread; the clock is
  injectable) calls ``LifecycleCoordinator.tick`` only. It reconciles first and
  admits ``WorkerReviewScheduler.tick`` through ``PauseCoordinator.dispatch_automatic``;
  the scheduler owns the 60 s interval, the busy merge (latest one), user
  priority and exit surfacing. A held tick shows the reconcile problems.
- Pause (``request_pause``): the shared paused flag is set at once (TaskFlow,
  HandoffService and the workflow's AutomationState read it), then
  ``PauseCoordinator.pause`` runs on a transition thread (local fence first,
  manager turn abort request, worker pause). The interruption is shown as
  requested / confirmed (turn stop observed) / unknown. Host collection is not
  paused: TaskFlow keeps calling ``WorkflowRun.collect(paused=True)``.
- Resume (``request_resume``, only after the UI sent ``reconciled: true``):
  ``PauseCoordinator.resume_observed`` derives the reconciliation from live
  facts; the flag is cleared only when the coordinator reports unpaused.
  Nothing held is replayed.

Free-work runs (explicit minimal mode): the run's execution unit is the worker
OMP process working in the project checkout. ``worktree`` is that checkout at
its HEAD when the run starts and ``shell`` is ``FreeWorkProcess`` (the worker
OMP's PID, cwd = checkout), so ``bind_run``/``resume_observed`` apply the same
file/tool/process/task/approval reconciliation. There is no host run, so the
60 s review does not apply: the host shell stays user-owned and the lifecycle
tick reports ``user_owner_or_control_hold``.

CW-19 U2: ``bind_production`` gets ``RecoveryCoordinator(SqliteRecoveryPort)``
(fresh Task metadata plus this controller's live run facts), the run's
termination observation (``RunObservation`` from the bound run's collected
record and shell lifecycle, a monotonic sequence per run), an exact-reference
ordinary stop (pidfd + start-tick recheck; the host shell gets HUP/TERM/CONT as
in C-D63, an OMP TERM), a bounded drain and an exact-reference force (only
after CW-13 authorizes it). ``shutdown_bound`` runs the coordinator's
at-most-once shutdown fence for a user-confirmed full shutdown. The retry
limit and the next-revision rule stay TaskFlow's (CW-13, R7).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from uuid import UUID, uuid5

from workbench.app.lifecycle import (
    ControlState, LifecycleCoordinator, LifecycleHeld, LifecycleJournal, LifecycleRecord, PeerRef,
)
from workbench.app.production import bind_production
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected, G3BridgeServer, MailboxMessage, TaskMailbox
from workbench.observation.worker_review import (
    ActiveRunRef, ReviewRequest, SerializedReviewAdmission, WorkerReviewScheduler,
)
from workbench.observation.workflow_binding import WorkflowObservationBinding
from workbench.policy.pause_automation.controller import PauseCoordinator, PausePolicyError, PauseStatus
from workbench.policy.recovery_manager import Artifact, RecoveryCoordinator, RunIdentity, RunObservation
from workbench.policy.recovery_manager.port_sqlite import RunFacts, SqliteRecoveryPort
from workbench.runtime.process_evidence import LinuxProcessProbe, ProcessRef
from workbench.storage.log_raw import MetadataAdmissionGate
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_persistent.adapter import PersistentShell
from workbench.workflow.run import _canonical

REVIEW_INTERVAL_SECONDS = 60  # C-AC-12
TICK_INTERVAL_SECONDS = 1.0
PAUSE_STORE_RETRY_SECONDS = 5.0  # CW-19 R4: a pause/resume that could not be stored is retried
REVIEW_DELIVERY_TIMEOUT = 30.0
INTERRUPTION = {"none": "none", "not_needed": "not_needed", "requested": "requested",
                "stop_observed": "confirmed", "request_failed": "request_failed", "unknown": "unknown"}
REVIEW_INSTRUCTION = ("Workbench periodic review (automatic, every 60 s while the experiment runs). Judge from "
                      "these public run facts whether the run looks healthy. Do not stop, restart or change "
                      "anything. Use to_manager only when the manager has to act.")


def _iso(wall: float | None) -> str | None:
    if wall is None:
        return None
    return datetime.fromtimestamp(wall, timezone.utc).isoformat(timespec="seconds")


def approval_hash(repository: Any, task_id: str, revision: int) -> str:
    """The run authority hash exactly as ``TaskWorkflow.start`` computes it."""
    task = repository.get_task_spec(task_id, revision)
    approvals = [d for d in repository.get_decisions(task_id, revision) if d["kind"] == "scope_approved"]
    if not approvals:
        raise LifecycleHeld("the run has no persisted scope approval")
    return sha256(_canonical({"task": task, "approval": approvals[-1]})).hexdigest()


def stored_message(repository: Any, message_id: str) -> MailboxMessage:
    """A persisted mailbox message as the identity handle ``bind_production`` binds to."""
    row = repository.get_message(message_id)
    content = row["content"]
    return MailboxMessage(
        message_id=row["message_id"], task_id=row["task_id"], revision=row["revision"],
        revision_id=content["revision_id"], run_id=row["run_id"],
        sender_role=ActorRole(content["sender_role"]), target_role=ActorRole(content["target_role"]),
        session_id=content["target_session_id"], session_generation=content["target_session_generation"],
        kind=MessageKind(content["kind"]),
        payload_json=json.dumps(content.get("payload") or {}, sort_keys=True, separators=(",", ":")),
        in_reply_to_message_id=content.get("in_reply_to_message_id"))


def active_run_ref(repository: Any, message_id: str) -> ActiveRunRef:
    """The persisted TASK identity (what ``WorkflowObservationBinding.resolve_run`` reads)."""
    message = stored_message(repository, message_id)
    if (message.sender_role is not ActorRole.MANAGER or message.target_role is not ActorRole.WORKER
            or message.kind is not MessageKind.TASK
            or message.revision_id != str(uuid5(UUID(message.task_id), f"task-spec-revision:{message.revision}"))):
        raise LifecycleHeld("persisted TASK identity or direction mismatch")
    return ActiveRunRef(message.task_id, message.revision_id, message.revision, message.run_id,
                        message.session_id, message.session_generation)


class ThreadLocalRepository:
    """``TaskRepository`` per thread on one database (SQLite connections are thread-bound).

    The pause/review ports read the run's repository from the tick thread, a
    transition thread and the binding caller's thread.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._opened: list[tuple[int, TaskRepository]] = []

    def _repository(self) -> TaskRepository:
        repository = getattr(self._local, "repository", None)
        if repository is None:
            repository = TaskRepository(self.path)
            self._local.repository = repository
            with self._lock:
                self._opened.append((threading.get_ident(), repository))
        return repository

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._repository(), name)

    def release_thread(self) -> None:
        repository = getattr(self._local, "repository", None)
        if repository is not None:
            self._local.repository = None
            with self._lock:
                self._opened = [item for item in self._opened if item[1] is not repository]
            repository.close()

    def close(self) -> None:
        """Close this thread's connection; others belong to threads that have ended (or close their own)."""
        self.release_thread()
        with self._lock:
            self._opened.clear()


class PaneShell(PersistentShell):
    """The host pane's ``PersistentShell`` under the pane's ``io_lock`` (CW-15 control port).

    ``bind_production`` binds a ``PersistentShell``; the backend loop pumps the
    same shell, so every call here takes the pane lock (as ``HostShellPort``).
    It never starts, closes or writes to the shell.
    """

    def __init__(self, pane: Any):  # noqa: D107 - deliberately no PersistentShell.__init__ (no new shell)
        self._pane = pane

    def poll(self, timeout: float = 0) -> dict[str, Any]:
        with self._pane.io_lock:
            self._pane.state = self._pane.shell.poll(0)
            return self._pane.state

    def snapshot(self) -> dict[str, Any]:
        with self._pane.io_lock:
            return self._pane.shell.snapshot()

    def request_takeover(self) -> dict[str, Any]:
        with self._pane.io_lock:
            self._pane.state = self._pane.shell.request_takeover()
            return self._pane.state

    def confirm_takeover(self) -> dict[str, Any]:
        with self._pane.io_lock:
            self._pane.state = self._pane.shell.confirm_takeover()
            return self._pane.state

    def claim_manager(self) -> dict[str, Any]:
        with self._pane.io_lock:
            self._pane.state = self._pane.shell.claim_manager()
            return self._pane.state


@dataclass(frozen=True)
class FreeWorkProcess:
    """A free-work run's execution unit for the CW-12 process check: the worker OMP.

    ``snapshot`` reports its PID as the run's process (its cwd must be the
    project checkout) with no host child: free work has no host-shell run.
    """

    worker_pid: int

    def snapshot(self) -> dict[str, Any]:
        return {"parent_pid": self.worker_pid, "lifecycle": {"lifetime": "ended", "child_pid": None}}


@dataclass
class _Bound:
    kind: str
    task_id: str
    revision: int
    run_id: str
    active: ActiveRunRef
    view: Any  # the run object PauseCoordinator binds (thread-safe repository)
    workflow_run: Any | None
    coordinator: LifecycleCoordinator | None = None
    error: str | None = None
    ended: bool = False
    pause_bound: bool = False  # PauseCoordinator.bind_run succeeded for this run
    policy_paused: bool = False  # PauseCoordinator.pause ran for this run (resume must reconcile)


def _signal_exact(ref: ProcessRef, signums: tuple[int, ...]) -> bool:
    """Signal the exact process (pid + start ticks, held by a pidfd); False when it already ended."""
    try:
        descriptor = os.pidfd_open(ref.pid)
    except ProcessLookupError:
        return False
    try:
        if LinuxProcessProbe.start_ticks(ref.pid) != ref.start_ticks:
            return False  # the pid is another process now: never signalled
        for signum in signums:
            signal.pidfd_send_signal(descriptor, signum)
        return True
    except (ProcessLookupError, FileNotFoundError):
        return False
    finally:
        os.close(descriptor)


class AutomationController:
    """The backend's automation state, loop and pause/resume; see the module docstring."""

    def __init__(self, *, bridge: G3BridgeServer, database: str | Path, journal: LifecycleJournal, raw: Any,
                 shell_pane: Callable[[], Any], project_dir: str | Path, artifacts_root: str | Path,
                 boot_marker: Callable[[], str | None], clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, interval_seconds: int = REVIEW_INTERVAL_SECONDS,
                 tick_interval: float = TICK_INTERVAL_SECONDS, request_timeout: float = 3.0,
                 stop_timeout: float = 2.0, review_timeout: float = REVIEW_DELIVERY_TIMEOUT,
                 retry_limit: int = 3, log: Callable[[str], None] | None = None,
                 pause_store: Any | None = None, hold: Callable[[], str | None] = lambda: None,
                 model_ok: Callable[[], bool] = lambda: True,
                 metadata: Callable[[str, BaseException | None], None] | None = None):
        self.bridge = bridge
        # CW-19 R4: the user's pause is durable (a backend crash never lifts it); ``hold`` is the backend's
        # admission hold for the 60 s review (C-D71 (3)/(4), C-AC-28).
        self._pause_store = pause_store
        self._metadata = metadata
        # a pause/resume that could not be stored: shown (``persistence_error`` + metadata fault) and retried
        self._save_lock = threading.Lock()
        self._pause_desired: bool | None = None
        self._pause_store_error: str | None = None
        self._pause_retry_at = 0.0
        self._hold = hold
        self._model_ok = model_ok
        # CW-19 U2: the CW-13 port's own attempt store, the termination sequence and requested stops per run
        self.recovery_db = Path(artifacts_root).parent / "recovery.sqlite3"
        self._observation_sequence: dict[str, int] = {}
        self._stop_requested: set[str] = set()
        self._request_timeout = request_timeout
        self.repository = ThreadLocalRepository(database)
        self.journal = journal
        self.raw = raw
        self._shell_pane = shell_pane
        self.project_dir = Path(project_dir)
        self.artifacts_root = Path(artifacts_root)
        self._boot_marker = boot_marker
        self._clock = clock
        self._wall = wall
        self._tick_interval = tick_interval
        self._review_timeout = review_timeout
        self._retry_limit = retry_limit
        self._log = log or (lambda _message: None)
        self._lock = threading.RLock()
        self._paused = False
        self.restored_pause = False
        if pause_store is not None:
            try:
                self._paused = self.restored_pause = pause_store.load() is True
            except Exception:
                self._paused = self.restored_pause = True  # unknown: fail closed, the user resumes
        self._transition: str | None = None
        self._transitions: list[str] = []
        self._transition_cv = threading.Condition(self._lock)
        self._transition_thread: threading.Thread | None = None
        self._bound: _Bound | None = None
        # R6: runs whose close arrived before (or while) they were bound; a late bind never binds them.
        self._ended_runs: dict[str, None] = {}
        self._closing = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()
        self._last_tick: dict[str, Any] | None = None
        self._last_review: Mapping[str, Any] | None = None
        self._last_exit: Mapping[str, Any] | None = None
        self._last_resume: dict[str, Any] | None = None
        self._pause_status: PauseStatus | None = None
        self._pause_error: str | None = None
        self.metadata = MetadataAdmissionGate()
        self.admission = SerializedReviewAdmission(automation_state=self._base_state())
        self.pause_policy = PauseCoordinator(bridge=bridge, admission=self.admission,
                                             automation_state=self._base_state,
                                             request_timeout=request_timeout, stop_timeout=stop_timeout)
        self.review = WorkerReviewScheduler(
            admission=self.admission, automation_state=self.pause_policy.automation_state,
            active_run=self._review_run, worker_state=self._worker_state,
            collect_non_model=self._collect_non_model, dispatch_review=self._dispatch_review,
            user_priority=lambda _run: False, on_exit=self._on_exit, clock=clock,
            interval_seconds=interval_seconds)
        self._interval = interval_seconds

    # -- the shared paused source (TaskFlow, HandoffService, AutomationState) -------------
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def _base_state(self) -> dict[str, Any]:
        return {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": self.paused(), "cancelled": False,
            "metadataHealthy": self.metadata.automatic_runs_allowed is True, "approvalValid": True}}

    def _save_pause(self, paused: bool) -> bool:
        """Store the user's pause/resume; a failure is shown as a fault and retried (the in-memory flag stays)."""
        if self._pause_store is None:
            return True
        with self._save_lock:
            saved, was = self._store_pause_locked(paused)
        self._pause_store_reported(paused, saved, was)
        return saved

    def _store_pause_locked(self, paused: bool) -> tuple[bool, str | None]:
        """Write ``paused`` with ``_save_lock`` held; (saved, the previous store error)."""
        self._pause_desired = paused
        try:
            saved = bool(self._pause_store.save(paused))
        except Exception:
            saved = False
        was = self._pause_store_error
        if saved:
            self._pause_store_error = None
        else:
            self._pause_store_error = "pause_not_stored" if paused else "resume_not_stored"
            self._pause_retry_at = self._clock() + PAUSE_STORE_RETRY_SECONDS
        return saved, was

    def _pause_store_reported(self, paused: bool, saved: bool, was: str | None) -> None:
        if not saved:
            if was is None:
                self._log(f"automation: the {'pause' if paused else 'resume'} could not be stored durably; "
                          "shown as a metadata fault and retried")
            self._report_metadata(OSError(f"automation.json not stored ({'pause' if paused else 'resume'})"))
        elif was is not None:
            self._log("automation: the pause state is stored durably again")
            self._report_metadata(None)

    def _report_metadata(self, error: BaseException | None) -> None:
        if self._metadata is None:
            return
        try:
            self._metadata("automation_pause", error)
        except Exception as exc:
            self._log(f"automation: metadata report failed ({type(exc).__name__})")

    def retry_pause_store(self) -> None:
        """Retry a pause/resume that could not be stored (from the loop)."""
        if self._pause_store is None:
            return
        # review-02 P3-1: read and write under one lock, so a pause/resume stored meanwhile is never overwritten
        # by the value this retry read earlier.
        with self._save_lock:
            desired = self._pause_desired
            if self._pause_store_error is None or desired is None or self._clock() < self._pause_retry_at:
                return
            saved, was = self._store_pause_locked(bool(desired))
        self._pause_store_reported(bool(desired), saved, was)

    def _held(self) -> str | None:
        try:
            reason = self._hold()
        except Exception:
            return "hold_unknown"
        return reason if isinstance(reason, str) and reason else None

    def raw_log_status(self) -> dict[str, Any] | None:
        """CW-19 (C-AC-24): the bound experiment run's raw-log accounting, if any."""
        with self._lock:
            bound = self._bound
        run = None if bound is None else bound.workflow_run
        record = getattr(run, "_record", None)
        status = record.get("raw_log_status") if isinstance(record, dict) else None
        return None if not isinstance(status, dict) else {"run_id": bound.run_id, **status}

    def model_changed(self, ok: bool) -> None:
        """C-D71 (4): the worker model's passive state for the bound run's durable record (no probe request)."""
        with self._lock:
            bound = self._bound
        coordinator = None if bound is None else bound.coordinator
        if coordinator is None:
            return
        at = _iso(self._wall()) or "unknown"
        try:
            if ok:
                coordinator.recheck_model(at)
            else:
                coordinator.model_failed(at)
        except Exception as exc:
            self._log(f"automation: model state not recorded ({type(exc).__name__})")

    # -- run binding (called by TaskFlow on its runner / outbox threads) ------------------
    def experiment_started(self, workflow_run: Any) -> None:
        """Bind a just started experiment ``WorkflowRun`` (runner thread: its repository is usable here)."""
        try:
            active = WorkflowObservationBinding.resolve_run(workflow_run)
        except Exception as exc:  # the run continues; automation for it is held
            self._bind_failed("experiment", workflow_run.task_id, workflow_run.revision, workflow_run.run_id,
                              f"run_identity_unavailable:{type(exc).__name__}")
            return
        view = SimpleNamespace(repository=self.repository, task_id=workflow_run.task_id,
                               revision=workflow_run.revision, run_id=workflow_run.run_id,
                               approval_hash=workflow_run.approval_hash, record_dir=workflow_run.record_dir,
                               worktree=workflow_run.worktree, shell=workflow_run.shell)
        self._bind(_Bound("experiment", workflow_run.task_id, workflow_run.revision, workflow_run.run_id, active,
                          view, workflow_run), workflow_run.task_message_id)

    def work_started(self, task_id: str, revision: int, run_id: str, message_id: str) -> None:
        """Bind a free-work run once its TASK message exists (outbox lane thread)."""
        try:
            active = active_run_ref(self.repository, message_id)
            if (active.task_id, active.revision, active.run_id) != (task_id, revision, run_id):
                raise LifecycleHeld("TASK message is not this run's")
            record_dir = self.artifacts_root / run_id
            record_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
            checkout = self.project_dir.resolve(strict=True)
            head = subprocess.run(("git", "-C", str(checkout), "rev-parse", "--verify", "HEAD"),
                                  capture_output=True, text=True, timeout=5, check=False)
            worker = self.bridge.peer(ActorRole.WORKER)
            view = SimpleNamespace(repository=self.repository, task_id=task_id, revision=revision, run_id=run_id,
                                   approval_hash=approval_hash(self.repository, task_id, revision),
                                   record_dir=record_dir,
                                   worktree=SimpleNamespace(path=checkout, commit=head.stdout.strip() or None),
                                   shell=FreeWorkProcess(worker.pid))
        except Exception as exc:
            self._bind_failed("work", task_id, revision, run_id, f"binding_unavailable:{type(exc).__name__}")
            return
        self._bind(_Bound("work", task_id, revision, run_id, active, view, None), message_id)

    def run_ended(self, run_id: str) -> None:
        """The run is closed (completed/failed/done): automation for it ends; nothing is replayed."""
        with self._lock:
            self._ended_runs[run_id] = None
            while len(self._ended_runs) > 64:
                self._ended_runs.pop(next(iter(self._ended_runs)))
            bound = self._bound
            if bound is None or bound.run_id != run_id:
                return
            bound.ended = True
            # The OMPs were paused for this run: keep it so the user's resume still goes through the
            # coordinator (which refuses a run that is no longer current; nothing is resumed silently).
            keep = bound.policy_paused and self._paused
            if not keep:
                self._bound = None
        if not keep:
            try:
                self.admission.set_active_run(None)
            except Exception:
                pass
        self._log(f"automation: run {run_id} ended; review stopped"
                  + ("; the paused run stays bound until a reconciled resume" if keep else ""))
        self._publish()

    def _bind_failed(self, kind: str, task_id: str, revision: int, run_id: str, error: str) -> None:
        with self._lock:
            if run_id in self._ended_runs:
                return  # R6: a closed run is never bound (also not as a failed binding)
            self._bound = _Bound(kind, task_id, revision, run_id, None, None, None, None, error)  # type: ignore[arg-type]
        self._log(f"automation: run {run_id} not bound ({error}); automatic review held")
        self._publish()

    def _ended_before_bind(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._ended_runs

    def _bind(self, bound: _Bound, message_id: str) -> None:
        if self._ended_before_bind(bound.run_id):  # R6: e.g. a quick cancel before work_started's bind
            self._log(f"automation: run {bound.run_id} already ended; not bound")
            return
        try:
            self.admission.set_active_run(bound.active)
            self.pause_policy.bind_run(bound.view)
            bound.pause_bound = True
            bound.coordinator = self._bind_lifecycle(bound, message_id)
        except Exception as exc:
            bound.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        with self._lock:
            ended = bound.run_id in self._ended_runs  # its close arrived while binding: stay unbound
            if not ended:
                self._bound = bound
                self._pause_status, self._pause_error = None, None
                self._last_review, self._last_tick, self._last_exit = None, None, None
                pause_now = self._paused  # started while paused (e.g. a work TASK created after the pause)
                if pause_now and bound.pause_bound:
                    self._enqueue("pause")  # the coordinator pauses this run too (manager abort request)
        if ended:
            try:
                self.admission.set_active_run(None)
            except Exception:
                pass
            self._log(f"automation: run {bound.run_id} ended while binding; not bound")
            self._publish()
            return
        self._log(f"automation: {bound.kind} run {bound.run_id} bound"
                  + (f" with error {bound.error}" if bound.error else ""))
        self._publish()

    def _bind_lifecycle(self, bound: _Bound, message_id: str) -> LifecycleCoordinator:
        pane = self._shell_pane()
        if pane is None or pane.exited():
            raise LifecycleHeld("host shell is not running")
        shell = PaneShell(pane)
        state = shell.poll(0)
        probe = LinuxProcessProbe()
        peers = {}
        for role in ("manager", "worker"):
            peer = self.bridge.peer(role)
            ticks = probe.start_ticks(peer.pid)
            if not ticks:
                raise LifecycleHeld(f"{role} OMP process identity unknown")
            peers[role] = PeerRef(role, peer.session_id, peer.generation, ProcessRef(role, peer.pid, ticks, 1))
        shell_ticks = probe.start_ticks(state["parent_pid"])
        if not shell_ticks:
            raise LifecycleHeld("host shell process identity unknown")
        record = LifecycleRecord(
            task_id=bound.task_id, revision=bound.revision, run_id=bound.run_id,
            approval_hash=bound.view.approval_hash, generation=1,
            boot_marker=self._boot_marker() or "unknown", boot_confirmed_marker=None,
            manager=peers["manager"], worker=peers["worker"],
            shell=ProcessRef("shell", state["parent_pid"], shell_ticks, state["owner_epoch"]),
            run_targets=(), control=ControlState(state["input_owner"], state["owner_epoch"], state["parent_mode"],
                                                 state["takeover_requested"], state["takeover_confirmed"]))
        previous = self.journal.read()
        # One durable record per backend data dir: a new run is the next generation (CAS).
        self.journal.commit(previous, record if previous is None
                            else replace(record, generation=previous.generation + 1))
        run_id = bound.run_id
        return bind_production(
            journal=self.journal, bridge=self.bridge,
            mailbox=TaskMailbox(self.repository, self.bridge),  # type: ignore[arg-type]
            message=stored_message(self.repository, message_id), shell=shell,
            pause=self.pause_policy, review=self.review,
            recovery=RecoveryCoordinator(SqliteRecoveryPort(self.repository.path, self.recovery_db, self._run_facts)),
            raw=self.raw, metadata=self.metadata, model=SimpleNamespace(check=lambda: self._model_ok() is True),
            termination=lambda: self._observation(run_id), boot_marker=lambda: self._boot_marker() or "unknown",
            authority=lambda: self._authority(run_id), normal_stop=lambda ref: self._normal_stop(run_id, ref),
            drain=self._drain, force=lambda ref: self._force(run_id, ref))

    # -- CW-19 U2: termination observation, CW-13 run facts and exact-reference stop callbacks ----------
    def _observation(self, run_id: str) -> RunObservation | None:
        """The bound run's managed-execution state now (CW-10); None when it is not the bound run."""
        with self._lock:
            bound = self._bound
            if bound is None or bound.run_id != run_id or bound.active is None:
                return None
            sequence = self._observation_sequence.get(run_id, 0) + 1
            self._observation_sequence[run_id] = sequence
            stop_requested = run_id in self._stop_requested
        identity = RunIdentity(bound.task_id, bound.revision, bound.run_id)
        workflow_run = bound.workflow_run
        if workflow_run is None:  # free work: the worker OMP is the execution unit
            pid = getattr(getattr(bound.view, "shell", None), "worker_pid", None)
            alive = None
            if type(pid) is int:
                try:
                    alive = LinuxProcessProbe.start_ticks(pid) is not None
                except OSError:
                    alive = False
            state = "running" if alive else "unknown"
            return RunObservation(identity, sequence, state, None, False, state, None, False, False, stop_requested)
        record = dict(getattr(workflow_run, "_record", {}) or {})
        try:
            life = (workflow_run.shell.snapshot() or {}).get("lifecycle") or {}
        except Exception:
            life = {}
        shell_state = {"exited": "exited", "unknown": "unknown"}.get(record.get("shell_state"), "running")
        lifetime = life.get("lifetime") if life.get("lifetime") in ("running", "ended") else "unknown"
        status = record.get("exit_status")
        return RunObservation(
            identity, sequence, shell_state, status if type(status) is int else None,
            shell_state == "exited" and record.get("exit_confirmed") is True, lifetime,
            True if lifetime == "ended" else False if lifetime == "running" else None,
            bool(life.get("input_returned")), bool(life.get("control_returned")), stop_requested)

    def _run_facts(self, identity: RunIdentity) -> RunFacts:
        """Live facts for the CW-13 port; raises for anything but the bound experiment run (fail closed)."""
        with self._lock:
            bound = self._bound
        if bound is None or bound.run_id != identity.run_id or bound.workflow_run is None:
            raise LifecycleHeld("no live facts for this run")
        observation = self._observation(identity.run_id)
        if observation is None:
            raise LifecycleHeld("run observation unavailable")
        run = bound.workflow_run
        record = dict(getattr(run, "_record", {}) or {})
        try:
            owner = (self._shell_pane().state or {}).get("input_owner")
        except Exception:
            owner = None
        raw_log = result = None
        collected = record.get("raw_log_collected")
        if isinstance(collected, dict) and collected.get("sha256"):
            try:
                content = run._read_raw() if hasattr(run, "_read_raw") else Path(run.raw_log).read_bytes()
                raw_log = Artifact("raw_log", str(run.raw_log), content, collected["sha256"])
            except (OSError, ValueError):
                raw_log = Artifact("raw_log", str(run.raw_log), None, None, "unreadable")
        collected = record.get("result_collected")
        if isinstance(collected, dict) and collected.get("sha256") and collected.get("path"):
            try:
                result = Artifact("result_file", str(collected["path"]), Path(collected["path"]).read_bytes(),
                                  collected["sha256"])
            except (OSError, ValueError):
                result = Artifact("result_file", str(collected["path"]), None, None, "unreadable")
        judgment = record.get("worker_judgment")
        return RunFacts(
            observation=observation, automation_state=self._base_state(),
            terminal_owner={"manager": "workbench", "user": "user"}.get(owner, "unknown"),
            metadata_healthy=self.metadata.automatic_runs_allowed is True,
            worktree_root=str(Path(run.worktree.path)), raw_log_path=str(run.raw_log), raw_log=raw_log,
            result=result, worker_report=judgment if isinstance(judgment, dict) else None,
            worker_report_id=(judgment or {}).get("report_id") if isinstance(judgment, dict) else None,
            source_commit=str(getattr(run.worktree, "commit", "") or ""))

    def _normal_stop(self, run_id: str, ref: ProcessRef) -> None:
        """CW-13-authorized ordinary stop of one exact process (RecoveryStopAdapter checked the decision)."""
        signums = ((signal.SIGHUP, signal.SIGTERM, signal.SIGCONT) if ref.role == "shell"
                   else (signal.SIGTERM, signal.SIGCONT))
        with self._lock:
            self._stop_requested.add(run_id)
        _signal_exact(ref, signums)
        self._log(f"automation: ordinary stop sent to {ref.role} pid {ref.pid} (run {run_id})")

    def _force(self, run_id: str, ref: ProcessRef) -> None:
        """CW-13-authorized force of one exact owned target only."""
        if not _signal_exact(ref, (signal.SIGKILL,)):
            raise LifecycleHeld("exact force target is gone or changed")
        self._log(f"automation: force (SIGKILL) sent to {ref.role} pid {ref.pid} (run {run_id})")

    @staticmethod
    def _drain(timeout: float) -> None:
        time.sleep(min(max(0.0, timeout), 0.05))

    def shutdown_bound(self, timeout: float = 2.0) -> dict[str, Any] | None:
        """C-AC-22: the bound run's at-most-once shutdown fence for a user-confirmed full shutdown."""
        with self._lock:
            bound = self._bound
        coordinator = None if bound is None else bound.coordinator
        if coordinator is None:
            return None
        try:
            result = coordinator.shutdown(user_confirmed=True, timeout=timeout)
        except Exception as exc:
            return {"run_id": bound.run_id, "confirmed": False, "complete": False,
                    "survivors": [], "problems": [f"shutdown_error:{type(exc).__name__}"]}
        return {"run_id": bound.run_id, "confirmed": result.confirmed, "complete": result.complete,
                "survivors": [{"role": ref.role, "pid": ref.pid, "start_ticks": ref.start_ticks}
                              for ref in result.survivors], "problems": list(result.problems)}

    def _authority(self, run_id: str) -> bool:
        with self._lock:
            bound = self._bound
            return (not self._closing and bound is not None and bound.run_id == run_id and not bound.ended
                    and bound.error is None)

    # -- the loop ------------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, name="automation-tick", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        try:
            while not self._stop.wait(self._tick_interval):
                try:
                    self.retry_pause_store()
                    self.tick()
                except Exception as exc:  # never let the loop die silently
                    self._log(f"automation tick failed: {type(exc).__name__}: {exc}")
        finally:
            self.repository.release_thread()

    def tick(self) -> dict[str, Any]:
        """One loop iteration: ``LifecycleCoordinator.tick`` for the bound run, then the published state."""
        with self._tick_lock:
            with self._lock:
                bound = self._bound
            result: dict[str, Any]
            hold = self._held()
            if bound is None:
                result = {"outcome": "idle", "problems": []}
            elif hold is not None:  # CW-19: no 60 s review (automatic model work) while held
                result = {"outcome": "held", "problems": [hold]}
            elif bound.coordinator is None:
                result = {"outcome": "held", "problems": [bound.error or "run_not_bound"]}
            else:
                try:
                    outcome, value = bound.coordinator.tick()
                except LifecycleHeld as exc:
                    outcome, value = "held", None
                    result = {"outcome": "held", "problems": [str(exc)[:200]]}
                except Exception as exc:
                    outcome, value = "held", None
                    result = {"outcome": "held", "problems": [f"tick_error:{type(exc).__name__}"]}
                else:
                    result = {"outcome": outcome, "problems": []}
                if outcome == "admitted" and value is not None:
                    with self._lock:
                        if self._bound is bound:
                            self._last_review = {
                                "status": value.status, "reason": value.reason,
                                "review_count": value.review_count, "last_review_at": value.last_review_at,
                                "next_due_at": value.next_due_at, "pending": value.pending,
                                "coalesced_count": value.coalesced_count}
                elif outcome != "admitted" and not result["problems"]:
                    try:
                        _, problems = bound.coordinator.automatic_admission()
                        result["problems"] = [p for p in problems if p != "command_already_attempted_no_replay"]
                    except Exception as exc:
                        result["problems"] = [f"reconcile_unavailable:{type(exc).__name__}"]
            result["at"] = self._wall()
            with self._lock:
                if self._bound is bound:
                    self._last_tick = result
            self._publish()
            return dict(result)

    # -- WorkerReviewScheduler ports -----------------------------------------------------------
    def _review_run(self) -> ActiveRunRef | None:
        with self._lock:
            bound = self._bound
        if bound is None or bound.kind != "experiment" or bound.ended:
            return None  # free work has no host run to review (module docstring)
        return bound.active

    def _worker_state(self, _run: ActiveRunRef) -> object:
        return self.bridge.probe(ActorRole.WORKER, timeout=2.0)

    def _collect_non_model(self, _run: ActiveRunRef) -> dict[str, Any]:
        """Public host facts from the run's latest collected record (the runner keeps collecting)."""
        with self._lock:
            bound = self._bound
        workflow_run = None if bound is None else bound.workflow_run
        if workflow_run is None:
            return {"phase": "unknown", "unknowns": ["no_host_run"]}
        record = dict(workflow_run._record)
        try:
            life = (workflow_run.shell.snapshot() or {}).get("lifecycle") or {}
        except Exception:
            life = {}
        state = record.get("shell_state")
        facts: dict[str, Any] = {
            "phase": {"sent": "starting", "running": "running", "exited": "exited",
                      "unknown": "unknown"}.get(state, "starting"),
            "exit_confirmed": state == "exited" and record.get("exit_confirmed") is True,
            "process_alive": life.get("lifetime") not in (None, "ended"),
            "unknowns": [item for item in (record.get("unknowns") or []) if isinstance(item, str)]}
        status = record.get("exit_status")
        if status is None or type(status) is int:
            facts["exit_status"] = status
        try:
            info = os.stat(workflow_run.raw_log)
            facts["output_bytes"] = info.st_size
            facts["last_output_age_seconds"] = max(0.0, self._wall() - info.st_mtime)
        except OSError:
            facts["unknowns"].append("raw_log_unavailable")
        return facts

    def _dispatch_review(self, request: ReviewRequest) -> dict[str, Any]:
        with self._lock:
            bound = self._bound
        if (bound is None or bound.ended or bound.active is None or self.paused() or self._held() is not None
                or bound.active.identity != request.run.identity):
            return {"status": "held"}
        mailbox = TaskMailbox(self.repository, self.bridge)  # type: ignore[arg-type]
        message = mailbox.create_message(
            bound.task_id, bound.revision, bound.run_id, ActorRole.MANAGER, ActorRole.WORKER,
            MessageKind.QUESTION, {"stage": "periodic_review", "instruction": REVIEW_INSTRUCTION,
                                   "facts": json.loads(json.dumps(dict(request.facts), default=str)),
                                   "coalesced_count": request.coalesced_count})
        status = self.pause_policy.status()
        binding = status.binding
        expected = None if binding is None else {
            ActorRole.MANAGER: (binding.manager_session_id, binding.manager_generation),
            ActorRole.WORKER: (binding.worker_session_id, binding.worker_generation)}
        # The G3 final check refuses the frame once the user asked for a pause.
        open_now = lambda: not self.paused()  # noqa: E731
        token = SimpleNamespace(current=open_now, claim=open_now)
        receipt = mailbox.deliver(message, timeout=self._review_timeout, expected_peers=expected,
                                  authority_token=token)
        self._log(f"automation: periodic review {message.message_id} -> {receipt.status.value}")
        return {"status": receipt.status.value}

    def _on_exit(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            self._last_exit = dict(event) | {"observed_at": _iso(self._wall())}
        self._log(f"automation: exit observed for run {event.get('run_id')} (status {event.get('exit_status')})")

    # -- pause / resume (ui_v1 pause, resume reconciled=true) ------------------------------------
    def request_pause(self) -> dict[str, Any]:
        with self._lock:
            if not self._paused:
                self._paused = True  # every reader holds new automatic work from now on
                self._last_resume = None
                self._enqueue("pause")
        self._save_pause(True)
        self._log("automation: pause requested by the user")
        self._publish()
        return self.status()

    def request_resume(self) -> dict[str, Any]:
        """Only after the UI's ``reconciled: true``; the coordinator still reconciles live facts."""
        with self._lock:
            if self._paused and "resume" not in self._transitions:
                self._enqueue("resume")
        self._log("automation: resume requested by the user")
        self._publish()
        return self.status()

    def _enqueue(self, kind: str) -> None:
        with self._lock:
            self._transitions.append(kind)
            if self._transition_thread is None:  # cleared under this lock when the thread leaves
                self._transition_thread = threading.Thread(target=self._run_transitions,
                                                           name="automation-transition", daemon=True)
                self._transition_thread.start()

    def _run_transitions(self) -> None:
        try:
            while True:
                with self._lock:
                    if not self._transitions or self._closing:
                        self._transitions.clear()
                        self._transition = None
                        self._transition_thread = None
                        self._transition_cv.notify_all()
                        return
                    kind = self._transitions.pop(0)
                    self._transition = "pausing" if kind == "pause" else "resuming"
                    bound = self._bound
                self._publish()
                try:
                    self._pause(bound) if kind == "pause" else self._resume(bound)
                except Exception as exc:
                    self._log(f"automation {kind} failed: {type(exc).__name__}: {exc}")
                with self._lock:
                    self._transition = None
                self._publish()
        finally:
            self.repository.release_thread()

    def _pause(self, bound: _Bound | None) -> None:
        if bound is None or not bound.pause_bound:
            with self._lock:
                self._pause_error = None if bound is None else "run_not_bound"
            return  # no bound run: no automatic model work in flight; new work is held by the flag
        error = None
        try:
            self.pause_policy.pause()
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        try:
            status = self.pause_policy.status()
        except Exception as exc:
            status, error = None, error or f"status_unavailable:{type(exc).__name__}"
        with self._lock:
            # Resume must reconcile whenever the coordinator recorded a pause for this run.
            bound.policy_paused = (status is not None and status.pause_id is not None
                                   and status.binding is not None and status.binding.run_id == bound.run_id)
            self._pause_status = status
            self._pause_error = error
        self._log("automation: paused" + (f" (pause policy: {error})" if error else
                                          f" (interruption {INTERRUPTION.get(status.abort_status)})"))

    def _bound_run_current(self, bound: _Bound) -> bool:
        """The run bound at pause time is still its Task's current run (unknown counts as current)."""
        if bound.ended:
            return False
        try:
            current = self.repository.get_current_run(bound.task_id)
        except Exception:
            return True  # unknown: the coordinator reconciles it (fail closed)
        return current is not None and current.get("run_id") == bound.run_id

    def _resume_peers_after_close(self) -> tuple[bool, str]:
        """F3: the paused run closed; lift the OMPs' pause (no run is left to reconcile against).

        A peer that is not connected or was not paused has nothing to lift. Any
        other answer (an abort still pending, a timeout) keeps Workbench paused.
        """
        answers = []
        for role in (ActorRole.MANAGER, ActorRole.WORKER):
            try:
                response = self.bridge.request(role, {"kind": "resume", "reconciled": True}, self._request_timeout)
                status = response.get("status") if isinstance(response, Mapping) else None
            except BridgeDisconnected:
                status = "not_connected"
            except Exception as exc:
                status = f"unknown:{type(exc).__name__}"
            answers.append(f"{role.value}={status}")
            if status not in {"resumed", "already_resumed", "not_connected"}:
                return False, ", ".join(answers)
        return True, ", ".join(answers)

    def _resume(self, bound: _Bound | None) -> None:
        outcome, reason, unbound = "resumed", None, False
        if bound is not None and bound.policy_paused and not self._bound_run_current(bound):
            # F3: the run bound at pause time closed while paused: reconcile to idle (no run).
            lifted, answers = self._resume_peers_after_close()
            if lifted:
                reason = f"run_closed_while_paused: reconciled to idle, no run ({answers})"
                with self._lock:
                    bound.ended = True
            else:
                outcome, reason = "refused", f"run_closed_while_paused: OMP pause not lifted ({answers})"
        elif bound is not None and bound.policy_paused:
            try:
                status = self.pause_policy.resume_observed(bound.view, user_resume=True)
            except Exception as exc:
                outcome, reason, status = "refused", f"{type(exc).__name__}: {str(exc)[:200]}", None
            else:
                if status.paused:
                    outcome, reason = "refused", status.persistence_error or "reconciliation_failed"
            with self._lock:
                if status is not None:
                    self._pause_status = status
        if outcome == "resumed":
            self._save_pause(False)  # stored before the flag clears: a crash in between keeps the pause
        with self._lock:
            if outcome == "resumed":
                self._paused = False
                self.restored_pause = False
                self._pause_error = None
                if bound is not None:
                    bound.policy_paused = False
                    if bound.ended and self._bound is bound:
                        self._bound = None
                        unbound = True
            self._last_resume = {"outcome": outcome, "reason": reason, "at": _iso(self._wall())}
        if unbound:
            try:
                self.admission.set_active_run(None)
            except Exception:
                pass
        self._log(f"automation: resume {outcome}" + (f" ({reason})" if reason else ""))

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Tests and shutdown: wait for queued pause/resume transitions."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._transitions or self._transition is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._transition_cv.wait(remaining)
            return True

    def close(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self.wait_idle(timeout)
        with self._lock:
            self._closing = True
            transition = self._transition_thread
        if transition is not None and transition is not threading.current_thread():
            transition.join(timeout)
        self.repository.close()

    # -- the published ui_v1 ``automation`` state ------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Composed from the controller's fields on each call (no I/O; the UI loop reads it)."""
        return json.loads(json.dumps(self._compose()))

    def _publish(self) -> None:
        """State changes are visible through ``status`` at once; kept as the single change point."""

    def _compose(self) -> dict[str, Any]:
        with self._lock:
            bound, paused, transition = self._bound, self._paused, self._transition
            tick, review, exit_event = self._last_tick, self._last_review, self._last_exit
            pause_status, pause_error, last_resume = self._pause_status, self._pause_error, self._last_resume
        store_error = self._pause_store_error
        problems = list((tick or {}).get("problems") or [])
        if transition is not None:
            state = transition
        elif paused:
            state = "paused"
        elif bound is None:
            state = "idle"
        elif bound.coordinator is None:
            state, problems = "held", [bound.error or "run_not_bound"]
        elif bound.kind == "experiment" and (tick or {}).get("outcome") == "held":
            state = "held"
        else:
            state = "active"
        run = None if bound is None else {
            "kind": bound.kind, "task_id": bound.task_id, "revision": bound.revision, "run_id": bound.run_id,
            "bound": bound.coordinator is not None, "error": bound.error}
        now = self._clock()
        review_view = {"applies": bound is not None and bound.kind == "experiment",
                       "interval_seconds": self._interval, "status": None, "reason": None, "review_count": 0,
                       "last_review_at": None, "next_due_in_seconds": None, "pending": False,
                       "coalesced_count": 0, "exit": None if exit_event is None else dict(exit_event)}
        if review is not None and bound is not None:
            last, due = review.get("last_review_at"), review.get("next_due_at")
            review_view.update({
                "status": review.get("status"), "reason": review.get("reason"),
                "review_count": review.get("review_count", 0), "pending": bool(review.get("pending")),
                "coalesced_count": review.get("coalesced_count", 0),
                "last_review_at": None if last is None else _iso(self._wall() - max(0.0, now - last)),
                "next_due_in_seconds": None if due is None else round(max(0.0, due - now), 1)})
        interruption: dict[str, Any] = {"state": "none", "pause_id": None, "requested_at": None,
                                        "confirmed_at": None, "manager_ack": None, "worker_ack": None,
                                        "unknown_tool_call_ids": [], "error": pause_error}
        if (paused or transition is not None) and pause_status is None and pause_error is None:
            interruption["state"] = ("not_needed" if bound is None or not bound.pause_bound
                                     else "requesting")
        if (paused or transition is not None) and pause_status is not None and pause_status.pause_id is not None:
            interruption.update({
                "state": INTERRUPTION.get(pause_status.abort_status, "unknown"),
                "pause_id": pause_status.pause_id, "requested_at": pause_status.requested_at,
                "confirmed_at": pause_status.stop_observed_at, "manager_ack": pause_status.manager_ack_status,
                "worker_ack": pause_status.worker_ack_status,
                "unknown_tool_call_ids": list(pause_status.unknown_tool_call_ids)})
        if pause_error is not None and (paused or transition is not None):
            interruption["state"] = "unknown"
        detail = {"idle": "no active run",
                  "active": ("free-work run: no host run to review; pause/resume apply"
                             if bound is not None and bound.kind == "work" else "automation active for the run"),
                  "held": "automatic work held: " + (", ".join(problems) or "reconciliation needed"),
                  "paused": ("paused by the user (kept across the backend restart); collection continues, new "
                             "automatic work is held" if self.restored_pause else
                             "paused by the user; collection continues, new automatic work is held"),
                  "pausing": "pausing: holding new work and requesting the manager turn to stop",
                  "resuming": "resuming: reconciling files, tools, processes, Task and approval"}[state]
        if store_error is not None:
            detail += f" (the {'pause' if store_error == 'pause_not_stored' else 'resume'} is not stored durably; retried)"
        return {"state": state, "source": "backend", "detail": detail, "paused": paused,
                "transition": transition, "run": run,
                "tick": None if tick is None else {"outcome": tick.get("outcome"),
                                                   "problems": list(tick.get("problems") or []),
                                                   "at": _iso(tick.get("at"))},
                "review": review_view, "interruption": interruption,
                "resume": None if last_resume is None else dict(last_resume),
                "retry_limit": self._retry_limit,
                "persistence_error": (pause_status.persistence_error if pause_status is not None
                                      and pause_status.persistence_error else store_error)}


__all__ = ["AutomationController", "FreeWorkProcess", "PaneShell", "REVIEW_INTERVAL_SECONDS", "TICK_INTERVAL_SECONDS",
           "ThreadLocalRepository", "active_run_ref", "approval_hash", "stored_message"]
