"""Periodic, evidence-bounded worker review scheduling.

This module deliberately accepts narrow callables instead of reaching into the
workflow, mailbox, or provider implementations. Callers own those adapters and
may pass only public run/session/exit facts across this boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from threading import Event, Lock, RLock, Thread, local
from types import MappingProxyType
from typing import Any, Callable, Mapping
from weakref import ReferenceType, ref

from workbench.contracts.ports_v2 import dispatch_allowed, parse_port


_FACT_FIELDS = {
    "phase", "progress_count", "output_bytes", "process_count",
    "process_alive", "exit_confirmed", "exit_status",
    "last_output_age_seconds", "unknown_count",
}
_PHASES = {"starting", "running", "exited", "unknown"}
_UNKNOWN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_BLOCKED_ON = {"io", "pipe", "child_process", "wait", "resource", "unknown"}
_SAFE_UNKNOWN_CODES = {
    "safe", "safe_unknown", "process_tree_partial", "process_tree_not_observed",
    "process_tree_unobserved", "shell_process_not_attached_to_runtime_probe",
    "control_observation_lost", "takeover_write_failure",
    "supervisor_identity_unconfirmed", "supervisor_return_without_boundary",
    "supervisor_disappeared", "control_fd_failure", "request_too_large",
    "request_write_failure", "close_observation_lost",
    "input_flush_or_release_failure", "control_release_failure",
    "unknown_or_manual_residue", "investigation_timeout",
    "investigation_in_flight", "investigation_failed", "investigation_invalid",
}
_EXIT_IDENTITY_FIELDS = (
    "task_id", "revision_id", "revision", "run_id", "session_id", "session_generation",
)
_RunIdentity = tuple[str, str, int, str, str, int]
_ExitKey = tuple[int, _RunIdentity]


@dataclass(frozen=True, slots=True)
class ActiveRunRef:
    """Identity-only reference to the currently active CW-10 run/session."""

    task_id: str
    revision_id: str
    revision: int
    run_id: str
    session_id: str
    session_generation: int
    active: bool = True

    def __post_init__(self) -> None:
        for name in ("task_id", "revision_id", "run_id", "session_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError(f"{name} must be a non-empty identifier")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if type(self.session_generation) is not int or self.session_generation < 1:
            raise ValueError("session_generation must be a positive integer")
        if type(self.active) is not bool:
            raise ValueError("active must be a boolean")

    @property
    def identity(self) -> tuple[str, str, int, str, str, int]:
        return (
            self.task_id, self.revision_id, self.revision, self.run_id,
            self.session_id, self.session_generation,
        )


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    run: ActiveRunRef
    due_at: float
    coalesced_count: int
    facts: Mapping[str, Any]


@dataclass(frozen=True, slots=True, eq=False)
class ReviewTicket:
    """One request issued by one admission owner at one authority epoch."""

    request: ReviewRequest
    _nonce: object
    _run_incarnation: int


@dataclass(frozen=True, slots=True, eq=False)
class ExitSourceToken:
    """Owner-issued proof that an exit source belongs to one run incarnation."""

    run: ActiveRunRef
    _nonce: object
    _run_incarnation: int


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    status: str
    current: bool = True


@dataclass(frozen=True, slots=True, eq=False)
class ReviewFenceToken:
    """One immutable authority generation for a single coordinator binding."""

    _fence: ReviewAdmissionFence
    generation: int
    binding_nonce: object
    run_identity: _RunIdentity | None
    run_incarnation: int
    mode: str
    reason: str

    def current(self) -> bool:
        return self._fence.current(self)


class ReviewAdmissionFence:
    """Short, monotonic run-bound admission CAS; never owns an action or I/O."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._generation = 0
        self._binding_nonce = object()
        self._token = ReviewFenceToken(self, 0, self._binding_nonce, None, 0,
                                       "closed", "unbound")

    def _replace(self, *, nonce: object, run: _RunIdentity | None,
                 incarnation: int, mode: str, reason: str) -> ReviewFenceToken:
        self._generation += 1
        self._token = ReviewFenceToken(self, self._generation, nonce, run,
                                       incarnation, mode, reason)
        return self._token

    def bind(self, run: ActiveRunRef | None, incarnation: int,
             expected: ReviewFenceToken) -> tuple[ReviewFenceToken, bool]:
        with self._lock:
            uninterrupted = self._token is expected
            nonce = object()
            token = self._replace(nonce=nonce, run=None if run is None else run.identity,
                                  incarnation=incarnation, mode="closed", reason="binding_changed")
            return token, uninterrupted

    def token(self) -> ReviewFenceToken:
        with self._lock:
            return self._token

    def current(self, token: ReviewFenceToken) -> bool:
        with self._lock:
            return self._token is token

    def close(self, reason: str = "paused") -> ReviewFenceToken:
        with self._lock:
            old = self._token
            return self._replace(nonce=old.binding_nonce, run=old.run_identity,
                                 incarnation=old.run_incarnation, mode="closed", reason=reason)

    def close_if_binding(self, expected: ReviewFenceToken,
                         reason: str = "paused", *,
                         exact_generation: bool = False) -> ReviewFenceToken | None:
        with self._lock:
            current = self._token
            if (current.binding_nonce is not expected.binding_nonce
                    or current.run_identity != expected.run_identity
                    or current.run_incarnation != expected.run_incarnation
                    or exact_generation and current is not expected):
                return None
            return self.close(reason)

    def open(self, expected: ReviewFenceToken) -> ReviewFenceToken | None:
        with self._lock:
            if self._token is not expected or expected.mode != "closed":
                return None
            return self._replace(nonce=expected.binding_nonce,
                                 run=expected.run_identity,
                                 incarnation=expected.run_incarnation,
                                 mode="open", reason="reconciled")

    def permit(self, run: ActiveRunRef,
               incarnation: int) -> ReviewFenceToken | None:
        with self._lock:
            token = self._token
            if (token.mode != "open" or not isinstance(run, ActiveRunRef)
                    or not run.active or type(incarnation) is not int or incarnation < 1):
                return None
            if (token.run_identity != run.identity
                    or token.run_incarnation != incarnation):
                return None
            return token

    def closed(self) -> bool:
        return self._token.mode != "open"

    def reconciliation_needed(self) -> bool:
        return self._token.reason == "reconciliation_needed"

    def dispatch(self, run: ActiveRunRef, incarnation: int,
                 action: Callable[[], Any]) -> tuple[str, Any | None]:
        permit = self.permit(run, incarnation)
        if permit is None:
            return ("reconciliation_needed" if self.reconciliation_needed() else "paused"), None
        return "admitted", action()


