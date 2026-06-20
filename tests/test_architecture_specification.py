from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import build_architecture_specification_validation_report
from powdrr_lift.cli import main
from powdrr_lift.core import architecture_specification_default_output_path


def test_create_architecture_specification_template_writes_default_file(
    tmp_path: Path,
) -> None:
    output_path = architecture_specification_default_output_path(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "architecture-specification",
                "--entity-type",
                "Service",
                "--entity-type",
                "Skill",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    template_text = output_path.read_text(encoding="utf-8")
    assert "# Allowed entity types:" in template_text
    assert "# - Set `id` to a date-based identifier" in template_text
    assert "id: null" in template_text
    assert "# - Service" in template_text
    assert "# - Skill" in template_text

    rendered_template = yaml.safe_load(template_text)
    assert rendered_template["version"] == 1
    assert [section for section in rendered_template] == [
        "version",
        "id",
        "title",
        "entities",
        "entity_relationships",
        "invariants",
        "guidance",
    ]


def test_validate_architecture_specification_reports_invalid_types_and_links() -> None:
    proposed_spec = """
    version: 1
    id: 2026-06-19
    title: Demo architecture

    entities:
      - id: Alpha
        type: Service
      - id: Beta
        type: Widget

    entity_relationships:
      - id: rel-1
        source: Alpha
        target: Gamma
        relationship: depends_on
        description: Alpha depends on Gamma.

    invariants:
      - id: inv-1
        description: Keep Alpha stable.
        related:
          entities:
            - Alpha
            - Delta
          entity_relationships:
            - rel-2

    guidance:
      - id: guide-1
        description: Mention Alpha in release notes.
        related:
          entities:
            - Alpha
          entity_relationships: []
    """

    report = build_architecture_specification_validation_report(
        proposed_spec,
        entity_types=["Service", "Skill"],
    )

    assert report.validation_successful is False
    assert report.allowed_entity_types == ["Service", "Skill"]
    assert [issue.code for issue in report.issues] == [
        "entity_type_not_allowed",
        "unknown_relationship_entity",
        "unknown_related_entity",
        "unknown_related_relationship",
    ]


def test_validate_architecture_specification_reports_success_for_valid_spec() -> None:
    proposed_spec = """
    version: 1
    id: 2026-06-19
    title: Demo architecture

    entities:
      - id: Alpha
        type: Service
      - id: Beta
        type: Skill

    entity_relationships:
      - id: rel-1
        source: Alpha
        target: Alpha
        relationship: depends_on
        description: Alpha depends on itself for the demo.

    invariants:
      - id: inv-1
        description: Keep Alpha stable.
        related:
          entities:
            - Alpha
          entity_relationships:
            - rel-1

    guidance:
      - id: guide-1
        description: Mention Beta in release notes.
        related:
          entities:
            - Beta
          entity_relationships: []
    """

    report = build_architecture_specification_validation_report(
        proposed_spec,
        entity_types=["Service", "Skill"],
    )

    assert report.validation_successful is True
    assert report.issues == []


def test_cli_validate_architecture_specification_reports_yaml(
    tmp_path: Path,
) -> None:
    spec_path = tmp_path / "architecture-specification.yaml"
    spec_path.write_text(
        """
        version: 1
        id: 2026-06-19
        title: Demo architecture

        entities:
          - id: Alpha
            type: Service
          - id: Beta
            type: Widget

        entity_relationships:
          - id: rel-1
            source: Alpha
            target: Alpha
            relationship: depends_on
            description: Alpha depends on itself for the demo.

        invariants:
          - id: inv-1
            description: Keep Alpha stable.
            related:
              entities:
                - Alpha
              entity_relationships:
                - rel-1

        guidance:
          - id: guide-1
            description: Mention Alpha in release notes.
            related:
              entities:
                - Alpha
              entity_relationships: []
        """,
        encoding="utf-8",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "evaluate-architecture-specification",
                "--entity-type",
                "Service",
                "--entity-type",
                "Skill",
                "--repo-root",
                str(tmp_path),
                "--input",
                str(spec_path),
            ]
        )

    assert exit_code == 1
    report = yaml.safe_load(stdout.getvalue())
    assert report["validation_successful"] is False
    assert report["allowed_entity_types"] == ["Service", "Skill"]
    assert {issue["code"] for issue in report["issues"]} == {
        "entity_type_not_allowed",
    }
