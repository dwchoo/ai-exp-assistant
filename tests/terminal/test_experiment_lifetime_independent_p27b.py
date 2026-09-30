"""Independent p2.7 deadline-fix verification (p27-deadline-test-01), PersistentShell level.

Expected outcomes are derived from the contracts before reading the fix:

- BRIEF 제약 "실험 실행 시간 상한 없음"; C-AC-07 "경과 시간만으로 실험을 종료하지
  않는다"; OPERATING-CONTRACT §3 "이 대기 간격은 실험 실행 시간 상한이 아니다".
  -> a healthy experiment running > 90 s with default arguments is never killed
  or marked unknown and ends with its real exit status (0 or failing).
- C-AC-08 example "실험 주 프로그램은 끝났지만 후손이 생존" and §3 (main return vs.
  all managed descendants): lifetime is not complete while any managed
  descendant (background, double-forked, setsid) lives; it completes after they
  exit; unknown only for real observation loss (supervisor disappearance).
- C-AC-34 / §3 stop: an explicit stop (PersistentShell.close) still ends an
  unbounded run and its descendants; nothing is replayed afterwards.
- The launch handshake stays bounded with a visible reason and no residue.
- §3 input-return barrier: an observer that is slow between collect cycles is
  not a supervisor failure; release and close both work from the barrier.
- C-AC-32: the Workbench shell-init ENV never reaches user/experiment env.

Every test owns its shells, temp directories and processes. Process identity is
(pid, /proc start time); leftovers are killed by pidfd with a start-time recheck
and reported as failures.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import signal
import tempfile
import time
import unittest
from uuid import uuid4

from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState
from workbench.terminal.shell_persistent.adapter import PersistentShell

SHELLS = (ShellChoice("bash", "/usr/bin/bash"), ShellChoice("sh", "/usr/bin/dash"))


# -- process identity -------------------------------------------------------------
def stat(pid: int) -> list[str] | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def alive(pid: int, start: str) -> bool:
    fields = stat(pid)
    return fields is not None and fields[19] == start and fields[0] not in {"Z", "X"}


def session_members(session: int) -> dict[int, str]:
    found = {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            fields = stat(int(name))
            if fields and int(fields[3]) == session and fields[0] not in {"Z", "X"}:
                found[int(name)] = fields[19]
    return found


def tree(root: int) -> dict[int, str]:
    """root and all live descendants by ppid (crosses setsid sessions)."""
    parents: dict[int, list[int]] = {}
    starts: dict[int, str] = {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            fields = stat(int(name))
            if fields and fields[0] not in {"Z", "X"}:
                parents.setdefault(int(fields[1]), []).append(int(name))
                starts[int(name)] = fields[19]
    found, queue = {}, [root]
    while queue:
        pid = queue.pop()
        if pid in starts and pid not in found:
            found[pid] = starts[pid]
            queue.extend(parents.get(pid, []))
    return found


def signal_exact(pid: int, start: str, signum: int) -> bool:
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        if not alive(pid, start):
            return False
        signal.pidfd_send_signal(descriptor, signum)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(descriptor)


def ports(shell: PersistentShell, request_id: str | None = None):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": request_id or str(uuid4()), "approvalHash": "e" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


def q(path: Path) -> str:
    return shlex.quote(str(path))


class Case(unittest.TestCase):
    def setUp(self):
        self.tracked: dict[int, str] = {}
        self.tracked_by: dict[int, dict[int, str]] = {}  # parent shell pid -> its processes

    def open(self, choice: ShellChoice, extra: dict[str, str] | None = None) -> tuple[PersistentShell, Path]:
        directory = Path(tempfile.mkdtemp(prefix="p27b-"))
        self.addCleanup(shutil.rmtree, directory, True)
        env = {"PATH": "/usr/bin:/bin", "TERM": "xterm", "HOME": str(directory), "LANG": "C.UTF-8"}
        env.update(extra or {})
        shell = PersistentShell(user_environment=env, choice=choice)
        init_dir = Path(shell._transport._init_dir.name)
        self.addCleanup(self.close_verified, shell, shell.parent_pid, init_dir)
        return shell, directory

    def close_verified(self, shell: PersistentShell, parent: int, init_dir: Path) -> None:
        shell.close()
        deadline = time.monotonic() + 4
        def leftovers():
            left = dict(session_members(parent))
            left.update({p: s for p, s in self.tracked_by.get(parent, {}).items() if alive(p, s)})
            return left
        while leftovers() and time.monotonic() < deadline:
            time.sleep(0.02)
        left = leftovers()
        for pid, start in left.items():
            signal_exact(pid, start, signal.SIGKILL)
        self.assertEqual(left, {}, "processes survived PersistentShell.close")
        self.assertFalse(init_dir.exists(), f"shell-init temp dir left behind: {init_dir}")

    def track(self, shell: PersistentShell) -> dict[int, str]:
        supervisor = shell.snapshot()["lifecycle"]["supervisor_pid"]
        found = tree(supervisor) if supervisor else {}
        self.tracked.update(found)
        self.tracked_by.setdefault(shell.parent_pid, {}).update(found)
        return found

    def until(self, shell, condition, seconds: float = 10.0, what: str = "") -> dict:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = shell.poll(0.02)
            shell.display_bytes()
            if condition(state):
                return state
        raise AssertionError({"timeout": what, "state": shell.snapshot(),
                              "events": shell._transport.events[-15:]})

    @staticmethod
    def pump(shells, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for shell in shells:
                shell.poll(0.01)
                shell.display_bytes()

    def handoff(self, shell: PersistentShell) -> None:
        shell.send_user(b"wb-handoff\r")
        self.until(shell, lambda s: s["parent_mode"] == "control_wait", what="control_wait")
        shell.claim_manager()

    def submit(self, shell: PersistentShell, script, **kwargs) -> str:
        control, automation = ports(shell)
        shell.submit(control, script, automation, **kwargs)  # production default unless kwargs
        return control["payload"]["requestId"]

    def finish(self, shell: PersistentShell, status: int) -> dict:
        state = self.until(shell, lambda s: s["lifecycle"]["input_barrier"] or s["phase"] == "unknown",
                           15, "input_barrier")
        life = state["lifecycle"]
        self.assertEqual((life["main_exit"], life["lifetime"], life["unknown"]), (status, "ended", []), life)
        shell.release_input()
        state = self.until(shell, lambda s: s["lifecycle"]["control_returned"], what="control_returned")
        self.assertEqual((state["lifecycle"]["controller_exit"], state["phase"]), (status, "control_returned"))
        self.assertNotIn("UNKNOWN:SUPERVISOR_FAILURE", shell._transport.events)
        return state


class UnboundedRuntimeTests(Case):
    def test_default_submit_runs_past_90s_with_real_status_and_long_descendant(self):
        """bash and dash, exit 0 / exit 7 / main returns 4 with a 93 s descendant."""
        cases = []
        began = time.monotonic()
        for choice in SHELLS:
            for label, status in (("ok", 0), ("fail", 7), ("desc", 4)):
                shell, d = self.open(choice)
                self.handoff(shell)
                marks = {k: d / k for k in ("bg", "dfork", "setsid", "starts")}
                if label == "desc":
                    detached = shlex.quote(f"sleep 25; printf X > {q(marks['setsid'])}")
                    script = (f"printf S >> {q(marks['starts'])}; "
                              f"(sleep 93; printf D > {q(marks['bg'])}) & "
                              f"( (sleep 20; printf F > {q(marks['dfork'])}) & ); "
                              f"setsid sh -c {detached} & "
                              f"sleep 1; exit {status}")
                else:
                    script = f"printf S >> {q(marks['starts'])}; sleep 92; exit {status}"
                self.submit(shell, script)
                cases.append((f"{choice.kind}:{label}", shell, label, status, marks))
        main_ids = {}
        for name, shell, label, _, _ in cases:
            state = self.until(shell, lambda s: s["lifecycle"]["experiment_started"], what=name)
            if label != "desc":
                child = state["lifecycle"]["child_pid"]
                main_ids[name] = (child, stat(child)[19])
        long_descendants = {}
        while time.monotonic() - began < 91:
            self.pump([c[1] for c in cases], 1.0)
            elapsed = time.monotonic() - began
            for name, shell, label, status, marks in cases:
                life = shell.snapshot()["lifecycle"]
                self.assertEqual(life["unknown"], [], f"{name} unknown after {elapsed:.0f}s")
                self.assertFalse(life["input_barrier"], f"{name} barrier after {elapsed:.0f}s")
                self.assertNotEqual(life["lifetime"], "ended", f"{name} ended after {elapsed:.0f}s")
                if label != "desc":
                    self.assertIsNone(life["main_exit"], f"{name} main ended after {elapsed:.0f}s")
                    self.assertTrue(alive(*main_ids[name]), f"{name} main killed after {elapsed:.0f}s")
                else:
                    if life["main_exit"] is not None:
                        self.assertEqual(life["main_exit"], status)
                        if name not in long_descendants and elapsed > 3:
                            long_descendants[name] = {p: s for p, s in self.track(shell).items()}
                    if name in long_descendants and elapsed > 30:
                        self.assertTrue(marks["dfork"].exists() and marks["setsid"].exists(),
                                        f"{name} short descendants killed")
                        self.assertFalse(marks["bg"].exists())
            for name, shell, *_ in cases:
                self.track(shell)
        for name, shell, label, status, marks in cases:
            with self.subTest(case=name):
                state = self.finish(shell, status)
                self.assertGreaterEqual(time.monotonic() - began, 92)
                self.assertEqual(marks["starts"].read_text(), "S", "experiment replayed")
                if label == "desc":
                    self.assertEqual((marks["bg"].read_text(), marks["dfork"].read_text(),
                                      marks["setsid"].read_text()), ("D", "F", "X"))
                    self.assertTrue(state["lifecycle"]["wait_empty"])


class DescendantLifetimeTests(Case):
    def descendant_script(self, kind: str, mark: Path) -> str:
        return {
            "background": f"(sleep 18; printf B > {q(mark)}) & exit 0",
            "double_fork": f"( (sleep 18; printf F > {q(mark)}) & ); exit 0",
            "setsid": f"setsid sh -c {shlex.quote(f'sleep 18; printf S > {q(mark)}')} & sleep 1; exit 0",
        }[kind]

    def test_main_return_with_one_live_managed_descendant_is_not_complete_until_it_exits(self):
        cases = []
        for choice in SHELLS:
            for kind in ("background", "double_fork", "setsid"):
                shell, d = self.open(choice)
                self.handoff(shell)
                mark = d / "descendant"
                self.submit(shell, self.descendant_script(kind, mark))
                cases.append((f"{choice.kind}:{kind}", shell, mark))
        watched = {}
        for name, shell, _ in cases:
            state = self.until(shell, lambda s: s["lifecycle"]["main_exit"] is not None, what=name)
            self.assertEqual((state["lifecycle"]["main_exit"], state["lifecycle"]["unknown"]), (0, []))
        self.pump([c[1] for c in cases], 1.5)
        for name, shell, _ in cases:
            found = self.track(shell)
            supervisor = shell.snapshot()["lifecycle"]["supervisor_pid"]
            sleeps = {p: s for p, s in found.items() if p != supervisor}
            self.assertTrue(sleeps, f"{name}: managed descendant not under the supervisor")
            watched[name] = sleeps
        pending = {name for name, *_ in cases}
        deadline = time.monotonic() + 40
        while pending and time.monotonic() < deadline:
            self.pump([c[1] for c in cases], 0.25)
            for name, shell, mark in cases:
                if name not in pending:
                    continue
                life = shell.snapshot()["lifecycle"]
                self.assertEqual(life["unknown"], [], name)
                living = any(alive(p, s) for p, s in watched[name].items())
                if living:
                    self.assertNotEqual(life["lifetime"], "ended", f"{name}: ended while descendant lives")
                    self.assertFalse(life["input_barrier"], name)
                elif life["input_barrier"]:
                    pending.discard(name)
        self.assertEqual(pending, set(), "lifetime never ended after descendants exited")
        for name, shell, mark in cases:
            with self.subTest(case=name):
                self.assertTrue(mark.exists(), f"{name}: descendant killed before finishing")
                self.finish(shell, 0)

    def test_supervisor_loss_is_unknown_and_never_completion(self):
        """Real observation loss (exact-identity SIGKILL of the supervisor)."""
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                self.handoff(shell)
                sentinel = d / "sentinel"
                self.submit(shell, "(sleep 30) & exit 0")
                self.until(shell, lambda s: s["lifecycle"]["lifetime"] == "active", what="active")
                self.pump([shell], 2.0)
                self.assertEqual(shell.snapshot()["lifecycle"]["unknown"], [])
                found = self.track(shell)
                supervisor = shell.snapshot()["lifecycle"]["supervisor_pid"]
                self.assertTrue(signal_exact(supervisor, found[supervisor], signal.SIGKILL))
                state = self.until(shell, lambda s: s["phase"] == "unknown", what="unknown")
                self.assertIn("supervisor_disappeared", state["lifecycle"]["unknown"])
                self.assertNotEqual(state["lifecycle"]["lifetime"], "ended")
                self.assertFalse(state["lifecycle"]["input_barrier"])
                self.assertIn("unknown_or_manual_residue", state["held_reasons"])
                self.assertTrue(shell.manual_input_hold())
                control, automation = ports(shell)
                with self.assertRaises(UnsafeShellState):
                    shell.submit(control, f"printf R > {q(sentinel)}", automation)
                self.assertFalse(sentinel.exists())


class ExplicitStopTests(Case):
    TREE = "sleep 1000 & ( (sleep 1000) & ); setsid sleep 1000 & "

    def run_and_close(self, choice: ShellChoice, tail: str, phase_ready) -> None:
        shell, d = self.open(choice)
        self.handoff(shell)
        starts = d / "starts"
        self.submit(shell, f"printf S >> {q(starts)}; {self.TREE}{tail}")
        self.until(shell, phase_ready, what="phase")
        self.pump([shell], 6.5)  # beyond the former 5 s main/descendant bounds
        life = shell.snapshot()["lifecycle"]
        self.assertEqual(life["unknown"], [])
        self.assertNotEqual(life["lifetime"], "ended")
        found = self.track(shell)
        sleeps = [p for p in found if Path(f"/proc/{p}/comm").read_text().strip() == "sleep"]
        self.assertGreaterEqual(len(sleeps), 3 if tail.startswith("exit") else 4, found)
        began = time.monotonic()
        shell.close()
        deadline = time.monotonic() + 4
        while any(alive(p, s) for p, s in found.items()) and time.monotonic() < deadline:
            time.sleep(0.02)
        survivors = {p: s for p, s in found.items() if alive(p, s)}
        self.assertEqual(survivors, {}, "explicit stop left managed descendants")
        self.assertLess(time.monotonic() - began, 6)
        control, automation = ports(shell)  # snapshot of the closed shell
        with self.assertRaises((UnsafeShellState, OSError, ValueError)):  # no new request, no replay
            shell.submit(control, f"printf S >> {q(starts)}", automation)
        time.sleep(1.0)
        self.assertEqual(starts.read_text(), "S", "experiment replayed after stop")

    def test_close_ends_running_unbounded_run_and_all_descendants(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                self.run_and_close(choice, "exec sleep 1000",
                                   lambda s: s["lifecycle"]["experiment_started"])

    def test_close_ends_descendant_phase_after_main_return(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                self.run_and_close(choice, "exit 0",
                                   lambda s: s["lifecycle"]["lifetime"] == "active")


class StartHandshakeTests(Case):
    """Launch holds are reachable only through the transport's probe seam:
    PersistentShell.submit passes literal argv, never fixture directives."""

    def held_launch(self, choice: ShellChoice, directive: str, *, exec_ready: bool) -> None:
        shell, d = self.open(choice)
        self.handoff(shell)
        marker, sentinel = d / "payload-ran", d / "sentinel"
        began = time.monotonic()
        shell._transport.dispatch_managed(str(uuid4()), f"{directive}\nprintf X > {q(marker)}")
        self.until(shell, lambda s: s["lifecycle"]["supervisor_pid"], what="supervisor")
        found = self.track(shell)
        state = self.until(shell, lambda s: s["phase"] == "unknown", 12, "bounded start failure")
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 5.0 + 3.0, "start handshake not bounded by the 5 s default")
        life = state["lifecycle"]
        self.assertIn("unknown:SUPERVISOR_FAILURE", life["unknown"])
        self.assertFalse(life["experiment_started"])
        self.assertEqual(life["exec_ready"], exec_ready)
        self.assertIn("unknown_or_manual_residue", state["held_reasons"])
        # Visible to the user after takeover (manager held the input before).
        shell.request_takeover()
        reason = shell.manual_input_hold()
        self.assertTrue(reason and "unknown" in reason, reason)
        self.track(shell)
        deadline = time.monotonic() + 4
        mine = self.tracked_by[shell.parent_pid]
        while any(alive(p, s) for p, s in mine.items()) and time.monotonic() < deadline:
            self.pump([shell], 0.05)
        self.assertEqual({p: s for p, s in mine.items() if alive(p, s)}, {},
                         "failed launch left supervisor/child residue")
        self.assertEqual(set(session_members(shell.parent_pid)), {shell.parent_pid})
        control, automation = ports(shell)
        with self.assertRaises(UnsafeShellState):
            shell.submit(control, f"printf R > {q(sentinel)}", automation)
        self.pump([shell], 0.5)
        self.assertFalse(marker.exists(), "held launch payload ran")
        self.assertFalse(sentinel.exists())
        self.assertTrue(found)

    def test_pre_fork_launch_hold_fails_bounded_without_residue(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                self.held_launch(choice, "#WB_START_HOLD", exec_ready=False)

    def test_post_exec_identity_hold_fails_bounded_without_residue(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                self.held_launch(choice, "#WB_CHILD_ID_HOLD", exec_ready=True)

    def test_short_start_timeout_never_bounds_the_run(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, _ = self.open(choice)
                self.handoff(shell)
                self.submit(shell, "sleep 4; exit 3", start_timeout=1)
                self.finish(shell, 3)


class InputBarrierTests(Case):
    def ended_unobserved(self, choice: ShellChoice) -> tuple[PersistentShell, dict]:
        shell, _ = self.open(choice)
        self.handoff(shell)
        self.submit(shell, "sleep 1; exit 3")
        self.until(shell, lambda s: s["lifecycle"]["experiment_started"], what="started")
        supervisor = shell.snapshot()["lifecycle"]["supervisor_pid"]
        identity = (supervisor, stat(supervisor)[19])
        self.track(shell)
        time.sleep(12.0)  # no collect cycle: the run ends meanwhile (> old 5 s release bound)
        state = shell.poll(0.05)
        shell.display_bytes()
        life = state["lifecycle"]
        self.assertEqual((life["main_exit"], life["lifetime"], life["input_barrier"], life["unknown"]),
                         (3, "ended", True, []), shell._transport.events[-10:])
        self.assertNotIn("UNKNOWN:SUPERVISOR_FAILURE", shell._transport.events)
        self.assertNotEqual(state["phase"], "unknown")
        self.assertTrue(alive(*identity), "supervisor gave up waiting at the barrier")
        return shell, state

    def test_barrier_reached_between_cycles_then_release(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, _ = self.ended_unobserved(choice)
                shell.release_input()
                state = self.until(shell, lambda s: s["lifecycle"]["control_returned"], what="returned")
                self.assertEqual((state["lifecycle"]["controller_exit"], state["lifecycle"]["unknown"]), (3, []))

    def test_barrier_reached_between_cycles_then_close(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, _ = self.ended_unobserved(choice)
                shell.close()
                deadline = time.monotonic() + 4
                mine = self.tracked_by[shell.parent_pid]
                while any(alive(p, s) for p, s in mine.items()) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual({p: s for p, s in mine.items() if alive(p, s)}, {})


class ShellInitEnvironmentTests(Case):
    """C-AC-32: no environment value of user or experiment names the Workbench init."""

    def environ(self, pid: int) -> dict[str, str]:
        raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        return dict(item.decode(errors="replace").partition("=")[::2] for item in raw if item)

    def check(self, choice: ShellChoice, user_env: str | None) -> None:
        extra = {} if user_env is None else {"ENV": user_env}
        shell, d = self.open(choice, extra)
        init_dir = shell._transport._init_dir.name
        manual = d / "manual-env"
        shell.send_user(f"env > {q(manual)}\r".encode())
        self.until(shell, lambda s: manual.exists() and manual.stat().st_size > 0
                   and s["parent_mode"] == "manual_prompt", what="manual env")
        self.handoff(shell)
        nested = d / "nested-env"
        self.submit(shell, f"sh -c {shlex.quote(f'env > {q(nested)}; exec sleep 3')} & exec sleep 3")
        state = self.until(shell, lambda s: s["lifecycle"]["experiment_started"], what="started")
        self.until(shell, lambda _: nested.exists() and nested.stat().st_size > 0, what="nested env")
        found = self.track(shell)
        experiment = self.environ(state["lifecycle"]["child_pid"])
        sleeps = [p for p in found if p != state["lifecycle"]["child_pid"]
                  and Path(f"/proc/{p}/comm").read_text().strip() == "sleep"]
        self.assertTrue(sleeps)
        descendant = self.environ(sleeps[0])
        texts = {"manual": manual.read_text(), "nested": nested.read_text(),
                 "experiment": "\n".join(f"{k}={v}" for k, v in experiment.items()),
                 "descendant": "\n".join(f"{k}={v}" for k, v in descendant.items())}
        for where, text in texts.items():
            self.assertNotIn(init_dir, text, f"{where} env names the Workbench init dir")
            self.assertNotIn("cw03-g2-", text, where)
            env_lines = [line for line in text.splitlines() if line.startswith("ENV=")]
            self.assertEqual(env_lines, [] if user_env is None else [f"ENV={user_env}"], where)
        self.finish(shell, 0)

    def test_dash_and_bash_user_and_experiment_env(self):
        existing = Path(tempfile.mkdtemp(prefix="p27b-envfile-"))
        self.addCleanup(shutil.rmtree, existing, True)
        (existing / "user.env").write_text(": user env file\n")
        for choice in SHELLS:
            for user_env in (None, str(existing / "user.env")):
                with self.subTest(shell=choice.kind, env=user_env):
                    self.check(choice, user_env)


if __name__ == "__main__":
    unittest.main()
