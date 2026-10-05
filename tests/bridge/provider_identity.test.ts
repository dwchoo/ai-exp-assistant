// CW-18 smoke-02 E1: the bridge must recognise the active delivery in every provider payload shape OMP 18.4.5
// builds, from fixtures captured with a local capture server (tests/bridge/fixtures/omp18_provider_payloads.json):
//   - openai-completions: payload.messages[-1] = {role: "user", content: [{type: "text", text}]}
//   - openai-codex (Responses) over SSE: payload.input[] (full history), last item
//     {role: "user", content: [{type: "input_text", text}]}
//   - openai-codex over the Codex WebSocket: payload.type "response.create" with previous_response_id; input[] is the
//     delta and holds only the new user item.
// The codex provider never emits after_provider_response, and a reasoning model's assistant message starts with an
// empty thinking block (the provider's encrypted reasoning). The identity rules stay exact: only the final user item,
// only this delivery's ids; history, older deliveries and other item kinds never match.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import workbenchG3Extension from "../../omp_bridge/g3/bridge.ts";

type Frame = Record<string, any>;
const SESSION_ID = "30000000-0000-4000-8000-000000000003";
const FIXTURES = JSON.parse(readFileSync(new URL("./fixtures/omp18_provider_payloads.json", import.meta.url), "utf8"));
const CODEX_SHAPES = ["openai_codex_responses_sse_full_input", "openai_codex_responses_websocket_delta_input"];
const ALL_SHAPES = ["openai_completions_messages", ...CODEX_SHAPES];

async function startBridge(role: "manager" | "worker" = "worker") {
	const directory = await mkdtemp(join(tmpdir(), "cw18-e1-identity-"));
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
	const names = ["WORKBENCH_G3_BRIDGE_SOCKET", "WORKBENCH_G3_ROLE", "WORKBENCH_G3_TOKEN", "WORKBENCH_G3_GENERATION",
		"WORKBENCH_G3_EVENT_SURFACE_PROBE"];
	const previous = new Map(names.map(name => [name, process.env[name]]));
	process.env.WORKBENCH_G3_BRIDGE_SOCKET = socketPath;
	process.env.WORKBENCH_G3_ROLE = role;
	process.env.WORKBENCH_G3_TOKEN = randomUUID();
	process.env.WORKBENCH_G3_GENERATION = "1";
	delete process.env.WORKBENCH_G3_EVENT_SURFACE_PROBE;
	const handlers = new Map<string, (...args: any[]) => any>();
	const sent: string[] = [];
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerTool() {},
		async sendUserMessage(message: string) { sent.push(message); },
		logger: { error() {} },
	};
	workbenchG3Extension(pi);
	handlers.get("session_start")!(undefined, {
		sessionManager: { getSessionId: () => SESSION_ID }, isIdle: () => true, hasPendingMessages: () => false,
		ui: { getEditorText: () => "" }, abort: () => {},
	});
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
	async function request(frame: Frame): Promise<Frame> {
		const requestId = randomUUID();
		sockets.at(-1)!.write(JSON.stringify({ ...frame, requestId }) + "\n");
		return waitFor(item => item.kind === "api_ack" && item.requestId === requestId);
	}
	const event = (name: string) => (item: Frame) => item.kind === "omp_event" && item.name === name;
	async function terminal(): Promise<Frame> {
		return waitFor(item => item.kind === "omp_event"
			&& (item.name === "delivery_omp_processed" || item.name === "delivery_processing_unknown"));
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
	return { frames, sent, handlers, waitFor, request, event, terminal, close };
}

function envelope(role: "manager" | "worker", stage = true): Frame {
	const worker = role === "worker";
	return {
		schemaVersion: 1, messageId: randomUUID(), deliveryAttemptId: randomUUID(),
		senderRole: worker ? "manager" : "worker", sessionId: SESSION_ID, sessionGeneration: 1,
		taskId: randomUUID(), revisionId: randomUUID(), runId: randomUUID(),
		event: { type: "message", messageKind: worker ? "task" : "report",
			payload: worker && stage ? { stage: "execute", revision: 1 } : { text: "fixture" } },
	};
}

