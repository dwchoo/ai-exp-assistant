"""Process-backed CW-15 lifecycle composition fixtures (no real reboot or OMP)."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.app import (
    CommandState, ControlState, LifecycleCoordinator, LifecycleHeld,
    LifecycleJournal, LifecycleRecord, PeerRef,
)
from workbench.policy.recovery_manager import RunIdentity, RunObservation
from workbench.runtime.process_evidence import (
    LinuxProcessProbe, ProcessEvidence, ProcessRef,
)
from workbench.storage.log_raw import MetadataAdmissionGate, RawLogStore


_HEARTBEAT = (
    "import signal,sys,time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN if '--ignore' in sys.argv else "
    "signal.SIG_DFL)\n"
    "while True:\n print('tick', flush=True); time.sleep(0.02)\n"
)


class FakePeers:
    def __init__(self, manager: PeerRef, worker: PeerRef):
        self.by_role = {"manager": manager, "worker": worker}

    def observe(self, role: str) -> PeerRef | None:
        return self.by_role.get(role)


class FakeControl:
    def __init__(self, state: ControlState):
        self.state = state

    def observe(self) -> ControlState:
        return self.state

    def request_takeover(self) -> ControlState:
        self.state = replace(self.state, input_owner="user", mode="manual_foreground",
                             takeover_requested=True)
        return self.state

    def confirm_takeover(self) -> ControlState:
        self.state = replace(self.state, takeover_confirmed=True)
        return self.state

    def handoff(self) -> ControlState:
        self.state = ControlState("manager", self.state.owner_epoch + 1,
                                  "control_wait", False, False)
        return self.state


class FakePause:
    paused = False
    cancelled = False
    persistence_error = None

    def status(self):
        return SimpleNamespace(paused=self.paused, cancelled=self.cancelled,
                               persistence_error=self.persistence_error)

    def dispatch_automatic(self, action):
        if self.paused or self.cancelled:
            return "held", None
        return "dispatched", action()


class FakeReview:
    def __init__(self):
        self.calls = 0

    def tick(self):
        self.calls += 1
        return self.calls


class FakeModel:
    healthy = True

    def check(self):
        return self.healthy


class FakeCommand:
    def __init__(self, peers: FakePeers, control: FakeControl):
        self.peers = peers
        self.control = control
        self.sent: list[str] = []

    def send(self, request_id: str, *, session_id: str, generation: int,
             owner_epoch: int) -> str:
        peer = self.peers.observe("worker")
        if (peer is None or (peer.session_id, peer.generation) !=
                (session_id, generation) or self.control.state.owner_epoch != owner_epoch
                or self.control.state.input_owner != "manager"):
            raise RuntimeError("stale command binding")
        self.sent.append(request_id)
        return "api_returned"

    def send_bound(self, request_id: str, *, session_id: str, generation: int,
                   owner_epoch: int, authority_token: object) -> str:
        if authority_token.claim() is not True:
            raise LifecycleHeld("command authority changed before send; no replay")
        return self.send(request_id, session_id=session_id, generation=generation,
                         owner_epoch=owner_epoch)


class ProcessFixture:
    def __init__(self):
        self.children: list[subprocess.Popen[bytes]] = []
        self.probe = LinuxProcessProbe()
        self.unknown_roles: set[str] = set()
        self.stop_calls: list[ProcessRef] = []
        self.drained = 0

    def spawn(self, role: str, *, ignore_term: bool = False) -> ProcessRef:
        argv = [sys.executable, "-u", "-c", _HEARTBEAT]
        if ignore_term:
            argv.append("--ignore")
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.children.append(process)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                ticks = self.probe.start_ticks(process.pid)
            except FileNotFoundError:
                ticks = None
            if ticks is not None:
                readable, _, _ = select.select([process.stdout], [], [],
                                               max(0, deadline - time.monotonic()))
                if not readable or process.stdout.readline() != b"tick\n":
                    raise AssertionError(f"process {role} did not become ready")
                return ProcessRef(role, process.pid, ticks, 1)
            time.sleep(0.01)
        raise AssertionError(f"process {role} did not expose start ticks")

    def observe(self, ref: ProcessRef) -> ProcessEvidence:
        if ref.role in self.unknown_roles:
            return ProcessEvidence(ref, "unknown")
        return self.probe.observe(ref)

    def normal_stop(self, ref: ProcessRef) -> None:
        self.stop_calls.append(ref)
        if self.probe.observe(ref).state != "alive":
            return
        os.kill(ref.pid, signal.SIGTERM)

    def drain(self, timeout: float) -> None:
        streams = [process.stdout for process in self.children if process.stdout]
        readable, _, _ = select.select(streams, [], [], max(timeout, 0))
        for stream in readable:
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            try:
                self.drained += len(os.read(descriptor, 65536))
            except BlockingIOError:
                pass

    def close(self) -> None:
        for process in self.children:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for process in self.children:
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=2)


class LifecycleRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cw15-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.processes = ProcessFixture()
        self.addCleanup(self.assert_processes_reaped)
        self.addCleanup(self.processes.close)
        self.manager_ref = self.processes.spawn("manager_omp")
        self.worker_ref = self.processes.spawn("worker_omp")
        self.shell_ref = self.processes.spawn("shell")
        self.child_ref = self.processes.spawn("experiment")
        self.manager = PeerRef("manager", str(uuid4()), 1, self.manager_ref)
        self.worker = PeerRef("worker", str(uuid4()), 1, self.worker_ref)
        self.peers = FakePeers(self.manager, self.worker)
        self.control = FakeControl(ControlState("manager", 1, "control_wait", False, False))
        self.pause = FakePause()
        self.review = FakeReview()
        self.model = FakeModel()
        self.metadata = MetadataAdmissionGate()
        self.raw = RawLogStore(Path(self.temp.name) / "raw", per_run_limit=4,
                               project_limit=20)
        self.addCleanup(self.raw.close)
        self.commands = FakeCommand(self.peers, self.control)
        self.boot = ["boot-a"]
        self.authorized = [True]
        self.identity = RunIdentity(str(uuid4()), 1, str(uuid4()))
        self.journal = LifecycleJournal(Path(self.temp.name) / "lifecycle.json")
        self.record = LifecycleRecord(
            task_id=self.identity.task_id, revision=1, run_id=self.identity.run_id,
            approval_hash="a" * 64, generation=1, boot_marker="boot-a",
            boot_confirmed_marker=None, manager=self.manager, worker=self.worker,
            shell=self.shell_ref, run_targets=(self.child_ref,),
            control=self.control.state,
        )
        self.app = self.make_app()
        self.app.initialize(self.record)

    def assert_processes_reaped(self) -> None:
        self.assertTrue(all(child.poll() is not None for child in self.processes.children),
                        "fixture subprocess residue after bounded cleanup")

    def make_app(self) -> LifecycleCoordinator:
        def termination():
            all_refs = (self.manager_ref, self.worker_ref,
                        self.shell_ref, self.child_ref)
            ended = all(self.processes.observe(ref).state == "dead" for ref in all_refs)
            return RunObservation(
                self.identity, 2 if ended else 1,
                "exited" if ended else "running", 0 if ended else None,
                ended, "ended" if ended else "running", ended,
                ended, ended, bool(self.processes.stop_calls),
            )

        return LifecycleCoordinator(
            journal=self.journal, peers=self.peers, control=self.control,
            processes=self.processes, pause=self.pause, review=self.review,
            model=self.model, metadata=self.metadata, raw=self.raw,
            commands=self.commands, stops=self.processes,
            termination=termination, boot_marker=lambda: self.boot[0],
            authority=lambda: self.authorized[0],
        )

    def test_p_c_ac_15_detach_keeps_processes_drain_and_active_review(self):
        self.app.frontend.attach()
        self.app.detach()
        self.assertFalse(self.app.frontend.attached)
        self.assertTrue(all(self.processes.observe(ref).state == "alive" for ref in
                            (self.manager_ref, self.worker_ref,
                             self.shell_ref, self.child_ref)))
        self.assertEqual(self.app.tick(), ("dispatched", 1))
        self.pause.paused = True
        self.assertEqual(self.app.tick(), ("held", None))
        self.assertEqual(self.review.calls, 1)
        self.processes.drain(0.05)
        self.assertGreater(self.processes.drained, 0)
        restarted = self.make_app()
        result = restarted.reconnect()
        self.assertTrue(result.ready)
        self.assertTrue(result.paused)
        self.assertTrue(restarted.frontend.attached)
        self.assertEqual(self.commands.sent, [])

    def test_p_c_ac_16_restart_reconciles_alive_dead_unknown_and_never_replays(self):
        self.app.dispatch_command("first")
        restarted = self.make_app()
        self.assertTrue(restarted.reconcile().ready)
        self.assertEqual(self.commands.sent, ["first"])
        with self.assertRaises(LifecycleHeld):
            restarted.dispatch_command("first")
        self.processes.unknown_roles.add("experiment")
        self.assertIn("experiment_identity_unknown", restarted.reconcile().problems)
        self.processes.unknown_roles.clear()
        os.kill(self.child_ref.pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while self.processes.observe(self.child_ref).state != "dead" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("run_termination_unconfirmed", restarted.reconcile().problems)
        self.peers.by_role["manager"] = replace(self.manager, session_id=str(uuid4()))
        self.assertIn("manager_session_unknown_or_drifted", restarted.reconcile().problems)
        self.assertEqual(self.commands.sent, ["first"])

    def test_p_c_ac_17_model_error_holds_decisions_not_run_or_raw_observation(self):
        self.app.model_failed("2026-09-29T01:00:00Z")
        self.assertEqual(self.app.tick(), ("held", None))
        self.assertEqual(self.processes.observe(self.child_ref).state, "alive")
        self.processes.drain(0.05)
        status = self.app.observe_raw(b"abcdef")
        self.assertEqual((status.stored_bytes, status.missing_bytes), (4, 2))
        self.assertTrue(status.truncated)
        self.model.healthy = False
        self.assertFalse(self.app.recheck_model("2026-09-29T01:00:01Z"))
        self.assertEqual(self.app.tick(), ("held", None))
        self.model.healthy = True
        self.assertTrue(self.app.recheck_model("2026-09-29T01:00:02Z"))
        self.assertEqual(self.app.tick(), ("dispatched", 1))
        record = LifecycleJournal(self.journal.path).read()
        self.assertEqual(record.last_failed_check_at, "2026-09-29T01:00:01Z")
        self.assertEqual(record.last_successful_check_at, "2026-09-29T01:00:02Z")
        self.assertEqual(self.processes.observe(self.child_ref).state, "alive")

    def test_p_c_ac_18_takeover_state_survives_detach_and_stale_handoff_blocks(self):
        requested = self.app.request_takeover()
        self.assertTrue(requested.takeover_requested)
        self.app.frontend.attach()
        self.app.detach()
        restarted = self.make_app()
        self.assertTrue(restarted.reconnect().ready)
        with self.assertRaises(LifecycleHeld):
            restarted.dispatch_command("blocked")
        with self.assertRaises(LifecycleHeld):
            restarted.confirm_takeover(session_id="stale", owner_epoch=1)
        confirmed = restarted.confirm_takeover(
            session_id=self.manager.session_id, owner_epoch=1)
        self.assertTrue(confirmed.takeover_confirmed)
        with self.assertRaises(LifecycleHeld):
            restarted.handoff(session_id=self.manager.session_id, owner_epoch=2)
        returned = restarted.handoff(session_id=self.manager.session_id, owner_epoch=1)
        self.assertEqual((returned.input_owner, returned.owner_epoch, returned.mode),
                         ("manager", 2, "control_wait"))
        self.assertEqual(restarted.dispatch_command("new"), "api_returned")
        self.assertEqual(self.commands.sent, ["new"])
        self.assertEqual(self.make_app().journal.read().command.delivery, "api_returned")

    def test_same_coordinator_handoff_reopens_only_after_durable_success(self):
        requested = self.app.request_takeover()
        takeover_generation = self.app._takeover_intent.epoch()
        self.app.confirm_takeover(session_id=self.manager.session_id,
                                  owner_epoch=requested.owner_epoch)
        self.assertEqual(self.app.tick(), ("held", None))
        fenced = self.journal.read()
        with patch.object(self.control, "handoff", side_effect=OSError("shell unavailable")):
            with self.assertRaises(OSError):
                self.app.handoff(session_id=self.manager.session_id,
                                 owner_epoch=requested.owner_epoch)
        self.assertEqual(self.app.tick(), ("held", None))
        self.assertEqual(self.journal.read(), fenced)
        self.assertEqual((self.review.calls, self.commands.sent), (0, []))
        self.assertEqual(self.app._takeover_intent.epoch(), takeover_generation)
        with patch.object(self.journal, "commit", side_effect=LifecycleHeld("disk failed")):
            with self.assertRaises(LifecycleHeld):
                self.app.handoff(session_id=self.manager.session_id,
                                 owner_epoch=requested.owner_epoch)
        self.assertEqual(self.app.tick(), ("held", None))
        self.assertEqual(self.journal.read(), fenced)
        self.assertEqual((self.review.calls, self.commands.sent), (0, []))
        self.assertEqual(self.app._takeover_intent.epoch(), takeover_generation)
        # The external handoff may have succeeded before the failed commit;
        # restore its observed state to make a deliberate second attempt.
        self.control.state = self.journal.read().control
        original_commit = self.journal.commit
        during_commit = []

        def observed_commit(expected, updated):
            result = original_commit(expected, updated)
            if updated.control.input_owner == "manager" and not updated.control.takeover_requested:
                during_commit.append(self.app.tick())
            return result

        with patch.object(self.journal, "commit", side_effect=observed_commit):
            returned = self.app.handoff(session_id=self.manager.session_id,
                                        owner_epoch=requested.owner_epoch)
        self.assertEqual(during_commit, [("held", None)])
        self.assertEqual(returned.input_owner, "manager")
        self.assertEqual(self.app._takeover_intent.epoch(), takeover_generation + 1)
        self.assertFalse(self.app._takeover_intent.claim(takeover_generation,
                                                        lambda: True))
        self.assertEqual(self.app.tick(), ("dispatched", 1))
        self.assertEqual(self.review.calls, 1)
        self.assertEqual(self.app.dispatch_command("post-handoff"), "api_returned")
        self.assertEqual(self.commands.sent, ["post-handoff"])

    def test_unbound_command_port_never_submits_and_preserves_unknown(self):
        calls: list[str] = []
        self.app.commands = SimpleNamespace(send=lambda request_id, **_: calls.append(request_id))
        with self.assertRaises(LifecycleHeld):
            self.app.dispatch_command("unbound")
        self.assertEqual(calls, [])
        self.assertEqual(self.journal.read().command.delivery, "unknown")

    def test_new_takeover_intent_survives_older_handoff_reopen(self):
        first = self.app.request_takeover()
        self.app.confirm_takeover(session_id=self.manager.session_id,
                                  owner_epoch=first.owner_epoch)
        fence = self.app._takeover_intent
        first_generation = fence.epoch()
        at_reopen, newer_intent, release_reopen = Event(), Event(), Event()
        release_newer, automatic_started = Event(), Event()
        outcomes: dict[str, object] = {}
        failures: dict[str, BaseException] = {}
        original_set, original_reopen = fence.set, fence.reopen

        def set_newer_intent():
            generation = original_set()
            outcomes["new_generation"] = fence.epoch()
            newer_intent.set()
            if not release_newer.wait(3):
                raise TimeoutError("new takeover was not released")
            return generation

        def pause_reopen(*args):
            outcomes["durable_at_reopen"] = self.journal.read()
            at_reopen.set()
            if not newer_intent.wait(3) or not release_reopen.wait(3):
                raise TimeoutError("handoff reopen barrier timed out")
            result = original_reopen(*args)
            outcomes["generation_after_reopen"] = fence.epoch()
            return result

        def handoff():
            try:
                outcomes["handoff"] = self.app.handoff(
                    session_id=self.manager.session_id, owner_epoch=first.owner_epoch)
            except BaseException as exc:
                failures["handoff"] = exc

        def takeover():
            try:
                outcomes["new_takeover"] = self.app.request_takeover()
            except BaseException as exc:
                failures["takeover"] = exc

        def automatic():
            automatic_started.set()
            try:
                outcomes["tick"] = self.app.tick()
                outcomes["command"] = self.app.dispatch_command("after-new-intent")
            except LifecycleHeld as exc:
                outcomes["command_error"] = exc
            except BaseException as exc:
                failures["automatic"] = exc

        with (patch.object(fence, "set", side_effect=set_newer_intent),
              patch.object(fence, "reopen", side_effect=pause_reopen)):
            handoff_thread = Thread(target=handoff, daemon=True)
            newer_thread = Thread(target=takeover, daemon=True)
            automatic_thread = Thread(target=automatic, daemon=True)
            try:
                handoff_thread.start()
                self.assertTrue(at_reopen.wait(2), "handoff did not commit manager control")
                self.assertEqual(outcomes["durable_at_reopen"].control.input_owner,
                                 "manager")
                newer_thread.start()
                self.assertTrue(newer_intent.wait(2), "new intent was not recorded")
                automatic_thread.start()
                self.assertTrue(automatic_started.wait(2))
                release_reopen.set()
                handoff_thread.join(3)
                automatic_thread.join(3)
                self.assertFalse(handoff_thread.is_alive() or automatic_thread.is_alive(),
                                 "handoff/automatic deadlock")
                outcomes["durable_before_new_takeover"] = self.journal.read()
            finally:
                release_reopen.set()
                release_newer.set()
                handoff_thread.join(3)
                newer_thread.join(3)
                automatic_thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in
                             (handoff_thread, newer_thread, automatic_thread)),
                         "takeover thread residue")
        self.assertEqual(failures, {})
        self.assertEqual(outcomes["durable_before_new_takeover"].control.input_owner,
                         "manager")
        self.assertEqual(outcomes["tick"], ("held", None))
        self.assertIsInstance(outcomes.get("command_error"), LifecycleHeld)
        self.assertNotIn("command", outcomes)
        self.assertEqual((self.review.calls, self.commands.sent), (0, []))
        self.assertEqual(outcomes["durable_before_new_takeover"].command.delivery,
                         "none")
        self.assertEqual((first_generation, outcomes["new_generation"],
                          outcomes["generation_after_reopen"]), (1, 2, 2))
        self.assertTrue(fence.is_set())
        self.assertTrue(outcomes["new_takeover"].takeover_requested)
        self.assertEqual((self.journal.read().generation,
                          self.journal.read().control.input_owner,
                          self.journal.read().control.takeover_requested),
                         (7, "user", True))
        second = outcomes["new_takeover"]
        self.app.confirm_takeover(session_id=self.manager.session_id,
                                  owner_epoch=second.owner_epoch)
        self.assertEqual(self.app.tick(), ("held", None))
        self.app.handoff(session_id=self.manager.session_id,
                         owner_epoch=second.owner_epoch)
        self.assertEqual(fence.epoch(), 3)
        self.assertFalse(fence.is_set())
        self.assertEqual(self.app.tick(), ("dispatched", 1))

    def test_duplicate_takeovers_attribute_newest_generation_without_replay(self):
        first = self.app.request_takeover()
        self.app.confirm_takeover(session_id=self.manager.session_id,
                                  owner_epoch=first.owner_epoch)
        durable = self.journal.read()
        durable_bytes = self.journal.path.read_bytes()
        self.assertEqual(durable.generation, 4)
        fence = self.app._takeover_intent
        older_set, release_older = Event(), Event()
        errors: list[BaseException] = []
        original_set = fence.set

        def ordered_set():
            generation = original_set()
            if generation == 2:
                older_set.set()
                if not release_older.wait(3):
                    raise TimeoutError("older duplicate was not released")
            return generation

        def duplicate():
            try:
                self.app.request_takeover()
            except BaseException as exc:
                errors.append(exc)

        with (patch.object(fence, "set", side_effect=ordered_set),
              patch.object(self.control, "request_takeover",
                           wraps=self.control.request_takeover) as external,
              patch.object(self.journal, "commit", wraps=self.journal.commit) as commit):
            older = Thread(target=duplicate, daemon=True)
            newer = Thread(target=duplicate, daemon=True)
            try:
                older.start()
                self.assertTrue(older_set.wait(2), "older duplicate did not set intent")
                newer.start()
                newer.join(3)
                self.assertFalse(newer.is_alive(), "newer duplicate deadlocked")
            finally:
                release_older.set()
                older.join(3)
                newer.join(3)
            self.assertFalse(older.is_alive(), "older duplicate deadlocked")
            self.assertEqual((external.call_count, commit.call_count), (0, 0))
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(exc, LifecycleHeld) for exc in errors))
        self.assertEqual(self.journal.read(), durable)
        self.assertEqual(self.journal.path.read_bytes(), durable_bytes)
        self.assertEqual((fence.epoch(), self.app._takeover_generation), (3, 3))
        self.assertTrue(fence.is_set())
        self.assertEqual(self.app.tick(), ("held", None))
        with self.assertRaises(LifecycleHeld):
            self.app.dispatch_command("before-valid-handoff")
        self.assertEqual((self.review.calls, self.commands.sent), (0, []))
        self.assertEqual(self.journal.read(), durable)

        with patch.object(self.control, "handoff", wraps=self.control.handoff) as external:
            returned = self.app.handoff(session_id=self.manager.session_id,
                                        owner_epoch=first.owner_epoch)
            self.assertEqual(external.call_count, 1)
        self.assertEqual((returned.input_owner, returned.mode),
                         ("manager", "control_wait"))
        self.assertEqual(self.journal.read().generation, 5)
        self.assertEqual(fence.epoch(), 4)
        self.assertFalse(fence.is_set())
        self.assertEqual(self.app.tick(), ("dispatched", 1))
        self.assertEqual(self.app.dispatch_command("after-valid-handoff"), "api_returned")
        self.assertEqual((self.review.calls, self.commands.sent),
                         (1, ["after-valid-handoff"]))
        self.assertEqual(self.journal.read().generation, 7)

    def _check_new_takeover_around_reopen(self, order: str) -> None:
        first = self.app.request_takeover()
        self.app.confirm_takeover(session_id=self.manager.session_id,
                                  owner_epoch=first.owner_epoch)
        fence = self.app._takeover_intent
        self.assertEqual(fence.epoch(), 1)
        intent_recorded, at_reopen = Event(), Event()
        release_takeover, release_reopen = Event(), Event()
        outcomes: dict[str, object] = {}
        failures: dict[str, BaseException] = {}
        original_set, original_reopen = fence.set, fence.reopen

        def record_new_intent():
            generation = original_set()
            outcomes["intent_at"] = time.monotonic_ns()
            outcomes["new_generation"] = generation
            intent_recorded.set()
            if not release_takeover.wait(3):
                raise TimeoutError("new takeover release timed out")
            return generation

        def observe_reopen(expected_epoch):
            outcomes["reopen_at"] = time.monotonic_ns()
            outcomes["expected_epoch"] = expected_epoch
            outcomes["reopen_result"] = original_reopen(expected_epoch)
            outcomes["epoch_after_reopen"] = fence.epoch()
            at_reopen.set()
            if order == "after" and not release_reopen.wait(3):
                raise TimeoutError("reopen release timed out")
            return outcomes["reopen_result"]

        def run(name, action):
            try:
                outcomes[name] = action()
            except BaseException as exc:
                failures[name] = exc

        handoff = Thread(target=run, args=("handoff", lambda: self.app.handoff(
            session_id=self.manager.session_id, owner_epoch=first.owner_epoch)), daemon=True)
        takeover = Thread(target=run, args=("takeover", self.app.request_takeover), daemon=True)

        def automatic():
            outcomes["tick"] = self.app.tick()
            try:
                outcomes["command"] = self.app.dispatch_command("new-intent-command")
            except LifecycleHeld as exc:
                outcomes["command_error"] = exc

        action = Thread(target=run, args=("automatic", automatic), daemon=True)
        with (patch.object(fence, "set", side_effect=record_new_intent),
              patch.object(fence, "reopen", side_effect=observe_reopen)):
            try:
                if order == "before":
                    takeover.start()
                    self.assertTrue(intent_recorded.wait(2), "new intent not recorded")
                    handoff.start()
                    self.assertTrue(at_reopen.wait(2), "handoff did not reopen")
                    handoff.join(2)
                    self.assertFalse(handoff.is_alive(), "handoff deadlocked")
                    action.start()
                else:
                    handoff.start()
                    self.assertTrue(at_reopen.wait(2), "handoff did not reopen")
                    takeover.start()
                    self.assertTrue(intent_recorded.wait(2), "new intent not recorded")
                    action.start()
                    release_reopen.set()
                    handoff.join(2)
                    self.assertFalse(handoff.is_alive(), "handoff deadlocked")
                action.join(2)
                self.assertFalse(action.is_alive(), "automatic action deadlocked")
                outcomes["durable_after_handoff"] = self.journal.read()
            finally:
                release_reopen.set()
                release_takeover.set()
                for thread in (handoff, takeover, action):
                    if thread.ident is not None:
                        thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in (handoff, takeover, action)),
                         "generation schedule thread residue")
        self.assertEqual(failures, {})
        if order == "before":
            self.assertLess(outcomes["intent_at"], outcomes["reopen_at"])
            self.assertEqual((outcomes["new_generation"], outcomes["reopen_result"],
                              outcomes["epoch_after_reopen"]), (2, False, 2))
        else:
            self.assertLess(outcomes["reopen_at"], outcomes["intent_at"])
            self.assertEqual((outcomes["new_generation"], outcomes["reopen_result"],
                              outcomes["epoch_after_reopen"]), (3, True, 2))
        self.assertEqual(outcomes["expected_epoch"], 1)
        durable = outcomes["durable_after_handoff"]
        self.assertEqual((durable.generation, durable.control.input_owner,
                          durable.control.takeover_requested, durable.command.delivery),
                         (5, "manager", False, "none"))
        self.assertEqual(outcomes["tick"], ("held", None))
        self.assertIsInstance(outcomes.get("command_error"), LifecycleHeld)
        self.assertNotIn("command", outcomes)
        self.assertEqual((self.review.calls, self.commands.sent), (0, []))
        self.assertEqual((self.journal.read().generation,
                          self.journal.read().control.input_owner,
                          self.journal.read().control.takeover_requested),
                         (7, "user", True))
        self.assertTrue(fence.is_set())
        self.assertFalse(fence.claim(1, lambda: True))
        before_stale = self.journal.read()
        before_stale_epoch = fence.epoch()
        with self.assertRaises(LifecycleHeld):
            self.app.handoff(session_id=self.manager.session_id,
                             owner_epoch=first.owner_epoch)
        self.assertEqual(self.journal.read(), before_stale)
        self.assertEqual(fence.epoch(), before_stale_epoch)
        self.assertTrue(fence.is_set())
        second = outcomes["takeover"]
        self.app.confirm_takeover(session_id=self.manager.session_id,
                                  owner_epoch=second.owner_epoch)
        self.assertEqual(self.app.tick(), ("held", None))
        self.app.handoff(session_id=self.manager.session_id,
                         owner_epoch=second.owner_epoch)
        self.assertEqual(fence.epoch(), outcomes["new_generation"] + 1)
        self.assertFalse(fence.is_set())
        self.assertFalse(fence.claim(1, lambda: True))
        self.assertEqual(self.app.tick(), ("dispatched", 1))
        self.assertEqual(self.app.dispatch_command("after-valid-handoff"), "api_returned")
        self.assertEqual(self.commands.sent, ["after-valid-handoff"])

    def test_new_intent_before_handoff_reopen_stays_fenced_until_own_handoff(self):
        self._check_new_takeover_around_reopen("before")

    def test_new_intent_after_handoff_reopen_stays_fenced_until_own_handoff(self):
        self._check_new_takeover_around_reopen("after")

    def test_duplicate_takeover_does_not_poison_valid_handoff_reopen(self):
        first = self.app.request_takeover()
        self.app.confirm_takeover(session_id=self.manager.session_id,
                                  owner_epoch=first.owner_epoch)
        fence = self.app._takeover_intent
        durable = self.journal.read()
        with self.assertRaisesRegex(LifecycleHeld, "takeover already requested"):
            self.app.request_takeover()
        self.assertEqual(self.journal.read(), durable)
        self.assertTrue(fence.is_set())
        self.assertEqual((self.review.calls, self.commands.sent), (0, []))
        original_reopen = fence.reopen
        reopen_calls = []

        def observe_reopen(expected_epoch):
            result = original_reopen(expected_epoch)
            reopen_calls.append((expected_epoch, result, fence.epoch()))
            return result

        with patch.object(fence, "reopen", side_effect=observe_reopen):
            self.app.handoff(session_id=self.manager.session_id,
                             owner_epoch=first.owner_epoch)
        self.assertEqual(reopen_calls, [(2, True, 3)])
        self.duplicate_trace = {
            "fence_epoch": fence.epoch(),
            "fence_set": fence.is_set(),
            "durable_generation": self.journal.read().generation,
            "durable_owner": self.journal.read().control.input_owner,
            "durable_takeover": self.journal.read().control.takeover_requested,
            "review_calls": self.review.calls,
            "command_sends": len(self.commands.sent),
        }
        self.assertFalse(fence.is_set())
        self.assertEqual(self.app.tick(), ("dispatched", 1))

    def test_p_c_ac_22_shutdown_requires_confirmation_and_full_termination(self):
        declined = self.app.shutdown(user_confirmed=False)
        self.assertFalse(declined.confirmed)
        self.assertEqual(self.processes.stop_calls, [])
        done = self.app.shutdown(user_confirmed=True, timeout=2)
        self.assertTrue(done.confirmed)
        self.assertTrue(done.complete, done.problems)
        self.assertEqual(done.survivors, ())
        self.assertEqual(len(self.processes.stop_calls), 4)
        self.assertTrue(self.journal.read().admission_closed)
        self.assertFalse(self.app.automatic_admission()[0])

    def test_shutdown_requires_exact_current_run_identity_even_with_no_survivors(self):
        refs = (self.manager_ref, self.worker_ref, self.shell_ref, self.child_ref)
        for ref in refs:
            os.kill(ref.pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while any(self.processes.observe(ref).state != "dead" for ref in refs):
            self.assertLess(time.monotonic(), deadline, "fixture PIDs did not exit")
            time.sleep(.01)

        def ended(identity):
            return RunObservation(identity, 2, "exited", 0, True, "ended",
                                  True, True, True, True)

        wrong = (
            ("task", ended(RunIdentity(str(uuid4()), self.identity.revision,
                                       self.identity.run_id))),
            ("revision", ended(RunIdentity(self.identity.task_id, 2,
                                           self.identity.run_id))),
            ("run", ended(RunIdentity(self.identity.task_id, self.identity.revision,
                                      str(uuid4())))),
            ("malformed", SimpleNamespace(identity=self.identity,
                                           fully_terminated=True)),
            ("none", None),
        )
        for name, observation in wrong:
            with self.subTest(observation=name):
                self.app.termination = lambda observation=observation: observation
                result = self.app.shutdown(user_confirmed=True, timeout=.05)
                self.assertTrue(result.confirmed)
                self.assertFalse(result.complete)
                self.assertEqual(result.survivors, ())
                self.assertEqual(result.problems, ("full_termination_unconfirmed",))
                self.assertEqual(len(self.processes.stop_calls), 4)
                self.assertEqual(self.journal.read().generation, 2)

        def unavailable():
            raise OSError("termination observation unavailable")

        self.app.termination = unavailable
        unknown = self.app.shutdown(user_confirmed=True, timeout=.05)
        self.assertEqual((unknown.complete, unknown.survivors, unknown.problems),
                         (False, (), ("full_termination_unconfirmed",)))
        self.assertEqual(len(self.processes.stop_calls), 4)
        self.assertEqual(self.journal.read().generation, 2)

        self.app.termination = lambda: ended(self.identity)
        complete = self.app.shutdown(user_confirmed=True, timeout=.05)
        self.assertEqual((complete.complete, complete.survivors, complete.problems),
                         (True, (), ()))
        self.assertEqual(len(self.processes.stop_calls), 4)
        self.assertEqual(self.journal.read().generation, 2)

    def test_shutdown_polling_waits_through_changing_wrong_identities(self):
        refs = (self.manager_ref, self.worker_ref, self.shell_ref, self.child_ref)
        for ref in refs:
            os.kill(ref.pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while any(self.processes.observe(ref).state != "dead" for ref in refs):
            self.assertLess(time.monotonic(), deadline, "fixture PIDs did not exit")
            time.sleep(.01)

        identities = (
            RunIdentity(str(uuid4()), self.identity.revision, self.identity.run_id),
            RunIdentity(self.identity.task_id, self.identity.revision + 1,
                        self.identity.run_id),
            RunIdentity(self.identity.task_id, self.identity.revision, str(uuid4())),
            self.identity,
        )
        observed = []

        def changing():
            identity = identities[len(observed)]
            observed.append(identity)
            return RunObservation(identity, len(observed), "exited", 0, True, "ended",
                                  True, True, True, True)

        self.app.termination = changing
        result = self.app.shutdown(user_confirmed=True, timeout=.3)
        self.assertEqual(observed, list(identities))
        self.assertEqual((result.complete, result.survivors, result.problems),
                         (True, (), ()))
        self.assertEqual(len(self.processes.stop_calls), 4)
        self.assertEqual(self.journal.read().generation, 2)

        self.app.termination = lambda: RunObservation(
            identities[2], 5, "exited", 0, True, "ended", True, True, True, True)
        repeated = self.app.shutdown(user_confirmed=True, timeout=.05)
        self.assertEqual((repeated.complete, repeated.survivors, repeated.problems),
                         (False, (), ("full_termination_unconfirmed",)))
        self.assertEqual(len(self.processes.stop_calls), 4)
        self.assertEqual(self.journal.read().generation, 2)

    def test_p_c_ac_22_survivor_is_reported_without_force(self):
        extra = self.processes.spawn("descendant", ignore_term=True)
        record = self.journal.read()
        self.journal.commit(record, replace(record, generation=2,
                                            run_targets=(self.child_ref, extra)))
        done = self.app.shutdown(user_confirmed=True, timeout=0.25)
        self.assertFalse(done.complete)
        self.assertIn(extra, done.survivors)
        self.assertIn("managed_survivors_or_unknown", done.problems)
        self.assertIn(extra, self.processes.stop_calls)
        self.assertEqual(self.processes.observe(extra).state, "alive")

    def test_p_c_ac_23_boot_change_and_metadata_latch_need_explicit_confirmation(self):
        self.boot[0] = "boot-b"
        self.assertIn("boot_confirmation_required", self.app.reconcile().problems)
        self.assertFalse(self.app.automatic_admission()[0])
        self.assertEqual(self.app.tick(), ("held", None))
        self.assertEqual(self.review.calls, 0)
        with self.assertRaises(LifecycleHeld):
            self.app.confirm_boot(user_confirmed=False)
        self.app.confirm_boot(user_confirmed=True)
        self.assertTrue(self.app.automatic_admission()[0])
        self.metadata.record_metadata_failure("sqlite fsync failed")
        self.assertFalse(self.app.automatic_admission()[0])
        self.assertEqual(self.processes.observe(self.child_ref).state, "alive")
        self.assertEqual(self.app.observe_raw(b"abc").stored_bytes, 3)
        self.metadata.record_durable_metadata_success()
        self.assertTrue(self.app.automatic_admission()[0])

    def test_ended_run_cannot_fence_or_deliver_command_with_live_peers(self):
        os.kill(self.child_ref.pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while self.processes.observe(self.child_ref).state != "dead" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.processes.observe(self.child_ref).state, "dead")
        self.app.termination = lambda: RunObservation(
            self.identity, 2, "exited", 0, True, "ended", True, True, True, False,
        )
        ended = self.app.reconcile()
        self.assertEqual(ended.state, "ended")
        self.assertEqual(ended.problems, ())
        before = self.journal.path.read_bytes()
        with patch.object(self.pause, "dispatch_automatic",
                          wraps=self.pause.dispatch_automatic) as dispatch:
            self.assertEqual(self.app.tick(), ("held", None))
            self.assertEqual(dispatch.call_count, 0)
            with self.assertRaisesRegex(LifecycleHeld, "run_ended"):
                self.app.dispatch_command("ended-command")
            self.assertEqual(dispatch.call_count, 0)
        self.assertEqual(self.commands.sent, [])
        self.assertEqual(self.journal.path.read_bytes(), before)
        self.assertEqual(self.journal.read().command.delivery, "none")
        self.assertEqual(self.review.calls, 0)

    def test_metadata_failure_holds_periodic_review_until_explicit_success(self):
        self.metadata.record_metadata_failure("metadata fsync failed")
        before = self.journal.path.read_bytes()
        with patch.object(self.pause, "dispatch_automatic",
                          wraps=self.pause.dispatch_automatic) as dispatch:
            for _ in range(2):
                self.assertEqual(self.app.tick(), ("held", None))
            self.assertEqual(dispatch.call_count, 0)
            self.assertEqual(self.review.calls, 0)
            self.assertEqual(self.commands.sent, [])
            self.assertEqual(self.journal.path.read_bytes(), before)
            self.metadata.record_durable_metadata_success()
            self.assertEqual(self.app.tick(), ("dispatched", 1))
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(self.review.calls, 1)
            self.assertEqual(self.commands.sent, [])
            self.assertEqual(self.journal.path.read_bytes(), before)

    def test_exact_process_incarnation_and_journal_cas(self):
        self.assertEqual(self.processes.observe(self.child_ref).state, "alive")
        reused = replace(self.child_ref, start_ticks=self.child_ref.start_ticks + 1)
        self.assertEqual(self.processes.observe(reused).state, "unknown")
        stale = self.journal.read()
        self.journal.commit(stale, replace(stale, generation=2, model_hold=True))
        with self.assertRaises(LifecycleHeld):
            self.journal.commit(stale, replace(stale, generation=2, model_hold=False))

    def test_recreated_coordinator_holds_unknown_command_and_control_drift(self):
        with patch.object(self.commands, "send", side_effect=OSError("delivery unknown")):
            with self.assertRaises(LifecycleHeld):
                self.app.dispatch_command("once")
        self.assertEqual(self.journal.read().command.delivery, "unknown")
        restarted = self.make_app()
        self.assertIn("command_delivery_unknown_no_replay", restarted.reconcile().problems)
        with self.assertRaises(LifecycleHeld):
            restarted.dispatch_command("once")
        self.assertEqual(self.commands.sent, [])
        self.pause.paused = True
        self.pause.cancelled = True
        self.authorized[0] = False
        held = restarted.reconcile()
        self.assertTrue(held.paused)
        self.assertTrue(held.cancelled)
        self.assertIn("authority_revoked_or_unknown", held.problems)
        self.assertEqual(restarted.tick(), ("held", None))
        self.assertEqual(self.review.calls, 0)

    def test_recreated_coordinator_rejects_process_session_and_epoch_drift(self):
        current = self.journal.read()
        reused_shell = replace(self.shell_ref, start_ticks=self.shell_ref.start_ticks + 1)
        self.journal.commit(current, replace(current, generation=2, shell=reused_shell))
        restarted = self.make_app()
        self.assertIn("shell_identity_unknown", restarted.reconcile().problems)
        self.assertFalse(restarted.automatic_admission()[0])
        self.assertEqual(self.processes.observe(self.shell_ref).state, "alive")
        self.peers.by_role["worker"] = replace(self.worker, generation=2)
        self.assertIn("worker_session_unknown_or_drifted", restarted.reconcile().problems)
        self.peers.by_role["worker"] = self.worker
        self.control.state = replace(self.control.state, owner_epoch=2)
        self.assertIn("control_or_owner_unknown_or_drifted", restarted.reconcile().problems)
        self.assertEqual(self.commands.sent, [])

    def test_repeated_confirmed_shutdown_has_no_new_stop_actions(self):
        first = self.app.shutdown(user_confirmed=True, timeout=2)
        self.assertTrue(first.complete, first.problems)
        calls = tuple(self.processes.stop_calls)
        second = self.app.shutdown(user_confirmed=True, timeout=2)
        self.assertTrue(second.complete, second.problems)
        self.assertEqual(tuple(self.processes.stop_calls), calls)

    def test_incomplete_shutdown_reobserves_without_duplicate_stop(self):
        extra = self.processes.spawn("stubborn", ignore_term=True)
        current = self.journal.read()
        self.journal.commit(current, replace(current, generation=current.generation + 1,
                                             run_targets=(self.child_ref, extra)))
        first = self.app.shutdown(user_confirmed=True, timeout=0.1)
        self.assertFalse(first.complete)
        calls = tuple(self.processes.stop_calls)
        second = self.app.shutdown(user_confirmed=True, timeout=0.1)
        self.assertFalse(second.complete)
        self.assertEqual(tuple(self.processes.stop_calls), calls)

    def test_command_send_finishes_before_concurrent_shutdown_close(self):
        entered, release, closing = Event(), Event(), Event()
        outcomes, failures = {}, []
        effect_times, close_times = [], []
        original_send = self.commands.send
        original_commit = self.journal.commit

        def recorded_commit(expected, updated):
            result = original_commit(expected, updated)
            if updated.admission_closed and not expected.admission_closed:
                close_times.append(time.monotonic_ns())
            return result

        def blocked_send(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("command test gate")
            result = original_send(*args, **kwargs)
            effect_times.append(time.monotonic_ns())
            return result

        def command():
            try:
                outcomes["command"] = self.app.dispatch_command("race-command")
            except BaseException as exc:
                failures.append(exc)

        def shutdown():
            closing.set()
            try:
                outcomes["shutdown"] = self.app.shutdown(user_confirmed=True, timeout=2)
            except BaseException as exc:
                failures.append(exc)

        with (patch.object(self.commands, "send", side_effect=blocked_send),
              patch.object(self.journal, "commit", side_effect=recorded_commit)):
            sender = Thread(target=command, daemon=True)
            stopper = Thread(target=shutdown, daemon=True)
            sender.start()
            self.assertTrue(entered.wait(2), "send did not enter")
            stopper.start()
            try:
                self.assertTrue(closing.wait(2))
                time.sleep(0.05)
                self.assertFalse(self.journal.read().admission_closed,
                                 "shutdown closed before in-flight send completed")
            finally:
                release.set()
                sender.join(3)
                stopper.join(3)
        self.assertFalse(sender.is_alive() or stopper.is_alive(), "command/shutdown deadlock")
        self.assertEqual(failures, [])
        self.assertEqual(outcomes["command"], "api_returned")
        self.assertTrue(outcomes["shutdown"].complete)
        self.assertEqual(self.commands.sent, ["race-command"])
        self.assertEqual((len(effect_times), len(close_times)), (1, 1))
        self.assertLess(effect_times[0], close_times[0])
        self.assertEqual(self.journal.read().command.delivery, "api_returned")
        with self.assertRaises(LifecycleHeld):
            self.app.dispatch_command("later-command")
        self.assertEqual(self.commands.sent, ["race-command"])

    def test_review_tick_finishes_before_concurrent_shutdown_close(self):
        entered, release, closing = Event(), Event(), Event()
        outcomes, failures = {}, []
        effect_times, close_times = [], []
        original_tick = self.review.tick
        original_commit = self.journal.commit

        def recorded_commit(expected, updated):
            result = original_commit(expected, updated)
            if updated.admission_closed and not expected.admission_closed:
                close_times.append(time.monotonic_ns())
            return result

        def blocked_review():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("review test gate")
            result = original_tick()
            effect_times.append(time.monotonic_ns())
            return result

        def tick():
            try:
                outcomes["tick"] = self.app.tick()
            except BaseException as exc:
                failures.append(exc)

        def shutdown():
            closing.set()
            try:
                outcomes["shutdown"] = self.app.shutdown(user_confirmed=True, timeout=2)
            except BaseException as exc:
                failures.append(exc)

        with (patch.object(self.review, "tick", side_effect=blocked_review),
              patch.object(self.journal, "commit", side_effect=recorded_commit)):
            reviewer = Thread(target=tick, daemon=True)
            stopper = Thread(target=shutdown, daemon=True)
            reviewer.start()
            self.assertTrue(entered.wait(2), "review did not enter")
            stopper.start()
            try:
                self.assertTrue(closing.wait(2))
                time.sleep(0.05)
                self.assertFalse(self.journal.read().admission_closed,
                                 "shutdown closed before in-flight review completed")
            finally:
                release.set()
                reviewer.join(3)
                stopper.join(3)
        self.assertFalse(reviewer.is_alive() or stopper.is_alive(), "review/shutdown deadlock")
        self.assertEqual(failures, [])
        self.assertEqual(outcomes["tick"], ("dispatched", 1))
        self.assertTrue(outcomes["shutdown"].complete)
        self.assertEqual((len(effect_times), len(close_times)), (1, 1))
        self.assertLess(effect_times[0], close_times[0])
        self.assertEqual(self.app.tick(), ("held", None))
        self.assertEqual(self.review.calls, 1)

    def test_shutdown_close_precedes_blocked_command_and_review(self):
        stop_entered, release = Event(), Event()
        outcomes, failures = {}, []
        close_times, stop_times = [], []
        original_stop = self.processes.normal_stop
        original_commit = self.journal.commit

        def recorded_commit(expected, updated):
            result = original_commit(expected, updated)
            if updated.admission_closed and not expected.admission_closed:
                close_times.append(time.monotonic_ns())
            return result

        def blocked_stop(ref):
            if not stop_entered.is_set():
                stop_times.append(time.monotonic_ns())
                self.assertEqual(self.app.tick(), ("held", None))
                stop_entered.set()
                if not release.wait(3):
                    raise TimeoutError("shutdown test gate")
            return original_stop(ref)

        def run(name, action):
            try:
                outcomes[name] = action()
            except BaseException as exc:
                failures.append(exc)

        with (patch.object(self.journal, "commit", side_effect=recorded_commit),
              patch.object(self.processes, "normal_stop", side_effect=blocked_stop),
              patch.object(self.pause, "dispatch_automatic",
                           wraps=self.pause.dispatch_automatic) as dispatch):
            stopper = Thread(target=lambda: run("shutdown", lambda: self.app.shutdown(
                user_confirmed=True, timeout=2)), daemon=True)
            stopper.start()
            self.assertTrue(stop_entered.wait(2), "shutdown action did not start")
            self.assertTrue(self.journal.read().admission_closed)
            sender = Thread(target=lambda: run("command", lambda: self.app.dispatch_command(
                "late-command")), daemon=True)
            reviewer = Thread(target=lambda: run("tick", self.app.tick), daemon=True)
            sender.start()
            reviewer.start()
            try:
                time.sleep(0.05)
                self.assertTrue(sender.is_alive() and reviewer.is_alive(),
                                "automatic calls did not wait for shutdown action")
            finally:
                release.set()
                stopper.join(3)
                sender.join(3)
                reviewer.join(3)
            self.assertEqual(dispatch.call_count, 0)
        self.assertFalse(any(thread.is_alive() for thread in (stopper, sender, reviewer)),
                         "shutdown/automatic deadlock")
        self.assertTrue(outcomes["shutdown"].complete)
        self.assertEqual((len(close_times), len(stop_times)), (1, 1))
        self.assertLess(close_times[0], stop_times[0])
        self.assertEqual(outcomes["tick"], ("held", None))
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], LifecycleHeld)
        self.assertEqual(self.commands.sent, [])
        self.assertEqual(self.review.calls, 0)
        self.assertEqual(self.journal.read().command.delivery, "none")

    def test_reentrant_admission_and_failed_external_callback_release_lock(self):
        original_reconcile = self.app.reconcile
        nested = []
        failures = []
        once = [False]

        def reentrant_reconcile():
            if not once[0]:
                once[0] = True
                nested.append(self.app.tick())
            return original_reconcile()

        def failing_send(*args, **kwargs):
            nested.append(self.app.tick())
            raise OSError("external send failed")

        def command():
            try:
                self.app.dispatch_command("reentrant-failed-command")
            except BaseException as exc:
                failures.append(exc)

        with (patch.object(self.app, "reconcile", side_effect=reentrant_reconcile),
              patch.object(self.commands, "send", side_effect=failing_send)):
            sender = Thread(target=command, daemon=True)
            sender.start()
            sender.join(3)
        self.assertFalse(sender.is_alive(), "reentrant automatic admission deadlock")
        self.assertEqual(nested, [("dispatched", 1), ("held", None)])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], LifecycleHeld)
        self.assertEqual(self.journal.read().command.delivery, "unknown")
        self.assertEqual(self.commands.sent, [])

        result = []
        stopper = Thread(target=lambda: result.append(
            self.app.shutdown(user_confirmed=True, timeout=2)), daemon=True)
        stopper.start()
        stopper.join(3)
        self.assertFalse(stopper.is_alive(), "failed send retained shutdown lock")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].complete, result[0].problems)
        self.assertEqual(self.app.tick(), ("held", None))
        self.assertEqual(self.review.calls, 1)

    def test_reentrant_post_close_journal_failure_releases_lock_without_replay(self):
        original_commit = self.journal.commit
        close_times, nested, failures = [], [], []

        def ambiguous_commit(expected, updated):
            result = original_commit(expected, updated)
            if updated.admission_closed and not expected.admission_closed:
                close_times.append(time.monotonic_ns())
                nested.append(self.app.tick())
                raise OSError("post-close journal callback failed")
            return result

        def first_shutdown():
            try:
                self.app.shutdown(user_confirmed=True, timeout=.1)
            except BaseException as exc:
                failures.append(exc)

        with patch.object(self.journal, "commit", side_effect=ambiguous_commit):
            first = Thread(target=first_shutdown, daemon=True)
            first.start()
            first.join(3)
        self.assertFalse(first.is_alive(), "reentrant journal callback deadlock")
        self.assertEqual(len(close_times), 1)
        self.assertEqual(nested, [("held", None)])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], OSError)
        self.assertTrue(self.journal.read().admission_closed)
        self.assertEqual(self.review.calls, 0)
        self.assertEqual(self.commands.sent, [])

        result = []
        second = Thread(target=lambda: result.append(
            self.app.shutdown(user_confirmed=True, timeout=.1)), daemon=True)
        second.start()
        second.join(3)
        self.assertFalse(second.is_alive(), "post-close failure retained shutdown lock")
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].complete)
        self.assertEqual(len(result[0].survivors), 4)
        self.assertEqual(self.processes.stop_calls, [])
        self.assertEqual(self.app.tick(), ("held", None))

    def test_review_finishes_before_takeover_fence_then_later_ticks_hold(self):
        entered, release, requesting = Event(), Event(), Event()
        outcomes, failures, effect_times, fence_times = {}, [], [], []
        original_tick = self.review.tick
        original_commit = self.journal.commit

        def blocked_review():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("review/takeover test gate")
            result = original_tick()
            effect_times.append(time.monotonic_ns())
            return result

        def recorded_commit(expected, updated):
            result = original_commit(expected, updated)
            if updated.control.takeover_requested and not expected.control.takeover_requested:
                fence_times.append(time.monotonic_ns())
            return result

        def run(name, action):
            try:
                outcomes[name] = action()
            except BaseException as exc:
                failures.append(exc)

        with (patch.object(self.review, "tick", side_effect=blocked_review),
              patch.object(self.journal, "commit", side_effect=recorded_commit)):
            reviewer = Thread(target=lambda: run("tick", self.app.tick), daemon=True)
            taker = Thread(target=lambda: (requesting.set(), run(
                "takeover", self.app.request_takeover)), daemon=True)
            reviewer.start()
            self.assertTrue(entered.wait(2))
            taker.start()
            try:
                self.assertTrue(requesting.wait(2))
                time.sleep(.05)
                self.assertFalse(self.journal.read().control.takeover_requested,
                                 "takeover fenced before review completed")
            finally:
                release.set()
                reviewer.join(3)
                taker.join(3)
        self.assertFalse(reviewer.is_alive() or taker.is_alive(), "review/takeover deadlock")
        self.assertEqual(failures, [])
        self.assertEqual(outcomes["tick"], ("dispatched", 1))
        self.assertTrue(outcomes["takeover"].takeover_requested)
        self.assertEqual((len(effect_times), len(fence_times)), (1, 1))
        self.assertLess(effect_times[0], fence_times[0])
        self.assertEqual(self.app.tick(), ("held", None))
        self.assertEqual(self.review.calls, 1)

    def test_takeover_fence_precedes_blocked_review_with_zero_actions(self):
        entered, release = Event(), Event()
        outcomes, failures = {}, []
        original_takeover = self.control.request_takeover

        def blocked_takeover():
            self.assertEqual(self.app.tick(), ("held", None))  # Same-thread reentry.
            entered.set()
            if not release.wait(3):
                raise TimeoutError("takeover/review test gate")
            return original_takeover()

        def run(name, action):
            try:
                outcomes[name] = action()
            except BaseException as exc:
                failures.append(exc)

        with (patch.object(self.control, "request_takeover", side_effect=blocked_takeover),
              patch.object(self.pause, "dispatch_automatic",
                           wraps=self.pause.dispatch_automatic) as dispatch):
            taker = Thread(target=lambda: run("takeover", self.app.request_takeover),
                           daemon=True)
            taker.start()
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.journal.read().control.takeover_requested)
            reviewer = Thread(target=lambda: run("tick", self.app.tick), daemon=True)
            reviewer.start()
            try:
                time.sleep(.05)
                self.assertTrue(reviewer.is_alive(), "review did not wait for takeover")
            finally:
                release.set()
                taker.join(3)
                reviewer.join(3)
            self.assertEqual(dispatch.call_count, 0)
        self.assertFalse(taker.is_alive() or reviewer.is_alive(), "takeover/review deadlock")
        self.assertEqual(failures, [])
        self.assertTrue(outcomes["takeover"].takeover_requested)
        self.assertEqual(outcomes["tick"], ("held", None))
        self.assertEqual(self.review.calls, 0)
        self.assertEqual(self.commands.sent, [])

    def test_unknown_inflight_command_does_not_block_or_replay_takeover(self):
        entered, release, requesting = Event(), Event(), Event()
        failures, outcomes = {}, {}

        def ambiguous_send(*_args, **_kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("unknown command test gate")
            raise OSError("delivery unknown")

        def command():
            try:
                self.app.dispatch_command("unknown-command")
            except BaseException as exc:
                failures["command"] = exc

        def takeover():
            requesting.set()
            try:
                outcomes["takeover"] = self.app.request_takeover()
            except BaseException as exc:
                failures["takeover"] = exc

        with (patch.object(self.commands, "send", side_effect=ambiguous_send) as send,
              patch.object(self.control, "request_takeover",
                           wraps=self.control.request_takeover) as control_request):
            sender = Thread(target=command, daemon=True)
            taker = Thread(target=takeover, daemon=True)
            sender.start()
            self.assertTrue(entered.wait(2))
            before = self.journal.read().command
            self.assertEqual(before.delivery, "unknown")
            taker.start()
            try:
                self.assertTrue(requesting.wait(2))
                time.sleep(.05)
                self.assertFalse(self.journal.read().control.takeover_requested)
            finally:
                release.set()
                sender.join(3)
                taker.join(3)
            self.assertEqual((send.call_count, control_request.call_count), (1, 1))
        self.assertFalse(sender.is_alive() or taker.is_alive(), "unknown/takeover deadlock")
        self.assertIsInstance(failures.get("command"), LifecycleHeld)
        self.assertNotIn("takeover", failures)
        self.assertTrue(outcomes["takeover"].takeover_requested)
        after = self.journal.read()
        self.assertEqual(after.command, before)
        self.assertTrue(after.control.takeover_requested)
        self.assertEqual(self.commands.sent, [])
        self.assertEqual(self.app.tick(), ("held", None))

    def test_takeover_external_failure_keeps_durable_fence_and_unknown_command(self):
        with patch.object(self.commands, "send", side_effect=OSError("unknown")):
            with self.assertRaises(LifecycleHeld):
                self.app.dispatch_command("unknown-command")
        before = self.journal.read().command
        nested = []

        def failing_takeover():
            nested.append(self.app.tick())
            raise OSError("control outcome unknown")

        failures = []

        def takeover():
            try:
                self.app.request_takeover()
            except BaseException as exc:
                failures.append(exc)

        with patch.object(self.control, "request_takeover", side_effect=failing_takeover):
            taker = Thread(target=takeover, daemon=True)
            taker.start()
            taker.join(3)
        self.assertFalse(taker.is_alive(), "reentrant failing takeover deadlock")
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], LifecycleHeld)
        after = self.journal.read()
        self.assertEqual(after.command, before)
        self.assertTrue(after.control.takeover_requested)
        self.assertEqual(nested, [("held", None)])
        self.assertEqual(self.commands.sent, [])
        self.assertEqual(self.review.calls, 0)

    def test_takeover_preserves_delivered_command_without_replay(self):
        self.assertEqual(self.app.dispatch_command("delivered-command"), "api_returned")
        before = self.journal.read().command
        self.assertTrue(self.app.request_takeover().takeover_requested)
        after = self.journal.read()
        self.assertEqual(after.command, before)
        self.assertEqual(self.commands.sent, ["delivered-command"])
        with self.assertRaises(LifecycleHeld):
            self.app.dispatch_command("delivered-command")
        with self.assertRaises(LifecycleHeld):
            self.app.dispatch_command("new-command")
        self.assertEqual(self.commands.sent, ["delivered-command"])
        self.assertEqual(self.app.tick(), ("held", None))

    def test_takeover_requires_fresh_manager_owner_and_processes_before_fence(self):
        original_manager = self.peers.by_role["manager"]
        original_control = self.control.state
        cases = (
            ("manager_session", lambda: self.peers.by_role.__setitem__(
                "manager", replace(original_manager, generation=2)),
             lambda: self.peers.by_role.__setitem__("manager", original_manager)),
            ("owner_epoch", lambda: setattr(self.control, "state", replace(
                original_control, owner_epoch=2)),
             lambda: setattr(self.control, "state", original_control)),
            ("manager_process", lambda: self.processes.unknown_roles.add("manager_omp"),
             lambda: self.processes.unknown_roles.discard("manager_omp")),
            ("shell_process", lambda: self.processes.unknown_roles.add("shell"),
             lambda: self.processes.unknown_roles.discard("shell")),
        )
        for name, disturb, restore in cases:
            with self.subTest(name=name):
                before = self.journal.path.read_bytes()
                disturb()
                try:
                    with patch.object(self.control, "request_takeover",
                                      wraps=self.control.request_takeover) as external:
                        with self.assertRaises(LifecycleHeld):
                            self.app.request_takeover()
                        self.assertEqual(external.call_count, 0)
                finally:
                    restore()
                self.assertEqual(self.journal.path.read_bytes(), before)
                self.assertEqual(self.commands.sent, [])
        # Even a rejected invocation permanently closes this coordinator's
        # automatic intent fence; a later owner must reconcile explicitly.
        self.assertEqual(self.app.tick(), ("held", None))
        self.assertEqual(self.review.calls, 0)


if __name__ == "__main__":
    unittest.main()
