"""Shared live harness for the CW-16 final integration scenarios (B1 compat, B2 flow/policy/shell, B3 fault).

Not a unittest module (nothing here is collected by ``unittest discover``). It is imported by the
``live_cw16_*.py`` scenario files. Everything runs through the product entrypoint
(``python -m workbench start|attach|status|shutdown|confirm-boot``) with the user's installed OMP and a
scripted local OpenAI-compatible provider: ZERO real model requests.

Isolation rules this module enforces (the session that runs it may itself live inside the user's tmux
and herdr):

* every child environment is built from scratch (never copied from ``os.environ``): no ``TMUX*``,
  ``HERDR_*``, ``WORKBENCH_*``, ``PI_*``, ``DISPLAY``/``WAYLAND_DISPLAY``, no credentials; a fake HOME
  with an EMPTY ``~/.omp/agent/agent.db``; ``HTTP(S)_PROXY``/``ALL_PROXY`` point at a closed local port;
* every path lives under one ``/tmp`` root created by this process (``mkdtemp``);
* processes are signalled only by exact identity (pid + start ticks, through a pidfd) and only when this
  harness started them or their command line / cwd names this harness's own unique temp root;
* other processes' ``environ`` is never read (only processes this harness started, e.g. the host shell);
* reports are JSON files under ``$WB_CW16_REPORT_DIR`` (default ``/tmp/wb-cw16-reports``).

API summary (see the B1 result file for the long form):

* :class:`ScriptedProvider` -- rules ``on(predicate, responder, role=..., once=...)`` returning
  :class:`Turn` (text / tool calls / HTTP error / delayed / streamed); counters per role, injected
  Workbench message log, tool results, aborted streams.
* :class:`Sandbox` -- fake HOME, git project, data dir with the scripted ``models.yml``, host shell
  selection (``bash`` | ``dash`` | ``none`` or an explicit bindir), entrypoint helpers, owned identity
  tracking, residue check, bounded cleanup.
* :class:`Ui` -- any argv on an owned PTY rendered with pyte (+REP, SU/SD), OSC 52 scanner, bounded raw tail.
* :class:`ScenarioReport` / :class:`StepRunner` / :class:`NotApplicable` -- per-scenario JSON evidence.
"""
from __future__ import annotations

import base64
import binascii
from collections import Counter
from dataclasses import dataclass, field
import datetime as _dt
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import platform
import pwd
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
import traceback
from typing import Any, Callable, Iterable

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from workbench.backend.omp_home import AGENT_DIR_NAME, omp_root  # noqa: E402
from workbench.runtime.process_evidence import LinuxProcessProbe  # noqa: E402

PREFIX = b"\x1d"  # product prefix (Ctrl-])
FOCUS_KEYS = {"manager_omp": b"1", "worker_omp": b"2", "host_shell": b"3"}
TITLES = {"manager_omp": "MANAGER OMP", "worker_omp": "WORKER OMP", "host_shell": "HOST SHELL"}
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
ROWS, COLS = 40, 170
BLOCKED_PROXY = "http://127.0.0.1:9"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
SETSID = shutil.which("setsid", path="/usr/bin:/bin") or "/usr/bin/setsid"
PROVIDER_NAME = "wbcw16"
MODEL_ID = "scripted"
REPORT_DIR = Path(os.environ.get("WB_CW16_REPORT_DIR") or "/tmp/wb-cw16-reports")

PASS, FAIL, NA, NOT_RUN = "pass", "fail", "n/a", "not_run"


# ----------------------------------------------------------------------------------------------- environment
def _user_bin(name: str) -> str:
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir  # not $HOME: scenario processes run with a fake HOME
    except KeyError:
        home = os.path.expanduser("~")
    return os.path.join(home, ".local", "bin", name)


def find_tool(name: str) -> str | None:
    """``$WB_CW16_<NAME>`` override, then PATH, then the user's ~/.local/bin (installed tools only, never run here)."""
    for candidate in (os.environ.get(f"WB_CW16_{name.upper()}"), shutil.which(name), _user_bin(name)):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def find_omp() -> str | None:
    """The user's installed OMP (any version; C-D72 (2): the version is recorded, never pinned)."""
    return find_tool("omp")


def pyte_available() -> bool:
    return importlib.util.find_spec("pyte") is not None


def _first_line(argv: list[str], env: dict[str, str] | None = None) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=20, env=env or clean_base_env(),
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable ({type(exc).__name__})"
    out = (done.stdout or done.stderr).strip().splitlines()
    return out[0].strip() if out else f"unavailable (exit {done.returncode})"


def tool_versions(omp: str | None = None) -> dict[str, str]:
    """Versions of everything a CW-16 scenario touches. Only ``--version``-style calls: no server is contacted."""
    omp = omp or find_omp()
    tmux, herdr = find_tool("tmux"), find_tool("herdr")
    dash = "unavailable"
    query = _first_line(["dpkg-query", "-W", "-f=${Version}", "dash"])
    if query and not query.startswith("unavailable"):
        dash = f"dash {query}"
    return {
        "omp": _first_line([omp, "--version"]) if omp else "missing",
        "omp_path": omp or "missing",
        "tmux": _first_line([tmux, "-V"]) if tmux else "missing",
        "herdr": _first_line([herdr, "--version"]) if herdr else "missing",
        "bash": _first_line(["/usr/bin/bash", "--version"]) if os.path.exists("/usr/bin/bash") else "missing",
        "dash": dash,
        "sh": os.path.realpath("/bin/sh"),
        "python": platform.python_version(),
        "kernel": platform.release(),
        "os": _first_line(["sh", "-c", ". /etc/os-release && echo \"$PRETTY_NAME\""]),
    }


