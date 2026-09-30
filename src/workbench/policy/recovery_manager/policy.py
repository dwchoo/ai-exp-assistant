"""Fail-closed, side-effect-free action plans for Manager recovery decisions.

The host adapter owns CW-10/CW-11/CW-12 observations and every external action.
Only recovery-attempt reservation crosses this boundary; it must be an atomic,
task-wide compare-and-set before a retry plan is returned.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any, Mapping, Protocol
from uuid import UUID

from workbench.contracts.ports_v2 import dispatch_allowed


_HASH_LEN = 64
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_TEXT = 4096
_MAX_ITEMS = 128
_MAX_RETRIES = 3


def _hash(value: object) -> bool:
    return (type(value) is str and len(value) == _HASH_LEN
            and all(char in "0123456789abcdef" for char in value))


def _text(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= _MAX_TEXT


def _uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _relative_path(value: object) -> bool:
    if not _text(value) or value.startswith("/") or "\\" in value:
        return False
    parts = value.rstrip("/").split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _covered(path: str, approved_paths: tuple[str, ...]) -> bool:
    target = PurePosixPath(path.rstrip("/"))
    return any(target == PurePosixPath(approved.rstrip("/")) or
               approved.endswith("/") and target.is_relative_to(
                   PurePosixPath(approved.rstrip("/"))) for approved in approved_paths)


def _strings(values: object, *, allow_empty: bool = True) -> tuple[str, ...]:
    if (not isinstance(values, (tuple, list)) or len(values) > _MAX_ITEMS
            or any(not _text(value) for value in values)
            or not allow_empty and not values):
        raise ValueError("bounded non-empty strings required")
    return tuple(values)


@dataclass(frozen=True, slots=True)
class RunIdentity:
    task_id: str
    revision: int
    run_id: str

    def __post_init__(self) -> None:
        if (not _uuid(self.task_id) or type(self.revision) is not int
                or self.revision < 1 or not _uuid(self.run_id)):
            raise ValueError("exact Task/revision/run identity required")


@dataclass(frozen=True, slots=True)
class CompletionCriteria:
    log_contains: str
    result_file: str
    result_contains: str

    def __post_init__(self) -> None:
        if (not _text(self.log_contains) or not _relative_path(self.result_file)
                or self.result_file.endswith("/") or not _text(self.result_contains)):
            raise ValueError("approved completion criteria are invalid")


@dataclass(frozen=True, slots=True)
class Artifact:
    """A fresh, bounded manager observation with CW-10 collection provenance."""

    source: str
    path: str
    content: bytes | None
    collected_sha256: str | None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.source not in {"raw_log", "result_file"} or not _text(self.path):
            raise ValueError("artifact source/path is invalid")
        if self.content is not None and type(self.content) is not bytes:
            raise ValueError("artifact content must be bytes")
        if self.content is not None and len(self.content) > _MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds its observation bound")
        if self.collected_sha256 is not None and not _hash(self.collected_sha256):
            raise ValueError("collection digest is invalid")
        if self.error is not None and not _text(self.error):
            raise ValueError("artifact error is invalid")
        if self.content is None and self.error is None:
            raise ValueError("missing artifact needs an explicit error")
        if self.content is not None and self.error is not None:
            raise ValueError("artifact cannot contain data and an error")

    @property
    def digest(self) -> str | None:
        return None if self.content is None else sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkerJudgment:
    identity: RunIdentity
    criteria: CompletionCriteria
    judgment: str
    reasons: tuple[str, ...]
    unknowns: tuple[str, ...]
    evidence_errors: tuple[str, ...]
    exit_status: int | None
    exit_confirmed: bool
    raw_log_path: str
    raw_log_sha256: str | None
    result_path: str
    result_sha256: str | None
    provenance: str
    report_id: str
    requires_code_change: bool = False
    code_change_reason: str = ""

    def __post_init__(self) -> None:
        if (self.judgment not in {"success", "failure", "indeterminate"}
                or not isinstance(self.identity, RunIdentity)
                or not isinstance(self.criteria, CompletionCriteria)
                or self.provenance not in {"durable_worker_report", "omp_assistant_response"}
                or not _text(self.report_id)
                or not _text(self.raw_log_path) or not _text(self.result_path)
                or type(self.exit_confirmed) is not bool
                or self.exit_status is not None and type(self.exit_status) is not int
                or self.raw_log_sha256 is not None and not _hash(self.raw_log_sha256)
                or self.result_sha256 is not None and not _hash(self.result_sha256)
                or type(self.requires_code_change) is not bool
                or self.requires_code_change and not _text(self.code_change_reason)):
            raise ValueError("worker judgment is incomplete or malformed")
        object.__setattr__(self, "reasons", _strings(self.reasons, allow_empty=False))
        object.__setattr__(self, "unknowns", _strings(self.unknowns))
        object.__setattr__(self, "evidence_errors", _strings(self.evidence_errors))

    @classmethod
    def from_cw11_report(cls, report: Mapping[str, Any], *, report_id: str,
                         provenance: str) -> WorkerJudgment:
        """Validate the persisted CW-11 report shape before Manager comparison."""
        if not isinstance(report, Mapping):
            raise ValueError("worker report is missing")
        required = {"task_id", "revision", "run_id", "criteria", "judgment", "reasons",
                    "unknowns", "evidence_errors", "exit_status", "exit_confirmed",
                    "raw_log", "raw_log_sha256", "result_file", "result_sha256",
                    "requires_code_change", "code_change_reason"}
        if not required <= report.keys() or not isinstance(report["criteria"], Mapping):
            raise ValueError("worker report is incomplete")
        criteria = report["criteria"]
        if set(criteria) != {"log_contains", "result_file", "result_contains"}:
            raise ValueError("worker criteria are malformed")
        errors = report["evidence_errors"]
        if (not isinstance(errors, (list, tuple)) or len(errors) > _MAX_ITEMS
                or any(not isinstance(item, Mapping)
                       or set(item) != {"source", "path", "error"}
                       or not all(_text(item[key]) for key in item) for item in errors)):
            raise ValueError("worker evidence errors are malformed")
        response = report.get("worker_response")
        if provenance == "omp_assistant_response" and not isinstance(response, Mapping):
            raise ValueError("OMP worker response provenance is missing")
        if response is not None and (not isinstance(response, Mapping)
                or response.get("source") != "omp_assistant_response"
                or response.get("decision") != report["judgment"]
                or response.get("task_id") != report["task_id"]
                or response.get("revision") != report["revision"]
                or response.get("run_id") != report["run_id"]
                or response.get("stage") != "analysis"):
            raise ValueError("OMP worker response identity is inconsistent")
        return cls(
            identity=RunIdentity(report["task_id"], report["revision"], report["run_id"]),
            criteria=CompletionCriteria(**criteria), judgment=report["judgment"],
            reasons=_strings(report["reasons"], allow_empty=False),
            unknowns=_strings(report["unknowns"]),
            evidence_errors=tuple(f"{item['source']} {item['path']}: {item['error']}"
                                  for item in errors),
            exit_status=report["exit_status"], exit_confirmed=report["exit_confirmed"],
            raw_log_path=report["raw_log"], raw_log_sha256=report["raw_log_sha256"],
            result_path=report["result_file"], result_sha256=report["result_sha256"],
            provenance=provenance, report_id=report_id,
            requires_code_change=report["requires_code_change"],
            code_change_reason=report["code_change_reason"],
        )


@dataclass(frozen=True, slots=True)
class ForceTarget:
    run_id: str
    pid: int
    start_ticks: int
    owner_epoch: int

    def __post_init__(self) -> None:
        if (not _uuid(self.run_id) or any(type(value) is not int or value < 1 for value in
                (self.pid, self.start_ticks, self.owner_epoch))):
            raise ValueError("exact owned process identity required")


@dataclass(frozen=True, slots=True)
class OrdinaryStopProof:
    """Owner-attested ordinary stop request bound to one run and exact target."""

    identity: RunIdentity
    target: ForceTarget
    request_id: str
    request_sequence: int
    source: str = "owned_terminal_control"

    def __post_init__(self) -> None:
        if (not isinstance(self.identity, RunIdentity)
                or not isinstance(self.target, ForceTarget)
                or self.target.run_id != self.identity.run_id
                or not _uuid(self.request_id)
                or type(self.request_sequence) is not int or self.request_sequence < 1
                or self.source != "owned_terminal_control"):
            raise ValueError("ordinary stop proof is malformed or unowned")


@dataclass(frozen=True, slots=True)
class TargetLiveness:
    """Owner-observed liveness of one exact target at one observation sequence."""

    target: ForceTarget
    observed_sequence: int
    alive: bool | None
    source: str = "owned_terminal_observation"

    def __post_init__(self) -> None:
        if (not isinstance(self.target, ForceTarget)
                or type(self.observed_sequence) is not int or self.observed_sequence < 1
                or self.alive is not None and type(self.alive) is not bool
                or self.source != "owned_terminal_observation"):
            raise ValueError("target liveness proof is malformed or unowned")


@dataclass(frozen=True, slots=True)
class RunObservation:
    """CW-10 managed-execution state, not merely the main process exit state.

    A known main exit_status may coexist with running/exit_confirmed=False while
    descendants remain. Exited/True means the full managed lifetime ended and
    input/control returned; a main-process return alone is not confirmation.
    """

    identity: RunIdentity
    sequence: int
    shell_state: str
    exit_status: int | None
    exit_confirmed: bool
    lifetime: str
    descendants_clear: bool | None
    input_returned: bool
    control_returned: bool
    stop_requested: bool
    observed_target: ForceTarget | None = None
    owned_target_confirmed: bool = False
    last_force_sequence: int | None = None
    stop_proof: OrdinaryStopProof | None = None
    target_liveness: TargetLiveness | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.identity, RunIdentity) or type(self.sequence) is not int
                or self.sequence < 1 or self.shell_state not in {"running", "exited", "unknown"}
                or self.lifetime not in {"running", "ended", "unknown"}
                or self.exit_status is not None and type(self.exit_status) is not int
                or self.descendants_clear is not None and type(self.descendants_clear) is not bool
                or any(type(value) is not bool for value in
                       (self.exit_confirmed, self.input_returned, self.control_returned,
                        self.stop_requested, self.owned_target_confirmed))
                or self.observed_target is not None and not isinstance(self.observed_target, ForceTarget)
                or self.target_liveness is not None and
                not isinstance(self.target_liveness, TargetLiveness)
                or self.stop_proof is not None and
                (not isinstance(self.stop_proof, OrdinaryStopProof)
                 or self.stop_proof.identity != self.identity or not self.stop_requested)
                or self.last_force_sequence is not None and
                (type(self.last_force_sequence) is not int or self.last_force_sequence < 1)):
            raise ValueError("terminal observation is malformed")

    @property
    def fully_terminated(self) -> bool:
        return (self.shell_state == "exited" and self.exit_confirmed
                and self.lifetime == "ended" and self.descendants_clear is True
                and self.input_returned and self.control_returned
                and (self.last_force_sequence is None
                     or self.sequence > self.last_force_sequence))


@dataclass(frozen=True, slots=True)
class RetryAttempt:
    task_id: str
    attempt_id: str
    kind: str

    def __post_init__(self) -> None:
        if (not _uuid(self.task_id) or not _text(self.attempt_id)
                or self.kind not in {"initial", "normal_improvement", "recovery"}):
            raise ValueError("retry history is malformed")


@dataclass(frozen=True, slots=True)
class RecoveryFacts:
    identity: RunIdentity
    current_identity: RunIdentity
    approval_hash: str
    current_approval_hash: str
    criteria: CompletionCriteria
    approved_paths: tuple[str, ...]
    automation_state: Mapping[str, Any]
    revoked: bool
    metadata_healthy: bool
    terminal_owner: str
    settings_revision: int
    authority_version: int
    observation: RunObservation
    worker: WorkerJudgment | None
    raw_log: Artifact | None
    result: Artifact | None
    worktree_root: str
    raw_log_path: str
    attempts: tuple[RetryAttempt, ...]
    delegated_force_target: ForceTarget | None = None
    source_commit: str = ""
    task_name: str = ""
    _automation_allowed: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.identity, RunIdentity)
                or not isinstance(self.current_identity, RunIdentity)
                or not _hash(self.approval_hash) or not _hash(self.current_approval_hash)
                or not isinstance(self.criteria, CompletionCriteria)
                or type(self.revoked) is not bool or type(self.metadata_healthy) is not bool
                or self.terminal_owner not in {"workbench", "user", "unknown"}
                or type(self.settings_revision) is not int or self.settings_revision < 1
                or type(self.authority_version) is not int or self.authority_version < 1
                or not isinstance(self.observation, RunObservation)
                or self.worker is not None and not isinstance(self.worker, WorkerJudgment)
                or self.raw_log is not None and not isinstance(self.raw_log, Artifact)
                or self.result is not None and not isinstance(self.result, Artifact)
                or not _text(self.worktree_root) or not self.worktree_root.startswith("/")
                or not _text(self.raw_log_path) or not self.raw_log_path.startswith("/")
                or self.delegated_force_target is not None and
                not isinstance(self.delegated_force_target, ForceTarget)
                or type(self.source_commit) is not str or len(self.source_commit) > _MAX_TEXT
                or type(self.task_name) is not str or len(self.task_name) > _MAX_TEXT):
            raise ValueError("recovery facts are malformed")
        paths = _strings(self.approved_paths, allow_empty=False)
        if any(not _relative_path(path) for path in paths):
            raise ValueError("approved scope path is invalid")
        object.__setattr__(self, "approved_paths", paths)
        if (not isinstance(self.attempts, tuple) or len(self.attempts) > _MAX_ITEMS
                or any(not isinstance(item, RetryAttempt) for item in self.attempts)
                or len({item.attempt_id for item in self.attempts}) != len(self.attempts)):
            raise ValueError("task-wide retry history is malformed")
        try:
            automation_allowed = dispatch_allowed(self.automation_state)
        except Exception:
            automation_allowed = False
        object.__setattr__(self, "_automation_allowed", automation_allowed)

    @property
    def retries_used(self) -> int:
        return sum(item.task_id == self.identity.task_id and item.kind == "recovery"
                   for item in self.attempts)


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    identity: RunIdentity
    decision_id: str
    kind: str = "evaluate"
    expected_settings_revision: int = 1
    mutation_paths: tuple[str, ...] = ()
    target_revision: int | None = None
    apply_current_run: bool = False
    force_target: ForceTarget | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.identity, RunIdentity) or not _text(self.decision_id)
                or self.kind not in {"evaluate", "recover", "apply_revision", "stop", "force"}
                or type(self.expected_settings_revision) is not int
                or self.expected_settings_revision < 1
                or self.target_revision is not None and
                (type(self.target_revision) is not int or self.target_revision < 1)
                or type(self.apply_current_run) is not bool
                or self.force_target is not None and not isinstance(self.force_target, ForceTarget)):
            raise ValueError("recovery request is malformed")
        paths = _strings(self.mutation_paths)
        if any(not _relative_path(path) for path in paths):
            raise ValueError("mutation paths are invalid")
        object.__setattr__(self, "mutation_paths", paths)


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    identity: RunIdentity
    outcome: str
    sources: tuple[str, ...]
    criteria: CompletionCriteria
    reasons: tuple[str, ...]
    evidence_errors: tuple[str, ...]
    unknowns: tuple[str, ...]
    attempts_used: int
    remaining_problems: tuple[str, ...]
    settings_revision: int


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: str
    steps: tuple[str, ...]
    report: EvidenceReport
    authority_version: int
    retry_ordinal: int | None = None


class RecoveryPort(Protocol):
    def read(self, identity: RunIdentity) -> RecoveryFacts:
        """Read fresh durable CW-10/11/12 facts, including every Task attempt."""

    def reserve_recovery(self, *, task_id: str, decision_id: str, expected_used: int,
                         expected_authority_version: int,
                         expected_settings_revision: int, maximum: int) -> bool:
        """Atomically reserve once per Task; false on duplicate/stale/limit."""


class RecoveryCoordinator:
    """Plan only; never sends OMP input, mutates source, relaunches, or kills."""

    def __init__(self, port: RecoveryPort, *, max_recovery_retries: int = _MAX_RETRIES):
        if type(max_recovery_retries) is not int or max_recovery_retries < 0:
            raise ValueError("recovery maximum must be nonnegative")
        self._port = port
        self._maximum = max_recovery_retries

    def decide(self, request: RecoveryRequest) -> Decision:
        if not isinstance(request, RecoveryRequest):
            raise TypeError("RecoveryRequest required")
        facts = self._port.read(request.identity)
        if not isinstance(facts, RecoveryFacts):
            raise ValueError("current recovery facts are unavailable")
        facts = deepcopy(facts)  # Keep pre-CAS evidence stable across an in-place port update.
        reasons: list[str] = []
        errors: list[str] = []
        unknowns: list[str] = []
        sources: list[str] = []
        worker = facts.worker
        if worker is not None:
            sources.append(f"worker:{worker.provenance}:{worker.report_id}")
            reasons.extend(worker.reasons)
            errors.extend(worker.evidence_errors)
            unknowns.extend(worker.unknowns)
        for artifact in (facts.raw_log, facts.result):
            if artifact is not None:
                sources.append(f"{artifact.source}:{artifact.path}:{artifact.digest or artifact.error}")
                if artifact.error:
                    errors.append(f"{artifact.source}: {artifact.error}")
        sources.append(f"terminal:sequence:{facts.observation.sequence}")
        sources.append(f"approval:{facts.approval_hash}")

        def result(outcome: str, problems: tuple[str, ...], steps: tuple[str, ...] = (),
                   ordinal: int | None = None,
                   attempts_used: int | None = None) -> Decision:
            return Decision(outcome, steps, EvidenceReport(
                identity=facts.identity, outcome=outcome, sources=tuple(sources),
                criteria=facts.criteria, reasons=tuple(reasons),
                evidence_errors=tuple(errors), unknowns=tuple(unknowns),
                attempts_used=(facts.retries_used if attempts_used is None else attempts_used),
                remaining_problems=problems,
                settings_revision=facts.settings_revision,
            ), facts.authority_version, ordinal)

        if (request.identity != facts.identity or facts.current_identity != facts.identity
                or facts.observation.identity != facts.identity
                or worker is not None and worker.identity != facts.identity):
            return result("blocked_unresolved", ("identity_mismatch",))
        if facts.approval_hash != facts.current_approval_hash:
            return result("blocked_unresolved", ("approval_changed",))
        if request.expected_settings_revision != facts.settings_revision:
            return result("blocked_unresolved", ("settings_revision_changed",))
        if facts.revoked:
            if facts.observation.fully_terminated:
                return result("blocked_unresolved", ("authority_revoked",))
            steps = (("wait_full_termination",) if facts.observation.stop_requested else
                     ("request_stop", "wait_full_termination"))
            return result("termination_required", ("authority_revoked",), steps)
        if not facts.metadata_healthy:
            return result("blocked_unresolved", ("metadata_unhealthy",))
        if not facts._automation_allowed:
            return result("blocked_unresolved", ("automation_not_authorized",))
        if facts.terminal_owner != "workbench":
            return result("blocked_unresolved", ("terminal_owner_unavailable",))
        observation = facts.observation
        if request.kind == "force":
            if (observation.last_force_sequence is not None
                    and observation.sequence <= observation.last_force_sequence):
                return result("blocked_unresolved", ("post_force_observation_required",))
            target = request.force_target
            if (target is None or facts.delegated_force_target != target
                    or observation.observed_target != target
                    or not observation.owned_target_confirmed
                    or target.run_id != facts.identity.run_id):
                return result("force_confirmation_required", ("force_target_unconfirmed",))
            if observation.fully_terminated:
                return result("blocked_unresolved", ("already_terminated",))
            proof = observation.stop_proof
            if proof is None:
                if observation.stop_requested:
                    return result("force_confirmation_required", ("ordinary_stop_proof_missing",))
                return result("termination_required", ("ordinary_stop_required",),
                              ("request_stop", "observe_after_stop"))
            if proof.target != target:
                return result("force_confirmation_required", ("ordinary_stop_target_changed",))
            if observation.sequence <= proof.request_sequence:
                return result("termination_required", ("post_stop_observation_required",),
                              ("observe_after_stop",))
            if (observation.shell_state != "running" or observation.exit_confirmed is not False
                    or observation.lifetime != "running"
                    or observation.descendants_clear is not False):
                return result("force_confirmation_required", ("non_termination_unconfirmed",))
            liveness = observation.target_liveness
            if (liveness is None or liveness.target != target
                    or liveness.observed_sequence != observation.sequence
                    or liveness.alive is not True):
                return result("force_confirmation_required", ("target_liveness_unconfirmed",))
            return result("force_requested", ("termination_not_yet_confirmed",),
                          ("force_exact_owned_target", "observe_full_termination"))
        if request.kind == "stop":
            if observation.fully_terminated:
                return result("blocked_unresolved", ("already_terminated",))
            steps = (("wait_full_termination",) if observation.stop_requested else
                     ("request_stop", "wait_full_termination"))
            return result("termination_required", ("termination_not_yet_confirmed",), steps)
        if request.kind == "apply_revision":
            if request.target_revision is None or request.target_revision <= facts.identity.revision:
                return result("blocked_unresolved", ("new_revision_required",))
            if not request.apply_current_run:
                return result("deferred_next_run", ("current_run_unchanged",))
            if not observation.fully_terminated:
                steps = (("wait_full_termination",) if observation.stop_requested else
                         ("request_stop", "wait_full_termination"))
                return result("termination_required", ("termination_not_yet_confirmed",), steps)
            if not all(_covered(path, facts.approved_paths)
                       for path in request.mutation_paths):
                return result("blocked_unresolved", ("mutation_outside_approved_scope",))
            steps = (("apply_new_revision", "mutate_approved_sources")
                     if request.mutation_paths else ("apply_new_revision",))
            return result("revision_relaunch", (),
                          steps + ("send_new_worker_instruction", "relaunch"))
        if not observation.fully_terminated:
            unknowns.append("full_termination_unconfirmed")
            return result("blocked_unresolved", ("full_termination_unconfirmed",))
        if (worker is None or worker.criteria != facts.criteria
                or worker.exit_status != observation.exit_status
                or not worker.exit_confirmed):
            return result("additional_investigation", ("worker_evidence_missing_or_mismatched",))
        if worker.unknowns or worker.evidence_errors:
            return result("additional_investigation", ("worker_evidence_unresolved",))
        if observation.exit_status is None:
            return result("additional_investigation", ("exit_status_unknown",))
        raw_log, observed_result = facts.raw_log, facts.result
        if (raw_log is None or observed_result is None
                or raw_log.source != "raw_log" or observed_result.source != "result_file"
                or raw_log.path != facts.raw_log_path
                or observed_result.path != str(PurePosixPath(facts.worktree_root) /
                                               facts.criteria.result_file)
                or raw_log.path != worker.raw_log_path
                or observed_result.path != worker.result_path
                or raw_log.error or observed_result.error
                or raw_log.digest is None or observed_result.digest is None
                or raw_log.digest != raw_log.collected_sha256
                or observed_result.digest != observed_result.collected_sha256
                or raw_log.digest != worker.raw_log_sha256
                or observed_result.digest != worker.result_sha256):
            return result("additional_investigation", ("artifact_missing_or_changed",))
        if not raw_log.content or not observed_result.content:
            return result("additional_investigation", ("artifact_empty",))
        criteria_met = (facts.criteria.log_contains.encode() in raw_log.content
                        and facts.criteria.result_contains.encode() in observed_result.content)
        expected = ("failure" if observation.exit_status != 0 or not criteria_met
                    else "success")
        expected_reason = ("nonzero_exit" if observation.exit_status != 0 else
                           "exit_zero_criteria_failed" if not criteria_met else "criteria_met")
        if (worker.judgment != expected or expected_reason not in worker.reasons
                or expected == "success" and worker.requires_code_change):
            return result("additional_investigation", ("worker_judgment_conflicts_with_criteria",))
        if request.kind == "evaluate":
            return result("confirmed_completion" if expected == "success" else
                          "confirmed_failure", () if expected == "success" else
                          (expected_reason,))
        if request.kind != "recover":
            return result("blocked_unresolved", ("unsupported_decision",))
        if expected != "failure":
            return result("blocked_unresolved", ("recovery_requires_confirmed_failure",))
        if not all(_covered(path, facts.approved_paths)
                   for path in request.mutation_paths):
            return result("blocked_unresolved", ("mutation_outside_approved_scope",))
        if facts.retries_used >= self._maximum:
            return result("blocked_unresolved", ("recovery_retry_limit_reached",))
        try:
            reserved = self._port.reserve_recovery(
                task_id=facts.identity.task_id, decision_id=request.decision_id,
                expected_used=facts.retries_used,
                expected_authority_version=facts.authority_version,
                expected_settings_revision=facts.settings_revision, maximum=self._maximum)
        except Exception:
            return result("blocked_unresolved", ("recovery_reservation_unconfirmed",))
        if reserved is not True:
            return result("blocked_unresolved", ("recovery_reservation_unconfirmed",))
        try:
            current = self._port.read(request.identity)
        except Exception:
            return result("blocked_unresolved", ("recovery_reservation_unconfirmed",))
        attempt = RetryAttempt(facts.identity.task_id, request.decision_id, "recovery")
        if (not isinstance(current, RecoveryFacts)
                or current.attempts != facts.attempts + (attempt,)
                or current.retries_used != facts.retries_used + 1
                or current != replace(facts, attempts=current.attempts)):
            return result("blocked_unresolved", ("recovery_reservation_unconfirmed",))
        sources.append(f"recovery_attempt:{request.decision_id}")
        steps = (("mutate_approved_sources",) if request.mutation_paths else ())
        return result("bounded_recovery_retry", (expected_reason,),
                      steps + ("send_new_worker_instruction", "relaunch"),
                      current.retries_used, current.retries_used)
