"""Structrr proposal and reconciliation contracts for multi-PR plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from powdrr_service.structrr.actual_diff import (
    StructrrActualDiff,
    compile_actual_structrr_diff,
)
from powdrr_service.structrr.candidate_comparison import compare_candidate_snapshot
from powdrr_service.structrr.design import project_sections
from powdrr_service.structrr.proposal import ProposalRevision, compile_proposal_revision
from powdrr_service.structrr.rebase import rebase_structrr_snapshot
from powdrr_service.structrr.snapshot import snapshot_digest
from powdrr_service.structrr.source_manifest import SourceManifest
from powdrr_service.structrr.user_stories import STORY_SECTIONS
from powdrr_service.verification.test_obligations import acceptance_criteria_contract

MULTIPR_STRUCTRR_SCHEMA = "multipr-structrr-proposal-v2"
_LEGACY_MULTIPR_STRUCTRR_SCHEMA = "multipr-structrr-proposal-v1"
_STRUCTURAL_SECTIONS = {"entities", "entity_relationships", "files", *STORY_SECTIONS}
_ACTIONS = {"added", "changed", "deleted", "removed"}


@dataclass(frozen=True, slots=True)
class MultiPRStructrrProposal:
    """Frozen plan-wide proposal partitioned into ordered PR proposals."""

    plan_id: str
    baseline_snapshot: Mapping[str, Any]
    full_proposal: ProposalRevision
    item_proposals: Mapping[str, ProposalRevision]
    item_order: tuple[str, ...]
    item_baselines: Mapping[str, Mapping[str, Any]]
    expected_final_snapshot: Mapping[str, Any]

    def to_data(self) -> dict[str, Any]:
        data = {
            "schema_version": MULTIPR_STRUCTRR_SCHEMA,
            "plan_id": self.plan_id,
            "baseline_fingerprint": snapshot_digest(self.baseline_snapshot),
            "baseline_snapshot": dict(self.baseline_snapshot),
            "full_proposal": self.full_proposal.to_data(),
            "item_proposals": {
                item_id: proposal.to_data()
                for item_id, proposal in sorted(self.item_proposals.items())
            },
            "item_order": list(self.item_order),
            "item_baseline_fingerprints": {
                item_id: snapshot_digest(snapshot)
                for item_id, snapshot in sorted(self.item_baselines.items())
            },
            "expected_final_fingerprint": snapshot_digest(self.expected_final_snapshot),
            "expected_final_snapshot": dict(self.expected_final_snapshot),
        }
        data["fingerprint"] = _content_fingerprint(data)
        return data


def compile_multi_pr_proposal(
    *,
    plan_id: str,
    baseline_snapshot: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    proposed_operations: Sequence[Mapping[str, Any]],
    source_ref: str,
) -> MultiPRStructrrProposal:
    """Validate one full diff and partition it across the reviewed PR items.

    Each model-generated operation must identify exactly one plan item. The
    complete proposal is compiled from all operations; per-item proposals are
    deterministic subsets, so they cannot silently invent work absent from the
    final plan diff.
    """
    item_by_id: dict[str, Mapping[str, Any]] = {}
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("multi-PR Structrr plan item has no stable id")
        if item_id in item_by_id:
            raise ValueError(f"duplicate Structrr plan item id: {item_id}")
        item_by_id[item_id] = item
    if not item_by_id:
        raise ValueError("multi-PR Structrr proposal requires plan items")

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
        item_id: {} for item_id in item_by_id
    }
    evidence_by_item: dict[str, list[Mapping[str, Any]]] = {
        item_id: [] for item_id in item_by_id
    }
    all_code_evidence: list[Mapping[str, Any]] = []
    all_sections: dict[str, list[dict[str, Any]]] = {}
    operation_ids: set[str] = set()
    for index, raw in enumerate(proposed_operations, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"Structrr operation {index} must be an object")
        operation_id = raw.get("operation_id")
        item_id = raw.get("item_id")
        section = raw.get("section")
        subject_id = raw.get("subject_id")
        action = raw.get("action")
        content = raw.get("content")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (operation_id, item_id, section, subject_id, action)
        ):
            raise ValueError(f"Structrr operation {index} is missing an identity")
        assert isinstance(operation_id, str)
        assert isinstance(item_id, str)
        assert isinstance(section, str)
        assert isinstance(subject_id, str)
        assert isinstance(action, str)
        if operation_id in operation_ids:
            raise ValueError(f"duplicate Structrr operation id: {operation_id}")
        if item_id not in item_by_id:
            raise ValueError(
                f"Structrr operation {operation_id!r} references unknown item "
                f"{item_id!r}"
            )
        if item_id == "final-integration-review":
            raise ValueError(
                "overall Structrr operations cannot be assigned to the final "
                "review item"
            )
        if section not in _STRUCTURAL_SECTIONS:
            raise ValueError(
                f"Structrr operation {operation_id!r} uses unsupported section "
                f"{section!r}"
            )
        if action not in _ACTIONS:
            raise ValueError(
                f"Structrr operation {operation_id!r} uses unsupported action "
                f"{action!r}"
            )
        if not isinstance(content, Mapping):
            raise ValueError(f"Structrr operation {operation_id!r} has no content")
        identity_key = "stable_key" if section == "files" else "id"
        if content.get(identity_key, subject_id) != subject_id:
            raise ValueError(
                f"Structrr operation {operation_id!r} content identity differs "
                "from subject_id"
            )
        normalized = (
            {
                "id": subject_id,
                "action": action,
                "content": {**dict(content), "id": subject_id},
            }
            if section in STORY_SECTIONS
            else {**dict(content), identity_key: subject_id, "action": action}
        )
        grouped[item_id].setdefault(section, []).append(normalized)
        all_sections.setdefault(section, []).append(normalized)
        raw_evidence = raw.get("code_evidence", [])
        if not isinstance(raw_evidence, list) or not all(
            isinstance(item, Mapping) for item in raw_evidence
        ):
            raise ValueError(
                f"Structrr operation {operation_id!r} code_evidence must be "
                "a list of objects"
            )
        if raw_evidence:
            evidence_record = {
                "operation_id": operation_id,
                "item_id": item_id,
                "section": section,
                "subject_id": subject_id,
                "references": [dict(item) for item in raw_evidence],
            }
            evidence_by_item[item_id].append(evidence_record)
            all_code_evidence.append(evidence_record)
        operation_ids.add(operation_id)

    def compile(
        proposal_id: str,
        selected_items: Sequence[Mapping[str, Any]],
        sections: Mapping[str, list[dict[str, Any]]],
        baseline: Mapping[str, Any],
        code_evidence: Sequence[Mapping[str, Any]],
    ) -> ProposalRevision:
        criteria = tuple(
            criterion
            for item in selected_items
            for criterion in _strings(item.get("acceptance_criteria"))
        )
        allowed_paths = tuple(
            path
            for item in selected_items
            for path in _strings(item.get("allowed_paths"))
        )
        preserve = tuple(
            value
            for item in selected_items
            for value in _strings(item.get("must_preserve"))
        )
        non_goals = tuple(
            value
            for item in selected_items
            for value in _strings(item.get("non_goals"))
        )
        test_cases: list[dict[str, Any]] = []
        for item in selected_items:
            item_id = str(item["id"])
            verification = item.get("verification")
            raw_test_cases = (
                verification.get("required_test_cases")
                if isinstance(verification, Mapping)
                else None
            )
            if isinstance(raw_test_cases, list):
                cases = [
                    dict(case) for case in raw_test_cases if isinstance(case, Mapping)
                ]
            else:
                criteria_for_item = _strings(item.get("acceptance_criteria"))
                cases = (
                    acceptance_criteria_contract(criteria_for_item)
                    if criteria_for_item
                    else []
                )
            for case in cases:
                case_id = str(case.get("id", case.get("obligation_id", "")))
                description = str(case.get("description", case.get("source_text", "")))
                test_cases.append(
                    {
                        "id": (
                            case_id
                            if len(selected_items) == 1
                            else f"{item_id}/{case_id}"
                        ),
                        "description": description,
                        "status": "planned",
                        **{
                            key: case[key]
                            for key in ("test_type", "scenario", "expected_outcome")
                            if key in case
                        },
                    }
                )
        return compile_proposal_revision(
            proposal_id,
            baseline,
            sections,
            acceptance_criteria=criteria,
            must_preserve=preserve,
            non_goals=non_goals,
            allowed_paths=allowed_paths or (".",),
            source_refs=(source_ref,),
            code_evidence=code_evidence,
            verification={
                "required_test_cases": test_cases,
                "validation_runs": [],
            },
        )

    full = compile(
        f"{plan_id}:full",
        tuple(item_by_id.values()),
        all_sections,
        baseline_snapshot,
        all_code_evidence,
    )
    projected = dict(baseline_snapshot)
    item_proposals: dict[str, ProposalRevision] = {}
    item_baselines: dict[str, Mapping[str, Any]] = {}
    for item_id, item in item_by_id.items():
        item_baselines[item_id] = dict(projected)
        item_proposals[item_id] = compile(
            f"{plan_id}:{item_id}",
            (item,),
            grouped[item_id],
            projected,
            evidence_by_item[item_id],
        )
        projected = _apply_sections(projected, grouped[item_id])
    expected_final = projected
    return MultiPRStructrrProposal(
        plan_id=plan_id,
        baseline_snapshot=dict(baseline_snapshot),
        full_proposal=full,
        item_proposals=item_proposals,
        item_order=tuple(item_by_id),
        item_baselines=item_baselines,
        expected_final_snapshot=expected_final,
    )


def load_multi_pr_proposal(path: str | Path) -> MultiPRStructrrProposal:
    """Load a persisted multi-PR proposal and verify every content fingerprint."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"could not read multi-PR Structrr proposal: {error}"
        ) from error
    if not isinstance(raw, Mapping) or raw.get("schema_version") not in {
        MULTIPR_STRUCTRR_SCHEMA,
        _LEGACY_MULTIPR_STRUCTRR_SCHEMA,
    }:
        raise ValueError("unsupported multi-PR Structrr proposal")
    unsigned = {key: value for key, value in raw.items() if key != "fingerprint"}
    if raw.get("fingerprint") != _content_fingerprint(unsigned):
        raise ValueError("multi-PR Structrr proposal fingerprint is invalid")
    baseline = raw.get("baseline_snapshot")
    full_raw = raw.get("full_proposal")
    item_raw = raw.get("item_proposals")
    expected_final = raw.get("expected_final_snapshot")
    if (
        not isinstance(baseline, Mapping)
        or not isinstance(full_raw, Mapping)
        or not isinstance(expected_final, Mapping)
    ):
        raise ValueError("multi-PR Structrr proposal is missing its baseline or diff")
    if raw.get("baseline_fingerprint") != snapshot_digest(baseline):
        raise ValueError("multi-PR Structrr baseline fingerprint is invalid")
    if raw.get("expected_final_fingerprint") != snapshot_digest(expected_final):
        raise ValueError("multi-PR expected final snapshot fingerprint is invalid")
    if not isinstance(item_raw, Mapping) or not all(
        isinstance(key, str) and isinstance(value, Mapping)
        for key, value in item_raw.items()
    ):
        raise ValueError("multi-PR item proposals must be a mapping")
    item_proposals = {
        key: ProposalRevision.from_data(value) for key, value in item_raw.items()
    }
    item_baselines: dict[str, Mapping[str, Any]]
    item_order: list[str]
    if raw.get("schema_version") == _LEGACY_MULTIPR_STRUCTRR_SCHEMA:
        item_baseline_raw = raw.get("item_baselines")
        if not isinstance(item_baseline_raw, Mapping) or not all(
            isinstance(key, str) and isinstance(value, Mapping)
            for key, value in item_baseline_raw.items()
        ):
            raise ValueError("multi-PR item baselines must be a mapping")
        if set(item_raw) != set(item_baseline_raw):
            raise ValueError("multi-PR item proposal and baseline IDs differ")
        item_baselines = {key: dict(value) for key, value in item_baseline_raw.items()}
        item_order = _recover_item_order(
            dict(baseline), item_proposals, item_baselines, dict(expected_final)
        )
    else:
        item_order_raw = raw.get("item_order")
        fingerprints_raw = raw.get("item_baseline_fingerprints")
        if (
            not isinstance(item_order_raw, list)
            or not all(isinstance(item_id, str) for item_id in item_order_raw)
            or len(item_order_raw) != len(set(item_order_raw))
            or set(item_order_raw) != set(item_proposals)
            or not isinstance(fingerprints_raw, Mapping)
            or not all(isinstance(key, str) for key in fingerprints_raw)
            or set(fingerprints_raw) != set(item_proposals)
            or not all(isinstance(value, str) for value in fingerprints_raw.values())
        ):
            raise ValueError("multi-PR item baseline references are invalid")
        item_order = cast(list[str], item_order_raw)
        fingerprints = cast(Mapping[str, Any], fingerprints_raw)
        item_baselines = _reconstruct_item_baselines(
            dict(baseline), item_order, item_proposals, fingerprints
        )
    proposal = MultiPRStructrrProposal(
        plan_id=_required_string(raw, "plan_id"),
        baseline_snapshot=dict(baseline),
        full_proposal=ProposalRevision.from_data(full_raw),
        item_proposals=item_proposals,
        item_order=tuple(item_order),
        item_baselines=item_baselines,
        expected_final_snapshot=dict(expected_final),
    )
    if proposal.full_proposal.structrr_baseline_fingerprint != snapshot_digest(
        proposal.baseline_snapshot
    ):
        raise ValueError("full proposal baseline does not match the saved bootstrap")
    for item_id, item_proposal in proposal.item_proposals.items():
        if item_proposal.structrr_baseline_fingerprint != snapshot_digest(
            proposal.item_baselines[item_id]
        ):
            raise ValueError(
                f"item {item_id!r} proposal does not match its projected baseline"
            )
    if raw.get("schema_version") == MULTIPR_STRUCTRR_SCHEMA:
        final_item_baseline = proposal.baseline_snapshot
        for item_id in proposal.item_order:
            final_item_baseline = project_structural_operations(
                final_item_baseline,
                _proposal_structural_operations(proposal.item_proposals[item_id]),
            )
        if snapshot_digest(final_item_baseline) != snapshot_digest(
            proposal.expected_final_snapshot
        ):
            raise ValueError(
                "multi-PR item proposals do not produce the saved final snapshot"
            )
    return proposal