class SerializedReviewAdmission:
    """One linearization boundary for run authority, exits, and review submission.

    State writers use ``set_active_run`` and ``set_automation_state``.
    Callback observations are checked against this owner; they cannot replace
    its state. Submission holds the same lock through the synchronous dispatch
    call; a second tick cannot submit that review again.
    """

    def __init__(self, run: ActiveRunRef | None = None, automation_state: object = None):
        self._lock = RLock()
        self._run = run
        self._state = self._validated_state(automation_state)
        self._epoch = 0
        self._run_incarnation = 1 if isinstance(run, ActiveRunRef) and run.active else 0
        self._exited: set[_ExitKey] = set()
        self._exit_sources: dict[object, ExitSourceToken] = {}
        self._internal_exit_source: ExitSourceToken | None = None
        # Weak historical registrations reject a still-live retired WorkflowRun
        # without retaining every source or confusing a reused object id.
        self._external_exit_sources: dict[int, ReferenceType[object]] = {}
        self._active_external_source: object | None = None
        self._active_external_token: ExitSourceToken | None = None
        self._consumed: set[tuple[int, tuple[str, str, int, str, str, int], float]] = set()
        self._tickets: dict[object, tuple[ReviewTicket, int, ReviewRequest]] = {}
        self._dispatching = False
        self._authority_fence: ReviewAdmissionFence | None = None
        self._peer_preflight: Callable[[], bool] | None = None
        self._callback_context = local()

    def install_authority_fence(self, fence: ReviewAdmissionFence) -> None:
        if not isinstance(fence, ReviewAdmissionFence):
            raise TypeError("ReviewAdmissionFence required")
        with self._lock:
            if self._authority_fence is not None:
                self._authority_fence.close()
            self._authority_fence = fence
            self._epoch += 1
            self._tickets.clear()

    def in_dispatch_callback(self) -> bool:
        return getattr(self._callback_context, "active", False)

    def install_peer_preflight(self, callback: Callable[[], bool]) -> None:
        if not callable(callback):
            raise TypeError("peer preflight must be callable")
        with self._lock:
            self._peer_preflight = callback

    @staticmethod
    def _validated_state(value: object) -> Mapping[str, Any] | None:
        try:
            port = parse_port(value)
            if port["kind"] == "AutomationState":
                return MappingProxyType(dict(port["payload"]))
        except Exception:
            pass
        return None

    def set_active_run(self, run: ActiveRunRef | None) -> int:
        if run is not None and not isinstance(run, ActiveRunRef):
            raise TypeError("active run must be an ActiveRunRef or None")
        with self._lock:
            if self._run == run:
                return self._epoch
            self._run = run
            if self._authority_fence is not None:
                self._authority_fence.close()
            self._epoch += 1  # An A-B-A replacement invalidates old snapshots.
            self._run_incarnation += 1
            self._tickets.clear()
            self._exit_sources.clear()
            self._internal_exit_source = None
            self._active_external_source = None
            self._active_external_token = None
            return self._epoch

    def set_automation_state(self, state: object) -> int:
        with self._lock:
            self._state = self._validated_state(state)
            self._epoch += 1
            self._tickets.clear()
            return self._epoch

    def reconcile_observation(self, run: ActiveRunRef | None, state: object,
                              *, observed_epoch: int) -> bool:
        """Check a callback hint without letting it overwrite owner state."""
        validated = self._validated_state(state)
        with self._lock:
            if self._epoch != observed_epoch:
                return False
            return (run == self._run and validated == self._state
                    and self._run is not None and self._state is not None)

    def snapshot(self) -> int:
        with self._lock:
            return self._epoch

    def run_incarnation(self) -> int:
        with self._lock:
            return self._run_incarnation

    def active_run_snapshot(self) -> tuple[ActiveRunRef | None, int]:
        with self._lock:
            return self._run, self._run_incarnation

    def activate_run_source(self, run: ActiveRunRef, source: object) -> ExitSourceToken | None:
        """Atomically activate a run and register its exact persisted WorkflowRun."""
        with self._lock:
            from workbench.observation.workflow_binding import WorkflowObservationBinding
            try:
                resolved = WorkflowObservationBinding.resolve_run(source)
            except (TypeError, ValueError, KeyError, AttributeError):
                return None
            if resolved != run or not run.active:
                return None
            previous = self._external_exit_sources.get(id(source))
            if previous is not None:
                registered = previous()
                if registered is not source:
                    if registered is None:
                        self._external_exit_sources.pop(id(source), None)
                    else:
                        return None
                else:
                    token = self._active_external_token
                    return token if (token is not None
                                 and self._active_external_source is source
                                 and self._run == run
                                 and self._exit_sources.get(token._nonce) is token
                                 and token._run_incarnation == self._run_incarnation) else None
            # A new external source may be issued only on the transition that
            # makes its run authoritative, never as a late first bind to A.
            if self._run == run:
                return None
            self._run = run
            if self._authority_fence is not None:
                self._authority_fence.close()
            self._epoch += 1
            self._run_incarnation += 1
            self._tickets.clear()
            self._exit_sources.clear()
            self._internal_exit_source = None
            self._active_external_source = source
            token = ExitSourceToken(run, object(), self._run_incarnation)
            self._exit_sources[token._nonce] = token
            self._active_external_token = token
            source_id = id(source)
            owner_ref = ref(self)

            def retire_source(source_ref: ReferenceType[object]) -> None:
                owner = owner_ref()
                if owner is not None:
                    with owner._lock:
                        if owner._external_exit_sources.get(source_id) is source_ref:
                            owner._external_exit_sources.pop(source_id, None)

            self._external_exit_sources[source_id] = ref(source, retire_source)
            return token

    def bind_exit_source(self, run: ActiveRunRef) -> ExitSourceToken | None:
        """Issue a source only for facts collected by the current scheduler tick."""
        with self._lock:
            if (not isinstance(run, ActiveRunRef) or self._run is None
                    or self._run != run or not self._run.active
                    or (self._run_incarnation, run.identity) in self._exited):
                return None
            if self._internal_exit_source is not None:
                return self._internal_exit_source
            token = ExitSourceToken(run, object(), self._run_incarnation)
            self._exit_sources[token._nonce] = token
            self._internal_exit_source = token
            return token

    def external_source_current(self, token: ExitSourceToken | None,
                                run: ActiveRunRef, source: object) -> bool:
        with self._lock:
            previous = self._external_exit_sources.get(id(source))
            return (self._active_external_source is source
                    and previous is not None and previous() is source
                    and self._active_external_token is token
                    and isinstance(token, ExitSourceToken)
                    and self._exit_sources.get(token._nonce) is token
                    and self._run == run == token.run
                    and token._run_incarnation == self._run_incarnation)

    def exit_source_current(self, token: ExitSourceToken | None) -> bool:
        with self._lock:
            return (isinstance(token, ExitSourceToken)
                    and self._exit_sources.get(token._nonce) is token
                    and self._run == token.run
                    and token._run_incarnation == self._run_incarnation)

    def ticket_current(self, ticket: ReviewTicket | None) -> bool:
        if not isinstance(ticket, ReviewTicket):
            return False
        with self._lock:
            issued = self._tickets.get(ticket._nonce)
            return (issued is not None and issued[0] is ticket
                    and issued[1] == self._epoch and issued[2] is ticket.request
                    and ticket._run_incarnation == self._run_incarnation)

    def discard_ticket(self, ticket: ReviewTicket | None) -> None:
        if not isinstance(ticket, ReviewTicket):
            return
        with self._lock:
            issued = self._tickets.get(ticket._nonce)
            if issued is not None and issued[0] is ticket:
                self._tickets.pop(ticket._nonce, None)

    def issue_review(self, run: ActiveRunRef, due_at: float,
                     coalesced_count: int, facts: Mapping[str, Any]) -> ReviewTicket | None:
        """Construct and bind a fresh request; caller-owned requests are never admitted."""
        due = _valid_time(due_at)
        if type(coalesced_count) is not int or coalesced_count < 0 or not isinstance(facts, Mapping):
            raise ValueError("invalid review request facts or coalesced count")
        with self._lock:
            if (not isinstance(run, ActiveRunRef) or self._run is None
                    or not self._run.active or self._run != run
                    or (self._run_incarnation, run.identity) in self._exited):
                return None
            ticket = ReviewTicket(
                ReviewRequest(run, due, coalesced_count, MappingProxyType(dict(facts))),
                object(),
                self._run_incarnation,
            )
            self._tickets[ticket._nonce] = (ticket, self._epoch, ticket.request)
            return ticket

    def fence_exit(self, token: ExitSourceToken) -> bool:
        with self._lock:
            if (not isinstance(token, ExitSourceToken)
                    or self._exit_sources.get(token._nonce) is not token
                    or self._run is None or self._run != token.run
                    or token._run_incarnation != self._run_incarnation):
                return False
            key = (token._run_incarnation, token.run.identity)
            if key in self._exited:
                return False
            self._exited.add(key)
            self._epoch += 1
            self._tickets.clear()
            self._exit_sources.clear()
            self._internal_exit_source = None
            self._active_external_source = None
            self._active_external_token = None
            return True

    def admit_and_dispatch(self, ticket: ReviewTicket | ReviewRequest, *,
                           dispatch_review: Callable[[ReviewRequest], object],
                           observed_epoch: int | None = None) -> AdmissionResult:
        """Admit a local callback; actual OMP delivery needs a bound transport adapter."""
        fence_before = self._authority_fence
        preflight = self._peer_preflight
        if fence_before is not None and preflight is not None:
            preflight()
        with self._lock:
            if self._dispatching:
                return AdmissionResult("reentrant")
            # Legacy request+epoch pairing is always invalid, even when the
            # caller presents the owner's current epoch after an A-B-A change.
            if observed_epoch is not None or not isinstance(ticket, ReviewTicket):
                return AdmissionResult("stale")
            issued = self._tickets.get(ticket._nonce)
            if (issued is None or issued[0] is not ticket or issued[1] != self._epoch
                    or issued[2] is not ticket.request
                    or ticket._run_incarnation != self._run_incarnation):
                return AdmissionResult("stale")
            request = ticket.request
            run = self._run
            if run is None or not run.active or run.identity != request.run.identity:
                return AdmissionResult("run_changed")
            if (self._run_incarnation, run.identity) in self._exited:
                return AdmissionResult("exited")
            state = self._state
            if state is None:
                return AdmissionResult("authority_invalid")
            if (state.get("cancelled") is True or state.get("metadataHealthy") is not True
                    or state.get("approvalValid") is not True):
                return AdmissionResult("authority_denied")
            if state.get("paused") is True:
                return AdmissionResult("paused")
            fence = self._authority_fence
            permit = None if fence is None else fence.permit(run, self._run_incarnation)
            if fence is not None and permit is None:
                return AdmissionResult(
                    "reconciliation_needed" if fence.reconciliation_needed() else "paused"
                )
            key = (self._run_incarnation, run.identity, request.due_at)
            if key in self._consumed:
                return AdmissionResult("consumed")
            self._consumed.add(key)
            self._dispatching = True
        self._callback_context.active = True
        try:
            try:
                delivery = dispatch_review(request)
            except BaseException:
                delivery = None
            try:
                status = (delivery.get("status") if isinstance(delivery, Mapping)
                          else getattr(delivery, "value", None))
            except BaseException:
                status = None
        finally:
            self._callback_context.active = False
        with self._lock:
            # A deferred/unknown result from an older authority generation is
            # consumed forever. It cannot requeue after pause or rebind.
            current = (self._epoch == issued[1] and self._run == run
                       and self._run_incarnation == ticket._run_incarnation
                       and (fence is None or permit is not None and fence.current(permit)))
            if status == "deferred" and current:
                self._consumed.discard(key)
            else:
                self._tickets.pop(ticket._nonce, None)
            self._dispatching = False
            if not current:
                return AdmissionResult(
                    status if status in {"omp_processed", "deferred"} else "unknown",
                    current=False,
                )
            return AdmissionResult(status if status in {"omp_processed", "deferred"} else "unknown")


