from __future__ import annotations

import pytest
from powdrr_service.github_worker_state import WorkItemIdentity, WorkItemState


def state(**changes: object) -> WorkItemState:
    values: dict[str, object] = {
        "identity": WorkItemIdentity(
            "powdrr-io/example",
            909,
            "plan",
            "item-0002",
            "sha256:" + "a" * 64,
        ),
        "item_state": "active",
        "attempt_outcome": "failed",
        "attempt_id": "attempt-1",
        "owner_generation": 2,
        "publication_state": "published",
        "ci_state": "unknown",
        "evidence_state": "confirmed",
        "pull_requests": ({"number": 1037, "merged": True},),
    }
    values.update(changes)
    return WorkItemState(**values)  # type: ignore[arg-type]


def test_failed_attempt_does_not_erase_published_work_item() -> None:
    item = state()

    assert item.attempt_outcome == "failed"
    assert item.item_state == "active"
    assert item.pull_requests[0]["number"] == 1037
    assert WorkItemState.from_data(item.to_data()) == item


@pytest.mark.parametrize(
    "changes",
    [
        {"publication_state": "ambiguous", "item_state": "fulfilled"},
        {"evidence_state": "unknown", "item_state": "fulfilled"},
        {"publication_state": "none", "item_state": "fulfilled"},
        {"ci_state": "passed"},
        {
            "pull_requests": ({"number": 3}, {"number": 4}),
            "publication_state": "published",
        },
    ],
)
def test_conflicting_or_unbound_evidence_cannot_advance_state(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        state(**changes)


def test_passed_ci_must_match_head_and_policy() -> None:
    item = state(
        ci_state="passed",
        observed_head="head-a",
        ci_head="head-a",
        check_policy_digest="sha256:" + "b" * 64,
    )

    assert item.ci_state == "passed"
    with pytest.raises(ValueError, match="bind the observed head"):
        state(ci_state="passed", observed_head="head-a", ci_head="head-b")


def test_old_unbound_evidence_remains_unknown() -> None:
    legacy = state(evidence_state="unknown", publication_state="pending")

    assert legacy.item_state == "active"
    assert legacy.evidence_state == "unknown"
