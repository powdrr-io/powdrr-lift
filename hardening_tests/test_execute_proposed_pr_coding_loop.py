from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

import pytest

from powdrr_lift.core import WorkflowInstance
from powdrr_lift.core.workflow_template_specification import (
    instantiate_workflow_template,
)
from powdrr_lift.workflow_task_agent import (
    WorkflowTaskAgentConfig,
    _build_workflow_client,
    run_workflow_task,
)
from powdrr_lift.workflow_task_scenario import LiveWorkflowTaskExchangeRecorder


@pytest.mark.real_coding_loop
def test_execute_proposed_pr_template_runs_coding_loop_with_handoff_context(
    tmp_path: Path,
) -> None:
    if os.environ.get("POWDRR_LIFT_RUN_LIVE_CODING_LOOP") != "1":
        pytest.skip("set POWDRR_LIFT_RUN_LIVE_CODING_LOOP=1 to run the live test")
    api_key = os.environ.get("DEEPINFRA_API_TOKEN") or os.environ.get(
        "DEEPINFRA_API_KEY"
    )
    if not api_key:
        pytest.skip("set DEEPINFRA_API_TOKEN or DEEPINFRA_API_KEY to run the live test")

    repository = tmp_path / "repository"
    fixture = (
        Path(__file__).parents[1]
        / "workflow-evals/scenarios/execute-proposed-pr/fixtures/task-002"
    )
    shutil.copytree(fixture, repository)
    (repository / "tests").mkdir()
    (repository / "skill-definitions").mkdir()
    (repository / "src" / "fixture_feature.py").write_text(
        "def fixture_feature() -> str:\n    raise NotImplementedError\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_fixture_feature.py").write_text(
        "from src.fixture_feature import fixture_feature\n\n\n"
        "def test_fixture_feature() -> None:\n"
        '    assert fixture_feature() == "fixture-handoff"\n',
        encoding="utf-8",
    )

    workflow_dir, _ = instantiate_workflow_template(
        Path(__file__).parents[1] / "templates/execute-proposed-pr.yaml",
        work_item_name="fixture-feature",
        workflow_instance_name="fixture-pr",
        output_root=tmp_path / "workflow",
        template_values={
            "proposed-pr-id": "fixture-pr",
            "feature-id": "fixture-feature",
            "verification-command": "python -m pytest -q",
        },
    )
    workflow = WorkflowInstance.from_directory(workflow_dir)
    workflow.complete_task(
        "fixture-pr-task-001",
        {
            "proposed_pr": "fixture-pr",
            "acceptance_criteria": [
                {
                    "id": "fixture-handoff",
                    "description": "fixture_feature returns fixture-handoff.",
                }
            ],
            "expected_tests": ["tests/test_fixture_feature.py"],
        },
    )
    workflow.complete_task(
        "fixture-pr-task-002",
        {
            "implementation_files": ["src/fixture_feature.py"],
            "test_files": ["tests/test_fixture_feature.py"],
            "verification_commands": [["python", "-m", "pytest", "-q"]],
        },
    )
    # Isolate the consolidated coding task while retaining the real template
    # materialization and its completed upstream handoff states.
    for later_task in workflow.tasks[3:]:
        (workflow_dir / f"{later_task.task_id}.yaml").unlink()
    task = WorkflowInstance.from_directory(workflow_dir).tasks[2]
    config = WorkflowTaskAgentConfig(
        workflow_dir=workflow_dir,
        repo_root=repository,
        provider="deepinfra",
        task_id="fixture-pr-task-003",
        api_key=api_key,
        allow_unmanaged_git=True,
        max_roundtrips=30,
    )
    client = LiveWorkflowTaskExchangeRecorder(_build_workflow_client(config, task))
    stdout = io.StringIO()
    stderr = io.StringIO()

    assert (
        run_workflow_task(config, client=client, stdout=stdout, stderr=stderr) == 0
    ), stderr.getvalue()
    assert "fixture-handoff" in (repository / "src" / "fixture_feature.py").read_text(
        encoding="utf-8"
    )
    assert WorkflowInstance.from_directory(workflow_dir).tasks[2].status.value == (
        "completed"
    )
    events = [
        json.loads(line)
        for line in (workflow_dir / "execution/fixture-pr-task-003/events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        event["event_type"] == "capability_decision"
        and event["payload"].get("arguments", {}).get("_tool_name")
        == "coding_loop_verification"
        for event in events
    )
    assert any(
        event["event_type"] == "evidence_recorded"
        and event["payload"].get("evidence_type") == "capability:process"
        and event["payload"].get("successful") is True
        for event in events
    )
