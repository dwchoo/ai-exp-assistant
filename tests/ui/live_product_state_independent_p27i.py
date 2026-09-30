"""Independent CW-12/CW-15 delta check of the CW-06 product UI state items (p27-cw1215-delta-test-01).

Expectations were derived from PLAN gate items P-C-AC-15/18/30 (+ CW-06/CW-15 tickets, C-D52) before
the implementation was read:

- C-AC-15 (UI part): quitting the product UI is only a detach. Both OMP processes and the host shell (and a
  host experiment started from it) keep the same identity (pid + start ticks); reattaching shows the same
  panes; the backend does not start a model run on its own (scripted counting provider stays at 0 requests).
- C-AC-18 (UI part): the host input owner / control mode / pending takeover request survive a detach and a
  reattach unchanged; nothing is sent to the host shell by the UI or backend on its own (no replayed input, no
  automatic handoff); the user can still take over explicitly afterwards; an abrupt UI kill is equal to detach.
- C-AC-30 (UI part, what is exercisable before CW-18 wiring): the automation state text is a backend-provided
  value, unaffected by focus changes, manual host input or takeover/handoff, and it survives reattach.
  (The pause status rows of CW-12 -- last work, confirmed changes, unknown tool results -- are not in
  ui_v1/the product UI yet; see the module-level PENDING_SUPPLIERS note recorded by the report.)
- No real model turns: real OMP 18.2.10 only ever talks to a local counting provider that answers 503; the
  provider request counter must stay 0. Every process/dir is owned and removed by exact identity.

Run: PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/ui -p live_product_state_independent_p27i.py -v
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import sys
import time
import unittest

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE.parent / "backend"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from independent_support import (  # noqa: E402
    LiveBackend, find_omp, identity, kill_exact, residue, session_members, shell_view, stop_and_verify, ticks,
)
import independent_support_cw06 as S  # noqa: E402
from live_product_omp_independent import LiveTerm, PREFIX, termios_flags  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402

OMP = find_omp()
PANES = ("manager_omp", "worker_omp", "host_shell")
IDLE_SECONDS = 9  # detached idle window: state must not drift (the 63 s window is owned by tests/backend)


class OwnedTerm(LiveTerm):
    """LiveTerm whose close() kills by exact identity (pidfd + start ticks), never by process group."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.leader_ticks = ticks(self.sid)

    def close(self) -> None:
        if self.fd >= 0:
            members = session_members({self.sid})
            for pid, start in members.items():
                kill_exact(pid, start)
            try:
                self.process.wait(5)
            except Exception:  # noqa: BLE001 - timeout: the residue check below reports it
                pass
            os.close(self.fd)
            self.fd = -1


def footer(term: LiveTerm) -> str:
    return term.line(term.rows - 1).strip()[:170]


def header(term: LiveTerm) -> str:
    return term.line(0)


