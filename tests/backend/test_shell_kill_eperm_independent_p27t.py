"""Independent re-verification of the C-D63 review findings R1 and R4 plus the UiServer handler contract (p27-shell-test-03).

Expectations come from result-p27-cd63-review-01 (R1, R4), root-adjudication-p27-cd63-r3 and C-D63 ("backend, OMP pane은
그대로다"), not from the worker's tests:

* R1: a session member that refuses a signal with EPERM (what sudo/su/pkexec leave behind) is skipped and reported as a
  survivor with reason ``permission_denied``; the other members are still ended; the kill answers ok, never raises,
  and the backend and both OMPs keep running and serving. Any other signal error is a survivor reason too
  (``signal_failed:<ERRNO>``), never an exception;
* R1 restart paths: closing the exited host shell / an exited OMP that left an unsignallable member behind never makes the
  restart fail: a new process starts and the survivor is reported;
* R1 shutdown: ``_close`` with unsignallable members completes, closes every pane without an ``error`` entry and
  reports the survivors;
* UiServer: a handler that raises unexpectedly is answered with ``internal_error`` and the server serves the next
  request on the same connection;
* R4: after a kill (or an exit) the dead shell contributes no ``shell_request``/``shell_foreground`` to the
  shutdown_request active work.

EPERM is simulated only by patching ``signal.pidfd_send_signal`` for the pidfd of one real member (resolved through
/proc/self/fdinfo); no sudo/su/pkexec is ever run. Every process a test starts is identified by pidfd + start ticks and
removed through that pidfd only (the p27s ``Owned`` helper / ``kill_exact``).
"""
from __future__ import annotations

import contextlib
import errno
import io
import os
from pathlib import Path
import select
import signal
import socket
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_restart_pane_independent_p27q import alive, kill_exact, start_ticks  # noqa: E402
from test_shell_kill_restart_independent_p27s import _ShellCase, stat  # noqa: E402
from workbench.backend.ui_server import Held  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.ui_v1 import ClientType, Reason  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402

REAL_PIDFD_SEND = signal.pidfd_send_signal


def pidfd_pid(fd: int) -> int | None:
    try:
        for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
            if line.startswith("Pid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


class SignalRefuses:
    """Patch ``signal.pidfd_send_signal`` so signalling the pidfd of one of ``pids`` fails with ``code``.

    ``pids=None`` refuses every pidfd signal. The real call is not made for a refused pidfd, so the member stays alive
    exactly as an unsignallable (other-user) process would.
    """

    def __init__(self, pids, code: int = errno.EPERM):
        self.pids = None if pids is None else set(pids)
        self.code = code
        self.refused: list[tuple[int | None, int]] = []
        self.allowed: list[tuple[int | None, int]] = []
        self._patch = mock.patch.object(signal, "pidfd_send_signal", self)

    def __call__(self, fd, signum, *args):
        pid = pidfd_pid(fd)
        if self.pids is None or pid in self.pids:
            self.refused.append((pid, signum))
            raise OSError(self.code, os.strerror(self.code))
        self.allowed.append((pid, signum))
        return REAL_PIDFD_SEND(fd, signum, *args)

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class _Base(_ShellCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self._kill_leftovers)
        self.leftovers: list[tuple[int, str]] = []  # (pid, ticks) of processes only this test may have left
        self.reapers: list = []

    def _kill_leftovers(self):
        for pid, ticks in self.leftovers:
            kill_exact(pid, ticks)
        for reap in self.reapers:  # after the kills: reap the children the backend could not
            reap()

    def reap_later(self, pid: int) -> None:
        """Reap a child this test made unreapable for the backend (every signal refused); pidfd-exact, no PID reuse."""
        fd = os.pidfd_open(pid)

        def reap():
            try:
                for _ in range(100):
                    try:
                        if os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG) is not None:
                            return
                    except ChildProcessError:
                        return  # already reaped by the backend
                    time.sleep(0.05)
            finally:
                os.close(fd)
        self.reapers.append(reap)

    def own_leftover(self, pid: int) -> int:
        ticks = start_ticks(pid)
        self.assertIsNotNone(ticks, f"{pid} is gone (fixture problem)")
        self.leftovers.append((pid, ticks))
        return pid

    def background(self, tag: str, number: int) -> int:
        # nohup: bash re-sends SIGHUP to its jobs when it is hung up, which would end the member without the backend.
        self.type(f"nohup sleep {number} >/dev/null 2>&1 & echo $! > {self.root}/{tag}")
        pid = self.pid_from(tag, str(number))
        self.prompt()
        return pid

    def omp_replay(self, pane_id: PaneId) -> bytes:
        return b"".join(chunk.data for chunk in self.backend.panes[pane_id]._replay)

    def assert_omps_serve(self, token: str):
        for pane_id in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP):
            self.type_line(pane_id, f"echo {token}-{pane_id.value}")
            needle = f"echo:{token}-{pane_id.value}".encode()
            self.wait(lambda: needle in self.omp_replay(pane_id), f"{pane_id.value} answers after the kill")


