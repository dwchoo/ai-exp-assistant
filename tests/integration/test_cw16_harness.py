"""Non-live checks of the CW-16 harness pieces later batches rely on (no OMP, no PTY, no model)."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cw16_harness as h  # noqa: E402


def post(provider: h.ScriptedProvider, body: dict, *, read: bool = True, timeout: float = 10):
    conn = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=timeout)
    conn.request("POST", "/v1/chat/completions", json.dumps(body), {"Content-Type": "application/json"})
    response = conn.getresponse()
    data = response.read() if read else b""
    return conn, response, data


def manager_request(text: str) -> dict:
    return {"tools": [{"function": {"name": "to_worker"}}], "messages": [{"role": "user", "content": text}]}


def sse_text(data: bytes) -> str:
    out = []
    for line in data.decode().splitlines():
        if line.startswith("data: {"):
            delta = json.loads(line[6:])["choices"][0]["delta"]
            out.append(delta.get("content") or "")
    return "".join(out)


class ScriptedProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = h.ScriptedProvider()
        self.addCleanup(self.provider.close)

    def test_rules_roles_counts_and_default(self):
        self.provider.on_text("hello", h.text("scripted hi"), role="manager", once=True)
        _, response, data = post(self.provider, manager_request("say hello"))
        self.assertEqual(response.status, 200)
        self.assertEqual(sse_text(data), "scripted hi")
        _, _, data = post(self.provider, manager_request("say hello"))  # once: falls to the default
        self.assertEqual(sse_text(data), "ok")
        _, _, data = post(self.provider, {"tools": [{"function": {"name": "to_manager"}}],
                                          "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(self.provider.snapshot()["requests"], {"manager": 2, "worker": 1})
        self.assertTrue(self.provider.wait_count("manager", 2, 1))
        self.assertFalse(self.provider.wait_count("manager", 3, 0.2))

    def test_tool_calls_and_tool_results_and_injected_messages(self):
        call = self.provider.call("manager", "to_worker", {"kind": "work"})
        self.provider.on_text("go", h.tools(call), role="manager")
        _, _, data = post(self.provider, manager_request("go"))
        frame = json.loads(data.decode().splitlines()[0][6:])
        self.assertEqual(frame["choices"][0]["delta"]["tool_calls"][0]["id"], call[2])
        injected = {"workbench_message_id": "m1", "kind": "report", "payload": {"handoff": "to_manager",
                                                                               "kind": "done"}}
        post(self.provider, manager_request(json.dumps(injected)))
        post(self.provider, {"tools": [{"function": {"name": "to_worker"}}],
                             "messages": [{"role": "tool", "tool_call_id": call[2],
                                           "content": json.dumps({"status": "dispatched"})}]})
        snap = self.provider.snapshot()
        self.assertEqual(snap["injected"]["manager"][0]["message_id"], "m1")
        self.assertEqual(snap["injected"]["manager"][0]["payload_kind"], "done")
        self.assertEqual(self.provider.tool_results[call[2]], {"status": "dispatched"})

    def test_http_error_turn(self):
        self.provider.on(lambda r: True, h.error(529, "overloaded"), role="worker")
        _, response, data = post(self.provider, {"tools": [{"function": {"name": "to_manager"}}],
                                                 "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(response.status, 529)
        self.assertEqual(json.loads(data)["error"]["message"], "overloaded")

    def test_streamed_turn_and_client_abort_is_recorded(self):
        self.provider.on_text("slow", h.text("abcdefghij", chunks=10, chunk_gap=0.3), role="manager")
        started = time.monotonic()
        _, response, data = post(self.provider, manager_request("slow"))
        self.assertEqual(sse_text(data), "abcdefghij")
        self.assertGreater(time.monotonic() - started, 2.0)
        body = json.dumps(manager_request("slow")).encode()
        with socket.create_connection(("127.0.0.1", self.provider.port), timeout=10) as raw:
            raw.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            raw.recv(256)  # headers and the first chunk; then the client goes away mid-stream
        deadline = time.monotonic() + 8
        while not self.provider.snapshot()["aborted"] and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.provider.snapshot()["aborted"][0]["role"], "manager")

    def test_script_errors_answer_instead_of_hanging(self):
        self.provider.on(lambda r: True, lambda r: 1 / 0, name="broken")
        _, response, data = post(self.provider, manager_request("x"))
        self.assertEqual(response.status, 200)
        self.assertIn("script-error", sse_text(data))
        self.assertTrue(self.provider.snapshot()["errors"])

    def test_models_yml_names_the_local_port_only(self):
        text = self.provider.models_yml()
        self.assertIn(f"http://127.0.0.1:{self.provider.port}/v1", text)
        self.assertIn("auth: none", text)
        self.assertEqual(self.provider.model, f"{h.PROVIDER_NAME}/{h.MODEL_ID}")


class HelperTests(unittest.TestCase):
    def test_shutdown_result(self):
        self.assertEqual(h.shutdown_result('no active work reported\n{"shutdown": {"verified": true}}\n'),
                         {"verified": True})
        self.assertEqual(h.shutdown_result('{"shutdown": {"verified": false}}'), {"verified": False})
        self.assertEqual(h.shutdown_result("not json"), {})

    def test_clean_env_never_inherits_outer_context(self):
        env = h.clean_base_env(TMUX="/tmp/x,1,0", HERDR_ENV="1", WORKBENCH_X="1", DISPLAY=":0", HOME="/tmp/h")
        self.assertFalse(any(k.startswith(("TMUX", "HERDR_", "WORKBENCH_")) or k == "DISPLAY" for k in env))
        with self.assertRaises(AssertionError):
            h.assert_env_isolated({"HOME": "/home/someone", **{k: h.BLOCKED_PROXY for k in h.PROXY_KEYS}},
                                  Path("/tmp/owned-root"))

    def test_step_runner_statuses_and_prerequisites(self):
        report = h.ScenarioReport("unit-runner", run_id="unit")
        runner = h.StepRunner(report)

        def failing():
            raise AssertionError("observed something else")

        def not_applicable():
            raise h.NotApplicable("plain has no outer")

        self.assertEqual(runner.run("a", lambda: {"x": 1}), h.PASS)
        self.assertEqual(runner.run("b", failing), h.FAIL)
        self.assertEqual(runner.run("c", not_applicable), h.NA)
        self.assertEqual(runner.run("d", lambda: None, requires=["b"]), h.NOT_RUN)
        self.assertEqual(runner.run("e", lambda: None, requires=["a", "c"]), h.PASS)
        self.assertEqual(runner.failures(), {"b": h.FAIL, "d": h.NOT_RUN})
        self.assertIn("observed something else", report.data["steps"]["b"]["assertion"])

    def test_kill_exact_refuses_a_wrong_start_time(self):
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait, 10)
        start = h.ticks(child.pid)
        self.assertFalse(h.kill_exact(child.pid, start + 1))
        self.assertIsNone(child.poll())
        self.assertTrue(h.kill_exact(child.pid, start))


if __name__ == "__main__":
    unittest.main()