@unittest.skipUnless(OMP, "real OMP 18.2.10 is required (scripted 503 provider, no model turns)")
class ProductUiDetachReattachState(unittest.TestCase):
    """Real entrypoint (`python -m workbench start/attach`) + real product UI + backend-owned PTYs."""

    def setUp(self):
        self.live = LiveBackend(OMP, path="/usr/bin:/bin")
        self.live.env.update(SHELL="/bin/bash", TERM="xterm-256color", LANG="C.UTF-8")
        self.terms: list[OwnedTerm] = []
        self.observed: dict = {}
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for term in self.terms:
            term.close()
        left, _ = stop_and_verify(self.live)
        fallback = self.live.cleanup()  # exact-identity fallback + provider + temp root; reported below
        self.assertEqual(left, {}, f"owned process/socket leak after confirmed shutdown: {left} / {fallback}")
        self.assertFalse(self.live.root.exists(), "owned temp root left behind")

    # -- helpers ----------------------------------------------------------
    def term(self, name: str, *args: str) -> OwnedTerm:
        term = OwnedTerm(name, self.live.root, self.live.env, self.live.project, *args)
        self.terms.append(term)
        return term

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for term in self.terms:
                if term.fd >= 0:
                    term.drain(0.02)

    def wait_status(self, predicate, timeout: float = 25.0) -> dict:
        end = time.monotonic() + timeout
        snap = None
        while time.monotonic() < end:
            for term in self.terms:
                if term.fd >= 0:
                    term.drain(0.02)
            snap = self.live.status()
            if snap is not None and predicate(snap):
                return snap
            time.sleep(0.1)
        raise AssertionError(f"status predicate timeout: {snap and {k: snap.get(k) for k in ('phase', 'attached', 'focus')}}")

    def pane_alive(self, snap: dict) -> dict:
        return {name: (pane["process"]["pid"], pane["process"]["start_ticks"]) for name, pane in snap["panes"].items()}

    def start_ui(self) -> tuple[OwnedTerm, dict]:
        ui = self.term("start", *self.live.start_args())
        self.assertTrue(ui.wait(lambda: "bridge manager=ok worker=ok" in ui.text(), 120),
                        [ui.line(0)[:150], ui.line(1)[:150], footer(ui)])
        snap = self.wait_status(lambda s: s["phase"] == "ready" and s["attached"])
        self.live.remember(snap)
        return ui, snap

    def reattach(self, name: str) -> OwnedTerm:
        ui = self.term(name, "attach", "--data-dir", str(self.live.data))
        self.assertTrue(ui.wait(lambda: "backend: ready" in ui.text() and "HOST SHELL" in ui.text(), 30),
                        [header(ui)[:150], ui.line(1)[:150], footer(ui)])
        self.wait_status(lambda s: s["attached"])
        return ui

    def detach(self, ui: OwnedTerm) -> None:
        ui.send(PREFIX + b"d")
        self.assertTrue(ui.wait(ui.done, 20), footer(ui))
        self.assertEqual(ui.status(), 0, "clean detach must exit 0")
        flags = termios_flags((ui.work / "after_raw").read_text())
        self.assertTrue(flags["icanon"] and flags["echo"] and flags["isig"], f"outer terminal not restored: {flags}")

    def marker(self) -> str:
        path = self.live.project / "marker"
        return path.read_text() if path.exists() else ""

    def run_in_shell(self, ui: OwnedTerm, command: str, want: str | None = None, timeout: float = 10.0) -> None:
        before = self.marker()
        ui.send(command.encode() + b"\r")
        end = time.monotonic() + timeout
        while time.monotonic() < end and (self.marker() == before if want is None else self.marker() != want):
            ui.drain(0.05)
        self.assertEqual(self.marker(), want if want is not None else self.marker(),
                         f"footer={footer(ui)!r} shell={self.live.status()['panes']['host_shell']['shell']} "
                         f"pane={ui.pane(PaneId.HOST_SHELL)[-400:]!r}")

    # -- the scenario -----------------------------------------------------
    def test_detach_reattach_keeps_processes_owner_request_and_sends_nothing(self):
        P, live = self.live.project, self.live
        marker = P / "marker"
        ui, first = self.start_ui()

        # -- baseline: three areas, user owner, backend-provided automation text ------------------------------------
        ids0 = identity(first)
        self.assertEqual(shell_view(first)["input_owner"], "user")
        head = header(ui)
        self.assertIn("host 입력 owner: user", head)
        self.assertIn("자동화: ", head, "automation state is not displayed")
        automation0 = first["automation"]["state"]
        self.assertIn(f"자동화: {automation0}", head)
        for title in ("MANAGER OMP", "WORKER OMP", "HOST SHELL"):
            self.assertIn(title, ui.text())

        # -- C-AC-18: explicit handoff (manager owns), refused user input, then a takeover request --------------
        ui.send(PREFIX + b"3")
        self.wait_status(lambda s: s["focus"] == "host_shell")
        ui.pause(1.0)
        self.run_in_shell(ui, f"printf a >> {marker}", "a")
        self.assertEqual(automation0, self.wait_status(lambda s: True)["automation"]["state"],
                         "manual host input changed the automation state")
        ui.send(PREFIX + b"h")  # before wb-handoff the backend must refuse; nothing may change
        ui.pause(1.0)
        self.assertEqual(shell_view(live.status())["input_owner"], "user", footer(ui))
        ui.send(b"wb-handoff\r")
        ui.pause(2.0)
        ui.send(PREFIX + b"h")
        try:
            self.wait_status(lambda s: shell_view(s)["input_owner"] == "manager", 12)
        except AssertionError as exc:
            raise AssertionError(f"handoff did not reach the manager: footer={footer(ui)!r} "
                                 f"shell={live.status()['panes']['host_shell']['shell']}") from exc
        self.assertTrue(ui.wait(lambda: "host 입력 owner: manager" in header(ui), 5), header(ui)[:170])
        self.assertIn(f"자동화: {automation0}", header(ui), "automation text changed with the owner")
        ui.send(f"echo x >> {P}/SHOULD-NOT-RUN\r".encode())
        self.assertTrue(ui.wait(lambda: "input_owner_manager" in ui.text(), 5), footer(ui))
        ui.pause(0.5)
        self.assertFalse((P / "SHOULD-NOT-RUN").exists(), "input reached a manager-owned shell")
        ui.send(PREFIX + b"t")
        ui.pause(1.2)
        request_footer = footer(ui)
        before = live.status()
        view_before, ident_before = shell_view(before), identity(before)
        self.assertNotIn("거부", request_footer)
        self.assertEqual(ident_before["backend"], ids0["backend"])
        self.assertEqual(ident_before["shell_parent"], ids0["shell_parent"])
        marker_before = self.marker()
        alive_before = self.pane_alive(before)
        self.assertEqual(marker_before, "a")

        # -- C-AC-15/18: detach with the handoff/request state in flight ----------------------------------------
        self.detach(ui)
        idle_start = time.monotonic()
        detached = self.wait_status(lambda s: s["attached"] is False)
        self.assertEqual(self.pane_alive(detached), alive_before, "a pane process changed while detached")
        self.assertTrue(all(p["alive"] for p in detached["panes"].values()))
        self.assertEqual(shell_view(detached), view_before, "owner/mode/request state drifted at detach")
        self.assertEqual(detached["automation"]["state"], automation0)
        while time.monotonic() - idle_start < IDLE_SECONDS:
            time.sleep(0.5)
        idle = live.status()
        self.assertFalse(idle["attached"])
        self.assertEqual(identity(idle), identity(detached), "identity drifted while detached")
        self.assertEqual(shell_view(idle), view_before, "state drifted while detached")
        self.assertEqual(self.marker(), marker_before, "something was sent to the host shell while detached")
        self.assertFalse((P / "SHOULD-NOT-RUN").exists())

        # -- reattach: same owner/mode/request; the UI sends nothing on its own -------------------------------
        ui2 = self.reattach("re1")
        ui2.pause(2.0)
        after = live.status()
        self.assertEqual(shell_view(after), view_before, "owner/request state changed by reattach")
        self.assertEqual(identity(after)["owner_epoch"], ident_before["owner_epoch"])
        self.assertEqual(self.pane_alive(after), alive_before)
        self.assertIn(f"host 입력 owner: {view_before['input_owner']}", header(ui2))
        self.assertIn(f"shell mode: {view_before['parent_mode']}", header(ui2))
        self.assertIn(f"자동화: {automation0}", header(ui2))
        self.assertEqual(self.marker(), marker_before, "reattach replayed or sent input to the host shell")
        self.assertFalse((P / "SHOULD-NOT-RUN").exists(), "old refused input was replayed on reattach")
        self.assertIn("marker", ui2.pane(PaneId.HOST_SHELL), "host shell pane history was not restored")

        # -- explicit confirm is the only way back to the user; then manual input works exactly once ---------------
        ui2.send(PREFIX + b"3")
        self.wait_status(lambda s: s["focus"] == "host_shell")
        ui2.send(PREFIX + b"c")
        confirmed = self.wait_status(lambda s: shell_view(s)["input_owner"] == "user")
        self.assertTrue(ui2.wait(lambda: "host 입력 owner: user" in header(ui2), 5), header(ui2)[:170])
        self.assertGreaterEqual(shell_view(confirmed)["owner_epoch"], view_before["owner_epoch"])
        self.run_in_shell(ui2, f"printf b >> {marker}", "ab")
        ui2.pause(1.0)
        self.assertEqual(self.marker(), "ab", "a command ran twice (replay) or was lost")

        # -- C-AC-15: a host experiment started from the UI shell survives UI death; handoff stays held --------
        pidfile = P / "exp.pid"
        ui2.send(f"sleep 300 & echo $! > {pidfile}\r".encode())
        end = time.monotonic() + 10
        while time.monotonic() < end and not (pidfile.exists() and pidfile.read_text().strip()):
            ui2.drain(0.05)
        exp_pid = int(pidfile.read_text().strip())
        exp = (exp_pid, ticks(exp_pid))
        self.assertIsNotNone(exp[1])
        ui2.send(b"wb-handoff\r")
        ui2.pause(2.0)
        ui2.send(PREFIX + b"h")
        self.assertTrue(ui2.wait(lambda: "handoff_held" in footer(ui2), 8), footer(ui2))
        self.assertIn("manual_jobs", footer(ui2), "held reasons are not shown by the UI")
        held_view = shell_view(live.status())
        self.assertEqual(held_view["input_owner"], "user", "a held handoff changed the owner")

        # -- abrupt UI death (SIGKILL) is a detach as well; owner/mode/request + experiment survive --------------
        ui_pid = ui2.ui_pid()
        self.assertIsNotNone(ui_pid, "UI child not found")
        self.assertTrue(kill_exact(ui_pid, ticks(ui_pid)))
        self.assertTrue(ui2.wait(ui2.done, 15))
        killed = self.wait_status(lambda s: s["attached"] is False)
        self.assertEqual(self.pane_alive(killed), alive_before)
        self.assertEqual(shell_view(killed), held_view)
        self.assertEqual(ticks(exp[0]), exp[1], "host experiment died with the UI")
        self.assertEqual(self.marker(), "ab")
        ui3 = self.reattach("re2")
        ui3.pause(1.5)
        self.assertEqual(shell_view(live.status()), held_view, "state changed by the second reattach")
        self.assertIn(f"shell mode: {held_view['parent_mode']}", header(ui3))
        self.assertIn("host 입력 owner: user", header(ui3))
        ui3.send(PREFIX + b"3")
        self.wait_status(lambda s: s["focus"] == "host_shell")

        # -- a wb-handoff that the backend holds (user-started background job = G2 open item, gates/G2.md) leaves
        #    the shell in control_wait: typed input must be refused visibly, never queued or replayed, and an
        #    explicit takeover confirm either works or is refused visibly with the state unchanged. --------------
        self.assertEqual(held_view["parent_mode"], "control_wait", held_view)
        ui3.send(f"printf z >> {marker}\r".encode())
        self.assertTrue(ui3.wait(lambda: "입력 거부" in footer(ui3), 6), footer(ui3))
        ui3.pause(1.0)
        self.assertEqual(self.marker(), "ab", "refused input reached the shell")
        self.assertEqual(shell_view(live.status())["queued"], 0, "refused input was queued")
        ui3.send(PREFIX + b"c")
        ui3.pause(1.5)
        confirm_footer = footer(ui3)
        after_confirm = shell_view(live.status())
        confirm_worked = after_confirm["parent_mode"] == "manual_prompt"
        if not confirm_worked:
            self.assertIn("takeover_held", confirm_footer, "a refused confirm is not shown by the UI")
            self.assertEqual(after_confirm["input_owner"], "user")
            self.assertEqual(after_confirm["owner_epoch"], held_view["owner_epoch"])
        ui3.pause(1.0)
        self.assertEqual(self.marker(), "ab", "refused input was replayed after the confirm attempt")
        self.assertEqual(self.pane_alive(live.status()), alive_before)
        self.assertEqual(ticks(exp[0]), exp[1])
        self.observed = {"takeover_confirm_worked_under_held_handoff": confirm_worked, "confirm_footer": confirm_footer}

        # -- provider never contacted: no automatic model run started by detach/reattach --------------------------
        self.assertEqual(live.provider.requests, 0, "a model turn was started")
        log = (live.data / "backend.log").read_text()
        self.assertEqual(log.count("starting in"), 1, log)
        self.assertEqual(len(live.backend_processes()), 1)
        self.assertEqual(os.stat(live.data).st_mode & 0o777, 0o700)

        # -- full shutdown while a UI is attached: the UI exits cleanly and nothing is left ---------------------
        result = live.shutdown()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(ui3.wait(ui3.done, 30), footer(ui3))
        # exit status on a backend-initiated close is not specified by the contract; only a clean (non-signal) exit
        # with the outer terminal restored is required
        self.assertIn(ui3.status(), (0, 1), ui3.status())
        flags = termios_flags((ui3.work / "after_raw").read_text())
        self.assertTrue(flags["icanon"] and flags["echo"] and flags["isig"], f"outer terminal not restored: {flags}")
        self.observed["ui_exit_status_on_backend_shutdown"] = ui3.status()
        end = time.monotonic() + 15
        while time.monotonic() < end and residue(live):
            time.sleep(0.1)
        self.assertEqual(residue(live), {}, "residue after confirmed shutdown")
        self.assertIsNone(ticks(exp[0]) if ticks(exp[0]) == exp[1] else None)
        self.assertNotEqual(ticks(exp[0]), exp[1], "host experiment survived the confirmed full shutdown")


