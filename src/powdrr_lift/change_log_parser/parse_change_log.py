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
    replaces: str | None = None


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
    proposed_prs: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    expected_tests: list[str] = field(default_factory=list)
    expected_outcomes: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ChangeEntity:
    id: str | None = None
    type: str | None = None
    action: str | None = None
    related: RelatedSection = field(default_factory=RelatedSection)


@dataclass(frozen=True, slots=True)
class ChangeEntityRelationship:
    id: str | None = None
    type: str | None = None
    source: str | None = None
    target: str | None = None
    relationship: str | None = None
    description: str | None = None
    rationale: str | None = None
    action: str | None = None
    related: RelatedSection = field(default_factory=RelatedSection)


@dataclass(frozen=True, slots=True)
class ChangeFile:
    path: str | None = None
    type: str | None = None
    entities: list[str] = field(default_factory=list)
    span: Span = field(default_factory=Span)
    summary: str | None = None
    rationale: str | None = None
    related: RelatedSection = field(default_factory=RelatedSection)


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
class ChangeFeatureState:
    id: str | None = None
    state: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeProposedPRState:
    id: str | None = None
    state: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeLog:
    version: int | str | None = None
    change_id: str | None = None
    title: str | None = None
    intent: Intent = field(default_factory=Intent)
    decisions: list[Decision] = field(default_factory=list)
    structured_files: list[str] = field(default_factory=list)
    file_changes: list[ChangeFile] = field(default_factory=list)
    entity_changes: list[ChangeEntity] = field(default_factory=list)
    entity_relationship_changes: list[ChangeEntityRelationship] = field(
        default_factory=list
    )
    invariant_changes: list[ChangeInvariant] = field(default_factory=list)
    guidance_changes: list[ChangeGuidance] = field(default_factory=list)
    feature_changes: list[ChangeFeatureState] = field(default_factory=list)
    proposed_prs: list[ChangeProposedPRState] = field(default_factory=list)


def parse_change_log(yaml_content: str) -> ChangeLog:
    loaded_content = yaml.safe_load(yaml_content)
    if loaded_content is None:
        return ChangeLog()

    if not isinstance(loaded_content, Mapping):
        raise ValueError("Change log YAML must decode to a mapping.")

    data = dict(loaded_content)
    version = _normalize_version(data.get("version"))
    if version is None:
        version = _infer_version_from_schema(data.get("schema"))
    if version == 1:
        if data.get("version") is None:
            data["version"] = version
        return _parse_change_log_v1(data)
    if version == 2:
        if data.get("version") is None:
            data["version"] = version
        return _parse_change_log_v2(data)

    raise ValueError("Unknown change log version")


def _parse_change_log_v1(data: Mapping[str, Any]) -> ChangeLog:
    file_changes = [
        _parse_file_change_v1(change_data)
        for change_data in _ensure_sequence(data.get("changes"))
    ]
    return ChangeLog(
        version=_normalize_version(data.get("version")),
        change_id=_coerce_optional_str(data.get("change_id", data.get("pr_id"))),
        title=_coerce_optional_str(data.get("title")),
        intent=_parse_intent(data.get("intent")),
        decisions=_parse_decisions(data),
        entity_changes=[
            _parse_entity(entity_data)
            for entity_data in _ensure_sequence(data.get("entities"))
        ],
        file_changes=file_changes,
        entity_relationship_changes=[
            _parse_relationship_change(relationship_change_data)
            for relationship_change_data in _ensure_sequence(
                data.get("relationship_changes")
            )
        ],
        feature_changes=[],
        proposed_prs=[],
    )


def _parse_change_log_v2(data: Mapping[str, Any]) -> ChangeLog:
    if "changes" in data:
        raise ValueError(
            "Version 2 changelogs must use top-level structured_files, files, "
            "entities, entity_relationships, invariants, guidance, features, "
            "and proposed_prs sections."
        )

    return ChangeLog(
        version=_normalize_version(data.get("version")),
        change_id=_coerce_optional_str(data.get("change_id", data.get("pr_id"))),
        title=_coerce_optional_str(data.get("title")),
        intent=_parse_intent(data.get("intent")),
        decisions=_parse_decisions(data),
        structured_files=_parse_path_sequence(data.get("structured_files")),
        file_changes=_parse_change_files(data),
        entity_changes=_parse_change_entities(data),
        entity_relationship_changes=_parse_change_entity_relationships(data),
        invariant_changes=_parse_change_invariants(data),
        guidance_changes=_parse_change_guidance(data),
        feature_changes=_parse_change_features(data),
        proposed_prs=_parse_change_proposed_prs(data),
    )


def _parse_intent(raw_intent: object | None) -> Intent:
    data = _ensure_mapping(raw_intent)
    return Intent(
        problem=_coerce_optional_str(data.get("problem")),
        goal=_coerce_optional_str(data.get("goal")),
    )


