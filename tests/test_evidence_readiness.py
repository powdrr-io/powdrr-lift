from dataclasses import replace

import pytest

from powdrr_lift.core.execution_state import (
    ExecutionEvidence,
    ExecutionFinding,
    FindingStatus,
    initial_execution_state,
)
from powdrr_lift.execution.evidence import (
    EvidenceRequirement,
    ReadinessEvaluator,
    dispose_finding,
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
        finding, status=FindingStatus.FIXED, actor_id="engineer"
    )
    assert disposition.finding.status is FindingStatus.FIXED
    with pytest.raises(ValueError):
        dispose_finding(finding, status=FindingStatus.OPEN, actor_id="engineer")
