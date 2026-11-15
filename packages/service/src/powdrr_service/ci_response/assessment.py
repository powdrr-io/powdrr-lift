"""Deterministic required-check outcome policy for CI response observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from powdrr_contracts.ci import CIObservation

CIResponseState = Literal["awaiting_ci", "awaiting_review", "failed", "blocked"]


@dataclass(frozen=True, slots=True)
class CIResponseAssessment:
    """A conservative terminal or wait decision for one exact-head observation."""

    state: CIResponseState
    reason: str


def assess_ci_observation(
    observation: CIObservation, *, pull_request_draft: bool | None = None
) -> CIResponseAssessment:
    """Never infer success from absent or unknown required-check policy."""
    if pull_request_draft is True:
        return CIResponseAssessment(
            "blocked", "The pull request is a draft and is not ready for review."
        )
    if observation.required_checks_status != "available":
        return CIResponseAssessment(
            "blocked",
            "GitHub required-check policy is unavailable; review readiness is unknown.",
        )

    states = [
        item.state.casefold().replace(" ", "_") for item in observation.required_checks
    ]
    if not states:
        return CIResponseAssessment(
            "blocked", "No required checks were observed; review readiness is unknown."
        )

    failed = {
        "fail",
        "failure",
        "cancel",
        "cancelled",
        "canceled",
        "timed_out",
        "action_required",
        "skipped",
        "neutral",
        "stale",
    }
    if any(state in failed for state in states):
        return CIResponseAssessment(
            "failed",
            "One or more required checks did not succeed on the current PR head.",
        )
    passed = {"pass", "passed", "success", "successful"}
    pending = {"pending", "queued", "in_progress", "expected", "waiting"}
    if all(state in passed for state in states):
        return CIResponseAssessment(
            "awaiting_review",
            "All observed required checks passed on the current PR head.",
        )
    if all(state in passed | pending for state in states) and any(
        state in pending for state in states
    ):
        return CIResponseAssessment(
            "awaiting_ci", "One or more required checks are still running."
        )
    return CIResponseAssessment(
        "blocked", "A required check has an unknown state; review readiness is unknown."
    )
