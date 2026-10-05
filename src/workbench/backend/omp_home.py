"""Workbench-owned OMP home (C-D64).

Both Workbench OMP processes run with a config root and agent dir under the
data dir (``<data dir>/omp-root`` and ``<data dir>/omp-root/agent``), so they
read no global OMP settings, skills, agents, sessions or memory of the user
and write no logs/run/cache/session files into the user's ``~/.omp``.
Only the auth store is shared: ``<agent dir>/agent.db`` is a symlink to the
user's own OMP ``agent.db``. SQLite resolves the link, so its -wal/-shm files
stay next to the user's file; OAuth refresh rotation therefore stays in one
place (a copy would fork it). The Workbench never opens, reads, copies or
hashes that file: it only ``lstat``s it and creates/repairs the link.

OMP 18.4.5 path rules (probe p27-home-probe-01 and the binary):
- config root = ``path.join(os.homedir(), PI_CONFIG_DIR || ".omp")``: an
  absolute value would become ``$HOME/<abs>``, so the root is passed relative to
  ``HOME`` (``..`` is normalised by ``path.join``, which also covers data dirs
  outside ``HOME``; the physical path is checked to be the same directory).
- agent dir = ``PI_CODING_AGENT_DIR`` unless a profile (``OMP_PROFILE``,
  ``PI_PROFILE``, ``--profile``) selects ``~/.omp/profiles/<p>/agent``.
- when the agent dir is ``<config root>/agent`` (our layout) OMP puts data,
  state and cache under ``$XDG_{DATA,STATE,CACHE}_HOME/omp`` if that directory
  exists, so those variables are not passed on in that case.
- the user's own auth store is resolved like the user's own omp resolves it
  (``user_omp_locations``): config root ``path.join(homedir, PI_CONFIG_DIR ||
  ".omp")``, a profile (``OMP_PROFILE``, else ``PI_PROFILE``; "" and
  "default" mean none) selects ``<root>/profiles/<p>``, otherwise an
  absolute ``PI_CODING_AGENT_DIR`` replaces ``<root>/agent``; the store is
  ``<agent dir>/agent.db`` unless the agent dir is the default one and
  ``$XDG_DATA_HOME/omp[/profiles/<p>]`` exists. Values whose meaning depends
  on the user's cwd or that OMP rejects are refused (never guessed).
- native addon (packages/natives loader-state): ``$XDG_DATA_HOME/omp/natives``
  when ``$XDG_DATA_HOME/omp`` exists, else ``os.homedir()/.omp/natives``;
  PI_CONFIG_DIR/PI_CODING_AGENT_DIR do not move it and OMP has no other
  setting for it. Every OMP start creates ``<natives>/<version>`` if missing,
  touches its mtime, extracts the embedded ``pi_natives.*.node`` files that
  are missing there and removes older version dirs. The only knob,
  XDG_DATA_HOME, would also move the home's data (agent.db included) out of
  the agent dir, so it stays withheld; ``natives_status`` reports in advance
  when a Workbench OMP will extract into the user's directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import pwd
import re
import stat
import time
from typing import Any, Mapping, Sequence

from workbench.backend.paths import DataDirError, ensure_private_dir, write_private_bytes

OMP_ROOT_NAME = "omp-root"
AGENT_DIR_NAME = "agent"
AUTH_STORE_NAME = "agent.db"
SQLITE_COMPANIONS = ("-wal", "-shm", "-journal")
CONFIG_NAME = "config.yml"
AGENTS_DIR_NAME = "agents"
TRANSPILER_CACHE_NAME = "bun-transpiler-cache"
# OMP 18.4.5 CURRENT_SETUP_VERSION: no first-run wizard in the Workbench home.
SETUP_VERSION = 2
OMP_APP_NAME = "omp"
USER_CONFIG_DIR = ".omp"
XDG_DIR_KEYS = ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
# The user's withheld XDG values travel in the isolation-check environment
# under this prefix so the check classifies the user's own OMP locations; the
# checked OMP process never receives them (launcher.check_isolation strips them).
USER_ENV_PREFIX = "WORKBENCH_USER_"
NATIVES_DIR_NAME = "natives"
NATIVES_ADDON_PREFIX = "pi_natives."
# Replaced by the Workbench home values (``OmpHome.environment``).
HOME_ENV_REPLACED = ("PI_CONFIG_DIR", "PI_CODING_AGENT_DIR", "BUN_RUNTIME_TRANSPILER_CACHE_PATH")
# Never passed to a Workbench OMP (and a leak when the checked OMP still has
# one): a profile selects another agent dir (with its own logins) under
# ~/.omp; the others point OMP config/session/data/db/cache at other
# locations. Audited against the OMP 18.4.5 binary (every PI_*/OMP_* name it
# reads): config overlays and session/db/cache dirs, plus the handoff
# variables OMP sets for its own child processes (eval kernels, daemons,
# workers), which would point a Workbench OMP's children at another OMP's
# session, artifacts or sockets. Not dropped (no OMP state location):
# PI_PACKAGE_DIR (install assets), OMP_NATIVE_LIBRARY_PATH (LD path),
# PI_TUI_WRITE_LOG/PI_TUI_TSP_RECORD (explicit debug output files), provider
# credentials and OMP_AUTH_BROKER_URL/_TOKEN/_ACCOUNT_POOL_FILE (auth, passed
# through like API keys).
HOME_ENV_FORBIDDEN = (
    "OMP_PROFILE", "PI_PROFILE",
    "PI_CONFIG_FILES", "PI_CODING_AGENT_SESSION_DIR", "OMP_TEXT_PREDICT_AGENT_DIR", "OMP_AUTORESEARCH_DB_DIR",
    "OMP_WORKTREE_DIR", "OMP_GITHUB_CACHE_DB", "OMP_COMMIT_CACHE_DB", "OMP_JUDGMENT_CACHE_DB",
    "OMP_AUTH_BROKER_SNAPSHOT_CACHE",
    "PI_SESSION_FILE", "PI_ARTIFACTS_DIR", "PI_EVAL_LOCAL_ROOTS", "PI_TOOL_BRIDGE_URL", "PI_TOOL_BRIDGE_TOKEN",
    "PI_TOOL_BRIDGE_SESSION", "OMP_DAEMON_PROJECT_DIR", "OMP_DAEMON_RUNTIME_DIR", "OMP_LSP_MUX_SOCKET",
    "OMP_LSP_MUX_PROJECT_DIR", "OMP_TEXT_PREDICT_SOCKET", "OMP_TINY_WORKER_SOCKET", "OMP_BLOB_BROKER_SOCKET",
    "OMP_BLOB_BROKER_CONFIG", "OMP_IDA_HOST_CONFIG",
)
HOME_ENV_DROP = HOME_ENV_FORBIDDEN + HOME_ENV_REPLACED
# The user's own variables that decide where the user's omp keeps its auth
# store; read for the link target only, never passed to a Workbench OMP.
USER_LOCATION_KEYS = ("PI_CONFIG_DIR", "PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE")
# OMP 18.4.5 profile names (packages/utils/src/dirs.ts).
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_WINDOWS_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?$", re.I)
# OMP 18.4.5 'omp --help' LLM provider key variables ("Core Providers" and
# "Additional LLM Providers"): with one of them set (non-empty) and no auth
# store, Workbench starts without the shared login (F4). Presence only.
PROVIDER_KEY_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "COPILOT_GITHUB_TOKEN", "AZURE_OPENAI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "XAI_API_KEY",
    "OPENROUTER_API_KEY", "KILO_API_KEY", "MISTRAL_API_KEY", "ZAI_API_KEY", "UMANS_AI_CODING_PLAN_API_KEY",
    "ABLITERATION_API_KEY", "MINIMAX_API_KEY", "OPENCODE_API_KEY", "CURSOR_ACCESS_TOKEN", "CLINE_API_KEY",
    "COMMAND_CODE_API_KEY", "CHARM_HYPER_API_KEY", "AI_GATEWAY_API_KEY", "STEPFUN_API_KEY", "HELMCODE_API_KEY",
    "WAFER_SERVERLESS_API_KEY", "YOLO_AUTO_API_KEY", "SINGULARITYAPI_DEV_API_KEY", "SINGULARITYAPI_TECH_API_KEY",
)

AUTH_GUIDANCE = (
    "OMP Workbench shares only your OMP login and found no usable OMP auth store at {store} ({detail}).\n"
    "Run 'omp' once outside Workbench and sign in with /login, then start Workbench again.\n"
    "No OMP login yet but a provider API key? With no auth store at that path, a provider API key "
    "environment variable from 'omp --help' (for example ANTHROPIC_API_KEY or OPENAI_API_KEY) lets "
    "Workbench start without the shared login; the key is passed to the Workbench OMPs as before.\n"
    "Workbench never creates this file and never reads its contents.")
ENV_KEY_NOTE = ("no OMP auth store at {store}: the Workbench OMPs start without the shared login and use the "
                "provider API key environment variable(s) {names}; run 'omp' once and /login to share your "
                "OMP login instead")
XDG_BASH_NOTE = ("{names} withheld from the Workbench OMPs (OMP would keep the Workbench home's data/state/cache "
                 "under {plural}); commands the OMP bash tool runs inside the Workbench panes therefore run without "
                 "{plural} too (tools that honour XDG_* fall back to ~/.local/share, ~/.local/state, ~/.cache). "
                 "The host shell pane keeps {plural}.")


class OmpHomeError(DataDirError):
    """The Workbench OMP home cannot be set up safely; nothing was started."""


def environment_home(environment: Mapping[str, str]) -> Path:
    """The home directory OMP will use (``os.homedir()``: ``HOME``, else the passwd entry)."""
    value = environment.get("HOME") or ""
    if value and os.path.isabs(value):
        return Path(os.path.normpath(value))
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def user_config_root(environment: Mapping[str, str]) -> Path:
    """The user's own OMP config root without any override (``~/.omp``)."""
    return environment_home(environment) / USER_CONFIG_DIR


