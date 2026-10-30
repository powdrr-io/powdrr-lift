"""Isolated scripted scenarios that exercise the production workflow-task agent."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.process.tasks import WorkflowInstance
from powdrr_lift.workrr.task_agent import (
    WorkflowTaskAgentConfig,
    _build_workflow_client,
    _run_task_deterministic_pre_step,
    run_workflow_task,
)


class WorkflowTaskScenarioError(ValueError):
    """Raised for malformed isolated workflow-task scenario inputs."""


def _adapt_legacy_task_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Translate old scripted task fixtures to one Procedrr fragment step."""
    action = response.get("action")
    if (
        action in {"complete", "next_step", "task_complete"}
        and "output_state" in response
    ):
        return {
            "step_json": json.dumps(
                {
                    "operation": {
                        "tool": "task_complete",
                        "parameters": {
                            "output_state": response["output_state"],
                            "terminate_workflow": response.get(
                                "terminate_workflow", False
                            ),
                        },
                    }
                }
            )
        }
    if action in {"complete", "next_step"}:
        return {
            "step_json": json.dumps(
                {
                    "operation": {
                        "tool": "respond",
                        "parameters": {"text": response.get("text", "")},
                    }
                }
            )
        }
    tool = {
        "edit": "edit",
        "yaml_edit": "yaml_edit",
        "file_management": "file_management",
        "read_document": "read_document",
        "list_files": "list_files",
        "gather_context": "gather_context",
        "invoke_tool": "invoke_tool",
        "task_run_process": "task_run_process",
        "get-human-input": "task_handoff",
        "task_handoff": "task_handoff",
        "prompt_user": "task_handoff",
    }.get(str(action))
    if tool is None:
        return dict(response)
    if tool == "invoke_tool":
        parameters = {
            "tool": response.get("tool"),
            "parameters": response.get("parameters", {}),
        }
    elif tool == "task_run_process":
        parameters = {
            "process_id": response.get("process_id"),
            "bindings": response.get("bindings", {}),
        }
    elif tool == "task_handoff":
        parameters = {
            "human_input": response.get("human_input"),
            "question": response.get("text", "Please provide the required input."),
            "continuation_instructions": "Use the human response to continue.",
        }
    elif tool == "edit":
        parameters = {
            "file_path": response.get("file_path"),
            "edits": response.get("edits", []),
        }
    else:
        parameters = {key: value for key, value in response.items() if key != "action"}
    return {
        "step_json": json.dumps({"operation": {"tool": tool, "parameters": parameters}})
    }


class LiveWorkflowTaskExchangeRecorder:
    """Capture complete exchanges while delegating to a real LLM client."""

    def __init__(
        self, client: Any, *, provider: str | None = None, model: str | None = None
    ) -> None:
        self._client = client
        self.provider = provider or getattr(client, "_provider", None)
        self.model = model or getattr(client, "_model", None)
        self.exchanges: list[dict[str, Any]] = []

    @property
    def messages(self) -> list[list[dict[str, str]]]:
        return [exchange["input"] for exchange in self.exchanges]

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "input": [dict(message) for message in messages],
            "provider": self.provider,
            "model": self.model,
        }
        try:
            response = self._client.complete_json(messages)
        except Exception as exc:
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            self.exchanges.append(record)
            raise
        adapted_response = _adapt_legacy_task_response(response)
        record["output"] = adapted_response
        usage = getattr(self._client, "last_usage", None)
        if isinstance(usage, Mapping):
            record["usage"] = dict(usage)
        self.exchanges.append(record)
        return adapted_response


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
        self._terminal_pending = False
        self.messages: list[list[dict[str, str]]] = []
        self.responses_served: list[dict[str, Any]] = []

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.messages.append(messages)
        if self._terminal_pending:
            self._terminal_pending = False
            return {"step_json": json.dumps({"terminal": "succeeded"})}
        try:
            response = next(self._responses)
            self.responses_served.append(response)
            adapted = _adapt_legacy_task_response(response)
            if response.get("action") in {"complete", "next_step"} and (
                "output_state" in response
            ):
                self._terminal_pending = True
            return adapted
        except StopIteration:
            return {"step_json": json.dumps({"terminal": "succeeded"})}


