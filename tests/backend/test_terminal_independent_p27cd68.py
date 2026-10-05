"""C-D68 independent tests (p27-cd68-test-01) for the worker's ``terminal`` tool on a REAL host shell.

Expectations come from DECISIONS.md C-D68 (1), (7), (8) and C-D65 (2), not from flow_terminal.py:

- (1)/C-D65 (2): a command runs only when the host terminal is the user's clean prompt with no job and no typed
  line; otherwise nothing is typed and ``host_terminal_busy`` comes back. It runs visibly (the pane shows the
  output) and returns the exit code and the end of the output (plus the log file).
- (7): it runs in the host terminal's current directory (where the user last cd'd); the parent shell's directory
  and environment are not changed; usable without a Task; paused -> only a NEW command is refused (a running one
  continues and may still be waited for); wait fixed at 120 s (C-D68 (9) replaced the 1800 s maximum; timeout_seconds is invalid); the command outlives the wait.
- (8): "running" after the wait; while it runs a check every 60 s (merged into the latest while the worker is
  busy, none while paused); when it ends and no waiting call received it, ONE completion notice (also after an
  aborted wait); paused -> delivered at the resume.
- Experiment runs and terminal commands never own the host shell together (HostGate), including a start race.
- Journal: no command output and no environment value.

Real ShellPane + HostShellPort (bash), a recording notice port, a fake clock where 60 s must pass. No OMP, no model.
"""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))

from workbench.backend.flow import HandoffService  # noqa: E402
from workbench.backend.flow_terminal import HostGate, TerminalService  # noqa: E402
from workbench.backend.panes import HostShellPort, ShellPane  # noqa: E402
from workbench.contracts.v1 import ActorRole  # noqa: E402
from workbench.terminal.shell_g2.prototype import ShellChoice  # noqa: E402

import test_task_flow as flow_fixtures  # noqa: E402

SESSION = str(uuid4())
AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}
SECRET_VALUE = "cd68-secret-value-7f3a"  # exported in the user's shell as WB_CD68_TOKEN


def wait_until(predicate, timeout=8.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def call(args, call_id=None):
    return {"request_id": str(uuid4()), "tool_call_id": call_id or f"c-{uuid4().hex[:10]}", "tool": "terminal",
            "args": args, "session_id": SESSION, "generation": 1}


class FakeClock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


class NoticePort:
    """Records every notice; ``outcome`` is what the worker side answers (delivered/deferred/paused/...)."""

    def __init__(self):
        self.outcome = "delivered"
        self.attempts: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, notice):
        with self.lock:
            self.attempts.append(dict(notice))
            return self.outcome

    def delivered(self, kind):
        with self.lock:
            return [n for n, o in zip(self.attempts, self._outcomes) if n["type"] == kind and o == "delivered"]

    def of(self, kind):
        with self.lock:
            return [n for n in self.attempts if n["type"] == kind]


class RecordingNotices(NoticePort):
    def __init__(self):
        super().__init__()
        self.sent: list[dict] = []

    def __call__(self, notice):
        with self.lock:
            self.attempts.append(dict(notice))
            if self.outcome == "delivered":
                self.sent.append(dict(notice))
            return self.outcome

    def sent_of(self, kind):
        with self.lock:
            return [n for n in self.sent if n["type"] == kind]


