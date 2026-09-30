from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import powdrr_lift.workrr.external_contract_research as research_module
from powdrr_lift.workrr.command_catalog import feature_command_catalog
from powdrr_lift.workrr.external_contract_research import (
    bind_external_contract_assessments,
    bind_external_contract_claims,
    bind_external_contract_search_selections,
    capture_external_sources,
    extract_external_contract_evidence,
    finalize_external_contract_context,
    prepare_external_contract_projection_requests,
    project_external_contract_requirements,
    redact_external_contract_search_event,
    search_external_contract_sources,
)
from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from procedrr.parser import parse_and_validate
from procedrr_evaluator import Evaluator


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


def test_extract_external_evidence_is_bounded_visible_and_hash_verified(
    tmp_path: Path,
) -> None:
    body = (
        b"<html><script>ignore @defer label</script>"
        b"<p>The @defer directive accepts an optional label argument."
        b" The server includes it in the response.</p></html>"
    )
    captured = capture_external_sources(
        [
            {
                "url": "https://spec.example.org/defer",
                "profile": "GraphQL defer directive",
                "research_question": "What arguments does @defer support?",
                "why_applicable": "The instruction names @defer.",
            }
        ],
        artifact_root=tmp_path,
        rationale="The request names a directive.",
        fetch=lambda url: (200, url, body),
    )

    evidence = extract_external_contract_evidence(
        captured["sources"],
        feature_description="Add @defer support.",
        artifact_root=tmp_path,
    )

    assert evidence["excerpts"]
    assert "optional label argument" in evidence["excerpts"][0]["text"]
    assert "ignore @defer label" not in json.dumps(evidence["excerpts"])
    assert sum(len(item["text"]) for item in evidence["excerpts"]) <= 8_000
    claim = {
        "source_ref": "external:1",
        "source_quote": "The @defer directive accepts an optional label argument.",
        "candidate_requirement": "Support the optional label argument on @defer.",
        "affected_surface": "DSLFragment.defer",
    }
    bound = bind_external_contract_claims(evidence, [claim])["claims"][0]
    assert bound["claim_ref"] == "claim:1"
    assert bound["canonical_url"] == "https://spec.example.org/defer"
    assert bound["content_sha256"] == hashlib.sha256(body).hexdigest()

    reflowed_claim = {
        **claim,
        "source_quote": (
            "The @defer directive accepts an optional label argument.  "
            "The server\nincludes it in the response."
        ),
    }
    reflowed = bind_external_contract_claims(evidence, [reflowed_claim])["claims"][0]
    assert reflowed["source_quote"] == (
        "The @defer directive accepts an optional label argument."
        " The server includes it in the response."
    )

    rejected = bind_external_contract_claims(
        evidence, [{**claim, "source_quote": "made up"}]
    )
    assert rejected["claims"] == []
    assert rejected["rejected_claims"] == [
        {
            "claim_index": 1,
            "reason": "citation_not_in_captured_excerpt",
            "source_quote": "made up",
        }
    ]


def test_external_contract_assessments_preserve_ask_and_record_defaults() -> None:
    claim = {
        "claim_ref": "claim:1",
        "source_ref": "external:1",
        "canonical_url": "https://spec.example.org/defer",
        "profile": "directive profile v1",
        "source_quote": "@defer accepts label.",
    }
    ask_result = bind_external_contract_assessments(
        [claim],
        [
            {
                "item": claim,
                "result": {
                    "decision": "unresolved",
                    "requirement": None,
                    "rationale": "The source profile may be newer than requested.",
                    "profile_compatibility": "uncertain",
                    "assumption_basis": None,
                },
            }
        ],
        clarification_policy="ask",
    )
    assert ask_result["requirements"] == []
    assert ask_result["dispositions"][0]["unresolved"] is True

    defaulted = bind_external_contract_assessments(
        [claim],
        [
            {
                "decision": "unresolved",
                "requirement": "Support the optional label argument on @defer.",
                "rationale": "The argument contract is version-independent here.",
                "profile_compatibility": (
                    "argument contract applies; newer payload details excluded"
                ),
                "assumption_basis": "The captured official directive definition.",
            }
        ],
        clarification_policy="normative_defaults",
    )
    assert defaulted["requirements"][0]["disposition"] == "assumed"
    assert defaulted["requirements"][0]["source_quote"] == claim["source_quote"]
    assert (
        prepare_external_contract_projection_requests(
            defaulted["requirements"], [claim]
        )["requests"][0]["claim"]["claim_ref"]
        == "claim:1"
    )


