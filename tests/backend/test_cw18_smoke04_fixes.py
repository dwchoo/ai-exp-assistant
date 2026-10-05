"""CW-18 smoke-04 G2/G3 (p27-cw18-smoke-fix-04), no OMP and no provider.

A staged worker analysis that was rejected (or whose outcome is unknown) must not leave the Task waiting forever:
the run closes as indeterminate (never success), the worker is free, the automation for the run ends, the manager
is told once (reason code, what was and was not judged) and nothing is resent. The reason is a machine code in the
run's closing record, the flow record and the Task ``last_result`` (never response text).
"""

from __future__ import annotations

from hashlib import sha256
import importlib.util
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4

from workbench.backend.flow import HandoffService
from workbench.backend.flow_tasks import ExperimentPorts, TaskFlow
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import BridgeTimeout, MailboxError
from workbench.tasks.repository import TaskRepository
from workbench.workflow.run import WorkerJudgmentUnavailable, WorkflowHeld, WorkflowRun, _canonical

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FLOW = _load("cw18_task_flow_for_smoke04", ROOT / "tests/backend/test_task_flow.py")
REASON = "invalid_assistant_response:bad_marker"
HOST = {"judgment": "success", "reasons": ["criteria_met"]}
SECRET = "SMOKE04_MODEL_TEXT_SENTINEL"


class Lifecycle:
    """The AutomationController hooks the flow calls, plus the review view it may read."""

    def __init__(self):
        self.ended: list[str] = []
        self.review = {"status": "unknown", "reason": "delivery_not_confirmed"}

    def experiment_started(self, run):
        pass

    def run_ended(self, run_id):
        self.ended.append(run_id)

    def status(self):
        return {"state": "active", "review": dict(self.review)}


class RejectedAnalysisRun(FLOW.FakeRun):
    """Exits like a real run, then judges as run.py does when the worker analysis is rejected."""

    def __init__(self, repository, run_id, gates, fixture):
        super().__init__(repository, run_id, gates)
        self.fixture = fixture
        self._record = {"shell_state": "exited", "exit_status": 0, "exit_confirmed": True,
                        "raw_log_collected": {"size": 5, "sha256": "0" * 64},
                        "result_collected": {"path": "out.txt", "size": 4, "sha256": "1" * 64}}
        self.persisted = 0

    def _persist(self):
        self.persisted += 1

    def collect(self, *, timeout=1, paused=False):
        record = super().collect(timeout=timeout, paused=paused)
        if record["exit_confirmed"]:
            self.fixture.exited.set()
        return record

    def judge(self):
        self.gates.judged += 1
        if self.fixture.mode == "never_idle":
            raise AssertionError("no analysis may start while the worker is not idle")
        if self.fixture.mode == "generic_error":  # fix-05 P2: an exception the run did not convert
            raise OSError("bridge event log unreadable")
        if self.fixture.mode == "paused_generic_error":
            self.fixture.controller.automation = {"state": "paused"}
            raise OSError("bridge event log unreadable")
        # run.py records the reason in run.json before raising (never response text).
        self._record["worker_analysis_rejected"] = {"stage": "analysis", "reason": REASON, "host_evidence": HOST}
        raise WorkerJudgmentUnavailable(REASON, HOST)


