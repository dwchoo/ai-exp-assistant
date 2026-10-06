"""C-D69 (5) (p27-cd69-smoke-01 P1): a managed request too large for the control pipe never strands the shell.

The control loop reads one ``RUN:<base64 JSON>`` line of at most 4096 bytes. A larger request was refused only
after the lifecycle had begun, which marked it unknown: the parent stayed in control wait owned by the manager
(no TAKEOVER could be sent) and every later command was refused. Now nothing is begun or written for an
oversize request, and the same shell goes back to the user and runs the next request.
"""
import time
import unittest
from uuid import uuid4

from workbench.terminal.shell_g2 import lifecycle
from workbench.terminal.shell_g2.lifecycle import encode_run, normalize_run
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell

SHELLS = (ShellChoice("bash", "/usr/bin/bash"), ShellChoice("sh", "/usr/bin/dash"))


def until(shell, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    state = shell.poll(0)
    while time.monotonic() < deadline:
        state = shell.poll(0.03)
        if predicate(state):
            return state
    raise AssertionError(f"state not reached: {state['parent_mode']} {state['input_owner']} {state['held_reasons']}")


def ports(shell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "e" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


class OversizeRequestReturnTests(unittest.TestCase):
    def handoff(self, shell):
        shell.send_user(b"wb-handoff\n")
        until(shell, lambda s: s["parent_mode"] == "control_wait")
        shell.claim_manager()

    def test_the_limit_is_the_encoded_run_line(self):
        command = "echo " + "x" * 5000
        argv = ["/usr/bin/bash", "-c", command]
        size = len(f"RUN:{encode_run(normalize_run('/usr/bin/bash', argv, str(uuid4())))}\n".encode())
        self.assertGreater(size, 4096)
        self.assertEqual(4096, getattr(lifecycle, "RUN_REQUEST_MAX", None))

    def test_oversize_request_leaves_the_lifecycle_clean_and_the_shell_returns_to_the_user(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "TERM": "xterm"}, choice=choice)
                self.addCleanup(shell.close)
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                self.handoff(shell)
                control, automation = ports(shell)
                with self.assertRaises(ValueError):
                    shell.submit(control, "echo " + "x" * 5000, automation)
                state = shell.snapshot()
                self.assertEqual(state["lifecycle"]["unknown"], [], "nothing was sent: no unknown lifecycle")
                self.assertIsNone(state["lifecycle"]["request_id"])
                self.assertNotIn(b"xxxxxxxx", shell.display_bytes())
                shell.request_takeover()
                state = until(shell, lambda s: s["input_owner"] == "user" and s["parent_mode"] == "manual_prompt")
                self.assertNotEqual(state["phase"], "unknown")
                marker = uuid4().hex[:8]
                shell.send_user(f"echo user-{marker}\n".encode())
                deadline, seen = time.monotonic() + 5, b""
                while time.monotonic() < deadline and f"user-{marker}".encode() * 1 not in seen.split(b"echo")[-1]:
                    shell.poll(0.03)
                    seen += shell.display_bytes()
                self.assertIn(f"user-{marker}".encode(), seen.split(b"echo")[-1], "the user's input runs again")
                until(shell, lambda s: s["parent_mode"] == "manual_prompt")
                self.handoff(shell)  # the next managed request runs on the same shell
                control, automation = ports(shell)
                shell.submit(control, "exit 7", automation)
                state = until(shell, lambda s: s["lifecycle"]["input_barrier"])
                shell.release_input()
                state = until(shell, lambda s: s["lifecycle"]["control_returned"])
                self.assertEqual(state["lifecycle"]["main_exit"], 7)


if __name__ == "__main__":
    unittest.main()
