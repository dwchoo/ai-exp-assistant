"""CW-16 O4 (C-AC-22): a full shutdown while both OMPs are in a turn that takes seconds to abort, through the
product entrypoint (``python -m workbench``) with fake OMPs (no model, no provider, no network) and a fake HOME.

- the confirm is answered at once (before the close, not after it), so a client never times out on it;
- both OMPs get SIGTERM together (one shared grace, not one grace per OMP);
- ``shutdown --yes`` (human form) shows progress on stderr, never a traceback, and ends with the verified result;
  ``--json`` ends with the result JSON on stdout.
Every process is started by this test under its own /tmp root and only those exact pids are ever signalled.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from support import SRC, ticks

from workbench.backend.client import UiClient
from workbench.contracts.ui_v1 import ClientType

FAKE_OMP = Path(__file__).with_name("fake_omp.py")
BLOCKED_PROXY = "http://127.0.0.1:9"
SLOW_ABORT = 8.0  # longer than the backend's OMP grace: the OMPs are KILLed after it


def kill_exact(pid: int, start: int) -> None:
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if ticks(pid) == start:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


class LiveShutdownSlowTurnTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw16-o4-", dir="/tmp"))
        self.addCleanup(self.cleanup)
        home = self.root / "home"
        (home / ".omp" / "agent").mkdir(parents=True)
        (home / ".omp" / "agent" / "agent.db").write_bytes(b"")  # fake empty store, no credential
        self.data = home / "wbdata"
        self.project = self.root / "project"
        self.project.mkdir()
        self.fake = self.root / "omp"
        self.fake.write_text(f"#!{sys.executable}\n" + FAKE_OMP.read_text())
        self.fake.chmod(0o700)
        self.terms = self.root / "terms.jsonl"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "SHELL": "/usr/bin/bash", "LANG": "C.UTF-8",
                    "TERM": "xterm-256color", "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".local/state"),
                    "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
                    "FAKE_PANE_RECORD": str(self.root / "panes.jsonl"), "FAKE_NOTICES": str(self.root / "n.jsonl"),
                    "FAKE_FRAMES": str(self.root / "frames.jsonl"), "FAKE_TERMS": str(self.terms),
                    **{key: BLOCKED_PROXY for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                                                      "https_proxy", "all_proxy")}}

    def cli(self, *args, timeout=150):
        return subprocess.run([sys.executable, "-m", "workbench", *args, "--data-dir", str(self.data)],
                              env=self.env, cwd=self.project, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)

    def ours(self):
        found = {}
        for name in os.listdir("/proc"):
            if not name.isdigit() or int(name) == os.getpid():
                continue
            try:
                cmdline = Path(f"/proc/{name}/cmdline").read_bytes()
                cwd = os.readlink(f"/proc/{name}/cwd")
            except OSError:
                continue
            if str(self.root).encode() in cmdline or cwd.startswith(str(self.root)):
                start = ticks(int(name))
                if start:
                    found[int(name)] = start
        return found

    def cleanup(self):
        try:
            if (self.data / "ui.sock").exists():
                self.cli("shutdown", "--yes", timeout=200)
        except Exception:
            pass
        deadline = time.monotonic() + 10
        while self.ours() and time.monotonic() < deadline:
            for pid, start in self.ours().items():
                kill_exact(pid, start)
            time.sleep(0.2)
        shutil.rmtree(self.root, ignore_errors=True)

    def busy_backend(self):
        """Started, both OMPs abort a turn slowly on SIGTERM, a worker command runs in the host shell."""
        result = self.cli("start", "--no-attach", "--omp", str(self.fake))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            status = json.loads(self.cli("status", "--json", timeout=30).stdout or "{}")
            if (status.get("snapshot") or {}).get("phase") == "ready":
                break
            time.sleep(0.3)
        else:
            self.fail("backend not ready")
        client = UiClient(self.data / "ui.sock", name="cw16-o4", timeout=10)
        client.attach((30, 120))
        for pane in ("manager_omp", "worker_omp"):
            self.assertTrue(client.request(ClientType.INPUT, f"slow-term {SLOW_ABORT}\n".encode(), pane=pane)["ok"])
        self.assertTrue(client.request(ClientType.INPUT, b'tool terminal {"command": "sleep 100"}\n',
                                       pane="worker_omp")["ok"])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            active = client.request(ClientType.SHUTDOWN_REQUEST)["active"]
            if active:
                break
            client.pump(0.3)
        self.assertTrue(active, "the worker command is not listed as active work")
        return client

    def term_times(self):
        lines = [json.loads(line) for line in self.terms.read_text().splitlines()] if self.terms.exists() else []
        return {item["role"]: item["at"] for item in lines}

    def test_confirm_answered_at_once_and_both_omps_signalled_together(self):
        client = self.busy_backend()
        with client:
            token = client.request(ClientType.SHUTDOWN_REQUEST)["token"]
            sent = time.monotonic()
            answer = client.request(ClientType.SHUTDOWN_CONFIRM, timeout=2.5, token=token)
            answered = time.monotonic() - sent
            self.assertTrue(answer["ok"], answer)
            self.assertLess(answered, 2.5)
            self.assertGreater(answer.get("result_deadline", 0), 10, answer)
            deadline = time.monotonic() + answer["result_deadline"]
            while client.closing is None and time.monotonic() < deadline:
                if not client.pump(0.2):
                    break
            closed = time.monotonic() - sent
        result = (client.closing or {}).get("result")
        self.assertIsNotNone(result, "no shutdown result")
        self.assertTrue(result["verified"], result)
        self.assertGreater(closed, answered + 1.0, "the answer must come before the (slow) close ends")
        times = self.term_times()
        self.assertEqual(set(times), {"manager", "worker"}, times)
        self.assertLess(abs(times["manager"] - times["worker"]), 1.0, f"OMPs signalled one after another: {times}")

    def test_cli_human_form_shows_progress_and_the_verified_result(self):
        self.busy_backend().close()
        result = self.cli("shutdown", "--yes")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("active work:", result.stdout)
        self.assertIn("종료 중", result.stderr)
        line = [item for item in result.stdout.splitlines() if item.startswith("shutdown result: ")][-1]
        self.assertTrue(json.loads(line[len("shutdown result: "):])["verified"])
        self.assertNotIn("종료 확인 실패", result.stderr)

    def test_cli_json_form_ends_with_the_result(self):
        self.busy_backend().close()
        result = self.cli("shutdown", "--yes", "--json")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["shutdown"]["verified"])
        self.assertNotIn("종료 중", result.stderr)


if __name__ == "__main__":
    unittest.main()
