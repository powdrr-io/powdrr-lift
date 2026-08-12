from __future__ import annotations

from pathlib import Path

from powdrr_lift.core.spec_context import (
    gather_proposal_context,
    gather_specification_context,
)


def test_gather_context_filters_tools_by_entity_type_and_labels(
    tmp_path: Path,
) -> None:
    spec_path = tmp_path / "docs" / "current" / "demo"
    spec_path.mkdir(parents=True)
    (spec_path / "architecture-specification.yaml").write_text(
        """
        schema: https://powdrr.io/schemas/specification-v1
        id: 2026-08-11
        entities: []
        modules: []
        tools:
          - id: python-tests
            labels: [pr-prep, python]
            template: pytest -q
          - id: javascript-tests
            labels: [pr-prep, javascript]
            template: npm test
        entity_relationships: []
        invariants: []
        guidance: []
        """,
        encoding="utf-8",
    )

    report = gather_specification_context(
        tmp_path,
        types=["tools"],
        filters={"entity_type": ["Tool"], "labels": ["pr-prep", "python"]},
    )

    assert report.filters == {
        "entity_type": ["Tool"],
        "labels": ["pr-prep", "python"],
    }
    assert [match.item["id"] for match in report.matches] == ["python-tests"]
    assert report.scope == "current"


def test_gather_context_excludes_proposals_and_gather_proposal_reads_only_proposals(
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "docs" / "current" / "demo"
    proposal_path = tmp_path / "docs" / "proposals" / "demo"
    current_path.mkdir(parents=True)
    proposal_path.mkdir(parents=True)
    current_yaml = """
    schema: https://powdrr.io/schemas/specification-v1
    requirements:
      - id: current-requirement
        description: Current behavior
    proposed_prs:
      - id: current-pr
    """
    proposal_yaml = """
    schema: https://powdrr.io/schemas/specification-v1
    features:
      - id: proposed-feature
        description: Proposed behavior
    proposed_prs:
      - id: proposed-pr
    """
    (current_path / "system-specification.yaml").write_text(
        current_yaml,
        encoding="utf-8",
    )
    (proposal_path / "feature-pr-specification.yaml").write_text(
        proposal_yaml,
        encoding="utf-8",
    )

    current_report = gather_specification_context(
        tmp_path,
        types=["requirements", "features", "proposed_prs"],
    )
    proposal_report = gather_proposal_context(
        tmp_path,
        types=["requirements", "features", "proposed_prs"],
    )

    assert current_report.scope == "current"
    assert {match.item["id"] for match in current_report.matches} == {
        "current-requirement",
        "current-pr",
    }
    assert proposal_report.scope == "proposal"
    assert {match.item["id"] for match in proposal_report.matches} == {
        "proposed-feature",
        "proposed-pr",
    }
