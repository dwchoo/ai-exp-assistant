"""Runtime harness for CW-17: real entrypoint, the installed OMP, owned temp resources.

Each harness owns one temp root (data dir, profile, project cwd), one local
counting provider, and only the processes it can prove it started: the
backend recorded in ``backend.json`` and the members of the sessions it owns.
Cleanup uses pidfd_open plus a start-time recheck and pidfd_send_signal.

C-D72 (2) / p27-cw16-gap-01: the tests were written against OMP 18.2.10
(``HISTORICAL_OMP_VERSION``); the version is now recorded, not required
(stderr, and ``$WB_LIVE_OMP_VERSION_RECORD`` as JSON lines when set). Every run
is sandboxed: the child environment is built from scratch (no ``TMUX*``,
``HERDR_*``, ``WORKBENCH_*``, ``PI_*``, display or agent sockets), HOME is a
fake home inside the owned root whose ``~/.omp/agent/agent.db`` is an EMPTY
placeholder (the user's credential store is never reachable), XDG dirs and
TMPDIR live in the root, and ``HTTP(S)_PROXY``/``ALL_PROXY`` point at a closed
local port. The counting provider is wired into the Workbench OMP home
(``<data>/omp-root/agent/models.yml``) so ``--model cw17-probe/scripted``
resolves to it and ``provider.requests`` counts every model request: zero real
model requests by construction (no credential, proxies closed).
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
import pwd

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
HISTORICAL_OMP_VERSION = "omp/18.2.10"  # C-D72 (2): what the tests were written against; recorded, not required
BLOCKED_PROXY = "http://127.0.0.1:9"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
ZSH_DEFAULT = "/usr/bin/zsh"
SETSID = shutil.which("setsid", path="/usr/bin:/bin") or "/usr/bin/setsid"
TEST_OMP_ARGS = ("--no-session", "--no-pty", "--no-skills", "--no-rules", "--no-title", "--no-extensions",
                 "--no-tools", "--model", "cw17-probe/scripted")

sys.path.insert(0, str(SRC))
from workbench.backend.omp_home import AGENT_DIR_NAME, omp_root  # noqa: E402
from workbench.runtime.process_evidence import LinuxProcessProbe  # noqa: E402

_FOUND: dict[str, str | None] = {}


def _user_name_and_home() -> tuple[str, str]:
    try:
        entry = pwd.getpwuid(os.getuid())
        return entry.pw_name, entry.pw_dir  # not $HOME: the runner may itself have a fake HOME
    except KeyError:
        return "user", os.path.expanduser("~")


def sandbox_env(root: Path, **extra: str) -> dict[str, str]:
    """A from-scratch child environment confined to ``root`` (fake HOME with an empty agent.db, proxies closed)."""
    user, _ = _user_name_and_home()
    home = root / "h"
    (home / ".omp" / "agent").mkdir(parents=True, exist_ok=True)
    store = home / ".omp" / "agent" / "agent.db"
    if not store.exists():
        store.write_bytes(b"")  # EMPTY placeholder: no credential exists in this home
    (root / "t").mkdir(exist_ok=True)
    (root / "r").mkdir(mode=0o700, exist_ok=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "USER": user, "LOGNAME": user, "LANG": "C.UTF-8",
           "TERM": "xterm-256color", "TMPDIR": str(root / "t"), "XDG_RUNTIME_DIR": str(root / "r"),
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
           "XDG_STATE_HOME": str(home / ".local/state"), "XDG_CACHE_HOME": str(home / ".cache"),
           "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1", **{key: BLOCKED_PROXY for key in PROXY_KEYS}}
    env.update(extra)
    return env


def assert_sandboxed(env: dict[str, str], root: Path) -> None:
    """Fail closed when a child environment could reach the user's home, servers or network."""
    leaked = sorted(k for k in env if k.startswith(("TMUX", "HERDR_", "WORKBENCH_"))
                    or k in ("DISPLAY", "WAYLAND_DISPLAY", "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS"))
    if leaked:
        raise AssertionError(f"outer context leaked into a child env: {leaked}")
    if not Path(env.get("HOME", "/")).resolve().is_relative_to(root.resolve()):
        raise AssertionError(f"child HOME is not inside the owned root: {env.get('HOME')}")
    if any(env.get(key) != BLOCKED_PROXY for key in PROXY_KEYS):
        raise AssertionError("a proxy variable is not blocked")


def _record_version(candidate: str, version: str) -> None:
    line = {"omp": candidate, "version": version, "historical": HISTORICAL_OMP_VERSION,
            "pinned": False, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    print(f"[live_harness] OMP version recorded (not pinned, C-D72 (2)): {json.dumps(line)}", file=sys.stderr)
    target = os.environ.get("WB_LIVE_OMP_VERSION_RECORD")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")


def find_omp() -> str | None:
    """The installed OMP (any ``omp/*`` version; C-D72 (2): recorded, not pinned). Probed in a sandbox."""
    if "omp" in _FOUND:
        return _FOUND["omp"]
    _, user_home = _user_name_and_home()
    candidate = os.environ.get("WB_LIVE_OMP") or shutil.which("omp") or os.path.join(user_home, ".local/bin/omp")
    found = None
    if os.access(candidate, os.X_OK):
        probe_root = Path(tempfile.mkdtemp(prefix="cw17-ver-", dir="/tmp"))
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=30,
                                    env=sandbox_env(probe_root), cwd=probe_root, stdin=subprocess.DEVNULL)
            version = result.stdout.strip()
            if version.startswith("omp/"):
                found = candidate
                _record_version(candidate, version)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            shutil.rmtree(probe_root, ignore_errors=True)
    _FOUND["omp"] = found
    return found


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
    def __init__(self, omp: str, *, path: str | None = None, seed_provider: bool = True):
        self.omp = omp
        self.seed_provider = seed_provider  # False: the test needs the data dir untouched before start
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
        # p27-cw16-gap-01: built from scratch in a sandbox (fake HOME, empty agent.db, proxies closed) instead of
        # copying os.environ with the user's real HOME. No PI_CODING_AGENT_DIR: the product (C-D64) withholds it
        # from its OMPs and would resolve the "user" auth store from it; here that store is the fake home's empty
        # placeholder, which the product links into its OMP home (as in cw16_harness.Sandbox).
        env = sandbox_env(self.root, PATH=path or "/usr/bin:/bin", SHELL=ZSH_DEFAULT,
                          PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        assert_sandboxed(env, self.root)
        self.env = env
        self.clients: list[PtyRun] = []
        self.known: dict[str, tuple[int, int]] = {}
        self.sessions: set[int] = set()

    # -- entrypoint ------------------------------------------------------
    def argv(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "workbench", *args]

    def wire_provider(self) -> None:
        """Put the counting provider into the Workbench OMP home the product gives its OMPs (C-D64).

        The product replaces ``PI_CODING_AGENT_DIR`` with ``<data>/omp-root/agent``, so the profile's
        ``models.yml`` is not seen there; without this the scripted model would not resolve and
        ``provider.requests`` could not count anything. Never writes through a symlinked data dir."""
        if not self.seed_provider or self.data.is_symlink():
            return
        if not self.data.exists():
            self.data.mkdir(mode=0o700)
        agent = omp_root(self.data) / AGENT_DIR_NAME
        agent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (omp_root(self.data), agent):
            os.chmod(directory, 0o700)
        target = agent / "models.yml"
        target.write_bytes((self.profile / "models.yml").read_bytes())
        os.chmod(target, 0o600)

    def start_args(self, *extra: str) -> list[str]:
        self.wire_provider()
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
