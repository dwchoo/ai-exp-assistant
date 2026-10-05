"""Independent C-D59 isolation contract tests (p27-cd59-test-01, unit V-CW-17-p2.7-isolation), no real OMP, no model calls.

Expectations come from C-D59 and the Root-specified contract (the investigation table in
result-p27-omp-iso-explore-01-agent.json), written before launcher.py was read:

- both Workbench OMP processes run with a Workbench-owned ``--config`` overlay (static file in the repo + a per-role file
  in the data dir) plus ``--no-extensions``; the explicit bridge ``--extension`` stays; NO ``--profile`` /
  ``PI_CODING_AGENT_DIR`` (that would drop the user's auth); nothing global is edited, nothing credential-like is copied
  into argv/overlays; user ``--omp-arg`` / WORKBENCH_OMP_ARGS still append after the isolation arguments (and may override).
- the overlay turns off every ambient source of the investigation table (context files, skills incl. orca, rules, MCP
  project config, memory, advisor, builtin ttsr rules ...) without touching model/auth settings; ``skills.enabled`` /
  ``--no-skills`` must not be used (they would also block Workbench skills); Workbench-only skills come from
  ``omp_bridge/skills`` via ``skills.customDirectories``; per-role skill filtering; task subagent definitions (not covered by
  disabledProviders) are closed by name via ``task.disabledAgents``, reading names only.
- a start-up check runs the same command in ``--mode rpc --no-session`` (get_state / get_available_commands only, bounded,
  own process group cleaned up by identity) and the backend records the result in the snapshot; a leak or a failed check
  is a visible warning, never silent, and never kills the session; it runs off the start path.

Layers: (1) static overlay + argv/env/overlay-writer contract with pure launcher calls, (2) ``check_isolation`` against a
fake RPC OMP shaped like the real 18.4.4 output, (3) the real ``workbench start/status`` entrypoint with a fake OMP that
records argv/env/config/RPC traffic per invocation. The live real-OMP check is ``live_omp_isolation_independent_p27m.py``.

C-D64 adaptation (p27-home-test-01): the "no ``PI_CODING_AGENT_DIR``" rule above is superseded. Both OMPs now run in a
Workbench-owned OMP home: ``PI_CODING_AGENT_DIR`` MUST be ``<data dir>/omp-root/agent`` and ``PI_CONFIG_DIR`` MUST name
``<data dir>/omp-root`` relative to HOME; profiles stay forbidden. The user's auth reaches OMP through the
``agent.db`` symlink, so the fake HOME of the entrypoint tests carries a fake (non-credential) ``agent.db`` regular file and
``start`` must refuse without one. The user's OMP config (``omp config get``) is no longer read or unioned in.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from workbench.backend import launcher
from workbench.backend.launcher import LaunchPlan
from workbench.terminal.shell_g2.prototype import ShellChoice

REPO = Path(__file__).resolve().parents[2]
STATIC_OVERLAY = REPO / "omp_bridge" / "omp-isolation.yml"
SKILLS_DIR = REPO / "omp_bridge" / "skills"
CAPABILITY_IDS = ("native", "omp-managed", "skillshare", "agents-md", "agents", "claude-md", "claude-plugins", "claude",
                  "cline", "codex", "cursor", "gemini", "github", "mcp-json", "omp-plugins", "opencode", "ssh-json",
                  "vscode", "windsurf", "agent-plugins", "builtin-defaults")
MODEL_PROVIDER_IDS = ("anthropic", "openai", "openai-codex", "github-copilot", "google", "google-gemini-cli",
                      "google-vertex", "amazon-bedrock", "openrouter", "ollama", "xai", "mistral", "groq", "deepseek")
AMBIENT_SKILL_FLAGS = ("enableClaudeProject", "enableClaudeUser", "enableCodexUser", "enablePiUser", "enablePiProject",
                       "enableAgentsUser", "enableAgentsProject")
# C-D59 corrections (p27-cd59-test-02, Root-authorized): ``--append-system-prompt ""`` (blocks the ambient
# APPEND_SYSTEM.md fallback) and ``--no-title`` (no TITLE_SYSTEM.md title call) are REQUIRED isolation args.
FORBIDDEN_ARGS = ("--profile", "--no-skills", "--system-prompt", "--system-prompt-template")
# C-D64: PI_CODING_AGENT_DIR is no longer forbidden; it must be the Workbench agent dir (see HOME_ENV_KEYS).
FORBIDDEN_ENV = ("OMP_PROFILE", "PI_PROFILE", "OMP_CODING_AGENT_DIR")
HOME_ENV_KEYS = ("PI_CONFIG_DIR", "PI_CODING_AGENT_DIR", "BUN_RUNTIME_TRANSPILER_CACHE_PATH")
FAKE_AUTH_BYTES = b"p27u fake auth store - not a credential\n"
# C-D68 (1)-(3): OMP 18.6.1's bundled task agents (manager only) and the Workbench-owned worker agents.
BUNDLED_AGENTS = frozenset({"scout", "reviewer", "security-reviewer", "task", "sonic"})
WORKBENCH_AGENTS = frozenset({"explorer", "analyst"})
CD68_ROLE_DISABLED = {"manager": WORKBENCH_AGENTS, "worker": BUNDLED_AGENTS}


def ambient_disabled(overlay: dict, role: str) -> list[str]:
    """``task.disabledAgents`` without the fixed C-D68 per-role set (which must be there in full)."""
    names = overlay["task"]["disabledAgents"]
    missing = CD68_ROLE_DISABLED[role] - set(names)
    assert not missing, f"C-D68: the {role} overlay does not disable {sorted(missing)}"
    return [name for name in names if name not in CD68_ROLE_DISABLED[role]]


def plant_fake_auth_store(home: Path) -> Path:
    """A regular file where the user's OMP keeps agent.db (any bytes; never a real credential)."""
    store = home / ".omp" / "agent" / "agent.db"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(FAKE_AUTH_BYTES)
    store.chmod(0o600)
    return store


def assert_workbench_home_env(test: unittest.TestCase, env: dict, home: Path, data: Path) -> None:
    """C-D64: the recorded OMP environment names the Workbench home under ``data`` (relative PI_CONFIG_DIR)."""
    root = data / "omp-root"
    test.assertEqual(env.get("PI_CODING_AGENT_DIR"), str(root / "agent"), env)
    value = env.get("PI_CONFIG_DIR")
    test.assertTrue(value and not os.path.isabs(value), f"PI_CONFIG_DIR must be HOME-relative: {value!r}")
    test.assertEqual(os.path.normpath(os.path.join(str(home), value)), str(root), "path.join(HOME, PI_CONFIG_DIR)")
    cache = env.get("BUN_RUNTIME_TRANSPILER_CACHE_PATH")
    test.assertTrue(cache and cache.startswith(str(root) + os.sep), f"bun cache outside the Workbench root: {cache}")


# ---------------------------------------------------------------------------------------- tiny YAML subset reader
def _scalar(text: str):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(item) for item in inner.split(",")] if inner else []
    if text in ("true", "false"):
        return text == "true"
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def parse_yaml_subset(text: str) -> dict:
    """Comments, ``key: scalar|[flow, list]`` and 2-space nested maps: all the static overlay uses."""
    root: dict = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = re.sub(r"\s+#.*$", "", raw) if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        key, _, value = line.strip().partition(":")
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if value.strip():
            parent[key.strip()] = _scalar(value)
        else:
            parent[key.strip()] = child = {}
            stack.append((indent, child))
    return root


def flatten(data, prefix=""):
    if isinstance(data, dict):
        for key, value in data.items():
            yield from flatten(value, f"{prefix}{key}.")
    else:
        yield prefix.rstrip("."), data


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_required_isolation_args(test: unittest.TestCase, argv) -> None:
    """``--append-system-prompt ""`` and ``--no-title`` exactly once each, after ``--no-extensions``."""
    argv = list(argv)
    test.assertEqual(argv.count("--append-system-prompt"), 1, argv)
    at = argv.index("--append-system-prompt")
    test.assertEqual(argv[at + 1], "", f"--append-system-prompt must carry an empty value: {argv}")
    test.assertIn("--no-title", argv)
    test.assertGreater(at, argv.index("--no-extensions"))


def plan_for(omp: str = "/usr/bin/omp-fake", args=()) -> LaunchPlan:
    return LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), omp, "omp/18.4.4", "/repo/omp_bridge/g3/bridge.ts",
                      tuple(args))


