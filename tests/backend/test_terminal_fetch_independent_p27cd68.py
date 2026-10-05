"""p27-cd68-test-04 independent tests for DECISIONS.md C-D68 (10) and the wait-tool adjudication.

Expectations come from C-D68 (10) (user answer "즉시 응답"), root-adjudication-p27-cd68-wait.json and the
fix-04/fix-05 result notes, not from the implementation:

- (10) a command-less ``terminal`` call never waits: it returns at once the running state with the output not yet
  returned, or the finished result; it is not a waiting call (the 60 s checks go on); a finished result it returned
  counts as received (no ``terminal_done`` afterwards).
- adjudication: the worker has no OMP ``wait`` tool (a worker ``wait`` is a leak); the manager keeps it.
- smoke-02: the running result, the check and the skill tell the worker to end its turn with one short text line.

Real ShellPane + HostShellPort (bash), fake clock; no OMP, no model, no network.
"""

from __future__ import annotations

from pathlib import Path
import re
import shlex
import sys
import time
import unittest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "ui"))

import test_terminal_independent_p27cd68 as base  # noqa: E402
from workbench.backend import launcher  # noqa: E402

wait_until = base.wait_until
REPO = HERE.parents[1]
SHORT_LINE = re.compile(r"one short text line", re.I)
NO_EMPTY = re.compile(r"never an empty reply", re.I)


class FetchNeverWaits(base.RealShellFixture):
    clock_driven = True

    def advance(self, seconds):
        self.clock.now += seconds
        self.terminal.tick()

    def start_slow(self):
        self.flag = self.root / "slow-go"
        command = (f"i=0; while [ ! -e {shlex.quote(str(self.flag))} ]; do i=$((i+1)); echo line-$i; sleep 0.1; done;"
                   " echo slow-done")
        result = self.tool({"command": command, "wait": 1})
        self.assertEqual(result["status"], "running", result)
        return result

    def finish(self):
        self.flag.touch()
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))

    def fetch(self):
        self.terminal._wait_seconds = 60  # a waiting implementation would block ~60 s here
        started = time.monotonic()
        result = self.terminal.handle(base.ActorRole.WORKER, base.call({"command": None}))
        return result, time.monotonic() - started

    def test_a_command_less_call_returns_running_at_once_with_only_new_output(self):
        first = self.start_slow()
        time.sleep(0.5)
        result, took = self.fetch()
        self.assertLess(took, 5, "the command-less call waited")
        self.assertEqual(result["status"], "running", result)
        self.assertEqual(result["log_path"], first["log_path"])
        self.assertGreaterEqual(result["elapsed_seconds"], 0)
        self.assertIn("line-", result["output_tail"], "no new output was returned")
        self.assertRegex(result["detail"], SHORT_LINE)
        seen = set(result["output_tail"].split())
        time.sleep(0.5)
        later, took = self.fetch()
        self.assertLess(took, 5)
        self.assertEqual(later["status"], "running", later)
        self.assertIn("line-", later["output_tail"])
        self.assertFalse(seen & set(later["output_tail"].split()), "output already returned was repeated")
        self.finish()

    def test_it_does_not_hold_back_the_check(self):
        self.start_slow()
        time.sleep(0.4)
        self.advance(30)
        self.fetch()
        self.assertEqual(self.notices.of("terminal_check"), [], "a check before 60 s")
        self.advance(31)
        checks = self.notices.sent_of("terminal_check")
        self.assertEqual(len(checks), 1, "the command-less call suppressed the 60 s check")
        self.assertRegex(checks[0]["instruction"], SHORT_LINE)
        self.assertRegex(checks[0]["instruction"], NO_EMPTY)
        self.fetch()
        self.advance(60)
        self.assertEqual(len(self.notices.sent_of("terminal_check")), 2, "a second fetch suppressed later checks")
        self.assertFalse(self.terminal._waiting_now(), "a command-less call must not count as a waiting call")
        self.finish()

    def test_a_returned_finished_result_counts_as_received_and_suppresses_the_done_notice(self):
        self.start_slow()
        self.finish()
        result, took = self.fetch()
        self.assertLess(took, 5)
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 0), result)
        self.assertIn("slow-done", result["output_tail"])
        self.advance(1)
        time.sleep(0.5)
        self.advance(1)
        self.assertEqual(self.notices.sent_of("terminal_done"), [], "terminal_done sent after the result was fetched")

    def test_without_a_fetch_the_done_notice_is_sent_once(self):
        self.start_slow()
        self.finish()
        self.advance(1)
        done = self.notices.sent_of("terminal_done")
        self.assertEqual(len(done), 1, done)
        self.assertRegex(done[0]["instruction"], re.compile(r"continue your work", re.I))

    def test_a_fetch_while_running_does_not_count_as_receiving_the_result(self):
        self.start_slow()
        self.fetch()  # running: no result received
        self.finish()
        self.advance(1)
        self.assertEqual(len(self.notices.sent_of("terminal_done")), 1, "a running fetch swallowed the done notice")

    def test_running_result_of_a_command_call_says_one_short_text_line(self):
        result = self.start_slow()
        self.assertRegex(result["detail"], SHORT_LINE)
        self.assertRegex(result["detail"], NO_EMPTY)
        self.finish()