class RealShellFixture(unittest.TestCase):
    clock_driven = False  # True: no notifier thread; the test drives tick() with a fake clock

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p27cd68-term-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "workflow").mkdir(mode=0o700)
        self.home = self.root / "home"
        self.home.mkdir()
        self.paused = False
        self.activity = None
        self.gate = HostGate()
        self.notices = RecordingNotices()
        self.clock = FakeClock()
        self.handoffs = HandoffService(self.root / "workflow" / "handoffs.jsonl",
                                       mailbox=flow_fixtures.FakeMailbox())
        self.pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8",
                               "WB_CD68_TOKEN": SECRET_VALUE, "WB_KEEP": "kept"})
        self.ui = bytearray()
        self.ui_lock = threading.Lock()
        self.stop = threading.Event()
        self.loop = threading.Thread(target=self._pump, daemon=True)
        self.loop.start()
        kwargs = {"clock": self.clock} if self.clock_driven else {"check_interval": 3600}
        self.terminal = TerminalService(
            handoffs=self.handoffs, host_shell=lambda: HostShellPort(self.pane, lambda: self.pane), gate=self.gate,
            log_root=self.root / "workflow" / "terminal", automation=lambda: AUTOMATION,
            paused=lambda: self.paused, activity=lambda: self.activity,
            sensitive_values=lambda: (SECRET_VALUE,), poll_interval=0.02, notify=self.notices,
            notice_retry=0.0 if self.clock_driven else 0.05, tick_interval=0.05, **kwargs)
        if not self.clock_driven:
            self.terminal.start()
        self.addCleanup(self._close)
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        self.user_cds(self.home)

    def _close(self):
        self.terminal.close()
        self.stop.set()
        self.loop.join(5)
        self.pane.close()
        self.handoffs.close()

    def _pump(self):
        while not self.stop.is_set():
            for chunk in self.pane.pump():
                with self.ui_lock:
                    self.ui.extend(chunk.data)
            time.sleep(0.01)

    def screen(self) -> bytes:
        with self.ui_lock:
            return bytes(self.ui)

    def idle(self) -> bool:
        port = HostShellPort(self.pane, lambda: self.pane)
        try:
            return port.busy() is None
        finally:
            port.detach()

    def user_types(self, data: bytes):
        self.assertIsNone(self.pane.admit(data))

    def user_cds(self, path: Path):
        self.user_types(f"cd {shlex.quote(str(path))}\r".encode())
        self.assertTrue(wait_until(lambda: self.pane.cwd() == str(path), 8))
        self.assertTrue(wait_until(self.idle, 8))

    def user_sees(self, script: str, marker: str) -> str:
        """The user runs ``script`` at the prompt; the line the shell printed after ``marker=``."""
        before = len(self.screen())
        self.user_types((script + "\r").encode())
        pattern = f"{marker}=".encode()
        self.assertTrue(wait_until(lambda: self.screen()[before:].count(pattern) >= 2, 8), self.screen()[before:])
        tail = self.screen()[before:].decode("utf-8", "replace").replace("\r", "")
        printed = [line for line in tail.split("\n") if line.startswith(f"{marker}=")]
        self.assertTrue(printed, tail)
        self.assertTrue(wait_until(self.idle, 8))
        return printed[-1][len(marker) + 1:]

    def tool(self, args, call_id=None, raw=False):
        # "wait" is test-only (C-D68 (9): the worker sets no wait): the service's fixed wait for this call. It is
        # never sent to the tool; raw=True sends args exactly as given (to check rejected fields).
        if not raw and isinstance(args, dict):
            args = dict(args)
            self.terminal._wait_seconds = args.pop("wait", None) or 30
        return self.terminal.handle(ActorRole.WORKER, call(args, call_id))

    def journal_text(self) -> str:
        path = self.root / "workflow" / "handoffs.jsonl"
        return path.read_text() if path.exists() else ""

    def journal(self) -> list[dict]:
        return [json.loads(line) for line in self.journal_text().splitlines() if line.strip()]

    def logs(self) -> list[Path]:
        directory = self.root / "workflow" / "terminal"
        return sorted(directory.glob("*.log")) if directory.exists() else []

    def assert_nothing_ran(self, marker: str, before: int):
        time.sleep(0.3)
        self.assertNotIn(marker.encode(), self.screen()[before:], "a refused command was typed into the shell")
        self.assertEqual(self.logs(), [], "a refused command got a log (it was started)")
        self.assertIsNone(self.terminal.current())