def clean_base_env(**extra: str) -> dict[str, str]:
    """A from-scratch environment: nothing inherited from this (possibly tmux/herdr-nested) session."""
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = "user"
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "xterm-256color", "USER": user, "LOGNAME": user}
    env.update(extra)
    for key in list(env):
        if key.startswith(("TMUX", "HERDR_", "WORKBENCH_")) or key in ("DISPLAY", "WAYLAND_DISPLAY"):
            del env[key]
    return env


def assert_env_isolated(env: dict[str, str], root: Path) -> None:
    """Fail closed when a child environment could reach the user's servers, home or network."""
    leaked = sorted(k for k in env if k.startswith(("TMUX", "HERDR_ENV", "HERDR_PANE", "HERDR_SOCKET", "WORKBENCH_"))
                    or k in ("DISPLAY", "WAYLAND_DISPLAY", "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS"))
    if leaked:
        raise AssertionError(f"outer context leaked into a child env: {leaked}")
    home = env.get("HOME", "")
    if not home or not Path(home).resolve().is_relative_to(root.resolve()):
        raise AssertionError(f"child HOME is not inside the owned root: {home}")
    for key in PROXY_KEYS:
        if env.get(key) != BLOCKED_PROXY:
            raise AssertionError(f"{key} is not blocked")


# ----------------------------------------------------------------------------------------------- processes
def ticks(pid: int) -> int | None:
    try:
        return LinuxProcessProbe.start_ticks(pid)
    except OSError:
        return None


def stat_fields(pid: int) -> list[bytes] | None:
    try:
        return Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def alive(pid: int, start: int | None) -> bool:
    fields = stat_fields(pid)
    return bool(fields) and fields[0] not in (b"Z", b"X") and start is not None and ticks(pid) == start


def kill_exact(pid: int, start: int | None, signum: int = signal.SIGKILL) -> bool:
    """Signal ``pid`` only if it is still the process that started at ``start`` ticks (pidfd: no reuse race)."""
    if not start:
        return False
    try:
        fd = os.pidfd_open(pid)
    except (ProcessLookupError, OSError):
        return False
    try:
        if ticks(pid) != start:
            return False
        signal.pidfd_send_signal(fd, signum)
        return True
    except (ProcessLookupError, OSError):
        return False
    finally:
        os.close(fd)


def processes_mentioning(needle: str) -> dict[int, int]:
    """pid -> start ticks of live processes whose cmdline or cwd names ``needle`` (an owned unique /tmp root)."""
    found: dict[int, int] = {}
    raw = needle.encode()
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        pid = int(name)
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        if raw in cmdline or cwd == needle or cwd.startswith(needle + "/"):
            fields = stat_fields(pid)
            start = ticks(pid)
            if start and fields and fields[0] not in (b"Z", b"X"):
                found[pid] = start
    return found


def session_members(session_ids: Iterable[int]) -> dict[int, int]:
    wanted = set(session_ids)
    members: dict[int, int] = {}
    if not wanted:
        return members
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        fields = stat_fields(int(name))
        if fields and int(fields[3]) in wanted and fields[0] not in (b"Z", b"X"):
            members[int(name)] = int(fields[19])
    return members


def pty_size_of(pid: int) -> tuple[int, int] | None:
    """(rows, cols) of the terminal on the process's stdin; opened O_NOCTTY, nothing read."""
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
    except OSError:
        return None
    finally:
        os.close(fd)


