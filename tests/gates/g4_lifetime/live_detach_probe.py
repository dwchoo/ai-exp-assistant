"""Actual two-OMP/parent-shell/supervisor G4 lifetime fixture, private UDS frontend."""
from __future__ import annotations

import argparse
import json
import os
import pty
from pathlib import Path
import shutil
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests/gates/g3_omp")]
from live_omp_probe import BridgeHarness, EXTENSION, _stop_omps
from live_rpc_pause_probe import semantic_provider
from live_tui_pause_reconcile_probe import _state, _envelope, _send
from live_tui_draft_probe import _drain_visible
from workbench.runtime.g4 import MetadataPort, WakePort, FrontendLease
from workbench.terminal.shell_g2.lifecycle import ManagedLifecycleProbe
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


def start_omp(root: Path, role: str, token: str) -> dict:
    cwd, sessions = root / f"cwd-{role}", root / f"sessions-{role}"
    cwd.mkdir()
    sessions.mkdir()
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        env = {key: value for key, value in os.environ.items() if not key.startswith("HERDR_")}
        env.update({"TERM": "xterm-256color", "LANG": "C.UTF-8", "COLORTERM": "truecolor",
                    "PI_CODING_AGENT_DIR": str(root / f"profile-{role}"),
                    "WORKBENCH_G3_BRIDGE_SOCKET": str(root / "bridge.sock"),
                    "WORKBENCH_G3_ROLE": role, "WORKBENCH_G3_TOKEN": token,
                    "WORKBENCH_G3_GENERATION": "1", "WORKBENCH_G3_EXPECTED_OMP_VERSION": "omp/18.2.10"})
        omp = shutil.which("omp")
        os.execvpe(omp, [omp, "--no-session", "--no-pty", "--no-skills", "--no-rules", "--no-title",
                        "--no-extensions", "--extension", str(EXTENSION), "--model", "g3-tui-pause/scripted",
                        "--no-tools", "--max-time=180", "--config", str(root / "config.yml"),
                        "--cwd", str(cwd), "--session-dir", str(sessions)], env)
    os.set_blocking(fd, False)
    return {"pid": pid, "fd": fd, "role": role}


