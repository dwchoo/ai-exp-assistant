"""CW-19: start-up reconcile, the admission hold and the previous backend's survivors.

``StartupReconciler.run`` is called by the backend right after it holds the
data dir's instance lock and before anything else (no TaskFlow load, no pane,
no UI socket, no record overwrite). It

- reads the previous ``backend.json`` (kept as ``backend.prev.json``) and lets
  ``BootStore.begin`` decide whether the boot must be confirmed (C-AC-23);
- classifies the previous end (``boot.classify``);
- on the same boot only, compares every process the previous backend recorded
  (pid + start ticks; the backend, both OMPs, the host shell, its supervisor,
  the experiment's main process, survivors carried from an earlier start) with
  ``/proc``. A process recorded under another or an unknown boot is never
  probed: its pid may belong to anything now (``ended_by_reboot``);
- lists the live members of the previous panes' own sessions;
- lists handoff outbox messages the journal shows as queued and never
  submitted (``queued_not_sent``, R9) or submitted without an outcome
  (``submitted_outcome_unknown``); none of them is sent again;
- names the run that was bound (``lifecycle.json``) and whether it is still the
  Task's current run (its outcome is unknown; it is never re-run).

Nothing is replayed and nothing is signalled here (C-D71 (1)): a live process of
the previous backend is a *survivor*. Survivors whose identity is proven (same
boot, pid, start ticks, owner) can be stopped one by one through
``SurvivorRegistry.stop`` (the manager's ``stop_survivor`` tool), which proves
the identity again right before signalling; the others are only shown.

``AdmissionHold`` is the one place that says whether a Workbench-originated
automatic action may start now (C-D71 (3)/(4), C-AC-28): reasons are
``boot_confirmation_required``, ``metadata_unavailable``,
``model_hold:<role>`` and ``shutdown_closing``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import select
import signal
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping

from workbench.backend.boot import (
    BOOT_UNKNOWN, FRESH, REBOOT, SAME_BOOT_CLEAN_STOP, SAME_BOOT_CRASH, SAME_BOOT_UNVERIFIED_STOP, BootState,
    BootStore, classify,
)
from workbench.backend.paths import DataLayout, read_private_json, write_private_json

BOOT_HOLD = "boot_confirmation_required"
METADATA_HOLD = "metadata_unavailable"
MODEL_HOLD_PREFIX = "model_hold:"
SHUTDOWN_HOLD = "shutdown_closing"
_HOLD_ORDER = (SHUTDOWN_HOLD, BOOT_HOLD, METADATA_HOLD)

SAME_BOOT = (SAME_BOOT_CLEAN_STOP, SAME_BOOT_UNVERIFIED_STOP, SAME_BOOT_CRASH)
SURVIVOR_GRACE = 3.0
SURVIVOR_KILL_WAIT = 1.0
MEMBER_LIMIT = 64
OUTBOX_TERMINAL = frozenset({"delivered", "unknown", "rejected", "held_paused", "withdrawn"})
OUTBOX_LISTED = 20
PANE_LEADERS = ("manager_omp", "worker_omp", "host_shell")


def model_hold(role: str) -> str:
    return f"{MODEL_HOLD_PREFIX}{role}"


def _role_name(role: Any) -> str | None:
    if role is None:
        return None
    return str(getattr(role, "value", role))


class AdmissionHold:
    """Why Workbench must not start an automatic action now; thread-safe, in memory."""

    def __init__(self, wall: Callable[[], float] = time.time):
        self._lock = threading.Lock()
        self._wall = wall
        self._reasons: dict[str, dict[str, Any]] = {}

    def set(self, reason: str, *, detail: str | None = None) -> bool:
        with self._lock:
            if reason in self._reasons:
                if detail is not None:
                    self._reasons[reason]["detail"] = detail
                return False
            self._reasons[reason] = {"reason": reason, "since": self._wall(), "detail": detail}
            return True

    def clear(self, reason: str) -> bool:
        with self._lock:
            return self._reasons.pop(reason, None) is not None

    def has(self, reason: str) -> bool:
        with self._lock:
            return reason in self._reasons

    def reason_for(self, role: Any = None) -> str | None:
        """The first reason that holds automatic work for ``role`` (None: work for any role)."""
        name = _role_name(role)
        with self._lock:
            for reason in _HOLD_ORDER:
                if reason in self._reasons:
                    return reason
            for reason in self._reasons:
                if reason.startswith(MODEL_HOLD_PREFIX) and (name is None or reason == model_hold(name)):
                    return reason
        return None

    def reasons(self) -> list[str]:
        with self._lock:
            return list(self._reasons)

    def view(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._reasons.values()]


# -- /proc helpers (never signal by number) -----------------------------------------------------
def _stat(pid: int) -> list[bytes] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    marker = raw.rfind(b") ")
    if marker < 0:
        return None
    fields = raw[marker + 2:].split()
    return fields if len(fields) > 19 else None


def _ticks(fields: list[bytes] | None) -> int | None:
    try:
        return int(fields[19]) if fields else None
    except (ValueError, IndexError):
        return None


def _live(fields: list[bytes] | None) -> bool:
    return bool(fields) and fields[0] not in (b"Z", b"X", b"x")


def _session(fields: list[bytes] | None) -> int | None:
    try:
        return int(fields[3]) if fields else None
    except (ValueError, IndexError):
        return None


def _uid(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def _comm(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()[:32] or None
    except OSError:
        return None


def observe(pid: int, start_ticks: int) -> str:
    """``alive`` (same pid, start ticks, owner), ``ended`` (gone or the pid is another process) or ``unknown``."""
    fields = _stat(pid)
    if fields is None:
        return "ended" if not os.path.exists(f"/proc/{pid}") else "unknown"
    ticks = _ticks(fields)
    if ticks is None:
        return "unknown"
    if ticks != start_ticks or not _live(fields):
        return "ended"
    if _uid(pid) != os.geteuid():
        return "unknown"
    return "alive"


def _pidfd_exited(fd: int) -> bool:
    try:
        return bool(select.select([fd], [], [], 0)[0])
    except (OSError, ValueError):
        return True


def _session_members(session: int, exclude: Iterable[int] = ()) -> list[int]:
    skip = set(exclude) | {session}
    members = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return members
    for name in names:
        if not name.isdecimal() or int(name) in skip:
            continue
        fields = _stat(int(name))
        if _live(fields) and _session(fields) == session:
            members.append(int(name))
            if len(members) >= MEMBER_LIMIT:
                break
    return sorted(members)


# -- survivors ----------------------------------------------------------------------------------
@dataclass
class Survivor:
    """A live process of a previous backend incarnation (never auto-killed, C-D71 (1))."""

    survivor_id: str
    name: str  # manager_omp | worker_omp | host_shell | supervisor | backend | run_target | session_member
    pid: int
    start_ticks: int | None
    boot_id: str | None
    stoppable: bool
    why_not: str | None = None  # why it is shown only
    session: int | None = None
    leader: tuple[int, int] | None = None  # session_member: the proven (pid, start ticks) of its session leader
    comm: str | None = None
    state: str = "alive"  # alive | ended | stopped | stop_unconfirmed
    stop: dict[str, Any] | None = None
    # stop_unconfirmed: the exact (pid, start ticks) identities that were signalled and not yet seen ending;
    # they are re-observed by ``refresh`` and are the only targets of a retried stop.
    pending: list[tuple[int, int]] = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        return {"survivor_id": self.survivor_id, "name": self.name, "pid": self.pid,
                "start_ticks": self.start_ticks, "boot_id": self.boot_id, "comm": self.comm,
                "session": self.session,
                "stoppable": self.stoppable and self.state in ("alive", "stop_unconfirmed"),
                "identity": "verified" if self.stoppable else "unverified", "why_not": self.why_not,
                "state": self.state, "stop": None if self.stop is None else dict(self.stop)}

    def carried(self) -> dict[str, Any]:
        """What the next start needs to prove it again (backend.json ``survivors``)."""
        return {"survivor_id": self.survivor_id, "name": self.name, "pid": self.pid,
                "start_ticks": self.start_ticks, "boot_id": self.boot_id, "stoppable": self.stoppable,
                "why_not": self.why_not, "session": self.session,
                "leader": None if self.leader is None else list(self.leader)}


class SurvivorRegistry:
    """The survivors of this start; ``stop`` is the manager's ``stop_survivor`` (C-D71 (1))."""

    def __init__(self, survivors: Iterable[Survivor], boot_source: Callable[[], str | None], *,
                 grace: float = SURVIVOR_GRACE, kill_wait: float = SURVIVOR_KILL_WAIT,
                 wall: Callable[[], float] = time.time):
        self._survivors = list(survivors)
        self._boot_source = boot_source
        self._grace, self._kill_wait = grace, kill_wait
        self._wall = wall
        self._lock = threading.Lock()  # state
        self._stop_lock = threading.Lock()  # one stop at a time

    def refresh(self) -> None:
        with self._lock:
            survivors = [s for s in self._survivors if s.state == "alive"]
            unconfirmed = [(s, list(s.pending)) for s in self._survivors if s.state == "stop_unconfirmed"]
        for survivor, pending in unconfirmed:
            # Re-observe by exact identity: an identity whose pid is gone or now another process has ended.
            left = [(pid, ticks) for pid, ticks in pending if observe(pid, ticks) != "ended"]
            with self._lock:
                if survivor.state != "stop_unconfirmed" or survivor.pending != pending:
                    continue  # a concurrent stop changed it; the next refresh sees the new state
                survivor.pending = left
                if survivor.stop is not None:
                    survivor.stop["remaining"] = sorted(pid for pid, _ in left)
                if not left:
                    survivor.state = "stopped"
                    if survivor.stop is not None:
                        survivor.stop["outcome"] = "stopped"
                        survivor.stop["confirmed_at"] = self._wall()
        for survivor in survivors:
            if survivor.start_ticks is not None and observe(survivor.pid, survivor.start_ticks) == "ended":
                with self._lock:
                    if survivor.state == "alive":
                        survivor.state = "ended"
            elif survivor.start_ticks is None and _stat(survivor.pid) is None:
                with self._lock:
                    survivor.state = "ended"

    def views(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        if refresh:
            self.refresh()
        with self._lock:
            return [s.view() for s in self._survivors]

    def alive(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        return [view for view in self.views(refresh=refresh) if view["state"] in ("alive", "stop_unconfirmed")]

    def carried(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.carried() for s in self._survivors if s.state in ("alive", "stop_unconfirmed")]

    def get(self, survivor_id: str) -> Survivor | None:
        with self._lock:
            for survivor in self._survivors:
                if survivor.survivor_id == survivor_id:
                    return survivor
        return None

    @staticmethod
    def _refused(reason: str, detail: str, survivor: Survivor | None = None) -> dict[str, Any]:
        return {"status": "refused", "reason": reason, "detail": detail,
                "survivor": None if survivor is None else survivor.view()}

    def stop(self, survivor_id: str, *, reason: str, requester: str) -> dict[str, Any]:
        """TERM, grace, KILL of one proven survivor (and the members of its own session when it leads one).

        The identity (same boot, pid, start ticks, owner; a member also its
        live session leader) is proven again right before the first signal and
        held through pidfds, so a recycled pid is never signalled.
        """
        with self._stop_lock:
            survivor = self.get(survivor_id)
            if survivor is None:
                return self._refused("unknown_survivor", "No survivor with this survivor_id; read workbench_status.")
            if not survivor.stoppable:
                return self._refused("identity_unverified", "This process is only shown: its identity is not proven "
                                     f"({survivor.why_not or 'unknown'}); Workbench never signals it.", survivor)
            if survivor.state == "stop_unconfirmed":
                self.refresh()  # it may have ended since the last stop
            if survivor.state not in ("alive", "stop_unconfirmed"):
                return self._refused("not_alive", f"The survivor is {survivor.state}; nothing was signalled.",
                                     survivor)
            boot = None
            try:
                boot = self._boot_source()
            except Exception:
                boot = None
            if not boot or boot != survivor.boot_id:
                return self._refused("boot_changed", "The boot changed or is unknown; nothing was signalled.",
                                     survivor)
            if survivor.state == "stop_unconfirmed":
                result = self._signal_pending(list(survivor.pending))
            else:
                result = self._signal(survivor)
            with self._lock:
                if result["status"] in ("stopped", "stop_unconfirmed"):
                    survivor.state = result["status"]
                    survivor.pending = list(result.pop("pending", []))
                elif result["status"] == "already_ended":
                    survivor.state = "ended" if survivor.state == "alive" else "stopped"
                    survivor.pending = []
                survivor.stop = {"reason": reason, "requester": requester, "at": self._wall(),
                                 "outcome": result["status"], "signalled": result.get("signalled", []),
                                 "remaining": result.get("remaining", [])}
            return {**result, "survivor": survivor.view()}

    def _pin(self, pid: int, start_ticks: int | None) -> tuple[int | None, str | None]:
        """(pidfd, None) for the proven process, else (None, why)."""
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return None, "ended"
        except OSError as exc:
            return None, f"pidfd_unavailable:{type(exc).__name__}"
        fields = _stat(pid)
        if start_ticks is None or _ticks(fields) != start_ticks or not _live(fields):
            os.close(fd)
            return None, "ended" if fields is None or _ticks(fields) != start_ticks else "identity_changed"
        if _uid(pid) != os.geteuid():
            os.close(fd)
            return None, "owner_changed"
        return fd, None

    def _signal_pending(self, pending: list[tuple[int, int]]) -> dict[str, Any]:
        """Retry of a stop_unconfirmed survivor: only the exact identities signalled before, pinned again."""
        pinned: dict[int, int] = {}
        ticks_of: dict[int, int] = {}
        # review-02 P3-2: an identity that cannot be pinned again (owner_changed, pidfd_unavailable, ...) is
        # unknown, not ended; only an exact-identity observation of ``ended`` drops it (as in ``refresh``).
        unpinned: list[tuple[int, int, str]] = []
        try:
            for pid, ticks in pending:
                fd, why = self._pin(pid, ticks)
                if fd is not None:
                    pinned[pid], ticks_of[pid] = fd, ticks
                elif observe(pid, ticks) != "ended":
                    unpinned.append((pid, ticks, why or "unknown"))
            if not pinned and not unpinned:
                return {"status": "already_ended", "reason": "ended", "detail": "It had already ended.",
                        "signalled": [], "remaining": []}
            if pinned:
                result = self._terminate(pinned, ticks_of, leader=None)
            else:
                result = {"status": "stopped", "reason": "ended", "signalled": [], "remaining": [], "pending": [],
                          "members": []}
            if unpinned:
                whys = sorted({why for _pid, _ticks, why in unpinned})
                result.update(
                    status="stop_unconfirmed", reason=f"not_pinned:{','.join(whys)}",
                    detail="Some processes could not be re-proven, so they were not signalled; they may still run.",
                    remaining=sorted(set(result["remaining"]) | {pid for pid, _t, _w in unpinned}),
                    pending=list(result["pending"]) + [(pid, ticks) for pid, ticks, _w in unpinned])
            return result
        finally:
            for pidfd in pinned.values():
                os.close(pidfd)

    def _terminate(self, pinned: dict[int, int], ticks_of: dict[int, int], leader: int | None) -> dict[str, Any]:
        """TERM+CONT, grace, KILL, wait — through the pinned pidfds only."""
        signalled: list[int] = []
        for pid, pidfd in pinned.items():
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                signal.pidfd_send_signal(pidfd, signal.SIGCONT)
                signalled.append(pid)
            except OSError:
                pass
        deadline = time.monotonic() + self._grace
        while time.monotonic() < deadline and not all(_pidfd_exited(pidfd) for pidfd in pinned.values()):
            time.sleep(0.02)
        for pid, pidfd in pinned.items():
            if not _pidfd_exited(pidfd):
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                except OSError:
                    pass
        deadline = time.monotonic() + self._kill_wait
        while time.monotonic() < deadline and not all(_pidfd_exited(pidfd) for pidfd in pinned.values()):
            time.sleep(0.02)
        remaining = sorted(pid for pid, pidfd in pinned.items() if not _pidfd_exited(pidfd))
        return {"status": "stop_unconfirmed" if remaining else "stopped",
                "reason": "still_alive" if remaining else "ended",
                "signalled": sorted(signalled), "remaining": remaining,
                "pending": [(pid, ticks_of[pid]) for pid in remaining],
                "members": sorted(pid for pid in pinned if pid != leader)}

    def _signal(self, survivor: Survivor) -> dict[str, Any]:
        pinned: dict[int, int] = {}
        ticks_of: dict[int, int] = {}
        leader_fd: int | None = None
        try:
            if survivor.leader is not None:  # a member: its session is provably the old pane's only while the
                leader_fd, why = self._pin(*survivor.leader)  # leader lives with the recorded identity
                if leader_fd is None:
                    return {"status": "refused", "reason": "identity_unverified",
                            "detail": f"Its session leader is no longer the recorded process ({why}); nothing "
                                      "was signalled."}
            fd, why = self._pin(survivor.pid, survivor.start_ticks)
            if fd is None:
                if why == "ended":
                    return {"status": "already_ended", "reason": "ended", "detail": "It had already ended.",
                            "signalled": [], "remaining": []}
                return {"status": "refused", "reason": why or "identity_changed",
                        "detail": "Its identity changed; nothing was signalled."}
            if survivor.leader is not None and _session(_stat(survivor.pid)) != survivor.leader[0]:
                os.close(fd)
                return {"status": "refused", "reason": "identity_changed",
                        "detail": "It left its session; nothing was signalled."}
            pinned[survivor.pid] = fd
            ticks_of[survivor.pid] = int(survivor.start_ticks)  # _pin proved it equal
            if survivor.leader is None and _session(_stat(survivor.pid)) == survivor.pid:
                # It leads its own session: pin the members while the live leader proves the session number.
                for member in _session_members(survivor.pid):
                    member_ticks = _ticks(_stat(member))
                    member_fd, _ = self._pin(member, member_ticks)
                    if member_fd is None or member_ticks is None:
                        continue
                    if _session(_stat(member)) != survivor.pid:
                        os.close(member_fd)
                        continue
                    pinned[member], ticks_of[member] = member_fd, member_ticks
                if _pidfd_exited(fd):  # the leader ended during the scan: the members are not proven
                    for member, member_fd in list(pinned.items()):
                        if member != survivor.pid:
                            os.close(member_fd)
                            del pinned[member]
            return self._terminate(pinned, ticks_of, leader=survivor.pid)
        finally:
            for pidfd in pinned.values():
                os.close(pidfd)
            if leader_fd is not None:
                os.close(leader_fd)


# -- outbox messages lost in a crash (R9) ----------------------------------------------------------
JSONL_LINE_LIMIT = 1024 * 1024  # a longer journal line is skipped (no journal record is that long)
LOST_TRACKED_LIMIT = 4096  # open (non-terminal) outbox entries tracked while streaming the journal


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Stream the JSON objects of a journal, line by line (bounded memory, the whole file; never follows a link)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return
    try:
        with os.fdopen(fd, "rb") as handle:
            skipping = False
            while True:
                line = handle.readline(JSONL_LINE_LIMIT)
                if not line:
                    return
                complete = line.endswith(b"\n")
                if skipping or not complete and len(line) >= JSONL_LINE_LIMIT:
                    skipping = not complete  # the rest of an over-long line
                    continue
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def read_jsonl(path: Path, limit: int = 64 * 1024 * 1024) -> list[dict[str, Any]]:
    """The JSON objects of the last ``limit`` bytes of a journal (the most recent records; a cut first line is
    skipped). The start-up reconcile streams the whole journal with ``iter_jsonl`` instead."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return []
    try:
        with os.fdopen(fd, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            offset = max(0, size - limit)
            handle.seek(offset)
            data = handle.read(limit)
    except OSError:
        return []
    lines = data.split(b"\n")
    if offset > 0 and lines:
        lines = lines[1:]  # began inside a record
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def lost_outbox(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Outbox messages of the previous incarnation without a terminal state (never resent).

    ``records`` may be a stream: only the records after the last ``backend_start`` marker count, and an entry is
    forgotten once it reached a terminal state (no outbox record follows one), so memory stays bounded.
    """
    entries: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("type") == "backend_start":
            entries.clear()
            continue
        if record.get("type") != "outbox" or not isinstance(record.get("handoff_id"), str):
            continue
        handoff_id = record["handoff_id"]
        state = record.get("state")
        if state in OUTBOX_TERMINAL:
            entries.pop(handoff_id, None)
            continue
        entry = entries.get(handoff_id)
        if entry is None:
            if len(entries) >= LOST_TRACKED_LIMIT:
                entries.pop(next(iter(entries)))  # the oldest open entry
            entry = entries[handoff_id] = {"handoff_id": handoff_id, "target_role": None, "kind": None,
                                           "submitted": False, "message_id": None}
        for name in ("target_role", "kind"):
            if record.get(name) is not None and entry[name] is None:
                entry[name] = record.get(name)
        if record.get("message_id"):
            entry["message_id"] = record["message_id"]
        if state == "submitted":
            entry["submitted"] = True
    return [{"handoff_id": entry["handoff_id"], "target_role": entry["target_role"], "kind": entry["kind"],
             "message_id": entry["message_id"],
             "state": "submitted_outcome_unknown" if entry["submitted"] else "queued_not_sent"}
            for entry in entries.values()]


# -- durable pause (R4) ------------------------------------------------------------------------------
class PauseStore:
    """``<data>/automation.json``: the user's pause survives a backend crash (SPEC: recovery never lifts it)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> bool:
        if not os.path.lexists(self.path):
            return False
        value = read_private_json(self.path, 4096)
        if not isinstance(value, dict) or type(value.get("paused")) is not bool:
            return True  # unreadable: fail closed (the user resumes explicitly)
        return value["paused"]

    def save(self, paused: bool) -> bool:
        try:
            write_private_json(self.path, {"version": 1, "paused": paused, "at": time.time()})
        except OSError:
            return False
        return True


# -- the start-up reconcile -------------------------------------------------------------------------
@dataclass
class StartupResult:
    boot: BootState
    report: dict[str, Any]
    survivors: list[Survivor] = field(default_factory=list)
    previous: dict[str, Any] | None = None


def _ref(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, Mapping):
        return None
    pid, ticks = value.get("pid"), value.get("start_ticks")
    if type(pid) is int and pid > 0 and type(ticks) is int and ticks > 0:
        return pid, ticks
    return None


class StartupReconciler:
    """See the module docstring; ``run`` never signals, starts or resends anything."""

    def __init__(self, layout: DataLayout, boot_store: BootStore, *, handoff_journal: Path,
                 lifecycle_journal: Path | None = None, run_target: Callable[[str], tuple[int, int] | None] | None = None,
                 current_run: Callable[[str], str | None] | None = None, wall: Callable[[], float] = time.time):
        self.layout = layout
        self.boot_store = boot_store
        self.handoff_journal = handoff_journal
        self.lifecycle_journal = lifecycle_journal
        self._run_target = run_target
        self._current_run = current_run
        self._wall = wall

    def run(self) -> StartupResult:
        previous = read_private_json(self.layout.record)
        notes: list[str] = []
        if previous is not None:
            try:
                write_private_json(self.layout.root / "backend.prev.json", previous)
            except OSError as exc:
                notes.append(f"previous record not kept: {type(exc).__name__}")
        elif os.path.lexists(self.layout.record):
            notes.append("previous backend record unreadable")
        history = (previous is not None or os.path.lexists(self.layout.record)
                   or os.path.lexists(self.boot_store.path))
        boot = self.boot_store.begin(previous)
        classification = classify(boot.current, previous, history=history)
        previous_boot = (previous or {}).get("boot_id")
        probe = classification in SAME_BOOT and previous_boot == boot.current
        processes: list[dict[str, Any]] = []
        survivors: list[Survivor] = []
        listed: set[int] = set()

        def add(name: str, ref: tuple[int, int] | None, ref_boot: Any, *, carried: Mapping[str, Any] | None = None
                ) -> str:
            if ref is None:
                return "unrecorded"
            pid, ticks = ref
            if not probe or ref_boot != boot.current:
                state = "ended_by_reboot" if classification in (REBOOT,) or ref_boot != boot.current else "unknown"
                if boot.current is None or classification == BOOT_UNKNOWN:
                    state = "not_probed_boot_unknown"
                processes.append({"name": name, "pid": pid, "start_ticks": ticks, "state": state})
                return state
            state = observe(pid, ticks)
            processes.append({"name": name, "pid": pid, "start_ticks": ticks, "state": state})
            if state == "alive" and pid not in listed:
                listed.add(pid)
                fields = _stat(pid)
                stoppable = True if carried is None else bool(carried.get("stoppable"))
                leader = None
                if carried is not None and isinstance(carried.get("leader"), list) and len(carried["leader"]) == 2:
                    leader = (int(carried["leader"][0]), int(carried["leader"][1]))
                    if observe(*leader) != "alive":
                        stoppable, leader = False, None
                survivors.append(Survivor(f"s{len(survivors) + 1}", name, pid, ticks, boot.current, stoppable,
                                          None if stoppable else (carried or {}).get("why_not") or "unverified",
                                          session=_session(fields), leader=leader, comm=_comm(pid)))
            return state

        recorded = (previous or {}).get("processes")
        recorded = recorded if isinstance(recorded, Mapping) else {}
        states = {}
        for name in ("backend", *PANE_LEADERS, "supervisor"):
            states[name] = add(name, _ref(recorded.get(name)), previous_boot)
        run = self._run_view(classification)
        if run is not None and self._run_target is not None:
            try:
                target = self._run_target(run["run_id"])
            except Exception:
                target = None
            run["target"] = None if target is None else {"pid": target[0], "start_ticks": target[1]}
            run["target_state"] = add("run_target", target, previous_boot)
        for item in (previous or {}).get("survivors") or ():
            if isinstance(item, Mapping):
                add(str(item.get("name") or "survivor"), _ref(item), item.get("boot_id"), carried=item)
        if probe:  # members of the previous panes' own sessions (each pane leads one)
            for name in PANE_LEADERS:
                ref = _ref(recorded.get(name))
                if ref is None:
                    continue
                leader_alive = states.get(name) == "alive"
                for pid in _session_members(ref[0], exclude=listed):
                    fields = _stat(pid)
                    ticks = _ticks(fields)
                    if ticks is None:
                        continue
                    listed.add(pid)
                    survivors.append(Survivor(
                        f"s{len(survivors) + 1}", "session_member", pid, ticks, boot.current, leader_alive,
                        None if leader_alive else f"its session leader ({name}) is gone, so the session is not "
                                                  "provably the old pane's", session=ref[0],
                        leader=ref if leader_alive else None, comm=_comm(pid)))
        lost = lost_outbox(iter_jsonl(self.handoff_journal))  # the whole journal, streamed (P3-4)
        report = {
            "classification": classification, "at": self._wall(),
            "boot": boot.view(), "previous": None if previous is None else {
                "pid": (_ref(recorded.get("backend")) or (None,))[0], "phase": previous.get("phase"),
                "boot_id": previous_boot,
                "shutdown_verified": (previous.get("shutdown") or {}).get("verified")
                if isinstance(previous.get("shutdown"), Mapping) else None},
            "probed": probe, "processes": processes, "survivors": [s.view() for s in survivors],
            "run": run, "outbox_lost": lost[:OUTBOX_LISTED], "outbox_lost_count": len(lost), "notes": notes}
        try:
            write_private_json(self.layout.root / "startup.json", report)
        except OSError as exc:
            report["notes"].append(f"startup report not written: {type(exc).__name__}")
        return StartupResult(boot, report, survivors, previous)

    def _run_view(self, classification: str) -> dict[str, Any] | None:
        if self.lifecycle_journal is None:
            return None
        try:
            from workbench.app.lifecycle import LifecycleJournal
            record = LifecycleJournal(self.lifecycle_journal).read()
        except Exception as exc:
            return {"task_id": None, "run_id": None, "state": f"lifecycle_record_unreadable:{type(exc).__name__}"}
        if record is None:
            return None
        current = None
        if self._current_run is not None:
            try:
                current = self._current_run(record.task_id)
            except Exception:
                current = "unknown"
        if current is not None and current != "unknown" and current != record.run_id:
            state = "not_current"  # its Task moved on (closed or re-run) before the previous backend ended
        elif current is None:
            state = "closed"
        elif classification in (REBOOT, BOOT_UNKNOWN):
            state = "interrupted_by_reboot" if classification == REBOOT else "outcome_unknown"
        elif classification == FRESH:
            state = "unknown"
        else:
            state = "outcome_unknown"  # never success; the Task is held and nothing is re-run
        return {"task_id": record.task_id, "revision": record.revision, "run_id": record.run_id, "state": state,
                "boot_marker": record.boot_marker}


__all__ = ["AdmissionHold", "BOOT_HOLD", "METADATA_HOLD", "MODEL_HOLD_PREFIX", "PauseStore", "SHUTDOWN_HOLD",
           "StartupReconciler", "StartupResult", "Survivor", "SurvivorRegistry", "lost_outbox", "model_hold",
           "observe", "read_jsonl", "iter_jsonl"]