class IdleOnlyRefusalMatrix(RealShellFixture):
    def test_a_background_job_refuses_without_typing(self):
        self.user_types(b"sleep 30 &\r")
        self.assertTrue(wait_until(lambda: not self.idle(), 8), "the job did not make the shell busy")
        before = len(self.screen())
        result = self.tool({"command": "echo JOB-NEVER", "wait": 5})
        self.assertEqual(result["status"], "host_terminal_busy", result)
        self.assert_nothing_ran("JOB-NEVER", before)
        self.user_types(b"kill %1; wait\r")
        self.assertTrue(wait_until(self.idle, 10), "the shell did not become idle after the job ended")
        done = self.tool({"command": "echo JOB-AFTER", "wait": 20})
        self.assertEqual((done["status"], done["exit_code"]), ("exited", 0), done)

    def test_a_stopped_job_refuses_without_typing(self):
        self.user_types(b"sleep 30\r")
        self.assertTrue(wait_until(lambda: not self.idle(), 8))
        time.sleep(0.5)  # let sleep own the terminal's foreground group first
        self.user_types(b"\x1a")  # ^Z: a stopped job at the prompt
        self.assertTrue(wait_until(lambda: b"Stopped" in self.screen(), 8), self.screen()[-300:])
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 8))
        before = len(self.screen())
        result = self.tool({"command": "echo STOPPED-NEVER", "wait": 5})
        self.assertEqual(result["status"], "host_terminal_busy", result)
        self.assertIn("job", result["reason"], result)  # refused because of the stopped job, at a prompt
        self.assert_nothing_ran("STOPPED-NEVER", before)
        self.user_types(b"kill -9 %1\r")
        port = HostShellPort(self.pane, lambda: self.pane)
        self.addCleanup(port.detach)
        self.assertTrue(wait_until(self.idle, 10), f"still busy after the stopped job was killed: {port.busy()} "
                                                   f"{self.screen()[-400:]!r}")

    def test_a_foreground_program_refuses(self):
        self.user_types(b"sleep 30\r")
        self.assertTrue(wait_until(lambda: not self.idle(), 8))
        before = len(self.screen())
        result = self.tool({"command": "echo FG-NEVER", "wait": 5})
        self.assertEqual(result["status"], "host_terminal_busy", result)
        self.assert_nothing_ran("FG-NEVER", before)
        self.user_types(b"\x03")
        self.assertTrue(wait_until(self.idle, 10))

    def test_a_typed_line_refuses_and_the_users_line_is_untouched(self):
        self.user_types(b"echo half-typed-by-user")
        self.assertTrue(wait_until(lambda: not self.idle(), 8))
        before = len(self.screen())
        result = self.tool({"command": "echo TYPED-NEVER", "wait": 5})
        self.assertEqual(result["status"], "host_terminal_busy", result)
        self.assert_nothing_ran("TYPED-NEVER", before)
        self.user_types(b"\r")  # the user's own line still runs as typed
        self.assertTrue(wait_until(lambda: self.screen()[before:].count(b"half-typed-by-user") >= 1, 8))
        self.assertTrue(wait_until(self.idle, 8))

    def test_an_exited_shell_refuses(self):
        self.user_types(b"exit\r")
        self.assertTrue(wait_until(self.pane.exited, 8))
        result = self.tool({"command": "echo EXITED-NEVER", "wait": 5})
        self.assertEqual(result["status"], "host_terminal_busy", result)
        self.assertEqual(self.logs(), [])

    def test_an_active_experiment_refuses_and_so_does_a_starting_one(self):
        self.assertIsNone(self.gate.acquire("experiment"))
        before = len(self.screen())
        result = self.tool({"command": "echo EXP-NEVER", "wait": 5})
        self.assertEqual(result["status"], "host_terminal_busy", result)
        self.assert_nothing_ran("EXP-NEVER", before)
        self.gate.release("experiment")
        self.activity = "an experiment run is starting"
        result = self.tool({"command": "echo EXP-NEVER", "wait": 5})
        self.assertEqual(result["status"], "host_terminal_busy", result)
        self.assert_nothing_ran("EXP-NEVER", before)
        self.assertIsNone(self.gate.owner, "a refusal leaves the gate free")

    def test_paused_refuses_a_new_command_only(self):
        self.paused = True
        before = len(self.screen())
        result = self.tool({"command": "echo PAUSED-NEVER", "wait": 5})
        self.assertEqual(result["status"], "paused", result)
        self.assert_nothing_ran("PAUSED-NEVER", before)
        self.paused = False
        ok = self.tool({"command": "echo PAUSED-AFTER", "wait": 20})
        self.assertEqual((ok["status"], ok["exit_code"]), ("exited", 0), ok)

    def test_wait_is_fixed_at_120_and_timeout_seconds_is_rejected(self):
        # C-D68 (9) replaces (7)'s "max 1800": the wait is fixed at 120 s; the worker supplies none, so
        # timeout_seconds (any value, null included) is invalid_arguments and nothing runs or is journaled as run.
        fresh = TerminalService(handoffs=self.handoffs, host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
                                gate=HostGate(), log_root=self.root / "workflow" / "terminal2",
                                automation=lambda: AUTOMATION, paused=lambda: True, check_interval=3600)
        self.addCleanup(fresh.close)
        out = fresh.handle(ActorRole.WORKER, call({"command": "true"}))  # paused: nothing runs, request journaled
        self.assertEqual(out["status"], "paused", out)
        waits = [r for r in self.journal() if r["type"] == "terminal_request"]
        self.assertEqual([r.get("wait_seconds") for r in waits], [120], "fixed 120 s wait")
        self.assertTrue(all("timeout_seconds" not in r for r in waits))
        before = len(self.screen())
        for bad in (1800, 5, None, 0, "60"):
            result = self.tool({"command": "echo TS-NEVER", "timeout_seconds": bad}, raw=True)
            self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), bad)
            self.assertIn("timeout_seconds", json.dumps(result))
        self.assert_nothing_ran("TS-NEVER", before)


