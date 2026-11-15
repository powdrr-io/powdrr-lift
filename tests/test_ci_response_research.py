from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from powdrr_contracts.ci import CIJobObservation
from powdrr_service.ci_response import (
    CIFailureUnit,
    bind_ci_research_source_selections,
    build_ci_research_plan,
    prepare_ci_research_search,
)

HEAD = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"


def _failure(
    *,
    phase: str = "runner_provisioning",
    signature: str = "configuration 'macos-14-arm64-us-default' is not supported",
    job_id: int = 127527360223,
    head_sha: str = HEAD,
) -> CIFailureUnit:
    return CIFailureUnit(
        failure_id="ci-failure-1",
        job_id=job_id,
        run_id=42107708170,
        run_attempt=2,
        head_sha=head_sha,
        phase=phase,
        signature=signature,
        evidence=("annotation:private-repo-secret-detail",),
        locally_executable=False,
    )


def _job(
    *,
    job_id: int = 127527360223,
    head_sha: str = HEAD,
    runner_id: int | None = None,
    steps: tuple[dict[str, object], ...] = (),
) -> CIJobObservation:
    return CIJobObservation(
        job_id=job_id,
        run_id=42107708170,
        run_attempt=2,
        head_sha=head_sha,
        status="completed",
        conclusion="failure",
        runner_id=runner_id,
        steps=steps,
    )


@pytest.mark.parametrize("visibility", ["public", "private", "internal", "unknown"])
def test_runner_rejection_produces_bounded_public_query(visibility: str) -> None:
    plan = build_ci_research_plan(_failure(), _job(), repository_visibility=visibility)

    assert plan.decision == "research"
    assert plan.reason_code == "unsupported_hosted_runner_label"
    assert len(plan.queries) == 1
    query = plan.queries[0]
    assert (
        query.query
        == "GitHub Actions hosted runner label macos-14-arm64-us-default support"
    )
    assert "GitHub Actions" in query.research_question
    assert visibility in query.applicability["repository_visibility"]
    assert query.public_identifiers == ("GitHub Actions", "macos-14-arm64-us-default")
    assert "private-repo" not in query.query
    assert "secret-detail" not in query.research_question
    assert plan.to_dict()["limits"] == {"max_queries": 2}


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("macos-14-arm64-us-default", "macos-14-arm64-us-default"),
        ("ubuntu-24.04", "ubuntu-24.04"),
        ("windows-2022", "windows-2022"),
        ("ubuntu-24.04-invalid.extra", None),
    ],
)
def test_runner_label_parser_preserves_full_supported_label(
    label: str, expected: str | None
) -> None:
    plan = build_ci_research_plan(
        _failure(signature=f"configuration '{label}' is not supported"), _job()
    )

    if expected is None:
        assert plan.decision == "skip"
        assert plan.reason_code == "runner_label_not_safely_identified"
    else:
        assert plan.decision == "research"
        assert plan.queries[0].public_identifiers[1] == expected


def test_invalid_visibility_is_normalized_without_failing() -> None:
    plan = build_ci_research_plan(
        _failure(),
        _job(),
        repository_visibility=None,
    )

    assert plan.queries[0].applicability["repository_visibility"] == "unknown"


