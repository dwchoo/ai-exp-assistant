"""Independent negative controls for the bounded live TUI delivery probe."""

from __future__ import annotations

import json
import os
import threading
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from uuid import uuid4

import live_tui_draft_probe as probe
from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream


def visible_screen(contents: str) -> TerminalScreen:
    screen = TerminalScreen(100, 30)
    make_stream(screen).feed(contents.encode())
    return screen


class TuiSurfaceTests(unittest.TestCase):
    def test_setup_screen_with_prompt_glyph_is_not_a_ready_composer(self) -> None:
        screen = visible_screen("Setup wizard\r\nπ > Choose a provider")
        state = probe._screen_state(screen, threading.Lock())

        self.assertTrue(state["setup_visible"])
        self.assertFalse(state["composer_visible"])

    def test_old_transcript_draft_does_not_prove_current_composer(self) -> None:
        draft = "draft-from-old-turn"
        screen = visible_screen(f"π >\r\n{draft}\r\nold answer\r\nπ >")
        state = probe._screen_state(screen, threading.Lock(), draft)

        self.assertTrue(state["draft_visible"])
        self.assertTrue(state["composer_visible"])
        self.assertFalse(state["draft_in_composer"])

    def test_visual_draft_remains_visible_after_api_reports_empty(self) -> None:
        draft = "draft-still-painted"
        screen = visible_screen(f"π >\r\n{draft}")
        api_state = {"editorKnown": True, "editorEmpty": True, "idle": True, "pending": False}
        surface = probe._screen_state(screen, threading.Lock(), draft)

        self.assertTrue(api_state["editorEmpty"])
        self.assertTrue(surface["draft_visible"])
        self.assertTrue(surface["draft_in_composer"])


