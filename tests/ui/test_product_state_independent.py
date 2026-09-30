"""Independent CW-06 focus-vs-owner, takeover, freshness, replay and geometry tests (p27-cw06-test-01).

Expectations (CW-06.md, BRIEF C-AC-06/15/18/19, SPEC s2.7, before the implementation was read):
- Focus and host input owner are two separately displayed values; focus changes never
  change (or request a change of) the owner; the owner follows backend state only.
- Takeover request/confirm/handoff visibly change the displayed owner state.
- The "last confirmed" time advances with backend traffic; stale values are not shown as current.
- Replayed history never answers old terminal queries (DSR/DA) — no duplicate input on reattach —
  while live queries are answered like a real terminal.
- Each pane's reported size equals the area the view draws for it; resize reports each pane's own size.
"""
import unittest

from independent_support_cw06 import RecordingSender, SID, snap
from workbench.contracts import ui_v1
from workbench.contracts.v1 import DisplayChunk, PaneId
from workbench.ui.product.input import PREFIX
from workbench.ui.product.model import MIN_COLS, MIN_ROWS, PANES, ProductModel, pane_inner_sizes
from workbench.ui.product.view import pane_rects


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def model(snapshot=None, clock=None):
    s = RecordingSender()
    m = ProductModel(s, 40, 150, clock=clock or Clock())
    m.attach_done(snapshot or snap())
    return m, s


def display(pane, data, *, replay=False, seq=1, sid=SID, gen=1):
    raw = ui_v1.encode_display(DisplayChunk(session_id=sid, session_generation=gen, pane_id=PaneId(pane),
                                            sequence=seq, data=data), replay=replay)
    return ui_v1.FrameDecoder().feed(raw)[0]


class FocusOwnerTests(unittest.TestCase):
    def test_status_shows_focus_and_owner_as_separate_fields(self):
        m, _ = model(snap(owner="manager", mode="control_wait"))
        line1, _ = m.status_lines()
        self.assertIn("focus: MANAGER OMP", line1)
        self.assertIn("owner: manager", line1)
        self.assertIn("owner=manager", m.pane_title(PaneId.HOST_SHELL))
        self.assertIn("FOCUS", m.pane_title(PaneId.MANAGER_OMP))
        self.assertNotIn("FOCUS", m.pane_title(PaneId.HOST_SHELL))

    def test_focusing_the_shell_does_not_change_or_request_owner(self):
        m, s = model(snap(owner="manager"))
        m.handle_input(bytes([PREFIX]) + b"3", now=0)
        self.assertIs(m.focus, PaneId.HOST_SHELL)
        self.assertEqual(m.input_owner(), "manager")
        self.assertEqual({k for k, *_ in s.sent}, {"focus"})
        line1, _ = m.status_lines()
        self.assertIn("focus: HOST SHELL", line1)
        self.assertIn("owner: manager", line1)

    def test_stale_state_push_does_not_revert_a_pending_focus_change(self):
        m, s = model(snap(focus="manager_omp"))
        m.handle_input(bytes([PREFIX]) + b"2", now=0)
        m.on_state(snap(focus="manager_omp"))  # pushed before the focus result
        self.assertIs(m.focus, PaneId.WORKER_OMP)
        rid = s.of("focus")[-1][3]
        m.on_result(ui_v1.result(rid, True, focus="worker_omp"))
        m.on_state(snap(focus="worker_omp"))
        self.assertIs(m.focus, PaneId.WORKER_OMP)

    def test_unknown_owner_is_not_shown_as_user(self):
        bad = snap()
        bad["panes"]["host_shell"]["input_owner"] = None
        bad["panes"]["host_shell"]["shell"]["input_owner"] = None
        m, _ = model(bad)
        self.assertEqual(m.input_owner(), "unknown")


class TakeoverTests(unittest.TestCase):
    def test_request_confirm_handoff_change_the_displayed_owner(self):
        m, s = model(snap(owner="manager", mode="control_wait"))
        m.handle_input(bytes([PREFIX]) + b"t", now=0)
        rid = s.of("takeover_request")[-1][3]
        m.on_result(ui_v1.result(rid, True, shell={"input_owner": "manager", "parent_mode": "control_wait",
                                                   "takeover_requested": True}))
        self.assertIn("인수 요청", m.footer())
        self.assertEqual(m.input_owner(), "manager", "request alone must not show the user as owner")
        m.handle_input(bytes([PREFIX]) + b"c", now=1)
        rid = s.of("takeover_confirm")[-1][3]
        m.on_result(ui_v1.result(rid, True, shell={"input_owner": "user", "parent_mode": "manual_prompt"}))
        self.assertEqual(m.input_owner(), "user")
        self.assertIn("owner: user", m.status_lines()[0])
        m.handle_input(bytes([PREFIX]) + b"h", now=2)
        rid = s.of("handoff")[-1][3]
        m.on_result(ui_v1.result(rid, True, shell={"input_owner": "manager", "parent_mode": "control_wait"}))
        self.assertEqual(m.input_owner(), "manager")
        self.assertIn("shell mode: control_wait", m.status_lines()[0])

    def test_request_not_recorded_by_backend_is_not_announced_as_pending(self):
        # Live (S8/explore): with the user already owning the shell, the backend answers ok but records no
        # request; the UI said "인수 요청됨 — prefix c 로 확인" and the following confirm was refused.
        m, s = model(snap(owner="user", mode="manual_prompt"))
        m.handle_input(bytes([PREFIX]) + b"t", now=0)
        rid = s.of("takeover_request")[-1][3]
        m.on_result(ui_v1.result(rid, True, shell={"input_owner": "user", "parent_mode": "manual_prompt",
                                                   "takeover_requested": False}))
        self.assertNotIn("prefix c", m.footer(), "UI asks to confirm a takeover the backend did not record")

    def test_refused_confirm_is_visible_and_owner_unchanged(self):
        m, s = model(snap(owner="manager"))
        m.handle_input(bytes([PREFIX]) + b"c", now=0)
        rid = s.of("takeover_confirm")[-1][3]
        m.on_result(ui_v1.result(rid, False, reason=ui_v1.Reason.TAKEOVER_HELD, detail="request first"))
        self.assertIn("takeover_held", m.footer())
        self.assertEqual(m.input_owner(), "manager")


