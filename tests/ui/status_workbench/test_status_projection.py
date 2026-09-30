from __future__ import annotations

import unittest

from workbench.policy.pause_automation import PauseBinding, PauseStatus
from workbench.ui.status_workbench import (
    AreaObservation, ConfirmedFileChange, HostObservation, RunningProcess,
    project_three_area_status,
)


class StatusProjectionTests(unittest.TestCase):
    def test_projects_three_areas_without_conflating_pause_cancel_or_takeover(self):
        binding = PauseBinding(
            task_id="task", revision=3, run_id="run", approval_hash="a" * 64,
            approved_scope_hash="b" * 64, approved_paths=("src/a.py",),
            manager_session_id="manager-session", manager_generation=2,
            worker_session_id="worker-session", worker_generation=4,
        )
        pause = PauseStatus(
            binding=binding, paused=True, cancelled=False, pause_id="pause-1",
            requested_at="2026-09-28T12:00:00Z", manager_ack_status="abort_requested",
            worker_ack_status="paused", abort_status="stop_observed",
            stop_observed_at="2026-09-28T12:00:01Z", unknown_tool_call_ids=("tool-7",),
            last_checked_at="2026-09-28T12:00:02Z", persistence_error=None,
            manager_unknown_tool_call_ids=("tool-7",),
        )
        projected = project_three_area_status(
            pause=pause,
            manager=AreaObservation("review request", "2026-09-28T11:59:00Z",
                                    "2026-09-28T12:00:02Z", "manager-session", 2,
                                    idle=True, pending=False, abort_status="stop_observed"),
            worker=AreaObservation("manual answer", "2026-09-28T11:58:00Z",
                                   "2026-09-28T12:00:02Z", "worker-session", 4,
                                   idle=True, pending=False, unknown_tool_results=("tool-8",)),
            host=HostObservation(
                "task", 3, "run", "collected output", "2026-09-28T12:00:01Z",
                "2026-09-28T12:00:02Z",
                (ConfirmedFileChange("out.txt", "modified", "2026-09-28T12:00:01Z", "c" * 64),),
                (RunningProcess(100, "python test.py", "2026-09-28T12:00:02Z"),),
                phase="running", exit_confirmed=False, exit_status=None,
                cancelled=False, terminal_owner="user",
            ),
        ).to_dict()
        self.assertEqual(set(projected), {"manager", "worker", "host"})
        self.assertTrue(projected["manager"]["paused"])
        self.assertEqual(projected["manager"]["abort_status"], "stop_observed")
        self.assertEqual(projected["manager"]["turn_stop_observed_at"], "2026-09-28T12:00:01Z")
        self.assertEqual(projected["worker"]["unknown_tool_results"], ["tool-8"])
        self.assertEqual(projected["manager"]["unknown_tool_results"], ["tool-7"])
        self.assertEqual(projected["host"]["confirmed_file_changes"][0]["path"], "out.txt")
        self.assertEqual(projected["host"]["running_processes"][0]["pid"], 100)
        self.assertEqual(projected["host"]["terminal_owner"], "user")
        self.assertFalse(projected["host"]["cancelled"])
        immutable = project_three_area_status(
            pause=pause, manager=AreaObservation(None, None, None),
            worker=AreaObservation(None, None, None),
            host=HostObservation("task", 3, "run", None, None, None, (), ()),
        )
        with self.assertRaises(TypeError):
            immutable.manager["paused"] = False

    def test_absent_observation_is_not_reported_as_empty_or_confirmed(self):
        pause = PauseStatus(None, True, False, "pause-2", "2026-09-28T12:00:00Z",
                            "unknown", "unknown", "unknown", None, (), None, None)
        projected = project_three_area_status(
            pause=pause,
            manager=AreaObservation(None, None, None),
            worker=AreaObservation(None, None, None),
            host=HostObservation(None, None, None, None, None, None, None, None),
        ).to_dict()
        self.assertIsNone(projected["host"]["confirmed_file_changes"])
        self.assertIsNone(projected["host"]["running_processes"])
        self.assertIsNone(projected["host"]["exit_confirmed"])
        self.assertFalse(projected["host"]["cancelled"])


if __name__ == "__main__":
    unittest.main()
