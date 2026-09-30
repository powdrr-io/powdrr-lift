from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from powdrr_lift.structrr.candidate_comparison import compare_candidate_snapshot
from powdrr_lift.structrr.proposal import ProposalRevision, compile_proposal_revision


def _proposal(plan: Mapping[str, Any], baseline: Mapping[str, Any]) -> ProposalRevision:
    return compile_proposal_revision(
        "change-1",
        baseline,
        plan,
        acceptance_criteria=("requested change is present",),
        must_preserve=(),
        non_goals=(),
        allowed_paths=("src", "tests"),
        source_refs=("instructions:1",),
    )


def test_candidate_comparison_fulfills_matching_entity_addition() -> None:
    baseline: dict[str, object] = {"entities": [], "entity_relationships": []}
    proposal = _proposal(
        {"entities": [{"id": "reset-api", "action": "added"}]}, baseline
    )
    candidate: dict[str, object] = {
        "entities": [{"id": "reset-api"}],
        "entity_relationships": [],
    }

    report = compare_candidate_snapshot(
        proposal, baseline, candidate, extraction_complete=True
    )

    assert report["passed"] is True
    assert report["findings"][0]["status"] == "fulfilled"
    assert report["fingerprint"].startswith("sha256:")


def test_candidate_comparison_fulfills_matching_entity_change() -> None:
    baseline: dict[str, object] = {
        "entities": [{"id": "reset-api", "description": "Old behavior"}],
        "entity_relationships": [],
    }
    proposal = _proposal(
        {
            "entities": [
                {
                    "id": "reset-api",
                    "action": "changed",
                    "description": "New behavior",
                }
            ]
        },
        baseline,
    )
    candidate: dict[str, object] = {
        "entities": [{"id": "reset-api", "description": "New behavior"}],
        "entity_relationships": [],
    }

    report = compare_candidate_snapshot(
        proposal, baseline, candidate, extraction_complete=True
    )

    assert report["passed"] is True
    assert report["findings"][0]["status"] == "fulfilled"


def test_candidate_comparison_blocks_missing_required_operation() -> None:
    baseline: dict[str, object] = {"entities": [], "entity_relationships": []}
    proposal = _proposal(
        {"entities": [{"id": "reset-api", "action": "added"}]}, baseline
    )

    report = compare_candidate_snapshot(
        proposal, baseline, baseline, extraction_complete=True
    )

    assert report["passed"] is False
    assert report["findings"][0]["status"] == "missing"


def test_candidate_comparison_marks_baseline_satisfied_operation() -> None:
    baseline: dict[str, object] = {
        "entities": [{"id": "reset-api"}],
        "entity_relationships": [],
    }
    proposal = _proposal(
        {"entities": [{"id": "reset-api", "action": "added"}]}, baseline
    )

    report = compare_candidate_snapshot(
        proposal, baseline, baseline, extraction_complete=True
    )

    assert report["passed"] is True
    assert report["findings"][0]["status"] == "already_satisfied"


def test_candidate_comparison_reports_unexpected_changes() -> None:
    baseline: dict[str, object] = {"entities": [], "entity_relationships": []}
    proposal = _proposal({}, baseline)
    candidate: dict[str, object] = {
        "entities": [{"id": "helper-api"}],
        "entity_relationships": [],
    }

    report = compare_candidate_snapshot(
        proposal, baseline, candidate, extraction_complete=True
    )

    assert report["passed"] is False
    assert report["findings"][0]["status"] == "unexpected"


def test_candidate_comparison_keeps_declarations_unresolved() -> None:
    baseline: dict[str, object] = {"entities": [], "entity_relationships": []}
    proposal = _proposal(
        {"features": [{"id": "behavior", "action": "added"}]}, baseline
    )
    candidate: dict[str, object] = {"entities": [], "entity_relationships": []}

    report = compare_candidate_snapshot(
        proposal, baseline, candidate, extraction_complete=True
    )

    assert report["passed"] is False
    assert report["findings"][0]["status"] == "not_evaluable"
    assert "behavior or invariant evidence" in report["findings"][0]["reason"]


def test_candidate_comparison_rejects_proposal_from_another_baseline() -> None:
    baseline: dict[str, object] = {"entities": [], "entity_relationships": []}
    proposal = _proposal({}, baseline)

    with pytest.raises(ValueError, match="baseline fingerprint"):
        compare_candidate_snapshot(
            proposal,
            {
                "entities": [{"id": "changed"}],
                "entity_relationships": [],
            },
            baseline,
            extraction_complete=True,
        )


def test_candidate_comparison_fails_closed_on_incomplete_extraction() -> None:
    baseline: dict[str, object] = {"entities": [], "entity_relationships": []}
    proposal = _proposal({}, baseline)

    report = compare_candidate_snapshot(
        proposal, baseline, baseline, extraction_complete=False
    )

    assert report["passed"] is False
    assert report["extraction_complete"] is False
