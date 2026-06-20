from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root

_DEFAULT_OUTPUT_PATH = (
    Path("docs") / "implementation" / "implementation-specification.yaml"
)
_DEFAULT_ARCHITECTURE_SPECIFICATION_PATH = (
    Path("docs") / "architecture" / "architecture-specification.yaml"
)


@dataclass(frozen=True, slots=True)
class ImplementationSpecificationValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ImplementationSpecificationValidationReport:
    validation_successful: bool
    architecture_id: str | None
    available_entity_ids: list[str] = field(default_factory=list)
    available_relationship_ids: list[str] = field(default_factory=list)
    issues: list[ImplementationSpecificationValidationIssue] = field(
        default_factory=list
    )


@dataclass(frozen=True, slots=True)
class _ArchitectureSpecificationSummary:
    architecture_id: str | None
    title: str | None
    entity_ids: list[str]
    relationship_ids: list[str]


def implementation_specification_default_output_path(
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return repo_root_path / _DEFAULT_OUTPUT_PATH


def render_implementation_specification_template(
    *,
    architecture_specification_path: str | Path | None = None,
    title: str | None = None,
    repo_root: str | Path | None = None,
) -> str:
    repo_root_path = _resolve_repo_root(repo_root)
    architecture_summary = _load_architecture_specification_summary(
        repo_root_path,
        architecture_specification_path=architecture_specification_path,
        issues=None,
    )
    if architecture_summary is None:
        raise ValueError(
            "The architecture specification could not be loaded. "
            "Create the architecture specification first and then retry."
        )
    architecture_id = architecture_summary.architecture_id
    if architecture_id is None:
        raise ValueError(
            "The architecture specification must include an id before you can "
            "generate an implementation specification template."
        )

    lines = [
        "# Implementation specification template.",
        "#",
        "# Instructions:",
        "# - Keep `architecture_id` aligned with the architecture specification.",
        "# - Copy entity ids and relationship ids only from the architecture",
        "#   specification listed below.",
        "# - Give each feature a unique id, a description, and functional",
        "#   requirements.",
        "# - Give each decision a unique id and description.",
        "#",
        f"# Architecture id: {architecture_id}",
        "# Available entities:",
        *[f"# - {entity_id}" for entity_id in architecture_summary.entity_ids],
        "# Available relationships:",
        *[
            f"# - {relationship_id}"
            for relationship_id in architecture_summary.relationship_ids
        ],
        "version: 1",
        "title: null",
        f"architecture_id: {json.dumps(architecture_id)}",
        "entities:",
        "  - id: null",
        "    rationale: null",
        "entity_relationships:",
        "  - id: null",
        "    rationale: null",
        "features:",
        "  - id: null",
        "    description: null",
        "    functional_requirements:",
        "      - null",
        "decisions:",
        "  - id: null",
        "    description: null",
        "",
    ]

    if title is not None:
        lines[lines.index("title: null")] = f"title: {json.dumps(title)}"

    return "\n".join(lines)


def create_implementation_specification_template(
    *,
    architecture_specification_path: str | Path | None = None,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    title: str | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_output_path = _resolve_output_path(repo_root_path, output_path)
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(
        render_implementation_specification_template(
            architecture_specification_path=architecture_specification_path,
            title=title,
            repo_root=repo_root_path,
        ),
        encoding="utf-8",
    )
    return resolved_output_path


def validate_implementation_specification_yaml(
    proposed_implementation_specification_yaml: str,
    *,
    architecture_specification_path: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> str:
    report = build_implementation_specification_validation_report(
        proposed_implementation_specification_yaml,
        architecture_specification_path=architecture_specification_path,
        repo_root=repo_root,
    )
    return yaml.safe_dump(_report_to_data(report), sort_keys=False)


def build_implementation_specification_validation_report(
    proposed_implementation_specification_yaml: str,
    *,
    architecture_specification_path: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> ImplementationSpecificationValidationReport:
    repo_root_path = _resolve_repo_root(repo_root)
    issues: list[ImplementationSpecificationValidationIssue] = []
    architecture_summary = _load_architecture_specification_summary(
        repo_root_path,
        architecture_specification_path=architecture_specification_path,
        issues=issues,
    )
    if architecture_summary is None:
        return ImplementationSpecificationValidationReport(
            validation_successful=False,
            architecture_id=None,
            available_entity_ids=[],
            available_relationship_ids=[],
            issues=issues,
        )

    try:
        raw_spec = _load_yaml_mapping(proposed_implementation_specification_yaml)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            ImplementationSpecificationValidationIssue(
                code="invalid_yaml",
                message=(
                    f"Could not parse proposed implementation specification YAML: {exc}"
                ),
            )
        )
        return ImplementationSpecificationValidationReport(
            validation_successful=False,
            architecture_id=architecture_summary.architecture_id,
            available_entity_ids=architecture_summary.entity_ids,
            available_relationship_ids=architecture_summary.relationship_ids,
            issues=issues,
        )

    for section_name in (
        "architecture_id",
        "entities",
        "entity_relationships",
        "features",
        "decisions",
    ):
        if section_name not in raw_spec:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="missing_required_section",
                    message=f"The {section_name} section is required.",
                    path=section_name,
                )
            )

    proposed_architecture_id = _required_string(
        raw_spec.get("architecture_id"),
        path="architecture_id",
        issues=issues,
        issue_code="architecture_id_missing",
        issue_message="The architecture_id field is required.",
    )
    if (
        proposed_architecture_id is not None
        and architecture_summary.architecture_id is not None
        and proposed_architecture_id != architecture_summary.architecture_id
    ):
        issues.append(
            ImplementationSpecificationValidationIssue(
                code="architecture_id_mismatch",
                message=(
                    f"Architecture id {proposed_architecture_id!r} does not match "
                    "the architecture specification id "
                    f"{architecture_summary.architecture_id!r}."
                ),
                path="architecture_id",
            )
        )

    entity_ids = _collect_entity_references(
        _coerce_sequence(
            raw_spec.get("entities"),
            path="entities",
            issues=issues,
            issue_code="invalid_entities_section",
            issue_message="entities must be a list of entity mappings.",
        ),
        available_entity_ids=set(architecture_summary.entity_ids),
        issues=issues,
    )
    if not entity_ids:
        issues.append(
            ImplementationSpecificationValidationIssue(
                code="no_entities_defined",
                message="Define at least one entity.",
                path="entities",
            )
        )

    _collect_relationship_references(
        _coerce_sequence(
            raw_spec.get("entity_relationships"),
            path="entity_relationships",
            issues=issues,
            issue_code="invalid_entity_relationships_section",
            issue_message=(
                "entity_relationships must be a list of relationship mappings."
            ),
        ),
        available_relationship_ids=set(architecture_summary.relationship_ids),
        issues=issues,
    )

    feature_ids = _collect_features(
        _coerce_sequence(
            raw_spec.get("features"),
            path="features",
            issues=issues,
            issue_code="invalid_features_section",
            issue_message="features must be a list of feature mappings.",
        ),
        issues=issues,
    )
    if not feature_ids:
        issues.append(
            ImplementationSpecificationValidationIssue(
                code="no_features_defined",
                message="Define at least one feature.",
                path="features",
            )
        )

    decision_ids = _collect_decisions(
        _coerce_sequence(
            raw_spec.get("decisions"),
            path="decisions",
            issues=issues,
            issue_code="invalid_decisions_section",
            issue_message="decisions must be a list of decision mappings.",
        ),
        issues=issues,
        used_ids=feature_ids,
    )
    if not decision_ids:
        issues.append(
            ImplementationSpecificationValidationIssue(
                code="no_decisions_defined",
                message="Define at least one decision.",
                path="decisions",
            )
        )

    return ImplementationSpecificationValidationReport(
        validation_successful=not issues,
        architecture_id=architecture_summary.architecture_id,
        available_entity_ids=architecture_summary.entity_ids,
        available_relationship_ids=architecture_summary.relationship_ids,
        issues=issues,
    )


def _load_architecture_specification_summary(
    repo_root: Path,
    *,
    architecture_specification_path: str | Path | None,
    issues: list[ImplementationSpecificationValidationIssue] | None,
) -> _ArchitectureSpecificationSummary | None:
    resolved_path = _resolve_input_path(
        repo_root,
        architecture_specification_path,
        default_path=_DEFAULT_ARCHITECTURE_SPECIFICATION_PATH,
    )
    try:
        raw_yaml = resolved_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if issues is not None:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="architecture_specification_missing",
                    message=(
                        "The architecture specification file does not exist: "
                        f"{resolved_path}"
                    ),
                    path=str(resolved_path),
                )
            )
        return None

    try:
        raw_spec = _load_yaml_mapping(raw_yaml)
    except Exception as exc:  # noqa: BLE001
        if issues is not None:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="invalid_architecture_specification",
                    message=(
                        f"Could not parse the architecture specification YAML: {exc}"
                    ),
                    path=str(resolved_path),
                )
            )
        return None

    architecture_id = _optional_string(raw_spec.get("id"))
    if architecture_id is None and issues is not None:
        issues.append(
            ImplementationSpecificationValidationIssue(
                code="architecture_id_missing",
                message="The architecture specification must include an id.",
                path=f"{resolved_path}#id",
            )
        )

    entity_ids = _collect_architecture_ids(raw_spec.get("entities"))
    relationship_ids = _collect_architecture_ids(raw_spec.get("entity_relationships"))

    return _ArchitectureSpecificationSummary(
        architecture_id=architecture_id,
        title=_optional_string(raw_spec.get("title")),
        entity_ids=entity_ids,
        relationship_ids=relationship_ids,
    )


