"""Independent C-D62 (1) UI checks (p27-restart-test-01): Enter restarts an exited manager/worker OMP pane.

Expectations were drafted from DECISIONS C-D62 (1) and the UI worker's required_behavior before the
implementation was read: an exited OMP pane shows a '종료됨' notice and title; Enter (CR or LF, alone or mixed in a
read, also right after a Hangul IME commit) sends exactly one ``restart_pane`` for that pane; any other byte, a paste,
wheel arrows, mouse reports and terminal-query replies never reach an exited pane; a pending restart is deduplicated;
refusals are visible; the notice ends once the new generation arrives; the host shell exit is unaffected. Once the new
OMP session streams, it is a live pane again (its terminal queries are answered, typed keys reach it).

Model tests use a fake sender. The PTY test runs the real product UI against a real ``Backend`` with a fake OMP that
echoes every raw byte it receives (no real OMP, no model, no credentials).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from support import FakeSender, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product.model import ProductModel  # noqa: E402

P = b"\x1d"
HANGUL = "한글".encode()
NOTICE = "OMP 종료됨 (exit 0) — Enter: 새 세션으로 다시 시작 · 이전 대화는 새 OMP에서 /resume"
SID_OLD, SID_NEW = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def make(*dead: str, generation: int = 1):
    sender = FakeSender()
    model = ProductModel(sender, 30, 120, clock=lambda: 1000.0)
    snap = snapshot(alive=tuple(p for p in ("manager_omp", "worker_omp", "host_shell") if p not in dead))
    for name, info in snap["panes"].items():
        info["generation"] = generation
        info["session_id"] = SID_OLD
        if name in dead:
            info["exit_status"] = 0
    model.apply_snapshot(snap)
    sender.sent.clear()
    return model, sender


def frame(pane: str, data: bytes, *, sid: str = SID_OLD, gen: int = 1, replay: bool = False) -> ui_v1.Frame:
    header = {"pane": pane, "session_id": sid, "generation": gen}
    if replay:
        header["replay"] = True
    return ui_v1.Frame(header, data)


def pane_bytes(sender: FakeSender) -> list:
    """Everything that would reach a pane's process: input and paste requests."""
    return [s for s in sender.sent if s[0] in ("input", "paste")]


