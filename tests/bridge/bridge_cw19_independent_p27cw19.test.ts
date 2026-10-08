// Independent CW-19 bridge checks (p27-cw19-test-01), from C-D71 (1)/(4) and root-accepted decision 3:
// - modelTurnOutcome: error or a non-empty error message -> ok false; stop/length/toolUse without an error -> ok
//   true; aborted (user or pause abort) -> nothing, even with an error message; anything else -> nothing; the
//   outcome carries only {ok, stopReason} (never message text);
// - stop_survivor is registered for the manager only, strict, with survivor_id (<= 16) and reason required.
import assert from "node:assert/strict";
import { test } from "node:test";
import workbenchG3Extension, { modelTurnOutcome } from "../../omp_bridge/g3/bridge.ts";

function register(role: "manager" | "worker") {
	const tools = new Map<string, any>();
	const saved = { ...process.env };
	Object.assign(process.env, { WORKBENCH_G3_BRIDGE_SOCKET: "/nonexistent/cw19-indep.sock", WORKBENCH_G3_ROLE: role,
		WORKBENCH_G3_TOKEN: "t", WORKBENCH_G3_GENERATION: "1" });
	try {
		workbenchG3Extension({ registerTool: (tool: any) => tools.set(tool.name, tool), on: () => undefined,
			sendUserMessage: async () => undefined, setActiveTools: () => undefined, getActiveTools: () => [] });
	} finally {
		process.env = saved;
	}
	return tools;
}

test("C-D71 (4): model outcome classes", () => {
	const SECRET = "provider said: invalid key sk-should-never-travel";
	const cases: Array<[Record<string, unknown>, unknown]> = [
		[{ stopReason: "error" }, { ok: false, stopReason: "error" }],
		[{ stopReason: "error", errorMessage: SECRET }, { ok: false, stopReason: "error" }],
		[{ stopReason: "stop", errorMessage: SECRET }, { ok: false, stopReason: "error_message" }],
		[{ stopReason: "toolUse", errorMessage: SECRET }, { ok: false, stopReason: "error_message" }],
		[{ errorMessage: SECRET }, { ok: false, stopReason: "error_message" }],
		[{ stopReason: "stop" }, { ok: true, stopReason: "stop" }],
		[{ stopReason: "length" }, { ok: true, stopReason: "length" }],
		[{ stopReason: "toolUse" }, { ok: true, stopReason: "toolUse" }],
		[{ stopReason: "stop", errorMessage: "" }, { ok: true, stopReason: "stop" }],
		[{ stopReason: "aborted" }, undefined],
		[{ stopReason: "aborted", errorMessage: SECRET }, undefined],
		[{ stopReason: "weird" }, undefined],
		[{}, undefined],
	];
	for (const [message, expected] of cases) {
		const outcome = modelTurnOutcome(message as any);
		assert.deepEqual(outcome, expected, JSON.stringify(message));
		if (outcome !== undefined) {
			assert.deepEqual(Object.keys(outcome).sort(), ["ok", "stopReason"]);
			assert.ok(!JSON.stringify(outcome).includes("sk-should-never-travel"));
		}
	}
	assert.equal(modelTurnOutcome(undefined as any), undefined);
});

test("C-D71 (1): stop_survivor for the manager only, strict arguments", () => {
	const manager = register("manager");
	const worker = register("worker");
	assert.ok(!worker.has("stop_survivor"), "the worker must not end survivors");
	const tool = manager.get("stop_survivor");
	assert.ok(tool);
	assert.equal(tool.parameters.additionalProperties, false);
	assert.deepEqual([...tool.parameters.required].sort(), ["reason", "survivor_id"]);
	assert.equal(tool.parameters.properties.survivor_id.type, "string");
	assert.equal(tool.parameters.properties.survivor_id.maxLength, 16);
	assert.equal(tool.parameters.properties.reason.type, "string");
	assert.ok(tool.parameters.properties.reason.minLength >= 1);
	assert.match(tool.description, /verif/i);
});
