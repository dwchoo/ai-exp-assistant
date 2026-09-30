"""Independent input, observer-loss and cleanup split-PG regressions."""

from pathlib import Path
from contextlib import contextmanager
import os
import shlex
import shutil
import sys
import unittest
from unittest import mock

from tests.gates.g2_shell import live_combined_boundary_probe as probe
from tests.gates.g2_shell.live_hook_env_probe import own_orphans


@contextmanager
def supervisor_variant(code):
    """Run a test mutation in the actual supervisor process without file edits."""
    original = probe.controller_source
    wrapper = (f"import sys; sys.path.insert(0, {str(probe.ROOT)!r}); "
               "from tests.gates.g2_shell import live_combined_boundary_probe as p; "
               f"exec({code!r}); "
               "sys.exit(p.split_supervisor(sys.argv[-2], sys.argv[-1]))")

    def source(shell):
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(probe.__file__).resolve()))} --supervisor "
        replacement = f"{shlex.quote(sys.executable)} -c {shlex.quote(wrapper)} --supervisor "
        value = original(shell)
        if command not in value:
            raise AssertionError("supervisor launch command changed")
        return value.replace(command, replacement)

    with mock.patch.object(probe, "controller_source", side_effect=source):
        yield


class SplitProcessGroupInputReturnTest(unittest.TestCase):
    def shells(self):
        if sys.platform != "linux":
            self.skipTest("Linux PTY and /proc required")
        shells = [shutil.which(name) for name in ("bash", "dash")]
        if any(shell is None for shell in shells):
            self.skipTest("Bash and dash required")
        return shells

    def test_default_foreground_quit_loses_observer_while_descendant_is_live(self):
        mutation = """
original_signal = p.signal.signal
def leave_quit_default(signum, handler):
    if signum == p.signal.SIGQUIT:
        return original_signal(signum, p.signal.SIG_DFL)
    return original_signal(signum, handler)
p.signal.signal = leave_quit_default
"""
        for shell in self.shells():
            with self.subTest(shell=shell), own_orphans() as orphans:
                original_wait = probe.wait
                observed = {}

                def capture(session, prefix, since):
                    observed["session"] = session
                    if prefix == "NO_TARGET:return:SIGQUIT":
                        original_wait(session, "RETURN:131", since)
                        descendant = observed["descendant"]
                        self.assertNotIn(probe.proc_fields(descendant)[3], {"Z", "X"})
                        events = session.events[since:]
                        self.assertFalse(any(event.startswith(("UNKNOWN:", "LIFETIME_DONE:",
                                                               "INPUT_RELEASED")) for event in events))
                        raise RuntimeError("observer lost with live setsid descendant")
                    event = original_wait(session, prefix, since)
                    if prefix == "DESCENDANT:":
                        pid, birth = map(int, event.split(":")[1:3])
                        orphans.append((pid, birth))
                        observed["descendant"] = pid
                    return event

                with supervisor_variant(mutation), mock.patch.object(probe, "wait", side_effect=capture):
                    with self.assertRaisesRegex(RuntimeError, "observer lost with live setsid descendant"):
                        probe.split_case(shell, "quit_return")
            self.assertFalse(Path(f"/proc/{observed['descendant']}").exists())

            with self.subTest(shell=shell, corrected=True):
                observed = {}
                original_wait = probe.wait

                def live_observer(session, prefix, since):
                    event = original_wait(session, prefix, since)
                    if prefix == "SUPERVISOR_PING:return":
                        sup = next(event for event in session.events if event.startswith("SUPERVISOR:"))
                        self.assertNotIn(probe.proc_fields(int(sup.split(":")[1]))[3], {"T", "Z", "X"})
                        self.assertNotIn("INPUT_BARRIER", session.events)
                        observed["ping"] = event
                    return event

                with mock.patch.object(probe, "wait", side_effect=live_observer):
                    result = probe.split_case(shell, "quit_return")
                self.assertEqual(observed["ping"], "SUPERVISOR_PING:return")
                self.assertEqual(result["main_return"], "MAIN_RETURN:17")

    def test_numeric_and_signal_statuses_have_same_parent_code_but_distinct_main_facts(self):
        for shell in self.shells():
            for name, number in (("int", 130), ("quit", 131)):
                with self.subTest(shell=shell, signal=name):
                    terminated = probe.split_case(shell, f"{name}_running")
                    numeric = probe.split_case(shell, f"exit{number}")
                    self.assertEqual(terminated["parent_return"], numeric["parent_return"])
                    self.assertEqual(numeric["main_return"], f"MAIN_RETURN:{number}")
                    self.assertEqual(terminated["main_return"], f"MAIN_RETURN:{128 - number}")
                    self.assertNotEqual(terminated["main_return"], numeric["main_return"])

    def test_single_cleanup_snapshot_misses_adopted_sets_id_descendant(self):
        mutation = """
def single_snapshot():
    import time
    children = p.Path(f'/proc/{p.os.getpid()}/task/{p.os.getpid()}/children')
    initial = children.read_text().split()
    p.emit('NEGATIVE_FIRST_SCAN:' + ','.join(initial))
    for pid in initial:
        p.os.kill(int(pid), p.signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        p.os.waitpid(-1, p.os.WNOHANG)
        later = children.read_text().split()
        if any(pid not in initial for pid in later):
            p.emit('NEGATIVE_RESIDUAL:' + ','.join(later))
            return
        time.sleep(0.005)
    raise RuntimeError('negative control failed to expose adoption')
p.reap_split_failure = single_snapshot
"""
        for shell in self.shells():
            with self.subTest(shell=shell), own_orphans() as orphans:
                original_wait = probe.wait
                observed = {}

                def capture(session, prefix, since):
                    if prefix.startswith("CLEANUP_REAPED:"):
                        first = original_wait(session, "NEGATIVE_FIRST_SCAN:", since)
                        original_wait(session, "NEGATIVE_RESIDUAL:", since)
                        descendant = observed["descendant"]
                        self.assertNotIn(str(descendant), first.split(":")[1].split(","))
                        self.assertNotIn(probe.proc_fields(descendant)[3], {"Z", "X"})
                        self.assertIn("UNKNOWN:INJECTED_FAILURE", session.events)
                        self.assertNotIn("CLEANUP_EMPTY:ECHILD", session.events)
                        raise RuntimeError("single snapshot retained live descendant")
                    event = original_wait(session, prefix, since)
                    if prefix == "FAILURE_UNREGISTERED:":
                        pid, birth = map(int, event.split(":")[1:])
                        middle = probe.proc_fields(pid)[0]
                        orphans.extend([(middle, probe.proc_fields(middle)[2]), (pid, birth)])
                        observed["descendant"] = pid
                    return event

                with supervisor_variant(mutation), mock.patch.object(probe, "wait", side_effect=capture):
                    with self.assertRaisesRegex(RuntimeError, "single snapshot retained live descendant"):
                        probe.split_failure_case(shell)
            for pid, _ in orphans:
                self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

            # The same real tree must be rediscovered after the first snapshot.
            result = probe.split_failure_case(shell)
            events = result["events"]
            descendant = result["owned_pids"][-1]
            scans = [event.split(":")[1].split(",") for event in events
                     if event.startswith("CLEANUP_SCAN:")]
            self.assertNotIn(str(descendant), scans[0])
            self.assertTrue(any(str(descendant) in scan for scan in scans[1:]))
            self.assertLess(events.index("UNKNOWN:INJECTED_FAILURE"),
                            events.index(f"CLEANUP_REAPED:{descendant}"))
            self.assertNotIn("LIFETIME_DONE:INTERRUPTS=0", events)
            for pid in result["owned_pids"]:
                self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    @unittest.skipUnless(sys.platform == "linux", "Linux PTY and /proc required")
    def test_missing_flush_exposes_real_marker_after_parent_reads_new_input(self):
        for shell_name in ("bash", "dash"):
            shell = shutil.which(shell_name)
            if shell is None:
                self.skipTest(f"{shell_name} required")
            with self.subTest(shell=shell_name):
                observed = {}
                original_wait = probe.wait
                original_write = probe.boundary.prototype.ShellProcess._write_all

                def queued_line(session, data):
                    # A complete stale key/paste command must be detected even
                    # when it does not swallow the later normal input line.
                    if data.startswith(b": > ") and not data.endswith(b"\n"):
                        data += b"\n"
                    original_write(session, data)

                def capture(session, prefix, since):
                    observed["session"] = session
                    event = original_wait(session, prefix, since)
                    if prefix == "NEW_INPUT":
                        directory = Path(os.readlink(f"/proc/{session.pid}/cwd"))
                        observed["spill"] = (directory / "spill").exists()
                        observed["following"] = (directory / "following").exists()
                        observed["new_input"] = event
                    return event

                with mock.patch.object(probe.boundary, "flush_queued_pty"), \
                     mock.patch.object(probe, "wait", side_effect=capture), \
                     mock.patch.object(probe.boundary.prototype.ShellProcess,
                                       "_write_all", queued_line):
                    with self.assertRaises(AssertionError):
                        probe.split_case(shell, "exit148")
                self.assertEqual(observed.get("new_input"), "NEW_INPUT")
                self.assertTrue(observed["spill"], "negative control produced no real marker")
                self.assertTrue(observed["following"], "queued handoff line never reached parent")
                session = observed["session"]
                owned = [session.pid] + [int(event.split(":")[1])
                                        for event in session.events
                                        if event.startswith(("SUPERVISOR:", "CHILD:"))]
                for pid in owned:
                    self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

                # Restore the real boundary, then require the same parent-read
                # sequence to succeed with exit 148 retained as an exit status.
                with mock.patch.object(probe.boundary.prototype.ShellProcess,
                                       "_write_all", queued_line):
                    result = probe.split_case(shell, "exit148")
                self.assertEqual(result["main_return"], "MAIN_RETURN:148")
                self.assertEqual(result["parent_return"], "RETURN:148")
                self.assertEqual(result["stop_continue_cycles"], [])
                for pid in result["owned_pids"]:
                    self.assertFalse(Path(f"/proc/{pid}").exists(), pid)


if __name__ == "__main__":
    unittest.main()
