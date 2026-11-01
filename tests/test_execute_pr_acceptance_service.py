from __future__ import annotations

import pytest
from powdrr_service.verification.execute_pr_acceptance import (
    build_execute_pr_test_report,
    execute_pr_attempt_passed,
)


def test_execute_pr_report_combines_evidence_and_keeps_advice_nonblocking() -> None:
    report, findings = build_execute_pr_test_report(
        {
            "baseline_result": {"passed": False},
            "quality_review": {"notes": ["Review this additional boundary case."]},
            "correction_quality_reviews": [
                {"notes": ["Review the correction rationale."]}
            ],
            "corrections": [{"attempt_id": "attempt-2"}],
        },
        test_sources={"tests/test_api.py": "test source"},
        candidate_result={"passed": True, "stdout": "2 passed"},
        correction_error="Rejected an unsafe edit.",
    )

    assert findings == []
    assert report["passed"] is True
    assert report["paths"] == ["tests/test_api.py"]
    assert report["candidate"]["stdout"] == "2 passed"
    assert report["baseline"] == {"passed": False}
    assert len(report["quality_notes"]) == 3
    assert "discarded" in report["quality_notes"][-1]
    assert report["corrections"] == [{"attempt_id": "attempt-2"}]


@pytest.mark.parametrize(
    ("candidate", "expected_finding"),
    [
        ({"passed": False, "returncode": 1}, "did not pass"),
        (
            {"passed": True, "coverage_evidence_error": "expected 3, found 2"},
            "coverage evidence contract",
        ),
    ],
)
def test_execute_pr_test_report_blocks_failed_or_incomplete_validation_evidence(
    candidate: dict[str, object], expected_finding: str
) -> None:
    report, findings = build_execute_pr_test_report(
        {"baseline_result": {"passed": False}},
        test_sources={"tests/test_api.py": "test source"},
        candidate_result=candidate,
        correction_error=None,
    )

    assert report["passed"] is False
    assert len(findings) == 1
    assert expected_finding in findings[0]


def test_final_acceptance_requires_every_blocking_gate_to_pass() -> None:
    assert (
        execute_pr_attempt_passed(
            attempt_status="completed",
            changed_paths=["src/api.py"],
            diff_check_returncode=0,
            validation_status="passed",
            integration_test_findings=[],
            structural_findings=[],
        )
        is True
    )
    failed_cases = (
        execute_pr_attempt_passed(
            attempt_status="failed",
            changed_paths=["src/api.py"],
            diff_check_returncode=0,
            validation_status="passed",
            integration_test_findings=[],
            structural_findings=[],
        ),
        execute_pr_attempt_passed(
            attempt_status="completed",
            changed_paths=[],
            diff_check_returncode=0,
            validation_status="passed",
            integration_test_findings=[],
            structural_findings=[],
        ),
        execute_pr_attempt_passed(
            attempt_status="completed",
            changed_paths=["src/api.py"],
            diff_check_returncode=1,
            validation_status="passed",
            integration_test_findings=[],
            structural_findings=[],
        ),
        execute_pr_attempt_passed(
            attempt_status="completed",
            changed_paths=["src/api.py"],
            diff_check_returncode=0,
            validation_status="failed",
            integration_test_findings=[],
            structural_findings=[],
        ),
        execute_pr_attempt_passed(
            attempt_status="completed",
            changed_paths=["src/api.py"],
            diff_check_returncode=0,
            validation_status="passed",
            integration_test_findings=["tests failed"],
            structural_findings=[],
        ),
        execute_pr_attempt_passed(
            attempt_status="completed",
            changed_paths=["src/api.py"],
            diff_check_returncode=0,
            validation_status="passed",
            integration_test_findings=[],
            structural_findings=["proposal mismatch"],
        ),
    )
    assert failed_cases == (False, False, False, False, False, False)
