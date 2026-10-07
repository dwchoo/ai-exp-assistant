"""C-D70 (5) independent checks of the agent-only recovery skill (p27-cd70-test-01).

From DECISIONS.md C-D70 (5) and the assignment p27-cd70-01 (7): a manager skill the user does not use, telling the
manager, on a Workbench notice, to read the status first and then decide (follow-up, cancel, restart_worker); it
never re-runs the worker's commands. Installed for the manager only through the role skill allow-list. Files only;
no OMP, no provider.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from workbench.backend import launcher

SKILLS = Path(__file__).resolve().parents[2] / "omp_bridge" / "skills"
NAME = "workbench-recovery"


def front_matter(text: str) -> dict[str, str]:
    head = text.split("---", 2)
    fields = {}
    for line in head[1].strip().splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


class RecoverySkillTests(unittest.TestCase):
    def setUp(self):
        self.text = (SKILLS / NAME / "SKILL.md").read_text(encoding="utf-8")
        self.front = front_matter(self.text)
        self.tmp = tempfile.TemporaryDirectory(prefix="p27cd70-skill-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)

    def test_front_matter_names_the_directory_and_says_it_is_for_the_agent(self):
        self.assertTrue(self.text.startswith("---\n"))
        self.assertEqual(self.front.get("name"), NAME)
        description = self.front.get("description", "")
        self.assertTrue(description)
        self.assertRegex(description, r"(?i)agent")
        self.assertRegex(description, r"(?i)not for the user|agent-only")

    def test_the_skill_is_english_and_concise(self):
        self.assertFalse(any("가" <= ch <= "힣" for ch in self.text), "skills are English")
        self.assertLess(len(self.text), 8000, "concise")

    def test_it_covers_every_notice_and_reads_the_status_before_deciding(self):
        for notice in ("worker_stalled", "worker_restarted", "report_delivery_unknown", "manager_recovery"):
            self.assertIn(notice, self.text)
        body = self.text.split("---", 2)[2]
        status_at = body.find("workbench_status")
        decide_at = min(i for i in (body.find("restart_worker", status_at), body.find("cancel", status_at)) if i >= 0)
        self.assertGreaterEqual(status_at, 0)
        self.assertLess(status_at, decide_at, "status first, then the decision")
        for choice in ("to_worker", "task_id", "cancel", "restart_worker", "reason"):
            self.assertIn(choice, body)
        self.assertRegex(body, r"(?i)never (re-?)?run the worker's commands")
        self.assertRegex(body, r"(?i)tell the user")

    def test_installed_for_the_manager_only(self):
        home, project = Path(self.tmp.name) / "h", Path(self.tmp.name) / "p"
        home.mkdir()
        project.mkdir()
        manager = launcher.role_overlay("manager", project_dir=project, home=home)["skills"]["includeSkills"]
        worker = launcher.role_overlay("worker", project_dir=project, home=home)["skills"]["includeSkills"]
        self.assertIn(NAME, manager)
        self.assertNotIn(NAME, worker)
        self.assertIn(NAME, launcher.role_skill_allowlist("manager"))
        self.assertNotIn(NAME, launcher.role_skill_allowlist("worker"))
        self.assertIn(NAME, launcher.workbench_skill_names(SKILLS))

    def test_the_manager_handoff_skill_points_to_it(self):
        to_worker = (SKILLS / "to-worker" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(NAME, to_worker)
        to_manager = (SKILLS / "to-manager" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("restart_worker", to_manager.replace("restart_worker:", ""),
                         "the worker skill does not offer the manager's restart tool")


if __name__ == "__main__":
    unittest.main()
