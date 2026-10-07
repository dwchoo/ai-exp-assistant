"""CW-18 smoke corrections D1/D2 (p27-cw18-smoke-fix-01). No OMP, no provider.

D1: under a strict tool schema (openai-codex: every property present, unused ones null) a model fills every
optional field. The backend treats null, an empty/blank string, an empty list/object and ``false`` for the flags
(``run``, ``cancel``, ``requires_code_change``) as ABSENT; a clearly empty execution placeholder is absent too,
but a real-looking execution on a ``work`` Task is rejected with a message naming ``spec.execution`` and the
expected value. Every rejection names the offending field and what is expected.
D2: ``spec.paths`` are repo-relative; an absolute path inside the project directory is normalised to a relative
one, one outside is rejected with a precise message.
"""

from __future__ import annotations

import inspect
from pathlib import Path
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))

from test_task_flow import (  # noqa: E402
    AUTOMATION, FakeWorkflow, FlowFixture, execution, request, wait_until,
)

from workbench.backend import flow, service  # noqa: E402
from workbench.backend.flow import HandoffService  # noqa: E402
from workbench.backend.flow_tasks import ExperimentPorts, TaskFlow  # noqa: E402
from workbench.contracts.v1 import ActorRole, MessageKind  # noqa: E402
from workbench.tasks.repository import TaskRepository  # noqa: E402

# What GPT-5.5 sent in the smoke, with the strict-schema nulls it now may send instead.
SMOKE_PLACEHOLDER_EXECUTION = {"source": "unused", "commit": "unused", "command": "true",
                               "criteria": {"log_contains": "unused", "result_file": "unused",
                                            "result_contains": "unused"},
                               "environment": [], "shell": "bash"}
EMPTY_EXECUTION = {"source": "", "commit": "", "command": "",
                   "criteria": {"log_contains": "", "result_file": "", "result_contains": ""},
                   "environment": [], "shell": "bash"}


def strict_work(**overrides):
    """A new work Task as a strict-schema model sends it: every field present, the unused ones null."""
    args = {"task_id": None, "kind": "work", "message": "Create work/hello.txt containing hello from worker.",
            "spec": {"goal": "create hello.txt", "paths": ["work/hello.txt"], "instructions": None,
                     "execution": None},
            "run": None, "cancel": None}
    args.update(overrides)
    return args


class SmokeFlowFixture(FlowFixture):
    """FlowFixture with a project directory (D2) bound into the TaskFlow."""

    def open(self, *, start=True):
        self.project = self.root / "project"
        self.project.mkdir(exist_ok=True)
        svc = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=self.mailbox,
                             paused=self.paused, retry_interval=0.01)
        ports = ExperimentPorts(host_shell=lambda: self.port,
                                make_workflow=lambda repository: FakeWorkflow(repository, self.runs, self.gates),
                                automation=lambda: AUTOMATION, environment_names=lambda: {"PATH"},
                                worktrees_root=self.root, artifacts_root=self.root)
        task_flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl",
                             repository_factory=lambda: TaskRepository(self.db), handoffs=svc,
                             omp_idle=lambda role: True, paused=self.paused, experiment=ports,
                             poll_interval=0.02, collect_slice=0.05, project_dir=self.project)
        svc.configure(policy=task_flow, active_task=task_flow.active_task)
        svc.start()
        if start:
            task_flow.start()
        self.service, self.flow = svc, task_flow
        self.controller.flow = task_flow
        self.services.append(svc)
        self.flows.append(task_flow)
        return task_flow

    def task_message(self):
        self.assertTrue(wait_until(lambda: any(m.kind is MessageKind.TASK for m in self.mailbox.created)))
        return next(m for m in self.mailbox.created if m.kind is MessageKind.TASK)

    def errors(self, result):
        self.assertEqual(result["status"], "rejected", result)
        return " | ".join(result.get("errors") or [result.get("detail", "")])