class SocketBase(_Base):
    """The backend loop runs in its own thread and the real UiServer socket is used, like the product UI."""

    def setUp(self):
        super().setUp()
        self.start_ticker()
        self.sock = None
        self.counter = 0
        self.connect()
        self.assertTrue(self.call(ClientType.ATTACH, size={"rows": 30, "cols": 100})["ok"])

    # The backend loop thread owns every pane: input goes through the socket, never from the test thread.
    def type(self, line: str) -> None:
        header = self.call(ClientType.INPUT, (line + "\r").encode(), pane="host_shell")
        self.assertTrue(header["ok"], f"{line!r} refused: {header}")

    def type_line(self, pane_id: PaneId, line: str):
        header = self.call(ClientType.INPUT, line.encode() + b"\n", pane=pane_id.value)
        self.assertTrue(header["ok"], f"{line!r} refused: {header}")

    def connect(self):
        if self.sock is not None:
            self.sock.close()
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(str(self.backend.layout.ui_socket))
        self.addCleanup(self.sock.close)
        self.decoder, self.frames = ui_v1.FrameDecoder(), []
        self.sock.sendall(ui_v1.encode_frame(ui_v1.hello("p27t")))
        self.assertEqual(self.next(lambda f: f.header.get("type") in ("welcome", "reject")).header["type"], "welcome")

    def pump(self, timeout=0.05):
        if select.select([self.sock], [], [], timeout)[0]:
            data = self.sock.recv(1 << 20)
            if data:
                self.frames.extend(self.decoder.feed(data))

    def next(self, predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for i, frame in enumerate(self.frames):
                if predicate(frame):
                    return self.frames.pop(i)
            self.pump()
        self.fail("no matching frame")

    def call(self, kind, payload=b"", **fields):
        self.counter += 1
        rid = f"t{self.counter}"
        self.sock.sendall(ui_v1.encode_frame(ui_v1.request(kind, rid, **fields), payload))
        return self.next(lambda f: f.header.get("type") == "result" and f.header.get("id") == rid).header

    def assert_backend_serves(self):
        self.assertTrue(self.ticker.is_alive(), "the backend loop stopped")
        self.assertNotIn("stopped", (self.backend.phase,))
        snapshot = self.call(ClientType.SNAPSHOT)
        self.assertTrue(snapshot["ok"], snapshot)


class EpermKillTests(SocketBase):
    def test_eperm_member_is_reported_the_others_die_and_nothing_else_is_touched(self):
        omps, shell = self.omps(), self.backend.shell
        first = self.background("m1", 7911)
        denied = self.own_leftover(self.background("m2", 7912))
        last = self.background("m3", 7913)
        backend_pid = os.getpid()

        with SignalRefuses({denied}) as refusing:
            result = self.call(ClientType.KILL_PANE, pane="host_shell")

        self.assertTrue(result["ok"], result)
        self.assertEqual((result["pane"], result["killed"]), ("host_shell", True))
        self.assertEqual(result["survivors"], [{"pid": denied, "reason": "permission_denied"}])
        self.assertTrue(refusing.refused, "the EPERM member was never signalled (fixture problem)")
        self.assertTrue({pid for pid, _ in refusing.refused} == {denied}, refusing.refused)
        self.assertTrue({first, last, shell.pid} <= {pid for pid, _ in refusing.allowed},
                        "the other members were not signalled after the refusal")
        self.assertFalse(alive(first), "a member behind the refusing one was not ended")
        self.assertFalse(alive(last), "a member behind the refusing one was not ended")
        self.assertFalse(alive(shell.pid))
        self.assertTrue(alive(denied), "the unsignallable member was ended by a bare signal")
        self.assertEqual(os.getpid(), backend_pid)
        self.assert_omps_untouched(omps)
        self.assertEqual(self.backend.kills[-1]["survivors"], result["survivors"])
        self.assertFalse(self.call(ClientType.SNAPSHOT)["snapshot"]["panes"]["host_shell"]["alive"])
        self.assert_backend_serves()
        self.assert_omps_serve("afterkill")

    def test_every_non_leader_member_refusing_still_ends_the_shell_and_reports_each_one(self):
        shell = self.backend.shell
        members = [self.own_leftover(self.background(tag, number)) for tag, number in (("m1", 7921), ("m2", 7922))]
        omps = self.omps()

        with SignalRefuses(members) as refusing:
            result = self.call(ClientType.KILL_PANE, pane="host_shell")

        self.assertTrue(result["ok"], result)
        self.assertEqual(sorted((s["pid"], s["reason"]) for s in result["survivors"]),
                         sorted((pid, "permission_denied") for pid in members))
        self.assertFalse(alive(shell.pid))
        self.assertTrue(all(alive(pid) for pid in members))
        self.assertEqual({pid for pid, _ in refusing.refused}, set(members))
        self.assert_omps_untouched(omps)
        self.assert_backend_serves()
        self.assert_omps_serve("allrefuse")

    def test_a_different_signal_error_is_a_survivor_reason_not_an_exception(self):
        shell = self.backend.shell
        odd = self.own_leftover(self.background("m1", 7931))
        with SignalRefuses({odd}, errno.EINVAL):
            result = self.call(ClientType.KILL_PANE, pane="host_shell")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["survivors"], [{"pid": odd, "reason": "signal_failed:EINVAL"}])
        self.assertFalse(alive(shell.pid))
        self.assert_backend_serves()

    def test_the_killed_shell_restarts_after_an_eperm_kill_and_the_survivor_stays_alone(self):
        denied = self.own_leftover(self.background("m1", 7941))
        old = self.backend.shell
        with SignalRefuses({denied}):
            killed = self.call(ClientType.KILL_PANE, pane="host_shell")
        self.assertTrue(killed["ok"] and killed["survivors"], killed)
        header = self.call(ClientType.RESTART_PANE, pane="host_shell")
        self.assertTrue(header["ok"], header)
        self.assertEqual(header["generation"], old.generation + 1)
        self.prompt("fresh prompt")
        self.assertTrue(alive(denied), "the restart signalled the member that refused")
        self.assertTrue(alive(self.backend.shell.pid))
        self.assert_backend_serves()


