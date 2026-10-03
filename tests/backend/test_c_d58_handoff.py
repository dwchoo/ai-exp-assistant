"""C-D58 (CW-18 U4) through the backend: a held wb-handoff never strands the user.

Real Bash host shell under the production Backend (fake OMP panes, isolation
check recorded as ok). While the handoff is held the backend keeps refusing
manager return and the HostShellPort hold, but the user's takeover request and
confirmation return the parent prompt, manual input is accepted again, and this
works for every held episode, including after a run given back by U2's port.
After the user cleans the jobs a fresh wb-handoff is accepted again.
"""
from pathlib import Path
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))

from test_restart_pane import FAKE_OMP, OK  # noqa: E402
from test_shell_kill_restart import Owned  # noqa: E402

from workbench.backend.launcher import LaunchPlan  # noqa: E402
from workbench.backend.paths import DataLayout, ensure_private_dir  # noqa: E402
from workbench.backend.service import Backend  # noqa: E402
from workbench.backend.ui_server import Held  # noqa: E402
from workbench.contracts.ui_v1 import Reason  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState  # noqa: E402

BASH = ShellChoice("bash", "/usr/bin/bash")


def ports(state):
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": state["parent_pid"], "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "e" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


class HeldHandoffBackendTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw18-u4-be-", dir="/tmp")
        self.addCleanup(self._dir.cleanup)
        self.root = root = Path(self._dir.name)
        project, home = root / "p", root / "h"
        project.mkdir()
        home.mkdir()
        fake = root / "omp"
        fake.write_text(FAKE_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
               "FAKE_PANE_RECORD": str(root / "panes.jsonl")}
        plan = LaunchPlan(BASH, str(fake), "omp/18.4.4", "/x/bridge.ts", ())
        patcher = mock.patch("workbench.backend.service.check_isolation",
                             lambda command, **kw: {"role": kw["role"], **OK})
        patcher.start()
        self.addCleanup(patcher.stop)
        layout = DataLayout(root / "d")
        ensure_private_dir(layout.root)
        self.backend = Backend(layout, plan, project_dir=str(project), environment=env)
        self.owned = Owned(self)
        self.addCleanup(self.close)
        self.backend._open()
        self.wait(lambda: self.backend.phase == "ready", "both fake OMPs registered")
        self.wait(lambda: self.mode() == "manual_prompt", "first prompt")

    def close(self):
        try:
            self.backend._close()
        finally:
            self.owned.cleanup()

    def wait(self, predicate, what, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.backend._tick(0.02)
            if predicate():
                return
        self.fail(f"timed out waiting for {what}; shell={self.backend.shell.shell_state()}")

    def mode(self):
        return self.backend.shell.state["parent_mode"]

    def held(self):
        return self.backend.shell.state["held_reasons"]

    def type(self, data):
        refused = self.backend.admit(PaneId.HOST_SHELL, data, "input")
        self.assertIsNone(refused, data)

    def assert_held_for_automation_but_recoverable(self, port):
        refused = self.backend.admit(PaneId.HOST_SHELL, b"x", "input")
        self.assertEqual(refused[0], Reason.INPUT_TARGET_UNKNOWN)
        self.assertIn("control wait", refused[1])
        with self.assertRaises(Held) as held:
            self.backend.handoff()
        self.assertEqual(held.exception.reason, Reason.HANDOFF_HELD)
        self.assertIsNotNone(port.hold("run"))  # nothing held while busy
        self.assertIsNone(self.backend.shell.automation_hold)
        shell = self.backend.takeover_request()
        self.assertEqual((shell["input_owner"], shell["takeover_requested"]), ("user", True))
        self.wait(lambda: self.mode() == "manual_prompt", "the parent prompt returned to the user")
        confirmed = self.backend.takeover_confirm()
        self.assertEqual((confirmed["input_owner"], confirmed["parent_mode"]), ("user", "manual_prompt"))

    def job(self, name):
        path = self.root / name
        self.type(f"sleep 61{len(name)}0 & echo $! > {path}\r".encode())
        self.wait(lambda: path.exists() and path.read_text().strip().isdecimal(), f"pid file {name}")
        self.wait(lambda: self.mode() == "manual_prompt", "prompt after the job")
        return self.owned.adopt(int(path.read_text()), f"61{len(name)}0")

    def test_job_held_handoff_is_recoverable_every_episode(self):
        port = self.backend._host_shell_port()
        self.addCleanup(port.detach)
        for name in ("bg1", "bg22"):
            pid = self.job(name)
            self.type(b"wb-handoff\r")
            self.wait(lambda: self.mode() == "control_wait" and "manual_jobs" in self.held(), "held handoff")
            self.assert_held_for_automation_but_recoverable(port)
            self.type(b"kill %1; wait\r")
            self.wait(lambda: not Path(f"/proc/{pid}").exists() and self.mode() == "manual_prompt", "cleaned up")
        # Root adjudication p27-cw18-needs-review: the fresh handoff probes no
        # jobs, so the job-caused latch clears and the manager may take over.
        self.type(b"wb-handoff\r")
        self.wait(lambda: self.mode() == "control_wait" and "manual_jobs" not in self.held(), "clean handoff")
        self.assertEqual(self.backend.handoff()["input_owner"], "manager")
        out = self.root / "ran"
        control, automation = ports(port.snapshot())
        port.submit(control, f"printf ok > {out}", automation)
        self.wait(lambda: self.backend.shell.state["lifecycle"]["input_barrier"], "run reached its barrier")
        port.release_input()
        self.wait(lambda: self.backend.shell.state["lifecycle"]["control_returned"], "run returned")
        self.assertEqual(out.read_text(), "ok")
        self.assertEqual(self.backend.shell.dropped_input_bytes, 0)

    def test_residue_hold_after_a_given_back_run_then_clean_handoff(self):
        port = self.backend._host_shell_port()
        self.addCleanup(port.detach)
        out = self.root / "ran"
        # U2 semantics unchanged: clean prompt, no jobs -> the port may hold;
        # while held the user's input is refused, and release gives it back.
        self.assertIsNone(port.hold("run"))
        self.assertEqual(self.backend.admit(PaneId.HOST_SHELL, b"y", "input")[0], Reason.HOST_SHELL_AUTOMATION)
        port.release_hold()
        self.type(b"wb-handoff\r")
        self.wait(lambda: self.mode() == "control_wait", "clean handoff")
        self.assertEqual(self.backend.handoff()["input_owner"], "manager")
        control, automation = ports(port.snapshot())
        port.submit(control, f"printf ok > {out}", automation)
        self.wait(lambda: self.backend.shell.state["lifecycle"]["input_barrier"], "run reached its barrier")
        port.release_input()
        self.wait(lambda: self.backend.shell.state["lifecycle"]["control_returned"], "run returned")
        port.request_takeover()  # U2 give-back
        self.wait(lambda: self.mode() == "manual_prompt", "shell given back")
        self.assertEqual(out.read_text(), "ok")
        self.type(b"wb-handoffx\x7f\r")  # erase key: unknown residue, not clean
        self.wait(lambda: self.mode() == "control_wait", "held handoff")
        self.assert_held_for_automation_but_recoverable(port)
        marker = self.root / "typed"
        self.type(f"printf ok > {marker}\r".encode())
        self.wait(lambda: marker.exists() and self.mode() == "manual_prompt"
                  and self.backend.shell.state["phase"] != "unknown", "manual input after the takeover")
        self.type(b"wb-handoff\r")
        self.wait(lambda: self.mode() == "control_wait", "clean handoff again")
        self.assertEqual(self.backend.handoff()["input_owner"], "manager")
        fresh, automation = ports(port.snapshot())
        port.submit(fresh, ":", automation)
        self.wait(lambda: self.backend.shell.state["lifecycle"]["input_barrier"], "second run barrier")
        port.release_input()
        self.wait(lambda: self.backend.shell.state["lifecycle"]["control_returned"], "second run returned")
        with self.assertRaises(UnsafeShellState):
            port.submit(fresh, ":", automation)  # never a replay
        self.assertEqual(self.backend.shell.dropped_input_bytes, 0)


if __name__ == "__main__":
    unittest.main()