def backend(root: Path, paused: bool) -> dict:
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    children, drains = [], []
    tokens = {role: str(uuid4()) for role in ("manager", "worker")}
    server = BridgeHarness(root / "bridge.sock", tokens)
    os.chmod(root / "bridge.sock", 0o600)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    providers = {role: semantic_provider(manager=False) for role in tokens}
    for role, provider in providers.items():
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        profile = root / f"profile-{role}"
        profile.mkdir()
        (profile / "models.yml").write_text(
            "providers:\n  g3-tui-pause:\n"
            f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: G4 fixture\n        contextWindow: 32768\n        maxTokens: 1024\n")
    (root / "config.yml").write_text("startup:\n  setupWizard: false\n")
    shell = ManagedLifecycleProbe(ShellChoice("bash", "/bin/bash"))
    listener = socket.socket(socket.AF_UNIX)
    observations = 0
    result = {"backend_pid": os.getpid(), "paused": paused, "result": "unknown"}
    try:
        shell.wait_ready()
        shell._write_all((f"export BOUNDARY_PREPARED=g4 BOUNDARY_TRAPS={shlex.quote(str(root / 'traps-before'))} "
                          f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(root / 'traps-after'))}; wb-handoff\n").encode())
        deadline = time.monotonic() + 3
        while not shell.control_wait_seen and time.monotonic() < deadline:
            shell._drain(0.02)
        shell.dispatch_managed("g4-owned-run", ["/bin/sleep", "180"], return_timeout=240)
        deadline = time.monotonic() + 5
        while not shell.lifecycle.experiment_started and time.monotonic() < deadline:
            shell._drain(0.02)
        if not shell.lifecycle.experiment_started or shell.lifecycle.unknown:
            raise RuntimeError("G2 managed experiment startup unknown")
        for role in tokens:
            child = start_omp(root, role, tokens[role])
            children.append(child)
            fd = int(child["fd"])
            screen = TerminalScreen(100, 30, reply=lambda data, fd=fd: os.write(fd, data))
            stop, drained = threading.Event(), {"role": role}
            thread = threading.Thread(target=_drain_visible, args=(fd, stop, drained, make_stream(screen), threading.Lock()), daemon=True)
            thread.start()
            drains.append((thread, stop, drained))
        peers = {role: server.peer(role, 15) for role in tokens}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all(_state(server, role).get("idle") is True and _state(server, role).get("editorKnown") is True for role in tokens):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("OMP readiness unknown")
        lease = FrontendLease()
        delivered = {}

        def deliver(approval):
            envelope = _envelope("worker", peers["worker"])
            envelope["taskId"], envelope["revisionId"], envelope["runId"] = approval["task_id"], approval["revision_id"], approval["run_id"]
            providers["worker"].semantic_expected[envelope["messageId"]] = {
                "workbench_message_id": envelope["messageId"], "kind": "task",
                "task_id": envelope["taskId"], "revision_id": envelope["revisionId"], "run_id": envelope["runId"]}
            delivered.update({"message_id": envelope["messageId"], "ack": _send(server, "worker", envelope)})

        wake = WakePort(MetadataPort(root / "metadata.jsonl"), deliver)
        wake.paused = paused
        if paused:
            result["pause_ack"] = server.request("worker", {"kind": "pause"}, timeout=3).get("status")
        def snapshot():
            pids = [shell.pid, shell.lifecycle.supervisor_pid, shell.lifecycle.child_pid, *(peers[r]["pid"] for r in tokens)]
            alive = all(pid and Path(f"/proc/{pid}/stat").exists() and Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z" for pid in pids)
            return {"backend_pid": os.getpid(), "omp_pids": {r: peers[r]["pid"] for r in tokens},
                    "omp_sessions": {r: [peers[r]["session_id"], peers[r]["generation"]] for r in tokens},
                    "parent_shell_pid": shell.pid, "supervisor_pid": shell.lifecycle.supervisor_pid,
                    "experiment_pid": shell.lifecycle.child_pid, "control_mode": "managed_control_wait" if shell.control_wait_seen else "unknown",
                    "owner": shell.boundary.owner, "owner_epoch": shell.boundary.owner_epoch,
                    "user_owner": lease.owner, "user_owner_epoch": lease.owner_epoch, "attached": lease.attached,
                    "ticks": wake.ticks, "deliveries": wake.deliveries, "wake_status": wake.status,
                    "owned_alive": alive,
                    "host_observations": observations, "host_running": alive and shell.lifecycle.experiment_started and not shell.lifecycle.unknown and shell.lifecycle.main_exit is None,
                    "host_unknowns": list(shell.lifecycle.unknown), "host_main_exit": shell.lifecycle.main_exit,
                    "provider_requests": {r: providers[r].RequestHandlerClass.requests for r in tokens},
                    "agent_start": server.event_count("worker", "agent_start"), "agent_end": server.event_count("worker", "agent_end"),
                    "semantic_match": providers["worker"].semantic_seen.get(delivered.get("message_id"), {}).get("matched") is True}
        listener.bind(str(root / "frontend.sock"))
        os.chmod(root / "frontend.sock", 0o600)
        listener.listen(1)
        listener.settimeout(0.05)
        shutdown = False
        while not shutdown:
            shell._drain(0)
            observations += 1
            wake.tick()
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                command = json.loads(connection.recv(65536))
                if command["op"] == "attach":
                    lease.attach()
                elif command["op"] == "detach":
                    lease.detach()
                elif command["op"] == "arm":
                    wake.arm(command["approval"])
                elif command["op"] == "shutdown":
                    shutdown = True
                elif command["op"] != "snapshot":
                    raise ValueError("unsupported fixture frontend command")
                connection.sendall(json.dumps(snapshot()).encode())
        result.update({"last": snapshot(), "delivered": delivered, "result": "observed"})
    finally:
        listener.close()
        shell.close()
        _stop_omps(children)
        for thread, stop, drained in drains:
            stop.set()
            thread.join(1)
        for child in children:
            os.close(int(child["fd"]))
        server.shutdown()
        server.server_close()
        server_thread.join(2)
        for provider in providers.values():
            provider.shutdown()
            provider.server_close()
        result["pty_drain"] = [item for _, _, item in drains]
        result["owned_pids"] = [shell.pid, shell.lifecycle.supervisor_pid, shell.lifecycle.child_pid, *(c["pid"] for c in children)]
        result["residue"] = [pid for pid in result["owned_pids"] if pid and Path(f"/proc/{pid}").exists()]
    return result


def run(paused: bool = False) -> dict:
    assert subprocess.run([shutil.which("omp"), "--version"], capture_output=True, text=True).stdout.strip() == "omp/18.2.10"
    with tempfile.TemporaryDirectory(prefix="cw05-g4-") as temporary:
        root = Path(temporary)
        clean = {key: value for key, value in os.environ.items() if not key.startswith("HERDR_")}
        child = subprocess.Popen([sys.executable, __file__, "--backend", str(root), *( ["--paused"] if paused else [])], env=clean, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pipe_output = {}
        def drain_pipes():
            pipe_output["output"], pipe_output["errors"] = child.communicate()
        pipe_thread = threading.Thread(target=drain_pipes, daemon=True)
        pipe_thread.start()
        def request(op, **values):
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(5)
                connection.connect(str(root / "frontend.sock"))
                connection.sendall(json.dumps({"op": op, **values}).encode())
                return json.loads(connection.recv(65536))
        try:
            deadline = time.monotonic() + 35
            while not (root / "frontend.sock").exists() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.05)
            before = request("attach")
            request("arm", approval={"approved": True, "scope": "one-no-tools-worker-wake", "task_id": str(uuid4()), "revision_id": str(uuid4()), "run_id": str(uuid4())})
            detached = request("detach")
            start = time.monotonic()
            while time.monotonic() - start < 62:
                time.sleep(0.1)
                if child.poll() is not None:
                    raise RuntimeError("backend exited while detached")
            after = request("attach")
            elapsed = time.monotonic() - start
            deadline = time.monotonic() + 12
            while not paused and after["agent_end"] != 1 and time.monotonic() < deadline:
                time.sleep(0.1)
                after = request("snapshot")
            request("shutdown")
            pipe_thread.join(10)
            if pipe_thread.is_alive():
                raise TimeoutError("backend cleanup/pipe drain timeout")
            output, errors = pipe_output["output"], pipe_output["errors"]
            final = json.loads(output)
            if paused and final.get("pause_ack") != "paused":
                raise RuntimeError("actual OMP pause acknowledgement unknown")
            identities = ("backend_pid", "omp_pids", "omp_sessions", "parent_shell_pid", "supervisor_pid", "experiment_pid", "control_mode", "owner", "owner_epoch", "user_owner", "user_owner_epoch")
            same = all(before[key] == detached[key] == after[key] for key in identities)
            pids = [before["backend_pid"], before["parent_shell_pid"], before["supervisor_pid"], before["experiment_pid"],
                    before["omp_pids"].get("manager"), before["omp_pids"].get("worker")]
            valid_pids = (set(before["omp_pids"]) == {"manager", "worker"}
                          and all(type(pid) is int and pid > 0 for pid in pids)
                          and len(set(pids)) == 6 and pids[0] == child.pid)
            lease_ok = before["attached"] is True and detached["attached"] is False and after["attached"] is True
            # Bind cleanup to the exact observed five backend-owned processes,
            # then independently check the OS; an empty reported flag is not proof.
            owned = final.get("owned_pids")
            cleanup_identity = (valid_pids and isinstance(owned, list) and len(owned) == 5
                                and all(type(pid) is int and pid > 0 for pid in owned)
                                and set(owned) == set(pids[1:])
                                and final.get("backend_pid", child.pid) == child.pid)
            reported = owned if isinstance(owned, list) else []
            os_residue = sorted({pid for pid in pids + reported if type(pid) is int and pid > 0 and Path(f"/proc/{pid}").exists()})
            cleanup_ok = cleanup_identity and final.get("residue") == [] and not os_residue
            wake_ok = (after["ticks"] == 1 and after["deliveries"] == 0 and after["provider_requests"] == {"manager": 0, "worker": 0} and after["agent_start"] == after["agent_end"] == 0) if paused else (after["ticks"] == after["deliveries"] == after["provider_requests"]["worker"] == after["agent_start"] == after["agent_end"] == 1 and after["provider_requests"]["manager"] == 0 and after["semantic_match"])
            return {"result": "passed" if same and valid_pids and lease_ok and cleanup_ok and wake_ok and elapsed > 60 and after["host_observations"] > detached["host_observations"] and after["host_running"] and child.returncode == 0 else "unknown", "version": "omp/18.2.10", "paused": paused, "elapsed_detached_seconds": elapsed, "before": before, "detached": detached, "after": after, "same_identities": same, "valid_process_identities": valid_pids, "lease_ok": lease_ok, "cleanup_identity_ok": cleanup_identity, "os_residue": os_residue, "wake_ok": wake_ok, "backend_cleanup": final, "backend_exit": child.returncode, "stderr_bytes": len(errors.encode())}
        finally:
            if child.poll() is None:
                child.terminate()
                pipe_thread.join(8)
                if pipe_thread.is_alive():
                    child.kill()
                    pipe_thread.join(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path)
    parser.add_argument("--paused", action="store_true")
    args = parser.parse_args()
    if args.backend:
        print(json.dumps(backend(args.backend, args.paused), sort_keys=True))
    else:
        result = run(args.paused)
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(0 if result["result"] == "passed" else 1)
