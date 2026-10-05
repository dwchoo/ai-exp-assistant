"""C-D68 independent live check (p27-cd68-test-01): what the real OMP sends to a provider, per role.

Real installed OMP (``omp`` on PATH, expected 18.6.1) started exactly as the backend builds it
(``omp_home.prepare_omp_home`` -> ``launcher.omp_environment`` -> ``launcher.role_overlay`` ->
``launcher.write_role_overlay`` -> ``launcher.omp_command``) in an isolated HOME under /tmp, against a
LOCAL fake OpenAI-responses provider (``models.yml`` in the throw-away Workbench home points
``openai-codex`` at 127.0.0.1 with a dummy key). No real provider, no credential, no user config: the
throw-away HOME has no ``~/.omp/agent/agent.db``, so the Workbench home has no auth link at all.

The fake provider answers the first main-session request with one scripted ``task`` tool call and
every other request with HTTP 500; every request body is captured (model, reasoning effort,
service_tier, tool names, the task tool's agent description).

Expectations come from DECISIONS.md C-D68, not from the implementation:
- (1) worker main request: no ``bash``/``eval`` (and no other command tool), keeps
  read/grep/glob/edit/write/web_search/todo, has the Workbench ``terminal`` and ``to_manager``;
- (2) worker subagents: only the Workbench ``explorer`` (smol) / ``analyst`` (slow) and they have NO
  command execution tool (``bash``, ``eval``, ``terminal``); OMP's bundled agents are not usable;
- (3) manager: OMP's tools unchanged (bash/eval/task present) and OMP's bundled subagents, no
  ``terminal``; the Workbench agents are not offered to it;
- (4) model / thinking / fast (``priority`` service tier) per role, subagents follow their role;
- (5) code mode off: tools are sent as direct function tools (no ``eval`` wrapper on the worker).

Run: PYTHONPATH=src:. python -m unittest tests/backend/live_omp_tools_independent_p27cd68.py
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from workbench.backend import launcher, omp_home

OMP = shutil.which("omp")
ROUTING_HINT = "Compress into one routing hint"
EXEC_TOOLS = {"bash", "eval", "terminal", "python", "shell", "exec", "browser"}


def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _tool_call(name, args):
    arguments = json.dumps(args)
    item = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": name, "arguments": arguments,
            "status": "completed"}
    return [{"type": "response.created", "response": {"id": "resp_1", "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": "",
                                                                                "status": "in_progress"}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0,
             "delta": arguments},
            {"type": "response.function_call_arguments.done", "item_id": "fc_1", "output_index": 0,
             "arguments": arguments},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "output": [item],
                                                         "usage": {"input_tokens": 10, "output_tokens": 5,
                                                                   "total_tokens": 15}}}]


def _tool_name(tool):
    return (tool.get("function") or tool).get("name") or tool.get("type")


class FakeProvider:
    """127.0.0.1 only; every request body captured. ``script``: ordered ``(when, tool, args)`` steps, each answered
    once with one function call: ``when`` is "main" (a main-session request: has ``task``) or "sub" (a subagent
    request: has OMP's ``yield``). Everything else gets HTTP 500."""

    def __init__(self, task_args=None, script=None):
        self.script = list(script or ([("main", "task", task_args)] if task_args is not None else []))
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                answer = None
                with provider.lock:
                    provider.requests.append(body)
                    names = {_tool_name(t) for t in body.get("tools") or []}
                    text = json.dumps(body.get("input"))
                    kind = "sub" if "yield" in names else "main" if "task" in names else None
                    if provider.script and ROUTING_HINT not in text and provider.script[0][0] == kind:
                        answer = provider.script.pop(0)
                if answer is not None:
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(_sse(_tool_call(answer[1], answer[2])))
                    return
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"error":"fake provider"}')

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def real(self):
        with self.lock:
            return [r for r in self.requests if ROUTING_HINT not in json.dumps(r.get("input"))]


