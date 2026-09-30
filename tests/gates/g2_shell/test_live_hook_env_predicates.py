"""Discriminating manual-residue controls on actual Bash and dash."""
from contextlib import contextmanager
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.gates.g2_shell import live_hook_env_probe as probe


@unittest.skipUnless(sys.platform == "linux" and shutil.which("bash") and shutil.which("dash"),
                     "Linux/Bash/dash required")
class HookEnvPredicatesTest(unittest.TestCase):
    def subreaper_state(self):
        libc = probe.ctypes.CDLL(None, use_errno=True)
        value = probe.ctypes.c_int()
        self.assertEqual(libc.prctl(37, probe.ctypes.byref(value), 0, 0, 0), 0)
        return value.value

    def setUp(self) -> None:
        self.original_subreaper = self.subreaper_state()

    def tearDown(self) -> None:
        self.assertEqual(self.subreaper_state(), self.original_subreaper)

    def assert_cleanup_failure_restores(self, owner, step):
        libc = probe.ctypes.CDLL(None, use_errno=True)
        previous = self.subreaper_state()
        prctl = Mock(wraps=libc.prctl)
        error = RuntimeError(f"injected cleanup {step} failure")
        fields = Mock(return_value=(0, 0, 42, "S"))
        kill = Mock()
        waitpid = Mock(return_value=(123456789, 0))
        {"proc_fields": fields, "kill": kill, "waitpid": waitpid}[step].side_effect = error
        try:
            with patch.object(probe.ctypes, "CDLL", return_value=SimpleNamespace(prctl=prctl)), \
                 patch.object(probe.combined, "proc_fields", fields), \
                 patch.object(probe.os, "kill", kill), \
                 patch.object(probe.os, "waitpid", waitpid):
                with self.assertRaises(RuntimeError) as caught:
                    with owner() as identities:
                        self.assertEqual(self.subreaper_state(), 1)
                        identities.append((123456789, 42))
                self.assertIs(caught.exception, error)
                self.assertEqual([entry.args[1] for entry in prctl.call_args_list
                                  if entry.args[0] == 36], [1, previous],
                                 "restoration call missing after cleanup failure")
            self.assertEqual(self.subreaper_state(), previous)
            fields.assert_called_once_with(123456789)
            if step == "proc_fields":
                kill.assert_not_called()
                waitpid.assert_not_called()
            else:
                kill.assert_called_once_with(123456789, probe.signal.SIGKILL)
                if step == "kill":
                    waitpid.assert_not_called()
                else:
                    waitpid.assert_called_once_with(123456789, 0)
        finally:
            # Also contain the deliberately broken restoration mutation.
            self.assertEqual(libc.prctl(36, previous, 0, 0, 0), 0)

    def test_cleanup_failures_restore_subreaper_and_propagate_original_error(self) -> None:
        for step in ("proc_fields", "kill", "waitpid"):
            with self.subTest(step=step):
                self.assert_cleanup_failure_restores(probe.own_orphans, step)

    def test_restore_omitting_cleanup_mutation_is_detected(self) -> None:
        @contextmanager
        def restore_after_cleanup_only():
            libc = probe.ctypes.CDLL(None, use_errno=True)
            previous = probe.ctypes.c_int()
            if libc.prctl(37, probe.ctypes.byref(previous), 0, 0, 0) != 0:
                raise OSError("get subreaper")
            if libc.prctl(36, 1, 0, 0, 0) != 0:
                raise OSError("set subreaper")
            identities = []
            try:
                yield identities
            finally:
                # Equivalent to the pre-repair structure: an unhandled cleanup
                # exception skips restoration placed after the cleanup loop.
                for pid, started in identities:
                    if probe.combined.proc_fields(pid)[2] == started:
                        probe.os.kill(pid, probe.signal.SIGKILL)
                    probe.os.waitpid(pid, 0)
                libc.prctl(36, previous.value, 0, 0, 0)

        for step in ("proc_fields", "kill", "waitpid"):
            with self.subTest(step=step):
                with self.assertRaisesRegex(AssertionError, "restoration call missing"):
                    self.assert_cleanup_failure_restores(restore_after_cleanup_only, step)

    def test_owned_manual_residue_holds_then_same_parent_recheck_proceeds(self) -> None:
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for kind in ("background", "escaped"):
                with self.subTest(shell=shell, kind=kind):
                    result = probe.case(shell, kind)
                    self.assertTrue(result["same_parent_recheck_proceeded"])
                    self.assertEqual(result["backend_auto_kills"], 0)
                    self.assertFalse(result["user_confirmation_completed"])
                    self.assertFalse(result["prior_scope_completed"])
                    if kind == "escaped":
                        self.assertEqual(result["prior_scope"], "unknown")

    def test_always_allow_candidate_fails_the_live_residue_hold(self) -> None:
        # Existing combined source has no manual-residue RUN admission check.
        for shell in (shutil.which("bash"), shutil.which("dash")):
            for kind in ("background", "escaped"):
                with self.subTest(shell=shell, kind=kind):
                    with patch.object(probe, "controller_source", probe.managed_controller_source):
                        with self.assertRaisesRegex(AssertionError, "automatically accepted"):
                            probe.case(shell, kind)

    def test_always_deny_candidate_fails_the_supported_post_cleanup_case(self) -> None:
        original = probe.controller_source

        def deny(shell):
            return original(shell).replace("                    __b_emit START",
                                           "                    __b_emit MANUAL_ACTIVE\n"
                                           "                    continue\n"
                                           "                    __b_emit START")

        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                with patch.object(probe, "controller_source", side_effect=deny):
                    with self.assertRaisesRegex(TimeoutError, "missing event START"):
                        probe.case(shell, "background")

    def test_user_confirmation_cannot_clear_the_live_unknown_scope(self) -> None:
        original = probe.controller_source

        def wrongly_clear(shell):
            return original(shell).replace(
                "                CONFIRM_MANUAL)\n                    __b_emit MANUAL_UNKNOWN",
                "                CONFIRM_MANUAL)\n                    WB_MANUAL_OBSERVATION=clear\n"
                "                    __b_emit MANUAL_UNKNOWN",
            )

        for shell in (shutil.which("bash"), shutil.which("dash")):
            with self.subTest(shell=shell):
                with patch.object(probe, "controller_source", side_effect=wrongly_clear):
                    with self.assertRaises(AssertionError):
                        probe.case(shell, "escaped")


if __name__ == "__main__":
    unittest.main()
