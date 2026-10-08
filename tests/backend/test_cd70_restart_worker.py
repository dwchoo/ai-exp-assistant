"""C-D70 (3)/(4)/(6): the manager's restart_worker and workbench_status on a real backend with fake OMPs.

Each fake OMP (a Python script on a real PTY) registers with the real G3 bridge, answers probes with an idle
state and acknowledges Workbench notices (recording them to a file); "ignore-term" makes it ignore SIGTERM. The
isolation check is replaced by a recorder (no RPC process, no provider).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from workbench.backend import flow_recovery, service as service_module
from workbench.backend.flow_tasks import FlowTask
from workbench.backend.launcher import LaunchPlan
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.service import Backend
from workbench.contracts.ui_v1 import Reason
from workbench.contracts.v1 import ActorRole, PaneId
from workbench.terminal.shell_g2.prototype import ShellChoice

FAKE_OMP = r'''#!{python}
import json, os, signal, socket, sys, threading, uuid
argv = sys.argv[1:]
if argv[:2] == ["config", "get"]:
    print("[]")
    sys.exit(0)
role = os.environ["WORKBENCH_G3_ROLE"]
session = str(uuid.uuid4())
generation = int(os.environ["WORKBENCH_G3_GENERATION"])
with open(os.environ["FAKE_PANE_RECORD"], "a") as stream:
    stream.write(json.dumps({{"role": role, "pid": os.getpid(), "session": session}}) + "\n")
sock = socket.socket(socket.AF_UNIX)
sock.connect(os.environ["WORKBENCH_G3_BRIDGE_SOCKET"])
lock = threading.Lock()
def send(frame):
    with lock:
        sock.sendall((json.dumps(frame) + "\n").encode())
send({{"kind": "hello", "protocolVersion": 1, "token": os.environ["WORKBENCH_G3_TOKEN"], "role": role,
      "ompSessionId": session, "generation": generation, "pid": os.getpid()}})
reader = sock.makefile("rb")
reader.readline()
state = {{"kind": "state", "role": role, "sessionId": session, "generation": generation, "idle": True,
         "pending": False, "approvalPending": False, "inFlightToolCount": 0, "editorKnown": True, "editorEmpty": True,
         "paused": False}}
def serve():
    for line in reader:
        frame = json.loads(line)
        if frame.get("kind") == "probe":
            send({{"kind": "api_ack", "requestId": frame["requestId"], "status": "state", "state": state}})
        elif frame.get("kind") == "notice":
            with open(os.environ["FAKE_NOTICES"], "a") as stream:
                stream.write(json.dumps({{"role": role, "session": session, "notice": frame["notice"]}}) + "\n")
            send({{"kind": "api_ack", "requestId": frame["requestId"], "status": "api_accepted"}})
threading.Thread(target=serve, daemon=True).start()
print("fake omp", role, flush=True)
for line in sys.stdin:
    if line.strip() == "exit":
        break
    if line.strip() == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("ignoring TERM", flush=True)
'''

OK = {"state": "ok", "ok": True, "leaks": [], "warnings": [], "error": None}


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] not in "ZX"
    except OSError:
        return False


class RestartWorkerFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cd70-rw-", dir="/tmp")
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        project, home = root / "p", root / "h"
        project.mkdir()
        home.mkdir()
        fake = root / "omp"
        fake.write_text(FAKE_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        self.record, self.notices = root / "panes.jsonl", root / "notices.jsonl"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
               "FAKE_PANE_RECORD": str(self.record), "FAKE_NOTICES": str(self.notices)}
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), str(fake), "omp/18.6.1", "/x/bridge.ts", ())
        self.gate = threading.Event()
        self.gate.set()
        patcher = mock.patch("workbench.backend.service.check_isolation", self.fake_check)
        patcher.start()
        self.addCleanup(patcher.stop)
        layout = DataLayout(root / "d")
        ensure_private_dir(layout.root)
        self.backend = Backend(layout, plan, project_dir=str(project), environment=env)
        self.addCleanup(self.close)
        self.backend._open()
        self.wait(lambda: self.backend.phase == "ready", "both fake OMPs registered")
        self.wait(lambda: self.backend.omp_isolation["state"] == "ok", "start-up isolation result")

    def fake_check(self, command, *, cwd, environment, role, allowed_skills, omp_version, cancel):
        self.gate.wait(10)
        return {"role": role, **OK}

    def close(self):
        self.gate.set()
        pids = [entry["pid"] for entry in self.invocations()]
        self.backend._close()
        for pid in pids:
            self.assertFalse(alive(pid), f"fake OMP {pid} survived close")

    def invocations(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines() if line.strip()]

    def received(self):
        if not self.notices.exists():
            return []
        return [json.loads(line) for line in self.notices.read_text().splitlines() if line.strip()]

    def wait(self, predicate, what, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.backend._tick(0.02)
            if predicate():
                return
        self.fail(f"timed out waiting for {what}; phase={self.backend.phase} reason={self.backend.reason}")

    def call(self, role, tool, args, *, call=None):
        """A bridge tool request on another thread while this thread runs the backend loop; (result, seconds)."""
        peer = self.backend.bridge.peer(role)
        request = {"request_id": str(uuid4()), "tool_call_id": call or f"call-{uuid4()}", "tool": tool,
                   "args": args, "session_id": peer.session_id, "generation": peer.generation}
        box: dict = {}

        def run():
            started = time.monotonic()
            box["result"] = self.backend._tool_request(peer, request)
            box["seconds"] = time.monotonic() - started
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.wait(lambda: "result" in box, f"the {tool} result", timeout=15)
        thread.join(1)
        return box["result"], box["seconds"]

    def open_task(self):
        """An open work Task in the worker's current session (as after its TASK was accepted)."""
        flow = self.backend.flow
        peer = self.backend.bridge.peer(ActorRole.WORKER)
        task = FlowTask(str(uuid4()), "work", status="running", message="collect facts", summary="collect facts",
                        commands=["lscpu"], run_id=str(uuid4()), run_revision=1, task_message_id=str(uuid4()),
                        worker_session=[peer.session_id, peer.generation], spec={"goal": "g", "paths": []})
        with flow._lock:
            flow.tasks[task.task_id] = task
        return task


