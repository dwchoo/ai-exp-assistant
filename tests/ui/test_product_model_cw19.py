"""CW-19: the product UI status line shows the boot confirmation wait (C-D58: display only), the restart
reconcile with survivors and the fault holds (model / metadata / raw log), all in Korean."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, snapshot  # noqa: E402
from workbench.ui.product.model import (  # noqa: E402
    BOOT_WAIT_TEXT, STARTUP_SETTLED_SECONDS, STARTUP_SHOWN_SECONDS, ProductModel)

NOW = 5000.0


def model_with(**changes):
    clock = [NOW]
    model = ProductModel(FakeSender(), 30, 260, clock=lambda: clock[0])
    state = snapshot()
    state.update(changes)
    model.apply_snapshot(state)
    return model, clock


class Cw19StatusTests(unittest.TestCase):
    def test_boot_confirmation_wait_names_the_cli_command(self):
        model, _ = model_with(boot={"boot_id": "b", "confirmation_required": True, "confirmed": False,
                                    "reason": "reboot"})
        line = model.status_lines()[1]
        self.assertIn(BOOT_WAIT_TEXT, line)
        self.assertIn("python -m workbench confirm-boot", line)
        model, _ = model_with(boot={"boot_id": "b", "confirmation_required": False, "confirmed": True})
        self.assertNotIn("confirm-boot", model.status_lines()[1])

    def test_restart_reconcile_and_survivors(self):
        startup = {"classification": "same_boot_crash", "at": NOW - 5, "outbox_lost_count": 1,
                   "run": {"state": "outcome_unknown"},
                   "survivors": [{"survivor_id": "s1", "name": "host_shell", "pid": 5, "state": "alive"},
                                 {"survivor_id": "s2", "name": "run_target", "pid": 6, "state": "stopped"}]}
        model, clock = model_with(startup=startup)
        line = model.status_lines()[1]
        self.assertIn("backend 비정상 종료 뒤 재시작", line)
        self.assertIn("결과 불명", line)
        self.assertIn("보내지 못한 메시지 1건", line)
        self.assertIn("이전 backend의 process 1개 남음", line)
        clock[0] = NOW + STARTUP_SHOWN_SECONDS + 1  # the restart line ages out; survivors stay shown
        model.apply_snapshot({**snapshot(), "startup": startup})
        self.assertIn("이전 backend의 process 1개 남음", model.status_lines()[1])
        startup["survivors"] = []
        model.apply_snapshot({**snapshot(), "startup": startup})
        self.assertNotIn("재시작", model.status_lines()[1])

    def test_restart_line_clears_once_settled_or_after_a_bounded_time(self):
        """CW-16 O3: not for the whole backend lifetime; status --json keeps the record."""
        self.assertLessEqual(STARTUP_SHOWN_SECONDS, 180)
        startup = {"classification": "same_boot_crash", "at": NOW - 5, "outbox_lost_count": 1,
                   "run": {"state": "outcome_unknown"}, "survivors": []}
        held = {"task_id": "t1", "kind": "work", "status": "held", "held_reason": "backend_restarted"}
        model, clock = model_with(startup=startup, task=held)
        self.assertIn("backend 비정상 종료 뒤 재시작", model.status_lines()[1])
        clock[0] = NOW + 60  # the reconciled Task is still open: still shown
        model.apply_snapshot({**snapshot(), "startup": startup, "task": held})
        self.assertIn("backend 비정상 종료 뒤 재시작", model.status_lines()[1])
        model.apply_snapshot({**snapshot(), "startup": startup, "task": {**held, "status": "closed"}})
        self.assertNotIn("재시작", model.status_lines()[1], "the Task is closed and nothing survived")
        clock[0] = NOW + STARTUP_SHOWN_SECONDS + 1  # still open, but past the bound
        model.apply_snapshot({**snapshot(), "startup": startup, "task": held})
        self.assertNotIn("재시작", model.status_lines()[1])
        # No Task was open and nothing survived: shown only briefly.
        clock[0] = NOW
        model.apply_snapshot({**snapshot(), "startup": startup, "task": None})
        self.assertIn("재시작", model.status_lines()[1])
        clock[0] = NOW + STARTUP_SETTLED_SECONDS + 1
        model.apply_snapshot({**snapshot(), "startup": startup, "task": None})
        self.assertNotIn("재시작", model.status_lines()[1])

    def test_fault_holds_say_that_execution_continues(self):
        model, _ = model_with(holds=[{"reason": "model_hold:worker"}, {"reason": "metadata_unavailable"}],
                              faults={"raw_log": {"text": "raw log run 64 MiB 한도 도달, 저장 중지 — 실행 계속"}})
        line = model.status_lines()[1]
        self.assertIn("모델 오류(worker): 자동 작업 보류, 실험·관측 계속", line)
        self.assertIn("metadata 저장 장애", line)
        self.assertIn("64 MiB 한도 도달", line)

    def test_older_backends_without_the_fields(self):
        model, _ = model_with()
        self.assertNotIn("confirm-boot", model.status_lines()[1])
        model, _ = model_with(startup="x", holds="y", faults=None, boot=None)
        model.status_lines()


if __name__ == "__main__":
    unittest.main()
