from __future__ import annotations

import pytest
from powdrr_service.ci_response.result import build_ci_response_result


def _observation(state: str) -> dict[str, object]:
    return {
        "observation_schema_version": 1,
        "head_sha": "a" * 40,
        "observation_id": "b" * 64,
        "response_state": state,
        "response_reason": "Current checks were observed.",
        "required_checks_status": "available",
        "waited_seconds": 15,
        "wait_deadline_reached": False,
    }


def test_ci_response_result_reports_published_repair_and_fresh_observation() -> None:
    result = build_ci_response_result(
        observation_result=_observation("awaiting_review"),
        repair_result={"dispatch_status": "accepted"},
        publication_result={"publication_status": "published"},
    )

    assert result["response_state"] == "awaiting_review"
    assert result["repair_dispatch_status"] == "accepted"
    assert result["publication_status"] == "published"
    assert result["head_sha"] == "a" * 40
    assert result["observation_id"] == "b" * 64


def test_ci_response_result_preserves_publication_block_reason() -> None:
    result = build_ci_response_result(
        observation_result=_observation("failed"),
        repair_result={"dispatch_status": "accepted"},
        publication_result={
            "publication_status": "blocked",
            "reasons": ["remote PR branch moved after CI observation"],
        },
    )

    assert result["response_state"] == "blocked"
    assert result["publication_status"] == "blocked"
    assert result["publication_reasons"] == [
        "remote PR branch moved after CI observation"
    ]
    assert result["response_reason"] == (
        "CI repair publication blocked: remote PR branch moved after CI observation"
    )


def test_ci_response_result_preserves_interrupted_dispatch_reason() -> None:
    result = build_ci_response_result(
        observation_result=_observation("failed"),
        repair_result={
            "dispatch_status": "interrupted",
            "reasons": ["A saved invocation has no result."],
        },
    )

    assert result["response_state"] == "blocked"
    assert result["repair_dispatch_status"] == "interrupted"
    assert result["repair_reasons"] == ["A saved invocation has no result."]


def test_ci_response_without_repair_keeps_observed_failure_state() -> None:
    result = build_ci_response_result(observation_result=_observation("failed"))

    assert result["response_state"] == "failed"
    assert result["repair_dispatch_status"] == "not_requested"
    assert result["publication_status"] == "not_attempted"


def test_ci_response_result_rejects_missing_observation_identity() -> None:
    with pytest.raises(ValueError, match="valid observation"):
        build_ci_response_result(observation_result={"response_state": "failed"})
