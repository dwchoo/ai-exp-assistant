"""p27-cd68-o1-fix-01 PTY level: OMP's tmux-wrapped notification from a real pane never shows as text.

A real backend ShellPane (PTY) prints the exact bytes OMP 18.6.1 writes when ``TMUX`` is set (OSC 777 in
a tmux DCS passthrough with doubled ESC, plus a trailing BEL), split over two writes; the PTY output is
pumped into the product model as the UI receives it.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, snapshot  # noqa: E402
from workbench.backend.panes import ShellPane  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.terminal.shell_g2.prototype import ShellChoice  # noqa: E402
from workbench.ui.product.model import ProductModel  # noqa: E402

PAYLOAD = json.dumps({"event": "stop", "query": "사용자 입력", "response": "reply text"}, ensure_ascii=False)
OSC = f"\x1b]777;notify;warp://cli-agent;{PAYLOAD}\x07".encode()
WRAPPED = b"\x1bPtmux;" + OSC.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\\x07"


def octal(data: bytes) -> str:
    return "".join(f"\\{byte:03o}" for byte in data)


class PtyPassthroughTests(unittest.TestCase):
    def test_a_pane_printing_the_wrapped_notification_shows_no_control_text(self):
        with tempfile.TemporaryDirectory(prefix="cd68-o1-", dir="/tmp") as tmp:
            pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                             {"PATH": "/usr/bin:/bin", "HOME": tmp, "LANG": "C.UTF-8", "TMUX": "/tmp/fake,1,0"})
            model = ProductModel(FakeSender(), 30, 160, clock=lambda: 1000.0)
            model.apply_snapshot(snapshot())
            seen = bytearray()
            try:
                half = len(WRAPPED) // 2
                line = (f"printf 'BEFORE-'; printf '{octal(WRAPPED[:half])}'; sleep 0.2; "
                        f"printf '{octal(WRAPPED[half:])}'; printf -- '-AFTER\\n'\r").encode()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and pane.state.get("parent_mode") != "manual_prompt":
                    for chunk in pane.pump():
                        seen += chunk.data
                        model.on_display(ui_v1.Frame({"pane": "host_shell", "session_id": "s", "generation": 1},
                                                     chunk.data))
                    time.sleep(0.02)
                self.assertIsNone(pane.admit(line))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and b"-AFTER\r\n" not in bytes(seen):
                    for chunk in pane.pump():
                        seen += chunk.data
                        model.on_display(ui_v1.Frame({"pane": "host_shell", "session_id": "s", "generation": 1},
                                                     chunk.data))
                    time.sleep(0.02)
            finally:
                pane.close()
            self.assertIn(b"\x1bPtmux;\x1b\x1b]777;notify;", bytes(seen), "the real PTY carried OMP's exact bytes")
            screen = "\n".join(line.rstrip() for line in model.panes[PaneId.HOST_SHELL].screen.display)
            self.assertIn("BEFORE--AFTER", screen)
            for text in ("tmux;", "777", "notify", "warp://", "reply text", "사용자 입력"):
                self.assertNotIn(text, screen, text)



class ModelStreamStateTests(unittest.TestCase):
    """p27-cd68-o1-fix-02 (a)/(e): the string state belongs to one pane session; a cut tail starts outside strings."""

    def frame(self, data, session="s1", generation=1, replay=False):
        header = {"pane": "manager_omp", "session_id": session, "generation": generation}
        if replay:
            header["replay"] = True
        return ui_v1.Frame(header, data)

    def shown(self, model):
        return [line.rstrip() for line in model.panes[PaneId.MANAGER_OMP].screen.display if line.strip()]

    def test_a_new_session_starts_with_a_fresh_stream(self):
        model = ProductModel(FakeSender(), 30, 160, clock=lambda: 1000.0)
        model.apply_snapshot(snapshot())
        model.on_display(self.frame(b"old\r\n\x1bPtmux;never ended"))
        model.on_display(self.frame(b"new text\r\n", session="s2", generation=2))
        self.assertEqual(self.shown(model), ["new text"])
        model.on_display(self.frame(b"x\x1b", session="s3", generation=3))
        model.on_display(self.frame(b"[1my", session="s4", generation=4))
        self.assertEqual(self.shown(model), ["[1my"])

    def test_a_cut_tail_never_starts_inside_a_wrapped_notification(self):
        from workbench.ui.product.model import _align
        for cut in range(1, len(WRAPPED)):
            model = ProductModel(FakeSender(), 30, 160, clock=lambda: 1000.0)
            model.apply_snapshot(snapshot())
            model.on_display(self.frame(_align(WRAPPED[cut:]) + b"after", replay=True))
            with self.subTest(cut=cut):
                self.assertEqual(self.shown(model), ["after"])
        self.assertEqual(_align(b"abc\x1b[1mX"), b"\x1b[1mX", "an ordinary sequence start is kept")
        self.assertEqual(_align(b"tle\x07\x1b\\Y"), b"Y", "after a string terminator")



class AlignLoopTests(unittest.TestCase):
    """p27-cd68-o1-fix-03 (review-02 P3): ``_align`` is a bounded loop; a plain leading backslash is kept."""

    def test_many_string_terminators_do_not_recurse(self):
        from workbench.ui.product.model import _align
        for count in (5000, 200000):
            with self.subTest(count=count):
                self.assertEqual(_align(b"x" + b"\x1b\\" * count), b"")
                self.assertEqual(_align(b"x" + b"\x1b\\" * count + b"tail \x1b[1mok"), b"tail \x1b[1mok")

    def test_the_catch_up_tail_survives_many_terminators(self):
        from workbench.ui.product import model as product_model
        m = ProductModel(FakeSender(), 30, 160, clock=lambda: 1000.0)
        m.apply_snapshot(snapshot())
        header = {"pane": "manager_omp", "session_id": "s1", "generation": 1}
        flood = b"x" + b"\x1b\\" * (product_model.CATCHUP_BACKLOG_BYTES // 2 + 10)
        self.assertGreater(len(flood), product_model.CATCHUP_BACKLOG_BYTES, "large enough to trigger catch-up")
        m.enqueue_display(ui_v1.Frame(header, flood + b"\x1b[1mvisible"))
        for _ in range(1000):
            if not m.feed_pending(max_bytes=1 << 20, max_seconds=5.0):
                break
        shown = [line.rstrip() for line in m.panes[PaneId.MANAGER_OMP].screen.display if line.strip()]
        self.assertEqual(shown, ["visible"])
        self.assertIn("따라잡음", m.notice, "the catch-up path (with _align) ran")

    def test_a_plain_leading_backslash_is_kept_when_the_cut_is_not_inside_a_terminator(self):
        from workbench.ui.product.model import _align, _safe_tail
        self.assertEqual(_align(b"\\path text", before=b"a"), b"\\path text")
        self.assertEqual(_align(b"\\after st", before=b"\x1b"), b"after st", "the cut split ESC \\")
        self.assertEqual(_safe_tail(b"0123\\path", 5), b"\\path")
        self.assertEqual(_safe_tail(b"0123\x1b\\path", 5), b"path")


if __name__ == "__main__":
    unittest.main()
