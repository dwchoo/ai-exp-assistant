"""Bounded takeover observations on the current combined Bash/dash candidate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.gates.g2_shell import live_input_jobs_probe as inputs
from workbench.terminal.shell_g2.control_probe import ControlWaitProbe

combined = inputs.combined
prototype = inputs.prototype


def controller_source(shell: str) -> str:
    source = inputs.controller_source(shell)
    source = source.replace(
        '    while :; do\n        if IFS= read',
        '''    while :; do
        if [ "$WB_TAKEOVER_HOLD" = after_send ]; then
            WB_TAKEOVER_HOLD=none
            __b_emit BEFORE_ACCEPT
            IFS= read -r __b_gate <&7 || return 1
        fi
        if IFS= read''',
    ).replace(
        '                    __b_emit ACCEPT',
        '''                    __b_emit ACCEPT
                    if [ "$WB_TAKEOVER_HOLD" = after_accept ]; then
                        WB_TAKEOVER_HOLD=none
                        __b_emit ACCEPT_BARRIER
                        IFS= read -r __b_gate <&7 || return 1
                    fi
''',
    ).replace(
        '                    __b_emit "TAKEOVER_ACK:$$"',
        '''                    if [ "$WB_TAKEOVER_ACK" = lost ]; then
                        __b_emit ACK_DROPPED
                    else
                        if [ "$WB_TAKEOVER_ACK" = delayed ]; then
                            __b_emit ACK_BARRIER
                            IFS= read -r __b_gate <&7 || return 1
                        fi
                        __b_emit "TAKEOVER_ACK:$$"
                    fi''',
    )
    return source


class TakeoverSession(inputs.InputJobsSession):
    """One-request takeover bridge exercising the shared foreground writer."""

    confirm_foreground_takeover = ControlWaitProbe.confirm_foreground_takeover
    send_confirmed_foreground = ControlWaitProbe.send_confirmed_foreground

    def _control_init_source(self) -> str:
        return controller_source(self.choice.executable)

    def __init__(self, *args, **kwargs) -> None:
        self.sent_id = None
        self.takeover_requested = False
        self.confirmed_target = None
        self.waiting = False
        self.manual_prompt_confirmed = False
        self._takeover_sent = False
        self._ack_seen = False
        self.takeover_writes = 0
        self.run_writes = 0
        self.child_identity = None
        self.supervisor_pid = None
        self.main_returned = False
        self._control_entered = False
        super().__init__(*args, **kwargs)

    def send_user(self, data: bytes) -> None:
        with self.boundary.lock:
            self._drain(timeout=0)
            if self._control_entered and not self.manual_prompt_confirmed:
                self.send_confirmed_foreground(data)
            elif self.manual_prompt_confirmed:
                # A failed automatic/write-race outcome stays unknown. A
                # separately verified same-parent manual prompt permits user
                # input, never another automatic RUN or request replay.
                prototype.ShellProcess.send_user(self, data)
            else:
                super().send_user(data)

    def dispatch_control(self, script: str, *, generation: int, owner_epoch: int) -> None:
        if self.takeover_requested or self.sent_id is not None:
            raise prototype.UnsafeShellState("takeover latched or request already sent")
        # Record before writing: a partial/failed send must never be retried.
        self.sent_id = "input-jobs"
        self.waiting = False
        try:
            super().dispatch_control(script, generation=generation, owner_epoch=owner_epoch)
        except BaseException:
            self.boundary.fail_closed()
            raise
        self.run_writes += 1

    def request_takeover(self) -> str:
        with self.boundary.lock:
            self.takeover_requested = True
            self.boundary.owner_change("user")
            self._send_takeover_if_safe()
            return "not_sent" if self.sent_id is None else "unknown"

    def _send_takeover_if_safe(self) -> None:
        if not self.takeover_requested or self._takeover_sent or not self.waiting:
            return
        self._takeover_sent = True
        self.takeover_writes += 1
        try:
            combined.boundary.flush_queued_pty(self)
            request = b"TAKEOVER\n"
            if os.write(self._request_fd, request) != len(request):
                raise OSError("partial takeover write; outcome unknown")
        except OSError:
            self.boundary.fail_closed()
            raise

    def _on_control_event(self, event: str) -> None:
        super()._on_control_event(event)
        if event == f"HANDOFF:{self.pid}":
            self._control_entered = True
            self.manual_prompt_confirmed = False
            self._ack_seen = False
        elif event == "START":
            self._events.append("START:input-jobs")
        elif event.startswith("SUPERVISOR:"):
            self.supervisor_pid = int(event.split(":")[1])
        elif event.startswith("CHILD:"):
            pid = int(event.split(":")[1])
            try:
                self.child_identity = (pid, combined.proc_fields(pid)[2])
            except FileNotFoundError:
                self.child_identity = None
        elif event.startswith("MAIN_RETURN:"):
            self.main_returned = True
            self.confirmed_target = None
        elif event.startswith(f"WAIT:{self.pid}:"):
            self.waiting = self.boundary.active_command is None
            self._send_takeover_if_safe()
        elif event == f"TAKEOVER_ACK:{self.pid}" and self._takeover_sent:
            self._ack_seen = True
        elif event == "READY" and self.takeover_requested:
            self.manual_prompt_confirmed = self._ack_seen

    def _current_foreground_child_group(self) -> int | None:
        if self.child_identity is None or self.main_returned:
            return None
        pid, started = self.child_identity
        try:
            fields = combined.proc_fields(pid)
            if fields[2] != started or fields[3] in {"X", "Z"}:
                return None
            group = os.getpgid(pid)
            return group if group == self._foreground_group() else None
        except (FileNotFoundError, ProcessLookupError):
            return None


def reject(call) -> None:
    try:
        call()
    except prototype.UnsafeShellState:
        return
    raise AssertionError("unsafe takeover operation was accepted")


def reject_user_without_write(session: TakeoverSession, data: bytes) -> None:
    with patch.object(session, "_write_all") as pty_write, patch.object(os, "write") as raw_write:
        reject(lambda: session.send_user(data))
        pty_write.assert_not_called()
        raw_write.assert_not_called()


def release_input(session: TakeoverSession, since: int) -> None:
    combined.wait(session, "INPUT_BARRIER", since)
    assert not session.manual_prompt_confirmed and session.takeover_writes == 0
    combined.boundary.flush_queued_pty(session)
    assert session.supervisor_pid is not None
    os.kill(session.supervisor_pid, signal.SIGUSR1)
    combined.wait(session, "INPUT_RELEASED", since)
    combined.wait(session, "RETURN:0", since)


def case(shell: str, name: str, *, partial_negative: bool = False) -> dict:
    if partial_negative and name not in {"partial_race", "eagain_race", "zero_race"}:
        raise ValueError("write-retry negative control requires a write race")
    pids = []
    with tempfile.TemporaryDirectory(prefix="cw03-takeover-") as directory, \
         TakeoverSession(prototype.ShellChoice(
             "bash" if Path(shell).name == "bash" else "sh", shell
         ), control_wait=True) as session:
        pids.append(session.pid)
        combined.wait(session, "READY", 0)
        session.take_user_control()
        hold = name if name in {"after_send", "after_accept"} else "none"
        ack = {"ack_lost": "lost", "ack_delayed": "delayed"}.get(name, "normal")
        session.send_user((f"export WB_TAKEOVER_HOLD={hold} WB_TAKEOVER_ACK={ack} "
                           f"BOUNDARY_TRAPS={shlex.quote(directory + '/traps')} "
                           f"BOUNDARY_TRAPS_AFTER={shlex.quote(directory + '/traps-after')}\n").encode())
        combined.wait(session, "READY", 1)
        since = len(session.events)
        session.send_user(b"wb-handoff\n")
        combined.wait(session, f"WAIT:{session.pid}:", since)
        epoch = session.return_to_manager()
        marker = Path(directory) / "spill"
        received = Path(directory) / "received"
        state = "not_sent"
        if name == "before_send":
            state = session.request_takeover()
            assert state == "not_sent" and not session._ack_seen
            assert not session.manual_prompt_confirmed
            reject(lambda: session.dispatch_control(":", generation=session.generation, owner_epoch=epoch))
        else:
            child = ("import os; os.write(9,b'READER_READY\\n'); "
                     f"data=os.read(0,6); open({str(received)!r},'wb').write(data)")
            script = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(child)}"
            session.dispatch_control(script, generation=session.generation, owner_epoch=epoch)
            if name == "after_send":
                assert "ACCEPT" not in session.events[since:]
                state = session.request_takeover()
                reject(lambda: session.dispatch_control(":", generation=session.generation, owner_epoch=epoch))
                os.write(session._recovery_fd, b"release\n")
            elif name == "after_accept":
                combined.wait(session, "ACCEPT_BARRIER", since)
                assert "START" not in session.events[since:]
                state = session.request_takeover()
                reject(lambda: session.dispatch_control(":", generation=session.generation, owner_epoch=epoch))
                os.write(session._recovery_fd, b"release\n")
            combined.wait(session, "READER_READY", since)
            inputs.assert_canonical_run(session, since)
            assert session.child_identity is not None
            pids.extend((session.supervisor_pid, session.child_identity[0]))
            if name not in {"after_send", "after_accept"}:
                state = session.request_takeover()
            assert state == "unknown" and not session.manual_prompt_confirmed
            reject(lambda: session.dispatch_control(":", generation=session.generation, owner_epoch=epoch))
            reject(lambda: session.send_confirmed_foreground(b"unconfirmed\n"))
            reject_user_without_write(session, b"unconfirmed\n")
            session.confirm_foreground_takeover()
            stale_line = f": > {shlex.quote(str(marker))}\n".encode()
            if name == "exit_race":
                os.kill(session.child_identity[0], signal.SIGTERM)
                combined.wait(session, "INPUT_BARRIER", since)
                reject(lambda: session.send_confirmed_foreground(stale_line))
                combined.boundary.flush_queued_pty(session)
                os.kill(session.supervisor_pid, signal.SIGUSR1)
                combined.wait(session, "INPUT_RELEASED", since)
                combined.wait(session, "RETURN:143", since)
                # Nonzero lifecycle stays unknown; do not open the manual prompt.
                assert not session.manual_prompt_confirmed and session.takeover_writes == 0
                assert session.boundary.needs_review
            elif name in {"partial_race", "eagain_race", "zero_race"}:
                original_write = os.write
                writes = []

                def short_write(fd: int, data) -> int:
                    if fd != session.master_fd:
                        return original_write(fd, data)
                    writes.append(bytes(data))
                    if len(writes) == 1:
                        if name == "partial_race":
                            written = original_write(fd, bytes(data[:6]))
                        else:
                            # Independent fixture input ends the reader while
                            # the attempted product write makes zero progress.
                            original_write(fd, b"first\n")
                            written = 0
                        release_input(session, since)
                        combined.wait(session, f"TAKEOVER_ACK:{session.pid}", since)
                        combined.wait(session, "READY", since)
                        if name == "eagain_race":
                            raise BlockingIOError("injected first-write EAGAIN")
                        return written
                    return original_write(fd, data)

                with patch.object(os, "write", side_effect=short_write):
                    payload = b"first\n" + stale_line if name == "partial_race" else stale_line
                    if partial_negative:
                        session.send_confirmed_foreground(payload)
                    else:
                        reject(lambda: session.send_confirmed_foreground(payload))
                if partial_negative:
                    assert len(writes) > 1, "negative control did not send unsafe remaining bytes"
                else:
                    assert len(writes) == 1, "remaining bytes were redirected after target exit"
            else:
                session.send_user(b"first\n" + stale_line)
                release_input(session, since)
                assert received.read_bytes() == b"first\n"
        reject(lambda: session.dispatch_control(":", generation=session.generation, owner_epoch=epoch))
        if name == "ack_delayed":
            combined.wait(session, "ACK_BARRIER", since)
            assert not session.manual_prompt_confirmed and not session._ack_seen
            reject_user_without_write(session, f": > {shlex.quote(str(marker))}\n".encode())
            session.request_takeover()
            assert session.takeover_writes == 1
            os.write(session._recovery_fd, b"release\n")
        if name == "ack_lost":
            combined.wait(session, "ACK_DROPPED", since)
            combined.wait(session, "READY", since)
            session.request_takeover()
            assert not session.manual_prompt_confirmed and session.takeover_writes == 1
            reject_user_without_write(session, f": > {shlex.quote(str(marker))}\n".encode())
        elif name != "exit_race":
            combined.wait(session, f"TAKEOVER_ACK:{session.pid}", since)
            combined.wait(session, "READY", since)
            assert session.manual_prompt_confirmed
            normal_since = len(session.events)
            session.send_user(b"__b_emit NEW_INPUT\n")
            combined.wait(session, "NEW_INPUT", normal_since)
            combined.wait(session, "READY", normal_since)
        if partial_negative:
            assert marker.exists(), "partial-write negative control did not leak at the parent prompt"
        else:
            assert not marker.exists(), "stale input escaped at the actual parent prompt"
        session.request_takeover()
        assert session.run_writes == (0 if name == "before_send" else 1)
        assert session.takeover_writes == (0 if name == "exit_race" else 1)
        assert session.events[since:].count("ACCEPT") == session.run_writes
        assert session.events[since:].count("START") == session.run_writes
        if name not in {"before_send", "exit_race"}:
            later = session.events[since:]
            assert later.index("INPUT_BARRIER") < later.index("INPUT_RELEASED")
            if name != "ack_lost":
                assert later.index("INPUT_RELEASED") < later.index(f"TAKEOVER_ACK:{session.pid}")
        result = {"shell": Path(shell).name, "case": name, "request_state": state,
                  "manual_prompt_confirmed": session.manual_prompt_confirmed,
                  "run_writes": session.run_writes, "takeover_writes": session.takeover_writes,
                  "marker_absent": not marker.exists(), "owned_pids": pids}
    assert not any(Path(f"/proc/{pid}").exists() for pid in pids), pids
    return result


def run() -> list[dict]:
    results = []
    for shell in (shutil.which("bash"), shutil.which("dash")):
        if shell is None:
            raise RuntimeError("Bash and dash required")
        for name in ("before_send", "after_send", "after_accept", "after_start",
                     "foreground", "exit_race", "partial_race", "eagain_race", "zero_race",
                     "ack_lost", "ack_delayed"):
            results.append(case(shell, name))
    return results


if __name__ == "__main__":
    print(json.dumps({"results": run(), "unknown": [
        "production adapter integration", "manual daemons before supervision",
    ]}, indent=2))
