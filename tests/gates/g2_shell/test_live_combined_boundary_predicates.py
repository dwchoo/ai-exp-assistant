"""Negative controls for the combined CW-03 input-return barrier."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sys
import time
import unittest
from unittest import mock

from tests.gates.g2_shell import live_combined_boundary_probe as probe


class CombinedInputReturnPredicatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shell = shutil.which("bash")
        if sys.platform != "linux" or cls.shell is None:
            raise unittest.SkipTest("Linux and Bash required")

    def test_missing_both_flushes_exposes_stale_input(self) -> None:
        with mock.patch.object(probe.boundary, "flush_queued_pty") as flush:
            with self.assertRaisesRegex(AssertionError, "input control mismatch"):
                probe.case(self.shell, negative=False)
        self.assertGreaterEqual(flush.call_count, 1)

    def test_child_return_and_exec_keep_parent_and_input_barriers(self) -> None:
        """Current child execution must reject the legacy eval escape mechanism."""
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for name in ("return", "exec"):
                with self.subTest(shell=shell, case=name):
                    self.assertIsNotNone(shell)
                    result = probe.simple_case(shell, name)
                    self.assertFalse(result["marker_spilled"])
                    self.assertTrue(result["trap_restored"])
                    self.assertTrue(result["main_return"].startswith("MAIN_RETURN:"))
                    for pid in result["owned_pids"]:
                        self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_current_control_wait_rejects_incompatible_prompt_and_trap(self) -> None:
        """No prompt readiness snapshot may bypass current admission checks."""
        from tests.gates.g2_shell import live_env_launch_probe as env

        for shell in (shutil.which("bash"), shutil.which("dash")):
            for kind in ("prompt", "trap"):
                with self.subTest(shell=shell, kind=kind):
                    self.assertIsNotNone(shell)
                    result = env.incompatible_case(shell, kind)
                    self.assertTrue(result["held_before_run"])
                    self.assertTrue(result["parent_pid_preserved"])

    def test_missing_child_ready_never_sends_interrupt(self) -> None:
        writes: list[bytes] = []
        original_write = probe.boundary.prototype.ShellProcess._write_all
        original_wait = probe.wait

        def record_write(session: object, data: bytes) -> None:
            writes.append(data)
            original_write(session, data)

        def hide_child_ready(session: object, prefix: str, since: int) -> str:
            if prefix == "SCRIPT_READY:":
                raise TimeoutError("child readiness unavailable")
            return original_wait(session, prefix, since)

        with mock.patch.object(probe.boundary.prototype.ShellProcess, "_write_all",
                               record_write), mock.patch.object(
                                   probe, "wait", side_effect=hide_child_ready):
            with self.assertRaisesRegex(TimeoutError, "child readiness unavailable"):
                probe.simple_case(self.shell, "interrupt")
        self.assertNotIn(b"\x03", writes)

    def test_unrelated_live_pid_cannot_claim_child_ready(self) -> None:
        writes: list[bytes] = []
        original_write = probe.boundary.prototype.ShellProcess._write_all
        original_wait = probe.wait

        def record_write(session: object, data: bytes) -> None:
            writes.append(data)
            original_write(session, data)

        def wrong_child_ready(session: object, prefix: str, since: int) -> str:
            event = original_wait(session, prefix, since)
            if prefix == "SCRIPT_READY:":
                return f"SCRIPT_READY:{os.getpid()}"
            return event

        with mock.patch.object(probe.boundary.prototype.ShellProcess, "_write_all",
                               record_write), mock.patch.object(
                                   probe, "wait", side_effect=wrong_child_ready):
            with self.assertRaises(AssertionError):
                probe.simple_case(self.shell, "interrupt")
        self.assertNotIn(b"\x03", writes)

    def test_early_release_signals_are_consumed_once_each(self) -> None:
        ready_read, ready_write = os.pipe()
        go_read, go_write = os.pipe()
        result_read, result_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(ready_read)
            os.close(go_write)
            os.close(result_read)
            try:
                previous = signal.pthread_sigmask(
                    signal.SIG_BLOCK, {signal.SIGUSR1, signal.SIGUSR2}
                )
                os.write(ready_write, b"R")
                if os.read(go_read, 1) != b"G":
                    os._exit(2)
                probe.await_release(signal.SIGUSR2)
                probe.await_release(signal.SIGUSR1)
                with mock.patch.object(probe, "TIMEOUT", 0.02):
                    try:
                        probe.await_release(signal.SIGUSR1)
                    except TimeoutError:
                        pass
                    else:
                        os._exit(3)
                blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
                if blocked != previous | {signal.SIGUSR1, signal.SIGUSR2}:
                    os._exit(4)
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
                if signal.pthread_sigmask(signal.SIG_BLOCK, set()) != previous:
                    os._exit(6)
                os.write(result_write, b"D")
                os._exit(0)
            except BaseException:
                os._exit(5)

        os.close(ready_write)
        os.close(go_read)
        os.close(result_write)
        reaped = False
        try:
            readable, _, _ = select.select([ready_read], [], [], 2.0)
            self.assertTrue(readable, "child did not install the signal mask")
            self.assertEqual(os.read(ready_read, 1), b"R")
            os.kill(pid, signal.SIGUSR1)
            os.kill(pid, signal.SIGUSR2)
            os.write(go_write, b"G")
            readable, _, _ = select.select([result_read], [], [], 2.0)
            self.assertTrue(readable, "early release signal was lost")
            self.assertEqual(os.read(result_read, 1), b"D")
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    reaped = True
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                    break
                time.sleep(0.01)
            self.assertTrue(reaped, "signal receiver did not exit")
        finally:
            if not reaped:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
            for fd in (ready_read, go_write, result_read):
                os.close(fd)

    def test_parent_wait_ctrl_c_stays_in_control_and_restores_int_trap(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for name in ("parent_wait", "parent_wait_prior"):
                for delay in (0.0, 0.05):
                    with self.subTest(shell=shell, name=name, ping_delay=delay):
                        self.assertIsNotNone(shell)
                        observed: dict[str, object] = {}
                        original_wait = probe.wait
                        original_os_write = os.write

                        def capture_wait(session: object, prefix: str, since: int) -> str:
                            observed["session"] = session
                            event = original_wait(session, prefix, since)
                            if prefix == "NEW_INPUT":
                                observed["events"] = session.events
                                directory = Path(os.readlink(f"/proc/{session.pid}/cwd"))
                                observed["traps_before"] = (directory / "traps-before").read_bytes()
                                observed["traps_after"] = (directory / "traps-after").read_bytes()
                            return event

                        def observe_before_ping(fd: int, data: bytes) -> int:
                            if data == b"PING\n":
                                session = observed["session"]
                                deadline = time.monotonic() + delay
                                while time.monotonic() < deadline:
                                    session._drain(min(0.01, deadline - time.monotonic()))
                                    session.display_bytes()
                                events = session.events
                                observed["events_before_ping"] = events
                                observed["marker_before_ping"] = (
                                    Path(os.readlink(f"/proc/{session.pid}/cwd")) / "spill"
                                ).exists()
                            return original_os_write(fd, data)

                        with mock.patch.object(probe, "wait", side_effect=capture_wait), \
                             mock.patch.object(probe.os, "write",
                                               side_effect=observe_before_ping):
                            result = probe.simple_case(shell, name)

                        before_ping = observed["events_before_ping"]
                        events = observed["events"]
                        self.assertTrue(any(e.startswith("HANDOFF:") for e in events))
                        self.assertLess(events.index("START"), events.index("PARENT_INT"))
                        self.assertNotIn("READY", before_ping[before_ping.index("START") + 1:])
                        self.assertNotIn("BAD_REQUEST", before_ping)
                        self.assertFalse(observed["marker_before_ping"])
                        self.assertEqual(events.count("START"), 1)
                        self.assertEqual(events.count("BAD_REQUEST"), 1)
                        ack = next(i for i, e in enumerate(events)
                                   if e.startswith("TAKEOVER_ACK:"))
                        self.assertLess(events.index("BAD_REQUEST"), ack)
                        self.assertGreater(events.index("READY", ack), ack)
                        self.assertEqual(events.count("PARENT_INT"), 1)
                        self.assertEqual(observed["traps_before"], observed["traps_after"])
                        if name == "parent_wait_prior":
                            self.assertIn(b"PRIOR_INT", observed["traps_before"])
                            self.assertLess(events.index("PRIOR_INT"), events.index("NEW_INPUT"))
                        else:
                            self.assertNotIn("PRIOR_INT", events)
                            self.assertGreaterEqual(events[ack:].count("READY"), 2)
                        self.assertFalse(result["marker_spilled"])
                        for pid in result["owned_pids"]:
                            self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_running_child_ctrl_c_keeps_no_tail_and_reap_barriers(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                self.assertIsNotNone(shell)
                observed: list[str] = []
                original_wait = probe.wait

                def capture_wait(session: object, prefix: str, since: int) -> str:
                    event = original_wait(session, prefix, since)
                    if prefix == "NEW_INPUT":
                        observed.extend(session.events[since:])
                    return event

                with mock.patch.object(probe, "wait", side_effect=capture_wait):
                    result = probe.simple_case(shell, "interrupt")
                ordered = ["START", "SIGNAL_INT", "MAIN_RETURN:-2",
                           "WAIT_EMPTY:ECHILD", "LIFETIME_DONE:INTERRUPTS=1",
                           "INPUT_BARRIER", "INPUT_RELEASED", "RETURN:130", "READY",
                           "NEW_INPUT"]
                self.assertEqual([observed.index(event) for event in ordered],
                                 sorted(observed.index(event) for event in ordered))
                child_ready = next(e for e in observed if e.startswith("SCRIPT_READY:"))
                self.assertLess(observed.index(child_ready), observed.index("SIGNAL_INT"))
                self.assertFalse(result["tail_executed"])
                for pid in result["owned_pids"]:
                    self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_errexit_preserves_control_status_and_preexisting_int_trap(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for name, expected in (("errexit", ("MAIN_RETURN:37", "RETURN:37")),
                                   ("errexit_interrupt", ("MAIN_RETURN:-2", "RETURN:130"))):
                with self.subTest(shell=shell, name=name):
                    self.assertIsNotNone(shell)
                    observed: dict[str, object] = {}
                    original_write = probe.boundary.prototype.ShellProcess._write_all
                    original_wait = probe.wait
                    original_os_write = os.write

                    def prepared_with_trap(session: object, data: bytes) -> None:
                        if b"set -e; wb-handoff\n" in data:
                            data = data.replace(
                                b"set -e; wb-handoff\n",
                                b"trap '__b_emit PRIOR_INT' INT; set -e; wb-handoff\n",
                            )
                            observed["prepared"] = True
                        elif data == b"__b_emit NEW_INPUT\n":
                            data = b"kill -INT $$; __b_emit NEW_INPUT\n"
                        original_write(session, data)

                    def capture_wait(session: object, prefix: str, since: int) -> str:
                        event = original_wait(session, prefix, since)
                        if prefix == "NEW_INPUT":
                            observed["events"] = session.events[since:]
                            observed["parent"] = session.pid
                            directory = Path(os.readlink(f"/proc/{session.pid}/cwd"))
                            observed["traps_before"] = (directory / "traps-before").read_bytes()
                            observed["traps_after"] = (directory / "traps-after").read_bytes()
                        return event

                    def ping_snapshot(fd: int, data: bytes) -> int:
                        if data == b"PING\n":
                            observed["before_ping"] = observed["session"].events
                        return original_os_write(fd, data)

                    def retain_session(session: object, prefix: str, since: int) -> str:
                        observed["session"] = session
                        return capture_wait(session, prefix, since)

                    with mock.patch.object(probe.boundary.prototype.ShellProcess,
                                           "_write_all", prepared_with_trap), \
                         mock.patch.object(probe, "wait", side_effect=retain_session), \
                         mock.patch.object(probe.os, "write", side_effect=ping_snapshot):
                        result = probe.simple_case(shell, name)

                    self.assertTrue(observed.get("prepared"))
                    events = observed["events"]
                    self.assertEqual((result["main_return"], result["parent_return"]),
                                     expected)
                    self.assertIn("e", result["errexit_flags"].split(":", 1)[1])
                    self.assertNotIn("BAD_REQUEST", observed["before_ping"])
                    self.assertEqual(events.count("START"), 1)
                    self.assertEqual(events.count("BAD_REQUEST"), 1)
                    self.assertNotIn("READY", events[:events.index("BAD_REQUEST")])
                    ack = next(i for i, event in enumerate(events)
                               if event.startswith("TAKEOVER_ACK:"))
                    self.assertLess(events.index("BAD_REQUEST"), ack)
                    self.assertLess(ack, events.index("PRIOR_INT"))
                    self.assertLess(events.index("PRIOR_INT"), events.index("NEW_INPUT"))
                    self.assertNotIn("PARENT_INT", events)
                    self.assertIn(b"PRIOR_INT", observed["traps_before"])
                    self.assertEqual(observed["traps_before"], observed["traps_after"])
                    waits = [event for event in events if event.startswith("WAIT:")]
                    self.assertEqual(len(waits), 1)
                    self.assertEqual(int(waits[0].split(":")[1]), observed["parent"])
                    self.assertFalse(result["marker_spilled"])
                    self.assertFalse(result["tail_executed"])
                    for pid in result["owned_pids"]:
                        self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_parent_wait_tstp_requires_a_real_stopped_state(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                self.assertIsNotNone(shell)
                observed: dict[str, object] = {"states": [], "requests": []}
                original_wait = probe.wait
                original_proc_fields = probe.proc_fields
                original_write = probe.boundary.prototype.ShellProcess._write_all
                original_os_write = os.write

                def retain_session(session: object, prefix: str, since: int) -> str:
                    observed["session"] = session
                    if prefix == "START":
                        observed["since"] = since
                    return original_wait(session, prefix, since)

                def retain_stop_boundary(session: object, data: bytes) -> None:
                    if data == b"\x1a":
                        observed["stop_index"] = len(session.events)
                    original_write(session, data)

                def observe_proc(pid: int) -> tuple[int, int, int, str]:
                    fields = original_proc_fields(pid)
                    session = observed.get("session")
                    if session is not None and pid == session.pid and "stop_index" in observed:
                        observed["states"].append(fields[3])
                        if fields[3] == "T":
                            observed["events_while_stopped"] = session.events[
                                observed["stop_index"]:
                            ]
                    return fields

                def observe_request(fd: int, data: bytes) -> int:
                    if data in {b"PING\n", b"TAKEOVER\n"}:
                        observed["requests"].append(data)
                    return original_os_write(fd, data)

                with mock.patch.object(probe, "wait", side_effect=retain_session), \
                     mock.patch.object(probe, "proc_fields", side_effect=observe_proc), \
                     mock.patch.object(probe.boundary.prototype.ShellProcess,
                                       "_write_all", retain_stop_boundary), \
                     mock.patch.object(probe.os, "write", side_effect=observe_request):
                    try:
                        result = probe.simple_case(shell, "parent_wait_suspend")
                    except probe.Inconclusive as exc:
                        self.assertIn("no observed stopped state", str(exc))
                        self.assertTrue(observed["states"])
                        self.assertNotIn("T", observed["states"])
                        self.assertNotIn(b"PING\n", observed["requests"])
                        self.assertNotIn(b"TAKEOVER\n", observed["requests"])
                        session = observed["session"]
                        events = session.events[observed["since"]:]
                        self.assertNotIn("READY", events)
                        self.assertNotIn("NEW_INPUT", events)
                        pids = [session.pid] + [
                            int(event.split(":")[1]) for event in session.events
                            if event.startswith(("SUPERVISOR:", "CHILD:"))
                        ]
                    else:
                        self.assertTrue(result["parent_stopped"])
                        self.assertIn("T", observed["states"])
                        self.assertFalse(any(
                            event in {"READY", "BAD_REQUEST", "INPUT_RELEASED"} or
                            event.startswith("TAKEOVER_ACK:")
                            for event in observed["events_while_stopped"]
                        ))
                        pids = result["owned_pids"]
                for pid in pids:
                    self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_start_and_return_signal_matrix_keeps_causal_barriers(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for stage in ("start", "return"):
                for signame in ("INT", "QUIT", "TSTP", "CONT"):
                    with self.subTest(shell=shell, stage=stage, signal=signame):
                        self.assertIsNotNone(shell)
                        if signame == "TSTP":
                            negative = probe.shared_pg_negative(shell, stage)
                            self.assertEqual(negative["classification"],
                                             "parent_escape_before_input_release")
                            candidate = probe.split_case(shell, "start")
                            self.assertEqual(candidate["main_return"], "MAIN_RETURN:17")
                            continue
                        observed: dict[str, object] = {}
                        original_wait = probe.wait
                        original_write = probe.boundary.prototype.ShellProcess._write_all
                        original_killpg = os.killpg
                        original_fields = probe.proc_fields

                        def capture_wait(session: object, prefix: str, since: int) -> str:
                            observed["session"] = session
                            if prefix == "START":
                                observed["since"] = since
                            event = original_wait(session, prefix, since)
                            if prefix == "NEW_INPUT":
                                observed["events"] = session.events[observed["since"]:]
                            return event

                        def before_signal(session: object) -> None:
                            session._drain(0.02)
                            session.display_bytes()
                            events = session.events[observed["since"]:]
                            observed["before_signal"] = events
                            observed["foreground"] = probe.boundary.foreground_group(session)
                            observed["parent_group"] = os.getpgid(session.pid)
                            observed["supervisor_pid"] = int(next(
                                e for e in events if e.startswith("SUPERVISOR:")
                            ).split(":")[1])
                            if stage == "return":
                                descendant = next(e for e in events
                                                  if e.startswith("DESCENDANT:"))
                                pid = int(descendant.split(":")[1])
                                observed["descendant_before_signal"] = (
                                    pid, probe.proc_fields(pid)
                                )

                        def capture_key(session: object, data: bytes) -> None:
                            if signame != "CONT" and data == {
                                "INT": b"\x03", "QUIT": b"\x1c", "TSTP": b"\x1a"
                            }[signame]:
                                before_signal(session)
                            original_write(session, data)

                        def capture_cont(group: int, signum: int) -> None:
                            if signame == "CONT" and signum == signal.SIGCONT \
                                    and "before_signal" not in observed:
                                before_signal(observed["session"])
                                observed["target_group"] = group
                            original_killpg(group, signum)

                        def capture_supervisor_state(pid: int) -> tuple[int, int, int, str]:
                            fields = original_fields(pid)
                            if pid == observed.get("supervisor_pid") and \
                                    "supervisor_state" not in observed:
                                observed["supervisor_state"] = fields[3]
                            return fields

                        with mock.patch.object(probe, "wait", side_effect=capture_wait), \
                             mock.patch.object(probe.boundary.prototype.ShellProcess,
                                               "_write_all", capture_key), \
                             mock.patch.object(probe.os, "killpg", side_effect=capture_cont), \
                             mock.patch.object(probe, "proc_fields",
                                               side_effect=capture_supervisor_state):
                            result = probe.signal_stage_case(shell, stage, signame)

                        before = observed["before_signal"]
                        events = observed["events"]
                        sup = next(e for e in before if e.startswith("SUPERVISOR:"))
                        sup_pid, sup_group = map(int, sup.split(":")[1:3])
                        self.assertEqual(observed["foreground"], sup_group)
                        self.assertNotEqual(sup_group, observed["parent_group"])
                        if signame == "CONT":
                            self.assertEqual(observed["target_group"], sup_group)
                        if stage == "start":
                            self.assertIn("START_BARRIER", before)
                            self.assertFalse(any(e.startswith(("CHILD:", "MAIN_RETURN:"))
                                                 for e in before))
                            self.assertLess(events.index(f"SIGNAL_{signame}"),
                                            next(i for i, e in enumerate(events)
                                                 if e.startswith("CHILD:")))
                            self.assertEqual(result["main_return"], "MAIN_RETURN:0")
                        else:
                            self.assertIn("MAIN_RETURN:17", before)
                            self.assertIn("LIFETIME_ACTIVE:WNOHANG=0", before)
                            descendant_pid, fields = observed["descendant_before_signal"]
                            self.assertEqual(fields[0], sup_pid)
                            self.assertNotIn(fields[3], {"Z", "X"})
                            self.assertLess(events.index("MAIN_RETURN:17"),
                                            events.index(f"SIGNAL_{signame}"))
                            self.assertLess(events.index(f"SIGNAL_{signame}"),
                                            events.index(f"DESCENDANT_REAPED:{descendant_pid}:23"))
                        self.assertEqual(events.count(f"SIGNAL_{signame}"), 1)
                        self.assertEqual(events.count("START"), 1)
                        self.assertLess(events.index("WAIT_EMPTY:ECHILD"),
                                        events.index("INPUT_BARRIER"))
                        self.assertLess(events.index("INPUT_BARRIER"),
                                        events.index("INPUT_RELEASED"))
                        ack = next(i for i, e in enumerate(events)
                                   if e.startswith("TAKEOVER_ACK:"))
                        self.assertNotIn("READY", events[:ack])
                        self.assertLess(ack, events.index("READY"))
                        self.assertLess(events.index("READY"), events.index("NEW_INPUT"))
                        expected = (("stopped_continued" if observed["supervisor_state"] == "T"
                                     else "unknown_no_stop") if signame == "TSTP"
                                    else "native_noop" if signame == "CONT" else "observed")
                        self.assertEqual(result["classification"], expected)
                        self.assertTrue(result["observer_alive"])
                        self.assertTrue(result["parent_control_returned"])
                        for pid in result["owned_pids"]:
                            if pid is not None:
                                self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_cont_cell_rejects_missing_signal_event(self) -> None:
        shell = shutil.which("bash")
        self.assertIsNotNone(shell)
        original_wait = probe.wait
        original_killpg = os.killpg
        observed: dict[str, object] = {}

        def missing_event(session: object, prefix: str, since: int) -> str:
            if prefix == "SIGNAL_CONT":
                observed["session"] = session
                observed["since"] = since
                return "SIGNAL_CONT"
            return original_wait(session, prefix, since)

        def drop_cont(group: int, signum: int) -> None:
            if signum != signal.SIGCONT:
                original_killpg(group, signum)

        rejected = False
        with mock.patch.object(probe, "wait", side_effect=missing_event), \
             mock.patch.object(probe.os, "killpg", side_effect=drop_cont):
            try:
                probe.signal_stage_case(shell, "start", "CONT")
            except AssertionError:
                rejected = True
        session = observed["session"]
        self.assertNotIn("SIGNAL_CONT", session.events[observed["since"]:])
        owned = [session.pid] + [int(event.split(":")[1])
                                 for event in session.events
                                 if event.startswith(("SUPERVISOR:", "CHILD:"))]
        for pid in owned:
            self.assertFalse(Path(f"/proc/{pid}").exists(), pid)
        self.assertTrue(rejected, "CONT cell accepted a missing signal event")

    def test_missing_signal_delivery_times_out_for_each_native_signal(self) -> None:
        shell = shutil.which("bash")
        self.assertIsNotNone(shell)
        original_write = probe.boundary.prototype.ShellProcess._write_all
        original_killpg = os.killpg
        keys = {"INT": b"\x03", "QUIT": b"\x1c", "TSTP": b"\x1a"}

        for signame in ("INT", "QUIT", "TSTP", "CONT"):
            with self.subTest(signal=signame):
                observed: dict[str, object] = {}
                original_wait = probe.wait

                def capture_wait(session: object, prefix: str, since: int) -> str:
                    observed["session"] = session
                    return original_wait(session, prefix, since)

                def drop_key(session: object, data: bytes) -> None:
                    if data != keys.get(signame):
                        original_write(session, data)

                def drop_cont(group: int, signum: int) -> None:
                    if not (signame == "CONT" and signum == signal.SIGCONT):
                        original_killpg(group, signum)

                with mock.patch.object(probe, "TIMEOUT", 0.2), \
                     mock.patch.object(probe, "wait", side_effect=capture_wait), \
                     mock.patch.object(probe.boundary.prototype.ShellProcess,
                                       "_write_all", drop_key), \
                     mock.patch.object(probe.os, "killpg", side_effect=drop_cont):
                    with self.assertRaisesRegex(TimeoutError,
                                                f"missing event SIGNAL_{signame}"):
                        probe.signal_stage_case(shell, "start", signame)
                session = observed["session"]
                owned = [session.pid] + [int(event.split(":")[1])
                                         for event in session.events
                                         if event.startswith(("SUPERVISOR:", "CHILD:"))]
                for pid in owned:
                    self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_aggregate_treats_missing_cont_event_as_failure(self) -> None:
        shell = shutil.which("bash")
        self.assertIsNotNone(shell)
        original_case = probe.split_case
        original_kill = os.kill

        def missing_cont(target_shell: str, name: str) -> dict:
            if (target_shell, name) != (shell, "suspend"):
                return original_case(target_shell, name)
            observed = {}
            original_wait = probe.wait

            def capture_wait(session: object, prefix: str, since: int) -> str:
                result = original_wait(session, prefix, since)
                if prefix.startswith("MAIN_STOPPED:"):
                    observed["stopped"] = True
                return result

            def drop_resume(pid: int, signum: int) -> None:
                if signum == signal.SIGUSR2 and observed.get("stopped"):
                    return
                original_kill(pid, signum)

            with mock.patch.object(probe, "TIMEOUT", 0.2), \
                 mock.patch.object(probe, "wait", side_effect=capture_wait), \
                 mock.patch.object(probe.os, "kill", side_effect=drop_resume):
                return original_case(target_shell, name)

        output = io.StringIO()
        with mock.patch.object(probe, "split_case", side_effect=missing_cont), \
             redirect_stdout(output):
            status = probe.main()
        report = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(status, 1)
        self.assertEqual(report["result"], "failed")
        self.assertEqual((report["shell"], report["case"]), (shell, "suspend"))
        self.assertIn("TimeoutError: missing event FOREGROUND_VERIFIED", report["error"])
        self.assertNotIn("signal_unknowns", report)
        self.assertFalse(any(item.get("mode") == "split_pg" for item in report["cases"]))

    def test_wrong_signal_and_delivery_error_do_not_pass_cell(self) -> None:
        shell = shutil.which("bash")
        self.assertIsNotNone(shell)
        original_write = probe.boundary.prototype.ShellProcess._write_all

        def send_quit_instead(session: object, data: bytes) -> None:
            original_write(session, b"\x1c" if data == b"\x03" else data)

        observed: dict[str, object] = {}
        original_wait = probe.wait

        def capture_wait(session: object, prefix: str, since: int) -> str:
            observed["session"] = session
            return original_wait(session, prefix, since)

        with mock.patch.object(probe, "TIMEOUT", 0.2), \
             mock.patch.object(probe, "wait", side_effect=capture_wait), \
             mock.patch.object(probe.boundary.prototype.ShellProcess,
                               "_write_all", send_quit_instead):
            with self.assertRaisesRegex(TimeoutError, "missing event SIGNAL_INT"):
                probe.signal_stage_case(shell, "start", "INT")
        events = observed["session"].events
        self.assertIn("SIGNAL_QUIT", events)
        self.assertNotIn("SIGNAL_INT", events)

        original_killpg = os.killpg

        def delivery_error(group: int, signum: int) -> None:
            if signum == signal.SIGCONT:
                raise OSError("injected SIGCONT delivery error")
            original_killpg(group, signum)

        with mock.patch.object(probe.os, "killpg", side_effect=delivery_error):
            with self.assertRaisesRegex(OSError, "injected SIGCONT delivery error"):
                probe.signal_stage_case(shell, "start", "CONT")

    def test_aggregate_reports_no_t_parent_wait_as_inconclusive(self) -> None:
        original_snapshot = probe.signal_snapshot

        def unknown_disposition(pid: int) -> dict:
            result = original_snapshot(pid)
            result["tstp_ignored"] = False
            return result

        output = io.StringIO()
        with mock.patch.object(probe, "signal_snapshot", side_effect=unknown_disposition), \
             redirect_stdout(output):
            status = probe.main()
        report = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(status, 2)
        self.assertEqual(report["result"], "inconclusive")
        self.assertEqual(report["case"], "parent_wait")
        self.assertIn("native TSTP disposition is not proven ignored", report["error"])
        self.assertFalse(any(item.get("case") == "parent_wait"
                             for item in report["cases"]))

    def test_split_suspend_exit148_and_parent_native_noop(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for name in ("suspend", "exit148", "parent_wait"):
                with self.subTest(shell=shell, case=name):
                    result = probe.split_case(shell, name)
                    if name == "suspend":
                        self.assertEqual(len(result["stop_continue_cycles"]), 2)
                        self.assertEqual(result["main_return"], "MAIN_RETURN:17")
                    elif name == "exit148":
                        self.assertEqual(result["main_return"], "MAIN_RETURN:148")
                        self.assertEqual(result["stop_continue_cycles"], [])
                    else:
                        self.assertEqual(result["classification"], "native_ignore_noop")
                        self.assertTrue(result["parent_disposition"]["tstp_ignored"])
                    for pid in result["owned_pids"]:
                        self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_split_missing_prepared_barrier_never_injects_tstp(self) -> None:
        original_wait = probe.wait
        original_write = probe.boundary.prototype.ShellProcess._write_all
        observed = {"writes": []}

        def hide_prepared(session: object, prefix: str, since: int) -> str:
            observed["session"] = session
            if prefix == "PREPARED:":
                raise TimeoutError("prepared child unavailable")
            return original_wait(session, prefix, since)

        def capture_write(session: object, data: bytes) -> None:
            observed["writes"].append(data)
            original_write(session, data)

        with mock.patch.object(probe, "wait", side_effect=hide_prepared), \
             mock.patch.object(probe.boundary.prototype.ShellProcess,
                               "_write_all", capture_write):
            with self.assertRaisesRegex(TimeoutError, "prepared child unavailable"):
                probe.split_case(self.shell, "suspend")
        self.assertNotIn(b"\x1a", observed["writes"])
        session = observed["session"]
        for event in session.events:
            if event.startswith(("SUPERVISOR:", "CHILD:")):
                self.assertFalse(Path(f"/proc/{event.split(':')[1]}").exists())

    def test_split_failed_foreground_restore_never_continues(self) -> None:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        for failure in ("set", "verify"):
            with self.subTest(failure=failure), \
                 mock.patch.object(probe.os, "tcsetpgrp") as setter, \
                 mock.patch.object(probe.os, "tcgetpgrp", return_value=42), \
                 mock.patch.object(probe.os, "killpg") as deliver, \
                 mock.patch.object(probe, "emit") as emit:
                if failure == "set":
                    setter.side_effect = OSError("foreground restoration unavailable")
                with self.assertRaises((OSError, RuntimeError)):
                    probe.continue_experiment(43)
                deliver.assert_not_called()
                emit.assert_not_called()
                self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), previous)

    def test_split_foreground_int_quit_preserve_observer_and_live_sets_id_descendant(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for signame in ("int", "quit"):
                for stage in ("start", "return", "running"):
                    with self.subTest(shell=shell, signal=signame, stage=stage):
                        result = probe.split_case(shell, f"{signame}_{stage}")
                        if stage == "running":
                            expected = -2 if signame == "int" else -3
                            self.assertEqual(result["main_return"], f"MAIN_RETURN:{expected}")
                            self.assertEqual(result["parent_return"], f"RETURN:{128 - expected}")
                        else:
                            self.assertEqual(result["main_return"], "MAIN_RETURN:17")
                            self.assertEqual(result["parent_return"], "RETURN:17")
                            self.assertEqual(result["signal_events"], [
                                f"SIGNAL_{signame.upper()}", f"NO_TARGET:{stage}:SIG{signame.upper()}"
                            ])
            for code in (130, 131):
                with self.subTest(shell=shell, numeric_exit=code):
                    result = probe.split_case(shell, f"exit{code}")
                    self.assertEqual(result["main_return"], f"MAIN_RETURN:{code}")
                    self.assertEqual(result["signal_events"], [])

    def test_split_cleanup_rediscovers_descendant_after_unregistered_sets_id_failure(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                result = probe.split_failure_case(shell)
                self.assertEqual(result["classification"], "explicit_unknown_cleanup_empty")
                self.assertIn("UNKNOWN:INJECTED_FAILURE", result["events"])
                self.assertGreaterEqual(sum(event.startswith("CLEANUP_SCAN:")
                                            for event in result["events"]), 3)
                for pid in result["owned_pids"]:
                    self.assertFalse(Path(f"/proc/{pid}").exists(), pid)


if __name__ == "__main__":
    unittest.main()
