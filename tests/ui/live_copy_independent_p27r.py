"""LIVE drag-to-copy check (p27-copy-test-01, C-D62 (2)), opt-in: ``WB_LIVE_COPY=1``.

Run (repo root, short scratch TMPDIR)::

    WB_LIVE_COPY=1 PYTHONPATH=src:tests/ui TMPDIR=/tmp/wbp27rl /tmp/cw02-g1-venv/bin/python -m unittest \\
        tests.ui.live_copy_independent_p27r -v

This module plays the user's OUTER terminal (a PTY it owns, rendered with pyte) for a real multiplexer client and sends only
the SGR mouse reports a real terminal would send under the mouse mode the multiplexer asked for.

1. tmux (``tmux -L <unique> -f <own conf>``, ``TMUX_TMPDIR`` in the scratch root): the real entrypoint
   (``start --no-attach`` + ``attach``) with the real OMP binary in the manager/worker panes. OMP runs with its own scratch
   ``PI_CODING_AGENT_DIR`` + ``HOME`` whose only provider is a local HTTP counter answering 503 -- no prompt is ever typed,
   the counter must stay 0, nothing of the user's OMP setup is read. Checks: ``set-clipboard on`` -> ``show-buffer`` equals
   the dragged host-shell text; ``set-clipboard external`` + ``allow-passthrough on`` -> the UI's OSC 52 reaches the tmux
   client (outer) stream; both off -> no copy anywhere and the footer notice names both options; a drag over OMP's own
   screen text copies exactly the cells shown.
2. herdr: its own server with ``HOME``/``XDG_*``/``HERDR_SOCKET_PATH`` under the scratch root (checked with
   ``herdr status server`` before launch -- if the socket is not ours the case is "not run"); no DISPLAY/WAYLAND variable
   is passed, so herdr cannot reach the desktop clipboard. The product UI attaches to a scripted ui_v1 fixture (no backend,
   no OMP) and the OSC 52 of a drag must reach the herdr client stream.

Every server is stopped by its own command and verified gone by pid + start ticks; every process naming the scratch root
is checked at the end (survivors are failures). The user's tmux/herdr servers, sockets and configs are never used.
"""
from __future__ import annotations

import base64
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
for extra in (HERE, HERE.parent / "backend"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from independent_support_cw06 import ScriptedServer  # noqa: E402
from live_mux_independent_p27n import (COLS, ROWS, OuterTerminal, clean_env, kill_identity, note,  # noqa: E402
                                       owned_processes, ticks)

REPO = HERE.parents[1]
SRC = str(REPO / "src")
LIVE = os.environ.get("WB_LIVE_COPY") == "1"
PREFIX = b"\x1d"
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"
BOOT = ("import sys; sys.path.insert(0, {src!r}); from pathlib import Path; "
        "from workbench.ui.product import run_product; raise SystemExit(run_product(Path({sock!r})))")
OMP = os.environ.get("WB_LIVE_COPY_OMP") or shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
OSC_ANY = re.compile(rb"\x1b\]52;[a-z]*;([A-Za-z0-9+/=]*)(?:\x07|\x1b\\)")


def b64(text: str) -> bytes:
    return base64.b64encode(text.encode())


def osc_payloads(raw: bytes) -> list[str]:
    out = []
    for match in OSC_ANY.finditer(raw):
        try:
            out.append(base64.b64decode(match.group(1)).decode())
        except ValueError:
            out.append("<undecodable>")
    return out


class Case:
    def __init__(self, label: str, term: OuterTerminal):
        self.label, self.term = label, term
        self.log: list[dict] = []

    def ok(self, check: str, ok: bool, **info) -> bool:
        record = {"mux": self.label, "check": check, "ok": bool(ok), **info}
        self.log.append(record)
        note(record)
        return bool(ok)

    def shell(self, command: str) -> None:
        self.term.send(b"\x15")
        self.term.send(command.encode() + b"\r")

    def row_of(self, needle: str) -> tuple[int, int] | None:
        return self.term.find(needle)

    def drag_cells(self, x0: int, x1: int, y: int) -> bool:
        """0-based outer columns x0..x1 on 0-based row y: press, two motions, release (SGR, 1-based)."""
        t = self.term
        pressed = t.report(0, x0 + 1, y + 1)
        moved = t.report(32, (x0 + x1) // 2 + 1, y + 1) and t.report(32, x1 + 1, y + 1)
        released = t.report(0, x1 + 1, y + 1, release=True)
        t.pump(0.4)
        return pressed and moved and released

    def screen_text(self, x0: int, x1: int, y: int) -> str:
        line = self.term.screen.buffer[y]
        return "".join(line[x].data for x in range(x0, x1 + 1)).rstrip(" ")

    def footer(self) -> str:
        return self.term.screen.display[ROWS - 1]


class LiveBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="wbp27r-", dir=os.environ.get("TMPDIR") or "/tmp"))
        os.chmod(cls.root, 0o700)

    @classmethod
    def tearDownClass(cls):
        end = time.monotonic() + 10
        left = owned_processes(cls.root)
        while left and time.monotonic() < end:
            time.sleep(0.2)
            left = owned_processes(cls.root)
        report = {"step": "residue", "class": cls.__name__, "left_before_kill": sorted(left)}
        for pid, start in left.items():
            kill_identity(pid, start, signal.SIGKILL)
        time.sleep(0.3)
        report["left_after"] = [pid for pid, start in left.items() if ticks(pid) == start]
        note(report)
        shutil.rmtree(cls.root, ignore_errors=True)

    def assert_all_ok(self, log: list[dict]) -> None:
        failed = [r for r in log if not r["ok"]]
        self.assertTrue(log and not failed, json.dumps(failed, ensure_ascii=False)[:4000])


