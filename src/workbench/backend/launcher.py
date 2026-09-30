"""Start-requirement checks, the production OMP launch plan and OMP isolation.

The launcher injects the G3 bridge extension and its role/token/generation
environment. It never reads, stores or copies OMP credentials: OMP keeps using
the user's own agent dir, auth, provider and model settings.

C-D59 isolation: both OMP processes also get ``--config`` overlays (the static
``omp_bridge/omp-isolation.yml`` plus a per-role overlay generated in the data
dir), ``--no-extensions``, ``--append-system-prompt ""`` and ``--no-title``, so
ambient context files (AGENTS.md/CLAUDE.md), APPEND_SYSTEM.md, user/project skills, rules, commands,
auto-discovered extensions, MCP project config, memory, Auto QA and task
subagent definitions stay out of the Workbench sessions. Nothing global is
edited and ``--profile``/``PI_CODING_AGENT_DIR`` are never used (they would
switch to a separate agent dir without the user's logins). User
``--omp-arg``/``WORKBENCH_OMP_ARGS`` come after the isolation arguments and can
therefore override them. ``check_isolation`` verifies the result at start
through RPC ``get_state``/``get_available_commands`` only (zero model calls).

User choices (2026-09-30): OMP's default Personality/Tone/Reasoning Format
blocks are kept (no ``personality: none``), so the user's personality preset
and ``~/.omp/agent/PERSONALITY.md`` reach the prompt; ``check_isolation``
reports an existing PERSONALITY.md as a warning (state ``warning``, not a
leak). ``--no-title`` disables OMP's session-title model call, so
TITLE_SYSTEM.md is unused and is a leak only if a user ``--omp-arg`` drops
``--no-title`` from the checked command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence

from workbench.backend.paths import write_private_json
from workbench.runtime.process_evidence import LinuxProcessProbe, ProcessRef

from workbench.terminal.shell_g2.prototype import ShellChoice, ShellUnavailable, select_shell

OMP_ENV = "WORKBENCH_OMP"
OMP_ARGS_ENV = "WORKBENCH_OMP_ARGS"
BRIDGE_EXTENSION_ENV = "WORKBENCH_BRIDGE_EXTENSION"
BRIDGE_GENERATION = 1
DISPLAY_TERM = "xterm-256color"
# Bridge variables are set per OMP child only and never exported to the shell.
_BRIDGE_KEYS = ("WORKBENCH_G3_BRIDGE_SOCKET", "WORKBENCH_G3_ROLE", "WORKBENCH_G3_TOKEN",
                "WORKBENCH_G3_GENERATION", "WORKBENCH_G3_EXPECTED_OMP_VERSION")
# Ambient environment that overrides the isolation overlay inside OMP: the
# PI_AUTO_QA env value wins over dev.autoqa (OMP 18.4.4), so OMP children do
# not inherit it (the user shell keeps it).
_OMP_ENV_DROP = ("PI_AUTO_QA",)

SHELL_REQUIREMENTS = (
    "OMP Workbench needs Bash or a POSIX sh on PATH for its persistent host shell.\n"
    "Neither 'bash' nor 'sh' was found in PATH={path!r}.\n"
    "Install bash (preferred) or make /bin/sh available on PATH, then run the start command again.\n"
    "Your login/default shell (for example zsh) is not used and does not need to change.\n"
    "No backend was started."
)


class StartRequirementError(RuntimeError):
    """A start prerequisite is missing; the message is user guidance."""


def _repository_root() -> Path:
    # src/workbench/backend/launcher.py -> repository root
    return Path(__file__).resolve().parents[3]


def default_bridge_extension() -> Path:
    return _repository_root() / "omp_bridge" / "g3" / "bridge.ts"


def default_isolation_overlay() -> Path:
    return _repository_root() / "omp_bridge" / "omp-isolation.yml"


def default_skills_dir() -> Path:
    return _repository_root() / "omp_bridge" / "skills"


# -- C-D59 isolation constants (verified with OMP 18.4.4) -------------------
# Capability-provider ids disabled per run; must equal the list in
# omp_bridge/omp-isolation.yml (asserted by tests). None is a model provider id.
ISOLATION_PROVIDER_IDS = (
    "native", "omp-managed", "skillshare", "agents-md", "agents", "claude-md", "claude-plugins", "claude",
    "cline", "codex", "cursor", "gemini", "github", "mcp-json", "omp-plugins", "opencode", "ssh-json",
    "vscode", "windsurf", "agent-plugins", "builtin-defaults",
)
# Role skill filter (skills.includeSkills globs) applied to omp_bridge/skills.
# Empty = no role filter. Role skills themselves are CW-18.
ROLE_SKILL_PATTERNS: dict[str, tuple[str, ...]] = {"manager": (), "worker": ()}
OMP_ROLE_NAMES = ("manager", "worker")
# What an isolated OMP 18.4.4 still shows: bundled task agents and bundled
# non-builtin-sourced commands. Anything else is reported as a leak.
BUNDLED_TASK_AGENTS = frozenset({"scout", "reviewer", "security-reviewer", "task", "sonic"})
BUNDLED_EXTRA_COMMANDS = frozenset({"autoresearch", "init"})
# OMP versions the Workbench evidence was produced with (drift is reported).
EVIDENCE_OMP_VERSIONS = {"bridge_g3": "18.2.10", "isolation": "18.4.4"}
# Present only in OMP's bundled default system prompt. A project/user
# SYSTEM.md (or --system-prompt) replaces it and uses another template.
DEFAULT_PROMPT_MARKER = "You are omp's"
# OMP's default prompt renders it (the isolation overlay keeps the default
# personality); observed for evidence only, not a leak.
PERSONALITY_HEADING = "# Personality"
# Rendered only while OMP Auto QA (dev.autoqa) is effectively on.
AUTOQA_MARKER = "xd://report_issue"
# Where OMP 18.4.4 looks for prompt files outside disabledProviders:
# APPEND_SYSTEM.md / TITLE_SYSTEM.md in <cwd>/{.omp,.claude,.codex,.gemini} and
# the user agent dir; PERSONALITY.md in the user agent dir only (a warning).
PROMPT_FILE_PROJECT_DIRS = (".omp", ".claude", ".codex", ".gemini")
_PROMPT_FILE_READ_LIMIT = 4096
# Bound for the start-path ``omp config get`` reads (all keys together).
CONFIG_READ_TIMEOUT = 8.0
USER_CONFIG_KEYS = ("disabledProviders", "task.disabledAgents")
ISOLATION_CHECK_TIMEOUT = 20.0
ISOLATION_CHECK_TOKEN = "isolation-check"
_RPC_CHECK_ARGS = ("--mode", "rpc", "--no-session", "--no-title")
_AGENT_NAME_READ_LIMIT = 16384
_RESERVED_AGENT_NAMES = frozenset({"main", "sub"})


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    shell: ShellChoice
    omp: str
    omp_version: str
    bridge_extension: str
    omp_args: tuple[str, ...] = field(default_factory=tuple)

    def to_argv(self) -> list[str]:
        argv = ["--shell-kind", self.shell.kind, "--shell-path", self.shell.executable,
                "--omp", self.omp, "--omp-version", self.omp_version,
                "--bridge-extension", self.bridge_extension]
        for item in self.omp_args:
            argv.append(f"--omp-arg={item}")
        return argv


def choose_shell(environment: Mapping[str, str]) -> ShellChoice:
    path = environment.get("PATH", "")
    try:
        return select_shell(path)
    except ShellUnavailable as exc:
        raise StartRequirementError(SHELL_REQUIREMENTS.format(path=path)) from exc


def omp_version(omp: str, timeout: float = 15.0) -> str:
    try:
        result = subprocess.run([omp, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StartRequirementError(f"cannot run '{omp} --version': {exc}") from exc
    lines = (result.stdout + result.stderr).strip().splitlines()
    if result.returncode != 0 or not lines:
        raise StartRequirementError(f"'{omp} --version' failed with exit status {result.returncode}")
    return lines[0].strip()


def build_plan(environment: Mapping[str, str], *, omp: str | None = None,
               omp_args: Sequence[str] = (), bridge_extension: str | None = None) -> LaunchPlan:
    """Check every start requirement before any backend process is created."""
    shell = choose_shell(environment)
    candidate = omp or environment.get(OMP_ENV) or shutil.which("omp", path=environment.get("PATH", ""))
    if not candidate or not os.access(candidate, os.X_OK):
        raise StartRequirementError(
            "OMP Workbench needs the 'omp' executable. Put it on PATH or pass --omp/WORKBENCH_OMP.\n"
            "No backend was started.")
    candidate = os.path.abspath(candidate)
    extension = Path(bridge_extension or environment.get(BRIDGE_EXTENSION_ENV) or default_bridge_extension())
    if not extension.is_file():
        raise StartRequirementError(
            f"OMP bridge extension not found: {extension}\nNo backend was started.")
    if not default_isolation_overlay().is_file():
        raise StartRequirementError(
            f"OMP isolation overlay not found: {default_isolation_overlay()}\nNo backend was started.")
    extra = tuple(omp_args) or tuple(shlex.split(environment.get(OMP_ARGS_ENV, "")))
    return LaunchPlan(shell, candidate, omp_version(candidate), str(extension.resolve()), extra)


def isolation_args(role_overlay: Path | str, append_system_prompt: str = "") -> list[str]:
    """Per-run isolation: static overlay, per-role overlay, no extension discovery.

    ``--append-system-prompt`` is always passed: OMP only falls back to an
    ambient APPEND_SYSTEM.md when the option is absent, and an empty value
    appends nothing. CW-18 supplies the role prompt through this slot.
    ``--no-title`` skips the session-title model call (TITLE_SYSTEM.md unused).
    """
    return ["--config", str(default_isolation_overlay()), "--config", str(role_overlay), "--no-extensions",
            "--append-system-prompt", append_system_prompt, "--no-title"]


def omp_command(plan: LaunchPlan, role_overlay: Path | str, append_system_prompt: str = "") -> list[str]:
    # Order matters: user arguments follow the isolation arguments (and may
    # override them); the explicit bridge extension still loads under
    # --no-extensions.
    return [plan.omp, *isolation_args(role_overlay, append_system_prompt), *plan.omp_args,
            "--extension", plan.bridge_extension]


def _read_head(path: Path, limit: int) -> str | None:
    """At most ``limit`` bytes of a regular file; never blocks on a FIFO/device."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _scalar(value: str, following: Sequence[str]) -> str | None:
    """A top-level YAML scalar: plain/quoted value or a block scalar (|, >)."""
    value = value.strip()
    if value[:1] in {"|", ">"}:
        block = []
        for line in following:
            if line and not line[:1].isspace():
                break
            block.append(line.strip())
        return " ".join(item for item in block if item) or None
    if value[:1] in {"'", '"'}:
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else None
    value = re.split(r"\s#", value, maxsplit=1)[0].strip()
    if not value or value[:1] in {"[", "{", "&", "*", "!"}:
        return None  # empty, nested or non-string: OMP ignores the definition
    return value


