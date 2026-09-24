from __future__ import annotations

import hashlib
import io
import os
import sys
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workbench.terminal.vt_g1.screen import TerminalScreen, make_stream
from workbench.terminal.vt_g1.session import PtySession
from workbench.ui.terminal_g1.input_router import InputRouter
from workbench.ui.terminal_g1 import app as g1_app


class _RecordingWindow:
    def __init__(self) -> None:
        self.text: list[str] = []

    def getmaxyx(self) -> tuple[int, int]:
        return (24, 90)

    def keypad(self, _enabled: bool) -> None:
        pass

    def nodelay(self, _enabled: bool) -> None:
        pass

    def erase(self) -> None:
        pass

    def addnstr(self, _row: int, _column: int, value: str, _width: int, *_attrs: int) -> None:
        self.text.append(value)

    def addstr(self, _row: int, _column: int, value: str, _attrs: int) -> None:
        self.text.append(value)

    def addch(self, *_args: object) -> None:
        pass

    def move(self, *_args: int) -> None:
        pass

    def refresh(self) -> None:
        pass


def _run_fake_ui(window: _RecordingWindow, sessions: list[object], selector: object, read_source: object) -> str:
    output = io.StringIO()
    with ExitStack() as stack:
        stack.enter_context(patch.object(g1_app, "selectors_module", SimpleNamespace(DefaultSelector=lambda: selector, EVENT_READ=1)))
        stack.enter_context(patch.object(g1_app, "_ColorPairs", return_value=SimpleNamespace(pair=lambda *_args: 0)))
        stack.enter_context(patch.object(g1_app, "_resize_pending", False))
        for name in ("noecho", "raw", "curs_set", "color_pair"):
            stack.enter_context(patch.object(g1_app.curses, name, return_value=0))
        for name in ("ACS_ULCORNER", "ACS_LLCORNER", "ACS_URCORNER", "ACS_LRCORNER", "ACS_HLINE", "ACS_VLINE"):
            stack.enter_context(patch.object(g1_app.curses, name, 0, create=True))
        stack.enter_context(patch.object(g1_app.os, "read", side_effect=read_source))
        stack.enter_context(patch.object(g1_app.sys, "stdin", SimpleNamespace(fileno=lambda: 0)))
        stack.enter_context(patch.object(g1_app.sys, "stdout", output))
        g1_app._run_ui(window, SimpleNamespace(scrollback=10), sessions)
    return output.getvalue() + " ".join(window.text)


class _ScriptedSession:
    CAP = 2 * 1024 * 1024

    def __init__(self, master_fd: int, *, pending: int = 0, output: bytes = b"", acceptances: list[int] | None = None) -> None:
        self.master_fd = master_fd
        self.pid = master_fd
        self.pending_write_bytes = pending
        self.output = output
        self.acceptances = list(acceptances or [])
        self.received = bytearray()

    def resize(self, _rows: int, _columns: int) -> None:
        pass

    def poll(self) -> None:
        return None

    def read_available(self) -> bytes:
        data, self.output = self.output, b""
        return data

    def write(self, data: bytes) -> int:
        limit = self.acceptances.pop(0) if self.acceptances else self.CAP - self.pending_write_bytes
        accepted = min(len(data), max(0, limit))
        self.received.extend(data[:accepted])
        self.pending_write_bytes += accepted
        return accepted

    def write_frame(self, frame: bytes) -> bool:
        if len(frame) > self.CAP - self.pending_write_bytes:
            return False
        self.received.extend(frame)
        self.pending_write_bytes += len(frame)
        return True

    def flush_writes(self) -> int:
        return 0


class _UiLoopLimit(AssertionError):
    pass


class _ScriptedSelector:
    def __init__(self, session: _ScriptedSession, chunks: list[bytes], *, exit_when: object = None) -> None:
        self.session = session
        self.chunks = chunks
        self.exit_when = exit_when
        self.registered: set[int] = set()
        self.calls = 0

    def register(self, fd: int, _mask: int, _data: str) -> None:
        self.registered.add(fd)

    def unregister(self, fd: int) -> None:
        self.registered.remove(fd)

    def select(self, _timeout: float) -> list[tuple[SimpleNamespace, None]]:
        self.calls += 1
        if self.calls > 100:
            raise _UiLoopLimit("UI did not handle input and F10 without waiting for a child drain")
        if self.session.output and self.session.master_fd in self.registered:
            return [(SimpleNamespace(fd=self.session.master_fd, data="pty"), None)]
        if self.exit_when is not None and self.exit_when() and not self.chunks:
            self.chunks.append(b"\x1b[21~")
        if self.chunks and 0 in self.registered:
            return [(SimpleNamespace(fd=0, data="input"), None)]
        return []

    def close(self) -> None:
        pass