class AnalysisFailureTests(FLOW.FlowFixture):
    mode = "rejected"

    def open(self, *, start=True):
        self.lifecycle = getattr(self, "lifecycle", None) or Lifecycle()
        self.exited = getattr(self, "exited", None) or threading.Event()
        self.made: list[RejectedAnalysisRun] = []
        fixture = self

        class Workflow(FLOW.FakeWorkflow):
            def start(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell):
                run = super().start(task_id, revision, worktree_path=worktree_path,
                                    artifacts_root=artifacts_root, automation=automation, shell=shell)
                made = RejectedAnalysisRun(self.repository, run.run_id, self.gates, fixture)
                fixture.made.append(made)
                return made

        def omp_idle(role):  # the worker is idle for the start; in "never_idle" not again after the exit
            return not (self.mode == "never_idle" and role is ActorRole.WORKER and self.exited.is_set())

        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=self.mailbox,
                                 paused=self.paused, retry_interval=0.01)
        ports = ExperimentPorts(host_shell=lambda: self.port,
                                make_workflow=lambda repository: Workflow(repository, self.runs, self.gates),
                                automation=lambda: FLOW.AUTOMATION, environment_names=lambda: {"PATH"},
                                worktrees_root=self.root, artifacts_root=self.root)
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl",
                        repository_factory=lambda: TaskRepository(self.db), handoffs=service,
                        omp_idle=omp_idle, paused=self.paused, experiment=ports, lifecycle=self.lifecycle,
                        poll_interval=0.02, collect_slice=0.05, analysis_idle_limit=0.4)
        service.configure(policy=flow, active_task=flow.active_task)
        service.start()
        if start:
            flow.start()
        self.service, self.flow = service, flow
        self.controller.flow = flow
        self.services.append(service)
        self.flows.append(flow)
        return flow

    def notices_to_manager(self):
        return [m for m in self.mailbox.to(ActorRole.MANAGER)
                if (m.payload or {}).get("handoff") == "workbench_notice"]

    def run_to_the_end(self):
        result = self.new_experiment()
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(FLOW.wait_until(lambda: self.flow.task_view()["status"] == "finished", 10),
                        self.flow.task_view())
        return result["task_id"]

    def assert_closed_indeterminate(self, task_id, reason, *, judged_by_host):
        view = self.flow.task_view()
        run_id = view["last_result"]["run_id"]
        # The Task is not waiting any more: finished (re-run possible), the worker is free.
        self.assertEqual(view["status"], "finished")
        self.assertEqual(view["held_reason"], f"judgment_unavailable:{reason}")
        self.assertEqual(self.flow.worker_view(), {"state": "idle", "task_id": None})
        last = view["last_result"]
        self.assertEqual((last["outcome"], last["judgment"], last["reason"], last["run_closed"], last["report"]),
                         ("judgment_unavailable", "indeterminate", reason, True, "not_sent"))
        self.assertEqual(last["not_judged"], ["worker_analysis"])
        self.assertEqual(last["judged"]["exit_confirmed"], True)
        self.assertEqual(last["judged"]["host_evidence"], HOST if judged_by_host else None)
        # The run is closed as indeterminate (never success) with the reason.
        self.assertIsNone(self.current_run(task_id))
        terminal = self.run_history(run_id)[-1]
        self.assertEqual(terminal["kind"], "completed")
        self.assertEqual((terminal["details"]["judgment"], terminal["details"]["reason"]), ("indeterminate", reason))
        ended = [r for r in self.ledger() if r.get("type") == "run_ended" and r.get("run_id") == run_id]
        self.assertEqual([(r["outcome"], r["reason"]) for r in ended], [("judgment_unavailable", reason)])
        # The automation for the run ends.
        self.assertTrue(FLOW.wait_until(lambda: self.lifecycle.ended == [run_id]))
        # The manager is told once: reason, judged / not judged; nothing is resent.
        self.assertTrue(FLOW.wait_until(lambda: len(self.notices_to_manager()) == 1))
        time.sleep(0.3)
        notices = self.notices_to_manager()
        self.assertEqual(len(notices), 1)
        payload = notices[0].payload
        self.assertEqual((notices[0].kind, payload["notice"], payload["task_id"], payload["reason"],
                          payload["judgment"], payload["not_judged"]),
                         (MessageKind.REPORT, "run_judgment_unavailable", task_id, reason, "indeterminate",
                          ["worker_analysis"]))
        self.assertIn(reason, payload["message"])
        self.assertIn("indeterminate", payload["message"])
        self.assertEqual(notices[0].in_reply_to_message_id, self.made[-1].task_message_id)
        self.assertEqual([n["kind"] for n in self.flow.notices], ["run_judgment_unavailable"])
        self.assertEqual(self.mailbox.to(ActorRole.WORKER), [], "no analysis question or delivery is resent")
        self.assertNotIn(SECRET, repr([self.ledger(), view, payload]))
        return run_id

    def test_rejected_analysis_closes_the_task_indeterminate_frees_worker_and_tells_manager_once(self):
        task_id = self.run_to_the_end()
        self.assert_closed_indeterminate(task_id, REASON, judged_by_host=True)
        self.assertEqual(self.gates.judged, 1, "the rejected analysis is never asked again")
        self.assertEqual(self.made[-1]._record["worker_analysis_rejected"]["reason"], REASON)
        # The worker is free: a new Task is accepted at once (not worker_busy).
        second = self.new_experiment("the next experiment")
        self.assertEqual(second["status"], "dispatched", second)

    def test_cancel_during_the_failed_analysis_still_closes_as_cancelled(self):
        self.gates.judge.clear()
        original = RejectedAnalysisRun.judge

        def judge(run):
            self.gates.judge.wait(5)
            return original(run)

        RejectedAnalysisRun.judge = judge
        self.addCleanup(setattr, RejectedAnalysisRun, "judge", original)
        result = self.new_experiment()
        task_id = result["task_id"]
        self.assertTrue(FLOW.wait_until(lambda: self.flow.task_view()["status"] == "waiting_report"))
        cancel = {"kind": "experiment", "task_id": task_id, "cancel": True, "message": "stop"}
        self.assertEqual(self.to_worker(cancel)["status"],
                         "cancel_requested")
        self.gates.judge.set()
        self.assertTrue(FLOW.wait_until(lambda: self.flow.task_view()["status"] == "closed", 10))
        self.assertEqual(self.flow.task_view()["closed_reason"], "cancelled")
        self.assertEqual(self.notices_to_manager(), [])
        self.assertIsNone(self.current_run(task_id))


