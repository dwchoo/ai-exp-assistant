// CW-19 (C-D71 (1)/(2)/(4)): the bridge side of backend restart recovery.
// - stop_survivor is a manager-only tool with {survivor_id, reason} (strict, essential);
// - the manager accepts the backend_restarted notice type;
// - an assistant message's model outcome is reported as model_turn_result without any text
//   (error / an error message -> ok false; stop, length, toolUse -> ok true; aborted -> nothing).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import workbenchG3Extension, { modelTurnOutcome } from "../../omp_bridge/g3/bridge.ts";

const SOURCE = readFileSync(new URL("../../omp_bridge/g3/bridge.ts", import.meta.url), "utf8");

function register(role: "manager" | "worker") {
	const tools = new Map<string, any>();
	const saved = { ...process.env };
	Object.assign(process.env, { WORKBENCH_G3_BRIDGE_SOCKET: "/nonexistent/cw19-bridge.sock", WORKBENCH_G3_ROLE: role,
		WORKBENCH_G3_TOKEN: "t", WORKBENCH_G3_GENERATION: "1" });
	try {
		workbenchG3Extension({ registerTool: (tool: any) => tools.set(tool.name, tool), on: () => undefined,
			sendUserMessage: async () => undefined, setActiveTools: () => undefined, getActiveTools: () => [] });
	} finally {
		process.env = saved;
	}
	return tools;
}

test("stop_survivor is a manager-only tool with survivor_id and reason", () => {
	const manager = register("manager");
	const tool = manager.get("stop_survivor");
	assert.ok(tool, "registered for the manager");
	assert.equal(tool.strict, true);
	assert.equal(tool.loadMode, "essential");
	assert.deepEqual(tool.parameters.required, ["survivor_id", "reason"]);
	assert.deepEqual(Object.keys(tool.parameters.properties).sort(), ["reason", "survivor_id"]);
	assert.match(tool.description, /never|only a survivor whose identity is verified/i);
	assert.equal(register("worker").has("stop_survivor"), false);
});

test("the manager takes the backend_restarted notice", () => {
	assert.match(SOURCE, /manager: new Set\(\[[^\]]*"backend_restarted"/);
});

test("model outcome classes carry no text", () => {
	assert.deepEqual(modelTurnOutcome({ stopReason: "error", errorMessage: "secret provider text" }),
		{ ok: false, stopReason: "error" });
	assert.deepEqual(modelTurnOutcome({ stopReason: "stop", errorMessage: "x" }), { ok: false, stopReason: "error_message" });
	assert.deepEqual(modelTurnOutcome({ stopReason: "stop" }), { ok: true, stopReason: "stop" });
	assert.deepEqual(modelTurnOutcome({ stopReason: "toolUse" }), { ok: true, stopReason: "toolUse" });
	assert.deepEqual(modelTurnOutcome({ stopReason: "length" }), { ok: true, stopReason: "length" });
	assert.equal(modelTurnOutcome({ stopReason: "aborted", errorMessage: "aborted" }), undefined);
	assert.equal(modelTurnOutcome({}), undefined);
	assert.ok(!JSON.stringify(modelTurnOutcome({ stopReason: "error", errorMessage: "secret" })).includes("secret"));
});