class FakeBridge:
    """A Unix socket where the bridge extension connects (no auth answer needed): records every frame and answers a
    ``tool_request`` with a canned ``tool_result``, so a command request that reaches the backend is visible."""

    def __init__(self, path: Path):
        self.path = path
        self.frames: list[dict] = []
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buffer = b""
        with conn:
            while not self.closed:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue
                    with self.lock:
                        self.frames.append(frame)
                    if frame.get("kind") == "tool_request":
                        result = {"kind": "tool_result", "requestId": frame.get("requestId"),
                                  "toolCallId": frame.get("toolCallId"),
                                  "result": {"status": "exited", "exit_code": 0, "output_tail": "FAKE-BACKEND"}}
                        try:
                            conn.sendall(json.dumps(result).encode() + b"\n")
                        except OSError:
                            return

    def requests(self, tool):
        with self.lock:
            return [f for f in self.frames if f.get("kind") == "tool_request" and f.get("tool") == tool]

    def hellos(self):
        with self.lock:
            return [f for f in self.frames if f.get("kind") == "hello"]

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def summary(body):
    tools = body.get("tools") or []
    task = next((t for t in tools if _tool_name(t) == "task"), None)
    return {"model": body.get("model"), "effort": (body.get("reasoning") or {}).get("effort"),
            "tier": body.get("service_tier"), "tools": sorted(_tool_name(t) for t in tools),
            "tool_types": {_tool_name(t): t.get("type") for t in tools},
            "task_tool": json.dumps(task) if task else None, "input": json.dumps(body.get("input"))}


