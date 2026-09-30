"""Data-dir precedence/permissions, single-instance lock and start-requirement checks."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from workbench.backend import launcher
from workbench.backend.cli import parser
from workbench.backend.launcher import LaunchPlan, StartRequirementError
from workbench.backend.paths import (
    BackendLocked, DataDirError, DataLayout, InstanceLock, ensure_private_dir, resolve_data_dir,
)
from workbench.terminal.shell_g2.prototype import ShellChoice


class DataDirTests(unittest.TestCase):
    def test_precedence_cli_then_env_then_xdg_then_home(self):
        env = {"WORKBENCH_DATA_DIR": "/e/wb", "XDG_STATE_HOME": "/x", "HOME": "/h"}
        self.assertEqual(resolve_data_dir("/c/wb", env), Path("/c/wb"))
        self.assertEqual(resolve_data_dir(None, env), Path("/e/wb"))
        self.assertEqual(resolve_data_dir(None, {"XDG_STATE_HOME": "/x", "HOME": "/h"}), Path("/x/omp-workbench"))
        self.assertEqual(resolve_data_dir(None, {"XDG_STATE_HOME": "rel", "HOME": "/h"}),
                         Path("/h/.local/state/omp-workbench"))
        self.assertEqual(resolve_data_dir(None, {"HOME": "/h"}), Path("/h/.local/state/omp-workbench"))
        self.assertTrue(resolve_data_dir("relative", {}).is_absolute())

    def test_private_dir_is_created_or_tightened_and_symlinks_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = ensure_private_dir(root / "a" / "data")
            self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o700)
            loose = root / "loose"
            loose.mkdir(mode=0o755)
            os.chmod(loose, 0o755)
            ensure_private_dir(loose)
            self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o700)
            (root / "link").symlink_to(loose)
            with self.assertRaises(DataDirError):
                ensure_private_dir(root / "link")
            (root / "file").write_text("x")
            with self.assertRaises(DataDirError):
                ensure_private_dir(root / "file")

    def test_socket_path_length_is_checked(self):
        with self.assertRaises(DataDirError):
            DataLayout(Path("/tmp/" + "d" * 110)).check_socket_paths()
        DataLayout(Path("/tmp/short")).check_socket_paths()

    def test_instance_lock_is_exclusive_across_processes_and_not_inherited(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backend.lock"
            first = InstanceLock(path)
            first.acquire()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(BackendLocked):
                InstanceLock(path).acquire()
            self.assertTrue(InstanceLock(path).held_elsewhere())
            code = ("import sys; sys.path.insert(0, 'src');"
                    "from pathlib import Path; from workbench.backend.paths import InstanceLock, BackendLocked\n"
                    f"try:\n    InstanceLock(Path({str(path)!r})).acquire()\nexcept BackendLocked:\n    raise SystemExit(7)")
            child = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
                                   timeout=30, close_fds=False)
            self.assertEqual(child.returncode, 7)
            first.release()
            self.assertFalse(InstanceLock(path).held_elsewhere())
            second = InstanceLock(path)
            second.acquire()
            second.release()


class StartRequirementTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw17-path-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def bindir(self, name, **links):
        path = self.root / name
        path.mkdir()
        for link, target in links.items():
            (path / link).symlink_to(target)
        return str(path)

    def test_bash_first_then_sh_and_login_shell_is_ignored(self):
        both = self.bindir("both", bash="/usr/bin/bash", sh="/usr/bin/dash")
        only_sh = self.bindir("sh", sh="/usr/bin/dash")
        zsh_default = {"PATH": both, "SHELL": "/usr/bin/zsh"}
        self.assertEqual(launcher.choose_shell(zsh_default), ShellChoice("bash", "/usr/bin/bash"))
        self.assertEqual(launcher.choose_shell({"PATH": only_sh, "SHELL": "/usr/bin/zsh"}),
                         ShellChoice("sh", "/usr/bin/dash"))

    def test_missing_bash_and_sh_gives_guidance(self):
        empty = self.bindir("empty")
        with self.assertRaises(StartRequirementError) as caught:
            launcher.choose_shell({"PATH": empty, "SHELL": "/usr/bin/zsh"})
        text = str(caught.exception)
        self.assertIn("Bash or a POSIX sh", text)
        self.assertIn("No backend was started", text)
        with self.assertRaises(StartRequirementError):
            launcher.build_plan({"PATH": empty})

    def test_missing_omp_or_extension_is_reported_before_start(self):
        shells = self.bindir("shells", bash="/usr/bin/bash")
        with self.assertRaises(StartRequirementError) as caught:
            launcher.build_plan({"PATH": shells})
        self.assertIn("'omp'", str(caught.exception))
        fake_omp = self.root / "omp"
        fake_omp.write_text("#!/bin/sh\necho omp/0.0-test\n")
        fake_omp.chmod(0o700)
        with self.assertRaises(StartRequirementError):
            launcher.build_plan({"PATH": shells}, omp=str(fake_omp), bridge_extension=str(self.root / "none.ts"))
        plan = launcher.build_plan({"PATH": shells, "WORKBENCH_OMP_ARGS": "--no-session --model 'a b'"},
                                   omp=str(fake_omp))
        self.assertEqual(plan.omp_version, "omp/0.0-test")
        self.assertEqual(plan.omp_args, ("--no-session", "--model", "a b"))
        self.assertTrue(Path(plan.bridge_extension).is_file())

    def test_omp_and_shell_environments_inject_only_bridge_identity(self):
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.2.10", "/x/bridge.ts",
                          ("--no-session",))
        base = {"PATH": "/usr/bin", "HOME": "/h", "WORKBENCH_G3_TOKEN": "leaked", "OPENAI_API_KEY": "user-own"}
        env = launcher.omp_environment(base, plan, role="worker", token="t0k", bridge_socket=Path("/d/bridge.sock"))
        self.assertEqual((env["WORKBENCH_G3_ROLE"], env["WORKBENCH_G3_TOKEN"], env["WORKBENCH_G3_GENERATION"]),
                         ("worker", "t0k", "1"))
        self.assertEqual(env["WORKBENCH_G3_BRIDGE_SOCKET"], "/d/bridge.sock")
        self.assertEqual(env["OPENAI_API_KEY"], "user-own")  # passed through, never stored
        self.assertEqual(launcher.omp_command(plan, "/d/omp-isolation-worker.yml"),
                         ["/x/omp", "--config", str(launcher.default_isolation_overlay()),
                          "--config", "/d/omp-isolation-worker.yml", "--no-extensions",
                          "--append-system-prompt", "", "--no-title", "--no-session",
                          "--extension", "/x/bridge.ts"])
        shell_env = launcher.shell_environment(base)
        self.assertNotIn("WORKBENCH_G3_TOKEN", shell_env)
        self.assertEqual(shell_env["TERM"], "xterm-256color")
        parsed = parser().parse_args(["_backend", "--data-dir", "/d", "--project-dir", "/p", *plan.to_argv()])
        self.assertEqual((parsed.shell_kind, parsed.shell_path, parsed.omp_arg), ("bash", "/usr/bin/bash",
                                                                                   ["--no-session"]))


class IsolationLaunchTests(unittest.TestCase):
    """C-D59: both OMP processes run with a per-run isolation overlay, never --profile."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw17-iso-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def plan(self, *omp_args):
        return LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.4", "/x/bridge.ts",
                          tuple(omp_args))

    def test_argv_isolation_first_then_user_args_then_bridge(self):
        overlay = self.root / "omp-isolation-manager.yml"
        argv = launcher.omp_command(self.plan("--model", "m", "--config", "/u/mine.yml"), overlay)
        static = str(launcher.default_isolation_overlay())
        self.assertEqual(argv[0], "/x/omp")
        # R1: an explicit (empty) --append-system-prompt keeps OMP from reading APPEND_SYSTEM.md
        # user choice: --no-title (no session-title model call; TITLE_SYSTEM.md unused)
        self.assertEqual(argv[1:9], ["--config", static, "--config", str(overlay), "--no-extensions",
                                     "--append-system-prompt", "", "--no-title"])
        # user --omp-arg / WORKBENCH_OMP_ARGS come after isolation and may override it
        self.assertEqual(argv[9:13], ["--model", "m", "--config", "/u/mine.yml"])
        self.assertEqual(argv[-2:], ["--extension", "/x/bridge.ts"])
        for banned in ("--profile", "--no-skills", "--no-context-files"):
            self.assertNotIn(banned, argv)
        self.assertFalse(any(item.startswith("--profile") for item in argv))
        # CW-18 supplies the role prompt through the same slot
        role = launcher.omp_command(self.plan(), overlay, "ROLE PROMPT")
        self.assertEqual(role[6:8], ["--append-system-prompt", "ROLE PROMPT"])
        self.assertEqual(role.count("--append-system-prompt"), 1)

    def test_static_overlay_file_matches_investigated_settings(self):
        path = launcher.default_isolation_overlay()
        self.assertTrue(path.is_file())
        text = path.read_text()
        line = next(item for item in text.splitlines() if item.startswith("disabledProviders:"))
        listed = [part.strip() for part in line.split("[", 1)[1].rstrip("]").split(",")]
        self.assertEqual(listed, list(launcher.ISOLATION_PROVIDER_IDS))
        for key in ("enableClaudeProject", "enableClaudeUser", "enableCodexUser", "enablePiUser",
                    "enablePiProject", "enableAgentsUser", "enableAgentsProject"):
            self.assertIn(f"  {key}: false", text)
        body = [item.split("#", 1)[0].rstrip() for item in text.splitlines()]
        self.assertFalse(any("customDirectories" in item for item in body))  # added per role, absolute
        self.assertIn("  enableProjectConfig: false", body)
        self.assertIn('  backend: "off"', body)
        self.assertIn("  builtinRules: false", body)
        # user choice: OMP's default personality stays (PERSONALITY.md is only warned about)
        self.assertFalse(any(item.startswith("personality:") for item in body))
        # Auto QA off (no consent modal, no report push)
        dev_block = [item for item in body[body.index("dev:") + 1:] if item.strip()]
        self.assertEqual(dev_block[0], "  autoqa: false")
        # skills.enabled=false / --no-skills would also drop Workbench custom skills
        skills_block = body[body.index("skills:") + 1:]
        skills_block = skills_block[:next(i for i, item in enumerate(skills_block) if item and not item.startswith(" "))]
        self.assertNotIn("  enabled: false", skills_block)
        self.assertTrue((launcher.default_skills_dir() / "README.md").is_file())

    def write_agent(self, directory, filename, text):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(text)

    def test_task_agent_names_follow_omp_discovery(self):
        home = self.root / "home"
        outer = self.root / "outer"
        project = outer / "repo" / "sub"
        project.mkdir(parents=True)
        agents = project / ".omp" / "agents"
        self.write_agent(agents, "canary.md",
                         "---\nname: canary-agent  # comment\ndescription: x\n---\nbody name: not-this\n")
        self.write_agent(agents, "quoted.md", "---\nname: \"q-agent\"\ndescription: |\n  long\n  text\n---\n")
        self.write_agent(agents, "plain.md", "no frontmatter\n")  # OMP drops it: no name
        self.write_agent(agents, "nodesc.md", "---\nname: scout\n---\n")  # dropped: no description
        self.write_agent(agents, "reserved.md", "---\nname: Main\ndescription: d\n---\n")  # reserved name
        self.write_agent(agents, "nested.md", "---\nname:\n  x: 1\ndescription: d\n---\n")  # not a string
        self.write_agent(agents, "notes.txt", "---\nname: ignored\ndescription: d\n---\n")
        # OMP reads only the nearest ancestor .omp/agents
        self.write_agent(outer / ".omp" / "agents", "far.md", "---\nname: reviewer\ndescription: d\n---\n")
        self.write_agent(home / ".omp" / "agent" / "agents", "mine.md", "---\ndescription: d\nname: user-agent\n---\n")
        names = launcher.task_agent_names(project, home)
        self.assertEqual(names, ("canary-agent", "q-agent", "user-agent"))
        # a bundled name is disabled only when a loadable ambient definition replaces it
        self.write_agent(agents, "override.md", "---\nname: scout\ndescription: mine\n---\n")
        self.assertIn("scout", launcher.task_agent_names(project, home))
        self.assertNotIn("reviewer", launcher.task_agent_names(project, home))
        # without a nearer dir the ancestor one is the nearest
        self.assertEqual(launcher.task_agent_names(outer / "repo", self.root / "nohome"), ("reviewer",))
        self.assertEqual(launcher.task_agent_names(self.root / "missing", self.root / "nohome"), ())
        # PI_CONFIG_DIR moves the user dir like OMP does
        self.write_agent(home / ".alt" / "agent" / "agents", "alt.md", "---\nname: alt-agent\ndescription: d\n---\n")
        self.assertIn("alt-agent", launcher.task_agent_names(self.root / "missing", home, {"PI_CONFIG_DIR": ".alt"}))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_agent_name_read_never_blocks_on_a_fifo(self):
        agents = self.root / "proj" / ".omp" / "agents"
        agents.mkdir(parents=True)
        os.mkfifo(agents / "pipe.md")
        self.assertIsNone(launcher._agent_definition_name(agents / "pipe.md"))
        self.assertEqual(launcher.task_agent_names(self.root / "proj", self.root / "nohome"), ())

    def test_role_overlay_content_and_private_file(self):
        home = self.root / "home"
        project = self.root / "proj"
        self.write_agent(project / ".omp" / "agents", "canary.md", "---\nname: canary-agent\n---\n")
        self.write_agent(project / ".omp" / "agents", "canary.md", "---\nname: canary-agent\ndescription: d\n---\n")
        overlay = launcher.role_overlay("worker", project_dir=project, home=home,
                                        user_disabled_providers=["openrouter", "claude"],
                                        user_disabled_agents=["sonic", "canary-agent"])
        self.assertEqual(overlay["skills"]["customDirectories"], [str(launcher.default_skills_dir())])
        # R4: Workbench-owned skill filter; user include/ignore lists are replaced, not inherited
        self.assertEqual(overlay["skills"]["includeSkills"], [])  # no role skills yet (CW-18)
        self.assertEqual(overlay["skills"]["ignoredSkills"], [])
        # R4: the user's own disabledAgents survive (union, no duplicates)
        self.assertEqual(overlay["task"]["disabledAgents"], ["sonic", "canary-agent"])
        self.assertEqual(overlay["disabledProviders"][:len(launcher.ISOLATION_PROVIDER_IDS)],
                         list(launcher.ISOLATION_PROVIDER_IDS))
        self.assertIn("openrouter", overlay["disabledProviders"])  # the user's own entry survives
        self.assertEqual(overlay["disabledProviders"].count("claude"), 1)
        filtered = launcher.role_overlay("manager", project_dir=project, home=home,
                                         role_skills={"manager": ("order-manager", "shared-*")})
        self.assertEqual(filtered["skills"]["includeSkills"], ["order-manager", "shared-*"])
        self.assertEqual(filtered["skills"]["ignoredSkills"], [])
        data = self.root / "data"
        data.mkdir(mode=0o700)
        path = launcher.write_role_overlay(data, "worker", overlay)
        self.assertEqual(path, data / "omp-isolation-worker.yml")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        import json
        self.assertEqual(json.loads(path.read_text()), overlay)  # JSON is valid YAML for --config
        with self.assertRaises(ValueError):
            launcher.role_overlay("observer", project_dir=project, home=home)

    def test_allowed_skills_follow_dir_and_role_filter(self):
        skills = self.root / "skills"
        for name in ("order-worker", "order-manager", "shared-x"):
            self.write_agent(skills / name, "SKILL.md", f"---\nname: {name}\ndescription: d\n---\n")
        (skills / "README.md").write_text("not a skill")
        (skills / "empty").mkdir()
        self.assertEqual(launcher.workbench_skill_names(skills), ("order-manager", "order-worker", "shared-x"))
        self.assertEqual(launcher.workbench_skill_names(skills, ("order-worker", "shared-*")),
                         ("order-worker", "shared-x"))
        self.assertEqual(launcher.workbench_skill_names(self.root / "none"), ())
        self.assertEqual(launcher.workbench_skill_names(launcher.default_skills_dir()), ())

    def test_user_disabled_providers_read_via_omp_config_get(self):
        fake = self.root / "omp"
        fake.write_text("#!/bin/sh\n[ \"$1 $2 $3 $4\" = 'config get disabledProviders --json' ] || exit 9\n"
                        "echo '{\"key\": \"disabledProviders\", \"value\": [\"openrouter\"], \"type\": \"array\"}'\n")
        fake.chmod(0o700)
        self.assertEqual(launcher.user_disabled_providers(str(fake), cwd=self.root, environment={"PATH": "/usr/bin"}),
                         ["openrouter"])
        broken = self.root / "broken"
        broken.write_text("#!/bin/sh\nexit 3\n")
        broken.chmod(0o700)
        self.assertIsNone(launcher.user_disabled_providers(str(broken), cwd=self.root, environment={}))

    def test_user_config_reads_share_a_bound_and_stop_their_group(self):
        fake = self.root / "omp"
        record = self.root / "pids"
        fake.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys, time