function workerFrame(target: Frame, overrides: Frame = {}): string {
	return "WB_WORKER_RESPONSE:" + JSON.stringify({
		stage: "execute", kind: "task", task_id: target.taskId, revision_id: target.revisionId, revision: 1,
		run_id: target.runId, message_id: target.messageId, delivery_attempt_id: target.deliveryAttemptId,
		session_id: SESSION_ID, session_generation: 1, response_id: randomUUID(), decision: "execute", ...overrides,
	});
}

/** The captured provider request with the placeholders replaced by exact envelope texts. */
function payload(shape: string, current: string, prior: string): Frame {
	const text = JSON.stringify(FIXTURES.provider_requests[shape]);
	const swap = (value: string) => JSON.stringify(value).slice(1, -1);
	return JSON.parse(text.replaceAll("{{CURRENT_DELIVERY}}", swap(current)).replaceAll("{{PRIOR_DELIVERY}}", swap(prior)));
}

function priorDelivery(role: "manager" | "worker"): string {
	const old = envelope(role);
	return JSON.stringify({ workbench_message_id: old.messageId, workbench_delivery_attempt_id: old.deliveryAttemptId,
		kind: old.event.messageKind, task_id: old.taskId, revision_id: old.revisionId, run_id: old.runId,
		session_id: SESSION_ID, session_generation: 1, payload: old.event.payload });
}

function assistantMessage(shape: string, text: string): Frame {
	const captured = FIXTURES.assistant_message_end[shape.startsWith("openai_codex")
		? "openai_codex_responses_reasoning" : "openai_completions"];
	return JSON.parse(JSON.stringify(captured).replaceAll("{{ASSISTANT_TEXT}}", JSON.stringify(text).slice(1, -1)));
}

function emitsResponseHook(shape: string): boolean {
	return FIXTURES.after_provider_response_emitted[shape.startsWith("openai_codex_responses_sse")
		? "openai_codex_responses_sse" : shape.startsWith("openai_codex") ? "openai_codex_responses_websocket"
			: "openai_completions"] === true;
}

test("fixtures are the captured OMP 18.4.5 shapes and contain no credentials", () => {
	const text = JSON.stringify(FIXTURES);
	assert.equal(/eyJ[A-Za-z0-9_-]{8,}|Bearer |sk-[A-Za-z0-9]{8,}|acct-/.test(text), false);
	const sse = FIXTURES.provider_requests.openai_codex_responses_sse_full_input;
	assert.equal("messages" in sse, false);
	assert.deepEqual(sse.input.at(-1), { role: "user", content: [{ type: "input_text", text: "{{CURRENT_DELIVERY}}" }] });
	assert.equal(sse.input.some((item: Frame) => item.content?.[0]?.text === "{{PRIOR_DELIVERY}}"), true);
	const ws = FIXTURES.provider_requests.openai_codex_responses_websocket_delta_input;
	assert.equal(ws.type, "response.create");
	assert.equal(typeof ws.previous_response_id, "string");
	assert.deepEqual(ws.input, [{ role: "user", content: [{ type: "input_text", text: "{{CURRENT_DELIVERY}}" }] }]);
	const chat = FIXTURES.provider_requests.openai_completions_messages;
	assert.deepEqual(chat.messages.at(-1), { role: "user", content: [{ type: "text", text: "{{CURRENT_DELIVERY}}" }] });
	assert.deepEqual(FIXTURES.after_provider_response_emitted,
		{ openai_completions: true, openai_codex_responses_sse: false, openai_codex_responses_websocket: false });
	const reasoning = FIXTURES.assistant_message_end.openai_codex_responses_reasoning.content;
	assert.deepEqual(reasoning.map((block: Frame) => block.type), ["thinking", "text"]);
	assert.equal(reasoning[0].thinking, "");
});

