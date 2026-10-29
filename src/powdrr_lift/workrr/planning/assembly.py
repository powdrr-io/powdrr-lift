"""Typed work-unit proposals, runtime facts, and deterministic allocation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from powdrr_service.planning.contracts import (
    MAX_CONTRACT_TEXT_LENGTH,
    MAX_ENUMERATION_RECORDS,
    PlanningContractError,
    Question,
)
from powdrr_service.planning.questions import (
    ContextRecord,
    QuestionRequest,
    SourceExcerpt,
)

from powdrr_lift.workrr.plan_sizing import (
    CANONICAL_RUNTIMES,
    MAX_PR_WORK_UNITS,
    PATH_RUNTIME_SUFFIXES,
    TARGET_PR_WORK_UNITS,
    ExplicitWorkUnit,
    allocate_explicit_work_units,
)
from powdrr_lift.workrr.planning.evidence import Evidence
from powdrr_lift.workrr.planning.source import Requirement, SegmentedSource

MAX_WORK_UNIT_CRITERIA = 2
MAX_ITEM_CRITERIA = 5
MAX_ITEM_SCOPE_ENTRIES = 3
_ROLES = frozenset({"implementation", "test", "configuration", "documentation"})
_RUNTIME_VALUES = frozenset({*CANONICAL_RUNTIMES, "neutral"})
_NEUTRAL_SUFFIXES = frozenset(
    {
        ".cfg",
        ".csv",
        ".html",
        ".ini",
        ".json",
        ".lock",
        ".md",
        ".properties",
        ".rst",
        ".svg",
        ".toml",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_PLACEHOLDER_RE = re.compile(
    r"(?:\{\{[^}]+\}\}|\$\{[^}]+\}|<\s*(?:TODO|TBD|PLACEHOLDER)[^>]*>)",
    re.IGNORECASE,
)

WORK_UNIT_PROPOSAL_PAGE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["records", "has_more", "next_anchor"],
    "properties": {
        "records": {
            "type": "array",
            "maxItems": MAX_ENUMERATION_RECORDS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "requirement_ids",
                    "deliverable",
                    "component",
                    "implementation_runtime",
                    "planned_writes",
                    "implementation_step",
                    "acceptance_criteria",
                    "validation_ids",
                    "required_outputs",
                ],
                "properties": {
                    "requirement_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "deliverable": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1_000,
                    },
                    "component": {"type": "string", "minLength": 1, "maxLength": 120},
                    "implementation_runtime": {
                        "type": "string",
                        "enum": sorted(_RUNTIME_VALUES),
                    },
                    "planned_writes": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "path",
                                "role",
                                "runtime",
                                "source_refs",
                                "evidence_ids",
                            ],
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 500,
                                },
                                "role": {"type": "string", "enum": sorted(_ROLES)},
                                "runtime": {
                                    "type": ["string", "null"],
                                    "enum": [*sorted(CANONICAL_RUNTIMES), None],
                                },
                                "source_refs": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string"},
                                },
                                "evidence_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                    "implementation_step": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_CONTRACT_TEXT_LENGTH,
                    },
                    "acceptance_criteria": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_WORK_UNIT_CRITERIA,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "validation_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "required_outputs": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["description", "requirement_ids"],
                            "properties": {
                                "description": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 1_000,
                                },
                                "requirement_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
        "has_more": {"type": "boolean"},
        "next_anchor": {"type": ["string", "null"]},
    },
}


class AssemblyError(ValueError):
    """Invalid work-unit proposal, allocation, or item description."""


class RuntimeClassificationRequired(AssemblyError):
    """A write path does not identify its language without repository evidence."""


@dataclass(frozen=True, slots=True)
class PlannedWrite:
    path: str
    role: Literal["implementation", "test", "configuration", "documentation"]
    runtime: str | None
    source_refs: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "runtime": self.runtime,
            "source_refs": list(self.source_refs),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class RequiredOutputProposal:
    description: str
    requirement_ids: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "requirement_ids": list(self.requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class WorkUnitProposal:
    requirement_ids: tuple[str, ...]
    deliverable: str
    component: str
    implementation_runtime: str
    planned_writes: tuple[PlannedWrite, ...]
    implementation_step: str
    acceptance_criteria: tuple[str, ...]
    validation_ids: tuple[str, ...]
    required_outputs: tuple[RequiredOutputProposal, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "requirement_ids": list(self.requirement_ids),
            "deliverable": self.deliverable,
            "component": self.component,
            "implementation_runtime": self.implementation_runtime,
            "planned_writes": [item.to_data() for item in self.planned_writes],
            "implementation_step": self.implementation_step,
            "acceptance_criteria": list(self.acceptance_criteria),
            "validation_ids": list(self.validation_ids),
            "required_outputs": [item.to_data() for item in self.required_outputs],
        }


@dataclass(frozen=True, slots=True)
class WorkUnit:
    id: str
    requirement_ids: tuple[str, ...]
    deliverable: str
    component: str
    implementation_runtime: str
    planned_writes: tuple[PlannedWrite, ...]
    implementation_step: str
    acceptance_criteria: tuple[str, ...]
    validation_ids: tuple[str, ...]
    prerequisite_unit_ids: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requirement_ids": list(self.requirement_ids),
            "deliverable": self.deliverable,
            "component": self.component,
            "implementation_runtime": self.implementation_runtime,
            "planned_writes": [item.to_data() for item in self.planned_writes],
            "implementation_step": self.implementation_step,
            "acceptance_criteria": list(self.acceptance_criteria),
            "validation_ids": list(self.validation_ids),
            "prerequisite_unit_ids": list(self.prerequisite_unit_ids),
        }


@dataclass(frozen=True, slots=True)
class PendingUnitOutput:
    unit_id: str
    description: str
    requirement_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PathFact:
    path: str
    runtime: str | None = None
    is_symlink: bool = False


@dataclass(frozen=True, slots=True)
class WorkUnitContext:
    requirements: Mapping[str, Requirement]
    unresolved_requirement_ids: tuple[str, ...]
    source: SegmentedSource
    evidence: Mapping[str, Evidence]
    validation_catalog: Mapping[str, Mapping[str, Any]]
    component_ids: frozenset[str]
    repository_paths: Mapping[str, PathFact]
    ambiguous_runtime_resolutions: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class AllocatedWorkUnits:
    units: tuple[WorkUnit, ...]
    pending_outputs: tuple[PendingUnitOutput, ...]


@dataclass(frozen=True, slots=True)
class AllocationGroup:
    id: str
    unit_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    component_ids: tuple[str, ...]
    implementation_runtime: str
    acceptance_criteria_count: int


@dataclass(frozen=True, slots=True)
class UnitAllocation:
    groups: tuple[AllocationGroup, ...]
    objective_value: int


@dataclass(frozen=True, slots=True)
class RequirementEnumerationPage:
    records: tuple[WorkUnitProposal, ...]
    has_more: bool
    next_anchor: str | None


@dataclass(frozen=True, slots=True)
class CohesionAccept:
    reason: str
    decision: Literal["accept"] = "accept"


@dataclass(frozen=True, slots=True)
class CohesionSplit:
    replacements: tuple[WorkUnitProposal, ...]
    reason: str
    decision: Literal["split"] = "split"


type CohesionDecision = CohesionAccept | CohesionSplit


@dataclass(frozen=True, slots=True)
class ItemScope:
    description: str
    unit_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ItemDescription:
    title: str
    objective: str
    scope: tuple[ItemScope, ...]


def parse_work_unit_proposal(
    value: Mapping[str, Any], *, context: WorkUnitContext
) -> WorkUnitProposal:
    """Parse one proposal and bind all model references to supplied catalogs."""
    expected = {
        "requirement_ids",
        "deliverable",
        "component",
        "implementation_runtime",
        "planned_writes",
        "implementation_step",
        "acceptance_criteria",
        "validation_ids",
        "required_outputs",
    }
    _exact_fields(value, expected, "/")
    requirement_ids = _reference_list(
        value["requirement_ids"],
        "/requirement_ids",
        known=set(context.unresolved_requirement_ids),
        minimum=1,
    )
    component = _text(value["component"], "/component", max_length=120)
    if component not in context.component_ids:
        raise PlanningContractError(
            "/component", "component is not in the supplied catalog"
        )
    declared_runtime = _text(
        value["implementation_runtime"], "/implementation_runtime", max_length=40
    )
    if declared_runtime not in _RUNTIME_VALUES:
        raise PlanningContractError(
            "/implementation_runtime", "runtime is not in the canonical registry"
        )
    raw_writes = _array(value["planned_writes"], "/planned_writes", maximum=20)
    if not raw_writes:
        raise PlanningContractError(
            "/planned_writes", "must contain at least one write"
        )
    writes = tuple(
        _parse_planned_write(item, pointer=f"/planned_writes/{index}", context=context)
        for index, item in enumerate(raw_writes)
    )
    non_neutral_runtimes = {
        runtime
        for write in writes
        if (runtime := _write_runtime(write, context)) is not None
    }
    if len(non_neutral_runtimes) > 1:
        raise PlanningContractError(
            "/planned_writes",
            "one work unit cannot mix implementation runtimes; split the work",
        )
    derived_runtime = next(iter(non_neutral_runtimes), "neutral")
    if declared_runtime != derived_runtime:
        raise PlanningContractError(
            "/implementation_runtime",
            f"runtime must be derived from planned writes ({derived_runtime})",
        )
    step = _text(value["implementation_step"], "/implementation_step")
    criteria = _text_list(
        value["acceptance_criteria"],
        "/acceptance_criteria",
        minimum=1,
        maximum=MAX_WORK_UNIT_CRITERIA,
    )
    validation_ids = _reference_list(
        value["validation_ids"],
        "/validation_ids",
        known=set(context.validation_catalog),
        minimum=1,
    )
    raw_outputs = _array(value["required_outputs"], "/required_outputs", maximum=8)
    outputs = tuple(
        _parse_required_output(
            item,
            pointer=f"/required_outputs/{index}",
            requirement_ids=set(context.unresolved_requirement_ids),
        )
        for index, item in enumerate(raw_outputs)
    )
    source_refs = {ref for write in writes for ref in write.source_refs}
    for requirement_id in requirement_ids:
        requirement = context.requirements[requirement_id]
        if not source_refs.intersection(requirement.source_refs):
            raise PlanningContractError(
                "/planned_writes",
                f"writes for {requirement_id} need one of its source anchors",
            )
    return WorkUnitProposal(
        requirement_ids,
        _text(value["deliverable"], "/deliverable", max_length=1_000),
        component,
        declared_runtime,
        writes,
        step,
        criteria,
        validation_ids,
        outputs,
    )


def _parse_planned_write(
    value: Any, *, pointer: str, context: WorkUnitContext
) -> PlannedWrite:
    if not isinstance(value, Mapping):
        raise PlanningContractError(pointer, "must be an object")
    _exact_fields(
        value,
        {"path", "role", "runtime", "source_refs", "evidence_ids"},
        pointer,
    )
    path = _text(value["path"], f"{pointer}/path", max_length=500)
    _validate_path_shape(path, f"{pointer}/path")
    role = _text(value["role"], f"{pointer}/role", max_length=40)
    if role not in _ROLES:
        raise PlanningContractError(f"{pointer}/role", "unknown planned-write role")
    runtime_raw = value["runtime"]
    runtime = (
        None
        if runtime_raw is None
        else _text(runtime_raw, f"{pointer}/runtime", max_length=40)
    )
    if runtime is not None and runtime not in CANONICAL_RUNTIMES:
        raise PlanningContractError(f"{pointer}/runtime", "runtime is not canonical")
    source_refs = _reference_list(
        value["source_refs"],
        f"{pointer}/source_refs",
        known={item.id for item in context.source.anchors},
        minimum=1,
    )
    evidence_ids = _reference_list(
        value["evidence_ids"],
        f"{pointer}/evidence_ids",
        known=set(context.evidence),
        minimum=0,
    )
    write = PlannedWrite(
        path,
        cast(Literal["implementation", "test", "configuration", "documentation"], role),
        runtime,
        source_refs,
        evidence_ids,
    )
    _write_runtime(write, context)
    return write


def _parse_required_output(
    value: Any, *, pointer: str, requirement_ids: set[str]
) -> RequiredOutputProposal:
    if not isinstance(value, Mapping):
        raise PlanningContractError(pointer, "must be an object")
    _exact_fields(value, {"description", "requirement_ids"}, pointer)
    refs = _reference_list(
        value["requirement_ids"],
        f"{pointer}/requirement_ids",
        known=requirement_ids,
        minimum=1,
    )
    return RequiredOutputProposal(
        _text(value["description"], f"{pointer}/description", max_length=1_000), refs
    )


def parse_work_unit_proposal_page(
    value: Mapping[str, Any],
    *,
    context: WorkUnitContext,
    requirement_order: Sequence[str],
    after_anchor: str | None = None,
) -> RequirementEnumerationPage:
    _exact_fields(value, {"records", "has_more", "next_anchor"}, "/")
    raw_records = _array(value["records"], "/records", maximum=MAX_ENUMERATION_RECORDS)
    proposals = tuple(
        parse_work_unit_proposal(item, context=context)
        if isinstance(item, Mapping)
        else _invalid_record(f"/records/{index}")
        for index, item in enumerate(raw_records)
    )
    has_more = _bool(value["has_more"], "/has_more")
    raw_anchor = value["next_anchor"]
    if has_more:
        anchor = _text(raw_anchor, "/next_anchor", max_length=120)
        if anchor not in requirement_order:
            raise PlanningContractError(
                "/next_anchor", "must be in the supplied requirement range"
            )
        if after_anchor is not None and after_anchor not in requirement_order:
            raise PlanningContractError(
                "/next_anchor", "supplied cursor is not in the requirement range"
            )
        cursor = (
            requirement_order.index(after_anchor) if after_anchor is not None else -1
        )
        if requirement_order.index(anchor) <= cursor:
            raise PlanningContractError(
                "/next_anchor", "must advance beyond the page cursor"
            )
    else:
        if raw_anchor is not None:
            raise PlanningContractError(
                "/next_anchor", "must be null on the final page"
            )
        anchor = None
    return RequirementEnumerationPage(proposals, has_more, anchor)


def _write_runtime(write: PlannedWrite, context: WorkUnitContext) -> str | None:
    """Derive a write's runtime from its path and pinned repository facts."""
    fact = context.repository_paths.get(write.path)
    if fact is not None and fact.is_symlink:
        raise PlanningContractError("/planned_writes/path", "write path is a symlink")
    suffix = PurePosixPath(write.path).suffix.lower()
    derived = None if suffix == ".h" else PATH_RUNTIME_SUFFIXES.get(suffix)
    if derived is None and suffix in _NEUTRAL_SUFFIXES:
        derived = None
    elif (
        derived is None
        and not suffix
        and PurePosixPath(write.path).name
        in {
            "Makefile",
            "Dockerfile",
            "Justfile",
            "Procfile",
            "LICENSE",
        }
    ):
        derived = None
    elif derived is None:
        derived = (
            fact.runtime if fact is not None else None
        ) or context.ambiguous_runtime_resolutions.get(write.path)
        if derived is None:
            raise RuntimeClassificationRequired(
                f"runtime for planned path {write.path!r} needs repository evidence"
            )
        if derived not in CANONICAL_RUNTIMES:
            raise PlanningContractError(
                "/planned_writes/path", "repository runtime is not canonical"
            )
    if write.runtime != derived:
        raise PlanningContractError(
            "/planned_writes/runtime",
            f"runtime for {write.path} must be {derived!r} based on the path "
            "and repository facts",
        )
    if fact is None:
        anchors = {item.id: item for item in context.source.anchors}
        additions = "\n".join(
            context.source.document.text[
                anchors[ref].start_offset : anchors[ref].end_offset
            ]
            for ref in write.source_refs
        )
        if write.path not in additions:
            raise PlanningContractError(
                "/planned_writes/path",
                "new paths must be named by a cited source anchor",
            )
    return derived