key = sys.argv[3]
if key == "task.disabledAgents":
    print(json.dumps({{"key": key, "value": ["sonic"], "type": "array"}}), flush=True)
    sys.exit(0)
child = subprocess.Popen(["sleep", "60"])  # same group: must be stopped too
with open({str(record)!r}, "a") as stream:
    stream.write(f"{{os.getpid()}} {{child.pid}}\\n")
time.sleep(60)
""")
        fake.chmod(0o700)
        started = time.monotonic()
        values = launcher.read_user_config(str(fake), cwd=self.root, environment={"PATH": os.environ["PATH"]},
                                           timeout=1.5)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(values, {"disabledProviders": None, "task.disabledAgents": ["sonic"]})
        for pid in record.read_text().split():
            path = Path(f"/proc/{pid}/stat")
            self.assertFalse(path.exists() and path.read_text().rsplit(") ", 1)[1][0] not in "ZX",
                             f"pid {pid} survived the bounded config read")

    def test_omp_environment_drops_auto_qa_override(self):
        env = launcher.omp_environment({"PATH": "/usr/bin", "PI_AUTO_QA": "1"}, self.plan(), role="worker",
                                       token="t", bridge_socket=Path("/d/b.sock"))
        self.assertNotIn("PI_AUTO_QA", env)
        self.assertEqual(launcher.shell_environment({"PI_AUTO_QA": "1"})["PI_AUTO_QA"], "1")

    def test_isolated_environment_never_sets_profile_or_agent_dir(self):
        env = launcher.omp_environment({"PATH": "/usr/bin"}, self.plan(), role="manager", token="t",
                                       bridge_socket=Path("/d/b.sock"))
        for key in ("PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE"):
            self.assertNotIn(key, env)


if __name__ == "__main__":
    unittest.main()