class WorkerHasNoWaitTool(unittest.TestCase):
    def observed(self, tools):
        return {"system_prompt_default": True, "context_files": [], "skills": [], "skill_commands": [], "rules": [],
                "mcp_tools": [], "task_agents": [], "other_commands": [], "tools": list(tools),
                "autoqa": False, "browser": False}

    def test_wait_is_not_in_the_worker_allowlist_and_is_a_worker_leak_only(self):
        self.assertNotIn("wait", launcher.WORKER_TOOLS)
        tools = ["read", "terminal", "to_manager", "task"]
        self.assertEqual(launcher.isolation_leaks(self.observed(tools), allowed_skills=(), role="worker"), [])
        self.assertIn("tool:wait", launcher.isolation_leaks(self.observed(tools + ["wait"]), allowed_skills=(),
                                                           role="worker"))
        self.assertEqual(launcher.isolation_leaks(self.observed(tools + ["wait"]), allowed_skills=(),
                                                  role="manager"), [], "the manager keeps wait")

    def test_the_worker_command_line_has_no_wait_and_the_manager_has_no_allowlist(self):
        import shutil
        import tempfile
        import test_omp_isolation_independent_p27m as p27m
        root = Path(tempfile.mkdtemp(prefix="p27cd68-nowait-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        argv = {}
        for role in ("worker", "manager"):
            overlay = launcher.write_role_overlay(root, role, launcher.role_overlay(role, project_dir=root, home=root))
            argv[role] = launcher.omp_command(p27m.plan_for(args=()), overlay)
        self.assertNotIn("wait", argv["worker"][argv["worker"].index("--tools") + 1].split(","))
        self.assertNotIn("--tools", argv["manager"], "the manager keeps every OMP tool, wait included")


class TextSaysEndTheTurnWithOneShortLine(unittest.TestCase):
    def test_the_worker_skill(self):
        text = (REPO / "omp_bridge/skills/to-manager/SKILL.md").read_text()
        self.assertRegex(text, SHORT_LINE)
        self.assertRegex(text, NO_EMPTY)
        self.assertRegex(text, re.compile(r"do not wait with repeated `?terminal`? calls", re.I))
        self.assertRegex(text, re.compile(r"`?command`? null returns the current state at once \(never waits\)", re.I))
        self.assertNotRegex(text, re.compile(r"`wait` tool|use the wait tool", re.I), "the worker has no wait tool")
        self.assertRegex(text, re.compile(r"after `done`, do not send another report for the same Task", re.I))


if __name__ == "__main__":
    unittest.main()
