"""The CW-19 fake OMP fixture keeps reading bridge frames after a deliver whose envelope is a JSON string (the
backend sends ``envelope`` as a string, ``ipc/bridge_g3/mailbox.py``); its reader thread used to die there."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

FAKE_OMP = Path(__file__).with_name("fake_omp.py")


class FakeOmpReaderTests(unittest.TestCase):
    def test_reader_survives_a_string_envelope(self):
        with tempfile.TemporaryDirectory(prefix="fake-omp-", dir="/tmp") as raw:
            root = Path(raw)
            path = str(root / "bridge.sock")
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(1)
            server.settimeout(10)
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "WORKBENCH_G3_ROLE": "worker",
                   "WORKBENCH_G3_GENERATION": "1", "WORKBENCH_G3_BRIDGE_SOCKET": path, "WORKBENCH_G3_TOKEN": "t",
                   "FAKE_PANE_RECORD": str(root / "panes.jsonl"), "FAKE_FRAMES": str(root / "frames.jsonl")}
            proc = subprocess.Popen([sys.executable, str(FAKE_OMP)], env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                conn, _ = server.accept()
                conn.settimeout(10)
                stream = conn.makefile("rwb")
                self.assertEqual(json.loads(stream.readline())["kind"], "hello")
                stream.write(b'{"kind":"hello_ack"}\n')
                envelope = json.dumps({"task_id": "t-1", "workbench_message_id": "m-1"})
                stream.write((json.dumps({"kind": "deliver", "requestId": "r1", "envelope": envelope}) + "\n").encode())
                stream.write(b'{"kind":"probe","requestId":"r2"}\n')
                stream.flush()
                acks = [json.loads(stream.readline()) for _ in range(2)]
                self.assertEqual([(a["requestId"], a["status"]) for a in acks], [("r1", "deferred"), ("r2", "state")])
                record = json.loads((root / "frames.jsonl").read_text().splitlines()[0])
                self.assertEqual((record["frame"]["task_id"], record["frame"]["message_id"]), ("t-1", "m-1"))
                conn.close()
            finally:
                proc.stdin.close()
                proc.wait(timeout=10)
                proc.stderr.close()
                server.close()


if __name__ == "__main__":
    unittest.main()
