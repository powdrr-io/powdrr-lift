from __future__ import annotations

import pytest
from powdrr_service.structrr.route_evidence_reconciliation import (
    reconcile_obligation_route_evidence,
)


def _contracts() -> list[dict[str, object]]:
    return [
        {
            "obligation_id": "obligation:retry",
            "review_routes": ["structural_diff", "behavior_validation"],
        }
    ]


def _record(route: str, **overrides: object) -> dict[str, object]:
    return {
        "obligation_id": "obligation:retry",
        "route": route,
        "candidate_tree": "sha256:candidate",
        "status": "passed",
        **overrides,
    }


def test_route_reconciliation_accepts_exact_passing_candidate_records() -> None:
    result = reconcile_obligation_route_evidence(
        _contracts(),
        [_record("structural_diff"), _record("behavior_validation")],
        candidate_tree="sha256:candidate",
    )

    assert result["passed"] is True
    assert result["issues"] == []
    assert result["expected_route_count"] == 2


@pytest.mark.parametrize(
    ("records", "kind"),
    [
        ([_record("structural_diff")], "missing_route"),
        (
            [
                _record("structural_diff"),
                _record("structural_diff"),
                _record("behavior_validation"),
            ],
            "duplicate_route",
        ),
        (
            [
                _record("structural_diff"),
                _record("behavior_validation", candidate_tree="sha256:old"),
            ],
            "stale_candidate",
        ),
        (
            [
                _record("structural_diff"),
                _record("behavior_validation", status="not_collected"),
            ],
            "not_collected",
        ),
        (
            [
                _record("structural_diff"),
                _record("behavior_validation", status="failed"),
            ],
            "failed",
        ),
        (
            [
                _record("structural_diff"),
                _record("behavior_validation", status="unsupported"),
            ],
            "invalid_status",
        ),
        (
            [
                _record("structural_diff"),
                _record("behavior_validation"),
                _record("orphaned_route"),
            ],
            "unlinked_route",
        ),
        (
            [
                _record("structural_diff"),
                {"status": "passed", "candidate_tree": "sha256:candidate"},
            ],
            "unlinked_route",
        ),
    ],
)
def test_route_reconciliation_rejects_incomplete_or_untrusted_records(
    records: list[dict[str, object]], kind: str
) -> None:
    result = reconcile_obligation_route_evidence(
        _contracts(), records, candidate_tree="sha256:candidate"
    )

    assert result["passed"] is False
    assert kind in {issue["kind"] for issue in result["issues"]}


def test_route_reconciliation_rejects_duplicate_compiled_route_pairs() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        reconcile_obligation_route_evidence(
            [
                {
                    "obligation_id": "obligation:retry",
                    "review_routes": ["structural_diff", "structural_diff"],
                }
            ],
            [],
            candidate_tree="sha256:candidate",
        )
