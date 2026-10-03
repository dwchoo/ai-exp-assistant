import { ContractError, parseControlEnvelope } from "./v1.ts";

export interface ShellControl { parentPid: number; generation: number; ownerEpoch: number; requestId: string; approvalHash: string; phase: string }
export interface Takeover { ownerEpoch: number; inputTarget: number | null; alreadyDeliveredCancelled: false }
export interface DeliveryObservation { envelope: unknown; stage: string }
export interface AutomationState { paused: boolean; cancelled: boolean; metadataHealthy: boolean; approvalValid: boolean }
export interface ResumeEvidence { userResume: boolean; taskId: string; runId: string; approvalHash: string; filesMatch: boolean; processesMatch: boolean; toolsMatch: boolean; taskMatch: boolean; approvalMatch: boolean; checkedAt: string; unknowns: string[] }
const fields: Record<string, string[]> = {
    ShellControl: ["parentPid", "generation", "ownerEpoch", "requestId", "approvalHash", "phase"],
    Takeover: ["ownerEpoch", "inputTarget", "alreadyDeliveredCancelled"],
    DeliveryObservation: ["envelope", "stage"],
    AutomationState: ["paused", "cancelled", "metadataHealthy", "approvalValid"],
    ResumeEvidence: ["userResume", "taskId", "runId", "approvalHash", "filesMatch", "processesMatch", "toolsMatch", "taskMatch", "approvalMatch", "checkedAt", "unknowns"],
};
function record(value: unknown): Record<string, any> {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new ContractError("object required");
    return value as Record<string, any>;
}
function exact(value: Record<string, any>, keys: string[]): void {
    if (Object.keys(value).length !== keys.length || !keys.every(key => Object.hasOwn(value, key))) throw new ContractError("unsupported fields");
}
function positive(value: unknown): void {
    if (!Number.isSafeInteger(value) || (value as number) < 1) throw new ContractError("positive integer required");
}
export function parsePort(input: unknown): { portVersion: 2; kind: string; payload: Record<string, any> } {
    const value = record(input); exact(value, ["portVersion", "kind", "payload"]);
    if (value.portVersion !== 2 || typeof value.kind !== "string" || !Object.hasOwn(fields, value.kind)) throw new ContractError("unsupported port version/kind");
    const payload = record(value.payload); exact(payload, fields[value.kind]);
    for (const [key, item] of Object.entries(payload)) {
        if (["parentPid", "generation", "ownerEpoch"].includes(key)) positive(item);
        else if (["requestId", "taskId", "runId"].includes(key)) {
            if (typeof item !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(item)) throw new ContractError("canonical UUID required");
        } else if (key === "approvalHash") {
            if (typeof item !== "string" || !/^[0-9a-f]{64}$/.test(item)) throw new ContractError("SHA256 required");
        } else if (key === "inputTarget") { if (item !== null) positive(item); }
        else if (key === "phase") { if (!["accepted", "supervisor_started", "experiment_started", "main_returned", "lifetime_ended", "input_returned", "control_returned", "unknown"].includes(item)) throw new ContractError("unsupported phase"); }
        else if (key === "stage") { if (!["local_received", "api_returned", "omp_processed", "structured_report", "business_result", "unknown"].includes(item)) throw new ContractError("unsupported stage"); }
        else if (key === "envelope") parseControlEnvelope(item);
        else if (key === "checkedAt") { if (typeof item !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(item)) throw new ContractError("UTC timestamp required"); }
        else if (key === "unknowns") { if (!Array.isArray(item) || item.some(v => typeof v !== "string" || !v)) throw new ContractError("explicit unknowns required"); }
        else if (typeof item !== "boolean") throw new ContractError("boolean required");
    }
    if (value.kind === "Takeover" && payload.alreadyDeliveredCancelled) throw new ContractError("takeover cannot cancel delivered work");
    return structuredClone(value) as { portVersion: 2; kind: string; payload: Record<string, any> };
}
export function adaptV1Delivery(envelope: unknown) { return parsePort({portVersion: 2, kind: "DeliveryObservation", payload: {envelope: parseControlEnvelope(envelope), stage: "local_received"}}); }
export function dispatchAllowed(input: unknown): boolean {
    const port = parsePort(input); if (port.kind !== "AutomationState") throw new ContractError("AutomationState required");
    const p = port.payload; return !p.paused && !p.cancelled && p.metadataHealthy && p.approvalValid;
}
export function resumeAllowed(input: unknown): boolean {
    const port = parsePort(input); if (port.kind !== "ResumeEvidence") throw new ContractError("ResumeEvidence required");
    const p = port.payload; return p.userResume && !p.unknowns.length && ["filesMatch", "processesMatch", "toolsMatch", "taskMatch", "approvalMatch"].every(k => p[k]);
}
