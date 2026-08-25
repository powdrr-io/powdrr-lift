"""Isolated scripted scenarios that exercise the production workflow-task agent."""

from __future__ import annotations

import io
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.core import WorkflowInstance
from powdrr_lift.workflow_scenario import _ScriptedWorkflowClient
from powdrr_lift.workflow_task_agent import WorkflowTaskAgentConfig, run_workflow_task


class WorkflowTaskScenarioError(ValueError):
    """Raised for malformed isolated workflow-task scenario inputs."""


def run_workflow_task_scenario(
    *,
    workflow_source: Path,
    responses: Sequence[Mapping[str, Any]],
    task_id: str,
    expected_output_state: Mapping[str, Any],
    fixture_root: Path | None = None,
) -> dict[str, Any]:
    """Copy a workflow fixture and run one real task with scripted LLM output.

    The isolated repository deliberately has no .git directory, so the task
    agent persists task state locally and cannot publish or create a PR.
    """
    if not workflow_source.is_dir():
        raise WorkflowTaskScenarioError(
            f"Workflow directory does not exist: {workflow_source}"
        )
    temporary = Path(tempfile.mkdtemp(prefix="powdrr-lift-task-scenario-"))
    repo_root = temporary / "repository"
    workflow_dir = repo_root / "workflow"
    try:
        if fixture_root is not None:
            shutil.copytree(
                fixture_root, repo_root, ignore=shutil.ignore_patterns(".git")
            )
        else:
            repo_root.mkdir()
        shutil.copytree(workflow_source, workflow_dir)
        client = _ScriptedWorkflowClient(responses)
        exit_code = run_workflow_task(
            WorkflowTaskAgentConfig(
                workflow_dir=workflow_dir, repo_root=repo_root, task_id=task_id
            ),
            client=client,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        task = next(
            (
                item
                for item in WorkflowInstance.from_directory(workflow_dir).tasks
                if item.task_id == task_id
            ),
            None,
        )
        actual = task.output_state if task is not None else None
        return {
            "exit_code": exit_code,
            "task_status": task.status.value if task is not None else None,
            "output_state": actual,
            "output_matches": actual == dict(expected_output_state),
            "roundtrips": len(client.messages),
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