@dataclass(frozen=True, slots=True)
class TickResult:
    status: str
    run_id: str | None
    dispatched: bool
    pending: bool
    coalesced_count: int
    review_count: int
    last_review_at: float | None
    next_due_at: float | None
    reason: str | None = None
    exit_event: Mapping[str, Any] | None = None
    hang_investigation: Mapping[str, Any] | None = None
    usage: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class UsageValue:
    value: int | None
    kind: str
    unit: str = "tokens"

    def __post_init__(self) -> None:
        if self.kind not in {"observed", "estimated", "unknown"}:
            raise ValueError("usage kind must be observed, estimated, or unknown")
        if not isinstance(self.unit, str) or not self.unit or len(self.unit) > 32:
            raise ValueError("usage unit must be a short non-empty string")
        if self.kind == "unknown":
            if self.value is not None:
                raise ValueError("unknown usage must not carry a numeric value")
        elif type(self.value) is not int or self.value < 0:
            raise ValueError("observed and estimated usage require a non-negative integer")


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    unit: str
    observed: int | None
    estimated: int | None
    unknown_samples: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            f"{self.unit}_observed": self.observed if self.observed is not None else "unknown",
            f"{self.unit}_estimated": self.estimated if self.estimated is not None else "unknown",
            f"{self.unit}_unknown": "unknown" if self.unknown_samples else "not_reported",
            "unknown_samples": self.unknown_samples,
        }


