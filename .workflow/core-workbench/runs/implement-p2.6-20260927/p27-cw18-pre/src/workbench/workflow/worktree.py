"""One explicit Git worktree preparation step; no lifecycle automation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


class WorktreePreparationError(RuntimeError):
    def __init__(self, stage: str, detail: str, target: Path | None = None):
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail
        self.target = target


@dataclass(frozen=True)
class PreparedWorktree:
    source: Path
    path: Path
    commit: str
    source_status_before: str
    source_status_after: str


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreePreparationError("git_unavailable", type(exc).__name__) from exc
    if result.returncode:
        raise WorktreePreparationError("git_failed", result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def prepare_execution_worktree(source: str | Path, commit: str, target: str | Path) -> PreparedWorktree:
    """Create one detached execution worktree from a full commit OID.

    The source may be dirty. No source file, existing target, worktree, or Git
    metadata is removed on failure; callers must report and choose a safe next
    step instead of falling back to the development directory.
    """
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise WorktreePreparationError("commit", "a full explicit commit OID is required")
    source_path = Path(source).resolve()
    requested_target = Path(target).absolute()
    if requested_target.exists() or requested_target.is_symlink():
        raise WorktreePreparationError("target", "refusing an existing execution path", requested_target)
    target_path = requested_target.resolve()
    if not source_path.is_dir():
        raise WorktreePreparationError("source", "source directory does not exist", target_path)
    root = Path(_git(source_path, "rev-parse", "--show-toplevel")).resolve()
    if _git(root, "rev-parse", "--is-bare-repository") != "false":
        raise WorktreePreparationError("source", "bare repository is not an execution source", target_path)
    if target_path == root or target_path.is_relative_to(root):
        raise WorktreePreparationError("target", "execution path must be outside the development worktree", target_path)
    if not target_path.parent.is_dir():
        raise WorktreePreparationError("target", "execution parent directory does not exist", target_path)
    if _git(root, "cat-file", "-t", commit) != "commit":
        raise WorktreePreparationError("commit", "OID does not name a commit", target_path)
    before = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    try:
        _git(root, "worktree", "add", "--detach", str(target_path), commit)
        actual_root = Path(_git(target_path, "rev-parse", "--show-toplevel")).resolve()
        actual_commit = _git(target_path, "rev-parse", "HEAD")
        if actual_root != target_path or actual_commit != commit or _git(target_path, "status", "--porcelain=v1"):
            raise WorktreePreparationError("verify", "new worktree identity or cleanliness is unconfirmed", target_path)
    except WorktreePreparationError as exc:
        raise WorktreePreparationError(exc.stage, exc.detail, target_path) from exc
    after = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if after != before:
        raise WorktreePreparationError("source_changed", "source status changed during preparation", target_path)
    return PreparedWorktree(root, target_path, commit, before, after)
