import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from workbench.terminal.shell_g2.lifecycle import ManagedLifecycle
from workbench.terminal.shell_g2.prototype import InputBoundary, UnsafeShellState, ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell, _Transport


def ports(shell):
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": 1, "ownerEpoch": shell.snapshot()["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "a" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


class PersistentShellUnitTests(unittest.TestCase):
    def shell(self):
        adapter = PersistentShell.__new__(PersistentShell)
        boundary = InputBoundary(1)
        boundary.ready, boundary.dirty = True, False
        t = Mock(pid=42, generation=1, boundary=boundary, lifecycle=ManagedLifecycle(42),
                 control_wait_seen=True, manual_prompt_confirmed=False, takeover_requested=False,
                 confirmed_target=None, sent_id=None, events=(), choice=ShellChoice("bash", "/bin/bash"))
        type(t).owner_epoch = property(lambda self: self.boundary.owner_epoch)
        t.take_user_control.side_effect = lambda: boundary.owner_change("user")
        t.dispatch_managed.side_effect = lambda request, *_args, **_kwargs: t.lifecycle.begin(request)
        t._manual_prompt_is_current.return_value = False
        t._foreground_group.return_value = 42
        adapter._transport, adapter._seen, adapter._takeover_sent, adapter._approval_hash = t, set(), False, None
        return adapter

    def test_takeover_latches_before_confirmation_and_never_cancels_sent_work(self):
        shell = self.shell()
        control, auto = ports(shell)
        shell.submit(control, "exit 0", auto)
        requested = shell.request_takeover()
        self.assertTrue(requested["takeover_requested"])
        self.assertFalse(requested["takeover_confirmed"])
        self.assertEqual(requested["lifecycle"]["request_id"], control["payload"]["requestId"])
        fresh, auto = ports(shell)
        with self.assertRaises(UnsafeShellState): shell.submit(fresh, ":", auto)
        with self.assertRaises(UnsafeShellState): shell.send_user(b"unsafe\n")
        shell._transport.dispatch_managed.assert_called_once()
        self.assertIsNone(shell.snapshot()["task_success"])

    def test_stale_dirty_closed_ports_and_duplicate_request_hold(self):
        for mutation in ("parentPid", "generation", "ownerEpoch"):
            shell = self.shell()
            control, auto = ports(shell)
            control["payload"][mutation] += 1
            with self.assertRaises(UnsafeShellState): shell.submit(control, ":", auto)
            shell._transport.dispatch_managed.assert_not_called()
        for field in ("paused", "cancelled", "metadataHealthy", "approvalValid"):
            shell = self.shell()
            control, auto = ports(shell)
            auto["payload"][field] = field in {"paused", "cancelled"}
            with self.assertRaises(UnsafeShellState): shell.submit(control, ":", auto)
        shell = self.shell()
        control, auto = ports(shell)
        shell._transport.boundary.pending_line.extend(b"unsubmitted")
        with self.assertRaises(UnsafeShellState): shell.submit(control, ":", auto)
        shell._transport.boundary.pending_line.clear()
        shell.submit(control, "#WB_SUBSTITUTE_EXEC:ignored\n:", auto)
        self.assertEqual(shell._transport.dispatch_managed.call_args.args[1], ["/bin/bash", "-c", "#WB_SUBSTITUTE_EXEC:ignored\n:"])
        shell._transport.lifecycle = ManagedLifecycle(42)
        with self.assertRaises(UnsafeShellState): shell.submit(control, ":", auto)

    def test_initial_user_owner_needs_no_second_takeover(self):
        shell = self.shell()
        t = shell._transport
        t.boundary.owner_change("user")
        t.manual_prompt_confirmed = True
        t.control_wait_seen = False
        result = shell.request_takeover()
        self.assertFalse(result["takeover_requested"])
        t.take_user_control.assert_not_called()

    def test_reaped_child_at_exec_ready_does_not_invalidate_completion(self):
        t = _Transport.__new__(_Transport)
        t.pid = 42
        t.lifecycle = ManagedLifecycle(42)
        t.lifecycle.begin("request")
        t.lifecycle.child_pid = 101
        t.lifecycle.child_prepared = t.lifecycle.foreground_verified = True
        t.boundary = InputBoundary(1)
        t._child_start = "stale"
        t._proc = Mock(side_effect=FileNotFoundError)
        t._on_control_event("EXEC_READY:101")
        self.assertTrue(t.lifecycle.exec_ready)
        self.assertFalse(t.lifecycle.unknown)
        self.assertIsNone(t._child_start)

    def test_manual_input_revalidates_parent_identity_foreground_and_late_loss(self):
        shell = self.shell()
        t = shell._transport
        t.boundary.owner_change("user")
        t.manual_prompt_confirmed = True
        t.control_wait_seen = False
        t._closed = False
        t._parent_start = "original"
        t.master_fd = 55
        fields = ["S", str(os.getpid()), "42", "42"] + ["0"] * 15 + ["original"]
        t._proc.return_value = fields
        t._manual_prompt_is_current.side_effect = lambda: _Transport._manual_prompt_is_current(t)
        t._manual_foreground_group.side_effect = lambda: _Transport._manual_foreground_group(t)
        t.send_manual_parent.side_effect = lambda data: _Transport.send_manual_parent(t, data)

        fields[19] = "reused"
        with self.assertRaises(UnsafeShellState): shell.send_user(b"stale\n")
        fields[19] = "original"
        fields[0] = "T"
        with self.assertRaises(UnsafeShellState): shell.send_user(b"stopped parent\n")
        fields[0] = "S"
        t._foreground_group.return_value = 43
        with self.assertRaises(UnsafeShellState): shell.send_user(b"wrong foreground\n")
        t._foreground_group.return_value = 42
        drains = 0
        def lose_control(*_args, **_kwargs):
            nonlocal drains
            drains += 1
            if drains == 2:
                t.boundary.fail_closed()
        t._drain.side_effect = lose_control
        with self.assertRaises(UnsafeShellState): shell.send_user(b"late loss\n")
        t._write_all.assert_not_called()

        t._drain.side_effect = None
        t.boundary.needs_review = False
        with patch("workbench.terminal.shell_persistent.adapter.os.write", return_value=6) as writer:
            shell.send_user(b"valid\n")
        writer.assert_called_once()
        self.assertEqual(bytes(writer.call_args.args[1]), b"valid\n")

    def test_manual_writer_partial_retry_and_no_progress_are_bounded(self):
        def transport():
            t = _Transport.__new__(_Transport)
            t.boundary = InputBoundary(1)
            t.boundary.owner_change("user")
            t.master_fd = 55
            t._drain = Mock()
            t._manual_prompt_is_current = Mock(return_value=True)
            return t

        t = transport()
        with patch("workbench.terminal.shell_persistent.adapter.os.write", side_effect=[1, 2]) as writer:
            t.send_manual_parent(b"abc")
        self.assertEqual([bytes(call.args[1]) for call in writer.call_args_list], [b"abc", b"bc"])
        self.assertEqual(bytes(t.boundary.pending_line), b"abc")

        t = transport()
        with (patch("workbench.terminal.shell_persistent.adapter.os.write",
                    side_effect=[InterruptedError(), 2]) as writer,
              patch("workbench.terminal.shell_persistent.adapter.select.select",
                    return_value=([], [55], []))):
            t.send_manual_parent(b"ab")
        self.assertEqual(writer.call_count, 2)
        self.assertEqual(bytes(t.boundary.pending_line), b"ab")

        t = transport()
        with (patch("workbench.terminal.shell_persistent.adapter.os.write",
                    side_effect=BlockingIOError) as writer,
              patch("workbench.terminal.shell_persistent.adapter.select.select",
                    return_value=([], [], []))):
            with self.assertRaises(TimeoutError):
                t.send_manual_parent(b"held")
        writer.assert_called_once()
        self.assertTrue(t.boundary.needs_review)
