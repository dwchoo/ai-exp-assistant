"""Public G3 worker-response port correlation and fail-closed checks."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from live_workflow_probe import _frame_from_contract
from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.workflow import G3WorkerResponsePort


def identifier():
    return str(uuid4())


class PublicEvents:
    def __init__(self, events):
        self.events = events

    def event_cursor(self):
        return 0

    def wait_any_event(self, role, names, expected, *, after_sequence, timeout):
        assert role.value == "worker" and timeout > 0
        for event in self.events:
            if (event["bridgeSequence"] > after_sequence and event["name"] in names
                    and all(event.get(key) == value for key, value in expected.items())):
                return dict(event)
        raise TimeoutError("no matching public event")


class WorkerResponsePortTests(unittest.TestCase):
    def fixture(self):
        message = SimpleNamespace(task_id=identifier(), revision_id=identifier(),
                                  revision=1, run_id=identifier(), message_id=identifier())
        receipt = SimpleNamespace(message_id=message.message_id,
                                  delivery_attempt_id=identifier(), session_id=identifier(),
                                  session_generation=1, status=MailboxStatus.OMP_PROCESSED)
        identity = {"messageId": message.message_id,
                    "deliveryAttemptId": receipt.delivery_attempt_id,
                    "taskId": message.task_id, "revisionId": message.revision_id,
                    "runId": message.run_id, "sessionId": receipt.session_id,
                    "generation": receipt.session_generation}
        response = {"stage": "execute", "kind": "task", "task_id": message.task_id,
                    "revision_id": message.revision_id, "revision": 1,
                    "run_id": message.run_id, "message_id": message.message_id,
                    "delivery_attempt_id": receipt.delivery_attempt_id,
                    "session_id": receipt.session_id, "session_generation": 1,
                    "response_id": identifier(), "decision": "execute"}
        assistant = {**identity, "name": "assistant_message_end", "bridgeSequence": 1,
                     "workerResponse": response}
        processed = {**identity, "name": "delivery_omp_processed", "bridgeSequence": 2,
                     "workerResponseId": response["response_id"]}
        return message, receipt, assistant, processed

    def test_exact_public_events_return_only_allowlisted_decision(self):
        message, receipt, assistant, processed = self.fixture()
        port = G3WorkerResponsePort(PublicEvents([assistant, processed]))
        port.arm("execute", message)
        result = port.observe("execute", message, receipt)
        self.assertEqual(result["response_id"], assistant["workerResponse"]["response_id"])
        self.assertEqual(result["assistant_event_sequence"], 1)
        self.assertEqual(result["delivery_event_sequence"], 2)
        self.assertEqual(result["decision"], "execute")
        self.assertNotIn("workerResponse", result)
        with self.assertRaises(ValueError):
            port.observe("execute", message, receipt)

    def test_rejected_missing_or_unlinked_response_never_authorizes(self):
        for variant in ("rejected", "missing", "unlinked", "wrong_identity", "extra_field", "wrong_kind"):
            with self.subTest(variant=variant):
                message, receipt, assistant, processed = self.fixture()
                if variant == "rejected":
                    assistant = {key: value for key, value in assistant.items() if key != "workerResponse"}
                    assistant["name"] = "worker_response_rejected"
                elif variant == "missing":
                    assistant = processed
                elif variant == "unlinked":
                    processed["workerResponseId"] = identifier()
                elif variant == "wrong_identity":
                    assistant["workerResponse"] = {**assistant["workerResponse"], "message_id": identifier()}
                elif variant == "extra_field":
                    assistant["workerResponse"] = {**assistant["workerResponse"], "raw_text": "SECRET"}
                elif variant == "wrong_kind":
                    assistant["workerResponse"] = {**assistant["workerResponse"], "kind": "question"}
                port = G3WorkerResponsePort(PublicEvents([assistant, processed]))
                port.arm("execute", message)
                with self.assertRaises((ValueError, TimeoutError)):
                    port.observe("execute", message, receipt)

    def test_unprocessed_receipt_and_repeated_arm_are_rejected(self):
        message, receipt, assistant, processed = self.fixture()
        port = G3WorkerResponsePort(PublicEvents([assistant, processed]))
        port.arm("execute", message)
        with self.assertRaises(ValueError):
            port.arm("execute", message)
        receipt.status = MailboxStatus.UNKNOWN
        with self.assertRaises(ValueError):
            port.observe("execute", message, receipt)

    def test_response_id_cannot_be_reused_for_another_delivery(self):
        first_message, first_receipt, first_assistant, first_processed = self.fixture()
        second_message, second_receipt, second_assistant, second_processed = self.fixture()
        reused_id = first_assistant["workerResponse"]["response_id"]
        second_assistant["workerResponse"]["response_id"] = reused_id
        second_processed["workerResponseId"] = reused_id
        port = G3WorkerResponsePort(PublicEvents([
            first_assistant, first_processed, second_assistant, second_processed]))
        port.arm("execute", first_message)
        port.arm("execute", second_message)
        port.observe("execute", first_message, first_receipt)
        with self.assertRaises(ValueError):
            port.observe("execute", second_message, second_receipt)

    def test_scripted_provider_derives_frame_from_contract_and_rejects_mutations(self):
        message, receipt, _assistant, _processed = self.fixture()
        fields = [
            {"name": "stage", "type": "literal_string", "value": "execute"},
            {"name": "kind", "type": "literal_string", "value": "task"},
            {"name": "task_id", "type": "canonical_uuid", "value": message.task_id},
            {"name": "revision_id", "type": "canonical_uuid", "value": message.revision_id},
            {"name": "revision", "type": "positive_safe_integer", "value": 1},
            {"name": "run_id", "type": "canonical_uuid", "value": message.run_id},
            {"name": "message_id", "type": "canonical_uuid", "value": message.message_id},
            {"name": "delivery_attempt_id", "type": "canonical_uuid", "value": receipt.delivery_attempt_id},
            {"name": "session_id", "type": "canonical_uuid", "value": receipt.session_id},
            {"name": "session_generation", "type": "positive_safe_integer", "value": 1},
            {"name": "response_id", "type": "canonical_uuid", "generate": "canonical_uuid"},
            {"name": "decision", "type": "enum_string", "allowed": ["execute", "hold"]},
        ]
        contract = {"version": 1, "marker": "WB_WORKER_RESPONSE:",
                    "format": "marker_plus_compact_flat_json",
                    "field_order": [field["name"] for field in fields], "fields": fields,
                    "output_rules": {"exactly_one_frame": True, "no_prose": True,
                                     "no_tools": True, "no_thinking": True,
                                     "no_markdown": True, "no_extra_content": True},
                    "instruction": "Emit only the marker immediately followed by one compact flat JSON object in field_order; no prose, tools, thinking, markdown, whitespace, or additional messages."}
        observed = {"response_contract": contract}
        frame = _frame_from_contract(observed, "execute")
        self.assertIsInstance(frame, str)
        self.assertTrue(frame.startswith(contract["marker"]))
        self.assertEqual(list(json.loads(frame[len(contract["marker"]):])), contract["field_order"])
        self.assertEqual(json.loads(frame[len(contract["marker"]):])["task_id"], message.task_id)
        self.assertIsNone(_frame_from_contract({}, "execute"))

        def changed(update):
            copy = deepcopy(contract)
            update(copy)
            return _frame_from_contract({"response_contract": copy}, "execute")

        marker_frame = changed(lambda item: item.__setitem__("marker", "OTHER_RESPONSE:"))
        self.assertTrue(marker_frame.startswith("OTHER_RESPONSE:"))
        self.assertFalse(marker_frame.startswith("WB_WORKER_RESPONSE:"))
        self.assertIsNone(changed(lambda item: item["field_order"].reverse()))
        self.assertIsNone(changed(lambda item: item["fields"].pop(0)))
        self.assertIsNone(changed(lambda item: item.__setitem__("unknown_schema", True)))
        changed_identity = changed(lambda item: item["fields"][2].__setitem__("value", identifier()))
        self.assertNotEqual(json.loads(changed_identity[len(contract["marker"]):])["task_id"], message.task_id)
        self.assertIsNone(changed(lambda item: item["fields"][-1].__setitem__("allowed", ["hold"])))
        self.assertIsNone(changed(lambda item: item["fields"][-2].pop("generate")))
        self.assertIsNone(changed(lambda item: item.__setitem__("instruction", "Answer in prose.")))
        self.assertIsNone(changed(lambda item: item.__setitem__("output_rules", {"no_prose": False})))


if __name__ == "__main__":
    unittest.main()
