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
const SESSION_ID = "30000000-0000-4000-8000-000000000001";

async function startBridge(role: "manager" | "worker" = "worker", eventSurfaceProbe = false, handlerFaultProbe = false) {
	const directory = await mkdtemp(join(tmpdir(), "cw04-g3-test-"));
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
	await new Promise<void>((resolve, reject) => {
		server.once("error", reject);
		server.listen(socketPath, resolve);
	});

	const names = ["WORKBENCH_G3_BRIDGE_SOCKET", "WORKBENCH_G3_ROLE", "WORKBENCH_G3_TOKEN", "WORKBENCH_G3_GENERATION", "WORKBENCH_G3_EVENT_SURFACE_PROBE", "WORKBENCH_G3_HANDLER_FAULT_PROBE"];
	const previous = new Map(names.map(name => [name, process.env[name]]));
	process.env.WORKBENCH_G3_BRIDGE_SOCKET = socketPath;
	process.env.WORKBENCH_G3_ROLE = role;
	process.env.WORKBENCH_G3_TOKEN = randomUUID();
	process.env.WORKBENCH_G3_GENERATION = "1";
	if (eventSurfaceProbe) process.env.WORKBENCH_G3_EVENT_SURFACE_PROBE = "1";
	else delete process.env.WORKBENCH_G3_EVENT_SURFACE_PROBE;
	if (handlerFaultProbe) process.env.WORKBENCH_G3_HANDLER_FAULT_PROBE = "1";
	else delete process.env.WORKBENCH_G3_HANDLER_FAULT_PROBE;

	const handlers = new Map<string, (...args: any[]) => any>();
	const registeredTools = new Map<string, Frame>();
	const sent: Array<{ message: string; options: Frame }> = [];
	const state = { idle: true, pending: false, editor: "" as string | undefined };
	let failAfterEnqueue = false;
	let holdSend = false;
	let releaseSend: (() => void) | undefined;
	let startedTurns = 0;
	let abortCalls = 0;
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerTool(tool: Frame) { registeredTools.set(tool.name, tool); },
		zod: { object: (shape: Frame) => shape, string: () => "string" },
		async sendUserMessage(message: string, options: Frame) {
			sent.push({ message, options });
			// OMP v18.2.10: omitted deliverAs starts a normal prompt when idle.
			if (state.idle && !("deliverAs" in options)) startedTurns += 1;
			if (holdSend) await new Promise<void>(resolve => { releaseSend = resolve; });
			if (failAfterEnqueue) throw new Error("simulated ACK uncertainty after enqueue");
		},
		logger: { error() {} },
	};
	const context = {
		sessionManager: { getSessionId: () => SESSION_ID },
		isIdle: () => state.idle,
		hasPendingMessages: () => state.pending,
		ui: { getEditorText: () => state.editor },
		abort: () => { abortCalls += 1; },
	};
	workbenchG3Extension(pi);
	handlers.get("session_start")!(undefined, context);

	async function waitFor(predicate: (frame: Frame) => boolean, timeoutMs = 1200): Promise<Frame> {
		const deadline = Date.now() + timeoutMs;
		while (Date.now() < deadline) {
			const index = frames.findIndex(predicate);
			if (index >= 0) return frames.splice(index, 1)[0];
			await delay(10);
		}
		throw new Error("bridge frame timed out");
	}

	await waitFor(frame => frame.kind === "hello");
	async function request(frame: Frame): Promise<Frame> {
		const requestId = randomUUID();
		sockets.at(-1)!.write(JSON.stringify({ ...frame, requestId }) + "\n");
		return waitFor(item => item.kind === "api_ack" && item.requestId === requestId);
	}
	async function close(): Promise<void> {
		releaseSend?.();
		handlers.get("session_shutdown")?.();
		for (const socket of sockets) socket.destroy();
		await new Promise<void>(resolve => server.close(() => resolve()));
		for (const name of names) {
			const value = previous.get(name);
			if (value === undefined) delete process.env[name];
			else process.env[name] = value;
		}
		await rm(directory, { recursive: true, force: true });
	}
	return {
		frames, sent, state, sockets, handlers, registeredTools, waitFor, request, close,
		get startedTurns() { return startedTurns; },
		get abortCalls() { return abortCalls; },
		setFailAfterEnqueue(value: boolean) { failAfterEnqueue = value; },
		setHoldSend(value: boolean) { holdSend = value; },
		releaseSend() { releaseSend?.(); releaseSend = undefined; },
	};
}

function envelope(overrides: Frame = {}): Frame {
	return {
		schemaVersion: 1,
		messageId: randomUUID(),
		deliveryAttemptId: randomUUID(),
		senderRole: "manager",
		sessionId: SESSION_ID,
		sessionGeneration: 1,
		taskId: randomUUID(),
		revisionId: randomUUID(),
		runId: randomUUID(),
		event: { type: "message", messageKind: "task", payload: { text: "fixture" } },
		...overrides,
	};
}

