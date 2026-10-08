"""CW-19 units: outbox under the admission hold, durable pause, the experiment raw log through RawLogStore,
TaskFlow's start hold and the CLI confirm-boot."""
from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import support  # noqa: F401  (sys.path)

from workbench.app.lifecycle import LifecycleJournal
from workbench.app.recovery import BOOT_HOLD, PauseStore
from workbench.backend import cli
from workbench.backend.automation import AutomationController
from workbench.backend.flow import HandoffService, OutboundMessage
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, G3BridgeServer, MailboxStatus
from workbench.storage.log_raw.store import RawLogStore
from workbench.workflow.run import WorkflowRun


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw19-f-", dir="/tmp"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)


class _Mailbox:
    def __init__(self):
        self.delivered = []

    def create_message(self, task_id, revision, run_id, sender_role, target_role, kind, payload, *,
                       in_reply_to_message_id=None):
        return SimpleNamespace(message_id=str(uuid4()), session_id="s", session_generation=1, task_id=task_id,
                               target_role=ActorRole(target_role))

    def deliver(self, message, *, timeout=20):
        self.delivered.append(message.message_id)
        return DeliveryReceipt(message.message_id, None, message.target_role, "s", 1, MailboxStatus.API_RETURNED,
                               {})


class OutboxHoldTests(TempRoot):
    def test_a_kept_message_waits_and_a_plain_one_is_held_never_sent(self):
        reason = {"value": "model_hold:worker"}
        mailbox = _Mailbox()
        service = HandoffService(self.root / "handoffs.jsonl", mailbox=mailbox, retry_interval=0.05,
                                 hold=lambda role: reason["value"] if role is ActorRole.WORKER else None)
        service.start()
        self.addCleanup(service.close)
        message = OutboundMessage(str(uuid4()), 1, str(uuid4()), ActorRole.MANAGER, ActorRole.WORKER,
                                  MessageKind.TASK, {"x": 1})
        kept = service.enqueue(message, origin="test-kept", keep_across_pause=True)
        plain = service.enqueue(message, origin="test-plain")
        self.assertEqual((kept["status"], plain["status"]), ("queued", "queued"))
        time.sleep(0.5)
        self.assertEqual(mailbox.delivered, [], "nothing reaches a held worker")
        reason["value"] = None
        deadline = time.monotonic() + 3
        while not mailbox.delivered and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)
        self.assertEqual(len(mailbox.delivered), 1, "only the kept one, once (never submitted, no replay)")

    def test_journal_failure_is_reported_as_metadata(self):
        events = []
        service = HandoffService(self.root / "handoffs.jsonl", mailbox=_Mailbox(),
                                 metadata=lambda source, error: events.append((source, type(error).__name__)))
        self.addCleanup(service.close)
        with mock.patch.object(service._journal, "append", side_effect=OSError("disk")):
            result = service.handle("manager", {"request_id": "r", "tool_call_id": "c", "tool": "to_worker",
                                                "session_id": str(uuid4()), "generation": 1, "args": {
                                                    "kind": "work", "message": "m",
                                                    "spec": {"goal": "g", "paths": ["a/"]}}})
        self.assertEqual(result["reason"], "journal_unavailable")
        self.assertEqual(events, [("handoff_journal", "OSError")])


class DurablePauseTests(TempRoot):
    def controller(self, hold=lambda: None):
        bridge = G3BridgeServer(self.root / f"b{uuid4().hex[:6]}.sock", {"manager": "m", "worker": "w"})
        self.addCleanup(bridge.close)
        controller = AutomationController(
            bridge=bridge, database=self.root / "tasks.sqlite3", journal=LifecycleJournal(self.root / "lc.json"),
            raw=None, shell_pane=lambda: None, project_dir=self.root, artifacts_root=self.root,
            boot_marker=lambda: "boot", pause_store=PauseStore(self.root / "automation.json"), hold=hold)
        self.addCleanup(controller.close)
        return controller

    def test_the_pause_survives_a_restart_until_a_reconciled_resume(self):
        first = self.controller()
        self.assertFalse(first.paused())
        first.request_pause()
        first.wait_idle(5)
        second = self.controller()  # a crash: nothing but the store carries the pause
        self.assertTrue(second.paused())
        self.assertIn("kept across the backend restart", second.status()["detail"])
        second.request_resume()
        second.wait_idle(5)
        self.assertFalse(second.paused())
        self.assertFalse(self.controller().paused())

    def test_the_hold_holds_the_review_tick(self):
        controller = self.controller(hold=lambda: BOOT_HOLD)
        self.assertEqual(controller.tick()["outcome"], "idle")  # no run: nothing to hold
        controller._bound = SimpleNamespace(coordinator=object(), run_id="r", kind="experiment")
        result = controller.tick()
        self.assertEqual((result["outcome"], result["problems"]), ("held", [BOOT_HOLD]))
        controller._bound = None


class _Shell:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def display_bytes(self):
        return self.chunks.pop(0) if self.chunks else b""


