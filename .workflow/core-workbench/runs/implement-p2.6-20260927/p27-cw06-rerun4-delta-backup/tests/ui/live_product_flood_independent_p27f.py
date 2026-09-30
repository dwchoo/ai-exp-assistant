"""Independent CW-06 runtime probe (p27-cw06-rerun-test-03): P2-1 host-shell flood through the real entrypoint.

Real ``python -m workbench start --no-attach`` + ``attach`` (real backend + real host shell) with a STUB OMP (no provider, no
credentials, zero model turns), product UI in a PTY this probe owns, isolated data dir / project dir / HOME.
In the host shell: a plain flood, then a coloured Korean flood held for >= 20 s with Ctrl-C typed mid-flood.

Pass: Ctrl-C reaches the shell and stops the flood within 1 s; no traceback; either no slow_client detach or
a visible reason (recorded which); catch-up indicator seen; prompt usable and screen shows the latest tail
afterwards; backend / pane identities unchanged.

Run: PYTHONPATH=src:tests/backend python -m unittest tests/ui/live_product_flood_independent_p27f.py
Evidence JSON: $CW06_FLOOD_EVIDENCE (default: printed only).
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import termios
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests" / "backend"))

from live_harness import PtyRun, kill_exact, session_members, ticks  # noqa: E402
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream  # noqa: E402

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
'''
ROWS, COLS = 30, 110
PREFIX = b"\x1d"
BASH = "/bin/bash"


class Watch:
    """Reads a PtyRun continuously. Raw bytes are scanned for tokens; the repo VT screen is fed only while
    ``feed`` is on (pyte cannot keep up with a flood's redraw stream), and ``repaint()`` rebuilds it from a
    full UI redraw (SIGWINCH -> ``win.clear()``), so the screen text is always what the UI itself holds."""

    NEEDLES = {"catchup": "따라잡음".encode(), "traceback": b"Traceback", "slow_client": b"slow_client",
               "closing_msg": b"[workbench]"}

    def __init__(self, run: PtyRun):
        self.run = run
        self.rows, self.cols = ROWS, COLS
        self.feed = True
        self.reset_screen()
        self.tail = b""
        self.seen = {key: False for key in self.NEEDLES}
        self.tokens: dict[bytes, float] = {}  # extra needle -> monotonic time first seen
        self.total = 0

    def reset_screen(self) -> None:
        self.screen = TerminalScreen(self.cols, self.rows)
        self.stream = make_stream(self.screen)

    def watch_token(self, token: str) -> None:
        self.tokens.setdefault(token.encode(), 0.0)

    def token_seen(self, token: str) -> float | None:
        return self.tokens.get(token.encode()) or None

    def pump(self, timeout: float = 0.02) -> None:
        for _ in range(256):
            self.run.drain(timeout)
            data = bytes(self.run.output)
            if not data:
                return
            self.run.output.clear()
            timeout = 0
            now = time.monotonic()
            self.total += len(data)
            window = self.tail + data
            for key, needle in self.NEEDLES.items():
                if needle in window:
                    self.seen[key] = True
            for needle, first in self.tokens.items():
                if not first and needle in window:
                    self.tokens[needle] = now
            self.tail = window[-256:]
            if self.feed:
                self.stream.feed(data)

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def wait(self, pred, timeout: float) -> float | None:
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            self.pump(0.02)
            if pred():
                return time.monotonic() - start
        self.pump(0)
        return time.monotonic() - start if pred() else None

    def repaint(self) -> str:
        """Force a full UI repaint (resize round trip) and return the screen the UI now shows."""
        self.feed = False
        self.pump(0.3)
        self.reset_screen()
        self.feed = True
        for cols in (self.cols - 1, self.cols):
            fcntl.ioctl(self.run.fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, cols, 0, 0))
            self.pump(0.6)
        return self.text()

    def send(self, data: bytes) -> None:
        self.run.send(data)


def proc_by_cmdline(needle: bytes) -> list[tuple[int, int]]:
    found = []
    for name in os.listdir("/proc"):
        if name.isdigit() and int(name) != os.getpid():
            try:
                if needle in Path(f"/proc/{name}/cmdline").read_bytes():
                    start = ticks(int(name))
                    if start is not None:
                        found.append((int(name), start))
            except OSError:
                continue
    return found


class FloodRuntime(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw06-flood-", dir="/tmp"))
        os.chmod(self.root, 0o700)
        self.data, self.project, self.home = self.root / "d", self.root / "project", self.root / "home"
        self.project.mkdir()
        self.home.mkdir()
        self.omp = self.root / "omp"
        self.omp.write_text(STUB)
        self.omp.chmod(0o755)
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "SHELL": BASH, "LANG": "C.UTF-8",
                    "TERM": "xterm-256color", "PYTHONPATH": str(REPO / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        self.run_: PtyRun | None = None
        self.known: dict[str, tuple[int, int]] = {}
        self.sessions: set[int] = set()
        self.residue: dict = {}
        self.ev: dict = {"model_turns": 0, "steps": {}}

    def cli(self, *args, timeout=60):
        import subprocess
        return subprocess.run([sys.executable, "-m", "workbench", *args], env=self.env, cwd=self.project,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def status(self):
        out = json.loads(self.cli("status", "--data-dir", str(self.data), "--json", timeout=30).stdout)
        return out["snapshot"] if out.get("running") else None

    def identities(self, snapshot) -> dict:
        ident = {"backend": (snapshot["backend"]["process"]["pid"], snapshot["backend"]["process"]["start_ticks"])}
        for name, pane in snapshot["panes"].items():
            if pane.get("process"):
                ident[name] = (pane["process"]["pid"], pane["process"]["start_ticks"])
        return ident

    def tearDown(self):
        if self.run_ is not None:
            self.run_.close()
        try:
            self.cli("shutdown", "--data-dir", str(self.data), "--yes", "--json", timeout=60)
        except Exception:  # noqa: BLE001 - fall through to exact-identity kill
            pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(ticks(pid) == start for pid, start in self.known.values()):
            time.sleep(0.1)  # shutdown returns when the backend acknowledged; allow it to exit
        left = {name: pid for name, (pid, start) in self.known.items() if ticks(pid) == start}
        members = session_members(self.sessions)
        needle = str(self.root).encode()
        strays = [(p, s) for p, s in proc_by_cmdline(needle)]
        self.residue = {"identities": left, "session_members": members, "cmdline_matches": strays}
        for pid, start in list(self.known.values()) + list(members.items()) + strays:
            kill_exact(pid, start)
        time.sleep(0.2)
        shutil.rmtree(self.root, ignore_errors=True)
        self.residue["root_exists"] = self.root.exists()
        evidence = os.environ.get("CW06_FLOOD_EVIDENCE")
        self.ev["residue"] = self.residue
        print("FLOOD-EVIDENCE " + json.dumps(self.ev, ensure_ascii=False, default=str))
        if evidence:
            Path(evidence).write_text(json.dumps(self.ev, ensure_ascii=False, indent=1, default=str))
        self.assertFalse(any(self.residue[k] for k in ("identities", "session_members", "cmdline_matches", "root_exists")),
                         f"residue after normal-path cleanup: {self.residue}")

    def test_host_shell_floods_keep_ui_responsive(self):
        # Real entrypoint: `start --no-attach` (the stub OMP never loads the bridge, so the backend stays in
        # `starting`), then the product UI via `attach` on a PTY this probe owns.
        started = self.cli("start", "--data-dir", str(self.data), "--omp", str(self.omp), "--no-attach",
                           "--timeout", "3")
        self.assertIn("starting backend", started.stdout + started.stderr)
        self.run_ = PtyRun([sys.executable, "-m", "workbench", "attach", "--data-dir", str(self.data)], self.env,
                           str(self.project), size=(ROWS, COLS))
        w = Watch(self.run_)
        self.assertIsNotNone(w.wait(lambda: "HOST SHELL" in w.text() and "STUB-OMP worker ready" in w.text(), 40),
                             w.text())
        snap0 = self.status()
        self.assertIsNotNone(snap0)
        ids0 = self.identities(snap0)
        self.known.update(ids0)
        self.sessions.update(pid for pid, _ in ids0.values())
        self.sessions.add(snap0["backend"]["session_id"])
        ui_ticks = self.run_.ticks

        # Focus the host shell, make the prompt recognisable.
        w.send(PREFIX + b"3")
        self.assertIsNotNone(w.wait(lambda: "focus: HOST SHELL" in w.text(), 10), w.text())
        w.send(b"PS1='PROMPT> '; clear\r")
        self.assertIsNotNone(w.wait(lambda: "PROMPT>" in w.text(), 15), w.text())

        def usable(tag: str) -> bool:
            """A fresh command round-trips through the shell (output token differs from the echoed command)."""
            w.send(f"echo USABLE_$((40+2))_{tag}\r".encode())
            return w.wait(lambda: f"USABLE_42_{tag}" in w.text().replace("\n", ""), 10) is not None

        def after_flood(name: str, token: str, needle: bytes) -> None:
            """Flood process gone, then a full UI repaint (retried for <= 90 s; lag recorded) must show marker + prompt."""
            gone_after = w.wait(lambda: not self.alive(needle) or self.run_.poll() is not None, 90)
            w.pump(0.5)
            self.ev["steps"].setdefault(name, {})["ui_exit_status"] = self.run_.poll()
            self.assertIsNone(self.run_.poll(), f"{name}: UI exited mid-flood (slow_client detach?): "
                              f"slow_client_text_seen={w.seen['slow_client']} message="
                              f"{w.tail[w.tail.rfind(b'[workbench]'):].decode('utf-8', 'replace')!r}")
            self.ev["steps"].setdefault(name, {})["flood_process_ended"] = gone_after is not None
            self.assertIsNotNone(gone_after, f"{name}: flood process still running")
            ended_at = time.monotonic()
            deadline, screen = ended_at + 90, ""
            while time.monotonic() < deadline:
                screen = w.repaint().replace("\n", "")
                if token in screen and "PROMPT>" in screen:
                    break
                w.pump(2.0)
            self.ev["steps"][name]["ui_tail_lag_after_flood_end_s"] = round(time.monotonic() - ended_at, 1)
            self.assertIn(token, screen, f"{name}: latest tail not on the repainted screen: {w.text()[-600:]}")
            self.assertIn("PROMPT>", screen)
            self.assertTrue(usable(name), f"{name}: prompt not usable")
            w.feed = False

        # --- F0: the packet's literal flood -----------------------------------------------------------------
        w.feed = False
        w.watch_token("F0_DONE_42")
        t0 = time.monotonic()
        w.send(b"yes | head -c 200000000; echo F0_DONE_$((6*7))\r")
        self.ev["steps"]["F0_200MB"] = {}
        after_flood("F0_200MB", "F0_DONE_42", b"head\x00-c\x00200000000")
        self.ev["steps"]["F0_200MB"].update(seconds=round(time.monotonic() - t0, 1), catchup_seen=w.seen["catchup"])

        # --- F1: plain flood sustained >= 20 s, natural end ---------------------------------------------------
        w.watch_token("F1_DONE_42")
        w.feed = False
        w.send(b"timeout 22 yes plainflood_f1; echo F1_DONE_$((6*7))\r")
        t1 = time.monotonic()
        self.assertIsNotNone(w.wait(lambda: self.alive(b"plainflood_f1"), 10), "F1 flood never started")
        self.ev["steps"]["F1_plain_22s"] = {}
        after_flood("F1_plain_22s", "F1_DONE_42", b"plainflood_f1")
        self.ev["steps"]["F1_plain_22s"]["seconds"] = round(time.monotonic() - t1, 1)
        self.assertGreaterEqual(time.monotonic() - t1, 20)

        # --- F2: coloured Korean flood, Ctrl-C at >= 20 s mid-flood ---------------------------------------------
        w.watch_token("F2_DONE_42")
        w.feed = False
        w.send("timeout 45 yes $'\\e[1;31m한글 색상 flood\\e[0m F2MARK'; echo F2_DONE_$((6*7))\r".encode())
        t2 = time.monotonic()
        self.assertIsNotNone(w.wait(lambda: self.alive(b"F2MARK"), 10), "F2 flood never started")
        while time.monotonic() - t2 < 20:
            w.pump(0.05)
            self.assertIsNone(self.run_.poll(), "UI exited during the flood: " + repr(w.tail))
        self.assertTrue(self.alive(b"F2MARK"), "flood ended by itself before Ctrl-C")
        victims = proc_by_cmdline(b"F2MARK")
        t_ctrl_c = time.monotonic()
        w.send(b"\x03")
        gone = None
        while time.monotonic() - t_ctrl_c < 5:
            w.pump(0.01)
            if not any(ticks(pid) == start for pid, start in victims):
                gone = time.monotonic() - t_ctrl_c
                break
        step = self.ev["steps"]["F2_ctrl_c"] = {
            "flood_seconds_before_ctrl_c": round(t_ctrl_c - t2, 1),
            "flood_process_gone_after_s": None if gone is None else round(gone, 3)}
        self.assertIsNotNone(gone, "flood still running 5 s after Ctrl-C")
        self.assertLess(gone, 1.0, f"Ctrl-C took {gone:.2f}s to stop the flood")
        # Interactive bash aborts the whole `cmd; echo TOKEN` list on Ctrl-C (confirmed in a plain bash PTY), so no
        # F2_DONE marker is expected. Same intent: flood gone (<1 s, above), '^C' + fresh prompt visible in the
        # host pane within 2 s, and a NEW command typed through the UI shows its output within 1 s.
        self.assertIsNone(self.run_.poll(), "F2: UI exited after Ctrl-C (slow_client detach?) " + repr(w.tail))

        def safe_text() -> str:  # pyte can raise IndexError on wide-char cells right after a resize repaint
            try:
                return w.text().replace("\n", "")
            except IndexError:
                return ""

        w.feed = False
        w.watch_token("F2_OK_42")
        prompt_after = None
        while time.monotonic() - t_ctrl_c < 2.0:
            try:
                w.repaint()
            except IndexError:
                pass  # repaint() returns w.text(); the resize round trip itself completed
            screen = safe_text()
            if "^C" in screen and "PROMPT>" in screen:
                prompt_after = time.monotonic() - t_ctrl_c
                break
        step["ctrl_c_and_prompt_visible_after_s"] = None if prompt_after is None else round(prompt_after, 2)
        self.assertIsNotNone(prompt_after, f"F2: '^C' and a fresh prompt not on the host pane within 2 s: {safe_text()[-600:]}")
        w.send(b"echo F2_OK_$((6*7))\r")  # the echoed command line reads F2_OK_$((6*7)); only the output has F2_OK_42
        t_new = time.monotonic()
        shown = w.wait(lambda: w.token_seen("F2_OK_42") is not None, 1.0)
        step["new_command_output_after_s"] = None if shown is None else round(shown, 3)
        self.assertIsNotNone(shown, "F2: new command output not shown within 1 s")
        step.update(catchup_seen=w.seen["catchup"], slow_client_seen=w.seen["slow_client"],
                    traceback=w.seen["traceback"])

        # --- global assertions ----------------------------------------------------------------------------------
        self.assertFalse(w.seen["traceback"], "traceback on screen")
        self.assertTrue(w.seen["catchup"], "catch-up indicator never shown during 3 floods")
        self.assertIsNone(self.run_.poll(), "UI process exited; slow_client detach? reason: " + repr(w.tail))
        self.assertEqual(ticks(self.run_.pid), ui_ticks, "UI identity changed")
        snap1 = self.status()
        self.assertIsNotNone(snap1)
        self.assertEqual(self.identities(snap1), ids0, "backend/pane identities changed")
        self.ev["identities_unchanged"] = True
        self.ev["slow_client_detach"] = w.seen["slow_client"]
        self.ev["ui_bytes_seen"] = w.total

        # --- orderly end: detach the UI, then shutdown --yes ----------------------------------------------------
        w.feed = False
        w.send(PREFIX + b"d")
        end = time.monotonic() + 10
        while self.run_.poll() is None and time.monotonic() < end:
            w.pump(0.05)
        self.assertEqual(self.run_.poll(), 0)

    @staticmethod
    def alive(needle: bytes) -> bool:
        return bool(proc_by_cmdline(needle))


if __name__ == "__main__":
    unittest.main()
