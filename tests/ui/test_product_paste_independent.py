"""Independent CW-06 paste framing and rejection-visibility tests (p27-cw06-test-01).

Expectations (BRIEF C-AC-19, CW-17 L-CW17-CONTRACT consumed by CW-06, before the implementation was read):
- A bracketed paste is one whole paste frame for the focused pane, bytes intact
  (Korean, CR/LF, embedded Esc/prefix bytes), however the host read splits it.
- A paste over 2 MiB is rejected as a whole in the UI: zero bytes sent, a visible reason.
- Backend refusals (queue_full, input_owner_manager, paste_too_large) are shown on screen.
- A refused paste leaves the parser usable: the next key is normal input.
"""
import unittest

from independent_support_cw06 import RecordingSender
from workbench.contracts import ui_v1
from workbench.contracts.v1 import PaneId
from workbench.ui.product.input import PARTIAL_HOLD_SECONDS, PREFIX
from workbench.ui.product.model import ProductModel

START, END = b"\x1b[200~", b"\x1b[201~"
LIMIT = ui_v1.MAX_PASTE_BYTES


def model():
    s = RecordingSender()
    m = ProductModel(s, 40, 150, clock=lambda: 1000.0)
    return m, s


def settle(m, t=10.0):
    m.flush_input(now=t)


def to_shell(m):
    m.handle_input(bytes([PREFIX]) + b"3", now=0)


class PasteFramingTests(unittest.TestCase):
    def test_korean_multiline_paste_is_one_intact_frame(self):
        m, s = model()
        to_shell(m)
        body = "첫 줄 한글\r\n둘째 줄 🙂\n\t셋째\x1b[31m빨강\x1b[0m " .encode() + bytes([PREFIX]) + b" end"
        m.handle_input(START + body + END, now=1)
        settle(m)
        pastes = s.of("paste")
        self.assertEqual(len(pastes), 1)
        self.assertEqual(pastes[0][1]["pane"], "host_shell")
        self.assertEqual(pastes[0][2], START + body + END)
        self.assertEqual(s.of("input"), [])
        self.assertFalse(m.parser.prefix_active, "prefix byte inside a paste armed the prefix")

    def test_paste_split_at_every_boundary_including_markers(self):
        body = "가나다\nline two\n".encode()
        frame = START + body + END
        for cut in range(1, len(frame)):
            with self.subTest(cut=cut):
                m, s = model()
                m.handle_input(frame[:cut], now=0.0)
                m.flush_input(now=0.5)  # long gap inside the paste must not release parts
                m.handle_input(frame[cut:], now=0.6)
                settle(m)
                if cut < len(START):
                    # A split introducer followed by a long gap may be released as keys; only check totals.
                    got = s.payloads("paste") + s.payloads("input")
                    self.assertEqual(got.replace(START, b"").replace(END, b""), body)
                else:
                    self.assertEqual([p for k, _, p, _ in s.sent if k == "paste"], [frame])
                    self.assertEqual(s.of("input"), [])

    def test_text_typed_after_paste_is_input_again(self):
        m, s = model()
        m.handle_input(START + b"p" + END + b"k", now=0)
        settle(m)
        self.assertEqual([(k, p) for k, _, p, _ in s.sent], [("paste", START + b"p" + END), ("input", b"k")])


class PanePasteModeTests(unittest.TestCase):
    """A real terminal wraps a paste in 200~/201~ only when the application enabled DECSET 2004.

    Live evidence (S5): ``cat > file`` in the host shell received the literal markers.
    """

    def _paste_after(self, pane_output):
        from independent_support_cw06 import SID
        from workbench.contracts.v1 import DisplayChunk
        m, s = model()
        to_shell(m)
        raw = ui_v1.encode_display(DisplayChunk(session_id=SID, session_generation=1, pane_id=PaneId.HOST_SHELL,
                                                sequence=1, data=pane_output))
        m.on_display(ui_v1.FrameDecoder().feed(raw)[0])
        m.handle_input(START + "붙여 abc".encode() + END, now=1)
        settle(m)
        return s.payloads("paste") + s.payloads("input")

    def test_app_with_bracketed_paste_gets_markers(self):
        self.assertEqual(self._paste_after(b"$ \x1b[?2004h"), START + "붙여 abc".encode() + END)

    def test_app_without_bracketed_paste_gets_plain_text(self):
        self.assertEqual(self._paste_after(b"$ \x1b[?2004h\x1b[?2004l"), "붙여 abc".encode(),
                         "paste markers delivered to an application that did not enable bracketed paste")


