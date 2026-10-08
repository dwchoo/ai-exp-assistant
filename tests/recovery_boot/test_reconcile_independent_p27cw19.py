"""Independent CW-19 checks (p27-cw19-test-01): boot marker, start-up reconcile and no replay.

Derived from tickets/CW-19.md and DECISIONS C-D71 (not from the implementer's tests):

- the boot marker is compared before any /proc identity check; a process recorded under another or an unknown
  boot is never probed (its pid may be anything now), and an unreadable marker or boot record fails closed;
- on the same boot, identity is pid + start ticks + owner: a recycled pid is "ended", a live process of another
  user is never a stoppable survivor, and nothing is signalled by the reconcile itself (C-D71 (1));
- a pending boot confirmation survives a crash before confirm-boot; the confirmation is durable before the hold
  is lifted, and a confirmation that cannot be written keeps the hold;
- the outbox listing names messages that were queued and never submitted and those submitted without an
  outcome; nothing is resent (C-AC-16, R9);
- the reconcile runs before any pane / TaskFlow / UI / record overwrite (Backend.run ordering).

Only processes started by this test are signalled (exact pid + start ticks); temp files live under /tmp.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from workbench.app import recovery as recovery_module
from workbench.app.recovery import (
    BOOT_HOLD, AdmissionHold, StartupReconciler, lost_outbox, read_jsonl,
)
from workbench.backend import boot as boot_module
from workbench.backend.boot import BootStore, classify
from workbench.backend.paths import DataLayout, ensure_private_dir, write_private_json

BOOT_A = "aaaaaaaa-0000-4000-8000-0000000c0019"
BOOT_B = "bbbbbbbb-0000-4000-8000-0000000c0019"


def start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    return int(raw[raw.rfind(b") ") + 2:].split()[19])


def alive(pid: int, ticks: int | None = None) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return False
    fields = raw[raw.rfind(b") ") + 2:].split()
    return fields[0] not in (b"Z", b"X") and (ticks is None or int(fields[19]) == ticks)


class OwnedProcesses:
    """Children this test started; cleanup kills only those exact identities."""

    def __init__(self):
        self.items: list[tuple[int, int, subprocess.Popen | None]] = []

    def spawn(self, script: str, *, session: bool = False) -> tuple[int, int]:
        child = subprocess.Popen(["/bin/sh", "-c", script], start_new_session=session, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while start_ticks(child.pid) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        ticks = start_ticks(child.pid)
        self.items.append((child.pid, ticks, child))
        return child.pid, ticks

    def adopt(self, pid: int) -> tuple[int, int]:
        ticks = start_ticks(pid)
        self.items.append((pid, ticks, None))
        return pid, ticks

    def cleanup(self):
        for pid, ticks, _child in self.items:
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            try:
                if start_ticks(pid) == ticks:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            except OSError:
                pass
            finally:
                os.close(fd)
        for _pid, _ticks, child in self.items:
            if child is not None:
                try:
                    child.wait(5)
                except subprocess.TimeoutExpired:
                    pass


def ref(pid: int, ticks: int, role: str = "x") -> dict:
    return {"role": role, "pid": pid, "start_ticks": ticks, "owner_epoch": 1}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw19-indep-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.layout = DataLayout(ensure_private_dir(self.root / "data"))
        ensure_private_dir(self.layout.workflow)
        self.owned = OwnedProcesses()
        self.addCleanup(self.owned.cleanup)
        self.boot = BOOT_A

    def store(self, source=None) -> BootStore:
        return BootStore(self.layout.root / "boot.json", boot_source=source or (lambda: self.boot))

    def reconcile(self, source=None, journal: Path | None = None):
        return StartupReconciler(self.layout, self.store(source),
                                 handoff_journal=journal or self.layout.workflow / "handoffs.jsonl").run()

    def previous(self, boot_id, processes, *, phase="running", shutdown=None, survivors=None):
        record = {"boot_id": boot_id, "phase": phase, "processes": processes}
        if shutdown is not None:
            record["shutdown"] = shutdown
        if survivors is not None:
            record["survivors"] = survivors
        write_private_json(self.layout.record, record)
        return record


class ClassifyMatrix(unittest.TestCase):
    """C-AC-23: the boot marker decides first; an unknown marker is never 'same boot'."""

    def test_matrix(self):
        crash = {"boot_id": BOOT_A, "phase": "running"}
        clean = {"boot_id": BOOT_A, "phase": "stopped", "shutdown": {"verified": True}}
        unverified = {"boot_id": BOOT_A, "phase": "stopped", "shutdown": {"verified": False}}
        no_result = {"boot_id": BOOT_A, "phase": "stopped"}
        cases = [
            (BOOT_A, None, False, "fresh"),
            (BOOT_A, None, True, "boot_unknown"),  # data dir used before, record gone: never "fresh"
            (None, crash, True, "boot_unknown"),  # marker unreadable: never "same boot"
            (None, None, False, "boot_unknown"),
            (BOOT_B, crash, True, "reboot"),
            (BOOT_B, clean, True, "reboot"),  # a clean stop under another boot is still a reboot
            (BOOT_A, crash, True, "same_boot_crash"),
            (BOOT_A, clean, True, "same_boot_clean_stop"),
            (BOOT_A, unverified, True, "same_boot_unverified_stop"),
            (BOOT_A, no_result, True, "same_boot_unverified_stop"),  # no evidence is not a verified stop
            (BOOT_A, {"phase": "running"}, True, "boot_unknown"),
            (BOOT_A, {"boot_id": "", "phase": "stopped", "shutdown": {"verified": True}}, True, "boot_unknown"),
            (BOOT_A, {"boot_id": 7, "phase": "running"}, True, "boot_unknown"),
        ]
        for current, previous, history, expected in cases:
            with self.subTest(current=current, previous=previous, history=history):
                self.assertEqual(classify(current, previous, history=history), expected)

    def test_shutdown_verified_must_be_exactly_true(self):
        for value in ("true", 1, None, {"x": 1}):
            record = {"boot_id": BOOT_A, "phase": "stopped", "shutdown": {"verified": value}}
            self.assertEqual(classify(BOOT_A, record, history=True), "same_boot_unverified_stop", value)

    def test_read_boot_id_unreadable_or_oversized_is_none(self):
        root = Path(tempfile.mkdtemp(prefix="cw19-bootid-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        self.assertIsNone(boot_module.read_boot_id(root / "missing"))
        (root / "empty").write_text("\n")
        self.assertIsNone(boot_module.read_boot_id(root / "empty"))
        (root / "big").write_text("x" * 65)
        self.assertIsNone(boot_module.read_boot_id(root / "big"))
        real = boot_module.read_boot_id()
        self.assertTrue(real and len(real) == 36, real)


class BootStoreDurability(Base):
    """The pending confirmation is durable before anything is served; confirm writes first."""

    def test_fresh_data_dir_needs_no_confirmation(self):
        state = self.store().begin(None)
        self.assertFalse(state.pending)
        self.assertTrue(state.persisted)
        self.assertEqual(json.loads((self.layout.root / "boot.json").read_text())["recorded_boot_id"], BOOT_A)

    def test_reboot_pending_survives_a_crash_before_confirm_and_a_same_boot_restart(self):
        self.store().begin(None)  # first life under BOOT_A
        self.boot = BOOT_B
        first = self.store().begin({"boot_id": BOOT_A, "phase": "running"})
        self.assertTrue(first.pending)
        self.assertEqual(first.reason, "reboot")
        on_disk = json.loads((self.layout.root / "boot.json").read_text())
        self.assertTrue(on_disk["pending"])
        self.assertEqual(stat_mode(self.layout.root / "boot.json"), 0o600)
        # The post-reboot backend crashed before confirm-boot; its own record says BOOT_B (same boot now).
        again = self.store().begin({"boot_id": BOOT_B, "phase": "running"})
        self.assertTrue(again.pending, "a same-boot restart must not forget the pending reboot confirmation")
        self.assertEqual(again.reason, "reboot")

    def test_confirm_requires_the_current_boot_and_is_durable_before_the_hold_lifts(self):
        self.store().begin(None)
        self.boot = BOOT_B
        store = self.store()
        store.begin({"boot_id": BOOT_A, "phase": "running"})
        with self.assertRaises(ValueError):
            store.confirm(BOOT_A)  # the old boot id is not the current boot
        self.assertTrue(store.state.pending)
        confirmed = store.confirm(BOOT_B)
        self.assertFalse(confirmed.pending)
        self.assertFalse(json.loads((self.layout.root / "boot.json").read_text())["pending"])
        restarted = self.store().begin({"boot_id": BOOT_B, "phase": "running"})
        self.assertFalse(restarted.pending, "a confirmed boot stays confirmed across a crash")
        with self.assertRaises(ValueError):
            self.store().confirm(BOOT_B)  # nothing pending in a fresh store object either

    def test_confirm_refused_when_the_marker_changed_since_start(self):
        self.store().begin(None)
        self.boot = BOOT_B
        store = self.store()
        store.begin({"boot_id": BOOT_A, "phase": "running"})
        self.boot = "cccccccc-0000-4000-8000-0000000c0019"  # the marker reads differently now
        with self.assertRaises(ValueError):
            store.confirm(BOOT_B)
        self.assertTrue(store.state.pending)

    def test_confirm_that_cannot_be_written_keeps_the_hold(self):
        self.store().begin(None)
        self.boot = BOOT_B
        store = self.store()
        store.begin({"boot_id": BOOT_A, "phase": "running"})
        with mock.patch.object(boot_module, "write_private_json", side_effect=OSError(28, "No space left")):
            with self.assertRaises(OSError):
                store.confirm(BOOT_B)
        self.assertTrue(store.state.pending)
        self.assertTrue(json.loads((self.layout.root / "boot.json").read_text())["pending"])

    def test_unreadable_marker_fails_closed(self):
        self.store().begin(None)
        state = self.store(lambda: None).begin({"boot_id": BOOT_A, "phase": "running"})
        self.assertTrue(state.pending)
        self.assertEqual(state.reason, "boot_marker_unknown")
        raising = self.store(lambda: (_ for _ in ()).throw(OSError("eio"))).begin({"boot_id": BOOT_A})
        self.assertTrue(raising.pending)

    def test_corrupt_unsafe_or_foreign_boot_record_fails_closed(self):
        path = self.layout.root / "boot.json"
        variants = {
            "garbage": lambda: (path.write_text("{not json"), os.chmod(path, 0o600)),
            "group_readable": lambda: (write_private_json(path, {"version": 1, "recorded_boot_id": BOOT_A,
                                                                 "confirmed_boot_id": None, "pending": False}),
                                       os.chmod(path, 0o640)),
            "symlink": lambda: os.symlink(self.root / "elsewhere.json", path),
            "pending_not_bool": lambda: write_private_json(path, {"version": 1, "recorded_boot_id": BOOT_A,
                                                                  "pending": "no"}),
            "wrong_version": lambda: write_private_json(path, {"version": 99, "recorded_boot_id": BOOT_A,
                                                               "pending": False}),
        }
        write_private_json(self.root / "elsewhere.json", {"version": 1, "recorded_boot_id": BOOT_A,
                                                          "pending": False})
        for name, make in variants.items():
            with self.subTest(name):
                if os.path.lexists(path):
                    os.unlink(path)
                make()
                state = self.store().begin({"boot_id": BOOT_A, "phase": "stopped",
                                            "shutdown": {"verified": True}})
                self.assertTrue(state.pending, name)
                self.assertEqual(state.reason, "boot_record_unreadable")

    def test_boot_record_write_failure_is_reported_not_raised(self):
        with mock.patch.object(boot_module, "write_private_json", side_effect=PermissionError(13, "denied")):
            state = self.store().begin({"boot_id": BOOT_B, "phase": "running"})
        self.assertTrue(state.pending)
        self.assertFalse(state.persisted)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


class ReconcileBootBeforeProc(Base):
    """R2/C-D71 (1): boot comparison first; old- or unknown-boot refs never reach /proc."""

    def spy(self):
        calls: list[int] = []
        real_stat, real_observe = recovery_module._stat, recovery_module.observe

        def stat(pid):
            calls.append(pid)
            return real_stat(pid)

        def observe(pid, ticks):
            calls.append(pid)
            return real_observe(pid, ticks)
        patches = [mock.patch.object(recovery_module, "_stat", stat),
                   mock.patch.object(recovery_module, "observe", observe),
                   mock.patch.object(recovery_module, "_session_members",
                                     lambda *a, **k: calls.append(-1) or [])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return calls

    def test_old_boot_refs_are_not_probed_even_when_pid_and_ticks_match_a_live_process(self):
        # pid reuse after a reboot with colliding start ticks: our live child has exactly the recorded identity
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.store().begin(None)
        self.previous(BOOT_A, {"backend": ref(pid, ticks), "host_shell": ref(pid, ticks),
                               "manager_omp": ref(pid, ticks)})
        self.boot = BOOT_B
        calls = self.spy()
        result = self.reconcile()
        self.assertEqual(result.report["classification"], "reboot")
        self.assertTrue(result.boot.pending)
        self.assertNotIn(pid, calls, "a process recorded under another boot was read from /proc")
        self.assertNotIn(-1, calls, "session members of an old-boot pane were scanned")
        self.assertEqual(result.survivors, [])
        self.assertEqual({p["state"] for p in result.report["processes"]}, {"ended_by_reboot"})
        self.assertFalse(result.report["probed"])
        self.assertTrue(alive(pid, ticks))

    def test_unknown_marker_never_probes(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.store().begin(None)
        self.previous(BOOT_A, {"host_shell": ref(pid, ticks)})
        calls = self.spy()
        result = self.reconcile(lambda: None)
        self.assertEqual(result.report["classification"], "boot_unknown")
        self.assertTrue(result.boot.pending)
        self.assertNotIn(pid, calls)
        self.assertEqual(result.survivors, [])
        self.assertEqual([p["state"] for p in result.report["processes"]], ["not_probed_boot_unknown"])

    def test_previous_record_without_boot_id_never_probes(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.store().begin(None)
        write_private_json(self.layout.record, {"phase": "running", "processes": {"host_shell": ref(pid, ticks)}})
        calls = self.spy()
        result = self.reconcile()
        self.assertNotIn(pid, calls)
        self.assertEqual(result.survivors, [])
        self.assertEqual(result.report["classification"], "boot_unknown")
        # boot.json (the durable boot record) still says this boot, so no confirmation is asked here; the
        # previous backend record alone is not trusted for /proc probing.

    def test_carried_survivor_from_an_older_boot_is_not_probed(self):
        # The previous record is from this boot, but it carries a survivor proven under an older boot.
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.store().begin(None)
        self.previous(BOOT_A, {}, survivors=[{"survivor_id": "s1", "name": "host_shell", "pid": pid,
                                               "start_ticks": ticks, "boot_id": BOOT_B, "stoppable": True}])
        calls = self.spy()
        result = self.reconcile()
        self.assertEqual(result.report["classification"], "same_boot_crash")
        self.assertNotIn(pid, calls)
        self.assertEqual(result.survivors, [])


class ReconcileSameBootIdentity(Base):
    def setUp(self):
        super().setUp()
        self.store().begin(None)

    def test_live_exact_identity_is_a_stoppable_survivor_and_is_not_signalled(self):
        pid, ticks = self.owned.spawn("exec sleep 60", session=True)
        self.previous(BOOT_A, {"host_shell": ref(pid, ticks), "worker_omp": ref(pid + 10_000_000, 5)})
        result = self.reconcile()
        self.assertEqual(result.report["classification"], "same_boot_crash")
        self.assertTrue(result.report["probed"])
        self.assertFalse(result.boot.pending)
        names = {s.name: s for s in result.survivors}
        self.assertIn("host_shell", names)
        self.assertTrue(names["host_shell"].stoppable)
        self.assertEqual(names["host_shell"].boot_id, BOOT_A)
        self.assertEqual(names["host_shell"].start_ticks, ticks)
        states = {p["name"]: p["state"] for p in result.report["processes"]}
        self.assertEqual(states["worker_omp"], "ended")
        time.sleep(0.2)
        self.assertTrue(alive(pid, ticks), "the reconcile itself must never signal a survivor (C-D71 (1))")

    def test_recycled_pid_with_other_start_ticks_is_ended_not_a_survivor(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.previous(BOOT_A, {"host_shell": ref(pid, ticks - 1 if ticks > 1 else ticks + 1),
                               "manager_omp": ref(pid, ticks + 7)})
        result = self.reconcile()
        self.assertEqual(result.survivors, [])
        self.assertEqual({p["state"] for p in result.report["processes"]}, {"ended"})

    def test_process_of_another_user_is_never_a_survivor(self):
        init_ticks = start_ticks(1)
        if init_ticks is None:
            self.skipTest("/proc/1/stat unreadable")
        self.previous(BOOT_A, {"host_shell": ref(1, init_ticks)})
        result = self.reconcile()
        self.assertEqual(result.survivors, [], "pid 1 (root) must never be listed as a stoppable survivor")
        self.assertEqual([p["state"] for p in result.report["processes"]], ["unknown"])

    def test_invalid_refs_are_unrecorded(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        self.previous(BOOT_A, {"host_shell": {"pid": str(pid), "start_ticks": ticks},
                               "manager_omp": {"pid": pid, "start_ticks": 0},
                               "worker_omp": {"pid": True, "start_ticks": ticks},
                               "supervisor": {"pid": -1, "start_ticks": ticks}})
        result = self.reconcile()
        self.assertEqual(result.survivors, [])
        self.assertEqual(result.report["processes"], [])

    def test_session_members_are_stoppable_only_while_the_recorded_leader_lives(self):
        # leader (own session) with a background member; record the leader as the old host shell
        leader, leader_ticks = self.owned.spawn("sleep 60 & wait", session=True)
        member = wait_member(leader)
        self.assertIsNotNone(member, "the session member did not start")
        self.owned.adopt(member)
        self.previous(BOOT_A, {"host_shell": ref(leader, leader_ticks)})
        result = self.reconcile()
        members = [s for s in result.survivors if s.name == "session_member"]
        self.assertEqual([s.pid for s in members], [member])
        self.assertTrue(members[0].stoppable)
        self.assertEqual(members[0].leader, (leader, leader_ticks))

    def test_session_members_of_a_dead_leader_are_display_only(self):
        leader, leader_ticks = self.owned.spawn("sleep 60 & wait", session=True)
        member = wait_member(leader)
        self.assertIsNotNone(member)
        self.owned.adopt(member)
        kill_exact(leader, leader_ticks)
        deadline = time.monotonic() + 5
        while alive(leader) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.previous(BOOT_A, {"host_shell": ref(leader, leader_ticks)})
        result = self.reconcile()
        members = [s for s in result.survivors if s.pid == member]
        self.assertEqual(len(members), 1)
        self.assertFalse(members[0].stoppable)
        self.assertIsNone(members[0].leader)
        self.assertTrue(members[0].why_not)

    def test_startup_report_written_private_and_previous_record_kept(self):
        pid, ticks = self.owned.spawn("exec sleep 60")
        previous = self.previous(BOOT_A, {"host_shell": ref(pid, ticks)})
        self.reconcile()
        kept = json.loads((self.layout.root / "backend.prev.json").read_text())
        self.assertEqual(kept, previous)
        self.assertEqual(stat_mode(self.layout.root / "startup.json"), 0o600)
        report = json.loads((self.layout.root / "startup.json").read_text())
        self.assertEqual(report["classification"], "same_boot_crash")


def wait_member(leader: int, timeout: float = 5.0) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name in os.listdir("/proc"):
            if not name.isdigit() or int(name) == leader:
                continue
            try:
                raw = Path(f"/proc/{name}/stat").read_bytes()
            except OSError:
                continue
            fields = raw[raw.rfind(b") ") + 2:].split()
            if int(fields[3]) == leader and fields[0] not in (b"Z", b"X"):
                return int(name)
        time.sleep(0.02)
    return None


def kill_exact(pid: int, ticks: int) -> None:
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if start_ticks(pid) == ticks:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    finally:
        os.close(fd)


class OutboxNoReplay(Base):
    """R9/C-AC-16: lost outbox messages are listed, never resent; terminal ones are not listed."""

    def journal(self, records):
        path = self.layout.workflow / "handoffs.jsonl"
        with open(path, "w") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")
            stream.write("{torn line without end")
        os.chmod(path, 0o600)
        return path

    def records(self):
        o = lambda hid, state, **kw: {"type": "outbox", "handoff_id": hid, "state": state, **kw}  # noqa: E731
        return [
            o("old", "pending", target_role="worker", kind="TASK"),  # before the last backend_start: not listed
            {"type": "backend_start", "pid": 1},
            o("never", "pending", target_role="worker", kind="TASK"),
            o("never", "created", message_id="m-never"),
            o("kept", "pending", target_role="manager", kind="REPORT"),
            o("kept", "kept_paused"),
            o("sub", "pending", target_role="worker", kind="TASK"),
            o("sub", "created", message_id="m-sub"),
            o("sub", "submitted", message_id="m-sub"),
            o("done", "pending", target_role="worker", kind="TASK"),
            o("done", "submitted"),
            o("done", "delivered"),
            o("unk", "pending", target_role="worker", kind="TASK"),
            o("unk", "unknown"),
            o("held", "pending", target_role="worker", kind="TASK"),
            o("held", "held_paused"),
            o("gone", "pending", target_role="worker", kind="TASK"),
            o("gone", "withdrawn"),
            o("rej", "pending", target_role="worker", kind="TASK"),
            o("rej", "rejected"),
            o("defer", "pending", target_role="worker", kind="TASK"),
            o("defer", "deferred"),
            o("req", "pending", target_role="manager", kind="REPORT"),
            o("req", "submitted"),
            o("req", "requeued"),  # submitted once: its outcome stays unknown, never "not sent"
        ]

    def test_listing(self):
        lost = {item["handoff_id"]: item for item in lost_outbox(read_jsonl(self.journal(self.records())))}
        self.assertEqual(set(lost), {"never", "kept", "sub", "defer", "req"})
        self.assertEqual(lost["never"]["state"], "queued_not_sent")
        self.assertEqual(lost["never"]["target_role"], "worker")
        self.assertEqual(lost["never"]["kind"], "TASK")
        self.assertEqual(lost["kept"]["state"], "queued_not_sent")
        self.assertEqual(lost["defer"]["state"], "queued_not_sent")
        self.assertEqual(lost["sub"]["state"], "submitted_outcome_unknown")
        self.assertEqual(lost["req"]["state"], "submitted_outcome_unknown")

    def test_reconcile_reports_counts_and_resends_nothing(self):
        self.store().begin(None)
        self.previous(BOOT_A, {})
        path = self.journal(self.records())
        before = path.read_bytes()
        result = self.reconcile(journal=path)
        self.assertEqual(result.report["outbox_lost_count"], 5)
        self.assertEqual(path.read_bytes(), before, "the reconcile must not append or rewrite the journal")

    def test_unreadable_or_symlinked_journal_lists_nothing_and_does_not_raise(self):
        target = self.root / "real.jsonl"
        target.write_text(json.dumps({"type": "outbox", "handoff_id": "x", "state": "pending"}) + "\n")
        link = self.layout.workflow / "handoffs.jsonl"
        os.symlink(target, link)
        self.assertEqual(read_jsonl(link), [], "a symlinked journal must not be followed")


class AdmissionHoldOrder(unittest.TestCase):
    def test_reason_for_roles(self):
        hold = AdmissionHold()
        self.assertIsNone(hold.reason_for("worker"))
        hold.set("model_hold:manager")
        self.assertIsNone(hold.reason_for("worker"), "a manager model error must not hold worker work")
        self.assertEqual(hold.reason_for("manager"), "model_hold:manager")
        hold.set("metadata_unavailable")
        self.assertEqual(hold.reason_for("worker"), "metadata_unavailable")
        hold.set(BOOT_HOLD)
        self.assertEqual(hold.reason_for("worker"), BOOT_HOLD)
        self.assertTrue(hold.clear(BOOT_HOLD))
        self.assertFalse(hold.clear(BOOT_HOLD))
        self.assertEqual(hold.reason_for(None), "metadata_unavailable")
        hold.clear("metadata_unavailable")
        self.assertEqual(hold.reason_for(None), "model_hold:manager")
        self.assertFalse(hold.set("model_hold:manager"), "a reason is set once")


class StartupOrdering(Base):
    """CW-19: the reconcile runs before any pane, TaskFlow load, UI socket or record overwrite."""

    def test_backend_run_reconciles_before_open_and_before_the_record_is_overwritten(self):
        from workbench.backend.service import Backend
        from workbench.backend.launcher import LaunchPlan
        from workbench.terminal.shell_g2.prototype import ShellChoice
        previous = self.previous(BOOT_A, {"host_shell": ref(4_000_000, 1)})
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/nonexistent/omp", "omp/0", "/x/bridge.ts", ())
        backend = Backend(self.layout, plan, project_dir=str(self.root), environment={"PATH": "/usr/bin:/bin"},
                          boot_source=lambda: BOOT_A)
        order: list[str] = []
        seen: dict = {}
        real = backend._reconcile_startup

        def reconcile():
            order.append("reconcile")
            seen["record_at_reconcile"] = json.loads(self.layout.record.read_text())
            real()

        def open_():
            order.append("open")
            seen["startup_at_open"] = backend.startup
            raise RuntimeError("stop here")
        with mock.patch.object(backend, "_reconcile_startup", reconcile), \
                mock.patch.object(backend, "_open", open_), \
                mock.patch.object(backend, "_close", lambda: {"verified": True}):
            backend.run()
        self.assertEqual(order, ["reconcile", "open"])
        self.assertEqual(seen["record_at_reconcile"], previous)
        self.assertEqual(seen["startup_at_open"]["classification"], "same_boot_crash")

    def test_a_reconcile_exception_fails_closed(self):
        from workbench.backend.service import Backend
        from workbench.backend.launcher import LaunchPlan
        from workbench.terminal.shell_g2.prototype import ShellChoice
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/nonexistent/omp", "omp/0", "/x/bridge.ts", ())
        backend = Backend(self.layout, plan, project_dir=str(self.root), environment={"PATH": "/usr/bin:/bin"},
                          boot_source=lambda: BOOT_A)
        with mock.patch.object(recovery_module.StartupReconciler, "run", side_effect=RuntimeError("boom")):
            backend._reconcile_startup()
        self.assertEqual(backend._hold_reason("worker"), BOOT_HOLD)
        self.assertTrue(backend.boot["confirmation_required"])
        self.assertEqual(backend.survivors.views(), [])


if __name__ == "__main__":
    unittest.main()
