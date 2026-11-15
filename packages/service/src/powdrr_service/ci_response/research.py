"""Deterministic research selection for externally defined CI failures."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from powdrr_contracts.ci import CIJobObservation

from powdrr_service.ci_response.evidence import CIFailureUnit
from powdrr_service.external_contract_research import (
    bind_external_contract_assessments,
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


def finalize_ci_research_assessment(
    research_plan: Mapping[str, Any],
    captured_sources: Mapping[str, Any],
    evidence: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    rejected_claims: Sequence[Mapping[str, Any]],
    assessment_result: Mapping[str, Any],
    *,
    artifact_root: Path,
) -> dict[str, Any]:
    """Persist source-backed claim dispositions without authorizing repairs."""
    prepared = prepare_ci_research_search(research_plan)
    original_query = research_plan["queries"][0]
    query_applicability = original_query["applicability"]
    applicability_inputs = {
        "runner_label": query_applicability["runner_label"],
        "repository_visibility": query_applicability["repository_visibility"],
        "runner_assignment": query_applicability["runner_assignment"],
    }
    sources = captured_sources.get("sources")
    excerpts = evidence.get("excerpts")
    requirements = assessment_result.get("requirements")
    dispositions = assessment_result.get("dispositions")
    if (
        not isinstance(sources, list)
        or not isinstance(excerpts, list)
        or not isinstance(requirements, list)
        or not isinstance(dispositions, list)
        or len(sources) > 3
        or len(excerpts) > 6
        or len(requirements) > 6
        or len(dispositions) > 6
    ):
        raise ValueError("CI research assessment inputs are malformed")
    if evidence.get("has_excerpts") is not bool(excerpts):
        raise ValueError("CI research evidence excerpt status is inconsistent")
    source_by_ref = {
        str(source.get("source_ref")): source
        for source in sources
        if isinstance(source, Mapping)
        and source.get("status") == "captured"
        and isinstance(source.get("source_ref"), str)
        and isinstance(source.get("content_sha256"), str)
    }
    for excerpt in excerpts:
        if not isinstance(excerpt, Mapping):
            raise ValueError("CI research evidence excerpt is malformed")
        source = source_by_ref.get(str(excerpt.get("source_ref")))
        if (
            source is None
            or excerpt.get("content_sha256") != source.get("content_sha256")
            or excerpt.get("canonical_url") != source.get("canonical_url")
        ):
            raise ValueError("CI research excerpt does not match its captured source")
    excerpts_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for excerpt in excerpts:
        source_ref = excerpt.get("source_ref")
        if isinstance(source_ref, str):
            excerpts_by_source.setdefault(source_ref, []).append(excerpt)

    if len(claims) > 6 or len(rejected_claims) > 6:
        raise ValueError("CI research claims exceed the excerpt-bound limit")
    for claim in claims:
        claim_ref = claim.get("claim_ref")
        source = source_by_ref.get(str(claim.get("source_ref")))
        if (
            not isinstance(claim_ref, str)
            or source is None
            or claim.get("content_sha256") != source.get("content_sha256")
            or claim.get("canonical_url") != source.get("canonical_url")
        ):
            raise ValueError("CI research claim is not bound to a captured source")
        quote = claim.get("source_quote")
        quote_span = claim.get("quoted_char_span")
        quote_is_bound = False
        if (
            isinstance(quote, str)
            and isinstance(quote_span, list)
            and len(quote_span) == 2
            and all(isinstance(offset, int) for offset in quote_span)
        ):
            for excerpt in excerpts_by_source.get(str(claim.get("source_ref")), []):
                excerpt_span = excerpt.get("normalized_char_span")
                excerpt_text = excerpt.get("text")
                if (
                    not isinstance(excerpt_span, list)
                    or len(excerpt_span) != 2
                    or not all(isinstance(offset, int) for offset in excerpt_span)
                    or not isinstance(excerpt_text, str)
                ):
                    continue
                start = quote_span[0] - excerpt_span[0]
                end = quote_span[1] - excerpt_span[0]
                if (
                    0 <= start <= end <= len(excerpt_text)
                    and excerpt_text[start:end] == quote
                ):
                    quote_is_bound = True
                    break
        if not quote_is_bound:
            raise ValueError("CI research claim quote does not match its bound excerpt")
        if not isinstance(source.get("retrieved_at"), str):
            raise ValueError("CI research source has no retrieval time")
    enriched_claims: list[dict[str, Any]] = []
    for claim in claims:
        source = source_by_ref[str(claim["source_ref"])]
        enriched_claims.append(
            {
                **dict(claim),
                "retrieved_at": source.get("retrieved_at"),
                "source_revision": source.get("source_revision"),
                "applicability_inputs": applicability_inputs,
            }
        )
    enriched_claims_by_ref = {
        str(claim["claim_ref"]): claim for claim in enriched_claims
    }
    enriched_requirements: list[dict[str, Any]] = []
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            raise ValueError("CI research requirement is malformed")
        claim = enriched_claims_by_ref.get(str(requirement.get("claim_ref")))
        if (
            claim is None
            or requirement.get("source_ref") != claim.get("source_ref")
            or requirement.get("canonical_url") != claim.get("canonical_url")
            or requirement.get("content_sha256") != claim.get("content_sha256")
        ):
            raise ValueError("CI research requirement lost source claim lineage")
        enriched_requirements.append(
            {
                **dict(requirement),
                "retrieved_at": claim.get("retrieved_at"),
                "source_revision": claim.get("source_revision"),
                "applicability_inputs": applicability_inputs,
            }
        )

    unresolved_claims = [
        {
            **dict(disposition),
            "claim": dict(enriched_claims_by_ref[str(disposition.get("claim_ref"))]),
        }
        for disposition in dispositions
        if isinstance(disposition, Mapping)
        and disposition.get("unresolved") is True
        and str(disposition.get("claim_ref")) in enriched_claims_by_ref
    ]
    has_excerpts = evidence.get("has_excerpts") is True and bool(excerpts)
    if requirements:
        status = "applicable_guidance"
    elif unresolved_claims:
        status = "unresolved"
    elif claims:
        status = "no_applicable_claim"
    elif has_excerpts:
        status = "no_supported_claim"
    elif source_by_ref:
        status = "no_relevant_excerpt"
    else:
        status = "sources_unavailable"
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_root / "ci-research-assessment.json"
    document = {
        "schema_version": "ci-research-assessment-v1",
        "failure_id": research_plan["failure_id"],
        "head_sha": research_plan["head_sha"],
        "research_question": prepared["research_context"],
        "source_path": captured_sources.get("path"),
        "evidence_path": evidence.get("path"),
        "status": status,
        "claims": enriched_claims,
        "rejected_claims": [dict(claim) for claim in rejected_claims],
        "requirements": enriched_requirements,
        "dispositions": [dict(item) for item in dispositions],
        "unresolved_claims": unresolved_claims,
        "repair_authorized": False,
    }
    artifact_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"path": str(artifact_path), **document}


def bind_ci_research_assessments(
    claims: Sequence[Mapping[str, Any]],
    assessments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind CI claim decisions without applying benchmark defaults."""
    if len(claims) > 6 or len(assessments) > 6:
        raise ValueError("CI research assessments exceed the excerpt-bound limit")
    return bind_external_contract_assessments(claims, assessments, benchmark_mode=False)


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
    "bind_ci_research_assessments",
    "bind_ci_research_source_selections",
    "build_ci_research_plan",
    "finalize_ci_research_assessment",
    "prepare_ci_research_search",
]
