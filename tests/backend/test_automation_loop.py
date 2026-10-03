"""CW-18 U3: the backend automation loop (lifecycle tick, 60 s review, pause/resume).

No OMP and no provider: a real G3 bridge with scripted in-process peers, the
real TaskRepository/TaskMailbox, a real host ShellPane running the experiment
through HostShellPort, the real TaskWorkflow/WorkflowRun and the real CW-11/12/15
parts behind ``AutomationController``. The review clock is injected.
"""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from workbench.app.lifecycle import LifecycleJournal
from workbench.backend.automation import AutomationController
from workbench.backend.flow import HandoffService
from workbench.backend.flow_tasks import ExperimentPorts, TaskFlow
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.backend.service import Backend
from workbench.backend.ui_server import Held
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import Reason
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer
from workbench.storage.log_raw import RawLogStore
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.workflow.run import TaskWorkflow

try:  # discover -s tests/backend, or python -m unittest tests.backend.<module>
    from test_task_flow import FakeMailbox, FakePort, FakeWorkflow, execution, request, wait_until
except ImportError:  # pragma: no cover
    from tests.backend.test_task_flow import (  # type: ignore[no-redef]
        FakeMailbox, FakePort, FakeWorkflow, execution, request, wait_until)

AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=15,
                          check=True).stdout.strip()


def git_repo(path: Path, name: str) -> str:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "fixture@example.invalid")
    git(path, "config", "user.name", "CW18 U3 Fixture")
    (path / name).write_text("base\n")
    git(path, "add", name)
    git(path, "commit", "-qm", "baseline")
    return git(path, "rev-parse", "HEAD")


class ScriptedPeer:
    """A bound G3 client answering probe/deliver/pause/resume like the bridge extension."""

    def __init__(self, bridge: G3BridgeServer, role: str, token: str):
        self.role, self.session, self.generation = role, str(uuid4()), 1
        self.idle, self.paused, self.abort_status = True, False, "none"
        self.busy_turn = role == "manager"  # the manager is in a turn when pause arrives
        self.confirm_stop = True
        self.deliveries: list[dict] = []
        self.frames: list[str] = []
        self._lock = threading.Lock()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(2)
        self.socket.connect(str(bridge.socket_path))
        self._send({"kind": "hello", "protocolVersion": 1, "role": role, "ompSessionId": self.session,
                    "generation": 1, "pid": os.getpid(), "token": token})
        assert json.loads(self.socket.recv(4096).split(b"\n")[0])["kind"] == "ready"
        self.socket.settimeout(None)
        self.thread = threading.Thread(target=self._serve, name=f"u3-peer-{role}", daemon=True)
        self.thread.start()

    def _send(self, frame):
        with self._lock:
            self.socket.sendall((json.dumps(frame) + "\n").encode())

    def state(self):
        return {"role": self.role, "sessionId": self.session, "generation": self.generation, "idle": self.idle,
                "pending": False, "approvalPending": False, "editorKnown": True, "editorEmpty": True,
                "inFlightToolCount": 0, "paused": self.paused, "abortStatus": self.abort_status,
                "unknownOutcomeToolCallIds": [], "unconfirmedToolCallIds": []}

    def _ack(self, request_id, **fields):
        self._send({"kind": "api_ack", "requestId": request_id, **fields})

    def _event(self, name, **fields):
        self._send({"kind": "omp_event", "name": name, "sessionId": self.session,
                    "generation": self.generation, **fields})

    def _serve(self):
        stream = self.socket.makefile("rb")
        try:
            for raw in stream:
                frame = json.loads(raw)
                kind, request_id = frame.get("kind"), frame.get("requestId")
                self.frames.append(kind)
                if kind == "probe":
                    self._ack(request_id, status="state", state=self.state())
                elif kind == "deliver":
                    envelope = json.loads(frame["envelope"])
                    self.deliveries.append(envelope)
                    self._ack(request_id, status="api_accepted")
                    self._event("delivery_omp_processed", messageId=envelope["messageId"],
                                deliveryAttemptId=envelope["deliveryAttemptId"], taskId=envelope["taskId"],
                                revisionId=envelope["revisionId"], runId=envelope["runId"],
                                providerRequestMatched=True, providerResponseObserved=True,
                                agentEndObserved=True)
                elif kind == "pause":
                    self.paused = True
                    if self.busy_turn:
                        self._ack(request_id, status="abort_requested", requestId=request_id)
                        if self.confirm_stop:
                            self.abort_status = "stop_observed"
                            self._event("turn_stop_observed", abortRequestId=request_id)
                        else:
                            self.abort_status = "requested"
                    else:
                        self._ack(request_id, status="paused", requestId=request_id)
                elif kind == "resume":
                    self.paused, self.abort_status = False, "none"
                    self._ack(request_id, status="resumed", requestId=request_id, state=self.state())
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
        self.thread.join(2)

    def reviews(self):
        return [d for d in self.deliveries if '"periodic_review"' in json.dumps(d)]


