"""CW-16 O4 (C-AC-22): ``shutdown`` waits for the backend's result up to the backend's own bound, shows progress on
stderr (human form only), never ends in a traceback, and without a result says that termination could not be
confirmed (exit 1) after checking the backend's exact pid. The UI socket is replaced by a fake client."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ticks

from workbench.backend import cli
from workbench.backend.client import ClientError
from workbench.contracts.ui_v1 import ClientType

VERIFIED = {"verified": True, "problems": []}


class Layout:
    def __init__(self, root):
        self.root = root
        self.ui_socket = root / "ui.sock"


class Client:
    """``answers`` per request kind (a value, or an exception to raise); ``closing_after`` seconds of pumping
    before the CLOSING frame (None: never); ``eof``: the connection ends on the first pump."""

    def __init__(self, answers, *, closing_after=None, result=VERIFIED, eof=False):
        self.answers, self.closing_after, self.result, self.eof = answers, closing_after, result, eof
        self.sent, self.closing, self.started = [], None, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, kind, **fields):
        self.sent.append(kind)
        answer = self.answers[kind]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def pump(self, timeout):
        if self.eof:
            return False
        self.started = self.started or time.monotonic()
        if self.closing_after is not None and time.monotonic() - self.started >= self.closing_after:
            self.closing = {"type": "closing", "result": self.result}
            return True
        time.sleep(min(timeout, 0.05))
        return True


def own_ref():
    return {"role": "backend", "pid": os.getpid(), "start_ticks": ticks(os.getpid()), "owner_epoch": 1}


def ended_ref():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    ref = {"role": "backend", "pid": child.pid, "start_ticks": ticks(child.pid) or 1, "owner_epoch": 1}
    child.wait()
    return ref


class ShutdownWait(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw16-o4-cli-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def run_cli(self, client, *, json_form=False, yes=True, **patches):
        out, err = io.StringIO(), io.StringIO()
        args = argparse.Namespace(data_dir=str(self.root), yes=yes, json=json_form)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "_layout", lambda _a, **_k: Layout(self.root)))
            stack.enter_context(mock.patch.object(cli, "UiClient", lambda *a, **k: client))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(cli, name, value))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = cli.cmd_shutdown(args)  # an exception here is the traceback the user would see
        return code, out.getvalue(), err.getvalue()

    def answers(self, confirm=None, ref=None):
        return {ClientType.SHUTDOWN_REQUEST: {"ok": True, "token": "t", "active": [{"kind": "task_run"}],
                                              "processes": {"backend": ref or own_ref()}},
                ClientType.SHUTDOWN_CONFIRM: confirm if confirm is not None
                else {"ok": True, "shutting_down": True, "result_deadline": 5}}

    def test_a_confirm_answer_that_times_out_still_waits_for_the_result(self):
        client = Client(self.answers(confirm=TimeoutError("no frame from backend")), closing_after=0.3)
        code, out, err = self.run_cli(client)
        self.assertEqual(code, 0, err)
        self.assertIn('"verified": true', out)
        self.assertNotIn("종료 확인 실패", err)

    def test_the_wait_follows_the_backend_bound_and_shows_progress(self):
        client = Client(self.answers(), closing_after=1.0)
        code, _out, err = self.run_cli(client, SHUTDOWN_PROGRESS_EVERY=0.3)
        self.assertEqual(code, 0, err)
        self.assertIn("최대 5초", err)
        self.assertGreaterEqual(err.count("종료 중…"), 2, err)

    def test_json_form_has_no_progress_and_ends_with_the_result(self):
        client = Client(self.answers(), closing_after=0.6)
        code, out, err = self.run_cli(client, json_form=True, SHUTDOWN_PROGRESS_EVERY=0.2)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out.splitlines()[-1]), {"shutdown": VERIFIED})
        self.assertNotIn("종료 중", err)

    def test_connection_lost_before_the_result_checks_the_backend_pid(self):
        for json_form in (False, True):
            with self.subTest(json=json_form):
                client = Client(self.answers(ref=ended_ref()), eof=True)
                code, out, err = self.run_cli(client, json_form=json_form)
                self.assertEqual(code, 1)
                self.assertIn("종료 확인 실패", err)
                self.assertIn("backend process는 종료됨", err)
                if json_form:
                    payload = json.loads(out.splitlines()[-1])
                    self.assertIsNone(payload["shutdown"])
                    self.assertEqual(payload["unconfirmed"]["backend"], "ended")

    def test_no_result_within_the_bound_with_the_backend_alive(self):
        client = Client(self.answers(confirm={"ok": True, "result_deadline": 0.5}))
        started = time.monotonic()
        code, _out, err = self.run_cli(client, BACKEND_GONE_WAIT=0.2)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(code, 1)
        self.assertIn("종료 확인 실패", err)
        self.assertIn("아직 실행 중", err)

    def test_a_lost_connection_on_the_confirm_is_reported_not_raised(self):
        client = Client(self.answers(confirm=ClientError("backend closed: backend_shutdown")), eof=True)
        code, _out, err = self.run_cli(client, BACKEND_GONE_WAIT=0.2)
        self.assertEqual(code, 1)
        self.assertIn("종료 확인 실패", err)

    def test_a_failed_request_stops_nothing_and_is_reported(self):
        client = Client({ClientType.SHUTDOWN_REQUEST: TimeoutError("no frame from backend"),
                         ClientType.SHUTDOWN_CONFIRM: {"ok": True}})
        code, _out, err = self.run_cli(client)
        self.assertEqual(code, 1)
        self.assertEqual(client.sent, [ClientType.SHUTDOWN_REQUEST])
        self.assertIn("nothing was stopped", err)

    def test_ctrl_c_during_the_request_stops_nothing_without_a_traceback(self):
        # C18 review P3: Ctrl-C before the confirmation is a cancel, never a traceback or a confirm
        client = Client({ClientType.SHUTDOWN_REQUEST: KeyboardInterrupt(),
                         ClientType.SHUTDOWN_CONFIRM: {"ok": True}})
        code, _out, err = self.run_cli(client)
        self.assertEqual(code, 1)
        self.assertEqual(client.sent, [ClientType.SHUTDOWN_REQUEST])
        self.assertIn("nothing was stopped", err)

    # -- fix-05 (C17 P3-2): no traceback without --yes on a closed stdin, or on Ctrl-C ----------------------
    def test_no_yes_on_a_closed_or_non_tty_stdin_prints_the_work_and_asks_for_yes(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        class BrokenTty(Tty):
            def readline(self, *_a):
                return ""  # input() raises EOFError (Ctrl-D / a tty closed under us)

        for label, stdin in (("none", None), ("not a tty", io.StringIO("")), ("eof on a tty", BrokenTty())):
            with self.subTest(stdin=label), mock.patch.object(sys, "stdin", stdin):
                client = Client(self.answers())
                code, out, err = self.run_cli(client, yes=False)
                self.assertEqual(code, 1, err)
                self.assertIn("task_run", out)  # the active work is shown
                self.assertIn("--yes", err)
                self.assertEqual(client.sent, [ClientType.SHUTDOWN_REQUEST], "nothing was confirmed")
                self.assertNotIn("Traceback", err)

    def test_ctrl_c_while_waiting_reports_unconfirmed_termination(self):
        class Interrupted(Client):
            def pump(self, timeout):
                raise KeyboardInterrupt

        for json_form in (False, True):
            with self.subTest(json=json_form):
                client = Interrupted(self.answers(), closing_after=None)
                code, out, err = self.run_cli(client, json_form=json_form, BACKEND_GONE_WAIT=0.2)
                self.assertEqual(code, 1)
                self.assertIn("종료 확인 실패", err)
                self.assertIn("Ctrl-C", err)
                if json_form:
                    self.assertIsNone(json.loads(out.splitlines()[-1])["shutdown"])
                else:
                    self.assertIn("shutdown result: null", out)

    def test_ctrl_c_at_the_prompt_cancels_without_confirming(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        client = Client(self.answers())
        with mock.patch.object(sys, "stdin", Tty()), mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            code, _out, err = self.run_cli(client, yes=False)
        self.assertEqual(code, 1)
        self.assertEqual(client.sent, [ClientType.SHUTDOWN_REQUEST])
        self.assertIn("nothing was stopped", err)


if __name__ == "__main__":
    unittest.main()