@unittest.skipUnless(OMP, "the omp binary is required")
class LiveRoleRequests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.version = launcher.omp_version(OMP)
        cls.base = Path(tempfile.mkdtemp(prefix="wb-cd68-test-tools-", dir="/tmp"))
        cls.started: list[int] = []

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def run_role(self, role, task_args, *, label, wait_requests=2, budget=45.0, script=None, bridge=False):
        root = self.base / label
        home = root / "home"
        project = root / "project"
        home.mkdir(parents=True)
        project.mkdir()
        provider = FakeProvider(task_args, script)
        self.addCleanup(provider.close)
        self.bridge = None
        if bridge:
            self.bridge = FakeBridge(root / "bridge.sock")
            self.addCleanup(self.bridge.close)
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "dumb",
               "LANG": "C.UTF-8"}
        wb_home = omp_home.prepare_omp_home(home / "data", env, skills_dir=launcher.default_skills_dir(),
                                            provider_ids=launcher.ISOLATION_PROVIDER_IDS)
        (wb_home.agent_dir / "models.yml").write_text(
            "providers:\n  openai-codex:\n"
            f"    baseUrl: http://127.0.0.1:{provider.port}/v1\n"
            "    api: openai-responses\n    apiKey: dummy-not-a-secret\n    models:\n"
            "      - id: gpt-6-luna\n        name: Luna\n        reasoning: true\n"
            "      - id: gpt-6.1-sol\n        name: Sol\n        reasoning: true\n")
        plan = launcher.LaunchPlan(None, OMP, self.version, str(launcher.default_bridge_extension()))
        omp_env = launcher.omp_environment(env, plan, role=role, token="t",
                                           bridge_socket=root / ("bridge.sock" if bridge else "absent.sock"),
                                           home=wb_home.environment())
        overlay = launcher.write_role_overlay(root, role, launcher.role_overlay(
            role, project_dir=project, home=home, environment=omp_env))
        command = launcher.omp_command(plan, overlay)
        proc = subprocess.Popen(command + ["--mode", "rpc", "--no-session"], cwd=project, env=omp_env,
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                start_new_session=True)

        def stop():
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # our own process group only
            except OSError:
                pass
            proc.wait(10)

        self.addCleanup(stop)
        proc.stdin.write(b'{"id":"p","type":"prompt","message":"please look around"}\n')
        proc.stdin.flush()
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline and len(provider.real()) < wait_requests:
            time.sleep(0.2)
        time.sleep(3)  # let a subagent request (if any) arrive
        requests = [summary(r) for r in provider.real()]
        stop()
        self.assertTrue(requests, f"{label}: no provider request; stderr={proc.stderr.read()[:800]!r}")
        return command, requests

    # -- worker ---------------------------------------------------------------------------
    def test_worker_main_request_has_terminal_and_no_builtin_command_tools(self):
        command, requests = self.run_role("worker", None, label="worker-main", wait_requests=1)
        main = requests[0]
        tools = set(main["tools"])
        self.assertFalse(tools & {"bash", "eval", "python", "browser"}, f"C-D68 (1): {main['tools']}")
        self.assertIn("terminal", tools, f"C-D68 (1): the worker's only command path must reach the model: "
                                         f"{main['tools']}")
        self.assertIn("to_manager", tools)
        self.assertNotIn("to_worker", tools)
        for kept in ("read", "grep", "glob", "edit", "write", "web_search", "todo"):
            self.assertIn(kept, tools, f"C-D68 (1) keeps {kept}")
        # C-D68 (5): code mode off = every tool is offered directly (function tools; OMP's edit may be a
        # freeform "custom" tool), none routed through an eval wrapper.
        self.assertEqual({name: kind for name, kind in main["tool_types"].items() if kind != "function"
                          and not (name == "edit" and kind == "custom")}, {}, main["tool_types"])
        # C-D68 (4): worker default = gpt-6-luna max + fast
        self.assertEqual((main["model"], main["effort"], main["tier"]), ("gpt-6-luna", "max", "priority"))

    def test_worker_task_tool_offers_only_the_workbench_agents(self):
        _, requests = self.run_role("worker", None, label="worker-agents", wait_requests=1)
        task = requests[0]["task_tool"]
        self.assertIsNotNone(task, "the worker keeps the task tool for its Workbench subagents (C-D68 (2))")
        for name in ("explorer", "analyst"):
            self.assertIn(name, task)
        for bundled in ("scout", "reviewer", "security-reviewer", "sonic"):
            self.assertNotIn(f'"{bundled}"', task, f"C-D68 (1)/(2): bundled agent {bundled} offered to the worker")
            self.assertNotIn(f"- {bundled}", task)

    def subagent(self, requests):
        """The subagent's request: the one whose tool set has OMP's ``yield`` (main sessions never do)."""
        found = [r for r in requests if "yield" in r["tools"]]
        return found[0] if found else None

    def test_worker_explorer_subagent_has_no_command_tool_and_its_role_model(self):
        _, requests = self.run_role("worker", self.task_args("explorer"), label="worker-explorer")
        sub = self.subagent(requests)
        self.assertIsNotNone(sub, f"no explorer request: {[r['tools'] for r in requests]}")
        # smol = gpt-6-luna xhigh + fast
        self.assertEqual((sub["model"], sub["effort"], sub["tier"]), ("gpt-6-luna", "xhigh", "priority"))
        self.assertFalse(set(sub["tools"]) & {"edit", "write"}, "explorer is read-only")
        self.assertFalse(set(sub["tools"]) & (EXEC_TOOLS - {"terminal"}),
                         f"C-D68 (2): a worker subagent has no command execution tool: {sub['tools']}")

    def test_worker_analyst_subagent_has_no_command_tool_and_its_role_model(self):
        _, requests = self.run_role("worker", self.task_args("analyst"), label="worker-analyst")
        sub = self.subagent(requests)
        self.assertIsNotNone(sub, f"no analyst request: {[r['tools'] for r in requests]}")
        # slow = gpt-6.1-sol high + fast
        self.assertEqual((sub["model"], sub["effort"], sub["tier"]), ("gpt-6.1-sol", "high", "priority"))
        self.assertFalse(set(sub["tools"]) & {"edit", "write"}, "analyst is read-only")
        self.assertFalse(set(sub["tools"]) & (EXEC_TOOLS - {"terminal"}),
                         f"C-D68 (2): a worker subagent has no command execution tool: {sub['tools']}")

    def test_worker_subagent_is_not_offered_terminal(self):
        # C-D68 (2) strictly: the subagent's model should not even see ``terminal`` (it refuses when called, see
        # test_a_worker_subagent_cannot_run_a_command_through_terminal; offering it invites useless calls).
        _, requests = self.run_role("worker", self.task_args("explorer"), label="worker-explorer-list")
        sub = self.subagent(requests)
        self.assertIsNotNone(sub)
        self.assertNotIn("terminal", sub["tools"], f"offered to the explorer subagent: {sub['tools']}")

    def test_worker_cannot_start_a_bundled_agent(self):
        for agent in ("scout", "task"):
            with self.subTest(agent=agent):
                _, requests = self.run_role("worker", self.task_args(agent), label=f"worker-bundled-{agent}",
                                            wait_requests=3, budget=25.0)
                self.assertIsNone(self.subagent(requests),
                                  f"C-D68 (1): bundled agent {agent} ran for the worker: "
                                  f"{[(r['model'], r['tools']) for r in requests]}")

    def test_worker_terminal_call_reaches_the_backend_positive_control(self):
        # Positive control for the next test: the worker's own terminal call does reach the bridge socket.
        script = [("main", "terminal", {"command": "echo MAIN_TERMINAL", "timeout_seconds": 1})]
        self.run_role("worker", None, label="worker-main-terminal", wait_requests=2, script=script, bridge=True)
        self.assertTrue(self.bridge.hellos(), "the worker bridge extension did not connect")
        self.assertEqual([f["args"]["command"] for f in self.bridge.requests("terminal")], ["echo MAIN_TERMINAL"])

    def test_a_worker_subagent_cannot_run_a_command_through_terminal(self):
        # C-D68 (2): even if a subagent's model calls ``terminal``, no command request may reach the backend.
        script = [("main", "task", self.task_args("explorer")),
                  ("sub", "terminal", {"command": "echo SUBAGENT_TERMINAL", "timeout_seconds": 1})]
        _, requests = self.run_role("worker", None, label="worker-sub-terminal", wait_requests=3, script=script,
                                    bridge=True)
        self.assertIsNotNone(self.subagent(requests), "control invalid: the explorer subagent never ran")
        ran = [f["args"].get("command") for f in self.bridge.requests("terminal")]
        self.assertEqual(ran, [], f"C-D68 (2): a worker subagent's terminal call reached the backend: {ran}; "
                                  f"hellos={[(h.get('role'), h.get('ompSessionId')) for h in self.bridge.hellos()]}")

    # -- manager ----------------------------------------------------------------------------
    def test_manager_main_request_keeps_omp_tools_and_its_model(self):
        _, requests = self.run_role("manager", None, label="manager-main", wait_requests=1)
        main = requests[0]
        tools = set(main["tools"])
        for kept in ("bash", "eval", "task", "read", "edit", "write"):
            self.assertIn(kept, tools, f"C-D68 (3): the manager keeps OMP's {kept}")
        self.assertIn("to_worker", tools)
        self.assertNotIn("terminal", tools, "terminal is the worker's tool")
        self.assertNotIn("to_manager", tools)
        # manager default = gpt-6.1-sol high, not fast
        self.assertEqual(main["model"], "gpt-6.1-sol")
        self.assertEqual(main["effort"], "high")
        self.assertIn(main["tier"], (None, "auto", "default"), "manager default is not fast")
        task = main["task_tool"] or ""
        for bundled in ("scout", "reviewer"):
            self.assertIn(bundled, task, f"C-D68 (3): the manager keeps OMP's bundled agent {bundled}")
        self.assertNotIn("explorer", task, "the Workbench worker agents are not the manager's")
        self.assertNotIn("analyst", task)

    def test_manager_subagents_follow_their_role(self):
        cases = {"scout": ("gpt-6-luna", "max", "priority"),        # smol = luna max + fast
                 "reviewer": ("gpt-6.1-sol", "high", None)}          # slow = sol high (no fast)
        for agent, expected in cases.items():
            with self.subTest(agent=agent):
                _, requests = self.run_role("manager", self.task_args(agent), label=f"manager-{agent}")
                sub = self.subagent(requests)
                self.assertIsNotNone(sub, f"no {agent} request: {[r['tools'] for r in requests]}")
                tier = sub["tier"] if sub["tier"] not in ("auto", "default") else None
                self.assertEqual((sub["model"], sub["effort"], tier), expected, sub["tools"])

    def test_manager_cannot_start_a_workbench_worker_agent(self):
        _, requests = self.run_role("manager", self.task_args("explorer"), label="manager-explorer",
                                    wait_requests=3, budget=25.0)
        sub = self.subagent(requests)
        self.assertIsNone(sub, f"the manager ran the worker's explorer: {sub}")

    @staticmethod
    def task_args(agent, tools=None):
        # OMP 18.6.1 task schema: {i, context, tasks: [{task, solutionSpace, name, agent, model?, tools?}]}
        item = {"task": "List the files in the project.", "solutionSpace": "open", "name": "Probe"}
        if agent is not None:
            item["agent"] = agent
        if tools is not None:
            item["tools"] = tools
        return {"i": "probe", "context": "probe", "tasks": [item]}

    def test_worker_cannot_hand_a_subagent_command_tools(self):
        # Adversarial: the task schema has a per-task ``tools`` list; asking for bash/eval/terminal must not give a
        # worker subagent a command execution tool (C-D68 (2)).
        _, requests = self.run_role("worker", self.task_args("explorer", ["bash", "eval", "terminal"]),
                                    label="worker-explorer-tools", wait_requests=3, budget=30.0)
        sub = self.subagent(requests)
        if sub is not None:
            self.assertFalse(set(sub["tools"]) & {"bash", "eval"}, f"C-D68 (2): {sub['tools']}")

    def test_worker_task_without_agent_runs_no_bundled_default(self):
        # The default agent is OMP's bundled ``task``: disabled for the worker (C-D68 (1)/(2)).
        _, requests = self.run_role("worker", self.task_args(None), label="worker-noagent", wait_requests=3,
                                    budget=25.0)
        sub = self.subagent(requests)
        self.assertTrue(sub is None or not set(sub["tools"]) & EXEC_TOOLS,
                        f"the default task agent ran with {sub and sub['tools']}")

if __name__ == "__main__":
    unittest.main()
