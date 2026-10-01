from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, TypedDict

import pytest

from powdrr_lift.core.decision_obligation import DecisionOutcome, DecisionResult
from powdrr_lift.structrr.actual_diff import (
    StructrrActualDiff,
    compile_actual_structrr_diff,
)
from powdrr_lift.structrr.candidate_comparison import compare_candidate_snapshot
from powdrr_lift.structrr.invariant_review import (
    InvariantApplicability,
    InvariantOrigin,
    InvariantRequirementBasis,
    InvariantReviewAssessment,
    InvariantReviewEvidencePacket,
    InvariantReviewObligation,
    build_invariant_review_result,
    compile_invariant_review_receipt,
    compile_invariant_review_worklist,
    load_invariant_review_receipt,
    review_candidate_invariants,
    write_invariant_review_receipt,
    write_invariant_review_run,
)
from powdrr_lift.structrr.obligation_evidence import NormativeStrength
from powdrr_lift.structrr.proposal import ProposalRevision, compile_proposal_revision
from powdrr_lift.structrr.source_manifest import compile_source_manifest


class _ReviewContext(TypedDict):
    proposal_fingerprint: str
    candidate_product_digest: str
    extraction_version: str
    reviewer_version: str


_CONTEXT: _ReviewContext = {
    "proposal_fingerprint": "sha256:proposal",
    "candidate_product_digest": "sha256:candidate",
    "extraction_version": "bootstrap-v1/python-v1",
    "reviewer_version": "reviewer-2026-09",
}


def _obligation(
    invariant_id: str = "callback-order",
    *,
    applicability: InvariantApplicability = InvariantApplicability.APPLICABLE,
    required: bool = True,
    strength: NormativeStrength = NormativeStrength.MUST,
) -> InvariantReviewObligation:
    return InvariantReviewObligation(
        invariant_id=invariant_id,
        origin=InvariantOrigin.INSTRUCTION,
        normative_strength=strength,
        required=required,
        requirement_basis=(
            InvariantRequirementBasis.NORMATIVE_MUST
            if strength is NormativeStrength.MUST
            else InvariantRequirementBasis.ACCEPTED_SHOULD
            if strength is NormativeStrength.SHOULD and required
            else InvariantRequirementBasis.ADVISORY
        ),
        applicability=applicability,
        applicability_rationale="the changed dispatch path may affect callback order",
        protected_subjects=("dispatch callbacks",),
        impact_selectors=("src/dispatch.py::dispatch",),
        review_method="source_and_behavior_review",
        required_evidence=("source:candidate-dispatch", "test:callback-order"),
    )


def _decisions(
    obligations: tuple[InvariantReviewObligation, ...],
    outcomes: Mapping[str, DecisionOutcome],
) -> tuple[DecisionResult, ...]:
    worklist = compile_invariant_review_worklist(obligations, **_CONTEXT)
    return tuple(
        build_invariant_review_result(
            specification,
            outcome=outcomes.get(specification.subject, DecisionOutcome.PASS),
            explanation=f"reviewed {specification.subject} against its evidence",
        )
        for specification in worklist.specifications
    )


def test_invariant_review_requires_fresh_pass_for_each_applicable_invariant(
    tmp_path: Path,
) -> None:
    obligations = (_obligation(), _obligation("network-isolation"))
    decisions = _decisions(obligations, {})
    receipt = compile_invariant_review_receipt(obligations, decisions, **_CONTEXT)

    assert receipt.passed is True
    assert len(receipt.decisions) == 2
    receipt.assert_current(obligations, **_CONTEXT)
    path = tmp_path / "invariant-review.json"
    write_invariant_review_receipt(path, receipt)
    loaded = load_invariant_review_receipt(path)
    loaded.assert_current(obligations, **_CONTEXT)
    assert loaded.to_data() == receipt.to_data()


@pytest.mark.parametrize(
    ("outcome", "expected_passed"),
    [(DecisionOutcome.FAIL, False), (DecisionOutcome.UNKNOWN, False)],
)
def test_invariant_failure_and_unknown_block_acceptance(
    outcome: DecisionOutcome, expected_passed: bool
) -> None:
    obligations = (_obligation(),)
    receipt = compile_invariant_review_receipt(
        obligations,
        _decisions(obligations, {"callback-order": outcome}),
        **_CONTEXT,
    )

    assert receipt.passed is expected_passed


def test_unknown_applicability_cannot_pass_invariant_review() -> None:
    obligations = (_obligation(applicability=InvariantApplicability.UNKNOWN),)
    with pytest.raises(ValueError, match="requires unknown outcome"):
        compile_invariant_review_receipt(
            obligations,
            _decisions(obligations, {"callback-order": DecisionOutcome.PASS}),
            **_CONTEXT,
        )
    receipt = compile_invariant_review_receipt(
        obligations,
        _decisions(obligations, {"callback-order": DecisionOutcome.UNKNOWN}),
        **_CONTEXT,
    )

    assert receipt.passed is False


