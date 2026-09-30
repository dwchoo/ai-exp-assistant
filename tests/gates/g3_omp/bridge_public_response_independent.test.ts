import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import workbenchG3Extension from "../../../omp_bridge/g3/bridge.ts";

type Frame = Record<string, any>;
const SESSION = "40000000-0000-4000-8000-000000000001";
const MARKER = "WB_WORKER_RESPONSE:";

async function fixture() {
	const directory = await mkdtemp(join(tmpdir(), "cw10-public-response-"));
	const socketPath = join(directory, "bridge.sock");
	const frames: Frame[] = [];
	const sockets: net.Socket[] = [];
	const server = net.createServer(socket => {
		sockets.push(socket);
		let pending = "";
		socket.on("data", chunk => {
			pending += chunk.toString();
			while (pending.includes("\n")) {
				const index = pending.indexOf("\n");
				frames.push(JSON.parse(pending.slice(0, index)) as Frame);
				pending = pending.slice(index + 1);
			}
		});
	});
	await new Promise<void>((resolve, reject) => {
		server.once("error", reject);
		server.listen(socketPath, resolve);
	});
	const keys = ["WORKBENCH_G3_BRIDGE_SOCKET", "WORKBENCH_G3_ROLE",
		"WORKBENCH_G3_TOKEN", "WORKBENCH_G3_GENERATION",
		"WORKBENCH_G3_EXPECTED_RESPONSE_MARKER"];
	const previous = new Map(keys.map(key => [key, process.env[key]]));
	process.env.WORKBENCH_G3_BRIDGE_SOCKET = socketPath;
	process.env.WORKBENCH_G3_ROLE = "worker";
	process.env.WORKBENCH_G3_TOKEN = randomUUID();
	process.env.WORKBENCH_G3_GENERATION = "1";
	delete process.env.WORKBENCH_G3_EXPECTED_RESPONSE_MARKER;
	const handlers = new Map<string, (...args: any[]) => any>();
	const sent: string[] = [];
	workbenchG3Extension({
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerTool() {},
		zod: { object: (shape: Frame) => shape, string: () => "string" },
		async sendUserMessage(message: string) { sent.push(message); },
		logger: { error() {} },
	});
	const context = {
		sessionManager: { getSessionId: () => SESSION },
		isIdle: () => true, hasPendingMessages: () => false,
		ui: { getEditorText: () => "" }, abort() {},
	};
	handlers.get("session_start")!(undefined, context);
	async function wait(predicate: (item: Frame) => boolean, timeout = 1200) {
		const deadline = Date.now() + timeout;
		while (Date.now() < deadline) {
			const index = frames.findIndex(predicate);
			if (index >= 0) return frames.splice(index, 1)[0];
			await delay(5);
		}
		throw new Error("public bridge event timed out");
	}
	await wait(item => item.kind === "hello");
	async function request(payload: Frame) {
		const requestId = randomUUID();
		sockets.at(-1)!.write(JSON.stringify({ ...payload, requestId }) + "\n");
		return wait(item => item.kind === "api_ack" && item.requestId === requestId);
	}
	async function close() {
		handlers.get("session_shutdown")?.();
		for (const socket of sockets) socket.destroy();
		await new Promise<void>(resolve => server.close(() => resolve()));
		for (const key of keys) {
			const value = previous.get(key);
			if (value === undefined) delete process.env[key];
			else process.env[key] = value;
		}
		await rm(directory, { recursive: true, force: true });
	}
	return { directory, frames, sockets, handlers, sent, wait, request, close, context };
}

function delivery() {
	return {
		schemaVersion: 1, messageId: randomUUID(), deliveryAttemptId: randomUUID(),
		senderRole: "manager", sessionId: SESSION, sessionGeneration: 1,
		taskId: randomUUID(), revisionId: randomUUID(), runId: randomUUID(),
		event: { type: "message", messageKind: "task", payload: { stage: "execute", revision: 1 } },
	};
}

function response(target: Frame): Frame {
	return {
		stage: "execute", kind: "task", task_id: target.taskId, revision_id: target.revisionId,
		revision: 1, run_id: target.runId, message_id: target.messageId,
		delivery_attempt_id: target.deliveryAttemptId, session_id: SESSION,
		session_generation: 1, response_id: randomUUID(), decision: "execute",
	};
}

function frameFromDeliveredContract(contract: Frame, decision: string): string {
	const values: Frame = {};
	for (const descriptor of contract.fields as Frame[]) {
		values[descriptor.name] = descriptor.generate === "canonical_uuid"
			? randomUUID() : descriptor.allowed ? decision : descriptor.value;
	}
	return contract.marker + JSON.stringify(values);
}

