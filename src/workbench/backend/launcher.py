"""Start-requirement checks and the production OMP launch plan.

The launcher injects only the G3 bridge extension and its role/token/generation
environment. It never reads, stores or copies OMP credentials: OMP keeps using
the user's own configuration and environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Mapping, Sequence

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


def default_bridge_extension() -> Path:
    # src/workbench/backend/launcher.py -> repository root
    return Path(__file__).resolve().parents[3] / "omp_bridge" / "g3" / "bridge.ts"


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
    extra = tuple(omp_args) or tuple(shlex.split(environment.get(OMP_ARGS_ENV, "")))
    return LaunchPlan(shell, candidate, omp_version(candidate), str(extension.resolve()), extra)


def omp_command(plan: LaunchPlan) -> list[str]:
    return [plan.omp, *plan.omp_args, "--extension", plan.bridge_extension]


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
