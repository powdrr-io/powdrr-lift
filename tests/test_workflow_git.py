from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import powdrr_lift.workflow_git as workflow_git
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
    workflow_dependencies_completion,
    workflow_id_from_task_id,
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
    assert workflow_id_from_task_id("feature-request-17-workflow-task-001") == (
        "feature-request-17"
    )


def test_workflow_git_state_loads_by_workflow_id_when_directory_is_shared(
    tmp_path: Path,
) -> None:
    workflow_dir = tmp_path / "docs" / "workflows" / "feature"
    workflow_dir.mkdir(parents=True)
    core_state = WorkflowGitState(
        proposed_pr_id="feature-core",
        base_branch="main",
        integration_branch="powdrr/feature-core",
        workflow_relative_directory="docs/workflows/feature",
    )
    integration_state = WorkflowGitState(
        proposed_pr_id="feature-integration",
        base_branch="main",
        integration_branch="powdrr/feature-integration",
        workflow_relative_directory="docs/workflows/feature",
    )

    core_path = save_workflow_git_state(workflow_dir, core_state)
    integration_path = save_workflow_git_state(workflow_dir, integration_state)

    assert core_path.name == "feature-core-workflow.yaml"
    assert integration_path.name == "feature-integration-workflow.yaml"
    assert load_workflow_git_state(workflow_dir, "feature-core") == core_state
    assert (
        load_workflow_git_state(workflow_dir, "feature-integration")
        == integration_state
    )
    assert load_workflow_git_state(workflow_dir) is None


def test_workflow_git_state_round_trips_workflow_dependencies() -> None:
    state = WorkflowGitState(
        proposed_pr_id="integration",
        base_branch="main",
        integration_branch="powdrr/integration",
        workflow_relative_directory="docs/workflows/feature",
        depends_on_workflows=("core",),
    )

    assert WorkflowGitState.from_data(state.to_data()) == state


def test_workflow_dependency_requires_merged_integration_pull_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = WorkflowGitState(
        proposed_pr_id="integration",
        base_branch="main",
        integration_branch="powdrr/integration",
        workflow_relative_directory="docs/workflows/feature",
        depends_on_workflows=("core",),
    )
    responses = [[{"state": "OPEN"}], [{"state": "MERGED"}]]

    monkeypatch.setattr(
        workflow_git,
        "_related_pull_requests",
        lambda _repo_root, _branches: (responses.pop(0), None),
    )

    assert workflow_dependencies_completion(tmp_path, state) == (
        False,
        ("core: integration PR for powdrr/core is not merged",),
    )
    assert workflow_dependencies_completion(tmp_path, state) == (True, ())


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


def test_claim_workflow_task_reports_ref_creation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")
    _, integration_branch = create_workflow_worktree(tmp_path, "feature-18")
    state = WorkflowGitState(
        proposed_pr_id="feature-18",
        base_branch="main",
        integration_branch=integration_branch,
        workflow_relative_directory="docs/workflows/feature-18",
    )
    original_git = workflow_git._git

    def failing_claim_git(
        repo_root: Path, arguments: list[str]
    ) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ["update-ref", "refs/agents/claims/feature-18/task-001"]:
            return subprocess.CompletedProcess(
                arguments,
                1,
                stdout="",
                stderr="permission denied",
            )
        if arguments[:3] == ["show-ref", "--verify", "--quiet"]:
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="")
        return original_git(repo_root, arguments)

    monkeypatch.setattr(workflow_git, "_git", failing_claim_git)

    with pytest.raises(
        RuntimeError, match="could not create task claim.*permission denied"
    ):
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


def test_inspection_reads_checkpoint_from_branch_when_worktree_is_missing(
    tmp_path: Path,
) -> None:
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
        '{"task_id": "task-001", "status": "open"}\n',
        encoding="utf-8",
    )
    _git(integration_worktree, "add", "docs/workflows/feature-17")
    _git(integration_worktree, "commit", "-m", "initialize workflow")
    task_worktree, task_branch = create_task_worktree(tmp_path, state, "task-001")
    claim_workflow_task(tmp_path, state, "task-001")
    _git(tmp_path, "worktree", "remove", "--force", str(task_worktree))
    _git(tmp_path, "worktree", "remove", "--force", str(integration_worktree))

    report = inspect_workflow_run(tmp_path, "feature-17")

    assert report["integration_branch_exists"] is True
    assert report["integration_worktree_exists"] is False
    assert report["workflow_git_state"] == state.to_data()
    assert report["workflow_git_state_source"] == (
        "docs/workflows/feature-17/feature-17-workflow.yaml"
    )
    assert report["task_branches"] == [{"branch": task_branch, "integrated": True}]
    assert report["tasks"] == [
        {
            "path": f"{integration_branch}:docs/workflows/feature-17/task-001.json",
            "task_id": "task-001",
            "status": "open",
        }
    ]

    cleaned = cleanup_workflow_run(tmp_path, "feature-17", report=report)

    assert cleaned["integration_checkpoint_preserved"] is True
    assert cleaned["integration_worktree_exists"] is False
    assert (
        not subprocess.run(
            ["git", "show-ref", "--verify", f"refs/heads/{task_branch}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        ).returncode
        == 0
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
