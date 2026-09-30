"""Independent aggregate coverage/provenance fail-closed controls."""
from __future__ import annotations

import copy
import unittest

import test_aggregate as fixture


class AggregateAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.AggregateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.expected, self.records = self.fixture.root, self.fixture.expected, self.fixture.records

    def test_extra_unknown_item_and_boolean_exit_cannot_pass(self):
        extra = copy.deepcopy(self.records)
        extra[0]["item_id"] = "G4-NOT-APPROVED"
        boolean_exit = copy.deepcopy(self.records)
        boolean_exit[0]["exit_code"] = False
        for records in ([], extra, boolean_exit):
            with self.subTest(records_count=len(records)), self.assertRaises(fixture.aggregate.EvidenceError):
                fixture.aggregate.validate(records, root=self.root, expected=self.expected)

    def test_stale_pinned_source_and_evidence_escape_are_rejected(self):
        for field in ("candidate_id", "requirements_digest", "approval_digest"):
            records = copy.deepcopy(self.records)
            records[0][field] = "stale-digest"
            with self.subTest(field=field), self.assertRaises(fixture.aggregate.EvidenceError):
                fixture.aggregate.validate(records, root=self.root, expected=self.expected)
        expected = copy.deepcopy(self.expected)
        expected["input_manifest"] = {"../outside-source": "0" * 64}
        with self.assertRaises(fixture.aggregate.EvidenceError):
            fixture.aggregate.validate(self.records, root=self.root, expected=expected)

    def test_empty_required_context_cannot_be_a_gate_certificate(self):
        expected = {**self.expected, "items": {}, "evidence": {}}
        with self.assertRaises(fixture.aggregate.EvidenceError):
            fixture.aggregate.validate([], root=self.root, expected=expected)

    def test_empty_or_schema_only_pinned_evidence_is_not_observed_runtime_evidence(self):
        evidence = self.root / "validator-fixture.json"
        for content in (b"", b'{"schemaVersion":1}'):
            with self.subTest(content=content):
                evidence.write_bytes(content)
                ref = {"path": evidence.name, "sha256": fixture.aggregate.digest(evidence)}
                expected = copy.deepcopy(self.expected)
                expected["evidence"] = {key: ref for key in expected["items"]}
                records = copy.deepcopy(self.records)
                for record in records: record["evidence_ref"] = ref
                with self.assertRaises(fixture.aggregate.EvidenceError):
                    fixture.aggregate.validate(records, root=self.root, expected=expected)


if __name__ == "__main__": unittest.main()
