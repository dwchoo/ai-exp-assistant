"""Independent negative controls for the real TUI /new session probe."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import live_tui_draft_probe as probe


class SessionSwitchOutcomeTests(unittest.TestCase):
    def _run_case(
        self, *, session_changes: bool = True, generation_increments: bool = True,
        connection_changes: bool = True, peer_absences: int = 0,
        leak_old_envelope: bool = False, wrong_provider_id: bool = False,
        fresh_agent_end: bool = True,
    ) -> tuple[dict[str, object], object]:
        class Provider:
            server_port = 12345

            class RequestHandlerClass:
                requests = 0

            def __init__(self):
                self.semantic_expected: dict[str, dict[str, object]] = {}
                self.semantic_seen: dict[str, dict[str, bool]] = {}

            def serve_forever(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        provider = Provider()
        old_session = str(uuid4())
        old_socket = object()
        old_peer = {"pid": 900002, "session_id": old_session,
                    "generation": 1, "socket": old_socket}
        new_peer = {"pid": old_peer["pid"],
                    "session_id": str(uuid4()) if session_changes else old_session,
                    "generation": 2 if generation_increments else 1,
                    "socket": object() if connection_changes else old_socket}

        class Bridge:
            def __init__(self, *_args):
                self.worker_peer_calls = 0
                self.remaining_absences = peer_absences
                self.probe_count = 0
                self.deliver_count = 0
                self.old_message_id = None
                self.events = [{"role": "worker", "name": "agent_end"}]
                self.event_cursor = 0

            def serve_forever(self):
                pass

            def peer(self, role, **_kwargs):
                if role == "manager":
                    return {"pid": 900001, "session_id": str(uuid4()),
                            "generation": 1, "socket": object()}
                self.worker_peer_calls += 1
                if self.worker_peer_calls == 1:
                    return old_peer
                if self.remaining_absences:
                    self.remaining_absences -= 1
                    raise TimeoutError("temporary bridge peer absence")
                if leak_old_envelope and self.worker_peer_calls == peer_absences + 2:
                    provider.RequestHandlerClass.requests += 1
                    provider.semantic_seen[self.old_message_id] = {"matched": True}
                return new_peer

            def request(self, _role, request, **_kwargs):
                if request["kind"] == "probe":
                    self.probe_count += 1
                    length = {2: 38, 3: 38, 5: 4}.get(self.probe_count, 0)
                    peer = new_peer if self.worker_peer_calls > 1 + peer_absences else old_peer
                    return {"state": {"editorKnown": True, "editorEmpty": length == 0,
                                      "editorLength": length, "idle": True, "pending": False,
                                      "sessionId": peer["session_id"],
                                      "generation": peer["generation"]}}
                self.deliver_count += 1
                envelope = json.loads(request["envelope"])
                message_id = envelope["messageId"]
                if self.deliver_count == 1:
                    self.old_message_id = message_id
                    return {"status": "deferred"}
                if self.deliver_count == 2:
                    return {"status": "rejected"}
                provider.RequestHandlerClass.requests += 1
                observed_id = str(uuid4()) if wrong_provider_id else message_id
                provider.semantic_seen[observed_id] = {"matched": True}
                if fresh_agent_end:
                    self.events.append({"role": "worker", "name": "agent_end"})
                return {"status": "api_accepted"}

            def wait_event(self, _role, _name, **_kwargs):
                if self.event_cursor < len(self.events):
                    event = self.events[self.event_cursor]
                    self.event_cursor += 1
                    return event
                raise TimeoutError("no new event")

            def shutdown(self):
                pass

            def server_close(self):
                pass

        bridge = Bridge()
        draft_screen_calls = 0

        def screen_state(_screen, _lock, draft="", **_kwargs):
            nonlocal draft_screen_calls
            if draft == "/new":
                return {"draft_in_composer": True}
            if draft:
                draft_screen_calls += 1
                visible = draft_screen_calls <= 2
                return {"draft_in_composer": visible, "draft_visible": visible}
            return {"composer_visible": True}

        def start(_omp, role, *_args, **_kwargs):
            return {"pid": 900001 if role == "manager" else 900002,
                    "fd": os.open(os.devnull, os.O_WRONLY)}

        clock = SimpleNamespace(now=100.0)

        def tick():
            clock.now += 0.25
            return clock.now

        with (
            patch.object(probe, "semantic_provider", return_value=provider),
            patch.object(probe, "BridgeHarness", return_value=bridge),
            patch.object(probe, "_start_omp", side_effect=start),
            patch.object(probe, "_stop_omps"),
            patch.object(probe, "_drain_visible"),
            patch.object(probe, "_screen_state", side_effect=screen_state),
            patch.object(probe.os, "chmod"),
            patch.object(probe.os, "write"),
            patch.object(probe.time, "monotonic", side_effect=tick),
            patch.object(probe.time, "sleep"),
        ):
            outcome = probe.run("unused-omp", session_switch=True)
        return outcome, bridge

    def test_real_transition_accepts_only_new_message_and_fresh_turn(self) -> None:
        outcome, bridge = self._run_case()

        self.assertEqual(outcome["result"], "passed_pair_tui_session_switch")
        self.assertEqual(outcome["provider_requests_before_new_message"], 0)
        self.assertEqual(outcome["provider_requests_after_new_message"], 1)
        self.assertEqual(bridge.deliver_count, 3)  # deferred, rejected, new delivery

    def test_same_pid_with_stale_session_cannot_pass(self) -> None:
        outcome, bridge = self._run_case(session_changes=False)

        self.assertEqual(outcome["result"], "tui_session_switch_not_observed")
        self.assertEqual(bridge.deliver_count, 1)

    def test_same_pid_with_stale_generation_cannot_pass(self) -> None:
        outcome, bridge = self._run_case(generation_increments=False)

        self.assertEqual(outcome["result"], "tui_session_switch_not_observed")
        self.assertEqual(bridge.deliver_count, 1)

    def test_same_socket_cannot_pass_even_if_session_and_generation_change(self) -> None:
        outcome, _ = self._run_case(connection_changes=False)

        self.assertFalse(outcome["session_transition"]["new_connection"])
        self.assertNotEqual(outcome["result"], "passed_pair_tui_session_switch")

    def test_old_deferred_envelope_must_not_leak_to_new_provider(self) -> None:
        outcome, _ = self._run_case(leak_old_envelope=True)

        self.assertTrue(outcome["old_provider_identity_seen"])
        self.assertNotEqual(outcome["result"], "passed_pair_tui_session_switch")

    def test_provider_request_for_wrong_message_id_cannot_pass(self) -> None:
        outcome, _ = self._run_case(wrong_provider_id=True)

        self.assertFalse(outcome["new_provider_identity_matched"])
        self.assertNotEqual(outcome["result"], "passed_pair_tui_session_switch")

    def test_stale_agent_end_cannot_complete_new_delivery(self) -> None:
        outcome, _ = self._run_case(fresh_agent_end=False)

        self.assertFalse(outcome["new_agent_end_observed"])
        self.assertNotEqual(outcome["result"], "passed_pair_tui_session_switch")

    def test_temporary_peer_absence_does_not_create_or_erase_transition(self) -> None:
        outcome, bridge = self._run_case(peer_absences=2)

        self.assertEqual(outcome["result"], "passed_pair_tui_session_switch")
        self.assertEqual(bridge.worker_peer_calls, 4)

    def test_temporary_peer_absence_alone_cannot_prove_transition(self) -> None:
        outcome, bridge = self._run_case(peer_absences=2, session_changes=False)

        self.assertEqual(outcome["result"], "tui_session_switch_not_observed")
        self.assertEqual(bridge.deliver_count, 1)


if __name__ == "__main__":
    unittest.main()
