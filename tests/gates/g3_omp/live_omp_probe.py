"""Start real OMP 18.2.10 processes and optionally run one bounded no-tools task.

The default probe checks only the two-process extension handshake. The opt-in
`--model-task-probe` submits one task to the worker, with a 45-second OMP session
limit and no tools; it reports only lifecycle counts and whether an exact response
marker appeared. A separate one-request read-only CLI check is `--auth-probe`.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socketserver
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from workbench.contracts.v1 import ActorRole, ControlEnvelope, MessageEvent, MessageKind

OMP_VERSION = "omp/18.2.10"
EXTENSION = REPO / "omp_bridge" / "g3" / "bridge.ts"


class BridgeHarness(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: Path, tokens: dict[str, str]):
        self.tokens = tokens
        self.condition = threading.Condition()
        self.peers: dict[str, dict[str, object]] = {}
        self.acks: dict[str, dict[str, object]] = {}
        self.events: list[dict[str, object]] = []
        self.consumed_events: set[int] = set()
        self.dropped_ack_ids: set[str] = set()
        self.discarded_ack_metadata: list[dict[str, object]] = []
        super().__init__(str(socket_path), PeerHandler)

    def peer(self, role: str, timeout: float = 20) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while role not in self.peers and time.monotonic() < deadline:
                self.condition.wait(deadline - time.monotonic())
            if role not in self.peers:
                raise TimeoutError(f"OMP extension did not connect for role={role}")
            return self.peers[role]

    def request(self, role: str, message: dict[str, object], timeout: float = 5) -> dict[str, object]:
        peer = self.peer(role)
        request_id = str(uuid4())
        frame = {**message, "requestId": request_id}
        with peer["write_lock"]:  # type: ignore[index]
            peer["socket"].sendall((json.dumps(frame) + "\n").encode())  # type: ignore[index,union-attr]
        deadline = time.monotonic() + timeout
        with self.condition:
            while request_id not in self.acks and time.monotonic() < deadline:
                self.condition.wait(deadline - time.monotonic())
            result = self.acks.pop(request_id, None)
        if result is None:
            raise TimeoutError(f"extension did not acknowledge {message.get('kind')}")
        return result

    def wait_event(self, role: str, name: str, timeout: float) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while time.monotonic() < deadline:
                for index, event in enumerate(self.events):
                    if (
                        index not in self.consumed_events
                        and event.get("role") == role
                        and event.get("name") == name
                    ):
                        self.consumed_events.add(index)
                        return event
                self.condition.wait(deadline - time.monotonic())
        raise TimeoutError("expected OMP lifecycle event was not observed")

    def event_count(self, role: str, name: str) -> int:
        with self.condition:
            return sum(event.get("role") == role and event.get("name") == name for event in self.events)


class PeerHandler(socketserver.StreamRequestHandler):
    server: BridgeHarness

    def _write(self, value: dict[str, object]) -> None:
        self.wfile.write((json.dumps(value) + "\n").encode())
        self.wfile.flush()

    def handle(self) -> None:
        try:
            hello = json.loads(self.rfile.readline(65537))
            if not isinstance(hello, dict) or hello.get("kind") != "hello":
                return
            role = hello.get("role")
            if role not in self.server.tokens or hello.get("token") != self.server.tokens[role]:
                return
            session_id = hello.get("ompSessionId")
            generation = hello.get("generation")
            pid = hello.get("pid")
            if not isinstance(session_id, str) or not isinstance(generation, int) or not isinstance(pid, int):
                return
            with self.server.condition:
                peer = {
                    "role": role,
                    "session_id": session_id,
                    "generation": generation,
                    "pid": pid,
                    "socket": self.request,
                    "write_lock": threading.Lock(),
                    "hello": hello,
                    "state": {},
                }
                self.server.peers[role] = peer
                self.server.condition.notify_all()
            self._write({"kind": "ready", "sessionId": session_id, "generation": generation})
            while True:
                raw = self.rfile.readline(65537)
                if not raw:
                    break
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    continue
                kind = frame.get("kind")
                with self.server.condition:
                    if kind == "state":
                        peer["state"] = frame
                    elif kind == "api_ack" and isinstance(frame.get("requestId"), str):
                        request_id = frame["requestId"]
                        if request_id in self.server.dropped_ack_ids:
                            self.server.dropped_ack_ids.remove(request_id)
                            self.server.discarded_ack_metadata.append({
                                "request_id": request_id,
                                "status": frame.get("status"),
                            })
                        else:
                            self.server.acks[request_id] = frame
                    elif kind == "omp_event":
                        self.server.events.append({
                            "role": role,
                            "name": frame.get("name"),
                            "toolName": frame.get("toolName"),
                            "approved": frame.get("approved"),
                            "sessionId": frame.get("sessionId"),
                            "generation": frame.get("generation"),
                            "responseMarkerMatched": frame.get("responseMarkerMatched"),
                        })
                    self.server.condition.notify_all()
        except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
            return
        finally:
            try:
                with self.server.condition:
                    current = self.server.peers.get(str(locals().get("role", "")))
                    if current and current.get("socket") is self.request:
                        self.server.peers.pop(str(role), None)
                    self.server.condition.notify_all()
            except Exception:
                pass


def _omp_version(omp: str) -> str:
    result = subprocess.run([omp, "--version"], text=True, capture_output=True, timeout=10, check=False)
    return (result.stdout + result.stderr).strip().splitlines()[0] if result.returncode == 0 else "unknown"


def _start_omp(
    omp: str,
    role: str,
    token: str,
    root: Path,
    socket_path: Path,
    config: Path,
    *,
    expected_marker: str | None = None,
    max_time: str | None = None,
) -> dict[str, object]:
    cwd = root / f"cwd-{role}"
    session_dir = root / f"sessions-{role}"
    cwd.mkdir()
    session_dir.mkdir()
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        env = dict(os.environ)
        env.update({
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "COLORTERM": "truecolor",
            "WORKBENCH_G3_BRIDGE_SOCKET": str(socket_path),
            "WORKBENCH_G3_ROLE": role,
            "WORKBENCH_G3_TOKEN": token,
            "WORKBENCH_G3_GENERATION": "1",
            "WORKBENCH_G3_EXPECTED_OMP_VERSION": OMP_VERSION,
        })
        if expected_marker is not None:
            env["WORKBENCH_G3_EXPECTED_RESPONSE_MARKER"] = expected_marker
        args = [
            omp,
            "--no-session",
            "--no-pty",
            "--no-tools",
            "--no-skills",
            "--no-rules",
            "--no-title",
            "--no-extensions",
            "--extension",
            str(EXTENSION),
            "--cwd",
            str(cwd),
            "--session-dir",
            str(session_dir),
            "--config",
            str(config),
        ]
        if max_time is not None:
            args.extend(["--max-time", max_time])
        os.execvpe(omp, args, env)
    os.set_blocking(fd, False)
    return {"pid": pid, "fd": fd, "role": role}


def _drain_pty(fd: int, stop: threading.Event, result: dict[str, object]) -> None:
    """Keep the child PTY writable without retaining terminal output."""
    result["bytes"] = 0
    while not stop.is_set():
        try:
            readable, _, _ = select.select([fd], [], [], 0.1)
            if not readable:
                continue
            data = os.read(fd, 65536)
            if not data:
                return
            result["bytes"] = int(result["bytes"]) + len(data)
        except OSError as exc:
            if exc.errno != errno.EIO:
                result["error"] = exc.errno
            return


def _stop_omps(children: list[dict[str, object]]) -> None:
    for child in children:
        try:
            os.killpg(int(child["pid"]), signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    remaining = {int(child["pid"]) for child in children}
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            try:
                os.waitpid(pid, os.WNOHANG)
                if not Path(f"/proc/{pid}").exists():
                    remaining.discard(pid)
            except ChildProcessError:
                remaining.discard(pid)
        time.sleep(0.03)
    for pid in remaining:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for pid in remaining:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


def _run_pair(omp: str, *, model_task_probe: bool) -> dict[str, object]:
    mode = "single-task" if model_task_probe else "handshake-only"
    with tempfile.TemporaryDirectory(prefix=f"cw04-g3-{mode}-") as temp:
        root = Path(temp)
        socket_path = root / "bridge.sock"
        config = root / "config.yml"
        config.write_text("# Isolated per-probe settings overlay.\n", encoding="utf-8")
        roles = ("manager", "worker")
        tokens = {role: str(uuid4()) for role in roles}
        server = BridgeHarness(socket_path, tokens)
        os.chmod(socket_path, 0o600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        children: list[dict[str, object]] = []
        drains: list[tuple[threading.Thread, threading.Event, dict[str, object]]] = []
        marker = f"G3_TASK_PROCESSED_{uuid4().hex}"
        evidence: dict[str, object] = {
            "mode": mode,
            "task_api_status": "not_sent",
            "provider_requests": 0,
            "provider_responses": 0,
            "assistant_marker_matched": False,
            "agent_end_observed": False,
            "tool_calls_observed": 0,
        }
        try:
            for role in roles:
                child = _start_omp(
                    omp,
                    role,
                    tokens[role],
                    root,
                    socket_path,
                    config,
                    expected_marker=marker if model_task_probe and role == "worker" else None,
                    max_time="45" if model_task_probe else None,
                )
                children.append(child)
                stop = threading.Event()
                drain_result: dict[str, object] = {"role": role}
                drain = threading.Thread(
                    target=_drain_pty,
                    args=(int(child["fd"]), stop, drain_result),
                    daemon=True,
                )
                drain.start()
                drains.append((drain, stop, drain_result))
            peers = {role: server.peer(role) for role in roles}
            if len({int(peers[role]["pid"]) for role in roles}) != 2:
                raise RuntimeError("two independent OMP PIDs were not observed")
            for role in roles:
                state_ack = server.request(role, {"kind": "probe"})
                if state_ack.get("status") != "state":
                    raise RuntimeError(f"OMP state probe failed for {role}")
                state = state_ack.get("state")
                if not isinstance(state, dict) or state.get("idle") is not True or state.get("editorEmpty") is not True:
                    raise RuntimeError(f"OMP session did not report idle/empty composer for {role}")

            if model_task_probe:
                peer = peers["worker"]
                envelope = ControlEnvelope(
                    message_id=str(uuid4()),
                    delivery_attempt_id=str(uuid4()),
                    sender_role=ActorRole.MANAGER,
                    session_id=str(peer["session_id"]),
                    session_generation=int(peer["generation"]),
                    task_id=str(uuid4()),
                    revision_id=str(uuid4()),
                    run_id=str(uuid4()),
                    event=MessageEvent(MessageKind.TASK, {
                        "text": (
                            "One-time Workbench bridge feasibility check. Do not use tools, "
                            "run commands, or change files or settings. Reply with exactly "
                            f"{marker} and nothing else."
                        ),
                    }),
                )
                try:
                    task_deadline = time.monotonic() + 35
                    response = server.request(
                        "worker",
                        {"kind": "deliver", "envelope": envelope.to_json()},
                        timeout=max(0.01, task_deadline - time.monotonic()),
                    )
                    evidence["task_api_status"] = response.get("status")
                    evidence["task_model_processed_flag"] = response.get("modelProcessed")
                    if response.get("status") == "api_accepted":
                        remaining = task_deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("single-task probe deadline elapsed")
                        server.wait_event("worker", "agent_end", timeout=remaining)
                except TimeoutError:
                    evidence["lifecycle_timeout"] = True
                evidence["provider_requests"] = server.event_count("worker", "provider_request_started")
                evidence["provider_responses"] = server.event_count("worker", "provider_response_received")
                with server.condition:
                    evidence["assistant_marker_matched"] = any(
                        event.get("role") == "worker"
                        and event.get("name") == "assistant_message_end"
                        and event.get("responseMarkerMatched") is True
                        for event in server.events
                    )
                evidence["agent_end_observed"] = server.event_count("worker", "agent_end") > 0
                evidence["provider_response_hook_missing"] = (
                    evidence["provider_requests"] > 0
                    and evidence["provider_responses"] == 0
                    and evidence["agent_end_observed"]
                )
                evidence["tool_calls_observed"] = sum(
                    server.event_count("worker", name)
                    for name in ("tool_call_allowed", "tool_call_blocked")
                )

            result_roles = []
            for role in roles:
                peer = peers[role]
                result_roles.append({
                    "role": role,
                    "omp_session_id": peer["session_id"],
                    "session_generation": peer["generation"],
                    "pid": peer["pid"],
                })
            evidence["roles"] = result_roles
            evidence["explicit_task_prompts_submitted"] = 1 if model_task_probe else 0
            evidence["tools_disabled"] = True
            evidence["one_task_submitted"] = model_task_probe
            evidence["probe_pass"] = (
                not model_task_probe
                or (
                    evidence.get("task_api_status") == "api_accepted"
                    and evidence.get("provider_requests") == 1
                    and evidence.get("assistant_marker_matched") is True
                    and evidence.get("agent_end_observed") is True
                    and evidence.get("tool_calls_observed") == 0
                )
            )
            return evidence
        finally:
            _stop_omps(children)
            for drain, stop, _ in drains:
                stop.set()
                drain.join(timeout=1)
            evidence["pty_drain"] = [result for _, _, result in drains]
            for child in children:
                try:
                    os.close(int(child["fd"]))
                except OSError:
                    pass
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def auth_probe(omp: str) -> int:
    """Make exactly one non-tool, read-only model request and print no response body."""
    with tempfile.TemporaryDirectory(prefix="cw04-g3-auth-") as temp:
        root = Path(temp)
        cwd, session_dir = root / "cwd", root / "sessions"
        cwd.mkdir()
        session_dir.mkdir()
        config = root / "config.yml"
        config.write_text("# Temporary settings overlay.\n", encoding="utf-8")
        command = [
            omp,
            "--print",
            "--no-session",
            "--no-tools",
            "--no-pty",
            "--no-extensions",
            "--no-skills",
            "--no-rules",
            "--no-title",
            "--cwd",
            str(cwd),
            "--session-dir",
            str(session_dir),
            "--config",
            str(config),
            "Reply exactly G3_AUTH_PROBE_OK and no other text.",
        ]
        result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=75, check=False)
        print(f"auth_probe_exit={result.returncode} exact_marker={'yes' if 'G3_AUTH_PROBE_OK' in result.stdout else 'no'}")
        return 0 if result.returncode == 0 and "G3_AUTH_PROBE_OK" in result.stdout else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--auth-probe", action="store_true", help="send exactly one read-only model request")
    mode.add_argument("--model-task-probe", action="store_true", help="send one no-tools task with a 45-second OMP limit")
    args = parser.parse_args()
    omp = shutil.which("omp")
    if not omp:
        print("OMP is not installed", file=sys.stderr)
        return 2
    version = _omp_version(omp)
    if version != OMP_VERSION:
        print(f"expected {OMP_VERSION}; found {version}", file=sys.stderr)
        return 2
    if args.auth_probe:
        return auth_probe(omp)
    result = _run_pair(omp, model_task_probe=args.model_task_probe)
    print(json.dumps({"omp_version": version, **result}, ensure_ascii=False))
    return 0 if result["probe_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
