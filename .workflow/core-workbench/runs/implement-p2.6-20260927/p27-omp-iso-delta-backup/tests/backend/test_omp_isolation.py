"""C-D59 start-up isolation check: predicates, fake-OMP RPC exchange, cleanup and snapshot field.

No real OMP and no model calls: a fake ``omp`` script speaks the two RPC
commands the check uses (get_state, get_available_commands).
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import time
import unittest

from workbench.backend import launcher
from workbench.backend.launcher import LaunchPlan
from workbench.backend.paths import DataLayout
from workbench.backend.service import Backend
from workbench.terminal.shell_g2.prototype import ShellChoice

CLEAN_PROMPT = textwrap.dedent("""\
    § Role
    You are omp's trusted coding assistant.
    <project-context>
    <workstation>
    - OS: linux
    </workstation>
    </project-context>
    # Internal URLs
    - `skill://<name>`: instructions.
    """)

LEAKY_PROMPT = textwrap.dedent("""\
    You are omp's trusted coding assistant.
    <skills>
    - canary-claude: CANARY_SKILL_claude
    - orca-cli: Use for Orca-managed worktrees
    - order-worker: Workbench skill
    </skills>

    <generic-rules>
    CANARY_RULE_BODY
    </generic-rules>

    <domain-rules>
    - canary2 (*.zz): CANARY_RULE_DESC when editing
    </domain-rules>
    <project-context>
    <repo-rules>
    MUST follow these context files for all tasks:
    <file path="/scratch/proj/AGENTS.md">
    CANARY_PROJ_AGENTS
    </file>
    </repo-rules>
    </project-context>

    ## MCP Tool Routes

    Execute each mounted tool: write JSON arguments to its path.
    - "canary_tool" → `xd://mcp__canary_tool` — CANARY_MCP_TOOL
    """)

TASK_DESCRIPTION = textwrap.dedent("""\
    # Delegation
    Use most specific agent.

    # Available Agents
    - `{extra}scout` (READ-ONLY; investigation only, no edits): scout.
    - `reviewer` (READ-ONLY): reviewer.
    - `task`: default.

    # Inputs
    - `name`: CamelCase
    """)


def state(prompt, agents_prefix=""):
    extra = f"canary-agent` (READ-ONLY): CANARY.\n- `" if agents_prefix else ""
    return {"model": {"provider": "openai-codex", "id": "gpt-x"},
            "systemPrompt": prompt.splitlines(),
            "dumpTools": [{"name": "read", "description": "r"},
                          {"name": "task", "description": TASK_DESCRIPTION.format(extra=extra)}]}


def commands(*extra):
    base = [{"name": "model", "source": "builtin"}, {"name": "autoresearch", "source": "extension"},
            {"name": "init", "source": "file"}]
    return base + [{"name": name, "source": source} for name, source in extra]


class ObservationPredicateTests(unittest.TestCase):
    def test_clean_isolated_state_has_no_leaks(self):
        observed = launcher.observe_isolation(state(CLEAN_PROMPT), commands())
        self.assertEqual(observed["context_files"], [])
        self.assertEqual(observed["skills"], [])
        self.assertEqual(observed["rules"], [])
        self.assertEqual(observed["mcp_tools"], [])
        self.assertEqual(observed["task_agents"], ["scout", "reviewer", "task"])
        self.assertEqual(observed["model"], "openai-codex/gpt-x")
        self.assertEqual(launcher.isolation_leaks(observed, allowed_skills=()), [])

    def test_every_ambient_category_is_reported(self):
        observed = launcher.observe_isolation(
            state(LEAKY_PROMPT, agents_prefix="x"),
            commands(("skill:canary-claude", "skill"), ("skill:order-worker", "skill"),
                     ("canary-cmd", "file"), ("green", "custom")))
        self.assertEqual(observed["context_files"], ["/scratch/proj/AGENTS.md"])
        self.assertEqual(observed["skills"], ["canary-claude", "orca-cli", "order-worker"])
        self.assertEqual(observed["skill_commands"], ["canary-claude", "order-worker"])
        self.assertEqual(observed["rules"], ["CANARY_RULE_BODY", "canary2"])
        self.assertEqual(observed["mcp_tools"], ["mcp__canary_tool"])
        self.assertIn("canary-agent", observed["task_agents"])
        leaks = launcher.isolation_leaks(observed, allowed_skills=("order-worker",))
        self.assertEqual(leaks, [
            "context_file:/scratch/proj/AGENTS.md",
            "skill:canary-claude", "skill:orca-cli",
            "rule:CANARY_RULE_BODY", "rule:canary2",
            "mcp:mcp__canary_tool",
            "task_agent:canary-agent",
            "command:canary-cmd(file)", "command:green(custom)",
        ])
        # the same Workbench skill is a leak for a role whose filter excludes it
        self.assertIn("skill:order-worker", launcher.isolation_leaks(observed, allowed_skills=()))

    def test_replaced_system_prompt_template_is_parsed_and_flagged(self):
        custom = textwrap.dedent("""\
            CANARY_SYSTEM_MD
            <skills>
            <skill name="orca-cli">
            Use for Orca
            </skill>
            </skills>
            CANARY_RULE_BODY
            <rules>
            <rule name="canary2">
            desc
            </rule>
            </rules>
            """)
        observed = launcher.observe_isolation(state(custom), commands())
        self.assertFalse(observed["system_prompt_default"])
        self.assertEqual(observed["skills"], ["orca-cli"])
        self.assertEqual(observed["rules"], ["canary2"])
        self.assertEqual(launcher.isolation_leaks(observed, allowed_skills=()),
                         ["system_prompt:replaced", "skill:orca-cli", "rule:canary2"])

    def test_summary_flags_leaks_failures_and_version_drift(self):
        ok = {"role": "manager", "state": "ok", "ok": True, "leaks": [], "error": None}
        leak = {"role": "worker", "state": "leak", "ok": False, "leaks": ["skill:orca-cli"], "error": None}
        failed = {"role": "worker", "state": "failed", "ok": False, "leaks": [], "error": "timed out"}
        summary = launcher.summarize_isolation({"manager": ok, "worker": leak}, "omp/18.4.4")
        self.assertEqual((summary["state"], summary["checked"], summary["ok"]), ("leak", True, False))
        self.assertEqual(summary["leaks"], ["worker:skill:orca-cli"])
        self.assertIn("worker:skill:orca-cli", summary["warning"])
        self.assertEqual(summary["omp_version"], "omp/18.4.4")
        self.assertEqual(summary["version_drift"], {"bridge_g3": "18.2.10"})
        self.assertEqual(launcher.summarize_isolation({"manager": ok, "worker": failed}, "omp/18.4.4")["state"],
                         "failed")
        clean = launcher.summarize_isolation({"manager": ok, "worker": dict(ok, role="worker")}, "omp/18.2.10")
        self.assertEqual((clean["state"], clean["ok"], clean["warning"]), ("ok", True, None))
        self.assertEqual(clean["version_drift"], {"isolation": "18.4.4"})
        pending = launcher.pending_isolation("omp/18.4.4")
        self.assertEqual((pending["state"], pending["checked"], pending["ok"]), ("pending", False, None))


FAKE_OMP = r'''#!{python}
import json, os, subprocess, sys, time
mode = os.environ.get("FAKE_MODE", "clean")
record = os.environ["FAKE_RECORD"]
child = subprocess.Popen(["sleep", "60"])  # same process group: must be cleaned too
with open(record, "w") as stream:
    json.dump({{"argv": sys.argv[1:], "pid": os.getpid(), "child": child.pid,
               "bridge": {{k: v for k, v in os.environ.items() if k.startswith("WORKBENCH_G3_")}}}}, stream)
if mode == "crash":
    sys.exit(4)
print(json.dumps({{"type": "ready"}}), flush=True)
prompt = {prompt!r} if mode == "leak" else "You are omp's assistant"
for line in sys.stdin:
    request = json.loads(line)
    if mode == "hang":
        continue
    if request["type"] == "get_state":
        data = {{"systemPrompt": prompt.splitlines(), "dumpTools": [], "model": {{"provider": "p", "id": "m"}}}}
        pad = {{"type": "event", "blob": "x" * 200000}}
        sys.stdout.write(json.dumps(pad) + "\n")
    else:
        data = {{"commands": [{{"name": "autoresearch", "source": "extension"}}]}}
    print(json.dumps({{"type": "response", "id": request["id"], "command": request["type"],
                      "success": True, "data": data}}), flush=True)
'''


class FakeOmpCheckTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw17-chk-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.fake = self.root / "omp"
        self.fake.write_text(FAKE_OMP.format(python=sys.executable, prompt=LEAKY_PROMPT))
        self.fake.chmod(0o700)
        self.record = self.root / "record.json"

    def run_check(self, mode, timeout=10.0, cancel=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin"), "FAKE_MODE": mode, "FAKE_RECORD": str(self.record),
               "WORKBENCH_G3_BRIDGE_SOCKET": str(self.root / "absent.sock"), "WORKBENCH_G3_TOKEN": "isolation-check"}
        return launcher.check_isolation([str(self.fake), "--config", "x.yml"], cwd=self.root, environment=env,
                                        role="worker", allowed_skills=(), omp_version="omp/0-test",
                                        timeout=timeout, cancel=cancel)

    def assert_cleaned(self, result):
        seen = json.loads(self.record.read_text())
        for pid in (seen["pid"], seen["child"]):
            self.assertFalse(Path(f"/proc/{pid}").exists() and
                             Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] not in "ZX",
                             f"pid {pid} survived the check")
        self.assertEqual(result["cleanup"]["pid"], seen["pid"])
        self.assertEqual(result["cleanup"]["state"], "dead")
        self.assertEqual(result["cleanup"]["group_left"], [])
        return seen

    def test_clean_rpc_exchange_appends_rpc_flags_and_reports_ok(self):
        result = self.run_check("clean")
        self.assertEqual((result["state"], result["ok"], result["leaks"], result["error"]), ("ok", True, [], None))
        seen = self.assert_cleaned(result)
        self.assertEqual(seen["argv"], ["--config", "x.yml", "--mode", "rpc", "--no-session", "--no-title"])
        self.assertEqual(seen["bridge"]["WORKBENCH_G3_BRIDGE_SOCKET"], str(self.root / "absent.sock"))
        self.assertEqual(result["observed"]["other_commands"], [["autoresearch", "extension"]])
        self.assertEqual(result["omp_version"], "omp/0-test")

    def test_leaking_omp_is_reported_not_raised(self):
        result = self.run_check("leak")
        self.assertEqual((result["state"], result["ok"]), ("leak", False))
        self.assertIn("context_file:/scratch/proj/AGENTS.md", result["leaks"])
        self.assertIn("skill:orca-cli", result["leaks"])
        self.assert_cleaned(result)

    def test_hanging_omp_times_out_and_is_cleaned_by_identity(self):
        started = time.monotonic()
        result = self.run_check("hang", timeout=1.5)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((result["state"], result["ok"]), ("failed", False))
        self.assertIn("timed out", result["error"])
        self.assert_cleaned(result)

    def test_exit_before_response_and_cancel_are_failures(self):
        result = self.run_check("crash")
        self.assertEqual(result["state"], "failed")
        self.assertIn("exited", result["error"])
        self.assert_cleaned(result)
        self.record.unlink()
        cancel = threading.Event()
        cancel.set()
        result = self.run_check("hang", cancel=cancel)
        self.assertEqual(result["state"], "failed")
        self.assertIn("cancelled", result["error"])
        # cancelled at once: the fake may be stopped before it records its pids
        self.assertEqual((result["cleanup"]["state"], result["cleanup"]["group_left"]), ("dead", []))
        if self.record.exists():
            self.assert_cleaned(result)

    def test_missing_executable_is_a_failure(self):
        result = launcher.check_isolation([str(self.root / "nope")], cwd=self.root, environment={},
                                          role="manager", allowed_skills=())
        self.assertEqual(result["state"], "failed")
        self.assertIn("spawn failed", result["error"])


class BackendSnapshotTests(unittest.TestCase):
    def test_snapshot_exposes_pending_isolation_and_omp_version(self):
        with tempfile.TemporaryDirectory(prefix="cw17-snap-") as directory:
            plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.4", "/x/bridge.ts")
            backend = Backend(DataLayout(Path(directory)), plan, project_dir=directory, environment={})
            snapshot = backend.snapshot()
            self.assertEqual(snapshot["backend"]["omp_version"], "omp/18.4.4")
            self.assertEqual(snapshot["omp_isolation"]["state"], "pending")
            self.assertEqual(snapshot["omp_isolation"]["omp_version"], "omp/18.4.4")
            self.assertIn("omp_isolation", backend._state_view(snapshot))

    def test_isolation_worker_records_result_for_both_roles(self):
        with tempfile.TemporaryDirectory(prefix="cw17-snap-") as directory:
            root = Path(directory)
            fake = root / "omp"
            fake.write_text(FAKE_OMP.format(python=sys.executable, prompt=LEAKY_PROMPT))
            fake.chmod(0o700)
            plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), str(fake), "omp/18.4.4", "/x/bridge.ts")
            env = {"PATH": os.environ.get("PATH", "/usr/bin"), "FAKE_RECORD": str(root / "r.json"),
                   "FAKE_MODE": "leak"}
            backend = Backend(DataLayout(root), plan, project_dir=directory, environment=env)
            jobs = {role: ([str(fake)], env, ()) for role in ("manager", "worker")}
            backend._run_isolation_check(jobs)
            summary = backend.snapshot()["omp_isolation"]
            self.assertEqual((summary["state"], summary["checked"], summary["ok"]), ("leak", True, False))
            self.assertEqual(sorted(summary["roles"]), ["manager", "worker"])
            self.assertTrue(any(item.startswith("manager:skill:") for item in summary["leaks"]))
            self.assertIsNotNone(summary["warning"])


class CliSummaryTests(unittest.TestCase):
    def test_start_and_status_summary_print_isolation_state_and_warning(self):
        import io
        from workbench.backend.cli import _print_summary
        summary = launcher.summarize_isolation(
            {"worker": {"state": "leak", "leaks": ["skill:orca-cli"], "error": None}}, "omp/18.4.4")
        summary["notes"] = ["could not read the user's disabledProviders"]
        snapshot = {"backend": {"pid": 1, "data_dir": "/d", "omp_version": "omp/18.4.4"}, "phase": "ready",
                    "reason": None, "panes": {}, "omp_isolation": summary}
        stream = io.StringIO()
        _print_summary(snapshot, stream)
        text = stream.getvalue()
        self.assertIn("omp isolation: leak (omp/18.4.4 evidence bridge_g3=18.2.10)", text)
        self.assertIn("WARNING: OMP isolation leak (ambient configuration loaded): worker:skill:orca-cli", text)
        self.assertIn("note: could not read", text)


if __name__ == "__main__":
    unittest.main()
