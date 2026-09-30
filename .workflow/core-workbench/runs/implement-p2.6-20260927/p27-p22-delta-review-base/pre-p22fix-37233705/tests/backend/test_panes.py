"""Real-PTY pane admission: whole-frame queueing, reasons, sequences and owned cleanup."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

from workbench.backend.panes import INPUT_QUEUE_BYTES, OmpPane, ShellPane
from workbench.contracts.ui_v1 import Reason
from workbench.contracts.v1 import PaneId
from workbench.runtime.process_evidence import LinuxProcessProbe
from workbench.terminal.shell_g2.prototype import ShellChoice


def pump_until(pane, predicate, timeout=5.0):
    chunks = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunks.extend(pane.pump())
        if predicate(chunks):
            return chunks
        time.sleep(0.02)
    raise AssertionError(f"timeout; got {b''.join(c.data for c in chunks)[-200:]!r}")


class OmpPaneTests(unittest.TestCase):
    def test_queue_admission_is_all_or_nothing_against_a_non_reading_process(self):
        pane = OmpPane(PaneId.WORKER_OMP, "worker", ["/bin/sh", "-c", "stty raw -echo; exec sleep 30"],
                       {"PATH": "/usr/bin:/bin"})
        try:
            time.sleep(0.3)
            self.assertEqual(pane.admit(b"x" * (INPUT_QUEUE_BYTES + 1))[0], Reason.PASTE_TOO_LARGE)
            self.assertEqual(pane.info()["queued_input_bytes"], 0)
            self.assertIsNone(pane.admit(b"a" * INPUT_QUEUE_BYTES))
            queued = pane.info()["queued_input_bytes"]
            self.assertGreater(queued, INPUT_QUEUE_BYTES // 2)  # the child never reads
            # Admission flushes first, so free space may grow slightly; a frame
            # larger than any possible free space must still be refused whole.
            refused = pane.admit(b"b" * INPUT_QUEUE_BYTES)
            self.assertEqual(refused[0], Reason.QUEUE_FULL)
            self.assertIn("free queue space", refused[1])
            self.assertLessEqual(pane.info()["queued_input_bytes"], queued)
            self.assertNotIn(b"b", pane._pending)
        finally:
            ref = pane.ref
            pane.close(grace=0.5)
        self.assertEqual(LinuxProcessProbe().observe(ref).state, "dead")

    def test_display_sequences_are_monotonic_and_replay_is_bounded(self):
        pane = OmpPane(PaneId.MANAGER_OMP, "manager",
                       ["/bin/sh", "-c", "i=0; while [ $i -lt 400 ]; do printf '%01000d\\n' $i; i=$((i+1)); done; sleep 30"],
                       {"PATH": "/usr/bin:/bin"})
        try:
            chunks = pump_until(pane, lambda c: sum(len(x.data) for x in c) >= 400 * 1000)
            sequences = [c.sequence for c in chunks]
            self.assertEqual(sequences, list(range(1, len(chunks) + 1)))
            self.assertTrue(all(c.session_id == pane.session_id and c.session_generation == 1 for c in chunks))
            retained = pane.replay()
            self.assertLessEqual(sum(len(c.data) for c in retained), 256 * 1024 + 65536)
            self.assertEqual(retained[-1].sequence, sequences[-1])
            pane.resize(40, 120)
            self.assertEqual((pane.info()["rows"], pane.info()["cols"]), (40, 120))
        finally:
            pane.close(grace=0.5)

    def test_close_reaps_every_process_group_in_the_pane_session(self):
        pane = OmpPane(PaneId.WORKER_OMP, "worker",
                       ["/bin/bash", "-c", "set -m; sleep 60 & sleep 60 & echo started; wait"], {"PATH": "/usr/bin:/bin"})
        pump_until(pane, lambda c: b"started" in b"".join(x.data for x in c))
        members = [p for p in os.listdir("/proc") if p.isdigit() and self._sid(p) == pane.pid]
        self.assertGreaterEqual(len(members), 3)
        pane.close(grace=0.5)
        time.sleep(0.2)
        self.assertEqual([p for p in os.listdir("/proc") if p.isdigit() and self._sid(p) == pane.pid], [])
        self.assertEqual(pane.admit(b"x")[0], Reason.PANE_UNAVAILABLE)

    def test_exited_process_leaves_the_select_set(self):
        pane = OmpPane(PaneId.MANAGER_OMP, "manager", ["/bin/sh", "-c", "echo bye"], {"PATH": "/usr/bin:/bin"})
        try:
            deadline = time.monotonic() + 5
            while (pane.fds() or pane.poll() is None) and time.monotonic() < deadline:
                pane.pump()
                time.sleep(0.02)
            self.assertEqual((pane.fds(), pane.poll()), ([], 0))
            self.assertFalse(pane.info()["alive"])
        finally:
            pane.close(grace=0.2)

    def test_close_after_reap_never_signals_a_foreign_session_reusing_the_pid(self):
        """P2-2 / C-D45: a reaped OMP PID proves nothing about a later owner."""
        pane = OmpPane(PaneId.WORKER_OMP, "worker", ["/bin/sh", "-c", "exit 0"], {"PATH": "/usr/bin:/bin"})
        deadline = time.monotonic() + 5
        while pane.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(pane.returncode)
        foreign = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & exec sleep 60"], start_new_session=True,
                                   stdin=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while len(self._members(foreign.pid)) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            members = self._members(foreign.pid)
            self.assertEqual(len(members), 2)
            pane.pid = foreign.pid  # simulated PID reuse by an unrelated session leader
            pane.close(grace=0.2)
            time.sleep(0.2)
            self.assertEqual(self._members(foreign.pid), members, "foreign session was signalled")
        finally:
            os.killpg(foreign.pid, signal.SIGKILL)  # test-owned session
            foreign.wait(5)

    def test_close_after_omp_exit_still_reaps_its_own_orphaned_session_members(self):
        pane = OmpPane(PaneId.WORKER_OMP, "worker", ["/bin/sh", "-c", "(trap '' HUP; exec sleep 60) & sleep 0.3; exit 0"],
                       {"PATH": "/usr/bin:/bin"})
        deadline = time.monotonic() + 5
        while (pane.poll() is None or not self._members(pane.pid)) and time.monotonic() < deadline:
            pane.pump()
            time.sleep(0.02)
        orphans = self._members(pane.pid)
        self.assertTrue(orphans)
        pane.close(grace=0.2)
        time.sleep(0.2)
        self.assertEqual(self._members(pane.pid), [])

    def test_omp_child_inherits_default_sigpipe_and_sigxfsz(self):
        """P2-3 / C-AC-32: Python's SIG_IGN for PIPE/XFSZ must not reach the OMP process."""
        pane = OmpPane(PaneId.WORKER_OMP, "worker", ["/bin/sh", "-c", "grep SigIgn /proc/$$/status; sleep 30"],
                       {"PATH": "/usr/bin:/bin"})
        try:
            chunks = pump_until(pane, lambda c: b"SigIgn" in b"".join(x.data for x in c)
                                and b"\n" in b"".join(x.data for x in c).split(b"SigIgn", 1)[1])
            text = b"".join(x.data for x in chunks).decode()
            mask = int(text.split("SigIgn:", 1)[1].split()[0], 16)
            self.assertFalse(mask & (1 << (signal.SIGPIPE - 1)), text)
            self.assertFalse(mask & (1 << (signal.SIGXFSZ - 1)), text)
        finally:
            pane.close(grace=0.5)

    @classmethod
    def _members(cls, session):
        return sorted(p for p in os.listdir("/proc") if p.isdigit() and cls._sid(p) == session)

    @staticmethod
    def _sid(pid):
        try:
            return int(Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()[3])
        except (OSError, IndexError, ValueError):
            return None


