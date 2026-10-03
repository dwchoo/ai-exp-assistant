"""Independent CW-06 tests of the real product UI loop on an owned PTY (p27-cw06-test-01).

The UI (``run_product``) runs unmodified against a scripted ui_v1 server; no real
OMP, no credentials. Expectations (CW-06.md, BRIEF C-AC-06/14/15/19, before the
implementation was read):
- Attach shows three areas at once, focus and owner separately, and the replayed
  pane tails; replayed terminal queries are not answered (no duplicate input).
- Ctrl-C / Ctrl-Z / Ctrl-\\ are forwarded as bytes (the UI is not signalled);
  a lone Esc reaches the pane promptly.
- A >2 MiB paste typed into the outer terminal is rejected whole with a visible
  reason; a Korean multi-line paste arrives as one intact paste frame.
- SIGWINCH makes the UI report every pane's new size.
- Exit paths (detach, backend closing, dropped connection, UI exception) restore
  the outer terminal: same termios, alt-screen/bracketed-paste off, cursor shown.
  Detach never requests shutdown.
"""
from pathlib import Path
import tempfile
import shutil
import time
import unittest

from independent_support_cw06 import ScriptedServer, UiPty, modes_restored, snap
from workbench.contracts import ui_v1
from workbench.ui.product.model import PANES, pane_inner_sizes

PREFIX = b"\x1d"
START, END = b"\x1b[200~", b"\x1b[201~"


class ProductPtyTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="cw06-indep-pty-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.work, True)

    def run_ui(self, server, rows=30, cols=120):
        self.addCleanup(server.close)
        ui = UiPty(server.path, self.work, rows=rows, cols=cols)
        self.addCleanup(ui.close)
        self.assertTrue(server.attached.wait(15), bytes(ui.output[-2000:]))
        self.assertTrue(ui.wait_text("HOST SHELL", 10), ui.screen_text())
        return ui

    def assert_restored(self, ui):
        self.assertTrue(ui.ui_done(15), bytes(ui.output[-3000:]))
        self.assertEqual(ui.after_file.read_text(), ui.before_file.read_text(), "outer termios not restored")
        self.assertEqual(modes_restored(bytes(ui.output)), {"alt_screen": True, "bracketed_paste": True,
                                                            "cursor_visible": True})

    # -- attach view -----------------------------------------------------
    def test_attach_shows_three_areas_focus_owner_and_replay_without_query_echo(self):
        server = ScriptedServer(snapshot=snap(owner="manager", mode="control_wait"), replay={
            "manager_omp": b"MGR-REPLAY\r\n\x1b[6n\x1b[c",
            "worker_omp": b"WRK-REPLAY\r\n\x1b[6n",
            "host_shell": b"SHELL-REPLAY$ \x1b[6n\x1b[>c"})
        ui = self.run_ui(server)
        for text in ("MANAGER OMP", "WORKER OMP", "HOST SHELL", "MGR-REPLAY", "WRK-REPLAY", "SHELL-REPLAY",
                     "focus: MANAGER OMP", "owner: manager", "마지막 확인"):
            self.assertTrue(ui.wait_text(text, 5), f"{text!r} not visible:\n{ui.screen_text()}")
        time.sleep(0.5)
        self.assertEqual(server.of("input"), [], "replayed queries answered as input")
        ui.send(PREFIX + b"q")
        self.assert_restored(ui)
        self.assertEqual(ui.status(), 0)
        self.assertEqual(len(server.of("detach")), 1)
        self.assertEqual([f for f in server.frames if str(f.header.get("type")).startswith("shutdown")], [])
        self.assertIn("backend keeps running", ui.screen_text())

    # -- keys ------------------------------------------------------------
    def test_signal_keys_are_forwarded_as_bytes_and_the_ui_survives(self):
        ui = self.run_ui(server := ScriptedServer())
        ui.send(PREFIX + b"2")
        self.assertTrue(server.wait(lambda: server.of("focus")))
        ui.send(b"\x03")
        ui.send(b"\x1a")
        ui.send(b"\x1c")
        self.assertTrue(server.wait(lambda: server.payloads("input", "worker_omp") == b"\x03\x1a\x1c"),
                        server.payloads("input"))
        self.assertIsNone(ui.status())
        self.assertTrue(ui.wait_text("WORKER OMP *FOCUS*", 3), ui.screen_text())
        ui.send(PREFIX + b"q")
        self.assert_restored(ui)

    def test_lone_esc_reaches_the_pane_promptly(self):
        ui = self.run_ui(server := ScriptedServer())
        t0 = time.monotonic()
        ui.send(b"\x1b")
        self.assertTrue(server.wait(lambda: server.payloads("input", "manager_omp") == b"\x1b", 2.0))
        self.assertLess(time.monotonic() - t0, 0.5, "Esc was held noticeably")
        ui.send(PREFIX + PREFIX)
        self.assertTrue(server.wait(lambda: server.payloads("input", "manager_omp") == b"\x1b\x1d", 2.0),
                        server.payloads("input"))
        ui.send(PREFIX + b"q")
        self.assert_restored(ui)

    # -- paste -----------------------------------------------------------
    def test_korean_multiline_paste_arrives_as_one_intact_frame(self):
        ui = self.run_ui(server := ScriptedServer())
        ui.send(PREFIX + b"3")
        self.assertTrue(server.wait(lambda: server.of("focus")))
        body = "echo 첫째줄\necho '둘째 줄 🙂'\n".encode() * 200
        ui.send(START + body + END, chunk=1000)
        self.assertTrue(server.wait(lambda: server.of("paste"), 5))
        time.sleep(0.3)
        pastes = server.of("paste")
        self.assertEqual(len(pastes), 1)
        self.assertEqual(pastes[0].header.get("pane"), "host_shell")
        self.assertEqual(pastes[0].payload, START + body + END)
        self.assertEqual(server.of("input"), [])
        ui.send(PREFIX + b"q")
        self.assert_restored(ui)

    def test_over_2mib_paste_is_rejected_whole_with_visible_reason(self):
        ui = self.run_ui(server := ScriptedServer())
        ui.send(PREFIX + b"3")
        self.assertTrue(server.wait(lambda: server.of("focus")))
        ui.send(START + b"q" * ui_v1.MAX_PASTE_BYTES + END, chunk=65536)
        self.assertTrue(ui.wait_text("paste_too_large", 20), ui.screen_text())
        time.sleep(0.3)
        self.assertEqual(server.of("paste"), [])
        self.assertNotIn(b"q", server.payloads("input"), "part of the rejected paste was delivered")
        ui.send(b"k")
        self.assertTrue(server.wait(lambda: server.payloads("input", "host_shell") == b"k"))
        ui.send(PREFIX + b"q")
        self.assert_restored(ui)

    def test_backend_queue_full_and_owner_refusals_are_visible(self):
        answers = {
            "paste": lambda h, p: {"ok": False, "reason": "queue_full", "detail": "free queue space 7"},
            "input": lambda h, p: ({"ok": False, "reason": "input_owner_manager",
                                    "detail": "manager owns shell input"} if p == b"z" else None),
        }
        ui = self.run_ui(server := ScriptedServer(answers=answers))
        ui.send(PREFIX + b"3")
        ui.send(START + b"hello" + END)
        self.assertTrue(ui.wait_text("queue_full", 5), ui.screen_text())
        ui.send(b"z")
        self.assertTrue(ui.wait_text("input_owner_manager", 5), ui.screen_text())
        ui.send(PREFIX + b"q")
        self.assert_restored(ui)

    # -- resize ----------------------------------------------------------
    def test_sigwinch_reports_every_pane_new_size(self):
        ui = self.run_ui(server := ScriptedServer(), rows=30, cols=120)
        time.sleep(0.3)
        before = len(server.of("resize"))
        ui.resize(45, 181)
        want = {p.value: pane_inner_sizes(45, 181)[p] for p in PANES}

        def got():
            return {f.header["pane"]: (f.header["rows"], f.header["cols"]) for f in server.of("resize")[before:]}
        self.assertTrue(server.wait(lambda: got() == want, 5), got())
        self.assertTrue(ui.wait_text("HOST SHELL", 3))
        ui.send(PREFIX + b"q")
        self.assert_restored(ui)

    # -- exit paths ------------------------------------------------------
    def test_backend_closing_restores_terminal_and_reports(self):
        ui = self.run_ui(server := ScriptedServer())
        server.closing("backend_shutdown")
        self.assert_restored(ui)
        self.assertEqual(ui.status(), 1)
        self.assertIn("backend_shutdown", ui.screen_text())

    def test_dropped_connection_restores_terminal(self):
        ui = self.run_ui(server := ScriptedServer())
        server.drop()
        self.assert_restored(ui)
        self.assertEqual(ui.status(), 1)

    def test_ui_exception_restores_terminal_and_reports_error(self):
        ui = self.run_ui(server := ScriptedServer())
        server.send({"v": 1, "type": "state", "snapshot": {"panes": ["not", "a", "dict"], "focus": 7}})
        self.assert_restored(ui)
        self.assertEqual(ui.status(), 1)
        self.assertIn("ui error", ui.screen_text())


if __name__ == "__main__":
    unittest.main()