def exe_of(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def owned_environ(pid: int, start: int) -> list[bytes] | None:
    """``environ`` of a process this harness started (e.g. the product host shell); never call it on others."""
    if ticks(pid) != start:
        return None
    try:
        return Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None


def iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------------------------- provider
def text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return []


@dataclass
class Turn:
    """One scripted assistant response."""

    text: str | None = None
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    status: int = 200          # != 200: an HTTP error response (model fault)
    error_body: str = ""
    delay: float = 0.0         # seconds before the response starts (a slow model turn)
    chunks: int = 1            # text split into this many SSE chunks ...
    chunk_gap: float = 0.0     # ... with this pause between them (a streaming turn that can be interrupted)


def text(value: str, **kw: Any) -> Turn:
    return Turn(text=value, **kw)


def tools(*calls: tuple[str, dict], **kw: Any) -> Turn:
    return Turn(tool_calls=list(calls), **kw)


def error(status: int = 503, body: str = "scripted overload", **kw: Any) -> Turn:
    return Turn(status=status, error_body=body, **kw)


@dataclass
class Request:
    """What a rule sees about one provider request (parsed once)."""

    index: int
    role: str                     # manager | worker | unknown (decided by the tool list)
    body: dict
    tool_names: list[str]
    last_role: str | None
    last_text: str
    injected: dict | None         # a Workbench-injected JSON message (workbench_message_id) if the last message is one
    tool_results: dict[str, Any]  # tool_call_id -> parsed result of the trailing tool messages


class ScriptedProvider:
    """A local OpenAI-compatible (``openai-completions``) SSE server driven by rules. No model anywhere.

    ``on(predicate, responder, role=None, once=False)``: rules are tried in insertion order; the first whose
    role matches and whose ``predicate(request)`` is true answers with ``responder(request)`` (a
    :class:`Turn`, a ``str`` (text) or ``None`` to fall through). Unmatched requests get ``default``.
    """

    def __init__(self, name: str = PROVIDER_NAME, *, default: Turn | None = None, log_limit: int = 2000):
        self.name = name
        self.default = default or Turn(text="ok")
        self.lock = threading.Lock()
        self.rules: list[dict] = []
        self.requests: Counter = Counter()
        self.log: list[dict] = []
        self.log_limit = log_limit
        self.injected: dict[str, list[dict]] = {"manager": [], "worker": [], "unknown": []}
        self.tool_results: dict[str, Any] = {}
        self.tool_names: dict[str, list[str]] = {}
        self.aborted: list[dict] = []
        self.errors: list[str] = []
        self._calls = 0
        self._changed = threading.Condition(self.lock)
        self.server = self._make_server()
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
                                        daemon=True, name=f"scripted-provider-{name}")
        self._thread.start()

    # -- configuration --------------------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self.server.server_port

    @property
    def model(self) -> str:
        return f"{self.name}/{MODEL_ID}"

    def models_yml(self) -> str:
        return ("providers:\n"
                f"  {self.name}:\n"
                f"    baseUrl: http://127.0.0.1:{self.port}/v1\n"
                "    api: openai-completions\n    auth: none\n    models:\n"
                f"      - id: {MODEL_ID}\n        name: CW-16 scripted\n        contextWindow: 32768\n"
                "        maxTokens: 1024\n")

    def on(self, predicate: Callable[[Request], bool], responder: Callable[[Request], Turn | str | None] | Turn | str,
           *, role: str | None = None, once: bool = False, name: str = "") -> dict:
        rule = {"predicate": predicate, "responder": responder, "role": role, "once": once, "name": name,
                "hits": 0}
        with self.lock:
            self.rules.append(rule)
        return rule

    def on_text(self, suffix: str, responder: Callable[[Request], Turn | str | None] | Turn | str, *,
                role: str | None = None, once: bool = False) -> dict:
        """A user turn whose last text ends with ``suffix`` (what the test typed into an OMP composer)."""
        return self.on(lambda r: r.injected is None and r.last_role == "user" and r.last_text.endswith(suffix),
                       responder, role=role, once=once, name=f"text:{suffix}")

    def call(self, role: str, name: str, args: dict, intent: str = "scripted cw16 intent") -> tuple[str, dict, str]:
        """A tool call tuple with a unique id (``i`` is the OMP intent field)."""
        with self.lock:
            self._calls += 1
            call_id = f"cw16-{role}-{self._calls}"
        return name, {"i": intent, **args}, call_id

    # -- observation ----------------------------------------------------------------------------------
    def count(self, role: str | None = None) -> int:
        with self.lock:
            return sum(self.requests.values()) if role is None else self.requests[role]

    def wait_count(self, role: str | None, at_least: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._changed:
            while (sum(self.requests.values()) if role is None else self.requests[role]) < at_least:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
        return True

    def snapshot(self) -> dict:
        with self.lock:
            return {"requests": dict(self.requests), "injected": {k: list(v) for k, v in self.injected.items()},
                    "aborted": list(self.aborted), "errors": list(self.errors),
                    "rules": [{"name": r["name"], "hits": r["hits"]} for r in self.rules]}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    # -- request handling -----------------------------------------------------------------------------
    def _parse(self, body: dict) -> Request:
        tool_names = sorted(t.get("function", {}).get("name", "") for t in body.get("tools") or [])
        role = "manager" if "to_worker" in tool_names else "worker" if "to_manager" in tool_names else "unknown"
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        blocks = text_blocks(last.get("content"))
        last_text = blocks[-1].strip() if blocks else ""
        injected = None
        if last.get("role") == "user":
            try:
                value = json.loads(last_text)
            except ValueError:
                value = None
            if isinstance(value, dict) and isinstance(value.get("workbench_message_id"), str):
                injected = value
        results: dict[str, Any] = {}
        for message in reversed(messages):
            if message.get("role") != "tool":
                break
            raw = "".join(text_blocks(message.get("content")))
            try:
                results[str(message.get("tool_call_id"))] = json.loads(raw)
            except ValueError:
                results[str(message.get("tool_call_id"))] = {"non_json": raw[:200]}
        with self.lock:
            self.requests[role] += 1
            index = sum(self.requests.values())
            self.tool_names.setdefault(role, tool_names)
            self.tool_results.update(results)
            if injected is not None:
                payload = injected.get("payload") if isinstance(injected.get("payload"), dict) else {}
                self.injected.setdefault(role, []).append({
                    "at": time.time(), "kind": injected.get("kind"), "message_id": injected["workbench_message_id"],
                    "handoff": payload.get("handoff"), "payload_kind": payload.get("kind"),
                    "stage": payload.get("stage"), "contract": "response_contract" in injected})
            if len(self.log) < self.log_limit:
                self.log.append({"i": index, "at": time.time(), "role": role, "last_role": last.get("role"),
                                 "text_tail": last_text[-120:] if injected is None else None,
                                 "injected_kind": injected.get("kind") if injected else None,
                                 "tool_results": sorted(results)})
            self._changed.notify_all()
        return Request(index, role, body, tool_names, last.get("role"), last_text, injected, results)

    def _answer(self, request: Request) -> Turn:
        with self.lock:
            rules = list(self.rules)
        for rule in rules:
            if rule["role"] not in (None, request.role) or (rule["once"] and rule["hits"]):
                continue
            try:
                if not rule["predicate"](request):
                    continue
                responder = rule["responder"]
                answer = responder(request) if callable(responder) else responder
            except Exception as exc:  # a script bug must never hang an OMP
                with self.lock:
                    self.errors.append(f"{rule['name']}: {type(exc).__name__}: {exc}")
                return Turn(text=f"script-error {type(exc).__name__}")
            if answer is None:
                continue
            with self.lock:
                rule["hits"] += 1
            return Turn(text=answer) if isinstance(answer, str) else answer
        return self.default

    def _make_server(self) -> ThreadingHTTPServer:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (http.server API)
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
                try:
                    request = provider._parse(json.loads(raw or b"{}"))
                    turn = provider._answer(request)
                except Exception as exc:  # malformed request: answer, never hang
                    with provider.lock:
                        provider.errors.append(f"parse: {type(exc).__name__}")
                    request, turn = None, Turn(text="ok")
                if turn.delay:
                    time.sleep(turn.delay)
                try:
                    if turn.status != 200:
                        body = json.dumps({"error": {"message": turn.error_body, "type": "scripted_error",
                                                     "code": turn.status}}).encode()
                        self.send_response(turn.status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    frames = provider._frames(turn)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    if turn.chunk_gap <= 0:
                        body = b"".join(frames)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for i, frame in enumerate(frames):
                        self.wfile.write(frame)
                        self.wfile.flush()
                        if i < len(frames) - 2:
                            time.sleep(turn.chunk_gap)
                    self.close_connection = True
                except (BrokenPipeError, ConnectionResetError):
                    with provider.lock:
                        provider.aborted.append({"at": time.time(), "index": request.index if request else None,
                                                 "role": request.role if request else None})

            do_GET = do_POST

            def log_message(self, *_args):
                pass

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @staticmethod
    def _frames(turn: Turn) -> list[bytes]:
        def chunk(delta: dict, finish: str | None = None, usage: bool = False) -> bytes:
            frame: dict[str, Any] = {"id": "cw16", "object": "chat.completion.chunk", "created": 0,
                                     "model": MODEL_ID, "choices": [{"index": 0, "delta": delta,
                                                                     "finish_reason": finish}]}
            if usage:
                frame["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            return f"data: {json.dumps(frame)}\n\n".encode()

        frames: list[bytes] = []
        if turn.tool_calls:
            calls = []
            for i, item in enumerate(turn.tool_calls):
                name, args = item[0], item[1]
                call_id = item[2] if len(item) > 2 else f"cw16-anon-{i}-{time.monotonic_ns()}"
                calls.append({"index": i, "id": call_id, "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}})
            frames.append(chunk({"role": "assistant", "tool_calls": calls}))
            finish = "tool_calls"
        else:
            content = turn.text if turn.text is not None else "ok"
            parts = max(1, turn.chunks)
            size = max(1, -(-len(content) // parts))
            pieces = [content[i:i + size] for i in range(0, len(content), size)] or [""]
            for n, piece in enumerate(pieces):
                frames.append(chunk({"role": "assistant", "content": piece} if n == 0 else {"content": piece}))
            finish = "stop"
        frames.append(chunk({}, finish, usage=True))
        frames.append(b"data: [DONE]\n\n")
        return frames


# ----------------------------------------------------------------------------------------------- UI driver
def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def screen_classes():
    """tests/ui/support.rep_screen_classes(): pyte plus REP and SU/SD (plain pyte keeps stale cells)."""
    return _load(REPO / "tests/ui/support.py", "cw16_ui_support").rep_screen_classes()


_OSC52 = re.compile(rb"\x1b\]52;([A-Za-z0-9]*);([A-Za-z0-9+/=?]*)(?:\x07|\x1b\\)")


class Ui:
    """``argv`` on an owned PTY (``setsid --ctty``), rendered with pyte; output drained on every call.

    Used for the product UI itself (``python -m workbench attach``) and for outer clients
    (``tmux attach``, ``herdr --session``). ``osc52`` collects every OSC 52 clipboard write that reached
    this (outermost) terminal as ``(selection, decoded bytes)``.
    """

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str | Path, *, rows: int = ROWS,
                 cols: int = COLS, label: str = "ui", raw_limit: int = 1 << 20):
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        try:
            self.process = subprocess.Popen([SETSID, "--ctty", *argv], env=env, cwd=str(cwd), stdin=slave,
                                            stdout=slave, stderr=slave, close_fds=True)
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.label, self.fd, self.rows, self.cols = label, master, rows, cols
        self.pid = self.process.pid
        self.start = ticks(self.pid)
        screen_class, stream_class = screen_classes()
        self.screen = screen_class(cols, rows)
        self.stream = stream_class(self.screen)
        self.raw = bytearray()
        self.raw_limit = raw_limit
        self.total_bytes = 0
        self.osc52: list[tuple[bytes, bytes]] = []
        self._osc_tail = b""

    # -- I/O -------------------------------------------------------------------------------------------
    def _feed(self, data: bytes) -> None:
        self.total_bytes += len(data)
        self.raw.extend(data)
        if len(self.raw) > self.raw_limit:
            del self.raw[: len(self.raw) - self.raw_limit]
        scan = self._osc_tail + data
        last_end = 0
        for match in _OSC52.finditer(scan):
            selection, body = match.group(1), match.group(2)
            try:
                decoded = base64.b64decode(body + b"=" * (-len(body) % 4), validate=True) if body != b"?" else b"?"
            except (binascii.Error, ValueError):
                decoded = b"<invalid>"
            self.osc52.append((selection, decoded))
            last_end = match.end()
        rest = scan[last_end:]
        start = rest.rfind(b"\x1b]52;")
        self._osc_tail = rest[start:][-(4 << 20):] if start >= 0 else rest[-8:]
        try:
            self.stream.feed(data)
        except Exception:  # a pyte quirk must not end the scenario; the raw bytes are kept
            pass

    def pump(self, seconds: float = 0.2) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if self.fd < 0:
                return
            try:
                ready = select.select([self.fd], [], [], max(0.0, min(remaining, 0.05)))[0]
            except (OSError, ValueError):
                return
            if ready:
                try:
                    data = os.read(self.fd, 1 << 16)
                except BlockingIOError:
                    data = b""
                except OSError:  # EIO: the client side closed
                    return
                if data:
                    self._feed(data)
                    continue
            if remaining <= 0:
                return

    def send(self, data: bytes, *, chunk: int = 4096, settle: float = 0.15) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + max(30.0, len(data) / 50_000)
        while view:
            try:
                written = os.write(self.fd, view[:chunk])
                view = view[written:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{self.label}: PTY input not accepted ({len(view)} bytes left)")
            self.pump(0)
        if settle:
            self.pump(settle)

    def keys(self, *parts: bytes, gap: float = 0.2) -> None:
        for part in parts:
            self.send(part, settle=gap)

    def type(self, value: str | bytes, *, gap: float = 0.02) -> None:
        for ch in (value.encode() if isinstance(value, str) else value):
            self.send(bytes([ch]), settle=gap)

    def paste(self, body: bytes, *, chunk: int = 65536, settle: float = 0.3) -> None:
        self.send(PASTE_START + body + PASTE_END, chunk=chunk, settle=settle)

    # -- screen ----------------------------------------------------------------------------------------
    def text(self) -> str:
        return "\n".join(self.screen.display)

    def lines(self) -> list[str]:
        return list(self.screen.display)

    def wait_text(self, pattern: str, timeout: float = 30) -> bool:
        return self.wait(lambda: re.search(pattern, self.text()) is not None, timeout)

    def wait(self, predicate: Callable[[], bool], timeout: float = 30) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.1)
            if predicate():
                return True
        return bool(predicate())

    def excerpt(self, *needles: str, width: int = 160) -> list[str]:
        """Only lines carrying the scenario's own markers (never whole OMP screens)."""
        return [line.strip()[:width] for line in self.lines() if any(n in line for n in needles)]

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.screen.resize(lines=rows, columns=cols)

    # -- lifetime --------------------------------------------------------------------------------------
    def alive(self) -> bool:
        return self.process.poll() is None

    def wait_exit(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            self.pump(0.1)
        self.pump(0.2)
        return self.process.poll()

    def close(self) -> int | None:
        if self.process.poll() is None:
            kill_exact(self.pid, self.start)
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                pass
        if self.fd >= 0:
            self.pump(0)
            os.close(self.fd)
            self.fd = -1
        return self.process.poll()


# ----------------------------------------------------------------------------------------------- sandbox
def make_bindir(directory: Path, *, exclude: Iterable[str] = (), links: dict[str, str] | None = None,
                farm: bool = False) -> Path:
    """A PATH directory. ``farm``: symlink every /usr/bin entry except ``exclude`` (e.g. a host without Bash)."""
    directory.mkdir(parents=True, exist_ok=True)
    skip = set(exclude)
    if farm:
        for entry in os.scandir("/usr/bin"):
            if entry.name not in skip and not (directory / entry.name).exists():
                (directory / entry.name).symlink_to(entry.path)
    for name, target in (links or {}).items():
        path = directory / name
        if path.is_symlink() or path.exists():
            path.unlink()
        path.symlink_to(target)
    return directory


def fake_zsh(bindir: Path, sentinel: Path) -> Path:
    """A ``zsh`` stand-in that only records being executed (zsh is not installed here; C-D72 (1))."""
    script = bindir / "zsh"
    script.write_text(f"#!/usr/bin/dash\necho ran >> {sentinel}\nexit 0\n")
    script.chmod(0o755)
    return script


class Sandbox:
    """One owned /tmp root: fake HOME, git project, Workbench data dir, scripted provider, owned identities.

    ``host_shell``: ``"bash"`` (PATH /usr/bin:/bin + OMP dir), ``"dash"`` (a /usr/bin symlink farm without
    bash/rbash: the product must pick ``sh`` -> dash), ``"none"`` (only ``omp`` on PATH) or ``"custom"``
    (call :meth:`use_path`). Use as a context manager or call :meth:`close` (idempotent).
    """

    def __init__(self, label: str, *, provider: ScriptedProvider | None = None, omp: str | None = None,
                 host_shell: str = "bash", shell_env: str = "/usr/bin/bash", report: "ScenarioReport | None" = None):
        self.label = label
        self.omp = omp or find_omp()
        if not self.omp:
            raise RuntimeError("OMP is not installed")
        self.root = Path(tempfile.mkdtemp(prefix=f"wbc16{re.sub(r'[^a-z0-9]', '', label.lower())[:8]}-",
                                          dir="/tmp"))
        os.chmod(self.root, 0o700)
        self.home = self.root / "h"
        self.data = self.root / "d"
        self.project = self.root / "p"
        self.tmp = self.root / "t"
        for directory in (self.home / ".omp" / "agent", self.project, self.tmp):
            directory.mkdir(parents=True)
        (self.home / ".omp" / "agent" / "agent.db").write_bytes(b"")  # fake EMPTY store: no credential
        self.owns_provider = provider is None
        self.provider = provider or ScriptedProvider()
        self.report = report
        self.known: dict[str, tuple[int, int]] = {}
        self.sessions: set[int] = set()
        self.owned: dict[int, int] = {}
        self.uis: list[Ui] = []
        self.cleanups: list[Callable[[], Any]] = []
        self.closed = False
        self.host_shell = host_shell
        self._init_project()
        agent = omp_root(self.data) / AGENT_DIR_NAME
        agent.mkdir(parents=True, mode=0o700)
        os.chmod(self.data, 0o700)
        os.chmod(omp_root(self.data), 0o700)
        (agent / "models.yml").write_text(self.provider.models_yml())
        omp_dir = str(Path(self.omp).parent)
        if host_shell == "bash":
            path = f"/usr/bin:/bin:{omp_dir}"
        elif host_shell == "dash":
            bindir = make_bindir(self.root / "bin", exclude=("bash", "rbash", "zsh"), farm=True,
                                 links={"omp": self.omp})
            path = str(bindir)
        elif host_shell == "none":
            path = str(make_bindir(self.root / "bin", links={"omp": self.omp}))
        elif host_shell == "custom":
            path = "/nonexistent-cw16"
        else:
            raise ValueError(host_shell)
        self.env = clean_base_env(
            PATH=path, HOME=str(self.home), SHELL=shell_env, TERM="xterm-256color", COLORTERM="truecolor",
            LANG="C.UTF-8", PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.tmp),
            XDG_CONFIG_HOME=str(self.home / ".config"), XDG_DATA_HOME=str(self.home / ".local/share"),
            XDG_STATE_HOME=str(self.home / ".local/state"), XDG_CACHE_HOME=str(self.home / ".cache"),
            XDG_RUNTIME_DIR=str(self.root / "r"),
            **{key: BLOCKED_PROXY for key in PROXY_KEYS}, NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
        (self.root / "r").mkdir(mode=0o700)
        assert_env_isolated(self.env, self.root)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _init_project(self) -> None:
        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(self.project), *args], capture_output=True, text=True,
                                  timeout=15, check=True, env=clean_base_env(HOME=str(self.home))).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "cw16@example.invalid")
        git("config", "user.name", "cw16")
        git("config", "commit.gpgsign", "false")
        (self.project / "README").write_text("cw16\n")
        git("add", "README")
        git("commit", "-qm", "base")
        self.commit = git("rev-parse", "HEAD")
        self.git = git

    def use_path(self, path: str) -> None:
        self.env["PATH"] = path

    # -- entrypoint ------------------------------------------------------------------------------------
    def argv(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "workbench", *args]

    def start_args(self, *extra: str, model: bool = True) -> list[str]:
        args = ["start", "--data-dir", str(self.data), "--omp", self.omp]
        if model:
            args += ["--omp-arg=--model", f"--omp-arg={self.provider.model}"]
        return [*args, *extra]

    def attach_argv(self, *extra: str) -> list[str]:
        return self.argv("attach", "--data-dir", str(self.data), *extra)

    def cli(self, *args: str, timeout: float = 150, record: bool = True) -> subprocess.CompletedProcess:
        started = time.monotonic()
        done = subprocess.run(self.argv(*args), env=self.env, cwd=self.project, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
        if record and self.report is not None:
            self.report.command(["python", "-m", "workbench", *args], done.returncode, done.stdout, done.stderr,
                                time.monotonic() - started)
        return done

    def start(self, *extra: str, timeout: float = 180) -> subprocess.CompletedProcess:
        return self.cli(*self.start_args("--no-attach", *extra), timeout=timeout)

    def status_full(self) -> tuple[int, dict | None]:
        done = self.cli("status", "--data-dir", str(self.data), "--json", timeout=30, record=False)
        try:
            return done.returncode, json.loads(done.stdout)
        except ValueError:
            return done.returncode, None

    def status(self) -> dict | None:
        _, value = self.status_full()
        return value.get("snapshot") if value and value.get("running") else None

    def wait_status(self, predicate: Callable[[dict], bool], timeout: float = 60, what: str = "",
                    pump: Iterable[Ui] = ()) -> dict:
        deadline = time.monotonic() + timeout
        snapshot = None
        uis = list(pump) or self.uis
        while time.monotonic() < deadline:
            snapshot = self.status()
            if snapshot is not None:
                try:
                    if predicate(snapshot):
                        return snapshot
                except (KeyError, TypeError, AttributeError):
                    pass
            for ui in uis:
                ui.pump(0.05)
            time.sleep(0.15)
        raise AssertionError(f"timed out waiting for {what or 'status'}; phase={snapshot and snapshot.get('phase')} "
                             f"provider={self.provider.snapshot()['requests']}")

    def wait_ready(self, timeout: float = 150) -> dict:
        snapshot = self.wait_status(lambda s: s["phase"] == "ready" and (s.get("bridge") or {}).get("manager")
                                    and (s.get("bridge") or {}).get("worker"), timeout, "ready with both OMPs")
        self.remember(snapshot)
        return snapshot

    def shutdown(self, *, yes: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
        args = ["shutdown", "--data-dir", str(self.data), "--json"] + (["--yes"] if yes else [])
        return self.cli(*args, timeout=timeout)

    def confirm_boot(self, *extra: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return self.cli("confirm-boot", "--data-dir", str(self.data), *extra, timeout=timeout)

    def ui(self, *extra: str, rows: int = ROWS, cols: int = COLS, label: str = "product-ui") -> Ui:
        """The product UI (``attach``) directly on an owned PTY (the plain-terminal outer)."""
        ui = Ui(self.attach_argv(*extra), self.env, self.project, rows=rows, cols=cols, label=label)
        self.track_ui(ui)
        return ui

    def track_ui(self, ui: Ui) -> Ui:
        self.uis.append(ui)
        self.own(ui.pid, ui.start)
        return ui

    # -- ownership -------------------------------------------------------------------------------------
    def own(self, pid: int, start: int | None = None) -> None:
        start = start or ticks(pid)
        if start:
            self.owned[pid] = start

    def remember(self, snapshot: dict) -> None:
        backend = snapshot.get("backend") or {}
        process = backend.get("process") or {}
        if process.get("pid"):
            self.known["backend"] = (process["pid"], process["start_ticks"])
        if backend.get("session_id"):
            self.sessions.add(backend["session_id"])
        for name, pane in (snapshot.get("panes") or {}).items():
            proc = pane.get("process") or {}
            if proc.get("pid"):
                self.known[name] = (proc["pid"], proc["start_ticks"])
                self.sessions.add(proc["pid"])  # each pane child leads its own session

    def backend_processes(self) -> list[int]:
        needle = f"_backend\0--data-dir\0{self.data}\0".encode()
        found = []
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    if needle in Path(f"/proc/{name}/cmdline").read_bytes():
                        fields = stat_fields(int(name))
                        if fields and fields[0] not in (b"Z", b"X"):
                            found.append(int(name))
                except OSError:
                    continue
        return found

    def attach_processes(self) -> dict[int, int]:
        """Live product UI clients (``workbench attach``) of this data dir: pid -> start ticks."""
        needle = f"workbench\0attach\0--data-dir\0{self.data}".encode()
        found = {}
        for pid, start in processes_mentioning(str(self.data)).items():
            try:
                if needle in Path(f"/proc/{pid}/cmdline").read_bytes():
                    found[pid] = start
            except OSError:
                continue
        return found

    def residue(self) -> dict:
        found = {
            "identities": {n: p for n, (p, s) in self.known.items() if alive(p, s)},
            "session_members": session_members(self.sessions),
            "backends": self.backend_processes(),
            "root_processes": processes_mentioning(str(self.root)),
            "sockets": [p.name for p in (self.data / "ui.sock", self.data / "bridge.sock") if p.exists()],
        }
        return {k: v for k, v in found.items() if v}

    def wait_no_residue(self, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        left = self.residue()
        while left and time.monotonic() < deadline:
            for ui in self.uis:
                ui.pump(0.05)
            time.sleep(0.2)
            left = self.residue()
        return left

    def close(self) -> dict:
        """Bounded cleanup. Normal path first (registered outer cleanups, confirmed shutdown, wait); exact-identity
        SIGKILL only as a fallback; the residue seen before the fallback is returned (a leak is a finding)."""
        if self.closed:
            return {}
        self.closed = True
        result: dict[str, Any] = {}
        for cleanup in reversed(self.cleanups):
            try:
                cleanup()
            except Exception as exc:  # cleanup continues
                result.setdefault("cleanup_errors", []).append(f"{type(exc).__name__}: {exc}")
        for ui in self.uis:
            try:
                ui.close()
            except OSError:
                pass
        try:
            if self.backend_processes():
                done = self.shutdown(timeout=90)
                result["shutdown_exit"] = done.returncode
        except Exception as exc:
            result["shutdown_error"] = type(exc).__name__
        result["residue_before_fallback"] = self.wait_no_residue(25)
        targets = dict(self.owned)
        for pid, start in self.known.values():
            targets[pid] = start
        targets.update(session_members(self.sessions))
        targets.update(processes_mentioning(str(self.root)))
        for pid, start in targets.items():
            if alive(pid, start):
                kill_exact(pid, start)
        if self.owns_provider:
            self.provider.close()
        for _ in range(10):  # a late write of an exiting OMP helper can recreate a directory
            shutil.rmtree(self.root, ignore_errors=True)
            if not self.root.exists():
                break
            time.sleep(0.5)
        result["root_removed"] = not self.root.exists()
        result["residue_after_fallback"] = {k: v for k, v in {
            "root_processes": processes_mentioning(str(self.root))}.items() if v}
        if self.report is not None:
            self.report.data["cleanup"] = result
        return result


# ----------------------------------------------------------------------------------------------- snapshots
def identity(snapshot: dict) -> dict:
    """What must not change across a UI or outer-client detach/reattach (backend, panes, bridge, shell)."""
    panes = snapshot.get("panes") or {}
    shell = (panes.get("host_shell") or {}).get("shell") or {}
    bridge = snapshot.get("bridge") or {}
    return {
        "backend": (snapshot.get("backend") or {}).get("process"),
        "backend_session": (snapshot.get("backend") or {}).get("session_id"),
        "panes": {name: ((pane.get("process") or {}).get("pid"), (pane.get("process") or {}).get("start_ticks"),
                         pane.get("session_id"), pane.get("generation")) for name, pane in panes.items()},
        "bridge": {role: {k: (bridge.get(role) or {}).get(k) for k in ("session_id", "generation", "pid")}
                   for role in ("manager", "worker")},
        "shell_parent": shell.get("parent"), "shell_generation": shell.get("generation"),
        "supervisor": shell.get("supervisor"),
    }


def owner_state(snapshot: dict) -> dict:
    shell = snapshot["panes"]["host_shell"]["shell"]
    return {k: shell.get(k) for k in ("input_owner", "owner_epoch", "parent_mode", "takeover_requested",
                                      "takeover_confirmed")}


def pane_sizes_for(rows: int, cols: int) -> dict[str, tuple[int, int]]:
    from workbench.ui.product.model import pane_inner_sizes
    return {pane.value: tuple(size) for pane, size in pane_inner_sizes(rows, cols).items()}


def wait_file(path: Path, *, contains: str | None = None, endswith: str = "\n", timeout: float = 15,
              pump: Iterable[Ui] = ()) -> str | None:
    deadline = time.monotonic() + timeout
    uis = list(pump)
    while time.monotonic() < deadline:
        try:
            value = path.read_text()
        except (OSError, UnicodeDecodeError):
            value = None
        if value is not None and (contains is None or contains in value) and value.endswith(endswith):
            return value
        for ui in uis:
            ui.pump(0.05)
        if not uis:
            time.sleep(0.05)
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def shutdown_result(stdout: str) -> dict:
    """``shutdown --json`` prints a human line first, then ``{"shutdown": {...}}``; returns the inner dict."""
    start = stdout.find("\n{") + 1 if not stdout.lstrip().startswith("{") else stdout.find("{")
    try:
        payload = json.loads(stdout[start:]) if start >= 0 else {}
    except ValueError:
        return {}
    return payload.get("shutdown", payload) if isinstance(payload, dict) else {}


# ----------------------------------------------------------------------------------------------- reports
class NotApplicable(Exception):
    """A step that does not apply to this combination (recorded as ``n/a`` with the reason, never as pass)."""


class ScenarioReport:
    """Per-scenario JSON evidence: commands, exits, identities, versions, step results, unknowns."""

    def __init__(self, scenario: str, *, run_id: str | None = None, **context: Any):
        self.run_id = run_id or os.environ.get("WB_CW16_RUN_ID") or time.strftime("%Y%m%dT%H%M%S")
        self.data: dict[str, Any] = {"scenario": scenario, "run_id": self.run_id, "started_at": iso_now(),
                                     "repo_head": _first_line(["git", "-C", str(REPO), "rev-parse", "HEAD"]),
                                     "context": context, "versions": {}, "commands": [], "steps": {},
                                     "unknowns": [], "result": None}

    def command(self, argv: list[str], exit_code: int, stdout: str, stderr: str, seconds: float) -> None:
        self.data["commands"].append({"argv": argv, "exit": exit_code, "seconds": round(seconds, 2),
                                      "stdout_tail": (stdout or "")[-600:], "stderr_tail": (stderr or "")[-400:]})

    def step(self, name: str, status: str, **observed: Any) -> None:
        self.data["steps"][name] = {"status": status, **observed}

    def unknown(self, what: str) -> None:
        self.data["unknowns"].append(what)

    def path(self) -> Path:
        directory = REPORT_DIR / self.run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{self.data['scenario']}.json"

    def write(self) -> Path:
        self.data["finished_at"] = iso_now()
        statuses = [step["status"] for step in self.data["steps"].values()]
        self.data["result"] = (FAIL if FAIL in statuses else NOT_RUN if NOT_RUN in statuses
                               else PASS if statuses else NOT_RUN)
        target = self.path()
        target.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, default=str))
        return target


class StepRunner:
    """Run named steps in order, recording pass / fail / n/a / not_run(reason) into a :class:`ScenarioReport`.

    A step function returns a dict of observations (pass) or raises: ``AssertionError`` -> fail,
    :class:`NotApplicable` -> n/a, any other exception -> fail (error). ``requires`` names earlier steps
    that must have passed (else the step is ``not_run`` with the reason).
    """

    def __init__(self, report: ScenarioReport):
        self.report = report
        self.status: dict[str, str] = {}

    def run(self, name: str, fn: Callable[[], dict | None], *, requires: Iterable[str] = ()) -> str:
        missing = [r for r in requires if self.status.get(r) not in (PASS, NA)]
        if missing:
            self.status[name] = NOT_RUN
            self.report.step(name, NOT_RUN, reason=f"prerequisite not passed: {missing}")
            return NOT_RUN
        started = time.monotonic()
        try:
            observed = fn() or {}
            status = PASS
            detail: dict[str, Any] = {"observed": observed}
        except NotApplicable as exc:
            status, detail = NA, {"reason": str(exc)}
        except AssertionError as exc:
            status, detail = FAIL, {"assertion": str(exc)[:4000]}
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed silently
            status, detail = FAIL, {"error": f"{type(exc).__name__}: {exc}"[:2000],
                                    "trace": traceback.format_exc()[-3000:]}
        self.status[name] = status
        self.report.step(name, status, seconds=round(time.monotonic() - started, 1), **detail)
        return status

    def failures(self) -> dict[str, str]:
        return {name: status for name, status in self.status.items() if status not in (PASS, NA)}


__all__ = [
    "REPO", "SRC", "PREFIX", "FOCUS_KEYS", "TITLES", "PASTE_START", "PASTE_END", "ROWS", "COLS", "BLOCKED_PROXY",
    "PROXY_KEYS", "REPORT_DIR", "PASS", "FAIL", "NA", "NOT_RUN",
    "find_omp", "find_tool", "pyte_available", "tool_versions", "clean_base_env", "assert_env_isolated",
    "ticks", "alive", "kill_exact", "processes_mentioning", "session_members", "pty_size_of", "exe_of",
    "owned_environ", "iso_now",
    "Turn", "text", "tools", "error", "Request", "ScriptedProvider",
    "Ui", "screen_classes", "make_bindir", "fake_zsh", "Sandbox",
    "identity", "owner_state", "pane_sizes_for", "wait_file", "shutdown_result",
    "NotApplicable", "ScenarioReport", "StepRunner",
]
