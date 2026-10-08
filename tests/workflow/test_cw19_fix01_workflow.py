"""p27-cw19-fix-01: an experiment start whose raw-log store fails is a recorded failure (review P3-3), and the
experiment report waits out a manager hold (review P3-2) instead of reaching a held manager."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from uuid import uuid4

from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.storage.log_raw.store import RawLogStore
from workbench.tasks.repository import TaskRepository
from workbench.workflow import TaskWorkflow, WorkflowHeld
from workbench.workflow.run import WorkflowRun

AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=10,
                          check=True).stdout.strip()


class StartRawStoreFailure(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cw19-fix01-wf-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        source = self.root / "source"
        source.mkdir()
        git(source, "init", "-q")
        git(source, "config", "user.email", "fixture@example.invalid")
        git(source, "config", "user.name", "fixture")
        (source / "tracked.txt").write_text("base\n")
        git(source, "add", "tracked.txt")
        git(source, "commit", "-qm", "base")
        self.source, self.commit = source, git(source, "rev-parse", "HEAD")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.repository = TaskRepository(self.root / "metadata.sqlite3")
        self.addCleanup(self.repository.close)

    def test_a_closed_raw_log_store_fails_the_run_truthfully(self):
        execution = {"source": str(self.source), "commit": self.commit, "command": "true",
                     "criteria": {"log_contains": "PASS", "result_file": "o.txt", "result_contains": "PASS"},
                     "environment": {"PATH": "/usr/bin:/bin"}, "shell": "bash"}
        task_id = self.repository.create_task({"goal": "g", "execution": execution})
        self.repository.approve_scope(task_id, 1, {"execution": execution, "paths": ["o.txt"]})
        self.repository.proceed(task_id, 1, "run")
        store = RawLogStore(self.root / "raw-logs")
        store.close()  # append now raises RuntimeError("RawLogStore is closed")
        mailbox = SimpleNamespace(create_message=lambda *a, **k: self.fail("nothing is sent"),
                                  deliver=lambda *a, **k: self.fail("nothing is sent"))
        workflow = TaskWorkflow(self.repository, mailbox, raw_store=store)
        with self.assertRaises(WorkflowHeld):
            workflow.start(task_id, 1, worktree_path=self.root / "wt", artifacts_root=self.artifacts,
                           automation=AUTOMATION)
        run_ids = [row["run_id"] for row in self.repository._connection.execute(  # read-only look at the fixture
            "SELECT run_id FROM runs WHERE task_id = ?", (task_id,))]
        self.assertEqual(len(run_ids), 1)
        self.assertIsNone(self.repository.get_current_run(task_id), "the started run is not left active")
        history = self.repository.get_run_history(run_ids[0])
        self.assertIn("artifact_setup", str(history))
        self.assertIn("RuntimeError", str(history), "the failure names the store error")


class JudgeReportHold(unittest.TestCase):
    """The report is created but not delivered while the manager is held; retry_report sends it once."""

    def run_object(self):
        run = object.__new__(WorkflowRun)
        self.delivered: list = []
        self.completed: list = []

        def deliver(message, **_options):
            self.delivered.append(message.message_id)
            return SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)
        run.task_id, run.revision, run.run_id, run.task_message_id = str(uuid4()), 1, str(uuid4()), str(uuid4())
        run.mailbox = SimpleNamespace(
            create_message=lambda *a, **k: SimpleNamespace(message_id=str(uuid4())), deliver=deliver)
        run.repository = SimpleNamespace(complete_run=lambda run_id, result: self.completed.append(run_id),
                                         fail_run=lambda run_id, result: self.completed.append(run_id))
        run._terminal, run._reported, run._run_closed = True, False, False
        run._record = {"shell_state": "exited"}
        run._persist = lambda: None
        run._analyse = lambda requires, reason, host: ({"judgment": "success"}, "success")
        return run

    def test_a_manager_hold_defers_the_report_until_it_lifts(self):
        run = self.run_object()
        events: list = []
        record = run.judge(on_report=lambda event, _record: events.append(event),
                           report_hold=lambda: "model_hold:manager")
        self.assertEqual(record["report"]["status"], MailboxStatus.DEFERRED.value)
        self.assertEqual(self.delivered, [], "nothing reaches a held manager")
        self.assertEqual(events, [], "no 'sending' while held")
        self.assertEqual(self.completed, [])
        record = run.retry_report(on_report=lambda event, _record: events.append(event))
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(record["report"]["status"], MailboxStatus.OMP_PROCESSED.value)
        self.assertNotIn("held", record["report"])
        self.assertEqual(events, ["sending"])

    def test_no_hold_delivers_at_once(self):
        run = self.run_object()
        record = run.judge(report_hold=lambda: None)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(record["report"]["status"], MailboxStatus.OMP_PROCESSED.value)

    def test_an_unreadable_hold_state_holds(self):
        run = self.run_object()

        def broken():
            raise RuntimeError("hold state unknown")
        record = run.judge(report_hold=broken)
        self.assertEqual(self.delivered, [])
        self.assertEqual(record["report"]["status"], MailboxStatus.DEFERRED.value)


if __name__ == "__main__":
    unittest.main()
