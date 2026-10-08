"""LIVE CW-16 I-SHELL scenarios (B2) on the product host terminal, Bash and real sh (dash). Opt-in: WB_LIVE_CW16=1.

Same path as live_cw16_flow_policy.py: ``python -m workbench start`` (installed OMP, version recorded), the product
UI on an owned PTY, a scripted local provider (ZERO real model requests). The host shell is ``bash`` (Bash on
PATH) or ``dash`` (no Bash on PATH: the product picks ``sh`` -> /usr/bin/dash). Every check is on the product's
behaviour; the scripted model only decides which tool it calls.

  SH1 the user prepares the parent shell (cd sub; export WB_CW16=...; PATH=$PWD/bin:$PATH): a worker ``terminal``
      command inherits cwd/PATH/value; an experiment inherits PATH/value (and runs in its worktree); ``cd``/
      ``export`` inside either never reach the parent; the parent PID/cwd stay; no WORKBENCH_* in the children.
  SH2 a worker command waiting in the foreground: takeover request (prefix t) holds new automatic sends at once;
      request and confirmation (prefix c) are separate (input before the confirmation does not reach the
      foreground); the delivered command's state is reported; manual input reaches the confirmed foreground;
      ``wb-handoff`` returns the shell to the control wait; nothing is replayed.
  SH3 unsubmitted input / a REPL (python3) / a background job in the host shell: ``terminal`` is refused with the
      reason (nothing typed) and an experiment is held (host_terminal_busy, no run).
  SH4 ``sh -c 'sleep 5 & exit 3'``: main return, descendant end and input return are separate; exit 3 is the
      confirmed status (terminal tool and experiment).
  SH5 incompatible prompt hooks/traps: bash PROMPT_COMMAND, bash ``trap DEBUG`` (observed) and ``trap CHLD``;
      dash PS1 and ``trap CHLD``: refused/held with the reason, the command never runs.

Run (repo root)::

    WB_LIVE_CW16=1 PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest tests/integration/live_cw16_shell.py -v

Filters: WB_CW16_SHELLS=bash,dash  WB_CW16_SCENARIOS=SH1,SH2,...
Reports: $WB_CW16_REPORT_DIR/<run id>/shell-<SHn>-<shell>.json, shell-summary.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cw16_harness as h  # noqa: E402
import cw16_flow_rig as fr  # noqa: E402
from cw16_flow_rig import Rig, execution, spec, stage_of  # noqa: E402

SKIP = fr.skip_reason()
SHELLS = [s for s in os.environ.get("WB_CW16_SHELLS", "bash,dash").split(",") if s]
EXP_SHELL = {"bash": "bash", "dash": "sh"}


def shell_of(snap: dict) -> dict:
    return snap["panes"]["host_shell"]["shell"]


def idle_prompt(snap: dict) -> bool:
    shell = shell_of(snap)
    return shell["input_owner"] == "user" and shell["parent_mode"] == "manual_prompt"


def user_text(suffix: str):
    return lambda r: r.injected is None and r.last_role == "user" and r.last_text.endswith(suffix)


def judge(facts: dict) -> str:
    if not facts.get("result_excerpt") or not facts.get("raw_log_excerpt"):
        return "indeterminate"
    if facts.get("exit_status") != 0:
        return "failure"
    criteria = facts.get("criteria") or {}
    ok = criteria.get("log_contains", "\0") in facts["raw_log_excerpt"] \
        and criteria.get("result_contains", "\0") in facts["result_excerpt"]
    return "success" if ok else "failure"


class ShellRig(Rig):
    def __init__(self, test, scenario: str, shell: str):
        super().__init__(test, f"shell-{scenario}-{shell}", host_shell=shell, shell=shell)
        self.shell = shell
        self.p.on(lambda r: r.injected is not None and "response_contract" in r.injected, self._contract,
                  role="worker", name="worker-contract")
        self.p.on(lambda r: stage_of(r.injected) == "periodic_review", "review ok", role="worker")

    @staticmethod
    def _contract(r: h.Request) -> str:
        if stage_of(r.injected) == "execute":
            return fr.answer_contract(r.injected, "execute")
        return fr.answer_contract(r.injected, judge((r.injected.get("payload") or {}).get("facts") or {}))

    def host(self, text: str, *, enter: bool = True) -> None:
        self.say("host_shell", text, enter=enter)

    def wait_idle(self, timeout: float = 30, what: str = "idle prompt") -> dict:
        return self.wait(idle_prompt, timeout, what)

    def worker_terminal(self, trigger: str, command: str, timeout: float = 60) -> dict:
        """The user asks the worker (worker pane) -> the scripted worker calls ``terminal(command)`` once."""
        holder: dict = {}
        done = threading.Event()

        def first(r):
            holder["call"] = self.terminal(command)
            holder["sent"] = time.time()
            return h.tools(holder["call"])

        self.p.on(user_text(trigger), first, role="worker", once=True, name=f"term:{trigger}")
        self.p.on(lambda r: "call" in holder and r.last_role == "tool" and holder["call"][2] in r.tool_results,
                  lambda r: (done.set(), f"{trigger} answered")[1], role="worker", once=True)
        self.say("worker_omp", trigger)
        assert self.pump_until(lambda: "call" in holder and self.result(holder["call"]) is not None, timeout), \
            f"no terminal result for {command!r}"
        result = dict(self.result(holder["call"]))
        result["_latency_s"] = round(self.result_at[holder["call"][2]] - holder["sent"], 2)
        return result

    def experiment(self, trigger: str, message: str, command: str, *, log: str, result_file: str,
                   result: str = "PASS", environment=None) -> dict:
        holder: dict = {}

        def respond(r):
            holder["call"] = self.to_worker(kind="experiment", message=message, spec=spec(message, ["out/"], execution(
                self.sb.project, self.sb.commit, command, log=log, result_file=result_file, result=result,
                environment=environment, shell=EXP_SHELL[self.shell])))
            return h.tools(holder["call"])

        self.p.on(user_text(trigger), respond, role="manager", once=True, name=f"exp:{trigger}")
        self.say("manager_omp", trigger)
        assert self.pump_until(lambda: "call" in holder and self.result(holder["call"]) is not None, 60), trigger
        holder["result"] = self.result(holder["call"])
        return holder

    def manager_call(self, trigger: str, build) -> dict:
        holder: dict = {}

        def respond(r):
            holder["call"] = build()
            return h.tools(holder["call"])

        self.p.on(user_text(trigger), respond, role="manager", once=True, name=f"mgr:{trigger}")
        self.say("manager_omp", trigger)
        assert self.pump_until(lambda: "call" in holder and self.result(holder["call"]) is not None, 60), trigger
        return self.result(holder["call"])

    def started_commands(self) -> list[str]:
        return [j.get("command") for j in self.journal() if j.get("type") == "terminal_started"]


def _shell_events(rig: Rig, run_id: str) -> list[tuple[str, str]]:
    import sqlite3
    connection = sqlite3.connect(f"file:{rig.sb.data / 'tasks.sqlite3'}?mode=ro", uri=True, timeout=5)
    try:
        return [(kind, created) for kind, created in connection.execute(
            "SELECT kind, created_at FROM shell_events WHERE run_id = ? ORDER BY created_at", (run_id,))]
    finally:
        connection.close()


def _seconds(start: str, end: str) -> float | None:
    from datetime import datetime
    try:
        return round((datetime.fromisoformat(end.replace("Z", "+00:00"))
                      - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds(), 2)
    except (TypeError, ValueError):
        return None


def _run_record(rig: Rig, run_id: str) -> dict:
    try:
        return json.loads((rig.sb.data / "workflow" / "runs" / run_id / "run.json").read_text())
    except (OSError, ValueError):
        return {}


@unittest.skipIf(SKIP, SKIP or "")
class Cw16ShellScenarios(unittest.TestCase):
    def _shell(self, scenario: str, shell: str) -> None:
        if shell not in SHELLS:
            self.skipTest(f"filtered out by WB_CW16_SHELLS ({shell})")
        if not fr.selected(scenario.split("-")[0]) and not fr.selected(scenario):
            self.skipTest(f"filtered out by WB_CW16_SCENARIOS ({scenario})")

    # ------------------------------------------------------------------------------------------- SH1 + SH4
    def _sh1_sh4(self, shell: str) -> None:
        self._shell("SH1", shell)
        rig = ShellRig(self, "SH1-SH4", shell)
        proj = rig.sb.project
        ctx: dict = {}
        probe = "printf '%s|%s|%s|%s\\n' \"$PWD\" \"${WB_CW16:-unset}\" \"$PATH\" \"$(env | grep -c '^WORKBENCH_')\""

        def start():
            ready = rig.start()
            shell_state = shell_of(ready)
            ctx["parent"] = shell_state["parent"]
            ctx["generation"] = shell_state["generation"]
            expected = {"bash": ("bash", "/usr/bin/bash"), "dash": ("sh", "/usr/bin/dash")}[shell]
            assert (shell_state["kind"], h.exe_of(ctx["parent"]["pid"])) == expected, shell_state
            return {"shell": shell_state["kind"], "exe": h.exe_of(ctx["parent"]["pid"]), "parent": ctx["parent"]}

        def prepare():
            rig.host(f"mkdir -p sub/bin && cd sub && export WB_CW16=sh1-value && export PATH=\"$PWD/bin:$PATH\" "
                     f"&& echo \"$$\" > {proj}/sh1-pid.txt")
            value = h.wait_file(proj / "sh1-pid.txt", timeout=15, pump=[rig.ui])
            assert value and int(value) == ctx["parent"]["pid"], (value, ctx["parent"])
            rig.wait_idle()
            return {"parent_pid_from_shell": int(value)}

        def terminal_inherits():
            result = rig.worker_terminal("sh1-term", f"{probe} > {proj}/sh1-term.txt; cd /; export WB_CHILD=terminal")
            assert result["status"] == "exited" and result["exit_code"] == 0, result
            pwd, value, path, count = (proj / "sh1-term.txt").read_text().strip().split("|")
            assert pwd == str(proj / "sub") and value == "sh1-value", (pwd, value)
            assert path.startswith(f"{proj}/sub/bin:"), path
            assert count == "0", count
            assert result["cwd"] == str(proj / "sub"), result["cwd"]
            return {"cwd": pwd, "WB_CW16": value, "path_head": path.split(":")[0], "workbench_vars": int(count)}

        def declared_environment():
            """D-B2-1 repro: the experiment names a variable the user exported in the host shell after start."""
            holder = rig.experiment("sh1-declared", "SH1 declared env", "echo SH1_DECL; printf ok > decl.out",
                                    log="SH1_DECL", result_file="decl.out", result="ok",
                                    environment=["PATH", "WB_CW16"])
            tid = holder["result"]["task_id"]
            started = rig.pump_until(lambda: (rig.task().get("runs_started") or 0) >= 1, 25)
            task = rig.task()
            observed = {"dispatched": holder["result"]["status"],
                        "task": {k: task.get(k) for k in ("status", "held_reason", "runs_started")},
                        "ui": rig.ui.excerpt("SH1 declared")}
            if not started:
                observed["worker_terminal_meanwhile"] = {k: v for k, v in rig.worker_terminal(
                    "sh1-meanwhile", "echo SH1_MEANWHILE").items() if k in ("status", "reason")}
                cancel = rig.manager_call("sh1-declared-cancel", lambda: rig.to_worker(
                    kind="experiment", message="SH1 cancel", task_id=tid, cancel=True))
                observed["cancel"] = cancel.get("status")
                rig.wait_task(lambda t: t.get("task_id") == tid and t["status"] == "closed", 30, "declared cancelled")
                rig.wait_idle()
            else:
                rig.wait_task(lambda t: t.get("task_id") == tid and t["status"] in ("finished", "closed"), 120,
                              "declared finished")
                rig.wait_idle(60)
            assert started, {"defect": "a variable exported in the host shell after start is reported missing "
                                       "(checked against the start-up environment): the experiment never starts",
                             **observed}
            return observed

        def experiment_inherits():
            holder = rig.experiment("sh1-exp", "SH1 inherit", f"{probe} > sh1.out; echo SH1_EXP; cd /; export WB_CHILD=exp",
                                    log="SH1_EXP", result_file="sh1.out", result="sh1-value",
                                    environment=["PATH"])
            assert holder["result"]["status"] == "dispatched", holder["result"]
            tid = holder["result"]["task_id"]
            task = rig.wait_task(lambda t: t.get("task_id") == tid and t["status"] in ("finished", "closed"), 150,
                                 "SH1 experiment")
            report = rig.wait_injected("manager", lambda i: i.get("kind") == "report" and i.get("task_id") == tid, 30)
            pwd, value, path, count = report["payload"]["result_excerpt"].strip().split("|")
            assert task["last_result"]["judgment"] == "success", task
            assert value == "sh1-value" and path.startswith(f"{proj}/sub/bin:") and count == "0", (value, path, count)
            assert pwd.startswith(str(rig.sb.data / "workflow" / "worktrees")), pwd
            return {"cwd": pwd, "WB_CW16": value, "path_head": path.split(":")[0], "workbench_vars": int(count),
                    "judgment": "success"}

        def parent_kept():
            rig.wait_idle(60, "shell given back")
            rig.host(f"printf '%s|%s|%s\\n' \"$PWD\" \"${{WB_CHILD:-unset}}\" \"$$\" > {proj}/sh1-after.txt")
            value = h.wait_file(proj / "sh1-after.txt", contains="|", timeout=15, pump=[rig.ui])
            pwd, child, pid = value.strip().split("|")
            snap = rig.wait_idle()
            state = shell_of(snap)
            assert pwd == str(proj / "sub"), pwd
            assert child == "unset", child
            assert int(pid) == ctx["parent"]["pid"] and state["parent"] == ctx["parent"], (pid, state["parent"])
            assert state["generation"] == ctx["generation"], state["generation"]
            return {"cwd": pwd, "WB_CHILD": child, "parent_pid": int(pid), "generation": state["generation"]}

        def sh4_terminal():
            started = time.time()
            result = rig.worker_terminal("sh4-term", "sh -c 'sleep 5 & exit 3'")
            assert result["status"] == "exited" and result["exit_code"] == 3, result
            assert result["duration_seconds"] >= 4.5, result["duration_seconds"]
            rig.wait_idle()
            return {"status": result["status"], "exit_code": 3, "duration_s": result["duration_seconds"],
                    "elapsed_s": round(time.time() - started, 1)}

        def sh4_experiment():
            holder = rig.experiment("sh4-exp", "SH4 descendant", "printf R > sh4.out; echo SH4_MAIN; sh -c 'sleep 5 & exit 3'",
                                    log="SH4_MAIN", result_file="sh4.out", result="R")
            tid = holder["result"]["task_id"]
            task = rig.wait_task(lambda t: t.get("task_id") == tid and t["status"] in ("finished", "closed"), 150,
                                 "SH4 experiment")
            report = rig.wait_injected("manager", lambda i: i.get("kind") == "report" and i.get("task_id") == tid, 30)
            payload = report["payload"]
            assert payload["exit_status"] == 3 and payload["exit_confirmed"] is True, payload
            assert payload["judgment"] == "failure" and payload["reasons"] == ["nonzero_exit"], payload
            run_id = task["last_result"]["run_id"]
            record = _run_record(rig, run_id)
            events = _shell_events(rig, run_id)
            kinds = [kind for kind, _ in events]
            assert "started" in kinds and "ended" in kinds, events
            stamps = {kind: at for kind, at in events}
            gap = _seconds(stamps["started"], stamps["ended"])
            # exit 3 is recorded only after the background descendant (sleep 5) ended and input returned
            assert gap is not None and gap >= 4.5, (events, gap)
            return {"exit_status": 3, "exit_confirmed": True, "judgment": "failure", "shell_events": events,
                    "started_to_ended_s": gap, "shell_state": record.get("shell_state")}

        rig.step("start", start)
        rig.step("SH1_prepare_parent", prepare, ("start",))
        rig.step("SH1_terminal_inherits", terminal_inherits, ("SH1_prepare_parent",))
        rig.step("SH1_experiment_inherits", experiment_inherits, ("SH1_prepare_parent",))
        rig.step("SH1_declared_env_exported_after_start", declared_environment, ("SH1_prepare_parent",))
        rig.step("SH1_parent_unchanged_no_propagation", parent_kept, ("SH1_prepare_parent",))
        rig.step("SH4_terminal_descendant_exit3", sh4_terminal, ("start",))
        rig.step("SH4_experiment_descendant_exit3", sh4_experiment, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        fr.assert_steps(self, rig)

    def test_sh1_sh4_bash(self):
        self._sh1_sh4("bash")

    def test_sh1_sh4_dash(self):
        self._sh1_sh4("dash")

    # ------------------------------------------------------------------------------------------- SH2
    def _sh2(self, shell: str) -> None:
        self._shell("SH2", shell)
        rig = ShellRig(self, "SH2", shell)
        proj = rig.sb.project
        command = f"read -r line; echo \"SH2_GOT:$line\" >> {proj}/sh2.txt"
        call: dict = {}

        def first(r):
            call["call"] = rig.terminal(command)
            return h.tools(call["call"])

        rig.p.on(user_text("sh2-go"), first, role="worker", once=True)
        rig.p.on(lambda r: "call" in call and r.last_role == "tool" and call["call"][2] in r.tool_results,
                 "SH2 command answered", role="worker", once=True)
        ctx: dict = {}

        def running():
            rig.start()
            rig.say("worker_omp", "sh2-go")
            assert rig.pump_until(lambda: command in rig.started_commands(), 30), rig.started_commands()
            snap = rig.wait(lambda s: shell_of(s)["input_owner"] == "manager", 20, "manager owns the shell")
            ctx["parent"] = shell_of(snap)["parent"]
            return {"shell": {k: shell_of(snap).get(k) for k in ("input_owner", "parent_mode", "phase")},
                    "operated_by": snap["panes"]["host_shell"].get("operated_by")}

        def request_holds_new_sends():
            rig.ui_key(b"t")
            snap = rig.wait(lambda s: shell_of(s)["takeover_requested"] is True, 15, "takeover requested")
            assert rig.ui.wait_text(r"인수 요청됨", 10), rig.ui.excerpt("인수")
            held = rig.experiment("sh2-exp", "SH2 held", "echo SH2_EXP; printf PASS > sh2.out", log="SH2_EXP",
                                  result_file="sh2.out")
            tid = held["result"]["task_id"]
            rig.pump(5)
            task = rig.task()
            assert task["task_id"] == tid and task.get("run_id") is None or task.get("held_reason"), task
            assert "SH2_EXP" not in rig.ui.text(), "an experiment was typed into the shell under takeover"
            cancel = rig.manager_call("sh2-cancel", lambda: rig.to_worker(kind="experiment", message="SH2 cancel",
                                                                        task_id=tid, cancel=True))
            assert cancel["status"] == "cancelled", cancel
            ctx["held_tid"] = tid
            return {"takeover_requested": True, "held_task": {k: task.get(k) for k in ("status", "held_reason",
                                                                                        "runs_started")},
                    "cancel": cancel["status"], "owner": shell_of(snap)["input_owner"]}

        def input_before_confirm():
            rig.focus("host_shell")
            rig.ui.type("early", gap=0.02)
            rig.ui.send(b"\r", settle=0.5)
            rig.pump(2)
            assert not (proj / "sh2.txt").exists(), "input before the confirmation reached the foreground"
            refusal = rig.ui.excerpt("거부", "인수", "owner")
            return {"delivered": False, "ui": refusal}

        def confirm_and_type():
            rig.ui_key(b"c")
            snap = rig.wait(lambda s: shell_of(s)["takeover_confirmed"] is True, 15, "takeover confirmed")
            assert rig.ui.wait_text(r"인수 확인됨", 10), rig.ui.excerpt("인수")
            rig.focus("host_shell")
            rig.ui.type("hello", gap=0.02)
            rig.ui.send(b"\r", settle=0.5)
            value = h.wait_file(proj / "sh2.txt", contains="SH2_GOT", timeout=15, pump=[rig.ui])
            assert value == "SH2_GOT:hello\n", value
            assert rig.pump_until(lambda: rig.result(call["call"]) is not None, 30), "the worker call never returned"
            delivered = rig.result(call["call"])
            assert delivered["status"] in ("exited", "unknown", "running"), delivered
            return {"foreground_got": value.strip(), "delivered_command_result": {k: delivered.get(k) for k in (
                "status", "exit_code", "reason", "detail")}, "shell": {k: shell_of(snap).get(k) for k in (
                    "input_owner", "parent_mode", "takeover_confirmed")}}

        def handoff_no_replay():
            snap = rig.wait(lambda s: shell_of(s)["parent_mode"] == "manual_prompt", 30, "prompt after the command")
            rig.host("wb-handoff")
            snap = rig.wait(lambda s: shell_of(s)["parent_mode"] == "control_wait", 20, "control wait")
            commands_before = rig.started_commands()
            rig.pump(6)
            after = rig.status()
            assert rig.started_commands() == commands_before, "a command was re-sent"
            assert (proj / "sh2.txt").read_text() == "SH2_GOT:hello\n", "the old command ran again"
            assert after["task"]["task_id"] == ctx["held_tid"] and after["task"]["status"] == "closed", after["task"]
            assert not any("SH2_EXP" in (c or "") for c in rig.started_commands())
            in_wait = rig.worker_terminal("sh2-in-wait", f"echo SH2_IN_WAIT > {proj}/sh2-in-wait.txt")
            assert in_wait["status"] == "host_terminal_busy" and not (proj / "sh2-in-wait.txt").exists(), in_wait
            # documented way back to the user's prompt (UI help: prefix t, c re-takeover)
            rig.ui_key(b"t")
            rig.ui_key(b"c")
            rig.wait_idle(20, "user prompt after the re-takeover")
            again = rig.worker_terminal("sh2-after", "echo SH2_AFTER")
            assert again["status"] == "exited" and "SH2_AFTER" in again["output_tail"], again
            assert shell_of(rig.status())["parent"] == ctx["parent"], "the parent shell changed"
            return {"control_wait": {k: shell_of(snap).get(k) for k in ("input_owner", "parent_mode")},
                    "replayed": 0, "cancelled_experiment_ran": False,
                    "terminal_in_control_wait": {k: in_wait.get(k) for k in ("status", "reason")},
                    "after_retakeover_terminal": again["status"]}

        rig.step("SH2_worker_command_waiting", running)
        rig.step("SH2_request_holds_new_sends", request_holds_new_sends, ("SH2_worker_command_waiting",))
        rig.step("SH2_input_before_confirm_not_delivered", input_before_confirm, ("SH2_request_holds_new_sends",))
        rig.step("SH2_confirm_manual_foreground_input", confirm_and_type, ("SH2_request_holds_new_sends",))
        rig.step("SH2_wb_handoff_control_wait_no_replay", handoff_no_replay, ("SH2_confirm_manual_foreground_input",))
        rig.step("shutdown", rig.shutdown_clean, ("SH2_worker_command_waiting",))
        fr.assert_steps(self, rig)

    def test_sh2_bash(self):
        self._sh2("bash")

    def test_sh2_dash(self):
        self._sh2("dash")

    # ------------------------------------------------------------------------------------------- SH3
    def _sh3(self, shell: str) -> None:
        self._shell("SH3", shell)
        rig = ShellRig(self, "SH3", shell)
        proj = rig.sb.project

        def refused(trigger: str, marker: str, expect: str) -> dict:
            result = rig.worker_terminal(trigger, f"echo {marker} > {proj}/{marker}.txt")
            assert result["status"] == "host_terminal_busy" and expect in result["reason"], result
            assert not (proj / f"{marker}.txt").exists(), "a refused command ran"
            assert not any(marker in (c or "") for c in rig.started_commands())
            return {"status": result["status"], "reason": result["reason"], "latency_s": result["_latency_s"]}

        def unsubmitted():
            rig.start()
            rig.host("echo SH3_PARTIAL", enter=False)
            snap = rig.wait(lambda s: shell_of(s)["parent_mode"] == "manual_input", 15, "manual_input")
            observed = refused("sh3-a", "SH3_A", "manual_input")
            rig.focus("host_shell")
            rig.ui.send(b"\x03", settle=0.5)  # Ctrl-C drops the typed line
            rig.wait_idle(15, "prompt after Ctrl-C")
            return {**observed, "held_reasons": shell_of(snap)["held_reasons"]}

        def repl():
            rig.host("python3")
            snap = rig.wait(lambda s: shell_of(s)["parent_mode"] == "manual_foreground", 20, "REPL foreground")
            observed = refused("sh3-b", "SH3_B", "manual_foreground")
            rig.host("exit()")
            rig.wait_idle(20, "prompt after the REPL")
            return {**observed, "held_reasons": shell_of(snap)["held_reasons"]}

        def job():
            pid_file = proj / "sh3-job.pid"
            rig.host(f"sleep 300 & echo $! > {pid_file}")
            value = h.wait_file(pid_file, timeout=15, pump=[rig.ui])
            pid = int(value)
            start = h.ticks(pid)
            rig.sb.own(pid, start)
            self.addCleanup(h.kill_exact, pid, start)
            rig.pump(1)
            observed = refused("sh3-c", "SH3_C", "jobs")
            holder = rig.experiment("sh3-exp", "SH3 held", "echo SH3_EXP; printf PASS > sh3.out", log="SH3_EXP",
                                    result_file="sh3.out")
            rig.pump(5)
            task = rig.task()
            assert (task["status"], task["held_reason"], task["runs_started"]) == ("dispatched", "host_terminal_busy", 0), task
            assert "worktrees/" not in rig.ui.text(), "Workbench typed into the user's shell"
            assert h.alive(pid, start), "the user's job was touched"
            rig.host(f"kill {pid}; wait")
            started = rig.pump_until(lambda: (rig.task().get("runs_started") or 0) >= 1, 40)
            final = rig.wait_task(lambda t: t["status"] in ("finished", "closed"), 120, "SH3 experiment") if started else rig.task()
            return {**observed, "experiment_held": {k: task.get(k) for k in ("status", "held_reason", "runs_started")},
                    "after_job_ended": {"started": started, "status": final.get("status"),
                                        "judgment": (final.get("last_result") or {}).get("judgment")},
                    "dispatched": holder["result"]["status"]}

        rig.step("SH3_unsubmitted_input_held", unsubmitted)
        rig.step("SH3_repl_foreground_held", repl, ("SH3_unsubmitted_input_held",))
        rig.step("SH3_background_job_held", job, ("SH3_unsubmitted_input_held",))
        rig.step("shutdown", rig.shutdown_clean, ("SH3_unsubmitted_input_held",))
        fr.assert_steps(self, rig)

    def test_sh3_bash(self):
        self._sh3("bash")

    def test_sh3_dash(self):
        self._sh3("dash")

    # ------------------------------------------------------------------------------------------- SH5
    def _sh5(self, shell: str, label: str, setup: str, *, may_be_compatible: bool = False) -> None:
        self._shell("SH5", shell)
        rig = ShellRig(self, f"SH5-{label}", shell)
        proj = rig.sb.project

        def broken():
            rig.start()
            rig.host(setup)
            rig.pump(2)
            first = rig.worker_terminal("sh5-a", f"echo SH5_A > {proj}/sh5a.txt")
            rig.pump(2)
            ran = (proj / "sh5a.txt").exists()
            if may_be_compatible and first["status"] == "exited":
                assert first["exit_code"] == 0 and (proj / "sh5a.txt").read_text() == "SH5_A\n", first
                rig.note(f"{label}: the product treats this setup as compatible: the command ran correctly")
                return {"compatible": True, "result": {k: first.get(k) for k in ("status", "exit_code")}}
            assert first["status"] in ("start_failed", "host_terminal_busy") and first.get("reason"), first
            assert not ran, "a command ran under an incompatible hook"
            second = rig.worker_terminal("sh5-b", f"echo SH5_B > {proj}/sh5b.txt")
            assert second["status"] == "host_terminal_busy" and second.get("reason"), second
            assert not (proj / "sh5b.txt").exists()
            if "hook" not in (second.get("reason") or ""):
                rig.note(f"{label}: held with reason {second.get('reason')!r}, which does not name the hook")
            holder = rig.experiment("sh5-exp", "SH5 held", "echo SH5_EXP; printf PASS > sh5.out", log="SH5_EXP",
                                    result_file="sh5.out")
            rig.pump(6)
            task = rig.task()
            assert task["runs_started"] == 0 and task["held_reason"], task
            state = shell_of(rig.status())
            return {"hook_named_in_reason": "hook" in (second.get("reason") or ""),
                    "first": {k: first.get(k) for k in ("status", "reason")},
                    "second": {k: second.get(k) for k in ("status", "reason")},
                    "experiment": {k: task.get(k) for k in ("status", "held_reason", "runs_started")},
                    "held_reasons": state["held_reasons"], "dispatched": holder["result"]["status"]}

        rig.step(f"SH5_{label}", broken)
        rig.step("shutdown", rig.shutdown_clean, (f"SH5_{label}",))
        fr.assert_steps(self, rig)

    def test_sh5_bash_prompt_command(self):
        self._sh5("bash", "prompt_command", "PROMPT_COMMAND='echo hi'")

    def test_sh5_bash_trap_debug(self):
        self._sh5("bash", "trap_debug", "trap 'true' DEBUG", may_be_compatible=True)

    def test_sh5_bash_trap_chld(self):
        self._sh5("bash", "trap_chld", "trap ':' CHLD")

    def test_sh5_dash_ps1(self):
        self._sh5("dash", "ps1", "PS1='% '")

    def test_sh5_dash_trap_chld(self):
        self._sh5("dash", "trap_chld", "trap ':' CHLD")


def tearDownModule() -> None:
    if not SKIP:
        print(f"\n[cw16-b2] summary: {fr.write_summary(('shell-',), 'shell-summary.json')}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
