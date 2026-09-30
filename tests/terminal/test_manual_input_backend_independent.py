"""Independent p2.7 check of manual host-shell input through the real backend (p27-input-test-01).

Path under test: ``python -m workbench start`` (real entrypoint, real OMP
18.2.10 with a local counting provider, no credentials) -> ui_v1 socket ->
ShellPane -> PersistentShell -> real Bash or dash. Expectations come from BRIEF
C-AC-08 (manual input to the confirmed current target; unknown target held with
a reason), C-AC-26 (edited/unsubmitted input holds automatic return) and the
p2.7 root adjudication (uncertainty holds claim/handoff, not manual input).

After a confirmed shutdown no owned process, socket or the host shell's
``/tmp/cw03-g2-*`` shell-init directory may remain.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from independent_support import (  # noqa: E402
    LiveBackend, PaneId, UiClient, finish, find_omp, settle, shell_children, start_no_attach, stop_and_verify,
    wait_client,
)
from workbench.contracts.ui_v1 import ClientType  # noqa: E402

OMP = find_omp()
PYTHON3 = "/usr/bin/python3"

OWN_GROUP_PROGRAM = r'''
import os, signal, sys, time
out = sys.argv[1]
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
r, w = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(w)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.setpgid(0, 0)
    os.read(r, 1)
    time.sleep(30)
    os._exit(0)
os.close(r)
os.setpgid(pid, pid)
os.tcsetpgrp(0, pid)
os.write(w, b"g")
_, status = os.waitpid(pid, 0)
os.tcsetpgrp(0, os.getpgrp())
with open(out, "w") as f:
    f.write(str(os.WTERMSIG(status) if os.WIFSIGNALED(status) else "exit"))
'''


def erase_x(kind: str) -> bytes:
    return b"\x1b[D\x1b[C\x7f" if kind == "bash" else b"\x1b[D\x1b[C" + b"\x7f" * 7


def init_dir_of(pid: int, kind: str) -> Path:
    if kind == "bash":
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return Path(argv[argv.index(b"--rcfile") + 1].decode()).parent
    for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if item.startswith(b"ENV="):
            return Path(item[4:].decode()).parent
    raise AssertionError("dash ENV init file not found")


@unittest.skipUnless(OMP, "real OMP 18.2.10 is required")
class BackendManualInputTests(unittest.TestCase):
    def open(self, kind: str):
        live = LiveBackend(OMP, path="/usr/bin:/bin")
        self.addCleanup(finish, self, live, [])
        if kind == "sh":
            bindir = live.root / "bin"
            bindir.mkdir()
            for name, target in {"sh": "/usr/bin/dash", "omp": OMP, "cat": "/usr/bin/cat",
                                 "sleep": "/usr/bin/sleep", "python3": PYTHON3}.items():
                (bindir / name).symlink_to(target)
            live.env["PATH"] = str(bindir)
        snapshot = start_no_attach(self, live)
        shell = snapshot["panes"]["host_shell"]["shell"]
        self.assertEqual(shell["kind"], kind)
        client = UiClient(live.data / "ui.sock")
        self.addCleanup(client.close)
        self.assertTrue(client.attach((30, 100))["ok"])
        self.live, self.client, self.kind = live, client, kind
        self.shell_pid = shell["parent"]["pid"]
        self.init_dir = init_dir_of(self.shell_pid, kind)
        self.assertTrue(self.init_dir.name.startswith("cw03-g2-") and self.init_dir.exists())
        return live, client

    # -- helpers ------------------------------------------------------------
    def shell(self) -> dict:
        return self.client.snapshot()["panes"]["host_shell"]

    def send(self, data: bytes, wait: float = 0.4) -> None:
        result = self.client.input(PaneId.HOST_SHELL, data)
        self.assertTrue(result.get("ok"), f"manual input {data!r} rejected: {result}")
        settle(self.client, wait)
        pane = self.shell()
        self.assertEqual((pane["dropped_input_bytes"], pane["last_input_problem"]), (0, None),
                         f"queued bytes withheld after {data!r}: {pane}")

    def held(self, data: bytes) -> dict:
        result = self.client.input(PaneId.HOST_SHELL, data)
        self.assertFalse(result.get("ok"), f"input {data!r} should be held: {result}")
        self.assertTrue(result.get("reason") and result.get("detail"), result)
        return result

    def mode(self, *modes: str, timeout: float = 10.0, clean: bool = False) -> dict:
        return wait_client(self.client, lambda s: s["panes"]["host_shell"]["shell"]["parent_mode"] in modes
                           and (not clean or s["panes"]["host_shell"]["shell"]["phase"] != "unknown"),
                           timeout)["panes"]["host_shell"]["shell"]

    def file(self, path: Path, expected: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.read_text() == expected:
                return
            settle(self.client, 0.05)
        self.fail(f"{path.name}: expected {expected!r}, got {path.read_text() if path.exists() else None!r}")

    def child(self, name: str, present: bool, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(info[0] == name for info in shell_children(self.shell_pid).values()) == present:
                return
            settle(self.client, 0.05)
        self.fail(f"{name} present={not present} after {timeout}s: {shell_children(self.shell_pid)}")

    def handoff_refused(self) -> None:
        refused = self.client.request(ClientType.HANDOFF)
        self.assertFalse(refused.get("ok"), f"handoff accepted on an uncertain line: {refused}")
        self.assertEqual(refused.get("reason"), "handoff_held", refused)
        self.assertTrue(refused.get("detail"))
        self.assertEqual(self.shell()["shell"]["input_owner"], "user")

    def confirmed_shutdown_leaves_nothing(self) -> None:
        self.client.detach()
        self.client.close()
        left, output = stop_and_verify(self.live)
        self.assertIn('"verified": true', output)
        self.assertEqual(left, {})
        self.assertFalse(self.init_dir.exists(), f"shell-init dir left after confirmed shutdown: {self.init_dir}")

    # -- scenario -------------------------------------------------------------
    def run_scenario(self, kind: str) -> None:
        live, client = self.open(kind)
        p = live.project
        m = p / "m"
        q = shlex.quote(str(m))
        self.mode("manual_prompt", clean=True)
        # Prompt: ESC sequences + erase, pending line holds handoff, not input.
        self.send(f"printf A >> {q}X".encode())
        self.send(erase_x(kind))
        self.assertEqual(self.shell()["shell"]["phase"], "unknown")
        self.handoff_refused()
        settle(client, 1.5)                        # no timer-based reset
        self.handoff_refused()
        self.send(b"\r")
        self.file(m, "A")
        self.mode("manual_prompt", clean=True)
        # Ctrl-U, Ctrl-W, Ctrl-C (pending + empty), Ctrl-D mid-line, Ctrl-Z at prompt.
        self.send(f"printf BAD >> {q}\x15printf B >> {q}\r".encode())
        self.file(m, "AB")
        self.send(f"printf C >> {q} junk\x17\r".encode())
        self.file(m, "ABC")
        self.send(f"printf NO >> {q}".encode())
        self.send(b"\x03")
        self.mode("manual_prompt", clean=True)
        self.send(b"\x03")
        self.mode("manual_prompt", clean=True)
        self.send(f"printf D >> {q}".encode())
        self.send(b"\x04\r")
        self.file(m, "ABCD")
        self.send(b"\x1a")
        self.handoff_refused()
        self.send(b"\r")
        self.mode("manual_prompt", clean=True)
        # cat: canonical edits, ESC data, EOF.
        out = p / "cat-out"
        self.send(f"cat > {shlex.quote(str(out))}\r".encode())
        self.child("cat", True)
        self.mode("manual_foreground")
        self.send(b"ab\x7fc\njunk\x15kept\n\x1b[A\n")
        self.file(out, "ac\nkept\n\x1b[A\n")
        self.handoff_refused()
        self.send(b"\x04")
        self.child("cat", False)
        self.mode("manual_prompt", clean=True)
        # python3 -q: editing, Ctrl-C, Ctrl-Z, fg, Ctrl-D.
        py = p / "py-out"
        self.send(b"python3 -q\r", wait=1.5)
        self.child("python3", True)
        self.send(f"garbage\x15open({str(py)!r}, 'w').write(str(40+3\x7f2))\r".encode(), wait=1.0)
        self.file(py, "42")
        self.send(b"while True: pass\r\r", wait=0.5)
        self.send(b"\x03", wait=0.5)
        self.child("python3", True)
        self.send(b"\x1a")
        self.mode("manual_prompt")
        self.send(b"fg\n")
        self.mode("manual_foreground")
        settle(client, 0.5)
        self.send(f"open({str(py)!r}, 'a').write('!')\r".encode())
        self.file(py, "42!")
        self.send(b"\x04")
        self.child("python3", False)
        self.mode("manual_prompt", clean=True)
        # sleep and sh -c 'sleep 30' get Ctrl-C.
        for command in (b"sleep 30\r", b"sh -c 'sleep 30; echo never'\r"):
            self.send(command)
            self.child("sleep", True)
            self.mode("manual_foreground")
            self.send(b"\x03")
            self.child("sleep", False)
            self.mode("manual_prompt", clean=True)
        # A program that makes its own foreground process group gets Ctrl-C.
        program, own = p / "own_group.py", p / "own-out"
        program.write_text(OWN_GROUP_PROGRAM)
        self.send(f"python3 {shlex.quote(str(program))} {shlex.quote(str(own))}\r".encode())
        wait_client(client, lambda s: s["panes"]["host_shell"]["shell"]["parent_mode"] == "manual_foreground"
                    and os.getpgid(self.shell_pid) != self._fg_group())
        self.send(b"\x03")
        self.file(own, "2")
        self.mode("manual_prompt", clean=True)
        # wb-handoff typed with editing keys is not a clean handoff.
        self.send(b"wb-handofx\x7ff\r", wait=1.5)
        self.handoff_refused()
        if self.shell()["shell"]["parent_mode"] == "control_wait":
            held = self.held(b"x")
            self.assertIn("control wait", held["detail"])
            taken = client.request(ClientType.TAKEOVER_REQUEST)
            self.assertTrue(taken.get("ok"), taken)
            self.mode("manual_prompt", clean=True)
        # A clean wb-handoff is accepted; afterwards the manager owns input.
        self.send(b"wb-handoff\r", wait=1.0)
        self.mode("control_wait")
        accepted = client.request(ClientType.HANDOFF)
        self.assertTrue(accepted.get("ok"), accepted)
        self.assertEqual(accepted["shell"]["input_owner"], "manager")
        self.assertEqual(self.held(b"x")["reason"], "input_owner_manager")
        self.confirmed_shutdown_leaves_nothing()

    def _fg_group(self) -> int:
        return self.shell()["shell"].get("foreground_group") or self._tpgid()

    def _tpgid(self) -> int:
        fields = Path(f"/proc/{self.shell_pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return int(fields[5])

    def test_bash_manual_input_via_ui_v1(self):
        self.run_scenario("bash")

    def test_dash_manual_input_via_ui_v1(self):
        self.run_scenario("sh")


if __name__ == "__main__":
    unittest.main()
