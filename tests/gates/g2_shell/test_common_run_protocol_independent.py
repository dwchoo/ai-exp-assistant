"""Independent runtime failures and scope boundaries for canonical RUN."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.gates.g2_shell import live_combined_boundary_probe as combined
from tests.gates.g2_shell.live_env_launch_probe import controlled
from tests.gates.g2_shell.test_control_probe import real_shells
from workbench.terminal.shell_g2 import lifecycle
from workbench.terminal.shell_g2.prototype import UnsafeShellState


@contextmanager
def supervisor_mutation(code):
    original = lifecycle.managed_controller_source
    wrapper = (f"import sys; sys.path.insert(0, {str(Path(lifecycle.__file__).resolve().parents[3])!r}); "
               "from workbench.terminal.shell_g2 import lifecycle as p; "
               f"exec({code!r}); sys.exit(p.run_supervisor(sys.argv[-2], sys.argv[-1]))")

    def source(shell):
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(lifecycle.__file__).resolve()))} --supervisor "
        replacement = f"{shlex.quote(sys.executable)} -c {shlex.quote(wrapper)} --supervisor "
        value = original(shell)
        if command not in value:
            raise AssertionError("fixed supervisor launch changed")
        return value.replace(command, replacement)

    with mock.patch.object(lifecycle, "managed_controller_source", side_effect=source):
        yield


class IndependentCommonRunProtocolTests(unittest.TestCase):
    def test_payload_cannot_consume_queued_control_or_recovery_descriptors(self):
        payload = r'''
import errno, json, os, select
def emit(text):
    os.write(9, (text + '\n').encode())
release = int(os.environ['WB_RELEASE_FD'])
os.fstat(release)
emit('FD_PROBE_READY')
os.read(0, 1)
inventory = {}
consumed = {}
for fd in (7, 8, 37, 38):
    try:
        os.fstat(fd)
        inventory[str(fd)] = 'open'
    except OSError as exc:
        inventory[str(fd)] = exc.errno
for fd in (37, 38):
    if inventory[str(fd)] == 'open' and select.select([fd], [], [], 0)[0]:
        consumed[str(fd)] = os.read(fd, 256).decode()
emit('FD_INVENTORY:' + json.dumps({'fds': inventory, 'consumed': consumed}))
child = os.fork()
if child == 0:
    os.setsid()
    emit('FD_RELEASE_WAIT:' + str(os.getpid()))
    ready = select.select([release], [], [], 3.0)[0]
    if ready and os.read(release, 1) == b'R':
        emit('FD_RELEASE_OK')
        os._exit(0)
    os._exit(70)
os._exit(0)
'''
        for choice in real_shells():
            for leaked in (False, True):
                mutation = "p.os.dup2(8,37); p.os.dup2(7,38)"
                if leaked:
                    mutation += "; p._close_child_fds=lambda keep: None"
                with self.subTest(shell=choice.kind, leaked=leaked), tempfile.TemporaryDirectory() as directory, \
                     supervisor_mutation(mutation), controlled(choice.executable, directory) as (session, parent, *_):
                    session.dispatch_managed("fd-boundary", [sys.executable, "-c", payload])
                    combined.wait(session, "FD_PROBE_READY", 0)
                    # Queue traffic while the prepared payload is alive. Its
                    # inherited aliases must not give it access to either pipe.
                    os.write(session._request_fd, b"TAKEOVER\n")
                    os.write(session._recovery_fd, b"RECOVERY_SENTINEL\n")
                    session._write_all(b"go\n")
                    event = combined.wait(session, "FD_INVENTORY:", 0)
                    report = json.loads(event.partition(":")[2])
                    expected = {str(fd): 9 for fd in (7, 8, 37, 38)}  # EBADF
                    if leaked:
                        with self.assertRaises(AssertionError):
                            self.assertEqual(report["fds"], expected)
                        self.assertEqual(report["consumed"], {
                            "37": "TAKEOVER\n", "38": "RECOVERY_SENTINEL\n"})
                    else:
                        self.assertEqual(report["fds"], expected)
                        self.assertEqual(report["consumed"], {})
                    descendant = int(combined.wait(session, "FD_RELEASE_WAIT:", 0).split(":")[1])
                    combined.wait(session, "LIFETIME_ACTIVE:", 0)
                    os.kill(session.lifecycle.supervisor_pid, signal.SIGUSR2)
                    combined.wait(session, "FD_RELEASE_OK", 0)
                    combined.wait(session, "INPUT_BARRIER", 0)
                    session.release_input()
                    combined.wait(session, "RETURN:0", 0)
                    if leaked:
                        # The negative payload stole both frames; provide new
                        # frames solely to finish and clean this owned fixture.
                        session.release_control()
                        os.write(session._recovery_fd, b"NEGATIVE_CLEANUP\n")
                    combined.wait(session, f"TAKEOVER_ACK:{parent}", 0)
                    combined.wait(session, "READY", session.events.index("INPUT_BARRIER"))
                    session._write_all(b"IFS= read -r recovered <&7; __b_emit RECOVERED:$recovered\n")
                    recovered = "NEGATIVE_CLEANUP" if leaked else "RECOVERY_SENTINEL"
                    combined.wait(session, f"RECOVERED:{recovered}", 0)
                    self.assertTrue(session.manual_prompt_confirmed)
                    self.assertFalse(session.lifecycle.unknown)
                self.assert_clean(session)
                self.assertFalse(Path(f"/proc/{descendant}").exists(), descendant)

    def assert_held(self, session):
        self.assertTrue(session.lifecycle.unknown)
        self.assertFalse(session.lifecycle.returned)
        for action in (session.release_input, session.release_control,
                       lambda: session.dispatch_managed("replacement", ":")):
            with self.assertRaises((UnsafeShellState, OSError)):
                action()
        self.assertEqual(session.events.count("ACCEPT"), 1)

    def assert_clean(self, session):
        for pid in (session.pid, session.lifecycle.supervisor_pid, session.lifecycle.child_pid):
            if pid is not None:
                self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def finish(self, session):
        combined.wait(session, "INPUT_BARRIER", 0)
        session.release_input()
        combined.wait(session, "INPUT_RELEASED", 0)
        combined.wait(session, "RETURN:", 0)
        session.release_control()
        combined.wait(session, "TAKEOVER_ACK:", 0)
        combined.wait(session, "READY", session.events.index("INPUT_BARRIER"))

    def test_literal_metacharacters_and_complex_script_stay_in_child_scope(self):
        for choice in real_shells():
            for kind in ("argv", "script"):
                with self.subTest(shell=choice.kind, kind=kind), tempfile.TemporaryDirectory() as directory:
                    marker = Path(directory) / "parent-reinterpretation"
                    child_directory = Path(directory) / "child"
                    child_directory.mkdir()
                    with controlled(choice.executable, directory) as (session, parent, *_):
                        if kind == "argv":
                            literal = f"$(touch {marker}); `touch {marker}` | > {marker} &"
                            payload = [sys.executable, "-c",
                                       "import os,sys; os.write(9, ('LITERAL:'+sys.argv[1]+'\\n').encode())", literal]
                            expected = "LITERAL:" + literal
                        else:
                            payload = (f"cd {shlex.quote(str(child_directory))}; export BOUNDARY_PREPARED=child; "
                                       f"printf '%s' \"$(printf '%s' \"$$\")\" | cat > {shlex.quote(str(marker))}; "
                                       "printf 'CHILD_SCOPE:%s:%s:%s\\n' \"$$\" \"$PPID\" \"$BOUNDARY_PREPARED\" >&9")
                            expected = "CHILD_SCOPE:"
                        session.dispatch_managed("scope", payload)
                        event = combined.wait(session, expected, 0)
                        self.finish(session)
                        self.assertEqual(session.pid, parent)
                        self.assertEqual(os.readlink(f"/proc/{parent}/cwd"), directory)
                        if kind == "argv":
                            self.assertFalse(marker.exists())
                        else:
                            child, supervisor, value = event.split(":")[1:]
                            self.assertEqual(int(child), session.lifecycle.child_pid)
                            self.assertEqual(int(supervisor), session.lifecycle.supervisor_pid)
                            self.assertEqual(value, "child")
                            self.assertEqual(marker.read_text(), child)
                        session._write_all(b"__b_emit PARENT_ENV:$BOUNDARY_PREPARED\n")
                        combined.wait(session, "PARENT_ENV:kept", session.events.index("INPUT_BARRIER"))
                    self.assert_clean(session)

    def test_failed_foreground_readback_cannot_release_or_execute_prepared_child(self):
        for choice in real_shells():
            for mode in ("missing", "wrong"):
                mutation = ("def readback(fd):\n    raise OSError('readback unavailable')\np.os.tcgetpgrp=readback"
                            if mode == "missing" else "p.os.tcgetpgrp=lambda fd: -1")
                with self.subTest(shell=choice.kind, mode=mode), tempfile.TemporaryDirectory() as directory, \
                     supervisor_mutation(mutation), controlled(choice.executable, directory) as (session, *_):
                    marker = Path(directory) / "executed"
                    session.dispatch_managed("readback", [sys.executable, "-c", f"open({str(marker)!r},'w').close()"])
                    combined.wait(session, "RETURN:", 0)
                    self.assertTrue(session.lifecycle.child_prepared)
                    self.assertFalse(marker.exists())
                    self.assertFalse(any(event.startswith(("EXEC_READY:", "EXPERIMENT_START:", "LIFETIME_DONE:"))
                                         for event in session.events))
                    self.assert_held(session)
                self.assert_clean(session)

    def test_missing_or_wrong_exec_event_cannot_grant_completion(self):
        original = lifecycle.ManagedLifecycleProbe._on_control_event
        for choice in real_shells():
            for mode in ("missing", "wrong"):
                def corrupt(session, event):
                    if event.startswith("EXEC_READY:"):
                        if mode == "missing":
                            return
                        event = f"EXEC_READY:{session.pid}"
                    original(session, event)

                with self.subTest(shell=choice.kind, mode=mode), tempfile.TemporaryDirectory() as directory, \
                     mock.patch.object(lifecycle.ManagedLifecycleProbe, "_on_control_event", corrupt), \
                     controlled(choice.executable, directory) as (session, *_):
                    session.dispatch_managed("ack", ":")
                    combined.wait(session, "INPUT_BARRIER", 0)
                    self.assertFalse(session.lifecycle.exec_ready)
                    self.assertFalse(session.lifecycle.experiment_started)
                    self.assertIsNone(session.lifecycle.main_exit)
                    self.assert_held(session)
                self.assert_clean(session)

    def test_child_normal_exit_before_exec_is_not_successful_exec_ack(self):
        mutation = "p.os.execvpe=lambda *args: p.os._exit(72)"
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory, \
                 supervisor_mutation(mutation), controlled(choice.executable, directory) as (session, *_):
                marker = Path(directory) / "never-executed"
                session.dispatch_managed("preexec-exit", [sys.executable, "-c", f"open({str(marker)!r},'w').close()"])
                # Drain until either a failure return or an incorrectly granted
                # input boundary makes the missing exec fact observable.
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    session._drain(0.02)
                    if any(e.startswith("RETURN:") or e == "INPUT_BARRIER" for e in session.events):
                        break
                self.assertFalse(marker.exists())
                self.assertFalse(session.lifecycle.exec_ready, session.events)
                self.assertFalse(session.lifecycle.experiment_started, session.events)
                self.assert_held(session)
            self.assert_clean(session)

    def test_supervisor_and_control_loss_hold_unknown_and_no_replay(self):
        for choice in real_shells():
            for fault in ("supervisor", "control"):
                with self.subTest(shell=choice.kind, fault=fault), tempfile.TemporaryDirectory() as directory, \
                     controlled(choice.executable, directory) as (session, *_):
                    session.dispatch_managed("lost", "#WB_START_HOLD\n:")
                    combined.wait(session, "START_BARRIER", 0)
                    supervisor = session.lifecycle.supervisor_pid
                    if fault == "supervisor":
                        os.kill(supervisor, signal.SIGKILL)
                        combined.wait(session, "RETURN:", 0)
                    else:
                        os.close(session._control_fd)
                        with self.assertRaises(OSError):
                            session._drain(0)
                    self.assert_held(session)
                    self.assertFalse(session.lifecycle.experiment_started)
                    self.assertFalse(session.lifecycle.input_returned)
                self.assert_clean(session)


if __name__ == "__main__":
    unittest.main()
