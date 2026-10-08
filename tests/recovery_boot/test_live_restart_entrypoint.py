"""CW-19 real processes through the product entrypoint (``python -m workbench``), with fake OMPs (no model, no
provider, no network) and a fake HOME; every process is started by this test under its own /tmp root.

1. ``start`` -> ready. The user starts a ``nohup`` job in the host shell, the manager (fake) dispatches a work
   Task (its TASK is never accepted by the fake worker: queued, not sent), the manager OMP ignores SIGHUP
   (``survive``) and the user pauses Workbench.
2. The backend is SIGKILLed (exact pid + start ticks) and started again with ``start``: the reconcile says
   ``same_boot_crash``; the old manager OMP is a proven survivor, the ``nohup`` job is shown only (its session
   leader died); the Task is held, its TASK is listed as not sent and never re-sent (no new run, no delivery to the
   new worker); the pause is kept; no ``backend_restarted`` notice while paused, exactly one after the resume.
   The new manager ends the old manager with ``stop_survivor``; ``shutdown --yes`` lists the shown survivor and is
   not reported as verified (exit 1).
3. The boot markers are changed to a different boot (as after a reboot): the next ``start`` holds automatic work
   (``to_worker`` held, no new run) until ``confirm-boot`` (refused without --yes/tty, accepted with --yes).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
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
OTHER_BOOT = "00000000-0000-4000-8000-00000000c019"


def alive(pid: int, start: int | None = None) -> bool:
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
    except (OSError, IndexError):
        return False
    return fields[0] not in (b"Z", b"X") and (start is None or int(fields[19]) == start)


def kill_exact(pid: int, start: int, signum: int = signal.SIGKILL) -> bool:
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return False
    try:
        if ticks(pid) != start:
            return False
        signal.pidfd_send_signal(fd, signum)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


class LiveRestartEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw19-live-", dir="/tmp"))
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
        self.notices, self.frames = self.root / "notices.jsonl", self.root / "frames.jsonl"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "SHELL": "/usr/bin/bash", "LANG": "C.UTF-8",
                    "TERM": "xterm-256color", "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".local/state"),
                    "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
                    "FAKE_PANE_RECORD": str(self.root / "panes.jsonl"), "FAKE_NOTICES": str(self.notices),
                    "FAKE_FRAMES": str(self.frames),
                    **{key: BLOCKED_PROXY for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                                                      "https_proxy", "all_proxy")}}

    # -- helpers -----------------------------------------------------------------------------------
    def cli(self, *args, timeout=150, stdin=subprocess.DEVNULL):
        return subprocess.run([sys.executable, "-m", "workbench", *args, "--data-dir", str(self.data)],
                              env=self.env, cwd=self.project, stdin=stdin, capture_output=True, text=True,
                              timeout=timeout)

    def start(self):
        result = self.cli("start", "--no-attach", "--omp", str(self.fake))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.wait_status(lambda s: s["phase"] == "ready", "ready")

    def status(self):
        result = self.cli("status", "--json", timeout=30)
        try:
            value = json.loads(result.stdout)
        except ValueError:
            return None
        return value.get("snapshot") if value.get("running") else None

    def wait_status(self, predicate, what, timeout=40):
        deadline = time.monotonic() + timeout
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = self.status()
            if snapshot is not None and predicate(snapshot):
                return snapshot
            time.sleep(0.3)
        self.fail(f"timed out: {what}; last={json.dumps(snapshot)[:1500] if snapshot else None}")

    def attached(self):
        client = UiClient(self.data / "ui.sock", name="cw19-live", timeout=10)
        client.attach((30, 120))
        return client

    def type(self, client, pane, text):
        answer = client.request(ClientType.INPUT, text.encode(), pane=pane)
        self.assertTrue(answer.get("ok"), answer)

    def lines(self, path):
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def tool_results(self, session):
        return [item["frame"]["result"] for item in self.lines(self.frames)
                if item["session"] == session and item["frame"].get("kind") == "tool_result"]

    def call(self, client, session, tool, args, timeout=20):
        before = len(self.tool_results(session))
        self.type(client, "manager_omp", f"tool {tool} {json.dumps(args)}\n")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            results = self.tool_results(session)
            if len(results) > before:
                return results[before]
            client.pump(0.1)
        self.fail(f"no {tool} result")

    def runs(self):
        connection = sqlite3.connect(f"file:{self.data / 'tasks.sqlite3'}?mode=ro", uri=True)
        try:
            return connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            connection.close()

    def backend_ref(self):
        record = json.loads((self.data / "backend.json").read_text())
        backend = record["processes"]["backend"]
        return backend["pid"], backend["start_ticks"], record

    def sigkill_backend(self):
        pid, start, record = self.backend_ref()
        self.assertTrue(kill_exact(pid, start))
        deadline = time.monotonic() + 10
        while alive(pid, start) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(pid, start))
        return record

    def ours(self):
        """Live processes whose command line or cwd is under this test's root (only ours)."""
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
            if self.status() is not None:
                self.cli("shutdown", "--yes", timeout=90)
        except Exception:
            pass
        deadline = time.monotonic() + 10
        while self.ours() and time.monotonic() < deadline:
            for pid, start in self.ours().items():
                kill_exact(pid, start)
            time.sleep(0.2)
        shutil.rmtree(self.root, ignore_errors=True)

    # -- the scenario ----------------------------------------------------------------------------------
    def test_sigkill_restart_reconcile_then_a_changed_boot(self):
        first = self.start()
        manager_session = first["bridge"]["manager"]["session_id"]
        client = self.attached()
        try:
            self.type(client, "host_shell", "nohup sleep 300 >/dev/null 2>&1 &\n")
            self.type(client, "manager_omp", "survive\n")
            dispatched = self.call(client, manager_session, "to_worker", {
                "kind": "work", "message": "cw19 live work", "spec": {"goal": "cw19", "paths": ["notes/"]}})
            self.assertEqual(dispatched["status"], "dispatched", dispatched)
            task_id = dispatched["task_id"]
            snapshot = self.wait_status(lambda s: (s.get("task") or {}).get("run_id"), "the work run")
            self.assertEqual(client.request(ClientType.PAUSE).get("ok"), True)
        finally:
            client.close()
        self.wait_status(lambda s: s["automation"]["paused"], "paused")
        time.sleep(1.0)
        runs_before = self.runs()
        record = self.sigkill_backend()
        old_manager = record["processes"]["manager_omp"]
        time.sleep(1.0)
        self.assertTrue(alive(old_manager["pid"], old_manager["start_ticks"]), "the fake manager survived the hangup")

        # -- restart on the same boot ------------------------------------------------------------
        second = self.start()
        startup = second["startup"]
        self.assertEqual(startup["classification"], "same_boot_crash")
        self.assertFalse(second["boot"]["confirmation_required"])
        self.assertTrue(second["automation"]["paused"], "R4: the user's pause survives the crash")
        states = {item["name"]: item["state"] for item in startup["processes"]}
        self.assertEqual(states["backend"], "ended")
        self.assertEqual(states["manager_omp"], "alive")
        survivors = {item["pid"]: item for item in startup["survivors"]}
        old = survivors[old_manager["pid"]]
        self.assertEqual((old["name"], old["stoppable"], old["identity"]), ("manager_omp", True, "verified"))
        # observed, not assumed (C-AC-16): whether the host shell outlived the hangup decides whether its job is
        # provably the old pane's (stoppable) or only shown
        shell_alive = states["host_shell"] == "alive"
        jobs = [item for item in startup["survivors"] if item["comm"] == "sleep"]
        self.assertEqual(len(jobs), 1, startup["survivors"])
        self.assertEqual((jobs[0]["name"], jobs[0]["stoppable"]), ("session_member", shell_alive))
        if os.environ.get("WB_CW19_REPORT"):  # optional evidence of what survived (observed processes)
            Path(os.environ["WB_CW19_REPORT"]).write_text(json.dumps(
                {"processes": startup["processes"], "survivors": startup["survivors"]}, indent=1))
        self.assertEqual(second["task"]["task_id"], task_id)
        self.assertEqual(second["task"]["held_reason"], "backend_restarted")
        self.assertIn(startup["outbox_lost_count"], (1,), startup)  # the TASK the fake worker never accepted
        self.assertEqual(startup["outbox_lost"][0]["state"], "queued_not_sent")
        new_manager = second["bridge"]["manager"]["session_id"]
        new_worker = second["bridge"]["worker"]["session_id"]
        time.sleep(3.0)
        self.assertEqual(self.runs(), runs_before, "no run was started again")
        self.assertEqual([item for item in self.lines(self.frames) if item["session"] == new_worker
                          and item["frame"].get("kind") == "deliver"], [], "nothing was re-sent")
        self.assertEqual([item for item in self.lines(self.notices) if item["notice"]["type"] == "backend_restarted"],
                         [], "no notice while paused")
        client = self.attached()
        try:
            self.assertTrue(client.request(ClientType.RESUME, reconciled=True).get("ok"))
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not any(
                    item["notice"]["type"] == "backend_restarted" for item in self.lines(self.notices)):
                client.pump(0.2)
            time.sleep(3.0)
            notices = [item for item in self.lines(self.notices) if item["notice"]["type"] == "backend_restarted"]
            self.assertEqual(len(notices), 1, "one notice")
            self.assertEqual((notices[0]["role"], notices[0]["session"]), ("manager", new_manager))
            self.assertIn(old_manager["pid"], [item["pid"] for item in notices[0]["notice"]["survivors"]])
            status = self.call(client, new_manager, "workbench_status", {})
            self.assertEqual(status["backend"]["startup"], "same_boot_crash")
            stopped = self.call(client, new_manager, "stop_survivor",
                                {"survivor_id": old["survivor_id"], "reason": "old manager session, not needed"})
            self.assertEqual(stopped["status"], "stopped", stopped)
            again = self.call(client, new_manager, "stop_survivor",
                              {"survivor_id": old["survivor_id"], "reason": "again"})
            self.assertEqual((again["status"], again["reason"]), ("refused", "not_alive"))
            unknown = self.call(client, new_manager, "stop_survivor", {"survivor_id": "s99", "reason": "x"})
            self.assertEqual((unknown["status"], unknown["reason"]), ("refused", "unknown_survivor"))
        finally:
            client.close()
        self.assertFalse(alive(old_manager["pid"], old_manager["start_ticks"]))
        # the manager left the other survivors running: they are never signalled by Workbench itself
        self.assertTrue(alive(jobs[0]["pid"], jobs[0]["start_ticks"]))
        result = self.cli("shutdown", "--yes", "--json", timeout=90)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)  # C-AC-22: never shown as success
        self.assertIn("previous_survivor", result.stdout)
        shutdown = json.loads(result.stdout.splitlines()[-1])["shutdown"]
        self.assertFalse(shutdown["verified"])
        self.assertIn("previous_backend_survivors_alive", shutdown["problems"])

        # -- a changed boot marker (as after an OS reboot) ------------------------------------------
        boot = json.loads((self.data / "boot.json").read_text())
        boot["recorded_boot_id"] = boot["confirmed_boot_id"] = OTHER_BOOT
        (self.data / "boot.json").write_text(json.dumps(boot))
        backend = json.loads((self.data / "backend.json").read_text())
        backend["boot_id"] = OTHER_BOOT
        (self.data / "backend.json").write_text(json.dumps(backend))
        runs_before = self.runs()
        third = self.start()
        self.assertTrue(third["boot"]["confirmation_required"])
        self.assertEqual(third["startup"]["classification"], "reboot")
        self.assertEqual({item["state"] for item in third["startup"]["processes"]}, {"ended_by_reboot"})
        self.assertEqual(third["startup"]["survivors"], [], "nothing recorded under another boot is probed")
        manager = third["bridge"]["manager"]["session_id"]
        client = self.attached()
        try:
            held = self.call(client, manager, "to_worker", {"kind": "work", "message": "after reboot",
                                                             "spec": {"goal": "g", "paths": ["notes/"]}})
            self.assertEqual((held["status"], held.get("reason")), ("held", "boot_confirmation_required"), held)
        finally:
            client.close()
        self.assertEqual(self.runs(), runs_before)
        status_text = self.cli("status").stdout
        self.assertIn("confirm-boot", status_text)
        refused = self.cli("confirm-boot")  # no tty, no --yes
        self.assertEqual(refused.returncode, 1)
        self.assertIn("환경·실행 조건", refused.stdout)
        confirmed = self.cli("confirm-boot", "--yes")
        self.assertEqual(confirmed.returncode, 0, confirmed.stdout + confirmed.stderr)
        after = self.wait_status(lambda s: not s["boot"]["confirmation_required"], "the confirmed boot")
        self.assertEqual(after["holds"], [])
        client = self.attached()
        try:
            allowed = self.call(client, manager, "to_worker", {"kind": "work", "message": "after confirm",
                                                                "spec": {"goal": "g", "paths": ["notes/"]}})
            self.assertNotEqual(allowed.get("reason"), "boot_confirmation_required", allowed)
        finally:
            client.close()
        self.assertEqual(self.cli("confirm-boot", "--yes").returncode, 1, "nothing pending any more")


if __name__ == "__main__":
    unittest.main()
