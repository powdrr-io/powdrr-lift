from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import build_system_specification_validation_report
from powdrr_lift.cli import main
from powdrr_lift.core import system_specification_default_output_path
from powdrr_lift.core.system_specification import create_system_specification_template


def test_create_system_specification_template_writes_default_file(
    tmp_path: Path,
) -> None:
    output_path = system_specification_default_output_path("powdrr-lift", tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "system-specification",
                "--work-item-name",
                "powdrr-lift",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    template_text = output_path.read_text(encoding="utf-8")
    assert "# System specification template." in template_text


def test_create_system_specification_template_restores_instructions_and_content(
    tmp_path: Path,
) -> None:
    output_path = system_specification_default_output_path("powdrr-lift", tmp_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        """# This file is read-only and should never be edited by a tool or agent.
schema: https://powdrr.io/schemas/specification-v1
id: existing-system
title: Existing system
requirements: []
approach: []
""",
        encoding="utf-8",
    )

    create_system_specification_template(
        work_item_name="powdrr-lift",
        repo_root=tmp_path,
    )

    template_text = output_path.read_text(encoding="utf-8")
    assert "# System specification template." in template_text
    assert "id: existing-system" in template_text
    assert (
        template_text.count("schema: https://powdrr.io/schemas/specification-v1") == 1
    )
    assert "# - Set `id` to a unique identifier" in template_text
    assert (
        "# - Delete these instructions and replace them with this comment at the top:"
        in template_text
    )
    assert (
        '#   "# This file is read-only and should never be edited by a tool or agent."'
        in template_text
    )
    assert (
        "# - `supercedes` is optional; omit it unless the item replaces ids."
        in template_text
    )
    assert "schema: https://powdrr.io/schemas/specification-v1" in template_text
    assert "requirements:" in template_text
    assert "approach:" in template_text
    assert "supercedes: []" not in template_text

    rendered_template = yaml.safe_load(template_text)
    assert [section for section in rendered_template] == [
        "schema",
        "id",
        "title",
        "requirements",
        "approach",
    ]


def test_create_system_specification_template_recovers_invalid_yaml_and_defaults(
    tmp_path: Path,
) -> None:
    output_path = system_specification_default_output_path("powdrr-lift", tmp_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "schema: [broken\n",
        encoding="utf-8",
    )

    create_system_specification_template(
        work_item_name="powdrr-lift",
        repo_root=tmp_path,
    )

    recovered = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert recovered["schema"] == "https://powdrr.io/schemas/specification-v1"
    assert recovered["requirements"][0]["state"] is None
    assert "# System specification template." in output_path.read_text(encoding="utf-8")


def test_create_system_specification_template_merges_defaults_into_existing_entries(
    tmp_path: Path,
) -> None:
    output_path = system_specification_default_output_path("powdrr-lift", tmp_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        """schema: https://powdrr.io/schemas/specification-v1
id: existing-system
requirements:
  - id: existing-requirement
    description: Keep this requirement.
custom_metadata: keep-this-too
""",
        encoding="utf-8",
    )

    create_system_specification_template(
        work_item_name="powdrr-lift",
        repo_root=tmp_path,
    )

    recovered = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert recovered["id"] == "existing-system"
    assert recovered["requirements"][0]["description"] == "Keep this requirement."
    assert recovered["requirements"][0]["state"] is None
    assert recovered["custom_metadata"] == "keep-this-too"
    assert recovered["approach"][0]["description"] is None


def test_validate_system_specification_reports_errors() -> None:
    proposed_spec = """
    version: 1
    id: sys-1

    requirements:
      - id: req-a
        state: added
      - id: req-b
        state: supercedes
        supercedes:
          - app-a

    approach:
      - id: app-a
        state: removed
        description: Remove this approach.
      - id: app-b
        state: supercedes
    """

    report = build_system_specification_validation_report(
        proposed_spec,
        work_item_name="powdrr-lift",
    )

    assert report.validation_successful is False
    assert report.system_id == "sys-1"
    assert report.requirement_ids == ["req-a", "req-b"]
    assert report.approach_ids == ["app-a", "app-b"]
    assert {issue.code for issue in report.issues} == {
        "added_description_required",
        "unknown_superceded_id",
        "removed_description_not_allowed",
        "supercedes_required",
    }


def test_validate_system_specification_flags_boilerplate() -> None:
    proposed_spec = """
    version: 1
    id: sys-1

    requirements:
      - id: null
        description: null
        state: null
        supercedes: []

    approach:
      - id: null
        description: null
        state: null
        supercedes: []
    """

    report = build_system_specification_validation_report(
        proposed_spec,
        work_item_name="powdrr-lift",
    )

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "boilerplate_not_removed",
        "section_item_id_missing",
    }