# ------------------------------------------------------------------------------- 1. static overlay + argv contract
class StaticOverlayContractTests(unittest.TestCase):
    def setUp(self):
        self.text = STATIC_OVERLAY.read_text()
        self.data = parse_yaml_subset(self.text)

    def test_every_ambient_capability_provider_is_disabled_and_no_model_provider_is(self):
        providers = self.data["disabledProviders"]
        self.assertIsInstance(providers, list)
        self.assertLessEqual(set(CAPABILITY_IDS), set(providers), sorted(set(CAPABILITY_IDS) - set(providers)))
        self.assertEqual([p for p in providers if p in MODEL_PROVIDER_IDS], [], "a model provider would lose its login")
        self.assertEqual(len(providers), len(set(providers)), "duplicate ids")

    def test_ambient_skill_sources_off_without_switching_skills_off(self):
        skills = self.data["skills"]
        for flag in AMBIENT_SKILL_FLAGS:
            self.assertIs(skills.get(flag), False, f"skills.{flag} must be false")
        self.assertNotIn("enabled", skills, "skills.enabled=false would also block the Workbench skills directory")
        self.assertNotIn("customDirectories", skills, "the role overlay owns the (absolute) skills directory")
        self.assertFalse(self.data.get("skills", {}).get("enabled") is False)

    def test_mcp_memory_advisor_ttsr_commands_pinned_off(self):
        self.assertIs(self.data["mcp"]["enableProjectConfig"], False)
        self.assertEqual(self.data["memory"]["backend"], "off")
        self.assertIs(self.data["memories"]["enabled"], False)
        self.assertIs(self.data["advisor"]["enabled"], False)
        self.assertIs(self.data["ttsr"]["builtinRules"], False)
        self.assertIs(self.data["commands"]["enableClaudeProject"], False)
        self.assertIs(self.data["commands"]["enableOpencodeProject"], False)

    def test_overlay_has_no_model_auth_or_profile_settings_and_no_user_paths(self):
        keys = [key for key, _ in flatten(self.data)]
        bad = [key for key in keys if re.search(r"(?i)(api|token|secret|cred|passw|oauth|auth|login|model|profile|agentdir"
                                                r"|provider(?!s$))", key.split(".")[-1]) and key != "disabledProviders"]
        self.assertEqual(bad, [], "the static overlay must not carry model/auth/profile settings")
        self.assertNotIn(str(Path.home()), self.text)
        self.assertNotIn("/home/", self.text)

    def test_workbench_skills_directory_holds_exactly_the_two_role_skills(self):
        # CW-18 (C-D64/C-D65): the only Workbench skills are to-worker (manager) and to-manager (worker).
        self.assertTrue(SKILLS_DIR.is_dir())
        self.assertEqual(launcher.default_skills_dir(), SKILLS_DIR)
        self.assertEqual(launcher.default_isolation_overlay(), STATIC_OVERLAY)
        self.assertEqual(sorted(p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").exists()),
                         ["to-manager", "to-worker"], "exactly the two CW-18 role skills, nothing else")
        self.assertEqual(launcher.workbench_skill_names(SKILLS_DIR), ("to-manager", "to-worker"),
                         "front matter names must equal the directory names")
        self.assertTrue((SKILLS_DIR / "README.md").is_file())

    def test_python_constants_match_the_static_file(self):
        self.assertEqual(list(launcher.ISOLATION_PROVIDER_IDS), self.data["disabledProviders"])


class ArgvEnvContractTests(unittest.TestCase):
    def role_files(self):
        root = Path(tempfile.mkdtemp(prefix="p27m-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def test_command_shape_for_manager_and_worker(self):
        root = self.role_files()
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                overlay = launcher.write_role_overlay(root, role, launcher.role_overlay(
                    role, project_dir=root, home=root))
                command = launcher.omp_command(plan_for(), overlay)
                self.assertEqual(command[0], "/usr/bin/omp-fake")
                configs = [command[i + 1] for i, arg in enumerate(command) if arg == "--config"]
                self.assertEqual(configs, [str(STATIC_OVERLAY), str(overlay)])
                self.assertEqual(command.count("--no-extensions"), 1)
                self.assertEqual(command.count("--extension"), 1)
                self.assertEqual(command[command.index("--extension") + 1], "/repo/omp_bridge/g3/bridge.ts")
                for forbidden in FORBIDDEN_ARGS:
                    self.assertFalse([a for a in command if a == forbidden or a.startswith(forbidden + "=")],
                                     f"{forbidden} in {command}")
                assert_required_isolation_args(self, command)
                self.assertEqual(command.count("--no-title"), 1, command)

    def test_user_arguments_are_appended_after_the_isolation_arguments(self):
        root = self.role_files()
        overlay = launcher.write_role_overlay(root, "worker", launcher.role_overlay("worker", project_dir=root, home=root))
        command = launcher.omp_command(plan_for(args=("--thinking", "high", "--flag-z")), overlay)
        for user in ("--thinking", "high", "--flag-z"):
            self.assertIn(user, command)
        last_isolation = max(command.index("--no-extensions"), max(i for i, a in enumerate(command) if a == "--config") + 1)
        self.assertGreater(command.index("--thinking"), last_isolation)
        self.assertEqual(command[command.index("--thinking"):command.index("--thinking") + 3],
                         ["--thinking", "high", "--flag-z"], "user args reordered")

    def test_environment_never_selects_a_profile_and_passes_auth_env_through_untouched(self):
        base = {"PATH": "/usr/bin", "HOME": "/home/u", "OPENAI_API_KEY": "sk-p27m-sentinel", "LANG": "C.UTF-8",
                "OMP_PROFILE": "p", "PI_PROFILE": "p", "PI_CODING_AGENT_DIR": "/home/u/elsewhere/agent"}
        plan = plan_for()
        home = {"PI_CONFIG_DIR": "wb/omp-root", "PI_CODING_AGENT_DIR": "/home/u/wb/omp-root/agent",
                "BUN_RUNTIME_TRANSPILER_CACHE_PATH": "/home/u/wb/omp-root/bun-transpiler-cache"}
        for role in ("manager", "worker"):
            env = launcher.omp_environment(base, plan, role=role, token="t0k", bridge_socket=Path("/x/b.sock"))
            for key in FORBIDDEN_ENV:
                self.assertNotIn(key, env)
            # C-D64: a user's own PI_CODING_AGENT_DIR is never passed on; only the Workbench home sets one
            self.assertNotIn("PI_CODING_AGENT_DIR", env)
            homed = launcher.omp_environment(base, plan, role=role, token="t0k", bridge_socket=Path("/x/b.sock"),
                                             home=home)
            for key in FORBIDDEN_ENV:
                self.assertNotIn(key, homed)
            self.assertEqual({k: homed[k] for k in HOME_ENV_KEYS}, home)
            for out in (env, homed):
                self.assertEqual(out["OPENAI_API_KEY"], "sk-p27m-sentinel", "the user's own env auth must stay in place")
                self.assertEqual(out["HOME"], "/home/u")
                self.assertEqual(out["WORKBENCH_G3_ROLE"], role)

    def test_no_credential_value_reaches_argv_or_overlays(self):
        root = self.role_files()
        (root / "proj" / ".omp").mkdir(parents=True)
        base = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-p27m-sentinel", "ANTHROPIC_API_KEY": "sk-ant-p27m-sentinel"}
        for role in ("manager", "worker"):
            overlay = launcher.write_role_overlay(root, role, launcher.role_overlay(
                role, project_dir=root / "proj", home=root))
            blob = " ".join(launcher.omp_command(plan_for(), overlay)) + overlay.read_text() + STATIC_OVERLAY.read_text()
            for secret in ("sk-p27m-sentinel", "sk-ant-p27m-sentinel"):
                self.assertNotIn(secret, blob)
        self.assertEqual(base["OPENAI_API_KEY"], "sk-p27m-sentinel")


# --------------------------------------------------------------------------------------- role overlay contract
class RoleOverlayContractTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p27m-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.parent = self.root / "parent"
        self.project = self.parent / "proj"
        for path in (self.home / ".omp" / "agent" / "agents", self.project / ".omp" / "agents",
                     self.parent / ".omp" / "agents", self.project / ".claude" / "agents"):
            path.mkdir(parents=True)

    def agent(self, directory: Path, filename: str, name: str | None, body: str = "BODY-SECRET-p27m") -> None:
        front = f"---\nname: {name}\ndescription: DESC-SECRET-p27m\n---\n" if name else ""
        (directory / filename).write_text(front + body + "\n")

    def test_task_disabled_agents_lists_project_ancestor_and_home_definition_names_only(self):
        # C-D59 correction: only definitions OMP 18.4.4 actually loads are listed: the NEAREST .omp/agents (a farther
        # ancestor's dir is shadowed), ~/.omp/agent/agents, frontmatter name AND description, never main/sub;
        # a file without frontmatter (stem-only) is not loaded by OMP, so it is not listed.
        self.agent(self.project / ".omp" / "agents", "a.md", "project-agent")
        self.agent(self.project / ".omp" / "agents", "stem-only.md", None)
        self.agent(self.project / ".omp" / "agents", "reserved.md", "main")
        self.agent(self.parent / ".omp" / "agents", "b.md", "ancestor-agent")
        self.agent(self.home / ".omp" / "agent" / "agents", "c.md", "home-agent")
        (self.project / ".omp" / "agents" / "notes.txt").write_text("not an agent")
        overlay = launcher.role_overlay("worker", project_dir=self.project, home=self.home)
        names = set(ambient_disabled(overlay, "worker"))  # C-D68: plus the worker's fixed set (bundled agents)
        self.assertEqual(names, {"project-agent", "home-agent"}, names)
        self.assertNotIn("notes", names)
        blob = json.dumps(overlay)
        self.assertNotIn("BODY-SECRET-p27m", blob)
        self.assertNotIn("DESC-SECRET-p27m", blob)
        # without a project-level dir the nearest ancestor's dir is the one OMP reads
        for extra in (self.project / ".omp" / "agents" / "a.md", self.project / ".omp" / "agents" / "stem-only.md",
                      self.project / ".omp" / "agents" / "reserved.md", self.project / ".omp" / "agents" / "notes.txt"):
            extra.unlink()
        (self.project / ".omp" / "agents").rmdir()
        (self.project / ".omp").rmdir()
        again = set(ambient_disabled(launcher.role_overlay("worker", project_dir=self.project, home=self.home), "worker"))
        self.assertEqual(again, {"ancestor-agent", "home-agent"}, again)

    def test_agent_definition_reading_is_bounded_and_robust(self):
        directory = self.project / ".omp" / "agents"
        self.agent(directory, "ok.md", "fine-agent")
        (directory / "empty.md").write_text("")
        (directory / "binary.md").write_bytes(os.urandom(4096))
        (directory / "huge.md").write_text("---\nname: huge-agent\ndescription: big\n---\n" + "x" * (8 << 20))
        (directory / "dir.md").mkdir()
        (directory / "loop.md").symlink_to(directory / "loop.md")
        broken = directory / "unreadable.md"
        broken.write_text("---\nname: unreadable-agent\n---\n")
        broken.chmod(0)
        self.addCleanup(broken.chmod, 0o600)
        started = time.monotonic()
        overlay = launcher.role_overlay("manager", project_dir=self.project, home=self.home)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertIn("fine-agent", overlay["task"]["disabledAgents"])
        self.assertIn("huge-agent", overlay["task"]["disabledAgents"])

    def test_missing_directories_yield_an_empty_agent_list_not_an_error(self):
        for role in ("manager", "worker"):  # C-D68: only the role's fixed set remains
            overlay = launcher.role_overlay(role, project_dir=self.root / "nowhere", home=self.root / "nohome")
            self.assertEqual(ambient_disabled(overlay, role), [])
            self.assertEqual(set(overlay["task"]["disabledAgents"]), CD68_ROLE_DISABLED[role])

    def test_disabled_providers_keep_the_users_entries_after_the_workbench_ids(self):
        overlay = launcher.role_overlay("manager", project_dir=self.project, home=self.home,
                                        user_disabled_providers=["my-private-thing", "cline", "my-private-thing"])
        providers = overlay["disabledProviders"]
        self.assertLessEqual(set(CAPABILITY_IDS) | {"my-private-thing"}, set(providers))
        self.assertEqual(len(providers), len(set(providers)))
        self.assertEqual([p for p in providers if p in MODEL_PROVIDER_IDS], [])

    def test_skills_directory_and_role_filters(self):
        default = launcher.role_overlay("manager", project_dir=self.project, home=self.home)
        self.assertEqual(default["skills"]["customDirectories"], [str(SKILLS_DIR)])
        self.assertTrue(os.path.isabs(default["skills"]["customDirectories"][0]))
        # CW-18: the default role filter is exact (manager only to-worker, worker only to-manager).
        self.assertEqual(default["skills"]["includeSkills"], ["to-worker"])
        self.assertEqual(launcher.role_overlay("worker", project_dir=self.project, home=self.home)["skills"]
                         ["includeSkills"], ["to-manager"])
        filters = {"manager": ("order-manager", "wb-*"), "worker": ("order-worker",)}
        manager = launcher.role_overlay("manager", project_dir=self.project, home=self.home, role_skills=filters)
        worker = launcher.role_overlay("worker", project_dir=self.project, home=self.home, role_skills=filters)
        self.assertEqual(manager["skills"]["includeSkills"], ["order-manager", "wb-*"])
        self.assertEqual(worker["skills"]["includeSkills"], ["order-worker"])
        other = self.root / "wbskills"
        moved = launcher.role_overlay("worker", project_dir=self.project, home=self.home, skills_dir=other)
        self.assertEqual(moved["skills"]["customDirectories"], [str(other)])
        with self.assertRaises(ValueError):
            launcher.role_overlay("intruder", project_dir=self.project, home=self.home)

    def test_role_skill_allowlist_reads_names_from_the_skills_directory(self):
        skills = self.root / "s"
        for name, front in (("one", "name: order-one"), ("two", "name: wb-two"), ("nofront", None)):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text(f"---\n{front}\ndescription: d\n---\nbody\n" if front else "body only\n")
        (skills / "README.md").write_text("not a skill")
        (skills / "empty").mkdir()
        self.assertEqual(launcher.workbench_skill_names(skills), ("nofront", "order-one", "wb-two"))
        self.assertEqual(launcher.workbench_skill_names(skills, ("order-*",)), ("order-one",))
        self.assertEqual(launcher.workbench_skill_names(self.root / "absent"), ())
        self.assertEqual(launcher.role_skill_allowlist("manager", SKILLS_DIR), ("to-worker",))
        self.assertEqual(launcher.role_skill_allowlist("worker", SKILLS_DIR), ("to-manager",))
        # The role filter is exact: look-alike or other skills in the directory are never allowed.
        crowded = self.root / "crowded"
        for name in ("to-worker", "to-manager", "to-worker-extra", "x-to-worker", "orca-cli", "to-managers"):
            (crowded / name).mkdir(parents=True)
            (crowded / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n")
        self.assertEqual(launcher.role_skill_allowlist("manager", crowded), ("to-worker",))
        self.assertEqual(launcher.role_skill_allowlist("worker", crowded), ("to-manager",))

    def test_written_overlay_is_private_valid_and_never_touches_the_static_file_or_home(self):
        static_hash = sha(STATIC_OVERLAY)
        home_before = sorted(str(p) for p in self.home.rglob("*"))
        data = self.root / "data"
        data.mkdir(mode=0o700)
        paths = {}
        for role in ("manager", "worker"):
            overlay = launcher.role_overlay(role, project_dir=self.project, home=self.home,
                                            user_disabled_providers=["u1"])
            paths[role] = launcher.write_role_overlay(data, role, overlay)
            self.assertEqual(stat.S_IMODE(paths[role].stat().st_mode), 0o600)
            self.assertEqual(paths[role].parent, data)
            self.assertEqual(json.loads(paths[role].read_text()), json.loads(json.dumps(overlay)))
        self.assertNotEqual(paths["manager"], paths["worker"])
        again = launcher.write_role_overlay(data, "worker", launcher.role_overlay("worker", project_dir=self.project,
                                                                                  home=self.home))
        self.assertEqual(again, paths["worker"])
        self.assertEqual(stat.S_IMODE(again.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in data.iterdir() if p.name.startswith(".")], [], "temp files left behind")
        self.assertEqual(sha(STATIC_OVERLAY), static_hash)
        self.assertEqual(sorted(str(p) for p in self.home.rglob("*")), home_before)
        with self.assertRaises(ValueError):
            launcher.write_role_overlay(data, "intruder", {})


class UserDisabledProvidersContractTests(unittest.TestCase):
    def fake(self, body: str) -> tuple[Path, Path]:
        root = Path(tempfile.mkdtemp(prefix="p27m-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        omp = root / "omp"
        omp.write_text("#!/usr/bin/env python3\nimport sys, json, time\n" + body)
        omp.chmod(0o755)
        return omp, root

    def test_reads_the_users_list_via_a_read_only_config_command(self):
        omp, root = self.fake('print(json.dumps({"value": ["mine", "cursor"], "argv": sys.argv[1:]}))\n')
        self.assertEqual(launcher.user_disabled_providers(str(omp), cwd=root, environment=os.environ), ["mine", "cursor"])

    def test_unreadable_or_odd_answers_yield_none_not_an_exception(self):
        for body in ('sys.exit(3)\n', 'print("not json")\n', 'print(json.dumps({"value": "cursor"}))\n',
                     'print(json.dumps({"value": [1, 2]}))\n', 'print(json.dumps([]))\n', 'raise SystemExit("boom")\n'):
            with self.subTest(body=body):
                omp, root = self.fake(body)
                self.assertIsNone(launcher.user_disabled_providers(str(omp), cwd=root, environment=os.environ))
        self.assertIsNone(launcher.user_disabled_providers("/nonexistent/omp", cwd="/tmp", environment=os.environ))

    def test_hanging_omp_is_bounded_by_the_timeout(self):
        omp, root = self.fake("time.sleep(30)\n")
        started = time.monotonic()
        self.assertIsNone(launcher.user_disabled_providers(str(omp), cwd=root, environment=os.environ, timeout=0.8))
        self.assertLess(time.monotonic() - started, 5.0)


# ------------------------------------------------------------------------- 2. check_isolation against a fake RPC OMP
FAKE_RPC = r'''#!/usr/bin/env python3
import json, os, sys, time, subprocess
mode = json.load(open(os.environ["FAKE_MODE_FILE"]))
log = open(os.environ["FAKE_LOG_FILE"], "a")
def record(**kw):
    log.write(json.dumps(kw) + "\n"); log.flush()
stat = open("/proc/self/stat").read().rsplit(")", 1)[1].split()
record(event="start", pid=os.getpid(), ticks=stat[19], argv=sys.argv[1:], env={k: os.environ.get(k) for k in
       ("PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE", "WORKBENCH_G3_TOKEN", "WORKBENCH_G3_BRIDGE_SOCKET",
        "WORKBENCH_G3_ROLE")}, cwd=os.getcwd())
if mode.get("child"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], stdout=sys.stdout)
    cs = open(f"/proc/{child.pid}/stat").read().rsplit(")", 1)[1].split()
    record(event="child", pid=child.pid, ticks=cs[19])
if mode["behaviour"] == "die":
    sys.exit(5)
shapes = mode.get("shapes", {})
# C-D68 (p27-cd68-test-01): like the real OMP 18.6.1 (live-verified in live_omp_tools_independent_p27cd68.py), the
# fake honours ``task.disabledAgents`` of every JSON ``--config`` overlay and the ``--tools`` built-in allowlist.
# Its agent list is OMP's bundled agents plus the Workbench explorer/analyst that the Workbench home always carries.
_argv = sys.argv[1:]
DISABLED, TOOLS_ALLOW = set(), None
for _i, _a in enumerate(_argv[:-1]):
    if _a == "--config":
        try:
            DISABLED |= set((json.load(open(_argv[_i + 1])).get("task") or {}).get("disabledAgents") or [])
        except Exception:
            pass
    if _a == "--tools":
        TOOLS_ALLOW = set(_argv[_i + 1].split(","))
def _agents_filter(tool):
    if tool.get("name") != "task":
        return tool
    lines = [l for l in tool.get("description", "").split("\n")
             if not (l.startswith("- `") and l[3:].split("`", 1)[0] in DISABLED)]
    return {**tool, "description": "\n".join(lines)}
PROMPT = "§ Role\nYou are omp's trusted coding assistant.\n" + shapes.get("prompt_extra", "") + "\n# Internal URLs\n- `skill://<name>`: x\n"
STATE = {"model": {"provider": "stubprov", "id": "stub-model-1"}, "systemPrompt": shapes.get("prompt", PROMPT),
         "dumpTools": [_agents_filter(t) for t in shapes.get("dumpTools", [
             {"name": "read", "description": "read"}, {"name": "bash", "description": "bash"},
             {"name": "eval", "description": "eval"},
             {"name": "task", "description": "Launch\n# Available Agents\n- `scout` (RO): a\n- `reviewer`: b\n- `security-reviewer`: c\n- `task`: d\n- `sonic`: e\n- `explorer` (RO): f\n- `analyst` (RO): g\n\n# Other\n"}])
             if TOOLS_ALLOW is None or t.get("name") in TOOLS_ALLOW]}
COMMANDS = shapes.get("commands", [{"name": "init", "source": "builtin"}, {"name": "autoresearch", "source": "extension"},
                                   {"name": "settings", "source": "builtin"}])
for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    record(event="rpc", type=req.get("type"))
    if mode["behaviour"] == "silent":
        continue
    if mode["behaviour"] == "garbage":
        sys.stdout.write("this is not json\n"); sys.stdout.flush(); continue
    if mode["behaviour"] == "error":
        sys.stdout.write(json.dumps({"id": req["id"], "type": "response", "command": req["type"], "success": False,
                                     "error": "boom"}) + "\n"); sys.stdout.flush(); continue
    if req.get("type") == "get_state":
        data = STATE
    elif req.get("type") == "get_available_commands":
        data = {"commands": COMMANDS}
    else:
        record(event="unexpected", type=req.get("type")); data = {}
    sys.stdout.write(json.dumps({"id": req["id"], "type": "response", "command": req["type"], "success": True,
                                 "data": data}) + "\n"); sys.stdout.flush()
    if mode["behaviour"] == "answer_then_exit_after" and req.get("type") == "get_available_commands":
        sys.exit(0)
'''

SKILLS_BLOCK = "<skills>\n- {items}\n</skills>\n"
PLANTED = {
    "context": {"prompt_extra": "<project-context>\n<repo-rules>\nMUST follow these context files for all tasks:\n"
                                '<file path="/scratch/parent/CLAUDE.md">\nCANARY_CTX_PARENT\n</file>\n'
                                '<file path="/scratch/proj/AGENTS.md">\nCANARY_CTX_PROJ\n</file>\n</repo-rules>\n'
                                "</project-context>\n",
                "needles": ["/scratch/parent/CLAUDE.md", "/scratch/proj/AGENTS.md"]},
    "orca-skill": {"prompt_extra": "<skills>\n- orca-cli: Use Orca CLI for worktrees\n- computer-use: desktop\n</skills>\n",
                   "commands": [{"name": "init", "source": "builtin"}, {"name": "skill:orca-cli", "source": "skill"},
                                {"name": "skill:computer-use", "source": "skill"}],
                   "needles": ["orca-cli", "computer-use"]},
    "project-skill": {"prompt_extra": "<skills>\n- canary-claude-skill: CANARY_DESC\n</skills>\n",
                      "commands": [{"name": "init", "source": "builtin"},
                                   {"name": "skill:canary-claude-skill", "source": "skill"}],
                      "needles": ["canary-claude-skill"]},
    "rule": {"prompt_extra": "<generic-rules>\nCANARY_RULE_ALWAYS_BODY\n</generic-rules>\n\n<domain-rules>\n"
                             "- canary-domain (*.zz): CANARY_RULE_DOMAIN_DESC\n</domain-rules>\n",
             "needles": ["CANARY_RULE_ALWAYS_BODY", "canary-domain"]},
    "mcp": {"prompt_extra": "## MCP Tool Routes\n\nExecute each mounted tool: write JSON arguments to its path.\n"
                            "- `xd://mcp__canary_canary_tool`: CANARY_MCP_TOOL\n",
            "needles": ["mcp__canary_canary_tool"]},
    "task-agent": {"dumpTools": [{"name": "task", "description": "Launch\n# Available Agents\n- `canary-agent`: CANARY_AGENT\n"
                                                                "- `scout` (RO): a\n- `task`: d\n\n# Other\n"}],
                   "needles": ["canary-agent"]},
    "command": {"commands": [{"name": "init", "source": "builtin"}, {"name": "canary-cmd", "source": "file"}],
                "needles": ["canary-cmd"]},
    "system-md": {"prompt": "CANARY_SYSTEM_MD_REPLACEMENT only\n", "needles": []},
}


class Harness:
    """A fake OMP script plus its mode/log files in an owned temp dir."""

    def __init__(self, test: unittest.TestCase):
        self.root = Path(tempfile.mkdtemp(prefix="p27m-", dir="/tmp"))
        test.addCleanup(shutil.rmtree, self.root, True)
        self.script = self.root / "omp"
        self.script.write_text(FAKE_RPC)
        self.script.chmod(0o755)
        self.mode_file, self.log_file = self.root / "mode.json", self.root / "log.jsonl"
        self.set_mode()
        self.env = {**os.environ, "FAKE_MODE_FILE": str(self.mode_file), "FAKE_LOG_FILE": str(self.log_file)}
        self.test = test
        test.addCleanup(self.reap)

    def set_mode(self, behaviour: str = "clean", shapes: dict | None = None, **extra) -> None:
        self.mode_file.write_text(json.dumps({"behaviour": behaviour, "shapes": shapes or {}, **extra}))

    def records(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.log_file.read_text().splitlines() if line.strip()]
        except OSError:
            return []

    def reap(self) -> None:
        for rec in self.records():
            if rec.get("event") in ("start", "child") and self.alive(rec):
                try:
                    os.kill(rec["pid"], signal.SIGKILL)  # only if the identity still matches (checked in alive())
                except OSError:
                    pass

    @staticmethod
    def alive(rec: dict) -> bool:
        try:
            fields = Path(f"/proc/{rec['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            return False
        return fields[0] not in "ZX" and fields[19] == rec["ticks"]

    def check(self, role="worker", allowed=(), timeout=15.0, cancel=None, role_config=True) -> dict:
        # C-D68: the checked command carries the role's real overlay (task.disabledAgents) and --tools, as the
        # pane command does; ``role_config=False`` is the negative control without them.
        if role_config:
            overlay = launcher.write_role_overlay(self.root, role, launcher.role_overlay(
                role, project_dir=self.root / "no-project", home=self.root / "no-home"))
            command = launcher.omp_command(plan_for(omp=str(self.script)), overlay)
        else:
            command = [str(self.script), "--config", "/x/static.yml", "--config", "/x/role.yml", "--no-extensions",
                       "--extension", "/x/bridge.ts"]
        return launcher.check_isolation(command, cwd=self.root, environment=self.env, role=role,
                                        allowed_skills=allowed, omp_version="omp/18.4.4", timeout=timeout, cancel=cancel)


class CheckIsolationContractTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)

    def test_clean_omp_is_ok_and_only_introspection_rpcs_are_sent(self):
        result = self.h.check()
        self.assertEqual((result["ok"], result["state"], result["leaks"], result["error"]), (True, "ok", [], None))
        events = self.h.records()
        rpc = [e["type"] for e in events if e["event"] == "rpc"]
        self.assertEqual(sorted(rpc), ["get_available_commands", "get_state"], "zero model calls: introspection only")
        self.assertEqual([e for e in events if e["event"] == "unexpected"], [])
        start = next(e for e in events if e["event"] == "start")
        for flag in ("--mode", "rpc", "--no-session", "--no-title", "--no-extensions", "--extension"):
            self.assertIn(flag, start["argv"])
        self.assertEqual(start["argv"].count("--config"), 2)
        self.assertNotIn("prompt", rpc)
        self.assertFalse([a for a in start["argv"] if a.startswith("--profile")])

    def test_every_planted_ambient_source_is_reported_as_a_leak(self):
        for name, planted in PLANTED.items():
            with self.subTest(source=name):
                self.h.set_mode(shapes={k: v for k, v in planted.items() if k != "needles"})
                result = self.h.check()
                self.assertEqual((result["ok"], result["state"]), (False, "leak"), result)
                self.assertTrue(result["leaks"], f"{name}: no leak listed")
                blob = json.dumps(result)
                for needle in planted["needles"]:
                    self.assertIn(needle, blob, f"{name}: {needle!r} not named in the report")

    def test_everything_at_once_lists_all_classes(self):
        merged: dict = {"prompt_extra": "", "commands": [{"name": "init", "source": "builtin"}], "dumpTools": None}
        needles = []
        for name, planted in PLANTED.items():
            if name == "system-md":
                continue
            merged["prompt_extra"] += planted.get("prompt_extra", "")
            merged["commands"] += [c for c in planted.get("commands", []) if c["name"] != "init"]
            if planted.get("dumpTools"):
                merged["dumpTools"] = planted["dumpTools"]
            needles += planted["needles"]
        merged = {k: v for k, v in merged.items() if v is not None}
        self.h.set_mode(shapes=merged)
        result = self.h.check()
        self.assertEqual(result["state"], "leak")
        blob = json.dumps(result)
        for needle in needles:
            self.assertIn(needle, blob)

    def test_workbench_skills_are_allowed_and_bundled_items_are_not_leaks(self):
        shapes = {"prompt_extra": "<skills>\n- order-worker: Workbench skill\n</skills>\n",
                  "commands": [{"name": "init", "source": "builtin"}, {"name": "autoresearch", "source": "extension"},
                               {"name": "skill:order-worker", "source": "skill"}]}
        self.h.set_mode(shapes=shapes)
        allowed = self.h.check(allowed=("order-worker",))
        self.assertEqual((allowed["ok"], allowed["leaks"]), (True, []), allowed["leaks"])
        blocked = self.h.check(allowed=())
        self.assertFalse(blocked["ok"], "a skill outside the allow-list must be reported")
        self.assertIn("order-worker", json.dumps(blocked))

    def test_the_real_role_allowlists_accept_only_the_roles_own_workbench_skill(self):
        # CW-18: manager ('to-worker',), worker ('to-manager',); the other role's skill or any other skill is a leak.
        def shapes(*names):
            return {"prompt_extra": "<skills>\n" + "".join(f"- {n}: d\n" for n in names) + "</skills>\n",
                    "commands": [{"name": "init", "source": "builtin"}]
                    + [{"name": f"skill:{n}", "source": "skill"} for n in names]}
        for role, own, other in (("manager", "to-worker", "to-manager"), ("worker", "to-manager", "to-worker")):
            allowed = launcher.role_skill_allowlist(role)
            self.assertEqual(allowed, (own,))
            with self.subTest(role=role, case="own"):
                self.h.set_mode(shapes=shapes(own))
                result = self.h.check(role=role, allowed=allowed)
                self.assertEqual((result["ok"], result["leaks"]), (True, []), result["leaks"])
            for intruder in (other, "orca-cli"):
                with self.subTest(role=role, case=intruder):
                    self.h.set_mode(shapes=shapes(own, intruder))
                    result = self.h.check(role=role, allowed=allowed)
                    self.assertFalse(result["ok"], f"{intruder} must be a leak for the {role}")
                    self.assertEqual(result["state"], "leak")
                    self.assertIn(f"skill:{intruder}", result["leaks"])
                    self.assertNotIn(f"skill:{own}", result["leaks"])

    def test_check_failures_are_failed_state_with_a_reason_never_ok(self):
        for behaviour in ("die", "garbage", "error"):
            with self.subTest(behaviour=behaviour):
                self.h.set_mode(behaviour)
                result = self.h.check(timeout=6.0)
                self.assertEqual((result["ok"], result["state"]), (False, "failed"), result)
                self.assertTrue(result["error"], "a failed check must say why")
                self.assertFalse([e for e in self.h.records() if e["event"] in ("start", "child") and self.h.alive(e)],
                                 "a check process outlived the failed check")

    def test_silent_omp_hits_the_bound_and_is_cleaned_up_by_identity(self):
        self.h.set_mode("silent")
        started = time.monotonic()
        result = self.h.check(timeout=1.5)
        self.assertLess(time.monotonic() - started, 12.0)
        self.assertEqual((result["ok"], result["state"]), (False, "failed"))
        self.assertRegex(str(result["error"]).lower(), r"timed out|timeout")
        starts = [e for e in self.h.records() if e["event"] == "start"]
        self.assertEqual(len(starts), 1)
        self.assertFalse(self.h.alive(starts[0]), "the silent OMP was not stopped")

    def test_child_processes_of_the_check_are_stopped_with_it(self):
        self.h.set_mode("clean", child=True)
        result = self.h.check()
        self.assertEqual(result["state"], "ok", result)
        events = self.h.records()
        child = next(e for e in events if e["event"] == "child")
        parent = next(e for e in events if e["event"] == "start")
        self.assertFalse(self.h.alive(parent))
        self.assertFalse(self.h.alive(child), "a grandchild of the check survived")

    def test_cancel_stops_the_check_promptly(self):
        import threading
        self.h.set_mode("silent")
        cancel = threading.Event()
        threading.Timer(0.6, cancel.set).start()
        started = time.monotonic()
        result = self.h.check(timeout=30.0, cancel=cancel)
        self.assertLess(time.monotonic() - started, 8.0)
        self.assertEqual(result["state"], "failed")
        starts = [e for e in self.h.records() if e["event"] == "start"]
        self.assertFalse(self.h.alive(starts[0]))

    def test_unspawnable_command_is_a_failed_result_not_an_exception(self):
        result = launcher.check_isolation(["/nonexistent/omp"], cwd="/tmp", environment=os.environ, role="worker",
                                          allowed_skills=(), omp_version="x")
        self.assertEqual((result["ok"], result["state"]), (False, "failed"))
        self.assertTrue(result["error"])

    def test_summary_aggregates_roles_and_marks_version(self):
        clean = self.h.check("manager")
        self.h.set_mode(shapes=PLANTED["orca-skill"])
        leaky = self.h.check("worker")
        summary = launcher.summarize_isolation({"manager": clean, "worker": leaky}, "omp/18.4.4")
        self.assertEqual((summary["checked"], summary["ok"], summary["state"]), (True, False, "leak"))
        self.assertTrue(summary["warning"], "a leak must carry a visible warning text")
        self.assertTrue(any("worker" in leak and "orca-cli" in leak for leak in summary["leaks"]), summary["leaks"])
        both_clean = launcher.summarize_isolation({"manager": clean, "worker": clean}, "omp/18.4.4")
        self.assertEqual((both_clean["checked"], both_clean["ok"], both_clean["leaks"]), (True, True, []))
        self.assertFalse(both_clean["warning"])
        self.assertEqual(both_clean["omp_version"], "omp/18.4.4")
        empty = launcher.summarize_isolation({}, "omp/18.4.4")
        self.assertFalse(empty["ok"], "no results at all must not read as ok")
        pending = launcher.pending_isolation("omp/18.4.4")
        self.assertEqual((pending["checked"], pending["ok"]), (False, None))

    def test_version_drift_is_visible_in_the_summary(self):
        # C-D68 (6): the isolation evidence is re-made with the installed OMP 18.6.1
        same = launcher.summarize_isolation({"manager": self.h.check("manager")}, "omp/18.6.1")
        newer = launcher.summarize_isolation({"manager": self.h.check("manager")}, "omp/19.1.0")
        self.assertEqual(newer["omp_version"], "omp/19.1.0")
        self.assertTrue(newer["version_drift"], "a version other than the evidence versions must show drift")
        self.assertNotEqual(same["version_drift"], newer["version_drift"])
        self.assertNotIn("isolation", same["version_drift"], "the running version equals the isolation evidence version")
        self.assertIn("isolation", newer["version_drift"])

    # C-D68 (p27-cd68-test-01) role-aware leaks with negative controls.
    def test_cd68_the_real_role_commands_are_clean_and_each_role_sees_its_own_agents(self):
        for role, agents, absent in (("manager", BUNDLED_AGENTS, WORKBENCH_AGENTS),
                                     ("worker", WORKBENCH_AGENTS, BUNDLED_AGENTS)):
            with self.subTest(role=role):
                result = self.h.check(role=role)
                self.assertEqual((result["state"], result["leaks"], result["warnings"]), ("ok", [], []), result)
                self.assertEqual(set(result["observed"]["task_agents"]), agents)
                self.assertFalse(set(result["observed"]["task_agents"]) & absent)
                tools = set(result["observed"]["tools"])
                if role == "worker":  # C-D68 (1)
                    self.assertFalse(tools & {"bash", "eval"}, tools)
                else:  # C-D68 (3): manager keeps OMP's tools
                    self.assertTrue({"bash", "eval"} <= tools, tools)

    def test_cd68_negative_control_worker_without_its_role_config_leaks_tools_and_bundled_agents(self):
        result = self.h.check(role="worker", role_config=False)
        self.assertEqual(result["state"], "leak", result)
        for leak in ("tool:bash", "tool:eval", *(f"task_agent:{name}" for name in sorted(BUNDLED_AGENTS))):
            self.assertIn(leak, result["leaks"])
        self.assertNotIn("task_agent:explorer", result["leaks"])

    def test_cd68_negative_control_manager_seeing_a_workbench_agent_is_a_leak(self):
        self.h.set_mode(shapes={"dumpTools": [{"name": "task", "description":
                                               "L\n# Available Agents\n- `scout`: a\n- `explorer`: f\n\n# Other\n"}]})
        result = self.h.check(role="manager", role_config=False)
        self.assertEqual(result["state"], "leak", result)
        self.assertIn("task_agent:explorer", result["leaks"])
        self.assertNotIn("task_agent:scout", result["leaks"])

    def test_cd68_worker_missing_a_workbench_agent_is_a_warning_and_a_bundled_one_a_leak(self):
        self.h.set_mode(shapes={"dumpTools": [{"name": "task", "description":
                                               "L\n# Available Agents\n- `explorer`: f\n\n# Other\n"}]})
        result = self.h.check(role="worker")
        self.assertEqual((result["state"], result["leaks"]), ("warning", []), result)
        self.assertEqual(result["warnings"], ["task_agent:missing:analyst"])
        self.h.set_mode(shapes={"dumpTools": [{"name": "task", "description":
                                               "L\n# Available Agents\n- `explorer`: f\n- `analyst`: g\n"
                                               "- `scout`: a\n\n# Other\n"}]})
        leak = self.h.check(role="worker", role_config=False)
        self.assertEqual(leak["leaks"], ["task_agent:scout"])
        self.h.set_mode(shapes={"dumpTools": [{"name": "read", "description": "r"}, {"name": "eval", "description": "e"}]})
        self.assertIn("tool:eval", self.h.check(role="worker", role_config=False)["leaks"])
        self.assertEqual(self.h.check(role="manager", role_config=False)["leaks"], [], "manager keeps eval")


# ------------------------------------------------------------------------- 3. the real workbench entrypoint
ENTRY_STUB = r'''#!/usr/bin/env python3
import json, os, sys, tty, time
ROOT = {root!r}
mode = json.load(open(ROOT + "/mode.json"))
argv = sys.argv[1:]
stat = open("/proc/self/stat").read().rsplit(")", 1)[1].split()
def record(**kw):
    with open(ROOT + "/calls.jsonl", "a") as log:
        log.write(json.dumps(kw) + "\n")
if "--version" in argv:
    print("omp/" + mode.get("version", "18.4.4")); sys.exit(0)
if argv[:2] == ["config", "get"]:
    record(event="config_get", argv=argv)
    if mode.get("config_fail"):
        sys.exit(4)
    print(json.dumps({{"value": mode.get("user_disabled_providers", [])}})); sys.exit(0)
configs = {{}}
for i, a in enumerate(argv):
    if a == "--config" and i + 1 < len(argv):
        try:
            configs[argv[i + 1]] = open(argv[i + 1]).read()
        except OSError:
            configs[argv[i + 1]] = None
rpc = "--mode" in argv and "rpc" in argv
record(event="start", kind="rpc" if rpc else "pane", pid=os.getpid(), ticks=stat[19], argv=argv, cwd=os.getcwd(),
       configs=configs, env={{k: os.environ.get(k) for k in ("PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE",
       "OMP_CODING_AGENT_DIR", "WORKBENCH_G3_TOKEN", "WORKBENCH_G3_BRIDGE_SOCKET", "WORKBENCH_G3_ROLE", "HOME",
       "PI_CONFIG_DIR", "BUN_RUNTIME_TRANSPILER_CACHE_PATH", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")}})
held = []
if os.environ.get("PI_CODING_AGENT_DIR") and not mode.get("no_home_files"):
    # like the real OMP: keep a data file of its agent dir open (models.db); never touches agent.db
    held.append(open(os.path.join(os.environ["PI_CODING_AGENT_DIR"], "models.db"), "ab"))
if rpc:
    behaviour = mode.get("behaviour", "clean")
    if behaviour == "die":
        sys.exit(6)
    shapes = mode.get("shapes", {{}})
    prompt = shapes.get("prompt", "You are omp's trusted coding assistant.\n" + shapes.get("prompt_extra", "") + "\n")
    state = {{"model": {{"provider": "stubprov", "id": "m1"}}, "systemPrompt": prompt,
             "dumpTools": shapes.get("dumpTools", [{{"name": "read", "description": "r"}}])}}
    commands = shapes.get("commands", [{{"name": "init", "source": "builtin"}}])
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        record(event="rpc", type=req.get("type"), pid=os.getpid())
        if behaviour == "silent":
            continue
        login = mode.get("login", {{"providers": [{{"id": "stubprov", "name": "Stub", "authenticated": True}},
                                                 {{"id": "other", "name": "Other", "authenticated": False}}]}})
        data = (state if req.get("type") == "get_state" else {{"commands": commands}} if req.get("type") == "get_available_commands"
                else login if req.get("type") == "get_login_providers" else {{}})
        sys.stdout.write(json.dumps({{"id": req["id"], "type": "response", "command": req["type"], "success": True,
                                     "data": data}}) + "\n"); sys.stdout.flush()
    sys.exit(0)
role = os.environ.get("WORKBENCH_G3_ROLE", "?")
tty.setraw(0)
sys.stdout.write(f"STUB-OMP {{role}} ready\r\n> "); sys.stdout.flush()
while True:
    data = os.read(0, 4096)
    if not data:
        break
    record(event="pane_input", role=role, data=data.decode("latin-1"))
    sys.stdout.write(f"[{{role}} got]\r\n> "); sys.stdout.flush()
'''
SRC = str(REPO / "src")
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"


def identity(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if fields[0] in "ZX" else fields[19]


def procs_naming(root: Path) -> dict[int, str]:
    needle, found = str(root).encode(), {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            blob = (entry / "cmdline").read_bytes() + (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in blob and (ticks := identity(int(entry.name))):
            found[int(entry.name)] = ticks
    return found


class RealEntrypointBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p27m-e-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.project = self.root / "proj"
        self.data = self.root / "data"
        (self.home / ".omp" / "agent" / "agents").mkdir(parents=True)
        (self.project / ".omp" / "agents").mkdir(parents=True)
        (self.home / ".omp" / "agent" / "agents" / "home-agent.md").write_text("---\nname: home-agent\ndescription: d\n---\nBODY-p27m\n")
        self.auth_store = plant_fake_auth_store(self.home)  # C-D64: start needs the user's auth store (fake bytes)
        (self.project / ".omp" / "agents" / "proj-agent.md").write_text("---\nname: proj-agent\ndescription: d\n---\nBODY-p27m\n")
        (self.root / "omp").write_text(ENTRY_STUB.format(root=str(self.root)))
        (self.root / "omp").chmod(0o755)
        self.set_mode()
        self.env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
                    "TERM": "xterm-256color", "HOME": str(self.home)}
        self.home_before = self.tree(self.home)
        self.seen: dict[int, str] = {}
        self.leaked: list[int] = []
        self.addCleanup(self.stop_backend)

    @staticmethod
    def tree(path: Path) -> list[tuple[str, int]]:
        return sorted((str(p.relative_to(path)), p.stat().st_size) for p in path.rglob("*") if p.is_file())

    def set_mode(self, behaviour="clean", shapes=None, **extra):
        (self.root / "mode.json").write_text(json.dumps({"behaviour": behaviour, "shapes": shapes or {}, **extra}))

    def calls(self) -> list[dict]:
        try:
            return [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines() if line.strip()]
        except OSError:
            return []

    def cli(self, *args, env_extra=None, timeout=90):
        return subprocess.run([sys.executable, "-c", MAIN, *args], env={**self.env, **(env_extra or {})}, cwd=self.project,
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def start(self, *extra, env_extra=None):
        out = self.cli("start", "--data-dir", str(self.data), "--omp", str(self.root / "omp"), "--no-attach",
                       "--timeout", "8", *extra, env_extra=env_extra)
        self.seen.update(procs_naming(self.root))
        return out

    def status_json(self) -> dict | None:
        out = self.cli("status", "--data-dir", str(self.data), "--json")
        if out.returncode != 0:
            return None
        try:
            body = json.loads(out.stdout)
        except ValueError:
            return None
        return body.get("snapshot") if isinstance(body, dict) and body.get("running") else None

    def wait_isolation(self, timeout=40.0) -> dict:
        deadline = time.monotonic() + timeout
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = self.status_json()
            if snapshot and (snapshot.get("omp_isolation") or {}).get("checked"):
                return snapshot
            time.sleep(0.3)
        self.fail(f"the isolation check never finished: {snapshot and snapshot.get('omp_isolation')}")

    def stop_backend(self):
        self.seen.update(procs_naming(self.root))
        try:
            self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=60)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(identity(p) == t for p, t in self.seen.items()):
            time.sleep(0.1)
        for pid, ticks in self.seen.items():  # exact identity (pid + start ticks) only
            if identity(pid) != ticks:
                continue
            self.leaked.append(pid)
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            try:
                if identity(pid) == ticks:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)

    def by_kind(self, kind: str, role: str | None = None) -> list[dict]:
        out = [c for c in self.calls() if c.get("event") == "start" and c.get("kind") == kind]
        return [c for c in out if role is None or c["env"].get("WORKBENCH_G3_ROLE") == role]


class RealEntrypointIsolationTests(RealEntrypointBase):
    def test_panes_and_check_use_isolated_commands_and_snapshot_reports_ok(self):
        self.set_mode(user_disabled_providers=["my-private-provider"])
        self.start("--omp-arg=--flag-from-cli")  # the stub never joins the bridge: `start` itself stays "starting"
        snapshot = self.wait_isolation()
        iso = snapshot["omp_isolation"]
        self.assertEqual((iso["checked"], iso["ok"], iso["leaks"]), (True, True, []))
        self.assertIn("18.4.4", str(snapshot["backend"]["omp_version"]))
        self.assertIn("18.4.4", str(iso["omp_version"] if "omp_version" in iso else snapshot["backend"]["omp_version"]))
        self.assertFalse(iso.get("warning"))
        for role in ("manager", "worker"):
            panes, checks = self.by_kind("pane", role), self.by_kind("rpc", role)
            self.assertEqual(len(panes), 1, f"{role}: expected exactly one pane launch")
            self.assertEqual(len(checks), 1, f"{role}: expected exactly one isolation check launch")
            pane, check = panes[0], checks[0]
            argv = pane["argv"]
            configs = [argv[i + 1] for i, a in enumerate(argv) if a == "--config"]
            self.assertEqual(len(configs), 2, argv)
            self.assertEqual(configs[0], str(STATIC_OVERLAY))
            self.assertIn("--no-extensions", argv)
            self.assertEqual(argv[argv.index("--extension") + 1], str(REPO / "omp_bridge" / "g3" / "bridge.ts"))
            for forbidden in FORBIDDEN_ARGS:
                self.assertFalse([a for a in argv if a == forbidden or a.startswith(forbidden + "=")], argv)
            assert_required_isolation_args(self, argv)
            self.assertEqual(argv.count("--no-title"), 1, argv)
            self.assertEqual(check["argv"][check["argv"].index("--append-system-prompt") + 1], "")
            for key in FORBIDDEN_ENV:
                self.assertIsNone(pane["env"][key], f"{role}: {key} set for the pane")
                self.assertIsNone(check["env"][key])
            # C-D64: both the pane and its check run in the Workbench OMP home
            assert_workbench_home_env(self, pane["env"], self.home, self.data)
            self.assertEqual({k: check["env"][k] for k in HOME_ENV_KEYS}, {k: pane["env"][k] for k in HOME_ENV_KEYS})
            self.assertEqual(pane["env"]["HOME"], str(self.home), "HOME must stay the user's (bash tools, git, ssh)")
            self.assertGreater(argv.index("--flag-from-cli"), max(argv.index("--no-extensions"), argv.index(configs[1])))
            # the check runs the SAME command in rpc mode, and never as the real bridge peer
            self.assertEqual(check["argv"][:len(argv)], argv, f"{role}: the check ran a different command")
            for flag in ("--mode", "rpc", "--no-session", "--no-title"):
                self.assertIn(flag, check["argv"])
            self.assertNotEqual(check["env"]["WORKBENCH_G3_TOKEN"], pane["env"]["WORKBENCH_G3_TOKEN"])
            self.assertNotEqual(check["env"]["WORKBENCH_G3_BRIDGE_SOCKET"], pane["env"]["WORKBENCH_G3_BRIDGE_SOCKET"])
            self.assertEqual(check["cwd"], pane["cwd"])
            # role overlay content as the OMP process saw it
            overlay = json.loads(pane["configs"][configs[1]])
            self.assertLessEqual(set(CAPABILITY_IDS), set(overlay["disabledProviders"]))
            # C-D64: the Workbench home reads no user OMP config, so nothing of `omp config get` is unioned back, and
            # the user's ~/.omp/agent/agents is not OMP's user agents dir any more (only the project dir counts)
            self.assertNotIn("my-private-provider", overlay["disabledProviders"])
            self.assertIn("proj-agent", overlay["task"]["disabledAgents"])
            self.assertNotIn("my-private-provider", overlay["task"]["disabledAgents"])
            self.assertNotIn("home-agent", overlay["task"]["disabledAgents"], "the user's agents dir was read")
            self.assertEqual(overlay["skills"]["customDirectories"], [str(SKILLS_DIR)])
            self.assertNotIn("BODY-p27m", json.dumps(overlay))
            self.assertEqual(stat.S_IMODE(Path(configs[1]).stat().st_mode), 0o600)
            self.assertTrue(str(Path(configs[1])).startswith(str(self.data)), "role overlays live in the data dir")
        rpc = [c["type"] for c in self.calls() if c.get("event") == "rpc"]
        self.assertEqual(sorted(rpc), ["get_available_commands"] * 2 + ["get_login_providers"] * 2 + ["get_state"] * 2,
                         "zero model calls")
        self.assertEqual([c for c in self.calls() if c.get("event") == "config_get"], [], "the user's OMP config was read")
        agent = self.data / "omp-root" / "agent"
        self.assertTrue((agent / "agent.db").is_symlink())
        self.assertEqual(os.readlink(agent / "agent.db"), str(self.auth_store))
        self.assertEqual(stat.S_IMODE((agent / "config.yml").stat().st_mode), 0o600)
        self.assertEqual([c for c in self.calls() if c.get("event") == "pane_input"], [], "nothing typed into panes")
        # exact-identity cleanup of the two check processes
        for check in self.by_kind("rpc"):
            self.assertNotEqual(identity(check["pid"]), check["ticks"], "a check process is still running")
        self.assertEqual(self.tree(self.home), self.home_before, "the user's home was written to")
        text = self.cli("status", "--data-dir", str(self.data)).stdout
        self.assertRegex(text, r"omp isolation: ok", "the status summary must show the isolation result")
        self.assertNotIn("WARNING", text)

    def test_workbench_omp_args_env_is_appended_too(self):
        self.start(env_extra={"WORKBENCH_OMP_ARGS": "--flag-from-env --other=1"})
        self.wait_isolation()
        for role in ("manager", "worker"):
            argv = self.by_kind("pane", role)[0]["argv"]
            self.assertIn("--flag-from-env", argv)
            self.assertIn("--other=1", argv)
            self.assertGreater(argv.index("--flag-from-env"), argv.index("--no-extensions"))

    def test_leak_is_a_visible_warning_and_the_session_stays_up(self):
        planted = {k: v for k, v in PLANTED["orca-skill"].items() if k != "needles"}
        planted["prompt_extra"] += PLANTED["context"]["prompt_extra"]
        self.set_mode(shapes=planted)
        self.start()
        snapshot = self.wait_isolation()
        iso = snapshot["omp_isolation"]
        self.assertEqual((iso["checked"], iso["ok"]), (True, False))
        self.assertTrue(iso["leaks"])
        blob = json.dumps(iso["leaks"])
        for needle in ("orca-cli", "computer-use", "/scratch/proj/AGENTS.md"):
            self.assertIn(needle, blob)
        self.assertTrue(iso["warning"], "no visible warning for a leak")
        self.assertIn("orca-cli", iso["warning"] + blob)
        text = self.cli("status", "--data-dir", str(self.data)).stdout
        self.assertIn("WARNING", text, "the leak must be printed by `status`")
        self.assertIn("orca-cli", text)
        self.assertIn(snapshot["phase"], ("ready", "running", "starting", "active"), snapshot["phase"])
        for pane in ("manager_omp", "worker_omp", "host_shell"):
            self.assertTrue(snapshot["panes"][pane]["alive"], f"{pane} was killed by the leak")
        self.assertEqual(self.cli("status", "--data-dir", str(self.data), "--json").returncode, 0)

    def test_failed_check_is_a_visible_warning_and_the_session_stays_up(self):
        self.set_mode("die")
        self.start()
        snapshot = self.wait_isolation()
        iso = snapshot["omp_isolation"]
        self.assertEqual((iso["checked"], iso["ok"]), (True, False))
        self.assertTrue(iso["warning"], "a failed check must be visible, not silent")
        text = self.cli("status", "--data-dir", str(self.data)).stdout
        self.assertIn("WARNING", text)
        for pane in ("manager_omp", "worker_omp"):
            self.assertTrue(snapshot["panes"][pane]["alive"])

    def test_omp_version_and_drift_are_recorded(self):
        self.set_mode(version="19.4.2")
        self.start()
        snapshot = self.wait_isolation()
        self.assertIn("19.4.2", str(snapshot["backend"]["omp_version"]))
        self.assertIn("19.4.2", json.dumps(snapshot["omp_isolation"]), "the running OMP version is not in the isolation field")
        text = self.cli("status", "--data-dir", str(self.data)).stdout
        self.assertIn("19.4.2", text)

    def test_unreadable_user_config_does_not_stop_the_start(self):
        # C-D64: the user's OMP config is not read at all any more (an unreadable one cannot matter)
        self.set_mode(config_fail=True)
        self.start()
        snapshot = self.wait_isolation()
        self.assertEqual(snapshot["omp_isolation"]["checked"], True)
        self.assertEqual([c for c in self.calls() if c.get("event") == "config_get"], [])
        overlay_paths = [a for a in self.by_kind("pane", "worker")[0]["argv"] if a.endswith("omp-isolation-worker.yml")]
        self.assertEqual(len(overlay_paths), 1)

    def test_shutdown_leaves_no_isolation_check_or_pane_processes(self):
        self.start()
        self.wait_isolation()
        self.seen.update(procs_naming(self.root))
        out = self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(identity(p) == t for p, t in self.seen.items()):
            time.sleep(0.1)
        self.assertEqual([p for p, t in self.seen.items() if identity(p) == t], [], "processes survived shutdown")


class StartRefusedWithoutAuthStoreTests(RealEntrypointBase):
    """C-D64: no user OMP auth store -> no backend, exit 2, login guidance, nothing created in the user's home."""

    def test_start_refuses_with_guidance_and_creates_nothing_in_the_home(self):
        self.auth_store.unlink()
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))
        out = self.start()
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        text = out.stdout + out.stderr
        self.assertIn("omp", text)
        self.assertIn("/login", text)
        self.assertIn(str(self.auth_store), text)
        self.assertEqual([c for c in self.calls() if c.get("event") == "start"], [], "an OMP was started")
        self.assertIsNone(self.status_json(), "a backend is running")
        self.assertEqual(sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")), before,
                         "the user's home changed")
        self.assertFalse((self.data / "omp-root" / "agent" / "agent.db").exists())
        self.assertFalse(os.path.lexists(self.data / "omp-root" / "agent" / "agent.db"))


class HungCheckStaysOffTheStartPathTests(RealEntrypointBase):
    def test_a_check_that_never_answers_does_not_block_the_backend_and_ends_as_a_visible_failure(self):
        self.set_mode("silent")
        self.start()
        started = time.monotonic()
        first = self.status_json()
        self.assertLess(time.monotonic() - started, 4.0, "the backend did not answer while the check hung")
        self.assertIsNotNone(first)
        self.assertFalse(first["omp_isolation"]["checked"], "the hung check cannot have reported already")
        self.assertIn(first["omp_isolation"]["state"], ("pending", "running", "checking"))
        for pane in ("manager_omp", "worker_omp", "host_shell"):
            self.assertTrue(first["panes"][pane]["alive"], "panes must run while the check is pending")
        snapshot = self.wait_isolation(timeout=60.0)
        iso = snapshot["omp_isolation"]
        self.assertEqual((iso["ok"], iso["state"]), (False, "failed"))
        self.assertTrue(iso["warning"])
        for pane in ("manager_omp", "worker_omp"):
            self.assertTrue(snapshot["panes"][pane]["alive"], "a hung check must not kill the session")
        for check in self.by_kind("rpc"):
            self.assertNotEqual(identity(check["pid"]), check["ticks"], "a hung check process was left running")


if __name__ == "__main__":
    unittest.main()
