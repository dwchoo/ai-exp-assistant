"""Independent failure controls for lifetime evidence and durable dispatch."""
from __future__ import annotations

import copy
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import live_detach_probe as probe
from workbench.runtime import g4


class LifetimeEvidenceBoundaryTests(unittest.TestCase):
    def run_evidence(self, defect="", *, paused=False, residue_pid=None):
        # Disposable frontend protocol double: only evidence is mutated. No
        # duplicate implementation of the probe's pass predicate lives here.
        identity = {"backend_pid": 900001, "omp_pids": {"manager": 900002, "worker": 900003},
                    "omp_sessions": {"manager": ["m-session", 1], "worker": ["w-session", 1]},
                    "parent_shell_pid": 900004, "supervisor_pid": 900005, "experiment_pid": 900006,
                    "control_mode": "managed_control_wait", "owner": "manager", "owner_epoch": 1,
                    "user_owner": "user", "user_owner_epoch": 1}
        if defect == "pid_alias":
            identity["omp_pids"]["worker"] = identity["omp_pids"]["manager"]
        if defect == "supervisor_alias":
            identity["supervisor_pid"] = identity["parent_shell_pid"]
        if defect == "backend_substitution":
            identity["backend_pid"] = 999999  # Popen.pid remains 900001.
        if defect == "non_omp_identity":
            identity["omp_pids"]["worker"] = None
        clock = [0.0]
        replies = []
        final = {"residue": [], "pause_ack": "paused", "owned_pids": [900002, 900003, 900004, 900005, 900006]}
        if defect == "residue":
            final["residue"] = [900002]
        if defect == "residue_masked":
            final["owned_pids"] = [residue_pid]
        child = SimpleNamespace(pid=900001, returncode=None)
        child.poll = lambda: child.returncode

        def communicate():
            child.returncode = 0
            return json.dumps(final), ""
        child.communicate = communicate
        child.terminate = lambda: setattr(child, "returncode", -15)
        child.kill = lambda: setattr(child, "returncode", -9)

        class PipeThread:
            def __init__(self, target, **_kwargs): self.target = target
            def start(self): pass
            def join(self, *_args): self.target()
            def is_alive(self): return False

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def settimeout(self, *_args): pass
            def connect(self, *_args): pass
            def sendall(self, data): self.op = json.loads(data)["op"]
            def recv(self, _limit):
                completed = self.op == "snapshot" or (self.op == "attach" and len(replies) >= 3)
                state = {**copy.deepcopy(identity), "attached": self.op != "detach", "ticks": int(completed),
                         "deliveries": int(completed and not paused), "provider_requests": {"manager": 0, "worker": int(completed and not paused)},
                         "agent_start": int(completed and not paused), "agent_end": int(completed and not paused),
                         "semantic_match": completed and not paused, "host_observations": 100 if completed else len(replies) + 1,
                         "host_running": True, "owned_alive": True, "host_unknowns": [], "host_main_exit": None}
                if self.op == "detach" and defect == "detach_not_detached": state["attached"] = True
                if completed and defect == "attach_not_attached": state["attached"] = False
                if completed and defect == "changed_pid": state["omp_pids"]["worker"] = 900100
                if completed and defect == "attach_owner_change": state["user_owner_epoch"] = 2
                if completed and defect == "stopped_observation": state["host_observations"] = 1
                if completed and defect == "stopped_host": state["host_running"] = False
                if completed and defect == "fake_delivery": state["semantic_match"] = False
                if completed and defect == "pause_leak": state["provider_requests"]["worker"] = 1
                if completed and defect == "short_detach": clock[0] = 59
                replies.append(state)
                return json.dumps(state).encode()

        actual_exists = Path.exists
        with ExitStack() as stack:
            stack.enter_context(patch.object(probe.subprocess, "run", return_value=SimpleNamespace(stdout="omp/18.2.10")))
            stack.enter_context(patch.object(probe.subprocess, "Popen", return_value=child))
            stack.enter_context(patch.object(probe.threading, "Thread", PipeThread))
            stack.enter_context(patch.object(probe.socket, "socket", return_value=Connection()))
            stack.enter_context(patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]))
            stack.enter_context(patch.object(probe.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)))
            stack.enter_context(patch.object(Path, "exists", lambda path: True if path.name == "frontend.sock" else actual_exists(path)))
            return probe.run(paused)

    def test_known_pass_and_existing_failure_controls_are_discriminating(self):
        self.assertEqual(self.run_evidence()["result"], "passed")
        self.assertEqual(self.run_evidence(paused=True)["result"], "passed")
        for defect in ("changed_pid", "attach_owner_change", "stopped_observation", "stopped_host",
                       "fake_delivery", "short_detach", "residue"):
            with self.subTest(defect=defect):
                self.assertNotEqual(self.run_evidence(defect)["result"], "passed")
        self.assertNotEqual(self.run_evidence("pause_leak", paused=True)["result"], "passed")

    def test_distinct_and_bound_process_identities_are_required(self):
        for defect in ("pid_alias", "supervisor_alias", "backend_substitution", "non_omp_identity"):
            with self.subTest(defect=defect):
                self.assertNotEqual(self.run_evidence(defect)["result"], "passed")

    def test_frontend_detach_and_reattach_must_really_change_display_lease(self):
        for defect in ("detach_not_detached", "attach_not_attached"):
            with self.subTest(defect=defect):
                self.assertNotEqual(self.run_evidence(defect)["result"], "passed")

    def test_live_owned_residue_cannot_be_masked_by_empty_backend_residue_flag(self):
        leaked = subprocess.Popen(["/bin/sleep", "10"])
        try:
            self.assertTrue(Path(f"/proc/{leaked.pid}").exists())
            result = self.run_evidence("residue_masked", residue_pid=leaked.pid)
            self.assertIsNone(leaked.poll())
            self.assertNotEqual(result["result"], "passed")
        finally:
            leaked.terminate()
            leaked.wait(timeout=2)
        self.assertFalse(Path(f"/proc/{leaked.pid}").exists())


