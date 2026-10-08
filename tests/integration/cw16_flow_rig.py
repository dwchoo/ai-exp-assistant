"""Shared rig for the CW-16 B2 flow / policy / shell scenarios (live_cw16_flow_policy.py, live_cw16_shell.py).

Not a unittest module. Built only on the B1 harness (``cw16_harness``): the real backend through
``python -m workbench start``, the user's installed OMP (version recorded, never pinned: C-D72 (2)), the product
UI on an owned PTY and a scripted local provider (ZERO model requests).

What the rig adds on top of the harness (nothing in ``cw16_harness`` is changed):

* a recording rule (first rule, never answers) that keeps every Workbench-injected message *in full* per role
  (with the time it reached the provider) and every tool result by call id (with the time it came back);
* full-argument builders for the bridge tools (strict schema: every optional field is sent as null);
* scripted-worker helpers: answer a staged ``response_contract`` (execute / analysis) with the fixture model of
  ``tests/workflow/live_workflow_probe._frame_from_contract`` (the model never invents an identifier);
* typing into a pane through the product UI (prefix + 1/2/3), UI key helpers (pause/resume, takeover, kill);
* the per-scenario report (``ScenarioReport`` + ``StepRunner``) and bounded cleanup through ``Sandbox.close``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cw16_harness as h  # noqa: E402

_frame_from_contract = h._load(h.REPO / "tests/workflow/live_workflow_probe.py",
                               "cw16_b2_workflow_probe")._frame_from_contract

LIVE = os.environ.get("WB_LIVE_CW16") == "1"
RUN_ID = os.environ.get("WB_CW16_RUN_ID") or time.strftime("cw16-b2-%Y%m%dT%H%M%S")
os.environ.setdefault("WB_CW16_RUN_ID", RUN_ID)
SCENARIOS = {s.strip() for s in os.environ.get("WB_CW16_SCENARIOS", "").split(",") if s.strip()}


def skip_reason() -> str | None:
    if not LIVE:
        return "set WB_LIVE_CW16=1 (real OMP, scripted local provider, no model)"
    if sys.platform != "linux":
        return "Linux only"
    if not h.find_omp():
        return "OMP is not installed"
    if not h.pyte_available():
        return "pyte is required (use /tmp/cw02-g1-venv/bin/python)"
    return None


def selected(name: str) -> bool:
    return not SCENARIOS or name in SCENARIOS


TO_WORKER_FIELDS = ("task_id", "kind", "message", "spec", "analysis", "commands", "run", "cancel")
TO_MANAGER_FIELDS = ("kind", "message", "task_id", "in_reply_to", "requires_code_change", "reason", "request")


def to_worker_args(**kw: Any) -> dict:
    args = {name: None for name in TO_WORKER_FIELDS}
    args.update(kw)
    return args


def to_manager_args(**kw: Any) -> dict:
    args = {name: None for name in TO_MANAGER_FIELDS}
    args.update(kw)
    return args


def spec(goal: str, paths: list[str], execution: dict | None = None, instructions: str | None = None) -> dict:
    return {"goal": goal, "paths": paths, "instructions": instructions, "execution": execution}


def execution(source: Path, commit: str, command: str | list[str], *, log: str, result_file: str,
              result: str = "PASS", environment: list[str] | None = None, shell: str = "bash") -> dict:
    return {"source": str(source), "commit": commit, "command": command,
            "criteria": {"log_contains": log, "result_file": result_file, "result_contains": result},
            "environment": ["PATH"] if environment is None else environment, "shell": shell}


def stage_of(injected: dict | None) -> str | None:
    payload = (injected or {}).get("payload")
    return payload.get("stage") if isinstance(payload, dict) else None


def answer_contract(injected: dict, decision: str) -> str:
    """The scripted worker's single staged answer (marker + compact JSON), or 'invalid' if the contract is new."""
    return _frame_from_contract(injected, decision) or "invalid-contract"