def allocate_work_units(
    proposals: Sequence[WorkUnitProposal], *, context: WorkUnitContext
) -> AllocatedWorkUnits:
    """Assign runtime-owned IDs and enforce exact unresolved-requirement coverage."""
    if not proposals:
        if context.unresolved_requirement_ids:
            raise AssemblyError("no work units cover unresolved requirements")
        return AllocatedWorkUnits((), ())
    unresolved = set(context.unresolved_requirement_ids)
    seen_requirements: set[str] = set()
    units: list[WorkUnit] = []
    pending: list[PendingUnitOutput] = []
    for ordinal, proposal in enumerate(proposals, start=1):
        if not set(proposal.requirement_ids) <= unresolved:
            raise AssemblyError("work unit references a non-unresolved requirement")
        seen_requirements.update(proposal.requirement_ids)
        unit_id = f"unit-{ordinal:04d}"
        units.append(
            WorkUnit(
                id=unit_id,
                requirement_ids=proposal.requirement_ids,
                deliverable=proposal.deliverable,
                component=proposal.component,
                implementation_runtime=proposal.implementation_runtime,
                planned_writes=proposal.planned_writes,
                implementation_step=proposal.implementation_step,
                acceptance_criteria=proposal.acceptance_criteria,
                validation_ids=proposal.validation_ids,
            )
        )
        pending.extend(
            PendingUnitOutput(unit_id, item.description, item.requirement_ids)
            for item in proposal.required_outputs
        )
    if seen_requirements != unresolved:
        missing = sorted(unresolved - seen_requirements)
        raise AssemblyError(
            f"work units leave unresolved requirements uncovered: {missing}"
        )
    return AllocatedWorkUnits(tuple(units), tuple(pending))


