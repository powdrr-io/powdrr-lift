"""Deterministic audits for cumulative intent and implementation lineage."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_lift.core.decision_obligation import content_fingerprint
from powdrr_lift.structrr.proposal import ProposalRevision
from powdrr_lift.structrr.rebase import snapshot_digest

INTENT_LINEAGE_HISTORY_SCHEMA = "intent-lineage-history-v1"


def audit_intent_lineage(history: Mapping[str, Any]) -> dict[str, Any]:
    """Audit a replayable history manifest and return concrete findings.

    The input contains immutable artifact data, not artifact paths, so replay is
    independent of filesystem ordering and produces the same report bytes.
    """
    findings: list[dict[str, Any]] = []

    def finding(
        code: str,
        severity: str,
        message: str,
        *,
        revision: int | None = None,
        artifact_refs: Sequence[str] = (),
        subject_id: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "code": code,
            "severity": severity,
            "message": message,
            "artifact_refs": sorted(set(artifact_refs)),
        }
        if revision is not None:
            item["revision"] = revision
        if subject_id is not None:
            item["subject_id"] = subject_id
        findings.append(item)

    if history.get("schema_version") != INTENT_LINEAGE_HISTORY_SCHEMA:
        finding(
            "unsupported_history_schema",
            "critical",
            f"history schema must be {INTENT_LINEAGE_HISTORY_SCHEMA}",
        )

    source_refs = {
        str(item.get("source_ref"))
        for item in _mappings(history.get("sources"))
        if isinstance(item.get("source_ref"), str) and item.get("source_ref")
    }
    source_records_by_ref = {
        str(item.get("source_ref")): item
        for item in _mappings(history.get("sources"))
        if isinstance(item.get("source_ref"), str) and item.get("source_ref")
    }
    for source in _mappings(history.get("sources")):
        exact_text = source.get("exact_text")
        expected_source_fingerprint = source.get("content_fingerprint")
        if (
            not isinstance(exact_text, str)
            or not exact_text
            or expected_source_fingerprint
            != "sha256:" + hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
        ):
            finding(
                "source_fingerprint_mismatch",
                "critical",
                "source record text is missing or does not match its fingerprint",
                artifact_refs=("sources",),
                subject_id=str(source.get("source_ref", "unknown")),
            )
    update_ids = {
        str(item.get("update_id"))
        for item in _mappings(history.get("sources"))
        if isinstance(item.get("update_id"), str) and item.get("update_id")
    }
    revisions = _mappings(history.get("revisions"))
    parsed_revisions: list[tuple[int, Mapping[str, Any]]] = []
    seen_revisions: set[int] = set()
    for item in revisions:
        number = item.get("revision")
        if not isinstance(number, int) or number < 1:
            finding(
                "invalid_revision_number", "high", "revision number must be positive"
            )
            continue
        if number in seen_revisions:
            finding(
                "duplicate_revision",
                "high",
                f"revision {number} occurs more than once",
                revision=number,
            )
            continue
        seen_revisions.add(number)
        parsed_revisions.append((number, item))
    parsed_revisions.sort(key=lambda pair: pair[0])

    previous_number: int | None = None
    previous_proposal_fingerprint: str | None = None
    latest_tree: str | None = None
    latest_revision_number = parsed_revisions[-1][0] if parsed_revisions else None
    for number, revision in parsed_revisions:
        if previous_number is not None and number != previous_number + 1:
            finding(
                "revision_gap",
                "high",
                f"revision {previous_number + 1} is missing from the history",
                revision=number,
            )
        previous_number = number
        revision_ref = f"revision:{number}"
        raw_proposal = revision.get("proposal_revision")
        proposal: ProposalRevision | None = None
        if isinstance(raw_proposal, Mapping):
            try:
                proposal = ProposalRevision.from_data(raw_proposal)
            except (TypeError, ValueError) as error:
                finding(
                    "invalid_proposal_revision",
                    "critical",
                    str(error),
                    revision=number,
                    artifact_refs=(revision_ref, "proposal_revision"),
                )
        else:
            finding(
                "missing_lineage_edge",
                "high",
                "proposal revision is missing",
                revision=number,
                artifact_refs=(revision_ref,),
                subject_id="proposal_revision",
            )
        parent_fingerprint = revision.get("parent_proposal_fingerprint")
        expected_parent = previous_proposal_fingerprint
        if parent_fingerprint != expected_parent:
            finding(
                "proposal_lineage_mismatch",
                "high",
                "proposal revision parent does not match the preceding "
                "accepted revision",
                revision=number,
                artifact_refs=(revision_ref, "proposal_revision"),
            )
        if proposal is not None:
            previous_proposal_fingerprint = proposal.fingerprint
        proposal_operations = proposal.operations if proposal is not None else ()
        current_proposal_fingerprint = (
            proposal.fingerprint if proposal is not None else None
        )

        lineage = revision.get("lineage")
        if not isinstance(lineage, Mapping):
            lineage = {}
        required_edges = (
            "source_update_ids",
            "intent_clause_ids",
            "execution_unit_ids",
            "intent_packet_fingerprints",
            "validation_evidence_fingerprints",
            "accepted_structrr_fingerprint",
            "repository_tree",
        )
        for edge in required_edges:
            value = lineage.get(edge)
            present = bool(value) if isinstance(value, (str, list, tuple)) else False
            if not present:
                finding(
                    "missing_lineage_edge",
                    "high",
                    f"lineage edge {edge} is missing",
                    revision=number,
                    artifact_refs=(revision_ref,),
                    subject_id=edge,
                )

        for update_id in _strings(lineage.get("source_update_ids")):
            if update_id not in update_ids:
                finding(
                    "missing_lineage_edge",
                    "high",
                    f"source update {update_id!r} has no source record",
                    revision=number,
                    artifact_refs=(revision_ref, "sources"),
                    subject_id=update_id,
                )
        clause_history_records = _mappings(history.get("intent_clause_history"))
        known_clause_ids = {
            str(item.get("clause_id"))
            for item in clause_history_records
            if isinstance(item.get("clause_id"), str)
        }
        for clause_id in _strings(lineage.get("intent_clause_ids")):
            if clause_id not in known_clause_ids:
                finding(
                    "missing_lineage_edge",
                    "high",
                    f"intent clause {clause_id!r} has no clause history record",
                    revision=number,
                    artifact_refs=(revision_ref, "intent_clause_history"),
                    subject_id=clause_id,
                )
                continue
            clause_records = [
                item
                for item in clause_history_records
                if item.get("clause_id") == clause_id
            ]
            source_ref = clause_records[-1].get("source_ref")
            source_record = source_records_by_ref.get(str(source_ref))
            if not isinstance(source_record, Mapping) or source_record.get(
                "update_id"
            ) not in _strings(lineage.get("source_update_ids")):
                finding(
                    "intent_source_update_mismatch",
                    "high",
                    "intent clause provenance does not resolve through this "
                    "revision's source updates",
                    revision=number,
                    artifact_refs=(revision_ref, "sources", "intent_clause_history"),
                    subject_id=clause_id,
                )

        raw_review = revision.get("proposal_review")
        if (
            not isinstance(raw_review, Mapping)
            or raw_review.get("accepted") is not True
            or proposal is None
            or raw_review.get("proposal_fingerprint") != proposal.fingerprint
        ):
            finding(
                "proposal_review_not_current",
                "high",
                "proposal review is missing, rejected, or bound to another proposal",
                revision=number,
                artifact_refs=(revision_ref, "proposal_review"),
            )

        raw_actualization = revision.get("actualization_report")
        if not isinstance(raw_actualization, Mapping):
            finding(
                "missing_actualization",
                "high",
                "accepted revision has no actualization report",
                revision=number,
                artifact_refs=(revision_ref,),
            )
        else:
            if raw_actualization.get("passed") is not True:
                finding(
                    "actualization_drift",
                    "high",
                    "accepted revision has a failed actualization report",
                    revision=number,
                    artifact_refs=(revision_ref, "actualization_report"),
                )
            stored_fingerprint = raw_actualization.get("fingerprint")
            unsigned = {
                key: value
                for key, value in raw_actualization.items()
                if key != "fingerprint"
            }
            if stored_fingerprint != content_fingerprint(unsigned):
                finding(
                    "actualization_fingerprint_mismatch",
                    "critical",
                    "actualization report content does not match its fingerprint",
                    revision=number,
                    artifact_refs=(revision_ref, "actualization_report"),
                )
            if (
                proposal is not None
                and raw_actualization.get("proposal_fingerprint")
                != proposal.fingerprint
            ):
                finding(
                    "actualization_proposal_mismatch",
                    "critical",
                    "actualization report references a different proposal revision",
                    revision=number,
                    artifact_refs=(
                        revision_ref,
                        "proposal_revision",
                        "actualization_report",
                    ),
                )
            actualization_findings = _mappings(raw_actualization.get("findings"))
            statuses = {
                item.get("decision_id"): item.get("status")
                for item in actualization_findings
            }
            if proposal is not None:
                for operation in proposal.operations:
                    decision_id = f"operation:{operation.operation_id}"
                    if statuses.get(decision_id) != "fulfilled":
                        finding(
                            "operation_drift",
                            "high",
                            "accepted proposal operation lacks a fulfilled "
                            "actualization finding",
                            revision=number,
                            artifact_refs=(
                                revision_ref,
                                "proposal_revision",
                                "actualization_report",
                            ),
                            subject_id=operation.operation_id,
                        )
            for item in actualization_findings:
                if item.get("status") in {
                    "contradicted",
                    "violated",
                    "unplanned_semantic_change",
                }:
                    finding(
                        "actualization_drift",
                        "high",
                        f"actualization finding is {item.get('status')}",
                        revision=number,
                        artifact_refs=(revision_ref, "actualization_report"),
                        subject_id=str(item.get("decision_id", "unknown")),
                    )
                elif item.get("fresh") is not True:
                    finding(
                        "stale_actualization_evidence",
                        "high",
                        "actualization evidence does not match the recorded diff",
                        revision=number,
                        artifact_refs=(revision_ref, "actualization_report"),
                        subject_id=str(item.get("decision_id", "unknown")),
                    )

        evidence = _mappings(revision.get("validation_evidence"))
        tree = lineage.get("repository_tree")
        latest_tree = tree if isinstance(tree, str) and tree else latest_tree
        evidence_fingerprints = {
            str(item.get("fingerprint"))
            for item in evidence
            if isinstance(item.get("fingerprint"), str)
        }
        for evidence_fingerprint in _strings(
            lineage.get("validation_evidence_fingerprints")
        ):
            if evidence_fingerprint not in evidence_fingerprints:
                finding(
                    "missing_lineage_edge",
                    "high",
                    "referenced validation evidence is absent from the revision",
                    revision=number,
                    artifact_refs=(revision_ref, "validation_evidence"),
                    subject_id=evidence_fingerprint,
                )
        accepted_state = revision.get("accepted_structrr_state")
        accepted_snapshot = (
            accepted_state.get("snapshot")
            if isinstance(accepted_state, Mapping)
            else None
        )
        snapshot_matches = False
        if isinstance(accepted_snapshot, Mapping):
            try:
                snapshot_matches = snapshot_digest(accepted_snapshot) == lineage.get(
                    "accepted_structrr_fingerprint"
                )
            except (TypeError, ValueError):
                snapshot_matches = False
        if (
            not isinstance(accepted_state, Mapping)
            or accepted_state.get("state_fingerprint")
            != lineage.get("accepted_structrr_fingerprint")
            or accepted_state.get("repository_tree") != tree
            or not snapshot_matches
        ):
            finding(
                "accepted_state_mismatch",
                "critical",
                "accepted Structrr state is missing or does not match lineage identity",
                revision=number,
                artifact_refs=(revision_ref, "accepted_structrr_state"),
            )

        units = _mappings(revision.get("execution_units"))
        unit_ids = {str(item.get("unit_id")) for item in units if item.get("unit_id")}
        for operation in proposal_operations:
            owners = [
                unit
                for unit in units
                if operation.operation_id in _strings(unit.get("operation_ids"))
                and unit.get("proposal_fingerprint") == current_proposal_fingerprint
            ]
            if len(owners) != 1:
                finding(
                    "operation_execution_lineage_incomplete",
                    "high",
                    "proposal operation must map to exactly one current execution unit",
                    revision=number,
                    artifact_refs=(
                        revision_ref,
                        "proposal_revision",
                        "execution_units",
                    ),
                    subject_id=operation.operation_id,
                )
        for unit_id in _strings(lineage.get("execution_unit_ids")):
            if unit_id not in unit_ids:
                finding(
                    "missing_lineage_edge",
                    "high",
                    "referenced execution unit is absent from the revision",
                    revision=number,
                    artifact_refs=(revision_ref, "execution_units"),
                    subject_id=unit_id,
                )
        packets = _mappings(revision.get("intent_packets"))
        packet_fingerprints = {
            str(item.get("packet_fingerprint"))
            for item in packets
            if isinstance(item.get("packet_fingerprint"), str)
        }
        for packet_fingerprint in _strings(lineage.get("intent_packet_fingerprints")):
            if packet_fingerprint not in packet_fingerprints:
                finding(
                    "missing_lineage_edge",
                    "high",
                    "referenced intent packet is absent from the revision",
                    revision=number,
                    artifact_refs=(revision_ref, "intent_packets"),
                    subject_id=packet_fingerprint,
                )
        for operation in proposal_operations:
            if (
                sum(
                    operation.operation_id in _strings(packet.get("operation_ids"))
                    and packet.get("execution_unit_id") in unit_ids
                    and packet.get("proposal_fingerprint")
                    == current_proposal_fingerprint
                    for packet in packets
                )
                != 1
            ):
                finding(
                    "operation_packet_lineage_incomplete",
                    "high",
                    "proposal operation must map to exactly one current intent packet",
                    revision=number,
                    artifact_refs=(revision_ref, "proposal_revision", "intent_packets"),
                    subject_id=operation.operation_id,
                )
        for record in evidence:
            if not record.get("fingerprint"):
                finding(
                    "missing_lineage_edge",
                    "medium",
                    "validation evidence has no fingerprint",
                    revision=number,
                    artifact_refs=(revision_ref, "validation_evidence"),
                )
            if record.get("candidate_tree") != tree:
                finding(
                    "stale_validation_evidence",
                    "high",
                    "validation evidence belongs to a different repository tree",
                    revision=number,
                    artifact_refs=(revision_ref, "validation_evidence"),
                    subject_id=str(record.get("contract_id", "unknown")),
                )
            if record.get("status") != "passed":
                finding(
                    "validation_not_passing",
                    "high",
                    f"validation evidence status is {record.get('status')!r}",
                    revision=number,
                    artifact_refs=(revision_ref, "validation_evidence"),
                    subject_id=str(record.get("contract_id", "unknown")),
                )
        _check_sources(proposal, source_refs, finding, number, revision_ref)

    active_clauses = _mappings(history.get("active_intent_clauses"))
    active_by_id: dict[str, Mapping[str, Any]] = {}
    all_clause_ids = {
        str(item.get("clause_id"))
        for item in _mappings(history.get("intent_clause_history"))
        if isinstance(item.get("clause_id"), str)
    }
    for clause in active_clauses:
        raw_clause_id = clause.get("clause_id")
        if not isinstance(raw_clause_id, str) or not raw_clause_id:
            finding("invalid_active_clause", "high", "active clause has no ID")
            continue
        clause_id = raw_clause_id
        if clause.get("active") is not True:
            finding(
                "inactive_clause_marked_active",
                "critical",
                "active intent inventory contains a clause marked inactive",
                artifact_refs=("active_intent_clauses",),
                subject_id=clause_id,
            )
        if clause_id in active_by_id:
            finding(
                "duplicate_active_clause",
                "high",
                "active clause ID occurs more than once",
                subject_id=clause_id,
            )
        active_by_id[clause_id] = clause
        source_ref = clause.get("source_ref")
        if not isinstance(source_ref, str) or source_ref not in source_refs:
            finding(
                "orphaned_intent_source",
                "high",
                "active intent clause has no resolvable source record",
                artifact_refs=("active_intent_clauses",),
                subject_id=clause_id,
            )
        supersedes = clause.get("supersedes_clause_id")
        if isinstance(supersedes, str) and supersedes:
            if supersedes not in all_clause_ids:
                finding(
                    "missing_supersession_target",
                    "high",
                    "supersession target is absent from clause history",
                    artifact_refs=("active_intent_clauses", "intent_clause_history"),
                    subject_id=clause_id,
                )
            elif supersedes in active_by_id or any(
                item.get("clause_id") == supersedes for item in active_clauses
            ):
                finding(
                    "superseded_clause_still_active",
                    "critical",
                    "a clause superseded by active intent is still active",
                    artifact_refs=("active_intent_clauses", "intent_clause_history"),
                    subject_id=str(supersedes),
                )

    current_invariants = _mappings(history.get("current_invariant_evidence"))
    if active_clauses and not current_invariants:
        finding(
            "missing_current_invariant_evidence",
            "high",
            "current invariant satisfaction has not been evaluated",
            artifact_refs=("current_invariant_evidence",),
        )
    current_by_clause: dict[str, list[Mapping[str, Any]]] = {}
    for record in current_invariants:
        current_clause_id = record.get("clause_id")
        if isinstance(current_clause_id, str):
            current_by_clause.setdefault(current_clause_id, []).append(record)
    for clause_id in active_by_id:
        matches = current_by_clause.get(clause_id, [])
        if not matches:
            finding(
                "missing_current_invariant_evidence",
                "high",
                "active intent clause has no current evidence result",
                revision=latest_revision_number,
                artifact_refs=("active_intent_clauses", "current_invariant_evidence"),
                subject_id=clause_id,
            )
        elif len(matches) > 1:
            finding(
                "duplicate_current_invariant_evidence",
                "high",
                "active intent clause has multiple current evidence results",
                revision=latest_revision_number,
                artifact_refs=("current_invariant_evidence",),
                subject_id=clause_id,
            )
    for record in current_invariants:
        clause_id = str(record.get("clause_id", "unknown"))
        if record.get("outcome") != "pass":
            finding(
                "current_invariant_failure",
                "critical",
                f"current invariant outcome is {record.get('outcome')!r}",
                revision=latest_revision_number,
                artifact_refs=("current_invariant_evidence",),
                subject_id=clause_id,
            )
        if not record.get("evidence_fingerprint"):
            finding(
                "missing_current_invariant_evidence",
                "high",
                "current invariant result has no evidence fingerprint",
                artifact_refs=("current_invariant_evidence",),
                subject_id=clause_id,
            )
        if latest_tree and record.get("candidate_tree") != latest_tree:
            finding(
                "stale_current_invariant_evidence",
                "critical",
                "current invariant evidence is not for the latest repository tree",
                revision=latest_revision_number,
                artifact_refs=("current_invariant_evidence", "latest_revision"),
                subject_id=clause_id,
            )

    findings.sort(
        key=lambda item: (
            item.get("revision", 0),
            item["code"],
            item.get("subject_id", ""),
            item["message"],
        )
    )
    severity_counts = dict(
        sorted(Counter(item["severity"] for item in findings).items())
    )
    drift_revisions = [
        item["revision"]
        for item in findings
        if isinstance(item.get("revision"), int)
        and item["code"]
        in {"operation_drift", "actualization_drift", "current_invariant_failure"}
    ]
    report: dict[str, Any] = {
        "schema_version": "intent-lineage-audit-report-v1",
        "history_fingerprint": content_fingerprint(dict(history)),
        "revision_count": len(parsed_revisions),
        "latest_repository_tree": latest_tree,
        "first_drift_revision": min(drift_revisions) if drift_revisions else None,
        "severity_counts": severity_counts,
        "passed": not findings,
        "findings": findings,
    }
    report["fingerprint"] = content_fingerprint(report)
    return report


def _check_sources(
    proposal: ProposalRevision | None,
    source_refs: set[str],
    add_finding: Any,
    revision: int,
    revision_ref: str,
) -> None:
    if proposal is None:
        return
    for source_ref in proposal.source_refs:
        if source_ref not in source_refs:
            add_finding(
                "orphaned_proposal_source",
                "high",
                f"proposal source {source_ref!r} has no source record",
                revision=revision,
                artifact_refs=(revision_ref, "proposal_revision", "sources"),
                subject_id=source_ref,
            )


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str) and item]


__all__ = ["INTENT_LINEAGE_HISTORY_SCHEMA", "audit_intent_lineage"]