def _xdg_omp_dir(environment: Mapping[str, str], key: str) -> Path | None:
    value = environment.get(key) or ""
    if not value or not os.path.isabs(value):
        return None
    candidate = Path(value) / OMP_APP_NAME
    return candidate if os.path.exists(candidate) else None


@dataclass(frozen=True, slots=True)
class UserOmpLocations:
    """Where the user's own omp keeps its config root, agent dir and auth store (paths only)."""

    config_root: Path
    agent_dir: Path
    auth_store: Path
    dirs: tuple[Path, ...]


def _unresolvable(key: str, value: str, reason: str) -> OmpHomeError:
    return OmpHomeError(
        f"cannot tell where your own omp keeps its OMP auth store: {key}={value!r} {reason}. Workbench links "
        f"the store your omp uses and does not guess; unset or fix {key} in the environment that starts Workbench "
        "(Workbench never passes it to its own OMPs).\nNothing was changed.")


def _profile(value: str, key: str, *, strict: bool) -> str | None:
    """OMP 18.4.5 ``KK``: "" and "default" are no profile; an invalid name is refused (``strict``) or none."""
    name = value.strip()
    if not name or name == "default":
        return None
    if name in (".", "..") or name.endswith(".") or not _PROFILE_NAME.match(name) or _WINDOWS_RESERVED.match(name):
        if strict:
            raise _unresolvable(key, value, "is not a valid OMP profile name, so omp would not use it as written")
        return None
    return name


