from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root

_DEFAULT_OUTPUT_PATH = Path("docs") / "architecture" / "architecture-specification.yaml"


@dataclass(frozen=True, slots=True)
class ArchitectureSpecificationValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ArchitectureSpecificationValidationReport:
    validation_successful: bool
    title: str | None
    allowed_entity_types: list[str] = field(default_factory=list)
    issues: list[ArchitectureSpecificationValidationIssue] = field(default_factory=list)


def architecture_specification_default_output_path(
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return repo_root_path / _DEFAULT_OUTPUT_PATH


def render_architecture_specification_template(
    entity_types: Sequence[str],
    *,
    title: str | None = None,
) -> str:
    normalized_entity_types = _normalize_entity_types(entity_types)
    if not normalized_entity_types:
        raise ValueError("At least one entity type must be provided.")

    lines = [
        "# Architecture specification template.",
        "#",
        "# Instructions:",
        "# - Fill in the sections below with the intended architecture.",
        "# - Choose each entity type from the allowed entity types listed here.",
        "# - Use `related.entities` and `related.entity_relationships` in",
        "#   invariants and guidance whenever they refer to a specific entity",
        "#   or relationship.",
        "# - Keep every entity mentioned in a relationship, invariant, or",
        "#   guidance item present in the `entities` section.",
        "#",
        "# Allowed entity types:",
        *[f"# - {entity_type}" for entity_type in normalized_entity_types],
        "version: 1",
        "title: null",
        "entities:",
        "  - id: null",
        "    type: null",
        "    summary: null",
        "    rationale: null",
        "entity_relationships:",
        "  - id: null",
        "    source: null",
        "    target: null",
        "    relationship: null",
        "    description: null",
        "    rationale: null",
        "invariants:",
        "  - id: null",
        "    description: null",
        "    rationale: null",
        "    related:",
        "      entities: []",
        "      entity_relationships: []",
        "guidance:",
        "  - id: null",
        "    description: null",
        "    rationale: null",
        "    related:",
        "      entities: []",
        "      entity_relationships: []",
        "",
    ]

    if title is not None:
        lines[lines.index("title: null")] = f"title: {json.dumps(title)}"

    return "\n".join(lines)


def create_architecture_specification_template(
    entity_types: Sequence[str],
    *,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    title: str | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_output_path = _resolve_output_path(repo_root_path, output_path)
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(
        render_architecture_specification_template(entity_types, title=title),
        encoding="utf-8",
    )
    return resolved_output_path


def validate_architecture_specification_yaml(
    proposed_architecture_specification_yaml: str,
    *,
    entity_types: Sequence[str],
) -> str:
    report = build_architecture_specification_validation_report(
        proposed_architecture_specification_yaml=proposed_architecture_specification_yaml,
        entity_types=entity_types,
    )
    return yaml.safe_dump(_report_to_data(report), sort_keys=False)


def build_architecture_specification_validation_report(
    proposed_architecture_specification_yaml: str,
    *,
    entity_types: Sequence[str],
) -> ArchitectureSpecificationValidationReport:
    allowed_entity_types = _normalize_entity_types(entity_types)
    issues: list[ArchitectureSpecificationValidationIssue] = []
    if not allowed_entity_types:
        issues.append(
            ArchitectureSpecificationValidationIssue(
                code="allowed_entity_types_missing",
                message="Provide at least one allowed entity type.",
            )
        )
        return ArchitectureSpecificationValidationReport(
            validation_successful=False,
            title=None,
            allowed_entity_types=[],
            issues=issues,
        )

    try:
        raw_spec = _load_yaml_mapping(proposed_architecture_specification_yaml)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            ArchitectureSpecificationValidationIssue(
                code="invalid_yaml",
                message=(
                    f"Could not parse proposed architecture specification YAML: {exc}"
                ),
            )
        )
        return ArchitectureSpecificationValidationReport(
            validation_successful=False,
            title=None,
            allowed_entity_types=allowed_entity_types,
            issues=issues,
        )

    title = _optional_string(raw_spec.get("title"))

    for section_name in (
        "entities",
        "entity_relationships",
        "invariants",
        "guidance",
    ):
        if section_name not in raw_spec:
            issues.append(
                ArchitectureSpecificationValidationIssue(
                    code="missing_required_section",
                    message=f"The {section_name} section is required.",
                    path=section_name,
                )
            )

    entity_ids = _collect_entity_ids(
        _coerce_sequence(
            raw_spec.get("entities"),
            path="entities",
            issues=issues,
            issue_code="invalid_entities_section",
            issue_message="entities must be a list of entity mappings.",
        ),
        allowed_entity_types=allowed_entity_types,
        issues=issues,
    )
    if not entity_ids:
        issues.append(
            ArchitectureSpecificationValidationIssue(
                code="no_entities_defined",
                message="Define at least one entity.",
                path="entities",
            )
        )
    relationship_ids = _collect_relationship_ids(
        _coerce_sequence(
            raw_spec.get("entity_relationships"),
            path="entity_relationships",
            issues=issues,
            issue_code="invalid_entity_relationships_section",
            issue_message=(
                "entity_relationships must be a list of relationship mappings."
            ),
        ),
        entity_ids=entity_ids,
        issues=issues,
    )
    _collect_related_references(
        _coerce_sequence(
            raw_spec.get("invariants"),
            path="invariants",
            issues=issues,
            issue_code="invalid_invariants_section",
            issue_message="invariants must be a list of invariant mappings.",
        ),
        section_name="invariants",
        entity_ids=entity_ids,
        relationship_ids=relationship_ids,
        issues=issues,
    )
    _collect_related_references(
        _coerce_sequence(
            raw_spec.get("guidance"),
            path="guidance",
            issues=issues,
            issue_code="invalid_guidance_section",
            issue_message="guidance must be a list of guidance mappings.",
        ),
        section_name="guidance",
        entity_ids=entity_ids,
        relationship_ids=relationship_ids,
        issues=issues,
    )

    return ArchitectureSpecificationValidationReport(
        validation_successful=not issues,
        title=title,
        allowed_entity_types=allowed_entity_types,
        issues=issues,
    )


def _collect_entity_ids(
    raw_entities: Sequence[object],
    *,
    allowed_entity_types: Sequence[str],
    issues: list[ArchitectureSpecificationValidationIssue],
) -> set[str]:
    entity_ids: set[str] = set()
    for index, raw_entity in enumerate(raw_entities):
        entity = _coerce_mapping(
            raw_entity,
            path=f"entities[{index}]",
            issues=issues,
            issue_code="invalid_entity_entry",
            issue_message="Each entity entry must be a mapping.",
        )
        if entity is None:
            continue

        entity_id = _required_string(
            entity.get("id"),
            path=f"entities[{index}].id",
            issues=issues,
            issue_code="entity_id_missing",
            issue_message="Each entity must include an id.",
        )
        entity_type = _required_string(
            entity.get("type"),
            path=f"entities[{index}].type",
            issues=issues,
            issue_code="entity_type_missing",
            issue_message="Each entity must include a type.",
        )
        if entity_id is None or entity_type is None:
            continue

        if entity_id in entity_ids:
            issues.append(
                ArchitectureSpecificationValidationIssue(
                    code="duplicate_entity_id",
                    message=f"Entity id {entity_id!r} appears more than once.",
                    path=f"entities[{index}].id",
                )
            )
            continue

        if entity_type not in allowed_entity_types:
            issues.append(
                ArchitectureSpecificationValidationIssue(
                    code="entity_type_not_allowed",
                    message=(
                        f"Entity type {entity_type!r} is not in the allowed "
                        "entity types."
                    ),
                    path=f"entities[{index}].type",
                )
            )
            continue

        entity_ids.add(entity_id)

    return entity_ids


def _collect_relationship_ids(
    raw_relationships: Sequence[object],
    *,
    entity_ids: set[str],
    issues: list[ArchitectureSpecificationValidationIssue],
) -> set[str]:
    relationship_ids: set[str] = set()
    for index, raw_relationship in enumerate(raw_relationships):
        relationship = _coerce_mapping(
            raw_relationship,
            path=f"entity_relationships[{index}]",
            issues=issues,
            issue_code="invalid_relationship_entry",
            issue_message="Each entity relationship entry must be a mapping.",
        )
        if relationship is None:
            continue

        relationship_id = _required_string(
            relationship.get("id"),
            path=f"entity_relationships[{index}].id",
            issues=issues,
            issue_code="relationship_id_missing",
            issue_message="Each entity relationship must include an id.",
        )
        source = _required_string(
            relationship.get("source"),
            path=f"entity_relationships[{index}].source",
            issues=issues,
            issue_code="relationship_source_missing",
            issue_message="Each entity relationship must include a source.",
        )
        target = _required_string(
            relationship.get("target"),
            path=f"entity_relationships[{index}].target",
            issues=issues,
            issue_code="relationship_target_missing",
            issue_message="Each entity relationship must include a target.",
        )

        if relationship_id is None or source is None or target is None:
            continue

        if relationship_id in relationship_ids:
            issues.append(
                ArchitectureSpecificationValidationIssue(
                    code="duplicate_relationship_id",
                    message=(
                        f"Entity relationship id {relationship_id!r} appears more "
                        "than once."
                    ),
                    path=f"entity_relationships[{index}].id",
                )
            )
            continue

        if source not in entity_ids:
            issues.append(
                ArchitectureSpecificationValidationIssue(
                    code="unknown_relationship_entity",
                    message=(
                        f"Relationship source {source!r} is not listed in entities."
                    ),
                    path=f"entity_relationships[{index}].source",
                )
            )
        if target not in entity_ids:
            issues.append(
                ArchitectureSpecificationValidationIssue(
                    code="unknown_relationship_entity",
                    message=(
                        f"Relationship target {target!r} is not listed in entities."
                    ),
                    path=f"entity_relationships[{index}].target",
                )
            )

        relationship_ids.add(relationship_id)

    return relationship_ids


def _collect_related_references(
    raw_items: Sequence[object],
    *,
    section_name: str,
    entity_ids: set[str],
    relationship_ids: set[str],
    issues: list[ArchitectureSpecificationValidationIssue],
) -> None:
    section_label = "invariant" if section_name == "invariants" else "guidance"
    for index, raw_item in enumerate(raw_items):
        item = _coerce_mapping(
            raw_item,
            path=f"{section_name}[{index}]",
            issues=issues,
            issue_code="invalid_section_entry",
            issue_message=f"Each {section_label} entry must be a mapping.",
        )
        if item is None:
            continue

        item_id = _required_string(
            item.get("id"),
            path=f"{section_name}[{index}].id",
            issues=issues,
            issue_code="section_id_missing",
            issue_message=f"Each {section_label} entry must include an id.",
        )
        if item_id is None:
            continue

        related = item.get("related")
        if related is None:
            continue

        related_mapping = _coerce_mapping(
            related,
            path=f"{section_name}[{index}].related",
            issues=issues,
            issue_code="invalid_related_entry",
            issue_message="related must be a mapping when present.",
        )
        if related_mapping is None:
            continue

        for related_index, related_entity in enumerate(
            _coerce_sequence(
                related_mapping.get("entities"),
                path=f"{section_name}[{index}].related.entities",
                issues=issues,
                issue_code="invalid_related_entities_section",
                issue_message="related.entities must be a list of strings.",
            )
        ):
            related_entity_id = _required_string(
                related_entity,
                path=f"{section_name}[{index}].related.entities[{related_index}]",
                issues=issues,
                issue_code="related_entity_missing",
                issue_message="Related entity references must be strings.",
            )
            if related_entity_id is None:
                continue

            if related_entity_id not in entity_ids:
                issues.append(
                    ArchitectureSpecificationValidationIssue(
                        code="unknown_related_entity",
                        message=(
                            f"Related entity {related_entity_id!r} is not listed in "
                            "entities."
                        ),
                        path=(
                            f"{section_name}[{index}].related.entities[{related_index}]"
                        ),
                    )
                )

        for related_index, related_relationship in enumerate(
            _coerce_sequence(
                related_mapping.get("entity_relationships"),
                path=f"{section_name}[{index}].related.entity_relationships",
                issues=issues,
                issue_code="invalid_related_relationships_section",
                issue_message="related.entity_relationships must be a list of strings.",
            )
        ):
            related_relationship_id = _required_string(
                related_relationship,
                path=(
                    f"{section_name}[{index}].related.entity_relationships["
                    f"{related_index}]"
                ),
                issues=issues,
                issue_code="related_relationship_missing",
                issue_message="Related relationship references must be strings.",
            )
            if related_relationship_id is None:
                continue

            if related_relationship_id not in relationship_ids:
                issues.append(
                    ArchitectureSpecificationValidationIssue(
                        code="unknown_related_relationship",
                        message=(
                            f"Related relationship {related_relationship_id!r} "
                            "is not listed in entity_relationships."
                        ),
                        path=(
                            f"{section_name}[{index}].related.entity_relationships["
                            f"{related_index}]"
                        ),
                    )
                )


def _load_yaml_mapping(raw_yaml: str) -> Mapping[str, Any]:
    loaded = yaml.safe_load(raw_yaml)
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise TypeError("Top-level architecture specification must be a mapping.")
    return cast(Mapping[str, Any], loaded)


def _coerce_sequence(
    raw_value: object,
    *,
    path: str,
    issues: list[ArchitectureSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> Sequence[object]:
    if raw_value is None:
        return ()
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        return raw_value
    issues.append(
        ArchitectureSpecificationValidationIssue(
            code=issue_code,
            message=issue_message,
            path=path,
        )
    )
    return ()


def _coerce_mapping(
    raw_value: object,
    *,
    path: str,
    issues: list[ArchitectureSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> Mapping[str, Any] | None:
    if isinstance(raw_value, Mapping):
        return cast(Mapping[str, Any], raw_value)

    issues.append(
        ArchitectureSpecificationValidationIssue(
            code=issue_code,
            message=issue_message,
            path=path,
        )
    )
    return None


def _required_string(
    raw_value: object,
    *,
    path: str,
    issues: list[ArchitectureSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> str | None:
    if raw_value is None:
        issues.append(
            ArchitectureSpecificationValidationIssue(
                code=issue_code,
                message=issue_message,
                path=path,
            )
        )
        return None

    value = str(raw_value).strip()
    if value == "":
        issues.append(
            ArchitectureSpecificationValidationIssue(
                code=issue_code,
                message=issue_message,
                path=path,
            )
        )
        return None

    return value


def _optional_string(raw_value: object) -> str | None:
    if raw_value is None:
        return None

    value = str(raw_value).strip()
    return value or None


def _normalize_entity_types(entity_types: Sequence[str]) -> list[str]:
    normalized_entity_types: list[str] = []
    seen_entity_types: set[str] = set()
    for raw_entity_type in entity_types:
        entity_type = str(raw_entity_type).strip()
        if not entity_type or entity_type in seen_entity_types:
            continue

        seen_entity_types.add(entity_type)
        normalized_entity_types.append(entity_type)

    return normalized_entity_types


def _resolve_output_path(repo_root: Path, output_path: str | Path | None) -> Path:
    if output_path is None:
        return repo_root / _DEFAULT_OUTPUT_PATH

    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        resolved_output_path = repo_root / resolved_output_path

    return resolved_output_path


def _report_to_data(
    report: ArchitectureSpecificationValidationReport,
) -> Mapping[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "title": report.title,
        "allowed_entity_types": report.allowed_entity_types,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                **({"path": issue.path} if issue.path is not None else {}),
            }
            for issue in report.issues
        ],
    }
