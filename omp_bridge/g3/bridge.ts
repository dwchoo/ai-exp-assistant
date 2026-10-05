import { randomUUID } from "node:crypto";
import net from "node:net";
import { parseControlEnvelope } from "../contract/v1.ts";

type Role = "manager" | "worker";
type Frame = Record<string, unknown>;
type DeliveryEvidence = {
	messageId: string;
	deliveryAttemptId: string;
	taskId: string;
	revisionId: string;
	runId: string;
	kind: string;
	role: Role;
	sessionId: string;
	generation: number;
	apiAccepted: boolean;
	providerRequestMatched: boolean;
	providerResponseObserved: boolean;
	awaitingProviderResponse: boolean;
	agentEndObserved: boolean;
	terminal: "pending" | "unknown" | "processed";
	workerStage?: "execute" | "analysis";
	workerRevision?: number;
	workerResponse?: Record<string, unknown>;
	workerResponseRejected?: string;
	providerRequestCount: number;
	assistantMessageCount: number;
};

const WORKER_RESPONSE_MARKER = "WB_WORKER_RESPONSE:";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
type WorkerResponseField = {
	name: string;
	type: "literal_string" | "canonical_uuid" | "positive_safe_integer" | "enum_string";
	value?: string | number;
	generate?: "canonical_uuid";
	allowed?: string[];
};

function workerResponseFields(delivery: DeliveryEvidence): WorkerResponseField[] {
	return [
		{ name: "stage", type: "literal_string", value: delivery.workerStage! },
		{ name: "kind", type: "literal_string", value: delivery.kind },
		{ name: "task_id", type: "canonical_uuid", value: delivery.taskId },
		{ name: "revision_id", type: "canonical_uuid", value: delivery.revisionId },
		{ name: "revision", type: "positive_safe_integer", value: delivery.workerRevision! },
		{ name: "run_id", type: "canonical_uuid", value: delivery.runId },
		{ name: "message_id", type: "canonical_uuid", value: delivery.messageId },
		{ name: "delivery_attempt_id", type: "canonical_uuid", value: delivery.deliveryAttemptId },
		{ name: "session_id", type: "canonical_uuid", value: delivery.sessionId },
		{ name: "session_generation", type: "positive_safe_integer", value: delivery.generation },
		{ name: "response_id", type: "canonical_uuid", generate: "canonical_uuid" },
		{ name: "decision", type: "enum_string", allowed: delivery.workerStage === "execute"
			? ["execute", "hold"] : ["success", "failure", "indeterminate"] },
	];
}

function workerResponseContract(delivery: DeliveryEvidence): Frame {
	const fields = workerResponseFields(delivery);
	return {
		version: 1,
		marker: WORKER_RESPONSE_MARKER,
		format: "marker_plus_compact_flat_json",
		field_order: fields.map(field => field.name),
		fields,
		output_rules: {
			exactly_one_frame: true, no_prose: true, no_tools: true,
			no_thinking: true, no_markdown: true, no_extra_content: true,
		},
		instruction: "Emit only the marker immediately followed by one compact flat JSON object in field_order; no prose, tools, thinking, markdown, whitespace, or additional messages.",
	};
}

function parseWorkerResponse(text: string, delivery: DeliveryEvidence): Record<string, unknown> | undefined {
	if (!text.startsWith(WORKER_RESPONSE_MARKER)) return undefined;
	const body = text.slice(WORKER_RESPONSE_MARKER.length);
	if (!body.startsWith("{") || !body.endsWith("}")) return undefined;
	const fields = workerResponseFields(delivery);
	const tokens = body.slice(1, -1).split(",");
	if (tokens.length !== fields.length) return undefined;
	const result: Record<string, unknown> = {};
	for (let index = 0; index < fields.length; index += 1) {
		const field = fields[index];
		const match = /^"([a-z_]+)":(?:"([A-Za-z0-9_-]+)"|([1-9][0-9]*))$/.exec(tokens[index]);
		if (!match || match[1] !== field.name) return undefined;
		const value = match[3] === undefined ? match[2] : Number(match[3]);
		if (field.type === "canonical_uuid" && (typeof value !== "string" || !UUID_PATTERN.test(value))) return undefined;
		if (field.type === "positive_safe_integer" && (typeof value !== "number"
			|| !Number.isSafeInteger(value) || value < 1)) return undefined;
		if ((field.type === "literal_string" || field.type === "enum_string") && typeof value !== "string") return undefined;
		if (field.value !== undefined && value !== field.value) return undefined;
		if (field.allowed !== undefined && !field.allowed.includes(value as string)) return undefined;
		result[field.name] = value;
	}
	return result;
}

// CW-18 smoke-02 E1: the provider request payload shapes OMP 18.4.5 builds (captured with a local capture
// server; tests/bridge/fixtures/omp18_provider_payloads.json):
//   - chat (openai-completions and similar): payload.messages[-1] = {role: "user", content: string | [{type: "text", text}]}
//   - Responses (openai-codex, openai-responses): payload.input[-1] = {role: "user", content: [{type: "input_text", text}]}
//     (item type absent or "message"); over the Codex WebSocket payload.input is only the delta after
//     previous_response_id, and the delivered prompt is its single user item.
// Only the final item of the one list present counts; a payload with both lists, or any other final item
// (assistant, developer, tool output, another item type or block type), establishes no identity.
type FinalUserTexts = { list: "messages" | "input"; item: Record<string, unknown> | undefined; texts: string[] };

function finalUserTexts(payload: unknown): FinalUserTexts | undefined {
	if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return undefined;
	const request = payload as Record<string, unknown>;
	const messages = Array.isArray(request.messages) ? request.messages : undefined;
	const input = Array.isArray(request.input) ? request.input : undefined;
	if ((messages === undefined) === (input === undefined)) return undefined;
	const list = messages !== undefined ? "messages" : "input";
	const last = (messages ?? input)!.at(-1);
	const item = typeof last === "object" && last !== null && !Array.isArray(last) ? last as Record<string, unknown> : undefined;
	if (item === undefined || item.role !== "user") return { list, item, texts: [] };
	if (list === "input" && item.type !== undefined && item.type !== "message") return { list, item, texts: [] };
	const blockType = list === "messages" ? "text" : "input_text";
	const content = item.content;
	const texts = typeof content === "string" ? [content]
		: Array.isArray(content) ? content.filter((block): block is { type: string; text: string } =>
			typeof block === "object" && block !== null && "type" in block && block.type === blockType
			&& "text" in block && typeof block.text === "string").map(block => block.text)
		: [];
	return { list, item, texts };
}

