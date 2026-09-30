"""Independent CW-06 UR-UX layout + host scrollback tests (p27-urux-layout-test-01).

Expectations come from C-D58 (user request), written before model.py/view.py were read:
- "상단에 omp 2개, 하단에 터미널": manager OMP and worker OMP side by side on top (together spanning the full width),
  host terminal below across the whole width ("가로로 길게"). The host shell pane therefore has the full inner width
  (columns - 2) and the backend is told exactly the size each pane really has.
- "터미널도 스크롤이 되어야해": output that scrolled off the host terminal can be scrolled back to in the UI, in order.
  While looking at history the keys drive the view and are never delivered to the shell, output arriving meanwhile
  does not move the viewed lines, and leaving scroll mode shows exactly the live screen with normal typing again.
  History keeps at least 5000 lines (Root packet). The outer terminal gets no mouse tracking (the terminal's own
  selection/copy must keep working), so scrolling is key driven; prefix commands stay usable.
- Keys used are the documented ones (prefix ``[`` enters, PgUp/PgDn/Home/End/Up/Down move, ``q`` leaves; ``prefix ?``).
Real runtime: the product UI loop on an owned PTY (scripted ui_v1 fixture), and the real ``workbench attach``
entrypoint against a real backend running a stub OMP (zero model turns, no provider, no credentials).
"""
from __future__ import annotations

import fcntl
import os
import struct
import termios
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from independent_support_cw06 import RecordingSender, SID, ScriptedServer, UiPty, snap
from workbench.contracts import ui_v1
from workbench.contracts.v1 import DisplayChunk, PaneId
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.ui.product.input import PREFIX
from workbench.ui.product.model import MIN_COLS, MIN_ROWS, ProductModel, pane_inner_sizes
from workbench.ui.product.view import pane_rects

P = bytes([PREFIX])
PGUP, PGDN, HOME, END = b"\x1b[5~", b"\x1b[6~", b"\x1b[H", b"\x1b[F"
NUM = re.compile(r"line (\d{5})")
MOUSE = re.compile(rb"\x1b\[\?[0-9;]*(?:1000|1001|1002|1003|1005|1006|1015)[0-9;]*h")
SIZES = [(MIN_ROWS, MIN_COLS), (MIN_ROWS + 1, MIN_COLS + 1), (24, 80), (25, 81), (30, 100), (31, 101), (41, 151),
         (51, 211), (60, 300)]


class Clock:
    def __call__(self):
        return 1000.0


def lines_bytes(first: int, last: int) -> bytes:
    return b"".join(f"line {i:05d}\r\n".encode() for i in range(first, last + 1))


class Host:
    """A pure ProductModel focused on the host shell, fed like the backend would."""

    def __init__(self, rows: int = 30, cols: int = 100, **snapshot):
        self.sender = RecordingSender()
        self.model = ProductModel(self.sender, rows, cols, clock=Clock())
        self.model.attach_done(snap(focus="host_shell", **snapshot))
        self.model.after_attach()
        self.model.handle_input(P + b"3", now=0)  # focus the host shell explicitly
        self.rows, self.cols = pane_inner_sizes(rows, cols)[PaneId.HOST_SHELL]
        self.seq = 0
        self.now = 1.0

    def feed(self, data: bytes) -> None:
        self.seq += 1
        raw = ui_v1.encode_display(DisplayChunk(session_id=SID, session_generation=1, pane_id=PaneId.HOST_SHELL,
                                                sequence=self.seq, data=data), replay=False)
        self.model.on_display(ui_v1.FrameDecoder().feed(raw)[0])
        while self.model.has_backlog():
            self.model.feed_pending()

    def keys(self, data: bytes) -> None:
        self.now += 1
        self.model.handle_input(data, now=self.now)

    def view(self) -> list[str]:
        out = []
        for line in self.model.pane_lines(PaneId.HOST_SHELL, self.rows):
            out.append("".join(getattr(line.get(x), "data", " ") or " " for x in range(self.cols)).rstrip())
        return out

    def live(self) -> list[str]:
        return [ln.rstrip() for ln in self.model.panes[PaneId.HOST_SHELL].screen.display]

    def numbers(self, view: list[str] | None = None) -> list[int]:
        return [int(m.group(1)) for ln in (view if view is not None else self.view()) if (m := NUM.fullmatch(ln))]

    def inputs(self) -> list[tuple[str, dict, bytes, str]]:
        return [f for f in self.sender.of("input") if f[1].get("pane") == "host_shell"]

    def assert_contiguous(self, test: unittest.TestCase, view: list[str] | None = None) -> list[int]:
        nums = self.numbers(view)
        test.assertTrue(nums, f"no output lines visible: {view or self.view()}")
        test.assertEqual(nums, list(range(nums[0], nums[0] + len(nums))), f"lines out of order or missing: {nums}")
        return nums