class TerminalScreenTests(unittest.TestCase):
    def test_wide_korean_glyph_uses_two_cells_without_losing_next_character(self) -> None:
        screen = TerminalScreen(8, 2)
        make_stream(screen).feed("가X".encode("utf-8"))

        self.assertEqual(screen.buffer[0][0].data, "가")
        self.assertEqual(screen.buffer[0][1].data, "")
        self.assertEqual(screen.buffer[0][2].data, "X")
        self.assertEqual(screen.cursor.x, 3)

    def test_unicode_truecolor_alt_screen_and_restore(self) -> None:
        screen = TerminalScreen(20, 5)
        stream = make_stream(screen)
        stream.feed("main 가".encode())
        stream.feed(b"\x1b[38;2;107;114;128mC")

        self.assertEqual("".join(screen.buffer[0][x].data for x in range(7)), "main 가")
        self.assertEqual(screen.buffer[0][7].fg, "6b7280")
        stream.feed(b"\x1b[?1049hALT")
        self.assertTrue(screen._terminal_control["using_alternate"])
        self.assertEqual("".join(screen.buffer[0][x].data for x in range(3)), "ALT")
        stream.feed(b"\x1b[?1049l")
        self.assertFalse(screen._terminal_control["using_alternate"])
        self.assertEqual(screen.buffer[0][0].data, "m")

    def test_device_status_cursor_and_device_attribute_replies(self) -> None:
        replies: list[bytes] = []
        screen = TerminalScreen(20, 5, reply=replies.append)
        stream = make_stream(screen)
        stream.feed(b"X\x1b[6n\x1b[c\x1b[5n")

        self.assertEqual(replies, [b"\x1b[1;2R", b"\x1b[?6c", b"\x1b[0n"])

    def test_resize_in_alternate_screen_restores_primary_at_new_size(self) -> None:
        screen = TerminalScreen(8, 2)
        stream = make_stream(screen)
        stream.feed(b"MAIN\x1b[?1049hALT")
        screen.resize(lines=4, columns=12)
        stream.feed(b"\x1b[?1049l")

        self.assertEqual((screen.columns, screen.lines), (12, 4))
        self.assertEqual("".join(screen.buffer[0][x].data for x in range(4)), "MAIN")

    def test_scrollback_can_page_and_restore(self) -> None:
        screen = TerminalScreen(8, 2, history=20)
        stream = make_stream(screen)
        stream.feed(b"one\r\ntwo\r\nthree\r\nfour\r\n")
        bottom = "".join(screen.buffer[0][x].data for x in range(3))
        screen.prev_page()
        prior = "".join(screen.buffer[0][x].data for x in range(3))
        screen.next_page()
        restored = "".join(screen.buffer[0][x].data for x in range(3))

        self.assertNotEqual(prior, bottom)
        self.assertEqual(restored, bottom)


