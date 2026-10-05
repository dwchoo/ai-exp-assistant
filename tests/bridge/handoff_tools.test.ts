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
import workbenchG3Extension, { terminalTimeoutMs } from "../../omp_bridge/g3/bridge.ts";

type Frame = Record<string, any>;
const SESSION_ID = "30000000-0000-4000-8000-000000000002";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

async function startBridge(role: "manager" | "worker", agent?: Frame) {
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
	const userMessages: string[] = [];
	const omp = { idle: true };
	const activeToolSets: string[][] = [];
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerTool(tool: Frame) { tools.set(tool.name, tool); },
		getActiveTools: () => ["read", "grep", "yield", ...tools.keys()],
		setActiveTools(names: string[]) { activeToolSets.push([...names]); },
		async sendUserMessage(text: string) { userMessages.push(text); },
		logger: { error() {} },
	};
	const context = {
		sessionManager: { getSessionId: () => SESSION_ID },
		isIdle: () => omp.idle, hasPendingMessages: () => false,
		ui: { getEditorText: () => "" }, abort: () => {},
		...(agent === undefined ? {} : { agent }),
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
	if (agent?.kind !== "sub") await waitFor(frame => frame.kind === "hello");
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
	return { frames, sockets, handlers, tools, waitFor, reply, close, userMessages, omp, activeToolSets };
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
			// C-D68: the worker also has `terminal`, its only command execution path.
			assert.deepEqual([...bridge.tools.keys()], role === "worker" ? [own, "terminal"] : [own], `${role} tools`);
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
		assert.deepEqual(props.cancel.type, ["boolean", "null"]);
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

// CW-18 smoke D1 (p27-cw18-smoke-fix-01): OMP 18.4.5 ships an extension tool to openai-codex without `strict`
// unless the tool sets it; with `strict: true` OMP converts the schema (every property required, optional
// ones `anyOf [T, null]`). The registered schema must therefore accept null (and blank placeholders) for
// every optional field, so OMP's local argument validation lets them through and the backend decides.
type Schema = Record<string, any>;

function typesOf(schema: Schema): string[] {
	return Array.isArray(schema.type) ? schema.type : schema.type === undefined ? [] : [schema.type];
}

// A small JSON-schema check (the keywords the bridge schemas use) standing in for OMP's local validation.
function schemaErrors(schema: Schema, value: unknown, path = "$"): string[] {
	if (Array.isArray(schema.anyOf)) {
		return schema.anyOf.some((branch: Schema) => schemaErrors(branch, value, path).length === 0)
			? [] : [`${path}: no anyOf branch`];
	}
	const types = typesOf(schema);
	const kind = value === null ? "null" : Array.isArray(value) ? "array"
		: typeof value === "number" ? (Number.isInteger(value) && types.includes("integer") ? "integer" : "number")
		: typeof value;
	if (types.length && !types.includes(kind)) return [`${path}: ${kind} not in ${types.join("|")}`];
	if (schema.enum && !schema.enum.includes(value)) return [`${path}: not in enum`];
	const errors: string[] = [];
	if (typeof value === "number") {
		if (schema.minimum !== undefined && value < schema.minimum) errors.push(`${path}: minimum`);
		if (schema.maximum !== undefined && value > schema.maximum) errors.push(`${path}: maximum`);
	}
	if (typeof value === "string") {
		if (schema.minLength !== undefined && value.length < schema.minLength) errors.push(`${path}: minLength`);
		if (schema.maxLength !== undefined && value.length > schema.maxLength) errors.push(`${path}: maxLength`);
		if (schema.pattern !== undefined && !new RegExp(schema.pattern).test(value)) errors.push(`${path}: pattern`);
	}
	if (Array.isArray(value)) {
		if (schema.minItems !== undefined && value.length < schema.minItems) errors.push(`${path}: minItems`);
		if (schema.maxItems !== undefined && value.length > schema.maxItems) errors.push(`${path}: maxItems`);
		value.forEach((item, index) => errors.push(...schemaErrors(schema.items ?? {}, item, `${path}[${index}]`)));
	}
	if (kind === "object") {
		const object = value as Record<string, unknown>;
		for (const name of schema.required ?? []) if (!(name in object)) errors.push(`${path}.${name}: required`);
		for (const [name, item] of Object.entries(object)) {
			const child = schema.properties?.[name];
			if (child === undefined) {
				if (schema.additionalProperties === false) errors.push(`${path}.${name}: unknown`);
			} else errors.push(...schemaErrors(child, item, `${path}.${name}`));
		}
	}
	return errors;
}

function objectNodes(schema: Schema, path = "$"): [string, Schema][] {
	const nodes: [string, Schema][] = [];
	if (typesOf(schema).includes("object")) nodes.push([path, schema]);
	for (const [name, child] of Object.entries(schema.properties ?? {})) nodes.push(...objectNodes(child as Schema, `${path}.${name}`));
	if (schema.items) nodes.push(...objectNodes(schema.items, `${path}[]`));
	for (const branch of schema.anyOf ?? []) nodes.push(...objectNodes(branch, path));
	return nodes;
}

const OPTIONAL: Record<string, string[]> = {
	to_worker: ["task_id", "spec", "run", "cancel"],
	to_manager: ["task_id", "in_reply_to", "requires_code_change", "reason", "request"],
};

test("both tools are strict-mode compatible: strict flag, closed objects, every optional field nullable", async () => {
	for (const [role, own] of [["manager", "to_worker"], ["worker", "to_manager"]] as const) {
		const bridge = await startBridge(role);
		try {
			const tool = bridge.tools.get(own)!;
			assert.equal(tool.strict, true, "OMP sends strict: true (and a strict schema) only when the tool asks");
			const parameters = tool.parameters as Schema;
			assert.deepEqual([...parameters.required].sort(), ["kind", "message"]);
			for (const [path, node] of objectNodes(parameters)) {
				assert.equal(node.additionalProperties, false, `${path} must be closed`);
				assert.equal(node.patternProperties, undefined, path);
				assert.equal(typeof node.properties, "object", path);
			}
			for (const name of OPTIONAL[own]) {
				const property = parameters.properties[name];
				assert.ok(typesOf(property).includes("null"), `${own}.${name} must accept null`);
				assert.match(property.description, /null when not used/, `${own}.${name} description`);
			}
			for (const name of ["kind", "message"]) assert.ok(!typesOf(parameters.properties[name]).includes("null"), name);
		} finally { await bridge.close(); }
	}
	const manager = await startBridge("manager");
	try {
		const spec = manager.tools.get("to_worker")!.parameters.properties.spec;
		for (const name of ["goal", "paths", "instructions", "execution"]) {
			assert.ok(typesOf(spec.properties[name]).includes("null"), `spec.${name} must accept null`);
		}
		assert.match(spec.properties.execution.description, /null when not used/);
		assert.match(spec.properties.execution.description, /kind work/);
		assert.match(spec.properties.paths.description, /repo-relative/i);
		assert.match(manager.tools.get("to_worker")!.description, /end your turn/);
	} finally { await manager.close(); }
});

test("strict-mode placeholders (nulls, blanks, false flags) pass the registered schema and reach the backend unchanged", async () => {
	const manager = await startBridge("manager");
	try {
		const tool = manager.tools.get("to_worker")!;
		const smoke = {
			i: "dispatch", task_id: null, kind: "work", message: "Create work/hello.txt",
			spec: { goal: "create hello.txt", paths: ["work/hello.txt"], instructions: null, execution: null },
			run: null, cancel: null,
		};
		const blanks = {
			task_id: "", kind: "work", message: "m",
			spec: { goal: "g", paths: [], instructions: "", execution: {
				source: "", commit: "", command: "", environment: [], shell: "bash",
				criteria: { log_contains: "", result_file: "", result_contains: "" } } },
			run: false, cancel: false,
		};
		// OMP adds its intent field `i` to the provider copy of the schema and strips it before execute.
		const { i: _i, ...smokeArgs } = smoke;
		for (const args of [smokeArgs, blanks]) assert.deepEqual(schemaErrors(tool.parameters, args), [], JSON.stringify(args));
		assert.notDeepEqual(schemaErrors(tool.parameters, { kind: "work", message: null }), []);
		assert.notDeepEqual(schemaErrors(tool.parameters, { kind: "work", message: "m", role: null }), []);
		const pending = tool.execute("call-null", smoke, new AbortController().signal, () => {}, {});
		const request = await manager.waitFor(frame => frame.kind === "tool_request");
		const { i: _intent, ...forwarded } = smoke;
		assert.deepEqual(request.args, forwarded, "the backend, not the bridge, decides what null means");
		manager.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "call-null", result: { status: "dispatched" } });
		assert.equal(textOf(await pending).status, "dispatched");
	} finally { await manager.close(); }
	const worker = await startBridge("worker");
	try {
		const tool = worker.tools.get("to_manager")!;
		for (const args of [
			{ kind: "done", message: "m", task_id: null, in_reply_to: null, requires_code_change: null, reason: null, request: null },
			{ kind: "done", message: "m", task_id: "", in_reply_to: "", requires_code_change: false, reason: "",
				request: { goal: "none", paths: [] } },
		]) assert.deepEqual(schemaErrors(tool.parameters, args), [], JSON.stringify(args));
	} finally { await worker.close(); }
});

