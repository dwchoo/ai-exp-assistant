"""Independent p2.7 deadline-fix verification through the production workflow path.

TaskWorkflow.start calls PersistentShell.submit with default arguments; the
collect() timeout is a collection cycle, not an experiment bound.
Contracts: BRIEF "실험 실행 시간 상한 없음", C-AC-07 (경과 시간만으로 종료하지 않음),
OPERATING-CONTRACT §3 (대기 간격은 실행 시간 상한이 아님; input-return barrier),
C-AC-34 / §3 stop and real-exit confirmation, no replay (C-AC-08).
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4

from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.tasks.repository import TaskRepository
from workbench.workflow import TaskWorkflow

AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], text=True,
                            capture_output=True, timeout=5, check=False)
    if result.returncode:
        raise AssertionError((args, result.returncode, result.stderr))
    return result.stdout.strip()


def stat(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def alive(pid, start):
    fields = stat(pid)
    return fields is not None and fields[19] == start and fields[0] not in {"Z", "X"}


def tree(root):
    parents, starts = {}, {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            fields = stat(int(name))
            if fields and fields[0] not in {"Z", "X"}:
                parents.setdefault(int(fields[1]), []).append(int(name))
                starts[int(name)] = fields[19]
    found, queue = {}, [root]
    while queue:
        pid = queue.pop()
        if pid in starts and pid not in found:
            found[pid] = starts[pid]
            queue.extend(parents.get(pid, []))
    return found


def session_members(session):
    found = {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            fields = stat(int(name))
            if fields and int(fields[3]) == session and fields[0] not in {"Z", "X"}:
                found[int(name)] = fields[19]
    return found


def kill_exact(pid, start):
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if alive(pid, start):
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        os.close(descriptor)


class RecordingMailbox:
    """Durable repository-backed mailbox seam with processed receipts (no OMP/network)."""
    def __init__(self, repository):
        self.repository = repository

    def create_message(self, task_id, revision, run_id, sender, target, kind, payload,
                       *, in_reply_to_message_id=None):
        message_id = str(uuid4())
        self.repository.create_message(task_id, revision, run_id,
                                       {"kind": kind.value, "sender": sender.value,
                                        "target": target.value, "reply_to": in_reply_to_message_id,
                                        "payload": payload}, message_id=message_id)
        return SimpleNamespace(message_id=message_id, task_id=task_id, revision=revision,
                               run_id=run_id, kind=kind, reply_to=in_reply_to_message_id)

    def deliver(self, message):
        attempt = self.repository.create_delivery_attempt(message.message_id)
        self.repository.record_delivery_status(attempt, "omp_processed", {"source": "p27b seam"})
        return SimpleNamespace(status=MailboxStatus.OMP_PROCESSED, delivery_attempt_id=attempt)


class WorkflowUnboundedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="p27b-wf-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "development"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.name", "Independent Fixture")
        git(self.source, "config", "user.email", "fixture@example.invalid")
        (self.source / "source.txt").write_text("committed\n")
        git(self.source, "add", "source.txt")
        git(self.source, "commit", "-qm", "fixture commit")
        self.commit = git(self.source, "rev-parse", "HEAD")
        self.artifacts = self.root / "records"
        self.artifacts.mkdir()
        self.repo = TaskRepository(self.root / "metadata.sqlite3")
        self.addCleanup(self.repo.close)
        self.workflow = TaskWorkflow(self.repo, RecordingMailbox(self.repo))
        self.tracked = {}

    def start(self, command, shell, label):
        execution = {"source": str(self.source), "commit": self.commit, "command": command,
                     "criteria": {"log_contains": "PASS", "result_file": "result.txt",
                                  "result_contains": "PASS"},
                     "environment": {"PATH": "/usr/bin:/bin", "TERM": "xterm"}, "shell": shell}
        task = self.repo.create_task({"goal": "p27b unbounded", "execution": execution})
        self.repo.approve_scope(task, 1, {"execution": execution, "paths": ["result.txt"]})
        self.repo.proceed(task, 1, "start")
        run = self.workflow.start(task, 1, worktree_path=self.root / label,
                                  artifacts_root=self.artifacts, automation=AUTOMATION)
        transport = run.shell._transport
        self.addCleanup(self.close_verified, run, run.shell.parent_pid, Path(transport._init_dir.name))
        return run

    def close_verified(self, run, parent, init_dir):
        run.close()
        def leftovers():
            left = dict(session_members(parent))
            left.update({p: s for p, s in self.tracked.items() if alive(p, s)})
            return left
        deadline = time.monotonic() + 4
        while leftovers() and time.monotonic() < deadline:
            time.sleep(0.02)
        left = leftovers()
        for pid, start in left.items():
            kill_exact(pid, start)
        self.assertEqual(left, {}, "processes survived WorkflowRun.close")
        self.assertFalse(init_dir.exists())

    def track(self, run):
        supervisor = run.shell.snapshot()["lifecycle"]["supervisor_pid"]
        found = tree(supervisor) if supervisor else {}
        self.tracked.update(found)
        return found

    def test_default_start_runs_past_90s_and_confirms_real_exit_across_idle_collect_gap(self):
        runs = []
        for shell in ("bash", "sh"):
            for label, command, status in (
                    ("ok", "sleep 92; printf 'PASS\\n'; printf PASS > result.txt; exit 0", 0),
                    ("fail", "sleep 92; exit 5", 5)):
                runs.append((f"{shell}-{label}", self.start(command, shell, f"{shell}-{label}"), status))
        began = time.monotonic()
        mains = {}
        while time.monotonic() - began < 90:
            for name, run, _ in runs:
                record = run.collect(timeout=0.5)  # short production collect cycles
                elapsed = time.monotonic() - began
                self.assertEqual((record["shell_state"], record["exit_confirmed"]), ("running", False),
                                 f"{name} after {elapsed:.0f}s: {record}")
                life = run.shell.snapshot()["lifecycle"]
                self.assertEqual(life["unknown"], [], f"{name} after {elapsed:.0f}s")
                if life["experiment_started"]:
                    mains.setdefault(name, (life["child_pid"], stat(life["child_pid"])[19]))
                    self.assertTrue(alive(*mains[name]), f"{name} experiment killed after {elapsed:.0f}s")
                    self.track(run)
        self.assertEqual(set(mains), {name for name, *_ in runs})
        # No collect cycle while the runs end (~92 s) and sit at the input barrier.
        while time.monotonic() - began < 104:
            time.sleep(0.5)
        for name, run, status in runs:
            with self.subTest(run=name):
                record = run.collect(timeout=10)
                self.assertEqual((record["shell_state"], record["exit_status"], record["exit_confirmed"]),
                                 ("exited", status, True), record)
                self.assertNotIn("UNKNOWN:SUPERVISOR_FAILURE", run.shell._transport.events)
                life = run.shell.snapshot()["lifecycle"]
                self.assertEqual((life["lifetime"], life["input_returned"], life["control_returned"],
                                  life["unknown"]), ("ended", True, True, []))
                if status == 0:
                    self.assertEqual(record["result_collected"]["size"], 4)

    def test_close_of_unbounded_workflow_run_cleans_descendants_without_replay(self):
        for shell in ("bash", "sh"):
            with self.subTest(shell=shell):
                starts = self.root / f"starts-{shell}"
                command = (f"printf S >> {shlex.quote(str(starts))}; sleep 1000 & ( (sleep 1000) & ); "
                           "setsid sleep 1000 & exec sleep 1000")
                run = self.start(command, shell, f"close-{shell}")
                record = run.collect(timeout=6.5)  # beyond the former 5 s bound
                self.assertEqual((record["shell_state"], record["exit_confirmed"]), ("running", False))
                self.assertEqual(run.shell.snapshot()["lifecycle"]["unknown"], [])
                found = self.track(run)
                self.assertGreaterEqual(len(found), 5, found)  # supervisor + 4 sleeps
                run.close()
                deadline = time.monotonic() + 4
                while any(alive(p, s) for p, s in found.items()) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual({p: s for p, s in found.items() if alive(p, s)}, {})
                time.sleep(1.0)
                self.assertEqual(starts.read_text(), "S", "experiment replayed after stop")


if __name__ == "__main__":
    unittest.main()
