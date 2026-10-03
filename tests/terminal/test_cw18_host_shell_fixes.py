"""CW-18 corrections F1/F2 (p27-cw18-fix-01) on the real product host shell.

F1: a user job (running, stopped, disowned) in the shell's session keeps the shell busy, found from
/proc without typing anything; the busy state ends with the job (no latch), and a held experiment then
starts without having consumed a retry.
F2: after a run the user's shell is back in the directory it was in before Workbench typed ``cd``; a user
who already left the worktree is never moved; a busy shell gets the ``cd`` later, never mixed with typing.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from test_cw18_host_shell_independent_p27w import HostShellHarness  # noqa: E402


class JobDetectionTests(HostShellHarness):
    def job(self, command: str, name: str) -> int:
        pid_file = self.root / f"{name}.pid"
        self.type(f"{command} & echo $! > {pid_file}\r".encode())
        self.assertTrue(self.until(lambda: pid_file.exists() and pid_file.read_text().strip().isdecimal()))
        pid = int(pid_file.read_text())
        self.started_pids.append(pid)
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"))
        return pid

    def test_disowned_job_still_in_the_session_keeps_the_shell_busy(self):
        port = self.port()
        pid_file = self.root / "disowned.pid"
        self.type(f"sleep 631 & echo $! > {pid_file}; disown\r".encode())
        self.assertTrue(self.until(lambda: pid_file.exists() and pid_file.read_text().strip().isdecimal()))
        self.started_pids.append(int(pid_file.read_text()))
        self.assertTrue(self.until(lambda: self.state()["parent_mode"] == "manual_prompt"))
        busy = port.busy()
        self.assertIsNotNone(busy)
        self.assertIn("jobs", busy)
        self.assertNotIn("manual_jobs", self.state()["held_reasons"], "found without a wb-handoff probe")

    def test_busy_ends_with_the_job_without_a_latch(self):
        port = self.port()
        pid = self.job("sleep 1", "short")
        self.assertIsNotNone(port.busy())
        self.assertTrue(self.until(lambda: not Path(f"/proc/{pid}").exists() or
                                   Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] == "Z", 5))
        self.assertTrue(self.until(lambda: port.busy() is None, 5), port.busy())

    def test_nothing_is_typed_to_find_jobs(self):
        port = self.port()
        self.job("sleep 632", "quiet")
        before = len(self.screen())
        for _ in range(5):
            self.assertIsNotNone(port.busy())
            self.assertIsNotNone(port.hold("run"))
        time.sleep(0.3)
        self.assertEqual(self.screen()[before:], b"", "busy() must not type into the shell")
        self.assertIsNone(self.pane.automation_hold)

    def test_a_held_experiment_starts_once_the_job_ended_without_using_a_retry(self):
        self.start_flow()
        self.job("sleep 2", "brief")
        result = self.experiment("printf 'PASS\\n'; printf PASS > outcome.txt", "m-job")
        self.assertTrue(self.until(lambda: self.view().get("held_reason") == "host_terminal_busy"), self.view())
        self.assertEqual(self.view().get("runs_started"), 0)
        self.assertTrue(self.until(lambda: self.view().get("status") == "finished", 30), self.view())
        view = self.view()
        self.assertEqual(view["runs_started"], 1, view)
        self.assertEqual((view["last_result"]["judgment"], view["last_result"]["run_closed"]), ("success", True))
        self.assertEqual(result["status"], "dispatched")
        self.assertTrue(self.until(self.given_back, 10))


class WorkingDirectoryTests(HostShellHarness):
    def pwd(self) -> str:
        return os.readlink(f"/proc/{self.pane.pid}/cwd")

    def test_directory_with_spaces_and_quotes_is_restored_after_a_run(self):
        home = self.root / "user dir's $HOME"
        home.mkdir()
        self.start_flow()
        self.type(b"cd \"" + str(home).encode().replace(b"$", b"\\$") + b"\"\r")
        self.assertTrue(self.until(lambda: self.pwd() == str(home)), self.pwd())
        self.experiment("printf 'PASS\\n'; printf PASS > outcome.txt", "m-cwd")
        self.assertTrue(self.until(lambda: self.view().get("status") == "finished", 30), self.view())
        self.assertTrue(self.until(self.given_back, 10), self.state())
        self.assertEqual(self.pwd(), str(home))
        self.assertEqual(self.pane.dropped_input_bytes, 0)
        # the next run restores to the same directory again
        self.experiment(None, "m-cwd-2", task_id=self.view()["task_id"], run=True)
        self.assertTrue(self.until(lambda: self.view().get("status") == "finished"
                                   and self.view().get("runs_started") == 2, 30), self.view())
        self.assertTrue(self.until(self.given_back, 10))
        self.assertEqual(self.pwd(), str(home))

    def test_a_user_who_left_the_worktree_is_not_moved(self):
        port = self.port()
        worktrees = self.root / "worktrees"
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.type(f"cd {elsewhere}\r".encode())
        self.assertTrue(self.until(lambda: self.pwd() == str(elsewhere)))
        before = len(self.screen())
        self.assertEqual(port.restore_cwd(str(self.root), worktrees, reason="restore"), "user_moved")
        self.assertEqual(self.pwd(), str(elsewhere))
        time.sleep(0.2)
        self.assertEqual(self.screen()[before:], b"", "nothing typed")
        self.assertIsNone(self.pane.automation_hold)

    def test_busy_shell_gets_the_cd_later_never_mixed_with_typing(self):
        port = self.port()
        slot = self.root / "worktrees" / "slot"
        slot.mkdir()
        self.type(f"cd {slot}\r".encode())
        self.assertTrue(self.until(lambda: self.pwd() == str(slot) and self.state()["parent_mode"] == "manual_prompt"))
        self.type(b"echo p27-fix-typed")  # the user is typing: busy
        self.assertTrue(self.until(lambda: port.busy() is not None))
        self.assertEqual(port.restore_cwd(str(self.root), self.root / "worktrees", reason="restore",
                                          idle_wait=0.3), "busy")
        self.assertEqual(self.pwd(), str(slot))
        self.assertIsNone(self.pane.automation_hold)
        self.type(b"\r")
        self.assertTrue(self.until(lambda: port.busy() is None, 5), port.busy())
        self.assertEqual(port.restore_cwd(str(self.root), self.root / "worktrees", reason="restore"), "restored")
        self.assertEqual(self.pwd(), str(self.root))
        self.assertIsNone(self.pane.automation_hold)
        self.assertIn(b"p27-fix-typed", self.screen())
        self.assertEqual(self.pane.dropped_input_bytes, 0)

    def test_a_busy_give_back_is_retried_by_the_flow(self):
        self.start_flow()
        self.type(b"cd /tmp\r")
        self.assertTrue(self.until(lambda: self.pwd() == "/tmp"))
        # the run's command leaves a job in the user's session: the give-back cannot type the cd yet
        pid_file = self.root / "left.pid"
        self.experiment(f"sleep 8 & echo $! > {pid_file}; printf 'PASS\\n'; printf PASS > outcome.txt", "m-left")
        self.assertTrue(self.until(lambda: pid_file.exists() and pid_file.read_text().strip().isdecimal(), 20))
        self.started_pids.append(int(pid_file.read_text()))
        self.assertTrue(self.until(lambda: self.view().get("status") == "finished", 30), self.view())
        self.assertTrue(self.until(lambda: self.pwd() == "/tmp", 15), self.pwd())
        self.assertTrue(self.until(self.given_back, 10), self.state())


if __name__ == "__main__":
    unittest.main()