// C-D68 (1): the worker's `terminal` tool; (3): the manager's to_worker says the worker does the task.
test("worker registers terminal: strict, essential, one nullable command (no wait input); manager has none", async () => {
	const worker = await startBridge("worker");
	try {
		const tool = worker.tools.get("terminal")!;
		assert.equal(tool.strict, true);
		assert.equal(tool.loadMode, "essential");
		const parameters = tool.parameters as Schema;
		assert.equal(parameters.additionalProperties, false);
		// C-D68 (9): the wait is fixed at 120 s; the worker sets none.
		assert.deepEqual(Object.keys(parameters.properties), ["command"]);
		assert.deepEqual([...parameters.required], ["command"]);
		assert.deepEqual(parameters.properties.command.type, ["string", "null"]);
		assert.doesNotMatch(JSON.stringify(parameters) + tool.description, /timeout_seconds|1800/);
		assert.match(tool.description, /waits up to 120 s for the exit/);
		// C-D68 (7): the host shell's current directory (where the user last cd'd), not the project directory.
		assert.match(parameters.properties.command.description, /current directory of the host terminal/);
		assert.doesNotMatch(parameters.properties.command.description, /project directory/);
		assert.match(tool.description, /current directory \(where the user last cd'd\)/);
		assert.doesNotMatch(tool.description, /project directory/);
		for (const needle of [/Workbench host terminal \(visible to the user\)/, /exit code/, /every shell command/,
			/no other way to run commands/, /only when the host terminal is free/, /host_terminal_busy/,
			/One command at a time/, /terminal_command_running/, /keeps running/,
			// C-D68 (8): on running the worker ends its turn; Workbench checks every 60 s and notifies the end.
			/status running: end your turn and do not start another command/,
			/check every 60 s while it runs and a completion notice when it exits/,
			// smoke-01 M3: no progress spam after running.
			/do not send progress reports about it unless the user or the manager asks or a check shows a problem/]) {
			assert.match(tool.description, needle);
		}
		for (const text of [tool.description, parameters.properties.command.description]) {
			assert.doesNotMatch(text, /wait for it again|waits again|to wait again/, "no re-wait loop");
		}
		for (const args of [{ command: "pytest -q" }, { command: null }]) {
			assert.deepEqual(schemaErrors(parameters, args), [], JSON.stringify(args));
		}
		assert.notDeepEqual(schemaErrors(parameters, { command: "ls", cwd: "/" }), []);
		assert.notDeepEqual(schemaErrors(parameters, { command: "ls", timeout_seconds: 180 }), [], "no wait input");
	} finally { await worker.close(); }
	const manager = await startBridge("manager");
	try {
		assert.equal(manager.tools.has("terminal"), false, "the manager keeps its own OMP tools (C-D68 (3))");
		const description = manager.tools.get("to_worker")!.description;
		assert.match(description, /The worker does the delegated task, not you: do not do it yourself/);
		assert.match(description, /wait for the worker's to_manager report/);
	} finally { await manager.close(); }
});

test("terminal waits a fixed 120 s plus a start slack (C-D68 (9))", () => {
	// C-D68 (9): fixed 120 s + the start slack, whatever the call carries.
	for (const params of [{ command: "x" }, { command: "x", timeout_seconds: 1800 }, { command: "x", timeout_seconds: 1 }, null]) {
		assert.equal(terminalTimeoutMs(params), 150_000, JSON.stringify(params));
	}
});

test("terminal sends one tool_request and outlives the 10 s handoff timeout", async () => {
	const bridge = await startBridge("worker");
	try {
		const pending = bridge.tools.get("terminal")!.execute("call-term", { i: "run tests", command: "pytest -q" }, new AbortController().signal, () => {}, {});
		const request = await bridge.waitFor(frame => frame.kind === "tool_request");
		assert.equal(request.tool, "terminal");
		assert.deepEqual(request.args, { command: "pytest -q" });
		await delay(10_400);
		const backendResult = { status: "exited", exit_code: 0, output_tail: "1 passed", log_path: "/tmp/x.log" };
		bridge.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "call-term", result: backendResult });
		assert.deepEqual(textOf(await pending), backendResult);
		assert.equal(bridge.frames.filter(frame => frame.kind === "tool_request").length, 0, "exactly one request frame");
	} finally { await bridge.close(); }
});

test("aborting a terminal call stops only the wait: the result says the command keeps running", async () => {
	const bridge = await startBridge("worker");
	try {
		const controller = new AbortController();
		const pending = bridge.tools.get("terminal")!.execute("call-ta", { command: "sleep 100" },
			controller.signal, () => {}, {});
		await bridge.waitFor(frame => frame.kind === "tool_request");
		controller.abort();
		const aborted = textOf(await pending);
		assert.equal(aborted.status, "outcome_unknown");
		assert.equal(aborted.reason, "aborted");
		assert.match(aborted.detail, /keeps running in the host terminal; end your turn/);
		assert.doesNotMatch(aborted.detail, /command null/);
		// C-D68 (8): the backend learns that this call no longer waits, so the completion notice is still sent.
		const abandoned = await bridge.waitFor(frame => frame.kind === "tool_request");
		assert.equal(abandoned.tool, "terminal_wait_abandoned");
		assert.deepEqual(abandoned.args, { tool_call_id: "call-ta" });
		assert.notEqual(abandoned.toolCallId, "call-ta");
		await delay(30);
		assert.equal(bridge.frames.filter(frame => frame.kind !== "state").length, 0, "nothing is sent to stop the command");
	} finally { await bridge.close(); }
});

// C-D68 (8): Workbench notices for the worker (terminal check / completion), outside any Task message.
test("worker takes a Workbench notice only when idle and unpaused, once per notice_id", async () => {
	const bridge = await startBridge("worker");
	try {
		const notice = { notice_id: randomUUID(), type: "terminal_done", command: "make", exit_code: 0 };
		bridge.omp.idle = false;
		bridge.reply({ kind: "notice", requestId: "n1", notice });
		let ack = await bridge.waitFor(frame => frame.kind === "api_ack" && frame.requestId === "n1");
		assert.equal(ack.status, "deferred");
		assert.equal(bridge.userMessages.length, 0, "a busy worker gets nothing");
		bridge.omp.idle = true;
		bridge.reply({ kind: "notice", requestId: "n2", notice });
		ack = await bridge.waitFor(frame => frame.kind === "api_ack" && frame.requestId === "n2");
		assert.equal(ack.status, "api_accepted");
		assert.equal(bridge.userMessages.length, 1);
		const message = JSON.parse(bridge.userMessages[0]);
		assert.equal(message.workbench_notice, "terminal_done");
		assert.equal(message.notice_id, notice.notice_id);
		assert.equal(message.exit_code, 0);
		bridge.reply({ kind: "notice", requestId: "n3", notice });
		ack = await bridge.waitFor(frame => frame.kind === "api_ack" && frame.requestId === "n3");
		assert.equal(ack.status, "duplicate_api_accepted");
		assert.equal(bridge.userMessages.length, 1, "never twice");
		bridge.reply({ kind: "pause", requestId: "p1" });
		await bridge.waitFor(frame => frame.kind === "api_ack" && frame.requestId === "p1");
		bridge.reply({ kind: "notice", requestId: "n4", notice: { ...notice, notice_id: randomUUID() } });
		ack = await bridge.waitFor(frame => frame.kind === "api_ack" && frame.requestId === "n4");
		assert.deepEqual([ack.status, ack.reason], ["deferred", "paused"]);
		for (const bad of [{ type: "terminal_check" }, { notice_id: randomUUID(), type: "other" }, "x"]) {
			bridge.reply({ kind: "notice", requestId: "nb", notice: bad });
			ack = await bridge.waitFor(frame => frame.kind === "api_ack" && frame.requestId === "nb");
			assert.equal(ack.status, "rejected");
		}
		assert.equal(bridge.userMessages.length, 1);
	} finally { await bridge.close(); }
	const manager = await startBridge("manager");
	try {
		manager.reply({ kind: "notice", requestId: "m1", notice: { notice_id: randomUUID(), type: "terminal_check" } });
		const ack = await manager.waitFor(frame => frame.kind === "api_ack" && frame.requestId === "m1");
		assert.equal(ack.status, "rejected");
		assert.equal(manager.userMessages.length, 0);
	} finally { await manager.close(); }
});

// p27-cd68-review-01 P2-2 (measured on OMP 18.6.1, /tmp/cd68fix-probe): every subagent session runs its own
// instance of this extension with ctx.agent = {kind: "sub", depth: 1, parentId, name}; the main session has
// {kind: "main", depth: 0}. A subagent instance never connects (its hello would replace the worker's bridge
// peer) and its tools refuse at once.
const SUBAGENT = { kind: "sub", id: "Probe", name: "explorer", depth: 1, parentId: "Main" };
test("a subagent session never connects to the bridge and its bridge tools refuse", async () => {
	const sub = await startBridge("worker", SUBAGENT);
	try {
		await delay(100);
		assert.equal(sub.frames.filter(frame => frame.kind === "hello").length, 0, "no second worker hello");
		// p27-cd68-fix-02 (measured on OMP 18.6.1): no registration-time signal exists, so at the subagent's
		// session_start the bridge tools leave the session's active tools: the subagent model is not offered them.
		assert.deepEqual(sub.activeToolSets, [["read", "grep", "yield"]]);
		for (const [name, args] of [["to_manager", { kind: "progress", message: "x" }],
			["terminal", { command: "echo hi" }]] as const) {
			const result = textOf(await sub.tools.get(name)!.execute(`call-${name}`, args,
				new AbortController().signal, () => {}, { agent: SUBAGENT }));
			assert.deepEqual([result.status, result.reason], ["rejected", "subagent_not_allowed"], name);
			assert.match(result.detail, /only the worker itself/);
		}
		await delay(30);
		assert.equal(sub.frames.filter(frame => frame.kind !== "state").length, 0, "nothing is sent");
	} finally { await sub.close(); }
	const main = await startBridge("worker", { kind: "main", id: "Main", name: "main", depth: 0 });
	try {
		assert.deepEqual(main.activeToolSets, [], "the worker itself keeps every tool");
		// A subagent ctx reaching the main instance's tool (shared registration) is refused as well.
		const refused = textOf(await main.tools.get("to_manager")!.execute("call-x", { kind: "progress", message: "x" },
			new AbortController().signal, () => {}, { agent: SUBAGENT }));
		assert.equal(refused.reason, "subagent_not_allowed");
		const pending = main.tools.get("to_manager")!.execute("call-main", { kind: "progress", message: "x" },
			new AbortController().signal, () => {}, { agent: { kind: "main", depth: 0 } });
		const request = await main.waitFor(frame => frame.kind === "tool_request");
		assert.equal(request.toolCallId, "call-main");
		main.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "call-main", result: { status: "queued" } });
		assert.equal(textOf(await pending).status, "queued");
	} finally { await main.close(); }
});
