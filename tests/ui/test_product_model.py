import json
import re
import shutil
import sys
import tempfile
import unittest
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product.input import (InputParser, Command, Mouse, PageScroll, Paste, PasteRejected,  # noqa: E402
                                        Passthrough, PREFIX)
from workbench.ui.product.layout import LAYOUT_FILE, Layout, load_layout, save_layout  # noqa: E402
from workbench.ui.product.model import (CATCHUP_BACKLOG_BYTES, CTRL_D_CONFIRM_SECONDS, CTRL_D_NOTICE, HELP_LINES, MIN_COLS, MIN_ROWS, PANES,  # noqa: E402
                                       ProductModel, _safe_tail, pane_boxes, pane_inner_sizes)  # noqa: E402

P = bytes([PREFIX])


class ParserTests(unittest.TestCase):
    def test_original_keys_pass_unchanged(self):
        parser = InputParser()
        keys = b"/help\r\x03\t\x1b[A\x1bOP\xed\x95\x9c\xea\xb8\x80"
        events = parser.feed(keys)
        self.assertEqual(b"".join(e.data for e in events if isinstance(e, Passthrough)), keys)

    def test_lone_escape_released_after_hold(self):
        parser = InputParser()
        self.assertEqual([], parser.feed(b"\x1b", now=0.0))
        self.assertEqual([], parser.flush(now=0.01))
        self.assertEqual([Passthrough(b"\x1b")], parser.flush(now=0.2))

    def test_prefix_command_and_literal(self):
        parser = InputParser()
        self.assertEqual([Command("1")], parser.feed(P + b"1"))
        self.assertEqual([Passthrough(P)], parser.feed(P + P))
        self.assertEqual([], parser.feed(P))
        self.assertTrue(parser.prefix_active)
        self.assertEqual([Command("d")], parser.feed(b"d"))

    def test_paste_whole_even_when_split(self):
        parser = InputParser()
        body = "안녕\n두 번째 줄".encode()
        out = []
        for chunk in (b"\x1b[20", b"0~" + body[:4], body[4:] + b"\x1b[2", b"01~x"):
            out += parser.feed(chunk)
        self.assertEqual([Paste(b"\x1b[200~" + body + b"\x1b[201~"), Passthrough(b"x")], out)

    def test_paste_containing_prefix_is_not_a_command(self):
        events = InputParser().feed(b"\x1b[200~a" + P + b"db\x1b[201~")
        self.assertEqual([Paste(b"\x1b[200~a" + P + b"db\x1b[201~")], events)

    def test_oversized_paste_rejected_whole(self):
        parser = InputParser()
        events = parser.feed(b"\x1b[200~" + b"a" * (2 * 1024 * 1024 + 10))
        events += parser.feed(b"\x1b[201~")
        self.assertEqual(1, len(events))
        self.assertIsInstance(events[0], PasteRejected)


def make(rows=30, cols=120):
    sender = FakeSender()
    model = ProductModel(sender, rows, cols, clock=lambda: 1000.0)
    model.apply_snapshot(snapshot())
    return model, sender


def text(model, pane):
    screen = model.panes[pane].screen
    return "\n".join(line.rstrip() for line in screen.display)


def display(pane, data, replay=False, gen=1):
    header = {"pane": pane, "session_id": "s", "generation": gen}
    if replay:
        header["replay"] = True
    return ui_v1.Frame(header, data)


class ModelTests(unittest.TestCase):
    def test_three_panes_render_and_status_separated(self):
        model, _ = make()
        for pane, word in (("manager_omp", "MGR"), ("worker_omp", "WRK"), ("host_shell", "$ ")):
            model.on_display(display(pane, word.encode()))
        self.assertIn("MGR", text(model, PaneId.MANAGER_OMP))
        self.assertIn("WRK", text(model, PaneId.WORKER_OMP))
        line1, line2 = model.status_lines()
        self.assertIn("focus: MANAGER OMP", line1)
        self.assertIn("host 입력 owner: user", line1)
        self.assertIn("shell mode: user_control", line1)
        self.assertIn("자동화: idle", line1)
        self.assertIn("backend: ready", line2)
        self.assertIn("마지막 확인", line2)
        self.assertIn("worker=down", line2)
        self.assertIn("FOCUS", model.pane_title(PaneId.MANAGER_OMP))
        self.assertNotIn("FOCUS", model.pane_title(PaneId.WORKER_OMP))
        self.assertIn("owner=user", model.pane_title(PaneId.HOST_SHELL))

    def test_exited_pane_and_unknown_owner(self):
        model, _ = make()
        model.apply_snapshot(snapshot(alive=("manager_omp",)))
        self.assertEqual("exited(1)", model.pane_status(PaneId.WORKER_OMP))
        model.state["panes"]["host_shell"].pop("input_owner")
        self.assertEqual("unknown", model.input_owner())

    def test_focus_switch_sends_focus_and_keeps_owner(self):
        model, sender = make()
        model.handle_input(P + b"3")
        self.assertEqual(PaneId.HOST_SHELL, model.focus)
        self.assertEqual([{"pane": "host_shell"}], [f for _, f, _ in sender.of("focus")])
        self.assertEqual("user", model.input_owner())
        self.assertEqual([], sender.of("takeover_request") + sender.of("takeover_confirm") + sender.of("handoff"))
        model.handle_input(P + b"\t")
        self.assertEqual(PaneId.MANAGER_OMP, model.focus)

    def test_keys_go_to_focused_pane_only_and_unchanged(self):
        model, sender = make()
        model.handle_input(b"/model\r")
        model.handle_input(P + b"2")
        model.handle_input(b"\x1b[A")
        inputs = sender.of("input")
        self.assertEqual(("input", {"pane": "manager_omp"}, b"/model\r"), inputs[0])
        self.assertEqual(("input", {"pane": "worker_omp"}, b"\x1b[A"), inputs[1])

    def test_literal_prefix_sent_to_focused_pane(self):
        model, sender = make()
        model.handle_input(P + P)
        self.assertEqual([("input", {"pane": "manager_omp"}, P)], sender.of("input"))

    def test_takeover_and_handoff_use_existing_types(self):
        model, sender = make()
        model.handle_input(P + b"t" + P + b"c" + P + b"h")
        self.assertEqual(["takeover_request", "takeover_confirm", "handoff"], [s[0] for s in sender.sent])

    def test_takeover_result_updates_owner_without_focus_change(self):
        model, sender = make()
        model.handle_input(P + b"t")
        rid = "r1"
        model.on_result({"id": rid, "ok": True, "shell": {"input_owner": "manager", "parent_mode": "manager_control"}})
        self.assertEqual("manager", model.input_owner())
        self.assertEqual(PaneId.MANAGER_OMP, model.focus)

    def test_paste_sent_whole_as_one_frame(self):
        model, sender = make()
        body = b"\x1b[200~" + "한글\n여러 줄\n".encode() + b"\x1b[201~"
        model.handle_input(body[:5])
        model.handle_input(body[5:12])
        model.handle_input(body[12:])
        pastes = sender.of("paste")
        self.assertEqual([("paste", {"pane": "manager_omp"}, body)], pastes)
        self.assertEqual([], sender.of("input"))

    def test_rejections_show_reason(self):
        model, sender = make()
        for reason in ("paste_too_large", "queue_full", "input_owner_manager", "input_target_unknown"):
            model.handle_input(b"\x1b[200~x\x1b[201~")
            rid = f"r{sender.count}"
            model.on_result({"id": rid, "ok": False, "reason": reason, "detail": "d"})
            self.assertIn(reason, model.notice)
            self.assertIn("붙여넣기", model.notice)

    def test_refused_typing_is_visible_but_query_reply_refusal_is_quiet(self):
        model, sender = make()
        model.handle_input(b"a")
        model.on_result({"id": "r1", "ok": False, "reason": "input_owner_manager", "detail": ""})
        self.assertIn("input_owner_manager", model.notice)
        model.notice = ""
        model.on_display(display("manager_omp", b"\x1b[6n"))
        self.assertEqual(("input", {"pane": "manager_omp"}, b"\x1b[1;1R"), sender.of("input")[-1])
        model.on_result({"id": f"r{sender.count}", "ok": False, "reason": "input_owner_manager"})
        self.assertEqual("", model.notice)

    def test_replay_does_not_answer_queries_again(self):
        model, sender = make()
        model.on_display(display("manager_omp", b"\x1b[6nhello", replay=True))
        self.assertEqual([], sender.of("input"))
        self.assertIn("hello", text(model, PaneId.MANAGER_OMP))

    def test_generation_change_resets_screen(self):
        model, _ = make()
        model.on_display(display("worker_omp", b"old"))
        model.on_display(display("worker_omp", b"new", gen=2))
        self.assertNotIn("old", text(model, PaneId.WORKER_OMP))

    def test_resize_sends_per_pane_actual_sizes(self):
        model, sender = make(30, 120)
        model.resize(40, 150)
        sent = {f["pane"]: (f["rows"], f["cols"]) for _, f, _ in sender.of("resize")}
        expected = {p.value: s for p, s in pane_inner_sizes(40, 150).items()}
        self.assertEqual(expected, sent)
        self.assertEqual(model.panes[PaneId.HOST_SHELL].screen.columns, expected["host_shell"][1])
        sender.sent.clear()
        model.resize(40, 150)
        self.assertEqual([], sender.of("resize"))

    def test_after_attach_sizes_and_redraw_nudge(self):
        model, sender = make(30, 120)
        model.after_attach()
        resizes = sender.of("resize")
        self.assertEqual(9, len(resizes))
        rows = [f["rows"] for _, f, _ in resizes[3:6]]
        self.assertEqual(model.sizes[PaneId.MANAGER_OMP][0] - 1, rows[0])

    def test_detach_command_and_help(self):
        model, sender = make()
        model.handle_input(P + b"?")
        self.assertTrue(model.help_open)
        model.handle_input(b"x")
        self.assertFalse(model.help_open)
        self.assertEqual([], sender.of("input"))
        model.handle_input(P + b"q")
        self.assertTrue(model.quit)
        self.assertEqual([], [s for s in sender.sent if s[0].startswith("shutdown")])

    def test_closing_sets_reason(self):
        model, _ = make()
        model.on_closing({"reason": "backend_shutdown"})
        self.assertEqual("backend_shutdown", model.closed_reason)
        self.assertIn("backend_shutdown", model.notice)


class IsolationHeaderTests(unittest.TestCase):
    """Review P3-R5 (UI part): the backend's omp_isolation warning is visible in the header when not ok."""

    def test_warning_shown_only_after_a_failed_or_leaking_check(self):
        model, _ = make()
        base = snapshot()
        for isolation, shown in ((None, False), ({"state": "pending", "checked": False, "warning": None}, False),
                                 ({"state": "ok", "checked": True, "ok": True, "warning": None}, False),
                                 ({"state": "failed", "checked": True, "ok": False,
                                   "warning": "OMP isolation check failed: manager: timeout"}, True),
                                 ({"state": "leak", "checked": True, "ok": False,
                                   "warning": "OMP isolation leak (ambient configuration loaded): worker:x"}, True)):
            with self.subTest(isolation=isolation):
                snap = dict(base)
                if isolation is not None:
                    snap["omp_isolation"] = isolation
                model.on_state(snap)
                line2 = model.status_lines()[1]
                self.assertEqual(shown, "OMP 격리" in line2, line2)
                if shown:
                    self.assertTrue(line2.startswith("경고 OMP 격리 " + isolation["state"]), line2)
                    self.assertIn(isolation["warning"], line2)
                    self.assertIn("backend: ready", line2)
                self.assertEqual(shown, model.isolation_warning() is not None)


class P27FixTests(unittest.TestCase):
    S, E = b"\x1b[200~", b"\x1b[201~"

    def test_split_decrst_2004_strips_markers_and_reenable_restores(self):
        model, sender = make()
        model.handle_input(P + b"3")
        model.on_display(display("host_shell", b"\x1b[?200"))
        model.on_display(display("host_shell", b"4h\x1b[?2004l"))
        model.handle_input(self.S + b"ab" + self.E)
        self.assertEqual(b"ab", sender.of("paste")[-1][2])
        model.on_display(display("host_shell", b"\x1b[?1;2004h"))
        model.handle_input(self.S + b"cd" + self.E)
        self.assertEqual(self.S + b"cd" + self.E, sender.of("paste")[-1][2])

    def test_sh_host_shell_strips_markers_unless_2004h_observed(self):
        model, sender = make()
        model.host_info()["shell"]["kind"] = "sh"
        model.handle_input(P + b"3")
        model.handle_input(self.S + b"ab" + self.E)
        self.assertEqual(b"ab", sender.of("paste")[-1][2])
        model.on_display(display("host_shell", b"\x1b[?2004h"))
        model.handle_input(self.S + b"cd" + self.E)
        self.assertEqual(self.S + b"cd" + self.E, sender.of("paste")[-1][2])
        model.on_display(display("host_shell", b"new", gen=2))  # new generation: unobserved again
        model.handle_input(self.S + b"ef" + self.E)
        self.assertEqual(b"ef", sender.of("paste")[-1][2])

    def test_bash_and_omp_panes_keep_markers_when_unobserved(self):
        model, sender = make()
        for key in (b"1", b"3"):
            model.handle_input(P + key)
            model.handle_input(self.S + b"x" + self.E)
            self.assertEqual(self.S + b"x" + self.E, sender.of("paste")[-1][2])

    def test_prefix_then_split_paste_is_framed(self):
        model, sender = make()
        model.handle_input(P + b"\x1b[20", now=0)
        model.handle_input(b"0~xy\x1b[201~", now=0.01)
        self.assertEqual([self.S + b"xy" + self.E], [x[2] for x in sender.of("paste")])
        self.assertEqual([], sender.of("input"))

    def test_prefix_then_multibyte_key_split_swallows_all(self):
        model, sender = make()
        model.handle_input(P + b"\xed")
        model.handle_input(b"\x95\x9cz")
        self.assertEqual(b"z", b"".join(x[2] for x in sender.of("input")))

    def test_help_open_paste_is_delivered_and_closes_help(self):
        model, sender = make()
        model.handle_input(P + b"?")
        model.handle_input(self.S + b"q" + self.E)
        self.assertFalse(model.help_open)
        self.assertEqual(1, len(sender.of("paste")))

    def test_refusal_notice_includes_held_reasons(self):
        model, sender = make()
        model.handle_input(P + b"h")
        rid = f"r{sender.sent.index(sender.of('handoff')[-1]) + 1}"
        model.on_result(ui_v1.result(rid, False, reason=ui_v1.Reason.HANDOFF_HELD, detail="not clean",
                                     shell={"held_reasons": ["unsubmitted_or_unconsumed_input"]}))
        self.assertIn("handoff_held", model.footer())
        self.assertIn("unsubmitted_or_unconsumed_input", model.footer())

    def test_job_held_refusal_shows_how_to_clear_it(self):
        for reasons in (["manual_jobs"], ["unknown_or_manual_residue"], ["unsubmitted_or_unconsumed_input", "manual_jobs"]):
            with self.subTest(reasons=reasons):
                model, sender = make()
                model.handle_input(P + b"h")
                rid = f"r{sender.sent.index(sender.of('handoff')[-1]) + 1}"
                model.on_result(ui_v1.result(rid, False, reason=ui_v1.Reason.HANDOFF_HELD, detail="jobs",
                                             shell={"held_reasons": reasons}))
                footer = model.footer()
                # p27-cd70-ui-01: job reasons are shown in plain Korean (not as raw codes); the raw-code cases keep theirs
                self.assertIn("사용자 job" if "manual_jobs" in reasons else "상태를 확인할 수 없음", footer)
                for needle in ("인수", "wb-handoff"):
                    self.assertIn(needle, footer)
                # the full recovery order (C-D58): take over, clear jobs, wb-handoff, give back, take over again
                self.assertTrue(self._in_order(footer, ["prefix t,c", "jobs", "wb-handoff", "prefix h", "prefix t,c"]), footer)

    @staticmethod
    def _in_order(text, needles):
        pos = 0
        for n in needles:
            pos = text.find(n, pos)
            if pos < 0:
                return False
            pos += len(n)
        return True

    def test_other_held_reasons_get_no_job_hint(self):
        model, sender = make()
        model.handle_input(P + b"h")
        rid = f"r{sender.sent.index(sender.of('handoff')[-1]) + 1}"
        model.on_result(ui_v1.result(rid, False, reason=ui_v1.Reason.HANDOFF_HELD, detail="typing",
                                     shell={"held_reasons": ["unsubmitted_or_unconsumed_input"]}))
        self.assertNotIn("wb-handoff", model.footer())

    def test_refusal_uses_state_held_reasons(self):
        model, sender = make()
        model.host_info()["shell"] = {"held_reasons": ["manual_residue"]}
        model.handle_input(P + b"c")
        rid = f"r{sender.sent.index(sender.of('takeover_confirm')[-1]) + 1}"
        model.on_result(ui_v1.result(rid, False, reason=ui_v1.Reason.TAKEOVER_HELD))
        self.assertIn("manual_residue", model.footer())


class PrefixSequenceTests(unittest.TestCase):
    def test_prefix_then_escape_sequence_is_consumed_whole(self):
        for seq in (b"\x1b[A", b"\x1b[15~", b"\x1bOP", b"\x1bx", b"\x1b[1;5C"):
            parser = InputParser()
            events = parser.feed(P + seq + b"Z")
            self.assertEqual([Command(seq.decode()), Passthrough(b"Z")], events, seq)

    def test_prefix_then_split_sequence_waits_then_consumes(self):
        parser = InputParser()
        self.assertEqual([], parser.feed(P + b"\x1b[1", now=0.0))
        self.assertEqual([Command("\x1b[15~")], parser.feed(b"5~", now=0.01))

    def test_prefix_then_lone_escape_is_one_unknown_command(self):
        parser = InputParser()
        self.assertEqual([], parser.feed(P + b"\x1b", now=0.0))
        self.assertEqual([Command("\x1b")], parser.flush(now=0.2))

    def test_split_paste_introducer_is_not_released_early(self):
        parser = InputParser()
        self.assertEqual([], parser.feed(b"\x1b[20", now=0.0))
        self.assertEqual([], parser.flush(now=0.2))  # > 50 ms but a known paste introducer prefix
        events = parser.feed(b"0~hi\nthere\x1b[201~", now=0.3)
        self.assertEqual([Paste(b"\x1b[200~hi\nthere\x1b[201~")], events)

    def test_stale_partial_introducer_is_eventually_released(self):
        parser = InputParser()
        parser.feed(b"\x1b[20", now=0.0)
        self.assertEqual([Passthrough(b"\x1b[20")], parser.flush(now=1.0))


class CatchUpTests(unittest.TestCase):
    def display(self, pane, data, replay=False, session="s", seq=1):
        header = {"type": "display", "pane": pane.value, "session_id": session, "generation": 1, "sequence": seq}
        if replay:
            header["replay"] = True
        return ui_v1.Frame(header, data)

    def test_backlog_is_fed_in_bounded_slices(self):
        model, _ = make()
        line = b"row\r\n" * 100
        for i in range(20):
            model.enqueue_display(self.display(PaneId.HOST_SHELL, line, seq=i))
        model.enqueue_display(self.display(PaneId.MANAGER_OMP, b"MGR"))
        fed_before = model.panes[PaneId.HOST_SHELL].backlog_bytes
        self.assertTrue(model.feed_pending(max_bytes=1000, max_seconds=1.0))
        self.assertLess(model.panes[PaneId.HOST_SHELL].backlog_bytes, fed_before)
        self.assertGreater(model.panes[PaneId.HOST_SHELL].backlog_bytes, 0)
        self.assertEqual("MGR", model.panes[PaneId.MANAGER_OMP].screen.display[0].strip())  # fair across panes
        while model.feed_pending():
            pass
        self.assertEqual(0, model.panes[PaneId.HOST_SHELL].backlog_bytes)

    def test_feed_slice_time_budget_holds_for_slow_pyte_content(self):
        """`yes` output is pyte's slowest case (~0.1 MB/s): one feed slice must still end near its time budget."""
        model, _ = make()
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"y\r\n" * 21845))  # 64 KiB, below catch-up
        started = time.perf_counter()
        model.feed_pending(max_seconds=0.02)
        self.assertLess(time.perf_counter() - started, 0.08)
        self.assertGreater(model.panes[PaneId.HOST_SHELL].backlog_bytes, 0)

    def test_flush_input_reports_whether_anything_changed(self):
        model, _ = make()
        model.handle_input(b"\x1d?", now=0)
        self.assertTrue(model.help_open)
        model.handle_input(b"\x1b", now=1)
        self.assertTrue(model.help_open)  # lone Esc is held briefly
        self.assertTrue(model.flush_input(now=2))  # released: closes help -> caller must redraw
        self.assertFalse(model.help_open)
        self.assertFalse(model.flush_input(now=3))

    def test_large_backlog_catches_up_to_tail_with_indicator_and_redraw_nudge(self):
        model, sender = make()
        chunk = b"".join(b"line %06d\r\n" % i for i in range(2000))  # ~24 KiB
        for i in range(30):  # > 256 KiB unfed
            model.enqueue_display(self.display(PaneId.HOST_SHELL, chunk, seq=i))
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"THE-LATEST-TAIL\r\n", seq=99))
        before = len(sender.of("resize"))
        while model.feed_pending():
            pass
        view = model.panes[PaneId.HOST_SHELL]
        self.assertGreater(view.skipped_bytes, 0)
        self.assertIn("출력 따라잡음", model.notice)
        self.assertIn("bytes 건너뜀", model.notice)
        self.assertTrue(model.catching_up(PaneId.HOST_SHELL))
        self.assertIn("따라잡음", model.pane_title(PaneId.HOST_SHELL))
        shown = "\n".join(view.screen.display)
        self.assertIn("THE-LATEST-TAIL", shown)
        self.assertEqual(2, len(sender.of("resize")) - before)  # rows-1 then rows for the pane
        self.assertTrue(all(f["pane"] == "host_shell" for _, f, _ in sender.of("resize")[before:]))

    def test_catch_up_keeps_bracketed_paste_state_from_skipped_output(self):
        model, _ = make()
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"\x1b[?2004l", seq=1))
        for i in range(30):
            model.enqueue_display(self.display(PaneId.HOST_SHELL, b"x" * 20000, seq=i + 2))
        while model.feed_pending():
            pass
        self.assertIs(False, model.panes[PaneId.HOST_SHELL].bracketed)

    def test_catch_up_does_not_answer_stale_queries(self):
        model, sender = make()
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"\x1b[6n", seq=1))
        for i in range(30):
            model.enqueue_display(self.display(PaneId.HOST_SHELL, b"y" * 20000, seq=i + 2))
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"\x1b[6n", seq=99))  # in the kept tail (replay=True)
        while model.feed_pending():
            pass
        self.assertEqual([], sender.of("input"))

    def test_replay_after_result_is_fed_completely_and_matches_synchronous_feed(self):
        def replay_bytes(n):
            app = b"\x1b[?1049h\x1b[2J\x1b[H\x1b[31mFULLSCREEN-%d\x1b[0m" % n
            body = b"".join(b"\x1b[%d;1Hrow %d %s" % (i % 20 + 1, i, b"z" * 40) for i in range(4000))
            data = app + body
            data += b"\x1b[3;3Hfinal-%d" % n
            return data + b"q" * max(0, 288 * 1024 - len(data))
        model, sender = make()
        sync, _ = make()
        for n, pane in enumerate(PANES):
            data = replay_bytes(n)
            self.assertGreater(len(data), CATCHUP_BACKLOG_BYTES - 1)
            for off in range(0, len(data), 60000):  # backend sends the replay in several frames
                frame = self.display(pane, data[off:off + 60000], replay=True, seq=off)
                model.enqueue_display(frame)
                sync.on_display(frame)
        while model.feed_pending():
            pass
        for pane in PANES:
            view = model.panes[pane]
            self.assertEqual(0, view.skipped_bytes)
            self.assertFalse(model.catching_up(pane))
            self.assertEqual(sync.panes[pane].screen.display, view.screen.display)
            self.assertEqual(sync.panes[pane].screen.cursor.y, view.screen.cursor.y)
        self.assertEqual("", model.notice)
        self.assertEqual([], sender.of("input"))

    def test_live_flood_after_replay_keeps_replay_and_only_catches_up_live(self):
        model, _ = make()
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"REPLAYED-LINE\r\n" * 30000, replay=True))
        for i in range(30):
            model.enqueue_display(self.display(PaneId.HOST_SHELL, b"live %d\r\n" % i * 2000, seq=i + 2))
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"LIVE-END\r\n", seq=99))
        while model.feed_pending():
            pass
        view = model.panes[PaneId.HOST_SHELL]
        self.assertGreater(view.skipped_bytes, 0)
        self.assertIn("LIVE-END", "\n".join(view.screen.display))

    def test_catch_up_tail_starts_at_a_safe_boundary(self):
        tail = _safe_tail(b"a" * 1000 + b"\x1b[31mBAD" + b"\r\nOK\r\n", 20)
        self.assertEqual(b"OK\r\n", tail)  # after the newline, not mid-CSI
        tail = _safe_tail(b"a" * 100 + b"[31mtext\x1b[2J\x1b[Hnew", 24)
        self.assertEqual(b"\x1b[2J\x1b[Hnew", tail[tail.index(b"\x1b"):])
        self.assertTrue(tail.startswith(b"\x1b"))
        self.assertEqual(b"k" * 5, _safe_tail(b"\x1b\\" + b"k" * 50, 5))  # no boundary: unchanged
        model, _ = make()
        for i in range(30):
            model.enqueue_display(self.display(PaneId.HOST_SHELL, b"\x1b[31mcolored %d\x1b[0m\r\n" % i * 1500, seq=i))
        while model.feed_pending():
            pass
        shown = "\n".join(model.panes[PaneId.HOST_SHELL].screen.display)
        self.assertNotIn("[31m", shown)

    def test_large_frame_feed_is_not_quadratic(self):
        model, _ = make()
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"a" * (1024 * 1024), replay=True))
        started = time.perf_counter()
        while model.feed_pending(max_bytes=1 << 30, max_seconds=60):
            pass
        self.assertLess(time.perf_counter() - started, 8.0)
        self.assertEqual(0, len(model.panes[PaneId.HOST_SHELL].backlog))

    def _flood(self, model, tag, frames=30, seq=0):
        for i in range(frames):  # > CATCHUP_BACKLOG_BYTES of live output
            model.enqueue_display(self.display(PaneId.HOST_SHELL, b"".join(b"%s %06d\r\n" % (tag, n)
                                                                           for n in range(2000)), seq=seq + i))

    def test_catch_up_again_while_a_previous_catch_up_tail_is_feeding(self):
        """A catch-up tail is fed quietly but never blocks the next catch-up (only attach replay does)."""
        model, sender = make()
        view = model.panes[PaneId.HOST_SHELL]
        self._flood(model, b"first")
        model.feed_pending(max_bytes=1024, max_seconds=1.0)  # catch-up; the kept tail is barely started
        skipped = view.skipped_bytes
        self.assertGreater(skipped, 0)
        self.assertTrue(view.backlog)  # tail still feeding
        self._flood(model, b"second", seq=100)
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"\x1b[6nSECOND-LATEST\r\n", seq=999))
        resizes = len(sender.of("resize"))
        model.feed_pending(max_bytes=1024, max_seconds=1.0)
        self.assertEqual(0, view.backlog_bytes, "live backlog not caught up while the previous tail was feeding")
        self.assertEqual(resizes, len(sender.of("resize")))  # repaint nudge rate limited while catch-ups repeat
        self.assertGreater(view.skipped_bytes, skipped + 30 * 24000 - 64 * 1024)
        self.assertLessEqual(sum(len(item[2]) for item in view.backlog), 64 * 1024)
        while model.feed_pending():
            pass
        shown = text(model, PaneId.HOST_SHELL)
        self.assertIn("SECOND-LATEST", shown)
        self.assertNotIn("first", shown)
        self.assertEqual(resizes + 2, len(sender.of("resize")))  # ... but still sent once the tail is fed
        self.assertEqual([], sender.of("input"))  # the kept tail's stale query is not answered

    def test_per_loop_cost_is_bounded_with_a_50k_frame_backlog(self):
        model, _ = make()
        view = model.panes[PaneId.HOST_SHELL]
        frame = b"\x1b[1;31m\xed\x95\x9c\xea\xb8\x80 colour flood\x1b[0m F2MARK\r\n" * 25  # ~1 KiB, coloured Korean
        for i in range(50_000):
            model.enqueue_display(self.display(PaneId.HOST_SHELL, frame, seq=i))
        self.assertGreater(view.backlog_bytes, 45 * 1024 * 1024)
        started = time.perf_counter()
        model.feed_pending(max_bytes=1024, max_seconds=0.02)  # includes the catch-up of ~50 MB
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.08, f"one loop with a 50k-frame backlog took {elapsed:.3f}s")
        self.assertEqual(0, view.backlog_bytes)
        # while attach replay blocks catch-up, the per-loop check does not scan the backlog either
        model2, _ = make()
        model2.enqueue_display(self.display(PaneId.HOST_SHELL, b"R\r\n" * 100_000, replay=True))
        for i in range(50_000):
            model2.enqueue_display(self.display(PaneId.HOST_SHELL, frame, seq=i))
        started = time.perf_counter()
        for _ in range(20):
            model2.feed_pending(max_bytes=1024, max_seconds=0.02)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(0, model2.panes[PaneId.HOST_SHELL].skipped_bytes)  # replay still pending: no catch-up

    def test_bracketed_paste_state_tracked_over_dropped_items_without_joining(self):
        model, _ = make()
        view = model.panes[PaneId.HOST_SHELL]
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"old\x1b[?20", seq=1, session="old"))
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"04h", seq=2, session="old"))
        # a new session starts with 2004l split across two frames, then a flood that is dropped entirely
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"new\x1b[?1;20", seq=3, session="new"))
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"04l", seq=4, session="new"))
        for i in range(30):
            model.enqueue_display(self.display(PaneId.HOST_SHELL, b"z" * 20000, seq=5 + i, session="new"))
        self.assertIs(False, view.bracketed)  # state known as soon as the output is received
        model.feed_pending(max_bytes=1024, max_seconds=1.0)
        self.assertGreater(view.skipped_bytes, 0)
        self.assertIs(False, view.bracketed)
        # a sequence split exactly at the end of the dropped part and completed in the next frame
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"q" * 300000 + b"\x1b[?200", seq=50, session="new"))
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"4h", seq=51, session="new"))
        while model.feed_pending():
            pass
        self.assertIs(True, view.bracketed)
        # a new session forgets the previous state
        model.enqueue_display(self.display(PaneId.HOST_SHELL, b"$ ", seq=1, session="newer"))
        self.assertIsNone(view.bracketed)