class NullableArgumentsTests(SmokeFlowFixture):
    def test_strict_null_work_task_is_dispatched_without_execution(self):
        result = self.to_worker(strict_work())
        self.assertEqual((result["status"], result["kind"]), ("dispatched", "work"), result)
        payload = self.task_message().payload
        self.assertEqual(payload["paths"], ["work/hello.txt"])
        self.assertNotIn("execution", payload)
        self.assertNotIn("instructions", payload)
        scope = self.decisions(result["task_id"], 1)[0]["details"]
        self.assertNotIn("execution", scope)
        self.assertNotIn("instructions", scope)

    def test_empty_strings_lists_and_false_flags_are_absent(self):
        args = strict_work(task_id="", run=False, cancel=False)
        args["spec"].update(instructions="  ", execution={})
        result = self.to_worker(args)
        self.assertEqual(result["status"], "dispatched", result)

    def test_clearly_empty_execution_placeholder_on_a_work_task_is_ignored(self):
        args = strict_work()
        args["spec"]["execution"] = EMPTY_EXECUTION
        result = self.to_worker(args)
        self.assertEqual(result["status"], "dispatched", result)
        self.assertNotIn("execution", self.task_message().payload)

    def test_real_looking_execution_on_a_work_task_is_rejected_naming_the_field(self):
        args = strict_work(task_id=None, run=False, cancel=False)
        args["spec"]["execution"] = SMOKE_PLACEHOLDER_EXECUTION
        result = self.to_worker(args)
        self.assertEqual(result["reason"], "invalid_arguments", result)
        text = self.errors(result)
        self.assertIn("spec.execution", text)
        self.assertIn("null", text)
        self.assertIn("work", text)
        self.assertNotIn("unused", text, "values are never echoed")
        self.assertIsNone(self.flow.task_view())

    def test_experiment_without_execution_names_the_expected_object(self):
        result = self.to_worker(strict_work(kind="experiment"))
        text = self.errors(result)
        self.assertIn("spec.execution", text)
        for name in ("source", "commit", "command", "criteria", "environment", "shell"):
            self.assertIn(name, text)

    def test_strict_null_experiment_is_dispatched(self):
        args = strict_work(kind="experiment", message="run the check")
        args["spec"] = {"goal": "check", "paths": ["src/"], "instructions": None, "execution": execution()}
        result = self.to_worker(args)
        self.assertEqual((result["status"], result["kind"]), ("dispatched", "experiment"), result)

    def test_follow_up_with_placeholder_spec_and_flags_is_a_question(self):
        first = self.running_work()
        args = {"task_id": first["task_id"], "kind": "work", "message": "also add a newline",
                "spec": {"goal": "", "paths": [], "instructions": None, "execution": None},
                "run": False, "cancel": False}
        result = self.to_worker(args)
        self.assertEqual(result["status"], "queued", result)
        # the lane creates the message after the call returns (p27-cd70-fix-02: no read before it exists)
        self.assertTrue(wait_until(lambda: any(m.kind is MessageKind.QUESTION for m in self.mailbox.created)))
        question = [m for m in self.mailbox.created if m.kind is MessageKind.QUESTION][-1]
        self.assertNotIn("paths", question.payload)

    def test_cancel_false_with_task_id_is_not_a_cancel(self):
        first = self.running_work()
        result = self.to_worker({"task_id": first["task_id"], "kind": "work", "message": "status?",
                                 "spec": None, "run": None, "cancel": False})
        self.assertEqual(result["status"], "queued", result)

    def test_rejections_name_the_field_and_the_expected_value(self):
        cases = {
            "task_id": ({"kind": "work", "message": "x", "task_id": "123"}, ("task_id", "null")),
            "run": ({"kind": "work", "message": "x", "run": "yes"}, ("run", "true", "null")),
            "spec": ({"kind": "work", "message": "x", "spec": "src/"}, ("spec", "goal", "paths", "null")),
            "paths": ({"kind": "work", "message": "x", "spec": {"goal": "g", "paths": "src/"}},
                      ("spec.paths", "repo-relative")),
            "kind": ({"kind": "nope", "message": "x"}, ("kind", "experiment", "work")),
        }
        for name, (args, needles) in cases.items():
            with self.subTest(name=name):
                text = self.errors(self.to_worker(args))
                for needle in needles:
                    self.assertIn(needle, text)

    def test_unknown_task_and_missing_spec_say_what_to_send(self):
        unknown = self.to_worker(strict_work(task_id=str(uuid4()), spec=None))
        self.assertEqual(unknown["reason"], "unknown_task")
        self.assertIn("task_id", unknown["detail"])
        self.assertIn("null", unknown["detail"])
        missing = self.to_worker(strict_work(spec=None))
        self.assertEqual(missing["reason"], "spec_required")
        self.assertIn("spec", self.errors(missing))
        self.assertIn("paths", self.errors(missing))
        cancel = self.to_worker({"task_id": None, "kind": "work", "message": "stop", "spec": None, "cancel": True})
        self.assertEqual(cancel["reason"], "task_id_required")
        self.assertIn("task_id", cancel["detail"])

    def test_unknown_keys_are_still_rejected_even_when_null(self):
        result = self.to_worker({**strict_work(), "role": None})
        self.assertEqual(result["reason"], "invalid_arguments")

    def test_validate_arguments_accepts_the_strict_null_shapes(self):
        self.assertEqual(flow.validate_arguments("to_worker", strict_work()), [])
        self.assertEqual(flow.validate_arguments("to_manager", {
            "kind": "done", "message": "m", "task_id": None, "in_reply_to": None, "requires_code_change": None,
            "reason": None, "request": None}), [])


