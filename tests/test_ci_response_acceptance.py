from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from powdrr_contracts.ci import ProtectedArtifact, RepairContract
from powdrr_service.ci_response.acceptance import (
    CIRepairAcceptance,
    CIRepairValidation,
    assess_ci_repair_candidate,
)

HEAD = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"
PROTECTED = ProtectedArtifact("tests/test_app.py", "b" * 64)
COMMAND = ("pytest", "tests/test_app.py")
DIFF = "c" * 64


def _contract(*, expires_at: datetime | None = None) -> RepairContract:
    expiry = expires_at or datetime.now(UTC) + timedelta(hours=1)
    return RepairContract.create(
        repository="example/project",
        pull_request_number=270,
        head_sha=HEAD,
        observation_id="observation-1",
        failure_id="failure-1",
        reproduction_id="reproduction-1",
        reproduction_status="reproduced",
        allowed_paths=("src",),
        protected_artifacts=(PROTECTED,),
        required_behavior=("Preserve the documented retry behavior.",),
        verification_commands=(COMMAND,),
        required_checks=("CI / unit",),
        research_claim_refs=(),
        stop_conditions=("Stop if protected artifacts change.",),
        expires_at=expiry.isoformat(),
    )


def _assess(
    *,
    contract: RepairContract | None = None,
    base_sha: str = HEAD,
    worker_status: str = "completed",
    changed_paths: tuple[str, ...] = ("src/app.py",),
    protected_artifacts: tuple[ProtectedArtifact, ...] = (PROTECTED,),
    diff_fingerprint: str | None = DIFF,
    validations: tuple[CIRepairValidation, ...] | None = None,
    now: datetime | None = None,
) -> CIRepairAcceptance:
    return assess_ci_repair_candidate(
        contract or _contract(),
        base_sha=base_sha,
        worker_status=worker_status,
        changed_paths=changed_paths,
        protected_artifacts=protected_artifacts,
        diff_fingerprint=diff_fingerprint,
        validations=validations
        if validations is not None
        else (CIRepairValidation(COMMAND, "passed", DIFF),),
        now=now,
    )


def test_accepts_only_in_scope_candidate_with_matching_validation() -> None:
    decision = _assess()

    assert decision.status == "accepted"
    assert decision.reasons == ()
    assert decision.changed_paths == ("src/app.py",)
    assert decision.validated_commands == (COMMAND,)
    assert decision.to_dict() == {
        "schema_version": "ci-repair-acceptance-v1",
        "status": "accepted",
        "contract_id": decision.contract_id,
        "reasons": [],
        "changed_paths": ["src/app.py"],
        "validated_commands": [["pytest", "tests/test_app.py"]],
    }


@pytest.mark.parametrize("case", ["base", "worker", "scope", "path", "duplicate_path"])
def test_rejects_stale_unsuccessful_or_out_of_scope_candidates(
    case: Literal["base", "worker", "scope", "path", "duplicate_path"],
) -> None:
    if case == "base":
        decision = _assess(base_sha="a" * 40)
        expected_reason = "candidate base does not match"
    elif case == "worker":
        decision = _assess(worker_status="timed_out")
        expected_reason = "did not complete successfully"
    elif case == "scope":
        decision = _assess(changed_paths=("docs/app.py",))
        expected_reason = "outside the repair contract"
    elif case == "path":
        decision = _assess(changed_paths=("src/../tests/test_app.py",))
        expected_reason = "invalid repository path"
    else:
        decision = _assess(changed_paths=("src/app.py", "src/app.py"))
        expected_reason = "contain duplicates"

    assert decision.status == "rejected"
    assert any(expected_reason in reason for reason in decision.reasons)


def test_rejects_changed_protected_artifact_and_weak_validation() -> None:
    changed_protected = ProtectedArtifact("tests/test_app.py", "d" * 64)
    decision = _assess(
        protected_artifacts=(changed_protected,),
        validations=(CIRepairValidation(COMMAND, "failed", DIFF),),
    )

    assert decision.status == "rejected"
    assert "protected artifact hashes do not match the contract" in decision.reasons
    assert "a required repair validation did not pass" in decision.reasons


@pytest.mark.parametrize("case", ["missing", "fingerprint", "command"])
def test_rejects_missing_or_unbound_validation_evidence(
    case: Literal["missing", "fingerprint", "command"],
) -> None:
    if case == "missing":
        decision = _assess(validations=())
        reason = "required repair validations are missing"
    elif case == "fingerprint":
        decision = _assess(
            validations=(CIRepairValidation(COMMAND, "passed", "d" * 64),)
        )
        reason = "does not match the candidate diff"
    else:
        decision = _assess(
            validations=(CIRepairValidation(("echo", "ok"), "passed", DIFF),)
        )
        reason = "uncontracted command"

    assert decision.status == "rejected"
    assert any(reason in item for item in decision.reasons)


def test_rejects_expired_contract() -> None:
    now = datetime.now(UTC)
    contract = _contract(expires_at=now - timedelta(seconds=1))

    decision = _assess(contract=contract, now=now)

    assert decision.status == "rejected"
    assert "repair contract has expired" in decision.reasons


def test_requires_timezone_aware_acceptance_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _assess(now=datetime(2026, 1, 1))


def test_rejects_malformed_validation_fingerprint() -> None:
    with pytest.raises(ValueError, match="must be SHA-256"):
        CIRepairValidation(COMMAND, "passed", "not-a-digest")


def test_procedrr_operation_dispatches_typed_acceptance_policy(
    tmp_path: Path,
) -> None:
    from powdrr_lift.workrr.command_catalog import (
        FeatureCommandRuntime,
        feature_command_catalog,
    )

    name = "assess_ci_repair_candidate"
    catalog = feature_command_catalog()
    contract = _contract()
    runtime = FeatureCommandRuntime(
        config=SimpleNamespace(benchmark_mode=False, uncertainty_policy="clarify"),
        runner=object(),
        worktree=tmp_path,
        output_root=tmp_path,
        branch="feature/ci-repair",
        slug="ci-repair",
        state={},
        catalog=catalog,
    )
    wired_catalog = catalog.with_logic(
        {name: lambda parameters: runtime.dispatch(name, [], parameters)}
    )
    parameters = {
        "repair_contract": contract.to_dict(),
        "base_sha": HEAD,
        "worker_status": "completed",
        "changed_paths": ["src/app.py"],
        "protected_artifacts": [PROTECTED.to_dict()],
        "diff_fingerprint": DIFF,
        "validations": [
            {
                "command": list(COMMAND),
                "status": "passed",
                "diff_fingerprint": DIFF,
            }
        ],
    }

    decision = wired_catalog.dispatch(name, parameters)

    assert decision["status"] == "accepted"
    assert decision["contract_id"] == contract.contract_id
