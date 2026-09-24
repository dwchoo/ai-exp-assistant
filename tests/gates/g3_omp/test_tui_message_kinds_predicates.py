"""Independent negative controls for four-kind delivery to two live TUI roles.

The doubles replace external OMP/provider observations. They do not establish
that real OMP emits those observations; the live probe owns that evidence.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import live_tui_message_kinds_probe as probe


class _Provider:
    server_port = 12345

    def __init__(self) -> None:
        self.RequestHandlerClass = type("Handler", (), {"requests": 0})
        self.semantic_expected: dict[str, dict[str, object]] = {}
        self.semantic_seen: dict[str, dict[str, bool]] = {}

    def serve_forever(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def server_close(self) -> None:
        pass


class _Bridge:
    def __init__(self, providers: dict[str, _Provider], defect: str = "") -> None:
        self.providers = providers
        self.defect = defect
        self.condition = threading.Condition()
        self.events: list[dict[str, object]] = []
        self.deliveries: list[tuple[str, dict[str, object]]] = []
        self.peers = {
            role: {"pid": 990001 + index, "session_id": str(uuid4()),
                   "generation": 1}
            for index, role in enumerate(probe.ROLES)
        }
        self.after_delivery_ready = True

    def serve_forever(self) -> None:
        pass

    def peer(self, role: str, **_kwargs: object) -> dict[str, object]:
        return self.peers[role]

    def request(self, role: str, frame: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if frame["kind"] == "probe":
            peer = self.peers[role]
            return {"state": {
                "sessionId": peer["session_id"], "generation": peer["generation"],
                "idle": self.after_delivery_ready, "pending": False,
                "editorKnown": True, "editorEmpty": True,
            }}

        envelope = json.loads(str(frame["envelope"]))
        self.deliveries.append((role, envelope))
        index = len(self.deliveries)
        message_id = envelope["messageId"]
        kind = envelope["event"]["messageKind"]
        other = "manager" if role == "worker" else "worker"
        fault_index = 2 if self.defect == "stale_context" else (
            4 if self.defect == "wrong_report_reply_id" else 3
        )
        faulty = index == fault_index
        provider_role = other if faulty and self.defect == "wrong_target" else role
        provider = self.providers[provider_role]
        if not (faulty and self.defect == "ack_without_processing"):
            provider.RequestHandlerClass.requests += 2 if faulty and self.defect == "duplicate_provider" else 1
            if not (faulty and self.defect == "omitted_kind"):
                seen_id = str(uuid4()) if faulty and self.defect == "wrong_current_id" else message_id
                if faulty and self.defect == "stale_context":
                    seen_id = self.deliveries[0][1]["messageId"]
                expected = self.providers[role].semantic_expected[message_id]
                observed = dict(expected)
                if faulty and self.defect in ("wrong_reply_id", "wrong_report_reply_id"):
                    observed["in_reply_to_message_id"] = str(uuid4())
                if faulty and self.defect == "wrong_current_fields":
                    observed["task_id"] = str(uuid4())
                provider.semantic_seen[seen_id] = {"matched": observed == expected}
        peer = self.peers[role]
        event = {"role": role, "name": "agent_end",
                 "sessionId": peer["session_id"], "generation": peer["generation"]}
        if faulty and self.defect == "wrong_session_end":
            event["sessionId"] = str(uuid4())
        if faulty and self.defect == "wrong_generation_end":
            event["generation"] = 2
        with self.condition:
            self.events.append(event)
            if faulty and self.defect == "cross_role_end":
                self.events.append({**event, "role": other})
            self.condition.notify_all()
        return {"status": "api_accepted", "modelProcessed": False}

    def event_count(self, role: str, name: str) -> int:
        return sum(event.get("role") == role and event.get("name") == name
                   for event in self.events)

    def shutdown(self) -> None:
        pass

    def server_close(self) -> None:
        pass


class FourKindOutcomeTests(unittest.TestCase):
    def _run_case(self, defect: str = "") -> tuple[dict[str, object], _Bridge]:
        providers: dict[str, _Provider] = {}

        def provider_factory(**_kwargs: object) -> _Provider:
            role = probe.ROLES[len(providers)]
            providers[role] = _Provider()
            return providers[role]

        holder: list[_Bridge] = []

        def bridge_factory(*_args: object) -> _Bridge:
            bridge = _Bridge(providers, defect)
            holder.append(bridge)
            return bridge

        def start(_omp: str, role: str, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"pid": holder[0].peers[role]["pid"],
                    "fd": os.open(os.devnull, os.O_WRONLY)}

        with (
            patch.object(probe, "semantic_provider", side_effect=provider_factory),
            patch.object(probe, "BridgeHarness", side_effect=bridge_factory),
            patch.object(probe, "_start_omp", side_effect=start),
            patch.object(probe, "_stop_omps"),
            patch.object(probe, "_drain_visible"),
            patch.object(probe, "_screen_state", return_value={"composer_visible": True}),
            patch.object(probe.os, "chmod"),
        ):
            outcome = probe.run("unused-omp")
        return outcome, holder[0]

    def test_four_distinct_kinds_reach_their_own_role_and_reply_chain(self) -> None:
        outcome, bridge = self._run_case()

        self.assertEqual(outcome["result"], "passed_pair_tui_four_kinds")
        self.assertEqual([record["kind"] for record in outcome["records"]],
                         ["task", "question", "answer", "report"])
        self.assertEqual([role for role, _ in bridge.deliveries],
                         ["worker", "worker", "manager", "manager"])
        self.assertEqual(outcome["records"][2]["reply_to_message_id"],
                         outcome["records"][1]["message_id"])
        self.assertEqual(outcome["records"][3]["reply_to_message_id"],
                         outcome["records"][0]["message_id"])

    def test_missing_kind_or_wrong_target_cannot_pass(self) -> None:
        for defect in ("omitted_kind", "wrong_target"):
            with self.subTest(defect=defect):
                outcome, _ = self._run_case(defect)
                self.assertEqual(outcome["result"], "delivery_observation_unknown")

    def test_current_identity_and_reply_linkage_are_required(self) -> None:
        for defect in ("wrong_current_id", "wrong_current_fields",
                       "wrong_reply_id", "wrong_report_reply_id", "stale_context"):
            with self.subTest(defect=defect):
                outcome, _ = self._run_case(defect)
                self.assertEqual(outcome["result"], "delivery_observation_unknown")

    def test_api_return_and_duplicate_provider_are_not_processing_proof(self) -> None:
        for defect in ("ack_without_processing", "duplicate_provider"):
            with self.subTest(defect=defect):
                outcome, _ = self._run_case(defect)
                self.assertEqual(outcome["result"], "delivery_observation_unknown")

    def test_event_must_belong_to_current_target_session_and_generation(self) -> None:
        for defect in ("cross_role_end", "wrong_session_end", "wrong_generation_end"):
            with self.subTest(defect=defect):
                outcome, _ = self._run_case(defect)
                self.assertEqual(outcome["result"], "delivery_observation_unknown")

    def test_stale_or_other_role_end_does_not_complete_current_delivery(self) -> None:
        provider = _Provider()
        bridge = _Bridge({"manager": provider, "worker": _Provider()})
        peer = bridge.peers["worker"]
        bridge.events.append({"role": "worker", "name": "agent_end",
                              "sessionId": peer["session_id"],
                              "generation": peer["generation"]})
        cursor = len(bridge.events)
        bridge.events.append({"role": "manager", "name": "agent_end",
                              "sessionId": bridge.peers["manager"]["session_id"],
                              "generation": 1})

        self.assertEqual(probe._new_end(bridge, "worker", peer, cursor, timeout=0.01),
                         (0, False))

    def test_initial_and_after_delivery_readiness_need_current_public_state_and_composer(self) -> None:
        bridge = _Bridge({role: _Provider() for role in probe.ROLES})
        screens = {role: (object(), threading.Lock()) for role in probe.ROLES}
        with patch.object(probe, "_screen_state", return_value={"composer_visible": True}):
            self.assertTrue(probe._ready(bridge, screens, bridge.peers, timeout=0.01))
            bridge.after_delivery_ready = False
            self.assertFalse(probe._ready(bridge, screens, bridge.peers, timeout=0.01))
            bridge.after_delivery_ready = True
            bridge.peers["worker"] = {**bridge.peers["worker"], "session_id": str(uuid4())}
            self.assertFalse(probe._ready(bridge, screens, {
                **bridge.peers, "worker": {**bridge.peers["worker"], "session_id": "old"},
            }, timeout=0.01))
        with patch.object(probe, "_screen_state", return_value={"composer_visible": False}):
            self.assertFalse(probe._ready(bridge, screens, bridge.peers, timeout=0.01))


if __name__ == "__main__":
    unittest.main()
