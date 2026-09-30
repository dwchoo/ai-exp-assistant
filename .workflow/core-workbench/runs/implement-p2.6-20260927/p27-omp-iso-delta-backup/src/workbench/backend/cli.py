"""Product entrypoint: ``start``, ``attach``, ``status`` and ``shutdown``.

``start`` attaches to a backend already serving the data dir; otherwise it
checks start requirements (Bash, then sh; OMP; bridge extension), launches a
detached backend in its own session and attaches. Exit codes: 0 success,
1 failure, 2 start requirement missing (no backend started), 3 not running.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import sys
import time

from workbench.backend.client import ClientError, NotRunning, UiClient, run_attach
from workbench.backend.launcher import LaunchPlan, StartRequirementError, build_plan
from workbench.backend.paths import DataDirError, DataLayout, InstanceLock, ensure_private_dir, resolve_data_dir
from workbench.contracts.ui_v1 import ClientType

EXIT_FAILURE, EXIT_REQUIREMENT, EXIT_NOT_RUNNING = 1, 2, 3
START_TIMEOUT = 120.0


def _layout(args: argparse.Namespace) -> DataLayout:
    root = resolve_data_dir(args.data_dir, os.environ)
    ensure_private_dir(root)
    layout = DataLayout(root)
    layout.check_socket_paths()
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
    isolation = snapshot.get("omp_isolation")
    if isolation:
        drift = isolation.get("version_drift") or {}
        drift_text = (" evidence " + ", ".join(f"{name}={version}" for name, version in sorted(drift.items()))
                      if drift else "")
        print(f"omp isolation: {isolation.get('state')} ({backend.get('omp_version')}{drift_text})", file=stream)
        if isolation.get("warning"):
            print(f"WARNING: {isolation['warning']}", file=stream)
        for note in isolation.get("notes") or ():
            print(f"  note: {note}", file=stream)


def _attach(layout: DataLayout, args: argparse.Namespace) -> int:
    if getattr(args, "plain", False):
        return run_attach(layout.ui_socket)
    from workbench.ui.product import run_product
    return run_product(layout.ui_socket)


def cmd_start(args: argparse.Namespace) -> int:
    layout = _layout(args)
    running = _running_snapshot(layout)
    if running is not None:
        print(f"backend already running (pid {running['backend']['pid']}); attaching instead of starting")
    else:
        try:
            plan = build_plan(os.environ, omp=args.omp, omp_args=args.omp_arg or (),
                              bridge_extension=args.bridge_extension)
        except StartRequirementError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_REQUIREMENT
        print(f"starting backend: shell {plan.shell.kind} ({plan.shell.executable}), {plan.omp_version}")
        pid = spawn_backend(layout, plan, os.getcwd())
        running = _wait_started(layout, pid, args.timeout)
        if running is None:
            print(f"backend did not become available; see {layout.log}", file=sys.stderr)
            return EXIT_FAILURE
        if running["backend"]["pid"] != pid:
            print(f"another start won the race; using backend pid {running['backend']['pid']}")
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
    return 0


def cmd_shutdown(args: argparse.Namespace) -> int:
    layout = _layout(args)
    try:
        client = UiClient(layout.ui_socket, name="workbench-cli", timeout=10)
    except NotRunning:
        print(f"no backend is running for {layout.root}", file=sys.stderr)
        return EXIT_NOT_RUNNING
    with client:
        pending = client.request(ClientType.SHUTDOWN_REQUEST)
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
        confirmed = client.request(ClientType.SHUTDOWN_CONFIRM, token=pending["token"])
        if not confirmed.get("ok"):
            print(f"shutdown refused: {confirmed.get('reason')}: {confirmed.get('detail')}", file=sys.stderr)
            return EXIT_FAILURE
        deadline = time.monotonic() + 30
        while client.closing is None and time.monotonic() < deadline:
            if not client.pump(0.2):
                break
        result = (client.closing or {}).get("result")
    if args.json:
        print(json.dumps({"shutdown": result}, sort_keys=True))
    else:
        print(f"shutdown result: {json.dumps(result, sort_keys=True)}")
    return 0 if result and result.get("verified") else EXIT_FAILURE


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