class LayoutModelTests(unittest.TestCase):
    def test_backend_gets_each_panes_real_size_and_host_has_full_inner_width(self):
        margins = set()
        for rows, cols in SIZES:
            with self.subTest(rows=rows, cols=cols):
                h = Host(rows, cols)
                inner = pane_inner_sizes(rows, cols)
                rects = pane_rects(rows, cols)
                sent = {}
                for _, fields, _, _ in h.sender.of("resize"):
                    sent[fields["pane"]] = (fields["rows"], fields["cols"])  # the last report per pane wins
                self.assertEqual(sent, {p.value: inner[p] for p in inner}, "reported size != real inner size")
                (mr, mc), (wr, wc), (hr, hc) = (inner[PaneId.MANAGER_OMP], inner[PaneId.WORKER_OMP],
                                                inner[PaneId.HOST_SHELL])
                self.assertEqual(hc, cols - 2, "host shell must span the full width")
                self.assertEqual(mc + wc + 4, cols, "top OMP panes must span the full width together")
                self.assertLessEqual(abs(mc - wc), 1, "top OMP panes are not about half each")
                self.assertEqual(mr, wr)
                self.assertGreaterEqual(min(mr, wr, hr, mc, wc, hc), 1)
                margins.add(rows - (mr + hr + 4))
                top = rects[PaneId.MANAGER_OMP]
                host = rects[PaneId.HOST_SHELL]
                self.assertEqual(host[1:4:2], (0, cols))
                self.assertGreater(host[0], top[0], "host shell must be below the OMP panes")
        self.assertEqual(len(margins), 1, f"non-pane rows differ between sizes: {margins}")
        self.assertGreaterEqual(min(margins), 3, "status header and footer rows are missing")

    def test_host_screen_wraps_at_the_full_inner_width_not_half(self):
        for rows, cols in SIZES:
            with self.subTest(rows=rows, cols=cols):
                h = Host(rows, cols)
                h.feed(b"x" * (cols - 2))
                self.assertEqual(h.live()[0], "x" * (cols - 2))
                self.assertEqual(h.live()[1], "")
                h.feed(b"y")
                self.assertEqual(h.live()[1], "y")
                self.assertEqual(h.model.panes[PaneId.HOST_SHELL].screen.columns, cols - 2)

    def test_resize_reports_new_full_width_host_and_half_width_omp_sizes(self):
        h = Host(30, 100)
        n = len(h.sender.of("resize"))
        for rows, cols in ((41, 151), (24, 81), (60, 300), (MIN_ROWS, MIN_COLS)):
            h.model.resize(rows, cols)
            inner = pane_inner_sizes(rows, cols)
            new = {f["pane"]: (f["rows"], f["cols"]) for _, f, _, _ in h.sender.of("resize")[n:]}
            n = len(h.sender.of("resize"))
            self.assertEqual(new.get("host_shell"), (inner[PaneId.HOST_SHELL][0], cols - 2), (rows, cols))
            self.assertEqual(new.get("manager_omp"), inner[PaneId.MANAGER_OMP])
            self.assertEqual(new.get("worker_omp"), inner[PaneId.WORKER_OMP])
            self.assertEqual(h.model.panes[PaneId.HOST_SHELL].screen.columns, cols - 2)


