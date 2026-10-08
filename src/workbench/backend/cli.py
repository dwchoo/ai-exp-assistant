"""Product entrypoint: ``start``, ``attach``, ``status``, ``confirm-boot`` and ``shutdown``.

``start`` attaches to a backend already serving the data dir; otherwise it
checks start requirements (Bash, then sh; OMP; bridge extension; the user's
OMP auth store and a usable Workbench OMP home, C-D64), launches a detached
backend in its own session and attaches. ``confirm-boot`` (CW-19, C-D58)
shows the environment and execution conditions after a changed boot and
confirms it; until then Workbench starts no automatic work. Exit codes:
0 success, 1 failure, 2 start requirement missing (no backend started),
3 not running.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import sys
import time
from typing import Mapping

from workbench.backend.client import ClientError, NotRunning, UiClient, run_attach
from workbench.backend.launcher import ISOLATION_CHECK_TIMEOUT, LaunchPlan, StartRequirementError, build_plan
from workbench.backend.omp_home import OmpHomeError, auth_guidance, config_dir_value, omp_root
from workbench.backend.paths import DataDirError, DataLayout, InstanceLock, ensure_private_dir, resolve_data_dir
from workbench.contracts.ui_v1 import ClientType

EXIT_FAILURE, EXIT_REQUIREMENT, EXIT_NOT_RUNNING = 1, 2, 3
START_TIMEOUT = 120.0
# The backend checks both roles one after the other (each bounded).
ISOLATION_WAIT = 2 * ISOLATION_CHECK_TIMEOUT + 10.0


def _layout(args: argparse.Namespace, *, create: bool = True) -> DataLayout:
    """The data dir layout; ``create=False`` creates nothing (an existing dir is still checked)."""
    root = resolve_data_dir(args.data_dir, os.environ)
    layout = DataLayout(root)
    layout.check_socket_paths()
    if create or os.path.lexists(root):
        ensure_private_dir(root)
    return layout


def _running_snapshot(layout: DataLayout) -> dict | None:
    try:
        with UiClient(layout.ui_socket, name="workbench-cli", timeout=5) as client:
            return client.snapshot()
    except (NotRunning, ClientError, OSError):
        return None


def _bootstrap_source() -> str:
    # Import this package from its own location without exporting PYTHONPATH
    # into the backend environment inherited by OMP and the user shell.
    package_parent = str(Path(__file__).resolve().parents[2])
    return (f"import sys; sys.path.insert(0, {package_parent!r}); "
            "from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))")


def spawn_backend(layout: DataLayout, plan: LaunchPlan, project_dir: str) -> int:
    """Double-fork into a new session; return the backend PID (not our child)."""
    argv = [sys.executable, "-c", _bootstrap_source(), "_backend", "--data-dir", str(layout.root),
            "--project-dir", project_dir, *plan.to_argv()]
    read_end, write_end = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    intermediate = os.fork()
    if intermediate == 0:  # pragma: no cover - child side
        try:
            os.close(read_end)
            os.setsid()
            backend = os.fork()
            if backend == 0:
                os.close(write_end)
                devnull = os.open(os.devnull, os.O_RDWR)
                log = os.open(layout.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                os.dup2(devnull, 0)
                os.dup2(log, 1)
                os.dup2(log, 2)
                os.closerange(3, 1 << 16)
                os.chdir(project_dir)
                os.execv(sys.executable, argv)
            os.write(write_end, str(backend).encode())
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(write_end)
    try:
        raw = b""
        while chunk := os.read(read_end, 64):
            raw += chunk
    finally:
        os.close(read_end)
    os.waitpid(intermediate, 0)
    if not raw.isdigit():
        raise RuntimeError("backend launch failed before exec")
    return int(raw)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            return stream.read().rsplit(b") ", 1)[1].split()[0] not in {b"Z", b"X"}
    except (OSError, IndexError):
        return False


def _wait_started(layout: DataLayout, pid: int, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = _running_snapshot(layout)
        if snapshot is not None and snapshot["phase"] != "starting":
            return snapshot
        if snapshot is None and not _alive(pid) and not InstanceLock(layout.lock).held_elsewhere():
            # Our backend exited and no other backend holds the data dir.
            return None
        select.select([], [], [], 0.1)
    return None


def _wait_isolation(layout: DataLayout, snapshot: dict, timeout: float = ISOLATION_WAIT,
                    stream=sys.stdout) -> dict:
    """Wait (bounded) for the start-up isolation check so the summary shows its result."""
    deadline = time.monotonic() + timeout
    announced = False
    while (snapshot.get("omp_isolation") or {}).get("state") == "pending" and time.monotonic() < deadline:
        if not announced:
            print("waiting for the OMP isolation check ...", file=stream, flush=True)
            announced = True
        select.select([], [], [], 0.2)
        fresh = _running_snapshot(layout)
        if fresh is None:
            break
        snapshot = fresh
    return snapshot


def _print_summary(snapshot: dict, stream=sys.stdout) -> None:
    backend, panes = snapshot["backend"], snapshot["panes"]
    shell = panes.get("host_shell", {}).get("shell", {})
    print(f"backend pid {backend['pid']} phase {snapshot['phase']}"
          + (f" ({snapshot['reason']})" if snapshot.get("reason") else ""), file=stream)
    print(f"data dir {backend['data_dir']}", file=stream)
    for name, pane in panes.items():
        process = pane.get("process") or {}
        print(f"  {name}: pid {process.get('pid')} alive={pane.get('alive')} owner={pane.get('input_owner')}",
              file=stream)
    if shell:
        print(f"  shell: {shell.get('kind')} {shell.get('executable')} mode={shell.get('parent_mode')}",
              file=stream)
    usage = snapshot.get("usage")
    if isinstance(usage, dict) and usage.get("task_id"):  # C-AC-21 (CW-16 D-B2-3)
        model = usage.get("model") or {}
        tokens = next((f"{value} tok" + (" (추정)" if key == "tokens_estimated" else "")
                       for key in ("tokens_observed", "tokens_estimated")
                       if type(value := model.get(key)) is int), "미확인")
        print(f"사용량: Task {usage.get('task_id')} run {usage.get('runs_started')} "
              f"(재시도 {usage.get('retries_used')}/{usage.get('retry_limit')}) · 60s 점검 "
              f"{usage.get('review_count') if usage.get('review_count') is not None else '-'} · 모델 {tokens}",
              file=stream)
    isolation = snapshot.get("omp_isolation")
    if isolation:
        drift = isolation.get("version_drift") or {}
        drift_text = (" evidence " + ", ".join(f"{name}={version}" for name, version in sorted(drift.items()))
                      if drift else "")
        print(f"omp isolation: {isolation.get('state')} ({backend.get('omp_version')}{drift_text})", file=stream)
        if isolation.get("state") == "pending":
            print("  note: the isolation check has no result yet; run 'status' to see it", file=stream)
        if isolation.get("warning"):
            print(f"WARNING: {isolation['warning']}", file=stream)
        for note in isolation.get("notes") or ():
            print(f"  note: {note}", file=stream)


def _print_recovery(snapshot: dict, stream=None) -> None:
    """CW-19: boot confirmation, start-up reconcile, survivors, holds and faults (user-facing, Korean)."""
    stream = stream or sys.stdout
    boot = snapshot.get("boot") or {}
    if boot.get("confirmation_required") and boot.get("reason") == "reconcile_failed":
        print("재시작 대조 실패: 이전 backend의 process·Task·메시지 상태를 확인하지 못했습니다. "
              "'python -m workbench confirm-boot' 로 환경을 확인하기 전까지 Workbench 자동 동작은 보류됩니다",
              file=stream)
    elif boot.get("confirmation_required"):
        print(f"부팅 확인 대기 ({boot.get('reason') or 'reboot'}): 'python -m workbench confirm-boot' 로 환경을 확인하기 "
              "전까지 Workbench 자동 동작은 보류됩니다", file=stream)
    startup = snapshot.get("startup") or {}
    classification = startup.get("classification")
    if classification and classification != "fresh":
        run = startup.get("run") or {}
        line = f"backend 재시작 대조: {classification}"
        if run.get("run_id"):
            line += f" | 이전 run {run.get('run_id')} 상태 {run.get('state')} (재실행·재전송 없음)"
        if startup.get("outbox_lost_count"):
            line += f" | 보내지 못한 메시지 {startup['outbox_lost_count']}건 (재전송 없음)"
        print(line, file=stream)
    for item in startup.get("survivors") or ():
        if item.get("state") not in ("alive", "stop_unconfirmed"):
            continue
        how = "manager가 stop_survivor로 종료 가능" if item.get("stoppable") else f"표시만 ({item.get('why_not')})"
        print(f"  이전 backend가 남긴 process {item.get('survivor_id')}: {item.get('name')} pid {item.get('pid')} "
              f"{item.get('comm') or ''} — {how}", file=stream)
    holds = [item.get("reason") for item in snapshot.get("holds") or ()]
    if holds:
        print(f"자동 동작 보류: {', '.join(str(reason) for reason in holds)}", file=stream)
    faults = snapshot.get("faults") or {}
    metadata = faults.get("metadata")
    if metadata:
        print(f"metadata 저장 장애: {', '.join(metadata.get('sources', {}).values())} — 실행·관측은 계속, 새 자동 작업 보류",
              file=stream)
    for role, model in sorted((faults.get("model") or {}).items()):
        if isinstance(model, dict) and model.get("state") == "error":
            print(f"모델 오류({role}): 자동 작업 보류, 실험·관측 계속 (다음 정상 응답에서 해제)", file=stream)
    raw = faults.get("raw_log")
    if isinstance(raw, dict) and raw.get("text"):
        print(f"raw log: {raw['text']}", file=stream)


def _attach(layout: DataLayout, args: argparse.Namespace) -> int:
    if getattr(args, "plain", False):
        return run_attach(layout.ui_socket)
    from workbench.ui.product import run_product
    return run_product(layout.ui_socket)


def omp_home_location(root: Path, environment: Mapping[str, str]) -> None:
    """C-D64: the data dir must not overlap the user's OMP dirs (pure path check, creates nothing)."""
    try:
        config_dir_value(omp_root(root), environment)
    except OmpHomeError as exc:
        raise StartRequirementError(f"{exc}\nNo backend was started.") from exc


