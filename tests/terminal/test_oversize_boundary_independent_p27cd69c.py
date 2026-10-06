"""C-D69 (5) independent (p27-cd69-stuck-test-01): the host shell request limit on a real PersistentShell.

Derived from the decision, not the implementation: the limit stays (one control line of at most 4096
bytes); a request over it must never begin a lifecycle or type anything, the shell must come back to the
user, and a request that fits must really run. Both bash and dash. The RUN line is ``RUN:`` + base64 +
``\\n`` so its size is 5 + 4k: the largest that fits is 4093 bytes, the next is 4097.
"""
import time
import unittest
from uuid import uuid4

from workbench.terminal.shell_g2.lifecycle import encode_run, normalize_run
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell

SHELLS = (ShellChoice("bash", "/usr/bin/bash"), ShellChoice("sh", "/usr/bin/dash"))
LIMIT = 4096


def run_line_bytes(executable, command):
    """Independent sizing of what the shell is handed: RUN:<base64 of the request JSON>\\n."""
    argv = [executable, "-c", command]
    return len(f"RUN:{encode_run(normalize_run(executable, argv, str(uuid4())))}\n".encode())


def largest_fitting(executable, make):
    n = 1
    while run_line_bytes(executable, make(n + 1)) <= LIMIT:
        n += 1
    return n


def until(shell, predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    state = shell.poll(0)
    while time.monotonic() < deadline:
        state = shell.poll(0.03)
        if predicate(state):
            return state
    raise AssertionError(f"state not reached: {state['parent_mode']} {state['input_owner']} {state['held_reasons']}")


class BoundaryTests(unittest.TestCase):
    def shell(self, choice):
        shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "TERM": "xterm", "LANG": "C.UTF-8"},
                                choice=choice)
        self.addCleanup(shell.close)
        until(shell, lambda s: s["parent_mode"] == "manual_prompt")
        return shell

    def handoff(self, shell):
        shell.send_user(b"wb-handoff\n")
        until(shell, lambda s: s["parent_mode"] == "control_wait")
        shell.claim_manager()
        state = shell.snapshot()
        return {"portVersion": 2, "kind": "ShellControl", "payload": {
            "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
            "requestId": str(uuid4()), "approvalHash": "e" * 64, "phase": "accepted"}}

    automation = {"portVersion": 2, "kind": "AutomationState", "payload": {
        "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}

    def run_to_end(self, shell, control, command):
        shell.submit(control, command, self.automation)
        seen = bytearray()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = shell.poll(0.03)
            seen.extend(shell.display_bytes())
            life = state["lifecycle"]
            if life["input_barrier"] and not life["input_returned"]:
                shell.release_input()
            if life["control_returned"] and life["input_returned"] and life["lifetime"] == "ended":
                seen.extend(shell.display_bytes())
                return life["main_exit"], bytes(seen)
        raise AssertionError(f"request did not end: {shell.snapshot()['lifecycle']}")

    def back_to_user(self, shell):
        shell.request_takeover()
        until(shell, lambda s: s["input_owner"] == "user" and s["parent_mode"] == "manual_prompt")
        marker = f"after-{uuid4().hex[:8]}"
        shell.send_user(f"echo {marker}-$((40+2))\n".encode())
        seen, deadline = bytearray(), time.monotonic() + 5
        while time.monotonic() < deadline and f"{marker}-42".encode() not in seen:
            shell.poll(0.03)
            seen.extend(shell.display_bytes())
        self.assertIn(f"{marker}-42".encode(), seen, "a manual command runs after the refusal")
        until(shell, lambda s: s["parent_mode"] == "manual_prompt")

    def check_boundary(self, choice, make, needle, exact=True):
        shell = self.shell(choice)
        n = largest_fitting(choice.executable, make)
        fits, over = make(n), make(n + 1)
        if exact:
            self.assertEqual(run_line_bytes(choice.executable, fits), 4093)
            self.assertEqual(run_line_bytes(choice.executable, over), 4097)
        self.assertLessEqual(run_line_bytes(choice.executable, fits), LIMIT)
        self.assertGreater(run_line_bytes(choice.executable, over), LIMIT)
        # one over: refused before anything begins or is written
        control = self.handoff(shell)
        before = shell.snapshot()
        shell.display_bytes()
        with self.assertRaises(ValueError):
            shell.submit(control, over, self.automation)
        after = shell.snapshot()
        self.assertEqual(after["lifecycle"]["unknown"], [])
        self.assertIsNone(after["lifecycle"]["request_id"])
        self.assertNotEqual(after["phase"], "unknown")
        self.assertEqual((after["generation"], after["owner_epoch"]), (before["generation"], before["owner_epoch"]))
        time.sleep(0.2)
        shell.poll(0)
        self.assertNotIn(needle, shell.display_bytes(), "nothing reached the terminal")
        self.back_to_user(shell)
        # exactly the largest request that fits: really runs on the same shell
        control = self.handoff(shell)
        code, shown = self.run_to_end(shell, control, fits)
        self.assertEqual(code, 0, shown[-400:])
        self.assertIn(needle, shown, "the boundary command ran and printed")
        self.back_to_user(shell)

    def test_ascii_boundary_4093_runs_4097_refused(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                self.check_boundary(choice, lambda n: "echo " + "q" * n, b"q" * 64)

    def test_multibyte_utf8_boundary(self):
        # JSON escapes non-ASCII: a Korean syllable costs far more than one byte of the line
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                make = lambda n: "printf '%s\\n' " + "한" * n
                n = largest_fitting(choice.executable, make)
                self.assertLess(n, 700, "non-ASCII is sized by its encoded form, not by characters")
                self.check_boundary(choice, make, "한".encode() * 16, exact=False)

    def test_repeated_oversize_never_strands_the_shell(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell = self.shell(choice)
                for _ in range(3):
                    control = self.handoff(shell)
                    with self.assertRaises(ValueError):
                        shell.submit(control, "echo " + "z" * 6000, self.automation)
                    self.assertEqual(shell.snapshot()["lifecycle"]["unknown"], [])
                    self.back_to_user(shell)
                control = self.handoff(shell)
                code, _ = self.run_to_end(shell, control, "exit 5")
                self.assertEqual(code, 5)


if __name__ == "__main__":
    unittest.main()
