"""Host reproduction of the CW-19 VM '45 s terminal' observation with tests/recovery_boot/fake_omp.py (no model)."""
import json, os, subprocess, sys, tempfile, time, shutil
from pathlib import Path
REPO = Path("/home/dwchoo/ai-exp-assistant"); SRC = REPO / "src"
sys.path.insert(0, str(SRC)); sys.path.insert(0, str(REPO / "tests/integration"))
import cw16_harness as h
from workbench.backend.client import UiClient
from workbench.contracts.ui_v1 import ClientType
root = Path(tempfile.mkdtemp(prefix="wbc16fake45-", dir="/tmp"))
home = root / "home"; (home / ".omp/agent").mkdir(parents=True); (home / ".omp/agent/agent.db").write_bytes(b"")
data = home / "wbdata"; project = root / "project"; project.mkdir()
env = h.clean_base_env(PATH="/usr/bin:/bin", HOME=str(home), SHELL="/usr/bin/bash", PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1",
    XDG_CONFIG_HOME=str(home/".config"), XDG_STATE_HOME=str(home/".local/state"), XDG_DATA_HOME=str(home/".local/share"), XDG_CACHE_HOME=str(home/".cache"),
    FAKE_PANE_RECORD=str(root/"panes.jsonl"), FAKE_NOTICES=str(root/"notices.jsonl"), FAKE_FRAMES=str(root/"frames.jsonl"),
    **{k: h.BLOCKED_PROXY for k in h.PROXY_KEYS})
def git(*a): subprocess.run(["git", "-C", str(project), *a], check=True, capture_output=True, env=h.clean_base_env(HOME=str(home)))
git("init", "-q"); git("config","user.email","x@x.invalid"); git("config","user.name","x"); (project/"README").write_text("x\n"); git("add","README"); git("commit","-qm","i")
fake = root / "omp"; fake.write_text(f"#!{sys.executable}\n" + (REPO/"tests/recovery_boot/fake_omp.py").read_text()); fake.chmod(0o700)
def cli(*a, t=120):
    return subprocess.run([sys.executable, "-m", "workbench", *a, "--data-dir", str(data)], env=env, cwd=project, capture_output=True, text=True, timeout=t, stdin=subprocess.DEVNULL)
def status():
    r = cli("status", "--json"); 
    try: v = json.loads(r.stdout)
    except ValueError: return None
    return v.get("snapshot") if v.get("running") else None
def lines(p): return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
def results(sess): return [i["frame"].get("result") for i in lines(root/"frames.jsonl") if i["session"]==sess and i["frame"].get("kind")=="tool_result"]
out = {}
try:
    r = cli("start", "--no-attach", "--omp", str(fake)); out["start"] = r.returncode
    for _ in range(120):
        s = status()
        if s and s["phase"] == "ready": break
        time.sleep(0.5)
    mgr, wrk = s["bridge"]["manager"]["session_id"], s["bridge"]["worker"]["session_id"]
    c = UiClient(data / "ui.sock", name="fake45", timeout=10); c.attach((30, 120))
    def tool(pane, sess, name, args, wait=60):
        before = len(results(sess)); t0 = time.monotonic()
        c.request(ClientType.INPUT, f"tool {name} {json.dumps(args)}\n".encode(), pane=pane)
        while time.monotonic() - t0 < wait:
            res = results(sess)
            if len(res) > before: return round(time.monotonic()-t0,1), res[before]
            c.pump(0.2)
        return None, None
    def threads():
        pid = status()["bridge"]["worker"]["pid"]
        st = Path(f"/proc/{pid}/status").read_text()
        return pid, [l for l in st.splitlines() if l.startswith("Threads:")][0]
    out["threads_before"] = threads()
    out["no_task"] = tool("worker_omp", wrk, "terminal", {"command": "echo base1"})
    out["dispatch"] = tool("manager_omp", mgr, "to_worker", {"kind": "work", "message": "w", "spec": {"goal": "g", "paths": ["notes/"]}})
    time.sleep(3); out["threads_after_dispatch"] = threads()
    s = status(); out["task_after_dispatch"] = s["task"]; out["worker_after_dispatch"] = s["worker"]
    out["with_task"] = tool("worker_omp", wrk, "terminal", {"command": "echo base2"}, wait=40)
    s = status(); out["task_after"] = s["task"]; out["shell"] = s["panes"]["host_shell"]["shell"]
    tid = out["dispatch"][1].get("task_id") if out["dispatch"][1] else None
    out["cancel"] = tool("manager_omp", mgr, "to_worker", {"kind": "work", "message": "cancel", "task_id": tid, "cancel": True})
    out["after_cancel"] = tool("worker_omp", wrk, "terminal", {"command": "echo base3"}, wait=40)
    s = status(); out["task_final"] = s["task"]; out["worker_final"] = s["worker"]
    c.close()
    out["journal"] = [json.dumps(r)[:260] for r in lines(data/"workflow"/"handoffs.jsonl")]
    out["frames"] = [json.dumps(r)[:260] for r in lines(root/"frames.jsonl")]
    import glob
    out["files"] = sorted(str(p.relative_to(data)) for p in data.rglob("*") if p.is_file())[:80]
finally:
    open("/tmp/wb-cw16-b2-dev/fake45-out3.json","w").write(json.dumps(out, indent=1, default=str)); print("written")
    cli("shutdown", "--yes", "--json", t=120)
    time.sleep(2)
    left = h.processes_mentioning(str(root)); print("left", left)
    for pid, st in left.items(): h.kill_exact(pid, st)
    shutil.rmtree(root, ignore_errors=True)
