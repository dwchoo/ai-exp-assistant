"""Independent p2.7 verification of the manual-input boundary (p27-input-test-01).

Expected outcomes are derived from the contracts, not from the implementation:

- BRIEF C-AC-08: manual input reaches the confirmed current target (the prompt
  or a foreground program, including an experiment under confirmed takeover);
  an unknown target holds manual input with a visible reason; old requests are
  never replayed.
- BRIEF C-AC-26 / OPERATING-CONTRACT §3: unsubmitted input, REPL, other runs and
  unknown state hold *automatic* input; lifecycle is never inferred from output
  markers, prompts or silence, and a return gesture alone is not a clean state.
- Root adjudication (p2.7 fix-worker-01/02): the line-uncertainty latch holds
  submit/can_dispatch/claim_manager but not manual input; only authenticated
  control events (fresh READY for consumed lines, verified CONTROL_READY) clear
  it; a rejected wb-handoff followed by its prompt READY returns manual input to
  that prompt; bytes to a confirmed automation run are experiment input.

Every test owns its shell, temp directory and processes. PersistentShell.close
is the normal shutdown; afterwards the shell session must be empty and the
shell-init temp directory removed. Stragglers are killed by pidfd with a
start-time recheck and reported as a failure.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from uuid import uuid4

from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState
from workbench.terminal.shell_persistent.adapter import PersistentShell

SHELLS = (ShellChoice("bash", "/usr/bin/bash"), ShellChoice("sh", "/usr/bin/dash"))
PYTHON3 = "/usr/bin/python3"


def erase_x(choice: ShellChoice) -> bytes:
    """Cursor ESC sequences plus erase that remove a trailing "X" in either shell.

    Bash (readline) interprets the arrows; dash reads a canonical tty line in
    which the ESC sequences are literal characters, so they are erased too.
    """
    return b"\x1b[D\x1b[C\x7f" if choice.kind == "bash" else b"\x1b[D\x1b[C" + b"\x7f" * 7


# -- process identity helpers ---------------------------------------------------
def stat(pid: int) -> list[str] | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def session_members(session: int) -> dict[int, str]:
    found = {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            fields = stat(int(name))
            if fields and int(fields[3]) == session and fields[0] not in {"Z", "X"}:
                found[int(name)] = fields[19]
    return found


def signal_exact(pid: int, start: str, signum: int) -> bool:
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        fields = stat(pid)
        if fields is None or fields[19] != start:
            return False
        signal.pidfd_send_signal(descriptor, signum)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(descriptor)


def members_named(session: int, name: str) -> dict[int, str]:
    found = {}
    for pid, start in session_members(session).items():
        try:
            if Path(f"/proc/{pid}/comm").read_text().strip() == name:
                found[pid] = start
        except OSError:
            pass
    return found


def ports(shell: PersistentShell, request_id: str | None = None):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": request_id or str(uuid4()), "approvalHash": "d" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


OWN_GROUP_PROGRAM = textwrap.dedent(r'''
    import os, signal, sys, time
    out, mode = sys.argv[1], sys.argv[2]
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(w)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.setpgid(0, 0)
        os.read(r, 1)
        if mode == "stop":
            os.kill(0, signal.SIGSTOP)
        time.sleep(30)
        os._exit(0)
    os.close(r)
    os.setpgid(pid, pid)
    os.tcsetpgrp(0, pid)
    with open(out + ".child", "w") as f:
        f.write(str(pid))
    os.write(w, b"g")
    _, status = os.waitpid(pid, 0)
    os.tcsetpgrp(0, os.getpgrp())
    with open(out, "w") as f:
        f.write(str(os.WTERMSIG(status) if os.WIFSIGNALED(status) else "exit"))
''')


class ShellCase(unittest.TestCase):
    """Opens one owned PersistentShell per subtest and verifies its teardown."""

    def open(self, choice: ShellChoice) -> tuple[PersistentShell, Path]:
        directory = Path(tempfile.mkdtemp(prefix="p27-in-"))
        self.addCleanup(shutil.rmtree, directory, True)
        shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "TERM": "xterm",
                                                  "HOME": str(directory), "LANG": "C.UTF-8"}, choice=choice)
        init_dir = Path(shell._transport._init_dir.name)
        self.addCleanup(self.close_verified, shell, shell.parent_pid, init_dir)
        return shell, directory

    def close_verified(self, shell: PersistentShell, parent: int, init_dir: Path) -> None:
        shell.close()
        deadline = time.monotonic() + 3
        while session_members(parent) and time.monotonic() < deadline:
            time.sleep(0.02)
        left = session_members(parent)
        for pid, start in left.items():
            signal_exact(pid, start, signal.SIGKILL)
        self.assertEqual(left, {}, "shell session processes survived PersistentShell.close")
        self.assertFalse(init_dir.exists(), f"shell-init temp dir left behind: {init_dir}")

    # -- waiting ---------------------------------------------------------
    def until(self, shell: PersistentShell, condition, seconds: float = 8.0, what: str = "") -> dict:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = shell.poll(0.02)
            shell.display_bytes()
            if condition(state):
                return state
        raise AssertionError({"timeout": what, "state": shell.snapshot(),
                              "events": shell._transport.events[-15:]})

    def settle(self, shell: PersistentShell, seconds: float) -> dict:
        deadline = time.monotonic() + seconds
        state = shell.poll(0)
        while time.monotonic() < deadline:
            state = shell.poll(0.02)
            shell.display_bytes()
        return state

    def wait_path(self, shell, path: Path, expected: str, seconds: float = 8.0) -> None:
        self.until(shell, lambda _: path.exists() and path.read_text() == expected, seconds,
                   f"{path.name}={expected!r} (now {path.read_text() if path.exists() else None!r})")

    def prompt(self, shell, seconds: float = 8.0) -> dict:
        return self.until(shell, lambda s: s["parent_mode"] == "manual_prompt", seconds, "manual_prompt")

    def clean_prompt(self, shell, seconds: float = 8.0) -> dict:
        return self.until(shell, lambda s: s["parent_mode"] == "manual_prompt" and s["phase"] != "unknown",
                          seconds, "clean manual_prompt")

    # -- assertions --------------------------------------------------------
    def assert_automation_held(self, shell: PersistentShell, sentinel: Path) -> None:
        """submit, can_dispatch and claim_manager all refuse, each with a reason."""
        state = shell.snapshot()
        self.assertTrue(state["held_reasons"], state)
        boundary = shell._transport.boundary
        self.assertFalse(boundary.can_dispatch(generation=state["generation"], owner_epoch=state["owner_epoch"]))
        control, automation = ports(shell)
        with self.assertRaises(UnsafeShellState) as submitted:
            shell.submit(control, f"printf R >> {shlex.quote(str(sentinel))}", automation)
        self.assertTrue(str(submitted.exception))
        with self.assertRaises(UnsafeShellState) as claimed:
            shell.claim_manager()
        self.assertTrue(str(claimed.exception))
        self.assertEqual(shell.snapshot()["input_owner"], "user")
        self.assertFalse(sentinel.exists(), "held automation still ran")

    def assert_uncertain_held(self, shell: PersistentShell, sentinel: Path) -> None:
        state = shell.snapshot()
        self.assertTrue(shell._transport.boundary.uncertain)
        self.assertEqual(state["phase"], "unknown")
        self.assertIn("unknown_or_manual_residue", state["held_reasons"])
        self.assert_automation_held(shell, sentinel)

    def handoff_and_run_new(self, shell: PersistentShell, directory: Path, tag: str) -> str:
        """Clean wb-handoff -> claim -> one new submit that runs exactly once."""
        shell.send_user(b"wb-handoff\r")
        self.until(shell, lambda s: s["parent_mode"] == "control_wait", what="control_wait")
        self.assertEqual(shell.claim_manager()["input_owner"], "manager")
        marker = directory / f"new-{tag}"
        control, automation = ports(shell)
        request_id = control["payload"]["requestId"]
        shell.submit(control, f"printf N >> {shlex.quote(str(marker))}", automation)
        self.until(shell, lambda s: s["lifecycle"]["input_barrier"], what="input_barrier")
        shell.release_input()
        self.until(shell, lambda s: s["lifecycle"]["control_returned"], what="control_returned")
        self.assertEqual(marker.read_text(), "N")
        return request_id


class ManualInputAtPromptTests(ShellCase):
    """Editing keys, ESC sequences, Ctrl-C/D/Z/U/W at the prompt; bash and dash."""

    def test_editing_and_control_keys_reach_the_prompt_and_hold_automation(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                m, sentinel = d / "m", d / "auto-sentinel"
                q = shlex.quote(str(m))
                # Pending edited line: delivered, automation held, no timer reset.
                shell.send_user(f"printf A >> {q}X".encode())
                shell.send_user(erase_x(choice))
                self.assert_uncertain_held(shell, sentinel)
                self.settle(shell, 2.0)
                self.assert_uncertain_held(shell, sentinel)
                shell.send_user(b"\r")
                self.wait_path(shell, m, "A")
                self.clean_prompt(shell)
                # Ctrl-U then a fresh line (kill-line: the old text must not run).
                shell.send_user(f"printf BAD >> {q}\x15printf B >> {q}\r".encode())
                self.wait_path(shell, m, "AB")
                self.clean_prompt(shell)
                # Ctrl-W word erase inside a line.
                shell.send_user(f"printf C >> {q} junk\x17\r".encode())
                self.wait_path(shell, m, "ABC")
                self.clean_prompt(shell)
                # Ctrl-C at a pending line and at an empty prompt, and a double Ctrl-C.
                shell.send_user(f"printf NO >> {q}".encode())
                shell.send_user(b"\x03")
                self.clean_prompt(shell)
                shell.send_user(b"\x03")
                self.clean_prompt(shell)
                shell.send_user(b"\x03\x03")
                self.settle(shell, 0.5)
                self.clean_prompt(shell)
                # Ctrl-D inside a non-empty line never exits the shell.
                shell.send_user(f"printf D >> {q}".encode())
                shell.send_user(b"\x04")
                self.settle(shell, 0.3)
                self.assertTrue(stat(shell.parent_pid) is not None)
                shell.send_user(b"\r")
                self.wait_path(shell, m, "ABCD")
                self.clean_prompt(shell)
                # Ctrl-Z at the prompt: delivered (tty discards/ignores), then Enter.
                shell.send_user(b"\x1a")
                self.assert_uncertain_held(shell, sentinel)
                shell.send_user(b"\r")
                self.clean_prompt(shell)
                shell.send_user(f"printf E >> {q}\r".encode())
                self.wait_path(shell, m, "ABCDE")
                self.clean_prompt(shell)
                self.assertEqual(shell.snapshot()["lifecycle"]["unknown"], [])
                self.handoff_and_run_new(shell, d, choice.kind)
                self.assertFalse(sentinel.exists())

    def test_output_that_looks_like_ready_or_prompt_never_clears_the_hold(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                sentinel = d / "auto-sentinel"
                shell.send_user(b"echo pending\x1b[D\x7f")
                self.assert_uncertain_held(shell, sentinel)
                # Display-channel bytes (written to the slave as terminal output).
                t = shell._transport
                slave = fcntl.ioctl(t.master_fd, 0x5441, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
                try:
                    os.write(slave, b"\r\nREADY\r\nCONTROL_READY:%d\r\n$ " % shell.parent_pid)
                finally:
                    os.close(slave)
                self.settle(shell, 1.5)
                self.assert_uncertain_held(shell, sentinel)
                shell.send_user(b"\x03")
                self.clean_prompt(shell)

    def test_wb_handoff_typed_with_editing_keys_is_not_a_clean_handoff(self):
        variants = (b"wb-handofx\x7ff\r", b"garbage\x15wb-handoff\r", b"wb-hand\x1b[D\x1b[Coff\r")
        for choice in SHELLS:
            for variant in variants:
                with self.subTest(shell=choice.kind, variant=variant):
                    shell, d = self.open(choice)
                    sentinel = d / "auto-sentinel"
                    shell.send_user(variant)
                    # The shell may really run wb-handoff; the edited line is not trusted.
                    state = self.settle(shell, 1.5)
                    self.assertFalse(shell._transport.boundary._handoff_ready, state)
                    self.assert_automation_held(shell, sentinel)
                    if state["parent_mode"] == "control_wait":
                        held = shell.manual_input_hold()
                        self.assertTrue(held and "control wait" in held, held)
                        with self.assertRaises(UnsafeShellState):
                            shell.send_user(b"x")
                        shell.request_takeover()
                        self.clean_prompt(shell)
                    else:
                        self.clean_prompt(shell)
                    self.assertEqual(shell.snapshot()["input_owner"], "user")
                    self.handoff_and_run_new(shell, d, "after-edit")
                    self.assertFalse(sentinel.exists())


class ManualInputToForegroundProgramTests(ShellCase):
    """Programs the user starts: data, editing and Ctrl-C/D/Z reach them."""

    def test_cat_receives_edited_data_and_eof(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                out, sentinel = d / "cat-out", d / "auto-sentinel"
                shell.send_user(f"cat > {shlex.quote(str(out))}\r".encode())
                self.until(shell, lambda s: s["parent_mode"] == "manual_foreground"
                           and members_named(shell.parent_pid, "cat"), what="cat fg")
                self.assertIsNone(shell.manual_input_hold())
                shell.send_user(b"ab\x7fc\n")          # canonical erase inside cat's tty read
                shell.send_user(b"junk\x15kept\n")      # canonical kill
                shell.send_user(b"\x1b[A\n")             # ESC sequence is data to cat
                self.wait_path(shell, out, "ac\nkept\n\x1b[A\n")
                self.assert_uncertain_held(shell, sentinel)
                shell.send_user(b"\x04")
                self.until(shell, lambda s: not members_named(shell.parent_pid, "cat"), what="cat exit")
                self.clean_prompt(shell)
                shell.send_user(b"printf more > " + shlex.quote(str(d / "more")).encode() + b"\r")
                self.wait_path(shell, d / "more", "more")
                self.clean_prompt(shell)
                self.handoff_and_run_new(shell, d, "cat")

    def test_python_repl_editing_ctrl_c_ctrl_z_fg_and_ctrl_d(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                out, sentinel = d / "py-out", d / "auto-sentinel"
                shell.send_user(f"{PYTHON3} -q\r".encode())
                self.until(shell, lambda s: s["parent_mode"] == "manual_foreground"
                           and members_named(shell.parent_pid, "python3"), what="python3 fg")
                self.settle(shell, 1.0)
                shell.send_user(f"garbage\x15open({str(out)!r}, 'w').write(str(40+3\x7f2))\r".encode())
                self.wait_path(shell, out, "42")
                shell.send_user(b"\x1b[A\x1b[B")
                shell.send_user(b"while True: pass\r\r")
                self.settle(shell, 0.5)
                shell.send_user(b"\x03")                 # KeyboardInterrupt, REPL survives
                self.settle(shell, 0.5)
                self.assertTrue(members_named(shell.parent_pid, "python3"))
                self.assert_uncertain_held(shell, sentinel)
                shell.send_user(b"\x1a")                 # suspend the REPL -> shell prompt
                self.prompt(shell)
                self.assertTrue(members_named(shell.parent_pid, "python3"))
                self.assert_automation_held(shell, sentinel)
                shell.send_user(b"fg\n")  # dash keeps a stopped readline's raw tty (no ICRNL)
                self.until(shell, lambda s: s["parent_mode"] == "manual_foreground", what="fg python3")
                self.settle(shell, 0.5)
                shell.send_user(f"open({str(out)!r}, 'a').write('!')\r".encode())
                self.wait_path(shell, out, "42!")
                shell.send_user(b"\x04")                 # EOF on an empty REPL line -> exit
                self.until(shell, lambda s: not members_named(shell.parent_pid, "python3"), what="python3 exit")
                self.clean_prompt(shell)
                self.handoff_and_run_new(shell, d, "python")

    def test_ctrl_c_and_ctrl_z_reach_sleep_and_nested_sh(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                sentinel = d / "auto-sentinel"
                for command in (b"sleep 30\r", b"sh -c 'sleep 30; echo never'\r"):
                    shell.send_user(command)
                    self.until(shell, lambda s: s["parent_mode"] == "manual_foreground"
                               and members_named(shell.parent_pid, "sleep"), what=repr(command))
                    shell.send_user(b"typed-ahead")      # unterminated tail to a non-reader
                    shell.send_user(b"\x03")             # ISIG flushes the queue and interrupts
                    self.until(shell, lambda s: not members_named(shell.parent_pid, "sleep"), what="sleep gone")
                    self.clean_prompt(shell)
                shell.send_user(b"sleep 30\r")
                self.until(shell, lambda s: members_named(shell.parent_pid, "sleep")
                           and s["parent_mode"] == "manual_foreground", what="sleep fg")
                shell.send_user(b"\x1a")
                self.prompt(shell)
                sleeper = members_named(shell.parent_pid, "sleep")
                self.assertTrue(sleeper)
                self.assertTrue(all(stat(pid)[0] in {"T", "t"} for pid in sleeper))
                self.assert_automation_held(shell, sentinel)
                shell.send_user(b"fg\n")  # dash keeps a stopped readline's raw tty (no ICRNL)
                self.until(shell, lambda s: s["parent_mode"] == "manual_foreground", what="fg sleep")
                shell.send_user(b"\x03")
                self.until(shell, lambda s: not members_named(shell.parent_pid, "sleep"), what="sleep gone")
                self.clean_prompt(shell)
                marker = d / "after"
                shell.send_user(f"printf ok > {shlex.quote(str(marker))}\r".encode())
                self.wait_path(shell, marker, "ok")
                self.clean_prompt(shell)
                self.handoff_and_run_new(shell, d, "sleep")

    def test_program_with_its_own_foreground_group_receives_ctrl_c(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                program = d / "own_group.py"
                program.write_text(OWN_GROUP_PROGRAM)
                out = d / "own-out"
                shell.send_user(f"{PYTHON3} {shlex.quote(str(program))} {shlex.quote(str(out))} run\r".encode())
                child_file = Path(str(out) + ".child")
                self.until(shell, lambda s: child_file.exists() and child_file.read_text()
                           and s["foreground_group"] == int(child_file.read_text()), what="child group fg")
                child = int(child_file.read_text())
                self.assertNotEqual(os.getpgid(child), os.getpgid(shell.parent_pid))
                self.assertIsNone(shell.manual_input_hold())
                shell.send_user(b"\x03")
                self.wait_path(shell, out, str(int(signal.SIGINT)))
                self.clean_prompt(shell)
                self.handoff_and_run_new(shell, d, "own-group")


class UnobservableTargetTests(ShellCase):
    """Target unknown -> manual input held with a visible reason (C-AC-08)."""

    def test_foreground_group_outside_the_session_is_held(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                m = d / "m"
                t = shell._transport
                t._foreground_group = lambda: os.getpgrp()   # a group of another session
                try:
                    held = shell.manual_input_hold()
                    self.assertTrue(held and "foreground process group" in held, held)
                    with self.assertRaises(UnsafeShellState):
                        shell.send_user(f"printf X > {shlex.quote(str(m))}\r".encode())
                finally:
                    del t._foreground_group
                self.settle(shell, 0.5)
                self.assertFalse(m.exists(), "held bytes reached the shell")
                shell.send_user(f"printf Y > {shlex.quote(str(m))}\r".encode())
                self.wait_path(shell, m, "Y")

    def test_stopped_foreground_group_is_unobservable_and_held(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                program = d / "own_group.py"
                program.write_text(OWN_GROUP_PROGRAM)
                out, sentinel = d / "own-out", d / "auto-sentinel"
                shell.send_user(f"{PYTHON3} {shlex.quote(str(program))} {shlex.quote(str(out))} stop\r".encode())
                child_file = Path(str(out) + ".child")
                self.until(shell, lambda _: child_file.exists() and child_file.read_text()
                           and (stat(int(child_file.read_text())) or ["?"])[0] in {"T", "t"}, what="child stopped")
                child = int(child_file.read_text())
                self.assertEqual(shell.snapshot()["foreground_group"], child)
                held = shell.manual_input_hold()
                self.assertTrue(held and "foreground process group" in held, held)
                with self.assertRaises(UnsafeShellState):
                    shell.send_user(b"\x03")
                self.assert_automation_held(shell, sentinel)
                self.assertTrue(signal_exact(child, stat(child)[19], signal.SIGKILL))
                self.wait_path(shell, out, str(int(signal.SIGKILL)))
                self.clean_prompt(shell)
                marker = d / "after"
                shell.send_user(f"printf ok > {shlex.quote(str(marker))}\r".encode())
                self.wait_path(shell, marker, "ok")

    def test_control_channel_loss_holds_manual_and_automatic_input(self):
        for choice in SHELLS:
            for how in (b"exec 9>&-\r", b"\x04"):
                with self.subTest(shell=choice.kind, how=how):
                    shell, d = self.open(choice)
                    sentinel = d / "auto-sentinel"
                    shell.send_user(how)
                    self.until(shell, lambda _: shell._transport.boundary.control_lost, what="control lost")
                    state = shell.poll(0)
                    self.assertEqual(state["phase"], "unknown")
                    held = shell.manual_input_hold()
                    self.assertTrue(held, held)
                    with self.assertRaises(UnsafeShellState):
                        shell.send_user(b"echo x\r")
                    self.assert_automation_held(shell, sentinel)
                    self.settle(shell, 1.0)
                    self.assertTrue(shell.manual_input_hold())

    def test_parent_identity_drift_holds_manual_input(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                m = d / "m"
                t = shell._transport
                # (a) recorded start time no longer matches (PID-reuse style drift).
                original = t._parent_start
                t._parent_start = str(int(original) + 1)
                try:
                    held = shell.manual_input_hold()
                    self.assertTrue(held, held)
                    with self.assertRaises(UnsafeShellState):
                        shell.send_user(f"printf X > {shlex.quote(str(m))}\r".encode())
                finally:
                    t._parent_start = original
                # (b) the real parent stopped: not a live target.
                start = stat(shell.parent_pid)[19]
                self.assertTrue(signal_exact(shell.parent_pid, start, signal.SIGSTOP))
                try:
                    self.until(shell, lambda _: stat(shell.parent_pid)[0] in {"T", "t"}, what="parent stopped")
                    held = shell.manual_input_hold()
                    self.assertTrue(held, held)
                    with self.assertRaises(UnsafeShellState):
                        shell.send_user(f"printf X > {shlex.quote(str(m))}\r".encode())
                finally:
                    signal_exact(shell.parent_pid, start, signal.SIGCONT)
                self.until(shell, lambda _: stat(shell.parent_pid)[0] not in {"T", "t"}, what="parent resumed")
                self.settle(shell, 0.3)
                self.assertFalse(m.exists(), "held bytes reached the shell")
                self.assertIsNone(shell.manual_input_hold())
                shell.send_user(f"printf Y > {shlex.quote(str(m))}\r".encode())
                self.wait_path(shell, m, "Y")


class RejectedHandoffTests(ShellCase):
    """Gap1: an incompatible hook rejects wb-handoff; the prompt's READY returns input."""

    def variants(self, choice: ShellChoice):
        if choice.kind == "bash":
            return ((b"PROMPT_COMMAND=\"$PROMPT_COMMAND; :\"\r", b"PROMPT_COMMAND='__b_emit READY'\r"),
                    (b"PS1='> '\r", b"PS1='$ '\r"),
                    (b"trap ':' CHLD\r", b"trap - CHLD\r"))
        return ((b"PS1=\"x$PS1\"\r", b"PS1=\"${PS1#x}\"\r"),
                (b"trap ':' CHLD\r", b"trap - CHLD\r"))

    def test_rejected_handoff_then_ready_returns_manual_input_and_keeps_automation_held(self):
        for choice in SHELLS:
            for breaker, repair in self.variants(choice):
                with self.subTest(shell=choice.kind, breaker=breaker):
                    shell, d = self.open(choice)
                    sentinel, m, out = d / "auto-sentinel", d / "m", d / "cat-out"
                    shell.send_user(breaker)
                    self.clean_prompt(shell)
                    shell.send_user(b"wb-handoff\r")
                    state = self.until(shell, lambda s: "unsupported_hook_or_trap" in s["held_reasons"]
                                       and s["parent_mode"] == "manual_prompt", what="rejected handoff")
                    self.assertEqual(state["input_owner"], "user")
                    self.assertIsNone(shell.manual_input_hold())
                    self.assert_automation_held(shell, sentinel)
                    # Editing keys and a foreground program work at the live prompt.
                    shell.send_user(f"printf A >> {shlex.quote(str(m))}X".encode() + erase_x(choice) + b"\r")
                    self.wait_path(shell, m, "A")
                    shell.send_user(f"cat > {shlex.quote(str(out))}\r".encode())
                    self.until(shell, lambda s: s["parent_mode"] == "manual_foreground", what="cat fg")
                    shell.send_user(b"x\x7fy\n\x04")
                    self.wait_path(shell, out, "y\n")
                    self.prompt(shell)
                    self.assert_automation_held(shell, sentinel)
                    shell.send_user(repair)
                    self.clean_prompt(shell)
                    self.handoff_and_run_new(shell, d, "repaired")
                    self.assertFalse(sentinel.exists())

    def test_rejected_handoff_outside_control_loop_without_ready_delivers_manual_input_holds_automation(self):
        # Expectation updated by p27-deadline-test-01 per Root adjudication
        # RA-P27-HOOK-REJECTED (.workflow/core-workbench/runs/implement-p2.6-20260927/
        # root-adjudication-p27-hook-rejected.json), superseding the former
        # "...without_any_prompt_ready_keeps_input_held_with_reason": the parent's
        # own authenticated HOOK_REJECTED (outside the control loop) is the rejected
        # wb-handoff's return to the interactive prompt, so manual input is delivered
        # to the observed prompt (target re-verified per write) even though no READY
        # follows; automation (submit/can_dispatch/claim_manager/handoff) stays held.
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                sentinel, m = d / "auto-sentinel", d / "m"
                breaker = b"PROMPT_COMMAND=\r" if choice.kind == "bash" else b"PS1='$ '\r"
                shell.send_user(breaker)
                self.settle(shell, 0.5)
                shell.send_user(b"wb-handoff\r")
                self.until(shell, lambda _: "HOOK_REJECTED" in shell._transport.events, what="rejected")
                state = self.settle(shell, 1.0)
                self.assertEqual(state["input_owner"], "user")
                self.assertFalse(shell._transport.control_wait_seen)
                self.assertIn("unsupported_hook_or_trap", state["held_reasons"])
                self.assertIsNone(shell.manual_input_hold())
                self.assert_automation_held(shell, sentinel)
                # Manual bytes (with editing keys) reach the observed prompt, twice,
                # although no READY ever confirms the consumed lines.
                shell.send_user(f"printf A >> {shlex.quote(str(m))}X".encode() + erase_x(choice) + b"\r")
                self.wait_path(shell, m, "A")
                self.assertIsNone(shell.manual_input_hold())
                shell.send_user(f"printf B >> {shlex.quote(str(m))}\r".encode())
                self.wait_path(shell, m, "AB")
                self.assertNotIn("READY", shell._transport.events[shell._transport.events.index("HOOK_REJECTED"):])
                # A second wb-handoff is rejected again; automation never runs.
                shell.send_user(b"wb-handoff\r")
                self.settle(shell, 1.0)
                self.assertFalse(shell._transport.control_wait_seen)
                self.assert_automation_held(shell, sentinel)
                self.assertFalse(sentinel.exists())


