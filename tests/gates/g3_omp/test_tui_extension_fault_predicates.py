"""Independent negative controls for two-TUI host outage and recovery evidence.

The doubles exercise the probe's decisions, not OMP itself. In particular, the
outage has no delivery attempt, so it cannot prove a production withholding policy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import live_tui_extension_fault_probe as probe
import live_tui_message_kinds_probe as kinds_probe


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


class _Socket:
    def __init__(self, bridge: "_Bridge", role: str) -> None:
        self.bridge = bridge
        self.role = role
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)
        self.bridge.bad_frame_seen = True
        defect = self.bridge.defect
        if defect == "malformed_ack":
            match = re.search(rb'"requestId":"([^"]+)"', data)
            assert match is not None
            self.bridge.acks[match.group(1).decode()] = {"status": "api_accepted"}
        if defect == "malformed_provider":
            self.bridge.providers["worker"].RequestHandlerClass.requests += 1
        if defect == "malformed_end":
            self.bridge.events.append({"role": "worker", "name": "agent_end",
                                       "sessionId": self.bridge.peers["worker"]["session_id"],
                                       "generation": 1})
        if defect == "malformed_worker_socket_changed":
            self.bridge.peers["worker"] = {
                **self.bridge.peers["worker"], "socket": _Socket(self.bridge, "worker"),
            }
        if defect == "malformed_manager_socket_changed":
            self.bridge.peers["manager"] = {
                **self.bridge.peers["manager"], "socket": _Socket(self.bridge, "manager"),
            }

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        self.bridge.peers.pop(self.role, None)


class _Bridge:
    def __init__(self, path: Path, providers: dict[str, _Provider],
                 previous: "_Bridge | None", defect: str) -> None:
        path.touch()
        self.path = path
        self.providers = providers
        self.previous = previous
        self.defect = defect
        self.condition = threading.Condition()
        self.events: list[dict[str, object]] = []
        self.deliveries: list[tuple[str, dict[str, object]]] = []
        self.acks: dict[str, dict[str, object]] = {}
        self.bad_frame_seen = False
        self.peers: dict[str, dict[str, object]] = {}
        for index, role in enumerate(probe.ROLES):
            old = previous.peers_at_start[role] if previous else None
            self.peers[role] = {
                "pid": (old["pid"] if old else 900001 + index)
                       + (1 if previous and defect == "new_pid" and role == "worker" else 0),
                "session_id": (old["session_id"] if old else str(uuid4()))
                              if not (previous and defect == "new_session" and role == "worker")
                              else str(uuid4()),
                "generation": (old["generation"] if old else 1)
                              + (1 if previous and defect == "new_generation" and role == "worker" else 0),
                "socket": old["socket"] if previous and defect == "old_socket" and role == "worker"
                          else _Socket(self, role),
                "write_lock": threading.Lock(),
            }
        if not previous and defect == "same_initial_pid":
            self.peers["worker"]["pid"] = self.peers["manager"]["pid"]
        if not previous and defect == "same_initial_session":
            self.peers["worker"]["session_id"] = self.peers["manager"]["session_id"]
        if not previous and defect == "wrong_initial_generation":
            self.peers["worker"]["generation"] = 2
        self.peers_at_start = {role: dict(peer) for role, peer in self.peers.items()}
        if previous and defect == "replay_on_reconnect":
            providers["worker"].RequestHandlerClass.requests += 1
        if previous and defect == "stale_end_on_reconnect":
            self.events.append({"role": "worker", "name": "agent_end",
                                "sessionId": self.peers["worker"]["session_id"],
                                "generation": self.peers["worker"]["generation"]})

    def serve_forever(self) -> None:
        pass

    def peer(self, role: str, **_kwargs: object) -> dict[str, object]:
        return self.peers[role]

    def request(self, role: str, frame: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if frame["kind"] == "probe":
            peer = self.peers[role]
            malformed_state_changed = self.bad_frame_seen and (
                (role == "worker" and self.defect == "malformed_worker_state_changed")
                or (role == "manager" and self.defect == "malformed_manager_state_changed")
            )
            return {"state": {
                "sessionId": str(uuid4()) if malformed_state_changed or (
                    self.previous and self.defect == "stale_public_session" and role == "worker"
                ) else peer["session_id"],
                "generation": peer["generation"],
                "idle": not ((self.previous and self.defect == "not_ready" and role == "worker")
                             or (self.bad_frame_seen and self.defect == "malformed_public_not_ready"
                                 and role == "worker")),
                "pending": False, "editorKnown": True, "editorEmpty": True,
            }}

        envelope = json.loads(str(frame["envelope"]))
        self.deliveries.append((role, envelope))
        target = self.providers[role]
        other = "manager" if role == "worker" else "worker"
        fault = self.defect == "wrong_provider" and role == "worker" and self.previous is None
        observed_provider = self.providers[other] if fault else target
        fresh_after_malformed = self.bad_frame_seen and len(self.deliveries) == 3
        observed_provider.RequestHandlerClass.requests += (
            2 if (self.defect == "duplicate_fresh_provider" and self.previous)
            or (fresh_after_malformed and self.defect == "malformed_fresh_duplicate") else 1
        )
        message_id = envelope["messageId"]
        expected = target.semantic_expected[message_id]
        seen_id = (str(uuid4()) if (
            (self.defect == "wrong_message_id" and role == "worker")
            or (fresh_after_malformed and self.defect == "malformed_fresh_wrong_id")
        ) else message_id)
        matched = not (self.defect == "wrong_provider_fields" and role == "worker")
        observed_provider.semantic_seen[seen_id] = {"matched": matched and (
            expected["kind"] == envelope["event"]["messageKind"]
            and expected["task_id"] == envelope["taskId"]
            and expected["revision_id"] == envelope["revisionId"]
            and expected["run_id"] == envelope["runId"]
            and expected.get("in_reply_to_message_id") == envelope["event"].get("inReplyToMessageId")
        )}
        peer = self.peers[role]
        event = {"role": role, "name": "agent_end",
                 "sessionId": peer["session_id"], "generation": peer["generation"]}
        if self.defect == "wrong_end_session" and role == "worker":
            event["sessionId"] = str(uuid4())
        if self.defect == "wrong_end_generation" and role == "worker":
            event["generation"] = 2
        if fresh_after_malformed and self.defect == "malformed_fresh_wrong_end_session":
            event["sessionId"] = str(uuid4())
        with self.condition:
            self.events.append(event)
            self.condition.notify_all()
        return {"status": "api_accepted", "modelProcessed": False}

    def event_count(self, role: str, name: str) -> int:
        return sum(event["role"] == role and event["name"] == name for event in self.events)

    def shutdown(self) -> None:
        pass

    def server_close(self) -> None:
        pass


class ExtensionFaultOutcomeTests(unittest.TestCase):
    def _run_case(self, defect: str = "", *, malformed_frame: bool = False
                  ) -> tuple[dict[str, object], list[_Bridge], dict[str, _Provider], list[bool]]:
        providers: dict[str, _Provider] = {}
        bridges: list[_Bridge] = []
        outage_screen_checks: list[bool] = []
        stopped = False

        def provider_factory(**_kwargs: object) -> _Provider:
            role = probe.ROLES[len(providers)]
            providers[role] = _Provider()
            return providers[role]

        def bridge_factory(path: Path, _tokens: object) -> _Bridge:
            bridge = _Bridge(path, providers, bridges[-1] if bridges else None, defect)
            bridges.append(bridge)
            return bridge

        def start(_omp: str, role: str, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"pid": bridges[0].peers_at_start[role]["pid"],
                    "fd": os.open(os.devnull, os.O_WRONLY)}

        def stop(_children: object) -> None:
            nonlocal stopped
            stopped = defect != "residual_child"

        original_exists = Path.exists

        def exists(path: Path) -> bool:
            if str(path).startswith("/proc/"):
                return not stopped and defect != "dead_tui_during_fault"
            return original_exists(path)

        def screen(*_args: object, present: bool, **_kwargs: object) -> bool:
            # The first check occurs after unlink and after old accepted peers close.
            boundary_reached = (bridges[0].bad_frame_seen and bridges[0].path.exists()) if malformed_frame else (
                not bridges[0].path.exists() and not bridges[0].peers
            )
            outage_screen_checks.append(boundary_reached)
            if not malformed_frame and present and defect == "spontaneous_provider_during_fault":
                providers["worker"].RequestHandlerClass.requests += 1
            if not malformed_frame and present and defect == "spontaneous_end_during_fault":
                bridges[0].events.append({"role": "worker", "name": "agent_end",
                                          "sessionId": bridges[0].peers_at_start["worker"]["session_id"],
                                          "generation": 1})
            if defect == "composer_input_missing" and present:
                return False
            if defect == "composer_not_cleared" and not present:
                return False
            if defect == "malformed_composer_input_missing" and present:
                return False
            if defect == "malformed_composer_not_cleared" and not present:
                return False
            return True

        with (
            patch.object(probe, "semantic_provider", side_effect=provider_factory),
            patch.object(probe, "BridgeHarness", side_effect=bridge_factory),
            patch.object(probe, "_start_omp", side_effect=start),
            patch.object(probe, "_stop_omps", side_effect=stop),
            patch.object(probe, "_drain_visible"),
            patch.object(probe, "_screen_state", return_value={"composer_visible": True}),
            patch.object(kinds_probe, "_screen_state", return_value={"composer_visible": True}),
            patch.object(probe, "_ready", side_effect=lambda server, screens, peers:
                         kinds_probe._ready(server, screens, peers, timeout=0.02)),
            patch.object(probe, "_wait_screen", side_effect=screen),
            patch.object(probe.os, "chmod"),
            patch.object(probe.Path, "exists", autospec=True, side_effect=exists),
            patch.object(probe, "cwd_processes", return_value=[]),
        ):
            outcome = probe.run("unused-omp", malformed_frame=malformed_frame)
        return outcome, bridges, providers, outage_screen_checks

    def test_host_outage_and_fresh_worker_only_recovery(self) -> None:
        outcome, bridges, providers, screen_checks = self._run_case()
        self.assertEqual(outcome["result"], "passed_pair_tui_extension_fault")
        self.assertEqual(len(bridges), 2)
        self.assertEqual(screen_checks, [True, True])
        self.assertEqual([role for role, _ in bridges[0].deliveries], ["worker", "manager"])
        self.assertEqual([role for role, _ in bridges[1].deliveries], ["worker"])
        self.assertEqual({role: providers[role].RequestHandlerClass.requests
                          for role in probe.ROLES}, {"manager": 1, "worker": 2})
        self.assertEqual(outcome["omp_children_remaining"], 0)

    def test_distinct_initial_process_and_session_binding_required(self) -> None:
        for defect in ("same_initial_pid", "same_initial_session", "wrong_initial_generation"):
            with self.subTest(defect=defect):
                outcome, _, _, _ = self._run_case(defect)
                self.assertNotEqual(outcome["result"], "passed_pair_tui_extension_fault")

    def test_current_provider_identity_and_agent_end_required(self) -> None:
        for defect in ("wrong_provider", "wrong_message_id", "wrong_provider_fields",
                       "wrong_end_session", "wrong_end_generation"):
            with self.subTest(defect=defect):
                outcome, _, _, _ = self._run_case(defect)
                self.assertNotEqual(outcome["result"], "passed_pair_tui_extension_fault")

    def test_composer_input_and_clear_required_during_outage(self) -> None:
        for defect in ("composer_input_missing", "composer_not_cleared", "dead_tui_during_fault"):
            with self.subTest(defect=defect):
                outcome, _, _, _ = self._run_case(defect)
                self.assertEqual(outcome["result"], "outage_observation_unknown")

    def test_spontaneous_processing_during_outage_is_rejected(self) -> None:
        for defect in ("spontaneous_provider_during_fault", "spontaneous_end_during_fault"):
            with self.subTest(defect=defect):
                outcome, _, _, _ = self._run_case(defect)
                self.assertEqual(outcome["result"], "outage_observation_unknown")

    def test_recovery_preserves_identity_with_new_socket_and_current_public_state(self) -> None:
        for defect in ("new_pid", "new_session", "new_generation", "old_socket",
                       "stale_public_session", "not_ready"):
            with self.subTest(defect=defect):
                outcome, _, _, _ = self._run_case(defect)
                self.assertEqual(outcome["result"], "recovery_observation_unknown")

    def test_fresh_processing_must_be_single_current_worker_delivery(self) -> None:
        outcome, _, _, _ = self._run_case("duplicate_fresh_provider")
        self.assertEqual(outcome["result"], "post_recovery_delivery_unknown")

    def test_reconnect_does_not_replay_prior_delivery(self) -> None:
        for defect in ("replay_on_reconnect", "stale_end_on_reconnect"):
            with self.subTest(defect=defect):
                outcome, _, _, _ = self._run_case(defect)
                self.assertEqual(outcome["result"], "recovery_observation_unknown")

    def test_cleanup_residual_is_reported_separately_from_precleanup_label(self) -> None:
        outcome, _, _, _ = self._run_case("residual_child")
        self.assertEqual(outcome["result"], "passed_pair_tui_extension_fault")
        self.assertGreater(outcome["omp_children_remaining"], 0)

    def test_malformed_frame_roundtrip_keeps_both_bindings_and_fresh_worker_only(self) -> None:
        outcome, bridges, providers, screen_checks = self._run_case(malformed_frame=True)
        self.assertEqual(outcome["result"], "passed_pair_tui_malformed_frame")
        self.assertEqual(len(bridges), 1)
        self.assertEqual(screen_checks, [True, True])
        worker_socket = bridges[0].peers_at_start["worker"]["socket"]
        self.assertEqual(len(worker_socket.sent), 1)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(worker_socket.sent[0])
        self.assertEqual([role for role, _ in bridges[0].deliveries],
                         ["worker", "manager", "worker"])
        self.assertEqual(bridges[0].acks, {})
        self.assertEqual({role: providers[role].RequestHandlerClass.requests
                          for role in probe.ROLES}, {"manager": 1, "worker": 2})
        self.assertEqual(outcome["final_agent_ends"], {"manager": 1, "worker": 2})
        self.assertEqual(outcome["omp_children_remaining"], 0)

    def test_malformed_frame_cannot_ack_or_process(self) -> None:
        for defect in ("malformed_ack", "malformed_provider", "malformed_end"):
            with self.subTest(defect=defect):
                outcome, bridges, _, _ = self._run_case(defect, malformed_frame=True)
                self.assertEqual(outcome["result"], "malformed_frame_observation_unknown")
                self.assertEqual(len(bridges[0].deliveries), 2)

    def test_malformed_frame_must_preserve_current_role_bindings_and_public_state(self) -> None:
        for defect in ("malformed_worker_socket_changed", "malformed_manager_socket_changed",
                       "malformed_worker_state_changed", "malformed_manager_state_changed",
                       "malformed_public_not_ready"):
            with self.subTest(defect=defect):
                outcome, _, _, _ = self._run_case(defect, malformed_frame=True)
                self.assertEqual(outcome["result"], "malformed_frame_observation_unknown")

    def test_malformed_frame_must_leave_worker_composer_usable_and_clearable(self) -> None:
        for defect in ("malformed_composer_input_missing", "malformed_composer_not_cleared"):
            with self.subTest(defect=defect):
                outcome, _, _, _ = self._run_case(defect, malformed_frame=True)
                self.assertEqual(outcome["result"], "malformed_frame_observation_unknown")

    def test_fresh_worker_after_malformed_needs_exactly_one_current_processing(self) -> None:
        for defect in ("malformed_fresh_duplicate", "malformed_fresh_wrong_id",
                       "malformed_fresh_wrong_end_session"):
            with self.subTest(defect=defect):
                outcome, _, _, _ = self._run_case(defect, malformed_frame=True)
                self.assertEqual(outcome["result"], "post_malformed_delivery_unknown")

    def test_malformed_precleanup_label_does_not_hide_residual_child(self) -> None:
        outcome, _, _, _ = self._run_case("residual_child", malformed_frame=True)
        self.assertEqual(outcome["result"], "passed_pair_tui_malformed_frame")
        self.assertGreater(outcome["omp_children_remaining"], 0)


if __name__ == "__main__":
    unittest.main()
