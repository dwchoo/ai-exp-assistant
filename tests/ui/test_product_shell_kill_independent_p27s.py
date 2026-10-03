"""Independent C-D63 UI checks (p27-shell-test-01): confirmed host terminal force-kill and Enter restart.

Expectations were drafted from DECISIONS C-D63 and the UI worker's required_behavior before the implementation was
read:

* ``Ctrl-] k`` in every IME-neutral form (letter, Ctrl-k, jamo ㅏ alone or with the IME's Space commit, the command
  menu) opens a confirmation modal; while it is open nothing reaches any pane, whatever pane has focus;
* only the k forms (k, Ctrl-k, ㅏ, prefix k) confirm and send exactly one ``kill_pane{pane: host_shell}``; every other
  input cancels (Esc, Enter, other keys, Hangul, paste - even one containing ``k`` - mouse click, wheel, resize) and
  nothing is sent;
* the modal says what happens; when the manager owns the shell input or has a command in flight it adds the warning;
* a pending kill is not duplicated; the result and every refusal are visible;
* an exited host shell keeps its screen, shows 'host terminal 종료됨 (exit N) — Enter: 새 shell 시작', Enter sends one
  ``restart_pane{pane: host_shell}``, other input/paste is not sent, refusals are shown and Enter retries;
* the command menu has a host terminal kill row without renumbering the existing items.

Model tests use a recording sender. The PTY test runs the real product UI against a scripted ui_v1 server
(no backend, no OMP, no model).
"""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from independent_support_cw06 import RecordingSender, ScriptedServer, UiPty, snap  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product.input import PARTIAL_HOLD_SECONDS, PREFIX  # noqa: E402
from workbench.ui.product.model import HELP_LINES, MENU_ITEMS, ProductModel  # noqa: E402

P = bytes([PREFIX])
HOLD = PARTIAL_HOLD_SECONDS + 0.001
PASTE = b"\x1b[200~%s\x1b[201~"
JAMO_K = "ㅏ".encode()
KILL_TEXT = "host terminal을 강제 종료합니다. 실행 중인 process가 모두 종료됩니다."
WARNING = "manager가 사용 중입니다 — 진행 중인 명령은 결과 불명으로 남습니다"
OPEN_FORMS = {"prefix_k": P + b"k", "prefix_ctrl_k": P + b"\x0b", "prefix_jamo": P + JAMO_K,
              "prefix_jamo_space": P + JAMO_K + b" ", "menu_ctrl_k": [P + b" ", b"\x0b"]}
CONFIRM_FORMS = {"k": b"k", "K": b"K", "ctrl_k": b"\x0b", "jamo": JAMO_K, "jamo_space": JAMO_K + b" ",
                 "prefix_k": P + b"k", "prefix_ctrl_k": P + b"\x0b", "prefix_jamo": P + JAMO_K}
CANCEL_FORMS = {"esc": b"\x1b", "enter": b"\r", "lf": b"\n", "y": b"y", "j": b"j", "space": b" ", "ctrl_c": b"\x03",
                "digit": b"1", "q": b"q", "tab": b"\t", "up": b"\x1b[A", "backspace": b"\x7f",
                "hangul_syllable": "한".encode(), "other_jamo": "ㅇ".encode(), "alt_k": b"\x1bk",
                "paste_with_k": PASTE % b"k", "paste_text": PASTE % b"rm -rf x\r",
                "click": b"\x1b[<0;10;10M", "click_release": b"\x1b[<0;10;10m", "wheel_up": b"\x1b[<64;10;10M",
                "wheel_down": b"\x1b[<65;10;10M", "shift_pgup": b"\x1b[5;2~", "prefix_q": P + b"q",
                "prefix_space": P + b" ", "prefix_1": P + b"1"}


def make(*, owner: str = "user", focus: str = "manager_omp", host_alive: bool = True, exit_status=None,
         in_flight: bool = False, generation: int = 1):
    sender = RecordingSender()
    model = ProductModel(sender, 40, 150, clock=lambda: 1000.0)
    snapshot = snap(owner=owner, focus=focus)
    host = snapshot["panes"]["host_shell"]
    host.update(alive=host_alive, exit_status=exit_status, generation=generation, manager_command_in_flight=in_flight,
                session_id="11111111-1111-4111-8111-111111111111")
    model.apply_snapshot(snapshot, adopt_focus=True)
    sender.sent.clear()
    return model, sender