class WorkerNeverIdleTests(AnalysisFailureTests):
    mode = "never_idle"

    def test_rejected_analysis_closes_the_task_indeterminate_frees_worker_and_tells_manager_once(self):
        task_id = self.run_to_the_end()
        reason = "worker_not_idle_for_analysis:review_unknown:delivery_not_confirmed"
        self.assert_closed_indeterminate(task_id, reason, judged_by_host=False)
        self.assertEqual(self.gates.judged, 0)
        self.assertEqual(self.made[-1]._record["worker_analysis_rejected"]["reason"], reason)
        self.assertGreaterEqual(self.made[-1].persisted, 1)

    def test_cancel_during_the_failed_analysis_still_closes_as_cancelled(self):
        self.skipTest("covered by AnalysisFailureTests")


class GenericAnalysisErrorTests(AnalysisFailureTests):
    """fix-05 P2: an unexpected exception that reaches the flow from the analysis stage ends like the known ones."""

    mode = "generic_error"

    def test_rejected_analysis_closes_the_task_indeterminate_frees_worker_and_tells_manager_once(self):
        task_id = self.run_to_the_end()
        reason = "analysis_error:OSError"
        self.assert_closed_indeterminate(task_id, reason, judged_by_host=False)
        self.assertEqual(self.gates.judged, 1)
        self.assertEqual(self.made[-1]._record["worker_analysis_rejected"]["reason"], reason)
        self.assertNotIn("runner_error", repr(self.ledger()))
        second = self.new_experiment("the next experiment")
        self.assertEqual(second["status"], "dispatched", second)


