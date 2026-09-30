"""Bounded retrieval and immutable capture for Procedrr external research.

Procedrr decides whether and what to research. This module only retrieves the
explicit source URLs it is given and records the response as evidence.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

MAX_SOURCES = 8
MAX_RESPONSE_BYTES = 1_000_000
FETCH_TIMEOUT_SECONDS = 12
MAX_SEARCH_QUERIES = 4
MAX_SEARCH_RESULTS = 5
MAX_SEARCH_RESPONSE_BYTES = 512_000
SEARCH_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
TAVILY_SEARCH_ENDPOINT = "https://api.tavily.com/search"
MAX_EVIDENCE_EXCERPTS_PER_SOURCE = 2
MAX_EVIDENCE_EXCERPT_CHARS = 1_200
MAX_EVIDENCE_TOTAL_CHARS = 8_000


class _VisibleHTMLText(HTMLParser):
    """Extract bounded visible text while ignoring executable/page chrome."""

    _BLOCK_TAGS = {
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
    _IGNORED_TAGS = {"script", "style", "svg", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        elif self._ignored_depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)


def extract_external_contract_evidence(
    sources: Sequence[Mapping[str, Any]],
    *,
    feature_description: str,
    artifact_root: Path,
) -> dict[str, Any]:
    """Verify captured bytes and select small, traceable visible-text excerpts.

    This operation does not interpret or accept requirements. It only gives a
    later Procedrr decision bounded source passages to assess.
    """
    excerpt_budget = MAX_EVIDENCE_TOTAL_CHARS
    excerpts: list[dict[str, Any]] = []
    source_results: list[dict[str, Any]] = []
    for source in sources[:MAX_SOURCES]:
        source_ref = source.get("source_ref")
        content_path = source.get("content_path")
        expected_hash = source.get("content_sha256")
        if (
            source.get("status") != "captured"
            or not isinstance(source_ref, str)
            or not isinstance(content_path, str)
            or not isinstance(expected_hash, str)
        ):
            continue
        path = Path(content_path)
        try:
            body = path.read_bytes()
        except OSError as error:
            source_results.append(
                {
                    "source_ref": source_ref,
                    "status": "unavailable",
                    "reason": str(error),
                }
            )
            continue
        actual_hash = hashlib.sha256(body).hexdigest()
        if actual_hash != expected_hash:
            source_results.append(
                {
                    "source_ref": source_ref,
                    "status": "integrity_error",
                    "reason": "captured content hash does not match its manifest",
                }
            )
            continue
        text = _visible_source_text(body)
        selected = _select_relevant_excerpts(
            text,
            queries=(
                str(source.get("research_question", "")),
                str(source.get("profile", "")),
                feature_description,
            ),
            count=MAX_EVIDENCE_EXCERPTS_PER_SOURCE,
            char_limit=MAX_EVIDENCE_EXCERPT_CHARS,
        )
        source_excerpts: list[dict[str, Any]] = []
        for start, end, excerpt in selected:
            if excerpt_budget <= 0:
                break
            excerpt = excerpt[:excerpt_budget]
            if not excerpt:
                continue
            item = {
                "source_ref": source_ref,
                "canonical_url": source.get("canonical_url", ""),
                "profile": source.get("profile", ""),
                "research_question": source.get("research_question", ""),
                "content_sha256": expected_hash,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "normalized_char_span": [start, min(end, start + len(excerpt))],
                "text": excerpt,
            }
            excerpts.append(item)
            source_excerpts.append(item)
            excerpt_budget -= len(excerpt)
        source_results.append(
            {
                "source_ref": source_ref,
                "status": "available" if source_excerpts else "no_relevant_excerpt",
                "excerpt_count": len(source_excerpts),
            }
        )

    document = {
        "schema_version": "external-contract-evidence-v1",
        "sources": source_results,
        "excerpts": excerpts,
        "limits": {
            "max_sources": MAX_SOURCES,
            "max_excerpts_per_source": MAX_EVIDENCE_EXCERPTS_PER_SOURCE,
            "max_excerpt_chars": MAX_EVIDENCE_EXCERPT_CHARS,
            "max_total_chars": MAX_EVIDENCE_TOTAL_CHARS,
        },
    }
    artifact_path = artifact_root / "external-contract-evidence.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"path": str(artifact_path), **document}


def _visible_source_text(body: bytes) -> str:
    decoded = body.decode("utf-8", errors="replace")
    parser = _VisibleHTMLText()
    try:
        parser.feed(decoded)
        raw_text = "\n".join(parser.parts)
    except Exception:
        raw_text = re.sub(r"<[^>]*>", " ", html.unescape(decoded))
    raw_text = html.unescape(raw_text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines()]
    return "\n".join(line for line in lines if line)


def _select_relevant_excerpts(
    text: str,
    *,
    queries: Sequence[str],
    count: int,
    char_limit: int,
) -> list[tuple[int, int, str]]:
    paragraphs = [
        (match.start(), match.end(), match.group().strip())
        for match in re.finditer(r"[^\n]+", text)
        if match.group().strip()
    ]
    query_tokens = {
        token.casefold()
        for query in queries
        for token in re.findall(r"[\w@.-]+", query)
        if len(token) > 2
    }
    stop_words = {
        "and",
        "are",
        "for",
        "from",
        "how",
        "into",
        "its",
        "that",
        "the",
        "their",
        "this",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
    query_tokens -= stop_words
    ranked: list[tuple[int, int]] = []
    for index, (_start, _end, paragraph) in enumerate(paragraphs):
        paragraph_tokens = {
            token.casefold() for token in re.findall(r"[\w@.-]+", paragraph)
        }
        overlap = len(query_tokens & paragraph_tokens)
        if overlap:
            ranked.append((overlap, index))
    ranked.sort(reverse=True)
    selected: list[tuple[int, int, str]] = []
    used: list[tuple[int, int]] = []
    for _score, index in ranked:
        left = max(0, index - 4)
        right = min(len(paragraphs), index + 4)
        while (
            left < index and paragraphs[right - 1][1] - paragraphs[left][0] > char_limit
        ):
            left += 1
        while (
            right > index + 1
            and paragraphs[right - 1][1] - paragraphs[left][0] > char_limit
        ):
            right -= 1
        start = paragraphs[left][0]
        end = paragraphs[right - 1][1]
        if any(start < used_end and used_start < end for used_start, used_end in used):
            continue
        excerpt = text[start:end][:char_limit]
        selected.append((start, start + len(excerpt), excerpt))
        used.append((start, end))
        if len(selected) >= count:
            break
    return sorted(selected)


def bind_external_contract_claims(
    evidence: Mapping[str, Any], claims: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Bind model-proposed claims to captured excerpts with exact text evidence.

    Models often reflow HTML-derived text when quoting it. Permit whitespace
    normalization only, then replace the submitted quote with the exact source
    substring so downstream artifacts retain a verbatim citation.
    """

    def normalized_with_offsets(value: str) -> tuple[str, list[int]]:
        normalized: list[str] = []
        offsets: list[int] = []
        pending_space: int | None = None
        for position, character in enumerate(value):
            if character.isspace():
                if normalized:
                    pending_space = position if pending_space is None else pending_space
                continue
            if pending_space is not None:
                normalized.append(" ")
                offsets.append(pending_space)
                pending_space = None
            normalized.append(character)
            offsets.append(position)
        return "".join(normalized), offsets

    excerpts = evidence.get("excerpts")
    if not isinstance(excerpts, list):
        raise ValueError("external contract evidence has no excerpt list")
    excerpt_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for excerpt in excerpts:
        if not isinstance(excerpt, Mapping):
            continue
        source_ref = excerpt.get("source_ref")
        text = excerpt.get("text")
        if isinstance(source_ref, str) and isinstance(text, str):
            excerpt_by_source.setdefault(source_ref, []).append(excerpt)
    bound: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, claim in enumerate(claims, start=1):
        quote = claim.get("source_quote")
        if not isinstance(quote, str) or not quote:
            rejected.append({"claim_index": index, "reason": "missing_source_citation"})
            continue
        normalized_quote, _ = normalized_with_offsets(quote)
        match: tuple[Mapping[str, Any], int, int] | None = None
        if normalized_quote:
            for source_excerpts in excerpt_by_source.values():
                for excerpt in source_excerpts:
                    excerpt_text = str(excerpt.get("text", ""))
                    normalized_excerpt, offsets = normalized_with_offsets(excerpt_text)
                    normalized_start = normalized_excerpt.find(normalized_quote)
                    if normalized_start >= 0:
                        normalized_end = normalized_start + len(normalized_quote)
                        match = (
                            excerpt,
                            offsets[normalized_start],
                            offsets[normalized_end - 1] + 1,
                        )
                        break
                if match is not None:
                    break
        if match is None:
            rejected.append(
                {
                    "claim_index": index,
                    "reason": "citation_not_in_captured_excerpt",
                    "source_quote": quote,
                }
            )
            continue
        matching_excerpt, quote_start, quote_end = match
        exact_quote = str(matching_excerpt.get("text", ""))[quote_start:quote_end]
        excerpt_span = matching_excerpt.get("normalized_char_span", [0, 0])
        base_offset = int(excerpt_span[0]) if isinstance(excerpt_span, list) else 0
        requirement = claim.get("candidate_requirement")
        surface = claim.get("affected_surface")
        if not isinstance(requirement, str) or not requirement.strip():
            rejected.append(
                {"claim_index": index, "reason": "missing_candidate_requirement"}
            )
            continue
        if not isinstance(surface, str) or not surface.strip():
            rejected.append(
                {"claim_index": index, "reason": "missing_affected_surface"}
            )
            continue
        bound.append(
            {
                **dict(claim),
                "source_quote": exact_quote,
                "claim_ref": f"claim:{index}",
                "source_ref": matching_excerpt.get("source_ref", ""),
                "canonical_url": matching_excerpt.get("canonical_url", ""),
                "profile": matching_excerpt.get("profile", ""),
                "content_sha256": matching_excerpt.get("content_sha256", ""),
                "normalized_text_sha256": matching_excerpt.get("text_sha256", ""),
                "source_excerpt_span": matching_excerpt.get("normalized_char_span", []),
                "quoted_char_span": [
                    base_offset + quote_start,
                    base_offset + quote_end,
                ],
            }
        )
    return {"claims": bound, "rejected_claims": rejected}


