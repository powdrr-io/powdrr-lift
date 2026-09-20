from __future__ import annotations

import pytest

from powdrr_lift.core.obligation_review import (
    ObligationEvidencePacket,
    ObligationReviewError,
    aggregate_obligation_reviews,
    bind_obligation_review,
    compile_obligation_evidence_packets,
    compile_targeted_repair_packets,
)


def _packets() -> tuple[ObligationEvidencePacket, ...]:
    return compile_obligation_evidence_packets(
        [
            {"obligation_id": "obligation:001", "description": "Keep entry data."},
            {"obligation_id": "obligation:002", "description": "Clear exit data."},
        ],
        evidence_refs=("git-diff@sha256:diff", "validation@sha256:tests"),
        changed_paths=("src/state.py", "tests/test_state.py"),
        diff_fingerprint="sha256:diff",
    )


def test_review_binding_owns_obligation_identity_and_targeted_repairs() -> None:
    packets = _packets()
    first = bind_obligation_review(
        packets[0],
        {
            "verdict": "preserved",
            "explanation": "Entry behavior is covered.",
        },
    )
    second = bind_obligation_review(
        packets[1],
        {
            "verdict": "altered",
            "explanation": "Exit cleanup is missing.",
        },
    )

    result = aggregate_obligation_reviews(packets, (first, second))
    repairs = compile_targeted_repair_packets(packets, (first, second))

    assert result["passed"] is False
    assert result["failed_obligation_ids"] == ["obligation:002"]
    assert len(repairs) == 1
    assert repairs[0].repair_id == "repair:obligation:002:1"
    assert repairs[0].changed_paths == packets[1].changed_paths


def test_review_binding_rejects_echoed_ids_and_stale_evidence() -> None:
    packet = _packets()[0]

    with pytest.raises(ObligationReviewError, match="structural fields"):
        bind_obligation_review(
            packet,
            {
                "obligation_id": "invented",
                "verdict": "preserved",
                "explanation": "Looks good.",
                "evidence_refs": list(packet.evidence_refs),
            },
        )

    with pytest.raises(ObligationReviewError, match="structural fields"):
        bind_obligation_review(
            packet,
            {
                "verdict": "preserved",
                "explanation": "Looks good.",
                "evidence_refs": list(packet.evidence_refs),
            },
        )
