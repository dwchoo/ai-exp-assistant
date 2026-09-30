"""F-P27-DEADLINE regressions: no elapsed-time end of an automated experiment.

BRIEF "실험 실행 시간 상한 없음", C-AC-07 "경과 시간만으로 실험을 종료하지 않는다",
OPERATING-CONTRACT §3 (the wait interval is not a runtime limit), C-AC-08
"주 프로그램은 끝났지만 후손이 생존" -> not complete until descendants end.
Only the startup handshake keeps a bounded timeout (``start_timeout``).
"""
from pathlib import Path
import shlex
import tempfile
import time
import unittest
from uuid import uuid4

from workbench.terminal.shell_g2.lifecycle import normalize_run
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell

SHELLS = (ShellChoice("bash", "/usr/bin/bash"), ShellChoice("sh", "/usr/bin/dash"))


def ports(shell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "d" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


def pump(shells, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for shell in shells:
            shell.poll(.02)
            shell.display_bytes()


def until(shell, condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = shell.poll(.02)
        shell.display_bytes()
        if condition(state):
            return state
    raise AssertionError({"timeout": shell.snapshot()})


class RunWireTests(unittest.TestCase):
    def test_only_the_startup_handshake_is_bounded(self):
        self.assertNotIn("start_timeout", normalize_run("/bin/bash", ":", "x"))
        self.assertEqual(normalize_run("/bin/bash", ":", "x", start_timeout=9)["start_timeout"], 9)
        for value in (True, 0, -1, float("inf"), float("nan"), 301):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_run("/bin/bash", ":", "x", start_timeout=value)
        # The legacy runtime keyword is accepted but never becomes a runtime bound.
        for value in (0.1, 120, 10_000):
            request = normalize_run("/bin/bash", ":", "x", return_timeout=value)
            self.assertNotIn("return_timeout", request)
            self.assertNotIn("start_timeout", request)


class UnboundedExperimentRuntimeTests(unittest.TestCase):
    def open(self, choice, directory):
        shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "HOME": directory}, choice=choice)
        self.addCleanup(shell.close)
        shell.send_user(b"wb-handoff\n")
        until(shell, lambda s: s["parent_mode"] == "control_wait")
        shell.claim_manager()
        return shell

    def test_default_submit_runs_past_60s_and_completes_normally(self):
        with tempfile.TemporaryDirectory(prefix="p27-long-") as directory:
            shells = [self.open(choice, directory) for choice in SHELLS]
            began = time.monotonic()
            for shell in shells:
                control, automation = ports(shell)
                shell.submit(control, "sleep 62", automation)  # production default: no timeout argument
            while time.monotonic() - began < 61:
                pump(shells, 1.0)
                for shell in shells:
                    life = shell.snapshot()["lifecycle"]
                    self.assertEqual(life["unknown"], [], f"unknown after {time.monotonic() - began:.0f}s")
                    self.assertIsNone(life["main_exit"], f"main ended after {time.monotonic() - began:.0f}s")
            for shell in shells:
                state = until(shell, lambda s: s["lifecycle"]["input_barrier"], 15)
                self.assertEqual((state["lifecycle"]["main_exit"], state["lifecycle"]["unknown"]), (0, []))
                shell.release_input()
                until(shell, lambda s: s["lifecycle"]["control_returned"])
            self.assertGreaterEqual(time.monotonic() - began, 62)

    def test_live_descendant_after_main_return_keeps_run_incomplete_until_it_exits(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="p27-desc-") as directory:
                shell = self.open(choice, directory)
                marker = Path(directory) / "descendant-finished"
                control, automation = ports(shell)
                shell.submit(control, f"(sleep 11; printf done > {shlex.quote(str(marker))}) & exit 0", automation)
                state = until(shell, lambda s: s["lifecycle"]["main_exit"] is not None)
                self.assertEqual(state["lifecycle"]["main_exit"], 0)
                returned = time.monotonic()
                while time.monotonic() - returned < 10:
                    pump([shell], 0.5)
                    life = shell.snapshot()["lifecycle"]
                    if marker.exists():
                        break
                    self.assertEqual(life["unknown"], [])
                    self.assertNotEqual(life["lifetime"], "ended")
                    self.assertFalse(life["input_barrier"])
                state = until(shell, lambda s: s["lifecycle"]["input_barrier"], 15)
                self.assertEqual(marker.read_text(), "done", "descendant was killed")
                self.assertEqual((state["lifecycle"]["lifetime"], state["lifecycle"]["unknown"]), ("ended", []))
                # The input barrier waits for the backend release; elapsed time is not a failure.
                pump([shell], 6.0)
                self.assertEqual(shell.snapshot()["lifecycle"]["unknown"], [])
                shell.release_input()
                state = until(shell, lambda s: s["lifecycle"]["control_returned"])
                self.assertEqual(state["lifecycle"]["unknown"], [])


class DashInitEnvironmentTests(unittest.TestCase):
    """C-AC-32: the Workbench init file never leaks through ENV to user or experiment."""

    def test_env_is_restored_after_dash_init(self):
        for supplied in (None, "/nonexistent/user-env-file"):
            with self.subTest(env=supplied), tempfile.TemporaryDirectory(prefix="p27-env-") as directory:
                env = {"PATH": "/usr/bin:/bin", "HOME": directory}
                if supplied is not None:
                    env["ENV"] = supplied
                shell = PersistentShell(user_environment=env, choice=ShellChoice("sh", "/usr/bin/dash"))
                self.addCleanup(shell.close)
                expected = supplied or "unset"
                manual, run = Path(directory) / "manual", Path(directory) / "run"
                shell.send_user(f"printf '%s' \"${{ENV-unset}}\" > {shlex.quote(str(manual))}\n".encode())
                until(shell, lambda _: manual.exists() and manual.read_text() != "")
                self.assertEqual(manual.read_text(), expected)
                shell.send_user(b"wb-handoff\n")
                until(shell, lambda s: s["parent_mode"] == "control_wait")
                shell.claim_manager()
                control, automation = ports(shell)
                shell.submit(control, f"printf '%s' \"${{ENV-unset}}\" > {shlex.quote(str(run))}", automation)
                until(shell, lambda s: s["lifecycle"]["input_barrier"])
                self.assertEqual(run.read_text(), expected)
                shell.release_input()
                until(shell, lambda s: s["lifecycle"]["control_returned"])


if __name__ == "__main__":
    unittest.main()
