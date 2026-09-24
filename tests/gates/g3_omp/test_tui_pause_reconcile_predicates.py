"""Independent boundary controls for the two-TUI pause/reconcile probe.

These doubles validate the probe's conclusions. Real OMP behavior is established
only by the separate live run, not by these tests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import live_tui_pause_reconcile_probe as probe


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
        self.peers = {
            role: {"pid": 990001 + index, "session_id": str(uuid4()), "generation": 1}
            for index, role in enumerate(probe.ROLES)
        }
        self.root: Path | None = None
        self.events: list[dict[str, object]] = []
        self.deliveries: list[tuple[str, dict[str, object]]] = []
        self.resume_requests: list[tuple[str, bool]] = []
        self.paused = {role: False for role in probe.ROLES}
        self.ever_paused = {role: False for role in probe.ROLES}
        self.approval_pending = False
        self.tool_in_flight = False
        self.post_resume_ready = True

    def serve_forever(self) -> None:
        pass

    def peer(self, role: str, **_kwargs: object) -> dict[str, object]:
        return self.peers[role]

    def _event(self, role: str, name: str, **fields: object) -> None:
        peer = self.peers[role]
        self.events.append({"role": role, "name": name,
                            "sessionId": peer["session_id"], "generation": peer["generation"],
                            **fields})

    def request(self, role: str, frame: dict[str, object], **_kwargs: object) -> dict[str, object]:
        kind = frame["kind"]
        if kind == "probe":
            peer = self.peers[role]
            return {"state": {
                "sessionId": peer["session_id"], "generation": peer["generation"],
                "idle": self.post_resume_ready, "pending": False,
                "editorKnown": True, "editorEmpty": True,
                "approvalPending": self.approval_pending if role == "manager" else False,
                "inFlightToolCount": int(self.tool_in_flight) if role == "manager" else 0,
                "paused": self.paused[role],
                "abortStatus": ("stop_observed" if self.paused[role] else "none")
                if role == "manager" else "none",
                "unknownOutcomeToolCallIds": ["pause-probe-bash"]
                if role == "manager" and self.ever_paused[role] else [],
            }}
        if kind == "pause":
            self.paused[role] = True
            self.ever_paused[role] = True
            if role == "manager":
                if self.defect != "stop_unobserved":
                    self._event(role, "turn_stop_observed")
                self._event(role, "agent_end")
                self.tool_in_flight = False
                return {"status": "abort_requested", "unconfirmedToolCallIds":
                        [] if self.defect == "unknown_missing_at_ack" else ["pause-probe-bash"]}
            return {"status": "paused"}
        if kind == "resume":
            reconciled = frame.get("reconciled") is True
            self.resume_requests.append((role, reconciled))
            if not reconciled:
                return {"status": "resumed" if self.defect == "resume_without_checks"
                        else "reconciliation_required"}
            self.paused[role] = False
            if self.defect == "not_ready_after_resume":
                self.post_resume_ready = False
            return {"status": "resumed"}
        envelope = json.loads(str(frame["envelope"]))
        self.deliveries.append((role, envelope))
        if self.paused[role]:
            if self.defect == "provider_while_paused":
                self.providers[role].RequestHandlerClass.requests += 1
            if self.defect == "agent_start_while_paused":
                self._event(role, "agent_start")
            return {"status": "deferred"}
        if len(self.deliveries) <= 4:
            if self.defect == "replayed_held":
                self.providers[role].RequestHandlerClass.requests += 1
            return {"status": "unknown_no_replay"}
        provider = self.providers[role]
        if self.defect != "missing_fresh_provider":
            provider.RequestHandlerClass.requests += 1
            seen_id = str(uuid4()) if self.defect == "wrong_fresh_id" else envelope["messageId"]
            provider.semantic_seen[seen_id] = {"matched": self.defect != "wrong_fresh_context"}
        if self.defect == "opposite_role_provider":
            self.providers["worker"].RequestHandlerClass.requests += 1
        if self.defect != "missing_fresh_end":
            self._event(role, "agent_end")
            if self.defect == "stale_session_end":
                self.events[-1]["sessionId"] = str(uuid4())
            if self.defect == "wrong_generation_end":
                self.events[-1]["generation"] = 2
        return {"status": "api_accepted", "modelProcessed": self.defect == "processed_at_ack"}

    def wait_event(self, role: str, name: str, **_kwargs: object) -> dict[str, object]:
        assert self.root is not None
        if name == "tool_approval_requested":
            self.approval_pending = True
        elif name == "tool_approval_resolved":
            self.approval_pending = False
        elif name == "tool_execution_start":
            self.tool_in_flight = True
            (self.root / "cwd-manager" / "started.txt").write_text("STARTED")
        self._event(role, name, approved=True)
        return self.events[-1]

    def event_count(self, role: str, name: str) -> int:
        return sum(event["role"] == role and event["name"] == name for event in self.events)

    def shutdown(self) -> None:
        pass

    def server_close(self) -> None:
        pass


class PauseOutcomeTests(unittest.TestCase):
    def _run_case(self, defect: str = "") -> tuple[dict[str, object], _Bridge]:
        providers: dict[str, _Provider] = {}
        holder: list[_Bridge] = []
        stopped = False

        def provider_factory(**_kwargs: object) -> _Provider:
            role = probe.ROLES[len(providers)]
            providers[role] = _Provider()
            return providers[role]

        def bridge_factory(path: Path, _tokens: dict[str, str]) -> _Bridge:
            bridge = _Bridge(providers, defect)
            bridge.root = path.parent
            holder.append(bridge)
            return bridge

        def start(_omp: str, _root: Path, role: str, _token: str) -> dict[str, object]:
            (_root / f"cwd-{role}").mkdir()
            return {"role": role, "pid": holder[0].peers[role]["pid"],
                    "fd": os.open(os.devnull, os.O_WRONLY)}

        def stop(_children: list[dict[str, object]]) -> None:
            nonlocal stopped
            stopped = True

        def processes(cwd: Path) -> list[int]:
            return [] if stopped else [int(holder[0].peers[cwd.name.removeprefix("cwd-")]["pid"])]

        def reconcile(_server: _Bridge, _root: Path, _peer: dict[str, object],
                      _held: dict[str, object], _status: str, _files: dict[str, object],
                      _processes: list[int]) -> tuple[dict[str, object], bool]:
            return {"checks": {"files": True}, "negative_withheld": {}}, True

        with (
            patch.object(probe, "semantic_provider", side_effect=provider_factory),
            patch.object(probe, "BridgeHarness", side_effect=bridge_factory),
            patch.object(probe, "_start", side_effect=start),
            patch.object(probe, "_stop_omps", side_effect=stop),
            patch.object(probe, "_drain_visible"),
            patch.object(probe, "_screen_state", return_value={"composer_visible": True}),
            patch.object(probe, "_wait_until", side_effect=lambda predicate, _timeout: bool(predicate())),
            patch.object(probe, "_reconcile", side_effect=reconcile),
            patch.object(probe, "cwd_processes", side_effect=processes),
            patch.object(probe.os, "chmod"),
            patch.object(probe.os, "write"),
            patch.object(probe.time, "sleep"),
        ):
            outcome = probe.run("unused-omp")
        return outcome, holder[0]

    def test_pause_ack_turn_stop_unknown_and_both_roles_held_before_explicit_resume(self) -> None:
        outcome, bridge = self._run_case()
        self.assertEqual(outcome["result"], "passed_pair_tui")
        self.assertEqual(outcome["manager_pause_ack"], "abort_requested")
        self.assertTrue(outcome["turn_stop_observed"])
        self.assertFalse(outcome["tool_end_at_ack"])
        self.assertEqual(outcome["post_resume_state"]["manager"]["unknownOutcomeToolCallIds"],
                         ["pause-probe-bash"])
        self.assertEqual(outcome["paused_delivery"], {"manager": "deferred", "worker": "deferred"})
        self.assertEqual(outcome["provider_delta_while_paused"], {"manager": 0, "worker": 0})
        self.assertEqual(outcome["agent_start_delta_while_paused"], {"manager": 0, "worker": 0})
        self.assertEqual(bridge.resume_requests,
                         [("manager", False), ("worker", False), ("manager", True), ("worker", True)])
        self.assertEqual(outcome["held_retry_after_resume"],
                         {"manager": "unknown_no_replay", "worker": "unknown_no_replay"})
        self.assertEqual(outcome["omp_children_remaining"], 0)
        self.assertEqual(outcome["cwd_processes_remaining"], {"manager": [], "worker": []})

    def test_fresh_delivery_requires_current_provider_and_separate_turn_completion(self) -> None:
        for defect in ("missing_fresh_provider", "wrong_fresh_id", "wrong_fresh_context",
                       "missing_fresh_end", "processed_at_ack", "opposite_role_provider",
                       "replayed_held", "not_ready_after_resume"):
            with self.subTest(defect=defect):
                outcome, _ = self._run_case(defect)
                self.assertNotEqual(outcome["result"], "passed_pair_tui")

    def test_ack_alone_or_paused_activity_cannot_authorize_resume(self) -> None:
        for defect in ("stop_unobserved", "unknown_missing_at_ack",
                       "resume_without_checks", "provider_while_paused",
                       "agent_start_while_paused"):
            with self.subTest(defect=defect):
                outcome, bridge = self._run_case(defect)
                self.assertFalse(outcome["explicit_resume_allowed"])
                self.assertFalse(any(reconciled for _, reconciled in bridge.resume_requests))

    def test_stale_session_or_generation_end_does_not_complete_fresh_delivery(self) -> None:
        for defect in ("stale_session_end", "wrong_generation_end"):
            with self.subTest(defect=defect):
                outcome, _ = self._run_case(defect)
                self.assertNotEqual(outcome["result"], "passed_pair_tui")


class ReconciliationScopeTests(unittest.TestCase):
    def test_changed_files_symlink_process_task_and_approval_withhold_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw04-pause-predicate-") as temporary:
            root = Path(temporary)
            cwd = root / "cwd-manager"
            cwd.mkdir()
            (cwd / "started.txt").write_text("STARTED")
            peer = {"pid": os.getpid()}
            held = {key: str(uuid4()) for key in ("taskId", "revisionId", "runId", "messageId")}
            state = {"paused": True, "abortStatus": "stop_observed",
                     "unknownOutcomeToolCallIds": ["pause-probe-bash"]}
            with (
                patch.object(probe, "_state", return_value=state),
                patch.object(probe, "cwd_processes", return_value=[os.getpid()]),
            ):
                evidence, allowed = probe._reconcile(
                    object(), root, peer, held, "deferred", probe._files(cwd), [os.getpid()]
                )
            self.assertTrue(allowed)
            self.assertTrue(all(evidence["checks"].values()))
            self.assertEqual(evidence["negative_withheld"], {
                "task": True, "approval": True, "process": True, "file": True, "symlink": True,
            })

    def test_unknown_tool_or_changed_live_scope_withholds_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw04-pause-predicate-") as temporary:
            root = Path(temporary)
            cwd = root / "cwd-manager"
            cwd.mkdir()
            (cwd / "started.txt").write_text("STARTED")
            peer = {"pid": os.getpid()}
            held = {key: str(uuid4()) for key in ("taskId", "revisionId", "runId", "messageId")}
            expected_files = probe._files(cwd)
            for change in ("tool", "file", "symlink", "process"):
                with self.subTest(change=change):
                    state = {"paused": True, "abortStatus": "stop_observed",
                             "unknownOutcomeToolCallIds": [] if change == "tool" else ["pause-probe-bash"]}
                    if change == "file":
                        (cwd / "started.txt").write_text("CHANGED")
                    if change == "symlink":
                        (cwd / "started.txt").unlink()
                        (cwd / "started.txt").symlink_to(root / "elsewhere")
                    with (
                        patch.object(probe, "_state", return_value=state),
                        patch.object(probe, "cwd_processes", return_value=[] if change == "process" else [os.getpid()]),
                    ):
                        _, allowed = probe._reconcile(
                            object(), root, peer, held, "deferred", expected_files, [os.getpid()]
                        )
                    self.assertFalse(allowed)
                    shutil.rmtree(root / "negative-scope")
                    if change == "file":
                        (cwd / "started.txt").write_text("STARTED")
                    if change == "symlink":
                        (cwd / "started.txt").unlink()
                        (cwd / "started.txt").write_text("STARTED")


if __name__ == "__main__":
    unittest.main()
