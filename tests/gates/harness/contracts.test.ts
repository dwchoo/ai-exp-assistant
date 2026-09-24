import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { ContractError, parseControlEnvelope, serializeControlEnvelope, validateDisplayChunk } from "../../../omp_bridge/contract/v1.ts";

const fixtureDirectory = new URL("../../contracts/fixtures/", import.meta.url);

async function fixture(name: string): Promise<unknown> {
	return JSON.parse(await readFile(new URL(name, fixtureDirectory), "utf8")) as unknown;
}

test("Python golden control envelope parses and matches the bound session generation", async () => {
	const value = await fixture("control-envelope-v1.json");
	const parsed = parseControlEnvelope(value, {
		sessionId: "30000000-0000-4000-8000-000000000001",
		sessionGeneration: 2,
	});
	assert.deepEqual(parsed, value);
	assert.equal(parsed.schemaVersion, 1);
	assert.equal(parsed.event.type, "commandStatus");
	if (parsed.event.type === "commandStatus") {
		assert.equal(parsed.event.state, "completed");
		assert.equal(parsed.event.exitCode, 0);
	}
});

test("logical message ID is stable while each delivery has its own attempt ID", async () => {
	const [first, retry] = await fixture("delivery-attempts-v1.json") as Record<string, unknown>[];
	const parsedFirst = parseControlEnvelope(first);
	const parsedRetry = parseControlEnvelope(retry);
	assert.equal(parsedFirst.messageId, parsedRetry.messageId);
	assert.notEqual(parsedFirst.deliveryAttemptId, parsedRetry.deliveryAttemptId);
	assert.deepEqual(parsedFirst.event, parsedRetry.event);
});

test("rejects unsupported versions, roles, identifiers, unknown fields, and sessions", async () => {
	const value = await fixture("control-envelope-v1.json") as Record<string, unknown>;
	const reject = (candidate: unknown, expected?: { sessionId: string; sessionGeneration: number }) => {
		assert.throws(() => parseControlEnvelope(candidate, expected), ContractError);
	};
	reject({ ...value, schemaVersion: 2 });
	reject({ ...value, senderRole: "orchestrator" });
	reject({ ...value, messageId: "invalid" });
	reject({ ...value, extra: true });
	reject(value, { sessionId: "30000000-0000-4000-8000-000000000002", sessionGeneration: 2 });
	reject(value, { sessionId: "30000000-0000-4000-8000-000000000001", sessionGeneration: 3 });
});

test("keeps accepted, started, completed, and unknown as distinct states", async () => {
	const value = await fixture("control-envelope-v1.json") as Record<string, any>;
	for (const state of ["accepted", "started", "completed", "unknown"]) {
		const event = { ...value.event, state };
		if (state !== "completed") delete event.exitCode;
		assert.equal(parseControlEnvelope({ ...value, event }).event.state, state);
	}
	assert.throws(
		() => parseControlEnvelope({ ...value, event: { ...value.event, state: "started" } }),
		ContractError,
	);
});

test("keeps PTY display data as raw bytes outside the control envelope", async () => {
	const display = validateDisplayChunk({
		sessionId: "30000000-0000-4000-8000-000000000001",
		sessionGeneration: 2,
		paneId: "host_shell",
		sequence: 1,
		data: new Uint8Array([0x1b, 0x5b, 0x33, 0x31, 0x6d, 0xff, 0x00]),
	});
	assert.deepEqual(Array.from(display.data), [0x1b, 0x5b, 0x33, 0x31, 0x6d, 0xff, 0x00]);
	assert.throws(() => validateDisplayChunk({ ...display, data: "text" }), ContractError);
	assert.throws(() => parseControlEnvelope(display), ContractError);
});

test("rejects non-JSON values nested inside a message payload", async () => {
	const [value] = await fixture("delivery-attempts-v1.json") as Record<string, any>[];
	const event = { ...value.event, payload: { ...value.event.payload, metadata: new Date(0) } };
	assert.throws(() => parseControlEnvelope({ ...value, event }), ContractError);
});

test("rejects explicit null for optional wire fields", async () => {
	const command = await fixture("control-envelope-v1.json") as Record<string, any>;
	const [message] = await fixture("delivery-attempts-v1.json") as Record<string, any>[];
	const cases = [
		{ ...command, taskId: null },
		{ ...command, event: { ...command.event, exitCode: null } },
		{ ...message, event: { ...message.event, inReplyToMessageId: null } },
	];
	for (const candidate of cases) {
		assert.throws(() => parseControlEnvelope(candidate), ContractError);
	}
});