class FreshnessTests(unittest.TestCase):
    def test_last_confirmed_advances_with_backend_frames(self):
        clock = Clock(1000.0)
        m, _ = model(clock=clock)
        first = m.status_lines()[1]
        clock.t = 1030.0
        self.assertIn("(30s 전)", m.status_lines()[1])
        m.on_state(snap())
        self.assertIn("(0s 전)", m.status_lines()[1])
        self.assertNotEqual(first, m.status_lines()[1])


class ReplayQueryTests(unittest.TestCase):
    QUERIES = b"\x1b[6n\x1b[c\x1b[>c\x1b[5n"

    def test_replayed_queries_are_not_answered(self):
        m, s = model()
        for pane in ("manager_omp", "worker_omp", "host_shell"):
            m.on_display(display(pane, b"old output " + self.QUERIES, replay=True))
        self.assertEqual(s.of("input"), [], "replayed DSR/DA produced input on reattach")

    def test_live_query_is_answered_once_to_its_pane(self):
        m, s = model()
        m.on_display(display("worker_omp", b"abc\x1b[6n"))
        replies = s.of("input")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0][1]["pane"], "worker_omp")
        self.assertRegex(replies[0][2], rb"^\x1b\[\d+;\d+R$")

    def test_reply_refusal_is_not_shown_as_user_input_refusal(self):
        m, s = model()
        m.on_display(display("host_shell", b"\x1b[6n"))
        rid = s.of("input")[-1][3]
        m.on_result(ui_v1.result(rid, False, reason=ui_v1.Reason.INPUT_OWNER_MANAGER, detail="x"))
        self.assertNotIn("input_owner_manager", m.footer())

    def test_generation_change_resets_the_pane_screen(self):
        m, _ = model()
        m.on_display(display("manager_omp", b"OLD-SESSION-TEXT"))
        m.on_display(display("manager_omp", b"new", gen=2))
        text = "\n".join(m.panes[PaneId.MANAGER_OMP].screen.display)
        self.assertNotIn("OLD-SESSION-TEXT", text)


class GeometryTests(unittest.TestCase):
    def test_reported_pane_sizes_match_drawn_areas(self):
        for rows in (MIN_ROWS, 9, 24, 30, 51):
            for cols in (MIN_COLS, 31, 80, 100, 101, 102, 200, 317):
                inner = pane_inner_sizes(rows, cols)
                rects = pane_rects(rows, cols)
                right = 0
                for pane in PANES:
                    top, left, height, width = rects[pane]
                    with self.subTest(rows=rows, cols=cols, pane=pane.value):
                        self.assertEqual(inner[pane], (height - 2, width - 2))
                        self.assertGreaterEqual(inner[pane][0], 1)
                        self.assertGreaterEqual(inner[pane][1], 1)
                        self.assertEqual(left, right, "panes overlap or leave a gap")
                        right = left + width
                self.assertEqual(right, cols)

    def test_resize_reports_each_pane_its_own_size(self):
        m, s = model()
        m.resize(50, 211)
        sent = {f["pane"]: (f["rows"], f["cols"]) for k, f, *_ in s.of("resize")}
        self.assertEqual(sent, {p.value: pane_inner_sizes(50, 211)[p] for p in PANES})
        for p in PANES:
            screen = m.panes[p].screen
            self.assertEqual((screen.lines, screen.columns), pane_inner_sizes(50, 211)[p])

    def test_too_small_then_back_reports_real_size(self):
        m, s = model()
        m.resize(5, 20)
        self.assertEqual(s.of("resize"), [])
        m.resize(33, 120)
        sent = {f["pane"]: (f["rows"], f["cols"]) for k, f, *_ in s.of("resize")}
        self.assertEqual(sent, {p.value: pane_inner_sizes(33, 120)[p] for p in PANES})


if __name__ == "__main__":
    unittest.main()
