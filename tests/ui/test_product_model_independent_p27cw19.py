"""Independent CW-19 UI checks (p27-cw19-test-01): the product UI only shows the waits (C-D58, C-D71).

- a pending boot confirmation is shown with the CLI command (the UI has no confirm action);
- an unknown marker / unreadable record has its own wait text;
- survivors still alive are counted; ended ones are not; holds for metadata/model faults are named;
- an old restart line without survivors disappears after a while (no permanent noise).
"""
from __future__ import annotations

import unittest

from workbench.ui.product.model import ProductModel


class Sender:
    def __init__(self):
        self.sent = []

    def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def __getattr__(self, name):
        return lambda *a, **k: self.sent.append((name, a, k))


class BackendRecoveryText(unittest.TestCase):
    def model(self, state, now=1_000_000.0):
        model = ProductModel(Sender(), 40, 160, clock=lambda: now)
        model.state = state
        return model

    def test_boot_wait_shows_the_cli_command(self):
        text = self.model({"boot": {"confirmation_required": True, "reason": "reboot"}}).backend_recovery_text()
        self.assertIn("confirm-boot", text)
        self.assertIn("재부팅 확인 대기", text)

    def test_unknown_marker_wait(self):
        for reason in ("boot_marker_unknown", "boot_record_unreadable"):
            text = self.model({"boot": {"confirmation_required": True, "reason": reason}}).backend_recovery_text()
            self.assertIn("confirm-boot", text)
            self.assertNotIn("재부팅 확인 대기", text)

    def test_no_wait_when_confirmed(self):
        text = self.model({"boot": {"confirmation_required": False, "confirmed": True}}).backend_recovery_text()
        self.assertNotIn("confirm-boot", text)

    def test_survivors_counted_only_while_alive_and_holds_named(self):
        state = {"startup": {"classification": "same_boot_crash", "at": 1_000_000.0 - 5,
                             "run": {"state": "outcome_unknown"}, "outbox_lost_count": 2,
                             "survivors": [{"state": "alive"}, {"state": "ended"}, {"state": "stop_unconfirmed"},
                                           {"state": "stopped"}]},
                 "holds": [{"reason": "metadata_unavailable"}, {"reason": "model_hold:worker"}]}
        text = self.model(state).backend_recovery_text()
        self.assertIn("backend 비정상 종료 뒤 재시작", text)
        self.assertIn("결과 불명", text)
        self.assertIn("2건", text)
        self.assertIn("process 2개", text)
        self.assertIn("metadata 저장 장애", text)
        self.assertIn("모델 오류(worker)", text)

    def test_old_restart_line_without_survivors_fades(self):
        state = {"startup": {"classification": "same_boot_crash", "at": 1_000_000.0 - 3600, "survivors": []}}
        self.assertEqual(self.model(state).backend_recovery_text(), "")

    def test_fresh_start_shows_nothing(self):
        state = {"startup": {"classification": "fresh", "at": 1_000_000.0, "survivors": []}, "holds": []}
        self.assertEqual(self.model(state).backend_recovery_text(), "")


if __name__ == "__main__":
    unittest.main()