test("a worker execute delivery is processed with its response in every captured payload shape", async () => {
	for (const shape of ALL_SHAPES) {
		const bridge = await startBridge("worker");
		try {
			const target = envelope("worker");
			assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(target) })).status, "api_accepted", shape);
			bridge.handlers.get("before_provider_request")!({ type: "before_provider_request",
				payload: payload(shape, bridge.sent.at(-1)!, priorDelivery("worker")) });
			if (emitsResponseHook(shape)) bridge.handlers.get("after_provider_response")!({ status: 200 });
			const frame = workerFrame(target);
			bridge.handlers.get("message_end")!({ message: assistantMessage(shape, frame), willContinue: false });
			bridge.handlers.get("agent_end")!();
			const processed = await bridge.terminal();
			assert.equal(processed.name, "delivery_omp_processed", `${shape}: ${processed.reason}`);
			assert.equal(processed.messageId, target.messageId, shape);
			assert.equal(processed.deliveryAttemptId, target.deliveryAttemptId, shape);
			const answer = await bridge.waitFor(bridge.event("assistant_message_end"));
			assert.equal(answer.workerResponse.decision, "execute", shape);
			assert.equal(processed.workerResponseId, answer.workerResponse.response_id, shape);
			assert.equal(JSON.stringify(bridge.frames).includes("WB_WORKER_RESPONSE"), false, shape);
		} finally { await bridge.close(); }
	}
});

test("a manager report delivery is processed in every captured payload shape", async () => {
	for (const shape of ALL_SHAPES) {
		const bridge = await startBridge("manager");
		try {
			const target = envelope("manager");
			assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(target) })).status, "api_accepted", shape);
			bridge.handlers.get("before_provider_request")!({ payload: payload(shape, bridge.sent.at(-1)!, priorDelivery("manager")) });
			if (emitsResponseHook(shape)) bridge.handlers.get("after_provider_response")!({ status: 200 });
			bridge.handlers.get("message_end")!({ message: assistantMessage(shape, "summary for the user"), willContinue: false });
			bridge.handlers.get("agent_end")!();
			const processed = await bridge.terminal();
			assert.equal(processed.name, "delivery_omp_processed", `${shape}: ${processed.reason}`);
			assert.equal(processed.messageId, target.messageId, shape);
		} finally { await bridge.close(); }
	}
});

test("Responses identity stays exact: history, other attempts, other roles and item kinds never match", async () => {
	const sse = "openai_codex_responses_sse_full_input";
	const cases: Array<[string, (current: string, prior: string) => Frame]> = [
		["current only in history, an older delivery last", (current, prior) => payload(sse, prior, current)],
		["no delivery (first prompt)", () => JSON.parse(JSON.stringify(
			FIXTURES.provider_requests.openai_codex_responses_websocket_first_prompt))],
		["other delivery attempt", (current, prior) => payload(sse, JSON.stringify(
			{ ...JSON.parse(current), workbench_delivery_attempt_id: randomUUID() }), prior)],
		["other run id", (current, prior) => payload(sse, JSON.stringify({ ...JSON.parse(current), run_id: randomUUID() }), prior)],
		["assistant item after the delivery", (current, prior) => {
			const value = payload(sse, current, prior);
			value.input.push({ type: "message", role: "assistant", content: [{ type: "output_text", text: current }] });
			return value;
		}],
		["delivery in a developer item", (current) => ({ input: [{ type: "message", role: "developer",
			content: [{ type: "input_text", text: current }] }] })],
		["delivery as output_text of a user item", (current) => ({ input: [{ role: "user",
			content: [{ type: "output_text", text: current }] }] })],
		["delivery in a function_call_output item", (current) => ({ input: [{ type: "function_call_output",
			call_id: "c1", output: current }] })],
		["user item of another item type", (current) => ({ input: [{ type: "item_reference", role: "user",
			content: [{ type: "input_text", text: current }] }] })],
		["messages and input both present", (current) => ({ messages: [{ role: "user", content: current }],
			input: [{ role: "user", content: [{ type: "input_text", text: current }] }] })],
		["input is not a list", (current) => ({ input: current })],
	];
	for (const [label, make] of cases) {
		const bridge = await startBridge("worker");
		try {
			const target = envelope("worker");
			assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(target) })).status, "api_accepted", label);
			bridge.handlers.get("before_provider_request")!({ payload: make(bridge.sent.at(-1)!, priorDelivery("worker")) });
			const unknown = await bridge.terminal();
			assert.equal(unknown.name, "delivery_processing_unknown", label);
			assert.equal(unknown.reason, "provider_request_identity_not_observed", label);
			assert.equal(unknown.messageId, target.messageId, label);
		} finally { await bridge.close(); }
	}
});