class PtyAndInputTests(unittest.TestCase):
    def test_large_paste_drains_losslessly_after_child_resumes_reading(self) -> None:
        body = ("한글 line\n".encode("utf-8") * 100_000)[: 1024 * 1024]
        paste = b"\x1b[200~" + body + b"\x1b[201~"
        expected_digest = hashlib.sha256(paste).hexdigest().encode("ascii")
        script = (
            "import hashlib, os, select, time, tty\n"
            "tty.setraw(0)\n"
            "os.write(1, b'READY\\n')\n"
            "time.sleep(0.3)\n"
            f"remaining = {len(paste)}\n"
            "digest = hashlib.sha256()\n"
            "while remaining:\n"
            "    if not select.select([0], [], [], 3)[0]: raise SystemExit(3)\n"
            "    chunk = os.read(0, min(65536, remaining))\n"
            "    if not chunk: raise SystemExit(4)\n"
            "    digest.update(chunk)\n"
            "    remaining -= len(chunk)\n"
            "os.write(1, b'RESULT:' + digest.hexdigest().encode() + b'\\n')\n"
        )
        session = PtySession([sys.executable, "-c", script])
        output = bytearray()
        try:
            deadline = time.monotonic() + 3
            while b"READY" not in output and time.monotonic() < deadline:
                output.extend(session.read_available())
                time.sleep(0.01)
            self.assertIn(b"READY", output)

            started = time.monotonic()
            session.write(paste)
            self.assertLess(time.monotonic() - started, 0.3)
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and session.poll() is None:
                session.flush_writes()
                output.extend(session.read_available())
                time.sleep(0.005)
            output.extend(session.read_available())
            self.assertEqual(session.poll(), 0)
            self.assertIn(b"RESULT:" + expected_digest, output)
            self.assertEqual(session.pending_write_bytes, 0)
        finally:
            session.close()

    def test_repeated_input_to_blocked_child_reports_backpressure_at_small_cap(self) -> None:
        script = (
            "import os, time, tty\n"
            "tty.setraw(0)\n"
            "os.write(1, b'READY')\n"
            "time.sleep(30)\n"
        )
        session = PtySession([sys.executable, "-c", script])
        try:
            deadline = time.monotonic() + 2
            output = bytearray()
            while b"READY" not in output and time.monotonic() < deadline:
                output.extend(session.read_available())
                time.sleep(0.01)
            self.assertIn(b"READY", output)

            cap = 2 * 1024 * 1024
            accepted = 0
            rejected = False
            chunk = b"x" * (1024 * 1024)
            for _ in range(4):
                started = time.monotonic()
                try:
                    result = session.write(chunk)
                except (BufferError, BlockingIOError):
                    rejected = True
                    break
                self.assertLess(time.monotonic() - started, 0.3)
                if result is False:
                    rejected = True
                    break
                received = result if type(result) is int else len(chunk)
                accepted += received
                if received < len(chunk):
                    rejected = True
                self.assertLessEqual(session.pending_write_bytes, cap)
                self.assertGreaterEqual(
                    session.pending_write_bytes,
                    accepted - 65536,
                    "accepted input disappeared before the child read it",
                )
                if rejected:
                    break
            self.assertTrue(rejected, "blocked child accepted input beyond the queue cap")
        finally:
            session.close()

    def test_real_pty_query_reply_uses_child_input_not_display_text(self) -> None:
        script = (
            "import os, select, tty\n"
            "tty.setraw(0)\n"
            "os.write(1, b'\\x1b[6n')\n"
            "if not select.select([0], [], [], 2)[0]: raise SystemExit(3)\n"
            "reply = os.read(0, 64)\n"
            "os.write(1, b'REPLY:' + reply.hex().encode() + b'\\n')\n"
        )
        session = PtySession([sys.executable, "-c", script])
        screen = TerminalScreen(20, 5, reply=session.write)
        stream = make_stream(screen)
        output = bytearray()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and session.poll() is None:
                data = session.read_available()
                output.extend(data)
                stream.feed(data)
                time.sleep(0.01)
            output.extend(session.read_available())
            self.assertEqual(session.poll(), 0)
            self.assertIn(b"REPLY:1b5b313b3152", output)
        finally:
            session.close()

    def test_real_pty_resize_and_output(self) -> None:
        session = PtySession(["/bin/sh", "-c", "stty size; printf 'PTY_OK\\n'"])
        try:
            session.resize(27, 91)
            deadline = time.monotonic() + 2
            output = bytearray()
            while time.monotonic() < deadline and session.poll() is None:
                output.extend(session.read_available())
                time.sleep(0.01)
            output.extend(session.read_available())
            self.assertIn(b"27 91", output)
            self.assertIn(b"PTY_OK", output)
            self.assertEqual(session.poll(), 0)
        finally:
            session.close()

    def test_focus_key_is_consumed_and_bracketed_paste_is_forwarded(self) -> None:
        router = InputRouter()
        paste = b"\x1b[200~first\nsecond " + "한".encode() + b"\x1b[201~"
        events = router.feed(b"before\x1b[17~" + paste)
        self.assertEqual(events, [b"before", "focus_next", paste])

    def test_bracketed_paste_keeps_literal_ui_key_bytes(self) -> None:
        router = InputRouter()
        paste = b"\x1b[200~" + "한글\n".encode("utf-8") + b"\x1b[17~\x1b[21~\x1b[201~"

        self.assertEqual(router.feed(paste), [paste])

    def test_split_paste_start_after_timeout_never_turns_content_into_ui_keys(self) -> None:
        router = InputRouter()
        events = router.feed(b"\x1b[200", now=1.0)
        events += router.flush(now=1.1)
        events += router.feed(b"~hello\x1b[21~\x1b[201~", now=1.2)

        self.assertEqual([event for event in events if isinstance(event, str)], [])
        self.assertEqual(
            b"".join(event for event in events if isinstance(event, bytes)),
            b"\x1b[200~hello\x1b[21~\x1b[201~",
        )

    def test_split_paste_end_after_timeout_restores_ui_key_handling(self) -> None:
        router = InputRouter()
        events = router.feed(b"\x1b[200~hello\x1b[20", now=1.0)
        events += router.flush(now=1.1)
        events += router.feed(b"1~\x1b[17~", now=1.2)

        self.assertEqual([event for event in events if isinstance(event, str)], ["focus_next"])
        self.assertEqual(
            b"".join(event for event in events if isinstance(event, bytes)),
            b"\x1b[200~hello\x1b[201~",
        )

    def test_large_input_to_nonreading_child_does_not_block_ui(self) -> None:
        script = (
            "import os, time, tty\n"
            "tty.setraw(0)\n"
            "os.write(1, b'READY')\n"
            "time.sleep(30)\n"
        )
        session = PtySession([sys.executable, "-c", script])
        writer: threading.Thread | None = None
        errors: list[BaseException] = []
        try:
            deadline = time.monotonic() + 2
            output = bytearray()
            while b"READY" not in output and time.monotonic() < deadline:
                output.extend(session.read_available())
                time.sleep(0.01)
            self.assertIn(b"READY", output)

            def send() -> None:
                try:
                    session.write(b"x" * (1024 * 1024))
                except BaseException as exc:
                    errors.append(exc)

            writer = threading.Thread(target=send, daemon=True)
            writer.start()
            writer.join(0.3)
            self.assertFalse(writer.is_alive(), "PTY write held the UI for more than 300 ms")
            self.assertEqual(errors, [])
        finally:
            session.close()
            if writer is not None:
                writer.join(2)
                self.assertFalse(writer.is_alive(), "PTY writer did not stop after child close")

    def test_close_reaps_running_child(self) -> None:
        session = PtySession(["/bin/sh", "-c", "sleep 30"])
        try:
            self.assertIsNone(session.poll())
        finally:
            session.close()
        self.assertIsNotNone(session.poll())

    def test_incomplete_function_key_is_forwarded_after_timeout(self) -> None:
        router = InputRouter()
        self.assertEqual(router.feed(b"\x1b[17", now=1.0), [])
        self.assertEqual(router.flush(now=1.1), [b"\x1b[17"])