def bind_external_contract_assessments(
    claims: Sequence[Mapping[str, Any]],
    assessments: Sequence[Mapping[str, Any]],
    *,
    benchmark_mode: bool,
) -> dict[str, Any]:
    """Bind applicability decisions and retain accepted or unresolved claims."""
    if len(claims) != len(assessments):
        raise ValueError("external contract assessment count does not match claims")
    requirements: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    for claim, collected_assessment in zip(claims, assessments, strict=True):
        assessment = collected_assessment
        # Procedrr list collectors retain each loop item alongside its result.
        if isinstance(collected_assessment, Mapping) and isinstance(
            collected_assessment.get("result"), Mapping
        ):
            assessment = collected_assessment["result"]
        claim_ref = claim.get("claim_ref")
        decision = assessment.get("decision")
        rationale = assessment.get("rationale")
        if (
            not isinstance(claim_ref, str)
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            raise ValueError(
                "external contract assessment is missing provenance or rationale"
            )
        if decision not in {"accept", "reject", "unresolved"}:
            raise ValueError("external contract assessment has an invalid decision")
        disposition: dict[str, Any] = {
            "claim_ref": claim_ref,
            "decision": decision,
            "rationale": rationale.strip(),
            "profile_compatibility": str(assessment.get("profile_compatibility", "")),
        }
        if decision == "reject":
            dispositions.append(disposition)
            continue
        requirement_text = assessment.get("requirement")
        if not isinstance(requirement_text, str) or not requirement_text.strip():
            if decision == "unresolved" and not benchmark_mode:
                disposition["unresolved"] = True
                dispositions.append(disposition)
                continue
            raise ValueError("accepted external contract assessment has no requirement")
        disposition["requirement"] = requirement_text.strip()
        if decision == "unresolved":
            if not benchmark_mode:
                disposition["unresolved"] = True
                dispositions.append(disposition)
                continue
            if (
                not isinstance(assessment.get("assumption_basis"), str)
                or not str(assessment.get("assumption_basis", "")).strip()
            ):
                raise ValueError("normative default must record its evidence basis")
            disposition["decision"] = "assumed"
            disposition["assumption_basis"] = str(
                assessment["assumption_basis"]
            ).strip()
        source_refs = [claim.get("source_ref")]
        requirement = {
            "requirement_ref": f"requirement:{len(requirements) + 1}",
            "claim_ref": claim_ref,
            "source_ref": source_refs[0],
            "canonical_url": claim.get("canonical_url", ""),
            "source_quote": claim.get("source_quote", ""),
            "profile": claim.get("profile", ""),
            "content_sha256": claim.get("content_sha256", ""),
            "normalized_text_sha256": claim.get("normalized_text_sha256", ""),
            "quoted_char_span": claim.get("quoted_char_span", []),
            "requirement": str(disposition["requirement"]),
            "disposition": disposition["decision"],
            "rationale": rationale.strip(),
            "assumption_basis": disposition.get("assumption_basis"),
        }
        requirements.append(requirement)
        dispositions.append(disposition)
    return {"requirements": requirements, "dispositions": dispositions}


def prepare_external_contract_projection_requests(
    requirements: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Pair accepted requirements with their evidence for design projection."""
    claims_by_ref = {
        str(claim.get("claim_ref")): claim
        for claim in claims
        if isinstance(claim.get("claim_ref"), str)
    }
    requests: list[dict[str, Any]] = []
    for requirement in requirements:
        claim_ref = requirement.get("claim_ref")
        claim = claims_by_ref.get(str(claim_ref))
        if claim is None:
            raise ValueError("external contract requirement has no source claim")
        requests.append({"requirement": dict(requirement), "claim": dict(claim)})
    return {"requests": requests}


def project_external_contract_requirements(
    requests: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project each accepted requirement without adding unsupported behavior.

    The source-backed requirement remains the complete normative outcome. This
    deterministic adapter supplies the typed scenario shape needed by the
    feature-design pipeline and keeps the exact citation attached.
    """
    projections: list[dict[str, Any]] = []
    for index, request in enumerate(requests, start=1):
        requirement = request.get("requirement")
        claim = request.get("claim")
        if not isinstance(requirement, Mapping) or not isinstance(claim, Mapping):
            raise ValueError("external contract projection request is malformed")
        requirement_text = requirement.get("requirement")
        surface = claim.get("affected_surface")
        source_quote = requirement.get("source_quote")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (requirement_text, surface, source_quote)
        ):
            raise ValueError("external contract projection request is incomplete")
        requirement_text = str(requirement_text).strip()
        surface = str(surface).strip()
        source_quote = str(source_quote)
        scenario = {
            "schema_version": "behavior-scenario-v1",
            "scenario_id": f"external-contract-scenario-{index}",
            "subject": surface,
            "given": f"The applicable external contract for {surface}.",
            "when": f"The implementation exercises {surface}.",
            "then": requirement_text,
            "related_requirements": [],
            "dimensions": {
                "normal_result": requirement_text,
                "error_behavior": "not_applicable",
                "continuation": "not_applicable",
                "unsupported_behavior": "not_applicable",
                "cancellation_cleanup": "not_applicable",
                "compatibility": str(requirement.get("profile", "")),
                "negative_boundaries": "not_applicable",
            },
            "evidence": [source_quote],
            "validator": f"Test that {surface} satisfies the cited requirement.",
            "capability_matrix": [
                {
                    "capability": surface,
                    "behavior": "support",
                    "evidence": [source_quote],
                }
            ],
            "routing": "include",
        }
        projections.append(
            {
                "description": f"External contract for {surface}: {requirement_text}",
                "acceptance_criterion": requirement_text,
                "expected_test": (
                    f"Test that {surface} satisfies the cited requirement."
                ),
                "behavior_scenario": scenario,
            }
        )
    return {"projections": projections}


def finalize_external_contract_context(
    *,
    evidence: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    assessment_result: Mapping[str, Any],
    projections: Sequence[Mapping[str, Any]],
    artifact_root: Path,
) -> dict[str, Any]:
    """Freeze evidence, decisions, and prompt projections with provenance."""
    requirements = assessment_result.get("requirements")
    if not isinstance(requirements, list):
        raise ValueError("external contract assessments have no requirements")
    if len(requirements) != len(projections):
        raise ValueError(
            "external contract projection count does not match requirements"
        )
    projected: list[dict[str, Any]] = []
    for index, (requirement, projection) in enumerate(
        zip(requirements, projections, strict=True), start=1
    ):
        if not isinstance(requirement, Mapping):
            raise ValueError("external contract requirement is malformed")
        required_fields = ("description", "acceptance_criterion", "expected_test")
        if any(
            not isinstance(projection.get(field), str)
            or not str(projection.get(field, "")).strip()
            for field in required_fields
        ):
            raise ValueError(f"external contract projection {index} is incomplete")
        scenario = projection.get("behavior_scenario")
        if not isinstance(scenario, Mapping):
            raise ValueError(
                f"external contract projection {index} has no behavior scenario"
            )
        if scenario.get("routing") != "include":
            raise ValueError(
                "external contract projections must be actionable include scenarios"
            )
        scenario_document = dict(scenario)
        scenario_document.setdefault("schema_version", "behavior-scenario-v1")
        scenario_document.setdefault(
            "scenario_id", f"external-contract-scenario-{index}"
        )
        source_quote = requirement.get("source_quote")
        scenario_evidence = scenario_document.get("evidence")
        if not isinstance(source_quote, str) or not isinstance(scenario_evidence, list):
            raise ValueError(
                "external contract projection has no source-linked evidence"
            )
        if not any(source_quote in str(item) for item in scenario_evidence):
            raise ValueError(
                f"external contract projection {index} does not cite its "
                "accepted source quote"
            )
        from powdrr_lift.core.behavior_contract import compile_behavior_scenarios

        try:
            compile_behavior_scenarios([scenario_document])
        except ValueError as error:
            raise ValueError(
                f"external contract projection {index} scenario is invalid: {error}"
            ) from error
        requirement_ref = requirement.get("requirement_ref")
        if not isinstance(requirement_ref, str):
            raise ValueError("external contract requirement has no bound identity")
        projected.append(
            {
                "id": f"external-{index}",
                "description": str(projection["description"]).strip(),
                "design": {
                    "kind": "feature",
                    "description": str(projection["description"]).strip(),
                    "acceptance_criterion": str(
                        projection["acceptance_criterion"]
                    ).strip(),
                    "expected_test": str(projection["expected_test"]).strip(),
                    "behavior_scenario": scenario_document,
                    "external_requirement_ref": requirement_ref,
                },
                "external_requirement_ref": requirement_ref,
            }
        )
    document = {
        "schema_version": "external-contract-context-v1",
        "evidence_path": evidence.get("path"),
        "claims": [dict(claim) for claim in claims],
        "requirements": [
            dict(item) for item in requirements if isinstance(item, Mapping)
        ],
        "dispositions": [
            dict(item)
            for item in assessment_result.get("dispositions", [])
            if isinstance(item, Mapping)
        ],
        "projected_obligations": projected,
        "unresolved_claims": [
            {
                **dict(item),
                "claim": next(
                    (
                        dict(claim)
                        for claim in claims
                        if claim.get("claim_ref") == item.get("claim_ref")
                    ),
                    {},
                ),
            }
            for item in assessment_result.get("dispositions", [])
            if isinstance(item, Mapping) and item.get("unresolved") is True
        ],
    }
    artifact_path = artifact_root / "external-contract-context.json"
    artifact_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"path": str(artifact_path), **document}