class RestartWorkerTests(RestartWorkerFixture):
    def test_restart_worker_ends_only_the_worker_omp_and_starts_a_new_session(self):
        worker, manager, shell = (self.backend.panes[PaneId.WORKER_OMP], self.backend.panes[PaneId.MANAGER_OMP],
                                  self.backend.shell)
        old_peer = self.backend.bridge.peer(ActorRole.WORKER)
        task = self.open_task()
        self.backend.watchdog.tick()  # the watchdog has seen the first worker session
        result, seconds = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "no answer after 2 checks"})
        self.assertEqual(result["status"], "restarted", result)
        self.assertLess(seconds, 10.0, "within the bridge's 10 s tool budget")
        self.assertFalse(alive(worker.pid), "the old worker OMP is gone")
        self.assertTrue(alive(manager.pid) and shell.alive(), "manager OMP and host shell untouched")
        self.assertIs(self.backend.panes[PaneId.MANAGER_OMP], manager)
        self.assertIs(self.backend.shell, shell)
        new = self.backend.panes[PaneId.WORKER_OMP]
        self.assertIsNot(new, worker)
        self.assertEqual(new.generation, worker.generation + 1)
        self.assertTrue(alive(new.pid))
        self.assertEqual(result["worker"]["pid"], new.pid)
        self.assertEqual(result["worker"]["pane_generation"], new.generation)
        self.assertTrue(result["worker"]["registered"])
        self.assertNotEqual(result["worker"]["session_id"], old_peer.session_id)
        self.assertEqual(result["task"]["task_id"], task.task_id)
        self.assertEqual(result["task"]["status"], "running", "the open Task is not cancelled")
        self.assertIn("no memory", result["detail"])
        self.assertIn("follow-up to_worker on the same task_id", result["detail"])
        # reason, requester and time in the restart history, the pane's restart info and the backend record
        entry = self.backend.restarts[-1]
        self.assertEqual((entry["pane"], entry["cause"], entry["requester"], entry["reason"]),
                         ("worker_omp", "restart_worker", "manager", "no answer after 2 checks"))
        self.assertIsInstance(entry["at"], float)
        info = self.backend.snapshot()["panes"]["worker_omp"]["restart"]
        self.assertEqual((info["requester"], info["reason"], info["state"]), ("manager", "no answer after 2 checks",
                                                                             "restarted"))
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual(record["restarts"][-1]["reason"], "no answer after 2 checks")
        journal = [json.loads(line) for line in (self.backend.layout.workflow / "handoffs.jsonl")
                   .read_text().splitlines()]
        self.assertEqual([r["reason"] for r in journal if r.get("type") == "restart_worker_request"],
                         ["no answer after 2 checks"])
        self.assertEqual(self.backend.flow.tasks[task.task_id].status, "running")
        # the manager is told once that the worker is a new session (real bridge notice frame)
        self.wait(lambda: any(n["notice"]["type"] == "worker_restarted" for n in self.received()),
                  "the worker_restarted notice")
        notices = [n for n in self.received() if n["notice"]["type"] == "worker_restarted"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["role"], "manager")
        notice = notices[0]["notice"]
        self.assertEqual((notice["task_id"], notice["cause"], notice["requester"], notice["reason"]),
                         (task.task_id, "restart_worker", "manager", "no answer after 2 checks"))
        self.wait(lambda: self.backend.phase == "ready", "ready after the restart")

    def test_a_command_running_in_the_host_shell_keeps_running(self):
        shell = self.backend.shell
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"sleep 6097 &\n", "input"))
        found: list[int] = []

        def find():
            for name in os.listdir("/proc"):
                if name.isdecimal():
                    try:
                        if Path(f"/proc/{name}/cmdline").read_bytes() == b"sleep\x006097\x00":
                            found.append(int(name))
                            return True
                    except OSError:
                        pass
            return False
        self.wait(find, "the host shell's background command")
        pid = found[0]
        fd = os.pidfd_open(pid)

        def end():
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(fd)
        self.addCleanup(end)
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "stuck"})
        self.assertEqual(result["status"], "restarted", result)
        self.assertTrue(alive(pid), "the host terminal command is not touched")
        self.assertTrue(shell.alive())
        self.assertIs(self.backend.shell, shell)

    def test_a_worker_that_ignores_term_is_killed_after_the_grace(self):
        worker = self.backend.panes[PaneId.WORKER_OMP]
        self.assertIsNone(self.backend.admit(PaneId.WORKER_OMP, b"ignore-term\n", "input"))
        self.wait(lambda: b"ignoring TERM" in b"".join(c.data for c in worker.replay()), "the fake to ignore TERM")
        with mock.patch.object(service_module, "RESTART_GRACE", 0.5):
            result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "stuck mid-turn"})
        self.assertEqual(result["status"], "restarted", result)
        self.assertFalse(alive(worker.pid))
        self.assertEqual(worker.returncode, -9, "SIGKILL after the grace")

    def test_refused_while_a_restart_is_in_progress_and_the_lock_is_shared_with_restart_pane(self):
        self.assertTrue(self.backend._restart_lock.acquire(blocking=False))
        try:
            result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "again"})
            self.assertEqual((result["status"], result["reason"]), ("refused", "restart_in_progress"))
        finally:
            self.backend._restart_lock.release()
        worker = self.backend.panes[PaneId.WORKER_OMP]
        self.assertTrue(alive(worker.pid))
        # a second call while the first one is being carried out
        peer = self.backend.bridge.peer(ActorRole.MANAGER)
        first: dict = {}

        def run_first():
            first["result"] = self.backend._tool_request(peer, {
                "request_id": "r1", "tool_call_id": "c1", "tool": "restart_worker", "args": {"reason": "one"},
                "session_id": peer.session_id, "generation": peer.generation})
        thread = threading.Thread(target=run_first, daemon=True)
        thread.start()
        self.wait(lambda: self.backend._restart_jobs, "the first job")
        second = self.backend._tool_request(peer, {
            "request_id": "r2", "tool_call_id": "c2", "tool": "restart_worker", "args": {"reason": "two"},
            "session_id": peer.session_id, "generation": peer.generation})
        self.assertEqual((second["status"], second["reason"]), ("refused", "restart_in_progress"))
        with self.assertRaises(Exception) as caught:
            self.backend.restart_pane(PaneId.WORKER_OMP)
        self.assertEqual(caught.exception.reason, Reason.RESTART_IN_PROGRESS)
        self.wait(lambda: "result" in first, "the first restart")
        self.assertEqual(first["result"]["status"], "restarted")
        self.assertEqual([e["reason"] for e in self.backend.restarts], ["one"])

    def test_the_same_tool_call_is_carried_out_once(self):
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "once"}, call="same")
        again, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "once"}, call="same")
        self.assertEqual(again, result)
        self.assertEqual(len(self.backend.restarts), 1)

    def test_the_worker_cannot_restart_and_bad_reasons_are_refused(self):
        worker = self.backend.panes[PaneId.WORKER_OMP]
        result, _ = self.call(ActorRole.WORKER, "restart_worker", {"reason": "self"})
        self.assertEqual((result["status"], result["reason"]), ("rejected", "tool_not_allowed_for_role"))
        result, _ = self.call(ActorRole.WORKER, "workbench_status", {})
        self.assertEqual((result["status"], result["reason"]), ("rejected", "tool_not_allowed_for_role"))
        for args in ({}, {"reason": " "}, {"reason": "x" * 501}, {"reason": "ok", "extra": 1}):
            result, _ = self.call(ActorRole.MANAGER, "restart_worker", args)
            self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), args)
        self.assertIs(self.backend.panes[PaneId.WORKER_OMP], worker)
        self.assertTrue(alive(worker.pid))
        self.assertEqual(self.backend.restarts, [])

    def test_refused_while_shutting_down(self):
        self.backend._shutdown_confirmed = True
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "late"})
        self.assertEqual((result["status"], result["reason"]), ("refused", "backend_shutdown"))
        self.backend._shutdown_confirmed = False

    def test_an_exited_pane_restart_from_the_ui_is_unchanged_and_records_the_user(self):
        self.assertIsNone(self.backend.admit(PaneId.WORKER_OMP, b"exit\n", "input"))
        worker = self.backend.panes[PaneId.WORKER_OMP]
        self.wait(lambda: worker.info()["alive"] is False, "the worker to exit")
        result = self.backend.restart_pane(PaneId.WORKER_OMP)
        self.assertTrue(result["restarted"])
        self.assertEqual((self.backend.restarts[-1]["requester"], self.backend.restarts[-1]["cause"]),
                         ("user", "user_restart"))
        with self.assertRaises(Exception) as caught:
            self.backend.restart_pane(PaneId.MANAGER_OMP)  # a live pane is still refused for the user
        self.assertEqual(caught.exception.reason, Reason.PANE_ALIVE)