def build_work_unit_proposal_request(
    context: WorkUnitContext,
    requirement_ids: Sequence[str],
    *,
    valid_example: Mapping[str, Any],
    page_after: str | None = None,
) -> QuestionRequest[RequirementEnumerationPage]:
    """Build one bounded enumeration question for at most eight requirements."""
    if not 1 <= len(requirement_ids) <= MAX_ENUMERATION_RECORDS:
        raise ValueError("work-unit questions require one to eight requirements")
    if len(set(requirement_ids)) != len(requirement_ids):
        raise ValueError("work-unit question requirement IDs must be unique")
    if not set(requirement_ids) <= set(context.unresolved_requirement_ids):
        raise ValueError("work-unit question includes a non-unresolved requirement")
    anchor_by_id = {item.id: item for item in context.source.anchors}
    source_refs = tuple(
        dict.fromkeys(
            ref
            for requirement_id in requirement_ids
            for ref in context.requirements[requirement_id].source_refs
        )
    )
    excerpts = tuple(
        SourceExcerpt(
            ref,
            context.source.document.text[
                anchor_by_id[ref].start_offset : anchor_by_id[ref].end_offset
            ],
            anchor_by_id[ref].start_offset,
        )
        for ref in source_refs
    )
    paths = tuple(
        ContextRecord(f"path:{path}", {"path": path, "runtime": fact.runtime})
        for path, fact in sorted(context.repository_paths.items())
    )
    evidence = tuple(
        ContextRecord(f"evidence:{evidence_id}", item.to_data())
        for evidence_id, item in sorted(context.evidence.items())
    )
    validations = tuple(
        ContextRecord(f"validation:{validation_id}", value)
        for validation_id, value in sorted(context.validation_catalog.items())
    )
    requirement_facts = tuple(
        ContextRecord(
            f"requirement:{requirement_id}",
            {
                "id": requirement_id,
                "statement": context.requirements[requirement_id].statement,
                "source_refs": list(context.requirements[requirement_id].source_refs),
            },
        )
        for requirement_id in requirement_ids
    )
    components = (
        ContextRecord("components", {"component_ids": sorted(context.component_ids)}),
    )
    accepted = (*requirement_facts, *validations, *components)
    all_refs = tuple(
        [
            *(item.ref_id for item in excerpts),
            *(item.ref_id for item in paths),
            *(item.ref_id for item in evidence),
            *(item.ref_id for item in accepted),
        ]
    )
    input_facts = {
        "requirements": [
            context.requirements[item].statement for item in requirement_ids
        ],
        "source": [
            {
                "ref_id": item.ref_id,
                "text": item.text,
                "start_offset": item.start_offset,
            }
            for item in excerpts
        ],
        "paths": [item.value for item in paths],
        "evidence": [item.value for item in evidence],
        "validations": [item.value for item in validations],
        "components": sorted(context.component_ids),
        "page_after": page_after,
    }
    question = Question(
        question_id=(
            f"propose-units-{requirement_ids[0]}-"
            f"{_digest({'page_after': page_after})[-12:]}"
        ),
        kind="propose_work_units",
        contract_version=1,
        subject_ids=tuple(requirement_ids),
        input_digest=_digest(input_facts),
        context_ref_ids=all_refs,
        expected_answer_schema="work-unit-proposals-v1",
        attempt=1,
    )

    def parse(value: Mapping[str, Any]) -> RequirementEnumerationPage:
        return parse_work_unit_proposal_page(
            value,
            context=context,
            requirement_order=requirement_ids,
            after_anchor=page_after,
        )

    return QuestionRequest(
        question=question,
        exact_question=(
            "Enumerate concrete implementation work units for only the supplied "
            "unresolved requirements. Each unit must describe one implementation "
            "step, at most two acceptance criteria, actual planned file writes, "
            "and required outputs that later work may consume. Do not invent "
            "requirement, component, validation, evidence, or source IDs. Split "
            "different implementation runtimes into separate units. Return a "
            "page of at most eight records."
        ),
        answer_field_definitions={
            "records": "WorkUnitProposal records with the exact fields in the schema.",
            "implementation_runtime": (
                "Canonical runtime derived from the actual planned writes, or "
                "neutral when all writes are non-executable."
            ),
            "required_outputs": (
                "Concrete outputs needed by later work, with references only to "
                "requirements this unit addresses."
            ),
            "has_more": "Whether another page is needed.",
            "next_anchor": (
                "A requirement ID from the supplied order, strictly after the "
                "previous page cursor, or null on the final page."
            ),
        },
        source_excerpts=excerpts,
        repository_evidence=(*paths, *evidence),
        accepted_facts=accepted,
        valid_example=valid_example,
        empty_example={"records": [], "has_more": False, "next_anchor": None},
        answer_schema=WORK_UNIT_PROPOSAL_PAGE_JSON_SCHEMA,
        parse_answer=parse,
    )


