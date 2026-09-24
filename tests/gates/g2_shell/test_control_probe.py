"""Decisive real-shell observations for the CW-03 control-wait assumption."""
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workbench.terminal.shell_g2.control_probe import CleanupIncomplete, ControlWaitProbe
from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState, select_shell


def real_shells():
    bash = select_shell()
    if bash.kind == "bash":
        yield bash
    sh = os.path.realpath("/bin/sh")
    if os.path.basename(sh) == "dash":
        yield ShellChoice("sh", sh)


class ControlWaitExperiment(unittest.TestCase):
    def enter_wait(self, session):
        session.wait_ready()
        session.take_user_control()
        session.send_user(b"wb-handoff\n")
        session.wait_control()
        self.assertTrue(session.boundary._handoff_seen)
        self.assertFalse(session.boundary.needs_review)
        handoff = session.events.index(f"HANDOFF:{session.pid}")
        self.assertNotIn("READY", session.events[handoff:])

    def test_same_interpreter_preserves_state_without_prompt_reentry(self):
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory:
                with ControlWaitProbe(choice) as session:
                    session.wait_ready()
                    session.take_user_control()
                    session.send_user(f"cd {shlex.quote(directory)}\n".encode())
                    session.wait_ready()
                    session.send_user(b"export CW03_CONTROL_VALUE=kept\n")
                    session.wait_ready()
                    session.send_user(b"wb-handoff\n")
                    session.wait_control()
                    handoff = session.events.index(f"HANDOFF:{session.pid}")
                    self.assertNotIn("READY", session.events[handoff:])
                    session.dispatch_control(
                        "one", '__cw_emit "STATE:$$:$PWD:$CW03_CONTROL_VALUE"',
                        expected_epoch=session.owner_epoch, generation=session.generation,
                    )
                    session.wait_event("ACCEPT:one")
                    session.wait_event("START:one")
                    self.assertIn(f"STATE:{session.pid}:{directory}:kept", session.wait_event("STATE:"))
                    self.assertEqual(session.wait_event("EVAL_RETURNED:one:"), "EVAL_RETURNED:one:0")
                    session.wait_control()
                    if session.delegated:
                        session.wait_event("LOCAL_LIFETIME_DONE:one")
                    self.assertFalse(any(e.startswith("DONE:one:") for e in session.events))
                    self.assertEqual(os.readlink(f"/proc/{session.pid}/cwd"), directory)
                    self.assertNotIn("READY", session.events[handoff:])

    def test_unsubmitted_tail_and_background_job_block_request(self):
        for choice in real_shells():
            with self.subTest(shell=choice.kind, case="tail"), ControlWaitProbe(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"wb-handoff\nprintf pending")
                session.wait_control()
                with self.assertRaises(UnsafeShellState):
                    session.dispatch_control("tail", ":", expected_epoch=session.owner_epoch,
                                             generation=session.generation)
                self.assertNotIn("ACCEPT:tail", session.events)

            with self.subTest(shell=choice.kind, case="background"), ControlWaitProbe(choice) as session:
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"sleep 30 &\n")
                session.wait_ready()
                session.send_user(b"wb-handoff\n")
                session.wait_control()
                self.assertTrue(session.boundary.needs_review)
                with self.assertRaises(UnsafeShellState):
                    session.dispatch_control("job", ":", expected_epoch=session.owner_epoch,
                                             generation=session.generation)
                self.assertNotIn("ACCEPT:job", session.events)

    def test_early_ready_hook_cannot_grant_control_before_handoff_wait(self):
        choice = select_shell()
        if choice.kind != "bash":
            self.skipTest("Bash unavailable")
        with tempfile.TemporaryDirectory() as directory:
            release_path = os.path.join(directory, "release")
            os.mkfifo(release_path)
            release_fd = os.open(release_path, os.O_RDWR | os.O_NONBLOCK)
            try:
                with ControlWaitProbe(choice) as session:
                    session.wait_ready()
                    session.take_user_control()
                    hook = (f"__cw_ready() {{ __cw_emit READY; "
                            f"IFS= read -r cw_release < {shlex.quote(release_path)}; }}\n")
                    session.send_user(hook.encode())
                    session.wait_ready()
                    session.send_user(b"wb-handoff\n")
                    with self.assertRaises(UnsafeShellState):
                        session.dispatch_control("early", ":", expected_epoch=session.owner_epoch,
                                                 generation=session.generation)
                    os.write(release_fd, b"go\n")
                    session.wait_control()
                    session.dispatch_control("later", ":", expected_epoch=session.owner_epoch,
                                             generation=session.generation)
                    session.wait_event("ACCEPT:later")
                    self.assertEqual(session.wait_event("EVAL_RETURNED:later:"),
                                     "EVAL_RETURNED:later:0")
                    self.assertFalse(any(e.startswith("DONE:later:") for e in session.events))
                    self.assertNotIn("ACCEPT:early", session.events)
            finally:
                os.close(release_fd)

    def test_takeover_before_and_after_send_blocks_new_requests(self):
        for choice in real_shells():
            with self.subTest(shell=choice.kind, case="before"), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                self.assertEqual(session.request_takeover(), "not_sent")
                with self.assertRaises(UnsafeShellState):
                    session.dispatch_control("later", ":", expected_epoch=session.owner_epoch,
                                             generation=session.generation)
                session.wait_event(f"TAKEOVER_ACK:{session.pid}")
                self.assertNotIn("ACCEPT:later", session.events)

            with self.subTest(shell=choice.kind, case="after"), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                session.dispatch_control("read-one", 'head -n 1 >/dev/null; __cw_emit INPUT_CONSUMED',
                                         expected_epoch=session.owner_epoch,
                                         generation=session.generation)
                session.wait_event("START:read-one")
                session.wait_foreground_child()
                self.assertEqual(session.request_takeover(), "unknown")
                target = session.confirm_foreground_takeover()
                self.assertEqual(target.command_id, "read-one")
                session._drain(timeout=0.1)
                self.assertNotIn(f"TAKEOVER_ACK:{session.pid}", session.events)
                with self.assertRaises(UnsafeShellState):
                    session.dispatch_control("later", ":", expected_epoch=session.owner_epoch,
                                             generation=session.generation)
                session.send_confirmed_foreground(b"manual\n")
                self.assertEqual(session.wait_event("INPUT_CONSUMED"), "INPUT_CONSUMED")
                self.assertEqual(session.wait_event("EVAL_RETURNED:read-one:"),
                                 "EVAL_RETURNED:read-one:0")
                session.wait_event(f"TAKEOVER_ACK:{session.pid}")
                session.wait_event("READY")
                session.send_user(b"__cw_emit MANUAL_AFTER_ACK\n")
                session.wait_event("MANUAL_AFTER_ACK")
                self.assertNotIn("ACCEPT:later", session.events)

    def test_background_return_is_not_experiment_completion(self):
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                session.dispatch_control("background", "sleep 30 &",
                                         expected_epoch=session.owner_epoch,
                                         generation=session.generation)
                session.wait_event("START:background")
                self.assertEqual(session.wait_event("ACTIVE_JOBS:background"),
                                 "ACTIVE_JOBS:background")
                self.assertFalse(any(e.startswith("DONE:background:") for e in session.events))
                self.assertEqual(session.request_takeover(), "unknown")
                session.wait_event(f"TAKEOVER_ACK:{session.pid}")
                job_pids = [int(e) for e in session.events if e.isdecimal()]
                self.assertTrue(job_pids)
                self.assertTrue(any(os.path.exists(f"/proc/{pid}") for pid in job_pids))
                with self.assertRaises(UnsafeShellState):
                    session.dispatch_control("later", ":", expected_epoch=session.owner_epoch,
                                             generation=session.generation)

    def test_non_delegated_eval_return_is_not_lifetime_done(self):
        with ControlWaitProbe(select_shell()) as session:
            if session.delegated:
                self.skipTest("delegated scope is available")
            self.enter_wait(session)
            session.dispatch_control("simple", ":", expected_epoch=session.owner_epoch,
                                     generation=session.generation)
            session.wait_event("EVAL_RETURNED:simple:0")
            session.wait_event("LIFETIME_UNKNOWN:simple")
            self.assertFalse(any(e.startswith("DONE:simple:") for e in session.events))

    def test_delayed_drain_short_daemon_never_creates_done(self):
        if shutil.which("setsid") is None:
            self.skipTest("setsid unavailable")
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                session.dispatch_control("short-daemon",
                                         "setsid sh -c 'sleep 0.05 &' >/dev/null 2>&1 & wait",
                                         expected_epoch=session.owner_epoch,
                                         generation=session.generation)
                time.sleep(0.3)  # Intentionally delay the first host drain past daemon exit.
                session.wait_event("EVAL_RETURNED:short-daemon:0")
                session.wait_control()
                if session.delegated:
                    session.wait_event("LOCAL_LIFETIME_DONE:short-daemon")
                self.assertFalse(any(e.startswith("DONE:short-daemon:") for e in session.events))

    def test_takeover_waits_for_shell_migration_and_manual_prompt(self):
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                self.enter_wait(session)
                session.dispatch_control("queued", "sleep 0.2",
                                         expected_epoch=session.owner_epoch,
                                         generation=session.generation)
                session.wait_event("START:queued")
                self.assertEqual(session.request_takeover(), "unknown")
                with self.assertRaises(UnsafeShellState):
                    session.send_user(b"__cw_emit TOO_EARLY\n")
                self.assertNotIn(f"TAKEOVER_ACK:{session.pid}", session.events)
                session.wait_event(f"TAKEOVER_ACK:{session.pid}")
                if session.delegated:
                    self.assertEqual(session._pid_cgroup(session.pid), session.manual_cgroup)
                self.assertNotIn("TOO_EARLY", session.events)
                session.wait_event("READY")
                session.send_user(b"__cw_emit AFTER_PROMPT\n")
                session.wait_event("AFTER_PROMPT")

    def test_cleanup_reports_residual_group(self):
        with tempfile.TemporaryDirectory() as directory:
            residual = Path(directory) / "residual"
            residual.mkdir()
            session = ControlWaitProbe(select_shell())
            session.run_cgroup = residual  # Inject a cgroup path that cannot drain.
            with self.assertRaises(CleanupIncomplete):
                session.close()
            self.assertEqual(session.cleanup_residuals, (residual,))

    def test_delegated_reparented_daemon_lifetime(self):
        if shutil.which("setsid") is None:
            self.skipTest("setsid unavailable")
        for choice in real_shells():
            with self.subTest(shell=choice.kind), tempfile.TemporaryDirectory() as directory:
                with ControlWaitProbe(choice) as session:
                    if not session.delegated:
                        self.skipTest("delegated user scope unavailable")
                    session.wait_ready()
                    session.take_user_control()
                    session.send_user(f"cd {shlex.quote(directory)}\n".encode())
                    session.wait_ready()
                    session.send_user(b"export CW03_SCOPE_VALUE=kept\n")
                    session.wait_ready()
                    session.send_user(b"wb-handoff\n")
                    session.wait_control()
                    shell_pid = session.pid
                    manual_group = session.manual_cgroup
                    self.assertEqual(session._pid_cgroup(shell_pid), manual_group)
                    pid_file = os.path.join(directory, "daemon-pid")
                    script = ("setsid sh -c 'sleep 0.8 & echo $! > "
                              f"{shlex.quote(pid_file)}' >/dev/null 2>&1 & "
                              f"while [ ! -s {shlex.quote(pid_file)} ]; do sleep 0.01; done; wait")
                    session.dispatch_control("daemon", script,
                                             expected_epoch=session.owner_epoch,
                                             generation=session.generation)
                    self.assertEqual(session._pid_cgroup(shell_pid), session.run_cgroup)
                    session.wait_event("EVAL_RETURNED:daemon:0")
                    daemon_pid = int(Path(pid_file).read_text(encoding="ascii").strip())
                    self.assertNotEqual(os.getsid(daemon_pid), shell_pid)
                    session.wait_control()
                    self.assertEqual(session._pid_cgroup(shell_pid), manual_group)
                    self.assertTrue(session._populated(session.run_cgroup))
                    session.wait_event("LOCAL_WORK_ACTIVE:daemon")
                    self.assertFalse(any(e.startswith("DONE:daemon:") for e in session.events))
                    session.wait_event("LOCAL_LIFETIME_DONE:daemon", timeout=2)
                    self.assertFalse(session._populated(session.run_cgroup))
                    self.assertFalse(any(e.startswith("DONE:daemon:") for e in session.events))

    def test_delegated_manual_phase_daemon_blocks_run(self):
        if shutil.which("setsid") is None:
            self.skipTest("setsid unavailable")
        for choice in real_shells():
            with self.subTest(shell=choice.kind), ControlWaitProbe(choice) as session:
                if not session.delegated:
                    self.skipTest("delegated user scope unavailable")
                session.wait_ready()
                session.take_user_control()
                session.send_user(b"setsid sh -c 'sleep 1 &' >/dev/null 2>&1 & wait\n")
                session.wait_ready()
                session.send_user(b"wb-handoff\n")
                session.wait_control()
                self.assertTrue(session._populated(session.manual_cgroup))
                with self.assertRaises(UnsafeShellState):
                    session.dispatch_control("behind-manual-daemon", ":",
                                             expected_epoch=session.owner_epoch,
                                             generation=session.generation)

    def test_delegated_control_loss_cleanup_keeps_lifetime_unknown(self):
        for choice in real_shells():
            with self.subTest(shell=choice.kind):
                session = ControlWaitProbe(choice)
                try:
                    if not session.delegated:
                        self.skipTest("delegated user scope unavailable")
                    self.enter_wait(session)
                    session.dispatch_control("lost", "sleep 30",
                                             expected_epoch=session.owner_epoch,
                                             generation=session.generation)
                    session.wait_event("START:lost")
                    session.wait_foreground_child()
                    run_group = session.run_cgroup
                    os.close(session._request_fd)
                    self.assertFalse(any(e.startswith("DONE:lost:") for e in session.events))
                    session.close()
                    self.assertFalse(run_group.exists())
                    self.assertFalse(os.path.exists(f"/proc/{session.pid}"))
                finally:
                    session.close()
