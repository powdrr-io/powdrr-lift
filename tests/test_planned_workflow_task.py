from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

import pytest

from powdrr_lift.workrr.github_issue_queue import PlannedWorkItem, ReviewedExecutionPlan
from powdrr_lift.workrr.planned_workflow_task import (
    PlannedWorkflowTaskOptions,
    run_planned_workflow_task,
)
from powdrr_lift.workrr.task_agent import WorkflowTaskAgentConfig


def _item() -> PlannedWorkItem:
    return PlannedWorkItem(
        item_id="search",
        title="Add search",
        objective="Allow users to search records.",
        scope=("search endpoint",),
        acceptance_criteria=("Returns matching records.",),
        dependencies=(),
        workflow_directory=None,
        workflow_id="search-workflow",
        workflow_file=None,
        implementation_steps=("Add an index.",),
        allowed_paths=("src/search.py",),
        inline_adjustments=({"path": "src/search.py", "note": "Keep it small."},),
        github_issue_number=42,
    )


def _options() -> PlannedWorkflowTaskOptions:
    return PlannedWorkflowTaskOptions(
        provider="deepinfra",
        repository="powdrr-io/example",
        api_key="secret",
        base_url="https://api.example.test",
        max_roundtrips=12,
        context_compaction_threshold=7,
        verbose=True,
        code_agent="minisweagent",
        minisweagent_model="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
    )


def test_existing_workflow_directory_uses_shared_task_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_dir = tmp_path / "workflow"
    workflow_dir.mkdir()
    output = io.StringIO()
    errors = io.StringIO()

    def callback(*_args: Any) -> None:
        return None

    captured: dict[str, Any] = {}

    def run(config: WorkflowTaskAgentConfig, *, stdout: TextIO, stderr: TextIO) -> int:
        captured.update(config=config, stdout=stdout, stderr=stderr)
        return 7

    monkeypatch.setattr(
        "powdrr_lift.workrr.planned_workflow_task.run_workflow_task", run
    )
    result = run_planned_workflow_task(
        workflow_path=workflow_dir,
        workflow_id="search-workflow",
        worker_id="worker-1",
        item=_item(),
        plan=ReviewedExecutionPlan("plan-1", (_item(),)),
        fulfilled_without_pr_dependencies=(),
        checkpoint_callback=callback,
        repo_root=tmp_path,
        options=_options(),
        stdout=output,
        stderr=errors,
    )

    assert result == 7
    config = captured["config"]
    assert config.workflow_dir == workflow_dir
    assert config.repo_root == tmp_path.resolve()
    assert config.provider == "deepinfra"
    assert config.api_key == "secret"
    assert config.max_roundtrips == 12
    assert config.task_checkpoint_callback is callback
    assert config.github_issue_number == 42
    assert config.github_repository == "powdrr-io/example"
    assert captured["stdout"] is output
    assert captured["stderr"] is errors


def test_plan_item_preparation_uses_unique_worktree_and_pinned_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_descriptor = tmp_path / "workflow.yaml"
    workflow_descriptor.write_text("descriptor", encoding="utf-8")
    worktree = tmp_path / "worktree"
    workflow_dir = worktree / "docs" / "workflows" / "runtime" / "instance"
    workflow_dir.mkdir(parents=True)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        "powdrr_lift.workrr.planned_workflow_task.load_plan_workflow_instance",
        lambda _path: SimpleNamespace(
            workflow_id="search-workflow",
            process_id="process-1",
            inputs={"proposed_pr_context": {"original": "context"}},
        ),
    )

    def create_worktree(
        _root: Path, _workflow_id: str, **kwargs: Any
    ) -> tuple[Path, str]:
        captured["worktree_kwargs"] = kwargs
        worktree.mkdir(exist_ok=True)
        return worktree, kwargs["branch_name"]

    monkeypatch.setattr(
        "powdrr_lift.workrr.planned_workflow_task.create_workflow_worktree",
        create_worktree,
    )

    def instantiate(**kwargs: Any) -> tuple[Path, tuple[Any, ...]]:
        captured["workflow_inputs"] = kwargs["input_state"]
        captured["output_root"] = kwargs["output_root"]
        return workflow_dir, ()

    monkeypatch.setattr(
        "powdrr_lift.workrr.planned_workflow_task.instantiate_procedrr_process_workflow",
        instantiate,
    )
    monkeypatch.setattr(
        "powdrr_lift.workrr.planned_workflow_task.save_workflow_git_state",
        lambda path, state: captured.update(state_path=path, git_state=state),
    )
    monkeypatch.setattr(
        "powdrr_lift.workrr.planned_workflow_task.run_workflow_task",
        lambda _config, **_kwargs: 0,
    )

    result = run_planned_workflow_task(
        workflow_path=workflow_descriptor,
        workflow_id="search-workflow",
        worker_id="worker-1",
        item=_item(),
        plan=ReviewedExecutionPlan("plan-1", (_item(),)),
        fulfilled_without_pr_dependencies=("legacy-dependency",),
        checkpoint_callback=lambda *_args: None,
        repo_root=tmp_path,
        options=_options(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert result == 0
    assert captured["worktree_kwargs"]["base_branch"] == "main"
    assert captured["worktree_kwargs"]["branch_name"].startswith(
        "powdrr/search-workflow-attempt-"
    )
    context = captured["workflow_inputs"]["proposed_pr_context"]
    assert context["original"] == "context"
    assert context["title"] == "Add search"
    assert context["scope"] == ["search endpoint"]
    assert context["implementation_steps"] == ["Add an index."]
    assert context["inline_plan_adjustments"] == [
        {"path": "src/search.py", "note": "Keep it small."}
    ]
    assert captured["git_state"].fulfilled_without_pr_dependencies == (
        "legacy-dependency",
    )
    assert captured["git_state"].publication_id
    assert captured["output_root"].is_relative_to(worktree)