def allocate_work_unit_groups(
    units: Sequence[WorkUnit],
    *,
    target_units: int = TARGET_PR_WORK_UNITS,
    max_units: int = MAX_PR_WORK_UNITS,
    standalone_unit_ids: frozenset[str] = frozenset(),
) -> UnitAllocation:
    """Group explicit work units with CP-SAT, using size and cohesion constraints."""
    try:
        allocation = allocate_explicit_work_units(
            [
                ExplicitWorkUnit(
                    unit.id,
                    unit.component,
                    unit.implementation_runtime,
                    len(unit.acceptance_criteria),
                )
                for unit in units
            ],
            target_units=target_units,
            max_units=max_units,
            max_criteria=MAX_ITEM_CRITERIA,
            max_components=MAX_ITEM_SCOPE_ENTRIES,
            standalone_unit_ids=standalone_unit_ids,
        )
    except ValueError as error:
        raise AssemblyError(str(error)) from error
    by_id = {unit.id: unit for unit in units}
    groups: list[AllocationGroup] = []
    for ordinal, unit_ids in enumerate(allocation.groups, start=1):
        chunk = [by_id[unit_id] for unit_id in unit_ids]
        groups.append(
            AllocationGroup(
                id=f"allocation-{ordinal:04d}",
                unit_ids=tuple(unit.id for unit in chunk),
                requirement_ids=tuple(
                    dict.fromkeys(req for unit in chunk for req in unit.requirement_ids)
                ),
                component_ids=tuple(dict.fromkeys(unit.component for unit in chunk)),
                implementation_runtime=next(
                    (
                        unit.implementation_runtime
                        for unit in chunk
                        if unit.implementation_runtime != "neutral"
                    ),
                    "neutral",
                ),
                acceptance_criteria_count=sum(
                    len(unit.acceptance_criteria) for unit in chunk
                ),
            )
        )
    return UnitAllocation(tuple(groups), allocation.objective_value)


