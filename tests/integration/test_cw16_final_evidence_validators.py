"""CW-16 G4: final evidence validators accept the genuine final set and reject each mutation.

Two validators are exercised against copies of the final records placed under a fresh
``/tmp/wb-cw16-g4-*`` directory (the originals are never modified):

* the workflow gate (``.agents/skills/workflow-ledger/scripts/workflow_tools.py gate``), run
  as a subprocess; the genuine set must exit 0 with ``{"passed": true, "issues": []}`` and every
  mutation must exit 1 with exactly the expected issue list (ids, check ids and kinds);
* ``tests/gates/harness/aggregate.py validate()`` (G4-EVIDENCE); the genuine set must return the
  seven CW-16 item ids and every mutation must raise ``EvidenceError`` with the exact message.

``FinalRecordsTests`` runs only when env ``WB_CW16_FINAL_REQUEST`` names the request JSON below;
otherwise it is reported as *skipped* (never as a pass). ``SelfCheckTests`` always runs: it drives
the same batteries against synthetic records (and a deliberately permissive validator, which the
batteries must catch), so the test logic is exercised without the final records.

Request JSON (schema_version 1; every path absolute; Root writes it from the formal R-stage run;
the aggregate expected context is fixed by Root independently of the submitted records)::

    {
      "schema_version": 1,
      "requirements": "/.../requirements-p27-cw16.json",
      "candidate": "<64-hex C16 candidate id>",
      "checks": ["/.../check-p27-cw16-<batch>-observed.json", ...],   # Root-observed check records
      "gate_result": "/.../gate-p27-cw16-final-result.json",           # recorded gate output
      "stale_check": "/.../check-cw16-final-observed.json",            # optional; default p2.6 6dc51f
      "missing_item": "I-FAULT",                                       # optional; default I-FAULT
      "aggregate": {
        "root": "/home/.../ai-exp-assistant",                          # optional; default this repo
        "records": "/.../aggregate-p27-cw16-records.json",             # JSON list of records
        "expected": {                                                   # object, or path to one
          "items": {"P-C-AC-19": {"owner_ticket": "CW-16", "required_evidence_level": "runtime"}, ...},
          "candidate_id": "<C16>",
          "requirements_digest": "<64-hex>",
          "approval_digest": "<64-hex approved bundle>",
          "input_manifest": {"<relpath>": "<sha256>", ...},             # object, or path to one
          "evidence": {"<item id>": {"path": "<relpath>", "sha256": "<sha256>"}, ...}
        }
      }
    }

``input_manifest`` covers ``src/** omp_bridge/** tests/** pyproject.toml`` and
``docs/features/core-workbench/{PLAN.json,SPEC.md,BRIEF.md,DECISIONS.md}`` (never VERIFICATION.md
or COMPATIBILITY.md). Each record is converted from one observed check item (see
``records_from_checks``): ``item_id, owner_ticket, candidate_id, requirements_digest,
approval_digest, input_manifest, required_evidence_level, observed_evidence_level,
evidence_kind ("actual_runtime"), result, exit_code, unknowns, command (check argv), cwd (check
cwd), environment, evidence_ref {path, sha256}`` where ``evidence_ref`` pins the observed check JSON.

Gate negative controls (each exit 1, exact issues): missing check carrying ``missing_item``
(``missing``); p2.6 observed record added (``stale`` plus its own ``failed_or_not_run``/
``unknown``); one item ``unknowns: ["x"]`` (``unknown``); 1-byte log tamper (``log_mismatch`` on
every item of that check); other candidate (``stale`` on every item); one item
``observed_evidence_level: fixture`` (``evidence_level``); acceptance change
(``acceptance_mismatch``). Aggregate negative controls: missing/duplicate/unowned record, changed
integrated input, failed result/unknown/non-zero exit, changed candidate_id, insufficient evidence
level, fixture-only runtime evidence, evidence content change/removal, pinned JSON with a
``fixture`` key.

No model or provider requests, no network, no credential access; writes only under /tmp.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
GATE_SCRIPT = REPO / ".agents/skills/workflow-ledger/scripts/workflow_tools.py"
AGGREGATE_MODULE = REPO / "tests/gates/harness/aggregate.py"
RUN_DIR = REPO / ".workflow/core-workbench/runs/implement-p2.6-20260927"
P27_REQUIREMENTS = RUN_DIR / "requirements-p27-cw16.json"
ENV_VAR = "WB_CW16_FINAL_REQUEST"

# Historical p2.6 final record (candidate 6dc51f...afa88); pinned so the expected kinds stay true.
P26_STALE_CHECK = RUN_DIR / "check-cw16-final-observed.json"
P26_STALE_SHA256 = "cdeca27f5c28a74f8b2b3c6b072aa791500d4473878e0f90a4d18f30ec0cc800"
P26_CHECK_ID = "cw16-final-01"
_P26_FAIL = ["stale", "failed_or_not_run", "unknown"]
P26_STALE_ISSUES = [
    {"id": "P-C-AC-19", "check_id": P26_CHECK_ID, "kinds": _P26_FAIL},
    {"id": "P-C-AC-20", "check_id": P26_CHECK_ID, "kinds": ["stale"]},
    {"id": "I-FLOW", "check_id": P26_CHECK_ID, "kinds": _P26_FAIL},
    {"id": "I-SHELL", "check_id": P26_CHECK_ID, "kinds": _P26_FAIL},
    {"id": "I-POLICY", "check_id": P26_CHECK_ID, "kinds": _P26_FAIL},
    {"id": "I-FAULT", "check_id": P26_CHECK_ID, "kinds": _P26_FAIL},
    {"id": "I-COMPAT", "check_id": P26_CHECK_ID, "kinds": _P26_FAIL},
]

# Fixed by PLAN (CW-16 gate_items), independent of the submitted records.
FINAL_ITEMS = ("P-C-AC-19", "P-C-AC-20", "I-FLOW", "I-SHELL", "I-POLICY", "I-FAULT", "I-COMPAT")
FINAL_OWNER = "CW-16"
MANIFEST_DIRS = ("src", "omp_bridge", "tests")
MANIFEST_FILES = (
    "pyproject.toml",
    "docs/features/core-workbench/PLAN.json",
    "docs/features/core-workbench/SPEC.md",
    "docs/features/core-workbench/BRIEF.md",
    "docs/features/core-workbench/DECISIONS.md",
)
MANIFEST_FORBIDDEN = (
    "docs/features/core-workbench/VERIFICATION.md",
    "docs/features/core-workbench/COMPATIBILITY.md",
)

# Exact aggregate.py EvidenceError messages.
E_COVERAGE = "missing, duplicate or unowned gate items"
E_INPUT = "current integrated input changed"
E_FAILED = "failed check or required unknown"
E_CANDIDATE = "changed candidate_id"
E_LEVEL = "insufficient evidence level"
E_FIXTURE_RUNTIME = "fixture-only runtime evidence"
E_EVIDENCE = "evidence content changed/missing"
E_FIXTURE_KEY = "synthetic evidence requires explicit provenance-only fixture mode"

_SHA_CHARS = set("0123456789abcdef")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def is_sha(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _SHA_CHARS


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    return path


def make_workdir(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"wb-cw16-g4-{label}-", dir="/tmp"))


def _load_module(name: str, path: Path):
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


_MODULES: dict = {}


def aggregate_module():
    if "aggregate" not in _MODULES:
        _MODULES["aggregate"] = _load_module("cw16_g4_aggregate", AGGREGATE_MODULE)
    return _MODULES["aggregate"]


def requirements_identity(requirements: dict) -> str:
    """Same identity the workflow tool computes (used only to build synthetic records)."""
    if "workflow_tools" not in _MODULES:
        _MODULES["workflow_tools"] = _load_module("cw16_g4_workflow_tools", GATE_SCRIPT)
    return _MODULES["workflow_tools"].digest(requirements)


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _relative(value) -> bool:
    return isinstance(value, str) and value and not Path(value).is_absolute() and ".." not in Path(value).parts


# --------------------------------------------------------------------------------------------
# Record conversion (synthetic self-check uses it; Root may use it to emit the records file)
# --------------------------------------------------------------------------------------------

def records_from_checks(check_paths, *, root: Path, approval_digest: str, input_manifest: dict,
                        requirements_digest: str, items: dict, sources: dict | None = None) -> list[dict]:
    """One aggregate record per expected item from the observed check that carries it.

    ``sources`` maps item id -> check_id when an item appears in more than one check.
    """
    found: dict[str, list] = {}
    for path in check_paths:
        check = read_json(path)
        for item in check["items"]:
            found.setdefault(item["id"], []).append((Path(path), check, item))
    records = []
    for item_id, spec in items.items():
        occurrences = found.get(item_id, [])
        if sources and item_id in sources:
            occurrences = [o for o in occurrences if o[1]["check_id"] == sources[item_id]]
        if len(occurrences) != 1:
            raise ValueError(f"{item_id}: expected exactly one observed occurrence, got {len(occurrences)}")
        path, check, item = occurrences[0]
        records.append({
            "item_id": item_id,
            "owner_ticket": spec["owner_ticket"],
            "candidate_id": check["before"],
            "requirements_digest": requirements_digest,
            "approval_digest": approval_digest,
            "input_manifest": input_manifest,
            "required_evidence_level": spec["required_evidence_level"],
            "observed_evidence_level": item["observed_evidence_level"],
            "evidence_kind": "actual_runtime" if item["observed_evidence_level"] == "runtime" else "fixture",
            "result": item["result"],
            "exit_code": check["exit_code"],
            "unknowns": list(item["unknowns"]),
            "command": list(check["argv"]),
            "cwd": check["cwd"],
            "environment": check["environment"] if isinstance(check["environment"], dict)
            else {"description": check["environment"]},
            "evidence_ref": {"path": str(path.resolve().relative_to(Path(root).resolve())),
                             "sha256": sha256_file(path)},
        })
    return records


# --------------------------------------------------------------------------------------------
# Workflow gate battery
# --------------------------------------------------------------------------------------------

class GateBattery:
    """Copies requirements, checks and logs into ``workdir`` and runs the gate CLI on mutations."""

    def __init__(self, workdir: Path, *, requirements: Path, candidate: str, checks: list,
                 stale_check: Path, stale_issues: list, missing_item: str, gate_script: Path = GATE_SCRIPT):
        self.workdir = Path(workdir)
        self.gate_script = Path(gate_script)
        self.candidate = candidate
        self.missing_item = missing_item
        self.stale_issues = stale_issues
        self.requirements_data = read_json(requirements)
        self.gates = self.requirements_data["gate_items"]
        self.requirements = write_json(self.workdir / "requirements.json", self.requirements_data)
        self.checks = [self._stage_check(Path(p), f"check-{i}") for i, p in enumerate(checks)]
        self.stale = self._stage_check(Path(stale_check), "stale")
        self._runs = 0
        ids = [c["check_id"] for c in self.checks]
        _expect(len(ids) == len(set(ids)), f"genuine check ids must be unique: {ids}")
        _expect(any(g["id"] == missing_item for g in self.gates), f"{missing_item} is not a required gate item")

    def _stage_check(self, path: Path, label: str) -> dict:
        check = read_json(path)
        log = self.workdir / "logs" / f"{label}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(check["log"], log)
        check["log"] = str(log)
        return check

    def run(self, label: str, checks: list, candidate: str | None = None) -> tuple[int, dict]:
        self._runs += 1
        case = self.workdir / "runs" / f"{self._runs:02d}-{label}"
        paths = [str(write_json(case / f"check-{i}.json", c)) for i, c in enumerate(checks)]
        request = write_json(case / "gate-request.json", {
            "requirements": str(self.requirements),
            "candidate": candidate or self.candidate,
            "checks": paths,
        })
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1",
               "LANG": "C.UTF-8", "HOME": str(self.workdir)}
        proc = subprocess.run([sys.executable, str(self.gate_script), "gate", "--input", str(request)],
                              cwd=case, env=env, capture_output=True, text=True, timeout=120)
        _expect(proc.returncode in (0, 1), f"{label}: gate exit {proc.returncode}: {proc.stderr.strip()[:500]}")
        try:
            result = json.loads(proc.stdout)
        except ValueError as exc:
            raise AssertionError(f"{label}: gate stdout is not JSON: {proc.stdout[:300]!r}") from exc
        return proc.returncode, result

    def occurrences(self, checks: list, which=None) -> list[tuple[str, str]]:
        """(gate id, check id) pairs in the gate's own reporting order."""
        out = []
        for gate in self.gates:
            for index, check in enumerate(checks):
                if which is not None and index not in which:
                    continue
                out.extend((gate["id"], check["check_id"]) for item in check["items"] if item["id"] == gate["id"])
        return out

    def _first_occurrence(self, checks: list) -> tuple[int, int]:
        for gate in self.gates:
            for ci, check in enumerate(checks):
                for ii, item in enumerate(check["items"]):
                    if item["id"] == gate["id"]:
                        return ci, ii
        raise AssertionError("no gate item occurrence in the genuine checks")

    def _reject(self, label: str, checks: list, expected: list, candidate: str | None = None) -> dict:
        code, result = self.run(label, checks, candidate)
        _expect(code == 1 and result.get("passed") is False,
                f"{label}: expected exit 1 / passed false, got exit {code} {result}")
        _expect(result.get("issues") == expected,
                f"{label}: issues mismatch\nexpected={json.dumps(expected)}\nactual={json.dumps(result.get('issues'))}")
        return result

    def _item_issue(self, gate_id: str, check_id: str, kinds: list) -> dict:
        return {"id": gate_id, "check_id": check_id, "kinds": kinds}

    # cases ------------------------------------------------------------------------------------

    def genuine(self) -> dict:
        code, result = self.run("genuine", copy.deepcopy(self.checks))
        _expect(code == 0 and result == {"passed": True, "issues": []}, f"genuine set rejected: exit {code} {result}")
        return result

    def missing_check(self) -> dict:
        kept = [c for c in self.checks if not any(i["id"] == self.missing_item for i in c["items"])]
        covered = {i["id"] for c in kept for i in c["items"]}
        expected = [{"id": g["id"], "kind": "missing"} for g in self.gates if g["id"] not in covered]
        _expect({"id": self.missing_item, "kind": "missing"} in expected, "missing item must be reported")
        return self._reject("missing-check", copy.deepcopy(kept), expected)

    def stale_record(self) -> dict:
        return self._reject("stale-record", copy.deepcopy(self.checks + [self.stale]), self.stale_issues)

    def unknowns(self) -> dict:
        checks = copy.deepcopy(self.checks)
        ci, ii = self._first_occurrence(checks)
        item = checks[ci]["items"][ii]
        item["unknowns"] = ["x"]
        return self._reject("unknowns", checks, [self._item_issue(item["id"], checks[ci]["check_id"], ["unknown"])])

    def log_tamper(self) -> dict:
        checks = copy.deepcopy(self.checks)
        ci, _ = self._first_occurrence(checks)
        tampered = self.workdir / "logs" / f"tampered-{ci}.log"
        data = bytearray(Path(checks[ci]["log"]).read_bytes())
        if data:
            data[len(data) // 2] ^= 0x01
        else:
            data = bytearray(b"\x00")
        tampered.write_bytes(bytes(data))
        _expect(sha256_bytes(bytes(data)) != checks[ci]["log_sha256"], "tamper must change the log digest")
        checks[ci]["log"] = str(tampered)
        expected = [self._item_issue(g, c, ["log_mismatch"]) for g, c in self.occurrences(checks, {ci})]
        return self._reject("log-tamper", checks, expected)

    def other_candidate(self) -> dict:
        other = sha256_bytes(("other-candidate:" + self.candidate).encode())
        checks = copy.deepcopy(self.checks)
        expected = [self._item_issue(g, c, ["stale"]) for g, c in self.occurrences(checks)]
        return self._reject("other-candidate", checks, expected, candidate=other)

    def fixture_level(self) -> dict:
        checks = copy.deepcopy(self.checks)
        ci, ii = self._first_occurrence(checks)
        item = checks[ci]["items"][ii]
        item["observed_evidence_level"] = "fixture"
        return self._reject("fixture-level", checks,
                            [self._item_issue(item["id"], checks[ci]["check_id"], ["evidence_level"])])

    def acceptance_mismatch(self) -> dict:
        checks = copy.deepcopy(self.checks)
        ci, ii = self._first_occurrence(checks)
        item = checks[ci]["items"][ii]
        gate = next(g for g in self.gates if g["id"] == item["id"])
        if len(item["acceptance_ids"]) > 1:
            item["acceptance_ids"] = item["acceptance_ids"][:-1]
        else:
            spare = next(a for a in self.requirements_data["acceptance_ids"] if a not in gate["acceptance_ids"])
            item["acceptance_ids"] = [spare]
        return self._reject("acceptance-mismatch", checks,
                            [self._item_issue(item["id"], checks[ci]["check_id"], ["acceptance_mismatch"])])

    def negative_cases(self):
        return [
            ("missing_check", self.missing_check),
            ("stale_record", self.stale_record),
            ("unknowns", self.unknowns),
            ("log_tamper", self.log_tamper),
            ("other_candidate", self.other_candidate),
            ("fixture_level", self.fixture_level),
            ("acceptance_mismatch", self.acceptance_mismatch),
        ]


# --------------------------------------------------------------------------------------------
# aggregate.py validate() battery
# --------------------------------------------------------------------------------------------

class AggregateBattery:
    """Stages manifest inputs and pinned evidence under ``workdir/root`` and mutates copies."""

    def __init__(self, workdir: Path, *, root: Path, expected: dict, records: list, validate=None):
        self.source_root = Path(root).resolve()
        self.root = (Path(workdir) / "root").resolve()
        self.root.mkdir(parents=True)
        self.expected = copy.deepcopy(expected)
        module = aggregate_module()
        self.error = module.EvidenceError
        self.validate = validate or module.validate
        staged = set(expected["input_manifest"]) | {e["path"] for e in expected["evidence"].values()}
        for rel in sorted(staged):
            _expect(_relative(rel), f"non-relative staged path: {rel!r}")
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.source_root / rel, target)
        self.records = copy.deepcopy(records)
        for record in self.records:
            if record.get("cwd") == str(self.source_root):
                record["cwd"] = str(self.root)

    def run(self, records: list, expected: dict | None = None):
        return self.validate(copy.deepcopy(records), root=self.root, expected=copy.deepcopy(expected or self.expected))

    def _reject(self, label: str, message: str, records: list, expected: dict | None = None) -> str:
        try:
            result = self.run(records, expected)
        except self.error as exc:
            _expect(str(exc) == message, f"{label}: expected {message!r}, got {str(exc)!r}")
            return str(exc)
        raise AssertionError(f"{label}: accepted, returned {result!r}; expected {message!r}")

    def _with(self, index: int, **changes) -> list:
        records = copy.deepcopy(self.records)
        records[index].update(changes)
        return records

    def _mutate_file(self, rel: str, label: str, message: str, mutate) -> str:
        path = self.root / rel
        original = path.read_bytes()
        try:
            mutate(path, original)
            return self._reject(label, message, self.records)
        finally:
            path.write_bytes(original)

    # cases ------------------------------------------------------------------------------------

    def genuine(self) -> list:
        result = self.run(self.records)
        _expect(result == sorted(self.expected["items"]), f"genuine records returned {result!r}")
        return result

    def missing_record(self):
        return self._reject("missing-record", E_COVERAGE, self.records[1:])

    def duplicate_record(self):
        self._reject("duplicate-record-extra", E_COVERAGE, self.records + [copy.deepcopy(self.records[0])])
        return self._reject("duplicate-record-replacing", E_COVERAGE,
                            self.records[:-1] + [copy.deepcopy(self.records[0])])

    def unowned_record(self):
        self._reject("unowned-record-extra", E_COVERAGE, self.records + [dict(self.records[0], item_id="X-UNOWNED")])
        return self._reject("unowned-record-renamed", E_COVERAGE, self._with(0, item_id="X-UNOWNED"))

    def changed_input(self):
        rel = sorted(self.expected["input_manifest"])[0]
        self._mutate_file(rel, "input-appended", E_INPUT, lambda p, o: p.write_bytes(o + b"\n"))
        return self._mutate_file(rel, "input-removed", E_INPUT, lambda p, o: p.unlink())

    def failed_or_unknown(self):
        last = len(self.records) - 1
        self._reject("result-failed", E_FAILED, self._with(last, result="failed"))
        self._reject("required-unknown", E_FAILED, self._with(last, unknowns=["x"]))
        return self._reject("nonzero-exit", E_FAILED, self._with(last, exit_code=1))

    def changed_candidate(self):
        other = sha256_bytes(("other-candidate:" + str(self.expected["candidate_id"])).encode())
        return self._reject("changed-candidate", E_CANDIDATE, self._with(0, candidate_id=other))

    def insufficient_level(self):
        index = next(i for i, r in enumerate(self.records)
                     if self.expected["items"][r["item_id"]]["required_evidence_level"] == "runtime")
        self._reject("fixture-observed-level", E_LEVEL, self._with(index, observed_evidence_level="fixture"))
        return self._reject("fixture-only-runtime", E_FIXTURE_RUNTIME, self._with(index, evidence_kind="fixture"))

    def evidence_changed(self):
        rel = self.records[0]["evidence_ref"]["path"]

        def flip(path, original):
            data = bytearray(original)
            data[len(data) // 2] ^= 0x01
            path.write_bytes(bytes(data))

        self._mutate_file(rel, "evidence-byte-flip", E_EVIDENCE, flip)
        return self._mutate_file(rel, "evidence-removed", E_EVIDENCE, lambda p, o: p.unlink())

    def fixture_key(self):
        for record in self.records:
            rel = record["evidence_ref"]["path"]
            try:
                payload = json.loads((self.root / rel).read_bytes().strip())
            except ValueError:
                continue
            if isinstance(payload, dict):
                break
        else:
            raise AssertionError("no pinned JSON-object evidence to carry a fixture key")
        path = self.root / rel
        original = path.read_bytes()
        try:
            payload["fixture"] = True
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
            sha = sha256_file(path)
            expected = copy.deepcopy(self.expected)
            records = copy.deepcopy(self.records)
            for ref in list(expected["evidence"].values()) + [r["evidence_ref"] for r in records]:
                if ref["path"] == rel:
                    ref["sha256"] = sha
            return self._reject("pinned-fixture-key", E_FIXTURE_KEY, records, expected)
        finally:
            path.write_bytes(original)

    def negative_cases(self):
        return [
            ("missing_record", self.missing_record),
            ("duplicate_record", self.duplicate_record),
            ("unowned_record", self.unowned_record),
            ("changed_input", self.changed_input),
            ("failed_or_unknown", self.failed_or_unknown),
            ("changed_candidate", self.changed_candidate),
            ("insufficient_level", self.insufficient_level),
            ("evidence_changed", self.evidence_changed),
            ("fixture_key", self.fixture_key),
        ]


# --------------------------------------------------------------------------------------------
# Request loading
# --------------------------------------------------------------------------------------------

def _abs_file(value, name: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
        raise ValueError(f"{name}: expected an absolute path to an existing file, got {value!r}")
    return Path(value)


def _object_or_path(value, name: str) -> dict:
    if isinstance(value, str):
        value = read_json(_abs_file(value, name))
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{name}: expected a nonempty JSON object")
    return value


def load_request(path) -> dict:
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("request: schema_version must be 1")
    requirements = _abs_file(data.get("requirements"), "requirements")
    if not is_sha(data.get("candidate")):
        raise ValueError("candidate: expected 64 lowercase hex characters")
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("checks: expected a nonempty list")
    checks = [_abs_file(c, f"checks[{i}]") for i, c in enumerate(checks)]
    gate_result = _abs_file(data.get("gate_result"), "gate_result")
    stale_check = _abs_file(data.get("stale_check", str(P26_STALE_CHECK)), "stale_check")
    missing_item = data.get("missing_item", "I-FAULT")
    aggregate = data.get("aggregate")
    if not isinstance(aggregate, dict):
        raise ValueError("aggregate: expected an object")
    root = Path(aggregate.get("root", str(REPO)))
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("aggregate.root: expected an absolute directory")
    expected = _object_or_path(aggregate.get("expected"), "aggregate.expected")
    expected = dict(expected, input_manifest=_object_or_path(expected.get("input_manifest"),
                                                             "aggregate.expected.input_manifest"))
    for key in ("items", "candidate_id", "requirements_digest", "approval_digest", "evidence"):
        if key not in expected:
            raise ValueError(f"aggregate.expected.{key}: missing")
    records = read_json(_abs_file(aggregate.get("records"), "aggregate.records"))
    if not isinstance(records, list) or not records:
        raise ValueError("aggregate.records: expected a nonempty JSON list")
    return {"requirements": requirements, "candidate": data["candidate"], "checks": checks,
            "gate_result": gate_result, "stale_check": stale_check, "missing_item": missing_item,
            "root": root, "expected": expected, "records": records}


def manifest_scope(root: Path) -> set[str]:
    """Files git would snapshot under the manifest scope (tracked + untracked, not ignored)."""
    proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "-c", "-o", "--exclude-standard", "--",
                           *MANIFEST_DIRS, *MANIFEST_FILES], capture_output=True, check=True, timeout=60)
    return {p for p in proc.stdout.decode().split("\0") if p and (root / p).is_file()}


