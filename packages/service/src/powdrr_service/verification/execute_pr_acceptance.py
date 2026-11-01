"""Service-owned final acceptance for execute-PR implementation attempts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def build_execute_pr_test_report(
    authoring: Mapping[str, Any],
    *,
    test_sources: Mapping[str, str],
    candidate_result: Mapping[str, Any],
    correction_error: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """Combine authoring and candidate evidence into a blocking test report."""
    findings: list[str] = []
    if candidate_result.get("passed") is not True:
        findings.append(
            "pre-coding generated integration tests did not pass: "
            + json.dumps(candidate_result, sort_keys=True)
        )
    coverage_error = candidate_result.get("coverage_evidence_error")
    if coverage_error:
        findings.append(
            "generated test execution did not satisfy the coverage evidence "
            f"contract: {coverage_error}"
        )

    correction_reviews = authoring.get("correction_quality_reviews", [])
    correction_reviews = (
        correction_reviews if isinstance(correction_reviews, list) else []
    )
    quality_review = authoring.get("quality_review")
    quality_review = quality_review if isinstance(quality_review, Mapping) else {}
    quality_notes = list(quality_review.get("notes", []))
    for review in correction_reviews:
        if isinstance(review, Mapping):
            quality_notes.extend(review.get("notes", []))
    if correction_error:
        quality_notes.append(
            "A proposed generated-test correction was discarded because it did not "
            "pass the bounded correction checks; the original tests were retained. "
            + correction_error
        )

    report = {
        "passed": not findings,
        "generation_method": authoring.get("generation_method", "miniswe_pre_coding"),
        "paths": sorted(test_sources),
        "test_sha256": authoring.get("test_sha256", {}),
        "test_obligations": authoring.get("test_obligations", []),
        "coverage_references": authoring.get("coverage_references", {}),
        "quality_review": authoring.get("quality_review"),
        "correction_quality_reviews": correction_reviews,
        "corrections": authoring.get("corrections", []),
        "quality_notes": quality_notes,
        "baseline_execution_evidence": authoring.get("baseline_execution_evidence"),
        "authoring_attempts": authoring.get("authoring_attempts", []),
        "baseline": authoring["baseline_result"],
        "correction_baseline": authoring.get("correction_baseline"),
        "candidate": dict(candidate_result),
        "repair_findings": findings,
    }
    return report, findings


def execute_pr_attempt_passed(
    *,
    attempt_status: str,
    changed_paths: Sequence[str],
    diff_check_returncode: int,
    validation_status: str,
    integration_test_findings: Sequence[str],
    structural_findings: Sequence[Any],
) -> bool:
    """Apply the blocking final acceptance criteria for a coding attempt."""
    return (
        attempt_status == "completed"
        and bool(changed_paths)
        and diff_check_returncode == 0
        and validation_status == "passed"
        and not integration_test_findings
        and not structural_findings
    )


__all__ = ["build_execute_pr_test_report", "execute_pr_attempt_passed"]
