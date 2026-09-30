"""Live G3 socket and persistent-shell CW-15 composition regression."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from workbench.app import (
    CommandState, ControlState, LifecycleHeld, LifecycleJournal, LifecycleRecord, PeerRef,
    RecoveryStopAdapter, bind_production,
)
from workbench.contracts.v1 import MessageKind
from workbench.ipc.bridge_g3.mailbox import (
    BridgeBoundMismatch, G3BridgeServer, MailboxError, MailboxStatus, TaskMailbox,
)
from workbench.observation.worker_review import (
    ActiveRunRef, SerializedReviewAdmission, WorkerReviewScheduler,
)
from workbench.policy.pause_automation.controller import PauseCoordinator
from workbench.policy.recovery_manager import (
    CompletionCriteria, RecoveryCoordinator, RecoveryFacts, RunIdentity,
    RunObservation,
)
from workbench.runtime.process_evidence import LinuxProcessProbe, ProcessRef
from workbench.storage.log_raw import MetadataAdmissionGate, RawLogStore
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell


READY = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True,
    "approvalValid": True,
}}


class ScriptedPeer:
    """Bound local G3 client using public probe/deliver acknowledgements."""

    def __init__(self, bridge: G3BridgeServer, role: str, generation: int = 1):
        self.role = role
        self.session = str(uuid4())
        self.generation = generation
        self.deliveries: list[str] = []
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(2)
        self.socket.connect(str(bridge.socket_path))
        hello = {"kind": "hello", "protocolVersion": 1, "role": role,
                 "ompSessionId": self.session, "generation": generation,
                 "pid": os.getpid(), "token": f"{role}-token"}
        self.socket.sendall((json.dumps(hello) + "\n").encode())
        assert json.loads(self.socket.recv(4096).split(b"\n")[0])["kind"] == "ready"
        self.socket.settimeout(None)
        self.thread = Thread(target=self._serve, name=f"cw15-positive-{role}")
        self.thread.start()

    def _serve(self) -> None:
        stream = self.socket.makefile("rb")
        try:
            for raw in stream:
                frame = json.loads(raw)
                if frame.get("kind") == "probe":
                    state = {"role": self.role, "sessionId": self.session,
                             "generation": self.generation, "idle": True, "pending": False,
                             "approvalPending": False, "editorKnown": True,
                             "editorEmpty": True, "inFlightToolCount": 0,
                             "paused": False}
                    response = {"kind": "api_ack", "requestId": frame["requestId"],
                                "status": "state", "state": state}
                    self.socket.sendall((json.dumps(response) + "\n").encode())
                elif frame.get("kind") == "deliver":
                    envelope = json.loads(frame["envelope"])
                    self.deliveries.append(envelope["messageId"])
                    response = {"kind": "api_ack", "requestId": frame["requestId"],
                                "status": "api_accepted"}
                    self.socket.sendall((json.dumps(response) + "\n").encode())
                    event = {"kind": "omp_event", "name": "delivery_omp_processed",
                             "sessionId": self.session, "generation": self.generation,
                             "messageId": envelope["messageId"],
                             "deliveryAttemptId": envelope["deliveryAttemptId"],
                             "taskId": envelope["taskId"],
                             "revisionId": envelope["revisionId"],
                             "runId": envelope["runId"],
                             "providerRequestMatched": True,
                             "providerResponseObserved": True,
                             "agentEndObserved": True}
                    self.socket.sendall((json.dumps(event) + "\n").encode())
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    def close(self) -> None:
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("G3 stand-in thread residue")


class ProductionBindingTests(unittest.TestCase):
    def test_g3_final_claim_orders_takeover_against_socket_submission(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cw15-claim-")))
            bridge = stack.enter_context(G3BridgeServer(
                root / "bridge.sock", {"manager": "manager-token", "worker": "worker-token"}))
            worker = ScriptedPeer(bridge, "worker")
            stack.callback(worker.close)
            original_send = bridge._send_until
            frames: list[str] = []
            active_trace: dict[str, int] = {}

            def counted_send(connection, payload, deadline):
                frames.append(json.loads(payload)["kind"])
                active_trace["frame"] = time.monotonic_ns()
                return original_send(connection, payload, deadline)

            class OrderedToken:
                def __init__(self, order: str):
                    self.order = order
                    self.checked = Event()
                    self.claimed = Event()
                    self.release = Event()
                    self.revoked = False
                    self.trace: dict[str, int] = {}

                def current(self):
                    self.trace["current_enter"] = time.monotonic_ns()
                    if self.order == "revoke_first":
                        self.checked.set()
                        if not self.release.wait(2):
                            raise TimeoutError("revoke did not arrive")
                    self.trace["current_return"] = time.monotonic_ns()
                    return True  # A stale check alone must never authorize send.

                def claim(self):
                    self.trace["claim_enter"] = time.monotonic_ns()
                    if self.revoked:
                        self.trace["claim_rejected"] = time.monotonic_ns()
                        return False
                    self.claimed.set()
                    if self.order == "claim_first" and not self.release.wait(2):
                        raise TimeoutError("revoke did not arrive")
                    self.trace["claim_return"] = time.monotonic_ns()
                    return True

            payload = {"kind": "deliver", "envelope": json.dumps({
                "messageId": str(uuid4()), "deliveryAttemptId": str(uuid4()),
                "taskId": str(uuid4()), "revisionId": str(uuid4()),
                "runId": str(uuid4()),
            })}
            order_traces = {}
            with patch.object(bridge, "_send_until", side_effect=counted_send):
                for order in ("revoke_first", "claim_first"):
                    token = OrderedToken(order)
                    active_trace = token.trace
                    outcome: dict[str, object] = {}

                    def submit():
                        try:
                            outcome["ack"] = bridge.request("worker", payload, timeout=1,
                                                             authority_token=token)
                        except BaseException as exc:
                            outcome["error"] = exc
                        finally:
                            token.trace["return"] = time.monotonic_ns()

                    sender = Thread(target=submit, daemon=True)
                    sender.start()
                    gate = token.checked if order == "revoke_first" else token.claimed
                    self.assertTrue(gate.wait(1), f"{order} final boundary not reached")
                    token.trace["revoke"] = time.monotonic_ns()
                    token.revoked = True
                    token.release.set()
                    sender.join(2)
                    self.assertFalse(sender.is_alive(), f"{order} deadlocked")
                    if order == "revoke_first":
                        self.assertIsInstance(outcome.get("error"), BridgeBoundMismatch)
                        self.assertEqual(frames, [])
                        self.assertEqual(worker.deliveries, [])
                        self.assertLess(token.trace["current_enter"], token.trace["revoke"])
                        self.assertLessEqual(token.trace["revoke"], token.trace["current_return"])
                        self.assertLess(token.trace["current_return"], token.trace["claim_enter"])
                        self.assertLessEqual(token.trace["claim_enter"], token.trace["claim_rejected"])
                    else:
                        self.assertEqual(outcome["ack"]["status"], "api_accepted")
                        self.assertEqual(frames, ["deliver"])
                        self.assertEqual(len(worker.deliveries), 1)
                        self.assertLess(token.trace["current_return"], token.trace["claim_enter"])
                        self.assertLess(token.trace["claim_enter"], token.trace["revoke"])
                        self.assertLess(token.trace["revoke"], token.trace["claim_return"])
                        self.assertLess(token.trace["claim_return"], token.trace["frame"])
                        self.assertLess(token.trace["frame"], token.trace["return"])
                    self.assertEqual(bridge._pending_acks, {})
                    order_traces[order] = dict(token.trace)
            self.claim_traces = {"orders": order_traces, "frames": list(frames),
                                 "deliveries": list(worker.deliveries)}

            for token, error in (
                (SimpleNamespace(current=False, claim=lambda: True), MailboxError),
                (SimpleNamespace(current=lambda: False, claim=lambda: True), BridgeBoundMismatch),
                (SimpleNamespace(current=lambda: 1, claim=lambda: True), BridgeBoundMismatch),
                (SimpleNamespace(current=lambda: (_ for _ in ()).throw(
                    RuntimeError("current unavailable")), claim=lambda: True), BridgeBoundMismatch),
                (SimpleNamespace(current=lambda: True, claim=lambda: 1), BridgeBoundMismatch),
                (SimpleNamespace(current=lambda: True, claim=lambda: (_ for _ in ()).throw(
                    RuntimeError("claim unavailable"))), BridgeBoundMismatch),
                (SimpleNamespace(current=lambda: True, claim=lambda: False), BridgeBoundMismatch),
                (SimpleNamespace(current=lambda: True, claim=False), MailboxError),
            ):
                with self.assertRaises(error):
                    bridge.request("worker", payload, timeout=.5, authority_token=token)
                self.assertEqual(bridge._pending_acks, {})
            self.assertEqual(frames, ["deliver"])
            self.assertEqual(len(worker.deliveries), 1)
            plain_payload = {**payload, "envelope": json.dumps({
                **json.loads(payload["envelope"]), "messageId": str(uuid4()),
                "deliveryAttemptId": str(uuid4()),
            })}
            active_trace = {}
            with patch.object(bridge, "_send_until", side_effect=counted_send):
                valid = bridge.request(
                    "worker", plain_payload, timeout=.5,
                    authority_token=SimpleNamespace(current=lambda: True,
                                                    claim=lambda: True))
            self.assertEqual(valid["status"], "api_accepted")
            self.assertEqual(frames, ["deliver", "deliver"])
            self.assertEqual(len(worker.deliveries), 2)
            self.assertEqual(bridge._pending_acks, {})

    def test_clean_git_baseline_allows_bound_positive_delivery_exactly_once(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(
                tempfile.TemporaryDirectory(prefix="cw15-positive-")))
            execution = root / "execution"
            execution.mkdir()
            for command in (("git", "init", "-q", str(execution)),
                            ("git", "-C", str(execution), "config", "user.name", "CW15 fixture"),
                            ("git", "-C", str(execution), "config", "user.email", "cw15@example.invalid"),
                            ("git", "-C", str(execution), "config", "core.filemode", "true")):
                subprocess.run(command, check=True, capture_output=True, timeout=5)
            (execution / "result.txt").write_text("clean baseline\n", encoding="utf-8")
            (execution / "result.txt").chmod(0o644)
            for command in (("git", "-C", str(execution), "add", "result.txt"),
                            ("git", "-C", str(execution), "commit", "-q", "-m", "fixture baseline")):
                subprocess.run(command, check=True, capture_output=True, timeout=5)
            baseline = subprocess.run(("git", "-C", str(execution), "rev-parse", "HEAD"),
                                      check=True, capture_output=True, timeout=5).stdout.strip()
            self.assertEqual(len(baseline), 40)
            self.assertEqual(subprocess.run(
                ("git", "-C", str(execution), "status", "--porcelain"),
                check=True, capture_output=True, timeout=5).stdout, b"")

            original_cwd = Path.cwd()
            os.chdir(execution)  # The authenticated peer PID has this stable cwd.
            stack.callback(os.chdir, original_cwd)
            repository = TaskRepository(root / "tasks.sqlite3")
            stack.callback(repository.close)
            bridge = stack.enter_context(G3BridgeServer(
                root / "bridge.sock", {"manager": "manager-token", "worker": "worker-token"}))
            manager = ScriptedPeer(bridge, "manager")
            stack.callback(manager.close)
            worker = ScriptedPeer(bridge, "worker")
            stack.callback(worker.close)
            shell = stack.enter_context(PersistentShell(
                user_environment={"PATH": "/usr/bin:/bin", "TERM": "xterm"},
                choice=ShellChoice("bash", "/bin/bash")))
            shell.send_user(b"wb-handoff\n")
            deadline = time.monotonic() + 2
            while shell.poll(.02)["parent_mode"] != "control_wait":
                self.assertLess(time.monotonic(), deadline, "shell handoff timed out")
            shell_state = shell.claim_manager()
            self.assertEqual(shell_state["input_owner"], "manager")

            task = repository.create_task({"goal": "bound positive delivery"})
            repository.approve_scope(task, 1, {"paths": ["result.txt"]})
            repository.proceed(task, 1, "start")
            run_id = repository.start_run(task, 1)
            task_record = repository.get_task_spec(task, 1)
            approval = repository.get_decisions(task, 1)[0]
            approval_hash = sha256(json.dumps(
                {"task": task_record, "approval": approval}, sort_keys=True,
                separators=(",", ":"), ensure_ascii=False,
            ).encode()).hexdigest()
            run = SimpleNamespace(repository=repository, task_id=task, revision=1,
                                  run_id=run_id, approval_hash=approval_hash,
                                  record_dir=root / "artifacts" / run_id,
                                  worktree=SimpleNamespace(path=execution))
            run.record_dir.mkdir(parents=True)
            mailbox = TaskMailbox(repository, bridge)
            message = mailbox.create_message(task, 1, run_id, "manager", "worker",
                                             MessageKind.TASK,
                                             {"instruction": "bounded stand-in"})
            probe = LinuxProcessProbe()
            own_ticks = probe.start_ticks(os.getpid())
            shell_ticks = probe.start_ticks(shell.parent_pid)
            shell_ref = ProcessRef("shell", shell.parent_pid, shell_ticks,
                                   shell_state["owner_epoch"])
            journal = LifecycleJournal(root / "lifecycle.json")
            journal.commit(None, LifecycleRecord(
                task_id=task, revision=1, run_id=run_id, approval_hash=approval_hash,
                generation=1, boot_marker="boot", boot_confirmed_marker=None,
                manager=PeerRef("manager", manager.session, 1,
                                ProcessRef("manager", os.getpid(), own_ticks, 1)),
                worker=PeerRef("worker", worker.session, 1,
                               ProcessRef("worker", os.getpid(), own_ticks, 1)),
                shell=shell_ref,
                run_targets=(),
                control=ControlState(shell_state["input_owner"],
                                     shell_state["owner_epoch"],
                                     shell_state["parent_mode"],
                                     shell_state["takeover_requested"],
                                     shell_state["takeover_confirmed"]),
            ))
            admission = SerializedReviewAdmission(automation_state=READY)
            active = ActiveRunRef(task, str(uuid5(UUID(task), "task-spec-revision:1")),
                                  1, run_id, worker.session, 1)
            admission.set_active_run(active)
            pause = PauseCoordinator(bridge=bridge, admission=admission,
                                     automation_state=lambda: READY, request_timeout=.5)
            status = pause.bind_run(run)
            self.assertFalse(status.paused, status)
            self.assertIsNone(status.persistence_error)
            entry = json.loads((run.record_dir / "entry-file-baseline.json").read_text())
            self.assertEqual(entry["execution_index_flags"], {})
            self.assertEqual(entry["execution_tracked_modes"], {"result.txt": "v3:100644"})
            for role in ("manager", "worker"):
                self.assertEqual(entry["peers"][role]["git"]["head"], baseline.decode())
            review = WorkerReviewScheduler(
                admission=admission, automation_state=lambda: READY,
                active_run=lambda: active, worker_state=lambda _: None,
                collect_non_model=lambda _: None, dispatch_review=lambda _: None,
                user_priority=lambda _: False, on_exit=lambda _: None,
                clock=lambda: 0.0,
            )
            raw = RawLogStore(root / "raw")
            stack.callback(raw.close)
            app = bind_production(
                journal=journal, bridge=bridge, mailbox=mailbox, message=message,
                shell=shell, pause=pause, review=review,
                recovery=RecoveryCoordinator(object()), raw=raw,
                metadata=MetadataAdmissionGate(),
                model=SimpleNamespace(check=lambda: True),
                termination=lambda: None, boot_marker=lambda: "boot",
                authority=lambda: True,
                normal_stop=lambda _: self.fail("unexpected stop"),
                drain=lambda _: None,
            )
            app.commands.timeout = 1
            self.assertTrue(app.reconcile().ready)
            self.assertEqual(app.dispatch_command(message.message_id), "omp_processed")
            self.assertEqual(worker.deliveries, [message.message_id])
            self.assertEqual(journal.read().command.delivery, "omp_processed")
            receipt = mailbox.deliver(message, timeout=.2)
            self.assertEqual(receipt.status, MailboxStatus.OMP_PROCESSED)
            history = repository.get_delivery_history(receipt.delivery_attempt_id)
            self.assertEqual([event["status"] for event in history],
                             ["attempted", "api_returned", "omp_processed"])
            with self.assertRaises(LifecycleHeld):
                app.dispatch_command(message.message_id)
            self.assertEqual(worker.deliveries, [message.message_id])
            self.assertFalse(pause.status().paused, pause.status())

            def throwing_current():
                raise RuntimeError("external authority unavailable")

            with patch.object(mailbox, "deliver", wraps=mailbox.deliver) as delivery:
                for name, token in (
                    ("missing_current", object()),
                    ("throwing_current", SimpleNamespace(current=throwing_current)),
                    ("false_current", SimpleNamespace(current=lambda: False)),
                    ("non_boolean_current", SimpleNamespace(current=lambda: 1)),
                ):
                    with self.subTest(authority_token=name):
                        candidate = mailbox.create_message(
                            task, 1, run_id, "manager", "worker", MessageKind.TASK,
                            {"instruction": name})
                        if name == "missing_current":
                            with self.assertRaises(TypeError):
                                pause.dispatch_bound_automatic(
                                    mailbox, candidate, authority_token=token)
                        else:
                            self.assertEqual(pause.dispatch_bound_automatic(
                                mailbox, candidate, authority_token=token), ("held", None))
                self.assertEqual(delivery.call_count, 0)
            self.assertEqual(worker.deliveries, [message.message_id])

            # A second bound command stops after probe but before G3's final
            # authority check. Takeover intent must invalidate that check now.
            race_message = mailbox.create_message(task, 1, run_id, "manager", "worker",
                                                  MessageKind.TASK,
                                                  {"instruction": "race stand-in"})
            race_journal = LifecycleJournal(root / "lifecycle-race.json")
            race_journal.commit(None, replace(journal.read(), generation=1,
                                              command=CommandState()))
            race_app = bind_production(
                journal=race_journal, bridge=bridge, mailbox=mailbox,
                message=race_message, shell=shell, pause=pause, review=review,
                recovery=RecoveryCoordinator(object()), raw=raw,
                metadata=MetadataAdmissionGate(),
                model=SimpleNamespace(check=lambda: True),
                termination=lambda: None, boot_marker=lambda: "boot",
                authority=lambda: True,
                normal_stop=lambda _: self.fail("unexpected stop"), drain=lambda _: None,
            )
            race_app.commands.timeout = .5
            probe_done, takeover_started = Event(), Event()
            results, errors, timestamps, submitted = {}, {}, {}, []
            original_request = bridge.request
            original_send = bridge._send_until
            original_commit = race_journal.commit
            intent = race_app._takeover_intent
            original_set = intent.set

            def after_probe(role, frame, timeout=5, **kwargs):
                if frame.get("kind") == "deliver":
                    timestamps["deliver_request"] = time.monotonic_ns()
                    results["pending"] = race_journal.read().command
                    self.assertEqual(results["pending"].delivery, "unknown")
                    probe_done.set()
                    self.assertTrue(takeover_started.wait(3), "takeover did not start")
                    self.assertTrue(intent.wait(1), "takeover intent was not immediate")
                    timestamps["intent_observed"] = time.monotonic_ns()
                    self.assertFalse(race_journal.read().control.takeover_requested)
                try:
                    return original_request(role, frame, timeout, **kwargs)
                finally:
                    if frame.get("kind") == "deliver":
                        timestamps["g3_final_return"] = time.monotonic_ns()

            def recorded_commit(expected, updated):
                result = original_commit(expected, updated)
                if (updated.control.takeover_requested
                        and not expected.control.takeover_requested):
                    timestamps["durable_takeover"] = time.monotonic_ns()
                return result

            def recorded_intent():
                generation = original_set()
                timestamps["intent_set"] = time.monotonic_ns()
                return generation

            def recorded_send(connection, payload, deadline):
                if json.loads(payload).get("kind") == "deliver":
                    submitted.append(time.monotonic_ns())
                return original_send(connection, payload, deadline)

            def takeover_race():
                if not probe_done.wait(3):
                    errors["takeover"] = TimeoutError("G3 boundary not reached")
                    return
                timestamps["takeover_invocation"] = time.monotonic_ns()
                takeover_started.set()
                try:
                    results["takeover"] = race_app.request_takeover()
                except BaseException as exc:
                    errors["takeover"] = exc

            with (patch.object(bridge, "request", side_effect=after_probe),
                  patch.object(bridge, "_send_until", side_effect=recorded_send),
                  patch.object(race_journal, "commit", side_effect=recorded_commit),
                  patch.object(intent, "set", side_effect=recorded_intent)):
                taker = Thread(target=takeover_race, daemon=True)
                taker.start()
                try:
                    results["command"] = race_app.dispatch_command(race_message.message_id)
                except BaseException as exc:
                    errors["command"] = exc
                finally:
                    timestamps["command_unwind"] = time.monotonic_ns()
                    taker.join(3)
            self.assertFalse(taker.is_alive(), "G3/takeover deadlock")
            self.assertTrue(probe_done.is_set())
            self.assertEqual(submitted, [], "deliver frame sent after takeover invocation")
            self.assertEqual(errors.get("takeover"), None)
            self.assertIsInstance(errors.get("command"), LifecycleHeld)
            self.assertIn("rejected before API submission", str(errors["command"]))
            self.assertLessEqual(timestamps["takeover_invocation"], timestamps["intent_set"])
            self.assertLessEqual(timestamps["intent_set"], timestamps["intent_observed"])
            self.assertLess(timestamps["intent_observed"], timestamps["g3_final_return"])
            self.assertLess(timestamps["g3_final_return"], timestamps["durable_takeover"])
            self.assertTrue(results["takeover"].takeover_requested)
            self.assertEqual(race_journal.read().command, results["pending"])
            self.assertEqual(worker.deliveries, [message.message_id])
            rejected = mailbox.deliver(race_message, timeout=.1)
            self.assertEqual(rejected.status, MailboxStatus.REJECTED)
            self.assertIs(rejected.details["api_called"], False)
            self.assertEqual([event["status"] for event in
                              repository.get_delivery_history(rejected.delivery_attempt_id)],
                             ["attempted", "failed"])
            self.intent_trace = {"timestamps_ns": dict(timestamps),
                                 "deliver_frames": len(submitted),
                                 "receipt_status": rejected.status.value,
                                 "receipt_api_called": rejected.details["api_called"],
                                 "command_delivery": race_journal.read().command.delivery}
            worker.close()
            self.assertTrue(self._disconnected(bridge))
            replacement = ScriptedPeer(bridge, "worker", generation=2)
            stack.callback(replacement.close)
            self.assertFalse(app.reconcile().ready)
            with self.assertRaises(LifecycleHeld):
                app.dispatch_command(message.message_id)
            self.assertEqual(replacement.deliveries, [])
            self.assertEqual(worker.deliveries, [message.message_id])
            self.assertEqual(app.observe_raw(b"raw evidence").stored_bytes, 12)
        self.assertEqual(probe.observe(shell_ref).state, "dead")
        self.assertFalse(root.exists())
        self.assertFalse(manager.thread.is_alive())
        self.assertFalse(worker.thread.is_alive())
        self.assertFalse(replacement.thread.is_alive())

    def test_recovery_stop_and_force_are_cw13_decision_gated(self):
        identity = RunIdentity(str(uuid4()), 1, str(uuid4()))
        facts = RecoveryFacts(
            identity=identity, current_identity=identity,
            approval_hash="a" * 64, current_approval_hash="a" * 64,
            criteria=CompletionCriteria("done", "result.txt", "pass"),
            approved_paths=("result.txt",), automation_state=READY,
            revoked=False, metadata_healthy=True, terminal_owner="workbench",
            settings_revision=1, authority_version=1,
            observation=RunObservation(identity, 1, "running", None, False,
                                       "running", False, False, False, False),
            worker=None, raw_log=None, result=None, worktree_root="/tmp",
            raw_log_path="/tmp/raw.log", attempts=(),
        )
        port = type("Port", (), {"read": lambda self, _: facts})()
        probe = LinuxProcessProbe()
        ref = ProcessRef("worker", os.getpid(), probe.start_ticks(os.getpid()), 1)
        requested = []
        adapter = RecoveryStopAdapter(RecoveryCoordinator(port), identity, probe,
                                      requested.append, lambda _: None)
        adapter.normal_stop(ref)
        self.assertEqual(requested, [ref])
        with self.assertRaises(LifecycleHeld):
            adapter.force_exact(ref)

    def test_real_g3_peers_mailbox_and_persistent_shell_bind_fail_closed(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cw15-bind-")))
            repository = TaskRepository(root / "tasks.sqlite3")
            stack.callback(repository.close)
            bridge = stack.enter_context(G3BridgeServer(
                root / "bridge.sock", {"manager": "manager-token", "worker": "worker-token"},
            ))
            sessions = {}
            clients = {}
            for role in ("manager", "worker"):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                stack.callback(client.close)
                client.settimeout(1)
                client.connect(str(bridge.socket_path))
                session = str(uuid4())
                hello = {"kind": "hello", "protocolVersion": 1, "role": role,
                         "ompSessionId": session, "generation": 1,
                         "pid": os.getpid(), "token": f"{role}-token"}
                client.sendall((json.dumps(hello) + "\n").encode())
                self.assertEqual(json.loads(client.recv(4096).split(b"\n")[0])["kind"], "ready")
                self.assertEqual(bridge.peer(role, timeout=1).session_id, session)
                sessions[role] = session
                clients[role] = client

            shell = stack.enter_context(PersistentShell(
                user_environment={"PATH": "/usr/bin:/bin", "TERM": "xterm"},
                choice=ShellChoice("bash", "/bin/bash"),
            ))
            shell_state = shell.snapshot()
            self.assertEqual(shell_state["input_owner"], "user")
            probe = LinuxProcessProbe()
            own_ticks = probe.start_ticks(os.getpid())
            shell_ticks = probe.start_ticks(shell.parent_pid)
            self.assertIsNotNone(own_ticks)
            self.assertIsNotNone(shell_ticks)
            task = repository.create_task({"goal": "live CW15 binding"})
            repository.approve_scope(task, 1, {"paths": []})
            repository.proceed(task, 1, "start")
            run = repository.start_run(task, 1)
            mailbox = TaskMailbox(repository, bridge)
            message = mailbox.create_message(task, 1, run, "manager", "worker",
                                             MessageKind.TASK, {"instruction": "bounded"})
            journal = LifecycleJournal(root / "lifecycle.json")
            record = LifecycleRecord(
                task_id=task, revision=1, run_id=run, approval_hash="a" * 64,
                generation=1, boot_marker="boot", boot_confirmed_marker=None,
                manager=PeerRef("manager", sessions["manager"], 1,
                                ProcessRef("manager", os.getpid(), own_ticks, 1)),
                worker=PeerRef("worker", sessions["worker"], 1,
                               ProcessRef("worker", os.getpid(), own_ticks, 1)),
                shell=ProcessRef("shell", shell.parent_pid, shell_ticks, shell_state["owner_epoch"]),
                run_targets=(),
                control=ControlState(shell_state["input_owner"], shell_state["owner_epoch"],
                                     shell_state["parent_mode"], shell_state["takeover_requested"],
                                     shell_state["takeover_confirmed"]),
            )
            journal.commit(None, record)
            admission = SerializedReviewAdmission(automation_state=READY)
            pause = PauseCoordinator(bridge=bridge, admission=admission,
                                     automation_state=lambda: READY)
            review = WorkerReviewScheduler(
                admission=admission, automation_state=lambda: READY,
                active_run=lambda: None, worker_state=lambda _: None,
                collect_non_model=lambda _: None, dispatch_review=lambda _: None,
                user_priority=lambda _: False, on_exit=lambda _: None,
                clock=lambda: 0.0,
            )
            recovery = RecoveryCoordinator(object())  # No policy decision without durable facts.
            raw = RawLogStore(root / "raw")
            stack.callback(raw.close)
            app = bind_production(
                journal=journal, bridge=bridge, mailbox=mailbox, message=message,
                shell=shell, pause=pause, review=review, recovery=recovery,
                raw=raw, metadata=MetadataAdmissionGate(), model=object(),
                termination=lambda: None, boot_marker=lambda: "boot",
                authority=lambda: True, normal_stop=lambda _: self.fail("stop"),
                drain=lambda _: None,
            )
            self.assertTrue(app.reconcile().ready)
            self.assertEqual(app.peers.observe("worker"), record.worker)
            self.assertEqual(app.control.observe(), record.control)
            self.assertEqual(app.tick()[0], "held")  # Real PauseCoordinator has no bound run.
            self.assertIsNotNone(app.observe_raw(b"live binding output\n"))
            app.frontend.attach()
            app.detach()
            self.assertFalse(app.frontend.attached)
            with self.assertRaises(BridgeBoundMismatch):
                bridge.request("worker", {"kind": "state"}, timeout=0.1,
                               expected_peer=(str(uuid4()), 1))
            with self.assertRaises(LifecycleHeld):
                app.dispatch_command(message.message_id)  # User owns the shell.
            self.assertEqual(app._record().command.delivery, "none")
            self.assertFalse(repository.get_delivery_history(message.message_id))

            clients["worker"].close()
            self.assertTrue(self._disconnected(bridge))
            self.assertIsNone(app.peers.observe("worker"))
            self.assertFalse(app.reconcile().ready)
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stack.callback(replacement.close)
            replacement.settimeout(1)
            replacement.connect(str(bridge.socket_path))
            replacement.sendall((json.dumps({
                "kind": "hello", "protocolVersion": 1, "role": "worker",
                "ompSessionId": str(uuid4()), "generation": 2,
                "pid": os.getpid(), "token": "worker-token",
            }) + "\n").encode())
            self.assertEqual(json.loads(replacement.recv(4096).split(b"\n")[0])["kind"], "ready")
            self.assertEqual(mailbox.deliver(message, timeout=0.1).status,
                             MailboxStatus.REJECTED)
            self.assertIsNone(app.peers.observe("worker"))
        self.assertEqual(probe.observe(record.shell).state, "dead")

    @staticmethod
    def _disconnected(bridge: G3BridgeServer) -> bool:
        import time
        for _ in range(100):
            try:
                bridge.peer("worker")
            except Exception:
                return True
            time.sleep(.01)
        return False
