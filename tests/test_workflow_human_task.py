from __future__ import annotations

import io
import subprocess
from pathlib import Path

from powdrr_lift.core.workflow_task_specification import (
    AssigneeType,
    HumanRole,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
)
from powdrr_lift.workflow_git import (
    WorkflowGitState,
    create_workflow_worktree,
    save_workflow_git_state,
)
from powdrr_lift.workflow_human_task import (
    HumanTaskRunnerConfig,
    run_human_task,
)


def test_run_human_task_presents_context_and_records_answer(tmp_path: Path) -> None:
    workflow = WorkflowInstance.create(tmp_path / "workflow")
    workflow.add_task(
        WorkflowTask(
            task_id="review-decision",
            status=TaskStatus.OPEN,
            description="Choose the release channel.",
            details="Select the channel that matches the approved rollout plan.",
            complexity=TaskComplexity.LOW,
            input_state={"channels": ["stable", "preview"]},
            assignee_type=AssigneeType.HUMAN,
            assignee_role=HumanRole.DECIDER,
            output_state_type="release-decision",
        )
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = run_human_task(
        HumanTaskRunnerConfig(workflow_dir=workflow.directory),
        input_func=lambda prompt: (assert_prompt(prompt), "stable")[1],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    completed = WorkflowInstance.from_directory(workflow.directory).tasks[0]
    assert completed.status is TaskStatus.COMPLETED
    assert completed.output_state == {"answer": "stable"}
    assert "Claimed human task: review-decision" in stdout.getvalue()
    assert '"channels": [' in stdout.getvalue()
    assert "Completed human task: review-decision" in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_run_human_task_can_select_role_and_read_answer_file(tmp_path: Path) -> None:
    workflow = WorkflowInstance.create(tmp_path / "workflow")
    workflow.add_task(
        WorkflowTask(
            task_id="review-task",
            status=TaskStatus.OPEN,
            description="Review the result.",
            complexity=TaskComplexity.LOW,
            input_state={},
            assignee_type=AssigneeType.HUMAN,
            assignee_role=HumanRole.REVIEWER,
        )
    )
    answer_file = tmp_path / "answer.txt"
    answer_file.write_text("Approved with one follow-up.\n", encoding="utf-8")

    exit_code = run_human_task(
        HumanTaskRunnerConfig(
            workflow_dir=workflow.directory,
            task_id="review-task",
            assignee_role=HumanRole.REVIEWER,
            answer_file=answer_file,
        ),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    completed = WorkflowInstance.from_directory(workflow.directory).tasks[0]
    assert completed.output_state == {"answer": "Approved with one follow-up."}


def test_run_human_task_claims_git_task_and_publishes_completion(
    tmp_path: Path,
    monkeypatch,
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
    workflow = WorkflowInstance.create(workflow_dir)
    workflow.add_task(
        WorkflowTask(
            task_id="human-review",
            status=TaskStatus.OPEN,
            description="Approve the change.",
            complexity=TaskComplexity.LOW,
            input_state={"change": "new behavior"},
            assignee_type=AssigneeType.HUMAN,
            assignee_role=HumanRole.REVIEWER,
        )
    )
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch=integration_branch,
        workflow_relative_directory="docs/workflows/feature-17",
    )
    save_workflow_git_state(workflow_dir, state)
    _git(integration_worktree, "add", "docs/workflows/feature-17")
    _git(integration_worktree, "commit", "-m", "initialize workflow")

    published: dict[str, object] = {}

    def _fake_publish(repo_root, published_workflow, *, reason, stdout, **kwargs):
        published.update(
            repo_root=repo_root,
            workflow=published_workflow,
            reason=reason,
            stdout=stdout,
            kwargs=kwargs,
        )

    monkeypatch.setattr(
        "powdrr_lift.workflow_human_task.publish_workflow_progress",
        _fake_publish,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert (
        run_human_task(
            HumanTaskRunnerConfig(
                workflow_dir=workflow_dir,
                repo_root=tmp_path,
            ),
            input_func=lambda _: "Approved.",
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )

    assert "complete human task human-review" == published["reason"]
    assert published["repo_root"].name == "human-review"
    task_branch_workflow = (
        Path(published["repo_root"]) / "docs" / "workflows" / "feature-17"
    )
    completed = WorkflowInstance.from_directory(task_branch_workflow).tasks[0]
    assert completed.status is TaskStatus.COMPLETED
    assert completed.output_state == {"answer": "Approved."}
    claim = subprocess.run(
        [
            "git",
            "show-ref",
            "--verify",
            "refs/agents/claims/feature-17/human-review",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert claim.returncode == 0


def _git(repo: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def assert_prompt(prompt: str) -> bool:
    assert prompt == "\nYour answer: "
    return True