// A reasoning model's assistant message (OMP 18.4.5 openai-codex) starts with a thinking block that carries only
// the provider's encrypted reasoning: `thinking` is "" and the rest is the opaque `thinkingSignature`. It holds
// no response content; visible thinking text or any other key or block is still response content (no_thinking).
function isOpaqueThinking(block: unknown): boolean {
	if (typeof block !== "object" || block === null || Array.isArray(block)) return false;
	const record = block as Record<string, unknown>;
	return record.type === "thinking" && (record.thinking === undefined || record.thinking === "")
		&& Object.keys(record).every(key => key === "type" || key === "thinking"
			|| (key === "thinkingSignature" && typeof record.thinkingSignature === "string"));
}

function env(name: string): string {
	const value = process.env[name];
	if (!value) throw new Error(`missing ${name}`);
	return value;
}

function sendLine(socket: net.Socket, value: Frame): void {
	if (!socket.destroyed) socket.write(`${JSON.stringify(value)}\n`);
}

function roleAllows(role: Role, senderRole: string, kind: string): boolean {
	return role === "worker"
		? senderRole === "manager" && (kind === "task" || kind === "question")
		: senderRole === "worker" && (kind === "answer" || kind === "report");
}

// CW-18 (C-D64/C-D65/C-D66) handoff tools. The extension only forwards one request
// frame; the backend decides (Task, one at a time, pause) and delivers through the
// existing TaskMailbox path. The result returns at once; it never waits for
// the other OMP. A lost answer is outcome_unknown and is never resent.
const TOOL_RESULT_TIMEOUT_MS = 10_000;
const MESSAGE_MAX = 8192;
// Smoke D1 (OMP 18.4.5, openai-codex): the tools are registered with `strict: true`, so OMP sends a strict
// schema (every property required, the optional ones `anyOf [T, null]`; OMP drops length/pattern keywords on
// that wire copy). Every optional field therefore accepts null here as well (OMP validates the arguments
// against this schema locally) and its description says "null when not used". Optional strings carry no
// minLength and the UUID pattern also matches "": the backend treats null, blank strings, empty lists/objects
// and false flags as absent and names the field when it rejects a call.
const NOT_USED = "null when not used.";
const TEXT = { type: "string", minLength: 1, maxLength: MESSAGE_MAX };
const OPTIONAL_TEXT = { type: ["string", "null"], maxLength: MESSAGE_MAX };
const SHORT = { type: "string", maxLength: 1024 };
const OPTIONAL_UUID = { type: ["string", "null"], pattern: `^(?:${UUID_PATTERN.source.slice(1, -1)})?$` };
const OPTIONAL_FLAG = { type: ["boolean", "null"] };
const PATHS = {
	type: ["array", "null"], items: SHORT, maxItems: 64,
	description: "Repo-relative paths (relative to the project root, e.g. \"src/app/\" or \"work/hello.txt\"); "
		+ "[] or null when none.",
};
const TO_WORKER_PARAMETERS = {
	type: "object",
	additionalProperties: false,
	required: ["kind", "message"],
	properties: {
		task_id: { ...OPTIONAL_UUID,
			description: `The active Task this follow-up, cancel or re-run belongs to (from a to_worker result); ${NOT_USED} `
				+ "null starts a new Task." },
		kind: { type: "string", enum: ["experiment", "work"],
			description: "experiment: run a command and judge criteria; work: the worker does the work itself." },
		message: { ...TEXT, description: "The instruction or summary for the worker (no secret values)." },
		spec: {
			type: ["object", "null"], additionalProperties: false, required: ["goal", "paths"],
			description: `The Task: goal and the paths the worker may change (required for a new Task); ${NOT_USED} `
				+ "null for a follow-up or cancel.",
			properties: {
				goal: { ...OPTIONAL_TEXT, description: "What the Task must achieve." },
				paths: { ...PATHS, description: `Paths the worker may change. ${PATHS.description}` },
				instructions: { ...OPTIONAL_TEXT, description: `Extra instructions for the worker; ${NOT_USED}` },
				execution: {
					type: ["object", "null"], additionalProperties: false,
					required: ["source", "commit", "command", "criteria", "environment", "shell"],
					description: `Experiment run (kind experiment only). Must be null for kind work; ${NOT_USED}`,
					properties: {
						source: SHORT, commit: SHORT,
						command: { anyOf: [{ type: "string", maxLength: MESSAGE_MAX },
							{ type: "array", items: { type: "string", maxLength: MESSAGE_MAX }, maxItems: 64 }] },
						criteria: {
							type: "object", additionalProperties: false,
							required: ["log_contains", "result_file", "result_contains"],
							// Smoke-02 E2: the CW-10 judge needs all three; none is optional.
							description: "All three are required and non-empty. Success = exit 0 AND log_contains in the "
								+ "command output AND result_file written by this run AND result_contains in it. Do not "
								+ "invent a condition the user did not ask for: ask the user, or make the command write a "
								+ "result file.",
							properties: {
								log_contains: { ...SHORT, description: "Text the command prints (stdout/stderr, the raw log)." },
								result_file: { ...SHORT, description: "Repo-relative path of a file the command writes during "
									+ "this run (e.g. out/result.txt). A file the run does not write or leaves unchanged "
									+ "(such as the script itself) makes the result indeterminate." },
								result_contains: { ...SHORT, description: "Text result_file must contain after the run." },
							},
						},
						environment: { type: "array", maxItems: 64, description: "Environment variable NAMES only, never values.",
							items: { type: "string", pattern: "^[A-Za-z_][A-Za-z0-9_]*$", maxLength: 128 } },
						shell: { type: "string", enum: ["bash", "sh"] },
					},
				},
			},
		},
		run: { ...OPTIONAL_FLAG, description: `true re-runs the current experiment Task (at most 3 re-runs per Task); ${NOT_USED}` },
		cancel: { ...OPTIONAL_FLAG, description: `true (with task_id) cancels that Task; the worker is told and becomes free; ${NOT_USED}` },
	},
};
const TO_MANAGER_PARAMETERS = {
	type: "object",
	additionalProperties: false,
	required: ["kind", "message"],
	properties: {
		kind: { type: "string", enum: ["answer", "progress", "done", "blocked", "report"] },
		message: { ...TEXT, description: "The report for the manager (no secret values)." },
		task_id: { ...OPTIONAL_UUID, description: `The Task this belongs to; ${NOT_USED}` },
		in_reply_to: { ...OPTIONAL_UUID,
			description: `workbench_message_id of the manager message answered (needed for kind answer); ${NOT_USED}` },
		requires_code_change: { ...OPTIONAL_FLAG, description: `true only when the result needs a code change; ${NOT_USED}` },
		reason: { ...OPTIONAL_TEXT, description: `Why (for blocked or requires_code_change); ${NOT_USED}` },
		request: {
			type: ["object", "null"], additionalProperties: false, required: ["goal", "paths"],
			description: "A request for new or wider scope (at least one path); the manager decides whether to send it "
				+ `as a Task; ${NOT_USED}`,
			properties: { goal: OPTIONAL_TEXT, paths: PATHS },
		},
	},
};
const HANDOFF_TOOLS: Record<Role, { name: "to_worker" | "to_manager"; label: string; description: string; parameters: Frame }> = {
	manager: {
		name: "to_worker", label: "To worker", parameters: TO_WORKER_PARAMETERS,
		description: "Send an instruction to the Workbench worker OMP. The worker does ONE task at a time. If it is "
			+ "busy you get worker_busy with the current task; wait for its to_manager report (done/blocked) or "
			+ "cancel the task. Without task_id (null) a new Task is dispatched at once (status dispatched; the user "
			+ "delegated this, no approval step). With the current task_id it is a follow-up message for the "
			+ "worker (status queued), run: true re-runs an experiment, cancel: true cancels the Task. Set every "
			+ "field you do not use to null. The result returns at once; then end your turn: the worker's to_manager "
			+ "report arrives as a new message (never wait or poll for it).",
	},
	worker: {
		name: "to_manager", label: "To manager", parameters: TO_MANAGER_PARAMETERS,
		description: "Report to the Workbench manager OMP about the active Task: answer, progress, done, blocked "
			+ "or report. Set every field you do not use to null. After done or blocked, end your turn. Never use it "
			+ "while answering a message that carries a response_contract.",
	},
};