class PersistingMailbox:
    """A non-public test mailbox that persists messages exactly as TaskMailbox stores them."""

    def __init__(self, repository, bridge):
        self.repository, self.bridge = repository, bridge

    def create_message(self, task_id, revision, run_id, sender, target, kind, payload, *,
                       in_reply_to_message_id=None):
        target = ActorRole(target)
        peer = self.bridge.peer(target)
        message_id = str(uuid4())
        content = {"schema_version": 1, "sender_role": ActorRole(sender).value, "target_role": target.value,
                   "kind": MessageKind(kind).value, "task_id": task_id, "revision": revision,
                   "revision_id": str(uuid5(UUID(task_id), f"task-spec-revision:{revision}")), "run_id": run_id,
                   "target_session_id": peer.session_id, "target_session_generation": peer.generation,
                   "in_reply_to_message_id": in_reply_to_message_id, "payload": dict(payload)}
        self.repository.create_message(task_id, revision, run_id, content, message_id=message_id)
        return SimpleNamespace(message_id=message_id, task_id=task_id, revision=revision, run_id=run_id)

    def deliver(self, message, **_):
        from workbench.ipc.bridge_g3.mailbox import MailboxStatus
        return SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class AutomationFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="cw18-u3-", dir="/tmp")))
        self.project = self.root / "project"
        git_repo(self.project, "README")
        self.source = self.root / "source"
        self.commit = git_repo(self.source, "tracked.txt")
        for name in ("worktrees", "runs", "workflow"):
            (self.root / name).mkdir(mode=0o700)
        # The scripted peers run in this process: its cwd is the OMPs' project checkout.
        previous = os.getcwd()
        os.chdir(self.project)
        self.stack.callback(os.chdir, previous)
        self.db = self.root / "tasks.sqlite3"
        self.repository = TaskRepository(self.db)
        self.stack.callback(self.repository.close)
        tokens = {"manager": str(uuid4()), "worker": str(uuid4())}
        self.bridge = self.stack.enter_context(G3BridgeServer(self.root / "bridge.sock", tokens))
        self.manager = ScriptedPeer(self.bridge, "manager", tokens["manager"])
        self.stack.callback(self.manager.close)
        self.worker = ScriptedPeer(self.bridge, "worker", tokens["worker"])
        self.stack.callback(self.worker.close)
        self.pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "LANG": "C.UTF-8"})
        self.stack.callback(self.pane.close)
        self.stop = threading.Event()
        pump = threading.Thread(target=self._pump, daemon=True)
        pump.start()
        self.stack.callback(pump.join, 5)
        self.stack.callback(self.stop.set)
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        self.raw = RawLogStore(self.root / "raw")
        self.stack.callback(self.raw.close)
        self.journal = LifecycleJournal(self.root / "lifecycle.json")
        self.clock = Clock()
        self.logs: list[str] = []
        self.controller = AutomationController(
            bridge=self.bridge, database=self.db, journal=self.journal, raw=self.raw,
            shell_pane=lambda: self.pane, project_dir=self.project, artifacts_root=self.root / "runs",
            boot_marker=lambda: "boot", clock=self.clock, request_timeout=2.0, stop_timeout=1.0,
            review_timeout=5.0, log=self.logs.append)
        self.stack.callback(self.controller.close)

    def _pump(self):
        while not self.stop.is_set():
            self.pane.pump()
            time.sleep(0.01)

    def approve(self, spec, paths):
        task = self.repository.create_task(spec)
        self.repository.approve_scope(task, 1, {"paths": paths, **({"execution": spec["execution"]}
                                                                 if "execution" in spec else {})})
        self.repository.proceed(task, 1, "approved by the user")
        return task

    def start_experiment(self, command="printf HOST_STARTED; sleep 30; printf PASS > outcome.txt"):
        spec = {"goal": "u3 fixture run", "paths": ["outcome.txt"], "execution": {
            "source": str(self.source), "commit": self.commit, "command": command,
            "criteria": {"log_contains": "HOST_STARTED", "result_file": "outcome.txt", "result_contains": "PASS"},
            "environment": ["PATH"], "shell": "bash"}}
        task = self.approve(spec, ["outcome.txt"])
        workflow = TaskWorkflow(self.repository, PersistingMailbox(self.repository, self.bridge))
        port = HostShellPort(self.pane, lambda: self.pane)
        self.assertIsNone(port.hold("starting"))
        try:
            run = workflow.start(task, 1, worktree_path=self.root / "worktrees" / "w1",
                                 artifacts_root=self.root / "runs", automation=AUTOMATION, shell=port)
        finally:
            port.release_hold()
        self.stack.callback(port.detach)
        return run

    def tick(self, advance=0.0):
        self.clock.now += advance
        return self.controller.tick()

    def status(self):
        return self.controller.status()


