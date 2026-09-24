"""Independent boundary tests for the real TUI in-flight /new probe."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import live_tui_session_inflight_probe as probe


class _Release(threading.Event):
    def __init__(self, provider: object):
        super().__init__()
        self.provider = provider

    def set(self) -> None:
        super().set()
        self.provider.response_sent.add(1)


class _Started(threading.Event):
    def wait(self, _timeout: float | None = None) -> bool:
        return self.is_set()


class SessionInflightPredicateTests(unittest.TestCase):
    def _run_case(self, **changes: object) -> dict[str, object]:
        class Provider:
            server_port = 12345

            def __init__(self):
                self.lock = threading.Lock()
                self.requests = 0
                self.expected: dict[str, dict[str, str]] = {}
                self.seen: dict[str, list[bool]] = {}
                self.request_message_ids: dict[int, list[str]] = {}
                self.response_sent: set[int] = set()
                self.request_started = _Started()
                self.release_first = _Release(self)

            def serve_forever(self) -> None:
                pass

            def shutdown(self) -> None:
                pass

            def server_close(self) -> None:
                pass

        provider = Provider()
        old_session = str(uuid4())
        new_session = str(uuid4())
        old_socket = SimpleNamespace(sendall=lambda _data: None)
        old_peer = {
            "pid": 901002, "session_id": old_session, "generation": 1,
            "socket": old_socket, "write_lock": threading.Lock(),
        }
        new_peer = {
            "pid": 901003 if changes.get("different_pid") else old_peer["pid"],
            "session_id": old_session if changes.get("same_session") else new_session,
            "generation": 1 if changes.get("same_generation") else 2,
            "socket": old_socket if changes.get("same_socket") else SimpleNamespace(sendall=lambda _data: None),
            "write_lock": threading.Lock(),
        }

        class Bridge:
            def __init__(self, *_args: object):
                self.condition = threading.Condition()
                self.events: list[dict[str, object]] = []
                self.transitioned = False
                self.deliveries = 0
                self.old_id = ""

            def serve_forever(self) -> None:
                pass

            def peer(self, role: str, **_kwargs: object) -> dict[str, object]:
                if role == "manager":
                    return {"pid": 901001, "session_id": str(uuid4()), "generation": 1}
                return new_peer if self.transitioned else old_peer

            def request(self, _role: str, request: dict[str, object], **_kwargs: object) -> dict[str, object]:
                kind = request["kind"]
                if kind == "probe":
                    peer = new_peer if self.transitioned else old_peer
                    return {"state": {
                        "sessionId": peer["session_id"], "generation": peer["generation"],
                        "idle": not self.transitioned or not changes.get("new_busy"),
                        "pending": bool(self.transitioned and changes.get("new_pending")),
                        "editorKnown": True, "editorEmpty": True,
                        "editorLength": 4 if not self.transitioned else 0,
                        "paused": bool(self.transitioned and changes.get("new_paused")),
                    }}
                if kind == "resume":
                    return {"status": changes.get("resume_ack", "resumed")}
                if kind != "deliver":
                    raise AssertionError(kind)
                self.deliveries += 1
                envelope = json.loads(request["envelope"])
                message_id = envelope["messageId"]
                if self.deliveries == 1:
                    return {"status": changes.get("old_retry_ack", "rejected")}
                provider.requests += 1
                provider.seen[message_id] = [not changes.get("wrong_new_context", False)]
                provider.request_message_ids[2] = [
                    self.old_id if changes.get("replayed_old") else message_id
                ]
                if changes.get("duplicate_provider"):
                    provider.requests += 1
                    provider.request_message_ids[3] = [message_id]
                if changes.get("new_agent_end", True):
                    self.events.append({
                        "role": "manager" if changes.get("wrong_end_role") else "worker",
                        "name": "agent_end",
                        "sessionId": old_session if changes.get("stale_end") else new_peer["session_id"],
                        "generation": 1 if changes.get("stale_end") else new_peer["generation"],
                    })
                return {
                    "status": changes.get("new_ack", "api_accepted"),
                    "modelProcessed": changes.get("processed_at_ack", False),
                }

            def event_count(self, role: str, name: str) -> int:
                return sum(event.get("role") == role and event.get("name") == name
                           for event in self.events)

            def shutdown(self) -> None:
                pass

            def server_close(self) -> None:
                pass

        bridge = Bridge()
        def ack_pair(_server: object, _ids: object, **_kwargs: object) -> tuple[dict[str, object], dict[str, object]]:
            old_id = next(iter(provider.expected))
            bridge.old_id = old_id
            provider.requests = 1
            provider.seen[old_id] = [True]
            provider.request_message_ids[1] = [old_id]
            if not changes.get("no_provider_start"):
                provider.request_started.set()
            return (
                {"status": changes.get("deliver_ack", "unknown_no_replay"),
                 "modelProcessed": None},
                {"status": changes.get("pause_ack", "paused")},
            )

        def new_peer_wait(_server: object, _old: object, _timeout: object = None,
                          **_kwargs: object) -> dict[str, object] | None:
            if changes.get("switch_after_release"):
                provider.release_first.set()
            bridge.transitioned = True
            return new_peer

        def screen_state(_screen: object, _lock: object, draft: str = "") -> dict[str, bool]:
            if draft == "/new":
                return {"draft_in_composer": True}
            return {"composer_visible": not (bridge.transitioned and changes.get("new_composer_hidden", False))}

        def start(_omp: str, role: str, *_args: object, **_kwargs: object) -> dict[str, object]:
            if changes.get("cwd_residual"):
                (Path(_args[1]) / f"cwd-{role}").mkdir(exist_ok=True)
            return {"pid": 901001 if role == "manager" else 901002,
                    "fd": os.open(os.devnull, os.O_WRONLY)}

        clock = SimpleNamespace(now=100.0)

        def tick() -> float:
            clock.now += 0.5
            return clock.now

        original_exists = Path.exists

        def exists(path: Path) -> bool:
            if changes.get("child_residual") and str(path) == "/proc/901002":
                return True
            return original_exists(path)

        with (
            patch.object(probe, "_provider", return_value=provider),
            patch.object(probe, "BridgeHarness", return_value=bridge),
            patch.object(probe, "_persistent_omp_launcher", return_value="unused-omp"),
            patch.object(probe, "_start_omp", side_effect=start),
            patch.object(probe, "_stop_omps"),
            patch.object(probe, "_drain_visible"),
            patch.object(probe, "_screen_state", side_effect=screen_state),
            patch.object(probe, "_wait_ack_pair", side_effect=ack_pair),
            patch.object(probe, "_wait_new_peer", side_effect=new_peer_wait),
            patch.object(probe, "cwd_processes", return_value=[901002] if changes.get("cwd_residual") else []),
            patch.object(probe.Path, "exists", exists),
            patch.object(probe.os, "chmod"),
            patch.object(probe.os, "write"),
            patch.object(probe.time, "monotonic", side_effect=tick),
            patch.object(probe.time, "sleep"),
        ):
            outcome = probe.run("unused-omp")
        return outcome

    def test_current_session_provider_and_end_can_pass(self) -> None:
        result = self._run_case()
        self.assertEqual(result["result"], "observed_session_inflight_isolation")
        self.assertEqual(result["provider_request_identities"], {"1": ["old"], "2": ["new"]})
        self.assertIs(result["new_model_processed_at_ack"], False)
        self.assertEqual(result["omp_children_remaining"], 0)
        self.assertTrue(all(not pids for pids in result["cwd_processes_remaining"].values()))

    def test_old_retry_accept_or_replay_cannot_pass(self) -> None:
        for changes in ({"old_retry_ack": "api_accepted"}, {"replayed_old": True}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self._run_case(**changes)["result"], "observed_session_inflight_isolation")

    def test_wrong_provider_context_or_duplicate_request_cannot_pass(self) -> None:
        for changes in ({"wrong_new_context": True}, {"duplicate_provider": True}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self._run_case(**changes)["result"], "observed_session_inflight_isolation")

    def test_stale_or_missing_new_agent_end_cannot_pass(self) -> None:
        for changes in ({"stale_end": True}, {"wrong_end_role": True},
                        {"new_agent_end": False}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self._run_case(**changes)["result"], "observed_session_inflight_isolation")

    def test_api_ack_alone_cannot_pass(self) -> None:
        for changes in ({"new_ack": "api_accepted", "wrong_new_context": True},
                        {"new_ack": "rejected"}, {"processed_at_ack": True}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self._run_case(**changes)["result"], "observed_session_inflight_isolation")

    def test_provider_must_start_before_switch_and_switch_must_overlap_hold(self) -> None:
        for changes in ({"no_provider_start": True}, {"switch_after_release": True}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self._run_case(**changes)["result"], "observed_session_inflight_isolation")

    def test_new_session_must_be_ready(self) -> None:
        for changes in ({"new_busy": True}, {"new_pending": True}, {"new_paused": True,
                         "resume_ack": "rejected"}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self._run_case(**changes)["result"], "observed_session_inflight_isolation")

    def test_hidden_new_composer_cannot_prove_ready_session(self) -> None:
        self.assertNotEqual(self._run_case(new_composer_hidden=True)["result"],
                            "observed_session_inflight_isolation")

    def test_cleanup_residual_is_reported_for_cli_gate(self) -> None:
        for changes in ({"child_residual": True}, {"cwd_residual": True}):
            with self.subTest(changes=changes):
                result = self._run_case(**changes)
                clean = (result["omp_children_remaining"] == 0
                         and all(not pids for pids in result["cwd_processes_remaining"].values()))
                self.assertFalse(clean)

    def test_new_hello_must_replace_socket(self) -> None:
        old = {"pid": 2, "session_id": "old", "generation": 1, "socket": object()}
        same_socket = {"pid": 2, "session_id": "new", "generation": 2,
                       "socket": old["socket"]}
        server = SimpleNamespace(peer=lambda _role, **_kwargs: same_socket)
        self.assertIsNone(probe._wait_new_peer(server, old, timeout=0.01))

    def test_stale_session_generation_or_pid_hello_is_ignored(self) -> None:
        old = {"pid": 2, "session_id": "old", "generation": 1, "socket": object()}
        for candidate in (
            {"pid": 2, "session_id": "old", "generation": 2, "socket": object()},
            {"pid": 2, "session_id": "new", "generation": 1, "socket": object()},
            {"pid": 3, "session_id": "new", "generation": 2, "socket": object()},
        ):
            with self.subTest(candidate=candidate):
                server = SimpleNamespace(peer=lambda _role, **_kwargs: candidate)
                self.assertIsNone(probe._wait_new_peer(server, old, timeout=0.01))


if __name__ == "__main__":
    unittest.main()
