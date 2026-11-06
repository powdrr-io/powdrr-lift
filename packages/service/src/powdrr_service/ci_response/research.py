"""Bounded source selection for GitHub Actions-specific CI failures."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse

from powdrr_service.external_contract_research import (
    capture_external_sources,
    extract_external_contract_evidence,
    search_external_contract_sources,
)

MAX_CI_RESEARCH_QUERIES = 2
MAX_CI_RESEARCH_SOURCES = 3
MAX_CI_RESEARCH_EXCERPTS = 3


def github_actions_research_queries(
    *, failure_signature: str, workflow_path: str | None, runner_labels: Sequence[str]
) -> tuple[dict[str, str], ...]:
    """Build at most two narrow searches from observed failure evidence."""
    signature = " ".join(failure_signature.split())[:180]
    if not signature:
        return ()
    queries = [
        {
            "query": f"site:docs.github.com/actions {signature}",
            "research_question": (
                "Does GitHub Actions documentation define this observed failure "
                "or runner behavior?"
            ),
            "profile": "github-actions-official-docs",
            "why_applicable": (
                "The failure signature was observed in a GitHub Actions job."
            ),
        }
    ]
    labels = ", ".join(
        dict.fromkeys(label.strip() for label in runner_labels if label.strip())
    )
    path = workflow_path.strip() if workflow_path else ""
    context = " ".join(value for value in (labels, path) if value)
    if context:
        queries.append(
            {
                "query": f"site:docs.github.com/actions {context} runner workflow",
                "research_question": (
                    "What documented runner or workflow constraints apply to the "
                    "observed labels and workflow path?"
                ),
                "profile": "github-actions-runner-workflow",
                "why_applicable": (
                    "Runner labels or workflow path came from the failed job."
                ),
            }
        )
    return tuple(queries[:MAX_CI_RESEARCH_QUERIES])


def select_github_actions_sources(
    searches: Mapping[str, object], *, maximum: int = MAX_CI_RESEARCH_SOURCES
) -> tuple[dict[str, str], ...]:
    """Keep only bounded candidates hosted on official GitHub documentation."""
    if maximum < 0:
        raise ValueError("maximum must not be negative")
    if maximum == 0:
        return ()
    raw_searches = searches.get("searches")
    if not isinstance(raw_searches, list):
        return ()
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for search_index, record in enumerate(raw_searches[:MAX_CI_RESEARCH_QUERIES], 1):
        if not isinstance(record, Mapping):
            continue
        candidates = record.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            url = candidate.get("url")
            if not isinstance(url, str):
                continue
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "docs.github.com":
                continue
            canonical = f"https://docs.github.com{parsed.path}"
            if canonical in seen:
                continue
            seen.add(canonical)
            question = record.get("research_question")
            title = candidate.get("title")
            selected.append(
                {
                    "url": canonical,
                    "research_question": str(question or "")[:500],
                    "why_applicable": (
                        "Search candidate "
                        f"{search_index} titled "
                        f"{str(title or 'GitHub Actions documentation')[:160]} "
                        "matches the observed CI research question; applicability "
                        "must be verified against captured content."
                    ),
                    "search_source_ref": f"search:{search_index}",
                    "search_title": str(title or "")[:160],
                    "profile": "github-actions-official-docs",
                }
            )
            if len(selected) >= maximum:
                return tuple(selected)
    return tuple(selected)


def research_github_actions_failure(
    *,
    failure_signature: str,
    workflow_path: str | None,
    runner_labels: Sequence[str],
    artifact_root: Path,
    api_key: str | None,
    cache_path: Path | None = None,
    search: Callable[[str, str], Mapping[str, object]] | None = None,
    fetch: Callable[[str], tuple[int, str, bytes]] | None = None,
) -> dict[str, object]:
    """Perform bounded official-docs research for one observed CI failure."""
    from powdrr_service.external_contract_research import (
        MAX_EVIDENCE_EXCERPT_CHARS,
    )

    queries = github_actions_research_queries(
        failure_signature=failure_signature,
        workflow_path=workflow_path,
        runner_labels=runner_labels,
    )
    if not queries:
        return {"status": "not_applicable", "queries": [], "evidence": []}
    search_result = search_external_contract_sources(
        queries,
        decision="research",
        api_key=api_key,
        cache_path=cache_path,
        search=search,
    )
    selected = select_github_actions_sources(
        search_result, maximum=MAX_CI_RESEARCH_SOURCES
    )
    captured = capture_external_sources(
        selected,
        artifact_root=artifact_root,
        decision="research",
        rationale="Official GitHub Actions evidence for one observed failure.",
        fetch=fetch,
    )
    evidence = extract_external_contract_evidence(
        captured.get("sources", []),
        feature_description=failure_signature,
        artifact_root=artifact_root,
    )
    excerpts = evidence.get("excerpts")
    records = []
    for item in (
        excerpts[:MAX_CI_RESEARCH_EXCERPTS] if isinstance(excerpts, list) else ()
    ):
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        records.append(
            {
                "source_ref": str(item.get("source_ref", "")),
                "url": str(item.get("canonical_url", "")),
                "sha256": str(item.get("content_sha256", "")),
                "excerpt": text[:MAX_EVIDENCE_EXCERPT_CHARS],
            }
        )
    return {
        "status": "evidence_available" if records else "unavailable_or_inconclusive",
        "queries": [dict(item) for item in queries],
        "search_candidate_count": search_result.get("candidate_count", 0),
        "captured_source_count": captured.get("captured_count", 0),
        "evidence": records,
    }


__all__ = [
    "MAX_CI_RESEARCH_QUERIES",
    "MAX_CI_RESEARCH_SOURCES",
    "MAX_CI_RESEARCH_EXCERPTS",
    "github_actions_research_queries",
    "select_github_actions_sources",
    "research_github_actions_failure",
]
