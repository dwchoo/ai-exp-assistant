"""C-D70 (2)-(6) independent end-to-end checks on a real backend (p27-cd70-test-01).

Expectations come from DECISIONS.md C-D70, the assignment p27-cd70-01 (2)-(6) and the root adjudication
p27-cd70-02 Q2/Q3, not from the implementation's tests.

A fake OMP (a Python script on a real PTY started by the real backend) registers with the real G3 bridge with its
role token and behaves per a control file ``<ctl>/<role>.json`` read at every frame:

* ``editorEmpty`` (default true): its composer state in probe answers;
* ``deliver``: ``accept`` (default; api_accepted + delivery_omp_processed) or ``unknown`` (unknown_no_replay);
* ``probe``: ``answer`` (default) or ``hang`` (never answers a probe).

It records every frame it gets (``frames.jsonl``: notices, deliveries, tool results) and understands stdin lines:
``tool <json>`` (send a bridge ``tool_request`` as this OMP), ``child`` (fork a ``sleep`` in its own session),
``ignore-term``, ``exit``. Started with the single argument ``decoy`` it only sleeps (a process with the same
executable path that a pattern kill would hit).

Everything runs under /tmp; every process this test starts directly is killed by exact identity (pidfd + start
time) in cleanup; survivors of the backend's own children are failures. No real OMP, no provider.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from workbench.backend import service as service_module
from workbench.backend.launcher import LaunchPlan
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.service import Backend
from workbench.backend.ui_server import Held
from workbench.contracts.ui_v1 import Reason
from workbench.contracts.v1 import ActorRole, PaneId
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.ui.product.model import ProductModel

REPORT_WAIT_KO = "worker 보고 대기 중"  # C-D70 (2): the Korean status line the assignment names

FAKE_OMP = r'''#!{python}
import json, os, signal, socket, sys, threading, time, uuid
argv = sys.argv[1:]
if argv[:2] == ["config", "get"]:
    print("[]")
    sys.exit(0)
if argv[:1] == ["decoy"]:
    time.sleep(600)
    sys.exit(0)
ctl = os.environ["CD70_CTL"]
role = os.environ["WORKBENCH_G3_ROLE"]
session = str(uuid.uuid4())
generation = int(os.environ["WORKBENCH_G3_GENERATION"])
def ticks(pid):
    return open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()[19]
def log(name, item):
    with open(os.path.join(ctl, name), "a") as stream:
        stream.write(json.dumps(item) + "\n")
def config():
    try:
        with open(os.path.join(ctl, role + ".json")) as stream:
            return json.load(stream)
    except Exception:
        return {{}}
log("panes.jsonl", {{"role": role, "pid": os.getpid(), "ticks": ticks(os.getpid()), "sid": os.getsid(0),
                    "session": session, "generation": generation}})
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
def state():
    return {{"kind": "state", "role": role, "sessionId": session, "generation": generation, "idle": True,
            "pending": False, "approvalPending": False, "inFlightToolCount": 0, "editorKnown": True,
            "editorEmpty": config().get("editorEmpty", True), "paused": False}}
def serve():
    for line in reader:
        frame = json.loads(line)
        kind = frame.get("kind")
        cfg = config()
        if kind == "probe":
            if cfg.get("probe") == "hang":
                continue
            send({{"kind": "api_ack", "requestId": frame["requestId"], "status": "state", "state": state()}})
        elif kind == "notice":
            log("frames.jsonl", {{"role": role, "session": session, "kind": "notice", "notice": frame["notice"]}})
            send({{"kind": "api_ack", "requestId": frame["requestId"], "status": "api_accepted"}})
        elif kind == "deliver":
            envelope = frame["envelope"]
            envelope = json.loads(envelope) if isinstance(envelope, str) else envelope
            log("frames.jsonl", {{"role": role, "session": session, "kind": "deliver", "envelope": envelope}})
            if cfg.get("deliver") == "unknown":
                send({{"kind": "api_ack", "requestId": frame["requestId"], "status": "unknown_no_replay"}})
                continue
            send({{"kind": "api_ack", "requestId": frame["requestId"], "status": "api_accepted"}})
            send({{"kind": "omp_event", "name": "delivery_omp_processed", "sessionId": session,
                  "generation": generation, "messageId": envelope.get("messageId"),
                  "deliveryAttemptId": envelope.get("deliveryAttemptId"), "taskId": envelope.get("taskId"),
                  "revisionId": envelope.get("revisionId"), "runId": envelope.get("runId"),
                  "providerRequestMatched": True, "providerResponseObserved": True, "agentEndObserved": True}})
        elif kind == "tool_result":
            log("frames.jsonl", {{"role": role, "session": session, "kind": "tool_result",
                                 "toolCallId": frame.get("toolCallId"), "result": frame.get("result")}})
threading.Thread(target=serve, daemon=True).start()
print("fake omp", role, flush=True)
for line in sys.stdin:
    command = line.strip()
    if command == "exit":
        break
    if command == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("ignoring TERM", flush=True)
    elif command == "child":
        pid = os.fork()
        if pid == 0:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            os.execv("/bin/sleep", ["sleep", "300"])
        log("helpers.jsonl", {{"role": role, "pid": pid, "ticks": ticks(pid)}})
        print("child=%d" % pid, flush=True)
    elif command.startswith("tool "):
        spec = json.loads(command[5:])
        send({{"kind": "tool_request", "requestId": str(uuid.uuid4()), "toolCallId": spec["call"],
              "tool": spec["tool"], "args": spec["args"], "sessionId": session, "generation": generation}})
'''

OK = {"state": "ok", "ok": True, "leaks": [], "warnings": [], "error": None}
WORK_COMMAND = "sleep 2.7; printf 'first-%s\\n' done-OUTPUT-5c1e"  # the output text is not in the command text
OUTPUT_MARK = "first-done-OUTPUT-5c1e"
SECOND_COMMAND = "echo second-ok"


def start_ticks(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if fields[0] in "ZX" else fields[19]


def alive(pid: int) -> bool:
    return start_ticks(pid) is not None


def kill_exact(pid: int, ticks: str | None) -> None:
    if ticks is None:
        return
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if start_ticks(pid) == ticks:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


def find_in_session(sid: int, needle: bytes) -> list[int]:
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            if os.getsid(int(name)) != sid:
                continue
            if needle in Path(f"/proc/{name}/cmdline").read_bytes():
                found.append(int(name))
        except OSError:
            pass
    return found


def first_found(found: list[int]):
    """A wait predicate that keeps what it found. Re-reading /proc after the wait can miss the process: the
    worker command's display wrapper execs into ``bash -c <command>`` and its cmdline reads empty during that
    exec (p27-flaky-01 (f)/(g): IndexError on ``find_in_session(...)[0]``)."""
    def keep(pids: list[int]) -> bool:
        found[:] = pids
        return bool(pids)
    return keep


def find_key(value, key):
    """The first value of ``key`` anywhere in a JSON-like structure."""
    if isinstance(value, dict):
        if key in value:
            return value[key]
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            found = find_key(item, key)
            if found is not None:
                return found
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return find_key(json.loads(value), key)
        except ValueError:
            return None
    return None


class _Sender:
    def send(self, kind, payload=b"", **fields):
        return "r1"


def status_line(snapshot) -> str:
    """The product UI's status lines for a real backend snapshot."""
    model = ProductModel(_Sender(), 30, 400)
    model.apply_snapshot(json.loads(json.dumps(snapshot)))
    return " ".join(model.status_lines())


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="p27cd70-e2e-", dir="/tmp")
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        self.project, home, self.ctl = root / "p", root / "h", root / "c"
        for directory in (self.project, home, self.ctl):
            directory.mkdir()
        self.fake = root / "omp"
        self.fake.write_text(FAKE_OMP.format(python=sys.executable))
        self.fake.chmod(0o700)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "CD70_CTL": str(self.ctl),
               "LANG": "C.UTF-8"}
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), str(self.fake), "omp/18.7.0", "/x/bridge.ts", ())
        self.gate = threading.Event()
        self.gate.set()
        patcher = mock.patch("workbench.backend.service.check_isolation", self.fake_check)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owned: list[tuple[int, str | None]] = []  # processes this test started directly
        layout = DataLayout(root / "d")
        ensure_private_dir(layout.root)
        self.backend = Backend(layout, plan, project_dir=str(self.project), environment=env)
        self.addCleanup(self.close)
        self.backend._open()
        self.wait(lambda: self.backend.phase == "ready", "both fake OMPs registered")
        self.wait(lambda: self.backend.omp_isolation["state"] == "ok", "start-up isolation result")
        self.wait(lambda: self.backend.shell.state.get("input_owner") == "user"
                  and self.backend.terminal is not None, "the host shell prompt")

    def fake_check(self, command, *, cwd, environment, role, allowed_skills, omp_version, cancel):
        self.gate.wait(15)
        return {"role": role, **OK}

    def close(self):
        self.gate.set()
        panes = [(e["pid"], e["ticks"]) for e in self.read("panes.jsonl")]
        helpers = self.read("helpers.jsonl")
        try:
            self.backend._close()
        finally:
            for pid, ticks in self.owned:
                kill_exact(pid, ticks)
            leftover = [h for h in helpers if start_ticks(h["pid"]) == h["ticks"]]
            for entry in leftover:
                kill_exact(entry["pid"], entry["ticks"])
        for pid, ticks in panes:
            self.assertNotEqual(start_ticks(pid), ticks, f"fake OMP {pid} survived the backend close")
        self.assertEqual(leftover, [], "an in-session helper survived the backend close")

    # -- helpers ----------------------------------------------------------------------
    def read(self, name):
        path = self.ctl / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def frames(self, role=None, kind=None, session=None):
        return [f for f in self.read("frames.jsonl") if (role is None or f["role"] == role)
                and (kind is None or f["kind"] == kind) and (session is None or f["session"] == session)]

    def notices(self, role, notice_type=None, session=None):
        return [f["notice"] for f in self.frames(role, "notice", session)
                if notice_type is None or f["notice"].get("type") == notice_type]

    def configure(self, role, **values):
        (self.ctl / f"{role}.json").write_text(json.dumps(values))

    def wait(self, predicate, what, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.backend._tick(0.02)
            if predicate():
                return
        self.fail(f"timed out waiting for {what}; phase={self.backend.phase} reason={self.backend.reason}")

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.backend._tick(0.02)

    def type_line(self, pane_id, line):
        self.assertIsNone(self.backend.admit(pane_id, line.encode() + b"\n", "input"))

    def peer_pid(self, role):
        """The registered bridge peer's pid, or None while that OMP is not connected."""
        try:
            return self.backend.bridge.peer(ActorRole(role)).pid
        except Exception:
            return None

    def session(self, role):
        return self.backend.bridge.peer(ActorRole(role)).session_id

    def tool(self, role, tool, args, *, wait=True, timeout=20.0, call=None):
        """A tool call as the fake OMP of ``role`` sends it on the real bridge; returns the tool result."""
        call = call or f"{tool}-{uuid4()}"
        self.type_line(PaneId.MANAGER_OMP if role == "manager" else PaneId.WORKER_OMP,
                       "tool " + json.dumps({"call": call, "tool": tool, "args": args}))
        if not wait:
            return call
        return self.result(role, call, timeout=timeout)

    def result(self, role, call, timeout=20.0):
        box = {}

        def got():
            for frame in self.frames(role, "tool_result"):
                if frame["toolCallId"] == call:
                    box["result"] = frame["result"]
                    return True
            return False
        self.wait(got, f"{role} tool result {call}", timeout=timeout)
        return box["result"]

    def open_work_task(self, commands=None):
        args = {"kind": "work", "message": "collect the hardware facts FULL-MESSAGE-91b2",
                "spec": {"goal": "hardware facts", "paths": []}, "analysis": "detailed",
                "commands": commands or [WORK_COMMAND, SECOND_COMMAND]}
        result = self.tool("manager", "to_worker", args)
        self.assertEqual(result["status"], "dispatched", result)
        task_id = result["task_id"]
        worker = self.session("worker")
        self.wait(lambda: any(find_key(f["envelope"], "task_id") == task_id
                              for f in self.frames("worker", "deliver", worker)), "the TASK at the worker")
        self.wait(lambda: (self.backend.flow.watch_view() or {}).get("worker_session") is not None,
                  "the Task's worker session")
        return task_id

    def restart_worker(self, reason="no answer after 2 status checks", **kwargs):
        return self.tool("manager", "restart_worker", {"reason": reason}, **kwargs)

    def decoy(self):
        process = subprocess.Popen([str(self.fake), "decoy"], start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while start_ticks(process.pid) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        ticks = start_ticks(process.pid)

        def end():
            kill_exact(process.pid, ticks)
            process.wait(5)
        self.addCleanup(end)
        return process


class RestartWorkerProcessTests(_Fixture):
    def test_only_the_worker_omp_session_is_ended_shell_manager_and_lookalikes_are_untouched(self):
        decoy = self.decoy()
        worker, manager, shell = (self.backend.panes[PaneId.WORKER_OMP], self.backend.panes[PaneId.MANAGER_OMP],
                                  self.backend.shell)
        self.type_line(PaneId.WORKER_OMP, "child")
        self.type_line(PaneId.MANAGER_OMP, "child")
        self.wait(lambda: len(self.read("helpers.jsonl")) == 2, "helpers in both OMP sessions")
        helpers = {h["role"]: h for h in self.read("helpers.jsonl")}
        # a host command running in the host shell
        self.type_line(PaneId.HOST_SHELL, "sleep 7031 &")
        found = []
        keep = first_found(found)
        self.wait(lambda: keep(find_in_session(shell.pid, b"7031")), "the host shell's background command")
        host_command = found[0]
        host_ticks = start_ticks(host_command)
        self.assertIsNotNone(host_ticks, "the host shell's background command is alive")
        self.owned.append((host_command, host_ticks))
        old_worker_session = self.session("worker")
        result = self.restart_worker()
        self.assertEqual(result.get("status"), "restarted", result)
        self.assertFalse(alive(worker.pid), "the worker OMP is ended")
        self.assertNotEqual(start_ticks(helpers["worker"]["pid"]), helpers["worker"]["ticks"],
                            "a process in the worker OMP's own session is ended with it")
        self.assertEqual(start_ticks(helpers["manager"]["pid"]), helpers["manager"]["ticks"],
                         "the manager OMP's own child is untouched")
        self.assertTrue(alive(manager.pid), "the manager OMP is untouched")
        self.assertIs(self.backend.panes[PaneId.MANAGER_OMP], manager)
        self.assertEqual(start_ticks(host_command), host_ticks, "the host command keeps running")
        self.assertTrue(shell.alive() and self.backend.shell is shell, "the host shell is untouched")
        self.assertIsNone(decoy.poll(), "a process with the same executable is never pattern-killed")
        new = self.backend.panes[PaneId.WORKER_OMP]
        self.assertEqual(new.generation, worker.generation + 1)
        self.assertTrue(alive(new.pid))
        line = status_line(self.backend.snapshot())
        self.assertIn("no answer after 2 status checks", line, "the UI restart notice shows the reason")
        self.assertIn("manager", line, "and the requester")
        entry = self.backend.restarts[-1]
        self.assertEqual((entry.get("requester"), entry.get("reason")), ("manager", "no answer after 2 status checks"))
        self.assertIsInstance(entry.get("at"), float)
        record = json.loads(Path(self.backend.layout.record).read_text())
        self.assertIn("no answer after 2 status checks", json.dumps(record), "the reason is in the backend record")
        self.wait(lambda: self.peer_pid("worker") == new.pid, "the new worker registered")
        self.assertNotEqual(self.session("worker"), old_worker_session)
        workers = [e for e in self.read("panes.jsonl") if e["role"] == "worker"]
        self.assertEqual(len(workers), 2, "exactly one new worker OMP")

    def test_a_worker_ignoring_sigterm_is_killed_after_the_grace_within_the_tool_budget(self):
        worker = self.backend.panes[PaneId.WORKER_OMP]
        self.type_line(PaneId.WORKER_OMP, "ignore-term")
        self.wait(lambda: b"ignoring TERM" in b"".join(c.data for c in worker.replay()), "TERM ignored")
        started = time.monotonic()
        result = self.restart_worker()
        elapsed = time.monotonic() - started
        self.assertEqual(result.get("status"), "restarted", result)
        self.assertLess(elapsed, 10.0, "inside the bridge's 10 s tool budget")
        self.assertGreaterEqual(elapsed, service_module.RESTART_GRACE - 0.5, "a polite grace before the KILL")
        self.assertFalse(alive(worker.pid))
        self.assertEqual(worker.returncode, -signal.SIGKILL)

    def test_the_worker_cannot_restart_itself_and_bad_reasons_restart_nothing(self):
        worker = self.backend.panes[PaneId.WORKER_OMP]
        result = self.tool("worker", "restart_worker", {"reason": "I am stuck"})
        self.assertEqual(result.get("status"), "rejected", result)
        self.assertEqual(result.get("reason"), "tool_not_allowed_for_role")
        result = self.tool("worker", "workbench_status", {})
        self.assertEqual(result.get("reason"), "tool_not_allowed_for_role", result)
        for args in ({}, {"reason": ""}, {"reason": "   \n"}, {"reason": None}, {"reason": 5},
                     {"reason": "x" * 600}):
            with self.subTest(args=args):
                result = self.tool("manager", "restart_worker", args)
                self.assertNotEqual(result.get("status"), "restarted", result)
        self.pump(0.3)
        self.assertIs(self.backend.panes[PaneId.WORKER_OMP], worker)
        self.assertTrue(alive(worker.pid))
        self.assertEqual(len([e for e in self.read("panes.jsonl") if e["role"] == "worker"]), 1)

    def test_three_concurrent_restart_calls_start_exactly_one_new_worker(self):
        worker = self.backend.panes[PaneId.WORKER_OMP]
        self.type_line(PaneId.WORKER_OMP, "ignore-term")  # keeps the first restart in its grace
        self.wait(lambda: b"ignoring TERM" in b"".join(c.data for c in worker.replay()), "TERM ignored")
        calls = [self.restart_worker(f"reason {n}", wait=False) for n in range(3)]
        results = [self.result("manager", call, timeout=25) for call in calls]
        statuses = sorted(r.get("status") for r in results)
        self.assertEqual(statuses.count("restarted"), 1, results)
        refused = [r for r in results if r.get("status") != "restarted"]
        self.assertTrue(all(r.get("reason") == "restart_in_progress" for r in refused), refused)
        self.assertEqual(len([e for e in self.read("panes.jsonl") if e["role"] == "worker"]), 2)
        self.assertEqual(len([e for e in self.backend.restarts if e.get("pane") == "worker_omp"]), 1)

    def test_a_user_restart_during_restart_worker_is_refused_and_vice_versa(self):
        worker = self.backend.panes[PaneId.WORKER_OMP]
        self.type_line(PaneId.WORKER_OMP, "ignore-term")
        self.wait(lambda: b"ignoring TERM" in b"".join(c.data for c in worker.replay()), "TERM ignored")
        call = self.restart_worker("manager wants it", wait=False)
        self.wait(lambda: self.backend._restart_jobs, "the restart_worker job")
        with self.assertRaises(Held) as caught:
            self.backend.restart_pane(PaneId.WORKER_OMP)
        self.assertEqual(caught.exception.reason, Reason.RESTART_IN_PROGRESS)
        self.assertEqual(self.result("manager", call, timeout=25).get("status"), "restarted")
        # the user restarts the (now exited) manager pane while its isolation re-check is held
        self.wait(lambda: "rechecking" not in self.backend.omp_isolation, "the worker re-check")
        self.gate.clear()
        self.type_line(PaneId.WORKER_OMP, "exit")
        new_worker = self.backend.panes[PaneId.WORKER_OMP]
        self.wait(lambda: new_worker.info()["alive"] is False, "the new worker exits")
        self.backend.restart_pane(PaneId.WORKER_OMP)
        self.wait(lambda: self.peer_pid("worker") == self.backend.panes[PaneId.WORKER_OMP].pid,
                  "the user-restarted worker registered")
        result = self.restart_worker("while the user restart is re-checked")
        self.assertEqual((result.get("status"), result.get("reason")), ("refused", "restart_in_progress"), result)
        self.gate.set()
        self.wait(lambda: "rechecking" not in self.backend.omp_isolation, "the re-check ends")
        self.assertEqual(self.restart_worker("now").get("status"), "restarted")
        causes = [(e.get("requester"), e.get("reason")) for e in self.backend.restarts if e["pane"] == "worker_omp"]
        self.assertEqual(causes, [("manager", "manager wants it"), ("user", None), ("manager", "now")])

    def test_a_restart_cut_short_by_the_backend_close_starts_no_new_worker(self):
        worker = self.backend.panes[PaneId.WORKER_OMP]
        self.type_line(PaneId.WORKER_OMP, "ignore-term")
        self.wait(lambda: b"ignoring TERM" in b"".join(c.data for c in worker.replay()), "TERM ignored")
        box = {}
        peer = self.backend.bridge.peer(ActorRole.MANAGER)

        def call():
            box["result"] = self.backend._tool_request(peer, {
                "request_id": "r-close", "tool_call_id": "c-close", "tool": "restart_worker",
                "args": {"reason": "racing the close"}, "session_id": peer.session_id,
                "generation": peer.generation})
        with mock.patch.object(service_module, "RESTART_GRACE", 6.0):
            thread = threading.Thread(target=call, daemon=True)
            thread.start()
            self.wait(lambda: self.backend._restart_jobs and self.backend._restart_jobs[0].phase == "terminating",
                      "the job in its grace")
            self.backend._shutdown_confirmed = True
            self.pump(0.2)
            thread.join(10)
        self.assertFalse(thread.is_alive(), "the tool call ends")
        self.assertNotEqual(box["result"].get("status"), "restarted", box["result"])
        self.assertEqual(box["result"].get("reason"), "backend_shutdown", box["result"])
        self.assertIs(self.backend.panes[PaneId.WORKER_OMP], worker, "no new worker after the shutdown began")
        self.assertEqual(len([e for e in self.read("panes.jsonl") if e["role"] == "worker"]), 1)
        self.assertTrue(self.backend._restart_lock.acquire(blocking=False), "the restart lock is released")
        self.backend._restart_lock.release()


class RecoveryFlowTests(_Fixture):
    def test_restart_mid_command_routes_the_old_completion_to_the_manager_then_resends_the_full_task(self):
        task_id = self.open_work_task()
        old_worker = self.session("worker")
        self.backend.watchdog.tick()
        call = self.tool("worker", "terminal", {"command": WORK_COMMAND}, wait=False)
        self.wait(lambda: self.backend.terminal.watch_state()["running"] is True, "the worker's command runs")
        shell = self.backend.shell
        found = []
        keep = first_found(found)
        self.wait(lambda: keep(find_in_session(shell.pid, b"2.7")), "the host command process")
        host_command = found[0]
        host_ticks = start_ticks(host_command)
        self.assertIsNotNone(host_ticks, "the host command process is alive")
        result = self.restart_worker("stuck mid-turn")
        self.assertEqual(result.get("status"), "restarted", result)
        self.assertEqual(start_ticks(host_command), host_ticks, "the host command keeps running after the restart")
        self.assertEqual((result.get("task") or {}).get("task_id"), task_id)
        self.assertNotIn((result.get("task") or {}).get("status"), ("closed", "cancelled"), "the Task stays open")
        self.assertRegex(json.dumps(result), r"(?i)no memory", "the manager learns the new worker has no memory")
        new_worker = self.session("worker")
        self.assertNotEqual(new_worker, old_worker)
        self.assertEqual(self.frames("worker", "tool_result", old_worker), [],
                         "the old session never got its terminal result")
        # the old session's command ends: its completion goes to the manager (Q3), once, without output
        self.wait(lambda: self.notices("manager", "worker_terminal_done"), "worker_terminal_done", timeout=20)
        self.pump(2.5)
        done = self.notices("manager", "worker_terminal_done")
        self.assertEqual(len(done), 1, done)
        self.assertNotIn(OUTPUT_MARK, json.dumps(done[0]), "no command output in the manager notice")
        self.assertEqual(done[0].get("exit_code"), 0)
        self.assertEqual(done[0].get("task_id"), task_id)
        self.assertTrue(done[0].get("log_path"))
        self.assertEqual(self.notices("worker", session=new_worker), [], "nothing for the new worker yet")
        restarted = self.notices("manager", "worker_restarted")
        self.assertEqual(len(restarted), 1, restarted)
        self.assertEqual((restarted[0].get("task_id"), restarted[0].get("cause"), restarted[0].get("reason")),
                         (task_id, "restart_worker", "stuck mid-turn"))
        self.assertEqual(self.frames("worker", "deliver", new_worker), [], "no automatic re-send without the manager")
        # the manager continues: the new session gets the full Task, the commands already run and the follow-up
        follow = self.tool("manager", "to_worker", {"kind": "work", "message": "continue FOLLOW-UP-77d0",
                                                     "task_id": task_id})
        self.assertEqual(follow.get("status"), "queued", follow)
        self.wait(lambda: self.frames("worker", "deliver", new_worker), "the re-sent Task")
        envelope = json.dumps(self.frames("worker", "deliver", new_worker)[0]["envelope"])
        for text in ("FULL-MESSAGE-91b2", "FOLLOW-UP-77d0", "hardware facts", "detailed",
                     json.dumps(WORK_COMMAND)[1:-1], json.dumps(SECOND_COMMAND)[1:-1]):
            self.assertIn(text, envelope, f"{text!r} is in the re-sent Task")
        already = find_key(self.frames("worker", "deliver", new_worker)[0]["envelope"], "commands_already_run")
        self.assertIsInstance(already, list, envelope[:2000])
        self.assertEqual(len(already), 1, already)
        self.assertIn("2.7", json.dumps(already[0]))
        self.assertEqual(already[0].get("exit_code"), 0)
        self.assertTrue(already[0].get("log_path"))
        self.assertIn("duration_seconds", already[0])
        self.assertEqual(len(self.frames("worker", "deliver", new_worker)), 1, "only the re-sent Task")
        # the Task's commands are still enforced for the new session
        refused = self.tool("worker", "terminal", {"command": "echo not-listed"})
        self.assertNotEqual(refused.get("status"), "exited", refused)
        self.assertIn("not_in_task_commands", json.dumps(refused))
        ran = self.tool("worker", "terminal", {"command": SECOND_COMMAND}, timeout=30)
        self.assertEqual((ran.get("status"), ran.get("exit_code")), ("exited", 0), ran)
        self.pump(3)
        self.assertEqual(len(self.notices("manager", "worker_terminal_done")), 1, "never duplicated")
        self.assertEqual([n for n in self.notices("worker", "terminal_done")
                          if n.get("command_id") == done[0].get("command_id")], [],
                         "the old command's completion never also goes to a worker")
        self.assertIsNotNone(call)

    def test_a_follow_up_to_the_same_session_is_not_a_resend(self):
        task_id = self.open_work_task()
        worker = self.session("worker")
        follow = self.tool("manager", "to_worker", {"kind": "work", "message": "plain follow-up", "task_id": task_id})
        self.assertEqual(follow.get("status"), "queued", follow)
        self.wait(lambda: len(self.frames("worker", "deliver", worker)) == 2, "the follow-up")
        envelope = self.frames("worker", "deliver", worker)[1]["envelope"]
        self.assertIsNone(find_key(envelope, "commands_already_run"), "same session: no re-send")
        self.assertNotIn("FULL-MESSAGE-91b2", json.dumps(envelope))

    def test_a_user_restart_of_an_exited_worker_tells_the_manager_once(self):
        task_id = self.open_work_task()
        self.backend.watchdog.tick()
        worker = self.backend.panes[PaneId.WORKER_OMP]
        self.type_line(PaneId.WORKER_OMP, "exit")
        self.wait(lambda: worker.info()["alive"] is False, "the worker exits")
        self.backend.restart_pane(PaneId.WORKER_OMP)
        self.wait(lambda: self.notices("manager", "worker_restarted"), "worker_restarted")
        self.pump(3)
        notices = self.notices("manager", "worker_restarted")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].get("task_id"), task_id)
        self.assertEqual(notices[0].get("cause"), "user_restart")


