"""Independent negative controls for the real TUI contention probe."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from uuid import uuid4

import live_tui_contention_probe as probe


class ContentionOutcomeTests(unittest.TestCase):
    def _run_case(self, mode: str, *, mutation: str = "") -> tuple[dict[str, object], object]:
        """Model only the external bridge, provider, and native TUI observations."""
        target = "manager" if mode == "busy" else "worker"
        sessions = {role: str(uuid4()) for role in probe.ROLES}
        world = SimpleNamespace()
        world.target = target
        world.mode = mode
        world.mutation = mutation
        world.native_root = None
        world.release = False
        world.hold_queries = 0
        world.deliveries = []
        world.wait_calls = 0
        world.screen_calls = 0

        class Provider:
            server_port = 12345

            def __init__(self, role: str):
                self.role = role
                self.RequestHandlerClass = type("Handler", (), {"requests": 0})
                self.semantic_expected: dict[str, dict[str, object]] = {}
                self.semantic_seen: dict[str, bool] = {}

            def serve_forever(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        providers = {role: Provider(role) for role in probe.ROLES}

        class Bridge:
            def __init__(self, socket: Path, _tokens):
                world.native_root = socket.parent
                self.condition = threading.Condition()
                self.events: list[dict[str, object]] = []
                self.phase = "initial"

            def serve_forever(self):
                pass

            def peer(self, role: str, **_kwargs):
                return {"pid": 900001 if role == "manager" else 900002,
                        "session_id": sessions[role], "generation": 1}

            def request(self, role: str, request: dict[str, object], **_kwargs):
                if request["kind"] == "probe":
                    held = role == target and self.phase == "held" and not world.release
                    if held:
                        world.hold_queries += 1
                    state = {
                        "sessionId": sessions[role], "generation": 1,
                        "idle": not held, "pending": False,
                        "approvalPending": held and mode == "approval",
                        "inFlightToolCount": int(held and mode == "busy"),
                        "editorKnown": True, "editorEmpty": True,
                        "editorLength": 0,
                    }
                    if mutation == "wrong_held_generation" and held and world.deliveries:
                        state["generation"] = 2
                    if mutation == "editor_intrusion" and held and world.deliveries:
                        state["editorEmpty"] = False
                        state["editorLength"] = 5
                    return {"state": state}

                envelope = json.loads(request["envelope"])
                world.deliveries.append((role, envelope))
                if len(world.deliveries) == 1:
                    if mutation == "approval_file_transient_early":
                        (world.native_root / f"cwd-{role}" / "approval.txt").unlink()
                    if mutation == "premature_provider":
                        providers[target].RequestHandlerClass.requests += 1
                    return {"status": "deferred", "modelProcessed": None}
                if len(world.deliveries) == 2:
                    providers[target].RequestHandlerClass.requests += 1
                    self.events.append({"role": target, "name": "agent_start"})
                    self.events.append({
                        "role": target, "name": "agent_end",
                        "sessionId": sessions[target], "generation": 1,
                        "messageId": envelope["messageId"],
                    })
                    if mutation == "stale_retry_end":
                        self.events[-1]["sessionId"] = str(uuid4())
                    providers[target].semantic_seen[envelope["messageId"]] = (
                        mutation != "wrong_provider_identity"
                    )
                    if mutation == "opposite_role_provider":
                        other = "worker" if role == "manager" else "manager"
                        providers[other].RequestHandlerClass.requests += 1
                    return {"status": "api_accepted", "modelProcessed": mutation == "processed_at_ack"}
                if mutation == "duplicate_replay":
                    providers[target].RequestHandlerClass.requests += 1
                return {"status": "duplicate_api_accepted"}

            def wait_event(self, role: str, name: str, **_kwargs):
                self.phase = "held"
                providers[role].RequestHandlerClass.requests = 1
                self.events.append({"role": role, "name": "agent_start"})
                event = {"role": role, "name": name}
                self.events.append(event)
                if mode == "busy":
                    (world.native_root / f"cwd-{role}" / "started.txt").write_text("STARTED")
                elif mutation in ("approval_file_early", "approval_file_transient_early"):
                    (world.native_root / f"cwd-{role}" / "approval.txt").write_text("APPROVED")
                return event

            def event_count(self, role: str, name: str) -> int:
                if role == target and name == "tool_approval_resolved" and mode == "approval":
                    if mutation != "approval_release_missing" and not world.release:
                        world.release = True
                        (world.native_root / f"cwd-{role}" / "approval.txt").write_text("APPROVED")
                        self.events.append({"role": role, "name": name,
                                            "approved": mutation != "approval_denied"})
                if role == target and name == "agent_end" and world.release:
                    if not any(event.get("name") == "agent_end" for event in self.events):
                        providers[target].RequestHandlerClass.requests += 1
                        self.events.append({
                            "role": role, "name": name,
                            "sessionId": sessions[target], "generation": 1,
                            "messageId": "native-user-turn",
                        })
                return sum(event.get("role") == role and event.get("name") == name
                           for event in self.events)

            def shutdown(self):
                pass

            def server_close(self):
                pass

        def screen_state(_screen, _lock):
            # Both initial TUI surfaces are ready. Later calls inspect the
            # target while the native tool or approval holds the turn.
            world.screen_calls += 1
            visible = not (mutation == "native_ui_hides_composer"
                           and world.screen_calls in (3, 4))
            return {"composer_visible": visible}

        def start(_omp, role, _token, _root, _profile, _mode):
            (_root / f"cwd-{role}").mkdir()
            return {"role": role, "pid": 900001 if role == "manager" else 900002,
                    "fd": os.open(os.devnull, os.O_WRONLY)}

        def wait_until(predicate, _timeout, _interval=0.05):
            world.wait_calls += 1
            if mode == "busy" and world.wait_calls == 3:
                world.release = True
                (world.native_root / f"cwd-{target}" / "result.txt").write_text("FINISHED")
            return bool(predicate())

        with (
            patch.object(probe, "_provider", side_effect=lambda _mode, cwd: providers[cwd.name.removeprefix("cwd-")]),
            patch.object(probe, "BridgeHarness", Bridge),
            patch.object(probe, "_start_tui", side_effect=start),
            patch.object(probe, "_stop_omps"),
            patch.object(probe, "_drain_visible"),
            patch.object(probe, "_screen_state", side_effect=screen_state),
            patch.object(probe, "_wait_until", side_effect=wait_until),
            patch.object(probe, "cwd_processes", return_value=[]),
            patch.object(probe.os, "chmod"),
            patch.object(probe.os, "write"),
            patch.object(probe.time, "sleep"),
        ):
            outcome = probe.run("unused-omp", mode)
        return outcome, world

    def test_native_approval_and_busy_hold_then_safe_retry(self) -> None:
        for mode in ("approval", "busy"):
            with self.subTest(mode=mode):
                outcome, world = self._run_case(mode)
                self.assertEqual(outcome["result"], f"passed_{mode}")
                self.assertEqual(len(outcome["hold_samples"]), 6)
                self.assertEqual(len(world.deliveries), 3)
                self.assertEqual(outcome["retry_ack"], "api_accepted")
                self.assertEqual(outcome["duplicate_ack"], "duplicate_api_accepted")

    def test_provider_activity_during_hold_blocks_success(self) -> None:
        outcome, _ = self._run_case("busy", mutation="premature_provider")
        self.assertNotEqual(outcome["result"], "passed_busy")

    def test_held_generation_change_blocks_success(self) -> None:
        outcome, _ = self._run_case("busy", mutation="wrong_held_generation")
        self.assertNotEqual(outcome["result"], "passed_busy")

    def test_native_approval_ui_can_hide_composer_while_editor_stays_untouched(self) -> None:
        outcome, _ = self._run_case("approval", mutation="native_ui_hides_composer")
        self.assertFalse(outcome["screen_at_delivery"]["composer_visible"])
        self.assertFalse(outcome["screen_after_hold"]["composer_visible"])
        self.assertTrue(all(sample["editor_length_unchanged"] for sample in outcome["hold_samples"]))
        self.assertEqual(outcome["result"], "passed_approval")

    def test_public_editor_change_during_hold_blocks_success(self) -> None:
        outcome, _ = self._run_case("approval", mutation="editor_intrusion")
        self.assertFalse(outcome["hold_samples"][0]["editor_length_unchanged"])
        self.assertNotEqual(outcome["result"], "passed_approval")

    def test_missing_native_approval_release_blocks_success(self) -> None:
        outcome, _ = self._run_case("approval", mutation="approval_release_missing")
        self.assertNotEqual(outcome["result"], "passed_approval")

    def test_denied_native_approval_blocks_success(self) -> None:
        outcome, _ = self._run_case("approval", mutation="approval_denied")
        self.assertNotEqual(outcome["result"], "passed_approval")

    def test_wrong_provider_identity_blocks_success(self) -> None:
        outcome, _ = self._run_case("busy", mutation="wrong_provider_identity")
        self.assertNotEqual(outcome["result"], "passed_busy")

    def test_stale_retry_end_and_other_role_activity_block_success(self) -> None:
        for mutation in ("stale_retry_end", "opposite_role_provider"):
            with self.subTest(mutation=mutation):
                outcome, _ = self._run_case("busy", mutation=mutation)
                self.assertNotEqual(outcome["result"], "passed_busy")

    def test_api_return_is_not_processing_and_duplicate_must_not_replay(self) -> None:
        for mutation in ("processed_at_ack", "duplicate_replay"):
            with self.subTest(mutation=mutation):
                outcome, _ = self._run_case("busy", mutation=mutation)
                self.assertNotEqual(outcome["result"], "passed_busy")

    def test_approval_file_created_before_native_release_cannot_pass(self) -> None:
        outcome, _ = self._run_case("approval", mutation="approval_file_early")
        self.assertFalse(outcome["approval_file_absent_before"])
        self.assertNotEqual(outcome["result"], "passed_approval")

    def test_transient_file_before_native_approval_cannot_pass(self) -> None:
        outcome, _ = self._run_case("approval", mutation="approval_file_transient_early")
        self.assertFalse(outcome["approval_file_absent_before"])
        self.assertTrue(outcome["approval_file_absent_during_hold"])
        self.assertNotEqual(outcome["result"], "passed_approval")


class ProviderIdentityTests(unittest.TestCase):
    def test_provider_requires_current_message_kind_and_task_context(self) -> None:
        provider = probe._provider("busy", Path("/unused"))
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        try:
            for wrong_field in (None, "workbench_message_id", "kind", "task_id", "revision_id", "run_id"):
                with self.subTest(wrong_field=wrong_field):
                    expected = {
                        "workbench_message_id": str(uuid4()), "kind": "report",
                        "task_id": str(uuid4()), "revision_id": str(uuid4()),
                        "run_id": str(uuid4()),
                    }
                    provider.semantic_expected[expected["workbench_message_id"]] = expected
                    observed = expected.copy()
                    if wrong_field:
                        observed[wrong_field] = str(uuid4())
                    request = Request(
                        f"http://127.0.0.1:{provider.server_port}/v1/chat/completions",
                        data=json.dumps({"messages": [{"role": "user", "content": json.dumps(observed)}]}).encode(),
                        headers={"Content-Type": "application/json"},
                    )
                    with urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, 200)
                        response.read()
                    matched = provider.semantic_seen.get(expected["workbench_message_id"])
                    self.assertEqual(matched is True, wrong_field is None)
        finally:
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
