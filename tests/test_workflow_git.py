from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from powdrr_lift.workflow_git import (
    WorkflowGitState,
    claim_workflow_task,
    cleanup_workflow_run,
    create_task_worktree,
    create_workflow_worktree,
    inspect_workflow_run,
    integration_branch_name,
    load_workflow_git_state,
    resolve_git_repository_root,
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

    with pytest.raises(RuntimeError, match="task claim already exists"):
        claim_workflow_task(tmp_path, state, "task-001")


def test_inspection_and_cleanup_preserve_integration_checkpoint(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")
    integration_worktree, integration_branch = create_workflow_worktree(
        tmp_path, "feature-17"
    )
    workflow_dir = integration_worktree / "docs" / "workflows" / "feature-17"
    workflow_dir.mkdir(parents=True)
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch=integration_branch,
        workflow_relative_directory="docs/workflows/feature-17",
    )
    save_workflow_git_state(workflow_dir, state)
    (workflow_dir / "task-001.json").write_text(
        '{"task_id": "task-001", "status": "in_progress"}\n',
        encoding="utf-8",
    )
    task_worktree, task_branch = create_task_worktree(tmp_path, state, "task-001")
    claim_workflow_task(tmp_path, state, "task-001")

    report = inspect_workflow_run(tmp_path, "feature-17")

    assert report["integration_branch_exists"] is True
    assert report["integration_worktree_exists"] is True
    assert report["task_branches"] == [{"branch": task_branch, "integrated": True}]
    assert report["claim_refs"] == ["refs/agents/claims/feature-17/task-001"]

    cleaned = cleanup_workflow_run(tmp_path, "feature-17", report=report)

    assert cleaned["integration_checkpoint_preserved"] is True
    assert cleaned["integration_worktree_exists"] is True
    assert not task_worktree.exists()
    assert not any(
        line.strip() == task_branch
        for line in subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    assert (
        not subprocess.run(
            ["git", "show-ref", "--verify", "refs/agents/claims/feature-17/task-001"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def test_inspection_follows_registered_nested_integration_worktree(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")

    nested_worktree = (
        tmp_path
        / ".worktrees"
        / "workflow-chat"
        / ".worktrees"
        / "powdrr"
        / "feature-17"
    )
    nested_worktree.parent.mkdir(parents=True)
    _git(
        tmp_path,
        "worktree",
        "add",
        str(nested_worktree),
        "-b",
        "powdrr/feature-17",
        "main",
    )
    workflow_dir = nested_worktree / "docs" / "workflows" / "feature-17"
    workflow_dir.mkdir(parents=True)
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch="powdrr/feature-17",
        workflow_relative_directory="docs/workflows/feature-17",
    )
    save_workflow_git_state(workflow_dir, state)
    (workflow_dir / "task-001.json").write_text(
        '{"task_id": "task-001", "status": "open"}\n',
        encoding="utf-8",
    )

    report = inspect_workflow_run(tmp_path, "feature-17")

    assert report["integration_worktree"] == str(nested_worktree)
    assert report["integration_worktree_exists"] is True
    assert report["workflow_git_state"] == state.to_data()
    assert report["tasks"] == [
        {
            "path": str(workflow_dir / "task-001.json"),
            "task_id": "task-001",
            "status": "open",
        }
    ]


def test_resolve_git_repository_root_from_nested_worktree(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")

    nested_worktree = tmp_path / ".worktrees" / "chat"
    _git(tmp_path, "worktree", "add", str(nested_worktree), "-b", "chat", "main")

    assert resolve_git_repository_root(nested_worktree) == tmp_path.resolve()