async function beginTurn(f: Awaited<ReturnType<typeof fixture>>, target: Frame) {
	assert.equal((await f.request({ kind: "deliver", envelope: JSON.stringify(target) })).status, "api_accepted");
	f.handlers.get("before_provider_request")!({ payload: { messages: [
		{ role: "user", content: [{ type: "text", text: f.sent.at(-1)! }] },
	] } });
	f.handlers.get("after_provider_response")!();
}

function assistant(f: Awaited<ReturnType<typeof fixture>>, text: string,
	options: { stopReason?: string; willContinue?: boolean; errorMessage?: string;
		extraBlock?: boolean; thinkingBlock?: boolean } = {}) {
	const content = [{ type: "text", text }];
	if (options.extraBlock) content.push({ type: "text", text: "other" });
	if (options.thinkingBlock) content.push({ type: "thinking", text: "CW10_PRIVATE_THINKING_SENTINEL" });
	f.handlers.get("message_end")!({ message: {
		role: "assistant", content, stopReason: options.stopReason ?? "stop",
		...(options.errorMessage ? { errorMessage: options.errorMessage } : {}),
	}, willContinue: options.willContinue ?? false });
}

test("public worker response appears only after terminal agent_end and contains allowlisted identity", async () => {
	const f = await fixture();
	try {
		const target = delivery();
		const expected = response(target);
		await beginTurn(f, target);
		assistant(f, MARKER + JSON.stringify(expected));
		assert.equal(f.frames.some(item => item.name === "assistant_message_end"), false);
		f.handlers.get("agent_end")!();
		const observed = await f.wait(item => item.name === "assistant_message_end");
		const processed = await f.wait(item => item.name === "delivery_omp_processed");
		assert.deepEqual(observed.workerResponse, expected);
		assert.equal(processed.workerResponseId, expected.response_id);
		assert.equal(observed.messageId, target.messageId);
		assert.equal(observed.deliveryAttemptId, target.deliveryAttemptId);
		assert.equal(JSON.stringify({ observed, processed }).includes(MARKER), false);
	} finally { await f.close(); }
});

test("execute and analysis deliveries carry complete, stage-bound response instructions", async () => {
	for (const stage of ["execute", "analysis"]) {
		const f = await fixture();
		try {
			const target = delivery();
			if (stage === "analysis") {
				target.event.messageKind = "question";
				target.event.payload.stage = "analysis";
			}
			await beginTurn(f, target);
			assert.equal(f.sent.length, 1);
			const delivered = JSON.parse(f.sent[0]);
			const contract = delivered.response_contract;
			assert.equal(contract.version, 1);
			assert.equal(contract.marker, MARKER);
			assert.equal(contract.format, "marker_plus_compact_flat_json");
			assert.deepEqual(contract.field_order, ["stage", "kind", "task_id", "revision_id",
				"revision", "run_id", "message_id", "delivery_attempt_id", "session_id",
				"session_generation", "response_id", "decision"]);
			assert.deepEqual(contract.fields.map((item: Frame) => item.name), contract.field_order);
			assert.deepEqual(contract.fields, [
				{ name: "stage", type: "literal_string", value: stage },
				{ name: "kind", type: "literal_string", value: target.event.messageKind },
				{ name: "task_id", type: "canonical_uuid", value: target.taskId },
				{ name: "revision_id", type: "canonical_uuid", value: target.revisionId },
				{ name: "revision", type: "positive_safe_integer", value: 1 },
				{ name: "run_id", type: "canonical_uuid", value: target.runId },
				{ name: "message_id", type: "canonical_uuid", value: target.messageId },
				{ name: "delivery_attempt_id", type: "canonical_uuid", value: target.deliveryAttemptId },
				{ name: "session_id", type: "canonical_uuid", value: SESSION },
				{ name: "session_generation", type: "positive_safe_integer", value: 1 },
				{ name: "response_id", type: "canonical_uuid", generate: "canonical_uuid" },
				{ name: "decision", type: "enum_string", allowed: stage === "execute"
					? ["execute", "hold"] : ["success", "failure", "indeterminate"] },
			]);
			assert.deepEqual(contract.output_rules, {
				exactly_one_frame: true, no_prose: true, no_tools: true,
				no_thinking: true, no_markdown: true, no_extra_content: true,
			});
			for (const phrase of ["marker", "compact flat JSON", "field_order", "no prose",
				"tools", "thinking", "markdown", "additional messages"]) {
				assert.match(contract.instruction, new RegExp(phrase));
			}
			const decision = stage === "execute" ? "execute" : "success";
			assistant(f, frameFromDeliveredContract(contract, decision));
			f.handlers.get("agent_end")!();
			const observed = await f.wait(item => item.name === "assistant_message_end");
			const processed = await f.wait(item => item.name === "delivery_omp_processed");
			assert.equal(observed.workerResponse.decision, decision);
			assert.equal(processed.workerResponseId, observed.workerResponse.response_id);
		} finally { await f.close(); }
	}
});

