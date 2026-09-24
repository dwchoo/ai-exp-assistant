"""Negative controls for identity observations from a live TUI delivery.

These tests replace nondeterministic OMP execution with structured observations.
They do not claim that a real OMP session emits the tested events.
"""

from __future__ import annotations

import io
import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import live_omp_probe as omp_probe
import live_tui_draft_probe as draft_probe
import live_tui_event_surface_probe as event_probe


FIELDS = (
    "workbench_message_id", "workbench_delivery_attempt_id", "kind",
    "task_id", "revision_id", "run_id",
)


def user_event(**changes: object) -> dict[str, object]:
    event: dict[str, object] = {
        "name": "delivery_user_message_end_probe",
        "role": "worker",
        "sessionId": "current-session",
        "generation": 3,
        "observedAt": 11.0,
        "identityFieldsPresent": True,
        "matches": dict.fromkeys(FIELDS, True),
        "roleMatched": True,
        "sessionMatched": True,
        "generationMatched": True,
    }
    event.update(changes)
    return event


def assistant_event(**changes: object) -> dict[str, object]:
    event: dict[str, object] = {
        "name": "delivery_assistant_message_end_probe",
        "role": "worker",
        "sessionId": "current-session",
        "generation": 3,
        "observedAt": 12.0,
        "stopReason": "error",
        "errorMessagePresent": True,
        "willContinue": False,
    }
    event.update(changes)
    return event


def provider_request_event(**changes: object) -> dict[str, object]:
    event: dict[str, object] = {
        "name": "provider_request_identity_probe",
        "role": "worker", "sessionId": "current-session", "generation": 3,
        "observedAt": 11.5,
        "eventObject": True, "payloadShape": "object",
        "payloadKeys": ["messages", "model"], "messagesArray": True,
        "lastMessageRole": "user", "lastContentShape": "blocks",
        "textBlockCount": 1, "structuredBlockCount": 1,
        "identityFieldsPresent": True, "matches": dict.fromkeys(FIELDS, True),
        "roleMatched": True, "sessionMatched": True, "generationMatched": True,
    }
    event.update(changes)
    return event


class EventStream:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.condition = threading.Condition()
        self.events = events


class EventIdentityTests(unittest.TestCase):
    def surface(self, events: list[dict[str, object]], *, start: int = 0) -> dict[str, object]:
        return draft_probe._event_surface_diagnostic(EventStream(events), start, 10.0)

    def test_six_current_identity_fields_and_context_are_required(self) -> None:
        self.assertTrue(self.surface([user_event()])["userIdentityExact"])
        for field in FIELDS:
            with self.subTest(field=field):
                matches = dict.fromkeys(FIELDS, True)
                matches[field] = False
                self.assertFalse(self.surface([user_event(matches=matches)])["userIdentityExact"])
                absent = dict.fromkeys((key for key in FIELDS if key != field), True)
                self.assertFalse(self.surface([user_event(matches=absent)])["userIdentityExact"])

        for attribute in ("identityFieldsPresent", "roleMatched", "sessionMatched", "generationMatched"):
            with self.subTest(attribute=attribute):
                self.assertFalse(self.surface([user_event(**{attribute: False})])["userIdentityExact"])

    def test_stale_event_wrong_attempt_and_no_user_event_cannot_establish_identity(self) -> None:
        self.assertFalse(self.surface([user_event()], start=1)["userIdentityExact"])
        self.assertFalse(self.surface([assistant_event()])["userIdentityExact"])
        self.assertFalse(self.surface([])["userIdentityExact"])
        self.assertFalse(self.surface([user_event(), user_event()])["userIdentityExact"])

    def test_unrelated_assistant_error_is_a_session_event_with_unknown_id_outcome(self) -> None:
        surface = self.surface([assistant_event()])
        self.assertFalse(surface["userIdentityExact"])
        self.assertEqual(surface["identitySpecificOutcome"], "unknown")
        self.assertEqual(surface["events"][0]["stopReason"], "error")

        surface = self.surface([user_event(), assistant_event(stopReason="stop", errorMessagePresent=False)])
        self.assertTrue(surface["userIdentityExact"])
        self.assertEqual(surface["identitySpecificOutcome"], "unknown")

    def test_event_output_contains_no_raw_user_or_assistant_content(self) -> None:
        surface = self.surface([
            user_event(content="PRIVATE_USER_BODY", payload="PRIVATE_PAYLOAD"),
            assistant_event(content="PRIVATE_ASSISTANT_BODY", errorMessage="PRIVATE_ERROR_BODY"),
        ])
        rendered = json.dumps(surface)
        for marker in ("PRIVATE_USER_BODY", "PRIVATE_PAYLOAD", "PRIVATE_ASSISTANT_BODY", "PRIVATE_ERROR_BODY"):
            self.assertNotIn(marker, rendered)


