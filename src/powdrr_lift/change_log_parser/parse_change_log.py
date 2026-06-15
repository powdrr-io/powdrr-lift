from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import yaml


@dataclass(frozen=True, slots=True)
class Intent:
    problem: str | None = None
    goal: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    id: str | None = None
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class Entity:
    id: str | None = None
    type: str | None = None
    action: str | None = None


@dataclass(frozen=True, slots=True)
class Span:
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True, slots=True)
class RelatedSection:
    files: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    guidance: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ChangeFile:
    path: str | None = None
    type: str | None = None
    entities: list[Entity] = field(default_factory=list)
    span: Span = field(default_factory=Span)
    summary: str | None = None
    rationale: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeInvariant:
    id: str | None = None
    description: str | None = None
    action: str | None = None
    related: RelatedSection = field(default_factory=RelatedSection)


@dataclass(frozen=True, slots=True)
class ChangeGuidance:
    id: str | None = None
    description: str | None = None
    action: str | None = None
    related: RelatedSection = field(default_factory=RelatedSection)


@dataclass(frozen=True, slots=True)
class Change:
    file: str | None = None
    span: Span = field(default_factory=Span)
    summary: str | None = None
    affects: list[str] = field(default_factory=list)
    rationale: str | None = None
    files: list[ChangeFile] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    invariants: list[ChangeInvariant] = field(default_factory=list)
    guidance: list[ChangeGuidance] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RelationshipChange:
    action: str | None = None
    source: str | None = None
    target: str | None = None
    relationship: str | None = None
    rationale: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeLog:
    version: int | str | None = None
    change_id: str | None = None
    title: str | None = None
    intent: Intent = field(default_factory=Intent)
    decisions: list[Decision] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    relationship_changes: list[RelationshipChange] = field(default_factory=list)


def parse_change_log(yaml_content: str) -> ChangeLog:
    loaded_content = yaml.safe_load(yaml_content)
    if loaded_content is None:
        return ChangeLog()

    if not isinstance(loaded_content, Mapping):
        raise ValueError("Change log YAML must decode to a mapping.")

    data = dict(loaded_content)
    version = _normalize_version(data.get("version"))
    top_level_entities = [
        _parse_entity(entity_data)
        for entity_data in _parse_sequence(data.get("entities"))
    ]
    top_level_relationship_changes = [
        _parse_relationship_change(relationship_change_data)
        for relationship_change_data in _parse_sequence(
            data.get("relationship_changes")
        )
    ]
    changes = [
        _parse_change(change_data, version=version)
        for change_data in _parse_sequence(data.get("changes"))
    ]

    return ChangeLog(
        version=data.get("version"),
        change_id=data.get("change_id", data.get("pr_id")),
        title=data.get("title"),
        intent=_parse_intent(data.get("intent")),
        decisions=_parse_decisions(data),
        entities=[
            *top_level_entities,
            *[entity for change in changes for entity in change.entities],
        ],
        changes=changes,
        relationship_changes=[
            *top_level_relationship_changes,
        ],
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
        for decision_data in _parse_sequence(data.get("decisions"))
    ]

    if not decisions and data.get("decision") is not None:
        decisions.append(_parse_decision(data.get("decision")))

    return decisions


def _parse_entity(raw_entity: object) -> Entity:
    data = _parse_mapping(raw_entity)
    return Entity(id=data.get("id"), type=data.get("type"), action=data.get("action"))


def _parse_change(raw_change: object, *, version: int | str | None) -> Change:
    data = _parse_mapping(raw_change)
    files = _parse_change_files(data, version=version)
    nested_entities, _ = _parse_change_entities(data, version=version)
    invariants = [
        _parse_invariant(invariant_data)
        for invariant_data in _parse_sequence(data.get("invariants"))
    ]
    guidance = [
        _parse_guidance(guidance_data)
        for guidance_data in _parse_sequence(data.get("guidance"))
    ]
    legacy_affects = [str(affect) for affect in _parse_sequence(data.get("affects"))]
    derived_affects = _normalize_entity_ids(
        [
            *legacy_affects,
            *[
                entity.id
                for file_entry in files
                for entity in file_entry.entities
                if entity.id is not None
            ],
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

    return Change(
        file=primary_file,
        span=_coerce_span(primary_span),
        summary=primary_summary,
        affects=list(derived_affects if version == 2 else legacy_affects),
        rationale=primary_rationale,
        files=files,
        entities=nested_entities,
        invariants=invariants,
        guidance=guidance,
    )


def _parse_change_files(
    data: Mapping[str, Any],
    *,
    version: int | str | None,
) -> list[ChangeFile]:
    files: list[ChangeFile] = []
    if version == 2:
        for raw_file in _parse_sequence(data.get("files")):
            file_data = _parse_mapping(raw_file)
            files.append(
                ChangeFile(
                    path=file_data.get("path"),
                    type=file_data.get("type"),
                    entities=[
                        _parse_entity(entity_data)
                        for entity_data in _parse_sequence(file_data.get("entities"))
                    ],
                    span=_parse_span(file_data.get("span")),
                    summary=file_data.get("summary"),
                    rationale=file_data.get("rationale"),
                )
            )
        return files

    if data.get("file") is not None:
        files.append(ChangeFile(path=data.get("file"), type=data.get("type")))
    return files


def _parse_change_entities(
    data: Mapping[str, Any],
    *,
    version: int | str | None,
) -> tuple[list[Entity], list[RelationshipChange]]:
    if version == 2:
        entities = [
            _parse_entity(entity_data)
            for entity_data in _parse_sequence(data.get("entities"))
        ]
        return entities, []

    entities = [
        _parse_entity(entity_data)
        for entity_data in _parse_sequence(data.get("entities"))
    ]
    relationships = [
        _parse_relationship_change(relationship_data)
        for relationship_data in _parse_sequence(data.get("relationship_changes"))
    ]
    return entities, relationships


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


def _parse_related_section(raw_related: object | None) -> RelatedSection:
    data = _parse_mapping(raw_related)
    return RelatedSection(
        files=[str(item) for item in _parse_sequence(data.get("files"))],
        entities=[str(item) for item in _parse_sequence(data.get("entities"))],
        invariants=[str(item) for item in _parse_sequence(data.get("invariants"))],
        guidance=[str(item) for item in _parse_sequence(data.get("guidance"))],
    )


def _parse_relationship_change(raw_relationship_change: object) -> RelationshipChange:
    data = _parse_mapping(raw_relationship_change)
    return RelationshipChange(
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


def _parse_sequence(raw_data: object | None) -> Sequence[object]:
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
