"""Deterministic research selection for externally defined CI failures."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from powdrr_contracts.ci import CIJobObservation

from powdrr_service.ci_response.evidence import CIFailureUnit

_RUNNER_LABEL_RE = re.compile(
    r"(?<![a-z0-9_-])(?P<label>(?:ubuntu|macos|windows)-\d{2,4}"
    r"(?:\.\d{2})?(?:-[a-z0-9]+)*)(?![a-z0-9_.-])",
    re.IGNORECASE,
)
_REPOSITORY_VISIBILITIES = {"public", "private", "internal", "unknown"}


@dataclass(frozen=True, slots=True)
class CIResearchQuery:
    """One bounded question about an external CI platform contract."""

    profile: str
    query: str
    research_question: str
    public_identifiers: tuple[str, ...]
    applicability: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "query": self.query,
            "research_question": self.research_question,
            "public_identifiers": list(self.public_identifiers),
            "applicability": dict(self.applicability),
        }


@dataclass(frozen=True, slots=True)
class CIResearchPlan:
    """A deterministic decision to research one CI failure or skip research."""

    decision: Literal["research", "skip"]
    reason_code: str
    failure_id: str
    head_sha: str
    queries: tuple[CIResearchQuery, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "ci-research-plan-v1",
            "decision": self.decision,
            "reason_code": self.reason_code,
            "failure_id": self.failure_id,
            "head_sha": self.head_sha,
            "queries": [query.to_dict() for query in self.queries],
            "limits": {"max_queries": 2},
        }


def build_ci_research_plan(
    failure: CIFailureUnit,
    job: CIJobObservation,
    *,
    repository_visibility: str | None = "unknown",
) -> CIResearchPlan:
    """Select a source query only for a proven, externally owned runner issue.

    Query text is generated from a tightly parsed hosted-runner label. The raw
    annotation, repository identity, and log excerpts never enter the query.
    """
    normalized_visibility = (
        repository_visibility.casefold()
        if isinstance(repository_visibility, str)
        else "unknown"
    )
    visibility = (
        normalized_visibility
        if normalized_visibility in _REPOSITORY_VISIBILITIES
        else "unknown"
    )
    if failure.job_id != job.job_id or failure.head_sha != job.head_sha:
        return _skip(failure, "failure_job_or_head_mismatch")
    if failure.phase != "runner_provisioning":
        return _skip(failure, "failure_has_no_external_runner_question")
    if job.runner_id not in (None, 0) or job.steps:
        return _skip(failure, "runner_was_assigned_or_steps_started")

    label_match = _RUNNER_LABEL_RE.search(failure.signature)
    if label_match is None:
        return _skip(failure, "runner_label_not_safely_identified")
    runner_label = label_match.group("label").lower()
    question = (
        f"Does GitHub Actions currently support hosted runner label "
        f"{runner_label} for repositories with {visibility} visibility, and what "
        "officially documented replacement or limitation applies?"
    )
    query = f"GitHub Actions hosted runner label {runner_label} support"
    return CIResearchPlan(
        decision="research",
        reason_code="unsupported_hosted_runner_label",
        failure_id=failure.failure_id,
        head_sha=failure.head_sha,
        queries=(
            CIResearchQuery(
                profile="github_actions_hosted_runner_support",
                query=query,
                research_question=question,
                public_identifiers=("GitHub Actions", runner_label),
                applicability={
                    "runner_label": runner_label,
                    "repository_visibility": visibility,
                    "runner_assignment": "not_assigned",
                },
            ),
        ),
    )


def _skip(failure: CIFailureUnit, reason_code: str) -> CIResearchPlan:
    return CIResearchPlan(
        decision="skip",
        reason_code=reason_code,
        failure_id=failure.failure_id,
        head_sha=failure.head_sha,
    )


__all__ = ["CIResearchPlan", "CIResearchQuery", "build_ci_research_plan"]
