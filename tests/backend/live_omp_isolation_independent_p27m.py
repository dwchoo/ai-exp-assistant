"""Independent LIVE C-D59 isolation check (p27-cd59-test-01, unit V-CW-17-p2.7-isolation), real OMP, ZERO model calls.

Runs only with ``WB_LIVE_OMP=1`` (``PYTHONPATH=src python -m unittest discover -s tests/backend -p
live_omp_isolation_independent_p27m.py``).

Expectations derive from C-D59 (written before launcher.py was read): the two OMP processes the Workbench launches must not
take user/global/parent-directory ambient configuration into the system prompt or the tools -- user and project skills
(``~/.agents/skills`` orca skills, ``.claude/skills``, ``.agents/skills``, ``.omp/skills``), AGENTS.md / CLAUDE.md context
files, rules, SYSTEM.md, auto-discovered extensions / MCP project config / task subagent definitions / slash commands --
while the Workbench bridge extension, the user's own auth / provider / model settings stay, nothing is copied and no global
config is edited (per-run options and overlays only). A skill in the Workbench skills directory IS visible, and role
specific skill filtering works. User ``--omp-arg`` values still apply.

This module does NOT use the implementation's own detector (``check_isolation`` / ``observe_isolation``) as its oracle:
it speaks the RPC protocol itself and greps the *raw* system prompt, tool descriptions and command list for unique canary
tokens; the production ``check_isolation`` is only cross-checked at the end. Only ``get_state`` and
``get_available_commands`` are sent (never a prompt), ``--no-session``; every process group is stopped by exact identity
(pid + start time, via pidfd) and any survivor is a failure. Credential stores are never opened; ambient user dirs are only
listed / stat-ed before and after to prove nothing changed.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from workbench.backend import launcher

LIVE = os.environ.get("WB_LIVE_OMP") == "1"
HOME = Path(os.environ.get("HOME") or Path.home())
CANARY = "CANARY_P27M"
RPC_ARGS = ["--mode", "rpc", "--no-session", "--no-title"]
CAPABILITY_PROVIDER_IDS = ("native", "omp-managed", "skillshare", "agents-md", "agents", "claude-md", "claude-plugins",
                           "claude", "cline", "codex", "cursor", "gemini", "github", "mcp-json", "omp-plugins",
                           "opencode", "ssh-json", "vscode", "windsurf", "agent-plugins", "builtin-defaults")

MCP_STUB = r'''import json, sys
open(sys.argv[1], "a").write("launched\n")
for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method, ident = message.get("method"), message.get("id")
    if method == "initialize":
        result = {"protocolVersion": message["params"].get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "canary", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "canary_tool", "description": "CANARY_P27M_MCP_TOOL",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif ident is None:
        continue
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
'''


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def skill_text(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{CANARY}_SKILL_BODY_{name}\n"


# ----------------------------------------------------------------------------------------------- owned processes
def start_ticks(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if fields[0] in "ZX" else fields[19]


def group_identities(pgid: int) -> dict[int, str]:
    found = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            fields = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] not in "ZX" and fields[2] == str(pgid):
            found[int(entry)] = fields[19]
    return found


def naming_identities(root: Path) -> dict[int, str]:
    needle, found = str(root).encode(), {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            blob = Path(f"/proc/{entry}/cmdline").read_bytes() + Path(f"/proc/{entry}/environ").read_bytes()
        except OSError:
            continue
        if needle in blob and (ticks := start_ticks(int(entry))):
            found[int(entry)] = ticks
    return found


def kill_identity(pid: int, ticks: str, sig: int) -> None:
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if start_ticks(pid) == ticks:  # exact identity: the pid was not reused
            signal.pidfd_send_signal(fd, sig)
    except OSError:
        pass
    finally:
        os.close(fd)


def alive(members: dict[int, str]) -> list[int]:
    return [pid for pid, ticks in members.items() if start_ticks(pid) == ticks]


def rpc_exchange(argv: list[str], cwd: Path, env: dict[str, str], root: Path, *, settle: float = 0.0,
                 timeout: float = 45.0) -> dict:
    """Send get_state + get_available_commands (nothing else), return the raw data; stop what we started."""
    sent: list[str] = []
    members: dict[int, str] = {}
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                start_new_session=True, close_fds=True)
        members[proc.pid] = start_ticks(proc.pid) or ""
        result: dict = {"argv": argv}
        try:
            deadline = time.monotonic() + timeout
            buffer, responses = b"", {}
            for request_id, kind in (("p27m-state", "get_state"), ("p27m-commands", "get_available_commands")):
                proc.stdin.write(json.dumps({"id": request_id, "type": kind}).encode() + b"\n")
                proc.stdin.flush()
                sent.append(kind)
                while request_id not in responses:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError(f"no answer to {kind}")
                    if not select.select([proc.stdout], [], [], min(left, 0.2))[0]:
                        if proc.poll() is not None:
                            raise RuntimeError(f"omp exited early: {proc.returncode}")
                        continue
                    chunk = os.read(proc.stdout.fileno(), 1 << 16)
                    if not chunk:
                        raise RuntimeError("omp closed stdout")
                    buffer += chunk
                    *lines, buffer = buffer.split(b"\n")
                    for line in lines:
                        try:
                            frame = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(frame, dict) and frame.get("type") == "response" and frame.get("id") in (
                                "p27m-state", "p27m-commands"):
                            if not frame.get("success"):
                                raise RuntimeError(f"{frame.get('command')} failed: {frame.get('error')}")
                            responses[frame["id"]] = frame.get("data") or {}
            time.sleep(settle)  # late side effects (MCP servers, extensions) get a moment to show up
            members.update(group_identities(proc.pid))
            members.update(naming_identities(root))
            result.update(state=responses["p27m-state"], commands=responses["p27m-commands"].get("commands") or [])
        finally:
            members.update(group_identities(proc.pid))
            for pid, ticks in members.items():
                kill_identity(pid, ticks, signal.SIGTERM)
            end = time.monotonic() + 4
            while time.monotonic() < end and alive(members):
                time.sleep(0.05)
            for pid, ticks in members.items():
                kill_identity(pid, ticks, signal.SIGKILL)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
        result["survivors"] = alive(members)
        stderr.seek(0)
        result["stderr"] = stderr.read(1 << 16).decode("utf-8", "replace")
    result["sent"] = sent
    return result


def surface(result: dict) -> str:
    """Everything the model would be handed or offered: prompt, tool definitions, slash commands (raw JSON text)."""
    state = result["state"]
    return json.dumps({"systemPrompt": state.get("systemPrompt"), "dumpTools": state.get("dumpTools"),
                       "commands": result["commands"]}, ensure_ascii=False)


def prompt_text(result: dict) -> str:
    prompt = result["state"].get("systemPrompt") or ""
    return "\n".join(str(item) for item in prompt) if isinstance(prompt, list) else str(prompt)


def skill_visible(name: str, result: dict) -> bool:
    """The skill is offered: as a /skill: command or as an entry of the prompt's skill list / skill:// URL."""
    if f"skill:{name}" in {c.get("name") for c in result["commands"]}:
        return True
    prompt = prompt_text(result)
    return bool(re.search(rf"(?m)^\s*-\s+{re.escape(name)}\s*:", prompt) or f'<skill name="{name}"' in prompt
                or f"skill://{name}" in prompt)


