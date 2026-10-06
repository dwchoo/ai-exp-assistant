// C-D69 (2)/(3) independent check (p27-cd69-test-01): the manager's to_worker tool in the real bridge extension.
// Expectations from DECISIONS.md C-D69, not from the implementation: to_worker carries a nullable analysis level
// (null = summary, detailed only on explicit request, kind work only) that reaches the backend unchanged; the tool
// text tells the manager to delegate an executable procedure and keep interpretation; no `analyst` anywhere.
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

test("to_worker has a nullable, optional analysis level of exactly summary/detailed", async () => {
	const manager = await startBridge("manager");
	try {
		const tool = manager.tools.get("to_worker")!;
		const analysis = tool.parameters.properties.analysis;
		assert.ok(analysis, "C-D69 (2): to_worker carries the analysis level");
		assert.deepEqual([...analysis.type].sort(), ["null", "string"]);
		assert.deepEqual(new Set(analysis.enum), new Set(["summary", "detailed", null]));
		assert.equal(analysis.enum.length, 3);
		assert.ok(!tool.parameters.required.includes("analysis"), "null/omitted means summary");
		assert.match(analysis.description, /summary/i);
		assert.match(analysis.description, /null/i);
		assert.match(analysis.description, /work/i);
		assert.match(analysis.description, /experiment/i);
		assert.equal(tool.parameters.additionalProperties, false);
	} finally { await manager.close(); }
});

test("the analysis level reaches the backend unchanged (detailed, summary and null)", async () => {
	const manager = await startBridge("manager");
	try {
		for (const [index, analysis] of ["detailed", "summary", null].entries()) {
			const args = { kind: "work", message: "run lscpu and free -h; report values", task_id: null, spec: null,
				analysis, run: null, cancel: null };
			const pending = manager.tools.get("to_worker")!.execute(`call-${index}`, args, new AbortController().signal,
				() => {}, {});
			const request = await manager.waitFor(frame => frame.kind === "tool_request");
			assert.equal(request.tool, "to_worker");
			assert.deepEqual(request.args, args, `analysis=${analysis}`);
			manager.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: `call-${index}`,
				result: { status: "queued" } });
			await pending;
		}
	} finally { await manager.close(); }
});

test("the tool texts state the executor boundary and never offer an analyst", async () => {
	const manager = await startBridge("manager");
	try {
		const description = manager.tools.get("to_worker")!.description as string;
		assert.match(description, /procedure|steps/i, "delegate an executable procedure");
		assert.match(description, /stop/i, "when to stop");
		assert.match(description, /interpret/i, "interpretation stays with the manager");
		assert.match(description, /detailed/i, "detailed analysis only when asked");
		for (const tool of manager.tools.values())
			assert.doesNotMatch(JSON.stringify(tool), /\banalyst\b/, `${tool.name} mentions analyst (C-D69 (3))`);
	} finally { await manager.close(); }
	const worker = await startBridge("worker");
	try {
		for (const tool of worker.tools.values())
			assert.doesNotMatch(JSON.stringify(tool), /\banalyst\b/, `${tool.name} mentions analyst (C-D69 (3))`);
	} finally { await worker.close(); }
});
