"""The production backend process: owns three PTYs, the bridge and the stores.

It runs in its own session (``setsid``) apart from any UI, holds a per-data-dir
instance lock for its whole lifetime and serves ui_v1 on ``ui.sock``. UI
connections come and go; only a confirmed shutdown request or a signal stops
it. It never starts a Task run or replays a request. The only processes it
starts again, when an attached UI asks for it, are an exited manager/worker OMP,
as a new session with its start-up command (C-D62), and an exited host shell, as
a new persistent shell started like the first one (C-D63). The backend itself is
never restarted. The only live pane it ends on request is the worker OMP, when
the manager calls ``restart_worker`` (C-D70 (3): only the worker OMP process it
started and its own session members, then a new worker session through the same
restart path); the host shell is force-killed with the members of its own
session only on the user's request (C-D63).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback
from typing import Any
from uuid import uuid4

from workbench.app.lifecycle import LifecycleJournal
from workbench.backend.automation import AutomationController
from workbench.backend.flow import (
    HANDOFF_JOURNAL_NAME, HandoffService, environment_value_findings, rejected, sensitive_environment_values,
)
from workbench.backend.flow_recovery import (
    RESTART_WORKER_TOOL, restart_detail, RESTARTS_SHOWN, STATUS_REPORTS_MAX, STATUS_TOOL, WatchPorts, Watchdog,
    bounded, validate_restart_arguments, validate_status_arguments,
)
from workbench.backend.flow_tasks import FLOW_LEDGER_NAME, ExperimentPorts, TaskFlow
from workbench.backend.flow_terminal import (
    ABANDON_TOOL, TERMINAL_DIRECTORY, TERMINAL_TOOL, HostGate, TerminalService,
)
from workbench.backend.launcher import (
    ISOLATION_PROVIDER_IDS, LaunchPlan, check_isolation, default_skills_dir, isolation_check_environment,
    omp_command, omp_environment, pending_isolation, role_overlay, role_skill_allowlist, shell_environment,
    summarize_isolation, write_role_overlay,
)
from workbench.backend.omp_home import (
    OmpHome, home_environment, natives_status, prepare_omp_home, verify_omp_home)
from workbench.backend.panes import HostShellPort, OmpPane, Pane, ShellPane, process_ref, ref_dict
from workbench.backend.paths import (
    BackendLocked, DataLayout, InstanceLock, ensure_private_dir, unlink_stale_socket, write_private_json,
)
from workbench.backend.ui_server import Held, UiServer
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import Reason
from workbench.contracts.v1 import ActorRole, DisplayChunk, PaneId
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected, G3BridgeServer, MailboxError, TaskMailbox
from workbench.policy.pause_automation.journal import PauseJournal
from workbench.runtime.process_evidence import LinuxProcessProbe, ProcessRef
from workbench.storage.log_raw.store import RawLogStore
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState
from workbench.workflow.run import TaskWorkflow
from workbench.workflow.worker_port import G3WorkerResponsePort

EXIT_LOCKED = 75
READY_TIMEOUT = 90.0
STATE_INTERVAL = 0.25
SHUTDOWN_TOKEN_TTL = 120.0
OMP_ROLES = (("manager", PaneId.MANAGER_OMP), ("worker", PaneId.WORKER_OMP))
ISOLATION_JOIN_TIMEOUT = 15.0
RESTART_HISTORY = 20
NOTICE_LOCK_WAIT = 0.2  # C-D68 (8): a delivery to the worker in progress defers a terminal notice
# C-D70 (3): restart_worker asks the worker OMP to end, waits this long, then KILLs its own session members.
RESTART_GRACE = 3.0
RESTART_JOB_BUDGET = 8.0  # the tool waits this long (from the request) for the restart itself ...
RESTART_TOOL_BUDGET = 9.0  # ... and until here for the new worker's bridge registration (the bridge waits 10 s)
WATCH_EVENTS = ("agent_start", "agent_end", "tool_execution_start")  # a worker turn the watchdog did not probe


@dataclass(eq=False)
class _RestartJob:
    """One manager restart_worker request, carried out on the backend loop (it owns the pane table)."""

    reason: str
    requester: str
    at: float
    phase: str = "new"  # new | terminating
    deadline: float = 0.0
    terminated: bool = False
    result: dict[str, Any] | None = None
    done: threading.Event = field(default_factory=threading.Event)


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} backend[{os.getpid()}] {message}", flush=True)


def read_boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


class Backend:
    """ui_v1 controller plus the backend-owned ports CW-06/CW-18/CW-19 consume."""

    def __init__(self, layout: DataLayout, plan: LaunchPlan, *, project_dir: str,
                 environment: dict[str, str]):
        self.layout, self.plan, self.project_dir = layout, plan, project_dir
        self.environment = dict(environment)
        self.phase, self.reason = "starting", None
        self.started_wall = time.time()
        self.ref = process_ref("backend", os.getpid())
        self.boot = {"boot_id": read_boot_id(), "confirmation_required": False, "confirmed": None}
        self.automation: dict[str, Any] = {"state": "not_configured", "source": "backend",
                                           "detail": "automation flow is not bound (CW-18)"}
        self.focus = PaneId.MANAGER_OMP
        self.panes: dict[PaneId, Pane] = {}
        self.shell: ShellPane | None = None
        self.bridge: G3BridgeServer | None = None
        self.mailbox: TaskMailbox | None = None
        # CW-18 to_worker/to_manager (C-D64/C-D65): HandoffService with the TaskFlow policy (U2).
        self.handoffs: HandoffService | None = None
        self.flow: TaskFlow | None = None
        # C-D68: the worker's terminal tool (its only command execution path), sharing the host-shell gate.
        self.terminal: TerminalService | None = None
        # ui_v1 pause/resume go to these; CW-18 U3 binds them to the AutomationController in _open.
        self.pause_hook = self._default_pause
        self.resume_hook = self._default_resume
        # CW-18 U3: lifecycle tick, 60 s worker review and pause/resume for the active run.
        self.automation_loop: AutomationController | None = None
        self.repository: TaskRepository | None = None
        self.raw_logs: RawLogStore | None = None
        self.pause_journal: PauseJournal | None = None
        self.lifecycle_journal: LifecycleJournal | None = None
        self.ui: UiServer | None = None
        self._lock = InstanceLock(layout.lock)
        self._stop_signal: int | None = None
        self._shutdown_token: tuple[str, float] | None = None
        self._shutdown_confirmed = False
        self._last_state_digest: str | None = None
        self._last_state_at = 0.0
        self.shutdown_result: dict[str, Any] | None = None
        # C-D59 start-up isolation check: runs off the UI loop in one thread.
        self.omp_isolation: dict[str, Any] = pending_isolation(plan.omp_version)
        self._isolation_notes: list[str] = []
        self._isolation_cancel = threading.Event()
        self._isolation_thread: threading.Thread | None = None
        self._isolation_dirty = False
        self._isolation_lock = threading.Lock()
        self._isolation_results: dict[str, dict[str, Any]] = {}
        self._isolation_rechecking: set[str] = set()
        # C-D64 Workbench-owned OMP home, prepared at start and again before an OMP restart.
        self.omp_home: OmpHome | None = None
        # C-D62 restart: each OMP pane's start-up launch, kept for the backend lifetime.
        self._launch: dict[PaneId, dict[str, Any]] = {}
        self._restart_lock = threading.Lock()
        self.restarts: list[dict[str, Any]] = []
        # C-D63: the host shell's start-up environment and cwd, and its kill/restart serialisation.
        self._shell_env: dict[str, str] | None = None
        self._shell_cwd: str | None = None
        self._shell_lock = threading.Lock()
        self.kills: list[dict[str, Any]] = []
        # C-D70: the watchdog, the manager's restart_worker jobs and the manager tools' results by call key.
        self.watchdog: Watchdog | None = None
        self._restart_jobs: list[_RestartJob] = []
        self._jobs_lock = threading.Lock()
        self._jobs_closed = False  # set under _jobs_lock when the backend aborts the jobs at shutdown
        self._recovery_lock = threading.Lock()
        self._recovery_results: dict[tuple, dict[str, Any]] = {}
        self._recovery_inflight: set[tuple] = set()
        self._causes_reported: set[int] = set()

    # -- lifecycle -------------------------------------------------------
    def run(self) -> int:
        ensure_private_dir(self.layout.root)
        self.layout.check_socket_paths()
        try:
            self._lock.acquire()
        except BackendLocked:
            _log("another backend holds this data dir; not starting")
            return EXIT_LOCKED
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        status = 0
        try:
            self._open()
            self._loop()
        except BaseException:
            _log("backend failure:\n" + traceback.format_exc())
            status = 1
        finally:
            self.shutdown_result = self._close()
            _log(f"stopped: {self.shutdown_result}")
            self._lock.release()
        return status

    def _on_signal(self, signum: int, _frame: object) -> None:
        self._stop_signal = signum

    def _prepare_omp_home(self) -> OmpHome:
        home = prepare_omp_home(self.layout.root, self.environment, skills_dir=default_skills_dir(),
                                provider_ids=ISOLATION_PROVIDER_IDS)
        # OMP has no setting for its native addon dir: say so when a Workbench OMP will extract into the user's.
        natives = natives_status(self.environment, home_environment(self.environment, home.environment()),
                                 self.plan.omp_version)
        for note in (*home.notes, *([natives["note"]] if natives["note"] else [])):
            _log(f"omp home: {note}")
            if note not in self._isolation_notes:
                self._isolation_notes.append(note)
        self.omp_home = home
        return home

    def _open(self) -> None:
        layout = self.layout
        _log(f"starting in {layout.root} (session {os.getsid(0)}, shell {self.plan.shell.executable})")
        # C-D64: before anything starts; an unusable home stops the start here.
        omp_home = self._prepare_omp_home()
        _log(f"omp home {omp_home.root} (agent.db {'linked' if omp_home.linked else 'not linked'})")
        ensure_private_dir(layout.workflow)
        self.repository = TaskRepository(layout.tasks)
        os.chmod(layout.tasks, 0o600)  # SQLite journals inherit the database mode
        self.raw_logs = RawLogStore(layout.raw_logs)
        self.pause_journal = PauseJournal(layout.pause_journal)
        self.lifecycle_journal = LifecycleJournal(layout.lifecycle_journal)
        for path in (layout.bridge_socket, layout.ui_socket):
            unlink_stale_socket(path)
        tokens = {role: str(uuid4()) for role, _ in OMP_ROLES}
        self.bridge = G3BridgeServer(layout.bridge_socket, tokens)
        self.bridge.start()
        self.mailbox = TaskMailbox(self.repository, self.bridge)
        self.handoffs = HandoffService(layout.workflow / HANDOFF_JOURNAL_NAME,
                                       mailbox_factory=self._handoff_mailbox, paused=self._automation_paused, sensitive_values=self._sensitive_values,
                                       peer_lookup=self._bridge_peer)
        self.handoffs.start()
        # CW-18 U3: one automation controller; the paused flag is the single source for every reader.
        self.automation_loop = AutomationController(
            bridge=self.bridge, database=layout.tasks, journal=self.lifecycle_journal, raw=self.raw_logs,
            shell_pane=lambda: self.shell, project_dir=self.project_dir,
            artifacts_root=ensure_private_dir(layout.workflow / "runs"), boot_marker=read_boot_id, log=_log)
        self.pause_hook = self.automation_loop.request_pause
        self.resume_hook = self.automation_loop.request_resume
        # CW-18: Tasks under the standing delegation (C-D66) and their runs (experiment: product host shell).
        host_gate = HostGate()  # C-D68: an experiment run or a worker terminal command owns the host shell
        self.flow = TaskFlow(
            layout.workflow / FLOW_LEDGER_NAME, repository_factory=lambda: TaskRepository(layout.tasks),
            handoffs=self.handoffs, omp_idle=self._omp_idle, paused=self._automation_paused,
            experiment=ExperimentPorts(
                host_shell=self._host_shell_port, make_workflow=self._make_workflow,
                automation=self._automation_port, environment_names=lambda: set(self._shell_env or {}),
                worktrees_root=ensure_private_dir(layout.workflow / "worktrees"),
                artifacts_root=ensure_private_dir(layout.workflow / "runs")),
            project_dir=self.project_dir, lifecycle=self.automation_loop, host_gate=host_gate,
            worker_session=self._worker_session_key)  # C-D70 (4): a new worker session gets the full Task again
        self.handoffs.configure(policy=self.flow, active_task=self.flow.active_task)
        self.terminal = TerminalService(
            handoffs=self.handoffs, host_shell=self._host_shell_port, gate=host_gate,
            log_root=ensure_private_dir(layout.workflow / TERMINAL_DIRECTORY), automation=self._automation_port,
            paused=self._automation_paused, activity=self.flow.experiment_host_activity,
            sensitive_values=self._sensitive_values, active_task=self.flow.active_task,
            task_commands=self.flow.active_commands,  # C-D69 (6): a Task's commands are the only ones run
            notify=self._worker_notice,  # C-D68 (8): checks and the completion notice
            # p27-cd70-02 Q3: the completion of a previous worker session's command goes to the manager until the
            # open Task reached the current worker session
            notify_manager=lambda notice: self._notice(ActorRole.MANAGER, notice), done_target=self._done_target)
        self.flow.terminal_runs = self.terminal.runs_for_task  # C-D69 (6)(d): the done report's commands run
        self.watchdog = self._make_watchdog()  # C-D70: status checks, stalled/restart/recovery notices
        # p27-cd70-02 Q2: a manager follow-up the worker accepted re-arms the status checks and worker_stalled
        self.flow.follow_up_submitted = lambda task_id: self.watchdog.worker_acted("manager_follow_up")
        self.automation = self.automation_loop.status()
        # The role is the peer's authenticated hello role, never a frame field.
        self.bridge.set_tool_handler(self._tool_request, undelivered=self._tool_result_undelivered,
                                     peer_gone=self._bridge_peer_gone)
        self.ui = UiServer(layout.ui_socket, self)
        self._write_record()
        self._shell_env = shell_environment(self.environment)
        self._shell_cwd = os.getcwd()
        self.shell = ShellPane(self.plan.shell, dict(self._shell_env))
        self.panes[PaneId.HOST_SHELL] = self.shell
        home = Path(self.environment.get("HOME") or Path.home())
        home_env = omp_home.environment()
        checks: dict[str, tuple[list[str], dict[str, str], tuple[str, ...]]] = {}
        for role, pane_id in OMP_ROLES:
            env = omp_environment(self.environment, self.plan, role=role, token=tokens[role],
                                  bridge_socket=layout.bridge_socket, home=home_env)
            # The Workbench home reads no user OMP config: nothing of the user's is unioned in.
            overlay_content = role_overlay(role, project_dir=self.project_dir, home=home, environment=env)
            overlay = write_role_overlay(layout.root, role, overlay_content)
            command = omp_command(self.plan, overlay)
            self.panes[pane_id] = OmpPane(pane_id, role, command, env, cwd=self.project_dir)
            checks[role] = (command, isolation_check_environment(
                self.environment, self.plan, role=role, absent_socket=layout.root / "isolation-check.sock",
                home=home_env), role_skill_allowlist(role))
            # The role token lives in the bridge, the OMP child environment and this
            # in-memory launch (never on disk) so an exited pane restarts as the same role.
            self._launch[pane_id] = {"role": role, "overlay": overlay_content, "command": list(command),
                                     "env": dict(env), "check": checks[role]}
        del tokens
        self._ready_deadline = time.monotonic() + READY_TIMEOUT
        self._write_record()
        self.flow.start()
        self.automation_loop.start()
        self.terminal.start()
        self.watchdog.start()
        # Started after every pane fork so no fork happens while it runs.
        self._isolation_thread = threading.Thread(target=self._run_isolation_check, args=(checks,),
                                                  name="omp-isolation-check", daemon=True)
        self._isolation_thread.start()

    def _run_isolation_check(self, checks: dict[str, tuple[list[str], dict[str, str], tuple[str, ...]]],
                             *, recheck: bool = False) -> None:
        """Check each role's exact OMP command (RPC introspection only, no model call).

        At start-up this checks both roles once; after a pane restart (C-D62) it
        re-checks that role and the summary combines it with the other role's result.
        """
        results: dict[str, dict[str, Any]] = {}
        failure: str | None = None
        try:
            for role, (command, env, allowed) in checks.items():
                if self._isolation_cancel.is_set():
                    break
                before = verify_omp_home(self.omp_home) if self.omp_home is not None else []
                result = check_isolation(command, cwd=self.project_dir, environment=env, role=role,
                                         allowed_skills=allowed, omp_version=self.plan.omp_version,
                                         cancel=self._isolation_cancel)
                after = verify_omp_home(self.omp_home) if self.omp_home is not None else []
                results[role] = _with_home_problems(result, list(dict.fromkeys(before + after)))
        except Exception as exc:  # never let the check die silently
            failure = f"OMP isolation check failed: {exc!r}"
        with self._isolation_lock:
            if recheck:
                self._isolation_rechecking.difference_update(checks)
            if failure is None:
                self._isolation_results.update(results)
                summary = self._isolation_summary()
            else:
                summary = summarize_isolation({}, self.plan.omp_version)
                summary["warning"] = failure
                summary["notes"] = list(self._isolation_notes)
            self.omp_isolation = summary
            self._isolation_dirty = True
        label = f"omp isolation re-check ({', '.join(checks)})" if recheck else "omp isolation check"
        if summary["state"] == "ok":
            _log(f"{label} ok")
        elif summary["state"] != "pending":
            _log(f"WARNING {label}: {summary['warning']}")

    def _isolation_summary(self) -> dict[str, Any]:
        """The snapshot field; a role being re-checked keeps the whole summary pending (caller holds the lock)."""
        if self._isolation_rechecking:
            summary = pending_isolation(self.plan.omp_version)
            summary["roles"] = {role: dict(item) for role, item in self._isolation_results.items()
                                if role not in self._isolation_rechecking}
            summary["rechecking"] = sorted(self._isolation_rechecking)
        else:
            summary = summarize_isolation(self._isolation_results, self.plan.omp_version)
        summary["notes"] = list(self._isolation_notes)
        return summary

    def _start_isolation_recheck(self, role: str,
                                 check: tuple[list[str], dict[str, str], tuple[str, ...]]) -> None:
        """Re-run one role's isolation check off the UI loop, after any check still running."""
        with self._isolation_lock:
            self._isolation_rechecking.add(role)
            self.omp_isolation = self._isolation_summary()
            self._isolation_dirty = True
        previous = self._isolation_thread

        def run() -> None:
            if previous is not None:
                previous.join()  # each check is bounded by its own timeout and the cancel event
            self._run_isolation_check({role: check}, recheck=True)

        thread = threading.Thread(target=run, name=f"omp-isolation-recheck-{role}", daemon=True)
        self._isolation_thread = thread
        thread.start()

    def _loop(self) -> None:
        assert self.ui is not None
        while self._stop_signal is None and not self._shutdown_confirmed:
            self._tick(0.05)
        _log(f"leaving loop (signal={self._stop_signal}, shutdown={self._shutdown_confirmed})")

    def _tick(self, timeout: float) -> None:
        """One loop iteration: UI requests, pane I/O, readiness, record and state push."""
        assert self.ui is not None
        reads = [fd for pane in self.panes.values() for fd in pane.fds()]
        writes = [pane.master_fd for pane in self.panes.values()
                  if isinstance(pane, OmpPane) and pane.wants_write() and pane.master_fd >= 0]
        self.ui.poll(timeout, extra_read=reads, extra_write=writes)
        # A restart request handled above may have replaced a pane: pump the current set.
        for pane in list(self.panes.values()):
            for chunk in pane.pump():
                self.ui.broadcast(chunk)
        self._run_restart_jobs()  # C-D70 (3): the manager's restart_worker, on this loop (it owns the panes)
        self._check_ready()
        if self._isolation_dirty:
            self._isolation_dirty = False
            self._write_record()
        now = time.monotonic()
        if now - self._last_state_at >= STATE_INTERVAL:
            self._last_state_at = now
            snapshot = self.snapshot()
            digest = repr(self._state_view(snapshot))
            if digest != self._last_state_digest:
                self._last_state_digest = digest
                self.ui.push_state(snapshot)

    def _check_ready(self) -> None:
        if self.phase == "ready":
            exited = self._exited_panes()
            if exited:
                self.phase, self.reason = "degraded", f"pane_exited:{','.join(exited)}"
                self._write_record()
            return
        if self.phase == "degraded":
            self._check_degraded()
            return
        if self.phase != "starting":
            return
        peers = self.bridge_state()
        if all(peers[role]["pid_matches_pane"] for role, _ in OMP_ROLES):
            self.phase, self.reason = "ready", None
            _log("ready: both OMP bridges connected")
            self._write_record()
            return
        exited = self._exited_panes()
        if exited or time.monotonic() > self._ready_deadline:
            missing = [role for role, _ in OMP_ROLES if not peers[role]["pid_matches_pane"]]
            self.phase = "degraded"
            self.reason = f"pane_exited:{','.join(exited)}" if exited else f"bridge_unconnected:{','.join(missing)}"
            _log(f"degraded: {self.reason}")
            self._write_record()

    def _check_degraded(self) -> None:
        """Degraded is recomputed: ready again only when every pane lives and both bridges match."""
        exited = self._exited_panes()
        if exited:
            reason: str | None = f"pane_exited:{','.join(exited)}"
        else:
            peers = self.bridge_state()
            missing = [role for role, _ in OMP_ROLES if not peers[role]["pid_matches_pane"]]
            reason = f"bridge_unconnected:{','.join(missing)}" if missing else None
        if reason is None:
            self.phase, self.reason = "ready", None
            _log("ready again: every pane alive and both OMP bridges connected")
            self._write_record()
        elif reason != self.reason:
            self.reason = reason
            _log(f"degraded: {reason}")
            self._write_record()

    def _exited_panes(self) -> list[str]:
        # A pane that exits is only reported here; an exited pane restarts on a UI request (C-D62/C-D63).
        exited = [p.pane_id.value for p in self.panes.values() if isinstance(p, OmpPane) and p.poll() is not None]
        if self.shell is not None and self.shell.exited():
            exited.append(PaneId.HOST_SHELL.value)
        return exited

    def _close(self) -> dict[str, Any]:
        self._isolation_cancel.set()
        if self.watchdog is not None:
            self.watchdog.close()  # no notice is sent after this; nothing is replayed
        self._abort_restart_jobs("the backend is shutting down; the worker was not restarted")
        if self.automation_loop is not None:
            self.automation_loop.close()  # the tick and pause/resume threads end first; nothing is replayed
        if self.flow is not None:
            self.flow.close()  # the runner stops before the panes close; a run left current stays unknown
        if self.terminal is not None:
            self.terminal.close()  # a worker command still running stays in the host shell; its wait ends
        if self._isolation_thread is not None:
            self._isolation_thread.join(ISOLATION_JOIN_TIMEOUT)
        refs = self.process_refs()
        if self.ui is not None:
            self.ui.stop_accepting()
        closed: list[dict[str, Any]] = []
        for pane in self.panes.values():
            try:
                closed.append(pane.close())
            except Exception as exc:  # keep closing the rest; report the failure
                closed.append({"pane": pane.pane_id.value, "error": repr(exc)})
        if self.bridge is not None:
            self.bridge.set_tool_handler(None)
            self.bridge.close()
        if self.handoffs is not None:
            self.handoffs.close()
        if self.repository is not None:
            self.repository.close()
        if self.raw_logs is not None:
            self.raw_logs.close()
        probe = LinuxProcessProbe()
        evidence = {name: probe.observe(ref).state for name, ref in refs.items() if name != "backend"}
        result = {"panes": closed, "processes": evidence,
                  "verified": all(state == "dead" for state in evidence.values())}
        record = self._record()
        record.update({"phase": "stopped", "shutdown": result})
        try:
            write_private_json(self.layout.record, record)
        except OSError:
            pass
        if self.ui is not None:
            self.ui.close(result)
        return result

    # -- identity --------------------------------------------------------
    def process_refs(self) -> dict[str, ProcessRef]:
        refs: dict[str, ProcessRef] = {}
        if self.ref is not None:
            refs["backend"] = self.ref
        for pane in self.panes.values():
            if pane.ref is not None:
                refs[pane.pane_id.value] = pane.ref
        if self.shell is not None and self.shell._supervisor_ref is not None:
            refs["supervisor"] = self.shell._supervisor_ref
        return refs

    def _record(self) -> dict[str, Any]:
        return {"contract": {"name": ui_v1.CONTRACT_NAME, "version": ui_v1.VERSION},
                "phase": self.phase, "reason": self.reason, "data_dir": str(self.layout.root),
                "ui_socket": str(self.layout.ui_socket), "project_dir": self.project_dir,
                "boot_id": self.boot["boot_id"], "omp_version": self.plan.omp_version,
                "shell": {"kind": self.plan.shell.kind, "executable": self.plan.shell.executable},
                "omp_isolation": {key: self.omp_isolation.get(key) for key in
                                  ("state", "ok", "leaks", "warnings", "warning", "version_drift", "notes")},
                "processes": {name: asdict(ref) for name, ref in self.process_refs().items()},
                "restarts": [dict(item) for item in self.restarts],
                "kills": [dict(item) for item in self.kills]}

    def _write_record(self) -> None:
        write_private_json(self.layout.record, self._record())

    def bridge_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"event_cursor": self.bridge.event_cursor() if self.bridge else 0}
        for role, pane_id in OMP_ROLES:
            pane = self.panes.get(pane_id)
            try:
                peer = self.bridge.peer(role, 0) if self.bridge else None
            except BridgeDisconnected:
                peer = None
            state[role] = {"connected": peer is not None,
                           "session_id": peer.session_id if peer else None,
                           "generation": peer.generation if peer else None,
                           "pid": peer.pid if peer else None,
                           "pid_matches_pane": bool(peer and isinstance(pane, OmpPane) and peer.pid == pane.pid)}
        return state

    # -- ui_v1 controller ------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {"contract": {"name": ui_v1.CONTRACT_NAME, "version": ui_v1.VERSION},
                "backend": {"pid": os.getpid(), "process": ref_dict(self.ref), "session_id": os.getsid(0),
                            "data_dir": str(self.layout.root), "project_dir": self.project_dir,
                            "started_at": self.started_wall, "omp_version": self.plan.omp_version},
                "phase": self.phase, "reason": self.reason,
                "attached": self.ui is not None and self.ui.attached is not None,
                "focus": self.focus.value,
                "panes": {pane_id.value: self._pane_info(pane, host=pane_id is PaneId.HOST_SHELL)
                          for pane_id, pane in self.panes.items()},
                "bridge": self.bridge_state(), "automation": self._automation_view(),
                "task": self.flow.task_view() if self.flow else None,
                "worker": self.flow.worker_view() if self.flow else {"state": "idle", "task_id": None},
                "omp_isolation": self.omp_isolation,
                "recovery": self._recovery_view(),
                "boot": dict(self.boot), "shutdown": {"pending": self._shutdown_token is not None},
                "ui": dict(self.ui.stats) if self.ui else {}}

    def _pane_info(self, pane: Any, *, host: bool) -> dict[str, Any]:
        """A pane's ui_v1 info; the host shell also says who operates it (``operated_by``, smoke-01 P2)."""
        info = dict(pane.info())
        if host:
            terminal = getattr(self, "terminal", None)
            try:
                info["operated_by"] = terminal.host_operator() if terminal is not None else None
            except Exception:
                info["operated_by"] = None
        return info

    @staticmethod
    def _state_view(snapshot: dict[str, Any]) -> dict[str, Any]:
        view = dict(snapshot)
        view["panes"] = {key: {k: v for k, v in value.items() if k != "sequence"}
                         for key, value in snapshot["panes"].items()}
        view.pop("ui", None)
        return view

    def replay(self) -> list[DisplayChunk]:
        return [chunk for pane in self.panes.values() for chunk in pane.replay()]

    def on_attach(self, size: tuple[int, int] | None) -> None:
        _log("ui attached")
        if size is not None:
            for pane in self.panes.values():
                pane.resize(*size)

    def on_detach(self) -> None:
        _log("ui detached")

    def _pane(self, pane_id: PaneId) -> Pane:
        pane = self.panes.get(pane_id)
        if pane is None:
            raise Held(Reason.PANE_UNAVAILABLE, f"{pane_id.value} is not running")
        return pane

    def admit(self, pane_id: PaneId, data: bytes, kind: str) -> tuple[Reason, str] | None:
        return self._pane(pane_id).admit(data)

    def resize(self, pane_id: PaneId | None, rows: int, cols: int) -> None:
        targets = self.panes.values() if pane_id is None else [self._pane(pane_id)]
        for pane in targets:
            pane.resize(rows, cols)

    def set_focus(self, pane_id: PaneId) -> None:
        self._pane(pane_id)
        self.focus = pane_id

    def _shell_action(self, action: str) -> dict[str, Any]:
        if self.shell is None:
            raise Held(Reason.PANE_UNAVAILABLE, "host shell is not running")
        if self.shell.exited():
            raise Held(Reason.PANE_UNAVAILABLE, "host shell has exited; restart it first")
        try:
            return getattr(self.shell, action)()
        except UnsafeShellState as exc:
            reason = Reason.HANDOFF_HELD if action == "handoff" else Reason.TAKEOVER_HELD
            raise Held(reason, str(exc)) from exc
        except OSError as exc:
            raise Held(Reason.INPUT_TARGET_UNKNOWN, f"shell control unavailable: {exc}") from exc

    def takeover_request(self) -> dict[str, Any]:
        return self._shell_action("request_takeover")

    def takeover_confirm(self) -> dict[str, Any]:
        return self._shell_action("confirm_takeover")

    def handoff(self) -> dict[str, Any]:
        return self._shell_action("handoff")

    def _active_work(self) -> list[dict[str, Any]]:
        active: list[dict[str, Any]] = []
        # An exited or killed shell has no work left: its request was closed as unknown (C-D63).
        if self.shell is not None and not self.shell.exited():
            state = self.shell.shell_state()
            if state["request_id"] is not None and state["phase"] not in {"control_returned"}:
                active.append({"kind": "shell_request", "request_id": state["request_id"], "phase": state["phase"]})
            if state["parent_mode"] == "manual_foreground":
                active.append({"kind": "shell_foreground", "detail": "a manual program is in the foreground"})
        for role, _ in OMP_ROLES:
            try:
                idle = self.bridge.probe(role, timeout=0.5).get("idle") if self.bridge else None
            except (MailboxError, OSError, TimeoutError):
                idle = None
            if idle is not True:
                active.append({"kind": "omp_turn", "role": role, "idle": idle})
        automation = self._automation_view()
        if automation.get("state") not in {"not_configured", "idle", "paused", "cancelled"}:
            active.append({"kind": "automation", "state": automation.get("state")})
        task = self.flow.task_view() if self.flow else None
        if task is not None and task.get("run_id") is not None:
            active.append({"kind": "task_run", "task_id": task["task_id"], "run_id": task["run_id"]})
        return active

    def shutdown_request(self) -> dict[str, Any]:
        token = uuid4().hex
        self._shutdown_token = (token, time.monotonic() + SHUTDOWN_TOKEN_TTL)
        return {"token": token, "active": self._active_work(), "expires_in": SHUTDOWN_TOKEN_TTL,
                "processes": {name: asdict(ref) for name, ref in self.process_refs().items()}}

    def shutdown_confirm(self, token: str) -> dict[str, Any]:
        if self._shutdown_token is None:
            raise Held(Reason.SHUTDOWN_NOT_REQUESTED, "request shutdown first")
        expected, expires = self._shutdown_token
        if token != expected or time.monotonic() > expires:
            self._shutdown_token = None
            raise Held(Reason.SHUTDOWN_TOKEN_MISMATCH, "shutdown token is stale or unknown; request again")
        self._shutdown_confirmed = True
        _log("shutdown confirmed by UI")
        return {"shutting_down": True}

    def confirm_boot(self, boot_id: str) -> dict[str, Any]:
        if not self.boot["confirmation_required"]:
            raise Held(Reason.BOOT_CONFIRMATION_NOT_REQUIRED, "no boot confirmation is pending")
        if boot_id != self.boot["boot_id"]:
            raise Held(Reason.BOOT_ID_MISMATCH, "boot id does not match the current boot")
        self.boot["confirmed"] = True
        return {"boot": dict(self.boot)}

    def _shutting_down(self) -> bool:
        return self._shutdown_confirmed or self._stop_signal is not None

    def restart_pane(self, pane_id: PaneId) -> dict[str, Any]:
        """Start a new session in an exited pane (OMP: C-D62, host shell: C-D63).

        Only a pane whose process was reaped is replaced; a live pane and the
        backend are never signalled. Requests are serialised; a spawn failure
        leaves the exited pane in place with the reason.
        """
        if self._shutting_down():
            raise Held(Reason.BACKEND_SHUTDOWN, "the backend is shutting down; panes are not restarted")
        if pane_id is PaneId.HOST_SHELL:
            return self._restart_shell()
        pane, launch = self.panes.get(pane_id), self._launch.get(pane_id)
        if not isinstance(pane, OmpPane) or launch is None:
            raise Held(Reason.PANE_UNAVAILABLE, f"{pane_id.value} has no OMP pane in this backend")
        if not self._restart_lock.acquire(blocking=False):
            raise Held(Reason.RESTART_IN_PROGRESS, "another OMP pane restart is being set up; try again")
        try:
            if pane.poll() is None:
                raise Held(Reason.PANE_ALIVE,
                           f"{pane_id.value} OMP is still running; only an exited OMP pane is restarted")
            with self._isolation_lock:
                rechecking = launch["role"] in self._isolation_rechecking
            if rechecking:
                raise Held(Reason.RESTART_IN_PROGRESS,
                           f"{pane_id.value} was just restarted and its isolation re-check is still running")
            return self._respawn(pane_id, pane, launch,
                                 cause={"cause": "user_restart", "requester": "user", "reason": None})
        finally:
            self._restart_lock.release()

    def _respawn(self, pane_id: PaneId, old: OmpPane, launch: dict[str, Any], *,
                 cause: dict[str, Any] | None = None, close_grace: float = 3.0) -> dict[str, Any]:
        role = launch["role"]
        cause = dict(cause or {"cause": "user_restart", "requester": "user", "reason": None})
        previous = {"process": ref_dict(old.ref), "session_id": old.session_id, "generation": old.generation,
                    "exit_status": old.returncode}
        count = (old.restart or {}).get("count", 0)
        try:
            # The same Workbench OMP home (config.yml rewritten, auth link repaired) and the
            # same per-role overlay content as at start-up, at the paths the argv/env name.
            if self.omp_home is not None:
                self._prepare_omp_home()
            write_role_overlay(self.layout.root, role, launch["overlay"])
            # The exited OMP is reaped: release its PTY and any members left in its own session.
            # A member that cannot be signalled (EPERM) is reported, never a restart failure.
            # C-D70 (3): a live worker OMP asked to end by restart_worker is KILLed here with its session.
            previous["survivors"] = old.close(grace=close_grace)["survivors"]
            previous["exit_status"] = old.returncode
            new = OmpPane(pane_id, role, launch["command"], launch["env"], cwd=self.project_dir,
                          size=old.size, generation=old.generation + 1)
        except Exception as exc:  # the pane stays exited with the reason; the backend keeps running
            detail = f"could not start a new {role} OMP: {exc}"
            old.restart = {"state": "failed", "count": count, "at": time.time(), "error": detail,
                           "previous": previous, **cause}
            _log(f"restart of {pane_id.value} failed: {exc!r}")
            self._write_record()
            raise Held(Reason.RESTART_FAILED, detail) from exc
        entry = {"pane": pane_id.value, "at": time.time(), "previous": previous,
                 "process": ref_dict(new.ref), "session_id": new.session_id, "generation": new.generation, **cause}
        new.restart = {"state": "restarted", "count": count + 1, "at": entry["at"], "error": None,
                       "previous": previous, **cause}
        self.panes[pane_id] = new
        self.restarts = (self.restarts + [entry])[-RESTART_HISTORY:]
        _log(f"restarted {pane_id.value}: pid {new.pid} (was {previous['process']}, exit {old.returncode}, "
             f"requested by {cause.get('requester')})")
        # Ready again only after the new OMP registers with its own pid (see _check_degraded).
        self.phase = "degraded"
        self._check_degraded()
        self._write_record()
        self._start_isolation_recheck(role, launch["check"])
        return {"pane": pane_id.value, "restarted": True, "process": ref_dict(new.ref),
                "session_id": new.session_id, "generation": new.generation,
                "survivors": previous["survivors"]}

    def _restart_shell(self) -> dict[str, Any]:
        old = self.shell
        if old is None or self._shell_env is None:
            raise Held(Reason.PANE_UNAVAILABLE, "host_shell has no shell pane in this backend")
        if not self._shell_lock.acquire(blocking=False):
            raise Held(Reason.RESTART_IN_PROGRESS, "a host shell kill or restart is in progress; try again")
        try:
            if not old.exited():
                raise Held(Reason.PANE_ALIVE, "the host shell is still running; only an exited host shell "
                                              "is restarted")
            previous = {"process": ref_dict(old.ref), "session_id": old.session_id, "generation": old.generation,
                        "exit_status": old.returncode, "input_owner": old.state["input_owner"],
                        "manager_command": None if old.closed_request is None else dict(old.closed_request)}
            count = (old.restart or {}).get("count", 0)
            try:
                # Members left in the exited shell's session were pinned when it exited; one that
                # cannot be signalled (EPERM) is reported and the new shell still starts.
                previous["survivors"] = old.close()["survivors"]
                if self._shell_cwd is not None and os.getcwd() != self._shell_cwd:
                    os.chdir(self._shell_cwd)  # the shell starts in the backend's start-up cwd
                new = ShellPane(self.plan.shell, dict(self._shell_env), size=old.size,
                                generation=old.generation + 1)
            except Exception as exc:  # the pane stays exited with the reason; the backend keeps running
                detail = f"could not start a new host shell: {exc}"
                old.restart = {"state": "failed", "count": count, "at": time.time(), "error": detail,
                               "previous": previous}
                _log(f"restart of host_shell failed: {exc!r}")
                self._write_record()
                raise Held(Reason.RESTART_FAILED, detail) from exc
            entry = {"pane": PaneId.HOST_SHELL.value, "at": time.time(), "previous": previous,
                     "process": ref_dict(new.ref), "session_id": new.session_id, "generation": new.generation}
            new.restart = {"state": "restarted", "count": count + 1, "at": entry["at"], "error": None,
                           "previous": previous}
            self.shell = new
            self.panes[PaneId.HOST_SHELL] = new
            self.restarts = (self.restarts + [entry])[-RESTART_HISTORY:]
            _log(f"restarted host_shell: pid {new.pid} (was {previous['process']}, exit {old.returncode})")
            if self.phase == "degraded":
                self._check_degraded()
            self._write_record()
            return {"pane": PaneId.HOST_SHELL.value, "restarted": True, "process": ref_dict(new.ref),
                    "session_id": new.session_id, "generation": new.generation,
                    "input_owner": new.state["input_owner"], "survivors": previous["survivors"]}
        finally:
            self._shell_lock.release()

    def kill_pane(self, pane_id: PaneId) -> dict[str, Any]:
        """Force-kill the host shell and the members of its session at once (C-D63).

        The UI has already asked the user to confirm; this executes on the
        request whatever the input owner is. OMP panes and the backend are never
        signalled; a manager command in flight is closed as unknown.
        """
        if self._shutting_down():
            raise Held(Reason.BACKEND_SHUTDOWN, "the backend is shutting down; the host shell is closed with it")
        if pane_id is not PaneId.HOST_SHELL:
            raise Held(Reason.PANE_NOT_KILLABLE,
                       f"only the host shell can be force-killed; {pane_id.value} ends through the OMP itself")
        shell = self.shell
        if shell is None:
            raise Held(Reason.PANE_UNAVAILABLE, "host_shell has no shell pane in this backend")
        if not self._shell_lock.acquire(blocking=False):
            raise Held(Reason.KILL_IN_PROGRESS, "a host shell kill or restart is in progress; try again")
        try:
            if shell.exited():
                raise Held(Reason.PANE_EXITED, "the host shell has already exited; restart it instead")
            try:
                result = shell.kill()
            except UnsafeShellState as exc:
                if shell.exited():
                    raise Held(Reason.PANE_EXITED, f"the host shell exited meanwhile: {exc}") from exc
                raise Held(Reason.KILL_FAILED, str(exc)) from exc
            except OSError as exc:  # never into the backend loop; the shell stays as it is
                _log(f"force-kill of host_shell failed: {exc!r}")
                raise Held(Reason.KILL_FAILED, f"host shell kill failed: {type(exc).__name__}: {exc}") from exc
            entry = {"pane": PaneId.HOST_SHELL.value, "at": result["at"], "process": result["process"],
                     "session_id": result["session_id"], "generation": result["generation"],
                     "input_owner": result["input_owner"], "manager_command": result["manager_command"],
                     "exit_status": result["exit_status"], "survivors": result["survivors"],
                     "left_session": result["left_session"]}
            self.kills = (self.kills + [entry])[-RESTART_HISTORY:]
            _log(f"force-killed host_shell pid {shell.pid} (owner {result['input_owner']}, "
                 f"manager command {result['manager_command']}, survivors {result['survivors']})")
            self._check_ready()
            self._write_record()
            return result
        finally:
            self._shell_lock.release()

    # -- ports for later tickets (CW-18/CW-19) ----------------------------
    # -- CW-18 automation: one paused source for HandoffService, TaskFlow and AutomationState ----
    def _automation_paused(self) -> bool:
        if self.automation_loop is not None:
            return self.automation_loop.paused()
        return self.automation.get("state") == "paused"

    def _automation_view(self) -> dict[str, Any]:
        """The ui_v1 ``automation`` state: the controller's (U3) once bound, else the backend's own."""
        if self.automation_loop is not None:
            return self.automation_loop.status()
        return dict(self.automation)

    def _handoff_mailbox(self) -> tuple[TaskMailbox, Any]:
        """Runs on the handoff outbox thread: SQLite connections are bound to their thread."""
        repository = TaskRepository(self.layout.tasks)
        return TaskMailbox(repository, self.bridge), repository.close

    def _tool_request(self, peer: Any, request: dict[str, Any]) -> dict[str, Any]:
        """Bridge tool requests: the worker's ``terminal`` (C-D68) waits for its command; the manager's
        ``restart_worker`` and ``workbench_status`` (C-D70) are answered here; the rest is handoffs."""
        tool = request.get("tool")
        watchdog = getattr(self, "watchdog", None)  # a backend built without __init__ (tests) has none
        if watchdog is not None and getattr(peer.role, "value", peer.role) == "worker" \
                and tool in (TERMINAL_TOOL, "to_manager"):
            watchdog.worker_acted(str(tool))  # C-D70 (1): the worker acted; its status checks start over
        if tool in (RESTART_WORKER_TOOL, STATUS_TOOL):
            return self._recovery_tool(peer, request)
        if request.get("tool") in (TERMINAL_TOOL, ABANDON_TOOL):
            terminal = self.terminal
            if terminal is None:
                return {"status": "rejected", "reason": "terminal_unavailable"}
            if request.get("tool") == ABANDON_TOOL:  # C-D68 (8): a terminal call stopped waiting
                return terminal.abandon(peer.role, request)
            return terminal.handle(peer.role, request)
        return self.handoffs.handle(peer.role, request)

    def _bridge_peer_gone(self, peer: Any) -> None:
        """A bridge session ended (closed or replaced): its waiting terminal calls are dropped (review-02 P3 (3))."""
        terminal = self.terminal
        if terminal is not None:
            terminal.peer_gone(peer.role, peer.session_id, peer.generation)

    def _tool_result_undelivered(self, peer: Any, request: dict[str, Any]) -> None:
        """A tool result the bridge could not write to its session (p27-cd68-fix-01 P2-1).

        Only ``terminal`` cares: that call did not get its command's result, so
        the completion notice is still sent.
        """
        terminal = self.terminal
        if request.get("tool") == TERMINAL_TOOL and terminal is not None:
            terminal.undelivered(peer.role, request)

    def _worker_notice(self, notice: Any) -> str:
        """C-D68 (8): one Workbench notice to the worker OMP; see ``_notice``."""
        return self._notice(ActorRole.WORKER, notice)

    def _notice(self, role: Any, notice: Any) -> str:
        """C-D68 (8), C-D70: one Workbench notice to the ``role`` OMP (the bridge ``notice`` frame).

        It holds the worker's delivery lock, which every mailbox delivery (a
        handoff, a workflow stage, an experiment's periodic review) holds until
        the worker's turn ends, so a notice never goes into the same turn. The
        bridge sends it only to an idle, unpaused worker. ``delivered``,
        ``deferred`` (busy: try again), ``paused``, ``not_connected`` (nothing
        sent), ``unknown`` (it may have been sent: never resent) or ``rejected``.
        """
        bridge = self.bridge
        role = role if isinstance(role, ActorRole) else ActorRole(role)
        try:
            peer = bridge.peer(role, 0)
        except (BridgeDisconnected, MailboxError):
            return "not_connected"
        lock = bridge.delivery_lock(role)
        if not lock.acquire(timeout=NOTICE_LOCK_WAIT):
            return "deferred"  # a delivery to this OMP is in progress: its turn
        try:
            ack = bridge.request(role, {"kind": "notice", "notice": dict(notice)}, timeout=5.0,
                                 expected_peer=(peer.session_id, peer.generation))
        except Exception:
            return "unknown"
        finally:
            lock.release()
        status = ack.get("status") if isinstance(ack, dict) else None
        if status in ("api_accepted", "duplicate_api_accepted"):
            return "delivered"
        if status == "deferred":
            return "paused" if ack.get("reason") == "paused" else "deferred"
        if status == "unknown_no_replay":
            return "unknown"
        return "rejected"

    # -- C-D70: the watchdog, restart_worker and workbench_status ----------------------------
    def _worker_session_key(self) -> tuple[str, int] | None:
        peer = self._bridge_peer(ActorRole.WORKER)
        return None if peer is None else (peer.session_id, peer.generation)

    def _done_target(self, started: dict[str, Any]) -> str:
        """``manager`` for a command a previous worker session started whose Task has not been re-delivered to the
        current worker session (p27-cd70-02 Q3), else ``worker``."""
        current = self._worker_session_key()
        session = (started.get("session_id"), started.get("generation"))
        if current is None or session == tuple(current):
            return "worker"
        task_id = started.get("task_id")
        if task_id is not None and self.flow is not None and self.flow.task_session(task_id) == list(current):
            return "worker"  # re-delivered: the current worker has the Task
        return "manager"

    def _probe(self, role: Any) -> dict[str, Any] | None:
        try:
            return self.bridge.probe(role, timeout=1.0) if self.bridge is not None else None
        except (MailboxError, OSError, TimeoutError):
            return None

    def _worker_turns(self, cursor: int) -> tuple[int, int]:
        if self.bridge is None:
            return cursor, 0
        return self.bridge.events_since(ActorRole.WORKER, WATCH_EVENTS, cursor)

    def _make_watchdog(self) -> Watchdog:
        handoffs, flow, terminal = self.handoffs, self.flow, self.terminal
        return Watchdog(WatchPorts(
            task=flow.watch_view, task_summary=flow.task_view, worker_state=flow.worker_view,
            peer=self._bridge_peer, probe=self._probe, turns=self._worker_turns,
            terminal=terminal.watch_state, outbox_busy=handoffs.lane_busy, reports=handoffs.report_entries,
            requeue=lambda: handoffs.requeue_for_new_session(ActorRole.MANAGER), paused=self._automation_paused,
            notify=self._notice, restart_cause=self._restart_cause, journal=handoffs.record))

    def _recovery_view(self) -> dict[str, Any] | None:
        """ui_v1 ``recovery`` (C-D70, additive)."""
        watchdog = self.watchdog
        if watchdog is None:
            return None
        try:
            return watchdog.view()
        except Exception:
            return None

    def _restart_cause(self, peer: Any) -> dict[str, Any]:
        """Why the worker OMP with this bridge peer is a new session (for the worker_restarted notice)."""
        pid = getattr(peer, "pid", None)
        if pid not in self._causes_reported:
            for entry in reversed(self.restarts):
                if entry.get("pane") == PaneId.WORKER_OMP.value and (entry.get("process") or {}).get("pid") == pid:
                    self._causes_reported.add(pid)
                    return {"cause": entry.get("cause") or "user_restart", "requester": entry.get("requester"),
                            "reason": entry.get("reason"), "at": entry.get("at")}
        pane = self.panes.get(PaneId.WORKER_OMP)
        if isinstance(pane, OmpPane) and pane.pid == pid:
            return {"cause": "omp_new_session", "requester": None, "reason": None}  # same process, new session
        return {"cause": "unknown", "requester": None, "reason": None}

    def _recovery_tool(self, peer: Any, request: dict[str, Any]) -> dict[str, Any]:
        """``restart_worker`` / ``workbench_status`` (C-D70 (3)/(6)): manager only, once per tool call key."""
        parsed = HandoffService._parse(peer.role, request)
        if isinstance(parsed, dict):
            self._journal({"type": "recovery_invalid", "reason": parsed["reason"]})
            return parsed
        key, key_dict = parsed.key, parsed.key_dict()
        if parsed.role is not ActorRole.MANAGER:
            self._journal({"type": "recovery_request", "key": key_dict, "tool": parsed.tool,
                           "redacted": "tool_not_allowed_for_role"})
            return rejected("tool_not_allowed_for_role")
        with self._recovery_lock:
            if key in self._recovery_results:
                return dict(self._recovery_results[key])
            if key in self._recovery_inflight:
                return {"status": "outcome_unknown", "reason": "duplicate_in_flight"}
            self._recovery_inflight.add(key)
        started = time.monotonic()
        try:
            if parsed.tool == RESTART_WORKER_TOOL:
                result = self._restart_worker(parsed, key_dict, started)
            else:
                result = self._workbench_status(parsed, key_dict)
        except Exception as exc:  # a restart may have started: its outcome is not known here
            result = {"status": "outcome_unknown", "reason": f"backend_error:{type(exc).__name__}"}
        with self._recovery_lock:
            self._recovery_inflight.discard(key)
            self._recovery_results[key] = result
            while len(self._recovery_results) > 256:
                del self._recovery_results[next(iter(self._recovery_results))]
        if parsed.tool == RESTART_WORKER_TOOL:
            self._journal({"type": "restart_worker_result", "key": key_dict,
                           "result": {k: v for k, v in result.items() if k != "detail"}})
        return dict(result)

    def _journal(self, record: dict[str, Any]) -> None:
        try:
            if self.handoffs is not None:
                self.handoffs.record(record)
        except (OSError, TypeError, ValueError):
            pass

    def _restart_worker(self, parsed: Any, key_dict: dict[str, Any], started: float) -> dict[str, Any]:
        args = parsed.args
        errors = validate_restart_arguments(args)
        if errors:
            self._journal({"type": "restart_worker_request", "key": key_dict, "redacted": "invalid_arguments",
                           "errors": errors})
            return rejected("invalid_arguments", errors=errors)
        reason = args["reason"].strip()
        try:
            findings = environment_value_findings({"reason": reason}, self._sensitive_values())
        except Exception:
            findings = ["reason:environment_check_unavailable"]
        if findings:
            self._journal({"type": "restart_worker_request", "key": key_dict, "redacted": "environment_value",
                           "fields": findings})
            return rejected("environment_value", fields=findings,
                            detail="Never put environment variable values in the reason.")
        self._journal({"type": "restart_worker_request", "key": key_dict, "reason": reason})
        if self._shutting_down():
            return {"status": "refused", "reason": "backend_shutdown",
                    "detail": "The backend is shutting down; the worker is not restarted."}
        if not isinstance(self.panes.get(PaneId.WORKER_OMP), OmpPane) or PaneId.WORKER_OMP not in self._launch:
            return {"status": "refused", "reason": "pane_unavailable", "detail": "There is no worker OMP pane."}
        if not self._restart_lock.acquire(blocking=False):
            return {"status": "refused", "reason": "restart_in_progress",
                    "detail": "Another OMP restart is in progress; wait for it (do not retry in a loop)."}
        with self._isolation_lock:
            rechecking = "worker" in self._isolation_rechecking
        if rechecking:
            self._restart_lock.release()
            return {"status": "refused", "reason": "restart_in_progress",
                    "detail": "The worker was just restarted and its isolation re-check still runs; wait for it."}
        job = _RestartJob(reason, "manager", time.time())
        with self._jobs_lock:  # the same lock as _abort_restart_jobs: no job is queued after the shutdown abort
            closing = self._jobs_closed or self._shutting_down()
            if not closing:
                self._restart_jobs.append(job)  # the backend loop owns the lock from here and releases it
        if closing:
            self._restart_lock.release()
            return {"status": "refused", "reason": "backend_shutdown",
                    "detail": "The backend is shutting down; the worker is not restarted."}
        if not job.done.wait(max(0.0, started + RESTART_JOB_BUDGET - time.monotonic())):
            if self._shutting_down():  # review P3-4: never "restarting" while the backend stops
                return {"status": "refused", "reason": "backend_shutdown",
                        "detail": "The backend is shutting down; no new worker session is started."}
            return {"status": "restarting", "reason": reason,
                    "detail": restart_detail(self._worker_task_open(), ready=False)}
        result = dict(job.result or {"status": "outcome_unknown", "reason": "no_result"})
        if result.get("status") != "restarted":
            return result
        pid = (result.get("process") or {}).get("pid")
        peer = None
        while time.monotonic() < started + RESTART_TOOL_BUDGET:
            peer = self._bridge_peer(ActorRole.WORKER)
            if peer is not None and peer.pid == pid:
                break
            peer = None
            time.sleep(0.05)
        flow_task = self.flow.task_view() if self.flow is not None else None
        task = None if flow_task is None else {name: flow_task.get(name) for name in (
            "task_id", "kind", "status", "active", "held_reason", "closed_reason", "summary")}
        terminal = self.terminal.watch_state() if self.terminal is not None else None
        return {"status": "restarted", "reason": reason,
                "worker": {"pane_generation": result.get("generation"), "pid": pid, "registered": peer is not None,
                           "session_id": None if peer is None else peer.session_id,
                           "generation": None if peer is None else peer.generation},
                "previous": result.get("previous"), "survivors": result.get("survivors"), "task": task,
                "terminal": terminal, "detail": restart_detail(self._worker_task_open(), ready=peer is not None)}

    def _worker_task_open(self) -> bool:
        """A Task the worker_restarted notice is sent for (the watchdog's rule: busy, or blocked and open)."""
        flow = self.flow
        if flow is None:
            return False
        if flow.watch_view() is not None:
            return True
        view = flow.task_view()
        return isinstance(view, dict) and view.get("status") == "blocked"

    def _run_restart_jobs(self) -> None:
        """The backend loop's part of restart_worker: ask the worker OMP to end, wait ``RESTART_GRACE`` without
        blocking the loop, then close it (KILL of its own session members) and respawn it (C-D62 path)."""
        while True:
            with self._jobs_lock:
                if not self._restart_jobs:
                    return
                job = self._restart_jobs[0]
            if not self._advance_restart(job):
                return
            with self._jobs_lock:
                self._restart_jobs.remove(job)

    def _advance_restart(self, job: _RestartJob) -> bool:
        """One step of a job; True once it finished (result set, restart lock released)."""
        pane, launch = self.panes.get(PaneId.WORKER_OMP), self._launch.get(PaneId.WORKER_OMP)
        try:
            if self._shutting_down():
                raise Held(Reason.BACKEND_SHUTDOWN, "the backend is shutting down; the worker was not restarted")
            if not isinstance(pane, OmpPane) or launch is None:
                raise Held(Reason.PANE_UNAVAILABLE, "worker_omp has no OMP pane in this backend")
            if job.phase == "new":
                job.phase, job.deadline = "terminating", time.monotonic() + RESTART_GRACE
                job.terminated = pane.poll() is None and pane.terminate()
                _log(f"restart_worker requested by the manager: worker OMP pid {pane.pid} "
                     f"{'asked to end' if job.terminated else 'not signalled (already exited or not provable)'}")
            if pane.poll() is None and job.terminated and time.monotonic() < job.deadline:
                return False  # still ending politely; the loop goes on meanwhile
            result = self._respawn(PaneId.WORKER_OMP, pane, launch, close_grace=0.0, cause={
                "cause": "restart_worker", "requester": "manager", "reason": job.reason, "requested_at": job.at})
            job.result = {"status": "restarted", **result, "previous": (self.panes[PaneId.WORKER_OMP].restart or {})
                          .get("previous")}
        except Held as exc:
            job.result = {"status": "failed" if exc.reason is Reason.RESTART_FAILED else "refused",
                          "reason": exc.reason.value, "detail": exc.detail}
        except Exception as exc:  # never into the backend loop
            job.result = {"status": "failed", "reason": Reason.RESTART_FAILED.value,
                          "detail": f"{type(exc).__name__}: {exc}"[:300]}
        self._restart_lock.release()
        job.done.set()
        return True

    def _abort_restart_jobs(self, detail: str) -> None:
        with self._jobs_lock:
            self._jobs_closed = True  # restart_worker queues nothing from now on
            jobs, self._restart_jobs = list(self._restart_jobs), []
        for job in jobs:
            job.result = {"status": "refused", "reason": Reason.BACKEND_SHUTDOWN.value, "detail": detail}
            self._restart_lock.release()
            job.done.set()

    def _workbench_status(self, parsed: Any, key_dict: dict[str, Any]) -> dict[str, Any]:
        """C-D70 (6): read-only; never waits on a mailbox (one bridge probe of at most 1 s)."""
        task_id, errors = validate_status_arguments(parsed.args)
        if errors:
            return rejected("invalid_arguments", errors=errors)
        flow, terminal, handoffs = self.flow, self.terminal, self.handoffs
        task = flow.status_view(task_id) if flow is not None else None
        if task_id is not None and task is None:
            return rejected("unknown_task", detail="task_id: no such Task; null means the current one")
        self._journal({"type": "workbench_status", "key": key_dict, "task_id": None if task is None
                       else task.get("task_id")})
        if task is not None and terminal is not None:
            try:
                runs = terminal.runs_for_task(task["task_id"])
                task["commands_run"] = list(runs)
                task["commands_run_notes"] = list(getattr(runs, "notes", ()))
            except Exception:
                task["commands_run"], task["commands_run_notes"] = None, ["the terminal journal could not be read"]
        peer = self._bridge_peer(ActorRole.WORKER)
        probe = self._probe(ActorRole.WORKER) if peer is not None else None
        pane = self.panes.get(PaneId.WORKER_OMP)
        restarts = [{name: entry.get(name) for name in ("at", "cause", "requester", "reason", "generation")}
                    | {"pid": (entry.get("process") or {}).get("pid")}
                    for entry in self.restarts if entry.get("pane") == PaneId.WORKER_OMP.value][-RESTARTS_SHOWN:]
        worker = {**(flow.worker_view() if flow is not None else {}),
                  "omp": None if probe is None else ("idle" if probe.get("idle") is True and probe.get("pending")
                                                     is False and probe.get("inFlightToolCount") == 0 else "busy"),
                  "connected": peer is not None, "session_id": None if peer is None else peer.session_id,
                  "generation": None if peer is None else peer.generation,
                  "pane_generation": getattr(pane, "generation", None), "pid": getattr(pane, "pid", None),
                  # read only: poll() would reap the child off the backend loop
                  "alive": isinstance(pane, OmpPane) and pane.returncode is None, "restarts": restarts}
        reports = []
        for entry in (handoffs.report_entries() if handoffs is not None else []):
            if entry.get("state") not in ("pending", "delivering", "unknown") or entry.get("submitted"):
                continue
            text, cut = bounded(entry.get("text"))
            state = entry["state"]
            if state == "pending" and entry.get("status") == "deferred":
                state = "deferred"
            reports.append({"task_id": entry.get("task_id"), "message_id": entry.get("message_id"),
                            "kind": entry.get("report_kind"), "origin": entry.get("origin"), "state": state,
                            "reason": entry.get("reason"), "blockers": entry.get("blockers"),
                            "requeued": entry.get("requeued"), "text": text, "text_truncated": cut})
        watchdog = self.watchdog
        return {"status": "ok", "task": task, "worker": worker,
                "terminal": terminal.watch_state() if terminal is not None else None,
                "reports": reports[-STATUS_REPORTS_MAX:], "reports_omitted": max(0, len(reports) - STATUS_REPORTS_MAX),
                "watchdog": None if watchdog is None else watchdog.view().get("watch")}

    def _bridge_peer(self, role: Any) -> Any:
        try:
            return self.bridge.peer(role, 0) if self.bridge is not None else None
        except (BridgeDisconnected, MailboxError):
            return None

    def _sensitive_values(self) -> tuple[str, ...]:
        """Secret-like environment values and the bridge tokens; compared in memory, never stored."""
        tokens = tuple(launch["env"].get("WORKBENCH_G3_TOKEN", "") for launch in list(self._launch.values()))
        return sensitive_environment_values(self.environment) + tuple(token for token in tokens if token)

    # -- CW-18 pause/resume (C-D66: Tasks need no UI approval) -------------------
    def pause(self) -> dict[str, Any]:
        return {"automation": dict(self.pause_hook())}

    def resume(self, reconciled: bool) -> dict[str, Any]:
        if reconciled is not True:
            raise Held(Reason.RESUME_NOT_RECONCILED, "resume needs reconciled: true")
        return {"automation": dict(self.resume_hook())}

    def _default_pause(self) -> dict[str, Any]:
        if self.automation.get("state") != "paused":
            self.automation = {"state": "paused", "source": "user", "detail": "paused from the UI",
                               "previous": self.automation.get("state")}
        return self.automation

    def _default_resume(self) -> dict[str, Any]:
        if self.automation.get("state") == "paused":
            self.automation = {"state": "idle", "source": "user", "detail": "resumed (reconciled)"}
        return self.automation

    def _omp_idle(self, role: Any) -> bool | None:
        """True/False from the OMP's bridge state; None when it is not connected or does not answer."""
        if self.bridge is None:
            return None
        try:
            return self.bridge.probe(role, timeout=1.0).get("idle") is True
        except (MailboxError, OSError, TimeoutError):
            return None

    def _host_shell_port(self) -> HostShellPort | None:
        shell = self.shell
        if shell is None or shell.exited():
            return None
        return HostShellPort(shell, lambda: self.shell)

    def _automation_port(self) -> dict[str, Any]:
        return {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": self._automation_paused(), "cancelled": False, "metadataHealthy": True,
            "approvalValid": True}}

    def _make_workflow(self, repository: TaskRepository) -> TaskWorkflow:
        """Runs on the task-flow runner thread: its own mailbox on the shared bridge."""
        return TaskWorkflow(repository, TaskMailbox(repository, self.bridge),
                            worker_port=G3WorkerResponsePort(self.bridge),
                            automation_source=self._automation_port)

    def set_automation_status(self, status: dict[str, Any]) -> None:
        """CW-18 publishes AutomationState/run status here; pushed to the UI as state."""
        self.automation = dict(status)

    def require_boot_confirmation(self) -> None:
        """CW-19 calls this after reconcile when the boot marker changed."""
        self.boot.update({"confirmation_required": True, "confirmed": False})


def _with_home_problems(result: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    """Add auth-link problems (C-D64) seen around one check as warnings; a leak or failure keeps its state."""
    if not problems:
        return result
    result = dict(result)
    result["warnings"] = list(result.get("warnings") or []) + problems
    if result.get("state") == "ok":
        result["state"] = "warning"
    return result


def run_backend(layout: DataLayout, plan: LaunchPlan, project_dir: str) -> int:
    backend = Backend(layout, plan, project_dir=project_dir, environment=dict(os.environ))
    return backend.run()


def main_backend(args: Any) -> int:
    layout = DataLayout(Path(args.data_dir))
    plan = LaunchPlan(ShellChoice(args.shell_kind, args.shell_path), args.omp, args.omp_version,
                      args.bridge_extension, tuple(args.omp_arg or ()))
    try:
        return run_backend(layout, plan, args.project_dir)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