function workerResponse(target: Frame, overrides: Frame = {}): Frame {
	return {
		stage: "execute", kind: "task", task_id: target.taskId, revision_id: target.revisionId,
		revision: 1, run_id: target.runId, message_id: target.messageId,
		delivery_attempt_id: target.deliveryAttemptId, session_id: SESSION_ID,
		session_generation: 1, response_id: randomUUID(), decision: "execute", ...overrides,
	};
}

async function driveWorkerTurn(bridge: Awaited<ReturnType<typeof startBridge>>, target: Frame,
	text: string, options: Frame = {}): Promise<Frame> {
	assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(target) })).status, "api_accepted");
	bridge.handlers.get("before_provider_request")!({ payload: { messages: [
		{ role: "user", content: [{ type: "text", text: bridge.sent.at(-1)!.message }] },
	] } });
	bridge.handlers.get("after_provider_response")!();
	bridge.handlers.get("message_end")!({ message: {
		role: "assistant", content: [{ type: "text", text }], stopReason: options.stopReason ?? "stop",
		...(options.errorMessage ? { errorMessage: options.errorMessage } : {}),
	}, willContinue: options.willContinue ?? false });
	if (options.extraMessage) bridge.handlers.get("message_end")!({ message: {
		role: "assistant", content: [{ type: "text", text }], stopReason: "stop",
	}, willContinue: false });
	if (options.extraRound) bridge.handlers.get("before_provider_request")!({ payload: { messages: [
		{ role: "user", content: [{ type: "text", text: bridge.sent.at(-1)!.message }] },
	] } });
	if (options.tool) bridge.handlers.get("tool_call")!({ toolCallId: "tool", toolName: "bash" });
	bridge.handlers.get("agent_end")!();
	return bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "delivery_omp_processed");
}

test("worker response is public only after terminal agent_end and linked to exact delivery", async () => {
	const bridge = await startBridge();
	try {
		const target = envelope({ event: { type: "message", messageKind: "task", payload: { stage: "execute", revision: 1 } } });
		const response = workerResponse(target);
		const text = "WB_WORKER_RESPONSE:" + JSON.stringify(response);
		const processed = await driveWorkerTurn(bridge, target, text);
		const assistant = await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "assistant_message_end");
		assert.deepEqual(assistant.workerResponse, response);
		assert.equal(assistant.messageId, target.messageId);
		assert.equal(processed.workerResponseId, response.response_id);
		assert.equal(JSON.stringify({ assistant, processed }).includes(text), false, "raw model text must not cross bridge");
	} finally { await bridge.close(); }
});

test("execute and analysis injections specify the exact self-consistent response contract", async () => {
	for (const [stage, kind, revision, decisions] of [
		["execute", "task", 1, ["execute", "hold"]],
		["analysis", "question", 2, ["success", "failure", "indeterminate"]],
	] as const) {
		const bridge = await startBridge();
		try {
			const target = envelope({ event: { type: "message", messageKind: kind, payload: { stage, revision } } });
			assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(target) })).status, "api_accepted");
			const injected = JSON.parse(bridge.sent.at(-1)!.message);
			const contract = injected.response_contract;
			assert.equal(contract.marker, "WB_WORKER_RESPONSE:");
			assert.equal(contract.format, "marker_plus_compact_flat_json");
			assert.deepEqual(contract.field_order, ["stage", "kind", "task_id", "revision_id", "revision", "run_id",
				"message_id", "delivery_attempt_id", "session_id", "session_generation", "response_id", "decision"]);
			assert.deepEqual(contract.fields.map((field: Frame) => field.name), contract.field_order);
			const fields = Object.fromEntries(contract.fields.map((field: Frame) => [field.name, field]));
			assert.equal(fields.stage.value, stage);
			assert.equal(fields.kind.value, kind);
			assert.equal(fields.revision.value, revision);
			for (const [name, value] of Object.entries({
				task_id: target.taskId, revision_id: target.revisionId, run_id: target.runId,
				message_id: target.messageId, delivery_attempt_id: target.deliveryAttemptId,
				session_id: SESSION_ID,
			})) assert.deepEqual(fields[name], { name, type: "canonical_uuid", value });
			assert.deepEqual(fields.session_generation, { name: "session_generation", type: "positive_safe_integer", value: 1 });
			assert.deepEqual(fields.response_id, { name: "response_id", type: "canonical_uuid", generate: "canonical_uuid" });
			assert.deepEqual(fields.decision, { name: "decision", type: "enum_string", allowed: decisions });
			assert.deepEqual(contract.output_rules, { exactly_one_frame: true, no_prose: true,
				no_tools: true, no_thinking: true, no_markdown: true, no_extra_content: true });
			assert.match(contract.instruction, /only the marker.*no prose, tools, thinking/);
			assert.equal(JSON.stringify(contract).includes("CW10_SECRET"), false);
		} finally { await bridge.close(); }
	}
	for (const [role, target] of [
		["worker", envelope()],
		["manager", envelope({ senderRole: "worker", event: { type: "message", messageKind: "report", payload: { stage: "analysis", revision: 1 } } })],
	] as const) {
		const bridge = await startBridge(role);
		try {
			assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(target) })).status, "api_accepted");
			assert.equal(Object.hasOwn(JSON.parse(bridge.sent.at(-1)!.message), "response_contract"), false);
		} finally { await bridge.close(); }
	}
});

