"""Per-item, per-subject Structrr planning contracts and projection checks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from powdrr_service.planning.assembly import WorkUnit
from powdrr_service.planning.contracts import (
    MAX_CONTRACT_TEXT_LENGTH,
    MAX_ENUMERATION_RECORDS,
    PlanningContractError,
    Question,
)
from powdrr_service.planning.multipr_structrr import (
    MultiPRStructrrProposal,
    compile_multi_pr_proposal,
)
from powdrr_service.planning.questions import (
    ContextRecord,
    QuestionRequest,
    SourceExcerpt,
)
from powdrr_service.planning.source import SegmentedSource
from powdrr_service.structrr.snapshot import snapshot_digest

_SECTIONS = frozenset({"entities", "entity_relationships", "files"})
_ACTIONS = frozenset({"added", "changed", "deleted", "removed"})
_IDENTITY_FIELDS = frozenset({"id", "stable_key", "action", "operation_id", "item_id"})
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class StructuralSubjectCandidate:
    section: Literal["entities", "entity_relationships", "files"]
    subject_ref: str | None
    new_identity_description: str | None
    action: Literal["added", "changed", "deleted", "removed"]
    requirement_ids: tuple[str, ...]
    unit_ids: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BoundStructuralSubject:
    subject_id: str
    section: Literal["entities", "entity_relationships", "files"]
    item_id: str
    action: Literal["added", "changed", "deleted", "removed"]
    requirement_ids: tuple[str, ...]
    unit_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    is_new: bool
    planned_path: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "section": self.section,
            "item_id": self.item_id,
            "action": self.action,
            "requirement_ids": list(self.requirement_ids),
            "unit_ids": list(self.unit_ids),
            "source_refs": list(self.source_refs),
            "is_new": self.is_new,
            "planned_path": self.planned_path,
        }


@dataclass(frozen=True, slots=True)
class StructuralSubjectPage:
    records: tuple[BoundStructuralSubject, ...]
    has_more: bool
    next_anchor: str | None
    page_ordinal: int


@dataclass(frozen=True, slots=True)
class StructuralChange:
    subject_id: str
    section: str
    item_id: str
    action: str
    result: Literal["unchanged", "change"]
    desired_content: Mapping[str, Any] | None
    reason: str
    requirement_ids: tuple[str, ...]
    unit_ids: tuple[str, ...]
    source_refs: tuple[str, ...]


class StructrrPlanningError(ValueError):
    """Invalid structural subject, change description, or projection."""


class UnorderedStructrrConflict(StructrrPlanningError):
    """Unordered plan items claim changes to the same structural subject."""


def _merge_duplicate_planned_file_subject(
    existing: BoundStructuralSubject,
    duplicate: BoundStructuralSubject,
) -> BoundStructuralSubject | None:
    if (
        existing.section != "files"
        or duplicate.section != "files"
        or existing.subject_id != duplicate.subject_id
        or existing.item_id != duplicate.item_id
        or existing.action != duplicate.action
        or existing.is_new != duplicate.is_new
        or existing.planned_path is None
        or existing.planned_path != duplicate.planned_path
    ):
        return None
    return BoundStructuralSubject(
        existing.subject_id,
        "files",
        existing.item_id,
        existing.action,
        tuple(sorted(set(existing.requirement_ids) | set(duplicate.requirement_ids))),
        tuple(sorted(set(existing.unit_ids) | set(duplicate.unit_ids))),
        tuple(sorted(set(existing.source_refs) | set(duplicate.source_refs))),
        existing.is_new,
        existing.planned_path,
    )


def coalesce_structural_subjects(
    subjects: Iterable[BoundStructuralSubject],
) -> tuple[BoundStructuralSubject, ...]:
    records: list[BoundStructuralSubject] = []
    indexes: dict[tuple[str, str], int] = {}
    for subject in subjects:
        key = (subject.section, subject.subject_id)
        existing_index = indexes.get(key)
        if existing_index is None:
            indexes[key] = len(records)
            records.append(subject)
            continue
        merged = _merge_duplicate_planned_file_subject(records[existing_index], subject)
        if merged is None:
            raise StructrrPlanningError("subject enumeration contains duplicates")
        records[existing_index] = merged
    return tuple(records)


def parse_structural_subject_page(
    value: Mapping[str, Any],
    *,
    plan_id: str,
    item_id: str,
    requirement_ids: set[str],
    units: Sequence[WorkUnit],
    source_ids: set[str],
    projected_baseline: Mapping[str, Any],
    prior_subject_ids: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    page_ordinal: int = 1,
) -> StructuralSubjectPage:
    _exact(value, {"records", "has_more", "next_anchor"}, "/")
    raw_records = _array(
        value["records"], "/records", minimum=0, maximum=MAX_ENUMERATION_RECORDS
    )
    unit_by_id = {unit.id: unit for unit in units}
    if not unit_by_id:
        raise StructrrPlanningError("subject enumeration requires allocated units")
    allowed_units = set(unit_by_id)
    planned_paths = {write.path for unit in units for write in unit.planned_writes}
    records: list[BoundStructuralSubject] = []
    page_keys: set[tuple[str, str]] = set()
    page_record_indexes: dict[tuple[str, str], int] = {}
    for index, raw in enumerate(raw_records):
        pointer = f"/records/{index}"
        if not isinstance(raw, Mapping):
            raise PlanningContractError(pointer, "must be an object")
        _exact(
            raw,
            {
                "section",
                "subject_ref",
                "new_identity_description",
                "action",
                "requirement_ids",
                "unit_ids",
                "source_refs",
            },
            pointer,
        )
        section = _text(raw["section"], f"{pointer}/section", max_length=40)
        if section not in _SECTIONS:
            raise PlanningContractError(
                f"{pointer}/section", "unsupported Structrr section"
            )
        subject_ref = _nullable_text(raw["subject_ref"], f"{pointer}/subject_ref")
        new_description = _nullable_text(
            raw["new_identity_description"],
            f"{pointer}/new_identity_description",
        )
        if (subject_ref is None) == (new_description is None):
            raise PlanningContractError(
                pointer,
                "exactly one of subject_ref and new_identity_description is required",
            )
        recovered_planned_file = False
        if section == "entities" and subject_ref is not None:
            try:
                _existing_subject_id("entities", subject_ref, projected_baseline)
            except PlanningContractError as entity_error:
                if "subject reference is absent from the projected baseline" not in str(
                    entity_error
                ):
                    raise
                try:
                    _existing_subject_id("files", subject_ref, projected_baseline)
                except PlanningContractError as file_error:
                    if (
                        "subject reference is absent from the projected baseline"
                        not in str(file_error)
                    ):
                        raise
                    referenced_path = _planned_file_reference_path(
                        subject_ref, planned_paths
                    )
                    if referenced_path in planned_paths:
                        # When a planned, baseline-absent file is mislabeled as
                        # an entity, resolve it as a new file subject directly.
                        section = "files"
                        new_description = referenced_path
                        subject_ref = None
                        recovered_planned_file = True
                else:
                    # Models sometimes place a known file or source-symbol
                    # reference in the entities section. Resolve its section
                    # from the projected baseline rather than asking the model
                    # to repeat a large inventory answer.
                    section = "files"
        action = _text(raw["action"], f"{pointer}/action", max_length=20)
        if action not in _ACTIONS:
            raise PlanningContractError(
                f"{pointer}/action", "unsupported Structrr action"
            )
        if recovered_planned_file:
            action = "added"
        # The model sometimes labels a planned, baseline-absent file as an
        # existing subject. Resolve that ambiguity deterministically from the
        # plan's write set and projected baseline instead of asking the model
        # to assign new-identity syntax correctly. Recognized file references
        # must resolve to an allocated planned write absent from the baseline.
        if section == "files" and subject_ref is not None:
            referenced_path = _planned_file_reference_path(subject_ref, planned_paths)
            if referenced_path is not None:
                try:
                    _existing_subject_id(section, subject_ref, projected_baseline)
                except PlanningContractError as error:
                    if (
                        "subject reference is absent from the projected baseline"
                        not in str(error)
                    ):
                        raise
                    new_description = _validate_planned_path(
                        referenced_path,
                        planned_paths,
                        f"{pointer}/subject_ref",
                    )
                    subject_ref = None
                    action = "added"
        requirements = _refs(
            raw["requirement_ids"],
            f"{pointer}/requirement_ids",
            requirement_ids,
            minimum=1,
        )
        unit_refs = _refs(
            raw["unit_ids"], f"{pointer}/unit_ids", allowed_units, minimum=1
        )
        covered_requirements = {
            requirement_id
            for unit_id in unit_refs
            for requirement_id in unit_by_id[unit_id].requirement_ids
        }
        if not set(requirements) <= covered_requirements:
            raise PlanningContractError(
                f"{pointer}/unit_ids",
                "cited requirements must belong to the cited units",
            )
        refs = _refs(
            raw["source_refs"],
            f"{pointer}/source_refs",
            source_ids,
            minimum=1,
            maximum=len(source_ids),
        )
        if subject_ref is not None:
            subject_id = _existing_subject_id(section, subject_ref, projected_baseline)
            if action == "added":
                # The model can identify the right existing subject but still
                # call its change "added". Once the reference resolves against
                # the projected baseline, the identity already exists; the
                # only non-destructive action that matches that evidence is
                # changed. Keep identity resolution deterministic and avoid
                # retrying a large inventory answer for this action mismatch.
                action = "changed"
            planned_path = (
                subject_id.removeprefix("path::")
                if section == "files" and subject_id.startswith("path::")
                else None
            )
            is_new = False
        else:
            if action != "added":
                raise PlanningContractError(
                    f"{pointer}/action", "a new identity must use action added"
                )
            assert new_description is not None
            if section == "files":
                planned_path = _validate_planned_file_description(
                    new_description,
                    planned_paths,
                    f"{pointer}/new_identity_description",
                )
                subject_id = f"path::{planned_path}"
            else:
                planned_path = None
                subject_id = _new_subject_id(plan_id, section, new_description)
            is_new = True
            if _subject_exists(section, subject_id, projected_baseline):
                raise PlanningContractError(
                    f"{pointer}/new_identity_description",
                    "new identity resolves to a subject already in the "
                    "projected baseline",
                )
        key = (section, subject_id)
        if key in prior_subject_ids:
            raise PlanningContractError(pointer, "subject was already enumerated")
        if key in page_keys:
            existing_index = page_record_indexes[key]
            existing = records[existing_index]
            merged = _merge_duplicate_planned_file_subject(
                existing,
                BoundStructuralSubject(
                    subject_id,
                    section,  # type: ignore[arg-type]
                    item_id,
                    action,  # type: ignore[arg-type]
                    requirements,
                    unit_refs,
                    refs,
                    is_new,
                    planned_path,
                ),
            )
            if merged is None or planned_path not in planned_paths:
                raise PlanningContractError(pointer, "subject was already enumerated")
            # Multiple work units can legitimately plan changes to one shared
            # file. Keep one structural subject and combine all supporting
            # units, requirements, and evidence.
            records[existing_index] = merged
            continue
        page_keys.add(key)
        page_record_indexes[key] = len(records)
        records.append(
            BoundStructuralSubject(
                subject_id,
                section,  # type: ignore[arg-type]
                item_id,
                action,  # type: ignore[arg-type]
                requirements,
                unit_refs,
                refs,
                is_new,
                planned_path,
            )
        )
    has_more = value["has_more"]
    if not isinstance(has_more, bool):
        raise PlanningContractError("/has_more", "must be a boolean")
    next_anchor = value["next_anchor"]
    expected_next = f"structural-page-{page_ordinal + 1:04d}"
    if has_more:
        if next_anchor != expected_next:
            raise PlanningContractError(
                "/next_anchor",
                f"must equal the next runtime-owned cursor {expected_next!r}",
            )
    elif next_anchor is not None:
        raise PlanningContractError("/next_anchor", "must be null on the final page")
    return StructuralSubjectPage(tuple(records), has_more, next_anchor, page_ordinal)


def complete_subject_enumeration(
    pages: Sequence[StructuralSubjectPage],
) -> tuple[BoundStructuralSubject, ...]:
    """Accept only a contiguous page sequence that reaches an explicit last page."""
    if not pages:
        raise StructrrPlanningError("at least one subject page is required")
    if [page.page_ordinal for page in pages] != list(range(1, len(pages) + 1)):
        raise StructrrPlanningError("subject pages are missing or out of order")
    for page in pages[:-1]:
        if (
            not page.has_more
            or page.next_anchor != f"structural-page-{page.page_ordinal + 1:04d}"
        ):
            raise StructrrPlanningError("subject enumeration has a broken page cursor")
    if pages[-1].has_more or pages[-1].next_anchor is not None:
        raise StructrrPlanningError("subject enumeration is incomplete")
    return coalesce_structural_subjects(
        record for page in pages for record in page.records
    )


def parse_structural_change(
    value: Mapping[str, Any],
    *,
    subject: BoundStructuralSubject,
    taxonomy_entity_types: set[str],
    planned_paths: set[str],
    known_entity_ids: set[str],
) -> StructuralChange:
    if not isinstance(value, Mapping):
        raise PlanningContractError("/", "must be an object")
    result = value.get("result")
    if result == "unchanged":
        _exact(value, {"result", "reason"}, "/")
        return StructuralChange(
            subject.subject_id,
            subject.section,
            subject.item_id,
            subject.action,
            "unchanged",
            None,
            _text(value["reason"], "/reason", max_length=1_000),
            subject.requirement_ids,
            subject.unit_ids,
            subject.source_refs,
        )
    if result != "change":
        raise PlanningContractError("/result", "must be unchanged or change")
    _exact(value, {"result", "desired_content", "reason"}, "/")
    content = value["desired_content"]
    if not isinstance(content, Mapping):
        raise PlanningContractError("/desired_content", "must be an object")
    if set(content).intersection(_IDENTITY_FIELDS):
        raise PlanningContractError(
            "/desired_content", "identity and operation fields are code-assigned"
        )
    _validate_desired_content(
        content,
        subject=subject,
        taxonomy_entity_types=taxonomy_entity_types,
        planned_paths=planned_paths,
        known_entity_ids=known_entity_ids,
    )
    return StructuralChange(
        subject.subject_id,
        subject.section,
        subject.item_id,
        subject.action,
        "change",
        dict(content),
        _text(value["reason"], "/reason", max_length=1_000),
        subject.requirement_ids,
        subject.unit_ids,
        subject.source_refs,
    )


def bind_structural_operation(change: StructuralChange) -> dict[str, Any] | None:
    """Add code-owned IDs and item attribution to one accepted subject change."""
    if change.result == "unchanged":
        return None
    identity_field = "stable_key" if change.section == "files" else "id"
    content = {**dict(change.desired_content or {}), identity_field: change.subject_id}
    if change.section == "files":
        content.setdefault("id", f"file:{content.get('path', change.subject_id)}")
    content["action"] = change.action
    operation_id = f"{change.item_id}:{change.section}:{change.subject_id}"
    return {
        "operation_id": operation_id,
        "item_id": change.item_id,
        "section": change.section,
        "subject_id": change.subject_id,
        "action": change.action,
        "content": content,
    }


def compile_structrr_plan(
    *,
    plan_id: str,
    baseline_snapshot: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    item_order: Sequence[str],
    operations: Sequence[Mapping[str, Any]],
    source_ref: str,
) -> MultiPRStructrrProposal:
    """Reject unordered subject conflicts, then compile ordered item projections."""
    item_by_id: dict[str, Mapping[str, Any]] = {}
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in item_by_id:
            raise StructrrPlanningError("plan item IDs must be unique nonempty strings")
        item_by_id[item_id] = item
    if set(item_order) != set(item_by_id) or len(set(item_order)) != len(item_order):
        raise StructrrPlanningError(
            "item order must contain every plan item exactly once"
        )
    dependency_map = {
        item_id: set(_string_list(item_by_id[item_id].get("dependencies")))
        for item_id in item_by_id
    }
    if any(dependencies - set(item_by_id) for dependencies in dependency_map.values()):
        raise StructrrPlanningError("plan contains an unknown item dependency")
    order_position = {item_id: index for index, item_id in enumerate(item_order)}
    if any(
        order_position[dependency] >= order_position[item_id]
        for item_id, dependencies in dependency_map.items()
        for dependency in dependencies
    ):
        raise StructrrPlanningError("item_order must place every dependency first")
    ordered_items = [item_by_id[item_id] for item_id in item_order]
    reject_unordered_subject_conflicts(operations, dependency_map)
    return compile_multi_pr_proposal(
        plan_id=plan_id,
        baseline_snapshot=baseline_snapshot,
        items=ordered_items,
        proposed_operations=operations,
        source_ref=source_ref,
    )


def build_subject_enumeration_request(
    *,
    plan_id: str,
    item_id: str,
    units: Sequence[WorkUnit],
    source: SegmentedSource,
    source_refs: Sequence[str],
    projected_baseline: Mapping[str, Any],
    prior_subjects: Sequence[BoundStructuralSubject] = (),
    page_ordinal: int = 1,
) -> QuestionRequest[StructuralSubjectPage]:
    unit_ids = {unit.id for unit in units}
    requirement_ids = {req for unit in units for req in unit.requirement_ids}
    path_refs_list: list[ContextRecord] = []
    path_ref_counts: dict[str, int] = {}
    for unit in units:
        for write in unit.planned_writes:
            base_ref_id = f"planned-write:{unit.id}:{write.path}"
            occurrence = path_ref_counts.get(base_ref_id, 0) + 1
            path_ref_counts[base_ref_id] = occurrence
            ref_id = (
                base_ref_id
                if occurrence == 1
                else f"{base_ref_id}:duplicate-{occurrence}"
            )
            path_refs_list.append(
                ContextRecord(ref_id, {"unit_id": unit.id, **write.to_data()})
            )
    path_refs = tuple(path_refs_list)
    excerpt_map = _source_excerpts(source, source_refs)
    unit_facts = tuple(
        ContextRecord(f"unit:{unit.id}", unit.to_data()) for unit in units
    )
    subject_index = _structural_subject_index(projected_baseline, units)
    baseline_fact = ContextRecord(
        f"projected-structrr:{item_id}:{snapshot_digest(projected_baseline)[:20]}",
        subject_index,
    )
    prior_facts = tuple(
        ContextRecord(
            f"prior-subject:{subject.section}:{subject.subject_id}",
            {"section": subject.section, "subject_id": subject.subject_id},
        )
        for subject in prior_subjects
    )
    pagination_fact = ContextRecord(
        f"structural-pagination:{item_id}:{page_ordinal:04d}",
        {
            "current_page": page_ordinal,
            "next_anchor_if_more": f"structural-page-{page_ordinal + 1:04d}",
        },
    )
    facts = (*unit_facts, *path_refs, baseline_fact, *prior_facts, pagination_fact)
    context_ids = (
        *(excerpt.ref_id for excerpt in excerpt_map),
        *(record.ref_id for record in facts),
    )
    example = _example_subject_page(
        item_id=item_id,
        units=units,
        source_refs=source_refs,
        projected_baseline=projected_baseline,
        prior_subjects=prior_subjects,
        page_ordinal=page_ordinal,
    )

    def parse(value: Mapping[str, Any]) -> StructuralSubjectPage:
        page = parse_structural_subject_page(
            value,
            plan_id=plan_id,
            item_id=item_id,
            requirement_ids=requirement_ids,
            units=units,
            source_ids=set(source_refs),
            projected_baseline=projected_baseline,
            prior_subject_ids={
                (item.section, item.subject_id) for item in prior_subjects
            },
            page_ordinal=page_ordinal,
        )
        if any(subject.section == "files" for subject in page.records):
            raise PlanningContractError(
                "/records", "planned file paths are registered deterministically"
            )
        return page

    return QuestionRequest(
        question=Question(
            f"structural-subjects-{item_id}-{page_ordinal:04d}",
            "enumerate_structural_subjects",
            1,
            (item_id, *sorted(unit_ids)),
            _digest(
                {
                    "baseline_index": baseline_fact.value,
                    "units": [unit.to_data() for unit in units],
                    "source": [(item.ref_id, item.text) for item in excerpt_map],
                    "prior": [item.to_data() for item in prior_subjects],
                    "page": page_ordinal,
                }
            ),
            context_ids,
            "structural-subject-page-v1",
            1,
        ),
        exact_question=(
            "Enumerate the entity and relationship subjects this one plan item "
            "changes in Structrr. Planned file paths have already been registered "
            "deterministically; do not return file subjects. Include only changes "
            "required by this item's allocated units. Use a supplied existing "
            "entity or relationship reference, or describe one new entity or "
            "relationship identity. "
            "Cite the requirements, units, and source anchors that "
            "justify each subject. Omit implementation details that do not change "
            "Structrr. "
            "For an existing subject, copy exactly one supplied subject_ref "
            "from accepted facts; never combine or construct identifiers. "
            "Use action added only when subject_ref is null and "
            "new_identity_description is non-null. An existing subject_ref must "
            "use changed, deleted, or removed; never mark an existing subject "
            "as added. "
            "Return at most eight subjects. An empty final page means no "
            "structural change is needed for this item."
            " If another page is needed, set has_more true and copy the exact "
            "next_anchor_if_more value from accepted facts; otherwise set "
            "has_more false and next_anchor null."
        ),
        answer_field_definitions={
            "section": "entities or entity_relationships.",
            "subject_ref": "Existing subject ID/stable key, or null for a new subject.",
            "new_identity_description": (
                "For a new entity or relationship, a concise identity description; "
                "otherwise null."
            ),
            "action": (
                "added for a new_identity_description; changed, deleted, or removed "
                "only for a supplied subject_ref."
            ),
            "requirement_ids": (
                "Supplied requirements affected by the structural change."
            ),
            "unit_ids": "Supplied work units that own the structural change.",
            "source_refs": "Supplied source anchors justifying the change.",
            "has_more": "True when another subject page is required.",
            "next_anchor": (
                "Copy next_anchor_if_more from accepted facts when has_more is "
                "true; otherwise null."
            ),
        },
        source_excerpts=excerpt_map,
        repository_evidence=(),
        accepted_facts=facts,
        valid_example=example,
        empty_example={"records": [], "has_more": False, "next_anchor": None},
        answer_schema=_subject_page_schema(
            sections=("entities", "entity_relationships")
        ),
        parse_answer=parse,
    )


def build_structural_change_request(
    subject: BoundStructuralSubject,
    *,
    projected_baseline: Mapping[str, Any],
    taxonomy_entity_types: Sequence[str],
    planned_paths: set[str],
    known_entity_ids: set[str],
    valid_example: Mapping[str, Any],
) -> QuestionRequest[StructuralChange]:
    facts = [
        ContextRecord(
            f"subject:{subject.section}:{subject.subject_id}", subject.to_data()
        ),
        ContextRecord(
            f"projected-baseline:{subject.item_id}:{snapshot_digest(projected_baseline)[:20]}",
            _structural_change_baseline_context(projected_baseline, subject),
        ),
    ]
    if subject.section == "entities":
        facts.append(
            ContextRecord(
                "entity-taxonomy", {"entity_types": list(taxonomy_entity_types)}
            )
        )
    if subject.section == "files":
        facts.append(ContextRecord("planned-paths", {"paths": sorted(planned_paths)}))
    if subject.section == "entity_relationships":
        facts.append(
            ContextRecord("entity-identities", {"ids": sorted(known_entity_ids)})
        )
    accepted_facts = tuple(facts)
    context_ids = tuple(item.ref_id for item in accepted_facts)
    desired_schema = _desired_content_schema(subject)
    question_example = valid_example
    if (
        subject.section == "files"
        and subject.action == "added"
        and subject.planned_path is not None
    ):
        question_example = {
            "result": "change",
            "desired_content": {"path": subject.planned_path},
            "reason": "The projected baseline does not yet contain this planned file.",
        }
    return QuestionRequest(
        question=Question(
            f"describe-structural-change-{subject.item_id}-{subject.subject_id}",
            "describe_structural_change",
            1,
            (subject.item_id, subject.subject_id),
            _digest(
                {
                    "subject": subject.to_data(),
                    "baseline": snapshot_digest(projected_baseline),
                    "taxonomy": (
                        list(taxonomy_entity_types)
                        if subject.section == "entities"
                        else []
                    ),
                    "paths": sorted(planned_paths)
                    if subject.section == "files"
                    else [],
                    "entity_ids": (
                        sorted(known_entity_ids)
                        if subject.section == "entity_relationships"
                        else []
                    ),
                }
            ),
            context_ids,
            "structural-change-v1",
            1,
        ),
        exact_question=(
            "For this one bound Structrr subject, determine whether the projected "
            "baseline already has the desired state. If the baseline lacks the "
            "desired state, return change with only the desired section-specific "
            "fields; an added subject needs change unless the baseline already "
            "contains that desired subject. Return unchanged only when the baseline "
            "already satisfies it. Give one concise reason of at most 200 characters; "
            "do not include deliberation. Do not return IDs, action, item assignment, "
            "operation ID, or another subject."
        ),
        answer_field_definitions={
            "result": "unchanged or change.",
            "desired_content": (
                "Section-specific desired fields; omitted for unchanged."
                + (
                    " For entity subjects, type must match one of the supplied "
                    "entity-taxonomy entity_types."
                    if subject.section == "entities"
                    else ""
                )
            ),
            "reason": "Why this subject should remain unchanged or receive the change.",
        },
        source_excerpts=(),
        repository_evidence=(),
        accepted_facts=accepted_facts,
        valid_example=question_example,
        empty_example={
            "result": "unchanged",
            "reason": "The projected baseline already has the required state.",
        },
        answer_schema={
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["result", "reason"],
                    "properties": {
                        "result": {"const": "unchanged"},
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1_000,
                        },
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["result", "desired_content", "reason"],
                    "properties": {
                        "result": {"const": "change"},
                        "desired_content": desired_schema,
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1_000,
                        },
                    },
                },
            ]
        },
        parse_answer=lambda value: parse_structural_change(
            value,
            subject=subject,
            taxonomy_entity_types=set(taxonomy_entity_types),
            planned_paths=planned_paths,
            known_entity_ids=known_entity_ids,
        ),
    )


def _example_subject_page(
    *,
    item_id: str,
    units: Sequence[WorkUnit],
    source_refs: Sequence[str],
    projected_baseline: Mapping[str, Any],
    prior_subjects: Sequence[BoundStructuralSubject],
    page_ordinal: int,
) -> Mapping[str, Any]:
    unit = units[0]
    prior_subject_ids = {
        (subject.section, subject.subject_id) for subject in prior_subjects
    }
    subject_index = _structural_subject_index(projected_baseline, units)
    existing_entities = {
        subject_id
        for subject_id in _record_ids(subject_index, "entities", "id")
        if ("entities", subject_id) not in prior_subject_ids
    }
    if existing_entities:
        candidate = {
            "section": "entities",
            "subject_ref": sorted(existing_entities)[0],
            "new_identity_description": None,
            "action": "changed",
            "requirement_ids": [unit.requirement_ids[0]],
            "unit_ids": [unit.id],
            "source_refs": [source_refs[0]],
        }
    else:
        return {"records": [], "has_more": False, "next_anchor": None}
    return {"records": [candidate], "has_more": False, "next_anchor": None}


def _structural_subject_index(
    snapshot: Mapping[str, Any], units: Sequence[WorkUnit]
) -> dict[str, Any]:
    """Give subject enumeration a compact baseline index for this item's paths."""
    paths = {
        write.path for unit in units for write in unit.planned_writes if write.path
    }
    entity_records = _mapping_records(snapshot.get("entities"))
    relevant_entities = [
        record
        for record in entity_records
        if record.get("path") in paths
        or record.get("id") in {f"file:{path}" for path in paths}
        or record.get("id") in paths
    ]
    relevant_entity_ids = {
        str(record["id"])
        for record in relevant_entities
        if isinstance(record.get("id"), str)
    }
    source_subjects = [
        record
        for record in _mapping_records(snapshot.get("source_subjects"))
        if record.get("path") in paths and isinstance(record.get("stable_key"), str)
    ]
    indexed_paths = {
        str(record["path"])
        for record in source_subjects
        if isinstance(record.get("path"), str)
    }
    source_subjects.extend(
        {
            "stable_key": f"path::{path}",
            "path": path,
            "kind": "file",
            "qualified_name": path,
        }
        for record in _mapping_records(snapshot.get("files"))
        if isinstance((path := record.get("path")), str)
        and path in paths
        and path not in indexed_paths
    )
    relationships = [
        record
        for record in _mapping_records(snapshot.get("entity_relationships"))
        if record.get("source") in relevant_entity_ids
        or record.get("target") in relevant_entity_ids
    ]
    return {
        "fingerprint": snapshot_digest(snapshot),
        "planned_paths": sorted(paths),
        "entities": [
            _pick_fields(record, ("id", "type", "name", "path"))
            for record in relevant_entities
        ],
        "entity_relationships": [
            _pick_fields(record, ("id", "source", "relationship", "target"))
            for record in relationships
        ],
        "source_subjects": [
            _pick_fields(
                record,
                (
                    "stable_key",
                    "path",
                    "kind",
                    "qualified_name",
                    "language",
                    "file_entity_id",
                    "span",
                ),
            )
            for record in source_subjects
        ],
    }


