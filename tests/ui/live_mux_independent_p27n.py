"""LIVE mux-scrolling check (p27-cd59-test-02), opt-in: ``WB_LIVE_MUX=1`` (tmux 3.4 and herdr must be installed).

Run (from the repo root, short scratch TMPDIR)::

    WB_LIVE_MUX=1 PYTHONPATH=src:tests/ui TMPDIR=/tmp/wbp27n /tmp/cw02-g1-venv/bin/python -m unittest \\
        tests.ui.live_mux_independent_p27n -v

The user runs the product UI inside tmux or herdr and needs scrolling there (wheel + keyboard fallback), also after divider
drags. This module plays the OUTER terminal itself (a PTY it owns, rendered with pyte) for a real multiplexer client and
sends only the mouse reports a real terminal would send under the mouse mode the multiplexer asked for
(single-tracking-mode model, ``OuterMouse``: 1000/1002/1003 are ONE state, any reset clears it). Inside the multiplexer pane
the real product UI (``run_product``) attaches to a scripted ui_v1 fixture server owned by this test (no backend, no OMP, no
model, no network). Verdicts are read from the outer screen: a scrolled pane title carries ``[SCROLL``.

Isolation: tmux runs as ``tmux -L <unique> -f <empty conf>`` with ``TMUX_TMPDIR`` in our scratch dir (never the default socket
or ``~/.tmux.conf``); herdr runs with its own ``XDG_CONFIG_HOME`` (own server, socket, session and log; the user's herdr
server, panes and config are never touched; the user's ``config.toml`` is only copied, read-only, for fidelity). No TMUX*/HERDR*
variable of the caller is passed on. Every server is stopped by its own command and verified gone by pid + start time; every
process naming our scratch root is checked at the end (survivors are failures).
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import secrets
import shlex
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
from independent_support_cw06 import ScriptedServer  # noqa: E402
from test_product_resize_independent_p27l import OuterMouse  # noqa: E402

REPO = HERE.parents[1]
SRC = Path(os.environ.get("P27N_SRC") or REPO / "src")  # red-run knob: a mutant tree (the caller must point PYTHONPATH at it too)
PREFIX = b"\x1d"  # Ctrl-]
ROWS, COLS = 46, 150
LIVE = os.environ.get("WB_LIVE_MUX") == "1"
REPORT = os.environ.get("WB_LIVE_MUX_REPORT")
LINES = b"".join(b"line %03d\r\n" % i for i in range(120))
BOOT = ("import sys; sys.path.insert(0, {src!r}); from pathlib import Path; "
        "from workbench.ui.product import run_product; raise SystemExit(run_product(Path({sock!r})))")


def clean_env(root: Path, **extra: str) -> dict[str, str]:
    """Nothing of the caller's own multiplexers (TMUX*, HERDR_*) reaches a child."""
    env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME", "LANG") if key in os.environ}
    env.setdefault("LANG", "C.UTF-8")
    env.update(TERM="xterm-256color", SHELL="/bin/bash", WB_P27N_ROOT=str(root), PYTHONDONTWRITEBYTECODE="1")
    env.update(extra)
    return env