def _recover_item_order(
    baseline: Mapping[str, Any],
    item_proposals: Mapping[str, ProposalRevision],
    item_baselines: Mapping[str, Mapping[str, Any]],
    expected_final: Mapping[str, Any],
) -> list[str]:
    """Recover the original sequence from legacy snapshots serialized by key."""
    projected = dict(baseline)
    item_ids_by_fingerprint: dict[str, list[str]] = {}
    for item_id, snapshot in item_baselines.items():
        item_ids_by_fingerprint.setdefault(snapshot_digest(snapshot), []).append(
            item_id
        )
    for item_ids in item_ids_by_fingerprint.values():
        item_ids.sort()
    ordered: list[str] = []
    while len(ordered) < len(item_proposals):
        projected_fingerprint = snapshot_digest(projected)
        matching = item_ids_by_fingerprint.get(projected_fingerprint)
        if not matching:
            raise ValueError("legacy multi-PR item baselines have no valid order")
        item_id = matching.pop(0)
        ordered.append(item_id)
        projected = project_structural_operations(
            projected, _proposal_structural_operations(item_proposals[item_id])
        )
    if snapshot_digest(projected) != snapshot_digest(expected_final):
        raise ValueError(
            "legacy multi-PR item order does not produce the saved final snapshot"
        )
    return ordered


