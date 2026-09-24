"""Independent negative controls for the two-TUI reconnect observation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import live_tui_reconnect_probe as probe


class _Socket:
    def __init__(self):
        self.sent: list[dict[str, object]] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(json.loads(data))

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        pass


class ReconnectOutcomeTests(unittest.TestCase):
    def _run_case(
        self, *, ack_status: str = "api_accepted", ack_discarded: bool = True,
        new_socket: bool = True, same_pid: bool = True, same_session: bool = True,
        same_generation: bool = True, duplicate_provider: bool = False,
        wrong_provider_identity: bool = False, duplicate_agent_end: bool = False,
        preexisting_agent_end: bool = False,
        composer_recovered: bool = True, public_state_ready: bool = True,
        draft_recovered: bool = True, draft_cleared: bool = True,
        cleanup_succeeds: bool = True,
    ) -> tuple[dict[str, object], object]:
        class Provider:
            server_port = 12345

            class RequestHandlerClass:
                requests = 0

            def __init__(self):
                self.semantic_expected: dict[str, object] = {}
                self.semantic_seen: dict[str, object] = {}

            def serve_forever(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        provider = Provider()
        old_peer = {"pid": 900002, "session_id": str(uuid4()), "generation": 1,
                    "socket": _Socket(), "write_lock": threading.Lock()}
        new_peer = {"pid": old_peer["pid"] if same_pid else 900003,
                    "session_id": old_peer["session_id"] if same_session else str(uuid4()),
                    "generation": old_peer["generation"] if same_generation else 2,
                    "socket": _Socket() if new_socket else old_peer["socket"],
                    "write_lock": threading.Lock()}
        manager_peer = {"pid": 900001, "session_id": str(uuid4()), "generation": 1,
                        "socket": _Socket(), "write_lock": threading.Lock()}

        class Bridge:
            def __init__(self, *_args):
                self.condition = threading.Condition()
                self.dropped_ack_ids: set[str] = set()
                self.discarded_ack_metadata: list[dict[str, object]] = []
                self.acks: dict[str, object] = {}
                self.reconnected = False
                self.worker_probe_count = 0
                self.deliveries = 0
                self.agent_ends = int(preexisting_agent_end)

            def serve_forever(self):
                pass

            def peer(self, role: str, **_kwargs):
                return manager_peer if role == "manager" else old_peer

            def wait_event(self, _role: str, _event: str, **_kwargs):
                frame = old_peer["socket"].sent[-1]
                envelope = json.loads(frame["envelope"])
                message_id = envelope["messageId"]
                provider.RequestHandlerClass.requests += 1
                provider.semantic_seen[
                    str(uuid4()) if wrong_provider_identity else message_id
                ] = {"matched": True}
                self.agent_ends += 1
                ack = {"request_id": frame["requestId"], "status": ack_status}
                if ack_discarded:
                    self.discarded_ack_metadata.append(ack)
                else:
                    self.acks[frame["requestId"]] = {"status": ack_status}
                return {"name": "agent_end"}

            def event_count(self, _role: str, _event: str) -> int:
                return self.agent_ends

            def request(self, role: str, frame: dict[str, object], **_kwargs):
                if frame["kind"] == "deliver":
                    self.deliveries += 1
                    if duplicate_provider:
                        provider.RequestHandlerClass.requests += 1
                    if not duplicate_agent_end:
                        return {"status": "duplicate_api_accepted"}
                    self.agent_ends += 1
                    return {"status": "duplicate_api_accepted"}
                if role == "worker":
                    self.worker_probe_count += 1
                peer = new_peer if self.reconnected and role == "worker" else (
                    manager_peer if role == "manager" else old_peer
                )
                state = {"sessionId": peer["session_id"],
                         "generation": peer["generation"], "idle": True,
                         "pending": False, "editorKnown": True, "editorEmpty": True}
                if self.reconnected and role == "worker" and not public_state_ready:
                    state["pending"] = True
                return {"state": state}

            def shutdown(self):
                pass

            def server_close(self):
                pass

        bridge = Bridge()
        children: list[dict[str, object]] = []
        stopped = False
        composer_calls = 0
        editor_calls = 0

        def start(_omp, role, *_args, **_kwargs):
            child = {"pid": manager_peer["pid"] if role == "manager" else old_peer["pid"],
                     "fd": os.open(os.devnull, os.O_WRONLY)}
            children.append(child)
            return child

        def reconnect(_server, _old_peer, **_kwargs):
            bridge.reconnected = True
            return new_peer

        def composer(*_args, **_kwargs):
            nonlocal composer_calls
            composer_calls += 1
            return composer_recovered if composer_calls == 3 else True

        def editor(*_args, **_kwargs):
            nonlocal editor_calls
            editor_calls += 1
            return draft_recovered if editor_calls == 1 else draft_cleared

        def stop(_children):
            nonlocal stopped
            stopped = cleanup_succeeds

        def exists(path: Path) -> bool:
            if str(path).startswith("/proc/"):
                return not stopped
            return Path.exists(path)

        with (
            patch.object(probe, "semantic_provider", return_value=provider),
            patch.object(probe, "BridgeHarness", return_value=bridge),
            patch.object(probe, "_start_omp", side_effect=start),
            patch.object(probe, "_stop_omps", side_effect=stop),
            patch.object(probe, "wait_reconnected", side_effect=reconnect),
            patch.object(probe, "_drain_visible"),
            patch.object(probe, "_wait_composer", side_effect=composer),
            patch.object(probe, "_wait_editor", side_effect=editor),
            patch.object(probe, "_screen_state", return_value={"composer_visible": True}),
            patch.object(probe.os, "chmod"),
            patch.object(probe.time, "sleep"),
            patch.object(probe.Path, "exists", autospec=True, side_effect=exists),
        ):
            outcome = probe.run("unused-omp")
        return outcome, bridge

    def test_only_one_processing_after_discarded_ack_and_reconnect(self) -> None:
        outcome, bridge = self._run_case()
        self.assertEqual(outcome["result"], "passed_pair_tui_reconnect")
        self.assertTrue(outcome["first_ack_received_and_discarded"])
        self.assertFalse(outcome["first_ack_available_to_host"])
        self.assertEqual(outcome["provider_requests_after_composer"], 1)
        self.assertEqual(outcome["agent_ends_after_composer"], 1)
        self.assertEqual(bridge.deliveries, 1)
        self.assertEqual(outcome["omp_children_remaining"], 0)

    def test_missing_or_visible_ack_cannot_prove_loss(self) -> None:
        for settings in ({"ack_discarded": False}, {"ack_status": "rejected"}):
            with self.subTest(settings=settings):
                outcome, _ = self._run_case(**settings)
                self.assertNotEqual(outcome["result"], "passed_pair_tui_reconnect")

    def test_reconnect_must_preserve_identity_on_a_new_socket(self) -> None:
        for settings in ({"new_socket": False}, {"same_pid": False},
                         {"same_session": False}, {"same_generation": False}):
            with self.subTest(settings=settings):
                outcome, _ = self._run_case(**settings)
                self.assertNotEqual(outcome["result"], "passed_pair_tui_reconnect")

    def test_provider_identity_and_processing_count_reject_duplicate(self) -> None:
        for settings in ({"wrong_provider_identity": True},
                         {"duplicate_provider": True}, {"duplicate_agent_end": True},
                         {"preexisting_agent_end": True}):
            with self.subTest(settings=settings):
                outcome, _ = self._run_case(**settings)
                self.assertNotEqual(outcome["result"], "passed_pair_tui_reconnect")

    def test_composer_and_public_state_must_recover(self) -> None:
        for settings in ({"composer_recovered": False}, {"public_state_ready": False},
                         {"draft_recovered": False}, {"draft_cleared": False}):
            with self.subTest(settings=settings):
                outcome, _ = self._run_case(**settings)
                self.assertNotEqual(outcome["result"], "passed_pair_tui_reconnect")

    def test_remaining_child_is_reported_after_cleanup(self) -> None:
        outcome, _ = self._run_case(cleanup_succeeds=False)
        self.assertGreater(outcome["omp_children_remaining"], 0)

    def test_public_editor_empty_without_visual_clear_does_not_pass(self) -> None:
        class Bridge:
            def request(self, *_args, **_kwargs):
                return {"state": {"editorKnown": True, "editorLength": 0,
                                  "editorEmpty": True}}

        with patch.object(probe, "_screen_state", return_value={
            "composer_visible": True, "draft_visible": True,
            "draft_in_composer": True,
        }):
            self.assertFalse(probe._wait_editor(
                Bridge(), object(), threading.Lock(), "", timeout=0.01,
                stale_text="old-draft",
            ))

    def test_visible_draft_without_matching_public_editor_does_not_pass(self) -> None:
        class Bridge:
            def request(self, *_args, **_kwargs):
                return {"state": {"editorKnown": True, "editorLength": 0,
                                  "editorEmpty": True}}

        with patch.object(probe, "_screen_state", return_value={
            "composer_visible": True, "draft_visible": True,
            "draft_in_composer": True,
        }):
            self.assertFalse(probe._wait_editor(
                Bridge(), object(), threading.Lock(), "new-draft", timeout=0.01,
            ))


if __name__ == "__main__":
    unittest.main()
