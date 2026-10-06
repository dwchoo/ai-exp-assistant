"""C-D69 (6) independent (p27-cd69-cmds-test-01): the worker harness on a real host shell (bash and dash).

Derived from DECISIONS.md C-D69 (6), not from the implementation:
(b) while the worker's Task lists ``commands``, ``terminal`` runs a listed command only as given; any other
    command (prefix/suffix, ``; cmd``, ``&&``, a newline, look-alike characters, case, other arguments) is refused
    and nothing is typed into the host shell; without a Task or without commands nothing is restricted.
(c) a command longer than one host shell request is written by the harness to a script file and run; the pane
    shows the command's first line and the script path; exit code, signal and output are those of the command.
Real backend ShellPane through HostShellPort; no OMP, no provider; temp files under /tmp only.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from workbench.backend import flow_terminal
from workbench.backend.flow_terminal import TerminalService
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.terminal.shell_g2.lifecycle import RUN_REQUEST_MAX, encode_run, normalize_run
from workbench.terminal.shell_g2.prototype import ShellChoice

import test_flow_terminal as tf

ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "WB_P27D_NAME": "fixture-value"}


def long_script(tag: str, *, exit_code: int, rows: int = 90) -> str:
    """A multi-line script with quotes, a function, both heredoc kinds, unicode and stderr (well over 4096 B)."""
    body = "\n".join(f"pad_{index:03d}=" + '"한글 \\"q\\" \'s\' $((' + str(index) + '+1))"' for index in range(rows))
    return (f"printf '{tag}-start %s\\n' \"$WB_P27D_NAME\"\n"
            f"{body}\n"
            "greet() { printf 'greet:%s|%s\\n' \"$1\" \"$2\"; }\n"
            "greet 'a b' \"c'd\"\n"
            "x=41\n"
            "cat <<EOF\nexpanded=$((x+1)) home-set=${HOME:+yes}\nEOF\n"
            "cat <<'EOF'\nliteral=$((x+1)) $HOME `date`\nEOF\n"
            "printf '유니코드 ✓ 😀 %s\\n' \"$pad_007\"\n"
            "printf 'to-stderr\\n' >&2\n"
            f"for i in 1 2 3; do printf '{tag}-loop-%s\\n' \"$i\"; done\n"
            f"exit {exit_code}\n")


def visible(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", text)


class _Harness(tf.ServiceFixture):
    CHOICE: ShellChoice

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.canary = self.root / "canary"
        self.canary.mkdir()
        self.pane = ShellPane(self.CHOICE, {**ENV, "HOME": str(self.home)})
        self.ui = bytearray()
        self.stop = threading.Event()
        self.loop = threading.Thread(target=self._loop, daemon=True)
        self.loop.start()
        self.terminal.close()
        self.terminal = self.make_terminal(lambda: HostShellPort(self.pane, lambda: self.pane))
        self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        self.type(f"cd {self.home}\r".encode())
        self.assertTrue(tf.wait_until(lambda: self.pane.cwd() == str(self.home), 5))
        self.assertTrue(tf.wait_until(self.idle, 5))

    def _loop(self):
        while not self.stop.is_set():
            for chunk in self.pane.pump():
                self.ui.extend(chunk.data)
            time.sleep(0.01)

    def tearDown(self):
        self.terminal.close()
        self.stop.set()
        self.loop.join(5)
        self.pane.close()
        super().tearDown()

    @property
    def logs(self) -> Path:
        return self.root / "workflow" / "terminal"

    def type(self, data):
        self.assertIsNone(self.pane.admit(data))

    def idle(self):
        port = HostShellPort(self.pane, lambda: self.pane)
        try:
            return port.busy() is None
        finally:
            port.detach()

    def records(self, kind):
        return [r for r in tf.journal(self.root) if r["type"] == kind]

    def settle(self):
        self.assertTrue(tf.wait_until(lambda: self.pane.state["input_owner"] == "user"
                                      and self.pane.state["parent_mode"] == "manual_prompt"
                                      and self.gate.owner is None and self.pane.automation_hold is None, 10),
                        self.pane.state)

    def restrict(self, commands, task_id="task-p27d"):
        self.terminal._task_commands = lambda: list(commands) if commands is not None else None
        self.terminal._active_task = lambda: SimpleNamespace(task_id=task_id)

    def oracle(self, script: str) -> tuple[int, str]:
        """The same text run by the same shell directly (``-c``): the expected exit code and output."""
        done = subprocess.run([self.CHOICE.executable, "-c", script], cwd=self.home, capture_output=True,
                              env={**ENV, "HOME": str(self.home)}, timeout=30)
        return done.returncode, (done.stdout + done.stderr).decode("utf-8", "replace")

    # -- (b) enforcement -------------------------------------------------------------------------------
    def test_bypass_attempts_are_refused_and_type_nothing(self):
        out = self.canary / "listed.txt"
        listed = ["echo listed-ok", f"printf '%s\\n' two > {out}"]
        self.restrict(listed)
        c = self.canary
        attempts = [
            f"echo listed-ok; touch {c}/semi",
            f"echo listed-ok && touch {c}/and",
            f"echo listed-ok || touch {c}/or",
            f"echo listed-ok\ntouch {c}/newline",
            f"echo listed-ok\r\ntouch {c}/crlf",
            f"echo listed-ok | tee {c}/pipe",
            f"echo listed-ok $(touch {c}/subst)",
            f"echo listed-ok `touch {c}/tick`",
            f"echo listed-ok & touch {c}/bg",
            f"touch {c}/prefix; echo listed-ok",
            " echo listed-ok",  # leading space
            "\techo listed-ok",
            "ECHO listed-ok",
            "Echo listed-ok",
            "echo LISTED-OK",
            "echo listed-ok extra-argument",
            "echo  listed-ok",  # inner spacing differs
            "echo listed-o",  # a prefix of a listed command
            "echo listed-okk",
            "echo listed‐ok",  # unicode hyphen
            "еcho listed-ok",  # Cyrillic e
            "ｅcho listed-ok",  # fullwidth e
            "echo listed-ok​",  # zero-width space (not whitespace for str.rstrip)
            f"printf '%s\\n' two > {c}/other.txt",  # a listed command with a different argument
            f"printf '%s\\n' two >> {out}",
            "echo listed-ok #",
        ]
        time.sleep(0.3)
        before, state = len(self.ui), dict(self.pane.state)
        for command in attempts:
            with self.subTest(command=command):
                result = self.run_tool({"command": command, "wait": 10})
                self.assertEqual((result.get("status"), result.get("reason")),
                                 ("not_in_task_commands", "not_in_task_commands"), result)
                self.assertEqual(result["allowed_commands"], listed)
                self.assertNotIn("command_id", result, "no command was created")
                self.assertIn("1. echo listed-ok", result["detail"])
        time.sleep(0.5)
        self.assertEqual(bytes(self.ui[before:]), b"", "a refusal types nothing, not even wb-handoff")
        self.assertEqual(sorted(p.name for p in self.canary.iterdir()), [], "nothing ran")
        self.assertEqual(self.records("terminal_started"), [])
        self.assertEqual(sorted(self.logs.iterdir()) if self.logs.exists() else [], [], "no log, no script")
        for key in ("input_owner", "parent_mode", "generation", "owner_epoch"):
            self.assertEqual(self.pane.state[key], state[key], key)
        self.assertIsNone(self.gate.owner)
        self.assertIsNone(self.pane.automation_hold)
        self.assertEqual(self.terminal.runs_for_task("task-p27d"), [], "refused attempts are not runs")
        # the listed commands themselves run, exactly as given (trailing ASCII whitespace only is ignored)
        first = self.run_tool({"command": "echo listed-ok  \n", "wait": 30})
        self.assertEqual((first.get("status"), first.get("exit_code")), ("exited", 0), first)
        self.assertIn("listed-ok", first["output_tail"])
        self.settle()
        second = self.run_tool({"command": listed[1], "wait": 30})
        self.assertEqual((second.get("status"), second.get("exit_code")), ("exited", 0), second)
        self.assertEqual(out.read_text(), "two\n")
        self.assertEqual(sorted(p.name for p in self.canary.iterdir()), ["listed.txt"])
        self.settle()

    def test_trailing_characters_the_shell_keeps_are_not_ignored(self):
        """C-D69 (6)(b) / test-01 P3-1 (fixed in fix-01): only trailing space, tab and newline are ignored; the
        shell keeps U+001F (and \\r, \\x0b, \\xa0, ...) in the last word, so ``echo listed-ok<US>`` is not
        the listed command and must be refused."""
        self.restrict(["echo listed-ok"])
        result = self.run_tool({"command": "echo listed-ok\x1f", "wait": 30})
        if result.get("status") == "exited":
            self.assertIn(b"listed-ok\x1f", Path(result["log_path"]).read_bytes(), "a different command ran")
            self.settle()
        self.assertEqual(result.get("status"), "not_in_task_commands", result)
        for char in ("\r", "\x0b", "\x0c", "\xa0", "\u3000"):
            with self.subTest(char=repr(char)):
                again = self.run_tool({"command": "echo listed-ok" + char, "wait": 10})
                self.assertEqual(again.get("status"), "not_in_task_commands", again)
        self.assertEqual(self.records("terminal_started"), [], "nothing was started")

    def test_no_task_no_commands_and_the_state_fetch_are_not_restricted(self):
        for commands in (None, []):
            with self.subTest(commands=commands):
                self.restrict(commands)
                result = self.run_tool({"command": "echo free-$((2+3))", "wait": 30})
                self.assertEqual((result.get("status"), result.get("exit_code")), ("exited", 0), result)
                self.assertIn("free-5", result["output_tail"])
                self.settle()
        self.restrict(["echo only-this"])
        for args in ({"command": None}, {"command": "   "}, {}):
            with self.subTest(args=args):
                fetched = self.run_tool(args)
                self.assertNotEqual(fetched.get("status"), "not_in_task_commands", fetched)

    # -- (c) script file --------------------------------------------------------------------------------
    def test_a_long_script_runs_from_a_private_script_file_like_the_inline_command(self):
        script = long_script("spill", exit_code=7)
        self.assertGreater(flow_terminal.command_request_bytes(self.CHOICE.executable, script), RUN_REQUEST_MAX)
        self.assertFalse(self.logs.exists() and any(self.logs.iterdir()))
        self.restrict([script])  # a listed long command is accepted as given and spilled
        before = len(self.ui)
        result = self.run_tool({"command": script, "wait": 30})
        self.assertEqual((result.get("status"), result.get("exit_code")), ("exited", 7), result)
        path = Path(result["script_path"])
        self.assertEqual(path.parent, self.logs)
        self.assertEqual(path.read_text(encoding="utf-8"), script)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.logs.stat().st_mode), 0o700)
        self.assertEqual(path.stat().st_uid, os.getuid())
        expected_code, expected = self.oracle(script)
        self.assertEqual(expected_code, 7)
        log = visible(Path(result["log_path"]).read_bytes())
        for line in expected.splitlines():
            self.assertIn(line, log, "the script-file run prints what the command prints")
        for needle in ("spill-start fixture-value", "greet:a b|c'd", "expanded=42 home-set=yes",
                       "literal=$((x+1)) $HOME `date`", "유니코드 ✓ 😀", "to-stderr", "spill-loop-3"):
            self.assertIn(needle, log)
        self.assertIn("spill-loop-3", result["output_tail"])
        shown = visible(bytes(self.ui[before:]))
        first = script.split("\n", 1)[0]
        self.assertIn(f"[worker] $ {first} … (script {path})", shown)
        self.assertNotIn("pad_050", shown, "the script body is neither typed nor echoed")
        self.assertNotIn("[worker] $", log, "the shown line is not command output")
        started = self.records("terminal_started")[-1]
        self.assertEqual((started["command"], started["script_path"]), (script, str(path)))
        ended = self.records("terminal_ended")[-1]
        self.assertEqual(ended["exit_code"], 7)
        runs = self.terminal.runs_for_task("task-p27d")
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["status"], runs[0]["exit_code"], runs[0]["script_path"]),
                         ("exited", 7, str(path)))
        self.assertEqual(runs[0]["command"], first + " …")
        self.settle()
        self.assertEqual(self.pane.cwd(), str(self.home), "the user's shell never moved")

    def test_exit_codes_and_signals_match_the_inline_run(self):
        pad = "\n: " + "p" * 4500
        cases = (("exit 0", 0, None), ("exit 255", 255, None), ("false", 1, None),
                 ("kill -TERM $$", None, "TERM"), ("kill -KILL $$", None, "KILL"))
        for tail, code, signal in cases:
            with self.subTest(tail=tail):
                inline = self.run_tool({"command": f"printf 'in\\n'; {tail}", "wait": 30})
                self.settle()
                spilled = self.run_tool({"command": f"printf 'sp\\n'{pad}\n{tail}", "wait": 30})
                self.settle()
                self.assertNotIn("script_path", inline)
                self.assertIn("script_path", spilled)
                for name in ("status", "exit_code", "signal"):
                    self.assertEqual(spilled.get(name), inline.get(name), (name, inline, spilled))
                if signal is None:
                    self.assertEqual((spilled["status"], spilled["exit_code"]), ("exited", code), spilled)
                else:
                    number = {"TERM": 15, "KILL": 9}[signal]
                    self.assertEqual((spilled.get("signal"), spilled.get("exit_code")), (number, 128 + number), spilled)

    def test_the_shown_line_is_the_first_line_cut_at_200_characters(self):
        first = "printf 'first-ok\\n'; : " + "w" * 300
        command = "\n\n" + first + "\n: " + "z" * 4500 + "\nprintf 'end-ok\\n'"
        before = len(self.ui)
        result = self.run_tool({"command": command, "wait": 30})
        self.assertEqual((result.get("status"), result.get("exit_code")), ("exited", 0), result)
        self.assertIn("end-ok", result["output_tail"])
        shown = visible(bytes(self.ui[before:]))
        self.assertIn(f"[worker] $ {first[:200]} … (script {result['script_path']})", shown)
        self.assertIn("first-ok", result["output_tail"])
        self.assertNotIn("w" * (201 - len("printf 'first-ok\\n'; : ")), shown, "only 200 characters are shown")
        self.settle()

    def test_a_first_line_of_wide_characters_still_fits_the_shown_request(self):
        # the shown line itself goes into the request: 200 non-BMP characters (12 JSON bytes each) must fit
        first = "printf '%s\\n' " + "😀" * 400
        command = first + "\nprintf 'wide-ok\\n'"
        self.assertGreater(flow_terminal.command_request_bytes(self.CHOICE.executable, command), RUN_REQUEST_MAX)
        result = self.run_tool({"command": command, "wait": 30})
        self.assertEqual((result.get("status"), result.get("exit_code")), ("exited", 0), result)
        self.assertIn("wide-ok", result["output_tail"])
        shown = flow_terminal.shown_command(command) + f" (script {result['script_path']})"
        argv = [self.CHOICE.executable, "-c", flow_terminal.SPILL_SCRIPT, self.CHOICE.executable, shown,
                result["script_path"]]
        need = len(f"RUN:{encode_run(normalize_run(self.CHOICE.executable, argv, str(uuid4())))}\n".encode())
        self.assertLessEqual(need, RUN_REQUEST_MAX)
        self.settle()

    def test_the_spill_request_never_exceeds_the_limit_for_wide_first_lines_and_deep_paths(self):
        """review-01 P3-3 (fixed in fix-01): ``shown`` (first line, <= 200 characters) plus the script path (sent
        twice) is cut by bytes, so the request stays within RUN_REQUEST_MAX instead of failing the start."""
        executable = self.CHOICE.executable
        for first in ("😀" * 400, "한" * 400, "\\" * 400, '"' * 400, "printf '%s\\n' " + "😀" * 300):
            for depth in (1, 5, 8, 10, 14):
                path = "/tmp/" + "/".join("d" * 90 for _ in range(depth)) + "/script-" + "0" * 32 + ".sh"
                def need(text):
                    argv = [executable, "-c", flow_terminal.SPILL_SCRIPT, executable, text, path]
                    return len(f"RUN:{encode_run(normalize_run(executable, argv, str(uuid4())))}\n".encode())

                if need(f"(script {path})") > RUN_REQUEST_MAX:
                    continue  # the path alone (sent twice) fills the request: nothing to cut from the first line
                with self.subTest(first=first[:6], path_length=len(path)):
                    shown = flow_terminal.spill_shown(executable, first + "\nmore", path)
                    self.assertLessEqual(need(shown), RUN_REQUEST_MAX, (len(shown), len(path)))
                    self.assertIn(f"(script {path})", shown, "the script path is always named")

    def test_a_200_emoji_first_line_runs_from_a_deeply_nested_log_directory(self):
        deep = self.root / "deep"
        for index in range(9):
            deep = deep / (f"level{index}-" + "n" * 90)
        deep.mkdir(parents=True, mode=0o700)
        self.terminal.close()
        self.terminal = TerminalService(handoffs=self.handoffs, host_shell=lambda: HostShellPort(self.pane,
                                                                                               lambda: self.pane),
                                        gate=self.gate, log_root=deep, automation=lambda: tf.AUTOMATION,
                                        sensitive_values=lambda: (tf.SECRET,), poll_interval=0.02)
        command = "printf '%s\\n' " + "😀" * 200 + "\n: " + "p" * 4300 + "\nprintf 'deep-ok\\n'"
        self.assertGreater(flow_terminal.command_request_bytes(self.CHOICE.executable, command), RUN_REQUEST_MAX)
        result = self.run_tool({"command": command, "wait": 30})
        self.assertEqual((result.get("status"), result.get("exit_code")), ("exited", 0), result)
        self.assertIn("deep-ok", result["output_tail"])
        script = Path(result["script_path"])
        self.assertEqual(script.parent, deep)
        self.assertGreater(len(str(script)), 800)
        self.settle()

    def test_an_abort_before_the_start_removes_the_script_and_the_log(self):
        original = TerminalService._abandoned_now
        seen = []

        def abandoned(service, key):
            seen.append(1)
            return len(seen) == 3 or original(service, key)  # after wb-handoff and the claim

        before = len(self.ui)
        with mock.patch.object(TerminalService, "_abandoned_now", abandoned):
            result = self.run_tool({"command": f"touch {self.canary}/never\n: " + "a" * 4500, "wait": 10})
        self.assertEqual(result.get("status"), "aborted", result)
        self.assertEqual(sorted(self.logs.iterdir()), [], "no script file and no log are left")
        self.assertEqual(list(self.canary.iterdir()), [])
        self.assertNotIn(b"(script ", bytes(self.ui[before:]))
        self.settle()
        self.assertEqual(self.pane.cwd(), str(self.home))

    def test_a_start_failure_of_a_long_command_returns_the_shell_and_runs_nothing(self):
        with mock.patch.object(HostShellPort, "claim_manager", side_effect=RuntimeError("forced claim failure")):
            result = self.run_tool({"command": f"touch {self.canary}/never\n: " + "b" * 4500, "wait": 10})
        self.assertEqual(result.get("status"), "start_failed", result)
        self.assertEqual(result["host_terminal"], {"input_owner": "user", "parent_mode": "manual_prompt"})
        self.assertNotIn("script_path", result)
        self.assertEqual(list(self.canary.iterdir()), [])
        self.assertEqual(self.terminal.runs_for_task("task-p27d"), [])
        self.settle()
        after = self.run_tool({"command": "echo after-failure-$((3*3))", "wait": 30})
        self.assertEqual((after.get("status"), after.get("exit_code")), ("exited", 0), after)
        self.assertIn("after-failure-9", after["output_tail"])
        self.settle()

    def test_a_listed_long_command_with_one_more_line_is_refused(self):
        script = long_script("listed", exit_code=0)
        self.restrict([script])
        before = len(self.ui)
        for variant in (script + "touch " + str(self.canary / "extra") + "\n", "\n" + script,
                        script.replace("exit 0", "exit 1")):
            with self.subTest(variant=variant[-30:]):
                result = self.run_tool({"command": variant, "wait": 10})
                self.assertEqual(result.get("status"), "not_in_task_commands", result)
                self.assertIn(" …", result["detail"], "the long listed command is shown by its first line")
                self.assertNotIn("pad_050", result["detail"])
        time.sleep(0.3)
        self.assertEqual(bytes(self.ui[before:]), b"")
        self.assertEqual(sorted(self.logs.iterdir()) if self.logs.exists() else [], [])


class BashHarnessTests(_Harness):
    CHOICE = ShellChoice("bash", "/usr/bin/bash")


class DashHarnessTests(_Harness):
    CHOICE = ShellChoice("sh", "/usr/bin/dash")


del _Harness

if __name__ == "__main__":
    unittest.main()
