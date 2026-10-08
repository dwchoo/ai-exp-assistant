"""CW-16 fix-01 product UI: B3 F2 (the pause-store failure stays visible at 170 columns) and D-B2-3 (usage on
the status line; unknown model usage is '미확인', never a number)."""
import sys
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_product_model import task_state  # noqa: E402
from support import FakeSender  # noqa: E402
from workbench.ui.product.model import ProductModel  # noqa: E402


def visible(text: str, cols: int) -> str:
    """The part of a status line a ``cols`` wide terminal shows (wide characters take two columns)."""
    out, used = [], 0
    for char in text:
        width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + width > cols - 1:
            break
        out.append(char)
        used += width
    return "".join(out)


def render(snap, cols=170):
    model = ProductModel(FakeSender(), 40, cols, clock=lambda: 1000.0)
    model.apply_snapshot(snap)
    return model


class PauseStoreFailureTests(unittest.TestCase):
    def crowded(self, **automation):
        """The B3 X6 state: metadata hold, raw-log fault, a long Task line and a pause that was not stored."""
        snap = task_state(task={"kind": "experiment", "status": "waiting_report", "held_reason": "paused",
                                "summary": "x6 raw log cap and metadata fault scenario with a long summary"},
                          automation={"state": "paused", "paused": True, "persistence_error": "pause_not_stored",
                                      **automation},
                          holds=[{"reason": "metadata_unavailable", "since": 1.0, "detail": None}],
                          faults={"raw_log": {"text": "raw log 저장 장애(PermissionError:13): 누락 1234 bytes — 실행 계속"}})
        return snap

    def test_visible_at_170_columns_with_other_faults(self):
        model = render(self.crowded())
        line2 = model.status_lines()[1]
        self.assertIn("저장 오류", visible(line2, 170), line2)
        self.assertEqual(line2.count("저장 오류"), 1, "shown once")

    def test_boot_wait_still_comes_first(self):
        snap = self.crowded()
        snap["boot"] = {"confirmation_required": True, "reason": "reboot"}
        line2 = render(snap).status_lines()[1]
        self.assertTrue(line2.startswith("재부팅 확인 대기"), line2)
        self.assertIn("저장 오류", visible(line2, 170), line2)

    def test_absent_without_a_store_error(self):
        snap = self.crowded()
        snap["automation"]["persistence_error"] = None
        self.assertNotIn("저장 오류", render(snap).status_lines()[1])


class UsageLineTests(unittest.TestCase):
    def usage(self, **model):
        return {"task_id": "t1", "runs_started": 2, "retries_used": 1, "retry_limit": 3, "review_count": 4,
                "model": {"tokens_observed": "unknown", "tokens_estimated": "unknown", **model},
                "model_known": any(isinstance(v, int) for v in model.values())}

    def test_unknown_model_usage_is_shown_as_unknown(self):
        line2 = render(task_state(task={}, usage=self.usage())).status_lines()[1]
        self.assertIn("사용량: 재시도 1/3 · 점검 4 · 모델 미확인", line2)

    def test_reported_model_usage(self):
        line2 = render(task_state(task={}, usage=self.usage(tokens_observed=1500))).status_lines()[1]
        self.assertIn("모델 1500 tok", line2)
        line2 = render(task_state(task={}, usage=self.usage(tokens_estimated=900))).status_lines()[1]
        self.assertIn("모델 ~900 tok(추정)", line2)

    def test_no_task_no_usage_text_and_an_older_backend(self):
        self.assertNotIn("사용량", render(task_state(usage=self.usage())).status_lines()[1])
        self.assertNotIn("사용량", render(task_state(task={})).status_lines()[1])


if __name__ == "__main__":
    unittest.main()
