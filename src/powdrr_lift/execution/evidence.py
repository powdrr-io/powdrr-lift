"""Validation evidence, reviewer finding dispositions, and readiness."""

from __future__ import annotations

from dataclasses import dataclass, replace

from powdrr_lift.core.execution_state import (
    ExecutionEvidence,
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


@dataclass(frozen=True, slots=True)
class PublishRequirements:
    """Kernel-owned requirements for crossing the publish boundary."""

    plan_fingerprint: str | None = None
    proposed_pr_fingerprint: str | None = None
    require_independent_review: bool = False
    author_id: str | None = None
    reviewer_findings: tuple[tuple[str, tuple[str, ...]], ...] = ()


class ReadinessEvaluator:
    """Pure readiness gate; no model or filesystem state is consulted."""

    def evaluate(
        self,
        state: ExecutionState,
        *,
        required_evidence: tuple[EvidenceRequirement, ...] = (),
        required_artifact_types: tuple[str, ...] = (),
        publish: PublishRequirements | None = None,
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
        if publish is not None:
            accepted_refs = {
                artifact.content_ref
                for artifact in state.artifacts
                if artifact.accepted
            }
            for label, fingerprint in (
                ("plan", publish.plan_fingerprint),
                ("proposed PR", publish.proposed_pr_fingerprint),
            ):
                if fingerprint is not None and fingerprint not in accepted_refs:
                    reasons.append(f"current accepted {label} fingerprint is missing")
            if publish.require_independent_review:
                if publish.author_id is not None and any(
                    reviewer_id == publish.author_id
                    for reviewer_id, _ in publish.reviewer_findings
                ):
                    reasons.append(
                        "review agreement: author cannot provide independent review"
                    )
                agreement = evaluate_review_agreement(publish.reviewer_findings)
                if not agreement.sufficient:
                    reasons.extend(
                        f"review agreement: {reason}"
                        for reason in agreement.disagreement_reasons
                    )
        return ReadinessReport(not reasons, tuple(reasons), tuple(satisfied))


@dataclass(frozen=True, slots=True)
class FindingDisposition:
    finding: ExecutionFinding
    previous_status: FindingStatus
    actor_id: str
    evidence_ids: tuple[str, ...] = ()


def dispose_finding(
    finding: ExecutionFinding,
    *,
    status: FindingStatus,
    actor_id: str,
    supporting_evidence: tuple[ExecutionEvidence, ...] = (),
) -> FindingDisposition:
    if status is FindingStatus.OPEN:
        raise ValueError("A finding disposition must be terminal or accepted.")
    if status in {
        FindingStatus.FIXED,
        FindingStatus.NOT_APPLICABLE,
        FindingStatus.ACCEPTED,
    } and not any(
        evidence.successful and evidence.fresh for evidence in supporting_evidence
    ):
        raise ValueError("This finding disposition requires fresh successful evidence.")
    return FindingDisposition(
        replace(finding, status=status),
        finding.status,
        actor_id,
        tuple(evidence.evidence_id for evidence in supporting_evidence),
    )


def invalidate_evidence(
    state: ExecutionState, changed_input_fingerprints: frozenset[str]
) -> ExecutionState:
    """Invalidate only evidence whose exact input fingerprint changed."""

    return replace(
        state,
        evidence=tuple(
            replace(evidence, fresh=False)
            if evidence.input_fingerprint in changed_input_fingerprints
            else evidence
            for evidence in state.evidence
        ),
    )


@dataclass(frozen=True, slots=True)
class ReviewAgreement:
    independent_reviewers: int
    agreeing_reviewers: int
    disagreement_reasons: tuple[str, ...] = ()

    @property
    def sufficient(self) -> bool:
        return self.independent_reviewers > 0 and not self.disagreement_reasons


def evaluate_review_agreement(
    reviewer_findings: tuple[tuple[str, tuple[str, ...]], ...],
) -> ReviewAgreement:
    """Compare normalized finding signatures from independent reviewers."""

    if not reviewer_findings:
        return ReviewAgreement(0, 0, ("no independent reviewer findings",))
    signatures = {tuple(sorted(findings)) for _, findings in reviewer_findings}
    reasons = () if len(signatures) == 1 else ("independent reviewers disagree",)
    return ReviewAgreement(
        len(reviewer_findings),
        len(reviewer_findings) if not reasons else 0,
        reasons,
    )