def search_external_contract_sources(
    queries: Sequence[Mapping[str, Any]],
    *,
    decision: str,
    api_key: str | None,
    cache_path: Path | None = None,
    search: Callable[[str, str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Search Tavily for candidates, caching bounded results outside event logs."""
    if len(queries) > MAX_SEARCH_QUERIES:
        raise ValueError(f"at most {MAX_SEARCH_QUERIES} external searches are allowed")
    if decision == "skip":
        return {
            "provider": "tavily",
            "searches": [],
            "candidate_count": 0,
            "unavailable_count": 0,
        }
    if decision != "research":
        return _unavailable_searches(queries, "Invalid research decision")
    if api_key is None or not api_key.strip():
        return _unavailable_searches(queries, "Tavily Search API key is not configured")
    searcher = search or _tavily_search
    api_key = api_key.strip()
    if any(character in api_key for character in "\r\n"):
        return _unavailable_searches(queries, "Tavily Search API key is malformed")
    searches: list[dict[str, Any]] = []
    for index, request in enumerate(queries):
        query = request.get("query")
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 600
            or len(query.split()) > 75
        ):
            searches.append(
                _search_record(request, index, "unavailable", "Invalid search query")
            )
            continue
        query = query.strip()
        cache_key = _search_cache_key(query)
        cached_candidates = (
            _load_search_cache(cache_path, cache_key)
            if cache_path is not None
            else None
        )
        if cached_candidates is not None:
            cached_records = [
                {**candidate, "query_index": index + 1}
                for candidate in cached_candidates
            ]
            searches.append(
                {
                    **_search_context(request, index),
                    "status": "cached",
                    "candidates": cached_records,
                }
            )
            continue
        try:
            payload = searcher(query, api_key)
            raw_results = payload.get("results")
            if not isinstance(raw_results, list):
                raise ValueError("Tavily response did not contain results")
            candidates: list[dict[str, Any]] = []
            for rank, hit in enumerate(raw_results[:MAX_SEARCH_RESULTS], start=1):
                if not isinstance(hit, Mapping):
                    continue
                url = hit.get("url")
                title = hit.get("title")
                description = hit.get("content")
                if not isinstance(url, str) or not isinstance(title, str):
                    continue
                try:
                    _validate_url(url)
                except ValueError:
                    continue
                candidates.append(
                    {
                        "query_index": index + 1,
                        "result_rank": rank,
                        "url": url,
                        "title": title[:300],
                        "snippet": (
                            description[:1200] if isinstance(description, str) else ""
                        ),
                    }
                )
            if cache_path is not None:
                _store_search_cache(
                    cache_path,
                    cache_key,
                    [
                        {
                            key: value
                            for key, value in candidate.items()
                            if key != "query_index"
                        }
                        for candidate in candidates
                    ],
                )
            searches.append(
                {
                    **_search_context(request, index),
                    "status": "searched",
                    "candidates": candidates,
                }
            )
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as error:
            searches.append(
                _search_record(request, index, "unavailable", str(error)[:300])
            )
        except Exception:  # noqa: BLE001 - keep provider failures fail-open
            searches.append(
                _search_record(request, index, "unavailable", "Search provider failed")
            )
    return {
        "provider": "tavily",
        "searches": searches,
        "candidate_count": sum(
            len(item.get("candidates", []))
            for item in searches
            if isinstance(item.get("candidates"), list)
        ),
        "unavailable_count": sum(item["status"] == "unavailable" for item in searches),
    }


def bind_external_contract_search_selections(
    search_results: Mapping[str, Any],
    selections: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Bind model-selected hit IDs to exact URLs and questions from search output."""
    searches = search_results.get("searches")
    if not isinstance(searches, list):
        raise ValueError("search results have no searches list")
    candidates: dict[tuple[int, int], tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for query_index, search_record in enumerate(searches, start=1):
        if not isinstance(search_record, Mapping):
            continue
        raw_candidates = search_record.get("candidates")
        if not isinstance(raw_candidates, list):
            continue
        for fallback_rank, candidate in enumerate(raw_candidates, start=1):
            if isinstance(candidate, Mapping):
                result_rank = candidate.get("result_rank", fallback_rank)
                if isinstance(result_rank, int):
                    candidates[(query_index, result_rank)] = (search_record, candidate)
    if len(selections) > MAX_SOURCES:
        raise ValueError(f"at most {MAX_SOURCES} source candidates may be selected")
    requests: list[dict[str, str]] = []
    seen: set[str] = set()
    for selection in selections:
        selected_query_index = selection.get("query_index")
        selected_result_rank = selection.get("result_rank")
        if not isinstance(selected_query_index, int) or not isinstance(
            selected_result_rank, int
        ):
            continue
        candidate_key = (selected_query_index, selected_result_rank)
        if candidate_key not in candidates:
            continue
        source_ref = f"search:{selected_query_index}:{selected_result_rank}"
        if source_ref in seen:
            continue
        seen.add(source_ref)
        search_record, candidate = candidates[candidate_key]
        url = candidate.get("url")
        title = str(candidate.get("title", ""))
        question = search_record.get("research_question")
        rationale = selection.get("why_applicable")
        if (
            not isinstance(url, str)
            or not isinstance(question, str)
            or not isinstance(rationale, str)
            or not url.strip()
            or not question.strip()
            or not rationale.strip()
        ):
            continue
        if _is_benchmark_or_verification_material(url, title):
            continue
        requests.append(
            {
                "url": url,
                "research_question": question,
                "why_applicable": rationale.strip(),
                "search_source_ref": source_ref,
                "search_title": title,
                "search_query": str(search_record.get("query", "")),
                "profile": str(search_record.get("profile", "")),
            }
        )
    return requests


def _is_benchmark_or_verification_material(url: str, title: str) -> bool:
    """Reject discovery hits that are task/evaluation artifacts, not contracts."""
    parsed = urllib.parse.urlsplit(url)
    path_parts = [part.casefold() for part in parsed.path.split("/") if part]
    excluded_path_markers = {
        "benchmark",
        "benchmarks",
        "harness",
        "harness-eval",
        "eval",
        "evaluation",
        "dataset",
        "datasets",
        "solution",
        "solutions",
        "verifier",
        "verifiers",
        "golden",
        "expected-output",
    }
    if any(
        part in excluded_path_markers
        or any(part.startswith(f"{marker}-") for marker in excluded_path_markers)
        for part in path_parts
    ):
        return True
    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold())
    return bool(
        re.search(
            r"\b(?:benchmark|benchmark task|challenge prompt|golden solution|"
            r"expected patch|verifier tests?|evaluation harness)\b",
            normalized_title,
        )
    )


def _unavailable_searches(
    queries: Sequence[Mapping[str, Any]], reason: str
) -> dict[str, Any]:
    return {
        "provider": "tavily",
        "searches": [
            _search_record(request, index, "unavailable", reason)
            for index, request in enumerate(queries)
        ],
        "candidate_count": 0,
        "unavailable_count": len(queries),
    }


def _search_record(
    request: Mapping[str, Any], index: int, status: str, reason: str
) -> dict[str, Any]:
    return {**_search_context(request, index), "status": status, "reason": reason}


def _search_context(request: Mapping[str, Any], index: int) -> dict[str, Any]:
    return {
        "search_ref": f"search-query:{index + 1}",
        "query": str(request.get("query", ""))[:600],
        "research_question": str(request.get("research_question", ""))[:500],
        "profile": str(request.get("profile", ""))[:300],
        "why_applicable": str(request.get("why_applicable", ""))[:500],
    }


def _tavily_search(query: str, api_key: str) -> Mapping[str, Any]:
    body = json.dumps(
        {
            "query": query,
            "search_depth": "basic",
            "max_results": MAX_SEARCH_RESULTS,
            "chunks_per_source": 1,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_favicon": False,
            "safe_search": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TAVILY_SEARCH_ENDPOINT,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        body = response.read(MAX_SEARCH_RESPONSE_BYTES + 1)
    if len(body) > MAX_SEARCH_RESPONSE_BYTES:
        raise ValueError("Tavily search response exceeds the 512,000-byte limit")
    parsed = json.loads(body)
    if not isinstance(parsed, Mapping):
        raise ValueError("Tavily returned a non-object response")
    return parsed


def _search_cache_key(query: str) -> str:
    request = {
        "provider": "tavily",
        "query": query,
        "search_depth": "basic",
        "max_results": MAX_SEARCH_RESULTS,
        "chunks_per_source": 1,
        "topic": "general",
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "include_favicon": False,
        "safe_search": True,
    }
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_search_cache(cache_path: Path, cache_key: str) -> list[dict[str, Any]] | None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(cache_path, timeout=5) as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS search_results "
                "(cache_key TEXT PRIMARY KEY, cached_at REAL NOT NULL, "
                "candidates_json TEXT NOT NULL)"
            )
            row = database.execute(
                "SELECT cached_at, candidates_json "
                "FROM search_results WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None or time.time() - float(row[0]) > SEARCH_CACHE_MAX_AGE_SECONDS:
            return None
        candidates = json.loads(str(row[1]))
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, dict) for candidate in candidates
        ):
            return None
        return candidates
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None


def _store_search_cache(
    cache_path: Path, cache_key: str, candidates: list[dict[str, Any]]
) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(cache_path, timeout=5) as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS search_results "
                "(cache_key TEXT PRIMARY KEY, cached_at REAL NOT NULL, "
                "candidates_json TEXT NOT NULL)"
            )
            database.execute(
                "INSERT INTO search_results (cache_key, cached_at, candidates_json) "
                "VALUES (?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "cached_at = excluded.cached_at, "
                "candidates_json = excluded.candidates_json",
                (
                    cache_key,
                    time.time(),
                    json.dumps(candidates, sort_keys=True, separators=(",", ":")),
                ),
            )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        # Cache availability must never block research or prompt generation.
        return


def redact_external_contract_search_event(
    kind: str, data: Mapping[str, Any], state: dict[str, bool]
) -> dict[str, Any]:
    """Keep raw provider results in the dedicated cache, not event logs."""
    sanitized = dict(data)
    inputs = data.get("inputs")
    command = inputs.get("command") if isinstance(inputs, Mapping) else None
    parameters = inputs if isinstance(inputs, Mapping) else {}
    command_name = (
        command[0]
        if isinstance(command, Sequence) and command and isinstance(command[0], str)
        else None
    )
    if kind == "operation" and command_name == "search_external_contract_sources":
        state["pending"] = True
        queries = parameters.get("queries")
        output = data.get("output")
        sanitized["inputs"] = {
            "command": list(command) if isinstance(command, Sequence) else [],
            "decision": parameters.get("decision"),
            "query_count": len(queries) if isinstance(queries, list) else 0,
        }
        sanitized["output"] = _search_event_summary(output)
    elif state.get("pending") and kind == "judge":
        sanitized.pop("messages", None)
        value = data.get("value")
        selections = value.get("selections") if isinstance(value, Mapping) else None
        sanitized["value"] = {
            "rationale": "Replayed cached external-source selection.",
            "selections": [
                {
                    "query_index": item.get("query_index"),
                    "result_rank": item.get("result_rank"),
                    "why_applicable": "Selected from the cached search candidates.",
                }
                for item in selections
                if isinstance(item, Mapping)
            ]
            if isinstance(selections, list)
            else [],
        }
    elif (
        state.get("pending")
        and kind == "operation"
        and command_name == "bind_external_contract_search_selections"
    ):
        selections = parameters.get("selections")
        output = data.get("output")
        requests = output.get("requests") if isinstance(output, Mapping) else None
        sanitized["inputs"] = {
            "command": list(command) if isinstance(command, Sequence) else [],
            "selection_count": len(selections) if isinstance(selections, list) else 0,
        }
        sanitized["output"] = {
            "request_count": len(requests) if isinstance(requests, list) else 0
        }
        state["pending"] = False
    return sanitized


def _search_event_summary(output: Any) -> dict[str, Any]:
    searches = output.get("searches") if isinstance(output, Mapping) else None
    statuses = (
        [
            search.get("status")
            for search in searches
            if isinstance(search, Mapping) and isinstance(search.get("status"), str)
        ]
        if isinstance(searches, list)
        else []
    )
    return {
        "provider": "tavily",
        "candidate_count": output.get("candidate_count", 0)
        if isinstance(output, Mapping)
        else 0,
        "unavailable_count": output.get("unavailable_count", 0)
        if isinstance(output, Mapping)
        else 0,
        "statuses": statuses,
    }


def capture_external_sources(
    requests: Sequence[Mapping[str, Any]],
    *,
    artifact_root: Path,
    decision: str = "research",
    rationale: str = "",
    fetch: Callable[[str], tuple[int, str, bytes]] | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    """Fetch bounded HTTPS URLs and persist content plus deterministic metadata.

    A failed or over-budget fetch becomes a source status, not a failed
    Procedrr operation, so callers can carry the uncertainty into the prompt.
    """
    if len(requests) > MAX_SOURCES:
        raise ValueError(f"at most {MAX_SOURCES} external sources may be fetched")
    if decision not in {"research", "skip"}:
        decision = "skip"
        rationale = "Invalid research decision; source retrieval was safely skipped."
    if not rationale.strip():
        rationale = "No research rationale was supplied; source retrieval was skipped."
    if decision == "skip" and requests:
        rationale = (
            rationale.strip()
            + " Source candidates were ignored because the decision was skip."
        )
        requests = ()
    fetcher = fetch or _fetch_https
    timestamp = retrieved_at or datetime.now(UTC).isoformat()
    evidence_dir = artifact_root / "external-contract-sources"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    sources: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        raw_url = request.get("url")
        url = raw_url if isinstance(raw_url, str) else ""
        source: dict[str, Any] = {
            "source_ref": f"external:{index + 1}",
            "requested_url": url,
            "research_question": str(request.get("research_question", "")),
            "why_applicable": _required_text(request, "why_applicable"),
            "status": "unavailable",
            "retrieved_at": timestamp,
        }
        for field in ("search_source_ref", "search_title", "search_query", "profile"):
            value = request.get(field)
            if isinstance(value, str) and value.strip():
                source[field] = value.strip()[:500]
        try:
            _validate_url(url)
            status, final_url, body = fetcher(url)
            _validate_url(final_url)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds the 1,000,000-byte limit")
            source.update(
                {
                    "status": "captured" if 200 <= status < 300 else "unavailable",
                    "http_status": status,
                    "canonical_url": final_url,
                    "content_sha256": hashlib.sha256(body).hexdigest(),
                    "content_bytes": len(body),
                }
            )
            if 200 <= status < 300:
                content_path = evidence_dir / f"source-{index + 1}.bin"
                content_path.write_bytes(body)
                source["content_path"] = str(content_path)
            else:
                source["reason"] = f"HTTP status {status}"
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as error:
            source["reason"] = str(error)[:500]
        sources.append(source)

    document = {
        "schema_version": "external-contract-source-capture-v1",
        "research_decision": decision,
        "decision_rationale": rationale.strip(),
        "retrieved_at": timestamp,
        "limits": {
            "max_sources": MAX_SOURCES,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "timeout_seconds": FETCH_TIMEOUT_SECONDS,
        },
        "sources": sources,
    }
    artifact_path = artifact_root / "external-contract-sources.json"
    artifact_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "path": str(artifact_path),
        "sources": sources,
        "captured_count": sum(item["status"] == "captured" for item in sources),
        "unavailable_count": sum(item["status"] != "captured" for item in sources),
    }


def _required_text(request: Mapping[str, Any], field: str) -> str:
    value = request.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"each external source request must include {field}")
    return value.strip()


def _validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("external source URLs must be credential-free HTTPS URLs")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise ValueError("local hosts are not valid external evidence sources")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("external source URL resolves to a non-public address")


def _fetch_https(url: str) -> tuple[int, str, bytes]:
    _validate_resolved_host(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Powdrr-Procedrr-Research/1.0",
            "Accept": "text/*, application/json",
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        response = opener.open(request, timeout=FETCH_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        return error.code, error.geturl(), error.read(MAX_RESPONSE_BYTES + 1)
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds the 1,000,000-byte limit")
        return response.status, response.geturl(), body


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        _validate_url(newurl)
        _validate_resolved_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_resolved_host(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    assert parsed.hostname is not None
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return  # The subsequent request records DNS failure as unavailable.
    for item in addresses:
        address = ipaddress.ip_address(str(item[4][0]).split("%", 1)[0])
        if not address.is_global:
            raise ValueError("external source host resolves to a non-public address")


__all__ = [
    "bind_external_contract_search_selections",
    "capture_external_sources",
    "redact_external_contract_search_event",
    "search_external_contract_sources",
]