def feed(model, data, t: float = 0.0) -> float:
    for part in (data if isinstance(data, list) else [data]):
        model.handle_input(part, now=t)
        model.flush_input(now=t + HOLD)
        t += 1.0
    return t


def pane_frames(sender) -> list:
    return [s for s in sender.sent if s[0] in ("input", "paste")]


class KillConfirmTests(unittest.TestCase):
    def test_every_open_form_shows_the_modal_and_sends_nothing_from_any_focus(self):
        for name, data in OPEN_FORMS.items():
            for focus in ("manager_omp", "worker_omp", "host_shell"):
                with self.subTest(form=name, focus=focus):
                    m, s = make(focus=focus)
                    feed(m, data)
                    self.assertTrue(m.kill_confirm_open, f"{name} did not open the confirmation")
                    self.assertEqual(s.sent, [], "something was sent while opening the confirmation")
                    lines = m.kill_confirm_lines()
                    self.assertIn(KILL_TEXT, lines)
                    self.assertTrue(any("k" in line and "취소" in line for line in lines), lines)
                    self.assertNotIn(WARNING, lines)
                    self.assertIn("취소", m.footer())

    def test_every_confirm_form_sends_exactly_one_kill_for_the_host_shell_and_no_pane_bytes(self):
        for open_name, opener in OPEN_FORMS.items():
            for name, data in CONFIRM_FORMS.items():
                with self.subTest(open=open_name, confirm=name):
                    m, s = make(focus="worker_omp")
                    t = feed(m, opener)
                    feed(m, data, t)
                    self.assertEqual([k[:3] for k in s.of("kill_pane")], [("kill_pane", {"pane": "host_shell"}, b"")])
                    self.assertEqual(pane_frames(s), [], "a key of the confirmation reached a pane")
                    self.assertFalse(m.kill_confirm_open)
                    self.assertFalse(m.quit)

    def test_split_reads_confirm_only_with_the_k_alone_and_bytes_after_the_k_in_the_same_read_cancel(self):
        # root-adjudication-p27-cd63-r3 (review R3): a confirming read is exactly one confirm key.
        m, s = make()
        for part in (P, b"k"):
            m.handle_input(part, now=0.0)
            m.flush_input(now=0.5)
        self.assertTrue(m.kill_confirm_open)
        feed(m, b"kls -la\r", 1.0)  # k plus typeahead in ONE read after the confirmation opened
        self.assertFalse(m.kill_confirm_open)
        self.assertEqual(s.of("kill_pane"), [], "a read that merely starts with k confirmed the kill")
        self.assertEqual(pane_frames(s), [], "keys typed after the k reached a pane")
        # The same opener, then k arriving alone in its own read: confirms exactly once.
        m3, s3 = make()
        for part in (P, b"k"):
            m3.handle_input(part, now=0.0)
            m3.flush_input(now=0.5)
        self.assertTrue(m3.kill_confirm_open)
        feed(m3, b"k", 1.0)
        self.assertEqual([k[:3] for k in s3.of("kill_pane")], [("kill_pane", {"pane": "host_shell"}, b"")])
        self.assertEqual(pane_frames(s3), [])
        m2, s2 = make()
        feed(m2, P + b"kk")  # both keys in one read: open and confirm
        self.assertEqual(len(s2.of("kill_pane")), 1)
        self.assertEqual(pane_frames(s2), [])

    def test_every_other_input_cancels_sends_nothing_and_the_next_k_is_plain_text(self):
        for name, data in CANCEL_FORMS.items():
            with self.subTest(cancel=name):
                m, s = make()
                t = feed(m, P + b"k")
                t = feed(m, data, t)
                self.assertFalse(m.kill_confirm_open, f"{name} did not cancel")
                self.assertEqual(s.of("kill_pane"), [], f"{name} confirmed the kill")
                self.assertEqual(pane_frames(s), [], f"{name} reached a pane")
                self.assertFalse(m.quit, f"{name} quit the UI from inside the confirmation")
                if m.menu_open:
                    feed(m, b"\x1b", t)
                s.sent.clear()
                feed(m, b"k", t + 2)
                self.assertEqual(s.of("kill_pane"), [])
                self.assertEqual(s.payloads("input"), b"k", "a later k is not plain text for the focus pane")

    def test_without_the_prefix_ctrl_k_k_and_jamo_are_ordinary_pane_input(self):
        for focus in ("host_shell", "manager_omp"):
            with self.subTest(focus=focus):
                m, s = make(focus=focus)
                feed(m, b"\x0b")  # readline kill-line in the host shell
                feed(m, b"k", 2.0)
                feed(m, JAMO_K, 3.0)
                self.assertFalse(m.kill_confirm_open)
                self.assertEqual(s.of("kill_pane"), [])
                self.assertEqual(s.payloads("input", focus), b"\x0bk" + JAMO_K)

    def test_scroll_mode_jamo_keys_never_open_or_confirm_a_kill(self):
        m, s = make(focus="host_shell")
        t = feed(m, P + b"[")
        t = feed(m, JAMO_K + b"k" + b"\x0b", t)
        self.assertFalse(m.kill_confirm_open)
        self.assertEqual(s.of("kill_pane"), [])
        feed(m, P + b"k", t)  # the prefix still opens it from scroll mode, and nothing is sent
        self.assertTrue(m.kill_confirm_open)
        self.assertEqual((s.of("kill_pane"), pane_frames(s)), ([], []))

    def test_a_lone_prefix_keeps_the_confirmation_until_the_next_key_decides(self):
        m, s = make()
        t = feed(m, P + b"k")
        t = feed(m, P, t)
        self.assertTrue(m.kill_confirm_open)
        feed(m, b"x", t)
        self.assertFalse(m.kill_confirm_open)
        self.assertEqual((s.of("kill_pane"), pane_frames(s)), ([], []))

    def test_a_window_resize_cancels_the_confirmation(self):
        m, s = make()
        feed(m, P + b"k")
        m.resize(44, 160)
        self.assertFalse(m.kill_confirm_open, "a resize did not cancel the confirmation")
        self.assertEqual((s.of("kill_pane"), pane_frames(s)), ([], []))

    def test_a_resize_never_confirms_and_nothing_reaches_a_pane(self):
        m, s = make()
        t = feed(m, P + b"k")
        m.resize(44, 160)
        m.resize(20, 70)
        self.assertEqual((s.of("kill_pane"), pane_frames(s)), ([], []))
        self.assertFalse(m.kill_confirm_open, "the resize left the confirmation open")
        feed(m, b"\x1b", t)  # Root adjudication C-D63: once closed by the resize, Esc is ordinary input
        self.assertFalse(m.kill_confirm_open)
        self.assertEqual(s.of("kill_pane"), [])
        self.assertEqual([f[:3] for f in pane_frames(s)], [("input", {"pane": "manager_omp"}, b"\x1b")])

    def test_state_pushes_and_output_while_open_neither_close_nor_confirm_it(self):
        m, s = make()
        feed(m, P + b"k")
        m.on_state(snap(owner="manager"))
        self.assertTrue(m.kill_confirm_open)
        self.assertIn(WARNING, m.kill_confirm_lines(), "the warning does not follow a state push")
        self.assertEqual(s.of("kill_pane"), [])


