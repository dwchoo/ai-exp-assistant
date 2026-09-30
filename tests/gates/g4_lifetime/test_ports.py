import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from workbench.runtime.g4 import MetadataPort, WakePort, FrontendLease


class RuntimePortTests(unittest.TestCase):
    def approval(self):
        return {"approved": True, "scope": "one-no-tools-worker-wake"}

    def test_real_fsync_success_permits_once_and_failure_holds_without_killing_run(self):
        with tempfile.TemporaryDirectory() as directory, subprocess.Popen(["/bin/sleep", "10"]) as existing:
            try:
                store = MetadataPort(Path(directory) / "metadata")
                delivered = []
                wake = WakePort(store, delivered.append)
                wake.arm(self.approval(), now=0)
                self.assertFalse(wake.tick(now=59.99))
                self.assertTrue(wake.tick(now=60))
                self.assertEqual(json.loads(store.path.read_text())["kind"], "wake")
                self.assertFalse(wake.tick(now=61))
                self.assertEqual(len(delivered), 1)
                store.fail = True
                failure = WakePort(store, delivered.append)
                failure.arm(self.approval(), now=0)
                self.assertFalse(failure.tick(now=60))
                self.assertEqual(failure.status, "held_metadata")
                self.assertEqual(len(delivered), 1)
                self.assertIsNone(existing.poll())
            finally:
                existing.terminate()
                existing.wait(timeout=2)

    def test_pause_consumes_tick_without_replay_and_attach_has_no_owner_change(self):
        with tempfile.TemporaryDirectory() as directory:
            delivered = []
            wake = WakePort(MetadataPort(Path(directory) / "metadata"), delivered.append)
            wake.arm(self.approval(), now=0)
            wake.paused = True
            self.assertFalse(wake.tick(now=60))
            wake.paused = False
            self.assertFalse(wake.tick(now=61))
            self.assertEqual(delivered, [])
            self.assertEqual(wake.ticks, 1)
            lease = FrontendLease()
            before = lease.owner, lease.owner_epoch
            lease.attach()
            with self.assertRaises(ValueError): lease.attach()
            lease.detach(); lease.attach()
            self.assertEqual((lease.owner, lease.owner_epoch), before)