class LayoutTests(unittest.TestCase):
    """C-D58: manager | worker on top, host shell full width below."""

    def test_boxes_and_inner_sizes(self):
        cases = {
            (30, 100): {"manager_omp": (2, 0, 14, 50), "worker_omp": (2, 50, 14, 50), "host_shell": (16, 0, 13, 100)},
            (45, 160): {"manager_omp": (2, 0, 21, 80), "worker_omp": (2, 80, 21, 80), "host_shell": (23, 0, 21, 160)},
            (50, 200): {"manager_omp": (2, 0, 24, 100), "worker_omp": (2, 100, 24, 100),
                        "host_shell": (26, 0, 23, 200)},
            (24, 81): {"manager_omp": (2, 0, 11, 40), "worker_omp": (2, 40, 11, 41), "host_shell": (13, 0, 10, 81)},
        }
        for (rows, cols), want in cases.items():
            with self.subTest(rows=rows, cols=cols):
                boxes = {p.value: box for p, box in pane_boxes(rows, cols).items()}
                self.assertEqual(want, boxes)
                inner = {p.value: s for p, s in pane_inner_sizes(rows, cols).items()}
                self.assertEqual({k: (h - 2, w - 2) for k, (_, _, h, w) in want.items()}, inner)

    def test_boxes_tile_the_body_exactly_for_many_sizes(self):
        for rows in range(MIN_ROWS, 60):
            for cols in (MIN_COLS, 31, 80, 99, 100, 101, 213):
                b = pane_boxes(rows, cols)
                m, w, h = b[PaneId.MANAGER_OMP], b[PaneId.WORKER_OMP], b[PaneId.HOST_SHELL]
                with self.subTest(rows=rows, cols=cols):
                    self.assertEqual((m[0], m[3] + w[3], m[1], w[1]), (2, cols, 0, m[3]))
                    self.assertEqual((m[0], m[2]), (w[0], w[2]))  # same top row, same height
                    self.assertEqual(h[0], m[0] + m[2])
                    self.assertEqual(h[0] + h[2], rows - 1)  # footer row is the last row
                    self.assertEqual((h[1], h[3]), (0, cols))
                    self.assertGreaterEqual(m[2] - 2, 2)  # neither row collapses at the minimum size
                    self.assertGreaterEqual(h[2] - 2, 2)
                    self.assertGreaterEqual(m[2], h[2])  # the top row gets the extra row on odd heights
                    self.assertLessEqual(m[2] - h[2], 1)
        self.assertTrue(make(MIN_ROWS - 1, 100)[0].too_small())
        self.assertFalse(make(MIN_ROWS, MIN_COLS)[0].too_small())

    def test_new_model_screens_and_resize_report_actual_pane_sizes(self):
        model, sender = make(30, 100)
        self.assertEqual({PaneId.MANAGER_OMP: (12, 48), PaneId.WORKER_OMP: (12, 48), PaneId.HOST_SHELL: (11, 98)},
                         {p: (s.lines, s.columns) for p, s in ((p, model.panes[p].screen) for p in PANES)})
        model.resize(45, 160)
        sent = {f["pane"]: (f["rows"], f["cols"]) for _, f, _ in sender.of("resize")}
        self.assertEqual({"manager_omp": (19, 78), "worker_omp": (19, 78), "host_shell": (19, 158)}, sent)
        for p in PANES:
            screen = model.panes[p].screen
            self.assertEqual(sent[p.value], (screen.lines, screen.columns))

    def test_view_module_uses_the_same_boxes(self):
        from workbench.ui.product.view import pane_rects
        for rows, cols in ((30, 100), (45, 160), (37, 101)):
            self.assertEqual(pane_boxes(rows, cols), pane_rects(rows, cols))


def numbered(count, start=0, width=5):
    return b"".join(b"line %0*d\r\n" % (width, i) for i in range(start, start + count))


def snapshot_of(model, pane):
    screen = model.panes[pane].screen
    return (list(screen.display), screen.cursor.x, screen.cursor.y, screen.cursor.hidden,
            {y: {x: c for x, c in row.items()} for y, row in screen.buffer.items()}, len(screen.history.top))


def visible(model, pane, rows=None):
    rows = rows or model.sizes[pane][0]
    out = []
    for line in model.pane_lines(pane, rows):
        out.append("".join(line.get(x, model.panes[pane].screen.default_char).data
                           for x in range(model.panes[pane].screen.columns)).rstrip())
    return out


class ScrollModeTests(unittest.TestCase):
    HOST = PaneId.HOST_SHELL

    def host(self, lines=200, rows=30, cols=100):
        model, sender = make(rows, cols)
        model.handle_input(P + b"3")  # focus host shell
        sender.sent.clear()
        model.on_display(display("host_shell", numbered(lines)))
        return model, sender

    def test_history_keeps_at_least_5000_lines_for_host_shell(self):
        model, _ = self.host(lines=6500)
        self.assertGreaterEqual(model.panes[self.HOST].screen.history.top.maxlen, 5000)
        self.assertGreaterEqual(len(model.panes[self.HOST].screen.history.top), 5000)
        model.handle_input(P + b"[")
        model.handle_input(b"\x1b[H")  # Home: as far back as history goes
        self.assertEqual(model.scroll_history(), model.scroll_offset())
        self.assertGreaterEqual(model.scroll_offset(), 5000)

    def test_enter_with_prefix_bracket_shows_indicator_and_scroll_keys(self):
        model, sender = self.host()
        live = visible(model, self.HOST)
        model.handle_input(P + b"[")
        self.assertIs(self.HOST, model.scroll_pane)
        self.assertEqual(live, visible(model, self.HOST))  # offset 0 = live
        self.assertIn("SCROLL", model.pane_title(self.HOST))
        self.assertIn("SCROLL", model.footer())
        page = model.sizes[self.HOST][0] - 1
        model.handle_input(b"\x1b[5~")  # PgUp
        self.assertEqual(page, model.scroll_offset())
        self.assertIn(f"{page}줄", model.pane_title(self.HOST))
        self.assertNotEqual(live, visible(model, self.HOST))
        model.handle_input(b"\x1b[A")
        model.handle_input(b"k")
        self.assertEqual(page + 2, model.scroll_offset())
        model.handle_input(b"\x1b[B")
        model.handle_input(b"j")
        model.handle_input(b"\x1b[6~")  # PgDn
        self.assertEqual(0, model.scroll_offset())
        model.handle_input(b"\x1b[6~")  # clamped at live
        self.assertEqual(0, model.scroll_offset())
        model.handle_input(b"g")
        self.assertEqual(model.scroll_history(), model.scroll_offset())
        model.handle_input(b"\x1b[F")  # End
        self.assertEqual(0, model.scroll_offset())
        model.handle_input(b"G")
        self.assertEqual(0, model.scroll_offset())
        self.assertEqual([], sender.of("input"))

    def test_top_of_history_shows_first_kept_lines_and_bottom_shows_live(self):
        model, _ = self.host(lines=200)
        rows = model.sizes[self.HOST][0]
        model.handle_input(P + b"[")
        model.handle_input(b"\x1b[H")
        self.assertEqual(f"line {0:05d}", visible(model, self.HOST)[0])
        self.assertEqual(f"line {rows - 1:05d}", visible(model, self.HOST)[-1])
        model.handle_input(b"\x1b[F")
        self.assertEqual(f"line {199:05d}", visible(model, self.HOST)[-2])

    def test_prefix_pgup_enters_and_goes_up_one_page(self):
        model, sender = self.host()
        model.handle_input(P + b"\x1b[5~")
        self.assertIs(self.HOST, model.scroll_pane)
        self.assertEqual(model.sizes[self.HOST][0] - 1, model.scroll_offset())
        self.assertEqual([], sender.of("input"))

    def test_keys_not_forwarded_while_scrolling_and_forwarded_again_after_q_or_esc(self):
        model, sender = self.host()
        model.handle_input(P + b"[")
        for key in (b"ls\r", b"\x03", b"\t", b"abc\x1bx", "한글".encode(), b"\x1b[15~", b"\x1bOP"):
            model.handle_input(key)
        model.handle_input(b"\x1b[200~pasted\x1b[201~")
        self.assertEqual([], sender.of("input") + sender.of("paste"))
        self.assertIn("붙여넣기", model.footer())
        model.handle_input(b"q")
        self.assertIsNone(model.scroll_pane)
        model.handle_input(b"ls\r")
        self.assertEqual([("input", {"pane": "host_shell"}, b"ls\r")], sender.of("input"))
        model.handle_input(P + b"[")
        model.handle_input(b"\x1b", now=10.0)
        self.assertIsNotNone(model.scroll_pane)  # a lone Esc is held briefly by the parser
        model.flush_input(now=10.5)
        self.assertIsNone(model.scroll_pane)
        sender.sent.clear()
        model.handle_input(b"x")
        self.assertEqual([b"x"], [d for *_, d in sender.of("input")])

    def test_multiple_keys_in_one_read(self):
        model, _ = self.host()
        model.handle_input(P + b"[")
        model.handle_input(b"\x1b[A\x1b[A\x1b[Bk")
        self.assertEqual(2, model.scroll_offset())
        model.handle_input(b"kq")
        self.assertIsNone(model.scroll_pane)

    def test_prefix_commands_still_work_in_scroll_mode_and_focus_change_leaves_it(self):
        model, sender = self.host()
        model.handle_input(P + b"[")
        model.handle_input(P + b"?")
        self.assertTrue(model.help_open)
        model.handle_input(b" ")
        self.assertIs(self.HOST, model.scroll_pane)
        model.handle_input(P + b"q")
        self.assertTrue(model.quit)
        model.quit = False
        model.handle_input(P + b"1")
        self.assertIsNone(model.scroll_pane)
        self.assertEqual(PaneId.MANAGER_OMP, model.focus)

    def test_new_output_while_scrolled_does_not_move_the_view(self):
        model, _ = self.host(lines=200)
        model.handle_input(P + b"[")
        for _ in range(3):
            model.handle_input(b"\x1b[5~")
        before = visible(model, self.HOST)
        offset = model.scroll_offset()
        model.on_display(display("host_shell", numbered(37, start=200)))
        self.assertEqual(before, visible(model, self.HOST), "view jumped on new output")
        self.assertEqual(offset + 37, model.scroll_offset())  # the indicator counts the lines now behind live
        # scrolling continues relative to the held position, and End reaches the new live screen
        model.handle_input(b"\x1b[F")
        self.assertEqual(f"line {236:05d}", [l for l in visible(model, self.HOST) if l][-1])

    def test_new_output_via_backlog_path_also_holds_the_view(self):
        model, _ = self.host(lines=200)
        model.handle_input(P + b"[")
        model.handle_input(b"\x1b[5~")
        before = visible(model, self.HOST)
        for i in range(5):
            frame = display("host_shell", numbered(20, start=200 + 20 * i))
            model.enqueue_display(frame)
        while model.feed_pending():
            pass
        self.assertEqual(before, visible(model, self.HOST))

    def test_exit_returns_to_the_live_screen_identical_to_never_scrolled(self):
        scrolled, _ = self.host(lines=300)
        plain, _ = self.host(lines=300)
        scrolled.handle_input(P + b"[")
        scrolled.handle_input(b"\x1b[5~\x1b[5~\x1b[A")
        scrolled.on_display(display("host_shell", numbered(10, start=300) + b"\x1b[3;7H\x1b[31mred"))
        plain.on_display(display("host_shell", numbered(10, start=300) + b"\x1b[3;7H\x1b[31mred"))
        scrolled.handle_input(b"q")
        self.assertEqual(snapshot_of(plain, self.HOST), snapshot_of(scrolled, self.HOST))
        self.assertEqual(visible(plain, self.HOST), visible(scrolled, self.HOST))

    def test_rendering_history_never_mutates_the_live_screen(self):
        model, _ = self.host(lines=300)
        model.on_display(display("host_shell", b"\x1b[5;9Hcursor-here"))
        before = snapshot_of(model, self.HOST)
        history_lines = list(model.panes[self.HOST].screen.history.top)
        model.handle_input(P + b"[")
        for key in (b"\x1b[5~", b"\x1b[5~", b"g", b"\x1b[6~", b"G", b"k"):
            model.handle_input(key)
            visible(model, self.HOST)
            model.pane_title(self.HOST)
        self.assertEqual(before, snapshot_of(model, self.HOST))
        self.assertEqual(history_lines, list(model.panes[self.HOST].screen.history.top))
        self.assertEqual(model.panes[self.HOST].screen.history.position, model.panes[self.HOST].screen.history.size)

    def test_live_output_after_scrolling_still_lands_on_the_live_screen_with_2004_tracking(self):
        model, _ = self.host(lines=100)
        model.on_display(display("host_shell", b"\x1b[?2004h"))
        model.handle_input(P + b"[\x1b[5~")
        model.on_display(display("host_shell", b"\x1b[?2004l$ prompt"))
        self.assertIs(False, model.panes[self.HOST].bracketed)
        model.handle_input(b"q")
        self.assertIn("$ prompt", text(model, self.HOST))

    def test_generation_change_or_catch_up_clamps_view_without_error(self):
        model, _ = self.host(lines=200)
        model.handle_input(P + b"[\x1b[5~")
        self.assertGreater(model.scroll_offset(), 0)
        model.on_display(display("host_shell", b"fresh", gen=2))  # new session resets screen and history
        self.assertEqual(0, model.scroll_offset())
        self.assertEqual(0, model.scroll_history())
        self.assertEqual(["fresh"], [l for l in visible(model, self.HOST) if l])
        model.handle_input(b"q")
        self.assertIsNone(model.scroll_pane)

    def test_alternate_screen_switch_leaves_scroll_mode(self):
        model, _ = self.host(lines=200)
        model.handle_input(P + b"[\x1b[5~")
        model.on_display(display("host_shell", b"\x1b[?1049h"))
        self.assertIsNone(model.scroll_pane)
        model.on_display(display("host_shell", numbered(50, start=1000)))  # alternate screen scrolls too
        model.on_display(display("host_shell", b"\x1b[?1049l"))
        model.handle_input(P + b"[")
        self.assertIn("line 00199", [l for l in visible(model, self.HOST)])  # primary screen is back

    def test_omp_pane_can_scroll_too(self):
        model, sender = make(30, 100)
        model.on_display(display("manager_omp", numbered(80)))
        model.handle_input(P + b"[\x1b[5~")
        self.assertIs(PaneId.MANAGER_OMP, model.scroll_pane)
        self.assertGreater(model.scroll_offset(), 0)
        self.assertEqual([], sender.of("input"))

    def test_resize_while_scrolled_keeps_a_valid_view(self):
        model, _ = self.host(lines=200)
        model.handle_input(P + b"[\x1b[5~")
        model.resize(45, 140)
        rows = model.sizes[self.HOST][0]
        self.assertEqual(rows, len(model.pane_lines(self.HOST, rows)))
        model.handle_input(b"G")
        model.handle_input(b"q")

    def test_help_lists_scroll_keys_and_layout(self):
        joined = "\n".join(HELP_LINES)
        for needle in ("prefix [", "PgUp", "PgDn", "Home", "End", "q/Esc", "scroll"):
            self.assertIn(needle, joined)
        model, _ = make()
        model.handle_input(P)
        self.assertIn("[", model.footer())


def sgr(button, x, y, release=False):
    return b"\x1b[<%d;%d;%d%s" % (button, x, y, b"m" if release else b"M")


WHEEL_UP, WHEEL_DOWN = 64, 65
TRACKING_ON = b"\x1b[?1000h\x1b[?1006h"


class MouseParserTests(unittest.TestCase):
    def test_sgr_reports_and_shift_page_keys_are_events_not_text(self):
        events = InputParser().feed(b"a" + sgr(64, 10, 5) + b"b" + sgr(0, 3, 4) + sgr(0, 3, 4, release=True)
                                    + b"\x1b[5;2~\x1b[6;2~c")
        self.assertEqual([Passthrough(b"a"), Mouse(64, 10, 5, False), Passthrough(b"b"), Mouse(0, 3, 4, False),
                          Mouse(0, 3, 4, True), PageScroll(1), PageScroll(-1), Passthrough(b"c")], events)

    def test_split_reports_are_held_whole_and_plain_pgup_still_passes(self):
        parser = InputParser()
        out = []
        for chunk in (b"\x1b", b"[", b"<6", b"4;10;", b"5", b"M\x1b[5", b";2", b"~x"):
            out += parser.feed(chunk, now=0.0)
        self.assertEqual([Mouse(64, 10, 5, False), PageScroll(1), Passthrough(b"x")], out)
        self.assertEqual([Passthrough(b"\x1b[5~")], InputParser().feed(b"\x1b[5~"))
        self.assertEqual([Passthrough(b"\x1b[1;5D\x1b[5;5~")], InputParser().feed(b"\x1b[1;5D\x1b[5;5~"))

    def test_partial_report_is_released_only_after_the_long_hold(self):
        parser = InputParser()
        self.assertEqual([], parser.feed(b"\x1b[<6", now=0.0))
        self.assertEqual([], parser.flush(now=0.2))
        self.assertEqual([Passthrough(b"\x1b[<6")], parser.flush(now=0.6))

    def test_report_after_prefix_keeps_the_prefix_pending(self):
        parser = InputParser()
        self.assertEqual([Mouse(64, 1, 1, False)], parser.feed(P + sgr(64, 1, 1)))
        self.assertTrue(parser.prefix_active)
        self.assertEqual([Command("d")], parser.feed(b"d"))


