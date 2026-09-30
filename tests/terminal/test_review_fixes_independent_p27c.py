"""Independent verification of review fixes P2-1 and P2-3 (p27-review-fix-test-01), shell side.

Expectations derived from the review findings and contracts before reading the fix:

- P2-1 / BRIEF "실험 실행 시간 상한 없음" / C-AC-08 example "주 프로그램은 끝났지만
  후손이 생존" / SPEC "관리된 shell과 인수" (subreaper, wait scope): a main program
  that runs > 60 s and keeps creating double-forked and setsid orphans must not
  make the supervisor accumulate zombies (count stays bounded at every sample,
  not only at the end); every orphan is still observed (reaped) as a managed
  descendant; the lifetime is not complete while a live descendant remains;
  nothing is killed or marked unknown because of elapsed time.
- P2-3 / C-AC-32 / C-AC-03: the Workbench's own (Python) SIG_IGN of SIGPIPE and
  SIGXFSZ must not leak into the user's environment: host shell, user-launched
  children, the experiment and all its descendants start with default
  SIGPIPE/SIGXFSZ (not ignored, not blocked). Observable consequence: in
  ``yes | head -1`` the writer dies by SIGPIPE (status 141) promptly, and a
  write past ``ulimit -f`` kills the writer by SIGXFSZ (status 153).

Every test owns its shells, temp directories and processes; identity is
(pid, /proc start time); leftovers are killed by pidfd with a start-time
recheck and reported as failures.
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

from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell

SHELLS = (ShellChoice("bash", "/usr/bin/bash"), ShellChoice("sh", "/usr/bin/dash"))
PIPE_XFSZ = (1 << (signal.SIGPIPE - 1)) | (1 << (signal.SIGXFSZ - 1))


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


def children_states(pid: int) -> list[str]:
    try:
        kids = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except OSError:
        return []
    return [s[0] for s in (stat(int(k)) for k in kids) if s]


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


def masks(text: str) -> dict[str, int]:
    return {line.split(":")[0]: int(line.split()[1], 16) for line in text.splitlines()
            if line.startswith(("SigIgn:", "SigBlk:"))}


def q(path: Path) -> str:
    return shlex.quote(str(path))


def ports(shell: PersistentShell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "c" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


class Case(unittest.TestCase):
    def setUp(self):
        self.tracked_by: dict[int, dict[int, str]] = {}

    def open(self, choice: ShellChoice) -> tuple[PersistentShell, Path]:
        directory = Path(tempfile.mkdtemp(prefix="p27c-"))
        self.addCleanup(shutil.rmtree, directory, True)
        env = {"PATH": "/usr/bin:/bin", "TERM": "xterm", "HOME": str(directory), "LANG": "C.UTF-8"}
        shell = PersistentShell(user_environment=env, choice=choice)
        init_dir = Path(shell._transport._init_dir.name)
        self.addCleanup(self.close_verified, shell, shell.parent_pid, init_dir)
        return shell, directory

    def close_verified(self, shell: PersistentShell, parent: int, init_dir: Path) -> None:
        shell.close()
        deadline = time.monotonic() + 5

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

    def handoff(self, shell: PersistentShell) -> None:
        shell.send_user(b"wb-handoff\r")
        self.until(shell, lambda s: s["parent_mode"] == "control_wait", what="control_wait")
        shell.claim_manager()

    def finish(self, shell: PersistentShell, status: int, seconds: float = 20) -> dict:
        state = self.until(shell, lambda s: s["lifecycle"]["input_barrier"] or s["phase"] == "unknown",
                           seconds, "input_barrier")
        life = state["lifecycle"]
        self.assertEqual((life["main_exit"], life["lifetime"], life["unknown"]), (status, "ended", []), life)
        shell.release_input()
        state = self.until(shell, lambda s: s["lifecycle"]["control_returned"], what="control_returned")
        self.assertEqual(state["lifecycle"]["controller_exit"], status)
        self.assertNotIn("UNKNOWN:SUPERVISOR_FAILURE", shell._transport.events)
        return state


class OrphanReapingDuringLongMainTests(Case):
    ITERATIONS = 124  # ~62 s of main runtime at 0.5 s per iteration
    PER_ITERATION = 8

    def script(self, d: Path) -> str:
        late2 = shlex.quote(f"sleep 22; printf S > {q(d / 'late2')}")
        return (
            f"i=0; while [ $i -lt {self.ITERATIONS} ]; do "
            "j=0; while [ $j -lt 4 ]; do ( sleep 0.2 & ); ( setsid sleep 0.2 & ); j=$((j+1)); done; "
            f"i=$((i+1)); printf '%s\\n' $i > {q(d / 'iter')}; "
            f"if [ $i -eq 60 ]; then ( (sleep 12; printf M > {q(d / 'mid')}) & ); fi; "
            "sleep 0.5; done; "
            f"( (sleep 20; printf L > {q(d / 'late')}) & ); "
            f"( setsid sh -c {late2} & ); "
            "exit 5")

    def test_long_main_spawning_orphans_keeps_zombies_bounded_and_tracks_descendants(self):
        cases = []
        for choice in SHELLS:
            shell, d = self.open(choice)
            self.handoff(shell)
            control, automation = ports(shell)
            shell.submit(control, self.script(d), automation)
            cases.append({"name": choice.kind, "shell": shell, "dir": d, "max_z": 0, "samples": 0,
                          "z_trace": [], "main_seen": None, "late_live_seen": False})
        began = time.monotonic()
        for case in cases:
            state = self.until(case["shell"], lambda s: s["lifecycle"]["experiment_started"], what=case["name"])
            case["supervisor"] = state["lifecycle"]["supervisor_pid"]
            case["child"] = (state["lifecycle"]["child_pid"], stat(state["lifecycle"]["child_pid"])[19])
        pending = {c["name"] for c in cases}
        last_sample = 0.0
        while pending and time.monotonic() - began < 140:
            for case in cases:
                case["shell"].poll(0.01)
                case["shell"].display_bytes()
            now = time.monotonic()
            if now - last_sample < 0.2:
                continue
            last_sample = now
            elapsed = now - began
            for case in cases:
                if case["name"] not in pending:
                    continue
                shell, d = case["shell"], case["dir"]
                life = shell.snapshot()["lifecycle"]
                self.assertEqual(life["unknown"], [], f"{case['name']} unknown at {elapsed:.0f}s")
                states = children_states(case["supervisor"])
                zombies = states.count("Z")
                case["samples"] += 1
                case["max_z"] = max(case["max_z"], zombies)
                case["z_trace"].append((round(elapsed, 1), zombies))
                self.assertLessEqual(zombies, 24, f"{case['name']}: {zombies} zombie orphans at {elapsed:.0f}s; "
                                                  f"trace tail {case['z_trace'][-10:]}")
                self.track(shell)
                if life["main_exit"] is None:
                    # A main that is gone before MAIN_RETURN was observed must
                    # have finished its own loop (exit 5 is checked below).
                    finished = (d / "iter").exists() and (d / "iter").read_text().strip() == str(self.ITERATIONS)
                    self.assertTrue(alive(*case["child"]) or finished,
                                    f"{case['name']} main killed at {elapsed:.0f}s")
                    self.assertNotEqual(life["lifetime"], "ended")
                    continue
                if case["main_seen"] is None:
                    case["main_seen"] = elapsed
                    self.assertEqual(life["main_exit"], 5)
                    self.assertGreater(elapsed, 60, f"{case['name']} main ended early")
                    self.assertEqual((d / "iter").read_text().strip(), str(self.ITERATIONS))
                    self.assertEqual((d / "mid").read_text(), "M", "mid-run orphan killed")
                late_done = (d / "late").exists() and (d / "late2").exists()
                if not late_done:
                    live = [s for s in states if s not in {"Z", "X"}]
                    if live:
                        case["late_live_seen"] = True
                    self.assertFalse(life["input_barrier"], f"{case['name']}: barrier while descendants live")
                    self.assertNotEqual(life["lifetime"], "ended",
                                        f"{case['name']}: lifetime ended while descendants live")
                elif life["input_barrier"]:
                    pending.discard(case["name"])
        self.assertEqual(pending, set(), f"lifetime never ended: {[(c['name'], c['z_trace'][-5:]) for c in cases]}")
        for case in cases:
            with self.subTest(shell=case["name"]):
                shell, d = case["shell"], case["dir"]
                self.assertTrue(case["late_live_seen"], "late descendants never observed under the supervisor")
                self.assertEqual(((d / "late").read_text(), (d / "late2").read_text()), ("L", "S"))
                state = self.finish(shell, 5)
                self.assertTrue(state["lifecycle"]["wait_empty"])
                reaped = [e for e in shell._transport.events if e.startswith("DESCENDANT_REAPED:")]
                expected = self.ITERATIONS * self.PER_ITERATION + 3
                self.assertGreaterEqual(len(reaped), expected, "orphans not observed as managed descendants")
                self.assertGreater(case["samples"], 300)
                print(f"\n[p27c P2-1 {case['name']}] samples={case['samples']} max_zombies={case['max_z']} "
                      f"reaped_events={len(reaped)} main_return_at={case['main_seen']:.1f}s")


class DefaultPipeXfszTests(Case):
    def setUp(self):
        super().setUp()
        # The Workbench/test Python process ignores both; the check is only
        # meaningful if that disposition exists to be (not) inherited.
        self.assertIs(signal.getsignal(signal.SIGPIPE), signal.SIG_IGN)
        self.assertIs(signal.getsignal(signal.SIGXFSZ), signal.SIG_IGN)

    @staticmethod
    def probe(d: Path, tag: str) -> str:
        """Status of this process, `yes | head -1` writer status + duration, and an XFSZ write."""
        return (f"cat /proc/$$/status > {q(d / (tag + '.status'))}; "
                f"s=$(date +%s%N); (yes; echo $? > {q(d / (tag + '.pipe'))}) | head -1 > /dev/null; "
                f"e=$(date +%s%N); echo $(( (e - s) / 1000000 )) > {q(d / (tag + '.ms'))}; "
                f"(ulimit -f 1; head -c 65536 /dev/zero > {q(d / (tag + '.big'))}; "
                f"echo $? > {q(d / (tag + '.xfsz'))}) 2>/dev/null")

    def wait_files(self, shell, paths: list[Path], what: str, seconds: float = 15) -> None:
        self.until(shell, lambda _: all(p.exists() and p.stat().st_size > 0 for p in paths), seconds, what)

    def check(self, d: Path, tag: str) -> None:
        text = (d / (tag + ".status")).read_text()
        values = masks(text)
        self.assertEqual(values["SigIgn"] & PIPE_XFSZ, 0, f"{tag} ignores SIGPIPE/SIGXFSZ: {values}")
        self.assertEqual(values["SigBlk"] & PIPE_XFSZ, 0, f"{tag} blocks SIGPIPE/SIGXFSZ: {values}")
        if (d / (tag + ".pipe")).exists():
            self.assertEqual((d / (tag + ".pipe")).read_text().strip(), "141", f"{tag}: yes not killed by SIGPIPE")
            self.assertLess(int((d / (tag + ".ms")).read_text()), 5000, f"{tag}: yes | head -1 slow")
            self.assertEqual((d / (tag + ".xfsz")).read_text().strip(), "153", f"{tag}: writer not killed by SIGXFSZ")

    def test_shell_user_children_experiment_and_descendants_have_default_pipe_xfsz(self):
        for choice in SHELLS:
            with self.subTest(shell=choice.kind):
                shell, d = self.open(choice)
                # Host shell itself, its interactive pipeline, and a user-launched child.
                child = shlex.quote(self.probe(d, "user_child"))
                shell.send_user(f"{self.probe(d, 'shell')}; sh -c {child}\r".encode())
                self.wait_files(shell, [d / "shell.xfsz", d / "user_child.xfsz"], "manual probes")
                self.until(shell, lambda s: s["parent_mode"] == "manual_prompt", what="prompt")
                status_shell = masks(Path(f"/proc/{shell.parent_pid}/status").read_text())
                self.assertEqual(status_shell["SigIgn"] & PIPE_XFSZ, 0, status_shell)
                # Experiment plus background, double-forked and setsid descendants.
                self.handoff(shell)
                descendant = {k: shlex.quote(self.probe(d, k)) for k in ("bg", "dfork", "setsid")}
                script = (f"{self.probe(d, 'experiment')}; "
                          f"sh -c {descendant['bg']} & "
                          f"( sh -c {descendant['dfork']} & ); "
                          f"( setsid sh -c {descendant['setsid']} & ); "
                          "wait; sleep 1; exit 0")
                began = time.monotonic()
                control, automation = ports(shell)
                shell.submit(control, script, automation)
                self.until(shell, lambda s: s["lifecycle"]["experiment_started"], what="started")
                self.track(shell)
                self.finish(shell, 0)
                self.assertLess(time.monotonic() - began, 15, "experiment with yes | head -1 did not end promptly")
                for tag in ("shell", "user_child", "experiment", "bg", "dfork", "setsid"):
                    self.check(d, tag)


if __name__ == "__main__":
    unittest.main()
