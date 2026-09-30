"""Requirement-derived cross-language rejection and adapter matrix."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import unittest

from workbench.contracts.v1 import ContractError
from workbench.contracts.ports_v2 import adapt_v1_delivery, parse_port


class IndependentPortContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = json.loads((Path(__file__).parent / "fixtures/ports-v2.json").read_text())

    def outcomes(self, cases, adapter=False):
        python = []
        for value in cases:
            try:
                (adapt_v1_delivery if adapter else parse_port)(value)
                python.append(True)
            except ContractError:
                python.append(False)
        script = """import {readFileSync} from 'node:fs';
import {parsePort,adaptV1Delivery} from './omp_bridge/contract/ports_v2.ts';
const {cases,adapter}=JSON.parse(readFileSync(0,'utf8'));
console.log(JSON.stringify(cases.map(value=>{try{(adapter?adaptV1Delivery:parsePort)(value);return true;}catch{return false;}})));"""
        run = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", script],
                             input=json.dumps({"cases": cases, "adapter": adapter}),
                             capture_output=True, text=True, timeout=10, check=True)
        return python, json.loads(run.stdout)

    def test_all_required_fields_versions_and_malformed_states_reject_in_both_languages(self):
        cases = []
        for port in self.values:
            for key in ("portVersion", "kind", "payload"):
                changed = copy.deepcopy(port)
                del changed[key]
                cases.append(changed)
            for key in port["payload"]:
                changed = copy.deepcopy(port)
                del changed["payload"][key]
                cases.append(changed)
            for version in (-1, 0, 1, 3, "2", True, None):
                cases.append({**port, "portVersion": version})
            cases.extend(({**port, "kind": "__proto__"}, {**port, "payload": []},
                          {**port, "payload": {**port["payload"], "futureField": True}}))
        for kind, key, values in (("ShellControl", "parentPid", (0, -1, True, "1")),
                                  ("ShellControl", "phase", ("completed", None, [])),
                                  ("Takeover", "inputTarget", (0, -1, True, "1")),
                                  ("AutomationState", "paused", (0, 1, "false", None)),
                                  ("ResumeEvidence", "unknowns", ([None], [""], "lost"))):
            port = next(v for v in self.values if v["kind"] == kind)
            for value in values:
                changed = copy.deepcopy(port)
                changed["payload"][key] = value
                cases.append(changed)
        python, typescript = self.outcomes(cases)
        self.assertEqual(python, [False] * len(cases))
        self.assertEqual(typescript, [False] * len(cases))

    def test_json_safe_integer_boundary_has_identical_rejection_semantics(self):
        cases = []
        for kind, field in (("ShellControl", "parentPid"), ("ShellControl", "generation"),
                            ("ShellControl", "ownerEpoch"), ("Takeover", "inputTarget")):
            port = copy.deepcopy(next(v for v in self.values if v["kind"] == kind))
            port["payload"][field] = 2 ** 53
            cases.append(port)
        python, typescript = self.outcomes(cases)
        self.assertEqual(python, typescript, "Cross-language port contracts disagree on unsafe JSON integers")
        self.assertEqual(typescript, [False] * len(cases))

    def test_v1_adapter_preserves_envelope_and_rejects_incomplete_or_new_versions(self):
        envelope = next(v for v in self.values if v["kind"] == "DeliveryObservation")["payload"]["envelope"]
        adapted = adapt_v1_delivery(envelope)
        self.assertEqual(adapted["payload"], {"envelope": envelope, "stage": "local_received"})
        cases = []
        expected = []
        for key in envelope:
            changed = copy.deepcopy(envelope)
            del changed[key]
            cases.append(changed)
            # These are optional in the approved preserved v1 envelope.
            expected.append(key in {"taskId", "revisionId", "runId"})
        cases.append({**envelope, "schemaVersion": 2})
        expected.append(False)
        python, typescript = self.outcomes([envelope, *cases], adapter=True)
        self.assertEqual(python, [True, *expected])
        self.assertEqual(typescript, python)


if __name__ == "__main__": unittest.main()
