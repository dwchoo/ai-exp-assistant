// CW-18 U1: bridge extension tools `to_worker` (manager) / `to_manager` (worker).
// Fake backend peer over a real Unix socket; no OMP and no provider.
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
const SESSION_ID = "30000000-0000-4000-8000-000000000002";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

async function startBridge(role: "manager" | "worker") {
	const directory = await mkdtemp(join(tmpdir(), "cw18-u1-tools-"));
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
	async function waitFor(predicate: (frame: Frame) => boolean, timeoutMs = 1500): Promise<Frame> {
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
	return { frames, sockets, handlers, tools, waitFor, reply, close };
}

function textOf(result: Frame): Frame {
	assert.equal(result.content.length, 1);
	assert.equal(result.content[0].type, "text");
	return JSON.parse(result.content[0].text);
}

test("manager registers only to_worker and worker only to_manager, both essential with JSON-schema parameters", async () => {
	for (const [role, own, other] of [["manager", "to_worker", "to_manager"], ["worker", "to_manager", "to_worker"]] as const) {
		const bridge = await startBridge(role);
		try {
			assert.deepEqual([...bridge.tools.keys()], [own], `${role} tools`);
			assert.equal(bridge.tools.has(other), false);
			const tool = bridge.tools.get(own)!;
			assert.equal(tool.loadMode, "essential", "schema must reach the provider without xd:// discovery");
			assert.equal(typeof tool.execute, "function");
			assert.equal(typeof tool.description, "string");
			assert.equal(tool.parameters.type, "object");
			assert.equal(tool.parameters.additionalProperties, false);
			const kinds = own === "to_worker" ? ["experiment", "work"] : ["answer", "progress", "done", "blocked", "report"];
			assert.deepEqual(tool.parameters.properties.kind.enum, kinds);
			assert.deepEqual([...tool.parameters.required].sort(), ["kind", "message"]);
			if (own === "to_worker") {
				// C-D66: one task at a time, no approval step, worker_busy while busy.
				assert.match(tool.description, /The worker does ONE task at a time\. If it is busy you get worker_busy with the current task; wait for its to_manager report \(done\/blocked\) or cancel the task\./);
				assert.doesNotMatch(tool.description, /approval_pending/);
			}
		} finally { await bridge.close(); }
	}
	const manager = await startBridge("manager");
	try {
		const props = manager.tools.get("to_worker")!.parameters.properties;
		assert.deepEqual(Object.keys(props).sort(), ["cancel", "kind", "message", "run", "spec", "task_id"]);
		assert.equal(props.cancel.type, "boolean");
		assert.deepEqual(Object.keys(props.spec.properties).sort(), ["execution", "goal", "instructions", "paths"]);
		assert.deepEqual(Object.keys(props.spec.properties.execution.properties).sort(),
			["command", "commit", "criteria", "environment", "shell", "source"]);
		assert.equal(props.spec.properties.execution.properties.environment.items.type, "string");
	} finally { await manager.close(); }
	const worker = await startBridge("worker");
	try {
		const props = worker.tools.get("to_manager")!.parameters.properties;
		assert.deepEqual(Object.keys(props).sort(),
			["in_reply_to", "kind", "message", "reason", "request", "requires_code_change", "task_id"]);
	} finally { await worker.close(); }
});

test("execute sends one tool_request frame and returns the matching tool_result immediately", async () => {
	const bridge = await startBridge("manager");
	try {
		const args = { kind: "work", message: "look at the parser" };
		const pending = bridge.tools.get("to_worker")!.execute("call-1", args, new AbortController().signal, () => {}, {});
		const request = await bridge.waitFor(frame => frame.kind === "tool_request");
		assert.deepEqual(Object.keys(request).sort(),
			["args", "generation", "kind", "requestId", "sessionId", "tool", "toolCallId"]);
		assert.match(request.requestId, UUID);
		assert.equal(request.toolCallId, "call-1");
		assert.equal(request.tool, "to_worker");
		assert.deepEqual(request.args, args);
		assert.equal(request.sessionId, SESSION_ID);
		assert.equal(request.generation, 1);
		// A result for another request or another tool call is ignored.
		bridge.reply({ kind: "tool_result", requestId: randomUUID(), toolCallId: "call-1", result: { status: "queued" } });
		bridge.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "other", result: { status: "queued" } });
		await delay(30);
		const backendResult = { status: "approval_pending", approval_id: randomUUID() };
		bridge.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "call-1", result: backendResult });
		const result = await pending;
		assert.deepEqual(textOf(result), backendResult);
		assert.deepEqual(result.details, backendResult);
		assert.equal(bridge.frames.filter(frame => frame.kind === "tool_request").length, 0, "exactly one request frame");
	} finally { await bridge.close(); }
});

