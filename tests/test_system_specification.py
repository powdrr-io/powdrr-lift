from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import build_system_specification_validation_report
from powdrr_lift.cli import main
from powdrr_lift.core import system_specification_default_output_path


def test_create_system_specification_template_writes_default_file(
    tmp_path: Path,
) -> None:
    output_path = system_specification_default_output_path(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "system-specification",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    template_text = output_path.read_text(encoding="utf-8")
    assert "# System specification template." in template_text
    assert "# - Set `id` to a unique identifier" in template_text
    assert (
        "# - Remove the boilerplate placeholder entries once the document is"
        in template_text
    )
    assert "requirements:" in template_text
    assert "approach:" in template_text

    rendered_template = yaml.safe_load(template_text)
    assert rendered_template["version"] == 1
    assert [section for section in rendered_template] == [
        "version",
        "id",
        "title",
        "requirements",
        "approach",
    ]


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

    report = build_system_specification_validation_report(proposed_spec)

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

    report = build_system_specification_validation_report(proposed_spec)

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "boilerplate_not_removed",
        "section_item_id_missing",
    }


def test_validate_system_specification_reports_success_for_valid_spec() -> None:
    proposed_spec = """
    version: 1
    id: sys-1

    requirements:
      - id: req-a
        description: Capture the first requirement.
        state: added
        supercedes: []
      - id: req-b
        description: Replace the old requirement.
        state: supercedes
        supercedes:
          - req-a

    approach:
      - id: app-a
        description: Implement the first approach.
        state: added
        supercedes: []
      - id: app-b
        state: removed
        supercedes: []
    """

    report = build_system_specification_validation_report(proposed_spec)

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

    report = build_system_specification_validation_report(proposed_spec)

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "duplicate_section_item_id",
    }


def test_cli_validate_system_specification_reports_yaml(
    tmp_path: Path,
) -> None:
    spec_path = tmp_path / "system-specification.yaml"
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
                "evaluate-system-specification",
                "--repo-root",
                str(tmp_path),
                "--input",
                str(spec_path),
            ]
        )

    assert exit_code == 0
    report = yaml.safe_load(stdout.getvalue())
    assert report["validation_successful"] is True
    assert report["system_id"] == "sys-1"
