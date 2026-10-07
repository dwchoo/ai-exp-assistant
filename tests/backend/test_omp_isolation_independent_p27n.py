"""Independent C-D59 isolation-correction tests (p27-cd59-test-02, unit V-CW-17-p2.7-isolation).

Expectations come from the review findings R1/R3/R4/R5 (p27-cd59-review-01), Root's adjudication and the user's answers
(personality: keep OMP default + warn; ``--no-title``), not from the fixes:

- R1: an ambient ``APPEND_SYSTEM.md`` (``<cwd>/{.omp,.claude,.codex,.gemini}`` or ``~/.omp/agent``) must not reach the system
  prompt: ``--append-system-prompt ""`` is a REQUIRED isolation argument (with a slot for CW-18's role prompt), and if such a
  file's content DOES show up in the prompt the check reports a leak instead of ``ok``.
- R3 + user answer: ``PERSONALITY.md`` is not blocked (no ``personality: none``, OMP's default Personality block stays) but the
  check raises a WARNING (state ``warning``, still ``ok=True``, no leak) that the status/start summary shows.
- ``TITLE_SYSTEM.md`` is no leak while ``--no-title`` is part of the command; ``--no-title`` is a REQUIRED isolation argument.
- dev.autoqa is off (static overlay) and ``PI_AUTO_QA`` never reaches the OMP children (pane, check).
- R4: ``task.disabledAgents`` lists only what OMP really loads (nearest ``.omp/agents`` + ``~/.omp/agent/agents``,
  frontmatter name AND description, not main/sub), unioned with the user's own value; the role overlay never widens or hides
  the Workbench skills through the user's skill lists.
- R5: ``start`` prints the isolation result (waits for it) instead of "pending"; a warning/leak is shown in the summary.

No model call anywhere: fake OMP scripts only (the live counterpart is ``live_omp_isolation_independent_p27n.py``).

C-D64 adaptation (p27-home-test-01): R3's PERSONALITY warning is superseded. With the Workbench-owned OMP home OMP reads
PERSONALITY.md only from the Workbench agent dir, so the user's ``~/.omp/agent/PERSONALITY.md`` is not read at all: it must
be neither a leak nor a warning, and the summary must not mention it. The entrypoint fake HOME carries a fake (non-credential)
``agent.db`` (see p27m).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))

import test_omp_isolation_independent_p27m as base  # noqa: E402
from workbench.backend import launcher  # noqa: E402

REPO = base.REPO
STATIC_OVERLAY = base.STATIC_OVERLAY
PROJECT_DIRS = (".omp", ".claude", ".codex", ".gemini")


def read_yaml(path: Path) -> dict:
    return base.parse_yaml_subset(path.read_text())


# ------------------------------------------------------------------------------------------ argv / env / overlay
class RequiredArgsAndEnvTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p27n-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def command(self, role="manager", args=(), append=None):
        overlay = launcher.write_role_overlay(self.root, role, launcher.role_overlay(
            role, project_dir=self.root, home=self.root))
        plan = base.plan_for(args=args)
        return launcher.omp_command(plan, overlay) if append is None else launcher.omp_command(plan, overlay, append)

    def test_append_system_prompt_is_empty_and_no_title_is_present_for_both_roles(self):
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                command = self.command(role)
                self.assertEqual(command.count("--append-system-prompt"), 1, command)
                self.assertEqual(command[command.index("--append-system-prompt") + 1], "", command)
                self.assertEqual(command.count("--no-title"), 1, command)
                self.assertGreater(command.index("--append-system-prompt"), command.index("--no-extensions"))
                self.assertEqual(command.count("--extension"), 1)
                for forbidden in ("--profile", "--no-skills", "--system-prompt", "--system-prompt-template"):
                    self.assertNotIn(forbidden, command)

    def test_the_append_slot_carries_a_role_prompt_without_adding_a_second_flag(self):
        command = self.command("worker", append="ROLE PROMPT for CW-18")
        self.assertEqual(command.count("--append-system-prompt"), 1)
        self.assertEqual(command[command.index("--append-system-prompt") + 1], "ROLE PROMPT for CW-18")

    def test_user_arguments_still_follow_every_isolation_argument(self):
        command = self.command("worker", args=("--thinking", "high"))
        last = max(command.index("--no-title"), command.index("--append-system-prompt") + 1, command.index("--no-extensions"))
        self.assertGreater(command.index("--thinking"), last)
        self.assertEqual(command[command.index("--thinking"):command.index("--thinking") + 2], ["--thinking", "high"])

    def test_static_overlay_turns_autoqa_off_and_does_not_touch_personality(self):
        data = read_yaml(STATIC_OVERLAY)
        self.assertIs(data.get("dev", {}).get("autoqa"), False)
        flat = dict(base.flatten(data))
        self.assertNotIn("personality", flat, "personality must stay at OMP's default (user answer)")
        self.assertFalse([k for k in flat if k.lower().startswith("personality")])
        for role in ("manager", "worker"):
            overlay = launcher.role_overlay(role, project_dir=self.root, home=self.root)
            self.assertNotIn("personality", json.dumps(overlay).lower(), "the role overlay must not set a personality")

    def test_pi_auto_qa_is_stripped_from_pane_and_check_environments_only_that_key(self):
        environment = {"PATH": "/usr/bin", "HOME": "/home/u", "PI_AUTO_QA": "1", "OPENAI_API_KEY": "sk-p27n-sentinel",
                       "PI_OTHER_P27N": "keep-me"}
        plan = base.plan_for()
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                pane = launcher.omp_environment(environment, plan, role=role, token="t", bridge_socket=Path("/x/b.sock"))
                check = launcher.isolation_check_environment(environment, plan, role=role, absent_socket=Path("/x/a.sock"))
                for env in (pane, check):
                    self.assertNotIn("PI_AUTO_QA", env)
                    self.assertEqual(env["OPENAI_API_KEY"], "sk-p27n-sentinel")
                    self.assertEqual(env["PI_OTHER_P27N"], "keep-me")
                    self.assertEqual(env["HOME"], "/home/u")
        self.assertEqual(environment["PI_AUTO_QA"], "1", "the caller's mapping must not be edited")
        for value in ("0", "true", ""):
            env = launcher.omp_environment({**environment, "PI_AUTO_QA": value}, plan, role="worker", token="t",
                                           bridge_socket=Path("/x/b.sock"))
            self.assertNotIn("PI_AUTO_QA", env)


class DisabledAgentsRulesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p27n-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.parent = self.root / "parent"
        self.project = self.parent / "proj"
        self.user_agents = self.home / ".omp" / "agent" / "agents"
        self.nearest = self.project / ".omp" / "agents"
        for path in (self.user_agents, self.nearest, self.parent / ".omp" / "agents", self.project / ".claude" / "agents",
                     self.project / ".codex" / "agents"):
            path.mkdir(parents=True)

    def write(self, directory: Path, filename: str, text: str) -> None:
        (directory / filename).write_text(text)

    def agents(self, **kwargs) -> list[str]:
        # C-D68: each role's overlay also disables a fixed set (manager: the Workbench worker agents, worker: OMP's
        # bundled agents); ``base.ambient_disabled`` asserts it is there in full and returns the ambient part.
        role = kwargs.pop("role", "manager")
        return base.ambient_disabled(launcher.role_overlay(role, project_dir=self.project, home=self.home, **kwargs),
                                     role)

    def test_only_the_nearest_project_dir_the_user_dir_and_valid_definitions_count(self):
        good = "---\nname: {n}\ndescription: some description\n---\nbody\n"
        self.write(self.nearest, "a.md", good.format(n="near-agent"))
        self.write(self.nearest, "no-description.md", "---\nname: nodesc-agent\n---\nbody\n")
        self.write(self.nearest, "no-name.md", "---\ndescription: only a description\n---\nbody\n")
        self.write(self.nearest, "no-frontmatter.md", "just text, stem no-frontmatter\n")
        self.write(self.nearest, "reserved-main.md", good.format(n="main"))
        self.write(self.nearest, "reserved-sub.md", good.format(n="sub"))
        self.write(self.nearest, "not-md.txt", good.format(n="txt-agent"))
        self.write(self.parent / ".omp" / "agents", "far.md", good.format(n="far-agent"))  # shadowed by the nearer dir
        self.write(self.project / ".claude" / "agents", "cl.md", good.format(n="claude-agent"))
        self.write(self.project / ".codex" / "agents", "cx.md", good.format(n="codex-agent"))
        self.write(self.user_agents, "u.md", good.format(n="user-agent"))
        self.write(self.home / ".omp" / "agent", "agents-file-not-dir.md", good.format(n="stray-agent"))
        for role in ("manager", "worker"):
            self.assertEqual(set(self.agents(role=role)), {"near-agent", "user-agent"}, role)

    def test_without_a_project_dir_the_nearest_ancestor_dir_is_read(self):
        shutil.rmtree(self.nearest)
        self.write(self.parent / ".omp" / "agents", "far.md", "---\nname: far-agent\ndescription: d\n---\n")
        self.assertEqual(set(self.agents()), {"far-agent"})

    def test_frontmatter_is_read_like_yaml_not_by_a_naive_prefix_match(self):
        cases = {
            "quoted.md": ('---\nname: "quoted-agent"\ndescription: "a: b"\n---\n', "quoted-agent"),
            "single.md": ("---\nname: 'single-agent'\ndescription: 'x'\n---\n", "single-agent"),
            "comment.md": ("---\nname: comment-agent # not part of the name\ndescription: d # neither\n---\n",
                           "comment-agent"),
            "folded.md": ("---\nname: folded-agent\ndescription: >\n  folded text\n  continues\n---\n", "folded-agent"),
            "literal.md": ("---\nname: literal-agent\ndescription: |\n  literal text\n---\n", "literal-agent"),
            "crlf.md": ("---\r\nname: crlf-agent\r\ndescription: d\r\n---\r\nbody\r\n", "crlf-agent"),
        }
        for filename, (text, _name) in cases.items():
            self.write(self.nearest, filename, text)
        self.write(self.nearest, "empty-description.md", "---\nname: empty-desc-agent\ndescription:\n---\n")
        self.write(self.nearest, "commented-out.md", "---\n# name: hidden-agent\nname: shown-agent\ndescription: d\n---\n")
        names = set(self.agents())
        self.assertEqual(names, {n for _t, n in cases.values()} | {"shown-agent"}, names)
        self.assertNotIn("hidden-agent", names)
        self.assertNotIn("empty-desc-agent", names)

    def test_the_users_own_disabled_agents_are_unioned_and_never_dropped(self):
        self.write(self.nearest, "a.md", "---\nname: near-agent\ndescription: d\n---\n")
        self.write(self.user_agents, "u.md", "---\nname: user-agent\ndescription: d\n---\n")
        agents = self.agents(user_disabled_agents=["sonic", "near-agent", "user-mute", "sonic"])
        self.assertLessEqual({"sonic", "user-mute", "near-agent", "user-agent"}, set(agents))
        self.assertEqual(len(agents), len(set(agents)), "duplicates in task.disabledAgents")
        self.assertEqual(set(agents), {"sonic", "user-mute", "near-agent", "user-agent"})
        self.assertEqual(self.agents(), ["near-agent", "user-agent"], "no user value: only what OMP loads")

    def test_a_user_value_survives_even_when_no_definition_exists(self):
        shutil.rmtree(self.nearest)
        shutil.rmtree(self.user_agents)
        self.assertEqual(self.agents(user_disabled_agents=["sonic"]), ["sonic"])

    def test_the_overlay_pins_the_workbench_skill_filters_against_the_users_lists(self):
        for role, patterns in (("manager", ("order-manager",)), ("worker", ("order-worker",))):
            overlay = launcher.role_overlay(role, project_dir=self.project, home=self.home,
                                            role_skills={"manager": ("order-manager",), "worker": ("order-worker",)})
            self.assertEqual(overlay["skills"]["includeSkills"], list(patterns))
            self.assertEqual(overlay["skills"]["ignoredSkills"], [], "the user's ignoredSkills must not hide Workbench skills")
        # CW-18: without an explicit filter each role gets exactly its own Workbench skills (C-D70 (5): the manager
        # also workbench-recovery; p27-cd70-test-01 edit).
        for role, own in (("manager", ["to-worker", "workbench-recovery"]), ("worker", ["to-manager"])):
            default = launcher.role_overlay(role, project_dir=self.project, home=self.home)
            self.assertEqual(default["skills"]["includeSkills"], own)
            self.assertEqual(default["skills"]["ignoredSkills"], [])

    def test_no_definition_body_or_description_leaks_into_the_overlay_or_the_file(self):
        self.write(self.nearest, "a.md", "---\nname: near-agent\ndescription: DESC-p27n-secret\n---\nBODY-p27n-secret\n")
        overlay = launcher.role_overlay("worker", project_dir=self.project, home=self.home)
        blob = json.dumps(overlay)
        self.assertNotIn("p27n-secret", blob)
        path = launcher.write_role_overlay(self.root, "worker", overlay)
        self.assertNotIn("p27n-secret", path.read_text())


# --------------------------------------------------------------------------------------- check with a fake OMP
class PromptFileCheckTests(unittest.TestCase):
    """``check_isolation`` against a fake OMP that returns a chosen system prompt, files planted in a fake HOME/project."""

    def setUp(self):
        self.h = base.Harness(self)
        self.home = self.h.root / "home"
        self.project = self.h.root / "proj"
        (self.home / ".omp" / "agent").mkdir(parents=True)
        self.project.mkdir()
        self.env = {**self.h.env, "HOME": str(self.home)}
        self.data = self.h.root / "data"
        self.data.mkdir()

    def check(self, *, role="worker", command_extra=None):
        overlay = launcher.write_role_overlay(self.data, role, launcher.role_overlay(
            role, project_dir=self.project, home=self.home))
        plan = base.plan_for(omp=str(self.h.script), args=tuple(command_extra or ()))
        command = launcher.omp_command(plan, overlay)
        return launcher.check_isolation(command, cwd=self.project, environment=self.env, role=role,
                                        allowed_skills=(), omp_version="omp/18.4.4", timeout=15.0)

    def plant(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_clean_state_is_ok(self):
        result = self.check()
        self.assertEqual((result["ok"], result["state"], result["leaks"]), (True, "ok", []), result)
        self.assertFalse(result.get("warnings"))

    def test_personality_file_is_a_warning_not_a_leak_and_not_ok_false(self):
        # C-D64: the user's PERSONALITY.md is not read any more -> neither a warning nor a leak (state ok)
        path = self.home / ".omp" / "agent" / "PERSONALITY.md"
        self.plant(path, "Talk like a pirate p27n\n")
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                result = self.check(role=role)
                self.assertEqual((result["state"], result["ok"], result["leaks"]), ("ok", True, []), result)
                self.assertNotIn("PERSONALITY", json.dumps(result.get("warnings")))
                self.assertNotIn("personality", json.dumps(result.get("warnings")).lower())
                self.assertNotIn(str(path), json.dumps(result.get("observed")), "the user's PERSONALITY.md was looked at")
        summary = launcher.summarize_isolation({"manager": self.check(role="manager"), "worker": self.check()}, "omp/18.4.4")
        self.assertEqual((summary["state"], summary["ok"], summary["leaks"], summary["warnings"]), ("ok", True, [], []))
        self.assertNotIn("PERSONALITY.md", summary.get("warning") or "")

    def test_default_personality_is_never_replaced_by_the_launcher(self):
        # the launcher must not drop the default prompt: no --system-prompt*, no personality setting anywhere
        self.h.set_mode()
        result = self.check()
        self.assertTrue(result["ok"], result)
        argv = next(e for e in self.h.records() if e["event"] == "start")["argv"]
        for flag in ("--system-prompt", "--system-prompt-template"):
            self.assertNotIn(flag, argv)
        self.assertNotIn("personality", " ".join(argv).lower())

    def test_append_system_md_that_reaches_the_prompt_is_a_leak_in_every_location(self):
        canary = "CANARY_APPEND_P27N_LINE"
        places = [self.project / name / "APPEND_SYSTEM.md" for name in PROJECT_DIRS]
        places.append(self.home / ".omp" / "agent" / "APPEND_SYSTEM.md")
        self.h.set_mode(shapes={"prompt_extra": f"\n## Extra\n{canary}\n"})
        for place in places:
            with self.subTest(place=str(place.relative_to(self.h.root))):
                self.plant(place, f"{canary}\n")
                result = self.check()
                self.assertEqual((result["ok"], result["state"]), (False, "leak"), result)
                self.assertIn("APPEND_SYSTEM.md", json.dumps(result["leaks"]), "the leak must name the file")
                place.unlink()

    def test_append_system_md_that_is_blocked_by_the_empty_arg_is_no_leak(self):
        # the fake OMP obeys ``--append-system-prompt ""``: the prompt does not contain the file, so no leak
        for name in PROJECT_DIRS:
            self.plant(self.project / name / "APPEND_SYSTEM.md", "CANARY_NOT_INJECTED_P27N\n")
        self.plant(self.home / ".omp" / "agent" / "APPEND_SYSTEM.md", "CANARY_NOT_INJECTED_P27N_USER\n")
        result = self.check()
        self.assertEqual((result["ok"], result["state"], result["leaks"]), (True, "ok", []), result)

    def test_title_system_md_is_no_leak_with_no_title_in_every_location(self):
        for name in PROJECT_DIRS:
            self.plant(self.project / name / "TITLE_SYSTEM.md", "TITLE CANARY p27n\n")
        self.plant(self.home / ".omp" / "agent" / "TITLE_SYSTEM.md", "TITLE CANARY user p27n\n")
        result = self.check()
        self.assertEqual((result["ok"], result["state"], result["leaks"]), (True, "ok", []), result)
        start = next(e for e in self.h.records() if e["event"] == "start")
        self.assertIn("--no-title", start["argv"])

    def test_a_leak_outranks_a_personality_warning(self):  # C-D64: PERSONALITY.md no longer warns; the leak still wins
        self.plant(self.home / ".omp" / "agent" / "PERSONALITY.md", "pirate\n")
        self.plant(self.project / ".claude" / "APPEND_SYSTEM.md", "CANARY_BOTH_P27N\n")
        self.h.set_mode(shapes={"prompt_extra": "CANARY_BOTH_P27N\n"})
        result = self.check()
        self.assertEqual((result["state"], result["ok"]), ("leak", False), result)
        self.assertTrue(result["leaks"])

    def test_the_check_command_is_the_pane_command_with_the_required_args(self):
        self.check()
        start = next(e for e in self.h.records() if e["event"] == "start")
        argv = start["argv"]
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "")
        self.assertEqual(argv.count("--append-system-prompt"), 1)
        self.assertIn("--no-title", argv)


# --------------------------------------------------------------------------- real entrypoint: start summary (R5)
class RealEntrypointSummaryTests(base.RealEntrypointBase):
    """The stub OMP never joins the bridge, so the first ``start`` times out while the backend keeps running; a second
    ``start --no-attach`` then takes the "already running" path of the real ``cmd_start``, which waits (bounded) for the
    isolation check and prints the summary. (Start against a really-ready backend: the live file.)"""

    def summary(self, *extra, env_extra=None):
        self.start(*extra, env_extra=env_extra)
        self.wait_isolation()
        out = self.cli("start", "--data-dir", str(self.data), "--omp", str(self.root / "omp"), "--no-attach",
                       "--timeout", "8", env_extra=env_extra)
        self.seen.update(base.procs_naming(self.root))
        return out

    def personality(self) -> Path:
        path = self.home / ".omp" / "agent" / "PERSONALITY.md"
        path.write_text("Speak like a pirate p27n\n")
        return path

    def test_start_prints_ok_not_pending_for_a_clean_omp(self):
        out = self.summary()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertRegex(out.stdout, r"omp isolation: ok", out.stdout)
        self.assertNotRegex(out.stdout, r"omp isolation: pending")
        self.assertNotIn("WARNING", out.stdout)

    def test_start_prints_the_personality_warning_through_the_real_entrypoint(self):
        # C-D64: the user's PERSONALITY.md is not read any more -> the real entrypoint prints ok and never mentions it
        path = self.personality()
        out = self.summary()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertRegex(out.stdout, r"omp isolation: ok", out.stdout)
        self.assertNotIn("PERSONALITY", out.stdout)
        self.assertNotIn(str(path), out.stdout)
        self.assertNotIn("WARNING", out.stdout)
        snapshot = self.wait_isolation()
        iso = snapshot["omp_isolation"]
        self.assertEqual((iso["state"], iso["ok"], iso["leaks"], iso["warnings"]), ("ok", True, [], []))
        self.assertNotIn("PERSONALITY", json.dumps(iso))
        text = self.cli("status", "--data-dir", str(self.data)).stdout
        self.assertRegex(text, r"omp isolation: ok")
        self.assertNotIn("PERSONALITY.md", text)
        # the default OMP personality is not removed by the Workbench: no personality option anywhere in the panes' argv
        for role in ("manager", "worker"):
            for call in self.by_kind("pane", role) + self.by_kind("rpc", role):
                self.assertNotIn("personality", " ".join(call["argv"]).lower())
                configs = [call["argv"][i + 1] for i, a in enumerate(call["argv"]) if a == "--config"]
                for config in configs:  # settings, not comments: the static file's comments may talk about it
                    keys = []
                    if Path(config).exists():
                        text = Path(config).read_text()
                        try:
                            keys = [k for k, _v in base.flatten(json.loads(text))]  # per-role overlay is JSON
                        except ValueError:
                            keys = [k for k, _v in base.flatten(base.parse_yaml_subset(text))]
                    self.assertFalse([k for k in keys if "personality" in k.lower()], (config, keys))

    def test_start_prints_a_leak_when_an_append_file_reaches_the_prompt(self):
        canary = "CANARY_APPEND_ENTRY_P27N"
        (self.project / ".claude").mkdir(exist_ok=True)
        (self.project / ".claude" / "APPEND_SYSTEM.md").write_text(canary + "\n")
        self.set_mode(shapes={"prompt_extra": canary + "\n"})
        out = self.summary()
        self.assertEqual(out.returncode, 0, "the session stays up on a leak: " + out.stdout + out.stderr)
        self.assertRegex(out.stdout, r"omp isolation: leak", out.stdout)
        self.assertRegex(out.stdout, r"WARNING:.*APPEND_SYSTEM\.md", out.stdout)
        snapshot = self.wait_isolation()
        self.assertEqual((snapshot["omp_isolation"]["state"], snapshot["omp_isolation"]["ok"]), ("leak", False))

    def test_an_append_file_that_did_not_reach_the_prompt_is_not_reported(self):
        (self.project / ".claude").mkdir(exist_ok=True)
        (self.project / ".claude" / "APPEND_SYSTEM.md").write_text("CANARY_BLOCKED_P27N\n")
        out = self.summary()
        self.assertRegex(out.stdout, r"omp isolation: ok", out.stdout)
        self.assertNotIn("WARNING", out.stdout)

    def test_the_pane_and_check_commands_carry_the_required_args_and_no_autoqa_env(self):
        self.start("--omp-arg=--flag-from-cli", env_extra={"PI_AUTO_QA": "1"})
        self.wait_isolation()
        for role in ("manager", "worker"):
            for call in self.by_kind("pane", role) + self.by_kind("rpc", role):
                argv = call["argv"]
                self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "", argv)
                self.assertIn("--no-title", argv)
                self.assertGreater(argv.index("--flag-from-cli"), argv.index("--no-title"))
        # the stub records selected env keys only; PI_AUTO_QA is not recorded, so use the process environment of a live
        # pane through /proc while it runs (start leaves the two panes and the checks' stubs up until shutdown)
        seen = 0
        for pid in [c["pid"] for c in self.by_kind("pane")]:
            try:
                blob = Path(f"/proc/{pid}/environ").read_bytes()
            except OSError:
                continue
            seen += 1
            self.assertNotIn(b"PI_AUTO_QA=", blob, "PI_AUTO_QA reached a Workbench OMP pane")
        self.assertGreaterEqual(seen, 1, "no live pane environment could be inspected")


if __name__ == "__main__":
    unittest.main()
