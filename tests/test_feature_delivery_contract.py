from __future__ import annotations

from copy import deepcopy

import pytest

from powdrr_lift.core import (
    build_feature_coverage_handoff,
    compile_feature_delivery_contract,
)


def _feature() -> dict:
    return {
        "id": "search",
        "requirements": [
            {"id": "req-index", "description": "Index searchable records."},
            {"id": "req-query", "description": "Return matching records."},
        ],
        "acceptance_criteria": [
            {"id": "ac-results", "description": "Queries return matches."}
        ],
        "expected_tests": [
            {"id": "test-search", "description": "Search behavior is tested."}
        ],
        "modules": [
            {"id": "search-index", "action": "added", "purpose": "Index records."}
        ],
        "features": [
            {
                "id": "search-api",
                "action": "added",
                "description": "Search records.",
            }
        ],
    }


def _proposed_prs() -> dict:
    return {
        "id": "search",
        "feature_ids": ["search-api"],
        "proposed_prs": [
            {
                "id": "search-index-pr",
                "intent": "Build the index.",
                "justification": "Queries need indexed data.",
                "dependent_prs": [],
            },
            {
                "id": "search-api-pr",
                "intent": "Expose search.",
                "justification": "Complete the feature.",
                "dependent_prs": ["search-index-pr"],
            },
        ],
        "modules": [
            {
                "id": "search-index",
                "action": "added",
                "proposed_pr_id": "search-index-pr",
            }
        ],
        "features": [
            {
                "id": "search-api",
                "action": "added",
                "proposed_pr_id": "search-api-pr",
            }
        ],
    }


def _allocation() -> dict:
    return {
        "assignments": [
            {"item_ref": "requirements:req-index", "proposed_pr_id": "search-index-pr"},
            {"item_ref": "requirements:req-query", "proposed_pr_id": "search-api-pr"},
            {
                "item_ref": "acceptance_criteria:ac-results",
                "proposed_pr_id": "search-api-pr",
            },
            {
                "item_ref": "expected_tests:test-search",
                "proposed_pr_id": "search-api-pr",
            },
        ]
    }


def test_compiler_produces_total_fingerprinted_feature_contract() -> None:
    contract = compile_feature_delivery_contract(
        _feature(), _proposed_prs(), _allocation()
    )

    assert contract.feature_id == "search"
    assert contract.fingerprint.startswith("sha256:")
    assert [item.proposed_pr_id for item in contract.proposed_prs] == [
        "search-index-pr",
        "search-api-pr",
    ]
    assert contract.proposed_prs[0].coverage_refs == ("requirements:req-index",)
    assert contract.proposed_prs[1].dependent_pr_ids == ("search-index-pr",)
    assert contract.proposed_prs[1].coverage_refs == (
        "acceptance_criteria:ac-results",
        "expected_tests:test-search",
        "requirements:req-query",
    )
    assert {item.effect_ref for item in contract.effects} == {
        "features:search-api:added",
        "modules:search-index:added",
    }


def test_allocation_order_does_not_change_contract() -> None:
    allocation = _allocation()
    reordered = {"assignments": list(reversed(allocation["assignments"]))}

    first = compile_feature_delivery_contract(_feature(), _proposed_prs(), allocation)
    second = compile_feature_delivery_contract(_feature(), _proposed_prs(), reordered)

    assert second.to_data() == first.to_data()


@pytest.mark.parametrize("case", ["missing", "duplicate", "unknown-item", "unknown-pr"])
def test_compiler_rejects_invalid_coverage_allocation(case: str) -> None:
    allocation = _allocation()
    if case == "missing":
        allocation["assignments"].pop()
    elif case == "duplicate":
        allocation["assignments"].append(dict(allocation["assignments"][0]))
    elif case == "unknown-item":
        allocation["assignments"][0]["item_ref"] = "requirements:not-real"
    else:
        allocation["assignments"][0]["proposed_pr_id"] = "not-real"

    with pytest.raises(ValueError):
        compile_feature_delivery_contract(_feature(), _proposed_prs(), allocation)


@pytest.mark.parametrize("case", ["missing", "duplicate", "unknown-pr"])
def test_compiler_rejects_effect_drift(case: str) -> None:
    proposed = _proposed_prs()
    if case == "missing":
        proposed["modules"] = []
    elif case == "duplicate":
        proposed["modules"].append(dict(proposed["modules"][0]))
    else:
        proposed["modules"][0]["proposed_pr_id"] = "not-real"

    with pytest.raises(ValueError):
        compile_feature_delivery_contract(_feature(), proposed, _allocation())


def test_compiler_rejects_feature_identity_drift() -> None:
    proposed = _proposed_prs()
    proposed["feature_ids"] = ["different-feature"]

    with pytest.raises(ValueError, match="feature_ids do not match"):
        compile_feature_delivery_contract(_feature(), proposed, _allocation())


def test_compiler_rejects_dependency_cycles_and_empty_prs() -> None:
    proposed = _proposed_prs()
    proposed["proposed_prs"][0]["dependent_prs"] = ["search-api-pr"]
    with pytest.raises(ValueError, match="cycle"):
        compile_feature_delivery_contract(_feature(), proposed, _allocation())

    proposed = _proposed_prs()
    proposed["proposed_prs"].append(
        {
            "id": "empty-pr",
            "intent": "Do nothing.",
            "justification": "Invalid fixture.",
            "dependent_prs": [],
        }
    )
    with pytest.raises(ValueError, match="no assigned feature work"):
        compile_feature_delivery_contract(_feature(), proposed, _allocation())


def test_handoff_is_source_owned_and_source_changes_invalidate_contract() -> None:
    feature = _feature()
    handoff = build_feature_coverage_handoff(feature)
    first = compile_feature_delivery_contract(feature, _proposed_prs(), _allocation())
    changed = deepcopy(feature)
    changed["requirements"][0]["description"] = "Index every searchable record."
    second = compile_feature_delivery_contract(changed, _proposed_prs(), _allocation())

    assert [item["item_ref"] for item in handoff["items"]] == [
        "requirements:req-index",
        "requirements:req-query",
        "acceptance_criteria:ac-results",
        "expected_tests:test-search",
    ]
    assert second.feature_fingerprint != first.feature_fingerprint
    assert second.fingerprint != first.fingerprint
