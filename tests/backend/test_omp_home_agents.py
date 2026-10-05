"""C-D68: the Workbench-owned subagent definitions are installed into the Workbench OMP home only."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest

from workbench.backend import launcher, omp_home


class InstallAgentsTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cd68-home-")
        self.addCleanup(self._dir.cleanup)
        self.home = Path(self._dir.name) / "home"
        self.home.mkdir()
        self.data = self.home / "data"
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        self.skills = self.home / "skills"
        self.skills.mkdir()

    def prepare(self, **kwargs):
        return omp_home.prepare_omp_home(self.data, self.env, skills_dir=self.skills,
                                         provider_ids=launcher.ISOLATION_PROVIDER_IDS, **kwargs)

    def test_prepare_installs_the_repo_definitions_private_into_the_workbench_agent_dir(self):
        home = self.prepare()
        installed = home.agent_dir / "agents"
        self.assertEqual(sorted(path.name for path in installed.iterdir()), ["analyst.md", "explorer.md"])
        for source in launcher.default_agents_dir().glob("*.md"):
            target = installed / source.name
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(installed).st_mode), 0o700)

    def test_nothing_is_written_under_the_users_omp_dir(self):
        self.prepare()
        self.assertFalse((self.home / ".omp").exists())

    def test_install_is_idempotent_and_repairs_an_edited_copy(self):
        home = self.prepare()
        target = home.agent_dir / "agents" / "explorer.md"
        target.write_text("tampered\n")
        self.prepare()
        self.assertEqual(target.read_bytes(), (launcher.default_agents_dir() / "explorer.md").read_bytes())

    def test_a_custom_agents_dir_and_an_unreadable_one(self):
        source = self.home / "src-agents"
        source.mkdir()
        (source / "one.md").write_text("---\nname: one\ndescription: x\n---\n")
        (source / "ignored.txt").write_text("x")
        home = self.prepare(agents_dir=source)
        self.assertIn("one.md", {path.name for path in (home.agent_dir / "agents").iterdir()})
        with self.assertRaises(omp_home.OmpHomeError):
            self.prepare(agents_dir=self.home / "missing")

    def test_installed_definitions_are_not_reported_as_ambient_but_foreign_ones_are(self):
        home = self.prepare()
        (home.agent_dir / "agents" / "foreign.md").write_text("---\nname: foreign-agent\ndescription: x\n---\n")
        project = self.home / "project"
        project.mkdir()
        env = {"PI_CONFIG_DIR": home.config_dir}
        names = launcher.task_agent_names(project, self.home, env, tuple(launcher.WORKBENCH_AGENT_ROLES))
        self.assertEqual(names, ("foreign-agent",))
        manager = launcher.role_overlay("manager", project_dir=project, home=self.home, environment=env)
        worker = launcher.role_overlay("worker", project_dir=project, home=self.home, environment=env)
        self.assertEqual(sorted(manager["task"]["disabledAgents"]), ["analyst", "explorer", "foreign-agent"])
        self.assertEqual(set(worker["task"]["disabledAgents"]), launcher.BUNDLED_TASK_AGENTS | {"foreign-agent"})


if __name__ == "__main__":
    unittest.main()