test("worker response rejects malformed, ambiguous, mismatched and nonterminal assistant output", async () => {
	for (const variant of ["prose", "suffix", "duplicate_key", "unknown_key", "wrong_identity", "wrong_order",
		"wrong_stage", "wrong_decision", "unsafe_revision", "multiple", "length", "continuation",
		"extra_message", "extra_round", "tool"]) {
		const bridge = await startBridge();
		try {
			const target = envelope({ event: { type: "message", messageKind: "task", payload: { stage: "execute", revision: 1 } } });
			const response = workerResponse(target, variant === "wrong_identity" ? { message_id: randomUUID() }
				: variant === "wrong_stage" ? { stage: "analysis" }
				: variant === "wrong_decision" ? { decision: "success" }
				: variant === "unsafe_revision" ? { revision: Number.MAX_SAFE_INTEGER + 1 } : {});
			let text = "WB_WORKER_RESPONSE:" + JSON.stringify(response);
			if (variant === "prose") text = "intro " + text;
			if (variant === "suffix") text += " ending";
			if (variant === "duplicate_key") text = text.slice(0, -1) + ',"decision":"execute"}';
			if (variant === "unknown_key") text = text.slice(0, -1) + ',"extra":"x"}';
			if (variant === "multiple") text += text;
			if (variant === "wrong_order") {
				const reordered = { kind: response.kind, stage: response.stage, ...response };
				delete reordered.stage;
				reordered.stage = response.stage;
				text = "WB_WORKER_RESPONSE:" + JSON.stringify(reordered);
			}
			const processed = await driveWorkerTurn(bridge, target, text, {
				stopReason: variant === "length" ? "length" : "stop",
				willContinue: variant === "continuation", extraMessage: variant === "extra_message",
				extraRound: variant === "extra_round", tool: variant === "tool",
			});
			const rejected = await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "worker_response_rejected");
			assert.equal(typeof rejected.reason, "string", variant);
			assert.equal(processed.workerResponseId, undefined, variant);
			assert.equal(bridge.frames.some(frame => frame.name === "assistant_message_end"), false, variant);
			assert.equal(JSON.stringify({ rejected, processed }).includes(text), false, variant);
		} finally { await bridge.close(); }
	}
});

test("worker response remains untrusted across pause and session switch", async () => {
	for (const interruption of ["pause", "switch"]) {
		const bridge = await startBridge();
		try {
			const target = envelope({ event: { type: "message", messageKind: "task", payload: { stage: "execute", revision: 1 } } });
			assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(target) })).status, "api_accepted");
			bridge.handlers.get("before_provider_request")!({ payload: { messages: [
				{ role: "user", content: [{ type: "text", text: bridge.sent.at(-1)!.message }] },
			] } });
			bridge.handlers.get("after_provider_response")!();
			bridge.handlers.get("message_end")!({ message: { role: "assistant", stopReason: "stop",
				content: [{ type: "text", text: "WB_WORKER_RESPONSE:" + JSON.stringify(workerResponse(target)) }] },
				willContinue: false });
			if (interruption === "pause") {
				assert.equal((await bridge.request({ kind: "pause" })).status, "paused");
				bridge.handlers.get("agent_end")!();
				const rejected = await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "worker_response_rejected");
				assert.equal(rejected.reason, "automation_paused");
				const processed = await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "delivery_omp_processed");
				assert.equal(processed.workerResponseId, undefined);
			} else {
				bridge.handlers.get("session_switch")!(undefined, {
					sessionManager: { getSessionId: () => randomUUID() }, isIdle: () => true,
					hasPendingMessages: () => false, ui: { getEditorText: () => "" }, abort: () => {},
				});
				const unknown = await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "delivery_processing_unknown");
				assert.equal(unknown.reason, "session_switched");
			}
			assert.equal(bridge.frames.some(frame => frame.name === "assistant_message_end"), false);
		} finally { await bridge.close(); }
	}
});