def omp_home_requirements(layout: DataLayout, environment: Mapping[str, str]) -> None:
    """C-D64: refuse to start without the user's OMP auth store (never created by Workbench).

    Also refused when the store's location cannot be resolved unambiguously
    from the user's own OMP variables; a provider API key variable admits a
    start without any store (the OMPs then run without the shared login).
    """
    try:
        guidance = auth_guidance(environment)
    except OmpHomeError as exc:
        raise StartRequirementError(f"{exc}\nNo backend was started.") from exc
    if guidance:
        raise StartRequirementError(guidance + "\nNo backend was started.")
    omp_home_location(layout.root, environment)


def cmd_start(args: argparse.Namespace) -> int:
    # Validated before anything is created: a refused start leaves no new directory (C-D64).
    try:
        omp_home_location(resolve_data_dir(args.data_dir, os.environ), os.environ)
    except StartRequirementError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REQUIREMENT
    layout = _layout(args, create=False)
    running = _running_snapshot(layout) if os.path.lexists(layout.root) else None
    if running is not None:
        print(f"backend already running (pid {running['backend']['pid']}); attaching instead of starting")
    else:
        try:
            plan = build_plan(os.environ, omp=args.omp, omp_args=args.omp_arg or (),
                              bridge_extension=args.bridge_extension)
            omp_home_requirements(layout, os.environ)
        except StartRequirementError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_REQUIREMENT
        ensure_private_dir(layout.root)
        print(f"starting backend: shell {plan.shell.kind} ({plan.shell.executable}), {plan.omp_version}")
        pid = spawn_backend(layout, plan, os.getcwd())
        running = _wait_started(layout, pid, args.timeout)
        if running is None:
            print(f"backend did not become available; see {layout.log}", file=sys.stderr)
            return EXIT_FAILURE
        if running["backend"]["pid"] != pid:
            print(f"another start won the race; using backend pid {running['backend']['pid']}")
    running = _wait_isolation(layout, running)
    _print_summary(running)
    if args.no_attach:
        return 0
    return _attach(layout, args)