def ticks(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if fields[0] in "ZX" else fields[19]


def kill_identity(pid: int, start: str | None, sig: int) -> None:
    if start is None:
        return
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if ticks(pid) == start:  # the pid was not reused: still the process we started
            signal.pidfd_send_signal(fd, sig)
    except OSError:
        pass
    finally:
        os.close(fd)


def owned_processes(root: Path) -> dict[int, str]:
    found, needle = {}, str(root).encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            blob = (entry / "cmdline").read_bytes() + (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in blob and (start := ticks(int(entry.name))):
            found[int(entry.name)] = start
    return found


def note(record: dict) -> None:
    print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
    if REPORT:
        with open(REPORT, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class _Screen(pyte.Screen):
    def __init__(self, cols, rows, reply):
        super().__init__(cols, rows)
        self._reply = reply

    def write_process_input(self, data):
        self._reply(data.encode())

    def report_device_status(self, mode=0, **kwargs):
        if not kwargs.get("private"):
            super().report_device_status(mode)


class OuterTerminal:
    """A PTY we own, acting as the user's terminal emulator for one multiplexer client."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str):
        master, slave = os.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

        def controlling_tty():
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                                     start_new_session=True, close_fds=True, preexec_fn=controlling_tty)
        self.start = ticks(self.proc.pid)
        os.close(slave)
        self.fd = master
        self.screen = _Screen(COLS, ROWS, self._answer)
        self.stream = pyte.ByteStream(self.screen)
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
                data = os.read(self.fd, 65536) if select.select([self.fd], [], [], left)[0] else b""
            except OSError:
                data = b""
            if data:
                self.raw.extend(data)
                try:
                    self.stream.feed(data)
                except TypeError:
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

    @property
    def mouse(self) -> OuterMouse:
        return OuterMouse.of(bytes(self.raw))

    def report(self, button: int, x: int, y: int, release: bool = False) -> bool:
        """Send what a real terminal would for this action under the CURRENT mode; False = it would send nothing."""
        self.pump(0.05)
        model = self.mouse
        if not model.clicks or (button & 32 and not model.motion):
            return False
        self.send(f"\x1b[<{button};{x};{y}{'m' if release else 'M'}".encode())
        return True

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def find(self, needle: str) -> tuple[int, int] | None:
        """0-based (column, row) of the first occurrence of ``needle`` on the outer screen."""
        for row, line in enumerate(self.screen.display):
            at = line.find(needle)
            if at >= 0:
                return at, row
        return None

    def close(self) -> None:
        if self.proc.poll() is None:
            kill_identity(self.proc.pid, self.start, signal.SIGKILL)  # the client's own session group leader
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.close(self.fd)
        except OSError:
            pass


class Driver:
    """Drives the product UI inside one multiplexer client and records named checks."""

    def __init__(self, term: OuterTerminal, mux: str, fixture: ScriptedServer, root: Path):
        self.term, self.mux, self.fixture, self.root = term, mux, fixture, root
        self.log: list[dict] = []

    def ok(self, check: str, ok: bool, **info) -> bool:
        record = {"mux": self.mux, "check": check, "ok": bool(ok), **info}
        self.log.append(record)
        note(record)
        return bool(ok)

    def mode(self) -> dict:
        model = self.term.mouse
        return {"outer_mode": model.mode, "outer_sgr": model.sgr}

    def title(self, name: str) -> tuple[int, int] | None:
        """(column, row) of a pane title on its top border (header line 'focus: NAME ...' is skipped)."""
        for row, line in enumerate(self.term.screen.display):
            at = line.find(f" {name}")
            if at > 0 and "focus:" not in line:
                return at, row
        return None

    def scrolled(self, name: str) -> bool:
        where = self.title(name)
        return where is not None and "[SCROLL" in self.term.screen.display[where[1]]

    def wheel(self, label: str) -> bool:
        where = self.title("WORKER OMP")
        if where is None:
            return self.ok(label, False, why="worker pane not on the outer screen")
        x, y = where[0] + 5, where[1] + 4  # 1-based cell inside the worker pane
        delivered = self.term.report(64, x, y)
        scrolled = delivered and self.term.until(lambda: self.scrolled("WORKER OMP"), 5)
        back = False
        if scrolled:
            for _ in range(8):
                self.term.report(65, x, y)
                if self.term.until(lambda: not self.scrolled("WORKER OMP"), 1.2):
                    back = True
                    break
        return self.ok(label, delivered and scrolled and back, delivered=delivered, scrolled=bool(scrolled),
                       back_to_live=back, **self.mode())

    def drag(self, label: str, axis: str) -> bool:
        if axis == "v":
            a, b = self.title("MANAGER OMP"), self.title("WORKER OMP")
            if a is None or b is None:
                return self.ok(label, False, why="titles not on screen")
            x, y = b[0] - 1, b[1] + 3  # worker's left border cell (1-based x), inside the top row
            target = lambda: (self.title("WORKER OMP") or (None,))[0] == b[0] - 4  # noqa: E731
            steps = [(x - i, y) for i in (1, 2, 3, 4)]
            end = (x - 4, y)
        else:
            h = self.title("HOST SHELL")
            if h is None:
                return self.ok(label, False, why="host title not on screen")
            x, y = h[0] + 8, h[1] + 1  # the host pane's top border row (1-based y)
            target = lambda: (self.title("HOST SHELL") or (None, None))[1] == h[1] - 2  # noqa: E731
            steps = [(x, y - i) for i in (1, 2)]
            end = (x, y - 2)
        pressed = self.term.report(0, x, y)
        moved = [self.term.report(32, sx, sy) for sx, sy in steps]
        released = self.term.report(0, end[0], end[1], release=True)
        landed = self.term.until(target, 5)
        return self.ok(label, pressed and all(moved) and released and landed, pressed=pressed, motion=moved,
                       released=released, landed=landed, **self.mode())

    def key(self, label: str, keys: bytes, leave: bytes, name: str = "MANAGER OMP") -> bool:
        self.term.send(keys)
        scrolled = self.term.until(lambda: self.scrolled(name), 5)
        self.term.send(leave)
        back = self.term.until(lambda: not self.scrolled(name), 5)
        return self.ok(label, scrolled and back, scrolled=scrolled, back_to_live=back)

    def start_ui(self, command_prefix: bytes = b"") -> bool:
        boot = BOOT.format(src=str(SRC), sock=str(self.fixture.path))
        self.term.pump(0.5)
        self.term.send(b"\x15" + command_prefix)  # Ctrl-U: drop any terminal-query echo the shell may have received
        self.term.send(f"clear; {sys.executable} -c {shlex.quote(boot)}; echo UIDONE-$?".encode() + b"\r")
        seen = self.term.until(lambda: self.fixture.attached.is_set() and "line 119" in self.term.text()
                               and self.title("WORKER OMP") is not None, 30)
        return self.ok("ui_attached", seen, screen_tail=self.term.text()[-300:])

    def run(self, extra=None, after_exit=None) -> list[dict]:
        term = self.term
        if not self.start_ui():
            return self.log
        term.pump(0.5)
        model = term.mouse
        self.ok("mouse_tracking_on_after_attach", model.clicks and model.wheel, **self.mode())
        self.wheel("wheel_before_drag")
        self.drag("drag_1_vertical", "v")
        self.ok("mouse_still_on_after_drag_1", term.mouse.clicks, **self.mode())
        self.wheel("wheel_after_drag_1")
        self.drag("drag_2_horizontal", "h")
        self.drag("drag_3_vertical_again", "v")
        term.pump(0.5)
        self.ok("mouse_still_on_after_drags", term.mouse.clicks, **self.mode())
        self.wheel("wheel_after_three_drags")
        if extra:
            extra(self)
        self.key("shift_pgup_pgdn", b"\x1b[5;2~", b"\x1b[6;2~" * 8)
        self.key("prefix_pgup", PREFIX + b"\x1b[5~", b"q")
        self.key("prefix_bracket", PREFIX + b"[", b"q")
        term.send(PREFIX + b"m")  # capture off: the multiplexer decides the outer mode; the UI must not crash
        term.pump(0.6)
        self.ok("prefix_m_off_recorded", True, note="outer mode is the multiplexer's own choice", **self.mode())
        self.key("shift_pgup_with_capture_off", b"\x1b[5;2~", b"\x1b[6;2~" * 8)
        term.send(PREFIX + b"m")
        term.pump(0.6)
        self.wheel("wheel_after_prefix_m_off_on")
        self.drag("drag_after_prefix_m_off_on", "v")
        self.wheel("wheel_after_drag_following_prefix_m")
        term.send(PREFIX + b"d")
        detached = term.until(lambda: "UIDONE-0" in term.text(), 10)
        term.pump(0.6)
        self.ok("detach_exit_status_0", detached, **self.mode())
        if after_exit:
            after_exit(self)
        return self.log


@unittest.skipUnless(LIVE, "live multiplexer check: set WB_LIVE_MUX=1")
class LiveMux(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="wbp27n-", dir=os.environ.get("TMPDIR") or "/tmp"))
        cls.started: list[tuple[int, str]] = []

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

    def fixture(self) -> ScriptedServer:
        server = ScriptedServer(replay={"manager_omp": LINES, "worker_omp": LINES.replace(b"line", b"work"),
                                        "host_shell": LINES.replace(b"line", b"host")})
        self.addCleanup(server.close)
        return server

    def assert_all_ok(self, log: list[dict]) -> None:
        failed = [r for r in log if not r["ok"]]
        self.assertTrue(log and not failed, json.dumps(failed, ensure_ascii=False)[:3000])

    def tmux(self, mouse: bool) -> list[dict]:
        sock = f"wbp27n-{secrets.token_hex(4)}"
        conf = self.root / f"{sock}.conf"
        conf.write_text(("set -g mouse on\n" if mouse else "") + "set -g status off\n")
        env = clean_env(self.root, TMUX_TMPDIR=str(self.root))  # the server socket lives in our own dir
        base = ["tmux", "-L", sock, "-f", str(conf)]
        subprocess.run(base + ["new-session", "-d", "-s", "wb", "-x", str(COLS), "-y", str(ROWS), "bash --norc --noprofile"],
                       env=env, check=True, cwd=self.root)
        pid = int(subprocess.run(base + ["display", "-p", "#{pid}"], env=env, capture_output=True, text=True,
                                 check=True).stdout)
        start = ticks(pid)
        term = OuterTerminal(base + ["attach", "-t", "wb"], env, str(self.root))
        driver = Driver(term, f"tmux-mouse-{'on' if mouse else 'off'}", self.fixture(), self.root)

        def flags(drv: Driver) -> None:
            out = subprocess.run(base + ["display", "-p", "-t", "wb", "#{mouse_any_flag} #{mouse_standard_flag} "
                                         "#{mouse_button_flag} #{mouse_all_flag} #{mouse_sgr_flag}"], env=env,
                                 capture_output=True, text=True).stdout.split()
            drv.ok("tmux_pane_mouse_flags_still_set_after_drags", len(out) == 5 and out[0] == "1" and out[4] == "1",
                   any_std_btn_all_sgr=out)

        def gone(drv: Driver) -> None:
            out = subprocess.run(base + ["display", "-p", "-t", "wb", "#{mouse_any_flag} #{mouse_standard_flag} "
                                         "#{mouse_button_flag} #{mouse_all_flag} #{mouse_sgr_flag}"], env=env,
                                 capture_output=True, text=True).stdout.split()
            drv.ok("tmux_pane_mouse_flags_all_cleared_after_exit", out == ["0"] * 5, any_std_btn_all_sgr=out)

        try:
            term.pump(1.0)
            driver.run(extra=flags, after_exit=gone)
        finally:
            term.close()
            subprocess.run(base + ["kill-server"], env=env, capture_output=True, timeout=30)
            end = time.monotonic() + 8
            while time.monotonic() < end and ticks(pid) == start:
                time.sleep(0.05)
            driver.ok("tmux_server_gone", ticks(pid) != start, socket=sock, pid=pid)
        return driver.log

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
        user_config = Path.home() / ".config/herdr/config.toml"
        (xdg / "herdr/config.toml").write_text(user_config.read_text() if user_config.is_file() else "")  # read-only copy
        env = clean_env(self.root, XDG_CONFIG_HOME=str(xdg))  # own server, socket, session and log
        term = OuterTerminal([herdr], env, str(self.root))
        driver = Driver(term, "herdr", self.fixture(), self.root)
        log_path = xdg / "herdr" / "herdr-server.log"
        try:
            ready = term.until(lambda: "$" in term.text() or "#" in term.text(), 30)
            term.pump(2.0)
            driver.ok("herdr_started", ready, screen_head=term.text()[:200])
            if ready:
                driver.run()
        finally:
            warnings = []
            if log_path.exists():
                warnings = [line for line in log_path.read_text(errors="replace").splitlines() if "failed to encode" in line]
            driver.ok("herdr_no_mouse_encode_failures", not warnings, count=len(warnings), sample=warnings[:2])
            stop = subprocess.run([herdr, "server", "stop"], env=env, capture_output=True, text=True, timeout=30)
            term.close()
            driver.ok("herdr_server_stopped", stop.returncode == 0, out=(stop.stdout + stop.stderr)[-200:])
        self.assert_all_ok(driver.log)

    def test_zz_no_residue(self):
        time.sleep(1.0)
        left = owned_processes(self.root)
        self.assertEqual(left, {}, f"processes naming the scratch root are still alive: {left}")


if __name__ == "__main__":
    unittest.main()
