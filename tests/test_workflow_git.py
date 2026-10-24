from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import powdrr_lift.workrr.git as workflow_git
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.git import (
    WorkflowGitState,
    claim_workflow_task,
    cleanup_workflow_run,
    create_task_worktree,
    create_workflow_worktree,
    inspect_workflow_run,
    integration_branch_name,
    load_workflow_git_state,
    new_publication_id,
    publication_branch_name,
    resolve_git_repository_root,
    save_workflow_git_state,
    synchronize_workflow_initialization,
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


def test_publication_branches_are_unique_and_stable_per_workflow() -> None:
    publication_id = new_publication_id()
    implementation_branch = publication_branch_name(
        "Feature Request 17", "implementation", publication_id
    )
    another_branch = publication_branch_name(
        "Feature Request 17", "implementation", new_publication_id()
    )

    assert implementation_branch.startswith("powdrr/feature-request-17-implementation-")
    assert implementation_branch != another_branch
    assert publication_branch_name("Feature Request 17", "implementation") == (
        "powdrr/feature-request-17-implementation"
    )
    assert (
        publication_branch_name("Feature Request 17", "plan-amendment", publication_id)
        == f"powdrr/feature-request-17-plan-amendment-{publication_id}"
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
        publication_id="pub-token",
        depends_on_workflows=("core",),
        fulfilled_without_pr_dependencies=("core",),
    )

    assert WorkflowGitState.from_data(state.to_data()) == state


def test_fulfilled_without_pr_dependency_does_not_require_a_merged_pr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = WorkflowGitState(
        proposed_pr_id="integration",
        base_branch="main",
        integration_branch="powdrr/integration",
        workflow_relative_directory="docs/workflows/feature",
        depends_on_workflows=("core",),
        fulfilled_without_pr_dependencies=("core",),
    )

    def unexpected_pr_lookup(*_args: Any) -> Any:
        pytest.fail("a fulfilled-without-PR dependency should not need PR lookup")

    monkeypatch.setattr(workflow_git, "_related_pull_requests", unexpected_pr_lookup)
    monkeypatch.setattr(
        workflow_git,
        "_related_workflow_attempt_pull_requests",
        unexpected_pr_lookup,
    )

    assert workflow_dependencies_completion(tmp_path, state) == (True, ())


def test_workflow_git_state_rejects_non_dependency_fulfillment() -> None:
    data = WorkflowGitState(
        proposed_pr_id="integration",
        base_branch="main",
        integration_branch="powdrr/integration",
        workflow_relative_directory="docs/workflows/feature",
        depends_on_workflows=("core",),
        fulfilled_without_pr_dependencies=("other",),
    ).to_data()

    with pytest.raises(ValueError, match="subset of depends_on_workflows"):
        WorkflowGitState.from_data(data)


def test_workflow_dependency_requires_merged_implementation_pull_request(
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
    responses = [
        [{"state": "OPEN"}, {"state": "OPEN"}],
        [{"state": "OPEN"}, {"state": "MERGED"}],
    ]
    requested_branches: list[list[str]] = []

    def related_pull_requests(
        _repo_root: Path, branches: list[str]
    ) -> tuple[list[dict[str, Any]], None]:
        requested_branches.append(branches)
        return responses.pop(0), None

    monkeypatch.setattr(
        workflow_git,
        "_related_pull_requests",
        related_pull_requests,
    )
    monkeypatch.setattr(
        workflow_git,
        "_related_workflow_attempt_pull_requests",
        lambda *_args: ([], None),
    )

    assert workflow_dependencies_completion(tmp_path, state) == (
        False,
        ("core: implementation PR for powdrr/core is not merged",),
    )
    assert workflow_dependencies_completion(tmp_path, state) == (True, ())
    assert requested_branches == [
        ["powdrr/core", "powdrr/core-implementation"],
        ["powdrr/core", "powdrr/core-implementation"],
    ]


def test_workflow_dependency_accepts_merged_worker_attempt_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = WorkflowGitState(
        proposed_pr_id="integration",
        base_branch="main",
        integration_branch="powdrr/integration-attempt-current",
        workflow_relative_directory="docs/workflows/feature",
        depends_on_workflows=("core",),
    )
    monkeypatch.setattr(
        workflow_git,
        "_related_pull_requests",
        lambda *_args: ([{"state": "CLOSED"}], None),
    )
    monkeypatch.setattr(
        workflow_git,
        "_related_workflow_attempt_pull_requests",
        lambda _repo, prefix: (
            ([{"state": "MERGED", "headRefName": f"{prefix}-attempt-old"}], None)
        ),
    )

    assert workflow_dependencies_completion(tmp_path, state) == (True, ())


def test_related_workflow_pr_lookup_includes_unique_publication_branches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        workflow_git,
        "_gh",
        lambda *_args: subprocess.CompletedProcess(
            ["gh", "pr", "list"],
            0,
            json.dumps(
                [
                    {
                        "state": "MERGED",
                        "headRefName": "powdrr/core-implementation-run-123",
                    },
                    {
                        "state": "OPEN",
                        "headRefName": "powdrr/core-plan-amendment-run-456",
                    },
                ]
            ),
            "",
        ),
    )

    pull_requests, error = workflow_git._related_workflow_attempt_pull_requests(
        tmp_path,
        "powdrr/core",
    )

    assert error is None
    assert [item["headRefName"] for item in pull_requests] == [
        "powdrr/core-implementation-run-123"
    ]


def test_force_synchronization_resets_stale_integration_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[Path, list[str]]] = []

    def fake_run_git(repo: Path, arguments: list[str]) -> str:
        calls.append((repo, arguments))
        if arguments == ["branch", "--show-current"]:
            return "powdrr/feature-17"
        return ""

    monkeypatch.setattr(workflow_git, "_run_git", fake_run_git)

    synchronize_workflow_initialization(tmp_path / "integration", tmp_path, force=True)

    assert calls == [
        (tmp_path.resolve(), ["branch", "--show-current"]),
        (
            (tmp_path / "integration").resolve(),
            ["reset", "--hard", "powdrr/feature-17"],
        ),
        (
            (tmp_path / "integration").resolve(),
            ["branch", "--show-current"],
        ),
        (
            (tmp_path / "integration").resolve(),
            [
                "push",
                "--force-with-lease",
                "--set-upstream",
                "origin",
                "powdrr/feature-17",
            ],
        ),
    ]


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
    assert integration_worktree.name == "powdrr-feature-17"
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


