"""Isolated scripted scenarios that exercise the production workflow-task agent."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.core import WorkflowInstance
from powdrr_lift.workflow_task_agent import (
    WorkflowTaskAgentConfig,
    _run_task_deterministic_pre_step,
    run_workflow_task,
)


class WorkflowTaskScenarioError(ValueError):
    """Raised for malformed isolated workflow-task scenario inputs."""


class _ScriptedWorkflowTaskClient:
    def __init__(self, responses: Sequence[Mapping[str, Any]]) -> None:
        self._responses = iter(dict(response) for response in responses)
        self.messages: list[list[dict[str, str]]] = []
        self.responses_served: list[dict[str, Any]] = []

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.messages.append(messages)
        try:
            response = next(self._responses)
            self.responses_served.append(response)
            return response
        except StopIteration as error:
            correction = _response_correction(messages)
            raise WorkflowTaskScenarioError(
                "The workflow task requested another scripted response. "
                f"Correction requested: {correction} "
                f"Request system prompt: {messages[0].get('content', '')[:120]!r} "
                f"Responses served: {json.dumps(self.responses_served, sort_keys=True)}"
            ) from error


def run_workflow_task_scenario(
    *,
    workflow_source: Path,
    responses: Sequence[Mapping[str, Any]],
    task_id: str | None,
    expected_output_state: Any,
    fixture_root: Path | None = None,
    run_all: bool = False,
) -> dict[str, Any]:
    """Run one real task, or every ready task, with scripted LLM output.

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
        source_tasks = WorkflowInstance.from_directory(workflow_dir).tasks
        target_task_id = task_id or source_tasks[-1].task_id
        source_task = next(
            (
                item
                for item in source_tasks
                if item.pre_step is not None
                and item.pre_step.action == "gather_context"
            ),
            None,
        )
        deterministic_state, _ = (
            _run_task_deterministic_pre_step(
                source_task, repo_root=repo_root, events=[]
            )
            if source_task is not None
            else (None, False)
        )
        client = _ScriptedWorkflowTaskClient(
            [_replace_pre_step_result(item, deterministic_state) for item in responses]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            exit_code = run_workflow_task(
                WorkflowTaskAgentConfig(
                    workflow_dir=workflow_dir,
                    repo_root=repo_root,
                    task_id=None if run_all else target_task_id,
                ),
                client=client,
                stdout=stdout,
                stderr=stderr,
            )
        except WorkflowTaskScenarioError as error:
            task_path = workflow_dir / f"{target_task_id}.yaml"
            raise WorkflowTaskScenarioError(
                f"{error}\nTask stdout:\n{stdout.getvalue()}\n"
                f"Task stderr:\n{stderr.getvalue()}\n"
                f"Persisted task:\n{task_path.read_text(encoding='utf-8')}"
            ) from error
        task = next(
            (
                item
                for item in WorkflowInstance.from_directory(workflow_dir).tasks
                if item.task_id == target_task_id
            ),
            None,
        )
        actual = task.output_state if task is not None else None
        expected = _replace_pre_step_result(expected_output_state, deterministic_state)
        final_tasks = WorkflowInstance.from_directory(workflow_dir).tasks
        return {
            "exit_code": exit_code,
            "task_status": task.status.value if task is not None else None,
            "output_state": actual,
            "output_matches": actual == expected,
            "roundtrips": len(client.messages),
            "all_tasks_completed": all(
                item.status.value == "completed" for item in final_tasks
            ),
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _replace_pre_step_result(value: Any, result: Any) -> Any:
    if value == "$deterministic_pre_step":
        return result
    if isinstance(value, Mapping):
        return {
            key: _replace_pre_step_result(item, result) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_pre_step_result(item, result) for item in value]
    return value


def _response_correction(messages: Sequence[Mapping[str, str]]) -> str:
    """Extract the latest repair guidance from a task-agent request."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        try:
            payload = json.loads(message["content"])
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        step_context = payload.get("step_context")
        if isinstance(step_context, list) and step_context:
            return str(step_context[-1])
    return "No repair guidance was included in the task-agent request."