test("internal handler fault is opt-in, one-shot, ACK-free and permanently no-replay", async () => {
	const disabled = await startBridge();
	try {
		assert.equal((await disabled.request({ kind: "deliver", envelope: JSON.stringify(envelope()),
			diagnosticFault: "handler_exception" })).status, "api_accepted");
		assert.equal(disabled.sent.length, 1);
	} finally { await disabled.close(); }
	const bridge = await startBridge("worker", false, true);
	try {
		const failed = envelope();
		const requestId = randomUUID();
		bridge.sockets.at(-1)!.write(JSON.stringify({ kind: "deliver", requestId,
			envelope: JSON.stringify(failed), diagnosticFault: "handler_exception" }) + "\n");
		const event = await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === `diagnostic_handler_fault:${requestId}`);
		assert.equal(event.requestId, requestId);
		assert.equal(event.messageId, failed.messageId);
		assert.equal((await bridge.request({ kind: "probe" })).status, "state");
		assert.equal(bridge.frames.some(frame => frame.kind === "api_ack" && frame.requestId === requestId), false);
		assert.equal(bridge.sent.length, 0);
		assert.equal(bridge.startedTurns, 0);
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(failed) })).status, "unknown_no_replay");
		bridge.sockets.at(-1)!.destroy();
		await bridge.waitFor(frame => frame.kind === "hello");
		assert.equal(bridge.sent.length, 0);
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(failed) })).status, "unknown_no_replay");
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(envelope()),
			diagnosticFault: "handler_exception" })).status, "api_accepted");
		assert.equal(bridge.sent.length, 1);
	} finally { await bridge.close(); }
});

test("extension binds role/session and defers busy, pending, approval, and composer states", async () => {
	const bridge = await startBridge();
	try {
		for (const invalid of [
			envelope({ senderRole: "worker" }),
			envelope({ sessionGeneration: 2 }),
			envelope({ sessionId: randomUUID() }),
		]) {
			assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(invalid) })).status, "rejected");
		}
		assert.equal(bridge.sent.length, 0);

		const candidate = envelope();
		bridge.state.idle = false;
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(candidate) })).status, "deferred");
		bridge.state.idle = true;
		bridge.state.pending = true;
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(candidate) })).status, "deferred");
		bridge.state.pending = false;
		bridge.state.editor = "user draft";
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(candidate) })).status, "deferred");
		bridge.state.editor = undefined;
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(candidate) })).status, "deferred");
		bridge.state.editor = "";
		bridge.handlers.get("tool_approval_requested")!({ toolCallId: "A", toolName: "write" });
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(candidate) })).status, "deferred");
		bridge.handlers.get("tool_approval_resolved")!({ toolCallId: "A", toolName: "write", approved: false });
		assert.equal(bridge.sent.length, 0);
	} finally {
		await bridge.close();
	}
});

test("API acceptance stays distinct from model completion and ACK loss never replays", async () => {
	const bridge = await startBridge();
	try {
		const first = envelope();
		const accepted = await bridge.request({ kind: "deliver", envelope: JSON.stringify(first) });
		assert.equal(accepted.status, "api_accepted");
		assert.equal(accepted.modelProcessed, false);
		assert.equal(bridge.sent.length, 1);
		const retry = { ...first, deliveryAttemptId: randomUUID() };
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(retry) })).status, "duplicate_api_accepted");
		assert.equal(bridge.sent.length, 1);

		bridge.setFailAfterEnqueue(true);
		const uncertain = envelope();
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(uncertain) })).status, "unknown_no_replay");
		bridge.setFailAfterEnqueue(false);
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify({ ...uncertain, deliveryAttemptId: randomUUID() }) })).status, "unknown_no_replay");
		assert.equal(bridge.sent.length, 2);
	} finally {
		await bridge.close();
	}
});

test("idle task starts a normal prompt without composer intrusion", async () => {
	const bridge = await startBridge();
	try {
		const response = await bridge.request({ kind: "deliver", envelope: JSON.stringify(envelope()) });
		assert.equal(response.status, "api_accepted");
		assert.equal(response.modelProcessed, false);
		assert.equal(bridge.sent.length, 1);
		assert.equal("deliverAs" in bridge.sent[0].options, false);
		assert.equal(bridge.sent[0].options.attribution, "agent");
		assert.equal(bridge.startedTurns, 1);
		assert.equal(bridge.state.editor, "");
	} finally {
		await bridge.close();
	}
});

