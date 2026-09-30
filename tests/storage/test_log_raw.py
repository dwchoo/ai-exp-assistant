"""CW-14 raw-log quota and storage-failure fixtures."""

from __future__ import annotations

import errno
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workbench.storage.log_raw import (
    MetadataAdmissionGate,
    RawLogStore,
    StoreIntegrityError,
)


def _append_from_process(root: str, run_id: str, payload: bytes) -> None:
    with RawLogStore(root, per_run_limit=100, project_limit=31) as store:
        store.append(run_id, payload)


class RawLogStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cw14-raw-log-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "raw"

    def test_default_caps_are_explicit_byte_limits(self):
        from workbench.storage.log_raw import DEFAULT_PER_RUN_LIMIT, DEFAULT_PROJECT_LIMIT

        self.assertEqual(DEFAULT_PER_RUN_LIMIT, 64 * 1024 * 1024)
        self.assertEqual(DEFAULT_PROJECT_LIMIT, 512 * 1024 * 1024)

    def test_run_limit_stops_at_exact_byte_and_keeps_observing(self):
        with RawLogStore(self.root, per_run_limit=5, project_limit=20) as store:
            first = store.append("run-a", b"abc")
            exact = store.append("run-a", b"de")
            over = store.append("run-a", b"fgh")

            self.assertEqual(first.stored_bytes, 3)
            self.assertEqual(exact.stored_bytes, 5)
            self.assertFalse(exact.truncated)
            self.assertEqual(over.stored_bytes, 5)
            self.assertEqual(over.observed_bytes, 8)
            self.assertEqual(over.dropped_bytes, 3)
            self.assertEqual(over.missing_bytes, 3)
            self.assertTrue(over.truncated)
            self.assertEqual(over.cap_source, "run")
            self.assertTrue(over.experiment_continues)
            self.assertTrue(over.observation_continues)
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abcde")

    def test_project_limit_is_shared_across_runs_and_survives_reopen(self):
        with RawLogStore(self.root, per_run_limit=10, project_limit=7) as store:
            first = store.append("run-a", b"abcde")
            second = store.append("run-b", b"wxyz")
            self.assertEqual(first.cap_source, None)
            self.assertEqual(second.stored_bytes, 2)
            self.assertEqual(second.observed_bytes, 4)
            self.assertEqual(second.dropped_bytes, 2)
            self.assertEqual(second.cap_source, "project")

        with RawLogStore(self.root, per_run_limit=10, project_limit=7) as reopened:
            third = reopened.append("run-c", b"q")
            self.assertEqual(third.stored_bytes, 0)
            self.assertEqual(third.dropped_bytes, 1)
            self.assertEqual(third.cap_source, "project")
            self.assertEqual(sum(path.stat().st_size for path in self.root.glob("*.log")), 7)

    def test_first_cap_is_reported_and_tied_caps_are_both_reported(self):
        with RawLogStore(self.root, per_run_limit=10, project_limit=9) as store:
            first = store.append("run-a", b"1234")
            self.assertIsNone(first.cap_source)
            second = store.append("run-b", b"56789")
            self.assertEqual(second.stored_bytes, 5)
            self.assertEqual(second.dropped_bytes, 0)
            self.assertEqual(second.cap_source, "project")

        other = Path(self.temp.name) / "tied"
        with RawLogStore(other, per_run_limit=4, project_limit=4) as store:
            result = store.append("run-a", b"12345")
            self.assertEqual(result.cap_source, "run+project")

    def test_run_cap_first_remains_its_source_after_project_cap(self):
        with RawLogStore(self.root, per_run_limit=4, project_limit=7) as store:
            run_exact = store.append("run-a", b"abcd")
            project_exact = store.append("run-b", b"xyz")
            run_over = store.append("run-a", b"lost")
            project_over = store.append("run-b", b"lost")
            self.assertEqual(run_exact.cap_source, "run")
            self.assertEqual(project_exact.cap_source, "project")
            self.assertEqual(run_over.cap_source, "run")
            self.assertEqual(project_over.cap_source, "project")
            self.assertEqual((run_over.stored_bytes, run_over.observed_bytes,
                              run_over.dropped_bytes, run_over.missing_bytes), (4, 8, 4, 4))
            self.assertEqual((project_over.stored_bytes, project_over.dropped_bytes), (3, 4))
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abcd")
            self.assertEqual((self.root / "run-b.log").read_bytes(), b"xyz")
        with RawLogStore(self.root, per_run_limit=4, project_limit=7) as reopened:
            self.assertEqual(reopened.status("run-a").cap_source, "run")
            self.assertEqual(reopened.status("run-b").cap_source, "project")

    def test_observation_times_and_reopen_status_are_bounded_and_immutable(self):
        with RawLogStore(self.root, per_run_limit=2, project_limit=8) as store:
            result = store.append("run-a", b"abc")
            snapshot = store.status("run-a")
            self.assertEqual(snapshot, result)
            self.assertIsNotNone(snapshot.first_observed_at)
            self.assertGreaterEqual(snapshot.last_observed_at, snapshot.first_observed_at)
            self.assertIsNotNone(snapshot.last_persisted_at)
            with self.assertRaises((AttributeError, TypeError)):
                snapshot.stored_bytes = 0

    def test_partial_write_is_accounted_and_does_not_escape_append(self):
        real_write = os.write
        calls = 0

        def partial_then_fail(fd, data):
            nonlocal calls
            if calls == 0:
                calls += 1
                return real_write(fd, data[:2])
            if calls == 1:
                calls += 1
                raise OSError(errno.ENOSPC, "fixture full")
            return real_write(fd, data)

        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            with patch("workbench.storage.log_raw.store.os.write", side_effect=partial_then_fail):
                result = store.append("run-a", b"abcde")

            self.assertEqual(result.stored_bytes, 2)
            self.assertEqual(result.observed_bytes, 5)
            self.assertEqual(result.dropped_bytes, 3)
            self.assertEqual(result.missing_bytes, 3)
            self.assertIn("OSError", result.storage_error)
            self.assertTrue(result.experiment_continues)
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"ab")

    def test_short_writes_that_make_progress_preserve_the_exact_prefix(self):
        real_write = os.write
        calls = 0

        def short_write(fd, data):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return real_write(fd, data[:1])
            return real_write(fd, data)

        with RawLogStore(self.root, per_run_limit=4, project_limit=9) as store:
            with patch("workbench.storage.log_raw.store.os.write", side_effect=short_write):
                result = store.append("run-a", b"abcde")
            self.assertEqual(result.stored_bytes, 4)
            self.assertEqual(result.dropped_bytes, 1)
            self.assertEqual(result.cap_source, "run")
            self.assertIsNone(result.storage_error)
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abcd")

    def test_raw_log_fsync_failure_is_reported_but_metadata_gate_stays_independent(self):
        real_fsync = os.fsync
        calls = 0

        def fail_first_fsync(fd):
            nonlocal calls
            if calls == 0:
                calls += 1
                raise OSError(errno.EIO, "fixture fsync failure")
            return real_fsync(fd)

        gate = MetadataAdmissionGate()
        gate.record_metadata_failure("sqlite write failed")
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            with patch("workbench.storage.log_raw.store.os.fsync", side_effect=fail_first_fsync):
                result = store.append("run-a", b"bytes")
            self.assertFalse(gate.automatic_runs_allowed)
            self.assertTrue(result.experiment_continues)
            self.assertIn("OSError", result.storage_error)
            self.assertEqual(result.missing_bytes, len(b"bytes"))
            self.assertIsNotNone(result.last_observed_at)
            self.assertIsNone(result.last_persisted_at)
            gate.record_durable_metadata_success()
            self.assertTrue(gate.automatic_runs_allowed)

    def test_status_directory_fsync_failure_reports_uncertain_persistence(self):
        real_fsync = os.fsync
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            def fail_status_directory(fd):
                if fd == store._status_fd:
                    raise OSError(errno.EIO, "fixture status directory fsync failure")
                return real_fsync(fd)

            with patch("workbench.storage.log_raw.store.os.fsync",
                       side_effect=fail_status_directory):
                result = store.append("run-a", b"abc")
            self.assertEqual((result.stored_bytes, result.observed_bytes), (3, 3))
            self.assertIn("OSError", result.storage_error)
            self.assertTrue(result.experiment_continues)
            self.assertTrue(result.observation_continues)
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abc")
            self.assertIsNotNone(store.status("run-a").storage_error)

    def test_open_and_quota_corruption_fail_closed_without_raising(self):
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            real_open = os.open

            def fail_log_open(path, flags, *args, **kwargs):
                if path == "run-a.log":
                    raise OSError(errno.EIO, "fixture open failure")
                return real_open(path, flags, *args, **kwargs)

            with patch("workbench.storage.log_raw.store.os.open", side_effect=fail_log_open):
                failed = store.append("run-a", b"abc")
            self.assertEqual(failed.stored_bytes, 0)
            self.assertEqual(failed.dropped_bytes, 3)
            self.assertIn("OSError", failed.storage_error)

        corrupt_root = Path(self.temp.name) / "corrupt"
        with RawLogStore(corrupt_root, per_run_limit=4, project_limit=8):
            (corrupt_root / "run-a.log").write_bytes(b"too long")
        with self.assertRaises(StoreIntegrityError):
            RawLogStore(corrupt_root, per_run_limit=4, project_limit=8)
        self.assertFalse((corrupt_root / "run-b.log").exists())

        live_root = Path(self.temp.name) / "quota-scan-error"
        with RawLogStore(live_root, per_run_limit=4, project_limit=8) as store:
            store.append("run-a", b"ab")
            (live_root / "run-a.log").write_bytes(b"too long")
            failed = store.append("run-b", b"x")
            self.assertIsNotNone(failed.storage_error)
            self.assertEqual(failed.dropped_bytes, 1)
            self.assertFalse((live_root / "run-b.log").exists())

    def test_scan_failure_is_observed_without_creating_or_removing_data(self):
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            with patch.object(store, "_scan_project",
                              side_effect=OSError(errno.EIO, "fixture scan failure")):
                failed = store.append("run-a", b"abc")
            self.assertEqual((failed.stored_bytes, failed.dropped_bytes), (0, 3))
            self.assertIsNotNone(failed.last_observed_at)
            self.assertIn("OSError", failed.storage_error)
            self.assertTrue(failed.experiment_continues)
            self.assertFalse((self.root / "run-a.log").exists())

    def test_consecutive_scan_failures_merge_from_newest_volatile_snapshot(self):
        observed = (
            "2026-09-29T01:00:00Z",
            "2026-09-29T01:00:01Z",
            "2026-09-29T01:00:02Z",
            "2026-09-29T01:00:03Z",
        )
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            original = store.append("run-a", b"abc", observed_at=observed[0])
            with patch.object(
                store,
                "_scan_project",
                side_effect=[
                    OSError(errno.EIO, "first fixture scan failure"),
                    OSError(errno.EIO, "second fixture scan failure"),
                ],
            ):
                first = store.append("run-a", b"de", observed_at=observed[1])
                second = store.append("run-a", b"fghi", observed_at=observed[2])

            self.assertEqual(
                (first.stored_bytes, first.observed_bytes, first.dropped_bytes,
                 first.missing_bytes),
                (3, 5, 2, 2),
            )
            self.assertEqual(
                (second.stored_bytes, second.observed_bytes, second.dropped_bytes,
                 second.missing_bytes),
                (3, 9, 6, 6),
            )
            self.assertEqual(second.first_observed_at, original.first_observed_at)
            self.assertEqual(second.last_observed_at, "2026-09-29T01:00:02.000000Z")
            self.assertEqual(second.last_persisted_at, original.last_persisted_at)

            with patch.object(
                store,
                "_scan_project",
                side_effect=OSError(errno.EIO, "status fixture scan failure"),
            ):
                current = store.status("run-a")

            self.assertEqual(
                (current.stored_bytes, current.observed_bytes, current.dropped_bytes,
                 current.missing_bytes),
                (3, 9, 6, 6),
            )
            self.assertEqual(current.first_observed_at, second.first_observed_at)
            self.assertEqual(current.last_observed_at, second.last_observed_at)
            self.assertEqual(current.last_persisted_at, second.last_persisted_at)
            self.assertIsNotNone(current.storage_error)
            with self.assertRaises((AttributeError, TypeError)):
                current.observed_bytes = 0

    def test_malformed_log_entry_returns_failure_snapshots_and_preserves_sentinels(self):
        malformed = self.root / ".log"
        sentinel = b"preserve malformed entry"
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            original = store.append(
                "run-a", b"abc", observed_at="2026-09-29T02:00:00Z"
            )
            malformed.write_bytes(sentinel)

            status_failure = store.status("run-a")
            self.assertEqual(status_failure.stored_bytes, original.stored_bytes)
            self.assertEqual(status_failure.observed_bytes, original.observed_bytes)
            self.assertEqual(status_failure.dropped_bytes, original.dropped_bytes)
            self.assertEqual(status_failure.missing_bytes, original.missing_bytes)
            self.assertIsNotNone(status_failure.storage_error)
            with self.assertRaises((AttributeError, TypeError)):
                status_failure.stored_bytes = 0

            append_failure = store.append(
                "run-a", b"de", observed_at="2026-09-29T02:00:01Z"
            )
            self.assertEqual(
                (append_failure.stored_bytes, append_failure.observed_bytes,
                 append_failure.dropped_bytes, append_failure.missing_bytes),
                (3, 5, 2, 2),
            )
            self.assertEqual(append_failure.first_observed_at, original.first_observed_at)
            self.assertEqual(append_failure.last_observed_at, "2026-09-29T02:00:01.000000Z")
            self.assertEqual(append_failure.last_persisted_at, original.last_persisted_at)
            self.assertIsNotNone(append_failure.storage_error)
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abc")
            self.assertEqual(malformed.read_bytes(), sentinel)

    def test_same_store_recovers_after_malformed_entry_is_removed(self):
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            initial = store.append("run-a", b"abc")
            malformed = self.root / ".log"
            malformed.write_bytes(b"temporary malformed entry")

            status_failure = store.status("run-a")
            self.assertIsNotNone(status_failure.storage_error)
            failed = store.append("run-a", b"de")
            self.assertEqual(
                (failed.stored_bytes, failed.observed_bytes, failed.dropped_bytes,
                 failed.missing_bytes),
                (3, 5, 2, 2),
            )
            self.assertEqual(failed.first_observed_at, initial.first_observed_at)
            malformed.unlink()
            recovered = store.append(
                "run-a", b"f", observed_at="2026-09-29T02:00:02Z"
            )
            self.assertEqual(
                (recovered.stored_bytes, recovered.observed_bytes,
                 recovered.dropped_bytes, recovered.missing_bytes),
                (4, 6, 2, 2),
            )
            self.assertEqual(recovered.last_observed_at, "2026-09-29T02:00:02.000000Z")
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abcf")
            self.assertEqual(store.status("run-a"), recovered)

    def test_same_run_store_branch_merge_preserves_both_observations(self):
        with RawLogStore(self.root, per_run_limit=3, project_limit=20) as store_a:
            initial = store_a.append("run-a", b"abc")
            with patch.object(
                store_a,
                "_scan_project",
                side_effect=OSError(errno.EIO, "fixture scan failure"),
            ):
                branch = store_a.append("run-a", b"de")
            self.assertEqual(
                (branch.stored_bytes, branch.observed_bytes, branch.dropped_bytes,
                 branch.missing_bytes),
                (3, 5, 2, 2),
            )

            with RawLogStore(self.root, per_run_limit=3, project_limit=20) as store_b:
                concurrent = store_b.append("run-a", b"f")
                self.assertEqual(
                    (concurrent.stored_bytes, concurrent.observed_bytes,
                     concurrent.dropped_bytes, concurrent.missing_bytes),
                    (3, 4, 1, 1),
                )

            final = store_a.append("run-a", b"g")
            self.assertEqual(
                (final.stored_bytes, final.observed_bytes, final.dropped_bytes,
                 final.missing_bytes),
                (3, 7, 4, 4),
            )
            self.assertEqual(final.first_observed_at, initial.first_observed_at)
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abc")
            self.assertEqual(store_a.status("run-a"), final)

    def test_repeated_local_failures_compose_with_concurrent_durable_drop(self):
        with RawLogStore(self.root, per_run_limit=3, project_limit=20) as store_a:
            initial = store_a.append("run-a", b"abc")
            with patch.object(store_a, "_scan_project",
                              side_effect=OSError(errno.EIO, "first scan failure")):
                first = store_a.append("run-a", b"de")
            self.assertEqual(
                (first.stored_bytes, first.observed_bytes, first.dropped_bytes,
                 first.missing_bytes), (3, 5, 2, 2))

            with RawLogStore(self.root, per_run_limit=3, project_limit=20) as store_b:
                concurrent = store_b.append("run-a", b"0123456789")
                self.assertEqual(
                    (concurrent.stored_bytes, concurrent.observed_bytes,
                     concurrent.dropped_bytes, concurrent.missing_bytes),
                    (3, 13, 10, 10))
            before_second_failure = store_a.status("run-a")
            self.assertEqual(
                (before_second_failure.stored_bytes,
                 before_second_failure.observed_bytes,
                 before_second_failure.dropped_bytes,
                 before_second_failure.missing_bytes), (3, 15, 12, 12))
            self.assertIsNotNone(before_second_failure.storage_error)

            with patch.object(store_a, "_scan_project",
                              side_effect=OSError(errno.EIO, "second scan failure")):
                second = store_a.append("run-a", b"g")
            self.assertEqual(
                (second.stored_bytes, second.observed_bytes, second.dropped_bytes,
                 second.missing_bytes), (3, 16, 13, 13))
            self.assertEqual(second.first_observed_at, initial.first_observed_at)
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abc")
            visible = store_a.status("run-a")
            self.assertEqual(
                (visible.stored_bytes, visible.observed_bytes,
                 visible.dropped_bytes, visible.missing_bytes),
                (3, 16, 13, 13))

            recovered = store_a.append("run-a", b"")
            self.assertEqual(
                (recovered.stored_bytes, recovered.observed_bytes,
                 recovered.dropped_bytes, recovered.missing_bytes),
                (3, 16, 13, 13))
            self.assertEqual(store_a.status("run-a"), recovered)
            again = store_a.append("run-a", b"")
            self.assertEqual(
                (again.stored_bytes, again.observed_bytes,
                 again.dropped_bytes, again.missing_bytes),
                (3, 16, 13, 13))

    def test_two_recovered_failure_cycles_apply_each_local_delta_once(self):
        with RawLogStore(self.root, per_run_limit=3, project_limit=20) as store_a:
            store_a.append("run-a", b"abc")
            with patch.object(store_a, "_scan_project",
                              side_effect=OSError(errno.EIO, "first scan failure")):
                first = store_a.append("run-a", b"de")
            self.assertEqual((first.stored_bytes, first.observed_bytes,
                              first.dropped_bytes, first.missing_bytes), (3, 5, 2, 2))
            recovered_first = store_a.append("run-a", b"")
            self.assertEqual((recovered_first.stored_bytes, recovered_first.observed_bytes,
                              recovered_first.dropped_bytes, recovered_first.missing_bytes),
                             (3, 5, 2, 2))

            with RawLogStore(self.root, per_run_limit=3, project_limit=20) as store_b:
                concurrent = store_b.append("run-a", b"f")
            self.assertEqual((concurrent.stored_bytes, concurrent.observed_bytes,
                              concurrent.dropped_bytes, concurrent.missing_bytes),
                             (3, 6, 3, 3))

            with patch.object(store_a, "_scan_project",
                              side_effect=OSError(errno.EIO, "second scan failure")):
                second = store_a.append("run-a", b"gh")
            self.assertEqual((second.stored_bytes, second.observed_bytes,
                              second.dropped_bytes, second.missing_bytes), (3, 8, 5, 5))
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abc")
            recovered_second = store_a.append("run-a", b"")
            repeated = store_a.append("run-a", b"")
            self.assertEqual((recovered_second.stored_bytes,
                              recovered_second.observed_bytes,
                              recovered_second.dropped_bytes,
                              recovered_second.missing_bytes), (3, 8, 5, 5))
            self.assertEqual((repeated.stored_bytes, repeated.observed_bytes,
                              repeated.dropped_bytes, repeated.missing_bytes),
                             (3, 8, 5, 5))
            self.assertGreaterEqual(repeated.last_observed_at,
                                    recovered_second.last_observed_at)
            self.assertEqual(store_a.status("run-a"), repeated)

    def test_visible_but_unconfirmed_failure_delta_is_not_applied_twice(self):
        with RawLogStore(self.root, per_run_limit=3, project_limit=20) as store_a:
            store_a.append("run-a", b"abc")
            with patch.object(store_a, "_scan_project",
                              side_effect=OSError(errno.EIO, "scan failure")):
                store_a.append("run-a", b"de")

            real_atomic = store_a._atomic_status
            calls = 0

            def fail_after_visible(status, raw_identity):
                nonlocal calls
                real_atomic(status, raw_identity)
                calls += 1
                if calls == 1:
                    raise OSError(errno.EIO, "post-rename confirmation failure")

            with patch.object(store_a, "_atomic_status", side_effect=fail_after_visible):
                failed = store_a.append("run-a", b"g")
            self.assertEqual(
                (failed.stored_bytes, failed.observed_bytes,
                 failed.dropped_bytes, failed.missing_bytes), (3, 6, 3, 3))

            with RawLogStore(self.root, per_run_limit=3, project_limit=20) as store_b:
                concurrent = store_b.append("run-a", b"h")
                self.assertEqual(
                    (concurrent.observed_bytes, concurrent.dropped_bytes), (6, 3))

            recovered = store_a.append("run-a", b"i")
            self.assertEqual(
                (recovered.stored_bytes, recovered.observed_bytes,
                 recovered.dropped_bytes, recovered.missing_bytes), (3, 8, 5, 5))
            self.assertEqual(store_a.status("run-a"), recovered)

    def test_pending_failure_recovery_keeps_zero_quota_cap_source(self):
        for per_run, project, expected in ((0, 20, "run"), (20, 0, "project")):
            with self.subTest(per_run=per_run, project=project):
                root = Path(self.temp.name) / f"zero-{per_run}-{project}"
                with RawLogStore(root, per_run_limit=per_run,
                                 project_limit=project) as store:
                    with patch.object(store, "_scan_project",
                                      side_effect=OSError(errno.EIO, "scan failure")):
                        failed = store.append("run-a", b"a")
                    self.assertEqual(
                        (failed.stored_bytes, failed.observed_bytes,
                         failed.dropped_bytes, failed.missing_bytes), (0, 1, 1, 1))
                    recovered = store.append("run-a", b"b")
                    self.assertEqual(
                        (recovered.stored_bytes, recovered.observed_bytes,
                         recovered.dropped_bytes, recovered.missing_bytes), (0, 2, 2, 2))
                    self.assertEqual(recovered.cap_source, expected)
                    self.assertEqual(store.status("run-a"), recovered)

    def test_malformed_status_entry_is_failure_snapshot_and_recovers(self):
        malformed = self.root / ".status" / ".json"
        sentinel = b"preserve malformed status entry"
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            initial = store.append("run-a", b"abc")
            malformed.write_bytes(sentinel)

            status_failure = store.status("run-a")
            self.assertIsNotNone(status_failure.storage_error)
            self.assertEqual(status_failure.stored_bytes, initial.stored_bytes)
            self.assertEqual(status_failure.observed_bytes, initial.observed_bytes)
            self.assertEqual(malformed.read_bytes(), sentinel)

            failed = store.append("run-a", b"de")
            self.assertEqual(
                (failed.stored_bytes, failed.observed_bytes, failed.dropped_bytes,
                 failed.missing_bytes),
                (3, 5, 2, 2),
            )
            self.assertIsNotNone(failed.storage_error)
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abc")
            self.assertEqual(malformed.read_bytes(), sentinel)

            malformed.unlink()
            recovered = store.append("run-a", b"f")
            self.assertEqual(
                (recovered.stored_bytes, recovered.observed_bytes,
                 recovered.dropped_bytes, recovered.missing_bytes),
                (4, 6, 2, 2),
            )
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"abcf")
            self.assertEqual(store.status("run-a"), recovered)

    def test_lock_open_failure_returns_missing_status_instead_of_raising(self):
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            real_open = os.open

            def fail_lock_open(path, flags, *args, **kwargs):
                if path == ".quota.lock":
                    raise OSError(errno.EIO, "fixture lock failure")
                return real_open(path, flags, *args, **kwargs)

            with patch("workbench.storage.log_raw.store.os.open", side_effect=fail_lock_open):
                result = store.append("run-a", b"observed")
            self.assertEqual(result.stored_bytes, 0)
            self.assertEqual(result.dropped_bytes, len(b"observed"))
            self.assertIsNotNone(result.last_observed_at)
            self.assertIn("OSError", result.storage_error)

    def test_symlink_special_and_path_traversal_are_rejected(self):
        external = Path(self.temp.name) / "outside"
        external.write_bytes(b"sentinel")
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            (self.root / "run-link.log").symlink_to(external)
            result = store.append("run-other", b"payload")
            self.assertIsNotNone(result.storage_error)
            self.assertEqual(external.read_bytes(), b"sentinel")
            with self.assertRaises(ValueError):
                store.append("../outside", b"payload")
            with self.assertRaises(ValueError):
                store.append("a/b", b"payload")

    def test_replaced_regular_log_path_is_not_followed(self):
        replacement = Path(self.temp.name) / "replacement.log"
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            store.append("run-a", b"original")
            replacement.write_bytes(b"original")
            os.replace(replacement, self.root / "run-a.log")
            result = store.append("run-a", b"new")

            self.assertIsNotNone(result.storage_error)
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"original")
            self.assertTrue(result.experiment_continues)

    def test_raw_path_replacement_between_scan_and_append_is_not_written(self):
        replacement = Path(self.temp.name) / "replacement.log"
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            store.append("run-a", b"before")
            replacement.write_bytes(b"before")
            real_scan = store._scan_project

            def replace_after_scan():
                scanned = real_scan()
                os.replace(replacement, self.root / "run-a.log")
                return scanned

            with patch.object(store, "_scan_project", side_effect=replace_after_scan):
                failed = store.append("run-a", b"unsafe")
            self.assertIsNotNone(failed.storage_error)
            self.assertEqual(failed.dropped_bytes, len(b"unsafe"))
            self.assertEqual((self.root / "run-a.log").read_bytes(), b"before")

    def test_replaced_root_or_status_directory_does_not_escape(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir(mode=0o700)
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"preserve")
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            parked = Path(self.temp.name) / "parked-raw"
            os.rename(self.root, parked)
            self.root.symlink_to(outside, target_is_directory=True)
            failed = store.append("run-a", b"unsafe")
            self.assertIsNotNone(failed.storage_error)
            self.assertFalse((outside / "run-a.log").exists())
            self.assertEqual(sentinel.read_bytes(), b"preserve")

        status_root = Path(self.temp.name) / "status-swapped"
        with RawLogStore(status_root, per_run_limit=20, project_limit=20) as store:
            parked_status = Path(self.temp.name) / "parked-status"
            os.rename(status_root / ".status", parked_status)
            (status_root / ".status").symlink_to(outside, target_is_directory=True)
            failed = store.append("run-a", b"unsafe")
            self.assertIsNotNone(failed.storage_error)
            self.assertFalse((outside / "run-a.json").exists())
            self.assertEqual(sentinel.read_bytes(), b"preserve")

    def test_special_file_and_replaced_lock_fail_without_blocking_or_writing(self):
        with RawLogStore(self.root, per_run_limit=20, project_limit=20) as store:
            os.mkfifo(self.root / "run-fifo.log")
            started = time.monotonic()
            special = store.append("run-a", b"payload")
            self.assertLess(time.monotonic() - started, 1)
            self.assertIsNotNone(special.storage_error)
            self.assertFalse((self.root / "run-a.log").exists())

        lock_root = Path(self.temp.name) / "lock-replaced"
        with RawLogStore(lock_root, per_run_limit=20, project_limit=20):
            pass
        replacement = Path(self.temp.name) / "new-lock"
        replacement.write_bytes(b"not the lock")
        os.replace(replacement, lock_root / ".quota.lock")
        with self.assertRaises(StoreIntegrityError):
            RawLogStore(lock_root, per_run_limit=20, project_limit=20)
        self.assertFalse((lock_root / "run-a.log").exists())

    def test_result_and_metadata_sentinels_outside_owned_raw_root_are_untouched(self):
        artifacts = Path(self.temp.name) / "artifacts"
        artifacts.mkdir()
        sentinels = {
            artifacts / "taskspec.json": b"task spec",
            artifacts / "summary.json": b"summary",
            artifacts / "result.bin": b"experiment result",
        }
        for path, data in sentinels.items():
            path.write_bytes(data)

        with RawLogStore(self.root, per_run_limit=2, project_limit=2) as store:
            store.append("run-a", b"logs")

        self.assertEqual({path: path.read_bytes() for path in sentinels}, sentinels)

    def test_nonempty_unmarked_root_is_rejected_without_mutation(self):
        root = Path(self.temp.name) / "not-owned"
        root.mkdir()
        root.chmod(0o700)
        sentinel = root / "user-data"
        sentinel.write_bytes(b"preserve")

        with self.assertRaises(StoreIntegrityError):
            RawLogStore(root, per_run_limit=20, project_limit=20)

        self.assertEqual(sentinel.read_bytes(), b"preserve")
        self.assertEqual({path.name for path in root.iterdir()}, {"user-data"})

    def test_concurrent_processes_never_exceed_project_limit(self):
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(target=_append_from_process,
                            args=(str(self.root), f"run-{index}", b"x" * 13))
            for index in range(6)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join()
                self.fail("concurrent writer exceeded fixture deadline")
            self.assertEqual(process.exitcode, 0)

        total = sum(path.stat().st_size for path in self.root.glob("*.log"))
        self.assertEqual(total, 31)

    def test_concurrent_threads_share_a_single_instance_safely(self):
        with RawLogStore(self.root, per_run_limit=100, project_limit=31) as store:
            threads = [threading.Thread(target=store.append,
                                        args=(f"thread-{index}", b"x" * 9))
                       for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())

        total = sum(path.stat().st_size for path in self.root.glob("*.log"))
        self.assertEqual(total, 31)