test("without the response hook only a clean assistant message for the matched request is response evidence", async () => {
	const shape = "openai_codex_responses_websocket_delta_input";
	for (const variant of ["error", "aborted", "error_message", "no_assistant_message"]) {
		const bridge = await startBridge("manager");
		try {
			const target = envelope("manager");
			assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(target) })).status, "api_accepted");
			bridge.handlers.get("before_provider_request")!({ payload: payload(shape, bridge.sent.at(-1)!, priorDelivery("manager")) });
			if (variant !== "no_assistant_message") {
				const message = assistantMessage(shape, "partial");
				if (variant === "error" || variant === "aborted") message.stopReason = variant;
				if (variant === "error_message") message.errorMessage = "provider failed";
				bridge.handlers.get("message_end")!({ message, willContinue: false });
			}
			bridge.handlers.get("agent_end")!();
			const unknown = await bridge.terminal();
			assert.equal(unknown.name, "delivery_processing_unknown", variant);
			assert.equal(unknown.providerResponseObserved, false, variant);
		} finally { await bridge.close(); }
	}
	// A user message (e.g. the injected delivery itself) is not a provider response.
	const bridge = await startBridge("manager");
	try {
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(envelope("manager")) })).status, "api_accepted");
		bridge.handlers.get("before_provider_request")!({ payload: payload(shape, bridge.sent.at(-1)!, priorDelivery("manager")) });
		bridge.handlers.get("message_end")!({ message: { role: "user", content: [{ type: "text", text: bridge.sent.at(-1)! }] } });
		bridge.handlers.get("agent_end")!();
		const unknown = await bridge.terminal();
		assert.equal(unknown.name, "delivery_processing_unknown");
		assert.equal(unknown.reason, "agent_ended_without_complete_provider_evidence");
	} finally { await bridge.close(); }
});

test("worker response: only an empty leading thinking block (encrypted reasoning) is ignored", async () => {
	const shape = "openai_codex_responses_websocket_delta_input";
	const signature = FIXTURES.assistant_message_end.openai_codex_responses_reasoning.content[0].thinkingSignature;
	const variants: Array<[string, (text: string) => Frame[], boolean]> = [
		["captured codex shape", text => [{ type: "thinking", thinking: "", thinkingSignature: signature }, { type: "text", text }], true],
		["two empty thinking blocks first", text => [{ type: "thinking", thinking: "" }, { type: "thinking", thinking: "" },
			{ type: "text", text }], true],
		["visible thinking text", text => [{ type: "thinking", thinking: "I will answer", thinkingSignature: signature },
			{ type: "text", text }], false],
		["thinking after the text", text => [{ type: "text", text }, { type: "thinking", thinking: "" }], false],
		["thinking with another text field", text => [{ type: "thinking", thinking: "", text: "hidden" }, { type: "text", text }], false],
		["thinking only", () => [{ type: "thinking", thinking: "" }], false],
		["two text blocks", text => [{ type: "thinking", thinking: "" }, { type: "text", text }, { type: "text", text: "x" }], false],
		["redacted thinking", text => [{ type: "redactedThinking", data: "x" }, { type: "text", text }], false],
	];
	for (const [label, content, accepted] of variants) {
		const bridge = await startBridge("worker");
		try {
			const target = envelope("worker");
			assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(target) })).status, "api_accepted");
			bridge.handlers.get("before_provider_request")!({ payload: payload(shape, bridge.sent.at(-1)!, priorDelivery("worker")) });
			bridge.handlers.get("message_end")!({ message: { role: "assistant", stopReason: "stop",
				content: content(workerFrame(target)) }, willContinue: false });
			bridge.handlers.get("agent_end")!();
			const processed = await bridge.terminal();
			assert.equal(processed.name, "delivery_omp_processed", label);
			if (accepted) {
				assert.equal(typeof processed.workerResponseId, "string", label);
				await bridge.waitFor(bridge.event("assistant_message_end"));
			} else {
				assert.equal(processed.workerResponseId, undefined, label);
				const rejected = await bridge.waitFor(bridge.event("worker_response_rejected"));
				assert.equal(rejected.reason, "invalid_assistant_response", label);
				assert.equal(JSON.stringify(rejected).includes("I will answer"), false, label);
			}
		} finally { await bridge.close(); }
	}
});
