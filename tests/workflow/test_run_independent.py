"""Independent CW-10 safety, authority, evidence and no-replay tests."""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.contracts.v1 import MessageKind
from workbench.ipc.bridge_g3.mailbox import MailboxStatus, TaskMailbox
from workbench.tasks.repository import AuthorizationError, TaskRepository
from workbench.terminal.shell_persistent.adapter import PersistentShell
from workbench.workflow import (TaskWorkflow, WorkflowHeld, WorktreePreparationError,
                                classify_worker_request, prepare_execution_worktree)


AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], text=True,
                            capture_output=True, timeout=5, check=False)
    if result.returncode:
        raise AssertionError((args, result.returncode, result.stderr))
    return result.stdout.strip()


class RecordingMailbox:
    """Public mailbox seam backed by real CW-09 facts, with controlled receipts."""
    def __init__(self, repository):
        self.repository = repository
        self.messages = []
        self.attempts = []
        self.status_by_kind = {}

    def create_message(self, task_id, revision, run_id, sender, target, kind, payload,
                       *, in_reply_to_message_id=None):
        message_id = str(uuid4())
        self.repository.create_message(task_id, revision, run_id,
                                       {"kind": kind.value, "sender": sender.value,
                                        "target": target.value, "reply_to": in_reply_to_message_id,
                                        "payload": payload}, message_id=message_id)
        message = SimpleNamespace(message_id=message_id, task_id=task_id, revision=revision,
                                  run_id=run_id, kind=kind, reply_to=in_reply_to_message_id)
        self.messages.append(message)
        return message

    def deliver(self, message):
        attempt = self.repository.create_delivery_attempt(message.message_id)
        self.attempts.append((message.message_id, attempt))
        status = self.status_by_kind.get(message.kind, MailboxStatus.OMP_PROCESSED)
        self.repository.record_delivery_status(attempt, "omp_processed" if status is MailboxStatus.OMP_PROCESSED
                                               else "unknown", {"source": "independent seam"})
        return SimpleNamespace(status=status, delivery_attempt_id=attempt)


class PublicRecordingMailbox(RecordingMailbox, TaskMailbox):
    """Public TaskMailbox type using only durable repository and receipt interfaces."""
    def deliver(self, message):
        receipt = super().deliver(message)
        receipt.session_id = "11111111-1111-4111-8111-111111111111"
        receipt.session_generation = 1
        return receipt


class IndependentWorkerPort:
    def __init__(self):
        self.armed = set()
        self.responses = []
        self.mutate = {}
        self.after = {}

    def arm(self, stage, message):
        self.armed.add((stage, message.message_id))

    def observe(self, stage, message, receipt):
        if (stage, message.message_id) not in self.armed:
            raise AssertionError("response cursor was not armed for this message")
        response = {
            "stage": stage, "task_id": message.task_id, "revision": message.revision,
            "run_id": message.run_id, "message_id": message.message_id,
            "delivery_attempt_id": receipt.delivery_attempt_id,
            "session_id": receipt.session_id,
            "session_generation": receipt.session_generation,
            "source": "omp_assistant_response", "response_id": str(uuid4()),
            "assistant_event_sequence": len(self.responses) + 1,
            "delivery_event_sequence": len(self.responses) + 2,
            "decision": "execute" if stage == "execute" else "success",
        }
        if stage in self.after:
            self.after[stage](message)
        if stage in self.mutate:
            response = self.mutate[stage](response)
        self.responses.append(response)
        return response


class WorkflowIndependentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cw10-independent-")
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
        (self.source / "source.txt").write_text("user dirty tracked\n")
        (self.source / "untracked.cfg").write_text("user untracked\n")
        self.status = git(self.source, "status", "--porcelain=v1", "--untracked-files=all")
        self.artifacts = self.root / "records"
        self.artifacts.mkdir()
        self.repo = TaskRepository(self.root / "metadata.sqlite3")
        self.addCleanup(self.repo.close)
        self.mailbox = RecordingMailbox(self.repo)
        self.workflow = TaskWorkflow(self.repo, self.mailbox)

    def execution(self, command="printf 'PASS\\n'; printf PASS > result.txt", *, shell="bash"):
        return {"source": str(self.source), "commit": self.commit, "command": command,
                "criteria": {"log_contains": "PASS", "result_file": "result.txt",
                             "result_contains": "PASS"},
                "environment": {"PATH": "/usr/bin:/bin", "TERM": "xterm"}, "shell": shell}

    def task(self, execution=None, *, approve=True, proceed=True, approval_execution=None):
        execution = execution or self.execution()
        task = self.repo.create_task({"goal": "bounded CW10", "execution": execution})
        if approve:
            self.repo.approve_scope(task, 1, {"execution": approval_execution or execution,
                                              "paths": ["result.txt"]})
        if proceed:
            self.repo.proceed(task, 1, "start")
        return task

    def start(self, task, label, **kwargs):
        run = self.workflow.start(task, 1, worktree_path=self.root / label,
                                  artifacts_root=self.artifacts, automation=AUTOMATION, **kwargs)
        self.addCleanup(run.close)
        return run

    def strict_workflow(self):
        mailbox = PublicRecordingMailbox(self.repo)
        port = IndependentWorkerPort()
        state = json.loads(json.dumps(AUTOMATION))
        workflow = TaskWorkflow(self.repo, mailbox, worker_port=port,
                                automation_source=lambda: state)
        return workflow, mailbox, port, state

    def strict_execution(self, command="printf 'PASS\\n'; printf PASS > result.txt"):
        execution = self.execution(command)
        execution["environment"] = ["PATH", "TERM", "CW10_SECRET"]
        return execution

    @staticmethod
    def transient_environment(secret="transient-value"):
        return {"PATH": "/usr/bin:/bin", "TERM": "xterm", "CW10_SECRET": secret}

    def test_explicit_commit_and_canonical_target_preserve_dirty_user_work(self):
        before_head = git(self.source, "rev-parse", "HEAD")
        tree_oid = git(self.source, "rev-parse", "HEAD^{tree}")
        existing = self.root / "existing"
        existing.mkdir()
        (existing / "user.txt").write_text("keep")
        symlink = self.root / "target-link"
        symlink.symlink_to(existing, target_is_directory=True)
        alias = self.root / "alias-to-development"
        alias.symlink_to(self.source, target_is_directory=True)
        rejected = (("HEAD", self.root / "bad-ref"),
                    (tree_oid, self.root / "bad-tree"),
                    ("f" * 40, self.root / "bad-oid"),
                    (self.commit, existing), (self.commit, symlink),
                    (self.commit, self.source / "nested"),
                    (self.commit, alias / "nested"))
        for commit, target in rejected:
            with self.subTest(commit=commit, target=target):
                with self.assertRaises(WorktreePreparationError):
                    prepare_execution_worktree(self.source, commit, target)
                self.assertEqual(git(self.source, "status", "--porcelain=v1", "--untracked-files=all"), self.status)
        self.assertEqual((existing / "user.txt").read_text(), "keep")
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), before_head)
        target = self.root / "execution"
        calls = []
        original_run = subprocess.run
        def observe_git(argv, **kwargs):
            calls.append(tuple(argv))
            return original_run(argv, **kwargs)
        with patch("workbench.workflow.worktree.subprocess.run", side_effect=observe_git):
            prepared = prepare_execution_worktree(self.source, self.commit, target)
        self.assertEqual(sum("worktree" in argv and "add" in argv for argv in calls), 1)
        self.assertFalse(any(any(verb in argv for verb in
                                 ("commit", "push", "merge", "stash", "reset", "clean"))
                             for argv in calls), calls)
        self.assertEqual(prepared.path, target)
        self.assertEqual(git(target, "rev-parse", "HEAD"), self.commit)
        self.assertEqual(git(target, "status", "--porcelain=v1"), "")
        self.assertEqual((target / "source.txt").read_text(), "committed\n")
        self.assertEqual((self.source / "source.txt").read_text(), "user dirty tracked\n")
        self.assertEqual((self.source / "untracked.cfg").read_text(), "user untracked\n")
        self.assertEqual(prepared.source_status_before, self.status)
        self.assertEqual(prepared.source_status_after, self.status)

    def test_scope_authority_and_direct_request_classification_cannot_start_run(self):
        execution = self.execution()
        task = self.task(execution, approve=False, proceed=False)
        with self.assertRaises(AuthorizationError):
            self.workflow.start(task, 1, worktree_path=self.root / "unauthorized",
                                artifacts_root=self.artifacts, automation=AUTOMATION)
        self.repo.approve_scope(task, 1, {"execution": self.execution("printf changed"),
                                          "paths": ["result.txt"]})
        self.repo.proceed(task, 1, "scope mismatch")
        with self.assertRaises(AuthorizationError):
            self.workflow.start(task, 1, worktree_path=self.root / "wrong-scope",
                                artifacts_root=self.artifacts, automation=AUTOMATION)
        self.assertFalse(self.mailbox.messages)
        self.assertIsNone(self.repo.get_current_run(task))
        self.assertFalse((self.root / "wrong-scope").exists())
        self.assertEqual(classify_worker_request({"paths": ["src/allowed.py"]}, ["src/"]),
                         "existing_task_scope")
        for path in ("src/../outside.py", "/outside.py", "src\\outside.py"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    classify_worker_request({"paths": [path]}, ["src/"])
        self.assertEqual(classify_worker_request({"paths": ["outside.py"]}, ["src/"]),
                         "scope_expansion_manager_confirmation")
        self.assertEqual(classify_worker_request({"paths": ["tiny.py"], "goal": "bounded",
                                                  "bounded_small_spec": True}, None),
                         "bounded_new_spec_requires_manager_approval")

    def test_task_delivery_unknown_is_durable_and_never_starts_host_or_replays(self):
        task = self.task()
        self.mailbox.status_by_kind[MessageKind.TASK] = MailboxStatus.UNKNOWN
        with self.assertRaises(WorkflowHeld):
            self.workflow.start(task, 1, worktree_path=self.root / "no-host",
                                artifacts_root=self.artifacts, automation=AUTOMATION)
        self.assertFalse((self.root / "no-host").exists())
        self.assertEqual(len(self.mailbox.messages), 1)
        self.assertEqual(len(self.mailbox.attempts), 1)
        message_id, attempt_id = self.mailbox.attempts[0]
        self.assertEqual([e["status"] for e in self.repo.get_delivery_history(attempt_id)],
                         ["attempted", "unknown"])
        self.assertEqual(self.repo.get_message(message_id)["task_id"], task)
        record = next(self.artifacts.glob("*/run.json"))
        self.assertEqual(json.loads(record.read_text())["worker_request"],
                         {"message_id": message_id, "status": "unknown"})
        self.assertIsNone(self.repo.get_current_run(task))

    def test_preparation_failure_reports_to_manager_without_source_fallback(self):
        task = self.task()
        target = self.root / "user-owned-target"
        target.mkdir()
        (target / "notes.txt").write_text("must survive")
        with self.assertRaises(WorktreePreparationError):
            self.workflow.start(task, 1, worktree_path=target,
                                artifacts_root=self.artifacts, automation=AUTOMATION)
        self.assertEqual((target / "notes.txt").read_text(), "must survive")
        self.assertEqual((self.source / "untracked.cfg").read_text(), "user untracked\n")
        self.assertEqual(git(self.source, "status", "--porcelain=v1", "--untracked-files=all"), self.status)
        self.assertEqual([message.kind for message in self.mailbox.messages],
                         [MessageKind.TASK, MessageKind.REPORT])
        self.assertEqual(self.mailbox.messages[-1].reply_to, self.mailbox.messages[0].message_id)
        report = self.repo.get_message(self.mailbox.messages[-1].message_id)
        self.assertEqual(report["content"]["payload"]["stage"], "preparation")
        self.assertTrue(report["content"]["payload"]["requires_manager_resolution"])
        self.assertIsNone(self.repo.get_current_run(task))
        self.assertFalse(any(event["kind"] == "sent" for event in self.repo.get_shell_history(
            self.mailbox.messages[0].run_id)))

    def test_exit_zero_without_result_provenance_is_indeterminate_not_success(self):
        command = "printf 'PASS\\n'"
        # A committed fixture result is copied into the detached worktree;
        # the experiment emits valid log text but never produces new result bytes.
        (self.source / "result.txt").write_text("PASS")
        git(self.source, "add", "result.txt")
        git(self.source, "commit", "-qm", "fixture result")
        latest = git(self.source, "rev-parse", "HEAD")
        revised = self.execution(command)
        revised["commit"] = latest
        task = self.task(revised)
        run = self.start(task, "preexisting-result")
        collected = run.collect(timeout=8)
        self.assertTrue(collected["exit_confirmed"])
        self.assertEqual(collected["exit_status"], 0)
        judged = run.judge()
        evidence = judged["worker_judgment"]
        self.assertEqual(evidence["judgment"], "indeterminate")
        self.assertIn("result_provenance_unconfirmed", evidence["reasons"])
        self.assertEqual(evidence["result_sha256"], sha256(b"PASS").hexdigest())
        self.assertEqual(evidence["run_id"], run.run_id)
        self.assertEqual(git(self.source, "status", "--porcelain=v1", "--untracked-files=all"), self.status)

    def test_pause_collects_terminal_facts_but_blocks_report_and_code_change_does_not_replay(self):
        task = self.task(self.execution("sleep 0.1; printf 'PASS\\n'; printf PASS > result.txt"))
        run = self.start(task, "paused")
        with self.assertRaises(WorkflowHeld):
            run.judge()
        collected = run.collect(timeout=8, paused=True)
        self.assertTrue(collected["exit_confirmed"])
        self.assertEqual(run.judge(paused=True)["status"], "deferred_paused")
        self.assertEqual(len(self.mailbox.messages), 1)
        self.assertEqual([event["kind"] for event in self.repo.get_shell_history(run.run_id)],
                         ["sent", "accepted", "started", "ended"])
        state = run.judge(requires_code_change=True, code_change_reason="specific result requires source edit")
        self.assertTrue(state["instruction_ended"])
        self.assertEqual(self.mailbox.messages[-1].reply_to, run.task_message_id)
        self.assertEqual(len(self.mailbox.messages), 2)
        with self.assertRaises(WorkflowHeld):
            run.judge()
        self.assertEqual(len(self.mailbox.attempts), 2)
        self.assertIsNone(self.repo.get_current_run(task))

    def test_missing_raw_log_after_confirmed_exit_is_indeterminate(self):
        task = self.task()
        run = self.start(task, "missing-raw-log")
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        run.raw_log.unlink()  # Only this temporary fixture artifact is removed.
        self.assert_indeterminate_report(run, "raw_log", "FileNotFoundError",
                                         "insufficient_raw_log")

    def assert_indeterminate_report(self, run, source, error, reason):
        prior_messages = len(self.mailbox.messages)
        prior_attempts = len(self.mailbox.attempts)
        state = run.judge()
        evidence = state["worker_judgment"]
        self.assertEqual(evidence["judgment"], "indeterminate")
        self.assertEqual(evidence["exit_status"], 0)
        self.assertTrue(evidence["exit_confirmed"])
        self.assertIn(reason, evidence["reasons"])
        errors = [item for item in evidence["evidence_errors"]
                  if item["source"] == source and item["error"] == error]
        self.assertEqual(len(errors), 1, evidence)
        self.assertTrue(Path(errors[0]["path"]).is_absolute())
        self.assertTrue(any(errors[0]["path"] in unknown and error in unknown
                            for unknown in evidence["unknowns"]))
        self.assertEqual(len(self.mailbox.messages), prior_messages + 1)
        self.assertEqual(len(self.mailbox.attempts), prior_attempts + 1)
        report = self.mailbox.messages[-1]
        self.assertEqual(report.kind, MessageKind.REPORT)
        self.assertEqual(report.reply_to, run.task_message_id)
        self.assertEqual(state["report"]["message_id"], report.message_id)
        self.assertEqual(state["report"]["status"], "omp_processed")
        reopened = TaskRepository(self.root / "metadata.sqlite3")
        try:
            saved = reopened.get_message(report.message_id)
            self.assertEqual(saved["content"]["payload"], evidence)
            self.assertEqual([item["status"] for item in reopened.get_delivery_history(
                self.mailbox.attempts[-1][1])], ["attempted", "omp_processed"])
        finally:
            reopened.close()
        self.assertEqual(json.loads(run.result_path.read_text())["report"], state["report"])
        with self.assertRaises(WorkflowHeld):
            run.judge()
        self.assertEqual(len(self.mailbox.messages), prior_messages + 1)
        self.assertEqual(len(self.mailbox.attempts), prior_attempts + 1)

    def test_unreadable_raw_log_after_collection_is_reported_without_replay(self):
        task = self.task()
        run = self.start(task, "unreadable-raw-log")
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        os.chmod(run.raw_log, 0)
        try:
            self.assert_indeterminate_report(run, "raw_log", "PermissionError",
                                             "insufficient_raw_log")
        finally:
            os.chmod(run.raw_log, 0o600)

    def test_truncated_and_changed_raw_log_cannot_be_promoted_to_success(self):
        for label, replacement in (("truncated", b""), ("changed", b"PASS\nchanged\n")):
            with self.subTest(label=label):
                task = self.task()
                run = self.start(task, label + "-raw-log")
                collected = run.collect(timeout=8)
                self.assertTrue(collected["exit_confirmed"])
                self.assertNotEqual(sha256(replacement).hexdigest(),
                                    collected["raw_log_collected"]["sha256"])
                run.raw_log.write_bytes(replacement)
                self.assert_indeterminate_report(run, "raw_log", "ChangedSinceCollection",
                                                 "insufficient_raw_log")

    def test_missing_and_changed_result_file_cannot_be_promoted_to_success(self):
        for label in ("missing", "changed"):
            with self.subTest(label=label):
                task = self.task()
                run = self.start(task, label + "-result")
                collected = run.collect(timeout=8)
                self.assertTrue(collected["exit_confirmed"])
                result_file = run.worktree.path / "result.txt"
                if label == "missing":
                    result_file.unlink()
                    error = "FileNotFoundError"
                else:
                    result_file.write_text("PASS\nchanged\n")
                    self.assertNotEqual(sha256(result_file.read_bytes()).hexdigest(),
                                        collected["result_collected"]["sha256"])
                    error = "ChangedSinceCollection"
                self.assert_indeterminate_report(run, "result_file", error,
                                                 "missing_or_empty_result_file")

    def test_shell_lifecycle_unknown_does_not_start_worker_judgment_or_report(self):
        task = self.task(self.execution("sleep 1; printf 'PASS\\n'; printf PASS > result.txt"))
        run = self.start(task, "lifecycle-unknown")
        unknown = run.shell.snapshot()
        unknown["phase"] = "unknown"
        unknown["lifecycle"]["unknown"] = ["control_observation_lost"]
        with patch.object(run.shell, "poll", return_value=unknown):
            observed = run.collect(timeout=.2)
        self.assertEqual(observed["shell_state"], "unknown")
        self.assertFalse(observed["exit_confirmed"])
        self.assertIn("control_observation_lost", observed["unknowns"])
        with self.assertRaises(WorkflowHeld):
            run.judge()
        self.assertEqual(len(self.mailbox.messages), 1)
        self.assertEqual(json.loads(run.result_path.read_text())["shell_state"], "unknown")

    def test_report_delivery_unknown_is_durable_and_cannot_auto_replay(self):
        task = self.task()
        run = self.start(task, "report-unknown")
        run.collect(timeout=8)
        self.mailbox.status_by_kind[MessageKind.REPORT] = MailboxStatus.UNKNOWN
        state = run.judge()
        self.assertEqual(state["report"]["status"], "unknown")
        self.assertEqual(len(self.mailbox.attempts), 2)
        _, attempt = self.mailbox.attempts[-1]
        self.assertEqual([e["status"] for e in self.repo.get_delivery_history(attempt)],
                         ["attempted", "unknown"])
        self.assertEqual(self.mailbox.messages[-1].reply_to, run.task_message_id)
        with self.assertRaises(WorkflowHeld):
            run.judge()
        self.assertEqual(len(self.mailbox.attempts), 2)
        self.assertEqual(json.loads(run.result_path.read_text())["report"]["status"], "unknown")

    def test_public_worker_response_identity_matrix_blocks_shell_dispatch(self):
        corruptions = {
            "task": lambda value: {**value, "task_id": str(uuid4())},
            "revision": lambda value: {**value, "revision": 2},
            "revision_boolean": lambda value: {**value, "revision": True},
            "run": lambda value: {**value, "run_id": str(uuid4())},
            "message": lambda value: {**value, "message_id": str(uuid4())},
            "attempt": lambda value: {**value, "delivery_attempt_id": str(uuid4())},
            "session": lambda value: {**value, "session_id": str(uuid4())},
            "generation": lambda value: {**value, "session_generation": 2},
            "generation_boolean": lambda value: {**value, "session_generation": True},
            "response_id": lambda value: {**value, "response_id": "not-a-uuid"},
            "event_order": lambda value: {**value, "delivery_event_sequence": value["assistant_event_sequence"]},
            "processed_only": lambda value: {},
            "missing_assistant": lambda value: None,
        }
        for label, corrupt in corruptions.items():
            with self.subTest(label=label):
                workflow, mailbox, port, _state = self.strict_workflow()
                port.mutate["execute"] = corrupt
                task = self.task(self.strict_execution())
                target = self.root / ("identity-" + label)
                try:
                    unexpected = workflow.start(task, 1, worktree_path=target,
                                                artifacts_root=self.artifacts, automation=AUTOMATION,
                                                environment_values=self.transient_environment())
                except (ValueError, WorkflowHeld):
                    pass
                else:
                    unexpected.close()
                    self.fail("untrusted worker response authorized host shell dispatch")
                self.assertFalse(target.exists())
                self.assertEqual([message.kind for message in mailbox.messages], [MessageKind.TASK])
                self.assertFalse(any(item["kind"] == "sent" for item in
                                     self.repo.get_shell_history(mailbox.messages[0].run_id)))
                self.assertIsNone(self.repo.get_current_run(task))

    def test_stale_worker_response_cannot_authorize_a_different_run(self):
        workflow, mailbox, port, _state = self.strict_workflow()
        first = self.task(self.strict_execution())
        run = workflow.start(first, 1, worktree_path=self.root / "first-response",
                             artifacts_root=self.artifacts, automation=AUTOMATION,
                             environment_values=self.transient_environment())
        self.addCleanup(run.close)
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        self.assertEqual(run.judge()["worker_judgment"]["judgment"], "success")
        old_response = port.responses[0]
        second = self.task(self.strict_execution())
        before = len(mailbox.messages)
        port.mutate["execute"] = lambda _value: old_response
        target = self.root / "stale-response"
        with self.assertRaises(ValueError):
            workflow.start(second, 1, worktree_path=target,
                           artifacts_root=self.artifacts, automation=AUTOMATION,
                           environment_values=self.transient_environment())
        self.assertFalse(target.exists())
        self.assertEqual([item.kind for item in mailbox.messages[before:]], [MessageKind.TASK])
        self.assertFalse(any(item["kind"] == "sent" for item in
                             self.repo.get_shell_history(mailbox.messages[-1].run_id)))

    def test_analysis_response_identity_and_judgment_guard_block_report(self):
        cases = (
            ("wrong-attempt", "printf 'PASS\\n'; printf PASS > result.txt",
             lambda value: {**value, "delivery_attempt_id": str(uuid4())}),
            ("missing-assistant", "printf 'PASS\\n'; printf PASS > result.txt",
             lambda _value: None),
            ("promoted-failure", "printf 'WRONG\\n'; printf BAD > result.txt",
             lambda value: value),
        )
        for label, command, corrupt in cases:
            with self.subTest(label=label):
                workflow, mailbox, port, _state = self.strict_workflow()
                port.mutate["analysis"] = corrupt
                task = self.task(self.strict_execution(command))
                run = workflow.start(task, 1, worktree_path=self.root / ("analysis-" + label),
                                     artifacts_root=self.artifacts, automation=AUTOMATION,
                                     environment_values=self.transient_environment())
                self.addCleanup(run.close)
                self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
                with self.assertRaises((ValueError, WorkflowHeld)):
                    run.judge()
                self.assertEqual([item.kind for item in mailbox.messages],
                                 [MessageKind.TASK, MessageKind.QUESTION])
                self.assertNotIn("report", json.loads(run.result_path.read_text()))

    def test_authority_changes_during_worktree_preparation_hold_new_dispatch(self):
        original_prepare = prepare_execution_worktree
        for action in ("pause", "cancel", "revoke", "approval_replacement"):
            with self.subTest(action=action):
                workflow, mailbox, _port, state = self.strict_workflow()
                execution = self.strict_execution()
                task = self.task(execution)
                def interrupt(*args, **kwargs):
                    prepared = original_prepare(*args, **kwargs)
                    run_id = mailbox.messages[0].run_id
                    if action == "pause":
                        state["payload"]["paused"] = True
                    elif action == "cancel":
                        self.repo.cancel_run(run_id, "independent interleaving")
                    elif action == "revoke":
                        self.repo.revoke_authority(task, 1, "independent interleaving")
                    else:
                        self.repo.approve_scope(task, 1, {"execution": execution,
                                                          "paths": ["result.txt"]})
                    return prepared
                target = self.root / ("prep-" + action)
                with patch("workbench.workflow.run.prepare_execution_worktree", side_effect=interrupt):
                    with self.assertRaises(WorkflowHeld):
                        workflow.start(task, 1, worktree_path=target,
                                       artifacts_root=self.artifacts, automation=AUTOMATION,
                                       environment_values=self.transient_environment())
                self.assertTrue(target.is_dir())
                self.assertFalse(any(item["kind"] == "sent" for item in
                                     self.repo.get_shell_history(mailbox.messages[0].run_id)))
                self.assertNotIn(MessageKind.REPORT, [item.kind for item in mailbox.messages])

    def test_pause_immediately_before_shell_submit_and_after_analysis_blocks_new_actions(self):
        workflow, mailbox, _port, state = self.strict_workflow()
        task = self.task(self.strict_execution())
        original_claim = PersistentShell.claim_manager
        def pause_after_claim(shell):
            answer = original_claim(shell)
            state["payload"]["paused"] = True
            return answer
        with patch.object(PersistentShell, "claim_manager", pause_after_claim):
            with self.assertRaises(WorkflowHeld):
                workflow.start(task, 1, worktree_path=self.root / "before-submit-paused",
                               artifacts_root=self.artifacts, automation=AUTOMATION,
                               environment_values=self.transient_environment())
        self.assertTrue((self.root / "before-submit-paused").is_dir())
        self.assertFalse(any(item["kind"] == "sent" for item in
                             self.repo.get_shell_history(mailbox.messages[0].run_id)))
        self.assertNotIn(MessageKind.REPORT, [item.kind for item in mailbox.messages])

        workflow, mailbox, port, state = self.strict_workflow()
        task = self.task(self.strict_execution())
        run = workflow.start(task, 1, worktree_path=self.root / "after-analysis-paused",
                             artifacts_root=self.artifacts, automation=AUTOMATION,
                             environment_values=self.transient_environment())
        self.addCleanup(run.close)
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        port.after["analysis"] = lambda _message: state["payload"].update(paused=True)
        with self.assertRaises(WorkflowHeld):
            run.judge()
        self.assertEqual([item.kind for item in mailbox.messages],
                         [MessageKind.TASK, MessageKind.QUESTION])
        self.assertNotIn("report", json.loads(run.result_path.read_text()))

    def test_direct_worker_routing_is_durable_but_never_self_authorizes(self):
        workflow, mailbox, _port, _state = self.strict_workflow()
        original = self.task(self.strict_execution())
        routes = [
            workflow.route_worker_request({"paths": ["result.txt"]}, task_id=original, revision=1),
            workflow.route_worker_request({"paths": ["small.txt"], "goal": "bounded change",
                                           "bounded_small_spec": True,
                                           "spec": {"goal": "bounded change",
                                                    "execution": self.strict_execution()}}),
            workflow.route_worker_request({"paths": ["outside.txt"], "goal": "expanded change"},
                                          task_id=original, revision=1),
        ]
        self.assertEqual([route["classification"] for route in routes],
                         ["existing_task_scope", "bounded_new_spec_requires_manager_approval",
                          "scope_expansion_manager_confirmation"])
        self.assertEqual([route["revision"] for route in routes], [2, 1, 3])
        self.assertEqual(routes[0]["task_id"], routes[2]["task_id"])
        self.assertNotEqual(routes[1]["task_id"], original)
        reopened = TaskRepository(self.root / "metadata.sqlite3")
        try:
            for route, expected_path in zip(routes, ("result.txt", "small.txt", "outside.txt")):
                self.assertFalse(route["dispatch_authorized"])
                self.assertIsNone(route["approval_decision_id"])
                spec = reopened.get_task_spec(route["task_id"], route["revision"])["spec"]
                self.assertEqual(spec["worker_request"]["paths"], [expected_path])
                self.assertEqual(spec["worker_request"]["classification"], route["classification"])
                with self.assertRaises(AuthorizationError):
                    workflow.start(route["task_id"], route["revision"],
                                   worktree_path=self.root / ("unauthorized-" + expected_path),
                                   artifacts_root=self.artifacts, automation=AUTOMATION,
                                   environment_values=self.transient_environment())
        finally:
            reopened.close()
        self.assertFalse(mailbox.messages)

    def test_transient_secret_reaches_shell_but_never_durable_evidence(self):
        sentinel = "CW10_PRIVATE_" + str(uuid4())
        digest = sha256(sentinel.encode()).hexdigest()
        command = ("test \"$(printf %s \"$CW10_SECRET\" | sha256sum | cut -d ' ' -f1)\" = "
                   + digest + " && printf 'PASS\\n' && printf PASS > result.txt")
        workflow, mailbox, _port, _state = self.strict_workflow()
        task = self.task(self.strict_execution(command))
        run = workflow.start(task, 1, worktree_path=self.root / "secret-execution",
                             artifacts_root=self.artifacts, automation=AUTOMATION,
                             environment_values=self.transient_environment(sentinel))
        self.addCleanup(run.close)
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        self.assertEqual(run.judge()["worker_judgment"]["judgment"], "success")
        self.assertEqual([item.kind for item in mailbox.messages],
                         [MessageKind.TASK, MessageKind.QUESTION, MessageKind.REPORT])
        self.assertEqual(self.repo.get_task_spec(task, 1)["spec"]["execution"]["environment"],
                         ["PATH", "TERM", "CW10_SECRET"])
        needle = sentinel.encode()
        persisted = [path for path in self.root.rglob("*") if path.is_file()]
        self.assertFalse([str(path) for path in persisted if needle in path.read_bytes()])


if __name__ == "__main__":
    unittest.main()
