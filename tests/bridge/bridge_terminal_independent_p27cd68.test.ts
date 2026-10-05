// C-D68 independent bridge tests (p27-cd68-test-01): the worker `terminal` tool, Workbench notices and the manager
// rule, against a fake backend peer on a real Unix socket. No OMP, no provider. Expectations from DECISIONS.md C-D68
// (1), (3), (7), (8), (9), not from bridge.ts.
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
const SESSION_ID = "30000000-0000-4000-8000-0000000cd068";

async function startBridge(role: "manager" | "worker") {
	const directory = await mkdtemp(join(tmpdir(), "p27cd68-bridge-"));
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
	const omp = { idle: true, editor: "" };
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerTool(tool: Frame) { tools.set(tool.name, tool); },
		async sendUserMessage(text: string) { userMessages.push(text); },
		logger: { error() {} },
	};
	const context = {
		sessionManager: { getSessionId: () => SESSION_ID },
		isIdle: () => omp.idle, hasPendingMessages: () => false,
		ui: { getEditorText: () => omp.editor }, abort: () => {},
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
	async function ack(requestId: string): Promise<Frame> {
		return waitFor(frame => frame.kind === "api_ack" && frame.requestId === requestId);
	}
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
	return { frames, handlers, tools, waitFor, reply, ack, close, userMessages, omp };
}

function textOf(result: Frame): Frame {
	return JSON.parse(result.content[0].text);
}

test("C-D68 (1)/(3): terminal exists only for the worker; the manager keeps to_worker with the manager rule", async () => {
	const worker = await startBridge("worker");
	try {
		assert.deepEqual([...worker.tools.keys()].sort(), ["terminal", "to_manager"]);
		const terminal = worker.tools.get("terminal")!;
		assert.equal(terminal.loadMode, "essential", "the model must see it without discovery");
		assert.match(terminal.description, /no other way to run commands/i);
		assert.match(terminal.description, /host terminal/);
	} finally { await worker.close(); }
	const manager = await startBridge("manager");
	try {
		assert.deepEqual([...manager.tools.keys()], ["to_worker"]);
		const description = manager.tools.get("to_worker")!.description as string;
		// C-D68 (3): work handed over with to_worker is not done by the manager; it waits for the worker's report.
		assert.match(description, /do not do it yourself/i);
		assert.match(description, /wait for the worker's to_manager report/i);
	} finally { await manager.close(); }
});

test("C-D68 (9): the wait is fixed at 120 s; no timeout_seconds in the schema or description", async () => {
	// C-D68 (9) replaces (7)'s "default 120 s, at most 1800 s": the worker sets no wait.
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		assert.deepEqual(Object.keys(terminal.parameters.properties), ["command"]);
		assert.deepEqual(terminal.parameters.required, ["command"]);
		assert.equal(terminal.parameters.additionalProperties, false);
		assert.doesNotMatch(JSON.stringify(terminal), /timeout_seconds|1800/);
		assert.match(terminal.description, /120 s/);
		assert.match(terminal.description, /end your turn/i);
		assert.match(terminal.description, /do not send progress reports/i);
	} finally { await worker.close(); }
	// The bridge waits for the backend's answer a bit longer than the fixed wait, whatever the arguments are.
	const base = terminalTimeoutMs({ command: "x" });
	assert.ok(base >= 120_000 && base <= 180_000, `fixed ${base}`);
	for (const params of [{ command: "x", timeout_seconds: 1800 }, { command: "x", timeout_seconds: 50_000 },
		{ command: "x", timeout_seconds: null }, { command: "x", timeout_seconds: "60" }, undefined, null]) {
		assert.equal(terminalTimeoutMs(params), base, JSON.stringify(params));
	}
});

test("C-D68 (10): a command-less terminal call is answered at once, so the bridge waits only briefly for it", async () => {
	const command = terminalTimeoutMs({ command: "x" });
	for (const params of [{ command: null }, { command: null, timeout_seconds: 1800 }]) {
		const limit = terminalTimeoutMs(params);
		assert.ok(limit <= 15_000, `a command-less call must not get the 120 s wait: ${limit}`);
		assert.ok(limit < command, "shorter than a command call's wait");
	}
	assert.equal(terminalTimeoutMs({ command: "make" }), command, "a command call keeps the fixed wait");
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		assert.match(terminal.description, /one short text line/i);
		assert.match(terminal.description, /never an empty reply/i);
		const pending = terminal.execute("fetch-1", { command: null }, new AbortController().signal, () => {}, {});
		const request = await worker.waitFor(frame => frame.kind === "tool_request" && frame.tool === "terminal");
		assert.deepEqual(request.args, { command: null });
		worker.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "fetch-1",
			result: { status: "running", output_tail: "more" } });
		assert.equal(textOf(await pending).status, "running");
	} finally { await worker.close(); }
});

