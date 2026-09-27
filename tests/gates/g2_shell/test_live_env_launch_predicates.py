"""Independent live predicates for the bounded CW-03 environment/launch slice."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import unittest
from unittest import mock

from tests.gates.g2_shell import live_env_launch_probe as probe


class EnvLaunchPredicatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if sys.platform != "linux" or any(shutil.which(name) is None
                                          for name in ("bash", "dash", "sh")):
            raise unittest.SkipTest("Linux, Bash, dash, and sh required")

    def test_environment_and_venv_preserve_parent_after_takeover(self) -> None:
        original_controlled = probe.controlled
        captures: list[dict] = []

        @contextmanager
        def capture(shell: str, directory: str, preparation: str = ""):
            with original_controlled(shell, directory, preparation) as context:
                session, parent, _, before, after = context
                try:
                    yield context
                finally:
                    captures.append({"parent": parent, "directory": directory,
                                     "events": list(session.events),
                                     "traps_equal": before.read_bytes() == after.read_bytes()})

        with mock.patch.object(probe, "controlled", side_effect=capture):
            for shell in (shutil.which("bash"), shutil.which("dash")):
                for venv in (False, True):
                    with self.subTest(shell=shell, venv=venv):
                        result = probe.environment_case(shell, venv=venv)
                        record = captures[-1]
                        events = record["events"]
                        start = events.index("START")
                        later = events[start:]
                        self.assertEqual(result["parent_pid_preserved"], True)
                        self.assertEqual(result["child_changes_isolated"], True)
                        self.assertTrue(record["traps_equal"])
                        self.assertLess(later.index("WAIT_EMPTY:ECHILD"),
                                        later.index("INPUT_BARRIER"))
                        self.assertLess(later.index("INPUT_BARRIER"),
                                        later.index("INPUT_RELEASED"))
                        ack = later.index(f"TAKEOVER_ACK:{record['parent']}")
                        self.assertLess(ack, later.index("PRIOR_HOOK", ack))
                        self.assertLess(ack, later.index("PARENT_ENV:" +
                                                          record["directory"] +
                                                          ":kept:kept:unset:" +
                                                          (str(Path(record["directory"]) / "venv")
                                                           if venv else "unset")))
                        self.assertIn("CHILD_AFTER:/:changed:child_only", later)
                        self.assertEqual(later.count("START"), 1)
                        owned = [record["parent"]] + [int(event.split(":")[1])
                                                      for event in events
                                                      if event.startswith(("SUPERVISOR:",
                                                                           "CHILD:"))]
                        for pid in owned:
                            self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_incompatible_hooks_reject_before_experiment_start(self) -> None:
        original_controlled = probe.controlled
        captures: list[dict] = []

        @contextmanager
        def capture(shell: str, directory: str, preparation: str = ""):
            with original_controlled(shell, directory, preparation) as context:
                session, parent, _, before, after = context
                try:
                    yield context
                finally:
                    captures.append({"parent": parent, "events": list(session.events),
                                     "needs_review": session.boundary.needs_review,
                                     "traps_equal": before.read_bytes() == after.read_bytes()})

        with mock.patch.object(probe, "controlled", side_effect=capture):
            for shell in (shutil.which("bash"), shutil.which("dash")):
                for kind in ("prompt", "trap"):
                    with self.subTest(shell=shell, kind=kind):
                        result = probe.incompatible_case(shell, kind)
                        record = captures[-1]
                        events = record["events"]
                        self.assertEqual(result["failure_class"], "HOOK_LOST")
                        self.assertTrue(record["needs_review"])
                        self.assertTrue(record["traps_equal"])
                        self.assertLess(events.index("HOOK_LOST"),
                                        events.index("HOOK_REJECTED"))
                        self.assertLess(events.index("HOOK_REJECTED"),
                                        events.index(f"TAKEOVER_ACK:{record['parent']}"))
                        self.assertNotIn("START", events)
                        self.assertFalse(any(event.startswith(("SUPERVISOR:", "CHILD:"))
                                             for event in events))
                        self.assertFalse(Path(f"/proc/{record['parent']}").exists())

    def test_pinned_fallback_literal_argv_and_unavailable_conda(self) -> None:
        original_launch_case = probe.launch_case
        original_which = shutil.which
        selected: list[tuple[str, str, str | None]] = []

        def capture_launch(choice, *, mode: str, substitute: str | None = None) -> dict:
            selected.append((choice.executable, mode, os.environ.get("PATH")))
            return original_launch_case(choice, mode=mode, substitute=substitute)

        with mock.patch.object(probe, "launch_case", side_effect=capture_launch):
            selection = probe.launch_selection()
            chosen = probe.combined.boundary.prototype.select_shell()
            argv = probe.launch_case(chosen, mode="argv")
            script = probe.launch_case(chosen, mode="script")
        self.assertEqual(selection["bash_preferred"], os.path.realpath(original_which("bash")))
        self.assertEqual(selection["fallback"], os.path.realpath(original_which("sh")))
        self.assertEqual([(path, mode) for path, mode, _ in selected[:2]],
                         [(selection["fallback"], "argv"),
                          (selection["fallback"], "script")])
        self.assertTrue(all(original_which("bash", path=path) is None
                            for _, _, path in selected[:2]))
        self.assertTrue(all(item["literal_argv"] for item in
                            (selection["fallback_cases"][0], argv)))
        self.assertEqual(script["mode"], "script")
        self.assertTrue(selection["failed_exec_no_shell"])
        self.assertTrue(selection["failed_child_no_replacement"])
        self.assertIn("Bash or sh", selection["missing_guidance"])

        def without_conda(name: str, *args, **kwargs):
            return None if name == "conda" else original_which(name, *args, **kwargs)

        output = io.StringIO()
        with mock.patch.object(probe.shutil, "which", side_effect=without_conda), \
             redirect_stdout(output):
            status = probe.main()
        report = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(status, 0)
        self.assertEqual(report["result"], "observed")
        self.assertEqual(len(report["cases"]), 10)
        self.assertEqual([item["cell"] for item in report["unavailable"]],
                         ["conda activation/deactivation"])
        self.assertFalse(any("conda" in str(item).lower() for item in report["cases"]))

    def test_child_identity_is_live_after_exec_before_release(self) -> None:
        prototype = probe.combined.boundary.prototype
        original_wait = probe.combined.wait
        for shell in (shutil.which("bash"), shutil.which("dash")):
            choice = prototype.ShellChoice("bash" if Path(shell).name == "bash" else "sh",
                                           os.path.realpath(shell))
            for mode in ("argv", "script"):
                with self.subTest(shell=shell, mode=mode):
                    observed: dict[str, object] = {}

                    def at_ready(session: object, prefix: str, since: int) -> str:
                        event = original_wait(session, prefix, since)
                        if prefix == "CHILD_READY:":
                            events = session.events[since:]
                            child = next(item for item in events if item.startswith("CHILD:"))
                            supervisor = next(item for item in events
                                              if item.startswith("SUPERVISOR:"))
                            pid, group = map(int, child.split(":")[1:3])
                            sup_pid = int(supervisor.split(":")[1])
                            observed.update({"event": event, "pid": pid,
                                             "group": group, "supervisor": sup_pid,
                                             "parent": session.pid,
                                             "fields": probe.combined.proc_fields(pid),
                                             "actual_group": os.getpgid(pid),
                                             "actual": os.readlink(f"/proc/{pid}/exe"),
                                             "events": list(events)})
                        return event

                    with mock.patch.object(probe.combined, "wait", side_effect=at_ready):
                        result = probe.launch_case(choice, mode=mode)
                    self.assertEqual(observed["event"],
                                     f"CHILD_READY:{observed['pid']}")
                    self.assertEqual(observed["fields"][0], observed["supervisor"])
                    self.assertEqual(observed["fields"][1], observed["parent"])
                    self.assertEqual(observed["actual_group"], observed["group"])
                    self.assertGreater(observed["fields"][2], 0)
                    self.assertNotIn(observed["fields"][3], {"Z", "X"})
                    self.assertNotIn("MAIN_RETURN:0", observed["events"])
                    self.assertNotIn("INPUT_BARRIER", observed["events"])
                    expected = os.path.realpath(sys.executable if mode == "argv" else shell)
                    self.assertEqual(os.path.realpath(observed["actual"]), expected)
                    self.assertEqual(result["actual_child_executable"], expected)
                    for pid in (observed["parent"], observed["supervisor"], observed["pid"]):
                        self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_wrong_actual_shell_rejected_with_unchanged_launch_event(self) -> None:
        bash = os.path.realpath(shutil.which("bash"))
        dash = os.path.realpath(shutil.which("dash"))
        choice = probe.combined.boundary.prototype.ShellChoice("bash", bash)
        original_wait = probe.combined.wait
        observed: dict[str, object] = {}

        def capture(session: object, prefix: str, since: int) -> str:
            event = original_wait(session, prefix, since)
            observed["session"] = session
            if prefix == "CHILD_READY:":
                observed["events"] = list(session.events[since:])
            return event

        with mock.patch.object(probe.combined, "wait", side_effect=capture):
            with self.assertRaisesRegex(AssertionError, "child executable mismatch"):
                probe.launch_case(choice, mode="script", substitute=dash)
        self.assertIn(f"LAUNCH:script:{bash}", observed["events"])
        self.assertTrue(any(event.startswith("CHILD_READY:")
                            for event in observed["events"]))
        self.assertFalse(any(event.startswith("MAIN_RETURN:")
                             for event in observed["events"]))
        session = observed["session"]
        owned = [session.pid] + [int(event.split(":")[1]) for event in session.events
                                 if event.startswith(("SUPERVISOR:", "CHILD:"))]
        for pid in owned:
            self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_invalid_child_identity_or_exe_is_rejected(self) -> None:
        bash = os.path.realpath(shutil.which("bash"))
        choice = probe.combined.boundary.prototype.ShellChoice("bash", bash)
        original_wait = probe.combined.wait
        original_fields = probe.combined.proc_fields
        original_getpgid = os.getpgid
        original_readlink = os.readlink

        for fault in ("wrong_parent", "wrong_session", "wrong_group", "zero_start",
                      "dead_child", "unreadable_exe", "missing_exe", "wrong_ready_pid"):
            with self.subTest(fault=fault):
                observed: dict[str, object] = {}

                def capture(session: object, prefix: str, since: int) -> str:
                    event = original_wait(session, prefix, since)
                    observed["session"] = session
                    if prefix == "CHILD_READY:":
                        observed["child"] = int(event.split(":")[1])
                        if fault == "dead_child":
                            os.kill(observed["child"], signal.SIGKILL)
                            time.sleep(0.02)
                        if fault == "wrong_ready_pid":
                            return f"CHILD_READY:{observed['child'] + 1}"
                    return event

                def fields(pid: int):
                    value = original_fields(pid)
                    if pid != observed.get("child"):
                        return value
                    if fault == "wrong_parent":
                        return value[0] + 1, value[1], value[2], value[3]
                    if fault == "wrong_session":
                        return value[0], value[1] + 1, value[2], value[3]
                    if fault == "zero_start":
                        return value[0], value[1], 0, value[3]
                    return value

                def group(pid: int) -> int:
                    value = original_getpgid(pid)
                    return value + 1 if fault == "wrong_group" and pid == observed.get("child") else value

                def exe(path: str) -> str:
                    if path == f"/proc/{observed.get('child')}/exe":
                        if fault == "unreadable_exe":
                            raise PermissionError("injected unreadable child exe")
                        if fault == "missing_exe":
                            raise FileNotFoundError("injected missing child exe")
                    return original_readlink(path)

                with mock.patch.object(probe.combined, "wait", side_effect=capture), \
                     mock.patch.object(probe.combined, "proc_fields", side_effect=fields), \
                     mock.patch.object(probe.os, "getpgid", side_effect=group), \
                     mock.patch.object(probe.os, "readlink", side_effect=exe):
                    with self.assertRaises((AssertionError, FileNotFoundError,
                                            PermissionError, ProcessLookupError)):
                        probe.launch_case(choice, mode="script")
                session = observed["session"]
                owned = [session.pid] + [int(event.split(":")[1])
                                         for event in session.events
                                         if event.startswith(("SUPERVISOR:", "CHILD:"))]
                for pid in owned:
                    self.assertFalse(Path(f"/proc/{pid}").exists(), pid)

    def test_changed_positive_start_time_cannot_pass(self) -> None:
        bash = os.path.realpath(shutil.which("bash"))
        choice = probe.combined.boundary.prototype.ShellChoice("bash", bash)
        original_wait = probe.combined.wait
        original_fields = probe.combined.proc_fields
        observed: dict[str, object] = {}

        def capture(session: object, prefix: str, since: int) -> str:
            event = original_wait(session, prefix, since)
            if prefix == "CHILD_READY:":
                observed["child"] = int(event.split(":")[1])
            return event

        def changed_start(pid: int):
            fields = original_fields(pid)
            if pid == observed.get("child"):
                return fields[0], fields[1], fields[2] + 1_000_000, fields[3]
            return fields

        with mock.patch.object(probe.combined, "wait", side_effect=capture), \
             mock.patch.object(probe.combined, "proc_fields", side_effect=changed_start):
            with self.assertRaises(AssertionError):
                probe.launch_case(choice, mode="script")

    def test_duplicate_child_ready_cannot_pass(self) -> None:
        bash = os.path.realpath(shutil.which("bash"))
        choice = probe.combined.boundary.prototype.ShellChoice("bash", bash)
        original_wait = probe.combined.wait

        def duplicate(session: object, prefix: str, since: int) -> str:
            event = original_wait(session, prefix, since)
            if prefix == "CHILD_READY:":
                session._events.append(event)
            return event

        with mock.patch.object(probe.combined, "wait", side_effect=duplicate):
            with self.assertRaises(AssertionError):
                probe.launch_case(choice, mode="script")


if __name__ == "__main__":
    unittest.main()
