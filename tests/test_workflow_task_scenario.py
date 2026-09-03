from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from powdrr_lift.core import (
    AgentRole,
    AssigneeType,
    CodingLoopSpec,
    CodingLoopVerification,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
)
from powdrr_lift.workflow_scenario import load_workflow_scenario, run_workflow_scenario
from powdrr_lift.workflow_task_agent import (
    WorkflowTaskAgentConfig,
    _build_workflow_client,
    run_workflow_task,
)
from powdrr_lift.workflow_task_scenario import (
    LiveWorkflowTaskExchangeRecorder,
    run_workflow_task_scenario,
)


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


@pytest.mark.real_coding_loop
def test_coding_task_agent_reacts_to_failure_and_verifies_the_fix(
    tmp_path: Path,
) -> None:
    if os.environ.get("POWDRR_LIFT_RUN_LIVE_CODING_LOOP") != "1":
        pytest.skip("set POWDRR_LIFT_RUN_LIVE_CODING_LOOP=1 to run the live test")
    provider = "deepinfra"
    api_key = os.environ.get("DEEPINFRA_API_TOKEN") or os.environ.get(
        "DEEPINFRA_API_KEY"
    )
    if not api_key:
        pytest.skip("set DEEPINFRA_API_TOKEN or DEEPINFRA_API_KEY to run the live test")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "skill-definitions").mkdir()
    product_path = tmp_path / "src" / "greeting.py"
    test_path = tmp_path / "tests" / "test_greeting.py"
    product_path.write_text(
        'def greeting(name: str) -> str:\n    return f"Hello {name}"\n',
        encoding="utf-8",
    )
    test_path.write_text(
        "from src.greeting import greeting\n\n\ndef test_greeting() -> None:\n"
        '    assert greeting("Ada") == "Hello, Ada!"\n',
        encoding="utf-8",
    )
    workflow = WorkflowInstance.create(
        tmp_path / "workflow",
        (
            WorkflowTask(
                task_id="coding-task",
                status=TaskStatus.OPEN,
                complexity=TaskComplexity.MEDIUM,
                input_state={
                    "goal": "Add punctuation to greetings.",
                    "current_product": (
                        'def greeting(name: str) -> str:\n    return f"Hello {name}"\n'
                    ),
                    "current_test": (
                        "from src.greeting import greeting\n\n\n"
                        "def test_greeting() -> None:\n"
                        '    assert greeting("Ada") == "Hello, Ada!"\n'
                    ),
                },
                description=(
                    "Fix the greeting implementation to satisfy the existing "
                    "punctuated assertion, then verify the change."
                ),
                details=(
                    "The input_state contains the exact current product and test "
                    "source. Make the smallest justified edits to "
                    "src/greeting.py. The test is intentionally red before the "
                    "first edit: preserve its expected punctuation. First return "
                    "an invoke_tool action with tool shell and parameters "
                    'command ["python", "-m", "pytest", "-q"] so you '
                    "can observe the current failure. Then return an edit action "
                    "for src/greeting.py and rerun the coding-loop verification "
                    "until it passes before advancing the task. For the edit, "
                    "return exactly one top-level file_edits entry with "
                    'file_path "src/greeting.py"; do not nest it under '
                    "parameters. "
                    "Return edits with top-level file_edits, each containing "
                    "file_path and line-based edits with kind replace, "
                    "start_line, end_line, and text; do not use old/new or "
                    "nest the edit under parameters. Return read_document "
                    "fields at the top level too, exactly as action, "
                    "file_path, start_line, and end_line. After pytest passes, "
                    'return next_step with top-level output_state {"verified": '
                    "true}."
                ),
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.CODER,
                llm_type="standard_reasoning",
                actions=("invoke_tool", "edit"),
                step_type="coding_loop",
                coding_loop=CodingLoopSpec(
                    goal="Make the greeting product code and test agree.",
                    verification=(
                        CodingLoopVerification(
                            id="pytest",
                            command=("python", "-m", "pytest", "-q"),
                        ),
                    ),
                    stopping_conditions=("pytest passes",),
                    max_iterations=12,
                ),
                output_state_type="coding-task-state",
            ),
        ),
    )
    config = WorkflowTaskAgentConfig(
        workflow_dir=workflow.directory,
        repo_root=tmp_path,
        provider=provider,
        api_key=api_key,
        allow_unmanaged_git=True,
        max_roundtrips=30,
    )
    # Build the production client through the same provider/mapping resolver as
    # the task runner, so DeepInfra selects the model for standard_reasoning.
    client = LiveWorkflowTaskExchangeRecorder(
        _build_workflow_client(config, workflow.tasks[0])
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        config,
        client=client,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0, stderr.getvalue()
    product = product_path.read_text(encoding="utf-8")
    test = test_path.read_text(encoding="utf-8")
    assert product != 'def greeting(name: str) -> str:\n    return f"Hello {name}"\n'
    assert test == (
        "from src.greeting import greeting\n\n\ndef test_greeting() -> None:\n"
        '    assert greeting("Ada") == "Hello, Ada!"\n'
    )
    subprocess.run(
        ["python", "-m", "pytest", "-q"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    completed = WorkflowInstance.from_directory(workflow.directory).tasks[0]
    assert completed.status is TaskStatus.COMPLETED
    assert completed.output_state == {"verified": True}
    durable_events = [
        json.loads(line)
        for line in (workflow.directory / "execution" / "coding-task" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    event_kinds = [event["payload"].get("kind") for event in durable_events]
    assert event_kinds.count("edit") >= 1
    assert event_kinds.count("run_process") >= 2
    assert event_kinds.count("next_step") >= 1
    failed_process_evidence = [
        event
        for event in durable_events
        if event["event_type"] == "evidence_recorded"
        and event["payload"].get("evidence_type") == "capability:process"
        and event["payload"].get("successful") is False
    ]
    assert failed_process_evidence, (
        "the live agent must observe the initial pytest failure"
    )
    assert any(
        event["payload"].get("evidence_type") == "capability:process"
        and event["payload"].get("successful") is True
        for event in durable_events
        if event["event_type"] == "evidence_recorded"
    )
    verification_decision = next(
        event
        for event in durable_events
        if event["event_type"] == "capability_decision"
        and event["payload"].get("tool_name") == "process"
    )
    assert verification_decision["payload"]["arguments"]["command"] == [
        "python",
        "-m",
        "pytest",
        "-q",
    ]
    assert len(client.exchanges) >= 2
    first_prompt = json.loads(client.exchanges[0]["input"][1]["content"])
    assert first_prompt["task"]["step_type"] == "coding_loop"
    assert first_prompt["task"]["coding_loop"]["verification"][0]["id"] == ("pytest")
    assert "invoke_tool" in client.exchanges[0]["input"][0]["content"]
    assert "edit" in client.exchanges[0]["input"][0]["content"]
    final_prompt = json.loads(client.exchanges[-1]["input"][1]["content"])
    assert any(
        event["kind"] == "coding_loop_verification"
        for event in final_prompt["events"]["recent"]
    )


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


def test_live_workflow_scenario_records_real_client_exchanges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    class FakeLiveClient:
        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            assert messages
            return {"action": "complete", "output_state": {"plan-state": {"ok": True}}}

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_scenario._build_workflow_client",
        lambda *_args, **_kwargs: FakeLiveClient(),
    )
    scenario = {
        "schema_version": 1,
        "id": "live-task",
        "execution_mode": "workflow_task",
        "workflow_dir": str(workflow.directory),
        "task_id": "execute-proposed-pr-task-001",
        "provider": {"mode": "live", "provider": "openai"},
        "expect": {"output_state": {"plan-state": {"ok": True}}},
    }

    result = run_workflow_scenario(
        scenario, scenario_path=tmp_path / "live.yaml", repo_root=tmp_path
    )

    assert result.status == "passed"
    assert len(result.llm_exchanges) == 1
    assert result.llm_exchanges[0]["output"]["action"] == "complete"
    assert result.analysis == {
        "exchange_count": 1,
        "transport_error_count": 0,
        "action_correction_count": 0,
        "no_progress_count": 0,
        "repeated_action_count": 0,
        "action_kinds": ["complete"],
        "roundtrip_limit_reached": False,
        "human_handoff": False,
    }
    assert result.stdout


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
    assert result.roundtrips == 16


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
