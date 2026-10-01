"""Compare proposed Structrr operations with an observed candidate snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_lift.core.decision_obligation import content_fingerprint
from powdrr_lift.structrr.proposal import ProposalRevision
from powdrr_lift.structrr.rebase import (
    rebase_structrr_snapshot,
    snapshot_digest,
)

CANDIDATE_COMPARISON_SCHEMA_VERSION = "candidate-comparison-v1"

_STRUCTURAL_SECTIONS = {
    "entities": "entity",
    "entity_relationships": "relationship",
    "files": "source_subject",
}
_OBSERVED_KINDS = {
    "add": {"added"},
    "remove": {"removed"},
    "change": {"changed", "presentation"},
}


def compare_candidate_snapshot(
    proposal: ProposalRevision,
    baseline_snapshot: Mapping[str, Any],
    candidate_snapshot: Mapping[str, Any],
    *,
    extraction_complete: bool,
) -> dict[str, Any]:
    """Report how observed candidate changes relate to a frozen proposal.

    Only structural proposal sections with a direct observation counterpart are
    matched deterministically. Declaration-only operations remain unevaluated;
    behavioral and invariant evidence must discharge those separately.
    """
    baseline_fingerprint = snapshot_digest(baseline_snapshot)
    if proposal.structrr_baseline_fingerprint != baseline_fingerprint:
        raise ValueError(
            "proposal baseline fingerprint does not match the comparison snapshot"
        )

    structural = rebase_structrr_snapshot(baseline_snapshot, candidate_snapshot)
    observed = list(structural.changes)
    findings: list[dict[str, Any]] = []
    consumed: set[int] = set()

    for operation in proposal.operations:
        area = _STRUCTURAL_SECTIONS.get(operation.section)
        if area is None:
            findings.append(
                {
                    "finding_id": f"operation:{operation.operation_id}",
                    "status": "not_evaluable",
                    "operation_id": operation.operation_id,
                    "expected": operation.to_data(),
                    "observed": [],
                    "reason": (
                        "this proposal section is declarative and requires its "
                        "separate behavior or invariant evidence route"
                    ),
                }
            )
            continue

        expected_kinds = _OBSERVED_KINDS.get(operation.action)
        if expected_kinds is None:
            findings.append(
                {
                    "finding_id": f"operation:{operation.operation_id}",
                    "status": "not_evaluable",
                    "operation_id": operation.operation_id,
                    "expected": operation.to_data(),
                    "observed": [],
                    "reason": "the proposal operation action is unsupported",
                }
            )
            continue
        matching = [
            (index, change)
            for index, change in enumerate(observed)
            if index not in consumed
            and change.area == area
            and change.identity == operation.subject_id
        ]
        exact = [
            (index, change)
            for index, change in matching
            if change.kind in expected_kinds
        ]
        if len(exact) == 1:
            index, change = exact[0]
            consumed.add(index)
            status = "fulfilled"
            reason = "the candidate contains the proposed structural operation"
            related = [change.to_data()]
        elif matching:
            status = "contradictory"
            reason = "the candidate changed the proposed subject in a different way"
            related = [change.to_data() for _, change in matching]
        elif _already_satisfied(operation, baseline_snapshot):
            status = "already_satisfied"
            reason = "the baseline already has the requested structural state"
            related = []
        else:
            status = "missing"
            reason = "the proposed structural operation is absent from the candidate"
            related = []
        findings.append(
            {
                "finding_id": f"operation:{operation.operation_id}",
                "status": status,
                "operation_id": operation.operation_id,
                "expected": operation.to_data(),
                "observed": related,
                "reason": reason,
            }
        )

    for index, change in enumerate(observed):
        if index in consumed:
            continue
        findings.append(
            {
                "finding_id": f"observed:{change.area}:{change.identity}:{change.kind}",
                "status": "unexpected",
                "operation_id": None,
                "expected": None,
                "observed": [change.to_data()],
                "reason": (
                    "the observed structural change has no matching proposal operation"
                ),
            }
        )

    blockers = {"missing", "contradictory", "unexpected", "not_evaluable"}
    passed = extraction_complete and not any(
        item["status"] in blockers for item in findings
    )
    report: dict[str, Any] = {
        "schema_version": CANDIDATE_COMPARISON_SCHEMA_VERSION,
        "scope": "structural_operations_only",
        "proposal_fingerprint": proposal.fingerprint,
        "baseline_fingerprint": baseline_fingerprint,
        "candidate_fingerprint": snapshot_digest(candidate_snapshot),
        "extraction_complete": extraction_complete,
        "separate_behavior_and_invariant_review_required": True,
        "passed": passed,
        "findings": findings,
    }
    report["fingerprint"] = content_fingerprint(report)
    return report


def _already_satisfied(operation: Any, baseline_snapshot: Mapping[str, Any]) -> bool:
    area = _STRUCTURAL_SECTIONS[operation.section]
    collection = {
        "entity": "entities",
        "relationship": "entity_relationships",
        "source_subject": "source_subjects",
    }[area]
    records = baseline_snapshot.get(collection)
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return False
    for record in records:
        if not isinstance(record, Mapping):
            continue
        identity = _record_identity(area, record)
        if identity == operation.subject_id:
            return operation.action == "add"
    return operation.action == "remove"


def _record_identity(area: str, record: Mapping[str, Any]) -> str:
    if area == "relationship":
        explicit = record.get("id")
        if isinstance(explicit, str) and explicit:
            return explicit
        return "|".join(
            str(record.get(key, "")) for key in ("source", "relationship", "target")
        )
    if area == "source_subject":
        value = record.get("stable_key")
    else:
        value = record.get("id")
    return value if isinstance(value, str) else ""


__all__ = [
    "CANDIDATE_COMPARISON_SCHEMA_VERSION",
    "compare_candidate_snapshot",
]
