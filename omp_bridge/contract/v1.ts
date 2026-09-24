/** Version 1 control envelope mirrored by workbench.contracts.v1. */

export const WIRE_VERSION = 1 as const;

export type ActorRole = "manager" | "worker" | "workbench" | "host_shell";
export type MessageKind = "task" | "question" | "answer" | "report";
export type CommandState = "accepted" | "started" | "completed" | "unknown";
export type PaneId = "manager_omp" | "worker_omp" | "host_shell";
export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export type MessageEvent = {
	type: "message";
	messageKind: MessageKind;
	payload: { [key: string]: JsonValue };
	inReplyToMessageId?: string;
};

export type CommandStatusEvent = {
	type: "commandStatus";
	commandId: string;
	state: CommandState;
	exitCode?: number;
};

export type ControlEnvelope = {
	schemaVersion: typeof WIRE_VERSION;
	messageId: string;
	deliveryAttemptId: string;
	senderRole: ActorRole;
	sessionId: string;
	sessionGeneration: number;
	taskId?: string;
	revisionId?: string;
	runId?: string;
	event: MessageEvent | CommandStatusEvent;
};

/** Raw PTY bytes are a distinct runtime value, never JSON control payload. */
export type DisplayChunk = {
	sessionId: string;
	sessionGeneration: number;
	paneId: PaneId;
	sequence: number;
	data: Uint8Array;
};

export class ContractError extends Error {}

const roles = new Set<ActorRole>(["manager", "worker", "workbench", "host_shell"]);
const messageKinds = new Set<MessageKind>(["task", "question", "answer", "report"]);
const commandStates = new Set<CommandState>(["accepted", "started", "completed", "unknown"]);
const panes = new Set<PaneId>(["manager_omp", "worker_omp", "host_shell"]);
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function record(value: unknown, field: string): Record<string, unknown> {
	if (value === null || typeof value !== "object" || Array.isArray(value)) {
		throw new ContractError(`${field} must be an object`);
	}
	const prototype = Object.getPrototypeOf(value);
	if (prototype !== Object.prototype && prototype !== null) {
		throw new ContractError(`${field} must be a plain object`);
	}
	return value as Record<string, unknown>;
}

function exactKeys(value: Record<string, unknown>, required: string[], optional: string[] = []): void {
	const allowed = new Set([...required, ...optional]);
	for (const key of required) {
		if (!(key in value)) throw new ContractError(`missing field: ${key}`);
	}
	for (const key of Object.keys(value)) {
		if (!allowed.has(key)) throw new ContractError(`unsupported field: ${key}`);
	}
}

function identifier(value: unknown, field: string): string {
	if (typeof value !== "string" || !uuidPattern.test(value)) {
		throw new ContractError(`${field} must be a canonical lowercase UUID string`);
	}
	return value;
}

function positiveInteger(value: unknown, field: string): number {
	if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
		throw new ContractError(`${field} must be a positive safe integer`);
	}
	return value;
}

function jsonObject(value: unknown, field: string): { [key: string]: JsonValue } {
	const normalized = normalizeJsonValue(value, field);
	if (normalized === null || typeof normalized !== "object" || Array.isArray(normalized)) {
		throw new ContractError(`${field} must be an object`);
	}
	return normalized as { [key: string]: JsonValue };
}

function normalizeJsonValue(value: unknown, field: string): JsonValue {
	if (value === null || typeof value === "string" || typeof value === "boolean") return value;
	if (typeof value === "number") {
		if (!Number.isFinite(value)) throw new ContractError(`${field} must be a finite JSON number`);
		if (Number.isInteger(value) && !Number.isSafeInteger(value)) {
			throw new ContractError(`${field} contains an integer-valued number outside the shared safe range`);
		}
		return value;
	}
	if (Array.isArray(value)) {
		return Array.from(value, (child, index) => normalizeJsonValue(child, `${field}[${index}]`));
	}
	if (typeof value === "object") {
		const data = record(value, field);
		return Object.fromEntries(
			Object.entries(data).map(([key, child]) => [key, normalizeJsonValue(child, `${field}.${key}`)]),
		);
	}
	throw new ContractError(`${field} must contain JSON-compatible values`);
}

