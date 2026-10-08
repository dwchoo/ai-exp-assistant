"""CW-19 U1: boot marker classification, durable boot confirmation and the start-up reconcile."""
from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest import mock

from support import Owned, TempData, ref, session_members, ticks, wait_for

from workbench.app import recovery
from workbench.app.recovery import (
    AdmissionHold, BOOT_HOLD, METADATA_HOLD, PauseStore, StartupReconciler, lost_outbox, model_hold,
)
from workbench.backend.boot import (
    BOOT_UNKNOWN, FRESH, REBOOT, SAME_BOOT_CLEAN_STOP, SAME_BOOT_CRASH, SAME_BOOT_UNVERIFIED_STOP, BootStore,
    classify,
)
from workbench.backend.paths import write_private_json

BOOT_A, BOOT_B = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


class ClassifyTests(unittest.TestCase):
    def test_matrix(self):
        crashed = {"boot_id": BOOT_A, "phase": "ready"}
        clean = {"boot_id": BOOT_A, "phase": "stopped", "shutdown": {"verified": True}}
        unverified = {"boot_id": BOOT_A, "phase": "stopped", "shutdown": {"verified": False}}
        self.assertEqual(classify(BOOT_A, None, history=False), FRESH)
        self.assertEqual(classify(BOOT_A, crashed, history=True), SAME_BOOT_CRASH)
        self.assertEqual(classify(BOOT_A, clean, history=True), SAME_BOOT_CLEAN_STOP)
        self.assertEqual(classify(BOOT_A, unverified, history=True), SAME_BOOT_UNVERIFIED_STOP)
        self.assertEqual(classify(BOOT_B, crashed, history=True), REBOOT)
        self.assertEqual(classify(None, crashed, history=True), BOOT_UNKNOWN)  # never "same boot"
        self.assertEqual(classify(BOOT_A, {"phase": "ready"}, history=True), BOOT_UNKNOWN)
        self.assertEqual(classify(BOOT_A, None, history=True), BOOT_UNKNOWN)


class BootStoreTests(unittest.TestCase):
    def setUp(self):
        self.data = TempData()
        self.addCleanup(self.data.cleanup)
        self.path = self.data.layout.root / "boot.json"

    def store(self, boot):
        return BootStore(self.path, boot_source=lambda: boot)

    def test_fresh_then_same_boot_needs_no_confirmation(self):
        state = self.store(BOOT_A).begin(None)
        self.assertFalse(state.pending)
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        self.assertFalse(self.store(BOOT_A).begin({"boot_id": BOOT_A}).pending)

    def test_reboot_pending_survives_a_crash_and_confirmation_is_durable(self):
        self.store(BOOT_A).begin(None)
        first = self.store(BOOT_B).begin({"boot_id": BOOT_A, "phase": "ready"})
        self.assertTrue(first.pending)
        self.assertEqual((first.reason, first.recorded, first.current), ("reboot", BOOT_A, BOOT_B))
        # crash before confirm-boot: the next start (same boot B) is still pending
        again = self.store(BOOT_B)
        state = again.begin({"boot_id": BOOT_B, "phase": "ready"})
        self.assertTrue(state.pending)
        with self.assertRaises(ValueError):
            again.confirm(BOOT_A)  # not the current boot
        confirmed = again.confirm(BOOT_B)
        self.assertFalse(confirmed.pending)
        self.assertTrue(confirmed.view()["confirmed"])
        # confirmed survives a restart
        self.assertFalse(self.store(BOOT_B).begin({"boot_id": BOOT_B, "phase": "ready"}).pending)

    def test_unknown_marker_and_corrupt_record_fail_closed(self):
        self.store(BOOT_A).begin(None)
        state = self.store(None).begin({"boot_id": BOOT_A})
        self.assertEqual((state.pending, state.reason), (True, "boot_marker_unknown"))
        self.path.write_text("{not json")
        os.chmod(self.path, 0o600)
        state = self.store(BOOT_A).begin({"boot_id": BOOT_A})
        self.assertEqual((state.pending, state.reason), (True, "boot_record_unreadable"))
        os.chmod(self.path, 0o644)  # unsafe mode is not trusted either
        self.path.write_text('{"version": 1, "pending": false}')
        self.assertTrue(BootStore(self.path, boot_source=lambda: BOOT_A).begin({"boot_id": BOOT_A}).pending)

    def test_previous_backend_without_boot_record_compares_its_boot(self):
        # a data dir from before boot.json: backend.json's boot id decides
        self.assertTrue(self.store(BOOT_B).begin({"boot_id": BOOT_A}).pending)

    def test_unwritable_state_still_holds_in_memory(self):
        self.store(BOOT_A).begin(None)
        with mock.patch("workbench.backend.boot.write_private_json", side_effect=OSError("ro")):
            state = self.store(BOOT_B).begin({"boot_id": BOOT_A})
        self.assertTrue(state.pending)
        self.assertFalse(state.persisted)


