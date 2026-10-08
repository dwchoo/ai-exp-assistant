"""LIVE CW-16 I-FAULT (B3): fault scenarios X1-X7 through the product path. Opt-in: WB_LIVE_CW16=1.

Every scenario starts the real backend with ``python -m workbench start`` (the user's installed OMP, any version --
recorded, never pinned: C-D72 (2)) on the scripted local provider of ``cw16_harness`` (ZERO model requests), drives
the product UI (``python -m workbench attach``) on an owned PTY and reads ``status --json``. Faults are injected
only into processes / files this run owns (exact pid + start ticks; the sandbox's own data dir).

  X1 UI detach (>= 60 s) while an experiment runs, automation is active and a host-terminal takeover is requested:
     periodic review only while active, pause kept across a second detach, takeover/owner/delivered-command state
     kept, no automatic send while detached, output produced while detached shown after reattach.
  X2 open Task + running experiment, backend SIGKILL (exact pid) -> ``start``: same_boot_crash, run outcome
     unknown (no re-run), no outbox resend, one ``backend_restarted`` notice to the new manager session, survivors
     listed, ``stop_survivor`` refuses an unverifiable survivor; L-CW19-RESTART (tests/recovery_boot/
     live_restart_independent_p27cw19.py) re-run by :class:`Cw19RestartRerun`.
  X3 model faults: a worker turn error -> ``model_hold:worker`` while the host run and its raw log continue; the
     user's next worker turn whose first tool is ``terminal`` is allowed (P2-2) and lifts the hold; a manager turn
     error holds a worker ``to_manager`` report which is delivered once after the manager recovers (P2-1).
  X4 ``shutdown`` while work runs: no ``--yes`` -> exit 1 + active work; ``--yes`` -> verified, exit 0, nothing
     left; a process left in another session -> unverified, exit 1, "종료 확인 실패"; plus the OMP daemon broker
     check (an owned broker must not outlive a verified shutdown).
  X5 real guest reboot in the isolated VM: :class:`VmReboot` wraps ``vm_reboot_driver.py host`` (opt-in
     WB_CW16_VM=1; never the host).
  X6 storage faults: a run printing 70 MiB stores 64 MiB and keeps running (cap shown); the Task spec/summary/
     worktree survive a restart; the 512 MiB project cap is the reduced-limit fixture test; data dir 0500 ->
     ``metadata_unavailable`` hold (new automatic work blocked, shown), a pause that cannot be stored is shown
     (P3-2); raw-log store failure is shown while the run continues.
  X7 C-D52: a worker ``terminal`` command delivered to the host shell, the user requests and confirms takeover
     (new automatic sends held), backend SIGKILL -> ``start``: the old command is ``unknown`` (durable), never
     replayed.

Run (repo root; the venv has pyte; strip TMUX*/HERDR_* from the runner's env)::

    WB_LIVE_CW16=1 PATH=$HOME/.local/bin:/usr/bin:/bin PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest \\
        tests/integration/live_cw16_fault.py -v

Filters: WB_CW16_FAULTS=X1,X2,X2R,X3,X4,X6,X7 (default all host scenarios), WB_CW16_VM=1 adds X5,
WB_CW16_DETACH_SECONDS (default 65). Reports: $WB_CW16_REPORT_DIR/<run id>/fault-<x>.json, fault-matrix.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
import unittest
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cw16_harness as h  # noqa: E402

LIVE = os.environ.get("WB_LIVE_CW16") == "1"
FAULTS = [x for x in os.environ.get("WB_CW16_FAULTS", "X1,X2,X2R,X3,X4,X6,X7").split(",") if x]
VM = os.environ.get("WB_CW16_VM") == "1"
DETACH_SECONDS = float(os.environ.get("WB_CW16_DETACH_SECONDS", "65"))
RUN_ID = os.environ.get("WB_CW16_RUN_ID") or time.strftime("cw16-b3-%Y%m%dT%H%M%S")
os.environ.setdefault("WB_CW16_RUN_ID", RUN_ID)
MIB = 1024 * 1024
BROKER_ARG = b"__omp_worker_daemon_broker"
CONTRACT = h._load(h.REPO / "tests/workflow/live_workflow_probe.py", "cw16_fault_contract")


def _skip_reason() -> str | None:
    if not LIVE:
        return "set WB_LIVE_CW16=1 (real OMP, scripted local provider, no model)"
    if sys.platform != "linux":
        return "Linux only"
    if not h.find_omp():
        return "OMP is not installed"
    if not h.pyte_available():
        return "pyte is required (use /tmp/cw02-g1-venv/bin/python)"
    return None


SKIP = _skip_reason()


# ============================================================================================ helpers
def notice_of(request: h.Request) -> dict | None:
    """A Workbench notice injected as a user message (``{"workbench_notice": <type>, ...}``)."""
    if request.last_role != "user" or not request.last_text.startswith("{"):
        return None
    try:
        value = json.loads(request.last_text)
    except ValueError:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("workbench_notice"), str) else None


def owned_brokers(root: Path) -> dict[int, dict]:
    """OMP daemon brokers whose cwd is inside this sandbox's own root (started by the sandbox's OMPs)."""
    found: dict[int, dict] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            if BROKER_ARG not in Path(f"/proc/{name}/cmdline").read_bytes():
                continue
            cwd = os.readlink(f"/proc/{name}/cwd")
        except OSError:
            continue
        if cwd == str(root) or cwd.startswith(str(root) + "/"):
            fields = h.stat_fields(int(name)) or []
            found[int(name)] = {"ticks": h.ticks(int(name)), "ppid": int(fields[1]) if fields else None,
                                "session": int(fields[3]) if fields else None, "cwd": cwd.replace(str(root), "$ROOT")}
    return found


def jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def lines_of(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


def socket_snapshot(sb: h.Sandbox) -> dict | None:
    """The backend snapshot over the UI socket, WITHOUT the CLI: every ``python -m workbench`` command re-tightens
    the data dir to 0700 (``ensure_private_dir``), which would silently undo a 0500 fault injection."""
    from workbench.backend.client import UiClient
    try:
        with UiClient(sb.data / "ui.sock", name="cw16-fault-probe", timeout=5) as client:
            return client.snapshot()
    except Exception:  # noqa: BLE001 - not running / transient
        return None


def wait_socket(sb: h.Sandbox, predicate: Callable[[dict], bool], timeout: float, what: str,
                pump: list | tuple = ()) -> dict:
    deadline = time.monotonic() + timeout
    snap = None
    while time.monotonic() < deadline:
        snap = socket_snapshot(sb)
        try:
            if snap is not None and predicate(snap):
                return snap
        except (KeyError, TypeError, AttributeError):
            pass
        for ui in pump:
            ui.pump(0.1)
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}; holds={snap and snap.get('holds')}")


def hold_reasons(snapshot: dict) -> list[str]:
    return [item.get("reason") for item in snapshot.get("holds") or []]


class Fault:
    """One fault scenario: own provider + sandbox(es), steps recorded in ``fault-<name>.json``."""

    name = "X?"

    def __init__(self) -> None:
        self.report = h.ScenarioReport(f"fault-{self.name.lower()}", run_id=RUN_ID, scenario_id=self.name)
        self.runner = h.StepRunner(self.report)
        self.hex = secrets.token_hex(3)
        self.notices: dict[str, list[dict]] = {"manager": [], "worker": [], "unknown": []}
        self.reviews: list[dict] = []
        self.provider = h.ScriptedProvider()
        self.provider.on(self._tap, None, name="tap")  # records notices / reviews, always falls through
        self._worker_rules()
        self.sandboxes: list[h.Sandbox] = []
        self.sb: h.Sandbox | None = None
        self.ui: h.Ui | None = None

    # -- provider ----------------------------------------------------------------------------------------
    def _tap(self, request: h.Request) -> bool:
        notice = notice_of(request)
        if notice is not None:
            self.notices.setdefault(request.role, []).append(
                {"at": time.time(), "type": notice.get("workbench_notice"), "notice_id": notice.get("notice_id"),
                 "task_id": notice.get("task_id"), "classification": notice.get("classification"),
                 "task_status": (notice.get("task") or {}).get("status"),
                 "survivors": [s.get("survivor_id") for s in notice.get("survivors") or []]})
        injected = request.injected or {}
        payload = injected.get("payload") if isinstance(injected.get("payload"), dict) else {}
        if request.role == "worker" and payload.get("stage") == "periodic_review":
            self.reviews.append({"at": time.time(), "coalesced": payload.get("coalesced_count"),
                                 "phase": (payload.get("facts") or {}).get("phase")})
        return False

    def _worker_rules(self) -> None:
        def staged(request: h.Request) -> h.Turn | None:
            injected = request.injected or {}
            if "response_contract" not in injected:
                return None
            payload = injected.get("payload") or {}
            if payload.get("stage") == "execute":
                decision = "execute"
            else:
                facts = payload.get("facts") or {}
                ok = facts.get("exit_status") == 0 and "PASS" in (facts.get("raw_log_excerpt") or "")
                decision = "success" if ok else "failure"
            return h.text(CONTRACT._frame_from_contract(injected, decision) or "invalid")

        self.provider.on(lambda r: r.injected is not None, staged, role="worker", name="worker-staged")
        self.provider.on(lambda r: (r.injected or {}).get("payload", {}).get("stage") == "periodic_review",
                         h.text("periodic review noted: the run continues"), role="worker", name="worker-review")

    def manager_says(self, trigger: str, responder: Callable[[h.Request], Any] | Any, *, once: bool = True) -> str:
        """The scripted manager answers the user's composer text ``trigger`` with ``responder``."""
        self.provider.on_text(trigger, responder, role="manager", once=once)
        return trigger

    def worker_says(self, trigger: str, responder: Callable[[h.Request], Any] | Any, *, once: bool = True) -> str:
        self.provider.on_text(trigger, responder, role="worker", once=once)
        return trigger

    def call(self, role: str, tool: str, args: dict) -> h.Turn:
        return h.tools(self.provider.call(role, tool, args))

    def experiment(self, sb: h.Sandbox, command: str, *, log_contains: str = "PASS", message: str = "cw16 run") -> dict:
        return {"kind": "experiment", "message": message, "task_id": None, "cancel": None, "run": None,
                "spec": {"goal": f"cw16 {self.name} run", "paths": ["outcome.txt"],
                         "execution": {"source": str(sb.project), "commit": sb.commit, "command": command,
                                       "criteria": {"log_contains": log_contains, "result_file": "outcome.txt",
                                                    "result_contains": "PASS"},
                                       "environment": ["PATH"], "shell": "bash"}}}

    # -- product driving ------------------------------------------------------------------------------------
    def sandbox(self, label: str, host_shell: str = "bash") -> h.Sandbox:
        sb = h.Sandbox(f"{self.name}{label}", provider=self.provider, host_shell=host_shell, report=self.report)
        self.sandboxes.append(sb)
        self.report.data.setdefault("roots", []).append(str(sb.root))
        return sb

    def start(self, sb: h.Sandbox) -> dict:
        done = sb.start()
        assert done.returncode == 0, done.stdout[-800:] + done.stderr[-600:]
        return sb.wait_ready()

    def open_ui(self, sb: h.Sandbox) -> h.Ui:
        ui = sb.ui()
        assert ui.wait(lambda: all(t in ui.text() for t in h.TITLES.values()), 60), ui.excerpt("OMP", "SHELL")
        self.ui = ui
        return ui

    def focus(self, sb: h.Sandbox, pane: str) -> dict:
        self.ui.send(h.PREFIX + h.FOCUS_KEYS[pane], settle=0.3)
        return sb.wait_status(lambda s: s["focus"] == pane, 15, f"focus {pane}", pump=[self.ui])

    def type_to(self, sb: h.Sandbox, pane: str, text: str) -> None:
        self.focus(sb, pane)
        self.ui.type(text, gap=0.03)
        self.ui.send(b"\r", settle=0.3)

    def prefix(self, key: bytes, settle: float = 0.5) -> None:
        self.ui.send(h.PREFIX + key, settle=settle)

    def toggle_pause(self, sb: h.Sandbox, paused: bool) -> dict:
        """prefix p opens the confirmation; one separate ``p`` confirms (pause, or resume after reconciling)."""
        self.prefix(b"p", settle=0.8)
        self.ui.send(b"p", settle=0.8)
        return sb.wait_status(lambda s: bool((s.get("automation") or {}).get("paused")) is paused, 60,
                              f"automation paused={paused}", pump=[self.ui])

    def detach(self, sb: h.Sandbox) -> int | None:
        ui = self.ui
        ui.send(h.PREFIX + b"q", settle=0.5)
        code = ui.wait_exit(15)
        sb.wait_status(lambda s: s["attached"] is False, 20, "detached")
        self.ui = None
        return code

    def run_task(self, sb: h.Sandbox, timeout: float = 120) -> dict:
        return sb.wait_status(lambda s: (s.get("task") or {}).get("kind") == "experiment"
                              and (s.get("task") or {}).get("run_id") and s["task"]["status"] == "running",
                              timeout, "experiment run started", pump=[self.ui] if self.ui else [])

    def raw_log(self, sb: h.Sandbox, run_id: str) -> Path:
        return sb.data / "raw-logs" / f"{run_id}.log"

    def size(self, path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return -1

    def kill_backend(self, sb: h.Sandbox) -> dict:
        snap = sb.status() or {}
        process = (snap.get("backend") or {}).get("process") or {}
        pid, start = process.get("pid"), process.get("start_ticks")
        assert pid and start and h.alive(pid, start), process
        sent = h.kill_exact(pid, start, signal.SIGKILL)
        deadline = time.monotonic() + 15
        while h.alive(pid, start) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.ui is not None:
            self.ui.close()
            self.ui = None
        return {"pid": pid, "start_ticks": start, "signalled": sent, "gone": not h.alive(pid, start)}

    def handoffs(self, sb: h.Sandbox) -> list[dict]:
        return jsonl(sb.data / "workflow" / "handoffs.jsonl")

    # -- the run ----------------------------------------------------------------------------------------------
    def steps(self) -> None:
        raise NotImplementedError

    def execute(self) -> dict:
        self.report.data["versions"] = h.tool_versions()
        try:
            self.steps()
        except h.NotApplicable as exc:
            self.report.step("scenario", h.NOT_RUN, reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - a harness error is a recorded failure, never a pass
            import traceback
            self.report.step("scenario_error", h.FAIL, error=f"{type(exc).__name__}: {exc}"[:2000],
                             trace=traceback.format_exc()[-3000:])
        finally:
            if self.ui is not None:
                self.ui.close()
            cleanups = []
            for sb in self.sandboxes:
                try:
                    os.chmod(sb.data, 0o700)
                    raw = sb.data / "raw-logs"
                    if raw.exists():
                        os.chmod(raw, 0o700)
                except OSError:
                    pass
                cleanups.append(sb.close())
            self.provider.close()
            self.report.data["cleanups"] = cleanups
            self.report.data["provider"] = self.provider.snapshot()
            self.report.data["provider"]["log_tail"] = self.provider.log[-30:]
            self.report.data["notices"] = self.notices
            self.report.data["reviews"] = self.reviews
            self.report.data["model_requests_real"] = 0  # scripted local provider only
            self.report.write()
        return {name: step.get("status") for name, step in self.report.data["steps"].items()}


# ============================================================================================ X1
class X1Detach(Fault):
    """UI detach >= 60 s while an experiment runs: (a) automation active, (b) a takeover requested, (c) paused."""

    name = "X1"

    def steps(self) -> None:
        sb = self.sb = self.sandbox("d")
        marker = sb.tmp / "x1-starts.txt"
        command = (f"echo X1START >> {marker}; for i in $(seq 1 320); do echo X1L $i; sleep 1; done; "
                   "printf PASS > outcome.txt; echo X1 PASS")
        self.manager_says("x1exp", lambda r: self.call("manager", "to_worker", self.experiment(sb, command)))
        r = self.runner
        state: dict[str, Any] = {}

        def review_view(snap: dict) -> dict:
            review = (snap.get("automation") or {}).get("review") or {}
            return {k: review.get(k) for k in ("status", "reason", "review_count", "next_due_in_seconds", "pending")}

        def shell_view(snap: dict) -> dict:
            shell = snap["panes"]["host_shell"]["shell"]
            return {k: shell.get(k) for k in ("takeover_requested", "takeover_confirmed", "input_owner", "owner_epoch",
                                              "parent_mode", "request_id", "phase", "held_reasons")}

        def detach_window(label: str) -> dict:
            snap0 = sb.status()
            base = {"ids": h.identity(snap0), "shell": shell_view(snap0), "review": review_view(snap0),
                    "reviews": len(self.reviews), "worker": self.provider.count("worker"),
                    "manager": self.provider.count("manager"), "log": self.size(self.raw_log(sb, state["run_id"])),
                    "task": {k: snap0["task"].get(k) for k in ("task_id", "run_id", "runs_started")}}
            ui_exit = self.detach(sb)
            started = time.monotonic()
            samples = []
            while time.monotonic() - started < DETACH_SECONDS:
                time.sleep(5)
                s = sb.status() or {}
                samples.append({"t": round(time.monotonic() - started), "attached": s.get("attached"),
                                **review_view(s)})
            mid = sb.status()
            self.open_ui(sb)
            snap1 = sb.wait_status(lambda s: s["attached"], 30, "reattached", pump=[self.ui])
            numbers: list[int] = []

            def visible() -> bool:
                numbers[:] = [int(n) for n in re.findall(r"X1L (\d+)", self.ui.text())]
                return bool(numbers) and max(numbers) >= state["line_floor"] + DETACH_SECONDS - 10

            state["line_floor"] = state.get("line_floor", 0)
            shown = self.ui.wait(visible, 15)
            state["line_floor"] = max(numbers or [0])
            return {"label": label, "ui_exit": ui_exit, "detach_seconds": round(time.monotonic() - started, 1),
                    "samples": samples, "review": [base["review"], review_view(snap1)],
                    "periodic_reviews_to_worker": len(self.reviews) - base["reviews"],
                    "provider_delta": {"worker": self.provider.count("worker") - base["worker"],
                                       "manager": self.provider.count("manager") - base["manager"]},
                    "raw_log_bytes": [base["log"], self.size(self.raw_log(sb, state["run_id"]))],
                    "identities_same": h.identity(snap1) == base["ids"] == h.identity(mid),
                    "shell": [base["shell"], shell_view(snap1)],
                    "task": [base["task"], {k: snap1["task"].get(k) for k in ("task_id", "run_id", "runs_started")}],
                    "automation_after": (snap1.get("automation") or {}).get("state"),
                    "paused_after": (snap1.get("automation") or {}).get("paused"),
                    "output_while_detached_shown": shown, "max_line_seen": max(numbers or [0])}

        def common(out: dict) -> None:
            assert out["ui_exit"] == 0 and out["identities_same"], out
            assert out["task"][0] == out["task"][1] and out["task"][1]["runs_started"] == 1, ("no new run", out)
            assert out["raw_log_bytes"][1] > out["raw_log_bytes"][0], ("collection continues while detached", out)
            assert out["provider_delta"]["manager"] == 0, ("no automatic manager turn while detached", out)
            assert out["output_while_detached_shown"], out

        def start_run() -> dict:
            self.start(sb)
            self.open_ui(sb)
            self.type_to(sb, "manager_omp", "x1exp")
            snap = self.run_task(sb)
            state["run_id"], state["task_id"] = snap["task"]["run_id"], snap["task"]["task_id"]
            assert h.wait_file(marker, contains="X1START", pump=[self.ui], timeout=30), "the run did not start"
            assert self.ui.wait(lambda: self.size(self.raw_log(sb, state["run_id"])) > 40, 30)
            return {"task": snap["task"], "automation": (snap.get("automation") or {}).get("state"),
                    "review": review_view(snap)}

        def detach_active() -> dict:
            out = detach_window("automation active, no takeover")
            common(out)
            assert out["shell"][0] == out["shell"][1], ("owner/delivered-command state changed", out)
            assert out["periodic_reviews_to_worker"] >= 1 and \
                (out["review"][1]["review_count"] or 0) > (out["review"][0]["review_count"] or 0), \
                ("automation active: the 60 s review must reach the worker while the UI is detached", out)
            return out

        def takeover() -> dict:
            self.focus(sb, "host_shell")
            before = sb.status()
            self.prefix(b"t")
            snap = sb.wait_status(lambda s: s["panes"]["host_shell"]["shell"]["takeover_requested"] is True, 20,
                                  "takeover requested", pump=[self.ui])
            shown = self.ui.wait(lambda: "인수 요청" in self.ui.text(), 5)
            out = {"before": shell_view(before), "after": shell_view(snap), "ui_notice_shown": shown,
                   "ui_lines": self.ui.excerpt("인수")[:3], "review": review_view(snap)}
            assert out["after"]["input_owner"] == "user" and "takeover_requested" in (out["after"]["held_reasons"] or []), out
            assert out["after"]["request_id"] == out["before"]["request_id"], ("the delivered run request changed", out)
            return out

        def detach_takeover() -> dict:
            out = detach_window("takeover requested")
            common(out)
            assert out["shell"][0] == out["shell"][1], ("takeover/owner/delivered-command state changed", out)
            return out

        def detach_paused() -> dict:
            paused = self.toggle_pause(sb, True)
            out = detach_window("paused")
            out["pause_shown_after_reattach"] = self.ui.wait(lambda: "일시정지" in self.ui.text(), 10)
            common(out)
            assert out["paused_after"] is True and out["pause_shown_after_reattach"], out
            assert out["periodic_reviews_to_worker"] == 0 and out["review"][0]["review_count"] == \
                out["review"][1]["review_count"], ("no review while paused", out)
            assert out["provider_delta"] == {"worker": 0, "manager": 0}, out
            assert out["shell"][0] == out["shell"][1], out
            out["paused_at"] = (paused.get("automation") or {}).get("state")
            return out

        def resume_and_finish() -> dict:
            resumed = self.toggle_pause(sb, False)
            sb.wait_status(lambda s: (s.get("task") or {}).get("status") in ("finished", "closed", "held", "blocked"),
                           300, "the run ends", pump=[self.ui])
            time.sleep(3)
            final = sb.status() or {}
            out = {"resume": (resumed.get("automation") or {}).get("resume"), "task": final.get("task"),
                   "run_started_lines": len(lines_of(marker)), "shell": shell_view(final)}
            assert out["run_started_lines"] == 1, ("the run was started again", out)
            assert (final.get("task") or {}).get("runs_started") == 1, out
            return out

        def shutdown() -> dict:
            done = sb.shutdown()
            result = h.shutdown_result(done.stdout)
            assert done.returncode == 0 and result.get("verified") is True, done.stdout[-800:]
            return {"verified": True, "left_running": result.get("left_running")}

        r.run("X1.start_run", start_run)
        r.run("X1.detach_active_review_continues", detach_active, requires=["X1.start_run"])
        r.run("X1.takeover_requested", takeover, requires=["X1.start_run"])
        r.run("X1.detach_takeover_state_kept", detach_takeover, requires=["X1.takeover_requested"])
        r.run("X1.detach_paused_pause_kept", detach_paused, requires=["X1.start_run"])
        r.run("X1.resume_no_replay", resume_and_finish, requires=["X1.detach_paused_pause_kept"])
        r.run("X1.shutdown", shutdown, requires=["X1.start_run"])


# ============================================================================================ X2
class X2Restart(Fault):
    """Open Task + running experiment, backend SIGKILL (exact pid), ``start``: reconcile, no replay, one notice."""

    name = "X2"

    def steps(self) -> None:
        sb = self.sb = self.sandbox("r")
        starts = sb.tmp / "x2-starts.txt"
        command = (f"echo X2START >> {starts}; nohup sleep 900 >/dev/null 2>&1 & "
                   "for i in $(seq 1 600); do echo X2L $i; sleep 1; done; printf PASS > outcome.txt; echo PASS")
        self.manager_says("x2exp", lambda r: self.call("manager", "to_worker", self.experiment(sb, command)))
        state: dict[str, Any] = {}

        def stop_call(stoppable: bool) -> Callable[[h.Request], h.Turn]:
            def respond(request: h.Request) -> h.Turn:
                survivors = state.get("survivors") or []
                target = next((s for s in survivors if s.get("state") == "alive"
                               and bool(s.get("stoppable")) is stoppable), None)
                state["stop_target"] = target
                return self.call("manager", "stop_survivor", {
                    "survivor_id": (target or {}).get("survivor_id") or "s99",
                    "reason": "cw16 X2: end a process the old backend left"})
            return respond

        self.manager_says("x2stopu", stop_call(False))
        self.manager_says("x2stopv", stop_call(True))
        r = self.runner

        def running() -> dict:
            self.start(sb)
            self.open_ui(sb)
            self.type_to(sb, "manager_omp", "x2exp")
            snap = self.run_task(sb)
            assert h.wait_file(starts, contains="X2START", pump=[self.ui], timeout=30)
            time.sleep(3)
            snap = sb.status()
            state.update(task=snap["task"], ids=h.identity(snap), handoffs=len(self.handoffs(sb)),
                         manager_injected=len(self.provider.injected["manager"]),
                         outbox_ids={rec.get("message_id") for rec in self.handoffs(sb) if rec.get("type") == "outbox"})
            state["brokers_before"] = owned_brokers(sb.root)
            return {"task": snap["task"], "brokers_before_kill": state["brokers_before"],
                    "outbox_records": len(state["outbox_ids"])}

        def kill() -> dict:
            out = self.kill_backend(sb)
            time.sleep(3)
            ids = state["ids"]
            panes = {name: h.alive(pid, start) for name, (pid, start, _sid, _gen) in ids["panes"].items()}
            out.update(panes_alive_after_kill=panes, brokers_after_kill=owned_brokers(sb.root),
                       starts_lines=len(lines_of(starts)))
            assert out["signalled"] and out["gone"], out
            return out

        def restart() -> dict:
            started = sb.start()
            assert started.returncode == 0, started.stdout[-800:] + started.stderr[-400:]
            snap = sb.wait_ready()
            startup = snap.get("startup") or {}
            state["survivors"] = startup.get("survivors") or []
            task = snap.get("task") or {}
            out = {"classification": startup.get("classification"), "run": startup.get("run"),
                   "processes": startup.get("processes"), "survivors": state["survivors"],
                   "outbox_lost": startup.get("outbox_lost"), "notice": startup.get("notice"),
                   "task": {k: task.get(k) for k in ("task_id", "status", "held_reason", "run_id", "runs_started")},
                   "brokers_at_reconcile": owned_brokers(sb.root),
                   "start_stdout_tail": started.stdout[-600:]}
            assert out["classification"] == "same_boot_crash", out
            assert (out["run"] or {}).get("state") == "outcome_unknown", out
            assert task.get("task_id") == state["task"]["task_id"] and task.get("held_reason") == "backend_restarted", out
            assert task.get("runs_started") == 1, out
            assert all(o.get("state") in ("queued_not_sent", "submitted_outcome_unknown")
                       for o in startup.get("outbox_lost") or []), out
            return out

        def notice_once() -> dict:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not [n for n in self.notices["manager"]
                                                       if n["type"] == "backend_restarted"]:
                time.sleep(0.5)
            time.sleep(15)  # a second copy would arrive within the 2 s notice retry
            restarted = [n for n in self.notices["manager"] if n["type"] == "backend_restarted"]
            records = [rec for rec in self.handoffs(sb) if rec.get("type") == "workbench_notice"
                       and rec.get("notice_type") == "backend_restarted"]
            out = {"notices_to_manager": restarted, "journal": records[-5:]}
            assert len(restarted) == 1, out
            assert restarted[0]["task_id"] == state["task"]["task_id"] and restarted[0]["classification"] == \
                "same_boot_crash", out
            return out

        def no_replay() -> dict:
            time.sleep(10)
            snap = sb.status() or {}
            records = self.handoffs(sb)
            last_start = max((i for i, rec in enumerate(records) if rec.get("type") == "backend_start"), default=-1)
            resent = [rec for rec in records[last_start + 1:] if rec.get("type") == "outbox"
                      and rec.get("message_id") in state["outbox_ids"]
                      and rec.get("state") in ("submitted", "delivered")]
            out = {"run_start_lines": len(lines_of(starts)), "runs_started": (snap.get("task") or {}).get("runs_started"),
                   "old_outbox_resent": resent, "task_status": (snap.get("task") or {}).get("status")}
            assert out["run_start_lines"] == 1 and out["runs_started"] == 1, out
            assert not resent, out
            return out

        def status_text() -> dict:
            done = sb.cli("status", "--data-dir", str(sb.data))
            lines = [l for l in done.stdout.splitlines() if "재시작" in l or "survivor" in l or "이전 backend" in l
                     or "process" in l]
            assert "same_boot_crash" in done.stdout, done.stdout[-800:]
            return {"lines": lines[:8]}

        def stop_survivor(trigger: str) -> dict:
            if self.ui is None:
                self.open_ui(sb)
            before = dict(self.provider.tool_results)
            self.type_to(sb, "manager_omp", trigger)
            deadline = time.monotonic() + 60
            result = None
            while time.monotonic() < deadline and result is None:
                self.ui.pump(0.3)
                new = {k: v for k, v in self.provider.tool_results.items() if k not in before}
                result = next((v for v in new.values() if isinstance(v, dict) and "status" in v
                               and ("survivor" in v or v.get("reason") in ("unknown_survivor", "identity_unverified",
                                                                          "not_alive")
                                    or v.get("status") in ("stopped", "refused", "already_ended", "stop_unconfirmed"))),
                              None)
            target = state.get("stop_target") or {}
            out = {"target": target, "result": result}
            assert result is not None, out
            if not target:
                raise h.NotApplicable(f"no alive survivor of this kind after the restart ({trigger}): {result}")
            if target.get("stoppable"):
                assert result.get("status") in ("stopped", "already_ended"), out
            else:
                assert result.get("status") == "refused", ("a survivor that cannot be proven must not be signalled", out)
                if target:
                    assert h.alive(target["pid"], target["start_ticks"]), ("refused but the process is gone", out)
            return out

        def shutdown() -> dict:
            if self.ui is not None:
                self.ui.close()
                self.ui = None
            done = sb.shutdown()
            result = h.shutdown_result(done.stdout)
            alive_survivors = [s for s in state.get("survivors") or [] if h.alive(s["pid"], s["start_ticks"])]
            out = {"exit": done.returncode, "verified": result.get("verified"), "problems": result.get("problems"),
                   "previous_survivors": result.get("previous_survivors"), "alive_survivors": len(alive_survivors)}
            if alive_survivors:  # C-AC-22: never verified while a previous survivor still runs
                assert done.returncode == 1 and result.get("verified") is False, out
                assert "previous_backend_survivors_alive" in (result.get("problems") or []), out
            else:
                assert done.returncode == 0 and result.get("verified") is True, out
            return out

        r.run("X2.running_open_task", running)
        r.run("X2.sigkill_backend", kill, requires=["X2.running_open_task"])
        r.run("X2.restart_reconcile", restart, requires=["X2.sigkill_backend"])
        r.run("X2.backend_restarted_once", notice_once, requires=["X2.restart_reconcile"])
        r.run("X2.no_replay", no_replay, requires=["X2.restart_reconcile"])
        r.run("X2.status_text", status_text, requires=["X2.restart_reconcile"])
        r.run("X2.stop_survivor_unverified_refused", lambda: stop_survivor("x2stopu"),
              requires=["X2.restart_reconcile"])
        r.run("X2.stop_survivor_verified_stopped", lambda: stop_survivor("x2stopv"), requires=["X2.restart_reconcile"])
        r.run("X2.shutdown", shutdown, requires=["X2.restart_reconcile"])


class Cw19RestartRerun:
    """L-CW19-RESTART (56 checks, no Task path) re-run on the current tree, bounded, from-scratch env."""

    name = "X2R"
    PROBE = h.REPO / "tests/recovery_boot/live_restart_independent_p27cw19.py"

    def execute(self) -> dict:
        report = h.ScenarioReport("fault-x2r-cw19-restart", run_id=RUN_ID, scenario_id="X2R",
                                  probe=str(self.PROBE.relative_to(h.REPO)))
        report.data["versions"] = h.tool_versions()
        target = report.path().with_name("fault-x2r-cw19-restart-probe.json")
        omp_dir = str(Path(h.find_omp()).parent)
        env = h.clean_base_env(PATH=f"{omp_dir}:/usr/bin:/bin", HOME=str(Path(h._user_bin("omp")).parents[2]),
                               PYTHONPATH=str(h.SRC), PYTHONDONTWRITEBYTECODE="1")
        started = time.monotonic()
        try:
            done = subprocess.run([sys.executable, str(self.PROBE), str(target)], cwd=h.REPO, env=env,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
            report.command(["python", str(self.PROBE.relative_to(h.REPO)), "<report>"], done.returncode, done.stdout,
                           done.stderr, time.monotonic() - started)
            data = json.loads(target.read_text()) if target.exists() else {}
            checks = data.get("checks") or []
            failed = [c["check"] for c in checks if not c.get("ok")]
            observed = {"exit": done.returncode, "checks": len(checks), "failed": failed,
                        "provider_requests": data.get("provider_requests"), "residue_root": data.get("residue_root"),
                        "probe_report": str(target)}
            ok = done.returncode == 0 and len(checks) >= 56 and not failed and not data.get("provider_requests")
            report.step("X2R.l_cw19_restart", h.PASS if ok else h.FAIL, observed=observed)
        except subprocess.TimeoutExpired:
            report.step("X2R.l_cw19_restart", h.FAIL, error="timeout 900 s")
        report.write()
        return {name: step.get("status") for name, step in report.data["steps"].items()}


# ============================================================================================ X3
class X3ModelFault(Fault):
    """Worker and manager turn errors (scripted HTTP 500): holds, observation continues, P2-2, P2-1."""

    name = "X3"

    def steps(self) -> None:
        sb = self.sb = self.sandbox("m")
        command = ("for i in $(seq 1 45); do echo X3L $i; sleep 1; done; printf PASS > outcome.txt; echo X3 PASS")
        self.manager_says("x3exp", lambda r: self.call("manager", "to_worker", self.experiment(sb, command)))
        state: dict[str, Any] = {"fail_worker": False, "fail_manager": False}
        # model faults: every request of that role errors while the flag is set (OMP may retry)
        self.provider.rules.insert(1, {"predicate": lambda r: state["fail_worker"] and r.role == "worker",
                                       "responder": h.error(500, "scripted worker model fault"), "role": "worker",
                                       "once": False, "name": "worker-fault", "hits": 0})
        self.provider.rules.insert(1, {"predicate": lambda r: state["fail_manager"] and r.role == "manager",
                                       "responder": h.error(500, "scripted manager model fault"), "role": "manager",
                                       "once": False, "name": "manager-fault", "hits": 0})
        terminal_marker = sb.tmp / "x3-terminal.txt"
        self.worker_says("x3recover", lambda r: self.call("worker", "terminal",
                                                          {"command": f"echo X3_RECOVERED >> {terminal_marker}"}))
        self.worker_says("x3report", lambda r: self.call("worker", "to_manager", {
            "kind": "progress", "message": f"X3 progress {self.hex} while the manager is held",
            "task_id": state.get("task_id")}))
        self.manager_says("x3work", lambda r: self.call("manager", "to_worker", {
            "kind": "work", "message": "cw16 X3: report progress", "task_id": None, "cancel": None, "run": None,
            "analysis": None, "commands": None, "spec": {"goal": "cw16 X3 report", "paths": ["notes/"]}}))
        r = self.runner

        def wait_hold(role: str, present: bool, timeout: float = 90) -> dict:
            return sb.wait_status(lambda s: (f"model_hold:{role}" in hold_reasons(s)) is present, timeout,
                                  f"model_hold:{role} present={present}", pump=[self.ui])

        def worker_fault() -> dict:
            self.start(sb)
            self.open_ui(sb)
            self.type_to(sb, "manager_omp", "x3exp")
            snap = self.run_task(sb)
            state["run_id"], state["task_id"] = snap["task"]["run_id"], snap["task"]["task_id"]
            state["fail_worker"] = True
            errors0 = self.provider.count("worker")
            self.type_to(sb, "worker_omp", f"x3fault {self.hex}")
            held = wait_hold("worker", True)
            log0 = self.size(self.raw_log(sb, state["run_id"]))
            time.sleep(6)
            snap = sb.status()
            log1 = self.size(self.raw_log(sb, state["run_id"]))
            shown = self.ui.wait(lambda: "모델 오류(worker)" in self.ui.text(), 10)
            out = {"holds": snap.get("holds"), "fault": (snap.get("faults") or {}).get("model"),
                   "worker_error_requests": self.provider.count("worker") - errors0,
                   "raw_log_growing_during_hold": [log0, log1], "task_status": snap["task"]["status"],
                   "ui_shown": shown, "ui_lines": self.ui.excerpt("모델 오류")[:2],
                   "manager_hold": "model_hold:manager" in hold_reasons(snap)}
            assert "model_hold:worker" in hold_reasons(held), out
            assert log1 > log0 and snap["task"]["run_id"] == state["run_id"], ("the run and its log must go on", out)
            assert shown and not out["manager_hold"], out
            return out

        def analysis_waits() -> dict:
            # the run ends during the worker hold: its analysis question must wait (no delivery to a held worker)
            sb.wait_status(lambda s: ((s.get("task") or {}).get("last_result") or {}).get("exit_status") is not None
                           or (s.get("task") or {}).get("status") in ("waiting_report", "finished", "held"), 120,
                           "the run ended", pump=[self.ui])
            injected0 = len(self.provider.injected["worker"])
            time.sleep(8)
            snap = sb.status()
            out = {"task": {k: snap["task"].get(k) for k in ("status", "held_reason", "last_result")},
                   "worker_deliveries_during_hold": len(self.provider.injected["worker"]) - injected0,
                   "still_held": "model_hold:worker" in hold_reasons(snap)}
            assert out["still_held"] and out["worker_deliveries_during_hold"] == 0, out
            return out

        def worker_recovers() -> dict:
            state["fail_worker"] = False
            self.type_to(sb, "worker_omp", f"x3wok {self.hex}")
            lifted = wait_hold("worker", False, 60)
            out = {"hold_after": hold_reasons(lifted),
                   "fault_after": ((lifted.get("faults") or {}).get("model") or {}).get("worker")}
            assert (out["fault_after"] or {}).get("recovered") is True, out
            return out

        def analysis_after() -> dict:
            snap = sb.wait_status(lambda s: (s.get("task") or {}).get("status") in ("finished", "closed"), 120,
                                  "the held analysis completes after the worker recovers", pump=[self.ui])
            analysis = [i for i in self.provider.injected["worker"] if i.get("stage") == "analysis"]
            out = {"task": {k: snap["task"].get(k) for k in ("status", "last_result")}, "analysis_deliveries": len(analysis)}
            assert len(analysis) == 1, out
            return out

        def p2_2() -> dict:
            # a second worker fault with the host terminal idle; the recovering turn's FIRST tool is terminal
            state["fail_worker"] = True
            self.type_to(sb, "worker_omp", f"x3fault2 {self.hex}")
            wait_hold("worker", True)
            state["fail_worker"] = False
            results0 = dict(self.provider.tool_results)
            self.type_to(sb, "worker_omp", "x3recover")
            deadline = time.monotonic() + 150
            result = None
            while time.monotonic() < deadline and result is None:
                self.ui.pump(0.3)
                result = next((v for k, v in self.provider.tool_results.items() if k not in results0
                               and isinstance(v, dict) and "status" in v), None)
            lifted = wait_hold("worker", False, 30)
            out = {"terminal_result": {k: (result or {}).get(k) for k in ("status", "reason", "exit_code")},
                   "marker": lines_of(terminal_marker), "hold_after": hold_reasons(lifted)}
            assert result is not None and result.get("status") == "exited" and result.get("exit_code") == 0, \
                ("P2-2: the first tool of the recovering turn must run", out)
            assert out["marker"] == ["X3_RECOVERED"], out
            return out

        def manager_fault() -> dict:
            self.type_to(sb, "manager_omp", "x3work")
            snap = sb.wait_status(lambda s: (s.get("task") or {}).get("kind") == "work"
                                  and s["task"]["status"] not in ("closed",), 60, "work Task open", pump=[self.ui])
            state["task_id"] = snap["task"]["task_id"]
            time.sleep(3)
            state["fail_manager"] = True
            self.type_to(sb, "manager_omp", f"x3mfault {self.hex}")
            held = wait_hold("manager", True)
            results0 = dict(self.provider.tool_results)
            manager_injected0 = len(self.provider.injected["manager"])
            self.type_to(sb, "worker_omp", "x3report")
            deadline = time.monotonic() + 60
            queued = None
            while time.monotonic() < deadline and queued is None:
                self.ui.pump(0.3)
                queued = next((v for k, v in self.provider.tool_results.items() if k not in results0
                               and isinstance(v, dict) and "status" in v), None)
            time.sleep(8)
            snap = sb.status()
            state["manager_injected0"] = manager_injected0
            out = {"to_manager_result": queued, "holds": hold_reasons(snap),
                   "delivered_during_hold": len(self.provider.injected["manager"]) - manager_injected0}
            assert "model_hold:manager" in hold_reasons(held), out
            assert queued is not None and queued.get("status") not in ("held", "rejected"), ("P2-1: the report "
                                                                                             "must be accepted", out)
            assert out["delivered_during_hold"] == 0, out
            return out

        def manager_recovers() -> dict:
            state["fail_manager"] = False
            self.type_to(sb, "manager_omp", "x3mok")
            lifted = wait_hold("manager", False, 90)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                got = [i for i in self.provider.injected["manager"][state["manager_injected0"]:]
                       if i.get("handoff") == "to_manager" and i.get("payload_kind") == "progress"]
                if got:
                    break
                self.ui.pump(0.5)
            time.sleep(10)
            got = [i for i in self.provider.injected["manager"][state["manager_injected0"]:]
                   if i.get("handoff") == "to_manager" and i.get("payload_kind") == "progress"]
            out = {"holds": hold_reasons(lifted), "progress_deliveries": len(got)}
            assert out["progress_deliveries"] == 1, ("P2-1: the held report is delivered exactly once", out)
            return out

        def shutdown() -> dict:
            done = sb.shutdown()
            result = h.shutdown_result(done.stdout)
            assert done.returncode == 0 and result.get("verified") is True, done.stdout[-600:]
            return {"verified": True}

        r.run("X3.worker_fault_hold", worker_fault)
        r.run("X3.analysis_waits_for_worker", analysis_waits, requires=["X3.worker_fault_hold"])
        r.run("X3.worker_recovers", worker_recovers, requires=["X3.worker_fault_hold"])
        r.run("X3.analysis_after_recovery", analysis_after, requires=["X3.worker_recovers"])
        r.run("X3.p2_2_first_tool_allowed", p2_2, requires=["X3.analysis_after_recovery"])
        r.run("X3.p2_1_manager_fault_report_kept", manager_fault, requires=["X3.worker_fault_hold"])
        r.run("X3.p2_1_delivered_once", manager_recovers, requires=["X3.p2_1_manager_fault_report_kept"])
        r.run("X3.shutdown", shutdown, requires=["X3.worker_fault_hold"])


# ============================================================================================ X4
class X4Shutdown(Fault):
    """``shutdown`` confirmation, verified and unverified full shutdown, OMP daemon broker after verification."""

    name = "X4"

    def steps(self) -> None:
        r = self.runner
        state: dict[str, Any] = {}
        a = self.sandbox("v")
        command = "for i in $(seq 1 300); do echo X4L $i; sleep 1; done; printf PASS > outcome.txt; echo PASS"
        self.manager_says("x4exp", lambda req: self.call("manager", "to_worker", self.experiment(a, command)))

        def refused_without_yes() -> dict:
            self.sb = a
            self.start(a)
            self.open_ui(a)
            self.type_to(a, "worker_omp", f"x4hello {self.hex}")  # one worker turn (OMP starts its daemon broker)
            self.provider.wait_count("worker", 1, 60)
            self.type_to(a, "manager_omp", "x4exp")
            snap = self.run_task(a)
            ids = h.identity(snap)
            done = a.shutdown(yes=False)
            after = a.status()
            out = {"exit": done.returncode, "stdout": done.stdout[-700:], "stderr": done.stderr[-300:],
                   "still_running": after is not None and h.identity(after) == ids}
            assert done.returncode == 1 and "refusing to shut down without confirmation" in done.stderr, out
            assert "active work:" in done.stdout and '"task_run"' in done.stdout, out
            assert out["still_running"], out
            state["ids"] = ids
            return out

        def verified() -> dict:
            self.ui.close()
            self.ui = None
            brokers_before = owned_brokers(a.root)
            pid_files = {}
            for path in a.data.rglob("broker.pid"):
                try:
                    pid_files[str(path.relative_to(a.data))] = json.loads(path.read_text()).get("pid")
                except (OSError, ValueError, AttributeError):
                    pid_files[str(path.relative_to(a.data))] = None
            done = a.shutdown()
            at = time.monotonic()
            result = h.shutdown_result(done.stdout)
            brokers_at_verified = owned_brokers(a.root)
            left = {name: h.alive(pid, start) for name, (pid, start, _s, _g) in state["ids"]["panes"].items()}
            state["broker"] = {"before_shutdown": brokers_before, "at_verified": brokers_at_verified,
                               "broker_pid_files_in_data_dir": pid_files}
            timeline = []
            while time.monotonic() - at < 20 and owned_brokers(a.root):
                time.sleep(0.25)
            state["broker"]["gone_after_seconds"] = round(time.monotonic() - at, 2) if not owned_brokers(a.root) \
                else None
            residue = {k: v for k, v in a.wait_no_residue(20).items()}
            out = {"exit": done.returncode, "verified": result.get("verified"), "processes": result.get("processes"),
                   "left_running": result.get("left_running"), "panes_alive_after": left, "residue": residue,
                   "timeline": timeline}
            assert done.returncode == 0 and result.get("verified") is True, out
            assert not any(left.values()) and residue == {}, out
            return out

        def broker() -> dict:
            info = state.get("broker") or {}
            out = dict(info)
            if not info.get("before_shutdown"):
                raise h.NotApplicable(f"this OMP started no daemon broker for the sandbox ({info})")
            # Contract (assignment p27-cw16-b3-01): a full shutdown must not report verified while a process the
            # Workbench OMPs started (their daemon broker, scoped to the Workbench data dir) is still alive.
            assert not info.get("at_verified"), ("verified shutdown while the Workbench OMP daemon broker is "
                                                 "still alive (it ends by itself later)", out)
            return out

        b = self.sandbox("u")

        def unverified() -> dict:
            self.sb = b
            self.start(b)
            self.open_ui(b)
            pid_file = b.tmp / "x4-setsid.pid"
            daemon_file = b.tmp / "x4-daemon.pid"
            # (1) a process in its own session that stays a descendant of the host shell (``setsid -w``): the
            #     product can see it (left_session) -> the shutdown must not be verified;
            # (2) a double-forked daemon (``setsid`` without -w re-parents to init): outside the shell's process
            #     tree, recorded as an observation only (CW-19 test-01 O1: not identifiable).
            self.type_to(b, "host_shell", f"setsid -w sleep 900 >/dev/null 2>&1 < /dev/null & echo $! > {pid_file}")
            assert h.wait_file(pid_file, pump=[self.ui], timeout=15)
            self.type_to(b, "host_shell", "setsid sh -c 'echo $$ > " + str(daemon_file) + "; exec sleep 901' "
                         ">/dev/null 2>&1 < /dev/null")
            assert h.wait_file(daemon_file, pump=[self.ui], timeout=15)
            time.sleep(1.5)
            daemon = int(daemon_file.read_text().split()[0])
            daemon_ticks = h.ticks(daemon)
            self.ui.close()
            self.ui = None
            done = b.cli("shutdown", "--data-dir", str(b.data), "--yes", timeout=120)
            payload = {}
            for line in done.stdout.splitlines():
                if line.startswith("shutdown result: "):
                    payload = json.loads(line[len("shutdown result: "):])
            out = {"exit": done.returncode, "stderr": done.stderr[-400:], "verified": payload.get("verified"),
                   "problems": payload.get("problems"), "left_running": payload.get("left_running"),
                   "active_listed": [l for l in done.stdout.splitlines() if "left_session" in l][:3],
                   "observation_double_forked_daemon": {
                       "alive_after_shutdown": h.alive(daemon, daemon_ticks),
                       "in_left_running": any(item.get("pid") == daemon for item in payload.get("left_running") or []),
                       "listed_as_active": any(f'"pid": {daemon}' in l for l in done.stdout.splitlines())}}
            assert done.returncode == 1 and payload.get("verified") is False, out
            assert "종료 확인 실패" in done.stderr, out
            assert payload.get("left_running"), out
            return out

        r.run("X4.no_yes_refused_active_listed", refused_without_yes)
        r.run("X4.yes_verified_nothing_left", verified, requires=["X4.no_yes_refused_active_listed"])
        r.run("X4.owned_broker_not_alive_at_verified", broker, requires=["X4.yes_verified_nothing_left"])
        r.run("X4.survivor_unverified_exit1", unverified)


# ============================================================================================ X6
class X6Storage(Fault):
    """raw-log run cap with the run continuing, restart preserving the Task, metadata/pause/raw-log store faults."""

    name = "X6"

    def steps(self) -> None:
        r = self.runner
        state: dict[str, Any] = {}
        a = self.sandbox("c")
        flood = ("head -c 73400320 /dev/zero | tr '\\0' 'x' | fold -w 199; echo X6_AFTER_CAP; "
                 "printf PASS > outcome.txt; echo X6 PASS")
        self.manager_says("x6flood", lambda req: self.call("manager", "to_worker", self.experiment(
            a, flood, log_contains="X6 PASS", message="cw16 X6 70 MiB")))

        def cap() -> dict:
            self.sb = a
            self.start(a)
            self.open_ui(a)
            self.type_to(a, "manager_omp", "x6flood")
            snap = self.run_task(a)
            run_id = state["run_id"] = snap["task"]["run_id"]
            state["task"] = snap["task"]
            self.ui.close()  # the flood is rendered by the backend VT only
            self.ui = None
            done = a.wait_status(lambda s: (s.get("task") or {}).get("status") in ("finished", "closed", "held",
                                                                                   "blocked"), 900,
                                 "the 70 MiB run ends")
            raw = (done.get("faults") or {}).get("raw_log") or {}
            size = self.size(self.raw_log(a, run_id))
            text = a.cli("status", "--data-dir", str(a.data)).stdout
            out = {"raw_log_status": raw, "stored_file_bytes": size, "task": {k: done["task"].get(k) for k in
                                                                              ("status", "last_result", "summary")},
                   "status_text": [l for l in text.splitlines() if "raw log" in l][:2]}
            assert size == 64 * MIB and raw.get("stored_bytes") == 64 * MIB, out
            assert raw.get("truncated") is True and raw.get("cap_source") in ("run", "run+project"), out
            assert "64 MiB" in (raw.get("text") or "") and out["status_text"], out
            judged = (done["task"].get("last_result") or {}).get("judged") or {}
            assert judged.get("exit_confirmed") is True and judged.get("exit_status") == 0, ("the run continued to "
                                                                                             "its end after the cap", out)
            return out

        def restart_keeps_task() -> dict:
            before = a.status()
            down = a.shutdown()
            assert down.returncode == 0, down.stdout[-400:]
            worktrees = sorted(str(p.relative_to(a.data)) for p in (a.data).rglob("outcome.txt"))
            self.start(a)
            after = a.status()
            out = {"task_before": {k: (before.get("task") or {}).get(k) for k in ("task_id", "summary", "status")},
                   "task_after": {k: (after.get("task") or {}).get(k) for k in ("task_id", "summary", "status")},
                   "worktree_outcome_files": worktrees,
                   "raw_log_after_restart": self.size(self.raw_log(a, state["run_id"])),
                   "run_record": (a.data / "workflow" / "runs" / state["run_id"] / "run.json").exists(),
                   "startup": (after.get("startup") or {}).get("classification")}
            assert out["task_after"]["task_id"] == out["task_before"]["task_id"], out
            assert out["task_after"]["summary"] == out["task_before"]["summary"], out
            assert worktrees and out["raw_log_after_restart"] == 64 * MIB and out["run_record"], out
            down = a.shutdown()
            assert down.returncode == 0
            return out

        def project_cap_fixture() -> dict:
            argv = [sys.executable, "-m", "unittest", "discover", "-v", "-s", "tests/recovery_boot", "-t",
                    "tests/recovery_boot", "-p", "test_faults_independent_p27cw19.py", "-k", "RawLogCaps"]
            done = subprocess.run(argv, cwd=h.REPO, env=h.clean_base_env(PYTHONPATH=str(h.SRC),
                                                                          HOME="/nonexistent-cw16",
                                                                          PYTHONDONTWRITEBYTECODE="1"),
                                  capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            self.report.command(argv, done.returncode, done.stdout, done.stderr, 0)
            ran = re.findall(r"^(test_\w+) .*\.\.\. (ok|FAIL|ERROR)", done.stderr, re.M)
            out = {"exit": done.returncode, "tests": ran, "evidence_level": "fixture (reduced project limit; the "
                   "512 MiB project cap has no runtime knob, by design)"}
            assert done.returncode == 0 and any("project" in name for name, _ in ran), out
            return out

        b = self.sandbox("f")
        self.manager_says("x6held", lambda req: self.call("manager", "to_worker", {
            "kind": "work", "message": "x6 while metadata is unavailable", "task_id": None, "cancel": None,
            "run": None, "spec": {"goal": "x6", "paths": ["notes/"]}}))
        self.worker_says("x6term", lambda req: self.call("worker", "terminal", {"command": "echo X6_SHOULD_NOT_RUN"}))
        quick = "echo X6R start; sleep 4; printf PASS > outcome.txt; echo X6R PASS"
        self.manager_says("x6raw", lambda req: self.call("manager", "to_worker", self.experiment(
            b, quick, log_contains="X6R PASS", message="cw16 X6 raw log store fault")))

        def start_b() -> dict:
            self.sb = b
            snap = self.start(b)
            self.open_ui(b)
            return {"phase": snap.get("phase")}

        def metadata_and_pause() -> dict:
            self.focus(b, "host_shell")
            os.chmod(b.data, 0o500)  # from here on: no CLI call until restored (see socket_snapshot)
            self.ui.type("exit", gap=0.03)
            self.ui.send(b"\r", settle=0.3)  # the pane ends -> the backend must write its record
            held = wait_socket(b, lambda s: "metadata_unavailable" in hold_reasons(s), 60, "metadata hold",
                               pump=[self.ui])
            shown = self.ui.wait(lambda: "metadata 저장 장애" in self.ui.text(), 10)
            results0 = dict(self.provider.tool_results)
            for pane, trigger in (("manager_omp", "x6held"), ("worker_omp", "x6term")):
                self.ui.send(h.PREFIX + h.FOCUS_KEYS[pane], settle=0.5)
                self.ui.type(trigger, gap=0.03)
                self.ui.send(b"\r", settle=0.3)
            deadline = time.monotonic() + 60
            new: dict = {}
            while time.monotonic() < deadline and len(new) < 2:
                self.ui.pump(0.3)
                new = {k: v for k, v in self.provider.tool_results.items() if k not in results0}
            self.prefix(b"p", settle=0.8)
            self.ui.send(b"p", settle=0.8)
            snap = wait_socket(b, lambda s: (s.get("automation") or {}).get("persistence_error"), 30,
                               "pause store failure shown", pump=[self.ui])
            still = socket_snapshot(b) or {}
            self.ui.resize(h.ROWS, 400)  # the same UI process, wider: is the pause store failure only cut off?
            wide = self.ui.wait(lambda: "저장 오류" in self.ui.text(), 10)
            wide_lines = [line.rstrip() for line in self.ui.lines()[:3]]
            self.ui.resize(h.ROWS, h.COLS)
            self.ui.pump(1.0)
            out = {"holds": held.get("holds"), "faults_metadata": (held.get("faults") or {}).get("metadata"),
                   "record_error": (held.get("faults") or {}).get("record_error"),
                   "ui_shown": shown, "ui_lines": self.ui.excerpt("metadata")[:2],
                   "new_work_results": list(new.values()),
                   "pause": {"paused": (snap.get("automation") or {}).get("paused"),
                             "persistence_error": (snap.get("automation") or {}).get("persistence_error"),
                             "detail": (snap.get("automation") or {}).get("detail"),
                             "metadata_sources": sorted(((snap.get("faults") or {}).get("metadata") or {})
                                                        .get("sources") or {})},
                   "pause_store_ui": self.ui.wait(lambda: "저장 오류" in self.ui.text(), 10),
                   "status_lines_170": [line.rstrip()[:170] for line in self.ui.lines()[:3]],
                   "pause_store_ui_at_400_cols": wide, "status_lines_400": wide_lines,
                   "hold_still_latched": "metadata_unavailable" in hold_reasons(still),
                   "backend_phase": still.get("phase"),
                   "bridges_connected": {r: (v or {}).get("connected") for r, v in (still.get("bridge") or {}).items()
                                         if r in ("manager", "worker")}}
            assert shown and len(new) == 2 and all(v.get("status") == "held" and v.get("reason") ==
                                                   "metadata_unavailable" for v in new.values()), out
            assert out["pause"]["paused"] is True and out["pause"]["persistence_error"] == "pause_not_stored", out
            assert "automation_pause" in out["pause"]["metadata_sources"], out
            assert out["hold_still_latched"] and out["backend_phase"] in ("ready", "degraded"), out
            state["pause_ui"] = {k: out[k] for k in ("pause_store_ui", "status_lines_170", "pause_store_ui_at_400_cols",
                                                     "status_lines_400")}
            return out

        def pause_store_failure_visible() -> dict:
            # P3-2 (CW-19): a pause that is not stored must be visible to the user at the default 170 columns,
            # not only on a wider terminal (same class as CW-19 VM F2: a safety display cut off by other text)
            out = state["pause_ui"]
            assert out["pause_store_ui"], ("the pause-store failure ('저장 오류') is cut off in the product UI at 170 "
                                           "columns (visible at 400)", out)
            return out

        def metadata_restored() -> dict:
            os.chmod(b.data, 0o700)
            snap = wait_socket(b, lambda s: "metadata_unavailable" not in hold_reasons(s)
                               and not (s.get("automation") or {}).get("persistence_error"), 60,
                               "metadata restored", pump=[self.ui])
            stored = json.loads((b.data / "automation.json").read_text())
            out = {"holds": hold_reasons(snap), "paused": (snap.get("automation") or {}).get("paused"),
                   "automation_json_paused": stored.get("paused", stored)}
            assert snap["automation"]["paused"] is True, out
            self.toggle_pause(b, False)
            return out

        def raw_log_store_fault() -> dict:
            raw_dir = b.data / "raw-logs"
            snap = b.wait_status(lambda s: s["panes"]["host_shell"]["shell"].get("parent_mode") == "manual_prompt",
                                 60, "host shell idle", pump=[self.ui])
            os.chmod(raw_dir, 0o500)
            try:
                self.type_to(b, "manager_omp", "x6raw")
                done = b.wait_status(lambda s: ((s.get("faults") or {}).get("raw_log") or {}).get("storage_error")
                                     or (s.get("task") or {}).get("status") in ("finished", "closed", "blocked"),
                                     120, "raw log store fault", pump=[self.ui])
                mid = done
                done = b.wait_status(lambda s: (s.get("task") or {}).get("status") in ("finished", "closed",
                                                                                       "blocked", "held"), 120,
                                     "the run ends", pump=[self.ui])
            finally:
                os.chmod(raw_dir, 0o700)
            raw = (mid.get("faults") or {}).get("raw_log") or (done.get("faults") or {}).get("raw_log") or {}
            out = {"raw_log": raw, "task": {k: (done.get("task") or {}).get(k) for k in ("status", "last_result")},
                   "raw_dir_mode_during": "0500"}
            assert raw.get("storage_error") and "raw log 저장 장애" in (raw.get("text") or ""), out
            judged = (((done.get("task") or {}).get("last_result") or {}).get("judged") or {})
            assert judged.get("exit_confirmed") is True and judged.get("exit_status") == 0, ("the run must "
                                                                                             "continue", out)
            return out

        def shutdown_b() -> dict:
            done = b.shutdown()
            result = h.shutdown_result(done.stdout)
            assert done.returncode == 0 and result.get("verified") is True, done.stdout[-500:]
            return {"verified": True}

        r.run("X6.run_cap_64mib_run_continues", cap)
        r.run("X6.restart_keeps_task_spec_worktree", restart_keeps_task, requires=["X6.run_cap_64mib_run_continues"])
        r.run("X6.project_cap_reduced_fixture", project_cap_fixture)
        r.run("X6.start_fault_sandbox", start_b)
        r.run("X6.raw_log_store_failure_shown", raw_log_store_fault, requires=["X6.start_fault_sandbox"])
        r.run("X6.metadata_hold_and_pause_store_failure", metadata_and_pause, requires=["X6.start_fault_sandbox"])
        r.run("X6.pause_store_failure_visible_at_170", pause_store_failure_visible,
              requires=["X6.metadata_hold_and_pause_store_failure"])
        r.run("X6.metadata_restored", metadata_restored, requires=["X6.metadata_hold_and_pause_store_failure"])
        r.run("X6.shutdown", shutdown_b, requires=["X6.start_fault_sandbox"])


# ============================================================================================ X7
class X7TakeoverRestart(Fault):
    """C-D52: takeover of a delivered worker command, then backend restart: durable unknown, never replayed."""

    name = "X7"

    def steps(self) -> None:
        sb = self.sb = self.sandbox("t")
        count = sb.tmp / "x7-count.txt"
        second = sb.tmp / "x7-second.txt"
        first_cmd = f"echo X7ONCE >> {count}; sleep 400; echo X7END"
        second_cmd = f"echo X7SECOND >> {second}"
        state: dict[str, Any] = {}
        self.manager_says("x7work", lambda r: self.call("manager", "to_worker", {
            "kind": "work", "message": "cw16 X7: run the two commands in order", "task_id": None, "cancel": None,
            "run": None, "analysis": None, "spec": {"goal": "cw16 X7", "paths": ["notes/"]},
            "commands": [first_cmd, second_cmd]}))
        self.provider.on(lambda r: (r.injected or {}).get("kind") == "task"
                         and ((r.injected or {}).get("payload") or {}).get("kind") == "work",
                         lambda r: self.call("worker", "terminal", {"command": first_cmd}), role="worker",
                         once=True, name="x7-first")
        self.worker_says("x7second", lambda r: self.call("worker", "terminal", {"command": second_cmd}))
        self.manager_says("x7status", lambda r: self.call("manager", "workbench_status", {"task_id": None}))
        r = self.runner

        def new_result(before: dict, timeout: float) -> dict | None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.ui is not None:
                    self.ui.pump(0.3)
                else:
                    time.sleep(0.3)
                new = {k: v for k, v in self.provider.tool_results.items() if k not in before}
                if new:
                    return next(iter(new.values()))
            return None

        def delivered() -> dict:
            self.start(sb)
            self.open_ui(sb)
            state["results0"] = dict(self.provider.tool_results)
            self.type_to(sb, "manager_omp", "x7work")
            assert h.wait_file(count, contains="X7ONCE", pump=[self.ui], timeout=90), "the command was not delivered"
            snap = sb.wait_status(lambda s: s["panes"]["host_shell"].get("operated_by") == "worker", 20,
                                  "worker command", pump=[self.ui])
            shell = snap["panes"]["host_shell"]["shell"]
            state["task_id"] = (snap.get("task") or {}).get("task_id")
            state["request"] = {k: shell.get(k) for k in ("request_id", "phase", "input_owner", "owner_epoch")}
            return {"shell": state["request"], "operated_by": snap["panes"]["host_shell"].get("operated_by"),
                    "task": {k: (snap.get("task") or {}).get(k) for k in ("task_id", "kind", "status")}}

        def takeover() -> dict:
            self.focus(sb, "host_shell")
            self.prefix(b"t")
            requested = sb.wait_status(lambda s: s["panes"]["host_shell"]["shell"]["takeover_requested"], 20,
                                       "takeover requested", pump=[self.ui])
            # the worker's first terminal call answers after its fixed 120 s wait (running) or earlier
            first = new_result({k: v for k, v in self.provider.tool_results.items()
                                if k in state["results0"] or not isinstance(v, dict) or "command_id" not in v}, 150)
            before = dict(self.provider.tool_results)
            self.type_to(sb, "worker_omp", "x7second")
            second_result = new_result(before, 150)
            self.focus(sb, "host_shell")
            self.prefix(b"c", settle=1.0)
            time.sleep(1.5)
            confirmed = sb.status()
            shell_r = requested["panes"]["host_shell"]["shell"]
            shell_c = confirmed["panes"]["host_shell"]["shell"]
            out = {"requested": {k: shell_r.get(k) for k in ("takeover_requested", "input_owner", "owner_epoch",
                                                             "held_reasons", "phase", "request_id")},
                   "first_terminal_result": {k: (first or {}).get(k) for k in ("status", "reason", "exit_code",
                                                                              "command_id")},
                   "second_send_result": second_result, "second_ran": second.exists(),
                   "confirm": {k: shell_c.get(k) for k in ("takeover_confirmed", "parent_mode", "phase",
                                                           "input_owner")},
                   "confirm_ui": self.ui.excerpt("인수")[:3], "count_lines": len(lines_of(count))}
            state["takeover"] = out
            assert shell_r["input_owner"] == "user" and "takeover_requested" in (shell_r.get("held_reasons") or []), out
            assert second_result is not None and second_result.get("status") != "exited" and not second.exists(), \
                ("a new automatic send after the takeover request", out)
            assert shell_c.get("takeover_confirmed") is True, out
            return out

        def restart() -> dict:
            killed = self.kill_backend(sb)
            time.sleep(2)
            done = sb.start()
            assert done.returncode == 0, done.stdout[-600:]
            snap = sb.wait_ready()
            startup = snap.get("startup") or {}
            out = {"kill": killed, "classification": startup.get("classification"), "survivors": [
                {k: s.get(k) for k in ("survivor_id", "name", "comm", "stoppable", "state")}
                for s in startup.get("survivors") or []], "outbox_lost": startup.get("outbox_lost"),
                "task": {k: (snap.get("task") or {}).get(k) for k in ("task_id", "status", "held_reason")}}
            assert out["classification"] == "same_boot_crash", out
            assert out["task"]["task_id"] == state["task_id"], out
            return out

        def unknown_no_replay() -> dict:
            self.open_ui(sb)
            before = dict(self.provider.tool_results)
            self.type_to(sb, "manager_omp", "x7status")
            status_result = new_result(before, 60) or {}
            runs = ((status_result.get("task") or {}).get("commands_run")) or []
            journal = self.handoffs(sb)
            started = [rec for rec in journal if rec.get("type") == "terminal_started"]
            time.sleep(15)
            mine = [run for run in runs if "X7ONCE" in json.dumps(run)]
            out = {"commands_run": [{k: run.get(k) for k in ("status", "exit_code", "command_id")} for run in runs][:4],
                   "notes": (status_result.get("task") or {}).get("commands_run_notes"),
                   "journal_terminal_started": len(started),
                   "count_lines_after_restart": len(lines_of(count)), "second_ran": second.exists()}
            assert out["count_lines_after_restart"] == 1 and not out["second_ran"], ("replayed", out)
            assert mine and all(run.get("status") == "unknown" for run in mine), ("the old command must be shown "
                                                                                  "unknown after the restart", out)
            return out

        def shutdown() -> dict:
            if self.ui is not None:
                self.ui.close()
                self.ui = None
            done = sb.shutdown()
            result = h.shutdown_result(done.stdout)
            out = {"exit": done.returncode, "verified": result.get("verified"), "problems": result.get("problems")}
            # the old shell's processes (the 400 s sleep) are previous survivors: unverified is the right answer
            assert (done.returncode == 0) == (result.get("verified") is True), out
            return out

        r.run("X7.command_delivered", delivered)
        r.run("X7.takeover_holds_new_sends", takeover, requires=["X7.command_delivered"])
        r.run("X7.backend_restart", restart, requires=["X7.command_delivered"])
        r.run("X7.old_command_unknown_no_replay", unknown_no_replay, requires=["X7.backend_restart"])
        r.run("X7.shutdown", shutdown, requires=["X7.backend_restart"])


# ============================================================================================ X5 (VM)
class VmReboot:
    """X5: ``vm_reboot_driver.py host`` (guest reboot only; the VM is started and stopped by the driver)."""

    name = "X5"

    def execute(self) -> dict:
        report = h.ScenarioReport("fault-x5-vm-reboot", run_id=RUN_ID, scenario_id="X5")
        out_dir = report.path().parent / "vm"
        argv = [sys.executable, str(h.REPO / "tests/integration/vm_reboot_driver.py"), "host", str(out_dir)]
        started = time.monotonic()
        try:
            done = subprocess.run(argv, cwd=h.REPO, env=h.clean_base_env(HOME=str(Path(h._user_bin("omp")).parents[2]),
                                                                          WB_CW16_RUN_ID=RUN_ID,
                                                                          PYTHONDONTWRITEBYTECODE="1"),
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3000)
            report.command(["python", "tests/integration/vm_reboot_driver.py", "host", "<dir>"], done.returncode,
                           done.stdout, done.stderr, time.monotonic() - started)
            vm = json.loads((out_dir / "vm-reboot-report.json").read_text())
            verdict = vm.get("verdict") or {}
            report.step("X5.vm_reboot", h.PASS if done.returncode == 0 and verdict.get("result") == "pass" else h.FAIL,
                        observed={"failed": verdict.get("failed"), "checks": len(verdict.get("checks") or []),
                                  "manifest": vm.get("manifest"), "reboots": vm.get("reboots"),
                                  "report": str(out_dir / "vm-reboot-report.json")})
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            report.step("X5.vm_reboot", h.FAIL, error=f"{type(exc).__name__}: {exc}")
        report.write()
        return {name: step.get("status") for name, step in report.data["steps"].items()}


SCENARIOS: dict[str, Callable[[], Any]] = {"X1": X1Detach, "X2": X2Restart, "X2R": Cw19RestartRerun,
                                           "X3": X3ModelFault, "X4": X4Shutdown, "X5": VmReboot,
                                           "X6": X6Storage, "X7": X7TakeoverRestart}


def write_matrix(run_id: str = RUN_ID) -> Path:
    directory = h.REPORT_DIR / run_id
    matrix: dict[str, Any] = {"run_id": run_id, "written_at": h.iso_now(), "scenarios": {}}
    for path in sorted(directory.glob("fault-*.json")):
        if path.name in ("fault-matrix.json",) or path.name.endswith("-probe.json"):
            continue
        data = json.loads(path.read_text())
        matrix["scenarios"][data.get("context", {}).get("scenario_id") or data["scenario"]] = {
            "result": data.get("result"),
            "steps": {name: step.get("status") for name, step in data.get("steps", {}).items()},
            "unknowns": data.get("unknowns")}
    target = directory / "fault-matrix.json"
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(matrix, indent=1, ensure_ascii=False))
    return target


# ============================================================================================ unittest
@unittest.skipIf(SKIP, SKIP or "")
class Cw16FaultScenarios(unittest.TestCase):
    """One test per scenario; a scenario fails when any step is not pass / n/a."""

    def _run(self, name: str) -> None:
        if name == "X5" and not VM:
            self.skipTest("X5 needs the isolated VM: set WB_CW16_VM=1 (host:reboot-vm)")
        if name != "X5" and name not in FAULTS:
            self.skipTest(f"filtered out by WB_CW16_FAULTS ({name})")
        steps = SCENARIOS[name]().execute()
        print(f"\n[cw16-fault] {name}: {steps}", file=sys.stderr)
        bad = {k: v for k, v in steps.items() if v not in (h.PASS, h.NA)}
        self.assertEqual(bad, {}, f"{name}: see {h.REPORT_DIR / RUN_ID}")
        self.assertTrue(steps, name)

    def test_x1_detach_with_active_work(self):
        self._run("X1")

    def test_x2_backend_sigkill_restart(self):
        self._run("X2")

    def test_x2r_l_cw19_restart_rerun(self):
        self._run("X2R")

    def test_x3_model_fault_holds(self):
        self._run("X3")

    def test_x4_shutdown_confirmation(self):
        self._run("X4")

    def test_x5_vm_reboot(self):
        self._run("X5")

    def test_x6_storage_faults(self):
        self._run("X6")

    def test_x7_takeover_during_delivery_restart(self):
        self._run("X7")


def tearDownModule() -> None:
    if not SKIP:
        print(f"\n[cw16-fault] matrix: {write_matrix()}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
