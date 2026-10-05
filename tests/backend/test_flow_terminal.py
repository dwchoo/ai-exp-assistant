"""C-D68 (1): the worker's ``terminal`` tool on the backend (no OMP, no provider).

- refusal matrix with a fake host port (nothing typed, nothing held);
- a real backend ShellPane through HostShellPort: exit code, output tail, log
  file, the command runs in the host shell's current directory (where the user
  last cd'd, C-D68 (7)) and the parent shell's directory is never changed, the
  user regains input, the UI sees the output; timeout and a later wait with
  ``command: null``; a new command while one runs; pause (new command refused,
  the running one continues); default wait 120 s, at most 1800 s;
- experiment run vs. terminal command exclusion through the shared HostGate
  (real TaskFlow with the U2b fakes);
- journal records (command, start/end, exit code, log path, refusal reason; no
  output, no environment value);
- the backend routes ``terminal`` tool requests to the TerminalService.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import stat
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

from workbench.backend.flow import HandoffService
from workbench.backend.flow_terminal import (
    DEFAULT_TIMEOUT, MAX_TIMEOUT, TAIL_BYTES, TAIL_LINES, HostGate, TerminalService, output_tail,
    validate_terminal_arguments,
)
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.backend.service import Backend
from workbench.contracts.v1 import ActorRole
from workbench.terminal.shell_g2.prototype import ShellChoice

import test_task_flow as flow_fixtures

WORKER_SESSION = str(uuid4())
AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}
SECRET = "s3cr3t-value-for-the-terminal-test"


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def call(args, call_id=None, role_session=WORKER_SESSION, tool="terminal"):
    return {"request_id": str(uuid4()), "tool_call_id": call_id or f"t-{uuid4().hex[:8]}", "tool": tool,
            "args": args, "session_id": role_session, "generation": 1}


def journal(root: Path) -> list[dict]:
    path = root / "workflow" / "handoffs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class FakeHostPort:
    """Records every write; a busy reason keeps it busy."""

    choice = SimpleNamespace(kind="bash")

    def __init__(self, busy=None):
        self.busy_reason = busy
        self.sent: list[bytes] = []
        self.holds: list[str] = []
        self.detached = 0

    def busy(self):
        return self.busy_reason

    def hold(self, reason):
        if self.busy_reason is not None:
            return self.busy_reason
        self.holds.append(reason)
        return None

    def release_hold(self, reason=None):
        pass

    def send_user(self, data):
        self.sent.append(data)

    def detach(self):
        self.detached += 1


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cd68-terminal-", dir="/tmp")
        self.root = Path(self.tmp.name)
        (self.root / "workflow").mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.paused = False
        self.activity = None
        self.port = FakeHostPort()
        self.gate = HostGate()
        self.handoffs = HandoffService(self.root / "workflow" / "handoffs.jsonl",
                                       mailbox=flow_fixtures.FakeMailbox())
        self.terminal = self.make_terminal(lambda: self.port)

    def make_terminal(self, host_shell):
        return TerminalService(handoffs=self.handoffs, host_shell=host_shell, gate=self.gate,
                               log_root=self.root / "workflow" / "terminal",
                               automation=lambda: AUTOMATION, paused=lambda: self.paused,
                               activity=lambda: self.activity, sensitive_values=lambda: (SECRET,),
                               poll_interval=0.02)

    def tearDown(self):
        self.terminal.close()
        self.handoffs.close()
        self.tmp.cleanup()

    def run_tool(self, args, role=ActorRole.WORKER, **kwargs):
        return self.terminal.handle(role, call(args, **kwargs))


class RefusalTests(ServiceFixture):
    def assert_nothing_typed(self):
        self.assertEqual(self.port.sent, [], "a refusal never types into the host shell")
        self.assertEqual(self.port.holds, [])
        self.assertIsNone(self.gate.owner)

    def test_only_the_worker_may_use_the_tool(self):
        result = self.run_tool({"command": "true", "timeout_seconds": None}, role=ActorRole.MANAGER)
        self.assertEqual((result["status"], result["reason"]), ("rejected", "tool_not_allowed_for_role"))
        self.assert_nothing_typed()

    def test_invalid_arguments_name_the_field(self):
        for args, field in (({"command": "ls", "timeout_seconds": 0}, "timeout_seconds"),
                            ({"command": "ls", "timeout_seconds": MAX_TIMEOUT + 1}, "timeout_seconds"),
                            ({"command": "ls", "timeout_seconds": True}, "timeout_seconds"),
                            ({"command": 5, "timeout_seconds": None}, "command"),
                            ({"command": "ls", "timeout_seconds": None, "cwd": "/"}, "cwd")):
            result = self.run_tool(args)
            self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), args)
            self.assertTrue(any(error.startswith(field) for error in result["errors"]), result)
        self.assert_nothing_typed()

    def test_default_wait_is_120_seconds_and_at_most_1800(self):
        # C-D68 (7) user decision 2026-10-05: "2분, 최대 30분".
        self.assertEqual((DEFAULT_TIMEOUT, MAX_TIMEOUT), (120, 1800))
        self.assertEqual(validate_terminal_arguments({"command": "ls", "timeout_seconds": 1800}), [])
        self.assertEqual(validate_terminal_arguments({"command": "ls", "timeout_seconds": None}), [])
        errors = validate_terminal_arguments({"command": "ls", "timeout_seconds": 1801})
        self.assertEqual(len(errors), 1)
        self.assertIn("1 to 1800", errors[0])
        self.assertIn("null for 120", errors[0])
        self.paused = True  # nothing runs; the journaled request shows the timeout used
        self.run_tool({"command": "true", "timeout_seconds": None})
        request = next(r for r in journal(self.root) if r["type"] == "terminal_request")
        self.assertEqual(request["timeout_seconds"], 120)

    def test_a_command_with_an_environment_value_is_refused_and_never_journaled(self):
        result = self.run_tool({"command": f"echo {SECRET}", "timeout_seconds": None})
        self.assertEqual((result["status"], result["reason"]), ("rejected", "environment_value"))
        self.assertNotIn(SECRET, (self.root / "workflow" / "handoffs.jsonl").read_text())
        self.assert_nothing_typed()

    def test_paused_refuses_a_new_command(self):
        self.paused = True
        result = self.run_tool({"command": "true", "timeout_seconds": None})
        self.assertEqual(result["status"], "paused")
        self.assert_nothing_typed()

    def test_busy_typing_or_missing_host_shell_is_host_terminal_busy(self):
        for reason in ("a line is being typed in the host shell", "the host shell has jobs",
                       "manager owns the host shell", "host shell has exited"):
            self.port.busy_reason = reason
            result = self.run_tool({"command": "true", "timeout_seconds": None})
            self.assertEqual((result["status"], result["reason"]), ("host_terminal_busy", reason))
            self.assert_nothing_typed()
        self.port.busy_reason = None
        missing = self.make_terminal(lambda: None)
        result = missing.handle(ActorRole.WORKER, call({"command": "true", "timeout_seconds": None}))
        self.assertEqual(result["status"], "host_terminal_busy")
        self.assert_nothing_typed()

    def test_an_active_or_starting_experiment_is_host_terminal_busy(self):
        self.activity = "an experiment run is starting"
        result = self.run_tool({"command": "true", "timeout_seconds": None})
        self.assertEqual((result["status"], result["reason"]), ("host_terminal_busy", "an experiment run is starting"))
        self.activity = None
        self.assertIsNone(self.gate.acquire("experiment"))
        result = self.run_tool({"command": "true", "timeout_seconds": None})
        self.assertEqual(result["status"], "host_terminal_busy")
        self.assertIn("experiment", result["reason"])
        self.gate.release("experiment")
        self.assert_nothing_typed()

    def test_wait_without_a_command_and_duplicate_calls(self):
        result = self.run_tool({"command": None, "timeout_seconds": None})
        self.assertEqual((result["status"], result["reason"]), ("rejected", "no_terminal_command"))
        first = self.run_tool({"command": None, "timeout_seconds": 5}, call_id="same")
        again = self.run_tool({"command": None, "timeout_seconds": 5}, call_id="same")
        self.assertEqual(first, again)
        self.assertTrue(any(r["type"] == "terminal_duplicate" for r in journal(self.root)))

    def test_refusals_are_journaled_with_their_reason(self):
        self.port.busy_reason = "the host shell has jobs"
        self.run_tool({"command": "make test", "timeout_seconds": None})
        records = journal(self.root)
        request = next(r for r in records if r["type"] == "terminal_request")
        self.assertEqual((request["command"], request["timeout_seconds"]), ("make test", DEFAULT_TIMEOUT))
        refused = next(r for r in records if r["type"] == "terminal_refused")
        self.assertEqual((refused["status"], refused["reason"]), ("host_terminal_busy", "the host shell has jobs"))
        result = next(r for r in records if r["type"] == "terminal_result")
        self.assertEqual(result["result"]["status"], "host_terminal_busy")

    def test_output_tail_is_bounded_and_readable(self):
        text, truncated = output_tail(b"\x1b[31mred\x1b[0m\r\nplain\r\n")
        self.assertEqual((text, truncated), ("red\nplain\n", False))
        many = b"".join(b"line %d\r\n" % i for i in range(1000))
        text, truncated = output_tail(many)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.splitlines()), TAIL_LINES)
        self.assertIn("line 999", text)
        text, truncated = output_tail(b"x" * (TAIL_BYTES * 3))
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode()), TAIL_BYTES)


class RealHostShellTerminalTests(ServiceFixture):
    """The TerminalService on a real backend ShellPane through HostShellPort."""

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8",
                               "WB_FIXTURE_NAME": "exported-by-user"})
        self.ui = bytearray()
        self.stop = threading.Event()
        self.loop = threading.Thread(target=self._backend_loop, daemon=True)
        self.loop.start()
        self.terminal.close()
        self.terminal = self.make_terminal(lambda: HostShellPort(self.pane, lambda: self.pane))
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        self.user_types(f"cd {self.home}\r".encode())
        self.assertTrue(wait_until(lambda: self.pane.cwd() == str(self.home), 5))
        self.assertTrue(wait_until(self.idle, 5))

    def _backend_loop(self):
        while not self.stop.is_set():
            for chunk in self.pane.pump():
                self.ui.extend(chunk.data)
            time.sleep(0.01)

    def tearDown(self):
        self.terminal.close()
        self.stop.set()
        self.loop.join(5)
        self.pane.close()
        super().tearDown()

    def user_types(self, data):
        self.assertIsNone(self.pane.admit(data))

    def idle(self):
        port = HostShellPort(self.pane, lambda: self.pane)
        try:
            return port.busy() is None
        finally:
            port.detach()

    def assert_user_owns_the_shell_again(self, cwd=None):
        self.assertTrue(wait_until(lambda: self.pane.state["input_owner"] == "user"
                                   and self.pane.state["parent_mode"] == "manual_prompt", 10), self.pane.state)
        self.assertIsNone(self.pane.automation_hold)
        self.assertEqual(self.pane.cwd(), str(cwd or self.home), "the parent shell's directory never changes")
        self.assertIsNone(self.gate.owner)
        marker = f"user-typed-{uuid4().hex[:6]}"
        self.user_types(f"echo {marker}\r".encode())
        self.assertTrue(wait_until(lambda: bytes(self.ui).count(marker.encode()) >= 2, 5), "the user regains input")

    def test_a_command_runs_in_the_host_shell_and_returns_exit_code_tail_and_log(self):
        result = self.run_tool({"command": "printf 'hello from %s\\n' \"$WB_FIXTURE_NAME\"; pwd; exit 3",
                                "timeout_seconds": 30})
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("hello from exported-by-user", result["output_tail"], "the child inherits the exported env")
        self.assertIn(str(self.home), result["output_tail"], "it runs where the user last cd'd")
        self.assertNotIn(str(self.project), result["output_tail"], "not in the project directory")
        self.assertEqual(result["cwd"], str(self.home), "the result records the directory used")
        self.assertNotIn("wb-handoff", result["output_tail"], "preparation is not command output")
        log = Path(result["log_path"])
        self.assertTrue(log.is_relative_to(self.root / "workflow" / "terminal"))
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertIn(b"hello from exported-by-user", log.read_bytes())
        self.assertGreaterEqual(result["duration_seconds"], 0)
        self.assertTrue(wait_until(lambda: b"hello from exported-by-user" in self.ui), "the user sees the output")
        self.assert_user_owns_the_shell_again()
        records = journal(self.root)
        started = next(r for r in records if r["type"] == "terminal_started")
        ended = next(r for r in records if r["type"] == "terminal_ended")
        request = next(r for r in records if r["type"] == "terminal_request")
        self.assertEqual(started["command_id"], result["command_id"])
        self.assertIn("exit 3", started["command"])
        self.assertEqual(started["cwd"], str(self.home), "the journal records the directory used")
        self.assertIsNone(request["task_id"], "usable without a Task")
        self.assertIsNone(started["task_id"], "usable without a Task")
        self.assertEqual((ended["status"], ended["exit_code"], ended["log_path"], ended["cwd"]),
                         ("exited", 3, result["log_path"], str(self.home)))
        self.assertNotIn("cwd_restore", ended, "the parent shell is never moved, so nothing is restored")
        self.assertTrue(ended["started_at"] and ended["ended_at"])
        text = (self.root / "workflow" / "handoffs.jsonl").read_text()
        self.assertNotIn("hello from exported-by-user", text, "output is only in the log file")
        self.assertNotIn("exported-by-user", text, "no environment value is stored")

    def test_typing_user_refuses_without_typing(self):
        self.user_types(b"echo half-typed")
        self.assertTrue(wait_until(lambda: not self.idle(), 5))
        before = bytes(self.ui)
        result = self.run_tool({"command": "echo never-run", "timeout_seconds": 5})
        self.assertEqual(result["status"], "host_terminal_busy", result)
        time.sleep(0.3)
        self.assertNotIn(b"never-run", bytes(self.ui)[len(before):])
        self.assertIsNone(self.pane.automation_hold)
        self.user_types(b"\x15")  # the user clears the line

    def test_timeout_then_wait_again_and_one_command_at_a_time(self):
        flag = self.root / "go"
        first = self.run_tool({"command": f"echo started; while [ ! -e {flag} ]; do sleep 0.05; done; echo late-done",
                               "timeout_seconds": 1})
        self.assertEqual(first["status"], "running", first)
        self.assertIn("started", first["output_tail"])
        self.assertIn("End your turn", first["detail"])  # C-D68 (8): no re-wait loop
        self.assertNotIn("command null", first["detail"])
        second = self.run_tool({"command": "echo other", "timeout_seconds": 5})
        self.assertEqual(second["status"], "terminal_command_running", second)
        self.assertEqual(second["command_id"], first["command_id"])
        self.assertIsNotNone(self.gate.acquire("experiment"), "an experiment sees the host busy meanwhile")
        self.assertEqual(self.gate.owner, "terminal")
        flag.touch()
        waited = self.run_tool({"command": None, "timeout_seconds": 30})
        self.assertEqual((waited["status"], waited["exit_code"], waited["command_id"]),
                         ("exited", 0, first["command_id"]), waited)
        self.assertIn("late-done", waited["output_tail"])
        self.assert_user_owns_the_shell_again()
        self.assertNotIn(b"other", Path(waited["log_path"]).read_bytes())

    def test_abandoned_wait_lets_the_command_finish_and_free_the_shell(self):
        # The bridge answers an aborted tool call itself; the backend keeps following the command.
        result = self.run_tool({"command": "sleep 0.5; echo finished-alone", "timeout_seconds": 1})
        self.assertIn(result["status"], ("running", "exited"))
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        self.assertEqual(self.terminal.current()["status"], "exited")
        self.assert_user_owns_the_shell_again()

    def test_pause_refuses_new_commands_while_the_running_one_continues(self):
        flag = self.root / "go"
        first = self.run_tool({"command": f"while [ ! -e {flag} ]; do sleep 0.05; done; echo resumed-output",
                               "timeout_seconds": 1})
        self.assertEqual(first["status"], "running", first)
        self.paused = True
        refused = self.run_tool({"command": "echo while-paused", "timeout_seconds": 5})
        self.assertEqual(refused["status"], "terminal_command_running")
        flag.touch()
        waited = self.run_tool({"command": None, "timeout_seconds": 30})
        self.assertEqual((waited["status"], waited["exit_code"]), ("exited", 0), waited)
        self.assertIn("resumed-output", waited["output_tail"])
        self.assert_user_owns_the_shell_again()
        paused = self.run_tool({"command": "echo while-paused", "timeout_seconds": 5})
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(self.terminal.current()["command_id"], first["command_id"], "nothing new was run")
        self.assertNotIn(b"while-paused", bytes(self.ui))
        self.paused = False

    def user_cds(self, path):
        self.user_types(f"cd {shlex.quote(str(path))}\r".encode())
        self.assertTrue(wait_until(lambda: self.pane.cwd() == str(path), 5))
        self.assertTrue(wait_until(self.idle, 5))

    def test_each_command_runs_where_the_user_last_cd_d_and_never_moves_the_shell(self):
        elsewhere = self.root / "elsewhere dir"
        elsewhere.mkdir()
        for place in (elsewhere, self.project, self.home):
            self.user_cds(place)
            before = len(self.ui)
            result = self.run_tool({"command": "pwd -P; cd / && pwd -P", "timeout_seconds": 30})
            self.assertEqual((result["status"], result["exit_code"], result["cwd"]), ("exited", 0, str(place)),
                             result)
            self.assertEqual(result["output_tail"].splitlines()[0], str(place), result["output_tail"])
            self.assert_user_owns_the_shell_again(cwd=place)  # a cd inside the command stays in the child
            self.assertNotIn(b"cd --", bytes(self.ui)[before:], "nothing changes the parent's directory")
        ended = [r for r in journal(self.root) if r["type"] == "terminal_ended"]
        self.assertEqual([r["cwd"] for r in ended], [str(elsewhere), str(self.project), str(self.home)])


class ExperimentExclusionTests(flow_fixtures.FlowFixture):
    """The real TaskFlow and a TerminalService share one HostGate."""

    def open(self, *, start=True):
        self.gate = getattr(self, "gate", None) or HostGate()
        flow = super().open(start=False)
        flow._host_gate = self.gate
        if start:
            flow.start()
        return flow

    def test_a_running_terminal_command_holds_the_experiment_start(self):
        self.assertIsNone(self.gate.acquire("terminal"))
        result = self.new_experiment()
        self.assertEqual(result["status"], "dispatched", result)
        # p27-cd68-review-01 P3-5: the worker's own command is the cause, not the user's shell.
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"]
                                   == "host_terminal_busy:worker_terminal_command"))
        time.sleep(0.1)
        self.assertEqual(self.runs, [], "nothing starts while a terminal command runs")
        self.assertEqual(self.port.holds, 0)
        self.gate.release("terminal")
        self.assertTrue(wait_until(lambda: len(self.runs) == 1 and self.flow.task_view()["status"] == "finished"))
        self.assertTrue(wait_until(lambda: self.gate.owner is None), "the experiment releases the gate after its run")

    def test_an_experiment_starting_or_running_refuses_the_terminal(self):
        terminal = TerminalService(handoffs=self.service, host_shell=lambda: FakeHostPort(), gate=self.gate,
                                   log_root=self.root / "terminal",
                                   automation=lambda: AUTOMATION, activity=self.flow.experiment_host_activity)
        self.gates.exit.clear()  # the experiment's host command keeps running
        result = self.new_experiment()
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        refused = terminal.handle(ActorRole.WORKER, call({"command": "true", "timeout_seconds": 1}))
        self.assertEqual(refused["status"], "host_terminal_busy", refused)
        self.gates.exit.set()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "finished"))
        self.assertTrue(wait_until(lambda: self.gate.owner is None))
        self.assertIsNone(self.flow.experiment_host_activity())
        terminal.close()

    def test_a_pending_experiment_start_is_reported_as_activity(self):
        self.port.busy_reason = "a line is being typed in the host shell"
        result = self.new_experiment()
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"] == "host_terminal_busy"))
        self.assertIsNotNone(self.flow.experiment_host_activity())
        self.port.busy_reason = None
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "finished"))


class ManagerRuleResultTests(flow_fixtures.FlowFixture):
    """C-D68 (3): every accepted to_worker result says the worker does the Task and the manager waits."""

    def test_dispatched_rerun_and_queued_results_carry_the_manager_rule(self):
        from workbench.backend.flow_tasks import MANAGER_RULE
        work = self.running_work()
        self.assertEqual(work["manager_rule"], MANAGER_RULE)
        follow_up = self.to_worker({"kind": "work", "message": "also rename it", "task_id": work["task_id"]})
        self.assertEqual((follow_up["status"], follow_up["manager_rule"]), ("queued", MANAGER_RULE), follow_up)
        self.assertEqual(self.to_manager({"kind": "done", "message": "done", "task_id": work["task_id"]})["status"],
                         "queued")
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "closed"))
        task_id = self.finished_experiment()
        rerun = self.to_worker({"kind": "experiment", "message": "again", "task_id": task_id, "run": True})
        self.assertEqual((rerun["status"], rerun["manager_rule"]), ("dispatched", MANAGER_RULE), rerun)
        self.assertIn("do not do it yourself", MANAGER_RULE)


class BackendRoutingTests(unittest.TestCase):
    def test_terminal_requests_go_to_the_terminal_service_and_the_rest_to_handoffs(self):
        seen = []
        backend = Backend.__new__(Backend)
        backend.handoffs = SimpleNamespace(handle=lambda role, request: seen.append(("handoffs", role)) or {"h": 1})
        backend.terminal = SimpleNamespace(handle=lambda role, request: seen.append(("terminal", role)) or {"t": 1})
        peer = SimpleNamespace(role=ActorRole.WORKER)
        self.assertEqual(backend._tool_request(peer, {"tool": "terminal"}), {"t": 1})
        self.assertEqual(backend._tool_request(peer, {"tool": "to_manager"}), {"h": 1})
        self.assertEqual(seen, [("terminal", ActorRole.WORKER), ("handoffs", ActorRole.WORKER)])
        backend.terminal = None
        self.assertEqual(backend._tool_request(peer, {"tool": "terminal"}),
                         {"status": "rejected", "reason": "terminal_unavailable"})


if __name__ == "__main__":
    unittest.main()