test("session generation fits the shared JS safe integer range", async () => {
	const value = await fixture("control-envelope-v1.json") as Record<string, unknown>;
	const maximum = Number.MAX_SAFE_INTEGER;
	assert.equal(parseControlEnvelope({ ...value, sessionGeneration: maximum }).sessionGeneration, maximum);
	assert.throws(() => parseControlEnvelope({ ...value, sessionGeneration: maximum + 1 }), ContractError);
});

test("command exit code fits the shared safe integer range", async () => {
	const value = await fixture("control-envelope-v1.json") as Record<string, any>;
	const maximum = Number.MAX_SAFE_INTEGER;
	for (const exitCode of [maximum, -maximum]) {
		const parsed = parseControlEnvelope({ ...value, event: { ...value.event, exitCode } });
		assert.equal(parsed.event.type, "commandStatus");
		if (parsed.event.type === "commandStatus") assert.equal(parsed.event.exitCode, exitCode);
	}
	for (const exitCode of [maximum + 1, -(maximum + 1), maximum + 3]) {
		assert.throws(() => parseControlEnvelope({ ...value, event: { ...value.event, exitCode } }), ContractError);
	}
});

test("nested payload numbers preserve the shared numeric boundary", async () => {
	const [value] = await fixture("delivery-attempts-v1.json") as Record<string, any>[];
	const maximum = Number.MAX_SAFE_INTEGER;
	const numbers = { positive: maximum, negative: -maximum, fraction: 1.25, largeId: "9007199254740993" };
	const parsed = parseControlEnvelope({ ...value, event: { ...value.event, payload: { numbers } } });
	assert.equal(parsed.event.type, "message");
	if (parsed.event.type === "message") assert.deepEqual(parsed.event.payload.numbers, numbers);
	for (const unsafe of [maximum + 1, -(maximum + 1), maximum + 3]) {
		assert.throws(
			() => parseControlEnvelope({ ...value, event: { ...value.event, payload: { numbers: [1.25, unsafe] } } }),
			ContractError,
		);
	}
	const roundedFromJson = JSON.parse("9007199254740993") as number;
	assert.equal(roundedFromJson, maximum + 1);
	assert.throws(
		() => parseControlEnvelope({ ...value, event: { ...value.event, payload: { number: roundedFromJson } } }),
		ContractError,
	);
});

test("PTY display counters fit the shared safe integer range", () => {
	const maximum = Number.MAX_SAFE_INTEGER;
	const display = {
		sessionId: "30000000-0000-4000-8000-000000000001",
		sessionGeneration: maximum,
		paneId: "host_shell",
		sequence: maximum,
		data: new Uint8Array([0xff]),
	};
	assert.deepEqual(validateDisplayChunk(display).data, display.data);
	for (const field of ["sessionGeneration", "sequence"] as const) {
		assert.throws(() => validateDisplayChunk({ ...display, [field]: maximum + 1 }), ContractError);
	}
});

test("control writer preserves validated payload and cannot emit a hidden unsafe number", async () => {
	const [message] = await fixture("delivery-attempts-v1.json") as Record<string, any>[];
	const ordinary = parseControlEnvelope(message);
	assert.deepEqual(parseControlEnvelope(JSON.parse(serializeControlEnvelope(message))), ordinary);

	const payload = { value: 7 };
	Object.defineProperty(payload, "toJSON", {
		enumerable: false,
		value: () => ({ value: Number.MAX_SAFE_INTEGER + 1 }),
	});
	const input = { ...message, event: { ...message.event, payload } };
	const validated = parseControlEnvelope(input);
	assert.equal(validated.event.type, "message");
	if (validated.event.type === "message") assert.equal(validated.event.payload.value, 7);

	let wire: string;
	try {
		wire = serializeControlEnvelope(input);
	} catch (error) {
		assert.ok(error instanceof ContractError);
		return;
	}
	const reparsed = parseControlEnvelope(JSON.parse(wire));
	assert.equal(reparsed.event.type, "message");
	if (reparsed.event.type === "message") assert.deepEqual(reparsed.event.payload, { value: 7 });
});
