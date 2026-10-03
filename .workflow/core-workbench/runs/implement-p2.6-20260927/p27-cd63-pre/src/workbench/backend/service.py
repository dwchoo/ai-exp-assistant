"""The production backend process: owns three PTYs, the bridge and the stores.

It runs in its own session (``setsid``) apart from any UI, holds a per-data-dir
instance lock for its whole lifetime and serves ui_v1 on ``ui.sock``. UI
connections come and go; only a confirmed shutdown request or a signal stops
it. It never starts a Task run or replays a request. The only process it starts
again is an exited manager/worker OMP, as a new session with its start-up
command, when an attached UI asks for it (C-D62); a live OMP, the host shell
and the backend itself are never restarted.
"""

from __future__ import annotations

from dataclasses import asdict
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
from workbench.backend.launcher import (
    LaunchPlan, check_isolation, isolation_check_environment, omp_command, omp_environment, pending_isolation,
    read_user_config, role_overlay, role_skill_allowlist, shell_environment, summarize_isolation,
    write_role_overlay,
)
from workbench.backend.panes import OmpPane, Pane, ShellPane, process_ref, ref_dict
from workbench.backend.paths import (
    BackendLocked, DataLayout, InstanceLock, ensure_private_dir, unlink_stale_socket, write_private_json,
)
from workbench.backend.ui_server import Held, UiServer
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import Reason
from workbench.contracts.v1 import DisplayChunk, PaneId
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected, G3BridgeServer, MailboxError, TaskMailbox
from workbench.policy.pause_automation.journal import PauseJournal
from workbench.runtime.process_evidence import LinuxProcessProbe, ProcessRef
from workbench.storage.log_raw.store import RawLogStore
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState

