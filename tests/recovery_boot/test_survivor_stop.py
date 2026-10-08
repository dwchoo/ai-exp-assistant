"""CW-19 C-D71 (1): stop_survivor proves the identity again and signals only that exact process (and its session)."""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from support import Owned, session_members, ticks, wait_for

from workbench.app.recovery import Survivor, SurvivorRegistry

BOOT = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


class SurvivorStopTests(unittest.TestCase):
    def setUp(self):
        self.owned = Owned()
        self.addCleanup(self.owned.cleanup)
        self.boot = BOOT

    def registry(self, *survivors, grace=0.3):
        return SurvivorRegistry(survivors, lambda: self.boot, grace=grace, kill_wait=1.0)

    def survivor(self, child, **extra):
        values = {"survivor_id": "s1", "name": "run_target", "pid": child.pid, "start_ticks": ticks(child.pid),
                  "boot_id": BOOT, "stoppable": True}
        values.update(extra)
        return Survivor(**values)

    def test_term_then_kill_of_a_term_ignoring_process_is_recorded(self):
        child = self.owned.spawn("trap '' TERM; while :; do sleep 0.05; done")
        time.sleep(0.3)  # the trap is set
        registry = self.registry(self.survivor(child))
        result = registry.stop("s1", reason="old experiment, not needed", requester="manager")
        self.assertEqual(result["status"], "stopped", result)
        self.assertEqual(child.wait(5), -9)  # TERM ignored, KILL after the grace
        view = registry.views()[0]
        self.assertEqual(view["state"], "stopped")
        self.assertEqual((view["stop"]["reason"], view["stop"]["requester"]), ("old experiment, not needed", "manager"))
        again = registry.stop("s1", reason="again", requester="manager")
        self.assertEqual((again["status"], again["reason"]), ("refused", "not_alive"))

    def test_refusals_signal_nothing(self):
        child = self.owned.spawn("exec sleep 60")
        shown = self.survivor(child, survivor_id="s2", stoppable=False, why_not="session leader gone")
        changed = self.survivor(child, survivor_id="s3", start_ticks=ticks(child.pid) + 3)  # pid reused
        registry = self.registry(self.survivor(child), shown, changed)
        with mock.patch("signal.pidfd_send_signal") as send:
            self.assertEqual(registry.stop("nope", reason="x", requester="manager")["reason"], "unknown_survivor")
            self.assertEqual(registry.stop("s2", reason="x", requester="manager")["reason"], "identity_unverified")
            changed_result = registry.stop("s3", reason="x", requester="manager")
            self.boot = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
            self.assertEqual(registry.stop("s1", reason="x", requester="manager")["reason"], "boot_changed")
            self.boot = None
            self.assertEqual(registry.stop("s1", reason="x", requester="manager")["reason"], "boot_changed")
        send.assert_not_called()
        self.assertNotEqual(changed_result["status"], "stopped")
        self.assertIsNone(child.poll())

    def test_a_session_leader_takes_its_session_members(self):
        leader = self.owned.spawn("trap '' TERM; sleep 60 & sleep 60 & wait", new_session=True)
        members = wait_for(lambda: len(session_members(leader.pid)) >= 2 and session_members(leader.pid))
        time.sleep(0.2)
        for pid in members:
            self.owned.remember(pid)
        registry = self.registry(self.survivor(leader, name="host_shell"))
        result = registry.stop("s1", reason="old shell", requester="manager")
        self.assertEqual(result["status"], "stopped", result)
        self.assertEqual(sorted(result["members"]), sorted(members))
        self.assertTrue(wait_for(lambda: not session_members(leader.pid)))

    def test_a_member_needs_its_live_leader(self):
        leader = self.owned.spawn("sleep 60 & exec sleep 61", new_session=True)
        member = wait_for(lambda: session_members(leader.pid))[0]
        self.owned.remember(member)
        survivor = Survivor("s1", "session_member", member, ticks(member), BOOT, True, session=leader.pid,
                            leader=(leader.pid, ticks(leader.pid) + 1))  # not the recorded leader any more
        with mock.patch("signal.pidfd_send_signal") as send:
            result = self.registry(survivor).stop("s1", reason="x", requester="manager")
        send.assert_not_called()
        self.assertEqual((result["status"], result["reason"]), ("refused", "identity_unverified"))
        ok = Survivor("s2", "session_member", member, ticks(member), BOOT, True, session=leader.pid,
                      leader=(leader.pid, ticks(leader.pid)))
        result = self.registry(ok).stop("s2", reason="x", requester="manager")
        self.assertEqual(result["status"], "stopped")
        self.assertIsNone(leader.poll())  # only the member

    def test_a_gone_survivor_is_reported_as_ended(self):
        child = self.owned.spawn("exec sleep 60")
        survivor = self.survivor(child)
        os.kill(child.pid, 9)
        child.wait(5)
        registry = self.registry(survivor)
        self.assertEqual(registry.alive(), [])
        self.assertEqual(registry.carried(), [])


if __name__ == "__main__":
    unittest.main()
