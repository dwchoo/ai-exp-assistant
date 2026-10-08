"""Shared helpers for the CW-19 unit tests: owned temp dirs and owned child processes only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from workbench.backend.paths import DataLayout, ensure_private_dir, write_private_json  # noqa: E402
from workbench.runtime.process_evidence import LinuxProcessProbe  # noqa: E402


def ticks(pid: int) -> int | None:
    try:
        return LinuxProcessProbe.start_ticks(pid)
    except OSError:
        return None


class Owned:
    """Processes this test started; cleanup kills only those exact identities (pidfd + start ticks)."""

    def __init__(self) -> None:
        self.children: list[subprocess.Popen] = []
        self.extra: list[tuple[int, int]] = []

    def spawn(self, script: str, *, new_session: bool = False) -> subprocess.Popen:
        child = subprocess.Popen(["/bin/sh", "-c", script], start_new_session=new_session,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        deadline = time.monotonic() + 5
        while ticks(child.pid) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return child

    def remember(self, pid: int) -> None:
        start = ticks(pid)
        if start:
            self.extra.append((pid, start))

    def cleanup(self) -> None:
        for pid, start in self.extra:
            kill_exact(pid, start)
        for child in self.children:
            if child.poll() is None:
                start = ticks(child.pid)
                if start:
                    kill_exact(child.pid, start)
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                pass


def kill_exact(pid: int, start: int) -> None:
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if ticks(pid) == start:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


def session_members(session: int) -> list[int]:
    members = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fields = Path(f"/proc/{name}/stat").read_bytes().rsplit(b") ", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[3]) == session and int(name) != session and fields[0] not in (b"Z", b"X"):
            members.append(int(name))
    return members


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


class TempData:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cw19-unit-", dir="/tmp"))
        self.layout = DataLayout(ensure_private_dir(self.root / "d"))
        ensure_private_dir(self.layout.workflow)

    def previous(self, record: dict) -> None:
        write_private_json(self.layout.record, record)

    def handoffs(self, records: list[dict]) -> Path:
        path = self.layout.workflow / "handoffs.jsonl"
        with open(path, "w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")
        os.chmod(path, 0o600)
        return path

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def ref(pid: int, start: int, role: str = "x") -> dict:
    return {"role": role, "pid": pid, "start_ticks": start, "owner_epoch": 1}