def parse_cohesion_decision(
    value: Mapping[str, Any], *, original: WorkUnitProposal, context: WorkUnitContext
) -> CohesionDecision:
    if not isinstance(value, Mapping):
        raise PlanningContractError("/", "must be an object")
    decision = value.get("decision")
    if decision == "accept":
        _exact_fields(value, {"decision", "reason"}, "/")
        return CohesionAccept(_text(value["reason"], "/reason", max_length=1_000))
    if decision != "split":
        raise PlanningContractError("/decision", "must be accept or split")
    _exact_fields(value, {"decision", "replacements", "reason"}, "/")
    reason = _text(value["reason"], "/reason", max_length=1_000)
    raw = _array(value["replacements"], "/replacements", minimum=2, maximum=4)
    replacements = tuple(
        parse_work_unit_proposal(item, context=context)
        if isinstance(item, Mapping)
        else _invalid_record(f"/replacements/{index}")
        for index, item in enumerate(raw)
    )
    original_requirements = set(original.requirement_ids)
    replacement_requirements = [
        req for item in replacements for req in item.requirement_ids
    ]
    if set(replacement_requirements) != original_requirements:
        raise PlanningContractError(
            "/replacements", "must preserve the original requirement coverage exactly"
        )
    original_validations = set(original.validation_ids)
    if {
        item for replacement in replacements for item in replacement.validation_ids
    } != original_validations:
        raise PlanningContractError(
            "/replacements", "must preserve validation obligations"
        )
    return CohesionSplit(replacements, reason)


