"""Versioned G1-G4 observation ports, separate from the preserved v1 envelope."""
from __future__ import annotations

import copy
import re
from typing import TypedDict
from .v1 import ContractError, ControlEnvelope, _identifier, _positive_integer


class ShellControl(TypedDict):
    parentPid: int
    generation: int
    ownerEpoch: int
    requestId: str
    approvalHash: str
    phase: str


class Takeover(TypedDict):
    ownerEpoch: int
    inputTarget: int | None
    alreadyDeliveredCancelled: bool


class DeliveryObservation(TypedDict):
    envelope: dict
    stage: str


class AutomationState(TypedDict):
    paused: bool
    cancelled: bool
    metadataHealthy: bool
    approvalValid: bool


class ResumeEvidence(TypedDict):
    userResume: bool
    taskId: str
    runId: str
    approvalHash: str
    filesMatch: bool
    processesMatch: bool
    toolsMatch: bool
    taskMatch: bool
    approvalMatch: bool
    checkedAt: str
    unknowns: list[str]


FIELDS = {
    "ShellControl": {"parentPid", "generation", "ownerEpoch", "requestId", "approvalHash", "phase"},
    "Takeover": {"ownerEpoch", "inputTarget", "alreadyDeliveredCancelled"},
    "DeliveryObservation": {"envelope", "stage"},
    "AutomationState": {"paused", "cancelled", "metadataHealthy", "approvalValid"},
    "ResumeEvidence": {"userResume", "taskId", "runId", "approvalHash", "filesMatch", "processesMatch", "toolsMatch", "taskMatch", "approvalMatch", "checkedAt", "unknowns"},
}
PHASES = {"accepted", "supervisor_started", "experiment_started", "main_returned", "lifetime_ended", "input_returned", "control_returned", "unknown"}
STAGES = {"local_received", "api_returned", "omp_processed", "structured_report", "business_result", "unknown"}


def parse_port(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"portVersion", "kind", "payload"} or type(value["portVersion"]) is not int or value["portVersion"] != 2:
        raise ContractError("unsupported port version/fields")
    kind, payload = value["kind"], value["payload"]
    if not isinstance(kind, str) or kind not in FIELDS or not isinstance(payload, dict) or set(payload) != FIELDS[kind]:
        raise ContractError("unsupported port kind/fields")
    for key, item in payload.items():
        if key in {"parentPid", "generation", "ownerEpoch"}:
            _positive_integer(item, key)
        elif key in {"requestId", "taskId", "runId"}:
            _identifier(item, key)
        elif key == "approvalHash":
            if not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None:
                raise ContractError("approvalHash must be SHA256")
        elif key == "inputTarget":
            if item is not None:
                _positive_integer(item, key)
        elif key == "phase":
            if not isinstance(item, str) or item not in PHASES:
                raise ContractError("unsupported shell phase")
        elif key == "stage":
            if not isinstance(item, str) or item not in STAGES:
                raise ContractError("unsupported delivery observation")
        elif key == "envelope":
            ControlEnvelope.from_dict(item)
        elif key == "checkedAt":
            if not isinstance(item, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", item) is None:
                raise ContractError("checkedAt must be UTC timestamp")
        elif key == "unknowns":
            if not isinstance(item, list) or any(not isinstance(v, str) or not v for v in item):
                raise ContractError("unknowns must be explicit strings")
        elif type(item) is not bool:
            raise ContractError("boolean expected")
    if kind == "Takeover" and payload["alreadyDeliveredCancelled"]:
        raise ContractError("takeover cannot cancel already delivered work")
    return copy.deepcopy(value)


def adapt_v1_delivery(envelope: object) -> dict:
    return parse_port({"portVersion": 2, "kind": "DeliveryObservation", "payload": {
        "envelope": ControlEnvelope.from_dict(envelope).to_dict(), "stage": "local_received"}})


def dispatch_allowed(port: object) -> bool:
    value = parse_port(port)
    if value["kind"] != "AutomationState":
        raise ContractError("AutomationState required")
    state = value["payload"]
    return not state["paused"] and not state["cancelled"] and state["metadataHealthy"] and state["approvalValid"]


def resume_allowed(port: object) -> bool:
    value = parse_port(port)
    if value["kind"] != "ResumeEvidence":
        raise ContractError("ResumeEvidence required")
    evidence = value["payload"]
    return evidence["userResume"] and not evidence["unknowns"] and all(evidence[key] for key in ("filesMatch", "processesMatch", "toolsMatch", "taskMatch", "approvalMatch"))