class RawLogStoreRunTests(TempRoot):
    def run_with(self, store, chunks):
        run_id = str(uuid4())
        record_dir = self.root / run_id
        record_dir.mkdir()
        run = WorkflowRun(None, None, str(uuid4()), 1, run_id, "m", SimpleNamespace(path=self.root, commit="c"),
                          _Shell(chunks), {"criteria": {}}, record_dir,
                          Path(store.root_path) / f"{run_id}.log" if store else record_dir / "raw.log",
                          record_dir / "run.json", None, "a" * 64, {}, set(), lambda: {}, None, raw_store=store)
        return run

    def test_run_cap_is_recorded_and_collection_continues(self):
        store = RawLogStore(self.root / "raw", per_run_limit=10, project_limit=100)
        self.addCleanup(store.close)
        run = self.run_with(store, [b"0123456", b"789abcdef"])
        run._drain()
        run._drain()
        status = run._record["raw_log_status"]
        self.assertEqual((status["stored_bytes"], status["cap_source"], status["truncated"]), (10, "run", True))
        self.assertEqual(run._read_raw(), b"0123456789")

    def test_project_cap(self):
        store = RawLogStore(self.root / "raw", per_run_limit=10, project_limit=15)
        self.addCleanup(store.close)
        first = self.run_with(store, [b"x" * 10])
        first._drain()
        second = self.run_with(store, [b"y" * 10])
        second._drain()
        self.assertEqual(second._record["raw_log_status"]["cap_source"], "project")
        self.assertEqual(second._record["raw_log_status"]["stored_bytes"], 5)

    def test_store_failure_marks_missing_bytes_and_never_raises(self):
        store = RawLogStore(self.root / "raw", per_run_limit=10, project_limit=100)
        self.addCleanup(store.close)
        run = self.run_with(store, [b"abc", b"def"])
        with mock.patch.object(store, "append", side_effect=OSError("disk")):
            run._drain()
        run._drain()
        status = run._record["raw_log_status"]
        self.assertEqual(status["missing_bytes"], 3)
        self.assertEqual(run._read_raw(), b"def")

    def test_an_empty_store_log_reads_as_empty(self):
        store = RawLogStore(self.root / "raw")
        self.addCleanup(store.close)
        self.assertEqual(self.run_with(store, [])._read_raw(), b"")

    def test_plain_file_failure_never_raises(self):
        run = self.run_with(None, [b"abc"])
        run.raw_log = self.root / "missing-dir" / "raw.log"
        run._drain()
        self.assertEqual(run._record["raw_log_status"]["missing_bytes"], 3)


class ConfirmBootCliTests(unittest.TestCase):
    SNAPSHOT = {"boot": {"boot_id": "b2", "recorded_boot_id": "b1", "confirmation_required": True,
                         "reason": "reboot"},
                "backend": {"data_dir": "/x", "project_dir": "/p", "omp_version": "omp/18.2.10"},
                "panes": {"host_shell": {"shell": {"kind": "bash", "executable": "/usr/bin/bash"}}},
                "omp_isolation": {"state": "ok"}, "automation": {"state": "paused", "paused": True},
                "task": {"kind": "experiment", "task_id": "t", "status": "held", "held_reason": "backend_restarted"},
                "startup": {"classification": "reboot", "run": {"run_id": "r", "state": "interrupted_by_reboot"},
                            "survivors": []}, "holds": [{"reason": "boot_confirmation_required"}]}

    def run_cli(self, *args, snapshot=None, answer=None, tty=False):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.request.return_value = answer or {"ok": True, "boot": {"confirmed": True}}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_running_snapshot", return_value=snapshot), \
                mock.patch.object(cli, "UiClient", return_value=client), \
                mock.patch.object(cli.os.path, "lexists", return_value=True), \
                mock.patch.object(cli, "ensure_private_dir"), \
                mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: tty)), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = cli.main(["confirm-boot", "--data-dir", "/tmp/cw19-cli-none", *args])
        return code, out.getvalue(), err.getvalue(), client

    def test_shows_the_conditions_and_needs_explicit_confirmation(self):
        code, out, err, client = self.run_cli(snapshot=self.SNAPSHOT)
        self.assertEqual(code, 1)
        self.assertIn("기록 b1 -> 현재 b2", out)
        self.assertIn("interrupted_by_reboot", out)
        self.assertIn("--yes", err)
        client.request.assert_not_called()
        code, out, _, client = self.run_cli("--yes", snapshot=self.SNAPSHOT)
        self.assertEqual(code, 0)
        self.assertEqual(client.request.call_args.kwargs, {"boot_id": "b2"})
        self.assertIn("부팅 확인됨", out)

    def test_refusals(self):
        self.assertEqual(self.run_cli("--yes", snapshot=None)[0], 3)
        not_pending = {**self.SNAPSHOT, "boot": {"boot_id": "b2", "confirmation_required": False}}
        self.assertEqual(self.run_cli("--yes", snapshot=not_pending)[0], 1)
        unknown = {**self.SNAPSHOT, "boot": {"boot_id": None, "confirmation_required": True}}
        code, _, err, client = self.run_cli("--yes", snapshot=unknown)
        self.assertEqual(code, 1)
        client.request.assert_not_called()
        code, _, err, _ = self.run_cli("--yes", snapshot=self.SNAPSHOT,
                                       answer={"ok": False, "reason": "boot_id_mismatch", "detail": "x"})
        self.assertEqual(code, 1)
        self.assertIn("boot_id_mismatch", err)


if __name__ == "__main__":
    unittest.main()