def _collect_architecture_ids(
    raw_value: object,
) -> list[str]:
    if not isinstance(raw_value, Sequence) or isinstance(raw_value, (str, bytes)):
        return []

    ids: list[str] = []
    seen_ids: set[str] = set()
    for raw_item in raw_value:
        if not isinstance(raw_item, Mapping):
            continue

        item_id = _optional_string(raw_item.get("id"))
        if item_id is None or item_id in seen_ids:
            continue

        seen_ids.add(item_id)
        ids.append(item_id)

    return ids


def _collect_entity_references(
    raw_entities: Sequence[object],
    *,
    available_entity_ids: set[str],
    issues: list[ImplementationSpecificationValidationIssue],
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
        if entity_id is None:
            continue

        if entity_id in entity_ids:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="duplicate_entity_id",
                    message=f"Entity id {entity_id!r} appears more than once.",
                    path=f"entities[{index}].id",
                )
            )
            continue

        if entity_id not in available_entity_ids:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="unknown_architecture_entity",
                    message=(
                        f"Entity {entity_id!r} is not listed in the selected "
                        "architecture specification."
                    ),
                    path=f"entities[{index}].id",
                )
            )
            continue

        entity_ids.add(entity_id)

    return entity_ids


def _collect_relationship_references(
    raw_relationships: Sequence[object],
    *,
    available_relationship_ids: set[str],
    issues: list[ImplementationSpecificationValidationIssue],
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
        if relationship_id is None:
            continue

        if relationship_id in relationship_ids:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="duplicate_relationship_id",
                    message=(
                        f"Entity relationship id {relationship_id!r} appears more "
                        "than once."
                    ),
                    path=f"entity_relationships[{index}].id",
                )
            )
            continue

        if relationship_id not in available_relationship_ids:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="unknown_architecture_relationship",
                    message=(
                        f"Entity relationship {relationship_id!r} is not listed in "
                        "the selected architecture specification."
                    ),
                    path=f"entity_relationships[{index}].id",
                )
            )
            continue

        relationship_ids.add(relationship_id)

    return relationship_ids


