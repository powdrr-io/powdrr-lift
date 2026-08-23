"""Interactive runner for durable workflow tasks assigned to humans."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from powdrr_lift.core.workflow_task_specification import (
    AssigneeType,
    HumanRole,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
)
from powdrr_lift.workflow_git import (
    WorkflowGitInconsistency,
    claim_workflow_task,
    create_workflow_worktree,
    load_workflow_git_state,
    resolve_git_repository_root,
    validate_workflow_git_state,
    workflow_id_from_task_id,
)
from powdrr_lift.workflow_task_agent import (
    _open_final_workflow_pull_request,
    publish_workflow_progress,
)


@dataclass(frozen=True, slots=True)
class HumanTaskRunnerConfig:
    workflow_dir: Path
    repo_root: Path | None = None
    task_id: str | None = None
    assignee_role: HumanRole | None = None
    answer: str | None = None
    answer_file: Path | None = None


def run_human_task(
    config: HumanTaskRunnerConfig,
    *,
    input_func: Callable[[str], str] = input,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Claim, present, and complete one ready human workflow task."""
    configured_repo_root = _resolve_repo_root(config.repo_root)
    configured_workflow_dir = config.workflow_dir.resolve()
    configured_workflow = WorkflowInstance.from_directory(configured_workflow_dir)
    configured_task = _select_human_task(
        configured_workflow,
        config.task_id,
        config.assignee_role,
    )
    if configured_task is None:
        print("No ready human task found.", file=stderr)
        return 1

    configured_git_state = load_workflow_git_state(
        configured_workflow_dir,
        workflow_id=workflow_id_from_task_id(configured_task.task_id),
    )
    repo_root = configured_repo_root
    workflow_dir = configured_workflow_dir
    if configured_git_state is not None:
        project_root = resolve_git_repository_root(configured_repo_root)
        try:
            integration_worktree, _integration_branch = create_workflow_worktree(
                project_root,
                configured_git_state.proposed_pr_id,
                base_branch=configured_git_state.base_branch,
            )
            _ = validate_workflow_git_state(
                project_root,
                configured_git_state,
                configured_task.task_id,
            )
            claim_workflow_task(
                project_root,
                configured_git_state,
                configured_task.task_id,
            )
            repo_root = integration_worktree
            workflow_dir = repo_root / configured_git_state.workflow_relative_directory
        except WorkflowGitInconsistency as exc:
            print(
                "Workflow Git state is inconsistent; no human task was started.",
                file=stderr,
            )
            print(str(exc), file=stderr)
            return 2
        print(
            f"Using workflow integration worktree: {repo_root}",
            file=stdout,
            flush=True,
        )

    workflow = WorkflowInstance.from_directory(workflow_dir)
    task = _select_human_task(workflow, configured_task.task_id, None)
    if task is None:
        print(
            f"Human task is no longer ready: {configured_task.task_id}",
            file=stderr,
        )
        return 2
    task = workflow.claim_task(task.task_id)
    print(f"Claimed human task: {task.task_id}", file=stdout)
    if configured_git_state is not None:
        publish_workflow_progress(
            repo_root,
            workflow,
            reason=f"claim human task {task.task_id}",
            stdout=stdout,
            open_pull_request=False,
        )
    _present_task(task, workflow, stdout)
    answer = _read_answer(config, input_func)
    completed = workflow.complete_task(task.task_id, {"answer": answer})
    print(f"Completed human task: {completed.task_id}", file=stdout)
    if configured_git_state is not None:
        publish_workflow_progress(
            repo_root,
            workflow,
            reason=f"complete human task {completed.task_id}",
            stdout=stdout,
            open_pull_request=False,
        )
        workflow = WorkflowInstance.from_directory(workflow_dir)
        if workflow.tasks and all(
            item.status is TaskStatus.COMPLETED for item in workflow.tasks
        ):
            _open_final_workflow_pull_request(
                repo_root,
                workflow,
                configured_git_state,
                stdout=stdout,
            )
    return 0


def _resolve_repo_root(repo_root: Path | None) -> Path:
    if repo_root is not None:
        return repo_root.resolve()
    return resolve_git_repository_root(Path.cwd())


def _select_human_task(
    workflow: WorkflowInstance,
    task_id: str | None,
    assignee_role: HumanRole | None,
) -> WorkflowTask | None:
    ready = workflow.ready_tasks(
        assignee_type=AssigneeType.HUMAN,
        assignee_role=assignee_role,
    )
    if task_id is None:
        return ready[0] if ready else None
    selected = next((task for task in ready if task.task_id == task_id), None)
    if selected is None:
        raise ValueError(f"Task is not a ready human task: {task_id}")
    return selected


def _present_task(
    task: WorkflowTask,
    workflow: WorkflowInstance,
    stdout: TextIO,
) -> None:
    print("\nHuman task", file=stdout)
    print(json.dumps(task.to_data(), indent=2, ensure_ascii=False), file=stdout)
    print("\nTask context", file=stdout)
    print(
        json.dumps(workflow.task_context(task.task_id), indent=2, ensure_ascii=False),
        file=stdout,
    )


def _read_answer(
    config: HumanTaskRunnerConfig,
    input_func: Callable[[str], str],
) -> str:
    if config.answer is not None and config.answer_file is not None:
        raise ValueError("Use only one of --answer or --answer-file.")
    if config.answer is not None:
        answer = config.answer
    elif config.answer_file is not None:
        answer = config.answer_file.read_text(encoding="utf-8")
    else:
        answer = input_func("\nYour answer: ")
    if not answer.strip():
        raise ValueError("Human answer must not be empty.")
    return answer.strip()


__all__ = ["HumanTaskRunnerConfig", "run_human_task"]
