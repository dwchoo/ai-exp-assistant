"""LIVE CW-18 end-to-end through the real product entrypoint, ZERO model calls (p27-cw18-test-01). Opt-in: WB_LIVE_CW18=1.

Run (repo root)::

    WB_LIVE_CW18=1 PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest tests/backend/live_cw18_independent_p27w.py -v

Setup (all under one /tmp root, removed afterwards): a FAKE HOME (never the real one) with an empty fake
``~/.omp/agent/agent.db`` (no credential), a git project, and the Workbench OMP home of the data dir
(``<data>/omp-root/agent``) holding a ``models.yml`` whose only provider is a scripted OpenAI-compatible
server on 127.0.0.1 (no model). HTTP(S)/ALL_PROXY point at a closed port (outbound blocked). ``omp`` is the
user's installed binary, always run with the fake HOME.

Flow (expectations from C-D64/C-D65/C-D66, CW-18 ticket and the unit assignments):
  1. ``python -m workbench start`` -> ready, both OMPs connected, isolation not failed;
     the product UI attached in a PTY (``python -m workbench attach``).
  2. the user types into the manager pane; the scripted manager calls ``to_worker`` (work Task) ->
     ``dispatched``; a second ``to_worker`` while the worker is on it -> ``worker_busy`` (nothing queued);
     the product UI shows the worker busy and the Task; the scripted worker writes a file inside the
     allowed path with its own write tool and reports ``to_manager`` done -> the manager receives the
     report; the Task closes (done) and the worker is idle.
  3. experiment Task: ``to_worker`` with an execution -> the run starts in the product host shell at once
     (idle shell), worker execute/analysis staged replies, report to the manager; the shell is given back.
  4. pause/resume once from the product UI (prefix p, p): ``to_worker`` while paused -> ``held:paused`` and
     no Task; resume (reconciled) -> not paused, nothing replayed.
  5. confirmed shutdown; no owned process, socket or temp file remains.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pty  # noqa: F401  (documented: the PTY is opened with os.openpty)
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
import unittest
from typing import Any

import pyte

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from workbench.backend.omp_home import AGENT_DIR_NAME, omp_root  # noqa: E402
from workbench.runtime.process_evidence import LinuxProcessProbe  # noqa: E402

LIVE = os.environ.get("WB_LIVE_CW18") == "1"
BLOCKED_PROXY = "http://127.0.0.1:9"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
PREFIX = b"\x1d"
ROWS, COLS = 40, 170
SETSID = shutil.which("setsid", path="/usr/bin:/bin") or "/usr/bin/setsid"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return []


class Script:
    """Both roles' scripted 'model' (one provider: the role is told by the tool list). Records ids/fields only."""

    def __init__(self, project: Path, commit: str):
        self.project, self.commit = project, commit
        self.lock = threading.Lock()
        self.requests = {"manager": 0, "worker": 0, "unknown": 0}
        self.tool_names: dict[str, list[str]] = {}
        self.results: dict[str, dict] = {}  # tool call id -> parsed tool result
        self.injected: dict[str, list[dict]] = {"manager": [], "worker": []}
        self.calls = 0
        self.write_calls: set[str] = set()
        self.busy_seen = threading.Event()
        self.ui_checked = threading.Event()
        self.frame_from_contract = _load(REPO / "tests/workflow/live_workflow_probe.py", "p27w_cw10").\
            _frame_from_contract

    def _call(self, role, name, args):
        self.calls += 1
        call_id = f"p27w-{role}-{self.calls}"
        return [(name, {"i": "scripted p27w intent", **args}, call_id)]

    def respond(self, request: dict) -> list | str | None:
        tools = sorted(t.get("function", {}).get("name", "") for t in request.get("tools") or [])
        role = "manager" if "to_worker" in tools else "worker" if "to_manager" in tools else "unknown"
        with self.lock:
            self.requests[role] += 1
            self.tool_names.setdefault(role, tools)
        messages = request.get("messages") or []
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            return self._after_tool(role, messages)
        blocks = text_blocks(last.get("content"))
        text = blocks[-1].strip() if blocks else ""
        try:
            injected = json.loads(text)
        except ValueError:
            injected = None
        if isinstance(injected, dict) and isinstance(injected.get("workbench_message_id"), str):
            return self._on_injected(role, injected)
        if role == "manager":
            if text.endswith("stage-work"):
                return self._call(role, "to_worker", {"kind": "work", "message": "write the p27w note",
                                                      "spec": {"goal": "p27w note", "paths": ["notes/"]}})
            if text.endswith("stage-exp"):
                return self._call(role, "to_worker", {"kind": "experiment", "message": "run the p27w check",
                                                      "spec": {"goal": "p27w check", "paths": ["outcome.txt"],
                                                               "execution": self.execution()}})
            if text.endswith("stage-paused"):
                return self._call(role, "to_worker", {"kind": "work", "message": "while paused",
                                                      "spec": {"goal": "paused", "paths": ["notes/"]}})
        return None

    def execution(self):
        return {"source": str(self.project), "commit": self.commit,
                "command": "printf 'P27W_HOST_RAN PASS\\n'; printf PASS > outcome.txt",
                "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
                "environment": ["PATH"], "shell": "bash"}

    def _after_tool(self, role, messages):
        latest = {}
        for message in reversed(messages):
            if message.get("role") != "tool":
                break
            body = "".join(text_blocks(message.get("content")))
            try:
                value = json.loads(body)
            except ValueError:
                value = {"non_json": body[:200]}
            latest[str(message.get("tool_call_id"))] = value
        with self.lock:
            self.results.update(latest)
        for call_id, value in latest.items():
            if role == "manager" and value.get("status") == "dispatched" and value.get("kind") == "work":
                # the worker is on it: a second Task must be answered worker_busy
                return self._call(role, "to_worker", {"kind": "work", "message": "second task while busy",
                                                      "spec": {"goal": "other", "paths": ["other/"]}})
            if role == "manager" and value.get("status") == "worker_busy":
                self.busy_seen.set()
            if role == "worker" and call_id in self.write_calls:
                self.ui_checked.wait(60)  # the test looks at the UI while the worker is busy
                return self._call(role, "to_manager", {"kind": "done", "message": "p27w note written"})
        return None

    def _on_injected(self, role, injected):
        payload = injected.get("payload") if isinstance(injected.get("payload"), dict) else {}
        with self.lock:
            self.injected[role].append({"kind": injected.get("kind"), "message_id": injected["workbench_message_id"],
                                        "handoff": payload.get("handoff"), "payload_kind": payload.get("kind"),
                                        "stage": payload.get("stage"), "contract": "response_contract" in injected})
        if role == "worker" and "response_contract" in injected:
            if payload.get("stage") == "execute":
                decision = "execute"
            else:
                facts = payload.get("facts") or {}
                ok = (facts.get("exit_status") == 0 and "PASS" in (facts.get("raw_log_excerpt") or "")
                      and "PASS" in (facts.get("result_excerpt") or ""))
                decision = "success" if ok else "failure"
            return self.frame_from_contract(injected, decision) or "invalid"
        if role == "worker" and payload.get("handoff") == "to_worker" and payload.get("kind") == "work" \
                and injected.get("kind") == "task":
            calls = self._call(role, "write", {"path": "notes/p27w.txt", "content": "written by the p27w worker\n"})
            self.write_calls.add(calls[0][2])
            return calls
        return None