def _subject_page_schema(
    *, sections: Sequence[str] = tuple(sorted(_SECTIONS))
) -> dict[str, Any]:
    record_schema = {
        **SUBJECT_RECORD_SCHEMA,
        "properties": {
            **SUBJECT_RECORD_SCHEMA["properties"],
            "section": {"type": "string", "enum": list(sections)},
            "subject_ref": {
                "type": ["string", "null"],
            },
        },
    }
    return {
        **SUBJECT_PAGE_SCHEMA,
        "properties": {
            **SUBJECT_PAGE_SCHEMA["properties"],
            "records": {
                **SUBJECT_PAGE_SCHEMA["properties"]["records"],
                "items": record_schema,
            },
        },
    }


def _structural_change_baseline_context(
    snapshot: Mapping[str, Any], subject: BoundStructuralSubject
) -> dict[str, Any]:
    """Provide the one existing subject and its direct references to change review."""
    subject_id = subject.subject_id
    paths = {subject.planned_path} if subject.planned_path else set()
    entities = _mapping_records(snapshot.get("entities"))
    relationships = _mapping_records(snapshot.get("entity_relationships"))
    source_subjects = _mapping_records(snapshot.get("source_subjects"))
    files = _mapping_records(snapshot.get("files"))
    target_relationship = next(
        (record for record in relationships if record.get("id") == subject_id),
        None,
    )
    related_entity_ids = {subject_id}
    if target_relationship is not None:
        related_entity_ids.update(
            value
            for value in (
                target_relationship.get("source"),
                target_relationship.get("target"),
            )
            if isinstance(value, str)
        )
    relevant_entities = [
        record
        for record in entities
        if record.get("id") in related_entity_ids
        or (subject.section == "files" and record.get("path") in paths)
    ]
    related_entity_ids.update(
        str(record["id"])
        for record in relevant_entities
        if isinstance(record.get("id"), str)
    )
    relevant_relationships = [
        record
        for record in relationships
        if record.get("id") == subject_id
        or record.get("source") in related_entity_ids
        or record.get("target") in related_entity_ids
    ]
    if subject.section == "files":
        relevant_source_subjects = [
            record
            for record in source_subjects
            if record.get("stable_key") == subject_id
            or record.get("path") in paths
            or record.get("file_entity_id") in related_entity_ids
        ]
    elif subject.section == "entities":
        relevant_source_subjects = [
            record
            for record in source_subjects
            if record.get("stable_key") == subject_id
        ]
    else:
        relevant_source_subjects = []
    relevant_files = [record for record in files if record.get("path") in paths]
    return {
        "fingerprint": snapshot_digest(snapshot),
        "entities": [
            _pick_fields(record, ("id", "type", "name", "path"))
            for record in relevant_entities
        ],
        "entity_relationships": [
            _pick_fields(record, ("id", "source", "relationship", "target"))
            for record in relevant_relationships
        ],
        "files": [_pick_fields(record, ("path",)) for record in relevant_files],
        "source_subjects": [
            _pick_fields(
                record,
                (
                    "stable_key",
                    "path",
                    "kind",
                    "qualified_name",
                    "language",
                    "file_entity_id",
                    "span",
                ),
            )
            for record in relevant_source_subjects
        ],
    }