class DeliveryOutcomeTests(unittest.TestCase):
    def _run_case(self, *, draft_stays_visible: bool) -> dict[str, object]:
        class Provider:
            server_port = 12345

            class RequestHandlerClass:
                requests = 0

            semantic_expected: dict[str, object] = {}

            @property
            def semantic_seen(self):
                return {message_id: {"matched": True} for message_id in self.semantic_expected}

            def serve_forever(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        provider = Provider()
        session_id = str(uuid4())

        class Bridge:
            def __init__(self, *_args):
                self.probe_count = 0
                # This event belongs to an older turn; it has no current message ID.
                self.events = [{"role": "worker", "name": "assistant_message_end",
                                "responseMarkerMatched": True}]

            def serve_forever(self):
                pass

            def peer(self, role):
                return {"pid": 900001 if role == "manager" else 900002,
                        "session_id": session_id, "generation": 1}

            def request(self, _target, request, **_kwargs):
                if request["kind"] == "probe":
                    self.probe_count += 1
                    editor_length = 38 if self.probe_count in (2, 3) else 0
                    return {"state": {"editorKnown": True, "editorEmpty": editor_length == 0,
                                      "editorLength": editor_length, "idle": True, "pending": False}}
                if self.probe_count <= 3:
                    return {"status": "deferred"}
                provider.RequestHandlerClass.requests = 1
                return {"status": "api_accepted", "modelProcessed": False}

            def wait_event(self, *_args, **_kwargs):
                return self.events[0]

            def shutdown(self):
                pass

            def server_close(self):
                pass

        # run() generates a "draft-" prefix and a 32-character UUID hex suffix.
        bridge = Bridge()
        # initial screen pair, typed draft, held delivery, then clear or stale paint.
        states = [
            {"composer_visible": True}, {"composer_visible": True},
            {"draft_in_composer": True, "draft_visible": True},
            {"draft_in_composer": True, "draft_visible": True},
        ]
        cleared_screen = {"draft_in_composer": False, "draft_visible": False}
        stale_screen = {"draft_in_composer": True, "draft_visible": True}
        screen_count = 0

        def screen_state(*_args):
            nonlocal screen_count
            screen_count += 1
            if screen_count <= len(states):
                return states[screen_count - 1]
            return stale_screen if draft_stays_visible else cleared_screen
        def start(_omp, role, *_args, **_kwargs):
            fd = os.open(os.devnull, os.O_WRONLY)
            return {"pid": 900001 if role == "manager" else 900002, "fd": fd}

        with (
            patch.object(probe, "semantic_provider", return_value=provider),
            patch.object(probe, "BridgeHarness", return_value=bridge),
            patch.object(probe, "_start_omp", side_effect=start),
            patch.object(probe, "_stop_omps"),
            patch.object(probe, "_drain_visible"),
            patch.object(probe, "_screen_state", side_effect=screen_state),
            patch.object(probe.os, "chmod"),
        ):
            outcome = probe.run("unused-omp")

        return outcome

    def test_api_empty_with_visual_draft_cannot_start_delivery(self) -> None:
        outcome = self._run_case(draft_stays_visible=True)

        self.assertEqual(outcome["result"], "draft_clear_unknown")
        self.assertTrue(outcome["draft_screen_after_clear"]["draft_visible"])
        self.assertEqual(outcome["provider_requests_while_held"], 0)
        self.assertNotIn("api_return_status", outcome)

    def test_unrelated_assistant_marker_cannot_confirm_current_message(self) -> None:
        outcome = self._run_case(draft_stays_visible=False)

        self.assertTrue(outcome["provider_identity_matched"])
        self.assertEqual(outcome["api_return_status"], "api_accepted")
        self.assertNotEqual(outcome["result"], "passed_pair_tui_draft_delivery")


class AsyncErrorOutcomeTests(unittest.TestCase):
    def test_screen_requires_this_runs_error_marker(self) -> None:
        screen = visible_screen("Error from previous turn\r\nπ >")

        state = probe._screen_state(screen, threading.Lock(), error_marker="G3_ASYNC_ERROR_current")

        self.assertTrue(state["error_visible"])
        self.assertFalse(state["error_marker_visible"])

    def test_failing_provider_checks_each_current_identifier(self) -> None:
        provider = probe._failing_provider()
        server_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        server_thread.start()
        provider.release_error.set()
        provider.error_marker = "G3_ASYNC_ERROR_current"
        try:
            for wrong_field in (None, "workbench_message_id", "kind", "task_id", "revision_id", "run_id"):
                with self.subTest(wrong_field=wrong_field):
                    message_id = str(uuid4())
                    expected = {
                        "workbench_message_id": message_id, "kind": "task",
                        "task_id": str(uuid4()), "revision_id": str(uuid4()), "run_id": str(uuid4()),
                    }
                    provider.semantic_expected[message_id] = expected
                    sent = expected.copy()
                    if wrong_field:
                        sent[wrong_field] = "another-request"
                        # Keep the lookup key current when testing its value.
                        if wrong_field == "workbench_message_id":
                            provider.semantic_expected["another-request"] = expected
                    body = json.dumps({"messages": [{"role": "user", "content": json.dumps(sent)}]}).encode()
                    request = Request(
                        f"http://127.0.0.1:{provider.server_port}/v1/chat/completions",
                        data=body, headers={"Content-Type": "application/json"},
                    )
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(request, timeout=2)
                    self.assertEqual(caught.exception.code, 422)
                    caught.exception.close()
                    lookup = sent["workbench_message_id"]
                    self.assertEqual(provider.semantic_seen[lookup]["matched"], wrong_field is None)
        finally:
            provider.shutdown()
            provider.server_close()
            server_thread.join(timeout=2)

    def _run_case(
        self, *, identity_field: str = "", fresh_error_marker: bool = True,
        fresh_agent_end: bool = True, fresh_success_marker: bool = False,
        error_after_ack: bool = True, error_released_by_ack: bool = True,
        error_sent: bool = True,
    ) -> tuple[dict[str, object], object]:
        class Provider:
            server_port = 12345

            class RequestHandlerClass:
                requests = 0

            semantic_expected: dict[str, dict[str, object]] = {}
            release_error = threading.Event()
            error_marker = ""

            @property
            def semantic_seen(self):
                seen = {}
                for message_id, expected in self.semantic_expected.items():
                    observed = expected.copy()
                    if identity_field:
                        observed[identity_field] = "another-request"
                    seen[message_id] = {"matched": all(
                        observed.get(key) == value for key, value in expected.items()
                    )}
                return seen

            def serve_forever(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        provider = Provider()
        provider.error_released_by_ack = error_released_by_ack
        provider.error_sent_at = 1_000_000 if error_after_ack and error_sent else (1 if error_sent else None)
        session_id = str(uuid4())

        class Bridge:
            def __init__(self, *_args):
                self.probe_count = 0
                self.deliver_count = 0
                # These are from an older turn and must never confirm this request.
                self.events = [
                    {"role": "worker", "name": "agent_end"},
                    {"role": "worker", "name": "assistant_message_end", "responseMarkerMatched": True},
                ]

            def serve_forever(self):
                pass

            def peer(self, role):
                return {"pid": 900001 if role == "manager" else 900002,
                        "session_id": session_id, "generation": 1}

            def request(self, _target, request, **_kwargs):
                if request["kind"] == "probe":
                    self.probe_count += 1
                    editor_length = 38 if self.probe_count in (2, 3) else 0
                    return {"state": {"editorKnown": True, "editorEmpty": editor_length == 0,
                                      "editorLength": editor_length, "idle": True, "pending": False}}
                self.deliver_count += 1
                if self.deliver_count == 1:
                    return {"status": "deferred"}
                provider.RequestHandlerClass.requests += 1
                if fresh_agent_end:
                    self.events.append({"role": "worker", "name": "agent_end"})
                if fresh_success_marker:
                    self.events.append({"role": "worker", "name": "assistant_message_end",
                                        "responseMarkerMatched": True})
                return {"status": "api_accepted", "modelProcessed": False}

            def shutdown(self):
                pass

            def server_close(self):
                pass

        bridge = Bridge()
        screen_count = 0

        def screen_state(*_args, **kwargs):
            nonlocal screen_count
            screen_count += 1
            if screen_count <= 2:
                return {"composer_visible": True}
            if screen_count <= 4:
                return {"draft_in_composer": True, "draft_visible": True}
            if "error_marker" in kwargs:
                return {"error_visible": True, "error_marker_visible": fresh_error_marker}
            return {"draft_in_composer": False, "draft_visible": False}

        def start(_omp, role, *_args, **_kwargs):
            fd = os.open(os.devnull, os.O_WRONLY)
            return {"pid": 900001 if role == "manager" else 900002, "fd": fd}

        # Advance bounded polling without a real OMP process or a 12-second timeout.
        clock = SimpleNamespace(now=100)

        def tick():
            clock.now += 1
            return clock.now

        with (
            patch.object(probe, "_failing_provider", return_value=provider),
            patch.object(probe, "BridgeHarness", return_value=bridge),
            patch.object(probe, "_start_omp", side_effect=start),
            patch.object(probe, "_stop_omps"),
            patch.object(probe, "_drain_visible"),
            patch.object(probe, "_screen_state", side_effect=screen_state),
            patch.object(probe.os, "chmod"),
            patch.object(probe.time, "monotonic", side_effect=tick),
            patch.object(probe.time, "sleep"),
        ):
            outcome = probe.run("unused-omp", async_error=True)

        return outcome, bridge

    def test_current_error_after_ack_with_fresh_turn_end_is_observed_once(self) -> None:
        outcome, bridge = self._run_case()

        self.assertEqual(outcome["result"], "observed_async_error")
        self.assertTrue(outcome["fresh_agent_end_observed"])
        self.assertFalse(outcome["fresh_assistant_marker_matched"])
        self.assertEqual(bridge.deliver_count, 2)  # held once, sent once after clear
        self.assertEqual(outcome["provider_requests"], 1)

    def test_prior_turn_end_cannot_confirm_current_error(self) -> None:
        outcome, _ = self._run_case(fresh_agent_end=False)

        self.assertFalse(outcome["fresh_agent_end_observed"])
        self.assertEqual(outcome["result"], "async_error_observation_unknown")

    def test_old_generic_error_screen_cannot_confirm_current_error(self) -> None:
        outcome, _ = self._run_case(fresh_error_marker=False)

        self.assertFalse(outcome["error_marker_visible"])
        self.assertEqual(outcome["result"], "async_error_observation_unknown")

    def test_http_error_alone_cannot_confirm_tui_error(self) -> None:
        outcome, _ = self._run_case(fresh_error_marker=False, fresh_agent_end=False)

        self.assertTrue(outcome["provider_error_sent"])
        self.assertEqual(outcome["result"], "async_error_observation_unknown")

    def test_current_success_marker_conflicts_with_error_outcome(self) -> None:
        outcome, _ = self._run_case(fresh_success_marker=True)

        self.assertTrue(outcome["fresh_assistant_marker_matched"])
        self.assertEqual(outcome["result"], "async_error_observation_unknown")

    def test_error_must_follow_api_ack(self) -> None:
        outcome, _ = self._run_case(error_after_ack=False)

        self.assertFalse(outcome["provider_error_after_api_ack"])
        self.assertEqual(outcome["result"], "async_error_observation_unknown")

    def test_provider_must_have_been_released_by_api_ack(self) -> None:
        outcome, _ = self._run_case(error_released_by_ack=False)

        self.assertFalse(outcome["provider_error_released_by_api_ack"])
        self.assertEqual(outcome["result"], "async_error_observation_unknown")

    def test_error_response_must_have_been_sent(self) -> None:
        outcome, _ = self._run_case(error_sent=False)

        self.assertFalse(outcome["provider_error_sent"])
        self.assertEqual(outcome["result"], "async_error_observation_unknown")

    def test_each_current_provider_identifier_must_match(self) -> None:
        for field in ("workbench_message_id", "kind", "task_id", "revision_id", "run_id"):
            with self.subTest(field=field):
                outcome, _ = self._run_case(identity_field=field)
                self.assertFalse(outcome["provider_identity_matched"])
                self.assertEqual(outcome["result"], "async_error_observation_unknown")


if __name__ == "__main__":
    unittest.main()
