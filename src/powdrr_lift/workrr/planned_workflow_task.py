"""Reusable execution adapter for one reviewed plan item."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from powdrr_service.planning.workflow_ids import slugify_workflow_id

from powdrr_lift.process.templates import instantiate_procedrr_process_workflow
from powdrr_lift.workrr.git import (
    WorkflowGitState,
    create_workflow_worktree,
    integration_branch_name,
    save_workflow_git_state,
)
from powdrr_lift.workrr.github_issue_plan_generation import (
    PlanGenerationError,
    load_plan_workflow_instance,
)
from powdrr_lift.workrr.github_issue_queue import (
    PlannedWorkItem,
    ReviewedExecutionPlan,
    TaskCheckpointCallback,
)
from powdrr_lift.workrr.task_agent import WorkflowTaskAgentConfig, run_workflow_task


@dataclass(frozen=True, slots=True)
class PlannedWorkflowTaskOptions:
    """Runtime settings shared by terminal and workspace plan-item adapters."""

    provider: str
    repository: str
    api_key: str | None = None
    base_url: str | None = None
    max_roundtrips: int = 30
    context_compaction_threshold: int = 0
    verbose: bool = False
    code_agent: str = "minisweagent"
    opencode_executable: str = "opencode"
    opencode_model: str | None = None
    minisweagent_executable: str = "mini"
    minisweagent_model: str = "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731"
    coding_agent_timeout_seconds: float = 1800.0
    coding_agent_validation_timeout_seconds: float = 1800.0


def run_planned_workflow_task(
    *,
    workflow_path: Path,
    workflow_id: str,
    worker_id: str,
    item: PlannedWorkItem,
    plan: ReviewedExecutionPlan,
    fulfilled_without_pr_dependencies: tuple[str, ...],
    checkpoint_callback: TaskCheckpointCallback,
    repo_root: Path,
    options: PlannedWorkflowTaskOptions,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Prepare and execute a single plan item without polling the GitHub queue."""
    root = repo_root.resolve()
    if workflow_path.is_dir():
        workflow_dir = workflow_path
        execution_root = root
    else:
        instance = load_plan_workflow_instance(workflow_path)
        if instance.workflow_id != workflow_id:
            raise PlanGenerationError(
                "workflow definition ID does not match its plan item"
            )
        workflow_inputs = dict(instance.inputs)
        workflow_context = dict(workflow_inputs["proposed_pr_context"])
        workflow_context.update(
            {
                "title": item.title,
                "objective": item.objective,
                "scope": list(item.scope),
                "acceptance_criteria": list(item.acceptance_criteria),
                "allowed_paths": list(item.allowed_paths),
            }
        )
        workflow_inputs["feature_description"] = item.objective
        if item.implementation_steps:
            workflow_context["implementation_steps"] = list(item.implementation_steps)
        if item.inline_adjustments:
            workflow_context["inline_plan_adjustments"] = [
                dict(adjustment) for adjustment in item.inline_adjustments
            ]
        workflow_inputs["proposed_pr_context"] = workflow_context

        execution_id = uuid.uuid4().hex[:12]
        workflow_instance_id = _plan_workflow_instance_id(
            workflow_id, workflow_inputs, worker_id, attempt_id=execution_id
        )
        integration_branch = integration_branch_name(
            f"{workflow_id}-attempt-{execution_id}"
        )
        integration_worktree, integration_branch = create_workflow_worktree(
            root,
            workflow_id,
            base_branch="main",
            branch_name=integration_branch,
            worktree_name=integration_branch,
        )
        runtime_relative_directory = (
            Path("docs")
            / "workflows"
            / slugify_workflow_id(plan.plan_id)
            / "runtime"
            / slugify_workflow_id(workflow_instance_id)
        )
        workflow_dir, _tasks = instantiate_procedrr_process_workflow(
            process_id=instance.process_id,
            process_directory=_bundled_procedrr_process_directory(),
            input_state=workflow_inputs,
            work_item_name=workflow_instance_id,
            workflow_instance_name=workflow_instance_id,
            output_root=(integration_worktree / runtime_relative_directory.parent),
        )
        save_workflow_git_state(
            workflow_dir,
            WorkflowGitState(
                proposed_pr_id=workflow_id,
                base_branch="main",
                integration_branch=integration_branch,
                workflow_relative_directory=(
                    workflow_dir.relative_to(integration_worktree).as_posix()
                ),
                publication_id=execution_id,
                depends_on_workflows=tuple(
                    planned.workflow_id
                    for dependency in item.dependencies
                    for planned in plan.items
                    if planned.item_id == dependency
                ),
                fulfilled_without_pr_dependencies=fulfilled_without_pr_dependencies,
            ),
        )
        execution_root = root

    return run_workflow_task(
        _task_config(
            workflow_dir=workflow_dir,
            repo_root=execution_root,
            workflow_id=workflow_id,
            issue_number=item.github_issue_number,
            checkpoint_callback=checkpoint_callback,
            options=options,
        ),
        stdout=stdout,
        stderr=stderr,
    )


def _task_config(
    *,
    workflow_dir: Path,
    repo_root: Path,
    workflow_id: str,
    issue_number: int | None,
    checkpoint_callback: TaskCheckpointCallback,
    options: PlannedWorkflowTaskOptions,
) -> WorkflowTaskAgentConfig:
    return WorkflowTaskAgentConfig(
        workflow_dir=workflow_dir,
        repo_root=repo_root,
        provider=options.provider,
        workflow_id=workflow_id,
        api_key=options.api_key,
        base_url=options.base_url,
        max_roundtrips=options.max_roundtrips,
        context_compaction_threshold=options.context_compaction_threshold,
        verbose=options.verbose,
        code_agent=options.code_agent,
        opencode_executable=options.opencode_executable,
        opencode_model=options.opencode_model,
        minisweagent_executable=options.minisweagent_executable,
        minisweagent_model=options.minisweagent_model,
        coding_agent_timeout_seconds=options.coding_agent_timeout_seconds,
        coding_agent_validation_timeout_seconds=(
            options.coding_agent_validation_timeout_seconds
        ),
        task_checkpoint_callback=checkpoint_callback,
        github_issue_number=issue_number,
        github_repository=options.repository,
    )


def _plan_workflow_instance_id(
    workflow_id: str,
    input_state: Mapping[str, Any],
    worker_id: str,
    *,
    attempt_id: str | None = None,
) -> str:
    serialized = json.dumps(
        input_state,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]
    suffix = f"-{slugify_workflow_id(worker_id)}"
    if attempt_id:
        suffix += f"-attempt-{slugify_workflow_id(attempt_id)}"
    return f"{workflow_id}-task-{fingerprint}{suffix}"


def _bundled_procedrr_process_directory() -> Path:
    """Locate Powdrr-Lift's process catalog, independent of target repository."""
    from powdrr_service.resources import bundled_process_directory

    return bundled_process_directory()
