from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from threading import Barrier, Event, Thread, current_thread
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from workbench.contracts.v1 import ActorRole
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, TaskMailbox
from workbench.observation.worker_review import ActiveRunRef, SerializedReviewAdmission
from workbench.policy.pause_automation import (
    PauseCoordinator, PauseJournal, PauseJournalError, PausePolicyError,
)
from workbench.policy.pause_automation.controller import _file_digest
from workbench.tasks.repository import TaskRepository


READY = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True,
}}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def stamp(moment=None):
    value = moment or datetime.now(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeBridge:
    def __init__(self):
        self.states = {
            "manager": {"role": "manager", "sessionId": str(uuid4()), "generation": 1,
                        "paused": False, "abortStatus": "none", "idle": False,
                        "pending": False, "approvalPending": False,
                        "editorKnown": True, "editorEmpty": True, "inFlightToolCount": 1,
                        "unconfirmedToolCallIds": ["tool-1"], "unknownOutcomeToolCallIds": [],
                        "unresolvedPriorSessions": []},
            "worker": {"role": "worker", "sessionId": str(uuid4()), "generation": 1,
                       "paused": False, "abortStatus": "none", "idle": True,
                       "pending": False, "approvalPending": False,
                       "editorKnown": True, "editorEmpty": True, "inFlightToolCount": 0,
                       "unconfirmedToolCallIds": [], "unknownOutcomeToolCallIds": [],
                       "unresolvedPriorSessions": []},
        }
        self.events = []
        self.requests = []
        self.fail_manager_pause = False

    def probe(self, role, timeout=5):
        return dict(self.states[role.value])

    def event_cursor(self):
        return len(self.events)

    def request(self, role, frame, timeout=5):
        self.requests.append((role.value, dict(frame)))
        state = self.states[role.value]
        if frame["kind"] == "pause":
            state["paused"] = True
            state["inFlightToolCount"] = 0
            state["idle"] = True
            if role is ActorRole.MANAGER and self.fail_manager_pause:
                state["abortStatus"] = "request_failed"
                return {"status": "abort_request_failed", "requestId": str(uuid4()),
                        "unconfirmedToolCallIds": ["tool-1"], "private": "api-token"}
            if role is ActorRole.MANAGER:
                request_id = str(uuid4())
                state["abortStatus"] = "stop_observed"
                event = {"role": "manager", "name": "turn_stop_observed",
                         "sessionId": state["sessionId"], "generation": state["generation"],
                         "bridgeSequence": len(self.events) + 1,
                         "abortRequestId": request_id,
                         "unconfirmedToolCallIds": ["tool-1"]}
                self.events.append(event)
                return {"status": "abort_requested", "requestId": request_id,
                        "unconfirmedToolCallIds": ["tool-1"]}
            return {"status": "paused", "requestId": str(uuid4()),
                    "unconfirmedToolCallIds": []}
        if frame["kind"] == "resume":
            if frame.get("reconciled") is not True:
                return {"status": "reconciliation_required"}
            state["paused"] = False
            state["abortStatus"] = "none"
            return {"status": "resumed", "requestId": str(uuid4()), "state": dict(state)}
        return {"status": "unexpected"}

    def wait_event(self, role, name, expected, *, after_sequence=0, timeout=15):
        return next(event for event in self.events[after_sequence:]
                    if event["name"] == name and all(event.get(k) == v for k, v in expected.items()))


class ThreadReadableRepository:
    """Give each policy thread its own SQLite reader for lock-order tests."""

    def __init__(self, path):
        self.path = path

    def _read(self, method, *args):
        repository = TaskRepository(self.path)
        try:
            return getattr(repository, method)(*args)
        finally:
            repository.close()

    def get_run(self, run_id):
        return self._read("get_run", run_id)

    def get_run_history(self, run_id):
        return self._read("get_run_history", run_id)

    def get_current_run(self, task_id):
        return self._read("get_current_run", task_id)

    def get_task_spec(self, task_id, revision):
        return self._read("get_task_spec", task_id, revision)

    def get_decisions(self, task_id, revision):
        return self._read("get_decisions", task_id, revision)


class PausePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cw12-pause-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = TaskRepository(self.root / "tasks.sqlite3")
        self.addCleanup(self.repository.close)
        task = {"goal": "approved task"}
        self.task_id = self.repository.create_task(task, task_id=str(uuid4()))
        self.repository.approve_scope(self.task_id, 1, {"paths": ["src/result.txt"], "kind": "bounded"})
        self.repository.proceed(self.task_id, 1, "start approved run")
        self.run_id = self.repository.start_run(self.task_id, 1)
        task_record = self.repository.get_task_spec(self.task_id, 1)
        approval = self.repository.get_decisions(self.task_id, 1)[0]
        approval_hash = sha256(canonical({"task": task_record, "approval": approval})).hexdigest()
        self.run = SimpleNamespace(
            repository=self.repository, task_id=self.task_id, revision=1, run_id=self.run_id,
            approval_hash=approval_hash, record_dir=self.root / "artifacts" / self.run_id,
        )
        self.run.record_dir.mkdir(parents=True)
        self.bridge = FakeBridge()
        self.base_state = {"portVersion": 2, "kind": "AutomationState", "payload": dict(READY["payload"])}
        self.admission = SerializedReviewAdmission(automation_state=self.base_state)
        self.admission.set_active_run(self.active_ref())
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.coordinator = self.make_coordinator()
        self.coordinator.bind_run(self.run)

    def make_coordinator(self):
        return PauseCoordinator(
            bridge=self.bridge, admission=self.admission,
            automation_state=lambda: self.base_state,
            request_timeout=0.1, stop_timeout=0.1, clock=lambda: stamp(self.now),
        )

    def active_ref(self, task_id=None, run_id=None):
        worker = self.bridge.states["worker"]
        task_id = task_id or self.task_id
        revision_id = str(uuid5(UUID(task_id), "task-spec-revision:1"))
        return ActiveRunRef(task_id, revision_id, 1,
                            run_id or self.run_id,
                            worker["sessionId"], worker["generation"])

    def resume_evidence(self, **changes):
        checked_at = changes.pop("checkedAt", stamp(self.now))
        return {"portVersion": 2, "kind": "ResumeEvidence", "payload": {
            "userResume": True, "taskId": self.task_id, "runId": self.run_id,
            "approvalHash": self.run.approval_hash,
            "filesMatch": True, "processesMatch": True, "toolsMatch": True,
            "taskMatch": True, "approvalMatch": True,
            "checkedAt": checked_at, "unknowns": [],
            **changes,
        }}

    def test_pause_is_durable_scoped_and_keeps_host_run_and_cancel_state_separate(self):
        self.coordinator.bind_run(self.run)
        result = self.coordinator.pause()
        self.assertTrue(result.paused)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.abort_status, "stop_observed")
        self.assertEqual(result.unknown_tool_call_ids, ("tool-1",))
        self.assertEqual(self.repository.get_current_run(self.task_id)["run_id"], self.run_id)
        self.assertEqual(self.coordinator.automation_state()["payload"]["paused"], True)
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "sent"), ("held", None))

        events = self.coordinator._journal.read()
        self.assertEqual([event.kind for event in events], [
            "pause_requested", "manager_pause_ack", "turn_stop_observed", "worker_pause_ack",
        ])
        for event in events:
            self.assertEqual(event.binding.task_id, self.task_id)
            self.assertEqual(event.binding.revision, 1)
            self.assertEqual(event.binding.run_id, self.run_id)
            self.assertEqual(event.binding.approved_paths, ("src/result.txt",))
            self.assertEqual(event.binding.approval_hash, self.run.approval_hash)

        observed = self.coordinator.collect_non_model(
            SimpleNamespace(collect=lambda *, timeout, paused: {
                "phase": "running", "paused": paused, "running_processes": ["python test.py"],
            }), timeout=0.05,
        )
        self.assertTrue(observed["paused"])
        self.assertEqual(observed["phase"], "running")
        self.assertFalse(self.bridge.states["worker"]["paused"] is False)
        self.assertNotIn("cancelled", [event.kind for event in events])

    def test_failed_abort_ack_is_unknown_and_sensitive_response_body_is_not_persisted(self):
        self.bridge.fail_manager_pause = True
        result = self.coordinator.pause()
        self.assertEqual(result.abort_status, "request_failed")
        self.assertIn("tool-1", result.unknown_tool_call_ids)
        raw = self.coordinator._journal.path.read_text()
        self.assertNotIn("api-token", raw)
        self.assertTrue(result.paused)
        self.assertEqual(self.coordinator.automation_state()["payload"]["cancelled"], False)

    def test_resume_requires_fresh_exact_reconciliation_and_never_replays_held_work(self):
        self.coordinator.pause()
        bad = (
            self.resume_evidence(userResume=False),
            self.resume_evidence(filesMatch=False),
            self.resume_evidence(unknowns=["tool outcome unresolved"]),
            self.resume_evidence(checkedAt=stamp(self.now - timedelta(seconds=1))),
            self.resume_evidence(runId=str(uuid4())),
        )
        for evidence in bad:
            with self.subTest(evidence=evidence["payload"]):
                with self.assertRaises(PausePolicyError):
                    self.coordinator.resume(self.run, evidence)
        self.now += timedelta(seconds=2)
        result = self.coordinator.resume(self.run, self.resume_evidence())
        self.assertFalse(result.paused)
        self.assertFalse(result.cancelled)
        self.assertEqual(self.coordinator.automation_state()["payload"]["paused"], False)
        self.assertEqual([frame["kind"] for _, frame in self.bridge.requests], [
            "pause", "pause", "resume", "resume",
        ])
        self.assertEqual([event.kind for event in self.coordinator._journal.read()][-4:], [
            "resume_requested", "manager_resumed", "worker_resumed", "resumed",
        ])
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "manual-independent"),
                         ("admitted", "manual-independent"))

    def _attach_live_resume_observations(self):
        worktree = self.root / "execution"
        worktree.mkdir()
        subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
        approved = worktree / "src" / "result.txt"
        approved.parent.mkdir()
        approved.write_text("before", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "src/result.txt"], check=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        approved.write_text("after", encoding="utf-8")
        commit = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        self.run.worktree = SimpleNamespace(path=worktree, commit=commit)
        self.run.shell = SimpleNamespace(snapshot=lambda: {"parent_pid": os.getpid()})
        return worktree

    def _attach_process_backed_peers(self, manager_cwd, worker_cwd):
        backing = self.bridge
        processes = {
            ActorRole.MANAGER: subprocess.Popen(["sleep", "10"], cwd=manager_cwd),
            ActorRole.WORKER: subprocess.Popen(["sleep", "10"], cwd=worker_cwd),
        }
        for process in processes.values():
            self.addCleanup(lambda process=process: (process.terminate(), process.wait(timeout=2))
                            if process.poll() is None else None)
        bridge = object.__new__(G3BridgeServer)
        bridge.peer = lambda role: SimpleNamespace(
            pid=processes[role].pid, session_id=backing.states[role.value]["sessionId"],
            generation=backing.states[role.value]["generation"])
        bridge.probe = backing.probe
        bridge.event_cursor = backing.event_cursor
        bridge.wait_event = backing.wait_event
        bridge.request = lambda role, frame, timeout, **_kwargs: backing.request(role, frame, timeout)
        self.bridge = bridge
        self.coordinator._bridge = bridge
        self.coordinator.bind_run(self.run)
        return backing

    def _attach_separate_execution_peers(self, *, flag=None, missing=False):
        worktree = self._attach_live_resume_observations()
        tracked = worktree / "outside.txt"
        tracked.write_text("base", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "outside.txt"], check=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "outside"],
                       check=True)
        self.run.worktree.commit = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True,
            text=True, check=True).stdout.strip()
        if flag is not None:
            subprocess.run(["git", "-C", str(worktree), "update-index", flag,
                            "outside.txt"], check=True)
        if missing:
            tracked.unlink()
        manager_cwd = self.root / "separate-manager"
        worker_cwd = self.root / "separate-worker"
        manager_cwd.mkdir()
        worker_cwd.mkdir()
        self._attach_process_backed_peers(manager_cwd, worker_cwd)
        return worktree, tracked

    def test_live_resume_derives_match_facts_instead_of_trusting_caller_booleans(self):
        self._attach_live_resume_observations()
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            status = self.coordinator.resume(self.run, self.resume_evidence(
                filesMatch=False, processesMatch=False, toolsMatch=False,
                taskMatch=False, approvalMatch=False))
        self.assertFalse(status.paused)

    def test_resume_observed_requires_explicit_intent_and_live_facts(self):
        self._attach_live_resume_observations()
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=False)
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_peer_pause_baseline_allows_stable_state_and_approved_worktree_change(self):
        worktree = self._attach_live_resume_observations()
        manager_cwd = self.root / "manager-peer"
        manager_cwd.mkdir()
        (manager_cwd / "existing.txt").write_text("baseline", encoding="utf-8")
        self._attach_process_backed_peers(manager_cwd, worktree)
        self.coordinator.pause()
        baseline = self.run.record_dir / "pause-file-baseline.json"
        self.assertTrue(baseline.is_file())
        self.assertNotIn("baseline", baseline.read_text(encoding="utf-8"))
        (worktree / "src" / "result.txt").write_text("approved after pause", encoding="utf-8")
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_peer_pause_baseline_allows_approved_change_in_peer_cwd(self):
        worktree = self._attach_live_resume_observations()
        self._attach_process_backed_peers(worktree / "src", worktree)
        self.coordinator.pause()
        (worktree / "src" / "result.txt").write_text("approved after pause", encoding="utf-8")
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_peer_pause_baseline_rejects_stable_out_of_scope_change_after_restart(self):
        worktree = self._attach_live_resume_observations()
        manager_cwd = self.root / "manager-peer"
        manager_cwd.mkdir()
        changed = manager_cwd / "existing.txt"
        changed.write_text("before", encoding="utf-8")
        self._attach_process_backed_peers(manager_cwd, worktree)
        self.coordinator.pause()
        restarted = self.make_coordinator()
        self.assertTrue(restarted.bind_run(self.run).paused)
        changed.write_text("after pause", encoding="utf-8")
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                restarted.resume_observed(self.run, user_resume=True)
        changed.write_text("before", encoding="utf-8")
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(restarted.resume_observed(self.run, user_resume=True).paused)

    def test_missing_peer_pause_baseline_blocks_resume(self):
        worktree = self._attach_live_resume_observations()
        manager_cwd = self.root / "manager-peer"
        manager_cwd.mkdir()
        self._attach_process_backed_peers(manager_cwd, worktree)
        self.coordinator.pause()
        (self.run.record_dir / "pause-file-baseline.json").unlink()
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_in_flight_write_after_pause_request_is_not_absorbed_into_baseline(self):
        worktree = self._attach_live_resume_observations()
        manager_cwd = self.root / "manager-peer"
        manager_cwd.mkdir()
        backing = self._attach_process_backed_peers(manager_cwd, worktree)
        entry = self.run.record_dir / "entry-file-baseline.json"
        self.assertTrue(entry.is_file())
        original_request = backing.request

        def finish_tool_after_pause_request(role, frame, timeout=5):
            if role is ActorRole.MANAGER and frame["kind"] == "pause":
                (manager_cwd / "late-output.txt").write_text("completed", encoding="utf-8")
            return original_request(role, frame, timeout)

        backing.request = finish_tool_after_pause_request
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_missing_entry_baseline_during_pause_fails_closed(self):
        worktree = self._attach_live_resume_observations()
        manager_cwd = self.root / "manager-peer"
        manager_cwd.mkdir()
        self._attach_process_backed_peers(manager_cwd, worktree)
        (self.run.record_dir / "entry-file-baseline.json").unlink()
        status = self.coordinator.pause()
        self.assertEqual(status.persistence_error, "file_baseline_unavailable")
        self.assertFalse((self.run.record_dir / "pause-file-baseline.json").exists())

    def test_missing_durable_entry_baseline_on_restart_does_not_recapture(self):
        worktree = self._attach_live_resume_observations()
        manager_cwd = self.root / "manager-peer"
        manager_cwd.mkdir()
        self._attach_process_backed_peers(manager_cwd, worktree)
        self.assertEqual(self.coordinator._journal.read()[0].kind,
                         "entry_baseline_recorded")
        (self.run.record_dir / "entry-file-baseline.json").unlink()
        restarted = self.make_coordinator()
        status = restarted.bind_run(self.run)
        self.assertEqual(status.persistence_error, "entry_baseline_unavailable")
        self.assertFalse((self.run.record_dir / "entry-file-baseline.json").exists())

    def test_unavailable_entry_scan_never_opens_automatic_dispatch(self):
        worktree = self._attach_live_resume_observations()
        manager_cwd = self.root / "manager-peer"
        manager_cwd.mkdir()
        with patch.object(PauseCoordinator, "_peer_file_state",
                          side_effect=PausePolicyError("peer scan unavailable")):
            self._attach_process_backed_peers(manager_cwd, worktree)
        self.assertEqual(self.coordinator.status().persistence_error,
                         "entry_baseline_unavailable")
        self.assertFalse((self.run.record_dir / "entry-file-baseline.json").exists())
        calls = []
        self.assertEqual(self.coordinator.dispatch_automatic(
            lambda: calls.append("local")), ("held", None))
        self.assertEqual(calls, [])

    def test_ignored_out_of_scope_output_blocks_resume(self):
        worktree = self._attach_live_resume_observations()
        (worktree / ".gitignore").write_text("ignored-output.txt\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "ignore"],
                       check=True)
        self.run.worktree.commit = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True,
            text=True, check=True).stdout.strip()
        self.assertEqual(set(PauseCoordinator._changed_files(worktree)), {"src/result.txt"})
        self.coordinator.pause()
        (worktree / "ignored-output.txt").write_text("late result", encoding="utf-8")
        self.assertIn("ignored-output.txt", PauseCoordinator._changed_files(worktree))
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_git_peer_clean_tracked_change_after_pause_blocks_resume(self):
        worktree = self._attach_live_resume_observations()
        manager_cwd = self.root / "manager-peer"
        manager_cwd.mkdir()
        tracked = manager_cwd / "tracked.txt"
        tracked.write_text("before", encoding="utf-8")
        subprocess.run(["git", "-C", str(manager_cwd), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(manager_cwd), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(manager_cwd), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        self._attach_process_backed_peers(manager_cwd, worktree)
        self.coordinator.pause()
        tracked.write_text("after", encoding="utf-8")
        subprocess.run(["git", "-C", str(manager_cwd), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(manager_cwd), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "after"],
                       check=True)
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_large_clean_git_peer_uses_bounded_changed_set_and_can_resume(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "large-git-peer"
        peer_root.mkdir()
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        for index in range(2050):
            (peer_root / f"clean-{index:04d}.txt").write_text("x", encoding="utf-8")
        with (peer_root / "large-clean.bin").open("wb") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        subprocess.run(["git", "-C", str(peer_root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        self.assertGreater(len(list(peer_root.glob("clean-*.txt"))), 2048)
        self.assertGreater((peer_root / "large-clean.bin").stat().st_size, 64 * 1024 * 1024)
        self._attach_process_backed_peers(peer_root, worktree)
        entry = json.loads((self.run.record_dir / "entry-file-baseline.json").read_text())
        self.assertEqual(entry["peers"]["manager"]["files"], {})
        self.assertEqual(len(entry["peers"]["manager"]["tracked_modes"]), 2051)
        self.assertEqual(entry["peers"]["manager"]["git"]["root"], str(peer_root))
        self.assertIsNone(self.coordinator.status().persistence_error)
        self.assertFalse(self.coordinator._authority_fence.closed())
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_git_peer_post_bind_changed_set_catches_tracked_untracked_and_ignored(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "git-peer-changes"
        peer_root.mkdir()
        tracked = peer_root / "tracked.txt"
        tracked.write_text("base", encoding="utf-8")
        (peer_root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        for target in (tracked, peer_root / "untracked.txt", peer_root / "ignored.txt"):
            with self.subTest(target=target.name):
                target.write_text("changed", encoding="utf-8")
                self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
                if target == tracked:
                    target.write_text("base", encoding="utf-8")
                else:
                    target.unlink()
                self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))

    def test_git_peer_assume_unchanged_cannot_hide_out_of_scope_tracked_edit(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "git-peer-hidden-edit"
        peer_root.mkdir()
        tracked = peer_root / "tracked.txt"
        tracked.write_text("before", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        subprocess.run(["git", "-C", str(peer_root), "update-index",
                        "--assume-unchanged", "tracked.txt"], check=True)
        tracked.write_text("after", encoding="utf-8")
        self.assertEqual(subprocess.run(
            ["git", "-C", str(peer_root), "diff", "--name-only", "HEAD"],
            capture_output=True, check=True).stdout, b"")
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_git_peer_stable_assume_unchanged_flag_hashes_hidden_edit(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "git-peer-stable-assume"
        peer_root.mkdir()
        tracked = peer_root / "tracked.txt"
        tracked.write_text("before", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        subprocess.run(["git", "-C", str(peer_root), "update-index",
                        "--assume-unchanged", "tracked.txt"], check=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        self.assertIn("tracked.txt", self.coordinator._index_flags(peer_root))
        tracked.write_text("after", encoding="utf-8")
        self.assertEqual(subprocess.run(
            ["git", "-C", str(peer_root), "diff", "--name-only", "HEAD"],
            capture_output=True, check=True).stdout, b"")
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_git_peer_skip_worktree_missing_path_is_live_but_appearance_blocks(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "git-peer-sparse"
        peer_root.mkdir()
        sparse = peer_root / "sparse.txt"
        sparse.write_text("base", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "add", "sparse.txt"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        subprocess.run(["git", "-C", str(peer_root), "update-index",
                        "--skip-worktree", "sparse.txt"], check=True)
        sparse.unlink()
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        entry = json.loads((self.run.record_dir / "entry-file-baseline.json").read_text())
        self.assertEqual(entry["peers"]["manager"]["files"]["sparse.txt"], "v2:missing")
        self.assertIn("sparse.txt", entry["peers"]["manager"]["index_flags"])
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        sparse.write_text("appeared", encoding="utf-8")
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
        sparse.unlink()
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_git_peer_existing_skip_worktree_file_change_blocks_resume(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "git-peer-existing-skip"
        peer_root.mkdir()
        tracked = peer_root / "tracked.txt"
        tracked.write_text("before", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        subprocess.run(["git", "-C", str(peer_root), "update-index",
                        "--skip-worktree", "tracked.txt"], check=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        tracked.write_text("after", encoding="utf-8")
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_git_peer_index_flag_removal_blocks_until_restored(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "git-peer-flag-removal"
        peer_root.mkdir()
        for name in ("assume.txt", "skip.txt"):
            (peer_root / name).write_text("base", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        for flag, name in (("assume-unchanged", "assume.txt"),
                           ("skip-worktree", "skip.txt")):
            subprocess.run(["git", "-C", str(peer_root), "update-index",
                            f"--{flag}", name], check=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        for flag, name in (("assume-unchanged", "assume.txt"),
                           ("skip-worktree", "skip.txt")):
            with self.subTest(flag=flag):
                subprocess.run(["git", "-C", str(peer_root), "update-index",
                                f"--no-{flag}", name], check=True)
                self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
                subprocess.run(["git", "-C", str(peer_root), "update-index",
                                f"--{flag}", name], check=True)
                self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))

    def test_git_index_fsmonitor_tag_is_treated_as_flagged_path(self):
        with patch.object(PauseCoordinator, "_bounded_git_records", side_effect=(
                (b"H tracked.txt",), (b"H tracked.txt",), (b"h tracked.txt",))):
            self.assertEqual(self.coordinator._index_flags(self.root),
                             {"tracked.txt": "H|H|h"})

    def test_git_peer_flag_change_without_content_change_blocks(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "git-peer-flag-change"
        peer_root.mkdir()
        tracked = peer_root / "tracked.txt"
        tracked.write_text("base", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        subprocess.run(["git", "-C", str(peer_root), "update-index",
                        "--skip-worktree", "tracked.txt"], check=True)
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))

    def test_git_index_flag_listing_has_byte_and_record_bounds(self):
        worktree = self._attach_live_resume_observations()
        with patch("workbench.policy.pause_automation.controller._MAX_GIT_NAME_BYTES", 8):
            with self.assertRaises(PausePolicyError):
                self.coordinator._index_flags(worktree)
        with patch("workbench.policy.pause_automation.controller._MAX_GIT_RECORDS", 0):
            with self.assertRaises(PausePolicyError):
                self.coordinator._index_flags(worktree)

    def test_execution_assume_unchanged_change_outside_peer_cwds_blocks_resume(self):
        worktree, tracked = self._attach_separate_execution_peers()
        self.coordinator.pause()
        subprocess.run(["git", "-C", str(worktree), "update-index",
                        "--assume-unchanged", "outside.txt"], check=True)
        tracked.write_text("hidden", encoding="utf-8")
        self.assertEqual(subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", "HEAD"],
            capture_output=True, check=True).stdout, b"src/result.txt\n")
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_execution_stable_assume_flag_hidden_edit_outside_peer_cwds_blocks(self):
        worktree, tracked = self._attach_separate_execution_peers(
            flag="--assume-unchanged")
        self.coordinator.pause()
        tracked.write_text("hidden", encoding="utf-8")
        self.assertEqual(subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", "HEAD"],
            capture_output=True, check=True).stdout, b"src/result.txt\n")
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_execution_skip_worktree_missing_is_stable_with_separate_peers(self):
        _, tracked = self._attach_separate_execution_peers(
            flag="--skip-worktree", missing=True)
        self.coordinator.pause()
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        tracked.write_text("appeared", encoding="utf-8")
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
        tracked.unlink()
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_execution_existing_skip_worktree_hidden_edit_outside_peer_cwds_blocks(self):
        worktree, tracked = self._attach_separate_execution_peers(
            flag="--skip-worktree")
        self.coordinator.pause()
        tracked.write_text("hidden", encoding="utf-8")
        self.assertEqual(subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", "HEAD"],
            capture_output=True, check=True).stdout, b"src/result.txt\n")
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_execution_flag_removal_blocks_and_restoration_reconciles(self):
        worktree, _ = self._attach_separate_execution_peers(
            flag="--skip-worktree")
        self.coordinator.pause()
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        subprocess.run(["git", "-C", str(worktree), "update-index",
                        "--no-skip-worktree", "outside.txt"], check=True)
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
        subprocess.run(["git", "-C", str(worktree), "update-index",
                        "--skip-worktree", "outside.txt"], check=True)
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_execution_index_observation_error_blocks_resume(self):
        self._attach_separate_execution_peers(flag="--assume-unchanged")
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        for method in ("_index_flags", "_changed_files"):
            with self.subTest(method=method), \
                    patch.object(PauseCoordinator, method,
                                 side_effect=PausePolicyError("index observation failed")), \
                    patch.object(PauseCoordinator, "_process_matches", return_value=True):
                with self.assertRaises(PausePolicyError):
                    self.coordinator.resume_observed(self.run, user_resume=True)
        self.assertTrue(self.coordinator.status().paused)

    def test_execution_legacy_baseline_without_flag_fields_blocks_resume(self):
        self._attach_separate_execution_peers(flag="--assume-unchanged")
        self.coordinator.pause()
        path = self.run.record_dir / "pause-file-baseline.json"
        baseline = json.loads(path.read_text(encoding="utf-8"))
        del baseline["execution_index_flags"]
        del baseline["execution_flagged_files"]
        path.write_text(json.dumps(baseline), encoding="utf-8")
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_execution_unchanged_resumes_with_both_peers_elsewhere(self):
        self._attach_separate_execution_peers()
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_peer_git_untracked_chmod_only_is_a_file_delta(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "peer-git-mode"
        peer_root.mkdir()
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        seed = peer_root / "seed.txt"
        seed.write_text("seed", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "add", "seed.txt"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        output = peer_root / "output.txt"
        output.write_text("same bytes", encoding="utf-8")
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        original_mode = stat.S_IMODE(output.stat().st_mode)
        output.chmod(original_mode | stat.S_IXUSR)
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
        output.chmod(original_mode)
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))

    def test_execution_untracked_chmod_only_changes_versioned_identity(self):
        worktree = self._attach_live_resume_observations()
        subprocess.run(["git", "-C", str(worktree), "rm", "--cached", "-q",
                        "src/result.txt"], check=True)
        output = worktree / "src" / "result.txt"
        manager_cwd = self.root / "mode-execution-manager"
        worker_cwd = self.root / "mode-execution-worker"
        manager_cwd.mkdir()
        worker_cwd.mkdir()
        self._attach_process_backed_peers(manager_cwd, worker_cwd)
        self.coordinator.pause()
        before = PauseCoordinator._changed_files(worktree)["src/result.txt"]
        original_mode = stat.S_IMODE(output.stat().st_mode)
        output.chmod(original_mode | stat.S_IXUSR)
        after = PauseCoordinator._changed_files(worktree)["src/result.txt"]
        self.assertNotEqual(before, after)
        self.assertEqual(before.split(":", 2)[2], after.split(":", 2)[2])
        self.assertTrue(before.startswith("v2:"))
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_execution_tracked_nonexecutable_chmod_outside_peer_cwds_blocks_resume(self):
        worktree, tracked = self._attach_separate_execution_peers()
        self.coordinator.pause()
        original_mode = stat.S_IMODE(tracked.stat().st_mode)
        tracked.chmod(original_mode & ~(stat.S_IRGRP | stat.S_IROTH))
        self.assertNotEqual(stat.S_IMODE(tracked.stat().st_mode), original_mode)
        self.assertEqual(subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", "HEAD"],
            capture_output=True, check=True).stdout, b"src/result.txt\n")
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_execution_tracked_approved_chmod_can_resume(self):
        worktree, _ = self._attach_separate_execution_peers()
        self.coordinator.pause()
        approved = worktree / "src" / "result.txt"
        approved.chmod(0o600)
        self.assertEqual(PauseCoordinator._tracked_modes(worktree, {})["src/result.txt"],
                         f"v3:{approved.stat().st_mode:06o}")
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_execution_tracked_path_removal_fails_closed(self):
        worktree, tracked = self._attach_separate_execution_peers()
        self.coordinator.pause()
        tracked.unlink()
        with self.assertRaises(PausePolicyError):
            PauseCoordinator._tracked_modes(worktree, {})
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_execution_tracked_special_replacement_blocks_resume(self):
        _, tracked = self._attach_separate_execution_peers()
        self.coordinator.pause()
        tracked.unlink()
        os.mkfifo(tracked)
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_oversized_mode_baseline_sidecars_block_resume(self):
        self._attach_separate_execution_peers()
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        for filename in ("entry-file-baseline.json", "pause-file-baseline.json"):
            with self.subTest(filename=filename):
                path = self.run.record_dir / filename
                original = path.read_bytes()
                try:
                    path.write_bytes(b"x" * (1024 * 1024 + 1))
                    with patch.object(PauseCoordinator, "_process_matches", return_value=True):
                        with self.assertRaises(PausePolicyError):
                            self.coordinator.resume_observed(self.run, user_resume=True)
                finally:
                    path.write_bytes(original)
        self.assertTrue(self.coordinator.status().paused)

    def test_tracked_mode_observation_bounds_and_unsafe_path_fail_closed(self):
        worktree, tracked = self._attach_separate_execution_peers()
        with patch("workbench.policy.pause_automation.controller._MAX_TRACKED_MODE_FILES", 1):
            with self.assertRaises(PausePolicyError):
                PauseCoordinator._tracked_modes(worktree, {})
        with patch("workbench.policy.pause_automation.controller._MAX_TRACKED_MODE_NAME_BYTES", 8):
            with self.assertRaises(PausePolicyError):
                PauseCoordinator._tracked_modes(worktree, {})
        tracked.unlink()
        tracked.symlink_to(worktree / "src" / "result.txt")
        with self.assertRaises(PausePolicyError):
            PauseCoordinator._tracked_modes(worktree, {})

    def test_peer_git_tracked_nonexecutable_chmod_outside_scope_blocks_resume(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "peer-tracked-nonexec-mode"
        peer_root.mkdir()
        tracked = peer_root / "tracked.txt"
        tracked.write_text("same", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        tracked.chmod(0o600)
        self.assertEqual(PauseCoordinator._changed_files(peer_root), {})
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_peer_git_tracked_approved_chmod_can_resume(self):
        worktree = self._attach_live_resume_observations()
        self._attach_process_backed_peers(worktree, worktree)
        self.coordinator.pause()
        approved = worktree / "src" / "result.txt"
        approved.chmod(0o600)
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertTrue(self.coordinator._peer_files_reconciled(self.run, binding))
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_empty_non_git_peer_root_chmod_blocks_resume(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "empty-peer-mode"
        peer_root.mkdir()
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        peer_root.chmod(0o555)
        self.addCleanup(peer_root.chmod, 0o755)
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_git_execution_root_chmod_blocks_resume(self):
        worktree, _ = self._attach_separate_execution_peers()
        self.coordinator.pause()
        worktree.chmod(0o555)
        self.addCleanup(worktree.chmod, 0o755)
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_git_peer_root_chmod_blocks_resume(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "git-peer-root-mode"
        peer_root.mkdir()
        (peer_root / "tracked.txt").write_text("same", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        peer_root.chmod(0o555)
        self.addCleanup(peer_root.chmod, 0o755)
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_git_execution_ancestor_chmod_outside_file_scope_blocks_resume(self):
        worktree, _ = self._attach_separate_execution_peers()
        self.coordinator.pause()
        ancestor = worktree / "src"
        ancestor.chmod(0o555)
        self.addCleanup(ancestor.chmod, 0o755)
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_approved_directory_chmod_reconciles_execution_and_git_peer(self):
        task_id = self.repository.create_task({"goal": "directory scope"}, task_id=str(uuid4()))
        self.repository.approve_scope(task_id, 1, {"paths": ["src/"], "kind": "bounded"})
        self.repository.proceed(task_id, 1, "start directory run")
        run_id = self.repository.start_run(task_id, 1)
        task_record = self.repository.get_task_spec(task_id, 1)
        approval = self.repository.get_decisions(task_id, 1)[0]
        approval_hash = sha256(canonical({"task": task_record, "approval": approval})).hexdigest()
        self.task_id, self.run_id = task_id, run_id
        self.run = SimpleNamespace(
            repository=self.repository, task_id=task_id, revision=1, run_id=run_id,
            approval_hash=approval_hash, record_dir=self.root / "artifacts" / run_id)
        self.run.record_dir.mkdir(parents=True)
        self.admission.set_active_run(self.active_ref())
        self.coordinator = self.make_coordinator()
        self.coordinator.bind_run(self.run)
        worktree = self._attach_live_resume_observations()
        self._attach_process_backed_peers(worktree, worktree)
        self.coordinator.pause()
        approved = worktree / "src"
        approved.chmod(0o555)
        self.addCleanup(approved.chmod, 0o755)
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_empty_nested_non_git_peer_directory_chmod_blocks_resume(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "empty-nested-peer"
        nested = peer_root / "nested"
        nested.mkdir(parents=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        nested.chmod(0o555)
        self.addCleanup(nested.chmod, 0o755)
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))

    def test_peer_directory_replacement_and_removal_fail_closed(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "replaced-peer"
        nested = peer_root / "nested"
        nested.mkdir(parents=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        old_identity = self.coordinator._directory_modes(peer_root)["nested/"]
        nested.rename(self.root / "moved-nested")
        nested.mkdir()
        self.assertNotEqual(self.coordinator._directory_modes(peer_root)["nested/"],
                            old_identity)
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
        nested.rmdir()
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_directory_mode_scan_bounds_and_unsafe_entries_fail_closed(self):
        root = self.root / "directory-mode-bounds"
        nested = root / "nested"
        nested.mkdir(parents=True)
        (root / "file.txt").write_text("same", encoding="utf-8")
        with patch("workbench.policy.pause_automation.controller._MAX_DIRECTORY_ENTRIES", 1):
            with self.assertRaises(PausePolicyError):
                PauseCoordinator._directory_modes(root)
        with patch("workbench.policy.pause_automation.controller._MAX_DIRECTORY_DEPTH", 0):
            with self.assertRaises(PausePolicyError):
                PauseCoordinator._directory_modes(root)
        nested.chmod(0o000)
        with self.assertRaises(PausePolicyError):
            PauseCoordinator._directory_modes(root)
        nested.chmod(0o755)
        (root / "link").symlink_to(nested, target_is_directory=True)
        with self.assertRaises(PausePolicyError):
            PauseCoordinator._directory_modes(root)

    def test_directory_mode_scan_rejects_symlink_swap_and_closes_fds(self):
        root = self.root / "directory-mode-swap"
        nested = root / "nested"
        nested.mkdir(parents=True)
        outside = self.root / "directory-mode-outside"
        outside.mkdir()
        before = len(os.listdir("/proc/self/fd"))
        original_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "nested" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                nested.rename(self.root / "moved-directory-mode")
                nested.symlink_to(outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        with patch("workbench.policy.pause_automation.controller.os.open",
                   side_effect=swap_before_open):
            with self.assertRaises(PausePolicyError):
                PauseCoordinator._directory_modes(root)
        self.assertTrue(swapped)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_git_filemode_disabled_fails_closed(self):
        worktree = self._attach_live_resume_observations()
        subprocess.run(["git", "-C", str(worktree), "config", "core.filemode", "false"],
                       check=True)
        with self.assertRaises(PausePolicyError):
            PauseCoordinator._changed_files(worktree)

    def test_peer_git_tracked_executable_bit_chmod_only_is_a_file_delta(self):
        worktree = self._attach_live_resume_observations()
        peer_root = self.root / "peer-tracked-mode"
        peer_root.mkdir()
        tracked = peer_root / "tracked.sh"
        tracked.write_text("echo same\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(peer_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "add", "tracked.sh"], check=True)
        subprocess.run(["git", "-C", str(peer_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
                       check=True)
        self._attach_process_backed_peers(peer_root, worktree)
        self.coordinator.pause()
        original_mode = stat.S_IMODE(tracked.stat().st_mode)
        tracked.chmod(original_mode | stat.S_IXUSR)
        self.assertIn("tracked.sh", PauseCoordinator._changed_files(peer_root))
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))

    def test_non_git_peer_chmod_only_blocks_then_unchanged_mode_resumes(self):
        worktree = self._attach_live_resume_observations()
        manager_cwd = self.root / "non-git-mode"
        manager_cwd.mkdir()
        observed = manager_cwd / "output.txt"
        observed.write_text("same bytes", encoding="utf-8")
        self._attach_process_backed_peers(manager_cwd, worktree)
        self.coordinator.pause()
        original_mode = stat.S_IMODE(observed.stat().st_mode)
        observed.chmod(original_mode | stat.S_IXUSR)
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)
        observed.chmod(original_mode)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            self.assertFalse(self.coordinator.resume_observed(
                self.run, user_resume=True).paused)

    def test_legacy_content_only_baseline_version_fails_closed(self):
        worktree = self._attach_live_resume_observations()
        manager_cwd = self.root / "legacy-mode"
        manager_cwd.mkdir()
        self._attach_process_backed_peers(manager_cwd, worktree)
        self.coordinator.pause()
        entry_path = self.run.record_dir / "entry-file-baseline.json"
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        self.assertEqual(entry["version"], 4)
        binding = self.coordinator.status().binding
        self.assertIsNotNone(binding)
        for obsolete in (1, 3):
            entry["version"] = obsolete
            entry_path.write_text(json.dumps(entry), encoding="utf-8")
            self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))
        entry["version"] = 4
        del entry["execution_directory_modes"]
        entry_path.write_text(json.dumps(entry), encoding="utf-8")
        self.assertFalse(self.coordinator._peer_files_reconciled(self.run, binding))

    def test_git_probe_error_with_repository_marker_fails_closed(self):
        worktree = self._attach_live_resume_observations()
        with patch("workbench.policy.pause_automation.controller.subprocess.run",
                   return_value=SimpleNamespace(returncode=128)):
            with self.assertRaises(PausePolicyError):
                self.coordinator._peer_manifest(worktree)

    def test_worktree_head_change_blocks_resume_even_when_clean(self):
        worktree = self._attach_live_resume_observations()
        self.coordinator.pause()
        subprocess.run(["git", "-C", str(worktree), "add", "src/result.txt"], check=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "after"],
                       check=True)
        self.now += timedelta(seconds=2)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume_observed(self.run, user_resume=True)

    def test_live_resume_rejects_out_of_scope_file_and_unobserved_tool(self):
        worktree = self._attach_live_resume_observations()
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        (worktree / "outside.txt").write_text("unapproved", encoding="utf-8")
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume(self.run, self.resume_evidence())
        (worktree / "outside.txt").unlink()
        self.bridge.states["manager"]["unknownOutcomeToolCallIds"] = ["not-recorded"]
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume(self.run, self.resume_evidence())

    def test_live_resume_rejects_process_and_persisted_task_or_approval_mismatch(self):
        self._attach_live_resume_observations()
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        for scan_result in (False, PausePolicyError("peer cwd unavailable")):
            with self.subTest(scan_result=scan_result):
                with patch.object(PauseCoordinator, "_process_matches", return_value=True), \
                     patch.object(PauseCoordinator, "_peer_files_reconciled",
                                  side_effect=scan_result if isinstance(scan_result, Exception)
                                  else None, return_value=scan_result):
                    with self.assertRaises(PausePolicyError):
                        self.coordinator.resume_observed(self.run, user_resume=True)
        with patch.object(PauseCoordinator, "_process_matches", return_value=False):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume(self.run, self.resume_evidence())
        original_hash = self.run.approval_hash
        self.run.approval_hash = "f" * 64
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume(self.run, self.resume_evidence())
        self.run.approval_hash = original_hash
        self.repository.complete_run(self.run_id)
        with patch.object(PauseCoordinator, "_process_matches", return_value=True):
            with self.assertRaises(PausePolicyError):
                self.coordinator.resume(self.run, self.resume_evidence())

    def test_process_reconciliation_reads_live_pid_and_cwd(self):
        worktree = self.root / "process-cwd"
        worktree.mkdir()
        process = subprocess.Popen(["sleep", "5"], cwd=worktree)
        try:
            self.run.worktree = SimpleNamespace(path=worktree)
            self.run.shell = SimpleNamespace(snapshot=lambda: {
                "parent_pid": process.pid, "lifecycle": {"child_pid": None, "lifetime": "ended"},
            })
            self.assertTrue(PauseCoordinator._process_matches(self.run))
        finally:
            process.terminate()
            process.wait(timeout=2)
        self.assertFalse(PauseCoordinator._process_matches(self.run))

    def test_process_reconciliation_allows_descendant_child_but_rejects_escape(self):
        worktree = self.root / "process-root"
        descendant = worktree / "nested"
        descendant.mkdir(parents=True)
        outside = self.root / "outside-process-root"
        outside.mkdir()
        parent = subprocess.Popen(["sleep", "10"], cwd=worktree)
        child = subprocess.Popen(["sleep", "10"], cwd=descendant)
        escaped = subprocess.Popen(["sleep", "10"], cwd=outside)
        self.addCleanup(lambda: [(process.terminate(), process.wait(timeout=2))
                                 for process in (parent, child, escaped)
                                 if process.poll() is None])
        self.run.worktree = SimpleNamespace(path=worktree)
        child_pid = child.pid
        self.run.shell = SimpleNamespace(snapshot=lambda: {
            "parent_pid": parent.pid,
            "lifecycle": {"child_pid": child_pid, "lifetime": "running"},
        })
        self.assertTrue(PauseCoordinator._process_matches(self.run))
        child_pid = escaped.pid
        self.assertFalse(PauseCoordinator._process_matches(self.run))
        child_pid = child.pid
        symlink = self.root / "linked-process-root"
        symlink.symlink_to(worktree, target_is_directory=True)
        self.run.worktree = SimpleNamespace(path=symlink)
        self.assertFalse(PauseCoordinator._process_matches(self.run))

    def test_worktree_file_scan_is_streamed_and_budgeted(self):
        worktree = self._attach_live_resume_observations()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            self.assertEqual(len(PauseCoordinator._changed_files(worktree)), 1)
        with patch("workbench.policy.pause_automation.controller._MAX_OBSERVED_BYTES", 4):
            with self.assertRaises(PausePolicyError):
                PauseCoordinator._changed_files(worktree)
        (worktree / "another.txt").write_text("x", encoding="utf-8")
        (worktree / "third.txt").write_text("x", encoding="utf-8")
        with patch("workbench.policy.pause_automation.controller._MAX_OBSERVED_FILES", 2):
            with self.assertRaises(PausePolicyError):
                PauseCoordinator._changed_files(worktree)
        with patch("workbench.policy.pause_automation.controller._MAX_GIT_NAME_BYTES", 8):
            with self.assertRaises(PausePolicyError):
                PauseCoordinator._changed_files(worktree)

    def test_peer_directory_scan_is_streamed_and_budgeted(self):
        directory = self.root / "peer-scan"
        directory.mkdir()
        (directory / "one.txt").write_text("12345", encoding="utf-8")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            self.assertEqual(len(PauseCoordinator._directory_manifest(directory)), 1)
        with patch("workbench.policy.pause_automation.controller._MAX_OBSERVED_BYTES", 4):
            with self.assertRaises(PausePolicyError):
                PauseCoordinator._directory_manifest(directory)
        (directory / "two.txt").write_text("x", encoding="utf-8")
        with patch("workbench.policy.pause_automation.controller._MAX_OBSERVED_FILES", 1):
            with self.assertRaises(PausePolicyError):
                PauseCoordinator._directory_manifest(directory)

    def test_file_scan_rejects_special_file_and_in_flight_mutation(self):
        directory = self.root / "unsafe-peer-scan"
        directory.mkdir()
        regular = directory / "regular.txt"
        regular.write_text("before", encoding="utf-8")
        fifo = directory / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(PausePolicyError):
            PauseCoordinator._directory_manifest(directory)
        original_read = os.read
        mutated = False

        def mutate_on_read(fd, amount):
            nonlocal mutated
            if not mutated:
                mutated = True
                regular.write_text("after!", encoding="utf-8")
            return original_read(fd, amount)

        with patch("workbench.policy.pause_automation.controller.os.read",
                   side_effect=mutate_on_read):
            with self.assertRaises(PausePolicyError):
                _file_digest(regular, 1024)

    def test_file_scan_rejects_mode_change_during_hash(self):
        target = self.root / "mode-during-hash.txt"
        target.write_text("unchanged", encoding="utf-8")
        original_mode = stat.S_IMODE(target.stat().st_mode)
        original_read = os.read
        changed = False

        def chmod_on_read(fd, amount):
            nonlocal changed
            if not changed:
                changed = True
                target.chmod(original_mode | stat.S_IXUSR)
            return original_read(fd, amount)

        with patch("workbench.policy.pause_automation.controller.os.read",
                   side_effect=chmod_on_read):
            with self.assertRaises(PausePolicyError):
                _file_digest(target, 1024)

    def test_peer_scan_rejects_directory_symlink_swap_and_closes_fds(self):
        directory = self.root / "peer-swap"
        nested = directory / "nested"
        nested.mkdir(parents=True)
        (nested / "safe.txt").write_text("safe", encoding="utf-8")
        private = self.root / "private-external"
        private.mkdir()
        (private / "secret.txt").write_text("private", encoding="utf-8")
        before = len(os.listdir("/proc/self/fd"))
        self.assertEqual(PauseCoordinator._directory_manifest(directory),
                         {"nested/safe.txt":
                          f"v2:{(nested / 'safe.txt').stat().st_mode:06o}:"
                          f"{sha256(b'safe').hexdigest()}"})
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        original_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "nested" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                nested.rename(directory / "moved")
                nested.symlink_to(private, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        with patch("workbench.policy.pause_automation.controller.os.open",
                   side_effect=swap_before_open):
            with self.assertRaises(OSError):
                PauseCoordinator._directory_manifest(directory)
        self.assertTrue(swapped)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_git_changed_set_rejects_directory_symlink_swap_and_closes_fds(self):
        worktree = self._attach_live_resume_observations()
        private = self.root / "private-git-external"
        private.mkdir()
        (private / "result.txt").write_text("private", encoding="utf-8")
        source = worktree / "src"
        before = len(os.listdir("/proc/self/fd"))
        original_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "src" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                source.rename(worktree / "moved")
                source.symlink_to(private, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        with patch("workbench.policy.pause_automation.controller.os.open",
                   side_effect=swap_before_open):
            with self.assertRaises(OSError):
                PauseCoordinator._changed_files(worktree)
        self.assertTrue(swapped)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_peer_scan_rejects_file_symlink_swap_without_reading_external_file(self):
        directory = self.root / "peer-file-swap"
        directory.mkdir()
        output = directory / "output.txt"
        output.write_text("approved", encoding="utf-8")
        private = self.root / "private-file"
        private.write_text("outside", encoding="utf-8")
        before = len(os.listdir("/proc/self/fd"))
        original_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "output.txt" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                output.rename(directory / "old-output.txt")
                output.symlink_to(private)
            return original_open(path, flags, *args, **kwargs)

        with patch("workbench.policy.pause_automation.controller.os.open",
                   side_effect=swap_before_open), \
             patch("workbench.policy.pause_automation.controller._digest_fd",
                   side_effect=AssertionError("external file read")):
            with self.assertRaises(OSError):
                PauseCoordinator._directory_manifest(directory)
        self.assertTrue(swapped)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_live_peer_cwd_file_reconciliation_fails_closed_on_unsafe_observation(self):
        cwds = {role: self.root / f"peer-{role.value}"
                for role in (ActorRole.MANAGER, ActorRole.WORKER)}
        processes = {}
        for role, cwd in cwds.items():
            cwd.mkdir()
            (cwd / "observed.txt").write_text(role.value, encoding="utf-8")
            processes[role] = subprocess.Popen(["sleep", "5"], cwd=cwd)
        bridge = object.__new__(G3BridgeServer)
        bridge.peer = lambda role: SimpleNamespace(pid=processes[role].pid)
        self.coordinator._bridge = bridge
        try:
            with patch.object(PauseCoordinator, "_directory_manifest",
                              wraps=PauseCoordinator._directory_manifest) as manifest:
                self.assertTrue(self.coordinator._peer_files_stable())
                self.assertEqual([call.args[0] for call in manifest.call_args_list],
                                 [cwds[ActorRole.MANAGER], cwds[ActorRole.MANAGER],
                                  cwds[ActorRole.WORKER], cwds[ActorRole.WORKER]])
            self.assertTrue(self.coordinator._peer_files_stable())
            with patch.object(PauseCoordinator, "_directory_manifest",
                              side_effect=[{"observed.txt": "manager"}] * 2
                                          + [{"observed.txt": "before"},
                                             {"observed.txt": "after"}]):
                self.assertFalse(self.coordinator._peer_files_stable())
            with patch.object(bridge, "peer", side_effect=LookupError("worker unavailable")):
                with self.assertRaises(LookupError):
                    self.coordinator._peer_files_stable()
            (cwds[ActorRole.WORKER] / "unsafe.txt").symlink_to(
                cwds[ActorRole.WORKER] / "observed.txt")
            with self.assertRaises(PausePolicyError):
                self.coordinator._peer_files_stable()
        finally:
            for process in processes.values():
                process.terminate()
                process.wait(timeout=2)

    def test_completed_failed_or_cancelled_run_never_opens_local_or_bound_dispatch(self):
        for terminal in ("complete_run", "fail_run", "cancel_run"):
            with self.subTest(terminal=terminal):
                # Each subcase owns a fresh persisted Task/run.
                if terminal != "complete_run":
                    self.repository.proceed(self.task_id, 1, "next run")
                    self.run_id = self.repository.start_run(self.task_id, 1)
                    self.run.run_id = self.run_id
                    self.run.record_dir = self.root / "artifacts" / self.run_id
                    self.run.record_dir.mkdir()
                    self.admission.set_active_run(self.active_ref())
                    self.coordinator.bind_run(self.run)
                if terminal == "cancel_run":
                    self.repository.cancel_run(self.run_id, "stop")
                else:
                    getattr(self.repository, terminal)(self.run_id)
                sent = []
                self.assertEqual(self.coordinator.dispatch_automatic(
                    lambda: sent.append("local")), ("held", None))
                mailbox = TaskMailbox(self.repository, self.bridge)
                mailbox.deliver = lambda *_args, **_kwargs: sent.append("transport")
                message = SimpleNamespace(task_id=self.task_id, revision=1, run_id=self.run_id)
                self.assertEqual(self.coordinator.dispatch_bound_automatic(
                    mailbox, message), ("held", None))
                self.assertEqual(sent, [])

    def test_revoked_run_never_opens_local_or_bound_dispatch(self):
        self.repository.revoke_authority(self.task_id, 1, "approval withdrawn")
        sent = []
        mailbox = TaskMailbox(self.repository, self.bridge)
        mailbox.deliver = lambda *_args, **_kwargs: sent.append("transport")
        message = SimpleNamespace(task_id=self.task_id, revision=1, run_id=self.run_id)
        self.assertEqual(self.coordinator.dispatch_automatic(
            lambda: sent.append("local")), ("held", None))
        self.assertEqual(self.coordinator.dispatch_bound_automatic(
            mailbox, message), ("held", None))
        self.assertEqual(sent, [])

    def test_terminal_transition_waits_for_open_dispatch_without_blocking_mailbox_write(self):
        entered, release, terminal_done = Event(), Event(), Event()
        self.run.repository = ThreadReadableRepository(self.repository.path)
        sent = []
        mailbox = TaskMailbox(self.repository, self.bridge)

        def delivery(*_args, **_kwargs):
            with TaskRepository(self.repository.path) as repository:
                repository.record_shell_event(self.run_id, "started", {"source": "mailbox"})
            sent.append("transport")
            entered.set()
            self.assertTrue(release.wait(2))
            return "ack"

        mailbox.deliver = delivery
        message = SimpleNamespace(task_id=self.task_id, revision=1, run_id=self.run_id)
        dispatch_result = []
        dispatch = Thread(target=lambda: dispatch_result.append(
            self.coordinator.dispatch_bound_automatic(mailbox, message)))
        dispatch.start()
        self.assertTrue(entered.wait(2))

        def finish():
            with TaskRepository(self.repository.path) as repository:
                repository.complete_run(self.run_id)
            terminal_done.set()

        terminal = Thread(target=finish)
        terminal.start()
        self.assertFalse(terminal_done.wait(.05))
        release.set()
        dispatch.join(2)
        terminal.join(2)
        self.assertFalse(dispatch.is_alive() or terminal.is_alive())
        self.assertTrue(terminal_done.is_set())
        self.assertEqual(sent, ["transport"])
        self.assertEqual(dispatch_result, [("admitted", "ack")])

    def test_completion_between_peer_preflight_and_bound_permit_emits_zero_bytes(self):
        original = self.coordinator._preflight_peer
        sent = []

        def retire_after_peer_check():
            self.assertTrue(original())
            self.repository.complete_run(self.run_id)
            return True

        self.coordinator._preflight_peer = retire_after_peer_check
        mailbox = TaskMailbox(self.repository, self.bridge)
        mailbox.deliver = lambda *_args, **_kwargs: sent.append("transport bytes")
        message = SimpleNamespace(task_id=self.task_id, revision=1, run_id=self.run_id)
        self.assertEqual(self.coordinator.dispatch_bound_automatic(
            mailbox, message), ("held", None))
        self.assertEqual(sent, [])

    def _assert_terminal_waits_for_local_dispatch(self, terminal_kind):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        entered, release, retired = Event(), Event(), Event()

        def action():
            entered.set()
            self.assertTrue(release.wait(2))
            return "opened"

        dispatch = Thread(target=lambda: self.coordinator.dispatch_automatic(action))
        dispatch.start()
        self.assertTrue(entered.wait(2))

        def retire():
            with TaskRepository(self.repository.path) as repository:
                if terminal_kind == "cancel_run":
                    repository.cancel_run(self.run_id, "race")
                else:
                    getattr(repository, terminal_kind)(self.run_id)
            retired.set()

        terminal = Thread(target=retire)
        terminal.start()
        self.assertFalse(retired.wait(.05))
        release.set()
        dispatch.join(2)
        terminal.join(2)
        self.assertFalse(dispatch.is_alive() or terminal.is_alive())
        self.assertTrue(retired.is_set())
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "late"), ("held", None))

    def test_fail_run_waits_for_open_local_dispatch_and_blocks_late_action(self):
        self._assert_terminal_waits_for_local_dispatch("fail_run")

    def test_cancel_run_waits_for_open_local_dispatch_and_blocks_late_action(self):
        self._assert_terminal_waits_for_local_dispatch("cancel_run")

    def test_restart_restores_pause_and_corrupt_journal_fails_closed(self):
        self.coordinator.pause()
        restored = self.make_coordinator()
        self.assertTrue(restored.bind_run(self.run).paused)
        before = len(self.bridge.requests)
        self.assertTrue(restored.pause().paused)
        self.assertEqual(len(self.bridge.requests), before)

        path = self.run.record_dir / "corrupt.jsonl"
        path.write_text("{partial", encoding="utf-8")
        from workbench.policy.pause_automation import PauseJournal
        with self.assertRaises(PauseJournalError):
            PauseJournal(path).read()

    def test_pause_fences_review_admission_before_abort_request(self):
        """A review cannot slip through after pause has closed local authority."""
        run = self.active_ref()
        self.admission.set_active_run(run)
        entered = Event()
        release = Event()
        original_update = self.coordinator._update_admission

        def delayed_update():
            entered.set()
            self.assertTrue(release.wait(2))
            original_update()

        self.coordinator._update_admission = delayed_update
        pause_thread = Thread(target=self.coordinator.pause)
        pause_thread.start()
        self.assertTrue(entered.wait(2))
        try:
            self.assertTrue(self.coordinator.status().paused)
            ticket = self.admission.issue_review(run, 1.0, 0, {})
            self.assertIsNotNone(ticket)
            sent = []
            result = self.admission.admit_and_dispatch(
                ticket, dispatch_review=lambda request: sent.append(request) or {"status": "omp_processed"},
            )
            self.assertIn(result.status, {"paused", "stale", "authority_denied"})
            self.assertEqual(sent, [])
        finally:
            release.set()
            pause_thread.join(2)
        self.assertFalse(pause_thread.is_alive())

    def test_pause_fences_dispatch_at_action_boundary(self):
        """A pause linearized between authority check and send must hold the send."""
        original_lock = self.coordinator._lock
        coordinator = self.coordinator

        class ReleaseGate:
            def __init__(self):
                self.depth = 0
                self.armed = True

            def __enter__(self):
                original_lock.acquire()
                self.depth += 1

            def __exit__(self, *_):
                self.depth -= 1
                original_lock.release()
                if self.depth == 0 and self.armed:
                    self.armed = False
                    coordinator.pause()

        coordinator._lock = ReleaseGate()
        paused_when_sent = []
        result = coordinator.dispatch_automatic(
            lambda: paused_when_sent.append(coordinator.status().paused) or "sent"
        )
        if result[0] == "held":
            self.assertEqual(paused_when_sent, [])
        else:
            self.assertEqual(result, ("admitted", "sent"))
            self.assertEqual(paused_when_sent, [False])
        self.assertTrue(coordinator.status().paused)

    def test_partial_peer_resume_recloses_both_peers_without_replaying_work(self):
        self.coordinator.pause()
        held = []
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: held.append("sent")),
                         ("held", None))
        original_request = self.bridge.request

        def partial_resume(role, frame, timeout=5):
            if role is ActorRole.WORKER and frame["kind"] == "resume":
                self.bridge.requests.append((role.value, dict(frame)))
                return {"status": "unknown", "requestId": str(uuid4())}
            return original_request(role, frame, timeout)

        self.bridge.request = partial_resume
        self.now += timedelta(seconds=2)
        before = len(self.bridge.requests)
        result = self.coordinator.resume(self.run, self.resume_evidence())
        self.assertTrue(result.paused)
        self.assertEqual(held, [])
        self.assertTrue(self.bridge.states["manager"]["paused"])
        self.assertTrue(self.bridge.states["worker"]["paused"])
        self.assertEqual([(role, frame["kind"]) for role, frame in self.bridge.requests[before:]],
                         [("manager", "resume"), ("worker", "resume"),
                          ("manager", "pause"), ("worker", "pause")])

    def test_restart_after_resumed_then_resume_failed_keeps_dispatch_closed(self):
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        original_publish = self.coordinator._publish_admission_state

        def fail_unpaused(state, *, fail_closed, expected_version=None):
            if state["payload"]["paused"] is False:
                return False
            return original_publish(state, fail_closed=fail_closed,
                                    expected_version=expected_version)

        self.coordinator._publish_admission_state = fail_unpaused
        self.assertTrue(self.coordinator.resume(self.run, self.resume_evidence()).paused)
        self.assertEqual([event.kind for event in self.coordinator._journal.read()][-2:],
                         ["resumed", "resume_failed"])
        restored = self.make_coordinator()
        self.assertTrue(restored.bind_run(self.run).paused)
        self.assertEqual(restored.dispatch_automatic(lambda: "sent"), ("held", None))
        self.assertTrue(self.admission._state["paused"])

    def test_refresh_requires_exact_stop_event_not_snapshot_or_replacement_session(self):
        self.bridge.wait_event = lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError())
        requested = self.coordinator.pause()
        self.assertEqual(requested.abort_status, "requested")
        self.assertIsNone(requested.stop_observed_at)
        self.assertEqual(self.coordinator.refresh().abort_status, "requested")
        self.bridge.states["manager"].update({
            "sessionId": str(uuid4()), "generation": 2,
            "abortStatus": "stop_observed", "unknownOutcomeToolCallIds": ["foreign-tool"],
        })
        refreshed = self.coordinator.refresh()
        self.assertEqual(refreshed.abort_status, "requested")
        self.assertIsNone(refreshed.stop_observed_at)
        self.assertNotIn("foreign-tool", refreshed.unknown_tool_call_ids)
        self.assertFalse(any(event.kind == "turn_stop_observed"
                             for event in self.coordinator._journal.read()))

    def test_pause_rejects_stop_event_from_replacement_session_even_with_request_id(self):
        replacement = str(uuid4())

        def replaced_stop(role, name, expected, *, after_sequence=0, timeout=15):
            self.bridge.states["manager"].update({
                "sessionId": replacement, "generation": 2,
                "abortStatus": "stop_observed",
            })
            return {"role": "manager", "name": name,
                    "bridgeSequence": after_sequence + 1,
                    "abortRequestId": expected["abortRequestId"],
                    "sessionId": replacement, "generation": 2}

        self.bridge.wait_event = replaced_stop
        result = self.coordinator.pause()
        self.assertEqual(result.abort_status, "requested")
        self.assertIsNone(result.stop_observed_at)
        self.assertFalse(any(event.kind == "turn_stop_observed"
                             for event in self.coordinator._journal.read()))

    def test_pause_rejects_stop_event_from_unrelated_request(self):
        manager = self.bridge.states["manager"]

        def unrelated_stop(role, name, expected, *, after_sequence=0, timeout=15):
            return {"role": "manager", "name": name,
                    "sessionId": manager["sessionId"],
                    "generation": manager["generation"],
                    "bridgeSequence": after_sequence + 1,
                    "abortRequestId": "unrelated-request"}

        self.bridge.wait_event = unrelated_stop
        result = self.coordinator.pause()
        self.assertEqual(result.abort_status, "requested")
        self.assertIsNone(result.stop_observed_at)
        self.assertFalse(any(event.kind == "turn_stop_observed"
                             for event in self.coordinator._journal.read()))

    def test_pause_rejects_pre_cursor_stop_event_with_exact_request(self):
        manager = self.bridge.states["manager"]

        def stale_stop(role, name, expected, *, after_sequence=0, timeout=15):
            return {"role": "manager", "name": name,
                    "sessionId": manager["sessionId"],
                    "generation": manager["generation"],
                    "bridgeSequence": after_sequence,
                    "abortRequestId": expected["abortRequestId"]}

        self.bridge.wait_event = stale_stop
        result = self.coordinator.pause()
        self.assertEqual(result.abort_status, "requested")
        self.assertIsNone(result.stop_observed_at)
        self.assertFalse(any(event.kind == "turn_stop_observed"
                             for event in self.coordinator._journal.read()))

    def test_cancelled_exact_run_stops_dispatch_and_preserves_artifacts(self):
        self.coordinator.pause()
        journal_before = self.coordinator._journal.path.read_bytes()
        result_file = self.run.record_dir / "result.json"
        result_file.write_text('{"outcome":"partial"}', encoding="utf-8")
        self.repository.cancel_run(self.run_id, "user cancelled approved run")
        self.assertTrue(self.coordinator.status().cancelled)
        sent = []
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: sent.append("sent")),
                         ("held", None))
        self.assertEqual(sent, [])
        self.assertEqual(result_file.read_text(encoding="utf-8"), '{"outcome":"partial"}')
        self.assertTrue(self.coordinator._journal.path.read_bytes().startswith(journal_before))
        self.assertEqual(self.repository.get_run_history(self.run_id)[-1]["kind"], "cancelled")

    def test_cancelled_unpaused_run_fences_new_automatic_dispatch(self):
        self.repository.cancel_run(self.run_id, "user cancelled approved run")
        sent = []
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: sent.append("sent")),
                         ("held", None))
        self.assertEqual(sent, [])
        self.assertTrue(self.coordinator.status().cancelled)

    def test_resume_rejects_session_replacement_and_unresolved_tool(self):
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        evidence = self.resume_evidence()
        self.bridge.states["worker"]["sessionId"] = str(uuid4())
        with self.assertRaises(PausePolicyError):
            self.coordinator.resume(self.run, evidence)
        self.bridge.states["worker"]["sessionId"] = self.coordinator.status().binding.worker_session_id
        self.bridge.states["worker"]["inFlightToolCount"] = 1
        with self.assertRaises(PausePolicyError):
            self.coordinator.resume(self.run, evidence)
        self.assertTrue(self.coordinator.status().paused)
        self.assertFalse(any(frame["kind"] == "resume" for _, frame in self.bridge.requests))

    def test_resume_rejects_revoked_approved_scope(self):
        self.coordinator.pause()
        self.repository.revoke_authority(self.task_id, 1, "approval withdrawn")
        self.now += timedelta(seconds=2)
        with self.assertRaises(PausePolicyError):
            self.coordinator.resume(self.run, self.resume_evidence())
        self.assertTrue(self.coordinator.status().paused)
        self.assertFalse(any(frame["kind"] == "resume" for _, frame in self.bridge.requests))

    def test_binding_different_run_clears_prior_pause_observations(self):
        first = self.coordinator.pause()
        self.assertEqual(first.abort_status, "stop_observed")
        second_task = self.repository.create_task({"goal": "another task"}, task_id=str(uuid4()))
        self.repository.approve_scope(second_task, 1, {"paths": ["src/other.txt"], "kind": "bounded"})
        self.repository.proceed(second_task, 1, "start another run")
        second_run_id = self.repository.start_run(second_task, 1)
        task = self.repository.get_task_spec(second_task, 1)
        approval = self.repository.get_decisions(second_task, 1)[0]
        second = SimpleNamespace(
            repository=self.repository, task_id=second_task, revision=1, run_id=second_run_id,
            approval_hash=sha256(canonical({"task": task, "approval": approval})).hexdigest(),
            record_dir=self.root / "artifacts" / second_run_id,
        )
        second.record_dir.mkdir(parents=True)
        self.admission.set_active_run(self.active_ref(second_task, second_run_id))
        try:
            status = self.coordinator.bind_run(second)
        except PausePolicyError:
            status = self.coordinator.status()
            self.assertEqual(status.binding.run_id, self.run_id)
            self.assertTrue(status.paused)
            return
        self.assertEqual(status.binding.run_id, second_run_id)
        self.assertFalse(status.paused)
        self.assertIsNone(status.manager_ack_status)
        self.assertIsNone(status.worker_ack_status)
        self.assertEqual(status.abort_status, "none")
        self.assertIsNone(status.stop_observed_at)
        self.assertEqual(status.unknown_tool_call_ids, ())

    def test_journal_rejects_symlink_and_corruption_and_serializes_writers(self):
        journal = PauseJournal(self.run.record_dir / "adversarial.jsonl")
        binding = self.coordinator.status().binding
        target = self.run.record_dir / "target.txt"
        target.write_text("protected", encoding="utf-8")
        journal.path.symlink_to(target)
        with self.assertRaises(PauseJournalError):
            journal.read()
        with self.assertRaises(PauseJournalError):
            journal.append(pause_id="p", kind="pause_requested", occurred_at=stamp(self.now), binding=binding)

        self.assertEqual(target.read_text(encoding="utf-8"), "protected")
        journal.path.unlink()
        start = Barrier(9)
        errors = []

        def append_one():
            try:
                start.wait(2)
                journal.append(pause_id="p", kind="pause_requested", occurred_at=stamp(self.now), binding=binding)
            except Exception as exc:
                errors.append(exc)

        writers = [Thread(target=append_one) for _ in range(8)]
        for writer in writers:
            writer.start()
        start.wait(2)
        for writer in writers:
            writer.join(2)
        self.assertFalse(any(writer.is_alive() for writer in writers))
        self.assertEqual(errors, [])
        self.assertEqual([event.sequence for event in journal.read()], list(range(1, 9)))
        with journal.path.open("ab") as handle:
            handle.write(b"{partial")
        with self.assertRaises(PauseJournalError):
            journal.read()
        with self.assertRaises(PauseJournalError):
            journal.append(pause_id="p", kind="pause_requested", occurred_at=stamp(self.now), binding=binding)

    def test_failed_journal_fsync_keeps_policy_closed_and_blocks_resume(self):
        with patch("workbench.policy.pause_automation.journal.os.fsync", side_effect=OSError("disk error")):
            status = self.coordinator.pause()
        self.assertTrue(status.paused)
        self.assertEqual(status.persistence_error, "PauseJournalError")
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "sent"), ("held", None))
        self.now += timedelta(seconds=2)
        with self.assertRaises(PauseJournalError):
            self.coordinator.resume(self.run, self.resume_evidence())

    def test_first_journal_append_fsyncs_file_and_checked_directory(self):
        journal = PauseJournal(self.run.record_dir / "first-durable.jsonl")
        binding = self.coordinator.status().binding
        real_fsync = os.fsync
        synced = []

        def record_fsync(fd):
            synced.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            real_fsync(fd)

        with patch("workbench.policy.pause_automation.journal.os.fsync", side_effect=record_fsync):
            journal.append(pause_id="p", kind="pause_requested",
                           occurred_at=stamp(self.now), binding=binding)
        self.assertEqual(synced, ["file", "directory"])
        self.assertEqual(len(journal.read()), 1)

    def test_directory_fsync_failure_fails_closed_and_parent_symlink_is_rejected(self):
        real_fsync = os.fsync
        synced = []

        def fail_directory(fd):
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            synced.append("directory" if directory else "file")
            if directory:
                raise OSError("directory fsync unavailable")
            real_fsync(fd)

        with patch("workbench.policy.pause_automation.journal.os.fsync", side_effect=fail_directory):
            status = self.coordinator.pause()
        self.assertTrue(status.paused)
        self.assertEqual(status.persistence_error, "PauseJournalError")
        self.assertEqual(synced[:2], ["file", "directory"])
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "sent"), ("held", None))
        alias = self.root / "journal-alias"
        alias.symlink_to(self.run.record_dir, target_is_directory=True)
        with self.assertRaises(PauseJournalError):
            PauseJournal(alias / "pause.jsonl").append(
                pause_id="p", kind="pause_requested", occurred_at=stamp(self.now),
                binding=self.coordinator.status().binding,
            )

    def test_first_directory_fsync_failure_stays_closed_with_current_or_readable_evidence(self):
        """CW15 owns recovery if crash loses the first failed journal name entirely."""
        real_fsync = os.fsync
        run = self.active_ref()
        self.admission.set_active_run(run)

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            real_fsync(fd)

        with patch("workbench.policy.pause_automation.journal.os.fsync", side_effect=fail_directory):
            status = self.coordinator.pause()
        self.assertTrue(status.paused)
        self.assertEqual(status.persistence_error, "PauseJournalError")
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "sent"), ("held", None))
        ticket = self.admission.issue_review(run, 1.0, 0, {})
        sent = []
        result = self.admission.admit_and_dispatch(
            ticket, dispatch_review=lambda request: sent.append(request) or {"status": "omp_processed"},
        )
        self.assertIn(result.status, {"paused", "stale", "authority_denied"})
        self.assertEqual(sent, [])
        observation = self.coordinator.collect_non_model(
            SimpleNamespace(collect=lambda *, timeout, paused: {"paused": paused, "phase": "running"}),
            timeout=0.05,
        )
        self.assertEqual(observation, {"paused": True, "phase": "running"})
        self.now += timedelta(seconds=2)
        with self.assertRaises(PauseJournalError):
            self.coordinator.resume(self.run, self.resume_evidence())
        retained = self.make_coordinator()
        self.assertTrue(retained.bind_run(self.run).paused)
        self.assertEqual(retained.dispatch_automatic(lambda: "sent"), ("held", None))

        # Same-process state still remembers the failed pause if the name goes
        # missing; a fresh bootstrap with zero surviving evidence is CW15-owned.
        self.coordinator._journal.path.unlink()
        rebound = self.coordinator.bind_run(self.run)
        self.assertTrue(rebound.paused)
        self.assertEqual(rebound.persistence_error, "journal_missing")
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "sent"), ("held", None))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is unavailable")
    def test_journal_rejects_fifo_without_blocking_reader(self):
        fifo = self.run.record_dir / "special.jsonl"
        os.mkfifo(fifo)
        script = (
            "from workbench.policy.pause_automation import PauseJournal, PauseJournalError\n"
            "import sys\n"
            "try:\n"
            "    PauseJournal(sys.argv[1]).read()\n"
            "except PauseJournalError:\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(1)\n"
        )
        try:
            outcome = subprocess.run(
                [sys.executable, "-B", "-c", script, str(fifo)],
                env={**os.environ, "PYTHONPATH": "src"},
                capture_output=True, timeout=1,
            )
        except subprocess.TimeoutExpired:
            self.fail("PauseJournal.read blocks while opening a FIFO")
        self.assertEqual(outcome.returncode, 0, outcome.stderr.decode(errors="replace"))

    def test_pause_and_review_callback_do_not_invert_admission_lock_order(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        run = self.active_ref()
        self.admission.set_active_run(run)
        ticket = self.admission.issue_review(run, 1.0, 0, {})
        self.assertIsNotNone(ticket)
        publishing = Event()
        release_publish = Event()
        original_publish = self.coordinator._publish_admission_state
        errors = []

        def delayed_publish(state, *, fail_closed, expected_version=None):
            publishing.set()
            if not release_publish.wait(3):
                raise TimeoutError("test did not release admission publish")
            return original_publish(state, fail_closed=fail_closed,
                                    expected_version=expected_version)

        self.coordinator._publish_admission_state = delayed_publish

        def pause_one():
            try:
                self.coordinator.pause()
            except Exception as exc:
                errors.append(exc)

        pause_thread = Thread(target=pause_one)
        pause_thread.start()
        self.assertTrue(publishing.wait(2))
        delivered = []
        result_holder = []

        def review_one():
            try:
                result_holder.append(self.admission.admit_and_dispatch(
                    ticket, dispatch_review=lambda request: {
                        "status": ("omp_processed" if self.coordinator.dispatch_automatic(
                            lambda: delivered.append(request)
                        )[0] == "admitted" else "deferred")
                    },
                ))
            except Exception as exc:
                errors.append(exc)

        review_thread = Thread(target=review_one)
        review_thread.start()
        review_thread.join(2)
        try:
            self.assertFalse(review_thread.is_alive(), "review callback waited for pause publisher")
            self.assertEqual(errors, [])
            self.assertEqual(delivered, [])
            self.assertEqual(result_holder[0].status, "paused")
        finally:
            release_publish.set()
            pause_thread.join(3)
        self.assertFalse(pause_thread.is_alive(), "pause deadlocked with review callback")
        self.assertTrue(self.coordinator.status().paused)

    def test_cancellation_status_callback_completes_while_publisher_waits_for_cw11_lock(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        run = self.active_ref()
        self.admission.set_active_run(run)
        self.coordinator.bind_run(self.run)
        ticket = self.admission.issue_review(run, 1.0, 0, {})
        self.assertIsNotNone(ticket)
        callback_entered = Event()
        publisher_entered = Event()
        release_callback = Event()
        errors = []
        callback_status = []
        review_results = []
        original_set = self.admission.set_automation_state

        def observed_set(state):
            if current_thread().name == "waiting-publisher":
                publisher_entered.set()
            return original_set(state)

        self.admission.set_automation_state = observed_set

        def review_callback(_request):
            callback_entered.set()
            if not release_callback.wait(3):
                raise TimeoutError("publisher did not enter the CW11 lock boundary")
            callback_status.append(self.coordinator.status())
            return {"status": "deferred"}

        def review():
            try:
                review_results.append(self.admission.admit_and_dispatch(
                    ticket, dispatch_review=review_callback,
                ))
            except Exception as exc:
                errors.append(exc)

        def publish():
            try:
                self.coordinator._update_admission()
            except Exception as exc:
                errors.append(exc)

        review_thread = Thread(target=review, name="review-callback", daemon=True)
        publisher_thread = Thread(target=publish, name="waiting-publisher", daemon=True)
        review_thread.start()
        self.assertTrue(callback_entered.wait(2))
        self.repository.cancel_run(self.run_id, "cancel during review status callback")
        publisher_thread.start()
        try:
            self.assertTrue(publisher_entered.wait(2))
        finally:
            release_callback.set()
            review_thread.join(3)
            publisher_thread.join(3)
        self.assertFalse(review_thread.is_alive() or publisher_thread.is_alive(),
                         "CW11 callback and publisher formed a lock cycle")
        self.assertEqual(errors, [])
        self.assertEqual([status.cancelled for status in callback_status], [True])
        self.assertEqual([result.status for result in review_results], ["deferred"])
        self.assertTrue(self.admission._state["cancelled"])
        self.assertTrue(self.coordinator._authority_fence.closed())

    def test_stale_unpaused_publisher_cannot_override_new_pause(self):
        captured = Event()
        release = Event()
        original = self.coordinator._publish_admission_state
        results = []

        def delayed(state, *, fail_closed, expected_version=None):
            if state["payload"]["paused"] is False:
                captured.set()
                if not release.wait(3):
                    raise TimeoutError("stale publisher was not released")
            return original(state, fail_closed=fail_closed,
                            expected_version=expected_version)

        self.coordinator._publish_admission_state = delayed
        old = Thread(target=lambda: results.append(self.coordinator._update_admission()))
        old.start()
        self.assertTrue(captured.wait(2))
        try:
            self.assertTrue(self.coordinator.pause().paused)
        finally:
            release.set()
            old.join(3)
        self.assertFalse(old.is_alive())
        self.assertEqual(results, [False])
        self.assertTrue(self.admission._state["paused"])
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "sent"), ("held", None))

    def test_paused_fence_blocks_review_while_mirror_is_stale_unpaused(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        run = self.active_ref()
        self.admission.set_active_run(run)
        self.coordinator.bind_run(self.run)
        publishing = Event()
        release = Event()
        original = self.coordinator._publish_admission_state
        errors = []
        sent = []

        def delayed(state, *, fail_closed, expected_version=None):
            if state["payload"]["paused"] is True:
                publishing.set()
                if not release.wait(3):
                    raise TimeoutError("paused mirror publication was not released")
            return original(state, fail_closed=fail_closed,
                            expected_version=expected_version)

        self.coordinator._publish_admission_state = delayed
        def pause_one():
            try:
                self.coordinator.pause()
            except Exception as exc:
                errors.append(exc)

        thread = Thread(target=pause_one)
        thread.start()
        try:
            self.assertTrue(publishing.wait(2))
            self.assertFalse(self.admission._state["paused"], "mirror should still be stale")
            ticket = self.admission.issue_review(run, 1.0, 0, {})
            self.assertIsNotNone(ticket)
            result = self.admission.admit_and_dispatch(
                ticket, dispatch_review=lambda request: sent.append(request),
            )
            self.assertEqual(result.status, "paused")
            self.assertEqual(sent, [])
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(self.admission._state["paused"])

    def test_inflight_unpaused_publisher_finishes_before_paused_publication(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        run = self.active_ref()
        self.admission.set_active_run(run)
        self.coordinator.bind_run(self.run)
        entered = Event()
        release = Event()
        paused_entered = Event()
        release_paused = Event()
        original = self.admission.set_automation_state
        errors = []
        sent = []

        def delayed(state):
            if state["payload"]["paused"] is False and not entered.is_set():
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("unpaused publication was not released")
            elif state["payload"]["paused"] is True and not release_paused.is_set():
                paused_entered.set()
                if not release_paused.wait(3):
                    raise TimeoutError("paused publication was not released")
            return original(state)

        self.admission.set_automation_state = delayed

        def run_action(action):
            try:
                action()
            except Exception as exc:
                errors.append(exc)

        old = Thread(target=lambda: run_action(self.coordinator._update_admission))
        old.start()
        self.assertTrue(entered.wait(2))
        pausing = Thread(target=lambda: run_action(self.coordinator.pause))
        pausing.start()
        try:
            self.assertTrue(paused_entered.wait(2))
            self.assertTrue(self.coordinator._authority_fence.closed())
            self.assertFalse(self.admission._state["paused"],
                             "CW11 mirror was not stale at the admission boundary")
            ticket = self.admission.issue_review(run, 1.0, 0, {})
            self.assertIsNotNone(ticket)
            result = self.admission.admit_and_dispatch(
                ticket, dispatch_review=lambda request: sent.append(request),
            )
            self.assertEqual(result.status, "paused")
            self.assertEqual(sent, [])
            self.assertEqual(self.coordinator.dispatch_automatic(lambda: "sent"), ("held", None))
        finally:
            release.set()
            release_paused.set()
            old.join(3)
            pausing.join(3)
        self.assertFalse(old.is_alive() or pausing.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(self.admission._state["paused"])
        self.assertTrue(self.coordinator.status().paused)

    def test_pause_a_cannot_record_or_publish_into_concurrently_bound_run_b(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        second_task = self.repository.create_task({"goal": "second approved task"})
        self.repository.approve_scope(second_task, 1, {"paths": ["second.txt"]})
        self.repository.proceed(second_task, 1, "start second run")
        second_run_id = self.repository.start_run(second_task, 1)
        task = self.repository.get_task_spec(second_task, 1)
        approval = self.repository.get_decisions(second_task, 1)[0]
        second_dir = self.root / "artifacts" / second_run_id
        second_dir.mkdir()
        second_run = SimpleNamespace(
            repository=ThreadReadableRepository(self.repository.path),
            task_id=second_task, revision=1,
            run_id=second_run_id, record_dir=second_dir,
            approval_hash=sha256(canonical({"task": task, "approval": approval})).hexdigest(),
        )
        entered = Event()
        release = Event()
        original = self.bridge.request
        errors = []
        observations = []

        def delayed(role, frame, timeout=5):
            if role is ActorRole.MANAGER and frame["kind"] == "pause":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("manager pause was not released")
            return original(role, frame, timeout)

        self.bridge.request = delayed

        def pause_a():
            try:
                observations.append(("A", self.coordinator.pause()))
            except Exception as exc:
                errors.append(exc)

        def bind_b():
            try:
                observations.append(("B", self.coordinator.bind_run(second_run)))
            except Exception as exc:
                errors.append(exc)

        pausing = Thread(target=pause_a)
        binding = Thread(target=bind_b)
        pausing.start()
        self.assertTrue(entered.wait(2))
        binding.start()
        try:
            self.assertEqual(self.coordinator._binding.run_id, self.run_id)
        finally:
            release.set()
            pausing.join(3)
            binding.join(3)
        self.assertFalse(pausing.is_alive() or binding.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([(name, item.binding.run_id) for name, item in observations],
                         [("A", self.run_id), ("B", second_run_id)])
        self.assertTrue(all(event.binding.run_id == self.run_id
                            for event in PauseJournal(self.run.record_dir / "pause.jsonl").read()))
        self.assertEqual(PauseJournal(second_dir / "pause.jsonl").read(), ())
        self.assertEqual(self.coordinator.status().binding.run_id, second_run_id)
        self.assertTrue(self.coordinator.status().paused,
                        "B cannot open while CW11 still owns A")
        self.assertTrue(self.coordinator._authority_fence.closed())
        sent = []
        requests_before = len(self.bridge.requests)
        mailbox = TaskMailbox(self.repository, self.bridge)
        mailbox.deliver = lambda *_args, **_kwargs: sent.append("transport")
        message = SimpleNamespace(task_id=second_task, revision=1, run_id=second_run_id)
        self.assertEqual(self.coordinator.dispatch_automatic(
            lambda: sent.append("local")), ("held", None))
        self.assertEqual(self.coordinator.dispatch_bound_automatic(
            mailbox, message), ("held", None))
        self.assertEqual(sent, [])
        self.assertEqual(len(self.bridge.requests), requests_before)
        self.admission.set_active_run(self.active_ref(second_task, second_run_id))
        self.assertFalse(self.coordinator.bind_run(second_run).paused)
        self.assertFalse(self.admission._state["paused"],
                         "exactly activated B inherited run A's pause")

    def test_resume_replacement_after_ack_or_publication_remains_held(self):
        for boundary in ("ack_state", "ack", "probe", "publication"):
            with self.subTest(boundary=boundary):
                coordinator = self.make_coordinator()
                coordinator.bind_run(self.run)
                coordinator.pause()
                self.now += timedelta(seconds=2)
                replacement = str(uuid4())
                original_request = self.bridge.request
                original_probe = self.bridge.probe
                original_publish = coordinator._publish_admission_state

                def replace_ack(role, frame, timeout=5):
                    response = original_request(role, frame, timeout)
                    if (boundary == "ack_state" and role is ActorRole.MANAGER
                            and frame["kind"] == "resume"):
                        response = {**response, "state": {**response["state"],
                                                          "sessionId": replacement}}
                    if boundary == "ack" and role is ActorRole.MANAGER and frame["kind"] == "resume":
                        self.bridge.states["manager"].update({"sessionId": replacement,
                                                               "generation": 2})
                    return response

                def replace_publication(state, *, fail_closed, expected_version=None):
                    published = original_publish(state, fail_closed=fail_closed,
                                                 expected_version=expected_version)
                    if boundary == "publication" and state["payload"]["paused"] is False:
                        self.bridge.states["manager"].update({"sessionId": replacement,
                                                               "generation": 2})
                    return published

                def replace_probe(role, timeout=5):
                    state = original_probe(role, timeout)
                    if (boundary == "probe" and role is ActorRole.MANAGER
                            and state.get("paused") is False):
                        self.bridge.states["manager"].update({"sessionId": replacement,
                                                               "generation": 2})
                        return dict(self.bridge.states["manager"])
                    return state

                self.bridge.request = replace_ack
                self.bridge.probe = replace_probe
                coordinator._publish_admission_state = replace_publication
                try:
                    status = coordinator.resume(self.run, self.resume_evidence())
                finally:
                    self.bridge.request = original_request
                    self.bridge.probe = original_probe
                self.assertTrue(status.paused)
                self.assertEqual(coordinator.dispatch_automatic(lambda: "sent"), ("held", None))
                self.assertTrue(self.admission._state["paused"])
                self.assertEqual(coordinator._journal.read()[-1].kind, "resume_failed")
                self.bridge.states["manager"].update({"sessionId": coordinator._binding.manager_session_id,
                                                       "generation": coordinator._binding.manager_generation})

    def test_missing_active_run_stays_held_until_exact_activation_and_rebind(self):
        admission = SerializedReviewAdmission(automation_state=self.base_state)
        coordinator = PauseCoordinator(
            bridge=self.bridge, admission=admission,
            automation_state=lambda: self.base_state,
            request_timeout=.1, stop_timeout=.1, clock=lambda: stamp(self.now),
        )
        self.assertTrue(coordinator.bind_run(self.run).paused)
        called = []
        self.assertEqual(coordinator.dispatch_automatic(
            lambda: called.append("local")), ("held", None))
        self.assertEqual(called, [])
        admission.set_active_run(self.active_ref())
        self.assertTrue(coordinator.status().paused)
        self.assertFalse(coordinator.bind_run(self.run).paused)
        self.assertEqual(coordinator.dispatch_automatic(
            lambda: called.append("exact") or "sent"), ("admitted", "sent"))
        self.assertEqual(called, ["exact"])

    def test_same_identity_new_incarnation_requires_exact_rebind(self):
        old_incarnation = self.coordinator._bound_incarnation
        old_token = self.coordinator._authority_fence.token()
        self.admission.set_active_run(None)
        self.admission.set_active_run(self.active_ref())
        self.assertGreater(self.admission.run_incarnation(), old_incarnation)
        self.assertTrue(self.coordinator.status().paused)
        called = []
        self.assertEqual(self.coordinator.dispatch_automatic(
            lambda: called.append("old")), ("held", None))
        self.assertIsNone(self.coordinator._authority_fence.open(old_token))
        self.assertFalse(self.coordinator.bind_run(self.run).paused)
        self.assertEqual(self.coordinator._bound_incarnation,
                         self.admission.run_incarnation())
        self.assertEqual(self.coordinator.dispatch_automatic(
            lambda: called.append("new") or "sent"), ("admitted", "sent"))
        self.assertEqual(called, ["new"])

    def test_incarnation_switch_at_bind_open_barrier_stays_closed(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        entered, release = Event(), Event()
        original = self.coordinator._peer_identity_current
        results, errors = [], []

        def delayed(binding):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("bind peer check was not released")
            return original(binding)

        self.coordinator._peer_identity_current = delayed
        def bind_one():
            try:
                results.append(self.coordinator.bind_run(self.run))
            except Exception as exc:
                errors.append(exc)

        binding = Thread(target=bind_one)
        binding.start()
        self.assertTrue(entered.wait(1))
        self.admission.set_active_run(None)
        self.admission.set_active_run(self.active_ref())
        try:
            release.set()
            binding.join(2)
        finally:
            release.set()
        self.assertFalse(binding.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(results[0].paused)
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "stale"), ("held", None))
        self.assertFalse(self.coordinator.bind_run(self.run).paused)

    def test_resume_after_admission_switch_to_b_cannot_reopen_a(self):
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        self.admission.set_active_run(ActiveRunRef(
            str(uuid4()), "revision-1", 1, str(uuid4()),
            self.bridge.states["manager"]["sessionId"], 1,
        ))
        before = len(self.bridge.requests)
        status = self.coordinator.resume(self.run, self.resume_evidence())
        self.assertTrue(status.paused)
        self.assertTrue(self.coordinator._authority_fence.closed())
        self.assertFalse(any(frame["kind"] == "resume"
                             for _, frame in self.bridge.requests[before:]))
        self.assertEqual(self.coordinator._journal.read()[-1].kind, "resume_failed")
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "A"), ("held", None))

    def test_active_switch_at_resume_open_barrier_stays_closed(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        entered, release = Event(), Event()
        original = self.coordinator._peer_identity_current
        results, errors = [], []

        def delayed(binding):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("resume peer check was not released")
            return original(binding)

        self.coordinator._peer_identity_current = delayed
        def resume_one():
            try:
                results.append(self.coordinator.resume(self.run, self.resume_evidence()))
            except Exception as exc:
                errors.append(exc)

        resuming = Thread(target=resume_one)
        resuming.start()
        self.assertTrue(entered.wait(2))
        self.admission.set_active_run(ActiveRunRef(
            str(uuid4()), "revision-1", 1, str(uuid4()),
            self.bridge.states["worker"]["sessionId"], 1,
        ))
        try:
            release.set()
            resuming.join(3)
        finally:
            release.set()
        self.assertFalse(resuming.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(results[0].paused)
        self.assertTrue(self.coordinator._authority_fence.closed())
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "A"), ("held", None))

    def test_resume_rejects_manager_switch_after_final_pair_probe(self):
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        original_ready = self.coordinator._post_resume_ready
        calls = 0

        def switch_after_final_probe(binding):
            nonlocal calls
            ready = original_ready(binding)
            calls += 1
            if calls == 2:
                self.bridge.states["manager"].update({"sessionId": str(uuid4()),
                                                       "generation": 2})
            return ready

        self.coordinator._post_resume_ready = switch_after_final_probe
        status = self.coordinator.resume(self.run, self.resume_evidence())
        self.assertTrue(status.paused)
        self.assertTrue(self.coordinator._authority_fence.closed())
        self.assertEqual(self.coordinator._journal.read()[-1].kind, "resume_failed")
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "sent"), ("held", None))

    def test_resume_rejects_manager_switch_during_worker_final_probe(self):
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        original_ready = self.coordinator._post_resume_ready
        original_probe = self.bridge.probe
        ready_calls = 0
        during_final = False

        def final_ready(binding):
            nonlocal ready_calls, during_final
            ready_calls += 1
            during_final = ready_calls == 2
            return original_ready(binding)

        def switch_while_worker_probed(role, timeout=5):
            state = original_probe(role, timeout)
            if during_final and role is ActorRole.WORKER:
                self.bridge.states["manager"].update({"sessionId": str(uuid4()),
                                                       "generation": 2})
            return state

        self.coordinator._post_resume_ready = final_ready
        self.bridge.probe = switch_while_worker_probed
        status = self.coordinator.resume(self.run, self.resume_evidence())
        self.assertTrue(status.paused)
        self.assertTrue(self.coordinator._authority_fence.closed())
        self.assertEqual(self.coordinator._journal.read()[-1].kind, "resume_failed")

    def test_peer_switch_immediately_before_first_review_admission_holds(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        run = self.active_ref()
        self.admission.set_active_run(run)
        self.coordinator.bind_run(self.run)
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        self.assertFalse(self.coordinator.resume(self.run, self.resume_evidence()).paused)
        ticket = self.admission.issue_review(run, 1.0, 0, {})
        self.assertIsNotNone(ticket)
        self.bridge.states["manager"].update({"sessionId": str(uuid4()),
                                               "generation": 2})
        sent = []
        self.assertEqual(self.coordinator.dispatch_automatic(
            lambda: sent.append("local")), ("held", None))
        result = self.admission.admit_and_dispatch(
            ticket, dispatch_review=lambda request: sent.append(request),
        )
        self.assertEqual(result.status, "reconciliation_needed")
        self.assertEqual(sent, [])
        self.assertTrue(self.coordinator._authority_fence.closed())
        self.assertTrue(self.coordinator.status().paused)

    def test_peer_switch_before_first_cw11_callback_never_submits(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        run = self.active_ref()
        self.admission.set_active_run(run)
        self.coordinator.bind_run(self.run)
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        self.assertFalse(self.coordinator.resume(self.run, self.resume_evidence()).paused)
        ticket = self.admission.issue_review(run, 1.0, 0, {})
        self.assertIsNotNone(ticket)
        self.bridge.states["manager"].update({"sessionId": str(uuid4()),
                                               "generation": 2})
        submitted = []
        result = self.admission.admit_and_dispatch(
            ticket, dispatch_review=lambda request: submitted.append(request),
        )
        self.assertEqual(result.status, "reconciliation_needed")
        self.assertEqual(submitted, [])
        self.assertTrue(self.coordinator._authority_fence.closed())
        self.assertEqual(self.coordinator.dispatch_automatic(
            lambda: submitted.append("local")), ("held", None))
        self.assertEqual(submitted, [])

    def test_concurrent_cancellation_records_one_exact_run_event_and_keeps_result(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        run = self.active_ref()
        self.admission.set_active_run(run)
        ticket = self.admission.issue_review(run, 1.0, 0, {})
        self.assertIsNotNone(ticket)
        artifact = self.run.record_dir / "result.json"
        artifact.write_text('{"outcome":"partial"}', encoding="utf-8")
        self.repository.cancel_run(self.run_id, "cancel during status and dispatch polling")
        barrier = Barrier(14)
        errors = []
        sent = []

        def poll(index):
            try:
                barrier.wait(3)
                for _ in range(30):
                    if index % 2:
                        self.coordinator.status()
                    else:
                        self.coordinator.dispatch_automatic(lambda: sent.append(index))
            except Exception as exc:
                errors.append(exc)

        def review():
            try:
                barrier.wait(3)
                self.admission.admit_and_dispatch(
                    ticket, dispatch_review=lambda request: {
                        "status": ("omp_processed" if self.coordinator.dispatch_automatic(
                            lambda: sent.append("review")
                        )[0] == "admitted" else "deferred")
                    },
                )
            except Exception as exc:
                errors.append(exc)

        threads = [Thread(target=poll, args=(index,)) for index in range(12)]
        threads.append(Thread(target=review))
        for thread in threads:
            thread.start()
        barrier.wait(3)
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads), "cancel polling deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(sent, [])
        self.assertTrue(self.coordinator.status().cancelled)
        cancellations = [event for event in self.coordinator._journal.read()
                         if event.kind == "cancelled"]
        self.assertEqual(len(cancellations), 1)
        self.assertEqual(cancellations[0].binding.run_id, self.run_id)
        self.assertEqual(cancellations[0].binding.task_id, self.task_id)
        self.assertEqual(cancellations[0].binding.revision, 1)
        self.assertEqual(artifact.read_text(encoding="utf-8"), '{"outcome":"partial"}')

    def test_repeated_review_local_status_and_pause_races_complete_without_late_actions(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        run = self.active_ref()
        self.admission.set_active_run(run)
        self.coordinator.bind_run(self.run)
        actions = []
        for cycle in range(20):
            barrier = Barrier(5)
            errors = []
            ticket = self.admission.issue_review(run, float(cycle + 1), 0, {})
            self.assertIsNotNone(ticket)

            def actor(operation):
                try:
                    barrier.wait(3)
                    operation()
                except Exception as exc:
                    errors.append(exc)

            def mark_action(source):
                actions.append((cycle, source, self.coordinator.status().paused))
                return "sent"

            operations = [
                lambda: self.admission.admit_and_dispatch(
                    ticket, dispatch_review=lambda request: {
                        "status": ("omp_processed" if self.coordinator.dispatch_automatic(
                            lambda: mark_action("review")
                        )[0] == "admitted" else "deferred")
                    },
                ),
                lambda: self.coordinator.dispatch_automatic(lambda: mark_action("local")),
                lambda: [self.coordinator.status() for _ in range(15)],
                self.coordinator.pause,
            ]
            threads = [Thread(target=actor, args=(operation,)) for operation in operations]
            for thread in threads:
                thread.start()
            barrier.wait(3)
            for thread in threads:
                thread.join(3)
            self.assertFalse(any(thread.is_alive() for thread in threads),
                             f"cycle {cycle} deadlocked")
            self.assertEqual(errors, [], f"cycle {cycle} raised")
            self.assertTrue(self.coordinator.status().paused)
            self.assertTrue(self.admission._state["paused"],
                            f"cycle {cycle} published stale unpaused admission")
            self.now += timedelta(seconds=2)
            self.assertFalse(self.coordinator.resume(self.run, self.resume_evidence()).paused)
            self.assertFalse(self.admission._state["paused"],
                             f"cycle {cycle} did not publish resumed admission")
            self.assertEqual(self.coordinator.dispatch_automatic(
                lambda: mark_action("post_resume"))[0], "admitted")
        self.assertTrue(actions, "stress never exercised an admitted action")
        self.assertTrue(all(not paused for _, _, paused in actions),
                        "an automatic action began after pause")

    def test_high_cycle_bind_pause_admission_has_no_lock_cycle(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        run = self.active_ref()
        self.admission.set_active_run(run)
        self.coordinator.bind_run(self.run)
        for cycle in range(30):
            barrier = Barrier(4)
            errors = []
            sent = []
            ticket = self.admission.issue_review(run, float(cycle + 1), 0, {})
            self.assertIsNotNone(ticket)

            def actor(action):
                try:
                    barrier.wait(3)
                    action()
                except Exception as exc:
                    errors.append(exc)

            threads = [
                Thread(target=actor, args=(lambda: self.coordinator.bind_run(self.run),)),
                Thread(target=actor, args=(self.coordinator.pause,)),
                Thread(target=actor, args=(lambda: self.admission.admit_and_dispatch(
                    ticket, dispatch_review=lambda request: {
                        "status": ("omp_processed" if self.coordinator.dispatch_automatic(
                            lambda: sent.append(self.coordinator.status().paused)
                        )[0] == "admitted" else "deferred")
                    },
                ),)),
            ]
            for thread in threads:
                thread.start()
            barrier.wait(3)
            for thread in threads:
                thread.join(3)
            self.assertFalse(any(thread.is_alive() for thread in threads),
                             f"cycle {cycle} deadlocked")
            self.assertEqual(errors, [], f"cycle {cycle} raised")
            self.assertTrue(self.coordinator.status().paused)
            self.assertTrue(self.admission._state["paused"],
                            f"cycle {cycle} left mirror unpaused")
            self.assertTrue(all(not paused for paused in sent),
                            f"cycle {cycle} dispatched after pause")
            self.now += timedelta(seconds=2)
            self.assertFalse(self.coordinator.resume(self.run, self.resume_evidence()).paused)

    def test_long_local_action_does_not_delay_pause_abort_or_new_fence(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        entered, release = Event(), Event()
        results = []
        finished = []

        def long_action():
            entered.set()
            self.assertTrue(release.wait(2))
            finished.append("completed")
            return "completed"

        action = Thread(target=lambda: results.append(
            self.coordinator.dispatch_automatic(long_action)))
        action.start()
        self.assertTrue(entered.wait(1))
        pausing = Thread(target=self.coordinator.pause)
        pausing.start()
        deadline = time.monotonic() + 1
        while not any(role == "manager" and frame["kind"] == "pause"
                      for role, frame in self.bridge.requests) and time.monotonic() < deadline:
            time.sleep(.01)
        try:
            self.assertTrue(any(role == "manager" and frame["kind"] == "pause"
                                for role, frame in self.bridge.requests),
                            "manager abort waited for in-flight local action")
            self.assertEqual(self.coordinator.dispatch_automatic(
                lambda: "late"), ("held", None))
        finally:
            release.set()
            action.join(2)
            pausing.join(2)
        self.assertFalse(action.is_alive() or pausing.is_alive())
        self.assertEqual(finished, ["completed"])
        self.assertEqual(results, [("held", None)])
        self.assertTrue(self.coordinator.status().paused)

    def test_second_pause_does_not_invalidate_first_manager_abort_token(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        entered, release = Event(), Event()
        original = self.coordinator._bound_request
        manager_tokens = []

        def delayed(role, frame, binding, token, *, pair=False):
            if role is ActorRole.MANAGER and frame["kind"] == "pause" and not manager_tokens:
                manager_tokens.append(token)
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("first manager pause was not released")
            return original(role, frame, binding, token, pair=pair)

        self.coordinator._bound_request = delayed
        first = Thread(target=self.coordinator.pause)
        first.start()
        self.assertTrue(entered.wait(1))
        second = Thread(target=self.coordinator.pause)
        second.start()
        try:
            self.assertTrue(manager_tokens[0].current(),
                            "redundant pause invalidated in-flight manager abort")
        finally:
            release.set()
            first.join(2)
            second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertTrue(any(role == "manager" and frame["kind"] == "pause"
                            for role, frame in self.bridge.requests))
        self.assertTrue(self.coordinator.status().paused)

    def test_new_pause_close_overtakes_resume_before_cas_open(self):
        self.run.repository = ThreadReadableRepository(self.repository.path)
        self.coordinator.pause()
        self.now += timedelta(seconds=2)
        opening, release = Event(), Event()
        original_open = self.coordinator._authority_fence.open
        results, errors = [], []

        def delayed_open(expected):
            opening.set()
            if not release.wait(2):
                raise TimeoutError("resume open was not released")
            return original_open(expected)

        self.coordinator._authority_fence.open = delayed_open
        def resume_one():
            try:
                results.append(self.coordinator.resume(self.run, self.resume_evidence()))
            except Exception as exc:
                errors.append(exc)

        resumed = Thread(target=resume_one)
        resumed.start()
        self.assertTrue(opening.wait(2))
        pausing = Thread(target=self.coordinator.pause)
        pausing.start()
        deadline = time.monotonic() + 1
        while self.coordinator._authority_fence.token().reason != "paused" and time.monotonic() < deadline:
            time.sleep(.01)
        try:
            self.assertTrue(self.coordinator._authority_fence.closed())
        finally:
            release.set()
            resumed.join(3)
            pausing.join(3)
        self.assertFalse(resumed.is_alive() or pausing.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(results[0].paused)
        self.assertTrue(self.coordinator._authority_fence.closed())
        self.assertEqual(self.coordinator.dispatch_automatic(lambda: "late"), ("held", None))

    def test_delayed_cancelled_a_lookup_cannot_close_bound_b(self):
        entered, release = Event(), Event()
        base = ThreadReadableRepository(self.repository.path)

        class DelayedRepository(ThreadReadableRepository):
            def get_run(self, run_id):
                if run_id == self_run_id:
                    entered.set()
                    if not release.wait(2):
                        raise TimeoutError("A repository lookup was not released")
                return super().get_run(run_id)

        self_run_id = self.run_id
        self.run.repository = DelayedRepository(self.repository.path)
        second_task = self.repository.create_task({"goal": "second approved task"})
        self.repository.approve_scope(second_task, 1, {"paths": ["second.txt"]})
        self.repository.proceed(second_task, 1, "start second run")
        second_run_id = self.repository.start_run(second_task, 1)
        task = self.repository.get_task_spec(second_task, 1)
        approval = self.repository.get_decisions(second_task, 1)[0]
        second_dir = self.root / "artifacts" / second_run_id
        second_dir.mkdir()
        second_run = SimpleNamespace(
            repository=base, task_id=second_task, revision=1,
            run_id=second_run_id, record_dir=second_dir,
            approval_hash=sha256(canonical({"task": task, "approval": approval})).hexdigest(),
        )
        self.repository.cancel_run(self.run_id, "cancel A during lookup")
        errors = []

        def inspect_a():
            try:
                self.coordinator.status()
            except Exception as exc:
                errors.append(exc)

        lookup = Thread(target=inspect_a)
        lookup.start()
        self.assertTrue(entered.wait(1))
        self.admission.set_active_run(self.active_ref(second_task, second_run_id))
        self.assertFalse(self.coordinator.bind_run(second_run).paused)
        try:
            release.set()
            lookup.join(2)
        finally:
            release.set()
        self.assertFalse(lookup.is_alive())
        self.assertEqual(errors, [])
        status = self.coordinator.status()
        self.assertEqual(status.binding.run_id, second_run_id)
        self.assertFalse(status.paused or status.cancelled)
        self.assertFalse(self.coordinator._authority_fence.closed())

    def test_delayed_a_repository_error_cannot_close_bound_b(self):
        entered, release = Event(), Event()
        old_run_id = self.run_id

        class FailingRepository(ThreadReadableRepository):
            def get_run(self, run_id):
                if run_id == old_run_id:
                    entered.set()
                    if not release.wait(2):
                        raise TimeoutError("A repository lookup was not released")
                    raise OSError("A metadata reader failed after replacement")
                return super().get_run(run_id)

        self.run.repository = FailingRepository(self.repository.path)
        second_task = self.repository.create_task({"goal": "second approved task"})
        self.repository.approve_scope(second_task, 1, {"paths": ["second.txt"]})
        self.repository.proceed(second_task, 1, "start second run")
        second_run_id = self.repository.start_run(second_task, 1)
        task = self.repository.get_task_spec(second_task, 1)
        approval = self.repository.get_decisions(second_task, 1)[0]
        second_dir = self.root / "artifacts" / second_run_id
        second_dir.mkdir()
        second_run = SimpleNamespace(
            repository=ThreadReadableRepository(self.repository.path),
            task_id=second_task, revision=1, run_id=second_run_id,
            record_dir=second_dir,
            approval_hash=sha256(canonical({"task": task, "approval": approval})).hexdigest(),
        )
        errors = []

        def inspect_a():
            try:
                self.coordinator.status()
            except Exception as exc:
                errors.append(exc)

        lookup = Thread(target=inspect_a, daemon=True)
        lookup.start()
        self.assertTrue(entered.wait(1))
        try:
            self.admission.set_active_run(self.active_ref(second_task, second_run_id))
            self.assertFalse(self.coordinator.bind_run(second_run).paused)
        finally:
            release.set()
            lookup.join(2)
        self.assertFalse(lookup.is_alive())
        self.assertEqual(errors, [])
        status = self.coordinator.status()
        self.assertEqual(status.binding.run_id, second_run_id)
        self.assertFalse(status.paused or status.cancelled)
        self.assertIsNone(status.persistence_error)
        self.assertFalse(self.coordinator._authority_fence.closed())

    def test_corrupt_or_foreign_journal_binding_fails_closed_on_bind(self):
        self.coordinator.pause()
        path = self.coordinator._journal.path
        with path.open("ab") as handle:
            handle.write(b"{partial")
        restarted = self.make_coordinator()
        status = restarted.bind_run(self.run)
        self.assertTrue(status.paused)
        self.assertEqual(status.persistence_error, "PauseJournalError")
        self.assertEqual(restarted.dispatch_automatic(lambda: "sent"), ("held", None))

        foreign_dir = self.root / "foreign-artifacts"
        foreign_dir.mkdir()
        foreign_run = SimpleNamespace(**{**vars(self.run), "record_dir": foreign_dir})
        foreign_binding = replace(self.coordinator.status().binding,
                                  approved_scope_hash="f" * 64)
        PauseJournal(foreign_dir / "pause.jsonl").append(
            pause_id="foreign", kind="pause_requested", occurred_at=stamp(self.now),
            binding=foreign_binding,
        )
        another = self.make_coordinator()
        foreign_status = another.bind_run(foreign_run)
        self.assertTrue(foreign_status.paused)
        self.assertEqual(foreign_status.persistence_error, "binding_mismatch")
        self.assertEqual(another.dispatch_automatic(lambda: "sent"), ("held", None))

    def test_directory_and_device_journal_reads_reject_promptly(self):
        with self.assertRaises(PauseJournalError):
            PauseJournal(self.run.record_dir).read()
        with self.assertRaises(PauseJournalError):
            PauseJournal("/dev/null").read()


if __name__ == "__main__":
    unittest.main()