EXPECTED_RUN = "a\x1b[Dc\nkept\none three\n\x1b[A\x1bOB\t\n"  # canonical tty of the experiment
LONG = {"return_timeout": 120}  # isolates input semantics from the legacy 5 s bound (see deadline test)


class ConfirmedRunTakeoverTests(ShellCase):
    """Gap2: confirmed takeover of an automation-launched foreground run."""

    def start_run(self, shell, d: Path, script: str, **submit) -> str:
        shell.send_user(b"wb-handoff\r")
        self.until(shell, lambda s: s["parent_mode"] == "control_wait", what="control_wait")
        shell.claim_manager()
        control, automation = ports(shell)
        shell.submit(control, script, automation, **submit)
        self.until(shell, lambda s: s["lifecycle"]["experiment_started"], what="experiment_started")
        shell.request_takeover()
        confirmed = shell.confirm_takeover()
        self.assertIsNotNone(confirmed["foreground_target"])
        return control["payload"]["requestId"]

    def return_path(self, shell, choice, d: Path, old_request: str, starts: Path) -> None:
        self.until(shell, lambda s: s["lifecycle"]["input_barrier"], what="input_barrier")
        shell.release_input()
        self.until(shell, lambda s: s["lifecycle"]["control_returned"] and s["parent_mode"] == "manual_prompt",
                   what="manual prompt after run")
        self.assertEqual(shell.snapshot()["lifecycle"]["unknown"], [])
        self.assert_automation_held(shell, d / "auto-sentinel")
        m = d / "post"
        shell.send_user(f"printf P >> {shlex.quote(str(m))}X".encode() + erase_x(choice) + b"\r")
        self.wait_path(shell, m, "P")
        self.clean_prompt(shell)
        self.handoff_and_run_new(shell, d, "post-run")
        # The old request is never replayed, even when offered again verbatim.
        control, automation = ports(shell, old_request)
        with self.assertRaises(UnsafeShellState):
            shell.submit(control, "true", automation)
        self.settle(shell, 0.5)
        self.assertEqual(starts.read_text(), "S", "old experiment ran again")

    def test_editing_and_control_bytes_are_experiment_input(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                starts, out = d / "starts", d / "run-out"
                old = self.start_run(shell, d, f"printf S >> {shlex.quote(str(starts))}; "
                                               f"exec cat >> {shlex.quote(str(out))}", **LONG)
                t = shell._transport
                boundary, target = t.boundary, shell.snapshot()["foreground_target"]
                lines = boundary.submitted_lines
                for chunk in (b"a\x1b[Db\x7fc\n", b"junk\x15kept\n", b"one two\x17three\n", b"\x1b[A\x1bOB\t\n"):
                    shell.send_user(chunk)
                    state = self.settle(shell, 0.4)
                    self.assertEqual(state["lifecycle"]["unknown"], [], chunk)
                    self.assertEqual(state["foreground_target"], target, chunk)
                    self.assertEqual((boundary.uncertain, boundary.submitted_lines), (False, lines), chunk)
                self.wait_path(shell, out, EXPECTED_RUN)
                self.assertTrue(members_named(shell.parent_pid, "cat"), "experiment ended by manual input")
                shell.send_user(b"\x1a")
                # Ctrl-Z: the lifecycle reports a stop, not unknown; the run stays.
                state = self.until(shell, lambda s: s["lifecycle"]["main_stopped"]
                                   or s["foreground_target"] is None, what="stop observed")
                self.assertEqual(state["lifecycle"]["unknown"], [])
                self.assertTrue(members_named(shell.parent_pid, "cat"), "experiment was ended by takeover")
                self.assert_automation_held(shell, d / "auto-sentinel")
                cats = members_named(shell.parent_pid, "cat")
                for pid, start in cats.items():
                    signal_exact(pid, start, signal.SIGCONT)
                state = self.until(shell, lambda s: not s["lifecycle"]["main_stopped"], what="continued")
                self.assertEqual(state["lifecycle"]["unknown"], [])
                if state["foreground_target"] is None:
                    self.assertIsNotNone(shell.confirm_takeover()["foreground_target"])
                shell.send_user(b"last\n")
                self.wait_path(shell, out, EXPECTED_RUN + "last\n")
                self.assertEqual(shell.snapshot()["lifecycle"]["unknown"], [])
                shell.send_user(b"\x04")
                self.return_path(shell, choice, d, old, starts)

    def test_long_running_experiment_keeps_running_under_takeover_then_ctrl_c(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                starts = d / "starts"
                old = self.start_run(shell, d, f"printf S >> {shlex.quote(str(starts))}; exec sleep 30", **LONG)
                target = shell.snapshot()["foreground_target"]
                shell.send_user(b"\x1b[A\x7f\x15ignored")
                state = self.settle(shell, 2.0)
                self.assertEqual((state["foreground_target"], state["lifecycle"]["unknown"]), (target, []))
                self.assertIsNone(state["lifecycle"]["main_exit"])
                self.assertTrue(members_named(shell.parent_pid, "sleep"), "experiment ended without a signal")
                self.assert_automation_held(shell, d / "auto-sentinel")
                shell.send_user(b"\x03")
                state = self.until(shell, lambda s: s["lifecycle"]["main_exit"] is not None, what="main_exit")
                self.assertEqual(state["lifecycle"]["unknown"], [])
                self.return_path(shell, choice, d, old, starts)


    def test_production_default_submit_keeps_taken_over_experiment_running_past_5s(self):
        """BRIEF "실험 실행 시간 상한 없음"; OPERATING-CONTRACT §3 "이 대기 간격은 실험 실행
        시간 상한이 아니다"; C-AC-08 foreground takeover "실험을 그대로 두고".

        Uses PersistentShell.submit exactly as src/workbench/workflow/run.py does
        (no return_timeout argument).
        """
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                starts, out = d / "starts", d / "run-out"
                old = self.start_run(shell, d, f"printf S >> {shlex.quote(str(starts))}; "
                                               f"exec cat >> {shlex.quote(str(out))}")
                began = time.monotonic()
                expected = ""
                while time.monotonic() - began < 8.0:
                    state = self.settle(shell, 1.0)
                    self.assertEqual(state["lifecycle"]["unknown"], [],
                                     f"experiment lifecycle became unknown after {time.monotonic() - began:.1f}s")
                    self.assertTrue(members_named(shell.parent_pid, "cat"),
                                    f"experiment killed after {time.monotonic() - began:.1f}s")
                    shell.send_user(b"tick\n")
                    expected += "tick\n"
                self.wait_path(shell, out, expected)
                shell.send_user(b"\x04")
                self.return_path(shell, choice, d, old, starts)


class ShellInitTempDirTests(ShellCase):
    """The per-shell /tmp/cw03-g2-* directory after normal vs. abrupt ends."""

    def test_normal_close_removes_init_dir(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                directory = Path(tempfile.mkdtemp(prefix="p27-in-"))
                self.addCleanup(shutil.rmtree, directory, True)
                shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "HOME": str(directory)},
                                        choice=choice)
                init_dir = Path(shell._transport._init_dir.name)
                self.assertTrue(init_dir.name.startswith("cw03-g2-") and init_dir.exists())
                parent = shell.parent_pid
                shell.send_user(b"wb-handoff\r")
                self.until(shell, lambda s: s["parent_mode"] == "control_wait")
                self.close_verified(shell, parent, init_dir)

    def test_sigkilled_owner_leaves_init_dir_behind(self):
        """Attribution: only an owner that never runs close() leaves the directory."""
        code = ("import sys, time\n"
                "from workbench.terminal.shell_g2.prototype import ShellChoice\n"
                "from workbench.terminal.shell_persistent.adapter import PersistentShell\n"
                "s = PersistentShell(user_environment={'PATH': '/usr/bin:/bin'},"
                " choice=ShellChoice('bash', '/usr/bin/bash'))\n"
                "print(s._transport._init_dir.name, s.parent_pid, flush=True)\n"
                "time.sleep(60)\n")
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        owner = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, env=env,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        owner_start = stat(owner.pid)[19]
        init_dir = None
        try:
            line = owner.stdout.readline().decode().split()
            init_dir, parent = Path(line[0]), int(line[1])
            parent_start = stat(parent)[19]
            self.assertTrue(signal_exact(owner.pid, owner_start, signal.SIGKILL))
            owner.wait(10)
            self.assertTrue(init_dir.exists(), "abrupt owner death unexpectedly removed the init dir")
            # The orphaned shell (own session) is ours: exact-identity cleanup.
            signal_exact(parent, parent_start, signal.SIGKILL)
            deadline = time.monotonic() + 3
            while session_members(parent) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(session_members(parent), {})
        finally:
            if owner.poll() is None:
                signal_exact(owner.pid, owner_start, signal.SIGKILL)
                owner.wait(10)
            owner.stdout.close()
            if init_dir is not None and init_dir.name.startswith("cw03-g2-"):
                shutil.rmtree(init_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
