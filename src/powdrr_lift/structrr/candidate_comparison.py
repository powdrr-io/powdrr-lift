"""Compare proposed Structrr operations with an observed candidate snapshot."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_service.structrr.snapshot import snapshot_digest

from powdrr_lift.core.decision_obligation import content_fingerprint
from powdrr_lift.structrr.actual_diff import StructrrActualDiff
from powdrr_lift.structrr.proposal import ProposalRevision
from powdrr_lift.structrr.rebase import rebase_structrr_snapshot

CANDIDATE_COMPARISON_SCHEMA_VERSION = "candidate-comparison-v3"

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
    validation_expectations: Sequence[Mapping[str, Any]] = (),
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
    span_only_changes = [
        change
        for change in structural.changes
        if _is_source_span_only_change(change.to_data())
    ]
    observed = [
        change
        for change in structural.changes
        if not _is_source_span_only_change(change.to_data())
    ]
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
        match_mode = "exact"
        manifest_match: Mapping[str, Any] | None = None
        manifest_path: str | None = None
        if not exact and operation.section == "files" and actual_diff is not None:
            manifest_path = _proposal_file_path(operation)
            if manifest_path is not None:
                expected_status = {
                    "add": "added",
                    "remove": "deleted",
                    "change": "modified",
                }.get(operation.action)
                manifest_matching = [
                    item
                    for item in actual_diff.source_observations
                    if item.get("path") == manifest_path
                    and item.get("status") == expected_status
                ]
                if len(manifest_matching) == 1:
                    manifest_match = manifest_matching[0]
                    match_mode = "manifest_file"
        if not exact and operation.section == "files":
            flexible = _flexible_source_subject_matches(
                operation, observed, consumed, proposal.allowed_paths
            )
            if len(flexible) == 1:
                exact = flexible
                matching = flexible
                match_mode = "allowed_location"
        if manifest_match is not None:
            expected_digest = _proposal_file_content_digest(operation)
            actual_digest = manifest_match.get("after_digest")
            if expected_digest is not None and actual_digest != expected_digest:
                status = "contradictory"
                reason = "the candidate file content differs from the proposal"
            else:
                status = "fulfilled"
                reason = "the candidate file matches the proposed manifest change"
            related = [dict(manifest_match)]
        elif len(exact) == 1:
            index, change = exact[0]
            related = [change.to_data()]
            after_matches = (
                True
                if match_mode == "allowed_location"
                else _after_matches(operation, change.after)
            )
            if after_matches:
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
                "match_mode": match_mode if status == "fulfilled" else None,
                "matched_path": (
                    change.after.get("path")
                    if status == "fulfilled"
                    and match_mode == "allowed_location"
                    and isinstance(change.after, Mapping)
                    else manifest_path
                    if status == "fulfilled" and match_mode == "manifest_file"
                    else None
                ),
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

    _classify_reviewable_additions(findings, proposal.allowed_paths)
    _apply_scope_decisions(
        findings,
        scope_decisions,
        proposal=proposal,
        candidate_fingerprint=snapshot_digest(candidate_snapshot),
        actual_diff=actual_diff,
    )

    blockers = {"missing", "contradictory", "not_evaluable"}
    complete = extraction_complete and (
        actual_diff is None or actual_diff.extraction_complete
    )
    passed = complete and not any(
        item["status"] in blockers
        or (
            item["status"] == "unexpected"
            and item.get("review_classification")
            not in {"implementation_detail", "validation_change"}
        )
        for item in findings
    )
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
        "ignored_structural_noise": {
            "source_span_only_changes": len(span_only_changes),
            "paths": sorted(
                {
                    str(change.after["path"])
                    for change in span_only_changes
                    if change.after is not None
                    and isinstance(change.after.get("path"), str)
                }
            ),
        },
        "reviewable_additions_by_path": _group_reviewable_additions(findings),
        "validation_expectations": _normalize_validation_expectations(
            validation_expectations
        ),
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


def _flexible_source_subject_matches(
    operation: Any,
    observed: Sequence[Any],
    consumed: set[int],
    allowed_paths: Sequence[str],
) -> list[tuple[int, Any]]:
    """Match a planned symbol at another allowed path when its name is stable.

    Only `files` operations opt into this location-flexible behavior. An
    implementation location can be narrowed with `implementation_paths` in the
    plan item; otherwise the proposal's allowed paths form the scope.
    """
    content = operation.content
    raw_paths = content.get("implementation_paths", allowed_paths)
    if not isinstance(raw_paths, Sequence) or isinstance(raw_paths, (str, bytes)):
        return []
    scopes = tuple(
        item
        for item in raw_paths
        if isinstance(item, str) and _is_repository_relative_path(item)
    )
    if not scopes:
        return []
    expected_name = content.get("qualified_name") or content.get("symbol")
    if not isinstance(expected_name, str) or not expected_name.strip():
        expected_name = operation.subject_id
    expected_name = expected_name.split("::")[-1].strip(" .")
    suffix = expected_name.rsplit(".", 1)[-1]
    expected_kind = content.get("kind")
    matches: list[tuple[int, Any]] = []
    for index, change in enumerate(observed):
        if index in consumed or change.area != "source_subject":
            continue
        if change.kind not in _OBSERVED_KINDS.get(operation.action, set()):
            continue
        record = change.after
        if not isinstance(record, Mapping):
            continue
        qualified_name = record.get("qualified_name")
        path = record.get("path")
        if not isinstance(qualified_name, str) or not isinstance(path, str):
            continue
        if (
            qualified_name != expected_name
            and qualified_name.rsplit(".", 1)[-1] != suffix
        ):
            continue
        if isinstance(expected_kind, str) and record.get("kind") != expected_kind:
            continue
        if not _within_allowed_path(path, scopes):
            continue
        matches.append((index, change))
    return matches


def _is_source_span_only_change(change: Mapping[str, Any]) -> bool:
    return (
        change.get("area") == "source_subject"
        and change.get("kind") == "changed"
        and bool(change.get("changed_fields"))
        and set(change.get("changed_fields", ())) <= {"span"}
    )


def _change_paths(change: Mapping[str, Any]) -> tuple[str, ...]:
    observed = change.get("observed")
    records = observed if isinstance(observed, list) else [change]
    paths = {
        str(record[key])
        for item in records
        if isinstance(item, Mapping)
        for record in (item.get("before"), item.get("after"))
        if isinstance(record, Mapping)
        for key in ("path",)
        if isinstance(record.get(key), str) and record[key]
    }
    if not paths:
        identity = change.get("observed", [{}])
        identity_record = identity[0] if isinstance(identity, list) and identity else {}
        subject_id = (
            identity_record.get("identity")
            if isinstance(identity_record, Mapping)
            else None
        )
        if isinstance(subject_id, str) and subject_id.startswith("file:"):
            paths.add(subject_id.removeprefix("file:"))
    return tuple(sorted(paths))


def _classify_reviewable_additions(
    findings: list[dict[str, Any]], allowed_paths: Sequence[str]
) -> None:
    """Keep in-scope additions visible without treating helpers as missing APIs."""
    for finding in findings:
        if finding.get("status") != "unexpected":
            continue
        observed_records = finding.get("observed")
        if not isinstance(observed_records, list) or len(observed_records) != 1:
            continue
        change = observed_records[0]
        if not isinstance(change, Mapping) or change.get("kind") != "added":
            continue
        area = change.get("area")
        if area not in {"source_subject", "entity"}:
            continue
        paths = _change_paths(finding)
        if not paths or not all(
            _within_allowed_path(path, allowed_paths) for path in paths
        ):
            continue
        is_test_path = all(_is_test_path(path) for path in paths)
        finding["review_classification"] = (
            "validation_change" if is_test_path else "implementation_detail"
        )
        finding["review_paths"] = list(paths)
        finding["review_required"] = True


def _is_test_path(path: str) -> bool:
    """Recognize test trees nested beneath a project or component directory."""
    parts = path.replace("\\", "/").strip("/").split("/")
    return any(part.casefold() in {"test", "tests"} for part in parts[:-1])


def _proposal_file_path(operation: Any) -> str | None:
    content = operation.content
    raw_path = content.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        return raw_path.replace("\\", "/").strip("/")
    identity = operation.subject_id
    if isinstance(identity, str) and identity.startswith("file:"):
        return identity.removeprefix("file:").replace("\\", "/").strip("/")
    return None


def _proposal_file_content_digest(operation: Any) -> str | None:
    content = operation.content.get("content")
    if not isinstance(content, str):
        return None
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _group_reviewable_additions(
    findings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for finding in findings:
        classification = finding.get("review_classification")
        if classification not in {"implementation_detail", "validation_change"}:
            continue
        paths = finding.get("review_paths")
        if not isinstance(paths, list):
            continue
        change = finding.get("observed", [{}])[0]
        subject_id = change.get("identity") if isinstance(change, Mapping) else None
        for path in paths:
            if not isinstance(path, str):
                continue
            item = grouped.setdefault(
                path,
                {
                    "path": path,
                    "classification": classification,
                    "change_count": 0,
                    "subjects": [],
                    "roles": [],
                    "review_required": True,
                },
            )
            item["change_count"] += 1
            if isinstance(subject_id, str):
                item["subjects"].append(subject_id)
            after = change.get("after") if isinstance(change, Mapping) else None
            role = (
                after.get("kind") or after.get("type")
                if isinstance(after, Mapping)
                else None
            )
            if isinstance(role, str) and role:
                item["roles"].append(role)
    return [
        {
            **item,
            "subjects": sorted(set(item["subjects"])),
            "roles": sorted(set(item["roles"])),
        }
        for _, item in sorted(grouped.items())
    ]


def _normalize_validation_expectations(
    expectations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Expose tests as evidence requests, never as required patch paths."""
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(expectations, start=1):
        identifier = item.get("id")
        description = (
            item.get("description")
            or item.get("text")
            or item.get("name")
            or item.get("selector")
        )
        if not isinstance(description, str) or not description.strip():
            continue
        normalized.append(
            {
                "expectation_id": (
                    identifier.strip()
                    if isinstance(identifier, str) and identifier.strip()
                    else f"validation-{index}"
                ),
                "description": description.strip(),
                "evidence_route": "task_validation_or_instruction_probe",
                "file_change_required": False,
            }
        )
    return normalized


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


def _is_repository_relative_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return normalized == "." or (
        bool(normalized)
        and not normalized.startswith("/")
        and all(part not in {"", ".", ".."} for part in normalized.split("/"))
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
