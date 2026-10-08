"""Independent L-CW17-DETACH runtime check (p27-cw17-test-01).

Expected outcomes derived from CW-17.md / SPEC s2.7 / C-AC-15/18 before the
implementation was read:
- The real entrypoint (``python -m workbench start``) on a plain PTY starts a
  backend in a session different from the frontend; the frontend is not its
  parent.
- Killing the attached frontend with SIGKILL is only a detach: backend, both
  OMP processes, the host shell and the bridge peers keep their identity.
- A second ``start`` while the backend runs attaches/reuses instead of spawning.
- After more than 60 s without any UI, reattaching through the real entrypoint
  finds the same backend/OMP/shell identities (pid + start ticks), the same
  input owner (here: the user, C-AC-18) and control mode, no new run, no
  duplicate delivery; the user still owns and can use the same parent shell.

This complements the worker test (manager-owned shell, clean detach) with a
user-owned shell and an abrupt SIGKILL of the attached client.
"""
import os
import time
import unittest

from independent_support import (
    LiveBackend, UiClient, finish, find_omp, identity, kill_exact, parent_pid, session_of, ticks, wait_file,
    wait_status,
)
from workbench.contracts import ui_v1
from workbench.contracts.v1 import PaneId

OMP = find_omp()
DETACHED_SECONDS = 63


@unittest.skipUnless(OMP, "real OMP is required for L-CW17-DETACH (version recorded, not pinned: C-D72 (2))")
class IndependentLiveDetachTests(unittest.TestCase):
    def test_sigkill_attached_frontend_then_over_60s_detached_reattach_keeps_user_owned_identities(self):
        live = LiveBackend(OMP, path="/usr/bin:/bin")
        self.addCleanup(finish, self, live)
        marker = live.project / "detach-marker"

        # 1. Real entrypoint, plain PTY: start + attach.
        first = live.pty(*live.start_args("--plain"))
        first.wait_output(b"attached to manager_omp", 120)
        started = wait_status(live, lambda s: s["phase"] == "ready" and s["attached"] is True)
        live.remember(started)
        backend_pid = started["backend"]["pid"]
        self.assertEqual(started["backend"]["process"]["pid"], backend_pid)
        self.assertNotEqual(started["backend"]["session_id"], session_of(first.pid), "backend shares UI session")
        self.assertEqual(started["backend"]["session_id"], session_of(backend_pid))
        self.assertNotEqual(parent_pid(backend_pid), first.pid, "frontend is the backend's parent")
        for role in ("manager", "worker"):
            self.assertTrue(started["bridge"][role]["pid_matches_pane"], started["bridge"])
        self.assertEqual(started["panes"]["host_shell"]["shell"]["input_owner"], "user")

        # 2. The user works in the host shell through the real client.
        first.send(b"\x1d3")
        wait_status(live, lambda s: s["focus"] == "host_shell")
        first.send(f"printf x >> {marker}\r".encode())
        self.assertEqual(wait_file(marker, "x"), "x")
        before = wait_status(live, lambda s: s["panes"]["host_shell"]["shell"]["parent_mode"] == "manual_prompt")
        self.assertTrue(before["attached"])

        # 3. SIGKILL the attached frontend: only the attachment ends.
        self.assertTrue(kill_exact(first.pid, first.ticks))
        self.assertEqual(first.wait(10), -9)
        after_kill = wait_status(live, lambda s: s["attached"] is False)
        self.assertEqual(identity(after_kill), identity(before))
        self.assertTrue(all(p["alive"] for p in after_kill["panes"].values()))

        # 4. A second start while running reuses the backend (no spawn).
        again = live.cli(*live.start_args("--no-attach"))
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("attaching instead of starting", again.stdout)
        self.assertEqual(live.backend_processes(), [backend_pid])

        # 5. More than 60 s without any UI; every identity stays exactly alive.
        refs = dict(live.known)
        t0 = time.monotonic()
        while time.monotonic() - t0 < DETACHED_SECONDS:
            for name, (pid, start) in refs.items():
                self.assertEqual(ticks(pid), start, f"{name} identity lost while detached")
            time.sleep(2)
        elapsed = time.monotonic() - t0
        self.assertGreater(elapsed, 60)
        idle = live.status()
        self.assertFalse(idle["attached"])
        self.assertEqual(identity(idle), identity(before))

        # 6. Reattach through the real entrypoint; focus, replay and user ownership survive.
        second = live.pty("attach", "--plain", "--data-dir", str(live.data))
        second.wait_output(b"attached to host_shell", 30)
        second.wait_output(b"detach-marker", 10)  # replayed shell tail shows the earlier command
        second.send(f"printf y >> {marker}\r".encode())
        self.assertEqual(wait_file(marker, "xy"), "xy", "user-owned shell input lost after reattach")
        second.send(b"\x1dd")
        self.assertEqual(second.wait(30), 0)
        self.assertIn(b"backend keeps running", second.output)

        # 7. Library client: replay ordering and final identity.
        with UiClient(live.data / "ui.sock") as client:
            attached = client.attach((30, 100))
            self.assertTrue(attached["ok"], attached)
            after = attached["snapshot"]
            client.pump(0.5)
            replay = [ui_v1.decode_display(f) for f in client.displays if f.header.get("replay")]
            for pane in PaneId:
                sequences = [c.sequence for c in replay if c.pane_id is pane]
                self.assertTrue(sequences, pane)
                self.assertEqual(sequences, sorted(set(sequences)), pane)
            client.detach()

        self.assertEqual(identity(after), identity(before))
        shell = after["panes"]["host_shell"]["shell"]
        self.assertEqual((shell["input_owner"], shell["parent_mode"]), ("user", "manual_prompt"))
        self.assertIsNone(shell["request_id"])
        self.assertIsNone(shell["supervisor"])
        self.assertEqual(after["bridge"]["event_cursor"], before["bridge"]["event_cursor"])
        self.assertEqual(live.provider.requests, 0, "a model run was started")
        self.assertEqual(marker.read_text(), "xy", "duplicate or replayed delivery")
        log = (live.data / "backend.log").read_text()
        self.assertEqual((log.count("starting in"), log.count("ready:")), (1, 1), log)
        self.assertEqual(log.count("another backend holds this data dir"), 0, log)
        self.assertEqual(log.count("ui attached"), 3, log)
        self.assertEqual(live.backend_processes(), [backend_pid])
        self.assertEqual(os.stat(live.data).st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
