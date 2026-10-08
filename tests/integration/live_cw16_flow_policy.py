"""LIVE CW-16 I-FLOW / I-POLICY scenarios (B2) through the product path. Opt-in: WB_LIVE_CW16=1.

Every scenario starts the real backend with ``python -m workbench start`` (the user's installed OMP, version
recorded, never pinned: C-D72 (2)), drives the product UI (``python -m workbench attach``) on an owned PTY and
answers both OMPs from a scripted local provider (``cw16_harness.ScriptedProvider``): ZERO real model requests.
The scripted model only plays the model's part (which tool it calls, what it answers); everything checked is the
product's behaviour: tool results, Task / run / automation state, injected messages, host terminal effects, UI.

Scenarios (plan result-p27-cw16-plan-01.md s5.4; C-D72 (3) for F5):
  F1  free work: manager pane input -> to_worker(work, commands) -> worker terminal -> exit/log_path ->
      to_manager done + commands_run -> manager receives -> Task closed done.
  F4  (with F1) the same toolCallId re-sent is idempotent (same result, one Task); a new Task while busy ->
      worker_busy (nothing queued).
  F2  experiment: worktree at the commit, host run, collect/judge, worker 1st judgment, manager receives the
      report (2nd check is the manager's): success / exit != 0 / exit 0 with the failure marker / missing result
      (indeterminate); a missing commit -> preparation failure reported, nothing run.
  F3  a delegated change inside the approved paths -> only a local commit; a local bare remote's refs are
      unchanged (no push), the experiment worktree is detached (no branch).
  F5  worker direct requests (C-D72 (3)): no Task -> unrestricted; inside an open Task -> run/report linked to
      that Task; beyond the open Task's scope -> nothing outside the Task runs and the worker's request reaches
      the manager classified as a scope expansion (no new Task, Task unchanged).
  F6  requires_code_change reports (done -> Task closed; blocked -> Task blocked, worker free): no automatic
      delivery to the worker until a new manager instruction, which is delivered once.
  F7  before any manager instruction automation stays idle and nothing runs (65 s).
  F45 (assignment: CW-19 VM '45 s' observation) worker ``terminal`` while a dispatched Task is not yet delivered
      (the worker OMP is mid-turn) and right after a cancel -> answered promptly.
  P1  silent run > 140 s: periodic reviews about every 60 s; a busy worker -> delayed, merged into the latest one.
  P7  (with P1) usage shown in status/UI (C-AC-21) and the review facts' usage stays 'unknown' (never a number).
  P2  pause: to_worker held:paused, terminal paused, no review while paused, raw log keeps growing, manual
      input works, the run's end is shown while paused; resume (reconciled) -> no replay.
  P3  pause during a streaming manager turn -> interrupt requested/confirmed (provider stream aborted); a
      running manager tool -> its call id recorded as unknown; resume reconciled; the aborted turn is not re-run.
  P4  failing run -> run:true x3 (one with a spec change: new revision, count kept) -> 4th re-run held
      retry_limit, no further run.
  P5  a spec change while a run is in progress is not applied to it (worker_busy); the run keeps its revision.
  P6  restart_worker ends only the worker OMP (verified dead), Task kept; stop_survivor unknown id refused;
      host terminal force kill (UI prefix k, k) verified, Enter starts a new shell.
  P8  cancel: a running experiment -> cancel_requested, the command is not killed, closed cancelled after its
      exit, run records kept and linked; a work Task cancel withdraws a queued follow-up (never delivered).
  CLK the clock fixture suite (fake-clock unit tests of the 60 s checks, merging, watchdog, pause policy, the
      121st review) is run and recorded (fixture evidence; not runtime).

Run (repo root; the venv has pyte)::

    WB_LIVE_CW16=1 PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest tests/integration/live_cw16_flow_policy.py -v

Filters: WB_CW16_SCENARIOS=F1,P2,...  Reports: $WB_CW16_REPORT_DIR/<run id>/flow-*.json, policy-*.json,
clock-fixture-suite.json, flow-policy-summary.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import unittest
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cw16_harness as h  # noqa: E402
import cw16_flow_rig as fr  # noqa: E402
from cw16_flow_rig import Rig, execution, spec, stage_of  # noqa: E402

SKIP = fr.skip_reason()


def judge_from_facts(facts: dict) -> str:
    """The scripted worker's 1st judgment from the public run facts (what a careful worker would say)."""
    if "preparation_error" in facts:
        return "indeterminate"
    if not facts.get("result_excerpt") or not facts.get("raw_log_excerpt"):
        return "indeterminate"
    if facts.get("exit_status") != 0:
        return "failure"
    criteria = facts.get("criteria") or {}
    if criteria.get("log_contains", "\0") in facts["raw_log_excerpt"] \
            and criteria.get("result_contains", "\0") in facts["result_excerpt"]:
        return "success"
    return "failure"


def contract_worker(rig: Rig, *, execute: str = "execute") -> None:
    """Staged worker answers: execute -> ``execute``; analysis -> judgment from the facts."""

    def answer(r: h.Request) -> str:
        stage = stage_of(r.injected)
        if stage == "execute":
            return fr.answer_contract(r.injected, execute)
        facts = (r.injected.get("payload") or {}).get("facts") or {}
        return fr.answer_contract(r.injected, judge_from_facts(facts))

    rig.p.on(lambda r: r.injected is not None and "response_contract" in r.injected, answer, role="worker",
             name="worker-contract")


class Chain:
    """One scripted model turn of several tool calls: ``trigger`` starts it, each step runs after the previous
    call's result came back. A step returns a call tuple (next tool call) or a str / Turn (ends the turn)."""

    def __init__(self, rig: Rig, role: str, trigger, steps: list, name: str):
        self.rig, self.steps, self.name = rig, steps, name
        self.calls: list[tuple] = []
        self.results: list[dict] = []
        self.started = threading.Event()
        self.done = threading.Event()
        rig.p.on(lambda r: not self.started.is_set() and trigger(r), self._start, role=role, name=f"{name}:start")
        rig.p.on(self._pending, self._next, role=role, name=f"{name}:next")

    def _emit(self, r: h.Request, index: int):
        if index >= len(self.steps):
            self.done.set()
            return f"{self.name} finished"
        out = self.steps[index](r, self)
        if isinstance(out, tuple):
            self.calls.append(out)
            return h.tools(out)
        self.done.set()
        return out

    def _start(self, r: h.Request):
        self.started.set()
        return self._emit(r, 0)

    def _pending(self, r: h.Request) -> bool:
        return (bool(self.calls) and len(self.results) < len(self.calls) and r.last_role == "tool"
                and self.calls[-1][2] in r.tool_results)

    def _next(self, r: h.Request):
        self.results.append(r.tool_results[self.calls[-1][2]])
        return self._emit(r, len(self.calls))

    def wait(self, timeout: float) -> bool:
        return self.rig.pump_until(self.done.is_set, timeout)


def user_text(suffix: str):
    return lambda r: r.injected is None and r.last_role == "user" and r.last_text.endswith(suffix)


def task_injected(r: h.Request) -> bool:
    return r.injected is not None and r.injected.get("kind") == "task" and "response_contract" not in r.injected


def quiet_window(rig: Rig, seconds: float, *, role: str = "worker") -> dict:
    """Pump for ``seconds``; report the provider requests and injected messages of ``role`` meanwhile."""
    before_requests = rig.p.count(role)
    before_injected = len(rig.injected(role))
    end = time.monotonic() + seconds
    states = set()
    while time.monotonic() < end:
        rig.pump(2.0)
        snap = rig.sb.status()
        if snap:
            states.add((snap.get("automation") or {}).get("state"))
    return {"seconds": seconds, "requests": rig.p.count(role) - before_requests,
            "injected": rig.injected(role)[before_injected:], "automation_states": sorted(s for s in states if s)}


def exp_task(rig: Rig, trigger: str, message: str, command, *, log: str, result_file: str, result: str = "PASS",
             environment=None, shell: str = "bash", commit: str | None = None, paths=("out/",)) -> dict:
    """A manager rule: user text ``trigger`` -> to_worker(experiment). Returns a dict that receives the call."""
    holder: dict = {}

    def respond(r: h.Request):
        call = rig.to_worker(kind="experiment", message=message, spec=spec(message, list(paths), execution(
            rig.sb.project, commit or rig.sb.commit, command, log=log, result_file=result_file, result=result,
            environment=environment, shell=shell)))
        holder["call"] = call
        return h.tools(call)

    rig.p.on(user_text(trigger), respond, role="manager", once=True, name=f"exp:{trigger}")
    return holder


def run_dir(rig: Rig, run_id: str) -> Path:
    return rig.sb.data / "workflow" / "runs" / run_id


def raw_log(rig: Rig, run_id: str) -> Path:
    return rig.sb.data / "raw-logs" / f"{run_id}.log"


def reports_for(rig: Rig, task_id: str) -> list[dict]:
    return rig.injected("manager", lambda i: i.get("kind") == "report" and i.get("task_id") == task_id)


