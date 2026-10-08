"""CW-16 fix-01 on a real in-process backend with fake OMPs (no model).

B3 F1 (C-AC-22): an OMP's daemon broker (a direct child of the Workbench OMP in its own session, like OMP
18.8.0's ``__omp_worker_daemon_broker``) is part of the full shutdown: waited for, ended only by its exact
identity when it lingers, and never left alive under ``verified: true``. D-B2-1: the live host shell's exported
names (not the start-up environment) are what the experiment admission sees.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from support import ticks  # noqa: E402
from test_backend_restart import BackendFixture, alive  # noqa: E402

from workbench.contracts.v1 import PaneId  # noqa: E402


class BrokerShutdownTests(BackendFixture):
    def setUp(self):
        super().setUp()
        self.brokers_file = self.root / "brokers.jsonl"
        self.env["FAKE_BROKERS"] = str(self.brokers_file)

    def start_broker(self, linger: float) -> int:
        backend = self.start()
        pane = backend.panes[PaneId.WORKER_OMP]
        self.assertIsNone(pane.admit(f"broker {linger}\n".encode()))
        self.wait(lambda: self.brokers_file.exists() and self.brokers_file.read_text().strip(), "the broker started")
        pid = json.loads(self.brokers_file.read_text().splitlines()[-1])["pid"]
        self.owned.remember(pid)  # cleanup only (exact identity)
        return pid

    def shutdown(self) -> dict:
        backend, self.backend = self.backend, None
        backend._shutdown_confirmed = True
        return backend._close()

    def test_a_broker_that_ends_after_its_omp_is_waited_for_before_verified(self):
        pid = self.start_broker(1.0)
        start = ticks(pid)
        result = self.shutdown()
        self.assertTrue(result["verified"], result)
        self.assertFalse(alive(pid), "an owned broker outlived a verified shutdown")
        observed = result["omp_children"]["observed"]
        self.assertEqual([(o["pid"], o["start_ticks"], o["parent"], o["ended"]) for o in observed],
                         [(pid, start, "worker_omp", "by_itself")], result["omp_children"])
        self.assertEqual(result["omp_children"]["alive"], [])

    def test_a_lingering_broker_is_ended_by_its_exact_identity(self):
        pid = self.start_broker(-1)
        result = self.shutdown()
        self.assertTrue(result["verified"], result)
        self.assertFalse(alive(pid))
        self.assertEqual([o["ended"] for o in result["omp_children"]["observed"]], ["terminated"])

    def test_a_broker_that_cannot_be_ended_is_never_verified(self):
        pid = self.start_broker(-1)
        real = signal.pidfd_send_signal

        def refuse(fd, signum, *args):
            raise PermissionError(1, "Operation not permitted")

        with mock.patch("workbench.backend.panes.signal.pidfd_send_signal", refuse):
            result = self.shutdown()
        self.assertFalse(result["verified"], result)
        self.assertIn("omp_detached_children_alive", result["problems"])
        self.assertEqual([(a["pid"], a["why_not"]) for a in result["omp_children"]["alive"]],
                         [(pid, "permission_denied")])
        self.assertTrue(alive(pid))
        del real

    def test_a_broker_is_pinned_before_the_run_stop_fence_ends_its_omp(self):
        # Live X4: the broker started < 2 s before the shutdown and the bound run's stop fence TERMed the OMP before
        # the panes closed; the broker was reparented and never seen. It must be pinned before the fence.
        pid = self.start_broker(-1)
        backend = self.backend
        pane = backend.panes[PaneId.WORKER_OMP]

        def fence():  # what the bound run's at-most-once stop does to the OMPs (exact identity, TERM)
            os.kill(pane.pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while pane.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            return None

        backend.automation_loop.shutdown_bound = fence
        pane._detached.clear()  # nothing from the periodic scan: only the shutdown's own scan can find it
        pane._detached_scan_at = time.monotonic() + 60
        result = self.shutdown()
        self.assertEqual([o["pid"] for o in result["omp_children"]["observed"]], [pid], result["omp_children"])
        self.assertFalse(alive(pid))
        self.assertTrue(result["verified"], result)

    def test_a_process_that_is_not_the_omps_child_is_never_a_candidate(self):
        # A user's own broker: same kind of process (own session) but not started by a Workbench OMP.
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(other.wait, 5)
        self.addCleanup(lambda: other.poll() is None and other.kill())
        pid = self.start_broker(0.5)
        result = self.shutdown()
        self.assertTrue(result["verified"], result)
        self.assertEqual([o["pid"] for o in result["omp_children"]["observed"]], [pid])
        self.assertIsNone(other.poll(), "a process the Workbench OMPs did not start was touched")


class LiveShellEnvironmentTests(BackendFixture):
    def test_a_variable_exported_after_start_is_seen_and_the_start_up_environment_is_not_used(self):
        backend = self.start()
        self.assertNotIn("WB_FIX01_LATE", self.env)
        self.assertEqual(backend._shell_exported_names(["PATH", "WB_FIX01_LATE"]), {"PATH"})
        self.assertIsNone(backend.shell.admit(b"export WB_FIX01_LATE=1\r"))
        self.wait(lambda: backend._shell_exported_names(["WB_FIX01_LATE"]) == {"WB_FIX01_LATE"},
                  "the late export seen by the live shell", timeout=10)
        self.assertIsNone(backend.shell.admit(b"unset PATH_IS_NOT_UNSET_HERE; export -n WB_FIX01_LATE\r"))
        self.wait(lambda: backend._shell_exported_names(["WB_FIX01_LATE"]) == set(),
                  "an un-exported name is missing again", timeout=10)


if __name__ == "__main__":
    unittest.main()