class ManagerRecoveryTests(_Fixture):
    def restart_manager(self):
        manager = self.backend.panes[PaneId.MANAGER_OMP]
        self.type_line(PaneId.MANAGER_OMP, "exit")
        self.wait(lambda: manager.info()["alive"] is False, "the manager exits")
        self.backend.restart_pane(PaneId.MANAGER_OMP)
        self.wait(lambda: self.peer_pid("manager")
                  == self.backend.panes[PaneId.MANAGER_OMP].pid, "the new manager registered")
        return self.session("manager")

    def test_a_report_held_by_the_manager_composer_is_shown_and_follows_a_new_manager_session(self):
        task_id = self.open_work_task()
        self.backend.watchdog.editor_wait = 1.0  # the 30 s of C-D70 (2), shortened (a test constant)
        self.configure("manager", editorEmpty=False)
        old_manager = self.session("manager")
        report = self.tool("worker", "to_manager", {"kind": "progress", "message": "HELD-REPORT-3c9a",
                                                    "task_id": task_id})
        self.assertIn(report.get("status"), ("queued", "accepted", "submitted"), report)
        self.wait(lambda: (self.backend.snapshot().get("recovery") or {}).get("report_wait"),
                  "the editor wait in the backend state", timeout=15)
        self.assertIn(REPORT_WAIT_KO, status_line(self.backend.snapshot()), "the product UI shows the wait")
        self.assertEqual([f for f in self.frames("manager", "deliver", old_manager)
                          if "HELD-REPORT-3c9a" in json.dumps(f)], [], "nothing typed into a busy composer")
        self.configure("manager", editorEmpty=True)
        # the user restarts the manager before the composer was emptied
        self.configure("manager", editorEmpty=False)
        new_manager = self.restart_manager()
        self.configure("manager", editorEmpty=True)
        self.wait(lambda: self.notices("manager", "manager_recovery", new_manager), "manager_recovery")
        self.wait(lambda: any("HELD-REPORT-3c9a" in json.dumps(f) for f in self.frames("manager", "deliver",
                                                                                       new_manager)),
                  "the never-submitted report re-sent to the new manager session", timeout=25)
        self.pump(3)
        recovery = self.notices("manager", "manager_recovery")
        self.assertEqual(len(recovery), 1, recovery)
        copies = [f for f in self.frames("manager", "deliver") if "HELD-REPORT-3c9a" in json.dumps(f)]
        self.assertEqual(len(copies), 1, "the report reaches the manager exactly once")
        self.wait(lambda: not (self.backend.snapshot().get("recovery") or {}).get("report_wait"),
                  "the editor wait cleared")
        self.assertNotIn(REPORT_WAIT_KO, status_line(self.backend.snapshot()), "the UI line is cleared")

    def test_the_recovery_notice_counts_a_report_the_outbox_already_re_sent_to_the_new_session(self):
        """C-D70 (5): the manager_recovery notice carries the count of re-sent reports. The outbox lane may re-send a
        never-submitted report to the new manager session before the watchdog's next tick sees that session; the
        count must still include it (here the lane wins deterministically: the watchdog is ticked by hand)."""
        task_id = self.open_work_task()
        dog = self.backend.watchdog
        dog.tick()  # both sessions seen
        dog.close()  # from here the test ticks it
        self.configure("manager", editorEmpty=False)
        self.tool("worker", "to_manager", {"kind": "progress", "message": "RACED-REPORT-0b7d", "task_id": task_id})
        self.wait(lambda: any(r.get("status") == "deferred" or r.get("blockers")
                              for r in self.backend.handoffs.report_entries()), "the report held by the composer")
        new_manager = self.restart_manager()
        self.configure("manager", editorEmpty=True)
        self.wait(lambda: any("RACED-REPORT-0b7d" in json.dumps(f) for f in self.frames("manager", "deliver",
                                                                                       new_manager)),
                  "the report re-sent to the new manager session", timeout=25)
        dog.tick()
        self.wait(lambda: self.notices("manager", "manager_recovery", new_manager), "manager_recovery")
        recovery = self.notices("manager", "manager_recovery", new_manager)[0]
        self.assertEqual(recovery.get("reports_resent"), 1,
                         "the notice says how many never-submitted reports went to the new session")

    def test_an_unknown_report_is_told_once_without_content_listed_after_a_manager_restart_and_never_resent(self):
        task_id = self.open_work_task()
        self.configure("manager", deliver="unknown")
        manager = self.session("manager")
        self.tool("worker", "to_manager", {"kind": "progress", "message": "UNKNOWN-REPORT-e41b", "task_id": task_id})
        self.wait(lambda: self.frames("manager", "deliver", manager), "the delivery attempt")
        self.configure("manager")
        self.wait(lambda: self.notices("manager", "report_delivery_unknown"), "report_delivery_unknown", timeout=25)
        self.pump(3)
        unknown = self.notices("manager", "report_delivery_unknown")
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].get("task_id"), task_id)
        self.assertNotIn("UNKNOWN-REPORT-e41b", json.dumps(unknown[0]), "never the report content")
        status = self.tool("manager", "workbench_status", {})
        self.assertEqual(status.get("status"), "ok", status)
        listed = [r for r in status.get("reports") or [] if r.get("state") == "unknown"]
        self.assertEqual(len(listed), 1, status.get("reports"))
        self.assertIn("UNKNOWN-REPORT-e41b", listed[0].get("text") or "", "the manager reads it with the status")
        new_manager = self.restart_manager()
        self.wait(lambda: self.notices("manager", "manager_recovery", new_manager), "manager_recovery")
        self.pump(3)
        recovery = self.notices("manager", "manager_recovery", new_manager)
        self.assertEqual(len(recovery), 1)
        self.assertEqual((recovery[0].get("reports_unknown"), recovery[0].get("reports_resent")), (1, 0))
        self.assertNotIn("UNKNOWN-REPORT-e41b", json.dumps(recovery[0]))
        self.assertEqual([f for f in self.frames("manager", "deliver", new_manager)
                          if "UNKNOWN-REPORT-e41b" in json.dumps(f)], [], "an unknown report is never re-sent")


