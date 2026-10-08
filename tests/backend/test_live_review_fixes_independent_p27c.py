"""Independent live verification of review fixes P2-2..P2-4 (p27-review-fix-test-01).

Real entrypoint (``python -m workbench start``), ui_v1 over the UDS, two real
OMP 18.2.10 processes on the local scripted provider (no credentials, no
network), real Bash host shell. Expectations derived from the contracts:

- P2-4 / C-AC-19: a 2 MiB paste (the admitted maximum) into a raw-mode user
  foreground program is delivered completely and in order while the backend
  keeps serving snapshot/status requests and OMP pane output with bounded
  latency -- also with ~500 extra (test-owned) processes on the host. If the
  foreground program changes mid-paste, the remainder follows the existing
  manual-input contract (C-AC-08: only the verified current target receives
  bytes, nothing duplicated or reordered, anything withheld is accounted for
  with a visible reason).
- P2-3 / C-AC-32: what the backend hands to OMP (observed at exec through a
  transparent ``/bin/sh`` wrapper, since the OMP runtime itself ignores SIGPIPE
  and SIGXFSZ after start), the host shell and its user-launched children have
  default SIGPIPE/SIGXFSZ; ``yes | head -1`` in the host shell ends promptly
  with the writer killed by SIGPIPE.
- P2-2 / C-D45: shutdown after an OMP pane already exited still cleans up
  without signalling unrelated (test-owned foreign session) processes.

Cleanup: confirmed shutdown then exact-identity fallback; every leak fails.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import signal
import statistics
import subprocess
import tempfile
import threading
import time
import unittest

from independent_support import (LiveBackend, PaneId, UiClient, find_omp, finish, kill_exact, start_no_attach,
                                 ticks, wait_client)
from workbench.contracts.ui_v1 import MAX_PASTE_BYTES

OMP = find_omp()
PIPE_XFSZ = (1 << (signal.SIGPIPE - 1)) | (1 << (signal.SIGXFSZ - 1))


def ordered_payload(size: int) -> bytes:
    """Printable, position-encoded bytes: any loss, duplication or reordering is visible."""
    blocks = (size + 7) // 8
    return b"".join(b"%08x" % i for i in range(blocks))[:size]


def masks(text: str) -> dict[str, int]:
    return {line.split(":")[0]: int(line.split()[1], 16) for line in text.splitlines()
            if line.startswith(("SigIgn:", "SigBlk:"))}


class Sleepers:
    """~500 test-owned sleepers (host load for P2-4), cleaned by exact identity."""

    def __init__(self, count: int):
        self.processes = [subprocess.Popen(["/usr/bin/sleep", "900"], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
                          for _ in range(count)]
        self.identities = [(p.pid, ticks(p.pid)) for p in self.processes]

    def alive(self) -> int:
        return sum(1 for pid, start in self.identities if ticks(pid) == start)

    def close(self) -> None:
        for pid, start in self.identities:
            kill_exact(pid, start)
        for process in self.processes:
            process.wait(10)


class LiveCase(unittest.TestCase):
    def live(self, omp: str | None = None) -> LiveBackend:
        live = LiveBackend(omp or OMP, path="/usr/bin:/bin")
        self.addCleanup(finish, self, live)
        start_no_attach(self, live)
        return live

    def client(self, live: LiveBackend) -> UiClient:
        client = UiClient(live.data / "ui.sock")
        self.addCleanup(client.close)
        self.assertTrue(client.attach((30, 100))["ok"])
        return client

    @staticmethod
    def shell_pane(snapshot: dict) -> dict:
        return snapshot["panes"]["host_shell"]

    def run_line(self, client: UiClient, line: str) -> None:
        result = client.input(PaneId.HOST_SHELL, (line + "\r").encode())
        self.assertTrue(result["ok"], result)

    def wait_mode(self, client: UiClient, mode: str, timeout: float = 15.0) -> dict:
        return wait_client(client, lambda s: self.shell_pane(s)["shell"]["parent_mode"] == mode, timeout)

    def wait_path(self, client: UiClient, path: Path, size: int = 1, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while not (path.exists() and path.stat().st_size >= size):
            self.assertLess(time.monotonic(), deadline, f"{path} not written")
            client.pump(0.05)
            client.displays.clear()


@unittest.skipUnless(OMP, "real OMP is required (version recorded, not pinned: C-D72 (2))")
class LivePasteResponsivenessTests(LiveCase):
    def omp_echo_latency(self, client: UiClient, pane: PaneId) -> float:
        """Time from one key admitted to the OMP pane until that pane's next display chunk."""
        client.displays.clear()
        began = time.monotonic()
        self.assertTrue(client.input(pane, b"x")["ok"])
        while not any(f.header.get("pane") == pane.value for f in client.displays):
            if time.monotonic() - began > 10:
                return float("inf")
            client.pump(0.02)
        latency = time.monotonic() - began
        client.displays.clear()
        return latency

    def test_2mib_raw_paste_with_500_host_sleepers_in_order_and_responsive(self):
        sleepers = Sleepers(500)
        self.addCleanup(sleepers.close)
        live = self.live()
        client = self.client(live)
        baseline = [self.omp_echo_latency(client, PaneId.WORKER_OMP) for _ in range(3)]
        self.assertLess(max(baseline), 5.0, f"OMP echo unavailable before the paste: {baseline}")
        out = live.project / "pasted"
        payload = ordered_payload(MAX_PASTE_BYTES)
        self.run_line(client, f"stty raw -echo; head -c {len(payload)} > {shlex.quote(str(out))}; stty sane")
        self.wait_mode(client, "manual_foreground")
        wait_client(client, lambda s: True)
        time.sleep(0.3)  # stty raw is in effect before the paste
        status_latency: list[float] = []

        def status_probe():
            for _ in range(2):
                began = time.monotonic()
                snapshot = live.status()
                status_latency.append(time.monotonic() - began if snapshot else float("inf"))
        began = time.monotonic()
        pasted = client.paste(PaneId.HOST_SHELL, payload)
        admit_latency = time.monotonic() - began
        self.assertTrue(pasted["ok"], pasted)
        prober = threading.Thread(target=status_probe, daemon=True)
        prober.start()
        snapshot_latency, echo_latency, in_flight_samples = [], [], 0
        while (not out.exists() or out.stat().st_size < len(payload)) and time.monotonic() - began < 90:
            start = time.monotonic()
            snapshot = client.snapshot()
            snapshot_latency.append(time.monotonic() - start)
            if self.shell_pane(snapshot)["queued_input_bytes"] > 0:
                in_flight_samples += 1
                if len(echo_latency) < 5:
                    echo_latency.append(self.omp_echo_latency(client, PaneId.WORKER_OMP))
            client.displays.clear()
            time.sleep(0.02)
        prober.join(60)
        elapsed = time.monotonic() - began
        pane = self.shell_pane(client.snapshot())
        report = {"elapsed": round(elapsed, 2), "admit": round(admit_latency, 3),
                  "snapshot_max": round(max(snapshot_latency, default=0), 3),
                  "snapshot_median": round(statistics.median(snapshot_latency or [0]), 3),
                  "samples": len(snapshot_latency), "in_flight_samples": in_flight_samples,
                  "echo": [round(x, 3) for x in echo_latency], "status": [round(x, 2) for x in status_latency],
                  "sleepers_alive": sleepers.alive()}
        print(f"\n[p27c P2-4 paste] {report}")
        self.assertEqual(sleepers.alive(), 500, "host load changed during the paste")
        self.assertEqual((pane["dropped_input_bytes"], pane["last_input_problem"]), (0, None), report)
        data = out.read_bytes()
        self.assertEqual(len(data), len(payload), report)
        self.assertTrue(data == payload, "pasted bytes lost, duplicated or reordered")
        self.assertLess(admit_latency, 1.0, report)
        self.assertLess(max(snapshot_latency), 1.0, report)
        self.assertLess(statistics.median(snapshot_latency), 0.25, report)
        self.assertTrue(echo_latency, f"paste ended before any OMP output check: {report}")
        self.assertLess(max(echo_latency), 2.0, report)
        self.assertEqual(len(status_latency), 2, report)
        self.assertLess(max(status_latency), 10.0, report)
        self.wait_mode(client, "manual_prompt")
        client.detach()

    def test_foreground_change_mid_paste_follows_manual_target_contract_visibly(self):
        live = self.live()
        client = self.client(live)
        first, second, after = live.project / "first", live.project / "second", live.project / "after"
        done = live.project / "done"
        keep = 256 * 1024
        payload = ordered_payload(MAX_PASTE_BYTES)
        self.run_line(client, f"stty raw -echo; head -c {keep} > {shlex.quote(str(first))}; "
                              f"timeout 10 cat > {shlex.quote(str(second))}; stty sane; "
                              f"printf D > {shlex.quote(str(done))}")
        self.wait_mode(client, "manual_foreground")
        time.sleep(0.3)
        began = time.monotonic()
        self.assertTrue(client.paste(PaneId.HOST_SHELL, payload)["ok"])
        latency = []
        while time.monotonic() - began < 30:
            start = time.monotonic()
            snapshot = client.snapshot()
            latency.append(time.monotonic() - start)
            client.displays.clear()
            if done.exists() and self.shell_pane(snapshot)["queued_input_bytes"] == 0:
                break
            time.sleep(0.05)
        client.pump(0.5)
        pane = self.shell_pane(client.snapshot())
        head_part = first.read_bytes()
        cat_part = second.read_bytes() if second.exists() else b""
        dropped, problem = pane["dropped_input_bytes"], pane["last_input_problem"]
        report = {"first": len(head_part), "second": len(cat_part), "dropped": dropped, "problem": problem,
                  "queued": pane["queued_input_bytes"], "mode": pane["shell"]["parent_mode"],
                  "held": pane["shell"]["held_reasons"], "latency_max": round(max(latency), 3)}
        print(f"\n[p27c P2-4 foreground change] {report}")
        self.assertTrue(done.exists(), report)
        self.assertEqual(pane["queued_input_bytes"], 0, report)
        self.assertTrue(head_part == payload[:keep], "first target got wrong/reordered bytes")
        written = len(payload) - dropped  # bytes the backend put on the PTY
        if cat_part:
            offset = payload.find(cat_part, keep)
            self.assertGreaterEqual(offset, keep, "second target got bytes out of order or duplicated")
            self.assertEqual(offset + len(cat_part), written,
                             f"bytes after the change neither delivered to the current target nor accounted: {report}")
        if dropped:
            self.assertTrue(problem, f"withheld remainder without a visible reason: {report}")
        else:
            self.assertEqual(problem, None, report)
            self.assertTrue(cat_part and payload.endswith(cat_part), report)
        if pane["shell"]["parent_mode"] != "manual_prompt":
            # Bytes possibly left for the shell must stay visible as a hold reason.
            self.assertIn("unsubmitted_or_unconsumed_input", pane["shell"]["held_reasons"], report)
        self.assertLess(max(latency), 1.0, report)
        # The shell stays usable for new manual input afterwards.
        self.run_line(client, f"printf ok > {shlex.quote(str(after))}")
        self.wait_path(client, after)
        self.assertEqual(after.read_text(), "ok")
        client.detach()


