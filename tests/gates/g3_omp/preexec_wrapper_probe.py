"""Bounded real OMP probe for same-name write delegation and pre-exec pause.

All model-visible paths and writes are confined to a temporary cwd. Terminal
output is drained and discarded; the result contains event names and file facts.
"""

from __future__ import annotations

import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time


EXTENSION = r'''
import net from "node:net";

export default function (pi: any): void {
  const socket = net.createConnection(process.env.G3_PROBE_SOCKET!);
  const allowed = new Set([process.env.G3_PROBE_A!, process.env.G3_PROBE_B!]);
  let paused = false;
  let released = false;
  let releaseA: (() => void) | undefined;
  let aCompleted = false;
  let releaseB: (() => void) | undefined;
  let buffer = "";
  function emit(name: string, extra: Record<string, unknown> = {}): void {
    socket.write(JSON.stringify({name, ...extra}) + "\n");
  }
  socket.on("data", chunk => {
    buffer += chunk.toString();
    for (;;) {
      const index = buffer.indexOf("\n");
      if (index < 0) break;
      const line = buffer.slice(0, index);
      buffer = buffer.slice(index + 1);
      try {
        const frame = JSON.parse(line);
        if (frame.command === "pause") { paused = true; emit("pause_ack"); }
        if (frame.command === "release") { released = true; releaseA?.(); emit("release_ack"); }
      } catch { emit("bad_control"); }
    }
  });
  socket.on("connect", () => emit("connected"));
  pi.on("tool_call", (event: any) => {
    const path = event?.input?.path;
    if (event?.toolName === "write" && !allowed.has(path)) {
      emit("foreign_write_blocked");
      return {block: true, reason: "Probe permits only two temporary files"};
    }
    if (event?.toolName === "write") emit("tool_call_write", {which: path === process.env.G3_PROBE_A ? "A" : "B"});
  });
  pi.on("tool_approval_requested", (event: any) => emit("approval_requested", {tool: event.toolName}));
  pi.on("tool_approval_resolved", (event: any) => emit("approval_resolved", {tool: event.toolName, approved: event.approved}));
  pi.on("tool_execution_start", (event: any) => emit("execution_start", {tool: event.toolName}));
  pi.on("tool_execution_end", (event: any) => emit("execution_end", {tool: event.toolName}));
  pi.on("agent_end", () => emit("agent_end"));
  pi.registerTool({
    name: "write", label: "Write", description: "Write a file",
    parameters: pi.zod.object({path: pi.zod.string(), content: pi.zod.string()}),
    approval: "write", deferrable: false,
    async execute(_id: string, params: any, signal: AbortSignal, onUpdate: any, ctx: any) {
      const which = params.path === process.env.G3_PROBE_A ? "A" : params.path === process.env.G3_PROBE_B ? "B" : "foreign";
      if (which === "B" && !aCompleted) {
        emit("wrapper_queued", {which});
        await new Promise<void>(resolve => { releaseB = resolve; });
      }
      emit("wrapper_enter", {which, paused, native: typeof ctx.invokeTool === "function"});
      if (which === "foreign") return {content: [{type: "text", text: "outside probe scope"}], isError: true};
      if (paused) { emit("wrapper_blocked", {which}); return {content: [{type: "text", text: "Workbench pause blocked mutation"}], isError: true}; }
      if (which === "A" && !released) await new Promise<void>(resolve => { releaseA = resolve; });
      if (signal?.aborted) return {content: [{type: "text", text: "aborted"}], isError: true};
      if (typeof ctx.invokeTool !== "function") return {content: [{type: "text", text: "native unavailable"}], isError: true};
      emit("native_begin", {which});
      try {
        const result = await ctx.invokeTool(params, {signal, onUpdate});
        emit("native_end", {which, isError: result.isError === true});
        return result;
      } finally {
        if (which === "A") { aCompleted = true; releaseB?.(); }
      }
    },
  });
}
'''