class Rig:
    """One scenario: provider + sandbox + UI + report. Use ``Rig(test, "F1")`` then ``rig.start()``."""

    def __init__(self, test, scenario: str, *, host_shell: str = "bash", **context: Any):
        self.scenario = scenario
        self.report = h.ScenarioReport(scenario, run_id=RUN_ID, host_shell=host_shell, **context)
        self.report.data["versions"] = h.tool_versions()
        self.runner = h.StepRunner(self.report)
        self.p = h.ScriptedProvider()
        self.lock = threading.Lock()
        self.inj: dict[str, list[dict]] = {"manager": [], "worker": [], "unknown": []}
        self.results: dict[str, dict] = {}
        self.result_at: dict[str, float] = {}
        self.notes: list[str] = []
        self.p.on(self._record, None, name="record")
        self.sb = h.Sandbox(scenario, provider=self.p, host_shell=host_shell, report=self.report)
        self.ui: h.Ui | None = None
        self._closed = False
        test.addCleanup(self.finish)

    # -- recording ------------------------------------------------------------------------------------
    def _record(self, request: h.Request) -> bool:
        now = time.time()
        with self.lock:
            if request.injected is not None:
                self.inj.setdefault(request.role, []).append({"at": now, "index": request.index, **request.injected})
            for call_id, value in request.tool_results.items():
                if call_id not in self.results:
                    self.results[call_id] = value
                    self.result_at[call_id] = now
        return False

    def injected(self, role: str, predicate: Callable[[dict], bool] = lambda i: True) -> list[dict]:
        with self.lock:
            return [i for i in self.inj.get(role, []) if predicate(i)]

    def wait_injected(self, role: str, predicate: Callable[[dict], bool], timeout: float = 60,
                      what: str = "") -> dict:
        found: list[dict] = []
        ok = self.pump_until(lambda: bool(found.extend(self.injected(role, predicate)) or found), timeout)
        if not ok:
            raise AssertionError(f"no {role} injected message ({what}) in {timeout}s; "
                                 f"kinds={[(i.get('kind'), stage_of(i)) for i in self.injected(role)]}")
        return found[0]

    def result(self, call: tuple | str) -> dict | None:
        call_id = call if isinstance(call, str) else call[2]
        with self.lock:
            return self.results.get(call_id)

    def wait_result(self, call: tuple | str, timeout: float = 60) -> dict:
        call_id = call if isinstance(call, str) else call[2]
        if not self.pump_until(lambda: self.result(call_id) is not None, timeout):
            raise AssertionError(f"no tool result for {call_id} in {timeout}s; provider={self.p.snapshot()['requests']}")
        return self.result(call_id)

    # -- tool builders ---------------------------------------------------------------------------------
    def to_worker(self, **kw: Any) -> tuple:
        return self.p.call("manager", "to_worker", to_worker_args(**kw))

    def to_manager(self, **kw: Any) -> tuple:
        return self.p.call("worker", "to_manager", to_manager_args(**kw))

    def terminal(self, command: str | None) -> tuple:
        return self.p.call("worker", "terminal", {"command": command})

    # -- lifecycle ------------------------------------------------------------------------------------
    def start(self, *, ui: bool = True) -> dict:
        done = self.sb.start()
        assert done.returncode == 0, done.stdout[-800:] + done.stderr[-800:]
        ready = self.sb.wait_ready()
        self.report.data["identities"] = h.identity(ready)
        self.report.data["omp_isolation"] = (ready.get("omp_isolation") or {}).get("state")
        if ui:
            self.ui = self.sb.ui(label=f"{self.scenario}-ui")
            assert self.ui.wait(lambda: "HOST SHELL" in self.ui.text() and "WORKER OMP" in self.ui.text(), 60), \
                self.ui.excerpt("OMP", "SHELL")
        return ready

    def pump(self, seconds: float = 0.2) -> None:
        if self.ui is not None:
            self.ui.pump(seconds)
        else:
            time.sleep(seconds)

    def pump_until(self, predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            self.pump(0.1)
        return bool(predicate())

    def status(self) -> dict:
        snap = self.sb.status()
        assert snap is not None, "backend is not running"
        return snap

    def wait(self, predicate: Callable[[dict], bool], timeout: float = 60, what: str = "") -> dict:
        try:
            return self.sb.wait_status(predicate, timeout, what, pump=[self.ui] if self.ui else [])
        except AssertionError as exc:
            snap = self.sb.status() or {}
            auto = snap.get("automation") or {}
            detail = {"task": snap.get("task"), "automation": {k: auto.get(k) for k in (
                "state", "paused", "detail", "resume", "transition", "run", "review", "interruption")},
                "shell": ((snap.get("panes") or {}).get("host_shell") or {}).get("shell"),
                "ui": self.ui.excerpt("자동화", "작업:", "재개", "거부", "일시정지") if self.ui else None}
            raise AssertionError(f"{exc}; last={json.dumps(detail, ensure_ascii=False, default=str)[:3000]}") from None

    def task(self) -> dict:
        return self.status().get("task") or {}

    def wait_task(self, predicate: Callable[[dict], bool], timeout: float = 60, what: str = "") -> dict:
        return self.wait(lambda s: predicate(s.get("task") or {}), timeout, what)["task"]

    def focus(self, pane: str) -> None:
        assert self.ui is not None
        self.ui.send(h.PREFIX + h.FOCUS_KEYS[pane], settle=0.3)
        self.wait(lambda s: s["focus"] == pane, 15, f"focus {pane}")

    def say(self, pane: str, text: str, *, enter: bool = True) -> None:
        """Type ``text`` into a pane through the product UI (as the user does)."""
        self.focus(pane)
        self.ui.type(text, gap=0.01)
        if enter:
            self.ui.send(b"\r", settle=0.3)

    def ui_key(self, key: bytes, *, confirm: bytes | None = None, settle: float = 0.5) -> None:
        self.ui.send(h.PREFIX + key, settle=settle)
        if confirm is not None:
            self.ui.send(confirm, settle=settle)

    def pause(self) -> dict:
        self.ui_key(b"p")
        assert self.ui.wait_text(r"자동화를 일시정지합니다", 10), self.ui.excerpt("자동화")
        self.ui.send(b"p", settle=0.5)
        return self.wait(lambda s: s["automation"]["state"] == "paused", 40, "paused")

    def resume(self) -> dict:
        self.ui_key(b"p")
        assert self.ui.wait_text(r"대조 후 재개합니다", 10), self.ui.excerpt("자동화")
        self.ui.send(b"p", settle=0.5)
        return self.wait(lambda s: s["automation"]["paused"] is False, 60, "resumed")

    def host_file(self, name: str) -> Path:
        return self.sb.project / name

    def journal(self) -> list[dict]:
        path = self.sb.data / "workflow" / "handoffs.jsonl"
        try:
            return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except (OSError, ValueError):
            return []

    def note(self, text: str) -> None:
        self.notes.append(text)

    def step(self, name: str, fn: Callable[[], dict | None], requires: tuple[str, ...] = ()) -> str:
        return self.runner.run(name, fn, requires=requires)

    def shutdown_clean(self) -> dict:
        if self.ui is not None:
            self.ui.send(h.PREFIX + b"q", settle=0.5)
            self.ui.wait_exit(10)
        done = self.sb.shutdown()
        payload = h.shutdown_result(done.stdout)
        left = self.sb.wait_no_residue(40)
        assert done.returncode == 0 and payload.get("verified") is True, done.stdout[-600:] + done.stderr[-300:]
        assert left == {}, left
        return {"verified": True, "residue": left}

    def finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.report.data["cleanup"] = self.sb.close()
        finally:
            snap = self.p.snapshot()
            self.report.data["provider"] = {"requests": snap["requests"], "aborted": snap["aborted"],
                                            "errors": snap["errors"], "rules": snap["rules"],
                                            "real_model_requests": 0, "log_tail": self.p.log[-80:]}
            self.report.data["injected"] = {role: [{"kind": i.get("kind"), "stage": stage_of(i),
                                                    "payload_kind": (i.get("payload") or {}).get("kind")
                                                    if isinstance(i.get("payload"), dict) else None,
                                                    "task_id": i.get("task_id"), "at": i.get("at")}
                                                   for i in items] for role, items in self.inj.items()}
            self.report.data["notes"] = self.notes
            self.p.close()
            path = self.report.write()
            print(f"\n[cw16-b2] {self.scenario}: {self.report.data['result']} -> {path}", file=sys.stderr)


def assert_steps(test, rig: Rig) -> None:
    test.assertEqual(rig.runner.failures(), {}, json.dumps(rig.report.data["steps"], ensure_ascii=False,
                                                           default=str)[:6000])


def write_summary(prefixes: tuple[str, ...], name: str) -> Path:
    """One summary JSON for every ``<prefix>*.json`` scenario report of this run."""
    directory = h.REPORT_DIR / RUN_ID
    rows = {}
    paths = sorted({p for prefix in prefixes for p in directory.glob(f"{prefix}*.json")})
    for path in paths:
        if path.name == name:
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        rows[data.get("scenario", path.stem)] = {"result": data.get("result"),
                                                 "steps": {k: v.get("status") for k, v in data.get("steps", {}).items()},
                                                 "unknowns": data.get("unknowns"), "path": str(path)}
    target = directory / name
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"run_id": RUN_ID, "scenarios": rows, "written_at": h.iso_now()}, indent=1,
                                 ensure_ascii=False))
    return target
