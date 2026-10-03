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

    def test_default_personality_block_is_no_leak_but_auto_qa_is(self):
        # user choice: OMP's default Personality block stays; only Auto QA is a leak
        prompt = CLEAN_PROMPT + "\n# Personality\nOMP default tone\n\n# Internal URLs\n" \
            "Write `<tool>: <concise description>` as plain text to xd://report_issue.\n"
        observed = launcher.observe_isolation(state(prompt), commands())
        self.assertTrue(observed["personality_block"])
        self.assertTrue(observed["autoqa"])
        self.assertEqual(launcher.isolation_leaks(observed, allowed_skills=()), ["autoqa:enabled"])
        plain = launcher.observe_isolation(state(CLEAN_PROMPT + "\n# Personality\nx\n"), commands())
        self.assertEqual(launcher.isolation_leaks(plain, allowed_skills=()), [])
        clean = launcher.observe_isolation(state(CLEAN_PROMPT + "see # Personality docs\n"), commands())
        self.assertEqual((clean["personality_block"], clean["autoqa"]), (False, False))

    def test_ambient_prompt_files_are_found_and_matched_against_the_prompt(self):
        with tempfile.TemporaryDirectory(prefix="cw17-pf-") as directory:
            root = Path(directory)
            project, home = root / "proj", root / "home"
            for name, text in ((".claude", "CANARY_APPEND_CLAUDE\nsecond line\n"), (".gemini", "\n\n")):
                (project / name).mkdir(parents=True)
                (project / name / "APPEND_SYSTEM.md").write_text(text)
            agent = home / ".omp" / "agent"
            agent.mkdir(parents=True)
            (agent / "PERSONALITY.md").write_text("CANARY_PERSONALITY_MD\n")
            (agent / "APPEND_SYSTEM.md").write_text("CANARY_APPEND_USER\n")
            (project / ".omp").mkdir()
            (project / ".omp" / "TITLE_SYSTEM.md").write_text("CANARY_TITLE\n")
            (project / ".codex").mkdir()
            os.mkfifo(project / ".codex" / "APPEND_SYSTEM.md")  # never blocks the check
            files = launcher.ambient_prompt_files(project, {"HOME": str(home)})
            kinds = sorted((item["kind"], Path(item["path"]).relative_to(root).as_posix()) for item in files)
            # C-D64: PERSONALITY.md is read only from the (Workbench-owned) agent dir: not ambient any more
            self.assertEqual(kinds, [
                ("append_system", "home/.omp/agent/APPEND_SYSTEM.md"),
                ("append_system", "proj/.claude/APPEND_SYSTEM.md"),
                ("append_system", "proj/.gemini/APPEND_SYSTEM.md"),
                ("title_system", "proj/.omp/TITLE_SYSTEM.md"),
            ])
            leaked = CLEAN_PROMPT + "\n# Personality\nCANARY_PERSONALITY_MD\n\nCANARY_APPEND_CLAUDE\nsecond line\n"
            title = f"title_system:{project / '.omp' / 'TITLE_SYSTEM.md'}"
            self.assertEqual(launcher.prompt_file_leaks(leaked, files, no_title=False), [
                f"append_system:{project / '.claude' / 'APPEND_SYSTEM.md'}", title])
            self.assertFalse(hasattr(launcher, "prompt_file_warnings"))
            # --no-title (isolation args): TITLE_SYSTEM.md is never used, so no leak
            self.assertEqual(launcher.prompt_file_leaks(leaked, files), [
                f"append_system:{project / '.claude' / 'APPEND_SYSTEM.md'}"])
            self.assertEqual(launcher.prompt_file_leaks(CLEAN_PROMPT, files), [])
            self.assertEqual(launcher.prompt_file_leaks(CLEAN_PROMPT, files, no_title=False), [title])
            # the user agent dir follows PI_CONFIG_DIR (relative to HOME, '..' normalised) like OMP
            other = root / "wb" / "omp-root" / "agent"
            other.mkdir(parents=True)
            (other / "APPEND_SYSTEM.md").write_text("X\n")
            (other / "PERSONALITY.md").write_text("X\n")
            moved = launcher.ambient_prompt_files(root / "none", {"HOME": str(home),
                                                                  "PI_CONFIG_DIR": "../wb/omp-root",
                                                                  "PI_CODING_AGENT_DIR": str(other)})
            self.assertEqual([(item["kind"], item["path"]) for item in moved],
                             [("append_system", str(other / "APPEND_SYSTEM.md"))])

    def test_summary_flags_leaks_failures_and_version_drift(self):
        ok = {"role": "manager", "state": "ok", "ok": True, "leaks": [], "error": None}
        leak = {"role": "worker", "state": "leak", "ok": False, "leaks": ["skill:orca-cli"], "error": None}
        failed = {"role": "worker", "state": "failed", "ok": False, "leaks": [], "error": "timed out"}
        summary = launcher.summarize_isolation({"manager": ok, "worker": leak}, "omp/18.4.5")
        self.assertEqual((summary["state"], summary["checked"], summary["ok"]), ("leak", True, False))
        self.assertEqual(summary["leaks"], ["worker:skill:orca-cli"])
        self.assertIn("worker:skill:orca-cli", summary["warning"])
        self.assertEqual(summary["omp_version"], "omp/18.4.5")
        # isolation evidence: C-D64 home probe + live p27-home-test-01 with OMP 18.4.5
        self.assertEqual(summary["version_drift"], {"bridge_g3": "18.2.10"})
        self.assertEqual(launcher.summarize_isolation({"manager": ok}, "omp/18.4.4")["version_drift"],
                         {"bridge_g3": "18.2.10", "isolation": "18.4.5"})
        self.assertEqual(launcher.summarize_isolation({"manager": ok, "worker": failed}, "omp/18.4.4")["state"],
                         "failed")
        clean = launcher.summarize_isolation({"manager": ok, "worker": dict(ok, role="worker")}, "omp/18.2.10")
        self.assertEqual((clean["state"], clean["ok"], clean["warning"]), ("ok", True, None))
        self.assertEqual(clean["version_drift"], {"isolation": "18.4.5"})
        pending = launcher.pending_isolation("omp/18.4.4")
        self.assertEqual((pending["state"], pending["checked"], pending["ok"]), ("pending", False, None))

    def test_summary_reports_a_warning_without_a_leak(self):
        warned = {"role": "manager", "state": "warning", "ok": True, "leaks": [], "error": None,
                  "warnings": ["auth_link:retargeted"]}
        ok = {"role": "worker", "state": "ok", "ok": True, "leaks": [], "error": None, "warnings": []}
        summary = launcher.summarize_isolation({"manager": warned, "worker": ok}, "omp/18.4.4")
        self.assertEqual((summary["state"], summary["ok"], summary["leaks"]), ("warning", True, []))
        self.assertEqual(summary["warnings"], ["manager:auth_link:retargeted"])
        self.assertEqual(summary["warning"], "OMP isolation warning: manager:auth_link:retargeted")
        leak = {"role": "worker", "state": "leak", "ok": False, "leaks": ["skill:x"], "error": None, "warnings": []}
        mixed = launcher.summarize_isolation({"manager": warned, "worker": leak}, "omp/18.4.4")
        self.assertEqual(mixed["state"], "leak")
        self.assertEqual(launcher.pending_isolation("omp/18.4.4")["warnings"], [])


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
if mode == "append":
    prompt += "\n\nCANARY_APPEND_PROJECT\n"
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

    def run_check(self, mode, timeout=10.0, cancel=None, extra=()):
        env = {"PATH": os.environ.get("PATH", "/usr/bin"), "FAKE_MODE": mode, "FAKE_RECORD": str(self.record),
               "HOME": str(self.root / "home"),
               "WORKBENCH_G3_BRIDGE_SOCKET": str(self.root / "absent.sock"), "WORKBENCH_G3_TOKEN": "isolation-check"}
        return launcher.check_isolation([str(self.fake), "--config", "x.yml", *extra], cwd=self.root, environment=env,
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

    def test_append_system_md_in_the_prompt_is_a_leak_and_an_unused_one_is_not(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "APPEND_SYSTEM.md").write_text("CANARY_APPEND_PROJECT\n")
        result = self.run_check("append")
        self.assertEqual((result["state"], result["ok"]), ("leak", False))
        self.assertEqual(result["leaks"], [f"append_system:{self.root / '.claude' / 'APPEND_SYSTEM.md'}"])
        self.assertEqual(result["observed"]["prompt_files"],
                         [{"kind": "append_system", "path": str(self.root / ".claude" / "APPEND_SYSTEM.md")}])
        self.assert_cleaned(result)
        self.record.unlink()
        clean = self.run_check("clean")  # the file exists but OMP did not append it
        self.assertEqual((clean["state"], clean["leaks"]), ("ok", []))
        self.assert_cleaned(clean)

    def test_users_personality_md_is_no_longer_reported(self):
        # C-D64: OMP reads PERSONALITY.md from its agent dir only (the Workbench home)
        agent = self.root / "home" / ".omp" / "agent"
        agent.mkdir(parents=True)
        (agent / "PERSONALITY.md").write_text("CANARY_PERSONALITY_MD\n")
        result = self.run_check("clean")
        self.assertEqual((result["state"], result["ok"], result["leaks"], result["error"]), ("ok", True, [], None))
        self.assertEqual(result["warnings"], [])
        self.assert_cleaned(result)

    def test_title_system_md_is_a_leak_only_without_no_title(self):
        agent = self.root / "home" / ".omp" / "agent"
        agent.mkdir(parents=True)
        (agent / "TITLE_SYSTEM.md").write_text("CANARY_TITLE\n")
        title = f"title_system:{agent / 'TITLE_SYSTEM.md'}"
        result = self.run_check("clean")  # command without --no-title: cannot be excluded
        self.assertEqual((result["state"], result["leaks"]), ("leak", [title]))
        self.assert_cleaned(result)
        self.record.unlink()
        result = self.run_check("clean", extra=("--no-title",))
        self.assertEqual((result["state"], result["leaks"], result["warnings"]), ("ok", [], []))
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
        self.assertIn("omp isolation: leak (omp/18.4.4 evidence bridge_g3=18.2.10, isolation=18.4.5)", text)
        self.assertIn("WARNING: OMP isolation leak (ambient configuration loaded): worker:skill:orca-cli", text)
        self.assertIn("note: could not read", text)


class CliIsolationWaitTests(unittest.TestCase):
    def snapshot(self, state):
        summary = launcher.pending_isolation("omp/18.4.4")
        if state != "pending":
            summary.update(state=state, checked=True, ok=state == "ok")
        return {"backend": {"pid": 1, "data_dir": "/d", "omp_version": "omp/18.4.4"}, "phase": "ready",
                "reason": None, "panes": {}, "omp_isolation": summary}

    def test_start_waits_for_the_isolation_result_before_the_summary(self):
        import io
        from unittest import mock
        from workbench.backend import cli
        sequence = iter([self.snapshot("pending"), self.snapshot("leak")])
        stream = io.StringIO()
        with mock.patch.object(cli, "_running_snapshot", side_effect=lambda layout: next(sequence)):
            final = cli._wait_isolation(None, self.snapshot("pending"), timeout=10, stream=stream)
        self.assertEqual(final["omp_isolation"]["state"], "leak")
        self.assertIn("waiting for the OMP isolation check", stream.getvalue())
        self.assertGreaterEqual(cli.ISOLATION_WAIT, 2 * launcher.ISOLATION_CHECK_TIMEOUT)

    def test_a_check_still_pending_after_the_bound_is_printed_as_pending(self):
        import io
        from unittest import mock
        from workbench.backend import cli
        stream = io.StringIO()
        with mock.patch.object(cli, "_running_snapshot", side_effect=lambda layout: self.snapshot("pending")):
            started = time.monotonic()
            final = cli._wait_isolation(None, self.snapshot("pending"), timeout=0.5, stream=stream)
        self.assertLess(time.monotonic() - started, 3)
        text = io.StringIO()
        cli._print_summary(final, text)
        self.assertIn("omp isolation: pending", text.getvalue())
        self.assertIn("run 'status'", text.getvalue())
        done = io.StringIO()
        self.assertIs(cli._wait_isolation(None, self.snapshot("ok"), stream=done)["omp_isolation"]["state"], "ok")
        self.assertEqual(done.getvalue(), "")  # no wait when the result is already there


if __name__ == "__main__":
    unittest.main()
