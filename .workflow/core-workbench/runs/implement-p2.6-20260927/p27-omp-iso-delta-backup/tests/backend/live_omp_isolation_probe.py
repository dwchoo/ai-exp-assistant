"""Live C-D59 probe: the isolated OMP command against a canary project, real OMP, zero model calls.

Runs only with WB_LIVE_OMP=1 (``PYTHONPATH=src:. python -m unittest
tests.backend.live_omp_isolation_probe``). It sends only RPC get_state and
get_available_commands to ``omp --mode rpc --no-session`` (no prompt, so no
model call), never edits ~/.omp or any global config and never uses
--profile. All files live in one temp dir; every spawned OMP process group is
stopped and reaped by the check itself.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from workbench.backend import launcher
from workbench.backend.launcher import LaunchPlan
from workbench.terminal.shell_g2.prototype import ShellChoice

LIVE = os.environ.get("WB_LIVE_OMP") == "1"
AMBIENT_USER_SKILLS = Path.home() / ".agents" / "skills"  # orca skills on the dev machine

MCP_STUB = r'''import json, sys
for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method, ident = message.get("method"), message.get("id")
    if method == "initialize":
        result = {"protocolVersion": message["params"].get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "canary", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "canary_tool", "description": "CANARY_MCP_TOOL",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif ident is None:
        continue
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
'''


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def skill(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\nCANARY_SKILL_BODY {name}\n"


def git_project(root: Path, name: str) -> Path:
    project = root / name
    project.mkdir()
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q", str(project)], check=True, timeout=30)
    return project


def canary_project(root: Path) -> Path:
    project = git_project(root, "proj")
    write(project / "AGENTS.md", "CANARY_AGENTS_MD\n")
    write(project / "CLAUDE.md", "CANARY_CLAUDE_MD\n")
    write(project / ".claude" / "skills" / "canary-claude" / "SKILL.md", skill("canary-claude", "CANARY_SKILL_CLAUDE"))
    write(project / ".agents" / "skills" / "canary-agents" / "SKILL.md", skill("canary-agents", "CANARY_SKILL_AGENTS"))
    write(project / ".omp" / "skills" / "canary-omp" / "SKILL.md", skill("canary-omp", "CANARY_SKILL_OMP"))
    write(project / ".omp" / "rules" / "canary.md", "---\ndescription: canary\nalwaysApply: true\n---\nCANARY_RULE_BODY\n")
    write(project / ".omp" / "rules" / "canary-domain.md",
          "---\ndescription: CANARY_RULE_DESC\nglobs: [\"*.zz\"]\n---\nCANARY_RULE_DOMAIN\n")
    write(project / ".omp" / "agents" / "canary.md", "---\nname: canary-agent\ndescription: CANARY_AGENT\n---\nx\n")
    write(project / ".omp" / "commands" / "canary-cmd.md", "CANARY_COMMAND\n")
    marker = root / "canary-extension-loaded"
    write(project / ".omp" / "extensions" / "canary-ext" / "index.ts",
          'import * as fs from "node:fs";\n'
          f"export default function canary(_pi: any): void {{ fs.writeFileSync({json.dumps(str(marker))}, \"x\"); }}\n")
    write(root / "mcpstub.py", MCP_STUB)
    write(project / ".mcp.json", json.dumps({"mcpServers": {"canary": {"command": sys.executable,
                                                                       "args": [str(root / "mcpstub.py")]}}}))
    return project


def ambient_user_skill_names() -> set[str]:
    try:
        return {entry.name for entry in AMBIENT_USER_SKILLS.iterdir() if (entry / "SKILL.md").is_file()}
    except OSError:
        return set()


@unittest.skipUnless(LIVE, "set WB_LIVE_OMP=1 to run the live OMP isolation probe")
class LiveOmpIsolationProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.omp = shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
        if not os.access(cls.omp, os.X_OK):
            raise unittest.SkipTest("omp not found")
        cls.version = launcher.omp_version(cls.omp)
        cls._dir = tempfile.TemporaryDirectory(prefix="wbiso-")
        cls.root = Path(cls._dir.name)
        cls.project = canary_project(cls.root)
        # SYSTEM.md replaces the default template (which hides the skill and
        # rule lists), so it is probed in its own tree.
        cls.sys_project = git_project(cls.root, "proj-sys")
        write(cls.sys_project / ".omp" / "SYSTEM.md", "CANARY_SYSTEM_MD\n")
        cls.skills = cls.root / "wbskills"
        write(cls.skills / "wb-canary-skill" / "SKILL.md", skill("wb-canary-skill", "WB_WORKBENCH_SKILL"))
        cls.plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), cls.omp, cls.version,
                              str(launcher.default_bridge_extension()))
        cls.env = launcher.isolation_check_environment(os.environ, cls.plan, role="worker",
                                                       absent_socket=cls.root / "absent.sock")
        home = Path(os.environ.get("HOME") or Path.home())
        overlay = launcher.role_overlay("worker", project_dir=cls.project, home=home, skills_dir=cls.skills,
                                        user_disabled_providers=launcher.user_disabled_providers(
                                            cls.omp, cwd=cls.project, environment=os.environ) or ())
        cls.overlay = launcher.write_role_overlay(cls.root, "worker", overlay)
        cls.allowed = launcher.workbench_skill_names(cls.skills)
        cls.isolated = launcher.check_isolation(launcher.omp_command(cls.plan, cls.overlay), cwd=cls.project,
                                                environment=cls.env, role="worker", allowed_skills=cls.allowed,
                                                omp_version=cls.version, keep_raw=True)
        cls.marker_after_isolated = (cls.root / "canary-extension-loaded").exists()
        cls.base = launcher.check_isolation([cls.omp, "--extension", cls.plan.bridge_extension], cwd=cls.project,
                                            environment=cls.env, role="worker", allowed_skills=cls.allowed,
                                            omp_version=cls.version, keep_raw=True)
        cls.marker_after_base = (cls.root / "canary-extension-loaded").exists()
        sys_overlay = launcher.write_role_overlay(cls.root, "manager", launcher.role_overlay(
            "manager", project_dir=cls.sys_project, home=home, skills_dir=cls.skills))
        cls.sys_isolated = launcher.check_isolation(
            launcher.omp_command(cls.plan, sys_overlay), cwd=cls.sys_project, environment=cls.env, role="manager",
            allowed_skills=cls.allowed, omp_version=cls.version, keep_raw=True)
        cls.sys_base = launcher.check_isolation(
            [cls.omp, "--extension", cls.plan.bridge_extension], cwd=cls.sys_project, environment=cls.env,
            role="manager", allowed_skills=cls.allowed, omp_version=cls.version, keep_raw=True)
        evidence = {name: {key: value for key, value in result.items() if key != "raw"}
                    for name, result in (("isolated", cls.isolated), ("base", cls.base),
                                         ("system_md_isolated", cls.sys_isolated), ("system_md_base", cls.sys_base))}
        evidence["extension_marker"] = {"isolated": cls.marker_after_isolated, "base": cls.marker_after_base}
        print("\nLIVE-OMP-ISOLATION " + json.dumps(evidence, sort_keys=True))

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def raw_text(self, result):
        return json.dumps(result.get("raw"))

    def test_isolated_command_loads_only_workbench_skill(self):
        result, observed = self.isolated, self.isolated["observed"]
        self.assertEqual((result["state"], result["error"]), ("ok", None), result["leaks"])
        self.assertNotIn("CANARY", self.raw_text(result))
        self.assertEqual(observed["context_files"], [])
        self.assertEqual(observed["rules"], [])
        self.assertEqual(observed["mcp_tools"], [])
        self.assertNotIn("canary-agent", observed["task_agents"])
        self.assertEqual(observed["skills"], ["wb-canary-skill"])
        self.assertEqual(observed["skill_commands"], ["wb-canary-skill"])
        self.assertIn("WB_WORKBENCH_SKILL", self.raw_text(result))
        for name in ambient_user_skill_names():
            self.assertNotIn(name, observed["skills"] + observed["skill_commands"])
        self.assertFalse(self.marker_after_isolated, "project extension ran under isolation")
        self.assertEqual(result["extension_errors"], [])  # the explicit bridge -e still loads
        self.assertEqual((result["cleanup"]["state"], result["cleanup"]["group_left"]), ("dead", []))

    def test_negative_control_without_isolation_shows_the_canaries(self):
        result, observed = self.base, self.base["observed"]
        self.assertEqual(result["state"], "leak", result["error"])
        leaks = set(result["leaks"])
        self.assertTrue(any(item.startswith("context_file:") and str(self.project) in item for item in leaks))
        self.assertTrue({"skill:canary-claude", "skill:canary-agents", "skill:canary-omp"} <= leaks)
        self.assertIn("rule:CANARY_RULE_BODY", leaks)
        self.assertIn("mcp:mcp__canary_tool", leaks)
        self.assertIn("task_agent:canary-agent", leaks)
        self.assertTrue(any(item.startswith("command:canary-cmd") for item in leaks))
        self.assertTrue(self.marker_after_base, "project extension should run without isolation")
        self.assertNotIn("wb-canary-skill", observed["skills"])
        for name in ambient_user_skill_names():
            self.assertIn(f"skill:{name}", leaks)
        self.assertEqual((result["cleanup"]["state"], result["cleanup"]["group_left"]), ("dead", []))

    def test_project_system_md_is_ignored_under_isolation_and_flagged_without(self):
        self.assertEqual(self.sys_isolated["state"], "ok", self.sys_isolated["leaks"])
        self.assertTrue(self.sys_isolated["observed"]["system_prompt_default"])
        self.assertNotIn("CANARY", self.raw_text(self.sys_isolated))
        self.assertIn("CANARY_SYSTEM_MD", self.raw_text(self.sys_base))
        self.assertIn("system_prompt:replaced", self.sys_base["leaks"])
        for result in (self.sys_isolated, self.sys_base):
            self.assertEqual((result["cleanup"]["state"], result["cleanup"]["group_left"]), ("dead", []))

    def test_isolation_keeps_the_users_model_and_auth(self):
        self.assertIsNotNone(self.isolated["observed"]["model"])
        self.assertEqual(self.isolated["observed"]["model"], self.base["observed"]["model"])
        self.assertNotIn("--profile", launcher.omp_command(self.plan, self.overlay))
        self.assertNotIn("PI_CODING_AGENT_DIR", {k for k in self.env if k not in os.environ})


if __name__ == "__main__":
    unittest.main()
