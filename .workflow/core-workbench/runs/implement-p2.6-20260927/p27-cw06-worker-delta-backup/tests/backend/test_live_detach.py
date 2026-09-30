"""L-CW17-DETACH runtime: detach for more than 60 s, reattach to the same backend.

The real entrypoint starts the backend with two real OMP 18.2.10 processes
(bridge extension loaded, local scripted provider) and a persistent Bash shell.
The minimal client runs on a plain PTY. Before detaching, the user runs one
marker command and hands the shell to manager control. After >60 s detached,
reattach must find the same backend/OMP/shell identities, the same input owner,
owner epoch and control mode, no new run and no duplicate delivery.
"""
import time
import unittest

from live_harness import LiveBackend, find_omp, ticks
from workbench.backend.client import UiClient
from workbench.contracts import ui_v1
from workbench.contracts.v1 import PaneId

OMP = find_omp()
DETACHED_SECONDS = 62


def wait_status(live, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    snapshot = None
    while time.monotonic() < deadline:
        for client in live.clients:
            client.drain(0)
        snapshot = live.status()
        if snapshot is not None and predicate(snapshot):
            return snapshot
        time.sleep(0.1)
    raise AssertionError(f"status predicate timeout: {snapshot}")


def identity(snapshot):
    panes = snapshot["panes"]
    shell = panes["host_shell"]["shell"]
    return {
        "backend": snapshot["backend"]["process"], "backend_session": snapshot["backend"]["session_id"],
        "panes": {name: (pane["process"], pane["session_id"], pane["generation"]) for name, pane in panes.items()},
        "bridge": {role: {k: snapshot["bridge"][role][k] for k in ("session_id", "generation", "pid",
                                                                    "pid_matches_pane")}
                   for role in ("manager", "worker")},
        "shell_parent": shell["parent"], "shell_generation": shell["generation"],
        "input_owner": shell["input_owner"], "owner_epoch": shell["owner_epoch"],
        "control_mode": shell["parent_mode"], "supervisor": shell["supervisor"],
        "request_id": shell["request_id"], "focus": snapshot["focus"],
    }


@unittest.skipUnless(OMP, "real OMP 18.2.10 is required for L-CW17-DETACH")
class LiveDetachTests(unittest.TestCase):
    def test_detach_over_60s_then_reattach_same_identities_no_new_run(self):
        live = LiveBackend(OMP, path="/usr/bin:/bin")
        residue_box = {}

        def finish():
            residue_box.update(live.cleanup())
            self.assertEqual({k: v for k, v in residue_box.items() if v}, {}, "owned process/socket leak")
        self.addCleanup(finish)
        marker = live.project / "delivery-marker"

        # 1. Real entrypoint on a plain PTY: start + attach with the minimal client.
        first = live.pty(*live.start_args("--plain"))
        first.wait_output(b"attached to manager_omp", 120)
        live.remember(live.status())
        first.send(b"\x1d3")  # focus the host shell
        wait_status(live, lambda s: s["focus"] == "host_shell")
        first.send(f"printf x >> {marker}\r".encode())
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            first.drain()
        self.assertEqual(marker.read_text(), "x")
        first.send(b"wb-handoff\r")
        wait_status(live, lambda s: s["panes"]["host_shell"]["shell"]["parent_mode"] == "control_wait")
        first.send(b"\x1dh")  # hand the shell to manager control
        before = wait_status(live, lambda s: s["panes"]["host_shell"]["input_owner"] == "manager")
        first.send(b"\x1dd")
        self.assertEqual(first.wait(30), 0)
        self.assertIn(b"backend keeps running", first.output)

        detached = wait_status(live, lambda s: s["attached"] is False)
        self.assertEqual(identity(detached), identity(before))
        self.assertEqual(detached["phase"], "ready")
        self.assertEqual(identity(before)["control_mode"], "control_wait")
        self.assertEqual(identity(before)["input_owner"], "manager")
        refs = dict(live.known)

        # 2. No UI for more than 60 s; every owned identity stays alive.
        started = time.monotonic()
        while time.monotonic() - started < DETACHED_SECONDS:
            for name, (pid, start) in refs.items():
                self.assertEqual(ticks(pid), start, f"{name} identity lost while detached")
            time.sleep(2)
        elapsed = time.monotonic() - started

        # 3. Reattach through the real entrypoint on a plain PTY.
        again = live.pty("attach", "--plain", "--data-dir", str(live.data))
        again.wait_output(b"attached to host_shell", 30)  # focus survived detach
        again.send(b"\x1dd")
        self.assertEqual(again.wait(30), 0)

        # 4. Reattach with the library client to inspect replay and ownership.
        with UiClient(live.data / "ui.sock") as client:
            attached = client.attach((30, 100))
            self.assertTrue(attached["ok"])
            after = attached["snapshot"]
            client.pump(0.5)
            replay = [ui_v1.decode_display(f) for f in client.displays if f.header.get("replay")]
            for pane in PaneId:
                sequences = [c.sequence for c in replay if c.pane_id is pane]
                self.assertTrue(sequences, pane)
                self.assertEqual(sequences, sorted(set(sequences)), pane)
                self.assertLessEqual(sequences[-1], after["panes"][pane.value]["sequence"])
            held = client.input(PaneId.HOST_SHELL, b"echo must-not-run\r")
            self.assertEqual((held["ok"], held["reason"]), (False, "input_owner_manager"))
            client.detach()

        self.assertGreater(elapsed, 60)
        self.assertEqual(identity(after), identity(before))
        for role in ("manager", "worker"):
            self.assertTrue(after["bridge"][role]["pid_matches_pane"])
        self.assertIsNone(after["panes"]["host_shell"]["shell"]["supervisor"])  # no managed run exists
        self.assertIsNone(after["panes"]["host_shell"]["shell"]["request_id"])
        self.assertEqual(after["bridge"]["event_cursor"], before["bridge"]["event_cursor"])
        self.assertEqual(live.provider.requests, 0)  # no model run was started
        self.assertEqual(marker.read_text(), "x")  # delivered exactly once, never replayed
        self.assertEqual(after["ui"]["attaches"], 3)
        log = (live.data / "backend.log").read_text()
        self.assertEqual((log.count("starting in"), log.count("ready:")), (1, 1), log)
        self.assertEqual(live.backend_processes(), [after["backend"]["pid"]])

        # 5. Normal path cleanup: confirmed shutdown, then OS-level verification.
        result = live.shutdown()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"verified": true', result.stdout)
        deadline = time.monotonic() + 10
        while any(live.leaks().values()) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual({k: v for k, v in live.leaks().items() if v}, {})


if __name__ == "__main__":
    unittest.main()