def diagnostic_outcome(*, async_error: bool = False, provider_request: bool = False) -> dict[str, object]:
    event = {
        "name": "delivery_user_message_end_probe", "role": "worker",
        "sessionId": "current-session", "generation": 3, "afterApiAck": True,
        "identityFieldsPresent": True, "matches": dict.fromkeys(FIELDS, True),
        "roleMatched": True, "sessionMatched": True, "generationMatched": True,
    }
    events = [event]
    if provider_request:
        request = provider_request_event()
        request["afterApiAck"] = True
        events.append(request)
    if async_error:
        events.append({
            "name": "delivery_assistant_message_end_probe", "role": "worker",
            "sessionId": "current-session", "generation": 3, "afterApiAck": True,
            "stopReason": "error", "errorMessagePresent": True, "willContinue": False,
        })
    return {
        "result": "observed_async_error" if async_error else "passed_pair_tui_draft_delivery",
        "api_return_status": "api_accepted",
        "provider_identity_matched": True,
        "provider_error_after_api_ack": True if async_error else None,
        "omp_children_remaining": 0,
        "event_surface": {
            "events": events, "userIdentityExact": True,
            "identitySpecificOutcome": "unknown",
        },
    }


class DiagnosticDecisionTests(unittest.TestCase):
    def invoke(self, normal: dict[str, object], failure: dict[str, object],
               *, provider_request: bool = False) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with (
            patch.object(event_probe.shutil, "which", return_value="/tmp/omp"),
            patch.object(event_probe, "_omp_version", return_value=event_probe.OMP_VERSION),
            patch.object(event_probe, "run", side_effect=[normal, failure]) as runtime,
            redirect_stdout(output),
        ):
            status = event_probe.main(provider_request=provider_request)
        if provider_request:
            self.assertIn(runtime.call_count, (1, 2))
        else:
            self.assertEqual(runtime.call_count, 2)
        self.assertNotIn("PRIVATE_BODY", output.getvalue())
        return status, json.loads(output.getvalue())

    def test_current_user_identity_can_be_seen_without_claiming_id_specific_outcome(self) -> None:
        status, report = self.invoke(diagnostic_outcome(), diagnostic_outcome(async_error=True))
        self.assertEqual(status, 0)
        self.assertEqual(report["result"], "diagnostic_pair_observed")
        self.assertTrue(all(item["eventSurface"]["identitySpecificOutcome"] == "unknown"
                            for item in report["evidence"]))

    def test_provider_identity_and_child_cleanup_are_required_for_pair(self) -> None:
        for change in ({"provider_identity_matched": False}, {"omp_children_remaining": 1}):
            with self.subTest(change=change):
                normal = diagnostic_outcome()
                normal.update(change)
                status, report = self.invoke(normal, diagnostic_outcome(async_error=True))
                self.assertNotEqual(status, 0)
                self.assertNotEqual(report["result"], "diagnostic_pair_observed")

    def test_post_ack_error_must_be_observed_after_ack_for_current_turn(self) -> None:
        for change in (
            {"provider_error_after_api_ack": False},
            {"result": "async_error_observation_unknown"},
        ):
            with self.subTest(change=change):
                failure = diagnostic_outcome(async_error=True)
                failure.update(change)
                status, report = self.invoke(diagnostic_outcome(), failure)
                self.assertNotEqual(status, 0)
                self.assertNotEqual(report["result"], "diagnostic_pair_observed")

        failure = diagnostic_outcome(async_error=True)
        failure["event_surface"]["events"][1]["afterApiAck"] = False
        status, report = self.invoke(diagnostic_outcome(), failure)
        self.assertNotEqual(status, 0)
        self.assertNotEqual(report["result"], "diagnostic_pair_observed")

    def test_api_return_without_provider_processing_cannot_pass(self) -> None:
        normal = diagnostic_outcome()
        normal["result"] = "inconclusive"
        status, report = self.invoke(normal, diagnostic_outcome(async_error=True))
        self.assertNotEqual(status, 0)
        self.assertNotEqual(report["result"], "diagnostic_pair_observed")

    def test_error_from_other_role_session_or_generation_cannot_pass(self) -> None:
        for change in ({"role": "manager"}, {"sessionId": "old-session"}, {"generation": 2}):
            with self.subTest(change=change):
                failure = diagnostic_outcome(async_error=True)
                failure["event_surface"]["events"][1].update(change)
                status, report = self.invoke(diagnostic_outcome(), failure)
                self.assertNotEqual(status, 0)
                self.assertNotEqual(report["result"], "diagnostic_pair_observed")

    def test_error_before_current_user_event_cannot_confirm_current_turn(self) -> None:
        failure = diagnostic_outcome(async_error=True)
        events = failure["event_surface"]["events"]
        events[:] = [events[1], events[0]]
        status, report = self.invoke(diagnostic_outcome(), failure)
        self.assertNotEqual(status, 0)
        self.assertNotEqual(report["result"], "diagnostic_pair_observed")

    def test_stale_error_from_previous_turn_cannot_confirm_current_user(self) -> None:
        failure = diagnostic_outcome(async_error=True)
        events = failure["event_surface"]["events"]
        previous_error = dict(events[1], sessionId="old-session")
        events[:] = [previous_error, events[0]]
        status, report = self.invoke(diagnostic_outcome(), failure)
        self.assertNotEqual(status, 0)
        self.assertNotEqual(report["result"], "diagnostic_pair_observed")

    def test_reported_identity_cannot_override_wrong_observed_user_context(self) -> None:
        for change in ({"role": "manager"}, {"sessionId": "old-session"}, {"generation": 2},
                       {"roleMatched": False}, {"sessionMatched": False}, {"generationMatched": False}):
            with self.subTest(change=change):
                failure = diagnostic_outcome(async_error=True)
                failure["event_surface"]["events"][0].update(change)
                status, report = self.invoke(diagnostic_outcome(), failure)
                self.assertNotEqual(status, 0)
                self.assertNotEqual(report["result"], "diagnostic_pair_observed")


