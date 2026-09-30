import copy
import json
from pathlib import Path
import subprocess
import unittest

from workbench.contracts.v1 import ContractError, ControlEnvelope
from workbench.contracts.ports_v2 import parse_port, adapt_v1_delivery, dispatch_allowed, resume_allowed


class PortTests(unittest.TestCase):
    def fixture(self):
        return json.loads((Path(__file__).parent / "fixtures/ports-v2.json").read_text())

    def test_cross_language_roundtrip_and_v1_adapter(self):
        values = self.fixture()
        self.assertEqual([parse_port(v) for v in values], values)
        result = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e",
            "import {readFileSync} from 'node:fs'; import {parsePort} from './omp_bridge/contract/ports_v2.ts'; console.log(JSON.stringify(JSON.parse(readFileSync('tests/contracts/fixtures/ports-v2.json','utf8')).map(parsePort)));"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), values)
        envelope = values[2]["payload"]["envelope"]
        self.assertEqual(ControlEnvelope.from_dict(envelope).to_dict(), envelope)
        self.assertEqual(adapt_v1_delivery(envelope)["payload"]["stage"], "local_received")

    def test_comma_key_cannot_replace_two_required_shell_fields(self):
        value = self.fixture()[0]
        del value["payload"]["generation"]
        del value["payload"]["ownerEpoch"]
        value["payload"]["generation,ownerEpoch"] = True
        with self.assertRaises(ContractError):
            parse_port(value)

    def test_old_versions_extra_fields_and_unknown_evidence_rejected_or_held(self):
        for value in self.fixture():
            for version in (0, 1, 3, True):
                with self.subTest(kind=value["kind"], version=version), self.assertRaises(ContractError):
                    parse_port({**value, "portVersion": version})
            with self.assertRaises(ContractError):
                parse_port({**value, "extra": True})
        automation = self.fixture()[3]
        self.assertTrue(dispatch_allowed(automation))
        for key in ("paused", "cancelled", "metadataHealthy", "approvalValid"):
            changed = copy.deepcopy(automation)
            changed["payload"][key] = key in ("paused", "cancelled")
            self.assertFalse(dispatch_allowed(changed))
        resume = self.fixture()[4]
        self.assertTrue(resume_allowed(resume))
        changed = copy.deepcopy(resume)
        changed["payload"]["unknowns"] = ["unconfirmed tool outcome"]
        self.assertFalse(resume_allowed(changed))
        takeover = self.fixture()[1]
        takeover["payload"]["alreadyDeliveredCancelled"] = True
        with self.assertRaises(ContractError):
            parse_port(takeover)
