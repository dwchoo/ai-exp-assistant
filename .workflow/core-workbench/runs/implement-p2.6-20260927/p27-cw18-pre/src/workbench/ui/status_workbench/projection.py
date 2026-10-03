"""Immutable, evidence-only status rows for manager, worker, and host UI areas."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

from workbench.policy.pause_automation import PauseStatus


@dataclass(frozen=True, slots=True)
class AreaObservation:
    last_action: str | None
    last_action_at: str | None
    observed_at: str | None
    session_id: str | None = None
    session_generation: int | None = None
    idle: bool | None = None
    pending: bool | None = None
    paused: bool | None = None
    abort_status: str | None = None
    unknown_tool_results: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfirmedFileChange:
    path: str
    change: str
    confirmed_at: str
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RunningProcess:
    pid: int
    command: str
    observed_at: str
    state: str = "running"


@dataclass(frozen=True, slots=True)
class HostObservation:
    task_id: str | None
    revision: int | None
    run_id: str | None
    last_action: str | None
    last_action_at: str | None
    observed_at: str | None
    confirmed_file_changes: tuple[ConfirmedFileChange, ...] | None
    running_processes: tuple[RunningProcess, ...] | None
    phase: str | None = None
    exit_confirmed: bool | None = None
    exit_status: int | None = None
    cancelled: bool = False
    terminal_owner: str | None = None


@dataclass(frozen=True, slots=True)
class ThreeAreaStatus:
    manager: Any
    worker: Any
    host: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "manager", _freeze(self.manager))
        object.__setattr__(self, "worker", _freeze(self.worker))
        object.__setattr__(self, "host", _freeze(self.host))

    def to_dict(self) -> dict[str, Any]:
        return {"manager": _thaw(self.manager), "worker": _thaw(self.worker),
                "host": _thaw(self.host)}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _area(observation: AreaObservation, pause: PauseStatus, *, role: str) -> dict[str, Any]:
    pause_unknowns = (pause.manager_unknown_tool_call_ids if role == "manager"
                      else pause.worker_unknown_tool_call_ids)
    unknowns = tuple(dict.fromkeys((*observation.unknown_tool_results,
                                    *pause_unknowns)))
    return {
        "role": role,
        "last_action": observation.last_action,
        "last_action_at": observation.last_action_at,
        "observed_at": observation.observed_at or pause.last_checked_at,
        "session_id": observation.session_id,
        "session_generation": observation.session_generation,
        "idle": observation.idle,
        "pending": observation.pending,
        "paused": pause.paused,
        "omp_paused": observation.paused,
        "abort_status": observation.abort_status if role == "manager" else None,
        "manager_abort_ack": pause.manager_ack_status if role == "manager" else None,
        "worker_pause_ack": pause.worker_ack_status if role == "worker" else None,
        "pause_requested_at": pause.requested_at,
        "turn_stop_observed_at": pause.stop_observed_at if role == "manager" else None,
        "unknown_tool_results": list(unknowns),
        "cancelled": pause.cancelled,
    }


def project_three_area_status(*, pause: PauseStatus, manager: AreaObservation,
                              worker: AreaObservation, host: HostObservation) -> ThreeAreaStatus:
    """Project only confirmed or explicitly unknown facts; never infer an outcome."""
    identity = None if pause.binding is None else {
        "task_id": pause.binding.task_id,
        "revision": pause.binding.revision,
        "run_id": pause.binding.run_id,
        "approval_hash": pause.binding.approval_hash,
        "approved_scope_hash": pause.binding.approved_scope_hash,
    }
    manager_row = _area(manager, pause, role="manager")
    worker_row = _area(worker, pause, role="worker")
    host_row = {
        "role": "host",
        "task": identity,
        "last_action": host.last_action,
        "last_action_at": host.last_action_at,
        "observed_at": host.observed_at or pause.last_checked_at,
        "phase": host.phase,
        "exit_confirmed": host.exit_confirmed,
        "exit_status": host.exit_status,
        "confirmed_file_changes": (None if host.confirmed_file_changes is None else
                                   [asdict(item) for item in host.confirmed_file_changes]),
        "running_processes": (None if host.running_processes is None else
                              [asdict(item) for item in host.running_processes]),
        "paused": pause.paused,
        "cancelled": host.cancelled or pause.cancelled,
        "terminal_owner": host.terminal_owner,
        "pause_requested_at": pause.requested_at,
        "last_checked_at": pause.last_checked_at,
        "unknown_tool_results": list(pause.unknown_tool_call_ids),
        "persistence_error": pause.persistence_error,
    }
    return ThreeAreaStatus(manager=manager_row, worker=worker_row, host=host_row)