test("active manager pause requests abort before an outstanding tool finishes", async () => {
	const bridge = await startBridge("manager");
	try {
		bridge.state.idle = false;
		bridge.handlers.get("agent_start")!();
		bridge.handlers.get("tool_execution_start")!({ toolCallId: "A", toolName: "bash" });
		const ack = await bridge.request({ kind: "pause" });
		assert.equal(ack.status, "abort_requested");
		assert.equal(bridge.abortCalls, 1, "pause must call public ctx.abort without waiting for tool A");
		assert.equal(bridge.frames.some(frame => frame.kind === "omp_event" && frame.name === "turn_stop_observed"), false);
		const pending = (await bridge.request({ kind: "probe" })).state;
		assert.equal(pending.abortStatus, "requested");
		assert.deepEqual(pending.unconfirmedToolCallIds, ["A"]);
		bridge.handlers.get("agent_end")!();
		await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "turn_stop_observed");
		const ended = (await bridge.request({ kind: "probe" })).state;
		assert.equal(ended.abortStatus, "stop_observed");
		assert.deepEqual(ended.unconfirmedToolCallIds, ["A"], "turn end cannot prove the tool result or rollback");
		bridge.handlers.get("tool_execution_end")!({ toolCallId: "A", toolName: "bash" });
		const afterToolEnd = (await bridge.request({ kind: "probe" })).state;
		assert.equal(
			new Set([
				...(afterToolEnd.unconfirmedToolCallIds ?? []),
				...(afterToolEnd.unknownOutcomeToolCallIds ?? []),
			]).has("A"),
			true,
			"tool_execution_end is not evidence that the interrupted tool committed or rolled back",
		);
	} finally {
		await bridge.close();
	}
});

test("reconciled resume retains unknown tool outcome through late end and session switch", async () => {
	const bridge = await startBridge("manager");
	try {
		bridge.state.idle = false;
		bridge.handlers.get("agent_start")!();
		bridge.handlers.get("tool_execution_start")!({ toolCallId: "A", toolName: "bash" });
		assert.equal((await bridge.request({ kind: "pause" })).status, "abort_requested");
		bridge.handlers.get("agent_end")!();
		await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "turn_stop_observed");
		assert.deepEqual((await bridge.request({ kind: "probe" })).state.unknownOutcomeToolCallIds, ["A"]);

		// A trusted backend may report reconciliation and the user's resume decision.
		// Neither that signal nor tool_execution_end proves A's file effect or rollback.
		assert.equal((await bridge.request({ kind: "resume", reconciled: true })).status, "resumed");
		const afterResume = (await bridge.request({ kind: "probe" })).state.unknownOutcomeToolCallIds;
		bridge.handlers.get("tool_execution_end")!({ toolCallId: "A", toolName: "bash" });
		const afterLateToolEnd = (await bridge.request({ kind: "probe" })).state.unknownOutcomeToolCallIds;

		const nextSessionId = randomUUID();
		bridge.handlers.get("session_switch")!(undefined, {
			sessionManager: { getSessionId: () => nextSessionId },
			isIdle: () => true,
			hasPendingMessages: () => false,
			ui: { getEditorText: () => "" },
			abort: () => {},
		});
		await bridge.waitFor(frame => frame.kind === "hello" && frame.generation === 2);
		const prior = (await bridge.request({ kind: "probe" })).state.unresolvedPriorSessions;
		const priorContainsA = prior.some((item: Frame) =>
			item.sessionId === SESSION_ID
			&& item.generation === 1
			&& item.unconfirmedToolCallIds.includes("A"));
		assert.equal((await bridge.request({ kind: "pause" })).status, "paused");
		assert.equal((await bridge.request({ kind: "resume", reconciled: true })).status, "resumed");
		const afterSecondResume = (await bridge.request({ kind: "probe" })).state.unresolvedPriorSessions;
		assert.deepEqual({
			afterResume,
			afterLateToolEnd,
			priorContainsA,
			priorSurvivedResume: afterSecondResume.some((item: Frame) =>
				item.sessionId === SESSION_ID
				&& item.generation === 1
				&& item.unconfirmedToolCallIds.includes("A")),
		}, {
			afterResume: ["A"],
			afterLateToolEnd: ["A"],
			priorContainsA: true,
			priorSurvivedResume: true,
		});
	} finally {
		await bridge.close();
	}
});

test("pause before agent_start still pairs the later turn stop with its abort request", async () => {
	const bridge = await startBridge("manager");
	try {
		bridge.state.idle = false;
		assert.equal((await bridge.request({ kind: "pause" })).status, "abort_requested");
		assert.equal(bridge.abortCalls, 1);
		bridge.handlers.get("agent_start")!();
		bridge.handlers.get("agent_end")!();
		const stop = await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "turn_stop_observed");
		assert.equal(typeof stop.abortRequestId, "string");
		assert.equal((await bridge.request({ kind: "probe" })).state.abortStatus, "stop_observed");
	} finally {
		await bridge.close();
	}
});

