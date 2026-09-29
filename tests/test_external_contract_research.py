from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import powdrr_lift.workrr.external_contract_research as research_module
from powdrr_lift.workrr.command_catalog import feature_command_catalog
from powdrr_lift.workrr.external_contract_research import (
    bind_external_contract_search_selections,
    capture_external_sources,
    redact_external_contract_search_event,
    search_external_contract_sources,
)
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
                "search_source_ref": "search:1:1",
                "search_title": "Official API specification",
                "search_query": 'deferSpec="20220824"',
                "profile": "graphql-defer-v1",
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
    assert record["search_source_ref"] == "search:1:1"
    assert record["search_query"] == 'deferSpec="20220824"'
    assert record["profile"] == "graphql-defer-v1"


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


def _query() -> dict[str, str]:
    return {
        "query": 'GraphQL deferSpec="20220824" defer label directive specification',
        "research_question": "Which arguments are defined for @defer?",
        "profile": "deferSpec=20220824",
        "why_applicable": "The requested operation implements @defer.",
    }


def test_tavily_search_results_are_bounded_and_only_return_candidate_fields() -> None:
    called: list[tuple[str, str]] = []

    def fake_search(query: str, key: str) -> dict[str, object]:
        called.append((query, key))
        return {
            "results": [
                {
                    "url": f"https://spec.example.org/{index}",
                    "title": f"Spec {index}",
                    "content": "The relevant versioned source.",
                    "extra": "not retained",
                }
                for index in range(8)
            ]
        }

    result = search_external_contract_sources(
        [_query()],
        decision="research",
        api_key="fake-secret",
        search=fake_search,
    )

    assert called == [(_query()["query"], "fake-secret")]
    assert result["candidate_count"] == 5
    assert result["searches"][0]["candidates"][0] == {
        "query_index": 1,
        "result_rank": 1,
        "url": "https://spec.example.org/0",
        "title": "Spec 0",
        "snippet": "The relevant versioned source.",
    }


def test_search_fails_open_without_credentials() -> None:
    result = search_external_contract_sources(
        [_query()],
        decision="research",
        api_key=None,
        search=lambda _query, _key: pytest.fail("provider must not be called"),
    )

    assert result["unavailable_count"] == 1
    assert "API key is not configured" in result["searches"][0]["reason"]


def test_skipped_research_never_calls_provider() -> None:
    result = search_external_contract_sources(
        [_query()],
        decision="skip",
        api_key="fake-secret",
        search=lambda _query, _key: pytest.fail("provider must not be called"),
    )

    assert result["searches"] == []


def test_tavily_http_request_uses_bearer_key_and_bounded_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int) -> bytes:
            return json.dumps(
                {
                    "results": [
                        {
                            "url": "https://spec.example.org/v1",
                            "title": "Versioned specification",
                            "content": "Official source.",
                        }
                    ]
                }
            ).encode()

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        observed["url"] = request.full_url  # type: ignore[attr-defined]
        observed["token"] = request.get_header("Authorization")  # type: ignore[attr-defined]
        observed["body"] = json.loads(request.data)  # type: ignore[attr-defined]
        observed["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(research_module.urllib.request, "urlopen", fake_urlopen)
    result = search_external_contract_sources(
        [_query()],
        decision="research",
        api_key="server-side-key",
    )

    assert observed["url"] == research_module.TAVILY_SEARCH_ENDPOINT
    assert observed["token"] == "Bearer server-side-key"
    assert observed["body"]["query"] == _query()["query"]
    assert observed["body"]["search_depth"] == "basic"
    assert observed["body"]["max_results"] == research_module.MAX_SEARCH_RESULTS
    assert observed["timeout"] == research_module.FETCH_TIMEOUT_SECONDS
    assert result["candidate_count"] == 1


def test_search_results_are_persistently_cached_for_identical_provider_inputs(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "external-search.sqlite3"
    calls: list[str] = []

    def fake_search(query: str, _key: str) -> dict[str, object]:
        calls.append(query)
        return {
            "results": [
                {
                    "url": "https://spec.example.org/v1",
                    "title": "Specification",
                    "content": "Cached discovery snippet.",
                }
            ]
        }

    first = search_external_contract_sources(
        [_query()],
        decision="research",
        api_key="key",
        cache_path=cache_path,
        search=fake_search,
    )
    second = search_external_contract_sources(
        [_query()],
        decision="research",
        api_key="key",
        cache_path=cache_path,
        search=fake_search,
    )

    assert calls == [_query()["query"]]
    assert first["searches"][0]["status"] == "searched"
    assert second["searches"][0]["status"] == "cached"
    assert second["searches"][0]["candidates"] == first["searches"][0]["candidates"]
    assert cache_path.is_file()


def test_search_event_log_redacts_raw_results_and_selection_context() -> None:
    state = {"pending": False}
    search_event = redact_external_contract_search_event(
        "operation",
        {
            "tool": "internal",
            "inputs": {
                "command": ["search_external_contract_sources"],
                "decision": "research",
                "queries": [_query()],
            },
            "output": {
                "provider": "tavily",
                "searches": [
                    {
                        "status": "searched",
                        "candidates": [
                            {
                                "url": "https://spec.example.org/v1",
                                "snippet": "Sensitive provider excerpt",
                            }
                        ],
                    }
                ],
                "candidate_count": 1,
                "unavailable_count": 0,
            },
        },
        state,
    )
    judge_event = redact_external_contract_search_event(
        "judge",
        {
            "messages": [{"content": "Sensitive provider excerpt"}],
            "value": {
                "rationale": "Sensitive provider excerpt is authoritative.",
                "selections": [{"query_index": 1, "result_rank": 1}],
            },
        },
        state,
    )
    binder_event = redact_external_contract_search_event(
        "operation",
        {
            "inputs": {
                "command": ["bind_external_contract_search_selections"],
                "search_results": {"snippet": "Sensitive provider excerpt"},
                "selections": [{"query_index": 1, "result_rank": 1}],
            },
            "output": {"requests": [{"url": "https://spec.example.org/v1"}]},
        },
        state,
    )

    persisted = json.dumps([search_event, judge_event, binder_event])
    assert "Sensitive provider excerpt" not in persisted
    assert "https://spec.example.org/v1" not in persisted
    assert search_event["output"] == {
        "provider": "tavily",
        "candidate_count": 1,
        "unavailable_count": 0,
        "statuses": ["searched"],
    }
    assert "messages" not in judge_event
    assert binder_event["output"] == {"request_count": 1}


def test_selection_binds_only_real_result_refs_and_preserves_query_scope() -> None:
    search_results = search_external_contract_sources(
        [_query()],
        decision="research",
        api_key="fake-secret",
        search=lambda _query, _key: {
            "results": [
                {
                    "url": "https://spec.example.org/defer/v1",
                    "title": "Defer v1 reference",
                    "content": "Versioned directive reference.",
                }
            ]
        },
    )

    requests = bind_external_contract_search_selections(
        search_results,
        [
            {
                "query_index": 1,
                "result_rank": 1,
                "why_applicable": "This source matches the named version.",
            },
            {
                "query_index": 1,
                "result_rank": 99,
                "why_applicable": "Invented reference must be ignored.",
            },
        ],
    )

    assert requests == [
        {
            "url": "https://spec.example.org/defer/v1",
            "research_question": _query()["research_question"],
            "why_applicable": "This source matches the named version.",
            "search_source_ref": "search:1:1",
            "search_title": "Defer v1 reference",
            "search_query": str(_query()["query"]),
            "profile": str(_query()["profile"]),
        }
    ]
