"""p27-cd70-ui-01 (b): a worker report queued while no manager OMP is connected shows in ``recovery.report_wait``
with its own reason (``manager_session_not_connected``) and clears once it is delivered.

The real HandoffService + TaskFlow + Watchdog with the fake mailbox/peers of test_cd70_fix03 (manager peer absent).
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from workbench.backend.flow_recovery import Watchdog, WatchPorts
from workbench.contracts.v1 import ActorRole

import test_cd70_fix03 as f3
import test_task_flow as fx

NEW_MANAGER = str(uuid4())


class Fixture(f3.Fixture):
    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.dog = Watchdog(WatchPorts(
            task=self.flow.watch_view, task_summary=self.flow.task_view, peer=self.peer,
            probe=lambda role: {"idle": True, "pending": False, "approvalPending": False, "inFlightToolCount": 0},
            turns=lambda cursor: (cursor, False), terminal=lambda: {"running": False},
            outbox_busy=self.service.lane_busy, reports=self.service.report_entries,
            requeue=lambda: self.service.requeue_for_new_session(ActorRole.MANAGER),
            notify=lambda role, notice: "not_connected", worker_state=self.flow.worker_view),
            clock=lambda: self.now, wall=lambda: 5000.0)


class ReportWaitForMissingManagerTests(Fixture):
    def test_an_unbound_report_is_a_report_wait_with_its_own_reason_until_delivered(self):
        self.assertIsNone(self.dog.view()["report_wait"])
        self.report_while_manager_gone()
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["reason"] == "target_not_connected"))
        entry = self.service.report_entries()[0]
        self.assertEqual(entry["waiting_for"], f3.WAITING)
        self.now += 3
        wait = self.dog.view()["report_wait"]
        self.assertEqual((wait["count"], wait["reason"]), (1, "manager_session_not_connected"))
        self.assertIsInstance(wait["since"], float)  # shown at once: no 30 s delay
        # a new manager session registers, the report is delivered and the wait is gone
        self.manager_session = (NEW_MANAGER, 1)
        self.service.requeue_for_new_session(ActorRole.MANAGER)
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "delivered"))
        self.assertIsNone(self.dog.view()["report_wait"])
        self.assertIsNone(self.service.report_entries()[0].get("waiting_for"))

    def test_a_report_for_a_connected_manager_is_not_waiting_for_a_session(self):
        task_id = self.work()
        self.to_manager({"kind": "done", "message": "ok", "task_id": task_id})
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "delivered"))
        self.assertIsNone(self.service.report_entries()[0].get("waiting_for"))
        self.assertIsNone(self.dog.view()["report_wait"])

    def test_the_editor_wait_is_unchanged_and_the_missing_manager_takes_precedence(self):
        self.service_reports = [
            {"handoff_id": "a", "origin": "worker", "state": "pending", "submitted": False,
             "editor_since": self.now - 40},
            {"handoff_id": "b", "origin": "worker", "state": "pending", "submitted": False,
             "waiting_for": f3.WAITING, "waiting_since": self.now - 2}]
        dog = Watchdog(WatchPorts(task=lambda: None, reports=lambda: self.service_reports),
                       clock=lambda: self.now, wall=lambda: 5000.0)
        wait = dog.report_wait()
        self.assertEqual((wait["count"], wait["reason"]), (2, "manager_session_not_connected"))
        self.service_reports.pop()
        wait = dog.report_wait()
        self.assertEqual((wait["count"], wait["reason"]), (1, "manager_editor_not_empty"))


if __name__ == "__main__":
    unittest.main()
