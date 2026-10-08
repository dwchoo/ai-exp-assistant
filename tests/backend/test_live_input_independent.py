"""Independent runtime characterization of host-shell user input via ui_v1 (p27-cw17-test-01).

Contract expectation (BRIEF C-AC-08 "수동 입력은 확인된 현재 대상에 전달한다",
C-AC-08 example "Foreground 실험 실행 중 사용자 인수 → 확인된 현재 대상에 수동
입력을 전달", C-AC-19 input/paste): once the user owns the host shell (initially,
or after a confirmed takeover), ordinary interactive input must reach the current
target: plain text, cursor keys (ESC sequences), Backspace and Ctrl-C at the
prompt, and data/Ctrl-C/Ctrl-D for a program the user started in the foreground
(``cat``, ``python3 -q``, ``sleep``). Holding input is only acceptable while the
target is genuinely unknown, and never as a permanent latch on a live prompt.

Every step is recorded (delivered vs rejected + reason) and printed as a table;
set CW17_CHAR_OUT=<file> to also write it as JSON. Each scenario owns a fresh
backend (real entrypoint, real Bash, two real OMP 18.2.10).
"""
import json
import os
import sys
import time
import unittest

from independent_support import (
    LiveBackend, PaneId, UiClient, finish, find_omp, kill_exact, settle, shell_children, shell_view,
    start_no_attach, wait_client, wait_file,
)
from workbench.contracts.ui_v1 import ClientType

OMP = find_omp()
ROWS: list[dict] = []


