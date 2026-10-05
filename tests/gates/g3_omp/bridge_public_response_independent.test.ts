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

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

// Root adjudication p27-cw18-response-id: response_id is a technical identity the bridge generates; the model-written
// frame carries exactly the delivered identity fields plus its decision and no response_id.
function response(target: Frame): Frame {
	return {
		stage: "execute", kind: "task", task_id: target.taskId, revision_id: target.revisionId,
		revision: 1, run_id: target.runId, message_id: target.messageId,
		delivery_attempt_id: target.deliveryAttemptId, session_id: SESSION,
		session_generation: 1, decision: "execute",
	};
}

// The same frame with a model-supplied response_id inserted at `position` of the key order.
function withModelResponseId(frame: Frame, value: unknown, position: "first" | "middle" | "last"): string {
	const entries = Object.entries(frame);
	const at = position === "first" ? 0 : position === "last" ? entries.length : entries.length - 1;
	entries.splice(at, 0, ["response_id", value]);
	return MARKER + JSON.stringify(Object.fromEntries(entries));
}

// The published control is the model frame plus one bridge-generated canonical UUID v4 response_id.
function assertPublished(workerResponse: Frame, modelFrame: Frame, label = "") {
	const { response_id: generated, ...rest } = workerResponse;
	assert.deepEqual(rest, modelFrame, label);
	assert.equal(typeof generated, "string", label);
	assert.match(generated as string, UUID_V4, label);
	return generated as string;
}

// A fixture model obeys the delivered contract and owns nothing else: every descriptor is a literal or a choice.
function frameFromDeliveredContract(contract: Frame, decision: string): string {
	const values: Frame = {};
	for (const descriptor of contract.fields as Frame[]) {
		assert.equal("generate" in descriptor, false, "no model-generated fields remain in the contract");
		values[descriptor.name] = descriptor.allowed ? decision : descriptor.value;
		assert.notEqual(values[descriptor.name], undefined, `contract field ${descriptor.name} has no value`);
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
		extraBlock?: boolean; thinkingBlock?: boolean; content?: Frame[] } = {}) {
	const content: Frame[] = options.content ?? [{ type: "text", text }];
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
		const generated = assertPublished(observed.workerResponse, expected);
		assert.equal(processed.workerResponseId, generated);
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
				"session_generation", "decision"]);
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
				{ name: "decision", type: "enum_string", allowed: stage === "execute"
					? ["execute", "hold"] : ["success", "failure", "indeterminate"] },
			]);
			assert.equal(JSON.stringify(contract).includes("response_id"), false);
			assert.equal(JSON.stringify(contract).includes("generate"), false);
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
			assert.deepEqual(Object.keys(observed.workerResponse).sort(),
				[...contract.field_order, "response_id"].sort());
			assert.equal(processed.workerResponseId, observed.workerResponse.response_id);
			assert.match(observed.workerResponse.response_id, UUID_V4);
		} finally { await f.close(); }
	}
});

