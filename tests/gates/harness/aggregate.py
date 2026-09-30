"""Fail-closed evidence aggregation; trust context is supplied by the verifier.

This validates provenance/coverage, not runtime semantics. The verifier must pin
the context from its frozen candidate, approvals and independently observed runs;
never manufacture the trust context from the submitted evidence records.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class EvidenceError(ValueError):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(records: list[dict], *, root: Path, expected: dict) -> list[str]:
    items = expected["items"]
    if not isinstance(items, dict) or not items or not expected.get("input_manifest") or not isinstance(expected.get("evidence"), dict) or set(expected["evidence"]) != set(items):
        raise EvidenceError("empty or incomplete required verifier context")
    if len(records) != len(items) or {r.get("item_id") for r in records} != set(items):
        raise EvidenceError("missing, duplicate or unowned gate items")
    for path, sha in expected["input_manifest"].items():
        target = (root / path).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file() or digest(target) != sha:
            raise EvidenceError("current integrated input changed")
    for record in records:
        item = items[record["item_id"]]
        for key in ("candidate_id", "requirements_digest", "approval_digest", "input_manifest"):
            if record.get(key) != expected[key]:
                raise EvidenceError(f"changed {key}")
        if record.get("owner_ticket") != item["owner_ticket"]:
            raise EvidenceError("gate ownership mismatch")
        required = item["required_evidence_level"]
        observed = record.get("observed_evidence_level")
        if required not in ("fixture", "runtime") or observed not in ("fixture", "runtime") or (required == "runtime" and observed != "runtime"):
            raise EvidenceError("insufficient evidence level")
        if record.get("required_evidence_level") != required:
            raise EvidenceError("requirements evidence level changed")
        if required == "runtime" and record.get("evidence_kind") != "actual_runtime":
            raise EvidenceError("fixture-only runtime evidence")
        if record.get("result") != "passed" or type(record.get("exit_code")) is not int or record["exit_code"] != 0 or record.get("unknowns") != []:
            raise EvidenceError("failed check or required unknown")
        if not isinstance(record.get("command"), list) or not record["command"] or any(not isinstance(v, str) or not v for v in record["command"]) or record.get("cwd") != str(root.resolve()) or not isinstance(record.get("environment"), dict):
            raise EvidenceError("missing invocation evidence")
        evidence = record.get("evidence_ref")
        pinned = expected["evidence"].get(record["item_id"])
        if not isinstance(evidence, dict) or evidence != pinned or set(evidence) != {"path", "sha256"}:
            raise EvidenceError("unpinned evidence")
        target = (root / evidence["path"]).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file() or digest(target) != evidence["sha256"]:
            raise EvidenceError("evidence content changed/missing")
        content = target.read_bytes().strip()
        if not content:
            raise EvidenceError("empty pinned evidence")
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            # Nonempty independently pinned command logs remain provenance,
            # not an inferred semantic runtime assertion.
            continue
        if isinstance(payload, dict) and (not payload or not set(payload).difference({"schemaVersion", "schema_version", "$schema", "schema", "version"})):
            raise EvidenceError("schema-only pinned evidence")
        if payload is None or payload in ([], "", False):
            raise EvidenceError("empty pinned observation")
        if isinstance(payload, dict) and "fixture" in payload and expected.get("validation_mode") != "provenance_only_fixture":
            raise EvidenceError("synthetic evidence requires explicit provenance-only fixture mode")
    return sorted(items)