class WorkbenchStatusTests(_Fixture):
    def test_shape_with_an_open_task_and_a_command_run(self):
        task_id = self.open_work_task(commands=[SECOND_COMMAND])
        ran = self.tool("worker", "terminal", {"command": SECOND_COMMAND}, timeout=30)
        self.assertEqual(ran.get("status"), "exited", ran)
        self.restart_worker("for the history")
        status = self.tool("manager", "workbench_status", {"task_id": None})
        self.assertEqual(status.get("status"), "ok", status)
        task = status["task"]
        self.assertEqual(task["task_id"], task_id)
        self.assertIn("FULL-MESSAGE-91b2", task.get("message", ""))
        self.assertEqual(task.get("analysis"), "detailed")
        self.assertEqual(task.get("commands"), [SECOND_COMMAND])
        runs = task.get("commands_run")
        self.assertEqual(len(runs), 1, runs)
        for key in ("exit_code", "duration_seconds", "log_path"):
            self.assertIn(key, runs[0])
        worker = status["worker"]
        for key in ("session_id", "generation", "restarts"):
            self.assertIn(key, worker)
        self.assertEqual([(r.get("requester"), r.get("reason")) for r in worker["restarts"]],
                         [("manager", "for the history")])
        self.assertIn("running", status["terminal"])
        self.assertIsInstance(status["reports"], list)
        by_id = self.tool("manager", "workbench_status", {"task_id": task_id})
        self.assertEqual(by_id["task"]["task_id"], task_id)
        unknown = self.tool("manager", "workbench_status", {"task_id": str(uuid4())})
        self.assertNotEqual(unknown.get("status"), "ok", unknown)

    def test_answers_fast_while_mailboxes_are_busy_and_the_worker_does_not_answer_probes(self):
        self.open_work_task()
        self.configure("worker", probe="hang")
        worker_lock = self.backend.bridge.delivery_lock(ActorRole.WORKER)
        manager_lock = self.backend.bridge.delivery_lock(ActorRole.MANAGER)
        self.assertTrue(worker_lock.acquire(timeout=5))
        self.assertTrue(manager_lock.acquire(timeout=5))
        try:
            started = time.monotonic()
            status = self.tool("manager", "workbench_status", {}, timeout=15)
            elapsed = time.monotonic() - started
        finally:
            worker_lock.release()
            manager_lock.release()
        self.assertEqual(status.get("status"), "ok", status)
        self.assertLess(elapsed, 4.0, "it never waits on a mailbox or a hanging probe")

    def test_the_answer_stays_bounded_with_many_long_reports(self):
        task_id = str(uuid4())
        entries = [{"handoff_id": str(uuid4()), "origin": "worker", "task_id": task_id, "message_id": str(uuid4()),
                    "report_kind": "progress", "text": "y" * 200_000, "state": "pending", "status": "deferred",
                    "submitted": False, "blockers": ["editor_not_empty"], "editor_since": None, "requeued": 0}
                   for _ in range(60)]
        with mock.patch.object(self.backend.handoffs, "report_entries", lambda: [dict(e) for e in entries]):
            status = self.tool("manager", "workbench_status", {})
        self.assertEqual(status.get("status"), "ok", status)
        size = len(json.dumps(status))
        self.assertLess(size, 200_000, f"the answer is bounded ({size} bytes for 12 MB of reports)")
        self.assertGreater(len(status["reports"]), 0)
        self.assertTrue(all(len(r.get("text") or "") <= 8192 for r in status["reports"]))


if __name__ == "__main__":
    unittest.main()
