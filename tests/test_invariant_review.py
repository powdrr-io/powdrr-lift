from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from powdrr_lift.core.decision_obligation import DecisionOutcome, DecisionResult
from powdrr_lift.structrr.invariant_review import (
    InvariantApplicability,
    InvariantOrigin,
    InvariantRequirementBasis,
    InvariantReviewObligation,
    build_invariant_review_result,
    compile_invariant_review_receipt,
    compile_invariant_review_worklist,
    load_invariant_review_receipt,
    write_invariant_review_receipt,
)
from powdrr_lift.structrr.obligation_evidence import NormativeStrength

_CONTEXT = {
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
            **{**_CONTEXT, "candidate_product_digest": "sha256:other"},
        )
    with pytest.raises(ValueError, match="stale proposal"):
        receipt.assert_current(
            obligations,
            **{**_CONTEXT, "proposal_fingerprint": "sha256:other"},
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
