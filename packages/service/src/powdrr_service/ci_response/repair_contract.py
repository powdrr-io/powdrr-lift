"""Fail-closed compilation of immutable CI correction boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from powdrr_contracts.ci import (
    CIFailureUnit as ContractFailureUnit,
)
from powdrr_contracts.ci import (
    CIObservation,
    LocalReproduction,
    ProtectedArtifact,
    RegisteredCIFix,
    RepairContract,
)


def compile_repair_contract(
    observation: CIObservation,
    failure: ContractFailureUnit,
    reproduction: LocalReproduction,
    *,
    allowed_paths: Sequence[str],
    protected_artifacts: Sequence[ProtectedArtifact],
    required_behavior: Sequence[str],
    verification_commands: Sequence[Sequence[str]],
    required_checks: Sequence[str],
    stop_conditions: Sequence[str],
    expires_at: str,
    research_assessment: Mapping[str, object] | None = None,
    research_claim_refs: Sequence[str] = (),
    registered_correction: RegisteredCIFix | None = None,
) -> RepairContract:
    """Bind a narrow correction to one observed failure and eligible evidence.

    The caller supplies a reviewed exact-path scope and baseline artifact hashes.
    This function validates those limits; it does not infer permissions from model
    output or authorize a repair by itself.
    """
    if failure.head_sha != observation.head_sha:
        raise ValueError("repair contract failure belongs to a different PR head")
    matching_jobs = [
        job
        for job in observation.jobs
        if job.job_id == failure.job_id
        and job.run_id == failure.run_id
        and job.run_attempt == failure.run_attempt
        and job.head_sha == failure.head_sha
    ]
    if len(matching_jobs) != 1:
        raise ValueError("repair contract failure does not identify one observed job")
    job = matching_jobs[0]
    if job.status != "completed" or job.conclusion not in {
        "failure",
        "timed_out",
        "cancelled",
    }:
        raise ValueError("repair contract target job has no completed failure")
    if reproduction.status not in {"reproduced", "not_executable_locally"}:
        raise ValueError("repair contract requires a reproduced or non-local failure")
    if (reproduction.status == "reproduced") != failure.locally_executable:
        raise ValueError(
            "repair contract reproduction status contradicts failure scope"
        )
    if observation.required_checks_status != "available":
        raise ValueError("repair contract requires observed required-check policy")
    observed_checks = {item.name for item in observation.required_checks}
    checks = tuple(dict.fromkeys(required_checks))
    if not checks or set(checks) != observed_checks:
        raise ValueError(
            "repair contract must retain the complete observed required policy"
        )
    paths = tuple(sorted(set(allowed_paths)))
    protected = tuple(sorted(protected_artifacts, key=lambda item: item.path))
    behavior = tuple(required_behavior)
    commands = tuple(tuple(command) for command in verification_commands)
    stops = tuple(stop_conditions)
    refs = tuple(sorted(set(research_claim_refs)))
    if reproduction.status == "not_executable_locally" or refs:
        _validate_research_refs(research_assessment, refs)
    if reproduction.status == "not_executable_locally" and not refs:
        raise ValueError(
            "non-local CI repairs require applicable source-backed claim references"
        )
    reproduction_payload = reproduction.to_dict()
    reproduction_id = (
        "reproduction-"
        + hashlib.sha256(
            json.dumps(
                reproduction_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:20]
    )
    return RepairContract.create(
        repository=observation.repository,
        pull_request_number=observation.pull_request_number,
        head_sha=observation.head_sha,
        observation_id=failure.observation_id,
        failure_id=failure.failure_id,
        reproduction_id=reproduction_id,
        reproduction_status=reproduction.status,
        allowed_paths=paths,
        protected_artifacts=protected,
        required_behavior=behavior,
        verification_commands=commands,
        required_checks=tuple(sorted(checks)),
        research_claim_refs=refs,
        stop_conditions=stops,
        expires_at=expires_at,
        registered_correction=registered_correction,
    )


def _validate_research_refs(
    assessment: Mapping[str, object] | None, claim_refs: tuple[str, ...]
) -> None:
    if assessment is None or assessment.get("status") != "applicable_guidance":
        raise ValueError("repair contract requires applicable research guidance")
    if assessment.get("repair_authorized") is not False:
        raise ValueError("CI research assessment cannot authorize repair")
    claims = assessment.get("claims")
    requirements = assessment.get("requirements")
    if not isinstance(claims, list) or not isinstance(requirements, list):
        raise ValueError("repair contract research assessment is malformed")
    claims_by_ref = {
        str(claim.get("claim_ref")): claim
        for claim in claims
        if isinstance(claim, Mapping)
        and isinstance(claim.get("claim_ref"), str)
        and isinstance(claim.get("source_ref"), str)
        and isinstance(claim.get("canonical_url"), str)
        and isinstance(claim.get("source_quote"), str)
        and bool(str(claim.get("source_quote", "")).strip())
        and isinstance(claim.get("content_sha256"), str)
        and len(str(claim.get("content_sha256"))) == 64
        and all(
            character in "0123456789abcdef"
            for character in str(claim.get("content_sha256", ""))
        )
        and bool(str(claim.get("source_ref", "")).strip())
        and bool(str(claim.get("canonical_url", "")).strip())
        and isinstance(claim.get("retrieved_at"), str)
        and bool(str(claim.get("retrieved_at", "")).strip())
        and isinstance(claim.get("applicability_inputs"), Mapping)
    }
    accepted_requirements = {
        str(requirement.get("claim_ref")): requirement
        for requirement in requirements
        if isinstance(requirement, Mapping)
        and requirement.get("disposition") == "accept"
    }
    if len(claims_by_ref) != sum(
        isinstance(claim, Mapping) and isinstance(claim.get("claim_ref"), str)
        for claim in claims
    ):
        raise ValueError("repair contract research claims contain duplicate refs")
    if len(accepted_requirements) != sum(
        isinstance(requirement, Mapping) and requirement.get("disposition") == "accept"
        for requirement in requirements
    ):
        raise ValueError("repair contract research requirements contain duplicate refs")
    for claim_ref in claim_refs:
        claim = claims_by_ref.get(claim_ref)
        requirement = accepted_requirements.get(claim_ref)
        if claim is None or requirement is None:
            raise ValueError(
                "repair contract research references must be accepted "
                "source-backed claims"
            )
        for field in ("source_ref", "canonical_url", "content_sha256"):
            if requirement.get(field) != claim.get(field):
                raise ValueError("repair contract research lineage is inconsistent")


__all__ = ["compile_repair_contract"]
