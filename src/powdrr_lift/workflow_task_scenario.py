"""Isolated scripted scenarios that exercise the production workflow-task agent."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from powdrr_lift.core import WorkflowInstance
from powdrr_lift.workflow_task_agent import (
    WorkflowTaskAgentConfig,
    _build_workflow_client,
    _run_task_deterministic_pre_step,
    run_workflow_task,
)


class WorkflowTaskScenarioError(ValueError):
    """Raised for malformed isolated workflow-task scenario inputs."""


class LiveWorkflowTaskExchangeRecorder:
    """Capture complete exchanges while delegating to a real LLM client."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.exchanges: list[dict[str, Any]] = []

    @property
    def messages(self) -> list[list[dict[str, str]]]:
        return [exchange["input"] for exchange in self.exchanges]

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "input": [dict(message) for message in messages],
        }
        try:
            response = self._client.complete_json(messages)
        except Exception as exc:
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            self.exchanges.append(record)
            raise
        record["output"] = response
        usage = getattr(self._client, "last_usage", None)
        if isinstance(usage, Mapping):
            record["usage"] = dict(usage)
        self.exchanges.append(record)
        return response


class _TeeTextIO:
    """Capture live-run output for the report while showing it to the operator."""

    def __init__(self, capture: io.StringIO, display: Any) -> None:
        self._capture = capture
        self._display = display

    def write(self, value: str) -> int:
        self._capture.write(value)
        written = self._display.write(value)
        self._display.flush()
        return written

    def flush(self) -> None:
        self._capture.flush()
        self._display.flush()


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
    skill_definitions_source: Path | None = None,
    responses: Sequence[Mapping[str, Any]],
    task_id: str | None,
    expected_output_state: Any,
    fixture_root: Path | None = None,
    run_all: bool = False,
    live_provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    max_roundtrips: int | None = None,
    max_stalled_roundtrips: int = 3,
    verbose: bool = False,
    stream_live: bool = False,
) -> dict[str, Any]:
    """Run one real task, or every ready task, with scripted LLM output.

    The isolated repository has a local Git baseline so Git-facing workflow
    tasks exercise the same status and diff behavior as production. Publishing
    remains disabled by the scenario's GitHub intrinsic stub.
    """
    if not workflow_source.is_dir():
        raise WorkflowTaskScenarioError(
            f"Workflow directory does not exist: {workflow_source}"
        )
    temporary = Path(tempfile.mkdtemp(prefix="powdrr-lift-task-scenario-"))
    repo_root = temporary / "repository"
    previous_uv_cache_dir = os.environ.get("UV_CACHE_DIR")
    os.environ.setdefault("UV_CACHE_DIR", str(temporary / "uv-cache"))
    workflow_dir = repo_root / "workflow"
    try:
        if fixture_root is not None:
            shutil.copytree(
                fixture_root, repo_root, ignore=shutil.ignore_patterns(".git")
            )
        else:
            repo_root.mkdir()
        if skill_definitions_source is not None:
            if not skill_definitions_source.is_dir():
                raise WorkflowTaskScenarioError(
                    "Skill definitions directory does not exist: "
                    f"{skill_definitions_source}"
                )
            shutil.copytree(
                skill_definitions_source,
                repo_root / "skill-definitions",
                ignore=shutil.ignore_patterns(".git"),
            )
        shutil.copytree(workflow_source, workflow_dir)
        _ensure_fixture_source_package(repo_root)
        _initialize_git_repository(repo_root)
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
        recorder: LiveWorkflowTaskExchangeRecorder | None = None
        if live_provider is None:
            client: Any = _ScriptedWorkflowTaskClient(
                [
                    _replace_pre_step_result(item, deterministic_state)
                    for item in responses
                ]
            )
        else:
            source_task_for_client = next(
                (item for item in source_tasks if item.task_id == target_task_id),
                source_tasks[-1],
            )
            recorder = LiveWorkflowTaskExchangeRecorder(
                _build_workflow_client(
                    WorkflowTaskAgentConfig(
                        workflow_dir=workflow_dir,
                        repo_root=repo_root,
                        provider=live_provider,
                        api_key=api_key,
                        base_url=base_url,
                        max_roundtrips=max_roundtrips,
                        max_stalled_roundtrips=max_stalled_roundtrips,
                        verbose=verbose,
                        allow_unmanaged_git=True,
                        run_deterministic_invoke_tool_pre_steps=live_provider
                        is not None,
                    ),
                    source_task_for_client,
                )
            )
            client = recorder
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdout_stream: Any = _TeeTextIO(stdout, sys.stdout) if stream_live else stdout
        stderr_stream: Any = _TeeTextIO(stderr, sys.stderr) if stream_live else stderr
        try:
            exit_code = run_workflow_task(
                WorkflowTaskAgentConfig(
                    workflow_dir=workflow_dir,
                    repo_root=repo_root,
                    task_id=None if run_all else target_task_id,
                    provider=live_provider or "local",
                    api_key=api_key,
                    base_url=base_url,
                    max_roundtrips=max_roundtrips,
                    max_stalled_roundtrips=max_stalled_roundtrips,
                    verbose=verbose,
                    allow_unmanaged_git=True,
                    run_deterministic_invoke_tool_pre_steps=live_provider is not None,
                ),
                client=client,
                stdout=stdout_stream,
                stderr=stderr_stream,
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
            "exchanges": recorder.exchanges if recorder is not None else [],
            "all_tasks_completed": all(
                item.status.value == "completed" for item in final_tasks
            ),
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "analysis": _analyze_live_run(
                recorder.exchanges if recorder is not None else [],
                stdout.getvalue(),
                stderr.getvalue(),
            ),
        }
    finally:
        if previous_uv_cache_dir is None:
            os.environ.pop("UV_CACHE_DIR", None)
        else:
            os.environ["UV_CACHE_DIR"] = previous_uv_cache_dir
        shutil.rmtree(temporary, ignore_errors=True)