def _frontmatter_fields(text: str, keys: Sequence[str]) -> dict[str, str | None]:
    lines = text.splitlines()
    fields: dict[str, str | None] = {key: None for key in keys}
    if not lines or lines[0].strip() != "---":
        return fields
    body = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        body.append(line)
    for index, line in enumerate(body):
        if not line or line[:1].isspace() or line.lstrip().startswith("#"):
            continue
        key, colon, value = line.partition(":")
        if colon and key.strip() in fields and fields[key.strip()] is None:
            fields[key.strip()] = _scalar(value, body[index + 1:])
    return fields


def _frontmatter_name(path: Path) -> str | None:
    """Read only the ``name:`` field of a Markdown frontmatter block."""
    head = _read_head(Path(path), _AGENT_NAME_READ_LIMIT)
    return _frontmatter_fields(head, ("name",))["name"] if head else None


def _agent_definition_name(path: Path) -> str | None:
    """The name OMP 18.4.4 would register for an agent file, else None.

    OMP drops a definition without a string ``name`` and ``description`` or
    with a reserved name (main/sub); only frontmatter is read here.
    """
    head = _read_head(path, _AGENT_NAME_READ_LIMIT)
    if not head:
        return None
    fields = _frontmatter_fields(head, ("name", "description"))
    name = fields["name"]
    if not name or not fields["description"] or name.strip().lower() in _RESERVED_AGENT_NAMES:
        return None
    return name


