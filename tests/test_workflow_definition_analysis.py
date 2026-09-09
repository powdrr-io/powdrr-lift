from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from powdrr_lift.cli import main
from powdrr_lift.workflow_definition_analysis import (
    analyze_workflow_definition,
    analyze_workflow_definitions,
    discover_workflow_definitions,
    render_skill_prompt_snapshots,
)


def test_definition_analysis_uses_runtime_action_parser_and_input_contract(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect the feature.
    inputs:
      - name: feature_id
    details: >-
      Use <feature-id>, then return
      {"action":"complete","text":"Inspected the feature."}.
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert report.validation_successful


def test_definition_analysis_reports_invalid_examples_and_unbound_placeholders(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect the feature.
    inputs:
      - name: feature_id
    details: >-
      Use <missing-id>, then return {"action":"not-a-real-action"}.
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert not report.validation_successful
    assert {issue.code for issue in report.issues} == {
        "invalid_action_example",
        "unbound_placeholder",
    }


def test_prompt_snapshots_use_production_builder_and_normalize_repository_root(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect files.
""",
        encoding="utf-8",
    )
    output_dir = tmp_path / "snapshots"

    paths = render_skill_prompt_snapshots(
        definition,
        output_dir=output_dir,
        repo_root=tmp_path,
    )

    assert [path.name for path in paths] == ["001-inspect.json"]
    snapshot = json.loads(paths[0].read_text(encoding="utf-8"))
    assert snapshot["messages"][0]["role"] == "system"
    assert "<root-intent>" in snapshot["messages"][1]["content"]
    assert str(tmp_path) not in paths[0].read_text(encoding="utf-8")


def test_definition_validation_cli_emits_machine_readable_report(
    tmp_path: Path,
) -> None:
    from powdrr_lift.cli import main

    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect files.
""",
        encoding="utf-8",
    )
    stdout = StringIO()

    with redirect_stdout(stdout):
        exit_code = main(["validate-workflow-definition", str(definition), "--json"])

    assert exit_code == 0
    assert json.loads(stdout.getvalue())["validation_successful"] is True


def test_definition_discovery_recurses_and_ignores_unsupported_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "skill.yaml").write_text("name: x\nsteps: []\n", encoding="utf-8")
    (tmp_path / "nested" / "template.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a definition", encoding="utf-8")

    assert discover_workflow_definitions([tmp_path]) == (
        tmp_path / "nested" / "template.json",
        tmp_path / "skill.yaml",
    )


def test_batch_definition_analysis_reports_all_definitions(tmp_path: Path) -> None:
    valid = tmp_path / "valid.yaml"
    valid.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect files.
""",
        encoding="utf-8",
    )
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("steps: not-a-list\n", encoding="utf-8")

    report = analyze_workflow_definitions([tmp_path])

    assert len(report.reports) == 2
    assert not report.validation_successful
    assert report.reports[0].definition == invalid
    assert report.reports[1].definition == valid


def test_batch_definition_validation_cli_fails_for_any_invalid_definition(
    tmp_path: Path,
) -> None:
    (tmp_path / "valid.yaml").write_text(
        "name: inspect\nwhen_to_use: [Inspect.]\nsteps:\n  - description: Inspect.\n",
        encoding="utf-8",
    )
    (tmp_path / "invalid.yaml").write_text("steps: invalid\n", encoding="utf-8")
    stdout = StringIO()

    with redirect_stdout(stdout):
        exit_code = main(["validate-workflow-definitions", str(tmp_path), "--json"])

    assert exit_code == 1
    report = json.loads(stdout.getvalue())
    assert report["definition_count"] == 2
    assert report["validation_successful"] is False


def test_prompt_snapshots_support_workflow_templates(tmp_path: Path) -> None:
    definition = tmp_path / "template.yaml"
    definition.write_text(
        """\
id: execute
task_templates:
  - description: Gather context.
    step_type: invoke_tool
    input_state: {proposal: <proposal-id>}
    details: Assign the result.
    output_state_type: context-state
""",
        encoding="utf-8",
    )

    paths = render_skill_prompt_snapshots(
        definition, output_dir=tmp_path / "snapshots", repo_root=tmp_path
    )

    assert [path.name for path in paths] == ["001-gather-context.json"]
    snapshot = json.loads(paths[0].read_text(encoding="utf-8"))
    assert snapshot["workflow_template"] == "execute"
    assert snapshot["step_type"] == "invoke_tool"


def test_definition_analysis_reports_missing_handoff_and_schema(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: handoff
when_to_use: [Test handoffs.]
steps:
  - id: produce
    description: Produce a result.
    outputs:
      - name: result
        required_for_next_step: true
  - id: consume
    description: Consume the result.
    inputs:
      - name: missing
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert {issue.code for issue in report.issues} >= {
        "missing_output_schema",
        "missing_handoff_input",
    }


def test_definition_analysis_reports_unreachable_steps(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: control-flow
when_to_use: [Test control flow.]
steps:
  - id: first
    description: Jump over the second step.
    next_step_override: last
  - id: skipped
    description: This step is unreachable.
  - id: last
    description: Finish.
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "unreachable_step" in {issue.code for issue in report.issues}


def test_definition_analysis_validates_examples_against_step_contract(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: predicated
when_to_use: [Test predicated actions.]
steps:
  - id: inspect
    description: Inspect.
    step_type: predicated
    completion:
      required_outputs: [answer]
    outputs:
      - name: answer
        required_for_next_step: true
        schema: {type: string}
    details: 'Return {"action":"next_step"}.'
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "invalid_action_example" in {issue.code for issue in report.issues}


def test_definition_analysis_warns_on_model_owned_idempotent_git_add(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: stage
when_to_use: [Stage files.]
steps:
  - id: stage
    description: Stage the files.
    tool_invocations:
      - tool: git
        command: [add, docs/proposal.yaml]
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert report.validation_successful
    issues = {issue.code: issue for issue in report.issues}
    assert issues["model_owned_deterministic_action"].severity == "warning"
    assert issues["idempotent_action_without_auto_advance"].severity == "warning"


def test_definition_analysis_warns_on_non_progress_cycle(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: loop
when_to_use: [Loop.]
steps:
  - id: loop
    description: Repeat the same observation.
    next_step_override: loop
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "non_progress_cycle" in {issue.code for issue in report.issues}


def test_definition_validation_cli_prints_liveness_warnings(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: stage
when_to_use: [Stage files.]
steps:
  - id: stage
    description: Stage the files.
    tool_invocations:
      - tool: git
        command: [add, docs/proposal.yaml]
""",
        encoding="utf-8",
    )
    stdout = StringIO()

    with redirect_stdout(stdout):
        exit_code = main(["validate-workflow-definition", str(definition)])

    assert exit_code == 0
    output = stdout.getvalue()
    assert "Workflow definition valid" in output
    assert "idempotent_action_without_auto_advance" in output
