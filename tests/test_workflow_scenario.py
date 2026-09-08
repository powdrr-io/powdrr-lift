from __future__ import annotations

import json
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from powdrr_lift.workflow_scenario import (
    WorkflowScenarioError,
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


@pytest.mark.skipif(
    os.environ.get("POWDRR_LIVE_LLM_TESTS") != "1",
    reason="Set POWDRR_LIVE_LLM_TESTS=1 to run the live provider scenario.",
)
def test_live_design_interview_builds_a_valid_specification_v1_document(
    tmp_path: Path,
) -> None:
    """Exercise every design-interview step with actual provider responses."""
    repository_root = Path(__file__).resolve().parents[1]
    scenario_path = tmp_path / "design-interview-live.yaml"
    scenario_path.write_text("# Scenario is assembled in the test.\n", encoding="utf-8")
    work_item_name = "live-design-interview"
    max_roundtrips = int(os.environ.get("POWDRR_LIVE_LLM_MAX_ROUNDTRIPS", "128"))
    scenario = {
        "schema_version": 1,
        "id": "live-design-interview",
        "definition": "skill-definitions/design-interview.yaml",
        "execution_mode": "workflow_chat",
        "request": (
            "Run the design-interview skill for work item live-design-interview. "
            "The feature is a small interaction log that records one human input "
            "and one model response as structured JSONL without storing secrets."
        ),
        "inputs": {
            "work_item_name": work_item_name,
            "feature_description": (
                "Record human inputs and model responses as structured JSONL "
                "without storing secrets."
            ),
        },
        "provider": {
            "mode": "live",
            "provider": os.environ.get("POWDRR_LIVE_LLM_PROVIDER", "auto"),
            "model": os.environ.get("POWDRR_LIVE_LLM_MODEL"),
            "max_roundtrips": max_roundtrips,
        },
        "expect": {
            "outcome": "complete",
            "required_files": [
                f"docs/proposals/{work_item_name}/design-interview-input.json",
                f"docs/proposals/{work_item_name}/feature-pr-specification.yaml",
            ],
            "max_roundtrips": max_roundtrips,
        },
    }

    result = run_workflow_scenario(
        scenario,
        scenario_path=scenario_path,
        repo_root=repository_root,
    )

    assert result.status == "passed", result.stderr
    assert any(
        event.get("kind") == "deterministic_pre_step"
        and event.get("template", {}).get("command", [None])[-2:]
        == [
            "evaluate",
            f"docs/proposals/{work_item_name}/feature-pr-specification.yaml",
        ]
        and event.get("result", {}).get("returncode") == 0
        for event in result.execution_events
    )


def test_scripted_scenario_runs_real_skill_actions_in_isolated_fixture(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "candidate"
    skill_path = repo_root / "skill-definitions" / "inspect.yaml"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """\
name: inspect
when_to_use:
  - Inspect a repository fixture.
steps:
  - id: inspect-status
    description: Inspect repository status.
    tool_invocations:
      - tool: shell
        command: [git, status, --short]
""",
        encoding="utf-8",
    )
    fixture = repo_root / "workflow-evals" / "scenarios" / "fixtures" / "base"
    fixture.mkdir(parents=True)
    (fixture / "example.txt").write_text("fixture\n", encoding="utf-8")
    scenario_path = repo_root / "workflow-evals" / "scenarios" / "inspect.yaml"
    scenario_path.write_text(
        """\
schema_version: 1
id: inspect-status
definition: skill-definitions/inspect.yaml
execution_mode: workflow_chat
fixture: fixtures/base
request: Inspect the fixture status.
provider:
  mode: scripted
  responses:
    - action: invoke_tool
      tool: shell
      parameters:
        command: [git, status, --short]
      text: Status inspected.
    - action: complete
expect:
  outcome: complete
  visited_steps:
    ordered: [inspect-status]
  required_actions:
    - kind: invoke_tool
      tool: shell
    - kind: complete
  forbidden_actions:
    - kind: prompt_user
  required_files: [example.txt]
  forbidden_files: [agent_error.txt]
  max_roundtrips: 2
  max_repeated_action_count: 0
""",
        encoding="utf-8",
    )

    scenario = load_workflow_scenario(scenario_path)
    result = run_workflow_scenario(
        scenario,
        scenario_path=scenario_path,
        repo_root=repo_root,
    )

    assert result.status == "passed"
    assert result.roundtrips == 2
    assert result.worktree_root is None
    assert all(assertion["passed"] for assertion in result.assertions)


def test_workflow_chain_preserves_one_fixture_across_skill_phases(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "candidate"
    skills_dir = repo_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    (skills_dir / "produce.yaml").write_text(
        """\
name: produce
when_to_use: [Produce an artifact.]
steps:
  - id: produce
    description: Produce the artifact.
    actions: [edit]
    outputs:
      - name: artifact
        type: object
        required_for_next_step: true
""",
        encoding="utf-8",
    )
    (skills_dir / "verify.yaml").write_text(
        """\
name: verify
when_to_use: [Verify an artifact.]
steps:
  - id: verify
    description: Verify the artifact.
    actions: [read_document]
""",
        encoding="utf-8",
    )
    scenario_path = repo_root / "chain.yaml"
    (repo_root / "produce-responses.yaml").write_text(
        """\
- action: edit
  file_path: artifact.json
  edits:
    - kind: add
      start_line: 1
      end_line: 1
      text: '{\"ok\": true}'
  outputs: {artifact: {ok: true}}
- action: next_step
""",
        encoding="utf-8",
    )
    scenario_path.write_text(
        """\
schema_version: 1
id: shared-fixture-chain
execution_mode: workflow_chain
phases:
  - id: produce
    definition: skill-definitions/produce.yaml
    request: Produce the artifact.
    provider:
      mode: scripted
      responses_file: produce-responses.yaml
    expect:
      outcome: complete
      required_files: [artifact.json]
  - id: verify
    definition: skill-definitions/verify.yaml
    request: Verify the artifact.
    provider:
      mode: scripted
      responses:
        - action: read_document
          file_path: artifact.json
          start_line: 1
          end_line: 1
        - action: next_step
    expect:
      outcome: complete
      required_actions: [{kind: read_document, file_path: artifact.json}]
  - id: verify-command
    execution_mode: command
    command:
      - python
      - -c
      - "import json; assert json.load(open('artifact.json'))['ok']"
    expect: {exit_code: 0}
expect:
  required_files: [artifact.json]
""",
        encoding="utf-8",
    )

    result = run_workflow_scenario(
        load_workflow_scenario(scenario_path),
        scenario_path=scenario_path,
        repo_root=repo_root,
    )

    assert result.status == "passed", result.stderr
    assert result.roundtrips == 4
    assert all(assertion["passed"] for assertion in result.assertions)


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
                "definition": "one.yaml",
                "request": "one",
                "provider": {"mode": "scripted", "responses": []},
            },
            {
                "id": "same",
                "definition": "two.yaml",
                "request": "two",
                "provider": {"mode": "scripted", "responses": []},
            },
        ],
        "expect": {},
    }
    with pytest.raises(WorkflowScenarioError, match="must be unique"):
        run_workflow_scenario(
            scenario, scenario_path=tmp_path / "scenario.yaml", repo_root=tmp_path
        )


