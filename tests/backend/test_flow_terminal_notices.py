"""C-D68 (8): the worker's ``terminal`` command after the call's wait (no OMP, no provider, fake clock).

- a ``running`` result tells the worker to end its turn (no re-wait loop);
- while the command runs, a check every 60 s (fake clock) with the output since
  the last check; none while a ``terminal`` call is waiting; merged into one
  (latest) while the worker is busy; none while paused;
- on exit, one completion notice unless a waiting call got the result; also
  after the call's wait ran out or the call was aborted (the bridge's
  ``terminal_wait_abandoned``); busy -> when idle; paused -> on resume; an
  unknown or rejected delivery is never resent;
- the backend's notice port: the worker delivery lock (no race with a mailbox
  delivery or an experiment review), the bridge ``notice`` frame and its ack;
- journal records for checks and the completion notice.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

from workbench.backend.flow import HandoffService
from workbench.backend.flow_terminal import (
    ABANDON_TOOL, CHECK_INTERVAL, HostGate, TerminalService,
)
from workbench.backend.service import Backend
from workbench.contracts.v1 import ActorRole
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected, BridgeTimeout

import test_task_flow as flow_fixtures

WORKER_SESSION = str(uuid4())
AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def call(args, call_id=None, tool="terminal"):
    args = {k: v for k, v in args.items() if k != "wait"} if isinstance(args, dict) else args  # test-only
    return {"request_id": str(uuid4()), "tool_call_id": call_id or f"t-{uuid4().hex[:8]}", "tool": tool,
            "args": args, "session_id": WORKER_SESSION, "generation": 1}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ScriptedPort:
    """A host shell port whose command runs until the test ends it (the dispatch protocol, no real shell)."""

    choice = SimpleNamespace(kind="bash", executable="/usr/bin/bash")

    def __init__(self, cwd="/tmp/where-the-user-is"):
        self._cwd = cwd
        self._lock = threading.Lock()
        self._out = bytearray()
        self.exit_code = None
        self.submitted = []
        self.typed = []
        self.takeovers = 0
        self.on_claim = None
        self.initial = b""  # the command's first output (before the submit is preparation, not output)

    # the idle check and the hold
    def busy(self):
        return None

    def hold(self, _reason):
        return None

    def release_hold(self, _reason=None):
        pass

    def detach(self):
        pass

    def cwd(self):
        return self._cwd

    def send_user(self, data):
        self.typed.append(bytes(data))

    def claim_manager(self):
        if self.on_claim is not None:
            self.on_claim()

    def release_input(self):
        pass

    def request_takeover(self):
        self.takeovers += 1

    def submit(self, _control, command, _automation):
        self.submitted.append(command)
        self.emit(self.initial)

    def emit(self, data: bytes):
        with self._lock:
            self._out.extend(data)

    def finish(self, code: int):
        self.exit_code = code

    def display_bytes(self):
        with self._lock:
            data, self._out = bytes(self._out), bytearray()
        return data

    def _life(self):
        ended = self.exit_code is not None
        return {"input_barrier": False, "input_returned": ended, "unknown": [], "control_returned": ended,
                "lifetime": "ended" if ended else "running", "main_exit": self.exit_code,
                "request_id": "r" if self.submitted else None}

    def poll(self, timeout=0):
        time.sleep(min(timeout, 0.01))
        return {"phase": "running", "parent_mode": "control_wait", "lifecycle": self._life()}

    def snapshot(self):
        return {"parent_pid": 1, "generation": 1, "owner_epoch": 1, "input_owner": "manager",
                "parent_mode": "control_wait", "lifecycle": self._life()}


class Notices:
    """The notice port: records every notice and answers the scripted outcome."""

    def __init__(self):
        self.sent: list[dict] = []
        self.attempts: list[dict] = []
        self.outcome = "delivered"

    def __call__(self, notice):
        notice = json.loads(json.dumps(notice))
        self.attempts.append(notice)
        if self.outcome == "delivered":
            self.sent.append(notice)
        return self.outcome

    def of(self, kind):
        return [n for n in self.sent if n["type"] == kind]


class NoticeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cd68-notice-", dir="/tmp")
        self.root = Path(self.tmp.name)
        (self.root / "workflow").mkdir(mode=0o700)
        self.paused = False
        self.clock = FakeClock()
        self.port = ScriptedPort()
        self.notices = Notices()
        self.handoffs = HandoffService(self.root / "workflow" / "handoffs.jsonl",
                                       mailbox=flow_fixtures.FakeMailbox())
        self.terminal = TerminalService(
            handoffs=self.handoffs, host_shell=lambda: self.port, gate=HostGate(),
            log_root=self.root / "workflow" / "terminal", automation=lambda: AUTOMATION,
            paused=lambda: self.paused, poll_interval=0.01, notify=self.notices, clock=self.clock)

    def tearDown(self):
        self.terminal.close()
        self.handoffs.close()
        self.tmp.cleanup()

    def journal(self, kind=None):
        path = self.root / "workflow" / "handoffs.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [r for r in records if kind is None or r["type"] == kind]

    def run_tool(self, args, call_id=None):
        self.terminal._wait_seconds = args.get("wait", 30)  # the fixed 120 s wait, shortened for the test
        return self.terminal.handle(ActorRole.WORKER, call(args, call_id))

    def start_running(self, output=b"first line\n", after=b""):
        """A command whose call returned ``running`` (the worker ended its turn); ``after`` comes later."""
        self.port.initial = output
        result = self.run_tool({"command": "make long", "wait": 1})
        self.assertEqual(result["status"], "running", result)
        self.assertIn(output.decode().strip(), result["output_tail"])
        self.emit(after)
        return result

    def emit(self, data):
        self.port.emit(data)
        self.assertTrue(wait_until(lambda: not self.port._out))  # the follower collected it
        time.sleep(0.05)

    def advance(self, seconds):
        self.clock.now += seconds
        self.terminal.tick()

    def finish(self, code=0, output=b""):
        self.port.emit(output)
        self.port.finish(code)
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))


class RunningResultTests(NoticeFixture):
    def test_running_tells_the_worker_to_end_its_turn(self):
        result = self.start_running()
        self.assertIn("End your turn", result["detail"])
        self.assertIn("every 60 s", result["detail"])
        self.assertIn("completion notice", result["detail"])
        self.assertNotIn("command null", result["detail"], "no re-wait loop")
        self.assertNotIn("wait for it again", result["detail"])
        self.assertEqual(CHECK_INTERVAL, 60)
        self.finish()


class PeriodicCheckTests(NoticeFixture):
    def test_a_check_every_60_seconds_with_the_output_since_the_last_one(self):
        first = self.start_running(b"before the result\n", after=b"compiling a\n")
        self.advance(59)
        self.assertEqual(self.notices.attempts, [], "nothing before 60 s")
        self.advance(1)
        checks = self.notices.of("terminal_check")
        self.assertEqual(len(checks), 1)
        check = checks[0]
        self.assertEqual((check["command_id"], check["command"], check["cwd"], check["log_path"], check["state"]),
                         (first["command_id"], "make long", "/tmp/where-the-user-is", first["log_path"], "running"))
        self.assertIn("compiling a", check["new_output"])
        self.assertNotIn("before the result", check["new_output"], "the running result already showed it")
        self.assertGreaterEqual(check["elapsed_seconds"], 0)
        self.assertIsNone(check["exit_code"])
        self.assertIsNone(check["task_id"], "a command without a Task")
        self.assertRegex(check["instruction"], r"errors or a hang")
        self.assertRegex(check["instruction"], r"to_manager")
        self.assertRegex(check["instruction"], r"no Task")
        self.emit(b"compiling b\n")
        self.advance(30)
        self.assertEqual(len(self.notices.of("terminal_check")), 1, "nothing between checks")
        self.advance(30)
        checks = self.notices.of("terminal_check")
        self.assertEqual(len(checks), 2)
        self.assertIn("compiling b", checks[1]["new_output"])
        self.assertNotIn("compiling a", checks[1]["new_output"], "only the output since the last check")
        sent = [r for r in self.journal("terminal_check") if r["outcome"] == "sent"]
        self.assertEqual([r["command_id"] for r in sent], [first["command_id"]] * 2)
        self.assertTrue(all("new_output" not in r for r in self.journal("terminal_check")), "no output journaled")
        self.finish()

    def test_no_check_while_a_terminal_call_waits_for_the_command(self):
        waiting = threading.Thread(target=self.run_tool, args=({"command": "make long", "wait": 30},))
        waiting.start()
        self.assertTrue(wait_until(lambda: self.terminal._waiting_now()))
        self.advance(61)
        self.advance(61)
        self.assertEqual(self.notices.attempts, [], "the waiting call sees the output itself")
        self.finish()
        waiting.join(10)

    def test_busy_worker_gets_one_merged_check_with_the_latest_output(self):
        self.start_running(after=b"part one\n")
        self.notices.outcome = "deferred"
        self.advance(60)
        self.advance(2)
        self.assertEqual(self.notices.sent, [])
        self.emit(b"part two\n")
        self.advance(58)  # the next check is due while one is still pending: merged
        self.notices.outcome = "delivered"
        self.advance(2)
        checks = self.notices.of("terminal_check")
        self.assertEqual(len(checks), 1, "one pending check, the latest")
        self.assertEqual(checks[0]["coalesced_count"], 1)
        self.assertIn("part one", checks[0]["new_output"])
        self.assertIn("part two", checks[0]["new_output"])
        outcomes = [r["outcome"] for r in self.journal("terminal_check")]
        self.assertIn("deferred", outcomes)
        self.assertIn("merged", outcomes)
        self.assertEqual(outcomes.count("sent"), 1)
        self.finish()

    def test_no_check_while_paused(self):
        self.start_running()
        self.paused = True
        self.advance(60)
        self.advance(60)
        self.assertEqual(self.notices.attempts, [])
        self.assertIn("skipped_paused", [r["outcome"] for r in self.journal("terminal_check")])
        self.paused = False
        self.advance(60)
        self.assertEqual(len(self.notices.of("terminal_check")), 1, "checks come back after the resume")
        self.finish()

    def test_no_check_after_the_command_ended(self):
        self.start_running()
        self.notices.outcome = "deferred"
        self.advance(60)
        self.finish(0)
        self.notices.outcome = "delivered"
        self.advance(2)
        self.advance(60)
        self.assertEqual(self.notices.of("terminal_check"), [], "the completion notice replaces a pending check")
        self.assertEqual(len(self.notices.of("terminal_done")), 1)


class CompletionNoticeTests(NoticeFixture):
    def done(self):
        return self.notices.of("terminal_done")

    def test_after_the_wait_ran_out_one_notice_with_the_result(self):
        first = self.start_running(b"building\n")
        self.finish(2, b"error: it failed\n")
        self.terminal.tick()
        self.advance(5)
        self.advance(120)
        notices = self.done()
        self.assertEqual(len(notices), 1, "exactly once")
        notice = notices[0]
        self.assertEqual((notice["command_id"], notice["command"], notice["cwd"], notice["status"],
                          notice["exit_code"], notice["log_path"]),
                         (first["command_id"], "make long", "/tmp/where-the-user-is", "exited", 2,
                          first["log_path"]))
        self.assertIn("error: it failed", notice["output_tail"])
        self.assertGreaterEqual(notice["duration_seconds"], 0)
        self.assertRegex(notice["instruction"], r"(?i)continue")
        records = self.journal("terminal_notice")
        self.assertEqual([r["outcome"] for r in records if r["outcome"] == "sent"], ["sent"])
        self.assertTrue(all("output_tail" not in r for r in records))

    def test_no_notice_when_a_waiting_call_got_the_result(self):
        result = {}
        self.port.emit(b"quick\n")
        waiting = threading.Thread(target=lambda: result.update(
            self.run_tool({"command": "make quick", "wait": 30})))
        waiting.start()
        self.assertTrue(wait_until(lambda: self.terminal._waiting_now()))
        self.finish(0)
        waiting.join(10)
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 0))
        self.advance(1)
        self.advance(120)
        self.assertEqual(self.notices.attempts, [])

    def test_a_null_fetch_of_the_finished_result_counts_as_received(self):
        # C-D68 (10): the fetch returns the finished result at once; no terminal_done follows it.
        self.start_running()
        self.finish(0, b"the end\n")
        fetched = self.run_tool({"command": None})
        self.assertEqual((fetched["status"], fetched["exit_code"]), ("exited", 0), fetched)
        self.assertIn("the end", fetched["output_tail"])
        self.advance(1)
        self.advance(60)
        self.assertEqual(self.done(), [])
        self.assertIn("not_needed", [r["outcome"] for r in self.journal("terminal_notice")])

    def test_an_aborted_waiting_call_does_not_swallow_the_notice(self):
        result = {}
        waiting = threading.Thread(target=lambda: result.update(
            self.run_tool({"command": "make test", "wait": 30}, call_id="call-aborted")))
        waiting.start()
        self.assertTrue(wait_until(lambda: self.terminal._waiting_now()))
        abandoned = self.terminal.abandon(ActorRole.WORKER, call({"tool_call_id": "call-aborted"},
                                                                   tool=ABANDON_TOOL))
        self.assertEqual(abandoned["status"], "recorded")
        self.finish(1)
        waiting.join(10)
        self.assertEqual(result["status"], "exited", "the backend still answers; the bridge dropped it")
        self.advance(1)
        self.advance(60)
        self.assertEqual(len(self.done()), 1)
        self.assertEqual(self.done()[0]["exit_code"], 1)
        self.assertTrue(any(r["type"] == "terminal_wait_abandoned" for r in self.journal()))

    def test_an_abort_that_arrives_after_the_result_still_gets_one_notice(self):
        waiting = threading.Thread(target=self.run_tool,
                                   args=({"command": "make test", "wait": 30}, "call-late"))
        waiting.start()
        self.assertTrue(wait_until(lambda: self.terminal._waiting_now()))
        self.finish(0)
        waiting.join(10)
        self.advance(1)
        self.assertEqual(self.done(), [])
        self.terminal.abandon(ActorRole.WORKER, call({"tool_call_id": "call-late"}, tool=ABANDON_TOOL))
        self.advance(1)
        self.terminal.abandon(ActorRole.WORKER, call({"tool_call_id": "call-late"}, tool=ABANDON_TOOL))
        self.advance(1)
        self.assertEqual(len(self.done()), 1, "never twice")

    def test_an_abort_before_the_call_registered_runs_nothing(self):
        # p27-cd68-review-01 P3-4: the bridge said "only a command that already started keeps running".
        self.terminal.abandon(ActorRole.WORKER, call({"tool_call_id": "call-early"}, tool=ABANDON_TOOL))
        result = self.run_tool({"command": "make test", "wait": 30}, "call-early")
        self.assertEqual((result["status"], result["reason"]), ("aborted", "call_abandoned"), result)
        self.assertEqual(self.port.submitted, [])
        self.assertEqual(self.port.typed, [], "nothing typed")
        self.assertIsNone(self.terminal.current())
        self.assertIsNone(self.terminal._gate.owner)
        self.assertTrue(any(r["type"] == "terminal_aborted" for r in self.journal()))
        self.advance(1)
        self.assertEqual(self.notices.attempts, [], "nothing ran, nothing to notify")

    def test_an_abort_during_the_start_sends_no_command_and_gives_the_shell_back(self):
        self.port.on_claim = lambda: self.terminal.abandon(
            ActorRole.WORKER, call({"tool_call_id": "call-mid"}, tool=ABANDON_TOOL))
        result = self.run_tool({"command": "make test", "wait": 30}, "call-mid")
        self.assertEqual((result["status"], result["reason"]), ("aborted", "call_abandoned"), result)
        self.assertEqual(self.port.submitted, [], "the command was never submitted")
        self.assertEqual(self.port.typed, [b"wb-handoff\n"])
        self.assertEqual(self.port.takeovers, 1, "the shell is given back to the user")
        self.assertIsNone(self.terminal._gate.owner)

    def test_a_result_that_never_reached_the_worker_still_gets_the_notice(self):
        # p27-cd68-review-01 P2-1: a bridge reconnect or worker respawn during the wait drops the tool_result.
        request = call({"command": "make test", "wait": 30}, "call-lost")
        result = {}
        waiting = threading.Thread(target=lambda: result.update(self.terminal.handle(ActorRole.WORKER, request)))
        waiting.start()
        self.assertTrue(wait_until(lambda: self.terminal._waiting_now()))
        self.finish(5)
        waiting.join(10)
        self.assertEqual(result["status"], "exited")
        self.advance(1)
        self.assertEqual(self.done(), [], "the call got it as far as the backend knows")
        self.terminal.undelivered(ActorRole.WORKER, request)
        self.advance(1)
        self.terminal.undelivered(ActorRole.WORKER, request)
        self.advance(1)
        self.assertEqual([n["exit_code"] for n in self.done()], [5], "once")
        self.assertTrue(any(r["type"] == "terminal_result_undelivered" for r in self.journal()))

    def test_a_busy_worker_gets_the_notice_when_idle(self):
        self.start_running()
        self.notices.outcome = "deferred"
        self.finish(0)
        self.advance(1)
        self.advance(2)
        self.assertEqual(self.done(), [])
        self.notices.outcome = "delivered"
        self.advance(2)
        self.advance(60)
        self.assertEqual(len(self.done()), 1)
        outcomes = [r["outcome"] for r in self.journal("terminal_notice")]
        self.assertIn("deferred", outcomes)
        self.assertEqual(outcomes.count("sent"), 1)

    def test_paused_holds_the_notice_until_the_resume(self):
        self.start_running()
        self.paused = True
        self.finish(3)
        self.advance(1)
        self.advance(600)
        self.assertEqual(self.notices.attempts, [])
        self.assertIn("held_paused", [r["outcome"] for r in self.journal("terminal_notice")])
        self.paused = False
        self.advance(1)
        self.assertEqual([n["exit_code"] for n in self.done()], [3])
        self.advance(60)
        self.assertEqual(len(self.done()), 1)

    def test_an_unknown_delivery_is_never_resent(self):
        self.start_running()
        self.notices.outcome = "unknown"
        self.finish(0)
        self.advance(1)
        self.notices.outcome = "delivered"
        self.advance(2)
        self.advance(60)
        self.assertEqual(len(self.notices.attempts), 1)
        self.assertEqual(self.done(), [])
        self.assertIn("unknown", [r["outcome"] for r in self.journal("terminal_notice")])

    def test_the_notice_of_an_earlier_command_survives_a_new_command(self):
        self.start_running()
        self.notices.outcome = "deferred"
        self.finish(4)
        self.advance(1)
        self.port = ScriptedPort()
        second = self.run_tool({"command": "echo next", "wait": 1})
        self.assertEqual(second["status"], "running")
        self.notices.outcome = "delivered"
        self.advance(2)
        self.assertEqual([n["exit_code"] for n in self.done()], [4])
        self.port.finish(0)
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))


class SmokeFixTests(NoticeFixture):
    """p27-cd68-fix-03: C-D68 (9) and smoke-01 / review-02 corrections."""

    def test_the_host_pane_shows_the_command_and_it_still_runs_as_shell_dash_c(self):
        result = self.start_running()
        argv = self.port.submitted[0]
        self.assertIsInstance(argv, list, "an argv, so nothing is typed into the parent shell")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertEqual(argv[-1], "make long", "the command text is an argument, never re-quoted")
        self.assertEqual(result["command"], "make long")
        self.finish()

    def test_running_tells_the_worker_to_end_its_turn_without_progress_reports(self):
        result = self.start_running()
        self.assertIn("End your turn now", result["detail"])
        self.assertIn("Do not send progress reports", result["detail"])
        self.assertIn("unless the user or the manager asks or a check shows a problem", result["detail"])
        self.finish()

    def test_a_queued_report_says_it_was_accepted_and_must_not_be_resent(self):
        from workbench.backend.flow import OutboundMessage
        from workbench.contracts.v1 import MessageKind
        outbound = OutboundMessage(str(uuid4()), 1, str(uuid4()), ActorRole.WORKER, ActorRole.MANAGER,
                                   MessageKind.REPORT, {"kind": "done"})
        result = self.handoffs.enqueue(outbound, origin="test")
        self.assertEqual(result["status"], "queued")
        self.assertNotIn("not yet processed", result["detail"])
        self.assertIn("Accepted", result["detail"])
        self.assertIn("Do not send it again", result["detail"])

    def test_an_abort_before_the_submit_leaves_no_log_file(self):
        self.port.on_claim = lambda: self.terminal.abandon(
            ActorRole.WORKER, call({"tool_call_id": "call-nolog"}, tool=ABANDON_TOOL))
        result = self.run_tool({"command": "make test", "wait": 30}, "call-nolog")
        self.assertEqual(result["status"], "aborted")
        log_root = self.root / "workflow" / "terminal"
        self.assertEqual(sorted(p.name for p in log_root.iterdir()) if log_root.exists() else [], [])

    def test_a_waiter_of_a_gone_worker_session_does_not_hold_back_checks(self):
        waiting = threading.Thread(target=self.run_tool, args=({"command": "make long", "wait": 30},))
        waiting.start()
        self.assertTrue(wait_until(lambda: self.terminal._waiting_now()))
        self.advance(61)
        self.assertEqual(self.notices.attempts, [])
        self.terminal.peer_gone(ActorRole.WORKER, WORKER_SESSION, 1)  # the worker's bridge connection ended
        self.assertFalse(self.terminal._waiting_now())
        self.advance(60)
        self.assertEqual(len(self.notices.of("terminal_check")), 1)
        self.terminal.peer_gone(ActorRole.MANAGER, WORKER_SESSION, 1)  # another role: ignored
        self.finish(0)
        waiting.join(10)
        self.advance(1)
        self.assertEqual(len(self.notices.of("terminal_done")), 1, "its result reached nobody")
        self.assertTrue(any(r["type"] == "terminal_peer_gone" for r in self.journal()))


class NonBlockingFetchTests(NoticeFixture):
    """C-D68 (10) (user, 2026-10-06): a call without a command never waits; it returns the current state."""

    def test_a_fetch_while_running_returns_at_once_with_the_output_not_yet_returned(self):
        first = self.start_running(b"first line\n", after=b"second line\n")
        started = time.monotonic()
        fetched = self.run_tool({"command": None, "wait": 30})
        self.assertLess(time.monotonic() - started, 1.0, "no waiting")
        self.assertEqual((fetched["status"], fetched["command_id"]), ("running", first["command_id"]), fetched)
        self.assertIn("second line", fetched["output_tail"])
        self.assertNotIn("first line", fetched["output_tail"], "already returned with the running result")
        self.assertGreaterEqual(fetched["elapsed_seconds"], 0)
        self.assertIn("End your turn now", fetched["detail"])
        again = self.run_tool({"command": None})
        self.assertEqual((again["status"], again["output_tail"]), ("running", ""), "nothing new since")
        self.emit(b"third line\n")
        self.assertIn("third line", self.run_tool({"command": None})["output_tail"])
        self.assertTrue(any(r["type"] == "terminal_fetch" for r in self.journal()))
        self.finish()

    def test_a_fetch_does_not_hold_back_the_check(self):
        self.start_running(after=b"progress\n")
        self.advance(30)
        self.run_tool({"command": None})
        self.assertFalse(self.terminal._waiting_now())
        self.advance(30)  # 60 s after the first call returned: the fetch did not move the check
        checks = self.notices.of("terminal_check")
        self.assertEqual(len(checks), 1)
        self.assertIn("progress", checks[0]["new_output"], "the check keeps its own output window")
        self.finish()

    def test_a_fetch_before_the_notice_was_sent_makes_it_unneeded_and_after_it_changes_nothing(self):
        self.start_running()
        self.notices.outcome = "deferred"  # the worker is busy: the notice waits
        self.finish(7)
        self.advance(1)
        self.assertEqual(self.run_tool({"command": None})["exit_code"], 7)
        self.notices.outcome = "delivered"
        self.advance(5)
        self.assertEqual(self.notices.of("terminal_done"), [], "the fetch received it")
        self.assertEqual(self.run_tool({"command": None})["exit_code"], 7, "the result stays fetchable")


class AbandonTests(NoticeFixture):
    def test_only_the_worker_and_a_tool_call_id(self):
        result = self.terminal.abandon(ActorRole.MANAGER, call({"tool_call_id": "x"}, tool=ABANDON_TOOL))
        self.assertEqual(result["status"], "rejected")
        for args in ({}, {"tool_call_id": ""}, {"tool_call_id": 5}, {"tool_call_id": "x" * 300}, None):
            result = self.terminal.abandon(ActorRole.WORKER, call(args, tool=ABANDON_TOOL))
            self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), args)


class NoticePortTests(unittest.TestCase):
    """``Backend._worker_notice``: one delivery to the worker at a time, through the bridge ``notice`` frame."""

    def backend(self, ack=None, error=None, connected=True):
        backend = Backend.__new__(Backend)
        lock = threading.Lock()
        sent = []

        def request(role, frame, timeout=5, **_kw):
            sent.append((role, frame))
            if error is not None:
                raise error
            return ack

        def peer(role, timeout=0):
            if not connected:
                raise BridgeDisconnected("no worker")
            return SimpleNamespace(session_id=WORKER_SESSION, generation=1)

        backend.bridge = SimpleNamespace(delivery_lock=lambda role: lock, request=request, peer=peer)
        return backend, lock, sent

    def test_ack_mapping(self):
        notice = {"notice_id": str(uuid4()), "type": "terminal_check"}
        for ack, outcome in (({"status": "api_accepted"}, "delivered"),
                             ({"status": "duplicate_api_accepted"}, "delivered"),
                             ({"status": "deferred", "reason": "OMP is busy"}, "deferred"),
                             ({"status": "deferred", "reason": "paused"}, "paused"),
                             ({"status": "unknown_no_replay"}, "unknown"),
                             ({"status": "rejected"}, "rejected")):
            backend, _lock, sent = self.backend(ack)
            self.assertEqual(backend._worker_notice(notice), outcome, ack)
            self.assertEqual(sent, [(ActorRole.WORKER, {"kind": "notice", "notice": notice})])
        backend, _lock, sent = self.backend(connected=False)
        self.assertEqual(backend._worker_notice(notice), "not_connected")
        self.assertEqual(sent, [])
        backend, _lock, _sent = self.backend(error=BridgeTimeout("late"))
        self.assertEqual(backend._worker_notice(notice), "unknown", "it may have been sent: never resent")

    def test_a_delivery_in_progress_to_the_worker_defers_the_notice(self):
        # An experiment's periodic review or a handoff holds the worker's delivery lock until its turn ends.
        backend, lock, sent = self.backend({"status": "api_accepted"})
        with lock:
            self.assertEqual(backend._worker_notice({"notice_id": str(uuid4()), "type": "terminal_check"}),
                             "deferred")
        self.assertEqual(sent, [], "never two deliveries into the same worker turn")

    def test_an_undelivered_terminal_result_goes_to_the_terminal_service(self):
        seen = []
        backend = Backend.__new__(Backend)
        backend.terminal = SimpleNamespace(undelivered=lambda role, request: seen.append((role, request["tool"])))
        backend._tool_result_undelivered(SimpleNamespace(role=ActorRole.WORKER), {"tool": "terminal"})
        backend._tool_result_undelivered(SimpleNamespace(role=ActorRole.WORKER), {"tool": "to_manager"})
        self.assertEqual(seen, [(ActorRole.WORKER, "terminal")])

    def test_a_gone_worker_peer_goes_to_the_terminal_service(self):
        seen = []
        backend = Backend.__new__(Backend)
        backend.terminal = SimpleNamespace(peer_gone=lambda role, session, generation: seen.append((role, session)))
        backend._bridge_peer_gone(SimpleNamespace(role=ActorRole.WORKER, session_id="s", generation=1))
        self.assertEqual(seen, [(ActorRole.WORKER, "s")])

    def test_the_host_pane_is_operated_by_the_worker_during_its_command(self):
        backend = Backend.__new__(Backend)
        backend.terminal = SimpleNamespace(host_operator=lambda: "worker")
        info = backend._pane_info(SimpleNamespace(info=lambda: {"pane": "host_shell", "input_owner": "manager"}),
                                  host=True)
        self.assertEqual(info["operated_by"], "worker")
        backend.terminal = SimpleNamespace(host_operator=lambda: None)
        self.assertIsNone(backend._pane_info(SimpleNamespace(info=lambda: {"pane": "host_shell"}), host=True)
                          ["operated_by"])
        self.assertNotIn("operated_by", backend._pane_info(SimpleNamespace(info=lambda: {"pane": "x"}), host=False))

    def test_the_backend_routes_the_bridge_abandon_signal(self):
        seen = []
        backend = Backend.__new__(Backend)
        backend.handoffs = SimpleNamespace(handle=lambda role, request: seen.append("handoffs") or {"h": 1})
        backend.terminal = SimpleNamespace(handle=lambda role, request: seen.append("terminal") or {"t": 1},
                                           abandon=lambda role, request: seen.append("abandon") or {"a": 1})
        peer = SimpleNamespace(role=ActorRole.WORKER)
        self.assertEqual(backend._tool_request(peer, {"tool": ABANDON_TOOL}), {"a": 1})
        self.assertEqual(seen, ["abandon"])


class ExperimentExclusionNoticeTests(flow_fixtures.FlowFixture):
    """An experiment run's reviews and terminal checks never both run: one host owner (HostGate)."""

    def open(self, *, start=True):
        self.gate = getattr(self, "gate", None) or HostGate()
        flow = super().open(start=False)
        flow._host_gate = self.gate
        if start:
            flow.start()
        return flow

    def test_no_terminal_check_or_notice_while_an_experiment_runs(self):
        notices = Notices()
        clock = FakeClock()
        terminal = TerminalService(handoffs=self.service, host_shell=lambda: ScriptedPort(), gate=self.gate,
                                   log_root=self.root / "terminal", automation=lambda: AUTOMATION,
                                   activity=self.flow.experiment_host_activity, notify=notices, clock=clock)
        self.gates.exit.clear()
        self.assertEqual(self.new_experiment()["status"], "dispatched")
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        refused = terminal.handle(ActorRole.WORKER, call({"command": "true", "wait": 1}))
        self.assertEqual(refused["status"], "host_terminal_busy")
        for _ in range(5):
            clock.now += 60
            terminal.tick()
        self.assertEqual(notices.attempts, [], "the experiment's own review is the only one")
        self.gates.exit.set()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "finished"))
        terminal.close()


if __name__ == "__main__":
    unittest.main()
