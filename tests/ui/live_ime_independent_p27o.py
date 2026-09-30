"""LIVE IME-neutral prefix check (p27-ime-test-01), opt-in: ``WB_LIVE_IME=1`` (tmux 3.4 and herdr must be installed).

Run (repo root, short scratch TMPDIR)::

    WB_LIVE_IME=1 PYTHONPATH=src:tests/ui TMPDIR=/tmp/wbp27o /tmp/cw02-g1-venv/bin/python -m unittest \\
        tests.ui.live_ime_independent_p27o -v

What a Korean-IME client sends (Ctrl combos, Space, digits, arrows, Hangul UTF-8 split over reads) is written into a real
multiplexer client (an outer PTY this test owns, rendered with pyte) and checked in two ways:

1. ``tap``: a raw byte recorder runs inside the multiplexer pane; the bytes it reads must equal the bytes sent, so tmux/
   herdr pass Ctrl-] + Ctrl-d/t/y/o/r/e/z, Space, digits, arrows, Enter/Tab/Esc and Hangul unchanged.
2. ``product``: the real entrypoint (``python -m workbench start --omp <stub>`` + ``attach``) runs inside the multiplexer.
   The stub OMP echoes every byte it receives (``[manager got b'..']``) and never contacts a provider (no model, no
   credentials, no network). Ctrl-] Ctrl-d detaches (exit 0, backend survives), Ctrl-] Space opens the menu and a digit read
   from the screen detaches, Esc closes the menu, Hangul after the prefix shows the hint and reaches no pane.

Isolation: tmux runs as ``tmux -L <unique> -f <own conf>`` with ``TMUX_TMPDIR`` in the scratch root; herdr runs with its own
``XDG_CONFIG_HOME`` (own server/socket/log) and a minimal own config (the user's herdr config is not read or copied); no TMUX*/HERDR*
variable of the caller is passed on. Servers are stopped by their own command and verified gone by pid + start ticks; every
process naming the scratch root is checked at the end (survivors are failures).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from live_mux_independent_p27n import (OuterTerminal, clean_env, kill_identity, note, owned_processes,  # noqa: E402
                                       ticks)

REPO = HERE.parents[1]
SRC = str(Path(os.environ.get("P27O_SRC") or REPO / "src"))
LIVE = os.environ.get("WB_LIVE_IME") == "1"
PREFIX = b"\x1d"
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"
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
TAP = '''import os, sys, tty
out = open(sys.argv[1], "ab", buffering=0)
tty.setraw(0)
os.write(1, b"TAP-READY\\r\\n")
while True:
    data = os.read(0, 4096)
    if not data or b"\\x00" in data:
        out.write(data.split(b"\\x00")[0]); break
    out.write(data)
os.write(1, b"TAP-DONE\\r\\n")
'''


def ctrl(letter: str) -> bytes:
    return bytes([ord(letter) - 96])


# what an IME client sends for the IME-neutral routes, each as separate writes (a read boundary between them)
TAP_CHUNKS = [PREFIX + ctrl(c) for c in "dtyorez"] + [PREFIX, b" ", PREFIX + b"1", PREFIX + b"0", PREFIX + b"\t",
                                                       b"\x1b[A", b"\x1b[B", b"\x1bOA", b"\x1bOB", b"\r", b"\n", b"1234567890",
                                                       PREFIX + PREFIX, b"\x03\x1c", PREFIX + b"[", PREFIX + b"=", PREFIX + b"?",
                                                       PREFIX + "한".encode()[:1], "한".encode()[1:], "ㅇ".encode(),
                                                       "안녕".encode()[:4], "안녕".encode()[4:], b"\x1b"]


class Case:
    """One multiplexer client with a fresh scratch backend."""

    def __init__(self, root: Path, label: str, term: OuterTerminal):
        self.root, self.label, self.term = root, label, term
        self.log: list[dict] = []
        self.data = root / f"d-{label}"
        self.omp = root / f"omp-{label}"
        self.omp.write_text(STUB)
        self.omp.chmod(0o755)
        self.env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
                    "TERM": "xterm-256color"}
        self.marker = 0

    def ok(self, check: str, ok: bool, **info) -> bool:
        record = {"mux": self.label, "check": check, "ok": bool(ok), **info}
        self.log.append(record)
        note(record)
        return bool(ok)

    def cli(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", MAIN, *args], env=self.env, cwd=self.root, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    # -- shell-in-the-multiplexer helpers ------------------------------------------------------------------------
    def shell(self, command: str) -> None:
        self.term.send(b"\x15")  # Ctrl-U: drop any terminal-query echo the shell may have received
        self.term.send(command.encode() + b"\r")

    def typed_ready(self) -> bool:
        self.term.pump(0.5)
        return self.term.until(lambda: "$" in self.term.text() or "#" in self.term.text(), 30)

    # -- 1. the multiplexer passes the bytes unchanged -------------------------------------------------------------
    def tap(self) -> None:
        out = self.root / f"tap-{self.label}.bin"
        script = self.root / f"tap-{self.label}.py"
        script.write_text(TAP)
        self.shell(f"clear; {shlex.quote(sys.executable)} {shlex.quote(str(script))} {shlex.quote(str(out))}")
        if not self.ok("tap_started", self.term.until(lambda: "TAP-READY" in self.term.text(), 20)):
            return
        sent = b""
        for chunk in TAP_CHUNKS:
            self.term.send(chunk)
            self.term.pump(0.12)
            sent += chunk
        self.term.pump(1.0)  # a lone Esc may be held by the multiplexer (escape-time)
        self.term.send(b"\x00")
        self.term.until(lambda: "TAP-DONE" in self.term.text(), 10)
        got = out.read_bytes() if out.exists() else b""
        # A multiplexer that owns the keyboard for a pane whose app has not asked for application cursor keys may send
        # arrows as CSI (ESC [ A) although the client sent SS3 (ESC O A): the same key, and the UI accepts both.
        # Everything else -- C0 bytes, Space, digits, Enter/Tab/Esc, UTF-8 -- must be byte-identical.
        canonical = re.sub(rb"\x1bO([ABCD])", lambda m: b"\x1b[" + m.group(1), sent)
        self.ok("bytes_pass_unchanged", got in (sent, canonical), sent=sent.hex(), got=got.hex(),
                exact=got == sent, ss3_arrows_became_csi=got == canonical != sent,
                first_diff=next((i for i, (a, b) in enumerate(zip(canonical, got)) if a != b), min(len(sent), len(got))))

    # -- 2. the real entrypoint + product UI ---------------------------------------------------------------------
    def attach_cmd(self) -> str:
        self.marker += 1
        return (f"clear; env PYTHONPATH={SRC} PYTHONDONTWRITEBYTECODE=1 {shlex.quote(sys.executable)} -c "
                f"{shlex.quote(MAIN)} attach --data-dir {shlex.quote(str(self.data))}; echo UIDONE{self.marker}-$?")

    def done(self, timeout: float = 15) -> bool:
        return self.term.until(lambda: f"UIDONE{self.marker}-0" in self.term.text(), timeout)

    def got(self, role: str) -> list[str]:
        return re.findall(rf"\[{role} got (b'[^\]]*)\]", self.term.text())

    def hangul_leaks(self) -> list[str]:
        """Any pane echo that carries a UTF-8 lead byte of the Hangul syllable/jamo blocks."""
        return [g for g in self.got("manager") + self.got("worker") if re.search(r"\\xe[a-d]", g)]

    def wait_text(self, needle: str, timeout: float = 10) -> bool:
        return self.term.until(lambda: needle in self.term.text(), timeout)

    def gone_text(self, needle: str, timeout: float = 10) -> bool:
        return self.term.until(lambda: needle not in self.term.text(), timeout)

    def product(self) -> None:
        started = self.cli("start", "--data-dir", str(self.data), "--omp", str(self.omp), "--no-attach", "--timeout", "3")
        if not self.ok("backend_started", "starting backend" in started.stdout, out=(started.stdout + started.stderr)[-300:]):
            return
        term = self.term
        self.shell(self.attach_cmd())
        if not self.ok("ui_attached", term.until(lambda: "STUB-OMP manager ready" in term.text()
                                                 and "STUB-OMP worker ready" in term.text(), 40),
                       screen_tail=term.text()[-300:]):
            return
        # -- Ctrl-] Space menu: opens, digits are on screen, Esc closes, nothing reaches a pane
        term.send(PREFIX + b" ")
        opened = self.wait_text("명령 메뉴", 10)
        row = next((ln for ln in term.text().splitlines() if "detach" in ln and "[Ctrl-]" in ln), "")
        found = re.search(r"(\d)\s+detach", row)
        self.ok("menu_opens_with_ctrl_bracket_space", opened and bool(found), row=row.strip()[:120])
        term.send("한글 abc".encode() + b"\t\x7f")
        term.send(b"\x1b")
        self.ok("menu_esc_closes", self.gone_text("명령 메뉴", 8))
        term.pump(0.4)
        self.ok("menu_forwarded_nothing_to_a_pane", not self.got("manager") and not self.got("worker"),
                manager=self.got("manager"), worker=self.got("worker"))
        term.send(b"x")
        self.ok("keys_reach_the_pane_after_esc", term.until(lambda: self.got("manager") == ["b'x'"], 8),
                manager=self.got("manager"))
        # -- Hangul right after the prefix: hint, nothing forwarded, prefix spent
        hangul = "한".encode()
        for chunk in (PREFIX, hangul[:1], hangul[1:]):
            term.send(chunk)
            term.pump(0.15)
        hinted = self.wait_text("한글 입력 상태", 8)
        term.pump(0.5)
        self.ok("hangul_after_prefix_hint_and_no_leak", hinted and not self.hangul_leaks(), hinted=hinted,
                manager=self.got("manager"), worker=self.got("worker"))
        term.send(b"d")
        self.ok("plain_d_after_the_hint_is_text_not_detach", term.until(lambda: self.got("manager")[-1:] == ["b'd'"], 8)
                and f"UIDONE{self.marker}" not in term.text(), manager=self.got("manager"))
        term.send("안".encode())
        self.ok("hangul_without_prefix_reaches_the_pane", term.until(
            lambda: any("\\xec\\x95\\x88" in g for g in self.got("manager")), 8), manager=self.got("manager"))
        # -- Ctrl aliases through the real UI: mouse toggle (notice text), zoom (worker title disappears), literal prefix
        term.send(PREFIX + ctrl("e"))
        self.ok("ctrl_e_toggles_mouse_off", self.wait_text("마우스 캡처 꺼짐", 8))
        term.send(PREFIX + b"m")
        self.ok("letter_m_toggles_mouse_on", self.wait_text("마우스 캡처 켜짐", 8))
        term.send(PREFIX + ctrl("z"))
        zoomed = self.gone_text("WORKER OMP", 8)
        term.send(PREFIX + ctrl("z"))
        self.ok("ctrl_z_zoom_toggles", zoomed and self.wait_text("WORKER OMP", 8), zoomed=zoomed)
        term.send(PREFIX + PREFIX)
        self.ok("prefix_prefix_is_a_literal_prefix", term.until(lambda: "b'\\x1d'" in "".join(self.got("manager")), 8),
                manager=self.got("manager"))
        term.send(PREFIX + ctrl("t"))
        term.send(PREFIX + ctrl("y"))
        term.send(PREFIX + ctrl("o"))
        term.send(PREFIX + ctrl("r"))
        term.pump(1.0)
        self.ok("other_ctrl_aliases_are_not_forwarded", not any("\\x14" in g or "\\x19" in g or "\\x0f" in g
                                                                  or "\\x12" in g for g in self.got("manager")),
                manager=self.got("manager"))
        status = self.cli("status", "--data-dir", str(self.data), "--json")
        # -- detach with the Ctrl alias
        term.send(PREFIX + ctrl("d"))
        self.ok("ctrl_bracket_ctrl_d_detaches_exit_0", self.done(), tail=term.text()[-200:])
        self.ok("backend_survives_detach", self.cli("status", "--data-dir", str(self.data), "--json").returncode == 0)
        # -- reattach; detach through the menu digit read from the screen
        self.shell(self.attach_cmd())
        if not self.ok("reattached", term.until(lambda: "STUB-OMP manager ready" in term.text(), 40)):
            return
        term.send(PREFIX + b" ")
        self.wait_text("명령 메뉴", 10)
        row = next((ln for ln in term.text().splitlines() if "detach" in ln and "[Ctrl-]" in ln), "")
        found = re.search(r"(\d)\s+detach", row)
        term.send(found.group(1).encode() if found else b"?")
        self.ok("menu_digit_detaches_exit_0", self.done(), digit=found and found.group(1))
        # -- and the same with arrows + Enter (last item = detach): Up from the first item wraps
        self.shell(self.attach_cmd())
        if term.until(lambda: "STUB-OMP manager ready" in term.text(), 40):
            term.send(PREFIX + b" ")
            self.wait_text("명령 메뉴", 10)
            term.send(b"\x1b[A")
            term.send(b"\r")
            self.ok("menu_up_enter_detaches_exit_0", self.done(), tail=term.text()[-160:])
        status_after = self.cli("status", "--data-dir", str(self.data), "--json")
        self.ok("backend_still_up_before_shutdown", status_after.returncode == 0, out=status_after.stdout[:200])

    def shutdown_backend(self) -> None:
        result = self.cli("shutdown", "--data-dir", str(self.data), "--yes")
        self.ok("backend_shutdown", result.returncode == 0, out=(result.stdout + result.stderr)[-200:])


@unittest.skipUnless(LIVE, "live multiplexer check: set WB_LIVE_IME=1")
class LiveIme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="wbp27o-", dir=os.environ.get("TMPDIR") or "/tmp"))

    @classmethod
    def tearDownClass(cls):
        end = time.monotonic() + 10
        left = owned_processes(cls.root)
        while left and time.monotonic() < end:
            time.sleep(0.2)
            left = owned_processes(cls.root)
        report = {"step": "residue", "left_before_kill": {pid: Path(f"/proc/{pid}/cmdline").read_bytes()[:100].decode(
            errors="replace") for pid in left if Path(f"/proc/{pid}").exists()}}
        for pid, start in left.items():
            kill_identity(pid, start, signal.SIGKILL)
        time.sleep(0.3)
        report["left_after"] = [pid for pid, start in left.items() if ticks(pid) == start]
        note(report)
        shutil.rmtree(cls.root, ignore_errors=True)
        cls.residue = report

    def assert_all_ok(self, log: list[dict]) -> None:
        failed = [r for r in log if not r["ok"]]
        self.assertTrue(log and not failed, json.dumps(failed, ensure_ascii=False)[:3000])

    def tmux(self, mouse: bool) -> list[dict]:
        sock = f"wbp27o-{secrets.token_hex(4)}"
        conf = self.root / f"{sock}.conf"
        conf.write_text(("set -g mouse on\n" if mouse else "") + "set -g status off\n")
        env = clean_env(self.root, TMUX_TMPDIR=str(self.root))
        base = ["tmux", "-L", sock, "-f", str(conf)]
        subprocess.run(base + ["new-session", "-d", "-s", "wb", "-x", "150", "-y", "46", "bash --norc --noprofile"],
                       env=env, check=True, cwd=self.root)
        pid = int(subprocess.run(base + ["display", "-p", "#{pid}"], env=env, capture_output=True, text=True,
                                 check=True).stdout)
        start = ticks(pid)
        term = OuterTerminal(base + ["attach", "-t", "wb"], env, str(self.root))
        case = Case(self.root, f"tmux-mouse-{'on' if mouse else 'off'}", term)
        try:
            if case.ok("shell_ready", case.typed_ready()):
                case.tap()
                case.product()
        finally:
            try:
                case.shutdown_backend()
            finally:
                term.close()
                subprocess.run(base + ["kill-server"], env=env, capture_output=True, timeout=30)
                end = time.monotonic() + 8
                while time.monotonic() < end and ticks(pid) == start:
                    time.sleep(0.05)
                case.ok("tmux_server_gone", ticks(pid) != start, socket=sock, pid=pid)
        return case.log

    def test_tmux_mouse_off(self):
        self.assert_all_ok(self.tmux(False))

    def test_tmux_mouse_on(self):
        self.assert_all_ok(self.tmux(True))

    def test_herdr_isolated_server(self):
        herdr = shutil.which("herdr")
        if not herdr:
            self.skipTest("herdr not installed")
        xdg = self.root / f"xdg-{secrets.token_hex(3)}"
        (xdg / "herdr").mkdir(parents=True)
        # our own minimal config (the user's herdr config is neither read nor copied): no first-run onboarding overlay, which would
        # otherwise take the keystrokes and offer to install agent integrations, and no network version/manifest checks
        (xdg / "herdr/config.toml").write_text("onboarding = false\n\n[update]\nversion_check = false\nmanifest_check = false\n")
        env = clean_env(self.root, XDG_CONFIG_HOME=str(xdg))
        term = OuterTerminal([herdr], env, str(self.root))
        case = Case(self.root, "herdr", term)
        try:
            ready = term.until(lambda: "$" in term.text() or "#" in term.text(), 30)
            term.pump(2.0)
            if case.ok("herdr_started", ready, screen_head=term.text()[:200]):
                case.tap()
                case.product()
        finally:
            try:
                case.shutdown_backend()
            finally:
                stop = subprocess.run([herdr, "server", "stop"], env=env, capture_output=True, text=True, timeout=30)
                term.close()
                case.ok("herdr_server_stopped", stop.returncode == 0, out=(stop.stdout + stop.stderr)[-200:])
        self.assert_all_ok(case.log)

    def test_zz_no_residue(self):
        time.sleep(1.0)
        left = owned_processes(self.root)
        self.assertEqual(left, {}, f"processes naming the scratch root are still alive: {left}")


if __name__ == "__main__":
    unittest.main()