class WhereAndWhat(RealShellFixture):
    def test_runs_where_the_user_cd_d_and_leaves_parent_cwd_and_env_alone(self):
        target = self.root / "user place"
        target.mkdir()
        self.user_cds(target)
        result = self.tool({"command": "pwd; cd /; export WB_CHILD_ONLY=leaked; unset WB_KEEP; echo moved-to-$PWD",
                           "wait": 20})
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 0), result)
        self.assertEqual(result["output_tail"].replace("\r", "").split("\n")[0], str(target))
        self.assertEqual(result.get("cwd"), str(target))
        self.assertTrue(wait_until(self.idle, 10), "the shell was not given back")
        seen = self.user_sees('echo "STATE=$PWD|${WB_CHILD_ONLY-unset}|${WB_KEEP-gone}"', "STATE")
        self.assertEqual(seen, f"{target}|unset|kept", "the parent shell's cwd/env changed")
        self.assertEqual(self.pane.cwd(), str(target))

    def test_exit_code_output_visible_in_pane_and_log(self):
        marker = f"visible-{uuid4().hex[:8]}"
        before = len(self.screen())
        result = self.tool({"command": f"echo {marker}-out; echo {marker}-err >&2; exit 7", "wait": 20})
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 7), result)
        self.assertIn(f"{marker}-out", result["output_tail"])
        self.assertIn(f"{marker}-err", result["output_tail"])
        self.assertTrue(wait_until(lambda: f"{marker}-out".encode() in self.screen()[before:]), "not shown to user")
        log = Path(result["log_path"]).read_bytes()
        self.assertIn(f"{marker}-out".encode(), log)
        self.assertIn(f"{marker}-err".encode(), log)
        self.assertTrue(wait_until(self.idle, 10))
        self.assertEqual(self.user_sees('echo "BACK=$?"', "BACK"), "0", "the user's prompt is clean again")

    def test_signal_exit_is_reported(self):
        result = self.tool({"command": "echo before-signal; kill -KILL $$", "wait": 20})
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(result["exit_code"], 137, result)
        self.assertEqual(result.get("signal"), 9, result)
        self.assertIn("before-signal", result["output_tail"])
        self.assertTrue(wait_until(self.idle, 10), "the shell was not given back after a signal")

    def test_no_task_needed_and_the_journal_has_no_output_or_env_value(self):
        marker = f"journal-out-{uuid4().hex[:8]}"
        result = self.tool({"command": f'echo {marker}; echo "tok=$WB_CD68_TOKEN"', "wait": 20})
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 0), result)
        self.assertIn(SECRET_VALUE, Path(result["log_path"]).read_text(), "the log holds the full output")
        text = self.journal_text()
        self.assertNotIn(marker + "\n", text)
        self.assertNotIn(SECRET_VALUE, text, "an environment value reached the journal")
        self.assertEqual(text.count(marker), text.count(f"echo {marker}"), "only the command text names the marker")
        started = next(r for r in self.journal() if r["type"] == "terminal_started")
        self.assertIsNone(started["task_id"])
        for record in self.journal():
            blob = json.dumps(record)
            for key in ('"output_tail":', '"new_output":', '"output":'):
                self.assertNotIn(key, blob, record)
        refused = self.tool({"command": f"echo {SECRET_VALUE}", "wait": 5})
        self.assertEqual(refused["status"], "rejected")
        self.assertNotIn(SECRET_VALUE, self.journal_text())