test("OMP's injected intent field is not forwarded as a tool argument", async () => {
	const bridge = await startBridge("worker");
	try {
		const pending = bridge.tools.get("to_manager")!.execute("call-i", { i: "report progress", kind: "progress", message: "50%" },
			new AbortController().signal, () => {}, {});
		const request = await bridge.waitFor(frame => frame.kind === "tool_request");
		assert.deepEqual(request.args, { kind: "progress", message: "50%" });
		bridge.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "call-i", result: { status: "queued" } });
		assert.equal(textOf(await pending).status, "queued");
	} finally { await bridge.close(); }
});

test("timeout returns outcome_unknown after 10 s and never resends", async () => {
	const bridge = await startBridge("worker");
	try {
		const started = Date.now();
		const pending = bridge.tools.get("to_manager")!.execute("call-t", { kind: "done", message: "finished" },
			new AbortController().signal, () => {}, {});
		const request = await bridge.waitFor(frame => frame.kind === "tool_request");
		const result = textOf(await pending);
		const elapsed = Date.now() - started;
		assert.equal(result.status, "outcome_unknown");
		assert.equal(result.reason, "timeout");
		assert.ok(elapsed >= 9_900 && elapsed < 12_000, `elapsed ${elapsed}`);
		// A late result is ignored, and nothing was sent again.
		bridge.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "call-t", result: { status: "queued" } });
		await delay(100);
		assert.equal(bridge.frames.filter(frame => frame.kind === "tool_request").length, 0);
	} finally { await bridge.close(); }
});

test("disconnect while waiting returns outcome_unknown and the request is not resent after reconnect", async () => {
	const bridge = await startBridge("manager");
	try {
		const pending = bridge.tools.get("to_worker")!.execute("call-d", { kind: "experiment", message: "run it" },
			new AbortController().signal, () => {}, {});
		await bridge.waitFor(frame => frame.kind === "tool_request");
		bridge.sockets.at(-1)!.destroy();
		const result = textOf(await pending);
		assert.equal(result.status, "outcome_unknown");
		assert.equal(result.reason, "bridge_disconnected");
		await bridge.waitFor(frame => frame.kind === "hello", 3000);
		await delay(150);
		assert.equal(bridge.frames.filter(frame => frame.kind === "tool_request").length, 0);
	} finally { await bridge.close(); }
});

test("abort while waiting returns outcome_unknown; not connected returns not_sent without a frame", async () => {
	const bridge = await startBridge("worker");
	try {
		const controller = new AbortController();
		const pending = bridge.tools.get("to_manager")!.execute("call-a", { kind: "blocked", message: "stuck" },
			controller.signal, () => {}, {});
		await bridge.waitFor(frame => frame.kind === "tool_request");
		controller.abort();
		const aborted = textOf(await pending);
		assert.equal(aborted.status, "outcome_unknown");
		assert.equal(aborted.reason, "aborted");
		bridge.handlers.get("session_shutdown")!();
		const offline = textOf(await bridge.tools.get("to_manager")!.execute("call-o", { kind: "blocked", message: "x" },
			new AbortController().signal, () => {}, {}));
		assert.equal(offline.status, "rejected");
		assert.equal(offline.reason, "bridge_not_connected");
		await delay(50);
		assert.equal(bridge.frames.filter(frame => frame.kind === "tool_request").length, 0);
	} finally { await bridge.close(); }
});

test("to_manager during a staged worker delivery is rejected without a frame and the stage reply stays rejected", async () => {
	const bridge = await startBridge("worker");
	try {
		const target = {
			schemaVersion: 1, messageId: randomUUID(), deliveryAttemptId: randomUUID(), senderRole: "manager",
			sessionId: SESSION_ID, sessionGeneration: 1, taskId: randomUUID(), revisionId: randomUUID(), runId: randomUUID(),
			event: { type: "message", messageKind: "task", payload: { stage: "execute", revision: 1 } },
		};
		const requestId = randomUUID();
		bridge.reply({ kind: "deliver", requestId, envelope: JSON.stringify(target) });
		const ack = await bridge.waitFor(frame => frame.kind === "api_ack" && frame.requestId === requestId);
		assert.equal(ack.status, "api_accepted");
		bridge.handlers.get("tool_call")!({ toolCallId: "call-s", toolName: "to_manager" });
		const result = textOf(await bridge.tools.get("to_manager")!.execute("call-s", { kind: "answer", message: "x" },
			new AbortController().signal, () => {}, {}));
		assert.equal(result.status, "rejected");
		assert.equal(result.reason, "staged_delivery_pending");
		await delay(30);
		assert.equal(bridge.frames.filter(frame => frame.kind === "tool_request").length, 0);
		bridge.handlers.get("before_provider_request")!({ payload: { messages: [] } });
		bridge.handlers.get("agent_end")!();
		const rejected = await bridge.waitFor(frame => frame.kind === "omp_event"
			&& (frame.name === "worker_response_rejected" || frame.name === "delivery_processing_unknown"));
		assert.notEqual(rejected.name, "assistant_message_end");
	} finally { await bridge.close(); }
});