class DirectScrollTests(unittest.TestCase):
    """Wheel and Shift+PgUp/PgDn scroll without entering scroll mode; typing returns to live (CW-06 UR-UX)."""

    def setUp(self):
        self.model, self.sender = make(30, 120)
        for pane in ("manager_omp", "worker_omp", "host_shell"):
            self.model.on_display(display(pane, numbered(120)))
        self.sender.sent.clear()

    def cell(self, pane, dx=0, dy=0):
        """1-based terminal cell of an inner cell of ``pane``."""
        top, left, _, _ = pane_boxes(self.model.rows, self.model.cols)[pane]
        return left + 2 + dx, top + 2 + dy

    def mouse(self, pane, button, dx=0, dy=0, release=False):
        x, y = self.cell(pane, dx, dy)
        self.model.handle_input(sgr(button, x, y, release))

    def sent_kinds(self):
        return [kind for kind, _, _ in self.sender.sent]

    def test_wheel_over_each_pane_scrolls_that_pane_and_sends_nothing(self):
        for pane in PANES:
            with self.subTest(pane=pane):
                self.mouse(pane, WHEEL_UP)
                self.assertEqual(3, self.model.scroll_offset(pane))
                self.assertIn("SCROLL", self.model.pane_title(pane))
                self.assertIn("3줄", self.model.pane_title(pane))
                self.assertIsNone(self.model.scroll_pane)  # no mode entered
        self.assertEqual([], self.sender.sent)
        self.assertIs(PaneId.MANAGER_OMP, self.model.focus)
        for pane in PANES:  # the other panes did not move
            self.assertEqual(3, self.model.scroll_offset(pane))

    def test_wheel_moves_three_lines_per_notch_down_to_live_and_ends_the_scroll(self):
        pane = PaneId.HOST_SHELL
        for _ in range(4):
            self.mouse(pane, WHEEL_UP)
        self.assertEqual(12, self.model.scroll_offset(pane))
        self.mouse(pane, WHEEL_DOWN)
        self.assertEqual(9, self.model.scroll_offset(pane))
        for _ in range(5):
            self.mouse(pane, WHEEL_DOWN)
        self.assertFalse(self.model.scrolled(pane))
        self.assertNotIn("SCROLL", self.model.pane_title(pane))
        self.assertEqual([], self.sender.sent)

    def test_wheel_scrolls_the_lines_under_the_pointer_not_the_focus_pane(self):
        self.mouse(PaneId.WORKER_OMP, WHEEL_UP)
        self.assertEqual(3, self.model.scroll_offset(PaneId.WORKER_OMP))
        self.assertEqual(0, self.model.scroll_offset(PaneId.MANAGER_OMP))
        self.assertFalse(self.model.scrolled(PaneId.MANAGER_OMP))
        self.assertIs(PaneId.MANAGER_OMP, self.model.focus)

    def test_wheel_stops_at_the_oldest_kept_line(self):
        pane = PaneId.MANAGER_OMP
        for _ in range(500):
            self.mouse(pane, WHEEL_UP)
        self.assertEqual(self.model.scroll_history(pane), self.model.scroll_offset(pane))
        self.assertEqual("line 00000", visible(self.model, pane)[0])

    def test_wheel_without_history_does_nothing(self):
        model, sender = make(30, 120)
        x, y = 3, 4
        model.handle_input(sgr(WHEEL_UP, x, y))
        self.assertFalse(model.scrolled(PaneId.MANAGER_OMP))
        self.assertEqual([], sender.sent)

    def test_shift_pgup_pgdn_scroll_the_focused_pane_one_page(self):
        self.model.handle_input(P + b"3")
        self.sender.sent.clear()
        page = self.model.sizes[PaneId.HOST_SHELL][0] - 1
        self.model.handle_input(b"\x1b[5;2~")
        self.assertEqual(page, self.model.scroll_offset(PaneId.HOST_SHELL))
        self.assertFalse(self.model.scrolled(PaneId.MANAGER_OMP))
        self.model.handle_input(b"\x1b[5;2~")
        self.assertEqual(2 * page, self.model.scroll_offset(PaneId.HOST_SHELL))
        self.model.handle_input(b"\x1b[6;2~\x1b[6;2~")
        self.assertFalse(self.model.scrolled(PaneId.HOST_SHELL))
        self.assertIsNone(self.model.scroll_pane)
        self.assertEqual([], self.sender.sent)

    def test_typing_returns_the_focused_pane_to_live_and_is_delivered_once(self):
        self.model.handle_input(b"\x1b[5;2~")
        self.mouse(PaneId.WORKER_OMP, WHEEL_UP)
        live = [ln.rstrip() for ln in self.model.panes[PaneId.MANAGER_OMP].screen.display]
        self.assertNotEqual(live, visible(self.model, PaneId.MANAGER_OMP))
        self.model.handle_input(b"ls\r")
        self.assertEqual([("input", {"pane": "manager_omp"}, b"ls\r")], self.sender.sent)
        self.assertFalse(self.model.scrolled(PaneId.MANAGER_OMP))
        self.assertEqual(live, visible(self.model, PaneId.MANAGER_OMP))
        self.assertEqual(3, self.model.scroll_offset(PaneId.WORKER_OMP))  # other panes keep their position

    def test_special_keys_also_return_to_live_and_arrive_unchanged(self):
        for key in (b"\x1b[A", b"\x1b", b"\x03", "한".encode(), b"\x1b[5~", b"q", b"\x1b[15~"):
            with self.subTest(key=key):
                self.model.handle_input(b"\x1b[5;2~")
                self.assertTrue(self.model.scrolled(PaneId.MANAGER_OMP))
                self.sender.sent.clear()
                self.model.handle_input(key, now=100.0)
                self.model.flush_input(now=101.0)
                self.assertEqual([("input", {"pane": "manager_omp"}, key)], self.sender.sent)
                self.assertFalse(self.model.scrolled(PaneId.MANAGER_OMP))

    def test_paste_while_scrolled_returns_to_live_and_is_pasted_once(self):
        self.model.handle_input(b"\x1b[5;2~")
        self.model.handle_input(b"\x1b[200~hello\nworld\x1b[201~")
        self.assertEqual([("paste", {"pane": "manager_omp"}, b"\x1b[200~hello\nworld\x1b[201~")], self.sender.sent)
        self.assertFalse(self.model.scrolled(PaneId.MANAGER_OMP))

    def test_prefix_command_does_not_return_to_live_but_focus_change_keeps_positions(self):
        self.mouse(PaneId.MANAGER_OMP, WHEEL_UP)
        self.model.handle_input(P + b"2")
        self.assertEqual(3, self.model.scroll_offset(PaneId.MANAGER_OMP))  # focus moved away, view kept
        self.model.handle_input(b"x")  # goes to worker (live); manager stays scrolled
        self.assertEqual(3, self.model.scroll_offset(PaneId.MANAGER_OMP))
        self.assertEqual([b"x"], [d for k, _, d in self.sender.sent if k == "input"])

    def test_mouse_reports_never_reach_a_pane_as_text(self):
        model, sender = self.model, self.sender
        for pane in PANES:
            for button in (0, 1, 2, 32, 35, 64, 65, 66, 67, 68, 0 | 8 | 16):
                for release in (False, True):
                    x, y = self.cell(pane, 1, 1)
                    model.handle_input(sgr(button, x, y, release))
        for x, y in ((1, 1), (1, 2), (120, 30), (60, 3), (60, 16), (1, 16), (500, 500), (0, 0)):
            model.handle_input(sgr(64, x, y) + sgr(0, x, y))
        self.assertEqual([], [f for f in sender.sent if f[0] in {"input", "paste"}])
        model.handle_input(b"a" + sgr(64, 3, 4) + b"b")  # interleaved with typing
        self.assertEqual([b"a", b"b"], [d for k, _, d in sender.sent if k == "input"])

    def test_wheel_on_borders_header_and_footer_is_ignored(self):
        for x, y in ((1, 3), (60, 3), (61, 3), (120, 10), (5, 1), (5, 2), (5, 30), (5, 17), (5, 16)):
            self.model.handle_input(sgr(WHEEL_UP, x, y))
        self.assertFalse(any(self.model.scrolled(p) for p in PANES))

    def test_wheel_is_forwarded_to_a_pane_whose_app_enabled_mouse_tracking(self):
        self.model.on_display(display("worker_omp", TRACKING_ON))
        self.mouse(PaneId.WORKER_OMP, WHEEL_UP, dx=4, dy=2)
        self.mouse(PaneId.WORKER_OMP, WHEEL_DOWN, dx=0, dy=0)
        self.assertEqual([("input", {"pane": "worker_omp"}, b"\x1b[<64;5;3M"),
                          ("input", {"pane": "worker_omp"}, b"\x1b[<65;1;1M")], self.sender.sent)
        self.assertFalse(self.model.scrolled(PaneId.WORKER_OMP))
        self.mouse(PaneId.HOST_SHELL, WHEEL_UP)  # another pane without tracking still scrolls the view
        self.assertEqual(3, self.model.scroll_offset(PaneId.HOST_SHELL))

    def test_tracking_pane_gets_clicks_and_releases_translated_and_focus_does_not_change(self):
        self.model.on_display(display("host_shell", b"\x1b[?1002h\x1b[?1006h"))
        self.mouse(PaneId.HOST_SHELL, 0, dx=9, dy=1)
        self.mouse(PaneId.HOST_SHELL, 0, dx=9, dy=1, release=True)
        self.assertEqual([b"\x1b[<0;10;2M", b"\x1b[<0;10;2m"], [d for _, _, d in self.sender.sent])
        self.assertEqual({"host_shell"}, {f["pane"] for _, f, _ in self.sender.sent})
        self.assertIs(PaneId.MANAGER_OMP, self.model.focus)
        self.assertEqual([], self.sender.of("focus"))

    def test_tracking_without_sgr_uses_the_legacy_encoding(self):
        self.model.on_display(display("worker_omp", b"\x1b[?1000h"))
        self.mouse(PaneId.WORKER_OMP, WHEEL_UP, dx=2, dy=1)
        self.mouse(PaneId.WORKER_OMP, 0, dx=2, dy=1, release=True)
        self.assertEqual([b"\x1b[M" + bytes((32 + 64, 32 + 3, 32 + 2)), b"\x1b[M" + bytes((32 + 3, 32 + 3, 32 + 2))],
                         [d for _, _, d in self.sender.sent])

    def test_mouse_tracking_ends_with_decrst_and_with_a_new_session(self):
        self.model.on_display(display("worker_omp", TRACKING_ON))
        self.model.on_display(display("worker_omp", b"\x1b[?1000l"))
        self.mouse(PaneId.WORKER_OMP, WHEEL_UP)
        self.assertEqual([], self.sender.sent)
        self.assertEqual(3, self.model.scroll_offset(PaneId.WORKER_OMP))
        self.model.on_display(display("manager_omp", TRACKING_ON))
        self.model.on_display(display("manager_omp", b"fresh", gen=2))  # process replaced: tracking forgotten
        self.mouse(PaneId.MANAGER_OMP, WHEEL_UP)
        self.assertEqual([], self.sender.sent)

    def test_pane_tracking_is_one_mode_like_a_real_terminal(self):
        # xterm/tmux: 1000/1002/1003 replace each other and ANY reset turns tracking off
        self.model.on_display(display("worker_omp", b"\x1b[?1002h\x1b[?1006h\x1b[?1000l"))
        self.mouse(PaneId.WORKER_OMP, WHEEL_UP)
        self.assertEqual([], self.sender.sent, "a reset of 1000 must end 1002 tracking too")
        self.assertEqual(3, self.model.scroll_offset(PaneId.WORKER_OMP))
        self.model.on_display(display("host_shell", b"\x1b[?1003h\x1b[?1000h\x1b[?1006h"))
        self.assertEqual({1000}, self.model.panes[PaneId.HOST_SHELL].modes & {1000, 1002, 1003})

    def test_motion_is_forwarded_only_to_apps_that_asked_for_it(self):
        # the outer terminal reports motion-while-pressed (1002) for divider drags; a 1000 app never sees it
        for mode, want in ((b"1000", b""), (b"1002", b"\x1b[<32;3;2M"), (b"1003", b"\x1b[<32;3;2M\x1b[<35;3;2M")):
            with self.subTest(mode=mode):
                self.setUp()
                self.model.on_display(display("worker_omp", b"\x1b[?" + mode + b"h\x1b[?1006h"))
                self.mouse(PaneId.WORKER_OMP, 32, dx=2, dy=1)  # left button held + motion
                self.mouse(PaneId.WORKER_OMP, 35, dx=2, dy=1)  # motion, no button (1003 only)
                self.assertEqual(want, b"".join(d for _, _, d in self.sender.sent))
                self.assertIs(PaneId.MANAGER_OMP, self.model.focus)

    def test_motion_over_a_pane_without_tracking_does_nothing(self):
        self.mouse(PaneId.WORKER_OMP, 32, dx=2, dy=1)
        self.assertEqual([], self.sender.sent)
        self.assertIs(PaneId.MANAGER_OMP, self.model.focus)

    def test_prefix_r_asks_the_app_loop_to_reassert_the_mouse_mode_once(self):
        self.assertFalse(self.model.take_mouse_reassert())
        self.model.handle_input(b"\x1dr")
        self.assertTrue(self.model.take_mouse_reassert())
        self.assertFalse(self.model.take_mouse_reassert())

    def test_tracking_seen_split_across_chunks_and_via_the_backlog_path(self):
        self.model.enqueue_display(display("worker_omp", b"\x1b[?10"))
        self.model.enqueue_display(display("worker_omp", b"00;1006h"))
        self.mouse(PaneId.WORKER_OMP, WHEEL_UP)  # tracked on receipt, before pyte is fed
        self.assertEqual([b"\x1b[<64;1;1M"], [d for _, _, d in self.sender.sent])

    def test_alternate_screen_wheel_becomes_arrow_keys_without_history_scroll(self):
        self.model.on_display(display("host_shell", b"\x1b[?1049h"))
        self.mouse(PaneId.HOST_SHELL, WHEEL_UP)
        self.mouse(PaneId.HOST_SHELL, WHEEL_DOWN)
        self.assertEqual([("input", {"pane": "host_shell"}, b"\x1b[A" * 3),
                          ("input", {"pane": "host_shell"}, b"\x1b[B" * 3)], self.sender.sent)
        self.assertFalse(self.model.scrolled(PaneId.HOST_SHELL))
        self.sender.sent.clear()
        self.model.on_display(display("host_shell", b"\x1b[?1h"))  # application cursor keys
        self.mouse(PaneId.HOST_SHELL, WHEEL_UP)
        self.assertEqual([b"\x1bOA" * 3], [d for _, _, d in self.sender.sent])
        self.sender.sent.clear()
        self.model.on_display(display("host_shell", b"\x1b[?1049l"))  # back on the primary screen: history scroll
        self.mouse(PaneId.HOST_SHELL, WHEEL_UP)
        self.assertEqual([], self.sender.sent)
        self.assertEqual(3, self.model.scroll_offset(PaneId.HOST_SHELL))

    def test_alternate_screen_with_mouse_tracking_forwards_instead_of_arrows(self):
        self.model.on_display(display("worker_omp", b"\x1b[?1049h" + TRACKING_ON))
        self.mouse(PaneId.WORKER_OMP, WHEEL_UP)
        self.assertEqual([b"\x1b[<64;1;1M"], [d for _, _, d in self.sender.sent])

    def test_left_click_focuses_a_pane_without_tracking(self):
        self.mouse(PaneId.HOST_SHELL, 0)
        self.assertIs(PaneId.HOST_SHELL, self.model.focus)
        self.assertEqual([("focus", {"pane": "host_shell"}, b"")], self.sender.sent)
        self.mouse(PaneId.HOST_SHELL, 0, release=True)
        self.mouse(PaneId.WORKER_OMP, 2)  # right button does not focus
        self.mouse(PaneId.WORKER_OMP, 32)  # drag/motion does not focus
        self.assertIs(PaneId.HOST_SHELL, self.model.focus)
        self.mouse(PaneId.WORKER_OMP, 0)
        self.assertIs(PaneId.WORKER_OMP, self.model.focus)
        self.assertEqual(["focus", "focus"], self.sent_kinds())

    def test_click_on_a_tracking_pane_is_forwarded_not_focused(self):
        self.model.on_display(display("worker_omp", TRACKING_ON))
        self.mouse(PaneId.WORKER_OMP, 0)
        self.assertIs(PaneId.MANAGER_OMP, self.model.focus)
        self.assertEqual(["input"], self.sent_kinds())

    def test_click_on_border_header_footer_or_help_is_ignored(self):
        for x, y in ((60, 3), (1, 1), (5, 2), (5, 30), (5, 16), (5, 17)):
            self.model.handle_input(sgr(0, x, y))
        self.assertEqual([], self.sender.sent)
        self.model.handle_input(P + b"?")
        self.mouse(PaneId.HOST_SHELL, 0)
        self.mouse(PaneId.HOST_SHELL, WHEEL_UP)
        self.assertTrue(self.model.help_open)
        self.assertEqual([], self.sender.sent)

    def test_prefix_m_toggles_mouse_capture_with_a_notice_and_ignores_reports_while_off(self):
        self.assertTrue(self.model.mouse_capture)
        self.model.handle_input(P + b"m")
        self.assertFalse(self.model.mouse_capture)
        self.assertIn("꺼짐", self.model.notice)
        self.mouse(PaneId.MANAGER_OMP, WHEEL_UP)
        self.mouse(PaneId.HOST_SHELL, 0)
        self.assertFalse(self.model.scrolled(PaneId.MANAGER_OMP))
        self.assertEqual([], self.sender.sent)
        self.model.handle_input(P + b"m")
        self.assertTrue(self.model.mouse_capture)
        self.assertIn("켜짐", self.model.notice)
        self.assertEqual([], self.sender.sent)  # nothing goes to any pane

    def test_footer_and_help_describe_the_direct_scroll_and_shift_drag(self):
        joined = "\n".join(HELP_LINES)
        for needle in ("휠", "Shift+PgUp", "Shift+PgDn", "prefix m", "Shift+드래그", "live 복귀"):
            self.assertIn(needle, joined)
        self.assertIn("Shift+드래그", self.model.footer())
        self.mouse(PaneId.MANAGER_OMP, WHEEL_UP)
        self.assertIn("휠", self.model.footer())
        self.assertNotIn("키는 pane으로 전달되지 않음", self.model.footer())

    def test_wheel_and_shift_pgup_also_work_while_in_scroll_mode(self):
        self.model.handle_input(P + b"[")
        self.mouse(PaneId.MANAGER_OMP, WHEEL_UP)
        self.model.handle_input(b"\x1b[5;2~")
        page = self.model.sizes[PaneId.MANAGER_OMP][0] - 1
        self.assertEqual(3 + page, self.model.scroll_offset())
        self.mouse(PaneId.MANAGER_OMP, WHEEL_DOWN, dx=1)
        self.model.handle_input(b"\x1b[6;2~")
        self.assertEqual(0, self.model.scroll_offset())
        self.assertIs(PaneId.MANAGER_OMP, self.model.scroll_pane)  # explicit mode stays until q/Esc
        self.model.handle_input(b"x")
        self.assertEqual([], self.sender.of("input"))  # mode still swallows keys
        self.model.handle_input(b"q")
        self.assertIsNone(self.model.scroll_pane)

    # -- delta-review P3 fixes ------------------------------------------------------------------------
    def test_f2_prefix_bracket_or_prefix_pgup_while_scrolled_keeps_the_anchor(self):
        self.mouse(PaneId.MANAGER_OMP, WHEEL_UP)
        self.mouse(PaneId.MANAGER_OMP, WHEEL_UP)
        self.assertEqual(6, self.model.scroll_offset())
        self.model.handle_input(P + b"[")
        self.assertEqual(6, self.model.scroll_offset())
        self.assertIs(PaneId.MANAGER_OMP, self.model.scroll_pane)
        self.model.handle_input(P + b"\x1b[5~")
        self.assertEqual(6 + self.model.sizes[PaneId.MANAGER_OMP][0] - 1, self.model.scroll_offset())

    def test_f2_prefix_bracket_keeps_the_anchor_while_output_arrives(self):
        self.mouse(PaneId.MANAGER_OMP, WHEEL_UP)
        self.model.on_display(display("manager_omp", numbered(10, start=120)))
        self.assertEqual(13, self.model.scroll_offset())
        self.model.handle_input(P + b"[")
        self.assertEqual(13, self.model.scroll_offset())

    def test_f3_catch_up_while_scrolled_returns_to_live_with_a_notice(self):
        self.mouse(PaneId.HOST_SHELL, WHEEL_UP)
        self.model.handle_input(P + b"3")
        self.model.handle_input(P + b"[")
        self.assertIs(PaneId.HOST_SHELL, self.model.scroll_pane)
        for i in range(3):
            self.model.enqueue_display(display("host_shell", numbered(CATCHUP_BACKLOG_BYTES // 8 // 2, start=1000 * i)))
        while self.model.feed_pending():
            pass
        self.assertIsNone(self.model.scroll_pane)
        self.assertFalse(self.model.scrolled(PaneId.HOST_SHELL))
        self.assertIn("live로 복귀", self.model.notice)
        self.assertEqual(0, self.model.scroll_offset(PaneId.HOST_SHELL))

    def test_f3_catch_up_leaves_other_panes_scroll_alone(self):
        self.mouse(PaneId.MANAGER_OMP, WHEEL_UP)
        for i in range(3):
            self.model.enqueue_display(display("host_shell", numbered(CATCHUP_BACKLOG_BYTES // 8 // 2, start=1000 * i)))
        while self.model.feed_pending():
            pass
        self.assertEqual(3, self.model.scroll_offset(PaneId.MANAGER_OMP))

    def test_f3_new_session_generation_returns_a_directly_scrolled_pane_to_live_with_a_notice(self):
        self.mouse(PaneId.WORKER_OMP, WHEEL_UP)
        self.model.on_display(display("worker_omp", b"fresh", gen=2))
        self.assertFalse(self.model.scrolled(PaneId.WORKER_OMP))
        self.assertIn("live로 복귀", self.model.notice)
        self.assertIn("fresh", text(self.model, PaneId.WORKER_OMP))

    def test_f4_backend_focus_change_leaves_scroll_mode(self):
        self.model.handle_input(P + b"[")
        self.assertIs(PaneId.MANAGER_OMP, self.model.scroll_pane)
        self.model.apply_snapshot(snapshot(focus="host_shell"))
        self.assertIs(PaneId.HOST_SHELL, self.model.focus)
        self.assertIsNone(self.model.scroll_pane)
        self.model.handle_input(b"x")
        self.assertEqual([("input", {"pane": "host_shell"}, b"x")], self.sender.sent)

    def test_f4_backend_snapshot_without_focus_change_keeps_scroll_mode(self):
        self.model.handle_input(P + b"[")
        self.model.apply_snapshot(snapshot(focus="manager_omp"))
        self.assertIs(PaneId.MANAGER_OMP, self.model.scroll_pane)


class SplitGeometryTests(unittest.TestCase):
    """C-D59: divider ratios, clamps, zoom and persistence (pure geometry)."""

    def test_default_layout_is_the_legacy_split_at_every_size(self):
        for rows in range(MIN_ROWS, 60):
            for cols in range(MIN_COLS, 140, 7):
                body = rows - 3
                boxes = pane_boxes(rows, cols, Layout())
                self.assertEqual(boxes, pane_boxes(rows, cols))
                self.assertEqual((cols // 2, (body + 1) // 2),
                                 (boxes[PaneId.MANAGER_OMP][3], boxes[PaneId.MANAGER_OMP][2]))

    def test_ratios_move_the_dividers_and_clamp_to_usable_panes(self):
        boxes = pane_boxes(30, 120, Layout(0.25, 0.5))
        self.assertEqual((30, 90), (boxes[PaneId.MANAGER_OMP][3], boxes[PaneId.WORKER_OMP][3]))
        low = pane_boxes(30, 120, Layout(0.01, 0.99))  # too small a manager, too tall a top row
        self.assertEqual(12, low[PaneId.MANAGER_OMP][3])  # 10 inner columns
        self.assertEqual(27 - 5, low[PaneId.MANAGER_OMP][2])  # host keeps 3 inner rows
        high = pane_boxes(30, 120, Layout(0.99, 0.01))
        self.assertEqual(12, high[PaneId.WORKER_OMP][3])
        self.assertEqual(5, high[PaneId.MANAGER_OMP][2])  # top row keeps 3 inner rows
        for layout in (Layout(0.0001, 0.0001), Layout(0.9999, 0.9999)):  # the smallest window: 2 inner rows
            b = pane_boxes(MIN_ROWS, MIN_COLS, layout)
            self.assertEqual(4, b[PaneId.MANAGER_OMP][2])
            self.assertEqual(4, b[PaneId.HOST_SHELL][2])
            self.assertGreaterEqual(min(b[PaneId.MANAGER_OMP][3], b[PaneId.WORKER_OMP][3]) - 2, 10)

    def test_boxes_tile_the_body_for_many_sizes_and_ratios(self):
        for rows in range(MIN_ROWS, 50, 3):
            for cols in (MIN_COLS, 31, 57, 80, 101, 213):
                for col in (None, 0.05, 0.33, 0.5, 0.72, 0.95):
                    for row in (None, 0.05, 0.4, 0.5, 0.9):
                        b = pane_boxes(rows, cols, Layout(col, row))
                        m, w, h = b[PaneId.MANAGER_OMP], b[PaneId.WORKER_OMP], b[PaneId.HOST_SHELL]
                        with self.subTest(rows=rows, cols=cols, col=col, row=row):
                            self.assertEqual(cols, m[3] + w[3])
                            self.assertEqual((m[1], w[1]), (0, m[3]))
                            self.assertEqual((m[0], m[2]), (w[0], w[2]))
                            self.assertEqual(h[0], m[0] + m[2])
                            self.assertEqual(rows - 1, h[0] + h[2])
                            self.assertGreaterEqual(min(m[2], h[2]) - 2, 2)
                            self.assertGreaterEqual(min(m[3], w[3]) - 2, 10)
                            if rows >= 13:
                                self.assertGreaterEqual(min(m[2], h[2]) - 2, 3)

    def test_zoom_shows_only_that_pane_over_the_whole_body(self):
        boxes = pane_boxes(30, 120, Layout(0.3, 0.6), PaneId.WORKER_OMP)
        self.assertEqual({PaneId.WORKER_OMP: (2, 0, 27, 120)}, boxes)
        inner = pane_inner_sizes(30, 120, Layout(0.3, 0.6), PaneId.WORKER_OMP)
        self.assertEqual((25, 118), inner[PaneId.WORKER_OMP])
        self.assertEqual(pane_inner_sizes(30, 120, Layout(0.3, 0.6))[PaneId.HOST_SHELL], inner[PaneId.HOST_SHELL])

    def test_view_module_uses_the_same_boxes_with_layout_and_zoom(self):
        from workbench.ui.product.view import pane_rects
        for layout, zoom in ((Layout(0.3, 0.6), None), (Layout(), PaneId.HOST_SHELL)):
            self.assertEqual(pane_boxes(37, 101, layout, zoom), pane_rects(37, 101, layout, zoom))


class LayoutFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cwl-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / LAYOUT_FILE

    def test_round_trip_mode_0600_atomic_and_no_leftovers(self):
        self.assertTrue(save_layout(self.path, Layout(0.3, 0.65), True))
        self.assertEqual((Layout(0.3, 0.65), True), load_layout(self.path))
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual([LAYOUT_FILE], sorted(p.name for p in self.dir.iterdir()))
        self.assertTrue(save_layout(self.path, Layout(None, None), False))
        self.assertEqual((Layout(), False), load_layout(self.path))
        self.assertEqual({"version": 1, "col_ratio": None, "row_ratio": None, "zoom": False},
                         json.loads(self.path.read_text()))

    def test_missing_or_unwritable_is_safe(self):
        self.assertEqual((Layout(), False), load_layout(self.path))
        self.assertFalse(save_layout(self.dir / "no" / "such" / LAYOUT_FILE, Layout(0.3, 0.3), False))

    def test_corrupt_or_unknown_content_yields_defaults(self):
        good = {"version": 1, "col_ratio": 0.3, "row_ratio": 0.4, "zoom": True}
        cases = [b"\xff\xfe garbage", b"", b"[]", b"null", b"{", b"NaN", json.dumps({**good, "version": 2}).encode(),
                 json.dumps({k: v for k, v in good.items() if k != "version"}).encode(),
                 json.dumps({**good, "col_ratio": 2}).encode(), json.dumps({**good, "col_ratio": 0}).encode(),
                 json.dumps({**good, "row_ratio": "0.5"}).encode(), json.dumps({**good, "row_ratio": True}).encode(),
                 b'{"version": 1, "col_ratio": NaN}', b'{"version": 1, "col_ratio": Infinity}',
                 json.dumps({**good, "zoom": "yes"}).encode(), b"[" * 5000,
                 b'{"version": 1, "pad": "' + b"x" * 5000 + b'"}']
        for raw in cases:
            with self.subTest(raw=raw[:30]):
                self.path.write_bytes(raw)
                self.assertEqual((Layout(), False), load_layout(self.path))
        self.path.write_text(json.dumps({**good, "future": {"a": 1}}))
        self.assertEqual((Layout(0.3, 0.4), True), load_layout(self.path))  # unknown extra keys are ignored

    def test_symlink_and_directory_are_not_followed(self):
        target = self.dir / "elsewhere"
        target.write_text(json.dumps({"version": 1, "col_ratio": 0.3, "row_ratio": 0.4, "zoom": True}))
        self.path.symlink_to(target)
        self.assertEqual((Layout(), False), load_layout(self.path))
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual((Layout(), False), load_layout(self.path))


class SplitModelTests(unittest.TestCase):
    """C-D59: keyboard, mouse, zoom, reset, debounce and scroll behaviour of the model."""

    def setUp(self):
        self.now = [0.0]
        self.sender = FakeSender()
        self.model = ProductModel(self.sender, 30, 120, clock=lambda: 1000.0, monotonic=lambda: self.now[0])
        self.model.apply_snapshot(snapshot())
        self.sender.sent.clear()
        self.seen_kinds = set()

    def kinds(self):
        return [k for k, _, _ in self.sender.sent]

    def resizes(self, clear=True):
        out = [(f["pane"], f["rows"], f["cols"]) for _, f, _ in self.sender.of("resize")]
        self.seen_kinds.update(self.kinds())
        if clear:
            self.sender.sent.clear()
        return out

    def widths(self):
        b = pane_boxes(30, 120, self.model.layout, self.model.zoom)
        return b[PaneId.MANAGER_OMP][3], b[PaneId.WORKER_OMP][3]

    # -- keyboard ----------------------------------------------------------------------------------------------
    def test_prefix_arrows_move_dividers_and_send_only_changed_panes(self):
        self.model.handle_input(P + b"\x1b[D", now=10.0)
        self.assertEqual([("manager_omp", 12, 56), ("worker_omp", 12, 60)], self.resizes())
        self.model.handle_input(P + b"\x1bOC", now=20.0)  # SS3 (application cursor keys) too
        self.model.handle_input(P + b"\x1b[C", now=30.0)
        self.assertEqual((62, 58), self.widths())
        self.resizes()
        self.model.handle_input(P + b"\x1b[A", now=40.0)
        self.assertEqual([("manager_omp", 11, 60), ("worker_omp", 11, 56), ("host_shell", 12, 118)], self.resizes())
        self.model.handle_input(P + b"\x1b[B", now=50.0)
        self.model.handle_input(P + b"\x1bOB", now=60.0)
        self.assertEqual([("manager_omp", 13, 60), ("worker_omp", 13, 56), ("host_shell", 10, 118)],
                         self.resizes()[-3:])
        self.assertTrue(self.model.layout_dirty)
        self.assertEqual({"resize"}, self.seen_kinds)  # nothing else, in particular no input keys

    def test_bare_arrows_repeat_for_a_second_and_are_never_forwarded_then(self):
        self.model.handle_input(P + b"\x1b[D", now=10.0)
        self.resizes()
        self.model.handle_input(b"\x1b[D", now=10.9)
        self.model.handle_input(b"\x1b[D\x1b[D", now=11.8)  # each arrow renews the window
        self.assertEqual((52, 68), self.widths())
        self.assertEqual({"resize"}, set(self.kinds()))
        self.assertIn("←↑↓→", self.model.footer())
        self.assertTrue(self.model.resize_repeat_active)
        self.resizes()
        self.model.handle_input(b"\x1b[D", now=12.9)  # 1.1 s after the last one: a normal key again
        self.assertEqual([("input", {"pane": "manager_omp"}, b"\x1b[D")], self.sender.sent)
        self.assertEqual((52, 68), self.widths())

    def test_repeat_ends_on_any_other_key_and_only_the_arrow_prefix_starts_it(self):
        self.model.handle_input(P + b"\x1b[D", now=1.0)
        self.resizes()
        self.model.handle_input(b"\x1b[Da\x1b[D", now=1.2)  # arrow, then 'a' ends the repeat, the next arrow is a key
        self.assertEqual(["resize", "resize", "input"], self.kinds())  # one arrow resized, the rest is forwarded verbatim
        self.assertEqual([b"a\x1b[D"], [p for k, _, p in self.sender.sent if k == "input"])
        self.sender.sent.clear()
        self.model.handle_input(b"\x1b[D", now=1.3)  # the repeat is over: a plain key now
        self.assertEqual([b"\x1b[D"], [p for k, _, p in self.sender.sent if k == "input"])
        self.sender.sent.clear()
        for opener in (P + b"r", P + b"z", P + b"="):
            self.model.handle_input(opener, now=5.0)
            self.sender.sent.clear()
            self.model.handle_input(b"\x1b[C", now=5.1)
            self.assertEqual([b"\x1b[C"], [p for k, _, p in self.sender.of("input")], opener)
            self.sender.sent.clear()

    def test_mouse_between_repeat_arrows_keeps_it_but_paste_and_scroll_mode_end_it(self):
        self.model.handle_input(P + b"\x1b[D", now=1.0)
        self.model.handle_input(b"\x1b[<64;5;5M", now=1.1)  # wheel over the manager
        self.resizes()
        self.model.handle_input(b"\x1b[D", now=1.2)
        self.assertEqual({"resize"}, set(self.kinds()))
        self.model.handle_input(b"\x1b[200~x\x1b[201~", now=1.3)
        self.sender.sent.clear()
        self.model.handle_input(b"\x1b[D", now=1.4)
        self.assertEqual(["input"], self.kinds())
        self.sender.sent.clear()
        self.model.handle_input(P + b"\x1b[D", now=2.0)
        self.model.handle_input(P + b"[", now=2.1)
        self.sender.sent.clear()
        self.model.handle_input(b"\x1b[D", now=2.2)  # scroll mode owns bare arrows
        self.assertEqual([], self.kinds())

    def test_repeat_hint_leaves_the_footer_after_the_window(self):
        self.model.handle_input(P + b"\x1b[D", now=1.0)
        self.assertFalse(self.model.flush_input(now=1.5))
        self.assertTrue(self.model.resize_repeat_active)
        self.assertTrue(self.model.flush_input(now=2.5))  # repaint asked
        self.assertFalse(self.model.resize_repeat_active)
        self.assertNotIn("계속 조절", self.model.footer())

    def test_dividers_stop_at_the_minimum_pane_size_with_a_notice(self):
        for i in range(60):
            self.model.handle_input(P + b"\x1b[D", now=float(i))
        self.assertEqual(12, self.widths()[0])
        self.assertIn("최소", self.model.notice)
        for i in range(60):
            self.model.handle_input(P + b"\x1b[B", now=float(i))
        self.assertEqual(3, self.model.sizes[PaneId.HOST_SHELL][0])  # host keeps 3 inner rows at the limit
        self.assertEqual(10, self.model.sizes[PaneId.MANAGER_OMP][1])  # manager kept its 10 inner columns

    def test_layout_follows_window_resizes_proportionally(self):
        self.model.layout = Layout(0.25, 0.7)
        self.model.resize(40, 200)
        self.assertEqual(50, self.model.sizes[PaneId.MANAGER_OMP][1] + 2)
        self.assertEqual(int(37 * 0.7 + 0.5), self.model.sizes[PaneId.MANAGER_OMP][0] + 2)
        sent = {f["pane"]: (f["rows"], f["cols"]) for _, f, _ in self.sender.of("resize")[-3:]}
        self.assertEqual({p.value: s for p, s in self.model.sizes.items()}, sent)

    # -- mouse -------------------------------------------------------------------------------------------------
    def press(self, x, y):
        self.model.handle_input(sgr(0, x, y))

    def move(self, x, y):
        self.model.handle_input(sgr(32, x, y))

    def release(self, x, y):
        self.model.handle_input(sgr(0, x, y, True))

    def test_drag_moves_the_vertical_divider_with_debounced_resize_frames(self):
        self.press(60, 8)  # manager's right border, inside the top row
        self.assertTrue(self.model.drag_active)
        self.assertEqual([], self.sender.sent)
        self.move(41, 8)  # leading edge: goes out at once
        self.assertEqual([("manager_omp", 12, 39), ("worker_omp", 12, 77)], self.resizes())
        for x, t in ((42, 0.01), (43, 0.02), (44, 0.03)):
            self.now[0] = t
            self.move(x, 8)
        self.assertEqual([], self.sender.sent, "dragging must not flood the backend")
        self.assertEqual(42, self.model.sizes[PaneId.MANAGER_OMP][1])  # the divider follows the pointer locally
        self.model.flush_resizes()
        self.assertEqual([], self.sender.sent, "still inside the debounce window")
        self.now[0] = 0.5
        self.model.flush_resizes()
        self.assertEqual([("manager_omp", 12, 42), ("worker_omp", 12, 74)], self.resizes())
        self.now[0] = 0.51
        self.release(50, 12)
        self.assertFalse(self.model.drag_active)
        self.assertEqual([("manager_omp", 12, 48), ("worker_omp", 12, 68)], self.resizes())
        self.assertTrue(self.model.layout_dirty)
        self.assertIs(PaneId.MANAGER_OMP, self.model.focus)

    def test_drag_grabbing_the_worker_side_border_and_clamping(self):
        self.press(61, 3)  # worker's left border at the title line
        self.move(1, 3)
        self.assertEqual(12, self.widths()[0])
        self.move(200, 3)
        self.assertEqual(12, self.widths()[1])
        self.release(70, 3)
        self.assertEqual(69, self.widths()[0])  # worker border grabbed (offset 0): divider at the pointer column

    def test_drag_moves_the_horizontal_divider(self):
        self.press(5, 16)  # bottom border of the top row
        self.move(5, 21)
        self.release(5, 21)
        top = pane_boxes(30, 120, self.model.layout)[PaneId.MANAGER_OMP][2]
        self.assertEqual(19, top)
        self.assertEqual((17, 58), self.model.sizes[PaneId.MANAGER_OMP])
        self.assertEqual((6, 118), self.model.sizes[PaneId.HOST_SHELL])
        self.press(5, 22)  # host's top border (the divider now sits lower) grabs it too
        self.move(5, 2)
        self.release(5, 2)
        self.assertEqual(5, pane_boxes(30, 120, self.model.layout)[PaneId.MANAGER_OMP][2])

    def test_divider_clicks_never_reach_panes_or_change_focus(self):
        for pane in ("manager_omp", "worker_omp", "host_shell"):
            self.model.on_display(display(pane, b"\x1b[?1000h\x1b[?1006h"))  # apps that track the mouse
        self.sender.sent.clear()
        for x, y in ((60, 8), (61, 8), (5, 16), (5, 17), (90, 16), (61, 3)):
            self.press(x, y)
            self.release(x, y)
        self.assertEqual([], self.sender.sent)
        self.assertIs(PaneId.MANAGER_OMP, self.model.focus)
        self.assertFalse(self.model.drag_active)
        self.press(61, 8)  # worker border again: a plain click never focuses the worker
        self.release(61, 8)
        self.assertEqual([], self.kinds())

    def test_lost_release_and_capture_toggle_end_the_drag(self):
        self.press(60, 8)
        self.model.handle_input(sgr(35, 30, 8))  # motion with no button held
        self.assertFalse(self.model.drag_active)
        self.press(60, 8)
        self.model.handle_input(P + b"m")
        self.assertFalse(self.model.drag_active)
        self.model.handle_input(P + b"m")
        self.press(60, 8)
        self.model.handle_input(P + b"?")
        self.assertFalse(self.model.drag_active)

    def test_press_inside_a_pane_still_focuses_and_wheel_over_border_is_ignored(self):
        self.press(90, 8)
        self.release(90, 8)
        self.assertIs(PaneId.WORKER_OMP, self.model.focus)
        self.sender.sent.clear()
        self.model.handle_input(sgr(64, 60, 8))
        self.assertEqual([], self.sender.sent)
        self.assertFalse(self.model.drag_active)

    # -- zoom / reset ----------------------------------------------------------------------------------------------
    def test_zoom_toggle_hides_other_panes_and_restores(self):
        self.model.handle_input(P + b"2")
        self.sender.sent.clear()
        self.model.handle_input(P + b"z")
        self.assertIs(PaneId.WORKER_OMP, self.model.zoom)
        self.assertEqual([("worker_omp", 25, 118)], self.resizes())
        self.assertIn("[ZOOM]", self.model.pane_title(PaneId.WORKER_OMP))
        self.assertNotIn("[ZOOM]", self.model.pane_title(PaneId.MANAGER_OMP))
        self.model.handle_input(sgr(0, 5, 8))  # where the manager used to be: hidden, no focus change, no input
        self.model.handle_input(sgr(0, 5, 8, True))
        self.assertIs(PaneId.WORKER_OMP, self.model.focus)
        self.assertEqual([], self.sender.sent)
        self.model.handle_input(P + b"\x1b[D")  # dividers do not move while zoomed
        self.assertEqual([], self.sender.sent)
        self.assertIn("확대", self.model.notice)
        self.model.handle_input(P + b"z")
        self.assertIsNone(self.model.zoom)
        self.assertEqual([("worker_omp", 12, 58)], self.resizes())

    def test_zoom_follows_focus_changes(self):
        self.model.handle_input(P + b"z")
        self.resizes()
        self.model.handle_input(P + b"3")
        self.assertIs(PaneId.HOST_SHELL, self.model.zoom)
        self.assertEqual({"manager_omp": (12, 58), "host_shell": (25, 118)},
                         {p: (r, c) for p, r, c in self.resizes()})
        self.model.pending.clear()  # (the fake sender never answers the focus request)
        self.model.apply_snapshot(snapshot(focus="worker_omp"))  # backend-driven focus
        self.assertIs(PaneId.WORKER_OMP, self.model.zoom)
        self.assertEqual({"host_shell", "worker_omp"}, {p for p, _, _ in self.resizes()})

    def test_reset_restores_the_default_split_and_zoom(self):
        self.model.handle_input(P + b"\x1b[D", now=1.0)
        self.model.handle_input(P + b"z", now=2.0)
        self.sender.sent.clear()
        self.model.handle_input(P + b"=", now=3.0)
        self.assertEqual(Layout(), self.model.layout)
        self.assertIsNone(self.model.zoom)
        self.assertEqual(pane_inner_sizes(30, 120), self.model.sizes)
        self.assertTrue(self.model.layout_dirty)
        self.assertEqual({"resize"}, set(self.kinds()))

    def test_help_and_footer_list_the_new_keys(self):
        joined = "\n".join(HELP_LINES)
        for needle in ("드래그", "prefix ←/→", "prefix ↑/↓", "prefix z", "prefix =", "1초"):
            self.assertIn(needle, joined)
        self.model.handle_input(P)
        self.assertIn("←↑↓→", self.model.footer())

    # -- persistence hooks -----------------------------------------------------------------------------------------
    def test_set_layout_state_sends_nothing_until_attach(self):
        self.model.set_layout_state(Layout(0.25, 0.6), True)
        self.assertEqual([], self.sender.sent)
        self.assertIs(PaneId.MANAGER_OMP, self.model.zoom)
        self.assertEqual((25, 118), self.model.sizes[PaneId.MANAGER_OMP])
        self.assertFalse(self.model.layout_dirty)
        self.model.after_attach()
        first = {f["pane"]: (f["rows"], f["cols"]) for _, f, _ in self.sender.of("resize")[:3]}
        self.assertEqual({p.value: s for p, s in self.model.sizes.items()}, first)
        self.assertEqual((Layout(0.25, 0.6), True), self.model.take_layout_state())
        self.assertFalse(self.model.layout_dirty)

    # -- scroll -----------------------------------------------------------------------------------------------------
    def test_scroll_positions_survive_resizes_and_are_clamped(self):
        for pane in ("manager_omp", "worker_omp", "host_shell"):
            self.model.on_display(display(pane, numbered(150)))
        self.model.scroll_by(40, PaneId.HOST_SHELL)
        self.model.scroll_by(7, PaneId.MANAGER_OMP)
        self.sender.sent.clear()
        for key in (b"\x1b[A", b"\x1b[A", b"\x1b[D", b"\x1b[B"):
            self.model.handle_input(P + key, now=1.0)
        self.assertEqual(40, self.model.scroll_offset(PaneId.HOST_SHELL))
        self.assertEqual(7, self.model.scroll_offset(PaneId.MANAGER_OMP))
        self.model.handle_input(P + b"z", now=2.0)  # zoom the manager: much taller
        for pane in PANES:
            self.assertLessEqual(self.model.scroll_offset(pane), self.model.scroll_history(pane) or 0)
            rows = self.model.sizes[pane][0]
            self.assertEqual(rows, len(self.model.pane_lines(pane, rows)))
        self.assertEqual(7, self.model.scroll_offset(PaneId.MANAGER_OMP))
        self.assertNotIn("input", self.kinds())
        self.model.scroll_by(10 ** 6, PaneId.HOST_SHELL)  # far beyond history is clamped
        self.assertEqual(self.model.scroll_history(PaneId.HOST_SHELL), self.model.scroll_offset(PaneId.HOST_SHELL))


# ---------------------------------------------------------------------------------------- IME-neutral prefix commands
CTRL = lambda ch: bytes([ord(ch) & 0x1F])  # noqa: E731
HANGUL_HINT = "한글 입력 상태: Ctrl-] 뒤 자모 + Space (예: ㅂ Space = q detach), Ctrl 조합(Ctrl-] Ctrl-q), Ctrl-] Space 메뉴"
# letter -> Ctrl alias (handoff/mouse move off the ambiguous Ctrl-h/Ctrl-m)
CTRL_ALIAS = {"q": "q", "t": "t", "c": "y", "r": "r", "z": "z", "h": "o", "m": "e"}
AMBIGUOUS_CTRL = (b"\x08", b"\x09", b"\x0a", b"\x0d", b"\x1b")  # Ctrl-h/i/j/m/[ = Backspace/Tab/LF/Enter/Esc
MENU_ORDER = ("[", "z", "t", "c", "h", "r", "m", "=", "?", "q")  # digits 1..9,0


def _effects(model, sender):
    return (model.quit, model.zoom, model.mouse_capture, model.help_open, model.scroll_pane,
            [x[0] for x in sender.sent])


class CtrlAliasTests(unittest.TestCase):
    def test_every_letter_command_has_a_ctrl_alias_with_the_same_effect(self):
        for letter, alias in CTRL_ALIAS.items():
            with self.subTest(letter=letter, alias=alias):
                plain, plain_sender = make()
                aliased, aliased_sender = make()
                plain.handle_input(P + letter.encode())
                aliased.handle_input(P + CTRL(alias))
                self.assertEqual(_effects(plain, plain_sender), _effects(aliased, aliased_sender))
                self.assertNotEqual(_effects(*make()), _effects(aliased, aliased_sender))
                self.assertEqual([], aliased_sender.of("input"))
                self.assertFalse(aliased.parser.prefix_active)

    def test_specific_ctrl_aliases(self):
        model, sender = make()
        model.handle_input(P + CTRL("t"))
        self.assertEqual(1, len(sender.of("takeover_request")))
        model.handle_input(P + CTRL("y"))
        self.assertEqual(1, len(sender.of("takeover_confirm")))
        model.handle_input(P + CTRL("o"))
        self.assertEqual(1, len(sender.of("handoff")))
        sender.sent.clear()
        model.handle_input(P + CTRL("r"))
        self.assertTrue(model.take_mouse_reassert())
        model.handle_input(P + CTRL("z"))
        self.assertIsNotNone(model.zoom)
        model.handle_input(P + CTRL("e"))
        self.assertFalse(model.mouse_capture)
        model.handle_input(P + CTRL("q"))
        self.assertTrue(model.quit)

    def test_held_ctrl_sequence_in_one_read_and_split_reads(self):
        model, sender = make()
        model.handle_input(P + CTRL("z") + P + CTRL("z"))  # zoom on, zoom off, no bytes to the pane
        self.assertIsNone(model.zoom)
        self.assertEqual([], sender.of("input"))
        model.handle_input(P)
        model.handle_input(CTRL("q"))
        self.assertTrue(model.quit)
        parser = InputParser()
        self.assertEqual([Command("\x11")], parser.feed(P + CTRL("q")))

    def test_plain_letters_still_work_in_english_mode(self):
        for key, check in ((b"q", lambda m, s: m.quit), (b"z", lambda m, s: m.zoom is not None),
                           (b"m", lambda m, s: not m.mouse_capture), (b"t", lambda m, s: s.of("takeover_request")),
                           (b"c", lambda m, s: s.of("takeover_confirm")), (b"h", lambda m, s: s.of("handoff")),
                           (b"r", lambda m, s: m.take_mouse_reassert())):
            model, sender = make()
            model.handle_input(P + key)
            self.assertTrue(check(model, sender), key)

    def test_ambiguous_ctrl_keys_are_not_commands(self):
        for raw in AMBIGUOUS_CTRL:
            if raw == b"\x09":  # Tab is the existing next-pane command
                continue
            with self.subTest(key=raw):
                model, sender = make()
                model.handle_input(P + raw, now=0.0)
                model.flush_input(now=0.5)
                self.assertFalse(model.quit)
                self.assertIsNone(model.zoom)
                self.assertTrue(model.mouse_capture)
                self.assertFalse(model.help_open)
                self.assertFalse(model.menu_open)
                self.assertEqual([], [x for x in sender.sent if x[0] != "focus"])
                self.assertIn("알 수 없는", model.notice)

    def test_ctrl_bracket_prefix_twice_and_other_ctrl_letters_are_not_aliases(self):
        model, sender = make()
        model.handle_input(P + P)
        self.assertEqual([b"\x1d"], [x[2] for x in sender.of("input")])
        for letter in "abcfglnsuvwx":  # Ctrl-k is the host terminal kill alias since C-D63, Ctrl-p pause/resume since CW-18 U5
            other, other_sender = make()
            other.handle_input(P + CTRL(letter))
            self.assertEqual([], other_sender.sent)
            self.assertFalse(other.quit)
            self.assertIsNone(other.zoom)
            self.assertIn("알 수 없는", other.notice)

    def test_ctrl_alias_while_prefix_arrow_resize_repeat_is_unchanged(self):
        model, sender = make()
        model.handle_input(P + b"\x1b[D", now=1.0)
        self.assertTrue(model.resize_repeat_active)
        model.handle_input(b"\x1b[D", now=1.2)  # bare arrow keeps resizing
        self.assertEqual([], sender.of("input"))
        model.handle_input(P + CTRL("z"), now=1.3)
        self.assertIsNotNone(model.zoom)
        self.assertFalse(model.resize_repeat_active)

    def test_bracketed_paste_after_prefix_is_unaffected(self):
        model, sender = make()
        model.handle_input(P)
        model.handle_input(b"\x1b[200~pa\nste\x1b[201~")
        self.assertEqual(1, len(sender.of("paste")))
        self.assertFalse(model.parser.prefix_active)
        self.assertEqual([], sender.of("input"))
        self.assertFalse(model.menu_open)


class HangulHintTests(unittest.TestCase):
    def test_hangul_after_prefix_shows_hint_and_forwards_nothing(self):
        for char in ("한", "안", "ㅢ", "ㄳ"):  # syllables and jamo without a 2-set key are never guessed
            with self.subTest(char=char):
                model, sender = make()
                model.handle_input(P + char.encode())
                self.assertEqual(HANGUL_HINT, model.notice)
                self.assertIn(HANGUL_HINT, model.footer())
                self.assertEqual([], sender.sent)
                self.assertFalse(model.parser.prefix_active)
                self.assertFalse(model.quit)
                self.assertFalse(model.menu_open)
                model.handle_input(b"x")  # prefix state cleared: plain key reaches the pane
                self.assertEqual([b"x"], [s[2] for s in sender.of("input")])

    def test_split_hangul_bytes_after_prefix_are_dropped_whole(self):
        model, sender = make()
        model.handle_input(P + b"\xec\x95")
        model.handle_input(b"\x88d")  # syllable '안' split across reads, then an English d
        self.assertEqual(HANGUL_HINT, model.notice)
        self.assertEqual([b"d"], [s[2] for s in sender.of("input")])
        self.assertFalse(model.quit)

    def test_hangul_hint_does_not_fire_without_prefix_and_english_clears_nothing_else(self):
        model, sender = make()
        model.handle_input("한글".encode())
        self.assertEqual("", model.notice)
        self.assertEqual(["한글".encode()], [s[2] for s in sender.of("input")])

    def test_hangul_hint_for_alt_multibyte_key(self):
        model, sender = make()
        model.handle_input(P + b"\x1b" + "한".encode())
        self.assertEqual(HANGUL_HINT, model.notice)
        self.assertEqual([], sender.sent)

    def test_multi_syllable_burst_after_prefix_is_dropped_whole_with_one_hint(self):
        for text in ("안녕", "안녕하세요"):
            data = text.encode()
            for cut in range(1, len(data)):
                with self.subTest(text=text, cut=cut):
                    model, sender = make()
                    model.handle_input(P + data[:cut], now=0.0)
                    model.handle_input(data[cut:] + b"d", now=0.01)  # the rest of the run, then an English d
                    self.assertEqual(HANGUL_HINT, model.notice)
                    self.assertEqual([b"d"], [s[2] for s in sender.of("input")])
                    self.assertFalse(model.quit)
                    self.assertFalse(model.parser.prefix_active)

    def test_multi_syllable_burst_followed_by_a_command_in_the_same_read(self):
        model, sender = make()
        model.handle_input(P + "안녕".encode() + P + b"\x11")  # Ctrl-q alias right after the run
        self.assertTrue(model.quit)
        self.assertEqual([], sender.of("input"))

    def test_ime_commit_key_right_after_the_run_is_consumed_too(self):
        keys = {"Enter": b"\r", "LF": b"\n", "Space": b" ", "Tab": b"\t", "Ctrl-d": b"\x04", "Ctrl-c": b"\x03"}
        for text in ("안녕",):
            data = text.encode()
            for name, key in keys.items():
                for cut in range(1, len(data) + 1):
                    with self.subTest(text=text, key=name, cut=cut):
                        model, sender = make()
                        model.handle_input(P + data[:cut], now=0.0)
                        model.handle_input(data[cut:] + key, now=0.01)  # same read (cut == len: key alone, split read)
                        self.assertEqual(HANGUL_HINT, model.notice)
                        self.assertEqual([], sender.sent)
                        self.assertFalse(model.quit)
                        self.assertFalse(model.menu_open)
                        self.assertFalse(model.parser.prefix_active)
                        model.handle_input(b"x", now=0.02)  # only the one commit key is spent
                        self.assertEqual([b"x"], [s[2] for s in sender.of("input")])

    def test_second_key_after_the_commit_key_is_handled_normally(self):
        model, sender = make()
        model.handle_input(P + "안".encode() + b"\r\r", now=0.0)
        self.assertEqual([b"\r"], [s[2] for s in sender.of("input")])
        model, sender = make()
        model.handle_input(P + "안".encode() + b" d", now=0.0)  # Space spent (opens no menu), d is text
        self.assertFalse(model.menu_open)
        self.assertEqual([b"d"], [s[2] for s in sender.of("input")])

    def test_commit_key_long_after_the_run_or_esc_or_prefix_is_not_swallowed(self):
        model, sender = make()
        model.handle_input(P + "ㅇ".encode(), now=0.0)
        model.handle_input(b"\r", now=5.0)
        self.assertEqual([b"\r"], [s[2] for s in sender.of("input")])
        model, sender = make()
        model.handle_input(P + "ㅇ".encode() + P + b"\x11", now=0.0)  # the prefix still starts a command
        self.assertTrue(model.quit)
        self.assertEqual([], sender.of("input"))

    def test_hangul_typed_long_after_the_dropped_run_reaches_the_pane(self):
        model, sender = make()
        model.handle_input(P + "안녕".encode(), now=0.0)
        model.flush_input(now=0.1)
        model.handle_input("글".encode(), now=5.0)
        self.assertEqual(["글".encode()], [s[2] for s in sender.of("input")])

    def test_prefix_key_alone_closes_the_open_menu(self):
        model, sender = make()
        model.handle_input(P + b" ", now=0.0)
        self.assertTrue(model.menu_open)
        self.assertTrue(model.handle_input(P, now=1.0))  # a redraw is requested
        self.assertFalse(model.menu_open)
        self.assertEqual([], sender.sent)
        self.assertFalse(model.parser.prefix_active)
        model.handle_input(b"1", now=2.0)  # fresh state: an ordinary key for the pane
        model.handle_input(b"x", now=3.0)
        self.assertEqual([b"1", b"x"], [s[2] for s in sender.of("input")])

    def test_prefix_alone_then_new_prefix_command_runs_normally_and_stays_closed(self):
        model, sender = make()
        model.handle_input(P + b" ", now=0.0)
        model.handle_input(P, now=1.0)
        model.handle_input(P + b"y", now=2.0)  # a fresh prefix + unknown key: nothing forwarded, no menu
        self.assertEqual([], sender.sent)
        self.assertFalse(model.menu_open)
        self.assertFalse(model.parser.prefix_active)

    def test_prefix_plus_lone_esc_closing_the_menu_leaks_nothing_even_after_the_flush(self):
        model, sender = make()
        model.handle_input(P + b" ", now=0.0)
        model.handle_input(P + b"\x1b", now=1.0)
        model.flush_input(now=2.0)
        self.assertFalse(model.menu_open)
        self.assertEqual([], sender.sent)
        model.handle_input(b"x", now=3.0)
        self.assertEqual([b"x"], [s[2] for s in sender.of("input")])

    def test_prefix_closing_the_menu_clears_the_parser_prefix_state(self):
        for follow in (b"d", b"1", b" ", b"x"):
            with self.subTest(follow=follow):
                model, sender = make()
                model.handle_input(P + b" ", now=0.0)
                model.handle_input(P, now=1.0)
                self.assertFalse(model.menu_open)
                self.assertFalse(model.parser.prefix_active)
                self.assertNotIn("prefix…", model.footer())
                model.handle_input(follow, now=2.0)  # a fresh state: the key goes to the pane, runs nothing
                self.assertEqual([follow], [s[2] for s in sender.of("input")])
                self.assertFalse(model.quit)
                self.assertFalse(model.menu_open)

    def test_hangul_while_menu_open_is_dropped_without_leaving_the_menu(self):
        model, sender = make()
        model.handle_input(P + b" ")
        model.handle_input("ㅇ".encode())
        self.assertTrue(model.menu_open)
        self.assertEqual([], sender.sent)


class JamoAfterPrefixTests(unittest.TestCase):
    """A lone 2-set jamo after the prefix runs the command of its QWERTY key; the IME commit key is swallowed."""

    def test_jamo_table_matches_the_standard_dubeolsik_layout(self):
        from workbench.ui.product.input import JAMO_KEYS
        expected = dict(zip("ㅂㅈㄷㄱㅅㅛㅕㅑㅐㅔㅁㄴㅇㄹㅎㅗㅓㅏㅣㅋㅌㅊㅍㅠㅜㅡ" "ㅃㅉㄸㄲㅆㅒㅖ",
                            "qwertyuiopasdfghjklzxcvbnm" "QWERTOP"))
        self.assertEqual(expected, JAMO_KEYS)

    def test_jamo_detaches_with_every_commit_key_split_and_late(self):
        keys = {"Space": b" ", "CR": b"\r", "LF": b"\n", "Tab": b"\t", "Ctrl-x": b"\x18"}
        data = "ㅂ".encode()
        for name, key in keys.items():
            for cut in range(1, len(data) + 1):
                for late in (0.01, 5.0):  # the IME may hold the jamo for seconds
                    with self.subTest(key=name, cut=cut, late=late):
                        model, sender = make()
                        model.handle_input(P, now=0.0)
                        model.handle_input(data[:cut], now=late if cut < len(data) else late + 3.0)  # the IME hands the jamo over only when it commits
                        if cut < len(data):
                            self.assertFalse(model.quit)
                            self.assertTrue(model.parser.prefix_active)  # partial UTF-8 held, never dropped
                            model.flush_input(now=late + 3.0)  # the real loop keeps flushing while it waits
                            self.assertTrue(model.parser.prefix_active)
                            self.assertFalse(model.quit)
                        model.handle_input(data[cut:], now=late + 3.0)
                        self.assertTrue(model.quit)
                        model.handle_input(key, now=late + 3.01)  # the commit key: spent, no menu, no pane
                        self.assertFalse(model.menu_open)
                        self.assertEqual([], sender.sent)
                        self.assertEqual("", model.notice)
                        self.assertFalse(model.parser.prefix_active)

    def test_jamo_and_commit_key_in_one_read(self):
        for tail in (b" ", b"\r"):
            model, sender = make()
            model.handle_input(P + "ㅂ".encode() + tail, now=0.0)
            self.assertTrue(model.quit)
            self.assertEqual([], sender.sent)
            self.assertFalse(model.menu_open)

    def test_prefix_stays_armed_for_any_length_of_time_before_the_jamo(self):
        model, sender = make()
        model.handle_input(P, now=0.0)
        for t in (1.0, 5.0, 60.0):
            model.flush_input(now=t)
            self.assertTrue(model.parser.prefix_active)
        model.handle_input("ㅂ".encode(), now=61.0)
        self.assertTrue(model.quit)
        self.assertEqual([], sender.sent)

    def test_every_jamo_runs_the_command_of_its_key_exactly_like_the_ascii_key(self):
        from workbench.ui.product.input import JAMO_KEYS
        for jamo, key in JAMO_KEYS.items():
            with self.subTest(jamo=jamo, key=key):
                typed, typed_sender = make()
                typed.handle_input(P + key.encode(), now=0.0)
                converted, sender = make()
                converted.handle_input(P + jamo.encode() + b" ", now=0.0)
                self.assertEqual(_effects(typed, typed_sender), _effects(converted, sender))
                self.assertEqual(typed.notice, converted.notice)
                self.assertFalse(converted.menu_open)
                self.assertEqual(typed.footer(), converted.footer())

    def test_named_commands(self):
        for jamo, attr in (("ㅋ", "zoom"), ("ㅂ", "quit"), ("ㅡ", "mouse_capture")):
            with self.subTest(jamo=jamo):
                model, sender = make()
                before = getattr(model, attr)
                model.handle_input(P + jamo.encode() + b" ", now=0.0)
                self.assertNotEqual(before, getattr(model, attr))
                self.assertEqual([], sender.of("input"))
        for jamo, kind in (("ㅅ", "takeover_request"), ("ㅊ", "takeover_confirm"), ("ㅗ", "handoff")):
            with self.subTest(jamo=jamo):
                model, sender = make()
                model.handle_input(P + jamo.encode() + b" ", now=0.0)
                self.assertEqual([kind], [x[0] for x in sender.sent])
        model, sender = make()
        model.handle_input(P + "ㄱ".encode() + b" ", now=0.0)  # r = redraw
        self.assertTrue(model.take_mouse_reassert())
        self.assertEqual([], sender.of("input"))

    def test_shifted_jamo_behaves_like_the_uppercase_key(self):
        model, sender = make()
        model.handle_input(P + "ㅆ".encode() + b" ", now=0.0)  # T = takeover request like t
        self.assertEqual(["takeover_request"], [x[0] for x in sender.sent])
        model, sender = make()
        model.handle_input(P + "ㅉ".encode() + b" ", now=0.0)  # W: unknown command key
        self.assertIn("'W'", model.notice)
        self.assertEqual([], sender.sent)
        model, sender = make()
        model.handle_input(P + "ㅃ".encode() + b" ", now=0.0)  # Q = detach like q
        self.assertTrue(model.quit)
        self.assertEqual([], sender.sent)

    def test_unmapped_result_is_an_unknown_command_key(self):
        model, sender = make()
        model.handle_input(P + "ㅁ".encode() + b" ", now=0.0)  # a
        self.assertIn("알 수 없는 prefix 명령 'a'", model.notice)
        self.assertEqual([], sender.sent)
        self.assertFalse(model.menu_open)

    def test_syllable_or_multi_character_run_keeps_the_hint(self):
        for text in ("안", "안녕", "ㅇㅇ", "ㅇ안"):
            with self.subTest(text=text):
                model, sender = make()
                model.handle_input(P + text.encode() + b" ", now=0.0)
                self.assertEqual(HANGUL_HINT, model.notice)
                self.assertEqual([], sender.sent)
                self.assertFalse(model.quit)
                self.assertFalse(model.menu_open)

    def test_later_characters_of_the_same_run_are_dropped_silently(self):
        model, sender = make()
        model.handle_input(P + "ㅂ".encode(), now=0.0)
        model.handle_input("ㅏ".encode(), now=0.1)
        self.assertTrue(model.quit)
        self.assertEqual([], sender.sent)

    def test_hangul_without_the_prefix_is_unchanged(self):
        model, sender = make()
        model.handle_input("ㅇ".encode() + b" ", now=0.0)
        self.assertEqual(["ㅇ ".encode()], [s[2] for s in sender.of("input")])
        self.assertFalse(model.quit)

    def test_ctrl_alias_prefix_space_menu_and_ascii_still_work(self):
        model, sender = make()
        model.handle_input(P + b"\x11")
        self.assertTrue(model.quit)
        model, sender = make()
        model.handle_input(P + b" ")
        self.assertTrue(model.menu_open)
        model.handle_input("ㅇ".encode())  # in the menu Hangul is still swallowed
        self.assertTrue(model.menu_open)
        self.assertFalse(model.quit)

    def test_hint_and_help_describe_the_jamo_usage(self):
        self.assertIn("자모", HANGUL_HINT)
        self.assertIn("Space", HANGUL_HINT)
        self.assertTrue(any("자모" in line and "Space" in line for line in HELP_LINES))


class JamoInScrollModeTests(unittest.TestCase):
    HOST = PaneId.HOST_SHELL

    def scrolling(self):
        model, sender = make(30, 100)
        model.handle_input(P + b"3")
        sender.sent.clear()
        model.on_display(display("host_shell", numbered(300)))
        model.handle_input(P + b"[", now=0.0)
        self.assertEqual(self.HOST, model.scroll_pane)
        return model, sender

    def test_jamo_letters_scroll_like_the_ascii_keys(self):
        for jamo, ascii_key in (("ㅓ", b"j"), ("ㅏ", b"k"), ("ㅎ", b"g")):
            for commit in (b" ", b"\r"):
                with self.subTest(jamo=jamo, commit=commit):
                    model, sender = self.scrolling()
                    reference, _ = self.scrolling()
                    model.handle_input(b"\x1b[5~", now=1.0)  # start one page back so both directions move
                    reference.handle_input(b"\x1b[5~", now=1.0)
                    model.handle_input(jamo.encode() + commit, now=2.0)
                    reference.handle_input(ascii_key, now=2.0)
                    self.assertEqual(reference.scroll_offset(), model.scroll_offset())
                    self.assertEqual(self.HOST, model.scroll_pane)
                    self.assertEqual([], sender.sent)

    def test_jamo_split_and_late_and_quit(self):
        model, sender = self.scrolling()
        model.handle_input(b"\x1b[5~", now=1.0)
        data = "ㅏ".encode()
        model.handle_input(data[:1], now=2.0)
        model.flush_input(now=9.0)
        model.handle_input(data[1:], now=10.0)  # k: one line back
        self.assertEqual(model._page() + 1, model.scroll_offset())
        model.handle_input(b" ", now=10.01)  # the commit key is spent
        model.handle_input("ㅂ".encode() + b" ", now=11.0)  # q quits scroll mode
        self.assertIsNone(model.scroll_pane)
        self.assertEqual([], sender.sent)

    def test_hangul_syllable_in_scroll_mode_is_still_never_forwarded(self):
        model, sender = self.scrolling()
        model.handle_input("안녕".encode(), now=1.0)
        self.assertEqual(self.HOST, model.scroll_pane)
        self.assertEqual([], sender.sent)

    def test_hangul_outside_scroll_mode_is_forwarded_unchanged(self):
        model, sender = make()
        model.handle_input("ㅓ".encode() + b" ", now=0.0)
        self.assertEqual(["ㅓ ".encode()], [s[2] for s in sender.of("input")])


class CommandMenuTests(unittest.TestCase):
    def open(self):
        model, sender = make()
        model.handle_input(P + b" ")
        self.assertTrue(model.menu_open)
        return model, sender

    def test_prefix_space_opens_menu_listing_all_commands_with_keys_and_digits(self):
        model, sender = self.open()
        lines = model.menu_lines()
        joined = "\n".join(lines)
        for digit, key in zip("1234567890", MENU_ORDER):
            self.assertTrue(any(line.lstrip("> ").startswith(digit) for line in lines), (digit, joined))
        for needle in ("Ctrl-q", "Ctrl-t", "Ctrl-y", "Ctrl-o", "Ctrl-r", "Ctrl-e", "Ctrl-z", "detach", "1/2/3",
                       "↑/↓", "Enter", "Esc"):
            self.assertIn(needle, joined)
        self.assertEqual(0, model.menu_index)
        self.assertEqual([], sender.sent)
        self.assertIn("메뉴", model.footer())

    def test_enter_runs_the_selected_item_and_arrows_navigate(self):
        for digit_index, key in enumerate(MENU_ORDER):
            with self.subTest(index=digit_index, key=key):
                model, sender = self.open()
                for _ in range(digit_index):
                    model.handle_input(b"\x1b[B")
                self.assertEqual(digit_index, model.menu_index)
                model.handle_input(b"\r")
                self.assertFalse(model.menu_open)
                ref, ref_sender = make()
                ref.handle_input(P + key.encode())
                self.assertEqual(_effects(ref, ref_sender), _effects(model, sender))

    def test_arrow_wraparound_and_ss3_arrows_and_newline_enter(self):
        model, sender = self.open()
        model.handle_input(b"\x1b[A")
        self.assertEqual(len(MENU_ORDER) - 1, model.menu_index)  # wraps to detach
        model.handle_input(b"\x1bOB")
        self.assertEqual(0, model.menu_index)
        model.handle_input(b"\x1bOB\x1b[B\x1b[A")
        self.assertEqual(1, model.menu_index)
        model.handle_input(b"\n")  # zoom
        self.assertIsNotNone(model.zoom)

    def test_digits_select_and_run_immediately(self):
        for digit, key in zip(b"1234567890", MENU_ORDER):
            with self.subTest(digit=chr(digit), key=key):
                model, sender = self.open()
                model.handle_input(bytes([digit]))
                self.assertFalse(model.menu_open)
                ref, ref_sender = make()
                ref.handle_input(P + key.encode())
                self.assertEqual(_effects(ref, ref_sender), _effects(model, sender))
                self.assertEqual([], sender.of("input"))

    def test_esc_cancels_and_nothing_is_sent(self):
        model, sender = self.open()
        model.handle_input(b"\x1b", now=0.0)
        model.flush_input(now=0.5)
        self.assertFalse(model.menu_open)
        self.assertEqual([], sender.sent)
        self.assertFalse(model.quit)

    def test_prefix_cancels_menu_without_running_the_following_key(self):
        for follow in (b" ", b"d", b"q", CTRL("d"), CTRL("q"), b"z"):
            with self.subTest(follow=follow):
                model, sender = self.open()
                model.handle_input(P + follow)
                self.assertFalse(model.menu_open)
                self.assertFalse(model.quit)
                self.assertIsNone(model.zoom)
                self.assertEqual([], sender.sent)
        model, sender = self.open()
        model.handle_input(P + P)  # literal-prefix form cancels too and is not forwarded
        self.assertFalse(model.menu_open)
        self.assertEqual([], sender.sent)

    def test_no_key_reaches_a_pane_while_the_menu_is_open(self):
        model, sender = self.open()
        for data in (b"hello", b"\x03", b"\t", b"\x7f", b"\x1b[5~", b"\x1b[6;2~", "한글".encode(), b"jk", b"\x1bx",
                     b"\x1b[<64;10;5M", b"\x1b[200~paste\x1b[201~"):
            model.handle_input(data, now=0.0)
            model.flush_input(now=1.0)
            self.assertTrue(model.menu_open, data)
        self.assertEqual([], sender.sent)
        self.assertEqual(0, model.menu_index)
        self.assertIn("메뉴", model.notice or model.footer())

    def test_focus_and_scroll_state_untouched_by_menu_open_close(self):
        model, sender = make()
        model.handle_input(P + b"3")
        sender.sent.clear()
        model.handle_input(P + b" ")
        model.handle_input(b"\x1b", now=0.0)
        model.flush_input(now=1.0)
        self.assertEqual(PaneId.HOST_SHELL, model.focus)
        self.assertEqual([], sender.sent)

    def test_menu_stays_ime_neutral_help_item_opens_help_and_footer_lists_menu(self):
        model, sender = self.open()
        model.handle_input(b"9")
        self.assertTrue(model.help_open)
        text = "\n".join(HELP_LINES)
        for needle in ("Ctrl-q", "Ctrl-t", "Ctrl-y", "Ctrl-o", "Ctrl-r", "Ctrl-e", "Ctrl-z", "Space", "한글"):
            self.assertIn(needle, text)
        closed, _ = make()
        self.assertIn("Space", closed.footer())
        closed.handle_input(P)
        self.assertIn("Ctrl", closed.footer())
        self.assertIn("Space", closed.footer())
        self.assertIn("←↑↓→", closed.footer())
        self.assertIn("[", closed.footer())

    def test_reopening_menu_resets_selection(self):
        model, _ = self.open()
        model.handle_input(b"\x1b[B\x1b[B")
        model.handle_input(b"\x1b", now=0.0)
        model.flush_input(now=1.0)
        model.handle_input(P + b" ")
        self.assertEqual(0, model.menu_index)


class DetachKeyTests(unittest.TestCase):
    """C-D61: detach is prefix q / Ctrl-q / menu 0 (like herdr); the old d / Ctrl-d never detach."""

    def test_every_detach_form_quits_and_sends_nothing(self):
        forms = {"q": P + b"q", "Q": P + b"Q", "Ctrl-q": P + b"\x11", "ㅂ Space": P + "ㅂ".encode() + b" ",
                 "ㅃ Space": P + "ㅃ".encode() + b" "}
        for name, data in forms.items():
            with self.subTest(form=name):
                model, sender = make()
                model.handle_input(data, now=0.0)
                self.assertTrue(model.quit)
                self.assertEqual([], sender.sent)
        model, sender = make()
        model.handle_input(P + b" ")
        model.handle_input(b"0")
        self.assertTrue(model.quit)
        self.assertEqual([], sender.sent)

    def test_old_detach_keys_do_not_detach_send_nothing_and_explain(self):
        forms = {"d": P + b"d", "D": P + b"D", "Ctrl-d": P + b"\x04", "ㅇ Space": P + "ㅇ".encode() + b" "}
        for name, data in forms.items():
            with self.subTest(form=name):
                model, sender = make()
                model.handle_input(data, now=0.0)
                self.assertFalse(model.quit)
                self.assertEqual([], sender.sent)
                self.assertIn("Ctrl-] q (Ctrl-q)", model.notice)
                self.assertIn("detach", model.notice)
                self.assertFalse(model.parser.prefix_active)
        for pane_key in (b"3", b"2"):  # also from another focus (host shell): nothing is sent to the pane
            model, sender = make()
            model.handle_input(P + pane_key)
            sender.sent.clear()
            model.handle_input(P + b"\x04")
            self.assertEqual([], sender.sent)
            self.assertFalse(model.quit)

    def test_prefix_q_only_applies_right_after_the_prefix_and_scroll_q_still_exits_scroll(self):
        model, sender = make()
        model.handle_input(b"q")  # a plain q is text for the pane
        self.assertFalse(model.quit)
        self.assertEqual([b"q"], [s[2] for s in sender.of("input")])
        model.handle_input(P + b"[")
        self.assertIsNotNone(model.scroll_pane)
        model.handle_input(b"q")  # scroll mode: q leaves scroll mode, does not detach
        self.assertIsNone(model.scroll_pane)
        self.assertFalse(model.quit)

    def test_texts_name_q_not_d(self):
        model, _ = make()
        self.assertIn("Ctrl-] q/Ctrl-q = detach", model.footer())
        self.assertNotIn("Ctrl-d", model.footer())
        text = "\n".join(HELP_LINES)
        self.assertIn("prefix q        detach", text)
        self.assertNotIn("prefix d ", text)
        self.assertIn("ㅂ Space = q", text)
        self.assertNotIn("ㅇ Space = d", text)
        self.assertIn("ㅂ Space = q", HANGUL_HINT)
        self.assertIn("Ctrl-q", HANGUL_HINT)
        model.handle_input(P)
        self.assertIn("Ctrl+q", model.footer())
        self.assertIn("영문 q", model.footer())


class CtrlDGuardTests(unittest.TestCase):
    """A raw Ctrl-d (0x04) to an OMP pane needs a second press within CTRL_D_CONFIRM_SECONDS (OMP exits on Ctrl-d)."""

    def inputs(self, sender):
        return [s[2] for s in sender.of("input")]

    def test_single_ctrl_d_is_held_with_a_notice_on_both_omp_panes(self):
        for pane_key, pane in ((b"1", PaneId.MANAGER_OMP), (b"2", PaneId.WORKER_OMP)):
            with self.subTest(pane=pane):
                model, sender = make()
                model.handle_input(P + pane_key, now=0.0)
                sender.sent.clear()
                model.handle_input(b"\x04", now=1.0)
                self.assertEqual([], sender.sent)
                self.assertEqual(CTRL_D_NOTICE, model.notice)
                self.assertIn("2초 안에 Ctrl-d", model.footer())

    def test_second_ctrl_d_within_two_seconds_forwards_exactly_one(self):
        for pane_key, pane in ((b"1", "manager_omp"), (b"2", "worker_omp")):
            with self.subTest(pane=pane):
                model, sender = make()
                model.handle_input(P + pane_key, now=0.0)
                sender.sent.clear()
                model.handle_input(b"\x04", now=1.0)
                model.handle_input(b"\x04", now=2.9)
                self.assertEqual([b"\x04"], self.inputs(sender))
                self.assertEqual(pane, sender.of("input")[0][1]["pane"])
                self.assertEqual("", model.notice)
                model.handle_input(b"\x04", now=3.0)  # a fresh cycle: held again, not forwarded
                self.assertEqual([b"\x04"], self.inputs(sender))
                self.assertEqual(CTRL_D_NOTICE, model.notice)

    def test_alt_ctrl_d_is_one_key_held_and_delivered_whole(self):
        """ESC 0x04 is one key: the first press is held (no lone ESC), the second form pressed is delivered whole."""
        for first, second in ((b"\x1b\x04", b"\x1b\x04"), (b"\x04", b"\x1b\x04"), (b"\x1b\x04", b"\x04")):
            with self.subTest(first=first, second=second):
                model, sender = make()
                model.handle_input(P + b"1", now=0.0)
                sender.sent.clear()
                model.handle_input(first, now=1.0)
                model.flush_input(now=1.5)
                self.assertEqual([], sender.sent)
                self.assertEqual(CTRL_D_NOTICE, model.notice)
                model.handle_input(second, now=1.9)
                model.flush_input(now=2.4)
                self.assertEqual([second], self.inputs(sender))
                self.assertEqual("", model.notice)

    def test_second_ctrl_d_after_the_timeout_is_a_new_first_press(self):
        model, sender = make()
        model.handle_input(b"\x04", now=0.0)
        model.handle_input(b"\x04", now=CTRL_D_CONFIRM_SECONDS + 0.5)
        self.assertEqual([], sender.of("input"))
        self.assertEqual(CTRL_D_NOTICE, model.notice)
        model.handle_input(b"\x04", now=CTRL_D_CONFIRM_SECONDS + 1.0)  # confirms the new first press
        self.assertEqual([b"\x04"], self.inputs(sender))

    def test_timeout_clears_the_pending_state_and_the_notice_on_flush(self):
        model, sender = make()
        model.handle_input(b"\x04", now=0.0)
        self.assertFalse(model.flush_input(now=1.0))
        self.assertEqual(CTRL_D_NOTICE, model.notice)
        self.assertTrue(model.flush_input(now=2.5))  # a redraw is requested
        self.assertEqual("", model.notice)
        model.handle_input(b"\x04", now=2.6)
        self.assertEqual([], sender.of("input"))

    def test_any_other_key_cancels_and_is_handled_normally(self):
        for other in (b"x", b"\r", b"\x03", b"\x1b[A", "한".encode()):
            with self.subTest(key=other):
                model, sender = make()
                model.handle_input(b"\x04", now=0.0)
                model.handle_input(other, now=0.5)
                self.assertEqual([other], self.inputs(sender))  # the other key went through, the Ctrl-d did not
                self.assertEqual("", model.notice)
                model.handle_input(b"\x04", now=1.0)  # cancelled: this is a first press again
                self.assertEqual([other], self.inputs(sender))

    def test_prefix_command_and_paste_cancel(self):
        model, sender = make()
        model.handle_input(b"\x04", now=0.0)
        model.handle_input(P + b"r", now=0.5)  # any command
        model.handle_input(b"\x04", now=1.0)
        self.assertEqual([], sender.of("input"))
        model, sender = make()
        model.handle_input(b"\x04", now=0.0)
        model.handle_input(b"\x1b[200~ab\x1b[201~", now=0.5)
        self.assertEqual(1, len(sender.of("paste")))
        model.handle_input(b"\x04", now=1.0)
        self.assertEqual([], sender.of("input"))
        self.assertEqual(CTRL_D_NOTICE, model.notice)

    def test_focus_change_cancels_and_other_pane_needs_its_own_pair(self):
        model, sender = make()
        model.handle_input(b"\x04", now=0.0)
        model.handle_input(P + b"2", now=0.5)
        model.handle_input(b"\x04", now=1.0)
        self.assertEqual([], sender.of("input"))  # worker: first press
        model.handle_input(P + b"1", now=1.5)
        model.handle_input(b"\x04", now=1.8)  # back on manager within 2 s of the first press: still cancelled
        self.assertEqual([], sender.of("input"))
        model.handle_input(b"\x04", now=1.9)
        self.assertEqual([b"\x04"], self.inputs(sender))

    def test_backend_driven_focus_change_and_click_focus_cancel(self):
        model, sender = make()
        model.handle_input(b"\x04", now=0.0)
        snap = snapshot()
        snap["focus"] = "worker_omp"
        model.apply_snapshot(snap)
        snap["focus"] = "manager_omp"
        model.apply_snapshot(snap)
        model.handle_input(b"\x04", now=0.5)
        self.assertEqual([], sender.of("input"))

    def test_host_shell_ctrl_d_is_sent_immediately_and_cancels_nothing_held_for_omp(self):
        model, sender = make()
        model.handle_input(P + b"3", now=0.0)
        sender.sent.clear()
        model.handle_input(b"\x04", now=0.1)
        self.assertEqual([b"\x04"], self.inputs(sender))
        self.assertEqual("", model.notice)

    def test_paste_containing_ctrl_d_is_unchanged(self):
        model, sender = make()
        frame = b"\x1b[200~a\x04b\x1b[201~"
        model.handle_input(frame, now=0.0)
        pastes = sender.of("paste")
        self.assertEqual(1, len(pastes))
        self.assertEqual(frame, pastes[0][2])
        self.assertEqual([], sender.of("input"))
        self.assertEqual("", model.notice)
        model.handle_input(frame[:6] + b"a\x04", now=1.0)  # a split paste with 0x04 inside: still no guard
        model.handle_input(b"b\x1b[201~", now=1.1)
        self.assertEqual([], sender.of("input"))
        self.assertEqual("", model.notice)

    def test_bytes_around_ctrl_d_in_one_read_are_handled_normally(self):
        model, sender = make()
        model.handle_input(b"ab\x04cd", now=0.0)  # a and b go through, Ctrl-d held then dropped by c, cd go through
        self.assertEqual([b"abcd"], self.inputs(sender))  # one frame: the first Ctrl-d is simply absent
        self.assertEqual("", model.notice)
        model, sender = make()
        model.handle_input(b"ab\x04", now=0.0)
        self.assertEqual([b"ab"], self.inputs(sender))
        self.assertEqual(CTRL_D_NOTICE, model.notice)
        model.handle_input(b"\x04cd", now=1.0)  # confirm, then text
        self.assertEqual([b"ab", b"\x04cd"], self.inputs(sender))
        model, sender = make()
        model.handle_input(b"\x04\x04", now=0.0)  # both in one read: held, then confirmed -> exactly one
        self.assertEqual([b"\x04"], self.inputs(sender))
        model, sender = make()
        model.handle_input(b"\x04\x04\x04", now=0.0)  # held, sent, held again
        self.assertEqual([b"\x04"], self.inputs(sender))
        self.assertEqual(CTRL_D_NOTICE, model.notice)

    def test_split_reads(self):
        model, sender = make()
        model.handle_input(b"x", now=0.0)
        model.handle_input(b"\x04", now=0.1)
        model.handle_input(b"\x04y", now=0.2)
        self.assertEqual([b"x", b"\x04y"], self.inputs(sender))
        # an ESC-prefixed sequence split around the Ctrl-d is not mistaken for it
        model, sender = make()
        model.handle_input(b"\x04", now=0.0)
        model.handle_input(b"\x1b", now=0.1)
        model.flush_input(now=0.3)
        model.handle_input(b"\x04", now=0.4)
        self.assertEqual([b"\x1b"], self.inputs(sender))

    def test_scroll_mode_help_and_menu_never_forward_ctrl_d_and_cancel_a_held_one(self):
        model, sender = make()
        model.handle_input(b"\x04", now=0.0)
        model.handle_input(P + b"[", now=0.5)
        model.handle_input(b"\x04", now=0.6)  # scroll mode: keys drive the view, nothing is sent
        self.assertEqual([], sender.of("input"))
        model.handle_input(b"q", now=0.7)
        model.handle_input(b"\x04", now=0.8)
        self.assertEqual([], sender.of("input"))
        model.handle_input(P + b"?", now=1.0)
        model.handle_input(b"\x04", now=1.1)  # closes the overlay only
        self.assertFalse(model.help_open)
        self.assertEqual([], sender.of("input"))

    def test_mouse_reports_do_not_cancel_a_held_ctrl_d(self):
        model, sender = make()
        model.handle_input(b"\x04", now=0.0)
        model.handle_input(b"\x1b[<35;5;5M", now=0.5)  # motion report over a border/pane
        model.handle_input(b"\x04", now=1.0)
        self.assertEqual([b"\x04"], [s[2] for s in sender.of("input") if s[2] == b"\x04"])

    def test_direct_scrolled_pane_returns_to_live_only_when_something_is_sent(self):
        model, sender = make(30, 100)
        model.on_display(display("manager_omp", numbered(300)))
        model.handle_input(b"\x1b[5;2~", now=0.0)
        self.assertTrue(model.scrolled(PaneId.MANAGER_OMP))
        model.handle_input(b"\x04", now=0.1)  # held: nothing sent, the view stays
        self.assertTrue(model.scrolled(PaneId.MANAGER_OMP))
        model.handle_input(b"\x04", now=0.2)  # delivered: back to live
        self.assertFalse(model.scrolled(PaneId.MANAGER_OMP))



class _Ticker:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def osc52(text, tmux=False):
    import base64
    plain = b"\x1b]52;c;" + base64.b64encode(text.encode()) + b"\x07"
    if not tmux:
        return plain
    return plain + b"\x1bPtmux;" + plain.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"


class DragToCopyTests(unittest.TestCase):
    """C-D62 (2): dragging in a pane that does not track the mouse selects and copies via OSC 52."""

    def setUp(self):
        self.ticker = _Ticker()
        self.sender = FakeSender()
        self.model = ProductModel(self.sender, 30, 120, clock=lambda: 1000.0, monotonic=self.ticker, environ={})
        self.model.apply_snapshot(snapshot())
        for pane in ("manager_omp", "worker_omp", "host_shell"):
            self.model.on_display(display(pane, f"{pane} hello world\r\nsecond row   \r\nthird".encode()))
        self.sender.sent.clear()

    def cell(self, pane, dx=0, dy=0):
        top, left, _, _ = pane_boxes(self.model.rows, self.model.cols)[pane]
        return left + 2 + dx, top + 2 + dy

    def mouse(self, pane, button, dx=0, dy=0, release=False):
        x, y = self.cell(pane, dx, dy)
        self.model.handle_input(sgr(button, x, y, release))

    def drag(self, pane, start, end, *, steps=()):
        self.mouse(pane, 0, *start)
        for point in steps:
            self.mouse(pane, 32, *point)
        self.mouse(pane, 32, *end)
        self.mouse(pane, 0, *end, release=True)

    def test_drag_in_each_pane_type_copies_that_text_as_osc52(self):
        for pane in PANES:
            with self.subTest(pane=pane):
                self.model.take_output()
                self.drag(pane, (0, 0), (len(pane.value) - 1, 0))
                self.assertEqual(osc52(pane.value), self.model.take_output())
                self.assertEqual(b"", self.model.take_output())  # taken once
                self.assertIn("복사됨: ", self.model.notice)

    def test_single_row_selection_is_a_substring_and_end_cell_is_included(self):
        pane = PaneId.WORKER_OMP
        self.drag(pane, (11, 0), (15, 0))
        self.assertEqual(osc52("hello"), self.model.take_output())
        self.assertEqual("복사됨: 5자", self.model.notice)

    def test_backward_drag_is_the_same_selection(self):
        self.drag(PaneId.WORKER_OMP, (15, 0), (11, 0))
        self.assertEqual(osc52("hello"), self.model.take_output())

    def test_rows_joined_with_newline_and_trailing_spaces_trimmed(self):
        pane = PaneId.HOST_SHELL
        self.drag(pane, (5, 0), (4, 2))  # from "hello world" row 0 to "third" row 2
        top = "host_shell hello world".rstrip()[5:]
        self.assertEqual(osc52(f"{top}\nsecond row\nthird"), self.model.take_output())

    def test_wide_characters_are_copied_once(self):
        pane = PaneId.MANAGER_OMP
        self.model.on_display(display("manager_omp", "\x1b[2J\x1b[H한글abc".encode()))
        self.drag(pane, (0, 0), (6, 0))
        self.assertEqual(osc52("한글abc"), self.model.take_output())
        self.drag(pane, (1, 0), (2, 0))  # starts on the trailing half of the first wide character
        self.assertEqual(osc52("한글"), self.model.take_output())

    def test_selection_is_clamped_to_its_pane_and_never_crosses_into_others(self):
        pane = PaneId.MANAGER_OMP
        self.mouse(pane, 0, 0, 0)
        x, y = self.cell(PaneId.WORKER_OMP, 3, 1)  # inside the other pane
        self.model.handle_input(sgr(32, x, y))
        self.model.handle_input(sgr(0, x, y, release=True))
        out = self.model.take_output()
        self.assertIn(b"\x1b]52;c;", out)
        import base64
        copied = base64.b64decode(out[len(b"\x1b]52;c;"):-1]).decode()
        self.assertTrue(copied.startswith("manager_omp hello world"))
        self.assertNotIn("worker", copied)
        self.assertIn("second row", copied)  # ran to the pane's last row/column, not into the neighbour
        spans = self.model.selection_spans(PaneId.WORKER_OMP, 12)
        self.assertEqual({}, spans)

    def test_release_over_a_border_still_copies(self):
        pane = PaneId.HOST_SHELL
        self.mouse(pane, 0, 0, 0)
        x, y = self.cell(pane, 5, 0)
        self.model.handle_input(sgr(32, x, y))
        self.model.handle_input(sgr(0, x, y - 5, release=True))  # far above: clamped to the top row
        self.assertEqual(osc52("host_s"), self.model.take_output())

    def test_click_without_movement_focuses_and_copies_nothing(self):
        self.mouse(PaneId.WORKER_OMP, 0, 3, 0)
        self.mouse(PaneId.WORKER_OMP, 0, 3, 0, release=True)
        self.assertIs(PaneId.WORKER_OMP, self.model.focus)
        self.assertEqual([("focus", {"pane": "worker_omp"}, b"")], self.sender.sent)
        self.assertEqual(b"", self.model.take_output())
        self.assertEqual({}, self.model.selection_spans(PaneId.WORKER_OMP, 12))

    def test_press_in_the_focused_pane_keeps_focus_untouched(self):
        self.mouse(PaneId.MANAGER_OMP, 0, 3, 0)
        self.mouse(PaneId.MANAGER_OMP, 0, 3, 0, release=True)
        self.assertEqual([], self.sender.sent)
        self.assertEqual(b"", self.model.take_output())

    def test_tracking_pane_still_gets_the_mouse_and_never_selects(self):
        self.model.on_display(display("worker_omp", b"\x1b[?1002h\x1b[?1006h"))
        self.sender.sent.clear()
        self.drag(PaneId.WORKER_OMP, (0, 0), (5, 0))
        self.assertEqual([b"\x1b[<0;1;1M", b"\x1b[<32;6;1M", b"\x1b[<0;6;1m"], [d for _, _, d in self.sender.sent])
        self.assertEqual(b"", self.model.take_output())
        self.assertEqual({}, self.model.selection_spans(PaneId.WORKER_OMP, 12))

    def test_highlight_spans_while_dragging_and_after_release(self):
        pane = PaneId.MANAGER_OMP
        self.mouse(pane, 0, 2, 0)
        self.assertEqual({}, self.model.selection_spans(pane, 12))  # not moved yet
        self.mouse(pane, 32, 4, 1)
        last = self.model.sizes[pane][1] - 1
        self.assertEqual({0: (2, last), 1: (0, 4)}, self.model.selection_spans(pane, 12))
        self.mouse(pane, 0, 4, 1, release=True)
        self.assertEqual({0: (2, last), 1: (0, 4)}, self.model.selection_spans(pane, 12))

    def test_highlight_ends_with_a_key_a_click_a_new_selection_or_a_focus_change(self):
        pane = PaneId.MANAGER_OMP

        def select():
            self.drag(pane, (0, 0), (3, 0))
            self.assertEqual({0: (0, 3)}, self.model.selection_spans(pane, 12))

        select()
        self.model.handle_input(b"x")
        self.assertEqual({}, self.model.selection_spans(pane, 12))
        select()
        self.mouse(pane, 0, 9, 1)  # any press
        self.assertEqual({}, self.model.selection_spans(pane, 12))
        self.mouse(pane, 0, 9, 1, release=True)
        select()
        self.drag(pane, (5, 1), (8, 1))  # a new selection replaces it
        self.assertEqual({1: (5, 8)}, self.model.selection_spans(pane, 12))
        self.model.handle_input(P + b"2")  # focus change
        self.assertEqual({}, self.model.selection_spans(pane, 12))
        self.mouse(pane, 0, 0, 0)
        self.mouse(pane, 32, 4, 0)
        self.mouse(pane, 0, 4, 0, release=True)
        self.model.handle_input(P + b"m")  # a prefix command is a key too
        self.assertEqual({}, self.model.selection_spans(pane, 12))

    def test_highlight_ends_when_the_pane_content_is_reset(self):
        pane = PaneId.MANAGER_OMP
        self.drag(pane, (0, 0), (3, 0))
        self.assertEqual({0: (0, 3)}, self.model.selection_spans(pane, 12))
        self.model.on_display(display("manager_omp", b"fresh", gen=2))  # the pane's process was replaced
        self.assertEqual({}, self.model.selection_spans(pane, 12))
        self.drag(pane, (0, 0), (3, 0))
        self.model.on_display(display("manager_omp", b"\x1b[?1049h"))  # alternate screen swaps the history
        self.assertEqual({}, self.model.selection_spans(pane, 12))

    def test_wheel_does_not_end_the_selection_and_it_stays_anchored_to_the_content(self):
        pane = PaneId.MANAGER_OMP
        self.model.on_display(display("manager_omp", numbered(120)))
        self.drag(pane, (0, 0), (9, 0))
        first = self.model.take_output()
        self.assertEqual({0: (0, 9)}, self.model.selection_spans(pane, 12))
        self.mouse(pane, 64)  # wheel up three lines: the highlight follows the content down
        self.assertEqual({3: (0, 9)}, self.model.selection_spans(pane, 12))
        self.assertTrue(first)

    def test_selection_past_output_after_scrolling_back(self):
        pane = PaneId.MANAGER_OMP
        self.model.on_display(display("manager_omp", numbered(120)))
        for _ in range(4):
            self.mouse(pane, 64)  # 12 lines back
        rows = visible(self.model, pane)
        self.drag(pane, (0, 0), (9, 1))
        self.assertEqual(osc52(rows[0] + "\n" + rows[1]), self.model.take_output())
        self.assertNotIn("line 00119", rows[0])

    def test_drag_above_the_top_edge_autoscrolls_history_and_extends_the_selection(self):
        pane = PaneId.MANAGER_OMP
        self.model.on_display(display("manager_omp", numbered(120)))
        live = visible(self.model, pane)
        self.mouse(pane, 0, 0, 2)
        for _ in range(5):
            self.ticker.now += 0.5
            self.mouse(pane, 32, 3, -1)  # on the top border, above the inner area
        self.mouse(pane, 0, 3, -1, release=True)
        self.assertEqual(5, self.model.scroll_offset(pane))
        self.assertIs(PaneId.MANAGER_OMP, self.model.focus)
        import base64
        out = self.model.take_output()
        copied = base64.b64decode(out[len(b"\x1b]52;c;"):-1]).decode().split("\n")
        view = visible(self.model, pane)  # the scrolled view now starts 5 lines above the old top
        self.assertNotEqual(live[0], view[0])
        # anchor = old row 2 (now view row 7, column 0); cursor = the top row of the view at column 3
        self.assertEqual([view[0][3:]] + view[1:7] + [view[7][:1]], copied)

    def test_drag_below_the_bottom_edge_autoscrolls_towards_live(self):
        pane = PaneId.MANAGER_OMP
        self.model.on_display(display("manager_omp", numbered(120)))
        for _ in range(3):
            self.mouse(pane, 64)  # 9 lines back
        rows = self.model.sizes[pane][0]
        self.mouse(pane, 0, 0, 0)
        for _ in range(4):
            self.ticker.now += 0.5
            self.mouse(pane, 32, 3, rows)  # bottom border
        self.mouse(pane, 0, 3, rows, release=True)
        self.assertEqual(5, self.model.scroll_offset(pane))
        self.assertTrue(self.model.take_output())

    def test_autoscroll_is_rate_limited_and_continues_from_the_loop_tick(self):
        pane = PaneId.MANAGER_OMP
        self.model.on_display(display("manager_omp", numbered(120)))
        self.mouse(pane, 0, 0, 2)
        self.mouse(pane, 32, 3, -1)
        self.mouse(pane, 32, 3, -1)  # same instant: no second step
        self.assertEqual(1, self.model.scroll_offset(pane))
        self.assertFalse(self.model.autoscroll_tick())
        self.ticker.now += 0.5
        self.assertTrue(self.model.autoscroll_tick())
        self.assertEqual(2, self.model.scroll_offset(pane))
        self.mouse(pane, 32, 3, 2)  # back inside: no further scrolling
        self.ticker.now += 0.5
        self.assertFalse(self.model.autoscroll_tick())

    def test_osc52_gets_a_tmux_passthrough_copy_only_when_tmux_is_set(self):
        for environ, tmux in (({}, False), ({"TMUX": "/tmp/tmux-1/default,1,0"}, True)):
            with self.subTest(tmux=tmux):
                model = ProductModel(self.sender, 30, 120, clock=lambda: 1000.0, monotonic=self.ticker, environ=environ)
                model.apply_snapshot(snapshot())
                model.on_display(display("manager_omp", b"abc"))
                self.mouse_on(model, 0, 0, 0)
                self.mouse_on(model, 32, 2, 0)
                self.mouse_on(model, 0, 2, 0, release=True)
                self.assertEqual(osc52("abc", tmux), model.take_output())
                self.assertEqual(tmux, "tmux" in model.notice)
                self.assertIn("복사됨: 3자", model.notice)
                if tmux:
                    self.assertIn("set-clipboard on 또는 allow-passthrough on", model.notice)

    def test_environment_tmux_is_read_when_no_environ_is_given(self):
        import os
        from unittest import mock
        model = ProductModel(self.sender, 30, 120, clock=lambda: 1000.0, monotonic=self.ticker)
        model.on_display(display("manager_omp", b"abc"))
        with mock.patch.dict(os.environ, {"TMUX": "x,1,0"}):
            self.mouse_on(model, 0, 0, 0)
            self.mouse_on(model, 32, 2, 0)
            self.mouse_on(model, 0, 2, 0, release=True)
        self.assertEqual(osc52("abc", True), model.take_output())

    def mouse_on(self, model, button, dx, dy, release=False):
        top, left, _, _ = pane_boxes(model.rows, model.cols)[PaneId.MANAGER_OMP]
        model.handle_input(sgr(button, left + 2 + dx, top + 2 + dy, release))

    def test_copy_over_one_mebibyte_is_refused_with_a_notice(self):
        from workbench.ui.product import model as model_module
        with mock_patch(model_module, "MAX_COPY_BYTES", 8):
            self.drag(PaneId.MANAGER_OMP, (0, 0), (8, 0))  # 9 bytes
            self.assertEqual(b"", self.model.take_output())
            self.assertIn("1 MiB", self.model.notice)
            self.drag(PaneId.MANAGER_OMP, (0, 0), (7, 0))  # exactly 8 bytes is fine
            self.assertEqual(osc52("manager_"), self.model.take_output())

    def test_cap_constant_is_one_mebibyte(self):
        from workbench.ui.product import model as model_module
        self.assertEqual(1024 * 1024, model_module.MAX_COPY_BYTES)

    def test_blank_selection_copies_nothing(self):
        self.drag(PaneId.MANAGER_OMP, (40, 5), (50, 5))
        self.assertEqual(b"", self.model.take_output())
        self.assertIn("비어", self.model.notice)

    def test_divider_drag_and_wheel_and_capture_off_are_unchanged(self):
        self.model.handle_input(P + b"m")
        self.drag(PaneId.MANAGER_OMP, (0, 0), (5, 0))
        self.assertEqual(b"", self.model.take_output())  # capture off: the terminal's own selection
        self.model.handle_input(P + b"m")
        before = self.model.layout
        _, left, _, width = pane_boxes(self.model.rows, self.model.cols)[PaneId.MANAGER_OMP]
        top = pane_boxes(self.model.rows, self.model.cols)[PaneId.MANAGER_OMP][0]
        self.model.handle_input(sgr(0, width, top + 3))  # on the vertical divider
        self.model.handle_input(sgr(32, width + 6, top + 3))
        self.model.handle_input(sgr(0, width + 6, top + 3, release=True))
        self.assertNotEqual(before, self.model.layout)
        self.assertEqual(b"", self.model.take_output())

    def test_help_gets_one_short_drag_to_copy_line(self):
        self.assertTrue(any("드래그" in ln and "복사" in ln for ln in HELP_LINES))
        self.assertLessEqual(len(HELP_LINES), HELP_LINES_BEFORE_COPY + 4)  # + exited-pane Enter (C-D62) + host kill/restart (C-D63) + pause/resume (CW-18 U5)

    def test_trimmed_history_rows_are_never_copied_and_an_empty_selection_is_cancelled(self):
        pane = PaneId.MANAGER_OMP
        self.model.on_display(display("manager_omp", "".join(f"\r\nold {i:04d}" for i in range(1000)).encode()))
        self.model.scroll_by(10_000, pane)
        self.mouse(pane, 0, 0, 0)
        self.mouse(pane, 32, 7, 1)
        self.model.on_display(display("manager_omp", "".join(f"\r\nflood {i:04d}" for i in range(600)).encode()))
        self.mouse(pane, 32, 7, 1)
        self.mouse(pane, 0, 7, 1, release=True)
        self.assertEqual(b"", self.model.take_output())
        self.assertIn("밀려나", self.model.notice)
        self.assertEqual({}, self.model.selection_spans(pane, 10))

    def test_partly_trimmed_selection_is_clamped_to_the_rows_that_remain(self):
        from workbench.ui.product.model import _Selection
        pane = PaneId.MANAGER_OMP
        self.model.on_display(display("manager_omp", "".join(f"\r\nold {i:04d}" for i in range(1000)).encode()))
        queue = self.model._history(pane)
        base = queue.pushed - len(queue)
        sel = _Selection(pane, queue, (base - 3, 5), (base + 1, 2), dragging=False,
                         resets=self.model.panes[pane].screen.resets)
        self.model._sel = sel
        clamped = self.model._selection()
        self.assertIs(sel, clamped)
        self.assertEqual(((base, 0), (base + 1, 2)), clamped.span())

    def test_full_terminal_reset_clears_the_selection_and_highlight(self):
        pane = PaneId.HOST_SHELL
        self.drag(pane, (0, 0), (8, 1))
        self.assertNotEqual({}, self.model.selection_spans(pane, 10))
        self.model.on_display(display("host_shell", b"\x1bc"))
        self.assertEqual({}, self.model.selection_spans(pane, 10))

    def test_footer_notice_and_help_name_drag_to_copy_first_and_keep_shift_drag(self):
        self.assertIn("드래그: 복사", self.model.footer())
        self.assertIn("Shift+드래그", self.model.footer())
        line = next(ln for ln in HELP_LINES if "prefix m" in ln)
        self.assertIn("드래그: 복사", line)
        self.assertIn("Shift+드래그", line)
        self.model.handle_input(P + b"m")
        self.model.handle_input(P + b"m")
        self.assertIn("드래그: 복사", self.model.notice)
        self.assertIn("Shift+드래그", self.model.notice)


HELP_LINES_BEFORE_COPY = 30


class mock_patch:
    def __init__(self, module, name, value):
        self.module, self.name, self.value = module, name, value

    def __enter__(self):
        self.old = getattr(self.module, self.name)
        setattr(self.module, self.name, self.value)

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.old)


class RestartExitedOmpTests(unittest.TestCase):
    """C-D62 (1): Enter in an exited manager/worker OMP pane restarts it as a new session; nothing else reaches it."""

    def exited(self, *dead, generation=1):
        model, sender = make()
        snap = snapshot(alive=tuple(p for p in ("manager_omp", "worker_omp", "host_shell") if p not in dead))
        for name in dead:
            snap["panes"][name]["exit_status"] = 0
        for info in snap["panes"].values():
            info["generation"] = generation
        model.apply_snapshot(snap)
        return model, sender

    def alive_again(self, model, pane, generation=2):
        snap = snapshot()
        for info in snap["panes"].values():
            info["generation"] = generation
        model.apply_snapshot(snap)

    def restarts(self, sender):
        return sender.of("restart_pane")

    def test_notice_text_title_and_wrapping_for_an_exited_pane(self):
        model, _ = self.exited("manager_omp")
        notice = model.restart_notice(PaneId.MANAGER_OMP)
        self.assertEqual("OMP 종료됨 (exit 0) — Enter: 새 세션으로 다시 시작 · 이전 대화는 새 OMP에서 /resume", notice)
        self.assertIn("종료됨", model.pane_title(PaneId.MANAGER_OMP))
        self.assertIn("exited(0)", model.pane_title(PaneId.MANAGER_OMP))
        self.assertIsNone(model.restart_notice(PaneId.WORKER_OMP))
        self.assertIsNone(model.restart_notice(PaneId.HOST_SHELL))
        self.assertNotIn("종료됨", model.pane_title(PaneId.WORKER_OMP))
        lines = model.restart_notice_lines(PaneId.MANAGER_OMP, 24)
        self.assertGreater(len(lines), 1)
        self.assertEqual(notice.split(), " ".join(lines).split())
        from wcwidth import wcswidth
        self.assertTrue(all(wcswidth(line) <= 24 for line in lines), lines)
        self.assertEqual([], model.restart_notice_lines(PaneId.WORKER_OMP, 24))
        unknown, _ = self.exited("worker_omp")
        unknown.state["panes"]["worker_omp"]["exit_status"] = None
        self.assertIn("exit ?", unknown.restart_notice(PaneId.WORKER_OMP))

    def test_notice_is_drawn_and_the_last_screen_stays(self):
        model, _ = self.exited("manager_omp")
        model.on_display(display("manager_omp", b"last words"))
        self.assertIn("last words", text(model, PaneId.MANAGER_OMP))  # the old screen is kept

    def test_enter_sends_exactly_one_restart_for_that_pane_and_shows_pending(self):
        for data in (b"\r", b"\n", b"ab\rcd", b"\r\r"):
            with self.subTest(data=data):
                model, sender = self.exited("manager_omp")
                sender.sent.clear()
                model.handle_input(data)
                self.assertEqual([("restart_pane", {"pane": "manager_omp"}, b"")], self.restarts(sender))
                self.assertEqual([], sender.of("input"))
                self.assertEqual("다시 시작 중…", model.notice)
                self.assertEqual("다시 시작 중…", model.restart_notice(PaneId.MANAGER_OMP))
                self.assertIn("다시 시작 중…", model.footer())

    def test_worker_pane_restarts_when_focused(self):
        model, sender = self.exited("worker_omp")
        model.handle_input(P + b"2")
        sender.sent.clear()
        model.handle_input(b"\r")
        self.assertEqual([("restart_pane", {"pane": "worker_omp"}, b"")], self.restarts(sender))

    def test_other_keys_and_pastes_are_not_sent_and_the_notice_explains_enter(self):
        model, sender = self.exited("manager_omp")
        sender.sent.clear()
        model.handle_input(b"hello \x1b[A\x04\x03")
        self.assertEqual([], sender.sent)
        self.assertIn("Enter", model.notice)
        model.notice = ""
        model.handle_input(b"\x1b[200~pasted\r\x1b[201~")
        self.assertEqual([], sender.sent, "a paste, even one containing Enter, never restarts or is sent")
        self.assertIn("Enter", model.notice)

    def test_no_pane_bytes_leave_via_mouse_or_queries_for_an_exited_pane(self):
        model, sender = self.exited("manager_omp")
        model.panes[PaneId.MANAGER_OMP].modes.add(1049)  # alt screen: the wheel would become arrow keys
        sender.sent.clear()
        model.handle_input(b"\x1b[<64;5;5M")
        model.on_display(display("manager_omp", b"\x1b[6n"))  # a terminal query the old screen answers
        self.assertEqual([], sender.of("input"))
        self.assertEqual({}, {k: v for k, v in model.pending.items() if v[0].value == "input"})

    def test_repeated_enter_while_pending_is_ignored(self):
        model, sender = self.exited("manager_omp")
        sender.sent.clear()
        model.handle_input(b"\r")
        model.handle_input(b"\r")
        model.handle_input(b"x\n")
        self.assertEqual(1, len(self.restarts(sender)))
        model.on_result({"id": "r1", "ok": True})  # accepted, but the snapshot still shows the old exited pane
        model.handle_input(b"\r")
        self.assertEqual(1, len(self.restarts(sender)))
        self.assertEqual("다시 시작 중…", model.restart_notice(PaneId.MANAGER_OMP))

    def test_ok_keeps_the_notice_until_the_new_generation_arrives_then_clears_it(self):
        model, sender = self.exited("manager_omp")
        model.on_display(display("manager_omp", b"old session"))
        sender.sent.clear()
        model.handle_input(b"\r")
        rid = f"r{sender.count}"
        model.on_result({"id": rid, "ok": True, "pane": "manager_omp", "restarted": True, "generation": 2})
        self.assertEqual("다시 시작 중…", model.restart_notice(PaneId.MANAGER_OMP))
        self.alive_again(model, "manager_omp")
        self.assertIsNone(model.restart_notice(PaneId.MANAGER_OMP))
        self.assertEqual("", model.notice)
        self.assertNotIn("종료됨", model.pane_title(PaneId.MANAGER_OMP))
        model.on_display(display("manager_omp", b"new session", gen=2))
        self.assertIn("new session", text(model, PaneId.MANAGER_OMP))
        self.assertNotIn("old session", text(model, PaneId.MANAGER_OMP))  # the generation reset cleared the screen
        sender.sent.clear()
        model.handle_input(b"typed")  # a live pane again: input flows
        self.assertEqual([b"typed"], [s[2] for s in sender.of("input")])
        self.assertEqual([], self.restarts(sender))

    def test_a_newer_generation_ends_the_pending_state_even_before_alive(self):
        model, sender = self.exited("manager_omp", generation=1)
        model.handle_input(b"\r")
        snap = snapshot(alive=("worker_omp", "host_shell"))
        for info in snap["panes"].values():
            info["generation"] = 2
        snap["panes"]["manager_omp"]["exit_status"] = 0
        model.apply_snapshot(snap)
        self.assertNotIn(PaneId.MANAGER_OMP, model._restarting)

    def test_refusals_show_in_the_footer_and_allow_another_enter(self):
        for reason, detail in (("pane_alive", "still running"), ("restart_in_progress", "busy"),
                               ("restart_failed", "could not start a new manager OMP: out of ptys"),
                               ("pane_not_restartable", "no")):
            with self.subTest(reason=reason):
                model, sender = self.exited("manager_omp")
                sender.sent.clear()
                model.handle_input(b"\r")
                model.on_result({"id": f"r{sender.count}", "ok": False, "reason": reason, "detail": detail})
                self.assertIn(reason, model.footer())
                self.assertIn(detail, model.footer())
                self.assertIn("MANAGER OMP", model.footer())
                self.assertEqual("OMP 종료됨 (exit 0) — Enter: 새 세션으로 다시 시작 · 이전 대화는 새 OMP에서 /resume",
                                 model.restart_notice(PaneId.MANAGER_OMP))
                model.handle_input(b"\r")
                self.assertEqual(2, len(self.restarts(sender)))

    def test_live_panes_host_shell_and_ctrl_d_guard_are_unchanged(self):
        model, sender = self.exited("worker_omp")  # manager focused and alive
        sender.sent.clear()
        model.handle_input(b"\r")
        model.handle_input(b"x")
        self.assertEqual([b"\r", b"x"], [s[2] for s in sender.of("input")])
        self.assertEqual([], self.restarts(sender))
        model.handle_input(b"\x04", now=1.0)
        self.assertEqual(CTRL_D_NOTICE, model.notice)
        model.handle_input(P + b"3")
        sender.sent.clear()
        model.handle_input(b"\r")
        self.assertEqual([("input", {"pane": "host_shell"}, b"\r")], sender.of("input"))
        self.assertEqual([], self.restarts(sender))

    def test_enter_is_not_reachable_from_the_menu_and_help_lists_the_line(self):
        self.assertEqual(1, sum("Enter = 새 OMP 세션으로 다시 시작" in line for line in HELP_LINES))
        model, sender = self.exited("manager_omp")
        model.handle_input(P + b" ")
        self.assertTrue(model.menu_open)
        sender.sent.clear()
        model.handle_input(b"\x1b")  # menu closed; Enter inside the menu runs a menu item, never a restart
        model.handle_input(P + b" ")
        model.handle_input(b"\r")  # first item = scroll mode
        self.assertEqual([], self.restarts(sender))

    def test_pane_scroll_mode_enter_does_not_restart(self):
        model, sender = self.exited("manager_omp")
        model.handle_input(P + b"[")
        sender.sent.clear()
        model.handle_input(b"\r")
        self.assertEqual([], self.restarts(sender))

    def test_help_overlay_keeps_key_swallowing_enter(self):
        model, sender = self.exited("manager_omp")
        model.handle_input(P + b"?")
        sender.sent.clear()
        model.handle_input(b"\r")  # closes the overlay only
        self.assertFalse(model.help_open)
        self.assertEqual([], self.restarts(sender))

    def test_display_of_a_new_session_makes_the_pane_live_before_the_next_state_push(self):
        model, sender = self.exited("manager_omp")
        model.state["panes"]["manager_omp"]["session_id"] = "old-session"
        model.handle_input(b"\r")
        self.assertTrue(model.pane_exited(PaneId.MANAGER_OMP))
        model.on_display(ui_v1.Frame({"pane": "manager_omp", "session_id": "old-session", "generation": 1}, b"late"))
        self.assertTrue(model.pane_exited(PaneId.MANAGER_OMP), "output of the old session must not revive the pane")
        model.on_display(ui_v1.Frame({"pane": "manager_omp", "session_id": "new-session", "generation": 2}, b"OMP"))
        self.assertFalse(model.pane_exited(PaneId.MANAGER_OMP))
        self.assertIsNone(model.restart_notice(PaneId.MANAGER_OMP))
        sender.sent.clear()
        model.handle_input(b"x")
        self.assertEqual(["input"], [k for k, *_ in [(m[0],) for m in sender.sent]])
        model.on_display(ui_v1.Frame({"pane": "manager_omp", "session_id": "new-session", "generation": 2}, b"\x1b[6n"))
        self.assertEqual(2, len(sender.of("input")), "the new session's DSR query was not answered")

    def test_queued_display_of_a_new_session_also_makes_the_pane_live(self):
        model, _ = self.exited("worker_omp")
        model.state["panes"]["worker_omp"]["session_id"] = "old-session"
        model.enqueue_display(ui_v1.Frame({"pane": "worker_omp", "session_id": "new", "generation": 2}, b"OMP"))
        self.assertFalse(model.pane_exited(PaneId.WORKER_OMP))


def host_exited(generation=1, *, focus_host=True, owner="user", exit_status=0, **host_extra):
    """A model whose host shell exited (snapshot ``alive`` false), host focused, nothing sent yet."""
    model, sender = make()
    snap = snapshot(alive=("manager_omp", "worker_omp"), owner=owner)
    snap["panes"]["host_shell"]["exit_status"] = exit_status
    snap["panes"]["host_shell"].update(host_extra)
    for info in snap["panes"].values():
        info["generation"] = generation
    model.apply_snapshot(snap)
    if focus_host:
        model.handle_input(P + b"3")
    sender.sent.clear()
    return model, sender


class RestartExitedHostShellTests(unittest.TestCase):
    """C-D63 (1): an exited host shell behaves like an exited OMP pane; Enter restarts it, nothing else reaches it."""

    NOTICE = "host terminal 종료됨 (exit 0) — Enter: 새 shell 시작"

    def test_notice_title_and_last_screen(self):
        model, _ = host_exited()
        model.on_display(display("host_shell", b"last prompt $"))
        self.assertEqual(self.NOTICE, model.restart_notice(PaneId.HOST_SHELL))
        self.assertTrue(model.pane_exited(PaneId.HOST_SHELL))
        self.assertIn("종료됨", model.pane_title(PaneId.HOST_SHELL))
        self.assertIn("exited(0)", model.pane_title(PaneId.HOST_SHELL))
        self.assertIn("last prompt", text(model, PaneId.HOST_SHELL))
        self.assertIsNone(model.restart_notice(PaneId.MANAGER_OMP))
        self.assertEqual(self.NOTICE.split(), " ".join(model.restart_notice_lines(PaneId.HOST_SHELL, 30)).split())
        unknown, _ = host_exited(exit_status=None)
        self.assertIn("exit ?", unknown.restart_notice(PaneId.HOST_SHELL))

    def test_enter_sends_exactly_one_restart_host_shell_and_dedupes(self):
        for data in (b"\r", b"\n", b"ab\rcd"):
            with self.subTest(data=data):
                model, sender = host_exited()
                model.handle_input(data)
                self.assertEqual([("restart_pane", {"pane": "host_shell"}, b"")], sender.of("restart_pane"))
                self.assertEqual([], sender.of("input"))
                self.assertEqual("다시 시작 중…", model.restart_notice(PaneId.HOST_SHELL))
                model.handle_input(b"\r")
                model.handle_input(b"x\n")
                self.assertEqual(1, len(sender.of("restart_pane")))
                model.on_result({"id": "r1", "ok": True, "pane": "host_shell", "input_owner": "user"})
                model.handle_input(b"\r")  # accepted, snapshot still exited: still pending
                self.assertEqual(1, len(sender.of("restart_pane")))

    def test_other_keys_pastes_mouse_and_queries_reach_nothing(self):
        model, sender = host_exited()
        model.panes[PaneId.HOST_SHELL].modes.add(1049)
        model.handle_input(b"hello \x1b[A\x04\x03")
        self.assertEqual([], sender.sent)
        self.assertIn("Enter", model.notice)
        self.assertIn("host terminal", model.notice)
        model.notice = ""
        model.handle_input(b"\x1b[200~pasted\r\x1b[201~")
        self.assertEqual([], sender.sent)
        self.assertIn("Enter", model.notice)
        model.handle_input(b"\x1b[<64;5;25M")  # wheel over the host pane (alt screen would send arrows)
        model.on_display(display("host_shell", b"\x1b[6n"))
        self.assertEqual([], sender.of("input"))
        self.assertEqual([], sender.of("paste"))

    def test_enter_is_a_normal_key_when_another_pane_is_focused(self):
        model, sender = host_exited(focus_host=False)
        model.handle_input(b"\r")
        self.assertEqual([], sender.of("restart_pane"))
        self.assertEqual([b"\r"], [s[2] for s in sender.of("input")])  # manager OMP is alive

    def test_state_push_with_a_live_shell_revives_the_pane(self):
        model, sender = host_exited(generation=1)
        model.handle_input(b"\r")
        snap = snapshot()
        for info in snap["panes"].values():
            info["generation"] = 2
        model.apply_snapshot(snap)
        self.assertFalse(model.pane_exited(PaneId.HOST_SHELL))
        self.assertIsNone(model.restart_notice(PaneId.HOST_SHELL))
        self.assertEqual("", model.notice)
        sender.sent.clear()
        model.handle_input(b"ls\r")
        self.assertEqual([b"ls\r"], [s[2] for s in sender.of("input")])
        self.assertEqual([], sender.of("restart_pane"))

    def test_new_generation_output_revives_before_the_state_push_and_old_output_does_not(self):
        model, sender = host_exited(generation=1)
        model.state["panes"]["host_shell"]["session_id"] = "old"
        model.handle_input(b"\r")
        model.on_display(ui_v1.Frame({"pane": "host_shell", "session_id": "old", "generation": 1}, b"late"))
        self.assertTrue(model.pane_exited(PaneId.HOST_SHELL))
        model.on_display(ui_v1.Frame({"pane": "host_shell", "session_id": "new", "generation": 2}, b"$ "))
        self.assertFalse(model.pane_exited(PaneId.HOST_SHELL))
        sender.sent.clear()
        model.handle_input(b"x")
        self.assertEqual([b"x"], [s[2] for s in sender.of("input")])
        model.enqueue_display(ui_v1.Frame({"pane": "host_shell", "session_id": "new", "generation": 2}, b"\x1b[6n"))
        model.feed_pending()
        self.assertEqual(2, len(sender.of("input")), "the new shell's DSR query is answered")

    def test_refusals_are_shown_and_allow_another_enter(self):
        for reason, detail in (("pane_alive", "still running"), ("restart_in_progress", "busy"),
                               ("restart_failed", "could not start a new shell"), ("backend_shutdown", "closing")):
            with self.subTest(reason=reason):
                model, sender = host_exited()
                model.handle_input(b"\r")
                model.on_result({"id": f"r{sender.count}", "ok": False, "reason": reason, "detail": detail})
                for needle in (reason, detail, "HOST SHELL"):
                    self.assertIn(needle, model.footer())
                self.assertEqual(self.NOTICE, model.restart_notice(PaneId.HOST_SHELL))
                model.handle_input(b"\r")
                self.assertEqual(2, len(sender.of("restart_pane")))

    def test_restart_result_input_owner_is_adopted(self):
        model, sender = host_exited(owner="manager")
        model.handle_input(b"\r")
        model.on_result({"id": f"r{sender.count}", "ok": True, "pane": "host_shell", "input_owner": "user"})
        self.assertEqual("user", model.input_owner())

    def test_live_host_shell_is_unchanged(self):
        model, sender = make()
        model.handle_input(P + b"3")
        sender.sent.clear()
        model.handle_input(b"\r")
        self.assertEqual([("input", {"pane": "host_shell"}, b"\r")], sender.of("input"))
        self.assertIsNone(model.restart_notice(PaneId.HOST_SHELL))
        self.assertNotIn("종료됨", model.pane_title(PaneId.HOST_SHELL))


CONFIRM_HEAD = "host terminal을 강제 종료합니다. 실행 중인 process가 모두 종료됩니다."
MANAGER_WARNING = "manager가 사용 중입니다 — 진행 중인 명령은 결과 불명으로 남습니다"


class KillHostTerminalTests(unittest.TestCase):
    """C-D63 (2): prefix k / Ctrl-k / ㅏ asks for confirmation; only the k forms confirm; kill_pane host_shell."""

    OPEN = {"k": P + b"k", "K": P + b"K", "Ctrl-k": P + b"\x0b", "ㅏ Space": P + "ㅏ".encode() + b" "}

    def opened(self, form="k", focus="manager"):
        model, sender = make()
        if focus == "host":
            model.handle_input(P + b"3")
        sender.sent.clear()
        model.handle_input(self.OPEN[form])
        return model, sender

    def kills(self, sender):
        return sender.of("kill_pane")

    def test_every_open_form_opens_the_confirmation_and_sends_nothing(self):
        for form in self.OPEN:
            with self.subTest(form=form):
                model, sender = self.opened(form)
                self.assertTrue(model.kill_confirm_open)
                self.assertEqual([], sender.sent)
                joined = "\n".join(model.kill_confirm_lines())
                self.assertIn(CONFIRM_HEAD, joined)
                self.assertIn("k: 종료 · Esc/다른 키: 취소", joined)
                self.assertNotIn(MANAGER_WARNING, joined)
                self.assertIn("강제 종료", model.footer())
                self.assertFalse(model.quit)

    def test_k_forms_confirm_with_exactly_one_kill_pane_host_shell(self):
        for open_form in self.OPEN:
            for confirm, data in (("k", b"k"), ("Ctrl-k", b"\x0b"), ("ㅏ", "ㅏ".encode()),
                                  ("ㅏ Space", "ㅏ".encode() + b" "), ("prefix k", P + b"k"),
                                  ("prefix Ctrl-k", P + b"\x0b"), ("prefix ㅏ Space", P + "ㅏ".encode() + b" ")):
                with self.subTest(open=open_form, confirm=confirm):
                    model, sender = self.opened(open_form)
                    model.handle_input(data)
                    self.assertFalse(model.kill_confirm_open)
                    self.assertEqual([("kill_pane", {"pane": "host_shell"}, b"")], self.kills(sender))
                    self.assertEqual([], sender.of("input"))
                    self.assertIn("강제 종료 중", model.notice)

    def test_only_a_single_confirm_key_on_its_own_confirms_extra_bytes_cancel(self):
        """C-D63 review R3: auto-repeat / typed text / an unbracketed paste starting with k cancels, sends nothing."""
        k_ko = "ㅏ".encode()
        extras = {"kk": b"kk", "kkk": b"kkk", "kill": b"kill", "k Enter": b"k\r", "unbracketed paste": b"kubectl get pods\n",
                  "Ctrl-k Ctrl-k": b"\x0b\x0b", "k Ctrl-k": b"k\x0b", "prefix k k": P + b"kk", "ㅏ k": k_ko + b"k",
                  "ㅏㅏ": k_ko + k_ko, "k wheel": b"k\x1b[<64;5;5M"}
        for name, data in extras.items():
            with self.subTest(extra=name):
                model, sender = self.opened("k")
                model.handle_input(data, now=0.0)
                model.flush_input(now=1.0)
                self.assertFalse(model.kill_confirm_open)
                self.assertEqual([], sender.sent, "nothing reaches a pane or the backend")
                self.assertIn("확인이 취소되었습니다", model.notice)
                self.assertIn("k 한 번만 눌러 확인", model.notice)
                sender.sent.clear()
                model.handle_input(b"ls\r")  # back to normal
                self.assertEqual([b"ls\r"], [s[2] for s in sender.of("input")])
        for name, data in {"k": b"k", "Ctrl-k": b"\x0b", "ㅏ Space": k_ko + b" ", "ㅏ Enter": k_ko + b"\r",
                           "prefix k": P + b"k", "prefix Ctrl-k": P + b"\x0b"}.items():
            with self.subTest(single=name):
                model, sender = self.opened("k")
                model.handle_input(data)
                self.assertEqual([("kill_pane", {"pane": "host_shell"}, b"")], self.kills(sender))
                self.assertEqual([], sender.of("input"))

    def test_two_separate_k_keys_still_confirm_on_the_second_chunk(self):
        model, sender = self.opened("k")
        model.handle_input(P)  # a lone prefix leaves the confirmation open
        self.assertTrue(model.kill_confirm_open)
        model.handle_input(b"k")
        self.assertEqual(1, len(self.kills(sender)))

    def test_nothing_else_confirms_and_cancel_sends_nothing(self):
        cancels = {"Esc": b"\x1b", "Enter": b"\r", "LF": b"\n", "x": b"x", "Space": b" ", "Tab": b"\t", "Ctrl-c": b"\x03",
                   "arrow": b"\x1b[A", "digit": b"1", "q": b"q", "Ctrl-q": b"\x11", "ㅂ": "ㅂ".encode(),
                   "paste": b"\x1b[200~k\x1b[201~", "wheel": b"\x1b[<64;5;5M", "click": b"\x1b[<0;5;5M",
                   "shift-pgup": b"\x1b[5;2~", "prefix x": P + b"x", "prefix q": P + b"q", "prefix Space": P + b" "}
        for name, data in cancels.items():
            with self.subTest(cancel=name):
                model, sender = self.opened("k")
                model.handle_input(data, now=0.0)
                model.flush_input(now=1.0)
                self.assertFalse(model.kill_confirm_open)
                self.assertEqual([], sender.sent, "nothing reaches a pane or the backend")
                self.assertFalse(model.quit, "a cancelling prefix command is not run")
                self.assertFalse(model.menu_open)
                sender.sent.clear()
                model.handle_input(b"ls\r")  # back to normal: the key flows to the focus pane
                self.assertEqual([b"ls\r"], [s[2] for s in sender.of("input")])

    def test_a_window_resize_cancels_the_confirmation_and_sends_nothing_to_a_pane(self):
        for size in ((44, 160), (20, 70), (5, 10)):  # larger, smaller, too small
            with self.subTest(size=size):
                model, sender = self.opened("k")
                sender.sent.clear()
                model.resize(*size)
                self.assertFalse(model.kill_confirm_open)
                self.assertEqual([], self.kills(sender))
                self.assertEqual([], sender.of("input"))
                model.resize(30, 120)
                sender.sent.clear()
                model.handle_input(b"k")  # back to normal: a plain k flows to the focus pane, never kills
                self.assertEqual([], self.kills(sender))
                self.assertEqual([b"k"], [s[2] for s in sender.of("input")])

    def test_works_whatever_pane_has_focus_and_keeps_the_focus(self):
        for focus in ("manager", "host"):
            with self.subTest(focus=focus):
                model, sender = self.opened("k", focus)
                before = model.focus
                model.handle_input(b"k")
                self.assertEqual([("kill_pane", {"pane": "host_shell"}, b"")], self.kills(sender))
                self.assertIs(before, model.focus)

    def test_manager_warning_when_the_manager_owns_the_shell_or_a_command_runs(self):
        for name, setup in (("owner", {"owner": "manager"}), ("in_flight", {"manager_command_in_flight": True})):
            with self.subTest(case=name):
                model, sender = make()
                snap = snapshot(owner=setup.get("owner", "user"))
                snap["panes"]["host_shell"]["manager_command_in_flight"] = setup.get("manager_command_in_flight", False)
                model.apply_snapshot(snap)
                model.handle_input(P + b"k")
                lines = model.kill_confirm_lines()
                self.assertIn(MANAGER_WARNING, lines)
                self.assertIn(CONFIRM_HEAD, "\n".join(lines))
                model.handle_input(b"k")
                self.assertEqual(1, len(self.kills(sender)), "confirming after the warning is allowed")
        quiet, _ = make()
        snap = snapshot()
        snap["panes"]["host_shell"]["manager_command_in_flight"] = False
        quiet.apply_snapshot(snap)
        quiet.handle_input(P + b"k")
        self.assertNotIn(MANAGER_WARNING, quiet.kill_confirm_lines())

    def test_ok_result_shows_the_killed_notice_and_survivors(self):
        model, sender = self.opened("k")
        model.handle_input(b"k")
        model.on_result({"id": f"r{sender.count}", "ok": True, "pane": "host_shell", "killed": True, "survivors": []})
        self.assertEqual("host terminal 강제 종료됨 — Enter로 새 shell", model.notice)
        self.assertIn("Enter로 새 shell", model.footer())
        other, other_sender = self.opened("k")
        other.handle_input(b"k")
        other.on_result({"id": f"r{other_sender.count}", "ok": True, "survivors": [{"pid": 7}]})
        self.assertIn("1개", other.notice)

    def test_refusals_are_shown_and_a_new_attempt_is_possible(self):
        for reason in ("pane_not_killable", "pane_exited", "kill_in_progress", "kill_failed", "backend_shutdown",
                       "not_attached", "pane_unavailable"):
            with self.subTest(reason=reason):
                model, sender = self.opened("k")
                model.handle_input(b"k")
                model.on_result({"id": f"r{sender.count}", "ok": False, "reason": reason, "detail": "why"})
                for needle in (reason, "why", "HOST SHELL", "강제 종료"):
                    self.assertIn(needle, model.footer())
                model.handle_input(P + b"k")
                self.assertTrue(model.kill_confirm_open)
                model.handle_input(b"k")
                self.assertEqual(2, len(self.kills(sender)))

    def test_a_kill_in_flight_is_not_asked_for_again(self):
        model, sender = self.opened("k")
        model.handle_input(b"k")
        model.handle_input(P + b"k")
        self.assertFalse(model.kill_confirm_open)
        self.assertIn("강제 종료 중", model.notice)
        self.assertEqual(1, len(self.kills(sender)))

    def test_menu_row_help_line_and_cycle(self):
        model, sender = make()
        model.handle_input(P + b" ")
        row = next(ln for ln in model.menu_lines() if "host terminal 강제 종료" in ln)
        self.assertIn("Ctrl-] k", row)
        self.assertIn("Ctrl-] Ctrl-k", row)
        self.assertEqual(1, sum("강제 종료" in line and "prefix k" in line for line in HELP_LINES))
        self.assertTrue(any("Ctrl-k" in line and "ㅏ" in line for line in HELP_LINES))
        model.handle_input(b"\x0b")  # Ctrl-k inside the menu runs the row; the menu closes
        self.assertFalse(model.menu_open)
        self.assertTrue(model.kill_confirm_open)
        self.assertEqual([], sender.sent)
        model.handle_input(b"\x1b", now=0.0)
        model.flush_input(now=1.0)
        self.assertFalse(model.kill_confirm_open)
        # the ten numbered items keep their digits and the arrow cycle
        model.handle_input(P + b" ")
        for digit, key in zip("1234567890", ("[", "z", "t", "c", "h", "r", "m", "=", "?", "q")):
            self.assertTrue(any(ln.lstrip("> ").startswith(digit) and f"Ctrl-] {key}" in ln for ln in model.menu_lines()))
        model.handle_input(b"\x1b[A")
        self.assertEqual(9, model.menu_index)

    def test_exited_host_kill_is_asked_and_backend_refusal_shown(self):
        model, sender = host_exited()
        model.handle_input(P + b"k")
        self.assertTrue(model.kill_confirm_open)
        model.handle_input(b"k")
        model.on_result({"id": f"r{sender.count}", "ok": False, "reason": "pane_exited", "detail": "already exited"})
        self.assertIn("pane_exited", model.footer())

    def test_opening_the_confirmation_drops_a_held_ctrl_d(self):
        model, sender = make()
        model.handle_input(b"\x04", now=1.0)  # first Ctrl-d to the manager OMP is held, not sent
        self.assertEqual([], sender.of("input"))
        model.handle_input(P + b"k", now=1.5)
        self.assertTrue(model.kill_confirm_open)
        self.assertIsNone(model._ctrl_d)
        model.handle_input(b"x", now=1.6)  # cancels the confirmation
        model.handle_input(b"\x04", now=1.7)  # a fresh first press: held again, never delivered
        self.assertEqual([], [s for s in sender.of("input") if s[2] == b"\x04"])


# ---- CW-18 U5 (C-D66): current Task / worker / automation state and pause/resume -------------------------------------
def task_state(task=None, worker=None, automation=None, **extra):
    """A snapshot with the ui_v1 task/worker/automation fields (all optional: an older backend sends none)."""
    snap = snapshot()
    if task is not None:
        base = {"task_id": "t1", "kind": "experiment", "status": "running", "summary": "lr sweep 3 runs", "since": 1.0,
                "active": True, "revision": 1, "run_id": "run-1", "runs_started": 1, "retry_limit": 3,
                "held_reason": None, "closed_reason": None, "cancel_requested": False, "last_result": None}
        base.update(task)
        snap["task"] = base
    if worker is not None:
        snap["worker"] = worker
    if automation is not None:
        snap["automation"] = {"state": "active", "source": "user", "detail": None, "paused": False,
                              "transition": None, "run": None, "tick": None, "review": None,
                              "interruption": {"state": "none"}, "resume": None, "retry_limit": 3,
                              "persistence_error": None}
        snap["automation"].update(automation)
    snap.update(extra)
    return snap


def shown(model):
    return "\n".join(model.status_lines())


class TaskWorkerStatusTests(unittest.TestCase):
    def render(self, snap, rows=30, cols=120):
        model = ProductModel(FakeSender(), rows, cols, clock=lambda: 1000.0)
        model.apply_snapshot(snap)
        return model

    def test_worker_state_idle_and_busy(self):
        idle = self.render(task_state(worker={"state": "idle", "task_id": None}))
        self.assertIn("worker: 대기", idle.status_lines()[0])
        busy = self.render(task_state(task={}, worker={"state": "busy", "task_id": "t1"}))
        self.assertIn("worker: 작업 중", busy.status_lines()[0])

    def test_task_kind_summary_and_status_texts(self):
        cases = {("experiment", "running"): ("실험", "실행 중"), ("work", "dispatched"): ("작업", "전달됨"),
                 ("experiment", "starting"): ("실험", "시작 중"), ("experiment", "waiting_report"): ("실험", "보고 대기"),
                 ("work", "cancelling"): ("작업", "취소 중"), ("experiment", "blocked"): ("실험", "막힘"),
                 ("experiment", "finished"): ("실험", "보고 완료"), ("work", "held"): ("작업", "보류")}
        for (kind, status), (kind_text, status_text) in cases.items():
            with self.subTest(kind=kind, status=status):
                model = self.render(task_state(task={"kind": kind, "status": status, "summary": "fix flaky test"}))
                line2 = model.status_lines()[1]
                self.assertIn(kind_text, line2)
                self.assertIn("fix flaky test", line2)
                self.assertIn(status_text, line2)
                self.assertIn("backend: ready", line2, "the rest of the line stays")

    def test_held_reason_and_cancel_request_are_shown(self):
        held = self.render(task_state(task={"status": "dispatched", "held_reason": "host_terminal_busy"}))
        self.assertIn("host terminal 사용 중", held.status_lines()[1])
        other = self.render(task_state(task={"status": "held", "held_reason": "needs_report_or_cancel"}))
        self.assertIn("needs_report_or_cancel", other.status_lines()[1])
        cancelling = self.render(task_state(task={"status": "cancelling", "cancel_requested": True}))
        self.assertIn("취소 중", cancelling.status_lines()[1])

    def test_task_held_by_host_jobs_hints_how_to_clear_it(self):
        base = task_state(task={"status": "dispatched", "held_reason": "host_terminal_busy"})
        jobs = json.loads(json.dumps(base))
        jobs.setdefault("panes", {}).setdefault("host_shell", {})["shell"] = {"held_reasons": ["manual_jobs"]}
        line = self.render(jobs).status_lines()[1]
        self.assertIn("wb-handoff", line)
        self.assertIn("prefix h", line)
        self.assertGreater(line.count("prefix t,c"), 1, line)
        self.assertNotIn("wb-handoff", self.render(base).status_lines()[1], "no jobs reported: no hint")

    def test_task_that_never_ran_shows_start_failure_not_report_done(self):
        # CW-18 smoke-02 E4: a finished experiment whose run failed to start was shown as "보고 완료".
        cases = (
            ({"status": "finished", "held_reason": "start_failed:WorkflowHeld",
              "last_result": {"outcome": "start_failed", "error": "WorkflowHeld"}}, "[시작 실패 · WorkflowHeld]"),
            ({"status": "finished", "held_reason": "start_failed:host_terminal_busy",
              "last_result": {"outcome": "start_failed", "error": "host_terminal_busy"}},
             "[시작 실패 · host terminal 사용 중]"),
            ({"status": "finished", "held_reason": "runner_error:OSError",
              "last_result": {"outcome": "not_started", "reason": "runner_error:OSError"}},
             "[시작 실패 · runner_error:OSError]"),
        )
        for task, expected in cases:
            with self.subTest(task=task):
                line = self.render(task_state(task={"kind": "experiment", "summary": "run exp", **task})).status_lines()[1]
                self.assertIn(expected, line)
                self.assertNotIn("보고 완료", line)
        done = self.render(task_state(task={"kind": "experiment", "status": "finished", "summary": "run exp",
                                            "last_result": {"outcome": "reported"}})).status_lines()[1]
        self.assertIn("[보고 완료]", done, "a run that reported keeps its label")

    def test_closed_task_shows_its_reason(self):
        for reason, text in (("done", "완료"), ("cancelled", "취소됨"), ("superseded_by_new_task", "새 작업으로 대체"),
                             ("mystery", "mystery")):
            with self.subTest(reason=reason):
                model = self.render(task_state(task={"status": "closed", "closed_reason": reason, "active": False}))
                line2 = model.status_lines()[1]
                self.assertIn("종료", line2)
                self.assertIn(text, line2)

    def test_no_task_or_null_task_shows_no_task_segment(self):
        for snap in (task_state(), dict(task_state(), task=None)):
            model = self.render(snap)
            self.assertNotIn("작업:", model.status_lines()[1])
            self.assertIn("마지막 확인", model.status_lines()[1])

    def test_unknown_and_malformed_fields_are_tolerated(self):
        garbage = [{"task": "x"}, {"task": []}, {"task": {"kind": 3, "status": None, "summary": 7}},
                   {"task": {"kind": "future_kind", "status": "future_status", "summary": None}},
                   {"worker": "busy"}, {"worker": {"state": "napping"}}, {"worker": None},
                   {"automation": "paused"}, {"automation": {"state": 5, "review": "soon", "interruption": 3}},
                   {"automation": {"state": "future", "review": {"next_due_in_seconds": "x", "applies": True}}}]
        for extra in garbage:
            with self.subTest(extra=extra):
                model = self.render(dict(snapshot(), **extra))
                line1, line2 = model.status_lines()
                self.assertIn("focus: MANAGER OMP", line1)
                self.assertIn("backend: ready", line2)
                model.handle_input(P + b"p")  # opening the pause confirmation never raises either
                model.handle_input(b"\x1b")

    def test_future_values_are_shown_raw_and_control_characters_are_never_shown(self):
        model = self.render(task_state(task={"kind": "future", "status": "warp", "summary": "a\x1b[31mred\x07\nline\tx"}))
        line2 = model.status_lines()[1]
        self.assertIn("future", line2)
        self.assertIn("warp", line2)
        self.assertNotIn("\x1b", line2)
        self.assertNotIn("\x07", line2)
        self.assertNotIn("\n", line2)
        self.assertNotIn("\t", line2)
        self.assertIn("red", line2)

    def test_summary_is_shortened_by_display_width_with_an_ellipsis(self):
        long_ko = "가" * 80
        for cols in (30, 60, 80, 120, 200):
            with self.subTest(cols=cols):
                model = self.render(task_state(task={"summary": long_ko}), cols=cols)
                line2 = model.status_lines()[1]
                self.assertIn("…", line2)
                self.assertLess(line2.count("가"), 80)
                self.assertIn("backend: ready", line2)
        short = self.render(task_state(task={"summary": "ok"}), cols=30).status_lines()[1]
        self.assertIn("ok", short)

    def test_automation_states_in_the_status_lines(self):
        for state in ("idle", "active", "held", "pausing", "paused", "resuming"):
            with self.subTest(state=state):
                model = self.render(task_state(automation={"state": state, "paused": state in ("paused", "pausing")}))
                self.assertIn(f"자동화: {state}", model.status_lines()[0])

    def test_review_next_due_and_delay_reasons(self):
        review = {"applies": True, "interval_seconds": 60, "status": "waiting", "reason": None, "review_count": 2,
                  "last_review_at": 900.0, "next_due_in_seconds": 41.6, "pending": False, "coalesced_count": 0, "exit": None}
        model = self.render(task_state(automation={"state": "active", "review": review}))
        self.assertIn("60s 대조 41s 후", model.status_lines()[1])
        delayed = dict(review, status="delayed", reason="worker_busy_or_unknown", coalesced_count=2, pending=True)
        model = self.render(task_state(automation={"state": "active", "review": delayed}))
        self.assertIn("대조 지연: worker_busy_or_unknown", model.status_lines()[1])
        model = self.render(task_state(automation={"state": "active", "review": dict(review, applies=False)}))
        self.assertNotIn("대조", model.status_lines()[1])
        model = self.render(task_state(automation={"state": "idle", "review": None}))
        self.assertNotIn("대조", model.status_lines()[1])

    def test_held_automation_reason_and_persistence_error(self):
        model = self.render(task_state(automation={"state": "held", "detail": "user_owner_or_control_hold"}))
        self.assertIn("보류: user_owner_or_control_hold", model.status_lines()[1])
        model = self.render(task_state(automation={"state": "held", "detail": None,
                                                   "tick": {"outcome": "held", "problems": ["host_busy"], "at": 1.0}}))
        self.assertIn("보류: host_busy", model.status_lines()[1])
        model = self.render(task_state(automation={"state": "active", "persistence_error": "disk full"}))
        self.assertIn("저장 오류", model.status_lines()[1])

    def test_paused_shows_interruption_state_and_a_refused_resume(self):
        texts = {"requested": "중단 요청됨", "confirmed": "중단 확인됨", "unknown": "중단 확인 불명",
                 "requesting": "중단 요청 중", "request_failed": "중단 요청 실패"}
        for state, text in texts.items():
            with self.subTest(interruption=state):
                model = self.render(task_state(automation={"state": "paused", "paused": True,
                                                           "interruption": {"state": state}}))
                self.assertIn(text, model.status_lines()[1])
        for quiet in ("none", "not_needed"):
            model = self.render(task_state(automation={"state": "paused", "paused": True,
                                                       "interruption": {"state": quiet}}))
            self.assertNotIn("중단", model.status_lines()[1])
        refused = self.render(task_state(automation={"state": "paused", "paused": True,
                                                     "resume": {"outcome": "refused", "reason": "not_reconciled", "at": 1.0}}))
        self.assertIn("재개 거부: not_reconciled", refused.status_lines()[1])

    def test_no_approval_ui_remains(self):
        model = self.render(dict(task_state(task={}), approvals=[{"approval_id": "a1", "status": "pending"}]))
        joined = shown(model) + "\n".join(HELP_LINES) + "\n".join(model.menu_lines())
        self.assertNotIn("승인", joined)
        self.assertNotIn("approval", joined.lower())


class PauseResumeTests(unittest.TestCase):
    def setUp(self):
        self.sender = FakeSender()
        self.model = ProductModel(self.sender, 30, 120, clock=lambda: 1000.0)

    def state(self, state, paused=None, **extra):
        paused = state in ("paused", "pausing") if paused is None else paused
        self.model.apply_snapshot(task_state(automation=dict({"state": state, "paused": paused}, **extra)))

    def calls(self, kind):
        return self.sender.of(kind)

    OPEN = {"p": P + b"p", "P": P + b"P", "Ctrl-p": P + b"\x10", "ㅔ Space": P + "ㅔ".encode() + b" "}
    CONFIRM = {"p": b"p", "Ctrl-p": b"\x10", "ㅔ": "ㅔ".encode(), "ㅔ Space": "ㅔ".encode() + b" ",
               "prefix p": P + b"p", "prefix Ctrl-p": P + b"\x10", "prefix ㅔ Space": P + "ㅔ".encode() + b" "}

    def test_every_open_form_opens_the_pause_confirmation_and_sends_nothing(self):
        for form, data in self.OPEN.items():
            with self.subTest(form=form):
                self.setUp()
                self.state("active")
                self.model.handle_input(data)
                self.assertTrue(self.model.pause_confirm_open)
                self.assertFalse(self.model.kill_confirm_open)
                self.assertEqual([], [s for s in self.sender.sent if s[0] != "focus"])
                joined = "\n".join(self.model.pause_confirm_lines())
                self.assertIn("자동화를 일시정지합니다 (p: 확인)", joined)
                self.assertIn("일시정지", self.model.footer())
                self.assertFalse(self.model.quit)

    def test_confirm_forms_send_exactly_one_pause_without_fields(self):
        for form, data in self.CONFIRM.items():
            with self.subTest(confirm=form):
                self.setUp()
                self.state("active")
                self.model.handle_input(P + b"p")
                self.model.handle_input(data)
                self.assertFalse(self.model.pause_confirm_open)
                self.assertEqual([("pause", {}, b"")], self.calls("pause"))
                self.assertEqual([], self.calls("resume"))
                self.assertEqual([], self.sender.of("input"))
                self.assertIn("일시정지 요청", self.model.notice)

    def test_every_other_input_cancels_without_sending(self):
        cancels = {"Esc": b"\x1b", "Enter": b"\r", "x": b"x", "Space": b" ", "Ctrl-c": b"\x03", "arrow": b"\x1b[A",
                   "digit": b"1", "k": b"k", "Ctrl-k": b"\x0b", "q": b"q", "ㅂ": "ㅂ".encode(),
                   "paste": b"\x1b[200~p\x1b[201~", "wheel": b"\x1b[<64;5;5M", "prefix x": P + b"x", "prefix q": P + b"q"}
        for name, data in cancels.items():
            with self.subTest(cancel=name):
                self.setUp()
                self.state("active")
                self.model.handle_input(P + b"p")
                self.model.handle_input(data, now=0.0)
                self.model.flush_input(now=1.0)
                self.assertFalse(self.model.pause_confirm_open)
                self.assertFalse(self.model.kill_confirm_open)
                self.assertEqual([], [s for s in self.sender.sent if s[0] != "focus"])
                self.assertFalse(self.model.quit)
                self.sender.sent.clear()
                self.model.handle_input(b"ls\r")
                self.assertEqual([b"ls\r"], [s[2] for s in self.sender.of("input")], "back to normal input")

    def test_extra_bytes_with_the_confirm_key_cancel(self):
        for name, data in {"pp": b"pp", "ps": b"ps", "p Enter": b"p\r", "Ctrl-p Ctrl-p": b"\x10\x10", "prefix p p": P + b"pp",
                           "ㅔㅔ": "ㅔㅔ".encode(), "p wheel": b"p\x1b[<64;5;5M", "paste-like": b"pwd\n"}.items():
            with self.subTest(extra=name):
                self.setUp()
                self.state("active")
                self.model.handle_input(P + b"p")
                self.model.handle_input(data, now=0.0)
                self.model.flush_input(now=1.0)
                self.assertFalse(self.model.pause_confirm_open)
                self.assertEqual([], self.calls("pause"))
                self.assertEqual([], self.sender.of("input"))
                self.assertIn("확인이 취소되었습니다", self.model.notice)
                self.assertIn("p 한 번만 눌러 확인", self.model.notice)

    def test_a_lone_prefix_keeps_it_open_and_the_next_p_confirms(self):
        self.state("active")
        self.model.handle_input(P + b"p")
        self.model.handle_input(P)
        self.assertTrue(self.model.pause_confirm_open)
        self.model.handle_input(b"p")
        self.assertEqual(1, len(self.calls("pause")))

    def test_resize_cancels_the_confirmation(self):
        for size in ((44, 160), (20, 70), (5, 10)):
            with self.subTest(size=size):
                self.setUp()
                self.state("active")
                self.model.handle_input(P + b"p")
                self.model.resize(*size)
                self.assertFalse(self.model.pause_confirm_open)
                self.assertEqual([], self.calls("pause"))

    def test_when_paused_the_confirmation_resumes_with_reconciled_true(self):
        for state in ("paused",):
            self.setUp()
            self.state(state)
            self.model.handle_input(P + b"\x10")
            self.assertTrue(self.model.pause_confirm_open)
            joined = "\n".join(self.model.pause_confirm_lines())
            self.assertIn("대조 후 재개합니다 (p: 확인)", joined)
            self.assertNotIn("일시정지합니다", joined)
            self.assertIn("재개", self.model.footer())
            self.model.handle_input("ㅔ".encode())
            self.assertEqual([("resume", {"reconciled": True}, b"")], self.calls("resume"))
            self.assertEqual([], self.calls("pause"))
            self.assertIn("재개 요청", self.model.notice)

    def test_resume_cancel_paths_send_nothing(self):
        for data in (b"\x1b", b"x", b"\r", b"pp", P + b"q"):
            with self.subTest(data=data):
                self.setUp()
                self.state("paused")
                self.model.handle_input(P + b"p")
                self.model.handle_input(data, now=0.0)
                self.model.flush_input(now=1.0)
                self.assertFalse(self.model.pause_confirm_open)
                self.assertEqual([], self.calls("resume"))
                self.assertEqual([], self.calls("pause"))

    def test_in_progress_and_unknown_states_do_not_open_the_confirmation(self):
        for state, text in (("pausing", "진행 중"), ("resuming", "진행 중")):
            with self.subTest(state=state):
                self.setUp()
                self.state(state)
                self.model.handle_input(P + b"p")
                self.assertFalse(self.model.pause_confirm_open)
                self.assertIn(text, self.model.notice)
                self.assertEqual([], self.sender.of("pause") + self.sender.of("resume"))
        self.setUp()
        self.model.apply_snapshot(dict(snapshot(), automation={}))
        self.model.handle_input(P + b"p")
        self.assertFalse(self.model.pause_confirm_open)
        self.assertIn("알 수 없", self.model.notice)

    def test_idle_and_held_automation_can_be_paused(self):
        for state in ("idle", "active", "held"):
            with self.subTest(state=state):
                self.setUp()
                self.state(state)
                self.model.handle_input(P + b"p")
                self.assertTrue(self.model.pause_confirm_open)
                self.assertEqual("pause", self.model.pause_confirm)

    def test_a_request_in_flight_is_not_asked_for_again(self):
        self.state("active")
        self.model.handle_input(P + b"p")
        self.model.handle_input(b"p")
        self.model.handle_input(P + b"p")
        self.assertFalse(self.model.pause_confirm_open)
        self.assertEqual(1, len(self.calls("pause")))
        self.assertIn("처리 중", self.model.notice)

    def test_ok_results_update_the_automation_state_and_notice(self):
        self.state("active")
        self.model.handle_input(P + b"p")
        self.model.handle_input(b"p")
        rid = f"r{self.sender.count}"
        self.model.on_result({"id": rid, "ok": True, "automation": {"state": "paused", "paused": True, "source": "user"}})
        self.assertIn("자동화: paused", self.model.status_lines()[0])
        self.assertIn("일시정지됨", self.model.notice)
        self.model.handle_input(P + b"p")
        self.model.handle_input(b"p")
        rid = f"r{self.sender.count}"
        self.model.on_result({"id": rid, "ok": True, "automation": {"state": "resuming", "paused": True}})
        self.assertIn("재개 진행 중", self.model.notice)
        self.model.on_state(task_state(automation={"state": "idle", "paused": False}))
        self.assertIn("자동화: idle", self.model.status_lines()[0])

    def test_refusals_are_shown_in_the_footer(self):
        self.state("paused")
        self.model.handle_input(P + b"p")
        self.model.handle_input(b"p")
        rid = f"r{self.sender.count}"
        self.model.on_result({"id": rid, "ok": False, "reason": "resume_not_reconciled", "detail": "resume needs reconciled: true"})
        self.assertIn("재개 거부", self.model.footer())
        self.assertIn("resume_not_reconciled", self.model.footer())
        self.state("active")
        self.model.handle_input(P + b"p")
        self.model.handle_input(b"p")
        rid = f"r{self.sender.count}"
        self.model.on_result({"id": rid, "ok": False, "reason": "backend_shutdown", "detail": ""})
        self.assertIn("일시정지 거부", self.model.footer())
        self.assertIn("backend_shutdown", self.model.footer())

    def test_menu_has_the_pause_row_and_keeps_the_numbered_items(self):
        self.state("active")
        self.model.handle_input(P + b" ")
        lines = self.model.menu_lines()
        row = next(ln for ln in lines if "일시정지" in ln)
        self.assertIn("Ctrl-p", row)
        self.assertGreater(lines.index(row), lines.index(next(ln for ln in lines if "host terminal 강제 종료" in ln)))
        self.model.handle_input(b"\x10")
        self.assertFalse(self.model.menu_open)
        self.assertTrue(self.model.pause_confirm_open)
        self.model.handle_input(b"\x1b", now=0.0)
        self.model.flush_input(now=1.0)
        self.assertFalse(self.model.pause_confirm_open)
        self.model.handle_input(P + b" ")
        self.model.handle_input(b"0")  # the 10th numbered item is still detach
        self.assertTrue(self.model.quit)

    def test_help_has_one_pause_line(self):
        lines = [ln for ln in HELP_LINES if re.match(r"\s*prefix p\s", ln)]
        self.assertEqual(1, len(lines), lines)
        self.assertIn("Ctrl-p", lines[0])
        self.assertIn("ㅔ", lines[0])

    def test_pause_and_kill_confirmations_never_overlap(self):
        self.state("active")
        self.model.handle_input(P + b"p")
        self.model.handle_input(P + b"k")  # the kill key cancels the pause confirmation; it does not open the kill one
        self.assertFalse(self.model.pause_confirm_open)
        self.assertFalse(self.model.kill_confirm_open)
        self.assertEqual([], self.sender.of("kill_pane"))
        self.model.handle_input(P + b"k")
        self.model.handle_input(b"p")  # p does not confirm the kill
        self.assertFalse(self.model.kill_confirm_open)
        self.assertEqual([], self.sender.of("kill_pane"))
        self.assertEqual([], self.calls("pause"))


class WorkerTerminalDisplayTests(unittest.TestCase):
    """p27-cd68-fix-03: smoke-01 P2 (owner shown as worker) and review-02 P3 (1) (readable held reason)."""

    def render(self, snap):
        model = ProductModel(FakeSender(), 30, 120, clock=lambda: 1000.0)
        model.apply_snapshot(snap)
        return model

    def test_a_worker_terminal_command_shows_worker_as_the_host_owner(self):
        snap = snapshot(owner="manager", mode="control_wait")
        snap["panes"]["host_shell"]["operated_by"] = "worker"
        model = self.render(snap)
        self.assertIn("host 입력 owner: worker", model.status_lines()[0])
        self.assertIn("owner=worker", model.pane_title(PaneId.HOST_SHELL))
        self.assertEqual(model.input_owner(), "manager", "the control logic still sees the manager claim")
        experiment = self.render(snapshot(owner="manager", mode="control_wait"))
        self.assertIn("host 입력 owner: manager", experiment.status_lines()[0], "experiment runs unchanged")
        returned = snapshot(owner="user")
        returned["panes"]["host_shell"]["operated_by"] = "worker"
        self.assertIn("host 입력 owner: user", self.render(returned).status_lines()[0], "the user owns it again")

    def test_the_workers_own_command_held_reason_is_readable_without_the_job_hint(self):
        snap = task_state(task={"status": "dispatched", "held_reason": "host_terminal_busy:worker_terminal_command"})
        snap["panes"]["host_shell"]["shell"]["held_reasons"] = ["manual_jobs"]
        line = self.render(snap).status_lines()[1]
        self.assertIn("worker 명령 실행 중", line)
        self.assertNotIn("host_terminal_busy:", line)
        self.assertNotIn("wb-handoff", line, "the user has nothing to clear")


if __name__ == "__main__":
    unittest.main()
