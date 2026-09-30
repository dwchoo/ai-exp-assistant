"""CW-15 binding of live predecessor ports; policy remains in those ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from workbench.contracts.v1 import ActorRole
from workbench.ipc.bridge_g3.mailbox import (
    BridgeDisconnected, DeliveryReceipt, G3BridgeServer, MailboxMessage,
    MailboxStatus, TaskMailbox,
)
from workbench.observation.worker_review import WorkerReviewScheduler
from workbench.policy.pause_automation.controller import PauseCoordinator
from workbench.policy.recovery_manager import (
    ForceTarget, RecoveryCoordinator, RecoveryRequest, RunIdentity, RunObservation,
)
from workbench.runtime.g4 import FrontendLease
from workbench.runtime.process_evidence import LinuxProcessProbe, ProcessRef
from workbench.storage.log_raw import MetadataAdmissionGate, RawLogStore
from workbench.terminal.shell_persistent.adapter import PersistentShell

from .lifecycle import (
    ControlState, LifecycleCoordinator, LifecycleHeld, LifecycleJournal, PeerRef,
)


@dataclass(slots=True)
class G3PeerAdapter:
    bridge: G3BridgeServer
    journal: LifecycleJournal
    processes: LinuxProcessProbe

    def observe(self, role: str) -> PeerRef | None:
        record = self.journal.read()
        if record is None or role not in {"manager", "worker"}:
            return None
        expected = record.manager if role == "manager" else record.worker
        try:
            peer = self.bridge.peer(role)
        except BridgeDisconnected:
            return None
        if (peer.role.value, peer.session_id, peer.generation, peer.pid) != (
                role, expected.session_id, expected.generation, expected.process.pid):
            return None
        if self.processes.observe(expected.process).state != "alive":
            return None
        return expected


@dataclass(slots=True)
class PersistentControlAdapter:
    shell: PersistentShell
    journal: LifecycleJournal
    processes: LinuxProcessProbe

    def _state(self, snapshot: object) -> ControlState:
        record = self.journal.read()
        if record is None or not isinstance(snapshot, dict):
            raise LifecycleHeld("shell control binding unavailable")
        if snapshot.get("parent_pid") != record.shell.pid:
            raise LifecycleHeld("shell process identity drifted")
        if self.processes.observe(record.shell).state != "alive":
            raise LifecycleHeld("shell process incarnation unknown")
        if snapshot.get("phase") == "unknown" or snapshot.get("held_reasons") is None:
            raise LifecycleHeld("shell control observation unknown")
        return ControlState(
            snapshot["input_owner"], snapshot["owner_epoch"],
            snapshot["parent_mode"], snapshot["takeover_requested"],
            snapshot["takeover_confirmed"],
        )

    def observe(self) -> ControlState:
        return self._state(self.shell.poll(0))

    def request_takeover(self) -> ControlState:
        return self._state(self.shell.request_takeover())

    def confirm_takeover(self) -> ControlState:
        return self._state(self.shell.confirm_takeover())

    def handoff(self) -> ControlState:
        return self._state(self.shell.claim_manager())


@dataclass(slots=True)
class BoundMailboxCommandAdapter:
    bridge: G3BridgeServer
    peers: G3PeerAdapter
    mailbox: TaskMailbox
    pause: PauseCoordinator
    control: PersistentControlAdapter
    message: MailboxMessage
    timeout: float = 20

    def send_bound(self, request_id: str, *, session_id: str, generation: int,
                   owner_epoch: int, authority_token: object) -> str:
        return self.send(request_id, session_id=session_id, generation=generation,
                         owner_epoch=owner_epoch, authority_token=authority_token)

    def send(self, request_id: str, *, session_id: str, generation: int,
             owner_epoch: int, authority_token: object) -> str:
        message = self.message
        if (request_id != message.message_id or message.target_role is not ActorRole.WORKER
                or (message.session_id, message.session_generation) !=
                (session_id, generation)):
            raise LifecycleHeld("mailbox message is not bound to current worker")
        peer = self.bridge.peer("worker")
        control = self.control.observe()
        if self.peers.observe("worker") is None or self.peers.observe("manager") is None:
            raise LifecycleHeld("current OMP process incarnation unknown")
        if ((peer.session_id, peer.generation) != (session_id, generation)
                or control.owner_epoch != owner_epoch or control.input_owner != "manager"
                or control.mode != "control_wait" or control.takeover_requested):
            raise LifecycleHeld("worker session or shell owner changed")
        _, receipt = self.pause.dispatch_bound_automatic(
            self.mailbox, message, timeout=self.timeout,
            authority_token=authority_token,
        )
        if not isinstance(receipt, DeliveryReceipt):
            raise LifecycleHeld("bound automatic delivery held before API submission; no replay")
        if receipt.status is MailboxStatus.OMP_PROCESSED:
            return "omp_processed"
        if receipt.status is MailboxStatus.API_RETURNED:
            return "api_returned"
        if receipt.details.get("api_called") is False:
            raise LifecycleHeld("delivery rejected before API submission; no replay")
        raise LifecycleHeld("delivery outcome unknown after API submission; no replay")


class RecoveryStopAdapter:
    """Execute only CW-13-authorized steps on exact live process references."""

    def __init__(self, recovery: RecoveryCoordinator, identity: RunIdentity,
                 processes: LinuxProcessProbe,
                 normal_stop: Callable[[ProcessRef], None],
                 drain: Callable[[float], None],
                 force: Callable[[ProcessRef], None] | None = None):
        self.recovery = recovery
        self.identity = identity
        self.processes = processes
        self._normal_stop = normal_stop
        self._drain = drain
        self._force = force
        self._stop_decision = None

    def normal_stop(self, ref: ProcessRef) -> None:
        if self._stop_decision is None:
            self._stop_decision = self.recovery.decide(RecoveryRequest(
                self.identity, str(uuid4()), kind="stop",
            ))
        if "request_stop" not in self._stop_decision.steps:
            if ("wait_full_termination" in self._stop_decision.steps
                    or "already_terminated" in self._stop_decision.report.remaining_problems):
                return
            raise LifecycleHeld("CW-13 ordinary stop is not authorized")
        evidence = self.processes.observe(ref)
        if evidence.state == "unknown":
            raise LifecycleHeld("exact stop target unknown")
        if evidence.state == "alive":
            self._normal_stop(ref)

    def drain(self, timeout: float) -> None:
        self._drain(timeout)

    def force_exact(self, ref: ProcessRef) -> None:
        target = ForceTarget(self.identity.run_id, ref.pid, ref.start_ticks,
                             ref.owner_epoch)
        decision = self.recovery.decide(RecoveryRequest(
            self.identity, str(uuid4()), kind="force", force_target=target,
        ))
        if "force_exact_owned_target" not in decision.steps or self._force is None:
            raise LifecycleHeld("CW-13 force proof unavailable")
        if self.processes.observe(ref).state != "alive":
            raise LifecycleHeld("exact force target not alive")
        self._force(ref)


def bind_production(
    *, journal: LifecycleJournal, bridge: G3BridgeServer,
    mailbox: TaskMailbox, message: MailboxMessage, shell: PersistentShell,
    pause: PauseCoordinator, review: WorkerReviewScheduler,
    recovery: RecoveryCoordinator, raw: RawLogStore,
    metadata: MetadataAdmissionGate, model: object,
    termination: Callable[[], RunObservation | None],
    boot_marker: Callable[[], str], authority: Callable[[], bool],
    normal_stop: Callable[[ProcessRef], None], drain: Callable[[float], None],
    force: Callable[[ProcessRef], None] | None = None,
    frontend: FrontendLease | None = None,
) -> LifecycleCoordinator:
    """Bind existing live owners; callbacks only actuate CW-13-approved steps."""
    for value, required in (
        (bridge, G3BridgeServer), (mailbox, TaskMailbox),
        (message, MailboxMessage), (shell, PersistentShell),
        (pause, PauseCoordinator), (review, WorkerReviewScheduler),
        (recovery, RecoveryCoordinator), (raw, RawLogStore),
        (metadata, MetadataAdmissionGate),
    ):
        if not isinstance(value, required):
            raise TypeError(f"{required.__name__} live port required")
    record = journal.read()
    if record is None or (message.task_id, message.revision, message.run_id) != (
            record.task_id, record.revision, record.run_id):
        raise LifecycleHeld("mailbox message and lifecycle run are not bound")
    processes = LinuxProcessProbe()
    control = PersistentControlAdapter(shell, journal, processes)
    peers = G3PeerAdapter(bridge, journal, processes)
    return LifecycleCoordinator(
        journal=journal, peers=peers,
        control=control, processes=processes, pause=pause, review=review,
        model=model, metadata=metadata, raw=raw,
        commands=BoundMailboxCommandAdapter(bridge, peers, mailbox, pause, control, message),
        stops=RecoveryStopAdapter(recovery, RunIdentity(record.task_id,
                                 record.revision, record.run_id), processes,
                                 normal_stop, drain, force),
        termination=termination, boot_marker=boot_marker, authority=authority,
        frontend=frontend or FrontendLease(),
    )
