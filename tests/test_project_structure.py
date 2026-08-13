from __future__ import annotations

from pathlib import Path

from powdrr_lift.cli import main
from powdrr_lift.core.project_structure import (
    PROJECT_STRUCTURE_TEMPLATE,
    create_project_structure_template,
    validate_project_structure_yaml,
)


def test_create_project_structure_template_creates_parent_directories(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "docs" / "project_structure" / "project-structure.yaml"

    rendered_path = create_project_structure_template(
        output_path=output_path,
        repo_root=tmp_path,
    )

    assert rendered_path == output_path.resolve()
    assert output_path.read_text(encoding="utf-8") == PROJECT_STRUCTURE_TEMPLATE
    assert (
        "schema: https://powdrr.io/schemas/specification-v1"
        in PROJECT_STRUCTURE_TEMPLATE
    )
    template_lines = PROJECT_STRUCTURE_TEMPLATE.splitlines()
    assert "relationships:" not in template_lines
    assert "evidence:" not in template_lines


def test_project_structure_validator_accepts_completed_specification_v1(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(
        """# This file is read-only and should never be edited by a tool or agent.
schema: https://powdrr.io/schemas/specification-v1
id: project-structure
""",
        encoding="utf-8",
    )

    report = validate_project_structure_yaml(output_path)

    assert report.validation_successful is True
    assert report.issues == []


def test_project_structure_validator_rejects_unremoved_template_instructions(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(PROJECT_STRUCTURE_TEMPLATE, encoding="utf-8")

    report = validate_project_structure_yaml(output_path)

    assert report.validation_successful is False
    assert any(
        issue.code == "template_boilerplate_not_removed" for issue in report.issues
    )


def test_project_structure_validator_rejects_removed_sections_and_module_evidence(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(
        """schema: https://powdrr.io/schemas/specification-v1
id: project-structure
entities: []
modules:
  - id: app
    action: added
    relative_location: src/app.py
    purpose: Application.
    evidence: source evidence
tools: []
entity_relationships: []
invariants: []
guidance: []
features: []
decisions: []
intent: {}
relationships: []
evidence: []
""",
        encoding="utf-8",
    )

    report = validate_project_structure_yaml(output_path)

    assert report.validation_successful is False
    assert {issue.path for issue in report.issues} >= {
        "modules[0].evidence",
        "relationships",
        "evidence",
    }


def test_project_structure_cli_validates_template(tmp_path: Path) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(
        """# This file is read-only and should never be edited by a tool or agent.
schema: https://powdrr.io/schemas/specification-v1
id: project-structure
""",
        encoding="utf-8",
    )

    assert main(["validate-project-structure", "--input", str(output_path)]) == 0


def test_project_structure_validator_rejects_explicit_empty_values(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(
        """schema: https://powdrr.io/schemas/specification-v1
id: project-structure
modules: []
tools:
  - id: tools
    action: added
    when_to_use: null
    parent_module: root
    related_module: root
""",
        encoding="utf-8",
    )

    report = validate_project_structure_yaml(output_path)

    assert report.validation_successful is False
    assert {(issue.code, issue.path) for issue in report.issues} >= {
        ("explicit_empty_value", "modules"),
        ("explicit_empty_value", "tools[0].when_to_use"),
    }
    assert {issue.path for issue in report.issues} >= {
        "tools[0].parent_module",
        "tools[0].related_module",
    }


def test_create_project_structure_template_preserves_existing_file(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "docs" / "project_structure" / "project-structure.yaml"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("schema: customized\n", encoding="utf-8")

    create_project_structure_template(output_path=output_path, repo_root=tmp_path)

    assert output_path.read_text(encoding="utf-8") == "schema: customized\n"


def test_project_structure_cli_creates_default_template(tmp_path: Path) -> None:
    output_path = tmp_path / "docs" / "project_structure" / "project-structure.yaml"

    exit_code = main(["project-structure", "--repo-root", str(tmp_path)])

    assert exit_code == 0
    assert output_path.is_file()
