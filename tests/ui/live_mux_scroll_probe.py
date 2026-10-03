"""Live check: product UI mouse wheel / keyboard scrolling inside tmux and herdr (opt-in, WB_LIVE_MUX=1).

The probe plays the OUTER terminal itself: it runs a tmux client (isolated server, ``tmux -L wbmx-<rand>``) or a
herdr client (isolated server: its own ``XDG_CONFIG_HOME`` with a copy of the user's config.toml) on a PTY it
owns, tracks the mouse mode the multiplexer asks that terminal for (``support.OuterMouse``: 1000/1002/1003 are
one state) and sends only the mouse reports a real terminal would send. Inside the multiplexer pane the real
entrypoint ``omp-workbench attach`` runs against a backend whose OMP is a stub (no model, no network). Nothing is
sent to any existing tmux/herdr session; every server, backend and directory is owned and removed by identity.

Run: WB_LIVE_MUX=1 PYTHONPATH=src TMPDIR=/tmp/wbmx /tmp/cw02-g1-venv/bin/python -m unittest \
        tests.ui.live_mux_scroll_probe -v            (report: $WB_LIVE_MUX_REPORT, JSON lines, optional)
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pyte  # noqa: E402
from support import OuterMouse  # noqa: E402

REPO = HERE.parents[1]
SRC = REPO / "src"
PREFIX = b"\x1d"
ROWS, COLS = 46, 150
LIVE = os.environ.get("WB_LIVE_MUX") == "1"
REPORT = os.environ.get("WB_LIVE_MUX_REPORT")
STUB_OMP = """#!/bin/sh
case "$1" in
  --version) echo "omp/18.4.4"; exit 0;;
  config) echo '[]'; exit 0;;
esac
for a in "$@"; do [ "$a" = rpc ] && exit 3; done
printf 'stub omp pane (no model)\\r\\n'
exec sleep 100000
"""


def clean_env(**extra: str) -> dict[str, str]:
    """No TMUX*/HERDR* from the caller's own multiplexers: children must never talk to the user's sessions."""
    env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME", "LANG") if key in os.environ}
    env.setdefault("LANG", "C.UTF-8")
    env.update(TERM="xterm-256color", SHELL="/bin/bash")
    env.update(extra)
    return env


def report(record: dict) -> None:
    print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
    if REPORT:
        with open(REPORT, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class _Screen(pyte.Screen):
    """Answers terminal queries (DA/DSR) like a terminal, so multiplexers do not wait on us."""

    def __init__(self, cols, rows, reply):
        super().__init__(cols, rows)
        self._reply = reply

    def write_process_input(self, data):
        self._reply(data.encode())

    def report_device_status(self, mode=0, **kwargs):
        if not kwargs.get("private"):  # CSI ? Ps n (DEC-specific DSR) is not answered
            super().report_device_status(mode)


class OuterTerminal:
    """A PTY we own, acting as the user's terminal emulator for a multiplexer client."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str):
        master, slave = os.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

        def controlling_tty():
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                                     start_new_session=True, close_fds=True, preexec_fn=controlling_tty)
        os.close(slave)
        self.fd = master
        self.screen = _Screen(COLS, ROWS, self._answer)
        self.stream = pyte.ByteStream(self.screen)
        self.mouse = OuterMouse()
        self.raw = bytearray()

    def _answer(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def pump(self, seconds: float = 0.05) -> None:
        end = time.monotonic() + seconds
        while True:
            left = max(0.0, end - time.monotonic())
            try:
                ready = select.select([self.fd], [], [], left)[0]
                data = os.read(self.fd, 65536) if ready else b""
            except OSError:
                data = b""
            if data:
                self.raw.extend(data)
                self.mouse.feed(data)
                try:
                    self.stream.feed(data)
                except TypeError:  # a sequence pyte cannot take (e.g. a private-marker variant): keep rendering
                    self.stream = pyte.ByteStream(self.screen)
            if time.monotonic() >= end:
                return

    def until(self, predicate, timeout: float = 15.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.05)
            if predicate():
                return True
        return False

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)
        self.pump(0.05)

    def mouse_event(self, button: int, x: int, y: int, release: bool = False) -> bool:
        """What a real terminal would send for this action under the current mode; False = nothing sent."""
        self.pump(0.05)
        data = self.mouse.report(button, x, y, release)
        if data is not None:
            self.send(data)
        return data is not None

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def find(self, needle: str) -> tuple[int, int] | None:
        """1-based (x, y) of the first cell of ``needle`` on the outer screen."""
        for index, line in enumerate(self.screen.display):
            at = line.find(needle)
            if at >= 0:
                return at + 1, index + 1
        return None

    def line_with(self, needle: str) -> str:
        return next((line for line in self.screen.display if needle in line), "")

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)  # the client's own session (we created it)
            except ProcessLookupError:
                pass
            self.proc.wait(5)
        try:
            os.close(self.fd)
        except OSError:
            pass


