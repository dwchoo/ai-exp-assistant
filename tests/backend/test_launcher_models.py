"""C-D68 launcher units: model table, worker tool allowlist, per-role subagent sets, leak rules.

No OMP process is started except a fake RPC script; no model request.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest

from workbench.backend import launcher
from workbench.backend.launcher import LaunchPlan

REPO = Path(__file__).resolve().parents[2]
SOL = "openai-codex/gpt-6.1-sol"
LUNA = "openai-codex/gpt-6-luna"

# C-D68 (4), exactly as decided (plan/task/memory/commit follow slow/default/tiny).
EXPECTED_MANAGER = {"default": f"{SOL}:high", "slow": f"{SOL}:high", "plan": f"{SOL}:high",
                    "smol": f"{LUNA}:max", "task": f"{LUNA}:max", "tiny": f"{LUNA}:max",
                    "memory": f"{LUNA}:max", "commit": f"{LUNA}:max"}
EXPECTED_WORKER = {"default": f"{LUNA}:max", "task": f"{LUNA}:max", "slow": f"{SOL}:high", "plan": f"{SOL}:high",
                   "smol": f"{LUNA}:xhigh", "tiny": f"{LUNA}:high", "memory": f"{LUNA}:high",
                   "commit": f"{LUNA}:high"}


def plan() -> LaunchPlan:
    return LaunchPlan(None, "/x/omp", "omp/18.6.1", "/x/bridge.ts")  # type: ignore[arg-type]


def project_and_home(root: Path) -> tuple[Path, Path]:
    project, home = root / "project", root / "home"
    project.mkdir()
    home.mkdir()
    return project, home


def observed(**fields):
    base = {"context_files": [], "skills": [], "skill_commands": [], "rules": [], "mcp_tools": [],
            "task_agents": [], "other_commands": [], "tools": [], "system_prompt_default": True,
            "autoqa": False, "browser": False}
    base.update(fields)
    return base


class ModelTableFileTests(unittest.TestCase):
    def test_workbench_table_is_exactly_the_c_d68_decision(self):
        table = launcher.load_model_table(launcher.default_models_file())
        self.assertEqual(table["manager"]["models"], EXPECTED_MANAGER)
        self.assertEqual(table["worker"]["models"], EXPECTED_WORKER)
        self.assertEqual(sorted(table["manager"]["fast"]), ["commit", "memory", "smol", "task", "tiny"])
        self.assertEqual(sorted(table["worker"]["fast"]), sorted(launcher.OMP_MODEL_ROLES))

    def test_table_file_has_no_secret_shaped_content_and_is_in_the_repo_dir(self):
        text = "\n".join(line for line in launcher.default_models_file().read_text().splitlines()
                         if not line.startswith("#"))
        self.assertEqual(launcher.default_models_file(), REPO / "omp_bridge" / "omp-models.yml")
        self.assertIsNone(re.search(r"(?i)api[_-]?key|token|secret|password", text))

    def test_parse_refuses_what_it_does_not_understand(self):
        good = launcher.default_models_file().read_text()
        bad = {
            "unknown section": good.replace("worker:", "workers:", 1),
            "duplicate role entry": good.replace("  tiny: openai-codex/gpt-6-luna:max\n",
                                                 "  tiny: openai-codex/gpt-6-luna:max\n  tiny: openai-codex/gpt-6-luna:max\n", 1),
            "no thinking level": good.replace("gpt-6.1-sol:high", "gpt-6.1-sol", 1),
            "bad thinking level": good.replace("gpt-6.1-sol:high", "gpt-6.1-sol:turbo", 1),
            "unknown fast role": good.replace("fast: [smol,", "fast: [bogus, smol,", 1),
            "missing role": good.replace("  commit: openai-codex/gpt-6-luna:max\n", "", 1),
            "tab indent": good.replace("  default: openai-codex/gpt-6.1-sol:high", "\tdefault: openai-codex/gpt-6.1-sol:high", 1),
        }
        for label, text in bad.items():
            with self.subTest(label), self.assertRaises(launcher.ModelTableError):
                launcher.parse_model_table(text)

    def test_missing_file_is_an_error_not_a_default(self):
        with self.assertRaises(launcher.ModelTableError):
            launcher.load_model_table("/nonexistent/omp-models.yml")

    def test_selector_helpers(self):
        self.assertEqual((launcher.selector_model(f"{SOL}:high"), launcher.selector_level(f"{SOL}:high")),
                         (SOL, "high"))


class ModelOverlayTests(unittest.TestCase):
    def setUp(self):
        self.table = launcher.load_model_table()

    def test_manager_main_session_is_not_fast_but_its_cheap_subagents_are(self):
        overlay = launcher.model_overlay("manager", self.table)
        self.assertEqual(overlay["modelRoles"], EXPECTED_MANAGER)
        self.assertEqual(overlay["tier"], {"openai": "none"})
        tiers = overlay["agentServiceTierOverrides"]
        self.assertEqual({name: tiers[name] for name in ("scout", "sonic", "task")}, dict.fromkeys(("scout", "sonic", "task"), "priority"))
        self.assertEqual({name: tiers[name] for name in ("reviewer", "security-reviewer")},
                         dict.fromkeys(("reviewer", "security-reviewer"), "none"))

    def test_worker_is_fast_everywhere(self):
        overlay = launcher.model_overlay("worker", self.table)
        self.assertEqual(overlay["modelRoles"], EXPECTED_WORKER)
        self.assertEqual(overlay["tier"], {"openai": "priority"})
        self.assertEqual(set(overlay["agentServiceTierOverrides"].values()), {"priority"})
        self.assertEqual(set(overlay["agentServiceTierOverrides"]),
                         set(launcher.BUNDLED_AGENT_ROLES) | set(launcher.WORKBENCH_AGENT_ROLES))

    def test_every_agent_maps_to_a_known_model_role(self):
        for mapping in (launcher.BUNDLED_AGENT_ROLES, launcher.WORKBENCH_AGENT_ROLES):
            self.assertTrue(set(mapping.values()) <= set(launcher.OMP_MODEL_ROLES))


class RoleOverlayTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cd68-ov-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.project, self.home = project_and_home(self.root)

    def overlay(self, role, **kwargs):
        return launcher.role_overlay(role, project_dir=self.project, home=self.home, **kwargs)

    def test_overlay_carries_the_model_table_per_role(self):
        manager, worker = self.overlay("manager"), self.overlay("worker")
        self.assertEqual(manager["modelRoles"], EXPECTED_MANAGER)
        self.assertEqual(worker["modelRoles"], EXPECTED_WORKER)
        self.assertEqual((manager["tier"], worker["tier"]), ({"openai": "none"}, {"openai": "priority"}))
        self.assertEqual(manager["task"]["agentServiceTierOverrides"]["task"], "priority")
        self.assertEqual(worker["task"]["agentServiceTierOverrides"]["explorer"], "priority")

    def test_an_explicit_table_wins_over_the_file(self):
        table = launcher.load_model_table()
        table["worker"]["models"]["smol"] = f"{LUNA}:low"
        self.assertEqual(self.overlay("worker", models=table)["modelRoles"]["smol"], f"{LUNA}:low")

    def test_manager_keeps_bundled_agents_and_loses_the_workbench_ones(self):
        agents = self.overlay("manager")["task"]["disabledAgents"]
        self.assertEqual(sorted(agents), sorted(launcher.WORKBENCH_AGENT_ROLES))
        self.assertFalse(set(agents) & launcher.BUNDLED_TASK_AGENTS)

    def test_worker_loses_every_bundled_agent_and_keeps_the_workbench_ones(self):
        agents = self.overlay("worker")["task"]["disabledAgents"]
        self.assertEqual(set(agents), launcher.BUNDLED_TASK_AGENTS)
        self.assertEqual(launcher.BUNDLED_TASK_AGENTS, set(launcher.BUNDLED_AGENT_ROLES))

    def test_only_the_worker_has_bash_and_eval_switched_off(self):
        worker, manager = self.overlay("worker"), self.overlay("manager")
        self.assertEqual((worker["bash"], worker["eval"]), ({"enabled": False}, {"py": False, "js": False}))
        self.assertNotIn("bash", manager)
        self.assertNotIn("eval", manager)

    def test_ambient_project_agents_are_still_disabled_for_both_roles(self):
        agents_dir = self.project / ".omp" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "canary.md").write_text("---\nname: canary-agent\ndescription: x\n---\nbody\n")
        for role in launcher.OMP_ROLE_NAMES:
            self.assertIn("canary-agent", self.overlay(role)["task"]["disabledAgents"])

    def test_workbench_definitions_in_the_home_agent_dir_are_not_ambient(self):
        env = {"PI_CONFIG_DIR": ".omp"}
        installed = self.home / ".omp" / "agent" / "agents"
        installed.mkdir(parents=True)
        for source in launcher.default_agents_dir().glob("*.md"):
            (installed / source.name).write_bytes(source.read_bytes())
        (installed / "stray.md").write_text("---\nname: stray-agent\ndescription: x\n---\nbody\n")
        names = launcher.task_agent_names(self.project, self.home, env, tuple(launcher.WORKBENCH_AGENT_ROLES))
        self.assertEqual(names, ("stray-agent",))
        # without the exclusion (the C-D59 call) the Workbench definitions would count as ambient
        self.assertIn("explorer", launcher.task_agent_names(self.project, self.home, env))

    def test_a_project_definition_shadowing_a_workbench_name_stays_ambient(self):
        agents_dir = self.project / ".omp" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "explorer.md").write_text("---\nname: explorer\ndescription: shadow\n---\nbody\n")
        self.assertIn("explorer", launcher.task_agent_names(self.project, self.home, {},
                                                           tuple(launcher.WORKBENCH_AGENT_ROLES)))

    def test_overlay_is_json_serialisable_and_written_private(self):
        path = launcher.write_role_overlay(self.root, "worker", self.overlay("worker"))
        self.assertEqual(json.loads(path.read_text())["modelRoles"], EXPECTED_WORKER)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class WorkerToolsTests(unittest.TestCase):
    def test_worker_allowlist_has_no_bash_or_eval_and_keeps_the_file_tools(self):
        self.assertFalse({"bash", "eval"} & set(launcher.WORKER_TOOLS))
        self.assertTrue({"read", "grep", "glob", "edit", "write", "web_search", "todo", "task"} <= set(launcher.WORKER_TOOLS))
        self.assertEqual(launcher.WORKER_FORBIDDEN_TOOLS, {"bash", "eval"})

    def test_worker_command_passes_tools_before_user_arguments(self):
        command = launcher.omp_command(LaunchPlan(None, "/x/omp", "omp/18.6.1", "/x/bridge.ts", ("--model", "m")),  # type: ignore[arg-type]
                                       "/d/omp-isolation-worker.yml")
        index = command.index("--tools")
        self.assertEqual(command[index + 1], ",".join(launcher.WORKER_TOOLS))
        self.assertLess(index, command.index("--model"))
        self.assertEqual(command[-2:], ["--extension", "/x/bridge.ts"])

    def test_manager_command_has_no_tool_restriction(self):
        self.assertNotIn("--tools", launcher.omp_command(plan(), "/d/omp-isolation-manager.yml"))

    def test_role_comes_from_the_overlay_name_or_the_argument(self):
        self.assertEqual(launcher.overlay_role("/d/omp-isolation-worker.yml"), "worker")
        self.assertEqual(launcher.overlay_role("/d/omp-isolation-manager.yml"), "manager")
        self.assertIsNone(launcher.overlay_role("/d/other.yml"))
        self.assertNotIn("--tools", launcher.omp_command(plan(), "/d/other.yml"))
        self.assertIn("--tools", launcher.omp_command(plan(), "/d/other.yml", role="worker"))
        self.assertNotIn("--tools", launcher.omp_command(plan(), "/d/omp-isolation-worker.yml", role="manager"))


class LeakRuleTests(unittest.TestCase):
    def test_worker_bash_eval_and_bundled_agents_are_leaks(self):
        seen = observed(tools=["read", "bash", "eval", "task"], task_agents=["scout", "task", "explorer", "analyst"])
        leaks = launcher.isolation_leaks(seen, allowed_skills=(), role="worker")
        self.assertEqual(leaks, ["task_agent:scout", "task_agent:task", "tool:bash", "tool:eval"])

    def test_the_worker_has_no_wait_tool_and_wait_is_a_worker_leak(self):
        # p27-cd68-fix-05 (root-adjudication-p27-cd68-wait): task subagent results auto-deliver in OMP 18.6.1 and
        # the worker has no async bash job, so `wait` only invited waiting on a terminal command (smoke-02 M4).
        self.assertNotIn("wait", launcher.WORKER_TOOLS)
        self.assertEqual(set(launcher.WORKER_TOOLS),
                         {"read", "grep", "glob", "edit", "write", "web_search", "todo", "task"})
        worker = observed(tools=list(launcher.WORKER_TOOLS) + ["to_manager", "terminal", "wait"],
                          task_agents=["analyst", "explorer"])
        self.assertEqual(launcher.isolation_leaks(worker, allowed_skills=(), role="worker"), ["tool:wait"])
        manager = observed(tools=["bash", "eval", "task", "wait", "to_worker"], task_agents=sorted(launcher.BUNDLED_TASK_AGENTS))
        self.assertEqual(launcher.isolation_leaks(manager, allowed_skills=(), role="manager"), [], "manager keeps wait")
        command = launcher.omp_command(plan(), "/d/omp-isolation-worker.yml")
        self.assertNotIn("wait", command[command.index("--tools") + 1].split(","))

    def test_any_worker_tool_outside_the_allowlist_is_a_leak(self):
        # p27-cd68-review-01 P3-3: --omp-arg --tools could re-enable python/notebook/computer/browser.
        seen = observed(tools=list(launcher.WORKER_TOOLS) + ["to_manager", "terminal", "python", "notebook",
                                                              "computer", "ssh", "mcp__x__y"],
                        task_agents=["analyst", "explorer"])
        self.assertEqual(launcher.isolation_leaks(seen, allowed_skills=(), role="worker"),
                         ["tool:computer", "tool:notebook", "tool:python", "tool:ssh"])
        self.assertEqual(launcher.WORKER_BRIDGE_TOOLS, frozenset({"to_manager", "terminal"}))
        manager = observed(tools=["bash", "eval", "python", "to_worker"])
        self.assertEqual(launcher.isolation_leaks(manager, allowed_skills=(), role="manager"), [])

    def test_clean_worker_has_no_leaks(self):
        seen = observed(tools=["read", "grep", "task", "to_manager"], task_agents=["analyst", "explorer"])
        self.assertEqual(launcher.isolation_leaks(seen, allowed_skills=(), role="worker"), [])

    def test_manager_keeps_bash_and_bundled_agents_but_not_workbench_agents(self):
        clean = observed(tools=["bash", "eval", "task"], task_agents=sorted(launcher.BUNDLED_TASK_AGENTS))
        self.assertEqual(launcher.isolation_leaks(clean, allowed_skills=(), role="manager"), [])
        mixed = observed(task_agents=["scout", "explorer", "canary-agent"])
        self.assertEqual(launcher.isolation_leaks(mixed, allowed_skills=(), role="manager"),
                         ["task_agent:explorer", "task_agent:canary-agent"])

    def test_without_a_role_the_c_d59_rule_stays(self):
        seen = observed(tools=["bash"], task_agents=["scout", "explorer"])
        self.assertEqual(launcher.isolation_leaks(seen, allowed_skills=()), ["task_agent:explorer"])

    def test_new_overlay_keys_create_no_false_leaks(self):
        # the observed set of a correctly configured role is exactly what the overlay produces
        for role, agents in (("manager", sorted(launcher.BUNDLED_TASK_AGENTS)), ("worker", ["analyst", "explorer"])):
            self.assertEqual(launcher.isolation_leaks(observed(task_agents=agents), allowed_skills=(), role=role), [])

    def test_a_missing_workbench_agent_is_a_warning_not_a_leak(self):
        self.assertEqual(launcher.role_expectation_warnings("worker", observed(tools=["task"], task_agents=["analyst"])),
                         ["task_agent:missing:explorer"])
        self.assertEqual(launcher.role_expectation_warnings("worker", observed(tools=["task"], task_agents=["analyst", "explorer"])), [])
        self.assertEqual(launcher.role_expectation_warnings("worker", observed(tools=[], task_agents=[])), [])
        self.assertEqual(launcher.role_expectation_warnings("manager", observed(tools=["task"], task_agents=[])), [])


FAKE_OMP = r'''#!{python}
import json, sys
tools = {tools!r}
agents = {agents!r}
description = "# Available Agents\n" + "".join("- `%s` (x): y\n" % a for a in agents)
state = {{"systemPrompt": "You are omp's assistant", "model": {{"provider": "openai-codex", "id": "gpt-6-luna"}},
          "thinkingLevel": "max",
          "dumpTools": [{{"name": n, "description": description if n == "task" else ""}} for n in tools]}}
for line in sys.stdin:
    request = json.loads(line)
    data = state if request["type"] == "get_state" else {{"commands": []}}
    print(json.dumps({{"id": request["id"], "type": "response", "command": request["type"], "success": True, "data": data}}), flush=True)
'''


class FakeCheckTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cd68-chk-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def check(self, role, tools, agents):
        fake = self.root / "omp"
        fake.write_text(FAKE_OMP.format(python=sys.executable, tools=tools, agents=agents))
        fake.chmod(0o700)
        env = {"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(self.root / "home")}
        return launcher.check_isolation([str(fake)], cwd=self.root, environment=env, role=role, allowed_skills=(),
                                        omp_version="omp/18.6.1", timeout=10.0)

    def test_worker_with_bash_and_a_bundled_agent_is_reported(self):
        result = self.check("worker", ["read", "bash", "eval", "task"], ["scout", "explorer", "analyst"])
        self.assertEqual((result["state"], result["ok"]), ("leak", False))
        self.assertEqual(result["leaks"], ["task_agent:scout", "tool:bash", "tool:eval"])
        self.assertEqual(result["observed"]["thinking_level"], "max")
        self.assertEqual(result["observed"]["model"], LUNA)

    def test_correct_worker_and_manager_are_ok(self):
        worker = self.check("worker", ["read", "task", "to_manager"], ["analyst", "explorer"])
        manager = self.check("manager", ["read", "bash", "eval", "task"], sorted(launcher.BUNDLED_TASK_AGENTS))
        self.assertEqual((worker["state"], worker["leaks"], worker["warnings"]), ("ok", [], []))
        self.assertEqual((manager["state"], manager["leaks"], manager["warnings"]), ("ok", [], []))

    def test_worker_missing_a_workbench_agent_warns(self):
        result = self.check("worker", ["read", "task"], ["analyst"])
        self.assertEqual((result["state"], result["leaks"], result["warnings"]),
                         ("warning", [], ["task_agent:missing:explorer"]))

    def test_manager_showing_a_workbench_agent_is_a_leak(self):
        result = self.check("manager", ["task"], ["scout", "explorer"])
        self.assertEqual(result["leaks"], ["task_agent:explorer"])


class WorkbenchAgentFileTests(unittest.TestCase):
    def files(self):
        return sorted(launcher.default_agents_dir().glob("*.md"))

    def frontmatter(self, path: Path) -> str:
        text = path.read_text()
        self.assertTrue(text.startswith("---\n"))
        return text.split("---\n", 2)[1]

    def test_exactly_the_two_workbench_agents_exist_with_the_declared_names(self):
        self.assertEqual({path.stem for path in self.files()}, set(launcher.WORKBENCH_AGENT_ROLES))
        for path in self.files():
            self.assertEqual(launcher._frontmatter_name(path), path.stem)
            self.assertEqual(launcher._agent_definition_name(path), path.stem)  # OMP would register it

    def test_agents_have_no_command_or_write_tools(self):
        for path in self.files():
            front = self.frontmatter(path)
            tools = re.findall(r"^  - (\S+)$", front.split("tools:", 1)[1].split("model:", 1)[0], re.M)
            self.assertTrue({"read", "grep", "glob", "yield"} <= set(tools), path.name)
            self.assertFalse({"bash", "eval", "edit", "write", "task", "ssh", "python"} & set(tools), path.name)

    def test_agents_run_on_their_role_model(self):
        for path in self.files():
            role = launcher.WORKBENCH_AGENT_ROLES[path.stem]
            self.assertRegex(self.frontmatter(path), rf'model:\n  - "@{role}"', path.name)

    def test_descriptions_are_short_english_and_names_do_not_collide_with_bundled_agents(self):
        for path in self.files():
            description = re.search(r"^description: (.+)$", self.frontmatter(path), re.M).group(1)
            self.assertTrue(description.isascii())
            self.assertLess(len(description), 260)
        self.assertFalse(set(launcher.WORKBENCH_AGENT_ROLES) & launcher.BUNDLED_TASK_AGENTS)


class IsolationOverlayFileTests(unittest.TestCase):
    def test_codex_code_mode_is_pinned_off_for_both_roles(self):
        text = launcher.default_isolation_overlay().read_text()
        self.assertRegex(text, r'(?m)^providers:\n(  #.*\n)*  openai-codex:\n    codeMode: "off"$')

    def test_static_overlay_still_matches_the_provider_id_constant(self):
        text = launcher.default_isolation_overlay().read_text()
        listed = re.search(r"(?m)^disabledProviders: \[(.*)\]$", text).group(1).split(", ")
        self.assertEqual(tuple(listed), launcher.ISOLATION_PROVIDER_IDS)

    def test_isolation_evidence_version_is_the_checked_omp(self):
        self.assertEqual(launcher.EVIDENCE_OMP_VERSIONS["isolation"], "18.6.1")


if __name__ == "__main__":
    unittest.main()
