from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workbench.terminal.shell_g2.prototype import (
    InputBoundary,
    ShellChoice,
    ShellProcess,
    ShellUnavailable,
    UnsafeShellState,
    select_shell,
)


def available_shells() -> list[ShellChoice]:
    result: list[ShellChoice] = []
    bash = shutil.which("bash")
    dash = shutil.which("dash")
    if bash:
        result.append(ShellChoice("bash", os.path.realpath(bash)))
    if dash:
        result.append(ShellChoice("sh", os.path.realpath(dash)))
    return result


class ShellSelectionTests(unittest.TestCase):
    def test_bash_is_preferred_and_sh_is_fixed_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw03-path-") as path_dir:
            sh = shutil.which("sh")
            self.assertIsNotNone(sh)
            os.symlink(sh, Path(path_dir) / "sh")
            choice = select_shell(path=path_dir)
            self.assertEqual((choice.kind, choice.executable), ("sh", os.path.realpath(sh)))
            with ShellProcess(choice) as session:
                session.wait_ready()
                self.assertEqual(session.choice, choice)
                self.assertTrue(session.boundary.can_dispatch(
                    generation=session.generation,
                    owner_epoch=session.owner_epoch,
                ))

    def test_startup_requirement_when_bash_and_sh_are_absent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw03-empty-path-") as empty_path:
            with self.assertRaises(ShellUnavailable):
                select_shell(path=empty_path)


class InputBoundaryTests(unittest.TestCase):
    def test_owner_return_requires_fresh_state_check_and_rejects_old_epoch(self) -> None:
        boundary = InputBoundary(generation=1)
        boundary.observe_event("READY")
        old_epoch = boundary.owner_epoch
        boundary.owner_change("user")
        current_epoch = boundary.owner_change("manager")

        self.assertFalse(boundary.can_dispatch(generation=1, owner_epoch=old_epoch))
        self.assertFalse(boundary.can_dispatch(generation=1, owner_epoch=current_epoch))

    def test_stale_ready_cannot_consume_newly_submitted_user_line(self) -> None:
        boundary = InputBoundary(generation=1)
        boundary.observe_event("READY")
        boundary.owner_change("user")
        boundary.observe_user_bytes(b"sleep 1\n")
        epoch = boundary.owner_change("manager")
        self.assertFalse(boundary.can_dispatch(generation=1, owner_epoch=epoch))

        # A queued READY from before this user line carries no proof that it ran.
        boundary.observe_event("READY")
        self.assertFalse(boundary.can_dispatch(generation=1, owner_epoch=epoch))

    def test_ready_before_done_latches_command_uncertainty(self) -> None:
        boundary = InputBoundary(generation=1)
        boundary.observe_event("READY")
        boundary.observe_event("START:cmd-1")
        boundary.observe_event("READY")

        self.assertTrue(boundary.needs_review)
        self.assertFalse(boundary.can_dispatch(generation=1, owner_epoch=boundary.owner_epoch))


