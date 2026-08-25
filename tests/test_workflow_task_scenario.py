from __future__ import annotations

from pathlib import Path

from powdrr_lift.core import (
    AgentRole,
    AssigneeType,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
)
from powdrr_lift.workflow_scenario import load_workflow_scenario, run_workflow_scenario
from powdrr_lift.workflow_task_scenario import run_workflow_task_scenario


def test_task_scenario_runs_the_real_task_agent(tmp_path: Path) -> None:
    workflow = WorkflowInstance.create(
        tmp_path / "source-workflow",
        (
            WorkflowTask(
                task_id="execute-proposed-pr-task-001",
                status=TaskStatus.OPEN,
                complexity=TaskComplexity.LOW,
                input_state={},
                description="Record plan.",
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                output_state_type="plan-state",
            ),
        ),
    )

    result = run_workflow_task_scenario(
        workflow_source=workflow.directory,
        task_id="execute-proposed-pr-task-001",
        responses=[
            {"action": "complete", "output_state": {"plan-state": {"ok": True}}}
        ],
        expected_output_state={"plan-state": {"ok": True}},
    )

    assert result["exit_code"] == 0
    assert result["task_status"] == "completed"
    assert result["output_matches"] is True


def test_workflow_scenario_dispatches_to_task_adapter(tmp_path: Path) -> None:
    workflow = WorkflowInstance.create(
        tmp_path / "source-workflow",
        (
            WorkflowTask(
                task_id="execute-proposed-pr-task-001",
                status=TaskStatus.OPEN,
                complexity=TaskComplexity.LOW,
                input_state={},
                description="Record plan.",
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                output_state_type="plan-state",
            ),
        ),
    )
    scenario = {
        "schema_version": 1,
        "id": "task",
        "execution_mode": "workflow_task",
        "workflow_dir": str(workflow.directory),
        "task_id": "execute-proposed-pr-task-001",
        "provider": {
            "mode": "scripted",
            "responses": [
                {"action": "complete", "output_state": {"plan-state": {"ok": True}}}
            ],
        },
        "expect": {"output_state": {"plan-state": {"ok": True}}},
    }

    result = run_workflow_scenario(
        scenario, scenario_path=tmp_path / "scenario.yaml", repo_root=tmp_path
    )

    assert result.status == "passed"


def test_execute_proposed_pr_task_fixture_completes_deterministic_handoff() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    scenario_path = (
        repo_root
        / "workflow-evals"
        / "scenarios"
        / "execute-proposed-pr"
        / "task-001-context.yaml"
    )

    result = run_workflow_scenario(
        load_workflow_scenario(scenario_path),
        scenario_path=scenario_path,
        repo_root=repo_root,
    )

    assert result.status == "passed"
    assert result.roundtrips == 1


def test_execute_proposed_pr_task_plan_fixture_resolves_upstream_context() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    scenario_path = (
        repo_root
        / "workflow-evals"
        / "scenarios"
        / "execute-proposed-pr"
        / "task-002-plan.yaml"
    )

    result = run_workflow_scenario(
        load_workflow_scenario(scenario_path),
        scenario_path=scenario_path,
        repo_root=repo_root,
    )

    assert result.status == "passed"
    assert result.roundtrips == 1


def test_execute_proposed_pr_full_fixture_runs_every_task() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    scenario_path = (
        repo_root
        / "workflow-evals"
        / "scenarios"
        / "execute-proposed-pr"
        / "task-all.yaml"
    )

    result = run_workflow_scenario(
        load_workflow_scenario(scenario_path),
        scenario_path=scenario_path,
        repo_root=repo_root,
    )

    assert result.status == "passed"
    assert result.roundtrips == 15


def test_execute_proposed_pr_failure_fixtures_recover_or_handoff() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    scenario_root = repo_root / "workflow-evals" / "scenarios" / "execute-proposed-pr"
    scenario_names = (
        "task-001-malformed-action.yaml",
        "task-001-invalid-output.yaml",
        "no-change-recovery.yaml",
        "scope-repair.yaml",
        "human-handoff.yaml",
    )

    results = [
        run_workflow_scenario(
            load_workflow_scenario(scenario_root / name),
            scenario_path=scenario_root / name,
            repo_root=repo_root,
        )
        for name in scenario_names
    ]

    assert all(result.status == "passed" for result in results)
    assert [result.roundtrips for result in results] == [2, 2, 2, 2, 1]
