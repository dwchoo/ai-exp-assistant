"""Independent adversarial tests for C-D69 (2)-(4) (p27-cd69-test-01).

Expectations come from DECISIONS.md C-D69, not from the implementation:
- (2) the manager hands the worker an executable procedure (commands/steps, pre-allowed fallbacks, stop conditions,
  the result to return) and keeps scope widening, interpretation and the user write-up; the worker runs only the
  given procedure and allowed fallbacks, reports failures/contradictions/missing information as facts and stops,
  never widens the scope; its report is sufficient facts plus summary-level analysis; detailed analysis only when the
  manager explicitly asks through ``to_worker``'s analysis level (``analysis``: null = summary, ``detailed``; it applies
  to kind work, so an experiment carrying one is rejected) and the Task message the worker sees shows it;
- (3) the worker's Workbench subagent ``analyst`` is gone, ``explorer`` stays (an older Workbench home's stale
  ``analyst.md`` is removed; the user's own files are not touched); isolation still holds;
- (4) no report length cap in the instructions.
No OMP, no provider, no credential: temp dirs under /tmp only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from test_task_flow import FlowFixture, wait_until  # noqa: E402
from workbench.backend import flow, launcher, omp_home  # noqa: E402
from workbench.contracts.v1 import MessageKind  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "omp_bridge" / "skills"
AGENTS = REPO / "omp_bridge" / "agents"
WORK = {"kind": "work", "message": "collect CPU/RAM with lscpu and free -h; report the values",
        "spec": {"goal": "hardware facts", "paths": []}}
EXPERIMENT_SPEC = {"goal": "g", "paths": ["out.txt"], "execution": {
    "source": "/tmp/src", "commit": "0" * 40, "command": "true",
    "criteria": {"log_contains": "x", "result_file": "out.txt", "result_contains": "x"},
    "environment": [], "shell": "bash"}}
LENGTH_CAP = re.compile(r"(at most|no more than|up to|maximum of|max\.?|under|fewer than|<=?)\s*\d+\s*"
                        r"(words?|lines?|characters?|chars|sentences?|paragraphs?|tokens?|bullets?)", re.I)


def errors(args):
    return flow.validate_arguments("to_worker", args)


def skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text()


# ------------------------------------------------------------------------------------------- (2) the analysis field
class AnalysisValidationTests(unittest.TestCase):
    def test_work_accepts_null_omitted_summary_and_detailed(self):
        self.assertEqual(errors(WORK), [])
        for value in (None, "summary", "detailed"):
            with self.subTest(value=value):
                self.assertEqual(errors({**WORK, "analysis": value}), [])

    def test_invalid_levels_are_rejected_naming_the_field(self):
        for value in ("deep", "full", "detail", "summary ", "detailed\n", 1, True, ["detailed"], {"level": "detailed"}):
            with self.subTest(value=value):
                found = errors({**WORK, "analysis": value})
                self.assertTrue(found, f"{value!r} accepted")
                self.assertTrue(all(e.startswith("analysis") for e in found), found)
        found = errors({**WORK, "analysis": "zz-p27cd69-sentinel"})
        self.assertNotIn("zz-p27cd69-sentinel", " ".join(found), "the error names the rule, not the value sent")

    def test_an_experiment_with_an_analysis_level_is_rejected_and_null_is_fine(self):
        base = {"kind": "experiment", "message": "run", "spec": EXPERIMENT_SPEC}
        self.assertFalse([e for e in errors({**base, "analysis": None}) if e.startswith("analysis")])
        for value in ("summary", "detailed"):
            with self.subTest(value=value):
                self.assertTrue([e for e in errors({**base, "analysis": value}) if e.startswith("analysis")])
        # a follow-up/re-run of an experiment (task_id, no spec) is an experiment too
        follow = {"kind": "experiment", "message": "again", "task_id": "0b9c7c4e-6c1e-4a51-9b1a-3f7c2d1e0a11",
                  "run": True, "analysis": "detailed"}
        self.assertTrue([e for e in errors(follow) if e.startswith("analysis")])


def cancel_settled(test, task_id):
    """Cancel the Task and wait until its cancel notice to the worker exists (p27-cd70-test-02).

    The cancel notice (a QUESTION with ``cancel: true``) is created asynchronously by the outbox; clearing
    ``mailbox.created`` before it appears lets the next subtest see it as its first message. The TASK is first
    waited for until it counts as sent, so the cancel always has a notice to wait for."""
    test.assertTrue(wait_until(lambda: test.flow._task_message_state.get(task_id) == "sent", 10),
                    "the TASK was never sent before the cancel")
    test.to_worker({"kind": "work", "message": "cancel", "task_id": task_id, "cancel": True})
    test.assertTrue(wait_until(lambda: test.flow.active_task() is None, 10))
    test.assertTrue(wait_until(lambda: any(m.kind is MessageKind.QUESTION and m.task_id == task_id
                                           and m.payload.get("cancel") is True for m in test.mailbox.created), 10),
                    "the cancel notice to the worker was never created")


class AnalysisTaskMessageTests(FlowFixture):
    def dispatch(self, **extra):
        result = self.to_worker({**WORK, **extra})
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) >= 1))
        message = self.mailbox.created[0]
        self.assertIs(message.kind, MessageKind.TASK)
        return result, message.payload

    def test_default_is_summary_and_the_task_message_carries_it_and_its_rule(self):
        for extra in ({}, {"analysis": None}):
            with self.subTest(extra=extra):
                self.mailbox.created.clear()
                result, payload = self.dispatch(**extra)
                self.assertEqual(payload.get("analysis"), "summary", payload)
                shown = json.dumps(payload)  # the bridge hands the payload to the worker as JSON text
                self.assertRegex(shown, re.compile(r"summary", re.I))
                rule = payload.get("analysis_rule", "")
                self.assertRegex(rule, re.compile(r"fact", re.I), "summary means facts plus a short summary")
                self.assertNotRegex(rule, LENGTH_CAP, "C-D69 (4): no length cap")
                cancel_settled(self, result["task_id"])

    def test_detailed_is_explicit_and_shown(self):
        _, payload = self.dispatch(analysis="detailed")
        self.assertEqual(payload.get("analysis"), "detailed", payload)
        self.assertRegex(payload.get("analysis_rule", ""), re.compile(r"detail", re.I))
        self.assertNotEqual(payload.get("analysis_rule"), flow.ANALYSIS_RULES["summary"])

    def test_explicit_level_on_a_follow_up_is_honoured_both_ways(self):
        result, payload = self.dispatch()
        self.assertEqual(payload["analysis"], "summary")
        self.assertTrue(wait_until(lambda: (self.flow.task_view() or {}).get("status") == "running"))
        self.to_worker({"kind": "work", "message": "now analyse the swap use in detail", "task_id": result["task_id"],
                        "analysis": "detailed"})
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 2))
        self.assertEqual(self.mailbox.created[1].payload.get("analysis"), "detailed")
        self.to_worker({"kind": "work", "message": "just the numbers again", "task_id": result["task_id"],
                        "analysis": "summary"})
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 3))
        self.assertEqual(self.mailbox.created[2].payload.get("analysis"), "summary")

    def test_invalid_or_experiment_levels_are_rejected_and_nothing_reaches_the_worker(self):
        for args in ({**WORK, "analysis": "deep"}, {**WORK, "analysis": 2},
                     {"kind": "experiment", "message": "run", "spec": EXPERIMENT_SPEC, "analysis": "summary"}):
            with self.subTest(args=args.get("analysis")):
                result = self.to_worker(args)
                self.assertEqual((result["status"], result.get("reason")), ("rejected", "invalid_arguments"), result)
                self.assertTrue([e for e in result.get("errors", []) if e.startswith("analysis")], result)
        self.assertEqual(self.mailbox.created, [])
        self.assertIsNone(self.flow.active_task())


# ---------------------------------------------------------------------------------------- (2)/(4) the skill texts
class SkillTextTests(unittest.TestCase):
    def test_worker_skill_states_the_executor_rules(self):
        text = skill("to-manager")
        flat = " ".join(text.split())
        self.assertRegex(flat, re.compile(r"\bonly\b[^.]*\b(steps|procedure)\b[^.]*\bfallbacks?\b", re.I),
                         "run only the given steps and the allowed fallbacks")
        for word in (r"fail", r"contradict", r"missing", r"\bstop"):
            self.assertRegex(flat, re.compile(word, re.I), word)
        self.assertRegex(flat, re.compile(r"(never|do not|don't)\s+widen", re.I), "never widen the scope")
        self.assertRegex(flat, re.compile(r"summary", re.I))
        self.assertRegex(flat, re.compile(r"detail[^.]*only when|only when[^.]*detail", re.I),
                         "detailed analysis only on explicit request")
        self.assertRegex(flat, re.compile(r"fact", re.I))

    def test_manager_skill_delegates_a_procedure_and_keeps_judgement(self):
        flat = " ".join(skill("to-worker").split())
        for needle in (r"command", r"fallback", r"stop", r"result", r"procedure"):
            self.assertRegex(flat, re.compile(needle, re.I), needle)
        self.assertRegex(flat, re.compile(r"interpret", re.I), "interpretation stays with the manager")
        self.assertRegex(flat, re.compile(r"widen|expand", re.I), "scope widening stays with the manager")
        self.assertRegex(flat, re.compile(r"`?analysis`?[^.]*null[^.]*summary", re.I))
        self.assertRegex(flat, re.compile(r"`?detailed`?[^.]*only when", re.I))
        self.assertRegex(flat, re.compile(r"experiment", re.I))

    def test_no_report_length_cap_and_no_analyst_in_either_skill(self):
        for name in ("to-manager", "to-worker"):
            with self.subTest(skill=name):
                text = skill(name)
                self.assertNotRegex(text, LENGTH_CAP, "C-D69 (4): no length cap")
                self.assertNotRegex(text, re.compile(r"\banalyst\b", re.I), "C-D69 (3)")
        self.assertRegex(" ".join(skill("to-manager").split()), re.compile(r"no length limit|length is not limited|"
                                                                          r"no limit on (the )?length", re.I))

    def test_the_to_worker_tool_text_in_the_bridge_states_the_boundary(self):
        source = (REPO / "omp_bridge" / "g3" / "bridge.ts").read_text()
        self.assertNotRegex(source, re.compile(r"\banalyst\b"), "C-D69 (3)")
        self.assertRegex(source, re.compile(r"analysis:\s*\{\s*type:\s*\[\"string\",\s*\"null\"\]"))


# ------------------------------------------------------------------------------------------------- (3) agents
class AgentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p27cd69-role-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_the_source_has_only_the_explorer(self):
        names = sorted(p.name for p in AGENTS.iterdir())
        self.assertEqual(names, ["explorer.md"])
        head = (AGENTS / "explorer.md").read_text().split("---")[1]
        self.assertRegex(head, r"name:\s*explorer")
        self.assertRegex(head, r'"@smol"')
        self.assertNotRegex(head, r"\b(bash|eval|terminal|edit|write|task)\b")

    def test_a_stale_analyst_in_an_existing_home_is_removed_and_the_users_files_stay(self):
        agent_dir = self.root / "agent"
        target = agent_dir / "agents"
        target.mkdir(parents=True)
        (target / "analyst.md").write_text("---\nname: analyst\ndescription: old\n---\n")
        (target / "mine.md").write_text("---\nname: mine\ndescription: the user's own\n---\n")
        (target / "notes.txt").write_text("keep")
        installed = omp_home.install_agents(agent_dir, AGENTS)
        self.assertEqual(tuple(installed), ("explorer.md",))
        self.assertFalse((target / "analyst.md").exists())
        self.assertTrue((target / "explorer.md").is_file())
        self.assertEqual((target / "explorer.md").read_bytes(), (AGENTS / "explorer.md").read_bytes())
        self.assertEqual((target / "notes.txt").read_text(), "keep")
        self.assertTrue((target / "mine.md").exists())
        omp_home.install_agents(agent_dir, AGENTS)  # idempotent, no analyst coming back
        self.assertFalse((target / "analyst.md").exists())

    def test_a_symlinked_stale_analyst_is_unlinked_without_touching_its_target(self):
        agent_dir = self.root / "agent"
        target = agent_dir / "agents"
        target.mkdir(parents=True)
        outside = self.root / "outside-analyst.md"
        outside.write_text("user file")
        os.symlink(outside, target / "analyst.md")
        omp_home.install_agents(agent_dir, AGENTS)
        self.assertFalse(os.path.lexists(target / "analyst.md"))
        self.assertEqual(outside.read_text(), "user file")

    def test_the_backend_home_preparation_removes_the_stale_analyst(self):
        home = self.root / "home"
        home.mkdir()
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        prepared = omp_home.prepare_omp_home(home / "data", env, skills_dir=launcher.default_skills_dir(),
                                             provider_ids=launcher.ISOLATION_PROVIDER_IDS)
        stale = prepared.agent_dir / "agents" / "analyst.md"
        stale.write_text("---\nname: analyst\ndescription: old\n---\n")
        again = omp_home.prepare_omp_home(home / "data", env, skills_dir=launcher.default_skills_dir(),
                                          provider_ids=launcher.ISOLATION_PROVIDER_IDS)
        self.assertFalse(stale.exists())
        self.assertTrue((again.agent_dir / "agents" / "explorer.md").is_file())

    def test_isolation_per_role_without_analyst(self):
        project, home = self.root / "p", self.root / "h"
        worker = launcher.role_overlay("worker", project_dir=project, home=home)
        manager = launcher.role_overlay("manager", project_dir=project, home=home)
        self.assertNotIn("explorer", worker["task"]["disabledAgents"], "the worker keeps its explorer")
        self.assertIn("explorer", manager["task"]["disabledAgents"], "the explorer is not the manager's")
        for bundled in ("scout", "reviewer", "security-reviewer", "task", "sonic"):
            self.assertIn(bundled, worker["task"]["disabledAgents"])
            self.assertNotIn(bundled, manager["task"]["disabledAgents"])
        for overlay in (worker, manager):
            self.assertNotIn("analyst", json.dumps(overlay))
        observed = {"tools": ["read", "task", "terminal", "to_manager"], "task_agents": ["explorer"]}
        self.assertEqual(launcher.role_expectation_warnings("worker", observed), [])
        self.assertEqual(launcher.role_expectation_warnings("worker", {**observed, "task_agents": []}),
                         ["task_agent:missing:explorer"])
        base = {"system_prompt_default": True, "context_files": [], "skills": [], "skill_commands": [], "rules": [],
                "mcp_tools": [], "other_commands": [], "tools": ["read", "task"], "autoqa": False, "browser": False}
        self.assertEqual(launcher.isolation_leaks({**base, "task_agents": ["explorer"]}, allowed_skills=(),
                                                  role="worker"), [])
        self.assertEqual(launcher.isolation_leaks({**base, "task_agents": ["explorer", "analyst"]}, allowed_skills=(),
                                                  role="worker"), ["task_agent:analyst"])


if __name__ == "__main__":
    unittest.main()
