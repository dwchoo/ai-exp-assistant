"""Runs the CW-18 bridge-extension Node tests under Node.

- tests/bridge/handoff_tools.test.ts: U1 tools ``to_worker``/``to_manager``.
- tests/bridge/provider_identity.test.ts: smoke-02 E1, delivery identity in every captured OMP payload shape.
"""

from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("node"), "node is required for the bridge extension tests")
class HandoffToolsExtensionTests(unittest.TestCase):
    def test_bridge_extension_handoff_tools(self):
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--no-warnings", "--test",
             str(ROOT / "tests/bridge/handoff_tools.test.ts")],
            cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertIn("# fail 0", result.stdout)

    def test_bridge_extension_provider_payload_identity(self):
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--no-warnings", "--test",
             str(ROOT / "tests/bridge/provider_identity.test.ts")],
            cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertIn("# fail 0", result.stdout)


if __name__ == "__main__":
    unittest.main()
