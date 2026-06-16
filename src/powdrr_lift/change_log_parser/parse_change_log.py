from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Dict, List, cast

import yaml


@dataclass(frozen=True, slots=True)
class Intent:
    problem: str | None = None
    goal: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    id: str 
    summary: str
    replaces: str | None = None


@dataclass(frozen=True, slots=True)
class Reference:
    id: str
    reasoning: str


@dataclass(frozen=True, slots=True)
class RelatedSection:
    files: list[Reference] = field(default_factory=list)
    entities: list[Reference] = field(default_factory=list)
    invariants: list[Reference] = field(default_factory=list)
    guidance: list[Reference] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ChangeEntity:
    id: str 
    type: str
    action: str
    related: RelatedSection | None = None


@dataclass(frozen=True, slots=True)
class ChangeEntityRelationship:
    id: str | None = None
    type: str | None = None
    parent_entity: str
    child_entity: str
    relationship: str
    description: str | None = None
    action: str | None = None  
    related: RelatedSection | None = None


@dataclass(frozen=True, slots=True)
class Span:
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True, slots=True)
class ChangeFile:
    path: str | None = None
    type: str | None = None
    span: Span = field(default_factory=Span)
    summary: str | None = None
    rationale: str | None = None
    related: RelatedSection | None = None


@dataclass(frozen=True, slots=True)
class ChangeInvariant:
    id: str | None = None
    description: str | None = None
    action: str | None = None
    related: RelatedSection | None = None


@dataclass(frozen=True, slots=True)
class ChangeGuidance:
    id: str | None = None
    description: str | None = None
    action: str | None = None
    related: RelatedSection | None = None


@dataclass(frozen=True, slots=True)
class ChangeLog:
    version: int | str | None = None
    change_id: str | None = None
    title: str | None = None
    intent: Intent = field(default_factory=Intent)
    decisions: list[Decision] = field(default_factory=list)
    file_changes: list[ChangeFile] = field(default_factory=list)
    entity_relationship_changes: list[ChangeEntityRelationship] = field(default_factory=list)
    entity_changes: list[ChangeEntity] = field(default_factory=list)
    guidance_changes: list[ChangeGuidance]
    invariant_changes: list[ChangeInvariant]


def parse_change_log(yaml_content: str) -> ChangeLog:
    loaded_content = yaml.safe_load(yaml_content)
    if loaded_content is None:
        return ChangeLog()

    if not isinstance(loaded_content, Mapping):
        raise ValueError("Change log YAML must decode to a mapping.")

    data = dict(loaded_content)
    version = _normalize_version(data.get("version"))

    if version == 1:
        _parse_change_log_v1(version, data)
    elif version == 2:
        _parse_change_log_v2(version, data)
    else:
        raise Exception("Unknown change log version")
    

def _parse_change_log_v1(version: int, data: Dict[str, Any]) -> ChangeLog:
    top_level_entities = [
        _parse_entity(entity_data)
        for entity_data in _ensure_is_sequence(data.get("entities"))
    ]
    top_level_relationship_changes = [
        _parse_relationship_change(relationship_change_data)
        for relationship_change_data in _ensure_is_sequence(
            data.get("relationship_changes")
        )
    ]
    changes = [
        _parse_change_v1(change_data, version=version)
        for change_data in _ensure_is_sequence(data.get("changes"))
    ]

    parsed_entities = [
        *(top_level_entities if version != 2 else []),
        *[entity for change in changes for entity in change.entities],
    ]
    parsed_relationship_changes = top_level_relationship_changes if version != 2 else []

    return ChangeLog(
        version=data.get("version"),
        change_id=data.get("change_id", data.get("pr_id")),
        title=data.get("title"),
        intent=_parse_intent(data.get("intent")),
        decisions=_parse_decisions(data),
        entities=parsed_entities,
        changes=changes,
        relationship_changes=parsed_relationship_changes,
    )