class EpermRestartTests(_Base):
    def test_host_shell_restart_with_an_unsignallable_leftover_starts_a_new_shell(self):
        old = self.backend.shell
        denied = self.own_leftover(self.background("m1", 7951))
        self.type("exit")
        self.wait(lambda: old.exited(), "host shell exit")
        for _ in range(20):
            self.backend._tick(0.01)
        self.assertTrue(alive(denied), "the leftover must outlive the shell (fixture problem)")

        with SignalRefuses({denied}) as refusing:
            result = self.backend.restart_pane(PaneId.HOST_SHELL)

        self.assertTrue(result["restarted"], result)
        self.assertEqual(result["survivors"], [{"pid": denied, "reason": "permission_denied"}])
        self.assertTrue(refusing.refused, "the restart did not try the leftover (fixture problem)")
        new = self.backend.shell
        self.assertIsNot(new, old)
        self.assertEqual(new.generation, old.generation + 1)
        self.assertTrue(alive(new.pid))
        self.prompt("fresh prompt")
        self.assertTrue(alive(denied))
        self.assertEqual(self.backend.restarts[-1]["previous"]["survivors"], result["survivors"])
        self.assertIsNone(self.backend.shell.restart["error"])
        self.wait(lambda: self.backend.phase == "ready", "ready again")

    def test_omp_restart_with_an_unsignallable_member_in_its_session_starts_a_new_omp(self):
        self.type_line(PaneId.MANAGER_OMP, "child")
        self.wait(lambda: any(e["kind"] == "child" and e["role"] == "manager" for e in self.helpers()),
                  "OMP helper in its session")
        entry = next(e for e in self.helpers() if e["kind"] == "child" and e["role"] == "manager")
        helper = entry["pid"]
        self.leftovers.append((helper, entry["ticks"]))
        old = self.exit_pane(PaneId.MANAGER_OMP)
        self.assertTrue(alive(helper), "the helper must outlive the OMP (fixture problem)")

        with SignalRefuses({helper}) as refusing:
            result = self.backend.restart_pane(PaneId.MANAGER_OMP)

        self.assertTrue(result["restarted"], result)
        self.assertEqual(result["survivors"], [{"pid": helper, "reason": "permission_denied"}])
        self.assertTrue(refusing.refused)
        new = self.backend.panes[PaneId.MANAGER_OMP]
        self.assertIsNot(new, old)
        self.assertEqual(new.generation, old.generation + 1)
        self.assertNotEqual(new.pid, old.pid)
        self.assertTrue(alive(new.pid) and alive(helper))
        self.wait(lambda: self.backend.phase == "ready" and "rechecking" not in self.backend.omp_isolation,
                  "ready after the OMP restart")
        self.assertTrue(alive(self.backend.panes[PaneId.WORKER_OMP].pid))
        self.assertTrue(alive(self.backend.shell.pid))