export default function workbenchG3Extension(pi: any): void {
	const socketPath = env("WORKBENCH_G3_BRIDGE_SOCKET");
	const role = env("WORKBENCH_G3_ROLE") as Role;
	const token = env("WORKBENCH_G3_TOKEN");
	const expectedResponseMarker = process.env.WORKBENCH_G3_EXPECTED_RESPONSE_MARKER;
	const eventSurfaceProbe = process.env.WORKBENCH_G3_EVENT_SURFACE_PROBE === "1";
	const handlerFaultProbe = process.env.WORKBENCH_G3_HANDLER_FAULT_PROBE === "1";
	let handlerFaultInjected = false;
	let generation = Number(env("WORKBENCH_G3_GENERATION"));
	if ((role !== "manager" && role !== "worker") || !Number.isSafeInteger(generation) || generation < 1) {
		throw new Error("invalid G3 role or session generation");
	}

	let socket: net.Socket | undefined;
	let context: any;
	let ompSessionId = "";
	let paused = false;
	let connected = false;
	let inbound = "";
	let shuttingDown = false;
	let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
	let reconnectAttempt = 0;
	let agentActive = false;
	let activeDelivery: DeliveryEvidence | undefined;
	let turnSequence = 0;
	let abortTargetTurn: number | undefined;
	let abortRequestId: string | undefined;
	let abortStatus: "none" | "requested" | "stop_observed" | "request_failed" = "none";
	let pauseEpoch = 0;
	const seen = new Map<string, "deferred" | "sending" | "api_accepted" | "unknown">();
	let probeDelivery: { message: Record<string, unknown>; role: Role; sessionId: string; generation: number } | undefined;
	const approvals = new Set<string>();
	const executingCalls = new Set<string>();
	const unknownOutcomeCalls = new Set<string>();
	const unresolvedPriorSessions: Array<{
		sessionId: string;
		generation: number;
		abortStatus: string;
		unconfirmedToolCallIds: string[];
	}> = [];
	const pendingToolResults = new Map<string, {
		toolCallId: string;
		client: net.Socket;
		settle: (result: Frame) => void;
	}>();

	function snapshot(): Frame {
		let editorText: string | undefined;
		try {
			editorText = context?.ui?.getEditorText?.();
		} catch {
			editorText = undefined;
		}
		let idle = false;
		let pending = true;
		try {
			idle = context?.isIdle?.() === true;
			pending = context?.hasPendingMessages?.() !== false;
		} catch {
			// Unknown state remains fail-closed.
		}
		return {
			kind: "state",
			role,
			sessionId: ompSessionId,
			generation,
			idle,
			pending,
			approvalPending: approvals.size > 0,
			inFlightToolCount: executingCalls.size,
			unconfirmedToolCallIds: [...executingCalls].sort(),
			unknownOutcomeToolCallIds: [...unknownOutcomeCalls].sort(),
			unresolvedPriorSessions: [...unresolvedPriorSessions],
			abortStatus,
			editorKnown: typeof editorText === "string",
			editorEmpty: editorText === "",
			editorLength: typeof editorText === "string" ? editorText.length : null,
			paused,
		};
	}

	function publishState(): void {
		if (connected) sendLine(socket!, snapshot());
	}

	function isCurrentConnection(client: net.Socket, sessionId: string, sessionGeneration: number): boolean {
		return !shuttingDown
			&& socket === client
			&& ompSessionId === sessionId
			&& generation === sessionGeneration;
	}

	function acknowledge(
		client: net.Socket,
		sessionId: string,
		sessionGeneration: number,
		requestId: string,
		status: string,
		extra: Frame = {},
	): void {
		if (isCurrentConnection(client, sessionId, sessionGeneration)) {
			sendLine(client, { kind: "api_ack", requestId, status, ...extra });
		}
	}

	function sendEvent(name: string, fields: Frame = {}): void {
		if (!connected) return;
		sendLine(socket!, { kind: "omp_event", name, sessionId: ompSessionId, generation, ...fields });
	}

	function deliveryIdentity(delivery: DeliveryEvidence): Frame {
		return {
			messageId: delivery.messageId,
			deliveryAttemptId: delivery.deliveryAttemptId,
			taskId: delivery.taskId,
			revisionId: delivery.revisionId,
			runId: delivery.runId,
			sessionId: delivery.sessionId,
			generation: delivery.generation,
		};
	}

	function finishDeliveryEvidence(): void {
		const delivery = activeDelivery;
		if (!delivery || delivery.terminal !== "pending" || !delivery.apiAccepted || !delivery.agentEndObserved) return;
		if (delivery.providerRequestMatched && delivery.providerResponseObserved) {
		if (delivery.workerStage && delivery.workerResponse && !delivery.workerResponseRejected
			&& delivery.providerRequestCount === 1 && delivery.assistantMessageCount === 1
			&& delivery.sessionId === ompSessionId && delivery.generation === generation && !paused) {
			sendEvent("assistant_message_end", {
				...deliveryIdentity(delivery), workerResponse: delivery.workerResponse,
			});
		} else if (delivery.workerStage) {
			sendEvent("worker_response_rejected", {
				...deliveryIdentity(delivery), reason: delivery.workerResponseRejected ?? "missing_terminal_worker_response",
			});
		}
			delivery.terminal = "processed";
			sendEvent("delivery_omp_processed", {
				...deliveryIdentity(delivery),
				providerRequestMatched: true,
				providerResponseObserved: true,
				agentEndObserved: true,
				...(delivery.workerStage && delivery.workerResponse && !delivery.workerResponseRejected
					&& delivery.providerRequestCount === 1 && delivery.assistantMessageCount === 1
					&& delivery.sessionId === ompSessionId && delivery.generation === generation && !paused
					? { workerResponseId: delivery.workerResponse.response_id } : {}),
			});
		} else {
			delivery.terminal = "unknown";
			sendEvent("delivery_processing_unknown", {
				...deliveryIdentity(delivery),
				providerRequestMatched: delivery.providerRequestMatched,
				providerResponseObserved: delivery.providerResponseObserved,
				agentEndObserved: true,
				reason: "agent_ended_without_complete_provider_evidence",
			});
		}
	}

	function markDeliveryUnknown(reason: string): void {
		const delivery = activeDelivery;
		if (!delivery || delivery.terminal !== "pending") return;
		delivery.terminal = "unknown";
		sendEvent("delivery_processing_unknown", {
			...deliveryIdentity(delivery),
			providerRequestMatched: delivery.providerRequestMatched,
			providerResponseObserved: delivery.providerResponseObserved,
			agentEndObserved: delivery.agentEndObserved,
			reason,
		});
	}

	function providerRequestContainsActiveDelivery(event: unknown, delivery: DeliveryEvidence): boolean {
		if (typeof event !== "object" || event === null) return false;
		const blocks = finalUserTexts((event as Record<string, unknown>).payload)?.texts ?? [];
		for (const text of blocks) {
			try {
				const value: unknown = JSON.parse(text);
				if (typeof value !== "object" || value === null || Array.isArray(value)) continue;
				const message = value as Record<string, unknown>;
				if (message.workbench_message_id === delivery.messageId
					&& message.workbench_delivery_attempt_id === delivery.deliveryAttemptId
					&& message.kind === delivery.kind
					&& message.task_id === delivery.taskId
					&& message.revision_id === delivery.revisionId
					&& message.run_id === delivery.runId) return true;
			} catch { /* The provider request only establishes identity for this exact JSON envelope. */ }
		}
		return false;
	}

	function isMessageSafe(): boolean {
		const state = snapshot();
		return state.idle === true
			&& state.pending === false
			&& state.approvalPending === false
			&& state.editorKnown === true
			&& state.editorEmpty === true
			&& executingCalls.size === 0;
	}

	async function handle(
		frame: Frame,
		client: net.Socket,
		sessionId: string,
		sessionGeneration: number,
	): Promise<void> {
		if (!isCurrentConnection(client, sessionId, sessionGeneration)) return;
		const ack = (requestId: string, status: string, extra: Frame = {}) =>
			acknowledge(client, sessionId, sessionGeneration, requestId, status, extra);
		if (frame.kind === "probe" && typeof frame.requestId === "string") {
			ack(frame.requestId, "state", { state: snapshot() });
			return;
		}
		if (frame.kind === "pause" && typeof frame.requestId === "string") {
			paused = true;
			if (activeDelivery?.workerStage) activeDelivery.workerResponseRejected = "automation_paused";
			pauseEpoch += 1;
			for (const toolCallId of executingCalls) unknownOutcomeCalls.add(toolCallId);
			// A deferred request must receive a new manager decision after resume.
			for (const [identity, state] of seen) {
				if (state === "deferred") seen.set(identity, "unknown");
			}
			let managerTurnMayBeActive = agentActive;
			if (role === "manager" && !managerTurnMayBeActive) {
				try {
					managerTurnMayBeActive = context?.isIdle?.() !== true;
				} catch {
					// An unknown idle state cannot establish that no turn needs abort.
					managerTurnMayBeActive = true;
				}
			}
			if (role === "manager" && managerTurnMayBeActive) {
				if (abortStatus !== "requested") {
					abortTargetTurn = agentActive ? turnSequence : undefined;
					abortRequestId = frame.requestId;
					abortStatus = "requested";
					try {
						context.abort();
					} catch (error) {
						abortStatus = "request_failed";
						ack(frame.requestId, "abort_request_failed", {
							reason: error instanceof Error ? error.message : "abort failed",
							unconfirmedToolCallIds: [...executingCalls].sort(),
						});
						publishState();
						return;
					}
				}
				ack(frame.requestId, "abort_requested", {
					unconfirmedToolCallIds: [...executingCalls].sort(),
				});
			} else {
				ack(frame.requestId, "paused", {
					unconfirmedToolCallIds: [...executingCalls].sort(),
					approvalPendingCount: approvals.size,
				});
			}
			publishState();
			return;
		}
		if (frame.kind === "resume" && typeof frame.requestId === "string") {
			if (!paused) {
				ack(frame.requestId, "already_resumed", { state: snapshot() });
			} else if (abortStatus === "requested") {
				ack(frame.requestId, "abort_pending", { state: snapshot() });
			} else if (frame.reconciled !== true) {
				ack(frame.requestId, "reconciliation_required", { state: snapshot() });
			} else {
				paused = false;
				// Resume authorizes new work; it does not establish a prior tool's effect.
				// Keep unresolved evidence until a separate explicit resolution contract exists.
				ack(frame.requestId, "resumed", { state: snapshot() });
				publishState();
			}
			return;
		}
		if (frame.kind === "tool_result" && typeof frame.requestId === "string") {
			const pending = pendingToolResults.get(frame.requestId);
			const result = frame.result;
			if (pending && pending.client === client && frame.toolCallId === pending.toolCallId
				&& typeof result === "object" && result !== null && !Array.isArray(result)) {
				pending.settle(result as Frame);
			}
			return;
		}
		if (frame.kind !== "deliver" || typeof frame.requestId !== "string" || typeof frame.envelope !== "string") return;

		const requestId = frame.requestId;
		let envelope: ReturnType<typeof parseControlEnvelope>;
		try {
			envelope = parseControlEnvelope(JSON.parse(frame.envelope) as unknown, {
				sessionId: ompSessionId,
				sessionGeneration: generation,
			});
		} catch (error) {
			ack(requestId, "rejected", { reason: error instanceof Error ? error.message : "invalid envelope" });
			return;
		}
		if (envelope.event.type !== "message" || !roleAllows(role, envelope.senderRole, envelope.event.messageKind)) {
			ack(requestId, "rejected", { reason: "role/kind does not match target OMP session" });
			return;
		}
		const messageIdentity = `${envelope.sessionId}:${envelope.sessionGeneration}:${envelope.messageId}`;
		const prior = seen.get(messageIdentity);
		if (prior === "api_accepted") {
			ack(requestId, "duplicate_api_accepted", { messageId: envelope.messageId });
			return;
		}
		if (prior === "unknown" || prior === "sending") {
			ack(requestId, "unknown_no_replay", { messageId: envelope.messageId });
			return;
		}
		if (paused) {
			seen.set(messageIdentity, "unknown");
			ack(requestId, "deferred", { reason: "Workbench automation is paused" });
			publishState();
			return;
		}
		if (!isMessageSafe()) {
			seen.set(messageIdentity, "deferred");
			ack(requestId, "deferred", { reason: "OMP is busy, has pending work/approval, or composer state is unsafe" });
			publishState();
			return;
		}

		seen.set(messageIdentity, "sending");
		if (handlerFaultProbe && !handlerFaultInjected && frame.diagnosticFault === "handler_exception") {
			handlerFaultInjected = true;
			seen.set(messageIdentity, "unknown");
			sendEvent(`diagnostic_handler_fault:${requestId}`, { requestId, messageId: envelope.messageId });
			throw new Error("opt-in G3 diagnostic frame-handler exception");
		}
		const sendPauseEpoch = pauseEpoch;
		const payloadStage = envelope.event.payload.stage;
		const payloadRevision = envelope.event.payload.revision;
		const workerStage = role === "worker" && ((envelope.event.messageKind === "task" && payloadStage === "execute")
			|| (envelope.event.messageKind === "question" && payloadStage === "analysis"))
			&& typeof payloadRevision === "number" && Number.isSafeInteger(payloadRevision) && payloadRevision > 0
			? payloadStage as "execute" | "analysis" : undefined;
		activeDelivery = {
			messageId: envelope.messageId,
			deliveryAttemptId: envelope.deliveryAttemptId,
			taskId: envelope.taskId,
			revisionId: envelope.revisionId,
			runId: envelope.runId,
			kind: envelope.event.messageKind,
			role,
			sessionId: ompSessionId,
			generation,
			apiAccepted: false,
			providerRequestMatched: false,
			providerResponseObserved: false,
			awaitingProviderResponse: false,
			agentEndObserved: false,
			terminal: "pending",
			providerRequestCount: 0,
			assistantMessageCount: 0,
			...(workerStage ? { workerStage, workerRevision: payloadRevision as number } : {}),
		};
		const message = {
			workbench_message_id: envelope.messageId,
			workbench_delivery_attempt_id: envelope.deliveryAttemptId,
			kind: envelope.event.messageKind,
			...(envelope.event.inReplyToMessageId === undefined
				? {} : { in_reply_to_message_id: envelope.event.inReplyToMessageId }),
			task_id: envelope.taskId,
			revision_id: envelope.revisionId,
			run_id: envelope.runId,
			session_id: ompSessionId,
			session_generation: generation,
			payload: envelope.event.payload,
			...(activeDelivery.workerStage ? { response_contract: workerResponseContract(activeDelivery) } : {}),
		};
		if (eventSurfaceProbe) probeDelivery = { message, role, sessionId, generation: sessionGeneration };
		try {
			await pi.sendUserMessage(JSON.stringify(message), {
				attribution: "agent",
			});
			if (pauseEpoch !== sendPauseEpoch) {
				// The public call may already have enqueued a turn before pause.
				seen.set(messageIdentity, "unknown");
				markDeliveryUnknown("automation_paused_during_injection");
				ack(requestId, "unknown_no_replay", { messageId: envelope.messageId });
			} else {
				seen.set(messageIdentity, "api_accepted");
				if (activeDelivery?.messageId === envelope.messageId) {
					activeDelivery.apiAccepted = true;
					finishDeliveryEvidence();
				}
				ack(requestId, "api_accepted", {
					messageId: envelope.messageId,
					modelProcessed: false,
				});
			}
		} catch (error) {
			// The public call may have enqueued before a later runtime error. Never replay it.
			seen.set(messageIdentity, "unknown");
			markDeliveryUnknown("send_user_message_outcome_unknown");
			ack(requestId, "unknown_no_replay", {
				messageId: envelope.messageId,
				reason: error instanceof Error ? error.message : "send failed",
			});
		}
		if (isCurrentConnection(client, sessionId, sessionGeneration)) publishState();
	}

	function failPendingTools(client: net.Socket | undefined, reason: string): void {
		for (const pending of [...pendingToolResults.values()]) {
			if (client === undefined || pending.client === client) pending.settle({ status: "outcome_unknown", reason });
		}
	}

	function requestHandoff(tool: "to_worker" | "to_manager", toolCallId: unknown, params: unknown,
		signal: AbortSignal | undefined): Promise<Frame> {
		// The worker's staged reply is one provider round without tools; never forward a report from inside it.
		if (activeDelivery?.workerStage && activeDelivery.terminal === "pending") {
			return Promise.resolve({ status: "rejected", reason: "staged_delivery_pending" });
		}
		if (typeof toolCallId !== "string" || toolCallId.length === 0
			|| typeof params !== "object" || params === null || Array.isArray(params)) {
			return Promise.resolve({ status: "rejected", reason: "invalid_tool_call" });
		}
		const client = socket;
		if (!connected || client === undefined || client.destroyed) {
			return Promise.resolve({ status: "rejected", reason: "bridge_not_connected" });
		}
		if (signal?.aborted) return Promise.resolve({ status: "rejected", reason: "aborted_before_send" });
		// OMP adds an intent field `i` to every tool schema; it is not a Workbench argument.
		const { i: _intent, ...args } = params as Frame;
		const requestId = randomUUID();
		return new Promise(resolve => {
			let settled = false;
			const onAbort = () => settle({ status: "outcome_unknown", reason: "aborted" });
			const timer = setTimeout(() => settle({ status: "outcome_unknown", reason: "timeout" }), TOOL_RESULT_TIMEOUT_MS);
			function settle(result: Frame): void {
				if (settled) return;
				settled = true;
				clearTimeout(timer);
				signal?.removeEventListener("abort", onAbort);
				pendingToolResults.delete(requestId);
				resolve(result);
			}
			pendingToolResults.set(requestId, { toolCallId, client, settle });
			signal?.addEventListener("abort", onAbort, { once: true });
			// Sent once. A timeout, abort or disconnect leaves the backend outcome unknown; no resend.
			sendLine(client, { kind: "tool_request", requestId, toolCallId, tool, args, sessionId: ompSessionId, generation });
		});
	}

	if (typeof pi.registerTool === "function") {
		const tool = HANDOFF_TOOLS[role];
		pi.registerTool({
			name: tool.name,
			label: tool.label,
			description: tool.description,
			parameters: tool.parameters,
			// OMP sends a strict (all required, optional nullable) schema to providers that support it (smoke D1).
			strict: true,
			// Ship the schema with every provider request instead of xd:// discovery.
			loadMode: "essential",
			async execute(toolCallId: string, params: unknown, signal?: AbortSignal) {
				const result = await requestHandoff(tool.name, toolCallId, params, signal);
				return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
			},
		});
	}

	function clearReconnect(): void {
		if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
		reconnectTimer = undefined;
	}

	function openConnection(): void {
		if (shuttingDown) return;
		inbound = "";
		connected = false;
		const client = net.createConnection(socketPath);
		const sessionId = ompSessionId;
		const sessionGeneration = generation;
		socket = client;
		client.on("connect", () => {
			if (!isCurrentConnection(client, sessionId, sessionGeneration)) {
				client.end();
				return;
			}
			connected = true;
			reconnectAttempt = 0;
			sendLine(client, {
				kind: "hello",
				protocolVersion: 1,
				token,
				role,
				ompSessionId,
				generation,
				pid: process.pid,
				ompVersion: process.env.WORKBENCH_G3_EXPECTED_OMP_VERSION ?? null,
			});
			publishState();
		});
		client.on("data", chunk => {
			if (!isCurrentConnection(client, sessionId, sessionGeneration)) return;
			inbound += chunk.toString("utf8");
			for (;;) {
				const index = inbound.indexOf("\n");
				if (index < 0) break;
				const line = inbound.slice(0, index);
				inbound = inbound.slice(index + 1);
				try {
					void handle(JSON.parse(line) as Frame, client, sessionId, sessionGeneration)
						.catch(error => pi.logger?.error?.("workbench G3 bridge frame handler failure", error));
				} catch (error) {
					pi.logger?.error?.("workbench G3 extension frame failure", error);
				}
			}
		});
		client.on("error", error => {
			failPendingTools(client, "bridge_disconnected");
			if (isCurrentConnection(client, sessionId, sessionGeneration)) {
				if (activeDelivery?.workerStage) markDeliveryUnknown("bridge_disconnected");
				connected = false;
				pi.logger?.error?.("workbench G3 bridge disconnected", error);
			}
		});
		client.on("close", () => {
			failPendingTools(client, "bridge_disconnected");
			if (!isCurrentConnection(client, sessionId, sessionGeneration)) return;
			if (activeDelivery?.workerStage) markDeliveryUnknown("bridge_disconnected");
			connected = false;
			if (shuttingDown || reconnectTimer !== undefined) return;
			const delayMs = Math.min(50 * (2 ** Math.min(reconnectAttempt, 5)), 1000);
			reconnectAttempt += 1;
			reconnectTimer = setTimeout(() => {
				reconnectTimer = undefined;
				if (isCurrentConnection(client, sessionId, sessionGeneration)) openConnection();
			}, delayMs);
			reconnectTimer.unref?.();
		});
	}

	function connect(ctx: any, reason: "start" | "switch"): void {
		if (shuttingDown) return;
		if (reason === "switch") {
			if (activeDelivery?.workerStage) markDeliveryUnknown("session_switched");
			probeDelivery = undefined;
			activeDelivery = undefined;
			const unresolvedTools = new Set([...executingCalls, ...unknownOutcomeCalls]);
			if (abortStatus === "requested" || abortStatus === "request_failed" || unresolvedTools.size > 0) {
				unresolvedPriorSessions.push({
					sessionId: ompSessionId,
					generation,
					abortStatus,
					unconfirmedToolCallIds: [...unresolvedTools].sort(),
				});
			}
			generation += 1;
			agentActive = false;
			abortTargetTurn = undefined;
			abortRequestId = undefined;
			abortStatus = "none";
			executingCalls.clear();
			unknownOutcomeCalls.clear();
			approvals.clear();
		}
		context = ctx;
		ompSessionId = String(ctx.sessionManager.getSessionId());
		clearReconnect();
		reconnectAttempt = 0;
		const previous = socket;
		socket = undefined;
		connected = false;
		inbound = "";
		previous?.end();
		openConnection();
	}

	pi.on("session_start", (_event: unknown, ctx: any) => connect(ctx, "start"));
	pi.on("session_switch", (_event: unknown, ctx: any) => {
		connect(ctx, "switch");
	});
	pi.on("session_shutdown", () => {
		if (activeDelivery?.workerStage) markDeliveryUnknown("session_shutdown");
		shuttingDown = true;
		failPendingTools(undefined, "session_shutdown");
		clearReconnect();
		const previous = socket;
		socket = undefined;
		connected = false;
		inbound = "";
		previous?.end();
	});
	pi.on("input", () => publishState());
	pi.on("agent_start", () => {
		agentActive = true;
		turnSequence += 1;
		if (abortStatus === "requested" && abortTargetTurn === undefined) abortTargetTurn = turnSequence;
		sendEvent("agent_start");
		publishState();
	});
	pi.on("agent_end", () => {
		agentActive = false;
		sendEvent("agent_end");
		if (activeDelivery?.terminal === "pending"
			&& activeDelivery.role === role
			&& activeDelivery.sessionId === ompSessionId
			&& activeDelivery.generation === generation) {
			activeDelivery.agentEndObserved = true;
			finishDeliveryEvidence();
		}
		if (abortStatus === "requested" && (abortTargetTurn === undefined || abortTargetTurn === turnSequence)) {
			// agent_end observes a stopped turn; it does not prove abort causation or tool rollback.
			abortStatus = "stop_observed";
			sendEvent("turn_stop_observed", {
				abortRequestId,
				unconfirmedToolCallIds: [...executingCalls].sort(),
			});
		}
		publishState();
	});
	pi.on("before_provider_request", (event: unknown) => {
		sendEvent("provider_request_started");
		if (activeDelivery?.terminal === "pending") {
			activeDelivery.providerRequestCount += 1;
			if (activeDelivery.workerStage && activeDelivery.providerRequestCount !== 1)
				activeDelivery.workerResponseRejected = "additional_provider_round";
			if (providerRequestContainsActiveDelivery(event, activeDelivery)) {
				activeDelivery.providerRequestMatched = true;
				activeDelivery.awaitingProviderResponse = true;
			} else if (!activeDelivery.providerRequestMatched) {
				markDeliveryUnknown("provider_request_identity_not_observed");
			}
		}
		if (!eventSurfaceProbe) return;
		const record = typeof event === "object" && event !== null ? event as Record<string, unknown> : undefined;
		const payload = record?.payload;
		const payloadShape = payload === null ? "null" : Array.isArray(payload) ? "array"
			: typeof payload === "object" ? "object" : typeof payload === "string" ? "string" : "other";
		const request = payloadShape === "object" ? payload as Record<string, unknown> : undefined;
		const payloadKeys = ["messages", "input", "model", "stream", "tools", "temperature"]
			.filter(key => request !== undefined && Object.hasOwn(request, key));
		const messages = Array.isArray(request?.messages) ? request.messages : undefined;
		// E1: the same final-item rule as the identity check (chat messages or Responses input).
		const final = finalUserTexts(payload);
		const lastMessage = final?.item;
		const lastRole = lastMessage?.role;
		const content = lastMessage?.content;
		const contentShape = typeof content === "string" ? "string" : Array.isArray(content) ? "blocks" : "other";
		const textBlocks = Array.isArray(content) ? final?.texts ?? [] : [];
		const candidateTexts = final?.texts ?? [];
		const structured: Record<string, unknown>[] = [];
		let observed: Record<string, unknown> | undefined;
		if (lastRole === "user") {
			for (const text of candidateTexts) {
				try {
					const parsed: unknown = JSON.parse(text);
					if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) structured.push(parsed as Record<string, unknown>);
				} catch { /* Do not inspect earlier history when the last message is unparseable. */ }
			}
		}
		if (structured.length === 1) observed = structured[0];
		const keys = ["workbench_message_id", "workbench_delivery_attempt_id", "kind", "task_id", "revision_id", "run_id"];
		const matches = Object.fromEntries(keys.map(key => [key, observed !== undefined && probeDelivery !== undefined
			&& typeof observed[key] === "string" && observed[key] === probeDelivery.message[key]]));
		sendEvent("provider_request_identity_probe", {
			eventObject: record !== undefined,
			payloadShape,
			payloadKeys,
			messagesArray: messages !== undefined,
			lastMessageRole: lastRole === "user" || lastRole === "assistant" || lastRole === "tool" || lastRole === "system" ? lastRole : "other",
			lastContentShape: contentShape,
			textBlockCount: textBlocks.length,
			structuredBlockCount: structured.length,
			identityFieldsPresent: keys.every(key => typeof observed?.[key] === "string"),
			matches,
			roleMatched: probeDelivery?.role === role,
			sessionMatched: probeDelivery?.sessionId === ompSessionId,
			generationMatched: probeDelivery?.generation === generation,
		});
	});
	pi.on("after_provider_response", () => {
		sendEvent("provider_response_received");
		if (activeDelivery?.terminal === "pending" && activeDelivery.awaitingProviderResponse) {
			activeDelivery.providerResponseObserved = true;
			activeDelivery.awaitingProviderResponse = false;
			finishDeliveryEvidence();
		}
	});
	pi.on("message_end", (event: { message?: { role?: string; content?: unknown; stopReason?: unknown; errorMessage?: unknown }; willContinue?: unknown }) => {
		const delivery = activeDelivery;
		if (delivery?.terminal === "pending" && delivery.awaitingProviderResponse && event?.message?.role === "assistant"
			&& event.message.stopReason !== "error" && event.message.stopReason !== "aborted"
			&& !(typeof event.message.errorMessage === "string" && event.message.errorMessage.length > 0)) {
			// E1: OMP 18.4.5's openai-codex provider never emits after_provider_response (its transport does not call
			// onResponse). The assistant message that ends the matched provider request is that request's response.
			delivery.providerResponseObserved = true;
			delivery.awaitingProviderResponse = false;
		}
		if (delivery?.workerStage && delivery.terminal === "pending" && event?.message?.role === "assistant") {
			delivery.assistantMessageCount += 1;
			const content = event.message.content;
			// E1: an opaque leading thinking block (encrypted reasoning, no text) is not response content.
			let start = 0;
			while (Array.isArray(content) && start < content.length && isOpaqueThinking(content[start])) start += 1;
			const rest = Array.isArray(content) ? content.slice(start) : [];
			const text = rest.length === 1
				&& typeof rest[0] === "object" && rest[0] !== null
				&& rest[0].type === "text" && typeof rest[0].text === "string" ? rest[0].text as string : undefined;
			const response = text === undefined ? undefined : parseWorkerResponse(text, delivery);
			if (delivery.assistantMessageCount !== 1 || event.message.stopReason !== "stop"
				|| event.message.errorMessage || event.willContinue === true || !response) {
				delivery.workerResponseRejected = "invalid_assistant_response";
			} else {
				delivery.workerResponse = response;
			}
		}
		if (eventSurfaceProbe && event?.message?.role === "user") {
			const content = event.message.content;
			const text = typeof content === "string" ? content : Array.isArray(content)
				? content.filter((block): block is { type: string; text: string } =>
					typeof block === "object" && block !== null
					&& "type" in block && block.type === "text"
					&& "text" in block && typeof block.text === "string")
					.map(block => block.text).join("") : "";
			let observed: Record<string, unknown> | undefined;
			try {
				const parsed: unknown = JSON.parse(text);
				if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) observed = parsed as Record<string, unknown>;
			} catch { /* Only a structured injected user message can establish identity. */ }
			const keys = ["workbench_message_id", "workbench_delivery_attempt_id", "kind", "task_id", "revision_id", "run_id"];
			const matches = Object.fromEntries(keys.map(key => [key, observed !== undefined && probeDelivery !== undefined
				&& typeof observed[key] === "string" && observed[key] === probeDelivery.message[key]]));
			sendEvent("delivery_user_message_end_probe", {
				identityFieldsPresent: keys.every(key => typeof observed?.[key] === "string"),
				matches,
				roleMatched: probeDelivery?.role === role,
				sessionMatched: probeDelivery?.sessionId === ompSessionId,
				generationMatched: probeDelivery?.generation === generation,
			});
		}
		if (eventSurfaceProbe && event?.message?.role === "assistant") {
			const reason = event.message.stopReason;
			sendEvent("delivery_assistant_message_end_probe", {
				stopReason: reason === "stop" || reason === "length" || reason === "toolUse" || reason === "error" || reason === "aborted" ? reason : null,
				errorMessagePresent: typeof event.message.errorMessage === "string" && event.message.errorMessage.length > 0,
				willContinue: typeof event.willContinue === "boolean" ? event.willContinue : null,
			});
		}
		if (event?.message?.role === "assistant"
			&& (event.message.stopReason === "error" || event.message.stopReason === "aborted"
				|| (typeof event.message.errorMessage === "string" && event.message.errorMessage.length > 0))) {
			markDeliveryUnknown("assistant_message_ended_with_error_or_abort");
		}
		if (!expectedResponseMarker || event?.message?.role !== "assistant") return;
		const content = event.message.content;
		const text = Array.isArray(content)
			? content
				.filter((block): block is { type: string; text: string } =>
					typeof block === "object"
						&& block !== null
						&& "type" in block
						&& block.type === "text"
						&& "text" in block
						&& typeof block.text === "string")
				.map(block => block.text)
				.join("")
			: "";
		// Report only whether the one-time probe sentinel appeared. Never forward model text.
		sendEvent("assistant_message_end", { responseMarkerMatched: text.includes(expectedResponseMarker) });
		publishState();
	});
	pi.on("tool_approval_requested", (event: { toolCallId: string; toolName: string }) => {
		approvals.add(event.toolCallId);
		sendEvent("tool_approval_requested", { toolCallId: event.toolCallId, toolName: event.toolName });
		publishState();
	});
	pi.on("tool_approval_resolved", (event: { toolCallId: string; toolName: string; approved: boolean }) => {
		approvals.delete(event.toolCallId);
		sendEvent("tool_approval_resolved", {
			toolCallId: event.toolCallId,
			toolName: event.toolName,
			approved: event.approved,
		});
		publishState();
	});
	pi.on("tool_call", (event: { toolCallId: string; toolName: string }) => {
		if (activeDelivery?.workerStage) activeDelivery.workerResponseRejected = "tool_activity";
		// Native OMP approval and direct manual input keep their own tool path.
		sendEvent("tool_call_observed", { toolCallId: event.toolCallId, toolName: event.toolName });
		publishState();
	});
	pi.on("tool_execution_start", (event: { toolCallId: string; toolName: string }) => {
		if (activeDelivery?.workerStage) activeDelivery.workerResponseRejected = "tool_activity";
		executingCalls.add(event.toolCallId);
		if (abortStatus === "requested") unknownOutcomeCalls.add(event.toolCallId);
		sendEvent("tool_execution_start", { toolCallId: event.toolCallId, toolName: event.toolName });
		publishState();
	});
	pi.on("tool_execution_end", (event: { toolCallId: string; toolName: string }) => {
		executingCalls.delete(event.toolCallId);
		sendEvent("tool_execution_end", { toolCallId: event.toolCallId, toolName: event.toolName });
		publishState();
	});
	pi.on("before_agent_start", () => {
		// A prompt is being prepared for the provider; completion is observed separately.
		publishState();
	});
}