def test_validate_system_specification_rejects_template_instructions() -> None:
    proposed_spec = """
    # System specification template.
    #
    # Instructions:
    # - Delete these instructions and replace them with this comment at the top:
    #   "# This file is read-only and should never be edited by a tool or agent."
    version: 1
    id: sys-1
    """

    report = build_system_specification_validation_report(
        proposed_spec,
        work_item_name="powdrr-lift",
    )

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "template_boilerplate_not_removed",
    }


def test_validate_system_specification_reports_duplicate_keys() -> None:
    proposed_spec = """
    version: 1
    id: sys-1

    requirements:
      - id: req-a
        description: Capture the first requirement.
        description: Capture the first requirement again.
        state: added

    approach:
      - id: app-a
        description: Implement the first approach.
        state: added
    """

    report = build_system_specification_validation_report(
        proposed_spec,
        work_item_name="powdrr-lift",
    )

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "duplicate_key_in_section",
    }


def test_validate_system_specification_flags_empty_optional_values() -> None:
    proposed_spec = """
    version: 1
    id: sys-1
    title: ""

    requirements:
      - id: req-a
        description: Capture the first requirement.
        state: added

    approach:
      - id: app-a
        description: Implement the first approach.
        state: added
        supercedes: []
    """

    report = build_system_specification_validation_report(
        proposed_spec,
        work_item_name="powdrr-lift",
    )

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "optional_value_empty",
    }


def test_validate_system_specification_reports_success_for_valid_spec() -> None:
    proposed_spec = """
    version: 1
    id: sys-1

    requirements:
      - id: req-a
        description: Capture the first requirement.
        state: added
      - id: req-b
        description: Replace the old requirement.
        state: supercedes
        supercedes:
          - req-a

    approach:
      - id: app-a
        description: Implement the first approach.
        state: added
      - id: app-b
        state: removed
    """

    report = build_system_specification_validation_report(
        proposed_spec,
        work_item_name="powdrr-lift",
    )

    assert report.validation_successful is True
    assert report.issues == []


def test_validate_system_specification_allows_sparse_spec() -> None:
    proposed_spec = """
    version: 1
    id: sys-1
    """

    report = build_system_specification_validation_report(
        proposed_spec,
        work_item_name="powdrr-lift",
    )

    assert report.validation_successful is True
    assert report.issues == []


def test_validate_system_specification_reports_duplicate_ids_across_sections() -> None:
    proposed_spec = """
    version: 1
    id: sys-1

    requirements:
      - id: spec-a
        description: Capture the first requirement.
        state: added
        supercedes: []

    approach:
      - id: spec-a
        description: Implement the first approach.
        state: added
        supercedes: []
    """

    report = build_system_specification_validation_report(
        proposed_spec,
        work_item_name="powdrr-lift",
    )

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "duplicate_section_item_id",
    }


def test_cli_validate_system_specification_reports_yaml(
    tmp_path: Path,
) -> None:
    spec_path = (
        tmp_path / "docs" / "specs" / "powdrr-lift" / "system-specification.yaml"
    )
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        """
        version: 1
        id: sys-1

        requirements:
          - id: req-a
            state: added
            description: Capture the first requirement.
            supercedes: []

        approach:
          - id: app-a
            state: added
            description: Implement the first approach.
            supercedes: []
        """,
        encoding="utf-8",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "evaluate",
                str(spec_path),
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    report = yaml.safe_load(stdout.getvalue())
    assert report["validation_successful"] is True
    assert report["system_id"] == "sys-1"


def test_cli_evaluate_includes_yaml_edit_for_top_level_specification_issue(
    tmp_path: Path,
) -> None:
    spec_path = tmp_path / "system-specification.yaml"
    spec_path.write_text(
        "version: 1\nrequirements: []\napproach: []\n",
        encoding="utf-8",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(["evaluate", str(spec_path), "--repo-root", str(tmp_path)])

    assert exit_code == 1
    report = yaml.safe_load(stdout.getvalue())
    issue = next(issue for issue in report["issues"] if issue["path"] == "id")
    assert issue["yaml_edit"] == {
        "kind": "yaml_edit",
        "file_path": str(spec_path),
        "operations": [
            {
                "op": "set_value",
                "path": ["id"],
                "value": "<correct-value>",
            }
        ],
    }


def test_cli_evaluate_returns_multiple_yaml_issues_with_repair_actions(
    tmp_path: Path,
) -> None:
    spec_path = tmp_path / "system-specification.yaml"
    spec_path.write_text(
        "version: 1\nrequirements: invalid\napproach: invalid\n",
        encoding="utf-8",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(["evaluate", str(spec_path), "--repo-root", str(tmp_path)])

    assert exit_code == 1
    report = yaml.safe_load(stdout.getvalue())
    assert len(report["issues"]) == 3
    assert {issue["path"] for issue in report["issues"]} == {
        "id",
        "requirements",
        "approach",
    }
    assert all("yaml_edit" in issue for issue in report["issues"])
