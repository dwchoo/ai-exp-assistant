"""CW-16 X5 / L-CW19-REBOOT: real guest reboot in the isolated ``wb-reboot`` VM (never the host). Not a unittest module.

Two parts in one file (the host copies this very file into the guest, so the guest code is exactly what the
manifest records):

GUEST DRIVER (runs INSIDE the VM only; phases ``probe | pre | post | pre2 | post2 | post2_extra``)
    Fixed from the L-CW19-REBOOT driver of p27-cw19-vm-02 (``/tmp/cw19-vm-02/driver.py`` sha256 3de6e82e...280ebe,
    itself vm-01 ``driver.source`` sha256 3154e010...f27c0 + the documented post2 change for the F1 positive case).
    The ONLY change is the guest path: ``ROOT`` = ``$CW16_VM_ROOT`` (default ``~/cw16-reboot``). ``post2_extra``
    is the vm-02 same-boot follow-up script (/tmp/cw19-vm-02/extra.py) folded in as a phase. No model/provider:
    fake OMPs (tests/recovery_boot/fake_omp.py), a fake HOME with an empty agent.db (no credential), proxies
    blocked. Every event goes to ROOT/log.jsonl. Only processes under ROOT (started by the driver) are signalled.

HOST ORCHESTRATION (``python tests/integration/vm_reboot_driver.py host [out_dir]``, repo root, host)
    ``start.sh`` (refuses when the VM already runs unless ``WB_VM_REUSE=1``) -> rsync the repo (exclude
    .git .workflow graphify-out __pycache__) -> manifest of ``src/** omp_bridge/** tests/recovery_boot/**`` on host
    and guest (must be equal; the aggregate is comparable with vm-01/vm-02) -> copy this file to the guest ->
    ``probe`` (real guest OMP, no prompt) -> ``pre`` -> guest ``sync; sudo reboot`` -> ``post`` -> ``pre2`` ->
    guest reboot -> ``post2`` -> ``post2_extra`` -> fetch the guest log -> ``stop.sh`` (disk files kept) ->
    :func:`judge` -> ``<out>/vm-reboot-report.json`` (exit 0 only when every check passes).
    ``python tests/integration/vm_reboot_driver.py judge <guest-log.jsonl>`` re-judges a saved guest log.

The host commands run with a from-scratch environment (no TMUX*/HERDR_* of the session this may run in).
"""

import json, os, pty, select, signal, sqlite3, subprocess, sys, time
from pathlib import Path

ROOT = Path(os.environ.get("CW16_VM_ROOT") or (Path.home() / "cw16-reboot"))  # CW-16: guest path only
REPO = Path.home() / "ai-exp-assistant"
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
from workbench.backend.client import UiClient  # noqa: E402
from workbench.contracts.ui_v1 import ClientType  # noqa: E402

PY = sys.executable
BLOCKED = "http://127.0.0.1:9"
STATE = ROOT / ("state.json" if os.environ.get("CW19_BASE", "main") == "main" else f"state-{os.environ['CW19_BASE']}.json")
LOG = ROOT / "log.jsonl"


def setup_env(base: Path, fake: bool):
    home = base / "home"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "SHELL": "/usr/bin/bash", "LANG": "C.UTF-8",
           "TERM": "xterm-256color", "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1",
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".local/state"),
           "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
           **{k: BLOCKED for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}}
    if fake:
        env.update({"FAKE_PANE_RECORD": str(base / "panes.jsonl"), "FAKE_NOTICES": str(base / "notices.jsonl"),
                    "FAKE_FRAMES": str(base / "frames.jsonl")})
    return env


BASE = ROOT / os.environ.get("CW19_BASE", "main")
ENV = setup_env(BASE, True)
DATA = BASE / "home" / "wbdata"
PROJECT = BASE / "project"
FAKE = BASE / "omp"


def log(step, **fields):
    entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "phase": PHASE, "step": step, **fields}
    with LOG.open("a") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(entry, ensure_ascii=False)[:1500], flush=True)


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def cli(*args, timeout=150, env=None, data=None, cwd=None, record=True):
    cmd = [PY, "-m", "workbench", *args, "--data-dir", str(data or DATA)]
    r = subprocess.run(cmd, env=env or ENV, cwd=cwd or (PROJECT if PROJECT.exists() else ROOT), stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=timeout)
    if record:
        log("cli", argv=["python", "-m", "workbench", *args], rc=r.returncode, stdout=r.stdout[-6000:],
            stderr=r.stderr[-2000:])
    return r


def status(data=None, env=None):
    r = cli("status", "--json", timeout=30, data=data, env=env, record=False)
    try:
        v = json.loads(r.stdout)
    except ValueError:
        return None
    return v.get("snapshot") if v.get("running") else None


def wait_status(pred, what, timeout=60, data=None, env=None):
    end = time.monotonic() + timeout
    s = None
    while time.monotonic() < end:
        s = status(data, env)
        if s is not None and pred(s):
            return s
        time.sleep(0.5)
    log("wait_timeout", what=what, last=json.dumps(s)[:3000] if s else None)
    return None


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def tool_results(session):
    return [i["frame"]["result"] for i in lines(BASE / "frames.jsonl")
            if i["session"] == session and i["frame"].get("kind") == "tool_result"]


def call(client, pane, session, tool, args, timeout=25):
    before = len(tool_results(session))
    ok = client.request(ClientType.INPUT, f"tool {tool} {json.dumps(args)}\n".encode(), pane=pane)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        res = tool_results(session)
        if len(res) > before:
            log("tool", pane=pane, tool=tool, args=args, result=res[before])
            return res[before]
        client.pump(0.1)
    log("tool_no_result", pane=pane, tool=tool, args=args, input_ok=ok)
    return None


def attached():
    c = UiClient(DATA / "ui.sock", name="cw19-vm", timeout=10)
    c.attach((30, 120))
    return c


