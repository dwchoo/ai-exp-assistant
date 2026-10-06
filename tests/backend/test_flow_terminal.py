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

from unittest import mock

from workbench.backend import flow_terminal
from workbench.backend.flow import HandoffService
from workbench.backend.flow_terminal import (
    TAIL_BYTES, TAIL_LINES, WAIT_SECONDS, HostGate, TerminalService, output_tail,
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
    # "wait" is test-only: the service's fixed wait (C-D68 (9)) for this call, never sent to the tool.
    args = {k: v for k, v in args.items() if k != "wait"} if isinstance(args, dict) else args
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
        self.terminal._wait_seconds = args.get("wait", 30) if isinstance(args, dict) else 30
        return self.terminal.handle(role, call(args, **kwargs))


class RefusalTests(ServiceFixture):
    def assert_nothing_typed(self):
        self.assertEqual(self.port.sent, [], "a refusal never types into the host shell")
        self.assertEqual(self.port.holds, [])
        self.assertIsNone(self.gate.owner)

    def test_only_the_worker_may_use_the_tool(self):
        result = self.run_tool({"command": "true"}, role=ActorRole.MANAGER)
        self.assertEqual((result["status"], result["reason"]), ("rejected", "tool_not_allowed_for_role"))
        self.assert_nothing_typed()

    def test_invalid_arguments_name_the_field(self):
        for args, field in (({"command": "ls", "timeout_seconds": 60}, "timeout_seconds"),
                            ({"command": "ls", "timeout_seconds": None}, "timeout_seconds"),
                            ({"command": 5}, "command"),
                            ({"command": "ls", "cwd": "/"}, "cwd")):
            result = self.run_tool(args)
            self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), args)
            self.assertTrue(any(error.startswith(field) for error in result["errors"]), result)
        self.assert_nothing_typed()

    def test_a_command_outside_the_task_commands_is_refused_and_nothing_is_typed(self):
        # C-D69 (6)(b): while the worker's active Task lists commands, only those run
        allowed = ["echo one", "printf 'two\\n' | tee /tmp/x"]
        self.terminal._task_commands = lambda: allowed
        for command in ("echo other", "echo one; echo extra", "echo  one"):
            with self.subTest(command=command):
                result = self.run_tool({"command": command})
                self.assertEqual((result["status"], result["reason"]), ("not_in_task_commands", "not_in_task_commands"))
                self.assertEqual(result["allowed_commands"], allowed)
                self.assertIn("1. echo one", result["detail"])
                self.assertIn("2. printf 'two\\n' | tee /tmp/x", result["detail"])
                self.assertIn("Nothing was typed", result["detail"])
                self.assert_nothing_typed()
        fetched = self.run_tool({"command": None})  # the state fetch is not restricted
        self.assertNotEqual(fetched.get("status"), "not_in_task_commands", fetched)
        self.terminal._task_commands = lambda: None  # no Task (or no commands): no restriction
        self.assertNotEqual(self.run_tool({"command": "echo other"})["status"], "not_in_task_commands")
        refused = [r for r in journal(self.root) if r["type"] == "terminal_refused"]
        self.assertEqual([r["status"] for r in refused if r["status"] == "not_in_task_commands"],
                         ["not_in_task_commands"] * 3)

    def test_the_size_check_matches_the_dispatch_encoding(self):
        executable = "/usr/bin/bash"
        room = flow_terminal.command_room(executable)
        self.assertGreater(room, 2500)
        self.assertLessEqual(flow_terminal.command_request_bytes(executable, "a" * room), 4096)
        self.assertGreater(flow_terminal.command_request_bytes(executable, "a" * (room + 1)), 4096)

    def test_a_start_failure_whose_return_is_not_confirmed_never_claims_the_terminal_is_the_users(self):
        class StuckPort(FakeHostPort):
            choice = SimpleNamespace(kind="bash", executable="/usr/bin/bash")

            def cwd(self):
                return "/tmp"

            def poll(self, timeout=0):
                return {"parent_mode": "control_wait", "input_owner": "manager"}

            def claim_manager(self):
                pass

            def display_bytes(self):
                return b""

            def snapshot(self):
                return {"parent_pid": 1, "generation": 1, "owner_epoch": 1, "input_owner": "manager",
                        "parent_mode": "control_wait",
                        "lifecycle": {"request_id": "r", "unknown": ["request_write_failure"]}}

            def submit(self, *args, **kwargs):
                raise OSError("partial managed request")

            def request_takeover(self):
                raise AssertionError("an unknown lifecycle is never taken over")

        self.port = StuckPort()
        with mock.patch.object(flow_terminal, "PREPARE_WAIT", 0.2), \
                mock.patch.object(flow_terminal, "RETURN_WAIT", 0.3, create=True):
            result = self.run_tool({"command": "true"})
        self.assertEqual(result["status"], "start_failed", result)
        self.assertNotIn("user's again", result["detail"])
        self.assertIn("could not confirm", result["detail"])
        self.assertIn("prefix t", result["detail"])
        self.assertEqual(result["host_terminal"], {"input_owner": "manager", "parent_mode": "control_wait"})
        failed = next(r for r in journal(self.root) if r["type"] == "terminal_start_failed")
        self.assertIs(failed["returned_to_user"], False)

    def test_the_wait_is_fixed_at_120_seconds(self):
        # C-D68 (9) user decision 2026-10-05: "120초 고정"; the worker sets no wait.
        self.assertEqual(WAIT_SECONDS, 120)
        self.assertEqual(validate_terminal_arguments({"command": "ls"}), [])
        errors = validate_terminal_arguments({"command": "ls", "timeout_seconds": 1800})
        self.assertEqual(errors, ["timeout_seconds: unknown field; only command"])
        fresh = self.make_terminal(lambda: self.port)
        self.assertEqual(fresh._wait_seconds, 120)
        fresh.close()
        self.paused = True  # nothing runs; the journaled request shows the wait used
        self.terminal.handle(ActorRole.WORKER, call({"command": "true"}))
        request = next(r for r in journal(self.root) if r["type"] == "terminal_request")
        self.assertEqual(request["wait_seconds"], 120)
        self.assertNotIn("timeout_seconds", request)

    def test_a_command_with_an_environment_value_is_refused_and_never_journaled(self):
        result = self.run_tool({"command": f"echo {SECRET}"})
        self.assertEqual((result["status"], result["reason"]), ("rejected", "environment_value"))
        self.assertNotIn(SECRET, (self.root / "workflow" / "handoffs.jsonl").read_text())
        self.assert_nothing_typed()

    def test_paused_refuses_a_new_command(self):
        self.paused = True
        result = self.run_tool({"command": "true"})
        self.assertEqual(result["status"], "paused")
        self.assert_nothing_typed()

    def test_busy_typing_or_missing_host_shell_is_host_terminal_busy(self):
        for reason in ("a line is being typed in the host shell", "the host shell has jobs",
                       "manager owns the host shell", "host shell has exited"):
            self.port.busy_reason = reason
            result = self.run_tool({"command": "true"})
            self.assertEqual((result["status"], result["reason"]), ("host_terminal_busy", reason))
            self.assert_nothing_typed()
        self.port.busy_reason = None
        missing = self.make_terminal(lambda: None)
        result = missing.handle(ActorRole.WORKER, call({"command": "true"}))
        self.assertEqual(result["status"], "host_terminal_busy")
        self.assert_nothing_typed()

    def test_an_active_or_starting_experiment_is_host_terminal_busy(self):
        self.activity = "an experiment run is starting"
        result = self.run_tool({"command": "true"})
        self.assertEqual((result["status"], result["reason"]), ("host_terminal_busy", "an experiment run is starting"))
        self.activity = None
        self.assertIsNone(self.gate.acquire("experiment"))
        result = self.run_tool({"command": "true"})
        self.assertEqual(result["status"], "host_terminal_busy")
        self.assertIn("experiment", result["reason"])
        self.gate.release("experiment")
        self.assert_nothing_typed()

    def test_wait_without_a_command_and_duplicate_calls(self):
        result = self.run_tool({"command": None})
        self.assertEqual((result["status"], result["reason"]), ("rejected", "no_terminal_command"))
        first = self.run_tool({"command": None}, call_id="same")
        again = self.run_tool({"command": None}, call_id="same")
        self.assertEqual(first, again)
        self.assertTrue(any(r["type"] == "terminal_duplicate" for r in journal(self.root)))

    def test_refusals_are_journaled_with_their_reason(self):
        self.port.busy_reason = "the host shell has jobs"
        self.run_tool({"command": "make test"})
        records = journal(self.root)
        request = next(r for r in records if r["type"] == "terminal_request")
        self.assertEqual((request["command"], request["wait_seconds"]), ("make test", 30))
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

    CHOICE = ShellChoice("bash", "/usr/bin/bash")

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.pane = ShellPane(self.CHOICE,
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
                                "wait": 30})
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

    def test_the_host_pane_shows_the_worker_command_without_changing_how_it_runs(self):
        # p27-cd68-fix-03 (smoke-01 P1): the user sees which command the worker runs, before its output.
        command = "echo \"args=$# zero=${0##*/}\"; echo 'quote \"ok\"'; exit 4"
        result = self.run_tool({"command": command, "wait": 30})
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 4), result)
        self.assertIn("args=0 zero=bash", result["output_tail"], "the same as <shell> -c <command>")
        self.assertIn("quote \"ok\"", result["output_tail"])
        self.assertNotIn("[worker] $", result["output_tail"], "the worker gets the output, not its own command")
        self.assertNotIn(b"[worker] $", Path(result["log_path"]).read_bytes())
        shown = bytes(self.ui).decode("utf-8", "replace")
        self.assertIn(f"[worker] $ {command}", shown)
        self.assertLess(shown.index(f"[worker] $ {command}"), shown.index("args=0"), "the command line comes first")
        self.assertEqual(shown.count(f"[worker] $ {command}"), 1)
        self.assert_user_owns_the_shell_again()
        self.user_types(b"history | tail -3\r")
        self.assertTrue(wait_until(lambda: b"history | tail -3" in bytes(self.ui)[len(shown.encode()):], 5))
        time.sleep(0.3)
        self.assertNotIn(b"args=", bytes(self.ui)[len(shown.encode()):], "nothing went into the parent's history")

    def test_a_long_multi_line_script_runs_from_a_script_file(self):
        # C-D69 (6)(c): too long for one host shell request -> the harness writes it to a script and runs it
        body = "\n".join(f"echo 'line {index:03d} 한글 \"quoted\" $((index={index}))' > /dev/null" for index in range(120))
        script = ("printf 'spill-start\\n'\n" + body + "\ncat <<'EOF'\nheredoc $HOME stays literal\nEOF\n"
                  "printf 'spill-done %s\\n' \"$WB_FIXTURE_NAME\"\nexit 6\n")
        self.assertGreater(flow_terminal.command_request_bytes("/usr/bin/bash", script), 4096)
        before = len(self.ui)
        result = self.run_tool({"command": script, "wait": 30})
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 6), result)
        self.assertIn("spill-start", result["output_tail"])
        self.assertIn("heredoc $HOME stays literal", result["output_tail"])
        self.assertIn("spill-done exported-by-user", result["output_tail"])
        script_path = Path(result["script_path"])
        self.assertEqual(script_path.parent, self.root / "workflow" / "terminal")
        self.assertEqual(stat.S_IMODE(script_path.stat().st_mode), 0o600)
        self.assertEqual(script_path.read_text(), script)
        shown = bytes(self.ui)[before:].decode("utf-8", "replace")
        self.assertIn(f"[worker] $ printf 'spill-start\\n' … (script {script_path})", shown)
        self.assertNotIn("line 050", shown, "the script text is not typed or echoed")
        log = Path(result["log_path"]).read_bytes()
        self.assertNotIn(b"[worker] $", log, "the shown line is not command output")
        self.assertIn(b"spill-done", log)
        started = next(r for r in journal(self.root) if r["type"] == "terminal_started")
        self.assertEqual((started["command"], started["script_path"]), (script, str(script_path)))
        self.assert_user_owns_the_shell_again()
        after = self.run_tool({"command": "echo next-ok", "wait": 30})
        self.assertEqual((after["status"], after["exit_code"]), ("exited", 0), after)
        self.assertNotIn("script_path", after)

    def test_only_the_task_commands_run_and_the_runs_are_listed_for_the_task(self):
        # C-D69 (6)(b)(d) on a real shell: a refused command types nothing; the runs are kept per Task
        long_command = "printf 'long-ok\\n'; : " + "w" * 4000
        self.terminal._task_commands = lambda: ["echo allowed-one", long_command]
        self.terminal._active_task = lambda: SimpleNamespace(task_id="task-cmds")
        before = len(self.ui)
        refused = self.run_tool({"command": "echo not-listed", "wait": 10})
        self.assertEqual(refused["status"], "not_in_task_commands", refused)
        time.sleep(0.3)
        self.assertNotIn(b"wb-handoff", bytes(self.ui)[before:], "nothing was typed")
        self.assertNotIn(b"not-listed", bytes(self.ui)[before:])
        first = self.run_tool({"command": "echo allowed-one\n", "wait": 30})  # trailing whitespace only
        self.assertEqual((first["status"], first["exit_code"]), ("exited", 0), first)
        second = self.run_tool({"command": long_command, "wait": 30})
        self.assertEqual((second["status"], second["exit_code"]), ("exited", 0), second)
        self.assertIn("long-ok", second["output_tail"])
        runs = self.terminal.runs_for_task("task-cmds")
        self.assertEqual([r["command"] for r in runs], ["echo allowed-one", long_command[:200] + " …"])
        self.assertEqual([(r["status"], r["exit_code"]) for r in runs], [("exited", 0), ("exited", 0)])
        self.assertEqual(runs[0]["log_path"], first["log_path"])
        self.assertEqual(runs[1]["script_path"], second["script_path"])
        self.assertIsInstance(runs[0]["duration_seconds"], float)
        self.assertEqual(self.terminal.runs_for_task("other-task"), [])
        self.assert_user_owns_the_shell_again()

    def test_a_start_failure_after_the_handoff_gives_the_shell_back_to_the_user(self):
        # C-D69 (5)(a): the request is refused by the shell after wb-handoff and the manager claim
        with mock.patch.object(flow_terminal, "command_request_bytes", lambda executable, command: 0):
            result = self.run_tool({"command": "echo " + "z" * 5053, "wait": 10})
        self.assertEqual(result["status"], "start_failed", result)
        self.assertIn("pipe atomic write", result["reason"])
        self.assertIn("the host terminal is the user's again", result["detail"])
        self.assertEqual(result["host_terminal"], {"input_owner": "user", "parent_mode": "manual_prompt"})
        self.assert_user_owns_the_shell_again()
        after = self.run_tool({"command": "echo after-failure", "wait": 30})
        self.assertEqual((after["status"], after["exit_code"]), ("exited", 0), after)
        self.assert_user_owns_the_shell_again()
        failed = next(r for r in journal(self.root) if r["type"] == "terminal_start_failed")
        self.assertIs(failed["returned_to_user"], True)

    def test_a_late_control_wait_after_a_failed_claim_still_returns_the_shell(self):
        # p27-cd69-stuck-review-01 P3-2: wb-handoff typed, the claim came before the control wait (slow shell)
        with mock.patch.object(flow_terminal, "PREPARE_WAIT", 0.0):
            result = self.run_tool({"command": "echo never-runs", "wait": 10})
        self.assertEqual(result["status"], "start_failed", result)
        self.assertIn("user's again", result["detail"])
        self.assertEqual(result["host_terminal"], {"input_owner": "user", "parent_mode": "manual_prompt"})
        self.assert_user_owns_the_shell_again()
        after = self.run_tool({"command": "echo after-late-wait", "wait": 30})
        self.assertEqual((after["status"], after["exit_code"]), ("exited", 0), after)

    def test_typing_user_refuses_without_typing(self):
        self.user_types(b"echo half-typed")
        self.assertTrue(wait_until(lambda: not self.idle(), 5))
        before = bytes(self.ui)
        result = self.run_tool({"command": "echo never-run", "wait": 5})
        self.assertEqual(result["status"], "host_terminal_busy", result)
        time.sleep(0.3)
        self.assertNotIn(b"never-run", bytes(self.ui)[len(before):])
        self.assertIsNone(self.pane.automation_hold)
        self.user_types(b"\x15")  # the user clears the line

    def test_timeout_then_wait_again_and_one_command_at_a_time(self):
        flag = self.root / "go"
        first = self.run_tool({"command": f"echo started; while [ ! -e {flag} ]; do sleep 0.05; done; echo late-done",
                               "wait": 1})
        self.assertEqual(first["status"], "running", first)
        self.assertIn("started", first["output_tail"])
        self.assertIn("End your turn", first["detail"])  # C-D68 (8): no re-wait loop
        self.assertNotIn("command null", first["detail"])
        second = self.run_tool({"command": "echo other", "wait": 5})
        self.assertEqual(second["status"], "terminal_command_running", second)
        self.assertEqual(second["command_id"], first["command_id"])
        self.assertIsNotNone(self.gate.acquire("experiment"), "an experiment sees the host busy meanwhile")
        self.assertEqual(self.gate.owner, "terminal")
        flag.touch()
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        waited = self.run_tool({"command": None})  # C-D68 (10): returns the finished result at once
        self.assertEqual((waited["status"], waited["exit_code"], waited["command_id"]),
                         ("exited", 0, first["command_id"]), waited)
        self.assertIn("late-done", waited["output_tail"])
        self.assert_user_owns_the_shell_again()
        self.assertNotIn(b"other", Path(waited["log_path"]).read_bytes())

    def test_abandoned_wait_lets_the_command_finish_and_free_the_shell(self):
        # The bridge answers an aborted tool call itself; the backend keeps following the command.
        result = self.run_tool({"command": "sleep 0.5; echo finished-alone", "wait": 1})
        self.assertIn(result["status"], ("running", "exited"))
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        self.assertEqual(self.terminal.current()["status"], "exited")
        self.assert_user_owns_the_shell_again()

    def test_pause_refuses_new_commands_while_the_running_one_continues(self):
        flag = self.root / "go"
        first = self.run_tool({"command": f"while [ ! -e {flag} ]; do sleep 0.05; done; echo resumed-output",
                               "wait": 1})
        self.assertEqual(first["status"], "running", first)
        self.paused = True
        refused = self.run_tool({"command": "echo while-paused", "wait": 5})
        self.assertEqual(refused["status"], "terminal_command_running")
        still = self.run_tool({"command": None})  # allowed while paused; never waits
        self.assertEqual(still["status"], "running", still)
        flag.touch()
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        waited = self.run_tool({"command": None})
        self.assertEqual((waited["status"], waited["exit_code"]), ("exited", 0), waited)
        self.assertIn("resumed-output", waited["output_tail"])
        self.assert_user_owns_the_shell_again()
        paused = self.run_tool({"command": "echo while-paused", "wait": 5})
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
            result = self.run_tool({"command": "pwd -P; cd / && pwd -P", "wait": 30})
            self.assertEqual((result["status"], result["exit_code"], result["cwd"]), ("exited", 0, str(place)),
                             result)
            self.assertEqual(result["output_tail"].splitlines()[0], str(place), result["output_tail"])
            self.assert_user_owns_the_shell_again(cwd=place)  # a cd inside the command stays in the child
            self.assertNotIn(b"cd --", bytes(self.ui)[before:], "nothing changes the parent's directory")
        ended = [r for r in journal(self.root) if r["type"] == "terminal_ended"]
        self.assertEqual([r["cwd"] for r in ended], [str(elsewhere), str(self.project), str(self.home)])


