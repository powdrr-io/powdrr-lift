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
_ALLOWED_ACTIONS = {"added", "removed"}


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


@dataclass(frozen=True, slots=True)
class _ImplementationSpecificationSectionItem:
    id: str
    action: str
    supercedes: list[str]
    path: str
    raw: Mapping[str, Any]


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
        "# - Give every entity and entity relationship an `action` of `added`",
        "#   or `removed`.",
        "# - Include `supercedes` only when it lists one or more ids; omit the",
        "#   field when it would be empty.",
        "# - Treat entity relationships with the same item shape as entities:",
        "#   `id`, `action`, `rationale`, and optional `supercedes`.",
        "# - Give each feature and decision an `action` of `added` or",
        "#   `removed`.",
        "# - Include `supercedes` only when it lists one or more ids; omit the",
        "#   field when it would be empty.",
        "# - Give each feature a unique id, description, and functional",
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
        "title: null",
        f"architecture_id: {json.dumps(architecture_id)}",
        "entities:",
        "  - id: null",
        "    action: null",
        "    rationale: null",
        "entity_relationships:",
        "  - id: null",
        "    action: null",
        "    rationale: null",
        "features:",
        "  - id: null",
        "    action: null",
        "    description: null",
        "    supercedes: null",
        "    functional_requirements:",
        "      - null",
        "decisions:",
        "  - id: null",
        "    action: null",
        "    description: null",
        "    supercedes: null",
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

    entity_ids, entity_items = _collect_section_items(
        _coerce_sequence(
            raw_spec.get("entities"),
            path="entities",
            issues=issues,
            issue_code="invalid_entities_section",
            issue_message="entities must be a list of entity mappings.",
        ),
        available_ids=set(architecture_summary.entity_ids),
        issues=issues,
        section_name="entities",
        item_label="entity",
    )
    _validate_supercedes(
        entity_items,
        available_ids=entity_ids,
        issues=issues,
        item_label="entity",
    )
    if not entity_ids:
        issues.append(
            ImplementationSpecificationValidationIssue(
                code="no_entities_defined",
                message="Define at least one entity.",
                path="entities",
            )
        )

    relationship_ids, relationship_items = _collect_section_items(
        _coerce_sequence(
            raw_spec.get("entity_relationships"),
            path="entity_relationships",
            issues=issues,
            issue_code="invalid_entity_relationships_section",
            issue_message=(
                "entity_relationships must be a list of relationship mappings."
            ),
        ),
        available_ids=set(architecture_summary.relationship_ids),
        issues=issues,
        section_name="entity_relationships",
        item_label="entity relationship",
    )
    _validate_supercedes(
        relationship_items,
        available_ids=relationship_ids,
        issues=issues,
        item_label="entity relationship",
    )

    feature_ids, feature_items = _collect_features(
        _coerce_sequence(
            raw_spec.get("features"),
            path="features",
            issues=issues,
            issue_code="invalid_features_section",
            issue_message="features must be a list of feature mappings.",
        ),
        issues=issues,
    )
    _validate_supercedes(
        feature_items,
        available_ids=feature_ids,
        issues=issues,
        item_label="feature",
    )
    if not feature_ids:
        issues.append(
            ImplementationSpecificationValidationIssue(
                code="no_features_defined",
                message="Define at least one feature.",
                path="features",
            )
        )

    decision_ids, decision_items = _collect_decisions(
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
    _validate_supercedes(
        decision_items,
        available_ids=decision_ids,
        issues=issues,
        item_label="decision",
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


def _collect_section_items(
    raw_items: Sequence[object],
    *,
    available_ids: set[str],
    issues: list[ImplementationSpecificationValidationIssue],
    section_name: str,
    item_label: str,
) -> tuple[set[str], list[_ImplementationSpecificationSectionItem]]:
    item_ids: set[str] = set()
    items: list[_ImplementationSpecificationSectionItem] = []
    for index, raw_item in enumerate(raw_items):
        item = _coerce_mapping(
            raw_item,
            path=f"{section_name}[{index}]",
            issues=issues,
            issue_code="invalid_specification_entry",
            issue_message="Each specification entry must be a mapping.",
        )
        if item is None:
            continue

        item_id = _required_string(
            item.get("id"),
            path=f"{section_name}[{index}].id",
            issues=issues,
            issue_code="specification_id_missing",
            issue_message="Each item must include an id.",
        )
        action = _required_action(
            item.get("action"),
            path=f"{section_name}[{index}].action",
            issues=issues,
        )
        supercedes = _coerce_string_list(
            item.get("supercedes"),
            path=f"{section_name}[{index}].supercedes",
            issues=issues,
        )
        if "supercedes" in item and _is_empty_optional_value(item.get("supercedes")):
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="supercedes_empty",
                    message=(
                        f"Each {item_label} must omit supercedes when it has no ids."
                    ),
                    path=f"{section_name}[{index}].supercedes",
                )
            )
        if item_id is None or action is None:
            continue

        if item_id in item_ids:
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="duplicate_specification_id",
                    message=(f"Specification id {item_id!r} appears more than once."),
                    path=f"{section_name}[{index}].id",
                )
            )
            continue

        if item_id not in available_ids:
            issue_code = (
                "unknown_architecture_entity"
                if item_label == "entity"
                else "unknown_architecture_relationship"
            )
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code=issue_code,
                    message=(
                        f"{item_label.title()} {item_id!r} is not listed in the "
                        "selected architecture specification."
                    ),
                    path=f"{section_name}[{index}].id",
                )
            )
            continue

        item_ids.add(item_id)
        items.append(
            _ImplementationSpecificationSectionItem(
                id=item_id,
                action=action,
                supercedes=supercedes,
                path=f"{section_name}[{index}]",
                raw=item,
            )
        )

    return item_ids, items