def test_issue_attempt_worktree_starts_fresh_from_base_branch(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("main\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "main baseline")
    main_commit = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    old_worktree, _ = create_workflow_worktree(tmp_path, "feature-17")
    (old_worktree / "stale-candidate.txt").write_text("stale\n", encoding="utf-8")
    _git(old_worktree, "add", "stale-candidate.txt")
    _git(old_worktree, "commit", "-m", "old attempt candidate")

    fresh_branch = "powdrr/feature-17-attempt-new"
    fresh_worktree, branch = create_workflow_worktree(
        tmp_path,
        "feature-17",
        branch_name=fresh_branch,
        worktree_name=fresh_branch,
    )

    assert branch == fresh_branch
    assert (fresh_worktree / "README.md").read_text(encoding="utf-8") == "main\n"
    assert not (fresh_worktree / "stale-candidate.txt").exists()
    assert (
        subprocess.run(
            ["git", "-C", str(fresh_worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == main_commit
    )


def test_create_workflow_worktree_resumes_remote_branch_and_syncs_latest_base(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo = tmp_path / "author"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "remote", "add", "origin", str(remote))
    plan = repo / "docs/plans/plan.yaml"
    workflow = repo / "docs/workflows/plan/pr-01.yaml"
    plan.parent.mkdir(parents=True)
    workflow.parent.mkdir(parents=True)
    plan.write_text("revision: original\n", encoding="utf-8")
    workflow.write_text("revision: original\n", encoding="utf-8")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-m", "initial plan")
    _git(repo, "push", "--set-upstream", "origin", "main")

    integration, branch = create_workflow_worktree(repo, "plan-pr-01")
    runtime = integration / "docs/workflows/plan/runtime/task.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text('{"status": "open"}\n', encoding="utf-8")
    _git(integration, "add", "docs/workflows/plan/runtime/task.json")
    _git(integration, "commit", "-m", "save workflow checkpoint")
    _git(integration, "push", "--set-upstream", "origin", branch)
    _git(repo, "switch", "main")
    plan.write_text("revision: amended\n", encoding="utf-8")
    workflow.write_text("revision: amended\n", encoding="utf-8")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-m", "amend reviewed plan")
    _git(repo, "push", "origin", "main")

    fresh = tmp_path / "fresh"
    subprocess.run(
        ["git", "clone", str(remote), str(fresh)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(fresh, "config", "user.email", "test@example.com")
    _git(fresh, "config", "user.name", "Test User")

    resumed, resumed_branch = create_workflow_worktree(fresh, "plan-pr-01")

    assert resumed_branch == branch
    assert plan.read_text(encoding="utf-8") == "revision: amended\n"
    assert (resumed / "docs/plans/plan.yaml").read_text(encoding="utf-8") == (
        "revision: amended\n"
    )
    assert (resumed / "docs/workflows/plan/pr-01.yaml").read_text(
        encoding="utf-8"
    ) == "revision: amended\n"
    assert (resumed / "docs/workflows/plan/runtime/task.json").read_text(
        encoding="utf-8"
    ) == '{"status": "open"}\n'
    for ref in (
        f"refs/remotes/origin/{branch}",
        "refs/remotes/origin/main",
    ):
        ancestor = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=resumed,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", ancestor, "HEAD"],
                cwd=resumed,
                capture_output=True,
                text=True,
            ).returncode
            == 0
        ), f"{ref} at {ancestor} is not an ancestor of the resumed branch"


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

    with pytest.raises(PowdrrExecutionError, match="task claim already exists"):
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
        PowdrrExecutionError, match="could not create task claim.*permission denied"
    ):
        claim_workflow_task(tmp_path, state, "task-001")


def test_validate_workflow_git_state_uses_attempt_branch_and_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")
    attempt_branch = "powdrr/feature-17-attempt-abc123"
    worktree, _ = create_workflow_worktree(
        tmp_path,
        "feature-17",
        branch_name=attempt_branch,
        worktree_name=attempt_branch,
    )
    workflow_dir = worktree / "docs" / "workflows" / "feature-17"
    workflow_dir.mkdir(parents=True)
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch=attempt_branch,
        workflow_relative_directory="docs/workflows/feature-17",
    )
    save_workflow_git_state(workflow_dir, state)
    monkeypatch.setattr(workflow_git, "_related_pull_requests", lambda *_: ([], None))
    monkeypatch.setattr(
        workflow_git, "_related_workflow_attempt_pull_requests", lambda *_: ([], None)
    )

    report = workflow_git.validate_workflow_git_state(
        tmp_path, state, "feature-17-task-001"
    )

    assert report["integration_branch"] == attempt_branch
    assert report["integration_worktree"] == str(worktree)
    assert report["integration_worktree_exists"] is True
    assert report["worktrees"] == [{"path": str(worktree), "branch": attempt_branch}]


def test_validate_workflow_git_state_uses_integration_branch_worktree_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
    workflow_dir = integration_worktree / "docs" / "workflows" / "feature-17"
    workflow_dir.mkdir(parents=True)
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch=integration_branch,
        workflow_relative_directory="docs/workflows/feature-17",
    )
    save_workflow_git_state(workflow_dir, state)
    monkeypatch.setattr(workflow_git, "_related_pull_requests", lambda *_: ([], None))
    monkeypatch.setattr(
        workflow_git, "_related_workflow_attempt_pull_requests", lambda *_: ([], None)
    )

    report = workflow_git.validate_workflow_git_state(
        tmp_path,
        state,
        "feature-17-task-001",
    )

    assert integration_branch == "powdrr/feature-17"
    assert integration_worktree.name == "powdrr-feature-17"
    assert report["integration_worktree"] == str(integration_worktree)


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
    _git(integration_worktree, "add", "docs/workflows/feature-17")
    _git(integration_worktree, "commit", "-m", "initialize workflow")
    task_worktree, task_branch = create_task_worktree(tmp_path, state, "task-001")
    claim_workflow_task(tmp_path, state, "task-001")
    (workflow_dir / "task-001.json").write_text(
        '{"task_id": "task-001", "status": "completed", '
        '"output_state": {"result": "done"}}\n',
        encoding="utf-8",
    )
    _git(integration_worktree, "add", "docs/workflows/feature-17")
    _git(integration_worktree, "commit", "-m", "complete task")

    report = inspect_workflow_run(tmp_path, "feature-17")

    assert report["integration_branch_exists"] is True
    assert report["integration_worktree_exists"] is True
    assert report["task_branches"] == [{"branch": task_branch, "integrated": True}]
    assert report["claim_refs"] == ["refs/agents/claims/feature-17/task-001"]

    cleaned = cleanup_workflow_run(tmp_path, "feature-17", report=report)

    assert cleaned["integration_checkpoint_preserved"] is True
    assert cleaned["integration_worktree_exists"] is True
    assert '"status": "in_progress"' in (workflow_dir / "task-001.json").read_text()
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


def test_cleanup_restores_a_task_locked_by_an_interrupted_worker(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(tmp_path, "remote", "add", "origin", str(remote))
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")
    integration_worktree, integration_branch = create_workflow_worktree(
        tmp_path, "feature-locked"
    )
    workflow_dir = integration_worktree / "docs" / "workflows" / "feature-locked"
    workflow_dir.mkdir(parents=True)
    state = WorkflowGitState(
        proposed_pr_id="feature-locked",
        base_branch="main",
        integration_branch=integration_branch,
        workflow_relative_directory="docs/workflows/feature-locked",
    )
    save_workflow_git_state(workflow_dir, state)
    task_file = workflow_dir / "task-001.json"
    task_file.write_text('{"task_id": "task-001", "status": "pending"}\n')
    _git(integration_worktree, "add", "docs/workflows/feature-locked")
    _git(integration_worktree, "commit", "-m", "initialize workflow")
    initial_checkpoint = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=integration_worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(
        integration_worktree,
        "push",
        "--set-upstream",
        "origin",
        integration_branch,
    )
    claim_workflow_task(tmp_path, state, "task-001")
    task_file.write_text('{"task_id": "task-001", "status": "locked"}\n')
    _git(integration_worktree, "add", "docs/workflows/feature-locked/task-001.json")
    _git(integration_worktree, "commit", "-m", "claim task")
    _git(integration_worktree, "push")

    report = inspect_workflow_run(tmp_path, "feature-locked")
    assert report["tasks"][0]["status"] == "locked"

    cleaned = cleanup_workflow_run(tmp_path, "feature-locked", report=report)

    assert cleaned["errors"] == []
    assert '{"task_id": "task-001", "status": "pending"}' in task_file.read_text()
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", integration_branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_head == initial_checkpoint
    assert (
        not subprocess.run(
            [
                "git",
                "show-ref",
                "--verify",
                "refs/agents/claims/feature-locked/task-001",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def test_cleanup_reopens_a_first_task_claim_without_prior_snapshot(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(tmp_path, "remote", "add", "origin", str(remote))
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")
    integration_worktree, integration_branch = create_workflow_worktree(
        tmp_path, "feature-first-claim"
    )
    workflow_dir = integration_worktree / "docs" / "workflows" / "feature-first-claim"
    workflow_dir.mkdir(parents=True)
    state = WorkflowGitState(
        proposed_pr_id="feature-first-claim",
        base_branch="main",
        integration_branch=integration_branch,
        workflow_relative_directory="docs/workflows/feature-first-claim",
    )
    save_workflow_git_state(workflow_dir, state)
    task_file = workflow_dir / "task-001.yaml"
    task_file.write_text("task_id: task-001\nstatus: locked\n", encoding="utf-8")
    _git(integration_worktree, "add", "docs/workflows/feature-first-claim")
    _git(integration_worktree, "commit", "-m", "initialize locked task")
    _git(
        integration_worktree,
        "push",
        "--set-upstream",
        "origin",
        integration_branch,
    )
    claim_workflow_task(tmp_path, state, "task-001")

    report = inspect_workflow_run(tmp_path, "feature-first-claim")
    assert report["tasks"][0]["status"] == "locked"

    cleaned = cleanup_workflow_run(tmp_path, "feature-first-claim", report=report)

    assert cleaned["errors"] == []
    assert "status: open" in task_file.read_text(encoding="utf-8")
    assert (
        subprocess.run(
            [
                "git",
                "show-ref",
                "--verify",
                "refs/agents/claims/feature-first-claim/task-001",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        ).returncode
        != 0
    )


def test_cleanup_removes_integration_artifacts_when_metadata_is_missing(
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

    report = inspect_workflow_run(tmp_path, "feature-17")
    assert report["inconsistencies"] == [
        "integration branch exists but its <workflow-id>-workflow.yaml "
        "metadata file is missing or invalid"
    ]

    cleaned = cleanup_workflow_run(tmp_path, "feature-17", report=report)

    assert cleaned["integration_checkpoint_preserved"] is False
    assert not integration_worktree.exists()
    assert (
        not subprocess.run(
            ["git", "show-ref", "--verify", f"refs/heads/{integration_branch}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def test_cleanup_restores_missing_metadata_from_configured_checkout(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")

    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch=integration_branch_name("feature-17"),
        workflow_relative_directory="docs/workflows/feature-17",
    )
    configured_workflow_dir = tmp_path / "docs" / "workflows" / "feature-17"
    configured_workflow_dir.mkdir(parents=True)
    save_workflow_git_state(configured_workflow_dir, state)
    (configured_workflow_dir / "task-001.json").write_text(
        '{"task_id": "task-001", "status": "open"}\n',
        encoding="utf-8",
    )
    _git(tmp_path, "add", "docs/workflows/feature-17")
    _git(tmp_path, "commit", "-m", "initialize workflow upstream")

    integration_worktree, integration_branch = create_workflow_worktree(
        tmp_path, "feature-17"
    )
    workflow_dir = integration_worktree / "docs" / "workflows" / "feature-17"

    (workflow_dir / "feature-17-workflow.yaml").unlink()
    (workflow_dir / "task-001.json").write_text(
        '{"task_id": "task-001", "status": "completed"}\n',
        encoding="utf-8",
    )
    _git(integration_worktree, "add", "docs/workflows/feature-17")
    _git(integration_worktree, "commit", "-m", "advance workflow")

    report = inspect_workflow_run(tmp_path, "feature-17")
    assert report["workflow_git_state"] is None

    cleaned = cleanup_workflow_run(tmp_path, "feature-17", report=report)

    assert cleaned["integration_checkpoint_preserved"] is True
    assert not any("workflow state" in error for error in cleaned["errors"])
    assert load_workflow_git_state(workflow_dir, "feature-17") == state
    assert '"status": "open"' in (workflow_dir / "task-001.json").read_text()


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
