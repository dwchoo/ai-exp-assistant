"""Independent CW-06 re-verification (p27-cw06-rerun-test-06): attach replay is never caught up, live flood still is.

Expectations come from ui_v1 attach semantics (``result`` then ``display`` frames with ``replay=true``: the backend's
retained tail, already bounded) and root-adjudication-p27-cw06-replay-regression: replay frames are fed completely (>= 256 KiB
per pane, starting in an early alt-screen full-screen app state), so every pane equals a synchronous pyte feed of the same
bytes with no catch-up and no indicator; a live flood *after* the replay still triggers catch-up and ends with a clean tail.
Real product UI loop on an owned PTY against a scripted ui_v1 fixture server; no OMP, no model.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import tempfile
import threading
import unittest

from independent_support_cw06 import ScriptedServer, UiPty
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream

PANES = ("manager_omp", "worker_omp", "host_shell")
REPLAY_MIN = 256 * 1024
CATCHUP = "따라잡음".encode()
ROWS, COLS = 40, 140


def replay_bytes(pane: str, minimum: int = REPLAY_MIN + 16 * 1024) -> bytes:
    """Early alt-screen enter + header on row 1 (scroll region rows 2..end), then coloured numbered lines to the end."""
    out = [b"\x1b[?1049h\x1b[2J\x1b[H", f"ALTAPP-HEADER-{pane}".encode(), b"\x1b[2;200r\x1b[2;1H"]
    size, n = sum(map(len, out)), 0
    while size < minimum:
        n += 1
        line = f"\x1b[1;3{n % 7 + 1}m{pane[:4]} row {n:06d}\x1b[0m plain text of the replayed full-screen app\r\n".encode()
        out.append(line)
        size += len(line)
    out.append(f"FINAL-{pane}".encode())
    return b"".join(out)


def flood(total: int, tail: bytes):
    n, sent = 0, 0
    while sent < total:
        lines, size = [], 0
        while size < 32768:
            n += 1
            line = f"\x1b[38;5;{n % 200 + 20}mflood {n:07d} \x1b[1;32mcolour\x1b[0m payload text\r\n".encode()
            lines.append(line)
            size += len(line)
        chunk = b"".join(lines)
        sent += len(chunk)
        yield chunk
    yield tail


def geometry(lines: list[str]) -> list[tuple[int, int, int, int]]:
    """(row0, col0, height, width) of each pane interior, from the UI's own border rows (ACS l/k/m/j/q/x).

    C-D58 layout, returned in PANES order: manager (top left), worker (top right), host shell (full width below).
    """
    top = next(i for i, ln in enumerate(lines) if ln.startswith("lq"))
    upper_bottom = next(i for i, ln in enumerate(lines) if i > top and ln.startswith("mq"))
    host_top = next(i for i, ln in enumerate(lines) if i > upper_bottom and ln.startswith("lq"))
    host_bottom = next(i for i, ln in enumerate(lines) if i > host_top and ln.startswith("mq"))
    boxes = [(top + 1, m.start() + 1, upper_bottom - top - 1, len(m.group(1)))
             for m in re.finditer(r"m(q+)j", lines[upper_bottom])]
    boxes += [(host_top + 1, m.start() + 1, host_bottom - host_top - 1, len(m.group(1)))
              for m in re.finditer(r"m(q+)j", lines[host_bottom])]
    return boxes


class Harness:
    def __init__(self, replay: dict[str, bytes]):
        self.server = ScriptedServer(replay=replay)
        self.work = Path(tempfile.mkdtemp(prefix="cw06-p27h-", dir="/tmp"))
        self.ui = UiPty(self.server.path, self.work, rows=ROWS, cols=COLS)

    def start(self):
        assert self.server.attached.wait(10), self.ui.screen_text()
        assert self.ui.wait_text("focus:", 10), self.ui.screen_text()
        return self

    def pane_lines(self, index: int) -> list[str]:
        lines = self.ui.screen_text().split("\n")
        row0, col0, height, width = geometry(lines)[index]
        return [lines[row0 + r][col0:col0 + width].rstrip() for r in range(height)]

    def close(self):
        self.ui.close()
        self.server.close()
        shutil.rmtree(self.work, ignore_errors=True)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.replay = {pane: replay_bytes(pane) for pane in PANES}
        for data in self.replay.values():
            self.assertGreaterEqual(len(data), REPLAY_MIN)
        self.h = Harness(self.replay)
        self.addCleanup(self.h.close)  # registered before start(): a failed start must not leak the fixture
        self.h.start()

    def _settle(self, ui):
        ui.wait_for(lambda: all(f"FINAL-{p}" in ui.screen_text() for p in PANES), 20)
        ui.drain(0.5)

    def test_replay_over_256kib_per_pane_equals_pyte_and_flood_after_still_catches_up_clean(self):
        server, ui = self.h.server, self.h.ui
        self._settle(ui)
        self.assertIsNone(ui.status(), "UI exited")
        self.assertNotIn(b"Traceback", bytes(ui.output))
        text = ui.screen_text()
        for pane in PANES:
            self.assertIn(f"FINAL-{pane}", text, "replay tail never rendered")
        self.assertNotIn(CATCHUP, bytes(ui.output), "catch-up indicator shown for replay frames")
        geo = geometry(text.split("\n"))
        self.assertEqual(len(geo), 3, text)
        (t0, l0, h0, w0), (t1, l1, h1, w1), (t2, l2, h2, w2) = geo
        self.assertEqual((t0, h0, l0), (t1, h1, 1), "top OMP panes are not one row")
        self.assertEqual(l1, l0 + w0 + 2, "top OMP panes overlap or leave a gap")
        self.assertEqual(l1 + w1 + 1, COLS, "top row does not span the full width")
        self.assertEqual((l2, w2), (1, COLS - 2), "host shell is not full width")
        self.assertEqual(t2, t0 + h0 + 2, "host shell does not sit directly below the top row")
        for index, pane in enumerate(PANES):  # the size the UI reported to the backend is the size it draws
            sent = [(f.header["rows"], f.header["cols"]) for f in server.of("resize") if f.header.get("pane") == pane]
            self.assertEqual(sent[-1], (geo[index][2], geo[index][3]), f"{pane}: reported size != drawn area")
        for index, pane in enumerate(PANES):
            _, _, height, width = geo[index]
            screen = TerminalScreen(width, height)
            make_stream(screen).feed(self.replay[pane])  # one synchronous feed of the exact bytes
            expected = [ln.rstrip() for ln in screen.display]
            actual = self.h.pane_lines(index)
            self.assertIn(f"ALTAPP-HEADER-{pane}", actual[0], f"{pane}: early alt-screen state lost\n" + "\n".join(actual))
            self.assertEqual(actual, expected, f"{pane}: rendered pane != synchronous pyte feed")

        # live flood after the replay: catch-up applies, tail clean
        state = {"done": False}

        def pump():
            for chunk in flood(8 * 1024 * 1024, b"\r\nTAILMARK-END-OF-FLOOD\r\n"):
                server.display("host_shell", chunk)
            state["done"] = True

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 15)
        ui.wait_for(lambda: state["done"], 90)  # drains the PTY while the flood runs
        self.assertTrue(state["done"], "fixture flood did not finish (UI stopped reading?)")
        self.assertTrue(ui.wait_for(lambda: "TAILMARK-END-OF-FLOOD" in ui.screen_text(), 20), ui.screen_text())
        ui.drain(0.5)
        self.assertIn(CATCHUP, bytes(ui.output), "live flood did not trigger catch-up")
        self.assertIsNone(ui.status(), "UI exited during flood")
        self.assertNotIn(b"Traceback", bytes(ui.output))
        host = self.h.pane_lines(2)
        body = [ln for ln in host if ln and "ALTAPP-HEADER" not in ln]
        self.assertTrue(any("TAILMARK-END-OF-FLOOD" in ln for ln in body), "\n".join(host))
        for ln in body:
            self.assertNotRegex(ln, r"[;\[\x1b]|\d+m", f"stray CSI text in tail line {ln!r}")
        # the complete lines that remain are consecutive flood lines (a coherent tail, not spliced fragments)
        numbers = [int(m.group(1)) for ln in body if (m := re.match(r"flood (\d{7}) colour payload text$", ln))]
        self.assertGreaterEqual(len(numbers), 5, "\n".join(host))
        self.assertEqual(numbers, list(range(numbers[0], numbers[0] + len(numbers))), "\n".join(host))
        # other panes untouched by the host flood
        self.assertIn("FINAL-manager_omp", ui.screen_text())
        self.assertIn("FINAL-worker_omp", ui.screen_text())


if __name__ == "__main__":
    unittest.main()