def test_finalize_external_context_binds_source_quote_to_projected_obligation(
    tmp_path: Path,
) -> None:
    requirement = {
        "requirement_ref": "requirement:1",
        "claim_ref": "claim:1",
        "source_ref": "external:1",
        "canonical_url": "https://spec.example.org/defer",
        "source_quote": "@defer accepts label.",
        "profile": "directive v1",
        "requirement": "DSLFragment.defer accepts an optional label.",
        "disposition": "accepted",
        "rationale": "Required to construct the named directive.",
    }
    scenario = {
        "subject": "DSLFragment.defer label",
        "given": "A fragment in the GraphQL DSL.",
        "when": 'defer(label="details") is called.',
        "then": 'The fragment prints @defer(label: "details").',
        "dimensions": {
            "normal_result": "The directive includes the label.",
            "error_behavior": (
                "Invalid labels follow existing argument rendering errors."
            ),
            "continuation": "Other DSL operations remain usable.",
            "unsupported_behavior": "not_applicable",
            "cancellation_cleanup": "not_applicable",
            "compatibility": "Calling defer() without a label remains supported.",
            "negative_boundaries": "No label argument is emitted when omitted.",
        },
        "evidence": ["Source: @defer accepts label."],
        "validator": "A focused DSL rendering test.",
        "routing": "include",
    }
    context = finalize_external_contract_context(
        evidence={"path": str(tmp_path / "external-contract-evidence.json")},
        claims=[{"claim_ref": "claim:1", "source_ref": "external:1"}],
        assessment_result={"requirements": [requirement], "dispositions": []},
        projections=[
            {
                "description": requirement["requirement"],
                "acceptance_criterion": scenario["then"],
                "expected_test": "Verify defer label rendering and omission.",
                "behavior_scenario": scenario,
            }
        ],
        artifact_root=tmp_path,
    )
    assert context["projected_obligations"][0]["design"]["behavior_scenario"][
        "scenario_id"
    ]
    assert (
        context["projected_obligations"][0]["design"]["external_requirement_ref"]
        == "requirement:1"
    )


def test_project_external_contract_requirement_preserves_accepted_scope() -> None:
    request = {
        "requirement": {
            "requirement_ref": "requirement:1",
            "requirement": "A provided label must be included in the payload.",
            "source_quote": (
                "If provided, the GraphQL Server must add it to the payload."
            ),
            "profile": "GraphQL incremental delivery v0.1",
        },
        "claim": {"affected_surface": "@defer DSL rendering"},
    }

    projection = project_external_contract_requirements([request])["projections"][0]

    assert projection["acceptance_criterion"] == request["requirement"]["requirement"]
    assert (
        projection["behavior_scenario"]["then"] == request["requirement"]["requirement"]
    )
    assert projection["behavior_scenario"]["evidence"] == [
        request["requirement"]["source_quote"]
    ]
    assert projection["behavior_scenario"]["routing"] == "include"


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

    request_body = observed.get("body")
    assert isinstance(request_body, dict)
    assert observed["url"] == research_module.TAVILY_SEARCH_ENDPOINT
    assert observed["token"] == "Bearer server-side-key"
    assert request_body["query"] == _query()["query"]
    assert request_body["search_depth"] == "basic"
    assert request_body["max_results"] == research_module.MAX_SEARCH_RESULTS
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


def test_selection_rejects_benchmark_and_verification_material() -> None:
    search_results = {
        "searches": [
            {
                "query": "GraphQL defer directive profile",
                "research_question": "What arguments does @defer define?",
                "profile": "GraphQL incremental delivery",
                "candidates": [
                    {
                        "result_rank": 1,
                        "url": "https://docs.example.org/spec/incremental/v1",
                        "title": "Incremental delivery specification",
                    },
                    {
                        "result_rank": 2,
                        "url": "https://example.org/lab/harness-eval/task/example",
                        "title": "Incremental delivery requirements",
                    },
                    {
                        "result_rank": 3,
                        "url": "https://example.org/results/defer",
                        "title": "Benchmark task: incremental delivery",
                    },
                ],
            }
        ]
    }

    requests = bind_external_contract_search_selections(
        search_results,
        [
            {"query_index": 1, "result_rank": rank, "why_applicable": "Relevant."}
            for rank in (1, 2, 3)
        ],
    )

    assert [request["url"] for request in requests] == [
        "https://docs.example.org/spec/incremental/v1"
    ]