class PaneLifecycleTests(unittest.TestCase):
    def test_large_valid_paste_is_delivered_once_after_prior_input_and_query_reply(self) -> None:
        body = b"A" * 50_000 + b"\x1b[21~" + b"B" * 50_000
        frame = b"\x1b[200~" + body + b"\x1b[201~"
        reply = b"\x1b[1;1R"
        manager = _ScriptedSession(101)
        sessions = [manager, _ScriptedSession(102), _ScriptedSession(103)]
        chunks = [b"PRE"] + [frame[offset : offset + 16384] for offset in range(0, len(frame), 16384)]
        chunks.append(b"\x1b[21~")
        selector = _ScriptedSelector(manager, chunks)
        source_reads = 0

        def read_source(_fd: int, _limit: int) -> bytes:
            nonlocal source_reads
            data = selector.chunks.pop(0)
            source_reads += 1
            if source_reads == 1:
                manager.output = b"\x1b[6n"
            return data

        _run_fake_ui(_RecordingWindow(), sessions, selector, read_source)

        expected = b"PRE" + reply + frame
        self.assertGreater(len(frame), 65536)
        self.assertEqual(len(manager.received), len(expected))
        self.assertEqual(hashlib.sha256(manager.received).digest(), hashlib.sha256(expected).digest())
        self.assertEqual(selector.chunks, [])

    def test_rejected_paste_is_atomic_and_explained_when_oversize_or_queue_has_no_room(self) -> None:
        cap = _ScriptedSession.CAP
        for name, pending, body in (
            ("larger than 2 MiB", 0, b"x" * (cap + 1)),
            ("less queue room than paste", cap - 4, b"hello"),
        ):
            with self.subTest(name=name):
                manager = _ScriptedSession(101, pending=pending)
                sessions = [manager, _ScriptedSession(102), _ScriptedSession(103)]
                frame = b"\x1b[200~" + body + b"\x1b[201~"
                chunks = [frame[offset : offset + 65536] for offset in range(0, len(frame), 65536)]
                chunks.append(b"\x1b[21~")
                selector = _ScriptedSelector(manager, chunks)
                window = _RecordingWindow()

                try:
                    display = _run_fake_ui(window, sessions, selector, lambda _fd, _limit: selector.chunks.pop(0))
                    exited = True
                except _UiLoopLimit:
                    display = " ".join(window.text)
                    exited = False

                self.assertEqual(len(manager.received), 0, "rejected paste delivered bytes to the child")
                self.assertLessEqual(manager.pending_write_bytes, cap)
                self.assertRegex(display.lower(), r"paste|붙여넣")
                self.assertRegex(display.lower(), r"reject|거절|초과|full|용량|공간")
                self.assertTrue(exited, "F10 remained blocked after paste rejection")

    def test_f10_exits_while_focused_child_input_queue_is_saturated(self) -> None:
        manager = _ScriptedSession(101, pending=_ScriptedSession.CAP)
        sessions = [manager, _ScriptedSession(102), _ScriptedSession(103)]
        selector = _ScriptedSelector(manager, [b"\x1b[21~"])

        try:
            _run_fake_ui(_RecordingWindow(), sessions, selector, lambda _fd, _limit: selector.chunks.pop(0))
        except _UiLoopLimit:
            self.fail("F10 remained blocked by the saturated child input queue")

        self.assertEqual(manager.received, b"")
        self.assertEqual(selector.chunks, [])
        self.assertLess(selector.calls, 100)

    def test_pty_query_replies_retry_zero_and_partial_writes_in_original_order(self) -> None:
        expected = b"\x1b[1;1R\x1b[?6c"
        manager = _ScriptedSession(101, output=b"\x1b[6n\x1b[c", acceptances=[0, 3])
        sessions = [manager, _ScriptedSession(102), _ScriptedSession(103)]
        selector = _ScriptedSelector(manager, [], exit_when=lambda: bytes(manager.received) == expected)

        try:
            _run_fake_ui(_RecordingWindow(), sessions, selector, lambda _fd, _limit: selector.chunks.pop(0))
        except _UiLoopLimit:
            pass

        self.assertEqual(manager.received, expected)
        self.assertEqual(selector.chunks, [], "UI did not finish after complete query replies")

    def test_exited_child_is_removed_from_ui_selector_after_empty_read(self) -> None:
        class FakeSession:
            def __init__(self, master_fd: int) -> None:
                self.master_fd = master_fd
                self.pid = master_fd

            def resize(self, _rows: int, _columns: int) -> None:
                pass

            def read_available(self) -> bytes:
                return b""

            def poll(self) -> int:
                return 0

            def write(self, _data: bytes) -> None:
                pass

        class FakeSelector:
            def __init__(self) -> None:
                self.registered: set[int] = set()
                self.dead_events = 0

            def register(self, fd: int, _mask: int, _data: str) -> None:
                self.registered.add(fd)

            def unregister(self, fd: int) -> None:
                self.registered.remove(fd)

            def select(self, _timeout: float) -> list[tuple[SimpleNamespace, None]]:
                if 101 in self.registered and self.dead_events < 2:
                    self.dead_events += 1
                    return [(SimpleNamespace(fd=101, data="pty"), None)]
                return [(SimpleNamespace(fd=0, data="input"), None)]

            def close(self) -> None:
                pass

        class FakeWindow:
            def getmaxyx(self) -> tuple[int, int]:
                return (24, 90)

            def keypad(self, _enabled: bool) -> None:
                pass

            def nodelay(self, _enabled: bool) -> None:
                pass

        selector = FakeSelector()
        selector_module = SimpleNamespace(DefaultSelector=lambda: selector, EVENT_READ=1)
        fake_stdin = SimpleNamespace(fileno=lambda: 0)
        sessions = [FakeSession(fd) for fd in (101, 102, 103)]
        with (
            patch.object(g1_app, "selectors_module", selector_module),
            patch.object(g1_app, "_draw"),
            patch.object(g1_app, "_ColorPairs"),
            patch.object(g1_app, "_resize_pending", False),
            patch.object(g1_app.curses, "noecho"),
            patch.object(g1_app.curses, "raw"),
            patch.object(g1_app.curses, "curs_set"),
            patch.object(g1_app.os, "read", return_value=b"\x1b[21~"),
            patch.object(g1_app.sys, "stdin", fake_stdin),
            patch.object(g1_app.sys, "stdout", io.StringIO()),
        ):
            g1_app._run_ui(FakeWindow(), SimpleNamespace(scrollback=10), sessions)

        self.assertGreaterEqual(selector.dead_events, 1)
        self.assertNotIn(101, selector.registered)


if __name__ == "__main__":
    unittest.main()
