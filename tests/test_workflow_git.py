from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from powdrr_lift.workflow_git import (
    WorkflowGitState,
    claim_workflow_task,
    create_task_worktree,
    create_workflow_worktree,
    integration_branch_name,
    load_workflow_git_state,
    save_workflow_git_state,
    task_branch_name,
)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_workflow_git_state_round_trips_and_names_branches(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "docs" / "workflows" / "feature"
    workflow_dir.mkdir(parents=True)
    state = WorkflowGitState(
        proposed_pr_id="Feature Request 17",
        base_branch="main",
        integration_branch="powdrr/feature-request-17",
        workflow_relative_directory="docs/workflows/feature",
    )

    save_workflow_git_state(workflow_dir, state)

    assert load_workflow_git_state(workflow_dir) == state
    assert integration_branch_name("Feature Request 17") == (
        "powdrr/feature-request-17"
    )
    assert task_branch_name("Feature Request 17", "task-001") == (
        "powdrr/feature-request-17-task/task-001"
    )


def test_create_task_worktree_starts_from_integration_branch(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")
    integration_worktree, integration_branch = create_workflow_worktree(
        tmp_path,
        "feature-17",
    )
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch=integration_branch,
        workflow_relative_directory="docs/workflows/feature-17",
    )

    task_worktree, task_branch = create_task_worktree(tmp_path, state, "task-001")

    assert integration_worktree.is_dir()
    assert task_worktree.is_dir()
    assert task_branch == "powdrr/feature-17-task/task-001"
    assert (
        subprocess.run(
            ["git", "-C", str(task_worktree), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == task_branch
    )


def test_claim_workflow_task_is_an_atomic_git_ref(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")
    _, integration_branch = create_workflow_worktree(tmp_path, "feature-17")
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch=integration_branch,
        workflow_relative_directory="docs/workflows/feature-17",
    )

    claim_workflow_task(tmp_path, state, "task-001")

    with pytest.raises(RuntimeError, match="already claimed"):
        claim_workflow_task(tmp_path, state, "task-001")