def runs_count():
    con = sqlite3.connect(f"file:{DATA / 'tasks.sqlite3'}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        con.close()


def ticks(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def ours():
    found = {}
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            cmd = Path(f"/proc/{name}/cmdline").read_bytes()
            cwd = os.readlink(f"/proc/{name}/cwd")
        except OSError:
            continue
        if str(ROOT).encode() in cmd or cwd.startswith(str(ROOT)):
            found[int(name)] = (ticks(int(name)), cmd.replace(b"\0", b" ")[:160].decode(errors="replace"))
    return found


def kill_ours():
    for pid, (start, _) in ours().items():
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            continue
        try:
            if ticks(pid) == start:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
        except OSError:
            pass
        finally:
            os.close(fd)


def ui_screen(seconds=6.0, cols=160):
    """Product UI (`python -m workbench attach`) in a PTY; returns the rendered screen text (pyte)."""
    import pyte
    rows = 40
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(PROJECT)
        os.execve(PY, [PY, "-m", "workbench", "attach", "--data-dir", str(DATA)], {**ENV, "COLUMNS": str(cols),
                                                                                   "LINES": str(rows)})
    import fcntl, struct, termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            stream.feed(data)
    start = ticks(pid)
    try:
        pfd = os.pidfd_open(pid)
        if ticks(pid) == start:
            signal.pidfd_send_signal(pfd, signal.SIGTERM)
        os.close(pfd)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    os.close(fd)
    return "\n".join(line.rstrip() for line in screen.display)


def snap_summary(s):
    if not s:
        return None
    task = s.get("task") or {}
    return {"phase": s.get("phase"), "boot": s.get("boot"), "holds": s.get("holds"), "faults": s.get("faults"),
            "automation": s.get("automation"), "startup": s.get("startup"),
            "task": {k: task.get(k) for k in ("task_id", "kind", "status", "run_id", "held_reason")},
            "sessions": {r: (s.get("bridge") or {}).get(r, {}).get("session_id") for r in ("manager", "worker")}}


def git(*args):
    return subprocess.run(["git", *args], cwd=PROJECT, env={**ENV, "GIT_AUTHOR_NAME": "cw19", "GIT_AUTHOR_EMAIL":
                          "cw19@vm", "GIT_COMMITTER_NAME": "cw19", "GIT_COMMITTER_EMAIL": "cw19@vm"},
                          capture_output=True, text=True, check=True).stdout.strip()


def experiment_args(message):
    st = json.loads(STATE.read_text())
    return {"kind": "experiment", "message": message,
            "spec": {"goal": "cw19 reboot exp", "paths": ["outcome.txt"],
                     "execution": {"source": str(PROJECT), "commit": st["commit"],
                                   "command": "printf 'CW19 PASS\\n'; printf PASS > outcome.txt",
                                   "criteria": {"log_contains": "PASS", "result_file": "outcome.txt",
                                                "result_contains": "PASS"},
                                   "environment": ["PATH"], "shell": "bash"}}}


# ------------------------------------------------------------------------------------------------------------
def phase_probe():
    """Real guest OMP 18.2.10 with a fake HOME (empty agent.db, no credential) and blocked proxies: does the
    entrypoint reach ready with the Workbench bridge extension? No prompt is typed."""
    base = ROOT / "probe"
    env = setup_env(base, False)
    home = Path(env["HOME"])
    data = home / "wbdata"
    omp = str(Path.home() / ".local/bin/omp")
    if (base / "project").exists():  # resume after a driver fault: the backend already runs
        r = None
    else:
      (home / ".omp" / "agent").mkdir(parents=True)
      (home / ".omp" / "agent" / "agent.db").write_bytes(b"")
      (base / "project").mkdir()
      r = subprocess.run([PY, "-m", "workbench", "start", "--no-attach", "--omp", omp, "--data-dir", str(data)],
                       env=env, cwd=base / "project", stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=200)
      log("probe_start", rc=r.returncode, stdout=r.stdout[-4000:], stderr=r.stderr[-3000:])
    s = wait_status(lambda s: s.get("phase") in ("ready", "degraded"), "probe ready", timeout=60, data=data, env=env)
    log("probe_status", summary={"phase": s.get("phase"), "reason": s.get("reason"),
                                 "bridge": {k: {kk: vv for kk, vv in (v or {}).items() if kk in
                                                ("registered", "session_id", "state", "generation")}
                                            for k, v in (s.get("bridge") or {}).items()}} if s else None)
    r = subprocess.run([PY, "-m", "workbench", "shutdown", "--yes", "--json", "--data-dir", str(data)], env=env,
                       cwd=base / "project", stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    log("probe_shutdown", rc=r.returncode, stdout=r.stdout[-3000:], stderr=r.stderr[-1000:])
    time.sleep(1)
    left = {p: v for p, v in ours().items() if str(base) in v[1]}
    log("probe_leftover", left=left)


def phase_pre():
    (BASE / "home" / ".omp" / "agent").mkdir(parents=True)
    (BASE / "home" / ".omp" / "agent" / "agent.db").write_bytes(b"")  # fake empty store, no credential
    PROJECT.mkdir()
    (PROJECT / "README").write_text("cw19 reboot project\n")
    git("init", "-q")
    git("add", "README")
    git("commit", "-q", "-m", "init")
    commit = git("rev-parse", "HEAD")
    FAKE.write_text(f"#!{PY}\n" + (REPO / "tests/recovery_boot/fake_omp.py").read_text())
    FAKE.chmod(0o700)
    b = boot_id()
    log("boot_before", boot_id=b)
    r = cli("start", "--no-attach", "--omp", str(FAKE))
    s = wait_status(lambda s: s["phase"] == "ready", "ready")
    log("ready", summary=snap_summary(s))
    manager = s["bridge"]["manager"]["session_id"]
    client = attached()
    try:
        client.request(ClientType.INPUT, b"nohup sleep 3000 >/dev/null 2>&1 &\n", pane="host_shell")
        d = call(client, "manager_omp", manager, "to_worker", {
            "kind": "work", "message": "cw19 reboot work", "spec": {"goal": "cw19 reboot", "paths": ["notes/"]}})
    finally:
        client.close()
    s = wait_status(lambda s: (s.get("task") or {}).get("run_id"), "the work run")
    time.sleep(1.0)
    pre = {"boot_id": b, "commit": commit, "task_id": d and d.get("task_id"), "runs": runs_count(),
           "summary": snap_summary(s), "manager_session": manager,
           "worker_session": s["bridge"]["worker"]["session_id"],
           "boot_json": json.loads((DATA / "boot.json").read_text()),
           "backend_json_processes": {k: {kk: v.get(kk) for kk in ("pid", "start_ticks")}
                                      for k, v in json.loads((DATA / "backend.json").read_text())
                                      .get("processes", {}).items() if isinstance(v, dict)},
           "backend_json_boot_id": json.loads((DATA / "backend.json").read_text()).get("boot_id"),
           "lifecycle_json": json.loads((DATA / "lifecycle.json").read_text())
           if (DATA / "lifecycle.json").exists() else None,
           "notices_count": len(lines(BASE / "notices.jsonl")), "frames_count": len(lines(BASE / "frames.jsonl")),
           "ours": {str(k): v for k, v in ours().items()}}
    STATE.write_text(json.dumps(pre, indent=1))
    log("pre_state", state=pre)
    cli("status")
    os.sync()


def phase_post():
    st = json.loads(STATE.read_text())
    now = boot_id()
    log("boot_after", boot_id_before=st["boot_id"], boot_id_after=now, changed=now != st["boot_id"])
    log("ours_before_start", ours={str(k): v for k, v in ours().items()})
    log("records_on_disk", runs=runs_count(), boot_json=json.loads((DATA / "boot.json").read_text()),
        backend_boot_id=json.loads((DATA / "backend.json").read_text()).get("boot_id"),
        files=sorted(p.name for p in DATA.iterdir()))
    notices0, frames0 = len(lines(BASE / "notices.jsonl")), len(lines(BASE / "frames.jsonl"))
    cli("start", "--no-attach", "--omp", str(FAKE))
    s = wait_status(lambda s: s["phase"] == "ready", "ready after reboot")
    log("ready_after_reboot", summary=snap_summary(s))
    manager = s["bridge"]["manager"]["session_id"]
    worker = s["bridge"]["worker"]["session_id"]
    cli("status")
    screen = ui_screen()
    log("ui_screen_before_confirm", has_boot_wait="재부팅 확인 대기" in screen, screen=screen)
    runs_before = runs_count()
    client = attached()
    try:
        r1 = call(client, "manager_omp", manager, "to_worker",
                  {"kind": "work", "message": "follow-up after reboot", "task_id": st["task_id"]})
        r2 = call(client, "worker_omp", worker, "terminal", {"command": "echo cw19-worker-terminal"})
        r3 = call(client, "manager_omp", manager, "to_worker",
                  {"kind": "work", "message": "cancel after reboot", "task_id": st["task_id"], "cancel": True})
        time.sleep(1.0)
        log("after_cancel", summary=snap_summary(status()))
        r4 = call(client, "manager_omp", manager, "to_worker", experiment_args("new experiment before confirm"))
        # user direct actions (allowed): host shell input, pause / resume, status
        client.request(ClientType.INPUT, f"echo CW19_USER_INPUT > {ROOT}/user_input.txt\n".encode(),
                       pane="host_shell")
        p = client.request(ClientType.PAUSE)
        time.sleep(1.0)
        q = client.request(ClientType.RESUME, reconciled=True)
        log("user_actions", pause=p, resume=q)
        end = time.monotonic() + 70  # > one 60 s review period: nothing automatic may happen
        while time.monotonic() < end:
            client.pump(0.5)
    finally:
        client.close()
    time.sleep(1)
    new_notices = [i for i in lines(BASE / "notices.jsonl")[notices0:]]
    new_deliveries = [i for i in lines(BASE / "frames.jsonl")[frames0:] if i["frame"].get("kind") == "deliver"]
    s = status()
    log("held_window", runs_before=runs_before, runs_now=runs_count(), notices_since_start=new_notices,
        deliveries_since_start=new_deliveries, user_input_file=(ROOT / "user_input.txt").read_text()
        if (ROOT / "user_input.txt").exists() else None, summary=snap_summary(s))
    st["post_held"] = {"r1": r1, "r2": r2, "r3": r3, "r4": r4}
    cli("confirm-boot")  # no tty, no --yes: shows conditions, refused
    cli("confirm-boot", "--json")
    notices1, frames1 = len(lines(BASE / "notices.jsonl")), len(lines(BASE / "frames.jsonl"))
    cli("confirm-boot", "--yes")
    s = wait_status(lambda s: not s["boot"]["confirmation_required"], "confirmed")
    log("after_confirm", summary=snap_summary(s))
    time.sleep(8)
    screen = ui_screen()
    log("ui_screen_after_confirm", has_boot_wait="재부팅 확인 대기" in screen, screen=screen)
    runs_mid = runs_count()
    client = attached()
    try:
        r5 = call(client, "manager_omp", manager, "to_worker", experiment_args("new experiment after confirm"))
        r6 = call(client, "worker_omp", worker, "terminal", {"command": "echo cw19-worker-terminal-after"})
        end = time.monotonic() + 15
        while time.monotonic() < end:
            client.pump(0.5)
    finally:
        client.close()
    log("after_confirm_effects", runs_before=runs_mid, runs_now=runs_count(),
        notices=lines(BASE / "notices.jsonl")[notices1:],
        deliveries=[i for i in lines(BASE / "frames.jsonl")[frames1:] if i["frame"].get("kind") == "deliver"],
        summary=snap_summary(status()))
    cli("confirm-boot", "--yes")  # nothing pending any more
    cli("status")
    cli("shutdown", "--yes", "--json", timeout=120)
    time.sleep(2)
    left = ours()
    log("leftover", ours={str(k): v for k, v in left.items()})
    if left:
        kill_ours()
        time.sleep(1)
        log("leftover_after_kill", ours={str(k): v for k, v in ours().items()})
    st["post_after"] = {"r5": r5, "r6": r6}
    STATE.write_text(json.dumps(st, indent=1))


def wait_terminal(session, before, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        res = tool_results(session)
        if len(res) > before:
            return res[before:]
        time.sleep(0.5)
    return None


def terminal_check(client, worker, label, command, seconds=45):
    before = len(tool_results(worker))
    ok = client.request(ClientType.INPUT, f"tool terminal {json.dumps({'command': command})}\n".encode(),
                        pane="worker_omp")
    end = time.monotonic() + seconds
    got = None
    while time.monotonic() < end:
        res = tool_results(worker)
        if len(res) > before:
            got = res[before:]
            break
        client.pump(0.3)
    log("terminal_" + label, input_ok=ok.get("ok"), results=got)
    return got


def phase_pre2():
    (BASE / "home" / ".omp" / "agent").mkdir(parents=True)
    (BASE / "home" / ".omp" / "agent" / "agent.db").write_bytes(b"")
    PROJECT.mkdir()
    (PROJECT / "README").write_text("cw19 reboot project 2\n")
    git("init", "-q"); git("add", "README"); git("commit", "-q", "-m", "init")
    FAKE.write_text(f"#!{PY}\n" + (REPO / "tests/recovery_boot/fake_omp.py").read_text())
    FAKE.chmod(0o700)
    b = boot_id()
    log("boot_before", boot_id=b)
    cli("start", "--no-attach", "--omp", str(FAKE))
    s = wait_status(lambda s: s["phase"] == "ready", "ready")
    manager, worker = s["bridge"]["manager"]["session_id"], s["bridge"]["worker"]["session_id"]
    client = attached()
    try:
        terminal_check(client, worker, "baseline_no_task", "echo cw19-baseline-1")
        d = call(client, "manager_omp", manager, "to_worker", {
            "kind": "work", "message": "cw19 reboot work 2", "spec": {"goal": "cw19 reboot 2", "paths": ["notes/"]}})
        terminal_check(client, worker, "baseline_with_task", "echo cw19-baseline-2")
    finally:
        client.close()
    s = wait_status(lambda s: (s.get("task") or {}).get("run_id"), "the work run")
    log("pre2_summary", summary=snap_summary(s))
    screen = ui_screen()
    log("ui_screen_pre2", screen=screen)
    STATE.write_text(json.dumps({"boot_id": b, "task_id": d and d.get("task_id"), "runs": runs_count()}))
    os.sync()


def phase_post2():
    st = json.loads(STATE.read_text())
    now = boot_id()
    log("boot_after", boot_id_before=st["boot_id"], boot_id_after=now, changed=now != st["boot_id"])
    log("ours_before_start", ours={str(k): v for k, v in ours().items()})
    cli("start", "--no-attach", "--omp", str(FAKE))
    s = wait_status(lambda s: s["phase"] == "ready", "ready after reboot")
    log("ready_after_reboot", summary=snap_summary(s))
    worker = s["bridge"]["worker"]["session_id"]
    for cols in (160, 400):
        screen = ui_screen(cols=cols)
        log(f"ui_screen_before_confirm_{cols}", has_boot_wait="재부팅 확인 대기" in screen,
            status_lines=[l for l in screen.splitlines()[:3]], bottom=screen.splitlines()[-2:])
    manager = s["bridge"]["manager"]["session_id"]
    n0, f0 = len(lines(BASE / "notices.jsonl")), len(lines(BASE / "frames.jsonl"))
    client = attached()
    try:
        terminal_check(client, worker, "before_confirm_open_task", "echo cw19-before-confirm-2", seconds=20)
        end = time.monotonic() + 65
        while time.monotonic() < end:
            client.pump(0.5)
    finally:
        client.close()
    log("held_window2", notices=lines(BASE / "notices.jsonl")[n0:],
        deliveries=[i for i in lines(BASE / "frames.jsonl")[f0:] if i["frame"].get("kind") == "deliver"],
        summary=snap_summary(status()))
    n1, f1 = len(lines(BASE / "notices.jsonl")), len(lines(BASE / "frames.jsonl"))
    cli("confirm-boot", "--yes")
    s = wait_status(lambda s: not s["boot"]["confirmation_required"], "confirmed")
    log("after_confirm", summary=snap_summary(s))
    client = attached()
    try:
        end = time.monotonic() + 75
        while time.monotonic() < end:
            client.pump(0.5)
            if any(i["frame"].get("kind") == "deliver" for i in lines(BASE / "frames.jsonl")[f1:]):
                break
        time.sleep(3)
        log("notice_open_task_after_confirm", notices=lines(BASE / "notices.jsonl")[n1:],
            deliveries=[i for i in lines(BASE / "frames.jsonl")[f1:] if i["frame"].get("kind") == "deliver"],
            summary=snap_summary(status()))
        call(client, "manager_omp", manager, "to_worker",
             {"kind": "work", "message": "cancel after confirm 2", "task_id": st["task_id"], "cancel": True})
        time.sleep(2)
        terminal_check(client, worker, "after_confirm_task_cancelled", "echo cw19-after-confirm-2")
    finally:
        client.close()
    screen = ui_screen(cols=400)
    log("ui_screen_after_confirm_400", has_boot_wait="재부팅 확인 대기" in screen,
        status_lines=[l for l in screen.splitlines()[:3]], host_shell=[l for l in screen.splitlines()[22:30]])
    cli("status")
    cli("shutdown", "--yes", "--json", timeout=120)
    time.sleep(2)
    left = ours()
    log("leftover", ours={str(k): v for k, v in left.items()})
    if left:
        kill_ours()
        time.sleep(1)
        log("leftover_after_kill", ours={str(k): v for k, v in ours().items()})


# ============================================================================== END OF THE GUEST DRIVER
GUEST_PHASES = {"probe": phase_probe, "pre": phase_pre, "post": phase_post, "pre2": phase_pre2, "post2": phase_post2}
PHASE = "import"


def phase_post2_extra():
    """vm-02 same-boot follow-up (was /tmp/cw19-extra.py): after the confirmed reboot cycle, restart in the same
    boot with only a closed Task and show that the worker ``terminal`` runs (confirmation lifted the hold)."""
    log("boot_now", boot_id=boot_id())
    cli("start", "--no-attach", "--omp", str(FAKE))
    s = wait_status(lambda s: s["phase"] == "ready", "ready")
    log("ready_same_boot", summary=snap_summary(s))
    worker = s["bridge"]["worker"]["session_id"]
    c = attached()
    try:
        terminal_check(c, worker, "after_confirm_task_closed_same_boot", "echo cw19-after-confirm-closed", seconds=30)
    finally:
        c.close()
    cli("shutdown", "--yes", "--json", timeout=120)
    time.sleep(2)
    log("leftover", ours={str(k): v for k, v in ours().items()})


GUEST_PHASES["post2_extra"] = phase_post2_extra


# ============================================================================== HOST ORCHESTRATION (CW-16 X5)
# Everything below runs on the HOST only. It never reboots the host: the only reboot command is
# ``ssh.sh 'sync; sudo reboot'`` executed inside the isolated guest. It starts the VM only when it is not
# already running (fail closed otherwise), and always stops it again with ``stop.sh`` (disk files are kept).
import hashlib  # noqa: E402
import re  # noqa: E402
from typing import Any  # noqa: E402
import pwd  # noqa: E402
import shlex  # noqa: E402

HOST_REPO = Path(__file__).resolve().parents[2]
MANIFEST_TOPS = ("src", "omp_bridge", "tests/recovery_boot")  # same scope as L-CW19-REBOOT vm-01/vm-02
RSYNC_EXCLUDES = (".git", ".workflow", "graphify-out", "__pycache__")
GUEST_REPO_NAME = "ai-exp-assistant"
GUEST_DRIVER_NAME = "cw16-vm-driver.py"
GUEST_PY = "~/wb-venv/bin/python"
CW19_VM_02_AGGREGATE = "a4aebd9c3c69d5e5ee440d1b0632b460628df2959adcf89bc277b78348eebcad"  # vm-02 (fix-01 candidate)

MANIFEST_SCRIPT = r'''
import hashlib, json, os, sys
root = sys.argv[1]
out = {}
for top in sys.argv[2:]:
    for d, dirs, files in os.walk(os.path.join(root, top)):
        dirs[:] = sorted(x for x in dirs if x not in ("__pycache__", "node_modules"))
        for f in sorted(files):
            if f.endswith(".pyc"): continue
            p = os.path.join(d, f)
            if os.path.islink(p) or not os.path.isfile(p): continue
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
agg = hashlib.sha256("".join(f"{k}\0{v}\n" for k, v in sorted(out.items())).encode()).hexdigest()
print(json.dumps({"count": len(out), "aggregate": agg, "files": out}))
'''


def _user_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        return Path.home()


VM_DIR = Path(os.environ.get("WB_VM_DIR") or (_user_home() / ".local/share/wb-vm/wb-reboot"))


def host_env() -> dict:
    """A from-scratch env for host commands (nothing from the tmux/herdr session this may run in)."""
    home = str(_user_home())
    return {"PATH": "/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "USER": pwd.getpwuid(os.getuid()).pw_name}


def manifest(root: Path) -> dict:
    """The L-CW19-REBOOT manifest (src/**, omp_bridge/**, tests/recovery_boot/**) computed locally."""
    done = subprocess.run([sys.executable, "-c", MANIFEST_SCRIPT, str(root), *MANIFEST_TOPS], capture_output=True,
                          text=True, timeout=120, env=host_env(), check=True)
    return json.loads(done.stdout)


class HostRun:
    """start VM -> rsync -> manifest compare -> guest phases with two guest reboots -> fetch log -> stop VM -> judge."""

    def __init__(self, out_dir: Path, run_id: str, probe: bool = True):
        self.out = out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.probe = probe
        self.guest_root = f"cw16-reboot-{run_id}"
        self.events: list[dict] = []
        self.report: dict = {"scenario": "X5-vm-reboot", "run_id": run_id, "vm_dir": str(VM_DIR),
                             "guest_root": "~/" + self.guest_root, "host_repo_head": None, "events": self.events}
        self.started_vm = False

    # -- commands -------------------------------------------------------------------------------------------
    def run(self, argv: list[str], *, timeout: float, input_bytes: bytes | None = None, what: str = "",
            keep: int = 4000) -> subprocess.CompletedProcess:
        started = time.monotonic()
        try:
            done = subprocess.run(argv, input=input_bytes, capture_output=True, timeout=timeout, env=host_env(),
                                  stdin=None if input_bytes is not None else subprocess.DEVNULL)
            rc, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as exc:
            rc, out, err = None, exc.stdout or b"", (exc.stderr or b"") + b"\n[timeout]"
            done = subprocess.CompletedProcess(argv, -1, out, err)
        self.events.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "what": what or argv[0],
                            "argv": [a.replace(str(_user_home()), "~") for a in argv[:6]], "rc": rc,
                            "seconds": round(time.monotonic() - started, 1),
                            "stdout_tail": out.decode(errors="replace")[-keep:],
                            "stderr_tail": err.decode(errors="replace")[-1500:]})
        return done

    def ssh(self, command: str, *, timeout: float = 120, input_bytes: bytes | None = None, what: str = "",
            keep: int = 4000) -> subprocess.CompletedProcess:
        return self.run([str(VM_DIR / "ssh.sh"), command], timeout=timeout, input_bytes=input_bytes,
                        what=what or command[:60], keep=keep)

    def guest_boot_id(self) -> str | None:
        done = self.ssh("cat /proc/sys/kernel/random/boot_id", timeout=15, what="boot_id")
        value = done.stdout.decode(errors="replace").strip()
        return value if done.returncode == 0 and len(value) == 36 else None

    def wait_ssh(self, *, not_boot: str | None = None, timeout: float = 300) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.guest_boot_id()
            if value and value != not_boot:
                return value
            time.sleep(3)
        raise AssertionError(f"guest ssh not back within {timeout}s (not_boot={not_boot})")

    # -- steps ----------------------------------------------------------------------------------------------
    def vm_pid(self) -> int | None:
        try:
            pid = int((VM_DIR / "vm.pid").read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if Path(f"/proc/{pid}").exists() else None

    def start_vm(self) -> None:
        for name in ("start.sh", "stop.sh", "ssh.sh", "disk.qcow2"):
            assert (VM_DIR / name).exists(), f"VM file missing: {VM_DIR / name}"
        if self.vm_pid() is not None and os.environ.get("WB_VM_REUSE") != "1":
            raise AssertionError(f"the VM already runs (pid {self.vm_pid()}); refusing to share it "
                                 "(set WB_VM_REUSE=1 only when this run owns host:reboot-vm)")
        done = self.run([str(VM_DIR / "start.sh")], timeout=60, what="start.sh")
        assert done.returncode == 0, done.stderr
        self.started_vm = True
        self.report["qemu_pid"] = self.vm_pid()
        self.report["qemu_ticks"] = ticks(self.report["qemu_pid"]) if self.report["qemu_pid"] else None
        self.report["boot_id_first"] = self.wait_ssh()

    def sync_repo(self) -> dict:
        excludes = [f"--exclude={x}" for x in RSYNC_EXCLUDES]
        rsh = (f"ssh -i {shlex.quote(str(VM_DIR / 'id_ed25519'))} -p 2222 -o StrictHostKeyChecking=no "
               f"-o UserKnownHostsFile={shlex.quote(str(VM_DIR / 'known_hosts'))} -o ConnectTimeout=5 -o LogLevel=ERROR")
        done = self.run(["rsync", "-a", "--delete", *excludes, "-e", rsh, f"{HOST_REPO}/",
                         f"dwchoo@127.0.0.1:{GUEST_REPO_NAME}/"], timeout=600, what="rsync")
        assert done.returncode == 0, done.stderr.decode(errors="replace")[-800:]
        host = manifest(HOST_REPO)
        guest_done = self.ssh(f"python3 - ~/{GUEST_REPO_NAME} {' '.join(MANIFEST_TOPS)}",
                              input_bytes=MANIFEST_SCRIPT.encode(), timeout=120, what="guest manifest", keep=10 ** 7)
        guest = json.loads(guest_done.stdout)
        self.events[-1]["stdout_tail"] = f"<manifest count={guest['count']} aggregate={guest['aggregate']}>"
        differing = sorted(k for k in set(host["files"]) | set(guest["files"])
                           if host["files"].get(k) != guest["files"].get(k))
        driver_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        result = {"scope": "src/**, omp_bridge/**, tests/recovery_boot/** (no __pycache__)",
                  "host": {"count": host["count"], "aggregate": host["aggregate"]},
                  "guest": {"count": guest["count"], "aggregate": guest["aggregate"]},
                  "equal": host["aggregate"] == guest["aggregate"] and not differing, "differing": differing[:50],
                  "equals_cw19_vm02_aggregate": host["aggregate"] == CW19_VM_02_AGGREGATE,
                  "driver": {"path": "tests/integration/vm_reboot_driver.py", "sha256": driver_sha},
                  "files": host["files"]}
        (self.out / "host-manifest.json").write_text(json.dumps(host, indent=1))
        (self.out / "guest-manifest.json").write_text(json.dumps(guest, indent=1))
        self.report["manifest"] = {k: v for k, v in result.items() if k != "files"}
        assert result["equal"], f"guest copy differs from the host: {differing[:10]}"
        return result

    def install_driver(self) -> None:
        body = Path(__file__).read_bytes()
        done = self.ssh(f"cat > ~/{GUEST_DRIVER_NAME} && sha256sum ~/{GUEST_DRIVER_NAME}", input_bytes=body,
                        timeout=60, what="install driver")
        assert done.returncode == 0 and hashlib.sha256(body).hexdigest() in done.stdout.decode(), done.stdout
        versions = self.ssh(". /etc/os-release; echo \"os=$PRETTY_NAME\"; echo \"kernel=$(uname -r)\"; "
                            "echo \"bash=$(bash --version | head -1)\"; echo \"omp=$(~/.local/bin/omp --version 2>&1 | head -1)\"; "
                            f"echo \"python=$({GUEST_PY} -V 2>&1)\"", timeout=60, what="guest versions")
        self.report["guest_versions"] = dict(line.split("=", 1) for line in
                                             versions.stdout.decode(errors="replace").splitlines() if "=" in line)

    def guest_phase(self, phase: str, base: str, timeout: float = 900) -> int | None:
        command = (f"cd ~ && CW19_BASE={base} CW16_VM_ROOT=$HOME/{self.guest_root} PYTHONDONTWRITEBYTECODE=1 "
                   f"{GUEST_PY} ~/{GUEST_DRIVER_NAME} {phase}")
        done = self.ssh(command, timeout=timeout, what=f"guest {phase} ({base})", keep=3000)
        return done.returncode if done.returncode is not None else None

    def reboot_guest(self) -> dict:
        before = self.guest_boot_id()
        assert before, "guest not reachable before reboot"
        at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.ssh("sync; sudo reboot", timeout=30, what="guest reboot")  # the guest only, never the host
        time.sleep(5)
        after = self.wait_ssh(not_boot=before, timeout=300)
        return {"cmd": "ssh.sh 'sync; sudo reboot'", "at": at, "boot_before": before, "boot_after": after,
                "back_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "qemu_pid_same": self.vm_pid() == self.report.get("qemu_pid")}

    def fetch_log(self) -> list[dict]:
        done = self.ssh(f"cat ~/{self.guest_root}/log.jsonl", timeout=60, what="fetch guest log", keep=200)
        raw = done.stdout.decode(errors="replace")
        (self.out / "guest-log.jsonl").write_text(raw)
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def stop_vm(self) -> dict:
        pid = self.vm_pid()
        done = self.run([str(VM_DIR / "stop.sh")], timeout=120, what="stop.sh")
        gone = pid is None or not Path(f"/proc/{pid}").exists()
        return {"rc": done.returncode, "qemu_gone": gone, "disk_kept": (VM_DIR / "disk.qcow2").exists()
                and (VM_DIR / "noble-base.img").exists()}

    def execute(self) -> dict:
        self.report["host_repo_head"] = subprocess.run(["git", "-C", str(HOST_REPO), "rev-parse", "HEAD"],
                                                       capture_output=True, text=True, env=host_env()).stdout.strip()
        error = None
        guest_events: list[dict] = []
        try:
            self.start_vm()
            self.sync_repo()
            self.install_driver()
            reboots = []
            if self.probe:
                self.report["phase_rc_probe"] = self.guest_phase("probe", "probe", timeout=400)
            self.report["phase_rc_pre"] = self.guest_phase("pre", "a")
            reboots.append(self.reboot_guest())
            self.report["phase_rc_post"] = self.guest_phase("post", "a")
            self.report["phase_rc_pre2"] = self.guest_phase("pre2", "b")
            reboots.append(self.reboot_guest())
            self.report["phase_rc_post2"] = self.guest_phase("post2", "b")
            self.report["phase_rc_post2_extra"] = self.guest_phase("post2_extra", "b", timeout=400)
            self.report["reboots"] = reboots
            guest_events = self.fetch_log()
            after = manifest(HOST_REPO)
            self.report["host_manifest_after"] = {"aggregate": after["aggregate"],
                                                  "unchanged": after["aggregate"] == self.report["manifest"]["host"]["aggregate"]}
        except Exception as exc:  # recorded; the VM is always stopped
            import traceback
            error = traceback.format_exc()[-3000:]
        finally:
            if self.started_vm:
                self.report["stop"] = self.stop_vm()
        self.report["error"] = error
        verdict = judge(guest_events, self.report)
        self.report["verdict"] = verdict
        target = self.out / "vm-reboot-report.json"
        target.write_text(json.dumps(self.report, indent=1, ensure_ascii=False, default=str))
        return verdict


# ------------------------------------------------------------------------------------------------ judge
def _events(events: list[dict], phase: str, step: str) -> list[dict]:
    return [e for e in events if e.get("phase") == phase and e.get("step") == step]


def _one(events: list[dict], phase: str, step: str) -> dict:
    found = _events(events, phase, step)
    return found[-1] if found else {}


def _cli(events: list[dict], phase: str, *argv: str) -> list[dict]:
    return [e for e in _events(events, phase, "cli") if e.get("argv", [])[3:3 + len(argv)] == list(argv)
            and len(e.get("argv", [])) == 3 + len(argv)]


def _tools(events: list[dict], phase: str) -> list[dict]:
    return [e for e in events if e.get("phase") == phase and e.get("step") in ("tool", "tool_no_result")]


def _shutdown_ok(event: dict) -> bool:
    out = event.get("stdout") or ""
    start = out.find("{\"shutdown\"")
    try:
        payload = json.loads(out[start:])["shutdown"] if start >= 0 else {}
    except (ValueError, KeyError):
        payload = {}
    return event.get("rc") == 0 and payload.get("verified") is True and payload.get("left_running") == []


def judge(events: list[dict], report: dict | None = None) -> dict:
    """C-AC-23 checks (the vm-01/vm-02 table) + the two CW-19 VM fixes, evaluated from the guest log alone."""
    report = report or {}
    checks: list[dict] = []

    def check(cid: str, what: str, ok: Any, detail: Any = None) -> None:
        checks.append({"id": cid, "check": what, "ok": bool(ok), "detail": detail})

    for phase in ("post", "post2"):
        after = _one(events, phase, "boot_after")
        check(f"R1-{phase}", "boot_id changed across the guest reboot", after.get("changed") is True,
              {k: after.get(k) for k in ("boot_id_before", "boot_id_after")})
        check(f"R2-{phase}", "no process of ours survived the reboot", _one(events, phase, "ours_before_start")
              .get("ours") == {}, _one(events, phase, "ours_before_start").get("ours"))
        ready = (_one(events, phase, "ready_after_reboot").get("summary") or {})
        startup = ready.get("startup") or {}
        procs = startup.get("processes") or []
        check(f"R3-{phase}", "records restored: classification reboot, refs ended_by_reboot not probed, "
              "run interrupted_by_reboot, outbox queued_not_sent (no resend)",
              startup.get("classification") == "reboot" and startup.get("probed") is False and procs
              and all(p.get("state") == "ended_by_reboot" for p in procs)
              and (startup.get("run") or {}).get("state") == "interrupted_by_reboot"
              and all(o.get("state") == "queued_not_sent" for o in startup.get("outbox_lost") or [])
              and startup.get("survivors") == [],
              {"classification": startup.get("classification"), "probed": startup.get("probed"),
               "processes": [p.get("state") for p in procs], "run": (startup.get("run") or {}).get("state"),
               "outbox_lost": [o.get("state") for o in startup.get("outbox_lost") or []]})
        holds = [h.get("reason") for h in ready.get("holds") or []]
        check(f"R4-{phase}", "confirmation required (reboot) and boot_confirmation_required hold",
              (ready.get("boot") or {}).get("confirmation_required") is True
              and (ready.get("boot") or {}).get("reason") == "reboot" and "boot_confirmation_required" in holds,
              {"boot": ready.get("boot"), "holds": holds})
    pre_records = _one(events, "post", "records_on_disk")
    check("R3-disk", "records on disk before start (boot.json, backend.json, tasks.sqlite3 runs>=1)",
          pre_records.get("runs", 0) >= 1 and {"boot.json", "backend.json", "tasks.sqlite3"} <= set(pre_records.get("files") or []),
          {"runs": pre_records.get("runs"), "files": pre_records.get("files")})
    # UI shows the wait (F2: also at 160 columns, before the isolation warning)
    check("R5-ui", "product UI shows the boot-confirmation wait before confirm",
          _one(events, "post", "ui_screen_before_confirm").get("has_boot_wait") is True
          and _one(events, "post2", "ui_screen_before_confirm_160").get("has_boot_wait") is True
          and _one(events, "post2", "ui_screen_before_confirm_400").get("has_boot_wait") is True)
    lines160 = _one(events, "post2", "ui_screen_before_confirm_160").get("status_lines") or []
    check("F2-fixed", "160 columns: the wait leads the second status line (not hidden by the isolation warning)",
          len(lines160) > 1 and lines160[1].lstrip().startswith("재부팅 확인 대기"), lines160[:2])
    # held before confirm
    tools = _tools(events, "post")
    results = [(e.get("tool"), (e.get("result") or {})) for e in tools]
    held_before = [r for r in results[:4]]
    check("R6-held", "before confirm: follow-up to_worker, worker terminal, new experiment held "
          "(boot_confirmation_required); cancel allowed",
          len(held_before) == 4
          and all(r.get("status") == "held" and r.get("reason") == "boot_confirmation_required"
                  for i, (_, r) in enumerate(held_before) if i in (0, 1, 3))
          and held_before[2][1].get("status") == "cancelled",
          [(t, r.get("status"), r.get("reason")) for t, r in held_before])
    t2 = _one(events, "post2", "terminal_before_confirm_open_task").get("results") or [{}]
    check("R6-held-post2", "cycle 2: worker terminal held before confirm with the Task open",
          t2[0].get("status") == "held" and t2[0].get("reason") == "boot_confirmation_required", t2[:1])
    window = _one(events, "post", "held_window")
    check("R7-quiet", "70 s held window: no notice, no delivery, no new run",
          window.get("notices_since_start") == [] and window.get("deliveries_since_start") == []
          and window.get("runs_before") == window.get("runs_now"),
          {k: window.get(k) for k in ("runs_before", "runs_now")})
    window2 = _one(events, "post2", "held_window2")
    check("R7-quiet-post2", "cycle 2 held window (65 s): no notice, no delivery",
          window2.get("notices") == [] and window2.get("deliveries") == [])
    actions = _one(events, "post", "user_actions")
    check("R8-user", "user direct actions allowed: host shell input ran, pause and resume accepted",
          window.get("user_input_file") == "CW19_USER_INPUT\n" and (actions.get("pause") or {}).get("ok") is True
          and (actions.get("resume") or {}).get("ok") is True,
          {"input": window.get("user_input_file")})
    plain = _cli(events, "post", "confirm-boot")
    as_json = _cli(events, "post", "confirm-boot", "--json")
    yes = _cli(events, "post", "confirm-boot", "--yes")
    check("R9-confirm", "confirm-boot: no tty/--yes -> exit 1 with the conditions; --json -> 1; --yes -> 0",
          plain and plain[0].get("rc") == 1 and "== 부팅 확인" in (plain[0].get("stdout") or "")
          and as_json and as_json[0].get("rc") == 1 and yes and yes[0].get("rc") == 0,
          [(e.get("argv")[3:], e.get("rc")) for e in plain + as_json + yes])
    confirmed = (_one(events, "post", "after_confirm").get("summary") or {})
    effects = _one(events, "post", "after_confirm_effects")
    check("R10-after", "after confirm: hold lifted, UI wait gone, a new experiment is dispatched (runs +1)",
          (confirmed.get("boot") or {}).get("confirmation_required") is False and confirmed.get("holds") == []
          and _one(events, "post", "ui_screen_after_confirm").get("has_boot_wait") is False
          and effects.get("runs_now") == (effects.get("runs_before") or 0) + 1,
          {k: effects.get(k) for k in ("runs_before", "runs_now")})
    check("R11-second", "a second confirm-boot --yes has nothing pending (exit 1)",
          len(yes) >= 2 and yes[1].get("rc") == 1, [e.get("rc") for e in yes])
    after_cancel = ((_one(events, "post", "after_cancel").get("summary") or {}).get("startup") or {}).get("notice") or {}
    check("F1-fixed-closed", "Task cancelled before confirm: the queued backend_restarted notice is dropped, "
          "nothing delivered after confirm", after_cancel.get("state") == "dropped_task_closed"
          and effects.get("notices") == [], {"notice": after_cancel.get("state"), "notices_after": effects.get("notices")})
    open_task = _one(events, "post2", "notice_open_task_after_confirm")
    notices = open_task.get("notices") or []
    check("F1-fixed-open", "Task open: exactly one backend_restarted notice to the manager after confirm",
          len(notices) == 1 and notices[0].get("role") == "manager"
          and (notices[0].get("notice") or {}).get("classification") == "reboot",
          {"count": len(notices)})
    downs = [e for p in ("probe", "post", "post2", "post2_extra") for e in _cli(events, p, "shutdown", "--yes", "--json")]
    probe_down = _events(events, "probe", "probe_shutdown")
    check("R12-shutdown", "every shutdown --yes verified, exit 0, nothing left running",
          downs and all(_shutdown_ok(e) for e in downs) and all(_shutdown_ok({**e, "rc": e.get("rc")}) for e in probe_down),
          [e.get("rc") for e in downs + probe_down])
    leftovers = [e.get("ours") for p in ("post", "post2", "post2_extra") for e in _events(events, p, "leftover")]
    leftovers += [e.get("left") for e in _events(events, "probe", "probe_leftover")]
    check("R12-residue", "no process of ours left after shutdown", leftovers and all(x == {} for x in leftovers), leftovers)
    extra = _one(events, "post2_extra", "terminal_after_confirm_task_closed_same_boot").get("results") or [{}]
    check("R13-terminal", "same boot after confirm (closed Task only): worker terminal runs (exited 0)",
          extra[0].get("status") == "exited" and extra[0].get("exit_code") == 0, extra[:1])
    if report:
        manifest_info = report.get("manifest") or {}
        check("M-manifest", "guest copy equals the host (manifest aggregate)", manifest_info.get("equal") is True,
              {k: manifest_info.get(k) for k in ("host", "guest")})
        reboots = report.get("reboots") or []
        check("V-reboots", "two guest reboots, boot id changed each time, same QEMU process (host untouched)",
              len(reboots) == 2 and all(r["boot_before"] != r["boot_after"] and r["qemu_pid_same"] for r in reboots),
              reboots)
        stop = report.get("stop") or {}
        check("V-stopped", "VM stopped with stop.sh, disk files kept", stop.get("rc") == 0 and stop.get("qemu_gone")
              and stop.get("disk_kept"), stop)
        check("V-no-error", "orchestration completed without error", not report.get("error"), report.get("error"))
    observations = {
        "terminal_after_confirm_with_unaccepted_task": [
            _one(events, "post", "tool_no_result").get("tool"),
            _one(events, "post2", "terminal_after_confirm_task_cancelled").get("results")],
    }
    failed = [c["id"] for c in checks if not c["ok"]]
    return {"result": "pass" if not failed else "fail", "failed": failed, "checks": checks,
            "observations": observations}


# ------------------------------------------------------------------------------------------------ entry
def main(argv: list[str]) -> int:
    global PHASE
    if argv and argv[0] in GUEST_PHASES:  # inside the guest only
        PHASE = argv[0]
        ROOT.mkdir(exist_ok=True)
        GUEST_PHASES[PHASE]()
        return 0
    if argv and argv[0] == "judge":  # re-judge a saved guest log (e.g. the vm-02 log) on the host
        events = [json.loads(line) for line in Path(argv[1]).read_text().splitlines() if line.strip()]
        verdict = judge(events)
        print(json.dumps(verdict, indent=1, ensure_ascii=False))
        return 0 if verdict["result"] == "pass" else 1
    if argv and argv[0] == "host":
        run_id = os.environ.get("WB_CW16_RUN_ID") or time.strftime("%Y%m%dT%H%M%S")
        out = Path(argv[1]) if len(argv) > 1 else Path("/tmp/wb-cw16-reports") / run_id / "vm"
        verdict = HostRun(out, re.sub(r"[^A-Za-z0-9_-]", "", run_id)[:40] or "run",
                          probe=os.environ.get("WB_VM_PROBE", "1") == "1").execute()
        print(json.dumps({"result": verdict["result"], "failed": verdict["failed"],
                          "report": str(out / "vm-reboot-report.json")}, ensure_ascii=False))
        return 0 if verdict["result"] == "pass" else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
