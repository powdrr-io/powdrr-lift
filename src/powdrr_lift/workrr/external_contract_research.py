"""Bounded retrieval and immutable capture for Procedrr external research.

Procedrr decides whether and what to research. This module only retrieves the
explicit source URLs it is given and records the response as evidence.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_SOURCES = 8
MAX_RESPONSE_BYTES = 1_000_000
FETCH_TIMEOUT_SECONDS = 12
MAX_SEARCH_QUERIES = 4
MAX_SEARCH_RESULTS = 5
MAX_SEARCH_RESPONSE_BYTES = 512_000
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def search_external_contract_sources(
    queries: Sequence[Mapping[str, Any]],
    *,
    decision: str,
    api_key: str | None,
    storage_rights_confirmed: bool,
    search: Callable[[str, str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Search Brave for candidate sources without treating hits as evidence.

    Search responses are returned transiently to Procedrr for source selection.
    The configured Brave plan must permit retaining search results because the
    Procedrr event log records bounded operation inputs and outputs.
    """
    if len(queries) > MAX_SEARCH_QUERIES:
        raise ValueError(f"at most {MAX_SEARCH_QUERIES} external searches are allowed")
    if decision == "skip":
        return {
            "provider": "brave",
            "searches": [],
            "candidate_count": 0,
            "unavailable_count": 0,
        }
    if decision != "research":
        return _unavailable_searches(queries, "Invalid research decision")
    if api_key is None or not api_key.strip():
        return _unavailable_searches(queries, "Brave Search API key is not configured")
    if not storage_rights_confirmed:
        return _unavailable_searches(
            queries,
            "Brave search-result storage rights were not explicitly confirmed",
        )
    searcher = search or _brave_search
    api_key = api_key.strip()
    if any(character in api_key for character in "\r\n"):
        return _unavailable_searches(queries, "Brave Search API key is malformed")
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
        try:
            payload = searcher(query.strip(), api_key)
            web = payload.get("web")
            raw_results = web.get("results") if isinstance(web, Mapping) else None
            if not isinstance(raw_results, list):
                raise ValueError("Brave response did not contain web results")
            candidates: list[dict[str, Any]] = []
            for rank, hit in enumerate(raw_results[:MAX_SEARCH_RESULTS], start=1):
                if not isinstance(hit, Mapping):
                    continue
                url = hit.get("url")
                title = hit.get("title")
                description = hit.get("description")
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
        "provider": "brave",
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
        for result_rank, candidate in enumerate(raw_candidates, start=1):
            if isinstance(candidate, Mapping):
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
        requests.append(
            {
                "url": url,
                "research_question": question,
                "why_applicable": rationale.strip(),
                "search_source_ref": source_ref,
                "search_title": str(candidate.get("title", "")),
                "search_query": str(search_record.get("query", "")),
                "profile": str(search_record.get("profile", "")),
            }
        )
    return requests


def _unavailable_searches(
    queries: Sequence[Mapping[str, Any]], reason: str
) -> dict[str, Any]:
    return {
        "provider": "brave",
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


def _brave_search(query: str, api_key: str) -> Mapping[str, Any]:
    query_string = urllib.parse.urlencode(
        {
            "q": query,
            "count": MAX_SEARCH_RESULTS,
            "search_lang": "en",
            "safesearch": "strict",
        }
    )
    request = urllib.request.Request(
        f"{BRAVE_SEARCH_ENDPOINT}?{query_string}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        body = response.read(MAX_SEARCH_RESPONSE_BYTES + 1)
    if len(body) > MAX_SEARCH_RESPONSE_BYTES:
        raise ValueError("Brave search response exceeds the 512,000-byte limit")
    parsed = json.loads(body)
    if not isinstance(parsed, Mapping):
        raise ValueError("Brave returned a non-object response")
    return parsed


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
    "search_external_contract_sources",
]