def build_unit_cohesion_request(
    proposal: WorkUnitProposal, *, context: WorkUnitContext, subject_id: str
) -> QuestionRequest[CohesionDecision]:
    """Ask whether one proposed unit is cohesive, permitting only a bounded split."""
    source_ids = tuple(
        dict.fromkeys(
            ref for write in proposal.planned_writes for ref in write.source_refs
        )
    )
    anchor_by_id = {item.id: item for item in context.source.anchors}
    excerpts = tuple(
        SourceExcerpt(
            ref,
            context.source.document.text[
                anchor_by_id[ref].start_offset : anchor_by_id[ref].end_offset
            ],
            anchor_by_id[ref].start_offset,
        )
        for ref in source_ids
    )
    fact = ContextRecord(f"proposal:{subject_id}", proposal.to_data())
    referenced_paths = tuple(
        ContextRecord(
            f"path:{write.path}",
            {
                "path": write.path,
                "runtime": context.repository_paths[write.path].runtime,
            },
        )
        for write in proposal.planned_writes
        if write.path in context.repository_paths
    )
    validations = tuple(
        ContextRecord(
            f"validation:{validation_id}", context.validation_catalog[validation_id]
        )
        for validation_id in proposal.validation_ids
        if validation_id in context.validation_catalog
    )
    evidence = tuple(
        ContextRecord(
            f"evidence:{evidence_id}", context.evidence[evidence_id].to_data()
        )
        for evidence_id in dict.fromkeys(
            evidence_id
            for write in proposal.planned_writes
            for evidence_id in write.evidence_ids
        )
        if evidence_id in context.evidence
    )
    components = ContextRecord(
        "components", {"component_ids": sorted(context.component_ids)}
    )
    context_records = (*referenced_paths, *evidence)
    accepted_facts = (fact, *validations, components)
    question = Question(
        question_id=f"cohesion-{subject_id}",
        kind="check_unit_cohesion",
        contract_version=1,
        subject_ids=(subject_id,),
        input_digest=_digest(
            {
                "proposal": proposal.to_data(),
                "source": [
                    {"ref_id": item.ref_id, "text": item.text} for item in excerpts
                ],
                "paths": [item.value for item in referenced_paths],
                "evidence": [item.value for item in evidence],
                "validations": [item.value for item in validations],
                "components": components.value,
            }
        ),
        context_ref_ids=(
            *(item.ref_id for item in excerpts),
            *(item.ref_id for item in context_records),
            *(item.ref_id for item in accepted_facts),
        ),
        expected_answer_schema="unit-cohesion-v1",
        attempt=1,
    )
    accept_example = {
        "decision": "accept",
        "reason": "One runtime and one cohesive deliverable.",
    }
    return QuestionRequest(
        question=question,
        exact_question=(
            "Check whether this one work unit represents one cohesive implementation "
            "step in one runtime. Accept it, or split it into two to four replacement "
            "proposals that preserve every requirement and validation obligation."
        ),
        answer_field_definitions={
            "decision": "accept or split.",
            "replacements": (
                "Required only for split; two to four full WorkUnitProposal records."
            ),
            "reason": "Short explanation of the cohesion decision.",
        },
        source_excerpts=excerpts,
        repository_evidence=context_records,
        accepted_facts=accepted_facts,
        valid_example=accept_example,
        empty_example=accept_example,
        answer_schema={
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["decision", "reason"],
                    "properties": {
                        "decision": {"const": "accept"},
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
                    "required": ["decision", "replacements", "reason"],
                    "properties": {
                        "decision": {"const": "split"},
                        "replacements": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": WORK_UNIT_PROPOSAL_PAGE_JSON_SCHEMA["properties"][
                                "records"
                            ]["items"],
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1_000,
                        },
                    },
                },
            ]
        },
        parse_answer=lambda value: parse_cohesion_decision(
            value, original=proposal, context=context
        ),
    )