class UsageLedger:
    """Unbounded usage accounting that never turns missing observations into zero."""

    def __init__(self, unit: str = "tokens") -> None:
        if not isinstance(unit, str) or not unit or len(unit) > 32:
            raise ValueError("usage unit must be a short non-empty string")
        self._unit = unit
        self._observed_total = 0
        self._estimated_total = 0
        self._saw_observed = False
        self._saw_estimated = False
        self._unknown_samples = 0

    def record(self, usage: UsageValue) -> None:
        if not isinstance(usage, UsageValue) or usage.unit != self._unit:
            raise ValueError("usage value unit does not match this ledger")
        if usage.kind == "observed":
            assert usage.value is not None
            self._observed_total += usage.value
            self._saw_observed = True
        elif usage.kind == "estimated":
            assert usage.value is not None
            self._estimated_total += usage.value
            self._saw_estimated = True
        else:
            self._unknown_samples += 1

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(
            unit=self._unit,
            observed=self._observed_total if self._saw_observed else None,
            estimated=self._estimated_total if self._saw_estimated else None,
            unknown_samples=self._unknown_samples,
        )


class _BoundedInvestigator:
    """Run one read-only investigator at a time without blocking the tick past its budget."""

    def __init__(self, callback: Callable[[ActiveRunRef, float], object]) -> None:
        self._callback = callback
        self._lock = Lock()
        self._in_flight = False

    def run(self, run: ActiveRunRef, timeout_seconds: float) -> tuple[str, object | None]:
        with self._lock:
            if self._in_flight:
                return "in_flight", None
            self._in_flight = True

        completed = Event()
        result: dict[str, object] = {}

        def invoke() -> None:
            try:
                result["value"] = self._callback(run, timeout_seconds)
            except BaseException:
                result["failed"] = True
            finally:
                with self._lock:
                    self._in_flight = False
                completed.set()

        thread = Thread(target=invoke, name="cw11-non-model-investigator", daemon=True)
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._in_flight = False
            return "failed", None
        if not completed.wait(timeout_seconds):
            return "timeout", None
        if result.get("failed") is True:
            return "failed", None
        return "completed", result.get("value")


def _valid_time(value: object) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError("clock must return a finite non-negative number")
    return float(value)


def _sanitize_facts(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"phase": "unknown", "unknown_count": 1}
    result: dict[str, Any] = {}
    for key in _FACT_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key == "phase":
            if isinstance(item, str) and item in _PHASES:
                result[key] = item
        elif key in {"process_alive", "exit_confirmed"}:
            if type(item) is bool:
                result[key] = item
        elif key in {"exit_status"}:
            if item is None or type(item) is int:
                result[key] = item
        elif key == "last_output_age_seconds":
            if type(item) in {int, float} and math.isfinite(item) and item >= 0:
                result[key] = float(item)
        elif type(item) is int and item >= 0:
            result[key] = item
    unknowns = value.get("unknowns")
    if isinstance(unknowns, (list, tuple)):
        result["unknown_count"] = sum(
            isinstance(item, str) and _UNKNOWN_RE.fullmatch(item) is not None
            for item in unknowns
        )
    return result


def _exit_event(run: ActiveRunRef, facts: Mapping[str, Any]) -> dict[str, Any] | None:
    if facts.get("exit_confirmed") is not True:
        return None
    exit_status = facts.get("exit_status")
    if exit_status is not None and type(exit_status) is not int:
        return None
    return {
        "task_id": run.task_id,
        "revision_id": run.revision_id,
        "revision": run.revision,
        "run_id": run.run_id,
        "session_id": run.session_id,
        "session_generation": run.session_generation,
        "exit_confirmed": True,
        "exit_status": exit_status,
    }


def _event_identity(event: Mapping[str, Any], run: ActiveRunRef | None) -> tuple[str, str, int, str, str, int] | None:
    optional_fields = tuple(name for name in _EXIT_IDENTITY_FIELDS if name != "run_id")
    supplied_fields = [name for name in optional_fields if name in event]
    if len(supplied_fields) != len(optional_fields):
        return None
    try:
        supplied = ActiveRunRef(**{name: event[name] for name in _EXIT_IDENTITY_FIELDS})
    except (TypeError, ValueError):
        return None
    if (event.get("run_id") != supplied.run_id
            or (run is not None and (not isinstance(run, ActiveRunRef)
                                     or supplied.identity != run.identity))):
        return None
    return supplied.identity


