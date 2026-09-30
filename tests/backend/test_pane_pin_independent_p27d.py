"""Independent verification of the P2-2 fix (p27-p22-rerun-test-01): pinned session ownership.

Expectations derived from C-D45 / C-AC-34 and review P2-2 before reading the fix:

- Only a process whose identity is proven is force-killed; a bare pid/session
  number that may have been reused is never signalled.
- After the OMP leader is reaped, its own orphans may still be cleaned, but only
  while ownership of the session number stays provable (the pane holds identity
  handles taken while the number was provably its own). Once no own member
  remains alive, the number proves nothing: zero signals, even if the number
  now names a foreign session (simulated as in p27c by pointing ``pane.pid`` at
  a test-owned foreign session leader).
- Orphans that fork after the leader was reaped are still own-session members
  and must be cleaned while an own anchor is alive.
- close() leaks no descriptors (pidfds, PTY master).
- Bounded pinning: with more members than SESSION_PIN_LIMIT the pane must never
  signal a process it did not pin/prove (SESSION_PIN_LIMIT is patched to small
  values instead of spawning 256+ processes).
- A live OMP's close still TERMs its group and reaps its whole session.

Helpers are reused from the p27c file (loaded by path, its TestCases are not imported).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import workbench.backend.panes as panes_module
from workbench.backend.panes import OmpPane
from workbench.contracts.v1 import PaneId

_spec = importlib.util.spec_from_file_location("_p27c_helpers", Path(__file__).with_name("test_pane_identity_independent_p27c.py"))
_p27c = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_p27c)
stat, alive, kill_exact, session_members = _p27c.stat, _p27c.alive, _p27c.kill_exact, _p27c.session_members
SignalSpy, FOREIGN = _p27c.SignalSpy, _p27c.FOREIGN

# Stand-in OMP. argv: dir, orphan_count. Orphans (alternating own/leader group) ignore
# SIGHUP, exit when <dir>/stop exists, and fork one grandchild once <dir>/go exists.
# The leader exits (status 3) only once <dir>/leader_exit exists. Everything has a
# 90 s self-destruct so a crashed test cannot leak.
OMP = r'''
import os, signal, sys, time
directory, count = sys.argv[1], int(sys.argv[2])
signal.signal(signal.SIGHUP, signal.SIG_IGN)
deadline = time.time() + 90
def flag(name):
    return os.path.exists(os.path.join(directory, name))
def grandchild():
    while time.time() < deadline:
        time.sleep(0.05)
    os._exit(0)
def orphan():
    forked = False
    while time.time() < deadline and not flag("stop"):
        if flag("go") and not forked:
            forked = True
            if os.fork() == 0:
                grandchild()
        time.sleep(0.05)
    os._exit(0)
for index in range(count):
    if os.fork() == 0:
        if index % 2:
            os.setpgid(0, 0)
        orphan()
print("UP", flush=True)
while time.time() < deadline and not flag("leader_exit"):
    time.sleep(0.05)
os._exit(3)
'''

LIVE = r'''
import os, signal, sys, time
mode = sys.argv[1]
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
for own_group in (False, True):
    if os.fork() == 0:
        if own_group:
            os.setpgid(0, 0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        end = time.time() + 90
        while time.time() < end:
            time.sleep(0.05)
        os._exit(0)
end = time.time() + 90
while time.time() < end:
    time.sleep(0.05)
'''


def wait_until(predicate, timeout: float, pane: OmpPane | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if pane is not None:
            pane.pump()
        time.sleep(0.02)
    return predicate()


def open_fds() -> set[str]:
    return {f"{name}->{os.readlink(f'/proc/self/fd/{name}')}" if os.path.exists(f"/proc/self/fd/{name}") else name
            for name in os.listdir("/proc/self/fd")}


class PanePinTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="p27d-pin-"))
        self.owned: dict[int, str] = {}
        self.leaders: list[subprocess.Popen] = []
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        for pid, start in self.owned.items():
            kill_exact(pid, start)
        wait_until(lambda: not any(alive(p, s) for p, s in self.owned.items()), 5)
        left = {p: s for p, s in self.owned.items() if alive(p, s)}
        for leader in self.leaders:
            leader.wait(5)
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()
        self.assertEqual(left, {}, "test-owned processes survived cleanup")

    def flag(self, name: str) -> None:
        (self.directory / name).write_text("1")

    def start(self, count: int) -> tuple[OmpPane, dict[int, str]]:
        pane = OmpPane(PaneId.WORKER_OMP, "worker", [sys.executable, "-c", OMP, str(self.directory), str(count)],
                       {"PATH": "/usr/bin:/bin"})
        self.owned[pane.pid] = str(pane.ref.start_ticks)

        def orphans() -> dict[int, str]:
            return {p: s for p, s in session_members(pane.pid).items() if p != pane.pid}

        self.assertTrue(wait_until(lambda: len(orphans()) == count, 10, pane), "orphans not started")
        found = orphans()
        self.owned.update(found)
        for _ in range(5):  # let the pane pump a few times while the leader is alive
            pane.pump()
            time.sleep(0.02)
        return pane, found

    def reap_leader(self, pane: OmpPane) -> None:
        self.flag("leader_exit")
        self.assertTrue(wait_until(lambda: pane.poll() is not None, 10, pane), "leader never exited")
        self.assertEqual(pane.returncode, 3)
        self.assertIsNone(stat(pane.pid), "leader not reaped")

    def foreign_session(self) -> tuple[subprocess.Popen, Path, dict[int, str]]:
        self.foreign_count = getattr(self, "foreign_count", 0) + 1
        log = self.directory / f"foreign{self.foreign_count}.log"
        leader = subprocess.Popen([sys.executable, "-c", FOREIGN, str(log), "stay"], start_new_session=True,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.leaders.append(leader)
        start = stat(leader.pid)[19]
        self.owned[leader.pid] = start
        self.assertTrue(wait_until(lambda: log.exists() and "READY:" in log.read_text(), 10))
        ready = [ln for ln in log.read_text().splitlines() if ln.startswith("READY:")][0]
        members = {int(p): stat(int(p))[19] for p in ready.split(":")[1].split()}
        self.owned.update(members)
        members[leader.pid] = start
        return leader, log, members

    def assert_foreign_untouched(self, spy: SignalSpy, log: Path, foreign: dict[int, str], session: int) -> None:
        received = [ln for ln in log.read_text().splitlines() if not ln.startswith("READY:")]
        survivors = {p: s for p, s in foreign.items() if alive(p, s)}
        calls = [c for c in spy.calls if c["target"] in set(foreign) | {session}]
        self.assertEqual((received, survivors, calls), ([], foreign, []), f"foreign signalled: {spy.calls}")

    def no_killpg(self, spy: SignalSpy) -> None:
        self.assertEqual([c for c in spy.calls if c["fn"] == "killpg"], [], f"killpg after reap: {spy.calls}")

    # (a) -----------------------------------------------------------------
    def test_a_own_members_all_exit_then_session_number_is_foreign_zero_signals(self):
        pane, orphans = self.start(2)
        self.reap_leader(pane)  # OMP gone, orphans alive (pinned while leader zombie)
        self.flag("stop")
        self.assertTrue(wait_until(lambda: not any(alive(p, s) for p, s in orphans.items()), 10),
                        "orphans did not exit")
        leader, log, foreign = self.foreign_session()
        spy = SignalSpy(self)
        pane.pid = leader.pid  # session number now names a live foreign session
        self.assertNotEqual(stat(leader.pid)[19], str(pane.ref.start_ticks))
        pane.close()
        time.sleep(0.3)
        spy.restore()
        self.assert_foreign_untouched(spy, log, foreign, leader.pid)
        self.no_killpg(spy)
        self.assertEqual(spy.calls, [], f"signals sent although nothing own was provable: {spy.calls}")

    def test_a_same_but_foreign_leader_already_exited(self):
        pane, orphans = self.start(2)
        self.reap_leader(pane)
        self.flag("stop")
        self.assertTrue(wait_until(lambda: not any(alive(p, s) for p, s in orphans.items()), 10))
        leader, log, foreign = self.foreign_session()
        spy = SignalSpy(self)
        # the foreign leader is killed by the test; its session number stays held by members
        kill_exact(leader.pid, foreign[leader.pid])
        leader.wait(5)
        self.owned.pop(leader.pid, None)
        foreign.pop(leader.pid)
        pane.pid = leader.pid
        self.assertTrue(set(session_members(leader.pid)) == set(foreign))
        spy.calls.clear()
        pane.close()
        time.sleep(0.3)
        spy.restore()
        self.assertEqual(spy.calls, [], f"foreign session signalled: {spy.calls}")
        self.assertEqual({p: s for p, s in foreign.items() if alive(p, s)}, foreign)
        self.assertEqual([ln for ln in log.read_text().splitlines() if not ln.startswith("READY:")], [])

    # (b) -----------------------------------------------------------------
    def test_b_grandchildren_forked_after_reap_are_cleaned_while_anchor_alive(self):
        leader, log, foreign = self.foreign_session()
        pane, orphans = self.start(2)
        spy = SignalSpy(self)
        self.reap_leader(pane)
        self.flag("go")  # orphans now fork grandchildren, after the OMP reap
        session = pane.pid
        self.assertTrue(wait_until(lambda: len(session_members(session)) >= 4, 10), "grandchildren not created")
        members = session_members(session)
        grand = {p: s for p, s in members.items() if p not in orphans}
        self.assertEqual(len(grand), 2)
        self.owned.update(grand)
        pane.close()
        self.assertTrue(wait_until(lambda: not any(alive(p, s) for p, s in members.items()), 3))
        time.sleep(0.2)
        spy.restore()
        self.assertEqual({p: s for p, s in members.items() if alive(p, s)}, {}, "own members survived close")
        self.assertEqual(session_members(session), {})
        self.assertTrue(spy.targets() >= set(members), spy.calls)
        self.assert_foreign_untouched(spy, log, foreign, leader.pid)
        self.no_killpg(spy)

    # (c) -----------------------------------------------------------------
    def test_c_no_descriptor_leak_after_close(self):
        for scenario in ("orphans_alive", "orphans_gone"):
            with self.subTest(scenario):
                before = len(os.listdir("/proc/self/fd"))
                pane, orphans = self.start(3)
                self.reap_leader(pane)
                if scenario == "orphans_gone":
                    self.flag("stop")
                    self.assertTrue(wait_until(lambda: not any(alive(p, s) for p, s in orphans.items()), 10))
                pane.close()
                wait_until(lambda: not any(alive(p, s) for p, s in orphans.items()), 3)
                after = len(os.listdir("/proc/self/fd"))
                self.assertEqual(after, before, f"fd leak in {scenario}: {before} -> {after}")
                for name in ("stop", "go", "leader_exit"):
                    (self.directory / name).unlink(missing_ok=True)

    def test_c_no_descriptor_leak_after_live_close(self):
        before = len(os.listdir("/proc/self/fd"))
        pane = OmpPane(PaneId.MANAGER_OMP, "manager", [sys.executable, "-c", LIVE, "stubborn"], {"PATH": "/usr/bin:/bin"})
        self.owned[pane.pid] = str(pane.ref.start_ticks)
        self.assertTrue(wait_until(lambda: len(session_members(pane.pid)) == 3, 10, pane))
        members = session_members(pane.pid)
        self.owned.update(members)
        pane.close(grace=0.3)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    # (d) -----------------------------------------------------------------
    def test_d_pin_limit_never_signals_unpinned_or_foreign(self):
        # SESSION_PIN_LIMIT is 256; sampled by patching it to 1 and 3 with 8 members.
        for limit in (1, 3):
            with self.subTest(limit=limit), mock.patch.object(panes_module, "SESSION_PIN_LIMIT", limit):
                for name in ("stop", "go", "leader_exit"):
                    (self.directory / name).unlink(missing_ok=True)
                leader, log, foreign = self.foreign_session()
                pane, orphans = self.start(8)
                spy = SignalSpy(self)
                self.reap_leader(pane)
                self.assertLessEqual(len(pane._pins), limit, "pane pinned more than the limit")
                pane.close()
                time.sleep(0.2)
                spy.restore()
                own = set(orphans)
                self.assertTrue(spy.targets() <= own, f"signal outside own orphans: {spy.calls}")
                self.assertTrue(all(c["fn"] == "pidfd" for c in spy.calls), f"non-pidfd signal: {spy.calls}")
                self.assert_foreign_untouched(spy, log, foreign, leader.pid)
                self.no_killpg(spy)
                self.assertFalse(any(c["signal"] != signal.SIGKILL for c in spy.calls), spy.calls)
                # every signalled pid was a member whose identity is unchanged (not reused/foreign)
                for call in spy.calls:
                    self.assertIn(call["target"], orphans)
                self.flag("stop")  # the rest (unpinned survivors are allowed) exit by themselves
                self.assertTrue(wait_until(lambda: not any(alive(p, s) for p, s in orphans.items()), 10))
                kill_exact(leader.pid, foreign[leader.pid])
                for pid, start in foreign.items():
                    kill_exact(pid, start)
                leader.wait(5)
                self.owned.pop(leader.pid, None)
                for pid in foreign:
                    self.owned.pop(pid, None)

    # (e) -----------------------------------------------------------------
    def _live_close(self, mode: str, grace: float):
        leader, log, foreign = self.foreign_session()
        spy = SignalSpy(self)
        pane = OmpPane(PaneId.MANAGER_OMP, "manager", [sys.executable, "-c", LIVE, mode], {"PATH": "/usr/bin:/bin"})
        self.owned[pane.pid] = str(pane.ref.start_ticks)
        self.assertTrue(wait_until(lambda: len(session_members(pane.pid)) == 3, 10, pane), "live fixture not up")
        members = session_members(pane.pid)
        self.owned.update(members)
        groups = {int(stat(p)[2]) for p in members}
        self.assertEqual(len(groups), 2, "fixture must span two process groups")
        result = pane.close(grace=grace)
        self.assertTrue(wait_until(lambda: not any(alive(p, s) for p, s in members.items()), 3))
        spy.restore()
        self.assertEqual({p: s for p, s in members.items() if alive(p, s)}, {}, "live session survived close")
        self.assertEqual(session_members(pane.pid), {})
        self.assertIsNotNone(pane.returncode)
        self.assertIsNone(stat(pane.pid), "leader not reaped")
        self.assert_foreign_untouched(spy, log, foreign, leader.pid)
        for call in spy.calls:
            if call["fn"] == "killpg":
                self.assertEqual((call["target"], call["leader_start"]), (pane.ref.pid, str(pane.ref.start_ticks)))
        return pane, spy, result

    def test_e_live_close_terms_group_and_kills_stubborn_session(self):
        pane, spy, result = self._live_close("stubborn", 0.4)
        self.assertTrue(any(c["fn"] == "killpg" and c["signal"] == signal.SIGTERM for c in spy.calls), spy.calls)
        self.assertEqual(result["exit_status"], -signal.SIGKILL)

    def test_e_live_close_cooperative_leader_gets_term(self):
        pane, spy, result = self._live_close("polite", 3.0)
        self.assertEqual(result["exit_status"], -signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