class ShellPaneTests(unittest.TestCase):
    def test_manual_input_reaches_the_parent_and_manager_ownership_holds_input(self):
        for choice in (ShellChoice("bash", "/usr/bin/bash"), ShellChoice("sh", "/usr/bin/dash")):
            with self.subTest(choice.kind), tempfile.TemporaryDirectory(prefix="cw17-pane-") as directory:
                pane = ShellPane(choice, {"PATH": "/usr/bin:/bin", "HOME": directory, "LANG": "C.UTF-8"})
                try:
                    marker = Path(directory) / "marker"
                    self.assertIsNone(pane.admit(f"printf %s $$ > {marker}\r".encode()))
                    pump_until(pane, lambda _: marker.exists() and marker.read_text() != "")
                    self.assertEqual(marker.read_text(), str(pane.shell.parent_pid))
                    self.assertEqual(pane.admit(b"x" * (INPUT_QUEUE_BYTES + 1))[0], Reason.PASTE_TOO_LARGE)
                    self.assertIsNone(pane.admit(b"wb-handoff\r"))
                    pump_until(pane, lambda _: pane.state["parent_mode"] == "control_wait")
                    state = pane.handoff()
                    self.assertEqual((state["input_owner"], state["parent_mode"]), ("manager", "control_wait"))
                    refused = pane.admit(b"echo no\r")
                    self.assertEqual(refused[0], Reason.INPUT_OWNER_MANAGER)
                    self.assertEqual(pane.info()["queued_input_bytes"], 0)
                    requested = pane.request_takeover()
                    self.assertEqual(requested["input_owner"], "user")
                    self.assertTrue(requested["takeover_requested"])
                    pump_until(pane, lambda _: pane.state["parent_mode"] == "manual_prompt")
                    self.assertIsNone(pane.admit(b"true\r"))
                    info = pane.info()
                    self.assertEqual(info["process"]["pid"], pane.shell.parent_pid)
                    self.assertIsNone(info["shell"]["supervisor"])
                    self.assertIsNone(pane.admit(b"exit\r"))
                    pump_until(pane, lambda _: pane.fds() == [])
                    self.assertFalse(pane.info()["alive"])
                finally:
                    ref = pane.ref
                    pane.close()
                self.assertEqual(LinuxProcessProbe().observe(ref).state, "dead")


if __name__ == "__main__":
    unittest.main()