def test_search_request_is_rebuilt_from_validated_plan_fields() -> None:
    plan = build_ci_research_plan(_failure(), _job()).to_dict()
    plan["queries"][0]["query"] = "private/repository secret issue"

    request = prepare_ci_research_search(plan)

    assert request["decision"] == "research"
    assert request["queries"] == [
        {
            "query": (
                "GitHub Actions hosted runner label macos-14-arm64-us-default support"
            ),
            "research_question": plan["queries"][0]["research_question"],
            "profile": "github_actions_hosted_runner_support",
            "why_applicable": (
                "The observed pre-runner rejection named macos-14-arm64-us-default; "
                "check support for unknown repositories."
            ),
        }
    ]
    assert "private-repo" not in str(request)
    assert "secret-detail" not in str(request)
    assert "private/repository" not in str(request)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan.update(schema_version="other"),
        lambda plan: plan.update(decision="skip"),
        lambda plan: plan.update(head_sha="not-a-sha"),
        lambda plan: plan["queries"][0].update(
            public_identifiers=["GitHub Actions", "macos-14", "private/repo"]
        ),
        lambda plan: plan["queries"][0].update(
            research_question="Search for private-repo confidential logs"
        ),
    ],
)
def test_search_request_rejects_tampered_plan(mutate: object) -> None:
    plan = build_ci_research_plan(_failure(), _job()).to_dict()
    mutate(plan)  # type: ignore[operator]

    with pytest.raises(ValueError):
        prepare_ci_research_search(plan)


def test_source_selection_binds_only_official_github_publishers() -> None:
    search_results = {
        "searches": [
            {
                "query": "GitHub Actions hosted runner label macos-14 support",
                "research_question": "Does GitHub support the runner?",
                "profile": "github_actions_hosted_runner_support",
                "candidates": [
                    {
                        "result_rank": 1,
                        "url": "https://docs.github.com/actions/hosted-runners",
                        "title": "GitHub hosted runners",
                    },
                    {
                        "result_rank": 2,
                        "url": "https://docs.github.com.attacker.example/forged",
                        "title": "Forged official looking hostname",
                    },
                    {
                        "result_rank": 3,
                        "url": "https://example.org/runner-labels",
                        "title": "Third-party runner list",
                    },
                ],
            }
        ]
    }

    bound = bind_ci_research_source_selections(
        search_results,
        [
            {"query_index": 1, "result_rank": 1, "why_applicable": "Official docs."},
            {"query_index": 1, "result_rank": 2, "why_applicable": "Looks similar."},
            {"query_index": 1, "result_rank": 3, "why_applicable": "Search hit."},
        ],
    )

    assert [item["url"] for item in bound["requests"]] == [
        "https://docs.github.com/actions/hosted-runners"
    ]
    assert [item["reason"] for item in bound["rejected_selections"]] == [
        "publisher_not_official_github",
        "publisher_not_official_github",
    ]