def cmd_attach(args: argparse.Namespace) -> int:
    layout = _layout(args)
    try:
        return _attach(layout, args)
    except NotRunning:
        print(f"no backend is running for {layout.root}; use 'start'", file=sys.stderr)
        return EXIT_NOT_RUNNING


def cmd_status(args: argparse.Namespace) -> int:
    layout = _layout(args)
    snapshot = _running_snapshot(layout)
    if snapshot is None:
        if args.json:
            print(json.dumps({"running": False, "data_dir": str(layout.root)}))
        else:
            print(f"no backend is running for {layout.root}")
        return EXIT_NOT_RUNNING
    if args.json:
        print(json.dumps({"running": True, "snapshot": snapshot}, sort_keys=True))
    else:
        _print_summary(snapshot)
        _print_recovery(snapshot)
    return 0


def _worktrees(layout: DataLayout) -> list[str]:
    try:
        return sorted(entry.name for entry in os.scandir(layout.workflow / "worktrees"))[:20]
    except OSError:
        return []


def _print_boot_conditions(layout: DataLayout, snapshot: dict, stream=None) -> None:
    """C-AC-23: the environment and execution conditions shown before the user confirms the boot."""
    stream = stream or sys.stdout
    boot, backend = snapshot.get("boot") or {}, snapshot.get("backend") or {}
    shell = ((snapshot.get("panes") or {}).get("host_shell") or {}).get("shell") or {}
    isolation = snapshot.get("omp_isolation") or {}
    automation = snapshot.get("automation") or {}
    task = snapshot.get("task") or {}
    print("== 부팅 확인: 환경·실행 조건 ==", file=stream)
    print(f"boot marker: 기록 {boot.get('recorded_boot_id')} -> 현재 {boot.get('boot_id')} "
          f"(사유 {boot.get('reason')})", file=stream)
    print(f"data dir: {backend.get('data_dir')}", file=stream)
    print(f"project dir: {backend.get('project_dir')}", file=stream)
    print(f"host shell: {shell.get('kind')} {shell.get('executable')} (mode {shell.get('parent_mode')})", file=stream)
    print(f"OMP: {backend.get('omp_version')} | 격리 확인: {isolation.get('state')}"
          + (f" ({isolation.get('warning')})" if isolation.get("warning") else ""), file=stream)
    print(f"자동화: {automation.get('state')}" + (" (일시정지 유지)" if automation.get("paused") else ""), file=stream)
    if task:
        print(f"Task: {task.get('kind')} {task.get('task_id')} 상태 {task.get('status')}"
              + (f" (보류 사유 {task.get('held_reason')})" if task.get("held_reason") else "")
              + (f' "{task.get("summary")}"' if task.get("summary") else ""), file=stream)
    worktrees = _worktrees(layout)
    if worktrees:
        print(f"worktree: {', '.join(worktrees)}", file=stream)
    _print_recovery({**snapshot, "boot": {}}, stream)
    print("확인하면 Workbench 자동 동작(worker 지시 전달, worker terminal, 감시·복구 알림, 60초 점검, 새 실험)이 다시 "
          "허용됩니다. 재부팅 전 실험은 다시 실행되지 않습니다.", file=stream)


