"""Reconcile implementation judgments with an accepted proposal and diff."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_lift.core.decision_obligation import content_fingerprint


def reconcile_actualization(
    *,
    proposal_fingerprint: str,
    diff_fingerprint: str,
    operation_ids: Sequence[str],
    retained_clause_ids: Sequence[str],
    unexplained_changes: Sequence[str],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a fail-closed report from ordered, diff-bound atomic judgments."""
    expected = [
        *(f"operation:{item}" for item in sorted(operation_ids)),
        *(f"intent:{item}" for item in sorted(retained_clause_ids)),
        *(f"unexplained:{item}" for item in sorted(unexplained_changes)),
    ]
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for decision in decisions:
        decision_id = decision.get("decision_id")
        if isinstance(decision_id, str):
            by_id.setdefault(decision_id, []).append(decision)

    findings: list[dict[str, Any]] = []
    for decision_id in expected:
        matches = by_id.get(decision_id, [])
        outcome = matches[0].get("outcome") if len(matches) == 1 else "missing"
        evidence = matches[0].get("evidence_fingerprint") if len(matches) == 1 else None
        explanation = matches[0].get("explanation") if len(matches) == 1 else None
        fresh = evidence == diff_fingerprint
        if decision_id.startswith("operation:"):
            status = (
                "fulfilled"
                if outcome == "pass" and fresh
                else "contradicted"
                if outcome == "fail" and fresh
                else "not_evaluable"
            )
        elif decision_id.startswith("intent:"):
            status = (
                "preserved"
                if outcome == "pass" and fresh
                else "violated"
                if outcome == "fail" and fresh
                else "insufficient_evidence"
            )
        else:
            status = (
                "implementation_detail"
                if outcome == "pass" and fresh
                else "unplanned_semantic_change"
                if outcome == "fail" and fresh
                else "not_evaluable"
            )
        findings.append(
            {
                "decision_id": decision_id,
                "status": status,
                "outcome": outcome,
                "evidence_fingerprint": evidence,
                "fresh": fresh,
                "explanation": explanation,
            }
        )
    passed = (
        len(by_id) == len(expected)
        and all(len(by_id.get(item, ())) == 1 for item in expected)
        and all(item["fresh"] for item in findings)
        and all(
            item["status"] in {"fulfilled", "preserved", "implementation_detail"}
            for item in findings
        )
    )
    report: dict[str, Any] = {
        "schema_version": "actualization-report-v1",
        "proposal_fingerprint": proposal_fingerprint,
        "diff_fingerprint": diff_fingerprint,
        "passed": passed,
        "findings": findings,
    }
    report["fingerprint"] = content_fingerprint(report)
    return report


__all__ = ["reconcile_actualization"]