class ExperimentLoopTests(AutomationFixture):
    def test_run_start_binds_lifecycle_and_the_tick_drives_60s_reviews_with_busy_merge(self):
        run = self.start_experiment()
        self.assertIsNone(self.journal.read(), "no lifecycle record before the run is bound")
        self.controller.experiment_started(run)
        record = self.journal.read()
        self.assertIsNotNone(record, "run start writes the first LifecycleRecord")
        self.assertEqual((record.task_id, record.revision, record.run_id, record.generation),
                         (run.task_id, run.revision, run.run_id, 1))
        self.assertEqual((record.manager.session_id, record.worker.session_id),
                         (self.manager.session, self.worker.session))
        self.assertEqual(record.shell.pid, self.pane.pid)
        self.assertEqual(record.approval_hash, run.approval_hash)
        status = self.status()
        self.assertEqual(set(status), {"state", "source", "detail", "paused", "transition", "run", "tick", "review",
                                       "interruption", "resume", "retry_limit", "persistence_error"})
        self.assertEqual(set(status["review"]), {"applies", "interval_seconds", "status", "reason", "review_count",
                                                 "last_review_at", "next_due_in_seconds", "pending",
                                                 "coalesced_count", "exit"})
        self.assertEqual(status["retry_limit"], 3)
        self.assertIn(status["state"], ui_v1.AUTOMATION_STATES)
        self.assertEqual(status["run"]["run_id"], run.run_id)
        self.assertTrue(status["run"]["bound"], status)

        first = self.tick()
        self.assertEqual(first["outcome"], "admitted", (first, self.logs))
        review = self.status()["review"]
        self.assertTrue(review["applies"])
        self.assertEqual((review["status"], review["interval_seconds"]), ("waiting", 60))
        self.assertEqual(review["next_due_in_seconds"], 60.0)
        self.tick(59.0)
        self.assertEqual(self.worker.reviews(), [], "nothing before 60 s")

        self.tick(1.0)
        self.assertEqual(self.status()["review"]["status"], "dispatched", self.status())
        self.assertEqual(len(self.worker.reviews()), 1)
        self.assertEqual(self.status()["review"]["review_count"], 1)
        self.assertEqual(self.status()["state"], "active")

        # Busy worker: the due review waits, later dues merge into the one pending review.
        self.worker.idle = False
        self.tick(60.0)
        review = self.status()["review"]
        self.assertEqual((review["status"], review["reason"], review["pending"]),
                         ("delayed", "worker_busy_or_unknown", True))
        self.tick(60.0)
        self.tick(60.0)
        review = self.status()["review"]
        self.assertEqual(review["coalesced_count"], 2, review)
        self.assertEqual(len(self.worker.reviews()), 1, "a busy worker gets no review")
        self.worker.idle = True
        self.tick(1.0)
        self.assertEqual(self.status()["review"]["status"], "dispatched")
        reviews = self.worker.reviews()
        self.assertEqual(len(reviews), 2, "the merged reviews are one delivery")
        self.assertEqual(self.status()["review"]["coalesced_count"], 0)

        # The run's close ends its automation; a later run is the journal's next generation.
        self.controller.run_ended(run.run_id)
        self.assertEqual(self.status()["state"], "idle")
        self.assertEqual(self.tick(60.0)["outcome"], "idle")
        self.assertEqual(len(self.worker.reviews()), 2)

    def test_pause_holds_reviews_collection_continues_and_resume_needs_reconciliation(self):
        run = self.start_experiment()
        self.controller.experiment_started(run)
        self.assertEqual(self.tick()["outcome"], "admitted")
        paused = self.controller.request_pause()
        self.assertTrue(paused["paused"], "the pause holds new work at once")
        self.assertTrue(self.controller.paused())
        self.assertTrue(self.controller.wait_idle(15))
        status = self.status()
        self.assertEqual(status["state"], "paused", status)
        interruption = status["interruption"]
        self.assertEqual(interruption["state"], "confirmed", interruption)
        self.assertEqual(interruption["manager_ack"], "abort_requested")
        self.assertIsNotNone(interruption["requested_at"])
        self.assertIsNotNone(interruption["confirmed_at"])
        self.assertIn("pause", self.manager.frames)
        self.assertIn("pause", self.worker.frames)

        # Paused: a due review is not dispatched, the tick is held.
        held = self.tick(120.0)
        self.assertEqual(held["outcome"], "held")
        self.assertIn("pause_or_cancel_active_or_unknown", held["problems"])
        self.assertEqual(self.worker.reviews(), [])
        # Host collection is not paused.
        collected = run.collect(timeout=0.3, paused=self.controller.paused())
        self.assertEqual((collected["shell_state"], collected["paused_at_collection"]), ("running", True))

        # The user's resume is reconciled against live facts before anything restarts.
        resumed = self.controller.request_resume()
        self.assertTrue(resumed["paused"])
        self.assertTrue(self.controller.wait_idle(20))
        status = self.status()
        self.assertEqual(status["resume"]["outcome"], "resumed", (status, self.logs))
        self.assertFalse(status["paused"])
        self.assertIn("resume", self.manager.frames)
        self.assertEqual(self.tick(1.0)["outcome"], "admitted")
        self.assertEqual(len(self.worker.reviews()), 1, "the review held over the pause runs once after it")

    def test_a_resume_that_does_not_reconcile_stays_paused_and_replays_nothing(self):
        run = self.start_experiment()
        self.controller.experiment_started(run)
        self.tick()
        self.manager.confirm_stop = False  # the manager turn stop is never observed
        self.controller.request_pause()
        self.assertTrue(self.controller.wait_idle(15))
        self.assertEqual(self.status()["interruption"]["state"], "requested")
        self.controller.request_resume()
        self.assertTrue(self.controller.wait_idle(20))
        status = self.status()
        self.assertEqual(status["resume"]["outcome"], "refused", status)
        self.assertTrue(status["paused"])
        self.assertTrue(self.controller.paused())
        self.assertNotIn("resume", self.manager.frames)
        self.assertEqual(self.tick(120.0)["outcome"], "held")
        self.assertEqual(self.worker.reviews(), [])