@unittest.skipUnless(OMP, "real OMP is required (version recorded, not pinned: C-D72 (2))")
class LiveSignalDispositionAndShutdownTests(LiveCase):
    def test_omp_host_shell_and_user_children_default_pipe_xfsz_then_clean_shutdown(self):
        probe_dir = Path(tempfile.mkdtemp(prefix="p27c-omp-"))
        self.addCleanup(shutil.rmtree, probe_dir, True)
        wrapper = probe_dir / "omp"
        wrapper.write_text("#!/bin/sh\n"
                           f"cat /proc/$$/status > {shlex.quote(str(probe_dir))}/status-$$ 2>/dev/null\n"
                           f"exec {shlex.quote(OMP)} \"$@\"\n")
        wrapper.chmod(0o700)
        foreign = subprocess.Popen(["/bin/sh", "-c", "sleep 600 & exec sleep 600"], start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        foreign_ids: dict[int, int] = {}
        deadline = time.monotonic() + 5
        while len(foreign_ids) < 2 and time.monotonic() < deadline:
            foreign_ids = {int(n): ticks(int(n)) for n in os.listdir("/proc")
                           if n.isdecimal() and _sid(int(n)) == foreign.pid}
            time.sleep(0.05)

        def reap_foreign():
            for pid, start in foreign_ids.items():
                kill_exact(pid, start)
            foreign.wait(10)
        self.addCleanup(reap_foreign)
        live = self.live(str(wrapper))
        client = self.client(live)
        snapshot = client.snapshot()
        # What the backend handed to each OMP at exec.
        for pane_id in (PaneId.MANAGER_OMP, PaneId.WORKER_OMP):
            pid = snapshot["panes"][pane_id.value]["process"]["pid"]
            recorded = masks((probe_dir / f"status-{pid}").read_text())
            self.assertEqual(recorded["SigIgn"] & PIPE_XFSZ, 0, f"{pane_id} exec'd with SIG_IGN: {recorded}")
            self.assertEqual(recorded["SigBlk"] & PIPE_XFSZ, 0, f"{pane_id} exec'd with blocked: {recorded}")
        self.assertTrue(snapshot["bridge"]["worker"]["pid_matches_pane"])
        # Host shell, a user-launched child and an interactive pipeline.
        shell_pid = snapshot["panes"]["host_shell"]["process"]["pid"]
        shell_masks = masks(Path(f"/proc/{shell_pid}/status").read_text())
        self.assertEqual(shell_masks["SigIgn"] & PIPE_XFSZ, 0, shell_masks)
        self.assertEqual(shell_masks["SigBlk"] & PIPE_XFSZ, 0, shell_masks)
        d = live.project
        self.wait_mode(client, "manual_prompt")
        self.run_line(client, f"sh -c 'cat /proc/$$/status' > {d}/child.status; "
                              f"s=$(date +%s%N); (yes; echo $? > {d}/pipe) | head -1 > /dev/null; "
                              f"e=$(date +%s%N); echo $(( (e - s) / 1000000 )) > {d}/ms; "
                              f"(ulimit -f 1; head -c 65536 /dev/zero > {d}/big; echo $? > {d}/xfsz) 2>/dev/null")
        self.wait_path(client, d / "xfsz")
        child = masks((d / "child.status").read_text())
        self.assertEqual(child["SigIgn"] & PIPE_XFSZ, 0, child)
        self.assertEqual(child["SigBlk"] & PIPE_XFSZ, 0, child)
        self.assertEqual((d / "pipe").read_text().strip(), "141", "yes not killed by SIGPIPE in the host shell")
        self.assertLess(int((d / "ms").read_text()), 5000)
        self.assertEqual((d / "xfsz").read_text().strip(), "153", "writer not killed by SIGXFSZ in the host shell")
        # P2-2 real path: worker OMP dies (exact identity), is reaped by the
        # backend, then a confirmed shutdown runs; the foreign session survives.
        worker = snapshot["panes"]["worker_omp"]["process"]
        self.assertTrue(kill_exact(worker["pid"], worker["start_ticks"]))
        wait_client(client, lambda s: s["panes"]["worker_omp"]["alive"] is False, 15)
        client.detach()
        client.close()
        finish(self, live)  # confirmed shutdown + leak check (also runs again as cleanup, idempotent)
        self.assertEqual({p: s for p, s in foreign_ids.items() if ticks(p) == s}, foreign_ids,
                         "shutdown signalled an unrelated session")


def _sid(pid: int) -> int | None:
    try:
        return os.getsid(pid)
    except OSError:
        return None


if __name__ == "__main__":
    unittest.main()
