"""L-CW19-RESTART (p27-cw19-test-01): real product entrypoint, real OMP binary, NO prompt typed (no model call).

Run (repo root)::

    PYTHONPATH=src /tmp/cw02-g1-venv/bin/python tests/recovery_boot/live_restart_independent_p27cw19.py [report.json]

Not a unittest module (never discovered). Everything lives under one /tmp root removed at the end: a fake HOME
(empty fake ``~/.omp/agent/agent.db``, no credential), a git project, the data dir, and the Workbench OMP home
whose only provider is a local counting HTTP endpoint on 127.0.0.1 (no model; any request would be counted and
answered 503). HTTP(S)/ALL_PROXY point at a closed port. No text is ever typed into an OMP pane, so OMP has no
reason to call a provider; the endpoint counter and the OMP session files (assistant messages) prove it.

Flow:
  A. ``start --no-attach`` -> ready. Attach the product UI in a PTY; in the host shell (user typing) start a
     ``nohup`` loop, a ``setsid`` sleep and a foreground loop (an experiment-like run); pause automation (UI).
  B. SIGKILL the backend (exact pid + start ticks); observe which previous processes live on.
  C. ``start --no-attach`` again -> reconcile record (same_boot_crash), survivors with verified identity, no
     replay (handoff journal / outbox), the pause kept, no backend_restarted notice (no open Task), boot ok.
  D. Metadata fault: data dir 0500, the host shell exits (the backend must write its record) -> held
     ``metadata_unavailable`` with the fault shown, backend and panes alive; data dir 0700 -> admission reopens.
  E. ``shutdown`` without --yes -> refused (exit 1) with the active work listed; ``shutdown --yes`` while previous
     survivors live -> not verified, exit 1, "종료 확인 실패". ``start`` again -> same_boot_unverified_stop with the
     survivors proven again; the probe ends its own survivors; ``shutdown --yes`` -> verified, exit 0.
Only processes started by this probe (their exact pid + start ticks) are signalled.
"""
from __future__ import annotations

import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
import threading
import time
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from workbench.backend.omp_home import AGENT_DIR_NAME, omp_root  # noqa: E402

BLOCKED_PROXY = "http://127.0.0.1:9"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
PREFIX = b"\x1d"
SETSID = shutil.which("setsid", path="/usr/bin:/bin") or "/usr/bin/setsid"
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>]|\x1b\][^\x07]*\x07")


def ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    fields = raw[raw.rfind(b") ") + 2:].split()
    return None if fields[0] in (b"Z", b"X") else int(fields[19])


def alive(pid: int | None, start: int | None) -> bool:
    return bool(pid) and start is not None and ticks(pid) == start


def kill_exact(pid: int, start: int, signum: int = signal.SIGKILL) -> bool:
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return False
    try:
        if ticks(pid) != start:
            return False
        signal.pidfd_send_signal(fd, signum)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def processes_mentioning(needle: str) -> dict[int, int]:
    found = {}
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            if os.stat(f"/proc/{name}").st_uid != os.geteuid():
                continue
            cmdline = Path(f"/proc/{name}/cmdline").read_bytes()
            try:
                cwd = os.readlink(f"/proc/{name}/cwd")
            except OSError:
                cwd = ""
            if needle.encode() in cmdline or cwd.startswith(needle):
                start = ticks(int(name))
                if start:
                    found[int(name)] = start
        except OSError:
            pass
    return found


