"""p27-polish-01 (5) (cd70 review-03 P3-1): ``reports_resent`` counts per delivering manager session.

A worker report queued while no manager OMP was connected (unbound) is counted by a ``manager_recovery`` notice.
When the manager session changes twice in a narrow window (S2 registers and is counted, then S2 goes away
before the lane created the report, S3 registers and the report is created for S3), the S3 notice must count
it too: the report arrives at S3, not at the dead S2. Never twice for the same session, never for a session
the report was not created for, and still once overall in the plain case.
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from workbench.contracts.v1 import ActorRole
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected

import test_cd70_fix03 as f3
import test_task_flow as fx

S2 = (str(uuid4()), 1)
S3 = (str(uuid4()), 1)
S4 = (str(uuid4()), 1)


class ResentCountTests(f3.Fixture):
    def block_creation(self):
        """The lane cannot create the report yet (it is between two attempts)."""
        original = self.mailbox.create_message
        blocked = [True]

        def create(*args, **kwargs):
            target = ActorRole(args[4] if len(args) > 4 else kwargs["target_role"])
            if target is ActorRole.MANAGER and blocked[0]:
                raise BridgeDisconnected("not yet")
            return original(*args, **kwargs)
        self.mailbox.create_message = create
        return lambda: blocked.__setitem__(0, False)

    def test_a_report_created_for_the_second_new_session_is_counted_by_its_recovery(self):
        self.report_while_manager_gone()
        unblock = self.block_creation()
        self.manager_session = S2
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 1, "S2's notice counts it")
        self.manager_session = S3  # S2 went away before the report was created for it
        unblock()
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "delivered"))
        self.assertEqual([m.session_id for m in self.manager_reports()], [S3[0]])
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 1,
                         "S3's notice counts the report that reached S3")
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 0, "once per session")
        self.manager_session = S4
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 0,
                         "a later session the report was not created for")
        self.assertEqual(len(self.manager_reports()), 1, "delivered once")

    def test_a_report_still_waiting_is_counted_again_by_the_next_session_only_once(self):
        self.report_while_manager_gone()
        unblock = self.block_creation()
        self.manager_session = S2
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 1)
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 0, "same session: once")
        self.manager_session = S3
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 1)
        unblock()
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "delivered"))
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 0)
        self.assertEqual([m.session_id for m in self.manager_reports()], [S3[0]])

    def test_a_report_delivered_to_the_first_new_session_is_not_counted_by_the_next(self):
        self.report_while_manager_gone()
        self.manager_session = S2
        self.assertTrue(fx.wait_until(lambda: self.service.report_entries()[0]["state"] == "delivered"))
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 1)
        self.manager_session = S3
        self.assertEqual(self.service.requeue_for_new_session(ActorRole.MANAGER), 0)


if __name__ == "__main__":
    unittest.main()