class ExitedPaneEnterTests(unittest.TestCase):
    def test_notice_and_title_for_each_exited_omp_pane(self):
        for pane in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP):
            with self.subTest(pane=pane.value):
                model, _ = make(pane.value)
                self.assertEqual(NOTICE, model.restart_notice(pane))
                self.assertIn("종료됨", model.pane_title(pane))
                self.assertIn("Enter", model.pane_title(pane))
                self.assertTrue(model.pane_exited(pane))
                self.assertTrue(model.restart_notice_lines(pane, 40))

    def test_every_enter_form_sends_exactly_one_restart_and_no_pane_bytes(self):
        cases = {
            "cr": [b"\r"], "lf": [b"\n"], "crlf": [b"\r\n"], "mixed": [b"abc\rdef\n"],
            "ime_commit_and_enter_one_read": [HANGUL + b"\r"],
            "ime_commit_then_enter": [HANGUL, b"\r"],
            "ime_commit_split_utf8_then_enter": [HANGUL[:2], HANGUL[2:], b"\r"],
            "enter_then_more": [b"\r", b"\r", b"x\n", HANGUL + b"\r"],
        }
        for name, reads in cases.items():
            with self.subTest(case=name):
                model, sender = make("manager_omp")
                for data in reads:
                    model.handle_input(data)
                model.flush_input(now=5000.0)
                self.assertEqual([("restart_pane", {"pane": "manager_omp"}, b"")], sender.of("restart_pane"))
                self.assertEqual([], pane_bytes(sender))
                self.assertEqual("다시 시작 중…", model.restart_notice(PaneId.MANAGER_OMP))

    def test_worker_and_manager_restarts_are_independent(self):
        model, sender = make("manager_omp", "worker_omp")
        model.handle_input(b"\r")
        model.handle_input(P + b"2")
        model.handle_input(b"\r")
        model.handle_input(b"\r")
        self.assertEqual([("restart_pane", {"pane": "manager_omp"}, b""), ("restart_pane", {"pane": "worker_omp"}, b"")],
                         sender.of("restart_pane"))
        self.assertEqual([], pane_bytes(sender))

    def test_other_bytes_pastes_ctrl_d_and_hangul_alone_never_reach_the_exited_pane(self):
        model, sender = make("manager_omp")
        for data in (b"hello", HANGUL, b"\x1b[A\x1b[B", b"\x1bOA", b"\t", b"\x03", b"\x04", b"\x04",
                     b"\x7f", b"\x1b[200~pasted\r\n\x1b[201~", b"\x1b[200~" + HANGUL + b"\x1b[201~"):
            model.handle_input(data, now=1.0)
        model.flush_input(now=5000.0)
        self.assertEqual([], sender.sent)
        self.assertIn("Enter", model.notice)

    def test_wheel_mouse_and_terminal_queries_of_the_exited_pane_send_nothing(self):
        model, sender = make("manager_omp")
        view = model.panes[PaneId.MANAGER_OMP]
        view.modes.update({1049})  # alt screen: the wheel would become arrow keys
        model.handle_input(b"\x1b[<64;5;5M\x1b[<65;5;5M")
        view.modes.update({1000, 1006})  # the old OMP had asked for mouse reports
        model.handle_input(b"\x1b[<0;5;5M\x1b[<0;5;5m\x1b[<64;5;5M")
        model.on_display(frame("manager_omp", b"\x1b[6n\x1b[c\x1b[>c"))  # late queries of the OLD session
        model.enqueue_display(frame("manager_omp", b"\x1b[6n"))
        while model.has_backlog():
            model.feed_pending()
        self.assertEqual([], pane_bytes(sender))
        self.assertEqual([], sender.of("restart_pane"))

    def test_pending_restart_is_deduplicated_until_ok_and_new_generation(self):
        model, sender = make("manager_omp")
        model.handle_input(b"\r")
        rid = f"r{sender.count}"
        for _ in range(3):
            model.handle_input(b"\r")
        model.on_result({"id": rid, "ok": True, "pane": "manager_omp", "restarted": True, "generation": 2,
                         "session_id": SID_NEW})
        model.handle_input(b"\r\n")
        model.apply_snapshot(make("manager_omp")[0].state)  # a state push that still shows the old exited pane
        model.handle_input(b"\r")
        self.assertEqual(1, len(sender.of("restart_pane")))
        self.assertEqual([], pane_bytes(sender))

    def test_refusals_are_shown_and_enter_may_try_again(self):
        for reason, detail in (("not_attached", "attach first"), ("pane_alive", "running"),
                               ("restart_in_progress", "re-check running"), ("restart_failed", "fork refused"),
                               ("backend_shutdown", "stopping")):
            with self.subTest(reason=reason):
                model, sender = make("manager_omp")
                model.handle_input(b"\r")
                model.on_result({"id": f"r{sender.count}", "ok": False, "reason": reason, "detail": detail})
                footer = model.footer()
                self.assertIn(reason, footer)
                self.assertIn(detail, footer)
                self.assertEqual(NOTICE, model.restart_notice(PaneId.MANAGER_OMP))
                model.handle_input(b"\r")
                self.assertEqual(2, len(sender.of("restart_pane")))

    def test_new_generation_snapshot_clears_notice_screen_and_input_flows(self):
        model, sender = make("manager_omp")
        model.on_display(frame("manager_omp", b"OLD-SCREEN"))
        model.handle_input(b"\r")
        model.on_result({"id": f"r{sender.count}", "ok": True, "pane": "manager_omp", "generation": 2})
        snap = make()[0].state
        snap["panes"]["manager_omp"].update(generation=2, session_id=SID_NEW)
        model.apply_snapshot(snap)
        model.on_display(frame("manager_omp", b"NEW-OMP", sid=SID_NEW, gen=2))
        self.assertIsNone(model.restart_notice(PaneId.MANAGER_OMP))
        self.assertNotIn("종료됨", model.pane_title(PaneId.MANAGER_OMP))
        text = "\n".join(model.panes[PaneId.MANAGER_OMP].screen.display)
        self.assertIn("NEW-OMP", text)
        self.assertNotIn("OLD-SCREEN", text)
        sender.sent.clear()
        model.handle_input(b"typed\r")
        self.assertEqual([("input", {"pane": "manager_omp"}, b"typed\r")], pane_bytes(sender))
        self.assertEqual([], sender.of("restart_pane"))
        model.on_display(frame("manager_omp", b"\x1b[6n", sid=SID_NEW, gen=2))
        self.assertTrue(any(s[1] == {"pane": "manager_omp"} and s[2].startswith(b"\x1b[") for s in pane_bytes(sender)),
                        "a live restarted pane answers terminal queries")

    def test_queries_of_the_new_session_are_answered_before_the_next_state_push(self):
        """The restart result carries no snapshot: the new OMP's first output (and its start-up terminal queries)
        can arrive while the last state still says 'exited'. The new session is live: its queries are answered."""
        model, sender = make("manager_omp")
        model.handle_input(b"\r")
        model.on_result({"id": f"r{sender.count}", "ok": True, "pane": "manager_omp", "restarted": True,
                         "generation": 2, "session_id": SID_NEW})
        sender.sent.clear()
        model.on_display(frame("manager_omp", b"\x1b[6n", sid=SID_NEW, gen=2))
        self.assertEqual(1, len(pane_bytes(sender)), "the new OMP's DSR query was not answered")

    def test_keys_typed_on_the_new_session_before_the_next_state_push_are_not_lost(self):
        model, sender = make("manager_omp")
        model.handle_input(b"\r")
        model.on_result({"id": f"r{sender.count}", "ok": True, "pane": "manager_omp", "restarted": True,
                         "generation": 2, "session_id": SID_NEW})
        model.on_display(frame("manager_omp", b"fresh OMP prompt", sid=SID_NEW, gen=2))
        sender.sent.clear()
        model.handle_input(b"x")
        self.assertEqual([("input", {"pane": "manager_omp"}, b"x")], pane_bytes(sender),
                         "a key typed on the visibly restarted OMP was dropped")

    def test_live_panes_are_unaffected_and_an_exited_host_shell_follows_c_d63(self):
        # Adapted for C-D63: an exited host shell is no longer left alone; like an exited OMP pane it shows the
        # host notice and Enter sends one restart_pane for host_shell. The protected invariants stay: live panes
        # (the OMP next to it, and a live host shell) never get a restart and receive their typed bytes unchanged.
        model, sender = make("host_shell")
        self.assertIn("host terminal 종료됨", model.restart_notice(PaneId.HOST_SHELL))
        self.assertIsNone(model.restart_notice(PaneId.MANAGER_OMP))
        model.handle_input(b"\r")
        self.assertEqual([("input", {"pane": "manager_omp"}, b"\r")], pane_bytes(sender))
        self.assertEqual([], sender.of("restart_pane"))
        model.handle_input(P + b"3")
        sender.sent.clear()
        model.handle_input(b"exit\r")
        self.assertEqual([("restart_pane", {"pane": "host_shell"}, b"")], sender.of("restart_pane"))
        self.assertEqual([], pane_bytes(sender), "typed bytes reached the exited host shell")
        live, live_sender = make()
        live.handle_input(P + b"3")
        live_sender.sent.clear()
        live.handle_input(b"exit\r")
        self.assertIsNone(live.restart_notice(PaneId.HOST_SHELL))
        self.assertEqual([], live_sender.of("restart_pane"))
        self.assertEqual([("input", {"pane": "host_shell"}, b"exit\r")], pane_bytes(live_sender))