class CompletionNotice(RealShellFixture):
    """Real clock; checks pushed out of the way (interval 3600 s) so only the completion notice is in play."""

    def gated(self, name):
        flag = self.root / name
        return flag, f"echo started-{name}; while [ ! -e {shlex.quote(str(flag))} ]; do sleep 0.05; done; echo ended-{name}"

    def test_running_then_exactly_one_completion_notice(self):
        flag, command = self.gated("a")
        first = self.tool({"command": command, "wait": 1})
        self.assertEqual(first["status"], "running", first)
        self.assertEqual(self.notices.sent_of("terminal_done"), [])
        flag.touch()
        self.assertTrue(wait_until(lambda: self.notices.sent_of("terminal_done"), 10))
        time.sleep(0.6)
        done = self.notices.sent_of("terminal_done")
        self.assertEqual(len(done), 1, done)
        self.assertEqual((done[0]["command"], done[0]["exit_code"]), (command, 0))
        self.assertIn("ended-a", done[0]["output_tail"])
        self.assertEqual(done[0]["log_path"], first["log_path"])
        again = self.tool({"command": None, "wait": 5})  # the result can still be fetched
        self.assertEqual((again["status"], again["exit_code"]), ("exited", 0))
        time.sleep(0.3)
        self.assertEqual(len(self.notices.sent_of("terminal_done")), 1, "never twice")

    def test_a_waiting_call_that_gets_the_result_needs_no_notice(self):
        result = self.tool({"command": "sleep 0.3; echo quick", "wait": 20})
        self.assertEqual(result["status"], "exited")
        time.sleep(0.6)
        self.assertEqual(self.notices.sent_of("terminal_done"), [])

    def test_an_aborted_wait_still_gets_exactly_one_notice(self):
        flag, command = self.gated("b")
        out = {}
        waiter = threading.Thread(target=lambda: out.update(self.tool({"command": command, "wait": 30},
                                                                     call_id="abort-me")))
        waiter.start()
        self.assertTrue(wait_until(lambda: self.terminal.current() is not None, 10))
        # The bridge answered the aborted call itself and tells the backend (C-D68 (8)).
        ack = self.terminal.abandon(ActorRole.WORKER, {"request_id": str(uuid4()), "tool_call_id": "x",
                                                       "tool": "terminal_wait_abandoned",
                                                       "args": {"tool_call_id": "abort-me"},
                                                       "session_id": SESSION, "generation": 1})
        self.assertEqual(ack.get("status"), "recorded", ack)
        flag.touch()
        waiter.join(20)
        self.assertTrue(wait_until(lambda: self.notices.sent_of("terminal_done"), 10),
                        "the aborted call's result never reached the worker")
        time.sleep(0.6)
        self.assertEqual(len(self.notices.sent_of("terminal_done")), 1)

    def test_a_busy_worker_gets_it_later_once(self):
        flag, command = self.gated("c")
        self.assertEqual(self.tool({"command": command, "wait": 1})["status"], "running")
        self.notices.outcome = "deferred"
        flag.touch()
        self.assertTrue(wait_until(lambda: len(self.notices.of("terminal_done")) >= 2, 10), "not retried")
        self.assertEqual(self.notices.sent_of("terminal_done"), [])
        self.notices.outcome = "delivered"
        self.assertTrue(wait_until(lambda: self.notices.sent_of("terminal_done"), 10))
        time.sleep(0.5)
        self.assertEqual(len(self.notices.sent_of("terminal_done")), 1)

    def test_paused_holds_the_notice_and_the_resume_delivers_it(self):
        flag, command = self.gated("d")
        first = self.tool({"command": command, "wait": 1})
        self.assertEqual(first["status"], "running")
        self.paused = True
        refused = self.tool({"command": "echo NEW-WHILE-PAUSED", "wait": 5})
        self.assertIn(refused["status"], ("paused", "terminal_command_running"), refused)
        flag.touch()  # the running command continues and ends while paused
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        time.sleep(0.6)
        self.assertEqual(self.notices.sent_of("terminal_done"), [], "a notice was sent while paused")
        waited = self.tool({"command": None, "wait": 5})  # waiting for it is allowed while paused
        self.assertEqual((waited["status"], waited["exit_code"]), ("exited", 0), waited)
        # That wait received the result: the notice is then not needed; a fresh command proves the resume path.
        self.paused = False
        flag2, command2 = self.gated("e")
        self.assertEqual(self.tool({"command": command2, "wait": 1})["status"], "running")
        self.paused = True
        flag2.touch()
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        time.sleep(0.6)
        self.assertEqual([n for n in self.notices.sent_of("terminal_done") if n["command"] == command2], [])
        self.paused = False  # resume
        self.assertTrue(wait_until(lambda: [n for n in self.notices.sent_of("terminal_done")
                                            if n["command"] == command2], 10), "the resume did not deliver it")
        time.sleep(0.4)
        self.assertEqual(len([n for n in self.notices.sent_of("terminal_done") if n["command"] == command2]), 1)