def _mapping_records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [record for record in value if isinstance(record, Mapping)]


def _pick_fields(record: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {field: record[field] for field in fields if field in record}


def _validate_desired_content(
    content: Mapping[str, Any],
    *,
    subject: BoundStructuralSubject,
    taxonomy_entity_types: set[str],
    planned_paths: set[str],
    known_entity_ids: set[str],
) -> None:
    if subject.action in {"deleted", "removed"}:
        if content:
            raise PlanningContractError(
                "/desired_content",
                "removal content must be empty; identity is code-bound",
            )
        return
    if subject.section == "entities":
        entity_type = content.get("type")
        if entity_type is not None and entity_type not in taxonomy_entity_types:
            raise PlanningContractError(
                "/desired_content/type",
                "entity type is not in the supplied Structrr taxonomy",
            )
        if subject.action == "added" and entity_type is None:
            raise PlanningContractError(
                "/desired_content/type", "new entities require a taxonomy type"
            )
    elif subject.section == "entity_relationships":
        for field in ("source", "target", "relationship"):
            if (
                not isinstance(content.get(field), str)
                or not str(content[field]).strip()
            ):
                raise PlanningContractError(
                    f"/desired_content/{field}",
                    "relationships require source, target, and relationship",
                )
        if (
            content["source"] not in known_entity_ids
            or content["target"] not in known_entity_ids
        ):
            raise PlanningContractError(
                "/desired_content",
                "relationship endpoints must be known projected entities",
            )
    elif subject.section == "files":
        path = content.get("path")
        if not isinstance(path, str) or path not in planned_paths:
            raise PlanningContractError(
                "/desired_content/path", "file subjects must name a planned write path"
            )
        if subject.planned_path is not None and path != subject.planned_path:
            raise PlanningContractError(
                "/desired_content/path",
                "file path differs from the code-bound new subject",
            )
        _path(path, "/desired_content/path")


def _desired_content_schema(
    subject: BoundStructuralSubject,
) -> dict[str, Any]:
    if subject.action in {"deleted", "removed"}:
        return {"type": "object", "additionalProperties": False, "maxProperties": 0}
    if subject.section == "entities":
        properties: dict[str, Any] = {
            "type": {"type": "string"},
            "description": {"type": "string"},
            "summary": {"type": "string"},
            "rationale": {"type": "string"},
        }
        return {
            "type": "object",
            "required": ["type"] if subject.action == "added" else [],
            "properties": properties,
            "additionalProperties": True,
        }
    if subject.section == "entity_relationships":
        return {
            "type": "object",
            "required": ["source", "target", "relationship"],
            "properties": {
                "source": {"type": "string"},
                "target": {"type": "string"},
                "relationship": {"type": "string"},
                "description": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "additionalProperties": True,
        }
    return {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
            "language": {"type": "string"},
            "kind": {"type": "string"},
            "qualified_name": {"type": "string"},
            "span": {
                "type": "object",
                "additionalProperties": False,
                "required": ["start_line", "end_line"],
                "properties": {
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
            },
        },
        "additionalProperties": True,
    }


def _existing_subject_id(
    section: str, subject_ref: str, snapshot: Mapping[str, Any]
) -> str:
    if section == "files":
        source_subjects = snapshot.get("source_subjects", [])
        if isinstance(source_subjects, list):
            for record in source_subjects:
                if isinstance(record, Mapping):
                    if record.get("stable_key") == subject_ref:
                        return subject_ref
                    if record.get("id") == subject_ref:
                        stable_key = record.get("stable_key")
                        if isinstance(stable_key, str) and stable_key:
                            return stable_key
                    path = record.get("path")
                    if isinstance(path, str) and subject_ref == f"file:{path}":
                        stable_key = record.get("stable_key")
                        return (
                            stable_key
                            if isinstance(stable_key, str) and stable_key
                            else f"path::{path}"
                        )
        entities = snapshot.get("entities", [])
        if isinstance(entities, list):
            for record in entities:
                if not isinstance(record, Mapping) or record.get("id") != subject_ref:
                    continue
                path = record.get("path")
                if isinstance(path, str) and subject_ref == f"file:{path}":
                    stable_key = record.get("stable_key")
                    return (
                        stable_key
                        if isinstance(stable_key, str) and stable_key
                        else f"path::{path}"
                    )
        files = snapshot.get("files", [])
        if isinstance(files, list):
            for record in files:
                if not isinstance(record, Mapping):
                    continue
                path = record.get("path")
                if isinstance(path, str):
                    if f"path::{path}" == subject_ref:
                        return subject_ref
                    if f"file:{path}" == subject_ref:
                        return f"path::{path}"
        raise PlanningContractError(
            "/subject_ref", "subject reference is absent from the projected baseline"
        )

    collection, identity = _collection_identity(section)
    records = snapshot.get(collection, [])
    if not isinstance(records, list):
        raise PlanningContractError(
            "/subject_ref", f"projected baseline {collection} is malformed"
        )
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if record.get(identity) == subject_ref:
            return str(record[identity])
        # Structrr source subjects may expose their stable identity as a path or
        # stable_key while callers naturally refer to the record's `id`.
        if section == "files" and record.get("id") == subject_ref:
            stable_key = record.get("stable_key")
            if isinstance(stable_key, str) and stable_key:
                return stable_key
    raise PlanningContractError(
        "/subject_ref", "subject reference is absent from the projected baseline"
    )


def _file_reference_path(subject_ref: str) -> str | None:
    if subject_ref.startswith("path::"):
        return subject_ref.removeprefix("path::")
    if subject_ref.startswith("file:"):
        return subject_ref.removeprefix("file:")
    return None


def _planned_file_reference_path(
    subject_ref: str, planned_paths: set[str]
) -> str | None:
    referenced_path = _file_reference_path(subject_ref)
    if referenced_path in planned_paths:
        return referenced_path
    repeated_parts = subject_ref.split("::")
    if (
        len(repeated_parts) == 2
        and repeated_parts[0] == repeated_parts[1]
        and repeated_parts[0] in planned_paths
    ):
        return repeated_parts[0]
    return None


def _subject_exists(section: str, subject_id: str, snapshot: Mapping[str, Any]) -> bool:
    if section == "files" and subject_id.startswith("path::"):
        path = subject_id.removeprefix("path::")
        files = snapshot.get("files", [])
        if isinstance(files, list) and any(
            isinstance(item, Mapping) and item.get("path") == path for item in files
        ):
            return True
    collection, identity = _collection_identity(section)
    records = snapshot.get(collection, [])
    return isinstance(records, list) and any(
        isinstance(item, Mapping) and item.get(identity) == subject_id
        for item in records
    )


def _collection_identity(section: str) -> tuple[str, str]:
    if section == "entities":
        return "entities", "id"
    if section == "entity_relationships":
        return "entity_relationships", "id"
    return "source_subjects", "stable_key"


def _new_subject_id(plan_id: str, section: str, description: str) -> str:
    slug = _SLUG.sub("-", description.casefold()).strip("-")[:48] or "subject"
    digest = hashlib.sha256(description.strip().encode()).hexdigest()[:10]
    namespace = "entity" if section == "entities" else "relationship"
    return f"plan:{plan_id}:{namespace}:{slug}:{digest}"


def _validate_planned_path(value: str, paths: set[str], pointer: str) -> str:
    _path(value, pointer)
    if value not in paths:
        raise PlanningContractError(
            pointer, "new file subject must name a planned write path"
        )
    return value


def _validate_planned_file_description(
    value: str, paths: set[str], pointer: str
) -> str:
    if value not in paths:
        referenced_path = _file_reference_path(value)
        if referenced_path is not None and referenced_path in paths:
            value = referenced_path
        else:
            referenced_paths = [
                path for path in paths if _contains_planned_path(value, path)
            ]
            if len(referenced_paths) == 1:
                value = referenced_paths[0]
            elif len(referenced_paths) > 1:
                raise PlanningContractError(
                    pointer,
                    "new file subject description names multiple planned write paths",
                )
    return _validate_planned_path(value, paths, pointer)


def _contains_planned_path(description: str, path: str) -> bool:
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_./-]){re.escape(path)}"
        r"(?=$|[\s,;:!?\"'`()\]]|\.(?=$|[\s,;:!?\"'`()\]]))"
    )
    return pattern.search(description) is not None


