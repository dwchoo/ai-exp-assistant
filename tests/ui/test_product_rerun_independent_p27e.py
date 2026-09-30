"""Independent CW-06 re-verification fixtures (p27-cw06-rerun-test-02): frozen candidate + F6-ui model notice."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FakeSender, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.ui.product.model import ProductModel  # noqa: E402

PREFIX = b"\x1d"


class NoticeTests(unittest.TestCase):
    def test_handoff_refusal_notice_lists_backend_held_reasons(self):
        sender = FakeSender()
        model = ProductModel(sender, 42, 213)
        model.apply_snapshot(snapshot())
        model.handle_input(PREFIX + b"h")
        rid = f"r{len(sender.sent)}"
        model.on_result(ui_v1.result(rid, False, reason=ui_v1.Reason.HANDOFF_HELD, detail="boundary unproven",
                                     shell={"held_reasons": ["unsubmitted_or_unconsumed_input", "multiline_residue"]}))
        for needle in ("handoff_held", "unsubmitted_or_unconsumed_input", "multiline_residue"):
            self.assertIn(needle, model.footer())


if __name__ == "__main__":
    unittest.main()