def parse_item_description(
    value: Mapping[str, Any], *, group: AllocationGroup
) -> ItemDescription:
    _exact_fields(value, {"title", "objective", "scope"}, "/")
    title = _text(value["title"], "/title", max_length=160)
    objective = _text(value["objective"], "/objective", max_length=1_000)
    raw_scope = _array(
        value["scope"], "/scope", minimum=1, maximum=MAX_ITEM_SCOPE_ENTRIES
    )
    scope: list[ItemScope] = []
    mentioned: list[str] = []
    known = set(group.unit_ids)
    for index, item in enumerate(raw_scope):
        pointer = f"/scope/{index}"
        if not isinstance(item, Mapping):
            raise PlanningContractError(pointer, "must be an object")
        _exact_fields(item, {"description", "unit_ids"}, pointer)
        refs = _reference_list(
            item["unit_ids"], f"{pointer}/unit_ids", known=known, minimum=1
        )
        mentioned.extend(refs)
        scope.append(
            ItemScope(
                _text(item["description"], f"{pointer}/description", max_length=500),
                refs,
            )
        )
    if set(mentioned) != known or len(mentioned) != len(set(mentioned)):
        raise PlanningContractError(
            "/scope", "must cover each allocated unit exactly once"
        )
    return ItemDescription(title, objective, tuple(scope))


