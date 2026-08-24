from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from powdrr_lift.workflow_definition_analysis import (
    analyze_workflow_definition,
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
