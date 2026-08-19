"""Git-backed lifecycle state for durable workflow runs."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

WORKFLOW_GIT_STATE_FILENAME = ".workflow-git.json"


@dataclass(frozen=True, slots=True)
class WorkflowGitState:
    """The immutable Git identity of one workflow run."""

    proposed_pr_id: str
    base_branch: str
    integration_branch: str
    workflow_relative_directory: str

    def to_data(self) -> dict[str, str]:
        return {
            "proposed_pr_id": self.proposed_pr_id,
            "base_branch": self.base_branch,
            "integration_branch": self.integration_branch,
            "workflow_relative_directory": self.workflow_relative_directory,
        }

    @classmethod
    def from_data(cls, data: object) -> WorkflowGitState:
        if not isinstance(data, dict):
            raise ValueError("Workflow Git state must be an object.")
        values: dict[str, str] = {}
        for key in (
            "proposed_pr_id",
            "base_branch",
            "integration_branch",
            "workflow_relative_directory",
        ):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Workflow Git state requires {key!r}.")
            values[key] = value.strip()
        return cls(**values)


def workflow_git_state_path(workflow_directory: str | Path) -> Path:
    return Path(workflow_directory) / WORKFLOW_GIT_STATE_FILENAME


def load_workflow_git_state(workflow_directory: str | Path) -> WorkflowGitState | None:
    path = workflow_git_state_path(workflow_directory)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return WorkflowGitState.from_data(data)
    except ValueError:
        return None


def save_workflow_git_state(
    workflow_directory: str | Path,
    state: WorkflowGitState,
) -> Path:
    path = workflow_git_state_path(workflow_directory)
    path.write_text(
        json.dumps(state.to_data(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def slugify_workflow_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    if not slug:
        raise ValueError("The proposed PR id must contain a letter or digit.")
    return slug


def integration_branch_name(proposed_pr_id: str) -> str:
    return f"powdrr/{slugify_workflow_id(proposed_pr_id)}"


def task_branch_name(proposed_pr_id: str, task_id: str) -> str:
    return (
        f"powdrr/{slugify_workflow_id(proposed_pr_id)}-task/"
        f"{slugify_workflow_id(task_id)}"
    )


def workflow_worktree_path(repo_root: str | Path, proposed_pr_id: str) -> Path:
    return (
        Path(repo_root) / ".worktrees" / "powdrr" / slugify_workflow_id(proposed_pr_id)
    )


def task_worktree_path(
    repo_root: str | Path,
    proposed_pr_id: str,
    task_id: str,
) -> Path:
    return (
        workflow_worktree_path(repo_root, proposed_pr_id)
        / "tasks"
        / slugify_workflow_id(task_id)
    )


def create_workflow_worktree(
    repo_root: str | Path,
    proposed_pr_id: str,
    *,
    base_branch: str = "main",
) -> tuple[Path, str]:
    repo_root_path = Path(repo_root).resolve()
    branch = integration_branch_name(proposed_pr_id)
    worktree = workflow_worktree_path(repo_root_path, proposed_pr_id)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        if not (worktree / ".git").exists():
            raise RuntimeError(
                f"Workflow worktree path is not a Git worktree: {worktree}"
            )
        return worktree, branch
    if (
        _git(
            repo_root_path, ["show-ref", "--verify", f"refs/heads/{branch}"]
        ).returncode
        == 0
    ):
        _run_git(repo_root_path, ["worktree", "add", str(worktree), branch])
    else:
        _run_git(
            repo_root_path,
            ["worktree", "add", str(worktree), "-b", branch, base_branch],
        )
    return worktree, branch


def create_task_worktree(
    repo_root: str | Path,
    state: WorkflowGitState,
    task_id: str,
) -> tuple[Path, str]:
    repo_root_path = Path(repo_root).resolve()
    branch = task_branch_name(state.proposed_pr_id, task_id)
    worktree = task_worktree_path(repo_root_path, state.proposed_pr_id, task_id)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        if not (worktree / ".git").exists():
            raise RuntimeError(f"Task worktree path is not a Git worktree: {worktree}")
        return worktree, branch
    if (
        _git(
            repo_root_path, ["show-ref", "--verify", f"refs/heads/{branch}"]
        ).returncode
        == 0
    ):
        _run_git(repo_root_path, ["worktree", "add", str(worktree), branch])
    else:
        _run_git(
            repo_root_path,
            ["worktree", "add", str(worktree), "-b", branch, state.integration_branch],
        )
    return worktree, branch


def claim_workflow_task(
    repo_root: str | Path,
    state: WorkflowGitState,
    task_id: str,
) -> None:
    """Atomically claim a task using a Git ref compare-and-swap."""
    repo_root_path = Path(repo_root).resolve()
    integration_head = _run_git(
        repo_root_path,
        ["rev-parse", state.integration_branch],
    )
    claim_ref = (
        f"refs/agents/claims/{slugify_workflow_id(state.proposed_pr_id)}/"
        f"{slugify_workflow_id(task_id)}"
    )
    result = _git(
        repo_root_path,
        [
            "update-ref",
            claim_ref,
            integration_head,
            "0" * 40,
        ],
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Workflow task {task_id} is already claimed for {state.proposed_pr_id!r}."
        )


def commit_and_push_workflow_initialization(
    worktree: str | Path,
    workflow_directory: str | Path,
) -> None:
    """Commit the initial task graph and publish the integration branch."""
    worktree_path = Path(worktree).resolve()
    workflow_path = Path(workflow_directory).resolve()
    relative_workflow = workflow_path.relative_to(worktree_path)
    _run_git(worktree_path, ["add", str(relative_workflow)])
    _run_git(
        worktree_path,
        ["commit", "-m", "Initialize durable workflow run"],
    )
    branch = _run_git(worktree_path, ["branch", "--show-current"])
    _run_git(worktree_path, ["push", "--set-upstream", "origin", branch])


def _git(repo_root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_git(repo_root: Path, arguments: list[str]) -> str:
    result = _git(repo_root, arguments)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()