class WorkbenchStatusTests(RestartWorkerFixture):
    def test_status_shape_and_timing(self):
        task = self.open_task()
        result, seconds = self.call(ActorRole.MANAGER, "workbench_status", {"task_id": None})
        self.assertLess(seconds, 3.0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(set(result), {"status", "backend", "task", "worker", "terminal", "reports",
                                       "reports_omitted", "watchdog"})  # CW-19: backend (restart, survivors)
        view = result["task"]
        self.assertEqual((view["task_id"], view["status"], view["message"], view["commands"], view["analysis"]),
                         (task.task_id, "running", "collect facts", ["lscpu"], "summary"))
        self.assertEqual(view["commands_run"], [])
        worker = result["worker"]
        self.assertEqual((worker["state"], worker["task_id"], worker["omp"], worker["connected"]),
                         ("busy", task.task_id, "idle", True))
        self.assertEqual(worker["session_id"], self.backend.bridge.peer(ActorRole.WORKER).session_id)
        self.assertEqual(worker["restarts"], [])
        self.assertEqual(result["terminal"]["running"], False)
        self.assertIsNone(result["terminal"]["last"])
        self.assertEqual(result["reports"], [])
        again, _ = self.call(ActorRole.MANAGER, "workbench_status", {"task_id": task.task_id})
        self.assertEqual(again["task"]["task_id"], task.task_id)
        unknown, _ = self.call(ActorRole.MANAGER, "workbench_status", {"task_id": str(uuid4())})
        self.assertEqual((unknown["status"], unknown["reason"]), ("rejected", "unknown_task"))
        bad, _ = self.call(ActorRole.MANAGER, "workbench_status", {"task_id": "nope"})
        self.assertEqual(bad["reason"], "invalid_arguments")

    def test_status_lists_restarts_with_reasons(self):
        self.call(ActorRole.MANAGER, "restart_worker", {"reason": "stuck"})
        self.wait(lambda: self.backend.bridge.peer(ActorRole.WORKER).pid == self.backend.panes[PaneId.WORKER_OMP].pid,
                  "the new worker")
        result, _ = self.call(ActorRole.MANAGER, "workbench_status", {})
        restarts = result["worker"]["restarts"]
        self.assertEqual(len(restarts), 1)
        self.assertEqual((restarts[0]["cause"], restarts[0]["requester"], restarts[0]["reason"]),
                         ("restart_worker", "manager", "stuck"))
        self.assertEqual(restarts[0]["pid"], self.backend.panes[PaneId.WORKER_OMP].pid)

    def test_the_snapshot_carries_the_recovery_view(self):
        snapshot = self.backend.snapshot()
        self.assertIn("recovery", snapshot)
        self.assertEqual(snapshot["recovery"]["report_wait"], None)


class WiringTests(RestartWorkerFixture):
    def test_a_submitted_follow_up_re_arms_the_watchdog_and_old_session_completions_are_routed(self):
        task = self.open_task()
        dog = self.backend.watchdog
        dog.tick()
        with dog._lock:
            dog._watch.checks, dog._watch.stalled = 2, True
        self.backend.flow.follow_up_submitted(task.task_id)  # p27-cd70-02 Q2
        self.assertEqual((dog._watch.checks, dog._watch.stalled), (0, False))
        peer = self.backend.bridge.peer(ActorRole.WORKER)
        started = {"session_id": peer.session_id, "generation": peer.generation, "task_id": task.task_id}
        self.assertEqual(self.backend._done_target(started), "worker")
        self.assertEqual(self.backend._done_target({**started, "session_id": str(uuid4())}), "worker",
                         "the open Task is in the current worker session")
        self.call(ActorRole.MANAGER, "restart_worker", {"reason": "stuck"})
        self.wait(lambda: self.backend.bridge.peer(ActorRole.WORKER).pid == self.backend.panes[PaneId.WORKER_OMP].pid,
                  "the new worker")
        self.assertEqual(self.backend._done_target(started), "manager", "p27-cd70-02 Q3: not re-delivered yet")


class ManagerRecoveryTests(RestartWorkerFixture):
    def test_a_new_manager_session_gets_one_recovery_notice(self):
        self.backend.watchdog.tick()  # sessions seen
        self.assertIsNone(self.backend.admit(PaneId.MANAGER_OMP, b"exit\n", "input"))
        manager = self.backend.panes[PaneId.MANAGER_OMP]
        self.wait(lambda: manager.info()["alive"] is False, "the manager to exit")
        self.backend.restart_pane(PaneId.MANAGER_OMP)
        self.wait(lambda: any(n["notice"]["type"] == "manager_recovery" for n in self.received()),
                  "the manager_recovery notice")
        notices = [n for n in self.received() if n["notice"]["type"] == "manager_recovery"]
        self.assertEqual(len(notices), 1)
        notice = notices[0]["notice"]
        self.assertEqual((notice["reports_resent"], notice["reports_unknown"]), (0, 0))
        self.assertEqual(notices[0]["session"], self.backend.bridge.peer(ActorRole.MANAGER).session_id)
        self.assertIn("workbench_status", notice["instruction"])
        self.assertEqual([n for n in self.received() if n["role"] == "worker"], [])


if __name__ == "__main__":
    unittest.main()
