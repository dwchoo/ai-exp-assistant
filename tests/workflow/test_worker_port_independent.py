"""Independent public-only CW-10 worker-response adapter regressions."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from workbench.contracts.v1 import ActorRole
from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.workflow import G3WorkerResponsePort
from live_workflow_probe import _worker_provider


def identity():
    message = SimpleNamespace(
        task_id=str(uuid4()), revision=1, revision_id=str(uuid4()),
        run_id=str(uuid4()), message_id=str(uuid4()),
    )
    receipt = SimpleNamespace(
        status=MailboxStatus.OMP_PROCESSED, message_id=message.message_id,
        delivery_attempt_id=str(uuid4()), session_id=str(uuid4()),
        session_generation=1,
    )
    response = {
        "stage": "execute", "kind": "task", "task_id": message.task_id,
        "revision_id": message.revision_id, "revision": 1, "run_id": message.run_id,
        "message_id": message.message_id, "delivery_attempt_id": receipt.delivery_attempt_id,
        "session_id": receipt.session_id, "session_generation": 1,
        "response_id": str(uuid4()), "decision": "execute",
    }
    return message, receipt, response


def event_pair(response, *, first_sequence=11, second_sequence=12):
    return [
        {"name": "assistant_message_end", "workerResponse": response,
         "bridgeSequence": first_sequence},
        {"name": "delivery_omp_processed", "workerResponseId": response["response_id"],
         "bridgeSequence": second_sequence},
    ]


class PublicEvents:
    """Only the public G3BridgeServer event_cursor/wait_any_event interface."""
    def __init__(self, events, *, cursor=10):
        self.events = list(events)
        self.cursor = cursor
        self.calls = []

    def event_cursor(self):
        return self.cursor

    def wait_any_event(self, role, names, expected, *, after_sequence, timeout):
        self.calls.append((role, names, dict(expected), after_sequence, timeout))
        if timeout <= 0:
            raise AssertionError("adapter passed an exhausted timeout")
        while self.events:
            event = self.events.pop(0)
            if event["bridgeSequence"] <= after_sequence:
                continue
            if event["name"] in names:
                return event
        raise TimeoutError("no matching public event")


class PublicWorkerPortIndependentTests(unittest.TestCase):
    def test_scripted_provider_uses_delivered_contract_without_fallback(self):
        """A real provider request must use changed contract data or fail closed."""
        server = _worker_provider()
        from threading import Thread
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def reply(contract):
            observed = {"workbench_message_id": str(uuid4()),
                        "payload": {"stage": "execute", "revision": 1}}
            if contract is not None:
                observed["response_contract"] = contract
            body = json.dumps({"messages": [{"role": "user", "content": json.dumps(observed)}]}).encode()
            request = Request(f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                              data=body, headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                chunks = [json.loads(line[6:]) for line in response.read().decode().splitlines()
                          if line.startswith("data: {")]
            return "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)

        fields = [
            {"name": "stage", "type": "literal_string", "value": "execute"},
            {"name": "kind", "type": "literal_string", "value": "task"},
            *({"name": name, "type": "canonical_uuid", "value": str(uuid4())}
              for name in ("task_id", "revision_id")),
            {"name": "revision", "type": "positive_safe_integer", "value": 1},
            *({"name": name, "type": "canonical_uuid", "value": str(uuid4())}
              for name in ("run_id", "message_id", "delivery_attempt_id", "session_id")),
            {"name": "session_generation", "type": "positive_safe_integer", "value": 1},
            {"name": "response_id", "type": "canonical_uuid", "generate": "canonical_uuid"},
            {"name": "decision", "type": "enum_string", "allowed": ["execute", "hold"]},
        ]
        contract = {
            "version": 1, "marker": "INDEPENDENT_MUTATED_MARKER:",
            "format": "marker_plus_compact_flat_json",
            "field_order": [field["name"] for field in fields], "fields": fields,
            "output_rules": {key: True for key in ("exactly_one_frame", "no_prose", "no_tools",
                                                    "no_thinking", "no_markdown", "no_extra_content")},
            "instruction": "Emit only the marker immediately followed by one compact flat JSON object in field_order; no prose, tools, thinking, markdown, whitespace, or additional messages.",
        }
        try:
            text = reply(contract)
            self.assertTrue(text.startswith(contract["marker"] + "{"))
            frame = json.loads(text[len(contract["marker"]):])
            self.assertEqual(list(frame), contract["field_order"])
            self.assertEqual(frame["decision"], "execute")
            for field in fields:
                if "value" in field:
                    self.assertEqual(frame[field["name"]], field["value"])
            self.assertEqual(str(UUID(frame["response_id"])), frame["response_id"])
            changed = {**contract, "fields": [dict(field) for field in fields]}
            changed["fields"][3]["value"] = str(uuid4())
            changed_frame = json.loads(reply(changed)[len(contract["marker"]):])
            self.assertEqual(changed_frame["revision_id"], changed["fields"][3]["value"])
            reordered = {**contract, "fields": [fields[1], fields[0], *fields[2:]],
                         "field_order": [fields[1]["name"], fields[0]["name"],
                                         *contract["field_order"][2:]]}
            self.assertEqual(list(json.loads(reply(reordered)[len(contract["marker"]):])),
                             reordered["field_order"])
            invalid = {
                "missing": None,
                "natural_language_only": {"instruction": "Please respond naturally"},
                "missing_descriptor": {**contract, "fields": fields[:-1]},
                "extra_descriptor": {**contract, "fields": [*fields, {"name": "extra", "type": "literal_string", "value": "x"}]},
                "order_mismatch": {**contract, "field_order": list(reversed(contract["field_order"]))},
                "identity_type": {**contract, "fields": [{**fields[0], "type": "canonical_uuid"}, *fields[1:]]},
                "decision_set": {**contract, "fields": [*fields[:-1], {**fields[-1], "allowed": ["success"]}]},
                "response_id_rule": {**contract, "fields": [*fields[:-2], {**fields[-2], "generate": "literal"}, fields[-1]]},
            }
            for label, variant in invalid.items():
                with self.subTest(label=label):
                    self.assertEqual(reply(variant), "invalid")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
    def test_valid_two_event_control_is_exactly_bound_and_one_shot(self):
        message, receipt, response = identity()
        bridge = PublicEvents(event_pair(response))
        port = G3WorkerResponsePort(bridge, timeout=2)
        port.arm("execute", message)
        verified = port.observe("execute", message, receipt)
        self.assertEqual(verified["response_id"], response["response_id"])
        self.assertEqual((verified["assistant_event_sequence"],
                          verified["delivery_event_sequence"]), (11, 12))
        self.assertNotIn("workerResponse", verified)
        self.assertEqual(len(bridge.calls), 2)
        for role, _names, expected, _cursor, timeout in bridge.calls:
            self.assertIs(role, ActorRole.WORKER)
            self.assertGreater(timeout, 0)
            self.assertEqual(expected, {
                "messageId": message.message_id,
                "deliveryAttemptId": receipt.delivery_attempt_id,
                "taskId": message.task_id, "revisionId": message.revision_id,
                "runId": message.run_id, "sessionId": receipt.session_id,
                "generation": receipt.session_generation,
            })
        self.assertEqual([call[3] for call in bridge.calls], [10, 11])
        with self.assertRaises(ValueError):
            port.observe("execute", message, receipt)
        with self.assertRaises(ValueError):
            port.arm("execute", SimpleNamespace(message_id="invalid-id"))

    def test_terminal_causality_and_identity_corruption_never_authorize(self):
        cases = {
            "processed_without_response": lambda response: [
                {"name": "delivery_omp_processed", "workerResponseId": response["response_id"],
                 "bridgeSequence": 11}],
            "response_without_processed": lambda response: event_pair(response)[:1],
            "unknown_before_response": lambda response: [
                {"name": "delivery_processing_unknown", "bridgeSequence": 11}, *event_pair(response, first_sequence=12, second_sequence=13)],
            "rejection_before_response": lambda response: [
                {"name": "worker_response_rejected", "bridgeSequence": 11}, *event_pair(response, first_sequence=12, second_sequence=13)],
            "mismatched_completion_id": lambda response: [
                event_pair(response)[0],
                {"name": "delivery_omp_processed", "workerResponseId": str(uuid4()),
                 "bridgeSequence": 12}],
            "inverse_sequence": lambda response: event_pair(response, first_sequence=12, second_sequence=11),
            "cursor_old_response": lambda response: event_pair(response, first_sequence=9, second_sequence=12),
            "second_response_before_completion": lambda response: [
                event_pair(response)[0],
                {"name": "assistant_message_end", "workerResponse": response,
                 "bridgeSequence": 12},
                {"name": "delivery_omp_processed", "workerResponseId": response["response_id"],
                 "bridgeSequence": 13}],
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                message, receipt, response = identity()
                bridge = PublicEvents(mutate(response))
                port = G3WorkerResponsePort(bridge, timeout=2)
                port.arm("execute", message)
                with self.assertRaises((ValueError, TimeoutError)):
                    port.observe("execute", message, receipt)
                with self.assertRaises(ValueError):
                    port.observe("execute", message, receipt)

    def test_public_response_shape_stage_and_wrong_delivery_are_rejected(self):
        changes = {
            "wrong_stage": {"stage": "analysis"},
            "wrong_kind": {"kind": "question"},
            "wrong_revision_id": {"revision_id": str(uuid4())},
            "wrong_task": {"task_id": str(uuid4())},
            "wrong_run": {"run_id": str(uuid4())},
            "wrong_message": {"message_id": str(uuid4())},
            "wrong_attempt": {"delivery_attempt_id": str(uuid4())},
            "wrong_session": {"session_id": str(uuid4())},
            "wrong_generation": {"session_generation": 2},
            "boolean_revision": {"revision": True},
            "float_generation": {"session_generation": 1.0},
            "negative_revision": {"revision": -1},
            "unsafe_generation": {"session_generation": 2**53},
            "malformed_uuid": {"response_id": "not-a-uuid"},
            "wrong_decision": {"decision": "success"},
            "secret_extra_field": {"private_reasoning": "CW10_PRIVATE_SENTINEL"},
        }
        for label, changed in changes.items():
            with self.subTest(label=label):
                message, receipt, response = identity()
                bridge = PublicEvents(event_pair({**response, **changed}))
                port = G3WorkerResponsePort(bridge)
                port.arm("execute", message)
                with self.assertRaises(ValueError):
                    port.observe("execute", message, receipt)
        for status in (MailboxStatus.UNKNOWN, MailboxStatus.DEFERRED):
            with self.subTest(receipt=status):
                message, receipt, response = identity()
                receipt.status = status
                port = G3WorkerResponsePort(PublicEvents(event_pair(response)))
                port.arm("execute", message)
                with self.assertRaises(ValueError):
                    port.observe("execute", message, receipt)

    def test_response_id_replay_and_duplicate_arm_fail_closed(self):
        first_message, first_receipt, first_response = identity()
        second_message, second_receipt, second_response = identity()
        second_response["response_id"] = first_response["response_id"]
        bridge = PublicEvents(event_pair(first_response))
        port = G3WorkerResponsePort(bridge)
        port.arm("execute", first_message)
        with self.assertRaises(ValueError):
            port.arm("execute", first_message)
        port.observe("execute", first_message, first_receipt)
        bridge.events = event_pair(second_response, first_sequence=13, second_sequence=14)
        bridge.cursor = 12
        port.arm("execute", second_message)
        with self.assertRaises(ValueError):
            port.observe("execute", second_message, second_receipt)

    def test_actual_workflow_probes_use_exported_public_port_not_provider_dictionary(self):
        root = Path(__file__).resolve().parents[2]
        for name in ("live_workflow_probe.py", "live_workflow_independent.py"):
            tree = ast.parse((root / "tests/workflow" / name).read_text())
            private_reads = [node for node in ast.walk(tree)
                             if isinstance(node, ast.Attribute) and node.attr == "responses"]
            self.assertFalse(private_reads, name)
            constructors = [node for node in ast.walk(tree)
                            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "G3WorkerResponsePort"]
            self.assertTrue(constructors, name)


if __name__ == "__main__":
    unittest.main()