class PeriodicChecks(RealShellFixture):
    """Fake clock: 60 s are made to pass by the test; the notifier step is driven with ``tick()``."""

    clock_driven = True

    def start_slow(self):
        self.flag = self.root / "slow-go"
        self.counter = self.root / "count"
        command = (f"i=0; while [ ! -e {shlex.quote(str(self.flag))} ]; do i=$((i+1)); echo line-$i; "
                   f"echo $i > {shlex.quote(str(self.counter))}; sleep 0.1; done; echo slow-done")
        result = self.tool({"command": command, "wait": 1})
        self.assertEqual(result["status"], "running", result)
        return command

    def advance(self, seconds):
        self.clock.now += seconds
        self.terminal.tick()

    def finish(self):
        self.flag.touch()
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))

    def test_one_check_per_minute_with_new_output(self):
        command = self.start_slow()
        time.sleep(0.4)  # real output keeps coming while the fake minute passes
        self.advance(30)
        self.assertEqual(self.notices.of("terminal_check"), [], "a check before 60 s")
        self.advance(31)
        checks = self.notices.sent_of("terminal_check")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["command"], command)
        self.assertIn("line-", checks[0]["new_output"])
        self.assertIn("log_path", checks[0])
        self.assertGreaterEqual(checks[0]["elapsed_seconds"], 0)
        self.terminal.tick()
        self.assertEqual(len(self.notices.sent_of("terminal_check")), 1, "two checks in the same minute")
        time.sleep(0.3)
        self.advance(60)
        later = self.notices.sent_of("terminal_check")
        self.assertEqual(len(later), 2)
        first_lines = set(checks[0]["new_output"].split())
        self.assertFalse(set(later[1]["new_output"].split()) & first_lines - {""},
                         "the second check repeats output the first one already sent")
        self.finish()

    def test_busy_worker_gets_one_merged_check_with_the_latest_output(self):
        self.start_slow()
        self.notices.outcome = "deferred"
        self.advance(61)
        time.sleep(0.3)
        self.advance(61)
        time.sleep(0.3)
        self.advance(61)
        self.assertEqual(self.notices.sent_of("terminal_check"), [])
        newest = int(self.counter.read_text())
        self.notices.outcome = "delivered"
        self.terminal.tick()
        sent = self.notices.sent_of("terminal_check")
        self.assertEqual(len(sent), 1, "merged checks must arrive as one")
        self.assertGreaterEqual(sent[0].get("coalesced_count", 0), 1)
        self.assertIn(f"line-{newest}", sent[0]["new_output"], "the merged check lacks the latest output")
        self.finish()

    def test_no_check_while_paused_and_none_after_the_end(self):
        self.start_slow()
        self.paused = True
        for _ in range(3):
            self.advance(61)
        self.assertEqual(self.notices.of("terminal_check"), [], "a check while paused")
        self.paused = False
        self.advance(61)
        self.assertEqual(len(self.notices.sent_of("terminal_check")), 1, "checks resume after the resume")
        self.finish()
        self.advance(61)
        self.advance(61)
        self.assertEqual(len(self.notices.sent_of("terminal_check")), 1, "a check after the command ended")
        self.assertEqual(len(self.notices.sent_of("terminal_done")), 1)

    def test_paused_end_notice_waits_for_the_resume(self):
        self.start_slow()
        self.paused = True
        self.finish()
        for _ in range(3):
            self.advance(5)
        self.assertEqual(self.notices.sent_of("terminal_done"), [])
        self.paused = False
        self.advance(1)
        self.assertEqual(len(self.notices.sent_of("terminal_done")), 1)
        self.advance(61)
        self.assertEqual(len(self.notices.sent_of("terminal_done")), 1)


