import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { parsePort, adaptV1Delivery, dispatchAllowed, resumeAllowed } from "../../../omp_bridge/contract/ports_v2.ts";
const values = JSON.parse(readFileSync(new URL("../../contracts/fixtures/ports-v2.json", import.meta.url), "utf8"));
test("shared five-port fixtures and v1 envelope compatibility", () => {
    assert.deepEqual(values.map(parsePort), values);
    assert.equal(adaptV1Delivery(values[2].payload.envelope).payload.stage, "local_received");
    assert.equal(dispatchAllowed(values[3]), true); assert.equal(resumeAllowed(values[4]), true);
});
test("unsupported old ports, lost ownership and unknown reconciliation cannot pass", () => {
    for (const value of values) for (const portVersion of [0, 1, 3, true]) assert.throws(() => parsePort({...value, portVersion}));
    assert.throws(() => parsePort({...values[1], payload: {...values[1].payload, alreadyDeliveredCancelled:true}}));
    assert.equal(dispatchAllowed({...values[3],payload:{...values[3].payload,paused:true}}),false);
    assert.equal(resumeAllowed({...values[4],payload:{...values[4].payload,unknowns:["tool"]}}),false);
});
test("comma-containing unknown key cannot replace two required ShellControl keys", () => {
    const value = structuredClone(values[0]);
    delete value.payload.generation;
    delete value.payload.ownerEpoch;
    value.payload["generation,ownerEpoch"] = true;
    assert.throws(() => parsePort(value), /unsupported fields/);
});
