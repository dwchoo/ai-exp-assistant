"""Independent CW-19 CLI checks (p27-cw19-test-01): ``confirm-boot`` and ``shutdown`` exit codes (C-D58, C-AC-22).

- confirm-boot: nothing running -> 3; nothing pending -> 1; unreadable current marker -> 1 (hold stays);
  no tty and no --yes -> 1 and nothing is sent; the backend's refusal -> 1; --yes -> the exact current boot id
  is sent and exit 0; the conditions shown include both boot ids, the data dir and the hold explanation;
- shutdown: a result that is not verified (or missing) is never exit 0 and prints "종료 확인 실패"; the active
  work list (previous survivors included) is printed before the confirmation; no --yes without a tty -> 1.
The UI socket is replaced by a fake client; no backend or OMP is started.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workbench.backend import cli
from workbench.contracts.ui_v1 import ClientType

BOOT_A = "aaaaaaaa-0000-4000-8000-0000000c0019"
BOOT_B = "bbbbbbbb-0000-4000-8000-0000000c0019"


class FakeClient:
    def __init__(self, answers, closing=None):
        self.answers = answers
        self.sent: list = []
        self.closing = closing

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, kind, **fields):
        self.sent.append((kind, fields))
        answer = self.answers.get(kind)
        return answer(fields) if callable(answer) else answer

    def pump(self, _timeout):
        return False


def pending_snapshot(boot_id=BOOT_B):
    return {"boot": {"boot_id": boot_id, "recorded_boot_id": BOOT_A, "confirmation_required": True,
                     "reason": "reboot"},
            "backend": {"data_dir": "/tmp/x", "project_dir": "/tmp/p", "omp_version": "omp/0"},
            "panes": {"host_shell": {"shell": {"kind": "bash", "executable": "/usr/bin/bash"}}},
            "automation": {"state": "paused", "paused": True},
            "task": {"task_id": "t-1", "kind": "experiment", "status": "held", "held_reason": "backend_restarted"},
            "startup": {"classification": "reboot", "run": {"run_id": "r-1", "state": "interrupted_by_reboot"},
                        "survivors": []},
            "holds": [{"reason": "boot_confirmation_required"}], "faults": {}}


class Cli(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw19-indep-cli-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def args(self, **kw):
        return argparse.Namespace(data_dir=str(self.root / "data"), yes=False, json=False, **kw)

    def run_cmd(self, fn, args, *, snapshot=None, client=None, tty=False):
        out, err = io.StringIO(), io.StringIO()
        patches = [mock.patch.object(cli, "_running_snapshot", lambda layout: snapshot),
                   mock.patch.object(cli.sys.stdin, "isatty", lambda: tty)]
        if client is not None:
            patches.append(mock.patch.object(cli, "UiClient", lambda *a, **k: client))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            (self.root / "data").mkdir(exist_ok=True, mode=0o700)
            code = fn(args)
        return code, out.getvalue(), err.getvalue()

    def test_confirm_boot_not_running(self):
        code, _out, _err = self.run_cmd(cli.cmd_confirm_boot, self.args(), snapshot=None)
        self.assertEqual(code, cli.EXIT_NOT_RUNNING)

    def test_confirm_boot_nothing_pending(self):
        snapshot = pending_snapshot()
        snapshot["boot"]["confirmation_required"] = False
        client = FakeClient({})
        code, _out, _err = self.run_cmd(cli.cmd_confirm_boot,
                                        argparse.Namespace(data_dir=str(self.root / "data"), yes=True, json=False),
                                        snapshot=snapshot, client=client)
        self.assertEqual(code, 1)
        self.assertEqual(client.sent, [])

    def test_confirm_boot_unreadable_marker_keeps_the_hold(self):
        client = FakeClient({})
        code, _out, err = self.run_cmd(cli.cmd_confirm_boot,
                                       argparse.Namespace(data_dir=str(self.root / "data"), yes=True, json=False),
                                       snapshot=pending_snapshot(None), client=client)
        self.assertEqual(code, 1)
        self.assertEqual(client.sent, [])

    def test_confirm_boot_refuses_without_tty_or_yes_and_sends_nothing(self):
        client = FakeClient({})
        code, out, err = self.run_cmd(cli.cmd_confirm_boot, self.args(), snapshot=pending_snapshot(), client=client)
        self.assertEqual(code, 1)
        self.assertEqual(client.sent, [])
        self.assertIn(BOOT_A, out)
        self.assertIn(BOOT_B, out)
        self.assertIn("/tmp/x", out)
        self.assertIn("refusing", err)

    def test_confirm_boot_tty_answer_no(self):
        client = FakeClient({})
        with mock.patch("builtins.input", lambda _p: "n"):
            code, _out, _err = self.run_cmd(cli.cmd_confirm_boot, self.args(), snapshot=pending_snapshot(),
                                            client=client, tty=True)
        self.assertEqual(code, 1)
        self.assertEqual(client.sent, [])

    def test_confirm_boot_yes_sends_the_current_boot_id(self):
        client = FakeClient({ClientType.CONFIRM_BOOT: {"ok": True, "boot": {"confirmation_required": False}}})
        code, out, _err = self.run_cmd(cli.cmd_confirm_boot,
                                       argparse.Namespace(data_dir=str(self.root / "data"), yes=True, json=False),
                                       snapshot=pending_snapshot(), client=client)
        self.assertEqual(code, 0)
        self.assertEqual(client.sent, [(ClientType.CONFIRM_BOOT, {"boot_id": BOOT_B})])
        self.assertIn("부팅 확인됨", out)

    def test_confirm_boot_backend_refusal_is_exit_1(self):
        client = FakeClient({ClientType.CONFIRM_BOOT: {"ok": False, "reason": "boot_id_mismatch", "detail": "x"}})
        code, _out, err = self.run_cmd(cli.cmd_confirm_boot,
                                       argparse.Namespace(data_dir=str(self.root / "data"), yes=True, json=False),
                                       snapshot=pending_snapshot(), client=client)
        self.assertEqual(code, 1)
        self.assertIn("boot_id_mismatch", err)

    def shutdown(self, result, *, yes=True, tty=False, active=None):
        client = FakeClient({ClientType.SHUTDOWN_REQUEST: {"token": "tok", "active": active or []},
                             ClientType.SHUTDOWN_CONFIRM: {"ok": True}},
                            closing=None if result is None else {"result": result})
        args = argparse.Namespace(data_dir=str(self.root / "data"), yes=yes, json=False)
        with mock.patch.object(cli, "_layout", lambda _a, **_k: SimpleLayout(self.root)):
            code, out, err = self.run_cmd(cli.cmd_shutdown, args, client=client, tty=tty)
        return code, out, err, client

    def test_shutdown_unverified_is_exit_1_with_the_failure_line(self):
        active = [{"kind": "previous_survivor", "survivor_id": "s1", "name": "host_shell", "pid": 4242}]
        code, out, err, _client = self.shutdown({"verified": False, "problems": ["previous_backend_survivors_alive"]},
                                                active=active)
        self.assertEqual(code, 1)
        self.assertIn("previous_survivor", out)
        self.assertIn("종료 확인 실패", err)
        self.assertIn("previous_backend_survivors_alive", err)

    def test_shutdown_without_result_is_exit_1(self):
        code, _out, err, _client = self.shutdown(None)
        self.assertEqual(code, 1)
        self.assertIn("종료 확인 실패", err)

    def test_shutdown_verified_is_exit_0(self):
        code, _out, err, _client = self.shutdown({"verified": True, "problems": []})
        self.assertEqual(code, 0)
        self.assertNotIn("종료 확인 실패", err)

    def test_shutdown_without_confirmation_sends_no_confirm(self):
        code, out, err, client = self.shutdown({"verified": True}, yes=False, tty=False,
                                               active=[{"kind": "previous_survivor", "survivor_id": "s1"}])
        self.assertEqual(code, 1)
        self.assertNotIn(ClientType.SHUTDOWN_CONFIRM, [kind for kind, _ in client.sent])
        self.assertIn("previous_survivor", out)


class SimpleLayout:
    def __init__(self, root):
        self.root = root
        self.ui_socket = root / "ui.sock"


if __name__ == "__main__":
    unittest.main()