def _collect_features(
    raw_features: Sequence[object],
    *,
    issues: list[ImplementationSpecificationValidationIssue],
) -> set[str]:
    feature_ids: set[str] = set()
    for index, raw_feature in enumerate(raw_features):
        feature = _coerce_mapping(
            raw_feature,
            path=f"features[{index}]",
            issues=issues,
            issue_code="invalid_feature_entry",
            issue_message="Each feature entry must be a mapping.",
        )
        if feature is None:
            continue

        feature_id = _required_string(
            feature.get("id"),
            path=f"features[{index}].id",
            issues=issues,
            issue_code="feature_id_missing",
            issue_message="Each feature must include an id.",
        )
        description = _required_string(
            feature.get("description"),
            path=f"features[{index}].description",
            issues=issues,
            issue_code="feature_description_missing",
            issue_message="Each feature must include a description.",
        )
        requirements = _coerce_sequence(
            feature.get("functional_requirements"),
            path=f"features[{index}].functional_requirements",
            issues=issues,
            issue_code="invalid_feature_requirements_section",
            issue_message=(
                "functional_requirements must be a list of requirement strings."
            ),
        )
        if feature_id is None or description is None:
            continue

        if feature_id in feature_ids:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="duplicate_specification_id",
                    message=(
                        f"Specification id {feature_id!r} appears more than once."
                    ),
                    path=f"features[{index}].id",
                )
            )
            continue

        if not requirements:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="feature_functional_requirements_missing",
                    message=(
                        "Each feature must include at least one functional requirement."
                    ),
                    path=f"features[{index}].functional_requirements",
                )
            )
            continue

        for requirement_index, requirement in enumerate(requirements):
            _required_string(
                requirement,
                path=(
                    f"features[{index}].functional_requirements[{requirement_index}]"
                ),
                issues=issues,
                issue_code="feature_functional_requirement_missing",
                issue_message="Functional requirements must be strings.",
            )

        feature_ids.add(feature_id)

    return feature_ids


