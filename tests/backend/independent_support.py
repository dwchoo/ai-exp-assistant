"""Shared helpers for the independent CW-17 tests (p27-cw17-test-01).

Builds on ``live_harness`` but fixes its normal-path ordering: the backend is
stopped through the confirmed shutdown and its exit is awaited *before* any
exact-identity fallback runs, so a slow but clean exit is never SIGKILLed and
a real leak is reported as a failure.
"""
from __future__ import annotations

import os
from pathlib import Path
import time

from live_harness import LiveBackend, find_omp, kill_exact, session_members, stat_fields, ticks  # noqa: F401
from workbench.backend.client import UiClient
from workbench.contracts.v1 import PaneId

LEAK_KEYS = ("identities", "session_members", "backends", "sockets")


def residue(live: LiveBackend) -> dict:
    return {key: value for key, value in live.leaks().items() if value}


def stop_and_verify(live: LiveBackend, timeout: float = 15.0) -> tuple[dict, str]:
    """Confirmed shutdown through the real entrypoint, then wait for OS-level exit."""
    output = ""
    if live.backend_processes():
        result = live.shutdown()
        output = result.stdout + result.stderr
    deadline = time.monotonic() + timeout
    while residue(live) and time.monotonic() < deadline:
        time.sleep(0.05)
    return residue(live), output


def finish(test, live: LiveBackend, extra_owned: list[tuple[int, int]] | None = None) -> None:
    """Normal path: shutdown + wait; leftovers are a test failure (then force-cleaned)."""
    for client in live.clients:
        client.close()
    left, _ = stop_and_verify(live)
    for pid, start in extra_owned or ():
        kill_exact(pid, start)
    fallback = live.cleanup()  # exact pidfd kill of anything still owned, provider, temp root
    test.assertEqual(left, {}, f"owned process/socket leak after confirmed shutdown: {left} / {fallback}")
    test.assertFalse(live.root.exists(), "owned temp root left behind")


def start_no_attach(test, live: LiveBackend, *extra: str) -> dict:
    result = live.cli(*live.start_args("--no-attach", *extra))
    test.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    snapshot = wait_status(live, lambda s: s["phase"] != "starting")
    live.remember(snapshot)
    test.assertEqual(snapshot["phase"], "ready", snapshot.get("reason"))
    return snapshot


def wait_status(live: LiveBackend, predicate, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    snapshot = None
    while time.monotonic() < deadline:
        for client in live.clients:
            client.drain(0)
        snapshot = live.status()
        if snapshot is not None and predicate(snapshot):
            return snapshot
        time.sleep(0.1)
    raise AssertionError(f"status predicate timeout: {snapshot}")


def wait_client(client: UiClient, predicate, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    snapshot = None
    while time.monotonic() < deadline:
        snapshot = client.snapshot()
        client.displays.clear()
        if predicate(snapshot):
            return snapshot
        client.pump(0.1)
    raise AssertionError(f"client predicate timeout: {snapshot}")


def settle(client: UiClient, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        client.pump(0.05)
    client.displays.clear()


def identity(snapshot: dict) -> dict:
    """Everything that must not change across detach/reattach."""
    panes = snapshot["panes"]
    shell = panes["host_shell"]["shell"]
    return {
        "backend": snapshot["backend"]["process"], "backend_session": snapshot["backend"]["session_id"],
        "started_at": snapshot["backend"]["started_at"],
        "panes": {name: (pane["process"], pane["session_id"], pane["generation"]) for name, pane in panes.items()},
        "bridge": {role: {k: snapshot["bridge"][role][k] for k in ("session_id", "generation", "pid",
                                                                    "pid_matches_pane")}
                   for role in ("manager", "worker")},
        "shell_parent": shell["parent"], "shell_generation": shell["generation"],
        "input_owner": shell["input_owner"], "owner_epoch": shell["owner_epoch"],
        "control_mode": shell["parent_mode"], "supervisor": shell["supervisor"],
        "request_id": shell["request_id"], "focus": snapshot["focus"],
    }


def shell_view(snapshot: dict) -> dict:
    pane = snapshot["panes"]["host_shell"]
    shell = pane["shell"]
    view = {k: shell[k] for k in ("input_owner", "owner_epoch", "parent_mode", "phase", "takeover_requested",
                                  "takeover_confirmed", "error")}
    view.update(dropped=pane["dropped_input_bytes"], problem=pane["last_input_problem"],
                queued=pane["queued_input_bytes"])
    return view


def comm(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return None


def shell_children(shell_pid: int) -> dict[int, tuple[str | None, int]]:
    """pid -> (comm, start ticks) for live processes in the host shell's session, minus the shell."""
    return {pid: (comm(pid), start) for pid, start in session_members({shell_pid}).items() if pid != shell_pid}


def wait_file(path: Path, expected: str, timeout: float = 10.0, client: UiClient | None = None) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text() == expected:
            return expected
        if client is not None:
            client.pump(0.05)
            client.displays.clear()
        else:
            time.sleep(0.05)
    return path.read_text() if path.exists() else None


def parent_pid(pid: int) -> int | None:
    fields = stat_fields(pid)
    return int(fields[1]) if fields else None


def session_of(pid: int) -> int | None:
    try:
        return os.getsid(pid)
    except OSError:
        return None


__all__ = ["LiveBackend", "PaneId", "UiClient", "find_omp", "kill_exact", "session_members", "ticks",
           "residue", "stop_and_verify", "finish", "start_no_attach", "wait_status", "wait_client", "settle",
           "identity", "shell_view", "comm", "shell_children", "wait_file", "parent_pid", "session_of"]