# --------------------------------------------------------------------------------------------
# Final records (skipped unless WB_CW16_FINAL_REQUEST is set)
# --------------------------------------------------------------------------------------------

class FinalRecordsTests(unittest.TestCase):
    """The genuine final set passes both validators; every mutation is rejected exactly."""

    @classmethod
    def setUpClass(cls):
        request = os.environ.get(ENV_VAR)
        if not request:
            raise unittest.SkipTest(f"{ENV_VAR} not set; final records not supplied (not a pass)")
        cls.request = load_request(request)
        if sha256_file(cls.request["stale_check"]) != P26_STALE_SHA256:
            raise AssertionError("stale_check is not the pinned p2.6 6dc51f record")
        cls.workdir = make_workdir("final")
        r = cls.request
        cls.gate = GateBattery(cls.workdir / "gate", requirements=r["requirements"], candidate=r["candidate"],
                               checks=r["checks"], stale_check=r["stale_check"], stale_issues=P26_STALE_ISSUES,
                               missing_item=r["missing_item"])
        cls.aggregate = AggregateBattery(cls.workdir / "aggregate", root=r["root"], expected=r["expected"],
                                         records=r["records"])

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "workdir", None):
            shutil.rmtree(cls.workdir, ignore_errors=True)

    def test_expected_context_is_pinned_by_root(self):
        r = self.request
        requirements = read_json(r["requirements"])
        self.assertEqual([g["id"] for g in requirements["gate_items"]], list(FINAL_ITEMS))
        self.assertTrue(all(g["required_evidence_level"] == "runtime" for g in requirements["gate_items"]))
        expected = r["expected"]
        self.assertEqual(expected["items"], {i: {"owner_ticket": FINAL_OWNER, "required_evidence_level": "runtime"}
                                             for i in FINAL_ITEMS})
        self.assertEqual(expected["candidate_id"], r["candidate"])
        for key in ("requirements_digest", "approval_digest"):
            self.assertTrue(is_sha(expected[key]), key)
        self.assertEqual(set(expected["evidence"]), set(FINAL_ITEMS))
        check_paths = {p.resolve() for p in r["checks"]}
        for item_id, ref in expected["evidence"].items():
            self.assertEqual(set(ref), {"path", "sha256"}, item_id)
            self.assertIn((r["root"] / ref["path"]).resolve(), check_paths, f"{item_id} evidence is not an observed check")

    def test_input_manifest_scope_is_complete(self):
        manifest = self.request["expected"]["input_manifest"]
        for forbidden in MANIFEST_FORBIDDEN:
            self.assertNotIn(forbidden, manifest)
        self.assertTrue(all(is_sha(v) for v in manifest.values()))
        self.assertEqual(set(manifest), manifest_scope(self.request["root"]))

    def test_records_match_observed_checks(self):
        checks = {p.resolve(): read_json(p) for p in self.request["checks"]}
        for record in self.request["records"]:
            with self.subTest(item=record.get("item_id")):
                check = checks[(self.request["root"] / record["evidence_ref"]["path"]).resolve()]
                items = [i for i in check["items"] if i["id"] == record["item_id"]]
                self.assertEqual(len(items), 1)
                item = items[0]
                self.assertEqual(record["result"], item["result"])
                self.assertEqual(record["unknowns"], item["unknowns"])
                self.assertEqual(record["observed_evidence_level"], item["observed_evidence_level"])
                self.assertEqual(record["evidence_kind"], "actual_runtime")
                self.assertEqual(record["exit_code"], check["exit_code"])
                self.assertEqual(record["command"], check["argv"])
                self.assertEqual(record["cwd"], check["cwd"])
                self.assertEqual(record["candidate_id"], check["before"])
                self.assertEqual(check["before"], check["after"])

    def test_recorded_gate_result_passed(self):
        self.assertEqual(read_json(self.request["gate_result"]), {"passed": True, "issues": []})

    def test_gate_accepts_genuine_set(self):
        self.gate.genuine()

    def test_gate_rejects_each_mutation(self):
        self.gate.genuine()  # a mutation only counts as rejected if the genuine set passes
        for name, case in self.gate.negative_cases():
            with self.subTest(case=name):
                case()

    def test_aggregate_accepts_genuine_set_at_repo_root(self):
        r = self.request
        module = aggregate_module()
        self.assertEqual(module.validate(copy.deepcopy(r["records"]), root=r["root"],
                                         expected=copy.deepcopy(r["expected"])), sorted(FINAL_ITEMS))

    def test_aggregate_accepts_genuine_copy(self):
        self.assertEqual(self.aggregate.genuine(), sorted(FINAL_ITEMS))

    def test_aggregate_rejects_each_mutation(self):
        self.aggregate.genuine()  # a mutation only counts as rejected if the genuine set passes
        for name, case in self.aggregate.negative_cases():
            with self.subTest(case=name):
                case()