# ---------------------------------------------------------------------------------------------------------------
# Deterministic fixture-level display checks on the real product UI loop (no OMP, no backend): a ui_v1 fixture
# server pushes the backend-provided state. This is NOT production wiring evidence; it checks what the CW-06
# port renders and that the UI itself never sends anything unsolicited (C-AC-18 UI part, C-AC-30 UI part).
# ---------------------------------------------------------------------------------------------------------------
class FixtureUiStateTests(unittest.TestCase):
    FORBIDDEN = {"input", "paste", "takeover_request", "takeover_confirm", "handoff", "shutdown_request",
                 "shutdown_confirm", "confirm_boot"}

    def setUp(self):
        self.servers: list[S.ScriptedServer] = []
        self.uis: list[S.UiPty] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for ui in self.uis:
            ui.close()
        roots = [server.root for server in self.servers]
        for server in self.servers:
            server.close()
        self.assertFalse([root for root in roots if root.exists()], "fixture temp dir left behind")

    def attach(self, snapshot: dict, name: str) -> tuple[S.ScriptedServer, S.UiPty]:
        server = S.ScriptedServer(snapshot=snapshot)  # one owned 0700 temp dir + UDS per UI connection
        self.servers.append(server)
        work = server.root / "work" / name
        work.mkdir(parents=True)
        ui = S.UiPty(server.path, work, rows=30, cols=140)
        self.uis.append(ui)
        self.assertTrue(server.wait(server.attached.is_set, 10), "UI did not attach")
        return server, ui

    @staticmethod
    def kinds(server: S.ScriptedServer) -> set[str]:
        return {f.header.get("type") for f in server.frames}

    def test_paused_automation_and_owner_are_shown_and_the_ui_sends_nothing_unsolicited(self):
        shared = S.snap(owner="manager", mode="control_wait", automation="paused")
        server, first = self.attach(shared, "one")
        self.assertTrue(first.wait_text("자동화: paused", 10), first.screen_text()[:400])
        self.assertIn("host 입력 owner: manager", first.screen_text())
        first.wait_for(lambda: False, 1.0)  # quiet second: a UI that auto-sends would show up here
        self.assertFalse(self.kinds(server) & self.FORBIDDEN, self.kinds(server))

        # focus change: automation and owner text unchanged, only a focus frame is sent
        first.send(bytes([0x1D]) + b"3")
        self.assertTrue(first.wait_text("HOST SHELL *FOCUS*", 5))
        self.assertIn("자동화: paused", first.screen_text())
        self.assertIn("host 입력 owner: manager", first.screen_text())
        self.assertTrue(server.wait(lambda: server.of("focus") != [], 5))

        # the backend pushes a new owner: shown, automation text unchanged, still no input sent by the UI
        server.state(S.snap(owner="user", mode="manual_prompt", automation="paused", focus="host_shell"))
        self.assertTrue(first.wait_text("host 입력 owner: user", 5))
        self.assertIn("shell mode: manual_prompt", first.screen_text())
        self.assertIn("자동화: paused", first.screen_text(), "automation text changed with the owner")
        self.assertFalse(self.kinds(server) & self.FORBIDDEN, self.kinds(server))

        # UI exit is a plain detach: no shutdown/handoff/takeover frame is sent
        first.send(bytes([0x1D]) + b"d")
        self.assertTrue(first.ui_done(10))
        self.assertEqual(first.status(), 0)
        self.assertTrue(server.wait(lambda: len(server.of("detach")) == 1, 5))
        self.assertFalse(self.kinds(server) & self.FORBIDDEN, self.kinds(server))

        # a later attach to the backend state (owner user, automation paused) shows it and sends nothing
        server2, second = self.attach(server.snapshot, "two")
        self.assertTrue(second.wait_text("자동화: paused", 10))
        self.assertIn("host 입력 owner: user", second.screen_text())
        second.wait_for(lambda: False, 1.0)
        self.assertFalse(self.kinds(server2) & self.FORBIDDEN, self.kinds(server2))

    def test_typed_input_goes_only_to_the_focus_pane_and_never_changes_state_text(self):
        server, ui = self.attach(S.snap(owner="user", mode="manual_prompt", automation="paused", focus="host_shell"), "k")
        self.assertTrue(ui.wait_text("자동화: paused", 10))
        ui.send(b"ls\r")
        self.assertTrue(server.wait(lambda: server.payloads("input", "host_shell") == b"ls\r", 5),
                        server.payloads("input"))
        self.assertEqual(server.payloads("input", "manager_omp"), b"")
        self.assertEqual(server.payloads("input", "worker_omp"), b"")
        self.assertIn("자동화: paused", ui.screen_text(), "manual input changed the automation text")
        self.assertIn("host 입력 owner: user", ui.screen_text())
        self.assertFalse({"takeover_request", "takeover_confirm", "handoff", "shutdown_request"} & self.kinds(server))


if __name__ == "__main__":
    unittest.main()