test("parser rejects changed contract frames, reordered keys, and natural-language-only reply", async () => {
	for (const variant of ["marker", "order", "identity", "decision", "response_id", "extra", "missing", "prose"]) {
		const f = await fixture();
		try {
			const target = delivery();
			await beginTurn(f, target);
			const contract = JSON.parse(f.sent[0]).response_contract;
			const valid = frameFromDeliveredContract(contract, "execute");
			const body = JSON.parse(valid.slice(contract.marker.length));
			let text = valid;
			switch (variant) {
				case "marker": text = "CHANGED_MARKER:" + JSON.stringify(body); break;
				case "order": text = contract.marker + JSON.stringify({ kind: body.kind, stage: body.stage,
					...Object.fromEntries(Object.entries(body).slice(2)) }); break;
				case "identity": text = contract.marker + JSON.stringify({ ...body, task_id: randomUUID() }); break;
				case "decision": text = contract.marker + JSON.stringify({ ...body, decision: "success" }); break;
				case "response_id": text = contract.marker + JSON.stringify({ ...body, response_id: "not-uuid" }); break;
				case "extra": text = contract.marker + JSON.stringify({ ...body, unexpected: "x" }); break;
				case "missing": { const { revision_id: _missing, ...rest } = body; text = contract.marker + JSON.stringify(rest); break; }
				case "prose": text = "I completed the task."; break;
			}
			assistant(f, text);
			f.handlers.get("agent_end")!();
			await f.wait(item => item.name === "worker_response_rejected");
			const processed = await f.wait(item => item.name === "delivery_omp_processed");
			assert.equal(processed.workerResponseId, undefined, variant);
			assert.equal(f.frames.some(item => item.name === "assistant_message_end"), false, variant);
		} finally { await f.close(); }
	}
});

test("strict frame parser rejects ambiguous, typed, or unbound control without leaking sentinels", async () => {
	const secret = "CW10_PRIVATE_FRAME_SENTINEL_" + randomUUID();
	const variants: Array<[string, (target: Frame, valid: Frame) => string]> = [
		["missing_marker", (_target, valid) => JSON.stringify(valid)],
		["two_markers", (_target, valid) => MARKER + MARKER + JSON.stringify(valid)],
		["prose_prefix", (_target, valid) => secret + " " + MARKER + JSON.stringify(valid)],
		["prose_suffix", (_target, valid) => MARKER + JSON.stringify(valid) + " " + secret],
		["two_json", (_target, valid) => MARKER + JSON.stringify(valid) + JSON.stringify(valid)],
		["truncated", (_target, valid) => MARKER + JSON.stringify(valid).slice(0, -1)],
		["duplicate", (_target, valid) => MARKER + JSON.stringify(valid).slice(0, -1) + ',"decision":"execute"}'],
		["unknown", (_target, valid) => MARKER + JSON.stringify(valid).slice(0, -1) + ',"secret":"' + secret + '"}'],
		["wrong_stage", (_target, valid) => MARKER + JSON.stringify({ ...valid, stage: "analysis" })],
		["wrong_kind", (_target, valid) => MARKER + JSON.stringify({ ...valid, kind: "question" })],
		["wrong_decision", (_target, valid) => MARKER + JSON.stringify({ ...valid, decision: "success" })],
		["wrong_task", (_target, valid) => MARKER + JSON.stringify({ ...valid, task_id: randomUUID() })],
		["wrong_attempt", (_target, valid) => MARKER + JSON.stringify({ ...valid, delivery_attempt_id: randomUUID() })],
		["wrong_session", (_target, valid) => MARKER + JSON.stringify({ ...valid, session_id: randomUUID() })],
		["bool_revision", (_target, valid) => MARKER + JSON.stringify({ ...valid, revision: true })],
		["float_generation", (_target, valid) => MARKER + JSON.stringify({ ...valid, session_generation: 1.5 })],
		["negative_revision", (_target, valid) => MARKER + JSON.stringify({ ...valid, revision: -1 })],
		["unsafe_generation", (_target, valid) => MARKER + JSON.stringify({ ...valid, session_generation: Number.MAX_SAFE_INTEGER + 1 })],
		["bad_uuid", (_target, valid) => MARKER + JSON.stringify({ ...valid, response_id: "not-a-uuid" })],
	];
	for (const [label, makeText] of variants) {
		const f = await fixture();
		try {
			const target = delivery();
			await beginTurn(f, target);
			assistant(f, makeText(target, response(target)));
			f.handlers.get("agent_end")!();
			const rejected = await f.wait(item => item.name === "worker_response_rejected");
			const processed = await f.wait(item => item.name === "delivery_omp_processed");
			assert.equal(rejected.reason, "invalid_assistant_response", label);
			assert.equal(processed.workerResponseId, undefined, label);
			assert.equal(f.frames.some(item => item.name === "assistant_message_end"), false, label);
			assert.equal(JSON.stringify({ rejected, processed, remaining: f.frames }).includes(secret), false, label);
		} finally { await f.close(); }
	}
});