class MetadataAdmissionGateTests(unittest.TestCase):
    def test_only_explicit_later_durable_success_recovers_admission(self):
        gate = MetadataAdmissionGate()
        self.assertTrue(gate.automatic_runs_allowed)
        failed = gate.record_metadata_failure("durable metadata write failed")
        self.assertFalse(failed.automatic_runs_allowed)
        self.assertEqual(failed.failure, "durable metadata write failed")

        recovered = gate.record_durable_metadata_success()
        self.assertTrue(recovered.automatic_runs_allowed)
        self.assertIsNone(recovered.failure)
        self.assertEqual(recovered.generation, failed.generation + 1)

    def test_raw_storage_failure_does_not_close_metadata_admission(self):
        with tempfile.TemporaryDirectory(prefix="cw14-admission-") as folder:
            raw_root = Path(folder) / "raw"
            result = Path(folder) / "result.bin"
            result.write_bytes(b"experiment result")
            gate = MetadataAdmissionGate()
            with RawLogStore(raw_root, per_run_limit=20, project_limit=20) as store:
                real_open = os.open

                def fail_raw_open(path, flags, *args, **kwargs):
                    if path == "run-a.log":
                        raise OSError(errno.EIO, "fixture raw log failure")
                    return real_open(path, flags, *args, **kwargs)

                with patch("workbench.storage.log_raw.store.os.open",
                           side_effect=fail_raw_open):
                    failed = store.append("run-a", b"abc")
                self.assertIsNotNone(failed.storage_error)
                self.assertTrue(gate.automatic_runs_allowed)
                blocked = gate.record_metadata_failure("durable metadata write failed")
                self.assertFalse(blocked.automatic_runs_allowed)
                self.assertEqual(result.read_bytes(), b"experiment result")
                self.assertTrue(failed.experiment_continues)
                gate.record_durable_metadata_success()
                self.assertTrue(gate.automatic_runs_allowed)
                self.assertEqual(result.read_bytes(), b"experiment result")


if __name__ == "__main__":
    unittest.main()