def model_of(result: dict):
    model = result["state"].get("model")
    return None if not isinstance(model, dict) else (model.get("provider"), model.get("id"))


# ----------------------------------------------------------------------------------------- ambient (read-only) view
def stat_tree(path: Path) -> dict[str, tuple[int, int]]:
    """(size, mtime_ns) of every entry below ``path`` -- metadata only, file contents are never opened."""
    out: dict[str, tuple[int, int]] = {}
    if not path.exists():
        return out
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in [*dirs, *files]:
            full = os.path.join(base, name)
            try:
                info = os.lstat(full)
            except OSError:
                continue
            out[os.path.relpath(full, path)] = (info.st_size, info.st_mtime_ns)
    return out


def ambient_view() -> dict:
    view: dict = {}
    for name in (".agents", ".claude/skills", ".codex/skills", ".claude/agents", ".omp/agent/skills",
                 ".omp/agent/rules", ".omp/agent/extensions", ".omp/agent/agents"):
        view[name] = stat_tree(HOME / name)
    for name in (".claude/CLAUDE.md", ".codex/AGENTS.md", ".omp/agent/config.yml", ".omp/agent/AGENTS.md",
                 ".omp/agent/SYSTEM.md", ".omp/agent/mcp.json"):
        try:
            info = os.lstat(HOME / name)
            view[name] = (info.st_size, info.st_mtime_ns)
        except OSError:
            view[name] = None
    # names only (never contents): a stray --profile / new directory shows up as a new name
    for name in (".omp", ".omp/agent"):
        try:
            view[name + "/*"] = sorted(entry for entry in os.listdir(HOME / name))
        except OSError:
            view[name + "/*"] = None
    return view