@unittest.skipIf(SKIP, SKIP or "")
class Cw16FlowScenarios(unittest.TestCase):
    """I-FLOW: F1-F7 and the CW-19 '45 s' terminal observation (F45)."""

    def setUp(self):
        name = self._testMethodName.split("_")[1].upper()
        if not fr.selected(name):
            self.skipTest(f"filtered out by WB_CW16_SCENARIOS ({name})")

    # ------------------------------------------------------------------------------------------- F1 + F4
    def test_f1_free_work_with_f4_idempotence(self):
        rig = Rig(self, "flow-F1-F4")
        commands = ["echo F1_ONE_$((40+2))", "printf 'F1_TWO\\n' > f1-two.txt"]
        release = threading.Event()
        state: dict[str, Any] = {}

        def manager(r: h.Request):
            if r.injected is None and r.last_role == "user" and r.last_text.endswith("f1-go"):
                state["first"] = rig.to_worker(kind="work", message="run the two F1 commands and report",
                                               spec=spec("F1 free work", ["notes/"]), commands=commands)
                return h.tools(state["first"])
            if r.last_role != "tool":
                return None
            first = state.get("first")
            if first and first[2] in r.tool_results and "dup_sent" not in state:
                state["dup_sent"] = True
                return h.tools(first)  # the same toolCallId again (a re-sent call)
            dup_ids = [cid for cid in r.tool_results if cid.startswith(first[2])] if first else []
            if state.get("dup_sent") and "dup_result" not in state and dup_ids:
                state["dup_id"] = dup_ids[0]  # OMP 18.8.0 renames a repeated model call id (<id>_dup1)
                state["dup_result"] = r.tool_results[dup_ids[0]]
                state["busy"] = rig.to_worker(kind="work", message="another task while busy",
                                              spec=spec("F4 other", ["other/"]))
                return h.tools(state["busy"])
            busy = state.get("busy")
            if busy and busy[2] in r.tool_results:
                return "noted: the worker is busy"
            return None

        rig.p.on(lambda r: True, manager, role="manager", name="f1-manager")
        task_id: dict = {}

        def first_step(r, chain):
            task_id["id"] = r.injected["task_id"]
            release.wait(90)  # keep the Task open while F4 and the busy UI are checked
            return rig.terminal(commands[0])

        worker = Chain(rig, "worker", task_injected, [
            first_step,
            lambda r, c: rig.terminal("echo F1_NOT_LISTED > f1-not-listed.txt"),
            lambda r, c: rig.terminal(commands[1]),
            lambda r, c: rig.to_manager(kind="done", message="F1: both commands ran", task_id=task_id["id"]),
            lambda r, c: "F1 reported",
        ], "f1-worker")

        def start():
            ready = rig.start()
            assert ready["worker"] == {"state": "idle", "task_id": None}, ready["worker"]
            return {"isolation": rig.report.data["omp_isolation"]}

        def f4():
            rig.say("manager_omp", "f1-go")
            assert rig.pump_until(lambda: "first" in state and rig.result(state["first"]) is not None, 60), state
            first = rig.result(state["first"])
            assert first["status"] == "dispatched" and first["kind"] == "work", first
            assert rig.pump_until(lambda: "busy" in state and rig.result(state["busy"]) is not None, 60), \
                {k: v for k, v in state.items() if k != "first"}
            busy = rig.result(state["busy"])
            dup = state.get("dup_result")
            if state.get("dup_id") == state["first"][2]:  # the bridge saw the same toolCallId: stored result
                assert dup == first, {"dup": dup, "first": first}
                dup_path = "same toolCallId reached the backend: identical stored result"
            else:  # OMP gave the repeated call a new id: a new request, answered worker_busy (no 2nd Task)
                assert dup["status"] == "worker_busy" and dup["task"]["task_id"] == first["task_id"], dup
                dup_path = f"OMP renamed the repeated call id to {state.get('dup_id')}: worker_busy, no second Task"
            assert busy["status"] == "worker_busy" and busy["task"]["task_id"] == first["task_id"], busy
            snap = rig.wait(lambda s: s["worker"]["state"] == "busy", 20, "worker busy")
            assert rig.ui.wait_text(r"worker: 작업 중", 20), rig.ui.excerpt("worker")
            ui_lines = rig.ui.excerpt("worker:", "작업:")
            tasks = rig.injected("worker", lambda i: i.get("kind") == "task")
            assert len(tasks) <= 1, tasks
            journal = [j for j in rig.journal() if j.get("type") == "duplicate"
                       or (j.get("key") or {}).get("tool_call_id") == state["first"][2]]
            release.set()
            return {"dispatched": first, "repeated_call": dup_path, "repeated_call_result": dup.get("status"),
                    "busy": busy["status"],
                    "busy_task": busy["task"], "ui": ui_lines, "worker": snap["worker"],
                    "journal_records_for_call": [j.get("type") for j in journal]}

        def f1():
            assert worker.wait(90), {"calls": worker.calls, "results": worker.results}
            one, refused, two, report = worker.results
            assert one["status"] == "exited" and one["exit_code"] == 0 and "F1_ONE_42" in one["output_tail"], one
            assert Path(one["log_path"]).read_text(errors="replace").count("F1_ONE_42") >= 1, one["log_path"]
            assert refused["status"] == "not_in_task_commands" and refused["allowed_commands"] == commands, refused
            assert not rig.host_file("f1-not-listed.txt").exists(), "a refused command ran"
            assert two["status"] == "exited" and two["exit_code"] == 0, two
            assert rig.host_file("f1-two.txt").read_text() == "F1_TWO\n"
            assert report["status"] == "queued", report
            done = rig.wait_task(lambda t: t.get("status") == "closed", 60, "Task closed")
            assert done["closed_reason"] == "done" and done["task_id"] == task_id["id"], done
            snap = rig.status()
            assert snap["worker"] == {"state": "idle", "task_id": None}, snap["worker"]
            got = rig.wait_injected("manager", lambda i: i.get("kind") == "report", 30, "done report")
            payload = got["payload"]
            assert payload["handoff"] == "to_manager" and payload["kind"] == "done", payload
            run = payload.get("commands_run") or []
            assert [(e["command"], e["status"], e["exit_code"]) for e in run] == \
                [(commands[0], "exited", 0), (commands[1], "exited", 0)], run
            assert all(Path(e["log_path"]).exists() for e in run), run
            assert got["task_id"] == task_id["id"], got
            tasks = rig.injected("worker", lambda i: i.get("kind") == "task")
            assert len(tasks) == 1, f"worker_busy / duplicate queued a second Task: {len(tasks)}"
            assert not (rig.sb.project / "other").exists()
            assert rig.ui.wait_text(r"F1_ONE_42", 10), "the host pane shows the worker's command output"
            return {"terminal": [{k: v.get(k) for k in ("status", "exit_code", "command", "log_path")}
                                 for v in (one, refused, two)], "report": {k: payload.get(k) for k in
                                                                           ("kind", "commands_run", "message")},
                    "task": done, "ui_host": rig.ui.excerpt("F1_ONE_42", "[worker]")}

        rig.step("start", start)
        rig.step("F4_repeated_call_and_busy", f4, ("start",))
        rig.note("F4: exact-toolCallId idempotence (a frame re-sent with the same id) is not reachable through OMP "
                 "18.8.0, which renames a repeated model call id; it is covered by the fixture suite "
                 "(tests/backend/test_handoff_service.py, test_flow_terminal.py) in policy-CLK.")
        release.set()
        rig.step("F1_free_work", f1, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- F2
    def test_f2_experiment_judgments_and_missing_commit(self):
        rig = Rig(self, "flow-F2")
        contract_worker(rig)
        cases = [
            ("a", "mkdir -p out; echo F2A_OK; printf PASS > out/a.txt", "F2A_OK", "out/a.txt", "success",
             ["criteria_met"]),
            ("b", "mkdir -p out; echo F2B_FAILED; printf FAIL > out/b.txt; exit 3", "F2B_OK", "out/b.txt",
             "failure", ["nonzero_exit"]),
            ("c", "mkdir -p out; echo F2C_FAILED; printf PASS > out/c.txt", "F2C_OK", "out/c.txt", "failure",
             ["exit_zero_criteria_failed"]),
            ("d", "echo F2D_OK", "F2D_OK", "out/d.txt", "indeterminate", ["missing_or_empty_result_file"]),
        ]
        holders = {key: exp_task(rig, f"f2-{key}", f"F2 case {key}", command, log=log, result_file=result_file)
                   for key, command, log, result_file, _, _ in cases}
        missing = exp_task(rig, "f2-missing", "F2 missing commit", "touch F2_SHOULD_NOT_RUN; echo X", log="X",
                           result_file="out/x.txt", commit="0" * 40)
        rig.step("start", lambda: rig.start() and {})

        def run_case(key, expected, reasons):
            def step():
                rig.say("manager_omp", f"f2-{key}")
                assert rig.pump_until(lambda: "call" in holders[key] and rig.result(holders[key]["call"]), 60)
                dispatched = rig.result(holders[key]["call"])
                assert dispatched["status"] == "dispatched", dispatched
                tid = dispatched["task_id"]
                task = rig.wait_task(lambda t: t.get("task_id") == tid and t["status"] in ("finished", "closed"),
                                     150, f"case {key} finished")
                result = task["last_result"]
                assert result["judgment"] == expected and result["run_closed"] is True, result
                report = rig.pump_until(lambda: bool(reports_for(rig, tid)), 30) and reports_for(rig, tid)[0]
                assert report, "no report reached the manager"
                payload = report["payload"]
                assert payload["judgment"] == expected and payload["reasons"] == reasons, payload
                assert payload["worker_response"]["decision"] == expected, payload.get("worker_response")
                run_id = result["run_id"]
                worktree = Path(payload["result_file"]).parents[len(Path(payload["criteria"]["result_file"]).parts) - 1]
                stages = [stage_of(i) for i in rig.injected("worker", lambda i: i.get("task_id") == tid)]
                assert stages[:2] == ["execute", "analysis"], stages
                assert raw_log(rig, run_id).exists() or Path(payload["raw_log"]).exists(), payload["raw_log"]
                assert str(worktree).startswith(str(rig.sb.data / "workflow" / "worktrees")), worktree
                return {"task_id": tid, "judgment": expected, "reasons": payload["reasons"],
                        "exit_status": payload["exit_status"], "exit_confirmed": payload["exit_confirmed"],
                        "worker_decision": payload["worker_response"]["decision"], "worktree": str(worktree),
                        "raw_log": payload["raw_log"], "stages_to_worker": stages}
            return step

        for key, _, _, _, expected, reasons in cases:
            rig.step(f"F2{key}_{expected}", run_case(key, expected, reasons), ("start",))

        def missing_commit():
            rig.say("manager_omp", "f2-missing")
            assert rig.pump_until(lambda: "call" in missing and rig.result(missing["call"]), 60)
            dispatched = rig.result(missing["call"])
            assert dispatched["status"] == "dispatched", dispatched
            tid = dispatched["task_id"]
            task = rig.wait_task(lambda t: t.get("task_id") == tid and t["status"] in ("finished", "closed")
                                 and t.get("last_result"), 120, "missing commit outcome")
            report = rig.pump_until(lambda: bool(reports_for(rig, tid)), 30) and reports_for(rig, tid)[0]
            ran = [p for p in (rig.sb.project, rig.sb.data) for p in p.rglob("F2_SHOULD_NOT_RUN")]
            assert not ran, ran
            records = [json.loads(p.read_text()) for p in (rig.sb.data / "workflow" / "runs").glob("*/run.json")]
            records = [rec for rec in records if rec.get("task_id") == tid]
            record = records[-1] if records else {}
            assert record.get("shell_state") == "preparation_failed", {
                "task": task, "records": [{k: rec.get(k) for k in ("run_id", "shell_state", "preparation_error")}
                                          for rec in records]}
            assert report and report["payload"].get("stage") == "preparation" \
                and report["payload"].get("judgment") == "indeterminate", report and report["payload"]
            return {"task": task, "preparation_error": record.get("preparation_error"),
                    "report_payload": {k: report["payload"].get(k) for k in ("stage", "judgment", "error",
                                                                             "requires_manager_resolution")},
                    "nothing_ran": True}

        rig.step("F2_missing_commit_nothing_runs", missing_commit, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- F3
    def test_f3_delegated_change_local_commit_only(self):
        rig = Rig(self, "flow-F3")
        contract_worker(rig)
        remote = rig.sb.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True,
                       env=h.clean_base_env(HOME=str(rig.sb.home)))
        rig.sb.git("remote", "add", "origin", str(remote))
        rig.sb.git("push", "-q", "origin", "HEAD:refs/heads/master")  # test setup (before the product starts)

        def remote_refs() -> str:
            return subprocess.run(["git", "-C", str(remote), "show-ref"], capture_output=True, text=True,
                                  env=h.clean_base_env(HOME=str(rig.sb.home))).stdout.strip()

        refs_before = remote_refs()
        commands = ["git add src/f3.txt", "git commit -qm 'f3 delegated change'"]
        work = {}

        def manager_work(r):
            work["call"] = rig.to_worker(kind="work", message="write src/f3.txt and commit it locally",
                                         spec=spec("F3 delegated change", ["src/"]), commands=commands)
            return h.tools(work["call"])

        rig.p.on(user_text("f3-work"), manager_work, role="manager", once=True)
        tid: dict = {}

        def first(r, c):
            tid["id"] = r.injected["task_id"]
            return rig.p.call("worker", "write", {"path": "src/f3.txt", "content": "F3 delegated change\n"})

        worker = Chain(rig, "worker", task_injected, [
            first,
            lambda r, c: rig.terminal(commands[0]),
            lambda r, c: rig.terminal(commands[1]),
            lambda r, c: rig.to_manager(kind="done", message="F3 committed locally", task_id=tid["id"]),
            lambda r, c: "F3 done",
        ], "f3-worker")
        exp: dict = {}
        rig.step("start", lambda: rig.start() and {})

        def delegated():
            rig.say("manager_omp", "f3-work")
            assert worker.wait(90), {"results": worker.results}
            write, add, commit, report = worker.results
            assert add["status"] == "exited" and add["exit_code"] == 0, add
            assert commit["status"] == "exited" and commit["exit_code"] == 0, commit
            rig.wait_task(lambda t: t.get("status") == "closed" and t.get("closed_reason") == "done", 60, "F3 done")
            head = rig.sb.git("rev-parse", "HEAD")
            changed = rig.sb.git("show", "--name-only", "--format=", "HEAD").split()
            assert changed == ["src/f3.txt"], changed
            assert head != rig.sb.commit
            exp["head"] = head
            return {"write_result": {k: write.get(k) for k in ("status", "non_json")} if isinstance(write, dict)
                    else write, "local_commit": head, "changed": changed}

        def experiment_at_commit():
            holder = exp_task(rig, "f3-exp", "F3 run the delegated change", "cat src/f3.txt; printf PASS > f3.out",
                              log="F3 delegated change", result_file="f3.out", commit=exp["head"])
            rig.say("manager_omp", "f3-exp")
            assert rig.pump_until(lambda: "call" in holder and rig.result(holder["call"]), 60)
            tid2 = rig.result(holder["call"])["task_id"]
            task = rig.wait_task(lambda t: t.get("task_id") == tid2 and t["status"] == "finished", 150, "F3 run")
            assert task["last_result"]["judgment"] == "success", task
            worktrees = rig.sb.git("worktree", "list", "--porcelain")
            branches = rig.sb.git("for-each-ref", "--format=%(refname)", "refs/heads")
            refs_after = remote_refs()
            pushes = [j for j in rig.journal() if "push" in json.dumps(j.get("command") or "")]
            assert refs_after == refs_before, {"before": refs_before, "after": refs_after}
            assert branches.split() == ["refs/heads/master"], branches
            assert "detached" in worktrees, worktrees
            assert rig.sb.git("rev-parse", "HEAD") == exp["head"]
            assert not pushes, pushes
            return {"remote_refs_unchanged": True, "branches": branches.split(), "worktrees": worktrees.splitlines(),
                    "judgment": "success", "push_commands": 0}

        rig.step("F3_local_commit_in_paths", delegated, ("start",))
        rig.step("F3_no_push_no_branch", experiment_at_commit, ("F3_local_commit_in_paths",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- F5
    def test_f5_worker_direct_request_boundaries(self):
        rig = Rig(self, "flow-F5")
        small = Chain(rig, "worker", user_text("f5-small please"), [
            lambda r, c: rig.terminal("echo F5_SMALL > f5-small.txt"),
            lambda r, c: "F5 small request done",
        ], "f5-small")
        open_task: dict = {}

        def manager_open(r):
            open_task["call"] = rig.to_worker(kind="work", message="F5 notes work; wait for details",
                                              spec=spec("F5 notes", ["notes/"]))
            return h.tools(open_task["call"])

        rig.p.on(user_text("f5-open"), manager_open, role="manager", once=True)
        rig.p.on(task_injected, "Task received; waiting for the user's details", role="worker", once=True)
        inside = Chain(rig, "worker", user_text("f5-in add notes/in.txt"), [
            lambda r, c: rig.terminal("mkdir -p notes && echo F5_IN > notes/in.txt"),
            lambda r, c: rig.to_manager(kind="progress", message="F5: the user's notes/in.txt is inside the Task",
                                        task_id=open_task["tid"]),
            lambda r, c: rig.to_manager(kind="done", message="F5 notes done", task_id=open_task["tid"]),
            lambda r, c: "F5 inside done",
        ], "f5-in")
        wide_task: dict = {}
        wide_commands = ["ls notes"]

        def manager_wide(r):
            wide_task["call"] = rig.to_worker(kind="work", message="F5 list notes only",
                                              spec=spec("F5 list notes", ["notes/"]), commands=wide_commands)
            return h.tools(wide_task["call"])

        rig.p.on(user_text("f5-wide-task"), manager_wide, role="manager", once=True)
        rig.p.on(lambda r: task_injected(r) and r.injected.get("task_id") == wide_task.get("tid"),
                 "Task received; waiting", role="worker")
        wide = Chain(rig, "worker", user_text("f5-wide also edit src/wide.txt"), [
            lambda r, c: rig.terminal("echo F5_WIDE > src/wide.txt"),
            lambda r, c: rig.to_manager(kind="report", message="F5: the user asked for src/wide.txt, outside this "
                                        "Task; not done", task_id=wide_task["tid"],
                                        request={"goal": "edit src/wide.txt for the user", "paths": ["src/"]}),
            lambda r, c: "F5 reported the wider request",
        ], "f5-wide")
        rig.step("start", lambda: rig.start() and {})

        def no_task_small():
            assert rig.status().get("task") is None
            rig.say("worker_omp", "f5-small please")
            assert small.wait(60), small.results
            result = small.results[0]
            assert result["status"] == "exited" and result["exit_code"] == 0, result
            assert rig.host_file("f5-small.txt").read_text() == "F5_SMALL\n"
            started = [j for j in rig.journal() if j.get("type") == "terminal_started"
                       and j.get("command") == "echo F5_SMALL > f5-small.txt"]
            assert started and started[-1].get("task_id") is None, started
            assert rig.status().get("task") is None, "a Task was created for a small direct request"
            assert not rig.injected("manager"), "the manager was involved"
            return {"result": result["status"], "task_id_of_run": None, "task": None}

        def inside_open_task():
            rig.say("manager_omp", "f5-open")
            assert rig.pump_until(lambda: "call" in open_task and rig.result(open_task["call"]), 60)
            open_task["tid"] = rig.result(open_task["call"])["task_id"]
            rig.wait_injected("worker", lambda i: i.get("kind") == "task" and i.get("task_id") == open_task["tid"],
                              60, "F5 Task delivered")
            rig.pump(1.0)
            rig.say("worker_omp", "f5-in add notes/in.txt")
            assert inside.wait(60), inside.results
            ran, progress, done, = inside.results
            assert ran["status"] == "exited" and rig.host_file("notes/in.txt").read_text() == "F5_IN\n", ran
            started = [j for j in rig.journal() if j.get("type") == "terminal_started"
                       and "F5_IN" in (j.get("command") or "")]
            assert started[-1].get("task_id") == open_task["tid"], started
            assert progress["status"] == "queued" and progress["task_id"] == open_task["tid"], progress
            got = rig.wait_injected("manager", lambda i: i.get("task_id") == open_task["tid"]
                                    and (i.get("payload") or {}).get("kind") == "progress", 30, "F5 progress")
            closed = rig.wait_task(lambda t: t.get("task_id") == open_task["tid"] and t["status"] == "closed", 60,
                                   "F5 open Task done")
            return {"run_task_id": started[-1].get("task_id"), "report_task_id": got["task_id"],
                    "closed_reason": closed["closed_reason"]}

        def beyond_scope():
            rig.say("manager_omp", "f5-wide-task")
            assert rig.pump_until(lambda: "call" in wide_task and rig.result(wide_task["call"]), 60)
            dispatched = rig.result(wide_task["call"])
            assert dispatched["status"] == "dispatched", dispatched
            wide_task["tid"] = dispatched["task_id"]
            rig.wait_injected("worker", lambda i: i.get("kind") == "task" and i.get("task_id") == wide_task["tid"],
                              60, "F5 wide Task delivered")
            before = rig.task()
            rig.pump(1.0)
            rig.say("worker_omp", "f5-wide also edit src/wide.txt")
            assert wide.wait(60), wide.results
            refused, reported = wide.results
            assert refused["status"] == "not_in_task_commands", refused
            assert not rig.host_file("src/wide.txt").exists(), "the worker widened the Task"
            assert reported["status"] == "queued", reported
            got = rig.wait_injected("manager", lambda i: i.get("task_id") == wide_task["tid"]
                                    and isinstance((i.get("payload") or {}).get("request"), dict), 30, "F5 request")
            request = got["payload"]["request"]
            assert request["classification"] == "scope_expansion_manager_confirmation", request
            assert request["dispatch_authorized"] is False, request
            after = rig.task()
            assert (after["task_id"], after["revision"], after["active"]) == (before["task_id"], before["revision"],
                                                                             True), (before, after)
            tasks = rig.injected("worker", lambda i: i.get("kind") == "task")
            return {"refused": refused["status"], "request": request, "task_unchanged": True,
                    "tasks_delivered_total": len(tasks)}

        rig.step("F5_no_task_unrestricted", no_task_small, ("start",))
        rig.step("F5_inside_open_task_linked", inside_open_task, ("start",))
        rig.step("F5_beyond_scope_reported_not_widened", beyond_scope, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        rig.note("C-D72 (3): the scripted worker plays the model's choice; checked are the product's answers "
                 "(unrestricted run without a Task, run/report linkage to the open Task, not_in_task_commands, "
                 "the scope_expansion classification delivered to the manager with dispatch_authorized=false).")
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- F6 + F7
    def test_f7_idle_then_f6_code_change_reports_end_instruction(self):
        rig = Rig(self, "flow-F6-F7")
        tasks: dict = {}

        def dispatcher(trigger, key):
            def respond(r):
                tasks[key] = rig.to_worker(kind="work", message=f"F6 {key}", spec=spec(f"F6 {key}", ["notes/"]))
                return h.tools(tasks[key])
            rig.p.on(user_text(trigger), respond, role="manager", once=True)

        dispatcher("f6-a", "a")
        dispatcher("f6-b", "b")
        Chain(rig, "worker", lambda r: task_injected(r) and r.injected.get("payload", {}).get("message") == "F6 a", [
            lambda r, c: rig.to_manager(kind="done", message="F6a: the result needs a code change in src/",
                                        task_id=r.injected["task_id"], requires_code_change=True,
                                        reason="the parser in src/ must change"),
            lambda r, c: "F6a reported",
        ], "f6a-worker")
        Chain(rig, "worker", lambda r: task_injected(r) and r.injected.get("payload", {}).get("message") == "F6 b", [
            lambda r, c: rig.to_manager(kind="blocked", message="F6b: blocked, a code change is needed",
                                        task_id=r.injected["task_id"], requires_code_change=True,
                                        reason="src/ change needed before continuing"),
            lambda r, c: "F6b reported",
        ], "f6b-worker")
        follow: dict = {}

        def follow_up(r):
            follow["call"] = rig.to_worker(kind="work", message="F6 follow-up: continue after the change",
                                           task_id=rig.result(tasks["b"])["task_id"])
            return h.tools(follow["call"])

        rig.p.on(user_text("f6-c"), follow_up, role="manager", once=True)
        rig.step("start", lambda: rig.start() and {})

        def f7():
            window = quiet_window(rig, 65, role="worker")
            snap = rig.status()
            shell = snap["panes"]["host_shell"]["shell"]
            assert window["requests"] == 0 and not window["injected"], window
            assert rig.p.count("manager") == 0, rig.p.count("manager")
            assert window["automation_states"] == ["idle"], window
            assert snap.get("task") is None and snap["automation"]["run"] is None, snap.get("task")
            assert (shell["input_owner"], shell["parent_mode"]) == ("user", "manual_prompt"), shell
            terminal_runs = [j for j in rig.journal() if j.get("type") == "terminal_started"]
            runs = list((rig.sb.data / "workflow" / "runs").glob("*")) if (rig.sb.data / "workflow" / "runs").exists() else []
            assert not terminal_runs and not runs, (terminal_runs, runs)
            return {"seconds": 65, "automation": window["automation_states"], "worker_requests": 0,
                    "manager_requests": 0, "runs": 0}

        def f6(key, expected_status):
            def step():
                rig.say("manager_omp", f"f6-{key}")
                assert rig.pump_until(lambda: key in tasks and rig.result(tasks[key]), 60)
                tid = rig.result(tasks[key])["task_id"]
                report = rig.wait_injected("manager", lambda i: i.get("task_id") == tid
                                           and (i.get("payload") or {}).get("requires_code_change") is True, 60,
                                           f"F6{key} report")
                task = rig.wait_task(lambda t: t.get("task_id") == tid and t["status"] == expected_status, 60,
                                     f"F6{key} {expected_status}")
                snap = rig.status()
                assert snap["worker"]["state"] == "idle", snap["worker"]
                window = quiet_window(rig, 62, role="worker")
                assert window["requests"] == 0 and not window["injected"], window
                after = rig.task()
                assert after["status"] == expected_status, after
                return {"task_id": tid, "status": expected_status, "closed_reason": task.get("closed_reason"),
                        "held_reason": task.get("held_reason"),
                        "report": {k: report["payload"].get(k) for k in ("kind", "requires_code_change", "reason")},
                        "quiet_seconds": 62, "worker_deliveries_meanwhile": 0}
            return step

        def new_instruction():
            before = len(rig.injected("worker"))
            rig.say("manager_omp", "f6-c")
            assert rig.pump_until(lambda: "call" in follow and rig.result(follow["call"]), 60)
            result = rig.result(follow["call"])
            assert result["status"] == "queued", result
            rig.pump_until(lambda: len(rig.injected("worker")) > before, 60)
            rig.pump(5)
            new = rig.injected("worker")[before:]
            assert len(new) == 1 and new[0]["kind"] == "question", [(i.get("kind"), i.get("payload")) for i in new]
            return {"follow_up": result["status"], "delivered_to_worker": len(new),
                    "task": {k: rig.task().get(k) for k in ("status", "held_reason")}}

        rig.step("F7_idle_before_instruction", f7, ("start",))
        rig.step("F6a_done_requires_code_change_closes", f6("a", "closed"), ("start",))
        rig.step("F6b_blocked_requires_code_change_no_auto", f6("b", "blocked"), ("start",))
        rig.step("F6_new_instruction_delivered_once", new_instruction, ("F6b_blocked_requires_code_change_no_auto",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- F45
    def test_f45_worker_terminal_with_undelivered_task_and_after_cancel(self):
        """CW-19 VM observation: worker ``terminal`` gave no result for ~45 s while an unaccepted Task was open or
        right after a cancel. With the real OMP the same conditions must answer at once."""
        rig = Rig(self, "flow-F45")
        go_on = threading.Event()
        timing: dict = {}

        def a(r, c):
            return rig.terminal("sleep 8; echo F45_A")

        def b(r, c):
            go_on.wait(60)  # the Task is dispatched (still undelivered: this worker turn is in progress)
            timing["b_sent"] = time.time()
            timing["task_delivered_before_b"] = bool(rig.injected("worker", lambda i: i.get("kind") == "task"))
            return rig.terminal("echo F45_B")

        direct = Chain(rig, "worker", user_text("f45-direct"), [a, b, lambda r, c: "F45 direct done"], "f45-direct")
        task: dict = {}

        def dispatch(r):
            task["call"] = rig.to_worker(kind="work", message="F45 work", spec=spec("F45", ["notes/"]))
            return h.tools(task["call"])

        def cancel(r):
            task["cancel"] = rig.to_worker(kind="work", message="F45 cancel", task_id=task["tid"], cancel=True)
            return h.tools(task["cancel"])

        rig.p.on(user_text("f45-task"), dispatch, role="manager", once=True)
        rig.p.on(user_text("f45-cancel"), cancel, role="manager", once=True)
        rig.p.on(task_injected, "F45 Task received", role="worker", once=True)
        after_cancel = Chain(rig, "worker", lambda r: r.injected is not None
                             and (r.injected.get("payload") or {}).get("cancel") is True, [
            lambda r, c: (timing.__setitem__("c_sent", time.time()), rig.terminal("echo F45_C"))[1],
            lambda r, c: "F45 after cancel done",
        ], "f45-after-cancel")
        rig.step("start", lambda: rig.start() and {})

        def undelivered():
            rig.say("worker_omp", "f45-direct")
            assert rig.pump_until(lambda: any(j.get("type") == "terminal_started" and "F45_A" in (j.get("command") or "")
                                              for j in rig.journal()), 30), "first command did not start"
            rig.say("manager_omp", "f45-task")
            assert rig.pump_until(lambda: "call" in task and rig.result(task["call"]), 30)
            dispatched = rig.result(task["call"])
            assert dispatched["status"] == "dispatched", dispatched
            task["tid"] = dispatched["task_id"]
            go_on.set()
            assert direct.wait(60), direct.results
            first, second = direct.results
            latency = rig.result_at[direct.calls[1][2]] - timing["b_sent"]
            assert second["status"] == "exited" and second["exit_code"] == 0, second
            assert latency < 10, latency
            assert timing["task_delivered_before_b"] is False, "the Task was already delivered (no repro condition)"
            delivered = rig.wait_injected("worker", lambda i: i.get("kind") == "task", 60, "Task after the turn")
            return {"first": first["status"], "second": second["status"], "second_latency_s": round(latency, 2),
                    "task_undelivered_at_second_call": True,
                    "task_delivered_after_turn_at": delivered["at"]}

        def after_cancel_step():
            rig.say("manager_omp", "f45-cancel")
            assert rig.pump_until(lambda: "cancel" in task and rig.result(task["cancel"]), 30)
            result = rig.result(task["cancel"])
            assert result["status"] == "cancelled", result
            assert after_cancel.wait(60), after_cancel.results
            latency = rig.result_at[after_cancel.calls[0][2]] - timing["c_sent"]
            third = after_cancel.results[0]
            assert third["status"] == "exited" and third["exit_code"] == 0, third
            assert latency < 10, latency
            return {"cancel": result["status"], "after_cancel": third["status"], "latency_s": round(latency, 2)}

        rig.step("F45_terminal_while_task_undelivered", undelivered, ("start",))
        rig.step("F45_terminal_right_after_cancel", after_cancel_step, ("F45_terminal_while_task_undelivered",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)


@unittest.skipIf(SKIP, SKIP or "")
class Cw16PolicyScenarios(unittest.TestCase):
    """I-POLICY: P1-P8 (P7 with P1)."""

    def setUp(self):
        name = self._testMethodName.split("_")[1].upper()
        if not fr.selected(name):
            self.skipTest(f"filtered out by WB_CW16_SCENARIOS ({name})")

    def _reviews(self, rig: Rig, after: float = 0.0) -> list[dict]:
        return rig.injected("worker", lambda i: stage_of(i) == "periodic_review" and i["at"] >= after)

    # ------------------------------------------------------------------------------------------- P1 + P7
    def test_p1_periodic_reviews_merge_and_p7_usage(self):
        rig = Rig(self, "policy-P1-P7")
        contract_worker(rig)
        holder = exp_task(rig, "p1-go", "P1 silent run", "mkdir -p out; sleep 200; echo P1_DONE; printf PASS > out/p1.txt",
                          log="P1_DONE", result_file="out/p1.txt")
        busy = {}

        def slow_turn(r):
            busy["start"] = time.time()
            # 45 pieces x 2 s: a ~90 s turn. Non-repetitive on purpose: OMP 18.8.0 cuts a streaming turn that
            # repeats itself (observed: it aborted "P1-busy-" x 90 after ~30 s and asked for a final answer).
            words = ["amber", "basalt", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper",
                     "kelp", "lagoon", "mesa", "nectar", "onyx", "prairie", "quartz", "river", "sierra", "tundra",
                     "umber", "valley", "willow", "xenon", "yarrow", "zephyr"]
            body = " ".join(f"{words[(i * 7) % 26]}-{i}" for i in range(180))
            return h.text(f"P1 notes: {body}.", chunks=45, chunk_gap=2.0)

        rig.p.on(user_text("p1-busy"), slow_turn, role="worker", once=True)
        rig.p.on(lambda r: stage_of(r.injected) == "periodic_review", "P1 review: looks healthy", role="worker")
        seen: dict = {"review_states": []}
        rig.step("start", lambda: rig.start() and {})

        def p1():
            rig.say("manager_omp", "p1-go")
            assert rig.pump_until(lambda: "call" in holder and rig.result(holder["call"]), 60)
            active = rig.wait(lambda s: s["automation"]["state"] == "active" and (s["automation"]["run"] or {}).get("bound"),
                              60, "active run")
            t0 = time.time()
            seen["t0"] = t0
            first = rig.wait_injected("worker", lambda i: stage_of(i) == "periodic_review", 90, "1st review")
            first_after = first["at"] - t0
            assert 50 <= first_after <= 80, first_after
            rig.pump_until(lambda: time.time() - t0 >= 95, 60)
            rig.say("worker_omp", "p1-busy")
            assert rig.pump_until(lambda: "start" in busy, 20), "worker turn did not start"
            busy_start = busy["start"]
            # the worker OMP turn streams ~90 s: reviews due meanwhile are delayed and merged
            while time.time() - busy_start < 85:
                snap = rig.sb.status()
                if snap:
                    review = snap["automation"]["review"]
                    seen["review_states"].append({k: review.get(k) for k in ("status", "reason", "pending",
                                                                            "coalesced_count", "review_count")})
                rig.pump(3)
            during = [i for i in self._reviews(rig, busy_start + 1)]
            assert rig.pump_until(lambda: bool(self._reviews(rig, busy_start + 1)), 60), "no review after the turn"
            after = self._reviews(rig, busy_start + 1)
            merged_review = after[0]
            merged = merged_review["payload"].get("coalesced_count", 0)
            timeline = [(round(i["at"] - t0, 1), i["payload"].get("coalesced_count")) for i in self._reviews(rig)]
            assert not during, {"reviews_inside_the_busy_turn": timeline, "busy_start": round(busy_start - t0, 1)}
            assert merged_review["at"] - t0 > 175, timeline  # the 120 s and 180 s reviews waited for the turn
            assert merged >= 1, {"timeline": timeline, "payload": merged_review["payload"].get("coalesced_count")}
            delayed = [s for s in seen["review_states"] if s.get("status") not in ("dispatched", "waiting", None)
                       or s.get("pending") or (s.get("coalesced_count") or 0) > 0]
            assert delayed, seen["review_states"][-5:]
            task = rig.wait_task(lambda t: t["status"] in ("finished", "closed"), 200, "P1 finished")
            assert task["last_result"]["judgment"] == "success", task
            facts = first["payload"]["facts"]
            return {"first_review_after_s": round(first_after, 1), "merged_review_coalesced": merged,
                    "delayed_states_seen": delayed[:3], "review_timeline_s": timeline,
                    "hang_classification": facts["hang_investigation"]["classification"],
                    "judgment": "success", "active_run": active["automation"]["run"]}

        def p7():
            reviews = self._reviews(rig)
            assert reviews, "no periodic review"
            usage = reviews[0]["payload"]["facts"].get("usage") or {}
            assert usage and all(usage.get(k) == "unknown" for k in ("tokens_observed", "tokens_estimated")), usage
            snap = rig.status()
            text = json.dumps(snap, ensure_ascii=False)
            # Workbench's own status lines (OMP panes draw their own footers; those are OMP's, not Workbench's)
            own = [line for line in rig.ui.lines() if re.search(r"자동화|작업:|backend:|focus:", line)]
            omp_lines = [line.strip()[:160] for line in rig.ui.lines() if re.search(r"usage|사용량|tokens?", line, re.I)
                         and line not in own]
            shown = {"status_has_usage": "usage" in text or "사용량" in text,
                     "ui_has_usage": any(re.search(r"usage|사용량", line, re.I) for line in own),
                     "workbench_status_lines": [line.strip()[:200] for line in own],
                     "omp_pane_lines_mentioning_usage": omp_lines[:6]}
            assert shown["status_has_usage"] or shown["ui_has_usage"], \
                {"review_facts_usage": usage, **shown, "C-AC-21": "usage display (unknown not shown as a value)",
                 "status_line": rig.ui.excerpt("자동화", "작업:")}
            return {"review_facts_usage": usage, **shown}

        rig.step("P1_reviews_60s_merge_when_busy", p1, ("start",))
        rig.step("P7_usage_in_review_facts_and_display", p7, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- P2
    def test_p2_pause_holds_automatic_work(self):
        rig = Rig(self, "policy-P2")
        contract_worker(rig)
        loop = ("mkdir -p out; i=0; while [ $i -lt 45 ]; do i=$((i+1)); echo P2_TICK $i; sleep 2; done; "
                "echo P2_DONE; printf PASS > out/p2.txt")
        holder = exp_task(rig, "p2-go", "P2 ticking run", loop, log="P2_DONE", result_file="out/p2.txt")
        sent: dict = {}

        def paused_send(r):
            sent["call"] = rig.to_worker(kind="work", message="P2 while paused", spec=spec("P2 paused", ["notes/"]))
            return h.tools(sent["call"])

        rig.p.on(user_text("p2-send"), paused_send, role="manager", once=True)

        def paused_cancel(r):
            sent["cancel"] = rig.to_worker(kind="experiment", message="P2 cancel", task_id=ctx["tid"], cancel=True)
            return h.tools(sent["cancel"])

        rig.p.on(user_text("p2-cancel"), paused_cancel, role="manager", once=True)
        term = Chain(rig, "worker", user_text("p2-term"), [lambda r, c: rig.terminal("echo P2_TERM"),
                                                           lambda r, c: "P2 term answered"], "p2-term")
        rig.p.on(lambda r: stage_of(r.injected) == "periodic_review", "P2 review ok", role="worker")
        ctx: dict = {}
        rig.step("start", lambda: rig.start() and {})

        def paused():
            rig.say("manager_omp", "p2-go")
            assert rig.pump_until(lambda: "call" in holder and rig.result(holder["call"]), 60)
            tid = rig.result(holder["call"])["task_id"]
            snap = rig.wait(lambda s: (s.get("task") or {}).get("run_id") and s["automation"]["state"] == "active",
                            60, "run active")
            run_id = snap["task"]["run_id"]
            ctx.update(tid=tid, run_id=run_id)
            log = raw_log(rig, run_id)
            assert rig.pump_until(lambda: log.exists() and b"P2_TICK 3" in log.read_bytes(), 30), "no ticks"
            snap = rig.pause()
            pause_at = time.time()
            ctx["pause_at"] = pause_at
            assert snap["automation"]["paused"] is True
            assert rig.ui.wait_text(r"일시정지됨", 10), rig.ui.excerpt("자동화")
            rig.say("manager_omp", "p2-send")
            assert rig.pump_until(lambda: "call" in sent and rig.result(sent["call"]), 60)
            held = rig.result(sent["call"])
            assert held == {"status": "held", "reason": "paused"}, held
            rig.say("worker_omp", "p2-term")
            assert term.wait(60), term.results
            assert term.results[0]["status"] == "paused", term.results[0]
            size1 = log.stat().st_size
            rig.pump(6)
            size2 = log.stat().st_size
            assert size2 > size1, (size1, size2)
            ctx["sizes"] = (size1, size2)
            return {"to_worker": held, "terminal": term.results[0]["status"], "raw_log_growing": [size1, size2],
                    "manual_input": "user prompts to both OMP panes were accepted (manager turn ran, worker turn ran)",
                    "ui": rig.ui.excerpt("일시정지")}

        def end_while_paused():
            log = raw_log(rig, ctx["run_id"])
            assert rig.pump_until(lambda: b"P2_DONE" in log.read_bytes(), 120), "the run did not end"
            done_at = time.time()
            shown = rig.wait(lambda s: (s.get("task") or {}).get("status") == "waiting_report"
                             and s["task"].get("held_reason") == "paused", 20, "the run's end shown while paused")
            assert rig.ui.wait_text(r"보고 대기", 10), rig.ui.excerpt("작업:")
            shown_after = time.time() - done_at
            reviews = [i for i in rig.injected("worker", lambda i: stage_of(i) == "periodic_review")
                       if i["at"] > ctx["pause_at"]]
            assert not reviews, f"{len(reviews)} reviews during the pause"
            assert time.time() - ctx["pause_at"] > 65, "the pause did not cover a review interval"
            analysis = [i for i in rig.injected("worker", lambda i: stage_of(i) == "analysis")]
            assert not analysis, "a judgment question was sent while paused"
            return {"end_shown_within_s": round(shown_after, 1), "review_exit": shown["automation"]["review"].get("exit"),
                    "task": {k: shown["task"].get(k) for k in ("status", "held_reason", "last_result")},
                    "reviews_while_paused": 0, "paused_for_s": round(time.time() - ctx["pause_at"], 1),
                    "ui": rig.ui.excerpt("작업:", "자동화")}

        def resumed():
            tasks_before = rig.task().get("task_id")
            rig.ui_key(b"p")
            assert rig.ui.wait_text(r"대조 후 재개합니다", 10), rig.ui.excerpt("자동화")
            rig.ui.send(b"p", settle=0.5)
            snap = rig.wait(lambda s: s["automation"]["paused"] is False or (s["automation"].get("resume") or {})
                            .get("outcome") == "refused", 60, "resume outcome")
            if snap["automation"]["paused"]:  # D-B2-2 repro: record a second attempt and the ways out
                first = snap["automation"]["resume"]
                rig.pump(5)
                rig.ui_key(b"p", confirm=b"p")
                rig.pump(10)
                second = rig.status()["automation"]["resume"]
                rig.say("manager_omp", "p2-cancel")
                cancel = rig.pump_until(lambda: "cancel" in sent and rig.result(sent["cancel"]), 30) \
                    and rig.result(sent["cancel"])
                raise AssertionError({"defect": "resume refused after the run ended while paused; Workbench stays "
                                                "paused (Task waiting_report/paused)", "first": first,
                                      "second": second, "manager_cancel_while_paused": cancel,
                                      "task": {k: rig.task().get(k) for k in ("status", "held_reason")},
                                      "ui": rig.ui.excerpt("재개", "일시정지")})
            assert snap["automation"]["resume"]["outcome"] == "resumed", snap["automation"]["resume"]
            rig.pump(8)
            after = rig.status()
            assert (after.get("task") or {}).get("task_id") == tasks_before, "a held to_worker was replayed"
            delivered = rig.injected("worker", lambda i: (i.get("payload") or {}).get("message") == "P2 while paused")
            assert not delivered, delivered
            final = rig.wait_task(lambda t: t["status"] in ("finished", "closed"), 90, "judged after resume")
            return {"resume": snap["automation"]["resume"], "replayed": 0, "after_resume_task": final}

        rig.step("P2_pause_holds_sends_and_terminal", paused, ("start",))
        rig.step("P2_run_end_shown_no_review_while_paused", end_while_paused, ("P2_pause_holds_sends_and_terminal",))
        rig.step("P2_resume_reconciled_no_replay", resumed, ("P2_pause_holds_sends_and_terminal",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- P3
    def test_p3_pause_interrupts_manager_turn(self):
        rig = Rig(self, "policy-P3")
        contract_worker(rig)
        holder = exp_task(rig, "p3-go", "P3 long run", "mkdir -p out; sleep 100; echo P3_DONE; printf PASS > out/p3.txt",
                          log="P3_DONE", result_file="out/p3.txt")
        rig.p.on(user_text("p3-slow"), h.text("P3 manager is thinking aloud " * 20, chunks=60, chunk_gap=0.5),
                 role="manager", once=True)
        tool_call: dict = {}

        def tool(r):
            tool_call["call"] = rig.p.call("manager", "bash", {"command": "sleep 30"})
            return h.tools(tool_call["call"])

        rig.p.on(user_text("p3-tool"), tool, role="manager", once=True)
        rig.p.on(lambda r: stage_of(r.injected) == "periodic_review", "P3 review ok", role="worker")
        rig.step("start", lambda: rig.start() and {})

        def interrupt_stream():
            rig.say("manager_omp", "p3-go")
            assert rig.pump_until(lambda: "call" in holder and rig.result(holder["call"]), 60)
            rig.wait(lambda s: (s["automation"]["run"] or {}).get("bound"), 60, "bound run")
            before = rig.p.count("manager")
            rig.say("manager_omp", "p3-slow")
            assert rig.p.wait_count("manager", before + 1, 20)
            rig.pump(2)
            snap = rig.pause()
            snap = rig.wait(lambda s: s["automation"]["interruption"]["state"] in ("confirmed", "unknown",
                                                                                 "request_failed"), 30, "interrupt")
            interruption = snap["automation"]["interruption"]
            assert interruption["state"] == "confirmed" and interruption["manager_ack"] == "abort_requested", interruption
            aborted = [a for a in rig.p.snapshot()["aborted"] if a["role"] == "manager"]
            assert aborted, "the manager stream was not aborted"
            assert rig.ui.wait_text(r"중단 확인됨", 10), rig.ui.excerpt("일시정지", "중단")
            count = rig.p.count("manager")
            resumed = rig.resume()
            assert resumed["automation"]["resume"]["outcome"] == "resumed", resumed["automation"]["resume"]
            rig.pump(8)
            assert rig.p.count("manager") == count, "the aborted manager turn was re-run after the resume"
            return {"interruption": interruption, "aborted_streams": aborted, "ui": rig.ui.excerpt("중단"),
                    "resume": resumed["automation"]["resume"], "manager_requests_after_resume": 0}

        def unknown_tool():
            rig.say("manager_omp", "p3-tool")
            assert rig.pump_until(lambda: "call" in tool_call, 20)
            rig.pump(2)
            rig.pause()
            snap = rig.wait(lambda s: s["automation"]["interruption"]["state"] in ("confirmed", "unknown",
                                                                                 "request_failed"), 30, "interrupt 2")
            interruption = snap["automation"]["interruption"]
            assert tool_call["call"][2] in interruption["unknown_tool_call_ids"], interruption
            resumed = rig.resume()
            outcome = resumed["automation"]["resume"]
            return {"interruption": interruption, "resume": outcome,
                    "ui": rig.ui.excerpt("중단", "재개")}

        rig.step("P3_pause_aborts_streaming_manager_turn", interrupt_stream, ("start",))
        rig.step("P3_running_manager_tool_recorded_unknown", unknown_tool, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- P4
    def test_p4_retry_limit_and_revision_change(self):
        rig = Rig(self, "policy-P4")
        contract_worker(rig)
        command = "mkdir -p out; echo P4_RUN; printf FAIL > out/p4.txt; exit 1"
        rev2 = "mkdir -p out; echo P4_RUN P4_REV2; printf FAIL > out/p4.txt; exit 1"
        holder = exp_task(rig, "p4-go", "P4 failing run", command, log="P4_OK", result_file="out/p4.txt")
        reruns: list = []
        state = {"reports": 0}

        def on_report(r):
            tid = r.injected.get("task_id")
            state["reports"] += 1
            n = state["reports"]
            if n == 2:  # the 2nd re-run with a spec change: a new revision for the next run
                call = rig.to_worker(kind="experiment", message="P4 re-run with the changed command", task_id=tid,
                                     run=True, spec=spec("P4 failing run", ["out/"], execution(
                                         rig.sb.project, rig.sb.commit, rev2, log="P4_OK", result_file="out/p4.txt")))
            else:
                call = rig.to_worker(kind="experiment", message=f"P4 re-run {n}", task_id=tid, run=True)
            reruns.append(call)
            return h.tools(call)

        rig.p.on(lambda r: r.injected is not None and r.injected.get("kind") == "report", on_report, role="manager")
        rig.step("start", lambda: rig.start() and {})

        def retries():
            rig.say("manager_omp", "p4-go")
            assert rig.pump_until(lambda: "call" in holder and rig.result(holder["call"]), 60)
            tid = rig.result(holder["call"])["task_id"]
            assert rig.pump_until(lambda: len(reruns) >= 4 and rig.result(reruns[3]) is not None, 400), \
                [rig.result(c) for c in reruns]
            results = [rig.result(c) for c in reruns]
            assert [r["status"] for r in results] == ["dispatched", "dispatched", "dispatched", "held"], results
            assert [r.get("retry") for r in results[:3]] == [1, 2, 3], results
            assert results[3]["reason"] == "retry_limit" and results[3]["runs_started"] == 4, results[3]
            rig.pump(10)
            task = rig.task()
            assert task["task_id"] == tid and task["runs_started"] == 4 and task["revision"] == 2, task
            runs = sorted((rig.sb.data / "workflow" / "runs").glob("*/run.json"),
                          key=lambda p: p.stat().st_mtime)
            records = [json.loads(p.read_text()) for p in runs]
            records = [rec for rec in records if rec.get("task_id") == tid]
            revisions = [rec.get("revision") for rec in records]
            assert revisions == [1, 1, 2, 2], revisions
            last_log = raw_log(rig, task["last_result"]["run_id"]).read_text(errors="replace")
            assert "P4_REV2" in last_log, last_log[-200:]
            return {"rerun_results": [{k: r.get(k) for k in ("status", "retry", "reason", "runs_started", "revision")}
                                      for r in results], "runs": len(records), "revisions": revisions,
                    "task": {k: task.get(k) for k in ("runs_started", "revision", "status", "last_result")},
                    "note": "after the limit the product holds re-runs (held:retry_limit); every earlier run's "
                            "judgment reached the manager as a report"}

        rig.step("P4_three_reruns_then_retry_limit", retries, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- P5
    def test_p5_change_during_run_not_applied(self):
        rig = Rig(self, "policy-P5")
        contract_worker(rig)
        holder = exp_task(rig, "p5-go", "P5 run", "mkdir -p out; sleep 25; echo P5_OK; printf PASS > out/p5.txt",
                          log="P5_OK", result_file="out/p5.txt")
        change: dict = {}

        def change_spec(r):
            change["call"] = rig.to_worker(kind="experiment", message="P5 change the command now", task_id=change["tid"],
                                           run=True, spec=spec("P5 run", ["out/"], execution(
                                               rig.sb.project, rig.sb.commit, "echo P5_CHANGED; printf PASS > out/p5.txt",
                                               log="P5_CHANGED", result_file="out/p5.txt")))
            return h.tools(change["call"])

        rig.p.on(user_text("p5-change"), change_spec, role="manager", once=True)
        rig.step("start", lambda: rig.start() and {})

        def during():
            rig.say("manager_omp", "p5-go")
            assert rig.pump_until(lambda: "call" in holder and rig.result(holder["call"]), 60)
            change["tid"] = rig.result(holder["call"])["task_id"]
            snap = rig.wait(lambda s: (s["automation"]["run"] or {}).get("bound"), 60, "bound run")
            run_id = snap["automation"]["run"]["run_id"]
            rig.say("manager_omp", "p5-change")
            assert rig.pump_until(lambda: "call" in change and rig.result(change["call"]), 60)
            result = rig.result(change["call"])
            assert result["status"] == "worker_busy", result
            task = rig.wait_task(lambda t: t["status"] in ("finished", "closed"), 90, "P5 finished")
            log = raw_log(rig, run_id).read_text(errors="replace")
            assert task["revision"] == 1 and task["runs_started"] == 1, task
            assert "P5_OK" in log and "P5_CHANGED" not in log, log[-200:]
            assert task["last_result"]["judgment"] == "success", task
            return {"change_result": result["status"], "run_revision": 1, "runs_started": 1,
                    "task": {k: task.get(k) for k in ("status", "revision", "runs_started")}}

        rig.step("P5_change_during_run_held", during, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- P6
    def test_p6_restart_worker_stop_survivor_kill_host(self):
        rig = Rig(self, "policy-P6")
        task: dict = {}

        def dispatch(r):
            task["call"] = rig.to_worker(kind="work", message="P6 open task", spec=spec("P6", ["notes/"]))
            return h.tools(task["call"])

        rig.p.on(user_text("p6-task"), dispatch, role="manager", once=True)
        rig.p.on(task_injected, "P6 Task received", role="worker")
        calls: dict = {}

        def restart(r):
            calls["restart"] = rig.p.call("manager", "restart_worker", {"reason": "cw16 p6: verify the forced restart"})
            return h.tools(calls["restart"])

        def survivor(r):
            calls["survivor"] = rig.p.call("manager", "stop_survivor", {"survivor_id": "nosuch0000",
                                                                       "reason": "cw16 p6 unknown id"})
            return h.tools(calls["survivor"])

        rig.p.on(user_text("p6-restart"), restart, role="manager", once=True)
        rig.p.on(user_text("p6-survivor"), survivor, role="manager", once=True)
        rig.step("start", lambda: rig.start() and {})

        def restart_worker():
            rig.say("manager_omp", "p6-task")
            assert rig.pump_until(lambda: "call" in task and rig.result(task["call"]), 60)
            tid = rig.result(task["call"])["task_id"]
            rig.wait_injected("worker", lambda i: i.get("kind") == "task", 60, "P6 Task delivered")
            before = rig.status()
            old = before["panes"]["worker_omp"]["process"]
            rig.say("manager_omp", "p6-restart")
            assert rig.pump_until(lambda: "restart" in calls and rig.result(calls["restart"]), 90)
            result = rig.result(calls["restart"])
            assert result["status"] == "restarted", result
            assert not h.alive(old["pid"], old["start_ticks"]), "the old worker OMP is still alive"
            after = rig.wait(lambda s: (s.get("bridge") or {}).get("worker")
                             and s["panes"]["worker_omp"]["process"]["pid"] != old["pid"], 60, "new worker")
            new = after["panes"]["worker_omp"]["process"]
            manager_pid = before["panes"]["manager_omp"]["process"]
            assert after["panes"]["manager_omp"]["process"] == manager_pid, "the manager OMP changed"
            assert after["panes"]["host_shell"]["process"] == before["panes"]["host_shell"]["process"]
            assert (after.get("task") or {}).get("task_id") == tid and after["task"]["status"] != "closed", after["task"]
            notice = rig.pump_until(lambda: any("worker_restarted" in json.dumps(i) for i in rig.injected("manager")), 30)
            return {"result": {k: result.get(k) for k in ("status", "reason", "worker", "previous")},
                    "old_worker_dead": True, "new_worker": new, "task_kept": after["task"]["status"],
                    "worker_restarted_notice_to_manager": notice}

        def stop_survivor():
            rig.say("manager_omp", "p6-survivor")
            assert rig.pump_until(lambda: "survivor" in calls and rig.result(calls["survivor"]), 60)
            result = rig.result(calls["survivor"])
            assert result["status"] == "refused" and result["reason"] == "unknown_survivor", result
            return {"result": result}

        def kill_host():
            before = rig.status()["panes"]["host_shell"]
            proc = before["process"]
            rig.focus("host_shell")
            rig.ui_key(b"k")
            assert rig.ui.wait_text(r"host terminal 강제 종료 확인", 10), rig.ui.excerpt("host terminal")
            rig.ui.send(b"k", settle=0.5)
            assert rig.ui.wait_text(r"host terminal 강제 종료됨", 30), rig.ui.excerpt("host terminal")
            assert rig.pump_until(lambda: not h.alive(proc["pid"], proc["start_ticks"]), 15), "shell still alive"
            rig.ui.send(b"\r", settle=0.5)
            after = rig.wait(lambda s: s["panes"]["host_shell"].get("alive")
                             and s["panes"]["host_shell"]["process"]["pid"] != proc["pid"]
                             and s["panes"]["host_shell"]["shell"]["parent_mode"] == "manual_prompt", 60, "new shell")
            rig.ui.type("echo P6_NEW_SHELL > p6-new.txt", gap=0.01)
            rig.ui.send(b"\r", settle=0.3)
            assert h.wait_file(rig.host_file("p6-new.txt"), contains="P6_NEW_SHELL", pump=[rig.ui]), "new shell input"
            return {"killed": proc, "new_shell": after["panes"]["host_shell"]["process"],
                    "generation": [before.get("generation"), after["panes"]["host_shell"].get("generation")],
                    "ui": rig.ui.excerpt("host terminal")}

        rig.step("P6_restart_worker_verified", restart_worker, ("start",))
        rig.step("P6_stop_survivor_unknown_refused", stop_survivor, ("start",))
        rig.step("P6_host_terminal_force_kill", kill_host, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    # ------------------------------------------------------------------------------------------- P8
    def test_p8_cancel_withdraw_keeps_observation(self):
        rig = Rig(self, "policy-P8")
        contract_worker(rig)
        holder = exp_task(rig, "p8-go", "P8 run", "mkdir -p out; sleep 12; echo P8_DONE; printf PASS > out/p8.txt",
                          log="P8_DONE", result_file="out/p8.txt")
        calls: dict = {}

        def cancel_exp(r):
            calls["cancel_exp"] = rig.to_worker(kind="experiment", message="P8 cancel", task_id=calls["exp_tid"],
                                                cancel=True)
            return h.tools(calls["cancel_exp"])

        def work(r):
            calls["work"] = rig.to_worker(kind="work", message="P8 work", spec=spec("P8 work", ["notes/"]))
            return h.tools(calls["work"])

        def follow(r):
            calls["follow"] = rig.to_worker(kind="work", message="P8 follow-up that must be withdrawn",
                                            task_id=calls["work_tid"])
            return h.tools(calls["follow"])

        def cancel_work(r):
            calls["cancel_work"] = rig.to_worker(kind="work", message="P8 cancel work", task_id=calls["work_tid"],
                                                 cancel=True)
            return h.tools(calls["cancel_work"])

        rig.p.on(user_text("p8-cancel-exp"), cancel_exp, role="manager", once=True)
        rig.p.on(user_text("p8-work"), work, role="manager", once=True)
        rig.p.on(user_text("p8-follow"), follow, role="manager", once=True)
        rig.p.on(user_text("p8-cancel-work"), cancel_work, role="manager", once=True)
        release = threading.Event()

        def busy_worker(r):
            release.wait(90)
            return "P8 work turn ends"

        rig.p.on(lambda r: task_injected(r) and (r.injected.get("payload") or {}).get("message") == "P8 work",
                 busy_worker, role="worker", once=True)
        rig.step("start", lambda: rig.start() and {})

        def cancel_running():
            rig.say("manager_omp", "p8-go")
            assert rig.pump_until(lambda: "call" in holder and rig.result(holder["call"]), 60)
            calls["exp_tid"] = rig.result(holder["call"])["task_id"]
            snap = rig.wait(lambda s: (s["automation"]["run"] or {}).get("bound"), 60, "bound run")
            run_id = snap["automation"]["run"]["run_id"]
            rig.say("manager_omp", "p8-cancel-exp")
            assert rig.pump_until(lambda: "cancel_exp" in calls and rig.result(calls["cancel_exp"]), 30)
            result = rig.result(calls["cancel_exp"])
            assert result["status"] == "cancel_requested", result
            mid = rig.task()
            assert mid["status"] == "cancelling" and mid["held_reason"] == "cancel_waiting_for_host_exit", mid
            log = raw_log(rig, run_id)
            assert rig.pump_until(lambda: log.exists() and b"P8_DONE" in log.read_bytes(), 40), "the command was killed"
            closed = rig.wait_task(lambda t: t["status"] == "closed", 60, "cancelled after exit")
            assert closed["closed_reason"] == "cancelled" and closed["last_result"]["run_id"] == run_id, closed
            record = json.loads((run_dir(rig, run_id) / "run.json").read_text())
            assert record.get("task_id") == calls["exp_tid"] and record.get("run_id") == run_id, \
                {k: record.get(k) for k in ("task_id", "run_id")}
            analysis = rig.injected("worker", lambda i: stage_of(i) == "analysis" and i.get("task_id") == calls["exp_tid"])
            assert not analysis, "a cancelled run was judged"
            return {"cancel": result["status"], "while_running": {k: mid.get(k) for k in ("status", "held_reason")},
                    "closed": {k: closed.get(k) for k in ("closed_reason", "last_result")},
                    "run_record_kept": True, "raw_log_kept": log.exists(), "command_completed": True}

        def withdraw():
            rig.say("manager_omp", "p8-work")
            assert rig.pump_until(lambda: "work" in calls and rig.result(calls["work"]), 60)
            calls["work_tid"] = rig.result(calls["work"])["task_id"]
            rig.wait_injected("worker", lambda i: i.get("kind") == "task" and i.get("task_id") == calls["work_tid"],
                              60, "P8 work delivered")
            rig.say("manager_omp", "p8-follow")
            assert rig.pump_until(lambda: "follow" in calls and rig.result(calls["follow"]), 30)
            queued = rig.result(calls["follow"])
            assert queued["status"] == "queued", queued
            rig.say("manager_omp", "p8-cancel-work")
            assert rig.pump_until(lambda: "cancel_work" in calls and rig.result(calls["cancel_work"]), 30)
            cancelled = rig.result(calls["cancel_work"])
            assert cancelled["status"] == "cancelled", cancelled
            release.set()
            rig.pump_until(lambda: bool(rig.injected("worker", lambda i: (i.get("payload") or {}).get("cancel"))), 30)
            rig.pump(8)
            followups = rig.injected("worker", lambda i: (i.get("payload") or {}).get("message")
                                     == "P8 follow-up that must be withdrawn")
            cancels = rig.injected("worker", lambda i: (i.get("payload") or {}).get("cancel") is True
                                   and i.get("task_id") == calls["work_tid"])
            withdrawn = [j for j in rig.journal() if "withdraw" in json.dumps(j.get("type") or "")
                         or "withdrawn" in json.dumps(j)[:400]]
            assert not followups, "the withdrawn follow-up reached the worker"
            assert len(cancels) == 1, len(cancels)
            closed = rig.task()
            assert closed["status"] == "closed" and closed["closed_reason"] == "cancelled", closed
            return {"follow_up": queued["status"], "cancel": cancelled, "follow_up_delivered": 0,
                    "cancel_notice_delivered": len(cancels), "withdraw_journal": [j.get("type") for j in withdrawn][:5]}

        rig.step("P8_cancel_running_experiment", cancel_running, ("start",))
        rig.step("P8_cancel_withdraws_queued_follow_up", withdraw, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)


# ================================================================================================ clock suite
CLOCK_SUITE = (
    # 60 s terminal checks / merge / pause (FakeClock)
    "tests/backend/test_flow_terminal_notices.py",
    "tests/backend/test_terminal_independent_p27cd68.py",
    "tests/backend/test_terminal_fix03_independent_p27cd68.py",
    "tests/backend/test_cd70_followups.py",
    # watchdog
    "tests/backend/test_watchdog_independent_p27cd70.py",
    "tests/backend/test_cd70_watchdog.py",
    "tests/backend/test_cd70_fix03.py",
    "tests/backend/test_cd70_ui_wait.py",
    "tests/recovery_boot/test_cw19_fix01.py",
    "tests/recovery_boot/test_survivors_holds_independent_p27cw19.py",
    # automation loop / review scheduler (60 s reviews, merge, 121st review) / pause policy
    "tests/backend/test_automation_loop.py",
    "tests/recovery_boot/test_cw19_fix02.py",
    "tests/observation/test_worker_review.py",
    "tests/observation/test_worker_review_independent.py",
    "tests/policy/pause_automation/test_pause_policy.py",
    # F4: exact-toolCallId idempotence of handoff and terminal calls (not reachable through OMP 18.8.0)
    "tests/backend/test_handoff_service.py",
    "tests/backend/test_flow_terminal.py",
)


class Cw16ClockFixtureSuite(unittest.TestCase):
    """CLK: the fake-clock unit suite for the timing edges a live run cannot reach in bounded time (fixture
    evidence; it supplements, never replaces, the live P1/P2/P3 observations). Runs without WB_LIVE_CW16."""

    def setUp(self):
        if not fr.selected("CLK"):
            self.skipTest("filtered out by WB_CW16_SCENARIOS (CLK)")

    def test_clock_fixture_suite(self):
        report = h.ScenarioReport("policy-CLK-clock-fixture-suite", run_id=fr.RUN_ID, evidence_level="fixture")
        rows = {}
        for module in CLOCK_SUITE:
            path = h.REPO / module
            env = h.clean_base_env(HOME="/tmp", PYTHONPATH=f"{h.SRC}:{path.parent}", PYTHONDONTWRITEBYTECODE="1")
            started = time.monotonic()
            try:
                done = subprocess.run([sys.executable, "-m", "unittest", str(path)], cwd=h.REPO, env=env,
                                      capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
                tail = (done.stderr or "").strip().splitlines()[-3:]
                ran = next((int(m.group(1)) for line in tail for m in [re.match(r"Ran (\d+) test", line)] if m), 0)
                rows[module] = {"exit": done.returncode, "ran": ran, "summary": tail[-1] if tail else "",
                                "seconds": round(time.monotonic() - started, 1)}
            except subprocess.TimeoutExpired:
                rows[module] = {"exit": None, "ran": 0, "summary": "timeout", "seconds": 900}
            report.step(module, h.PASS if rows[module]["exit"] == 0 and rows[module]["ran"] > 0 else h.FAIL,
                        **rows[module])
        report.data["context"]["total_tests"] = sum(r["ran"] for r in rows.values())
        print(f"\n[cw16-b2] clock suite: {report.write()}", file=sys.stderr)
        bad = {m: r for m, r in rows.items() if r["exit"] != 0 or not r["ran"]}
        self.assertEqual(bad, {})


def tearDownModule() -> None:
    if not SKIP:
        print(f"\n[cw16-b2] summary: {fr.write_summary(('flow-', 'policy-'), 'flow-policy-summary.json')}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