# --------------------------------------------------------------------------------------------
# Self-check on synthetic records (always runs)
# --------------------------------------------------------------------------------------------

SYN_ITEMS = ("G-A", "G-B", "G-C")


def build_synthetic(workdir: Path) -> dict:
    """A small but complete final set: requirements, two checks + logs, stale record, repo root."""
    requirements = {
        "schema_version": 1, "revision": "synthetic-final", "authority": "self-check",
        "acceptance_ids": ["A-1", "A-2", "A-3", "A-4"],
        "gate_items": [
            {"id": "G-A", "acceptance_ids": ["A-1", "A-2"], "required_evidence_level": "runtime"},
            {"id": "G-B", "acceptance_ids": ["A-3"], "required_evidence_level": "runtime"},
            {"id": "G-C", "acceptance_ids": ["A-4"], "required_evidence_level": "runtime"},
        ],
    }
    old_requirements = dict(requirements, revision="synthetic-historical")
    candidate = sha256_bytes(b"synthetic-c16")
    root = workdir / "repo"
    run = root / ".workflow/run"
    for rel, text in {"src/workbench/a.py": "A = 1\n", "omp_bridge/b.ts": "export {}\n", "tests/t.py": "pass\n",
                      "pyproject.toml": "[project]\nname='x'\n",
                      "docs/features/core-workbench/PLAN.json": "{}\n"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    req_path = write_json(run / "requirements.json", requirements)

    def check(check_id, items, *, identity, cand, log_text):
        log = run / f"{check_id}.log"
        log.write_text(log_text)
        return {"schema_version": 1, "check_id": check_id, "requirements_identity": identity,
                "argv": ["/usr/bin/env", "python3", "-m", "unittest", check_id], "cwd": str(root.resolve()),
                "environment": {"description": "synthetic"}, "started_at": "2026-10-08T00:00:00+00:00",
                "finished_at": "2026-10-08T00:01:00+00:00", "exit_code": 0, "timed_out": False,
                "log": str(log), "log_sha256": sha256_file(log), "before": cand, "after": cand,
                "stable": True, "items": items}

    def item(item_id, acceptance, result="passed", unknowns=()):
        return {"id": item_id, "acceptance_ids": acceptance, "result": result,
                "observed_evidence_level": "runtime", "evidence": f"synthetic {item_id}", "unknowns": list(unknowns)}

    identity = requirements_identity(requirements)
    c1 = write_json(run / "check-1-observed.json", check(
        "syn-1", [item("G-A", ["A-1", "A-2"]), item("G-B", ["A-3"])], identity=identity, cand=candidate,
        log_text="OK 1\n" * 20))
    c2 = write_json(run / "check-2-observed.json", check(
        "syn-2", [item("G-C", ["A-4"])], identity=identity, cand=candidate, log_text="OK 2\n" * 20))
    stale = write_json(run / "check-stale-observed.json", check(
        "syn-old", [item("G-A", ["A-1", "A-2"], "failed", ["u"]), item("G-B", ["A-3"]),
                    item("G-C", ["A-4"], "not_run")],
        identity=requirements_identity(old_requirements), cand=sha256_bytes(b"historical"), log_text="old\n"))
    stale_issues = [
        {"id": "G-A", "check_id": "syn-old", "kinds": ["stale", "failed_or_not_run", "unknown"]},
        {"id": "G-B", "check_id": "syn-old", "kinds": ["stale"]},
        {"id": "G-C", "check_id": "syn-old", "kinds": ["stale", "failed_or_not_run"]},
    ]
    manifest = {rel: sha256_file(root / rel) for rel in
                ("src/workbench/a.py", "omp_bridge/b.ts", "tests/t.py", "pyproject.toml",
                 "docs/features/core-workbench/PLAN.json")}
    items = {i: {"owner_ticket": FINAL_OWNER, "required_evidence_level": "runtime"} for i in SYN_ITEMS}
    expected = {
        "items": items, "candidate_id": candidate, "requirements_digest": identity,
        "approval_digest": sha256_bytes(b"bundle"), "input_manifest": manifest,
        "evidence": {"G-A": {"path": ".workflow/run/check-1-observed.json", "sha256": sha256_file(c1)},
                     "G-B": {"path": ".workflow/run/check-1-observed.json", "sha256": sha256_file(c1)},
                     "G-C": {"path": ".workflow/run/check-2-observed.json", "sha256": sha256_file(c2)}},
    }
    records = records_from_checks([c1, c2], root=root, approval_digest=expected["approval_digest"],
                                  input_manifest=manifest, requirements_digest=identity, items=items)
    return {"requirements": req_path, "candidate": candidate, "checks": [c1, c2], "stale": stale,
            "stale_issues": stale_issues, "root": root, "expected": expected, "records": records}


class SelfCheckTests(unittest.TestCase):
    """Drives both batteries on synthetic records so the test logic runs without final records."""

    def setUp(self):
        self.workdir = make_workdir("self")
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.syn = build_synthetic(self.workdir)

    def gate_battery(self, gate_script: Path = GATE_SCRIPT, label: str = "gate") -> GateBattery:
        s = self.syn
        return GateBattery(self.workdir / label, requirements=s["requirements"], candidate=s["candidate"],
                           checks=s["checks"], stale_check=s["stale"], stale_issues=s["stale_issues"],
                           missing_item="G-C", gate_script=gate_script)

    def aggregate_battery(self, validate=None, label: str = "aggregate") -> AggregateBattery:
        s = self.syn
        return AggregateBattery(self.workdir / label, root=s["root"], expected=s["expected"],
                                records=s["records"], validate=validate)

    def test_gate_genuine_and_mutations(self):
        battery = self.gate_battery()
        battery.genuine()
        for name, case in battery.negative_cases():
            with self.subTest(case=name):
                case()
        # Originals are untouched (copies only).
        for path in self.syn["checks"]:
            check = read_json(path)
            self.assertEqual(sha256_file(Path(check["log"])), check["log_sha256"])

    def test_gate_battery_catches_permissive_or_wrong_gate(self):
        for label, body in (("permissive", "print(json.dumps({'passed': True, 'issues': []}))\nsys.exit(0)"),
                            ("wrong-kind", "print(json.dumps({'passed': False, 'issues': [{'id': 'G-A', 'kind': 'x'}]}))\n"
                                           "sys.exit(1)")):
            stub = self.workdir / f"stub-{label}.py"
            stub.write_text(f"import json, sys\n{body}\n")
            battery = self.gate_battery(stub, f"gate-{label}")
            for name, case in battery.negative_cases():
                with self.subTest(stub=label, case=name):
                    with self.assertRaises(AssertionError):
                        case()

    def test_aggregate_genuine_and_mutations(self):
        battery = self.aggregate_battery()
        self.assertEqual(battery.genuine(), sorted(SYN_ITEMS))
        for name, case in battery.negative_cases():
            with self.subTest(case=name):
                case()
        self.assertEqual(battery.genuine(), sorted(SYN_ITEMS), "file mutations must be restored")

    def test_aggregate_battery_catches_permissive_or_wrong_validator(self):
        error = aggregate_module().EvidenceError

        def permissive(records, *, root, expected):
            return sorted(expected["items"])

        def wrong_message(records, *, root, expected):
            raise error("something else")

        for label, validate in (("permissive", permissive), ("wrong-message", wrong_message)):
            battery = self.aggregate_battery(validate, f"aggregate-{label}")
            for name, case in battery.negative_cases():
                with self.subTest(validator=label, case=name):
                    with self.assertRaises(AssertionError):
                        case()

    def test_records_from_checks_requires_unique_source(self):
        s = self.syn
        duplicate = write_json(self.workdir / "dup-check.json", read_json(s["checks"][0]))
        with self.assertRaises(ValueError):
            records_from_checks([*s["checks"], duplicate], root=s["root"], approval_digest="0" * 64,
                                input_manifest=s["expected"]["input_manifest"], requirements_digest="0" * 64,
                                items=s["expected"]["items"])

    def test_load_request_rejects_malformed_and_accepts_complete(self):
        s = self.syn
        records = write_json(self.workdir / "records.json", s["records"])
        manifest = write_json(self.workdir / "manifest.json", s["expected"]["input_manifest"])
        gate_result = write_json(self.workdir / "gate-result.json", {"passed": True, "issues": []})
        good = {"schema_version": 1, "requirements": str(s["requirements"]), "candidate": s["candidate"],
                "checks": [str(p) for p in s["checks"]], "gate_result": str(gate_result),
                "stale_check": str(s["stale"]), "missing_item": "G-C",
                "aggregate": {"root": str(s["root"]), "records": str(records),
                              "expected": dict(s["expected"], input_manifest=str(manifest))}}
        loaded = load_request(write_json(self.workdir / "request-good.json", good))
        self.assertEqual(loaded["expected"]["input_manifest"], s["expected"]["input_manifest"])
        bad_variants = {
            "schema": dict(good, schema_version=2),
            "candidate": dict(good, candidate="C16"),
            "checks-empty": dict(good, checks=[]),
            "checks-relative": dict(good, checks=["check.json"]),
            "no-gate-result": {k: v for k, v in good.items() if k != "gate_result"},
            "no-aggregate": {k: v for k, v in good.items() if k != "aggregate"},
            "no-records": dict(good, aggregate={k: v for k, v in good["aggregate"].items() if k != "records"}),
            "no-approval": dict(good, aggregate=dict(good["aggregate"], expected={
                k: v for k, v in good["aggregate"]["expected"].items() if k != "approval_digest"})),
        }
        for name, request in bad_variants.items():
            with self.subTest(variant=name):
                with self.assertRaises(ValueError):
                    load_request(write_json(self.workdir / f"request-{name}.json", request))

    def test_pinned_p26_stale_issue_kinds_match_gate(self):
        if not P26_STALE_CHECK.is_file() or not P27_REQUIREMENTS.is_file():
            self.skipTest("p2.6 stale record or p2.7 requirements not present")
        self.assertEqual(sha256_file(P26_STALE_CHECK), P26_STALE_SHA256)
        battery = GateBattery(self.workdir / "p26", requirements=P27_REQUIREMENTS,
                              candidate=sha256_bytes(b"any-c16"), checks=[], stale_check=P26_STALE_CHECK,
                              stale_issues=P26_STALE_ISSUES, missing_item="I-FAULT")
        self.assertEqual([g["id"] for g in battery.gates], list(FINAL_ITEMS))
        battery.stale_record()


if __name__ == "__main__":
    unittest.main()
