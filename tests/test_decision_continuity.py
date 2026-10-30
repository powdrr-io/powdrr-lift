from __future__ import annotations

import io
import json
from typing import Any

import pytest
from powdrr_service.planning.multipr_structrr import inherited_decision_records

from powdrr_lift.workrr.decision_continuity import (
    decision_continuity_findings,
    review_inherited_decisions,
)
from powdrr_lift.workrr.llm import WorkflowLLMHTTPError


def test_inherited_decision_inventory_uses_structrr_baseline_only() -> None:
    baseline = {
        "active_intent": [
            {
                "clause_id": "api-stable",
                "kind": "invariant",
                "statement": "Keep the API stable.",
                "active": True,
            },
            {
                "clause_id": "retired",
                "kind": "guidance",
                "statement": "Old guidance.",
                "active": False,
            },
        ],
        "human-decisions": [
            {"id": "storage", "decision": "Use SQLite."},
        ],
        # Canonical active_intent prevents duplicate legacy invariant entries.
        "invariants": [{"id": "api-stable", "description": "Keep the API stable."}],
    }

    records = inherited_decision_records(baseline)

    assert [record["decision_id"] for record in records] == [
        "active_intent:api-stable",
        "human-decisions:storage",
    ]


def test_each_inherited_decision_gets_an_independent_review() -> None:
    class ReviewClient:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = []

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            self.messages.extend(messages)
            payload = json.loads(messages[1]["content"])
            decision_id = payload["decision"]["decision_id"]
            return {
                "outcome": "pass" if decision_id == "one" else "fail",
                "explanation": f"Reviewed {decision_id} individually.",
            }

    client = ReviewClient()
    decisions = (
        {"decision_id": "one", "section": "invariants", "record": {"id": "one"}},
        {"decision_id": "two", "section": "human-decisions", "record": {"id": "two"}},
    )
    results = review_inherited_decisions(
        client,
        phase="planning",
        decisions=decisions,
        evidence={"plan_item": {"objective": "Implement feature."}},
    )

    assert [result["outcome"] for result in results] == ["pass", "fail"]
    assert len(client.messages) == 4
    assert "one" in client.messages[1]["content"]
    assert "two" in client.messages[3]["content"]
    assert decision_continuity_findings(results) == (
        "inherited decision 'two': fail; Reviewed two individually.",
    )


def test_missing_reviewer_is_unknown_and_blocks_continuity() -> None:
    results = review_inherited_decisions(
        None,
        phase="implementation",
        decisions=({"decision_id": "invariant:one"},),
        evidence={},
    )

    assert results[0]["outcome"] == "unknown"
    assert decision_continuity_findings(results)


def test_provider_legacy_verdict_reason_shape_is_normalized() -> None:
    class ReviewClient:
        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            return {
                "decision_id": "guidance:bootstrap-review-before-edit",
                "verdict": "pass",
                "reason": "The proposed additions do not use the snapshot.",
            }

    result = review_inherited_decisions(
        ReviewClient(),
        phase="planning",
        decisions=(
            {
                "decision_id": "guidance:bootstrap-review-before-edit",
                "record": {"description": "Review inferred entities first."},
            },
        ),
        evidence={"plan_item": {"objective": "Add CI."}},
    )

    assert result == (
        {
            "decision_id": "guidance:bootstrap-review-before-edit",
            "outcome": "pass",
            "explanation": "The proposed additions do not use the snapshot.",
        },
    )
    assert decision_continuity_findings(result) == ()


def test_inherited_decision_review_retries_http_429_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RateLimitedThenPass:
        calls = 0

        def complete_json(
            self,
            _messages: list[dict[str, str]],
            *,
            response_schema: dict[str, Any] | None = None,
        ) -> dict[str, str]:
            del response_schema
            self.calls += 1
            if self.calls == 1:
                raise WorkflowLLMHTTPError("OpenAI", 429, "engine overloaded")
            return {"outcome": "pass", "explanation": "Reviewed after retry."}

    delays: list[float] = []
    monkeypatch.setattr("powdrr_lift.workrr.llm.time.sleep", delays.append)
    client = RateLimitedThenPass()
    results = review_inherited_decisions(
        client,
        phase="planning",
        decisions=({"decision_id": "guidance:one"},),
        evidence={"plan_item": {"objective": "Add CI."}},
        max_provider_retries=2,
        retry_backoff_seconds=1.5,
        stderr=io.StringIO(),
    )

    assert results[0]["outcome"] == "pass"
    assert client.calls == 2
    assert delays == [1.5]