test("parser rejects changed contract frames, reordered keys, and natural-language-only reply", async () => {
	for (const variant of ["marker", "order", "identity", "decision", "two_response_ids", "extra", "missing", "prose"]) {
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
				case "two_response_ids": text = contract.marker + JSON.stringify(body).slice(0, -1)
					+ `,"response_id":"${randomUUID()}","response_id":"${randomUUID()}"}`; break;
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
		["two_response_ids", (_target, valid) => MARKER + JSON.stringify(valid).slice(0, -1)
			+ `,"response_id":"${randomUUID()}","response_id":"${randomUUID()}"}`],
		["two_response_ids_split", (_target, valid) => withModelResponseId(valid, randomUUID(), "first").slice(0, -1)
			+ `,"response_id":"${randomUUID()}"}`],
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
	for (const variant of ["continuation", "extra_round", "tool", "error", "abort", "extra_block"]) {
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

const THINKING_SENTINEL = "CW10_PRIVATE_THINKING_SENTINEL_C_D67";

// C-D67: a thinking item (any position, with or without text, any extra keys) is ignored for the verdict and
// is never recorded; the verdict depends only on the single marker text item.
test("C-D67: thinking items in any position or shape are ignored and a valid marker still publishes", async () => {
	const shapes: Array<[string, (marker: string, target: Frame) => Frame[]]> = [
		["visible_before", marker => [{ type: "thinking", thinking: THINKING_SENTINEL, thinkingSignature: "sig" },
			{ type: "text", text: marker }]],
		["visible_after", marker => [{ type: "text", text: marker },
			{ type: "thinking", thinking: THINKING_SENTINEL }]],
		["empty_with_signature", marker => [{ type: "thinking", thinking: "", thinkingSignature: "opaque" },
			{ type: "text", text: marker }]],
		["text_key_only", marker => [{ type: "thinking", text: THINKING_SENTINEL },
			{ type: "text", text: marker }]],
		["multiple_around", marker => [{ type: "thinking", thinking: THINKING_SENTINEL },
			{ type: "text", text: marker }, { type: "thinking", thinking: THINKING_SENTINEL + "_2", extra: { a: 1 } }]],
		["thinking_mimics_frame", (marker, target) => [{ type: "thinking",
			thinking: MARKER + JSON.stringify({ ...response(target), decision: "hold" }) + THINKING_SENTINEL },
			{ type: "text", text: marker }]],
	];
	for (const [label, build] of shapes) {
		const f = await fixture();
		try {
			const target = delivery();
			const expected = response(target);
			await beginTurn(f, target);
			assistant(f, "", { content: build(MARKER + JSON.stringify(expected), target) });
			f.handlers.get("agent_end")!();
			const observed = await f.wait(item => item.name === "assistant_message_end");
			const processed = await f.wait(item => item.name === "delivery_omp_processed");
			const generated = assertPublished(observed.workerResponse, expected, label);
			assert.equal(observed.workerResponse.decision, "execute", label);
			assert.equal(processed.workerResponseId, generated, label);
			assert.equal(f.frames.some(item => item.name === "worker_response_rejected"), false, label);
			assert.equal(JSON.stringify({ observed, processed, remaining: f.frames })
				.includes("CW10_PRIVATE_THINKING_SENTINEL"), false, label);
		} finally { await f.close(); }
	}
});

test("C-D67: thinking never rescues an otherwise invalid response", async () => {
	const thinking = { type: "thinking", thinking: THINKING_SENTINEL, thinkingSignature: "sig" };
	const variants: Array<[string, string, (marker: string, target: Frame) => Frame[],
		{ stopReason?: string; errorMessage?: string; toolEvent?: boolean; extraRound?: boolean }]> = [
		["thinking_only", "bad_marker", () => [thinking], {}],
		["thinking_empty_text", "bad_marker", () => [thinking, { type: "text", text: "" }], {}],
		["thinking_extra_text_after", "extra_text", marker => [thinking, { type: "text", text: marker },
			{ type: "text", text: "done" }], {}],
		["thinking_extra_text_before", "extra_text", marker => [thinking, { type: "text", text: "ok" },
			{ type: "text", text: marker }], {}],
		["thinking_prose_around_marker", "extra_text", marker => [thinking,
			{ type: "text", text: "Done. " + marker }], {}],
		["thinking_prose_suffix", "extra_text", marker => [thinking, { type: "text", text: marker + " ok" }], {}],
		["thinking_toolcall_item", "tool_activity", marker => [thinking, { type: "toolCall", id: "t1", name: "bash",
			arguments: {} }, { type: "text", text: marker }], {}],
		["thinking_tooluse_item", "tool_activity", marker => [thinking, { type: "tool_use", id: "t1", name: "bash",
			input: {} }, { type: "text", text: marker }], {}],
		["thinking_tool_event", "tool_activity", marker => [thinking, { type: "text", text: marker }], { toolEvent: true }],
		["thinking_bad_identity", "identity_mismatch", (_marker, target) => [thinking, { type: "text",
			text: MARKER + JSON.stringify({ ...response(target), task_id: randomUUID() }) }], {}],
		["thinking_error", "provider_error", marker => [thinking, { type: "text", text: marker }],
			{ stopReason: "error", errorMessage: "CW10_PRIVATE_ERROR_SENTINEL" }],
		["thinking_abort", "provider_error", marker => [thinking, { type: "text", text: marker }],
			{ stopReason: "aborted" }],
		["thinking_extra_round", "additional_provider_round", marker => [thinking, { type: "text", text: marker }],
			{ extraRound: true }],
	];
	for (const [label, detail, build, options] of variants) {
		const f = await fixture();
		try {
			const target = delivery();
			await beginTurn(f, target);
			assistant(f, "", { content: build(MARKER + JSON.stringify(response(target)), target),
				stopReason: options.stopReason, errorMessage: options.errorMessage });
			if (options.extraRound) f.handlers.get("before_provider_request")!({ payload: {
				messages: [{ role: "user", content: [{ type: "text", text: f.sent.at(-1)! }] }],
			} });
			if (options.toolEvent) f.handlers.get("tool_call")!({ toolCallId: "independent-tool", toolName: "bash" });
			f.handlers.get("agent_end")!();
			const terminal = await f.wait(item => item.name === "worker_response_rejected"
				|| item.name === "delivery_processing_unknown");
			assert.equal(f.frames.some(item => item.name === "assistant_message_end"), false, label);
			assert.equal(f.frames.some(item => item.name === "delivery_omp_processed"
				&& item.workerResponseId), false, label);
			// A provider error/abort may end as an unknown outcome (as without thinking); never as published control.
			if (options.stopReason) assert.equal(["worker_response_rejected", "delivery_processing_unknown"]
				.includes(terminal.name), true, label);
			else assert.equal(terminal.name, "worker_response_rejected", label);
			// A tool_call event or an extra provider round rejects with its own specific reason; every other rejection is an invalid
			// assistant response with a concrete detail code.
			if (terminal.name !== "worker_response_rejected") { /* unknown outcome: nothing published */ }
			else if (options.toolEvent) assert.equal(terminal.reason, "tool_activity", label);
			else if (options.extraRound) assert.equal(terminal.reason, "additional_provider_round", label);
			else {
				assert.equal(terminal.reason, "invalid_assistant_response", label);
				assert.equal(terminal.detail, detail, label);
			}
			const serialized = JSON.stringify({ terminal, remaining: f.frames });
			assert.equal(serialized.includes("CW10_PRIVATE_THINKING_SENTINEL"), false, label);
			assert.equal(serialized.includes("CW10_PRIVATE_ERROR_SENTINEL"), false, label);
		} finally { await f.close(); }
	}
});

// Root adjudication p27-cw18-response-id: the bridge, not the model, supplies response_id.
test("published control carries a bridge-generated canonical UUID v4 response_id, distinct per response", async () => {
	const ids = new Set<string>();
	for (let turn = 0; turn < 6; turn += 1) {
		const f = await fixture();
		try {
			const target = delivery();
			const expected = response(target);
			await beginTurn(f, target);
			assistant(f, MARKER + JSON.stringify(expected));
			f.handlers.get("agent_end")!();
			const observed = await f.wait(item => item.name === "assistant_message_end");
			const processed = await f.wait(item => item.name === "delivery_omp_processed");
			const generated = assertPublished(observed.workerResponse, expected, `turn ${turn}`);
			assert.equal(processed.workerResponseId, generated);
			// The id is neither derived from nor equal to any delivered identity.
			for (const identity of [target.taskId, target.revisionId, target.runId, target.messageId,
				target.deliveryAttemptId, SESSION]) assert.notEqual(generated, identity);
			ids.add(generated);
		} finally { await f.close(); }
	}
	assert.equal(ids.size, 6);
	// Two responses of the same bridge instance (execute then analysis) are distinct as well.
	const f = await fixture();
	try {
		const seen: string[] = [];
		for (const stage of ["execute", "analysis"]) {
			const target = delivery();
			if (stage === "analysis") { target.event.messageKind = "question"; target.event.payload.stage = "analysis"; }
			await beginTurn(f, target);
			const contract = JSON.parse(f.sent.at(-1)!).response_contract;
			assistant(f, frameFromDeliveredContract(contract, stage === "execute" ? "execute" : "success"));
			f.handlers.get("agent_end")!();
			const observed = await f.wait(item => item.name === "assistant_message_end");
			const processed = await f.wait(item => item.name === "delivery_omp_processed");
			assert.match(observed.workerResponse.response_id, UUID_V4, stage);
			assert.equal(processed.workerResponseId, observed.workerResponse.response_id, stage);
			seen.push(observed.workerResponse.response_id);
		}
		assert.notEqual(seen[0], seen[1]);
	} finally { await f.close(); }
});

test("one model-supplied response_id is ignored, never trusted; the published id is bridge-generated", async () => {
	const supplied: Array<[string, unknown]> = [
		["valid_uuid", randomUUID()], ["not_uuid", "not-a-uuid"], ["short_group", "12345678-1234-4234-8234-12345678901"],
		["uppercase", randomUUID().toUpperCase()], ["number", 7], ["empty", ""],
	];
	for (const position of ["first", "middle", "last"] as const) {
		for (const [label, value] of supplied) {
			const f = await fixture();
			try {
				const target = delivery();
				const expected = response(target);
				await beginTurn(f, target);
				assistant(f, withModelResponseId(expected, value, position));
				f.handlers.get("agent_end")!();
				const observed = await f.wait(item => item.name === "assistant_message_end");
				const processed = await f.wait(item => item.name === "delivery_omp_processed");
				const generated = assertPublished(observed.workerResponse, expected, `${position}/${label}`);
				assert.notEqual(generated, value, `${position}/${label}`);
				assert.equal(processed.workerResponseId, generated, `${position}/${label}`);
				assert.equal(f.frames.some(item => item.name === "worker_response_rejected"), false, `${position}/${label}`);
			} finally { await f.close(); }
		}
	}
});

test("two model-supplied response_id values are bad_marker; identity mismatch stays exact-match", async () => {
	const cases: Array<[string, (target: Frame) => string, string]> = [
		["two_valid", target => MARKER + JSON.stringify(response(target)).slice(0, -1)
			+ `,"response_id":"${randomUUID()}","response_id":"${randomUUID()}"}`, "bad_marker"],
		["two_invalid", target => MARKER + JSON.stringify(response(target)).slice(0, -1)
			+ ',"response_id":"x","response_id":"y"}', "bad_marker"],
		["identity_mismatch_with_response_id", target => withModelResponseId(
			{ ...response(target), task_id: randomUUID() }, randomUUID(), "middle"), "identity_mismatch"],
		["bad_decision_with_response_id", target => withModelResponseId(
			{ ...response(target), decision: "success" }, randomUUID(), "last"), "bad_marker"],
	];
	for (const [label, build, detail] of cases) {
		const f = await fixture();
		try {
			const target = delivery();
			await beginTurn(f, target);
			assistant(f, build(target));
			f.handlers.get("agent_end")!();
			const rejected = await f.wait(item => item.name === "worker_response_rejected");
			const processed = await f.wait(item => item.name === "delivery_omp_processed");
			assert.equal(rejected.reason, "invalid_assistant_response", label);
			assert.equal(rejected.detail, detail, label);
			assert.equal(processed.workerResponseId, undefined, label);
			assert.equal(f.frames.some(item => item.name === "assistant_message_end"), false, label);
		} finally { await f.close(); }
	}
});