class PasteLimitTests(unittest.TestCase):
    def _paste(self, total, chunk=65536):
        m, s = model()
        to_shell(m)
        body = b"x" * (total - len(START) - len(END))
        frame = START + body + END
        for i in range(0, len(frame), chunk):
            m.handle_input(frame[i:i + chunk], now=1)
        settle(m)
        return m, s

    def test_exactly_2mib_frame_is_sent_whole(self):
        m, s = self._paste(LIMIT)
        self.assertEqual(len(s.of("paste")), 1)
        self.assertEqual(len(s.of("paste")[0][2]), LIMIT)
        self.assertEqual(s.of("input"), [])

    def test_over_2mib_is_rejected_whole_with_visible_reason_and_zero_bytes(self):
        m, s = self._paste(LIMIT + 1)
        self.assertEqual(s.of("paste"), [])
        self.assertEqual(s.of("input"), [], "part of an over-limit paste was delivered")
        self.assertIn("paste_too_large", m.notice)
        self.assertIn("paste_too_large", m.footer())
        m.handle_input(b"z", now=20)
        settle(m, 30)
        self.assertEqual(s.payloads("input"), b"z", "parser stuck after rejection")

    def test_rejection_visible_even_if_help_overlay_is_open(self):
        m, s = model()
        to_shell(m)
        m.handle_input(bytes([PREFIX]) + b"?", now=2)
        self.assertTrue(m.help_open)
        m.handle_input(START + b"y" * LIMIT + END, now=3)
        settle(m)
        self.assertNotIn(b"y", s.payloads("paste") + s.payloads("input"), "over-limit bytes delivered")
        self.assertIn("paste_too_large", m.notice, "over-limit paste while help is open was dropped without a reason")

    def test_paste_while_help_open_is_not_silently_lost(self):
        m, s = model()
        m.handle_input(bytes([PREFIX]) + b"?", now=0)
        m.handle_input(START + "붙여넣기".encode() + END, now=1)
        settle(m)
        delivered = bool(s.of("paste"))
        self.assertTrue(delivered or m.notice, "paste during help overlay vanished: no delivery and no notice")


class BackendRefusalVisibilityTests(unittest.TestCase):
    def _refuse(self, kind, reason, detail, pane_key=b"3", payload=b"a"):
        m, s = model()
        m.handle_input(bytes([PREFIX]) + pane_key, now=0)
        if kind == "paste":
            m.handle_input(START + payload + END, now=1)
        else:
            m.handle_input(payload, now=1)
        settle(m)
        rid = [r for k, _, _, r in s.sent if k == kind][-1]
        m.on_result(ui_v1.result(rid, False, reason=ui_v1.Reason(reason), detail=detail))
        return m

    def test_queue_full_paste_refusal_is_shown(self):
        m = self._refuse("paste", "queue_full", "1048576 bytes exceeds free queue space 9")
        self.assertIn("queue_full", m.footer())
        self.assertIn("HOST SHELL", m.footer())

    def test_input_owner_manager_refusal_is_shown(self):
        m = self._refuse("input", "input_owner_manager", "manager owns shell input; request takeover first")
        self.assertIn("input_owner_manager", m.footer())

    def test_backend_paste_too_large_refusal_is_shown(self):
        m = self._refuse("paste", "paste_too_large", "x bytes exceeds 2097152", pane_key=b"1")
        self.assertIn("paste_too_large", m.footer())
        self.assertIn("MANAGER OMP", m.footer())

    def test_accepted_result_does_not_overwrite_a_refusal_notice(self):
        m, s = model()
        to_shell(m)
        m.handle_input(b"a", now=1)
        m.handle_input(b"b", now=1)
        settle(m)
        first, second = [r for k, _, _, r in s.sent if k == "input"]
        m.on_result(ui_v1.result(first, False, reason=ui_v1.Reason.QUEUE_FULL, detail="full"))
        m.on_result(ui_v1.result(second, True, pane="host_shell", accepted_bytes=1))
        self.assertIn("queue_full", m.footer())


if __name__ == "__main__":
    unittest.main()
