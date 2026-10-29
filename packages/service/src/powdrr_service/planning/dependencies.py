"""Typed dependency, validation, and human-setup planning contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from powdrr_service.planning.assembly import WorkUnit
from powdrr_service.planning.contracts import (
    MAX_CONTRACT_TEXT_LENGTH,
    MAX_ENUMERATION_RECORDS,
    PlanningContractError,
    Question,
)
from powdrr_service.planning.evidence import Evidence
from powdrr_service.planning.questions import (
    ContextRecord,
    QuestionRequest,
    SourceExcerpt,
)
from powdrr_service.planning.source import Requirement, SegmentedSource

_VALIDATION_KINDS = frozenset({"local_command", "ci_check", "manual_observation"})
_SETUP_TRIGGER: Literal["after_implementation_before_validation"] = (
    "after_implementation_before_validation"
)
_SENTINEL_RE = re.compile(
    r"\b(?:none|no (?:manual )?prerequisite|not applicable|n/?a)\b", re.IGNORECASE
)
VALIDATION_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["description", "kind", "target", "source_refs", "evidence_ids"],
    "properties": {
        "description": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CONTRACT_TEXT_LENGTH,
        },
        "kind": {"type": "string", "enum": sorted(_VALIDATION_KINDS)},
        "target": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CONTRACT_TEXT_LENGTH,
        },
        "source_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
}
VALIDATION_PAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["records", "has_more", "next_anchor"],
    "properties": {
        "records": {
            "type": "array",
            "maxItems": MAX_ENUMERATION_RECORDS,
            "items": VALIDATION_RECORD_SCHEMA,
        },
        "has_more": {"type": "boolean"},
        "next_anchor": {"type": ["string", "null"]},
    },
}
VALIDATION_COVERAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["covered", "validation_ids", "reason"],
    "properties": {
        "covered": {"type": "boolean"},
        "validation_ids": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
    },
}
HUMAN_SETUP_NEED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["required", "reason", "source_refs", "evidence_ids"],
    "properties": {
        "required": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "source_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
}
MANUAL_SETUP_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "instructions", "verification", "source_refs"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "instructions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_CONTRACT_TEXT_LENGTH,
            },
        },
        "verification": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CONTRACT_TEXT_LENGTH,
        },
        "source_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    },
}
HUMAN_SETUP_DESCRIPTION_SCHEMA = MANUAL_SETUP_PROPOSAL_SCHEMA
OUTPUT_RESOLUTION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["result", "unit_id", "reason"],
            "properties": {
                "result": {"const": "unit"},
                "unit_id": {"type": "string"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["result", "evidence_ids", "description", "reason"],
            "properties": {
                "result": {"const": "external"},
                "evidence_ids": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_CONTRACT_TEXT_LENGTH,
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["result", "reason"],
            "properties": {
                "result": {"const": "unresolved"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
            },
        },
    ]
}
DEPENDENCY_CONFIRMATION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["result", "output", "requirement_ids", "reason"],
            "properties": {
                "result": {"const": "required"},
                "output": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_CONTRACT_TEXT_LENGTH,
                },
                "requirement_ids": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["result", "reason"],
            "properties": {
                "result": {"const": "independent"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
            },
        },
    ]
}
SETUP_EQUIVALENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["equivalent", "reason"],
    "properties": {
        "equivalent": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
    },
}


@dataclass(frozen=True, slots=True)
class ValidationProposal:
    description: str
    kind: Literal["local_command", "ci_check", "manual_observation"]
    target: str
    source_refs: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValidationDescriptionPage:
    records: tuple[ValidationProposal, ...]
    has_more: bool
    next_anchor: str | None


@dataclass(frozen=True, slots=True)
class ValidationCoverageDecision:
    covered: bool
    validation_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class Validation:
    id: str
    description: str
    kind: Literal["local_command", "ci_check", "manual_observation"]
    target: str
    requirement_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "kind": self.kind,
            "target": self.target,
            "requirement_ids": list(self.requirement_ids),
            "source_refs": list(self.source_refs),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class HumanSetupNeed:
    required: bool
    reason: str
    source_refs: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManualSetupProposal:
    title: str
    instructions: tuple[str, ...]
    verification: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManualSetup:
    id: str
    requirement_ids: tuple[str, ...]
    owner: Literal["human"]
    title: str
    instructions: tuple[str, ...]
    verification: str
    trigger: Literal["after_implementation_before_validation"]
    unblocks_validation_ids: tuple[str, ...]
    source_refs: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requirement_ids": list(self.requirement_ids),
            "owner": self.owner,
            "title": self.title,
            "instructions": list(self.instructions),
            "verification": self.verification,
            "trigger": self.trigger,
            "unblocks_validation_ids": list(self.unblocks_validation_ids),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True, slots=True)
class DependencyCandidate:
    unit_id: str
    deliverable: str
    requirement_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnitDependencyEdge:
    before_unit_id: str
    after_unit_id: str
    output: str
    reason: str
    requirement_ids: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "before_unit_id": self.before_unit_id,
            "after_unit_id": self.after_unit_id,
            "output": self.output,
            "reason": self.reason,
            "requirement_ids": list(self.requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class ItemDependencyEdge:
    before_item_id: str
    after_item_id: str
    reason: str
    requirement_ids: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "before_item_id": self.before_item_id,
            "after_item_id": self.after_item_id,
            "reason": self.reason,
            "requirement_ids": list(self.requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    unit_edges: tuple[UnitDependencyEdge, ...]
    item_edges: tuple[ItemDependencyEdge, ...]
    unit_order: tuple[str, ...]
    item_order: tuple[str, ...]
    ready_item_ids: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "unit_edges": [edge.to_data() for edge in self.unit_edges],
            "item_edges": [edge.to_data() for edge in self.item_edges],
            "unit_order": list(self.unit_order),
            "item_order": list(self.item_order),
            "ready_item_ids": list(self.ready_item_ids),
        }


@dataclass(frozen=True, slots=True)
class OutputResolution:
    result: Literal["unit", "external", "unresolved"]
    unit_id: str | None
    description: str | None
    evidence_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class DependencyConfirmation:
    required: bool
    output: str | None
    requirement_ids: tuple[str, ...]
    reason: str


def bind_confirmed_edge(
    producer_unit_id: str,
    consumer_unit_id: str,
    confirmation: DependencyConfirmation,
) -> UnitDependencyEdge | None:
    """Turn a required confirmation into an explicit producer-to-consumer edge."""
    if not confirmation.required:
        return None
    if producer_unit_id == consumer_unit_id:
        raise DependencyError("a unit cannot depend on itself")
    if confirmation.output is None or not confirmation.requirement_ids:
        raise DependencyError("required dependency confirmation is incomplete")
    return UnitDependencyEdge(
        producer_unit_id,
        consumer_unit_id,
        confirmation.output,
        confirmation.reason,
        confirmation.requirement_ids,
    )


def bind_unit_prerequisites(
    units: Sequence[WorkUnit], edges: Sequence[UnitDependencyEdge]
) -> tuple[WorkUnit, ...]:
    """Copy confirmed producer IDs into each consumer WorkUnit record."""
    unit_ids = {unit.id for unit in units}
    prerequisites: dict[str, set[str]] = {unit.id: set() for unit in units}
    for edge in edges:
        if edge.before_unit_id not in unit_ids or edge.after_unit_id not in unit_ids:
            raise DependencyError("prerequisite edge references an unknown unit")
        if edge.before_unit_id == edge.after_unit_id:
            raise DependencyError("a unit cannot depend on itself")
        prerequisites[edge.after_unit_id].add(edge.before_unit_id)
    return tuple(
        replace(unit, prerequisite_unit_ids=tuple(sorted(prerequisites[unit.id])))
        for unit in units
    )


@dataclass(frozen=True, slots=True)
class SetupEquivalence:
    equivalent: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CompiledSetupOwnership:
    manual_prerequisites: Mapping[str, Mapping[str, Any]]
    added_item_edges: tuple[ItemDependencyEdge, ...]


class DependencyError(ValueError):
    """Invalid planning dependency, validation, or setup record."""


class DependencyCycleError(DependencyError):
    """A confirmed dependency cycle that requires plan repair."""

    def __init__(self, nodes: Sequence[str]) -> None:
        self.nodes = tuple(nodes)
        super().__init__("dependency cycle involves: " + ", ".join(self.nodes))


def parse_validation_proposal(
    value: Mapping[str, Any],
    *,
    source_ids: set[str],
    evidence_ids: set[str],
) -> ValidationProposal:
    _exact(value, {"description", "kind", "target", "source_refs", "evidence_ids"})
    kind = _text(value["kind"], "/kind", max_length=40)
    if kind not in _VALIDATION_KINDS:
        raise PlanningContractError("/kind", "unknown validation kind")
    description = _text(value["description"], "/description")
    target = _text(value["target"], "/target")
    return ValidationProposal(
        description,
        kind,  # type: ignore[arg-type]
        target,
        _refs(value["source_refs"], "/source_refs", source_ids, minimum=1),
        _refs(value["evidence_ids"], "/evidence_ids", evidence_ids, minimum=0),
    )


def bind_validation(
    validation_id: str,
    proposal: ValidationProposal,
    *,
    requirement_ids: Sequence[str],
    known_requirement_ids: set[str],
) -> Validation:
    requirement_refs = _unique_tuple(requirement_ids, "/requirement_ids", minimum=1)
    if set(requirement_refs) - known_requirement_ids:
        raise PlanningContractError(
            "/requirement_ids", "must reference known requirements"
        )
    return Validation(
        _identifier(validation_id, "/id"),
        proposal.description,
        proposal.kind,
        proposal.target,
        requirement_refs,
        proposal.source_refs,
        proposal.evidence_ids,
    )


def parse_validation_description_page(
    value: Mapping[str, Any],
    *,
    source_ids: set[str],
    evidence_ids: set[str],
    anchor_order: Sequence[str],
    after_anchor: str | None = None,
) -> ValidationDescriptionPage:
    _exact(value, {"records", "has_more", "next_anchor"})
    raw_records = _array(
        value["records"], "/records", minimum=0, maximum=MAX_ENUMERATION_RECORDS
    )
    records = tuple(
        parse_validation_proposal(
            item, source_ids=source_ids, evidence_ids=evidence_ids
        )
        if isinstance(item, Mapping)
        else _invalid(f"/records/{index}")
        for index, item in enumerate(raw_records)
    )
    has_more = value["has_more"]
    if not isinstance(has_more, bool):
        raise PlanningContractError("/has_more", "must be a boolean")
    next_anchor = value["next_anchor"]
    if has_more:
        cursor = -1 if after_anchor is None else _position(anchor_order, after_anchor)
        if not isinstance(next_anchor, str):
            raise PlanningContractError(
                "/next_anchor", "must advance within the supplied range"
            )
        if _position(anchor_order, next_anchor) <= cursor:
            raise PlanningContractError(
                "/next_anchor", "must advance beyond the page cursor"
            )
    elif next_anchor is not None:
        raise PlanningContractError("/next_anchor", "must be null on the final page")
    return ValidationDescriptionPage(records, has_more, next_anchor)


def parse_validation_coverage(
    value: Mapping[str, Any], *, validation_ids: set[str]
) -> ValidationCoverageDecision:
    _exact(value, {"covered", "validation_ids", "reason"})
    covered = value["covered"]
    if not isinstance(covered, bool):
        raise PlanningContractError("/covered", "must be a boolean")
    refs = _refs(
        value["validation_ids"],
        "/validation_ids",
        validation_ids,
        minimum=1 if covered else 0,
    )
    if not covered and refs:
        raise PlanningContractError(
            "/validation_ids", "must be empty when covered is false"
        )
    return ValidationCoverageDecision(
        covered, refs, _text(value["reason"], "/reason", max_length=1_000)
    )


def parse_human_setup_need(
    value: Mapping[str, Any], *, source_ids: set[str], evidence_ids: set[str]
) -> HumanSetupNeed:
    _exact(value, {"required", "reason", "source_refs", "evidence_ids"})
    required = value["required"]
    if not isinstance(required, bool):
        raise PlanningContractError("/required", "must be a boolean")
    reason = _text(value["reason"], "/reason", max_length=1_000)
    _reject_sentinel(reason, "/reason")
    return HumanSetupNeed(
        required,
        reason,
        _refs(value["source_refs"], "/source_refs", source_ids, minimum=1),
        _refs(value["evidence_ids"], "/evidence_ids", evidence_ids, minimum=0),
    )


def parse_manual_setup_proposal(
    value: Mapping[str, Any], *, source_ids: set[str]
) -> ManualSetupProposal:
    _exact(value, {"title", "instructions", "verification", "source_refs"})
    title = _text(value["title"], "/title", max_length=160)
    verification = _text(value["verification"], "/verification")
    _reject_sentinel(title, "/title")
    _reject_sentinel(verification, "/verification")
    raw_instructions = _array(
        value["instructions"], "/instructions", minimum=1, maximum=20
    )
    instructions = tuple(
        _text(item, f"/instructions/{index}")
        for index, item in enumerate(raw_instructions)
    )
    for index, instruction in enumerate(instructions):
        _reject_sentinel(instruction, f"/instructions/{index}")
    return ManualSetupProposal(
        title,
        instructions,
        verification,
        _refs(value["source_refs"], "/source_refs", source_ids, minimum=1),
    )


def bind_manual_setup(
    setup_id: str,
    proposal: ManualSetupProposal,
    *,
    need: HumanSetupNeed,
    requirement_ids: Sequence[str],
    known_requirement_ids: set[str],
    validation_ids: Sequence[str],
    known_validation_ids: set[str],
) -> ManualSetup:
    if not need.required:
        raise DependencyError("cannot create setup when human setup was not required")
    if not set(proposal.source_refs) <= set(need.source_refs):
        raise DependencyError("setup description must cite accepted setup-need anchors")
    refs = _unique_tuple(validation_ids, "/unblocks_validation_ids", minimum=1)
    if set(refs) - known_validation_ids:
        raise PlanningContractError(
            "/unblocks_validation_ids", "must reference known validation IDs"
        )
    requirement_refs = _unique_tuple(requirement_ids, "/requirement_ids", minimum=1)
    if set(requirement_refs) - known_requirement_ids:
        raise PlanningContractError(
            "/requirement_ids", "must reference known requirements"
        )
    return ManualSetup(
        _identifier(setup_id, "/id"),
        requirement_refs,
        "human",
        proposal.title,
        proposal.instructions,
        proposal.verification,
        _SETUP_TRIGGER,
        refs,
        proposal.source_refs,
    )


def parse_validation_record(
    value: Mapping[str, Any],
    *,
    requirement_ids: set[str],
    source_ids: set[str],
    evidence_ids: set[str],
) -> Validation:
    _exact(
        value,
        {
            "id",
            "description",
            "kind",
            "target",
            "requirement_ids",
            "source_refs",
            "evidence_ids",
        },
    )
    proposal = parse_validation_proposal(
        {
            "description": value["description"],
            "kind": value["kind"],
            "target": value["target"],
            "source_refs": value["source_refs"],
            "evidence_ids": value["evidence_ids"],
        },
        source_ids=source_ids,
        evidence_ids=evidence_ids,
    )
    req_refs = _refs(
        value["requirement_ids"], "/requirement_ids", requirement_ids, minimum=1
    )
    return bind_validation(
        _identifier(value["id"], "/id"),
        proposal,
        requirement_ids=req_refs,
        known_requirement_ids=requirement_ids,
    )


def parse_manual_setup_record(
    value: Mapping[str, Any],
    *,
    requirement_ids: set[str],
    validation_ids: set[str],
    source_ids: set[str],
) -> ManualSetup:
    _exact(
        value,
        {
            "id",
            "requirement_ids",
            "owner",
            "title",
            "instructions",
            "verification",
            "trigger",
            "unblocks_validation_ids",
            "source_refs",
        },
    )
    if value["owner"] != "human":
        raise PlanningContractError("/owner", "must be human")
    if value["trigger"] != _SETUP_TRIGGER:
        raise PlanningContractError("/trigger", "unsupported manual setup trigger")
    proposal = parse_manual_setup_proposal(
        {
            "title": value["title"],
            "instructions": value["instructions"],
            "verification": value["verification"],
            "source_refs": value["source_refs"],
        },
        source_ids=source_ids,
    )
    return ManualSetup(
        _identifier(value["id"], "/id"),
        _refs(value["requirement_ids"], "/requirement_ids", requirement_ids, minimum=1),
        "human",
        proposal.title,
        proposal.instructions,
        proposal.verification,
        _SETUP_TRIGGER,
        _refs(
            value["unblocks_validation_ids"],
            "/unblocks_validation_ids",
            validation_ids,
            minimum=1,
        ),
        proposal.source_refs,
    )


def parse_output_resolution(
    value: Mapping[str, Any],
    *,
    consumer_unit_id: str,
    candidate_unit_ids: set[str],
    evidence_ids: set[str],
) -> OutputResolution:
    if not isinstance(value, Mapping):
        raise PlanningContractError("/", "must be an object")
    tag = value.get("result")
    if tag == "unit":
        _exact(value, {"result", "unit_id", "reason"})
        unit_id = _text(value["unit_id"], "/unit_id", max_length=120)
        if unit_id not in candidate_unit_ids:
            raise PlanningContractError(
                "/unit_id", "must reference a supplied candidate unit"
            )
        if unit_id == consumer_unit_id:
            raise PlanningContractError("/unit_id", "a unit cannot depend on itself")
        return OutputResolution(
            "unit",
            unit_id,
            None,
            (),
            _text(value["reason"], "/reason", max_length=1_000),
        )
    if tag == "external":
        _exact(value, {"result", "evidence_ids", "description", "reason"})
        refs = _refs(value["evidence_ids"], "/evidence_ids", evidence_ids, minimum=1)
        description = _text(value["description"], "/description")
        reason = _text(value["reason"], "/reason", max_length=1_000)
        return OutputResolution("external", None, description, refs, reason)
    if tag == "unresolved":
        _exact(value, {"result", "reason"})
        return OutputResolution(
            "unresolved",
            None,
            None,
            (),
            _text(value["reason"], "/reason", max_length=1_000),
        )
    raise PlanningContractError("/result", "must be unit, external, or unresolved")


def parse_dependency_confirmation(
    value: Mapping[str, Any],
    *,
    allowed_requirement_ids: set[str],
) -> DependencyConfirmation:
    if not isinstance(value, Mapping):
        raise PlanningContractError("/", "must be an object")
    tag = value.get("result")
    if tag == "required":
        _exact(value, {"result", "output", "requirement_ids", "reason"})
        return DependencyConfirmation(
            True,
            _text(value["output"], "/output"),
            _refs(
                value["requirement_ids"],
                "/requirement_ids",
                allowed_requirement_ids,
                minimum=1,
            ),
            _text(value["reason"], "/reason", max_length=1_000),
        )
    if tag == "independent":
        _exact(value, {"result", "reason"})
        return DependencyConfirmation(
            False, None, (), _text(value["reason"], "/reason", max_length=1_000)
        )
    raise PlanningContractError("/result", "must be required or independent")


def parse_setup_equivalence(value: Mapping[str, Any]) -> SetupEquivalence:
    _exact(value, {"equivalent", "reason"})
    equivalent = value["equivalent"]
    if not isinstance(equivalent, bool):
        raise PlanningContractError("/equivalent", "must be a boolean")
    return SetupEquivalence(
        equivalent, _text(value["reason"], "/reason", max_length=1_000)
    )


def build_setup_equivalence_request(
    first: ManualSetup, second: ManualSetup
) -> QuestionRequest[SetupEquivalence]:
    if first.id == second.id:
        raise DependencyError("setup equivalence needs two different setup records")
    facts = (
        ContextRecord(f"setup:{first.id}", first.to_data()),
        ContextRecord(f"setup:{second.id}", second.to_data()),
    )
    example = {
        "equivalent": False,
        "reason": "The actions affect different repository resources.",
    }
    return QuestionRequest(
        question=Question(
            f"setup-equivalence-{first.id}-{second.id}",
            "compare_setup_equivalence",
            1,
            (first.id, second.id),
            _digest({"first": first.to_data(), "second": second.to_data()}),
            tuple(item.ref_id for item in facts),
            "setup-equivalence-v1",
            1,
        ),
        exact_question=(
            "Are these two human setup proposals the same external action, with "
            "the same owner and timing, so one setup can unblock both validations? "
            "Compare the actual resource and required action, not just similar wording."
        ),
        answer_field_definitions={
            "equivalent": (
                "True only when completing one action satisfies both setup needs."
            ),
            "reason": "Explain the concrete resource/action comparison.",
        },
        source_excerpts=(),
        repository_evidence=(),
        accepted_facts=facts,
        valid_example=example,
        empty_example=example,
        answer_schema=SETUP_EQUIVALENCE_SCHEMA,
        parse_answer=parse_setup_equivalence,
    )


def build_validation_description_request(
    requirement: Requirement,
    *,
    source: SegmentedSource,
    evidence: Mapping[str, Evidence],
    anchor_order: Sequence[str],
    valid_example: Mapping[str, Any],
    after_anchor: str | None = None,
) -> QuestionRequest[ValidationDescriptionPage]:
    excerpts = _source_excerpts(source, requirement.source_refs)
    evidence_records = tuple(
        ContextRecord(f"evidence:{key}", value.to_data())
        for key, value in sorted(evidence.items())
    )
    requirement_fact = ContextRecord(
        f"requirement:{requirement.id}",
        {"id": requirement.id, "statement": requirement.statement},
    )
    context_refs = (
        *(item.ref_id for item in excerpts),
        *(item.ref_id for item in evidence_records),
        requirement_fact.ref_id,
    )

    def parse(value: Mapping[str, Any]) -> ValidationDescriptionPage:
        return parse_validation_description_page(
            value,
            source_ids={item.ref_id for item in excerpts},
            evidence_ids=set(evidence),
            anchor_order=anchor_order,
            after_anchor=after_anchor,
        )

    return QuestionRequest(
        question=Question(
            question_id=(
                f"validations-{requirement.id}-"
                f"{_digest({'after_anchor': after_anchor})[-12:]}"
            ),
            kind="describe_requirement_validations",
            contract_version=1,
            subject_ids=(requirement.id,),
            input_digest=_digest(
                {
                    "requirement": requirement.statement,
                    "source": [(item.ref_id, item.text) for item in excerpts],
                    "evidence": [item.value for item in evidence_records],
                    "cursor": after_anchor,
                }
            ),
            context_ref_ids=context_refs,
            expected_answer_schema="validation-descriptions-v1",
            attempt=1,
        ),
        exact_question=(
            "Describe concrete observations that establish whether this one "
            "requirement is satisfied. Prefer existing repository commands, CI "
            "checks, or observable behavior. Do not propose tests for unrelated "
            "requirements. Return at most eight validation records."
        ),
        answer_field_definitions={
            "description": "What the validation establishes.",
            "kind": "local_command, ci_check, or manual_observation.",
            "target": "The exact command, CI check, or observable target.",
            "source_refs": "Cited source anchors supporting this validation.",
            "evidence_ids": "Supplied repository evidence IDs used to choose it.",
        },
        source_excerpts=excerpts,
        repository_evidence=evidence_records,
        accepted_facts=(requirement_fact,),
        valid_example=valid_example,
        empty_example={"records": [], "has_more": False, "next_anchor": None},
        answer_schema=VALIDATION_PAGE_SCHEMA,
        parse_answer=parse,
    )


def build_validation_coverage_request(
    criterion_id: str,
    criterion_statement: str,
    validations: Mapping[str, Validation],
) -> QuestionRequest[ValidationCoverageDecision]:
    records = tuple(
        ContextRecord(f"validation:{key}", value.to_data())
        for key, value in sorted(validations.items())
    )
    criterion = ContextRecord(
        f"criterion:{criterion_id}",
        {"id": criterion_id, "statement": criterion_statement},
    )
    facts = (criterion, *records)
    context_refs = tuple(item.ref_id for item in facts)
    available_ids = tuple(validations)
    valid = (
        {
            "covered": True,
            "validation_ids": [available_ids[0]],
            "reason": "The command directly checks the criterion behavior.",
        }
        if available_ids
        else {
            "covered": False,
            "validation_ids": [],
            "reason": "No supplied validation observes this criterion.",
        }
    )
    empty = {
        "covered": False,
        "validation_ids": [],
        "reason": "No supplied validation observes this criterion.",
    }
    return QuestionRequest(
        question=Question(
            f"validation-coverage-{criterion_id}",
            "check_validation_coverage",
            1,
            (criterion_id,),
            _digest(
                {
                    "criterion": criterion_statement,
                    "validations": [item.value for item in records],
                }
            ),
            context_refs,
            "validation-coverage-v1",
            1,
        ),
        exact_question=(
            "Do the supplied validations directly establish this acceptance "
            "criterion? Select only validations that observe its behavior or "
            "explicitly check its outcome. If none do, say uncovered so the "
            "planner can request one focused validation description."
        ),
        answer_field_definitions={
            "covered": (
                "True only when one or more supplied validations establish "
                "the criterion."
            ),
            "validation_ids": "Supplied validation IDs; empty when covered is false.",
            "reason": "Explain the connection or the missing observation.",
        },
        source_excerpts=(),
        repository_evidence=(),
        accepted_facts=facts,
        valid_example=valid,
        empty_example=empty,
        answer_schema=VALIDATION_COVERAGE_SCHEMA,
        parse_answer=lambda value: parse_validation_coverage(
            value, validation_ids=set(validations)
        ),
    )


def build_requires_human_setup_request(
    validation: Validation,
    *,
    requirements: Mapping[str, Requirement],
    source: SegmentedSource,
    evidence: Mapping[str, Evidence],
) -> QuestionRequest[HumanSetupNeed]:
    missing_requirements = set(validation.requirement_ids) - set(requirements)
    if missing_requirements:
        raise DependencyError(
            "validation references unknown requirements: "
            f"{sorted(missing_requirements)}"
        )
    excerpts = _source_excerpts(source, validation.source_refs)
    used_evidence = tuple(
        ContextRecord(f"evidence:{key}", evidence[key].to_data())
        for key in validation.evidence_ids
        if key in evidence
    )
    missing_evidence = set(validation.evidence_ids) - set(evidence)
    if missing_evidence:
        raise DependencyError(
            f"validation references unavailable evidence: {sorted(missing_evidence)}"
        )
    facts = (
        ContextRecord(f"validation:{validation.id}", validation.to_data()),
        *(
            ContextRecord(
                f"requirement:{req_id}",
                {"id": req_id, "statement": requirements[req_id].statement},
            )
            for req_id in validation.requirement_ids
        ),
    )
    context_refs = (
        *(item.ref_id for item in excerpts),
        *(item.ref_id for item in used_evidence),
        *(item.ref_id for item in facts),
    )
    valid = {
        "required": False,
        "reason": "The existing local validation runs within the worker's authority.",
        "source_refs": list(validation.source_refs),
        "evidence_ids": list(validation.evidence_ids),
    }
    return QuestionRequest(
        question=Question(
            f"human-setup-need-{validation.id}",
            "requires_human_setup",
            1,
            (validation.id,),
            _digest({"validation": validation.to_data(), "context": context_refs}),
            context_refs,
            "human-setup-need-v1",
            1,
        ),
        exact_question=(
            "Does this validation require a concrete action outside the worker's "
            "authorized capabilities before it can run? Ordinary code changes, "
            "local tests, dependency ordering, and routine PR review do not count. "
            "Repository-readable absence alone does not prove remote setup is absent."
        ),
        answer_field_definitions={
            "required": (
                "True only when a human must perform a real external setup action."
            ),
            "reason": "Explain why setup is or is not required for this validation.",
            "source_refs": "One or more supplied source anchors.",
            "evidence_ids": (
                "Supplied evidence used for this determination; may be empty."
            ),
        },
        source_excerpts=excerpts,
        repository_evidence=used_evidence,
        accepted_facts=facts,
        valid_example=valid,
        empty_example=valid,
        answer_schema=HUMAN_SETUP_NEED_SCHEMA,
        parse_answer=lambda value: parse_human_setup_need(
            value,
            source_ids={item.ref_id for item in excerpts},
            evidence_ids=set(validation.evidence_ids),
        ),
    )


def build_human_setup_description_request(
    validation: Validation,
    need: HumanSetupNeed,
    *,
    source: SegmentedSource,
    evidence: Mapping[str, Evidence],
) -> QuestionRequest[ManualSetupProposal]:
    if not need.required:
        raise DependencyError(
            "setup description cannot be requested when setup is not required"
        )
    refs = tuple(dict.fromkeys((*validation.source_refs, *need.source_refs)))
    excerpts = _source_excerpts(source, refs)
    facts = (
        ContextRecord(f"validation:{validation.id}", validation.to_data()),
        ContextRecord(
            f"setup-need:{validation.id}",
            {
                "reason": need.reason,
                "requirement_ids": list(validation.requirement_ids),
                "validation_id": validation.id,
            },
        ),
    )
    missing_evidence = set(need.evidence_ids) - set(evidence)
    if missing_evidence:
        raise DependencyError(
            "setup description references unavailable evidence: "
            f"{sorted(missing_evidence)}"
        )
    evidence_records = tuple(
        ContextRecord(f"evidence:{evidence_id}", evidence[evidence_id].to_data())
        for evidence_id in need.evidence_ids
    )
    context_refs = (
        *(item.ref_id for item in excerpts),
        *(item.ref_id for item in evidence_records),
        *(item.ref_id for item in facts),
    )
    example = {
        "title": "Install and configure the required integration",
        "instructions": [
            "Register the integration.",
            "Install it in the target repository.",
        ],
        "verification": "Confirm the integration check can access the installation.",
        "source_refs": list(need.source_refs),
    }
    return QuestionRequest(
        question=Question(
            f"describe-human-setup-{validation.id}",
            "describe_human_setup",
            1,
            (validation.id,),
            _digest(
                {
                    "validation": validation.to_data(),
                    "need": need.reason,
                    "source": context_refs,
                }
            ),
            context_refs,
            "manual-setup-description-v1",
            1,
        ),
        exact_question=(
            "Describe only the concrete human action needed before this validation. "
            "Give ordered instructions and a verifiable completion check. Do not "
            "choose setup IDs, owners, triggers, PR IDs, or validation IDs."
        ),
        answer_field_definitions={
            "title": "Short imperative setup title.",
            "instructions": "Ordered, actionable human steps.",
            "verification": "How a human confirms setup is complete.",
            "source_refs": "Source anchors that justify this setup description.",
        },
        source_excerpts=excerpts,
        repository_evidence=evidence_records,
        accepted_facts=facts,
        valid_example=example,
        empty_example=example,
        answer_schema=MANUAL_SETUP_PROPOSAL_SCHEMA,
        parse_answer=lambda value: parse_manual_setup_proposal(
            value, source_ids={item.ref_id for item in excerpts}
        ),
    )


def build_output_resolution_request(
    consumer_unit_id: str,
    output_description: str,
    candidates: Sequence[DependencyCandidate],
    *,
    evidence: Mapping[str, Evidence],
    page_ordinal: int,
) -> QuestionRequest[OutputResolution]:
    if not 1 <= len(candidates) <= MAX_ENUMERATION_RECORDS:
        raise ValueError("output resolution needs one to eight candidates per page")
    if len({item.unit_id for item in candidates}) != len(candidates):
        raise ValueError("output candidates must have unique unit IDs")
    facts = tuple(
        ContextRecord(
            f"candidate:{item.unit_id}",
            {
                "unit_id": item.unit_id,
                "deliverable": item.deliverable,
                "requirement_ids": list(item.requirement_ids),
            },
        )
        for item in candidates
    )
    evidence_records = tuple(
        ContextRecord(f"evidence:{key}", value.to_data())
        for key, value in sorted(evidence.items())
    )
    output_fact = ContextRecord(
        f"required-output:{consumer_unit_id}",
        {"consumer_unit_id": consumer_unit_id, "description": output_description},
    )
    context_refs = (
        output_fact.ref_id,
        *(item.ref_id for item in facts),
        *(item.ref_id for item in evidence_records),
    )
    return QuestionRequest(
        question=Question(
            f"resolve-output-{consumer_unit_id}-{page_ordinal:04d}",
            "resolve_prerequisite_output",
            1,
            (consumer_unit_id,),
            _digest(
                {
                    "consumer": consumer_unit_id,
                    "output": output_description,
                    "candidates": context_refs,
                }
            ),
            context_refs,
            "prerequisite-output-resolution-v1",
            1,
        ),
        exact_question=(
            "Can this required output be supplied by one of these candidate work "
            "units? Select a candidate only if it provides the named output. If it "
            "is external, cite supplied evidence. If this page cannot resolve it, "
            "return unresolved so the scheduler can inspect the next page."
        ),
        answer_field_definitions={
            "result": "unit, external, or unresolved.",
            "unit_id": "A supplied candidate unit, only for result=unit.",
            "evidence_ids": "Supplied evidence, required for result=external.",
            "description": "Concrete external output, only for result=external.",
            "reason": "Why this candidate resolves or does not resolve the output.",
        },
        source_excerpts=(),
        repository_evidence=(*facts, *evidence_records),
        accepted_facts=(output_fact,),
        valid_example={
            "result": "unresolved",
            "reason": "No candidate on this page provides the output.",
        },
        empty_example={
            "result": "unresolved",
            "reason": "No candidate on this page provides the output.",
        },
        answer_schema=OUTPUT_RESOLUTION_SCHEMA,
        parse_answer=lambda value: parse_output_resolution(
            value,
            consumer_unit_id=consumer_unit_id,
            candidate_unit_ids={item.unit_id for item in candidates},
            evidence_ids=set(evidence),
        ),
    )


def build_dependency_confirmation_request(
    producer: WorkUnit,
    consumer: WorkUnit,
    output: str,
    *,
    allowed_requirement_ids: set[str],
) -> QuestionRequest[DependencyConfirmation]:
    if producer.id == consumer.id:
        raise DependencyError("producer and consumer must be different units")
    endpoint_requirement_ids = set(producer.requirement_ids) | set(
        consumer.requirement_ids
    )
    if not allowed_requirement_ids <= endpoint_requirement_ids:
        raise DependencyError("dependency question includes unrelated requirement IDs")
    facts = (
        ContextRecord(f"producer:{producer.id}", producer.to_data()),
        ContextRecord(f"consumer:{consumer.id}", consumer.to_data()),
        ContextRecord("candidate-output", {"description": output}),
        ContextRecord(
            "candidate-requirements",
            {"requirement_ids": sorted(allowed_requirement_ids)},
        ),
    )
    context_refs = tuple(item.ref_id for item in facts)
    example = {
        "result": "independent",
        "reason": "The consumer does not use this output.",
    }
    return QuestionRequest(
        question=Question(
            f"confirm-dependency-{producer.id}-{consumer.id}-{_digest(output)[-12:]}",
            "confirm_dependency",
            1,
            (producer.id, consumer.id),
            _digest(
                {
                    "producer": producer.to_data(),
                    "consumer": consumer.to_data(),
                    "output": output,
                }
            ),
            context_refs,
            "dependency-confirmation-v1",
            1,
        ),
        exact_question=(
            f"Does consumer {consumer.id} need the producer's named output before "
            "it can be implemented or validated? Answer required only when there "
            "is a real information or artifact dependency."
        ),
        answer_field_definitions={
            "result": "required or independent.",
            "output": "The exact producer output used by the consumer.",
            "requirement_ids": "Affected supplied requirement IDs.",
            "reason": "Why the dependency is required or why work is independent.",
        },
        source_excerpts=(),
        repository_evidence=(),
        accepted_facts=facts,
        valid_example=example,
        empty_example=example,
        answer_schema=DEPENDENCY_CONFIRMATION_SCHEMA,
        parse_answer=lambda value: parse_dependency_confirmation(
            value, allowed_requirement_ids=allowed_requirement_ids
        ),
    )


def compile_dependency_graph(
    units: Sequence[WorkUnit],
    unit_to_item: Mapping[str, str],
    confirmed_edges: Sequence[UnitDependencyEdge],
    *,
    item_order_hint: Sequence[str],
    ci_bootstrap_item_id: str | None = None,
    ci_bootstrap_requirement_ids: Sequence[str] = (),
) -> DependencyGraph:
    """Validate unit edges, project them to items, and compute stable ready/order."""
    unit_ids = {unit.id for unit in units}
    if set(unit_to_item) != unit_ids:
        raise DependencyError("every unit must map to exactly one item")
    item_ids = set(unit_to_item.values())
    if set(item_order_hint) != item_ids or len(set(item_order_hint)) != len(
        item_order_hint
    ):
        raise DependencyError("item order must list every compiled item exactly once")
    normalized: dict[tuple[str, str], UnitDependencyEdge] = {}
    for edge in confirmed_edges:
        if edge.before_unit_id not in unit_ids or edge.after_unit_id not in unit_ids:
            raise DependencyError("dependency edge references an unknown unit")
        if edge.before_unit_id == edge.after_unit_id:
            raise DependencyError("dependency edge cannot reference the same unit")
        if (
            not edge.output.strip()
            or not edge.reason.strip()
            or not edge.requirement_ids
        ):
            raise DependencyError(
                "dependency edge needs output, reason, and requirements"
            )
        endpoint_requirements = set(
            next(
                unit.requirement_ids for unit in units if unit.id == edge.before_unit_id
            )
        ) | set(
            next(
                unit.requirement_ids for unit in units if unit.id == edge.after_unit_id
            )
        )
        if not set(edge.requirement_ids) <= endpoint_requirements:
            raise DependencyError("dependency edge references unrelated requirements")
        key = (edge.before_unit_id, edge.after_unit_id)
        previous = normalized.get(key)
        if previous is None:
            normalized[key] = edge
        else:
            normalized[key] = UnitDependencyEdge(
                edge.before_unit_id,
                edge.after_unit_id,
                "; ".join(dict.fromkeys((previous.output, edge.output))),
                "; ".join(dict.fromkeys((previous.reason, edge.reason))),
                tuple(
                    dict.fromkeys((*previous.requirement_ids, *edge.requirement_ids))
                ),
            )
    unit_order = _topological_order(
        unit_ids,
        normalized.values(),
        key=lambda edge: (edge.before_unit_id, edge.after_unit_id),
        rank={unit.id: index for index, unit in enumerate(units)},
    )
    projected: dict[tuple[str, str], ItemDependencyEdge] = {}
    for edge in normalized.values():
        before = unit_to_item[edge.before_unit_id]
        after = unit_to_item[edge.after_unit_id]
        if before == after:
            continue
        key = (before, after)
        existing = projected.get(key)
        if existing is None:
            projected[key] = ItemDependencyEdge(
                before, after, edge.reason, edge.requirement_ids
            )
        else:
            projected[key] = ItemDependencyEdge(
                before,
                after,
                "; ".join(dict.fromkeys((existing.reason, edge.reason))),
                tuple(
                    dict.fromkeys((*existing.requirement_ids, *edge.requirement_ids))
                ),
            )
    if ci_bootstrap_item_id is not None:
        if ci_bootstrap_item_id not in item_ids:
            raise DependencyError("CI bootstrap item is unknown")
        if not ci_bootstrap_requirement_ids:
            raise DependencyError("CI bootstrap edges need source requirement IDs")
        all_requirements = {
            requirement_id for unit in units for requirement_id in unit.requirement_ids
        }
        if set(ci_bootstrap_requirement_ids) - all_requirements:
            raise DependencyError("CI bootstrap edge references unknown requirements")
        for item_id in item_ids:
            if item_id != ci_bootstrap_item_id:
                projected.setdefault(
                    (ci_bootstrap_item_id, item_id),
                    ItemDependencyEdge(
                        ci_bootstrap_item_id,
                        item_id,
                        "CI bootstrap is required before product validation.",
                        tuple(dict.fromkeys(ci_bootstrap_requirement_ids)),
                    ),
                )
    rank = {item_id: index for index, item_id in enumerate(item_order_hint)}
    item_order = _topological_order(
        item_ids,
        projected.values(),
        key=lambda edge: (edge.before_item_id, edge.after_item_id),
        rank=rank,
        before_attr="before_item_id",
        after_attr="after_item_id",
    )
    indegree = {item_id: 0 for item_id in item_ids}
    for item_edge in projected.values():
        indegree[item_edge.after_item_id] += 1
    ready = tuple(item_id for item_id in item_order_hint if indegree[item_id] == 0)
    return DependencyGraph(
        tuple(normalized[key] for key in sorted(normalized)),
        tuple(projected[key] for key in sorted(projected)),
        tuple(unit_order),
        tuple(item_order),
        ready,
    )


def compile_manual_setup_ownership(
    setups: Sequence[ManualSetup],
    *,
    validation_to_item: Mapping[str, str],
    validations: Mapping[str, Validation],
    item_order: Sequence[str],
    equivalence_groups: Sequence[Sequence[str]] = (),
) -> CompiledSetupOwnership:
    """Assign shared setup once and project handoffs into ordered item records."""
    setup_ids = {setup.id for setup in setups}
    if len(setup_ids) != len(setups):
        raise DependencyError("setup IDs must be unique")
    if len(set(item_order)) != len(item_order):
        raise DependencyError("item order contains duplicates")
    if set(validation_to_item.values()) - set(item_order):
        raise DependencyError("validation is assigned to an item outside the plan")
    if set(validation_to_item) - set(validations):
        raise DependencyError(
            "validation ownership references unknown validation records"
        )
    order = {item_id: index for index, item_id in enumerate(item_order)}
    parent = {setup_id: setup_id for setup_id in setup_ids}

    def find(setup_id: str) -> str:
        while parent[setup_id] != setup_id:
            parent[setup_id] = parent[parent[setup_id]]
            setup_id = parent[setup_id]
        return setup_id

    for group in equivalence_groups:
        members = tuple(dict.fromkeys(group))
        if len(members) < 2 or set(members) - setup_ids:
            raise DependencyError("equivalence groups must contain known setup IDs")
        root = find(members[0])
        for member in members[1:]:
            other = find(member)
            if root != other:
                low, high = sorted((root, other))
                parent[high] = low
                root = low
    roots = {setup_id: find(setup_id) for setup_id in setup_ids}
    minimum_by_root: dict[str, str] = {}
    for setup_id, root in roots.items():
        minimum_by_root[root] = min(minimum_by_root.get(root, setup_id), setup_id)
    aliases = {setup_id: minimum_by_root[root] for setup_id, root in roots.items()}
    merged: dict[str, list[ManualSetup]] = defaultdict(list)
    by_id = {setup.id: setup for setup in setups}
    for setup_id, owner_id in aliases.items():
        merged[owner_id].append(by_id[setup_id])
    assigned: dict[str, list[ManualSetup]] = defaultdict(list)
    added_edges: dict[tuple[str, str], ItemDependencyEdge] = {}
    for owner_id in sorted(merged):
        related = merged[owner_id]
        validation_ids = tuple(
            dict.fromkeys(
                validation
                for setup in related
                for validation in setup.unblocks_validation_ids
            )
        )
        missing = set(validation_ids) - set(validation_to_item)
        if missing:
            raise DependencyError(
                f"setup references unknown validations: {sorted(missing)}"
            )
        consumers = tuple(
            dict.fromkeys(validation_to_item[item] for item in validation_ids)
        )
        owner_item = min(consumers, key=lambda item: order[item])
        representative = by_id[owner_id]
        assigned[owner_item].append(
            ManualSetup(
                representative.id,
                tuple(
                    dict.fromkeys(
                        req for setup in related for req in setup.requirement_ids
                    )
                ),
                "human",
                representative.title,
                representative.instructions,
                representative.verification,
                _SETUP_TRIGGER,
                validation_ids,
                tuple(
                    dict.fromkeys(ref for setup in related for ref in setup.source_refs)
                ),
            )
        )
        for consumer in consumers:
            if consumer != owner_item:
                key = (owner_item, consumer)
                added_edges[key] = ItemDependencyEdge(
                    owner_item,
                    consumer,
                    f"Shared human setup {owner_id} must be completed first.",
                    tuple(
                        dict.fromkeys(
                            req for setup in related for req in setup.requirement_ids
                        )
                    ),
                )
    compiled: dict[str, Mapping[str, Any]] = {}
    for item_id, records in assigned.items():
        records.sort(key=lambda setup: (order[item_id], setup.id))
        triggers = {setup.trigger for setup in records}
        if triggers != {_SETUP_TRIGGER}:
            raise DependencyError(
                "unsupported human setup trigger requires plan repair"
            )
        titles = tuple(dict.fromkeys(setup.title for setup in records))
        validation_ids = tuple(
            dict.fromkeys(
                validation
                for setup in records
                for validation in setup.unblocks_validation_ids
            )
        )
        compiled[item_id] = {
            "title": "; ".join(titles),
            "instructions": [
                instruction for setup in records for instruction in setup.instructions
            ],
            "verification": "; ".join(
                dict.fromkeys(setup.verification for setup in records)
            ),
            "trigger": _SETUP_TRIGGER,
            "unblocks": "; ".join(
                dict.fromkeys(
                    validations[validation_id].target
                    for validation_id in validation_ids
                )
            ),
        }
    return CompiledSetupOwnership(
        compiled,
        tuple(added_edges[key] for key in sorted(added_edges)),
    )


def apply_setup_ownership(
    graph: DependencyGraph, ownership: CompiledSetupOwnership
) -> DependencyGraph:
    """Add shared-setup edges and recompute item order and the ready set."""
    merged: dict[tuple[str, str], ItemDependencyEdge] = {
        (edge.before_item_id, edge.after_item_id): edge for edge in graph.item_edges
    }
    for edge in ownership.added_item_edges:
        key = (edge.before_item_id, edge.after_item_id)
        previous = merged.get(key)
        if previous is None:
            merged[key] = edge
        else:
            merged[key] = ItemDependencyEdge(
                edge.before_item_id,
                edge.after_item_id,
                "; ".join(dict.fromkeys((previous.reason, edge.reason))),
                tuple(
                    dict.fromkeys((*previous.requirement_ids, *edge.requirement_ids))
                ),
            )
    rank = {item_id: index for index, item_id in enumerate(graph.item_order)}
    ordered = _topological_order(
        set(graph.item_order),
        tuple(merged.values()),
        key=lambda edge: (edge.before_item_id, edge.after_item_id),
        rank=rank,
        before_attr="before_item_id",
        after_attr="after_item_id",
    )
    indegree = {item_id: 0 for item_id in graph.item_order}
    for edge in merged.values():
        indegree[edge.after_item_id] += 1
    ready = tuple(item_id for item_id in graph.item_order if indegree[item_id] == 0)
    return DependencyGraph(
        graph.unit_edges,
        tuple(merged[key] for key in sorted(merged)),
        graph.unit_order,
        tuple(ordered),
        ready,
    )


def _topological_order(
    nodes: set[str],
    edges: Sequence[Any] | Any,
    *,
    key: Any,
    rank: Mapping[str, int],
    before_attr: str = "before_unit_id",
    after_attr: str = "after_unit_id",
) -> list[str]:
    outgoing: dict[str, set[str]] = {node: set() for node in nodes}
    indegree = {node: 0 for node in nodes}
    for edge in edges:
        before = getattr(edge, before_attr)
        after = getattr(edge, after_attr)
        if after not in outgoing[before]:
            outgoing[before].add(after)
            indegree[after] += 1
    ready = [
        (rank.get(node, len(rank)), node)
        for node, degree in indegree.items()
        if degree == 0
    ]
    ready.sort()
    ordered: list[str] = []
    while ready:
        _position, current = ready.pop(0)
        ordered.append(current)
        for successor in sorted(
            outgoing[current], key=lambda node: (rank.get(node, len(rank)), node)
        ):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append((rank.get(successor, len(rank)), successor))
                ready.sort()
    if len(ordered) != len(nodes):
        raise DependencyCycleError(
            sorted(node for node, degree in indegree.items() if degree)
        )
    return ordered


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=list)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _exact(value: Mapping[str, Any], fields: set[str]) -> None:
    actual = set(value)
    if actual != fields:
        unknown, missing = sorted(actual - fields), sorted(fields - actual)
        detail = f"unknown fields {unknown}" if unknown else f"missing fields {missing}"
        raise PlanningContractError("/", detail)


def _text(
    value: Any, pointer: str, *, max_length: int = MAX_CONTRACT_TEXT_LENGTH
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise PlanningContractError(pointer, "must be nonempty bounded text")
    return value.strip()


def _identifier(value: Any, pointer: str) -> str:
    text = _text(value, pointer, max_length=120)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]*", text):
        raise PlanningContractError(pointer, "must be a stable identifier")
    return text


def _array(value: Any, pointer: str, *, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise PlanningContractError(
            pointer, f"must contain {minimum} to {maximum} entries"
        )
    return value


def _refs(
    value: Any, pointer: str, known: set[str], *, minimum: int
) -> tuple[str, ...]:
    values = _array(value, pointer, minimum=minimum, maximum=MAX_ENUMERATION_RECORDS)
    refs = tuple(
        _identifier(item, f"{pointer}/{index}") for index, item in enumerate(values)
    )
    if len(set(refs)) != len(refs):
        raise PlanningContractError(pointer, "references must be unique")
    unknown = set(refs) - known
    if unknown:
        raise PlanningContractError(pointer, f"unknown references: {sorted(unknown)}")
    return refs


def _unique_tuple(
    value: Sequence[str], pointer: str, *, minimum: int
) -> tuple[str, ...]:
    refs = tuple(
        _identifier(item, f"{pointer}/{index}") for index, item in enumerate(value)
    )
    if len(refs) < minimum or len(set(refs)) != len(refs):
        raise PlanningContractError(pointer, "must contain unique references")
    return refs


def _reject_sentinel(value: str, pointer: str) -> None:
    if _SENTINEL_RE.search(value):
        raise PlanningContractError(pointer, "must describe a concrete human action")


def _source_excerpts(
    source: SegmentedSource, refs: Sequence[str]
) -> tuple[SourceExcerpt, ...]:
    anchors = {item.id: item for item in source.anchors}
    excerpts: list[SourceExcerpt] = []
    for ref in dict.fromkeys(refs):
        anchor = anchors.get(ref)
        if anchor is None:
            raise PlanningContractError(
                "/source_refs", f"unknown source anchor {ref!r}"
            )
        excerpts.append(
            SourceExcerpt(
                ref,
                source.document.text[anchor.start_offset : anchor.end_offset],
                anchor.start_offset,
            )
        )
    return tuple(excerpts)


def _position(order: Sequence[str], anchor: str) -> int:
    if anchor not in order:
        raise PlanningContractError(
            "/next_anchor", "must be in the supplied source range"
        )
    return order.index(anchor)


def _invalid(pointer: str) -> Any:
    raise PlanningContractError(pointer, "must be an object")