def run_workflow_task_scenario(
    *,
    workflow_source: Path,
    process_catalog_source: Path | None = None,
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
    guidance: Sequence[str] = (),
    shared_repo_root: Path | None = None,
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
    temporary = (
        Path(tempfile.mkdtemp(prefix="powdrr-lift-task-scenario-"))
        if shared_repo_root is None
        else None
    )
    repo_root = (
        (temporary / "repository")
        if temporary is not None
        else (shared_repo_root.resolve() if shared_repo_root is not None else None)
    )
    assert repo_root is not None
    workflow_dir = repo_root / "workflow"
    try:
        if fixture_root is not None and not (repo_root / ".git").exists():
            shutil.copytree(
                fixture_root, repo_root, ignore=shutil.ignore_patterns(".git")
            )
        else:
            repo_root.mkdir(parents=True, exist_ok=True)
        if process_catalog_source is not None:
            if not process_catalog_source.is_dir():
                raise WorkflowTaskScenarioError(
                    "Procedrr process catalog directory does not exist: "
                    f"{process_catalog_source}"
                )
            shutil.copytree(
                process_catalog_source,
                repo_root / "docs" / "procedrr" / "processes",
                ignore=shutil.ignore_patterns(".git"),
            )
        if workflow_dir.exists():
            shutil.rmtree(workflow_dir)
        shutil.copytree(workflow_source, workflow_dir)
        _ensure_fixture_source_package(repo_root)
        if not (repo_root / ".git").exists():
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
        pre_step_runtime = ExecutionRuntime(
            "scenario-pre-step",
            profile_id="default",
            workflow_directory=repo_root.parent / "scenario-execution",
            repo_root=repo_root,
        )
        deterministic_state, _ = (
            _run_task_deterministic_pre_step(
                source_task,
                repo_root=repo_root,
                events=[],
                runtime=pre_step_runtime,
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
            live_client = _build_workflow_client(
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
                    run_deterministic_invoke_tool_pre_steps=live_provider is not None,
                    guidance=tuple(guidance),
                ),
                source_task_for_client,
            )
            recorder = LiveWorkflowTaskExchangeRecorder(
                live_client,
                provider=live_provider,
                model=getattr(live_client, "_model", None),
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
                    guidance=tuple(guidance),
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
            "exchanges": (
                recorder.exchanges if recorder is not None else client.messages
            ),
            "usage_summary": summarize_run_usage(
                recorder.exchanges if recorder is not None else [],
                provider=recorder.provider if recorder is not None else live_provider,
                model=recorder.model if recorder is not None else None,
            ),
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
        if temporary is not None:
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


def _analyze_live_run(
    exchanges: Sequence[Mapping[str, Any]], stdout: str, stderr: str
) -> dict[str, Any]:
    """Summarize Procedrr step proposals without replacing raw exchanges."""
    steps: list[Mapping[str, Any]] = []
    for item in exchanges:
        output = item.get("output")
        encoded = output.get("step_json") if isinstance(output, Mapping) else None
        if not isinstance(encoded, str):
            continue
        try:
            step = json.loads(encoded)
        except json.JSONDecodeError:
            continue
        if isinstance(step, Mapping):
            steps.append(step)
    operation_tools = [
        operation.get("tool")
        for step in steps
        if isinstance((operation := step.get("operation")), Mapping)
        and isinstance(operation.get("tool"), str)
    ]
    repeated_operations = sum(
        1
        for previous, current in zip(operation_tools, operation_tools[1:], strict=False)
        if previous == current
    )
    return {
        "exchange_count": len(exchanges),
        "transport_error_count": sum("error" in item for item in exchanges),
        "step_repair_count": sum(
            "previous response was invalid" in str(message.get("content", "")).lower()
            for item in exchanges
            for message in item.get("input", [])
            if isinstance(message, Mapping)
        ),
        "operation_error_count": stdout.count("operation_error")
        + stderr.count("operation_error"),
        "repeated_operation_count": repeated_operations,
        "operation_tools": operation_tools,
        "process_limit_reached": "budget exceeded" in stderr,
        "human_handoff": "waiting for human task" in stdout.lower(),
    }


def summarize_run_usage(
    exchanges: Sequence[Mapping[str, Any]],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Summarize provider usage, retaining unknown cost as null."""
    totals: dict[str, dict[str, Any]] = {}
    for exchange in exchanges:
        exchange_model = str(exchange.get("model") or model or "unknown")
        exchange_provider = str(exchange.get("provider") or provider or "unknown")
        key = f"{exchange_provider}/{exchange_model}"
        entry = totals.setdefault(
            key,
            {
                "provider": exchange_provider,
                "model": exchange_model,
                "calls": 0,
                "tokens_sent": 0,
                "tokens_received": 0,
                "cost_usd": 0.0,
                "cost_known": True,
            },
        )
        entry["calls"] += 1
        usage = exchange.get("usage")
        if not isinstance(usage, Mapping):
            entry["cost_known"] = False
            continue
        sent = usage.get("prompt_tokens", usage.get("input_tokens"))
        received = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(sent, int):
            entry["tokens_sent"] += sent
        if isinstance(received, int):
            entry["tokens_received"] += received
        cost = next(
            (
                usage[name]
                for name in ("cost", "total_cost", "cost_usd")
                if isinstance(usage.get(name), (int, float))
            ),
            None,
        )
        if isinstance(cost, (int, float)):
            entry["cost_usd"] += float(cost)
        else:
            entry["cost_known"] = False
    models = []
    for entry in totals.values():
        entry["cost_usd"] = (
            round(entry["cost_usd"], 10) if entry.pop("cost_known") else None
        )
        models.append(entry)
    known = bool(models) and all(item["cost_usd"] is not None for item in models)
    return {
        "calls": sum(item["calls"] for item in models),
        "tokens_sent": sum(item["tokens_sent"] for item in models),
        "tokens_received": sum(item["tokens_received"] for item in models),
        "cost_usd": (
            round(sum(item["cost_usd"] for item in models), 10) if known else None
        ),
        "models": models,
    }