class ToManagerPlaceholderTests(SmokeFlowFixture):
    def report(self, **fields):
        work = self.running_work()
        args = {"kind": "done", "message": "wrote work/hello.txt", "task_id": work["task_id"], **fields}
        result = self.to_manager(args)
        self.assertEqual(result["status"], "queued", result)
        self.assertTrue(wait_until(lambda: any(m.target_role is ActorRole.MANAGER for m in self.mailbox.created)))
        return self.mailbox.to(ActorRole.MANAGER)[-1].payload

    def test_null_and_false_placeholders_are_not_reported(self):
        payload = self.report(in_reply_to=None, requires_code_change=False, reason=None, request=None)
        self.assertEqual(payload, {"handoff": "to_manager", "kind": "done", "message": "wrote work/hello.txt"})

    def test_smoke_placeholder_request_without_paths_is_not_a_request(self):
        payload = self.report(in_reply_to="", requires_code_change=None, reason="",
                              request={"goal": "none", "paths": []})
        self.assertNotIn("request", payload)
        self.assertNotIn("requires_code_change", payload)
        self.assertNotIn("reason", payload)

    def test_malformed_request_is_still_rejected_naming_the_shape(self):
        work = self.running_work()
        for request_value in ({"goal": "g"}, {"goal": "g", "paths": [], "extra": 1}, "src/"):
            with self.subTest(request=request_value):
                result = self.to_manager({"kind": "done", "message": "m", "task_id": work["task_id"],
                                          "request": request_value})
                self.assertEqual(result["reason"], "invalid_arguments", result)
                self.assertIn("request", self.errors(result))
                self.assertIn("null", self.errors(result))

    def test_real_fields_are_kept(self):
        payload = self.report(requires_code_change=True, reason="parser bug",
                              request={"goal": "fix parser", "paths": ["src/parser/"]})
        self.assertIs(payload["requires_code_change"], True)
        self.assertEqual(payload["reason"], "parser bug")
        self.assertEqual(payload["request"]["paths"], ["src/parser/"])


