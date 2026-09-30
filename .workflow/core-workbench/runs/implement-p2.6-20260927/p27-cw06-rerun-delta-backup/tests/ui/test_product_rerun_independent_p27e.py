"""Independent CW-06 re-verification fixtures (p27-cw06-rerun-test-02): frozen candidate + F6-ui model notice."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO = Path(__file__).resolve().parents[2]

from support import FakeSender, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.ui.product.model import ProductModel  # noqa: E402

PREFIX = b"\x1d"

FROZEN = {
    "src/workbench/ui/product/input.py": "e3b87b1c40772f5705ebb45456d5af41cbd7d00a1e7b6de8f4cf725c62efad61",
    "src/workbench/ui/product/model.py": "2892ae20a5baf0330ae506abaff5dd67d0ebcda63c96b95d3aff335d53857659",
    "src/workbench/ui/product/app.py": "28eda32d93bee15e6df21a55f8a2d4a000318508a8d892501e0bcfe94a1e9ba2",
    "src/workbench/ui/product/view.py": "c41b895574027ab5391066c4cf7b04ba5744f53bd94fc943a2d1bee7446514d0",
    "src/workbench/backend/cli.py": "9b44e4847275514ec27e7c9474dfe7ad0c5556d3803651f7fc292be0402a2b4e",
}


class FrozenAndNoticeTests(unittest.TestCase):
    def test_frozen_production_files_unchanged(self):
        for rel, digest in FROZEN.items():
            self.assertEqual(hashlib.sha256((REPO / rel).read_bytes()).hexdigest(), digest, rel)

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
