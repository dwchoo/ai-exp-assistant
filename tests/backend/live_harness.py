"""Runtime harness for CW-17: real entrypoint, real OMP 18.2.10, owned temp resources.

Each harness owns one temp root (data dir, profile, project cwd), one local
counting provider, and only the processes it can prove it started: the
backend recorded in ``backend.json`` and the members of the sessions it owns.
Cleanup uses pidfd_open plus a start-time recheck and pidfd_send_signal.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
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
import fcntl

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
OMP_VERSION = "omp/18.2.10"
ZSH_DEFAULT = "/usr/bin/zsh"
SETSID = shutil.which("setsid", path="/usr/bin:/bin") or "/usr/bin/setsid"
TEST_OMP_ARGS = ("--no-session", "--no-pty", "--no-skills", "--no-rules", "--no-title", "--no-extensions",
                 "--no-tools", "--model", "cw17-probe/scripted")

sys.path.insert(0, str(SRC))
from workbench.runtime.process_evidence import LinuxProcessProbe  # noqa: E402


def find_omp() -> str | None:
    candidate = shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
    if not os.access(candidate, os.X_OK):
        return None
    try:
        result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return candidate if result.stdout.strip() == OMP_VERSION else None


class _CountingHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        self.server.requests += 1
        self.send_error(503)

    do_GET = do_POST

    def log_message(self, *_args):
        return


def stat_fields(pid: int) -> list[bytes] | None:
    try:
        return Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def session_members(session_ids: set[int]) -> dict[int, int]:
    """pid -> start ticks for live processes in any of the given sessions."""
    members = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        fields = stat_fields(int(name))
        if fields and int(fields[3]) in session_ids and fields[0] not in {b"Z", b"X"}:
            members[int(name)] = int(fields[19])
    return members


def ticks(pid: int) -> int | None:
    try:
        return LinuxProcessProbe.start_ticks(pid)
    except OSError:
        return None


def kill_exact(pid: int, start_ticks: int) -> bool:
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        if LinuxProcessProbe.start_ticks(pid) != start_ticks:
            return False
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        return True
    except (ProcessLookupError, FileNotFoundError):
        return False
    finally:
        os.close(descriptor)


class PtyRun:
    """One entrypoint invocation on a plain PTY, output drained continuously."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str, size=(30, 100)):
        # setsid --ctty makes the PTY the controlling terminal without forking
        # this (multi-threaded) test process by hand; the PID stays the client's.
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))
        try:
            self.process = subprocess.Popen([SETSID, "--ctty", *argv], env=env, cwd=cwd, stdin=slave,
                                            stdout=slave, stderr=slave, close_fds=True)
        finally:
            os.close(slave)
        self.pid, self.fd = self.process.pid, master
        self.ticks = ticks(self.pid)
        self.output = bytearray()
        self.status: int | None = None

    def drain(self, timeout: float = 0.05) -> None:
        if self.fd < 0:
            return
        try:
            if select.select([self.fd], [], [], timeout)[0]:
                data = os.read(self.fd, 65536)
                if data:
                    self.output.extend(data)
        except OSError:
            pass

    def wait_output(self, marker: bytes, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while marker not in self.output:
            if time.monotonic() > deadline or self.poll() is not None:
                self.drain(0.2)
                if marker in self.output:
                    return
                raise AssertionError(f"PTY output lacks {marker!r}: {bytes(self.output[-2000:])!r}")
            self.drain()

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def poll(self) -> int | None:
        if self.status is None:
            self.status = self.process.poll()
        return self.status

    def wait(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        while self.poll() is None and time.monotonic() < deadline:
            self.drain()
        if self.status is None:
            raise AssertionError(f"PTY client did not exit: {bytes(self.output[-2000:])!r}")
        self.drain(0)
        return self.status

    def close(self) -> None:
        if self.poll() is None:
            kill_exact(self.pid, self.ticks)
            deadline = time.monotonic() + 5
            while self.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class LiveBackend:
    def __init__(self, omp: str, *, path: str | None = None):
        self.omp = omp
        self.root = Path(tempfile.mkdtemp(prefix="cw17-", dir="/tmp"))
        self.data = self.root / "d"
        self.project = self.root / "project"
        self.profile = self.root / "profile"
        for directory in (self.project, self.profile):
            directory.mkdir()
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
        self.provider.requests = 0
        self._provider_thread = threading.Thread(target=self.provider.serve_forever, kwargs={"poll_interval": 0.1},
                                                 daemon=True)
        self._provider_thread.start()
        (self.profile / "models.yml").write_text(
            "providers:\n  cw17-probe:\n"
            f"    baseUrl: http://127.0.0.1:{self.provider.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: CW17 fixture\n        contextWindow: 32768\n        maxTokens: 1024\n")
        (self.root / "config.yml").write_text("startup:\n  setupWizard: false\n")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HERDR_", "WORKBENCH_", "TMUX", "PI_", "PYTHON"))}
        env.update({"PATH": path or "/usr/bin:/bin", "SHELL": ZSH_DEFAULT, "PI_CODING_AGENT_DIR": str(self.profile),
                    "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1", "HOME": env.get("HOME", str(self.root)),
                    "LANG": "C.UTF-8"})
        self.env = env
        self.clients: list[PtyRun] = []
        self.known: dict[str, tuple[int, int]] = {}
        self.sessions: set[int] = set()

    # -- entrypoint ------------------------------------------------------
    def argv(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "workbench", *args]

    def start_args(self, *extra: str) -> list[str]:
        omp_args = [f"--omp-arg={item}" for item in (*TEST_OMP_ARGS, "--config", str(self.root / "config.yml"))]
        return ["start", "--data-dir", str(self.data), "--omp", self.omp, *omp_args, *extra]

    def cli(self, *args: str, timeout: float = 150) -> subprocess.CompletedProcess:
        return subprocess.run(self.argv(*args), env=self.env, cwd=self.project, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    def pty(self, *args: str) -> PtyRun:
        run = PtyRun(self.argv(*args), self.env, str(self.project))
        self.clients.append(run)
        return run

    def status(self) -> dict | None:
        result = self.cli("status", "--data-dir", str(self.data), "--json", timeout=30)
        value = json.loads(result.stdout)
        return value["snapshot"] if value.get("running") else None

    def record(self) -> dict | None:
        try:
            return json.loads((self.data / "backend.json").read_text())
        except (OSError, ValueError):
            return None

    def remember(self, snapshot: dict) -> None:
        """Bind cleanup to identities this harness observed from its own backend."""
        backend = snapshot["backend"]
        self.known["backend"] = (backend["process"]["pid"], backend["process"]["start_ticks"])
        self.sessions.add(backend["session_id"])
        for name, pane in snapshot["panes"].items():
            process = pane.get("process")
            if process:
                self.known[name] = (process["pid"], process["start_ticks"])
                self.sessions.add(process["pid"])  # each pane child leads its own session

    def backend_processes(self) -> list[int]:
        """Every live backend process for this data dir, found by cmdline."""
        found = []
        needle = f"_backend\0--data-dir\0{self.data}\0".encode()
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    if needle in Path(f"/proc/{name}/cmdline").read_bytes():
                        fields = stat_fields(int(name))
                        if fields and fields[0] not in {b"Z", b"X"}:
                            found.append(int(name))
                except OSError:
                    continue
        return found

    def leaks(self) -> dict:
        alive = {name: pid for name, (pid, start) in self.known.items() if ticks(pid) == start}
        return {"identities": alive, "session_members": session_members(self.sessions),
                "backends": self.backend_processes(),
                "sockets": [p.name for p in (self.data / "ui.sock", self.data / "bridge.sock") if p.exists()]}

    def shutdown(self) -> subprocess.CompletedProcess:
        return self.cli("shutdown", "--data-dir", str(self.data), "--yes", "--json", timeout=60)

    def cleanup(self) -> dict:
        """Normal path: confirmed shutdown. Fallback: exact pidfd kill. Returns residue."""
        for client in self.clients:
            client.close()
        if self.backend_processes():
            try:
                self.shutdown()
            except subprocess.TimeoutExpired:
                pass
        residue = self.leaks()
        if any(residue[key] for key in ("identities", "session_members", "backends")):
            for pid, start in self.known.values():
                kill_exact(pid, start)
            for pid, start in session_members(self.sessions).items():
                kill_exact(pid, start)
            deadline = time.monotonic() + 5
            while any(self.leaks()[k] for k in ("identities", "session_members")) and time.monotonic() < deadline:
                time.sleep(0.05)
        self.provider.shutdown()
        self.provider.server_close()
        self._provider_thread.join(5)
        shutil.rmtree(self.root, ignore_errors=True)
        return residue