test("C-D68 (8): a terminal call answered by the backend is not abandoned; an abort after the answer sends nothing", async () => {
	const worker = await startBridge("worker");
	try {
		const controller = new AbortController();
		const pending = worker.tools.get("terminal")!.execute("call-ok", { command: "make" },
			controller.signal, () => {}, {});
		const request = await worker.waitFor(frame => frame.kind === "tool_request");
		assert.equal(request.tool, "terminal");
		assert.deepEqual(request.args, { command: "make" });
		worker.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "call-ok",
			result: { status: "exited", exit_code: 2, output_tail: "boom" } });
		assert.deepEqual(textOf(await pending), { status: "exited", exit_code: 2, output_tail: "boom" });
		controller.abort();
		await delay(50);
		assert.deepEqual(worker.frames.filter(frame => frame.kind === "tool_request"), [],
			"an abort after the result must not tell the backend the result was not received");
	} finally { await worker.close(); }
});

test("C-D68 (8): two aborted terminal calls each name their own call in the abandon signal", async () => {
	const worker = await startBridge("worker");
	try {
		for (const id of ["call-a1", "call-a2"]) {
			const controller = new AbortController();
			const pending = worker.tools.get("terminal")!.execute(id, { command: null },
				controller.signal, () => {}, {});
			const request = await worker.waitFor(frame => frame.kind === "tool_request" && frame.tool === "terminal");
			assert.equal(request.toolCallId, id);
			controller.abort();
			assert.equal(textOf(await pending).status, "outcome_unknown");
			const abandoned = await worker.waitFor(frame => frame.kind === "tool_request"
				&& frame.tool === "terminal_wait_abandoned");
			assert.deepEqual(abandoned.args, { tool_call_id: id });
			assert.equal(abandoned.sessionId, SESSION_ID);
		}
	} finally { await worker.close(); }
});

test("C-D68 (8): a notice held while paused is accepted after the resume, once; a busy worker defers it", async () => {
	const worker = await startBridge("worker");
	try {
		const notice = { notice_id: randomUUID(), type: "terminal_done", command: "pytest", exit_code: 1,
			output_tail: "1 failed", log_path: "/tmp/x.log" };
		worker.reply({ kind: "pause", requestId: "p" });
		await worker.ack("p");
		worker.reply({ kind: "notice", requestId: "n1", notice });
		assert.deepEqual([(await worker.ack("n1")).status], ["deferred"]);
		assert.equal(worker.userMessages.length, 0, "nothing reaches a paused worker");
		worker.reply({ kind: "resume", requestId: "r", reconciled: true });
		assert.equal((await worker.ack("r")).status, "resumed");
		worker.omp.editor = "user is typing";
		worker.reply({ kind: "notice", requestId: "n2", notice });
		assert.equal((await worker.ack("n2")).status, "deferred", "the user's composer text is never disturbed");
		worker.omp.editor = "";
		worker.reply({ kind: "notice", requestId: "n3", notice });
		assert.equal((await worker.ack("n3")).status, "api_accepted");
		assert.equal(worker.userMessages.length, 1);
		const message = JSON.parse(worker.userMessages[0]);
		assert.equal(message.workbench_notice, "terminal_done");
		for (const key of ["command", "exit_code", "output_tail", "log_path"]) assert.deepEqual(message[key], (notice as Frame)[key]);
		worker.reply({ kind: "notice", requestId: "n4", notice });
		assert.equal((await worker.ack("n4")).status, "duplicate_api_accepted");
		assert.equal(worker.userMessages.length, 1, "exactly once");
		const check = { notice_id: randomUUID(), type: "terminal_check", command: "pytest", elapsed_seconds: 61,
			new_output: "..." };
		worker.reply({ kind: "notice", requestId: "n5", notice: check });
		assert.equal((await worker.ack("n5")).status, "api_accepted");
		assert.equal(JSON.parse(worker.userMessages[1]).workbench_notice, "terminal_check");
		worker.reply({ kind: "notice", requestId: "n6", notice: { ...check, notice_id: "not-a-uuid" } });
		assert.equal((await worker.ack("n6")).status, "rejected");
		assert.equal(worker.userMessages.length, 2);
	} finally { await worker.close(); }
});
