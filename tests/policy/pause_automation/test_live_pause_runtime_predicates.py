"""Negative controls for the actual OMP CW12 probe's final pass predicate."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from live_pause_runtime_probe import OMP_VERSION, qualifies  # noqa: E402


class LivePauseRuntimePredicateTests(unittest.TestCase):
    def setUp(self):
        task = {"task_id": "task", "revision": 1, "run_id": "run",
                "approval_hash": "a" * 64}
        self.good = {
            "omp_version": OMP_VERSION, "initial_ready": True,
            "distinct_peers": True, "persisted_binding": True,
            "peer_identity": {
                "manager": {"session_id": "manager", "generation": 1, "pid": 100},
                "worker": {"session_id": "worker", "generation": 1, "pid": 101},
            },
            "task_identity": task,
            "binding_identity": {
                **task, "approved_scope_hash": "b" * 64,
                "approved_paths": ["outcome.txt"],
                "manager_session_id": "manager", "manager_generation": 1,
                "worker_session_id": "worker", "worker_generation": 1,
            },
            "persisted_scope": {"hash": "b" * 64, "paths": ["outcome.txt"]},
            "paused_state_identity": {
                "manager": {"session_id": "manager", "generation": 1},
                "worker": {"session_id": "worker", "generation": 1},
            },
            "status_identity": {
                "manager": {"session_id": "manager", "generation": 1},
                "worker": {"session_id": "worker", "generation": 1},
                "host": task,
            },
            "abort_request_correlated": True,
            "abort_journal_request_id": "abort-1",
            "abort_event": {"request_id": "abort-1", "session_id": "manager",
                            "generation": 1, "role": "manager"},
            "pause": {"paused": True, "manager_ack": "abort_requested",
                      "worker_ack": "paused", "abort": "stop_observed",
                      "stop_observed": True, "unknown_tools": ["pause-probe-bash"]},
            "native_approval_pending": True, "native_approval_resolved": True,
            "tool_started": True, "result_absent_before_pause": True,
            "automatic_held": True, "cw11_admission": "paused",
            "host_collection": {"paused": True, "run_id_matches": True,
                                "shell_state": "running"},
            "host_experiment_preserved": True,
            "manager_files": {"scope_valid": True,
                              "started.txt": sha256(b"STARTED").hexdigest(),
                              "result.txt": None,
                              "other_entries_sha256": sha256(b"[]").hexdigest()},
            "status_file_changes": [{"path": "cwd-manager/started.txt",
                                     "change": "created", "confirmed_at": "2026-09-28T00:00:00Z",
                                     "content_hash": sha256(b"STARTED").hexdigest()}],
            "host_child_pid": 102, "host_process_count": 3,
            "status_running_processes": [{"pid": 102,
                                          "command": "printf HOST_STARTED; sleep 30; printf HOST_DONE > outcome.txt",
                                          "observed_at": "2026-09-28T00:00:00Z",
                                          "state": "running"}],
            "cwd_processes": {"manager": [100], "worker": [101]},
            "status_unknown_rows": {"manager": ["pause-probe-bash"],
                                    "worker": [], "host": ["pause-probe-bash"]},
            "manual_activity": {"provider_delta": 1, "start_session_matches": True,
                                "end_session_matches": True, "omp_remains_paused": True,
                                "coordinator_remains_paused": True, "automatic_held": True},
            "paused_provider_delta": {"manager": 0, "worker": 0},
            "no_paused_provider_requests": True, "no_paused_agent_start": True,
            "three_area_paused": True, "three_area_identity": True,
            "reconciliation": {key: True for key in
                               ("filesMatch", "toolsMatch", "processesMatch",
                                "taskMatch", "approvalMatch")},
            "held_ack": "deferred", "resumed": True,
            "out_of_scope_rejected": True,
            "removed_out_of_scope_file": True,
            "reconciliation_after_cleanup": True,
            "directory_mode_rejected": True,
            "directory_mode_restored": True,
            "held_retry_ack": "unknown_no_replay",
            "bound_automatic_transport": True,
            "fresh_delivery": {"dispatch": "admitted", "status": "omp_processed"},
            "fresh_provider_delta": 1,
            "fresh_agent_start": True, "fresh_agent_end": True,
            "zero_residue": True,
            "omp_children_remaining": 0,
            "cwd_processes_remaining": {"manager": [], "worker": []},
            "host_processes_remaining": [], "host_parent_remaining": False,
            "drain_threads_remaining": 0, "bridge_socket_removed": True,
            "bridge_thread_remaining": False, "provider_threads_remaining": 0,
        }

    def test_positive_control_requires_all_independent_boundaries(self):
        self.assertTrue(qualifies(self.good))

    def test_out_of_scope_peer_output_must_be_rejected_before_resume(self):
        for key in ("out_of_scope_rejected", "removed_out_of_scope_file",
                    "reconciliation_after_cleanup", "directory_mode_rejected",
                    "directory_mode_restored"):
            with self.subTest(key=key):
                bad = deepcopy(self.good)
                bad[key] = False
                self.assertFalse(qualifies(bad))

    def test_wrong_identity_stop_and_scope_cannot_pass(self):
        mutations = {
            "replacement_stop_session": ("abort_event", "session_id", "replacement"),
            "wrong_stop_request": ("abort_event", "request_id", "foreign-abort"),
            "missing_stop": ("abort_event", None, None),
            "wrong_manager_generation": ("paused_state_identity", "manager", {"session_id": "manager", "generation": 2}),
            "wrong_worker_status_session": ("status_identity", "worker", {"session_id": "replacement", "generation": 1}),
            "wrong_task_run": ("binding_identity", "run_id", "foreign-run"),
            "wrong_scope": ("persisted_scope", "hash", "c" * 64),
        }
        for label, (field, nested, value) in mutations.items():
            with self.subTest(label=label):
                bad = deepcopy(self.good)
                if nested is None:
                    bad[field] = value
                else:
                    bad[field][nested] = value
                self.assertFalse(qualifies(bad))

    def test_paused_activity_incomplete_reconciliation_and_replay_cannot_pass(self):
        mutations = {
            "paused_provider": ("paused_provider_delta", "manager", 1),
            "paused_agent": ("no_paused_agent_start", None, False),
            "file_change": ("reconciliation", "filesMatch", False),
            "unknown_tool": ("reconciliation", "toolsMatch", False),
            "process_change": ("reconciliation", "processesMatch", False),
            "task_change": ("reconciliation", "taskMatch", False),
            "approval_change": ("reconciliation", "approvalMatch", False),
            "held_replay": ("held_retry_ack", None, "omp_processed"),
            "host_not_collected": ("host_collection", "paused", False),
        }
        for label, (field, nested, value) in mutations.items():
            with self.subTest(label=label):
                bad = deepcopy(self.good)
                if nested is None:
                    bad[field] = value
                else:
                    bad[field][nested] = value
                self.assertFalse(qualifies(bad))

    def test_missing_fresh_turn_or_cleanup_residue_cannot_pass(self):
        mutations = {
            "fresh_delivery_missing": ("fresh_delivery", "status", "deferred"),
            "unbound_automatic_delivery": ("bound_automatic_transport", None, False),
            "fresh_provider_missing": ("fresh_provider_delta", None, 0),
            "fresh_agent_start_missing": ("fresh_agent_start", None, False),
            "fresh_agent_end_missing": ("fresh_agent_end", None, False),
            "cleanup_residue": ("zero_residue", None, False),
            "child_residue_even_if_flag_true": ("omp_children_remaining", None, 1),
            "provider_thread_residue_even_if_flag_true": ("provider_threads_remaining", None, 1),
        }
        for label, (field, nested, value) in mutations.items():
            with self.subTest(label=label):
                bad = deepcopy(self.good)
                if nested is None:
                    bad[field] = value
                else:
                    bad[field][nested] = value
                self.assertFalse(qualifies(bad))

    def test_projected_file_process_unknown_and_manual_evidence_is_required(self):
        mutations = {
            "invalid_files": ("manager_files", "scope_valid", False),
            "missing_file_row": ("status_file_changes", None, []),
            "wrong_file_hash": ("status_file_changes", 0, {**self.good["status_file_changes"][0],
                                                           "content_hash": "0" * 64}),
            "missing_process_row": ("status_running_processes", None, []),
            "wrong_process_pid": ("status_running_processes", 0,
                                  {**self.good["status_running_processes"][0], "pid": 999}),
            "wrong_process_state": ("status_running_processes", 0,
                                    {**self.good["status_running_processes"][0], "state": "exited"}),
            "missing_host_process": ("host_process_count", None, 0),
            "missing_unknown_manager": ("status_unknown_rows", "manager", []),
            "missing_unknown_worker_row": ("status_unknown_rows", "worker", None),
            "missing_unknown_host": ("status_unknown_rows", "host", []),
            "foreign_unknown_manager": ("status_unknown_rows", "manager", ["other-tool"]),
            "manual_provider_missing": ("manual_activity", "provider_delta", 0),
            "manual_not_paused": ("manual_activity", "omp_remains_paused", False),
            "manual_wrong_session": ("manual_activity", "start_session_matches", False),
            "manual_coordinator_unpaused": ("manual_activity", "coordinator_remains_paused", False),
            "manual_automatic_sent": ("manual_activity", "automatic_held", False),
        }
        for label, (field, nested, value) in mutations.items():
            with self.subTest(label=label):
                bad = deepcopy(self.good)
                if nested is None:
                    bad[field] = value
                else:
                    bad[field][nested] = value
                self.assertFalse(qualifies(bad))

    def test_unbounded_provider_or_terminal_text_cannot_be_reported_as_pass(self):
        for key in ("raw_prompt", "provider_response_body", "terminal_output", "private_token"):
            with self.subTest(key=key):
                bad = deepcopy(self.good)
                bad[key] = "sensitive text"
                self.assertFalse(qualifies(bad))


if __name__ == "__main__":
    unittest.main()