class EpermShutdownTests(_Base):
    def test_shutdown_with_unsignallable_members_completes_reports_them_and_raises_nothing(self):
        denied_shell = self.own_leftover(self.background("m1", 7961))
        self.type_line(PaneId.WORKER_OMP, "child")
        self.wait(lambda: any(e["kind"] == "child" and e["role"] == "worker" for e in self.helpers()),
                  "worker helper")
        entry = next(e for e in self.helpers() if e["kind"] == "child" and e["role"] == "worker")
        denied_omp = entry["pid"]
        self.leftovers.append((denied_omp, entry["ticks"]))
        refs = {name: ref for name, ref in self.backend.process_refs().items() if name != "backend"}

        # killpg of the OMP group refuses too (a root member in the group): the pinned KILL below decides.
        with SignalRefuses({denied_shell, denied_omp}) as refusing, \
                mock.patch.object(os, "killpg", side_effect=PermissionError(errno.EPERM, "Operation not permitted")):
            result = self.backend._close()
        self.backend._close = lambda: result  # closed here; the fixture teardown must not close twice

        self.assertTrue(refusing.refused)
        self.assertEqual([p for p in result["panes"] if "error" in p], [], result)
        by_pane = {p["pane"]: p for p in result["panes"]}
        self.assertEqual(set(by_pane), {"host_shell", "manager_omp", "worker_omp"})
        self.assertIn({"pid": denied_shell, "reason": "permission_denied"}, by_pane["host_shell"]["survivors"])
        self.assertIn({"pid": denied_omp, "reason": "permission_denied"}, by_pane["worker_omp"]["survivors"])
        self.assertTrue(result["verified"], result)
        for name, ref in refs.items():
            self.assertNotEqual(start_ticks(ref.pid), str(ref.start_ticks), f"{name} survived the shutdown")
        self.assertTrue(alive(denied_shell) and alive(denied_omp))
        record = self.backend.layout.record.read_text()
        self.assertIn('"phase": "stopped"', record.replace('":"', '": "'))

    def test_shutdown_where_every_pidfd_signal_refuses_completes_without_an_exception(self):
        shell = self.backend.shell
        omps = [self.backend.panes[p] for p in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP)]
        for pane in [shell, *omps]:  # the test, not the (refused) backend, removes and reaps them afterwards
            self.leftovers.append((pane.pid, str(pane.ref.start_ticks)))
            self.reap_later(pane.pid)
        member = self.own_leftover(self.background("m1", 7971))

        with SignalRefuses(None) as refusing:
            result = self.backend._close()  # must return, never raise
        self.backend._close = lambda: result

        self.assertTrue(refusing.refused)
        self.assertEqual([p for p in result["panes"] if "error" in p], [], result)
        self.assertEqual(len(result["panes"]), 3)
        self.assertTrue(alive(member), "a refused member cannot have been ended")
        self.assertIn("verified", result)  # the PTY hang-up may end the leaders; the report just has to exist
        self.assertIsNotNone(stat(member))


