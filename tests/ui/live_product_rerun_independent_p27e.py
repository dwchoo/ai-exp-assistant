"""Independent CW-06 live re-verification (p27-cw06-rerun-test-02): real entrypoint on owned PTYs.

Reuses the p27-cw06-test-01 harness (LiveTerm, teardown with exact-identity residue check).
- F1-sh: host shell is dash (PATH without bash, as the CW-17 START test): bracketed paste markers must
  not reach `cat > f` nor the dash prompt.  Also bash `cat > f` plain text (S5).
- F6-ui: after a typed multi-line heredoc, `wb-handoff` + prefix h refusal notice shows held reasons.
- P-C-AC-14 (real OMP, at most 2 tiny model turns in total): (a) Esc cancels a manager turn, visible in the
  pane; (b) approval/confirmation prompt through the UI, or not-applicable with evidence.
Run: PYTHONPATH=src:tests/ui python -m unittest tests/ui/live_product_rerun_independent_p27e.py
Model turns (tests 4/5) run only with CW06_LIVE_MODEL=1 (default: skipped).
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_product_omp_independent as L  # noqa: E402

from workbench.contracts.v1 import PaneId  # noqa: E402

MGR, SH = PaneId.MANAGER_OMP, PaneId.HOST_SHELL
PREFIX, START, END = L.PREFIX, L.START, L.END


def new_lines(before: str, after: str) -> list[str]:
    seen = {line.strip() for line in before.splitlines()}
    return [line.strip()[:160] for line in after.splitlines() if line.strip() and line.strip() not in seen]


@unittest.skipIf(L.SKIP, f"real OMP unavailable: {L.SKIP}")
class RerunLiveProbe(L.LiveProductOmpProbe):
    test_live_product_ui_with_real_omp = None  # inherited probe is not re-run here
    turns = 0

    def setUp(self):
        super().setUp()
        base = os.environ.get("CW06_LIVE_EVIDENCE_DIR")
        if base:
            self.evidence_path = Path(base) / f"{self._testMethodName}.json"

    def no_bash_env(self) -> None:
        bindir = self.root / "bin"
        bindir.mkdir()
        for name, target in (("sh", "/usr/bin/dash"), ("omp", L.OMP), ("cat", "/usr/bin/cat"),
                             ("stty", "/usr/bin/stty"), ("sleep", "/usr/bin/sleep"), ("setsid", "/usr/bin/setsid")):
            (bindir / name).symlink_to(target)
        self.env["PATH"] = str(bindir)
        self.env["SHELL"] = "/bin/false"

    def boot(self):
        ui = self.term("start", "start", "--data-dir", str(self.data), "--omp", L.OMP, "--omp-arg=--no-session")
        self.assertTrue(ui.wait(lambda: "bridge manager=ok worker=ok" in ui.text(), 120), ui.line(1)[:150])
        snap = self.wait_status(lambda s: s["phase"] == "ready" and s["attached"])
        self.remember(snap)
        self.assertTrue(ui.wait(lambda: "HOST SHELL" in ui.text(), 30))
        ui.send(PREFIX + b"3")
        self.wait_status(lambda s: s["focus"] == "host_shell")
        ui.pause(1.0)
        return ui, snap

    def paste_cat(self, ui, name: str, text: str) -> bytes:
        P = self.project
        ui.send(f"cat > {P}/{name}\r".encode())
        ui.pause(0.8)
        ui.send(START + text.encode() + END)
        ui.pause(0.4)
        ui.keys(b"\r", b"\x04")
        ui.wait(lambda: (P / name).exists() and (P / name).read_bytes().endswith(b"\n"), 6)
        ui.pause(0.5)
        return (P / name).read_bytes() if (P / name).exists() else b""

    # -- F1 sh -------------------------------------------------------------
    def test_f1_sh_no_bracketed_markers_reach_dash_shell_programs(self):
        self.no_bash_env()
        P = self.project
        ui, snap = self.boot()
        shell = snap["panes"]["host_shell"]["shell"]
        exe = os.readlink(f"/proc/{shell['parent']['pid']}/exe")
        raw_cat = self.paste_cat(ui, "cat_sh.txt", "붙여 abc")
        # paste at the dash prompt itself, then Enter: markers would make the command name garbage
        ui.send(START + f"echo DASH-PASTE-OK > {P}/dp.txt".encode() + END)
        ui.pause(0.5)
        ui.send(b"\r")
        ui.wait(lambda: (P / "dp.txt").exists(), 6)
        dp = (P / "dp.txt").read_bytes() if (P / "dp.txt").exists() else b""
        self.step("F1_sh", shell_kind=shell["kind"], exe=exe, cat_bytes=raw_cat.decode(errors="replace"),
                  dash_prompt_file=dp.decode(errors="replace"),
                  screen_esc_marker="200~" in ui.text() or "201~" in ui.text())
        self.assertEqual((shell["kind"], exe), ("sh", "/usr/bin/dash"))
        self.assertEqual(raw_cat, "붙여 abc\n".encode(), "markers/garbage reached cat in sh")
        self.assertNotIn(b"\x1b", raw_cat)
        self.assertEqual(dp, b"DASH-PASTE-OK\n", "markers reached the dash command line")
        ui.send(PREFIX + b"d")
        ui.wait(ui.done, 15)

    # -- S5 bash + F6 -------------------------------------------------------
    def test_bash_cat_plain_and_f6_held_reasons_after_heredoc(self):
        P = self.project
        ui, snap = self.boot()
        self.assertEqual(snap["panes"]["host_shell"]["shell"]["kind"], "bash")
        raw_cat = self.paste_cat(ui, "cat_bash.txt", "붙여 abc")
        self.step("S5_bash_cat", bytes=raw_cat.decode(errors="replace"))
        self.assertEqual(raw_cat, "붙여 abc\n".encode())
        # multi-line heredoc typed key by key in the host shell, then wb-handoff + prefix h
        for chunk in (f"cat > {P}/h.txt <<'EOF'", "한글 줄 1", "line 2", "EOF"):
            ui.send(chunk.encode() + b"\r")
            ui.pause(0.4)
        ui.wait(lambda: (P / "h.txt").exists(), 5)
        ui.pause(0.5)
        heredoc_ok = (P / "h.txt").exists() and (P / "h.txt").read_text() == "한글 줄 1\nline 2\n"
        ui.send(b"wb-handoff\r")
        ui.pause(2.0)
        s0 = self.status()["panes"]["host_shell"]["shell"]
        ui.send(PREFIX + b"h")
        ui.pause(1.5)
        s1 = self.status()["panes"]["host_shell"]["shell"]
        footer = ui.line(ui.rows - 1).strip()[:200]
        self.step("F6_ui", heredoc_ok=heredoc_ok, owner_before=s0["input_owner"], owner_after=s1["input_owner"],
                  held_reasons_backend=s1.get("held_reasons") or s0.get("held_reasons"), footer=footer,
                  held_in_screen=excerpt_held(ui.text()))
        self.assertTrue(heredoc_ok)
        if s1["input_owner"] == "manager":
            # boundary proven this time: handoff legitimately succeeded, so no refusal to inspect
            self.step("F6_ui_note", note="handoff succeeded; refusal path not reached")
            self.fail("handoff was not held after the heredoc; F6 refusal notice not exercised: " + footer)
        self.assertIn("held", footer, footer)
        reasons = s1.get("held_reasons") or s0.get("held_reasons") or []
        for reason in reasons:
            self.assertIn(reason, footer)
        ui.send(PREFIX + b"d")
        ui.wait(ui.done, 15)

    # -- P-C-AC-14 with the real OMP ---------------------------------------
    @unittest.skipIf(not L.MODEL_TURNS, L.MODEL_SKIP_REASON)
    def test_ac14_esc_cancel_and_approval_prompt_real_omp(self):
        ui, _ = self.boot()
        ui.send(PREFIX + b"1")
        self.wait_status(lambda s: s["focus"] == "manager_omp")

        def pane() -> str:
            return ui.pane(MGR)

        def top() -> int:
            runs = re.findall(r"(?:\b\d{1,3}\s+){4,}\d{1,3}\b", pane())
            return max([int(n) for run in runs for n in run.split()] or [0])

        # (a) turn 1: long counting turn, Esc while streaming
        self.turns += 1
        ui.send(b"Count from 1 to 600 as digits separated by single spaces, one line, nothing else.")
        ui.pause(0.3)
        ui.send(b"\r")
        streaming = ui.wait(lambda: re.search(r"\b1 2 3 4 5 6\b", pane()) is not None and top() >= 20, 90)
        before_txt = pane()
        at_esc = top()
        ui.send(b"\x1b")
        ui.pause(2.5)
        after_2 = top()
        ui.pause(2.5)
        after_5 = top()
        text = pane()
        added = [l for l in new_lines(before_txt, text) if not re.fullmatch(r"[\d\s]+", l)]
        markers = [l for l in added if re.search(r"bort|nterrupt|ancel|topped|Esc", l)]
        stopped = after_5 == after_2 and after_5 < 600
        self.step("AC14a_esc_cancel", streaming_seen=streaming, top_at_esc=at_esc, top_2s=after_2, top_5s=after_5,
                  reached_600=bool(re.search(r"\b599 600\b", text)), stopped=stopped, cancel_lines=markers,
                  other_new_lines=added[:6], alive=self.status()["panes"]["manager_omp"]["alive"])
        self.assertTrue(streaming)
        self.assertTrue(stopped, "output kept growing / completed after Esc")
        self.assertTrue(markers, f"no visible cancel marker in the pane; new lines: {added[:6]}")

        # (b) turn 2: harmless read-only tool; answer an approval prompt through the UI if one appears
        self.turns += 1
        self.assertLessEqual(self.turns, 2)
        ui.send(b"\x15")
        before = pane()
        ui.send(b"Use your shell tool to run exactly: true   Then reply with only the word DONEP27.")
        ui.pause(0.3)
        ui.send(b"\r")
        prompt_re = re.compile(r"Allow|Approve|approve|Confirm|confirm|\[y/n\]|Yes|Deny|permission|Permission", re.I)
        ask = lambda: [l for l in new_lines(before, pane()) if prompt_re.search(l) and "Use your shell tool" not in l]  # noqa: E731
        done = lambda: re.search(r"DONEP27", "\n".join(new_lines(before, pane()))) is not None  # noqa: E731
        ui.wait(lambda: bool(ask()) or done(), 120)
        prompt_lines = ask()
        prompt_screen = new_lines(before, pane())[-14:] if prompt_lines else []
        answered = None
        if prompt_lines and not done():
            ui.send(b"\r")  # the highlighted default of a confirm dialog; recorded, not assumed
            answered = "Enter"
            ui.wait(done, 90)
        replied = done()
        ran = [l for l in new_lines(before, pane()) if re.search(r"\btrue\b", l) and "Use your shell tool" not in l][:4]
        self.step("AC14b_approval", prompt_seen=bool(prompt_lines), prompt_lines=prompt_lines[:4],
                  prompt_screen=prompt_screen, answered_with=answered, reply_seen=replied, tool_lines=ran,
                  model_turns_used=self.turns,
                  verdict=("answered" if prompt_lines and replied else
                           "not_applicable_auto_approved" if replied and not prompt_lines else "inconclusive"))
        self.assertTrue(replied, "manager never produced the tool turn reply")
        ui.send(PREFIX + b"d")
        ui.wait(ui.done, 15)


    # -- P-C-AC-14 redesigned: ONE turn, tool approval (b) + Esc cancel of a running tool (a) --------------
    @unittest.skipIf(not L.MODEL_TURNS, L.MODEL_SKIP_REASON)
    def test_ac14_single_turn_sleep_tool_approval_and_esc(self):
        ui, _ = self.boot()
        ui.send(PREFIX + b"1")
        self.wait_status(lambda s: s["focus"] == "manager_omp")
        omp_pid = self.known["manager_omp"][0]

        proj = str(self.project)
        seen_cands: dict[int, dict] = {}

        def sleepers() -> dict[int, int]:
            ppid, comm = {}, {}
            for name in os.listdir("/proc"):
                if name.isdigit():
                    try:
                        ppid[int(name)] = int(Path(f"/proc/{name}/stat").read_bytes().rsplit(b") ", 1)[1].split()[1])
                        comm[int(name)] = Path(f"/proc/{name}/comm").read_text().strip()
                    except (OSError, IndexError):
                        pass
            found = {}
            for pid in list(ppid):
                try:
                    cl = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                    if cl[:2] != [b"sleep", b"25"]:
                        continue
                    start = int(Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()[19])
                    cwd = os.readlink(f"/proc/{pid}/cwd")
                except (OSError, IndexError):
                    continue
                chain, node = [], pid
                for _ in range(12):
                    node = ppid.get(node, 0)
                    chain.append((node, comm.get(node)))
                    if node <= 1:
                        break
                if cwd == proj or any(n == omp_pid for n, _ in chain):
                    found[pid] = start
                    seen_cands[pid] = {"cwd_is_project": cwd == proj, "chain": chain, "start": start}
            return found

        pane = lambda: ui.pane(MGR)  # noqa: E731
        self.turns += 1
        before = pane()
        ui.send(b"Use your shell tool to run exactly this command and nothing else: sleep 25; echo WBDONE7")
        ui.pause(0.3)
        ui.send(b"\r")
        prompt_re = re.compile(r"Allow|Approve|Confirm|\[y/n\]|\bYes\b|\bDeny\b|permission", re.I)
        echo = lambda l: "Use your shell tool" in l  # noqa: E731
        ask = lambda: [l for l in new_lines(before, pane()) if prompt_re.search(l) and not echo(l)]  # noqa: E731
        out = lambda: [l for l in new_lines(before, pane()) if "WBDONE7" in l and "sleep" not in l and not echo(l)]  # noqa: E731
        t0 = time.monotonic()
        box = lambda: any(re.match(r"[│|]\s*\$ sleep 25", l) for l in new_lines(before, pane()))  # noqa: E731
        ui.wait(lambda: bool(ask()) or bool(sleepers()) or box() or bool(out()), 120)
        prompt_lines = ask()
        prompt_screen = new_lines(before, pane())[-16:] if prompt_lines else []
        answered = after_answer = None
        if prompt_lines and not sleepers():
            ui.send(b"\r")
            answered = "Enter"
            ui.wait(lambda: bool(sleepers()), 60)
            after_answer = new_lines(before, pane())[-16:]
        running = sleepers()
        ui.pause(3.5)
        running_screen = new_lines(before, pane())[-16:]
        still_running = sleepers()
        t_esc = time.monotonic()
        ui.send(b"\x1b")
        gone_at = None
        while time.monotonic() - t_esc < 10:
            ui.pause(0.25)
            if gone_at is None and not any(sleepers().get(p) == st for p, st in running.items()):
                gone_at = round(time.monotonic() - t_esc, 1)
        after_screen = new_lines(before, pane())[-16:]
        markers = [l for l in new_lines(before, pane()) if re.search(r"bort|nterrupt|ancel|topped", l) and not echo(l)]
        ui.wait(lambda: bool(out()), max(0.0, 40 - (time.monotonic() - t_esc)))
        wbdone = out()
        left = [p for p, st in running.items() if L.ticks(p) == st]
        self.step("AC14_single_turn", model_turns_used=self.turns, prompt_seen=bool(prompt_lines),
                  prompt_lines=prompt_lines[:4], prompt_screen=prompt_screen, answered_with=answered,
                  screen_after_answer=after_answer, sleep_seen_running=bool(running), sleep_ids=sorted(running.items()), candidates={str(k): v for k, v in seen_cands.items()}, tool_box_seen=box(),
                  still_running_before_esc=bool(still_running), screen_running=running_screen,
                  sleep_gone_after_esc_s=gone_at, screen_after_esc=after_screen, interruption_lines=markers,
                  wbdone_output_lines=wbdone, sleep_left_by_identity=left, seconds_to_running=round(t_esc - t0, 1),
                  b_verdict=("answered_via_ui" if prompt_lines else "not_applicable_no_prompt"))
        self.assertTrue(running, "tool never started (sleep child of OMP not seen)")
        self.assertTrue(still_running, "sleep was not still running at Esc time")
        self.assertFalse(wbdone, "WBDONE7 appeared: Esc did not cancel")
        self.assertIsNotNone(gone_at, "sleep child not gone within 10 s of Esc")
        self.assertEqual(left, [])
        self.assertTrue(markers, "no interruption/abort state visible in pane")
        ui.send(PREFIX + b"d")
        ui.wait(ui.done, 15)


def excerpt_held(text: str) -> list[str]:
    return [line.strip()[:200] for line in text.splitlines() if "held" in line]


if __name__ == "__main__":
    unittest.main()