def _parse_change_log_v2(version: int, data: Dict[str, Any]) -> ChangeLog:
    top_level_entities = [
        _parse_entity(entity_data)
        for entity_data in _ensure_is_sequence(data.get("entities"))
    ]
    top_level_relationship_changes = [
        _parse_relationship_change(relationship_change_data)
        for relationship_change_data in _ensure_is_sequence(
            data.get("relationship_changes")
        )
    ]
    changes = [
        _parse_change(change_data, version=version)
        for change_data in _ensure_is_sequence(data.get("changes"))
    ]

    parsed_entities = [
        *(top_level_entities if version != 2 else []),
        *[entity for change in changes for entity in change.entities],
    ]
    parsed_relationship_changes = top_level_relationship_changes if version != 2 else []

    return ChangeLog(
        version=data.get("version"),
        change_id=data.get("change_id", data.get("pr_id")),
        title=data.get("title"),
        intent=_parse_intent(data.get("intent")),
        decisions=_parse_decisions(data),
        entities=parsed_entities,
        changes=changes,
        relationship_changes=parsed_relationship_changes,
    )    


def _normalize_version(raw_version: object | None) -> int | str | None:
    if isinstance(raw_version, str) and raw_version.isdigit():
        return int(raw_version)

    return cast(int | str | None, raw_version)


def _parse_intent(raw_intent: object | None) -> Intent:
    data = _parse_mapping(raw_intent)
    return Intent(problem=data.get("problem"), goal=data.get("goal"))


def _parse_decision(raw_decision: object | None) -> Decision:
    data = _parse_mapping(raw_decision)
    return Decision(id=data.get("id"), summary=data.get("summary"))


def _parse_decisions(data: Mapping[str, Any]) -> list[Decision]:
    decisions = [
        _parse_decision(decision_data)
        for decision_data in _ensure_is_sequence(data.get("decisions"))
    ]

    if not decisions and data.get("decision") is not None:
        decisions.append(_parse_decision(data.get("decision")))

    return decisions


def _parse_entity(raw_entity: object) -> Entity:
    data = _parse_mapping(raw_entity)
    return Entity(id=data.get("id"), type=data.get("type"), action=data.get("action"))


def _parse_entity_id(raw_entity: object) -> str | None:
    if isinstance(raw_entity, str):
        return _normalize_entity_id(raw_entity)

    if isinstance(raw_entity, Mapping):
        return _normalize_entity_id(cast(str | None, raw_entity.get("id")))

    return _normalize_entity_id(str(raw_entity))


def _parse_change_v1(raw_change: object, *, version: int | str | None) -> ChangeFile:
    data = _parse_mapping(raw_change)
    files = _parse_change_files(data, version=version)
    nested_entities = _parse_change_entities(data, version=version)
    legacy_affects = [str(affect) for affect in _ensure_is_sequence(data.get("affects"))]
    derived_affects = _normalize_entity_ids(
        [
            *legacy_affects,
            *[entity_id for file_entry in files for entity_id in file_entry.entities],
            *[entity.id for entity in nested_entities if entity.id is not None],
        ]
    )
    primary_file = data.get("file")
    if primary_file is None:
        primary_file = files[0].path if files else None
    primary_span = data.get("span")
    if primary_span is None and files:
        primary_span = files[0].span
    primary_summary = data.get("summary")
    if primary_summary is None and files:
        primary_summary = files[0].summary
    primary_rationale = data.get("rationale")
    if primary_rationale is None and files:
        primary_rationale = files[0].rationale

    return ChangeFile(
        file=primary_file,
        span=_coerce_span(primary_span),
        summary=primary_summary,
        affects=list(derived_affects if version == 2 else legacy_affects),
        rationale=primary_rationale,
        files=files,
        entities=nested_entities,
    )


def _parse_change_files(
    data: Mapping[str, Any],
    *,
    version: int | str | None,
) -> list[ChangeFile]:
    files: list[ChangeFile] = []
    if version == 2:
        for raw_file in _ensure_is_sequence(data.get("files")):
            file_data = _parse_mapping(raw_file)
            file_entities: list[str] = []
            for entity_id in _ensure_is_sequence(file_data.get("entities")):
                normalized_entity_id = _parse_entity_id(entity_id)
                if normalized_entity_id is not None:
                    file_entities.append(normalized_entity_id)

            files.append(
                ChangeFile(
                    path=file_data.get("path"),
                    type=file_data.get("type"),
                    entities=file_entities,
                    span=_parse_span(file_data.get("span")),
                    summary=file_data.get("summary"),
                    rationale=file_data.get("rationale"),
                )
            )
        if not files and data.get("file") is not None:
            files.append(
                ChangeFile(
                    path=data.get("file"),
                    type=data.get("type"),
                    entities=[],
                    span=_parse_span(data.get("span")),
                    summary=data.get("summary"),
                    rationale=data.get("rationale"),
                )
            )
        return files

    if data.get("file") is not None:
        files.append(
            ChangeFile(
                path=data.get("file"),
                type=data.get("type"),
                entities=[],
                span=_parse_span(data.get("span")),
                summary=data.get("summary"),
                rationale=data.get("rationale"),
            )
        )
    return files