class ManagerWarningTests(unittest.TestCase):
    def test_warning_when_the_manager_owns_the_input_or_has_a_command_in_flight(self):
        for owner, in_flight, warned in (("user", False, False), ("manager", False, True), ("user", True, True),
                                         ("manager", True, True)):
            with self.subTest(owner=owner, in_flight=in_flight):
                m, s = make(owner=owner, in_flight=in_flight)
                feed(m, P + b"\x0b")
                lines = m.kill_confirm_lines()
                self.assertEqual(WARNING in lines, warned, lines)
                self.assertIn(KILL_TEXT, lines)
                self.assertEqual(s.sent, [])
                feed(m, b"k", 5.0)  # C-D63: allowed after the confirmation even when the manager uses the shell
                self.assertEqual([k[:3] for k in s.of("kill_pane")], [("kill_pane", {"pane": "host_shell"}, b"")])


class KillResultTests(unittest.TestCase):
    def confirmed(self, **kw):
        m, s = make(**kw)
        feed(m, P + b"kk")
        rid = s.of("kill_pane")[0][3]
        return m, s, rid

    def test_a_pending_kill_is_not_repeated_and_the_result_is_shown(self):
        m, s, rid = self.confirmed()
        feed(m, P + b"k", 5.0)
        self.assertFalse(m.kill_confirm_open, "a second confirmation opened while the kill is pending")
        feed(m, b"k", 6.0)
        self.assertEqual(len(s.of("kill_pane")), 1)
        self.assertIn("강제 종료 중", m.notice)
        m.on_result({"id": rid, "ok": True, "pane": "host_shell", "killed": True, "survivors": [],
                     "input_owner": "user", "manager_owned": False, "exit_status": -1})
        self.assertIn("강제 종료됨", m.notice)
        self.assertIn("Enter", m.notice)
        feed(m, P + b"k", 7.0)
        self.assertTrue(m.kill_confirm_open, "the confirmation cannot be opened again after the result")

    def test_survivors_are_mentioned(self):
        m, _, rid = self.confirmed()
        m.on_result({"id": rid, "ok": True, "pane": "host_shell", "killed": True, "survivors": [4242, 4343]})
        self.assertIn("2", m.notice)

    def test_every_refusal_is_visible_with_its_reason(self):
        for reason in ("pane_exited", "kill_in_progress", "kill_failed", "backend_shutdown", "not_attached",
                       "pane_not_killable", "pane_unavailable"):
            with self.subTest(reason=reason):
                m, s, rid = self.confirmed()
                m.on_result({"id": rid, "ok": False, "reason": reason, "detail": f"detail-{reason}"})
                self.assertIn(reason, m.notice)
                self.assertIn(f"detail-{reason}", m.notice)
                self.assertIn("강제 종료", m.notice)
                feed(m, P + b"k", 9.0)
                self.assertTrue(m.kill_confirm_open, "a refused kill blocks the next confirmation")


