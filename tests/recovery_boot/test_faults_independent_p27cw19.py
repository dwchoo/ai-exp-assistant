"""Independent CW-19 checks (p27-cw19-test-01): model error, metadata failure, raw log caps, durable pause.

Derived from C-AC-17/24/28, C-D71 (4)/(5) and the root-accepted implementer decisions 3, 7 and 8:

- a model error holds only that role's automatic work; the next error-free assistant message of the same role
  lifts it; an aborted message neither holds nor lifts; Workbench sends no request to find out;
- a metadata write failure (unwritable data dir, journal) never stops the backend: it is a held state with the
  fault shown, and a later durable write reopens admission;
- an experiment run's raw log goes through the RawLogStore: 64 MiB per run, 512 MiB per project (defaults), the
  cap source and the missing bytes are recorded and the drain never raises (execution continues), also when the
  store itself fails;
- the user's pause survives a backend crash (durable), an unreadable pause file pauses (fail closed), and a pause
  that could not be stored is shown, not silently lost.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from workbench.app.lifecycle import LifecycleJournal
from workbench.app.recovery import METADATA_HOLD, PauseStore, model_hold
from workbench.backend.automation import AutomationController
from workbench.backend.flow import HandoffService
from workbench.backend.launcher import LaunchPlan
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.service import Backend
from workbench.contracts.v1 import ActorRole
from workbench.storage.log_raw import store as raw_store_module
from workbench.storage.log_raw.store import RawLogStore
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.workflow.run import WorkflowRun

BOOT_A = "aaaaaaaa-0000-4000-8000-0000000c0019"
MIB = 1024 * 1024


class Temp(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw19-indep-f-", dir="/tmp"))
        self.addCleanup(self._rm)
        self.layout = DataLayout(ensure_private_dir(self.root / "data"))
        ensure_private_dir(self.layout.workflow)

    def _rm(self):
        for path in (self.root, *self.root.rglob("*")):
            try:
                if path.is_dir() and not path.is_symlink():
                    os.chmod(path, 0o700)
            except OSError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def backend(self) -> Backend:
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/nonexistent/omp", "omp/0", "/x/bridge.ts", ())
        return Backend(self.layout, plan, project_dir=str(self.root), environment={"PATH": "/usr/bin:/bin"},
                       boot_source=lambda: BOOT_A)


class ModelHold(Temp):
    """C-D71 (4): passive per-role hold and lift."""

    def feed(self, backend, events):
        cursor = [0]

        def events_after(names, after):
            self.assertEqual(tuple(names), ("model_turn_result",))
            out = [e for e in events if e["bridgeSequence"] > after]
            cursor[0] = max([after] + [e["bridgeSequence"] for e in events])
            return cursor[0], out
        backend.bridge = SimpleNamespace(events_after=events_after)
        backend._poll_model_events()

    @staticmethod
    def event(seq, role, ok, reason="stop"):
        return {"name": "model_turn_result", "role": role, "ok": ok, "stopReason": reason, "bridgeSequence": seq}

    def test_error_holds_only_that_role_and_next_ok_lifts(self):
        backend = self.backend()
        events = [self.event(1, "worker", False, "error")]
        self.feed(backend, events)
        self.assertEqual(backend._hold_reason(ActorRole.WORKER), model_hold("worker"))
        self.assertIsNone(backend._hold_reason(ActorRole.MANAGER), "a worker model error must not hold the manager")
        faults = backend._faults_view()["model"]
        self.assertEqual(faults["worker"]["state"], "error")
        events.append(self.event(2, "manager", True))  # another role's success does not lift it
        self.feed(backend, events)
        self.assertEqual(backend._hold_reason(ActorRole.WORKER), model_hold("worker"))
        events.append(self.event(3, "worker", True, "toolUse"))
        self.feed(backend, events)
        self.assertIsNone(backend._hold_reason(ActorRole.WORKER))
        self.assertEqual(backend._faults_view()["model"]["worker"]["state"], "ok")

    def test_aborted_or_malformed_events_neither_hold_nor_lift(self):
        backend = self.backend()
        events = [{"name": "model_turn_result", "role": "worker", "stopReason": "aborted", "bridgeSequence": 1},
                  {"name": "model_turn_result", "role": "worker", "ok": "false", "bridgeSequence": 2},
                  {"name": "model_turn_result", "role": "user", "ok": False, "bridgeSequence": 3}]
        self.feed(backend, events)
        self.assertIsNone(backend._hold_reason(ActorRole.WORKER))
        events.append(self.event(4, "worker", False, "error"))
        events.append({"name": "model_turn_result", "role": "worker", "stopReason": "aborted", "bridgeSequence": 5})
        self.feed(backend, events)
        self.assertEqual(backend._hold_reason(ActorRole.WORKER), model_hold("worker"))

    def test_error_then_ok_in_one_poll_ends_lifted_and_events_are_read_once(self):
        backend = self.backend()
        events = [self.event(1, "manager", False, "error"), self.event(2, "manager", True)]
        self.feed(backend, events)
        self.assertIsNone(backend._hold_reason(ActorRole.MANAGER))
        events.append(self.event(3, "manager", False, "error_message"))
        self.feed(backend, events)
        self.assertEqual(backend._hold_reason(ActorRole.MANAGER), model_hold("manager"))
        self.feed(backend, events)  # nothing new: the state stays
        self.assertEqual(backend._hold_reason(ActorRole.MANAGER), model_hold("manager"))

    def test_bridge_failure_while_polling_changes_nothing(self):
        backend = self.backend()
        backend.bridge = SimpleNamespace(events_after=mock.Mock(side_effect=OSError("closed")))
        backend._poll_model_events()
        self.assertEqual(backend.hold.reasons(), [])


class MetadataFailSoft(Temp):
    """C-AC-28 / R3: an unwritable data dir is a held state, never a backend stop."""

    def test_record_write_failure_latches_the_hold_and_a_later_write_reopens(self):
        backend = self.backend()
        backend._record = lambda: {"phase": "running", "boot_id": BOOT_A}  # the record's content is not under test
        self.assertTrue(backend._write_record())
        os.chmod(self.layout.root, 0o500)
        try:
            if os.access(self.layout.root, os.W_OK):
                self.skipTest("running with a capability that ignores directory permissions")
            self.assertFalse(backend._write_record())  # must not raise
        finally:
            os.chmod(self.layout.root, 0o700)
        self.assertEqual(backend._hold_reason(ActorRole.WORKER), METADATA_HOLD)
        view = backend._faults_view()
        self.assertEqual(view["metadata"]["state"], "failed")
        self.assertIn("backend_record", view["metadata"]["sources"])
        self.assertTrue(view["record_error"])
        self.assertFalse(backend._automation_port()["payload"]["metadataHealthy"])
        self.assertTrue(backend._write_record())
        self.assertIsNone(backend._hold_reason(ActorRole.WORKER))
        self.assertIsNone(backend._faults_view()["metadata"])
        self.assertTrue(backend._automation_port()["payload"]["metadataHealthy"])

    def test_handoff_journal_failure_holds_and_reports(self):
        events: list = []
        service = HandoffService(self.root / "handoffs.jsonl", mailbox=object(),
                                 metadata=lambda source, error: events.append((source, error)))
        with mock.patch.object(service._journal, "append", side_effect=OSError(28, "No space left")):
            result = service.handle(ActorRole.MANAGER, {
                "tool_call_id": "c1", "request_id": "r1", "session_id": "0f0e0d0c-0b0a-4908-8706-050403020100",
                "generation": 1, "tool": "to_worker", "args": {"kind": "work", "message": "x"}})
        self.assertEqual(result, {"status": "rejected", "reason": "journal_unavailable"})
        self.assertEqual([source for source, _error in events], ["handoff_journal"])

    def test_metadata_event_success_clears_every_source(self):
        backend = self.backend()
        backend._metadata_event("task_metadata", OSError("x"))
        backend._metadata_event("flow_ledger", OSError("y"))
        self.assertEqual(set(backend._faults_view()["metadata"]["sources"]), {"task_metadata", "flow_ledger"})
        backend._metadata_event("handoff_journal", None)
        self.assertIsNone(backend._hold_reason(ActorRole.WORKER))


class Shell:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def display_bytes(self):
        return self.chunks.pop(0) if self.chunks else b""


def bare_run(store, run_id="run-0001", shell=None, raw_log=None):
    run = WorkflowRun.__new__(WorkflowRun)
    run.run_id = run_id
    run.raw_store = store
    run._record = {}
    run._raw_missing = 0
    run.shell = shell
    run.raw_log = raw_log or Path("/nonexistent/raw.log")
    return run


class RawLogCaps(Temp):
    """C-D71 (5) / C-AC-24: run raw logs under the run and project caps; execution and collection continue."""

    def test_backend_defaults_are_64_and_512_mib(self):
        self.assertEqual(raw_store_module.DEFAULT_PER_RUN_LIMIT, 64 * MIB)
        self.assertEqual(raw_store_module.DEFAULT_PROJECT_LIMIT, 512 * MIB)
        store = RawLogStore(self.root / "raw")
        self.addCleanup(store.close)
        self.assertEqual((store.per_run_limit, store.project_limit), (64 * MIB, 512 * MIB))

    def test_run_cap_64_mib_drain_never_raises(self):
        store = RawLogStore(self.root / "raw")
        self.addCleanup(store.close)
        chunk = b"y" * (4 * MIB)
        run = bare_run(store, shell=Shell([chunk] * 17))  # 68 MiB
        for _ in range(17):
            run._drain()
        status = run._record["raw_log_status"]
        self.assertEqual(status["stored_bytes"], 64 * MIB)
        self.assertEqual(status["observed_bytes"], 68 * MIB)
        self.assertTrue(status["truncated"])
        self.assertEqual(status["cap_source"], "run")
        self.assertEqual(os.path.getsize(Path(store.root_path) / "run-0001.log"), 64 * MIB)
        run.shell.chunks.append(b"after the cap\n")
        run._drain()  # still no exception
        self.assertEqual(run._record["raw_log_status"]["stored_bytes"], 64 * MIB)

    def test_project_cap_is_reported_as_project(self):
        store = RawLogStore(self.root / "raw", per_run_limit=4 * MIB, project_limit=6 * MIB)
        self.addCleanup(store.close)
        first = bare_run(store, "run-0001", Shell([b"a" * (4 * MIB)]))
        first._drain()
        second = bare_run(store, "run-0002", Shell([b"b" * (3 * MIB)]))
        second._drain()
        status = second._record["raw_log_status"]
        self.assertEqual(status["stored_bytes"], 2 * MIB)
        self.assertEqual(status["cap_source"], "project")
        self.assertTrue(status["truncated"])

    def test_store_failure_records_missing_bytes_and_continues(self):
        failing = SimpleNamespace(append=mock.Mock(side_effect=RuntimeError("RawLogStore is closed")))
        run = bare_run(failing, shell=Shell([b"x" * 100, b"y" * 50]))
        run._drain()
        run._drain()
        status = run._record["raw_log_status"]
        self.assertEqual(status["missing_bytes"], 150)
        self.assertEqual(status["storage_error"], "RuntimeError")

    def test_backend_raw_log_view_texts(self):
        backend = self.backend()
        for status, needle in (({"truncated": True, "cap_source": "run", "missing_bytes": 5}, "64 MiB"),
                               ({"truncated": True, "cap_source": "project", "missing_bytes": 5}, "512 MiB"),
                               ({"storage_error": "OSError", "missing_bytes": 7}, "누락 7")):
            with self.subTest(status=status):
                backend.automation_loop = SimpleNamespace(raw_log_status=lambda s=status: {"run_id": "r", **s})
                view = backend._raw_log_view()
                self.assertIn(needle, view["text"])
                self.assertIn("실행 계속", view["text"])
        backend.automation_loop = SimpleNamespace(raw_log_status=lambda: {"run_id": "r", "truncated": False,
                                                                          "missing_bytes": 0})
        self.assertIsNone(backend._raw_log_view())


class DurablePause(Temp):
    """R4: the user's pause survives a backend crash; unreadable state pauses; a lost pause is shown."""

    def controller(self, store, log=None):
        controller = AutomationController(
            bridge=None, database=self.layout.tasks, journal=LifecycleJournal(self.layout.lifecycle_journal),
            raw=None, shell_pane=lambda: None, project_dir=self.root, artifacts_root=ensure_private_dir(
                self.layout.workflow / "runs"), boot_marker=lambda: BOOT_A, pause_store=store,
            log=log or (lambda _m: None))
        self.addCleanup(lambda: _close(controller))
        return controller

    def test_pause_survives_a_new_controller(self):
        store = PauseStore(self.layout.root / "automation.json")
        first = self.controller(store)
        self.assertFalse(first.paused())
        first.request_pause()
        self.assertTrue(json.loads((self.layout.root / "automation.json").read_text())["paused"])
        second = self.controller(PauseStore(self.layout.root / "automation.json"))
        self.assertTrue(second.paused(), "a backend crash lifted the user's pause")
        self.assertIn("kept across the backend restart", json.dumps(second.status()))

    def test_unreadable_pause_file_pauses(self):
        path = self.layout.root / "automation.json"
        for content in ("{bad", json.dumps({"paused": "yes"}), json.dumps([1])):
            with self.subTest(content=content):
                path.write_text(content)
                os.chmod(path, 0o600)
                self.assertTrue(PauseStore(path).load())
        path.unlink()
        self.assertFalse(PauseStore(path).load())

    def test_pause_that_cannot_be_stored_is_shown(self):
        """A pause the backend could not make durable would be lost by a crash; the user must see that."""
        logs: list[str] = []
        store = SimpleNamespace(load=lambda: False, save=lambda paused: False)
        controller = self.controller(store, log=logs.append)
        status = controller.request_pause()
        self.assertTrue(controller.paused())
        shown = bool(status.get("persistence_error")) or any(
            word in str(status.get("detail")) for word in ("not stored", "저장", "durab"))
        self.assertTrue(shown, f"the failed pause store is only logged ({logs}), not shown in the automation "
                               f"state (persistence_error={status.get('persistence_error')!r})")


def _close(controller):
    try:
        controller.close()
    except Exception:
        pass


if __name__ == "__main__":
    unittest.main()