def test_ci_research_procedure_captures_only_official_sources_and_excerpts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from powdrr_service.external_contract_research import (
        capture_external_sources as capture_sources,
    )
    from powdrr_service.external_contract_research import (
        search_external_contract_sources as search_sources,
    )
    from powdrr_service.procedrr import parse_and_validate
    from powdrr_service.procedrr_evaluator import Evaluator
    from powdrr_service.procedrr_evaluator.model_client import ProcedrrModelClient
    from powdrr_service.resources import bundled_process_directory

    from powdrr_lift.workrr import command_catalog as command_module
    from powdrr_lift.workrr.command_catalog import (
        FeatureCommandRuntime,
        feature_command_catalog,
    )

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    source_text = (
        "GitHub Actions macOS 14 hosted runners are available for public repositories. "
        "Runner labels determine the selected image."
    )

    def search_with_fixture(
        queries: object,
        *,
        decision: str,
        api_key: str | None,
        cache_path: Path | None = None,
    ) -> dict[str, Any]:
        return search_sources(
            queries,  # type: ignore[arg-type]
            decision=decision,
            api_key="test-key",
            cache_path=cache_path,
            search=lambda *_: {
                "results": [
                    {
                        "url": "https://docs.github.com/actions/hosted-runners",
                        "title": "GitHub hosted runners",
                        "content": source_text,
                    },
                    {
                        "url": "https://example.org/hosted-runners",
                        "title": "Runner label list",
                        "content": "Third-party runner information.",
                    },
                ]
            },
        )

    def capture_with_fixture(requests: object, **kwargs: Any) -> dict[str, Any]:
        return capture_sources(
            requests,  # type: ignore[arg-type]
            fetch=lambda url: (200, url, source_text.encode("utf-8")),
            retrieved_at="2026-11-15T00:00:00+00:00",
            **kwargs,
        )

    monkeypatch.setattr(
        command_module, "search_external_contract_sources", search_with_fixture
    )
    monkeypatch.setattr(
        command_module, "capture_external_sources", capture_with_fixture
    )
    process_dir = bundled_process_directory()
    command_catalog = feature_command_catalog()
    process = parse_and_validate(
        (process_dir / "research-ci-response-failure.yaml").read_text(),
        command_catalog=command_catalog,
    )
    runtime = FeatureCommandRuntime(
        config=SimpleNamespace(benchmark_mode=False, uncertainty_policy="clarify"),
        runner=object(),
        worktree=tmp_path,
        output_root=tmp_path / "artifacts",
        branch="feature/test",
        slug="ci-research",
        state={},
        catalog=command_catalog,
    )

    class Planning:
        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            assert response_schema is not None
            assert set(response_schema["required"]) == {"rationale", "selections"}
            return {
                "rationale": "Select the official GitHub documentation.",
                "selections": [
                    {
                        "query_index": 1,
                        "result_rank": 1,
                        "why_applicable": (
                            "This is the official hosted-runner reference."
                        ),
                    },
                    {
                        "query_index": 1,
                        "result_rank": 2,
                        "why_applicable": "This third-party list is not authoritative.",
                    },
                ],
            }

    evaluator = Evaluator(
        ProcedrrModelClient(Planning(), process_directory=process_dir),
        lambda _tool, parameters: runtime.dispatch(
            parameters["command"][0],
            parameters["command"],
            {key: value for key, value in parameters.items() if key != "command"},
        ),
        command_catalog=command_catalog,
    )
    plan = build_ci_research_plan(_failure(), _job(), repository_visibility="public")
    result = evaluator.evaluate(process, {"research_plan": plan.to_dict()})

    sources = result.bindings["ci_research_sources"]
    evidence = result.bindings["ci_research_evidence"]
    assert sources["captured_count"] == 1
    assert sources["unavailable_count"] == 0
    assert len(sources["sources"]) == 1
    assert sources["sources"][0]["canonical_url"].startswith("https://docs.github.com/")
    assert evidence["has_excerpts"] is True
    assert "macOS 14 hosted runners" in evidence["excerpts"][0]["text"]
    assert (
        evidence["excerpts"][0]["content_sha256"]
        == hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    )
    assert json.loads(Path(evidence["path"]).read_text())["has_excerpts"] is True


@pytest.mark.parametrize(
    ("failure", "job", "visibility", "reason"),
    [
        (
            _failure(signature="configuration is not supported"),
            _job(),
            "unknown",
            "runner_label_not_safely_identified",
        ),
        (
            _failure(phase="check_annotation"),
            _job(),
            "unknown",
            "failure_has_no_external_runner_question",
        ),
        (
            _failure(job_id=7),
            _job(),
            "unknown",
            "failure_job_or_head_mismatch",
        ),
        (
            _failure(),
            _job(head_sha="a" * 40),
            "unknown",
            "failure_job_or_head_mismatch",
        ),
        (
            _failure(),
            _job(runner_id=8),
            "unknown",
            "runner_was_assigned_or_steps_started",
        ),
        (
            _failure(),
            _job(steps=({"name": "checkout", "status": "completed"},)),
            "unknown",
            "runner_was_assigned_or_steps_started",
        ),
    ],
)
def test_unsafe_or_inapplicable_failure_skips_research(
    failure: CIFailureUnit,
    job: CIJobObservation,
    visibility: str,
    reason: str,
) -> None:
    plan = build_ci_research_plan(failure, job, repository_visibility=visibility)

    assert plan.decision == "skip"
    assert plan.reason_code == reason
    assert plan.queries == ()
