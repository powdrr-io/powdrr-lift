from dataclasses import replace

import pytest

from powdrr_lift.core.execution_state import (
    ExecutionArtifact,
    ExecutionEvidence,
    ExecutionFinding,
    FindingStatus,
    initial_execution_state,
)
from powdrr_lift.execution.evidence import (
    EvidenceRequirement,
    PublishRequirements,
    ReadinessEvaluator,
    dispose_finding,
    evaluate_review_agreement,
    invalidate_evidence,
)


def test_readiness_requires_fresh_evidence_and_closed_findings() -> None:
    state = initial_execution_state("run-1", profile_id="profile")
    requirement = EvidenceRequirement("pytest", "input-1", "test suite")
    blocked = ReadinessEvaluator().evaluate(
        state,
        required_evidence=(requirement,),
        required_artifact_types=("implementation",),
    )
    assert not blocked.ready
    state = replace(
        state,
        evidence=(ExecutionEvidence("e-1", "a-1", "pytest", "input-1", True),),
        artifacts=(),
    )
    state = replace(
        state, findings=(ExecutionFinding("f-1", "review-1", "high", "fix it"),)
    )
    report = ReadinessEvaluator().evaluate(state, required_evidence=(requirement,))
    assert any("blocking finding" in reason for reason in report.reasons)


def test_stale_evidence_does_not_satisfy_readiness() -> None:
    state = initial_execution_state("run-1", profile_id="profile")
    state = replace(
        state,
        evidence=(ExecutionEvidence("e-1", "a-1", "pytest", "input-1", True, False),),
    )
    report = ReadinessEvaluator().evaluate(
        state,
        required_evidence=(EvidenceRequirement("pytest", "input-1", "tests"),),
    )
    assert not report.ready


def test_finding_disposition_is_typed_and_terminal() -> None:
    finding = ExecutionFinding("f-1", "review-1", "high", "fix it")
    disposition = dispose_finding(
        finding,
        status=FindingStatus.FIXED,
        actor_id="engineer",
        supporting_evidence=(ExecutionEvidence("e-1", "a-1", "pytest", "i-1", True),),
    )
    assert disposition.finding.status is FindingStatus.FIXED
    with pytest.raises(ValueError):
        dispose_finding(finding, status=FindingStatus.OPEN, actor_id="engineer")


def test_edit_invalidation_and_review_disagreement() -> None:
    state = initial_execution_state("run-1", profile_id="profile")
    state = replace(
        state,
        evidence=(
            ExecutionEvidence("e-1", "a-1", "pytest", "changed", True),
            ExecutionEvidence("e-2", "a-2", "pytest", "unrelated", True),
        ),
    )
    invalidated = invalidate_evidence(state, frozenset({"changed"}))
    assert not invalidated.evidence[0].fresh
    assert invalidated.evidence[1].fresh
    agreement = evaluate_review_agreement(
        (("reviewer-a", ("finding-1",)), ("reviewer-b", ("finding-2",)))
    )
    assert not agreement.sufficient


def test_publish_gate_requires_current_accepted_artifacts_and_agreement() -> None:
    state = initial_execution_state("run-1", profile_id="profile")
    blocked = ReadinessEvaluator().evaluate(
        state,
        publish=PublishRequirements(
            plan_fingerprint="plan-current",
            proposed_pr_fingerprint="pr-current",
            require_independent_review=True,
            reviewer_findings=(
                ("reviewer-a", ("finding-1",)),
                ("reviewer-b", ("finding-2",)),
            ),
        ),
    )
    assert not blocked.ready
    assert any("fingerprint" in reason for reason in blocked.reasons)
    assert any("review agreement" in reason for reason in blocked.reasons)
    state = replace(
        state,
        artifacts=(
            # Accepted artifacts carry the content fingerprint used by the gate.
            ExecutionArtifact("plan", "plan", "v1", "architect", "plan-current", True),
            ExecutionArtifact("pr", "proposed-pr", "v1", "manager", "pr-current", True),
        ),
    )
    ready = ReadinessEvaluator().evaluate(
        state,
        publish=PublishRequirements(
            plan_fingerprint="plan-current",
            proposed_pr_fingerprint="pr-current",
            require_independent_review=True,
            reviewer_findings=(
                ("reviewer-a", ("finding-1",)),
                ("reviewer-b", ("finding-1",)),
            ),
        ),
    )
    assert ready.ready


def test_publish_rejects_author_as_independent_reviewer() -> None:
    state = initial_execution_state("run-author-review", profile_id="profile")
    report = ReadinessEvaluator().evaluate(
        state,
        publish=PublishRequirements(
            require_independent_review=True,
            author_id="author",
            reviewer_findings=(("author", ("no-findings",)),),
        ),
    )

    assert not report.ready
    assert any(
        "author cannot provide independent review" in reason
        for reason in report.reasons
    )