test("continuation, extra provider round, tool, error, and abort never publish worker control", async () => {
	for (const variant of ["continuation", "extra_round", "tool", "error", "abort", "extra_block", "thinking"]) {
		const f = await fixture();
		try {
			const target = delivery();
			await beginTurn(f, target);
			assistant(f, MARKER + JSON.stringify(response(target)), {
				willContinue: variant === "continuation",
				stopReason: variant === "error" ? "error" : variant === "abort" ? "aborted" : "stop",
				errorMessage: variant === "error" ? "CW10_PRIVATE_ERROR_SENTINEL" : undefined,
				extraBlock: variant === "extra_block",
				thinkingBlock: variant === "thinking",
			});
			if (variant === "extra_round") f.handlers.get("before_provider_request")!({ payload: {
				messages: [{ role: "user", content: [{ type: "text", text: f.sent.at(-1)! }] }],
			} });
			if (variant === "tool") f.handlers.get("tool_call")!({
				toolCallId: "independent-tool", toolName: "bash",
			});
			f.handlers.get("agent_end")!();
			const terminal = await f.wait(item => item.name === "worker_response_rejected"
				|| item.name === "delivery_processing_unknown");
			assert.equal(terminal.name === "worker_response_rejected"
				|| terminal.name === "delivery_processing_unknown", true, variant);
			assert.equal(f.frames.some(item => item.name === "assistant_message_end"), false, variant);
			assert.equal(JSON.stringify({ terminal, remaining: f.frames })
				.includes("CW10_PRIVATE_ERROR_SENTINEL"), false, variant);
			assert.equal(JSON.stringify({ terminal, remaining: f.frames })
				.includes("CW10_PRIVATE_THINKING_SENTINEL"), false, variant);
		} finally { await f.close(); }
	}
});

test("pause or session switch after candidate but before terminal end prevents public control", async () => {
	for (const variant of ["pause", "switch"]) {
		const f = await fixture();
		try {
			const target = delivery();
			await beginTurn(f, target);
			assistant(f, MARKER + JSON.stringify(response(target)));
			if (variant === "pause") {
				assert.equal((await f.request({ kind: "pause" })).status, "paused");
				f.handlers.get("agent_end")!();
				const rejection = await f.wait(item => item.name === "worker_response_rejected");
				assert.equal(rejection.reason, "automation_paused");
			} else {
				f.handlers.get("session_switch")!(undefined, {
					...f.context, sessionManager: { getSessionId: () => randomUUID() },
				});
				const unknown = await f.wait(item => item.name === "delivery_processing_unknown");
				assert.equal(unknown.reason, "session_switched");
			}
			assert.equal(f.frames.some(item => item.name === "assistant_message_end"), false);
		} finally { await f.close(); }
	}
});

test("control-socket loss after assistant candidate cannot publish it on reconnect", async () => {
	const f = await fixture();
	try {
		const target = delivery();
		await beginTurn(f, target);
		assistant(f, MARKER + JSON.stringify(response(target)));
		f.sockets.at(-1)!.destroy();
		await delay(30);
		f.handlers.get("agent_end")!();
		await delay(30);
		assert.equal(f.frames.some(item => item.name === "assistant_message_end"), false);
		assert.equal(f.frames.some(item => item.name === "delivery_omp_processed"
			&& item.workerResponseId), false);
	} finally { await f.close(); }
});
