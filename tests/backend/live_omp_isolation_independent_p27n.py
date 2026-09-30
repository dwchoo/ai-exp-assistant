"""LIVE C-D59 isolation-correction check against real OMP 18.4.4 (p27-cd59-test-02), ZERO model calls. Opt-in: ``WB_LIVE_OMP=1``.

Run (repo root)::

    WB_LIVE_OMP=1 PYTHONPATH=src:tests/backend TMPDIR=/tmp/wbp27n /tmp/cw02-g1-venv/bin/python -m unittest \\
        tests.backend.live_omp_isolation_independent_p27n -v

Everything runs with a FAKE ``HOME`` (a scratch dir; the user's ``~/.omp`` ``~/.agents`` ``~/.claude`` ``~/.codex`` are never
read or written, and a metadata-only view of them is compared before/after) and a fake ``ANTHROPIC_API_KEY`` string that only
makes OMP list a model: only ``get_state`` / ``get_available_commands`` are sent (no prompt, no session, no title call), so no
provider is ever contacted. Every OMP process group is stopped by exact identity (pid + start time via pidfd); survivors are
failures. The oracle is the raw system prompt / tool list OMP itself reports, not the implementation's detector (which is only
cross-checked where the assertion is about ITS output: warning / leak state, start summary).

Covered (review R1/R3/R4/R5 + user answers): APPEND_SYSTEM.md canaries (4 project locations + user level) absent; a fake-HOME
PERSONALITY.md gives state ``warning`` with the default personality block kept; TITLE_SYSTEM.md is no leak with ``--no-title``;
dev.autoqa off (``xd://report_issue`` absent, also with ``PI_AUTO_QA`` in the environment); ``task.disabledAgents`` equals the
set of definitions OMP really loads (OMP is the oracle) and is unioned with the user's own value; the real ``start`` entrypoint
prints the isolation state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))

import live_omp_isolation_independent_p27m as live  # noqa: E402
from workbench.backend import launcher  # noqa: E402

LIVE = os.environ.get("WB_LIVE_OMP") == "1"
CANARY = "CANARY_P27N"
FAKE_KEY = "sk-ant-fake-p27n-never-used"
PROJECT_DIRS = (".omp", ".claude", ".codex", ".gemini")
BUNDLED = {"scout", "reviewer", "security-reviewer", "task", "sonic"}
MAIN = "import sys; from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def listed_agents(result: dict) -> set[str]:
    """Names in the task tool's '# Available Agents' block, as OMP reports them."""
    for tool in result["state"].get("dumpTools") or []:
        if isinstance(tool, dict) and tool.get("name") == "task":
            text = str(tool.get("description") or "")
            block = text.split("# Available Agents", 1)[-1] if "# Available Agents" in text else ""
            names = set()
            for line in block.splitlines()[1:]:  # blank lines separate groups (project / user / bundled) inside the block
                match = re.match(r"^- `([^`]+)`", line)
                if match:
                    names.add(match.group(1))
                elif line.strip():
                    break
            return names
    return set()