def _validate_supercedes(
    items: Sequence[_ImplementationSpecificationSectionItem],
    *,
    available_ids: set[str],
    issues: list[ImplementationSpecificationValidationIssue],
    item_label: str,
) -> None:
    for item in items:
        if "supercedes" not in item.raw:
            continue
        if _is_empty_optional_value(item.raw.get("supercedes")):
            continue

        for superceded_id in item.supercedes:
            if superceded_id not in available_ids:
                issues.append(
                    ImplementationSpecificationValidationIssue(
                        code="unknown_supercedes_id",
                        message=(
                            f"{item_label.title()} {item.id!r} supercedes unknown "
                            f"{item_label} id {superceded_id!r}."
                        ),
                        path=f"{item.path}.supercedes",
                    )
                )


def _required_action(
    raw_value: object,
    *,
    path: str,
    issues: list[ImplementationSpecificationValidationIssue],
) -> str | None:
    value = _required_string(
        raw_value,
        path=path,
        issues=issues,
        issue_code="action_missing",
        issue_message="Each item must include an action.",
    )
    if value is None:
        return None

    if value not in _ALLOWED_ACTIONS:
        issues.append(
            ImplementationSpecificationValidationIssue(
                code="invalid_action",
                message="Each item action must be one of 'added' or 'removed'.",
                path=path,
            )
        )
        return None

    return value


def _coerce_string_list(
    raw_value: object,
    *,
    path: str,
    issues: list[ImplementationSpecificationValidationIssue],
) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, Sequence) or isinstance(raw_value, (str, bytes)):
        issues.append(
            ImplementationSpecificationValidationIssue(
                code="invalid_supercedes_section",
                message="supercedes must be a list of ids.",
                path=path,
            )
        )
        return []

    values: list[str] = []
    for index, raw_item in enumerate(raw_value):
        value = _required_string(
            raw_item,
            path=f"{path}[{index}]",
            issues=issues,
            issue_code="supercedes_id_missing",
            issue_message="supercedes values must be ids.",
        )
        if value is not None:
            values.append(value)
    return values


def _is_empty_optional_value(raw_value: object) -> bool:
    if raw_value is None:
        return True
    if isinstance(raw_value, str):
        return raw_value.strip() == ""
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        return len(raw_value) == 0
    return False


def _collect_features(
    raw_features: Sequence[object],
    *,
    issues: list[ImplementationSpecificationValidationIssue],
) -> tuple[set[str], list[_ImplementationSpecificationSectionItem]]:
    feature_ids: set[str] = set()
    items: list[_ImplementationSpecificationSectionItem] = []
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
        action = _required_action(
            feature.get("action"),
            path=f"features[{index}].action",
            issues=issues,
        )
        description = _required_string(
            feature.get("description"),
            path=f"features[{index}].description",
            issues=issues,
            issue_code="feature_description_missing",
            issue_message="Each feature must include a description.",
        )
        supercedes = _coerce_string_list(
            feature.get("supercedes"),
            path=f"features[{index}].supercedes",
            issues=issues,
        )
        if "supercedes" in feature and _is_empty_optional_value(
            feature.get("supercedes")
        ):
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="supercedes_empty",
                    message="Each feature must omit supercedes when it has no ids.",
                    path=f"features[{index}].supercedes",
                )
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
        if feature_id is None or action is None or description is None:
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
        items.append(
            _ImplementationSpecificationSectionItem(
                id=feature_id,
                action=action,
                supercedes=supercedes,
                path=f"features[{index}]",
                raw=feature,
            )
        )

    return feature_ids, items


def _collect_decisions(
    raw_decisions: Sequence[object],
    *,
    issues: list[ImplementationSpecificationValidationIssue],
    used_ids: set[str],
) -> tuple[set[str], list[_ImplementationSpecificationSectionItem]]:
    decision_ids: set[str] = set()
    seen_ids: set[str] = set(used_ids)
    items: list[_ImplementationSpecificationSectionItem] = []
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
        action = _required_action(
            decision.get("action"),
            path=f"decisions[{index}].action",
            issues=issues,
        )
        _required_string(
            decision.get("description"),
            path=f"decisions[{index}].description",
            issues=issues,
            issue_code="decision_description_missing",
            issue_message="Each decision must include a description.",
        )
        supercedes = _coerce_string_list(
            decision.get("supercedes"),
            path=f"decisions[{index}].supercedes",
            issues=issues,
        )
        if "supercedes" in decision and _is_empty_optional_value(
            decision.get("supercedes")
        ):
            issues.append(
                ImplementationSpecificationValidationIssue(
                    code="supercedes_empty",
                    message="Each decision must omit supercedes when it has no ids.",
                    path=f"decisions[{index}].supercedes",
                )
            )
        if decision_id is None:
            continue
        if action is None:
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
        items.append(
            _ImplementationSpecificationSectionItem(
                id=decision_id,
                action=action,
                supercedes=supercedes,
                path=f"decisions[{index}]",
                raw=decision,
            )
        )

    return decision_ids, items


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
