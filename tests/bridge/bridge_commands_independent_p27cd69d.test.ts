// C-D69 (6) independent check (p27-cd69-cmds-test-01): the manager-authored `commands` in the real bridge extension.
// Expectations from DECISIONS.md C-D69 (6), not from the implementation: to_worker carries an optional, nullable
// list of exact shell commands (kind work only) that reaches the backend unchanged, with the same bounds the
// backend enforces (32 entries, 8192 characters each); the manager is told to write the commands itself; the
// worker's terminal text names the Task-command refusal and the harness script file, not the old command_too_long.
// Fake backend peer over a real Unix socket under the OS temp dir; no OMP and no provider.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import workbenchG3Extension from "../../omp_bridge/g3/bridge.ts";

type Frame = Record<string, any>;
const SESSION_ID = "30000000-0000-4000-8000-000000cd069d";

async function startBridge(role: "manager" | "worker") {
	const directory = await mkdtemp(join(tmpdir(), "p27cd69d-bridge-"));
	const socketPath = join(directory, "bridge.sock");
	const frames: Frame[] = [];
	const sockets: net.Socket[] = [];
	const server = net.createServer(socket => {
		sockets.push(socket);
		let pending = "";
		socket.on("data", chunk => {
			pending += chunk.toString("utf8");
			while (pending.includes("\n")) {
				const index = pending.indexOf("\n");
				frames.push(JSON.parse(pending.slice(0, index)) as Frame);
				pending = pending.slice(index + 1);
			}
		});
	});
	await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(socketPath, resolve); });
	const names = ["WORKBENCH_G3_BRIDGE_SOCKET", "WORKBENCH_G3_ROLE", "WORKBENCH_G3_TOKEN", "WORKBENCH_G3_GENERATION"];
	const previous = new Map(names.map(name => [name, process.env[name]]));
	process.env.WORKBENCH_G3_BRIDGE_SOCKET = socketPath;
	process.env.WORKBENCH_G3_ROLE = role;
	process.env.WORKBENCH_G3_TOKEN = randomUUID();
	process.env.WORKBENCH_G3_GENERATION = "1";
	const handlers = new Map<string, (...args: any[]) => any>();
	const tools = new Map<string, Frame>();
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerTool(tool: Frame) { tools.set(tool.name, tool); },
		getActiveTools: () => ["read", "grep", "yield", ...tools.keys()],
		setActiveTools() {},
		async sendUserMessage() {},
		logger: { error() {} },
	};
	const context = {
		sessionManager: { getSessionId: () => SESSION_ID },
		isIdle: () => true, hasPendingMessages: () => false,
		ui: { getEditorText: () => "" }, abort: () => {},
	};
	workbenchG3Extension(pi);
	handlers.get("session_start")!(undefined, context);
	async function waitFor(predicate: (frame: Frame) => boolean, timeoutMs = 2000): Promise<Frame> {
		const deadline = Date.now() + timeoutMs;
		while (Date.now() < deadline) {
			const index = frames.findIndex(predicate);
			if (index >= 0) return frames.splice(index, 1)[0];
			await delay(5);
		}
		throw new Error("bridge frame timed out");
	}
	await waitFor(frame => frame.kind === "hello");
	function reply(frame: Frame): void { sockets.at(-1)!.write(JSON.stringify(frame) + "\n"); }
	async function close(): Promise<void> {
		handlers.get("session_shutdown")?.();
		for (const socket of sockets) socket.destroy();
		await new Promise<void>(resolve => server.close(() => resolve()));
		for (const name of names) {
			const value = previous.get(name);
			if (value === undefined) delete process.env[name]; else process.env[name] = value;
		}
		await rm(directory, { recursive: true, force: true });
	}
	return { tools, waitFor, reply, close };
}

function flat(text: string): string { return text.replace(/\s+/g, " "); }

test("to_worker.commands is an optional nullable list bounded like the backend", async () => {
	const manager = await startBridge("manager");
	try {
		const tool = manager.tools.get("to_worker")!;
		const commands = tool.parameters.properties.commands;
		assert.ok(commands, "C-D69 (6)(a): to_worker carries commands");
		assert.deepEqual([...commands.type].sort(), ["array", "null"]);
		assert.equal(commands.maxItems, 32);
		assert.equal(commands.items.type, "string");
		assert.equal(commands.items.minLength, 1);
		assert.equal(commands.items.maxLength, 8192);
		assert.ok(!(tool.parameters.required ?? []).includes("commands"), "null/omitted means no commands");
		assert.equal(tool.parameters.additionalProperties, false);
		const text = flat(commands.description);
		assert.match(text, /exact/i);
		assert.match(text, /in order/i);
		assert.match(text, /written by you/i, "the manager writes the commands, not the worker");
		assert.match(text, /alternative/i, "alternatives are more entries");
		assert.match(text, /refuses any other command/i);
		assert.match(text, /replaces the list; null keeps it/i);
		assert.match(text, /null for kind experiment/i);
	} finally { await manager.close(); }
});

test("commands reach the backend exactly as written (multi-line, unicode, quotes, spacing, null)", async () => {
	const manager = await startBridge("manager");
	try {
		const lists = [["  ls -la /tmp  ", "printf '%s\\n' \"한글 ✓ 😀\"", "cat <<'EOF'\nline $HOME\nEOF", "x".repeat(8192)],
			null];
		for (const [index, commands] of lists.entries()) {
			const args = { kind: "work", message: "run these; report key values", task_id: null,
				spec: { goal: "facts", paths: [], instructions: null, execution: null }, analysis: null, commands,
				run: null, cancel: null };
			const pending = manager.tools.get("to_worker")!.execute(`cmd-${index}`, args, new AbortController().signal,
				() => {}, {});
			const request = await manager.waitFor(frame => frame.kind === "tool_request");
			assert.equal(request.tool, "to_worker");
			assert.deepEqual(request.args.commands, commands);
			manager.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: `cmd-${index}`,
				result: { status: "dispatched" } });
			await pending;
		}
	} finally { await manager.close(); }
});

test("the worker's terminal text names the Task refusal and the script file, not command_too_long", async () => {
	const worker = await startBridge("worker");
	try {
		const tool = worker.tools.get("terminal")!;
		const text = flat(JSON.stringify(tool));
		assert.match(text, /not_in_task_commands/);
		assert.match(text, /only those run, exactly as given/i);
		assert.match(text, /script file Workbench writes/i);
		assert.doesNotMatch(text, /command_too_long/);
		assert.doesNotMatch(text, /write it to a file with the write tool/i);
		assert.ok(!worker.tools.has("to_worker"), "the worker cannot hand itself commands");
	} finally { await worker.close(); }
	const manager = await startBridge("manager");
	try {
		assert.ok(!manager.tools.has("terminal"), "the manager does not run the commands itself");
		const message = flat(manager.tools.get("to_worker")!.parameters.properties.message.description);
		assert.match(message, /exact commands themselves in commands/i);
	} finally { await manager.close(); }
});
