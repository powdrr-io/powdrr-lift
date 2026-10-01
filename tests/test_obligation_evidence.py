from __future__ import annotations

import pytest

from powdrr_lift.core.implementation_packet import compile_implementation_packet
from powdrr_lift.structrr.obligation_evidence import (
    DiffExpectation,
    EvidenceRoute,
    ObligationEvidenceContract,
    assert_obligation_evidence_complete,
    compile_obligation_evidence_contract,
)


@pytest.mark.parametrize(
    ("kind", "strength", "polarity", "expectation", "route"),
    [
        (
            "interface",
            "must",
            "required",
            DiffExpectation.REQUIRED,
            EvidenceRoute.STRUCTURAL_DIFF,
        ),
        (
            "feature",
            "should",
            "required",
            DiffExpectation.EXPECTED,
            EvidenceRoute.STRUCTURAL_DIFF,
        ),
        (
            "feature",
            "unspecified",
            "required",
            DiffExpectation.UNRESOLVED,
            EvidenceRoute.STRUCTURAL_DIFF,
        ),
        (
            "invariant",
            "must",
            "required",
            DiffExpectation.NONE,
            EvidenceRoute.INVARIANT_REVIEW,
        ),
        (
            "non_goal",
            "must",
            "prohibited",
            DiffExpectation.NONE,
            EvidenceRoute.SCOPE_REVIEW,
        ),
        (
            "entity",
            "may",
            "permitted",
            DiffExpectation.NONE,
            EvidenceRoute.INVARIANT_REVIEW,
        ),
    ],
)
def test_obligation_evidence_classifies_diff_and_review_routes(
    kind: str,
    strength: str,
    polarity: str,
    expectation: DiffExpectation,
    route: EvidenceRoute,
) -> None:
    contract = compile_obligation_evidence_contract(
        obligation_id="obligation:instruction-001",
        clause_id="instruction-001",
        requirement_strength=strength,
        kind=kind,
        polarity=polarity,
    )

    assert contract.diff_expectation is expectation
    assert route in contract.review_routes
    assert ObligationEvidenceContract.from_data(contract.to_data()) == contract


def test_obligation_evidence_fingerprint_rejects_mutated_expectation() -> None:
    contract = compile_obligation_evidence_contract(
        obligation_id="obligation:instruction-001",
        clause_id="instruction-001",
        requirement_strength="must",
        kind="interface",
        polarity="required",
    )
    data = contract.to_data()
    data["diff_expectation"] = "none"

    with pytest.raises(ValueError, match="fingerprint"):
        ObligationEvidenceContract.from_data(data)


def test_obligation_evidence_requires_exact_obligation_coverage() -> None:
    contract = compile_obligation_evidence_contract(
        obligation_id="obligation:instruction-001",
        clause_id="instruction-001",
        requirement_strength="must",
        kind="invariant",
        polarity="required",
    )

    assert_obligation_evidence_complete((contract,), (contract.obligation_id,))
    with pytest.raises(ValueError, match="coverage mismatch"):
        assert_obligation_evidence_complete(
            (contract,), ("obligation:instruction-002",)
        )


def test_worker_packet_explains_structural_and_invariant_evidence_routes() -> None:
    interface = compile_obligation_evidence_contract(
        obligation_id="obligation:instruction-001",
        clause_id="instruction-001",
        requirement_strength="must",
        kind="interface",
        polarity="required",
    )
    invariant = compile_obligation_evidence_contract(
        obligation_id="obligation:instruction-002",
        clause_id="instruction-002",
        requirement_strength="must",
        kind="invariant",
        polarity="required",
    )
    packet = compile_implementation_packet(
        objective="Add a public API while preserving callback order.",
        obligations=("Add the API.", "Preserve callback order."),
        required_tests=(
            {"description": "Verify the API."},
            {"description": "Verify callback order."},
        ),
        allowed_paths=("src", "tests"),
        validation_profiles=("pytest",),
        obligation_evidence_contracts=(
            interface.to_data(),
            invariant.to_data(),
        ),
    )

    rendered = packet.render()
    assert (
        "Instruction obligation evidence expectations:\n"
        "- obligation:instruction-001 (must): diff=required; review routes="
        "structural_diff, behavior_validation. the requested entity or interface "
        "must be present in the candidate diff\n"
        "- obligation:instruction-002 (must): diff=none; review routes="
        "invariant_review, behavior_validation. preservation or prohibition is "
        "established by targeted review and behavior evidence"
    ) in rendered
    assert "diff=required" in rendered
    assert "diff=none" in rendered
    assert "invariant_review" in rendered
    assert packet.from_data(packet.to_data()).to_data() == packet.to_data()