class Counter:
    def __init__(self):
        self.requests: list[str] = []
        counter = self

        class Handler(BaseHTTPRequestHandler):
            def _any(self):  # noqa: N802
                counter.requests.append(f"{self.command} {self.path}")
                body = b'{"error":"cw19 probe: no model here"}'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST = _any  # noqa: N815

            def log_message(self, *_a):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Ui:
    def __init__(self, argv, env, cwd):
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 170, 0, 0))
        try:
            self.process = subprocess.Popen([SETSID, "--ctty", *argv], env=env, cwd=cwd, stdin=slave, stdout=slave,
                                            stderr=slave, close_fds=True)
        finally:
            os.close(slave)
        self.fd = master
        self.start = ticks(self.process.pid)
        self.buffer = b""

    def pump(self, seconds=0.2):
        deadline = time.monotonic() + seconds
        while self.fd >= 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                if select.select([self.fd], [], [], min(remaining, 0.05))[0]:
                    data = os.read(self.fd, 65536)
                    if not data:
                        return
                    self.buffer = (self.buffer + data)[-400_000:]
            except OSError:
                return

    def text(self) -> str:
        return ANSI.sub(b"", self.buffer).decode("utf-8", "replace")

    def send(self, data: bytes):
        try:
            os.write(self.fd, data)
        except OSError:
            pass
        self.pump(0.1)

    def type(self, text: str):
        for ch in text.encode():
            self.send(bytes([ch]))
        self.send(b"\r")

    def wait_text(self, pattern, timeout=20) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.2)
            if re.search(pattern, self.text()):
                return True
        return False

    def close(self):
        if self.process.poll() is None:
            try:
                os.write(self.fd, PREFIX + b"q")
                self.pump(0.5)
                os.write(self.fd, b"q")
            except OSError:
                pass
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                if self.start:
                    kill_exact(self.process.pid, self.start)
                self.process.wait(5)
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class Probe:
    def __init__(self):
        self.omp = shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
        self.root = Path(tempfile.mkdtemp(prefix="wb-cw19-live-", dir="/tmp"))
        self.home = self.root / "home"
        (self.home / ".omp" / "agent").mkdir(parents=True)
        (self.home / ".omp" / "agent" / "agent.db").write_bytes(b"")  # fake empty store, no credential
        self.data = self.home / "wbdata"
        self.project = self.root / "project"
        self.project.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(self.project), *a], capture_output=True, text=True,  # noqa
                                        timeout=15, check=True).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "cw19@example.invalid")
        git("config", "user.name", "cw19")
        (self.project / "README").write_text("cw19\n")
        git("add", "README")
        git("commit", "-qm", "base")
        self.counter = Counter()
        agent = omp_root(self.data) / AGENT_DIR_NAME
        agent.mkdir(parents=True, mode=0o700)
        os.chmod(self.data, 0o700)
        os.chmod(omp_root(self.data), 0o700)
        (agent / "models.yml").write_text(
            "providers:\n  wbcw19:\n"
            f"    baseUrl: http://127.0.0.1:{self.counter.server.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: cw19 none\n        contextWindow: 32768\n        maxTokens: 1024\n")
        self.env = {"PATH": "/usr/bin:/bin:" + str(Path(self.omp).parent), "HOME": str(self.home),
                    "SHELL": "/usr/bin/bash", "LANG": "C.UTF-8", "TERM": "xterm-256color",
                    "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_DATA_HOME": str(self.home / ".local/share"),
                    "XDG_STATE_HOME": str(self.home / ".local/state"), "XDG_CACHE_HOME": str(self.home / ".cache"),
                    **{key: BLOCKED_PROXY for key in PROXY_KEYS}, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        self.ui: Ui | None = None
        self.report: dict[str, Any] = {"root": str(self.root), "omp": self.omp, "commands": [], "checks": [],
                                       "observations": {}}
        self.owned: dict[str, tuple[int, int]] = {}

    # -- helpers -----------------------------------------------------------------------------------------------
    def cli(self, *args, timeout=180, record=True):
        started = time.time()
        out = subprocess.run([sys.executable, "-m", "workbench", *args], env=self.env, cwd=self.project,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        if record:
            self.report["commands"].append({
                "argv": ["python", "-m", "workbench", *[a.replace(str(self.root), "$ROOT") for a in args]],
                "exit": out.returncode, "at": started, "seconds": round(time.time() - started, 2),
                "stdout": out.stdout[-3000:].replace(str(self.root), "$ROOT"),
                "stderr": out.stderr[-2000:].replace(str(self.root), "$ROOT")})
        return out

    def status(self):
        out = self.cli("status", "--data-dir", str(self.data), "--json", timeout=30, record=False)
        try:
            value = json.loads(out.stdout)
        except ValueError:
            return None
        return value.get("snapshot") if value.get("running") else None

    def wait_status(self, predicate, timeout=90, what=""):
        deadline = time.monotonic() + timeout
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = self.status()
            if snapshot is not None and predicate(snapshot):
                return snapshot
            if self.ui is not None:
                self.ui.pump(0.3)
            else:
                time.sleep(0.3)
        raise AssertionError(f"timed out: {what}; last phase={snapshot and snapshot.get('phase')}")

    def check(self, name, ok, detail=None):
        self.report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail is not None and not ok else ""),
              flush=True)
        return ok

    def record(self):
        return json.loads((self.data / "backend.json").read_text())

    def attach(self):
        self.ui = Ui([sys.executable, "-m", "workbench", "attach", "--data-dir", str(self.data)], self.env,
                     str(self.project))
        self.ui.pump(3.0)

    def detach(self):
        if self.ui is not None:
            self.ui.close()
            self.ui = None

    def pid_file(self, name, timeout=15):
        path = self.root / f"{name}.pid"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ui is not None:
                self.ui.pump(0.2)
            else:
                time.sleep(0.2)
            text = path.read_text().strip() if path.exists() else ""
            if text.isdigit():
                pid = int(text)
                start = ticks(pid)
                if start:
                    self.owned[name] = (pid, start)
                    return pid, start
        raise AssertionError(f"{name} did not start")

    def start(self, label):
        out = self.cli("start", "--data-dir", str(self.data), "--omp", self.omp,
                       "--omp-arg=--model", "--omp-arg=wbcw19/scripted", "--no-attach")
        self.check(f"{label}: start exit 0", out.returncode == 0, out.stderr[-800:])
        ready = self.wait_status(lambda s: s.get("phase") == "ready", 120, f"{label} ready")
        record = self.record()
        backend = record["processes"]["backend"]
        self.report["observations"][f"{label}_backend"] = {"pid": backend["pid"], "start_ticks": backend["start_ticks"],
                                                           "boot_id": record.get("boot_id")}
        return ready, record

    # -- the flow ------------------------------------------------------------------------------------------------
    def run(self):
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.report["boot_id"] = boot_id
        version = subprocess.run([self.omp, "--version"], capture_output=True, text=True, timeout=30,
                                 env=self.env)
        self.report["omp_version"] = (version.stdout or version.stderr).strip()

        # A. first backend, host-shell work, pause
        ready1, record1 = self.start("A")
        self.check("A: boot record says the current boot", record1.get("boot_id") == boot_id)
        self.check("A: fresh classification", (ready1.get("startup") or {}).get("classification") == "fresh",
                   ready1.get("startup"))
        self.attach()
        self.ui.send(PREFIX + b"3")
        r = self.root
        self.ui.type(f"nohup sh -c 'echo $$ > {r}/nohup.pid; while :; do date +%s >> {r}/nohup.out; sleep 0.5; "
                     f"done' >/dev/null 2>&1 &")
        self.ui.type(f"setsid sh -c 'echo $$ > {r}/setsid.pid; exec sleep 900' >/dev/null 2>&1 </dev/null &")
        self.ui.type(f"sh -c 'echo $$ > {r}/fg.pid; while :; do echo CW19TICK; sleep 0.5; done'")
        nohup, setsid_, fg = self.pid_file("nohup"), self.pid_file("setsid"), self.pid_file("fg")
        self.check("A: foreground run prints in the host pane", self.ui.wait_text(r"CW19TICK", 15))
        self.ui.send(PREFIX + b"p")
        self.ui.wait_text(r"일시정지", 10)
        self.ui.send(b"p")
        paused = self.wait_status(lambda s: (s.get("automation") or {}).get("paused") is True, 30, "paused")
        self.check("A: automation paused by the user", paused["automation"]["paused"] is True)
        time.sleep(1.5)  # the pause file is written
        record1 = self.record()
        refs = {name: (ref["pid"], ref["start_ticks"]) for name, ref in record1["processes"].items()
                if isinstance(ref, dict) and ref.get("pid")}
        self.report["observations"]["A_refs"] = {k: list(v) for k, v in refs.items()}
        self.report["observations"]["A_user_processes"] = {"nohup": list(nohup), "setsid": list(setsid_),
                                                           "foreground": list(fg)}
        handoffs = self.data / "workflow" / "handoffs.jsonl"
        journal_before = handoffs.read_bytes() if handoffs.exists() else b""

        # B. SIGKILL the backend
        backend_pid, backend_ticks = refs["backend"]
        killed = kill_exact(backend_pid, backend_ticks)
        self.check("B: SIGKILL sent to the exact backend", killed)
        deadline = time.monotonic() + 10
        while alive(backend_pid, backend_ticks) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.check("B: backend gone", not alive(backend_pid, backend_ticks))
        time.sleep(3.0)
        after_kill = {name: alive(*ref) for name, ref in refs.items()}
        after_kill.update({"nohup_job": alive(*nohup), "setsid_job": alive(*setsid_), "foreground_run": alive(*fg)})
        self.report["observations"]["B_alive_after_sigkill"] = after_kill
        print(f"after SIGKILL alive: {after_kill}", flush=True)
        for name, ref in refs.items():  # remember what lives on so that cleanup can end it exactly
            if name != "backend" and alive(*ref):
                self.owned[f"old_{name}"] = ref
        self.detach()

        # C. restart via the entrypoint
        ready2, record2 = self.start("C")
        startup = ready2.get("startup") or {}
        self.report["observations"]["C_startup"] = startup
        self.check("C: classification same_boot_crash", startup.get("classification") == "same_boot_crash",
                   startup.get("classification"))
        self.check("C: previous backend pid named", (startup.get("previous") or {}).get("pid") == backend_pid)
        self.check("C: boot not pending (same boot)", (ready2.get("boot") or {}).get("confirmation_required")
                   is False, ready2.get("boot"))
        self.check("C: probed on the same boot", startup.get("probed") is True)
        states = {item["name"]: item["state"] for item in startup.get("processes") or []}
        for name, was_alive in after_kill.items():
            if name in states:
                expected = "alive" if was_alive else "ended"
                self.check(f"C: reconcile state of previous {name} = {expected}", states[name] == expected,
                           states[name])
        survivors = {item["pid"]: item for item in startup.get("survivors") or []}
        for name, ref in refs.items():
            if name != "backend" and after_kill.get(name):
                self.check(f"C: surviving {name} listed with verified identity",
                           survivors.get(ref[0], {}).get("identity") == "verified", survivors.get(ref[0]))
        if after_kill["nohup_job"]:
            self.check("C: surviving nohup job listed", nohup[0] in survivors, sorted(survivors))
        self.report["observations"]["C_setsid_job_listed"] = setsid_[0] in survivors
        self.check("C: every survivor shown alive is really alive with its recorded start ticks",
                   all(alive(pid, item["start_ticks"]) for pid, item in survivors.items()
                       if item.get("state") == "alive"),
                   {pid: item.get("state") for pid, item in survivors.items()})
        if after_kill["foreground_run"]:
            self.check("C: surviving foreground run listed", fg[0] in survivors, sorted(survivors))
        self.report["observations"]["C_survivor_summary"] = [
            {k: item.get(k) for k in ("survivor_id", "name", "pid", "comm", "state", "identity", "stoppable", "why_not")}
            for item in survivors.values()]
        self.check("C: nothing was signalled by the restart (survivors still alive)",
                   all(alive(*ref) for name, ref in refs.items() if name != "backend" and after_kill.get(name))
                   and (alive(*nohup) or not after_kill["nohup_job"]))
        automation = ready2.get("automation") or {}
        self.check("C: the pause is kept across the crash", automation.get("paused") is True, automation.get("state"))
        self.check("C: no backend_restarted notice without an open Task", startup.get("notice") is None,
                   startup.get("notice"))
        self.check("C: no outbox message listed as lost", startup.get("outbox_lost_count") == 0)
        journal_after = handoffs.read_bytes() if handoffs.exists() else b""
        new_lines = [json.loads(line) for line in journal_after[len(journal_before):].splitlines() if line.strip()]
        self.check("C: no outbox/delivery record after the restart (no replay)",
                   not [x for x in new_lines if x.get("type") == "outbox"], [x.get("type") for x in new_lines])
        new_omps = {name: record2["processes"][name]["pid"] for name in ("manager_omp", "worker_omp")}
        self.check("C: new OMP processes (not the survivors) are the bridge peers",
                   all(new_omps[name] != refs[name][0] for name in new_omps)
                   and all((ready2.get("bridge") or {}).get(role, {}).get("pid_matches_pane")
                           for role in ("manager", "worker")), {"new": new_omps, "bridge": ready2.get("bridge")})
        text_status = self.cli("status", "--data-dir", str(self.data), timeout=30)
        self.check("C: status shows the reconcile and the survivors", "backend 재시작 대조: same_boot_crash"
                   in text_status.stdout and "이전 backend가 남긴 process" in text_status.stdout,
                   text_status.stdout[-1500:])
        log = (self.data / "backend.log").read_text(errors="replace") if (self.data / "backend.log").exists() else ""
        stale = [line for line in log.splitlines() if re.search(r"token|hello|unauthori|reject", line, re.I)]
        self.report["observations"]["C_bridge_auth_log_lines"] = stale[-20:]

        # D. metadata fault: the record cannot be written while the host shell exits
        self.attach()
        os.chmod(self.data, 0o500)
        try:
            self.ui.send(PREFIX + b"3")
            self.ui.type("exit")
            held = self.wait_status(lambda s: any(h.get("reason") == "metadata_unavailable"
                                                  for h in s.get("holds") or []), 30, "metadata hold")
            self.report["observations"]["D_holds"] = held.get("holds")
            self.report["observations"]["D_faults"] = held.get("faults")
            self.check("D: metadata fault held and shown", bool((held.get("faults") or {}).get("metadata")),
                       held.get("faults"))
            self.check("D: backend keeps running (degraded, not stopped)", held.get("phase") in ("degraded", "ready"),
                       held.get("phase"))
            bridge = held.get("bridge") or {}
            self.check("D: both OMP sessions still connected and observed",
                       all((bridge.get(role) or {}).get("pid_matches_pane") for role in ("manager", "worker")),
                       bridge)
            self.report["observations"]["D_phase"] = [held.get("phase"), held.get("reason")]
            text = self.cli("status", "--data-dir", str(self.data), timeout=30)
            self.check("D: status prints the metadata fault", "metadata 저장 장애" in text.stdout, text.stdout[-800:])
        finally:
            os.chmod(self.data, 0o700)
        reopened = self.wait_status(lambda s: not any(h.get("reason") == "metadata_unavailable"
                                                      for h in s.get("holds") or []), 30, "metadata hold lifted")
        self.check("D: admission reopens after a durable write", (reopened.get("faults") or {}).get("metadata") is None)
        self.detach()

        # E. full shutdown: refused without confirmation; unverified while previous survivors live
        refused = self.cli("shutdown", "--data-dir", str(self.data), timeout=60)
        self.check("E: shutdown without --yes (no tty) refused, exit 1", refused.returncode == 1, refused.returncode)
        self.check("E: active work listed incl. previous survivors", "previous_survivor" in refused.stdout,
                   refused.stdout[-1500:])
        self.check("E: backend still running after the refusal", self.status() is not None)
        down = self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=120)
        result = None
        match = re.search(r"shutdown result: (.*)", down.stdout)
        if match:
            try:
                result = json.loads(match.group(1))
            except ValueError:
                result = None
        self.report["observations"]["E_shutdown_unverified"] = result
        survivors_alive = any(alive(*ref) for key, ref in self.owned.items() if key.startswith("old_")) \
            or alive(*nohup)
        if survivors_alive:
            self.check("E: shutdown with live survivors is not verified, exit 1",
                       down.returncode == 1 and result is not None and result.get("verified") is False,
                       {"exit": down.returncode, "result": result})
            self.check("E: '종료 확인 실패' shown", "종료 확인 실패" in down.stderr, down.stderr[-500:])
        deadline = time.monotonic() + 20
        while self.status() is not None and time.monotonic() < deadline:
            time.sleep(0.5)

        ready3, _record3 = self.start("F")
        startup3 = ready3.get("startup") or {}
        self.report["observations"]["F_startup"] = startup3
        self.check("F: classification after the unverified stop",
                   startup3.get("classification") == ("same_boot_unverified_stop" if survivors_alive
                                                       else "same_boot_clean_stop"), startup3.get("classification"))
        carried = {item["pid"] for item in startup3.get("survivors") or []}
        self.check("F: survivors proven again on the next start",
                   all(ref[0] in carried for key, ref in self.owned.items()
                       if key.startswith("old_") and alive(*ref)), sorted(carried))
        # The probe ends its own survivors (exact identity), then a confirmed shutdown is verified.
        for key, ref in list(self.owned.items()):
            if key.startswith("old_") or key in ("nohup", "setsid", "fg"):
                kill_exact(*ref)
        time.sleep(1.5)
        final = self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=120)
        match = re.search(r"shutdown result: (.*)", final.stdout)
        result = json.loads(match.group(1)) if match else None
        self.report["observations"]["F_shutdown"] = result
        self.check("F: shutdown with nothing left is verified, exit 0",
                   final.returncode == 0 and result is not None and result.get("verified") is True,
                   {"exit": final.returncode, "result": result, "stderr": final.stderr[-400:]})
        deadline = time.monotonic() + 20
        while self.status() is not None and time.monotonic() < deadline:
            time.sleep(0.5)
        self.boot_change(boot_id)

    def boot_change(self, boot_id):
        """G: the data dir's records name another boot (as after a reboot); hold until confirm-boot (CLI)."""
        from workbench.backend.paths import read_private_json, write_private_json
        other = "00000000-0000-4000-8000-00000000c019"
        for name, key in (("boot.json", "recorded_boot_id"), ("backend.json", "boot_id")):
            path = self.data / name
            value = read_private_json(path)
            value[key] = other
            if name == "boot.json":
                value["confirmed_boot_id"] = other
            write_private_json(path, value)
        ready, _record = self.start("G")
        boot = ready.get("boot") or {}
        startup = ready.get("startup") or {}
        self.report["observations"]["G_boot"] = boot
        self.report["observations"]["G_startup_processes"] = startup.get("processes")
        self.check("G: reboot classified, confirmation required", startup.get("classification") == "reboot"
                   and boot.get("confirmation_required") is True and boot.get("reason") == "reboot", boot)
        self.check("G: old-boot refs not probed", startup.get("probed") is False and all(
            item.get("state") == "ended_by_reboot" for item in startup.get("processes") or []),
            startup.get("processes"))
        self.check("G: admission held", any(h.get("reason") == "boot_confirmation_required"
                                            for h in ready.get("holds") or []), ready.get("holds"))
        text = self.cli("status", "--data-dir", str(self.data), timeout=30)
        self.check("G: status shows the confirmation wait", "부팅 확인 대기" in text.stdout, text.stdout[-600:])
        self.attach()  # a user action is allowed before the confirmation: resume the kept pause
        self.ui.send(PREFIX + b"p")
        self.ui.wait_text(r"재개", 10)
        self.ui.send(b"p")
        try:
            resumed = self.wait_status(lambda s: (s.get("automation") or {}).get("paused") is False, 45, "resume")
            self.check("G: user resume allowed while the boot is unconfirmed", True)
            self.report["observations"]["G_resume"] = (resumed.get("automation") or {}).get("resume")
        except AssertionError as exc:
            self.check("G: user resume allowed while the boot is unconfirmed", False, str(exc))
        still = self.status() or {}
        self.check("G: boot hold still on after the resume", any(h.get("reason") == "boot_confirmation_required"
                                                                for h in still.get("holds") or []))
        self.detach()
        refused = self.cli("confirm-boot", "--data-dir", str(self.data), timeout=30)
        self.check("G: confirm-boot without --yes/tty refused, exit 1", refused.returncode == 1, refused.stderr)
        self.check("G: confirm-boot shows both boot ids and the data dir",
                   other in refused.stdout and boot_id in refused.stdout and "data dir" in refused.stdout,
                   refused.stdout[-800:])
        confirmed = self.cli("confirm-boot", "--data-dir", str(self.data), "--yes", timeout=30)
        self.check("G: confirm-boot --yes exit 0", confirmed.returncode == 0, confirmed.stderr)
        after = self.status() or {}
        self.check("G: hold lifted after confirm-boot", not after.get("holds")
                   and (after.get("boot") or {}).get("confirmation_required") is False, after.get("holds"))
        again = self.cli("confirm-boot", "--data-dir", str(self.data), "--yes", timeout=30)
        self.check("G: a second confirm-boot has nothing pending, exit 1", again.returncode == 1, again.stderr)
        stored = json.loads((self.data / "boot.json").read_text())
        self.check("G: confirmation durable in boot.json", stored.get("pending") is False
                   and stored.get("confirmed_boot_id") == boot_id, stored.get("pending"))
        down = self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=120)
        self.check("G: final shutdown verified, exit 0", down.returncode == 0, down.stderr[-400:])

    def no_model_evidence(self):
        self.report["provider_requests"] = list(self.counter.requests)
        self.check("no request reached the local model endpoint", not self.counter.requests, self.counter.requests)
        assistant = 0
        files = 0
        for path in self.root.rglob("*.jsonl"):
            if "sessions" not in str(path) and "session" not in path.name:
                continue
            files += 1
            try:
                for line in path.read_text(errors="replace").splitlines():
                    if '"role":"assistant"' in line.replace(" ", ""):
                        assistant += 1
            except OSError:
                pass
        self.report["omp_session_files"] = files
        self.report["omp_assistant_messages"] = assistant
        self.check("zero assistant messages in the OMP session files", assistant == 0, {"files": files})

    def cleanup(self):
        self.detach()
        try:
            if self.status() is not None:
                self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=90, record=False)
        except Exception:
            pass
        for ref in self.owned.values():
            kill_exact(*ref)
        deadline = time.monotonic() + 20
        while processes_mentioning(str(self.root)) and time.monotonic() < deadline:
            time.sleep(0.3)
        left = processes_mentioning(str(self.root))
        for pid, start in left.items():
            kill_exact(pid, start)
        self.report["residue_processes_killed"] = sorted(left)
        self.counter.close()
        try:
            os.chmod(self.data, 0o700)
        except OSError:
            pass
        for _ in range(10):
            shutil.rmtree(self.root, ignore_errors=True)
            time.sleep(0.5)
            if not self.root.exists():
                break
        self.report["residue_root"] = self.root.exists()


def main() -> int:
    probe = Probe()
    error = None
    try:
        probe.run()
    except Exception as exc:  # recorded; cleanup always runs
        import traceback
        error = traceback.format_exc()
        probe.check("probe completed", False, error[-1500:])
    finally:
        try:
            probe.no_model_evidence()
        finally:
            probe.cleanup()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    probe.report["error"] = error
    if out:
        out.write_text(json.dumps(probe.report, indent=1, ensure_ascii=False, default=str))
    failed = [c["check"] for c in probe.report["checks"] if not c["ok"]]
    print(f"checks: {len(probe.report['checks'])}, failed: {failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
