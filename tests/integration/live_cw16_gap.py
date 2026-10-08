"""LIVE CW-16 gap scenarios (p27-cw16-gap-01): behaviours the C16 formal run did not re-observe on OMP 18.8.0.

Opt-in: ``WB_LIVE_CW16=1``. Built only on the B1 harness (``cw16_harness``) and the B2 rig (``cw16_flow_rig``):
the real backend through ``python -m workbench start``, the user's installed OMP (version recorded, never pinned:
C-D72 (2)), the product UI (``python -m workbench attach``) on an owned PTY rendered by pyte (the outermost
terminal emulator: it keeps 24-bit colour) and a scripted local provider. Fake HOME with an EMPTY agent.db,
proxies closed: ZERO real model requests, no credential reachable.

Scenarios (VERIFICATION draft s9/s11 gaps):
  COLOR    G1-COLOR: SGR colours written by a host-shell program reach the outer terminal through the product UI
           with the same colour: 16 basic colours, 256-colour cube, 256-colour grey ramp, 256-colour system
           indices and 24-bit RGB (foreground and background). Each class is its own step; the expected value is
           the colour the program wrote (pyte's xterm palette for indices), compared as RGB. C-D73: a 24-bit RGB is
           expected as the nearest 256-colour palette entry (6x6x6 cube or grey ramp, the nearer; computed here).
  SCROLL   G1-TEXT-TOOLS scroll: host pane history in scroll mode (prefix [): top of a 400-line output, position
           kept while new output arrives, End -> live, q exits and no scroll key reaches the shell; Shift+PgUp
           direct scroll and the return to live on input; mouse wheel when the UI captures the mouse; scroll mode
           on an OMP pane.
  COMPOSER G3-TUI-CONTENTION non-empty composer hold (C-D70 (2)): a worker report to a manager whose composer
           holds a draft is deferred (not delivered, draft intact, never submitted), shown after 30 s in status
           (``recovery.report_wait`` reason ``manager_editor_not_empty``) and in the UI; once the user empties
           the composer it is delivered exactly once.
  SESSION  G3-SESSION: ``/new`` in the manager OMP TUI while the Workbench runs: same OMP process, the bridge
           re-registers with the new OMP session id and generation + 1, nothing delivered before is replayed
           (no injected message, ``reports_resent`` 0), the only model turn the switch causes is the single
           ``manager_recovery`` notice of C-D70 (5), the open Task is unchanged, and the next worker report is
           delivered once to the new session (its envelope names the new session and generation).

Run (repo root; the venv has pyte; TMUX*/HERDR_* stripped)::

    env -i HOME=/tmp/<fake> PATH=$HOME_REAL/.local/bin:/usr/bin:/bin LANG=C.UTF-8 PYTHONPATH=src \\
      PYTHONDONTWRITEBYTECODE=1 WB_LIVE_CW16=1 WB_CW16_REPORT_DIR=<dir> \\
      /tmp/cw02-g1-venv/bin/python -m unittest -v tests/integration/live_cw16_gap.py

Filters: ``WB_CW16_SCENARIOS=COLOR,SCROLL,COMPOSER,SESSION``. Reports: ``$WB_CW16_REPORT_DIR/<run id>/gap-*.json``
and ``gap-summary.json``.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cw16_harness as h  # noqa: E402
import cw16_flow_rig as fr  # noqa: E402
from cw16_flow_rig import Rig, spec  # noqa: E402

SKIP = fr.skip_reason()

BASIC = ("black", "red", "green", "brown", "blue", "magenta", "cyan", "white")


def _palette() -> list[str]:
    from pyte.graphics import FG_BG_256
    return [value.lower() for value in FG_BG_256]


def nearest_256(rgb_hex: str) -> str:
    """C-D73 expectation: the nearest cube or grey-ramp colour of the xterm palette (a tie keeps the cube)."""
    rgb = [int(rgb_hex[i:i + 2], 16) for i in (0, 2, 4)]
    palette = _palette()
    candidates = list(range(16, 256))  # cube 16-231 first, then the grey ramp 232-255
    return palette[min(candidates, key=lambda index: sum(
        (a - int(palette[index][i:i + 2], 16)) ** 2 for a, i in zip(rgb, (0, 2, 4))))]


def as_rgb(value: str) -> str | None:
    """A pyte cell colour as 6-digit RGB hex in the xterm palette (None for 'default' / unknown)."""
    value = (value or "").lower()
    if value == "bfightmagenta":  # pyte.graphics.BG_AIXTERM[105] is misspelled upstream; it is bright magenta
        value = "brightmagenta"
    if re.fullmatch(r"[0-9a-f]{6}", value):
        return value
    palette = _palette()
    if value in BASIC:
        return palette[BASIC.index(value)]
    if value.startswith("bright") and value[6:] in BASIC:
        return palette[8 + BASIC.index(value[6:])]
    if value.isdigit() and int(value) < 256:
        return palette[int(value)]
    return None


def find_cell(ui: h.Ui, marker: str, *, skip_after: str = "") -> tuple[int, int] | None:
    """(row, col) of ``marker`` on the outer screen, on a row that is program output (not the typed command)."""
    for row, line in enumerate(ui.lines()):
        col = line.find(marker)
        if col >= 0 and not (skip_after and skip_after in line):
            return row, col
    return None


def cell_colors(ui: h.Ui, row: int, col: int) -> dict:
    cell = ui.screen.buffer[row][col]
    return {"char": cell.data, "fg": cell.fg, "bg": cell.bg, "fg_rgb": as_rgb(cell.fg), "bg_rgb": as_rgb(cell.bg)}


def user_text(suffix: str):
    return lambda r: r.injected is None and r.last_role == "user" and r.last_text.endswith(suffix)


def task_injected(r: h.Request) -> bool:
    return r.injected is not None and r.injected.get("kind") == "task" and "response_contract" not in r.injected


STATUS_ROWS = 2  # the product UI's two status lines (they also name the focused pane: "focus: HOST SHELL")


def title_row(ui: h.Ui, title: str) -> int | None:
    """Screen row of the pane box whose title contains ``title`` (the status lines are skipped)."""
    for row, line in enumerate(ui.lines()):
        if row >= STATUS_ROWS and title in line:
            return row
    return None


def title_line(ui: h.Ui, title: str) -> str:
    row = title_row(ui, title)
    return "" if row is None else ui.lines()[row]


def pane_text(rig: Rig, title: str) -> str:
    """Rough text of the screen columns under the pane whose title contains ``title`` (top row of the pane box)."""
    lines = rig.ui.lines()
    for row, line in enumerate(lines):
        col = line.find(title) if row >= STATUS_ROWS else -1
        if col >= 0:
            left = line.rfind("┌", 0, col)
            left = 0 if left < 0 else left
            right = line.find("┐", col)
            right = len(line) if right < 0 else right
            return "\n".join(item[left:right + 1] for item in lines[row:])
    return ""


class _Chain:
    """One scripted worker turn of tool calls (the B2 ``Chain`` pattern, local copy to stay additive)."""

    def __init__(self, rig: Rig, role: str, trigger, steps: list, name: str):
        import threading
        self.rig, self.steps, self.name = rig, steps, name
        self.calls: list[tuple] = []
        self.results: list[dict] = []
        self.started = threading.Event()
        self.done = threading.Event()
        rig.p.on(lambda r: not self.started.is_set() and trigger(r), self._start, role=role, name=f"{name}:start")
        rig.p.on(self._pending, self._next, role=role, name=f"{name}:next")

    def _emit(self, r: h.Request, index: int):
        if index >= len(self.steps):
            self.done.set()
            return f"{self.name} finished"
        out = self.steps[index](r, self)
        if isinstance(out, tuple):
            self.calls.append(out)
            return h.tools(out)
        self.done.set()
        return out

    def _start(self, r: h.Request):
        self.started.set()
        return self._emit(r, 0)

    def _pending(self, r: h.Request) -> bool:
        return (bool(self.calls) and len(self.results) < len(self.calls) and r.last_role == "tool"
                and self.calls[-1][2] in r.tool_results)

    def _next(self, r: h.Request):
        self.results.append(r.tool_results[self.calls[-1][2]])
        return self._emit(r, len(self.calls))

    def wait(self, timeout: float) -> bool:
        return self.rig.pump_until(self.done.is_set, timeout)


def open_task(rig: Rig, trigger: str, goal: str) -> dict:
    """Manager rule: user text ``trigger`` -> to_worker(work); the worker acknowledges the Task with text only."""
    holder: dict = {}

    def respond(r: h.Request):
        holder["call"] = rig.to_worker(kind="work", message=goal, spec=spec(goal, ["notes/"]))
        return h.tools(holder["call"])

    rig.p.on(user_text(trigger), respond, role="manager", once=True, name=f"open:{trigger}")
    rig.p.on(lambda r: task_injected(r) and r.injected.get("task_id") == holder.get("tid"),
             "Task received; waiting for the user", role="worker", name=f"ack:{trigger}")
    return holder


def dispatch(rig: Rig, holder: dict, trigger: str) -> str:
    rig.say("manager_omp", trigger)
    assert rig.pump_until(lambda: "call" in holder and rig.result(holder["call"]) is not None, 60), holder
    result = rig.result(holder["call"])
    assert result["status"] == "dispatched", result
    holder["tid"] = result["task_id"]
    rig.wait_injected("worker", lambda i: i.get("kind") == "task" and i.get("task_id") == holder["tid"], 60,
                      "Task delivered")
    rig.pump(1.0)
    return holder["tid"]


@unittest.skipIf(SKIP, SKIP or "")
class Cw16GapScenarios(unittest.TestCase):
    def setUp(self):
        name = self._testMethodName.split("_")[1].upper()
        if not fr.selected(name):
            self.skipTest(f"filtered out by WB_CW16_SCENARIOS ({name})")

    @classmethod
    def tearDownClass(cls):
        fr.write_summary(("gap-",), "gap-summary.json")

    # ------------------------------------------------------------------------------------------- G1-COLOR
    def test_color_sgr_fidelity_through_the_product_ui(self):
        rig = Rig(self, "gap-G1-COLOR")
        palette = _palette()
        rig.step("start", lambda: rig.start() and {"term": rig.sb.env.get("TERM"),
                                                     "colorterm": rig.sb.env.get("COLORTERM")})

        def emit(cases: list[tuple[str, str]], tag: str) -> dict:
            """cases: (marker suffix, SGR parameters). Prints each marker in its colour; returns observed cells."""
            parts = " ".join(f"\\033[{sgr}m%s\\033[0m" for _, sgr in cases)
            args = " ".join(f'"GQ{tag}""{suffix}"' for suffix, _ in cases)  # the typed line never shows the marker
            rig.say("host_shell", f"printf '{parts}\\n' {args}")
            last = f"GQ{tag}{cases[-1][0]}"
            assert rig.ui.wait_text(re.escape(last), 20), rig.ui.excerpt("GQ")
            rig.pump(0.5)
            seen = {}
            for suffix, sgr in cases:
                marker = f"GQ{tag}{suffix}"
                where = find_cell(rig.ui, marker, skip_after="printf")
                assert where is not None, (marker, rig.ui.excerpt("GQ"))
                seen[marker] = {"sgr": sgr, **cell_colors(rig.ui, *where)}
            return seen

        def compare(seen: dict, expected: dict) -> dict:
            wrong = {}
            for marker, (attr, want) in expected.items():
                got = seen[marker][f"{attr}_rgb"]
                if got != want:
                    wrong[marker] = {"sgr": seen[marker]["sgr"], "expected": want, "outer": got,
                                     "outer_raw": seen[marker][attr]}
            assert not wrong, {"mismatch": wrong}
            return {"observed": seen}

        def basic16():
            cases = [("R1", "31"), ("G2", "32"), ("B4", "34"), ("K9", "91"), ("Y3", "43")]
            seen = emit(cases, "A")
            return compare(seen, {"GQAR1": ("fg", palette[1]), "GQAG2": ("fg", palette[2]), "GQAB4": ("fg", palette[4]),
                                  "GQAK9": ("fg", palette[9]), "GQAY3": ("bg", palette[3])})

        def sgr_ranges():
            """fix-05 (C17 P2-2): every SGR 30-37 / 90-97 (fg) and 40-47 / 100-107 (bg), e.g. 93/103 (pyte 'brightbrown')."""
            groups = [("F", 30, 0, "fg"), ("G", 90, 8, "fg"), ("H", 40, 0, "bg"), ("I", 100, 8, "bg")]
            observed, expected = {}, {}
            for tag, first, base, attr in groups:
                cases = [(f"N{first + n}", str(first + n)) for n in range(8)]
                observed.update(emit(cases, tag))
                expected.update({f"GQ{tag}N{first + n}": (attr, palette[base + n]) for n in range(8)})
            return compare(observed, expected)

        def cube256():
            cases = [("C196", "38;5;196"), ("C046", "38;5;46"), ("C214", "38;5;214"), ("B021", "48;5;21")]
            seen = emit(cases, "B")
            return compare(seen, {"GQBC196": ("fg", palette[196]), "GQBC046": ("fg", palette[46]),
                                  "GQBC214": ("fg", palette[214]), "GQBB021": ("bg", palette[21])})

        def grey256():
            cases = [("G236", "38;5;236"), ("G244", "38;5;244"), ("G252", "38;5;252"), ("H240", "48;5;240")]
            seen = emit(cases, "C")
            return compare(seen, {"GQCG236": ("fg", palette[236]), "GQCG244": ("fg", palette[244]),
                                  "GQCG252": ("fg", palette[252]), "GQCH240": ("bg", palette[240])})

        def system256():
            cases = [("S001", "38;5;1"), ("S004", "38;5;4"), ("S008", "38;5;8"), ("S012", "38;5;12")]
            seen = emit(cases, "D")
            return compare(seen, {"GQDS001": ("fg", palette[1]), "GQDS004": ("fg", palette[4]),
                                  "GQDS008": ("fg", palette[8]), "GQDS012": ("fg", palette[12])})

        def truecolor():
            cases = [("T1", "38;2;18;52;86"), ("T2", "38;2;255;128;0"), ("T3", "38;2;200;30;140"),
                     ("U1", "48;2;250;100;50")]
            seen = emit(cases, "E")
            return compare(seen, {"GQET1": ("fg", nearest_256("123456")), "GQET2": ("fg", nearest_256("ff8000")),
                                  "GQET3": ("fg", nearest_256("c81e8c")), "GQEU1": ("bg", nearest_256("fa6432"))})

        def omp_pane_colours():
            rows = rig.ui.screen.buffer
            colours = set()
            for row in range(2, rig.ui.rows // 2):
                for col in range(1, rig.ui.cols - 1):
                    cell = rows[row][col]
                    if cell.fg != "default":
                        colours.add(cell.fg)
            return {"distinct_outer_fg_in_omp_panes": sorted(colours)[:40], "count": len(colours),
                    "omp": rig.report.data.get("versions", {}).get("omp"),
                    "note": "information only: OMP's own theme colours as they reach the outer terminal"}

        rig.step("COLOR_basic16", basic16, ("start",))
        rig.step("COLOR_sgr_30_37_90_97_40_47_100_107", sgr_ranges, ("start",))
        rig.step("COLOR_256_cube", cube256, ("start",))
        rig.step("COLOR_256_grey_ramp", grey256, ("start",))
        rig.step("COLOR_256_system_indices", system256, ("start",))
        rig.step("COLOR_24bit_rgb", truecolor, ("start",))
        rig.step("COLOR_omp_pane_info", omp_pane_colours, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- G1-TEXT-TOOLS scroll
    def test_scroll_host_and_omp_pane_history_in_the_product_ui(self):
        rig = Rig(self, "gap-G1-SCROLL")
        rig.step("start", lambda: rig.start() and {})

        def host_title() -> str:
            return title_line(rig.ui, "HOST SHELL")

        def visible(marker: str) -> bool:
            return marker in rig.ui.text()

        def scroll_mode():
            rig.say("host_shell", "seq -f 'GQS%04g' 1 400")
            assert rig.ui.wait_text("GQS0400", 20), rig.ui.excerpt("GQS")
            rig.say("host_shell", "(sleep 5; echo GQ\"\"LATE) &")
            rig.pump(0.5)
            rig.ui_key(b"[", settle=0.5)
            assert rig.ui.wait(lambda: "[SCROLL" in host_title(), 10), host_title()
            entered = host_title().strip()
            rig.ui.send(b"g", settle=0.5)  # top of history
            assert rig.ui.wait(lambda: visible("GQS0001"), 10), rig.ui.excerpt("GQS")
            top_view = [line for line in rig.ui.lines() if "GQS" in line][:3]
            assert not visible("GQS0400"), "the top of history and the live end are both on screen"
            rig.pump(7.0)  # the background job prints GQLATE meanwhile
            assert visible("GQS0001") and not visible("GQLATE"), "the scrolled view moved with new output"
            kept = [line for line in rig.ui.lines() if "GQS" in line][:3]
            assert kept == top_view, {"before": top_view, "after": kept}
            rig.ui.send(b"\x1b[6~", settle=0.4)  # PgDn: one page down
            assert not visible("GQS0001"), "PgDn did not move the view"
            rig.ui.send(b"G", settle=0.5)  # End/G: live
            assert rig.ui.wait(lambda: visible("GQLATE"), 10), rig.ui.excerpt("GQ")
            rig.ui.send(b"q", settle=0.5)
            assert rig.ui.wait(lambda: "[SCROLL" not in host_title(), 10), host_title()
            rig.say("host_shell", "echo GQ\"\"DONE")
            assert rig.ui.wait_text("GQDONE", 10), rig.ui.excerpt("GQ")
            leaked = [line for line in rig.ui.lines() if re.search(r"\$ *[gGq]+ *echo|command not found|not found", line)]
            assert not leaked, {"scroll keys reached the shell": leaked}
            return {"title_in_mode": entered, "top_view": top_view, "kept_while_output_arrived": True,
                    "live_after_G": True, "keys_not_forwarded": True}

        def shift_pgup_direct():
            rig.focus("host_shell")
            assert visible("GQDONE"), rig.ui.excerpt("GQ")
            rig.ui.send(b"\x1b[5;2~", settle=0.6)  # Shift+PgUp: direct scroll, no mode
            assert rig.ui.wait(lambda: "[SCROLL" in host_title(), 10), host_title()
            assert not visible("GQDONE") and any("GQS" in line for line in rig.ui.lines()), rig.ui.excerpt("GQ")
            title = host_title().strip()
            rig.ui.type("echo GQ\"\"LIVE", gap=0.01)  # typing into a scrolled pane returns it to live
            rig.ui.send(b"\r", settle=0.5)
            assert rig.ui.wait_text("GQLIVE", 10), rig.ui.excerpt("GQ")
            assert rig.ui.wait(lambda: "[SCROLL" not in host_title(), 10), host_title()
            return {"title_scrolled": title, "back_to_live_on_input": True}

        def mouse_wheel():
            raw = bytes(rig.ui.raw)
            capture = any(seq in raw for seq in (b"\x1b[?1000h", b"\x1b[?1002h", b"\x1b[?1003h"))
            if not capture:
                raise h.NotApplicable("the product UI did not enable mouse reporting (prefix m toggles it)")
            row = title_row(rig.ui, "HOST SHELL") + 3
            for _ in range(4):
                rig.ui.send(f"\x1b[<64;20;{row + 1}M".encode(), settle=0.2)
            assert rig.ui.wait(lambda: "[SCROLL" in host_title(), 10), host_title()
            title = host_title().strip()
            for _ in range(10):
                rig.ui.send(f"\x1b[<65;20;{row + 1}M".encode(), settle=0.1)
            rig.ui.type("echo GQ\"\"WHEEL", gap=0.01)
            rig.ui.send(b"\r", settle=0.5)
            assert rig.ui.wait_text("GQWHEEL", 10), rig.ui.excerpt("GQ")
            return {"title_after_wheel": title}

        def omp_pane_scroll_mode():
            rig.focus("manager_omp")
            rig.ui_key(b"[", settle=0.5)
            title = lambda: title_line(rig.ui, "MANAGER OMP")  # noqa: E731
            assert rig.ui.wait(lambda: "[SCROLL" in title(), 10), title()
            shown = title().strip()
            rig.ui.send(b"q", settle=0.5)
            assert rig.ui.wait(lambda: "[SCROLL" not in title(), 10), title()
            return {"manager_title_in_mode": shown}

        rig.step("SCROLL_mode_history_kept_live_exit", scroll_mode, ("start",))
        rig.step("SCROLL_shift_pgup_direct_and_live_on_input", shift_pgup_direct, ("start",))
        rig.step("SCROLL_mouse_wheel", mouse_wheel, ("start",))
        rig.step("SCROLL_omp_pane_mode", omp_pane_scroll_mode, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- G3-TUI-CONTENTION
    def test_composer_nonempty_holds_worker_report_until_cleared(self):
        rig = Rig(self, "gap-G3-COMPOSER")
        draft = "GQDRAFT42 keep me"
        holder = open_task(rig, "gap-c-open", "gap composer task")
        reporter = _Chain(rig, "worker", user_text("gap-c-report"), [
            lambda r, c: rig.to_manager(kind="progress", message="GQ composer progress", task_id=holder["tid"]),
            lambda r, c: "reported",
        ], "gap-c-worker")
        ctx: dict = {}
        rig.step("start", lambda: rig.start() and {})

        def draft_in_manager_composer():
            ctx["tid"] = dispatch(rig, holder, "gap-c-open")
            rig.focus("manager_omp")
            rig.ui.type(draft, gap=0.02)
            assert rig.ui.wait(lambda: "GQDRAFT42" in pane_text(rig, "MANAGER OMP"), 10), rig.ui.excerpt("GQDRAFT")
            ctx["manager_requests"] = rig.p.count("manager")
            ctx["manager_injected"] = len(rig.injected("manager"))
            return {"task_id": ctx["tid"], "draft_visible": True}

        def report_deferred():
            rig.say("worker_omp", "gap-c-report")
            assert reporter.wait(60), reporter.results
            queued = reporter.results[0]
            assert queued["status"] == "queued", queued
            began = time.monotonic()
            snap = rig.wait(lambda s: ((s.get("recovery") or {}).get("report_wait") or {}).get("reason")
                            == "manager_editor_not_empty", 75, "report_wait shown")
            waited = time.monotonic() - began
            wait = snap["recovery"]["report_wait"]
            assert waited >= 25, f"report_wait shown after {waited:.1f}s (expected the 30 s editor wait)"
            assert rig.ui.wait_text("worker 보고 대기 중: manager 입력창을 비우면 전달됩니다", 15), \
                rig.ui.excerpt("보고 대기", "worker")
            assert len(rig.injected("manager")) == ctx["manager_injected"], "the report was delivered anyway"
            assert rig.p.count("manager") == ctx["manager_requests"], "the manager OMP ran a turn meanwhile"
            assert "GQDRAFT42" in pane_text(rig, "MANAGER OMP"), "the draft disappeared"
            task = rig.task()
            assert task.get("task_id") == ctx["tid"], task
            return {"tool_result": queued["status"], "report_wait": wait, "shown_after_s": round(waited, 1),
                    "ui": rig.ui.excerpt("보고 대기"), "manager_requests_delta": 0}

        def cleared_then_delivered_once():
            rig.focus("manager_omp")
            for _ in range(len(draft)):
                rig.ui.send(b"\x7f", settle=0.03)
            assert rig.ui.wait(lambda: "GQDRAFT" not in pane_text(rig, "MANAGER OMP"), 10), rig.ui.excerpt("GQDRAFT")
            cleared = time.monotonic()
            got = rig.wait_injected("manager", lambda i: (i.get("payload") or {}).get("kind") == "progress"
                                    and i.get("task_id") == ctx["tid"], 60, "deferred report delivered")
            latency = time.monotonic() - cleared
            rig.wait(lambda s: (s.get("recovery") or {}).get("report_wait") is None, 30, "report_wait cleared")
            assert rig.ui.wait(lambda: "manager 입력창을 비우면" not in rig.ui.text(), 15), rig.ui.excerpt("보고 대기")
            rig.pump(5.0)
            same = rig.injected("manager", lambda i: i.get("workbench_message_id") == got.get("workbench_message_id"))
            assert len(same) == 1, f"delivered {len(same)} times"
            submitted = [e for e in rig.p.log if e.get("role") == "manager" and "GQDRAFT" in (e.get("text_tail") or "")]
            assert not submitted, {"the draft was submitted to the model": submitted}
            assert "GQDRAFT" not in str(got.get("payload")), "the draft leaked into the delivered report"
            return {"delivered_after_clear_s": round(latency, 2), "message": (got.get("payload") or {}).get("message"),
                    "deliveries": len(same)}

        rig.step("COMPOSER_draft_in_manager", draft_in_manager_composer, ("start",))
        rig.step("COMPOSER_report_deferred_and_shown", report_deferred, ("COMPOSER_draft_in_manager",))
        rig.step("COMPOSER_cleared_then_delivered_once", cleared_then_delivered_once,
                 ("COMPOSER_report_deferred_and_shown",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- G3-SESSION
    def test_session_switch_in_manager_tui_reregisters_without_replay(self):
        rig = Rig(self, "gap-G3-SESSION")
        ctx: dict = {"manager_texts": []}

        def capture(r: h.Request) -> bool:  # records, never answers
            if r.role == "manager":
                ctx["manager_texts"].append((r.index, r.injected is not None, r.last_role, r.last_text))
            return False

        rig.p.on(capture, None, name="gap-s-capture")
        holder = open_task(rig, "gap-s-open", "gap session task")
        first = _Chain(rig, "worker", user_text("gap-s-progress"), [
            lambda r, c: rig.to_manager(kind="progress", message="GQ before the switch", task_id=holder["tid"]),
            lambda r, c: "progress sent",
        ], "gap-s-first")
        second = _Chain(rig, "worker", user_text("gap-s-done"), [
            lambda r, c: rig.to_manager(kind="done", message="GQ after the switch", task_id=holder["tid"]),
            lambda r, c: "done sent",
        ], "gap-s-second")

        def start():
            ready = rig.start()
            ctx["bridge0"] = dict(ready["bridge"]["manager"])
            ctx["pane0"] = dict(ready["panes"]["manager_omp"]["process"])
            return {"bridge_manager": ctx["bridge0"]}

        def delivered_before_switch():
            ctx["tid"] = dispatch(rig, holder, "gap-s-open")
            rig.say("worker_omp", "gap-s-progress")
            assert first.wait(60), first.results
            got = rig.wait_injected("manager", lambda i: (i.get("payload") or {}).get("kind") == "progress"
                                    and i.get("task_id") == ctx["tid"], 60, "progress before the switch")
            ctx["first_id"] = got.get("workbench_message_id")
            rig.pump(2.0)
            return {"message_id": ctx["first_id"]}

        def switch():
            before = rig.status()["bridge"]["manager"]
            ctx["requests_before"] = {role: rig.p.count(role) for role in ("manager", "worker")}
            ctx["texts_before"] = len(ctx["manager_texts"])
            ctx["journal_before"] = len(rig.journal())
            ctx["injected_before"] = {role: len(rig.injected(role)) for role in ("manager", "worker")}
            ctx["task_before"] = {k: rig.task().get(k) for k in ("task_id", "revision", "active", "status")}
            rig.focus("manager_omp")
            rig.ui.type("/new", gap=0.05)
            assert rig.ui.wait(lambda: "/new" in pane_text(rig, "MANAGER OMP"), 10), rig.ui.excerpt("/new")
            rig.pump(0.5)
            enters = 0
            changed = lambda s: s["bridge"]["manager"].get("session_id") not in (None, before.get("session_id"))  # noqa
            for _ in range(2):
                rig.ui.send(b"\r", settle=0.5)
                enters += 1
                try:
                    snap = rig.wait(changed, 20, "manager bridge re-registered with a new session")
                    break
                except AssertionError:
                    if enters >= 2:
                        raise
            after = snap["bridge"]["manager"]
            pane = snap["panes"]["manager_omp"]["process"]
            assert after.get("pid") == before.get("pid") == ctx["pane0"]["pid"], (before, after, ctx["pane0"])
            assert pane == ctx["pane0"], {"pane process changed": (ctx["pane0"], pane)}
            assert after.get("pid_matches_pane") is True, after
            assert after.get("generation") == before.get("generation") + 1, (before, after)
            ctx["bridge1"] = dict(after)
            return {"before": before, "after": after, "enters": enters}

        def no_replay():
            """C-D70 (5): a new manager session gets exactly one manager_recovery notice (one manager turn) and
            nothing that was already delivered is sent again (reports_resent 0, no injected message)."""
            rig.pump(12.0)
            snap = rig.status()
            delta_req = {role: rig.p.count(role) - ctx["requests_before"][role] for role in ("manager", "worker")}
            delta_inj = {role: len(rig.injected(role)) - ctx["injected_before"][role] for role in ("manager", "worker")}
            assert delta_inj == {"manager": 0, "worker": 0}, {"replayed/injected after the switch": delta_inj}
            assert delta_req == {"manager": 1, "worker": 0}, {"model requests after the switch": delta_req}
            new_texts = ctx["manager_texts"][ctx["texts_before"]:]
            assert len(new_texts) == 1, new_texts
            _, was_injected, last_role, text = new_texts[0]
            assert not was_injected and last_role == "user", new_texts[0][:3]
            assert "manager_recovery" in text and "your OMP session is new" in text, text[-600:]
            resent = re.search(r'"reports_resent"\s*:\s*(\d+)', text)
            assert resent and int(resent.group(1)) == 0, text[-600:]
            assert ctx["bridge1"]["session_id"] in text, "the notice does not name the new session"
            notices = [j for j in rig.journal()[ctx["journal_before"]:]
                       if j.get("type") == "workbench_notice" and j.get("notice_type") == "manager_recovery"]
            outcomes = sorted(j.get("outcome") for j in notices)
            assert outcomes == ["queued", "sent"], notices
            task = {k: (snap.get("task") or {}).get(k) for k in ("task_id", "revision", "active", "status")}
            assert task == ctx["task_before"], {"before": ctx["task_before"], "after": task}
            assert snap["bridge"]["manager"].get("session_id") == ctx["bridge1"].get("session_id"), snap["bridge"]
            return {"requests_delta": delta_req, "injected_delta": delta_inj, "task": task,
                    "recovery_notice": {"reports_resent": 0, "journal": outcomes}}

        def delivered_to_new_session_once():
            rig.say("worker_omp", "gap-s-done")
            assert second.wait(60), second.results
            assert second.results[0]["status"] == "queued", second.results
            got = rig.wait_injected("manager", lambda i: (i.get("payload") or {}).get("kind") == "done"
                                    and i.get("task_id") == ctx["tid"], 60, "done after the switch")
            closed = rig.wait_task(lambda t: t.get("task_id") == ctx["tid"] and t.get("status") == "closed", 60,
                                   "Task closed done")
            rig.pump(3.0)
            firsts = rig.injected("manager", lambda i: i.get("workbench_message_id") == ctx["first_id"])
            assert len(firsts) == 1, f"the pre-switch report was delivered {len(firsts)} times"
            dones = rig.injected("manager", lambda i: (i.get("payload") or {}).get("kind") == "done")
            assert len(dones) == 1, f"done delivered {len(dones)} times"
            session_fields = {k: got.get(k) for k in ("session_id", "session_generation") if k in got}
            if session_fields:
                assert session_fields.get("session_id", ctx["bridge1"]["session_id"]) == ctx["bridge1"]["session_id"], \
                    session_fields
            return {"closed_reason": closed.get("closed_reason"), "envelope_session": session_fields or None}

        rig.step("start", start)
        rig.step("SESSION_delivery_before_switch", delivered_before_switch, ("start",))
        rig.step("SESSION_new_reregisters_same_process", switch, ("SESSION_delivery_before_switch",))
        rig.step("SESSION_no_replay_one_recovery_notice", no_replay, ("SESSION_new_reregisters_same_process",))
        rig.step("SESSION_next_report_to_new_session_once", delivered_to_new_session_once,
                 ("SESSION_new_reregisters_same_process",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)


if __name__ == "__main__":
    unittest.main()
