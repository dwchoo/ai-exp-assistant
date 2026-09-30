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
		const payload = (event as Record<string, unknown>).payload;
		if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return false;
		const messages = (payload as Record<string, unknown>).messages;
		if (!Array.isArray(messages)) return false;
		const last = messages.at(-1);
		if (typeof last !== "object" || last === null) return false;
		const lastMessage = last as Record<string, unknown>;
		if (lastMessage.role !== "user") return false;
		const content = lastMessage.content;
		const blocks = typeof content === "string" ? [content]
			: Array.isArray(content) ? content.filter((block): block is { type: string; text: string } =>
				typeof block === "object" && block !== null && "type" in block && block.type === "text"
				&& "text" in block && typeof block.text === "string").map(block => block.text)
			: [];
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
			if (isCurrentConnection(client, sessionId, sessionGeneration)) {
				if (activeDelivery?.workerStage) markDeliveryUnknown("bridge_disconnected");
				connected = false;
				pi.logger?.error?.("workbench G3 bridge disconnected", error);
			}
		});
		client.on("close", () => {
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
		const payloadKeys = ["messages", "model", "stream", "tools", "temperature"]
			.filter(key => request !== undefined && Object.hasOwn(request, key));
		const messages = Array.isArray(request?.messages) ? request.messages : undefined;
		const last = messages?.at(-1);
		const lastMessage = typeof last === "object" && last !== null ? last as Record<string, unknown> : undefined;
		const lastRole = lastMessage?.role;
		const content = lastMessage?.content;
		const contentShape = typeof content === "string" ? "string" : Array.isArray(content) ? "blocks" : "other";
		const textBlocks = Array.isArray(content)
			? content.filter((block): block is { type: string; text: string } =>
				typeof block === "object" && block !== null
				&& "type" in block && block.type === "text"
				&& "text" in block && typeof block.text === "string")
				.map(block => block.text) : [];
		const candidateTexts = typeof content === "string" ? [content] : textBlocks;
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
		if (delivery?.workerStage && delivery.terminal === "pending" && event?.message?.role === "assistant") {
			delivery.assistantMessageCount += 1;
			const content = event.message.content;
			const text = Array.isArray(content) && content.length === 1
				&& typeof content[0] === "object" && content[0] !== null
				&& content[0].type === "text" && typeof content[0].text === "string" ? content[0].text : undefined;
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
