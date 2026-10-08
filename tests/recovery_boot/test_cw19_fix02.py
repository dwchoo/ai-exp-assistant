"""p27-cw19-fix-02 (review-02 corrections).

- P2-1: the ``queued`` result never promises delivery: a pause (C-D65 drop) or a rejection leaves it unsent; the worker
  is told the manager decides again after the resume and that it resends only if asked (result text and skill).
- P3-1: a pause-store retry never writes a stale value over a pause/resume stored meanwhile.
- P3-2: a stop retry that cannot pin an identity again keeps it (unknown, not ended): ``stop_unconfirmed``.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from uuid import uuid4

from support import Owned, ticks

from workbench.app.lifecycle import LifecycleJournal
from workbench.app.recovery import Survivor, SurvivorRegistry
from workbench.backend.automation import AutomationController
from workbench.backend.flow import HandoffService, OutboundMessage
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.contracts.v1 import ActorRole, MessageKind

BOOT = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"
TO_MANAGER = Path(__file__).resolve().parents[2] / "omp_bridge" / "skills" / "to-manager" / "SKILL.md"


class Temp(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw19-fix02-", dir="/tmp"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)


# -- P2-1 ------------------------------------------------------------------------------------------------------------
class _Mailbox:
    def create_message(self, *args, **kwargs):  # never reached: the outbox thread is not started
        raise AssertionError("not started")


class QueuedTextIsTruthful(Temp):
    def enqueue(self, sender, target):
        handoffs = HandoffService(self.root / "handoffs.jsonl", mailbox=_Mailbox(), paused=lambda: False)
        self.addCleanup(handoffs.close)
        outbound = OutboundMessage(str(uuid4()), 1, str(uuid4()), sender, target, MessageKind.ANSWER,
                                   {"handoff": "to_manager", "kind": "answer", "message": "m"})
        return handoffs._queue((sender.value, str(uuid4()), 1, f"call-{uuid4()}"), outbound)

    def assert_no_promise(self, detail):
        self.assertNotRegex(detail, re.compile(r"\bit is delivered\b[^.]*\bonce\b", re.I), detail)
        self.assertRegex(detail, re.compile(r"at most once", re.I))
        self.assertRegex(detail, re.compile(r"pauses?[^.]*not sent", re.I))
        self.assertIn("Do not send it again", detail)

    def test_the_worker_result_says_a_pause_or_rejection_leaves_it_unsent(self):
        result = self.enqueue(ActorRole.WORKER, ActorRole.MANAGER)
        self.assertEqual(result["status"], "queued", result)
        detail = result["detail"]
        self.assert_no_promise(detail)
        self.assertRegex(detail, re.compile(r"rejected[^.]*not sent", re.I))
        self.assertRegex(detail, re.compile(r"manager decides again after the resume", re.I))
        self.assertRegex(detail, re.compile(r"unless asked", re.I))

    def test_the_manager_result_is_truthful_too(self):
        result = self.enqueue(ActorRole.MANAGER, ActorRole.WORKER)
        self.assertEqual(result["status"], "queued", result)
        self.assert_no_promise(result["detail"])
        self.assertIn("not_sent", result["detail"])

    def test_the_to_manager_skill_matches(self):
        line = next(l for l in TO_MANAGER.read_text(encoding="utf-8").splitlines() if "`queued` when" in l)
        self.assertNotRegex(line, re.compile(r"it is delivered once", re.I))
        self.assertRegex(line, re.compile(r"at most once", re.I))
        self.assertRegex(line, re.compile(r"pauses automation first or it is rejected", re.I))
        self.assertRegex(line, re.compile(r"manager decides again after the resume", re.I))
        self.assertRegex(line, re.compile(r"resend only if asked", re.I))


# -- P3-1 ------------------------------------------------------------------------------------------------------------
class _HookLock:
    """``_save_lock`` stand-in: runs ``hook`` once, right after the first release once armed."""

    def __init__(self):
        self._lock = threading.Lock()
        self.hook = None

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        hook, self.hook = self.hook, None
        if hook is not None:
            hook()
        return False


class PauseRetryNeverWritesAStaleValue(Temp):
    def controller(self):
        layout = DataLayout(ensure_private_dir(self.root / "data"))
        ensure_private_dir(layout.workflow)
        self.works = {"value": False}
        self.saved: list = []

        def save(paused):
            if self.works["value"]:
                self.saved.append(paused)
                return True
            return False
        self.clock = [0.0]
        controller = AutomationController(
            bridge=None, database=layout.tasks, journal=LifecycleJournal(layout.lifecycle_journal), raw=None,
            shell_pane=lambda: None, project_dir=self.root, artifacts_root=ensure_private_dir(layout.workflow / "runs"),
            boot_marker=lambda: BOOT, pause_store=SimpleNamespace(load=lambda: False, save=save),
            clock=lambda: self.clock[0], metadata=lambda source, error: None)
        self.addCleanup(controller.close)
        return controller

    def test_a_pause_during_a_resume_retry_is_the_value_on_disk(self):
        controller = self.controller()
        self.assertFalse(controller._save_pause(False), "the resume is not stored")
        self.assertEqual(controller.status()["persistence_error"], "resume_not_stored")
        lock = _HookLock()
        controller._save_lock = lock
        self.works["value"] = True
        self.clock[0] += 10
        lock.hook = controller.request_pause  # the user pauses right after the retry read the wanted value
        controller.retry_pause_store()
        self.assertTrue(controller.paused())
        self.assertEqual(self.saved[-1], True, f"the user's pause is what lands on disk: {self.saved}")
        self.assertTrue(controller._pause_desired)
        self.assertIsNone(controller.status()["persistence_error"])

    def test_a_retry_with_nothing_due_writes_nothing(self):
        controller = self.controller()
        self.works["value"] = True
        controller.retry_pause_store()
        self.assertEqual(self.saved, [])


# -- P3-2 ------------------------------------------------------------------------------------------------------------
class UnpinnableIdentityIsUnknown(unittest.TestCase):
    def setUp(self):
        self.owned = Owned()
        self.addCleanup(self.owned.cleanup)

    def registry(self, pending, pid):
        survivor = Survivor("s1", "host_shell", pid, pending[0][1], BOOT, True, state="stop_unconfirmed",
                            stop={"reason": "r", "requester": "manager", "outcome": "stop_unconfirmed",
                                  "signalled": [pid], "remaining": [pid]}, pending=list(pending))
        return SurvivorRegistry([survivor], lambda: BOOT, grace=0.2, kill_wait=2.0)

    @staticmethod
    def fail_pin(registry, pids, why):
        real = registry._pin
        registry._pin = lambda pid, start: (None, why) if pid in pids else real(pid, start)

    def test_an_identity_that_cannot_be_pinned_stays_listed(self):
        for why in ("owner_changed", "pidfd_unavailable:OSError"):
            with self.subTest(why=why):
                child = self.owned.spawn("exec sleep 30")
                start = ticks(child.pid)
                registry = self.registry([(child.pid, start)], child.pid)
                self.fail_pin(registry, {child.pid}, why)
                result = registry.stop("s1", reason="retry", requester="manager")
                self.assertEqual(result["status"], "stop_unconfirmed", result)
                self.assertIn(why, result["reason"])
                self.assertEqual(result["remaining"], [child.pid])
                self.assertEqual(result["signalled"], [])
                self.assertIsNone(child.poll(), "an unproven identity is never signalled")
                self.assertEqual([v["state"] for v in registry.alive()], ["stop_unconfirmed"],
                                 "a shutdown check still sees it")
                self.assertEqual(registry.get("s1").pending, [(child.pid, start)])

    def test_a_mixed_retry_stops_the_pinned_one_and_keeps_the_other(self):
        stubborn = self.owned.spawn("trap '' TERM HUP; while :; do sleep 0.1; done")
        other = self.owned.spawn("exec sleep 30")
        pending = [(stubborn.pid, ticks(stubborn.pid)), (other.pid, ticks(other.pid))]
        registry = self.registry(pending, stubborn.pid)
        self.fail_pin(registry, {other.pid}, "pidfd_unavailable:OSError")
        result = registry.stop("s1", reason="retry", requester="manager")
        stubborn.wait(5)
        self.assertEqual(result["status"], "stop_unconfirmed", result)
        self.assertEqual(result["signalled"], [stubborn.pid])
        self.assertEqual(result["remaining"], [other.pid])
        self.assertEqual(registry.get("s1").pending, [pending[1]])
        self.assertIsNone(other.poll())

    def test_an_ended_identity_still_ends_the_stop(self):
        child = self.owned.spawn("exit 0")
        start = ticks(child.pid) or 1
        child.wait(5)
        registry = self.registry([(child.pid, start)], child.pid)
        result = registry._signal_pending([(child.pid, start)])
        self.assertEqual(result["status"], "already_ended", result)


if __name__ == "__main__":
    unittest.main()
