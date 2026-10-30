"""Deterministic, atomic repair operations for an unpublished planning draft."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from powdrr_service.planning.plan_models import PlannedWorkItem, ReviewedExecutionPlan
from powdrr_service.planning.workflow_ids import slugify_workflow_id

_REPAIR_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUBJECT_KINDS = frozenset(
    {"requirement", "unit", "validation", "setup", "structrr_subject", "item"}
)
_OPERATION_KINDS = frozenset(
    {
        "add_item",
        "update_item",
        "retire_item",
        "set_dependency",
        "update_requirement",
        "assign_setup",
    }
)
_DISCOVERY_KINDS = frozenset(
    {
        "source_change",
        "existing_implementation",
        "scope_defect",
        "dependency_defect",
        "human_setup_required",
        "structrr_conflict",
        "external_constraint",
    }
)


class PlanRepairError(ValueError):
    """A discovery, patch, or candidate draft is invalid."""


@dataclass(frozen=True, slots=True)
class RepairDraft:
    """An unpublished plan revision and the exact artifacts compiled from it."""

    plan: ReviewedExecutionPlan
    manifest: Mapping[str, Any]
    artifacts: Mapping[str, str]
    fulfilled_item_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RepairSubject:
    subject_kind: str
    subject_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PlanDiscovery:
    """One semantic repair trigger pinned to a plan revision and repository base."""

    id: str
    kind: str
    summary: str
    requirement_ids: tuple[str, ...]
    item_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    requested_outcome: str
    base_plan_digest: str
    base_commit: str

    @classmethod
    def from_data(cls, value: Mapping[str, Any]) -> PlanDiscovery:
        expected = {
            "id",
            "kind",
            "summary",
            "requirement_ids",
            "item_ids",
            "evidence_ids",
            "requested_outcome",
            "base_plan_digest",
            "base_commit",
        }
        if set(value) != expected:
            raise PlanRepairError("discovery fields are invalid")
        identifier, kind = value.get("id"), value.get("kind")
        summary, outcome = value.get("summary"), value.get("requested_outcome")
        base_digest, base_commit = (
            value.get("base_plan_digest"),
            value.get("base_commit"),
        )
        if not isinstance(identifier, str) or not _REPAIR_ID.fullmatch(identifier):
            raise PlanRepairError("discovery ID is invalid")
        if not isinstance(kind, str) or kind not in _DISCOVERY_KINDS:
            raise PlanRepairError("discovery kind is invalid")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            raise PlanRepairError("discovery summary is invalid")
        if not isinstance(outcome, str) or not outcome.strip() or len(outcome) > 2000:
            raise PlanRepairError("discovery requested outcome is invalid")
        if not _valid_digest(base_digest):
            raise PlanRepairError("discovery base plan digest is invalid")
        if not isinstance(base_commit, str) or not re.fullmatch(
            r"[0-9a-f]{40,64}", base_commit
        ):
            raise PlanRepairError("discovery base commit is invalid")
        return cls(
            identifier,
            kind,
            summary,
            _unique_texts(
                value.get("requirement_ids"), "requirement_ids", allow_empty=True
            ),
            _unique_texts(value.get("item_ids"), "item_ids", allow_empty=True),
            _unique_texts(value.get("evidence_ids"), "evidence_ids", allow_empty=True),
            outcome,
            str(base_digest),
            base_commit,
        )


@dataclass(frozen=True, slots=True)
class RepairImpact:
    subject_ids: tuple[tuple[str, str], ...]
    item_ids: tuple[str, ...]
    artifact_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanRepairPatch:
    """Code-owned patch envelope with compare-and-swap digests."""

    repair_id: str
    base_plan_digest: str
    discovery_ids: tuple[str, ...]
    affected_item_ids: tuple[str, ...]
    operations: tuple[Mapping[str, Any], ...]
    expected_result_digest: str

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": "plan-repair-patch-v1",
            "repair_id": self.repair_id,
            "base_plan_digest": self.base_plan_digest,
            "discovery_ids": list(self.discovery_ids),
            "affected_item_ids": list(self.affected_item_ids),
            "operations": [dict(operation) for operation in self.operations],
            "expected_result_digest": self.expected_result_digest,
        }

    @classmethod
    def from_data(cls, value: Mapping[str, Any]) -> PlanRepairPatch:
        expected = {
            "schema",
            "repair_id",
            "base_plan_digest",
            "discovery_ids",
            "affected_item_ids",
            "operations",
            "expected_result_digest",
        }
        if set(value) != expected or value.get("schema") != "plan-repair-patch-v1":
            raise PlanRepairError("repair patch envelope is malformed")
        repair_id = value.get("repair_id")
        base_digest = value.get("base_plan_digest")
        expected_digest = value.get("expected_result_digest")
        if not isinstance(repair_id, str) or not _REPAIR_ID.fullmatch(repair_id):
            raise PlanRepairError("repair_id is invalid")
        if not _valid_digest(base_digest) or not _valid_digest(expected_digest):
            raise PlanRepairError("repair patch digest is invalid")
        assert isinstance(base_digest, str)
        assert isinstance(expected_digest, str)
        discoveries = _unique_texts(value.get("discovery_ids"), "discovery_ids")
        affected = _unique_texts(value.get("affected_item_ids"), "affected_item_ids")
        raw_operations = value.get("operations")
        if not isinstance(raw_operations, list) or not raw_operations:
            raise PlanRepairError("repair patch requires operations")
        operations = tuple(_validate_operation(item) for item in raw_operations)
        return cls(
            repair_id,
            base_digest,
            discoveries,
            affected,
            operations,
            expected_digest,
        )


def item_content_digest(item: PlannedWorkItem) -> str:
    """Hash the stable semantic fields of one plan item, excluding issue state."""
    value = {
        "item_id": item.item_id,
        "title": item.title,
        "objective": item.objective,
        "scope": list(item.scope),
        "acceptance_criteria": list(item.acceptance_criteria),
        "dependencies": list(item.dependencies),
        "workflow_id": item.workflow_id,
        "workflow_file": item.workflow_file,
        "implementation_steps": list(item.implementation_steps),
        "allowed_paths": list(item.allowed_paths),
        "manual_prerequisite": item.manual_prerequisite,
        "inline_adjustments": list(item.inline_adjustments),
    }
    return _digest(value)


def item_input_contract_digest(
    item: PlannedWorkItem, manifest: Mapping[str, Any] | None
) -> str:
    """Hash the plan fields and validation/setup records consumed by one item."""
    data = manifest if isinstance(manifest, Mapping) else {}
    item_units = data.get("item_unit_ids", {})
    unit_ids = (
        item_units.get(item.item_id, []) if isinstance(item_units, Mapping) else []
    )
    unit_records = data.get("units", [])
    units = (
        [
            record
            for record in unit_records
            if isinstance(record, Mapping) and record.get("id") in unit_ids
        ]
        if isinstance(unit_records, list)
        else []
    )
    validation_ids = {
        validation_id
        for unit in units
        for validation_id in (
            unit.get("validation_ids", []) if isinstance(unit, Mapping) else []
        )
        if isinstance(validation_id, str)
    }
    setup_records = data.get("setups", [])
    setups = (
        [
            setup
            for setup in setup_records
            if isinstance(setup, Mapping) and setup.get("owner_item_id") == item.item_id
        ]
        if isinstance(setup_records, list)
        else []
    )
    validation_ids.update(
        validation_id
        for setup in setups
        for validation_id in setup.get("unblocks_validation_ids", [])
        if isinstance(validation_id, str)
    )
    validations = data.get("validations", [])
    relevant_validations = (
        [
            validation
            for validation in validations
            if isinstance(validation, Mapping)
            and validation.get("id") in validation_ids
        ]
        if isinstance(validations, list)
        else []
    )
    coverage = data.get("validation_coverage", [])
    item_coverage = (
        [
            record
            for record in coverage
            if isinstance(record, Mapping) and record.get("item_id") == item.item_id
        ]
        if isinstance(coverage, list)
        else []
    )
    return _digest(
        {
            "item_content_digest": item_content_digest(item),
            "units": units,
            "setups": setups,
            "validations": relevant_validations,
            "validation_coverage": item_coverage,
        }
    )


def plan_revision_digest(draft: RepairDraft) -> str:
    """Compute a canonical CAS identity for the plan and semantic manifest."""
    manifest_digest = _manifest_semantic_digest(draft.manifest)
    return _digest(
        {
            "plan_id": draft.plan.plan_id,
            "source_document": draft.plan.source_document,
            "items": [item_content_digest(item) for item in draft.plan.items],
            "planning_semantic_digest": manifest_digest,
        }
    )


def expand_repair_impact(
    draft: RepairDraft,
    declared_subjects: Sequence[RepairSubject | Mapping[str, Any]],
    *,
    shared_structrr_subjects: Mapping[str, Sequence[str]] | None = None,
) -> RepairImpact:
    """Validate proposed subjects and close their impact over concrete references."""
    subjects = tuple(_repair_subject(value) for value in declared_subjects)
    if not subjects:
        raise PlanRepairError("repair impact must name at least one subject")
    requirements = _record_ids(draft.manifest, "requirements", "id")
    units = _record_map(draft.manifest, "units", "id")
    validations = _record_ids(draft.manifest, "validations", "id")
    setups = _record_ids(draft.manifest, "setups", "id")
    items = {item.item_id: item for item in draft.plan.items}
    allocations = draft.manifest.get("item_unit_ids")
    if not isinstance(allocations, Mapping):
        raise PlanRepairError("manifest item_unit_ids is malformed")
    unit_to_item = {
        str(unit_id): str(item_id)
        for item_id, refs in allocations.items()
        if isinstance(refs, list | tuple)
        for unit_id in refs
    }
    valid_subjects = {
        "requirement": requirements,
        "unit": set(units),
        "validation": validations,
        "setup": setups,
        "structrr_subject": None,
        "item": set(items),
    }
    item_ids: set[str] = set()
    subject_ids: set[tuple[str, str]] = set()
    for subject in subjects:
        if subject.subject_kind != "structrr_subject":
            valid_ids = valid_subjects[subject.subject_kind]
            if valid_ids is None or subject.subject_id not in valid_ids:
                raise PlanRepairError(
                    f"unknown {subject.subject_kind} {subject.subject_id!r}"
                )
        subject_ids.add((subject.subject_kind, subject.subject_id))
        if subject.subject_kind == "item":
            item_ids.add(subject.subject_id)
        elif subject.subject_kind == "unit":
            if subject.subject_id in unit_to_item:
                item_ids.add(unit_to_item[subject.subject_id])
        elif subject.subject_kind == "requirement":
            for unit_id, unit in units.items():
                if subject.subject_id in _string_values(unit.get("requirement_ids")):
                    if unit_id in unit_to_item:
                        item_ids.add(unit_to_item[unit_id])
        elif subject.subject_kind == "validation":
            for unit_id, unit in units.items():
                if subject.subject_id in _string_values(unit.get("validation_ids")):
                    if unit_id in unit_to_item:
                        item_ids.add(unit_to_item[unit_id])
        elif subject.subject_kind == "setup":
            setup = next(
                record
                for record in _records(draft.manifest, "setups")
                if record.get("id") == subject.subject_id
            )
            owner = setup.get("owner_item_id")
            if isinstance(owner, str):
                item_ids.add(owner)
            for validation_id in _string_values(
                setup.get("unblocks_validation_ids", []), allow_empty=True
            ):
                for unit_id, unit in units.items():
                    if validation_id in _string_values(unit.get("validation_ids")):
                        if unit_id in unit_to_item:
                            item_ids.add(unit_to_item[unit_id])
        elif subject.subject_kind == "structrr_subject":
            linked = (shared_structrr_subjects or {}).get(subject.subject_id)
            if linked is None:
                raise PlanRepairError(
                    f"structrr subject {subject.subject_id!r} has no impact catalog"
                )
            item_ids.update(linked)

    # Dependency edges are concrete references: include the connected component.
    adjacency: dict[str, set[str]] = defaultdict(set)
    for item in draft.plan.items:
        for dependency in item.dependencies:
            adjacency[item.item_id].add(dependency)
            adjacency[dependency].add(item.item_id)
    pending = deque(item_ids)
    while pending:
        item_id = pending.popleft()
        if item_id not in items:
            raise PlanRepairError(f"impact references unknown item {item_id!r}")
        for related in adjacency[item_id] - item_ids:
            item_ids.add(related)
            pending.append(related)

    declared_item_ids = set(items)
    if item_ids - declared_item_ids:
        raise PlanRepairError("expanded impact contains an unknown plan item")
    paths = {path for path in draft.artifacts if path.endswith("-execution-plan.yaml")}
    structrr = draft.manifest.get("structrr")
    if isinstance(structrr, Mapping) and isinstance(structrr.get("proposal_path"), str):
        paths.add(str(structrr["proposal_path"]))
    for item_id in item_ids:
        workflow = items[item_id].workflow_file
        if workflow:
            paths.add(workflow)
    return RepairImpact(
        tuple(sorted(subject_ids)),
        tuple(sorted(item_ids)),
        tuple(sorted(path for path in paths if path)),
    )


def build_plan_repair_patch(
    draft: RepairDraft,
    *,
    repair_id: str,
    discovery_ids: Sequence[str | PlanDiscovery],
    affected_item_ids: Sequence[str],
    operations: Sequence[Mapping[str, Any]],
    replacement_items: Mapping[str, PlannedWorkItem] | None = None,
    replacement_units: Sequence[Mapping[str, Any]] = (),
    replacement_requirements: Mapping[str, Mapping[str, Any]] | None = None,
) -> PlanRepairPatch:
    """Create a model-independent patch envelope and result CAS digest."""
    if not _REPAIR_ID.fullmatch(repair_id):
        raise PlanRepairError("repair_id is invalid")
    discovery_records = tuple(
        item if isinstance(item, PlanDiscovery) else None for item in discovery_ids
    )
    for discovery in discovery_records:
        if discovery is None:
            continue
        if discovery.base_plan_digest != plan_revision_digest(draft):
            raise PlanRepairError("discovery is pinned to a different plan revision")
        base = draft.manifest.get("base")
        if isinstance(base, Mapping) and discovery.base_commit != base.get(
            "commit_sha"
        ):
            raise PlanRepairError("discovery is pinned to a different repository base")
        if set(discovery.requirement_ids) - _record_ids(
            draft.manifest, "requirements", "id"
        ):
            raise PlanRepairError("discovery references an unknown requirement")
        if set(discovery.item_ids) - {item.item_id for item in draft.plan.items}:
            raise PlanRepairError("discovery references an unknown item")
        if set(discovery.evidence_ids) - _record_ids(draft.manifest, "evidence", "id"):
            raise PlanRepairError("discovery references unknown evidence")
    discoveries = _unique_texts(
        [
            item.id if isinstance(item, PlanDiscovery) else item
            for item in discovery_ids
        ],
        "discovery_ids",
    )
    validated_operations = tuple(_validate_operation(item) for item in operations)
    if not validated_operations:
        raise PlanRepairError("repair patch requires operations")
    affected_set = set(_unique_texts(list(affected_item_ids), "affected_item_ids"))
    affected_set.update(
        allocated_repair_item_id(
            draft.plan.plan_id, repair_id, str(operation["new_item_ref"])
        )
        for operation in validated_operations
        if operation["operation"] == "add_item"
    )
    affected = tuple(sorted(affected_set))
    candidate_plan, candidate_manifest, _retired = _apply_operations(
        draft,
        repair_id=repair_id,
        affected_item_ids=set(affected),
        operations=validated_operations,
        replacement_items=replacement_items or {},
        replacement_units=replacement_units,
        replacement_requirements=replacement_requirements or {},
    )
    candidate = RepairDraft(candidate_plan, candidate_manifest, draft.artifacts)
    return PlanRepairPatch(
        repair_id,
        plan_revision_digest(draft),
        discoveries,
        affected,
        validated_operations,
        plan_revision_digest(candidate),
    )


def apply_plan_repair_patch(
    draft: RepairDraft,
    patch: PlanRepairPatch | Mapping[str, Any],
    *,
    replacement_items: Mapping[str, PlannedWorkItem] | None = None,
    replacement_units: Sequence[Mapping[str, Any]] = (),
    replacement_requirements: Mapping[str, Mapping[str, Any]] | None = None,
    compile_candidate: Callable[
        [ReviewedExecutionPlan, Mapping[str, Any]], Mapping[str, str]
    ],
) -> RepairDraft:
    """Apply all operations to copies, validate, then return one compiled draft."""
    resolved = PlanRepairPatch.from_data(
        patch.to_data() if isinstance(patch, PlanRepairPatch) else patch
    )
    if resolved.base_plan_digest != plan_revision_digest(draft):
        raise PlanRepairError("repair patch base digest is stale")
    candidate_plan, candidate_manifest, retired = _apply_operations(
        draft,
        repair_id=resolved.repair_id,
        affected_item_ids=set(resolved.affected_item_ids),
        operations=resolved.operations,
        replacement_items=replacement_items or {},
        replacement_units=replacement_units,
        replacement_requirements=replacement_requirements or {},
    )
    candidate_without_repair = RepairDraft(
        candidate_plan, candidate_manifest, draft.artifacts
    )
    actual_result_digest = plan_revision_digest(candidate_without_repair)
    if actual_result_digest != resolved.expected_result_digest:
        raise PlanRepairError("repair patch result digest does not match")

    old_item_digests = {
        item.item_id: item_content_digest(item) for item in draft.plan.items
    }
    new_item_digests = {
        item.item_id: item_content_digest(item) for item in candidate_plan.items
    }
    changed = {
        item_id
        for item_id in old_item_digests.keys() | new_item_digests.keys()
        if old_item_digests.get(item_id) != new_item_digests.get(item_id)
    }
    if changed - set(resolved.affected_item_ids):
        raise PlanRepairError("patch changes an item outside its declared impact")
    old_input_contract_digests = {
        item.item_id: item_input_contract_digest(item, draft.manifest)
        for item in draft.plan.items
    }
    new_input_contract_digests = {
        item.item_id: item_input_contract_digest(item, candidate_manifest)
        for item in candidate_plan.items
    }
    changed_input_contracts = {
        item_id
        for item_id in old_input_contract_digests.keys()
        | new_input_contract_digests.keys()
        if old_input_contract_digests.get(item_id)
        != new_input_contract_digests.get(item_id)
    }
    if changed_input_contracts - set(resolved.affected_item_ids):
        raise PlanRepairError(
            "patch changes an item input contract outside its declared impact"
        )
    prior_repair = draft.manifest.get("latest_repair")
    repair_record = {
        "repair_id": resolved.repair_id,
        "discovery_ids": list(resolved.discovery_ids),
        "old_semantic_revision_digest": _manifest_semantic_digest(draft.manifest),
        "new_semantic_revision_digest": _manifest_semantic_digest(candidate_manifest),
        "old_plan_digest": resolved.base_plan_digest,
        "new_plan_digest": actual_result_digest,
        "affected_item_ids": list(resolved.affected_item_ids),
        "old_item_digests": {
            item_id: old_item_digests[item_id]
            for item_id in sorted(set(old_item_digests) & changed)
        },
        "new_item_digests": {
            item_id: new_item_digests[item_id]
            for item_id in sorted(set(new_item_digests) & changed)
        },
        "old_input_contract_digests": {
            item_id: old_input_contract_digests[item_id]
            for item_id in sorted(
                set(old_input_contract_digests) & changed_input_contracts
            )
        },
        "new_input_contract_digests": {
            item_id: new_input_contract_digests[item_id]
            for item_id in sorted(
                set(new_input_contract_digests) & changed_input_contracts
            )
        },
        "retired_items": retired,
    }
    if prior_repair is not None:
        repair_record["previous_repair"] = prior_repair
    candidate_manifest = _set_latest_repair(candidate_manifest, repair_record)
    candidate_plan = replace(candidate_plan, planning=candidate_manifest)
    artifacts = dict(compile_candidate(candidate_plan, candidate_manifest))
    _validate_artifact_preservation(
        draft, candidate_plan, candidate_manifest, artifacts, changed
    )
    return RepairDraft(
        candidate_plan,
        candidate_manifest,
        artifacts,
        draft.fulfilled_item_ids,
    )


def _apply_operations(
    draft: RepairDraft,
    *,
    repair_id: str,
    affected_item_ids: set[str],
    operations: Sequence[Mapping[str, Any]],
    replacement_items: Mapping[str, PlannedWorkItem],
    replacement_units: Sequence[Mapping[str, Any]],
    replacement_requirements: Mapping[str, Mapping[str, Any]],
) -> tuple[ReviewedExecutionPlan, Mapping[str, Any], list[dict[str, Any]]]:
    items = {item.item_id: item for item in draft.plan.items}
    original_items = dict(items)
    manifest = _copy_json(draft.manifest)
    allocations = manifest.get("item_unit_ids")
    if not isinstance(allocations, dict):
        raise PlanRepairError("manifest item_unit_ids is malformed")
    allocations = {str(key): list(value) for key, value in allocations.items()}
    unit_records = _record_map(manifest, "units", "id")
    for unit in replacement_units:
        unit_id = unit.get("id")
        if not isinstance(unit_id, str) or not unit_id:
            raise PlanRepairError("replacement work unit has no ID")
        unit_records[unit_id] = _copy_json(unit)
    manifest["units"] = list(unit_records.values())
    requirements = _record_map(manifest, "requirements", "id")
    retired_records: list[dict[str, Any]] = []

    for operation in operations:
        kind = operation["operation"]
        if kind in {"update_item", "retire_item"}:
            item_id = str(operation["item_id"])
            _require_affected(item_id, affected_item_ids)
            item = items.get(item_id)
            if item is None:
                raise PlanRepairError(f"unknown item {item_id!r}")
            if operation["expected_digest"] != item_content_digest(item):
                raise PlanRepairError(f"item {item_id!r} digest is stale")
        if kind == "update_item":
            original_item = items.get(item_id)
            if original_item is None:
                raise PlanRepairError(f"unknown item {item_id!r}")
            replacement = replacement_items.get(item_id)
            if replacement is None or replacement.item_id != item_id:
                raise PlanRepairError("updated item must preserve its item ID")
            if (
                replacement.workflow_id != original_item.workflow_id
                or replacement.workflow_file != original_item.workflow_file
            ):
                raise PlanRepairError(
                    "updated item must preserve its workflow identity"
                )
            if item_id in draft.fulfilled_item_ids:
                raise PlanRepairError("fulfilled items cannot be rewritten")
            new_unit_ids = _string_values(operation["replacement_unit_ids"])
            _ensure_units_exist(new_unit_ids, unit_records)
            old_requirements = _requirements_for_item(
                item_id, allocations, unit_records
            )
            new_requirements = {
                requirement_id
                for unit_id in new_unit_ids
                for requirement_id in _string_values(
                    unit_records[unit_id].get("requirement_ids")
                )
            }
            complete = _complete_requirement_ids(manifest)
            if old_requirements - new_requirements - complete:
                raise PlanRepairError(
                    "updated item lost unresolved requirement allocation"
                )
            items[item_id] = replace(
                replacement, github_issue_number=original_item.github_issue_number
            )
            allocations[item_id] = list(new_unit_ids)
        elif kind == "add_item":
            new_ref = str(operation["new_item_ref"])
            template = replacement_items.get(new_ref)
            if template is None:
                raise PlanRepairError(f"missing accepted replacement item {new_ref!r}")
            item_id = allocated_repair_item_id(draft.plan.plan_id, repair_id, new_ref)
            if item_id in items:
                raise PlanRepairError(f"allocated item ID already exists: {item_id}")
            workflow_id = slugify_workflow_id(f"{draft.plan.plan_id}-{item_id}")
            workflow_file = (
                f"docs/workflows/{slugify_workflow_id(draft.plan.plan_id)}/"
                f"{workflow_id}.yaml"
            )
            added = replace(
                template,
                item_id=item_id,
                workflow_id=workflow_id,
                workflow_file=workflow_file,
                github_issue_number=None,
            )
            unit_ids = _string_values(operation["unit_ids"])
            _ensure_units_exist(unit_ids, unit_records)
            unit_requirements = {
                requirement_id
                for unit_id in unit_ids
                for requirement_id in _string_values(
                    unit_records[unit_id].get("requirement_ids")
                )
            }
            if set(operation["requirement_ids"]) != unit_requirements:
                raise PlanRepairError(
                    "new item requirement allocation differs from its units"
                )
            if not unit_requirements <= set(requirements):
                raise PlanRepairError("new item references an unknown requirement")
            items[item_id] = added
            allocations[item_id] = list(unit_ids)
        elif kind == "retire_item":
            disposition = str(operation["disposition"])
            replacements = _string_values(
                operation["replacement_item_ids"], allow_empty=True
            )
            if item_id in draft.fulfilled_item_ids and disposition != "fulfilled":
                raise PlanRepairError("fulfilled items cannot be superseded")
            if disposition == "superseded" and not replacements:
                raise PlanRepairError("superseded items require successors")
            if any(
                successor not in items and successor not in replacement_items
                for successor in replacements
            ):
                raise PlanRepairError("retirement references an unknown successor")
            requirements_from_retired = _requirements_for_item(
                item_id, allocations, unit_records
            )
            successor_requirements = (
                set().union(
                    *(
                        _requirements_for_successor(
                            successor,
                            items,
                            allocations,
                            unit_records,
                            replacement_items,
                        )
                        for successor in replacements
                    )
                )
                if replacements
                else set()
            )
            if (
                disposition == "superseded"
                and not requirements_from_retired <= successor_requirements
            ):
                missing = sorted(requirements_from_retired - successor_requirements)
                raise PlanRepairError(
                    "successors do not preserve retired requirement allocation: "
                    f"{missing}"
                )
            if disposition == "fulfilled":
                if item_id not in draft.fulfilled_item_ids:
                    raise PlanRepairError(
                        "fulfilled disposition lacks durable completion state"
                    )
                evidence_by_id = _record_map(manifest, "evidence", "id")
                evidence_ids = _string_values(operation["evidence_ids"])
                if not evidence_ids or set(evidence_ids) - set(evidence_by_id):
                    raise PlanRepairError(
                        "fulfilled retirement requires registered evidence"
                    )
            retired_records.append(
                {
                    "item_id": item_id,
                    "disposition": disposition,
                    "reason": operation["reason"],
                    "successor_item_ids": replacements,
                    "requirement_ids": sorted(requirements_from_retired),
                }
            )
            if disposition == "superseded":
                del items[item_id]
                allocations.pop(item_id, None)
                for consumer_id, consumer in tuple(items.items()):
                    if item_id in consumer.dependencies:
                        dependency_tuple = tuple(
                            dependency
                            for dependency in consumer.dependencies
                            if dependency != item_id
                        )
                        dependency_tuple = tuple(
                            dict.fromkeys((*dependency_tuple, *replacements))
                        )
                        items[consumer_id] = replace(
                            consumer, dependencies=dependency_tuple
                        )
        elif kind == "set_dependency":
            before = str(operation["before_item_id"])
            after = str(operation["after_item_id"])
            _require_affected(before, affected_item_ids)
            _require_affected(after, affected_item_ids)
            if before not in items or after not in items or before == after:
                raise PlanRepairError("dependency edge endpoints are invalid")
            edge_digest = dependency_edge_digest(items[before], items[after])
            if operation["expected_edge_digest"] != edge_digest:
                raise PlanRepairError("dependency edge digest is stale")
            dependency_list = list(items[after].dependencies)
            if operation["present"]:
                if before not in dependency_list:
                    dependency_list.append(before)
            else:
                dependency_list = [
                    dependency for dependency in dependency_list if dependency != before
                ]
            items[after] = replace(items[after], dependencies=tuple(dependency_list))
        elif kind == "update_requirement":
            requirement_id = str(operation["requirement_id"])
            current = requirements.get(requirement_id)
            if current is None:
                raise PlanRepairError(f"unknown requirement {requirement_id!r}")
            if operation["expected_digest"] != _digest(current):
                raise PlanRepairError("requirement digest is stale")
            impacted_items = {
                owner
                for owner, refs in allocations.items()
                if any(
                    requirement_id
                    in _string_values(unit_records[unit_id].get("requirement_ids"))
                    for unit_id in refs
                    if unit_id in unit_records
                )
            }
            if impacted_items - affected_item_ids:
                raise PlanRepairError(
                    "requirement repair omits an allocated item from impact"
                )
            replacement_ref = str(operation["replacement_ref"])
            requirement_replacement = replacement_requirements.get(replacement_ref)
            if (
                requirement_replacement is None
                or requirement_replacement.get("id") != requirement_id
            ):
                raise PlanRepairError("replacement requirement must preserve its ID")
            _string_values(operation["source_refs"])
            if not set(operation["source_refs"]) <= set(
                requirement_replacement.get("source_refs", [])
            ):
                raise PlanRepairError(
                    "replacement requirement dropped source provenance"
                )
            requirements[requirement_id] = _copy_json(requirement_replacement)
        elif kind == "assign_setup":
            setup_id = str(operation["setup_id"])
            setups = _record_map(manifest, "setups", "id")
            setup = setups.get(setup_id)
            if setup is None:
                raise PlanRepairError(f"unknown setup {setup_id!r}")
            if operation["expected_digest"] != _digest(setup):
                raise PlanRepairError("setup digest is stale")
            owner = str(operation["owner_item_id"])
            validation_ids = _string_values(operation["validation_ids"])
            if owner not in items:
                raise PlanRepairError("setup owner item is unknown")
            previous_owner = setup.get("owner_item_id")
            if isinstance(previous_owner, str):
                _require_affected(previous_owner, affected_item_ids)
            _require_affected(owner, affected_item_ids)
            if set(validation_ids) - _record_ids(manifest, "validations", "id"):
                raise PlanRepairError("setup references an unknown validation")
            setup["owner_item_id"] = owner
            setup["unblocks_validation_ids"] = validation_ids
            manifest["setups"] = list(setups.values())

    # Retired-but-fulfilled records remain available as plan history and keep their
    # requirement/unit assignment. Superseded records are represented in latest_repair.
    manifest["item_unit_ids"] = allocations
    if set(allocations) != set(items) or any(not refs for refs in allocations.values()):
        raise PlanRepairError("every active item must have a work-unit allocation")
    manifest["requirements"] = list(requirements.values())
    assigned_units = {unit_id for refs in allocations.values() for unit_id in refs}
    missing_units = assigned_units - set(unit_records)
    if missing_units:
        raise PlanRepairError(
            f"item allocation references missing units: {sorted(missing_units)}"
        )
    assigned_unit_list = [unit_id for refs in allocations.values() for unit_id in refs]
    if len(assigned_unit_list) != len(set(assigned_unit_list)):
        raise PlanRepairError("work units must be assigned to exactly one item")
    manifest["units"] = [
        record for unit_id, record in unit_records.items() if unit_id in assigned_units
    ]
    _validate_requirement_allocation(manifest)
    ordered_items = _stable_topological_order(items, original_items)
    candidate_plan = replace(draft.plan, items=ordered_items, planning=None)
    manifest = _with_semantic_digest(manifest)
    return candidate_plan, manifest, retired_records


def dependency_edge_digest(producer: PlannedWorkItem, consumer: PlannedWorkItem) -> str:
    """Hash the presence/absence state for one directed dependency edge."""
    return _digest(
        {
            "before_item_id": producer.item_id,
            "after_item_id": consumer.item_id,
            "present": producer.item_id in consumer.dependencies,
        }
    )


def _stable_topological_order(
    items: Mapping[str, PlannedWorkItem], original_items: Mapping[str, PlannedWorkItem]
) -> tuple[PlannedWorkItem, ...]:
    original_order = {item_id: index for index, item_id in enumerate(original_items)}
    indegree = {item_id: 0 for item_id in items}
    children: dict[str, set[str]] = defaultdict(set)
    for item in items.values():
        for dependency in item.dependencies:
            if dependency not in items:
                raise PlanRepairError(
                    f"dependency references unknown item {dependency!r}"
                )
            if dependency == item.item_id:
                raise PlanRepairError("dependency graph contains a self-cycle")
            indegree[item.item_id] += 1
            children[dependency].add(item.item_id)
    ready = [item_id for item_id, degree in indegree.items() if degree == 0]
    ordered: list[str] = []
    while ready:
        ready.sort(key=lambda item_id: (original_order.get(item_id, 10**9), item_id))
        current = ready.pop(0)
        ordered.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(ordered) != len(items):
        raise PlanRepairError("dependency graph contains a cycle")
    return tuple(items[item_id] for item_id in ordered)


def _validate_artifact_preservation(
    draft: RepairDraft,
    candidate_plan: ReviewedExecutionPlan,
    candidate_manifest: Mapping[str, Any],
    artifacts: Mapping[str, str],
    changed_item_ids: set[str],
) -> None:
    old_items = {item.item_id: item for item in draft.plan.items}
    allowed_workflow_paths = {
        item.workflow_file
        for item_id, item in old_items.items()
        if item_id in changed_item_ids and item.workflow_file
    }
    allowed_workflow_paths.update(
        item.workflow_file
        for item in candidate_plan.items
        if item.item_id in changed_item_ids and item.workflow_file
    )
    candidate_workflow_paths = {
        item.workflow_file for item in candidate_plan.items if item.workflow_file
    }
    missing_workflows = candidate_workflow_paths - set(artifacts)
    if missing_workflows:
        raise PlanRepairError(
            f"compiled repair omits workflow artifacts: {sorted(missing_workflows)}"
        )
    for path in set(draft.artifacts) & candidate_workflow_paths:
        if path not in allowed_workflow_paths and draft.artifacts[
            path
        ] != artifacts.get(path):
            raise PlanRepairError(
                f"repair changed unrelated workflow artifact {path!r}"
            )
    structrr = draft.manifest.get("structrr")
    shared_artifacts: set[str] = set()
    if isinstance(structrr, Mapping) and isinstance(structrr.get("proposal_path"), str):
        shared_artifacts.add(str(structrr["proposal_path"]))
    shared_artifacts.update(
        path for path in draft.artifacts if path.endswith("-execution-plan.yaml")
    )
    candidate_structrr = candidate_manifest.get("structrr")
    if isinstance(candidate_structrr, Mapping) and isinstance(
        candidate_structrr.get("proposal_path"), str
    ):
        shared_artifacts.add(str(candidate_structrr["proposal_path"]))
    shared_artifacts.update(
        path for path in artifacts if path.endswith("-execution-plan.yaml")
    )
    unexpected_artifacts = set(artifacts) - candidate_workflow_paths - shared_artifacts
    if unexpected_artifacts:
        raise PlanRepairError(
            f"compiled repair has undeclared artifacts: {sorted(unexpected_artifacts)}"
        )
    for path in draft.artifacts:
        if (
            path not in candidate_workflow_paths
            and path not in allowed_workflow_paths
            and path not in shared_artifacts
            and draft.artifacts[path] != artifacts.get(path)
        ):
            raise PlanRepairError(f"repair changed unrelated artifact {path!r}")


def _validate_requirement_allocation(manifest: Mapping[str, Any]) -> None:
    requirements = _record_map(manifest, "requirements", "id")
    units = _record_map(manifest, "units", "id")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, list):
        raise PlanRepairError("manifest coverage is malformed")
    complete = _complete_requirement_ids(manifest)
    allocated = {
        str(requirement_id)
        for unit in units.values()
        for requirement_id in _string_values(unit.get("requirement_ids"))
    }
    missing = set(requirements) - allocated - complete
    if missing:
        raise PlanRepairError(
            f"requirements have no work allocation: {sorted(missing)}"
        )


def _complete_requirement_ids(manifest: Mapping[str, Any]) -> set[str]:
    coverage = manifest.get("coverage")
    if not isinstance(coverage, list):
        raise PlanRepairError("manifest coverage is malformed")
    return {
        str(record.get("requirement_id"))
        for record in coverage
        if isinstance(record, Mapping) and record.get("status") == "complete"
    }


def _manifest_semantic_digest(manifest: Mapping[str, Any]) -> str:
    records = {
        key: value
        for key, value in manifest.items()
        if key not in {"semantic_revision_digest", "latest_repair"}
    }
    return _digest(records)


def _with_semantic_digest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    updated = _copy_json(manifest)
    updated.pop("semantic_revision_digest", None)
    updated["semantic_revision_digest"] = _manifest_semantic_digest(updated)
    return updated


def _set_latest_repair(
    manifest: Mapping[str, Any], repair_record: Mapping[str, Any]
) -> dict[str, Any]:
    updated = _copy_json(manifest)
    updated["latest_repair"] = _copy_json(repair_record)
    return updated


def _validate_operation(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanRepairError("repair operation must be an object")
    kind = value.get("operation")
    if kind not in _OPERATION_KINDS:
        raise PlanRepairError("repair operation kind is invalid")
    fields: dict[str, set[str]] = {
        "add_item": {
            "operation",
            "new_item_ref",
            "unit_ids",
            "reason",
            "requirement_ids",
        },
        "update_item": {
            "operation",
            "item_id",
            "expected_digest",
            "replacement_unit_ids",
            "reason",
        },
        "retire_item": {
            "operation",
            "item_id",
            "expected_digest",
            "disposition",
            "reason",
            "replacement_item_ids",
            "evidence_ids",
        },
        "set_dependency": {
            "operation",
            "before_item_id",
            "after_item_id",
            "present",
            "expected_edge_digest",
            "reason",
        },
        "update_requirement": {
            "operation",
            "requirement_id",
            "expected_digest",
            "replacement_ref",
            "source_refs",
            "reason",
        },
        "assign_setup": {
            "operation",
            "setup_id",
            "owner_item_id",
            "validation_ids",
            "expected_digest",
        },
    }
    if set(value) != fields[str(kind)]:
        raise PlanRepairError(f"{kind} operation fields are invalid")
    for name in ("reason",):
        if name in value and (
            not isinstance(value[name], str) or not value[name].strip()
        ):
            raise PlanRepairError(f"{kind} operation {name} is required")
    for name in ("expected_digest", "expected_edge_digest"):
        if name in value and not _valid_digest(value[name]):
            raise PlanRepairError(f"{kind} operation {name} is invalid")
    for name in (
        "unit_ids",
        "replacement_unit_ids",
        "requirement_ids",
        "source_refs",
        "replacement_item_ids",
        "evidence_ids",
        "validation_ids",
    ):
        if name in value:
            _unique_texts(
                value[name],
                name,
                allow_empty=(name in {"replacement_item_ids", "evidence_ids"}),
            )
    if kind == "retire_item" and value.get("disposition") not in {
        "fulfilled",
        "superseded",
    }:
        raise PlanRepairError("retire_item disposition is invalid")
    if kind == "set_dependency" and not isinstance(value.get("present"), bool):
        raise PlanRepairError("set_dependency.present must be boolean")
    return _copy_json(value)


def _repair_subject(value: RepairSubject | Mapping[str, Any]) -> RepairSubject:
    if isinstance(value, RepairSubject):
        subject = value
    else:
        if set(value) != {"subject_kind", "subject_id", "reason"}:
            raise PlanRepairError("repair impact subject fields are invalid")
        subject = RepairSubject(
            str(value.get("subject_kind", "")),
            str(value.get("subject_id", "")),
            str(value.get("reason", "")),
        )
    if subject.subject_kind not in _SUBJECT_KINDS:
        raise PlanRepairError("repair impact subject kind is invalid")
    if not subject.subject_id.strip() or not subject.reason.strip():
        raise PlanRepairError("repair impact subject ID and reason are required")
    return subject


def _requirements_for_item(
    item_id: str,
    allocations: Mapping[str, Sequence[str]],
    units: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    return {
        requirement_id
        for unit_id in allocations.get(item_id, ())
        for requirement_id in _string_values(
            units.get(unit_id, {}).get("requirement_ids")
        )
    }


def _requirements_for_successor(
    item_id: str,
    items: Mapping[str, PlannedWorkItem],
    allocations: Mapping[str, Sequence[str]],
    units: Mapping[str, Mapping[str, Any]],
    replacement_items: Mapping[str, PlannedWorkItem],
) -> set[str]:
    if item_id in items:
        return _requirements_for_item(item_id, allocations, units)
    item = replacement_items.get(item_id)
    return set() if item is None else set()


def _ensure_units_exist(unit_ids: Sequence[str], units: Mapping[str, Any]) -> None:
    missing = set(unit_ids) - set(units)
    if missing:
        raise PlanRepairError(
            f"replacement references unknown units: {sorted(missing)}"
        )


def _require_affected(item_id: str, affected_item_ids: set[str]) -> None:
    if item_id not in affected_item_ids:
        raise PlanRepairError(f"item {item_id!r} is outside declared repair impact")


def allocated_repair_item_id(plan_id: str, repair_id: str, reference: str) -> str:
    """Allocate a stable successor ID from the plan, repair, and model-local ref."""
    slug = slugify_workflow_id(f"{plan_id}-{reference}")
    suffix = hashlib.sha256(f"{repair_id}:{reference}".encode()).hexdigest()[:8]
    return slugify_workflow_id(f"{slug}-{suffix}")


def _record_map(
    manifest: Mapping[str, Any], key: str, identity: str
) -> dict[str, dict[str, Any]]:
    records = _records(manifest, key)
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        value = record.get(identity)
        if not isinstance(value, str) or not value or value in result:
            raise PlanRepairError(f"manifest {key} has invalid or duplicate {identity}")
        result[value] = _copy_json(record)
    return result


def _record_ids(manifest: Mapping[str, Any], key: str, identity: str) -> set[str]:
    return set(_record_map(manifest, key, identity))


def _records(manifest: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    records = manifest.get(key)
    if not isinstance(records, list) or not all(
        isinstance(item, Mapping) for item in records
    ):
        raise PlanRepairError(f"manifest {key} is malformed")
    return records


def _string_values(value: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise PlanRepairError("expected a list of non-empty string references")
    if not allow_empty and not value:
        raise PlanRepairError("reference list must not be empty")
    if len(value) != len(set(value)):
        raise PlanRepairError("reference list contains duplicates")
    return list(value)


def _unique_texts(
    value: Any, label: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise PlanRepairError(f"{label} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise PlanRepairError(f"{label} must not be empty")
    if len(value) != len(set(value)):
        raise PlanRepairError(f"{label} contains duplicates")
    return tuple(value)


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_DIGEST.fullmatch(value))


def _digest(value: Any) -> str:
    raw = json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def _copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


__all__ = [
    "PlanRepairError",
    "PlanDiscovery",
    "PlanRepairPatch",
    "RepairDraft",
    "RepairImpact",
    "RepairSubject",
    "apply_plan_repair_patch",
    "allocated_repair_item_id",
    "build_plan_repair_patch",
    "dependency_edge_digest",
    "expand_repair_impact",
    "item_content_digest",
    "item_input_contract_digest",
    "plan_revision_digest",
]