class FreeWorkLoopTests(AutomationFixture):
    def test_free_work_run_binds_holds_review_and_pauses_and_resumes(self):
        task = self.approve({"goal": "clean parser", "paths": ["src/"]}, ["src/"])
        run_id = self.repository.start_run(task, 1, inputs={"kind": "work"})
        message = PersistingMailbox(self.repository, self.bridge).create_message(
            task, 1, run_id, ActorRole.MANAGER, ActorRole.WORKER, MessageKind.TASK, {"handoff": "to_worker"})
        self.controller.work_started(task, 1, run_id, message.message_id)
        status = self.status()
        self.assertEqual((status["run"]["kind"], status["run"]["bound"]), ("work", True), (status, self.logs))
        self.assertEqual(self.journal.read().run_id, run_id)
        self.assertFalse(status["review"]["applies"])
        tick = self.tick(120.0)
        self.assertEqual(tick["outcome"], "held")
        self.assertIn("user_owner_or_control_hold", tick["problems"])
        self.assertEqual(self.status()["state"], "active", "free work has no host run to review")
        self.assertEqual(self.worker.reviews(), [])

        self.controller.request_pause()
        self.assertTrue(self.controller.wait_idle(15))
        self.assertEqual(self.status()["interruption"]["state"], "confirmed")
        self.controller.request_resume()
        self.assertTrue(self.controller.wait_idle(20))
        self.assertEqual(self.status()["resume"]["outcome"], "resumed", (self.status(), self.logs))
        self.assertFalse(self.controller.paused())

        # A second run is the journal's next generation; the first run's close unbinds it.
        self.repository.complete_run(run_id, {"done": True})
        self.controller.run_ended(run_id)
        self.assertEqual(self.status()["state"], "idle")
        self.repository.proceed(task, 1, "again")
        second = self.repository.start_run(task, 1, inputs={"kind": "work"})
        message = PersistingMailbox(self.repository, self.bridge).create_message(
            task, 1, second, ActorRole.MANAGER, ActorRole.WORKER, MessageKind.TASK, {"handoff": "to_worker"})
        self.controller.work_started(task, 1, second, message.message_id)
        record = self.journal.read()
        self.assertEqual((record.run_id, record.generation), (second, 2))