class HoldTests(unittest.TestCase):
    def test_reasons_by_role(self):
        hold = AdmissionHold()
        self.assertIsNone(hold.reason_for("worker"))
        hold.set(model_hold("worker"))
        self.assertEqual(hold.reason_for("worker"), "model_hold:worker")
        self.assertIsNone(hold.reason_for("manager"))
        self.assertEqual(hold.reason_for(None), "model_hold:worker")
        hold.set(BOOT_HOLD)
        self.assertEqual(hold.reason_for("manager"), BOOT_HOLD)
        hold.set(METADATA_HOLD)
        self.assertEqual(hold.reason_for("worker"), BOOT_HOLD)  # boot first
        self.assertTrue(hold.clear(BOOT_HOLD))
        self.assertEqual(hold.reason_for("manager"), METADATA_HOLD)


class PauseStoreTests(unittest.TestCase):
    def test_durable_pause_and_fail_closed(self):
        data = TempData()
        self.addCleanup(data.cleanup)
        store = PauseStore(data.layout.root / "automation.json")
        self.assertFalse(store.load())
        self.assertTrue(store.save(True))
        self.assertTrue(PauseStore(store.path).load())
        self.assertTrue(store.save(False))
        self.assertFalse(PauseStore(store.path).load())
        store.path.write_text("garbage")
        os.chmod(store.path, 0o600)
        self.assertTrue(PauseStore(store.path).load())  # unreadable: kept paused


class LostOutboxTests(unittest.TestCase):
    def test_only_the_last_incarnation_without_a_terminal_state(self):
        records = [
            {"type": "outbox", "handoff_id": "old", "target_role": "worker", "kind": "task", "state": "pending"},
            {"type": "backend_start", "pid": 1},
            {"type": "outbox", "handoff_id": "q", "target_role": "manager", "kind": "report", "state": "pending"},
            {"type": "outbox", "handoff_id": "q", "state": "created", "message_id": "m1"},
            {"type": "outbox", "handoff_id": "s", "target_role": "worker", "kind": "question", "state": "pending"},
            {"type": "outbox", "handoff_id": "s", "state": "submitted"},
            {"type": "outbox", "handoff_id": "d", "target_role": "worker", "kind": "task", "state": "pending"},
            {"type": "outbox", "handoff_id": "d", "state": "submitted"},
            {"type": "outbox", "handoff_id": "d", "state": "delivered"},
        ]
        lost = {item["handoff_id"]: item for item in lost_outbox(records)}
        self.assertEqual(set(lost), {"q", "s"})
        self.assertEqual(lost["q"]["state"], "queued_not_sent")
        self.assertEqual(lost["q"]["message_id"], "m1")
        self.assertEqual(lost["s"]["state"], "submitted_outcome_unknown")