test("resume cannot clear pause while a requested manager abort has no observed stop", async () => {
	const bridge = await startBridge("manager");
	try {
		bridge.state.idle = false;
		bridge.handlers.get("agent_start")!();
		assert.equal((await bridge.request({ kind: "pause" })).status, "abort_requested");
		const premature = await bridge.request({ kind: "resume", reconciled: true });
		assert.equal(premature.status, "abort_pending");
		assert.equal(premature.state.paused, true);
		assert.equal(premature.state.abortStatus, "requested");
		bridge.handlers.get("agent_end")!();
		await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "turn_stop_observed");
		assert.equal((await bridge.request({ kind: "resume", reconciled: true })).status, "resumed");
	} finally {
		await bridge.close();
	}
});

test("session switch retains unresolved prior-session tool and abort evidence", async () => {
	const bridge = await startBridge("manager");
	try {
		bridge.state.idle = false;
		bridge.handlers.get("agent_start")!();
		bridge.handlers.get("tool_execution_start")!({ toolCallId: "A", toolName: "bash" });
		assert.equal((await bridge.request({ kind: "pause" })).status, "abort_requested");
		const nextSessionId = randomUUID();
		bridge.handlers.get("session_switch")!(undefined, {
			sessionManager: { getSessionId: () => nextSessionId },
			isIdle: () => true,
			hasPendingMessages: () => false,
			ui: { getEditorText: () => "" },
			abort: () => {},
		});
		await bridge.waitFor(frame => frame.kind === "hello" && frame.generation === 2);
		const state = (await bridge.request({ kind: "probe" })).state;
		assert.equal(state.paused, true);
		assert.deepEqual(state.unresolvedPriorSessions, [{
			sessionId: SESSION_ID,
			generation: 1,
			abortStatus: "requested",
			unconfirmedToolCallIds: ["A"],
		}]);
	} finally {
		await bridge.close();
	}
});

test("pause during an unresolved sendUserMessage reports uncertainty and forbids replay", async () => {
	const bridge = await startBridge("worker");
	try {
		bridge.setHoldSend(true);
		const candidate = envelope();
		const delivery = bridge.request({ kind: "deliver", envelope: JSON.stringify(candidate) });
		for (let i = 0; bridge.sent.length === 0 && i < 50; i += 1) await delay(10);
		assert.equal(bridge.sent.length, 1, "public sendUserMessage was entered before pause");
		assert.equal((await bridge.request({ kind: "pause" })).status, "paused");
		bridge.releaseSend();
		// The public API may already have enqueued the turn. Its later resolution
		// cannot prove that the pause prevented that automatic model work.
		const result = await delivery;
		assert.equal(result.status, "unknown_no_replay");
		assert.notEqual(result.modelProcessed, true);
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify({
			...candidate, deliveryAttemptId: randomUUID(),
		}) })).status, "unknown_no_replay");
		assert.equal(bridge.sent.length, 1);
	} finally {
		await bridge.close();
	}
});

test("idle manager and active worker pause hold automation without aborting a turn", async () => {
	for (const role of ["manager", "worker"] as const) {
		const bridge = await startBridge(role);
		try {
			bridge.state.idle = role === "manager";
			if (role === "worker") bridge.handlers.get("agent_start")!();
			const ack = await bridge.request({ kind: "pause" });
			assert.equal(ack.status, "paused");
			assert.equal(bridge.abortCalls, 0);
			assert.equal((await bridge.request({ kind: "probe" })).state.paused, true);
			const automatic = envelope({
				senderRole: role === "worker" ? "manager" : "worker",
				event: { type: "message", messageKind: role === "worker" ? "task" : "report", payload: { text: "later" } },
			});
			bridge.state.idle = true;
			assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(automatic) })).status, "deferred");
			assert.equal(bridge.sent.length, 0);
			assert.equal(bridge.startedTurns, 0);
		} finally {
			await bridge.close();
		}
	}
});

test("pause leaves native user input and its tool path available without starting automatic work", async () => {
	const bridge = await startBridge("worker");
	try {
		assert.equal((await bridge.request({ kind: "pause" })).status, "paused");
		bridge.handlers.get("input")!({ text: "user's explicit instruction" });
		assert.equal(bridge.handlers.get("tool_call")!({ toolCallId: "manual", toolName: "write" }), undefined);
		assert.equal(bridge.sent.length, 0);
		assert.equal(bridge.startedTurns, 0);
	} finally {
		await bridge.close();
	}
});