EXIT_LOCKED = 75
READY_TIMEOUT = 90.0
STATE_INTERVAL = 0.25
SHUTDOWN_TOKEN_TTL = 120.0
OMP_ROLES = (("manager", PaneId.MANAGER_OMP), ("worker", PaneId.WORKER_OMP))
ISOLATION_JOIN_TIMEOUT = 15.0
RESTART_HISTORY = 20


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
        # C-D62 restart: each OMP pane's start-up launch, kept for the backend lifetime.
        self._launch: dict[PaneId, dict[str, Any]] = {}
        self._restart_lock = threading.Lock()
        self.restarts: list[dict[str, Any]] = []

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

    def _open(self) -> None:
        layout = self.layout
        _log(f"starting in {layout.root} (session {os.getsid(0)}, shell {self.plan.shell.executable})")
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
        self.ui = UiServer(layout.ui_socket, self)
        self._write_record()
        self.shell = ShellPane(self.plan.shell, shell_environment(self.environment))
        self.panes[PaneId.HOST_SHELL] = self.shell
        home = Path(self.environment.get("HOME") or Path.home())
        # Bounded (shared timeout, own process groups): never blocks readiness.
        user = read_user_config(self.plan.omp, cwd=self.project_dir, environment=self.environment)
        for key, value in user.items():
            if value is None:
                self._isolation_notes.append(f"could not read the user's {key}; the isolation overlay "
                                             "replaced it for this run")
        checks: dict[str, tuple[list[str], dict[str, str], tuple[str, ...]]] = {}
        for role, pane_id in OMP_ROLES:
            overlay_content = role_overlay(
                role, project_dir=self.project_dir, home=home, environment=self.environment,
                user_disabled_providers=user.get("disabledProviders") or (),
                user_disabled_agents=user.get("task.disabledAgents") or ())
            overlay = write_role_overlay(layout.root, role, overlay_content)
            command = omp_command(self.plan, overlay)
            env = omp_environment(self.environment, self.plan, role=role, token=tokens[role],
                                  bridge_socket=layout.bridge_socket)
            self.panes[pane_id] = OmpPane(pane_id, role, command, env, cwd=self.project_dir)
            checks[role] = (command, isolation_check_environment(
                self.environment, self.plan, role=role, absent_socket=layout.root / "isolation-check.sock"),
                role_skill_allowlist(role))
            # The role token lives in the bridge, the OMP child environment and this
            # in-memory launch (never on disk) so an exited pane restarts as the same role.
            self._launch[pane_id] = {"role": role, "overlay": overlay_content, "command": list(command),
                                     "env": dict(env), "check": checks[role]}
        del tokens
        self._ready_deadline = time.monotonic() + READY_TIMEOUT
        self._write_record()
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
                results[role] = check_isolation(command, cwd=self.project_dir, environment=env, role=role,
                                                allowed_skills=allowed, omp_version=self.plan.omp_version,
                                                cancel=self._isolation_cancel)
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
        # A pane that exits is only reported here; an exited OMP pane restarts on a UI request (C-D62).
        exited = [p.pane_id.value for p in self.panes.values() if isinstance(p, OmpPane) and p.poll() is not None]
        if self.shell is not None and self.shell._exited:
            exited.append(PaneId.HOST_SHELL.value)
        return exited

    def _close(self) -> dict[str, Any]:
        self._isolation_cancel.set()
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
            self.bridge.close()
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
                "restarts": [dict(item) for item in self.restarts]}

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
                "panes": {pane_id.value: pane.info() for pane_id, pane in self.panes.items()},
                "bridge": self.bridge_state(), "automation": dict(self.automation),
                "omp_isolation": self.omp_isolation,
                "boot": dict(self.boot), "shutdown": {"pending": self._shutdown_token is not None},
                "ui": dict(self.ui.stats) if self.ui else {}}

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
        if self.shell is not None:
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
        if self.automation.get("state") not in {"not_configured", "idle", "paused", "cancelled"}:
            active.append({"kind": "automation", "state": self.automation.get("state")})
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

    def restart_pane(self, pane_id: PaneId) -> dict[str, Any]:
        """Start a new OMP session in an exited manager/worker pane with its start-up launch (C-D62).

        Only an OMP whose process was reaped is replaced; a live OMP, the host
        shell and the backend are never signalled. Requests are serialised; a
        spawn failure leaves the exited pane in place with the reason.
        """
        if self._shutdown_confirmed or self._stop_signal is not None:
            raise Held(Reason.BACKEND_SHUTDOWN, "the backend is shutting down; OMP panes are not restarted")
        if pane_id is PaneId.HOST_SHELL:
            raise Held(Reason.PANE_NOT_RESTARTABLE,
                       "only an exited manager or worker OMP pane can be restarted, not the host shell")
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
            return self._respawn(pane_id, pane, launch)
        finally:
            self._restart_lock.release()

    def _respawn(self, pane_id: PaneId, old: OmpPane, launch: dict[str, Any]) -> dict[str, Any]:
        role = launch["role"]
        previous = {"process": ref_dict(old.ref), "session_id": old.session_id, "generation": old.generation,
                    "exit_status": old.returncode}
        count = (old.restart or {}).get("count", 0)
        try:
            # The same per-role overlay content as at start-up, at the path the argv names.
            write_role_overlay(self.layout.root, role, launch["overlay"])
            # The exited OMP is reaped: release its PTY and any members left in its own session.
            old.close()
            new = OmpPane(pane_id, role, launch["command"], launch["env"], cwd=self.project_dir,
                          size=old.size, generation=old.generation + 1)
        except Exception as exc:  # the pane stays exited with the reason; the backend keeps running
            detail = f"could not start a new {role} OMP: {exc}"
            old.restart = {"state": "failed", "count": count, "at": time.time(), "error": detail,
                           "previous": previous}
            _log(f"restart of {pane_id.value} failed: {exc!r}")
            self._write_record()
            raise Held(Reason.RESTART_FAILED, detail) from exc
        entry = {"pane": pane_id.value, "at": time.time(), "previous": previous,
                 "process": ref_dict(new.ref), "session_id": new.session_id, "generation": new.generation}
        new.restart = {"state": "restarted", "count": count + 1, "at": entry["at"], "error": None,
                       "previous": previous}
        self.panes[pane_id] = new
        self.restarts = (self.restarts + [entry])[-RESTART_HISTORY:]
        _log(f"restarted {pane_id.value}: pid {new.pid} (was {previous['process']}, exit {old.returncode})")
        # Ready again only after the new OMP registers with its own pid (see _check_degraded).
        self.phase = "degraded"
        self._check_degraded()
        self._write_record()
        self._start_isolation_recheck(role, launch["check"])
        return {"pane": pane_id.value, "restarted": True, "process": ref_dict(new.ref),
                "session_id": new.session_id, "generation": new.generation}

    # -- ports for later tickets (CW-18/CW-19) ----------------------------
    def set_automation_status(self, status: dict[str, Any]) -> None:
        """CW-18 publishes AutomationState/run status here; pushed to the UI as state."""
        self.automation = dict(status)

    def require_boot_confirmation(self) -> None:
        """CW-19 calls this after reconcile when the boot marker changed."""
        self.boot.update({"confirmation_required": True, "confirmed": False})


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