def ambient_user_skill_names() -> set[str]:
    names = set()
    for base in (HOME / ".agents" / "skills", HOME / ".omp" / "agent" / "skills"):
        try:
            names |= {entry.name for entry in base.iterdir() if (entry / "SKILL.md").is_file()}
        except OSError:
            pass
    return names


@unittest.skipUnless(LIVE, "set WB_LIVE_OMP=1 to run the live OMP isolation check")
class LiveIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.omp = shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
        if not os.access(cls.omp, os.X_OK):
            raise unittest.SkipTest("omp not found")
        cls.version = launcher.omp_version(cls.omp)
        cls._dir = tempfile.TemporaryDirectory(prefix="p27m-")
        cls.root = Path(cls._dir.name)
        cls.ambient_before = ambient_view()
        cls.user_skills = ambient_user_skill_names()
        cls.marker = {name: cls.root / f"marker-{name}" for name in ("ext", "mcp", "ext-sys")}
        # the git root is `parent`, the project a subdirectory: parent-directory files are inside the walk-up
        parent = cls.root / "parent"
        parent.mkdir()
        subprocess.run(["git", "init", "-q", str(parent)], check=True, timeout=30)
        cls.project = parent / "proj"
        cls.project.mkdir()
        write(parent / "CLAUDE.md", f"{CANARY}_PARENT_CLAUDE_MD\n")
        write(cls.project / "AGENTS.md", f"{CANARY}_PROJECT_AGENTS_MD\n")
        write(cls.project / ".claude" / "skills" / "canary-claude-skill" / "SKILL.md",
              skill_text("canary-claude-skill", f"{CANARY}_DESC_CLAUDE_SKILL"))
        write(cls.project / ".agents" / "skills" / "canary-agents-skill" / "SKILL.md",
              skill_text("canary-agents-skill", f"{CANARY}_DESC_AGENTS_SKILL"))
        write(cls.project / ".omp" / "skills" / "canary-omp-skill" / "SKILL.md",
              skill_text("canary-omp-skill", f"{CANARY}_DESC_OMP_SKILL"))
        write(cls.project / ".omp" / "rules" / "canary-always.md",
              f"---\ndescription: canary\nalwaysApply: true\n---\n{CANARY}_RULE_ALWAYS_BODY\n")
        write(cls.project / ".omp" / "rules" / "canary-domain.md",
              f"---\ndescription: {CANARY}_RULE_DOMAIN_DESC\nglobs: [\"*.zz\"]\n---\nbody\n")
        write(cls.project / ".omp" / "agents" / "canary-agent.md",
              f"---\nname: canary-agent\ndescription: {CANARY}_AGENT_DESC\n---\nbody\n")
        write(cls.project / ".claude" / "commands" / "canary-claude-cmd.md", f"{CANARY}_CMD_CLAUDE\n")
        write(cls.project / ".omp" / "commands" / "canary-omp-cmd.md", f"{CANARY}_CMD_OMP\n")
        write(cls.project / ".omp" / "extensions" / "canary-ext" / "index.ts",
              'import * as fs from "node:fs";\n'
              f"export default function canary(_pi: any): void {{ fs.appendFileSync({json.dumps(str(cls.marker['ext']))},"
              ' "x"); }\n')
        write(cls.root / "mcpstub.py", MCP_STUB)
        write(cls.project / ".mcp.json", json.dumps({"mcpServers": {"canary": {
            "command": sys.executable, "args": [str(cls.root / "mcpstub.py"), str(cls.marker["mcp"])]}}}))
        # SYSTEM.md replaces the whole template, so it is probed in a tree of its own
        cls.sys_project = cls.root / "syspar" / "proj"
        cls.sys_project.parent.mkdir()
        subprocess.run(["git", "init", "-q", str(cls.sys_project.parent)], check=True, timeout=30)
        cls.sys_project.mkdir()
        write(cls.sys_project / ".omp" / "SYSTEM.md", f"{CANARY}_SYSTEM_MD_REPLACEMENT\n")
        # Workbench-only skills (scratch copy of the layout omp_bridge/skills has: <name>/SKILL.md)
        cls.wb_skills = cls.root / "wbskills"
        for name in ("wb-both", "wb-manager-only", "wb-worker-only"):
            write(cls.wb_skills / name / "SKILL.md", skill_text(name, f"{CANARY}_WB_DESC_{name}"))
        cls.base_env = {k: v for k, v in os.environ.items() if not k.startswith("WORKBENCH_")}
        cls.plan = launcher.build_plan(cls.base_env, omp=cls.omp, omp_args=("--append-system-prompt",
                                                                            f"{CANARY}_USER_APPEND_ARG"))
        cls.user_dp = launcher.user_disabled_providers(cls.omp, cwd=cls.project, environment=cls.base_env)

    @classmethod
    def tearDownClass(cls):
        leftovers = naming_identities(cls.root)
        for pid, ticks in leftovers.items():
            kill_identity(pid, ticks, signal.SIGKILL)
        cls._dir.cleanup()
        cls.leftover_processes = sorted(leftovers)

    # -- runners
    def control(self, cwd: Path, settle: float = 2.5) -> dict:
        return rpc_exchange([self.omp, *RPC_ARGS], cwd, dict(self.base_env), self.root, settle=settle)

    def wb_command(self, role: str, project: Path, *, skills_dir=None, role_skills=None, plan=None):
        overlay = launcher.write_role_overlay(self.root, role, launcher.role_overlay(
            role, project_dir=project, home=HOME, user_disabled_providers=self.user_dp or (),
            skills_dir=skills_dir or self.wb_skills, role_skills=role_skills))
        plan = plan or self.plan
        command = launcher.omp_command(plan, overlay)
        env = launcher.omp_environment(self.base_env, plan, role=role, token="p27m-token",
                                       bridge_socket=self.root / "absent-bridge.sock")
        return command, env

    def isolated(self, role: str, project: Path, *, settle: float = 2.5, **kwargs) -> dict:
        command, env = self.wb_command(role, project, **kwargs)
        result = rpc_exchange([*command, *RPC_ARGS], project, env, self.root, settle=settle)
        result["command"], result["env"] = command, env
        return result

    def assert_clean_run(self, result: dict) -> None:
        self.assertEqual(result["sent"], ["get_state", "get_available_commands"], "only introspection RPCs allowed")
        self.assertEqual(result["survivors"], [], "an OMP process survived the exact-identity cleanup")

    # -- tests
    def test_00_omp_version_is_the_one_the_evidence_is_about(self):
        # C-D68 (6): the isolation evidence is re-made with the installed OMP 18.6.1
        self.assertEqual(launcher.EVIDENCE_OMP_VERSIONS["isolation"], "18.6.1")
        self.assertIn(launcher.EVIDENCE_OMP_VERSIONS["isolation"], self.version)

    def test_negative_control_shows_every_canary_and_the_orca_skills(self):
        for marker in self.marker.values():
            marker.unlink(missing_ok=True)
        base = self.control(self.project, settle=4.0)
        self.assert_clean_run(base)
        text = surface(base)
        for token in (f"{CANARY}_PARENT_CLAUDE_MD", f"{CANARY}_PROJECT_AGENTS_MD", f"{CANARY}_RULE_ALWAYS_BODY",
                      f"{CANARY}_AGENT_DESC", f"{CANARY}_DESC_CLAUDE_SKILL", f"{CANARY}_DESC_AGENTS_SKILL",
                      f"{CANARY}_DESC_OMP_SKILL", "canary-claude-cmd", "canary-omp-cmd"):
            self.assertIn(token, text, f"control invalid: {token} not visible without isolation")
        self.assertTrue(self.marker["ext"].exists(), "control invalid: project extension did not run")
        self.assertTrue(self.marker["mcp"].exists() or f"{CANARY}_MCP_TOOL" in text,
                        "control invalid: project MCP server neither launched nor listed")
        for name in self.user_skills:
            self.assertTrue(skill_visible(name, base), f"control invalid: the user's ambient skill {name!r} not visible")
        self.assertTrue({"orca-cli", "orchestration", "computer-use"} & self.user_skills,
                        "the developer machine has no orca skills in ~/.agents/skills to prove absence against")
        sys_base = self.control(self.sys_project)
        self.assert_clean_run(sys_base)
        self.assertIn(f"{CANARY}_SYSTEM_MD_REPLACEMENT", surface(sys_base))

    def test_isolated_command_exposes_no_ambient_configuration_for_manager_and_worker(self):
        for marker in self.marker.values():
            marker.unlink(missing_ok=True)
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                result = self.isolated(role, self.project)
                self.assert_clean_run(result)
                text = surface(result)
                self.assertNotIn(CANARY + "_", text.replace(f"{CANARY}_USER_APPEND_ARG", "").replace(
                    f"{CANARY}_WB_DESC_", "").replace(f"{CANARY}_SKILL_BODY", ""),
                                 f"{role}: a canary reached the prompt/tools/commands")
                for token in ("canary-claude-skill", "canary-agents-skill", "canary-omp-skill", "canary-agent",
                              "canary-claude-cmd", "canary-omp-cmd", "CANARY_P27M_MCP_TOOL", "mcp__canary"):
                    self.assertNotIn(token, text, f"{role}: {token} leaked")
                for name in self.user_skills | {"orca-cli", "orchestration", "computer-use"}:
                    self.assertFalse(skill_visible(name, result), f"{role}: the user's skill {name!r} leaked")
                self.assertNotIn(f"{CANARY}_RULE_ALWAYS_BODY", prompt_text(result))
                self.assertFalse(self.marker["ext"].exists(), f"{role}: the project extension ran")
                self.assertFalse(self.marker["mcp"].exists(), f"{role}: the project MCP server was launched")

    def test_isolated_system_md_is_ignored_and_default_prompt_kept(self):
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                result = self.isolated(role, self.sys_project)
                self.assert_clean_run(result)
                self.assertNotIn(f"{CANARY}_SYSTEM_MD_REPLACEMENT", surface(result))
                self.assertGreater(len(prompt_text(result)), 5000, "the default system prompt was replaced")

    def test_workbench_skills_directory_is_visible_and_role_filtered(self):
        for role in ("manager", "worker"):
            with self.subTest(role=role, filter="none"):
                result = self.isolated(role, self.project)
                text = surface(result)
                for name in ("wb-both", "wb-manager-only", "wb-worker-only"):
                    self.assertTrue(skill_visible(name, result), f"{role}: Workbench skill {name} not visible")
                self.assertIn(f"{CANARY}_WB_DESC_wb-both", prompt_text(result))
                names = {c.get("name") for c in result["commands"] if c.get("source") == "skill"}
                self.assertEqual(names, {"skill:wb-both", "skill:wb-manager-only", "skill:wb-worker-only"}, names)
        patterns = {"manager": ("wb-both", "wb-manager-*"), "worker": ("wb-both", "wb-worker-*")}
        expect = {"manager": {"skill:wb-both", "skill:wb-manager-only"}, "worker": {"skill:wb-both", "skill:wb-worker-only"}}
        for role in ("manager", "worker"):
            with self.subTest(role=role, filter="role"):
                result = self.isolated(role, self.project, role_skills=patterns)
                self.assert_clean_run(result)
                names = {c.get("name") for c in result["commands"] if c.get("source") == "skill"}
                self.assertEqual(names, expect[role])
                other = "wb-worker-only" if role == "manager" else "wb-manager-only"
                self.assertFalse(skill_visible(other, result))

    def test_auth_provider_and_model_settings_are_kept(self):
        control = self.control(self.project, settle=0)
        self.assertIsNotNone(model_of(control), "the non-isolated OMP reports no model (nothing to compare against)")
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                result = self.isolated(role, self.project, settle=0)
                self.assertEqual(model_of(result), model_of(control), "isolation changed the provider/model")

    def test_no_profile_or_agent_dir_and_user_arguments_still_apply(self):
        for role in ("manager", "worker"):
            with self.subTest(role=role):
                result = self.isolated(role, self.project, settle=0)
                command, env = result["command"], result["env"]
                self.assertFalse([a for a in command if a.startswith("--profile")], command)
                self.assertNotIn("--no-skills", command)
                for key in ("PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE", "OMP_CODING_AGENT_DIR"):
                    self.assertNotIn(key, env)
                    self.assertNotIn(key, self.base_env, f"the test host itself sets {key}")
                self.assertIn("--no-extensions", command)
                self.assertIn("--extension", command)
                self.assertEqual(command[command.index("--extension") + 1], self.plan.bridge_extension)
                self.assertGreater(command.index("--append-system-prompt"), command.index("--no-extensions"),
                                   "user args must follow the isolation args")
                self.assertIn(f"{CANARY}_USER_APPEND_ARG", prompt_text(result), "the user's --omp-arg had no effect")
                self.assertEqual(result["stderr"].count("Failed to load extension"), 0, result["stderr"][-400:])

    def test_detector_sensitivity_a_user_arg_can_reenable_one_ambient_source(self):
        """Documented trade-off: --omp-arg follows the isolation args and may override them. The same detector must then
        see the leak, proving the clean results above are not an artefact of a blind detector."""
        reenable = self.root / "reenable-agents.yml"
        write(reenable, "disabledProviders: []\nskills:\n  enableAgentsUser: true\n  enableAgentsProject: true\n")
        plan = launcher.build_plan(self.base_env, omp=self.omp, omp_args=("--config", str(reenable)))
        result = self.isolated("worker", self.project, plan=plan, settle=0)
        self.assert_clean_run(result)
        self.assertTrue(skill_visible("canary-agents-skill", result), "override did not take effect / detector blind")
        self.assertTrue(any(skill_visible(name, result) for name in self.user_skills), "user skills not seen")
        self.assertFalse(skill_visible("canary-claude-skill", result), "an unrelated source was re-enabled too")

    def test_zzz_production_check_agrees_and_nothing_ambient_changed(self):
        for role in ("manager", "worker"):
            command, env = self.wb_command(role, self.project)
            check_env = launcher.isolation_check_environment(self.base_env, self.plan, role=role,
                                                             absent_socket=self.root / "absent-check.sock")
            report = launcher.check_isolation(command, cwd=self.project, environment=check_env, role=role,
                                              allowed_skills=launcher.workbench_skill_names(self.wb_skills),
                                              omp_version=self.version, keep_raw=True)
            self.assertEqual((report["state"], report["leaks"], report["error"]), ("ok", [], None), role)
            self.assertEqual(report["cleanup"]["group_left"], [])
            self.assertEqual(report["cleanup"]["state"], "dead")
            self.assertEqual(alive(naming_identities(self.root)), [])
        after = ambient_view()
        for key, value in self.ambient_before.items():
            self.assertEqual(after[key], value, f"ambient user configuration changed: {key}")


if __name__ == "__main__":
    unittest.main()
