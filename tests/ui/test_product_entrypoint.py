"""Real entrypoint smoke: `python -m workbench attach` (product UI) against a real backend
that runs a stub OMP (no provider, no credentials). Detach and reattach keep the backend."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pyte  # noqa: E402
from test_product_pty import P, SRC, UiProcess  # noqa: E402

STUB = '''#!/usr/bin/env python3
import os, sys, tty
if "--version" in sys.argv:
    print("omp/18.2.10"); sys.exit(0)
role = os.environ.get("WORKBENCH_G3_ROLE", "?")
tty.setraw(0)
sys.stdout.write(f"STUB-OMP {role} ready\\r\\n> "); sys.stdout.flush()
while True:
    data = os.read(0, 4096)
    if not data:
        break
    sys.stdout.write(f"[{role} got {data!r}]\\r\\n> "); sys.stdout.flush()
'''
ENV = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
       "TERM": "xterm-256color"}
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"


class EntrypointSmoke(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cw06-entry-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.omp = self.root / "omp"
        self.omp.write_text(STUB)
        self.omp.chmod(0o755)
        self.data = self.root / "d"
        self.addCleanup(self.stop_backend)

    def cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, "-c", MAIN, *args], env=ENV, cwd=self.root, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def stop_backend(self):
        result = self.cli("shutdown", "--data-dir", str(self.data), "--yes")
        for entry in Path("/proc").iterdir():  # exact-identity fallback: only processes naming our temp root
            if entry.name.isdigit():
                try:
                    cmdline = (entry / "cmdline").read_bytes()
                except OSError:
                    continue
                if str(self.root).encode() in cmdline and int(entry.name) != os.getpid():
                    try:
                        os.kill(int(entry.name), signal.SIGKILL)
                    except OSError:
                        pass

    def attach(self):
        master, slave = os.openpty()
        UiProcess.set_size(master, (30, 120))
        ui = UiProcess.__new__(UiProcess)
        ui.proc = subprocess.Popen([sys.executable, "-c", MAIN, "attach", "--data-dir", str(self.data)],
                                   stdin=slave, stdout=slave, stderr=slave, env=ENV, start_new_session=True)
        os.close(slave)
        ui.fd, ui.raw = master, bytearray()
        ui.screen = pyte.Screen(120, 30)
        ui.stream = pyte.ByteStream(ui.screen)
        self.addCleanup(ui.close)
        return ui

    def test_product_ui_attach_detach_reattach(self):
        # backend stays in `starting` (stub OMP never loads the bridge); the UI attaches regardless.
        started = self.cli("start", "--data-dir", str(self.data), "--omp", str(self.omp), "--no-attach",
                           "--timeout", "3")
        self.assertIn("starting backend", started.stdout)
        first = self.attach()
        self.assertTrue(first.until(lambda: "STUB-OMP manager ready" in first.text()
                                    and "STUB-OMP worker ready" in first.text()), first.text())
        self.assertIn("focus: MANAGER OMP", first.text())
        first.send(b"/help\r")
        self.assertTrue(first.until(lambda: "manager got b'/help\\r'" in first.text()), first.text())
        first.send(P + b"2" + "한글".encode())
        self.assertTrue(first.until(lambda: "worker got" in first.text()), first.text())
        first.send(P + b"d")
        self.assertEqual(0, first.wait_exit())
        self.assertIn(b"detached; backend keeps running", bytes(first.raw))
        status = self.cli("status", "--data-dir", str(self.data), "--json")
        self.assertEqual(0, status.returncode, "backend must survive UI detach")
        second = self.attach()
        self.assertTrue(second.until(lambda: "manager got b'/help" in second.text()), second.text())
        self.assertEqual(1, second.text().count("manager got b'/help"))  # replay, no duplicate delivery
        second.send(P + b"d")
        self.assertEqual(0, second.wait_exit())
        self.assertEqual(0, self.cli("status", "--data-dir", str(self.data), "--json").returncode)


if __name__ == "__main__":
    unittest.main()