def _path(value: str, pointer: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in value
        or str(path) != value
    ):
        raise PlanningContractError(
            pointer, "must be a normalized repository-relative path"
        )


def _record_ids(snapshot: Mapping[str, Any], section: str, identity: str) -> set[str]:
    records = snapshot.get(section, [])
    if not isinstance(records, list):
        return set()
    return {
        str(item[identity])
        for item in records
        if isinstance(item, Mapping) and isinstance(item.get(identity), str)
    }


def _source_excerpts(
    source: SegmentedSource, refs: Sequence[str]
) -> tuple[SourceExcerpt, ...]:
    anchors = {item.id: item for item in source.anchors}
    result: list[SourceExcerpt] = []
    for ref in dict.fromkeys(refs):
        anchor = anchors.get(ref)
        if anchor is None:
            raise PlanningContractError(
                "/source_refs", f"unknown source anchor {ref!r}"
            )
        result.append(
            SourceExcerpt(
                ref,
                source.document.text[anchor.start_offset : anchor.end_offset],
                anchor.start_offset,
            )
        )
    return tuple(result)


def reject_unordered_subject_conflicts(
    operations: Sequence[Mapping[str, Any]], dependencies: Mapping[str, set[str]]
) -> None:
    by_subject: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    seen_within_item: set[tuple[str, str, str]] = set()
    for index, operation in enumerate(operations):
        item_id, section, subject_id = (
            operation.get("item_id"),
            operation.get("section"),
            operation.get("subject_id"),
        )
        if not isinstance(item_id, str) or not item_id:
            raise StructrrPlanningError(
                f"structural operation {index + 1} has incomplete identity"
            )
        if not isinstance(section, str) or not section:
            raise StructrrPlanningError(
                f"structural operation {index + 1} has incomplete identity"
            )
        if not isinstance(subject_id, str) or not subject_id:
            raise StructrrPlanningError(
                f"structural operation {index + 1} has incomplete identity"
            )
        local = (item_id, section, subject_id)
        if local in seen_within_item:
            raise StructrrPlanningError(
                "one item cannot describe the same subject more than once"
            )
        seen_within_item.add(local)
        by_subject.setdefault((section, subject_id), []).append(operation)
    closure: dict[str, set[str]] = {item_id: set() for item_id in dependencies}
    for item_id in dependencies:
        pending = list(dependencies[item_id])
        while pending:
            parent = pending.pop()
            if parent not in closure[item_id]:
                closure[item_id].add(parent)
                pending.extend(dependencies[parent])
    for (section, subject_id), records in by_subject.items():
        items = list(dict.fromkeys(str(item["item_id"]) for item in records))
        for index, first in enumerate(items):
            for second in items[index + 1 :]:
                if first not in closure[second] and second not in closure[first]:
                    raise UnorderedStructrrConflict(
                        f"unordered items {first!r} and {second!r} both change "
                        f"{section} subject {subject_id!r}"
                    )


