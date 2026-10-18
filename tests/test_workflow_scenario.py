from __future__ import annotations

import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from powdrr_lift.workrr.scenario import (
    WorkflowScenarioError,
    WorkflowScenarioResult,
    extract_scripted_responses,
    load_workflow_scenario,
    run_workflow_scenario,
)


def test_extract_scripted_responses_supports_live_report_shapes(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "llm_exchanges": [
                    {"output": {"action": "next_step"}},
                    [{"role": "assistant", "content": '{"action":"complete"}'}],
                ]
            }
        ),
        encoding="utf-8",
    )
    assert extract_scripted_responses(report) == [
        {"action": "next_step"},
        {"action": "complete"},
    ]


def test_cli_extract_workflow_responses_writes_fixture(tmp_path: Path) -> None:
    from powdrr_lift.cli import main

    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"llm_exchanges": [{"output": {"action": "next_step"}}]}),
        encoding="utf-8",
    )
    output = tmp_path / "responses.yaml"
    assert (
        main(
            [
                "extract-workflow-responses",
                "--report",
                str(report),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert "action: next_step" in output.read_text(encoding="utf-8")


def test_cli_verify_extracted_replays_the_generated_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from powdrr_lift.cli import main

    scenario = tmp_path / "live.yaml"
    scenario.write_text(
        """\
schema_version: 1
id: live-record
execution_mode: workflow_task
workflow_dir: workflow-evals/workflow-tasks/execute-proposed-pr
task_id: execute-proposed-pr-task-001
provider: {mode: live, provider: openai}
expect: {}
""",
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_run(
        scenario_data: dict[str, object], **_kwargs: object
    ) -> WorkflowScenarioResult:
        calls.append(scenario_data)
        return WorkflowScenarioResult(
            scenario_id="live-record",
            definition="workflow-evals/workflow-tasks/execute-proposed-pr",
            status="passed",
            assertions=(),
            execution_events=(),
            audit_events=(),
            roundtrips=1,
            llm_exchanges=({"output": {"action": "next_step"}},),
        )

    monkeypatch.setattr("powdrr_lift.cli.run_workflow_scenario", fake_run)
    output = tmp_path / "responses.yaml"
    assert (
        main(
            [
                "workflow-scenario",
                "--scenario",
                str(scenario),
                "--repo-root",
                str(tmp_path),
                "--extract-responses",
                str(output),
                "--verify-extracted",
            ]
        )
        == 0
    )
    assert len(calls) == 2
    assert calls[1]["provider"] == {
        "mode": "scripted",
        "responses": [{"action": "next_step"}],
    }


def test_live_execute_workflow_scenario_uses_checked_in_workflow_tasks() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    scenario_path = (
        repository_root
        / "workflow-evals/scenarios/execute-proposed-pr/live-task-all.yaml"
    )
    scenario = load_workflow_scenario(scenario_path)
    scripted_scenario = load_workflow_scenario(
        repository_root / "workflow-evals/scenarios/execute-proposed-pr/task-all.yaml"
    )
    scenario["provider"] = scripted_scenario["provider"]
    scenario["expect"] = scripted_scenario["expect"]

    result = run_workflow_scenario(
        scenario, scenario_path=scenario_path, repo_root=repository_root
    )

    assert result.status == "passed"
    assert result.assertions
    assert all(assertion["passed"] for assertion in result.assertions)
    assert result.roundtrips > 0


def test_checked_in_workflow_scenario_suite_passes() -> None:
    from powdrr_lift.cli import main

    repository_root = Path(__file__).resolve().parents[1]
    assert (
        main(
            [
                "workflow-scenario-suite",
                "--manifest",
                str(repository_root / "workflow-evals/scenarios/manifest.yaml"),
                "--repo-root",
                str(repository_root),
            ]
        )
        == 0
    )


def test_workflow_chain_rejects_duplicate_phase_ids_before_execution(
    tmp_path: Path,
) -> None:
    scenario = {
        "schema_version": 1,
        "id": "duplicate-phases",
        "execution_mode": "workflow_chain",
        "phases": [
            {
                "id": "same",
                "execution_mode": "workflow_task",
                "workflow_dir": "tasks",
                "task_id": "one",
                "provider": {"mode": "scripted", "responses": []},
            },
            {
                "id": "same",
                "execution_mode": "workflow_task",
                "workflow_dir": "tasks",
                "task_id": "two",
                "provider": {"mode": "scripted", "responses": []},
            },
        ],
        "expect": {},
    }
    with pytest.raises(WorkflowScenarioError, match="must be unique"):
        run_workflow_scenario(
            scenario, scenario_path=tmp_path / "scenario.yaml", repo_root=tmp_path
        )


def test_cli_runs_procedrr_task_scenario_and_emits_json_result() -> None:
    from powdrr_lift.cli import main

    repo_root = Path(__file__).resolve().parents[1]
    scenario_path = (
        repo_root / "workflow-evals/scenarios/execute-proposed-pr/task-001-context.yaml"
    )
    stdout = StringIO()

    with redirect_stdout(stdout), redirect_stderr(StringIO()):
        exit_code = main(
            [
                "workflow-scenario",
                "--scenario",
                str(scenario_path),
                "--repo-root",
                str(repo_root),
                "--json",
            ]
        )

    assert exit_code == 0
    assert json.loads(stdout.getvalue())["status"] == "passed"