def _parse_change_entities(
    data: Mapping[str, Any],
    *,
    version: int | str | None,
) -> list[Entity]:
    if version == 2:
        return [
            _parse_entity(entity_data)
            for entity_data in _ensure_is_sequence(data.get("entities"))
        ]

    return [
        _parse_entity(entity_data)
        for entity_data in _ensure_is_sequence(data.get("entities"))
    ]


def _parse_invariant(raw_invariant: object) -> ChangeInvariant:
    data = _parse_mapping(raw_invariant)
    return ChangeInvariant(
        id=data.get("id"),
        description=data.get("description"),
        action=data.get("action"),
        related=_parse_related_section(data.get("related")),
    )


def _parse_guidance(raw_guidance: object) -> ChangeGuidance:
    data = _parse_mapping(raw_guidance)
    return ChangeGuidance(
        id=data.get("id"),
        description=data.get("description"),
        action=data.get("action"),
        related=_parse_related_section(data.get("related")),
    )


def _parse_related(raw_related: object | None) -> RelatedSection:
    data = _parse_mapping(raw_related)
    return RelatedSection(
        files=_parse_references(data.get("files")),
        entities=_parse_references(data.get("entities")),
        invariants=_parse_references(data.get("invariants")),
        guidance=_parse_references(data.get("guidance")),
    )  


def _parse_references(raw_referenes: object | None) -> Sequence[Reference]:
    data = _ensure_is_sequence(raw_referenes)
    return [
        Reference(
            id=data.get("id"),
            reasoning=data.get("reasoning")
        )
    ]ß


def _parse_related_section(raw_related: object | None) -> RelatedSection:
    data = _parse_mapping(raw_related)
    return RelatedSection(
        files=[_parse_relation(item) for item in _ensure_is_sequence(data.get("files"))],
        entities=[str(item) for item in _ensure_is_sequence(data.get("entities"))],
        invariants=[str(item) for item in _ensure_is_sequence(data.get("invariants"))],
        guidance=[str(item) for item in _ensure_is_sequence(data.get("guidance"))],
    )


def _parse_relationship_change(raw_relationship_change: object) -> ChangeEntityRelationship:
    data = _parse_mapping(raw_relationship_change)
    return ChangeEntityRelationship(
        action=data.get("action"),
        source=data.get("source"),
        target=data.get("target"),
        relationship=data.get("relationship"),
        rationale=data.get("rationale"),
    )


def _parse_span(raw_span: object | None) -> Span:
    data = _parse_mapping(raw_span)
    return Span(start_line=data.get("start_line"), end_line=data.get("end_line"))


def _coerce_span(raw_span: object | None) -> Span:
    if isinstance(raw_span, Span):
        return raw_span

    return _parse_span(raw_span)


def _parse_mapping(raw_data: object | None) -> dict[str, Any]:
    if raw_data is None:
        return {}

    if not isinstance(raw_data, Mapping):
        raise ValueError("Expected a mapping in the changelog structure.")

    return dict(raw_data)


def _ensure_is_sequence(raw_data: object | None) -> Sequence[object]:
    if raw_data is None:
        return ()

    if isinstance(raw_data, (str, bytes)) or not isinstance(raw_data, Sequence):
        raise ValueError("Expected a sequence in the changelog structure.")

    return raw_data


def _normalize_entity_ids(entity_ids: Sequence[str | None]) -> tuple[str, ...]:
    normalized_entity_ids: list[str] = []
    seen_entity_ids: set[str] = set()
    for entity_id in entity_ids:
        normalized_entity_id = _normalize_entity_id(entity_id)
        if normalized_entity_id is None or normalized_entity_id in seen_entity_ids:
            continue

        seen_entity_ids.add(normalized_entity_id)
        normalized_entity_ids.append(normalized_entity_id)

    return tuple(normalized_entity_ids)


def _normalize_entity_id(entity_id: str | None) -> str | None:
    if entity_id is None:
        return None

    normalized_entity_id = entity_id.strip()
    return normalized_entity_id or None