FAKE_OMP = r'''#!{python}
import json, os, socket, sys, tty, uuid
argv = sys.argv[1:]
if argv[:2] == ["config", "get"]:
    print("[]")
    sys.exit(0)
role = os.environ["WORKBENCH_G3_ROLE"]
with open(os.environ["P27Q_RECORD"], "a") as stream:
    stream.write(json.dumps({{"pid": os.getpid(), "role": role, "argv": argv}}) + "\n")
sock = socket.socket(socket.AF_UNIX)
sock.connect(os.environ["WORKBENCH_G3_BRIDGE_SOCKET"])
hello = {{"kind": "hello", "protocolVersion": 1, "token": os.environ["WORKBENCH_G3_TOKEN"], "role": role,
          "ompSessionId": str(uuid.uuid4()), "generation": int(os.environ["WORKBENCH_G3_GENERATION"]),
          "pid": os.getpid()}}
sock.sendall((json.dumps(hello) + "\n").encode())
json.loads(sock.makefile("rb").readline())
tty.setraw(0)
os.write(1, ("FAKE-%s pid=%d ready\r\n" % (role, os.getpid())).encode())
got = open(os.environ["P27Q_RECORD"] + ".bytes", "ab", buffering=0)
while True:
    data = os.read(0, 4096)
    if not data:
        break
    got.write(json.dumps({{"pid": os.getpid(), "data": data.hex()}}).encode() + b"\n")
    if b"QUIT!" in data:
        break
    os.write(1, ("[%s got %r]\r\n" % (role, data)).encode())
'''


