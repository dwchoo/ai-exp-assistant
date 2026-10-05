"""Opt-in (WB_LIVE_CW18=1): the CW-18 smoke D1/D2 regression with two real OMPs and a scripted provider.

Runs ``live_strict_placeholder_probe.py`` (fake HOME, scripted 127.0.0.1 provider, no model) and requires exit 0.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
OMP = os.environ.get("WB_OMP") or shutil.which("omp") or str(Path.home() / ".local/bin/omp")


@unittest.skipUnless(os.environ.get("WB_LIVE_CW18") == "1" and Path(OMP).exists(),
                     "set WB_LIVE_CW18=1 (real OMP, scripted provider, no model)")
class StrictPlaceholderLiveTests(unittest.TestCase):
    def test_smoke_shaped_placeholders_dispatch_and_report_cleanly(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run([sys.executable, str(ROOT / "tests/bridge/live_strict_placeholder_probe.py"), OMP],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])
        outcome = json.loads(result.stdout)
        self.assertTrue(outcome["ok"] and all(outcome["checks"].values()), outcome["checks"])


if __name__ == "__main__":
    unittest.main()