def build_item_description_request(
    group: AllocationGroup, units: Mapping[str, WorkUnit]
) -> QuestionRequest[ItemDescription]:
    """Build the singular, frozen-group item-description question."""
    selected = [units[unit_id] for unit_id in group.unit_ids]
    accepted = tuple(ContextRecord(unit.id, unit.to_data()) for unit in selected)
    context_ids = tuple(record.ref_id for record in accepted)
    question = Question(
        question_id=f"describe-{group.id}",
        kind="describe_item",
        contract_version=1,
        subject_ids=(group.id,),
        input_digest=_digest(
            {
                "group": group.__dict__
                if hasattr(group, "__dict__")
                else {"id": group.id, "unit_ids": group.unit_ids}
            }
        ),
        context_ref_ids=context_ids,
        expected_answer_schema="describe-item-v1",
        attempt=1,
    )
    example = {
        "title": "Implement bounded work-unit group",
        "objective": "Deliver the allocated implementation units.",
        "scope": [
            {
                "description": "Implement and validate the grouped units.",
                "unit_ids": list(group.unit_ids),
            }
        ],
    }
    return QuestionRequest(
        question=question,
        exact_question=(
            "Describe this frozen CP-SAT group as a reviewable implementation item. "
            "Do not add, remove, or reorder work units. Return concise title, "
            "objective, and scope."
        ),
        answer_field_definitions={
            "title": "Specific, outcome-oriented PR title.",
            "objective": "One concise statement of the group's result.",
            "scope": (
                "One to three scope records that assign every supplied unit "
                "exactly once."
            ),
        },
        source_excerpts=(),
        repository_evidence=(),
        accepted_facts=accepted,
        valid_example=example,
        empty_example=example,
        answer_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "objective", "scope"],
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 160},
                "objective": {"type": "string", "minLength": 1, "maxLength": 1000},
                "scope": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_ITEM_SCOPE_ENTRIES,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["description", "unit_ids"],
                        "properties": {
                            "description": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 500,
                            },
                            "unit_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        parse_answer=lambda value: parse_item_description(value, group=group),
    )


def _digest(value: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), default=list
            ).encode()
        ).hexdigest()
    )


def _validate_path_shape(path: str, pointer: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path
        or parsed.is_absolute()
        or ".." in parsed.parts
        or "." in parsed.parts
        or "\\" in path
    ):
        raise PlanningContractError(
            pointer, "must be a normalized repository-relative path"
        )
    if str(parsed) != path:
        raise PlanningContractError(
            pointer, "must be a normalized repository-relative path"
        )


def _exact_fields(value: Mapping[str, Any], expected: set[str], pointer: str) -> None:
    actual = set(value)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        detail = f"unknown fields {unknown}" if unknown else f"missing fields {missing}"
        raise PlanningContractError(pointer, detail)


def _array(value: Any, pointer: str, *, minimum: int = 0, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        raise PlanningContractError(pointer, "must be an array")
    if not minimum <= len(value) <= maximum:
        raise PlanningContractError(
            pointer, f"must contain between {minimum} and {maximum} entries"
        )
    return value


def _text(
    value: Any, pointer: str, *, max_length: int = MAX_CONTRACT_TEXT_LENGTH
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise PlanningContractError(
            pointer, f"must be nonempty text of at most {max_length} characters"
        )
    if _PLACEHOLDER_RE.search(value):
        raise PlanningContractError(pointer, "contains an unresolved placeholder")
    return value.strip()


def _text_list(
    value: Any, pointer: str, *, minimum: int, maximum: int
) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{pointer}/{index}")
        for index, item in enumerate(
            _array(value, pointer, minimum=minimum, maximum=maximum)
        )
    )


def _reference_list(
    value: Any, pointer: str, *, known: set[str], minimum: int
) -> tuple[str, ...]:
    result = _text_list(
        value, pointer, minimum=minimum, maximum=MAX_ENUMERATION_RECORDS
    )
    if len(set(result)) != len(result):
        raise PlanningContractError(pointer, "references must be unique")
    unknown = set(result) - known
    if unknown:
        raise PlanningContractError(pointer, f"unknown references: {sorted(unknown)}")
    return result


def _bool(value: Any, pointer: str) -> bool:
    if not isinstance(value, bool):
        raise PlanningContractError(pointer, "must be a boolean")
    return value


def _invalid_record(pointer: str) -> Any:
    raise PlanningContractError(pointer, "must be an object")