def test_non_applicable_invariant_requires_review_of_its_evidence() -> None:
    obligation = _obligation(applicability=InvariantApplicability.NOT_APPLICABLE)
    worklist = compile_invariant_review_worklist((obligation,), **_CONTEXT)
    decisions = _decisions((obligation,), {})
    receipt = compile_invariant_review_receipt((obligation,), decisions, **_CONTEXT)

    assert len(worklist.specifications) == 1
    assert "non-applicability" in worklist.specifications[0].predicate
    assert receipt.passed is True
    assert receipt.obligation_fingerprints == (
        (obligation.invariant_id, obligation.fingerprint),
    )


def test_receipt_rejects_missing_duplicate_and_unknown_decisions() -> None:
    obligations = (_obligation(),)
    decision = _decisions(obligations, {})[0]

    with pytest.raises(ValueError, match="incomplete"):
        compile_invariant_review_receipt(obligations, (), **_CONTEXT)
    with pytest.raises(ValueError, match="duplicate"):
        compile_invariant_review_receipt(obligations, (decision, decision), **_CONTEXT)
    unknown = replace(decision, decision_id="invariant:other")
    with pytest.raises(ValueError, match="unknown decisions"):
        compile_invariant_review_receipt(obligations, (unknown,), **_CONTEXT)


def test_receipt_rejects_stale_candidate_proposal_and_obligation() -> None:
    obligations = (_obligation(),)
    receipt = compile_invariant_review_receipt(
        obligations, _decisions(obligations, {}), **_CONTEXT
    )

    with pytest.raises(ValueError, match="stale candidate"):
        receipt.assert_current(
            obligations,
            proposal_fingerprint=_CONTEXT["proposal_fingerprint"],
            candidate_product_digest="sha256:other",
            extraction_version=_CONTEXT["extraction_version"],
            reviewer_version=_CONTEXT["reviewer_version"],
        )
    with pytest.raises(ValueError, match="stale proposal"):
        receipt.assert_current(
            obligations,
            proposal_fingerprint="sha256:other",
            candidate_product_digest=_CONTEXT["candidate_product_digest"],
            extraction_version=_CONTEXT["extraction_version"],
            reviewer_version=_CONTEXT["reviewer_version"],
        )
    changed_obligation = (
        replace(
            obligations[0],
            impact_selectors=("src/dispatch.py::other",),
        ),
    )
    with pytest.raises(ValueError, match="stale obligations"):
        receipt.assert_current(changed_obligation, **_CONTEXT)


def test_strength_cannot_silently_downgrade_must_or_promote_may() -> None:
    with pytest.raises(ValueError, match="requirement basis"):
        _obligation(required=False)
    with pytest.raises(ValueError, match="requirement basis"):
        _obligation(required=True, strength=NormativeStrength.MAY)


def test_should_invariant_records_its_acceptance_basis() -> None:
    obligation = _obligation(strength=NormativeStrength.SHOULD)
    assert obligation.requirement_basis is InvariantRequirementBasis.ACCEPTED_SHOULD
    assert InvariantReviewObligation.from_data(obligation.to_data()) == obligation

    with pytest.raises(ValueError, match="requirement basis"):
        replace(obligation, requirement_basis=InvariantRequirementBasis.ADVISORY)


def _comparison_with_body_change(
    tmp_path: Path,
) -> tuple[ProposalRevision, StructrrActualDiff, dict[str, Any]]:
    baseline_root = tmp_path / "baseline"
    candidate_root = tmp_path / "candidate"
    (baseline_root / "src").mkdir(parents=True)
    (candidate_root / "src").mkdir(parents=True)
    source_path = "src/dispatch.py"
    (baseline_root / source_path).write_text(
        "def dispatch(): return callbacks\n", encoding="utf-8"
    )
    (candidate_root / source_path).write_text(
        "def dispatch(): return reversed(callbacks)\n", encoding="utf-8"
    )
    snapshot: dict[str, Any] = {
        "entities": [],
        "entity_relationships": [],
        "source_subjects": [],
        "source_bindings": [],
    }
    proposal = compile_proposal_revision(
        "preserve-callback-order",
        snapshot,
        {},
        acceptance_criteria=("preserve callback order",),
        must_preserve=("callbacks run in registration order",),
        non_goals=(),
        allowed_paths=("src", "tests"),
        source_refs=("instruction:callback-order",),
    )
    baseline_manifest = compile_source_manifest(
        baseline_root,
        (source_path,),
        submission_base="submission-base",
        source_revision="baseline-revision",
        taxonomy_fingerprint="taxonomy-v1",
    )
    candidate_manifest = compile_source_manifest(
        candidate_root,
        (source_path,),
        submission_base="submission-base",
        source_revision="candidate-revision",
        taxonomy_fingerprint="taxonomy-v1",
    )
    actual_diff = compile_actual_structrr_diff(
        snapshot, snapshot, baseline_manifest, candidate_manifest
    )
    comparison = compare_candidate_snapshot(
        proposal,
        snapshot,
        snapshot,
        extraction_complete=True,
        actual_diff=actual_diff,
    )
    return proposal, actual_diff, comparison


