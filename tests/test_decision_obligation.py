from __future__ import annotations

import importlib.util

import pytest
from powdrr_service.decision_obligation import (
    DecisionOutcome,
    DecisionResult,
    DecisionSpecification,
    DecisionWorklist,
)


def test_legacy_agent_decision_obligation_module_is_removed() -> None:
    assert importlib.util.find_spec("powdrr_lift.core.decision_obligation") is None


def test_worklist_order_and_fingerprint_are_deterministic() -> None:
    first = DecisionSpecification("b", "family", "b", "predicate", "input-b")
    second = DecisionSpecification("a", "family", "a", "predicate", "input-a")

    left = DecisionWorklist.compile((first, second))
    right = DecisionWorklist.compile((second, first))

    assert [item.decision_id for item in left.specifications] == ["a", "b"]
    assert left.fingerprint == right.fingerprint


def test_result_must_match_every_identity_dimension() -> None:
    specification = DecisionSpecification(
        "decision", "family", "subject", "predicate", "input"
    )
    result = DecisionResult(
        "decision",
        DecisionOutcome.PASS,
        "evidence says yes",
        "decision-v1",
        "subject",
        "stale-input",
        "evidence",
    )

    with pytest.raises(ValueError, match="stale decision inputs"):
        result.validate_against(specification)
