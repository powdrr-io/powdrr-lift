"""Validation evidence, reviewer finding dispositions, and readiness."""

from __future__ import annotations

from dataclasses import dataclass, replace

from powdrr_lift.core.execution_state import (
    ExecutionFinding,
    ExecutionState,
    FindingStatus,
    ObligationStatus,
)


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    evidence_type: str
    input_fingerprint: str
    description: str


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    reasons: tuple[str, ...] = ()
    satisfied_requirements: tuple[str, ...] = ()


class ReadinessEvaluator:
    """Pure readiness gate; no model or filesystem state is consulted."""

    def evaluate(
        self,
        state: ExecutionState,
        *,
        required_evidence: tuple[EvidenceRequirement, ...] = (),
        required_artifact_types: tuple[str, ...] = (),
    ) -> ReadinessReport:
        reasons: list[str] = []
        satisfied: list[str] = []
        for requirement in required_evidence:
            match = next(
                (
                    evidence
                    for evidence in state.evidence
                    if evidence.evidence_type == requirement.evidence_type
                    and evidence.input_fingerprint == requirement.input_fingerprint
                    and evidence.successful
                    and evidence.fresh
                ),
                None,
            )
            if match is None:
                reasons.append(
                    f"missing fresh successful evidence: {requirement.description}"
                )
            else:
                satisfied.append(requirement.description)
        for obligation in state.obligations:
            if obligation.status is ObligationStatus.OPEN:
                reasons.append(f"open obligation: {obligation.description}")
        for finding in state.findings:
            if finding.blocking and finding.status in {
                FindingStatus.OPEN,
                FindingStatus.ACCEPTED,
            }:
                reasons.append(f"blocking finding: {finding.description}")
        for artifact_type in required_artifact_types:
            if not any(
                artifact.artifact_type == artifact_type and artifact.accepted
                for artifact in state.artifacts
            ):
                reasons.append(f"missing accepted artifact: {artifact_type}")
        return ReadinessReport(not reasons, tuple(reasons), tuple(satisfied))


@dataclass(frozen=True, slots=True)
class FindingDisposition:
    finding: ExecutionFinding
    previous_status: FindingStatus
    actor_id: str


def dispose_finding(
    finding: ExecutionFinding,
    *,
    status: FindingStatus,
    actor_id: str,
) -> FindingDisposition:
    if status in {FindingStatus.OPEN, FindingStatus.ACCEPTED}:
        raise ValueError("A finding disposition must be a terminal disposition.")
    return FindingDisposition(replace(finding, status=status), finding.status, actor_id)
