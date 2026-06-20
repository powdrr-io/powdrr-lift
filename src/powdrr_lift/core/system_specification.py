from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root

_DEFAULT_OUTPUT_PATH = Path("docs") / "system" / "system-specification.yaml"
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


def system_specification_default_output_path(
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return repo_root_path / _DEFAULT_OUTPUT_PATH


def render_system_specification_template(*, title: str | None = None) -> str:
    lines = [
        "# System specification template.",
        "#",
        "# Instructions:",
        "# - Fill in the requirements and approach sections.",
        "# - Set `id` to a unique identifier for this system description.",
        "# - Use `state: added` for new items and include a description.",
        "# - Use `state: removed` for retired items and leave description empty.",
        "# - Use `state: supercedes` when an item replaces same-section ids.",
        "# - Keep ids unique across both sections.",
        "# - Reference only same-section ids in `supercedes`.",
        "#",
        "# Optional title:",
        "# - Set `title` if a human-readable summary is helpful.",
        "version: 1",
        "id: null",
        "title: null",
        "requirements:",
        "  - id: null",
        "    description: null",
        "    state: null",
        "    supercedes: []",
        "approach:",
        "  - id: null",
        "    description: null",
        "    state: null",
        "    supercedes: []",
        "",
    ]

    if title is not None:
        lines[lines.index("title: null")] = f"title: {json.dumps(title)}"

    return "\n".join(lines)


def create_system_specification_template(
    *,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    title: str | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_output_path = _resolve_output_path(repo_root_path, output_path)
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(
        render_system_specification_template(title=title),
        encoding="utf-8",
    )
    return resolved_output_path


def validate_system_specification_yaml(
    proposed_system_specification_yaml: str,
    *,
    repo_root: str | Path | None = None,
) -> str:
    report = build_system_specification_validation_report(
        proposed_system_specification_yaml,
        repo_root=repo_root,
    )
    return yaml.safe_dump(_report_to_data(report), sort_keys=False)


def build_system_specification_validation_report(
    proposed_system_specification_yaml: str,
    *,
    repo_root: str | Path | None = None,
) -> SystemSpecificationValidationReport:
    _ = _resolve_repo_root(repo_root)
    issues: list[SystemSpecificationValidationIssue] = []

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

    for section_name in ("id", "requirements", "approach"):
        if section_name not in raw_spec:
            issues.append(
                SystemSpecificationValidationIssue(
                    code="missing_required_section",
                    message=f"The {section_name} section is required.",
                    path=section_name,
                )
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
            )
        )

    return items


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
        if item.description not in (None, ""):
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
        if not item.supercedes:
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


def _report_to_data(report: SystemSpecificationValidationReport) -> dict[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "system_id": report.system_id,
        "requirement_ids": report.requirement_ids,
        "approach_ids": report.approach_ids,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "path": issue.path,
            }
            for issue in report.issues
        ],
    }


def _section_label(section_name: str) -> str:
    if section_name == "requirements":
        return "requirement"
    if section_name == "approach":
        return "approach"
    return section_name.rstrip("s")


def _resolve_output_path(repo_root: Path, output_path: str | Path | None) -> Path:
    if output_path is None:
        return repo_root / _DEFAULT_OUTPUT_PATH
    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        return repo_root / resolved_output_path
    return resolved_output_path