def user_omp_locations(environment: Mapping[str, str]) -> UserOmpLocations:
    """The user's own omp 18.4.5 locations from the user's environment (``Xge`` in packages/utils dirs.ts).

    ``environment`` is the user's (never a Workbench OMP environment). Pure
    path logic plus ``exists`` on the XDG candidates OMP itself tests; raises
    ``OmpHomeError`` naming the variable when the result would depend on the
    user's cwd or on a value OMP rejects.
    """
    home = str(environment_home(environment))
    base = Path(os.path.normpath(home + os.sep + (environment.get("PI_CONFIG_DIR") or USER_CONFIG_DIR)))
    key = "OMP_PROFILE" if "OMP_PROFILE" in environment else "PI_PROFILE"
    profile = _profile(environment.get(key, ""), key, strict=True)
    config_root = base / "profiles" / profile if profile else base
    default_agent = config_root / AGENT_DIR_NAME
    agent_dir = default_agent
    override = environment.get("PI_CODING_AGENT_DIR") or ""
    if not profile and override:
        stale = _profile(environment.get("PI_PROFILE", ""), "PI_PROFILE", strict=False)
        if not (stale and override == str(base / "profiles" / stale / AGENT_DIR_NAME)):
            if not os.path.isabs(override):
                raise _unresolvable("PI_CODING_AGENT_DIR", override,
                                    "is relative, so omp resolves it against its current directory")
            agent_dir = Path(os.path.normpath(override))
    data = None
    if agent_dir == default_agent:
        value = environment.get("XDG_DATA_HOME") or ""
        if value and not os.path.isabs(value):
            raise _unresolvable("XDG_DATA_HOME", value, "is relative, so omp resolves it against its current directory")
        if value:
            candidate = Path(value) / OMP_APP_NAME
            if profile:
                candidate = candidate / "profiles" / profile
            data = candidate if os.path.exists(candidate) else None
    dirs = [user_config_root(environment), base, config_root, agent_dir]
    for xdg_key in XDG_DIR_KEYS:
        xdg = _xdg_omp_dir(environment, xdg_key)
        if xdg is not None:
            dirs.append(xdg)
    unique = tuple(dict.fromkeys(dirs))
    return UserOmpLocations(config_root, agent_dir, (data or agent_dir) / AUTH_STORE_NAME, unique)