test("resume exposes reconciliation state and never replays a paused request", async () => {
	const bridge = await startBridge("worker");
	try {
		bridge.state.idle = false;
		const beforePause = envelope();
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(beforePause) })).status, "deferred");
		bridge.state.idle = true;
		await bridge.request({ kind: "pause" });
		const duringPause = envelope();
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(duringPause) })).status, "deferred");
		const resumeCheck = await bridge.request({ kind: "resume" });
		assert.equal(resumeCheck.status, "reconciliation_required");
		assert.equal(resumeCheck.state.paused, true);
		assert.equal(bridge.sent.length, 0);
		// The trusted backend reports its separate files/processes/approval comparison.
		// The bridge only applies that explicit decision; it cannot perform the comparison.
		assert.equal((await bridge.request({ kind: "resume", reconciled: true })).status, "resumed");
		assert.equal((await bridge.request({ kind: "probe" })).state.paused, false);
		assert.equal(bridge.sent.length, 0, "explicit resume must not replay the earlier deferred envelope");
		for (const stale of [beforePause, duringPause]) {
			assert.equal(
				(await bridge.request({ kind: "deliver", envelope: JSON.stringify({ ...stale, deliveryAttemptId: randomUUID() }) })).status,
				"unknown_no_replay",
			);
		}
		assert.equal(bridge.sent.length, 0);
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(envelope()) })).status, "api_accepted");
		assert.equal(bridge.sent.length, 1);
	} finally {
		await bridge.close();
	}
});

test("delivery keeps reply linkage for task, question, answer, and report", async () => {
	for (const [role, senderRole, kinds] of [
		["worker", "manager", ["task", "question"]],
		["manager", "worker", ["answer", "report"]],
	] as const) {
		const bridge = await startBridge(role);
		try {
			for (const kind of kinds) {
				const inReplyToMessageId = randomUUID();
				const candidate = envelope({
					senderRole,
					event: { type: "message", messageKind: kind, payload: { text: kind }, inReplyToMessageId },
				});
				assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(candidate) })).status, "api_accepted");
				const delivered = JSON.parse(bridge.sent.at(-1)!.message) as Frame;
				assert.equal(delivered.kind, kind);
				assert.equal(delivered.workbench_message_id, candidate.messageId);
				assert.equal(delivered.in_reply_to_message_id, inReplyToMessageId);
			}
		} finally {
			await bridge.close();
		}
	}
});

test("extension reconnects after transport loss without replaying accepted input", async () => {
	const bridge = await startBridge();
	try {
		const accepted = envelope();
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(accepted) })).status, "api_accepted");
		bridge.sockets[0].destroy();
		await bridge.waitFor(frame => frame.kind === "hello", 900);
		assert.equal(bridge.sockets.length, 2);
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify({ ...accepted, deliveryAttemptId: randomUUID() }) })).status, "duplicate_api_accepted");
		assert.equal(bridge.sent.length, 1);
	} finally {
		await bridge.close();
	}
});

test("session switch rejects a late frame from the previous socket", async () => {
	const bridge = await startBridge();
	try {
		const oldSocket = bridge.sockets[0];
		const nextSessionId = randomUUID();
		const nextContext = {
			sessionManager: { getSessionId: () => nextSessionId },
			isIdle: () => true,
			hasPendingMessages: () => false,
			ui: { getEditorText: () => "" },
		};
		bridge.handlers.get("session_switch")!(undefined, nextContext);
		const staleRequestId = randomUUID();
		oldSocket.write(JSON.stringify({ kind: "probe", requestId: staleRequestId }) + "\n");
		const hello = await bridge.waitFor(frame => frame.kind === "hello" && frame.generation === 2);
		assert.equal(hello.ompSessionId, nextSessionId);
		await delay(60);
		assert.equal(
			bridge.frames.some(frame => frame.kind === "api_ack" && frame.requestId === staleRequestId),
			false,
			"old transport framed a request against the new OMP session",
		);
		assert.equal(
			(await bridge.request({ kind: "deliver", envelope: JSON.stringify(envelope({ sessionId: nextSessionId, sessionGeneration: 2 })) })).status,
			"api_accepted",
		);
	} finally {
		await bridge.close();
	}
});

