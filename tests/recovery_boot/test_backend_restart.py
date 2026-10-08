"""CW-19 on a real in-process backend with fake OMPs (real PTYs, real G3 bridge, real host shell; no model).

Covered: start-up order (reconcile before TaskFlow/UI/record overwrite), reboot -> admission hold until
confirm-boot (user actions still allowed), the backend_restarted notice (once; never while paused or before the
confirmation), survivors in status and stop_survivor, durable pause, fail-soft record writes (metadata hold) and
the passive model-error hold. The boot marker comes from an injected source.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from support import Owned, ref, ticks

from workbench.app.recovery import BOOT_HOLD, METADATA_HOLD
from workbench.backend import service as service_module
from workbench.backend.boot import BootStore
from workbench.backend.flow_tasks import FLOW_LEDGER_NAME, FlowTask
from workbench.backend.launcher import LaunchPlan
from workbench.backend.paths import DataLayout, ensure_private_dir, write_private_json
from workbench.backend.service import Backend
from workbench.backend.ui_server import Held
from workbench.contracts.ui_v1 import Reason
from workbench.contracts.v1 import ActorRole, PaneId
from workbench.terminal.shell_g2.prototype import ShellChoice

BOOT_A, BOOT_B = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
FAKE_OMP = Path(__file__).with_name("fake_omp.py")
OK = {"state": "ok", "ok": True, "leaks": [], "warnings": [], "error": None}


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] not in "ZX"
    except OSError:
        return False


class BackendFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw19-be-", dir="/tmp")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        project, home = self.root / "p", self.root / "h"
        project.mkdir()
        home.mkdir()
        fake = self.root / "omp"
        fake.write_text(f"#!{sys.executable}\n" + FAKE_OMP.read_text())
        fake.chmod(0o700)
        self.record, self.notices = self.root / "panes.jsonl", self.root / "notices.jsonl"
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
                    "FAKE_PANE_RECORD": str(self.record), "FAKE_NOTICES": str(self.notices)}
        self.plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), str(fake), "omp/18.6.1", "/x/bridge.ts", ())
        self.project = str(project)
        patcher = mock.patch("workbench.backend.service.check_isolation",
                             lambda command, **kw: {"role": kw["role"], **OK})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.layout = DataLayout(self.root / "d")
        ensure_private_dir(self.layout.root)
        ensure_private_dir(self.layout.workflow)
        self.boot = BOOT_A
        self.backend: Backend | None = None
        self.owned = Owned()
        self.addCleanup(self.owned.cleanup)
        self.addCleanup(self.close)

    # -- lifecycle -------------------------------------------------------------------------------
    def start(self):
        self.backend = Backend(self.layout, self.plan, project_dir=self.project, environment=self.env,
                               boot_source=lambda: self.boot)
        self.backend._reconcile_startup()
        self.backend._open()
        self.wait(lambda: self.backend.phase == "ready", "both fake OMPs registered")
        return self.backend

    def close(self):
        backend, self.backend = self.backend, None
        if backend is not None:
            backend._shutdown_confirmed = True
            backend._close()

    def wait(self, predicate, what, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.backend._tick(0.02)
            if predicate():
                return
        self.fail(f"timed out waiting for {what}; phase={self.backend.phase} reason={self.backend.reason}")

    def spin(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.backend._tick(0.02)

    def call(self, role, tool, args):
        peer = self.backend.bridge.peer(role)
        request = {"request_id": str(uuid4()), "tool_call_id": f"call-{uuid4()}", "tool": tool, "args": args,
                   "session_id": peer.session_id, "generation": peer.generation}
        box: dict = {}
        thread = threading.Thread(target=lambda: box.update(result=self.backend._tool_request(peer, request)),
                                  daemon=True)
        thread.start()
        self.wait(lambda: "result" in box, f"the {tool} result", timeout=15)
        thread.join(1)
        return box["result"]

    def received(self, kind=None):
        if not self.notices.exists():
            return []
        items = [json.loads(line) for line in self.notices.read_text().splitlines() if line.strip()]
        return [item for item in items if kind is None or item["notice"].get("type") == kind]

    # -- previous incarnation ---------------------------------------------------------------------
    def previous(self, *, boot=BOOT_A, phase="ready", processes=None, open_task=True, paused=False):
        BootStore(self.layout.root / "boot.json", boot_source=lambda: boot).begin(None)
        write_private_json(self.layout.record, {"boot_id": boot, "phase": phase, "processes": processes or {}})
        if open_task:
            task = FlowTask(str(uuid4()), "experiment", status="running", message="run the check", summary="check",
                            run_id=str(uuid4()), run_revision=1, task_message_id=str(uuid4()), runs_started=1,
                            spec={"goal": "g", "paths": ["out.txt"]})
            path = self.layout.workflow / FLOW_LEDGER_NAME
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "task", "task": asdict(task)}) + "\n")
            os.chmod(path, 0o600)
            self.task_id = task.task_id
        if paused:
            write_private_json(self.layout.root / "automation.json", {"version": 1, "paused": True})

    def new_task_args(self):
        return {"kind": "work", "message": "new work", "spec": {"goal": "g", "paths": ["notes/"]}}


class StartOrderTests(BackendFixture):
    def test_reconcile_reads_the_previous_record_before_anything_starts(self):
        self.previous(processes={})
        calls = []
        backend = Backend(self.layout, self.plan, project_dir=self.project, environment=self.env,
                          boot_source=lambda: self.boot)
        original = service_module.StartupReconciler.run

        def spy(reconciler):
            calls.append(("reconcile", json.loads(self.layout.record.read_text())["phase"],
                          backend.flow is None and backend.ui is None and not backend.panes))
            return original(reconciler)

        with mock.patch.object(service_module.StartupReconciler, "run", spy), \
                mock.patch.object(Backend, "_open", lambda self_: calls.append(("open",))), \
                mock.patch.object(Backend, "_loop", lambda self_: calls.append(("loop",))), \
                mock.patch.object(Backend, "_close", lambda self_: {"verified": True}):
            self.assertEqual(backend.run(), 0)
        self.assertEqual(calls, [("reconcile", "ready", True), ("open",), ("loop",)])


    def test_a_failing_reconcile_fails_closed_and_can_be_confirmed(self):
        backend = Backend(self.layout, self.plan, project_dir=self.project, environment=self.env,
                          boot_source=lambda: self.boot)
        with mock.patch.object(service_module.StartupReconciler, "run", side_effect=RuntimeError("boom")):
            backend._reconcile_startup()
        self.assertEqual(backend.hold.reason_for(None), BOOT_HOLD)
        self.assertEqual((backend.boot["reason"], backend.startup["classification"]),
                         ("reconcile_failed", "boot_unknown"))
        backend.confirm_boot(BOOT_A)
        self.assertIsNone(backend.hold.reason_for(None))


class RebootHoldTests(BackendFixture):
    def test_reboot_holds_automatic_work_until_confirm_boot(self):
        self.previous(boot=BOOT_A)
        self.boot = BOOT_B
        backend = self.start()
        snapshot = backend.snapshot()
        self.assertTrue(snapshot["boot"]["confirmation_required"])
        self.assertEqual(snapshot["startup"]["classification"], "reboot")
        self.assertIn(BOOT_HOLD, [item["reason"] for item in snapshot["holds"]])
        self.assertEqual(snapshot["task"]["held_reason"], "backend_restarted")
        # Workbench-originated automatic work is held ...
        result = self.call(ActorRole.MANAGER, "to_worker", self.new_task_args())
        self.assertEqual((result["status"], result.get("reason")), ("held", BOOT_HOLD), result)
        self.assertEqual(backend.flow.task_view()["task_id"], self.task_id, "no new Task")
        terminal = self.call(ActorRole.WORKER, "terminal", {"command": "echo not-run"})
        self.assertEqual((terminal["status"], terminal["reason"]), ("held", BOOT_HOLD), terminal)
        self.spin(1.5)
        self.assertEqual(self.received("backend_restarted"), [], "no notice before confirm-boot")
        # ... the user's own actions are not
        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"echo user-typed\n", "input"))
        self.assertTrue(backend.pause()["automation"]["paused"])
        self.assertFalse(backend.resume(True)["automation"]["state"] == "held")
        backend.automation_loop.wait_idle(5)
        cancel = self.call(ActorRole.MANAGER, "to_worker", {"kind": "experiment", "message": "cancel it",
                                                            "task_id": self.task_id, "cancel": True})
        self.assertNotEqual(cancel.get("reason"), BOOT_HOLD, cancel)
        # confirm-boot: exact current boot only, durable before the hold lifts
        with self.assertRaises(Held) as refused:
            backend.confirm_boot(BOOT_A)
        self.assertEqual(refused.exception.reason, Reason.BOOT_ID_MISMATCH)
        answer = backend.confirm_boot(BOOT_B)
        self.assertTrue(answer["boot"]["confirmed"])
        stored = json.loads((self.layout.root / "boot.json").read_text())
        self.assertEqual((stored["pending"], stored["confirmed_boot_id"]), (False, BOOT_B))
        self.assertIsNone(backend.hold.reason_for(ActorRole.WORKER))
        with self.assertRaises(Held) as again:
            backend.confirm_boot(BOOT_B)
        self.assertEqual(again.exception.reason, Reason.BOOT_CONFIRMATION_NOT_REQUIRED)

    def test_pending_confirmation_survives_a_restart(self):
        self.previous(boot=BOOT_A, open_task=False)
        self.boot = BOOT_B
        backend = self.start()
        self.assertTrue(backend.snapshot()["boot"]["confirmation_required"])
        self.close()  # the backend ends before the user confirmed
        backend = self.start()
        self.assertTrue(backend.snapshot()["boot"]["confirmation_required"])
        self.assertEqual(backend.hold.reason_for(None), BOOT_HOLD)


class RestartNoticeTests(BackendFixture):
    def test_notice_once_after_the_resume_with_survivors_and_status(self):
        survivor = self.owned.spawn("exec sleep 60")
        self.previous(processes={"host_shell": ref(survivor.pid, ticks(survivor.pid), "host_shell")}, paused=True)
        backend = self.start()
        snapshot = backend.snapshot()
        self.assertEqual(snapshot["startup"]["classification"], "same_boot_crash")
        self.assertTrue(snapshot["automation"]["paused"], "R4: the pause survives the restart")
        names = [(item["name"], item["pid"], item["stoppable"]) for item in snapshot["startup"]["survivors"]]
        self.assertEqual(names, [("host_shell", survivor.pid, True)])
        self.spin(2.5)
        self.assertEqual(self.received("backend_restarted"), [], "never while paused")
        backend.resume(True)
        backend.automation_loop.wait_idle(5)
        self.wait(lambda: self.received("backend_restarted"), "the backend_restarted notice")
        self.spin(2.5)
        notices = self.received("backend_restarted")
        self.assertEqual(len(notices), 1, "sent once")
        self.assertEqual(notices[0]["role"], "manager")
        fields = notices[0]["notice"]
        self.assertEqual(fields["task_id"], self.task_id)
        self.assertEqual(fields["classification"], "same_boot_crash")
        self.assertEqual([item["pid"] for item in fields["survivors"]], [survivor.pid])
        self.assertIsNone(survivor.poll(), "never signalled by Workbench itself")
        status = self.call(ActorRole.MANAGER, "workbench_status", {})
        self.assertEqual([item["survivor_id"] for item in status["backend"]["survivors"]], ["s1"])
        # stop_survivor: manager only, proven identity, recorded
        refused = self.call(ActorRole.WORKER, "stop_survivor", {"survivor_id": "s1", "reason": "x"})
        self.assertEqual(refused["status"], "rejected")
        invalid = self.call(ActorRole.MANAGER, "stop_survivor", {"survivor_id": "s1"})
        self.assertEqual(invalid["reason"], "invalid_arguments")
        self.assertIsNone(survivor.poll())
        stopped = self.call(ActorRole.MANAGER, "stop_survivor", {"survivor_id": "s1", "reason": "old shell"})
        self.assertEqual(stopped["status"], "stopped", stopped)
        survivor.wait(5)
        self.assertEqual(backend.survivor_stops[-1]["reason"], "old shell")
        record = json.loads(self.layout.record.read_text())
        self.assertEqual(record["survivor_stops"][-1]["outcome"], "stopped")
        self.assertEqual(record["survivors"], [])

    def test_no_notice_without_an_open_task(self):
        self.previous(open_task=False)
        backend = self.start()
        self.spin(2.0)
        self.assertEqual(self.received("backend_restarted"), [])
        self.assertIsNone(backend.snapshot()["startup"]["notice"])

    def test_shutdown_lists_and_never_verifies_a_live_survivor(self):
        survivor = self.owned.spawn("exec sleep 60")
        self.previous(processes={"worker_omp": ref(survivor.pid, ticks(survivor.pid), "worker")}, open_task=False)
        backend = self.start()
        active = backend.shutdown_request()["active"]
        self.assertIn(("previous_survivor", survivor.pid), [(item["kind"], item.get("pid")) for item in active])
        self.backend = None
        backend._shutdown_confirmed = True
        result = backend._close()
        self.assertFalse(result["verified"], "C-AC-22")
        self.assertIn("previous_backend_survivors_alive", result["problems"])
        self.assertIsNone(survivor.poll(), "not ended by the shutdown")


class FaultHoldTests(BackendFixture):
    def test_unwritable_record_is_a_metadata_hold_not_a_stop(self):
        backend = self.start()
        with mock.patch.object(service_module, "write_private_json", side_effect=OSError(30, "Read-only")):
            self.assertFalse(backend._write_record())
            backend._check_ready()  # loop paths that write the record do not raise
        self.assertEqual(backend.hold.reason_for(ActorRole.WORKER), METADATA_HOLD)
        self.assertIsNotNone(backend.snapshot()["faults"]["metadata"])
        result = self.call(ActorRole.MANAGER, "to_worker", self.new_task_args())
        self.assertEqual((result["status"], result.get("reason")), ("held", METADATA_HOLD))
        backend._metadata_retry_at = 0.0
        self.spin(0.2)  # the next durable write succeeds and reopens admission
        self.assertIsNone(backend.hold.reason_for(ActorRole.WORKER))
        self.assertIsNone(backend.snapshot()["faults"]["metadata"])

    def test_task_metadata_fault_holds_the_dispatch(self):
        backend = self.start()
        import sqlite3
        with mock.patch.object(backend.flow, "_to_worker", side_effect=sqlite3.OperationalError("readonly")):
            result = self.call(ActorRole.MANAGER, "to_worker", self.new_task_args())
        self.assertEqual((result["status"], result.get("reason")), ("held", METADATA_HOLD))
        self.assertEqual(backend.hold.reason_for(ActorRole.WORKER), METADATA_HOLD)

    def test_model_error_holds_that_role_until_its_next_clean_answer(self):
        backend = self.start()
        worker = backend.panes[PaneId.WORKER_OMP]
        worker.admit(b"model-error\n")
        self.wait(lambda: backend.hold.reason_for(ActorRole.WORKER) == "model_hold:worker", "the model hold")
        self.assertIsNone(backend.hold.reason_for(ActorRole.MANAGER), "the manager is not held")
        self.assertEqual(backend.snapshot()["faults"]["model"]["worker"]["state"], "error")
        result = self.call(ActorRole.MANAGER, "to_worker", self.new_task_args())
        self.assertEqual((result["status"], result.get("reason")), ("held", "model_hold:worker"))
        self.assertEqual(backend._notice(ActorRole.WORKER, {"notice_id": str(uuid4()), "type": "status_check"}),
                         "paused", "notices to the held role wait")
        worker.admit(b"model-aborted\n")  # an abort is not a recovery
        self.spin(0.5)
        self.assertEqual(backend.hold.reason_for(ActorRole.WORKER), "model_hold:worker")
        worker.admit(b"model-ok\n")
        self.wait(lambda: backend.hold.reason_for(ActorRole.WORKER) is None, "the passive recovery")
        self.assertTrue(backend.snapshot()["faults"]["model"]["worker"]["recovered"])


if __name__ == "__main__":
    unittest.main()
