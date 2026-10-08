"""A fake OMP for CW-19 backend tests (no model, no provider): registers with the real G3 bridge, answers probes
as idle, acknowledges Workbench notices (recorded to $FAKE_NOTICES) and records other frames (tool results,
deliveries, which it always defers: nothing is ever submitted) to $FAKE_FRAMES. Pane input lines:

- ``model-error`` / ``model-ok`` / ``model-aborted``: a ``model_turn_result`` event like the bridge extension
  sends (no text; aborted sends nothing);
- ``tool <name> <json args>``: one bridge ``tool_request`` (as a model's tool call would);
- ``broker <seconds>``: start a child in its own session that lives on that long after this OMP ends (<0: until
  killed), like OMP 18.8.0's daemon broker; its pid goes to $FAKE_BROKERS;
- ``survive``: ignore SIGHUP and keep running after the PTY hangs up (an OMP that outlives a backend crash);
- ``slow-term <seconds>``: on SIGTERM record the time (to $FAKE_TERMS) and exit only after that long, like an OMP
  aborting a model turn in flight (CW-16 O4).
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("omp/18.2.10")
    sys.exit(0)
if argv[:2] == ["config", "get"]:
    print("[]")
    sys.exit(0)
role = os.environ["WORKBENCH_G3_ROLE"]
session = str(uuid.uuid4())
generation = int(os.environ["WORKBENCH_G3_GENERATION"])
with open(os.environ["FAKE_PANE_RECORD"], "a") as stream:
    stream.write(json.dumps({"role": role, "pid": os.getpid(), "session": session}) + "\n")
sock = socket.socket(socket.AF_UNIX)
sock.connect(os.environ["WORKBENCH_G3_BRIDGE_SOCKET"])
lock = threading.Lock()


def send(frame):
    with lock:
        sock.sendall((json.dumps(frame) + "\n").encode())


send({"kind": "hello", "protocolVersion": 1, "token": os.environ["WORKBENCH_G3_TOKEN"], "role": role,
      "ompSessionId": session, "generation": generation, "pid": os.getpid()})
reader = sock.makefile("rb")
reader.readline()
state = {"kind": "state", "role": role, "sessionId": session, "generation": generation, "idle": True,
         "pending": False, "approvalPending": False, "inFlightToolCount": 0, "editorKnown": True, "editorEmpty": True,
         "paused": False}


def serve():
    for line in reader:
        frame = json.loads(line)
        if frame.get("kind") == "probe":
            send({"kind": "api_ack", "requestId": frame["requestId"], "status": "state", "state": state})
        elif frame.get("kind") == "notice":
            with open(os.environ["FAKE_NOTICES"], "a") as stream:
                stream.write(json.dumps({"role": role, "session": session, "notice": frame["notice"]}) + "\n")
            send({"kind": "api_ack", "requestId": frame["requestId"], "status": "api_accepted"})
        elif frame.get("kind") in ("pause", "resume"):
            send({"kind": "api_ack", "requestId": frame["requestId"],
                  "status": "paused" if frame["kind"] == "pause" else "resumed"})
        elif frame.get("kind") == "deliver":  # never submitted: the backend keeps it pending and retries
            send({"kind": "api_ack", "requestId": frame.get("requestId"), "status": "deferred",
                  "reason": "fake_omp_not_accepting"})
            if os.environ.get("FAKE_FRAMES"):
                envelope = frame.get("envelope") or {}
                if isinstance(envelope, str):  # the backend sends the envelope as a JSON string
                    try:
                        envelope = json.loads(envelope)
                    except ValueError:
                        envelope = {}
                if not isinstance(envelope, dict):
                    envelope = {}
                with open(os.environ["FAKE_FRAMES"], "a") as stream:
                    stream.write(json.dumps({"role": role, "session": session, "pid": os.getpid(), "frame": {
                        "kind": "deliver", "task_id": envelope.get("task_id"),
                        "message_id": envelope.get("workbench_message_id") or envelope.get("message_id")}}) + "\n")
        elif os.environ.get("FAKE_FRAMES"):
            with open(os.environ["FAKE_FRAMES"], "a") as stream:
                stream.write(json.dumps({"role": role, "session": session, "pid": os.getpid(), "frame": frame})
                             + "\n")


threading.Thread(target=serve, daemon=True).start()
print("fake omp", role, flush=True)
survive = False


def commands():
    global survive
    for line in sys.stdin:
        command = line.strip()
        if command == "exit":
            break
        if command == "survive":
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            survive = True
            print("surviving a hangup", flush=True)
            continue
        if command.startswith("slow-term "):
            delay = float(command.split(" ", 1)[1])

            def on_term(*_):
                if os.environ.get("FAKE_TERMS"):
                    with open(os.environ["FAKE_TERMS"], "a") as stream:
                        stream.write(json.dumps({"role": role, "pid": os.getpid(), "at": time.time()}) + "\n")
                time.sleep(delay)
                os._exit(143)

            signal.signal(signal.SIGTERM, on_term)
            print("slow term", delay, flush=True)
            continue
        if command.startswith("broker "):  # CW-16 B3 F1: a daemon broker like OMP 18.8.0's (own session)
            linger = float(command.split(" ", 1)[1])  # seconds it lives on after this OMP ends (<0: until killed)
            code = ("import os, sys, time\nparent = int(sys.argv[1])\nlinger = float(sys.argv[2])\n"
                    "while os.getppid() == parent:\n    time.sleep(0.05)\n"
                    "time.sleep(linger) if linger >= 0 else time.sleep(3600)\n")
            child = subprocess.Popen([sys.executable, "-c", code, str(os.getpid()), str(linger)],
                                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            if os.environ.get("FAKE_BROKERS"):
                with open(os.environ["FAKE_BROKERS"], "a") as stream:
                    stream.write(json.dumps({"role": role, "pid": child.pid}) + "\n")
            print("broker started", child.pid, flush=True)
            continue
        if command.startswith("tool "):
            _, name, args = command.split(" ", 2)
            send({"kind": "tool_request", "requestId": str(uuid.uuid4()), "toolCallId": f"fake-{uuid.uuid4()}",
                  "tool": name, "args": json.loads(args), "sessionId": session, "generation": generation})
            print("tool sent", name, flush=True)
            continue
        if command in ("model-error", "model-ok", "model-aborted"):
            fields = {"model-error": {"ok": False, "stopReason": "error"}, "model-ok": {"ok": True, "stopReason": "stop"}}
            if command in fields:
                send({"kind": "omp_event", "name": "model_turn_result", "sessionId": session, "generation": generation,
                      **fields[command]})
            print("event", command, flush=True)


try:
    commands()
except OSError:  # the PTY hung up
    pass
if survive:  # the PTY hung up (stdin ended): keep running like a process the backend no longer owns
    try:
        sys.stdout = open(os.devnull, "w")
    except OSError:
        pass
    time.sleep(600)
