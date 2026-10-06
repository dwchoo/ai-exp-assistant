// C-D69 corrections independent check (p27-cd69-test-02): the per-message limit the skills state (8192 characters)
// matches the schema of both handoff tools in the real bridge extension, and a long to_worker message (the executable
// procedure, well over the old 1024-character summary) reaches the backend unchanged.
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
const SESSION_ID = "30000000-0000-4000-8000-0000000cd069";

async function startBridge(role: "manager" | "worker") {
	const directory = await mkdtemp(join(tmpdir(), "p27cd69-bridge-"));
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


const LIMIT = 8192;

test("to_worker and to_manager accept a message of exactly 8192 characters and no more", async () => {
	for (const [role, tool] of [["manager", "to_worker"], ["worker", "to_manager"]] as const) {
		const bridge = await startBridge(role);
		try {
			const message = bridge.tools.get(tool)!.parameters.properties.message;
			assert.equal(message.maxLength, LIMIT, `${tool}.message maxLength`);
			assert.equal(message.minLength, 1);
		} finally { await bridge.close(); }
	}
});

test("a long procedure in to_worker reaches the backend whole", async () => {
	const manager = await startBridge("manager");
	try {
		for (const size of [1025, 4096, LIMIT]) {
			const message = Array.from({ length: size }, (_, i) => String.fromCharCode(97 + (i * 7) % 26)).join("");
			const args = { kind: "work", message, task_id: null, spec: { goal: "g", paths: [] }, analysis: null,
				run: null, cancel: null };
			const pending = manager.tools.get("to_worker")!.execute(`long-${size}`, args, new AbortController().signal,
				() => {}, {});
			const request = await manager.waitFor(frame => frame.kind === "tool_request" && frame.toolCallId === `long-${size}`);
			assert.equal(request.args.message.length, size);
			assert.equal(request.args.message, message);
			manager.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: `long-${size}`,
				result: { status: "queued" } });
			await pending;
		}
	} finally { await manager.close(); }
});
