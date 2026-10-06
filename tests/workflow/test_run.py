"""CW-10 vertical run checks with actual temporary Git and Bash/sh PTYs."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from uuid import uuid4

from workbench.ipc.bridge_g3.mailbox import MailboxStatus, TaskMailbox
from workbench.tasks.repository import AuthorizationError, TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell
from workbench.workflow import (
    TaskWorkflow, WorkflowHeld, WorktreePreparationError,
    classify_worker_request, prepare_execution_worktree, verified_worker_response,
)


AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True,
}}


def git(directory, *args):
    result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True,
                            text=True, timeout=10, check=True)
    return result.stdout.strip()


class PublicMailboxFixture:
    def __init__(self, repository):
        self.repository = repository
        self.messages = []

    def create_message(self, task_id, revision, run_id, sender, target, kind, payload,
                       *, in_reply_to_message_id=None):
        assert self.repository.get_run(run_id)["task_id"] == task_id
        message = SimpleNamespace(message_id=str(uuid4()), task_id=task_id, revision=revision,
                                  run_id=run_id, sender=sender, target=target, kind=kind,
                                  payload=payload, reply_to=in_reply_to_message_id)
        self.messages.append(message)
        return message

    def deliver(self, message):
        assert message in self.messages
        return SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)


class StrictMailboxFixture(TaskMailbox):
    """Public TaskMailbox type with fixture transport, for fail-closed port tests."""
    def __init__(self, repository):
        self.fixture = PublicMailboxFixture(repository)

    @property
    def messages(self):
        return self.fixture.messages

    def create_message(self, *args, **kwargs):
        return self.fixture.create_message(*args, **kwargs)

    def deliver(self, message):
        self.fixture.deliver(message)
        return SimpleNamespace(status=MailboxStatus.OMP_PROCESSED,
                               delivery_attempt_id=str(uuid4()),
                               session_id="11111111-1111-4111-8111-111111111111",
                               session_generation=1)


class WorkerPortFixture:
    def __init__(self, repository, automation_state):
        self.repository = repository
        self.automation_state = automation_state
        self.after_execute = None
        self.after_analysis = None
        self.mismatch = False
        self.armed = []

    def arm(self, stage, message):
        self.armed.append((stage, message.message_id))

    def observe(self, stage, message, receipt):
        assert (stage, message.message_id) in self.armed
        if stage == "execute" and self.after_execute is not None:
            self.after_execute(message)
        if stage == "analysis" and self.after_analysis is not None:
            self.after_analysis(message)
        return {"stage": stage, "task_id": message.task_id, "revision": message.revision,
                "run_id": message.run_id,
                "message_id": str(uuid4()) if self.mismatch else message.message_id,
                "delivery_attempt_id": receipt.delivery_attempt_id,
                "session_id": receipt.session_id, "session_generation": receipt.session_generation,
                "source": "omp_assistant_response", "response_id": str(uuid4()),
                "assistant_event_sequence": len(self.armed),
                "delivery_event_sequence": len(self.armed) + 1,
                "decision": "execute" if stage == "execute" else (
                    "indeterminate" if "preparation_error" in message.payload.get("facts", {}) else "success")}


class WorkflowRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cw10-workflow-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.email", "fixture@example.invalid")
        git(self.source, "config", "user.name", "CW10 Fixture")
        (self.source / "tracked.txt").write_text("base\n")
        git(self.source, "add", "tracked.txt")
        git(self.source, "commit", "-qm", "fixture baseline")
        self.commit = git(self.source, "rev-parse", "HEAD")
        (self.source / "tracked.txt").write_text("user dirty content\n")
        (self.source / "untracked.txt").write_text("preserve me\n")
        self.status = git(self.source, "status", "--porcelain=v1", "--untracked-files=all")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.repository = TaskRepository(self.root / "metadata.sqlite3")
        self.addCleanup(self.repository.close)
        self.mailbox = PublicMailboxFixture(self.repository)
        self.workflow = TaskWorkflow(self.repository, self.mailbox)

    def approved_task(self, command, *, shell="bash"):
        execution = {"source": str(self.source), "commit": self.commit,
                     "command": command, "criteria": {"log_contains": "PASS",
                     "result_file": "outcome.txt", "result_contains": "PASS"},
                     "environment": {"PATH": "/usr/bin:/bin", "TERM": "xterm"}, "shell": shell}
        task_id = self.repository.create_task({"goal": "bounded fixture", "execution": execution})
        self.repository.approve_scope(task_id, 1, {"execution": execution, "paths": ["outcome.txt"]})
        self.repository.proceed(task_id, 1, "run approved fixture")
        return task_id

    def strict_task(self, command="printf 'PASS\\n'; printf PASS > outcome.txt"):
        state = {"portVersion": 2, "kind": "AutomationState", "payload": dict(AUTOMATION["payload"])}
        mailbox = StrictMailboxFixture(self.repository)
        worker_port = WorkerPortFixture(self.repository, state)
        workflow = TaskWorkflow(self.repository, mailbox, worker_port=worker_port,
                                automation_source=lambda: state)
        execution = {"source": str(self.source), "commit": self.commit,
                     "command": command, "criteria": {"log_contains": "PASS",
                     "result_file": "outcome.txt", "result_contains": "PASS"},
                     "environment": ["PATH", "TERM", "CW10_SECRET"], "shell": "bash"}
        task_id = self.repository.create_task({"goal": "bounded strict fixture", "execution": execution})
        self.repository.approve_scope(task_id, 1, {"execution": execution, "paths": ["outcome.txt"]})
        self.repository.proceed(task_id, 1, "run approved strict fixture")
        return workflow, mailbox, worker_port, state, task_id

    def test_four_exit_and_evidence_outcomes_on_real_host_shell(self):
        cases = (
            ("success", "printf 'PASS\\n'; printf PASS > outcome.txt", "success", 0),
            ("nonzero", "printf 'ERROR\\n'; printf BAD > outcome.txt; exit 7", "failure", 7),
            ("criteria_fail", "printf 'WRONG\\n'; printf BAD > outcome.txt", "failure", 0),
            ("insufficient", "printf PASS > outcome.txt", "indeterminate", 0),
        )
        for name, command, expected, exit_status in cases:
            with self.subTest(case=name):
                task_id = self.approved_task(command, shell="sh" if name == "criteria_fail" else "bash")
                worktree_path = self.root / f"execution-{name}"
                run = self.workflow.start(task_id, 1, worktree_path=worktree_path,
                                          artifacts_root=self.artifacts, automation=AUTOMATION)
                try:
                    collected = run.collect(timeout=8)
                    self.assertTrue(collected["exit_confirmed"], collected)
                    self.assertEqual(collected["exit_status"], exit_status)
                    judged = run.judge()
                    self.assertEqual(judged["worker_judgment"]["judgment"], expected)
                    self.assertEqual(judged["report"]["status"], "omp_processed")
                    self.assertEqual(judged["worker_judgment"]["run_id"], run.run_id)
                    self.assertEqual(json.loads(run.result_path.read_text())["run_id"], run.run_id)
                    self.assertEqual(git(worktree_path, "rev-parse", "HEAD"), self.commit)
                    self.assertEqual(git(self.source, "status", "--porcelain=v1", "--untracked-files=all"), self.status)
                    self.assertEqual((self.source / "untracked.txt").read_text(), "preserve me\n")
                    self.assertEqual([e["kind"] for e in self.repository.get_shell_history(run.run_id)],
                                     ["sent", "accepted", "started", "ended"])
                    self.assertIsNone(self.repository.get_current_run(task_id))
                    self.assertTrue(run.raw_log.is_file())
                    self.assertTrue(worktree_path.is_dir(), "execution worktree is retained")
                finally:
                    run.close()

    def test_pause_collects_exit_without_new_report_then_resumes(self):
        task_id = self.approved_task("sleep 0.1; printf 'PASS\\n'; printf PASS > outcome.txt")
        run = self.workflow.start(task_id, 1, worktree_path=self.root / "paused-execution",
                                  artifacts_root=self.artifacts, automation=AUTOMATION)
        try:
            state = run.collect(timeout=8, paused=True)
            self.assertTrue(state["exit_confirmed"])
            self.assertEqual(run.judge(paused=True)["status"], "deferred_paused")
            self.assertEqual(len(self.mailbox.messages), 1)
            self.assertEqual(run.judge()["worker_judgment"]["judgment"], "success")
            self.assertEqual(len(self.mailbox.messages), 2)
        finally:
            run.close()

    def test_code_modification_report_ends_instruction_without_auto_return(self):
        task_id = self.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        run = self.workflow.start(task_id, 1, worktree_path=self.root / "modification-execution",
                                  artifacts_root=self.artifacts, automation=AUTOMATION)
        try:
            run.collect(timeout=8)
            state = run.judge(requires_code_change=True, code_change_reason="result shows a source fix is needed")
            self.assertTrue(state["instruction_ended"])
            self.assertIsNone(self.repository.get_current_run(task_id))
            self.assertEqual(len(self.mailbox.messages), 2)
            with self.assertRaises(WorkflowHeld):
                run.judge()
            self.assertEqual(len(self.mailbox.messages), 2)
        finally:
            run.close()

    def test_ambiguous_report_delivery_is_not_replayed(self):
        task_id = self.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        run = self.workflow.start(task_id, 1, worktree_path=self.root / "uncertain-delivery",
                                  artifacts_root=self.artifacts, automation=AUTOMATION)
        try:
            run.collect(timeout=8)
            original_deliver = self.mailbox.deliver

            def lost_receipt(message):
                original_deliver(message)
                raise RuntimeError("receipt lost after delivery")

            self.mailbox.deliver = lost_receipt
            with self.assertRaisesRegex(RuntimeError, "receipt lost"):
                run.judge()
            self.assertEqual(json.loads(run.result_path.read_text())["report"]["status"], "delivery_unknown")
            with self.assertRaises(WorkflowHeld):
                run.judge()
            self.assertEqual(len(self.mailbox.messages), 2)
        finally:
            run.close()

    # -- CW-18 U2 delta: injected host shell and the deferred-report resend -------------
    def names_task(self, command, *, shell="bash"):
        execution = {"source": str(self.source), "commit": self.commit, "command": command,
                     "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
                     "environment": ["PATH"], "shell": shell}
        task_id = self.repository.create_task({"goal": "injected shell", "execution": execution})
        self.repository.approve_scope(task_id, 1, {"execution": execution, "paths": ["outcome.txt"]})
        self.repository.proceed(task_id, 1, "run with the host shell")
        return task_id

    def test_injected_shell_runs_the_experiment_and_is_never_closed(self):
        host = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "HOME": str(self.root)},
                               choice=ShellChoice("bash", "/usr/bin/bash"))
        host.choice = ShellChoice("bash", "/usr/bin/bash")
        host.detach = lambda: setattr(host, "detached", True)
        self.addCleanup(host.close)
        task_id = self.names_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        run = self.workflow.start(task_id, 1, worktree_path=self.root / "injected", artifacts_root=self.artifacts,
                                  automation=AUTOMATION, shell=host)
        self.assertIs(run.shell, host)
        self.assertFalse(run.owns_shell)
        collected = run.collect(timeout=8)
        self.assertTrue(collected["exit_confirmed"], collected)
        judged = run.judge()
        self.assertEqual(judged["worker_judgment"]["judgment"], "success")
        record = json.loads(run.result_path.read_text())
        self.assertEqual((record["shell_source"], record["parent_pid"], record["environment_names"]),
                         ("injected_host_shell", host.parent_pid, ["PATH"]))
        run.close()
        self.assertTrue(getattr(host, "detached", False))
        self.assertFalse(host._transport._closed, "an injected shell is never closed by the workflow")
        self.assertIsNone(self.repository.get_current_run(task_id))

    def test_injected_shell_needs_names_only_and_the_approved_shell_kind(self):
        host = SimpleNamespace(choice=ShellChoice("sh", "/usr/bin/dash"))
        task_id = self.names_task("true")
        with self.assertRaises(ValueError):
            self.workflow.start(task_id, 1, worktree_path=self.root / "x1", artifacts_root=self.artifacts,
                                automation=AUTOMATION, environment_values={"PATH": "/bin"}, shell=host)
        with self.assertRaisesRegex(WorkflowHeld, "shell kind"):
            self.workflow.start(task_id, 1, worktree_path=self.root / "x2", artifacts_root=self.artifacts,
                                automation=AUTOMATION, shell=host)
        legacy = self.approved_task("true")  # environment values in the spec cannot use a host shell
        with self.assertRaisesRegex(WorkflowHeld, "environment names"):
            self.workflow.start(legacy, 1, worktree_path=self.root / "x3", artifacts_root=self.artifacts,
                                automation=AUTOMATION, shell=SimpleNamespace(choice=ShellChoice("bash", "/bin/bash")))
        self.assertIsNone(self.repository.get_current_run(task_id), "nothing started")

    def test_a_deferred_report_is_resent_once_and_nothing_else_is_replayed(self):
        task_id = self.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        run = self.workflow.start(task_id, 1, worktree_path=self.root / "deferred-report",
                                  artifacts_root=self.artifacts, automation=AUTOMATION)
        try:
            run.collect(timeout=8)
            statuses = [MailboxStatus.DEFERRED]
            original = self.mailbox.deliver

            def deliver(message, **_):
                original(message)
                return SimpleNamespace(status=statuses.pop(0) if statuses else MailboxStatus.OMP_PROCESSED)

            self.mailbox.deliver = deliver
            judged = run.judge()
            self.assertEqual(judged["report"]["status"], "deferred")
            self.assertIsNotNone(self.repository.get_current_run(task_id), "a deferred report completes nothing")
            again = run.retry_report()
            self.assertEqual(again["report"]["status"], "omp_processed")
            self.assertIsNone(self.repository.get_current_run(task_id))
            with self.assertRaises(WorkflowHeld):
                run.retry_report()
        finally:
            run.close()

    # -- CW-18 review R1/R5: report acceptance and the typing hook ------------------------
    def test_report_accepted_by_the_manager_closes_the_run_before_the_turn_ends(self):
        task_id = self.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        run = self.workflow.start(task_id, 1, worktree_path=self.root / "accepted-report",
                                  artifacts_root=self.artifacts, automation=AUTOMATION)
        try:
            run.collect(timeout=8)
            events = []
            original = self.mailbox.deliver

            def deliver(message, on_submitted=None, **_):
                original(message)
                on_submitted()  # the manager OMP accepted the report into its session
                events.append(("current_run_after_ack", self.repository.get_current_run(task_id)))
                return SimpleNamespace(status=MailboxStatus.UNKNOWN)  # its turn outlasted the receipt window

            self.mailbox.deliver = deliver
            judged = run.judge(on_report=lambda event, record: events.append((event, record["report"]["status"])))
            self.assertEqual(events, [("sending", "delivery_unknown"), ("submitted", "api_returned"),
                                      ("current_run_after_ack", None)])
            self.assertEqual(judged["report"]["status"], "unknown")
            self.assertIsNone(self.repository.get_current_run(task_id), "closed at the acceptance")
            self.assertEqual(self.repository.get_run_history(run.run_id)[-1]["kind"], "completed")
            with self.assertRaises(WorkflowHeld):
                run.judge()
        finally:
            run.close()

    def test_without_on_report_an_unknown_report_still_closes_nothing(self):
        task_id = self.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        run = self.workflow.start(task_id, 1, worktree_path=self.root / "unknown-report",
                                  artifacts_root=self.artifacts, automation=AUTOMATION)
        try:
            run.collect(timeout=8)
            original = self.mailbox.deliver

            def deliver(message, on_submitted=None, **_):
                original(message)
                self.assertIsNone(on_submitted)
                return SimpleNamespace(status=MailboxStatus.UNKNOWN)

            self.mailbox.deliver = deliver
            self.assertEqual(run.judge()["report"]["status"], "unknown")
            self.assertIsNotNone(self.repository.get_current_run(task_id))
        finally:
            run.close()

    def test_before_shell_input_runs_after_the_task_delivery_and_can_stop_any_typing(self):
        task_id = self.names_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        host = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "HOME": str(self.root)},
                               choice=ShellChoice("bash", "/usr/bin/bash"))
        host.choice = ShellChoice("bash", "/usr/bin/bash")
        host.detach = lambda: None
        self.addCleanup(host.close)
        typed = []
        original_send = host.send_user
        host.send_user = lambda data: (typed.append(data), original_send(data))[1]
        calls = []

        def refuse():
            calls.append(len(self.mailbox.messages))
            raise WorkflowHeld("host_terminal_busy: nothing was typed into the host shell")

        with self.assertRaisesRegex(WorkflowHeld, "host_terminal_busy"):
            self.workflow.start(task_id, 1, worktree_path=self.root / "refused-typing",
                                artifacts_root=self.artifacts, automation=AUTOMATION, shell=host,
                                before_shell_input=refuse)
        self.assertEqual(calls[0] >= 1, True, "the TASK was delivered before the hook")
        self.assertEqual(typed, [], "nothing was typed")
        self.assertIsNone(self.repository.get_current_run(task_id))

    def test_the_parent_cwd_file_is_read_only_when_pwd_finished_writing_it(self):
        # p27-cd69-stuck-03: `pwd -P > file` creates the file before pwd writes it; reading it as soon as it
        # existed saw "" and held the run ("parent shell cwd could not be verified"), a flake under load.
        import shlex

        class SlowPwdShell:
            choice = ShellChoice("bash", "/usr/bin/bash")

            def __init__(self, parts):
                self.typed, self.pending, self.polls = [], None, 0
                self.parts = parts

            def send_user(self, data):
                self.typed.append(data)
                if data == b"wb-handoff\n":
                    raise RuntimeError("reached wb-handoff")
                words = shlex.split(data.decode())
                if words[:1] == ["cd"] and "pwd" in words:
                    target, path = Path(words[1]), Path(words[-1])
                    path.write_text("")  # the redirection opened (and truncated) the file
                    self.pending = (path, str(target.resolve()) + "\n")

            def poll(self, timeout=0):
                self.polls += 1
                if self.pending is not None:
                    path, text = self.pending
                    step = self.polls  # pwd writes its line in pieces, a few polls later
                    path.write_text(text[:max(0, min(len(text), (step - 3) * self.parts))])
                return {"parent_mode": "manual_prompt"}

            def display_bytes(self):
                return b""

            def detach(self):
                pass

        for parts in (1000, 4):
            with self.subTest(parts=parts):
                task_id = self.names_task("printf 'PASS\\n'; printf PASS > outcome.txt")
                host = SlowPwdShell(parts)
                with self.assertRaisesRegex(RuntimeError, "reached wb-handoff"):
                    self.workflow.start(task_id, 1, worktree_path=self.root / f"slow-pwd-{parts}",
                                        artifacts_root=self.artifacts, automation=AUTOMATION, shell=host)
                self.assertEqual(host.typed[-1], b"wb-handoff\n", "the cwd was verified once pwd finished")

    def test_a_parent_cwd_that_really_differs_or_never_completes_still_holds(self):
        import shlex

        class WrongPwdShell:
            choice = ShellChoice("bash", "/usr/bin/bash")

            def __init__(self, text):
                self.typed, self.text = [], text

            def send_user(self, data):
                self.typed.append(data)
                words = shlex.split(data.decode())
                if words[:1] == ["cd"] and "pwd" in words:
                    Path(words[-1]).write_text(self.text)

            def poll(self, timeout=0):
                return {"parent_mode": "manual_prompt"}

            def display_bytes(self):
                return b""

            def detach(self):
                pass

        for name, text in (("elsewhere", "/tmp\n"), ("unterminated", "/tmp")):
            with self.subTest(case=name):
                task_id = self.names_task("printf 'PASS\\n'; printf PASS > outcome.txt")
                host = WrongPwdShell(text)
                with self.assertRaisesRegex(WorkflowHeld, "parent shell cwd could not be verified"):
                    self.workflow.start(task_id, 1, worktree_path=self.root / f"wrong-pwd-{name}",
                                        artifacts_root=self.artifacts, automation=AUTOMATION, shell=host)
                self.assertNotIn(b"wb-handoff\n", host.typed)

    def test_preparation_failure_preserves_source_and_existing_target(self):
        task_id = self.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        target = self.root / "existing-target"
        target.mkdir()
        (target / "user.txt").write_text("do not touch\n")
        with self.assertRaises(WorktreePreparationError):
            self.workflow.start(task_id, 1, worktree_path=target,
                                artifacts_root=self.artifacts, automation=AUTOMATION)
        self.assertEqual((target / "user.txt").read_text(), "do not touch\n")
        self.assertEqual(git(self.source, "status", "--porcelain=v1", "--untracked-files=all"), self.status)
        self.assertEqual((self.source / "untracked.txt").read_text(), "preserve me\n")
        self.assertIsNone(self.repository.get_current_run(task_id))
        self.assertEqual(len(self.mailbox.messages), 2)
        self.assertEqual(self.mailbox.messages[-1].payload["stage"], "preparation")
        self.assertTrue(self.mailbox.messages[-1].payload["requires_manager_resolution"])
        self.assertTrue(list(self.artifacts.glob("*/run.json")))

    def test_authority_and_direct_request_classification(self):
        task_id = self.repository.create_task({"goal": "no scope", "execution": {
            "source": str(self.source), "commit": self.commit,
            "command": ":", "criteria": {"log_contains": "PASS", "result_file": "outcome.txt",
            "result_contains": "PASS"}, "environment": {"PATH": "/usr/bin:/bin"}, "shell": "bash",
        }})
        with self.assertRaises(AuthorizationError):
            self.workflow.start(task_id, 1, worktree_path=self.root / "unauthorized",
                                artifacts_root=self.artifacts, automation=AUTOMATION)
        self.assertEqual(classify_worker_request({"paths": ["allowed.py"]}, ["allowed.py"]),
                         "existing_task_scope")
        self.assertEqual(classify_worker_request({"paths": ["other.py"]}, ["allowed.py"]),
                         "scope_expansion_manager_confirmation")
        self.assertEqual(classify_worker_request({"paths": ["src/deep/file.py"]}, ["src/"]),
                         "existing_task_scope")
        self.assertEqual(classify_worker_request({"paths": ["src/deep/file.py"]}, ["src"]),
                         "scope_expansion_manager_confirmation")
        with self.assertRaises(ValueError):
            classify_worker_request({"paths": ["src/../other.py"]}, ["src/"])
        self.assertEqual(classify_worker_request({"paths": ["small.py"], "goal": "tiny",
                                                  "bounded_small_spec": True}, None),
                         "bounded_new_spec_requires_manager_approval")
        paused = {"portVersion": 2, "kind": "AutomationState", "payload": {
            **AUTOMATION["payload"], "paused": True,
        }}
        with self.assertRaises(WorkflowHeld):
            self.workflow.start(task_id, 1, worktree_path=self.root / "paused-not-started",
                                artifacts_root=self.artifacts, automation=paused)
        self.assertIsNone(self.repository.get_current_run(task_id))

    def test_explicit_commit_and_target_guards_do_not_mutate_source(self):
        with self.assertRaises(WorktreePreparationError):
            prepare_execution_worktree(self.source, "HEAD", self.root / "bad-ref")
        with self.assertRaises(WorktreePreparationError):
            prepare_execution_worktree(self.source, self.commit, self.source / "inside-source")
        self.assertEqual(git(self.source, "status", "--porcelain=v1", "--untracked-files=all"), self.status)

    def test_public_worker_port_identity_and_current_authority_fail_closed(self):
        workflow, mailbox, worker_port, state, task_id = self.strict_task()
        no_port = TaskWorkflow(self.repository, mailbox)
        with self.assertRaises(WorkflowHeld):
            no_port.start(task_id, 1, worktree_path=self.root / "no-port",
                          artifacts_root=self.artifacts, automation=AUTOMATION)
        self.assertFalse((self.root / "no-port").exists())
        worker_port.mismatch = True
        with self.assertRaises(ValueError):
            workflow.start(task_id, 1, worktree_path=self.root / "wrong-worker-identity",
                           artifacts_root=self.artifacts, automation=AUTOMATION,
                           environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                               "CW10_SECRET": "private-sentinel"})
        self.assertFalse((self.root / "wrong-worker-identity").exists())
        self.assertIsNone(self.repository.get_current_run(task_id))
        self.assertEqual(len(mailbox.messages), 1)
        with self.assertRaises(ValueError):
            verified_worker_response("execute", mailbox.messages[0],
                                     mailbox.deliver(mailbox.messages[0]), {})

    def test_rejected_worker_response_reason_is_recorded_in_run_events(self):
        # F2 (smoke-03): the bridge's machine reason, not only "ValueError", is in the run record and events.
        from workbench.workflow.worker_port import WorkerResponseRejected
        workflow, mailbox, worker_port, _state, task_id = self.strict_task()

        def rejected(_message):
            raise WorkerResponseRejected("invalid_assistant_response:extra_text")

        worker_port.after_execute = rejected
        with self.assertRaises(WorkerResponseRejected):
            workflow.start(task_id, 1, worktree_path=self.root / "rejected-response",
                           artifacts_root=self.artifacts, automation=AUTOMATION,
                           environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                               "CW10_SECRET": "private-sentinel"})
        self.assertFalse((self.root / "rejected-response").exists())
        record = json.loads(next(self.artifacts.glob("*/run.json")).read_text())
        expected = {"type": "WorkerResponseRejected", "reason": "invalid_assistant_response:extra_text",
                    "detail": "worker response rejected: invalid_assistant_response:extra_text"}
        self.assertEqual(record["preparation_error"], expected)
        run_id = record["run_id"]
        shell = self.repository.get_shell_history(run_id)
        self.assertEqual(shell[-1]["kind"], "failed")
        self.assertEqual(shell[-1]["details"]["preparation_error"], expected)
        failed = [event for event in self.repository.get_run_history(run_id) if event["kind"] == "failed"]
        self.assertEqual(failed[-1]["details"], {"stage": "preparation", "error": expected})
        self.assertEqual([message.kind.value for message in mailbox.messages], ["task"])

    def test_rejected_or_unknown_worker_analysis_records_reason_and_raises_unavailable(self):
        # smoke-04 G2/G3: a rejected / unverifiable / unknown analysis is no worker judgment: the run records the
        # machine reason (never response text) with what the host evidence guard found, sends no report and raises
        # WorkerJudgmentUnavailable (a WorkflowHeld) so the caller closes the run and tells the manager once.
        from workbench.workflow import WorkerJudgmentUnavailable
        from workbench.workflow.worker_port import WorkerResponseRejected

        def rejected(_message):
            raise WorkerResponseRejected("invalid_assistant_response:bad_marker")

        def timeout(_message):
            raise TimeoutError("public worker response deadline expired")

        cases = {
            "rejected": ("invalid_assistant_response:bad_marker", "success", rejected, None),
            "timeout": ("worker_response_timeout", "success", timeout, None),
            "unverified": ("worker_response_unverified", "success", None, "mismatch"),
            "conflict": ("worker_judgment_conflicts_with_evidence", "indeterminate", None, None),
            "delivery_unknown": ("analysis_delivery_unknown", "success", None, "unknown"),
            "delivery_rejected": ("analysis_delivery_rejected", "success", None, "rejected"),
        }
        for label, (reason, host_judgment, hook, special) in cases.items():
            with self.subTest(case=label):
                command = ("printf PASS > outcome.txt" if label == "conflict"
                           else "printf 'PASS\\n'; printf PASS > outcome.txt")
                workflow, mailbox, worker_port, _state, task_id = self.strict_task(command)
                run = workflow.start(task_id, 1, worktree_path=self.root / f"analysis-{label}",
                                     artifacts_root=self.artifacts, automation=AUTOMATION,
                                     environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                         "CW10_SECRET": "private-sentinel"})
                try:
                    self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
                    worker_port.after_analysis = hook
                    worker_port.mismatch = special == "mismatch"
                    if special in ("unknown", "rejected"):
                        status = MailboxStatus.UNKNOWN if special == "unknown" else MailboxStatus.REJECTED
                        original = mailbox.deliver
                        mailbox.deliver = lambda message, _o=original, _s=status: (
                            SimpleNamespace(**{**vars(_o(message)), "status": _s}))
                    with self.assertRaises(WorkerJudgmentUnavailable) as raised:
                        run.judge()
                    self.assertEqual(raised.exception.reason, reason)
                    self.assertEqual(raised.exception.host_evidence["judgment"], host_judgment)
                    record = json.loads(run.result_path.read_text())
                    self.assertEqual(record["worker_analysis_rejected"]["reason"], reason)
                    self.assertEqual(record["worker_analysis_rejected"]["host_evidence"]["judgment"], host_judgment)
                    self.assertIsNone(record["worker_judgment"])
                    self.assertNotIn("report", record)
                    self.assertEqual([m.kind.value for m in mailbox.messages], ["task", "question"])
                    self.assertIsNotNone(self.repository.get_current_run(task_id), "the caller closes the run")
                finally:
                    run.close()
        # A pause while the analysis was rejected keeps the plain CW-10 hold (nothing is reported or closed).
        workflow, mailbox, worker_port, state, task_id = self.strict_task()
        run = workflow.start(task_id, 1, worktree_path=self.root / "analysis-rejected-paused",
                             artifacts_root=self.artifacts, automation=AUTOMATION,
                             environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                 "CW10_SECRET": "private-sentinel"})
        try:
            self.assertTrue(run.collect(timeout=8)["exit_confirmed"])

            def paused_then_rejected(_message):
                state["payload"]["paused"] = True
                raise WorkerResponseRejected("invalid_assistant_response:bad_marker")

            worker_port.after_analysis = paused_then_rejected
            with self.assertRaises(WorkflowHeld) as raised:
                run.judge()
            self.assertNotIsInstance(raised.exception, WorkerJudgmentUnavailable)
            self.assertNotIn("worker_analysis_rejected", json.loads(run.result_path.read_text()))
        finally:
            run.close()

    def test_unexpected_analysis_exceptions_end_unavailable_on_a_real_run(self):
        # fix-05 P2: any other exception in the analysis stage (bridge wait, observe, persist) is no worker
        # judgment either: the run records analysis_error:<Type>, delivers no report and raises
        # WorkerJudgmentUnavailable with the host evidence, so the caller closes the run indeterminate once.
        from workbench.ipc.bridge_g3.mailbox import BridgeTimeout, MailboxError
        from workbench.workflow import WorkerJudgmentUnavailable
        from workbench.workflow.run import WorkflowRun

        def raising(error):
            def hook(_message):
                raise error
            return hook

        cases = {
            "bridge_wait": ("analysis_error:BridgeTimeout", raising(BridgeTimeout("bridge wait expired")), None),
            "mailbox": ("analysis_error:MailboxError", raising(MailboxError("session lost")), None),
            "observe_oserror": ("analysis_error:OSError", raising(OSError("event log unreadable")), None),
            "observe_keyerror": ("analysis_error:KeyError", raising(KeyError("bridgeSequence")), None),
            "persist": ("analysis_error:OSError", None, "persist"),
            "report_persist": ("analysis_error:OSError", None, "report_persist"),
        }
        for label, (reason, hook, special) in cases.items():
            with self.subTest(case=label):
                workflow, mailbox, worker_port, _state, task_id = self.strict_task()
                run = workflow.start(task_id, 1, worktree_path=self.root / f"analysis-error-{label}",
                                     artifacts_root=self.artifacts, automation=AUTOMATION,
                                     environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                         "CW10_SECRET": "private-sentinel"})
                delivered = []
                original_deliver = mailbox.deliver

                def deliver(message, _o=original_deliver):
                    delivered.append(message.kind.value)
                    return _o(message)

                mailbox.deliver = deliver
                try:
                    self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
                    worker_port.after_analysis = hook
                    if special is not None:
                        def failing_persist(_run=run, _special=special):
                            if _special == "persist" or "report" in _run._record:
                                raise OSError("disk full")
                            WorkflowRun._persist(_run)

                        run._persist = failing_persist
                    with self.assertRaises(WorkerJudgmentUnavailable) as raised:
                        run.judge()
                    self.assertEqual(raised.exception.reason, reason)
                    self.assertEqual(raised.exception.host_evidence,
                                     {"judgment": "success", "reasons": ["criteria_met"]})
                    rejected = run._record["worker_analysis_rejected"]
                    self.assertEqual((rejected["stage"], rejected["reason"]), ("analysis", reason))
                    self.assertIsNone(run._record["worker_judgment"])
                    self.assertNotIn("report", delivered, "no report is delivered")
                    self.assertEqual(delivered, ["question"])
                    if special == "report_persist":
                        self.assertEqual(run._record["report"]["status"], "not_sent")
                    else:
                        self.assertNotIn("report", run._record)
                    if special is None:
                        on_disk = json.loads(run.result_path.read_text())
                        self.assertEqual(on_disk["worker_analysis_rejected"]["reason"], reason)
                        self.assertNotIn("persist_error", on_disk["worker_analysis_rejected"])
                    else:
                        self.assertEqual(rejected["persist_error"], "OSError")
                    self.assertIsNotNone(self.repository.get_current_run(task_id), "the caller closes the run")
                    with self.assertRaises(WorkflowHeld):  # nothing is reported later
                        run.retry_report()
                finally:
                    run.close()
        # A pause that happens with the failure keeps the plain CW-10 hold (nothing is reported or closed).
        workflow, mailbox, worker_port, state, task_id = self.strict_task()
        run = workflow.start(task_id, 1, worktree_path=self.root / "analysis-error-paused",
                             artifacts_root=self.artifacts, automation=AUTOMATION,
                             environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                 "CW10_SECRET": "private-sentinel"})
        try:
            self.assertTrue(run.collect(timeout=8)["exit_confirmed"])

            def paused_then_failed(_message):
                state["payload"]["paused"] = True
                raise OSError("event log unreadable")

            worker_port.after_analysis = paused_then_failed
            with self.assertRaises(WorkflowHeld) as raised:
                run.judge()
            self.assertNotIsInstance(raised.exception, WorkerJudgmentUnavailable)
            self.assertNotIn("worker_analysis_rejected", json.loads(run.result_path.read_text()))
            self.assertEqual([m.kind.value for m in mailbox.messages], ["task", "question"])
            self.assertIsNotNone(self.repository.get_current_run(task_id))
        finally:
            run.close()

    def test_cancel_revoke_and_pause_at_worker_and_analysis_barriers(self):
        for action in ("cancel", "revoke", "pause"):
            with self.subTest(action=action):
                workflow, mailbox, worker_port, state, task_id = self.strict_task()
                def interrupt(message):
                    if action == "cancel":
                        self.repository.cancel_run(message.run_id, "cancel before worktree")
                    elif action == "revoke":
                        self.repository.revoke_authority(task_id, 1, "revoke before worktree")
                    else:
                        state["payload"]["paused"] = True
                worker_port.after_execute = interrupt
                target = self.root / f"barrier-{action}"
                with self.assertRaises(WorkflowHeld):
                    workflow.start(task_id, 1, worktree_path=target,
                                   artifacts_root=self.artifacts, automation=AUTOMATION,
                                   environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                       "CW10_SECRET": "private-sentinel"})
                self.assertFalse(target.exists())
                self.assertEqual(len(mailbox.messages), 1)
        workflow, mailbox, worker_port, state, task_id = self.strict_task()
        run = workflow.start(task_id, 1, worktree_path=self.root / "analysis-paused",
                             artifacts_root=self.artifacts, automation=AUTOMATION,
                             environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                 "CW10_SECRET": "private-sentinel"})
        try:
            self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
            state["payload"]["paused"] = True
            with self.assertRaises(WorkflowHeld):
                run.judge()
            self.assertEqual(len(mailbox.messages), 1)
            state["payload"]["paused"] = False
            judged = run.judge()
            self.assertEqual(judged["worker_judgment"]["worker_response"]["decision"], "success")
            self.assertEqual([m.kind.value for m in mailbox.messages], ["task", "question", "report"])
        finally:
            run.close()

    def test_pause_after_worktree_before_shell_dispatch_holds_without_sent_event(self):
        workflow, mailbox, _worker_port, state, task_id = self.strict_task()
        original_claim = PersistentShell.claim_manager

        def pause_after_claim(shell):
            result = original_claim(shell)
            state["payload"]["paused"] = True
            return result

        target = self.root / "pre-dispatch-paused"
        with patch.object(PersistentShell, "claim_manager", pause_after_claim):
            with self.assertRaises(WorkflowHeld):
                workflow.start(task_id, 1, worktree_path=target,
                               artifacts_root=self.artifacts, automation=AUTOMATION,
                               environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                   "CW10_SECRET": "private-sentinel"})
        self.assertTrue(target.is_dir(), "prepared worktree remains for explicit disposition")
        run_id = mailbox.messages[0].run_id
        self.assertFalse(any(event["kind"] == "sent" for event in self.repository.get_shell_history(run_id)))
        self.assertEqual(len(mailbox.messages), 1)

    def test_cancel_and_revoke_after_collection_block_worker_analysis(self):
        for action in ("cancel", "revoke"):
            with self.subTest(action=action):
                workflow, mailbox, _worker_port, _state, task_id = self.strict_task()
                run = workflow.start(task_id, 1, worktree_path=self.root / f"analysis-{action}",
                                     artifacts_root=self.artifacts, automation=AUTOMATION,
                                     environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                         "CW10_SECRET": "private-sentinel"})
                try:
                    self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
                    if action == "cancel":
                        self.repository.cancel_run(run.run_id, "cancel before analysis")
                    else:
                        self.repository.revoke_authority(task_id, 1, "revoke before analysis")
                    with self.assertRaises(WorkflowHeld):
                        run.judge()
                    self.assertEqual(len(mailbox.messages), 1)
                    self.assertIsNone(json.loads(run.result_path.read_text())["worker_judgment"])
                finally:
                    run.close()

    def test_strict_preparation_failure_requires_worker_analysis_before_report(self):
        workflow, mailbox, worker_port, _state, task_id = self.strict_task()
        worker_port.after_analysis = lambda _message: None
        target = self.root / "strict-existing-target"
        target.mkdir()
        (target / "user.txt").write_text("preserve")
        with self.assertRaises(WorktreePreparationError):
            workflow.start(task_id, 1, worktree_path=target,
                           artifacts_root=self.artifacts, automation=AUTOMATION,
                           environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                               "CW10_SECRET": "private-sentinel"})
        self.assertEqual((target / "user.txt").read_text(), "preserve")
        self.assertEqual([message.kind.value for message in mailbox.messages], ["task", "question", "report"])
        report_record = json.loads(next(self.artifacts.glob("*/run.json")).read_text())
        self.assertEqual(report_record["preparation_report"]["status"], "omp_processed")
        self.assertIsNone(self.repository.get_current_run(task_id))

    def test_worker_analysis_cannot_promote_missing_evidence_or_wrong_identity(self):
        for case in ("missing-log", "wrong-identity"):
            with self.subTest(case=case):
                command = "printf PASS > outcome.txt" if case == "missing-log" else "printf 'PASS\\n'; printf PASS > outcome.txt"
                workflow, mailbox, worker_port, _state, task_id = self.strict_task(command)
                run = workflow.start(task_id, 1, worktree_path=self.root / case,
                                     artifacts_root=self.artifacts, automation=AUTOMATION,
                                     environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                         "CW10_SECRET": "private-sentinel"})
                try:
                    self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
                    if case == "wrong-identity":
                        worker_port.mismatch = True
                    with self.assertRaises((WorkflowHeld, ValueError)):
                        run.judge()
                    self.assertEqual([message.kind.value for message in mailbox.messages], ["task", "question"])
                    self.assertNotIn("report", json.loads(run.result_path.read_text()))
                finally:
                    run.close()

    def test_direct_request_durable_routes_and_secret_values_not_recorded(self):
        workflow, _mailbox, _port, _state, task_id = self.strict_task()
        within = workflow.route_worker_request({"paths": ["outcome.txt"]}, task_id=task_id, revision=1)
        self.assertEqual(within["classification"], "existing_task_scope")
        self.assertEqual(within["task_id"], task_id)
        self.assertEqual(within["revision"], 2)
        self.assertIsNotNone(within["prior_approval_decision_id"])
        self.assertEqual(self.repository.get_task_spec(task_id, 2)["spec"]["worker_request"]["paths"],
                         ["outcome.txt"])
        bounded = workflow.route_worker_request({"paths": ["new.txt"], "goal": "small bounded job",
                                                 "bounded_small_spec": True})
        self.assertEqual(bounded["classification"], "bounded_new_spec_requires_manager_approval")
        self.assertEqual(self.repository.get_task_spec(bounded["task_id"], 1)["spec"]["worker_request"]["paths"],
                         ["new.txt"])
        expanded = workflow.route_worker_request({"paths": ["outside.txt"], "goal": "expanded job"},
                                                 task_id=task_id, revision=1)
        self.assertEqual(expanded["classification"], "scope_expansion_manager_confirmation")
        self.assertEqual(expanded["revision"], 3)
        self.assertFalse(expanded["dispatch_authorized"])
        with self.assertRaises(AuthorizationError):
            workflow.start(task_id, 3, worktree_path=self.root / "unapproved-expansion",
                           artifacts_root=self.artifacts, automation=AUTOMATION,
                           environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                               "CW10_SECRET": "private-sentinel"})
        secret = "PRIVATE_CW10_SENTINEL_" + str(uuid4())
        run = workflow.start(task_id, 1, worktree_path=self.root / "secret-safe",
                             artifacts_root=self.artifacts, automation=AUTOMATION,
                             environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                 "CW10_SECRET": secret})
        try:
            run.collect(timeout=8)
            run.judge()
            self.assertNotIn(secret, run.result_path.read_text())
            self.assertNotIn(secret.encode(), (self.root / "metadata.sqlite3").read_bytes())
            self.assertNotIn(secret.encode(), run.raw_log.read_bytes())
        finally:
            run.close()


if __name__ == "__main__":
    unittest.main()
