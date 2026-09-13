"""Agent-owned workflow execution context values."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    """Persisted workflow identity shared across agent executions."""

    worktree_root: Path
    branch_name: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    skill_name: str | None = None
    request: str | None = None

    def to_data(self) -> dict[str, object]:
        return {
            "worktree_root": str(self.worktree_root),
            "branch_name": self.branch_name,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "skill_name": self.skill_name,
            "request": self.request,
        }
