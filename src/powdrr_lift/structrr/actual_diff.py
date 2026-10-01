"""Compile an auditable actual diff from observed Structrr snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from powdrr_lift.core.decision_obligation import content_fingerprint
from powdrr_lift.structrr.rebase import rebase_structrr_snapshot, snapshot_digest
from powdrr_lift.structrr.source_manifest import (
    SourceManifest,
    compare_source_manifests,
)

ACTUAL_DIFF_SCHEMA_VERSION = "structrr-actual-diff-v1"


@dataclass(frozen=True, slots=True)
class StructrrActualDiff:
    """Structural operations and file observations for one candidate."""

    baseline_snapshot_fingerprint: str
    candidate_snapshot_fingerprint: str
    baseline_manifest_fingerprint: str
    candidate_manifest_fingerprint: str
    baseline_product_digest: str
    candidate_product_digest: str
    submission_base: str
    baseline_revision: str
    candidate_revision: str
    structural_operations: tuple[Mapping[str, Any], ...]
    source_observations: tuple[Mapping[str, Any], ...]
    declaration_changes: tuple[Mapping[str, Any], ...]
    behavioral_review_candidates: tuple[str, ...]
    extraction_complete: bool
    unknowns: tuple[str, ...]

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": ACTUAL_DIFF_SCHEMA_VERSION,
            "baseline_snapshot_fingerprint": self.baseline_snapshot_fingerprint,
            "candidate_snapshot_fingerprint": self.candidate_snapshot_fingerprint,
            "baseline_manifest_fingerprint": self.baseline_manifest_fingerprint,
            "candidate_manifest_fingerprint": self.candidate_manifest_fingerprint,
            "baseline_product_digest": self.baseline_product_digest,
            "candidate_product_digest": self.candidate_product_digest,
            "submission_base": self.submission_base,
            "baseline_revision": self.baseline_revision,
            "candidate_revision": self.candidate_revision,
            "structural_operations": [
                dict(item) for item in self.structural_operations
            ],
            "source_observations": [dict(item) for item in self.source_observations],
            "declaration_changes": [dict(item) for item in self.declaration_changes],
            "behavioral_review_candidates": list(self.behavioral_review_candidates),
            "extraction_complete": self.extraction_complete,
            "unknowns": list(self.unknowns),
        }
        if include_fingerprint:
            data["fingerprint"] = content_fingerprint(data)
        return data

    @property
    def fingerprint(self) -> str:
        return str(self.to_data()["fingerprint"])


def compile_actual_structrr_diff(
    baseline_snapshot: Mapping[str, Any],
    candidate_snapshot: Mapping[str, Any],
    baseline_manifest: SourceManifest,
    candidate_manifest: SourceManifest,
) -> StructrrActualDiff:
    """Build a diff with separate code observations and declared-intent changes.

    Source body changes are reported as behavioral review candidates. They do
    not count as proof that a requested behavior is implemented. Specification
    changes are reported separately and never become implementation evidence.
    """
    if baseline_manifest.submission_base != candidate_manifest.submission_base:
        raise ValueError("actual diff manifests must share one submission base")

    structural = rebase_structrr_snapshot(baseline_snapshot, candidate_snapshot)
    source_report = compare_source_manifests(baseline_manifest, candidate_manifest)
    raw_operations = [change.to_data() for change in structural.changes]
    operations: list[dict[str, Any]] = []
    unknowns = list(source_report["baseline_errors"])
    unknowns.extend(source_report["candidate_errors"])
    for change in raw_operations:
        action = _operation_action(str(change["kind"]))
        before = change.get("before")
        after = change.get("after")
        evidence_paths = sorted(
            {
                str(record["path"])
                for record in (before, after)
                if isinstance(record, Mapping)
                and isinstance(record.get("path"), str)
                and record["path"]
            }
        )
        operation = {
            "operation_id": "actual:"
            + content_fingerprint(change).removeprefix("sha256:"),
            "section": str(change["area"]),
            "subject_id": str(change["identity"]),
            "action": action,
            "kind": change["kind"],
            "before": before,
            "after": after,
            "changed_fields": list(change["changed_fields"]),
            "evidence_paths": evidence_paths,
            "reason": str(change["reason"]),
        }
        operations.append(operation)
        if action == "unresolved":
            unknowns.append(
                "ambiguous structural identity for "
                f"{change['area']}:{change['identity']}"
            )

    observation_changes = [
        item
        for item in source_report["changes"]
        if item["evidence_kind"] == "source_observation"
    ]
    declaration_changes = [
        item
        for item in source_report["changes"]
        if item["evidence_kind"] == "declaration"
    ]
    behavior_paths = tuple(
        sorted(
            {
                str(item["path"])
                for item in observation_changes
                if item["status"] in {"added", "modified", "deleted"}
            }
        )
    )
    complete = bool(source_report["passed"]) and not unknowns
    return StructrrActualDiff(
        baseline_snapshot_fingerprint=snapshot_digest(baseline_snapshot),
        candidate_snapshot_fingerprint=snapshot_digest(candidate_snapshot),
        baseline_manifest_fingerprint=baseline_manifest.fingerprint,
        candidate_manifest_fingerprint=candidate_manifest.fingerprint,
        baseline_product_digest=baseline_manifest.product_digest,
        candidate_product_digest=candidate_manifest.product_digest,
        submission_base=baseline_manifest.submission_base,
        baseline_revision=baseline_manifest.source_revision,
        candidate_revision=candidate_manifest.source_revision,
        structural_operations=tuple(operations),
        source_observations=tuple(observation_changes),
        declaration_changes=tuple(declaration_changes),
        behavioral_review_candidates=behavior_paths,
        extraction_complete=complete,
        unknowns=tuple(unknowns),
    )


def _operation_action(kind: str) -> str:
    if kind == "added":
        return "add"
    if kind == "removed":
        return "remove"
    if kind in {"changed", "presentation", "moved", "renamed"}:
        return "change"
    return "unresolved"


__all__ = [
    "ACTUAL_DIFF_SCHEMA_VERSION",
    "StructrrActualDiff",
    "compile_actual_structrr_diff",
]