class PauseWithoutRunTests(unittest.TestCase):
    def test_pause_without_a_run_holds_new_work_and_resume_clears_it(self):
        with tempfile.TemporaryDirectory(prefix="cw18-u3-norun-", dir="/tmp") as tmp:
            root = Path(tmp)
            tokens = {"manager": "m", "worker": "w"}
            with G3BridgeServer(root / "bridge.sock", tokens) as bridge:
                raw = RawLogStore(root / "raw")
                controller = AutomationController(
                    bridge=bridge, database=root / "tasks.sqlite3", journal=LifecycleJournal(root / "l.json"),
                    raw=raw, shell_pane=lambda: None, project_dir=root, artifacts_root=root,
                    boot_marker=lambda: "boot", clock=lambda: 0.0)
                try:
                    self.assertEqual(controller.status()["state"], "idle")
                    self.assertIn(controller.status()["state"], ui_v1.AUTOMATION_STATES)
                    controller.request_pause()
                    self.assertTrue(controller.paused())
                    self.assertTrue(controller.wait_idle(5))
                    status = controller.status()
                    self.assertEqual((status["state"], status["interruption"]["state"]), ("paused", "not_needed"))
                    self.assertIn(status["interruption"]["state"], ui_v1.INTERRUPTION_STATES)
                    self.assertEqual(controller.tick()["outcome"], "idle")
                    controller.request_resume()
                    self.assertTrue(controller.wait_idle(5))
                    self.assertFalse(controller.paused())
                    self.assertEqual(controller.status()["resume"]["outcome"], "resumed")
                finally:
                    controller.close()
                    raw.close()