def _parse_decision(raw_decision: object | None) -> Decision:
    data = _ensure_mapping(raw_decision)
    return Decision(
        id=_coerce_optional_str(data.get("id")),
        summary=_coerce_optional_str(data.get("summary")),
        replaces=_coerce_optional_str(data.get("replaces")),
    )


def _parse_decisions(data: Mapping[str, Any]) -> list[Decision]:
    decisions = [
        _parse_decision(decision_data)
        for decision_data in _ensure_sequence(data.get("decisions"))
    ]
    if not decisions and data.get("decision") is not None:
        decisions.append(_parse_decision(data.get("decision")))
    return decisions


def _parse_entity(raw_entity: object) -> ChangeEntity:
    data = _ensure_mapping(raw_entity)
    return ChangeEntity(
        id=_coerce_optional_str(data.get("id")),
        type=_coerce_optional_str(data.get("type")),
        action=_coerce_optional_str(data.get("action")),
        related=_parse_related_section(data.get("related")),
    )


def _parse_relationship_change(
    raw_relationship_change: object,
) -> ChangeEntityRelationship:
    data = _ensure_mapping(raw_relationship_change)
    return ChangeEntityRelationship(
        id=_coerce_optional_str(data.get("id")),
        type=_coerce_optional_str(data.get("type")),
        source=_coerce_optional_str(data.get("source")),
        target=_coerce_optional_str(data.get("target")),
        relationship=_coerce_optional_str(data.get("relationship")),
        description=_coerce_optional_str(data.get("description")),
        rationale=_coerce_optional_str(data.get("rationale")),
        action=_coerce_optional_str(data.get("action")),
        related=_parse_related_section(data.get("related")),
    )


def _parse_file_change_v1(raw_change: object) -> ChangeFile:
    data = _ensure_mapping(raw_change)
    path = _coerce_optional_str(data.get("path", data.get("file")))
    span = _coerce_span(data.get("span"))
    entities = _parse_id_sequence(data.get("affects"))
    summary = _coerce_optional_str(data.get("summary"))
    rationale = _coerce_optional_str(data.get("rationale"))
    return ChangeFile(
        path=path,
        entities=list(entities),
        span=span,
        summary=summary,
        rationale=rationale,
        related=RelatedSection(entities=list(entities)),
    )


def _parse_change_files(data: Mapping[str, Any]) -> list[ChangeFile]:
    files: list[ChangeFile] = []
    for raw_file in _ensure_sequence(data.get("files")):
        file_data = _ensure_mapping(raw_file)
        related = _parse_related_section(file_data.get("related"))
        entities = _parse_id_sequence(file_data.get("entities")) or list(
            related.entities
        )
        files.append(
            ChangeFile(
                path=_coerce_optional_str(file_data.get("path")),
                type=_coerce_optional_str(file_data.get("type")),
                entities=entities,
                span=_coerce_span(file_data.get("span")),
                summary=_coerce_optional_str(file_data.get("summary")),
                rationale=_coerce_optional_str(file_data.get("rationale")),
                related=related,
            )
        )
    return files


def _parse_change_entities(data: Mapping[str, Any]) -> list[ChangeEntity]:
    return [
        _parse_entity(entity_data)
        for entity_data in _ensure_sequence(data.get("entities"))
    ]


def _parse_change_entity_relationships(
    data: Mapping[str, Any],
) -> list[ChangeEntityRelationship]:
    return [
        _parse_relationship_change(relationship_change_data)
        for relationship_change_data in _ensure_sequence(
            data.get("entity_relationships")
        )
    ]


def _parse_change_invariants(data: Mapping[str, Any]) -> list[ChangeInvariant]:
    return [
        _parse_invariant(invariant_data)
        for invariant_data in _ensure_sequence(data.get("invariants"))
    ]


def _parse_change_guidance(data: Mapping[str, Any]) -> list[ChangeGuidance]:
    return [
        _parse_guidance(guidance_data)
        for guidance_data in _ensure_sequence(data.get("guidance"))
    ]


def _parse_change_features(data: Mapping[str, Any]) -> list[ChangeFeatureState]:
    return [
        _parse_feature_state(feature_data)
        for feature_data in _ensure_sequence(data.get("features"))
    ]


def _parse_change_proposed_prs(
    data: Mapping[str, Any],
) -> list[ChangeProposedPRState]:
    return [
        _parse_proposed_pr_state(pr_data)
        for pr_data in _ensure_sequence(data.get("proposed_prs", data.get("prs")))
    ]


def _parse_invariant(raw_invariant: object) -> ChangeInvariant:
    data = _ensure_mapping(raw_invariant)
    return ChangeInvariant(
        id=_coerce_optional_str(data.get("id")),
        description=_coerce_optional_str(data.get("description")),
        action=_coerce_optional_str(data.get("action")),
        related=_parse_related_section(data.get("related")),
    )