class ScrollModelTests(unittest.TestCase):
    def setUp(self):
        self.h = Host(30, 100)
        self.h.feed(lines_bytes(1, 400))
        self.assertGreater(400, 3 * self.h.rows, "fixture must be more than 3 screens of output")

    def test_scrolling_back_shows_earlier_lines_in_order_and_walks_the_whole_history(self):
        h = self.h
        live = h.assert_contiguous(self, h.view())
        self.assertEqual(live[-1], 400)
        h.keys(P + b"[")
        self.assertEqual(h.view(), h.live(), "entering scroll mode must not change what is shown")
        h.keys(PGUP)
        first = h.assert_contiguous(self)
        self.assertLess(first[0], live[0])
        self.assertLess(first[-1], live[-1])
        h.keys(HOME)
        top = h.assert_contiguous(self)
        self.assertEqual(top[0], 1, "Home must reach the earliest retained line")
        seen, guard = set(top), 0
        while max(seen) < 400 and guard < 200:  # page down from the top to live: nothing is skipped
            guard += 1
            h.keys(PGDN)
            seen.update(h.assert_contiguous(self))
        self.assertEqual(sorted(seen), list(range(1, 401)), "paging back to live skipped or repeated out of order")
        h.keys(END)
        h.keys(HOME)
        h.keys(b"\x1b[B" * 3)  # Down x3
        self.assertEqual(h.assert_contiguous(self)[0], 4)
        h.keys(b"\x1b[A")  # Up x1
        self.assertEqual(h.assert_contiguous(self)[0], 3)

    def test_keys_typed_while_scrolled_are_not_delivered(self):
        h = self.h
        h.keys(P + b"[")
        h.keys(PGUP)
        before_view, before_sent = h.view(), len(h.sender.sent)
        h.keys(b"zzz hello 123\r\x03\x04\t !")
        self.assertEqual(h.sender.sent[before_sent:], [], "keys typed in scroll mode reached the backend")
        self.assertEqual(h.view(), before_view, "unrelated keys must not move the view")
        h.keys(b"\x1b[200~pasted text\x1b[201~")
        self.assertEqual(h.sender.of("paste"), [], "a paste in scroll mode reached the shell")
        self.assertEqual(h.view(), before_view)

    def test_output_arriving_while_scrolled_does_not_move_the_viewed_lines(self):
        for step in (PGUP + PGUP, b"\x1b[A", HOME, PGUP + PGUP + PGDN):
            with self.subTest(step=step):
                h = Host(30, 100)
                h.feed(lines_bytes(1, 400))
                h.keys(P + b"[")
                h.keys(step)
                before = h.view()
                h.assert_contiguous(self, before)
                h.feed(lines_bytes(401, 460))
                self.assertEqual(h.view(), before, "viewed lines moved when output arrived")
                h.feed(b"\x1b[2J\x1b[Hafter a full repaint\r\n")
                self.assertEqual(h.view(), before, "viewed lines moved after a full-screen repaint")
                h.feed(lines_bytes(461, 900))
                self.assertEqual(h.view(), before, "viewed lines moved under a long burst of output")

    def test_leaving_scroll_mode_shows_exactly_the_live_screen_and_typing_works(self):
        h = self.h
        h.keys(P + b"[")
        h.keys(HOME)
        h.feed(lines_bytes(401, 450))
        self.assertNotEqual(h.view(), h.live())
        h.keys(b"q")
        self.assertEqual(h.view(), h.live(), "scroll-mode exit must show exactly the live screen")
        self.assertEqual(h.assert_contiguous(self)[-1], 450, "output that arrived while scrolled is missing")
        before = len(h.inputs())
        h.keys(b"ls\r")
        self.assertEqual([f[2] for f in h.inputs()[before:]], [b"ls\r"], "typing does not reach the shell again")
        self.assertEqual(h.view(), h.live())

    def test_escape_also_leaves_scroll_mode_without_being_forwarded(self):
        h = self.h
        h.keys(P + b"[")
        h.keys(PGUP)
        h.keys(b"\x1b")
        h.now += 10
        h.model.flush_input(now=h.now)
        self.assertEqual(h.view(), h.live())
        self.assertEqual(h.inputs(), [], "the Esc that leaves scroll mode was forwarded to the shell")
        h.keys(b"x")
        self.assertEqual([f[2] for f in h.inputs()], [b"x"])

    def test_history_keeps_at_least_5000_lines(self):
        h = Host(30, 100)
        for first in range(1, 6501, 500):
            h.feed(lines_bytes(first, first + 499))
        live = h.assert_contiguous(self, h.view())
        h.keys(P + b"[")
        h.keys(HOME)
        top = h.assert_contiguous(self)
        self.assertGreaterEqual(live[0] - top[0], 5000, f"only {live[0] - top[0]} history lines retained")
        h.keys(PGDN)
        h.assert_contiguous(self)

    def test_prefix_commands_still_work_in_scroll_mode(self):
        cases = {
            "t": lambda h: len(h.sender.of("takeover_request")) == 1,
            "c": lambda h: len(h.sender.of("takeover_confirm")) == 1,
            "h": lambda h: len(h.sender.of("handoff")) == 1,
            "r": lambda h: len(h.sender.of("resize")) > h._resizes,
            "?": lambda h: h.model.help_open is True,
            "d": lambda h: h.model.quit is True,
            "1": lambda h: h.sender.of("focus")[-1][1]["pane"] == "manager_omp",
            "2": lambda h: h.sender.of("focus")[-1][1]["pane"] == "worker_omp",
            "\t": lambda h: h.sender.of("focus")[-1][1]["pane"] == "manager_omp",
        }
        for key, effect in cases.items():
            with self.subTest(key=key):
                h = Host(30, 100)
                h.feed(lines_bytes(1, 200))
                h.keys(P + b"[")
                h.keys(PGUP)
                h._resizes = len(h.sender.of("resize"))
                h.keys(P + key.encode())
                self.assertTrue(effect(h), f"prefix {key!r} had no effect while scrolled")
                self.assertEqual(h.inputs(), [], "a prefix command leaked text to the shell")