class PausedGenericAnalysisErrorTests(AnalysisFailureTests):
    mode = "paused_generic_error"

    def test_rejected_analysis_closes_the_task_indeterminate_frees_worker_and_tells_manager_once(self):
        # A pause together with the failure keeps the CW-10 hold: the run stays current, nothing is told.
        task_id = self.new_experiment()["task_id"]
        self.assertTrue(FLOW.wait_until(lambda: self.flow.task_view()["status"] == "held", 10),
                        self.flow.task_view())
        view = self.flow.task_view()
        self.assertEqual((view["held_reason"], view["last_result"]["outcome"], view["last_result"]["run_closed"]),
                         ("run_held", "held", False))
        self.assertIn("analysis_error:OSError", view["last_result"]["reason"])
        self.assertIsNotNone(self.current_run(task_id))
        time.sleep(0.3)
        self.assertEqual(self.notices_to_manager(), [])
        self.assertEqual(self.flow.notices, [])
        self.assertEqual(self.lifecycle.ended, [])
        self.assertEqual(self.gates.judged, 1)

    def test_cancel_during_the_failed_analysis_still_closes_as_cancelled(self):
        self.skipTest("covered by AnalysisFailureTests")


# -- real WorkflowRun.judge in the flow (no host shell: collect reports the exit; judge is run.py's own) -------
SESSION = str(uuid4())


class RunMailbox(FLOW.FakeMailbox):
    """The run's worker/report transport; ``report`` decides how the REPORT delivery behaves."""

    def __init__(self, fixture):
        super().__init__()
        self.fixture = fixture

    def deliver(self, message, *, timeout=20, on_submitted=None):
        self.delivered.append(message.message_id)
        if message.kind is MessageKind.REPORT:
            if self.fixture.report == "error_before_acceptance":
                raise MailboxError("manager session lost")
            if on_submitted is not None:
                on_submitted()
            if self.fixture.report == "error_after_acceptance":
                raise MailboxError("manager turn wait lost")
        return FLOW.DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, SESSION, 1,
                                    FLOW.MailboxStatus.OMP_PROCESSED, {})


class WorkerPort:
    def __init__(self, fixture):
        self.fixture = fixture
        self.observed = 0

    def arm(self, stage, message):
        pass

    def observe(self, stage, message, receipt):
        self.observed += 1
        if self.fixture.observe is not None:
            self.fixture.observe()
        return {"stage": stage, "task_id": message.task_id, "revision": message.revision, "run_id": message.run_id,
                "message_id": message.message_id, "delivery_attempt_id": receipt.delivery_attempt_id,
                "session_id": receipt.session_id, "session_generation": receipt.session_generation,
                "source": "omp_assistant_response", "response_id": str(uuid4()),
                "assistant_event_sequence": 1, "delivery_event_sequence": 2, "decision": "success"}


class CollectedRun(WorkflowRun):
    """run.py's WorkflowRun; only the host-shell collection is replaced by a confirmed exit (exit 0)."""

    def collect(self, *, timeout=5, paused=False):
        self._terminal = True
        self._record.update({"shell_state": "exited", "exit_status": 0, "exit_confirmed": True})
        self.fixture.exited.set()
        return dict(self._record)


