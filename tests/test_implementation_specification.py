from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import build_implementation_specification_validation_report
from powdrr_lift.cli import main
from powdrr_lift.core import (
    architecture_specification_default_output_path,
    implementation_specification_default_output_path,
)


def _write_architecture_specification(repo_root: Path) -> Path:
    architecture_specification_path = architecture_specification_default_output_path(
        repo_root
    )
    architecture_specification_path.parent.mkdir(parents=True, exist_ok=True)
    architecture_specification_path.write_text(
        """
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
            target: Beta
            relationship: depends_on
            description: Alpha depends on Beta.

        invariants: []
        guidance: []
        """,
        encoding="utf-8",
    )
    return architecture_specification_path


def test_create_implementation_specification_template_writes_default_file(
    tmp_path: Path,
) -> None:
    _write_architecture_specification(tmp_path)
    output_path = implementation_specification_default_output_path(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "implementation-specification",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    template_text = output_path.read_text(encoding="utf-8")
    assert "# Implementation specification template." in template_text
    assert "# Architecture id: 2026-06-19" in template_text
    assert "# - Alpha" in template_text
    assert "# - rel-1" in template_text
    assert 'architecture_id: "2026-06-19"' in template_text
    assert "    action: null" in template_text
    assert "    supercedes: null" in template_text

    rendered_template = yaml.safe_load(template_text)
    assert [section for section in rendered_template] == [
        "title",
        "architecture_id",
        "entities",
        "entity_relationships",
        "features",
        "decisions",
    ]


def test_validate_implementation_specification_reports_errors(
    tmp_path: Path,
) -> None:
    _write_architecture_specification(tmp_path)
    proposed_spec = """
    version: 1
    architecture_id: 2026-06-20

    entities:
      - id: Alpha
        action: added
      - id: Ghost
        action: maybe
        supercedes: []

    entity_relationships:
      - id: rel-1
        action: removed
      - id: rel-missing
        action: added

    features:
      - id: feature-a
        action: added
        description: Implement the first feature.
        supercedes: []
        functional_requirements:
          - Must be implemented.
      - id: feature-a
        action: removed
        description: Duplicate feature id.
        functional_requirements:
          - Must also be implemented.

    decisions:
      - id: decision-a
        action: added
        description: Choose the main approach.
        supercedes: []
      - id: feature-a
        action: removed
        description: Duplicate across sections.
    """

    report = build_implementation_specification_validation_report(
        proposed_spec,
        repo_root=tmp_path,
    )

    assert report.validation_successful is False
    assert report.architecture_id == "2026-06-19"
    assert report.available_entity_ids == ["Alpha", "Beta"]
    assert report.available_relationship_ids == ["rel-1"]
    assert {issue.code for issue in report.issues} == {
        "architecture_id_mismatch",
        "invalid_action",
        "supercedes_empty",
        "unknown_architecture_relationship",
        "duplicate_specification_id",
    }


def test_validate_implementation_specification_reports_success_for_valid_spec(
    tmp_path: Path,
) -> None:
    _write_architecture_specification(tmp_path)
    proposed_spec = """
    version: 1
    architecture_id: 2026-06-19

    entities:
      - id: Alpha
        action: added
      - id: Beta
        action: removed

    entity_relationships:
      - id: rel-1
        action: added

    features:
      - id: feature-a
        action: added
        description: Implement the first feature.
        functional_requirements:
          - Must be implemented.

    decisions:
      - id: decision-a
        action: removed
        description: Choose the main approach.
    """

    report = build_implementation_specification_validation_report(
        proposed_spec,
        repo_root=tmp_path,
    )

    assert report.validation_successful is True
    assert report.issues == []


def test_cli_validate_implementation_specification_reports_yaml(
    tmp_path: Path,
) -> None:
    _write_architecture_specification(tmp_path)
    spec_path = tmp_path / "implementation-specification.yaml"
    spec_path.write_text(
        """
        version: 1
        architecture_id: 2026-06-20

        entities:
          - id: Alpha
            action: added

        entity_relationships:
          - id: rel-1
            action: added

        features:
          - id: feature-a
            action: added
            description: Implement the first feature.
            functional_requirements:
              - Must be implemented.

        decisions:
          - id: decision-a
            action: removed
            description: Choose the main approach.
        """,
        encoding="utf-8",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "evaluate-implementation-specification",
                "--repo-root",
                str(tmp_path),
                "--input",
                str(spec_path),
            ]
        )

    assert exit_code == 1
    report = yaml.safe_load(stdout.getvalue())
    assert report["validation_successful"] is False
    assert report["architecture_id"] == "2026-06-19"
    assert {issue["code"] for issue in report["issues"]} == {
        "architecture_id_mismatch",
    }
