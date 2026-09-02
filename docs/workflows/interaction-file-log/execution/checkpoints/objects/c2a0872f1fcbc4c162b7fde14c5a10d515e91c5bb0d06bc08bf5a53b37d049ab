from __future__ import annotations

from pathlib import Path

import yaml

from powdrr_lift.core.workflow_relationships import validate_workflow_relationships


def _write_state(
    tmp_path: Path,
    *,
    relationships: list[dict[str, object]],
    invariants: list[dict[str, object]],
) -> Path:
    state = tmp_path / "example-workflow.yaml"
    state.write_text(
        yaml.safe_dump(
            {
                "invariants": invariants,
                "relationships": relationships,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return state


def test_workflow_relationship_invariant_accepts_one_existing_target(
    tmp_path: Path,
) -> None:
    state = _write_state(
        tmp_path,
        invariants=[
            {
                "id": "workflow-target",
                "relationship": "implements",
                "cardinality": "exactly_one",
            }
        ],
        relationships=[
            {
                "invariant_id": "workflow-target",
                "relationship": "implements",
                "source_type": "workflow",
                "source_id": "workflow-1",
                "target_type": "proposal",
                "target_id": "proposal-1",
            }
        ],
    )

    report = validate_workflow_relationships([state])

    assert report.validation_successful is True
    assert report.relationships_checked == 1
    assert report.issues == []


def test_workflow_relationship_invariant_rejects_missing_relationship(
    tmp_path: Path,
) -> None:
    state = _write_state(
        tmp_path,
        invariants=[
            {
                "id": "workflow-target",
                "relationship": "implements",
                "cardinality": "exactly_one",
            }
        ],
        relationships=[],
    )

    report = validate_workflow_relationships([state])

    assert report.validation_successful is False
    assert any(
        issue.code == "relationship_cardinality_failed" for issue in report.issues
    )


def test_required_relationship_invariant_cannot_be_omitted(tmp_path: Path) -> None:
    state = _write_state(tmp_path, invariants=[], relationships=[])

    report = validate_workflow_relationships(
        [state],
        required_invariant_ids=["workflow-target"],
    )

    assert report.validation_successful is False
    assert any(
        issue.code == "missing_required_relationship_invariant"
        for issue in report.issues
    )


def test_workflow_relationship_invariant_rejects_file_pointer(tmp_path: Path) -> None:
    state = _write_state(
        tmp_path,
        invariants=[
            {
                "id": "workflow-target",
                "relationship": "implements",
                "cardinality": "exactly_one",
            }
        ],
        relationships=[
            {
                "invariant_id": "workflow-target",
                "relationship": "implements",
                "source_type": "workflow",
                "source_id": "workflow-1",
                "target_type": "proposal",
                "target_id": "proposal-1",
                "target_path": "target.yaml",
            }
        ],
    )

    report = validate_workflow_relationships([state])

    assert report.validation_successful is False
    assert any(issue.code == "file_pointer_forbidden" for issue in report.issues)
