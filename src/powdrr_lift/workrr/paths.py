"""Filesystem boundary helpers shared by workflow agents."""

from __future__ import annotations

from pathlib import Path

from powdrr_lift.errors import PowdrrExecutionError


def is_dedicated_worktree(repo_root: Path) -> bool:
    """Return whether a repository path represents a dedicated worktree."""
    return ".worktrees" in repo_root.parts or (repo_root / ".git").is_file()


def resolve_project_root(configured_repo_root: Path, worktree_root: Path) -> Path:
    """Return the primary checkout root used for shared local model storage."""
    if not is_dedicated_worktree(configured_repo_root):
        return configured_repo_root
    worktree_parts = worktree_root.parts
    worktree_marker = ".worktrees"
    if worktree_marker not in worktree_parts:
        raise PowdrrExecutionError(
            f"Could not determine project root for worktree {worktree_root}."
        )
    marker_index = worktree_parts.index(worktree_marker)
    if marker_index == 0:
        raise PowdrrExecutionError(
            f"Could not determine project root for worktree {worktree_root}."
        )
    return Path(*worktree_parts[:marker_index])


def resolve_worktree_file_path(file_path_value: str, worktree_root: Path) -> Path:
    """Resolve a user-provided path while enforcing the worktree boundary."""
    resolved_path = Path(file_path_value.strip())
    if resolved_path.is_absolute():
        candidate_path = resolved_path.resolve(strict=False)
    else:
        candidate_path = (worktree_root / resolved_path).resolve(strict=False)

    resolved_worktree_root = worktree_root.resolve(strict=False)
    if not candidate_path.is_relative_to(resolved_worktree_root):
        raise PowdrrExecutionError(
            f"Workflow edit action file_path must stay within {resolved_worktree_root}."
        )
    return candidate_path