def _definition_names(directory: Path) -> list[str]:
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    names = []
    for entry in entries:
        if entry.suffix == ".md" and entry.is_file():
            name = _agent_definition_name(entry)
            if name:
                names.append(name)
    return names


def _nearest_project_agents_dir(project_dir: Path | str) -> Path | None:
    current = Path(project_dir).resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".omp" / "agents"
        if candidate.is_dir():
            return candidate
    return None


def omp_user_dir(home: Path | str, environment: Mapping[str, str] | None = None) -> Path:
    """OMP's user agent dir as seen by its discovery helpers (``~/.omp/agent``)."""
    config_dir = (environment or {}).get("PI_CONFIG_DIR") or ".omp"
    return Path(home) / config_dir / "agent"


def task_agent_names(project_dir: Path | str, home: Path | str,
                     environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Names of the ambient task subagent definitions OMP 18.4.4 actually loads.

    OMP reads only the nearest ``<ancestor>/.omp/agents`` and
    ``~/.omp/agent/agents`` (plugin agent dirs are closed by
    disabledProviders) and keeps only valid definitions; such a definition
    replaces a bundled agent of the same name, so that name is disabled too.
    A file OMP would not load is not listed, so no bundled agent is disabled
    because of it. Used for ``task.disabledAgents`` since disabledProviders
    does not cover these dirs.
    """
    names: set[str] = set()
    nearest = _nearest_project_agents_dir(project_dir)
    if nearest is not None:
        names.update(_definition_names(nearest))
    names.update(_definition_names(omp_user_dir(home, environment) / "agents"))
    return tuple(sorted(names))


def workbench_skill_names(skills_dir: Path | str, patterns: Sequence[str] = ()) -> tuple[str, ...]:
    """Skills in the Workbench skills dir (``<name>/SKILL.md``) after a role filter."""
    root = Path(skills_dir)
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return ()
    names = []
    for entry in entries:
        skill = entry / "SKILL.md"
        if entry.is_dir() and skill.is_file():
            names.append(_frontmatter_name(skill) or entry.name)
    if patterns:
        names = [name for name in names if any(fnmatch.fnmatchcase(name, item) for item in patterns)]
    return tuple(sorted(set(names)))


def _string_list(raw: bytes) -> list[str] | None:
    try:
        value = json.loads(raw)["value"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _bounded_outputs(argvs: Sequence[Sequence[str]], *, cwd: Path | str, environment: Mapping[str, str],
                     deadline: float, limit: int = 1 << 16) -> list[tuple[int | None, bytes, str | None]]:
    """Run every ``argv`` at once, each in its own session, until exit or ``deadline``.

    Each process group is stopped and its leader reaped afterwards (a leader
    stays unreaped until then, so its group id cannot be reused). Returns
    (returncode or None, stdout, error) per argv.
    """
    jobs: list[dict[str, Any]] = []
    for argv in argvs:
        job: dict[str, Any] = {"process": None, "ref": None, "output": b"", "error": None, "open": False}
        jobs.append(job)
        try:
            process = subprocess.Popen(list(argv), cwd=cwd, env=dict(environment), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       start_new_session=True, close_fds=True)
        except OSError as exc:
            job["error"] = f"spawn failed: {exc}"
            continue
        try:
            ticks = LinuxProcessProbe.start_ticks(process.pid)
        except OSError:
            ticks = None
        job.update(process=process, open=True,
                   ref=ProcessRef("omp-config-read", process.pid, ticks, 1) if ticks else None)
    try:
        while any(job["open"] for job in jobs):
            remaining = deadline - time.monotonic()
            active = {job["process"].stdout.fileno(): job for job in jobs if job["open"]}
            ready, _, _ = select.select(list(active), [], [], max(0.0, min(remaining, 0.1)))
            for fd in ready:
                job = active[fd]
                chunk = os.read(fd, 1 << 16)
                job["output"] += chunk
                if not chunk:
                    job["open"] = False
                elif len(job["output"]) > limit:
                    job.update(open=False, error="output too large")
            for fd, job in active.items():
                if job["open"] and fd not in ready and _leader_exited(job["process"].pid):
                    # The leader is done (a descendant may hold stdout open):
                    # keep what it wrote before exiting, without blocking.
                    while job["open"] and select.select([fd], [], [], 0)[0]:
                        chunk = os.read(fd, 1 << 16)
                        job["output"] += chunk
                        if not chunk:
                            break
                        if len(job["output"]) > limit:
                            job["error"] = "output too large"
                            break
                    job["open"] = False
            if remaining <= 0:
                for job in jobs:
                    if job["open"]:
                        job.update(open=False, error="timed out")
    finally:
        for job in jobs:
            if job["process"] is not None:
                _stop_group(job["process"], job["ref"])
    return [(None if job["error"] or job["process"] is None else job["process"].returncode,
             job["output"], job["error"]) for job in jobs]


def read_user_config(omp: str, keys: Sequence[str] = USER_CONFIG_KEYS, *, cwd: Path | str,
                     environment: Mapping[str, str], timeout: float = CONFIG_READ_TIMEOUT
                     ) -> dict[str, list[str] | None]:
    """The user's own values of a few array settings, read-only via ``omp config get``.

    Only the named keys are requested (no credentials). The reads run
    concurrently under one ``timeout``; each runs in its own process group
    that is stopped and reaped. A key is ``None`` when it could not be read
    in time.
    """
    results = _bounded_outputs([[omp, "config", "get", key, "--json"] for key in keys], cwd=cwd,
                               environment=environment, deadline=time.monotonic() + timeout)
    return {key: _string_list(output) if code == 0 else None for key, (code, output, _error) in zip(keys, results)}


def user_disabled_providers(omp: str, *, cwd: Path | str, environment: Mapping[str, str],
                            timeout: float = CONFIG_READ_TIMEOUT) -> list[str] | None:
    """The user's own ``disabledProviders`` (one config key; no credentials).

    The per-role overlay replaces this array, so the user's entries are merged
    back in. ``None`` means it could not be read.
    """
    return read_user_config(omp, ("disabledProviders",), cwd=cwd, environment=environment,
                            timeout=timeout)["disabledProviders"]


def role_overlay(role: str, *, project_dir: Path | str, home: Path | str,
                 user_disabled_providers: Sequence[str] = (), user_disabled_agents: Sequence[str] = (),
                 skills_dir: Path | str | None = None, role_skills: Mapping[str, Sequence[str]] | None = None,
                 environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The per-role ``--config`` overlay that completes omp-isolation.yml.

    ``--config`` replaces arrays, so the user's own ``disabledProviders`` and
    ``task.disabledAgents`` are unioned back in (the Workbench never enables
    what the user disabled). The skill filter is Workbench-owned: ``includeSkills``
    is the role filter (``[]`` = every Workbench skill) and ``ignoredSkills`` is
    ``[]``, so the user's global include/ignore lists can neither hide
    Workbench skills nor widen the role filter; ambient skill sources are
    already off, so those lists have nothing else to act on here.
    """
    if role not in OMP_ROLE_NAMES:
        raise ValueError(f"unknown OMP role {role!r}")
    providers = list(ISOLATION_PROVIDER_IDS)
    providers += [item for item in dict.fromkeys(user_disabled_providers) if item not in providers]
    patterns = tuple((ROLE_SKILL_PATTERNS if role_skills is None else role_skills).get(role, ()))
    skills: dict[str, Any] = {"customDirectories": [str(skills_dir or default_skills_dir())],
                              "includeSkills": list(patterns), "ignoredSkills": []}
    agents = list(dict.fromkeys(user_disabled_agents))
    agents += [name for name in task_agent_names(project_dir, home, environment) if name not in agents]
    return {"disabledProviders": providers, "skills": skills, "task": {"disabledAgents": agents}}


def role_skill_allowlist(role: str, skills_dir: Path | str | None = None) -> tuple[str, ...]:
    return workbench_skill_names(skills_dir or default_skills_dir(), ROLE_SKILL_PATTERNS.get(role, ()))


def write_role_overlay(directory: Path, role: str, overlay: Mapping[str, Any]) -> Path:
    """Write ``omp-isolation-<role>.yml`` (JSON, valid YAML) 0600 into a private dir."""
    if role not in OMP_ROLE_NAMES:
        raise ValueError(f"unknown OMP role {role!r}")
    path = Path(directory) / f"omp-isolation-{role}.yml"
    write_private_json(path, dict(overlay))
    return path


def omp_environment(base: Mapping[str, str], plan: LaunchPlan, *, role: str, token: str,
                    bridge_socket: Path) -> dict[str, str]:
    env = {key: value for key, value in base.items() if key not in _BRIDGE_KEYS and key not in _OMP_ENV_DROP}
    env.update({
        "TERM": DISPLAY_TERM,
        "COLORTERM": env.get("COLORTERM", "truecolor"),
        "WORKBENCH_G3_BRIDGE_SOCKET": str(bridge_socket),
        "WORKBENCH_G3_ROLE": role,
        "WORKBENCH_G3_TOKEN": token,
        "WORKBENCH_G3_GENERATION": str(BRIDGE_GENERATION),
        "WORKBENCH_G3_EXPECTED_OMP_VERSION": plan.omp_version,
    })
    return env


def shell_environment(base: Mapping[str, str]) -> dict[str, str]:
    env = {key: value for key, value in base.items() if key not in _BRIDGE_KEYS}
    env["TERM"] = DISPLAY_TERM
    return env


# -- C-D59 start-up isolation check -----------------------------------------
def _block(prompt: str, tag: str) -> list[str]:
    lines: list[str] = []
    for match in re.finditer(rf"<{tag}>\n(.*?)</{tag}>", prompt, re.S):
        lines.extend(line.strip() for line in match.group(1).splitlines() if line.strip())
    return lines


def observe_isolation(state: Mapping[str, Any], commands: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Names of every ambient-injectable item visible in RPC get_state/get_available_commands."""
    prompt = state.get("systemPrompt") or ""
    if isinstance(prompt, list):
        prompt = "\n".join(str(item) for item in prompt)
    # Default template: "- name: description" lists; custom (SYSTEM.md /
    # --system-prompt) template: <skill name="..."> and <rule name="...">.
    skills = [re.sub(r":.*", "", line[2:]).strip() for line in _block(prompt, "skills") if line.startswith("- ")]
    skills += re.findall(r'<skill name="([^"]*)"', prompt)
    rules = [line[:80] for line in _block(prompt, "generic-rules")]
    rules += [line[2:].split(" ", 1)[0].rstrip(":") for line in _block(prompt, "domain-rules")
              if line.startswith("- ")]
    rules += re.findall(r'<rule name="([^"]*)"', prompt)
    tools = [str(tool.get("name")) for tool in state.get("dumpTools") or [] if isinstance(tool, Mapping)]
    agents: list[str] = []
    for tool in state.get("dumpTools") or []:
        if isinstance(tool, Mapping) and tool.get("name") == "task":
            section = str(tool.get("description") or "").split("# Available Agents", 1)
            if len(section) == 2:
                body = re.split(r"\n#", section[1], maxsplit=1)[0]
                agents = re.findall(r"^- `([^`]+)`", body, re.M)
    skill_commands, other = [], []
    for command in commands:
        name, source = str(command.get("name")), command.get("source")
        if source == "skill":
            skill_commands.append(name.removeprefix("skill:"))
        elif source != "builtin":
            other.append([name, source])
    model = state.get("model") if isinstance(state.get("model"), Mapping) else {}
    return {
        "context_files": re.findall(r'<file path="([^"]*)"', prompt),
        "skills": sorted(set(skills)),
        "skill_commands": sorted(set(skill_commands)),
        "rules": rules,
        "mcp_tools": sorted(set(re.findall(r"`xd://(mcp__[^`]+)`", prompt))
                            | {tool for tool in tools if tool.startswith("mcp__")}),
        "task_agents": agents,
        "other_commands": other,
        "tools": tools,
        "system_prompt_chars": len(prompt),
        "system_prompt_default": DEFAULT_PROMPT_MARKER in prompt,
        "personality_block": re.search(rf"^{re.escape(PERSONALITY_HEADING)}\s*$", prompt, re.M) is not None,
        "autoqa": AUTOQA_MARKER in prompt,
        "model": f"{model.get('provider')}/{model.get('id')}" if model else None,
    }


def isolation_leaks(observed: Mapping[str, Any], *, allowed_skills: Sequence[str]) -> list[str]:
    allowed = set(allowed_skills)
    leaks = [] if observed.get("system_prompt_default", True) else ["system_prompt:replaced"]
    leaks += [f"context_file:{path}" for path in observed["context_files"]]
    leaks += [f"skill:{name}" for name in sorted(set(observed["skills"]) | set(observed["skill_commands"]))
              if name not in allowed]
    leaks += [f"rule:{rule}" for rule in observed["rules"]]
    leaks += [f"mcp:{name}" for name in observed["mcp_tools"]]
    leaks += [f"task_agent:{name}" for name in observed["task_agents"] if name not in BUNDLED_TASK_AGENTS]
    leaks += [f"command:{name}({source})" for name, source in observed["other_commands"]
              if name not in BUNDLED_EXTRA_COMMANDS]
    if observed.get("autoqa"):
        leaks.append("autoqa:enabled")
    return leaks


def _probe_lines(text: str, count: int = 3) -> list[str]:
    """Up to ``count`` non-empty lines (complete ones only) of a bounded read."""
    lines = text.splitlines()
    if len(text.encode("utf-8", "replace")) >= _PROMPT_FILE_READ_LIMIT and lines:
        lines = lines[:-1]  # the last line may be cut by the read limit
    return [line.strip() for line in lines if line.strip()][:count]


def ambient_prompt_files(project_dir: Path | str, environment: Mapping[str, str]) -> list[dict[str, Any]]:
    """Existing ambient prompt files OMP 18.4.4 reads outside disabledProviders.

    Each entry: kind (append_system/personality/title_system), path and up to
    three non-empty lines (bounded read) used to see whether it reached the
    prompt. Paths follow OMP: ``<cwd>/{.omp,.claude,.codex,.gemini}`` and the
    user agent dir (PERSONALITY.md: ``PI_CODING_AGENT_DIR`` or the same dir).
    """
    home = environment.get("HOME") or str(Path.home())
    user = omp_user_dir(home, environment)
    agent_dir = Path(environment.get("PI_CODING_AGENT_DIR") or user)
    project = Path(project_dir)
    candidates = [("append_system", project / name / "APPEND_SYSTEM.md") for name in PROMPT_FILE_PROJECT_DIRS]
    candidates += [("append_system", user / "APPEND_SYSTEM.md"), ("personality", agent_dir / "PERSONALITY.md")]
    candidates += [("title_system", project / name / "TITLE_SYSTEM.md") for name in PROMPT_FILE_PROJECT_DIRS]
    candidates += [("title_system", user / "TITLE_SYSTEM.md")]
    found = []
    for kind, path in candidates:
        text = _read_head(path, _PROMPT_FILE_READ_LIMIT)
        if text is not None:
            found.append({"kind": kind, "path": str(path), "probe": _probe_lines(text)})
    return found


def prompt_file_leaks(prompt: str, files: Sequence[Mapping[str, Any]], *, no_title: bool = True) -> list[str]:
    """Ambient prompt files whose content reached the prompt (or cannot be excluded).

    APPEND_SYSTEM.md is a leak when all its probe lines are in the system
    prompt (an empty file adds nothing). TITLE_SYSTEM.md feeds only the title
    model call, which RPC cannot show: with ``--no-title`` it is unused;
    without, an existing one is reported. PERSONALITY.md is a warning
    (``prompt_file_warnings``), not a leak.
    """
    leaks = []
    for item in files:
        probe = item.get("probe") or []
        if item["kind"] == "personality" or (item["kind"] == "title_system" and no_title):
            continue
        if item["kind"] == "title_system" or (probe and all(line in prompt for line in probe)):
            leaks.append(f"{item['kind']}:{item['path']}")
    return leaks


def prompt_file_warnings(files: Sequence[Mapping[str, Any]]) -> list[str]:
    """PERSONALITY.md is read by OMP (default personality kept by user choice): warn, do not leak."""
    return [f"personality:{item['path']}" for item in files if item["kind"] == "personality"]


def isolation_check_environment(base: Mapping[str, str], plan: LaunchPlan, *, role: str,
                                absent_socket: Path) -> dict[str, str]:
    """Same environment as the pane, but the bridge points at a socket that never exists."""
    return omp_environment(base, plan, role=role, token=ISOLATION_CHECK_TOKEN, bridge_socket=absent_socket)


class _CheckFailed(Exception):
    pass


def _group_members(pgid: int) -> list[int]:
    members = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as stream:
                fields = stream.read().rsplit(b") ", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 2 and fields[2] == str(pgid).encode() and fields[0] not in {b"Z", b"X"}:
            members.append(int(entry))
    return sorted(members)


def _leader_exited(pid: int) -> bool:
    try:  # WNOWAIT: observe without reaping, so the group id stays ours
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _stop_group(process: subprocess.Popen, ref: ProcessRef | None) -> dict[str, Any]:
    """Stop the check's own session/process group, then reap the leader.

    The leader is our unreaped child and led a new session, so its PID (the
    group id) cannot be reused until we reap it: every signal below reaches
    exactly this group.
    """
    pgid = process.pid
    for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
        if _leader_exited(pgid) and not _group_members(pgid):
            break
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and not (_leader_exited(pgid) and not _group_members(pgid)):
            time.sleep(0.02)
    left = _group_members(pgid)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    state = LinuxProcessProbe().observe(ref).state if ref is not None else "unknown"
    return {"pid": pgid, "start_ticks": ref.start_ticks if ref else None, "state": state,
            "returncode": process.returncode, "group_left": left}


def _rpc_exchange(process: subprocess.Popen, requests: Sequence[tuple[str, str]], deadline: float,
                  cancel: threading.Event | None) -> dict[str, Any]:
    assert process.stdin is not None and process.stdout is not None
    fd = process.stdout.fileno()
    buffer = b""
    responses: dict[str, Any] = {}
    for request_id, kind in requests:
        try:
            process.stdin.write(json.dumps({"id": request_id, "type": kind}).encode() + b"\n")
            process.stdin.flush()
        except OSError as exc:
            raise _CheckFailed(f"omp exited before '{kind}' could be sent ({exc})") from exc
        while request_id not in responses:
            if cancel is not None and cancel.is_set():
                raise _CheckFailed("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _CheckFailed(f"timed out waiting for '{kind}'")
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.1))
            if not ready:
                # A child may keep stdout open after OMP itself died.
                if _leader_exited(process.pid):
                    raise _CheckFailed(f"omp exited before answering '{kind}'")
                continue
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                raise _CheckFailed(f"omp exited (closed stdout) before answering '{kind}'")
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if isinstance(frame, dict) and frame.get("type") == "response" and frame.get("id") in dict(requests):
                    if not frame.get("success", False):
                        raise _CheckFailed(f"'{frame.get('command')}' failed: {frame.get('error')}")
                    responses[frame["id"]] = frame.get("data") or {}
    return responses


def check_isolation(command: Sequence[str], *, cwd: Path | str, environment: Mapping[str, str], role: str,
                    allowed_skills: Sequence[str], omp_version: str | None = None,
                    timeout: float = ISOLATION_CHECK_TIMEOUT, cancel: threading.Event | None = None,
                    keep_raw: bool = False) -> dict[str, Any]:
    """Run the pane's OMP command once in RPC mode and list what it loaded.

    Only ``get_state`` and ``get_available_commands`` are sent (no prompt, no
    model call); ``--no-session`` keeps no session. Bounded by ``timeout``;
    the check's own process group is always stopped and reaped. ``keep_raw``
    adds the raw RPC data (tests/evidence only; not for the snapshot).
    """
    started = time.monotonic()
    result: dict[str, Any] = {"role": role, "state": "failed", "ok": False, "leaks": [], "error": None,
                              "warnings": [], "observed": None, "extension_errors": [], "omp_version": omp_version,
                              "duration": None, "cleanup": None}
    argv = [*command, *_RPC_CHECK_ARGS]
    with tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=dict(environment), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=stderr, start_new_session=True,
                                       close_fds=True)
        except OSError as exc:
            result.update(error=f"spawn failed: {exc}", duration=round(time.monotonic() - started, 3))
            return result
        try:
            ticks = LinuxProcessProbe.start_ticks(process.pid)
        except OSError:
            ticks = None
        ref = ProcessRef("isolation-check", process.pid, ticks, 1) if ticks else None
        try:
            data = _rpc_exchange(process, (("wb-iso-state", "get_state"), ("wb-iso-commands", "get_available_commands")),
                                 started + timeout, cancel)
        except _CheckFailed as exc:
            result["error"] = str(exc)
            data = None
        finally:
            result["cleanup"] = _stop_group(process, ref)
        stderr.seek(0)
        errors = stderr.read(1 << 16).decode("utf-8", "replace")
    result["extension_errors"] = [line.strip() for line in errors.splitlines() if "Failed to load extension" in line]
    if data is not None:
        observed = observe_isolation(data["wb-iso-state"], data["wb-iso-commands"].get("commands") or [])
        prompt = data["wb-iso-state"].get("systemPrompt") or ""
        if isinstance(prompt, list):
            prompt = "\n".join(str(item) for item in prompt)
        files = ambient_prompt_files(cwd, environment)
        observed["prompt_files"] = [{key: item[key] for key in ("kind", "path")} for item in files]
        leaks = isolation_leaks(observed, allowed_skills=allowed_skills) + prompt_file_leaks(
            prompt, files, no_title="--no-title" in command)
        warnings = prompt_file_warnings(files)
        result.update(observed=observed, leaks=leaks, warnings=warnings)
        if keep_raw:
            result["raw"] = data
        if result["extension_errors"]:
            result["error"] = "; ".join(result["extension_errors"])
        else:
            result.update(state="leak" if leaks else ("warning" if warnings else "ok"), ok=not leaks)
    result["duration"] = round(time.monotonic() - started, 3)
    return result


def _version_number(text: str | None) -> str | None:
    match = re.search(r"(\d+\.\d+\.\d+)", text or "")
    return match.group(1) if match else None


def _version_drift(omp_version: str | None) -> dict[str, str]:
    running = _version_number(omp_version)
    return {name: version for name, version in EVIDENCE_OMP_VERSIONS.items() if version != running}


def pending_isolation(omp_version: str | None) -> dict[str, Any]:
    return {"state": "pending", "checked": False, "ok": None, "leaks": [], "warnings": [], "warning": None,
            "omp_version": omp_version, "evidence_versions": dict(EVIDENCE_OMP_VERSIONS),
            "version_drift": _version_drift(omp_version), "roles": {}}


def summarize_isolation(results: Mapping[str, Mapping[str, Any]], omp_version: str | None) -> dict[str, Any]:
    """Aggregate per-role check results into the backend snapshot field."""
    summary = pending_isolation(omp_version)
    leaks = [f"{role}:{leak}" for role, item in results.items() for leak in item.get("leaks") or []]
    warnings = [f"{role}:{note}" for role, item in results.items() for note in item.get("warnings") or []]
    errors = [f"{role}: {item.get('error')}" for role, item in results.items() if item.get("error")]
    states = {item.get("state") for item in results.values()}
    state = "failed" if errors or "failed" in states or not results else ("leak" if leaks else ("warning" if warnings else "ok"))
    warning = None
    if state == "leak":
        warning = "OMP isolation leak (ambient configuration loaded): " + ", ".join(leaks)
    elif state == "failed":
        warning = "OMP isolation check failed: " + ("; ".join(errors) or "no result")
    elif state == "warning":
        warning = "OMP keeps its default personality and reads PERSONALITY.md: " + ", ".join(warnings)
    summary.update(state=state, checked=True, ok=state in ("ok", "warning"), leaks=leaks, warnings=warnings,
                   warning=warning,
                   roles={role: dict(item) for role, item in results.items()})
    return summary
