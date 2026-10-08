"""P2-4 runtime: a 2 MiB paste to a raw host-shell foreground program keeps the backend responsive.

Real entrypoint, two real OMP 18.2.10 processes (local scripted provider), real
Bash. While the paste drains into ``head`` in raw mode, every UI snapshot
round trip stays under one second (C-AC-19), and the bytes arrive complete.
"""
import shlex
import time
import unittest

from live_harness import LiveBackend, find_omp
from workbench.backend.client import UiClient
from workbench.contracts.ui_v1 import MAX_PASTE_BYTES
from workbench.contracts.v1 import PaneId

OMP = find_omp()


@unittest.skipUnless(OMP, "real OMP is required (version recorded, not pinned: C-D72 (2))")
class LivePasteResponsivenessTests(unittest.TestCase):
    def test_2mib_raw_paste_keeps_snapshot_latency_under_one_second(self):
        live = LiveBackend(OMP, path="/usr/bin:/bin")

        def finish():
            if live.backend_processes():
                live.shutdown()
            deadline = time.monotonic() + 15
            while any(live.leaks().values()) and time.monotonic() < deadline:
                time.sleep(0.05)
            left = {k: v for k, v in live.leaks().items() if v}
            live.cleanup()
            self.assertEqual(left, {}, "owned process/socket leak")
        self.addCleanup(finish)
        result = live.cli(*live.start_args("--no-attach"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = time.monotonic() + 120
        status = None
        while time.monotonic() < deadline:
            status = live.status()
            if status is not None and status["phase"] != "starting":
                break
            time.sleep(0.2)
        live.remember(status)
        self.assertEqual(status["phase"], "ready", status)
        out = live.project / "pasted"
        payload = (bytes(range(32, 127)) * (MAX_PASTE_BYTES // 95 + 1))[:MAX_PASTE_BYTES]
        with UiClient(live.data / "ui.sock") as client:
            self.assertTrue(client.attach((30, 100))["ok"])
            command = f"stty raw -echo; head -c {len(payload)} > {shlex.quote(str(out))}; stty sane\r"
            self.assertTrue(client.input(PaneId.HOST_SHELL, command.encode())["ok"])
            deadline = time.monotonic() + 10
            while client.snapshot()["panes"]["host_shell"]["shell"]["parent_mode"] != "manual_foreground":
                self.assertLess(time.monotonic(), deadline)
                client.pump(0.05)
                client.displays.clear()
            client.pump(0.3)
            began = time.monotonic()
            pasted = client.paste(PaneId.HOST_SHELL, payload)
            self.assertTrue(pasted["ok"], pasted)
            worst = time.monotonic() - began
            while (not out.exists() or out.stat().st_size < len(payload)) and time.monotonic() - began < 60:
                start = time.monotonic()
                snapshot = client.snapshot()
                worst = max(worst, time.monotonic() - start)
                client.displays.clear()
                time.sleep(0.05)
            pane = snapshot["panes"]["host_shell"]
            self.assertEqual((pane["dropped_input_bytes"], pane["last_input_problem"]), (0, None))
            self.assertEqual(out.read_bytes(), payload)
            self.assertLess(worst, 1.0, f"backend request latency {worst:.2f}s during paste")
            client.detach()


if __name__ == "__main__":
    unittest.main()