def contiguous(nums: list[int]) -> bool:
    return bool(nums) and nums == list(range(nums[0], nums[0] + len(nums)))


def boxes(screen_lines: list[str]) -> list[tuple[int, int, int, int]]:
    """(row0, col0, height, width) of manager, worker, host interiors from the UI's own ACS border rows."""
    top = next(i for i, ln in enumerate(screen_lines) if ln.startswith("lq"))
    mid = next(i for i, ln in enumerate(screen_lines) if i > top and ln.startswith("mq"))
    host_top = next(i for i, ln in enumerate(screen_lines) if i > mid and ln.startswith("lq"))
    host_bottom = next(i for i, ln in enumerate(screen_lines) if i > host_top and ln.startswith("mq"))
    out = [(top + 1, m.start() + 1, mid - top - 1, len(m.group(1))) for m in re.finditer(r"m(q+)j", screen_lines[mid])]
    out += [(host_top + 1, m.start() + 1, host_bottom - host_top - 1, len(m.group(1)))
            for m in re.finditer(r"m(q+)j", screen_lines[host_bottom])]
    return out


class PtyBase(unittest.TestCase):
    ROWS, COLS = 40, 140

    def setUp(self):
        self.server = ScriptedServer()
        self.work = Path(tempfile.mkdtemp(prefix="cw06-p27j-", dir="/tmp"))
        self.ui = UiPty(self.server.path, self.work, rows=self.ROWS, cols=self.COLS)
        self.addCleanup(shutil.rmtree, self.work, True)
        self.addCleanup(self.server.close)
        self.addCleanup(self.ui.close)
        self.assertTrue(self.server.attached.wait(10), self.ui.screen_text())
        self.assertTrue(self.ui.wait_text("focus:", 10), self.ui.screen_text())

    def host_lines(self) -> list[str]:
        lines = self.ui.screen_text().split("\n")
        row0, col0, height, width = boxes(lines)[2]
        return [lines[row0 + r][col0:col0 + width].rstrip() for r in range(height)]

    def host_numbers(self) -> list[int]:
        return [int(m.group(1)) for ln in self.host_lines() if (m := NUM.fullmatch(ln))]

    def send_host(self, data: bytes) -> None:
        self.server.display("host_shell", data)

    def focus_host(self) -> None:
        self.ui.send(P + b"3")
        self.assertTrue(self.server.wait(lambda: any(f.header.get("pane") == "host_shell"
                                                     for f in self.server.of("focus"))))
        self.assertTrue(self.ui.wait_text("focus: HOST SHELL", 10), self.ui.screen_text())