class DurableDispatchBoundaryTests(unittest.TestCase):
    def test_file_and_directory_fsync_failures_hold_new_dispatch_without_killing_run(self):
        existing = subprocess.Popen(["/bin/sleep", "15"])
        try:
            for fail_at in (1, 2):
                with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as directory:
                    calls = []
                    original = g4.os.fsync
                    def sync(fd):
                        calls.append(fd)
                        if len(calls) == fail_at: raise OSError("independent fsync failure")
                        original(fd)
                    deliveries = []
                    wake = g4.WakePort(g4.MetadataPort(Path(directory) / "metadata"), deliveries.append)
                    wake.arm({"approved": True, "scope": "one-no-tools-worker-wake"}, now=0)
                    with patch.object(g4.os, "fsync", sync):
                        self.assertFalse(wake.tick(now=60))
                    self.assertEqual(wake.status, "held_metadata")
                    self.assertFalse(wake.tick(now=120))
                    self.assertEqual(deliveries, [])
                    self.assertIsNone(existing.poll())
        finally:
            existing.terminate()
            existing.wait(timeout=2)
        self.assertFalse(Path(f"/proc/{existing.pid}").exists())

    def test_dispatch_occurs_after_both_syncs_and_callback_failure_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            original = g4.os.fsync
            def sync(fd):
                original(fd)
                calls.append("synced")
            def delivery(_approval):
                self.assertEqual(calls, ["synced", "synced"])
                calls.append("delivery")
                raise RuntimeError("lost API outcome")
            wake = g4.WakePort(g4.MetadataPort(Path(directory) / "metadata"), delivery)
            wake.arm({"approved": True, "scope": "one-no-tools-worker-wake"}, now=0)
            with patch.object(g4.os, "fsync", sync), self.assertRaises(RuntimeError): wake.tick(now=60)
            self.assertEqual(wake.status, "unknown")
            self.assertFalse(wake.tick(now=120))
            self.assertEqual(calls, ["synced", "synced", "delivery"])


if __name__ == "__main__": unittest.main()
