"""Fail-closed lifecycle composition; predecessor policies stay in their ports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import fcntl
import json
import os
from pathlib import Path
import stat
from threading import Event, Lock, RLock
import time
from typing import Callable, Protocol

from workbench.policy.recovery_manager import RunIdentity, RunObservation
from workbench.runtime.g4 import FrontendLease
from workbench.runtime.process_evidence import ProcessEvidence, ProcessRef


_MAX_RECORD = 64 * 1024


class LifecycleHeld(RuntimeError):
    """A lifecycle action cannot be bound to current durable/live evidence."""


@dataclass(frozen=True, slots=True)
class PeerRef:
    role: str
    session_id: str
    generation: int
    process: ProcessRef

    def __post_init__(self) -> None:
        if (self.role not in {"manager", "worker"} or not self.session_id
                or type(self.generation) is not int or self.generation < 1
                or not isinstance(self.process, ProcessRef)):
            raise ValueError("bound OMP peer identity required")


@dataclass(frozen=True, slots=True)
class ControlState:
    input_owner: str
    owner_epoch: int
    mode: str
    takeover_requested: bool
    takeover_confirmed: bool

    def __post_init__(self) -> None:
        if (self.input_owner not in {"manager", "user", "unknown"}
                or type(self.owner_epoch) is not int or self.owner_epoch < 1
                or self.mode not in {"control_wait", "manual_prompt", "manual_foreground",
                                     "manual_input", "unknown"}
                or type(self.takeover_requested) is not bool
                or type(self.takeover_confirmed) is not bool):
            raise ValueError("bound shell control state required")


@dataclass(frozen=True, slots=True)
class CommandState:
    request_id: str | None = None
    delivery: str = "none"
    session_id: str | None = None
    owner_epoch: int | None = None

    def __post_init__(self) -> None:
        if self.delivery not in {"none", "unknown", "api_returned", "omp_processed"}:
            raise ValueError("command delivery state is invalid")
        if self.delivery == "none":
            if any(value is not None for value in
                   (self.request_id, self.session_id, self.owner_epoch)):
                raise ValueError("unsent command cannot have a delivery identity")
        elif (not self.request_id or not self.session_id
              or type(self.owner_epoch) is not int or self.owner_epoch < 1):
            raise ValueError("sent command needs exact session and owner epoch")


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    task_id: str
    revision: int
    run_id: str
    approval_hash: str
    generation: int
    boot_marker: str
    boot_confirmed_marker: str | None
    manager: PeerRef
    worker: PeerRef
    shell: ProcessRef
    run_targets: tuple[ProcessRef, ...]
    control: ControlState
    command: CommandState = CommandState()
    model_hold: bool = False
    last_successful_check_at: str | None = None
    last_failed_check_at: str | None = None
    admission_closed: bool = False
    shutdown_confirmed: bool = False

    def __post_init__(self) -> None:
        if (not self.task_id or not self.run_id or not self.approval_hash
                or type(self.revision) is not int or self.revision < 1
                or type(self.generation) is not int or self.generation < 1
                or not self.boot_marker
                or self.boot_confirmed_marker is not None
                and not self.boot_confirmed_marker
                or self.manager.role != "manager" or self.worker.role != "worker"
                or not isinstance(self.shell, ProcessRef)
                or not isinstance(self.run_targets, tuple)
                or any(not isinstance(item, ProcessRef) for item in self.run_targets)
                or not isinstance(self.control, ControlState)
                or not isinstance(self.command, CommandState)
                or any(type(value) is not bool for value in
                       (self.model_hold, self.admission_closed, self.shutdown_confirmed))
                or any(value is not None and (not isinstance(value, str) or not value)
                       for value in (self.last_successful_check_at,
                                     self.last_failed_check_at))):
            raise ValueError("durable lifecycle record is malformed")

    @classmethod
    def from_dict(cls, value: object) -> LifecycleRecord:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("lifecycle record shape is invalid")
        try:
            manager = PeerRef(**{**value["manager"],
                                 "process": ProcessRef(**value["manager"]["process"])})
            worker = PeerRef(**{**value["worker"],
                                "process": ProcessRef(**value["worker"]["process"])})
            return cls(**{**value, "manager": manager, "worker": worker,
                          "shell": ProcessRef(**value["shell"]),
                          "run_targets": tuple(ProcessRef(**item)
                                               for item in value["run_targets"]),
                          "control": ControlState(**value["control"]),
                          "command": CommandState(**value["command"])})
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("lifecycle record fields are invalid") from exc


class LifecycleJournal:
    """One atomic/fsynced record with compare-and-set across coordinator restarts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock = RLock()

    def read(self) -> LifecycleRecord | None:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_size > _MAX_RECORD):
                raise LifecycleHeld("lifecycle journal path is unsafe")
            payload = os.read(descriptor, _MAX_RECORD + 1)
        finally:
            os.close(descriptor)
        if len(payload) > _MAX_RECORD:
            raise LifecycleHeld("lifecycle journal exceeds its bound")
        try:
            envelope = json.loads(payload)
            if (not isinstance(envelope, dict) or set(envelope) != {"version", "record"}
                    or type(envelope["version"]) is not int or envelope["version"] != 1):
                raise ValueError("journal version mismatch")
            return LifecycleRecord.from_dict(envelope["record"])
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise LifecycleHeld("lifecycle journal is corrupt") from exc

    def _write(self, record: LifecycleRecord) -> None:
        payload = json.dumps({"version": 1, "record": asdict(record)},
                             sort_keys=True, separators=(",", ":")).encode()
        if len(payload) > _MAX_RECORD:
            raise LifecycleHeld("lifecycle journal exceeds its bound")
        parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        name = f".lifecycle-{os.getpid()}-{os.urandom(8).hex()}"
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path.name,
                           src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                try:
                    os.unlink(name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent_fd)

    def commit(self, expected: LifecycleRecord | None,
               updated: LifecycleRecord) -> LifecycleRecord:
        if not isinstance(updated, LifecycleRecord):
            raise TypeError("LifecycleRecord required")
        with self._thread_lock:
            lock_fd = os.open(str(self.path) + ".lock",
                              os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(lock_fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
                    raise LifecycleHeld("lifecycle journal lock is unsafe")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                current = self.read()
                if current != expected:
                    raise LifecycleHeld("lifecycle journal changed; reconcile before action")
                if expected is None:
                    if updated.generation != 1:
                        raise LifecycleHeld("initial lifecycle generation must be one")
                elif updated.generation != expected.generation + 1:
                    raise LifecycleHeld("lifecycle generation must advance once")
                self._write(updated)
                return updated
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)


@dataclass(frozen=True, slots=True)
class Reconciliation:
    state: str
    problems: tuple[str, ...]
    processes: tuple[ProcessEvidence, ...]
    paused: bool | None
    cancelled: bool | None

    @property
    def ready(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    confirmed: bool
    complete: bool
    survivors: tuple[ProcessRef, ...]
    problems: tuple[str, ...]


class PeerPort(Protocol):
    def observe(self, role: str) -> PeerRef | None: ...


class ControlPort(Protocol):
    def observe(self) -> ControlState | None: ...
    def request_takeover(self) -> ControlState: ...
    def confirm_takeover(self) -> ControlState: ...
    def handoff(self) -> ControlState: ...


class ProcessPort(Protocol):
    def observe(self, ref: ProcessRef) -> ProcessEvidence: ...


class CommandPort(Protocol):
    def send_bound(self, request_id: str, *, session_id: str, generation: int,
                   owner_epoch: int, authority_token: object) -> str: ...


class _SubmissionFence:
    """Order an immediate takeover revoke against one G3 submission claim."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._revoked = Event()
        self._epoch = 0

    def is_set(self) -> bool:
        return self._revoked.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._revoked.wait(timeout)

    def set(self) -> int:
        with self._lock:
            self._epoch += 1
            self._revoked.set()
            return self._epoch

    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def claim(self, epoch: int, valid: Callable[[], bool]) -> bool:
        with self._lock:
            if self._revoked.is_set() or self._epoch != epoch:
                return False
            try:
                return valid() is True and not self._revoked.is_set()
            except Exception:
                return False

    def reopen(self, expected_epoch: int) -> bool:
        with self._lock:
            if self._epoch != expected_epoch:
                return False
            self._epoch += 1
            self._revoked.clear()
            return True


class _LifecyclePermit:
    """One durable command incarnation plus an invocation-time user intent."""

    def __init__(self, journal: LifecycleJournal, pending: LifecycleRecord,
                 takeover_intent: _SubmissionFence):
        self._journal = journal
        self._pending = pending
        self._takeover_intent = takeover_intent
        self._epoch = takeover_intent.epoch()

    def current(self) -> bool:
        if self._takeover_intent.is_set():
            return False
        try:
            observed = self._journal.read()
        except Exception:
            return False
        return (not self._takeover_intent.is_set()
                and self._takeover_intent.epoch() == self._epoch
                and observed == self._pending
                and not self._pending.admission_closed
                and not self._pending.control.takeover_requested)

    def claim(self) -> bool:
        def valid() -> bool:
            return (self._journal.read() == self._pending
                    and not self._pending.admission_closed
                    and not self._pending.control.takeover_requested)

        return self._takeover_intent.claim(self._epoch, valid)


class StopPort(Protocol):
    def normal_stop(self, ref: ProcessRef) -> None: ...
    def drain(self, timeout: float) -> None: ...


class LifecycleCoordinator:
    """Reconcile first; never replay a command or infer full termination."""

    def __init__(
        self, *, journal: LifecycleJournal, peers: PeerPort, control: ControlPort,
        processes: ProcessPort, pause: object, review: object, model: object,
        metadata: object, raw: object, commands: CommandPort, stops: StopPort,
        termination: Callable[[], RunObservation | None],
        boot_marker: Callable[[], str], authority: Callable[[], bool],
        frontend: FrontendLease | None = None,
    ):
        self.journal = journal
        self.peers = peers
        self.control = control
        self.processes = processes
        self.pause = pause
        self.review = review
        self.model = model
        self.metadata = metadata
        self.raw = raw
        self.commands = commands
        self.stops = stops
        self.termination = termination
        self.boot_marker = boot_marker
        self.authority = authority
        self.frontend = frontend or FrontendLease()
        # Automatic external effects finish before a shutdown or takeover
        # *durable* fence can commit. Takeover intent can independently revoke
        # a pending G3 submission while an earlier action holds this lock.
        # Reentrant because a same-thread port callback may observe this owner.
        self._linear_lock = RLock()
        # request_takeover sets this before waiting for the long-running action
        # lock. G3's final authority check reads it without taking that lock.
        self._takeover_intent = _SubmissionFence()
        self._takeover_generation = 0

    def initialize(self, record: LifecycleRecord) -> LifecycleRecord:
        with self._linear_lock:
            return self.journal.commit(None, record)

    def _record(self) -> LifecycleRecord:
        record = self.journal.read()
        if record is None:
            raise LifecycleHeld("no durable lifecycle binding")
        return record

    def _change(self, record: LifecycleRecord, **changes: object) -> LifecycleRecord:
        return self.journal.commit(record, replace(record, generation=record.generation + 1,
                                                   **changes))

    def detach(self) -> None:
        with self._linear_lock:
            self.frontend.detach()

    def reconnect(self) -> Reconciliation:
        with self._linear_lock:
            result = self.reconcile()
            self.frontend.attach()
            return result

    @staticmethod
    def _current_run_observation(record: LifecycleRecord, observed: object) -> bool:
        return (isinstance(observed, RunObservation)
                and observed.identity == RunIdentity(record.task_id, record.revision,
                                                       record.run_id))

    def reconcile(self) -> Reconciliation:
        record = self._record()
        problems: list[str] = []
        evidence: list[ProcessEvidence] = []
        current_boot: str | None = None
        try:
            current_boot = self.boot_marker()
            if not current_boot:
                raise ValueError("missing boot marker")
        except Exception:
            problems.append("boot_marker_unknown")
        if (current_boot is not None and current_boot != record.boot_marker
                and record.boot_confirmed_marker != current_boot):
            problems.append("boot_confirmation_required")
        for expected in (record.manager, record.worker):
            try:
                actual = self.peers.observe(expected.role)
            except Exception:
                actual = None
            if actual != expected:
                problems.append(f"{expected.role}_session_unknown_or_drifted")
        try:
            actual_control = self.control.observe()
        except Exception:
            actual_control = None
        if actual_control != record.control:
            problems.append("control_or_owner_unknown_or_drifted")
        for ref in (record.manager.process, record.worker.process,
                    record.shell, *record.run_targets):
            try:
                observed = self.processes.observe(ref)
            except Exception:
                observed = ProcessEvidence(ref, "unknown")
            evidence.append(observed)
            if observed.state == "unknown":
                problems.append(f"{ref.role}_identity_unknown")
            elif observed.state == "dead" and ref not in record.run_targets:
                problems.append(f"{ref.role}_dead")
        ended = False
        try:
            observed_run = self.termination()
            if isinstance(observed_run, RunObservation):
                if not self._current_run_observation(record, observed_run):
                    problems.append("run_termination_identity_unknown")
                else:
                    ended = observed_run.fully_terminated
        except Exception:
            pass
        if (any(item.state == "dead" and item.ref in record.run_targets
                for item in evidence) and not ended):
            problems.append("run_termination_unconfirmed")
        if record.command.delivery == "unknown":
            problems.append("command_delivery_unknown_no_replay")
        paused: bool | None = None
        cancelled: bool | None = None
        try:
            pause_status = self.pause.status()
            paused, cancelled = pause_status.paused, pause_status.cancelled
            if pause_status.persistence_error is not None:
                problems.append("pause_persistence_unknown")
        except Exception:
            problems.append("pause_or_cancel_unknown")
        try:
            if self.authority() is not True:
                problems.append("authority_revoked_or_unknown")
        except Exception:
            problems.append("authority_revoked_or_unknown")
        if record.admission_closed:
            problems.append("shutdown_admission_closed")
        return Reconciliation(
            "ready" if not problems and not ended else "ended" if not problems else "unknown",
            tuple(dict.fromkeys(problems)), tuple(evidence), paused, cancelled,
        )

    def _automatic_admission(self, record: LifecycleRecord,
                             current: Reconciliation) -> tuple[str, ...]:
        """Common run/authority/durability fence for command and review."""
        problems = list(current.problems)
        if current.state != "ready":
            problems.append("run_ended" if current.state == "ended" else "run_not_ready")
        if self._takeover_intent.is_set():
            problems.append("takeover_intent_active")
        if record.model_hold:
            problems.append("model_recheck_required")
        if current.paused is not False or current.cancelled is not False:
            problems.append("pause_or_cancel_active_or_unknown")
        if getattr(self.metadata, "automatic_runs_allowed", None) is not True:
            problems.append("metadata_durability_unconfirmed")
        if (record.control.input_owner != "manager" or record.control.mode != "control_wait"
                or record.control.takeover_requested):
            problems.append("user_owner_or_control_hold")
        return tuple(dict.fromkeys(problems))

    def _command_admission(self, record: LifecycleRecord,
                           current: Reconciliation) -> tuple[str, ...]:
        problems = list(self._automatic_admission(record, current))
        if record.command.delivery != "none":
            problems.append("command_already_attempted_no_replay")
        return tuple(dict.fromkeys(problems))

    def automatic_admission(self) -> tuple[bool, tuple[str, ...]]:
        with self._linear_lock:
            problems = self._command_admission(self._record(), self.reconcile())
            return not problems, problems

    def dispatch_command(self, request_id: str) -> str:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request identity required")
        with self._linear_lock:
            record = self._record()
            problems = self._command_admission(record, self.reconcile())
            if problems:
                raise LifecycleHeld(",".join(problems))
            pending = CommandState(request_id, "unknown", record.worker.session_id,
                                   record.control.owner_epoch)
            pending_record = self._change(record, command=pending)
            permit = _LifecyclePermit(self.journal, pending_record,
                                      self._takeover_intent)
            try:
                delivery = self.commands.send_bound(
                    request_id, session_id=record.worker.session_id,
                    generation=record.worker.generation,
                    owner_epoch=record.control.owner_epoch,
                    authority_token=permit,
                )
            except LifecycleHeld:
                raise
            except Exception as exc:
                raise LifecycleHeld("command delivery unknown; no replay") from exc
            if delivery not in {"api_returned", "omp_processed"}:
                raise LifecycleHeld("command delivery unknown; no replay")
            current = self._record()
            self._change(current, command=replace(pending, delivery=delivery))
            return delivery

    def tick(self) -> tuple[str, object | None]:
        with self._linear_lock:
            record = self._record()
            if self._automatic_admission(record, self.reconcile()):
                return "held", None
            return self.pause.dispatch_automatic(self.review.tick)

    def observe_raw(self, data: bytes, *, observed_at: str | None = None) -> object:
        record = self._record()
        return self.raw.append(record.run_id, data, observed_at=observed_at)

    def model_failed(self, observed_at: str) -> LifecycleRecord:
        with self._linear_lock:
            record = self._record()
            return self._change(record, model_hold=True,
                                last_failed_check_at=observed_at)

    def recheck_model(self, observed_at: str) -> bool:
        with self._linear_lock:
            record = self._record()
            try:
                successful = self.model.check() is True
            except Exception:
                successful = False
            if successful:
                self._change(record, model_hold=False,
                             last_successful_check_at=observed_at)
            else:
                self._change(record, model_hold=True,
                             last_failed_check_at=observed_at)
            return successful

    def request_takeover(self) -> ControlState:
        generation = self._takeover_intent.set()  # Before _linear_lock wait.
        with self._linear_lock:
            record = self._record()
            if record.admission_closed:
                raise LifecycleHeld("shutdown admission is closed")
            if record.control.takeover_requested:
                self._takeover_generation = max(self._takeover_generation, generation)
                raise LifecycleHeld("takeover already requested; no replay")
            # Unknown command delivery forbids replay, not an explicit user
            # takeover. Check only the current manager/control incarnations.
            self._require_current_control(record)
            self._change(record, control=replace(record.control, takeover_requested=True))
            self._takeover_generation = generation
            try:
                actual = self.control.request_takeover()
            except Exception as exc:
                raise LifecycleHeld("takeover state unknown") from exc
            current = self._record()
            if not isinstance(actual, ControlState) or not actual.takeover_requested:
                raise LifecycleHeld("takeover confirmation is unavailable")
            self._change(current, control=actual)
            return actual

    def _require_current_control(self, record: LifecycleRecord) -> None:
        try:
            if (self.peers.observe("manager") != record.manager
                    or self.control.observe() != record.control
                    or self.processes.observe(record.manager.process).state != "alive"
                    or self.processes.observe(record.shell).state != "alive"):
                raise LifecycleHeld("current shell owner/session is unconfirmed")
        except LifecycleHeld:
            raise
        except Exception as exc:
            raise LifecycleHeld("current shell owner/session is unknown") from exc

    def confirm_takeover(self, *, session_id: str, owner_epoch: int) -> ControlState:
        with self._linear_lock:
            record = self._record()
            if (session_id != record.manager.session_id
                    or owner_epoch != record.control.owner_epoch
                    or not record.control.takeover_requested):
                raise LifecycleHeld("stale takeover confirmation")
            self._require_current_control(record)
            actual = self.control.confirm_takeover()
            if not isinstance(actual, ControlState) or not actual.takeover_confirmed:
                raise LifecycleHeld("foreground takeover unconfirmed")
            self._change(record, control=actual)
            return actual

    def handoff(self, *, session_id: str, owner_epoch: int) -> ControlState:
        with self._linear_lock:
            returning_generation = self._takeover_generation
            record = self._record()
            if (session_id != record.manager.session_id
                    or owner_epoch != record.control.owner_epoch
                    or not record.control.takeover_confirmed):
                raise LifecycleHeld("stale or unconfirmed handoff")
            self._require_current_control(record)
            actual = self.control.handoff()
            if (not isinstance(actual, ControlState) or actual.input_owner != "manager"
                    or actual.mode != "control_wait" or actual.takeover_requested):
                raise LifecycleHeld("current-session handoff unconfirmed")
            self._change(record, control=actual)
            self._takeover_intent.reopen(returning_generation)
            return actual

    def confirm_boot(self, *, user_confirmed: bool) -> LifecycleRecord:
        if user_confirmed is not True:
            raise LifecycleHeld("explicit boot confirmation required")
        with self._linear_lock:
            record = self._record()
            marker = self.boot_marker()
            if not isinstance(marker, str) or not marker:
                raise LifecycleHeld("boot marker unavailable")
            self.reconcile()  # Expose drift; confirmation does not repair it.
            return self._change(record, boot_confirmed_marker=marker)

    def shutdown(self, *, user_confirmed: bool, timeout: float = 2.0) -> ShutdownResult:
        if user_confirmed is not True:
            return ShutdownResult(False, False, (), ("user_confirmation_required",))
        if type(timeout) not in {int, float} or not 0 < timeout <= 10:
            raise ValueError("bounded shutdown timeout required")
        with self._linear_lock:
            record = self._record()
            first_stop_request = not record.shutdown_confirmed
            if first_stop_request or not record.admission_closed:
                record = self._change(record, admission_closed=True, shutdown_confirmed=True)
            targets = tuple(dict.fromkeys((record.manager.process, record.worker.process,
                                           record.shell, *record.run_targets)))
            problems: list[str] = []
            # The durable confirmation is the at-most-once stop-attempt fence. A
            # crash after this commit is unknown, not permission to replay a stop.
            if first_stop_request:
                for ref in targets:
                    try:
                        self.stops.normal_stop(ref)
                    except Exception:
                        problems.append(f"{ref.role}_normal_stop_unknown")
        deadline = time.monotonic() + timeout
        survivors = targets
        terminated = False
        while True:
            try:
                self.stops.drain(min(0.02, max(0, deadline - time.monotonic())))
            except Exception:
                problems.append("observation_drain_unknown")
            observed = []
            for ref in targets:
                try:
                    observed.append(self.processes.observe(ref))
                except Exception:
                    observed.append(ProcessEvidence(ref, "unknown"))
            survivors = tuple(item.ref for item in observed if item.state != "dead")
            try:
                run = self.termination()
                terminated = (self._current_run_observation(record, run)
                              and run.fully_terminated)
            except Exception:
                terminated = False
            if not survivors and terminated or time.monotonic() >= deadline:
                break
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        if survivors:
            problems.append("managed_survivors_or_unknown")
        if not terminated:
            problems.append("full_termination_unconfirmed")
        return ShutdownResult(True, not problems, survivors,
                              tuple(dict.fromkeys(problems)))