@unittest.skipUnless(LIVE, "set WB_LIVE_OMP=1 to run the live OMP isolation-correction check")
class LiveIsolationP27n(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.omp = shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
        if not os.access(cls.omp, os.X_OK):
            raise unittest.SkipTest("omp not found")
        cls.version = launcher.omp_version(cls.omp)
        cls.real_home = Path(os.environ.get("HOME") or Path.home())
        cls.ambient_before = live.ambient_view()  # metadata only
        cls.root = Path(tempfile.mkdtemp(prefix="wbp27n-", dir=os.environ.get("TMPDIR") or "/tmp"))
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        leftovers = live.naming_identities(cls.root)
        for pid, ticks in leftovers.items():
            live.kill_identity(pid, ticks, signal.SIGKILL)
        time.sleep(0.2)
        cls.leftover = [pid for pid, ticks in leftovers.items() if live.start_ticks(pid) == ticks]
        shutil.rmtree(cls.root, ignore_errors=True)

    # -- fixtures
    def home(self, **files: str) -> Path:
        """A fresh fake HOME; keyword ``name=text`` writes ``~/.omp/agent/<name with __ as dot>``."""
        type(self).counter += 1
        home = self.root / f"home{self.counter}"
        (home / ".omp" / "agent").mkdir(parents=True)
        for name, text in files.items():
            write(home / ".omp" / "agent" / name.replace("__", "."), text)
        return home

    def project(self) -> Path:
        type(self).counter += 1
        path = self.root / f"proj{self.counter}"
        path.mkdir()
        return path

    def env(self, home: Path, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("WORKBENCH_", "PI_", "OMP_", "TMUX", "HERDR_"))}
        env.update(HOME=str(home), ANTHROPIC_API_KEY=FAKE_KEY, **extra)
        return env

    def control(self, project: Path, env: dict[str, str], settle: float = 1.0) -> dict:
        return live.rpc_exchange([self.omp, *live.RPC_ARGS], project, env, self.root, settle=settle)

    def wb(self, role: str, project: Path, home: Path, env: dict[str, str], *, user_agents=(), args=()):
        plan = launcher.build_plan(env, omp=self.omp, omp_args=tuple(args))
        user = launcher.read_user_config(self.omp, cwd=project, environment=env)
        overlay = launcher.write_role_overlay(self.root, role, launcher.role_overlay(
            role, project_dir=project, home=home, user_disabled_providers=user.get("disabledProviders") or (),
            user_disabled_agents=[*(user.get("task.disabledAgents") or ()), *user_agents]))
        command = launcher.omp_command(plan, overlay)
        pane_env = launcher.omp_environment(env, plan, role=role, token="p27n-token",
                                            bridge_socket=self.root / "absent-bridge.sock")
        return plan, command, pane_env

    def isolated(self, role: str, project: Path, home: Path, env: dict[str, str], settle: float = 1.0, **kwargs) -> dict:
        _plan, command, pane_env = self.wb(role, project, home, env, **kwargs)
        result = live.rpc_exchange([*command, *live.RPC_ARGS], project, pane_env, self.root, settle=settle)
        result["command"], result["env"] = command, pane_env
        return result

    def check(self, role: str, project: Path, home: Path, env: dict[str, str]) -> dict:
        plan, command, pane_env = self.wb(role, project, home, env)
        check_env = launcher.isolation_check_environment(env, plan, role=role, absent_socket=self.root / "absent-check.sock")
        return launcher.check_isolation(command, cwd=project, environment=check_env, role=role, allowed_skills=(),
                                        omp_version=self.version, keep_raw=True)

    def clean(self, result: dict) -> None:
        self.assertEqual(result["sent"], ["get_state", "get_available_commands"], "only introspection RPCs allowed")
        self.assertEqual(result["survivors"], [], "an OMP process survived the exact-identity cleanup")

    # -- 0
    def test_00_version_and_fake_home_control_works(self):
        self.assertIn("18.4.4", self.version)
        home = self.home()
        result = self.control(self.project(), self.env(home))
        self.clean(result)
        self.assertIn("# Personality", live.prompt_text(result), "the default personality block is missing in the control")
        self.assertEqual(BUNDLED - listed_agents(result), set(), "the control does not list the bundled agents")

    # -- R1: APPEND_SYSTEM.md
    def test_append_system_md_is_injected_by_plain_omp_but_never_by_the_workbench_command(self):
        places = {f"project/{name}": name for name in PROJECT_DIRS}
        for label, name in places.items():
            with self.subTest(place=label):
                home, project = self.home(), self.project()
                canary = f"{CANARY}_APPEND_{name.strip('.').upper()}"
                write(project / name / "APPEND_SYSTEM.md", canary + "\n")
                env = self.env(home)
                control = self.control(project, env)
                self.clean(control)
                self.assertIn(canary, live.prompt_text(control), f"control invalid: {label} not injected by plain OMP")
                for role in ("manager", "worker"):
                    result = self.isolated(role, project, home, env)
                    self.clean(result)
                    self.assertNotIn(canary, live.surface(result), f"{role}: {label}/APPEND_SYSTEM.md reached the prompt")
                    argv = result["command"]
                    self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "")
                    report = self.check(role, project, home, env)
                    self.assertEqual((report["state"], report["ok"], report["leaks"]), ("ok", True, []), report)
        with self.subTest(place="user"):
            canary = f"{CANARY}_APPEND_USER"
            home, project = self.home(APPEND_SYSTEM__md=canary + "\n"), self.project()
            env = self.env(home)
            control = self.control(project, env)
            self.assertIn(canary, live.prompt_text(control), "control invalid: user-level APPEND_SYSTEM.md not injected")
            for role in ("manager", "worker"):
                result = self.isolated(role, project, home, env)
                self.clean(result)
                self.assertNotIn(canary, live.surface(result), f"{role}: user-level APPEND_SYSTEM.md reached the prompt")

    def test_a_user_supplied_append_prompt_still_applies_and_ambient_files_do_not_stack(self):
        home, project = self.home(), self.project()
        write(project / ".claude" / "APPEND_SYSTEM.md", f"{CANARY}_AMBIENT_APPEND\n")
        env = self.env(home)
        result = self.isolated("worker", project, home, env, args=("--append-system-prompt", f"{CANARY}_USER_ARG"))
        self.clean(result)
        prompt = live.prompt_text(result)
        self.assertIn(f"{CANARY}_USER_ARG", prompt, "a user --omp-arg must still override the empty slot")
        self.assertNotIn(f"{CANARY}_AMBIENT_APPEND", prompt)

    # -- R3: PERSONALITY.md (keep default + warn)
    def test_personality_md_gives_a_warning_and_the_default_personality_block_is_kept(self):
        marker = f"{CANARY}_PERSONALITY_TEXT"
        clean_home, project = self.home(), self.project()
        env = self.env(clean_home)
        base = self.isolated("manager", project, clean_home, env)
        self.clean(base)
        self.assertIn("# Personality", live.prompt_text(base), "OMP's default personality block must stay")
        report = self.check("manager", project, clean_home, env)
        self.assertEqual((report["state"], report["ok"], report["leaks"]), ("ok", True, []), report)
        home = self.home(PERSONALITY__md=marker + "\n")
        env = self.env(home)
        control = self.control(project, env)
        self.assertIn(marker, live.prompt_text(control), "control invalid: PERSONALITY.md is not read by plain OMP")
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                report = self.check(role, project, home, env)
                self.assertEqual((report["state"], report["ok"], report["leaks"]), ("warning", True, []), report)
                self.assertTrue(any("PERSONALITY.md" in item for item in report["warnings"]), report["warnings"])
                result = self.isolated(role, project, home, env)
                self.clean(result)
                self.assertIn("# Personality", live.prompt_text(result), "the default personality block was removed")
                self.assertNotIn("personality", " ".join(result["command"]).lower())

    # -- title
    def test_title_system_md_is_not_a_leak_with_no_title(self):
        canary = f"{CANARY}_TITLE"
        home = self.home(TITLE_SYSTEM__md=canary + "\n")
        project = self.project()
        for name in PROJECT_DIRS:
            write(project / name / "TITLE_SYSTEM.md", canary + "\n")
        env = self.env(home)
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                result = self.isolated(role, project, home, env)
                self.clean(result)
                self.assertIn("--no-title", result["command"])
                self.assertNotIn(canary, live.surface(result))
                report = self.check(role, project, home, env)
                self.assertEqual((report["state"], report["ok"], report["leaks"], report["error"]), ("ok", True, [], None),
                                 report)

    # -- dev.autoqa
    def test_autoqa_is_off_and_pi_auto_qa_is_stripped(self):
        home, project = self.home(), self.project()
        env = self.env(home)
        control = self.control(project, env)
        self.assertIn("xd://report_issue", live.surface(control), "control invalid: plain OMP has no auto-QA tool here")
        for label, environment in (("plain env", env), ("PI_AUTO_QA=1", {**env, "PI_AUTO_QA": "1"}),
                                   ("PI_AUTO_QA=true", {**env, "PI_AUTO_QA": "true"})):
            for role in ("manager", "worker"):
                with self.subTest(env=label, role=role):
                    result = self.isolated(role, project, home, environment)
                    self.clean(result)
                    self.assertNotIn("xd://report_issue", live.surface(result), "the auto-QA tool is still offered")
                    self.assertNotIn("PI_AUTO_QA", result["env"])

    # -- R4: disabledAgents
    def test_disabled_agents_equal_what_omp_really_loads_and_union_the_users_value(self):
        home = self.home(config__yml="task:\n  disabledAgents:\n    - sonic\n")
        parent = self.root / f"par{self.counter}"
        project = parent / "proj"
        project.mkdir(parents=True)
        good = "---\nname: {n}\ndescription: {d}\n---\nbody\n"
        agents = project / ".omp" / "agents"
        write(agents / "plain.md", good.format(n="canary-plain", d="plain"))
        write(agents / "quoted.md", '---\nname: "canary-quoted"\ndescription: "a: b"\n---\n')
        write(agents / "comment.md", "---\nname: canary-comment # tail\ndescription: d # tail\n---\n")
        write(agents / "folded.md", "---\nname: canary-folded\ndescription: >\n  folded\n  text\n---\n")
        write(agents / "nodesc.md", "---\nname: canary-nodesc\n---\nbody\n")
        write(agents / "noname.md", "---\ndescription: only\n---\nbody\n")
        write(agents / "stem-only.md", "no frontmatter\n")
        write(agents / "reserved.md", good.format(n="main", d="d"))
        write(parent / ".omp" / "agents" / "far.md", good.format(n="canary-far", d="far"))  # shadowed by the nearer dir
        write(project / ".claude" / "agents" / "cl.md", good.format(n="canary-claude", d="cl"))
        write(home / ".omp" / "agent" / "agents" / "u.md", good.format(n="canary-user", d="user"))
        env = self.env(home)
        control = self.control(project, env)
        self.clean(control)
        loaded = listed_agents(control)
        self.assertNotIn("sonic", loaded, "control invalid: the user's own disabledAgents value is not effective")
        planted = {n for n in loaded if n.startswith("canary-")}
        self.assertIn("canary-plain", planted, "control invalid: OMP does not load the planted project agents")
        self.assertNotIn("canary-nodesc", planted)  # OMP drops a definition without a description
        self.assertNotIn("canary-far", planted, "OMP reads only the nearest .omp/agents")
        self.assertNotIn("canary-claude", planted, "the .claude/agents dir is not a task-agent source here")
        expected = set(launcher.task_agent_names(project, home))
        self.assertEqual(expected, planted, f"task_agent_names != what OMP loads: {expected} vs {planted}")
        self.assertEqual({"canary-plain", "canary-quoted", "canary-comment", "canary-folded", "canary-user"}, expected)
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                result = self.isolated(role, project, home, env)
                self.clean(result)
                after = listed_agents(result)
                self.assertFalse({n for n in after if n.startswith("canary-")}, f"ambient agents survived: {after}")
                self.assertNotIn("sonic", after, "the user's own disabled agent was re-enabled")
                self.assertEqual(after, BUNDLED - {"sonic"}, after)
                report = self.check(role, project, home, env)
                self.assertEqual((report["state"], report["leaks"]), ("ok", []), report)

    def test_an_invalid_agent_file_does_not_disable_the_bundled_agent_of_the_same_name(self):
        home, project = self.home(), self.project()
        write(project / ".omp" / "agents" / "reviewer.md", "---\nname: reviewer\n---\nno description: not loaded\n")
        env = self.env(home)
        control = self.control(project, env)
        self.assertIn("reviewer", listed_agents(control))
        result = self.isolated("worker", project, home, env)
        self.clean(result)
        self.assertIn("reviewer", listed_agents(result), "a definition OMP does not load must not switch a bundled agent off")

    # -- R5: real start entrypoint
    def test_real_start_prints_the_isolation_state_and_shuts_down_clean(self):
        marker = f"{CANARY}_START_PERSONALITY"
        home, project = self.home(PERSONALITY__md=marker + "\n"), self.project()
        write(project / ".claude" / "APPEND_SYSTEM.md", f"{CANARY}_START_APPEND\n")
        data = self.root / "startdata"
        tmp = self.root / "starttmp"
        tmp.mkdir()
        env = {**self.env(home, PI_AUTO_QA="1"), "PYTHONPATH": str(Path(launcher.__file__).parents[2]),
               "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp), "TERM": "xterm-256color"}

        def cli(*args, timeout=180):
            return subprocess.run([sys.executable, "-c", MAIN, *args], env=env, cwd=project, capture_output=True,
                                  text=True, timeout=timeout, stdin=subprocess.DEVNULL)

        try:
            out = cli("start", "--data-dir", str(data), "--omp", self.omp, "--no-attach", "--timeout", "120")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertRegex(out.stdout, r"omp isolation: warning", out.stdout)
            self.assertRegex(out.stdout, r"WARNING:.*PERSONALITY\.md", out.stdout)
            self.assertNotRegex(out.stdout, r"omp isolation: (pending|ok|leak|failed)")
            status = cli("status", "--data-dir", str(data), "--json")
            snapshot = json.loads(status.stdout)["snapshot"]
            iso = snapshot["omp_isolation"]
            self.assertEqual((iso["state"], iso["ok"], iso["leaks"]), ("warning", True, []), iso)
            self.assertIn("PERSONALITY.md", json.dumps(iso["warnings"]))
            # the panes are real OMP processes: PI_AUTO_QA must not be in their environment
            seen = 0
            for pid, ticks in live.naming_identities(self.root).items():
                try:
                    blob = Path(f"/proc/{pid}/environ").read_bytes()
                except OSError:
                    continue
                if b"WORKBENCH_G3_ROLE=" in blob:
                    seen += 1
                    self.assertNotIn(b"PI_AUTO_QA=", blob, "PI_AUTO_QA reached a Workbench OMP pane")
            self.assertGreaterEqual(seen, 2, "the two OMP panes were not found")
        finally:
            cli("shutdown", "--data-dir", str(data), "--yes", "--json", timeout=90)
            end = time.monotonic() + 15
            while time.monotonic() < end and live.alive(live.naming_identities(self.root)):
                time.sleep(0.2)
        self.assertEqual(live.alive(live.naming_identities(self.root)), [], "processes of the started backend survived")

    def test_zzz_nothing_ambient_changed_and_no_leftover_process(self):
        after = live.ambient_view()
        for key, value in self.ambient_before.items():
            self.assertEqual(after[key], value, f"ambient user configuration changed: {key}")
        self.assertEqual(live.alive(live.naming_identities(self.root)), [])


if __name__ == "__main__":
    unittest.main()
