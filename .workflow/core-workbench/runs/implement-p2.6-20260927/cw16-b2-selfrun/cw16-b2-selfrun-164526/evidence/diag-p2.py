import sys, json, time
sys.path.insert(0, "/home/dwchoo/ai-exp-assistant/tests/integration")
import cw16_harness as h, cw16_flow_rig as fr
import live_cw16_flow_policy as lp
class T:
    def __init__(self): self.c=[]
    def addCleanup(self, f, *a): self.c.append((f,a))
variant = sys.argv[1]
cmds = {"file_before": "mkdir -p out; printf PASS > out/p.txt; sleep 15; echo DIAG_DONE",
        "file_during": "mkdir -p out; sleep 15; echo DIAG_DONE; printf PASS > out/p.txt",
        "still_running": "mkdir -p out; printf PASS > out/p.txt; sleep 60; echo DIAG_DONE"}
t = T(); rig = fr.Rig(t, f"diagp2-{variant}")
lp.contract_worker(rig)
holder = lp.exp_task(rig, "dp-go", "diag", cmds[variant], log="DIAG_DONE", result_file="out/p.txt")
try:
    rig.start(); rig.say("manager_omp", "dp-go")
    rig.pump_until(lambda: "call" in holder and rig.result(holder["call"]), 60)
    s = rig.wait(lambda s: (s["automation"]["run"] or {}).get("bound"), 60, "bound")
    rid = s["automation"]["run"]["run_id"]; rig.pump(3)
    rig.pause()
    if variant != "still_running":
        log = lp.raw_log(rig, rid); rig.pump_until(lambda: log.exists() and b"DIAG_DONE" in log.read_bytes(), 40); rig.pump(5)
    else:
        rig.pump(5)
    rig.ui_key(b"p"); rig.ui.send(b"p", settle=0.5); rig.pump(10)
    a = rig.status()["automation"]
    print(variant, "RESUME", a["resume"], a["state"], (rig.task() or {}).get("status"))
finally:
    for f,a in reversed(t.c): f(*a)