class ExitedHostTests(unittest.TestCase):
    def test_notice_title_and_enter_restart_once(self):
        for code in (0, 137, -9):
            with self.subTest(code=code):
                m, s = make(focus="host_shell", host_alive=False, exit_status=code)
                self.assertEqual(m.restart_notice(PaneId.HOST_SHELL),
                                 f"host terminal 종료됨 (exit {code}) — Enter: 새 shell 시작")
                self.assertIn("종료됨", m.pane_title(PaneId.HOST_SHELL))
                feed(m, b"ls -la")
                feed(m, PASTE % b"echo pasted\r", 3.0)
                feed(m, "한글".encode(), 5.0)
                self.assertEqual(pane_frames(s), [], "typed bytes reached the exited host shell")
                self.assertEqual(s.of("restart_pane"), [])
                feed(m, b"\r", 7.0)
                feed(m, b"\r\r", 8.0)
                self.assertEqual([r[:3] for r in s.of("restart_pane")], [("restart_pane", {"pane": "host_shell"}, b"")])
                self.assertEqual(pane_frames(s), [])

    def test_restart_refusal_is_shown_and_enter_retries(self):
        m, s = make(focus="host_shell", host_alive=False, exit_status=0)
        feed(m, b"\r")
        rid = s.of("restart_pane")[0][3]
        m.on_result({"id": rid, "ok": False, "reason": "restart_failed", "detail": "out of ptys"})
        self.assertIn("restart_failed", m.notice)
        self.assertIn("out of ptys", m.notice)
        feed(m, b"\r", 3.0)
        self.assertEqual(len(s.of("restart_pane")), 2)

    def test_state_push_of_the_new_shell_revives_the_pane_and_input_flows(self):
        m, s = make(focus="host_shell", host_alive=False, exit_status=0)
        feed(m, b"\r")
        rid = s.of("restart_pane")[0][3]
        m.on_result({"id": rid, "ok": True, "pane": "host_shell", "restarted": True, "generation": 2,
                     "session_id": "22222222-2222-4222-8222-222222222222", "input_owner": "user"})
        snapshot = snap(focus="host_shell")
        snapshot["panes"]["host_shell"].update(generation=2, session_id="22222222-2222-4222-8222-222222222222")
        m.on_state(snapshot)
        self.assertIsNone(m.restart_notice(PaneId.HOST_SHELL))
        s.sent.clear()
        feed(m, b"echo hi\r", 3.0)
        self.assertEqual(s.payloads("input", "host_shell"), b"echo hi\r")
        self.assertEqual(s.of("restart_pane"), [])

    def test_kill_then_exited_state_then_enter_restarts(self):
        m, s = make(focus="manager_omp")
        feed(m, P + b"kk")
        rid = s.of("kill_pane")[0][3]
        m.on_result({"id": rid, "ok": True, "pane": "host_shell", "killed": True, "survivors": []})
        snapshot = snap(focus="manager_omp")
        snapshot["panes"]["host_shell"].update(alive=False, exit_status=-9, generation=1)
        m.on_state(snapshot)
        feed(m, b"\r", 3.0)  # Enter on the live manager pane is input for it, not a restart
        self.assertEqual(s.payloads("input", "manager_omp"), b"\r")
        self.assertEqual(s.of("restart_pane"), [])
        feed(m, P + b"3", 4.0)
        feed(m, b"\r", 5.0)
        self.assertEqual([r[:3] for r in s.of("restart_pane")], [("restart_pane", {"pane": "host_shell"}, b"")])


