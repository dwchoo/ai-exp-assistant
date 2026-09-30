"""Independent P2-2 verification (p27-review-fix-test-01): OMP pane close after PID/session reuse.

Expectations derived from the contracts before reading the fix:

- C-D45 / C-AC-34 / SPEC "관리된 shell과 인수": force-kill only a process whose
  identity is confirmed; an unprovable or foreign target is never killed.
- Review P2-2: after the OMP child was reaped, its PID/session number may name
  an unrelated process. Close/shutdown must then send *zero* signals to that
  foreign process or to any member of the foreign session, including when the
  foreign session leader itself already exited, and must never ``killpg`` a
  bare number whose identity was not proven at call time.
- The pane still cleans members of its *own* session that outlived the OMP
  leader (orphans in the leader's or a separate process group).

Real PID reuse needs a full pid_max cycle (4194304 here) or CAP_SYS_ADMIN
(ns_last_pid; unprivileged user namespaces are blocked on this host), so reuse
is simulated: the OMP child really exits and is reaped by the pane, then the
pane's PID number is pointed at a test-owned foreign session leader number
while its recorded start time stays the OMP's (exactly the state a reused
number presents). Every signal-sending call is spied at call time.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from workbench.backend.panes import OmpPane
from workbench.contracts.v1 import PaneId

FOREIGN = r'''
import os, signal, sys, time
log, mode = sys.argv[1], sys.argv[2]
def record(tag):
    def handler(signum, _frame):
        with open(log, "a") as out:
            out.write(f"{tag}:{os.getpid()}:{signal.Signals(signum).name}\n")
    return handler
def arm(tag):
    for s in signal.Signals:
        if s not in (signal.SIGKILL, signal.SIGSTOP, signal.SIGCHLD):
            try:
                signal.signal(s, record(tag))
            except (OSError, ValueError):
                pass
arm("leader")
members = []
for own_group in (False, True):
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read)
        if own_group:
            os.setpgid(0, 0)
        arm("member")
        os.write(write, b"R")
        os.close(write)
        while True:
            time.sleep(1)
    os.close(write)
    assert os.read(read, 1) == b"R"
    os.close(read)
    members.append(pid)
with open(log, "a") as out:
    out.write("READY:" + " ".join(map(str, members)) + "\n")
if mode == "exit":
    os._exit(0)
while True:
    time.sleep(1)
'''

# OMP stand-in whose own session keeps orphans after the leader exits: one in
# the leader's group, one in a separate group, both ignoring the tty SIGHUP.
OWN_ORPHANS = r'''
import os, signal, sys, time
signal.signal(signal.SIGHUP, signal.SIG_IGN)
for own_group in (False, True):
    if os.fork() == 0:
        if own_group:
            os.setpgid(0, 0)
        while True:
            time.sleep(1)
print("ORPHANS", flush=True)
time.sleep(0.2)
os._exit(3)
'''


def stat(pid: int) -> list[str] | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def alive(pid: int, start: str) -> bool:
    fields = stat(pid)
    return fields is not None and fields[19] == start and fields[0] not in {"Z", "X"}


def kill_exact(pid: int, start: str) -> None:
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if alive(pid, start):
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def session_members(session: int) -> dict[int, str]:
    found = {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            fields = stat(int(name))
            if fields and int(fields[3]) == session and fields[0] not in {"Z", "X"}:
                found[int(name)] = fields[19]
    return found


def pidfd_target(fd: int) -> int | None:
    try:
        for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
            if line.startswith("Pid:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


class SignalSpy:
    """Records every signal the pane sends, with the target identity at call time."""

    def __init__(self, test: unittest.TestCase):
        self.calls: list[dict] = []
        self._originals = (os.kill, os.killpg, signal.pidfd_send_signal)
        spy = self

        def killpg(group, signum):
            fields = stat(group)
            spy.calls.append({"fn": "killpg", "target": group, "signal": signum,
                              "leader_start": fields[19] if fields else None,
                              "leader_ppid": int(fields[1]) if fields else None,
                              "leader_state": fields[0] if fields else None})
            return spy._originals[1](group, signum)

        def kill(pid, signum):
            spy.calls.append({"fn": "kill", "target": pid, "signal": signum})
            return spy._originals[0](pid, signum)

        def pidfd_send_signal(fd, signum, *args):
            spy.calls.append({"fn": "pidfd", "target": pidfd_target(fd), "signal": signum})
            return spy._originals[2](fd, signum, *args)

        os.kill, os.killpg, signal.pidfd_send_signal = kill, killpg, pidfd_send_signal
        test.addCleanup(self.restore)

    def restore(self) -> None:
        os.kill, os.killpg, signal.pidfd_send_signal = self._originals

    def targets(self) -> set[int]:
        return {call["target"] for call in self.calls}


class PaneCloseAfterReuseTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="p27c-reuse-"))
        self.owned: dict[int, str] = {}
        self.leaders: list[subprocess.Popen] = []
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        for pid, start in self.owned.items():
            kill_exact(pid, start)
        deadline = time.monotonic() + 5
        while any(alive(p, s) for p, s in self.owned.items()) and time.monotonic() < deadline:
            time.sleep(0.02)
        left = {p: s for p, s in self.owned.items() if alive(p, s)}
        for leader in self.leaders:
            leader.wait(5)  # reap our own foreign-session leaders
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()
        self.assertEqual(left, {}, "test-owned processes survived cleanup")

    def reaped_pane(self, argv: list[str]) -> OmpPane:
        pane = OmpPane(PaneId.WORKER_OMP, "worker", argv, {"PATH": "/usr/bin:/bin"})
        self.owned[pane.pid] = str(pane.ref.start_ticks)
        deadline = time.monotonic() + 10
        while pane.poll() is None and time.monotonic() < deadline:
            pane.pump()
            time.sleep(0.02)
        self.assertIsNotNone(pane.returncode, "stand-in OMP never exited")
        self.assertIsNone(stat(pane.pid), "pane did not reap its own child")
        return pane

    def foreign_session(self, mode: str) -> tuple[subprocess.Popen, Path, dict[int, str]]:
        log = self.directory / f"foreign-{mode}.log"
        leader = subprocess.Popen([sys.executable, "-c", FOREIGN, str(log), mode], start_new_session=True,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.leaders.append(leader)
        leader_start = stat(leader.pid)[19]
        self.owned[leader.pid] = leader_start
        deadline = time.monotonic() + 10
        while not (log.exists() and "READY:" in log.read_text()) and time.monotonic() < deadline:
            time.sleep(0.02)
        ready = [line for line in log.read_text().splitlines() if line.startswith("READY:")]
        self.assertTrue(ready, "foreign session never became ready")
        members = {int(p): stat(int(p))[19] for p in ready[0].split(":")[1].split()}
        self.owned.update(members)
        if mode == "exit":
            self.assertEqual(leader.wait(10), 0)
            self.owned.pop(leader.pid)
            self.assertIsNone(stat(leader.pid))
        else:
            members[leader.pid] = leader_start
        for pid in members:
            self.assertEqual(os.getsid(pid), leader.pid, "fixture member outside the foreign session")
        return leader, log, members

    def assert_no_foreign_signal(self, spy: SignalSpy, log: Path, foreign: dict[int, str], session: int) -> None:
        received = [line for line in log.read_text().splitlines() if not line.startswith("READY:")]
        survivors = {p: s for p, s in foreign.items() if alive(p, s)}
        foreign_calls = [c for c in spy.calls if c["target"] in set(foreign) | {session}]
        self.assertEqual((received, survivors, foreign_calls), ([], foreign, []),
                         f"foreign session {session} was signalled (C-D45); all calls: {spy.calls}")

    def assert_killpg_only_on_proven_child(self, spy: SignalSpy, pane: OmpPane) -> None:
        for call in spy.calls:
            if call["fn"] != "killpg":
                continue
            proven = (call["target"] == pane.ref.pid and call["leader_start"] == str(pane.ref.start_ticks)
                      and call["leader_ppid"] == os.getpid())
            self.assertTrue(proven, f"killpg on a bare/unproven number: {call}")

    def test_reaped_omp_pid_now_names_a_live_foreign_leader(self):
        pane = self.reaped_pane(["/bin/sh", "-c", "exit 3"])
        leader, log, foreign = self.foreign_session("stay")
        spy = SignalSpy(self)
        pane.pid = leader.pid  # simulated reuse: same number, different start time
        self.assertNotEqual(stat(leader.pid)[19], str(pane.ref.start_ticks))
        result = pane.close()
        time.sleep(0.3)  # let any delivered catchable signal reach the log
        spy.restore()
        self.assertEqual(result["exit_status"], 3)
        self.assert_no_foreign_signal(spy, log, foreign, leader.pid)
        self.assert_killpg_only_on_proven_child(spy, pane)
        self.assertEqual([c for c in spy.calls if c["fn"] == "killpg"], [], "killpg after the OMP was reaped")

    def test_reaped_omp_session_number_now_names_a_foreign_session_whose_leader_exited(self):
        pane = self.reaped_pane(["/bin/sh", "-c", "exit 3"])
        leader, log, foreign = self.foreign_session("exit")
        spy = SignalSpy(self)
        pane.pid = leader.pid  # the number is a foreign session ID; no live leader holds it
        self.assertEqual(set(session_members(leader.pid)), set(foreign))
        pane.close()
        time.sleep(0.3)
        spy.restore()
        self.assert_no_foreign_signal(spy, log, foreign, leader.pid)
        self.assertEqual([c for c in spy.calls if c["fn"] == "killpg"], [], "killpg after the OMP was reaped")

    def test_own_orphaned_session_members_are_still_cleaned_and_foreign_untouched(self):
        leader, log, foreign = self.foreign_session("stay")  # unrelated, concurrently alive
        spy = SignalSpy(self)
        pane = OmpPane(PaneId.WORKER_OMP, "worker", [sys.executable, "-c", OWN_ORPHANS],
                       {"PATH": "/usr/bin:/bin"})
        session = pane.pid
        self.owned[pane.pid] = str(pane.ref.start_ticks)
        orphans: dict[int, str] = {}
        deadline = time.monotonic() + 10
        while (pane.poll() is None or len(orphans) < 2) and time.monotonic() < deadline:
            pane.pump()
            orphans.update({p: s for p, s in session_members(session).items() if p != session})
            time.sleep(0.02)
        self.owned.update(orphans)
        self.assertEqual(pane.returncode, 3)
        self.assertEqual(len(orphans), 2, f"fixture orphans not observed: {orphans}")
        groups = {int(stat(p)[2]) for p in orphans}
        self.assertEqual(len(groups), 2, "orphans must span the leader's and a separate group")
        pane.close()
        deadline = time.monotonic() + 3
        while any(alive(p, s) for p, s in orphans.items()) and time.monotonic() < deadline:
            time.sleep(0.02)
        spy.restore()
        self.assertEqual({p: s for p, s in orphans.items() if alive(p, s)}, {},
                         "own orphaned session members survived close")
        self.assertEqual(session_members(session), {})
        self.assert_no_foreign_signal(spy, log, foreign, leader.pid)
        self.assertEqual([c for c in spy.calls if c["fn"] == "killpg"], [], "killpg after the OMP was reaped")
        self.assertTrue(spy.targets() >= set(orphans), spy.calls)

    def test_live_omp_close_signals_only_its_proven_group_and_session(self):
        leader, log, foreign = self.foreign_session("stay")
        spy = SignalSpy(self)
        pane = OmpPane(PaneId.MANAGER_OMP, "manager",
                       ["/bin/sh", "-c", "trap '' TERM; sleep 1000 & exec sleep 1000"],
                       {"PATH": "/usr/bin:/bin"})
        self.owned[pane.pid] = str(pane.ref.start_ticks)
        deadline = time.monotonic() + 5
        members: dict[int, str] = {}
        while len(members) < 2 and time.monotonic() < deadline:
            pane.pump()
            members = session_members(pane.pid)
            time.sleep(0.05)
        self.owned.update(members)
        pane.close(grace=0.5)
        deadline = time.monotonic() + 3
        while any(alive(p, s) for p, s in members.items()) and time.monotonic() < deadline:
            time.sleep(0.02)
        spy.restore()
        self.assertEqual({p: s for p, s in members.items() if alive(p, s)}, {}, "live pane session survived close")
        self.assert_killpg_only_on_proven_child(spy, pane)
        self.assert_no_foreign_signal(spy, log, foreign, leader.pid)


if __name__ == "__main__":
    unittest.main()