export function parseControlEnvelope(
	input: unknown,
	expectedSession?: { sessionId: string; sessionGeneration: number },
): ControlEnvelope {
	const data = record(input, "control envelope");
	exactKeys(
		data,
		["schemaVersion", "messageId", "deliveryAttemptId", "senderRole", "sessionId", "sessionGeneration", "event"],
		["taskId", "revisionId", "runId"],
	);
	if (data.schemaVersion !== WIRE_VERSION) throw new ContractError(`unsupported schemaVersion: ${String(data.schemaVersion)}`);
	if (typeof data.senderRole !== "string" || !roles.has(data.senderRole as ActorRole)) {
		throw new ContractError(`unsupported senderRole: ${String(data.senderRole)}`);
	}
	const messageId = identifier(data.messageId, "messageId");
	const deliveryAttemptId = identifier(data.deliveryAttemptId, "deliveryAttemptId");
	const sessionId = identifier(data.sessionId, "sessionId");
	const sessionGeneration = positiveInteger(data.sessionGeneration, "sessionGeneration");
	for (const key of ["taskId", "revisionId", "runId"] as const) {
		if (data[key] !== undefined) identifier(data[key], key);
	}
	if (expectedSession !== undefined) {
		identifier(expectedSession.sessionId, "expectedSession.sessionId");
		positiveInteger(expectedSession.sessionGeneration, "expectedSession.sessionGeneration");
		if (sessionId !== expectedSession.sessionId || sessionGeneration !== expectedSession.sessionGeneration) {
			throw new ContractError("control envelope does not match the expected session generation");
		}
	}

	const eventData = record(data.event, "event");
	let event: MessageEvent | CommandStatusEvent;
	if (eventData.type === "message") {
		exactKeys(eventData, ["type", "messageKind", "payload"], ["inReplyToMessageId"]);
		if (typeof eventData.messageKind !== "string" || !messageKinds.has(eventData.messageKind as MessageKind)) {
			throw new ContractError(`unsupported messageKind: ${String(eventData.messageKind)}`);
		}
		const replyTo = eventData.inReplyToMessageId === undefined
			? undefined
			: identifier(eventData.inReplyToMessageId, "inReplyToMessageId");
		event = {
			type: "message",
			messageKind: eventData.messageKind as MessageKind,
			payload: jsonObject(eventData.payload, "payload"),
			...(replyTo === undefined ? {} : { inReplyToMessageId: replyTo }),
		};
	} else if (eventData.type === "commandStatus") {
		exactKeys(eventData, ["type", "commandId", "state"], ["exitCode"]);
		const commandId = identifier(eventData.commandId, "commandId");
		if (typeof eventData.state !== "string" || !commandStates.has(eventData.state as CommandState)) {
			throw new ContractError(`unsupported command state: ${String(eventData.state)}`);
		}
		if (eventData.exitCode !== undefined) {
			if (!Number.isSafeInteger(eventData.exitCode) || eventData.state !== "completed") {
				throw new ContractError("exitCode must be an integer on a completed command");
			}
		}
		event = {
			type: "commandStatus",
			commandId,
			state: eventData.state as CommandState,
			...(eventData.exitCode === undefined ? {} : { exitCode: eventData.exitCode as number }),
		};
	} else {
		throw new ContractError(`unsupported event type: ${String(eventData.type)}`);
	}

	return {
		schemaVersion: WIRE_VERSION,
		messageId,
		deliveryAttemptId,
		senderRole: data.senderRole as ActorRole,
		sessionId,
		sessionGeneration,
		...(data.taskId === undefined ? {} : { taskId: data.taskId as string }),
		...(data.revisionId === undefined ? {} : { revisionId: data.revisionId as string }),
		...(data.runId === undefined ? {} : { runId: data.runId as string }),
		event,
	};
}

export function serializeControlEnvelope(input: unknown): string {
	return JSON.stringify(parseControlEnvelope(input));
}

export function validateDisplayChunk(input: unknown): DisplayChunk {
	const data = record(input, "display chunk");
	exactKeys(data, ["sessionId", "sessionGeneration", "paneId", "sequence", "data"]);
	const sessionId = identifier(data.sessionId, "sessionId");
	const sessionGeneration = positiveInteger(data.sessionGeneration, "sessionGeneration");
	const sequence = positiveInteger(data.sequence, "sequence");
	if (typeof data.paneId !== "string" || !panes.has(data.paneId as PaneId)) {
		throw new ContractError(`unsupported paneId: ${String(data.paneId)}`);
	}
	if (!(data.data instanceof Uint8Array)) throw new ContractError("display data must be raw Uint8Array bytes");
	return { sessionId, sessionGeneration, paneId: data.paneId as PaneId, sequence, data: data.data };
}
