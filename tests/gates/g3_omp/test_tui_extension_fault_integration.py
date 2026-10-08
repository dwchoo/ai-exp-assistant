"""Independent live G3 extension failure boundaries, including real reconnect."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import live_omp_probe
import live_tui_extension_fault_probe as probe


class ExtensionFaultIntegrationTests(unittest.TestCase):
    # C-D72 (2): this gate was first observed on a pinned OMP 18.2.10 (historical record). It now
    # records the OMP version it actually ran instead of refusing other versions; what it observes
    # is unchanged. The record goes to stderr and, when WB_G3_OMP_VERSION_RECORD names a file, to
    # that JSON file.
    @classmethod
    def setUpClass(cls) -> None:
        cls.omp = shutil.which("omp")
        if cls.omp is None:
            raise RuntimeError("Actual OMP is required for the G3 integration gate")
        cls.omp_version = probe._omp_version(cls.omp)
        if not cls.omp_version.startswith("omp/"):
            raise RuntimeError(f"OMP version could not be determined: {cls.omp_version!r}")
        # The bridge reports WORKBENCH_G3_EXPECTED_OMP_VERSION; make it the version actually run.
        cls._version_patch = patch.object(live_omp_probe, "OMP_VERSION", cls.omp_version)
        cls._version_patch.start()
        record = {"gate": "G3 extension fault integration", "omp": cls.omp, "omp_version": cls.omp_version,
                  "historical_pin": "omp/18.2.10"}
        print(f"\n[g3-version-record] {json.dumps(record, sort_keys=True)}", file=sys.stderr)
        target = os.environ.get("WB_G3_OMP_VERSION_RECORD")
        if target:
            Path(target).write_text(json.dumps(record, sort_keys=True) + "\n")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._version_patch.stop()

    def assert_clean(self, result: dict[str, object]) -> None:
        # C-D72 (2) delta: the probe waits (bounded, no signal) for its own OMP daemon broker before measuring.
        print(f"\n[g3-broker-wait] {json.dumps(result.get('omp_broker_wait'), sort_keys=True)}", file=sys.stderr)
        self.assertEqual(result["omp_children_remaining"], 0, result)
        self.assertEqual((result.get("omp_broker_wait") or {}).get("remaining"), [], result)
        self.assertTrue(all(not pids for pids in result["cwd_processes_remaining"].values()), result)
        for pid in result["pids"].values():
            self.assertFalse(Path(f"/proc/{pid}").exists())

    def assert_outage(self, result: dict[str, object]) -> None:
        self.assertTrue(result["outage_delivery_attempted"])
        self.assertIn(result["outage_delivery_status"], ("unavailable", "unknown_no_replay"))
        for key in ("host_unreachable_during_fault", "worker_composer_usable_during_fault",
                    "worker_composer_cleared_during_fault", "original_tuis_alive_during_fault",
                    "recovery_same_pid_session_generation", "recovered_public_states_ready",
                    "outage_delivery_not_replayed", "fresh_message_distinct_from_outage"):
            self.assertIs(result[key], True, key)
        for key in ("provider_counts_during_fault", "provider_counts_after_reconnect"):
            self.assertEqual(result[key], {"manager": 1, "worker": 1}, key)
        self.assertEqual(result["agent_starts_during_fault"], result["baseline_agent_starts"])
        self.assertEqual(result["agent_ends_during_fault"], {"manager": 1, "worker": 1})
        self.assertEqual(result["final_provider_counts"], {"manager": 1, "worker": 2})
        self.assertIs(result["fresh_delivery_after_recovery"], True)

    def test_outage_request_is_not_success_or_replayed_after_same_process_reconnect(self) -> None:
        result = probe.run(self.omp, outage_delivery=True)
        self.assert_clean(result)
        self.assert_outage(result)

    def test_live_outage_false_ack_control_is_rejected(self) -> None:
        original = probe.BridgeHarness.request
        mutations = []

        def false_ack(server, role, frame, **kwargs):
            if frame["kind"] == "deliver" and role not in server.peers:
                mutations.append(json.loads(frame["envelope"])["messageId"])
                return {"status": "api_accepted", "modelProcessed": False}
            return original(server, role, frame, **kwargs)

        with patch.object(probe.BridgeHarness, "request", false_ack):
            result = probe.run(self.omp, outage_delivery=True)
        self.assert_clean(result)
        self.assertEqual(mutations, [result["outage_message_id"]])
        self.assertEqual(result["outage_delivery_status"], "api_accepted")
        with self.assertRaises(AssertionError):
            self.assert_outage(result)

    def test_opt_in_fault_before_enqueue_survives_real_reconnect_without_replay(self) -> None:
        observations = []

        def exercise(server, peers, providers, screens, children, result, **_kwargs):
            def counts():
                return tuple((providers[role].RequestHandlerClass.requests,
                              server.event_count(role, "agent_start"),
                              server.event_count(role, "agent_end")) for role in probe.ROLES)

            def assert_delivery(record):
                self.assertEqual(record["ack_status"], "api_accepted")
                self.assertIs(record["model_processed_at_ack"], False)
                self.assertIs(record["provider_fields_match"], True)
                self.assertEqual(record["target_provider_delta"], 1)
                self.assertEqual(record["other_provider_delta"], 0)
                self.assertEqual(record["target_agent_end_delta"], 1)
                self.assertEqual(record["other_agent_end_delta"], 0)
                self.assertIs(record["end_session_match"], True)

            # Worker baseline already proves env-only normal delivery. Manager
            # lacks the opt-in env: an explicit diagnostic must remain ordinary.
            original = server.request

            def diagnostic_without_opt_in(role, frame, **kwargs):
                if role == "manager" and frame["kind"] == "deliver":
                    frame = {**frame, "diagnosticFault": "handler_exception"}
                return original(role, frame, **kwargs)

            with patch.object(server, "request", diagnostic_without_opt_in):
                assert_delivery(probe._deliver(server, providers, peers, "manager",
                                              probe.MessageKind.ANSWER, probe.ActorRole.WORKER))
            self.assertFalse(any(event["name"].startswith("diagnostic_handler_fault:")
                                 for event in server.events))
            self.assertTrue(probe._ready(server, screens, peers))
            before = counts()
            worker = peers["worker"]
            malformed_id = str(uuid4())
            malformed = (f'{{"kind":"deliver","requestId":"{malformed_id}",'
                         '"diagnosticFault":"handler_exception",}\n').encode()
            with worker["write_lock"]:
                worker["socket"].sendall(malformed)
            original("worker", {"kind": "probe"})  # Ordered read-back boundary.
            self.assertNotIn(malformed_id, server.acks)
            self.assertEqual(counts(), before)
            self.assertFalse(any(event["name"].startswith("diagnostic_handler_fault:")
                                 for event in server.events))

            fault = probe._fault_envelope(worker)
            request_id = str(uuid4())
            frame = {"kind": "deliver", "requestId": request_id,
                     "envelope": fault.to_json(), "diagnosticFault": "handler_exception"}
            with worker["write_lock"]:
                worker["socket"].sendall((json.dumps(frame) + "\n").encode())
            original("worker", {"kind": "probe"})
            event = server.wait_event("worker", f"diagnostic_handler_fault:{request_id}", timeout=5)
            self.assertEqual(event["sessionId"], worker["session_id"])
            self.assertEqual(event["generation"], worker["generation"])

            def assert_no_processing():
                self.assertNotIn(request_id, server.acks)
                self.assertEqual(counts(), before)
                self.assertNotIn(fault.message_id, providers["worker"].semantic_seen)

            assert_no_processing()
            # Mutate only the observed host ACK table after a real handler fault;
            # this assertion must discriminate false success, not trust a flag.
            server.acks[request_id] = {"status": "api_accepted"}
            try:
                with self.assertRaises(AssertionError):
                    assert_no_processing()
            finally:
                server.acks.pop(request_id)

            worker["socket"].shutdown(socket.SHUT_RDWR)
            worker["socket"].close()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                replacement = server.peers.get("worker")
                if replacement is not None and replacement["socket"] is not worker["socket"]:
                    break
                time.sleep(0.05)
            else:
                self.fail("Worker did not reconnect")
            for key in ("pid", "session_id", "generation"):
                self.assertEqual(replacement[key], worker[key], key)
            peers["worker"] = replacement
            self.assertTrue(probe._ready(server, screens, peers))
            retry = original("worker", {"kind": "deliver", "envelope": fault.to_json()})
            self.assertEqual(retry["status"], "unknown_no_replay")
            assert_no_processing()
            draft = f"independent-fault-{uuid4().hex}"
            os.write(int(children[1]["fd"]), draft.encode())
            self.assertTrue(probe._wait_screen(*screens["worker"], draft, present=True))
            os.write(int(children[1]["fd"]), b"\x7f" * len(draft))
            self.assertTrue(probe._wait_screen(*screens["worker"], draft, present=False))
            fresh = probe._deliver(server, providers, peers, "worker",
                                   probe.MessageKind.TASK, probe.ActorRole.MANAGER)
            assert_delivery(fresh)
            self.assertNotEqual(fresh["message_id"], fault.message_id)
            self.assertNotIn(fault.message_id, providers["worker"].semantic_seen)
            self.assertTrue(probe._ready(server, screens, peers))
            observations.append((fault.message_id, retry["status"]))

        with patch.object(probe, "_malformed_frame", exercise):
            result = probe.run(self.omp, handler_fault=True)
        self.assert_clean(result)
        self.assertEqual(len(observations), 1, result)


if __name__ == "__main__":
    unittest.main()