# ---------------------------------------------------------------------------------------------------------------------
@unittest.skipUnless(LIVE, "live copy check: set WB_LIVE_COPY=1")
class LiveCopyTmux(LiveBase):
    def test_tmux_real_backend_and_omp(self):
        if not os.access(OMP, os.X_OK):
            self.skipTest(f"no OMP binary at {OMP}")
        from live_harness import TEST_OMP_ARGS, LiveBackend  # tests/backend (counting 503 provider, own agent dir)

        live = LiveBackend(OMP, path=f"{Path(OMP).parent}:/usr/bin:/bin")
        home = live.root / "home"
        home.mkdir()
        live.env.update(HOME=str(home), SHELL="/bin/bash")  # nothing of the user's OMP / shell setup is read
        for key in [k for k in live.env if k.startswith(("XDG_", "DISPLAY", "WAYLAND", "DBUS"))]:
            live.env.pop(key)
        case = Case("tmux", None)  # type: ignore[arg-type]
        sock = f"wbp27r-{secrets.token_hex(4)}"
        conf = self.root / f"{sock}.conf"
        conf.write_text("set -g status off\nset -s set-clipboard on\nset -g allow-passthrough off\n")
        env = clean_env(self.root, TMUX_TMPDIR=str(self.root))
        base = ["tmux", "-L", sock, "-f", str(conf)]

        def tmux(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(base + list(args), env=env, capture_output=True, text=True, timeout=20)

        term = server_pid = server_start = None
        try:
            started = live.cli(*live.start_args("--no-attach"), timeout=150)
            case.ok("backend_started", started.returncode == 0, out=(started.stdout + started.stderr)[-300:])
            snap = None
            end = time.monotonic() + 60
            while time.monotonic() < end:
                snap = live.status()
                if snap and snap.get("phase") != "starting":
                    break
                time.sleep(0.2)
            if snap:
                live.remember(snap)
            if not case.ok("backend_ready", bool(snap) and snap.get("phase") == "ready",
                           phase=snap and snap.get("phase"), omp_isolation=(snap or {}).get("omp_isolation", {}).get("state")):
                return
            subprocess.run(base + ["new-session", "-d", "-s", "wb", "-x", str(COLS), "-y", str(ROWS),
                                   "bash --norc --noprofile"], env=env, check=True, cwd=self.root, timeout=20)
            server_pid = int(tmux("display", "-p", "#{pid}").stdout)
            server_start = ticks(server_pid)
            term = OuterTerminal(base + ["attach", "-t", "wb"], env, str(self.root))
            case.term = term
            term.pump(1.0)
            attach = (f"clear; env PYTHONPATH={SRC} PYTHONDONTWRITEBYTECODE=1 {shlex.quote(sys.executable)} -c "
                      f"{shlex.quote(MAIN)} attach --data-dir {shlex.quote(str(live.data))}; echo UIDONE-$?")
            case.shell(attach)
            if not case.ok("ui_attached", term.until(lambda: "HOST SHELL" in term.text() and "MANAGER OMP" in term.text(), 40),
                           tail=term.text()[-200:]):
                return
            term.pump(3.0)  # OMP draws its start screen
            term.send(PREFIX + b"3")  # focus the host shell
            term.pump(0.5)

            def show_line(tag: str) -> tuple[str, tuple[int, int] | None]:
                token = f"{tag}-{secrets.token_hex(3)}"
                head, tail = token.split("-", 1)
                term.send(f"printf '%s-%s 한글 end\\n' {head} {tail}\r".encode())
                text = f"{token} 한글 end"
                term.until(lambda: case.row_of(text) is not None, 10)
                return text, case.row_of(text)

            def drag_line(text: str, where) -> bytes:
                col, row = where
                mark = len(term.raw)
                width = sum(2 if ord(ch) >= 0x1100 else 1 for ch in text)
                case.drag_cells(col, col + width - 1, row)
                term.pump(0.8)
                return bytes(term.raw[mark:])

            # -- A: set-clipboard on -> tmux buffer
            tmux("delete-buffer")
            text, where = show_line("CLIPON")
            if case.ok("A_host_line_visible", where is not None, text=text):
                raw = drag_line(text, where)
                buf = tmux("show-buffer")
                case.ok("A_set_clipboard_on_show_buffer_equals_selection", buf.stdout == text,
                        show_buffer=buf.stdout[:80], rc=buf.returncode)
                case.ok("A_notice_with_tmux_hint", "복사됨" in case.footer() and "set-clipboard on" in case.footer(),
                        footer=case.footer().strip()[:120])
                case.ok("A_info_outer_stream_osc52", True, forwarded_to_outer=text in osc_payloads(raw))
            # -- B: set-clipboard external + allow-passthrough on -> OSC 52 reaches the tmux client stream
            tmux("set", "-s", "set-clipboard", "external")
            tmux("set", "-g", "allow-passthrough", "on")
            tmux("delete-buffer")
            term.send(b"\x15")
            text, where = show_line("PASSTHRU")
            if case.ok("B_host_line_visible", where is not None, text=text):
                raw = drag_line(text, where)
                case.ok("B_passthrough_osc52_on_client_stream", text in osc_payloads(raw),
                        payloads=[p[:40] for p in osc_payloads(raw)], exact_bel_form=b"\x1b]52;c;" + b64(text) + b"\x07" in raw)
                buf = tmux("show-buffer")
                case.ok("B_external_does_not_fill_the_tmux_buffer", buf.stdout != text, show_buffer=buf.stdout[:60])
            # -- C: both off -> nothing copied anywhere, the notice explains
            tmux("set", "-s", "set-clipboard", "off")
            tmux("set", "-g", "allow-passthrough", "off")
            tmux("delete-buffer")
            text, where = show_line("BOTHOFF")
            if case.ok("C_host_line_visible", where is not None, text=text):
                raw = drag_line(text, where)
                buf = tmux("show-buffer")
                case.ok("C_nothing_reaches_buffer_or_client", text not in osc_payloads(raw) and buf.stdout != text,
                        payloads=len(osc_payloads(raw)), show_buffer=buf.stdout[:60])
                footer = case.footer()
                case.ok("C_notice_names_both_options", "set-clipboard on" in footer and "allow-passthrough on" in footer,
                        footer=footer.strip()[:120])
            # -- D: a drag over the real OMP's own screen (manager pane; OMP does not track the mouse)
            tmux("set", "-s", "set-clipboard", "on")
            tmux("delete-buffer")
            term.send(b"\x15")  # clear the host command line again (keys go to the host; nothing to OMP)
            from workbench.ui.product.model import pane_boxes
            from workbench.contracts.v1 import PaneId
            top, left, height, width = pane_boxes(ROWS, COLS)[PaneId.MANAGER_OMP]
            pick = None
            for y in range(top + 1, top + height - 1):
                row = case.screen_text(left + 1, left + width - 2, y)
                words = [m for m in re.finditer(r"[A-Za-z0-9][\x21-\x7e]*(?: [\x21-\x7e]+)*", row) if len(m.group(0)) >= 6]
                if words:
                    m = words[0]
                    pick = (left + 1 + m.start(), left + 1 + m.end() - 1, y)
                    break
            if case.ok("D_omp_text_found_in_manager_pane", pick is not None,
                       manager_rows_nonblank=sum(1 for y in range(top + 1, top + height - 1)
                                                 if case.screen_text(left + 1, left + width - 2, y))):
                x0, x1, y = pick
                want = case.screen_text(x0, x1, y)
                case.drag_cells(x0, x1, y)
                term.pump(0.8)
                buf = tmux("show-buffer")
                case.ok("D_omp_pane_drag_copies_exactly_the_shown_cells", buf.stdout == want, want_len=len(want),
                        equal=buf.stdout == want, got_len=len(buf.stdout))
            case.ok("provider_requests_zero", live.provider.requests == 0, requests=live.provider.requests)
            term.send(PREFIX + b"q")
            case.ok("detach_exit_0", term.until(lambda: "UIDONE-0" in term.text(), 15), tail=term.text()[-120:])
        finally:
            if term is not None:
                term.close()
            if server_pid is not None:
                tmux("kill-server")
                end = time.monotonic() + 8
                while time.monotonic() < end and ticks(server_pid) == server_start:
                    time.sleep(0.05)
                case.ok("tmux_server_gone", ticks(server_pid) != server_start, socket=sock)
            from independent_support import stop_and_verify
            left, _ = stop_and_verify(live, timeout=20)  # confirmed shutdown through the entrypoint, then wait for exit
            fallback = live.cleanup()  # exact-identity kill only if something is still there; provider + temp root
            case.ok("backend_shutdown_no_residue", not left, residue=left,
                    fallback={k: v for k, v in fallback.items() if v})
        self.assert_all_ok(case.log)

    def test_zz_no_residue(self):
        time.sleep(1.0)
        self.assertEqual({}, owned_processes(self.root))


# ---------------------------------------------------------------------------------------------------------------------
@unittest.skipUnless(LIVE, "live copy check: set WB_LIVE_COPY=1")
class LiveCopyHerdr(LiveBase):
    def test_herdr_isolated_server(self):
        herdr = shutil.which("herdr")
        if not herdr:
            self.skipTest("herdr not installed")
        home = self.root / "hh"
        xdg = {name: self.root / f"x-{name.lower()}" for name in
               ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")}
        for path in [home, *xdg.values()]:
            path.mkdir(mode=0o700)
        (xdg["XDG_CONFIG_HOME"] / "herdr").mkdir()
        config = xdg["XDG_CONFIG_HOME"] / "herdr/config.toml"
        config.write_text("onboarding = false\n\n[update]\nversion_check = false\nmanifest_check = false\n")
        sock = self.root / "herdr.sock"
        env = clean_env(self.root, HOME=str(home), HERDR_SOCKET_PATH=str(sock), HERDR_CONFIG_PATH=str(config),
                        **{k: str(v) for k, v in xdg.items()})
        case = Case("herdr", None)  # type: ignore[arg-type]
        status = subprocess.run([herdr, "status", "server"], env=env, capture_output=True, text=True, timeout=20)
        out = status.stdout + status.stderr
        ours = f"socket: {sock}" in out and "not running" in out.lower()
        note({"mux": "herdr", "step": "pre_status", "out": out[-300:]})
        if not ours:
            self.skipTest(f"not run: cannot prove an isolated herdr server socket ({out.strip()[-160:]!r})")
        fixture = ScriptedServer(replay={"host_shell": "HERDR-COPY 한글 end\r\nnext".encode()})
        self.addCleanup(fixture.close)
        term = OuterTerminal([herdr], env, str(self.root))
        case.term = term
        try:
            ready = term.until(lambda: "$" in term.text() or "#" in term.text(), 30)
            term.pump(2.0)
            if case.ok("herdr_started", ready, head=term.text()[:120]):
                boot = BOOT.format(src=SRC, sock=str(fixture.path))
                case.shell(f"clear; {shlex.quote(sys.executable)} -c {shlex.quote(boot)}; echo UIDONE-$?")
                seen = term.until(lambda: fixture.attached.is_set() and case.row_of("HERDR-COPY") is not None, 30)
                if case.ok("ui_attached", seen, tail=term.text()[-200:]):
                    col, row = case.row_of("HERDR-COPY")
                    mark = len(term.raw)
                    moved = case.drag_cells(col, col + len("HERDR-COPY 한글 end") + 2 - 1, row)
                    term.pump(1.0)
                    raw = bytes(term.raw[mark:])
                    case.ok("herdr_forwards_osc52_to_its_client", "HERDR-COPY 한글 end" in osc_payloads(raw),
                            delivered=moved, payloads=[p[:40] for p in osc_payloads(raw)])
                    case.ok("herdr_notice_without_tmux_hint", "복사됨" in case.footer() and "tmux" not in case.footer(),
                            footer=case.footer().strip()[:100])
                    case.ok("herdr_ui_sent_no_input", not [f for f in fixture.frames if f.header.get("type") == "input"])
                    term.send(PREFIX + b"q")
                    case.ok("detach_exit_0", term.until(lambda: "UIDONE-0" in term.text(), 15))
        finally:
            stop = subprocess.run([herdr, "server", "stop"], env=env, capture_output=True, text=True, timeout=30)
            term.close()
            case.ok("herdr_server_stopped", stop.returncode == 0, out=(stop.stdout + stop.stderr)[-200:])
        self.assert_all_ok(case.log)

    def test_zz_no_residue(self):
        time.sleep(1.0)
        self.assertEqual({}, owned_processes(self.root))


if __name__ == "__main__":
    unittest.main()