def _parse_guidance(raw_guidance: object) -> ChangeGuidance:
    data = _ensure_mapping(raw_guidance)
    return ChangeGuidance(
        id=_coerce_optional_str(data.get("id")),
        description=_coerce_optional_str(data.get("description")),
        action=_coerce_optional_str(data.get("action")),
        related=_parse_related_section(data.get("related")),
    )


def _parse_feature_state(raw_feature: object) -> ChangeFeatureState:
    data = _ensure_mapping(raw_feature)
    return ChangeFeatureState(
        id=_coerce_optional_str(data.get("id")),
        state=_coerce_optional_str(data.get("state")),
    )


def _parse_proposed_pr_state(raw_pr: object) -> ChangeProposedPRState:
    data = _ensure_mapping(raw_pr)
    return ChangeProposedPRState(
        id=_coerce_optional_str(data.get("id")),
        state=_coerce_optional_str(data.get("state")),
    )


def _parse_related_section(raw_related: object | None) -> RelatedSection:
    if raw_related is None:
        return RelatedSection()

    data = _ensure_mapping(raw_related)
    return RelatedSection(
        files=_parse_id_sequence(data.get("files")),
        entities=_parse_id_sequence(data.get("entities")),
        invariants=_parse_id_sequence(data.get("invariants")),
        guidance=_parse_id_sequence(data.get("guidance")),
        proposed_prs=_parse_id_sequence(data.get("proposed_prs", data.get("prs"))),
        acceptance_criteria=_parse_id_sequence(data.get("acceptance_criteria")),
        expected_tests=_parse_id_sequence(data.get("expected_tests")),
        expected_outcomes=_parse_id_sequence(data.get("expected_outcomes")),
        non_goals=_parse_id_sequence(data.get("non_goals")),
        risks=_parse_id_sequence(data.get("risks")),
    )


def _parse_id_sequence(raw_values: object | None) -> list[str]:
    values: list[str] = []
    for raw_value in _ensure_sequence(raw_values):
        normalized_value = _parse_id_value(raw_value)
        if normalized_value is not None:
            values.append(normalized_value)
    return values


def _parse_path_sequence(raw_values: object | None) -> list[str]:
    values: list[str] = []
    for raw_value in _ensure_sequence(raw_values):
        if isinstance(raw_value, str):
            normalized_value = raw_value.strip()
        else:
            normalized_value = str(raw_value).strip()

        if normalized_value:
            values.append(normalized_value)
    return values


def _parse_id_value(raw_value: object) -> str | None:
    if isinstance(raw_value, str):
        return _normalize_entity_id(raw_value)

    if isinstance(raw_value, Mapping):
        return _normalize_entity_id(_coerce_optional_str(raw_value.get("id")))

    return _normalize_entity_id(str(raw_value))


def _parse_span(raw_span: object | None) -> Span:
    data = _ensure_mapping(raw_span)
    return Span(
        start_line=_coerce_int(data.get("start_line")),
        end_line=_coerce_int(data.get("end_line")),
    )


def _coerce_span(raw_span: object | None) -> Span:
    if isinstance(raw_span, Span):
        return raw_span

    return _parse_span(raw_span)


def _ensure_mapping(raw_data: object | None) -> dict[str, Any]:
    if raw_data is None:
        return {}

    if not isinstance(raw_data, Mapping):
        raise ValueError("Expected a mapping in the changelog structure.")

    return dict(raw_data)


def _ensure_sequence(raw_data: object | None) -> Sequence[object]:
    if raw_data is None:
        return ()

    if isinstance(raw_data, (str, bytes)) or not isinstance(raw_data, Sequence):
        raise ValueError("Expected a sequence in the changelog structure.")

    return raw_data


def _normalize_version(raw_version: object | None) -> int | str | None:
    if isinstance(raw_version, str) and raw_version.isdigit():
        return int(raw_version)

    return cast(int | str | None, raw_version)


def _coerce_optional_str(raw_value: object | None) -> str | None:
    if raw_value is None:
        return None

    value = str(raw_value).strip()
    return value or None


def _infer_version_from_schema(raw_schema: object | None) -> int | None:
    if not isinstance(raw_schema, str):
        return None

    if raw_schema.endswith("changelog-v1"):
        return 1
    if raw_schema.endswith("changelog-v2"):
        return 2

    return None


def _coerce_int(raw_value: object | None) -> int | None:
    if raw_value is None:
        return None

    if isinstance(raw_value, bool):
        return int(raw_value)

    if isinstance(raw_value, int):
        return raw_value

    if isinstance(raw_value, str):
        stripped_value = raw_value.strip()
        if stripped_value == "":
            return None
        if stripped_value.isdigit() or (
            stripped_value.startswith("-") and stripped_value[1:].isdigit()
        ):
            return int(stripped_value)

    try:
        return int(str(raw_value))
    except (TypeError, ValueError):
        return None


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