class RealRunAnalysisTests(AnalysisFailureTests):
    mode = "real"
    observe = None
    report = "normal"
    persist_fails = False

    def open(self, *, start=True):
        self.lifecycle = getattr(self, "lifecycle", None) or Lifecycle()
        self.exited = getattr(self, "exited", None) or threading.Event()
        self.made = []
        self.run_mailbox = getattr(self, "run_mailbox", None) or RunMailbox(self)
        self.worker_port = getattr(self, "worker_port", None) or WorkerPort(self)
        fixture = self

        def current_automation():
            return {"portVersion": 2, "kind": "AutomationState",
                    "payload": {**FLOW.AUTOMATION["payload"], "paused": fixture.paused()}}

        class Workflow:
            def __init__(self, repository):
                self.repository = repository

            def start(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell):
                repository = self.repository
                task = repository.get_task_spec(task_id, revision)
                approval = [d for d in repository.get_decisions(task_id, revision) if d["kind"] == "scope_approved"][-1]
                execution = dict(task["spec"]["execution"])
                run_id = repository.start_run(task_id, revision, inputs={})
                worktree = Path(worktree_path)
                worktree.mkdir()
                (worktree / "out.txt").write_text("PASS")
                record_dir = Path(artifacts_root) / run_id
                record_dir.mkdir()
                (record_dir / "raw.log").write_bytes(b"PASS\n")
                message = fixture.run_mailbox.create_message(task_id, revision, run_id, ActorRole.MANAGER,
                                                             ActorRole.WORKER, MessageKind.TASK, {"stage": "execute"})
                run = CollectedRun(
                    repository, fixture.run_mailbox, task_id, revision, run_id, message.message_id,
                    SimpleNamespace(path=worktree.resolve(), commit=execution["commit"]), None, execution,
                    record_dir, record_dir / "raw.log", record_dir / "run.json", None,
                    sha256(_canonical({"task": task, "approval": approval})).hexdigest(),
                    {"task_id": task_id, "run_id": run_id, "shell_state": "sent", "worker_judgment": None},
                    {"sent"}, current_automation, fixture.worker_port, owns_shell=False)
                run.fixture = fixture
                if fixture.persist_fails:
                    def persist(_run=run):
                        if _run._terminal:
                            raise OSError("disk full")
                        WorkflowRun._persist(_run)
                    run._persist = persist
                fixture.made.append(run)
                return run

        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=self.mailbox,
                                 paused=self.paused, retry_interval=0.01)
        ports = ExperimentPorts(host_shell=lambda: self.port, make_workflow=Workflow,
                                automation=lambda: FLOW.AUTOMATION, environment_names=lambda: {"PATH"},
                                worktrees_root=self.root, artifacts_root=self.root)
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl",
                        repository_factory=lambda: TaskRepository(self.db), handoffs=service,
                        omp_idle=lambda role: True, paused=self.paused, experiment=ports, lifecycle=self.lifecycle,
                        poll_interval=0.02, collect_slice=0.05, analysis_idle_limit=0.4)
        service.configure(policy=flow, active_task=flow.active_task)
        service.start()
        if start:
            flow.start()
        self.service, self.flow = service, flow
        self.controller.flow = flow
        self.services.append(service)
        self.flows.append(flow)
        return flow

    def run_kinds(self):
        return [m.kind.value for m in self.run_mailbox.created]

    def assert_real_unavailable(self, reason, *, judged_by_host=True):
        task_id = self.run_to_the_end()
        run_id = self.assert_closed_indeterminate(task_id, reason, judged_by_host=judged_by_host)
        run = self.made[-1]
        self.assertEqual(run._record["worker_analysis_rejected"]["reason"], reason)
        self.assertIsNone(run._record["worker_judgment"])
        self.assertEqual(self.run_kinds(), ["task", "question"], "one analysis question, no report")
        self.assertEqual([e["kind"] for e in self.run_history(run_id)].count("completed"), 1)
        return run

    def test_rejected_analysis_closes_the_task_indeterminate_frees_worker_and_tells_manager_once(self):
        # The harness itself: a processed analysis and an accepted report free the worker without a notice.
        task_id = self.new_experiment()["task_id"]
        self.assertTrue(FLOW.wait_until(lambda: any(r.get("type") == "report_receipt" for r in self.ledger()), 10),
                        self.flow.task_view())  # the final receipt is recorded after the acceptance freed the worker
        self.assertEqual(self.flow.task_view()["status"], "finished")
        last = self.flow.task_view()["last_result"]
        self.assertEqual((last["outcome"], last["judgment"], last["report"]),
                         ("reported", "success", "omp_processed"))
        self.assertIsNone(self.current_run(task_id))
        self.assertEqual(self.run_kinds(), ["task", "question", "report"])
        self.assertEqual(self.notices_to_manager(), [])

    def test_cancel_during_the_failed_analysis_still_closes_as_cancelled(self):
        self.skipTest("covered by AnalysisFailureTests")

    def test_bridge_wait_error_at_observe(self):
        def observe():
            raise BridgeTimeout("public bridge wait expired")
        self.observe = observe
        run = self.assert_real_unavailable("analysis_error:BridgeTimeout")
        self.assertEqual(run._record["worker_analysis_rejected"]["host_evidence"], HOST)

    def test_os_and_key_errors_at_observe(self):
        def observe():
            raise KeyError("bridgeSequence")
        self.observe = observe
        self.assert_real_unavailable("analysis_error:KeyError")

    def test_persist_failure_during_the_analysis(self):
        self.persist_fails = True
        run = self.assert_real_unavailable("analysis_error:OSError")
        self.assertEqual(run._record["worker_analysis_rejected"]["persist_error"], "OSError")

    def test_pause_during_the_analysis_failure_keeps_the_hold(self):
        def observe():
            self.controller.automation = {"state": "paused"}
            raise OSError("event log unreadable")
        self.observe = observe
        task_id = self.new_experiment()["task_id"]
        self.assertTrue(FLOW.wait_until(lambda: self.flow.task_view()["status"] == "held", 10),
                        self.flow.task_view())
        view = self.flow.task_view()
        self.assertEqual((view["held_reason"], view["last_result"]["run_closed"]), ("run_held", False))
        self.assertIsNotNone(self.current_run(task_id))
        self.assertNotIn("worker_analysis_rejected", self.made[-1]._record)
        time.sleep(0.3)
        self.assertEqual(self.notices_to_manager(), [])
        self.assertEqual(self.lifecycle.ended, [])
        self.assertEqual(self.run_kinds(), ["task", "question"])

    def test_error_after_the_report_was_accepted_keeps_the_delivered_report(self):
        self.report = "error_after_acceptance"
        task_id = self.new_experiment()["task_id"]
        self.assertTrue(FLOW.wait_until(lambda: self.flow.task_view()["status"] == "finished", 10),
                        self.flow.task_view())
        self.assertTrue(FLOW.wait_until(lambda: any(r.get("type") == "run_error" for r in self.ledger())))
        view = self.flow.task_view()
        last = view["last_result"]
        self.assertEqual((view["held_reason"], last["outcome"], last["judgment"], last["report"], last["run_closed"]),
                         (None, "reported", "success", "accepted_by_manager", True))
        self.assertEqual(last["error"], "report_error:MailboxError")
        run_id = last["run_id"]
        self.assertIsNone(self.current_run(task_id))
        self.assertEqual([e["kind"] for e in self.run_history(run_id)].count("completed"), 1)
        self.assertEqual([r for r in self.ledger() if r.get("type") == "run_ended"][-1]["outcome"], "reported")
        self.assertEqual(len([r for r in self.ledger() if r.get("type") == "run_ended"]), 1)
        self.assertTrue(FLOW.wait_until(lambda: self.lifecycle.ended == [run_id]))
        time.sleep(0.3)
        self.assertEqual(self.notices_to_manager(), [])
        self.assertEqual(self.flow.notices, [])
        self.assertEqual(self.flow.worker_view(), {"state": "idle", "task_id": None})
        self.assertNotIn("runner_error", repr(self.ledger()))

    def test_error_while_the_report_was_being_delivered_is_an_unknown_report_never_resent(self):
        self.report = "error_before_acceptance"
        task_id = self.new_experiment()["task_id"]
        self.assertTrue(FLOW.wait_until(lambda: self.flow.task_view()["status"] == "closed", 10),
                        self.flow.task_view())
        view = self.flow.task_view()
        self.assertEqual(view["closed_reason"], "report_outcome_unknown")
        self.assertEqual(view["last_result"]["error"], "report_error:MailboxError")
        run_id = view["last_result"]["run_id"]
        self.assertIsNone(self.current_run(task_id))
        self.assertEqual([e["kind"] for e in self.run_history(run_id)].count("completed"), 1)
        self.assertEqual([n["kind"] for n in self.flow.notices], ["report_outcome_unknown"])
        self.assertEqual(self.run_kinds(), ["task", "question", "report"])
        self.assertEqual(self.run_mailbox.delivered.count(self.run_mailbox.created[-1].message_id), 1)


