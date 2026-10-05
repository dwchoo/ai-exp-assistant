"""C-D68 independent unit tests (p27-cd68-test-01): model table, code mode, worker tool/agent restrictions, role leaks.

Expected values are transcribed from DECISIONS.md C-D68 (4) (the user's words: "luna는 무조건 max와 fast",
"worker default luna max fast, slow sol high fast, smol luma xhigh fast, tiny luna high fast"; Root fills
plan = slow, task = default, memory/commit = tiny), not from omp-models.yml. No OMP, no provider.
The live counterpart (real OMP 18.6.1 + local fake provider) is ``live_omp_tools_independent_p27cd68.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))

import test_omp_isolation_independent_p27m as p27m  # noqa: E402
from workbench.backend import launcher  # noqa: E402

SOL_HIGH = "openai-codex/gpt-6.1-sol:high"
LUNA_MAX = "openai-codex/gpt-6-luna:max"
LUNA_XHIGH = "openai-codex/gpt-6-luna:xhigh"
LUNA_HIGH = "openai-codex/gpt-6-luna:high"
# C-D68 (4): (selector, fast) per OMP model role.
DECISION = {
    "manager": {"default": (SOL_HIGH, False), "slow": (SOL_HIGH, False), "plan": (SOL_HIGH, False),
                "smol": (LUNA_MAX, True), "task": (LUNA_MAX, True), "tiny": (LUNA_MAX, True),
                "memory": (LUNA_MAX, True), "commit": (LUNA_MAX, True)},
    "worker": {"default": (LUNA_MAX, True), "task": (LUNA_MAX, True), "slow": (SOL_HIGH, True),
               "plan": (SOL_HIGH, True), "smol": (LUNA_XHIGH, True), "tiny": (LUNA_HIGH, True),
               "memory": (LUNA_HIGH, True), "commit": (LUNA_HIGH, True)},
}
# Subagent -> model role: OMP 18.6.1 bundled agents (manager) and the Workbench definitions (worker, C-D68 (2)).
MANAGER_AGENTS = {"scout": "smol", "reviewer": "slow", "task": "task"}
WORKER_AGENTS = {"explorer": "smol", "analyst": "slow"}
BUNDLED = p27m.BUNDLED_AGENTS
REPO = Path(__file__).resolve().parents[2]


def frontmatter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n"), path
    head = text[4:text.index("\n---\n", 4)]
    data: dict = {}
    key = None
    for line in head.splitlines():
        if line.startswith("  - ") and key:
            data.setdefault(key, []).append(line[4:].strip().strip('"'))
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            if value.strip():
                data[key] = value.strip()
    return data


class ModelTableTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p27cd68-models-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def overlay(self, role, **kwargs):
        return launcher.role_overlay(role, project_dir=self.root / "p", home=self.root / "h", **kwargs)

    def test_the_owned_table_is_exactly_the_users_decision(self):
        table = launcher.load_model_table()
        self.assertEqual(launcher.default_models_file(), REPO / "omp_bridge" / "omp-models.yml",
                         "one Workbench-owned file (C-D68 (4))")
        for role, expected in DECISION.items():
            with self.subTest(role=role):
                self.assertEqual(table[role]["models"], {k: v[0] for k, v in expected.items()})
                self.assertEqual(set(table[role]["fast"]), {k for k, v in expected.items() if v[1]})

    def test_every_start_overlay_carries_the_table_per_role(self):
        for role, expected in DECISION.items():
            with self.subTest(role=role):
                overlay = self.overlay(role)  # no models= : the start path reads the owned file
                self.assertEqual(overlay["modelRoles"], {k: v[0] for k, v in expected.items()})
                main_fast = expected["default"][1]
                self.assertEqual(overlay["tier"]["openai"] == "priority", main_fast,
                                 f"{role}: the main session is fast exactly when its default role is")
                tiers = overlay["task"]["agentServiceTierOverrides"]
                agents = WORKER_AGENTS if role == "worker" else MANAGER_AGENTS
                for agent, agent_role in agents.items():
                    self.assertEqual(tiers.get(agent) == "priority", expected[agent_role][1],
                                     f"{role}/{agent} ({agent_role}) follows its role's fast setting: {tiers}")

    def test_the_table_is_not_on_the_command_line_so_an_in_session_change_stays_possible(self):
        # C-D68 (4): applied through the overlay at every start; /model inside OMP may still change it.
        for role in ("manager", "worker"):
            path = launcher.write_role_overlay(self.root, role, self.overlay(role))
            argv = launcher.omp_command(p27m.plan_for(), path)
            for flag in ("--model", "--models", "--thinking", "--smol", "--slow", "--plan"):
                self.assertNotIn(flag, argv, f"{role}: {flag} would pin the model over in-session changes")

    def test_a_changed_table_applies_at_the_next_start_and_bad_tables_are_refused(self):
        custom = launcher.parse_model_table((REPO / "omp_bridge" / "omp-models.yml").read_text().replace(
            "worker:\n  default: openai-codex/gpt-6-luna:max", "worker:\n  default: openai-codex/gpt-6-luna:low"))
        self.assertEqual(self.overlay("worker", models=custom)["modelRoles"]["default"], "openai-codex/gpt-6-luna:low")
        good = (REPO / "omp_bridge" / "omp-models.yml").read_text()
        for bad in (good.replace("  commit: openai-codex/gpt-6-luna:high\n", ""),        # missing role
                    good.replace("gpt-6-luna:xhigh", "gpt-6-luna:turbo"),                # unknown thinking level
                    good.replace("fast: [smol, task, tiny, memory, commit]", "fast: [smol, bogus]"),
                    good.replace("manager:\n", "observer:\n")):
            with self.subTest(bad=bad[:0]):
                self.assertNotEqual(bad, good, "fixture did not change the table")
                with self.assertRaises(launcher.ModelTableError):
                    launcher.parse_model_table(bad)
        missing = self.root / "absent.yml"
        with self.assertRaises(launcher.ModelTableError):
            launcher.load_model_table(missing)


class CodeModeTests(unittest.TestCase):
    def test_codex_code_mode_is_off_for_both_omps(self):
        # C-D68 (5): providers.openai-codex.codeMode: off in the overlay both OMPs load with --config
        data = p27m.parse_yaml_subset(p27m.STATIC_OVERLAY.read_text())
        self.assertEqual(data["providers"]["openai-codex"]["codeMode"], "off")
        root = Path(tempfile.mkdtemp(prefix="p27cd68-cm-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        for role in ("manager", "worker"):
            overlay = launcher.role_overlay(role, project_dir=root, home=root)
            self.assertNotIn("codeMode", json.dumps(overlay), "the role overlay must not turn code mode back on")
            argv = launcher.omp_command(p27m.plan_for(), launcher.write_role_overlay(root, role, overlay))
            configs = [argv[i + 1] for i, a in enumerate(argv) if a == "--config"]
            self.assertEqual(configs[0], str(p27m.STATIC_OVERLAY), f"{role} loads the static overlay")


class WorkerRestrictionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p27cd68-wr-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def command(self, role, *args):
        overlay = launcher.write_role_overlay(self.root, role, launcher.role_overlay(
            role, project_dir=self.root, home=self.root))
        return launcher.omp_command(p27m.plan_for(args=args), overlay)

    def test_the_worker_tools_allowlist_has_no_command_tool_and_keeps_the_decided_tools(self):
        argv = self.command("worker")
        self.assertEqual(argv.count("--tools"), 1)
        tools = argv[argv.index("--tools") + 1].split(",")
        self.assertFalse({"bash", "eval", "python", "browser", "ssh"} & set(tools), tools)
        self.assertTrue({"read", "grep", "glob", "edit", "write", "web_search", "todo"} <= set(tools), tools)
        self.assertIn("task", tools, "the worker keeps subagents (Workbench-only, C-D68 (2))")
        self.assertLess(argv.index("--tools"), argv.index("--extension"))
        self.assertNotIn("--tools", self.command("manager"), "C-D68 (3): the manager keeps OMP's tools")

    def test_the_role_follows_the_overlay_file_not_a_guess(self):
        # The role comes from the generated overlay name; an explicit role wins.
        plan = p27m.plan_for()
        self.assertIn("--tools", launcher.omp_command(plan, self.root / "omp-isolation-worker.yml"))
        self.assertNotIn("--tools", launcher.omp_command(plan, self.root / "omp-isolation-manager.yml"))
        self.assertIn("--tools", launcher.omp_command(plan, self.root / "x.yml", role="worker"))

    def test_agent_sets_per_role(self):
        worker = launcher.role_overlay("worker", project_dir=self.root, home=self.root)["task"]["disabledAgents"]
        manager = launcher.role_overlay("manager", project_dir=self.root, home=self.root)["task"]["disabledAgents"]
        self.assertTrue(BUNDLED <= set(worker), worker)
        self.assertFalse(set(WORKER_AGENTS) & set(worker), worker)
        self.assertTrue(set(WORKER_AGENTS) <= set(manager), manager)
        self.assertFalse(BUNDLED & set(manager), manager)

    def test_the_workbench_agent_definitions_have_no_command_or_write_tool_and_their_role_model(self):
        directory = REPO / "omp_bridge" / "agents"
        found = {}
        for path in sorted(directory.glob("*.md")):
            data = frontmatter(path)
            found[data["name"]] = data
        self.assertEqual(set(found), set(WORKER_AGENTS), "only an exploration and an analysis agent (C-D68 (2))")
        for name, role in WORKER_AGENTS.items():
            tools = set(found[name].get("tools") or [])
            self.assertTrue(tools, f"{name}: an explicit tool list (no inherited default tools)")
            self.assertFalse(tools & {"bash", "eval", "terminal", "edit", "write", "task", "to_manager", "browser"},
                             f"{name}: {tools}")
            self.assertEqual(found[name].get("model"), [f"@{role}"], f"{name} follows its {role} role")


class RoleLeakTests(unittest.TestCase):
    """``isolation_leaks`` per role with negative controls (observed shapes like observe_isolation's output)."""

    @staticmethod
    def observed(tools=(), agents=()):
        return {"system_prompt_default": True, "context_files": [], "skills": [], "skill_commands": [], "rules": [],
                "mcp_tools": [], "task_agents": list(agents), "other_commands": [], "tools": list(tools),
                "autoqa": False, "browser": False}

    def test_command_tools_leak_only_for_the_worker(self):
        for tool in ("bash", "eval"):
            self.assertIn(f"tool:{tool}", launcher.isolation_leaks(self.observed([tool, "read"]), allowed_skills=(),
                                                                   role="worker"))
            self.assertEqual(launcher.isolation_leaks(self.observed([tool, "read"]), allowed_skills=(),
                                                      role="manager"), [])
        self.assertEqual(launcher.isolation_leaks(self.observed(["read", "terminal", "to_manager", "task"]),
                                                  allowed_skills=(), role="worker"), [])

    def test_agents_leak_per_role(self):
        for name in sorted(BUNDLED):
            self.assertEqual(launcher.isolation_leaks(self.observed(agents=[name]), allowed_skills=(), role="worker"),
                             [f"task_agent:{name}"])
            self.assertEqual(launcher.isolation_leaks(self.observed(agents=[name]), allowed_skills=(),
                                                      role="manager"), [])
        for name in WORKER_AGENTS:
            self.assertEqual(launcher.isolation_leaks(self.observed(agents=[name]), allowed_skills=(),
                                                      role="manager"), [f"task_agent:{name}"])
            self.assertEqual(launcher.isolation_leaks(self.observed(agents=[name]), allowed_skills=(), role="worker"),
                             [])
        self.assertEqual(launcher.isolation_leaks(self.observed(agents=["canary"]), allowed_skills=(),
                                                  role="worker"), ["task_agent:canary"])


if __name__ == "__main__":
    unittest.main()