class RestartProductPtyTests(unittest.TestCase):
    """The real product UI + a real Backend: exit, stray input, Hangul IME + Enter, one restart, input flows."""

    def setUp(self):
        from test_product_pty import UiProcess
        from workbench.backend.launcher import LaunchPlan
        from workbench.backend.paths import DataLayout, ensure_private_dir
        from workbench.backend.service import Backend
        from workbench.terminal.shell_g2.prototype import ShellChoice

        self._dir = tempfile.TemporaryDirectory(prefix="p27qu-", dir="/tmp")
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        project, home = root / "p", root / "h"
        project.mkdir()
        home.mkdir()
        fake = root / "omp"
        fake.write_text(FAKE_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        self.record = root / "panes.jsonl"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "P27Q_RECORD": str(self.record),
               "LANG": "C.UTF-8"}
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), str(fake), "omp/18.4.4", "/x/bridge.ts")
        patcher = mock.patch("workbench.backend.service.check_isolation",
                             lambda *a, role, **k: {"role": role, "state": "ok", "ok": True, "leaks": [],
                                                    "warnings": [], "error": None})
        patcher.start()
        self.addCleanup(patcher.stop)
        layout = DataLayout(root / "d")
        ensure_private_dir(layout.root)
        self.backend = Backend(layout, plan, project_dir=str(project), environment=env)
        self.stop = threading.Event()
        self.ticker: threading.Thread | None = None
        self.addCleanup(self._close_backend)
        self.backend._open()
        deadline = time.monotonic() + 15
        while self.backend.phase != "ready" and time.monotonic() < deadline:
            self.backend._tick(0.02)
        self.assertEqual("ready", self.backend.phase)
        self.ticker = threading.Thread(target=self._tick_loop, daemon=True)
        self.ticker.start()
        self.ui = UiProcess(layout.ui_socket)
        self.addCleanup(self.ui.close)

    def _tick_loop(self):
        while not self.stop.is_set():
            self.backend._tick(0.02)

    def _close_backend(self):
        self.stop.set()
        if self.ticker is not None:
            self.ticker.join(5)
        pids = [e["pid"] for e in self.invocations()]
        self.backend._close()
        for pid in pids:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0]
            except OSError:
                continue
            self.assertIn(state, "ZX", f"fake OMP {pid} survived the backend close")

    def invocations(self):
        return [json.loads(x) for x in self.record.read_text().splitlines() if x.strip()] if self.record.exists() else []

    def received(self, pid):
        path = Path(str(self.record) + ".bytes")
        lines = path.read_text().splitlines() if path.exists() else []
        return b"".join(bytes.fromhex(json.loads(x)["data"]) for x in lines if json.loads(x)["pid"] == pid)

    def test_exit_stray_input_then_hangul_commit_plus_enter_restarts_once_and_input_flows(self):
        ui = self.ui
        old = next(e for e in self.invocations() if e["role"] == "manager")
        self.assertTrue(ui.until(lambda: f"FAKE-manager pid={old['pid']} ready" in ui.text()), ui.text())
        ui.send(b"QUIT!")
        self.assertTrue(ui.until(lambda: "OMP 종료됨" in ui.text() and "Enter" in ui.text(), 8), ui.text())
        self.assertIn("종료됨", ui.text().splitlines()[0] + "".join(ui.text().splitlines()[:3]))
        for data in (b"stray", HANGUL, b"\x1b[A", b"\x1b[<64;5;5M", b"\x1b[200~paste\r\x1b[201~", b"\x04", b"\x04"):
            ui.send(data)
            ui.until(lambda: False, 0.1)
        self.assertEqual(2, len(self.invocations()), "a stray byte restarted the pane")
        ui.send(HANGUL + b"\r")  # an IME commit and Enter in one read
        self.assertTrue(ui.until(lambda: len(self.invocations()) == 3, 8), self.invocations())
        new = self.invocations()[-1]
        self.assertEqual("manager", new["role"])
        self.assertTrue(ui.until(lambda: f"FAKE-manager pid={new['pid']} ready" in ui.text()
                                 and "OMP 종료됨" not in ui.text() and "다시 시작 중" not in ui.text(), 8), ui.text())
        self.assertNotIn(f"pid={old['pid']}", ui.text(), "the old screen was not replaced")
        ui.send(b"\r")  # a live pane again: Enter is input, not a restart
        ui.send(b"after")
        self.assertTrue(ui.until(lambda: b"after" in self.received(new["pid"]), 8), ui.text())
        self.assertEqual(3, len(self.invocations()), "Enter on the live pane started another OMP")
        got_new = self.received(new["pid"])
        for stray in (b"stray", HANGUL, b"paste", b"\x04"):
            self.assertNotIn(stray, got_new)
        self.assertEqual(b"stray" in self.received(old["pid"]), False)
        worker = next(e for e in self.invocations() if e["role"] == "worker")
        self.assertEqual(b"", self.received(worker["pid"]), "the worker received bytes")
        ui.send(P + b"q")
        self.assertEqual(0, ui.wait_exit())


if __name__ == "__main__":
    unittest.main()
