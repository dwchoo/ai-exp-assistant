// C-D70 independent bridge tests (p27-cd70-test-01): the manager-only recovery tools and the per-role Workbench
// notices, against a fake backend peer on a real Unix socket. No OMP, no provider. Expectations from DECISIONS.md
// C-D70 (2), (3), (5) and the assignment p27-cd70-01 (2)/(3)/(6), not from bridge.ts.
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
const SESSION_ID = "30000000-0000-4000-8000-0000000cd070";
const MANAGER_NOTICES = ["worker_stalled", "report_delivery_unknown", "worker_restarted", "manager_recovery"];

async function startBridge(role: "manager" | "worker") {
	const directory = await mkdtemp(join(tmpdir(), "p27cd70-bridge-"));
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
	const omp = { idle: true, editor: "", pending: false };
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerTool(tool: Frame) { tools.set(tool.name, tool); },
		getActiveTools: () => ["read", ...tools.keys()],
		setActiveTools() {},
		async sendUserMessage(text: string) { userMessages.push(text); },
		logger: { error() {} },
	};
	const context = {
		sessionManager: { getSessionId: () => SESSION_ID },
		isIdle: () => omp.idle, hasPendingMessages: () => omp.pending,
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
	return { frames, tools, waitFor, reply, ack, close, userMessages, omp, context };
}

test("C-D70 (3)/(5): restart_worker and workbench_status exist for the manager only", async () => {
	const manager = await startBridge("manager");
	try {
		assert.deepEqual([...manager.tools.keys()].sort(), ["restart_worker", "to_worker", "workbench_status"]);
		const restart = manager.tools.get("restart_worker")!;
		assert.deepEqual(restart.parameters.required, ["reason"], "reason is required");
		assert.equal(restart.parameters.additionalProperties, false);
		assert.equal(restart.parameters.properties.reason.type, "string", "a reason is never null");
		assert.ok(restart.parameters.properties.reason.minLength >= 1, "a blank reason is refused locally");
		assert.ok(restart.parameters.properties.reason.maxLength <= 2000, "the reason is bounded");
		const status = manager.tools.get("workbench_status")!;
		assert.equal(status.parameters.additionalProperties, false);
		assert.deepEqual(Object.keys(status.parameters.properties).filter(name => name !== "task_id"), [],
			"workbench_status takes no argument but an optional task_id");
		assert.match(status.description, /read-only/i);
	} finally { await manager.close(); }
	const worker = await startBridge("worker");
	try {
		assert.deepEqual([...worker.tools.keys()].sort(), ["terminal", "to_manager"],
			"the worker can neither restart itself nor read the manager's status");
	} finally { await worker.close(); }
});

test("C-D70 (3): restart_worker goes to the backend as a manager tool_request with the reason and returns its result", async () => {
	const manager = await startBridge("manager");
	try {
		const tool = manager.tools.get("restart_worker")!;
		const running = tool.execute("call-rw-1", { reason: "no answer after 2 status checks" }, undefined, undefined,
			manager.context);
		const request = await manager.waitFor(frame => frame.kind === "tool_request");
		assert.equal(request.tool, "restart_worker");
		assert.deepEqual(request.args, { reason: "no answer after 2 status checks" });
		assert.equal(request.sessionId, SESSION_ID);
		const result = { status: "restarted", worker: { generation: 2 }, detail: "follow-up on the same task_id" };
		manager.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: request.toolCallId, result });
		const answered = await running;
		assert.deepEqual(JSON.parse(answered.content[0].text), result);
	} finally { await manager.close(); }
});

test("C-D70 (2): each role takes only its own Workbench notice types; a manager notice is injected once", async () => {
	const manager = await startBridge("manager");
	try {
		let n = 0;
		for (const type of MANAGER_NOTICES) {
			const notice = { notice_id: randomUUID(), type, task_id: randomUUID(), instruction: "call workbench_status" };
			manager.reply({ kind: "notice", requestId: `m${++n}`, notice });
			assert.equal((await manager.ack(`m${n}`)).status, "api_accepted", type);
			manager.reply({ kind: "notice", requestId: `m${++n}`, notice });
			assert.equal((await manager.ack(`m${n}`)).status, "duplicate_api_accepted", `${type} again`);
		}
		assert.equal(manager.userMessages.length, MANAGER_NOTICES.length, "each notice reaches the manager once");
		assert.deepEqual(manager.userMessages.map(text => JSON.parse(text).workbench_notice), MANAGER_NOTICES);
		for (const type of ["status_check", "terminal_done", "terminal_check", "bogus"]) {
			manager.reply({ kind: "notice", requestId: `m${++n}`, notice: { notice_id: randomUUID(), type } });
			assert.equal((await manager.ack(`m${n}`)).status, "rejected", `${type} is not a manager notice`);
		}
		assert.equal(manager.userMessages.length, MANAGER_NOTICES.length);
	} finally { await manager.close(); }
	const worker = await startBridge("worker");
	try {
		let n = 0;
		worker.reply({ kind: "notice", requestId: `w${++n}`, notice: { notice_id: randomUUID(), type: "status_check",
			task_id: randomUUID(), instruction: "report done or blocked" } });
		assert.equal((await worker.ack(`w${n}`)).status, "api_accepted");
		for (const type of MANAGER_NOTICES) {
			worker.reply({ kind: "notice", requestId: `w${++n}`, notice: { notice_id: randomUUID(), type } });
			assert.equal((await worker.ack(`w${n}`)).status, "rejected", `${type} never goes into the worker`);
		}
		assert.equal(worker.userMessages.length, 1);
	} finally { await worker.close(); }
});

test("C-D70 (2): a manager notice never disturbs the user's composer or a busy manager; it is deferred, not lost", async () => {
	const manager = await startBridge("manager");
	try {
		const notice = { notice_id: randomUUID(), type: "worker_stalled", task_id: randomUUID(), idle_seconds: 180 };
		manager.omp.editor = "the user is typing";
		manager.reply({ kind: "notice", requestId: "a", notice });
		assert.equal((await manager.ack("a")).status, "deferred");
		manager.omp.editor = "";
		manager.omp.idle = false;
		manager.reply({ kind: "notice", requestId: "b", notice });
		assert.equal((await manager.ack("b")).status, "deferred");
		manager.omp.idle = true;
		manager.omp.pending = true;
		manager.reply({ kind: "notice", requestId: "c", notice });
		assert.equal((await manager.ack("c")).status, "deferred");
		assert.equal(manager.userMessages.length, 0);
		manager.omp.pending = false;
		manager.reply({ kind: "notice", requestId: "d", notice });
		assert.equal((await manager.ack("d")).status, "api_accepted");
		assert.equal(manager.userMessages.length, 1, "the retried notice goes in exactly once");
	} finally { await manager.close(); }
});