class BusyPeriodTests(FLOW.FlowFixture):
    """fix-05 P3a: the analysis idle limit measures one continuous busy period of the worker."""

    def open(self, *, start=True):
        self.exited = getattr(self, "exited", None) or threading.Event()
        self.worker_busy = getattr(self, "worker_busy", False)
        self.judged = getattr(self, "judged", None) or []
        self.defer_always = getattr(self, "defer_always", False)
        fixture = self

        class Run(FLOW.FakeRun):
            def collect(self, *, timeout=1, paused=False):
                if not fixture.exited.is_set():
                    fixture.worker_busy = True  # a review turn starts right at the exit
                    fixture.exited.set()
                return {"shell_state": "exited", "exit_confirmed": True}

            def judge(self):
                fixture.judged.append(time.monotonic())
                if fixture.defer_always or len(fixture.judged) == 1:
                    fixture.worker_busy = not fixture.defer_always  # the next turn began before delivery
                    self._record["worker_analysis_request"] = {"status": "deferred"}
                    raise WorkflowHeld("worker analysis delivery was not confirmed; no report or replay")
                return super().judge()

        class Workflow(FLOW.FakeWorkflow):
            def start(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell):
                run = super().start(task_id, revision, worktree_path=worktree_path,
                                    artifacts_root=artifacts_root, automation=automation, shell=shell)
                return Run(self.repository, run.run_id, self.gates)

        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=self.mailbox,
                                 paused=self.paused, retry_interval=0.01)
        ports = ExperimentPorts(host_shell=lambda: self.port,
                                make_workflow=lambda repository: Workflow(repository, self.runs, self.gates),
                                automation=lambda: FLOW.AUTOMATION, environment_names=lambda: {"PATH"},
                                worktrees_root=self.root, artifacts_root=self.root)
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl",
                        repository_factory=lambda: TaskRepository(self.db), handoffs=service,
                        omp_idle=lambda role: not (role is ActorRole.WORKER and fixture.worker_busy),
                        paused=self.paused, experiment=ports, lifecycle=Lifecycle(),
                        poll_interval=0.02, collect_slice=0.05, analysis_idle_limit=1.0)
        service.configure(policy=flow, active_task=flow.active_task)
        service.start()
        if start:
            flow.start()
        self.service, self.flow = service, flow
        self.controller.flow = flow
        self.services.append(service)
        self.flows.append(flow)
        return flow

    def test_an_idle_gap_starts_a_new_busy_period(self):
        self.new_experiment()
        self.assertTrue(self.exited.wait(5))
        time.sleep(0.7)  # busy 0.7 s (< 1.0 s)
        self.worker_busy = False
        self.assertTrue(FLOW.wait_until(lambda: len(self.judged) == 1))
        time.sleep(0.7)  # busy again 0.7 s: 1.4 s in total, but never 1.0 s in one go
        self.assertEqual(self.flow.task_view()["status"], "waiting_report", self.flow.task_view())
        self.worker_busy = False
        self.assertTrue(FLOW.wait_until(lambda: self.flow.task_view()["status"] == "finished", 5))
        last = self.flow.task_view()["last_result"]
        self.assertEqual((last["outcome"], last["judgment"]), ("reported", "success"), last)
        self.assertEqual(len(self.judged), 2)

    def test_deliveries_deferred_while_the_worker_looks_idle_still_end_at_the_limit(self):
        self.defer_always = True
        self.new_experiment()
        self.assertTrue(self.exited.wait(5))
        self.worker_busy = False
        self.assertTrue(FLOW.wait_until(lambda: self.flow.task_view()["status"] == "finished", 5),
                        self.flow.task_view())
        last = self.flow.task_view()["last_result"]
        self.assertEqual((last["outcome"], last["judgment"]), ("judgment_unavailable", "indeterminate"))
        self.assertTrue(last["reason"].startswith("worker_not_idle_for_analysis"), last)
        self.assertGreaterEqual(self.judged[-1] - self.judged[0], 0.9)


if __name__ == "__main__":
    unittest.main()
