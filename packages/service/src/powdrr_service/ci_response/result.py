"""Build the bounded terminal result passed from CI repair to the issue queue."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_RESPONSE_STATES = {
    "awaiting_ci",
    "awaiting_human",
    "awaiting_review",
    "failed",
    "blocked",
}
_DISPATCH_STATES = {
    "accepted",
    "blocked",
    "human_required",
    "interrupted",
    "not_requested",
    "rejected",
}
_PUBLICATION_STATES = {"blocked", "not_attempted", "published"}
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


def build_ci_response_result(
    *,
    observation_result: Mapping[str, Any],
    repair_result: Mapping[str, Any] | None = None,
    publication_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine the current observation with repair and publication outcomes.

    A successful process exit is not evidence that the response completed. The
    returned state records a blocked action explicitly while retaining the
    observed head and observation identity needed by the issue queue.
    """
    head_sha = observation_result.get("head_sha")
    observation_id = observation_result.get("observation_id")
    observed_state = observation_result.get("response_state")
    if (
        observation_result.get("observation_schema_version") != 1
        or not isinstance(head_sha, str)
        or _SHA_RE.fullmatch(head_sha) is None
        or not isinstance(observation_id, str)
        or _DIGEST_RE.fullmatch(observation_id) is None
        or not isinstance(observed_state, str)
        or observed_state not in _RESPONSE_STATES
    ):
        raise ValueError("CI response finalization requires a valid observation")

    dispatch_status = (
        repair_result.get("dispatch_status")
        if repair_result is not None
        else "not_requested"
    )
    if not isinstance(dispatch_status, str) or dispatch_status not in _DISPATCH_STATES:
        dispatch_status = "blocked"
    publication_status = (
        publication_result.get("publication_status")
        if publication_result is not None
        else "not_attempted"
    )
    if (
        not isinstance(publication_status, str)
        or publication_status not in _PUBLICATION_STATES
    ):
        publication_status = "blocked"

    repair_reasons = _bounded_reasons(repair_result)
    publication_reasons = _bounded_reasons(publication_result)
    response_state = str(observed_state)
    if (
        dispatch_status == "human_required"
        or dispatch_status in {"blocked", "interrupted", "rejected"}
        or publication_status == "blocked"
        or (dispatch_status == "accepted" and publication_status != "published")
        or (
            publication_status == "published"
            and observed_state in {"failed", "blocked"}
        )
    ):
        response_state = (
            "awaiting_human" if dispatch_status == "human_required" else "blocked"
        )

    reason = str(
        observation_result.get("response_reason")
        or observation_result.get("reason")
        or ""
    )[:500]
    if publication_status == "blocked" and publication_reasons:
        reason = "CI repair publication blocked: " + "; ".join(publication_reasons)
    elif dispatch_status == "human_required" and repair_reasons:
        reason = "CI repair requires human action: " + "; ".join(repair_reasons)
    elif dispatch_status in {"blocked", "interrupted", "rejected"} and repair_reasons:
        reason = "CI repair dispatch blocked: " + "; ".join(repair_reasons)
    elif response_state == "blocked" and not reason:
        reason = "CI response did not reach an accepted terminal state."

    waited_seconds = observation_result.get("waited_seconds", 0)
    if isinstance(waited_seconds, bool) or not isinstance(waited_seconds, int):
        waited_seconds = 0
    return {
        "observation_schema_version": 1,
        "head_sha": head_sha,
        "observation_id": observation_id,
        "response_state": response_state,
        "response_reason": reason,
        "required_checks_status": (
            observation_result.get("required_checks_status")
            if isinstance(observation_result.get("required_checks_status"), str)
            and observation_result.get("required_checks_status")
            in {"available", "unavailable"}
            else "unavailable"
        ),
        "waited_seconds": max(0, min(waited_seconds, 3600)),
        "wait_deadline_reached": observation_result.get("wait_deadline_reached")
        is True,
        "repair_dispatch_status": dispatch_status,
        "repair_reasons": repair_reasons,
        "publication_status": publication_status,
        "publication_reasons": publication_reasons,
    }


def _bounded_reasons(result: Mapping[str, Any] | None) -> list[str]:
    if result is None:
        return []
    raw = result.get("reasons")
    if not isinstance(raw, list):
        return []
    return [
        " ".join(reason.split())[:240]
        for reason in raw[:8]
        if isinstance(reason, str) and reason.strip()
    ]


__all__ = ["build_ci_response_result"]