@pytest.mark.parametrize(
    ("decision", "source_available"),
    [("skip", False), ("research", False), ("research", True)],
)
def test_external_research_gate_and_evidence_control_model_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    source_available: bool,
) -> None:
    from powdrr_lift.workrr import jev_classifier

    skills_dir = Path("docs/procedrr/skill-definitions")
    document = parse_and_validate(
        (skills_dir / "resolve-external-contracts.yaml").read_text(),
        command_catalog=feature_command_catalog(),
    )
    # Exercise the real workflow through claim binding; later assessment and
    # projection do not decide whether these provider calls should happen.
    document["steps"] = document["steps"][
        : next(i for i, step in enumerate(document["steps"]) if "for_each" in step)
    ]
    jev_requests: list[dict[str, Any]] = []
    planning_outputs: list[set[str]] = []
    quote = "The @defer directive accepts an optional label argument."

    def classify(request: dict[str, Any], *_args: Any) -> dict[str, Any]:
        jev_requests.append(request)
        assert request["allowed_values"] == ["research", "skip"]
        assert (
            request["state"]["context"]["feature_description"] == "Add @defer support."
        )
        return {"choice": decision}

    monkeypatch.setattr(jev_classifier, "_call_jev", classify)

    class Planning:
        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            assert response_schema is not None
            required = set(response_schema["required"])
            planning_outputs.append(required)
            if required == {"rationale", "queries"}:
                return {
                    "rationale": "The directive needs its contract.",
                    "queries": [_query()],
                }
            if required == {"rationale", "selections"}:
                return {
                    "rationale": "Use the official specification.",
                    "selections": [
                        {
                            "query_index": 1,
                            "result_rank": 1,
                            "why_applicable": "Directive contract.",
                        }
                    ]
                    if decision == "research"
                    else [],
                }
            if required == {"claims"}:
                assert source_available
                return {
                    "claims": [
                        {
                            "source_quote": quote,
                            "candidate_requirement": "Support optional label.",
                            "affected_surface": "@defer",
                        }
                    ]
                }
            pytest.fail(f"Unexpected text-model contract: {required}")

    def fetch(url: str) -> tuple[int, str, bytes]:
        if not source_available:
            raise TimeoutError("source unavailable")
        return 200, url, quote.encode()

    def execute(_tool: str, parameters: Mapping[str, Any]) -> Any:
        name = parameters["command"][0]
        args = {key: value for key, value in parameters.items() if key != "command"}
        if name == "search_external_contract_sources":
            return search_external_contract_sources(
                **args,
                api_key="test-key",
                search=lambda *_: {
                    "results": [
                        {
                            "url": "https://spec.example.org/defer",
                            "title": "Official specification",
                            "content": quote,
                        }
                    ]
                },
            )
        if name == "bind_external_contract_search_selections":
            return {"requests": bind_external_contract_search_selections(**args)}
        if name == "capture_external_contract_sources":
            return capture_external_sources(**args, artifact_root=tmp_path, fetch=fetch)
        if name == "extract_external_contract_evidence":
            return extract_external_contract_evidence(**args, artifact_root=tmp_path)
        if name == "bind_external_contract_claims":
            return bind_external_contract_claims(**args)
        pytest.fail(f"Unexpected command: {name}")

    planning = Planning()
    evaluator = Evaluator(
        WorkrrProcedrrClient(planning, skills_dir=skills_dir),
        execute,
        judge_clients={
            "jev": WorkrrProcedrrClient(
                JevSemanticClassifierClient(planning, api_key="test-key"),
                skills_dir=skills_dir,
            ),
        },
        command_catalog=feature_command_catalog(),
    )
    result = evaluator.evaluate(
        document,
        {
            "feature_description": "Add @defer support.",
            "feature_design": {},
            "clarification_policy": "ask",
        },
    )
    assert len(jev_requests) == 1
    assert ({"rationale", "queries"} in planning_outputs) == (decision == "research")
    assert ({"claims"} in planning_outputs) == source_available
    claims = result.bindings["bound_external_contract_claims"]["claims"]
    assert bool(claims) == source_available
    if source_available:
        assert claims[0]["source_quote"] == quote
    evidence = json.loads((tmp_path / "external-contract-evidence.json").read_text())
    assert evidence["has_excerpts"] == source_available
    assert evidence["has_excerpts"] == bool(evidence["excerpts"])
