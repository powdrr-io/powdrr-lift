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


__all__ = ["capture_external_sources"]