class WorkerReviewScheduler:
    """Tick-driven periodic review over injected public observation callables.

    ``collect_non_model`` and the optional bounded investigator are called
    before any authority or pause decision, so a local pause can defer model
    work without suppressing process/log/exit reads. Usage observations remain
    provenance-tagged through both review facts and tick status. The optional
    ``investigate_non_model`` callback receives a timeout budget and returns
    process/lifecycle evidence only; ``collect_usage`` returns a ``UsageValue``
    or its public mapping form.
    A dispatch is considered a completed review only after an explicit
    ``omp_processed`` status. Unknown delivery is not replayed implicitly.
    """

    def __init__(
        self,
        *,
        admission: SerializedReviewAdmission,
        automation_state: Callable[[], object],
        active_run: Callable[[], ActiveRunRef | None],
        worker_state: Callable[[ActiveRunRef], object],
        collect_non_model: Callable[[ActiveRunRef], object],
        dispatch_review: Callable[[ReviewRequest], object],
        user_priority: Callable[[ActiveRunRef], bool],
        on_exit: Callable[[Mapping[str, Any]], object],
        clock: Callable[[], float],
        interval_seconds: int = 60,
        investigate_non_model: Callable[[ActiveRunRef, float], object] | None = None,
        investigation_timeout_seconds: int | float = 2.0,
        collect_usage: Callable[[ActiveRunRef], object] | None = None,
    ) -> None:
        if type(interval_seconds) is not int or interval_seconds < 1:
            raise ValueError("interval_seconds must be a positive integer")
        if (type(investigation_timeout_seconds) not in {int, float}
                or not math.isfinite(investigation_timeout_seconds)
                or investigation_timeout_seconds <= 0):
            raise ValueError("investigation_timeout_seconds must be finite and positive")
        if not isinstance(admission, SerializedReviewAdmission):
            raise TypeError("a SerializedReviewAdmission owner is required")
        self.admission = admission
        self._automation_state = automation_state
        self._active_run = active_run
        self._worker_state = worker_state
        self._collect_non_model = collect_non_model
        self._investigator = (
            _BoundedInvestigator(investigate_non_model)
            if investigate_non_model is not None else None
        )
        self._investigation_timeout = float(investigation_timeout_seconds)
        self._collect_usage = collect_usage
        self._dispatch_review = dispatch_review
        self._user_priority = user_priority
        self._on_exit = on_exit
        self._clock = clock
        self._interval = interval_seconds
        self._run_identity: tuple[str, str, int, str, str, int] | None = None
        self._observed_incarnation: int | None = None
        self._next_due_at: float | None = None
        self._pending: ReviewRequest | None = None
        self._pending_ticket: ReviewTicket | None = None
        self._paused_incarnation: int | None = None
        self._review_count = 0
        self._last_review_at: float | None = None
        self._surfaced_exits: dict[_ExitKey, Mapping[str, Any]] = {}
        self._pending_exit_events: dict[_ExitKey, tuple[Mapping[str, Any], ExitSourceToken]] = {}
        self._exit_in_flight: set[_ExitKey] = set()
        self._exit_lock = Lock()
        self._tick_lock = Lock()
        self._usage = UsageLedger()
        self._last_hang_report: Mapping[str, Any] | None = None

    def usage_snapshot(self) -> dict[str, int | str]:
        """C-AC-21: the bound run's model usage so far (missing observations stay ``unknown``, never zero).

        Read from any thread: a run change swaps in a new ledger, so the ledger read here is one run's."""
        ledger = self._usage
        return ledger.snapshot().as_dict()

    def activate_run_source(self, run: ActiveRunRef, source: object) -> ExitSourceToken | None:
        return self.admission.activate_run_source(run, source)

    def bind_exit_source(self, run: ActiveRunRef) -> ExitSourceToken | None:
        return self.admission.bind_exit_source(run)

    def notify_exit_event(self, event: object, *, source: ExitSourceToken | None = None,
                          run: ActiveRunRef | None = None) -> bool:
        """Surface an exit only for an owner-bound incarnation; retry UI failures safely."""
        if not isinstance(source, ExitSourceToken):
            return False
        if not isinstance(event, Mapping):
            return False
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or not run_id or event.get("exit_confirmed") is not True:
            return False
        exit_status = event.get("exit_status")
        if exit_status is not None and type(exit_status) is not int:
            return False
        identity = _event_identity(event, run)
        if identity is None or identity != source.run.identity:
            return False
        key = (source._run_incarnation, identity)
        normalized = {
            "task_id": identity[0],
            "revision_id": identity[1],
            "revision": identity[2],
            "run_id": identity[3],
            "session_id": identity[4],
            "session_generation": identity[5],
            "run_incarnation": source._run_incarnation,
            "exit_confirmed": True,
            "exit_status": exit_status,
        }
        sequence = event.get("event_sequence")
        if type(sequence) is int and sequence > 0:
            normalized["event_sequence"] = sequence
        public_event = MappingProxyType(normalized)

        with self._exit_lock:
            if key in self._surfaced_exits or key in self._exit_in_flight:
                return False
            retrying_pending = key in self._pending_exit_events
            if retrying_pending:
                pending_event, pending_source = self._pending_exit_events[key]
                if pending_source is not source:
                    return False
                public_event = pending_event
            self._exit_in_flight.add(key)

        if not retrying_pending:
            try:
                active = self._active_run()
            except Exception:
                with self._exit_lock:
                    self._exit_in_flight.discard(key)
                return False
            with self._exit_lock:
                known_identity = self._run_identity
            is_current = (
                isinstance(active, ActiveRunRef)
                and active.identity == identity
                and (active.active or known_identity == identity)
            ) or (active is None and known_identity == identity)
            if not is_current:
                with self._exit_lock:
                    self._exit_in_flight.discard(key)
                return False
            observed_epoch = self.admission.snapshot()
            try:
                observed_authority = self._automation_state()
            except Exception:
                observed_authority = None
            self.admission.reconcile_observation(active, observed_authority,
                                                 observed_epoch=observed_epoch)
            # The admission fence precedes the potentially blocking UI callback.
            if not self.admission.fence_exit(source):
                with self._exit_lock:
                    self._exit_in_flight.discard(key)
                return False
            with self._exit_lock:
                if key in self._surfaced_exits:
                    self._exit_in_flight.discard(key)
                    return False
                self._pending_exit_events[key] = (public_event, source)

        try:
            self._on_exit(public_event)
        except Exception:
            with self._exit_lock:
                self._exit_in_flight.discard(key)
            return False

        with self._exit_lock:
            self._surfaced_exits[key] = public_event
            self._pending_exit_events.pop(key, None)
            self._exit_in_flight.discard(key)
            if (identity == self._run_identity and
                    source._run_incarnation == self._observed_incarnation and
                    source._run_incarnation == self.admission.run_incarnation()):
                self._pending = None
                self._pending_ticket = None
        return True

    def tick(self) -> TickResult:
        if not self._tick_lock.acquire(blocking=False):
            return TickResult("busy", None, False, False, 0, 0, None, None,
                              reason="tick_in_flight")
        try:
            return self._tick()
        finally:
            self._tick_lock.release()

    def _tick(self) -> TickResult:
        now = _valid_time(self._clock())
        try:
            run = self._active_run()
        except Exception:
            run = None
        if not isinstance(run, ActiveRunRef) or not run.active:
            # A previously accepted exit remains bound to its immutable run
            # identity even after the active-run provider has cleared it. Keep
            # retrying only those already-pending notifications; never admit a
            # new unbound late event through this path.
            with self._exit_lock:
                pending_exits = tuple(self._pending_exit_events.items())
            for (_, identity), (event, source) in pending_exits:
                pending_run = ActiveRunRef(*identity, active=False)
                self.notify_exit_event(event, source=source, run=pending_run)
            self._reset_run()
            return self._result("inactive", None, now, reason="no_active_run")

        # This token is issued before any further callback can block. A later
        # A->None->A replacement cannot turn its old collected facts into an
        # exit of the new incarnation.
        exit_source = self.bind_exit_source(run)
        incarnation = (exit_source._run_incarnation if exit_source is not None
                       else self.admission.run_incarnation())
        observed_epoch = self.admission.snapshot()
        try:
            observed_authority = self._automation_state()
        except Exception:
            observed_authority = None
        self.admission.reconcile_observation(run, observed_authority,
                                             observed_epoch=observed_epoch)

        with self._exit_lock:
            previous_identity = self._run_identity
        if run.identity != previous_identity or incarnation != self._observed_incarnation:
            self._begin_run(run, incarnation)
        exit_key = (incarnation, run.identity)

        # UI failures remain retryable after replacement, but each retry keeps
        # its original incarnation key and cannot fence or clear new work.
        with self._exit_lock:
            pending_exits = tuple(self._pending_exit_events.items())
        for (_, identity), (event, source) in pending_exits:
            self.notify_exit_event(event, source=source,
                                   run=ActiveRunRef(*identity, active=False))
        with self._exit_lock:
            surfaced_exit = self._surfaced_exits.get(exit_key)
            retry_exit = self._pending_exit_events.get(exit_key)
        if surfaced_exit is not None:
            self._pending = None
            self._pending_ticket = None
            return self._result(
                "exited", run.run_id, now, reason="confirmed_exit",
                exit_event=surfaced_exit,
            )
        if retry_exit is not None:
            self._pending = None
            self._pending_ticket = None
            return self._result("exit_pending", run.run_id, now, reason="exit_surface_retry")

        try:
            collected = self._collect_non_model(run)
        except Exception:
            collected = {"phase": "unknown", "unknowns": ["collection_failed"]}
        facts = _sanitize_facts(collected)
        self._last_hang_report = MappingProxyType(self._investigation_report(run, collected))
        facts["hang_investigation"] = dict(self._last_hang_report)
        if self._last_hang_report.get("classification") == "exit_observed":
            lifecycle = self._last_hang_report.get("lifecycle_evidence", {})
            facts["exit_confirmed"] = True
            facts["exit_status"] = lifecycle.get("exit_status") if isinstance(lifecycle, Mapping) else None
        usage_value = self._usage_observation(run)
        self._usage.record(usage_value)
        usage = self._usage.snapshot().as_dict()
        facts["usage"] = usage
        exit_event = _exit_event(run, facts)
        if exit_event is not None:
            self.notify_exit_event(exit_event, source=exit_source, run=run)
            with self._exit_lock:
                surfaced_exit = self._surfaced_exits.get(exit_key)
                retry_exit = self._pending_exit_events.get(exit_key)
            self._pending = None
            self._pending_ticket = None
            if surfaced_exit is not None:
                return self._result(
                    "exited", run.run_id, now, reason="confirmed_exit",
                    exit_event=surfaced_exit,
                )
            if retry_exit is not None:
                return self._result("exit_pending", run.run_id, now, reason="exit_surface_retry")
            return self._result("held", run.run_id, now, reason="stale_exit_event")

        allowed, state = self._read_authority()

        # Cancellation and invalid authority discard stale automatic work. A
        # pause is different: retain one latest due review, but do not dispatch.
        if not state:
            self._discard_pending()
            return self._result("held", run.run_id, now, reason="automation_state_invalid")
        if state.get("cancelled") is True or state.get("metadataHealthy") is not True or state.get("approvalValid") is not True:
            self._discard_pending()
            return self._result("held", run.run_id, now, reason="automation_not_authorized")

        # Collection and authority callbacks can span a run replacement. Reset
        # before coalescing or issuing a ticket, even for A->None->the same A.
        incarnation = self.admission.run_incarnation()
        if incarnation != self._observed_incarnation:
            self._begin_run(run, incarnation)
        self._advance_schedule(run, now, facts)
        # A writer may replace the run between the preceding read and ticket
        # issuance. Drop any coalescing done across that boundary as well.
        incarnation = self.admission.run_incarnation()
        if incarnation != self._observed_incarnation:
            self._begin_run(run, incarnation)
            self._advance_schedule(run, now, facts)
            return self._result("waiting", run.run_id, now, reason="new_run_incarnation")
        if state.get("paused") is True or not allowed:
            self._paused_incarnation = self.admission.run_incarnation()
            return self._result("paused", run.run_id, now, reason="automation_paused")
        if (self._pending is not None and self._paused_incarnation is not None
                and not self.admission.ticket_current(self._pending_ticket)):
            # A pause may hold a due review, but an A-B-A run replacement may
            # not turn that old request into new work.
            if self._paused_incarnation != self.admission.run_incarnation():
                self._discard_pending()
                return self._result("held", run.run_id, now, reason="active_run_changed")
            self._pending_ticket = self.admission.issue_review(
                run, self._pending.due_at, self._pending.coalesced_count, facts,
            )
            if self._pending_ticket is not None:
                self._pending = self._pending_ticket.request
        self._paused_incarnation = None
        if self._pending is None:
            return self._result("waiting", run.run_id, now, reason="next_interval_not_due")

        try:
            priority_value = self._user_priority(run)
            priority = type(priority_value) is not bool or priority_value
        except Exception:
            priority = True
        guard_result = self._dispatch_guard(run, now)
        if guard_result is not None:
            return guard_result
        if priority:
            return self._result("delayed", run.run_id, now, reason="user_priority")

        try:
            worker = self._worker_state(run)
        except Exception:
            worker = None
        ready, reason = self._worker_ready(run, worker)
        guard_result = self._dispatch_guard(run, now)
        if guard_result is not None:
            return guard_result
        if not ready:
            return self._result("delayed", run.run_id, now, reason=reason)

        request = self._pending
        ticket = self._pending_ticket
        if request is None or ticket is None or request.run.identity != run.identity:
            self._discard_pending()
            return self._result("held", run.run_id, now, reason="review_ticket_unavailable")
        # Callback observations are merely hints. Explicit owner writers win
        # over a stale callback snapshot; admission itself reads no callback.
        outcome = self.admission.admit_and_dispatch(
            ticket, dispatch_review=self._dispatch_review,
        )
        status = outcome.status
        if not outcome.current:
            self._discard_pending()
            return self._result("held", run.run_id, now, reason="admission_authority_changed")
        if status == "omp_processed":
            self._review_count += 1
            self._last_review_at = now
            self._pending = None
            self._pending_ticket = None
            return self._result("dispatched", run.run_id, now, reason=None)
        if status == "deferred":
            return self._result("delayed", run.run_id, now, reason="delivery_deferred")
        if status != "unknown":
            if status in {"run_changed", "authority_denied", "authority_invalid", "exited"}:
                self._discard_pending()
            return self._result("paused" if status == "paused" else "held", run.run_id,
                                now, reason=f"admission_{status}")
        # A non-deferred non-acknowledged result may have reached the provider;
        # retain no handle that could cause accidental duplicate model work.
        self._pending = None
        self._pending_ticket = None
        return self._result("unknown", run.run_id, now, reason="delivery_not_confirmed")

    def _read_authority(self) -> tuple[bool, Mapping[str, Any]]:
        try:
            port = parse_port(self._automation_state())
            if port["kind"] != "AutomationState":
                return False, {}
            allowed = dispatch_allowed(port)
            return allowed, port["payload"]
        except Exception:
            return False, {}

    def _dispatch_guard(self, run: ActiveRunRef, now: float) -> TickResult | None:
        """Re-read both dispatch authorities after callbacks that may take time."""
        observed_epoch = self.admission.snapshot()
        try:
            current = self._active_run()
        except Exception:
            current = None
        # Keep authority last: active_run may block and mutate the automation
        # port while it is in flight. No external callback follows this read.
        allowed, state = self._read_authority()
        observed_port = ({"portVersion": 2, "kind": "AutomationState", "payload": dict(state)}
                         if state else None)
        reconciled = self.admission.reconcile_observation(
            current, observed_port, observed_epoch=observed_epoch,
        )
        if not reconciled:
            self._discard_pending()
            return self._result("held", run.run_id, now, reason="observation_owner_mismatch")

        if not state:
            self._discard_pending()
            return self._result("held", run.run_id, now, reason="automation_state_invalid")
        if (state.get("cancelled") is True
                or state.get("metadataHealthy") is not True
                or state.get("approvalValid") is not True):
            self._discard_pending()
            return self._result("held", run.run_id, now, reason="automation_not_authorized")
        if not isinstance(current, ActiveRunRef) or not current.active:
            self._discard_pending()
            return self._result("inactive", run.run_id, now, reason="no_active_run")
        if current.identity != run.identity:
            self._discard_pending()
            return self._result("held", run.run_id, now, reason="active_run_changed")
        with self._exit_lock:
            exit_key = (self._observed_incarnation, run.identity)
            surfaced_exit = self._surfaced_exits.get(exit_key)
            retry_exit = self._pending_exit_events.get(exit_key)
        if surfaced_exit is not None:
            self._pending = None
            self._pending_ticket = None
            return self._result(
                "exited", run.run_id, now, reason="confirmed_exit",
                exit_event=surfaced_exit,
            )
        if retry_exit is not None:
            self._pending = None
            self._pending_ticket = None
            return self._result("exit_pending", run.run_id, now, reason="exit_surface_retry")
        if state.get("paused") is True or not allowed:
            self._paused_incarnation = self.admission.run_incarnation()
            return self._result("paused", run.run_id, now, reason="automation_paused")
        return None

    def _investigation_report(self, run: ActiveRunRef, collected: object) -> dict[str, Any]:
        if self._investigator is None:
            status, evidence = "collected_non_model_facts", collected
            source = "non_model_collector"
        else:
            status, evidence = self._investigator.run(run, self._investigation_timeout)
            source = "bounded_non_model_investigator"

        if not isinstance(evidence, Mapping):
            evidence = {}
            if status in {"completed", "collected_non_model_facts"}:
                status = "invalid_evidence"

        process_evidence = evidence.get("process_evidence", ())
        lifecycle_evidence = evidence.get("lifecycle_evidence")
        if not isinstance(lifecycle_evidence, Mapping):
            lifecycle_evidence = {
                key: evidence[key]
                for key in ("phase", "exit_confirmed", "exit_status", "lifetime_ended", "event_sequence")
                if key in evidence
            }
        raw_unknowns = evidence.get("unknowns", ())
        candidate_unknowns = _safe_unknowns(raw_unknowns)
        unknowns = [value for value in candidate_unknowns if value in _SAFE_UNKNOWN_CODES]
        raw_count = len(raw_unknowns) if isinstance(raw_unknowns, (list, tuple)) else 0
        if (raw_count != len(candidate_unknowns)
                or any(value not in _SAFE_UNKNOWN_CODES for value in candidate_unknowns)):
            unknowns.append("non_model_unknown_present")
        status_unknowns = {
            "timeout": "investigation_timeout",
            "in_flight": "investigation_in_flight",
            "failed": "investigation_failed",
            "invalid_evidence": "investigation_invalid",
        }
        if status in status_unknowns:
            unknowns.append(status_unknowns[status])
        report = investigate_hang(
            run.run_id,
            process_evidence=process_evidence,
            lifecycle_evidence=lifecycle_evidence,
            silence_seconds=evidence.get("silence_seconds"),
            unknowns=unknowns,
        )
        report["source"] = source
        report["investigation_status"] = status
        report["bounded"] = self._investigator is not None
        if self._investigator is not None:
            report["timeout_seconds"] = self._investigation_timeout
        return report

    def _usage_observation(self, run: ActiveRunRef) -> UsageValue:
        if self._collect_usage is None:
            return UsageValue(None, "unknown")
        try:
            value = self._collect_usage(run)
        except Exception:
            return UsageValue(None, "unknown")
        if isinstance(value, UsageValue):
            return value if value.unit == "tokens" else UsageValue(None, "unknown")
        if not isinstance(value, Mapping):
            return UsageValue(None, "unknown")
        try:
            return UsageValue(
                value=value.get("value"),
                kind=value.get("kind"),
                unit=value.get("unit", "tokens"),
            )
        except (TypeError, ValueError):
            return UsageValue(None, "unknown")

    def _begin_run(self, run: ActiveRunRef, incarnation: int) -> None:
        with self._exit_lock:
            self._run_identity = run.identity
        self._observed_incarnation = incarnation
        self._next_due_at = None
        self.admission.discard_ticket(self._pending_ticket)
        self._pending = None
        self._pending_ticket = None
        self._paused_incarnation = None
        self._review_count = 0
        self._last_review_at = None
        self._usage = UsageLedger()
        self._last_hang_report = None

    def _reset_run(self) -> None:
        with self._exit_lock:
            self._run_identity = None
        self._observed_incarnation = None
        self._next_due_at = None
        self.admission.discard_ticket(self._pending_ticket)
        self._pending = None
        self._pending_ticket = None
        self._paused_incarnation = None
        self._review_count = 0
        self._last_review_at = None
        self._usage = UsageLedger()
        self._last_hang_report = None

    def _discard_pending(self) -> None:
        self.admission.discard_ticket(self._pending_ticket)
        self._pending = None
        self._pending_ticket = None
        self._paused_incarnation = None
        self._next_due_at = None

    def _advance_schedule(self, run: ActiveRunRef, now: float, facts: Mapping[str, Any]) -> None:
        if self._next_due_at is None:
            self._next_due_at = now + self._interval
            return
        if now < self._next_due_at:
            return
        due_count = int((now - self._next_due_at) // self._interval) + 1
        latest_due = self._next_due_at + (due_count - 1) * self._interval
        self._next_due_at += due_count * self._interval
        if self._pending is None:
            coalesced = max(0, due_count - 1)
        else:
            coalesced = self._pending.coalesced_count + due_count
        self.admission.discard_ticket(self._pending_ticket)
        self._pending_ticket = self.admission.issue_review(run, latest_due, coalesced, facts)
        self._pending = self._pending_ticket.request if self._pending_ticket is not None else None

    def _worker_ready(self, run: ActiveRunRef, value: object) -> tuple[bool, str | None]:
        if not isinstance(value, Mapping):
            return False, "worker_state_unknown"
        if (value.get("role") != "worker"
                or value.get("sessionId") != run.session_id
                or type(value.get("generation")) is not int
                or value.get("generation") != run.session_generation):
            return False, "worker_session_mismatch"
        if (value.get("idle") is not True
                or value.get("pending") is not False
                or value.get("approvalPending") is not False
                or value.get("editorKnown") is not True
                or value.get("editorEmpty") is not True
                or type(value.get("inFlightToolCount")) is not int
                or value.get("inFlightToolCount") != 0
                or value.get("paused") is not False):
            return False, "worker_busy_or_unknown"
        return True, None

    def _result(
        self,
        status: str,
        run_id: str | None,
        now: float,
        *,
        reason: str | None = None,
        exit_event: Mapping[str, Any] | None = None,
    ) -> TickResult:
        del now  # retained in the signature to keep each transition clock-explicit
        pending = self._pending
        return TickResult(
            status=status,
            run_id=run_id,
            dispatched=status == "dispatched",
            pending=pending is not None,
            coalesced_count=pending.coalesced_count if pending else 0,
            review_count=self._review_count,
            last_review_at=self._last_review_at,
            next_due_at=self._next_due_at,
            reason=reason,
            exit_event=MappingProxyType(dict(exit_event)) if exit_event else None,
            hang_investigation=self._last_hang_report,
            usage=MappingProxyType(self._usage.snapshot().as_dict()),
        )


def _safe_unknowns(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [value for value in values if isinstance(value, str) and _UNKNOWN_RE.fullmatch(value)]


def _safe_process(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    pid = value.get("pid")
    if type(pid) is int and pid > 0:
        result["pid"] = pid
    alive, stalled = value.get("alive"), value.get("stalled")
    if type(alive) is bool:
        result["alive"] = alive
    if type(stalled) is bool:
        result["stalled"] = stalled
    state = value.get("state")
    if isinstance(state, str) and state in {"R", "S", "D", "T", "Z", "X", "I"}:
        result["state"] = state
    blocked_on = value.get("blocked_on")
    if isinstance(blocked_on, str) and blocked_on in _BLOCKED_ON:
        result["blocked_on"] = blocked_on
    return result or None


def investigate_hang(
    run_id: str,
    *,
    process_evidence: object,
    lifecycle_evidence: object,
    silence_seconds: int | float | None = None,
    unknowns: object = (),
) -> dict[str, Any]:
    """Classify only explicit lifecycle/process facts; silence is informational."""
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        raise ValueError("run_id must be a non-empty identifier")
    raw_processes = process_evidence if isinstance(process_evidence, (list, tuple)) else ()
    processes = [safe for item in raw_processes if (safe := _safe_process(item)) is not None]
    lifecycle_source = lifecycle_evidence if isinstance(lifecycle_evidence, Mapping) else {}
    lifecycle: dict[str, Any] = {}
    phase = lifecycle_source.get("phase")
    if isinstance(phase, str) and phase in _PHASES | {"lifetime_ended"}:
        lifecycle["phase"] = phase
    for name in ("exit_confirmed", "lifetime_ended"):
        if type(lifecycle_source.get(name)) is bool:
            lifecycle[name] = lifecycle_source[name]
    exit_status = lifecycle_source.get("exit_status")
    if exit_status is None or type(exit_status) is int:
        if "exit_status" in lifecycle_source:
            lifecycle["exit_status"] = exit_status
    if type(lifecycle_source.get("event_sequence")) is int and lifecycle_source["event_sequence"] > 0:
        lifecycle["event_sequence"] = lifecycle_source["event_sequence"]

    explicit_exit = (
        lifecycle.get("exit_confirmed") is True
        and lifecycle.get("lifetime_ended") is True
        and lifecycle.get("phase") in {"exited", "lifetime_ended"}
    )
    explicit_hang = (
        lifecycle.get("phase") == "running"
        and any(item.get("alive") is True
                and item.get("stalled") is True
                and item.get("blocked_on") in _BLOCKED_ON - {"unknown"}
                for item in processes)
    )
    if explicit_exit:
        classification, basis = "exit_observed", ["lifecycle exit confirmation"]
    elif explicit_hang:
        classification, basis = "suspected_hang", ["process evidence", "running lifecycle"]
    else:
        classification, basis = "unknown", []
    silence = None
    if (silence_seconds is not None and type(silence_seconds) in {int, float}
            and math.isfinite(silence_seconds) and silence_seconds >= 0):
        silence = float(silence_seconds)
    next_observations = []
    if not any(item.get("alive") is not None for item in processes):
        next_observations.append("inspect_process_tree")
    if lifecycle.get("phase") not in {"exited", "lifetime_ended"}:
        next_observations.append("collect_lifecycle_and_exit_event")
    if not next_observations:
        next_observations.append("continue_non_model_collection")
    return {
        "run_id": run_id,
        "classification": classification,
        "basis": basis,
        "process_evidence": processes,
        "lifecycle_evidence": lifecycle,
        "silence_seconds": silence,
        "unknowns": _safe_unknowns(unknowns),
        "suggested_next_observations": next_observations,
    }


__all__ = [
    "ActiveRunRef",
    "ReviewRequest",
    "TickResult",
    "UsageLedger",
    "UsageSnapshot",
    "UsageValue",
    "WorkerReviewScheduler",
    "investigate_hang",
]