class StartupReconcileTests(unittest.TestCase):
    def setUp(self):
        self.data = TempData()
        self.addCleanup(self.data.cleanup)
        self.owned = Owned()
        self.addCleanup(self.owned.cleanup)

    def reconcile(self, boot, **kwargs):
        layout = self.data.layout
        return StartupReconciler(layout, BootStore(layout.root / "boot.json", boot_source=lambda: boot),
                                 handoff_journal=layout.workflow / "handoffs.jsonl", **kwargs).run()

    def test_fresh_data_dir(self):
        result = self.reconcile(BOOT_A)
        self.assertEqual(result.report["classification"], FRESH)
        self.assertFalse(result.boot.pending)
        self.assertEqual(result.survivors, [])

    def test_same_boot_crash_lists_proven_survivors_and_signals_nothing(self):
        alive = self.owned.spawn("exec sleep 60")
        dead = self.owned.spawn("exit 0")
        dead.wait(5)
        dead_ticks = 1234
        reused = self.owned.spawn("exec sleep 60")
        self.data.previous({"boot_id": BOOT_A, "phase": "ready", "processes": {
            "backend": ref(dead.pid, dead_ticks, "backend"),
            "host_shell": ref(alive.pid, ticks(alive.pid), "host_shell"),
            "worker_omp": ref(reused.pid, ticks(reused.pid) + 7, "worker"),  # pid reused: another process now
        }})
        with mock.patch("os.kill") as kill, mock.patch("signal.pidfd_send_signal") as send:
            result = self.reconcile(BOOT_A)
        kill.assert_not_called()
        send.assert_not_called()
        self.assertEqual(result.report["classification"], SAME_BOOT_CRASH)
        states = {item["name"]: item["state"] for item in result.report["processes"]}
        self.assertEqual(states, {"backend": "ended", "host_shell": "alive", "worker_omp": "ended"})
        self.assertEqual([(s.name, s.pid, s.stoppable) for s in result.survivors], [("host_shell", alive.pid, True)])
        self.assertTrue((self.data.layout.root / "backend.prev.json").exists())
        self.assertTrue((self.data.layout.root / "startup.json").exists())
        self.assertIsNone(alive.poll())

    def test_other_boot_refs_are_never_probed(self):
        alive = self.owned.spawn("exec sleep 60")
        BootStore(self.data.layout.root / "boot.json", boot_source=lambda: BOOT_A).begin(None)
        self.data.previous({"boot_id": BOOT_A, "phase": "ready", "processes": {
            "host_shell": ref(alive.pid, ticks(alive.pid), "host_shell")},
            "survivors": [{"survivor_id": "s1", "name": "run_target", "pid": alive.pid,
                           "start_ticks": ticks(alive.pid), "boot_id": BOOT_A, "stoppable": True}]})
        with mock.patch.object(recovery, "observe", wraps=recovery.observe) as probe, \
                mock.patch.object(recovery, "_session_members", wraps=recovery._session_members) as members:
            result = self.reconcile(BOOT_B)
        probe.assert_not_called()  # R2: a pid recorded under another boot may be anything now
        members.assert_not_called()
        self.assertEqual(result.report["classification"], REBOOT)
        self.assertTrue(result.boot.pending)
        self.assertEqual({item["state"] for item in result.report["processes"]}, {"ended_by_reboot"})
        self.assertEqual(result.survivors, [])

    def test_unknown_boot_probes_nothing_and_holds(self):
        alive = self.owned.spawn("exec sleep 60")
        self.data.previous({"boot_id": BOOT_A, "phase": "ready",
                            "processes": {"host_shell": ref(alive.pid, ticks(alive.pid))}})
        with mock.patch.object(recovery, "observe") as probe:
            result = self.reconcile(None)
        probe.assert_not_called()
        self.assertEqual(result.report["classification"], BOOT_UNKNOWN)
        self.assertTrue(result.boot.pending)
        self.assertEqual(result.survivors, [])

    def test_members_of_a_previous_pane_session(self):
        leader = self.owned.spawn("sleep 60 & sleep 60 & wait", new_session=True)
        members = wait_for(lambda: len(session_members(leader.pid)) >= 2 and session_members(leader.pid))
        for pid in members:
            self.owned.remember(pid)
        self.data.previous({"boot_id": BOOT_A, "phase": "ready", "processes": {
            "host_shell": ref(leader.pid, ticks(leader.pid), "host_shell")}})
        result = self.reconcile(BOOT_A)
        by_pid = {s.pid: s for s in result.survivors}
        self.assertTrue(by_pid[leader.pid].stoppable)
        for pid in members:
            self.assertEqual(by_pid[pid].name, "session_member")
            self.assertTrue(by_pid[pid].stoppable)  # its live leader proves the session
            self.assertEqual(by_pid[pid].leader, (leader.pid, ticks(leader.pid)))

    def test_members_without_their_leader_are_only_shown(self):
        leader = self.owned.spawn("sleep 60 & sleep 60 & wait", new_session=True)
        members = wait_for(lambda: len(session_members(leader.pid)) >= 2 and session_members(leader.pid))
        for pid in members:
            self.owned.remember(pid)
        leader_ticks = ticks(leader.pid)
        os.kill(leader.pid, 9)  # our own child
        leader.wait(5)
        self.data.previous({"boot_id": BOOT_A, "phase": "ready", "processes": {
            "host_shell": ref(leader.pid, leader_ticks, "host_shell")}})
        result = self.reconcile(BOOT_A)
        shown = [s for s in result.survivors if s.pid in members]
        self.assertEqual(len(shown), 2)
        self.assertTrue(all(not s.stoppable and "session leader" in s.why_not for s in shown))

    def test_run_state_and_lost_outbox_are_reported(self):
        self.data.handoffs([{"type": "backend_start"},
                            {"type": "outbox", "handoff_id": "h1", "target_role": "worker", "kind": "task",
                             "state": "pending"}])
        self.data.previous({"boot_id": BOOT_A, "phase": "ready", "processes": {}})
        result = self.reconcile(BOOT_A)
        self.assertEqual(result.report["outbox_lost_count"], 1)
        self.assertEqual(result.report["outbox_lost"][0]["state"], "queued_not_sent")

    def test_clean_and_unverified_stops(self):
        self.data.previous({"boot_id": BOOT_A, "phase": "stopped", "shutdown": {"verified": True}})
        self.assertEqual(self.reconcile(BOOT_A).report["classification"], SAME_BOOT_CLEAN_STOP)
        self.data.previous({"boot_id": BOOT_A, "phase": "stopped", "shutdown": {"verified": False}})
        self.assertEqual(self.reconcile(BOOT_A).report["classification"], SAME_BOOT_UNVERIFIED_STOP)


if __name__ == "__main__":
    unittest.main()
