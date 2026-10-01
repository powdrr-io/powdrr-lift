"""Compare proposed Structrr operations with an observed candidate snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_lift.core.decision_obligation import content_fingerprint
from powdrr_lift.structrr.actual_diff import StructrrActualDiff
from powdrr_lift.structrr.proposal import ProposalRevision
from powdrr_lift.structrr.rebase import (
    rebase_structrr_snapshot,
    snapshot_digest,
)

CANDIDATE_COMPARISON_SCHEMA_VERSION = "candidate-comparison-v2"

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


@dataclass(frozen=True, slots=True)
class CandidateScopeDecision:
    """A bounded review explaining one in-scope structural detail."""

    finding_id: str
    classification: str
    rationale: str
    evidence_paths: tuple[str, ...]
    proposal_fingerprint: str
    candidate_fingerprint: str
    reviewer_version: str
    actual_diff_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.classification != "necessary_detail":
            raise ValueError("unsupported candidate scope decision")
        if not all(
            value.strip()
            for value in (
                self.finding_id,
                self.rationale,
                self.proposal_fingerprint,
                self.candidate_fingerprint,
                self.reviewer_version,
            )
        ):
            raise ValueError("candidate scope decision fields are required")
        if not self.evidence_paths or len(set(self.evidence_paths)) != len(
            self.evidence_paths
        ):
            raise ValueError("candidate scope decision requires unique evidence paths")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": "candidate-scope-decision-v1",
            "finding_id": self.finding_id,
            "classification": self.classification,
            "rationale": self.rationale,
            "evidence_paths": list(self.evidence_paths),
            "proposal_fingerprint": self.proposal_fingerprint,
            "candidate_fingerprint": self.candidate_fingerprint,
            "actual_diff_fingerprint": self.actual_diff_fingerprint,
            "reviewer_version": self.reviewer_version,
        }


def compare_candidate_snapshot(
    proposal: ProposalRevision,
    baseline_snapshot: Mapping[str, Any],
    candidate_snapshot: Mapping[str, Any],
    *,
    extraction_complete: bool,
    actual_diff: StructrrActualDiff | None = None,
    scope_decisions: Sequence[CandidateScopeDecision] = (),
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

    if actual_diff is not None and (
        actual_diff.baseline_snapshot_fingerprint != baseline_fingerprint
        or actual_diff.candidate_snapshot_fingerprint
        != snapshot_digest(candidate_snapshot)
    ):
        raise ValueError("actual diff does not match the comparison snapshots")

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
            related = [change.to_data()]
            if _after_matches(operation, change.after):
                consumed.add(index)
                status = "fulfilled"
                reason = "the candidate contains the proposed structural operation"
            else:
                status = "contradictory"
                reason = (
                    "the candidate changed the proposed subject, but its observed "
                    "after-values do not satisfy the proposal"
                )
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

    _apply_scope_decisions(
        findings,
        scope_decisions,
        proposal=proposal,
        candidate_fingerprint=snapshot_digest(candidate_snapshot),
        actual_diff=actual_diff,
    )

    blockers = {"missing", "contradictory", "unexpected", "not_evaluable"}
    complete = extraction_complete and (
        actual_diff is None or actual_diff.extraction_complete
    )
    passed = complete and not any(item["status"] in blockers for item in findings)
    report: dict[str, Any] = {
        "schema_version": CANDIDATE_COMPARISON_SCHEMA_VERSION,
        "scope": "structural_operations_only",
        "proposal_fingerprint": proposal.fingerprint,
        "baseline_fingerprint": baseline_fingerprint,
        "candidate_fingerprint": snapshot_digest(candidate_snapshot),
        "extraction_complete": complete,
        "separate_behavior_and_invariant_review_required": True,
        "passed": passed,
        "findings": findings,
    }
    if actual_diff is not None:
        report["actual_diff_fingerprint"] = actual_diff.fingerprint
        report["candidate_product_digest"] = actual_diff.candidate_product_digest
        report["behavioral_review_candidates"] = list(
            actual_diff.behavioral_review_candidates
        )
        report["declaration_changes"] = [
            dict(item) for item in actual_diff.declaration_changes
        ]
        report["extraction_unknowns"] = list(actual_diff.unknowns)
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
            if operation.action in {"add", "change"}:
                return _after_matches(operation, record)
            return False
    return operation.action == "remove"


def _after_matches(operation: Any, record: Mapping[str, Any] | None) -> bool:
    if record is None:
        return operation.action == "remove"
    expected = {
        key: value for key, value in operation.content.items() if key not in {"action"}
    }
    return all(
        key in record and record[key] == value for key, value in expected.items()
    )


def _apply_scope_decisions(
    findings: list[dict[str, Any]],
    decisions: Sequence[CandidateScopeDecision],
    *,
    proposal: ProposalRevision,
    candidate_fingerprint: str,
    actual_diff: StructrrActualDiff | None,
) -> None:
    by_id = {item.finding_id: item for item in decisions}
    if len(by_id) != len(decisions):
        raise ValueError("candidate scope decisions contain duplicate finding IDs")
    unused = set(by_id)
    for finding in findings:
        decision = by_id.get(str(finding["finding_id"]))
        if decision is None:
            continue
        if finding["status"] != "unexpected":
            raise ValueError("scope decisions can only explain unexpected changes")
        if decision.proposal_fingerprint != proposal.fingerprint:
            raise ValueError("scope decision is bound to a different proposal")
        if decision.candidate_fingerprint != candidate_fingerprint:
            raise ValueError("scope decision is bound to a different candidate")
        expected_actual_diff = actual_diff.fingerprint if actual_diff else None
        if decision.actual_diff_fingerprint != expected_actual_diff:
            raise ValueError("scope decision is bound to a different actual diff")

        change = finding["observed"][0]
        changed_paths = {
            str(record["path"])
            for record in (change.get("before"), change.get("after"))
            if isinstance(record, Mapping) and isinstance(record.get("path"), str)
        }
        if not changed_paths or not set(decision.evidence_paths) <= changed_paths:
            raise ValueError(
                "scope decision evidence must reference changed source paths"
            )
        if actual_diff is not None:
            manifested_paths = {
                str(item["path"]) for item in actual_diff.source_observations
            }
            if not set(decision.evidence_paths) <= manifested_paths:
                raise ValueError(
                    "scope decision evidence is absent from the source manifest"
                )
        if not all(
            _within_allowed_path(path, proposal.allowed_paths)
            for path in decision.evidence_paths
        ):
            raise ValueError(
                "scope decision evidence is outside proposal allowed paths"
            )

        finding["status"] = "necessary_detail"
        finding["reason"] = decision.rationale
        finding["scope_decision"] = decision.to_data()
        unused.remove(decision.finding_id)
    if unused:
        raise ValueError(
            f"scope decisions reference unknown findings: {sorted(unused)}"
        )


def _within_allowed_path(path: str, scopes: Sequence[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        scope == "."
        or normalized == scope.rstrip("/")
        or normalized.startswith(scope.rstrip("/") + "/")
        for scope in scopes
    )


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
    "CandidateScopeDecision",
    "CANDIDATE_COMPARISON_SCHEMA_VERSION",
    "compare_candidate_snapshot",
]