class HostGateRace(flow_fixtures.FlowFixture):
    """A real TaskFlow experiment start and a terminal command racing for the host shell: never both."""

    def open(self, *, start=True):
        self.gate = getattr(self, "gate", None) or HostGate()
        flow = super().open(start=False)
        flow._host_gate = self.gate
        if start:
            flow.start()
        return flow

    def test_gate_is_exclusive_under_contention(self):
        gate = HostGate()
        inside, peak, lock = [0], [0], threading.Lock()

        def contender(owner):
            for _ in range(200):
                if gate.acquire(owner, lambda: time.sleep(0.0005)) is None:
                    with lock:
                        inside[0] += 1
                        peak[0] = max(peak[0], inside[0])
                    time.sleep(0.0005)
                    with lock:
                        inside[0] -= 1
                    gate.release(owner)

        threads = [threading.Thread(target=contender, args=(name,)) for name in ("experiment", "terminal") * 4]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(peak[0], 1)
        gate.release("terminal")
        self.assertIsNone(gate.acquire("experiment"))
        self.assertIsNotNone(gate.acquire("terminal"))
        gate.release("experiment")

    def test_a_terminal_start_racing_an_experiment_start(self):
        # The terminal command owns the shell from its idle check to its give-back. While it is in the middle of its
        # start (input held), an experiment start that races it must not start a run; once the terminal lets go
        # (here: it stops before typing), the experiment starts.
        seen = {}
        holding = threading.Event()
        let_go = threading.Event()

        class Port:
            choice = None

            def busy(port):
                return None

            def hold(port, _reason):
                seen["owner_at_hold"] = self.gate.owner
                holding.set()
                let_go.wait(10)
                seen["runs_while_held"] = len(self.runs)
                return None

            def release_hold(port, _reason=None):
                pass

            def detach(port):
                pass

            def cwd(port):
                raise RuntimeError("stop before typing")

            def send_user(port, data):
                raise AssertionError("nothing may be typed")

            def request_takeover(port):
                pass

            def snapshot(port):
                return {}

        terminal = TerminalService(handoffs=self.service, host_shell=Port, gate=self.gate,
                                   log_root=self.root / "terminal", automation=lambda: AUTOMATION,
                                   activity=self.flow.experiment_host_activity)
        self.addCleanup(terminal.close)
        out = {}
        thread = threading.Thread(target=lambda: out.update(terminal.handle(
            ActorRole.WORKER, call({"command": "true"}))))
        thread.start()
        self.assertTrue(holding.wait(10))
        self.assertEqual(seen["owner_at_hold"], "terminal")
        result = self.new_experiment()
        self.assertEqual(result["status"], "dispatched", result)
        time.sleep(0.5)
        let_go.set()
        thread.join(10)
        self.assertEqual(seen["runs_while_held"], 0, "an experiment run started while the terminal held the shell")
        self.assertEqual(out.get("status"), "start_failed", out)
        self.assertTrue(wait_until(lambda: len(self.runs) == 1 and self.flow.task_view()["status"] == "finished", 15),
                        self.flow.task_view())
        self.assertTrue(wait_until(lambda: self.gate.owner is None, 10))

if __name__ == "__main__":
    unittest.main()
