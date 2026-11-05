from __future__ import annotations

import hashlib

import pytest
from powdrr_contracts.ci import (
    CIAnnotation,
    CIJobObservation,
    ContractError,
    LocalReproduction,
)
from powdrr_service.ci_response import (
    build_failure_units,
    compare_local_reproduction,
    normalize_github_snapshot,
    render_ci_observation_checkpoint,
)

HEAD = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"


def test_runner_rejection_without_steps_is_not_locally_executable() -> None:
    message = "The configuration 'macos-14-arm64-us-default' is not supported"
    annotation = CIAnnotation(
        check_run_id=127527360223,
        path=None,
        start_line=None,
        end_line=None,
        level="failure",
        message=message,
        raw_sha256=hashlib.sha256(message.encode()).hexdigest(),
    )
    job = CIJobObservation(
        job_id=127527360223,
        run_id=42107708170,
        run_attempt=2,
        head_sha=HEAD,
        status="completed",
        conclusion="failure",
        runner_id=0,
        steps=(),
        annotations=(annotation,),
    )

    (failure,) = build_failure_units((job,))
    result = compare_local_reproduction(
        ci_signature=failure.signature,
        local_output=None,
        command=(),
        exit_code=None,
        phase_exercised=False,
        environment_comparable=False,
        locally_executable=failure.locally_executable,
    )

    assert failure.phase == "runner_provisioning"
    assert failure.locally_executable is False
    assert result.status == "not_executable_locally"
    assert "before a local command" in result.reason


def test_different_earlier_local_setup_error_is_not_a_non_reproduction() -> None:
    result = compare_local_reproduction(
        ci_signature="test_failure: expected 2, got 3",
        local_output="ModuleNotFoundError: package unavailable",
        command=("pytest", "tests/test_behavior.py"),
        exit_code=1,
        phase_exercised=False,
        environment_comparable=True,
    )

    assert result.status == "not_comparable"
    assert result.matching_failure is None


def test_matching_failure_signature_is_reproduced() -> None:
    result = compare_local_reproduction(
        ci_signature="AssertionError: expected 2, got 3",
        local_output="E AssertionError: expected 2, got 3",
        command=("pytest", "tests/test_behavior.py"),
        exit_code=1,
        phase_exercised=True,
        environment_comparable=True,
    )

    assert result.status == "reproduced"
    assert result.matching_failure is True


def test_similar_but_nonmatching_local_error_is_not_called_reproduced() -> None:
    result = compare_local_reproduction(
        ci_signature="AssertionError: expected 2, got 3",
        local_output="E AssertionError: expected 2, got 4",
        command=("pytest", "tests/test_behavior.py"),
        exit_code=1,
        phase_exercised=True,
        environment_comparable=True,
    )

    assert result.status == "not_reproduced"
    assert result.matching_failure is False


def test_signature_normalization_redacts_tokens_and_credentials() -> None:
    from powdrr_service.ci_response import normalize_failure_signature

    normalized = normalize_failure_signature(
        "GH TOKEN=ghp_0123456789abcdefghijklmnopqrstuv Password=hunter2"
    )

    assert "ghp_0123456789" not in normalized
    assert "hunter2" not in normalized
    assert "<redacted>" in normalized


def test_green_comparable_execution_is_a_non_reproduction() -> None:
    result = compare_local_reproduction(
        ci_signature="AssertionError: expected 2, got 3",
        local_output="1 passed in 0.1s",
        command=("pytest", "tests/test_behavior.py"),
        exit_code=0,
        phase_exercised=True,
        environment_comparable=True,
    )

    assert result.status == "not_reproduced"
    assert result.matching_failure is False


def test_reproduction_contract_rejects_ambiguous_reproduced_state() -> None:
    with pytest.raises(ContractError, match="matching_failure=true"):
        LocalReproduction("reproduced", "uncertain")


def test_non_failure_jobs_do_not_create_failure_units() -> None:
    job = CIJobObservation(
        job_id=3,
        run_id=4,
        run_attempt=1,
        head_sha=HEAD,
        status="completed",
        conclusion="cancelled",
    )

    assert build_failure_units((job,)) == ()


def test_normalized_checkpoint_is_small_repeatable_and_records_exact_head() -> None:
    raw_message = "The configuration 'macos-14-arm64-us-default' is not supported"
    snapshot = {
        "pull_request": {"head": {"sha": HEAD}},
        "runs": [{"id": 9, "run_attempt": 2}],
        "jobs_by_run": {
            "9": [
                {
                    "id": 10,
                    "check_run_url": "https://api.github.test/check-runs/11",
                    "status": "completed",
                    "conclusion": "failure",
                    "runner_id": 0,
                    "steps": [],
                }
            ]
        },
        "annotations_by_check_run": {"11": [{"message": raw_message}]},
    }

    jobs = normalize_github_snapshot(snapshot)
    failures = build_failure_units(jobs)
    first_id, first_body = render_ci_observation_checkpoint(
        repository="example/project",
        pull_request_number=270,
        issue_number=107,
        head_sha=HEAD,
        jobs=jobs,
        failures=failures,
    )
    second_id, second_body = render_ci_observation_checkpoint(
        repository="example/project",
        pull_request_number=270,
        issue_number=107,
        head_sha=HEAD,
        jobs=jobs,
        failures=failures,
    )

    assert first_id == second_id
    assert first_body == second_body
    assert len(first_body) < 1000
    assert HEAD in first_body
    assert "runner_provisioning" in first_body
    assert "raw_sha256" not in first_body