def test_review_candidate_invariants_runs_separate_review_after_structural_pass(
    tmp_path: Path,
) -> None:
    proposal, actual_diff, comparison = _comparison_with_body_change(tmp_path)
    assert comparison["passed"] is True
    obligation = replace(
        _obligation(),
        impact_selectors=("src/dispatch.py::dispatch",),
    )
    packets_seen: list[InvariantReviewEvidencePacket] = []

    def reviewer(
        packet: InvariantReviewEvidencePacket,
    ) -> InvariantReviewAssessment:
        packets_seen.append(packet)
        assert dict(packet.candidate_source_context)["src/dispatch.py"] == (
            "callbacks are returned in reverse order"
        )
        assert packet.relevant_paths == ("src/dispatch.py",)
        return InvariantReviewAssessment(
            DecisionOutcome.FAIL,
            "The candidate reverses callbacks despite passing structural comparison.",
        )

    run = review_candidate_invariants(
        (obligation,),
        proposal=proposal,
        actual_diff=actual_diff,
        comparison_report=comparison,
        extraction_version="python-extractor-v1",
        reviewer_version="invariant-reviewer-v1",
        baseline_source={"src/dispatch.py": "callbacks run in registration order"},
        candidate_source={
            "src/dispatch.py": "callbacks are returned in reverse order",
            "source:candidate-dispatch": "candidate source excerpt",
        },
        validation_evidence={"test:callback-order": "pytest passed"},
        reviewer=reviewer,
    )

    assert len(packets_seen) == 1
    assert len(run.packets) == 1
    assert run.packets[0].fingerprint.startswith("sha256:")
    assert run.packets[0].to_data()["accepted_proposal"]["fingerprint"] == (
        proposal.fingerprint
    )
    run_path = tmp_path / "review" / "run.json"
    write_invariant_review_run(run_path, run)
    persisted = json.loads(run_path.read_text(encoding="utf-8"))
    assert persisted["packets"][0]["fingerprint"] == run.packets[0].fingerprint
    assert persisted["receipt"] == run.receipt.to_data()
    assert run.receipt.passed is False
    assert run.receipt.decisions[0].outcome is DecisionOutcome.FAIL
    packet_bindings = ((obligation.invariant_id, run.packets[0].fingerprint),)
    run.receipt.assert_current(
        (obligation,),
        proposal_fingerprint=proposal.fingerprint,
        candidate_product_digest=actual_diff.candidate_product_digest,
        extraction_version="python-extractor-v1",
        reviewer_version="invariant-reviewer-v1",
        review_packet_fingerprints=packet_bindings,
    )
    with pytest.raises(ValueError, match="stale evidence packets"):
        run.receipt.assert_current(
            (obligation,),
            proposal_fingerprint=proposal.fingerprint,
            candidate_product_digest=actual_diff.candidate_product_digest,
            extraction_version="python-extractor-v1",
            reviewer_version="invariant-reviewer-v1",
            review_packet_fingerprints=((obligation.invariant_id, "sha256:other"),),
        )


def test_invariant_review_returns_unknown_without_required_evidence(
    tmp_path: Path,
) -> None:
    proposal, actual_diff, comparison = _comparison_with_body_change(tmp_path)
    obligation = replace(
        _obligation(),
        impact_selectors=("src/dispatch.py::dispatch",),
    )
    reviewer_called = False

    def reviewer(
        packet: InvariantReviewEvidencePacket,
    ) -> InvariantReviewAssessment:
        nonlocal reviewer_called
        reviewer_called = True
        return InvariantReviewAssessment(DecisionOutcome.PASS, "approved")

    run = review_candidate_invariants(
        (obligation,),
        proposal=proposal,
        actual_diff=actual_diff,
        comparison_report=comparison,
        extraction_version="python-extractor-v1",
        reviewer_version="invariant-reviewer-v1",
        baseline_source={"src/dispatch.py": "baseline callback implementation"},
        candidate_source={
            "src/dispatch.py": "candidate callback implementation",
            "source:candidate-dispatch": "candidate source excerpt",
        },
        validation_evidence={},
        reviewer=reviewer,
    )

    assert reviewer_called is False
    assert run.packets[0].missing_evidence == ("test:callback-order",)
    assert run.receipt.passed is False
    assert run.receipt.decisions[0].outcome is DecisionOutcome.UNKNOWN