def _collect_decisions(
    raw_decisions: Sequence[object],
    *,
    issues: list[ImplementationSpecificationValidationIssue],
    used_ids: set[str],
) -> set[str]:
    decision_ids: set[str] = set()
    seen_ids: set[str] = set(used_ids)
    for index, raw_decision in enumerate(raw_decisions):
        decision = _coerce_mapping(
            raw_decision,
            path=f"decisions[{index}]",
            issues=issues,
            issue_code="invalid_decision_entry",
            issue_message="Each decision entry must be a mapping.",
        )
        if decision is None:
            continue

        decision_id = _required_string(
            decision.get("id"),
            path=f"decisions[{index}].id",
            issues=issues,
            issue_code="decision_id_missing",
            issue_message="Each decision must include an id.",
        )
        _required_string(
            decision.get("description"),
            path=f"decisions[{index}].description",
            issues=issues,
            issue_code="decision_description_missing",
            issue_message="Each decision must include a description.",
        )
        if decision_id is None:
            continue

        if decision_id in seen_ids:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="duplicate_specification_id",
                    message=(
                        f"Specification id {decision_id!r} appears more than once."
                    ),
                    path=f"decisions[{index}].id",
                )
            )
            continue

        seen_ids.add(decision_id)
        decision_ids.add(decision_id)

    return decision_ids


def _load_yaml_mapping(raw_yaml: str) -> Mapping[str, Any]:
    loaded = yaml.safe_load(raw_yaml)
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise TypeError("Top-level implementation specification must be a mapping.")
    return cast(Mapping[str, Any], loaded)


def _coerce_sequence(
    raw_value: object,
    *,
    path: str,
    issues: list[ImplementationSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> Sequence[object]:
    if raw_value is None:
        return ()
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        return raw_value
    issues.append(
        ImplementationSpecificationValidationIssue(
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
    issues: list[ImplementationSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> Mapping[str, Any] | None:
    if isinstance(raw_value, Mapping):
        return cast(Mapping[str, Any], raw_value)

    issues.append(
        ImplementationSpecificationValidationIssue(
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
    issues: list[ImplementationSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> str | None:
    if raw_value is None:
        issues.append(
            ImplementationSpecificationValidationIssue(
                code=issue_code,
                message=issue_message,
                path=path,
            )
        )
        return None

    value = str(raw_value).strip()
    if value == "":
        issues.append(
            ImplementationSpecificationValidationIssue(
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


def _resolve_output_path(repo_root: Path, output_path: str | Path | None) -> Path:
    if output_path is None:
        return repo_root / _DEFAULT_OUTPUT_PATH

    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        resolved_output_path = repo_root / resolved_output_path

    return resolved_output_path


def _resolve_input_path(
    repo_root: Path,
    input_path: str | Path | None,
    *,
    default_path: Path,
) -> Path:
    if input_path is None:
        return repo_root / default_path

    resolved_input_path = Path(input_path)
    if not resolved_input_path.is_absolute():
        resolved_input_path = repo_root / resolved_input_path

    return resolved_input_path


def _report_to_data(
    report: ImplementationSpecificationValidationReport,
) -> Mapping[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "architecture_id": report.architecture_id,
        "available_entity_ids": report.available_entity_ids,
        "available_relationship_ids": report.available_relationship_ids,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                **({"path": issue.path} if issue.path is not None else {}),
            }
            for issue in report.issues
        ],
    }
