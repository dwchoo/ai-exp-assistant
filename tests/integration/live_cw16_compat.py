"""LIVE CW-16 I-COMPAT / P-C-AC-19 / P-C-AC-20 matrix through the product path (B1). Opt-in: WB_LIVE_CW16=1.

Every run starts the real backend with ``python -m workbench start`` (user's installed OMP, any version --
recorded, never pinned: C-D72 (2)) on a scripted local provider (ZERO model requests) and drives the
product UI (``python -m workbench attach``) inside one outer terminal:

* ``plain``  -- the product UI directly on an owned PTY;
* ``tmux``   -- an isolated tmux server (``-S <own socket>`` ``-f /dev/null``); the UI runs from the pane's
  shell and is rendered by an owned ``tmux attach`` client PTY;
* ``herdr``  -- an isolated Herdr session (own HERDR_CONFIG_PATH / XDG_* / HOME under /tmp, preflighted);
  the UI runs from the pane's shell (``herdr pane run``), rendered by an owned ``herdr --session`` client PTY.

and the product host terminal on ``bash`` (Bash chosen) or ``dash`` (no Bash on PATH: ``sh`` -> dash).

Steps per outer x host shell (each recorded pass / fail / n/a / not_run with the reason):
  C1 start + three areas + focus vs input owner shown separately (focus moves never change the owner)
  C2 input to all three panes, OMP composer keys (slash menu + Esc, Alt-Enter, Ctrl-C), Hangul/wide text,
     multi-line bracketed paste, prefix literal / Esc / Ctrl-C to a foreground program, >2 MiB paste refused
     with a visible reason and no byte reaching the shell
  C3 resize: the product and every pane child see the new size (and back)
  C4 OMP pane OSC 52 (OMP ``/copy`` of a scripted reply) reaches the outer terminal; host-pane OSC 52 does not;
     no tmux passthrough text drawn into a pane
  C5 UI detach (prefix q) >= 60 s with a user loop running in the host shell, then ``attach``: backend /
     OMP x2 / shell / supervisor identities unchanged, output produced while detached kept, owner/control
     unchanged, no new model request / Task, no duplicated input
  C6 outer client detach and reattach (tmux detach-client, herdr client quit) with the product UI alive
     inside; ``n/a`` for plain (no outer persistence layer)
  C7 ``shutdown --yes`` verified, then the outer is torn down: no owned process, socket or temp file remains

plus the shell-selection runs SEL1-SEL3 (C-D72 (1): no zsh-login check):
  SEL1 Bash first: PATH {bash, sh->dash, omp}, SHELL = a fake ``zsh`` sentinel that is never executed
  SEL2 no Bash: PATH {sh->dash, omp} -> /usr/bin/dash
  SEL3 neither: PATH {omp} -> start (attach mode, real PTY) prints the requirement, exits != 0, starts nothing

Run (repo root; the venv has pyte)::

    WB_LIVE_CW16=1 PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest tests/integration/live_cw16_compat.py -v

Filters: WB_CW16_OUTERS=plain,tmux,herdr  WB_CW16_SHELLS=bash,dash  WB_CW16_DETACH_SECONDS (default 65).
Reports: $WB_CW16_REPORT_DIR/<run id>/compat-<outer>-<shell>.json, sel-<n>.json, compat-matrix.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import unittest
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cw16_harness as h  # noqa: E402

LIVE = os.environ.get("WB_LIVE_CW16") == "1"
OUTERS = [o for o in os.environ.get("WB_CW16_OUTERS", "plain,tmux,herdr").split(",") if o]
SHELLS = [s for s in os.environ.get("WB_CW16_SHELLS", "bash,dash").split(",") if s]
DETACH_SECONDS = float(os.environ.get("WB_CW16_DETACH_SECONDS", "65"))
RUN_ID = os.environ.get("WB_CW16_RUN_ID") or time.strftime("cw16-b1-%Y%m%dT%H%M%S")
os.environ.setdefault("WB_CW16_RUN_ID", RUN_ID)
STEPS = ("C1", "C2", "C3", "C4", "C5", "C6", "C7")
EXPECTED_SHELL = {"bash": ("bash", "/usr/bin/bash"), "dash": ("sh", "/usr/bin/dash")}
BIG_PASTE = 2 * 1024 * 1024 + 64


def _skip_reason() -> str | None:
    if not LIVE:
        return "set WB_LIVE_CW16=1 (real OMP, scripted local provider, no model)"
    if sys.platform != "linux":
        return "Linux only"
    if not h.find_omp():
        return "OMP is not installed"
    if not h.pyte_available():
        return "pyte is required (use /tmp/cw02-g1-venv/bin/python)"
    return None


SKIP = _skip_reason()


def _ui_exit_marker(token: str) -> str:
    return f"__CW16_UI_EXIT_{token}_"


# ============================================================================================ outer adapters
class Outer:
    """One outer terminal around the product UI. ``client`` is the owned PTY renderer of the outermost terminal."""

    name = "outer"
    chrome_rows = 0
    drops_large_paste = False  # the outer itself discards very large bracketed pastes (observed for herdr 0.9.3)

    def __init__(self, sb: h.Sandbox, rows: int = h.ROWS, cols: int = h.COLS):
        self.sb, self.rows, self.cols = sb, rows, cols
        self.client: h.Ui | None = None
        self.facts: dict[str, Any] = {}
        self._token = secrets.token_hex(3)
        self._runs = 0

    # the command a user types in the outer's shell to start the product UI
    def attach_command(self) -> str:
        self._runs += 1
        marker = _ui_exit_marker(f"{self._token}{self._runs}")
        self._exit_marker = marker
        env = {k: self.sb.env[k] for k in ("PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "HOME", "LANG")}
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        argv = " ".join(shlex.quote(a) for a in self.sb.attach_argv())
        return f"env {prefix} {argv}; echo {marker}$?__"

    def open(self) -> None:
        raise NotImplementedError

    def detach_ui(self) -> int | None:
        """prefix q in the product UI; returns the UI's exit status when the outer shows it."""
        raise NotImplementedError

    def reattach_ui(self) -> None:
        raise NotImplementedError

    def detach_client(self) -> dict:
        raise h.NotApplicable(f"{self.name}: no outer persistence layer")

    def reattach_client(self) -> dict:
        raise h.NotApplicable(f"{self.name}: no outer persistence layer")

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.client.resize(rows, cols)

    def enable_clipboard(self) -> str | None:
        """Apply the outer's documented clipboard setting (the product's hint); None when there is none."""
        return None

    def close(self) -> dict:
        return {}

    def _wait_exit_marker(self, timeout: float = 20) -> int | None:
        pattern = re.escape(self._exit_marker) + r"(\d+)__"
        found: list[int] = []

        def seen() -> bool:
            match = re.search(pattern, self.screen_text())
            if match:
                found.append(int(match.group(1)))
            return bool(match)

        self.client.wait(seen, timeout)
        return found[0] if found else None

    def screen_text(self) -> str:
        return self.client.text()