test("opt-in provider request identity uses only one structured final user message", async () => {
	const bridge = await startBridge("worker", true);
	const keys = ["workbench_message_id", "workbench_delivery_attempt_id", "kind", "task_id", "revision_id", "run_id"];
	const secret = "PRIVATE_PROVIDER_BODY_AND_CREDENTIAL";
	try {
		const candidate = envelope({
			event: { type: "message", messageKind: "task", payload: { text: secret } },
		});
		const ack = await bridge.request({ kind: "deliver", envelope: JSON.stringify(candidate) });
		assert.equal(ack.status, "api_accepted");
		assert.equal(ack.modelProcessed, false);
		const injected = JSON.parse(bridge.sent[0].message) as Frame;
		bridge.handlers.get("before_provider_request")!({
			payload: { model: "local-openai-completions", messages: [
				{ role: "system", content: "prior context" },
				{ role: "user", content: [{ type: "text", text: JSON.stringify(injected) }] },
			] },
		});
		const observed = await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "provider_request_identity_probe");
		assert.equal(observed.sessionId, SESSION_ID);
		assert.equal(observed.generation, 1);
		assert.equal(observed.roleMatched, true);
		assert.equal(observed.sessionMatched, true);
		assert.equal(observed.generationMatched, true);
		assert.equal(observed.payloadShape, "object");
		assert.equal(observed.messagesArray, true);
		assert.equal(observed.lastMessageRole, "user");
		assert.equal(observed.structuredBlockCount, 1);
		assert.equal(observed.identityFieldsPresent, true);
		assert.deepEqual(observed.matches, Object.fromEntries(keys.map(key => [key, true])));
		assert.equal("status" in observed, false);
		assert.equal("modelProcessed" in observed, false);
		assert.equal(JSON.stringify(observed).includes(secret), false);
	} finally {
		await bridge.close();
	}
});

test("provider identity rejects history-only IDs, malformed input, duplicate JSON, and wrong attempt", async () => {
	const bridge = await startBridge("worker", true);
	const keys = ["workbench_message_id", "workbench_delivery_attempt_id", "kind", "task_id", "revision_id", "run_id"];
	try {
		const candidate = envelope();
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(candidate) })).status, "api_accepted");
		const injected = JSON.parse(bridge.sent[0].message) as Frame;
		const current = JSON.stringify(injected);
		const cases: Array<{ name: string; event: unknown; shape?: string; blocks?: number }> = [
			{ name: "prior history only", event: { payload: { messages: [
				{ role: "user", content: current }, { role: "user", content: "unrelated latest input" },
			] } }, blocks: 0 },
			{ name: "missing payload", event: {}, shape: "other" },
			{ name: "null payload", event: { payload: null }, shape: "null" },
			{ name: "string payload", event: { payload: "raw" }, shape: "string" },
			{ name: "malformed messages", event: { payload: { messages: {} } }, shape: "object" },
			{ name: "duplicate structured blocks", event: { payload: { messages: [
				{ role: "user", content: [{ type: "text", text: current }, { type: "text", text: current }] },
			] } }, blocks: 2 },
			{ name: "wrong attempt", event: { payload: { messages: [
				{ role: "user", content: JSON.stringify({ ...injected, workbench_delivery_attempt_id: randomUUID() }) },
			] } }, blocks: 1 },
			{ name: "non-user final message", event: { payload: { messages: [
				{ role: "user", content: current }, { role: "assistant", content: current },
			] } }, blocks: 0 },
		];
		for (const item of cases) {
			bridge.handlers.get("before_provider_request")!(item.event);
			const observed = await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "provider_request_identity_probe");
			assert.equal(keys.every(key => observed.matches[key] === true), false, item.name);
			if (item.shape !== undefined) assert.equal(observed.payloadShape, item.shape, item.name);
			if (item.blocks !== undefined) assert.equal(observed.structuredBlockCount, item.blocks, item.name);
		}

		const nextSessionId = randomUUID();
		bridge.handlers.get("session_switch")!(undefined, {
			sessionManager: { getSessionId: () => nextSessionId },
			isIdle: () => true, hasPendingMessages: () => false,
			ui: { getEditorText: () => "" },
		});
		await bridge.waitFor(frame => frame.kind === "hello" && frame.generation === 2);
		bridge.handlers.get("before_provider_request")!({ payload: { messages: [{ role: "user", content: current }] } });
		const switched = await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "provider_request_identity_probe" && frame.generation === 2);
		assert.equal(switched.sessionId, nextSessionId);
		assert.equal(switched.generation, 2);
		assert.equal(switched.roleMatched, false);
		assert.equal(switched.sessionMatched, false);
		assert.equal(switched.generationMatched, false);
		assert.equal(keys.every(key => switched.matches[key] === true), false);
	} finally {
		await bridge.close();
	}
});

test("provider request diagnostic event is absent without explicit opt-in", async () => {
	const bridge = await startBridge();
	try {
		const candidate = envelope();
		assert.equal((await bridge.request({ kind: "deliver", envelope: JSON.stringify(candidate) })).status, "api_accepted");
		const injected = JSON.parse(bridge.sent[0].message) as Frame;
		bridge.handlers.get("before_provider_request")!({ payload: { messages: [{ role: "user", content: JSON.stringify(injected) }] } });
		await bridge.waitFor(frame => frame.kind === "omp_event" && frame.name === "provider_request_started");
		await delay(20);
		assert.equal(bridge.frames.some(frame => frame.kind === "omp_event" && frame.name === "provider_request_identity_probe"), false);
	} finally {
		await bridge.close();
	}
});
