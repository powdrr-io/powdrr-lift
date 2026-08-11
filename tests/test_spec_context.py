from __future__ import annotations

from pathlib import Path

from powdrr_lift.core.spec_context import gather_specification_context


def test_gather_context_filters_tools_by_entity_type_and_labels(
    tmp_path: Path,
) -> None:
    spec_path = tmp_path / "docs" / "specs" / "demo"
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