def _initialize_git_repository(repo_root: Path) -> None:
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Workflow Scenario"),
        ("config", "user.email", "workflow-scenario@example.invalid"),
        ("add", "."),
        ("commit", "-m", "Scenario fixture"),
    ):
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise WorkflowTaskScenarioError(
                f"Could not initialize scenario repository: git {' '.join(arguments)}: "
                f"{result.stderr.strip()}"
            )


def _ensure_fixture_source_package(repo_root: Path) -> None:
    source_directory = repo_root / "src"
    if not source_directory.is_dir():
        return
    package_marker = source_directory / "__init__.py"
    if not package_marker.exists():
        package_marker.write_text(
            '"""Isolated workflow scenario source package."""\n',
            encoding="utf-8",
        )


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


def _analyze_live_run(
    exchanges: Sequence[Mapping[str, Any]], stdout: str, stderr: str
) -> dict[str, Any]:
    """Summarize actionable failure patterns without replacing raw exchanges."""
    outputs = [item.get("output") for item in exchanges]
    action_kinds = [
        output.get("action")
        for output in outputs
        if isinstance(output, Mapping) and isinstance(output.get("action"), str)
    ]
    repeated_actions = sum(
        1
        for previous, current in zip(action_kinds, action_kinds[1:], strict=False)
        if previous == current
    )
    return {
        "exchange_count": len(exchanges),
        "transport_error_count": sum("error" in item for item in exchanges),
        "action_correction_count": stderr.count("action needs correction"),
        "no_progress_count": stderr.count("made no progress"),
        "repeated_action_count": repeated_actions,
        "action_kinds": action_kinds,
        "roundtrip_limit_reached": "reached the configured roundtrip limit" in stderr,
        "human_handoff": "Workflow blocked on human task" in stdout,
    }