def _exact(value: Mapping[str, Any], expected: set[str], pointer: str) -> None:
    actual = set(value)
    if actual != expected:
        unknown, missing = sorted(actual - expected), sorted(expected - actual)
        detail = f"unknown fields {unknown}" if unknown else f"missing fields {missing}"
        raise PlanningContractError(pointer, detail)


def _array(value: Any, pointer: str, *, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise PlanningContractError(
            pointer, f"must contain {minimum} to {maximum} entries"
        )
    return value


def _text(
    value: Any, pointer: str, *, max_length: int = MAX_CONTRACT_TEXT_LENGTH
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise PlanningContractError(pointer, "must be nonempty bounded text")
    return value.strip()


def _nullable_text(value: Any, pointer: str) -> str | None:
    return None if value is None else _text(value, pointer)


def _refs(
    value: Any,
    pointer: str,
    known: set[str],
    *,
    minimum: int,
    maximum: int = MAX_ENUMERATION_RECORDS,
) -> tuple[str, ...]:
    values = _array(value, pointer, minimum=minimum, maximum=maximum)
    refs = tuple(
        _text(item, f"{pointer}/{index}", max_length=120)
        for index, item in enumerate(values)
    )
    if len(set(refs)) != len(refs):
        raise PlanningContractError(pointer, "references must be unique")
    unknown = set(refs) - known
    if unknown:
        raise PlanningContractError(pointer, f"unknown references: {sorted(unknown)}")
    return refs


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str)]


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=list)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


