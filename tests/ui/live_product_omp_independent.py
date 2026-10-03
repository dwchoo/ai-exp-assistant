"""Independent CW-06 live probe: real entrypoint + real OMP 18.2.10 + product UI (p27-cw06-test-01).

Runs ``python -m workbench start`` on a plain PTY this probe owns, with an owned
data dir and project dir, the user's existing OMP configuration (no profile
override, nothing under ~/.omp is read, copied or printed; ``--no-session`` only
keeps this probe's two sessions out of the user's history) and at most two tiny
model turns. Evidence written: only this probe's own markers, booleans, sizes,
identities and timings — never full OMP screens (they can show session titles).

Covers P-C-AC-01 (manager conversation), P-C-AC-06 (three areas, focus vs owner,
takeover), P-C-AC-14 (slash command, composer editing, Esc/Ctrl-C, prefix
literal) and the C-AC-19 paste / resize / detach / reattach / abrupt-kill items.

Run: PYTHONPATH=src:tests/ui python -m unittest tests/ui/live_product_omp_independent.py
Evidence JSON: $CW06_LIVE_EVIDENCE (default: <tmp root>/../cw06-live-evidence.json, printed path).
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
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

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from workbench.runtime.process_evidence import LinuxProcessProbe  # noqa: E402
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream  # noqa: E402
from workbench.ui.product.model import PANES, pane_inner_sizes  # noqa: E402
from workbench.ui.product.view import pane_rects  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402

OMP = os.environ.get("CW06_LIVE_OMP", os.path.expanduser("~/.local/bin/omp"))
OMP_VERSION = "omp/18.2.10"
PREFIX = b"\x1d"
START, END = b"\x1b[200~", b"\x1b[201~"
ROWS, COLS = 42, 213
MODEL_TURNS = os.environ.get("CW06_LIVE_MODEL") == "1"  # opt-in: real model turns only when CW06_LIVE_MODEL=1
MODEL_SKIP_REASON = "real model turns are opt-in: set CW06_LIVE_MODEL=1 to enable"


def omp_available() -> str | None:
    if not os.access(OMP, os.X_OK):
        return f"{OMP} is not executable"
    try:
        out = subprocess.run([OMP, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"omp --version failed: {exc}"
    return None if out == OMP_VERSION else f"omp version {out!r} != {OMP_VERSION}"


SKIP = omp_available()


def ticks(pid: int) -> int | None:
    try:
        return LinuxProcessProbe.start_ticks(pid)
    except OSError:
        return None


def kill_exact(pid: int, start: int) -> bool:
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        if ticks(pid) != start:
            return False
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(fd)


def session_members(sids: set[int]) -> dict[int, int]:
    found = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fields = Path(f"/proc/{name}/stat").read_bytes().rsplit(b") ", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[3]) in sids and fields[0] not in {b"Z", b"X"}:
            found[int(name)] = int(fields[19])
    return found


def pty_size_of(pid: int) -> tuple[int, int] | None:
    """Window size of the terminal on the process's stdin (opened O_NOCTTY, nothing read)."""
    try:
        target = os.readlink(f"/proc/{pid}/fd/0")
        if not target.startswith("/dev/pts/"):
            return None
        fd = os.open(target, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
        return rows, cols
    finally:
        os.close(fd)


class LiveTerm:
    """One entrypoint invocation on an owned PTY, wrapped by /bin/sh to record the outer termios."""

    def __init__(self, name: str, root: Path, env: dict[str, str], cwd: Path, *args: str,
                 rows: int = ROWS, cols: int = COLS):
        self.work = root / f"term-{name}"
        self.work.mkdir()
        self.rows, self.cols = rows, cols
        script = ('W="$1"; shift; stty -g > "$W/before"; "$WB_PY" -m workbench "$@"; echo $? > "$W/status"; '
                  'stty -g > "$W/after_raw"; stty sane; printf "\\033[?1049l\\033[?2004l\\033[?25h"; '
                  'stty -g > "$W/after_sane"; printf "\\n__SH_DONE__\\n"; exec sleep 300')
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.process = subprocess.Popen(["/usr/bin/setsid", "--ctty", "/bin/sh", "-c", script, "sh",
                                         str(self.work), *args], stdin=slave, stdout=slave, stderr=slave,
                                        close_fds=True, env=dict(env, WB_PY=sys.executable), cwd=str(cwd))
        os.close(slave)
        self.fd = master
        self.sid = self.process.pid
        self.output = bytearray()
        self._new_screen()

    def _new_screen(self) -> None:
        self.screen = TerminalScreen(self.cols, self.rows)
        self.stream = make_stream(self.screen)
        self.stream.feed(bytes(self.output))

    def drain(self, timeout: float = 0.05) -> None:
        try:
            while select.select([self.fd], [], [], timeout)[0]:
                data = os.read(self.fd, 1 << 16)
                if not data:
                    break
                self.output.extend(data)
                self.stream.feed(data)
                timeout = 0
        except OSError:
            pass

    def send(self, data: bytes, chunk: int = 4096) -> None:
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(self.fd, view[:chunk]):]
            except BlockingIOError:
                pass
            self.drain(0)

    def keys(self, *parts: bytes, gap: float = 0.15) -> None:
        for part in parts:
            self.send(part)
            self.pause(gap)

    def pause(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.drain(0.03)

    def wait(self, predicate, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.drain(0.05)
            if predicate():
                return True
        return predicate()

    def line(self, y: int) -> str:
        row = self.screen.buffer[y]
        return "".join(row[x].data for x in range(self.cols))

    def text(self) -> str:
        return "\n".join(self.line(y) for y in range(self.rows))

    def pane_lines(self, pane: PaneId) -> list[str]:
        top, left, height, width = pane_rects(self.rows, self.cols)[pane]
        out = []
        for y in range(top + 1, top + height - 1):
            row = self.screen.buffer[y]
            out.append("".join(row[x].data for x in range(left + 1, left + width - 1)))
        return out

    def pane(self, pane: PaneId) -> str:
        return "\n".join(self.pane_lines(pane))

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self._new_screen()

    def done(self) -> bool:
        return b"__SH_DONE__" in self.output

    def status(self) -> int | None:
        try:
            return int((self.work / "status").read_text())
        except (OSError, ValueError):
            return None

    def ui_pid(self) -> int | None:
        try:
            kids = Path(f"/proc/{self.sid}/task/{self.sid}/children").read_text().split()
        except OSError:
            return None
        return int(kids[0]) if kids else None

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.sid, signal.SIGKILL)  # this PTY's own session only
            except OSError:
                pass
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                pass
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def excerpt(text: str, *needles: str) -> list[str]:
    """Only lines carrying this probe's own markers (never whole OMP screens)."""
    return [line.strip()[:160] for line in text.splitlines() if any(n in line for n in needles)]


def restored_ratio(before: list[str], after: list[str]) -> float:
    """Share of the non-blank pane lines seen before detach that are on screen again after reattach."""
    want = [line.strip() for line in before if line.strip()]
    have = {line.strip() for line in after if line.strip()}
    return 1.0 if not want else sum(1 for line in want if line in have) / len(want)


def termios_flags(stty_g: str) -> dict[str, bool]:
    parts = stty_g.strip().split(":")
    lflag = int(parts[3], 16)
    return {"icanon": bool(lflag & termios.ICANON), "echo": bool(lflag & termios.ECHO),
            "isig": bool(lflag & termios.ISIG)}


@unittest.skipIf(SKIP, f"real OMP unavailable: {SKIP}")
class LiveProductOmpProbe(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw06-live-", dir="/tmp"))
        os.chmod(self.root, 0o700)
        self.data = self.root / "d"
        self.project = self.root / "project"
        self.project.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("TMUX", "HERDR_", "WORKBENCH_", "PYTHON"))}
        path = env.get("PATH", "/usr/bin:/bin")
        env.update(PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1", TERM="xterm-256color", LANG="C.UTF-8",
                   PATH=f"{Path(OMP).parent}:{path}")
        self.env = env
        self.terms: list[LiveTerm] = []
        self.known: dict[str, tuple[int, int]] = {}
        self.sessions: set[int] = set()
        self.ev: dict = {"model_turns_enabled": MODEL_TURNS, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "omp": OMP_VERSION,
                         "size": [ROWS, COLS], "steps": {}}
        self.evidence_path = Path(os.environ.get("CW06_LIVE_EVIDENCE")
                                  or self.root.parent / f"cw06-live-evidence-{self.root.name}.json")

    # -- helpers ---------------------------------------------------------
    def cli(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "workbench", *args, "--data-dir", str(self.data)],
                              env=self.env, cwd=self.project, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)

    def status(self) -> dict | None:
        out = json.loads(self.cli("status", "--json").stdout)
        return out["snapshot"] if out.get("running") else None

    def remember(self, snap: dict) -> None:
        backend = snap["backend"]["process"]
        self.known["backend"] = (backend["pid"], backend["start_ticks"])
        self.sessions.add(snap["backend"]["session_id"])
        for name, pane in snap["panes"].items():
            proc = pane.get("process") or {}
            if proc.get("pid"):
                self.known[name] = (proc["pid"], proc["start_ticks"])
                self.sessions.add(proc["pid"])

    @staticmethod
    def ident(snap: dict) -> dict:
        shell = snap["panes"]["host_shell"]["shell"]
        return {"backend": (snap["backend"]["process"]["pid"], snap["backend"]["process"]["start_ticks"]),
                "panes": {n: (p["process"]["pid"], p["process"]["start_ticks"], p.get("session_id"),
                              p.get("generation")) for n, p in snap["panes"].items()},
                "shell_parent": json.dumps(shell.get("parent"), sort_keys=True),
                "shell_generation": shell.get("generation")}

    def owner(self, snap: dict) -> tuple:
        shell = snap["panes"]["host_shell"]["shell"]
        return shell["input_owner"], shell["owner_epoch"], shell["parent_mode"]

    def term(self, name: str, *args: str, **kw) -> LiveTerm:
        term = LiveTerm(name, self.root, self.env, self.project, *args, **kw)
        self.terms.append(term)
        return term

    def step(self, name: str, **values) -> None:
        self.ev["steps"][name] = values

    def wait_status(self, predicate, timeout: float = 20.0) -> dict:
        end = time.monotonic() + timeout
        snap = None
        while time.monotonic() < end:
            snap = self.status()
            if snap is not None and predicate(snap):
                return snap
            for term in self.terms:
                term.drain(0.05)
            time.sleep(0.1)
        raise AssertionError(f"status predicate timeout (phase={snap and snap.get('phase')})")

    def shell_cmd(self, term: LiveTerm, command: str, marker_file: Path, timeout: float = 10.0) -> str | None:
        term.send(command.encode() + b"\r")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            term.drain(0.05)
            if marker_file.exists() and marker_file.read_text().endswith("\n"):
                return marker_file.read_text()
        return marker_file.read_text() if marker_file.exists() else None

    def tearDown(self):
        residue = {}
        try:
            for term in self.terms:
                term.close()
            if self.status() is not None:
                result = self.cli("shutdown", "--yes", "--json", timeout=90)
                self.ev["shutdown"] = {"exit": result.returncode,
                                       "verified": '"verified": true' in result.stdout}
            end = time.monotonic() + 20
            while time.monotonic() < end:
                alive = {n: p for n, (p, s) in self.known.items() if ticks(p) == s}
                members = session_members(self.sessions)
                if not alive and not members:
                    break
                time.sleep(0.1)
            residue = {"identities": {n: p for n, (p, s) in self.known.items() if ticks(p) == s},
                       "session_members": session_members(self.sessions),
                       "sockets": [p.name for p in (self.data / "ui.sock", self.data / "bridge.sock") if p.exists()]}
            residue = {k: v for k, v in residue.items() if v}
            for pid, start in list(self.known.values()) + list(session_members(self.sessions).items()):
                kill_exact(pid, start)  # fallback only; residue above already recorded as a failure
        finally:
            self.ev["residue"] = residue
            self.ev["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self.evidence_path.write_text(json.dumps(self.ev, indent=2, ensure_ascii=False, default=str))
            print(f"\n[cw06-live] evidence: {self.evidence_path}")
            shutil.rmtree(self.root, ignore_errors=True)
        self.assertEqual(residue, {}, "owned process/socket residue after confirmed shutdown")

    # -- the probe -------------------------------------------------------
    def test_live_product_ui_with_real_omp(self):
        P = self.project
        MGR, WRK, SH = PaneId.MANAGER_OMP, PaneId.WORKER_OMP, PaneId.HOST_SHELL

        # S0 start through the real entrypoint in a plain PTY; product UI attaches.
        t0 = time.monotonic()
        ui = self.term("start", "start", "--data-dir", str(self.data), "--omp", OMP, "--omp-arg=--no-session")
        self.assertTrue(ui.wait(lambda: "bridge manager=ok worker=ok" in ui.text(), 120),
                        [ui.line(0)[:150], ui.line(1)[:150], ui.line(ui.rows - 1)[:150]])
        first = self.wait_status(lambda s: s["phase"] == "ready" and s["attached"])
        self.remember(first)
        ids0 = self.ident(first)
        self.assertTrue(ui.wait(lambda: all(len(ui.pane(p).strip()) > 20 for p in (MGR, WRK)), 60))
        self.step("S0_start", seconds=round(time.monotonic() - t0, 1), phase=first["phase"],
                  backend_pid=ids0["backend"][0], product_ui="focus:" in ui.text() and "HOST SHELL" in ui.text(),
                  minimal_client_text="attached to manager_omp" in ui.text())
        self.assertFalse(self.ev["steps"]["S0_start"]["minimal_client_text"])

        # S1 P-C-AC-06: three areas + focus and owner shown separately.
        head = ui.line(0)
        titles = {t: t in ui.text() for t in ("MANAGER OMP", "WORKER OMP", "HOST SHELL")}
        o0 = self.owner(first)
        self.step("S1_three_areas", titles=titles, header=head.strip()[:200], owner=o0, focus=first["focus"])
        self.assertTrue(all(titles.values()))
        self.assertIn("focus: MANAGER OMP", head)
        self.assertIn(f"host 입력 owner: {o0[0]}", head)

        # S2 focus changes never change the owner.
        seen = []
        names = {"manager_omp": "MANAGER OMP", "worker_omp": "WORKER OMP", "host_shell": "HOST SHELL"}
        for key, pane in ((b"2", "worker_omp"), (b"3", "host_shell"), (b"1", "manager_omp"), (b"3", "host_shell")):
            ui.send(PREFIX + key)
            snap = self.wait_status(lambda s, pane=pane: s["focus"] == pane)
            shown = ui.wait(lambda pane=pane: f"focus: {names[pane]}" in ui.line(0)
                            and f"{names[pane]} *FOCUS*" in ui.text(), 3)
            seen.append((pane, self.owner(snap), shown))
            self.assertTrue(shown, f"focus {pane} not shown")
            self.assertEqual(self.owner(snap), o0, f"focus {pane} changed owner")
        self.step("S2_focus_vs_owner", sequence=[(p, list(o), shown) for p, o, shown in seen], owner_unchanged=True)

        # S3 host shell sees its pane size; OMP panes see theirs.
        want = {p: pane_inner_sizes(ROWS, COLS)[p] for p in PANES}
        size = self.shell_cmd(ui, f"stty size > {P}/size1", P / "size1")
        omp_sizes = {n: pty_size_of(self.known[n][0]) for n in ("manager_omp", "worker_omp")}
        self.step("S3_sizes", shell=size and size.strip(), want={p.value: want[p] for p in PANES},
                  omp=omp_sizes)
        self.assertEqual(size.strip(), "%d %d" % want[SH])
        self.assertEqual(omp_sizes["manager_omp"], want[MGR])
        self.assertEqual(omp_sizes["worker_omp"], want[WRK])

        # S8 owner (clean prompt boundary, before any multi-line input): handoff refused before wb-handoff
        # (visible), wb-handoff + prefix h hands the shell to the manager, typed/pasted input is then
        # refused visibly, focus does not change the owner, takeover request + confirm return it to the user.
        footer = lambda: ui.line(ui.rows - 1).strip()[:160]  # noqa: E731
        s8 = {"before": self.owner(self.status())}
        ui.send(PREFIX + b"h")  # before wb-handoff: must be refused visibly (C-AC-08 direct handoff)
        ui.pause(1.0)
        s8["early_handoff_footer"], s8["early_owner"] = footer(), self.owner(self.status())
        ui.send(b"wb-handoff\r")
        ui.pause(2.0)
        s8["after_wb_handoff"] = self.owner(self.status())
        ui.send(PREFIX + b"h")
        ui.pause(1.5)
        s8["after_handoff"], s8["handoff_footer"] = self.owner(self.status()), footer()
        s8["header_manager"] = "host 입력 owner: manager" in ui.line(0)
        ui.send(f"echo x > {P}/SHOULD-NOT-RUN\r".encode())
        s8["owner_refusal_visible"] = ui.wait(lambda: "input_owner_manager" in ui.text(), 5)
        s8["owner_refusal_footer"] = footer()
        ui.send(START + f"echo y > {P}/SHOULD-NOT-PASTE".encode() + END)
        ui.pause(0.8)
        s8["paste_refusal_footer"] = footer()
        ui.send(PREFIX + b"1")
        s_f = self.wait_status(lambda s: s["focus"] == "manager_omp")
        s8["owner_after_focus_while_manager"] = self.owner(s_f)
        ui.send(PREFIX + b"3")
        self.wait_status(lambda s: s["focus"] == "host_shell")
        ui.send(PREFIX + b"t")
        ui.pause(1.0)
        s_t = self.status()
        s8["after_request"] = self.owner(s_t)
        s8["takeover_requested"] = s_t["panes"]["host_shell"]["shell"].get("takeover_requested")
        s8["request_footer"] = footer()
        ui.send(PREFIX + b"c")
        ui.pause(1.0)
        s8["after_confirm"], s8["confirm_footer"] = self.owner(self.status()), footer()
        s8["header_user"] = "host 입력 owner: user" in ui.line(0)
        seen_1 = re.search(r"마지막 확인 (\d\d:\d\d:\d\d)", ui.line(1))
        ui.pause(6.5)
        seen_2 = re.search(r"마지막 확인 (\d\d:\d\d:\d\d)", ui.line(1))
        s8["last_confirmed"] = [seen_1 and seen_1.group(1), seen_2 and seen_2.group(1)]
        s8["refused_bytes_not_run"] = not (P / "SHOULD-NOT-RUN").exists() and not (P / "SHOULD-NOT-PASTE").exists()
        self.step("S8_owner", **s8)
        self.assertEqual(s8["after_handoff"][0], "manager", s8)
        self.assertTrue(s8["header_manager"], s8)
        self.assertTrue(s8["owner_refusal_visible"], s8)
        self.assertIn("input_owner_manager", s8["paste_refusal_footer"])
        self.assertEqual(s8["owner_after_focus_while_manager"], s8["after_handoff"])
        self.assertEqual(s8["after_confirm"][0], "user", s8)
        self.assertGreater(s8["after_confirm"][1], s8["after_handoff"][1])
        self.assertTrue(s8["header_user"])
        self.assertTrue(s8["refused_bytes_not_run"])
        self.assertNotEqual(s8["last_confirmed"][0], s8["last_confirmed"][1], "last-confirmed did not advance")
        time.sleep(0.5)
        self.shell_cmd(ui, f"echo OWNER-BACK > {P}/ob.txt", P / "ob.txt")

        # S4 Korean multi-line paste into bash arrives intact.
        body = f"cat > {P}/paste.txt <<'EOF'\n한글 첫째 줄\n둘째 줄 🙂 tab\there\nEOF\n".encode()
        ui.send(START + body + END)
        ui.pause(1.0)
        needed_enter = not (P / "paste.txt").exists()
        if needed_enter:
            ui.send(b"\r")
        ui.wait(lambda: (P / "paste.txt").exists(), 5)
        pasted = (P / "paste.txt").read_text() if (P / "paste.txt").exists() else None
        self.step("S4_korean_paste", intact=pasted == "한글 첫째 줄\n둘째 줄 🙂 tab\there\n",
                  needed_enter=needed_enter, observed=pasted)
        self.assertEqual(pasted, "한글 첫째 줄\n둘째 줄 🙂 tab\there\n")

        # S5 paste into a foreground program that did not enable bracketed paste (cat).
        ui.send(f"cat > {P}/cat.txt\r".encode())
        ui.pause(0.8)
        ui.send(START + "붙여 abc".encode() + END)
        ui.pause(0.3)
        ui.keys(b"\r", b"\x04")
        ui.wait(lambda: (P / "cat.txt").exists() and (P / "cat.txt").read_bytes().endswith(b"\n"), 5)
        raw = (P / "cat.txt").read_bytes() if (P / "cat.txt").exists() else b""
        self.step("S5_paste_to_cat_without_2004", bytes=raw.decode(errors="replace"),
                  markers_leaked=b"\x1b[200~" in raw or b"[200~" in raw)

        # S6 prefix-prefix literal, Esc, Ctrl-C reach the shell's foreground program.
        ui.send(f"cat -v > {P}/catv.txt\r".encode())
        ui.pause(0.8)
        ui.keys(PREFIX + PREFIX, b"\x1b", b"x\r")
        ui.pause(0.5)
        ui.send(b"\x03")
        ui.pause(1.0)  # bash re-arms readline after SIGINT; earlier type-ahead may be flushed (real tty behaviour)
        after_cc = self.shell_cmd(ui, f"echo AFTER-CC > {P}/cc.txt", P / "cc.txt")
        catv = (P / "catv.txt").read_text() if (P / "catv.txt").exists() else None
        self.step("S6_literal_prefix_esc_ctrlc", catv=catv, shell_back_after_ctrl_c=after_cc == "AFTER-CC\n")
        self.assertEqual(catv, "^]^[x\n")
        self.assertEqual(after_cc, "AFTER-CC\n")

        # S7 >2 MiB paste rejected whole with a visible reason; no byte reaches the shell.
        ui.send(START + b"#" * (2 * 1024 * 1024) + END, chunk=65536)
        visible = ui.wait(lambda: "paste_too_large" in ui.text(), 30)
        snap = self.status()
        after_big = self.shell_cmd(ui, f"echo OK-AFTER-BIG > {P}/big.txt", P / "big.txt")
        self.step("S7_big_paste", visible_reason=visible, footer=excerpt(ui.text(), "paste_too_large"),
                  queued=snap["panes"]["host_shell"].get("queued_input_bytes"),
                  clean_line_after=after_big == "OK-AFTER-BIG\n")
        self.assertTrue(visible)
        self.assertEqual(after_big, "OK-AFTER-BIG\n", "rejected paste bytes polluted the shell line")

        # S9 queue_full visible while the shell's foreground program does not read.
        ui.send(f"stty -echo; sleep 6; timeout 12 cat > /dev/null; stty echo; echo QF-DONE > {P}/qf.txt\r".encode())
        ui.pause(0.8)
        big = (b"y" * 99 + b"\n") * 15000  # ~1.43 MiB, queued behind the sleeping program
        ui.send(START + big + END, chunk=65536)
        ui.pause(0.5)
        ui.send(START + (b"z" * 99 + b"\n") * 8000 + END, chunk=65536)  # ~0.76 MiB > free space
        qf_visible = ui.wait(lambda: "queue_full" in ui.text(), 10)
        qf_foot = excerpt(ui.text(), "queue_full")
        done = ui.wait(lambda: (P / "qf.txt").exists(), 60)
        ui.send(b"\x03")
        self.step("S9_queue_full", visible=qf_visible, footer=qf_foot, drained_and_prompt_back=done)
        self.assertTrue(qf_visible)

        # S10 P-C-AC-01: a goal/code question in the manager pane, reply rendered there (model turn 1).
        if MODEL_TURNS:
            ui.send(PREFIX + b"1")
            self.wait_status(lambda s: s["focus"] == "manager_omp")
            prompt = "Code check: join the strings wb and ok, uppercase them, then append the value of 6*7. Reply with only that."
            t1 = time.monotonic()
            ui.send(prompt.encode())
            ui.pause(0.5)
            echoed = "join the strings" in ui.pane(MGR)
            ui.send(b"\r")
            replied = ui.wait(lambda: re.search(r"WBOK\s*42", ui.pane(MGR)) is not None, 180)
            reply_lines = excerpt(ui.pane(MGR), "WBOK")
            self.step("S10_manager_turn", prompt_echoed_in_manager_pane=echoed, reply_seen=replied,
                      reply_lines=reply_lines, seconds=round(time.monotonic() - t1, 1),
                      in_worker_pane="WBOK" in ui.pane(WRK))
            self.assertTrue(echoed)
            self.assertTrue(replied)
            self.assertFalse("WBOK" in ui.pane(WRK))
        else:
            self.step("S10_manager_turn", skipped=MODEL_SKIP_REASON)


        # S11 P-C-AC-14 in both OMP panes: composer edit, Alt+Enter newline, Ctrl-C clear (pressed once
        # on a non-empty editor only: a second press exits OMP), slash autocomplete + Esc, /hotkeys.
        s11 = {}
        for key, pane in ((b"2", WRK), (b"1", MGR)):
            ui.send(PREFIX + key)
            self.wait_status(lambda s, pane=pane: s["focus"] == pane.value)
            r = {}
            ui.keys(b"qzedit", b"X", b"\x7f")
            r["backspace"] = "qzedit" in ui.pane(pane) and "qzeditX" not in ui.pane(pane)
            ui.keys(b"\x1b\r", b"qzline2", gap=0.4)
            rows_edit = [i for i, line in enumerate(ui.pane_lines(pane)) if "qzedit" in line]
            rows_l2 = [i for i, line in enumerate(ui.pane_lines(pane)) if "qzline2" in line]
            r["alt_enter_newline"] = bool(rows_edit and rows_l2 and rows_l2[-1] > rows_edit[-1])
            ui.keys(b"\x03", gap=0.8)
            r["ctrl_c_cleared"] = "qzedit" not in ui.pane(pane) and "qzline2" not in ui.pane(pane)
            ui.keys(b"/hot", gap=1.0)
            r["slash_menu"] = excerpt(ui.pane(pane), "hotkeys")[:2]
            ui.keys(b"\x1b", gap=0.8)
            r["esc_closed_menu"] = not any("Show all keyboard shortcuts" in line for line in ui.pane_lines(pane))
            ui.keys(b"\x15", gap=0.3)  # Ctrl+U: clear the line without arming Ctrl+C exit
            ui.keys(b"/hotkeys", gap=0.8)
            ui.keys(b"\r", gap=1.5)
            r["hotkeys_table"] = sum(1 for line in ui.pane_lines(pane) if re.search(r"Ctrl\+|Alt\+|Slash commands", line))
            r["alive"] = self.status()["panes"][pane.value]["alive"]
            s11[pane.value] = r
        self.step("S11_tui_keys", **s11)
        for pane_name, r in s11.items():
            for field in ("backspace", "alt_enter_newline", "ctrl_c_cleared", "esc_closed_menu", "alive"):
                self.assertTrue(r[field], (pane_name, field, r))
            self.assertTrue(r["slash_menu"], (pane_name, r))
            self.assertGreaterEqual(r["hotkeys_table"], 3, (pane_name, r))

        # S12 Esc cancels a running turn in the worker pane (model turn 2).
        if MODEL_TURNS:
            ui.send(PREFIX + b"2")
            self.wait_status(lambda s: s["focus"] == "worker_omp")
            ui.send(b"Count from 1 to 400 as digits separated by single spaces, one line, nothing else.")
            ui.pause(0.3)
            ui.send(b"\r")
            def top() -> int:  # highest number inside a run of >= 5 counted numbers (not the prompt's "400")
                runs = re.findall(r"(?:\b\d{1,3}\s+){4,}\d{1,3}\b", ui.pane(WRK))
                return max([int(n) for run in runs for n in run.split()] or [0])
            streaming = ui.wait(lambda: re.search(r"\b1 2 3 4 5 6\b", ui.pane(WRK)) is not None, 90)
            at_esc = top()
            ui.send(b"\x1b")
            ui.pause(3.0)
            after_3s = top()
            ui.pause(3.0)
            text = ui.pane(WRK)
            self.step("S12_esc_cancel", streaming_seen=streaming, max_number_at_esc=at_esc,
                      max_number_3s=after_3s, max_number_6s=top(),
                      abort_marker=excerpt(text, "bort", "nterrupt", "ancel", "topped")[:3],
                      reached_400=bool(re.search(r"\b399 400\b", text)),
                      worker_alive=self.status()["panes"]["worker_omp"]["alive"])
        else:
            self.step("S12_esc_cancel", skipped=MODEL_SKIP_REASON)

        # S13 SIGWINCH: every pane process sees its new size.
        ui.resize(36, 173)
        ui.pause(1.5)
        want2 = {p: pane_inner_sizes(36, 173)[p] for p in PANES}
        ui.send(PREFIX + b"3")
        self.wait_status(lambda s: s["focus"] == "host_shell")
        size2 = self.shell_cmd(ui, f"stty size > {P}/size2", P / "size2")
        omp2 = {n: pty_size_of(self.known[n][0]) for n in ("manager_omp", "worker_omp")}
        self.step("S13_resize", shell=size2 and size2.strip(), omp=omp2, want={p.value: want2[p] for p in PANES})
        self.assertEqual(size2.strip(), "%d %d" % want2[SH])
        self.assertEqual(omp2["manager_omp"], want2[MGR])
        self.assertEqual(omp2["worker_omp"], want2[WRK])
        self.shell_cmd(ui, f"echo once >> {P}/count.txt; echo COUNT-MARK > {P}/cm.txt", P / "cm.txt")

        # S14 UI exit = detach; backend and all three identities unchanged.
        ui.pause(1.0)
        pre = {pane: ui.pane_lines(pane) for pane in PANES}
        ui.send(PREFIX + b"q")
        self.assertTrue(ui.wait(ui.done, 15))
        s_d = self.wait_status(lambda s: s["attached"] is False)
        self.step("S14_detach", exit=ui.status(), message="backend keeps running" in ui.output.decode(errors="replace"),
                  identities_same=self.ident(s_d) == ids0, all_alive=all(p["alive"] for p in s_d["panes"].values()),
                  outer_restored=(ui.work / "after_raw").read_text() == (ui.work / "before").read_text())
        self.assertEqual(ui.status(), 0)
        self.assertEqual(self.ident(s_d), ids0)
        self.assertEqual((ui.work / "after_raw").read_text(), (ui.work / "before").read_text())

        # S15 reattach restores all three panes; no duplicate input.
        ui2 = self.term("attach", "attach", "--data-dir", str(self.data), rows=36, cols=173)
        ratio = lambda term: {p.value: round(restored_ratio(pre[p], term.pane_lines(p)), 2) for p in PANES}  # noqa: E731
        restored = ui2.wait(lambda: min(ratio(ui2).values()) >= 0.8, 20)
        ui2.pause(2.0)
        s_r = self.wait_status(lambda s: s["attached"])
        self.step("S15_reattach", restored=restored, restored_ratio=ratio(ui2),
                  shell_tail="COUNT-MARK" in ui2.pane(SH), focus_kept=s_r["focus"],
                  identities_same=self.ident(s_r) == ids0, count_file=(P / "count.txt").read_text())
        self.assertTrue(restored)
        self.assertEqual((P / "count.txt").read_text(), "once\n", "duplicate input after reattach")
        self.assertEqual(s_r["focus"], "host_shell")
        self.assertEqual(self.ident(s_r), ids0)

        # S16 SIGKILL of the UI process: backend intact, outer terminal restorable, reattach works.
        pid = ui2.ui_pid()
        start = ticks(pid)
        self.assertTrue(kill_exact(pid, start))
        self.assertTrue(ui2.wait(ui2.done, 10))
        s_k = self.wait_status(lambda s: s["attached"] is False)
        raw = termios_flags((ui2.work / "after_raw").read_text())
        sane = termios_flags((ui2.work / "after_sane").read_text())
        ui3 = self.term("reattach", "attach", "--data-dir", str(self.data), rows=36, cols=173)
        back = ui3.wait(lambda: "HOST SHELL" in ui3.text() and min(ratio(ui3).values()) >= 0.8, 20)
        ratio3 = ratio(ui3)
        ui3.send(PREFIX + b"q")
        ui3.wait(ui3.done, 15)
        self.step("S16_sigkill_ui", identities_same=self.ident(s_k) == ids0, raw_after_kill=raw,
                  restored_ratio=ratio3,
                  after_stty_sane=sane, reattach_after_kill=back, reattach_exit=ui3.status())
        self.assertEqual(self.ident(s_k), ids0)
        self.assertTrue(all(sane.values()))
        self.assertTrue(back)
        self.assertEqual(ui3.status(), 0)
        self.ev["total_seconds"] = round(time.monotonic() - t0, 1)


if __name__ == "__main__":
    unittest.main()