def cmd_confirm_boot(args: argparse.Namespace) -> int:
    layout = _layout(args, create=False)
    snapshot = _running_snapshot(layout) if os.path.lexists(layout.root) else None
    if snapshot is None:
        print(f"no backend is running for {layout.root}; use 'start'", file=sys.stderr)
        return EXIT_NOT_RUNNING
    boot = snapshot.get("boot") or {}
    if not boot.get("confirmation_required"):
        print("부팅 확인이 필요하지 않습니다 (대기 중인 확인 없음)", file=sys.stderr)
        return EXIT_FAILURE
    _print_boot_conditions(layout, snapshot)
    boot_id = boot.get("boot_id")
    if not isinstance(boot_id, str) or not boot_id:
        print("현재 boot marker를 읽을 수 없어 확인할 수 없습니다; 자동 동작은 계속 보류됩니다", file=sys.stderr)
        return EXIT_FAILURE
    if not args.yes:
        if not sys.stdin.isatty():
            print("refusing to confirm the boot without confirmation (use --yes)", file=sys.stderr)
            return EXIT_FAILURE
        if input("이 환경으로 계속할까요? [y/N] ").strip().lower() != "y":
            print("cancelled")
            return EXIT_FAILURE
    try:
        with UiClient(layout.ui_socket, name="workbench-cli", timeout=10) as client:
            answer = client.request(ClientType.CONFIRM_BOOT, boot_id=boot_id)
    except (NotRunning, ClientError, OSError) as exc:
        print(f"confirm-boot failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    if not answer.get("ok"):
        print(f"confirm-boot refused: {answer.get('reason')}: {answer.get('detail')}", file=sys.stderr)
        return EXIT_FAILURE
    if args.json:
        print(json.dumps({"confirmed": True, "boot": answer.get("boot")}, sort_keys=True))
    else:
        print(f"부팅 확인됨 ({boot_id}): Workbench 자동 동작이 다시 허용됩니다")
    return 0


SHUTDOWN_RESULT_WAIT = 150.0  # used when an older backend does not send its own bound (``result_deadline``)
SHUTDOWN_PROGRESS_EVERY = 5.0  # seconds between the human form's progress lines (stderr)
BACKEND_GONE_WAIT = 10.0  # after a lost connection: how long to watch the backend's exact pid end


def _backend_state(ref: object, wait: float) -> str:
    """``ended``, ``alive`` or ``unknown`` for the backend's exact pid and start ticks (never signalled)."""
    if not isinstance(ref, Mapping) or type(ref.get("pid")) is not int or type(ref.get("start_ticks")) is not int:
        return "unknown"
    from workbench.app.recovery import observe
    deadline = time.monotonic() + wait
    while True:
        state = observe(ref["pid"], ref["start_ticks"])
        if state != "alive" or time.monotonic() >= deadline:
            return state
        time.sleep(0.1)


def cmd_shutdown(args: argparse.Namespace) -> int:
    """C-AC-22: show the active work, confirm, then wait for the backend's own result; a result that is not
    verified, or none at all, is never shown as success, and no failure ends in a traceback (CW-16 O4)."""
    layout = _layout(args)
    try:
        client = UiClient(layout.ui_socket, name="workbench-cli", timeout=10)
    except NotRunning:
        print(f"no backend is running for {layout.root}", file=sys.stderr)
        return EXIT_NOT_RUNNING
    except (OSError, ClientError) as exc:
        print(f"backend did not answer: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    result: dict | None = None
    lost: str | None = None
    backend_ref: object = None
    with client:
        try:
            pending = client.request(ClientType.SHUTDOWN_REQUEST)
        except (OSError, TimeoutError, ClientError) as exc:
            print(f"shutdown request failed (nothing was stopped): {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        if not pending.get("ok", True) or "token" not in pending:
            print(f"shutdown request refused: {pending.get('reason')}: {pending.get('detail')}", file=sys.stderr)
            return EXIT_FAILURE
        backend_ref = (pending.get("processes") or {}).get("backend")
        active = pending.get("active", [])
        print("active work:" if active else "no active work reported")
        for item in active:
            print(f"  - {json.dumps(item, sort_keys=True)}")
        if not args.yes:
            if not sys.stdin.isatty():
                print("refusing to shut down without confirmation (use --yes)", file=sys.stderr)
                return EXIT_FAILURE
            if input("stop the backend, both OMP sessions and the host shell? [y/N] ").strip().lower() != "y":
                print("cancelled")
                return EXIT_FAILURE
        wait = SHUTDOWN_RESULT_WAIT
        try:
            confirmed = client.request(ClientType.SHUTDOWN_CONFIRM, token=pending["token"])
        except TimeoutError:  # an older backend answers only when its close ends: keep waiting for the result
            confirmed = {"ok": True}
        except (OSError, ClientError) as exc:
            confirmed = {"ok": True}
            lost = f"{type(exc).__name__}: {exc}"
        if not confirmed.get("ok"):
            print(f"shutdown refused: {confirmed.get('reason')}: {confirmed.get('detail')}", file=sys.stderr)
            return EXIT_FAILURE
        bound = confirmed.get("result_deadline")
        if type(bound) in (int, float) and bound > 0:
            wait = float(bound)
        started = time.monotonic()
        deadline, next_progress = started + wait, started + SHUTDOWN_PROGRESS_EVERY
        if not args.json and client.closing is None and lost is None:
            print(f"종료 중: 진행 중인 OMP turn 중단과 process 정리를 기다립니다 (최대 {wait:.0f}초)", file=sys.stderr)
        while client.closing is None and lost is None:
            now = time.monotonic()
            if now >= deadline:
                lost = f"no result within {wait:.0f} s"
                break
            if not args.json and now >= next_progress:
                print(f"종료 중… {now - started:.0f}초 경과", file=sys.stderr)
                next_progress = now + SHUTDOWN_PROGRESS_EVERY
            try:
                if not client.pump(min(0.2, deadline - now)):
                    lost = "the backend closed the connection before its result"
            except (OSError, ClientError) as exc:
                lost = f"{type(exc).__name__}: {exc}"
        result = (client.closing or {}).get("result")
    if not isinstance(result, dict):
        result = None
    if result is None:
        # C-AC-22: no result is never a success; say whether the backend itself is gone (exact pid identity).
        backend = _backend_state(backend_ref, BACKEND_GONE_WAIT)
        cause = lost or "the backend sent no result"
        backend_text = {"ended": "backend process는 종료됨", "alive": "backend process가 아직 실행 중",
                        "unknown": "backend process 상태 불명"}[backend]
        if args.json:
            print(json.dumps({"shutdown": None, "unconfirmed": {"cause": cause, "backend": backend}},
                             sort_keys=True))
        else:
            print("shutdown result: null")
        print(f"종료 확인 실패: 종료 결과를 받지 못해 OMP·host shell 등의 종료를 확인할 수 없습니다 "
              f"({backend_text}; {cause}). 'status'로 확인하세요", file=sys.stderr)
        return EXIT_FAILURE
    if args.json:
        print(json.dumps({"shutdown": result}, sort_keys=True))
    else:
        print(f"shutdown result: {json.dumps(result, sort_keys=True)}")
        if not result.get("verified"):  # C-AC-22: never shown as success
            print("종료 확인 실패: 일부 process의 종료를 확인하지 못했습니다 "
                  f"({', '.join(result.get('problems') or ['result unknown'])})", file=sys.stderr)
    return 0 if result.get("verified") else EXIT_FAILURE


def cmd_backend(args: argparse.Namespace) -> int:
    from workbench.backend.service import main_backend
    return main_backend(args)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="omp-workbench", description=__doc__.splitlines()[0])
    sub = root.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--data-dir", help="data dir (default: $WORKBENCH_DATA_DIR or $XDG_STATE_HOME/omp-workbench)")
        return p

    start = common(sub.add_parser("start", help="start (or reuse) the backend and attach"))
    start.add_argument("--omp", help="OMP executable (default: $WORKBENCH_OMP or 'omp' on PATH)")
    start.add_argument("--omp-arg", action="append", help="extra argument passed to both OMP processes")
    start.add_argument("--bridge-extension", help="path to the G3 bridge extension")
    start.add_argument("--no-attach", action="store_true", help="start or reuse the backend without attaching")
    start.add_argument("--plain", action="store_true", help="attach with the minimal client instead of the product UI")
    start.add_argument("--timeout", type=float, default=START_TIMEOUT, help=argparse.SUPPRESS)
    start.set_defaults(handler=cmd_start)
    attach = common(sub.add_parser("attach", help="attach to the running backend"))
    attach.add_argument("--plain", action="store_true", help="use the minimal client instead of the product UI")
    attach.set_defaults(handler=cmd_attach)
    status = common(sub.add_parser("status", help="show backend status"))
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=cmd_status)
    confirm = common(sub.add_parser("confirm-boot", help="show the conditions after a reboot and confirm the boot"))
    confirm.add_argument("--yes", action="store_true", help="confirm without prompting")
    confirm.add_argument("--json", action="store_true")
    confirm.set_defaults(handler=cmd_confirm_boot)
    shutdown = common(sub.add_parser("shutdown", help="request and confirm a full backend shutdown"))
    shutdown.add_argument("--yes", action="store_true", help="confirm without prompting")
    shutdown.add_argument("--json", action="store_true")
    shutdown.set_defaults(handler=cmd_shutdown)
    backend = sub.add_parser("_backend", help=argparse.SUPPRESS)
    backend.add_argument("--data-dir", required=True)
    backend.add_argument("--project-dir", required=True)
    backend.add_argument("--shell-kind", required=True, choices=("bash", "sh"))
    backend.add_argument("--shell-path", required=True)
    backend.add_argument("--omp", required=True)
    backend.add_argument("--omp-version", required=True)
    backend.add_argument("--bridge-extension", required=True)
    backend.add_argument("--omp-arg", action="append")
    backend.set_defaults(handler=cmd_backend)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except DataDirError as exc:
        print(f"data dir error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