class RepoRelativePathTests(SmokeFlowFixture):
    def test_absolute_paths_inside_the_project_are_made_relative(self):
        args = strict_work()
        args["spec"]["paths"] = [str(self.project / "work" / "hello.txt"), str(self.project / "src") + "/",
                                 "./docs/notes.md"]
        result = self.to_worker(args)
        self.assertEqual(result["status"], "dispatched", result)
        expected = ["work/hello.txt", "src/", "docs/notes.md"]
        self.assertEqual(self.task_message().payload["paths"], expected)
        self.assertEqual(self.decisions(result["task_id"], 1)[0]["details"]["paths"], expected)

    def test_absolute_path_outside_the_project_is_rejected_precisely(self):
        args = strict_work()
        args["spec"]["paths"] = ["work/ok.txt", "/etc/passwd"]
        result = self.to_worker(args)
        self.assertEqual(result["reason"], "invalid_paths", result)
        text = self.errors(result)
        self.assertIn("spec.paths[1]", text)
        self.assertIn("outside the project", text)
        self.assertIn("repo-relative", text)
        self.assertIsNone(self.flow.task_view())

    def test_project_root_and_dot_dot_are_rejected_precisely(self):
        for paths, index in (([str(self.project)], 0), (["src/", "../other"], 1), (["a//b"], 0)):
            with self.subTest(paths=paths):
                args = strict_work()
                args["spec"]["paths"] = paths
                result = self.to_worker(args)
                self.assertEqual(result["reason"], "invalid_paths", result)
                self.assertIn(f"spec.paths[{index}]", self.errors(result))

    def test_worker_request_paths_are_normalised_too(self):
        work = self.to_worker({"kind": "work", "message": "m", "spec": {"goal": "g", "paths": ["src/parser/"]}})
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"
                                   and len(self.mailbox.delivered) >= 1))
        result = self.to_manager({"kind": "blocked", "message": "need more", "task_id": work["task_id"],
                                  "request": {"goal": "g", "paths": [str(self.project / "src/parser/x.py")]}})
        self.assertEqual(result["status"], "queued", result)
        self.assertTrue(wait_until(lambda: self.mailbox.to(ActorRole.MANAGER)))
        request_payload = self.mailbox.to(ActorRole.MANAGER)[-1].payload["request"]
        self.assertEqual(request_payload["paths"], ["src/parser/x.py"])
        self.assertEqual(request_payload["classification"], "existing_task_scope")

    def test_backend_binds_the_project_directory_into_the_task_flow(self):
        source = inspect.getsource(service.Backend._open)
        call = source[source.index("TaskFlow("):]
        self.assertIn("project_dir=self.project_dir", call[:call.index("lifecycle=")])


class HandoffServiceNormalisationTests(unittest.TestCase):
    """The policy sees normalised arguments (absent fields dropped), also with the U1 placeholder policy."""

    def test_policy_receives_arguments_without_absent_fields(self):
        import tempfile

        seen = []

        class Policy:
            def decide(self, req, active):
                seen.append(dict(req.args))
                return flow.HandoffDecision({"status": "noted"})

        with tempfile.TemporaryDirectory(prefix="cw18-smoke-norm-", dir="/tmp") as tmp:
            svc = HandoffService(Path(tmp) / "handoffs.jsonl", policy=Policy())
            try:
                result = svc.handle(ActorRole.MANAGER, request("to_worker", strict_work(), "n-1"))
                self.assertEqual(result, {"status": "noted"})
                self.assertEqual(seen[-1], {"kind": "work", "message": strict_work()["message"],
                                            "spec": {"goal": "create hello.txt", "paths": ["work/hello.txt"]}})
                svc.handle(ActorRole.WORKER, request("to_manager", {
                    "kind": "report", "message": "m", "task_id": "", "in_reply_to": None,
                    "requires_code_change": False, "reason": " ", "request": {"goal": "", "paths": []}}, "n-2"))
                self.assertEqual(seen[-1], {"kind": "report", "message": "m"})
            finally:
                svc.close()


if __name__ == "__main__":
    unittest.main()