def tearDownModule():  # noqa: N802 - unittest hook
    if not ROWS:
        return
    lines = ["", "host-shell input characterization (scenario | step | bytes | ok | reason | observed)"]
    for row in ROWS:
        lines.append(f"  {row['scenario']} | {row['step']} | {row['bytes']} | {row['ok']} | "
                     f"{row['reason'] or '-'} | {row['observed']}")
    print("\n".join(lines), file=sys.stderr)
    out = os.environ.get("CW17_CHAR_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as stream:
            json.dump(ROWS, stream, indent=1, ensure_ascii=False)


@unittest.skipUnless(OMP, "real OMP is required (version recorded, not pinned: C-D72 (2))")
class IndependentHostShellInputTests(unittest.TestCase):
    def open(self, scenario: str):
        live = LiveBackend(OMP, path="/usr/bin:/bin")
        owned_extra: list[tuple[int, int]] = []
        self.addCleanup(finish, self, live, owned_extra)
        snapshot = start_no_attach(self, live)
        shell = snapshot["panes"]["host_shell"]["shell"]
        self.assertEqual(shell["kind"], "bash")
        client = UiClient(live.data / "ui.sock")
        self.addCleanup(client.close)
        self.assertTrue(client.attach((30, 100))["ok"])
        self.scenario, self.client, self.live = scenario, client, live
        self.shell_pid = shell["parent"]["pid"]
        return live, client

    def send(self, step: str, data: bytes, *, wait: float = 1.0) -> dict:
        result = self.client.input(PaneId.HOST_SHELL, data)
        settle(self.client, wait)
        view = shell_view(self.client.snapshot())
        row = {"scenario": self.scenario, "step": step, "bytes": repr(data)[:60], "ok": result.get("ok"),
               "reason": result.get("reason"), "detail": result.get("detail"), "shell_after": view,
               "observed": ""}
        ROWS.append(row)
        return row

    def note(self, row: dict, observed: str) -> None:
        row["observed"] = observed

    def children(self, name: str) -> dict:
        return {pid: info for pid, info in shell_children(self.shell_pid).items() if info[0] == name}

    def wait_child(self, name: str, present: bool, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.children(name)
            if bool(found) == present:
                return found
            settle(self.client, 0.1)
        return self.children(name)

    def reap(self, name: str) -> None:
        """Test-owned recovery: exact-identity kill of a stuck child the user started."""
        for pid, (_, start) in self.children(name).items():
            kill_exact(pid, start)
        self.wait_child(name, False)
        wait_client(self.client, lambda s: s["panes"]["host_shell"]["shell"]["parent_mode"] != "manual_foreground")

    def expect_delivered(self, row: dict, effect_ok: bool, observed: str) -> None:
        self.note(row, observed)
        with self.subTest(scenario=self.scenario, step=row["step"]):
            self.assertTrue(row["ok"], f"rejected: {row['reason']}: {row['detail']}")
            self.assertTrue(effect_ok, observed)

    # -- scenarios ---------------------------------------------------------
    def test_a_confirmed_takeover_then_text_and_user_foreground_programs(self):
        live, client = self.open("A:takeover+foreground")
        m = live.project / "m-a"
        row = self.send("wb-handoff", b"wb-handoff\r", wait=1.5)
        mode = row["shell_after"]["parent_mode"]
        self.note(row, f"parent_mode={mode}")
        self.assertEqual(mode, "control_wait")
        handoff = client.request(ClientType.HANDOFF)
        self.assertTrue(handoff["ok"], handoff)
        held = self.send("text while manager owns", f"printf M >> {m}\r".encode())
        self.note(held, f"file={m.read_text() if m.exists() else None}")
        self.assertEqual((held["ok"], held["reason"]), (False, "input_owner_manager"))
        request = client.request(ClientType.TAKEOVER_REQUEST)
        self.assertTrue(request["ok"], request)
        settle(client, 1.0)
        confirm = client.request(ClientType.TAKEOVER_CONFIRM)
        ROWS.append({"scenario": self.scenario, "step": "takeover_request+confirm", "bytes": "-",
                     "ok": confirm.get("ok"), "reason": confirm.get("reason"), "detail": confirm.get("detail"),
                     "shell_after": confirm.get("shell"), "observed": "confirmed takeover"})
        self.assertTrue(confirm["ok"], confirm)
        self.assertEqual((confirm["shell"]["input_owner"], confirm["shell"]["takeover_confirmed"],
                          confirm["shell"]["parent_mode"]), ("user", True, "manual_prompt"))

        row = self.send("plain text + Enter", f"printf T >> {m}\r".encode())
        self.expect_delivered(row, wait_file(m, "T", 5, client) == "T", f"file={m.read_text() if m.exists() else None}")

        # cat started by the user in the foreground.
        out = live.project / "m-cat"
        row = self.send("start cat", f"cat > {out}\r".encode())
        cats = self.wait_child("cat", True)
        self.expect_delivered(row, bool(cats), f"cat running={bool(cats)} mode={row['shell_after']['parent_mode']}")
        row = self.send("cat: data line", b"abc\n")
        self.expect_delivered(row, wait_file(out, "abc\n", 3, client) == "abc\n",
                              f"cat file={out.read_text() if out.exists() else None!r}")
        row = self.send("cat: Ctrl-D", b"\x04")
        gone = not self.wait_child("cat", False, 3)
        self.expect_delivered(row, gone, f"cat exited={gone}")
        if not gone:
            self.reap("cat")
        row = self.send("plain text after cat", f"printf U >> {m}\r".encode())
        self.expect_delivered(row, wait_file(m, "TU", 5, client) == "TU", f"file={m.read_text() if m.exists() else None}")

        # python3 REPL started by the user.
        py_out = live.project / "m-py"
        row = self.send("start python3 -q", b"python3 -q\r", wait=2.0)
        pys = self.wait_child("python3", True)
        self.expect_delivered(row, bool(pys), f"python3 running={bool(pys)}")
        row = self.send("python3: statement", f"open({str(py_out)!r}, 'w').write('P')\n".encode(), wait=1.5)
        self.expect_delivered(row, wait_file(py_out, "P", 3, client) == "P",
                              f"py file={py_out.read_text() if py_out.exists() else None!r}")
        row = self.send("python3: Up arrow (history)", b"\x1b[A")
        self.expect_delivered(row, True, "history recall (echo not asserted)")
        row = self.send("python3: Ctrl-C", b"\x03")
        self.expect_delivered(row, bool(self.children("python3")), "KeyboardInterrupt in REPL, REPL stays")
        row = self.send("python3: exit()", b"exit()\n")
        gone = not self.wait_child("python3", False, 3)
        self.expect_delivered(row, gone, f"python3 exited={gone}")
        if not gone:
            self.reap("python3")

        # Ctrl-C to a long-running foreground program.
        row = self.send("start sleep 30", b"sleep 30\r")
        sleeps = self.wait_child("sleep", True)
        self.expect_delivered(row, bool(sleeps), f"sleep running={bool(sleeps)}")
        row = self.send("sleep: Ctrl-C", b"\x03")
        gone = not self.wait_child("sleep", False, 3)
        self.expect_delivered(row, gone, f"sleep interrupted={gone}")
        if not gone:
            self.reap("sleep")
        row = self.send("plain text at end", f"printf V >> {m}\r".encode())
        self.expect_delivered(row, wait_file(m, "TUV", 5, client) == "TUV",
                              f"file={m.read_text() if m.exists() else None}")
        client.detach()

    def test_b_cursor_keys_while_editing_then_more_text(self):
        live, client = self.open("B:arrow keys")
        m = live.project / "m-b"
        row = self.send("type command (no Enter)", f"printf A >> {m}".encode(), wait=0.5)
        self.expect_delivered(row, True, "line pending")
        row = self.send("Left arrow ESC[D", b"\x1b[D", wait=0.5)
        self.expect_delivered(row, True, "-")
        row = self.send("Right arrow ESC[C", b"\x1b[C", wait=0.5)
        self.expect_delivered(row, True, "-")
        row = self.send("Enter", b"\r")
        self.expect_delivered(row, wait_file(m, "A", 5, client) == "A", f"file={m.read_text() if m.exists() else None}")
        row = self.send("Up arrow (history) at prompt", b"\x1b[A", wait=0.5)
        self.expect_delivered(row, True, "-")
        row = self.send("Ctrl-U clears recalled line", b"\x15", wait=0.5)
        self.expect_delivered(row, True, "-")
        settle(client, 3.0)  # a latch must not persist on a live prompt
        row = self.send("plain text after cursor keys", f"printf B >> {m}\r".encode())
        self.expect_delivered(row, wait_file(m, "AB", 5, client) == "AB",
                              f"file={m.read_text() if m.exists() else None} phase={row['shell_after']['phase']}")
        client.detach()

    def test_c_backspace_correction_then_more_text(self):
        live, client = self.open("C:backspace")
        m = live.project / "m-c"
        row = self.send("typo + Backspace + Enter", f"printf B >> {m}X".encode() + b"\x7f\r")
        self.expect_delivered(row, wait_file(m, "B", 5, client) == "B", f"file={m.read_text() if m.exists() else None}")
        settle(client, 3.0)
        row = self.send("plain text after Backspace", f"printf C >> {m}\r".encode())
        self.expect_delivered(row, wait_file(m, "BC", 5, client) == "BC",
                              f"file={m.read_text() if m.exists() else None} phase={row['shell_after']['phase']}")
        client.detach()

    def test_d_ctrl_c_cancels_prompt_line_then_more_text(self):
        live, client = self.open("D:Ctrl-C at prompt")
        m = live.project / "m-d"
        row = self.send("type command (no Enter)", f"printf NO >> {m}".encode(), wait=0.5)
        self.expect_delivered(row, True, "line pending")
        row = self.send("Ctrl-C", b"\x03")
        self.expect_delivered(row, not m.exists(), f"line cancelled={not m.exists()}")
        settle(client, 3.0)
        row = self.send("plain text after Ctrl-C", f"printf C >> {m}\r".encode())
        self.expect_delivered(row, wait_file(m, "C", 5, client) == "C",
                              f"file={m.read_text() if m.exists() else None} phase={row['shell_after']['phase']}")
        client.detach()


if __name__ == "__main__":
    unittest.main()