def test_failed_scenario_reports_trajectory_assertion_and_retains_fixture(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "candidate"
    definition_path = repo_root / "skill.yaml"
    definition_path.parent.mkdir()
    definition_path.write_text(
        """\
name: finish
when_to_use:
  - Finish work.
steps:
  - id: finish
    description: Finish work.
""",
        encoding="utf-8",
    )
    scenario_path = repo_root / "failed.yaml"
    scenario_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "must-not-prompt",
                "definition": "skill.yaml",
                "execution_mode": "workflow_chat",
                "request": "Finish.",
                "provider": {
                    "mode": "scripted",
                    "responses": [{"action": "complete", "text": "Done."}],
                },
                "expect": {
                    "outcome": "complete",
                    "forbidden_actions": [{"kind": "complete"}],
                },
            }
        ),
        encoding="utf-8",
    )

    result = run_workflow_scenario(
        load_workflow_scenario(scenario_path),
        scenario_path=scenario_path,
        repo_root=repo_root,
        keep_failed=True,
    )

    assert result.status == "failed"
    assert result.worktree_root is not None
    assert result.worktree_root.is_dir()
    assert any(
        assertion["name"] == "forbidden_actions" and not assertion["passed"]
        for assertion in result.assertions
    )


def test_cli_runs_scripted_scenario_and_emits_json_result(tmp_path: Path) -> None:
    from powdrr_lift.cli import main

    repo_root = tmp_path / "candidate"
    repo_root.mkdir()
    (repo_root / "skill.yaml").write_text(
        """\
name: finish
when_to_use:
  - Finish work.
steps:
  - id: finish
    description: Finish work.
""",
        encoding="utf-8",
    )
    (repo_root / "scenario.yaml").write_text(
        """\
schema_version: 1
id: cli-finish
definition: skill.yaml
execution_mode: workflow_chat
request: Finish the work.
provider:
  mode: scripted
  responses:
    - action: complete
      text: Done.
expect:
  outcome: complete
""",
        encoding="utf-8",
    )
    stdout = StringIO()

    with redirect_stdout(stdout), redirect_stderr(StringIO()):
        exit_code = main(
            [
                "workflow-scenario",
                "--scenario",
                "scenario.yaml",
                "--repo-root",
                str(repo_root),
                "--json",
            ]
        )

    assert exit_code == 0
    assert json.loads(stdout.getvalue())["status"] == "passed"