class BackendHookTests(unittest.TestCase):
    """Backend's ui_v1 pause/resume and automation state go to the controller (not_configured is gone)."""

    class Stub:
        pause = Backend.pause
        resume = Backend.resume
        _automation_paused = Backend._automation_paused
        _automation_view = Backend._automation_view

        def __init__(self, loop):
            self.automation = {"state": "not_configured"}
            self.automation_loop = loop
            self.pause_hook, self.resume_hook = loop.request_pause, loop.request_resume

    def test_pause_resume_and_the_published_state_use_the_controller(self):
        loop = SimpleNamespace(flag=False)
        loop.paused = lambda: loop.flag
        loop.status = lambda: {"state": "paused" if loop.flag else "idle", "paused": loop.flag}

        def pause():
            loop.flag = True
            return loop.status()

        def resume():
            loop.flag = False
            return loop.status()

        loop.request_pause, loop.request_resume = pause, resume
        backend = self.Stub(loop)
        self.assertEqual(backend._automation_view()["state"], "idle")
        self.assertEqual(backend.pause()["automation"]["state"], "paused")
        self.assertTrue(backend._automation_paused())
        with self.assertRaises(Held) as caught:
            backend.resume(False)
        self.assertEqual(caught.exception.reason, Reason.RESUME_NOT_RECONCILED)
        self.assertTrue(backend._automation_paused(), "an unreconciled resume changes nothing")
        self.assertEqual(backend.resume(True)["automation"]["state"], "idle")
        self.assertFalse(backend._automation_paused())


class TaskFlowHookTests(unittest.TestCase):
    """TaskFlow tells the automation controller about run starts and closes (outside its lock)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cw18-u3-hooks-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "workflow").mkdir(mode=0o700)
        self.db = self.root / "tasks.sqlite3"
        self.events: list[tuple] = []
        self.runs: list[dict] = []
        self.mailbox = FakeMailbox()
        hook = SimpleNamespace(
            experiment_started=lambda run: self.events.append(("experiment_started", run.run_id)),
            work_started=lambda *args: self.events.append(("work_started", *args)),
            run_ended=lambda run_id: self.events.append(("run_ended", run_id)))
        self.service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=self.mailbox,
                                      retry_interval=0.01)
        ports = ExperimentPorts(host_shell=FakePort, make_workflow=lambda r: FakeWorkflow(r, self.runs),
                                automation=lambda: AUTOMATION, environment_names=lambda: {"PATH"},
                                worktrees_root=self.root, artifacts_root=self.root)
        self.flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl",
                             repository_factory=lambda: TaskRepository(self.db), handoffs=self.service,
                             omp_idle=lambda role: True, experiment=ports, lifecycle=hook,
                             poll_interval=0.02, collect_slice=0.05)
        self.service.configure(policy=self.flow, active_task=self.flow.active_task)
        self.service.start()
        self.flow.start()
        self.addCleanup(self.service.close)
        self.addCleanup(self.flow.close)

    def test_experiment_and_work_runs_report_start_and_close(self):
        pending = self.service.handle(ActorRole.MANAGER, request("to_worker", {
            "kind": "experiment", "message": "run it",
            "spec": {"goal": "g", "paths": ["src/"], "execution": execution()}}, "m-1"))
        self.assertEqual(pending["status"], "dispatched")  # C-D66: no UI approval
        self.assertTrue(wait_until(lambda: len(self.events) >= 2))
        run_id = self.runs[0]["run_id"]
        self.assertEqual(self.events[:2], [("experiment_started", run_id), ("run_ended", run_id)])

        work = self.service.handle(ActorRole.MANAGER, request("to_worker", {
            "kind": "work", "message": "refactor", "spec": {"goal": "w", "paths": ["src/"]}}, "m-2"))
        self.assertEqual(work["status"], "dispatched")
        self.assertTrue(wait_until(lambda: any(e[0] == "work_started" for e in self.events)))
        started = next(e for e in self.events if e[0] == "work_started")
        task = self.flow.task_view()
        self.assertEqual(started[1:], (work["task_id"], 1, task["run_id"], self.flow.tasks[work["task_id"]]
                                       .task_message_id))
        done = self.service.handle(ActorRole.WORKER, request("to_manager", {"kind": "done", "message": "ok"}, "w-1"))
        self.assertEqual(done["status"], "queued")
        self.assertTrue(wait_until(lambda: ("run_ended", started[3]) in self.events))


if __name__ == "__main__":
    unittest.main()
