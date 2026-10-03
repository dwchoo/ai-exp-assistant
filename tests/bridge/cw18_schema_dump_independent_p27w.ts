// p27-cw18-test-01: print the bridge extension's registered tool for each role as JSON (no socket is opened).
import workbenchG3Extension from "../../omp_bridge/g3/bridge.ts";

const out: Record<string, unknown> = {};
for (const role of ["manager", "worker"]) {
	process.env.WORKBENCH_G3_BRIDGE_SOCKET = "/nonexistent/p27w-schema-dump.sock";
	process.env.WORKBENCH_G3_ROLE = role;
	process.env.WORKBENCH_G3_TOKEN = "p27w-dump-token";
	process.env.WORKBENCH_G3_GENERATION = "1";
	const tools: unknown[] = [];
	workbenchG3Extension({ on() {}, registerTool(tool: Record<string, unknown>) { tools.push({ ...tool, execute: typeof tool.execute }); },
		async sendUserMessage() {}, logger: { error() {} } });
	out[role] = tools;
}
process.stdout.write(JSON.stringify(out));