class Backend:
    """A Workbench backend in an owned data dir; OMP is a stub script (no model calls)."""

    def __init__(self, root: Path):
        self.root = root
        self.data = root / "d"
        self.project = root / "p"
        self.project.mkdir()
        self.tmp = root / "t"
        self.tmp.mkdir()
        self.omp = root / "omp"
        self.omp.write_text(STUB_OMP)
        self.omp.chmod(0o700)
        self.env = clean_env(PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.tmp))
        # ``start`` waits for the phase to leave "starting" (the stub never connects the bridge: ~90 s); the UI
        # can attach earlier, so the probe only waits for the backend's UI socket to answer.
        self.starter = subprocess.Popen(self.cli("start", "--omp", str(self.omp), "--no-attach"), env=self.env,
                                        cwd=self.project, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        status, end = None, time.monotonic() + 60
        while status is None and time.monotonic() < end:
            time.sleep(0.5)
            result = subprocess.run(self.cli("status", "--json"), env=self.env, capture_output=True, text=True,
                                    timeout=30)
            try:
                status = json.loads(result.stdout).get("snapshot") if result.returncode == 0 else None
            except ValueError:
                status = None
        report({"step": "backend_start", "phase": status and status.get("phase")})
        if status is None:
            raise RuntimeError("backend did not answer status")
        self.pid = status["backend"]["pid"]
        self.start_ticks = _ticks(self.pid)

    def cli(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "workbench.backend.cli", args[0], "--data-dir", str(self.data), *args[1:]]

    def attach_command(self) -> str:
        env = " ".join(f"{k}={v}" for k, v in (("PYTHONPATH", SRC), ("TMPDIR", self.tmp),
                                                ("PYTHONDONTWRITEBYTECODE", "1")))
        return f"env {env} {sys.executable} -m workbench.backend.cli attach --data-dir {self.data}"

    def shutdown(self) -> dict:
        result = subprocess.run(self.cli("shutdown", "--yes", "--json"), env=self.env, capture_output=True,
                                text=True, timeout=60)
        end = time.monotonic() + 20
        while time.monotonic() < end and _ticks(self.pid) == self.start_ticks:
            time.sleep(0.1)
        alive = _ticks(self.pid) == self.start_ticks and self.start_ticks is not None
        if alive:
            os.kill(self.pid, signal.SIGKILL)  # same pid AND same start time: still our backend
        if self.starter.poll() is None:
            self.starter.kill()  # our own ``start`` child, still polling for the phase
        self.starter.wait(5)
        return {"rc": result.returncode, "backend_left_running": alive}


def owned_processes(root: Path) -> dict[int, str]:
    """pid -> start ticks of every process whose argv or environment mentions our owned root dir."""
    found = {}
    needle = str(root).encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if needle in (entry / "cmdline").read_bytes() or needle in (entry / "environ").read_bytes():
                ticks = _ticks(int(entry.name))
                if ticks is not None:
                    found[int(entry.name)] = ticks
        except OSError:
            continue
    return found


def _ticks(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


class Session:
    """Drive the product UI through one multiplexer client."""

    def __init__(self, term: OuterTerminal, name: str):
        self.term, self.name = term, name
        self.log: list[dict] = []

    def note(self, check: str, ok: bool, **info) -> bool:
        record = {"mux": self.name, "check": check, "ok": bool(ok), **info}
        self.log.append(record)
        report(record)
        return ok

    def host(self) -> tuple[int, int] | None:
        """1-based (x, y) of the host pane's title on its top border (not the header's "focus: HOST SHELL")."""
        for index, line in enumerate(self.term.screen.display):
            at = line.find(" HOST SHELL")
            if at >= 0 and "owner=" in line[at:]:
                return at + 2, index + 1
        return None

    def host_title(self) -> str:
        where = self.host()
        return self.term.screen.display[where[1] - 1] if where else ""

    def scrolled(self) -> bool:
        return "HOST SHELL" in self.host_title() and "[SCROLL" in self.host_title()

    def mode(self) -> dict:
        return {"outer_tracking": self.term.mouse.tracking, "outer_sgr": self.term.mouse.sgr}

    def wheel_check(self, label: str) -> bool:
        where = self.host()
        if where is None:
            return self.note(label, False, why="host pane not on screen")
        x, y = where[0] + 4, where[1] + 3
        delivered = self.term.mouse_event(64, x, y)
        scrolled = delivered and self.term.until(self.scrolled, 5)
        back = False
        if scrolled:
            for _ in range(4):
                self.term.mouse_event(65, x, y)
                if self.term.until(lambda: not self.scrolled(), 1.5):
                    back = True
                    break
        return self.note(label, delivered and scrolled and back, delivered=delivered, scrolled=bool(scrolled),
                         back_to_live=back, **self.mode())

    def drag_host_divider(self) -> bool:
        where = self.host()
        if where is None:
            return self.note("divider_drag", False, why="host pane not on screen")
        x, y = where[0] - 2, where[1]  # the border cell between the host box's top-left corner and its title
        pressed = self.term.mouse_event(0, x, y)
        moved = [self.term.mouse_event(32, x, y - step) for step in (1, 2)]
        released = self.term.mouse_event(0, x, y - 2, release=True)
        ok = self.term.until(lambda: self.host() is not None and self.host()[1] == y - 2, 5)
        return self.note("divider_drag", pressed and all(moved) and released and ok, pressed=pressed,
                         motion_reported=moved, released=released, host_row_before=y,
                         host_row_after=(self.host() or (None, None))[1], **self.mode())

    def key_check(self, label: str, keys: bytes, leave: bytes) -> bool:
        self.term.send(keys)
        scrolled = self.term.until(self.scrolled, 5)
        title = self.host_title().strip()
        self.term.send(leave)
        back = self.term.until(lambda: not self.scrolled(), 5)
        return self.note(label, scrolled and back, scrolled=scrolled, back_to_live=back, title=title[:90])

    def run(self, backend: Backend, extra=None) -> list[dict]:
        term = self.term
        term.send(backend.attach_command().encode() + b"\r")
        if not self.note("ui_attached", term.until(lambda: "HOST SHELL" in term.text() and "MANAGER OMP"
                                                   in term.text(), 30), screen_tail=term.text()[-400:]):
            return self.log
        term.send(PREFIX + b"3")  # focus host
        term.until(lambda: "focus: HOST SHELL" in term.text(), 5)
        term.send(b"clear; seq -f 'L%04g' 1 400\r")
        self.note("host_output", term.until(lambda: "L0400" in term.text(), 10))
        header = next((line for line in term.screen.display if "omp isolation" in line.lower()
                       or "격리" in line), "")
        self.note("isolation_header_visible", bool(header), line=header.strip()[:140])
        term.pump(0.5)
        self.note("mouse_mode_after_attach", term.mouse.tracking in (1002, 1003) and term.mouse.sgr, **self.mode())
        self.wheel_check("wheel_before_drag")
        self.drag_host_divider()
        term.pump(0.5)
        self.note("mouse_mode_after_drag", term.mouse.tracking in (1002, 1003) and term.mouse.sgr, **self.mode())
        self.wheel_check("wheel_after_drag")
        self.drag_host_divider()
        self.wheel_check("wheel_after_second_drag")
        if extra:
            extra(self)
        self.key_check("shift_pgup", b"\x1b[5;2~", b"\x1b[6;2~" * 3)
        self.key_check("prefix_pgup", PREFIX + b"\x1b[5~", b"q")
        self.key_check("prefix_bracket_pgup", PREFIX + b"[" + b"\x1b[5~", b"q")
        term.send(PREFIX + b"m")
        term.pump(0.5)
        self.note("prefix_m_off", True, info="outer mode is the multiplexer's own choice", **self.mode())
        term.send(PREFIX + b"m")
        term.pump(0.5)
        self.wheel_check("wheel_after_prefix_m_off_on")
        term.send(PREFIX + b"q")
        detached = term.until(lambda: "detached; backend keeps running" in term.text(), 10)
        term.pump(0.5)
        self.note("detach", detached, **self.mode())
        return self.log


@unittest.skipUnless(LIVE, "live multiplexer probe: set WB_LIVE_MUX=1")
class LiveMuxScrollProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="wbmx-", dir=os.environ.get("TMPDIR") or "/tmp"))
        try:
            cls.backend = Backend(cls.root)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        backend = getattr(cls, "backend", None)
        if backend is not None:
            report({"step": "backend_shutdown", **backend.shutdown()})
        else:  # the start itself failed: stop whatever it created in our data dir
            env = clean_env(PYTHONPATH=str(SRC), TMPDIR=str(cls.root / "t"))
            subprocess.run([sys.executable, "-m", "workbench.backend.cli", "shutdown", "--data-dir",
                            str(cls.root / "d"), "--yes", "--json"], env=env, capture_output=True, timeout=60)
        end = time.monotonic() + 10
        left = owned_processes(cls.root)
        while left and time.monotonic() < end:
            time.sleep(0.2)
            left = owned_processes(cls.root)
        leftovers = {pid: Path(f"/proc/{pid}/cmdline").read_bytes()[:120].decode(errors="replace")
                     for pid in left if Path(f"/proc/{pid}").exists()}
        for pid, ticks in left.items():
            if _ticks(pid) == ticks:  # same pid and start time: ours
                os.kill(pid, signal.SIGKILL)
        report({"step": "residue", "left_before_kill": leftovers, "left_after": list(owned_processes(cls.root))})
        shutil.rmtree(cls.root, ignore_errors=True)
        report({"step": "root_removed", "exists": cls.root.exists()})

    def tmux(self, mouse: bool) -> list[dict]:
        sock = f"wbmx-{secrets.token_hex(4)}"
        conf = self.root / f"{sock}.conf"
        conf.write_text("set -g mouse on\n" if mouse else "")
        env = clean_env(TMUX_TMPDIR=str(self.root))  # the server socket lives in our own dir
        base = ["tmux", "-L", sock, "-f", str(conf)]
        subprocess.run(base + ["new-session", "-d", "-s", "wb", "-x", str(COLS), "-y", str(ROWS),
                               "bash --norc --noprofile"], env=env, check=True, cwd=self.root)
        pid = int(subprocess.run(base + ["display", "-p", "#{pid}"], env=env, capture_output=True, text=True,
                                 check=True).stdout)
        term = OuterTerminal(base + ["attach", "-t", "wb"], env, str(self.root))
        name = f"tmux-{'mouse-on' if mouse else 'mouse-off'}"

        def flags(session: Session) -> None:
            out = subprocess.run(base + ["display", "-p", "-t", "wb", "#{mouse_any_flag} #{mouse_standard_flag} "
                                         "#{mouse_button_flag} #{mouse_all_flag} #{mouse_sgr_flag}"],
                                 env=env, capture_output=True, text=True).stdout.split()
            session.note("tmux_pane_mouse_flags", out[:3] != ["0", "0", "0"] and out[0] == "1",
                         any_std_btn_all_sgr=out)

        session = Session(term, name)
        try:
            session.run(self.backend, extra=flags)
        finally:
            term.close()
            subprocess.run(base + ["kill-server"], env=env, capture_output=True)
            end = time.monotonic() + 5
            while time.monotonic() < end and Path(f"/proc/{pid}").exists():
                time.sleep(0.05)
            session.note("tmux_server_gone", not Path(f"/proc/{pid}").exists(), socket=sock, pid=pid)
        return session.log

    def test_tmux_mouse_off(self):
        log = self.tmux(False)
        self.assertTrue(all(r["ok"] for r in log if r["check"] != "isolation_header_visible"), log)

    def test_tmux_mouse_on(self):
        log = self.tmux(True)
        self.assertTrue(all(r["ok"] for r in log if r["check"] != "isolation_header_visible"), log)

    def herdr(self, mouse_capture: bool = True) -> list[dict]:
        herdr = shutil.which("herdr")
        if not herdr:
            self.skipTest("herdr not installed")
        xdg = self.root / f"xdg-{secrets.token_hex(3)}"
        (xdg / "herdr").mkdir(parents=True)
        user_config = Path.home() / ".config/herdr/config.toml"
        config = user_config.read_text() if user_config.is_file() else ""  # read-only copy of the user's settings
        if not mouse_capture:
            config += "\n[ui]\nmouse_capture = false\n"
        (xdg / "herdr/config.toml").write_text(config)
        env = clean_env(XDG_CONFIG_HOME=str(xdg))  # separate server, socket, session and log files
        term = OuterTerminal([herdr], env, str(self.root))
        session = Session(term, "herdr" if mouse_capture else "herdr-mouse_capture-off")
        log_path = xdg / "herdr/herdr-server.log"
        try:
            ready = term.until(lambda: "$" in term.text() or "#" in term.text(), 30)
            term.pump(2.0)
            session.note("herdr_started", ready, screen_head=term.text()[:300])
            session.run(self.backend)
        finally:
            warnings = []
            if log_path.exists():
                warnings = [line for line in log_path.read_text(errors="replace").splitlines()
                            if "failed to encode" in line]
            session.note("herdr_no_encode_warnings", not warnings, count=len(warnings), sample=warnings[:2])
            stop = subprocess.run([herdr, "server", "stop"], env=env, capture_output=True, text=True, timeout=30)
            term.close()
            session.note("herdr_server_stopped", stop.returncode == 0, out=(stop.stdout + stop.stderr)[-200:])
        return session.log

    def test_herdr(self):
        log = self.herdr()
        self.assertTrue(all(r["ok"] for r in log if r["check"] != "isolation_header_visible"), log)

    def test_herdr_mouse_capture_off_is_recorded(self):
        """User-side ``[ui] mouse_capture = false``: recorded only (what the user would see), never asserted."""
        log = self.herdr(mouse_capture=False)
        self.assertTrue(any(r["check"] == "ui_attached" and r["ok"] for r in log), log)


if __name__ == "__main__":
    unittest.main()