class PtyLayoutScrollTests(PtyBase):
    def test_drawn_layout_and_reported_sizes(self):
        self.ui.drain(0.5)
        geo = boxes(self.ui.screen_text().split("\n"))
        self.assertEqual(len(geo), 3, self.ui.screen_text())
        (t0, l0, h0, w0), (t1, l1, h1, w1), (t2, l2, h2, w2) = geo
        self.assertEqual((t0, h0, l0), (t1, h1, 1))
        self.assertEqual(l1, l0 + w0 + 2)
        self.assertEqual(l1 + w1 + 1, self.COLS)
        self.assertEqual((l2, w2), (1, self.COLS - 2), "host shell is not drawn across the full width")
        self.assertEqual(t2, t0 + h0 + 2)
        for pane, (_, _, height, width) in zip(("manager_omp", "worker_omp", "host_shell"), geo):
            sent = [(f.header["rows"], f.header["cols"]) for f in self.server.of("resize") if f.header["pane"] == pane]
            self.assertEqual(sent[-1], (height, width), f"{pane}: backend told a size other than the drawn area")
        self.send_host(b"y" * (self.COLS - 2) + b"Z")
        self.assertTrue(self.ui.wait_text("Z", 10))
        self.assertEqual(self.host_lines()[0], "y" * (self.COLS - 2), self.host_lines())
        self.assertEqual(self.host_lines()[1], "Z")
        # live window resize: the new full-width host size is reported and drawn
        self.ui.resize(45, 170)
        self.assertTrue(self.server.wait(lambda: any(f.header["pane"] == "host_shell" and f.header["cols"] == 168
                                                     for f in self.server.of("resize"))), "no resize for 170 cols")
        self.assertIsNone(self.ui.status(), "UI exited on resize")

    def test_scroll_back_in_the_real_ui_loop(self):
        self.focus_host()
        sent = b""
        for first in range(1, 301, 20):
            chunk = lines_bytes(first, first + 19)
            sent += chunk
            self.send_host(chunk)
        self.assertTrue(self.ui.wait_for(lambda: 300 in self.host_numbers(), 20), self.ui.screen_text())
        live_before = self.host_lines()
        height = len(live_before)
        self.assertGreater(300, 3 * height)
        self.ui.send(P + b"[")
        self.ui.send(PGUP)
        # a curses repaint can be caught half way: wait for a settled (contiguous) picture, then judge it
        self.assertTrue(self.ui.wait_for(lambda: contiguous(self.host_numbers()) and max(self.host_numbers()) < 300, 10),
                        self.ui.screen_text())
        self.ui.send(HOME)
        self.assertTrue(self.ui.wait_for(lambda: contiguous(self.host_numbers()) and self.host_numbers()[0] == 1, 10),
                        self.ui.screen_text())
        top = self.host_numbers()
        # keys typed while scrolled never reach the shell; output arriving meanwhile does not move the view
        base = len(self.server.of("input"))
        self.ui.send(b"zzz hello 123\r")
        self.ui.send(b"\x1b[200~pasted\x1b[201~")
        extra = lines_bytes(301, 340)
        sent += extra
        self.send_host(extra)
        self.ui.drain(0.8)
        self.assertEqual(self.host_numbers(), top, "viewed lines moved while output arrived")
        self.assertEqual(self.server.of("input")[base:], [], "typed keys were delivered in scroll mode")
        self.assertEqual(self.server.of("paste"), [])
        self.assertIsNone(self.ui.status(), "UI exited")
        # leave: exactly the live screen (a synchronous pyte feed of everything sent), typing works again
        self.ui.send(b"q")
        width = boxes(self.ui.screen_text().split("\n"))[2][3]
        expected = TerminalScreen(width, height)
        make_stream(expected).feed(sent)
        want = [ln.rstrip() for ln in expected.display]
        self.assertTrue(self.ui.wait_for(lambda: self.host_lines() == want, 10),
                        "\n".join(self.host_lines()) + "\n--- expected live ---\n" + "\n".join(want))
        self.ui.send(b"ls\r")
        self.assertTrue(self.server.wait(lambda: any(f.payload == b"ls\r" for f in self.server.of("input"))))
        self.assertEqual(self.server.payloads("input", "host_shell"), b"ls\r")

    def test_prefix_detach_works_in_scroll_mode_and_no_mouse_tracking_is_enabled(self):
        self.focus_host()
        self.send_host(lines_bytes(1, 120))
        self.assertTrue(self.ui.wait_for(lambda: 120 in self.host_numbers(), 20), self.ui.screen_text())
        self.ui.send(P + b"[")
        self.ui.send(HOME)
        self.assertTrue(self.ui.wait_for(lambda: 1 in self.host_numbers(), 10), self.ui.screen_text())
        self.ui.send(P + b"?")
        self.assertTrue(self.ui.wait_text("prefix", 10), self.ui.screen_text())
        self.ui.send(b" ")
        self.ui.send(P + b"d")
        self.assertTrue(self.ui.ui_done(15), self.ui.screen_text())
        self.assertEqual(self.ui.status(), 0)
        self.assertTrue(self.server.wait(lambda: self.server.of("detach")))
        output = bytes(self.ui.output)
        self.assertIsNone(MOUSE.search(output), f"mouse tracking enabled: {MOUSE.search(output)}")
        self.assertNotIn(b"Traceback", output)


