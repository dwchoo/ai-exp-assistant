"""C-D69 (5)(a) as amended in p27-cd69-cmds-01 (g), independent (p27-cd69-cmds-test-01): a failed experiment start.

Derived from DECISIONS.md C-D69 (5)(a): after any start failure the host shell returns to the user and nothing
the user types mixes with Workbench's own input. Checked here on the real TaskWorkflow and a real backend
ShellPane (bash and dash): the start's input hold lasts through the give-back (takeover and the ``cd`` back to the
user's directory); only a ``wb-handoff`` that was really typed makes the give-back wait for a control wait; the
product port is handed back unchanged. Fake mailboxes; no OMP, no provider.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from workbench.backend.flow_tasks import HOLD_REASON, TaskFlow
from workbench.backend.panes import HostShellPort
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.workflow import run as run_module

import test_task_flow as fx
import test_task_flow_independent_p27cd69c as c69

_Base = c69.BashExperimentTests.__mro__[1]


class _StartFailure(_Base):
    def setUp(self):
        super().setUp()
        self.ports: list[HostShellPort] = []
        original = HostShellPort.detach

        def detach(port):
            self.ports.append(port)  # the workflow may detach while the start still watches the port
            return original(port)

        patcher = mock.patch.object(HostShellPort, "detach", detach)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_ports_unchanged(self):
        self.assertTrue(self.ports)
        self.assertEqual([p for p in self.ports if "send_user" in vars(p)], [],
                         "after the start every port's own send_user is back (no wrapper left)")

    def spy_give_back(self):
        seen: list[dict] = []
        original = TaskFlow._give_back

        def give_back(flow, port, ports=None, *, after_failure=False, hold_reason=None):
            seen.append({"after_failure": after_failure, "hold_reason": hold_reason,
                         "hold": self.pane.automation_hold})
            return original(flow, port, ports, after_failure=after_failure, hold_reason=hold_reason)

        return seen, mock.patch.object(TaskFlow, "_give_back", give_back)

    def spy_settled(self):
        calls: list[float] = []
        original = TaskFlow._await_settled

        def settled(port):
            calls.append(time.monotonic())
            return original(port)

        return calls, mock.patch.object(TaskFlow, "_await_settled", staticmethod(settled))

    def test_a_failure_after_wb_handoff_holds_input_through_the_give_back(self):
        during: list[tuple] = []
        restore, takeover = HostShellPort.restore_cwd, HostShellPort.request_takeover

        def restore_cwd(port, target, inside, **kwargs):
            refused = self.pane.admit(b"echo typed-during-restore\r")
            during.append(("restore", self.pane.automation_hold, kwargs.get("reason"), refused is not None))
            return restore(port, target, inside, **kwargs)

        def request_takeover(port):
            during.append(("takeover", self.pane.automation_hold))
            return takeover(port)

        seen, give_back = self.spy_give_back()
        settled, settle = self.spy_settled()
        before = len(self.ui)
        with give_back, settle, \
                mock.patch.object(HostShellPort, "claim_manager", side_effect=RuntimeError("forced claim failure")), \
                mock.patch.object(HostShellPort, "restore_cwd", restore_cwd), \
                mock.patch.object(HostShellPort, "request_takeover", request_takeover):
            result = self.to_worker("printf PASS > outcome.txt")
            self.assertEqual(result["status"], "dispatched", result)
            outcome = self.finished()
        self.assertEqual(outcome["outcome"], "start_failed", self.flow.task_view())
        self.assertIn(b"wb-handoff", bytes(self.ui[before:]))
        self.assertEqual(len(settled), 1, "a typed wb-handoff is waited for (it may reach the control wait late)")
        self.assertEqual(seen[0]["after_failure"], True)
        self.assertEqual((seen[0]["hold_reason"], seen[0]["hold"]), (HOLD_REASON, HOLD_REASON),
                         "the start's own hold is still set when the give-back begins")
        self.assertIn(("takeover", HOLD_REASON), during, "the takeover happens under the start's hold")
        restores = [item for item in during if item[0] == "restore"]
        self.assertTrue(restores, "the user's directory is restored")
        self.assertEqual(restores[0][1:], (HOLD_REASON, HOLD_REASON, True),
                         "the cd back runs under the same hold; user input is refused meanwhile")
        self.assert_returned()
        self.assertNotIn(b"typed-during-restore", bytes(self.ui[before:]))
        self.assert_ports_unchanged()

    def test_a_failure_before_wb_handoff_does_not_wait_for_a_control_wait(self):
        seen, give_back = self.spy_give_back()
        settled, settle = self.spy_settled()
        before = len(self.ui)
        with give_back, settle, mock.patch.object(run_module, "_complete_line", lambda path: None):
            result = self.to_worker("printf PASS > outcome.txt")
            self.assertEqual(result["status"], "dispatched", result)
            outcome = self.finished()
        self.assertEqual(outcome["outcome"], "start_failed", self.flow.task_view())
        self.assertIn("cwd could not be verified", outcome.get("detail", ""))
        self.assertNotIn(b"wb-handoff", bytes(self.ui[before:]), "the start failed before its wb-handoff")
        self.assertEqual(settled, [], "no wait for a control wait that cannot come")
        self.assertEqual((seen[0]["after_failure"], seen[0]["hold_reason"], seen[0]["hold"]),
                         (False, HOLD_REASON, HOLD_REASON))
        self.assert_returned()  # the cd the start typed is undone: the user's directory is back
        self.assert_ports_unchanged()

    def test_a_successful_start_hands_the_port_back_unchanged(self):
        result = self.to_worker("printf 'PASS ok\\n'; printf PASS > outcome.txt")
        self.assertEqual(result["status"], "dispatched", result)
        self.assertEqual(self.finished().get("judgment"), "success", self.flow.task_view())
        self.assert_returned()
        self.assert_ports_unchanged()


for _name in [name for name in dir(_Base) if name.startswith("test_")]:
    setattr(_StartFailure, _name, None)  # the p27cd69c tests run in their own module


class BashStartFailureTests(_StartFailure):
    CHOICE = ShellChoice("bash", "/usr/bin/bash")


class DashStartFailureTests(_StartFailure):
    CHOICE = ShellChoice("sh", "/usr/bin/dash")


del _StartFailure, _Base

if __name__ == "__main__":
    unittest.main()
