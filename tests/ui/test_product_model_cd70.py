"""C-D70 (2)/(3): the product UI status line shows a worker report waiting on the manager's composer and a worker
restart the manager asked for (with its reason)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.ui.product.model import (MANAGER_RESTART_SHOWN_SECONDS, REPORT_WAIT_TEXT,  # noqa: E402
                                       REPORT_WAIT_NO_MANAGER_TEXT, ProductModel)

NOW = 1000.0


def model_with(**changes):
    clock = [NOW]
    model = ProductModel(FakeSender(), 30, 200, clock=lambda: clock[0])
    state = snapshot()
    for key, value in changes.items():
        if key == "restart":
            state["panes"]["worker_omp"]["restart"] = value
        else:
            state[key] = value
    model.apply_snapshot(state)
    return model, clock


class RecoveryStatusTests(unittest.TestCase):
    def test_report_wait_is_shown_in_korean_and_cleared(self):
        model, _ = model_with(recovery={"report_wait": {"count": 1, "reason": "manager_editor_not_empty",
                                                        "since": NOW - 31}, "watch": None})
        self.assertEqual(REPORT_WAIT_TEXT, "worker 보고 대기 중: manager 입력창을 비우면 전달됩니다")
        self.assertIn(REPORT_WAIT_TEXT, model.status_lines()[1])
        model, _ = model_with(recovery={"report_wait": {"count": 3, "reason": "manager_editor_not_empty",
                                                        "since": NOW}, "watch": None})
        self.assertIn(REPORT_WAIT_TEXT + " (3건)", model.status_lines()[1])
        for recovery in ({"report_wait": None, "watch": None}, None, "x"):
            model, _ = model_with(recovery=recovery)
            self.assertNotIn("보고 대기", model.status_lines()[1])

    def test_report_wait_for_a_missing_manager_session_has_its_own_text(self):
        self.assertEqual(REPORT_WAIT_NO_MANAGER_TEXT,
                         "worker 보고 대기 중: manager OMP가 꺼져 있음 (다시 시작하면 전달)")
        model, _ = model_with(recovery={"report_wait": {"count": 1, "reason": "manager_session_not_connected",
                                                        "since": NOW - 5}, "watch": None})
        line = model.status_lines()[1]
        self.assertIn(REPORT_WAIT_NO_MANAGER_TEXT, line)
        self.assertNotIn(REPORT_WAIT_TEXT, line)
        model, _ = model_with(recovery={"report_wait": {"count": 2, "reason": "manager_session_not_connected",
                                                        "since": NOW}, "watch": None})
        self.assertIn(REPORT_WAIT_NO_MANAGER_TEXT + " (2건)", model.status_lines()[1])
        model, _ = model_with(recovery={"report_wait": None, "watch": None})  # cleared on delivery
        self.assertNotIn("보고 대기", model.status_lines()[1])

    def test_report_wait_without_or_with_an_unknown_reason_keeps_the_editor_text(self):
        for wait in ({"count": 1, "since": NOW}, {"count": 1, "reason": "something_new", "since": NOW}):
            model, _ = model_with(recovery={"report_wait": wait, "watch": None})
            self.assertIn(REPORT_WAIT_TEXT, model.status_lines()[1])

    def test_an_older_backend_without_recovery_shows_nothing_new(self):
        model, _ = model_with()
        self.assertEqual(model.recovery_text(), "")

    def test_a_manager_requested_worker_restart_is_shown_with_its_reason_for_a_while(self):
        restart = {"state": "restarted", "count": 1, "at": NOW - 5, "error": None, "cause": "restart_worker",
                   "requester": "manager", "reason": "no answer\nafter 2 checks"}
        model, clock = model_with(restart=restart)
        line = model.status_lines()[1]
        self.assertIn("worker 재시작됨 (manager 요청: no answer after 2 checks)", line)
        clock[0] = NOW + MANAGER_RESTART_SHOWN_SECONDS
        self.assertNotIn("worker 재시작됨", model.status_lines()[1])

    def test_a_long_reason_is_clipped_and_a_user_restart_is_not_shown(self):
        model, _ = model_with(restart={"state": "restarted", "at": NOW, "requester": "manager", "reason": "x" * 300})
        text = model.recovery_text()
        self.assertLess(len(text), 80)
        self.assertIn("…", text)
        model, _ = model_with(restart={"state": "restarted", "at": NOW, "requester": "user", "reason": None,
                                       "cause": "user_restart"})
        self.assertEqual(model.recovery_text(), "")
        model, _ = model_with(restart={"state": "failed", "at": NOW, "requester": "manager", "reason": "r"})
        self.assertEqual(model.recovery_text(), "")


class RejectionNoticeHeldTextTests(unittest.TestCase):
    """C-D70 pre-UR polish: held reasons in a refusal notice are plain Korean; unknown codes stay as they are."""

    def refuse(self, reasons, host=False):
        sender = FakeSender()
        model = ProductModel(sender, 42, 213)
        model.apply_snapshot(snapshot())
        model.handle_input(b"\x1dh")
        rid = f"r{len(sender.sent)}"
        model.on_result(ui_v1.result(rid, False, reason=ui_v1.Reason.HANDOFF_HELD, detail="busy",
                                     shell={"held_reasons": reasons}))
        return model.footer()

    def test_known_codes_are_shown_in_korean_not_as_codes(self):
        footer = self.refuse(["host_terminal_busy:worker_terminal_command"])
        self.assertIn("worker 명령 실행 중", footer)
        self.assertNotIn("worker_terminal_command", footer)
        footer = self.refuse(["host_terminal_busy"])
        self.assertIn("host terminal 사용 중", footer)
        self.assertNotIn("host_terminal_busy", footer)

    def test_unknown_codes_fall_back_to_the_raw_code_and_mix_with_known_ones(self):
        footer = self.refuse(["host_terminal_busy", "multiline_residue"])
        self.assertIn("host terminal 사용 중", footer)
        self.assertIn("multiline_residue", footer)

    def test_the_job_hint_stays_for_job_reasons(self):
        footer = self.refuse(["manual_jobs"])
        self.assertNotIn("manual_jobs", footer)
        self.assertIn("wb-handoff", footer)
        self.assertIn("prefix t,c", footer)


if __name__ == "__main__":
    unittest.main()
