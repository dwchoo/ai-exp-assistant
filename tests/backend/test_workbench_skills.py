"""CW-18 U6: Workbench role skills (to-worker for manager, to-manager for worker).

No OMP and no model calls: the skill files, the role filter, the overlay and the
isolation expectations are checked as data.
"""
from pathlib import Path
import inspect
import json
import re
import unittest

from workbench.backend import flow_tasks, launcher

SKILLS = launcher.default_skills_dir()
BRIDGE = Path(launcher.default_bridge_extension()).read_text(encoding="utf-8")
FLOW_TASKS = Path(inspect.getsourcefile(flow_tasks)).read_text(encoding="utf-8")


def frontmatter(name: str) -> dict[str, str]:
    text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, f"{name}: no frontmatter"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


def body(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def schema_properties(constant: str) -> set[str]:
    """Top-level property names of a bridge.ts tool schema constant."""
    start = BRIDGE.index(f"const {constant} = {{")
    end = BRIDGE.index("\n};", start)
    block = BRIDGE[start:end]
    top = re.findall(r"^\t\t([a-z_]+):", block, re.M)
    return set(top)


class SkillFileTests(unittest.TestCase):
    def test_both_skills_exist_with_matching_frontmatter(self):
        for name in ("to-worker", "to-manager"):
            fields = frontmatter(name)
            self.assertEqual(fields["name"], name)
            self.assertTrue(fields["description"].strip())
            self.assertLessEqual(len(body(name).splitlines()), 90, "about one page")

    def test_only_these_two_skill_directories_exist(self):
        dirs = sorted(entry.name for entry in SKILLS.iterdir() if entry.is_dir())
        self.assertEqual(dirs, ["to-manager", "to-worker"])

    def test_to_worker_documents_every_tool_field(self):
        text = body("to-worker")
        for field in schema_properties("TO_WORKER_PARAMETERS") - {"properties"}:
            self.assertIn(field, text, f"to_worker field {field} is not explained")
        for needle in ("experiment", "work", "worker_busy", "held", "host_terminal_busy", "run: true",
                       "cancel", "task_id", "3", "execution", "criteria", "environment"):
            self.assertIn(needle, text)
        self.assertRegex(text, r"(?i)one task at a time")
        self.assertRegex(text, r"(?i)never.*(value|secret)")
        self.assertRegex(text, r"(?i)no push|never push")
        self.assertRegex(text, r"(?i)merge")

    def test_to_worker_documents_the_full_host_terminal_recovery_order(self):
        text = body("to-worker")
        line = next(l for l in text.splitlines() if "held:host_terminal_busy" in l)
        pos = 0
        for needle in ("prefix t", "finish or kill", "`wb-handoff`", "prefix h", "prefix t"):
            pos = line.find(needle, pos)
            self.assertGreaterEqual(pos, 0, f"{needle!r} missing or out of order in: {line}")
            pos += len(needle)

    def test_to_manager_documents_every_tool_field_and_the_marker_rule(self):
        text = body("to-manager")
        for field in schema_properties("TO_MANAGER_PARAMETERS"):
            self.assertIn(field, text, f"to_manager field {field} is not explained")
        for kind in ("answer", "progress", "done", "blocked", "report"):
            self.assertIn(kind, text)
        self.assertIn("response_contract", text)
        self.assertIn("WB_WORKER_RESPONSE:", text)
        self.assertRegex(text, r"(?i)no tools")
        self.assertRegex(text, r"(?i)host terminal")
        self.assertRegex(text, r"(?i)inside the (task's )?paths")

    def test_marker_matches_the_bridge(self):
        match = re.search(r'WORKER_RESPONSE_MARKER = "([^"]+)"', BRIDGE)
        self.assertIn(match.group(1), body("to-manager"))

    def test_no_secret_looking_content(self):
        for name in ("to-worker", "to-manager"):
            self.assertNotRegex(body(name), r"(?i)(api[_-]?key|token)\s*[=:]\s*\S{8,}")


def emitted_notice_kinds() -> set[str]:
    """Every notice kind flow_tasks.py can put into a to_worker result's ``notices``."""
    kinds = set(re.findall(r'_notice\(kind="([a-z_]+)"', FLOW_TASKS))
    kinds |= set(re.findall(r'reason = "(report_[a-z_]+)" if', FLOW_TASKS))  # _report_lost
    kinds |= set(re.findall(r'lost = "(report_[a-z_]+)" if', FLOW_TASKS))
    kinds |= set(re.findall(r'else "(report_[a-z_]+)"', FLOW_TASKS))
    return kinds


def section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert match, f"no section {heading}"
    return match.group(1)


def backticked(text: str) -> set[str]:
    return set(re.findall(r"`([a-z_:]+)`", text))


class ResultDocumentationTests(unittest.TestCase):
    def test_emitted_notice_kinds_are_the_expected_ones(self):
        self.assertEqual(emitted_notice_kinds(), {"report_outcome_unknown", "report_not_delivered", "task_cancelled",
                                                  "task_not_started", "run_start_failed",
                                                  "run_judgment_unavailable"})  # smoke-04 G2

    def test_to_worker_documents_every_emitted_notice_and_no_stale_one(self):
        notices = section(body("to-worker"), "Notices")
        self.assertIn("notices", notices)
        documented = {name for name in backticked(notices)
                      if name.startswith(("report_", "task_", "run_")) and name != "task_id"}
        self.assertEqual(documented, emitted_notice_kinds())
        for name in documented:
            self.assertIn(f'"{name}"', FLOW_TASKS, f"stale notice name {name}")

    def test_to_worker_says_a_new_task_may_follow_the_report_in_the_same_turn(self):
        text = body("to-worker")
        self.assertRegex(text, r"(?i)same turn")
        self.assertRegex(text, r"(?i)worker is (already )?free")

    def test_to_worker_statuses_and_held_reasons_exist_in_code(self):
        results = section(body("to-worker"), "Results")
        for name in re.findall(r"`(held:[a-z_]+)`", results):
            reason = name.split(":", 1)[1]
            self.assertTrue(re.search(rf'"{reason}"|{reason}', FLOW_TASKS + Path(inspect.getsourcefile(
                flow_tasks)).with_name("flow.py").read_text(encoding="utf-8")), f"stale held reason {name}")

    def test_to_manager_documents_the_rejected_results_the_code_emits(self):
        results = section(body("to-manager"), "Results")
        flow = Path(inspect.getsourcefile(flow_tasks)).with_name("flow.py").read_text(encoding="utf-8")
        names = set(re.findall(r"`rejected:([a-z_]+)`", results)) | set(re.findall(r"`held:([a-z_]+)`", results))
        self.assertTrue({"task_not_delivered", "no_active_task", "unknown_task", "in_reply_to_required",
                         "no_task_message", "no_active_run"} <= names, names)
        for name in names:
            self.assertRegex(FLOW_TASKS + flow, rf'(rejected|held)\("{name}"', f"stale to_manager result {name}")
        text = body("to-manager")
        self.assertRegex(text, r"(?i)task_not_delivered.*has not reached you")
        self.assertRegex(text, r"(?i)wait for (it|the task)")


class SmokeCorrectionSkillTests(unittest.TestCase):
    """CW-18 smoke D1-D3 (p27-cw18-smoke-fix-01): null for unused fields, repo-relative paths, end the turn."""

    def test_to_worker_says_end_the_turn_and_never_wait_or_poll(self):
        text = body("to-worker")
        self.assertRegex(text, r"(?i)end your turn")
        self.assertRegex(text, r"(?i)arrives as a new message")
        self.assertRegex(text, r"(?i)never (use|call) the `wait` tool")
        self.assertRegex(text, r"(?i)(do not|never) poll")
        results = section(text, "Results")
        self.assertRegex(results, r"(?i)`dispatched`[^\n]*end your turn")
        self.assertRegex(results, r"(?i)`queued`[^\n]*end your turn")

    def test_to_worker_says_paths_are_repo_relative_and_unused_fields_null(self):
        text = body("to-worker")
        self.assertRegex(text, r"(?i)repo-relative")
        self.assertRegex(text, r"(?i)null")
        self.assertRegex(text, r"(?i)`spec\.execution`[^\n]*null[^\n]*work")
        self.assertNotRegex(text, r"(?i)wait for the report\.")

    def test_to_manager_says_end_the_turn_after_done_or_blocked_and_null_fields(self):
        text = body("to-manager")
        self.assertRegex(text, r"(?i)after (a )?`done` or `blocked`[^\n]*end your turn")
        self.assertRegex(text, r"(?i)null")
        self.assertRegex(text, r"(?i)`request`[^\n]*null")
        self.assertRegex(text, r"(?i)`requires_code_change`[^\n]*null")

    def test_bridge_descriptions_match_the_skills(self):
        self.assertRegex(BRIDGE, r"end your turn")
        self.assertRegex(BRIDGE, r"null when not used")

    def test_skills_stay_about_one_page(self):
        for name in ("to-worker", "to-manager"):
            text = body(name)
            self.assertLess(len(text.splitlines()), 70, name)
            self.assertLess(len(text), 9000, name)


class RoleFilterTests(unittest.TestCase):
    def test_role_patterns(self):
        self.assertEqual(launcher.ROLE_SKILL_PATTERNS["manager"], ("to-worker",))
        self.assertEqual(launcher.ROLE_SKILL_PATTERNS["worker"], ("to-manager",))

    def test_allowlists_follow_the_filter(self):
        self.assertEqual(launcher.role_skill_allowlist("manager"), ("to-worker",))
        self.assertEqual(launcher.role_skill_allowlist("worker"), ("to-manager",))

    def test_overlay_carries_the_role_filter_and_the_skills_dir(self):
        for role, expected in (("manager", ["to-worker"]), ("worker", ["to-manager"])):
            overlay = launcher.role_overlay(role, project_dir=SKILLS, home=SKILLS)
            self.assertEqual(overlay["skills"], {"customDirectories": [str(SKILLS)],
                                                  "includeSkills": expected, "ignoredSkills": []})
            json.dumps(overlay)

    def test_overlay_filters_are_disjoint(self):
        manager = set(launcher.ROLE_SKILL_PATTERNS["manager"])
        worker = set(launcher.ROLE_SKILL_PATTERNS["worker"])
        self.assertFalse(manager & worker)


class IsolationExpectationTests(unittest.TestCase):
    @staticmethod
    def observed(*names):
        return {"context_files": [], "skills": list(names), "skill_commands": list(names), "rules": [],
                "mcp_tools": [], "task_agents": [], "other_commands": [], "system_prompt_default": True}

    def test_role_skill_is_expected_for_its_role_only(self):
        manager = launcher.role_skill_allowlist("manager")
        worker = launcher.role_skill_allowlist("worker")
        self.assertEqual(launcher.isolation_leaks(self.observed("to-worker"), allowed_skills=manager), [])
        self.assertEqual(launcher.isolation_leaks(self.observed("to-manager"), allowed_skills=worker), [])
        self.assertEqual(launcher.isolation_leaks(self.observed("to-manager"), allowed_skills=manager),
                         ["skill:to-manager"])
        self.assertEqual(launcher.isolation_leaks(self.observed("to-worker"), allowed_skills=worker),
                         ["skill:to-worker"])

    def test_any_other_skill_is_still_a_leak(self):
        leaks = launcher.isolation_leaks(self.observed("to-worker", "orca-cli", "canary"),
                                         allowed_skills=launcher.role_skill_allowlist("manager"))
        self.assertEqual(leaks, ["skill:canary", "skill:orca-cli"])


class TerminalAndManagerRuleTests(unittest.TestCase):
    """C-D68 (1)/(3) (p27-cd68-terminal-01): the worker runs commands only with terminal; the manager does not
    do delegated work itself."""

    def test_to_manager_says_every_shell_command_goes_through_terminal(self):
        text = body("to-manager")
        commands = section(text, "Running commands: `terminal`")
        self.assertRegex(commands, r"(?i)every shell command[^\n]*`terminal` tool[^\n]*no other way to execute commands")
        for status in ("exited", "running", "host_terminal_busy", "terminal_command_running", "paused"):
            self.assertIn(f"`{status}`", commands, status)
        self.assertNotIn("timeout_seconds", commands, "C-D68 (9): the wait is fixed")
        self.assertRegex(commands, r"waits up to 120 s")
        for field in ("command", "exit_code", "output_tail", "log_path"):
            self.assertIn(f"`{field}`", commands, field)
        # C-D68 (8): on running the worker ends its turn; Workbench checks every 60 s and sends a completion
        # notice. No re-wait loop is suggested.
        self.assertRegex(commands, r"(?i)`running`[^\n]*end your turn[^\n]*do not start another command")
        self.assertRegex(commands, r"`terminal_check`[^\n]*every 60 s")
        self.assertRegex(commands, r"`terminal_done`")
        self.assertNotRegex(commands, r"(?i)to wait again|wait for it again|wait for it with `command` null")
        self.assertNotRegex(text, r"(?i)run tests\)")  # no hint at another way to run commands
        self.assertLess(len(text.splitlines()), 70)

    def test_to_worker_skill_bridge_and_result_carry_the_manager_rule(self):
        text = body("to-worker")
        self.assertRegex(text, r"(?i)the worker does the work you delegate: do not do it yourself")
        self.assertRegex(text, r"(?i)wait for the worker's `to_manager` report")
        self.assertIn("manager_rule", section(text, "Results"))
        self.assertRegex(BRIDGE, r"The worker does the delegated task, not you: \"\s*\+\s*\"do not do it yourself")
        self.assertIn("The worker does this Task; do not do it yourself", flow_tasks.MANAGER_RULE)
        self.assertIn("to_manager report", flow_tasks.MANAGER_RULE)
        self.assertGreaterEqual(FLOW_TASKS.count('"manager_rule": MANAGER_RULE'), 3)
        self.assertLess(len(text), 9000)

    def test_bridge_registers_terminal_for_the_worker_only(self):
        self.assertRegex(BRIDGE, r'if \(role === "worker"\) \{\s*pi\.registerTool\(\{\s*name: TERMINAL_TOOL\.name')
        self.assertRegex(BRIDGE, r"Use it for every shell command")


class WorkerSubagentRuleTests(unittest.TestCase):
    """C-D68 (p27-cd68-skill-01): the worker's only subagents are explorer and analyst; agent is always set."""

    def test_to_manager_names_explorer_and_analyst_and_requires_agent(self):
        text = body("to-manager")
        sub = section(text, "Subagents: `task` tool")
        for name in ("explorer", "analyst"):
            self.assertIn(f"`{name}`", sub, name)
        self.assertRegex(sub, r"(?i)always set `agent` to `explorer` or `analyst`")
        self.assertRegex(sub, r"(?i)without `agent` fails")
        self.assertRegex(sub, r"(?i)never send `to_manager` reports and never run terminal commands")
        self.assertIn("`terminal`", section(text, "Running commands: `terminal`"))  # existing guidance intact
        self.assertLess(len(text.splitlines()), 70)

    def test_agent_definitions_match_the_skill(self):
        agents = Path(launcher.default_bridge_extension()).parent.parent / "agents"
        names = sorted(
            re.search(r"^name: (\S+)$", p.read_text(encoding="utf-8"), re.M).group(1)
            for p in agents.glob("*.md")
        )
        self.assertEqual(names, ["analyst", "explorer"])


class ReviewFixSkillTests(unittest.TestCase):
    """p27-cd68-fix-01: subagents are refused the bridge tools; the worker's own command holds an experiment."""

    def test_to_manager_says_subagents_are_refused_the_bridge_tools(self):
        sub = section(body("to-manager"), "Subagents: `task` tool")
        self.assertRegex(sub, r"(?i)`to_manager`[^\n]*`terminal`[^\n]*`subagent_not_allowed`")
        self.assertIn("subagent_not_allowed", BRIDGE)

    def test_to_worker_says_wait_when_the_workers_own_command_holds_the_host_terminal(self):
        text = body("to-worker")
        line = next(l for l in text.splitlines() if "held:host_terminal_busy:worker_terminal_command" in l)
        rest = line.split("held:host_terminal_busy:worker_terminal_command", 1)[1]
        self.assertRegex(rest, r"(?i)^[^.]*the worker's own `terminal` command[^.]*\. wait")
        self.assertRegex(rest, r"(?i)do not ask the user to take over")


class SmokeWordingSkillTests(unittest.TestCase):
    """p27-cd68-fix-03 (smoke-01 M2/M3)."""

    def test_running_means_end_the_turn_without_progress_reports(self):
        commands = section(body("to-manager"), "Running commands: `terminal`")
        line = next(l for l in commands.splitlines() if l.startswith("- `running`"))
        self.assertRegex(line, r"End your turn now")
        self.assertRegex(line, r"(?i)do not send progress reports[^.]*unless the user or the manager asks or a check "
                               r"shows a problem")

    def test_a_queued_report_is_not_resent(self):
        text = body("to-manager")
        self.assertRegex(text, r"(?i)`queued`[^\n]*accepted[^\n]*never send the same report again")


if __name__ == "__main__":
    unittest.main()
