"""CW-18 independent verification (p27-cw18-test-01): the product UI's Task / worker / automation state.

Expectations (C-D66 "UI는 현재 Task와 worker 상태를 표시", CW-18 U5 required behaviour):
- every Task status / kind / worker state the backend can publish (``ui_v1`` vocabularies and the REAL
  ``TaskFlow`` views) has a readable text; unknown or wrongly typed fields never break the UI;
- backend text (Task summary, reasons) cannot inject terminal control sequences into the status lines;
- the worker state and the current Task stay visible on a common 80-column terminal;
- p / Ctrl-p / ㅔ open the pause (running) or resume (paused) confirmation, only a confirm key sends, and
  resume always carries ``reconciled: true``; the k (kill) confirmation keeps working next to it.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from wcwidth import wcswidth

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender  # noqa: E402
from workbench.backend.flow_tasks import FlowTask  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.ui.product.input import PREFIX  # noqa: E402
from workbench.ui.product.model import ProductModel  # noqa: E402

P = bytes([PREFIX])


def visible(text: str, cols: int) -> str:
    out, used = [], 0
    for ch in text:
        size = max(wcswidth(ch), 0)
        if used + size > cols:
            break
        out.append(ch)
        used += size
    return "".join(out)


def model(cols=120, rows=30):
    return ProductModel(FakeSender(), rows, cols, clock=lambda: 1000.0)


def flow_task(**changes):
    task = FlowTask(str(uuid4()), "experiment", summary="lr sweep on the parser benchmark")
    task.set_status("running")
    for name, value in changes.items():
        setattr(task, name, value)
    return task


class VocabularyTests(unittest.TestCase):
    def test_every_published_task_status_kind_and_worker_state_has_text(self):
        for status in ui_v1.TASK_STATUSES:
            for kind in ui_v1.TASK_KINDS:
                with self.subTest(status=status, kind=kind):
                    m = model()
                    view = flow_task(kind=kind)
                    view.set_status(status)
                    m.apply_snapshot({"task": view.view(), "worker": {"state": "busy", "task_id": view.task_id}})
                    line1, line2 = m.status_lines()
                    self.assertNotIn(f"[{status}", line2, "raw status shown instead of its text")
                    self.assertIn("작업:", line2)
        for state in ui_v1.WORKER_STATES:
            m = model()
            m.apply_snapshot({"worker": {"state": state, "task_id": None}})
            self.assertNotIn(f"worker: {state}", m.status_lines()[0])

    def test_held_reasons_from_the_flow_are_shown(self):
        for reason in ("host_terminal_busy", "backend_restarted", "paused", "worker_busy"):
            with self.subTest(reason=reason):
                m = model()
                m.apply_snapshot({"task": flow_task(held_reason=reason, status="dispatched").view(),
                                  "worker": {"state": "busy", "task_id": "x"}})
                line2 = m.status_lines()[1]
                self.assertTrue(reason in line2 or (reason == "host_terminal_busy" and "host terminal 사용 중" in line2),
                                line2)

    def test_automation_interruption_states_have_text(self):
        for state in ("requested", "confirmed", "unknown"):
            m = model()
            m.apply_snapshot({"automation": {"state": "paused", "paused": True, "interruption": {"state": state}}})
            self.assertIn("중단", m.status_lines()[1], state)


class RobustnessTests(unittest.TestCase):
    def test_control_sequences_in_backend_text_never_reach_the_status_lines(self):
        evil = "\x1b]52;c;ZXZpbA==\x07\x1b[2J\x1b[31mred\r\nnext\x00\x9b31m"
        m = model()
        m.apply_snapshot({"task": {"task_id": "t", "kind": "work", "status": "held", "summary": evil,
                                   "held_reason": evil, "closed_reason": evil},
                          "worker": {"state": evil, "task_id": "t"},
                          "automation": {"state": "held", "paused": False, "detail": evil,
                                         "review": {"applies": True, "status": "delayed", "reason": evil},
                                         "resume": {"outcome": "refused", "reason": evil}}})
        for line in m.status_lines():
            for bad in ("\x1b", "\x07", "\r", "\n", "\x00", "\x9b"):
                self.assertNotIn(bad, line)

    def test_wrong_types_and_missing_fields_do_not_break_the_ui(self):
        shapes = [
            {"task": {"kind": 5, "status": ["x"], "summary": {"a": 1}, "held_reason": 3, "cancel_requested": "yes"}},
            {"task": "busy", "worker": "busy", "automation": ["paused"]},
            {"task": None, "worker": None, "automation": None},
            {"worker": {"state": None}, "automation": {"state": None, "review": "x", "interruption": 5}},
            {"automation": {"state": "paused", "paused": True, "resume": "refused", "interruption": {"state": 3}}},
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                m = model()
                m.apply_snapshot(shape)
                m.status_lines()
                m.footer()
                m.handle_input(P + b"p")  # must not raise; unknown automation never opens a confirmation
                if not isinstance(shape.get("automation"), dict) or not shape["automation"].get("state"):
                    self.assertFalse(m.pause_confirm_open)
                    self.assertEqual(m.sender.of("pause") + m.sender.of("resume"), [])

    def test_worker_and_task_are_visible_on_an_80_column_terminal(self):
        m = model(cols=80)
        task = flow_task(held_reason="host_terminal_busy", status="dispatched")
        m.apply_snapshot({"task": task.view(), "worker": {"state": "busy", "task_id": task.task_id},
                          "automation": {"state": "active", "paused": False}})
        line1, line2 = (visible(line, 80) for line in m.status_lines())
        self.assertIn("작업:", line2, line2)
        self.assertIn("host terminal 사용 중", line2, f"held reason cut at 80 columns: {line2!r}")
        self.assertIn("worker", line1 + line2, f"worker state cut at 80 columns: {line1!r} / {line2!r}")


class PauseResumeConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.m = model()

    def automation(self, state):
        self.m.apply_snapshot({"automation": {"state": state, "paused": state == "paused"}})

    def test_running_pause_needs_the_confirm_key_and_sends_no_fields(self):
        self.automation("active")
        self.m.handle_input(P + "ㅔ".encode() + b" ")
        self.assertTrue(self.m.pause_confirm_open)
        self.assertIn("자동화를 일시정지합니다 (p: 확인)", "\n".join(self.m.pause_confirm_lines()))
        self.m.handle_input(b"\x10")
        self.assertEqual(self.m.sender.of("pause"), [("pause", {}, b"")])

    def test_paused_resume_always_sends_reconciled_true(self):
        self.automation("paused")
        self.m.handle_input(P + b"p")
        self.assertIn("대조 후 재개합니다 (p: 확인)", "\n".join(self.m.pause_confirm_lines()))
        self.m.handle_input(b"p")
        self.assertEqual(self.m.sender.of("resume"), [("resume", {"reconciled": True}, b"")])
        self.assertEqual(self.m.sender.of("pause"), [])

    def test_transition_states_never_open_a_second_request(self):
        for state in ("pausing", "resuming"):
            with self.subTest(state=state):
                self.setUp()
                self.automation(state)
                self.m.handle_input(P + b"p")
                self.m.handle_input(b"p")
                self.assertEqual(self.m.sender.of("pause") + self.m.sender.of("resume"), [])

    def test_kill_confirmation_still_works_and_never_pauses(self):
        self.automation("active")
        self.m.handle_input(P + b"k")
        self.assertTrue(self.m.kill_confirm_open)
        self.assertFalse(self.m.pause_confirm_open)
        self.m.handle_input(b"p")  # p does not confirm a kill and does not pause
        self.assertEqual(self.m.sender.of("kill_pane") + self.m.sender.of("pause"), [])
        self.m.handle_input(P + b"k")
        self.m.handle_input("ㅏ".encode())
        self.assertEqual(len(self.m.sender.of("kill_pane")), 1)
        self.assertEqual(self.m.sender.of("pause"), [])

    def test_confirmation_keys_never_reach_a_pane(self):
        self.automation("active")
        self.m.handle_input(P + b"p")
        self.m.handle_input(b"x")
        self.assertEqual(self.m.sender.of("input"), [])
        self.assertEqual(self.m.sender.of("pause"), [])


if __name__ == "__main__":
    unittest.main()