def make_server(script: Script) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                answer = script.respond(json.loads(raw))
            except Exception as exc:  # never hang the OMP on a script bug
                answer = f"script-error {type(exc).__name__}"
            if isinstance(answer, list):
                delta = {"role": "assistant", "tool_calls": [
                    {"index": i, "id": call_id, "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}
                    for i, (name, args, call_id) in enumerate(answer)]}
                finish = "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": answer if isinstance(answer, str) else "ok"}, "stop"
            frames = [{"id": "p", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"id": "p", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                       "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}]
            body = b"".join(f"data: {json.dumps(f)}\n\n".encode() for f in frames) + b"data: [DONE]\n\n"
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST

        def log_message(self, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def ticks(pid):
    try:
        return LinuxProcessProbe.start_ticks(pid)
    except OSError:
        return None


def processes_mentioning(needle: str) -> dict[int, int]:
    found = {}
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            cmdline = Path(f"/proc/{name}/cmdline").read_bytes()
            try:  # OMP's own helper daemons (e.g. text-predict) run with a cwd inside our temp root
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


def kill_exact(pid, start):
    try:
        fd = os.pidfd_open(pid)
    except (ProcessLookupError, OSError):
        return
    try:
        if ticks(pid) == start:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    finally:
        os.close(fd)


class Ui:
    """The product UI on a PTY, rendered with pyte."""

    def __init__(self, argv, env, cwd):
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        try:
            self.process = subprocess.Popen([SETSID, "--ctty", *argv], env=env, cwd=cwd, stdin=slave, stdout=slave,
                                            stderr=slave, close_fds=True)
        finally:
            os.close(slave)
        self.fd = master
        self.start = ticks(self.process.pid)
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)

    def pump(self, seconds=0.2):
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.fd < 0:
                return
            try:
                if select.select([self.fd], [], [], min(remaining, 0.05))[0]:
                    data = os.read(self.fd, 65536)
                    if data:
                        self.stream.feed(data)
            except OSError:
                return

    def text(self):
        return "\n".join(self.screen.display)

    def send(self, data: bytes):
        os.write(self.fd, data)
        self.pump(0.15)

    def wait_text(self, pattern, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.2)
            if re.search(pattern, self.text()):
                return True
        return False

    def close(self):
        if self.process.poll() is None:
            os.write(self.fd, PREFIX + b"q")  # detach
            self.pump(0.5)
            os.write(self.fd, b"q")  # confirm
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                kill_exact(self.process.pid, self.start)
                self.process.wait(5)
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


@unittest.skipUnless(LIVE, "set WB_LIVE_CW18=1 (real OMP, scripted provider, no model)")
class LiveCw18EndToEnd(unittest.TestCase):
    def setUp(self):
        self.omp = shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
        self.assertTrue(os.access(self.omp, os.X_OK), "omp is not installed")
        self.root = Path(tempfile.mkdtemp(prefix="wb-p27w-live-", dir="/tmp"))
        self.addCleanup(self.cleanup)
        self.home = self.root / "home"
        (self.home / ".omp" / "agent").mkdir(parents=True)
        (self.home / ".omp" / "agent" / "agent.db").write_bytes(b"")  # fake empty store, no credential
        self.data = self.home / "wbdata"
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "notes").mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(self.project), *a], capture_output=True,  # noqa: E731
                                        text=True, timeout=15, check=True).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "p27w@example.invalid")
        git("config", "user.name", "p27w")
        (self.project / "README").write_text("p27w\n")
        git("add", "README")
        git("commit", "-qm", "base")
        self.commit = git("rev-parse", "HEAD")
        self.script = Script(self.project, self.commit)
        self.server = make_server(self.script)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        agent = omp_root(self.data) / AGENT_DIR_NAME
        agent.mkdir(parents=True, mode=0o700)
        os.chmod(self.data, 0o700)
        os.chmod(omp_root(self.data), 0o700)
        (agent / "models.yml").write_text(
            "providers:\n  wbp27w:\n"
            f"    baseUrl: http://127.0.0.1:{self.server.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: p27w scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
        self.env = {"PATH": "/usr/bin:/bin:" + str(Path(self.omp).parent), "HOME": str(self.home),
                    "SHELL": "/usr/bin/bash", "LANG": "C.UTF-8", "TERM": "xterm-256color",
                    "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_DATA_HOME": str(self.home / ".local/share"),
                    "XDG_STATE_HOME": str(self.home / ".local/state"), "XDG_CACHE_HOME": str(self.home / ".cache"),
                    **{key: BLOCKED_PROXY for key in PROXY_KEYS}, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        self.ui: Ui | None = None
        self.report: dict[str, Any] = {}

    def cli(self, *args, timeout=150):
        return subprocess.run([sys.executable, "-m", "workbench", *args], env=self.env, cwd=self.project,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def status(self):
        out = self.cli("status", "--data-dir", str(self.data), "--json", timeout=30)
        try:
            value = json.loads(out.stdout)
        except ValueError:
            return None
        return value.get("snapshot") if value.get("running") else None

    def wait_status(self, predicate, timeout=60, what=""):
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
        self.fail(f"timed out: {what}; last snapshot task={snapshot and snapshot.get('task')} "
                  f"worker={snapshot and snapshot.get('worker')} automation="
                  f"{snapshot and (snapshot.get('automation') or {}).get('state')} script={self.script.requests}")

    def cleanup(self):
        if self.ui is not None:
            try:
                self.ui.close()
            except OSError:
                pass
        try:
            if self.status() is not None:
                self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=60)
        except Exception:
            pass
        deadline = time.monotonic() + 20
        while processes_mentioning(str(self.root)) and time.monotonic() < deadline:
            time.sleep(0.2)
        left = processes_mentioning(str(self.root))
        for pid, start in left.items():  # only processes whose command line names our own temp root
            kill_exact(pid, start)
        self.server.shutdown()
        self.server.server_close()
        for _ in range(10):  # a late write of an exiting OMP helper can recreate a directory
            shutil.rmtree(self.root, ignore_errors=True)
            time.sleep(0.5)
            if not self.root.exists():
                break
        self.report["residue_processes"] = sorted(left)
        self.report["residue_root"] = self.root.exists()
        out = os.environ.get("WB_LIVE_CW18_REPORT")
        if out:
            Path(out).write_text(json.dumps(self.report, indent=1, default=str))

    def type_into_manager(self, text):
        self.ui.send(PREFIX + b"1")  # focus the manager pane
        self.ui.pump(0.3)
        for ch in text.encode():
            self.ui.send(bytes([ch]))
        self.ui.send(b"\r")

    def test_end_to_end(self):
        version = subprocess.run([self.omp, "--version"], capture_output=True, text=True, timeout=30,
                                 env={"PATH": self.env["PATH"], "HOME": str(self.home)})
        self.report["omp"] = (version.stdout or version.stderr).strip()
        start = self.cli("start", "--data-dir", str(self.data), "--omp", self.omp,
                         "--omp-arg=--model", "--omp-arg=wbp27w/scripted", "--no-attach")
        self.report["start"] = {"exit": start.returncode, "stdout": start.stdout[-1200:], "stderr": start.stderr[-800:]}
        self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
        ready = self.wait_status(lambda s: s["phase"] == "ready" and (s.get("bridge") or {}).get("manager")
                                 and (s.get("bridge") or {}).get("worker"), 120, "both OMPs connected")
        self.report["isolation"] = (ready.get("omp_isolation") or {}).get("state")
        self.assertNotIn(self.report["isolation"], ("failed",), ready.get("omp_isolation"))
        self.assertEqual(ready["worker"], {"state": "idle", "task_id": None})
        self.ui = Ui([sys.executable, "-m", "workbench", "attach", "--data-dir", str(self.data)], self.env,
                     str(self.project))
        self.assertTrue(self.ui.wait_text(r"worker: 대기", 30), self.ui.text())

        # -- 2. free work -------------------------------------------------------------------------------
        self.type_into_manager("stage-work")
        busy_view = self.wait_status(lambda s: (s.get("task") or {}).get("kind") == "work"
                                     and s["worker"]["state"] == "busy", 60, "work Task dispatched")
        self.assertTrue(self.script.busy_seen.wait(60), f"no worker_busy: {self.script.results}")
        statuses = [v.get("status") for v in self.script.results.values()]
        self.assertIn("dispatched", statuses)
        self.assertIn("worker_busy", statuses)
        busy = next(v for v in self.script.results.values() if v.get("status") == "worker_busy")
        self.assertEqual(busy["task"]["task_id"], busy_view["task"]["task_id"])
        self.assertTrue(self.ui.wait_text(r"worker: 작업 중", 20), self.ui.text())
        self.assertTrue(self.ui.wait_text(r"작업: 작업", 10), self.ui.text())
        self.report["ui_busy_lines"] = [line for line in self.ui.text().splitlines() if "worker" in line or "작업:" in line]
        self.script.ui_checked.set()
        done = self.wait_status(lambda s: (s.get("task") or {}).get("status") == "closed", 90, "work Task done")
        self.assertEqual(done["task"]["closed_reason"], "done", done["task"])
        self.assertEqual(done["worker"]["state"], "idle")
        self.assertEqual((self.project / "notes" / "p27w.txt").read_text(), "written by the p27w worker\n")
        self.assertTrue(any(i["handoff"] == "to_manager" and i["payload_kind"] == "done"
                            for i in self.script.injected["manager"]), self.script.injected["manager"])
        self.assertEqual(sum(1 for i in self.script.injected["worker"] if i["kind"] == "task"), 1,
                         "worker_busy queued nothing")
        self.assertFalse((self.project / "other").exists())

        # -- 3. experiment on the host shell -------------------------------------------------------------
        self.type_into_manager("stage-exp")
        finished = self.wait_status(lambda s: (s.get("task") or {}).get("kind") == "experiment"
                                    and s["task"]["status"] in ("finished", "closed", "held"), 150, "experiment")
        self.report["experiment"] = finished["task"]
        self.assertEqual(finished["task"]["status"], "finished", finished["task"])
        result = finished["task"]["last_result"]
        self.assertEqual((result.get("judgment"), result.get("run_closed")), ("success", True), result)
        self.assertTrue(any(i.get("stage") == "execute" for i in self.script.injected["worker"]))
        shell = finished["panes"]["host_shell"]
        given_back = self.wait_status(lambda s: s["panes"]["host_shell"]["shell"]["input_owner"] == "user"
                                      and s["panes"]["host_shell"]["shell"]["parent_mode"] == "manual_prompt",
                                      30, "shell given back")
        self.assertIsNone(given_back["panes"]["host_shell"].get("automation_hold"), shell)
        self.assertTrue(self.ui.wait_text(r"P27W_HOST_RAN PASS", 10), "the host pane shows the run")

        # -- 4. pause / resume once ----------------------------------------------------------------------------
        self.ui.send(PREFIX + b"p")
        self.assertTrue(self.ui.wait_text(r"자동화를 일시정지합니다", 10), self.ui.text())
        self.ui.send(b"p")
        paused = self.wait_status(lambda s: (s.get("automation") or {}).get("state") == "paused", 30, "paused")
        self.assertTrue(paused["automation"]["paused"])
        self.assertTrue(self.ui.wait_text(r"일시정지됨", 10), self.ui.text())
        tasks_before = paused.get("task")
        self.type_into_manager("stage-paused")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(v.get("reason") == "paused" for v in self.script.results.values()):
            self.ui.pump(0.3)
        self.assertTrue(any(v == {"status": "held", "reason": "paused"} for v in self.script.results.values()),
                        self.script.results)
        self.ui.send(PREFIX + b"p")
        self.assertTrue(self.ui.wait_text(r"대조 후 재개합니다", 10), self.ui.text())
        self.ui.send(b"p")
        resumed = self.wait_status(lambda s: (s.get("automation") or {}).get("paused") is False, 60, "resumed")
        self.report["resume"] = resumed["automation"].get("resume")
        time.sleep(2)
        after = self.status()
        self.assertEqual(after.get("task"), tasks_before, "nothing held during the pause was replayed")
        self.assertEqual(after["worker"]["state"], "idle")
        self.report["provider_requests"] = dict(self.script.requests)
        self.report["tools_seen"] = self.script.tool_names

        # -- 5. shutdown -----------------------------------------------------------------------------------------
        self.ui.close()
        self.ui = None
        down = self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=90)
        self.assertEqual(down.returncode, 0, down.stdout + down.stderr)
        deadline = time.monotonic() + 30
        while processes_mentioning(str(self.root)) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(processes_mentioning(str(self.root)), {}, "owned processes left after shutdown")

    def test_experiment_waits_while_the_user_has_a_background_job(self):
        """C-D65 (2): Workbench types a run's start only when the user's host shell has NO job."""
        start = self.cli("start", "--data-dir", str(self.data), "--omp", self.omp,
                         "--omp-arg=--model", "--omp-arg=wbp27w/scripted", "--no-attach")
        self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
        self.wait_status(lambda s: s["phase"] == "ready" and (s.get("bridge") or {}).get("manager")
                         and (s.get("bridge") or {}).get("worker"), 120, "both OMPs connected")
        self.ui = Ui([sys.executable, "-m", "workbench", "attach", "--data-dir", str(self.data)], self.env,
                     str(self.project))
        self.assertTrue(self.ui.wait_text(r"worker: 대기", 30), self.ui.text())
        pid_file = self.root / "user-job.pid"
        self.ui.send(PREFIX + b"3")  # the host terminal
        for ch in f"cd /tmp; sleep 300 & echo $! > {pid_file}".encode():
            self.ui.send(bytes([ch]))
        self.ui.send(b"\r")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (pid_file.exists() and pid_file.read_text().strip().isdigit()):
            self.ui.pump(0.2)
        job = int(pid_file.read_text())
        job_ticks = ticks(job)
        self.addCleanup(kill_exact, job, job_ticks)
        self.ui.pump(1.0)
        self.type_into_manager("stage-exp")
        dispatched = self.wait_status(lambda s: (s.get("task") or {}).get("kind") == "experiment", 60, "dispatched")
        time.sleep(4)
        snap = self.status()
        task, shell = snap["task"], snap["panes"]["host_shell"]["shell"]
        self.report["bg_job"] = {"task": task, "shell_mode": shell["parent_mode"], "held": shell["held_reasons"],
                                 "owner": shell["input_owner"]}
        self.assertTrue(Path(f"/proc/{job}").exists(), "the user's job must keep running")
        self.assertEqual((shell["input_owner"], shell["parent_mode"]), ("user", "manual_prompt"),
                         f"the user's shell was taken: {self.report['bg_job']}")
        self.assertEqual((task["status"], task["held_reason"], task["runs_started"]),
                         ("dispatched", "host_terminal_busy", 0), task)
        self.assertNotRegex(self.ui.text(), r"worktrees/", "Workbench typed into the user's shell")


if __name__ == "__main__":
    unittest.main()
