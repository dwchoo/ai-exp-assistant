"""Independent C-D62 (1) checks (p27-restart-test-01): restart of an exited manager/worker OMP pane.

Expectations were drafted from DECISIONS C-D62 (1), CW-17 'OMP pane 재시작' and the senior/UI worker
required_behavior before the implementation was read. A fake OMP (Python script on a real PTY) records its
argv/env/cwd/session, registers with the real G3 bridge with the injected role token, and understands a few
stdin commands:

* ``exit``      - exit 0
* ``child``     - fork a ``sleep`` helper that stays in the OMP session (and on its PTY) after the OMP exits
* ``escape``    - fork a helper that leaves the session (setsid), keeps the bridge connection and keeps sending
                  late ``state``/``omp_event`` frames of the OLD OMP session after the OMP exits
* ``echo <x>``  - print ``echo:<x>``

A control file ``noregister-<role>`` makes the next fake OMP of that role skip the bridge registration. The
isolation check is replaced by a recorder (no RPC process, no model). Every helper process is recorded with its
start ticks and is killed by exact identity (pidfd) in cleanup; survivors are failures.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from workbench.backend.launcher import LaunchPlan
from workbench.backend.panes import OmpPane
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.service import Backend
from workbench.backend.ui_server import Held
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, Reason
from workbench.contracts.v1 import ContractError, PaneId
from workbench.ipc.bridge_g3.mailbox import BridgeBoundMismatch, BridgeDisconnected
from workbench.terminal.shell_g2.prototype import ShellChoice

FAKE_OMP = r'''#!{python}
import json, os, socket, sys, time, uuid
argv = sys.argv[1:]
if argv[:2] == ["config", "get"]:
    print("[]")
    sys.exit(0)
ctl = os.environ["P27Q_CTL"]
role = os.environ["WORKBENCH_G3_ROLE"]
def ticks(pid):
    return open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()[19]
session = str(uuid.uuid4())
with open(os.path.join(ctl, "panes.jsonl"), "a") as stream:
    stream.write(json.dumps({{"argv": argv, "pid": os.getpid(), "ticks": ticks(os.getpid()), "sid": os.getsid(0),
                              "cwd": os.getcwd(), "env": dict(os.environ), "session": session}}) + "\n")
sock = None
flag = os.path.join(ctl, "noregister-" + role)
if os.path.exists(flag):
    os.unlink(flag)
else:
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(os.environ["WORKBENCH_G3_BRIDGE_SOCKET"])
    generation = int(os.environ["WORKBENCH_G3_GENERATION"])
    hello = {{"kind": "hello", "protocolVersion": 1, "token": os.environ["WORKBENCH_G3_TOKEN"], "role": role,
              "ompSessionId": session, "generation": generation, "pid": os.getpid()}}
    sock.sendall((json.dumps(hello) + "\n").encode())
    json.loads(sock.makefile("rb").readline())
print("fake omp", role, "pid=%d" % os.getpid(), "bridge_gen=" + os.environ["WORKBENCH_G3_GENERATION"], flush=True)
def helper(kind):
    pid = os.fork()
    if pid:
        with open(os.path.join(ctl, "helpers.jsonl"), "a") as stream:
            stream.write(json.dumps({{"kind": kind, "role": role, "pid": pid, "ticks": ticks(pid)}}) + "\n")
        print(kind + "=%d" % pid, flush=True)
        return
    try:
        if kind == "child":
            import signal
            signal.signal(signal.SIGHUP, signal.SIG_IGN)  # survives the leader's exit (like a nohup'd tool)
            os.execv("/bin/sleep", ["sleep", "300"])
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        end = time.monotonic() + 60
        while time.monotonic() < end:
            frames = [{{"kind": "state", "role": role, "sessionId": session, "generation": 1, "late": True}},
                      {{"kind": "omp_event", "sessionId": session, "generation": 1, "name": "late_old_session"}}]
            sock.sendall(b"".join((json.dumps(f) + "\n").encode() for f in frames))
            time.sleep(0.1)
    except BaseException:
        pass
    os._exit(0)
for line in sys.stdin:
    command = line.strip()
    if command == "exit":
        break
    if command in ("child", "escape"):
        helper(command)
    elif command.startswith("echo "):
        print("echo:" + command[5:], flush=True)
'''

OK = {"state": "ok", "ok": True, "leaks": [], "warnings": [], "error": None}


def start_ticks(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if fields[0] in "ZX" else fields[19]


def alive(pid: int) -> bool:
    return start_ticks(pid) is not None


def kill_exact(pid: int, ticks: str) -> None:
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


def own_children() -> dict[str, list[int]]:
    """Direct children of this (backend) process: live and zombie (unreaped) ones."""
    me, live, zombies = os.getpid(), [], []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            fields = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[1]) == me:
            (zombies if fields[0] == "Z" else live).append(int(entry))
    return {"live": sorted(live), "zombies": sorted(zombies)}


def fd_targets() -> list[str]:
    out = []
    for name in os.listdir("/proc/self/fd"):
        try:
            out.append(os.readlink(f"/proc/self/fd/{name}"))
        except OSError:
            pass
    return out


class ContractTests(unittest.TestCase):
    """restart_pane parse/validation (ui_v1): a pane field, nothing else, no payload."""

    def frame(self, payload=b"", **header):
        return ui_v1.Frame({"v": 1, "type": "restart_pane", "id": "r1", **header}, payload)

    def test_valid_panes_parse_and_host_shell_is_left_to_the_backend(self):
        self.assertEqual(ClientType("restart_pane"), ClientType.RESTART_PANE)
        for pane in ("manager_omp", "worker_omp", "host_shell"):
            with self.subTest(pane=pane):
                message = ui_v1.parse_client_frame(self.frame(pane=pane), 1)
                self.assertIs(message.type, ClientType.RESTART_PANE)
                self.assertEqual(message.fields, {"pane": PaneId(pane)})
                self.assertEqual(message.payload, b"")

    def test_missing_unknown_or_non_string_pane_and_any_payload_are_rejected(self):
        for header in ({}, {"pane": "shell"}, {"pane": "MANAGER_OMP"}, {"pane": 1}, {"pane": None}, {"pane": ["manager_omp"]}):
            with self.subTest(header=header), self.assertRaises(ContractError):
                ui_v1.parse_client_frame(self.frame(**header), 1)
        with self.assertRaises(ContractError):
            ui_v1.parse_client_frame(self.frame(b"\r", pane="manager_omp"), 1)
        with self.assertRaises(ContractError):
            ui_v1.parse_client_frame(ui_v1.Frame({"v": 1, "type": "restart_pane", "pane": "manager_omp"}, b""), 1)
        with self.assertRaises(ContractError):  # before hello
            ui_v1.parse_client_frame(self.frame(pane="manager_omp"), None)

    def test_refusal_reasons_are_part_of_the_contract(self):
        for value in ("pane_alive", "pane_not_restartable", "restart_in_progress", "restart_failed",
                      "backend_shutdown", "not_attached", "pane_unavailable"):
            self.assertEqual(Reason(value).value, value)


class _BackendCase(unittest.TestCase):
    ticker_wanted = False

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="p27q-", dir="/tmp")
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        self.root = root
        self.project, home, self.ctl = root / "p", root / "h", root / "c"
        for directory in (self.project, home, self.ctl):
            directory.mkdir()
        fake = root / "omp"
        fake.write_text(FAKE_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        self.fake = fake
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "P27Q_CTL": str(self.ctl),
               "PI_AUTO_QA": "1", "LANG": "C.UTF-8"}
        self.plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), str(fake), "omp/18.4.4", "/x/bridge.ts",
                               ("--plan-arg",))
        self.checks: list[tuple[str, list[str], dict[str, str]]] = []
        self.check_results: dict[str, list[dict]] = {}
        self.gate = threading.Event()
        self.gate.set()
        patcher = mock.patch("workbench.backend.service.check_isolation", self.fake_check)
        patcher.start()
        self.addCleanup(patcher.stop)
        layout = DataLayout(root / "d")
        ensure_private_dir(layout.root)
        self.backend = Backend(layout, self.plan, project_dir=str(self.project), environment=env)
        self.stop = threading.Event()
        self.ticker: threading.Thread | None = None
        self.addCleanup(self.close)
        self.baseline_fds = len(fd_targets())
        self.backend._open()
        self.wait(lambda: self.backend.phase == "ready", "both fake OMPs registered")
        self.wait(lambda: self.backend.omp_isolation["state"] == "ok", "start-up isolation result")

    def fake_check(self, command, *, cwd, environment, role, allowed_skills, omp_version, cancel):
        self.checks.append((role, list(command), dict(environment)))
        self.gate.wait(10)
        queued = self.check_results.get(role)
        return {"role": role, **(queued.pop(0) if queued else OK)}

    # -- helpers
    def start_ticker(self):
        self.ticker = threading.Thread(target=self._tick_loop, daemon=True)
        self.ticker.start()

    def _tick_loop(self):
        while not self.stop.is_set():
            self.backend._tick(0.02)

    def close(self):
        self.gate.set()
        self.stop.set()
        if self.ticker is not None:
            self.ticker.join(5)
        panes = [(e["pid"], e["ticks"]) for e in self.invocations()]
        helpers = self.helpers()
        try:
            self.backend._close()
        finally:
            survivors = []
            for entry in helpers:
                if start_ticks(entry["pid"]) == entry["ticks"]:
                    if entry["kind"] == "child":
                        survivors.append(entry)  # a helper inside the OMP session must be gone
                    kill_exact(entry["pid"], entry["ticks"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and any(start_ticks(e["pid"]) == e["ticks"] for e in helpers):
                time.sleep(0.02)
        for pid, ticks in panes:
            self.assertNotEqual(start_ticks(pid), ticks, f"fake OMP {pid} survived close")
        self.assertEqual([], survivors, "an in-session helper survived the backend close")
        self.assertEqual([], [e for e in helpers if start_ticks(e["pid"]) == e["ticks"]], "helper residue")

    def invocations(self) -> list[dict]:
        path = self.ctl / "panes.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def helpers(self) -> list[dict]:
        path = self.ctl / "helpers.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def of_role(self, role: str) -> list[dict]:
        return [e for e in self.invocations() if e["env"].get("WORKBENCH_G3_ROLE") == role]

    def wait(self, predicate, what, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ticker is None:
                self.backend._tick(0.02)
            else:
                time.sleep(0.02)
            if predicate():
                return
        self.fail(f"timed out waiting for {what}; phase={self.backend.phase} reason={self.backend.reason}")

    def type_line(self, pane_id: PaneId, line: str):
        self.assertIsNone(self.backend.admit(pane_id, line.encode() + b"\n", "input"))

    def exit_pane(self, pane_id: PaneId) -> OmpPane:
        pane = self.backend.panes[pane_id]
        self.type_line(pane_id, "exit")
        self.wait(lambda: pane.info()["alive"] is False, f"{pane_id.value} to exit")
        return pane

    def restart_ready(self, pane_id: PaneId) -> OmpPane:
        self.backend.restart_pane(pane_id)
        new = self.backend.panes[pane_id]
        self.wait(lambda: self.backend.phase == "ready" and "rechecking" not in self.backend.omp_isolation
                  and self.backend.omp_isolation["state"] == "ok", f"{pane_id.value} ready after restart")
        return new


class RestartLaunchIdentityTests(_BackendCase):
    def test_both_roles_restart_with_the_exact_start_argv_env_cwd_and_a_new_pid(self):
        start = {role: self.of_role(role)[0] for role in ("manager", "worker")}
        for role, pane_id in (("manager", PaneId.MANAGER_OMP), ("worker", PaneId.WORKER_OMP)):
            with self.subTest(role=role):
                other_id = PaneId.WORKER_OMP if pane_id is PaneId.MANAGER_OMP else PaneId.MANAGER_OMP
                other, shell = self.backend.panes[other_id], self.backend.panes[PaneId.HOST_SHELL]
                other_ident, shell_pid = (other.pid, start_ticks(other.pid)), shell.ref.pid
                old = self.exit_pane(pane_id)
                new = self.restart_ready(pane_id)
                again = self.of_role(role)[-1]
                first = start[role]
                self.assertEqual(again["pid"], new.pid)
                self.assertNotEqual(new.pid, old.pid)
                self.assertEqual(again["argv"], first["argv"], "the same start-up argv")
                self.assertEqual(again["env"], first["env"], "the same environment (role token included)")
                self.assertEqual(again["cwd"], first["cwd"])
                self.assertEqual(again["cwd"], str(self.project))
                self.assertNotIn("PI_AUTO_QA", again["env"])
                argv = again["argv"]
                self.assertFalse({"--resume", "-r", "--continue", "-c", "--session"} & set(argv), argv)
                for flag in ("--no-extensions", "--no-title", "--plan-arg"):
                    self.assertIn(flag, argv)
                self.assertEqual("", argv[argv.index("--append-system-prompt") + 1])
                self.assertEqual("/x/bridge.ts", argv[argv.index("--extension") + 1])
                overlays = [argv[i + 1] for i, a in enumerate(argv) if a == "--config"]
                self.assertTrue(any(p.endswith(f"omp-isolation-{role}.yml") and Path(p).is_file() for p in overlays),
                                overlays)
                self.assertNotEqual(again["session"], first["session"], "a NEW OMP session")
                self.assertEqual(again["sid"], new.pid, "the new OMP leads its own session")
                # The other OMP, the host shell and the backend are untouched.
                self.assertIs(self.backend.panes[other_id], other)
                self.assertEqual((other.pid, start_ticks(other.pid)), other_ident)
                self.assertIs(self.backend.panes[PaneId.HOST_SHELL], shell)
                self.assertEqual(shell.ref.pid, shell_pid)
                self.assertTrue(shell.alive())
                bridge = self.backend.bridge_state()
                self.assertEqual((bridge[role]["pid"], bridge[role]["pid_matches_pane"]), (new.pid, True))
                self.assertTrue(bridge["worker" if role == "manager" else "manager"]["pid_matches_pane"])
        self.assertEqual(4, len(self.invocations()))

    def test_role_token_is_kept_in_memory_only_never_written_to_the_data_dir(self):
        tokens = {e["env"]["WORKBENCH_G3_TOKEN"] for e in self.invocations()}
        self.exit_pane(PaneId.MANAGER_OMP)
        self.restart_ready(PaneId.MANAGER_OMP)
        self.assertEqual(tokens, {e["env"]["WORKBENCH_G3_TOKEN"] for e in self.invocations()})
        found = []
        for path in self.backend.layout.root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    blob = path.read_bytes()
                except OSError:
                    continue
                found += [str(path) for token in tokens if token.encode() in blob]
        self.assertEqual([], found, "a role token reached disk")
        snapshot = json.dumps(self.backend.snapshot())
        self.assertFalse(any(token in snapshot for token in tokens), "a role token reached the UI snapshot")

    def test_new_pane_takes_input_and_streams_under_a_new_session_and_generation(self):
        old = self.exit_pane(PaneId.WORKER_OMP)
        new = self.restart_ready(PaneId.WORKER_OMP)
        self.assertNotEqual(new.session_id, old.session_id)
        self.assertEqual(new.generation, old.generation + 1)
        self.type_line(PaneId.WORKER_OMP, "echo after-restart")
        seen = bytearray()

        def got():
            for chunk in self.backend.replay():
                if chunk.pane_id is PaneId.WORKER_OMP and chunk.session_id == new.session_id:
                    self.assertEqual(chunk.session_generation, new.generation)
                    seen.extend(chunk.data)
            return b"echo:after-restart" in seen
        self.wait(got, "echo from the restarted OMP")


class RestartCleanupTests(_BackendCase):
    def test_old_pty_and_in_session_children_are_released(self):
        manager = self.backend.panes[PaneId.MANAGER_OMP]
        self.type_line(PaneId.MANAGER_OMP, "child")
        self.wait(lambda: any(h["kind"] == "child" for h in self.helpers()), "in-session helper started")
        helper = next(h for h in self.helpers() if h["kind"] == "child")
        old_fd = manager.master_fd
        old_target = os.readlink(f"/proc/self/fd/{old_fd}")
        ptmx_before = sum(t == "/dev/ptmx" for t in fd_targets())
        self.exit_pane(PaneId.MANAGER_OMP)
        self.assertEqual(start_ticks(helper["pid"]), helper["ticks"], "precondition: the helper outlives the OMP")
        self.restart_ready(PaneId.MANAGER_OMP)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and start_ticks(helper["pid"]) == helper["ticks"]:
            self.backend._tick(0.02)
        self.assertNotEqual(start_ticks(helper["pid"]), helper["ticks"], "the old OMP session's helper survived")
        self.assertEqual(ptmx_before, sum(t == "/dev/ptmx" for t in fd_targets()), "PTY master count changed")
        self.assertEqual(old_target, "/dev/ptmx")
        self.assertEqual([], own_children()["zombies"], "an unreaped child after the restart")

    def test_five_exit_restart_cycles_on_both_roles_leak_no_fd_process_or_thread(self):
        self.wait(lambda: not self.backend._isolation_thread or not self.backend._isolation_thread.is_alive(),
                  "start-up isolation thread done")
        fds, children = len(fd_targets()), own_children()
        threads = threading.active_count()
        for cycle in range(5):
            for pane_id in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP):
                self.exit_pane(pane_id)
                self.restart_ready(pane_id)
        self.wait(lambda: not self.backend._isolation_thread.is_alive(), "last re-check thread done")
        self.wait(lambda: threading.active_count() <= threads, "bridge handler threads of old sessions ended", 5)
        after = own_children()
        self.assertEqual([], after["zombies"])
        self.assertEqual(len(children["live"]), len(after["live"]), (children, after))
        self.assertEqual(fds, len(fd_targets()), "fd count changed over 10 restarts")
        for pane_id in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP):
            info = self.backend.snapshot()["panes"][pane_id.value]
            self.assertEqual(info["generation"], 6)
            self.assertEqual(info["restart"]["count"], 5)
            self.assertTrue(info["alive"])
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual(10, len(record["restarts"]))
        self.assertEqual(12, len(self.invocations()))
        dead = [e for e in self.invocations()[:-2] if e["pid"] not in (self.backend.panes[PaneId.MANAGER_OMP].pid,
                                                                      self.backend.panes[PaneId.WORKER_OMP].pid)]
        self.assertTrue(all(start_ticks(e["pid"]) != e["ticks"] for e in dead))
        for previous, entry in zip(record["restarts"], record["restarts"][2:]):
            self.assertEqual(previous["process"]["pid"], entry["previous"]["process"]["pid"])


class RestartPhaseAndBridgeTests(_BackendCase):
    def test_phase_is_ready_again_only_when_every_pane_lives_and_both_bridges_match(self):
        self.exit_pane(PaneId.MANAGER_OMP)
        self.exit_pane(PaneId.WORKER_OMP)
        self.wait(lambda: self.backend.phase == "degraded", "degraded")
        self.backend.restart_pane(PaneId.MANAGER_OMP)
        for _ in range(40):
            self.backend._tick(0.02)
        self.assertEqual(self.backend.phase, "degraded", "the worker is still exited")
        self.assertEqual(self.backend.reason, "pane_exited:worker_omp")
        # the new worker OMP does not register: phase stays degraded (bridge), never ready
        (self.ctl / "noregister-worker").write_text("")
        self.backend.restart_pane(PaneId.WORKER_OMP)
        self.wait(lambda: self.backend.reason == "bridge_unconnected:worker", "bridge reason for the worker")
        for _ in range(40):
            self.backend._tick(0.02)
        self.assertEqual(self.backend.phase, "degraded")
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual((record["phase"], record["reason"]), ("degraded", "bridge_unconnected:worker"))
        # its exit and a normal restart bring everything back
        self.wait(lambda: "rechecking" not in self.backend.omp_isolation, "worker re-check done")
        self.exit_pane(PaneId.WORKER_OMP)
        self.restart_ready(PaneId.WORKER_OMP)
        self.assertIsNone(self.backend.reason)

    def test_bridge_generation_1_on_pane_generation_2_does_not_confuse_identity_or_fencing(self):
        before = self.backend.bridge_state()["manager"]
        old_identity = (before["session_id"], before["generation"])
        self.assertEqual(before["generation"], 1)
        old = self.exit_pane(PaneId.MANAGER_OMP)
        new = self.restart_ready(PaneId.MANAGER_OMP)
        after = self.backend.bridge_state()["manager"]
        self.assertEqual(new.generation, 2)
        self.assertEqual(after["generation"], 1, "bridge generation is not the pane generation (by design)")
        self.assertEqual(after["pid"], new.pid)
        self.assertNotEqual(after["session_id"], old_identity[0])
        self.assertNotEqual(after["pid"], old.pid)
        # a request bound to the OLD OMP session is fenced, never delivered to the new one
        with self.assertRaises(BridgeBoundMismatch):
            self.backend.bridge.request("manager", {"kind": "probe"}, 2, expected_peer=old_identity)

    def test_a_stale_peer_of_the_old_process_neither_satisfies_readiness_nor_leaks_late_events(self):
        self.type_line(PaneId.MANAGER_OMP, "escape")
        self.wait(lambda: any(h["kind"] == "escape" for h in self.helpers()), "escaped helper started")
        old_session = self.backend.bridge_state()["manager"]["session_id"]
        old = self.exit_pane(PaneId.MANAGER_OMP)
        self.wait(lambda: self.backend.phase == "degraded", "degraded after the exit")
        stale = self.backend.bridge_state()["manager"]
        self.assertEqual((stale["connected"], stale["pid"]), (True, old.pid), "precondition: stale old peer connected")
        self.assertEqual(self.backend.reason, "pane_exited:manager_omp")
        # the new OMP does not register: only the stale old-session peer is connected
        (self.ctl / "noregister-manager").write_text("")
        self.backend.restart_pane(PaneId.MANAGER_OMP)
        self.wait(lambda: self.backend.reason == "bridge_unconnected:manager", "bridge reason (stale peer only)")
        for _ in range(30):
            self.backend._tick(0.02)
        self.assertEqual(self.backend.phase, "degraded", "the stale peer of the old pid made the pane ready")
        self.assertNotEqual(self.backend.bridge_state()["manager"]["pid"], self.backend.panes[PaneId.MANAGER_OMP].pid)
        # a normal restart replaces the stale peer; its late frames are ignored from then on
        self.wait(lambda: "rechecking" not in self.backend.omp_isolation, "re-check done")
        self.exit_pane(PaneId.MANAGER_OMP)
        new = self.restart_ready(PaneId.MANAGER_OMP)
        cursor = self.backend.bridge.event_cursor()
        for _ in range(25):
            self.backend._tick(0.02)
        state = self.backend.bridge_state()["manager"]
        self.assertEqual((state["pid"], state["pid_matches_pane"]), (new.pid, True))
        self.assertNotEqual(state["session_id"], old_session)
        late = [e for e in list(self.backend.bridge._events)
                if e.get("bridgeSequence", 0) > cursor and e.get("sessionId") == old_session]
        self.assertEqual([], late, "late old-session events accepted after the new OMP registered")
        self.assertEqual(self.backend.phase, "ready")
        self.assertNotEqual(old.pid, new.pid)

    def test_isolation_is_rechecked_for_that_role_only_and_a_leak_is_shown(self):
        start_checks = list(self.checks)
        worker_check = next(c for c in start_checks if c[0] == "worker")
        self.check_results["worker"] = [{"state": "leak", "ok": False, "leaks": ["APPEND_SYSTEM.md"], "warnings": [],
                                         "error": None}]
        self.exit_pane(PaneId.WORKER_OMP)
        self.backend.restart_pane(PaneId.WORKER_OMP)
        self.wait(lambda: len(self.checks) == len(start_checks) + 1 and "rechecking" not in self.backend.omp_isolation,
                  "one re-check")
        role, command, env = self.checks[-1]
        self.assertEqual(role, "worker")
        self.assertEqual((command, env), (worker_check[1], worker_check[2]), "the start-time worker check again")
        isolation = self.backend.snapshot()["omp_isolation"]
        self.assertEqual(isolation["state"], "leak", isolation)
        self.assertIn("worker", isolation["roles"])
        self.assertIn("manager", isolation["roles"])
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual(record["omp_isolation"]["state"], "leak")


class RestartRefusalAndFailureTests(_BackendCase):
    def test_concurrent_requests_start_exactly_one_new_omp(self):
        self.exit_pane(PaneId.MANAGER_OMP)
        barrier = threading.Barrier(8)
        outcomes: list[object] = []
        lock = threading.Lock()

        def call():
            barrier.wait(5)
            try:
                value: object = self.backend.restart_pane(PaneId.MANAGER_OMP)
            except Held as held:
                value = held.reason
            with lock:
                outcomes.append(value)
        threads = [threading.Thread(target=call) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        oks = [o for o in outcomes if isinstance(o, dict)]
        refusals = [o for o in outcomes if not isinstance(o, dict)]
        self.assertEqual(1, len(oks), outcomes)
        self.assertTrue(set(refusals) <= {Reason.RESTART_IN_PROGRESS, Reason.PANE_ALIVE}, refusals)
        self.wait(lambda: self.backend.phase == "ready", "ready")
        self.assertEqual(3, len(self.invocations()), "exactly one new OMP")

    def test_refusals_carry_reasons_and_touch_nothing(self):
        panes = dict(self.backend.panes)
        count = len(self.invocations())
        # C-D63 superseded the old host-shell rule: a live host shell is refused like a live OMP (pane_alive).
        shell_pid = self.backend.panes[PaneId.HOST_SHELL].pid
        for pane_id, reason in ((PaneId.MANAGER_OMP, Reason.PANE_ALIVE), (PaneId.WORKER_OMP, Reason.PANE_ALIVE),
                                (PaneId.HOST_SHELL, Reason.PANE_ALIVE)):
            with self.subTest(pane=pane_id.value), self.assertRaises(Held) as held:
                self.backend.restart_pane(pane_id)
            self.assertEqual(held.exception.reason, reason)
            self.assertTrue(held.exception.detail)
        self.exit_pane(PaneId.WORKER_OMP)
        self.backend._stop_signal = signal.SIGTERM  # a signal is being handled: shutting down
        try:
            with self.assertRaises(Held) as held:
                self.backend.restart_pane(PaneId.WORKER_OMP)
            self.assertEqual(held.exception.reason, Reason.BACKEND_SHUTDOWN)
        finally:
            self.backend._stop_signal = None
        self.assertEqual(panes, self.backend.panes)
        self.assertEqual(count, len(self.invocations()))
        self.assertTrue(all(alive(p.pid) for p in (panes[PaneId.MANAGER_OMP],)))
        self.assertTrue(alive(shell_pid), "a refused host-shell restart touched the live shell")
        self.assertTrue(self.backend.panes[PaneId.HOST_SHELL].alive())

    def test_host_shell_exit_is_restarted_only_on_request(self):
        # Adapted for C-D63 (was test_host_shell_exit_is_never_restarted): an exited host shell is now restarted,
        # but only by an explicit restart_pane request. The protected invariants stay: the backend never restarts it
        # by itself, and a restarted OMP does not turn a backend with an exited host shell ready.
        shell = self.backend.panes[PaneId.HOST_SHELL]
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"exit\n", "input"))
        self.wait(lambda: shell._exited, "host shell exit")
        self.exit_pane(PaneId.MANAGER_OMP)
        self.backend.restart_pane(PaneId.MANAGER_OMP)
        for _ in range(40):
            self.backend._tick(0.02)
        self.assertIs(self.backend.panes[PaneId.HOST_SHELL], shell, "the backend restarted the host shell by itself")
        self.assertIs(self.backend.shell, shell)
        self.assertEqual(self.backend.phase, "degraded")
        self.assertIn("host_shell", self.backend.reason)
        self.assertFalse(self.backend.snapshot()["panes"]["host_shell"]["alive"])
        omp_count = len(self.invocations())
        result = self.backend.restart_pane(PaneId.HOST_SHELL)
        new = self.backend.panes[PaneId.HOST_SHELL]
        self.assertIsNot(new, shell)
        self.assertEqual((result["restarted"], result["generation"], result["input_owner"]),
                         (True, shell.generation + 1, "user"))
        self.assertEqual(omp_count, len(self.invocations()), "a host-shell restart started an OMP")
        self.wait(lambda: self.backend.phase == "ready", "ready once every pane lives again")
        with self.assertRaises(Held) as held:
            self.backend.restart_pane(PaneId.HOST_SHELL)
        self.assertEqual(held.exception.reason, Reason.PANE_ALIVE)

    def test_spawn_failure_keeps_the_loop_sane_and_a_later_retry_works(self):
        old = self.exit_pane(PaneId.MANAGER_OMP)
        with mock.patch("workbench.backend.panes.pty.fork", side_effect=OSError(11, "fork refused")):
            with self.assertRaises(Held) as held:
                self.backend.restart_pane(PaneId.MANAGER_OMP)
        self.assertEqual(held.exception.reason, Reason.RESTART_FAILED)
        self.assertIn("fork refused", held.exception.detail)
        for _ in range(30):  # the loop keeps running on the exited (released) pane
            self.backend._tick(0.02)
        snapshot = self.backend.snapshot()
        info = snapshot["panes"]["manager_omp"]
        self.assertFalse(info["alive"])
        self.assertEqual(info["restart"]["state"], "failed")
        self.assertEqual(self.backend.phase, "degraded")
        self.assertIs(self.backend.panes[PaneId.MANAGER_OMP], old)
        record = json.loads(self.backend.layout.record.read_text())
        self.assertEqual(record["phase"], "degraded")
        self.assertEqual(2, len(self.invocations()), "no OMP started by the failed attempt")
        new = self.restart_ready(PaneId.MANAGER_OMP)
        self.assertEqual(new.generation, old.generation + 1)
        self.assertEqual(3, len(self.invocations()))

    def test_an_omp_that_cannot_exec_any_more_exits_again_and_is_restartable_later(self):
        self.exit_pane(PaneId.MANAGER_OMP)
        self.fake.chmod(0o600)  # e.g. the OMP binary was removed/replaced during the session
        try:
            self.backend.restart_pane(PaneId.MANAGER_OMP)
            new = self.backend.panes[PaneId.MANAGER_OMP]
            self.wait(lambda: new.info()["alive"] is False, "the unexecutable OMP exits")
            self.assertEqual(self.backend.phase, "degraded")
        finally:
            self.fake.chmod(0o700)
        self.wait(lambda: "rechecking" not in self.backend.omp_isolation, "re-check done")
        self.restart_ready(PaneId.MANAGER_OMP)


class UiSocketRestartTests(_BackendCase):
    """restart_pane through the real UiServer socket (the only UI path)."""

    def setUp(self):
        super().setUp()
        self.start_ticker()
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(str(self.backend.layout.ui_socket))
        self.addCleanup(self.sock.close)
        self.decoder = ui_v1.FrameDecoder()
        self.frames: list[ui_v1.Frame] = []
        self.counter = 0
        self.send({**ui_v1.hello("p27q")})
        welcome = self.next(lambda f: f.header.get("type") in ("welcome", "reject"))
        self.assertEqual(welcome.header["type"], "welcome")

    def send(self, header, payload=b""):
        self.sock.sendall(ui_v1.encode_frame(header, payload))

    def pump(self, timeout=0.05):
        if select.select([self.sock], [], [], timeout)[0]:
            data = self.sock.recv(1 << 20)
            if data:
                self.frames.extend(self.decoder.feed(data))

    def next(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for i, frame in enumerate(self.frames):
                if predicate(frame):
                    return self.frames.pop(i)
            self.pump()
        self.fail("no matching frame")

    def call(self, kind, payload=b"", **fields):
        self.counter += 1
        rid = f"q{self.counter}"
        self.send(ui_v1.request(kind, rid, **fields), payload)
        return self.next(lambda f: f.header.get("type") == "result" and f.header.get("id") == rid).header

    def test_not_attached_host_live_invalid_then_ok_with_new_display_identity(self):
        refused = self.call(ClientType.RESTART_PANE, pane="manager_omp")
        self.assertEqual((refused["ok"], refused["reason"]), (False, "not_attached"))
        attached = self.call(ClientType.ATTACH, size={"rows": 30, "cols": 120})
        self.assertTrue(attached["ok"])
        # C-D63: a live host shell is refused as pane_alive (pane_not_restartable is no longer returned).
        for pane, reason in (("host_shell", "pane_alive"), ("manager_omp", "pane_alive"),
                             ("worker_omp", "pane_alive")):
            header = self.call(ClientType.RESTART_PANE, pane=pane)
            self.assertEqual((header["ok"], header["reason"]), (False, reason), header)
            self.assertTrue(header.get("detail"))
        for bad in ({"pane": "bogus"}, {}):
            header = self.call(ClientType.RESTART_PANE, **bad)
            self.assertEqual((header["ok"], header["reason"]), (False, "invalid_message"), header)
        header = self.call(ClientType.RESTART_PANE, b"\r", pane="manager_omp")
        self.assertEqual((header["ok"], header["reason"]), (False, "invalid_message"), header)
        old = self.backend.panes[PaneId.MANAGER_OMP]
        self.assertTrue(self.call(ClientType.INPUT, b"exit\n", pane="manager_omp")["ok"])
        self.next(lambda f: f.header.get("type") == "state"
                  and f.header.get("snapshot", {}).get("panes", {}).get("manager_omp", {}).get("alive") is False)
        ok = self.call(ClientType.RESTART_PANE, pane="manager_omp")
        self.assertTrue(ok["ok"], ok)
        new = self.backend.panes[PaneId.MANAGER_OMP]
        self.assertEqual((ok["pane"], ok["restarted"], ok["generation"], ok["session_id"]),
                         ("manager_omp", True, old.generation + 1, new.session_id))
        self.assertEqual(ok["process"]["pid"], new.pid)
        frame = self.next(lambda f: f.header.get("type") == "display" and f.header.get("pane") == "manager_omp"
                          and f.header.get("session_id") == new.session_id)
        self.assertEqual(frame.header["generation"], new.generation)
        self.assertNotIn(b"pid=%d" % old.pid, frame.payload)
        state = self.next(lambda f: f.header.get("type") == "state"
                          and f.header["snapshot"]["panes"]["manager_omp"].get("generation") == new.generation
                          and f.header["snapshot"]["panes"]["manager_omp"].get("alive") is True)
        self.assertEqual(state.header["snapshot"]["panes"]["manager_omp"]["restart"]["previous"]["process"]["pid"],
                         old.pid)
        again = self.call(ClientType.RESTART_PANE, pane="manager_omp")
        self.assertEqual(again["ok"], False)
        self.assertIn(again["reason"], ("pane_alive", "restart_in_progress"))
        self.assertEqual(3, len(self.invocations()))


if __name__ == "__main__":
    unittest.main()