class ChildEnvironmentTests(unittest.TestCase):
    def test_non_diagnostic_omp_child_does_not_inherit_event_surface_opt_in(self) -> None:
        class ChildExec(Exception):
            pass

        with (
            patch.dict(os.environ, {"WORKBENCH_G3_EVENT_SURFACE_PROBE": "1"}),
            patch.object(Path, "mkdir"),
            patch.object(omp_probe.pty, "fork", return_value=(0, 0)),
            patch.object(omp_probe.os, "chdir"),
            patch.object(omp_probe.fcntl, "ioctl"),
            patch.object(omp_probe.os, "execvpe", side_effect=ChildExec) as exec_child,
        ):
            with self.assertRaises(ChildExec):
                omp_probe._start_omp(
                    "omp", "worker", "token", Path("/tmp/g3-test-child-env"),
                    Path("/tmp/g3-test.sock"), Path("/tmp/g3-test-config.json"),
                    event_surface_probe=False,
                )
        child_env = exec_child.call_args.args[2]
        self.assertFalse("WORKBENCH_G3_EVENT_SURFACE_PROBE" in child_env)


class ProviderRequestDecisionTests(unittest.TestCase):
    def pair(self, normal: dict[str, object] | None = None,
             failure: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
        return DiagnosticDecisionTests.invoke(
            self,
            normal if normal is not None else diagnostic_outcome(provider_request=True),
            failure if failure is not None else diagnostic_outcome(async_error=True, provider_request=True),
            provider_request=True,
        )

    def test_current_provider_request_is_observed_without_final_outcome_claim(self) -> None:
        status, report = self.pair()
        self.assertEqual(status, 0)
        self.assertEqual(report["result"], "diagnostic_pair_observed")
        for item in report["evidence"]:
            self.assertTrue(item["providerRequestIdentityExact"])
            self.assertEqual(item["eventSurface"]["identitySpecificOutcome"], "unknown")

    def test_provider_request_before_user_message_end_is_valid(self) -> None:
        normal = diagnostic_outcome(provider_request=True)
        failure = diagnostic_outcome(async_error=True, provider_request=True)
        for outcome in (normal, failure):
            events = outcome["event_surface"]["events"]
            events[:2] = [events[1], events[0]]
        status, report = self.pair(normal=normal, failure=failure)
        self.assertEqual(status, 0)
        self.assertEqual(report["result"], "diagnostic_pair_observed")
        self.assertTrue(all(item["eventSurface"]["identitySpecificOutcome"] == "unknown"
                            for item in report["evidence"]))

    def test_generic_provider_event_or_prior_history_id_cannot_prove_current_input(self) -> None:
        for defect in ("generic_only", "history_only", "wrong_attempt", "wrong_message_id"):
            with self.subTest(defect=defect):
                normal = diagnostic_outcome(provider_request=True)
                events = normal["event_surface"]["events"]
                if defect == "generic_only":
                    events[1] = {"name": "provider_request_started", "role": "worker",
                                 "sessionId": "current-session", "generation": 3,
                                 "afterApiAck": True}
                else:
                    request = events[1]
                    if defect == "history_only":
                        # The HTTP provider's broad history scan can report a prior ID.
                        # The final user content has no structured current identity.
                        request["structuredBlockCount"] = 0
                        request["identityFieldsPresent"] = False
                        request["matches"] = dict.fromkeys(FIELDS, False)
                    else:
                        matches = dict.fromkeys(FIELDS, True)
                        matches["workbench_delivery_attempt_id" if defect == "wrong_attempt"
                                else "workbench_message_id"] = False
                        request["matches"] = matches
                status, report = self.pair(normal=normal)
                self.assertNotEqual(status, 0)
                self.assertEqual(report["result"], "provider_request_identity_unknown")

    def test_non_user_final_message_and_bad_payload_cannot_pass(self) -> None:
        for change in (
            {"lastMessageRole": "assistant"}, {"lastMessageRole": "tool"},
            {"eventObject": False}, {"payloadShape": "array"},
            {"payloadShape": "other"}, {"messagesArray": False},
            {"identityFieldsPresent": False},
        ):
            with self.subTest(change=change):
                normal = diagnostic_outcome(provider_request=True)
                normal["event_surface"]["events"][1].update(change)
                status, report = self.pair(normal=normal)
                self.assertNotEqual(status, 0)
                self.assertEqual(report["result"], "provider_request_identity_unknown")

    def test_multiple_structured_blocks_or_wrong_field_names_cannot_pass(self) -> None:
        for change in (
            {"structuredBlockCount": 2, "textBlockCount": 2},
            {"structuredBlockCount": 0},
            {"matches": dict.fromkeys(("x1", "x2", "x3", "x4", "x5", "x6"), True)},
        ):
            with self.subTest(change=change):
                normal = diagnostic_outcome(provider_request=True)
                normal["event_surface"]["events"][1].update(change)
                status, report = self.pair(normal=normal)
                self.assertNotEqual(status, 0)
                self.assertEqual(report["result"], "provider_request_identity_unknown")

    def test_wrong_context_missing_or_duplicate_request_cannot_pass(self) -> None:
        for change in (
            {"role": "manager"}, {"sessionId": "old-session"}, {"generation": 2},
            {"roleMatched": False}, {"sessionMatched": False}, {"generationMatched": False},
            {"afterApiAck": False},
        ):
            with self.subTest(change=change):
                normal = diagnostic_outcome(provider_request=True)
                normal["event_surface"]["events"][1].update(change)
                status, report = self.pair(normal=normal)
                self.assertNotEqual(status, 0)
                self.assertEqual(report["result"], "provider_request_identity_unknown")

        for defect in ("missing", "duplicate"):
            with self.subTest(defect=defect):
                normal = diagnostic_outcome(provider_request=True)
                events = normal["event_surface"]["events"]
                if defect == "missing":
                    events.pop(1)
                elif defect == "duplicate":
                    events.append(dict(events[1]))
                status, report = self.pair(normal=normal)
                self.assertNotEqual(status, 0)
                self.assertEqual(report["result"], "provider_request_identity_unknown")

    def test_provider_event_summary_never_exposes_raw_payload_or_content(self) -> None:
        stream = EventStream([provider_request_event(
            payload={"messages": "PRIVATE_BODY"},
            content="PRIVATE_BODY", requestBody="PRIVATE_BODY",
        )])
        surface = draft_probe._event_surface_diagnostic(stream, 0, 10.0)
        self.assertNotIn("PRIVATE_BODY", json.dumps(surface))


if __name__ == "__main__":
    unittest.main()
