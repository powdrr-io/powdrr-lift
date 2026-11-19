from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import pytest
from powdrr_contracts.ci import (
    CIFailureUnit,
    CIJobObservation,
    CIObservation,
    CIRequiredCheck,
    LocalReproduction,
    ProtectedArtifact,
    RegisteredCIFix,
    RepairContract,
)
from powdrr_service.ci_response import prepare_ci_repair_contract

HEAD = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"


def _case() -> tuple[CIObservation, CIFailureUnit, LocalReproduction]:
    observation = CIObservation(
        repository="example/project",
        pull_request_number=270,
        head_sha=HEAD,
        jobs=(
            CIJobObservation(
                job_id=11,
                run_id=10,
                run_attempt=1,
                head_sha=HEAD,
                status="completed",
                conclusion="failure",
                workflow_path=".github/workflows/ci.yml",
                check_context="CI / unit",
            ),
        ),
        required_checks_status="available",
        required_check_source="branch protection",
        required_checks=(
            CIRequiredCheck(name="CI / unit", state="failure", head_sha=HEAD),
        ),
    )
    failure = CIFailureUnit(
        failure_id="failure-1",
        observation_id="observation-1",
        job_id=11,
        phase="test_failure",
        signature="FAILED tests/test_app.py::test_example",
        evidence=("job_log:11:sha256:" + "a" * 64,),
        locally_executable=True,
        head_sha=HEAD,
        run_id=10,
        run_attempt=1,
        workflow_path=".github/workflows/ci.yml",
        command=("pytest", "tests/test_app.py::test_example"),
        test_node_id="tests/test_app.py::test_example",
    )
    reproduction = LocalReproduction(
        status="reproduced",
        reason="same assertion failure reproduced",
        command=("pytest", "tests/test_app.py::test_example"),
        exit_code=1,
        matching_failure=True,
    )
    return observation, failure, reproduction


def test_prepare_repair_contract_intersects_evidence_and_reviewed_scope() -> None:
    observation, failure, reproduction = _case()

    contract = prepare_ci_repair_contract(
        observation=observation,
        failure=failure,
        reproduction=reproduction,
        changed_paths=("src/app.py", "docs/unrelated.md"),
        failure_log_excerpt="ImportError while loading src/app.py",
        reviewed_context={
            "allowed_paths": ["src"],
            "acceptance_criteria": ["Preserve the documented application behavior."],
            "verification_command": "pytest tests/test_app.py::test_example",
        },
        protected_artifacts=(ProtectedArtifact("tests/test_app.py", "b" * 64),),
        now=datetime(2026, 11, 16, tzinfo=UTC),
    )

    assert contract.allowed_paths == ("src/app.py",)
    assert contract.protected_artifacts == (
        ProtectedArtifact("tests/test_app.py", "b" * 64),
    )
    assert contract.required_checks == ("CI / unit",)
    assert contract.verification_commands == (
        ("pytest", "tests/test_app.py::test_example"),
    )


@pytest.mark.parametrize(
    ("changed_paths", "excerpt", "message"),
    (
        (("src/app.py", "src/other.py"), "src/app.py src/other.py", "exactly one"),
        (("src/app.py",), "failure without source reference", "exactly one"),
    ),
)
def test_prepare_repair_contract_rejects_ambiguous_source_evidence(
    changed_paths: tuple[str, ...], excerpt: str, message: str
) -> None:
    observation, failure, reproduction = _case()

    with pytest.raises(ValueError, match=message):
        prepare_ci_repair_contract(
            observation=observation,
            failure=failure,
            reproduction=reproduction,
            changed_paths=changed_paths,
            failure_log_excerpt=excerpt,
            reviewed_context={
                "allowed_paths": ["src"],
                "acceptance_criteria": ["Preserve behavior."],
                "verification_command": "pytest tests/test_app.py",
            },
            protected_artifacts=(ProtectedArtifact("tests/test_app.py", "b" * 64),),
        )


def test_prepare_repair_contract_compiles_exact_registered_formatter_fix() -> None:
    observation, _, _ = _case()
    path = "src/app.py"
    command = ("ruff", "format", "--check", path)
    excerpt = (
        "Run ruff format --check .\n"
        f"Would reformat: {path}\n"
        "1 file would be reformatted\n"
    )
    failure = CIFailureUnit(
        failure_id="formatter-1",
        observation_id="observation-1",
        job_id=11,
        phase="formatter_failure",
        signature=f"Would reformat: {path}",
        evidence=("job_log:11:sha256:" + "a" * 64,),
        locally_executable=True,
        head_sha=HEAD,
        run_id=10,
        run_attempt=1,
        command=command,
    )
    reproduction = LocalReproduction(
        status="reproduced",
        reason="The exact formatter check reports the same single target.",
        command=command,
        exit_code=1,
        matching_failure=True,
    )

    contract = prepare_ci_repair_contract(
        observation=observation,
        failure=failure,
        reproduction=reproduction,
        changed_paths=(path,),
        failure_log_excerpt=excerpt,
        reviewed_context={
            "allowed_paths": ["src"],
            "acceptance_criteria": ["Preserve application behavior."],
            "verification_command": "pytest -q",
        },
        protected_artifacts=(),
        now=datetime(2026, 11, 16, tzinfo=UTC),
    )

    assert contract.allowed_paths == (path,)
    assert contract.protected_artifacts == ()
    assert contract.registered_correction == RegisteredCIFix(
        correction_id="ruff-format-v1",
        path=path,
        check_command=command,
        diagnostic_sha256=sha256(excerpt.encode()).hexdigest(),
    )
    assert command in contract.verification_commands
    assert RepairContract.from_dict(contract.to_dict()) == contract
