from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

from powdrr_lift.workrr import pr_execution_session
from powdrr_lift.workrr.github_issue_queue import WorkerReport
from powdrr_lift.workrr.pr_execution_session import (
    PrExecutionSessionConfig,
    TaskExecutionFailure,
    run_pr_execution_worker,
)


def test_worker_adapter_passes_typed_configuration_to_existing_queue(
    tmp_path: Path, monkeypatch: Any
) -> None:
    resolved_root = tmp_path / "repo"
    item = object()
    plan = object()
    client = object()

    def checkpoint_callback(*_args: object) -> None:
        return None

    observed: dict[str, Any] = {}

    monkeypatch.setattr(
        pr_execution_session, "resolve_repo_root", lambda _path: resolved_root
    )

    def make_client(repository: str) -> object:
        observed["repository"] = repository
        return client

    monkeypatch.setattr(pr_execution_session, "GitHubIssueClient", make_client)

    def execute_task(**kwargs: Any) -> int:
        observed["task"] = kwargs
        return 0

    monkeypatch.setattr(pr_execution_session, "run_planned_workflow_task", execute_task)

    report = WorkerReport("awaiting_review", issue_number=42, item_id="feature")

    def run_queue(**kwargs: Any) -> WorkerReport:
        observed["queue"] = kwargs
        result = kwargs["run_task"](
            tmp_path / "workflow.yaml",
            "workflow-id",
            "claimed-worker",
            cast(Any, item),
            cast(Any, plan),
            ("prior-item",),
            checkpoint_callback,
        )
        assert result == 0
        return report

    monkeypatch.setattr(pr_execution_session, "run_worker_once", run_queue)
    stdout = object()
    stderr = object()
    result = run_pr_execution_worker(
        PrExecutionSessionConfig(
            repo_root=tmp_path,
            repository="owner/repo",
            worker_id="worker-1",
            provider="deepinfra",
            api_key="test-key",
            base_url="https://provider.invalid/v1",
            max_roundtrips=12,
            context_compaction_threshold=9000,
            verbose=True,
            code_agent="minisweagent",
            minisweagent_model="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
            coding_agent_timeout_seconds=120,
            coding_agent_validation_timeout_seconds=240,
        ),
        stdout=cast(Any, stdout),
        stderr=cast(Any, stderr),
    )

    assert result is report
    assert observed["repository"] == "owner/repo"
    queue = observed["queue"]
    assert queue["client"] is client
    assert queue["repo_root"] == resolved_root.resolve()
    assert queue["worker_id"] == "worker-1"
    task = observed["task"]
    assert task["workflow_path"] == tmp_path / "workflow.yaml"
    assert task["workflow_id"] == "workflow-id"
    assert task["worker_id"] == "claimed-worker"
    assert task["item"] is item
    assert task["plan"] is plan
    assert task["fulfilled_without_pr_dependencies"] == ("prior-item",)
    assert task["checkpoint_callback"] is checkpoint_callback
    assert task["repo_root"] == resolved_root.resolve()
    options = task["options"]
    assert options.provider == "deepinfra"
    assert options.repository == "owner/repo"
    assert options.api_key == "test-key"
    assert options.base_url == "https://provider.invalid/v1"
    assert options.max_roundtrips == 12
    assert options.context_compaction_threshold == 9000
    assert options.verbose is True
    assert options.minisweagent_model == "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731"
    assert options.coding_agent_timeout_seconds == 120
    assert options.coding_agent_validation_timeout_seconds == 240
    assert task["stdout"] is stdout
    assert task["stderr"]._stream is stderr


def test_nonzero_task_result_is_raised_with_stderr_diagnostic(
    tmp_path: Path, monkeypatch: Any
) -> None:
    output = StringIO()
    errors = StringIO()

    monkeypatch.setattr(
        pr_execution_session, "resolve_repo_root", lambda _path: tmp_path
    )
    monkeypatch.setattr(
        pr_execution_session, "GitHubIssueClient", lambda _repository: object()
    )

    def execute_task(**kwargs: Any) -> int:
        kwargs["stderr"].write("Procedrr task failed: provider returned HTTP 402\n")
        return 1

    monkeypatch.setattr(pr_execution_session, "run_planned_workflow_task", execute_task)

    def run_queue(**kwargs: Any) -> None:
        kwargs["run_task"](
            tmp_path / "workflow.yaml",
            "workflow-id",
            "worker-id",
            cast(Any, object()),
            cast(Any, object()),
            (),
            cast(Any, lambda *_args: None),
        )

    monkeypatch.setattr(pr_execution_session, "run_worker_once", run_queue)

    with pytest.raises(TaskExecutionFailure, match="HTTP 402") as failure:
        run_pr_execution_worker(
            PrExecutionSessionConfig(
                repo_root=tmp_path,
                repository="owner/repo",
                worker_id="worker-id",
            ),
            stdout=output,
            stderr=errors,
        )

    assert failure.value.return_code == 1
    assert "HTTP 402" in str(failure.value)
    assert "HTTP 402" in errors.getvalue()
