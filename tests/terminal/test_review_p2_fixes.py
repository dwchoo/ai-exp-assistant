"""Review p27-review-cw17-01 P2 regressions for the shell side.

P2-1: the supervisor reaps subreaper orphans while the main program still runs.
P2-3: shell, user children and experiments start with default SIGPIPE/SIGXFSZ.
P2-4: a 2 MiB paste to a raw foreground program never blocks the backend loop.
"""
from pathlib import Path
import shlex
import signal
import tempfile
import time
import unittest
from uuid import uuid4

from workbench.backend.panes import INPUT_QUEUE_BYTES, ShellPane
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell

SHELLS = (ShellChoice("bash", "/usr/bin/bash"), ShellChoice("sh", "/usr/bin/dash"))
DEFAULT_BITS = (1 << (signal.SIGPIPE - 1)) | (1 << (signal.SIGXFSZ - 1))


def ports(shell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "e" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


def until(shell, condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = shell.poll(.02)
        shell.display_bytes()
        if condition(state):
            return state
    raise AssertionError({"timeout": shell.snapshot()})


def stat_state(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def sig_ignored(path):
    for line in Path(path).read_text().splitlines():
        if line.startswith("SigIgn:"):
            return int(line.split()[1], 16)
    raise AssertionError(f"no SigIgn in {path}")


class ManagedShellCase(unittest.TestCase):
    def open(self, choice, directory):
        shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "HOME": directory}, choice=choice)
        self.addCleanup(shell.close)
        return shell

    def claim(self, shell):
        shell.send_user(b"wb-handoff\n")
        until(shell, lambda s: s["parent_mode"] == "control_wait")
        shell.claim_manager()


class SupervisorReapsOrphansDuringMainTests(ManagedShellCase):
    def test_fifty_double_fork_orphans_never_accumulate_as_zombies(self):
        with tempfile.TemporaryDirectory(prefix="p27-zombie-") as directory:
            shell = self.open(ShellChoice("bash", "/usr/bin/bash"), directory)
            self.claim(shell)
            control, automation = ports(shell)
            shell.submit(control, "for i in $(seq 50); do ( sh -c 'exit 0' & ) ; done; sleep 4", automation)
            state = until(shell, lambda s: s["lifecycle"]["experiment_started"])
            supervisor = state["lifecycle"]["supervisor_pid"]
            time.sleep(2.0)
            shell.poll(0)
            children = Path(f"/proc/{supervisor}/task/{supervisor}/children").read_text().split()
            zombies = [pid for pid in children if stat_state(pid) == "Z"]
            self.assertLessEqual(len(zombies), 2, f"{len(zombies)} unreaped orphans while main runs")
            self.assertIsNone(shell.snapshot()["lifecycle"]["main_exit"])
            reaped = [e for e in shell._transport.events if e.startswith("DESCENDANT_REAPED:")]
            self.assertGreaterEqual(len(reaped), 48)
            state = until(shell, lambda s: s["lifecycle"]["input_barrier"])
            self.assertEqual((state["lifecycle"]["main_exit"], state["lifecycle"]["unknown"]), (0, []))
            shell.release_input()
            until(shell, lambda s: s["lifecycle"]["control_returned"])


class DefaultSignalDispositionTests(ManagedShellCase):
    def test_shell_user_child_and_experiment_have_default_pipe_and_xfsz(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="p27-sig-") as directory:
                root = Path(directory)
                shell = self.open(choice, directory)
                parent, child, run = root / "parent", root / "child", root / "run"
                shell.send_user(f"cat /proc/$$/status > {shlex.quote(str(parent))}; "
                                f"sh -c 'cat /proc/$$/status' > {shlex.quote(str(child))}\n".encode())
                until(shell, lambda _: child.exists() and child.stat().st_size > 0)
                self.claim(shell)
                control, automation = ports(shell)
                shell.submit(control, f"cat /proc/$$/status > {shlex.quote(str(run))}", automation)
                until(shell, lambda s: s["lifecycle"]["input_barrier"])
                shell.release_input()
                until(shell, lambda s: s["lifecycle"]["control_returned"])
                for label, path in (("shell", parent), ("user child", child), ("experiment", run)):
                    self.assertFalse(sig_ignored(path) & DEFAULT_BITS, f"{label} ignores PIPE/XFSZ")


class LargeForegroundPasteTests(unittest.TestCase):
    def test_2mib_paste_to_raw_foreground_program_keeps_backend_loop_responsive(self):
        size = INPUT_QUEUE_BYTES
        payload = (bytes(range(32, 127)) * (size // 95 + 1))[:size]
        for choice in SHELLS:
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory(prefix="p27-paste-") as directory:
                out = Path(directory) / "out"
                pane = ShellPane(choice, {"PATH": "/usr/bin:/bin", "HOME": directory})
                try:
                    command = f"stty raw -echo; head -c {size} > {shlex.quote(str(out))}; stty sane\r"
                    self.assertIsNone(pane.admit(command.encode()))
                    deadline = time.monotonic() + 5
                    while pane.state["parent_mode"] != "manual_foreground" and time.monotonic() < deadline:
                        pane.pump()
                        time.sleep(0.02)
                    time.sleep(0.3)  # let stty raw take effect before the paste
                    pane.pump()
                    began = time.monotonic()
                    self.assertIsNone(pane.admit(payload))
                    worst = time.monotonic() - began
                    while (pane.wants_write() or not out.exists() or out.stat().st_size < size) \
                            and time.monotonic() - began < 60:
                        start = time.monotonic()
                        pane.pump()
                        pane.info()  # what the backend snapshot/status path reads
                        worst = max(worst, time.monotonic() - start)
                        time.sleep(0.01)
                    self.assertEqual((pane.dropped_input_bytes, pane.last_input_problem), (0, None))
                    self.assertEqual(out.read_bytes(), payload)
                    self.assertLess(worst, 0.25, f"one backend loop step blocked {worst:.2f}s")
                finally:
                    pane.close()


if __name__ == "__main__":
    unittest.main()
