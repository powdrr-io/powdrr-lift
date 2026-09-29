from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from powdrr_lift.workrr.command_catalog import feature_command_catalog
from powdrr_lift.workrr.external_contract_research import capture_external_sources
from procedrr.parser import parse_and_validate


def test_external_contract_research_procedure_is_valid() -> None:
    source = Path(
        "docs/procedrr/skill-definitions/resolve-external-contracts.yaml"
    ).read_text(encoding="utf-8")

    parse_and_validate(source, command_catalog=feature_command_catalog())


def test_capture_external_source_persists_bytes_and_content_hash(
    tmp_path: Path,
) -> None:
    body = b"versioned standard excerpt"
    result = capture_external_sources(
        [
            {
                "url": "https://spec.example.org/v1",
                "research_question": "Which version-specific arguments apply?",
                "why_applicable": "The request names profile v1.",
            }
        ],
        artifact_root=tmp_path,
        rationale="The request names a versioned external contract.",
        fetch=lambda url: (200, url, body),
        retrieved_at="2026-09-29T12:00:00+00:00",
    )

    assert result["captured_count"] == 1
    record = result["sources"][0]
    assert record["content_sha256"] == hashlib.sha256(body).hexdigest()
    assert Path(record["content_path"]).read_bytes() == body
    document = json.loads(Path(result["path"]).read_text())
    assert document["schema_version"] == "external-contract-source-capture-v1"
    assert document["sources"] == result["sources"]


def test_unavailable_source_is_recorded_without_aborting_capture(
    tmp_path: Path,
) -> None:
    result = capture_external_sources(
        [
            {
                "url": "https://docs.example.org/unavailable",
                "research_question": "What is the response shape?",
                "why_applicable": "It may define the requested API.",
            }
        ],
        artifact_root=tmp_path,
        rationale="The request names an external contract.",
        fetch=lambda _url: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    assert result["unavailable_count"] == 1
    assert result["sources"][0]["status"] == "unavailable"
    assert result["sources"][0]["reason"] == "timed out"


@pytest.mark.parametrize(
    "url",
    [
        "http://spec.example.org/v1",
        "https://user:pass@spec.example.org/v1",
        "https://127.0.0.1/private",
        "https://metadata.internal/latest",
    ],
)
def test_rejects_non_https_or_local_source_urls(tmp_path: Path, url: str) -> None:
    result = capture_external_sources(
        [
            {
                "url": url,
                "research_question": "test question",
                "why_applicable": "test",
            }
        ],
        artifact_root=tmp_path,
        rationale="The request names a standard.",
        fetch=lambda _url: pytest.fail("unsafe candidate must not be fetched"),
    )

    assert result["unavailable_count"] == 1
    assert result["sources"][0]["status"] == "unavailable"


def test_source_count_is_bounded(tmp_path: Path) -> None:
    requests = [
        {
            "url": f"https://spec.example.org/{index}",
            "research_question": "test question",
            "why_applicable": "test",
        }
        for index in range(9)
    ]
    with pytest.raises(ValueError, match="at most 8"):
        capture_external_sources(requests, artifact_root=tmp_path)


def test_invalid_source_candidate_is_an_unavailable_record(tmp_path: Path) -> None:
    result = capture_external_sources(
        [
            {
                "url": "file:///etc/passwd",
                "research_question": "test question",
                "why_applicable": "test",
            }
        ],
        artifact_root=tmp_path,
        rationale="A standard was named.",
        fetch=lambda _url: (200, _url, b"must not fetch"),
    )

    assert result["unavailable_count"] == 1
    assert result["sources"][0]["status"] == "unavailable"
    assert "HTTPS" in result["sources"][0]["reason"]