class DashHostShellTerminalTests(RealHostShellTerminalTests):
    """C-D69 (6) on dash: the script-file run and the Task command restriction."""

    CHOICE = ShellChoice("sh", "/usr/bin/dash")
    KEPT = ("test_only_the_task_commands_run_and_the_runs_are_listed_for_the_task",)

    def test_a_long_multi_line_script_runs_from_a_script_file_on_dash(self):
        script = "printf 'dash-start\\n'\n" + "\n".join(f": line {index:04d} {'z' * 40}" for index in range(120)) \
            + "\nprintf 'dash-done\\n'\nexit 9\n"
        result = self.run_tool({"command": script, "wait": 30})
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 9), result)
        self.assertIn("dash-done", result["output_tail"])
        self.assertIn(f"[worker] $ printf 'dash-start\\n' … (script {result['script_path']})",
                      bytes(self.ui).decode("utf-8", "replace"))
        self.assert_user_owns_the_shell_again()


for _name in [name for name in dir(RealHostShellTerminalTests) if name.startswith("test_")]:
    if _name not in DashHostShellTerminalTests.KEPT:
        setattr(DashHostShellTerminalTests, _name, None)  # bash-specific expectations stay with the bash class


class CommandRecordCorrectionTests(ServiceFixture):
    """p27-cd69-cmds-fix-01: light run records, the report list after a restart, the exact match, the spill cap."""

    def started(self, command_id, task_id, command="echo x", **extra):
        self.handoffs.record({"type": "terminal_started", "command_id": command_id, "task_id": task_id,
                              "command": command, "log_path": f"/logs/{command_id}.log", **extra})

    def ended(self, command_id, status="exited", exit_code=0):
        self.handoffs.record({"type": "terminal_ended", "command_id": command_id, "status": status,
                              "exit_code": exit_code, "signal": None, "duration_seconds": 0.5})

    def test_a_finished_run_keeps_a_light_record_not_the_command_object(self):
        import gc
        import weakref
        command = flow_terminal._Command("cid-1", "echo " + "q" * 3000, {}, "task-light", Path("/logs/cid-1.log"))
        command.recent.extend(b"o" * 30000)
        command.since.extend(b"s" * 30000)
        command.unseen.extend(b"u" * 30000)
        command.script_path = Path("/logs/cid-1.sh")
        ref = weakref.ref(command)
        self.terminal._remember_run(command)
        self.terminal._finish_run(command, {"status": "exited", "exit_code": 3, "signal": None,
                                            "duration_seconds": 1.5})
        del command
        gc.collect()
        self.assertIsNone(ref(), "the Task's run record must not keep the command (and its output buffers)")
        (_, entry), = self.terminal._task_runs["task-light"].runs
        self.assertEqual((entry["status"], entry["exit_code"], entry["duration_seconds"]), ("exited", 3, 1.5))
        self.assertEqual(entry["script_path"], "/logs/cid-1.sh")
        self.assertLessEqual(len(entry["command"]), 210, "the command is shortened")

    def test_runs_are_rebuilt_from_the_journal_after_a_restart(self):
        self.started("c1", "T", "echo one")
        self.ended("c1")
        self.started("c2", "T", "printf 'two\\n'\nmore", script_path="/logs/c2.sh")
        self.ended("c2", "exited", 7)
        self.started("c3", "T", "sleep 99")  # no end record: the backend restarted while it ran
        self.started("c4", "OTHER", "echo other")
        self.ended("c4")
        runs = self.terminal.runs_for_task("T")  # this service never saw them (a restarted backend)
        self.assertEqual([(r["command"], r["status"], r["exit_code"]) for r in runs],
                         [("echo one", "exited", 0), ("printf 'two\\n' …", "exited", 7), ("sleep 99", "unknown", None)])
        self.assertEqual(runs[1]["script_path"], "/logs/c2.sh")
        self.assertEqual(runs[0]["log_path"], "/logs/c1.log")
        self.assertTrue(any("rebuilt from the terminal journal" in note and "restart" in note for note in runs.notes),
                        runs.notes)
        self.assertEqual(self.terminal.runs_for_task("NONE"), [])
        self.assertEqual(self.terminal.runs_for_task("NONE").notes, [], "nothing ran and the journal says so")

    def test_an_unreadable_journal_is_said_not_shown_as_an_empty_list(self):
        def broken(types):
            raise OSError("journal unreadable")
        self.terminal._handoffs = SimpleNamespace(read_records=broken, record=lambda record: {})
        runs = self.terminal.runs_for_task("T")
        self.assertEqual(runs, [])
        self.assertTrue(any("could not be read" in note and "does not show that nothing ran" in note
                            for note in runs.notes), runs.notes)

    def test_older_runs_past_the_cap_are_counted(self):
        kept = flow_terminal.RUNS_KEPT
        for index in range(kept + 6):
            self.started(f"id-{index:03d}", "MANY", f"echo run-{index}")
            self.ended(f"id-{index:03d}")
        runs = self.terminal.runs_for_task("MANY")
        self.assertEqual(len(runs), kept)
        self.assertEqual(runs[0]["command"], "echo run-6", "the most recent runs are listed")
        self.assertEqual(runs[-1]["command"], f"echo run-{kept + 5}")
        self.assertTrue(any(f"6 older runs omitted" in note for note in runs.notes), runs.notes)

    def test_older_runs_past_the_cap_are_counted_from_memory_when_the_journal_is_unreadable(self):
        self.terminal._handoffs = SimpleNamespace(record=lambda record: {})  # no read_records
        kept = flow_terminal.RUNS_KEPT
        for index in range(kept + 6):
            command = flow_terminal._Command(f"id-{index}", f"echo run-{index}", {}, "MEM", Path(f"/logs/{index}.log"))
            self.terminal._remember_run(command)
            self.terminal._finish_run(command, {"status": "exited", "exit_code": 0, "duration_seconds": 0.1})
        runs = self.terminal.runs_for_task("MEM")
        self.assertEqual(len(runs), kept)
        self.assertTrue(any("6 older runs omitted" in note for note in runs.notes), runs.notes)
        self.assertTrue(any("could not be read" in note for note in runs.notes), runs.notes)

    def test_the_exact_match_ignores_only_trailing_space_tab_and_newline(self):
        self.terminal._task_commands = lambda: ["echo a", "echo b \t"]
        for fine in ("echo a", "echo a \t\n\n", "echo b", "echo b\n"):
            with self.subTest(fine=repr(fine)):
                self.assertIsNone(self.terminal._not_in_task_commands(fine, {}))
        for bad in ("echo a\r", "echo a\x0b", "echo a\x0c", "echo a\x1c", "echo a\x1f", "echo a\x85", "echo a\xa0",
                    "echo a\u2028", "echo a\u3000", "echo a\r\n", "echo a \x0b", " echo a", "echo a;"):
            with self.subTest(bad=repr(bad)):
                result = self.run_tool({"command": bad, "wait": 5})
                self.assertEqual(result["status"], "not_in_task_commands", result)
        self.assertEqual(self.port.sent, [], "nothing was typed")

    def test_the_script_request_never_exceeds_the_request_limit(self):
        from workbench.terminal.shell_g2.lifecycle import RUN_REQUEST_MAX, encode_run, normalize_run
        executable = "/usr/bin/bash"
        script = Path("/tmp") / ("d" * 200) / ("e" * 200) / ("f" * 200) / ("g" * 200) / ("h" * 200) / "0123.sh"

        def size(shown):
            argv = [executable, "-c", flow_terminal.SPILL_SCRIPT, executable, shown, str(script)]
            return len(f"RUN:{encode_run(normalize_run(executable, argv, flow_terminal._SIZING_ID))}\n".encode())

        command = "\U0001F642" * 300 + "\nsecond line"
        plain = f"{flow_terminal.shown_command(command)} (script {script})"
        self.assertGreater(size(plain), RUN_REQUEST_MAX, "without the cap this request would be refused")
        shown = flow_terminal.spill_shown(executable, command, script)
        self.assertLessEqual(size(shown), RUN_REQUEST_MAX)
        self.assertTrue(shown.endswith(f"(script {script})"))
        self.assertTrue(shown.startswith("\U0001F642"), "the first line is cut, not dropped")
        short = flow_terminal.spill_shown(executable, "echo hi\nmore", Path("/tmp/x/a.sh"))
        self.assertEqual(short, "echo hi … (script /tmp/x/a.sh)", "a normal command is shown as before")


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

    def test_the_flow_s_task_commands_restrict_the_terminal_and_feed_the_done_report(self):
        # C-D69 (6): the backend wiring (service.py) on the real TaskFlow and TerminalService
        terminal = TerminalService(handoffs=self.service, host_shell=lambda: FakeHostPort(), gate=self.gate,
                                   log_root=self.root / "terminal", automation=lambda: AUTOMATION,
                                   activity=self.flow.experiment_host_activity, active_task=self.flow.active_task,
                                   task_commands=self.flow.active_commands)
        self.addCleanup(terminal.close)
        self.flow.terminal_runs = terminal.runs_for_task
        result = self.to_worker({"kind": "work", "message": "facts", "spec": {"goal": "g", "paths": []},
                                 "commands": ["echo listed"]})
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        refused = terminal.handle(ActorRole.WORKER, call({"command": "echo mine"}))
        self.assertEqual(refused["status"], "not_in_task_commands", refused)
        listed = terminal.handle(ActorRole.WORKER, call({"command": "echo listed"}))
        self.assertNotEqual(listed["status"], "not_in_task_commands", listed)
        self.to_manager({"kind": "done", "message": "done"})
        self.assertTrue(wait_until(lambda: self.flow.worker_view()["state"] == "idle"))
        report = [m.payload for m in self.mailbox.created if m.payload.get("handoff") == "to_manager"][-1]
        self.assertEqual(report["commands_run"], [], "nothing ran (the fake shell cannot start a command)")
        self.assertIsNone(self.flow.active_commands())
        after = terminal.handle(ActorRole.WORKER, call({"command": "echo mine"}))
        self.assertNotEqual(after["status"], "not_in_task_commands", "no Task: no restriction")
        source = Path(flow_terminal.__file__).with_name("service.py").read_text()
        self.assertIn("task_commands=self.flow.active_commands", source)
        self.assertIn("self.flow.terminal_runs = self.terminal.runs_for_task", source)

    def test_an_experiment_starting_or_running_refuses_the_terminal(self):
        terminal = TerminalService(handoffs=self.service, host_shell=lambda: FakeHostPort(), gate=self.gate,
                                   log_root=self.root / "terminal",
                                   automation=lambda: AUTOMATION, activity=self.flow.experiment_host_activity)
        self.gates.exit.clear()  # the experiment's host command keeps running
        result = self.new_experiment()
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        refused = terminal.handle(ActorRole.WORKER, call({"command": "true", "wait": 1}))
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