SUBJECT_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "section",
        "subject_ref",
        "new_identity_description",
        "action",
        "requirement_ids",
        "unit_ids",
        "source_refs",
    ],
    "properties": {
        "section": {"type": "string", "enum": sorted(_SECTIONS)},
        "subject_ref": {"type": ["string", "null"]},
        "new_identity_description": {"type": ["string", "null"]},
        "action": {"type": "string", "enum": sorted(_ACTIONS)},
        "requirement_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
        },
        "unit_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "source_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    },
}
SUBJECT_PAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["records", "has_more", "next_anchor"],
    "properties": {
        "records": {
            "type": "array",
            "maxItems": MAX_ENUMERATION_RECORDS,
            "items": SUBJECT_RECORD_SCHEMA,
        },
        "has_more": {"type": "boolean"},
        "next_anchor": {"type": ["string", "null"]},
    },
    "allOf": [
        {
            "if": {"properties": {"has_more": {"const": True}}},
            "then": {"properties": {"next_anchor": {"type": "string", "minLength": 1}}},
            "else": {"properties": {"next_anchor": {"type": "null"}}},
        }
    ],
}


__all__ = [
    "BoundStructuralSubject",
    "StructuralChange",
    "StructuralSubjectCandidate",
    "StructuralSubjectPage",
    "StructrrPlanningError",
    "UnorderedStructrrConflict",
    "bind_structural_operation",
    "build_structural_change_request",
    "build_subject_enumeration_request",
    "coalesce_structural_subjects",
    "complete_subject_enumeration",
    "compile_structrr_plan",
    "parse_structural_change",
    "parse_structural_subject_page",
    "reject_unordered_subject_conflicts",
]
