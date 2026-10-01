from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.structrr.candidate_comparison import (
    CandidateScopeDecision,
    compare_candidate_snapshot,
)
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


def test_candidate_comparison_rejects_matching_identity_with_wrong_after_values() -> (
    None
):
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
                    "description": "Requested behavior",
                }
            ]
        },
        baseline,
    )
    candidate: dict[str, object] = {
        "entities": [{"id": "reset-api", "description": "Different behavior"}],
        "entity_relationships": [],
    }

    report = compare_candidate_snapshot(
        proposal, baseline, candidate, extraction_complete=True
    )

    assert report["passed"] is False
    assert report["findings"][0]["status"] == "contradictory"
    assert "after-values" in report["findings"][0]["reason"]


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


def test_candidate_comparison_accepts_evidenced_in_scope_helper_detail(
    tmp_path: Path,
) -> None:
    from powdrr_lift.structrr.actual_diff import compile_actual_structrr_diff
    from powdrr_lift.structrr.source_manifest import compile_source_manifest

    root = tmp_path
    (root / "src").mkdir()
    (root / "src/helpers.py").write_text("def helper(): pass\n", encoding="utf-8")
    baseline: dict[str, Any] = {
        "entities": [],
        "entity_relationships": [],
        "source_subjects": [],
        "source_bindings": [],
    }
    candidate = {
        **baseline,
        "source_subjects": [
            {
                "stable_key": "python::src.helpers.helper",
                "path": "src/helpers.py",
                "kind": "function",
                "qualified_name": "src.helpers.helper",
            }
        ],
    }
    proposal = _proposal({}, baseline)
    baseline_manifest = compile_source_manifest(
        root,
        (),
        submission_base="base",
        source_revision="base",
        taxonomy_fingerprint="taxonomy",
    )
    candidate_manifest = compile_source_manifest(
        root,
        ("src/helpers.py",),
        submission_base="base",
        source_revision="candidate",
        taxonomy_fingerprint="taxonomy",
    )
    actual_diff = compile_actual_structrr_diff(
        baseline, candidate, baseline_manifest, candidate_manifest
    )
    finding_id = "observed:source_subject:python::src.helpers.helper:added"
    decision = CandidateScopeDecision(
        finding_id=finding_id,
        classification="necessary_detail",
        rationale="The private helper supports the requested implementation.",
        evidence_paths=("src/helpers.py",),
        proposal_fingerprint=proposal.fingerprint,
        candidate_fingerprint=actual_diff.candidate_snapshot_fingerprint,
        actual_diff_fingerprint=actual_diff.fingerprint,
        reviewer_version="scope-review-v1",
    )

    report = compare_candidate_snapshot(
        proposal,
        baseline,
        candidate,
        extraction_complete=True,
        actual_diff=actual_diff,
        scope_decisions=(decision,),
    )

    assert report["passed"] is True
    assert report["findings"][0]["status"] == "necessary_detail"
