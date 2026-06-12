from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

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


@dataclass(frozen=True, slots=True)
class Span:
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True, slots=True)
class Change:
    id: str | None = None
    file: str | None = None
    span: Span = field(default_factory=Span)
    summary: str | None = None
    affects: list[str] = field(default_factory=list)
    rationale: str | None = None


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
    decision: Decision = field(default_factory=Decision)
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
    return ChangeLog(
        version=data.get("version"),
        change_id=data.get("change_id", data.get("pr_id")),
        title=data.get("title"),
        intent=_parse_intent(data.get("intent")),
        decision=_parse_decision(data.get("decision")),
        entities=[
            _parse_entity(entity_data)
            for entity_data in _parse_sequence(data.get("entities"))
        ],
        changes=[
            _parse_change(change_data)
            for change_data in _parse_sequence(data.get("changes"))
        ],
        relationship_changes=[
            _parse_relationship_change(relationship_change_data)
            for relationship_change_data in _parse_sequence(
                data.get("relationship_changes")
            )
        ],
    )


def _parse_intent(raw_intent: object | None) -> Intent:
    data = _parse_mapping(raw_intent)
    return Intent(problem=data.get("problem"), goal=data.get("goal"))


def _parse_decision(raw_decision: object | None) -> Decision:
    data = _parse_mapping(raw_decision)
    return Decision(id=data.get("id"), summary=data.get("summary"))


def _parse_entity(raw_entity: object) -> Entity:
    data = _parse_mapping(raw_entity)
    return Entity(id=data.get("id"), type=data.get("type"))


def _parse_change(raw_change: object) -> Change:
    data = _parse_mapping(raw_change)
    return Change(
        id=data.get("id"),
        file=data.get("file"),
        span=_parse_span(data.get("span")),
        summary=data.get("summary"),
        affects=[str(affect) for affect in _parse_sequence(data.get("affects"))],
        rationale=data.get("rationale"),
    )


def _parse_span(raw_span: object | None) -> Span:
    data = _parse_mapping(raw_span)
    return Span(start_line=data.get("start_line"), end_line=data.get("end_line"))


def _parse_relationship_change(raw_relationship_change: object) -> RelationshipChange:
    data = _parse_mapping(raw_relationship_change)
    return RelationshipChange(
        action=data.get("action"),
        source=data.get("source"),
        target=data.get("target"),
        relationship=data.get("relationship"),
        rationale=data.get("rationale"),
    )


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