# --- real entrypoint + real backend + stub OMP (zero model turns) -------------------------------------------------
STUB = '''#!/usr/bin/env python3
import os, sys, tty
if "--version" in sys.argv:
    print("omp/18.2.10"); sys.exit(0)
role = os.environ.get("WORKBENCH_G3_ROLE", "?")
tty.setraw(0)
sys.stdout.write(f"STUB-OMP {role} ready\\r\\n> "); sys.stdout.flush()
while True:
    data = os.read(0, 4096)
    if not data:
        break
    sys.stdout.write(f"[{role} got {data!r}]\\r\\n> "); sys.stdout.flush()
'''
SRC = str(Path(__file__).resolve().parents[2] / "src")
ENV = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
       "TERM": "xterm-256color"}
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"


def proc_identity(pid: int) -> tuple[int, str] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[1].split()
    return None if fields[0] in "ZX" else (pid, fields[19])  # a reaped-by-nobody zombie holds no resources


def procs_naming(root: Path) -> dict[int, str]:
    """Processes (pid -> starttime) whose cmdline or environment names our temp root."""
    needle, found = str(root).encode(), {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            blob = (entry / "cmdline").read_bytes() + (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in blob and (ident := proc_identity(int(entry.name))):
            found[ident[0]] = ident[1]
    return found


class RealEntrypointRuntime(unittest.TestCase):
    ROWS, COLS = 40, 140

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw06-p27j-rt-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.omp = self.root / "omp"
        self.omp.write_text(STUB)
        self.omp.chmod(0o755)
        self.data = self.root / "d"
        self.seen: dict[int, str] = {}
        self.addCleanup(self.stop_backend)

    def cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, "-c", MAIN, *args], env=ENV, cwd=self.root, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def stop_backend(self):
        self.seen.update(procs_naming(self.root))
        self.cli("shutdown", "--data-dir", str(self.data), "--yes")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(proc_identity(p) == (p, t) for p, t in self.seen.items()):
            time.sleep(0.1)
        leaked = [p for p, t in self.seen.items() if proc_identity(p) == (p, t)]
        self.leaked_info = []
        for pid in leaked:  # exact identity (pid + starttime) only
            try:
                self.leaked_info.append((pid, Path(f"/proc/{pid}/cmdline").read_bytes()[:200]))
            except OSError:
                pass
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            try:
                if proc_identity(pid) == (pid, self.seen[pid]):
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        self.leaked = leaked

    def attach_ui(self) -> UiPty:
        ui = UiPty.__new__(UiPty)
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", self.ROWS, self.COLS, 0, 0))
        ui.rows, ui.cols = self.ROWS, self.COLS
        # own session with the PTY as controlling terminal, so a window-size change delivers SIGWINCH
        ui.process = subprocess.Popen(["/usr/bin/setsid", "--ctty", sys.executable, "-c", MAIN, "attach",
                                       "--data-dir", str(self.data)],
                                      stdin=slave, stdout=slave, stderr=slave, env=ENV, close_fds=True)
        os.close(slave)
        ui.fd, ui.output = master, bytearray()
        ui.before_file = ui.after_file = ui.status_file = self.root / "unused"
        self.addCleanup(ui.close)
        return ui

    def test_host_shell_full_width_size_and_scrollback(self):
        started = self.cli("start", "--data-dir", str(self.data), "--omp", str(self.omp), "--no-attach", "--timeout", "3")
        self.assertIn("starting backend", started.stdout)
        ui = self.attach_ui()
        self.assertTrue(ui.wait_for(lambda: "STUB-OMP manager ready" in ui.screen_text()
                                    and "STUB-OMP worker ready" in ui.screen_text(), 30), ui.screen_text())
        self.seen.update(procs_naming(self.root))
        geo = boxes(ui.screen_text().split("\n"))
        self.assertEqual(len(geo), 3, ui.screen_text())
        self.assertEqual((geo[2][1], geo[2][3]), (1, self.COLS - 2), "host pane is not the full width")
        self.assertEqual(geo[0][0], geo[1][0], "the two OMP panes are not on one row")
        self.assertLess(geo[0][0], geo[2][0], "host pane is not below the OMP panes")

        def host_text() -> list[str]:
            lines = ui.screen_text().split("\n")
            row0, col0, height, width = boxes(lines)[2]
            return [lines[row0 + r][col0:col0 + width].rstrip() for r in range(height)]

        def numbers() -> list[int]:
            return [int(m.group(1)) for ln in host_text() if (m := re.fullmatch(r"LINE-(\d+)", ln))]

        ui.send(P + b"3")
        self.assertTrue(ui.wait_text("focus: HOST SHELL", 15), ui.screen_text())
        ui.send(b"stty size\r")
        rows, cols = geo[2][2], geo[2][3]
        self.assertTrue(ui.wait_for(lambda: f"{rows} {cols}" in host_text(), 20),
                        f"stty size != drawn host pane {rows} {cols}\n" + "\n".join(host_text()))
        ui.send(b"for i in $(seq 1 300); do echo LINE-$i; done\r")
        self.assertTrue(ui.wait_for(lambda: 300 in numbers(), 30), "\n".join(host_text()))
        self.assertGreater(300, 3 * len(host_text()))
        ui.send(P + b"[")
        ui.send(PGUP)
        # a curses repaint can be caught half way: wait for a settled (contiguous) picture, then judge it
        self.assertTrue(ui.wait_for(lambda: contiguous(numbers()) and 300 not in numbers(), 10), "\n".join(host_text()))
        ui.send(HOME)
        self.assertTrue(ui.wait_for(lambda: contiguous(numbers()) and numbers()[0] == 1, 10), "\n".join(host_text()))
        ui.send(b"echo SCROLLTYPED\r")  # scroll mode: must never reach bash
        ui.drain(1.0)
        ui.send(b"q")
        self.assertTrue(ui.wait_for(lambda: 300 in numbers(), 10), "\n".join(host_text()))
        ui.send(b"echo ALIVE-$((6*7))\r")
        self.assertTrue(ui.wait_for(lambda: "ALIVE-42" in host_text(), 15), "\n".join(host_text()))
        self.assertNotIn("SCROLLTYPED", ui.screen_text().replace("echo SCROLLTYPED", ""))
        self.assertNotIn("SCROLLTYPED", "\n".join(host_text()), "keys typed in scroll mode reached bash")
        # a window resize reaches the host shell as the new full-width size
        ui.resize(44, 160)  # the earlier bytes are a 140-col picture: only look at what is drawn from now on
        ui.drain(1.0)
        mark = len(ui.output)
        ui.send(b"stty size\r")
        self.assertTrue(ui.wait_for(lambda: re.search(rb"(?<!\d)\d+ 158(?!\d)", bytes(ui.output[mark:])) is not None, 20),
                        bytes(ui.output[mark:])[-600:])
        ui.send(P + b"d")
        self.assertTrue(ui.wait_for(lambda: b"detached; backend keeps running" in bytes(ui.output), 10))
        self.assertEqual(ui.process.wait(15), 0, "attach process did not exit cleanly after detach")
        ui.drain(0.2)
        self.assertIsNone(MOUSE.search(bytes(ui.output)), "mouse tracking enabled by the real entrypoint")
        self.assertEqual(self.cli("status", "--data-dir", str(self.data), "--json").returncode, 0)
        self.stop_backend()
        self.assertEqual(self.leaked, [], f"leaked processes: {self.leaked_info}")


if __name__ == "__main__":
    unittest.main()