def scripted_provider(a: Path, b: Path, events: list[dict[str, object]]) -> ThreadingHTTPServer:
    """Return one fixed two-call assistant turn, then a fixed final response."""

    class Handler(BaseHTTPRequestHandler):
        requests = 0

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            type(self).requests += 1
            first = self.requests == 1
            second = self.requests == 2
            calls = [
                {"index": i, "id": f"probe-call-{which}", "type": "function", "function": {"name": "write", "arguments": json.dumps({"path": str(path), "content": f"{which}_OK"})}}
                for i, (which, path) in enumerate((("A", a), ("B", b)))
            ]
            if first:
                events.extend(({"name": "prepared_write", "which": which, "source": "scripted_provider"} for which in ("A", "B")))
            if first:
                delta = {"role": "assistant", "tool_calls": calls}
            elif second:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "probe-read-A", "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": str(a)})}}]}
            else:
                delta = {"role": "assistant", "content": "Probe report complete."}
            finish = "tool_calls" if first or second else "stop"
            frames = [
                {"id": "g3-probe", "object": "chat.completion.chunk", "created": 0, "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "g3-probe", "object": "chat.completion.chunk", "created": 0, "model": "scripted", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            payload = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            if first:
                events.append({"name": "batch_response_sent"})
            elif not second:
                events.append({"name": "report_sent"})

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def main() -> int:
    omp = shutil.which("omp")
    if not omp or subprocess.run([omp, "--version"], capture_output=True, text=True, timeout=10).stdout.strip() != "omp/18.2.10":
        print(json.dumps({"result": "inconclusive", "reason": "OMP v18.2.10 unavailable"}))
        return 2
    with tempfile.TemporaryDirectory(prefix="cw04-g3-preexec-") as temporary:
        root = Path(temporary)
        cwd = root / "cwd"
        cwd.mkdir()
        extension = root / "probe.ts"
        extension.write_text(EXTENSION)
        config = root / "config.yml"
        config.write_text("# Probe-only overlay\n")
        a, b = cwd / "A.txt", cwd / "B.txt"
        profile = root / "agent"
        profile.mkdir()
        events: list[dict[str, object]] = []
        provider = scripted_provider(a, b, events)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        (profile / "models.yml").write_text(
            "providers:\n"
            "  g3-probe:\n"
            f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
            "    api: openai-completions\n"
            "    auth: none\n"
            "    models:\n"
            "      - id: scripted\n"
            "        name: Scripted G3 probe\n"
            "        contextWindow: 32768\n"
            "        maxTokens: 1024\n"
        )
        sock_path = root / "events.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(sock_path))
        listener.listen(1)
        listener.setblocking(False)
        output_bytes = 0
        child_pid = -1
        fd = -1
        client: socket.socket | None = None
        deadline = time.monotonic() + 50
        try:
            child_pid, fd = pty.fork()
            if child_pid == 0:
                os.chdir(cwd)
                env = dict(os.environ)
                env.update({"TERM": "xterm-256color", "PI_CODING_AGENT_DIR": str(profile), "G3_PROBE_SOCKET": str(sock_path), "G3_PROBE_A": str(a), "G3_PROBE_B": str(b)})
                os.execvpe(omp, [omp, "--print", "--model", "g3-probe/scripted", "--no-session", "--no-pty", "--no-skills", "--no-rules", "--no-title", "--no-extensions", "--extension", str(extension), "--tools=write,read", "--auto-approve", "--max-time=40", "--cwd", str(cwd), "--session-dir", str(root / "sessions"), "--config", str(config), "Run the scripted tool-control probe."], env)
            os.set_blocking(fd, False)
            buffer = b""
            pause_sent = False
            release_sent = False
            while time.monotonic() < deadline:
                watched = [fd, listener]
                if client:
                    watched.append(client)
                ready, _, _ = select.select(watched, [], [], 0.1)
                if fd in ready:
                    try:
                        chunk = os.read(fd, 65536)
                        output_bytes += len(chunk)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                if listener in ready and not client:
                    client, _ = listener.accept()
                    client.setblocking(False)
                if client and client in ready:
                    chunk = client.recv(65536)
                    if chunk:
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            event = json.loads(line)
                            events.append(event)
                            name = event.get("name")
                            if name == "wrapper_enter" and event.get("which") == "A" and not pause_sent:
                                client.sendall(b'{"command":"pause"}\n')
                                pause_sent = True
                            if name == "pause_ack" and not release_sent:
                                client.sendall(b'{"command":"release"}\n')
                                release_sent = True
                if any(e.get("name") == "agent_end" for e in events):
                    break
                if child_pid > 0:
                    exited, _ = os.waitpid(child_pid, os.WNOHANG)
                    if exited:
                        child_pid = -1
                        break
            sequence = [(e.get("name"), e.get("which")) for e in events]
            def before(left: tuple[str, str | None], right: tuple[str, str | None]) -> bool:
                return left in sequence and right in sequence and sequence.index(left) < sequence.index(right)

            same_batch_order = (
                before(("prepared_write", "A"), ("prepared_write", "B"))
                and before(("prepared_write", "B"), ("batch_response_sent", None))
                and before(("batch_response_sent", None), ("wrapper_enter", "A"))
                and before(("wrapper_enter", "A"), ("wrapper_queued", "B"))
                and before(("wrapper_queued", "B"), ("pause_ack", None))
                and before(("pause_ack", None), ("native_end", "A"))
                and before(("native_end", "A"), ("wrapper_blocked", "B"))
            )
            wrapper_b_blocked = any(e.get("name") == "wrapper_blocked" and e.get("which") == "B" for e in events)
            native_a_finished = any(e.get("name") == "native_end" and e.get("which") == "A" and e.get("isError") is False for e in events)
            native_b_started = any(e.get("name") == "native_begin" and e.get("which") == "B" for e in events)
            read_started = any(e.get("name") == "execution_start" and e.get("tool") == "read" for e in events)
            read_end_index = next((i for i, e in enumerate(events) if e.get("name") == "execution_end" and e.get("tool") == "read"), -1)
            report_index = next((i for i, e in enumerate(events) if e.get("name") == "report_sent"), -1)
            read_finished = read_end_index >= 0
            report_finished = read_finished and report_index > read_end_index
            result = {
                "result": "passed" if same_batch_order and native_a_finished and wrapper_b_blocked and not native_b_started and read_started and read_finished and report_finished and provider.RequestHandlerClass.requests == 3 and any(e.get("name") == "agent_end" for e in events) and a.exists() and a.read_text() == "A_OK" and not b.exists() else "inconclusive",
                "event_sequence": [{k: v for k, v in e.items() if k in ("name", "which", "tool", "approved", "native", "paused", "isError")} for e in events],
                "a_exists": a.exists(), "a_content_exact": a.read_text() == "A_OK" if a.exists() else False,
                "b_exists": b.exists(), "pty_bytes_drained": output_bytes,
                "approval_mode": "auto_approve",
                "b_queued_in_wrapper": any(e.get("name") == "wrapper_queued" and e.get("which") == "B" for e in events),
                "native_a_finished": native_a_finished,
                "same_batch_order": same_batch_order,
                "native_b_started": native_b_started,
                "wrapper_b_blocked": wrapper_b_blocked,
                "read_started": read_started,
                "read_finished": read_finished,
                "report_finished": report_finished,
                "provider_requests": provider.RequestHandlerClass.requests,
            }
            print(json.dumps(result))
            return 0 if result["result"] == "passed" else 1
        finally:
            if child_pid > 0:
                try:
                    os.killpg(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    try:
                        reaped, _ = os.waitpid(child_pid, os.WNOHANG)
                    except ChildProcessError:
                        reaped = child_pid
                    if reaped:
                        break
                    time.sleep(0.05)
                else:
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        os.waitpid(child_pid, 0)
                    except ChildProcessError:
                        pass
            if client:
                client.close()
            listener.close()
            if fd >= 0:
                os.close(fd)
            provider.shutdown()
            provider.server_close()
            provider_thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
