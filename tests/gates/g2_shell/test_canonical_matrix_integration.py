"""Independent live integration evidence across the three migrated matrices."""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import signal
import sys
import unittest
from unittest import mock

from tests.gates.g2_shell import live_input_jobs_probe as inputs
from tests.gates.g2_shell import live_hook_env_probe as hooks
from tests.gates.g2_shell import live_takeover_probe as takeover
from tests.gates.g2_shell.test_common_run_protocol_independent import supervisor_mutation
from workbench.terminal.shell_g2 import lifecycle
from workbench.terminal.shell_g2.prototype import UnsafeShellState


@unittest.skipUnless(sys.platform == "linux" and shutil.which("bash") and shutil.which("dash"),
                     "Linux/Bash/dash required")
class CanonicalMatrixIntegrationTests(unittest.TestCase):
    @contextmanager
    def observe_runs(self, missing_exec=False):
        # Stop the supervisor at the real kernel-confirmed exec event, before
        # detach, so even ':' provides inspectable live process identities.
        barrier = """
original_emit = p._emit
def held_emit(event):
    original_emit(event)
    if event.startswith('EXEC_READY:'):
        p._await_release(p.signal.SIGUSR2)
p._emit = held_emit
"""
        original = lifecycle.ManagedLifecycleProbe._on_control_event
        sessions = []
        identities = []

        def observe(session, event):
            if session not in sessions:
                sessions.append(session)
            if not (missing_exec and event.startswith("EXEC_READY:")):
                original(session, event)
            if event.startswith("EXEC_READY:"):
                child = int(event.partition(":")[2])
                supervisor = session.lifecycle.supervisor_pid
                self.assertEqual(inputs.combined.proc_fields(child)[0], supervisor)
                self.assertEqual(inputs.combined.proc_fields(supervisor)[0], session.pid)
                self.assertEqual(os.getsid(child), session.pid)
                self.assertEqual(os.getsid(supervisor), session.pid)
                self.assertEqual(os.getpgid(child), child)
                self.assertNotEqual(os.getpgid(supervisor), child)
                self.assertEqual(os.tcgetpgrp(session.master_fd), child)
                self.assertNotIn(inputs.combined.proc_fields(child)[3], {"Z", "X"})
                self.assertNotIn(inputs.combined.proc_fields(supervisor)[3], {"T", "Z", "X"})
                identities.append((session.pid, supervisor, child))
                os.kill(supervisor, signal.SIGUSR2)

        with supervisor_mutation(barrier), \
             mock.patch.object(inputs, "managed_controller_source", side_effect=lambda shell: lifecycle.managed_controller_source(shell)), \
             mock.patch.object(hooks, "managed_controller_source", side_effect=lambda shell: lifecycle.managed_controller_source(shell)), \
             mock.patch.object(lifecycle.ManagedLifecycleProbe, "_on_control_event", observe):
            yield sessions, identities
        for parent, supervisor, child in identities:
            for pid in (parent, supervisor, child):
                self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def assert_causal_runs(self, session, count):
        events = session.events
        accepts = [i for i, event in enumerate(events) if event == "ACCEPT"]
        self.assertEqual(len(accepts), count)
        order = ("ACCEPT", "SUPERVISOR:", "CHILD_PREPARED:", "FOREGROUND_VERIFIED:",
                 "EXEC_READY:", "CHILD:", "EXPERIMENT_START:", "MAIN_RETURN:",
                 "WAIT_EMPTY:ECHILD", "LIFETIME_DONE:", "INPUT_BARRIER",
                 "INPUT_RELEASED", "RETURN:", f"WAIT:{session.pid}:")
        for index, begin in enumerate(accepts):
            end = accepts[index + 1] if index + 1 < len(accepts) else len(events)
            run = events[begin:end]
            positions = [next(i for i, event in enumerate(run) if event.startswith(prefix))
                         for prefix in order]
            self.assertEqual(positions, sorted(positions), run)
            prepared = next(event for event in run if event.startswith("CHILD_PREPARED:"))
            child = prepared.split(":")[1]
            self.assertIn(f"EXEC_READY:{child}", run)
            self.assertIn(f"EXPERIMENT_START:{child}", run)
            if index + 1 < len(accepts):
                returned = begin + positions[-1]
                transition = events[returned:end]
                transitions = (f"TAKEOVER_ACK:{session.pid}", "READY",
                               f"HANDOFF:{session.pid}", f"WAIT:{session.pid}:")
                cursor = 1  # Exclude the previous request's WAIT itself.
                for prefix in transitions:
                    cursor = next(i for i in range(cursor, len(transition))
                                  if transition[i].startswith(prefix)) + 1

    def test_three_matrices_share_live_identities_and_complete_boundaries(self):
        cases = ((inputs, "clean", 2), (hooks, "background", 1),
                 (hooks, "escaped", 1), (takeover, "after_start", 1))
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for module, name, count in cases:
                with self.subTest(shell=shell, matrix=module.__name__, case=name):
                    with self.observe_runs() as (sessions, identities):
                        result = module.case(shell, name)
                    self.assertEqual(len(sessions), 1)
                    self.assertEqual(len(identities), count)
                    self.assert_causal_runs(sessions[0], count)
                    if module is inputs:
                        self.assertTrue(result["marker_absent"])
                        self.assertEqual(result["successful_dispatches"], 2)
                    elif module is hooks:
                        self.assertTrue(result["same_parent_recheck_proceeded"])
                        self.assertFalse(result["prior_scope_completed"])
                    else:
                        self.assertEqual(result["run_writes"], 1)
                        self.assertTrue(result["marker_absent"])

    def test_lost_and_delayed_takeover_ack_keep_request_and_confirmation_separate(self):
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for name in ("ack_lost", "ack_delayed"):
                with self.subTest(shell=shell, case=name):
                    with self.observe_runs() as (sessions, identities):
                        result = takeover.case(shell, name)
                    session = sessions[0]
                    self.assert_causal_runs(session, 1)
                    self.assertEqual(len(identities), 1)
                    self.assertEqual(result["request_state"], "unknown")
                    self.assertEqual(result["run_writes"], 1)
                    self.assertEqual(result["takeover_writes"], 1)
                    self.assertTrue(result["marker_absent"])
                    if name == "ack_lost":
                        self.assertFalse(result["manual_prompt_confirmed"])
                        self.assertNotIn(f"TAKEOVER_ACK:{session.pid}", session.events)
                    else:
                        self.assertTrue(result["manual_prompt_confirmed"])
                        self.assertLess(session.events.index("ACK_BARRIER"),
                                        session.events.index(f"TAKEOVER_ACK:{session.pid}"))

    def test_missing_exec_boundary_is_detected_by_each_live_matrix_and_cannot_rearm(self):
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for module, name in ((inputs, "clean"), (hooks, "background"), (takeover, "after_start")):
                with self.subTest(shell=shell, matrix=module.__name__):
                    with self.observe_runs(missing_exec=True) as (sessions, identities):
                        with self.assertRaises((AssertionError, UnsafeShellState)):
                            module.case(shell, name)
                    self.assertEqual(len(identities), 1)
                    session = sessions[0]
                    self.assertTrue(session.lifecycle.unknown)
                    self.assertFalse(session.lifecycle.exec_ready)
                    self.assertFalse(session.lifecycle.returned)
                    self.assertEqual(session.events.count("ACCEPT"), 1)
                    with self.assertRaises(UnsafeShellState):
                        session.rearm_after_handoff()


if __name__ == "__main__":
    unittest.main()
