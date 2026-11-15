"""Deterministic research selection for externally defined CI failures."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from powdrr_contracts.ci import CIJobObservation

from powdrr_service.ci_response.evidence import CIFailureUnit
from powdrr_service.external_contract_research import (
    bind_external_contract_search_selections,
)

_RUNNER_LABEL_RE = re.compile(
    r"(?<![a-z0-9_-])(?P<label>(?:ubuntu|macos|windows)-\d{2,4}"
    r"(?:\.\d{2})?(?:-[a-z0-9]+)*)(?![a-z0-9_.-])",
    re.IGNORECASE,
)
_REPOSITORY_VISIBILITIES = {"public", "private", "internal", "unknown"}
_OFFICIAL_GITHUB_RESEARCH_HOSTS = {"docs.github.com", "github.blog"}


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


def prepare_ci_research_search(
    research_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a research plan and adapt it to bounded generic search inputs.

    Plans cross a Procedrr operation boundary as JSON, so reconstruct the only
    supported query from its public runner label rather than forwarding mutable
    query text or identifiers to the search provider.
    """
    if research_plan.get("schema_version") != "ci-research-plan-v1":
        raise ValueError("CI research plan schema version is unsupported")
    if research_plan.get("decision") != "research":
        raise ValueError("CI research subprocess requires a research decision")
    if research_plan.get("reason_code") != "unsupported_hosted_runner_label":
        raise ValueError("CI research plan reason is unsupported")
    queries = research_plan.get("queries")
    if not isinstance(queries, list) or len(queries) != 1:
        raise ValueError("CI research plan must contain exactly one query")
    query = queries[0]
    if not isinstance(query, Mapping):
        raise ValueError("CI research query is malformed")
    applicability = query.get("applicability")
    identifiers = query.get("public_identifiers")
    if not isinstance(applicability, Mapping) or not isinstance(identifiers, list):
        raise ValueError("CI research query provenance is malformed")
    label = applicability.get("runner_label")
    visibility = applicability.get("repository_visibility")
    if (
        not isinstance(label, str)
        or _RUNNER_LABEL_RE.fullmatch(label) is None
        or not isinstance(visibility, str)
        or visibility not in _REPOSITORY_VISIBILITIES
        or applicability.get("runner_assignment") != "not_assigned"
        or identifiers != ["GitHub Actions", label]
        or query.get("profile") != "github_actions_hosted_runner_support"
    ):
        raise ValueError("CI research query is not a safe hosted-runner query")
    expected_question = (
        f"Does GitHub Actions currently support hosted runner label {label} for "
        f"repositories with {visibility} visibility, and what officially "
        "documented replacement or limitation applies?"
    )
    if query.get("research_question") != expected_question:
        raise ValueError("CI research question does not match its applicability")
    failure_id = research_plan.get("failure_id")
    head_sha = research_plan.get("head_sha")
    if (
        not isinstance(failure_id, str)
        or not failure_id.strip()
        or not isinstance(head_sha, str)
        or len(head_sha) not in (40, 64)
        or any(char not in "0123456789abcdef" for char in head_sha)
    ):
        raise ValueError("CI research plan provenance is malformed")

    safe_label = label.lower()
    return {
        "decision": "research",
        "rationale": (
            "A matching runner-provisioning failure occurred before runner "
            "assignment; verify official support for the identified hosted label."
        ),
        "research_context": expected_question,
        "queries": [
            {
                "query": f"GitHub Actions hosted runner label {safe_label} support",
                "research_question": expected_question,
                "profile": "github_actions_hosted_runner_support",
                "why_applicable": (
                    f"The observed pre-runner rejection named {safe_label}; "
                    f"check support for {visibility} repositories."
                ),
            }
        ],
    }


def bind_ci_research_source_selections(
    search_results: Mapping[str, Any], selections: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """Bind at most three model selections, rejecting non-GitHub publishers."""
    if len(selections) > 3:
        raise ValueError("at most three CI research sources may be selected")
    searches = search_results.get("searches")
    if not isinstance(searches, list) or len(searches) > 2:
        raise ValueError("CI research search results are malformed")
    candidates_by_index: dict[tuple[int, int], Mapping[str, Any]] = {}
    for query_index, search in enumerate(searches, start=1):
        if not isinstance(search, Mapping):
            continue
        candidates = search.get("candidates")
        if not isinstance(candidates, list):
            continue
        for fallback_rank, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, Mapping):
                continue
            rank = candidate.get("result_rank", fallback_rank)
            if isinstance(rank, int) and not isinstance(rank, bool):
                candidates_by_index[(query_index, rank)] = candidate

    accepted: list[Mapping[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for selection in selections:
        query_index = selection.get("query_index")
        result_rank = selection.get("result_rank")
        if (
            isinstance(query_index, bool)
            or not isinstance(query_index, int)
            or isinstance(result_rank, bool)
            or not isinstance(result_rank, int)
        ):
            rejected.append(
                {"selection": dict(selection), "reason": "invalid_source_coordinates"}
            )
            continue
        candidate = candidates_by_index.get((query_index, result_rank))
        url = candidate.get("url") if candidate is not None else None
        try:
            parsed_url = urlsplit(url) if isinstance(url, str) else None
        except ValueError:
            parsed_url = None
        host = (
            parsed_url.hostname.casefold().rstrip(".")
            if parsed_url is not None and parsed_url.hostname
            else ""
        )
        if candidate is None:
            rejected.append(
                {"selection": dict(selection), "reason": "source_not_in_search_results"}
            )
        elif (
            parsed_url is None
            or parsed_url.scheme != "https"
            or parsed_url.username is not None
            or parsed_url.password is not None
            or host not in _OFFICIAL_GITHUB_RESEARCH_HOSTS
        ):
            rejected.append(
                {
                    "selection": dict(selection),
                    "reason": "publisher_not_official_github",
                }
            )
        else:
            accepted.append(selection)

    requests = bind_external_contract_search_selections(search_results, accepted)
    return {"requests": requests, "rejected_selections": rejected}


def _skip(failure: CIFailureUnit, reason_code: str) -> CIResearchPlan:
    return CIResearchPlan(
        decision="skip",
        reason_code=reason_code,
        failure_id=failure.failure_id,
        head_sha=failure.head_sha,
    )


__all__ = [
    "CIResearchPlan",
    "CIResearchQuery",
    "bind_ci_research_source_selections",
    "build_ci_research_plan",
    "prepare_ci_research_search",
]