class ShutdownRequestAfterKillTests(_Base):
    def shell_kinds(self) -> list[str]:
        return [item["kind"] for item in self.backend.shutdown_request()["active"]
                if item["kind"].startswith("shell")]

    def test_a_killed_manager_command_is_no_active_shell_work_before_or_after_the_restart(self):
        self.hand_to_manager()
        control, automation = self.control_ports()
        shell = self.backend.shell
        shell.shell.submit(control, f"echo $$ > {self.root}/run; exec sleep 7981", automation)
        self.pid_from("run", "7981")
        self.assertIn("shell_request", self.shell_kinds(), "fixture: the in-flight command must be active work")

        result = self.backend.kill_pane(PaneId.HOST_SHELL)

        self.assertTrue(result["manager_command_in_flight"])
        self.assertEqual(self.shell_kinds(), [], "the dead shell still reports active work")
        for _ in range(30):
            self.backend._tick(0.01)
        self.assertEqual(self.shell_kinds(), [])
        self.backend.restart_pane(PaneId.HOST_SHELL)
        self.prompt("fresh prompt")
        self.assertEqual(self.shell_kinds(), [], "the restarted idle shell reports work from the old one")

    def test_a_killed_foreground_program_is_no_active_shell_work(self):
        self.type(f"sh -c 'echo $$ > {self.root}/fg; exec sleep 7982'")
        self.pid_from("fg", "7982")
        self.wait(lambda: self.mode() == "manual_foreground", "foreground job")
        self.assertIn("shell_foreground", self.shell_kinds(), "fixture: the foreground program must be active work")
        self.backend.kill_pane(PaneId.HOST_SHELL)
        self.assertEqual(self.shell_kinds(), [])

    def test_an_exited_shell_with_an_unfinished_request_is_no_active_shell_work(self):
        self.hand_to_manager()
        control, automation = self.control_ports()
        shell = self.backend.shell
        shell.shell.submit(control, f"echo $$ > {self.root}/run; exec sleep 7983", automation)
        run = self.pid_from("run", "7983")
        self.assertIn("shell_request", self.shell_kinds(), "fixture: the in-flight command must be active work")
        kill_exact(shell.pid, str(shell.ref.start_ticks))  # the shell dies from outside, request unfinished
        self.wait(lambda: shell.exited(), "the shell ended")
        for _ in range(20):
            self.backend._tick(0.01)
        self.assertEqual(self.shell_kinds(), [])


class HandlerFailureTests(SocketBase):
    def test_a_raising_handler_answers_internal_error_and_the_next_request_is_served(self):
        omps, shell = self.omps(), self.backend.shell
        errors_before = self.backend.ui.stats["handler_errors"]
        for kind, attr, exc in ((ClientType.KILL_PANE, "kill_pane", PermissionError(errno.EPERM, "Operation not permitted")),
                                (ClientType.KILL_PANE, "kill_pane", RuntimeError("boom")),
                                (ClientType.RESTART_PANE, "restart_pane", OSError(errno.EIO, "io"))):
            with self.subTest(attr=attr, exc=type(exc).__name__):
                with mock.patch.object(self.backend, attr, side_effect=exc), \
                        contextlib.redirect_stderr(io.StringIO()):
                    header = self.call(kind, pane="host_shell")
                self.assertEqual((header["ok"], header["reason"]), (False, "internal_error"), header)
                self.assertIn(type(exc).__name__, header["detail"])
                self.assert_backend_serves()  # same connection, next request
                self.assertTrue(alive(shell.pid))
                self.assert_omps_untouched(omps)
        self.assertEqual(self.backend.ui.stats["handler_errors"], errors_before + 3)
        # The failures changed nothing: a real kill still works over the same connection.
        ok = self.call(ClientType.KILL_PANE, pane="host_shell")
        self.assertTrue(ok["ok"], ok)
        self.assertFalse(alive(shell.pid))
        self.assert_omps_serve("afterhandlerfailure")


if __name__ == "__main__":
    unittest.main()
