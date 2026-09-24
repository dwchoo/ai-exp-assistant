"""Independent negative controls for the host-only subreaper feasibility probe.

These tests alter observations at the parent/probe boundary while the real
supervisor and detached descendant still run and are cleaned up by the probe.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from tests.gates.g2_shell import live_subreaper_lifetime_probe as probe


class SubreaperLifetimePredicatesTest(unittest.TestCase):
    def run_probe(self) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = probe.run()
        result = json.loads(output.getvalue().splitlines()[-1])
        self.assertIn("pids", result, result)
        for pid in result["pids"].values():
            self.assertFalse(os.path.exists(f"/proc/{pid}"), f"residual pid {pid}")
        return status, result

    def assert_rejected(self, check: str) -> None:
        status, result = self.run_probe()
        self.assertEqual(status, 1, result)
        self.assertEqual(result["result"], "failed")
        self.assertFalse(result["checks"][check], result)

    def test_real_observation_succeeds_and_cleans_up(self) -> None:
        status, result = self.run_probe()
        self.assertEqual(status, 0, result)
        self.assertEqual(result["result"], "observed")
        self.assertTrue(all(result["checks"].values()), result)

    def test_live_descendant_cannot_be_reported_as_empty(self) -> None:
        original = probe.events_until

        def erroneous_wait(*args):
            events = original(*args)
            for event in events:
                if event["event"] == "live_wait":
                    event["result"] = -1  # ECHILD-like result, while child is alive
            return events

        with mock.patch.object(probe, "events_until", side_effect=erroneous_wait):
            self.assert_rejected("no_false_completion")

    def test_adoption_must_point_to_this_supervisor(self) -> None:
        original = probe.proc_fields

        def wrong_parent(pid):
            _, session, started, state = original(pid)
            return 1, session, started, state

        with mock.patch.object(probe, "proc_fields", side_effect=wrong_parent):
            self.assert_rejected("adopted_live_child")

    def test_pid_start_time_must_match_live_process(self) -> None:
        original = probe.proc_fields
        calls = 0

        def stale_identity(pid):
            nonlocal calls
            fields = original(pid)
            calls += 1
            if calls == 1:
                return fields[0], fields[1], fields[2] + 1, fields[3]
            return fields

        with mock.patch.object(probe, "proc_fields", side_effect=stale_identity):
            self.assert_rejected("same_child_identity")

    def test_reaped_descendant_must_have_expected_pid_and_exit(self) -> None:
        original = probe.events_until
        for field, replacement in (("pid", -1), ("exit", 99)):
            with self.subTest(field=field):
                def wrong_reap(*args):
                    events = original(*args)
                    for event in events:
                        if event["event"] == "descendant_reaped":
                            event[field] = replacement
                    return events

                with mock.patch.object(probe, "events_until", side_effect=wrong_reap):
                    self.assert_rejected("descendant_exit")

    def test_empty_wait_requires_echild_after_reap(self) -> None:
        original = probe.events_until

        def still_pending(*args):
            events = original(*args)
            for event in events:
                if event["event"] == "empty_wait":
                    event["errno"] = "WNOHANG_0"
            return events

        with mock.patch.object(probe, "events_until", side_effect=still_pending):
            self.assert_rejected("empty_after_reap")

    def test_reported_residual_process_prevents_completion(self) -> None:
        original = Path.exists

        def residual_reported(path):
            if path.parent == Path("/proc") and path.name.isdecimal():
                return True
            return original(path)

        with mock.patch.object(Path, "exists", residual_reported):
            self.assert_rejected("no_residual_processes")


if __name__ == "__main__":
    unittest.main()