def _reconstruct_item_baselines(
    baseline: Mapping[str, Any],
    item_order: Sequence[str],
    item_proposals: Mapping[str, ProposalRevision],
    fingerprints: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Rebuild per-item baselines from compact fingerprints and prior diffs."""
    projected = dict(baseline)
    item_baselines: dict[str, Mapping[str, Any]] = {}
    for item_id in item_order:
        if fingerprints.get(item_id) != snapshot_digest(projected):
            raise ValueError(
                f"item {item_id!r} baseline fingerprint does not match its projection"
            )
        item_baselines[item_id] = dict(projected)
        projected = project_structural_operations(
            projected, _proposal_structural_operations(item_proposals[item_id])
        )
    return item_baselines


def _proposal_structural_operations(
    proposal: ProposalRevision,
) -> list[dict[str, Any]]:
    action_map = {"add": "added", "change": "changed", "remove": "removed"}
    operations: list[dict[str, Any]] = []
    for operation in proposal.operations:
        action = action_map.get(operation.action)
        if action is None:
            raise ValueError(
                f"unsupported proposal operation action: {operation.action!r}"
            )
        subject_id = operation.subject_id
        if operation.section == "files":
            stable_key = operation.content.get("stable_key")
            if isinstance(stable_key, str) and stable_key:
                subject_id = stable_key
        operations.append(
            {
                "operation_id": operation.operation_id,
                "section": operation.section,
                "subject_id": subject_id,
                "action": action,
                "content": dict(operation.content),
            }
        )
    return operations


def rebase_item_proposal(
    full_baseline: Mapping[str, Any],
    current_baseline: Mapping[str, Any],
    proposal: ProposalRevision,
) -> dict[str, Any]:
    """Check whether intervening repository changes touch this PR's subjects."""
    if proposal.structrr_baseline_fingerprint != snapshot_digest(full_baseline):
        raise ValueError("item proposal does not match its projected baseline")
    entity_refs, source_subject_refs = _proposal_references(proposal)
    report = rebase_structrr_snapshot(
        full_baseline,
        current_baseline,
        referenced_entity_ids=entity_refs,
        referenced_source_subject_keys=source_subject_refs,
        referenced_story_subject_ids=[
            operation.subject_id
            for operation in proposal.operations
            if operation.section in STORY_SECTIONS
        ],
    )
    return report.to_data()


def rebase_final_plan_state(
    expected_final_snapshot: Mapping[str, Any],
    current_snapshot: Mapping[str, Any],
    proposal: ProposalRevision,
) -> dict[str, Any]:
    """Detect interlaced changes to subjects owned by the completed plan."""
    entity_refs, source_subject_refs = _proposal_references(proposal)
    report = rebase_structrr_snapshot(
        expected_final_snapshot,
        current_snapshot,
        referenced_entity_ids=entity_refs,
        referenced_source_subject_keys=source_subject_refs,
        referenced_story_subject_ids=[
            operation.subject_id
            for operation in proposal.operations
            if operation.section in STORY_SECTIONS
        ],
    )
    return report.to_data()


def _proposal_references(proposal: ProposalRevision) -> tuple[list[str], list[str]]:
    entity_refs = [
        operation.subject_id
        for operation in proposal.operations
        if operation.section == "entities"
    ]
    entity_refs.extend(
        str(operation.content[key])
        for operation in proposal.operations
        if operation.section == "entity_relationships"
        for key in ("source", "target")
        if isinstance(operation.content.get(key), str)
    )
    source_subject_refs = [
        operation.subject_id
        for operation in proposal.operations
        if operation.section == "files"
    ]
    return entity_refs, source_subject_refs


def reconcile_item_candidate(
    *,
    proposal: ProposalRevision,
    baseline_snapshot: Mapping[str, Any],
    candidate_snapshot: Mapping[str, Any],
    baseline_manifest: SourceManifest,
    candidate_manifest: SourceManifest,
) -> tuple[StructrrActualDiff, dict[str, Any]]:
    """Re-bootstrap comparison result for one PR's candidate implementation."""
    actual = compile_actual_structrr_diff(
        baseline_snapshot,
        candidate_snapshot,
        baseline_manifest,
        candidate_manifest,
    )
    comparison = compare_candidate_snapshot(
        proposal,
        baseline_snapshot,
        candidate_snapshot,
        extraction_complete=actual.extraction_complete,
        actual_diff=actual,
    )
    return actual, comparison


def structural_repair_findings(comparison: Mapping[str, Any]) -> tuple[str, ...]:
    """Return concise, deterministic gaps for a tailored coding repair prompt."""
    findings = comparison.get("findings")
    if not isinstance(findings, list):
        return ("Structrr comparison did not produce findings.",)
    blockers = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            blockers.append("Malformed Structrr comparison finding.")
            continue
        status = finding.get("status")
        if status in {"missing", "contradictory", "not_evaluable"} or (
            status == "unexpected"
            and finding.get("review_classification")
            not in {"implementation_detail", "validation_change"}
        ):
            blockers.append(
                f"{finding.get('finding_id', 'unknown')}: {status}; "
                f"{finding.get('reason', 'no reason supplied')}"
            )
    return tuple(blockers)


def active_decision_drift_findings(
    baseline_snapshot: Mapping[str, Any], candidate_snapshot: Mapping[str, Any]
) -> tuple[str, ...]:
    """Find removals or edits to accepted decisions and invariants.

    Newly added active clauses are allowed and carried into the worker context.
    Existing records are keyed by stable IDs; an edit or removal requires an
    explicit plan revision before implementation may proceed.
    """
    findings: list[str] = []
    for section, identity_key in (
        ("active_intent", "clause_id"),
        ("human-decisions", "id"),
        ("invariants", "id"),
    ):
        before = _records_by_identity(baseline_snapshot.get(section), identity_key)
        after = _records_by_identity(candidate_snapshot.get(section), identity_key)
        for identity in sorted(before):
            if identity not in after:
                findings.append(f"accepted {section} record {identity!r} was removed")
            elif _json_fingerprint(before[identity]) != _json_fingerprint(
                after[identity]
            ):
                findings.append(f"accepted {section} record {identity!r} was changed")
    return tuple(findings)


def inherited_decision_records(
    baseline_snapshot: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Return the pre-existing decisions and invariants a PR must carry forward.

    The baseline is the Structrr state at this PR's execution point, including
    earlier accepted plan diffs. Feature intent is intentionally excluded: it
    lives in the current proposal overlay, not in this baseline inventory.
    """
    records: list[dict[str, Any]] = []
    active_intent = _records_by_identity(
        baseline_snapshot.get("active_intent"), "clause_id"
    )
    if active_intent:
        for identity, record in sorted(active_intent.items()):
            if record.get("active", True) is False:
                continue
            records.append(
                {
                    "decision_id": f"active_intent:{identity}",
                    "section": "active_intent",
                    "record_id": identity,
                    "record": record,
                }
            )
    else:
        # Older Structrr snapshots may predate active_intent. Read their source
        # sections without duplicating records once the canonical section exists.
        for section in ("invariants", "guidance"):
            for identity, record in sorted(
                _records_by_identity(baseline_snapshot.get(section), "id").items()
            ):
                if record.get("state") in {"removed", "retired", "superseded"}:
                    continue
                records.append(
                    {
                        "decision_id": f"{section}:{identity}",
                        "section": section,
                        "record_id": identity,
                        "record": record,
                    }
                )
    for identity, record in sorted(
        _records_by_identity(baseline_snapshot.get("human-decisions"), "id").items()
    ):
        records.append(
            {
                "decision_id": f"human-decisions:{identity}",
                "section": "human-decisions",
                "record_id": identity,
                "record": record,
            }
        )
    return tuple(records)


def write_multi_pr_proposal(
    path: str | Path, proposal: MultiPRStructrrProposal
) -> None:
    """Persist the immutable proposal in canonical JSON form."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(proposal.to_data(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _apply_sections(
    baseline: Mapping[str, Any], sections: Mapping[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Use the shared desired-state transition semantics."""
    return project_sections(baseline, sections)


def project_structural_operations(
    baseline_snapshot: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply one newly completed item batch to an already projected snapshot."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_operation_ids: set[str] = set()
    for index, operation in enumerate(operations, start=1):
        operation_id = operation.get("operation_id")
        section = operation.get("section")
        subject_id = operation.get("subject_id")
        action = operation.get("action")
        content = operation.get("content")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (operation_id, section, subject_id, action)
        ):
            raise ValueError(f"Structrr operation {index} has incomplete identity")
        assert isinstance(operation_id, str)
        assert isinstance(section, str)
        assert isinstance(subject_id, str)
        assert isinstance(action, str)
        if operation_id in seen_operation_ids:
            raise ValueError(f"duplicate Structrr operation id: {operation_id}")
        seen_operation_ids.add(operation_id)
        if section not in _STRUCTURAL_SECTIONS or action not in _ACTIONS:
            raise ValueError(f"Structrr operation {operation_id!r} is unsupported")
        if not isinstance(content, Mapping):
            raise ValueError(f"Structrr operation {operation_id!r} has no content")
        identity_key = "stable_key" if section == "files" else "id"
        if content.get(identity_key, subject_id) != subject_id:
            raise ValueError(
                f"Structrr operation {operation_id!r} content identity differs "
                "from subject_id"
            )
        grouped.setdefault(section, []).append(
            {
                "id": subject_id,
                "action": action,
                "content": {**dict(content), "id": subject_id},
            }
            if section in STORY_SECTIONS
            else {**dict(content), identity_key: subject_id, "action": action}
        )
    return _apply_sections(baseline_snapshot, grouped)


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"multi-PR Structrr proposal {key} must be non-empty text")
    return value


def _records_by_identity(value: Any, identity_key: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return {}
    return {
        str(record[identity_key]): dict(record)
        for record in value
        if isinstance(record, Mapping)
        and isinstance(record.get(identity_key), str)
        and record[identity_key]
    }


def _json_fingerprint(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_fingerprint(value: Mapping[str, Any]) -> str:
    return (
        "sha256:" + hashlib.sha256(_json_fingerprint(value).encode("utf-8")).hexdigest()
    )


__all__ = [
    "MULTIPR_STRUCTRR_SCHEMA",
    "MultiPRStructrrProposal",
    "active_decision_drift_findings",
    "inherited_decision_records",
    "compile_multi_pr_proposal",
    "project_structural_operations",
    "load_multi_pr_proposal",
    "rebase_item_proposal",
    "rebase_final_plan_state",
    "reconcile_item_candidate",
    "structural_repair_findings",
    "write_multi_pr_proposal",
]
