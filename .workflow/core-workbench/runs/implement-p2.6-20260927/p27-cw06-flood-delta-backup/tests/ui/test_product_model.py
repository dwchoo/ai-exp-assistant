import sys
import unittest
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, snapshot  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product.input import InputParser, Command, Paste, PasteRejected, Passthrough, PREFIX  # noqa: E402
from workbench.ui.product.model import ProductModel, pane_inner_sizes  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
