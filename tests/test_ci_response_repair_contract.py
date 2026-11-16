from __future__ import annotations

from dataclasses import replace

import pytest
from powdrr_contracts.ci import (
    CIConclusion,
    CIFailureUnit,
    CIJobObservation,
    CIObservation,
    CIRequiredCheck,
    ContractError,
    LocalReproduction,
    ProtectedArtifact,
    RepairContract,
)
from powdrr_service.ci_response import compile_repair_contract

HEAD = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"


def _case(
    *,
    conclusion: CIConclusion = "failure",
    phase: str = "test_failure",
) -> tuple[CIObservation, CIFailureUnit]:
    job = CIJobObservation(
        job_id=11,
        run_id=10,
        run_attempt=1,
        head_sha=HEAD,
        status="completed",
        conclusion=conclusion,
        workflow_path=".github/workflows/ci.yml",
        check_context="CI / unit",
    )
    observation = CIObservation(
        repository="example/project",
        pull_request_number=270,
        head_sha=HEAD,
        jobs=(job,),
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
        phase=phase,
        signature="FAILED tests/test_app.py::test_example",
        evidence=("job_log:11:sha256:" + "a" * 64,),
        locally_executable=phase != "runner_provisioning",
        head_sha=HEAD,
        run_id=10,
        run_attempt=1,
        workflow_path=".github/workflows/ci.yml",
        test_node_id=(
            "tests/test_app.py::test_example" if phase == "test_failure" else None
        ),
    )
    return observation, failure


def _compile(
    observation: CIObservation,
    failure: CIFailureUnit,
    reproduction: LocalReproduction | None = None,
    *,
    allowed_paths: tuple[str, ...] = ("src/app.py",),
    research_assessment: dict[str, object] | None = None,
    research_claim_refs: tuple[str, ...] = (),
) -> RepairContract:
    return compile_repair_contract(
        observation,
        failure,
        reproduction
        or LocalReproduction(
            "reproduced", "matching failure reproduced", matching_failure=True
        ),
        allowed_paths=allowed_paths,
        protected_artifacts=(ProtectedArtifact("tests/test_app.py", "b" * 64),),
        required_behavior=("Preserve the documented retry behavior.",),
        verification_commands=(("pytest", "tests/test_app.py"),),
        required_checks=("CI / unit",),
        stop_conditions=("Stop if protected artifacts change.",),
        expires_at="2026-11-16T00:00:00Z",
        research_assessment=research_assessment,
        research_claim_refs=research_claim_refs,
    )


def test_repair_contract_is_deterministic_round_trippable_and_head_bound() -> None:
    observation, failure = _case()

    contract = _compile(observation, failure)
    restored = RepairContract.from_dict(contract.to_dict())

    assert restored == contract
    assert restored.contract_id.startswith("repair-")
    assert restored.content_sha256 == contract.content_sha256
    assert restored.head_sha == HEAD
    assert restored.protected_artifacts == (
        ProtectedArtifact("tests/test_app.py", "b" * 64),
    )
    changed = {**contract.to_dict(), "allowed_paths": ["src/other.py"]}
    with pytest.raises(ContractError, match="content_sha256"):
        RepairContract.from_dict(changed)


def test_repair_contract_rejects_inconclusive_reproduction_and_head_mismatch() -> None:
    observation, failure = _case()
    with pytest.raises(ValueError, match="reproduced or non-local"):
        _compile(
            observation,
            failure,
            LocalReproduction("not_comparable", "different operating system"),
        )

    stale_failure = replace(failure, head_sha="a" * 40)
    with pytest.raises(ValueError, match="different PR head"):
        _compile(observation, stale_failure)


def test_repair_contract_rejects_protected_paths_and_unknown_checks() -> None:
    observation, failure = _case()
    with pytest.raises(ContractError, match="overlap protected artifacts"):
        _compile(
            observation,
            failure,
            allowed_paths=("tests/test_app.py",),
        )
    with pytest.raises(ContractError, match="overlap protected artifacts"):
        _compile(observation, failure, allowed_paths=("tests",))


def test_repair_contract_cannot_drop_an_observed_required_check() -> None:
    observation, failure = _case()
    observation = replace(
        observation,
        required_checks=(
            *observation.required_checks,
            CIRequiredCheck(name="CI / integration", state="success", head_sha=HEAD),
        ),
    )
    with pytest.raises(ValueError, match="complete observed required policy"):
        _compile(observation, failure)
    with pytest.raises(ValueError, match="observed required policy"):
        compile_repair_contract(
            observation,
            failure,
            LocalReproduction("reproduced", "same failure", matching_failure=True),
            allowed_paths=("src/app.py",),
            protected_artifacts=(ProtectedArtifact("tests/test_app.py", "b" * 64),),
            required_behavior=("Preserve retry behavior.",),
            verification_commands=(("pytest",),),
            required_checks=("CI / unknown",),
            stop_conditions=("Stop on changed assertions.",),
            expires_at="2026-11-16T00:00:00Z",
        )


def test_non_local_runner_repair_requires_accepted_bound_research() -> None:
    observation, failure = _case(conclusion="failure", phase="runner_provisioning")
    reproduction = LocalReproduction(
        "not_executable_locally", "runner provisioning failed before execution"
    )
    assessment = {
        "status": "applicable_guidance",
        "repair_authorized": False,
        "claims": [
            {
                "claim_ref": "claim:1",
                "source_quote": "The runner is supported for public repositories.",
                "content_sha256": "c" * 64,
                "canonical_url": "https://docs.github.com/actions/runners",
                "source_ref": "external:1",
                "retrieved_at": "2026-11-15T00:00:00Z",
                "applicability_inputs": {
                    "runner_label": "macos-14",
                    "repository_visibility": "public",
                },
            }
        ],
        "requirements": [
            {
                "claim_ref": "claim:1",
                "disposition": "accept",
                "source_ref": "external:1",
                "canonical_url": "https://docs.github.com/actions/runners",
                "content_sha256": "c" * 64,
            },
        ],
    }

    contract = _compile(
        observation,
        failure,
        reproduction,
        research_assessment=assessment,
        research_claim_refs=("claim:1",),
    )
    assert contract.research_claim_refs == ("claim:1",)
    assert contract.reproduction_status == "not_executable_locally"

    assessment["requirements"] = [
        {"claim_ref": "claim:1", "disposition": "unresolved"},
    ]
    with pytest.raises(ValueError, match="accepted source-backed"):
        _compile(
            observation,
            failure,
            reproduction,
            research_assessment=assessment,
            research_claim_refs=("claim:1",),
        )


def test_repair_contract_paths_are_exact_relative_paths() -> None:
    for path in (
        "../outside.py",
        "/tmp/file.py",
        "src/*.py",
        "src\\file.py",
        "C:/outside.py",
        "src/line\nbreak.py",
    ):
        with pytest.raises(ContractError, match="normalized exact"):
            ProtectedArtifact(path, "d" * 64)