class PlainOuter(Outer):
    name = "plain"

    def open(self) -> None:
        self.client = self.sb.ui(rows=self.rows, cols=self.cols, label="plain-ui")

    def detach_ui(self) -> int | None:
        self.client.send(h.PREFIX + b"q", settle=0.5)
        status = self.client.wait_exit(10)
        if status is None:  # an older build asked for a confirmation
            self.client.send(b"q", settle=0.3)
            status = self.client.wait_exit(10)
        return status

    def reattach_ui(self) -> None:
        self.client = self.sb.ui(rows=self.rows, cols=self.cols, label="plain-ui-2")


class TmuxOuter(Outer):
    """An isolated tmux server: own socket under the sandbox root, ``-f /dev/null``, env built from scratch."""

    name = "tmux"
    chrome_rows = 1  # the default status line
    session = "cw16"

    def __init__(self, sb: h.Sandbox, rows: int = h.ROWS, cols: int = h.COLS):
        super().__init__(sb, rows, cols)
        self.tmux = h.find_tool("tmux")
        if not self.tmux:
            raise h.NotApplicable("tmux is not installed")
        self.sock = sb.root / "x.sock"
        self.env = dict(sb.env, SHELL="/bin/sh", PATH=sb.env["PATH"])
        h.assert_env_isolated(self.env, sb.root)
        sb.cleanups.append(self.close)

    def tm(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
        return subprocess.run([self.tmux, "-S", str(self.sock), *args], env=self.env, cwd=self.sb.project,
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def open(self) -> None:
        made = subprocess.run([self.tmux, "-S", str(self.sock), "-f", "/dev/null", "new-session", "-d", "-s",
                               self.session, "-x", str(self.cols), "-y", str(self.rows), "/bin/sh"],
                              env=self.env, cwd=self.sb.project, capture_output=True, text=True, timeout=15,
                              stdin=subprocess.DEVNULL)
        if made.returncode:
            raise AssertionError(f"isolated tmux did not start: {made.stderr.strip()}")
        server = self.tm("display-message", "-p", "#{pid}").stdout.strip()
        if server.isdigit():
            self.sb.own(int(server))
            self.facts["server_pid"] = int(server)
        self.facts["socket"] = str(self.sock)
        self.facts["default_terminal"] = self.tm("show-options", "-gv", "default-terminal").stdout.strip()
        self.facts["set_clipboard"] = self.tm("show-options", "-gv", "set-clipboard").stdout.strip()
        self.facts["escape_time"] = self.tm("show-options", "-sv", "escape-time").stdout.strip()
        self._attach_client("tmux-client")
        self._run_attach()

    def _attach_client(self, label: str) -> None:
        self.client = self.sb.track_ui(h.Ui([self.tmux, "-S", str(self.sock), "attach", "-t", self.session],
                                            self.env, self.sb.project, rows=self.rows, cols=self.cols,
                                            label=label))
        if not self.client.wait(lambda: self.tm("list-clients").stdout.strip() != "", 10):
            raise AssertionError("tmux client did not attach")

    def _run_attach(self) -> None:
        self.tm("send-keys", "-t", self.session, "-l", self.attach_command())
        self.tm("send-keys", "-t", self.session, "Enter")

    def screen_text(self) -> str:
        captured = self.tm("capture-pane", "-p", "-t", self.session).stdout
        return self.client.text() + "\n" + captured

    def detach_ui(self) -> int | None:
        self.client.send(h.PREFIX + b"q", settle=0.5)
        return self._wait_exit_marker(20)

    def reattach_ui(self) -> None:
        self._run_attach()

    def enable_clipboard(self) -> str | None:
        done = self.tm("set-option", "-g", "set-clipboard", "on")
        self.facts["set_clipboard_after"] = self.tm("show-options", "-gv", "set-clipboard").stdout.strip()
        return f"tmux set-option -g set-clipboard on (exit {done.returncode})"

    def detach_client(self) -> dict:
        self.client.send(b"\x02d", settle=0.5)  # the user's tmux prefix: C-b d
        status = self.client.wait_exit(10)
        clients = self.tm("list-clients").stdout.strip()
        return {"client_exit": status, "clients_after": clients, "session_alive":
                self.tm("has-session", "-t", self.session).returncode == 0}

    def reattach_client(self) -> dict:
        self._attach_client("tmux-client-2")
        return {"clients": len(self.tm("list-clients").stdout.strip().splitlines())}

    def close(self) -> dict:
        if getattr(self, "_closed", False):
            return self.facts.get("teardown", {})
        self._closed = True
        result: dict[str, Any] = {}
        if self.sock.exists():
            result["kill_server_exit"] = self.tm("kill-server").returncode
        deadline = time.monotonic() + 10
        server = self.facts.get("server_pid")
        while time.monotonic() < deadline and server and h.alive(server, self.sb.owned.get(server)):
            time.sleep(0.1)
        result["server_gone"] = not (server and h.alive(server, self.sb.owned.get(server)))
        if self.client is not None:
            self.client.wait_exit(5)
        result["socket_left_by_tmux"] = self.sock.exists()  # tmux 3.4 leaves an -S socket file behind
        if self.sock.exists() and result["server_gone"]:
            self.sock.unlink()
        result["socket_removed"] = not self.sock.exists()
        self.facts["teardown"] = result
        return result


class HerdrOuter(Outer):
    """An isolated Herdr 0.9.x session: HERDR_CONFIG_PATH, XDG_* and HOME under the sandbox root (preflighted).

    The user's default Herdr server is never queried; the ancestry of this test process (which may run inside
    the user's Herdr) is recorded as information only (``nested_ancestry``); isolation is by env + own paths.
    """

    name = "herdr"
    chrome_rows = 1
    drops_large_paste = True

    def __init__(self, sb: h.Sandbox, rows: int = h.ROWS, cols: int = h.COLS):
        super().__init__(sb, rows, cols)
        self.herdr = h.find_tool("herdr")
        if not self.herdr:
            raise h.NotApplicable("herdr is not installed")
        base = sb.root / "hd"
        for sub in ("cfg", "data", "state", "cache"):
            (base / sub).mkdir(parents=True)
        runtime = base / "rt"
        runtime.mkdir(mode=0o700)
        config = base / "herdr.toml"
        config.write_text('onboarding = false\n[terminal]\ndefault_shell = "/bin/bash"\nshell_mode = "non_login"\n'
                          '[ui]\nsidebar_start_collapsed = true\nsidebar_collapsed_mode = "hidden"\n'
                          '[update]\nversion_check = false\nmanifest_check = false\n')
        self.env = dict(sb.env, XDG_CONFIG_HOME=str(base / "cfg"), XDG_DATA_HOME=str(base / "data"),
                        XDG_STATE_HOME=str(base / "state"), XDG_CACHE_HOME=str(base / "cache"),
                        XDG_RUNTIME_DIR=str(runtime), PATH=f"{sb.env['PATH']}:{Path(self.herdr).parent}")
        h.assert_env_isolated(self.env, sb.root)
        self.env["HERDR_CONFIG_PATH"] = str(config)  # the only HERDR_* key: our own disposable config
        self.name_ = "cw16" + secrets.token_hex(3)
        self.started = False
        sb.cleanups.append(self.close)

    def hd(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
        return subprocess.run([self.herdr, *args], env=self.env, cwd=self.sb.root, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)

    def _ancestry_has_herdr(self) -> bool:
        pid, names = os.getpid(), []
        while pid > 1:
            try:
                names.append(Path(f"/proc/{pid}/comm").read_text().strip())
                pid = int(next(line.split()[1] for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                               if line.startswith("PPid:")))
            except (OSError, StopIteration, ValueError):
                break
        return any(name.startswith("herdr") for name in names)

    def _sessions(self) -> list[dict]:
        listing = self.hd("session", "list", "--json")
        if listing.returncode:
            raise AssertionError(f"herdr session list failed: {listing.stderr.strip()[:300]}")
        return json.loads(listing.stdout).get("sessions", [])

    def open(self) -> None:
        self.facts["nested_ancestry_info"] = self._ancestry_has_herdr()
        sessions = self._sessions()
        isolated = all(Path(item[key]).resolve().is_relative_to(self.sb.root.resolve())
                       for item in sessions for key in ("session_dir", "socket_path") if item.get(key))
        self.facts["preflight_paths_isolated"] = isolated
        if not isolated:
            raise AssertionError(f"herdr isolation preflight failed: {sessions}")
        self.started = True
        self.client = self.sb.track_ui(h.Ui([self.herdr, "--session", self.name_], self.env, self.sb.root,
                                            rows=self.rows, cols=self.cols, label="herdr-client"))
        self.pane = self._wait_pane()
        for item in self._sessions():
            if item.get("name") == self.name_:
                self.facts["session"] = {k: item.get(k) for k in ("running", "session_dir", "socket_path")}
                if not Path(item["socket_path"]).resolve().is_relative_to(self.sb.root.resolve()):
                    raise AssertionError("herdr session socket outside the owned root")
        self._run_attach()

    def _wait_pane(self) -> str:
        deadline = time.monotonic() + 20
        last = ""
        while time.monotonic() < deadline:
            self.client.pump(0.2)
            listed = self.hd("--session", self.name_, "pane", "list")
            last = listed.stdout or listed.stderr
            if listed.returncode == 0:
                try:
                    found = _find_pane(json.loads(listed.stdout))
                except ValueError:
                    found = None
                if found:
                    return found
        raise AssertionError(f"isolated herdr pane unavailable: {last[:300]}")

    def _run_attach(self) -> None:
        done = self.hd("--session", self.name_, "pane", "run", self.pane, self.attach_command())
        if done.returncode:
            raise AssertionError(f"herdr pane run failed: {done.stderr.strip()[:300]}")

    def screen_text(self) -> str:
        read = self.hd("--session", self.name_, "pane", "read", self.pane, "--source", "visible")
        return self.client.text() + "\n" + (read.stdout if read.returncode == 0 else "")

    def detach_ui(self) -> int | None:
        self.client.send(h.PREFIX + b"q", settle=0.5)
        return self._wait_exit_marker(20)

    def reattach_ui(self) -> None:
        self._run_attach()

    def detach_client(self) -> dict:
        self.client.send(b"\x02q", settle=0.5)  # herdr prefix C-b q: quit this client, the session stays
        status = self.client.wait_exit(10)
        running = next((item.get("running") for item in self._sessions() if item.get("name") == self.name_), None)
        return {"client_exit": status, "session_running": running}

    def reattach_client(self) -> dict:
        self.client = self.sb.track_ui(h.Ui([self.herdr, "session", "attach", self.name_], self.env, self.sb.root,
                                            rows=self.rows, cols=self.cols, label="herdr-client-2"))
        return {}

    def close(self) -> dict:
        if getattr(self, "_closed", False) or not self.started:
            return self.facts.get("teardown", {})
        self._closed = True
        result = {"stop_exit": self.hd("session", "stop", self.name_, "--json").returncode}
        time.sleep(0.5)
        result["delete_exit"] = self.hd("session", "delete", self.name_, "--json").returncode
        try:
            result["session_deleted"] = all(item.get("name") != self.name_ for item in self._sessions())
        except (AssertionError, ValueError) as exc:
            result["session_deleted"] = f"unknown: {exc}"
        if self.client is not None:
            self.client.wait_exit(5)
        self.facts["teardown"] = result
        return result


def _find_pane(value: Any) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get("pane_id"), str):
            return value["pane_id"]
        for item in value.values():
            if found := _find_pane(item):
                return found
    if isinstance(value, list):
        for item in value:
            if found := _find_pane(item):
                return found
    return None


OUTER_TYPES = {"plain": PlainOuter, "tmux": TmuxOuter, "herdr": HerdrOuter}


# ============================================================================================ scenario
class CompatRun:
    """C1-C7 for one outer x host shell. Results go to ``compat-<outer>-<shell>.json``."""

    def __init__(self, outer_name: str, shell: str):
        self.outer_name, self.shell = outer_name, shell
        self.report = h.ScenarioReport(f"compat-{outer_name}-{shell}", run_id=RUN_ID, outer=outer_name,
                                       host_shell=shell)
        self.runner = h.StepRunner(self.report)
        self.hex = secrets.token_hex(3)
        self.copy_marker = f"CW16COPY{self.hex.upper()}"

    # -- helpers ------------------------------------------------------------------------------------------
    @property
    def ui(self) -> h.Ui:
        return self.outer.client

    def focus(self, pane: str) -> dict:
        self.ui.send(h.PREFIX + h.FOCUS_KEYS[pane], settle=0.3)
        return self.sb.wait_status(lambda s: s["focus"] == pane, 15, f"focus {pane}", pump=[self.ui])

    def host(self, command: str, path: Path, *, contains: str | None = None, timeout: float = 15) -> str | None:
        self.ui.send(command.encode() + b"\r", settle=0.2)
        return h.wait_file(path, contains=contains, timeout=timeout, pump=[self.ui])

    def product_size(self) -> tuple[int, int] | None:
        pids = self.sb.attach_processes()
        sizes = {pid: h.pty_size_of(pid) for pid in pids}
        sizes = {pid: size for pid, size in sizes.items() if size}
        return next(iter(sizes.values())) if sizes else None

    # -- the run --------------------------------------------------------------------------------------------
    def execute(self) -> dict:
        provider = h.ScriptedProvider()
        provider.on_text("c4copy", h.text(f"{self.copy_marker} scripted reply for the copy check"), role="manager")
        self.report.data["versions"] = h.tool_versions()
        self.sb = h.Sandbox(f"{self.outer_name[:2]}{self.shell[:2]}", provider=provider, host_shell=self.shell,
                            report=self.report)
        self.report.data["root"] = str(self.sb.root)
        try:
            self.outer = OUTER_TYPES[self.outer_name](self.sb)
            r = self.runner
            r.run("C1", self.c1)
            r.run("C2", self.c2, requires=["C1"])
            r.run("C3", self.c3, requires=["C1"])
            r.run("C4", self.c4, requires=["C1"])
            r.run("C5", self.c5, requires=["C1"])
            r.run("C6", self.c6, requires=["C1"])
            r.run("C7", self.c7, requires=["C1"])
            self.report.data["outer_facts"] = self.outer.facts
        except h.NotApplicable as exc:
            for step in STEPS:
                self.report.step(step, h.NOT_RUN, reason=str(exc))
        finally:
            self.report.data["provider"] = provider.snapshot()
            self.report.data["provider"]["log_tail"] = provider.log[-20:]
            cleanup = self.sb.close()
            provider.close()
            if cleanup.get("residue_before_fallback") and self.runner.status.get("C7") == h.PASS:
                self.report.step("C7", h.FAIL, assertion=f"residue at final cleanup: {cleanup}")
            self.report.data["report_path"] = str(self.report.path())
            self.report.write()
        return {step: self.report.data["steps"].get(step, {}).get("status", h.NOT_RUN) for step in STEPS}

    # C1 ---------------------------------------------------------------------------------------------------
    def c1(self) -> dict:
        started = self.sb.start()
        assert started.returncode == 0, started.stdout[-800:] + started.stderr[-800:]
        kind, exe = EXPECTED_SHELL[self.shell]
        announced = f"shell {kind} ({exe})"
        assert announced in started.stdout, started.stdout[-600:]
        ready = self.sb.wait_ready()
        shell = ready["panes"]["host_shell"]["shell"]
        parent = shell["parent"]
        assert (shell["kind"], h.exe_of(parent["pid"])) == (kind, exe), (shell["kind"], h.exe_of(parent["pid"]))
        isolation = (ready.get("omp_isolation") or {}).get("state")
        assert isolation != "failed", ready.get("omp_isolation")
        self.outer.open()
        assert self.ui.wait(lambda: all(t in self.ui.text() for t in h.TITLES.values()), 60), \
            self.ui.excerpt("OMP", "SHELL", "focus")
        attached = self.sb.wait_status(lambda s: s["attached"], 20, "attached", pump=[self.ui])
        self.ids0 = h.identity(attached)
        owner0 = h.owner_state(attached)
        assert owner0["input_owner"] == "user", owner0
        assert self.ui.wait(lambda: "host 입력 owner: user" in self.ui.text(), 10), self.ui.excerpt("owner")
        seen = []
        for pane in ("worker_omp", "host_shell", "manager_omp", "host_shell"):
            snap = self.focus(pane)
            shown = self.ui.wait(lambda pane=pane: f"focus: {h.TITLES[pane]}" in self.ui.text()
                                 and f"{h.TITLES[pane]} *FOCUS*" in self.ui.text(), 5)
            seen.append({"pane": pane, "shown": shown, "owner": h.owner_state(snap)["input_owner"],
                         "epoch": h.owner_state(snap)["owner_epoch"]})
            assert shown, f"focus {pane} not shown: {self.ui.excerpt('focus')}"
            assert h.owner_state(snap) == owner0, f"focus {pane} changed the owner: {h.owner_state(snap)}"
        return {"start_line": next((l for l in started.stdout.splitlines() if "shell" in l), ""),
                "shell": {"kind": shell["kind"], "exe": exe, "pid": parent["pid"]}, "isolation": isolation,
                "focus_sequence": seen, "owner": owner0, "omp_version": ready.get("omp_version")
                or self.report.data["versions"]["omp"], "header": self.ui.excerpt("focus:")[:1]}

    # C2 ---------------------------------------------------------------------------------------------------
    def c2(self) -> dict:
        P = self.sb.project
        out: dict[str, Any] = {}
        self.focus("host_shell")
        marker = f"C2H_{self.hex}"
        got = self.host(f"printf '%s\\n' {marker} > {P}/c2h.txt", P / "c2h.txt", contains=marker)
        out["host_marker"] = got == marker + "\n"
        assert out["host_marker"], got
        hangul = "한글 wide 字 🙂 C2K"
        got = self.host(f"printf '%s\\n' '{hangul}' > {P}/c2k.txt", P / "c2k.txt", contains="C2K")
        out["hangul_wide_typed"] = got == hangul + "\n"
        out["hangul_on_screen"] = self.ui.wait(lambda: "한글" in self.ui.text(), 5)
        assert out["hangul_wide_typed"], repr(got)
        body = f"cat > {P}/paste.txt <<'EOF'\n한글 첫째 줄\n둘째 줄 🙂 tab\there\nEOF\n".encode()
        self.ui.paste(body)
        if not h.wait_file(P / "paste.txt", contains="here", timeout=4, pump=[self.ui]):
            self.ui.send(b"\r", settle=0.3)
            out["paste_needed_enter"] = True
        pasted = h.wait_file(P / "paste.txt", contains="here", timeout=10, pump=[self.ui])
        out["multiline_paste_intact"] = pasted == "한글 첫째 줄\n둘째 줄 🙂 tab\there\n"
        assert out["multiline_paste_intact"], repr(pasted)
        # prefix literal, Esc, Ctrl-C to the host foreground program (cat -v shows what arrived)
        self.ui.send(f"cat -v > {P}/catv.txt\r".encode(), settle=0.8)
        self.ui.keys(h.PREFIX + h.PREFIX, b"\x1b", gap=0.7)
        self.ui.keys(b"x\r", gap=0.5)
        self.ui.send(b"\x03", settle=1.0)
        back = self.host(f"echo AFTER-CC > {P}/cc.txt", P / "cc.txt", contains="AFTER-CC")
        catv = (P / "catv.txt").read_text() if (P / "catv.txt").exists() else None
        out["catv"], out["shell_back_after_ctrl_c"] = catv, back == "AFTER-CC\n"
        assert catv == "^]^[x\n", repr(catv)
        assert out["shell_back_after_ctrl_c"], back
        # > 2 MiB paste refused whole, with a visible reason, the shell line stays clean
        dropped_before = self.sb.status()["panes"]["host_shell"].get("dropped_input_bytes")
        self.ui.paste(b"#" * BIG_PASTE, chunk=65536, settle=0.5)
        wait = 20 if self.outer.drops_large_paste else 60
        out["big_paste_reason_visible"] = self.ui.wait(lambda: "paste_too_large" in self.outer.screen_text(), wait)
        self.ui.pump(2.0)
        out["big_paste_bytes_on_host_line"] = "#" * 40 in self.ui.text()
        after = self.host(f"echo OK-AFTER-BIG > {P}/big.txt", P / "big.txt", contains="OK-AFTER-BIG")
        out["clean_line_after_big_paste"] = after == "OK-AFTER-BIG\n"
        assert out["clean_line_after_big_paste"], repr(after)
        assert not out["big_paste_bytes_on_host_line"], self.ui.excerpt("####")
        if not out["big_paste_reason_visible"] and self.outer.drops_large_paste:
            # herdr 0.9.3 drops bracketed pastes larger than ~1 MiB before any application sees them (raw probe:
            # 1 MiB delivered, 1.9 MiB and 2 MiB+64 not): the product's 2 MiB refusal is unreachable here.
            out["big_paste_product_refusal"] = "n/a: the outer did not deliver the paste"
            out["big_paste_dropped_input_bytes"] = [dropped_before, self.sb.status()["panes"]["host_shell"].get(
                "dropped_input_bytes")]
        else:
            assert out["big_paste_reason_visible"], self.ui.excerpt("paste", "MiB")
        # OMP composers: marker, Alt-Enter newline, Ctrl-C clear, slash menu + Esc, Hangul
        for pane in ("manager_omp", "worker_omp"):
            self.focus(pane)
            r: dict[str, Any] = {}
            m1, m2 = f"qz{pane[0]}{self.hex}", f"ql{pane[0]}{self.hex}"
            self.ui.type(m1, gap=0.03)
            r["typed_visible"] = self.ui.wait(lambda: m1 in self.ui.text(), 5)
            self.ui.keys(b"\x1b\r", gap=0.8)
            self.ui.type(m2, gap=0.03)

            def two_rows() -> bool:
                lines = self.ui.lines()
                rows1 = [i for i, line in enumerate(lines) if m1 in line]
                rows2 = [i for i, line in enumerate(lines) if m2 in line]
                return bool(rows1 and rows2 and rows2[-1] > rows1[-1])

            r["alt_enter_newline"] = self.ui.wait(two_rows, 5)
            self.ui.keys(b"\x03", gap=1.0)
            r["ctrl_c_cleared"] = self.ui.wait(lambda: m1 not in self.ui.text() and m2 not in self.ui.text(), 4)
            self.ui.type("/hot", gap=0.05)
            r["slash_menu"] = self.ui.wait(lambda: "hotkeys" in self.ui.text(), 5)
            self.ui.keys(b"\x1b", gap=1.0)
            r["esc_closed_menu"] = self.ui.wait(lambda: "/hot" not in self.ui.text()
                                                or "hotkeys" not in self.ui.text(), 3)
            self.ui.keys(b"\x15", gap=0.4)  # Ctrl-U: clear whatever is left, no exit arming
            hangul_m = f"한글{self.hex}"
            self.ui.type(hangul_m.encode(), gap=0.03)
            r["hangul_composer"] = self.ui.wait(lambda: hangul_m in self.ui.text(), 5)
            self.ui.keys(b"\x15", gap=0.4)
            r["alive"] = (self.sb.status() or {}).get("panes", {}).get(pane, {}).get("alive")
            out[pane] = r
            for key in ("typed_visible", "alt_enter_newline", "ctrl_c_cleared", "slash_menu", "esc_closed_menu",
                        "hangul_composer", "alive"):
                assert r[key], (pane, key, r)
        return out

    # C3 ---------------------------------------------------------------------------------------------------
    def _check_sizes(self, label: str) -> dict:
        P = self.sb.project
        size = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            size = self.product_size()
            if size and size[1] == self.outer.cols:
                break
            self.ui.pump(0.2)
        assert size, "product UI tty size unknown"
        want = h.pane_sizes_for(*size)
        omp = {}
        ok = self.ui.wait(lambda: all(h.pty_size_of(self.sb.known[n][0]) == want[n]
                                      for n in ("manager_omp", "worker_omp")), 10)
        for n in ("manager_omp", "worker_omp"):
            omp[n] = h.pty_size_of(self.sb.known[n][0])
        self.focus("host_shell")
        target = P / f"size-{label}.txt"
        shell = self.host(f"stty size > {target}", target, timeout=10)
        result = {"outer": [self.outer.rows, self.outer.cols], "product": list(size),
                  "want": {k: list(v) for k, v in want.items()}, "omp": {k: v and list(v) for k, v in omp.items()},
                  "shell": shell and shell.strip()}
        assert ok, result
        assert shell and shell.strip() == "%d %d" % want["host_shell"], result
        assert size[0] in (self.outer.rows, self.outer.rows - self.outer.chrome_rows), result
        return result

    def c3(self) -> dict:
        before = self._check_sizes("initial")
        self.outer.resize(34, 150)
        self.ui.pump(1.5)
        small = self._check_sizes("small")
        self.outer.resize(h.ROWS, h.COLS)
        self.ui.pump(1.5)
        back = self._check_sizes("back")
        return {"initial": before, "small": small, "back": back}

    # C4 ---------------------------------------------------------------------------------------------------
    def _copy_reply(self) -> dict:
        """OMP ``/copy`` in the manager pane (18.8.0: a picker, latest reply selected, Enter copies)."""
        r: dict[str, Any] = {}
        self.focus("manager_omp")
        osc_before = len(self.ui.osc52)
        self.ui.type("/copy", gap=0.05)
        self.ui.pump(0.8)
        self.ui.send(b"\r", settle=0.5)
        r["copy_picker"] = self.ui.wait(lambda: "copy" in self.ui.text() and "⏎" in self.ui.text(), 10)
        if r["copy_picker"]:
            self.ui.send(b"\r", settle=0.5)
        r["product_notice"] = self.ui.wait(lambda: "복사됨" in self.outer.screen_text(), 15)
        r["notice_lines"] = self.ui.excerpt("복사")
        self.ui.wait(lambda: any(self.copy_marker.encode() in d for _, d in self.ui.osc52[osc_before:]), 8)
        r["outer_received_osc52"] = any(self.copy_marker.encode() in d for _, d in self.ui.osc52[osc_before:])
        r["osc52_writes_seen"] = len(self.ui.osc52) - osc_before
        self.ui.keys(b"\x1b", gap=0.8)  # close anything left open
        return r

    def c4(self) -> dict:
        out: dict[str, Any] = {}
        before_requests = self.sb.provider.count("manager")
        self.focus("manager_omp")
        self.ui.type("c4copy", gap=0.03)
        self.ui.send(b"\r", settle=0.3)
        out["reply_visible"] = self.ui.wait(lambda: self.copy_marker in self.ui.text(), 60)
        assert out["reply_visible"], self.ui.excerpt("c4copy", "CW16")
        self.ui.pump(1.0)
        out["default_outer_config"] = first = self._copy_reply()
        received = first["outer_received_osc52"]
        if not received:
            # the outer drops application clipboard writes by default (tmux set-clipboard external): apply the
            # setting the product's notice names, then copy again
            setting = self.outer.enable_clipboard()
            if setting:
                out["outer_setting_applied"] = setting
                out["after_outer_setting"] = second = self._copy_reply()
                received = second["outer_received_osc52"]
        out["outer_received_osc52"] = received
        out["manager_requests"] = self.sb.provider.count("manager") - before_requests
        # host pane OSC 52 must never be forwarded
        self.focus("host_shell")
        host_marker = f"HOSTC4{self.hex}"
        osc_host = len(self.ui.osc52)
        self.ui.send(f"printf '\\033]52;c;%s\\007' $(printf {host_marker} | base64)\r".encode(), settle=2.5)
        out["host_osc52_forwarded"] = any(host_marker.encode() in d for _, d in self.ui.osc52[osc_host:])
        text = self.outer.screen_text()
        out["passthrough_text_drawn"] = "Ptmux" in text or "tmux;" in text
        assert first["product_notice"], out
        assert received, out
        assert not out["host_osc52_forwarded"], out
        assert not out["passthrough_text_drawn"], self.ui.excerpt("tmux")
        return out

    # C5 ---------------------------------------------------------------------------------------------------
    def c5(self) -> dict:
        P = self.sb.project
        self.focus("host_shell")
        loop = P / "loop.txt"
        command = (f"echo once >> {P}/count.txt; i=0; while [ $i -lt 400 ]; do i=$((i+1)); "
                   f"echo \"C5L $i\"; echo $i >> {loop}; sleep 1; done")
        self.ui.send(command.encode() + b"\r", settle=0.3)
        assert h.wait_file(loop, contains="3\n", timeout=15, pump=[self.ui]), "user loop did not start"
        before = self.sb.status()
        ids_before, owner_before = h.identity(before), h.owner_state(before)
        task_before = before.get("task")
        requests_before = self.sb.provider.snapshot()["requests"]
        attach_before = self.sb.attach_processes()
        ui_exit = self.outer.detach_ui()
        detached = self.sb.wait_status(lambda s: s["attached"] is False, 20, "detached", pump=[self.ui])
        attach_after_detach = self.sb.attach_processes()
        n_detach = len(loop.read_text().split())
        started = time.monotonic()
        while time.monotonic() - started < DETACH_SECONDS:
            self.ui.pump(0.5)
        during = self.sb.status()
        n_after = len(loop.read_text().split())
        self.outer.reattach_ui()
        shown = self.ui.wait(lambda: "HOST SHELL" in self.ui.text(), 30)
        attached = self.sb.wait_status(lambda s: s["attached"], 30, "reattached", pump=[self.ui])
        latest: list[int] = []

        def loop_visible() -> bool:
            numbers = [int(n) for n in re.findall(r"C5L (\d+)", self.ui.text())]
            latest[:] = numbers
            return bool(numbers) and max(numbers) > n_detach + DETACH_SECONDS - 10

        visible = self.ui.wait(loop_visible, 15)
        ids_after, owner_after = h.identity(attached), h.owner_state(attached)
        result = {"detach_seconds": round(time.monotonic() - started, 1), "ui_exit": ui_exit,
                  "ui_processes_before": len(attach_before), "ui_processes_while_detached": len(attach_after_detach),
                  "loop_lines_at_detach": n_detach, "loop_lines_after": n_after,
                  "identities_same": ids_after == ids_before == h.identity(during),
                  "owner_before": owner_before, "owner_after": owner_after, "focus_after": attached.get("focus"),
                  "task_same": attached.get("task") == task_before, "provider_requests_before": requests_before,
                  "provider_requests_after": self.sb.provider.snapshot()["requests"], "reattach_shown": shown,
                  "loop_output_visible_after_reattach": visible, "loop_numbers_seen_max": max(latest or [0])}
        # stop the user's loop (Ctrl-C to the host foreground program), the prompt is back
        self.focus("host_shell")
        self.ui.send(b"\x03", settle=1.0)
        back = self.host(f"echo C5-BACK > {P}/c5back.txt", P / "c5back.txt", contains="C5-BACK")
        result["prompt_back_after_ctrl_c"] = back == "C5-BACK\n"
        result["count_file"] = (P / "count.txt").read_text()
        assert ui_exit in (0, None) and not attach_after_detach, result
        if self.outer.name != "plain":
            assert ui_exit == 0, result
        assert detached["attached"] is False
        assert n_after - n_detach >= DETACH_SECONDS - 5, result
        assert result["identities_same"], (ids_before, ids_after)
        assert owner_after == owner_before, result
        assert result["task_same"] and result["provider_requests_after"] == requests_before, result
        assert shown and visible, result
        assert result["count_file"] == "once\n", result
        assert result["prompt_back_after_ctrl_c"], result
        return result

    # C6 ---------------------------------------------------------------------------------------------------
    def c6(self) -> dict:
        if self.outer.name == "plain":
            raise h.NotApplicable("plain PTY: the product UI is the outermost client; no outer persistence layer "
                                  "(UI detach/reattach is C5)")
        P = self.sb.project
        before = self.sb.status()
        ids_before = h.identity(before)
        ui_before = self.sb.attach_processes()
        old_client = self.ui
        detach = self.outer.detach_client()
        alive_after = {pid: h.alive(pid, start) for pid, start in ui_before.items()}
        mid = self.sb.status()
        time.sleep(3)
        reattach = self.outer.reattach_client()
        shown = self.ui.wait(lambda: "HOST SHELL" in self.ui.text(), 30)
        self.focus("host_shell")
        marker = f"C6_{self.hex}"
        got = self.host(f"printf '%s\\n' {marker} > {P}/c6.txt", P / "c6.txt", contains=marker)
        after = self.sb.status()
        result = {"detach": detach, "reattach": reattach, "product_ui_alive_while_client_away": alive_after,
                  "attached_while_client_away": mid and mid.get("attached"), "shown_after_reattach": shown,
                  "input_after_reattach": got == marker + "\n",
                  "ui_process_same": self.sb.attach_processes() == ui_before,
                  "identities_same": h.identity(after) == ids_before}
        assert old_client.process.poll() is not None, result
        assert ui_before and all(alive_after.values()), result
        for key in ("shown_after_reattach", "input_after_reattach", "ui_process_same", "identities_same"):
            assert result[key], (key, result)
        return result

    # C7 ---------------------------------------------------------------------------------------------------
    def c7(self) -> dict:
        ui_before = self.sb.attach_processes()
        done = self.sb.shutdown()
        payload = h.shutdown_result(done.stdout)
        verified = payload.get("verified")
        ui_gone = self.ui.wait(lambda: not any(h.alive(p, s) for p, s in ui_before.items()), 20)
        teardown = self.outer.close()
        left = self.sb.wait_no_residue(30)
        result = {"shutdown_exit": done.returncode, "verified": verified, "ui_clients_exited": ui_gone,
                  "outer_teardown": teardown, "residue": left,
                  "shutdown_keys": sorted(payload)[:20]}
        assert done.returncode == 0 and verified is True, (done.stdout[-600:], done.stderr[-400:])
        assert ui_gone, result
        assert left == {}, result
        if self.outer.name == "tmux":
            assert teardown.get("server_gone") and teardown.get("socket_removed"), result
        if self.outer.name == "herdr":
            assert teardown.get("session_deleted") is True, result
        return result


def write_matrix(run_id: str = RUN_ID) -> Path:
    """Aggregate every compat-*.json / sel-*.json of ``run_id`` into compat-matrix.json."""
    directory = h.REPORT_DIR / run_id
    matrix: dict[str, Any] = {"run_id": run_id, "written_at": h.iso_now(), "cells": {}, "selection": {},
                              "versions": None}
    for path in sorted(directory.glob("compat-*-*.json")):
        if path.name == "compat-matrix.json":
            continue
        data = json.loads(path.read_text())
        context = data.get("context", {})
        matrix["versions"] = matrix["versions"] or data.get("versions")
        matrix["cells"][f"{context.get('outer')}/{context.get('host_shell')}"] = {
            step: (data["steps"].get(step) or {}).get("status", h.NOT_RUN) for step in STEPS}
    for path in sorted(directory.glob("sel-*.json")):
        data = json.loads(path.read_text())
        matrix["selection"][data["scenario"]] = data.get("result")
    target = directory / "compat-matrix.json"
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(matrix, indent=1, ensure_ascii=False))
    return target


# ============================================================================================ unittest
@unittest.skipIf(SKIP, SKIP or "")
class Cw16CompatMatrix(unittest.TestCase):
    """One test per outer x host shell; each runs C1-C7 and fails when any step is not pass / n/a."""

    def _run(self, outer: str, shell: str) -> None:
        if outer not in OUTERS or shell not in SHELLS:
            self.skipTest(f"filtered out by WB_CW16_OUTERS/WB_CW16_SHELLS ({outer}/{shell})")
        cells = CompatRun(outer, shell).execute()
        print(f"\n[cw16-compat] {outer}/{shell}: {cells}", file=sys.stderr)
        bad = {k: v for k, v in cells.items() if v not in (h.PASS, h.NA)}
        self.assertEqual(bad, {}, f"{outer}/{shell}: see {h.REPORT_DIR / RUN_ID}/compat-{outer}-{shell}.json")
        if outer != "plain":
            self.assertEqual(cells["C6"], h.PASS)

    def test_plain_bash(self):
        self._run("plain", "bash")

    def test_plain_dash(self):
        self._run("plain", "dash")

    def test_tmux_bash(self):
        self._run("tmux", "bash")

    def test_tmux_dash(self):
        self._run("tmux", "dash")

    def test_herdr_bash(self):
        self._run("herdr", "bash")

    def test_herdr_dash(self):
        self._run("herdr", "dash")


@unittest.skipIf(SKIP, SKIP or "")
class Cw16ShellSelection(unittest.TestCase):
    """SEL1-SEL3 through ``python -m workbench start`` with a real PATH (C-D72 (1): Bash first, sh fallback)."""

    def _sandbox(self, scenario: str, links: dict[str, str]) -> tuple[h.Sandbox, h.ScenarioReport, Path]:
        report = h.ScenarioReport(scenario, run_id=RUN_ID)
        report.data["versions"] = h.tool_versions()
        sb = h.Sandbox(scenario, host_shell="custom", report=report)
        self.addCleanup(self._finish, sb, report)
        bindir = h.make_bindir(sb.root / "bin", links={**links, "omp": sb.omp})
        sentinel = sb.root / "zsh-ran"
        zsh = h.fake_zsh(bindir, sentinel)
        sb.use_path(str(bindir))
        sb.env["SHELL"] = str(zsh)
        report.data["context"] = {"path": sorted(p.name for p in bindir.iterdir()), "SHELL": "fake zsh sentinel"}
        return sb, report, sentinel

    def _finish(self, sb: h.Sandbox, report: h.ScenarioReport) -> None:
        cleanup = sb.close()
        sb.provider.close() if not sb.owns_provider else None
        report.data["cleanup"] = cleanup
        print(f"\n[cw16-sel] {report.data['scenario']}: {report.write()}", file=sys.stderr)

    def _selected(self, scenario: str, links: dict[str, str], kind: str, exe: str, probe: str) -> None:
        sb, report, sentinel = self._sandbox(scenario, links)
        runner = h.StepRunner(report)
        state: dict[str, Any] = {}

        def start() -> dict:
            done = sb.start()
            assert done.returncode == 0, done.stdout[-600:] + done.stderr[-600:]
            assert f"shell {kind} ({exe})" in done.stdout, done.stdout[-600:]
            ready = sb.wait_ready()
            shell = ready["panes"]["host_shell"]["shell"]
            pid = shell["parent"]["pid"]
            state["pid"], state["start"] = pid, shell["parent"].get("start_ticks") or h.ticks(pid)
            environ = h.owned_environ(pid, state["start"]) or []
            assert (shell["kind"], h.exe_of(pid)) == (kind, exe), (shell["kind"], h.exe_of(pid))
            assert f"SHELL={sb.env['SHELL']}".encode() in environ, "the user's SHELL setting changed"
            return {"announced": f"shell {kind} ({exe})", "kind": shell["kind"], "exe": h.exe_of(pid), "pid": pid,
                    "shell_env_preserved": True}

        def operate() -> dict:
            ui = sb.ui(label="sel-ui")
            assert ui.wait(lambda: "HOST SHELL" in ui.text(), 60), ui.excerpt("SHELL")
            ui.send(h.PREFIX + b"3", settle=0.3)
            sb.wait_status(lambda s: s["focus"] == "host_shell", 15, "focus host", pump=[ui])
            marker = sb.project / "sel-marker"
            ui.send(probe.format(marker=marker).encode() + b"\r", settle=0.3)
            value = h.wait_file(marker, endswith="", contains=":", timeout=15, pump=[ui])
            first, _, second = (value or "").partition(":")
            if kind == "bash":
                assert first.startswith("5.") and int(second) == state["pid"], value
            else:
                assert int(first) == state["pid"] and second == "none", value
            ui.send(h.PREFIX + b"q", settle=0.5)
            ui.wait_exit(10)
            return {"marker": value}

        def stop() -> dict:
            done = sb.shutdown()
            payload = h.shutdown_result(done.stdout)
            left = sb.wait_no_residue(30)
            assert done.returncode == 0 and payload.get("verified") is True, done.stdout[-500:]
            assert left == {}, left
            assert not sentinel.exists(), "the fake zsh default shell was executed"
            return {"verified": True, "zsh_sentinel_executed": sentinel.exists()}

        runner.run("start", start)
        runner.run("operate", operate, requires=["start"])
        runner.run("shutdown", stop, requires=["start"])
        self.assertEqual(runner.failures(), {}, report.data["steps"])

    def test_sel1_bash_first_fake_zsh_never_executed(self):
        self._selected("sel-1", {"bash": "/usr/bin/bash", "sh": "/usr/bin/dash"}, "bash", "/usr/bin/bash",
                       "printf '%s' \"$BASH_VERSION:$$\" > {marker}")

    def test_sel2_no_bash_falls_back_to_dash(self):
        self._selected("sel-2", {"sh": "/usr/bin/dash"}, "sh", "/usr/bin/dash",
                       "printf '%s' \"$$:${{BASH_VERSION:-none}}\" > {marker}")

    def test_sel3_neither_bash_nor_sh_guidance_nonzero_nothing_started(self):
        sb, report, sentinel = self._sandbox("sel-3", {})
        runner = h.StepRunner(report)

        def attempt() -> dict:
            before = sorted(str(p.relative_to(sb.data)) for p in sb.data.rglob("*"))
            run = h.Ui(sb.argv(*sb.start_args()), sb.env, sb.project, rows=30, cols=120, label="sel3-start")
            sb.track_ui(run)
            status = run.wait_exit(90)
            text = bytes(run.raw).decode(errors="replace")
            after = sorted(str(p.relative_to(sb.data)) for p in sb.data.rglob("*"))
            status_run = sb.cli("status", "--data-dir", str(sb.data), "--json", timeout=30)
            result = {"exit": status, "guidance": [l.strip() for l in text.splitlines()
                                                   if "Bash" in l or "backend" in l.lower()][:6],
                      "data_dir_unchanged": before == after, "backends": sb.backend_processes(),
                      "status_exit": status_run.returncode, "zsh_sentinel_executed": sentinel.exists()}
            assert status not in (None, 0), result
            assert "Bash" in text and "No backend was started" in text, result
            assert "attached to" not in text, result
            assert result["data_dir_unchanged"] and not result["backends"], result
            assert status_run.returncode == 3, result
            assert not sentinel.exists(), result
            return result

        runner.run("start_refused", attempt)
        self.assertEqual(runner.failures(), {}, report.data["steps"])


def tearDownModule() -> None:  # after both classes: the matrix holds the compat cells and SEL1-SEL3
    if not SKIP:
        print(f"\n[cw16-compat] matrix: {write_matrix()}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
