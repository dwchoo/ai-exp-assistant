import sys
import unittest
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product.input import (InputParser, Command, Mouse, PageScroll, Paste, PasteRejected,  # noqa: E402
                                        Passthrough, PREFIX)
from workbench.ui.product.model import (CATCHUP_BACKLOG_BYTES, HELP_LINES, MIN_COLS, MIN_ROWS, PANES,  # noqa: E402
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
        model.handle_input(P + b"d")
        self.assertTrue(model.quit)
        self.assertEqual([], [s for s in sender.sent if s[0].startswith("shutdown")])

    def test_closing_sets_reason(self):
        model, _ = make()
        model.on_closing({"reason": "backend_shutdown"})
        self.assertEqual("backend_shutdown", model.closed_reason)
        self.assertIn("backend_shutdown", model.notice)


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
        model.handle_input(P + b"d")
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


if __name__ == "__main__":
    unittest.main()
