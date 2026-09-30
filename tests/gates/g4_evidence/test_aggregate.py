"""Validator contract fixtures; these do not certify runtime gate success."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("aggregate", REPO / "tests/gates/harness/aggregate.py")
aggregate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aggregate)


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cw05-evidence-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        plan = json.loads((REPO / "docs/features/core-workbench/PLAN.json").read_text())
        items = {i["id"]: {k: i[k] for k in ("owner_ticket", "required_evidence_level")}
                 for i in plan["gate_items"] if i["id"].startswith(("M0-", "G1-", "G2-", "G3-", "G4-"))}
        # Actual integrated source bytes, with synthetic observed-run metadata.
        inputs = {}
        for path in ("src/workbench/contracts/v1.py", "src/workbench/contracts/ports_v2.py", "src/workbench/runtime/g4.py", "src/workbench/terminal/shell_g2/lifecycle.py"):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((REPO / path).read_bytes())
            inputs[path] = aggregate.digest(target)
        evidence = self.root / "validator-fixture.json"
        evidence.write_text('{"fixture":"synthetic run provenance, not runtime acceptance"}')
        ref = {"path": evidence.name, "sha256": aggregate.digest(evidence)}
        self.expected = {"items": items, "candidate_id": "c" * 64, "requirements_digest": "r" * 64,
                         "validation_mode": "provenance_only_fixture",
                         "approval_digest": "a" * 64, "input_manifest": inputs,
                         "evidence": {i: ref for i in items}}
        self.records = [{**{k: self.expected[k] for k in ("candidate_id", "requirements_digest", "approval_digest", "input_manifest")},
                         "item_id": i, **item, "observed_evidence_level": item["required_evidence_level"],
                         "evidence_kind": "actual_runtime" if item["required_evidence_level"] == "runtime" else "fixture",
                         "result": "passed", "exit_code": 0, "unknowns": [], "command": ["fixture-command"],
                         "cwd": str(self.root), "environment": {"fixture": True}, "evidence_ref": ref}
                        for i, item in items.items()]

    def test_integrated_valid_set_contract(self):
        self.assertEqual(aggregate.validate(self.records, root=self.root, expected=self.expected), sorted(self.expected["items"]))
        self.assertEqual(len(self.records), 25)

    def test_missing_duplicate_and_owned_item(self):
        for records in (self.records[:-1], self.records + [self.records[0]], [self.records[0]] * len(self.records)):
            with self.assertRaises(aggregate.EvidenceError):
                aggregate.validate(records, root=self.root, expected=self.expected)

    def test_identity_level_check_unknown_and_provenance_negatives(self):
        changes = {"candidate_id": "other", "requirements_digest": "other", "approval_digest": "other",
                   "input_manifest": {}, "owner_ticket": "CW-99", "required_evidence_level": "fixture",
                   "observed_evidence_level": "fixture", "evidence_kind": "fixture", "result": "unknown",
                   "exit_code": 1, "unknowns": ["lost control"], "command": [], "environment": None,
                   "evidence_ref": {"path": "missing", "sha256": "x"}}
        index = next(i for i, r in enumerate(self.records) if r["item_id"] == "G4-LIFETIME")
        for key, value in changes.items():
            with self.subTest(key=key):
                records = copy.deepcopy(self.records)
                records[index][key] = value
                with self.assertRaises(aggregate.EvidenceError):
                    aggregate.validate(records, root=self.root, expected=self.expected)

    def test_current_input_or_evidence_bytes_changed(self):
        target = self.root / next(iter(self.expected["input_manifest"]))
        original = target.read_bytes()
        target.write_bytes(original + b"# changed\n")
        with self.assertRaises(aggregate.EvidenceError):
            aggregate.validate(self.records, root=self.root, expected=self.expected)
        target.write_bytes(original)
        (self.root / "validator-fixture.json").write_text("changed")
        with self.assertRaises(aggregate.EvidenceError):
            aggregate.validate(self.records, root=self.root, expected=self.expected)

    def test_synthetic_provenance_needs_explicit_fixture_mode_and_nonempty_context(self):
        for key in ("validation_mode", "input_manifest", "evidence"):
            expected = copy.deepcopy(self.expected)
            expected[key] = None if key == "validation_mode" else {}
            with self.subTest(key=key), self.assertRaises(aggregate.EvidenceError):
                aggregate.validate(self.records, root=self.root, expected=expected)

    def test_null_empty_and_schema_alias_payloads_reject_even_when_re_pinned(self):
        evidence = self.root / "validator-fixture.json"
        for content in ("null", "[]", "{}", "false", '{"$schema":"fixture","version":1}'):
            evidence.write_text(content)
            ref = {"path": evidence.name, "sha256": aggregate.digest(evidence)}
            expected, records = copy.deepcopy(self.expected), copy.deepcopy(self.records)
            expected["evidence"] = {i: ref for i in expected["items"]}
            for record in records:
                record["evidence_ref"] = ref
            with self.subTest(content=content), self.assertRaises(aggregate.EvidenceError):
                aggregate.validate(records, root=self.root, expected=expected)