class MenuAndHelpTests(unittest.TestCase):
    def test_menu_has_the_kill_row_and_keeps_the_numbered_items(self):
        self.assertEqual([item[0] for item in MENU_ITEMS], ["[", "z", "t", "c", "h", "r", "m", "=", "?", "q"],
                         "the existing digit layout of the command menu changed")
        m, s = make()
        feed(m, P + b" ")
        lines = m.menu_lines()
        self.assertTrue(any("host terminal 강제 종료" in line for line in lines), lines)
        feed(m, b"\x0b", 2.0)
        self.assertFalse(m.menu_open)
        self.assertTrue(m.kill_confirm_open)
        self.assertEqual(s.sent, [])
        feed(m, b"\x1b", 3.0)
        feed(m, P + b" ", 4.0)
        feed(m, b"0", 5.0)  # the 10th item is still detach
        self.assertTrue(m.quit)
        self.assertEqual(s.of("kill_pane"), [])

    def test_help_mentions_the_kill_and_the_enter_restart(self):
        text = "\n".join(HELP_LINES)
        self.assertIn("강제 종료", text)
        self.assertIn("Ctrl-k", text)
        self.assertIn("새 shell", text)


class ProductPtyKillTests(unittest.TestCase):
    """The real product UI on a PTY: the modal is drawn, cancels, confirms, and Enter restarts the host shell."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="p27s-ui-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.server = ScriptedServer(snapshot=snap(owner="manager"))
        self.addCleanup(self.server.close)
        self.ui = UiPty(self.server.path, self.work, rows=30, cols=120)
        self.addCleanup(self.ui.close)
        self.assertTrue(self.server.attached.wait(10), "UI did not attach")
        self.assertTrue(self.ui.wait_text("manager", 10))

    def kinds(self):
        return [f.header.get("type") for f in self.server.frames]

    def test_modal_cancel_confirm_and_restart_through_the_real_ui(self):
        ui, server = self.ui, self.server
        ui.send(P + b"k")
        self.assertTrue(ui.wait_text(KILL_TEXT, 5), ui.screen_text())
        self.assertIn(WARNING, ui.screen_text())
        ui.send(b"x")
        self.assertTrue(ui.wait_for(lambda: KILL_TEXT not in ui.screen_text(), 5), "x did not cancel the modal")
        time.sleep(0.3)
        self.assertNotIn("kill_pane", self.kinds())
        self.assertEqual(server.payloads("input") + server.payloads("paste"), b"")
        ui.send(P + b"\x0b")
        self.assertTrue(ui.wait_text(KILL_TEXT, 5))
        ui.send("ㅏ".encode())
        self.assertTrue(server.wait(lambda: "kill_pane" in self.kinds(), 5), self.kinds())
        kill = [f for f in server.frames if f.header.get("type") == "kill_pane"]
        self.assertEqual([f.header.get("pane") for f in kill], ["host_shell"])
        self.assertEqual(server.payloads("input") + server.payloads("paste"), b"")
        exited = snap(owner="user")
        exited["panes"]["host_shell"].update(alive=False, exit_status=-9, generation=1)
        server.state(exited)
        self.assertTrue(ui.wait_text("host terminal 종료됨 (exit -9)", 5), ui.screen_text())
        ui.send(P + b"3")
        time.sleep(0.2)
        ui.send(b"ls\r")
        self.assertTrue(server.wait(lambda: "restart_pane" in self.kinds(), 5), self.kinds())
        time.sleep(0.3)
        restart = [f for f in server.frames if f.header.get("type") == "restart_pane"]
        self.assertEqual([f.header.get("pane") for f in restart], ["host_shell"])
        self.assertEqual(server.payloads("input", "host_shell"), b"", "typed bytes reached the exited host shell")
        ui.send(P + b"q")
        self.assertTrue(ui.ui_done(10), "the UI did not detach")
        self.assertEqual(ui.status(), 0)


if __name__ == "__main__":
    unittest.main()