def user_auth_store(environment: Mapping[str, str]) -> Path:
    """Where the user's own omp keeps ``agent.db`` (``user_omp_locations``)."""
    return user_omp_locations(environment).auth_store


def provider_key_names(environment: Mapping[str, str]) -> list[str]:
    """Names (never values) of the provider API key variables set non-empty in ``environment``."""
    return [key for key in PROVIDER_KEY_ENV if environment.get(key)]


@dataclass(frozen=True, slots=True)
class AuthStoreState:
    ok: bool
    detail: str
    identity: tuple[int, int] | None = None


def auth_store_state(path: Path) -> AuthStoreState:
    """``lstat`` only: the user's store must be a regular file this user owns."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return AuthStoreState(False, "missing")
    except NotADirectoryError:
        return AuthStoreState(False, "missing")
    except OSError as exc:
        return AuthStoreState(False, f"not accessible: {exc.strerror}")
    if stat.S_ISLNK(info.st_mode):
        return AuthStoreState(False, "a symlink, not a regular file")
    if not stat.S_ISREG(info.st_mode):
        return AuthStoreState(False, "not a regular file")
    if info.st_uid != os.geteuid():
        return AuthStoreState(False, "owned by another user")
    return AuthStoreState(True, "present", (info.st_dev, info.st_ino))


def auth_guidance(environment: Mapping[str, str]) -> str | None:
    """The login guidance when Workbench cannot start its OMPs with a login, else None.

    None with a usable store, and also with no store at all ("missing") when a
    provider API key variable is set (the OMPs then start without the link).
    """
    store = user_auth_store(environment)
    state = auth_store_state(store)
    if state.ok or (state.detail == "missing" and provider_key_names(environment)):
        return None
    return AUTH_GUIDANCE.format(store=store, detail=state.detail)


def omp_root(data_root: Path | str) -> Path:
    return Path(data_root) / OMP_ROOT_NAME


def _inside(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def config_dir_value(root: Path | str, environment: Mapping[str, str]) -> str:
    """``PI_CONFIG_DIR`` for ``root``: relative to HOME, as OMP joins it to ``os.homedir()``.

    Refused when the value would not name ``root`` both lexically (OMP's
    ``path.join``) and physically (e.g. a symlinked HOME), or when ``root`` is
    the user's own OMP config root or inside it.
    """
    home = str(environment_home(environment))
    target = os.path.abspath(root)
    value = os.path.relpath(target, home)
    if value in (".", "") or os.path.isabs(value):
        raise OmpHomeError(f"the Workbench OMP home {target} cannot be the home directory itself")
    if os.path.normpath(os.path.join(home, value)) != target or (
            os.path.realpath(home + os.sep + value) != os.path.realpath(target)):
        raise OmpHomeError(
            f"the Workbench OMP home {target} cannot be expressed relative to HOME={home} (OMP joins "
            "PI_CONFIG_DIR to HOME; a symlinked HOME or data dir path breaks that). Use a data dir whose "
            "path resolves the same way, for example under HOME.")
    user_roots = {os.path.realpath(item) for item in user_omp_locations(environment).dirs}
    physical = os.path.realpath(target)
    if any(_inside(physical, item) or _inside(item, physical) for item in user_roots):
        raise OmpHomeError(f"the Workbench OMP home {target} overlaps the user's OMP directory; "
                           "choose another data dir")
    return value


def home_config(skills_dir: Path | str, provider_ids: Sequence[str]) -> dict[str, Any]:
    """``<agent dir>/config.yml`` content, rewritten at every backend start.

    Minimal isolation (probe E12: same prompt/tools as the full overlay) plus
    the setup keys that keep OMP's first-run wizard away. The ``--config``
    overlays stay on the command line: project config overrides this file.
    """
    return {
        "setupVersion": SETUP_VERSION,
        "startup": {"setupWizard": False},
        "disabledProviders": list(provider_ids),
        "skills": {"customDirectories": [str(skills_dir)], "includeSkills": [], "ignoredSkills": []},
        "dev": {"autoqa": False},
        # User decision 2026-10-03: no browser automation (OMP 18.4.5: the eval `browser` prelude).
        "browser": {"enabled": False},
    }


def default_agents_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "omp_bridge" / "agents"


def install_agents(agent_dir: Path, agents_dir: Path | str) -> tuple[str, ...]:
    """Copy the Workbench-owned subagent definitions into ``<agent dir>/agents`` (C-D68).

    OMP reads user agent definitions from there (the Workbench home, never
    ``~/.omp``). Both OMPs see the directory; the per-role ``task.disabledAgents``
    decides who may use which. Rewritten at every start/restart (0600) so repo
    edits apply; a definition removed from the source is left (the launcher
    disables unknown definitions it finds there). Returns the file names.
    """
    try:
        sources = sorted(item for item in Path(agents_dir).iterdir() if item.suffix == ".md" and item.is_file())
    except OSError as exc:
        raise OmpHomeError(f"cannot read the Workbench agent definitions in {agents_dir}: {exc}") from exc
    target = ensure_private_dir(agent_dir / AGENTS_DIR_NAME)
    for source in sources:
        write_private_bytes(target / source.name, source.read_bytes())
    return tuple(item.name for item in sources)


def _config_text(content: Mapping[str, Any]) -> bytes:
    header = ("# Generated by OMP Workbench at every backend start (C-D64); changes here are replaced.\n"
              "# JSON is valid YAML.\n")
    return (header + json.dumps(content, indent=2) + "\n").encode()


@dataclass(frozen=True, slots=True)
class OmpHome:
    """A prepared Workbench OMP home (paths only; never credential content)."""

    root: Path
    agent_dir: Path
    config_dir: str
    auth_store: Path
    linked: bool
    auth_identity: tuple[int, int] | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    moved_aside: tuple[str, ...] = field(default_factory=tuple)

    @property
    def link(self) -> Path:
        return self.agent_dir / AUTH_STORE_NAME

    @property
    def config(self) -> Path:
        return self.agent_dir / CONFIG_NAME

    def environment(self) -> dict[str, str]:
        return {"PI_CONFIG_DIR": self.config_dir, "PI_CODING_AGENT_DIR": str(self.agent_dir),
                "BUN_RUNTIME_TRANSPILER_CACHE_PATH": str(self.root / TRANSPILER_CACHE_NAME)}


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _move_aside(agent_dir: Path, name: str, stamp: str) -> str:
    """Rename ``name`` inside the Workbench agent dir (never follows, never reads)."""
    target = f"{name}.moved-{stamp}"
    os.rename(agent_dir / name, agent_dir / target)
    return target


def _link_to(link: Path, target: Path) -> None:
    """Point ``link`` at ``target``; an existing link is replaced atomically (rename never follows)."""
    temporary = link.with_name(f".{link.name}-link-{os.getpid()}-{os.urandom(4).hex()}")
    os.symlink(target, temporary)
    try:
        os.replace(temporary, link)
    except OSError:
        os.unlink(temporary)
        raise


def prepare_omp_home(data_root: Path | str, environment: Mapping[str, str], *, skills_dir: Path | str,
                     provider_ids: Sequence[str], agents_dir: Path | str | None = None) -> OmpHome:
    """Create/repair the Workbench OMP home; idempotent, run at every backend start and OMP restart.

    Creates ``omp-root`` and ``omp-root/agent`` (0700) and rewrites
    ``config.yml`` and installs the Workbench subagent definitions
    (``agents_dir``, default ``omp_bridge/agents``) into ``agent/agents``.
    With a usable user auth store, ``agent.db`` becomes (or
    stays) a symlink to it; anything else named ``agent.db`` (an older
    Workbench-local store, a directory) and stale ``agent.db-wal/-shm/-journal``
    in the Workbench agent dir are renamed aside there. Without one, nothing is
    created outside the data dir and an existing link is removed (OMP would
    otherwise create the user's store through it); the result carries the
    login guidance as a note.
    """
    root = omp_root(data_root)
    config_dir = config_dir_value(root, environment)
    ensure_private_dir(root)
    agent_dir = ensure_private_dir(root / AGENT_DIR_NAME)
    write_private_bytes(agent_dir / CONFIG_NAME, _config_text(home_config(skills_dir, provider_ids)))
    install_agents(agent_dir, agents_dir if agents_dir is not None else default_agents_dir())
    store = user_auth_store(environment)
    state = auth_store_state(store)
    link = agent_dir / AUTH_STORE_NAME
    stamp = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{os.urandom(3).hex()}"
    notes: list[str] = []
    moved: list[str] = []
    try:
        current = os.lstat(link)
    except FileNotFoundError:
        current = None
    if state.ok:
        if current is not None and not stat.S_ISLNK(current.st_mode):
            moved.append(_move_aside(agent_dir, AUTH_STORE_NAME, stamp))
            current = None
        for suffix in SQLITE_COMPANIONS:  # stale companions of a former local store
            if _lexists(agent_dir / (AUTH_STORE_NAME + suffix)):
                moved.append(_move_aside(agent_dir, AUTH_STORE_NAME + suffix, stamp))
        if current is None or os.readlink(link) != str(store):
            _link_to(link, store)
        if moved:
            notes.append(f"moved aside in {agent_dir}: {', '.join(moved)} (agent.db now links to {store})")
    else:
        if current is not None and stat.S_ISLNK(current.st_mode):
            os.unlink(link)
            notes.append(f"removed the agent.db link in {agent_dir}: the user's auth store is {state.detail}")
        keys = provider_key_names(environment) if state.detail == "missing" else []
        if keys:
            notes.append(ENV_KEY_NOTE.format(store=store, names=", ".join(keys)))
        else:
            notes.append(AUTH_GUIDANCE.format(store=store, detail=state.detail).replace("\n", " "))
    withheld = [key for key in XDG_DIR_KEYS if environment.get(key) and _xdg_omp_dir(environment, key) is not None]
    if withheld:
        notes.append(XDG_BASH_NOTE.format(names=", ".join(withheld), plural="them" if len(withheld) > 1 else "it"))
    return OmpHome(root=root, agent_dir=agent_dir, config_dir=config_dir, auth_store=store, linked=state.ok,
                   auth_identity=state.identity, notes=tuple(notes), moved_aside=tuple(moved))


def verify_omp_home(home: OmpHome) -> list[str]:
    """Problems with the auth link (``lstat``/``readlink`` only); empty when shared as set up."""
    problems = []
    if home.linked:
        try:
            info = os.lstat(home.link)
        except FileNotFoundError:
            info = None
        if info is None:
            problems.append("auth_link:missing")
        elif not stat.S_ISLNK(info.st_mode):
            problems.append("auth_link:replaced_by_a_file")
        elif os.readlink(home.link) != str(home.auth_store):
            problems.append("auth_link:retargeted")
        state = auth_store_state(home.auth_store)
        if not state.ok:
            problems.append(f"auth_store:{state.detail.replace(' ', '_')}")
        elif state.identity != home.auth_identity:
            problems.append("auth_store:replaced")
    for suffix in SQLITE_COMPANIONS:
        if _lexists(home.agent_dir / (AUTH_STORE_NAME + suffix)):
            problems.append(f"auth_link:split:{AUTH_STORE_NAME + suffix}")
    return problems


def home_environment(base: Mapping[str, str], home: Mapping[str, str] | None) -> dict[str, str]:
    """``base`` without the overrides OMP must not see, plus the Workbench home values."""
    env = {key: value for key, value in base.items() if key not in HOME_ENV_DROP}
    if home is not None:
        for key in XDG_DIR_KEYS:
            if _xdg_omp_dir(base, key) is not None:
                env.pop(key, None)
        env.update(home)
    return env


def withheld_user_values(base: Mapping[str, str]) -> dict[str, str]:
    """The user's values ``home_environment`` withholds, keyed ``WORKBENCH_USER_<KEY>``.

    The XDG values OMP would use, and the user's own location variables
    (``USER_LOCATION_KEYS``) so the check finds the user's store and dirs.
    """
    values = {USER_ENV_PREFIX + key: base[key] for key in XDG_DIR_KEYS
              if base.get(key) and _xdg_omp_dir(base, key) is not None}
    values.update({USER_ENV_PREFIX + key: base[key] for key in USER_LOCATION_KEYS if key in base})
    return values


def user_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """The user's original OMP-relevant environment behind a Workbench OMP environment."""
    env = {key: value for key, value in environment.items()
           if not key.startswith(USER_ENV_PREFIX) and key not in USER_LOCATION_KEYS}
    for key in XDG_DIR_KEYS:
        value = environment.get(USER_ENV_PREFIX + key)
        if value:
            env[key] = value
    for key in USER_LOCATION_KEYS:
        if USER_ENV_PREFIX + key in environment:
            env[key] = environment[USER_ENV_PREFIX + key]
    return env


def home_env_leaks(environment: Mapping[str, str]) -> list[str]:
    """``env:<KEY>`` for every forbidden override still in a Workbench OMP's environment."""
    return [f"env:{key}" for key in HOME_ENV_FORBIDDEN if key in environment]


def without_user_values(environment: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in environment.items() if not key.startswith(USER_ENV_PREFIX)}


def natives_dir(environment: Mapping[str, str]) -> Path:
    """OMP 18.4.5's native addon dir for an OMP started with ``environment``."""
    data = _xdg_omp_dir(environment, "XDG_DATA_HOME")
    return (data if data is not None else user_config_root(environment)) / NATIVES_DIR_NAME


def _addon_present(directory: Path) -> bool:
    """A ``pi_natives.*.node`` regular file in ``directory`` (names and types only)."""
    try:
        with os.scandir(directory) as entries:
            return any(entry.name.startswith(NATIVES_ADDON_PREFIX) and entry.name.endswith(".node")
                       and entry.is_file(follow_symlinks=False) for entry in entries)
    except OSError:
        return False


def natives_status(user_env: Mapping[str, str], omp_env: Mapping[str, str], omp_version: str | None) -> dict[str, Any]:
    """Where a Workbench OMP keeps its native addon and whether it would extract it into a user location.

    ``user_env`` is the user's environment, ``omp_env`` the Workbench OMP's.
    ``split``: the Workbench OMP uses another natives dir than the user's own
    OMP (an XDG user: XDG_DATA_HOME is withheld). ``note`` is set when the
    Workbench OMP would create the version dir or extract the addon into one
    of the user's OMP directories (lstat/scandir only; nothing is written).
    """
    match = re.search(r"(\d+\.\d+\.\d+)", omp_version or "")
    version = match.group(1) if match else None
    wb_dir = natives_dir(omp_env)
    user_dir = natives_dir(user_env)
    status: dict[str, Any] = {"version": version, "dir": None, "user_dir": None, "present": None,
                              "split": wb_dir != user_dir, "note": None}
    if version is None:
        return status
    target, own = wb_dir / version, user_dir / version
    status.update(dir=str(target), user_dir=str(own), present=_addon_present(target))
    if status["present"]:
        return status
    if status["split"]:
        status["note"] = (
            f"OMP {version} takes its native addon dir only from XDG_DATA_HOME or HOME; Workbench withholds "
            f"XDG_DATA_HOME (OMP would also keep the Workbench home's data under it), so the Workbench OMPs "
            f"create {target} and extract the addon there (new files outside your own {own}).")
    else:
        status["note"] = (
            f"OMP {version} has not extracted its native addon into {target} yet: the first Workbench OMP "
            f"extracts it there (the same files your own omp {version} extracts; OMP has no setting to move "
            "this directory).")
    return status


def observe_open_paths(pid: int) -> list[str]:
    """Paths of the file descriptors a process we started holds open (link names only)."""
    paths = set()
    directory = f"/proc/{pid}/fd"
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    for entry in entries:
        try:
            target = os.readlink(f"{directory}/{entry}")
        except OSError:
            continue
        if target.startswith("/"):
            paths.add(target.removesuffix(" (deleted)"))
    return sorted(paths)


def classify_open_paths(paths: Sequence[str], environment: Mapping[str, str]) -> dict[str, Any]:
    """Where an OMP started with ``environment`` keeps its files open.

    ``user_paths`` are files under the user's own OMP directories other than
    the shared auth store and its SQLite companions (a leak of the home split).
    The user's directories and store are resolved from the user's original
    environment (``user_environment``: XDG values the OMP did not receive).
    """
    home = environment_home(environment)
    config_dir = environment.get("PI_CONFIG_DIR") or USER_CONFIG_DIR
    root = os.path.realpath(os.path.normpath(os.path.join(home, config_dir)))
    agent = os.path.realpath(environment.get("PI_CODING_AGENT_DIR") or os.path.join(root, AGENT_DIR_NAME))
    user_env = user_environment(environment)
    try:
        locations = user_omp_locations(user_env)
    except OmpHomeError:  # refused at start; never guess here either: no override
        locations = user_omp_locations({k: v for k, v in user_env.items()
                                        if k not in USER_LOCATION_KEYS and k != "XDG_DATA_HOME"})
    store = os.path.realpath(locations.auth_store)
    shared = {store + suffix for suffix in ("", *SQLITE_COMPANIONS)}
    user_dirs = {os.path.realpath(item) for item in locations.dirs}
    user_paths = [path for path in paths if path not in shared and any(_inside(path, item) for item in user_dirs)]
    return {
        "config_root": root, "agent_dir": agent,
        "config_root_in_use": any(_inside(path, root) and not _inside(path, agent) for path in paths),
        "agent_dir_in_use": any(_inside(path, agent) for path in paths),
        "auth_store_open": any(path == store for path in paths),
        "user_paths": user_paths,
    }
