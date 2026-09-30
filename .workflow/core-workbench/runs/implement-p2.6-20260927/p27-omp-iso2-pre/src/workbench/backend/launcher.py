"""Start-requirement checks, the production OMP launch plan and OMP isolation.

The launcher injects the G3 bridge extension and its role/token/generation
environment. It never reads, stores or copies OMP credentials: OMP keeps using
the user's own agent dir, auth, provider and model settings.

C-D59 isolation: both OMP processes also get ``--config`` overlays (the static
``omp_bridge/omp-isolation.yml`` plus a per-role overlay generated in the data
dir) and ``--no-extensions``, so ambient context files (AGENTS.md/CLAUDE.md),
user/project skills, rules, commands, auto-discovered extensions, MCP project
config, memory and task subagent definitions stay out of the Workbench
sessions. Nothing global is edited and ``--profile``/``PI_CODING_AGENT_DIR``
are never used (they would switch to a separate agent dir without the user's
logins). User ``--omp-arg``/``WORKBENCH_OMP_ARGS`` come after the isolation
arguments and can therefore override them. ``check_isolation`` verifies the
result at start through RPC ``get_state``/``get_available_commands`` only
(zero model calls).
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
ISOLATION_CHECK_TIMEOUT = 20.0
ISOLATION_CHECK_TOKEN = "isolation-check"
_RPC_CHECK_ARGS = ("--mode", "rpc", "--no-session", "--no-title")
_AGENT_NAME_READ_LIMIT = 4096


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


def isolation_args(role_overlay: Path | str) -> list[str]:
    """Per-run isolation: static overlay, per-role overlay, no extension discovery."""
    return ["--config", str(default_isolation_overlay()), "--config", str(role_overlay), "--no-extensions"]


def omp_command(plan: LaunchPlan, role_overlay: Path | str) -> list[str]:
    # Order matters: user arguments follow the isolation arguments (and may
    # override them); the explicit bridge extension still loads under
    # --no-extensions.
    return [plan.omp, *isolation_args(role_overlay), *plan.omp_args, "--extension", plan.bridge_extension]


def _frontmatter_name(path: Path) -> str | None:
    """Read only the ``name:`` field of a Markdown frontmatter block."""
    try:
        with open(path, "rb") as stream:
            head = stream.read(_AGENT_NAME_READ_LIMIT).decode("utf-8", "replace")
    except OSError:
        return None
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key.strip() == "name":
            value = value.strip().strip("\"'").strip()
            return value or None
    return None


def _definition_names(directory: Path) -> list[str]:
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    names = []
    for entry in entries:
        if entry.suffix == ".md" and entry.is_file():
            names.append(_frontmatter_name(entry) or entry.stem)
    return names


def task_agent_names(project_dir: Path | str, home: Path | str) -> tuple[str, ...]:
    """Names of task subagent definitions OMP would discover (not a capability provider).

    Project ``.omp/agents`` in the project dir and every ancestor, plus
    ``~/.omp/agent/agents``. Only the frontmatter ``name`` (else the file stem)
    is read. Used for ``task.disabledAgents`` since disabledProviders does not
    cover them.
    """
    names: set[str] = set()
    current = Path(project_dir).resolve()
    for directory in (current, *current.parents):
        names.update(_definition_names(directory / ".omp" / "agents"))
    names.update(_definition_names(Path(home) / ".omp" / "agent" / "agents"))
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


def user_disabled_providers(omp: str, *, cwd: Path | str, environment: Mapping[str, str],
                            timeout: float = 15.0) -> list[str] | None:
    """The user's own ``disabledProviders`` (one config key; no credentials).

    The per-role overlay replaces this array, so the user's entries are merged
    back in. ``None`` means it could not be read.
    """
    try:
        result = subprocess.run([omp, "config", "get", "disabledProviders", "--json"], cwd=cwd,
                                env=dict(environment), stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout, check=False)
        value = json.loads(result.stdout)["value"] if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def role_overlay(role: str, *, project_dir: Path | str, home: Path | str,
                 user_disabled_providers: Sequence[str] = (), skills_dir: Path | str | None = None,
                 role_skills: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
    """The per-role ``--config`` overlay that completes omp-isolation.yml."""
    if role not in OMP_ROLE_NAMES:
        raise ValueError(f"unknown OMP role {role!r}")
    providers = list(ISOLATION_PROVIDER_IDS)
    providers += [item for item in dict.fromkeys(user_disabled_providers) if item not in providers]
    skills: dict[str, Any] = {"customDirectories": [str(skills_dir or default_skills_dir())]}
    patterns = tuple((ROLE_SKILL_PATTERNS if role_skills is None else role_skills).get(role, ()))
    if patterns:
        skills["includeSkills"] = list(patterns)
    return {"disabledProviders": providers, "skills": skills,
            "task": {"disabledAgents": list(task_agent_names(project_dir, home))}}


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
    env = {key: value for key, value in base.items() if key not in _BRIDGE_KEYS}
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
    return leaks


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
                              "observed": None, "extension_errors": [], "omp_version": omp_version,
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
        leaks = isolation_leaks(observed, allowed_skills=allowed_skills)
        result.update(observed=observed, leaks=leaks)
        if keep_raw:
            result["raw"] = data
        if result["extension_errors"]:
            result["error"] = "; ".join(result["extension_errors"])
        else:
            result.update(state="leak" if leaks else "ok", ok=not leaks)
    result["duration"] = round(time.monotonic() - started, 3)
    return result


def _version_number(text: str | None) -> str | None:
    match = re.search(r"(\d+\.\d+\.\d+)", text or "")
    return match.group(1) if match else None


def _version_drift(omp_version: str | None) -> dict[str, str]:
    running = _version_number(omp_version)
    return {name: version for name, version in EVIDENCE_OMP_VERSIONS.items() if version != running}


def pending_isolation(omp_version: str | None) -> dict[str, Any]:
    return {"state": "pending", "checked": False, "ok": None, "leaks": [], "warning": None,
            "omp_version": omp_version, "evidence_versions": dict(EVIDENCE_OMP_VERSIONS),
            "version_drift": _version_drift(omp_version), "roles": {}}


def summarize_isolation(results: Mapping[str, Mapping[str, Any]], omp_version: str | None) -> dict[str, Any]:
    """Aggregate per-role check results into the backend snapshot field."""
    summary = pending_isolation(omp_version)
    leaks = [f"{role}:{leak}" for role, item in results.items() for leak in item.get("leaks") or []]
    errors = [f"{role}: {item.get('error')}" for role, item in results.items() if item.get("error")]
    states = {item.get("state") for item in results.values()}
    state = "failed" if errors or "failed" in states or not results else ("leak" if leaks else "ok")
    warning = None
    if state == "leak":
        warning = "OMP isolation leak (ambient configuration loaded): " + ", ".join(leaks)
    elif state == "failed":
        warning = "OMP isolation check failed: " + ("; ".join(errors) or "no result")
    summary.update(state=state, checked=True, ok=state == "ok", leaks=leaks, warning=warning,
                   roles={role: dict(item) for role, item in results.items()})
    return summary
