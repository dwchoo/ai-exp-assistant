"""p27-cw19-fix-01: corrections from review-01, vm-01 and test-01 (red on the reviewed candidate, green here).

- P2-1: a worker ``to_manager`` answered ``queued`` waits out a CW-19 manager hold and is delivered once; one that
  still ends unsent (e.g. dropped by a pause, C-D65) is listed in ``workbench_status``.
- P2-2: the hold check of a tool call applies the ``model_turn_result`` received before it (the turn that lifts
  the hold may call ``terminal``).
- P3-1: a ``stop_unconfirmed`` survivor is re-observed by exact identity and can be retried.
- P3-4: the lost-outbox listing streams the whole handoff journal (records past 64 MiB are seen).
- P3-5 / VM F2: reconcile_failed has its own text; the boot wait leads the status line.
- VM F1: the backend_restarted notice is built when sent and dropped for a Task closed meanwhile.
- Test P3-2: a pause that cannot be stored is shown (persistence_error + metadata fault) and retried.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from uuid import uuid4

from support import Owned, ticks

from test_backend_restart import BackendFixture

from workbench.app.lifecycle import LifecycleJournal
from workbench.app.recovery import (
    BOOT_HOLD, AdmissionHold, Survivor, SurvivorRegistry, iter_jsonl, lost_outbox, model_hold, read_jsonl,
)
from workbench.backend import cli
from workbench.backend.automation import AutomationController
from workbench.backend.flow import HandoffService, OutboundMessage
from workbench.backend.flow_recovery import Watchdog, WatchPorts
from workbench.backend.launcher import LaunchPlan
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.service import Backend
from workbench.contracts.v1 import ActorRole, MessageKind, PaneId
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.ui.product.model import BOOT_RECONCILE_FAILED_TEXT, BOOT_WAIT_TEXT, ProductModel

BOOT = "cccccccc-3333-4333-8333-cccccccccccc"
SKILL = Path(__file__).resolve().parents[2] / "omp_bridge" / "skills" / "to-worker" / "SKILL.md"


class Temp(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw19-fix01-", dir="/tmp"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)


class _Mailbox:
    def __init__(self):
        self.delivered: list[str] = []

    def create_message(self, task_id, revision, run_id, sender_role, target_role, kind, payload, *,
                       in_reply_to_message_id=None):
        return SimpleNamespace(message_id=str(uuid4()), session_id="s", session_generation=1, task_id=task_id,
                               target_role=ActorRole(target_role))

    def deliver(self, message, *, timeout=20):
        self.delivered.append(message.message_id)
        return DeliveryReceipt(message.message_id, None, message.target_role, "s", 1, MailboxStatus.API_RETURNED,
                               {})


class Sender:
    """A ProductModel sender that only records."""

    def __getattr__(self, name):
        return lambda *a, **k: None


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- P2-1 ------------------------------------------------------------------------------------------------------------
class WorkerToManagerWaits(Temp):
    def service(self, *, hold=lambda role: None, paused=lambda: False):
        mailbox = _Mailbox()
        service = HandoffService(self.root / "handoffs.jsonl", mailbox=mailbox, retry_interval=0.05, hold=hold,
                                 paused=paused)
        service.start()
        self.addCleanup(service.close)
        return service, mailbox

    @staticmethod
    def answer():
        return OutboundMessage(str(uuid4()), 1, str(uuid4()), ActorRole.WORKER, ActorRole.MANAGER,
                               MessageKind.ANSWER, {"handoff": "to_manager", "kind": "answer", "message": "m"})

    def queue_worker_call(self, service, outbound):
        key = (ActorRole.WORKER.value, str(uuid4()), 1, f"call-{uuid4()}")  # a worker's to_manager call
        return service._queue(key, outbound)

    def test_a_worker_answer_waits_out_a_manager_model_hold_and_is_delivered_once(self):
        reason = {"value": model_hold("manager")}
        service, mailbox = self.service(hold=lambda role: reason["value"] if role is ActorRole.MANAGER else None)
        result = self.queue_worker_call(service, self.answer())
        self.assertEqual(result["status"], "queued")
        time.sleep(0.4)
        self.assertEqual(mailbox.delivered, [], "nothing reaches a held manager")
        states = [entry["state"] for entry in service.report_entries()]
        self.assertEqual(states, ["pending"], "the queued answer is kept, not dropped as held_paused")
        reason["value"] = None
        self.assertTrue(wait_until(lambda: mailbox.delivered))
        time.sleep(0.3)
        self.assertEqual(len(mailbox.delivered), 1, "delivered once (never submitted while held: no replay)")

    def test_boot_and_metadata_holds_keep_it_too(self):
        for hold in (BOOT_HOLD, "metadata_unavailable"):
            with self.subTest(hold):
                reason = {"value": hold}
                service, mailbox = self.service(hold=lambda role: reason["value"])
                self.queue_worker_call(service, self.answer())
                time.sleep(0.3)
                self.assertEqual(mailbox.delivered, [])
                reason["value"] = None
                self.assertTrue(wait_until(lambda: len(mailbox.delivered) == 1))

    def test_a_pause_still_drops_it_and_it_is_listed(self):
        """C-D65 pause semantics are unchanged (independent p27w test): the drop is shown, not hidden."""
        paused = {"value": True}
        service, mailbox = self.service(paused=lambda: paused["value"])
        self.queue_worker_call(service, self.answer())
        self.assertTrue(wait_until(lambda: [e["state"] for e in service.report_entries()] == ["held_paused"]))
        self.assertEqual(service.report_entries()[0]["origin"], "worker")  # listed as not_sent by workbench_status
        paused["value"] = False
        time.sleep(0.3)
        self.assertEqual(mailbox.delivered, [])

    def test_a_backend_message_without_keep_is_still_dropped_under_a_hold(self):
        service, mailbox = self.service(hold=lambda role: BOOT_HOLD)
        service.enqueue(self.answer(), origin="test-plain")
        self.assertTrue(wait_until(lambda: [e["state"] for e in service.report_entries()] == ["held_paused"]))
        self.assertEqual(mailbox.delivered, [])


class StatusListsUnsentWorkerMessages(Temp):
    def test_a_worker_message_that_ended_unsent_is_listed_not_hidden(self):
        layout = DataLayout(ensure_private_dir(self.root / "data"))
        ensure_private_dir(layout.workflow)
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/nonexistent/omp", "omp/0", "/x/bridge.ts", ())
        backend = Backend(layout, plan, project_dir=str(self.root), environment={"PATH": "/usr/bin:/bin"},
                          boot_source=lambda: BOOT)
        backend._journal = lambda record: None
        entry = {"task_id": "t", "message_id": None, "report_kind": "answer", "state": "rejected",
                 "reason": "target_session_changed", "blockers": [], "requeued": 3, "text": "the answer",
                 "submitted": False}
        backend.handoffs = SimpleNamespace(report_entries=lambda: [
            {**entry, "origin": "worker"}, {**entry, "origin": "backend", "text": "a notice"},
            {**entry, "origin": "worker", "state": "delivered", "text": "delivered"}])
        result = backend._workbench_status(SimpleNamespace(args={}), {})
        self.assertEqual([(item["origin"], item["state"], item["text"]) for item in result["reports"]],
                         [("worker", "not_sent", "the answer")])


# -- P2-2 ------------------------------------------------------------------------------------------------------------
class ModelHoldSyncUnit(Temp):
    def test_the_hold_check_applies_events_received_before_it(self):
        layout = DataLayout(ensure_private_dir(self.root / "data"))
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/nonexistent/omp", "omp/0", "/x/bridge.ts", ())
        backend = Backend(layout, plan, project_dir=str(self.root), environment={"PATH": "/usr/bin:/bin"},
                          boot_source=lambda: BOOT)
        events = [{"name": "model_turn_result", "role": "worker", "ok": False, "stopReason": "error",
                   "bridgeSequence": 1}]
        backend.bridge = SimpleNamespace(events_after=lambda names, after: (
            max([after] + [e["bridgeSequence"] for e in events]), [e for e in events if e["bridgeSequence"] > after]))
        changed: list = []
        backend.automation_loop = SimpleNamespace(model_changed=changed.append, metadata=None)
        backend._poll_model_events()
        self.assertEqual(backend._hold_reason(ActorRole.WORKER), model_hold("worker"))
        self.assertEqual(changed, [False])
        events.append({"name": "model_turn_result", "role": "worker", "ok": True, "stopReason": "toolUse",
                       "bridgeSequence": 2})
        # no loop poll in between: the tool call of the same turn is checked right away
        self.assertIsNone(backend._hold_reason(ActorRole.WORKER))
        self.assertEqual(changed, [False], "the run record side effect runs on the loop, not in the hold check")
        backend._poll_model_events()
        self.assertEqual(changed, [False, True])
        backend._poll_model_events()
        self.assertEqual(changed, [False, True], "applied once")


class ModelHoldLiftingTurnCallsTerminal(BackendFixture):
    def setUp(self):
        super().setUp()
        self.frames = self.root / "frames.jsonl"
        self.env["FAKE_FRAMES"] = str(self.frames)

    def tool_results(self):
        if not self.frames.exists():
            return []
        return [item["frame"] for item in map(json.loads, self.frames.read_text().splitlines())
                if item["frame"].get("kind") == "tool_result"]

    def test_the_turn_that_lifts_the_hold_may_call_terminal(self):
        backend = self.start()
        worker = backend.panes[PaneId.WORKER_OMP]
        worker.admit(b"model-error\n")
        self.wait(lambda: backend.hold.reason_for(ActorRole.WORKER) == model_hold("worker"), "the model hold")
        # One healthy turn: its message_end event, then its tool call on the same socket. No loop poll between.
        worker.admit(b"model-ok\n")
        worker.admit(b'tool terminal {"command": "true"}\n')
        deadline = time.monotonic() + 1.5
        while not self.tool_results() and time.monotonic() < deadline:
            time.sleep(0.05)  # the request is decided on the bridge thread; the loop is not ticked here
        deadline = time.monotonic() + 30
        while not self.tool_results() and time.monotonic() < deadline:
            self.backend._tick(0.02)  # a started command finishes through the loop; the hold decision was made
        results = self.tool_results()
        self.assertEqual(len(results), 1, results)
        result = results[0].get("result") or results[0]
        self.assertNotEqual(result.get("reason"), model_hold("worker"),
                            f"the lifting turn's own terminal call was refused as held: {result}")


# -- P3-1 ------------------------------------------------------------------------------------------------------------
class UnconfirmedSurvivor(unittest.TestCase):
    def setUp(self):
        self.owned = Owned()
        self.addCleanup(self.owned.cleanup)

    def survivor(self, pid, start, *, pending):
        return Survivor("s1", "host_shell", pid, start, BOOT, True, state="stop_unconfirmed",
                        stop={"reason": "r", "requester": "manager", "outcome": "stop_unconfirmed",
                              "signalled": [pid], "remaining": [pid]}, pending=pending)

    def test_an_ended_identity_leaves_the_list(self):
        child = self.owned.spawn("exit 0")
        start = ticks(child.pid) or 1
        child.wait(5)
        registry = SurvivorRegistry([self.survivor(child.pid, start, pending=[(child.pid, start)])], lambda: BOOT)
        self.assertEqual(registry.alive(), [])
        self.assertEqual(registry.views()[0]["state"], "stopped")
        self.assertEqual(registry.carried(), [], "not carried into the next record")

    def test_a_live_one_stays_listed_and_can_be_retried_exactly(self):
        child = self.owned.spawn("trap '' TERM HUP; while :; do sleep 0.1; done")
        start = ticks(child.pid)
        registry = SurvivorRegistry([self.survivor(child.pid, start, pending=[(child.pid, start)])], lambda: BOOT,
                                    grace=0.2, kill_wait=2.0)
        alive = registry.alive()
        self.assertEqual(len(alive), 1)
        self.assertTrue(alive[0]["stoppable"], "the manager may retry the stop")
        result = registry.stop("s1", reason="retry", requester="manager")
        self.assertEqual(result["status"], "stopped", result)
        child.wait(5)
        self.assertEqual(result["signalled"], [child.pid])
        self.assertEqual(registry.alive(), [])

    def test_a_retry_never_signals_a_recycled_pid(self):
        child = self.owned.spawn("exec sleep 30")
        start = ticks(child.pid)
        registry = SurvivorRegistry([self.survivor(child.pid, start, pending=[(child.pid, start + 1)])],
                                    lambda: BOOT, grace=0.1, kill_wait=0.1)
        result = registry.stop("s1", reason="retry", requester="manager")
        self.assertEqual(result["status"], "refused", result)  # the pending identity ended: nothing to signal
        self.assertIsNone(child.poll(), "the process with another start time was not signalled")

    def test_a_retry_needs_the_same_boot(self):
        child = self.owned.spawn("exec sleep 30")
        start = ticks(child.pid)
        registry = SurvivorRegistry([self.survivor(child.pid, start, pending=[(child.pid, start)])],
                                    lambda: "dddddddd-4444-4444-8444-dddddddddddd")
        result = registry.stop("s1", reason="retry", requester="manager")
        self.assertEqual((result["status"], result["reason"]), ("refused", "boot_changed"))
        self.assertIsNone(child.poll())


# -- P3-4 ------------------------------------------------------------------------------------------------------------
class LargeJournal(Temp):
    def test_records_past_64_mib_are_read(self):
        path = self.root / "handoffs.jsonl"
        filler = json.dumps({"type": "request", "pad": "x" * 4000}) + "\n"
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "backend_start"}) + "\n")
            stream.write(json.dumps({"type": "outbox", "handoff_id": "old", "state": "pending"}) + "\n")
            for _ in range(70 * 1024 * 1024 // len(filler) + 1):
                stream.write(filler)
            stream.write(json.dumps({"type": "backend_start"}) + "\n")
            stream.write(json.dumps({"type": "outbox", "handoff_id": "new", "target_role": "manager",
                                     "kind": "report", "state": "pending"}) + "\n")
            stream.write(json.dumps({"type": "outbox", "handoff_id": "new", "state": "submitted"}) + "\n")
        self.assertGreater(path.stat().st_size, 64 * 1024 * 1024)
        lost = lost_outbox(iter_jsonl(path))
        self.assertEqual([(item["handoff_id"], item["state"]) for item in lost],
                         [("new", "submitted_outcome_unknown")])
        tail = read_jsonl(path)
        self.assertEqual(tail[-1]["handoff_id"], "new", "the bounded read keeps the most recent records")

    def test_terminal_entries_are_forgotten_and_an_overlong_line_skipped(self):
        path = self.root / "handoffs.jsonl"
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "outbox", "handoff_id": "a", "state": "pending"}) + "\n")
            stream.write("{" + "y" * (2 * 1024 * 1024) + "\n")
            stream.write(json.dumps({"type": "outbox", "handoff_id": "a", "state": "delivered"}) + "\n")
            stream.write(json.dumps({"type": "outbox", "handoff_id": "b", "state": "pending"}) + "\n")
        self.assertEqual([item["handoff_id"] for item in lost_outbox(iter_jsonl(path))], ["b"])


# -- P3-5 / VM F2 ----------------------------------------------------------------------------------------------------
class RecoveryTexts(unittest.TestCase):
    def model(self, state):
        model = ProductModel(Sender(), 40, 160, clock=lambda: 1_000_000.0)
        model.state = state
        return model

    def test_reconcile_failed_is_not_shown_as_a_reboot(self):
        state = {"boot": {"confirmation_required": True, "reason": "reconcile_failed"}}
        text = self.model(state).backend_recovery_text()
        self.assertIn(BOOT_RECONCILE_FAILED_TEXT, text)
        self.assertNotIn("재부팅", text)
        stream = io.StringIO()
        cli._print_recovery(state, stream)
        self.assertIn("재시작 대조 실패", stream.getvalue())
        self.assertNotIn("부팅 확인 대기 (reconcile_failed)", stream.getvalue())

    def test_the_boot_wait_leads_the_status_line_before_an_isolation_warning(self):
        state = {"boot": {"confirmation_required": True, "reason": "reboot"},
                 "omp_isolation": {"checked": True, "state": "failed", "warning": "w" * 120}}
        line = self.model(state).status_lines()[1]
        self.assertTrue(line.startswith(BOOT_WAIT_TEXT), line[:120])
        self.assertEqual(line.count("confirm-boot"), 1, "shown once")
        self.assertLess(len(BOOT_WAIT_TEXT), 160)

    def test_the_to_worker_skill_names_the_new_held_reasons(self):
        text = SKILL.read_text()
        for reason in ("held:boot_confirmation_required", "held:metadata_unavailable", "held:model_hold:worker",
                       "held:shutdown_closing"):
            self.assertIn(reason, text)
        self.assertIn("confirm-boot", text)
        self.assertNotIn("wait for the worker's report or cancel the Task", text)


# -- VM F1 -----------------------------------------------------------------------------------------------------------
class RestartNoticeBuiltWhenSent(unittest.TestCase):
    def watchdog(self, hold, sent):
        def notify(role, notice):
            if hold.reason_for(role) is not None:
                return "paused"
            sent.append(notice)
            return "delivered"
        now = [100.0]
        records: list = []
        watchdog = Watchdog(WatchPorts(task=lambda: None, notify=notify, journal=records.append),
                            clock=lambda: now[0], notice_retry=1.0)
        return watchdog, now, records

    def test_a_task_cancelled_before_the_send_gets_no_notice(self):
        hold, sent = AdmissionHold(), []
        hold.set(BOOT_HOLD)
        watchdog, now, records = self.watchdog(hold, sent)
        task = {"status": "running"}
        watchdog.enqueue_notice(ActorRole.MANAGER, "backend_restarted", {"task_id": "t", "task": dict(task)},
                                build=lambda: None if task["status"] == "closed" else {"task_id": "t",
                                                                                       "task": dict(task)})
        watchdog.tick()
        task["status"] = "closed"  # the user cancels before confirm-boot
        hold.clear(BOOT_HOLD)
        now[0] += 2
        watchdog.tick()
        self.assertEqual(sent, [])
        self.assertEqual(watchdog.queued(), [])
        self.assertIn("dropped_not_applicable", [r.get("outcome") for r in records])

    def test_the_content_is_the_state_at_the_send(self):
        hold, sent = AdmissionHold(), []
        hold.set(BOOT_HOLD)
        watchdog, now, _ = self.watchdog(hold, sent)
        task = {"status": "held"}
        notice_id = watchdog.enqueue_notice(ActorRole.MANAGER, "backend_restarted", {"task": dict(task)},
                                            build=lambda: {"task": dict(task)})
        watchdog.tick()
        task["status"] = "running"
        hold.clear(BOOT_HOLD)
        now[0] += 2
        watchdog.tick()
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]["notice_id"], sent[0]["type"], sent[0]["task"]["status"]),
                         (notice_id, "backend_restarted", "running"))

    def test_the_backend_builder_follows_the_task(self):
        layout = DataLayout(ensure_private_dir(Path(tempfile.mkdtemp(prefix="cw19-fix01-n-", dir="/tmp"))))
        self.addCleanup(shutil.rmtree, layout.root, True)
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/nonexistent/omp", "omp/0", "/x/bridge.ts", ())
        backend = Backend(layout, plan, project_dir=str(layout.root), environment={"PATH": "/usr/bin:/bin"},
                          boot_source=lambda: BOOT)
        backend.startup = {"classification": "reboot", "run": None, "outbox_lost_count": 0}
        view = {"task_id": "t1", "status": "held", "held_reason": "backend_restarted"}
        backend.flow = SimpleNamespace(task_view=lambda: dict(view))
        built: dict = {}
        backend.watchdog = SimpleNamespace(enqueue_notice=lambda role, kind, fields, build=None: built.update(
            fields=fields, build=build) or "n-1")
        backend._queue_restart_notice()
        self.assertEqual(built["fields"]["task"]["status"], "held")
        view["status"] = "running"
        self.assertEqual(built["build"]()["task"]["status"], "running")
        view.update(task_id="t2")  # another Task is open now: the notice was about t1
        self.assertIsNone(built["build"]())
        view.update(task_id="t1", status="closed")
        self.assertIsNone(built["build"]())
        self.assertEqual(backend._restart_notice["state"], "dropped_task_closed")


# -- test P3-2 -------------------------------------------------------------------------------------------------------
class PauseStoreFault(Temp):
    def test_a_failed_pause_store_is_a_fault_and_is_retried(self):
        layout = DataLayout(ensure_private_dir(self.root / "data"))
        ensure_private_dir(layout.workflow)
        works = {"value": False}
        saved: list = []

        def save(paused):
            if works["value"]:
                saved.append(paused)
                return True
            return False
        events: list = []
        clock = [0.0]
        controller = AutomationController(
            bridge=None, database=layout.tasks, journal=LifecycleJournal(layout.lifecycle_journal), raw=None,
            shell_pane=lambda: None, project_dir=self.root, artifacts_root=ensure_private_dir(layout.workflow / "runs"),
            boot_marker=lambda: BOOT, pause_store=SimpleNamespace(load=lambda: False, save=save),
            clock=lambda: clock[0], metadata=lambda source, error: events.append((source, error is None)))
        self.addCleanup(controller.close)
        status = controller.request_pause()
        self.assertTrue(controller.paused(), "the in-memory pause stays")
        self.assertEqual(status["persistence_error"], "pause_not_stored")
        self.assertIn("not stored durably", status["detail"])
        self.assertEqual(events, [("automation_pause", False)], "reported as a metadata fault")
        controller.retry_pause_store()  # not due yet
        self.assertEqual(saved, [])
        works["value"] = True
        clock[0] += 10
        controller.retry_pause_store()
        self.assertEqual(saved, [True])
        self.assertIsNone(controller.status()["persistence_error"])
        self.assertEqual(events[-1], ("automation_pause", True), "the fault clears once stored")
        self.assertTrue(controller.paused())

    def test_another_durable_write_does_not_hide_an_unstored_pause(self):
        layout = DataLayout(ensure_private_dir(self.root / "data"))
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/nonexistent/omp", "omp/0", "/x/bridge.ts", ())
        backend = Backend(layout, plan, project_dir=str(self.root), environment={"PATH": "/usr/bin:/bin"},
                          boot_source=lambda: BOOT)
        backend._metadata_event("automation_pause", OSError("automation.json not stored (pause)"))
        backend._metadata_event("backend_record", None)  # the backend record could be written meanwhile
        self.assertEqual(backend._hold_reason(ActorRole.WORKER), "metadata_unavailable")
        self.assertIn("automation_pause", backend._faults_view()["metadata"]["sources"])
        backend._metadata_event("automation_pause", None)
        self.assertIsNone(backend._hold_reason(ActorRole.WORKER))
        self.assertIsNone(backend._faults_view()["metadata"])

    def test_the_ui_shows_the_store_error(self):
        model = ProductModel(Sender(), 40, 160, clock=lambda: 1_000_000.0)
        model.state = {"automation": {"state": "paused", "paused": True, "persistence_error": "pause_not_stored"}}
        self.assertIn("저장 오류", model.automation_text())


if __name__ == "__main__":
    unittest.main()
