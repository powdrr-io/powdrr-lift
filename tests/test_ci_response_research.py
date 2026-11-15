from __future__ import annotations

import pytest
from powdrr_contracts.ci import CIJobObservation
from powdrr_service.ci_response import CIFailureUnit, build_ci_research_plan

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
