from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from powdrr_lift.workrr import pr_execution_session
from powdrr_lift.workrr.github_issue_queue import WorkerReport
from powdrr_lift.workrr.pr_execution_session import (
    PrExecutionSessionConfig,
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
    assert task["stderr"] is stderr