class RealShellGateTests(unittest.TestCase):
    def assert_live_job(self, pid: int) -> int:
        with open(f"/proc/{pid}/status", encoding="ascii") as status:
            fields = dict(line.split(":", 1) for line in status if ":" in line)
        self.assertNotIn(fields["State"].strip().split()[0], {"X", "Z"})
        return int(fields["PPid"].strip())

    def assert_dispatch_blocked_without_write(
        self, session: ShellProcess, command_id: str, epoch: int
    ) -> None:
        with patch.object(session, "_write_all") as write:
            with self.assertRaises(UnsafeShellState):
                session.dispatch(command_id, "__wb_emit SHOULD_NOT_RUN", expected_epoch=epoch)
            write.assert_not_called()
        self.assertFalse(any(event.startswith(f"START:{command_id}") for event in session.events))

    def test_unterminated_manual_quote_keeps_auto_dispatch_blocked(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"printf 'unfinished\n")
                epoch = session.return_to_manager()
                with self.assertRaises(UnsafeShellState):
                    session.dispatch("behind-quote", ":", expected_epoch=epoch)
                self.assertFalse(any(event.startswith("START:behind-quote") for event in session.events))

    def test_manual_background_job_before_owner_return_blocks_auto_dispatch(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"sleep 30 &\n")
                session.wait_ready()
                epoch = session.return_to_manager()

                self.assertFalse(session.boundary.can_dispatch(
                    generation=session.generation,
                    owner_epoch=epoch,
                ))
                with self.assertRaises(UnsafeShellState):
                    session.dispatch("behind-user-job", ":", expected_epoch=epoch)
                self.assertFalse(any(event.startswith("START:behind-user-job") for event in session.events))

    def test_lost_prompt_hook_keeps_auto_dispatch_blocked(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                command = b"unset PROMPT_COMMAND\n" if choice.kind == "bash" else b"PS1='plain$ '\n"
                session.send_user(command)
                with self.assertRaises(TimeoutError):
                    session.wait_ready(timeout=0.3)
                epoch = session.return_to_manager()
                self.assertFalse(session.boundary.can_dispatch(
                    generation=session.generation,
                    owner_epoch=epoch,
                ))

    def test_manual_cd_export_requires_direct_handoff_in_the_same_shell(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                original_pid = session.pid
                session.take_user_control()
                session.send_user(b"cd /tmp\n")
                session.wait_ready()
                session.send_user(b"export CW03_G2_ENV=kept\n")
                session.wait_ready()

                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "before-handoff", epoch)
                session.take_user_control()
                before_handoff = len(session.events)
                session.send_user(b"wb-handoff\n")
                session.wait_event("HANDOFF")
                self.assertTrue(any(
                    event.startswith("HANDOFF") for event in session.events[before_handoff:]
                ))
                session.wait_ready()
                epoch = session.return_to_manager()
                self.assertTrue(session.boundary.can_dispatch(
                    generation=session.generation,
                    owner_epoch=epoch,
                ))
                self.assertEqual(os.readlink(f"/proc/{original_pid}/cwd"), "/tmp")
                session.dispatch(
                    "same-shell",
                    "__wb_emit STATE:$PWD:$CW03_G2_ENV",
                    expected_epoch=epoch,
                )
                session.wait_command("same-shell")
                self.assertIn("STATE:/tmp:kept", session.events)
                self.assertEqual(session.pid, original_pid)
                self.assertEqual(os.readlink(f"/proc/{original_pid}/cwd"), "/tmp")

    def test_handoff_before_last_user_command_does_not_authorize_dispatch(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"wb-handoff\n")
                session.wait_ready()
                session.send_user(b"export CW03_AFTER_HANDOFF=changed\n")
                session.wait_ready()
                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "stale-handoff", epoch)

    def test_unsubmitted_tail_after_handoff_blocks_auto_dispatch(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"wb-handoff\n")
                session.wait_ready()
                session.send_user(b"echo USER_PENDING")
                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "behind-tail", epoch)

    def test_manual_background_job_remains_blocked_after_handoff(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"sleep 30 &\n")
                session.wait_ready()
                session.send_user(b"wb-handoff\n")
                session.wait_ready()
                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "behind-handoff-job", epoch)

    def test_prompt_hook_job_after_handoff_snapshot_blocks_auto_dispatch(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                if choice.kind == "bash":
                    hook = (
                        b"PROMPT_COMMAND='if [[ -n $__wb_handoff_pid ]]; then "
                        b"sleep 30 & __wb_emit HOOK_JOB:$!; fi; __wb_emit READY'\n"
                    )
                else:
                    hook = (
                        b"PS1='$( if [ -n \"$__wb_handoff_pid\" ]; then "
                        b"sleep 30 >/dev/null 2>&1 & __wb_emit HOOK_JOB:$!; "
                        b"fi; __wb_ready; printf \"$ \" )'\n"
                    )
                session.send_user(hook)
                session.wait_ready()

                before_handoff = len(session.events)
                session.send_user(b"wb-handoff\n")
                session.wait_ready()
                handoff_events = session.events[before_handoff:]
                snapshot_end = handoff_events.index("JOBS_END:HANDOFF")
                hook_job = next(
                    index for index, event in enumerate(handoff_events)
                    if event.startswith("HOOK_JOB:")
                )
                self.assertLess(snapshot_end, hook_job)
                self.assertLess(hook_job, handoff_events.index("READY"))
                hook_pid = int(handoff_events[hook_job].partition(":")[2])
                self.assertTrue(Path(f"/proc/{hook_pid}").exists())

                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "behind-hook-job", epoch)
                self.assertFalse(session.boundary.can_dispatch(
                    generation=session.generation,
                    owner_epoch=epoch,
                ))

    def test_redefined_prompt_function_job_after_handoff_blocks_auto_dispatch(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                if choice.kind == "bash":
                    redefine = (
                        b"__wb_emit() { if [[ $1 == READY && -n $__wb_handoff_pid ]]; then "
                        b"sleep 30 >/dev/null 2>&1 & printf 'HOOK_JOB:%s\\n' \"$!\" >&9; "
                        b"fi; printf '%s\\n' \"$*\" >&9; }\n"
                    )
                else:
                    redefine = (
                        b"__wb_ready() { if [ -n \"$__wb_handoff_pid\" ]; then "
                        b"sleep 30 >/dev/null 2>&1 & __wb_emit HOOK_JOB:$!; "
                        b"fi; __wb_emit READY; }\n"
                    )
                session.send_user(redefine)
                session.wait_ready()

                before_handoff = len(session.events)
                session.send_user(b"wb-handoff\n")
                session.wait_ready()
                handoff_events = session.events[before_handoff:]
                snapshot_end = handoff_events.index("JOBS_END:HANDOFF")
                hook_job = next(
                    index for index, event in enumerate(handoff_events)
                    if event.startswith("HOOK_JOB:")
                )
                self.assertLess(snapshot_end, hook_job)
                self.assertLess(hook_job, handoff_events.index("READY"))
                hook_pid = int(handoff_events[hook_job].partition(":")[2])
                self.assertTrue(Path(f"/proc/{hook_pid}").exists())

                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "behind-redefined-hook", epoch)
                self.assertFalse(session.boundary.can_dispatch(
                    generation=session.generation,
                    owner_epoch=epoch,
                ))

    def test_legacy_prompt_builtin_override_negative_control(self) -> None:
        """Internal-helper tampering exposes the removed prompt dispatch path."""
        executable = shutil.which("bash")
        if executable is None:
            self.skipTest("Bash is unavailable")
        choice = ShellChoice("bash", os.path.realpath(executable))
        with ShellProcess(choice) as session:
            session.wait_ready()
            session.take_user_control()
            session.send_user(
                b'builtin() { if [[ $1 == jobs ]]; then return 0; fi; command builtin "$@"; }\n'
            )
            session.wait_ready()
            session.send_user(
                b'__wb_emit() { if [[ $1 == READY && -n $__wb_handoff_pid ]]; then '
                b'sleep 30 >/dev/null 2>&1 & __wb_emit HIDDEN_JOB:$!; fi; '
                b'printf "%s\\n" "$*" >&9; }\n'
            )
            session.wait_ready()

            before_handoff = len(session.events)
            session.send_user(b"wb-handoff\n")
            session.wait_ready()
            events = session.events[before_handoff:]
            snapshot_end = events.index("JOBS_END:HANDOFF")
            job_index = next(i for i, event in enumerate(events) if event.startswith("HIDDEN_JOB:"))
            self.assertLess(snapshot_end, job_index)
            self.assertLess(job_index, events.index("READY"))
            job_pid = int(events[job_index].partition(":")[2])
            self.assert_live_job(job_pid)

            epoch = session.return_to_manager()
            with self.subTest(check="can_dispatch"):
                self.assertTrue(session.boundary.can_dispatch(
                    generation=session.generation, owner_epoch=epoch,
                ))
            with self.subTest(check="dispatch"):
                with patch.object(session, "_write_all") as write:
                    session.dispatch("behind-builtin-override", ":", expected_epoch=epoch)
                write.assert_called_once()

    def test_legacy_prompt_unfinished_hook_negative_control(self) -> None:
        """A forged READY authorizes a write before the old prompt hook completes."""
        executable = shutil.which("bash")
        if executable is None:
            self.skipTest("Bash is unavailable")
        choice = ShellChoice("bash", os.path.realpath(executable))
        with tempfile.TemporaryDirectory(prefix="cw03-hook-pause-") as directory:
            release_path = Path(directory) / "release"
            os.mkfifo(release_path)
            with ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(f"exec 8<>{shlex.quote(str(release_path))}\n".encode())
                session.wait_ready()
                session.send_user(
                    b'__wb_emit() { if [[ $1 == READY && -n $__wb_handoff_pid ]]; then '
                    b'printf "READY\\nJOBS_BEGIN:READY\\nJOBS_END:READY\\nHOOK_PAUSED\\n" >&9; '
                    b'while ! read -r -t 0.4 __wb_release <&8; do :; done; '
                    b'sleep 30 >/dev/null 2>&1 & printf "HOOK_JOB:%s\\n" "$!" >&9; '
                    b'else printf "%s\\n" "$*" >&9; fi; }\n'
                )
                session.wait_ready()

                release_fd = os.open(release_path, os.O_WRONLY | os.O_NONBLOCK)
                try:
                    session.send_user(b"wb-handoff\n")
                    session.wait_event("HOOK_PAUSED")
                    self.assertNotIn("HOOK_JOB", " ".join(session.events))
                    epoch = session.return_to_manager()
                    dispatchable_while_paused = session.boundary.can_dispatch(
                        generation=session.generation, owner_epoch=epoch,
                    )
                    with patch.object(session, "_write_all") as write:
                        try:
                            session.dispatch("behind-unfinished-hook", ":", expected_epoch=epoch)
                        except UnsafeShellState:
                            dispatch_rejected = True
                        else:
                            dispatch_rejected = False
                        dispatch_wrote = write.called
                finally:
                    os.write(release_fd, b"go\n")
                    os.close(release_fd)

                job_event = session.wait_event("HOOK_JOB:")
                job_pid = int(job_event.partition(":")[2])
                self.assert_live_job(job_pid)
                self.assertTrue(dispatchable_while_paused)
                self.assertFalse(dispatch_rejected)
                self.assertTrue(dispatch_wrote)

    def test_legacy_prompt_dash_jobs_override_negative_control(self) -> None:
        executable = shutil.which("dash")
        if executable is None:
            self.skipTest("dash is unavailable")
        choice = ShellChoice("sh", os.path.realpath(executable))
        with ShellProcess(choice) as session:
            session.wait_ready()
            session.take_user_control()
            session.send_user(b"jobs() { :; }\n")
            session.wait_ready()
            session.send_user(
                b'__wb_ready() { if [ -n "$__wb_handoff_pid" ]; then '
                b'sleep 30 >/dev/null 2>&1 & __wb_emit HIDDEN_JOB:$!; '
                b'fi; __wb_emit READY; }\n'
            )
            session.wait_ready()

            before_handoff = len(session.events)
            session.send_user(b"wb-handoff\n")
            session.wait_ready()
            events = session.events[before_handoff:]
            snapshot_end = events.index("JOBS_END:HANDOFF")
            job_index = next(i for i, event in enumerate(events) if event.startswith("HIDDEN_JOB:"))
            self.assertLess(snapshot_end, job_index)
            self.assertLess(job_index, events.index("READY"))
            job_pid = int(events[job_index].partition(":")[2])
            self.assert_live_job(job_pid)

            epoch = session.return_to_manager()
            with self.subTest(check="can_dispatch"):
                self.assertTrue(session.boundary.can_dispatch(
                    generation=session.generation, owner_epoch=epoch,
                ))
            with self.subTest(check="dispatch"):
                with patch.object(session, "_write_all") as write:
                    session.dispatch("behind-jobs-override", ":", expected_epoch=epoch)
                write.assert_called_once()

    def test_legacy_prompt_dash_post_snapshot_job_negative_control(self) -> None:
        executable = shutil.which("dash")
        if executable is None:
            self.skipTest("dash is unavailable")
        choice = ShellChoice("sh", os.path.realpath(executable))
        with ShellProcess(choice) as session:
            session.wait_ready()
            session.take_user_control()
            session.send_user(
                b'__wb_emit() { if [ "$1" = HOOK_OK:HANDOFF ]; then '
                b'sleep 30 >/dev/null 2>&1 & printf "PARENT_JOB:%s\\n" "$!" >&9; '
                b'fi; printf "%s\\n" "$*" >&9; }\n'
            )
            session.wait_ready()

            before_handoff = len(session.events)
            session.send_user(b"wb-handoff\n")
            session.wait_ready()
            events = session.events[before_handoff:]
            snapshot_end = events.index("JOBS_END:HANDOFF")
            job_index = next(i for i, event in enumerate(events) if event.startswith("PARENT_JOB:"))
            self.assertLess(snapshot_end, job_index)
            self.assertLess(job_index, events.index("READY"))
            job_pid = int(events[job_index].partition(":")[2])
            self.assertEqual(self.assert_live_job(job_pid), session.pid)

            epoch = session.return_to_manager()
            with self.subTest(check="can_dispatch"):
                self.assertTrue(session.boundary.can_dispatch(
                    generation=session.generation, owner_epoch=epoch,
                ))
            with self.subTest(check="dispatch"):
                with patch.object(session, "_write_all") as write:
                    session.dispatch("behind-parent-job", ":", expected_epoch=epoch)
                write.assert_called_once()

    def test_manual_suspended_job_remains_blocked_after_handoff(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"sleep 30\n")
                session.wait_foreground_child()
                session.send_user(b"\x1a")
                session.wait_ready()
                session.send_user(b"wb-handoff\n")
                session.wait_ready()
                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "behind-suspended-job", epoch)

    def test_handoff_text_inside_ps2_does_not_authorize_dispatch(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"printf 'unfinished\n")
                session.send_user(b"wb-handoff\n")
                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "behind-ps2", epoch)

    def test_nested_shell_handoff_cannot_authorize_parent_dispatch(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(
                    b"bash --noprofile --norc -i\n" if choice.kind == "bash" else b"dash -i\n"
                )
                session.wait_foreground_child()
                session.send_user(b"wb-handoff\n")
                session._detect_nested_shell()
                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "behind-nested", epoch)

    def test_lost_hook_and_cancel_remain_blocked_after_handoff(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable, case="hook"), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                command = b"unset PROMPT_COMMAND\n" if choice.kind == "bash" else b"PS1='plain$ '\n"
                session.send_user(command)
                with self.assertRaises(TimeoutError):
                    session.wait_ready(timeout=0.3)
                session.send_user(b"wb-handoff\n")
                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "behind-lost-hook", epoch)

            with self.subTest(shell=choice.executable, case="cancel"), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"sleep 30\n")
                session.wait_foreground_child()
                session.send_user(b"\x03")
                session.wait_ready()
                session.send_user(b"wb-handoff\n")
                session.wait_ready()
                epoch = session.return_to_manager()
                self.assert_dispatch_blocked_without_write(session, "behind-cancel", epoch)

    def test_unsubmitted_user_line_and_paste_fail_closed(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"echo USER_PENDING")
                epoch = session.return_to_manager()
                self.assertFalse(session.boundary.can_dispatch(
                    generation=session.generation,
                    owner_epoch=epoch,
                ))
                with self.assertRaises(UnsafeShellState):
                    session.dispatch(
                        "must-not-submit-user-line",
                        "__wb_emit SHOULD_NOT_RUN",
                        expected_epoch=epoch,
                    )
                self.assertFalse(any("START:must-not-submit-user-line" in item for item in session.events))

        for choice in available_shells():
            with self.subTest(shell=choice.executable, input="paste"), ShellProcess(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"\x1b[200~echo pasted\n\x1b[201~")
                epoch = session.return_to_manager()
                self.assertFalse(session.boundary.can_dispatch(
                    generation=session.generation,
                    owner_epoch=epoch,
                ))

    def test_real_current_shell_jobs_fd9_reports_background_and_suspend(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable, job="background"), ShellProcess(choice) as session:
                session.wait_ready()
                epoch = session.owner_epoch
                session.dispatch("background", "sleep 30 &", expected_epoch=epoch)
                session.wait_command("background")
                self.assertTrue(any(item.startswith("JOBS_BEGIN:background") for item in session.events))
                self.assertTrue(any(item.isdecimal() for item in session.events))
                self.assertTrue(session.boundary.needs_review)
                self.assertFalse(session.boundary.can_dispatch(
                    generation=session.generation,
                    owner_epoch=session.owner_epoch,
                ))

            with self.subTest(shell=choice.executable, job="suspend"), ShellProcess(choice) as session:
                session.wait_ready()
                session.dispatch("suspend", "sleep 30", expected_epoch=session.owner_epoch)
                session.wait_event("START:suspend")
                session.wait_foreground_child()
                session.take_user_control()
                session.send_user(b"\x1a")
                session.wait_command("suspend")
                self.assertTrue(any(item.startswith("JOBS_BEGIN:suspend") for item in session.events))
                self.assertTrue(any(item.isdecimal() for item in session.events))
                self.assertTrue(session.boundary.needs_review)

    def test_failure_repl_interrupt_and_exit_never_become_clean(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable, case="failure"), ShellProcess(choice) as session:
                session.wait_ready()
                session.dispatch("failure", "false", expected_epoch=session.owner_epoch)
                session.wait_command("failure")
                self.assertIn("DONE:failure:1", session.events)
                self.assertTrue(session.boundary.needs_review)

            with self.subTest(shell=choice.executable, case="repl"), ShellProcess(choice) as session:
                session.wait_ready()
                session.dispatch("repl", "read -r CW03_VALUE", expected_epoch=session.owner_epoch)
                session.wait_event("START:repl")
                with self.assertRaises(UnsafeShellState):
                    session.dispatch("blocked-behind-repl", ":", expected_epoch=session.owner_epoch)
                time.sleep(0.1)
                self.assertFalse(any(item.startswith("DONE:repl:") for item in session.events))
                session.take_user_control()
                session.send_user(b"\x03")
                with self.assertRaises(TimeoutError):
                    session.wait_command("repl", timeout=0.3)
                self.assertTrue(session.boundary.needs_review)

            with self.subTest(shell=choice.executable, case="exit"), ShellProcess(choice) as session:
                session.wait_ready()
                session.dispatch("exit-shell", "exit 23", expected_epoch=session.owner_epoch)
                session.wait_event("SHELL_EXIT")
                self.assertFalse(any(item.startswith("DONE:exit-shell:") for item in session.events))
                self.assertTrue(session.boundary.needs_review)

    def test_nested_interactive_shell_latches_unknown(self) -> None:
        for choice in available_shells():
            with self.subTest(shell=choice.executable), ShellProcess(choice) as session:
                session.wait_ready()
                session.dispatch(
                    "nested",
                    "bash --noprofile --norc -i" if choice.kind == "bash" else "dash -i",
                    expected_epoch=session.owner_epoch,
                )
                session.wait_event("START:nested")
                session.wait_foreground_child()
                session._detect_nested_shell()
                self.assertTrue(session.boundary.needs_review)
                self.assertFalse(session.boundary.can_dispatch(
                    generation=session.generation,
                    owner_epoch=session.owner_epoch,
                ))


if __name__ == "__main__":
    unittest.main()
