"""Service-owned validation policy for system specifications."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from powdrr_service.structrr.user_story_validation import validate_story_document

_ALLOWED_STATES = {"added", "removed", "supercedes"}


@dataclass(frozen=True, slots=True)
class SystemSpecificationValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class SystemSpecificationValidationReport:
    validation_successful: bool
    system_id: str | None
    requirement_ids: list[str] = field(default_factory=list)
    approach_ids: list[str] = field(default_factory=list)
    issues: list[SystemSpecificationValidationIssue] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _SystemSectionItem:
    id: str
    description: str | None
    state: str | None
    supercedes: list[str]
    path: str
    raw: dict[str, Any]


def build_system_specification_validation_report(
    proposed_system_specification_yaml: str,
) -> SystemSpecificationValidationReport:
    issues: list[SystemSpecificationValidationIssue] = []

    try:
        raw_node = yaml.compose(proposed_system_specification_yaml)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            SystemSpecificationValidationIssue(
                code="invalid_yaml",
                message=f"Could not parse proposed system specification YAML: {exc}",
            )
        )
        return SystemSpecificationValidationReport(
            validation_successful=False,
            system_id=None,
            requirement_ids=[],
            approach_ids=[],
            issues=issues,
        )

    if raw_node is not None:
        _collect_duplicate_key_issues(raw_node, path="", issues=issues)

    try:
        raw_spec = _load_yaml_mapping(proposed_system_specification_yaml)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            SystemSpecificationValidationIssue(
                code="invalid_yaml",
                message=f"Could not parse proposed system specification YAML: {exc}",
            )
        )
        return SystemSpecificationValidationReport(
            validation_successful=False,
            system_id=None,
            requirement_ids=[],
            approach_ids=[],
            issues=issues,
        )

    _validate_template_boilerplate_removed(
        proposed_system_specification_yaml,
        issues=issues,
    )

    system_id = _required_string(
        raw_spec.get("id"),
        path="id",
        issues=issues,
        issue_code="system_id_missing",
        issue_message="The id field is required.",
    )

    requirement_items = _coerce_sequence(
        raw_spec.get("requirements"),
        path="requirements",
        issues=issues,
        issue_code="invalid_requirements_section",
        issue_message="requirements must be a list of requirement mappings.",
    )
    approach_items = _coerce_sequence(
        raw_spec.get("approach"),
        path="approach",
        issues=issues,
        issue_code="invalid_approach_section",
        issue_message="approach must be a list of approach mappings.",
    )

    _validate_boilerplate_removed(
        requirement_items,
        section_name="requirements",
        issues=issues,
    )
    _validate_boilerplate_removed(
        approach_items,
        section_name="approach",
        issues=issues,
    )

    requirement_entries = _collect_section_items(
        requirement_items,
        section_name="requirements",
        issues=issues,
    )
    approach_entries = _collect_section_items(
        approach_items,
        section_name="approach",
        issues=issues,
    )

    _validate_optional_title(raw_spec, issues=issues)

    requirement_ids = [entry.id for entry in requirement_entries]
    approach_ids = [entry.id for entry in approach_entries]
    requirement_id_set = set(requirement_ids)
    approach_id_set = set(approach_ids)

    for entry in requirement_entries:
        _validate_section_item(
            entry,
            section_name="requirements",
            same_section_ids=requirement_id_set,
            issues=issues,
        )

    for entry in approach_entries:
        if entry.id in requirement_id_set:
            issues.append(
                SystemSpecificationValidationIssue(
                    code="duplicate_section_item_id",
                    message=(
                        f"Approach id {entry.id!r} appears in both requirements "
                        "and approach."
                    ),
                    path=f"{entry.path}.id",
                )
            )
        _validate_section_item(
            entry,
            section_name="approach",
            same_section_ids=approach_id_set,
            issues=issues,
        )

    issues.extend(
        SystemSpecificationValidationIssue(i.code, i.message, i.subject_id)
        for i in validate_story_document(raw_spec).issues
    )
    return SystemSpecificationValidationReport(
        validation_successful=not issues,
        system_id=system_id,
        requirement_ids=requirement_ids,
        approach_ids=approach_ids,
        issues=issues,
    )


def _collect_section_items(
    raw_items: Sequence[object],
    *,
    section_name: str,
    issues: list[SystemSpecificationValidationIssue],
) -> list[_SystemSectionItem]:
    items: list[_SystemSectionItem] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        item = _coerce_mapping(
            raw_item,
            path=f"{section_name}[{index}]",
            issues=issues,
            issue_code="invalid_section_item",
            issue_message=(
                f"Each {_section_label(section_name)} entry must be a mapping."
            ),
        )
        if item is None:
            continue

        item_id = _required_string(
            item.get("id"),
            path=f"{section_name}[{index}].id",
            issues=issues,
            issue_code="section_item_id_missing",
            issue_message=f"Each {_section_label(section_name)} must include an id.",
        )
        if item_id is None:
            continue

        if item_id in seen_ids:
            issues.append(
                SystemSpecificationValidationIssue(
                    code="duplicate_section_item_id",
                    message=(
                        f"{_section_label(section_name).capitalize()} id {item_id!r} "
                        "appears more than once."
                    ),
                    path=f"{section_name}[{index}].id",
                )
            )
            continue

        seen_ids.add(item_id)
        items.append(
            _SystemSectionItem(
                id=item_id,
                description=_optional_string(item.get("description")),
                state=_optional_string(item.get("state")),
                supercedes=_coerce_string_list(
                    item.get("supercedes"),
                    path=f"{section_name}[{index}].supercedes",
                    issues=issues,
                    issue_code="invalid_supercedes_section",
                    issue_message=(
                        f"{section_name}[{index}].supercedes must be a list of ids."
                    ),
                ),
                path=f"{section_name}[{index}]",
                raw=item,
            )
        )

    return items


def _validate_optional_title(
    raw_spec: Mapping[str, Any],
    *,
    issues: list[SystemSpecificationValidationIssue],
) -> None:
    if "title" not in raw_spec:
        return
    if _is_empty_optional_value(raw_spec.get("title")):
        issues.append(
            SystemSpecificationValidationIssue(
                code="optional_value_empty",
                message=(
                    "The title field is optional, but if provided it must not be empty."
                ),
                path="title",
            )
        )


def _collect_duplicate_key_issues(
    node: Any,
    *,
    path: str,
    issues: list[SystemSpecificationValidationIssue],
) -> None:
    if isinstance(node, MappingNode):
        seen_keys: set[str] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                continue

            key = key_node.value
            child_path = f"{path}.{key}" if path else key
            if key in seen_keys:
                issues.append(
                    SystemSpecificationValidationIssue(
                        code="duplicate_key_in_section",
                        message=(
                            f"Key {key!r} appears more than once in "
                            f"{path or 'the document'}."
                        ),
                        path=child_path,
                    )
                )
                continue

            seen_keys.add(key)
            _collect_duplicate_key_issues(
                value_node,
                path=child_path,
                issues=issues,
            )
    elif isinstance(node, SequenceNode):
        for index, value_node in enumerate(node.value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            _collect_duplicate_key_issues(
                value_node,
                path=child_path,
                issues=issues,
            )


def _validate_boilerplate_removed(
    raw_items: Sequence[object],
    *,
    section_name: str,
    issues: list[SystemSpecificationValidationIssue],
) -> None:
    for index, raw_item in enumerate(raw_items):
        item = _coerce_mapping(
            raw_item,
            path=f"{section_name}[{index}]",
            issues=issues,
            issue_code="invalid_section_item",
            issue_message=(
                f"Each {_section_label(section_name)} entry must be a mapping."
            ),
        )
        if item is None:
            continue

        if (
            _is_empty_optional_value(item.get("id"))
            and _is_empty_optional_value(item.get("description"))
            and _is_empty_optional_value(item.get("state"))
        ):
            issues.append(
                SystemSpecificationValidationIssue(
                    code="boilerplate_not_removed",
                    message=(
                        f"Remove the {section_name} boilerplate placeholder "
                        "entry before finalizing the system specification."
                    ),
                    path=f"{section_name}[{index}]",
                )
            )


def _validate_template_boilerplate_removed(
    proposed_system_specification_yaml: str,
    *,
    issues: list[SystemSpecificationValidationIssue],
) -> None:
    boilerplate_markers = (
        "# System specification template.",
        "# - Delete these instructions and replace them with this comment at the top:",
    )
    if any(
        marker in proposed_system_specification_yaml for marker in boilerplate_markers
    ):
        issues.append(
            SystemSpecificationValidationIssue(
                code="template_boilerplate_not_removed",
                message=(
                    "Remove the template instructions before validating the "
                    "system specification."
                ),
                path=None,
            )
        )


def _validate_section_item(
    item: _SystemSectionItem,
    *,
    section_name: str,
    same_section_ids: set[str],
    issues: list[SystemSpecificationValidationIssue],
) -> None:
    if item.state is None:
        issues.append(
            SystemSpecificationValidationIssue(
                code="section_item_state_missing",
                message=f"Each {_section_label(section_name)} must include a state.",
                path=f"{item.path}.state",
            )
        )
        return

    if item.state not in _ALLOWED_STATES:
        issues.append(
            SystemSpecificationValidationIssue(
                code="invalid_section_item_state",
                message=(
                    f"{_section_label(section_name).capitalize()} {item.id!r} has "
                    f"invalid state {item.state!r}; use added, removed, or "
                    "supercedes."
                ),
                path=f"{item.path}.state",
            )
        )
        return

    if item.state == "added":
        if item.description is None or not item.description.strip():
            issues.append(
                SystemSpecificationValidationIssue(
                    code="added_description_required",
                    message=(
                        f"{_section_label(section_name).capitalize()} {item.id!r} "
                        "must include a description when state is added."
                    ),
                    path=f"{item.path}.description",
                )
            )
    elif item.state == "removed":
        if "description" in item.raw and _is_empty_optional_value(
            item.raw.get("description")
        ):
            issues.append(
                SystemSpecificationValidationIssue(
                    code="optional_value_empty",
                    message=(
                        f"{_section_label(section_name).capitalize()} {item.id!r} "
                        "must remove the description field when state is removed."
                    ),
                    path=f"{item.path}.description",
                )
            )
        elif item.description not in (None, ""):
            issues.append(
                SystemSpecificationValidationIssue(
                    code="removed_description_not_allowed",
                    message=(
                        f"{_section_label(section_name).capitalize()} {item.id!r} "
                        "must not include a description when state is removed."
                    ),
                    path=f"{item.path}.description",
                )
            )
    else:
        if "supercedes" not in item.raw:
            issues.append(
                SystemSpecificationValidationIssue(
                    code="supercedes_required",
                    message=(
                        f"{_section_label(section_name).capitalize()} {item.id!r} "
                        "must list same-section ids in supercedes when state is "
                        "supercedes."
                    ),
                    path=f"{item.path}.supercedes",
                )
            )
            return

        if _is_empty_optional_value(item.raw.get("supercedes")):
            issues.append(
                SystemSpecificationValidationIssue(
                    code="optional_value_empty",
                    message=(
                        f"{_section_label(section_name).capitalize()} {item.id!r} "
                        "must not leave supercedes empty."
                    ),
                    path=f"{item.path}.supercedes",
                )
            )
            return

        for superceded_id in item.supercedes:
            if superceded_id == item.id:
                issues.append(
                    SystemSpecificationValidationIssue(
                        code="supercedes_self_reference",
                        message=(
                            f"{_section_label(section_name).capitalize()} {item.id!r} "
                            "cannot supercede itself."
                        ),
                        path=f"{item.path}.supercedes",
                    )
                )
                continue

            if superceded_id not in same_section_ids:
                issues.append(
                    SystemSpecificationValidationIssue(
                        code="unknown_superceded_id",
                        message=(
                            f"{_section_label(section_name).capitalize()} {item.id!r} "
                            f"supercedes unknown {_section_label(section_name)} id "
                            f"{superceded_id!r}."
                        ),
                        path=f"{item.path}.supercedes",
                    )
                )

    if item.state != "supercedes" and item.supercedes:
        issues.append(
            SystemSpecificationValidationIssue(
                code="unexpected_supercedes",
                message=(
                    f"{_section_label(section_name).capitalize()} {item.id!r} "
                    "should only set supercedes when state is supercedes."
                ),
                path=f"{item.path}.supercedes",
            )
        )


def _load_yaml_mapping(raw_yaml: str) -> dict[str, Any]:
    loaded = yaml.safe_load(raw_yaml)
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise TypeError("YAML root must be a mapping.")
    return dict(cast(Mapping[str, Any], loaded))


def _coerce_mapping(
    value: object,
    *,
    path: str,
    issues: list[SystemSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        issues.append(
            SystemSpecificationValidationIssue(
                code=issue_code,
                message=issue_message,
                path=path,
            )
        )
        return None
    return dict(cast(Mapping[str, Any], value))


def _coerce_sequence(
    value: object,
    *,
    path: str,
    issues: list[SystemSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        issues.append(
            SystemSpecificationValidationIssue(
                code=issue_code,
                message=issue_message,
                path=path,
            )
        )
        return []
    return list(value)


def _coerce_string_list(
    value: object,
    *,
    path: str,
    issues: list[SystemSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        issues.append(
            SystemSpecificationValidationIssue(
                code=issue_code,
                message=issue_message,
                path=path,
            )
        )
        return []

    items: list[str] = []
    for index, raw_item in enumerate(value):
        item = _optional_string(raw_item)
        if item is None:
            issues.append(
                SystemSpecificationValidationIssue(
                    code=issue_code,
                    message=f"Each entry in {path} must be a string id.",
                    path=f"{path}[{index}]",
                )
            )
            continue
        items.append(item)
    return items


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def _is_empty_optional_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Mapping):
        return len(value) == 0
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return len(value) == 0
    return False


def _required_string(
    value: object,
    *,
    path: str,
    issues: list[SystemSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> str | None:
    string_value = _optional_string(value)
    if string_value is None or not string_value.strip():
        issues.append(
            SystemSpecificationValidationIssue(
                code=issue_code,
                message=issue_message,
                path=path,
            )
        )
        return None
    return string_value


def _section_label(section_name: str) -> str:
    if section_name == "requirements":
        return "requirement"
    if section_name == "approach":
        return "approach"
    return section_name.rstrip("s")
