from __future__ import annotations

from powdrr_lift.workrr.actualization import reconcile_actualization


def _decision(decision_id: str, outcome: str = "pass") -> dict[str, str]:
    return {
        "decision_id": decision_id,
        "outcome": outcome,
        "evidence_fingerprint": "diff-1",
        "explanation": "supported by the observed diff",
    }


def test_actualization_requires_fresh_fulfillment_and_preservation() -> None:
    report = reconcile_actualization(
        proposal_fingerprint="proposal-1",
        diff_fingerprint="diff-1",
        operation_ids=("op-1",),
        retained_clause_ids=("clause-1",),
        unexplained_changes=("semantic-change-review",),
        decisions=(
            _decision("operation:op-1"),
            _decision("intent:clause-1"),
            _decision("unexplained:semantic-change-review"),
        ),
    )

    assert report["passed"] is True
    assert [item["status"] for item in report["findings"]] == [
        "fulfilled",
        "preserved",
        "implementation_detail",
    ]
    assert report["fingerprint"].startswith("sha256:")


def test_actualization_blocks_missing_failed_and_stale_decisions() -> None:
    report = reconcile_actualization(
        proposal_fingerprint="proposal-1",
        diff_fingerprint="diff-1",
        operation_ids=("op-1", "op-2"),
        retained_clause_ids=("clause-1",),
        unexplained_changes=(),
        decisions=(
            _decision("operation:op-1", "fail"),
            {**_decision("intent:clause-1"), "evidence_fingerprint": "old-diff"},
        ),
    )

    assert report["passed"] is False
    assert [item["status"] for item in report["findings"]] == [
        "contradicted",
        "not_evaluable",
        "insufficient_evidence",
    ]


def test_actualization_rejects_duplicate_decisions() -> None:
    decision = _decision("operation:op-1")
    report = reconcile_actualization(
        proposal_fingerprint="proposal-1",
        diff_fingerprint="diff-1",
        operation_ids=("op-1",),
        retained_clause_ids=(),
        unexplained_changes=(),
        decisions=(decision, decision),
    )

    assert report["passed"] is False
