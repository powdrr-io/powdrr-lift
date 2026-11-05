"""Bounded source selection for GitHub Actions-specific CI failures."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

MAX_CI_RESEARCH_QUERIES = 2
MAX_CI_RESEARCH_SOURCES = 3


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


__all__ = [
    "MAX_CI_RESEARCH_QUERIES",
    "MAX_CI_RESEARCH_SOURCES",
    "github_actions_research_queries",
    "select_github_actions_sources",
]
