"""Reusable application boundary for one PR execution worker attempt."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from powdrr_lift.core import resolve_repo_root
from powdrr_lift.workrr.github_issue_queue import (
    GitHubIssueClient,
    PlannedWorkItem,
    ReviewedExecutionPlan,
    TaskCheckpointCallback,
    WorkerReport,
    run_worker_once,
)
from powdrr_lift.workrr.planned_workflow_task import (
    PlannedWorkflowTaskOptions,
    run_planned_workflow_task,
)


@dataclass(frozen=True, slots=True)
class PrExecutionSessionConfig:
    """Inputs needed to execute one eligible proposed PR."""

    repo_root: Path | None
    repository: str
    worker_id: str
    provider: str = "auto"
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


def run_pr_execution_worker(
    config: PrExecutionSessionConfig,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> WorkerReport:
    """Execute the first eligible PR item through the existing queue adapter.

    Queue policy, GitHub effects, checkpointing, worktree management, and task
    retries remain owned by their existing workflow modules. This function only
    adapts application configuration to that flow and returns its structured
    report to the CLI or a workspace dispatcher.
    """
    repo_root = resolve_repo_root(config.repo_root).resolve()
    output = stdout or sys.stdout
    diagnostics = stderr or sys.stderr
    options = PlannedWorkflowTaskOptions(
        provider=config.provider,
        repository=config.repository,
        api_key=config.api_key,
        base_url=config.base_url,
        max_roundtrips=config.max_roundtrips,
        context_compaction_threshold=config.context_compaction_threshold,
        verbose=config.verbose,
        code_agent=config.code_agent,
        opencode_executable=config.opencode_executable,
        opencode_model=config.opencode_model,
        minisweagent_executable=config.minisweagent_executable,
        minisweagent_model=config.minisweagent_model,
        coding_agent_timeout_seconds=config.coding_agent_timeout_seconds,
        coding_agent_validation_timeout_seconds=(
            config.coding_agent_validation_timeout_seconds
        ),
    )

    def run_task(
        workflow_path: Path,
        workflow_id: str,
        worker_id: str,
        item: PlannedWorkItem,
        plan: ReviewedExecutionPlan,
        fulfilled_without_pr_dependencies: tuple[str, ...],
        checkpoint_callback: TaskCheckpointCallback,
    ) -> int:
        return run_planned_workflow_task(
            workflow_path=workflow_path,
            workflow_id=workflow_id,
            worker_id=worker_id,
            item=item,
            plan=plan,
            fulfilled_without_pr_dependencies=fulfilled_without_pr_dependencies,
            checkpoint_callback=checkpoint_callback,
            repo_root=repo_root,
            options=options,
            stdout=output,
            stderr=diagnostics,
        )

    return run_worker_once(
        client=GitHubIssueClient(config.repository),
        repo_root=repo_root,
        worker_id=config.worker_id,
        run_task=run_task,
    )
