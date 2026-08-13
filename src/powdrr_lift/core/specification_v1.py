"""Shared validation rules for specification-v1 module and tool sections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from powdrr_lift.core.specification_actions import ENTITY_ACTIONS

_MODULE_FIELDS = {
    "id",
    "action",
    "parent_module",
    "relative_location",
    "related_modules",
    "purpose",
}
_TOOL_FIELDS = {
    "id",
    "action",
    "related_module",
    "related_modules",
    "labels",
    "when_to_use",
    "template",
    "how_to_use",
    "evidence",
}


@dataclass(frozen=True, slots=True)
class SpecificationV1Issue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ModuleToolValidationResult:
    module_ids: set[str] = field(default_factory=set)
    tool_ids: set[str] = field(default_factory=set)
    issues: list[SpecificationV1Issue] = field(default_factory=list)


def validate_module_tool_sections(
    raw_modules: Sequence[object],
    raw_tools: Sequence[object],
    *,
    module_ids: set[str] | None = None,
) -> ModuleToolValidationResult:
    """Validate shared specification-v1 module/tool shape and references."""
    issues: list[SpecificationV1Issue] = []
    discovered_module_ids = set(module_ids or ())
    discovered_tool_ids: set[str] = set()
    module_items: list[tuple[int, Mapping[str, Any], str]] = []

    for index, raw_module in enumerate(raw_modules):
        path = f"modules[{index}]"
        if not isinstance(raw_module, Mapping):
            issues.append(
                SpecificationV1Issue(
                    "invalid_module_entry", "Each module entry must be a mapping.", path
                )
            )
            continue
        _check_unknown_fields(raw_module, _MODULE_FIELDS, f"modules[{index}]", issues)
        module_id = _required_string(
            raw_module.get("id"),
            f"{path}.id",
            issues,
            "module_id_missing",
            "Each module must include an id.",
        )
        _validate_action(raw_module.get("action"), f"{path}.action", issues)
        if module_id is None:
            continue
        if module_id in discovered_module_ids:
            issues.append(
                SpecificationV1Issue(
                    "duplicate_module_id",
                    f"Module id {module_id!r} appears more than once.",
                    f"{path}.id",
                )
            )
            continue
        discovered_module_ids.add(module_id)
        module_items.append((index, raw_module, module_id))

    for index, module, _ in module_items:
        _validate_optional_reference(
            module.get("parent_module"),
            discovered_module_ids,
            f"modules[{index}].parent_module",
            issues,
        )
        _validate_reference_list(
            module.get("related_modules"),
            discovered_module_ids,
            f"modules[{index}].related_modules",
            issues,
        )

    for index, raw_tool in enumerate(raw_tools):
        path = f"tools[{index}]"
        if not isinstance(raw_tool, Mapping):
            issues.append(
                SpecificationV1Issue(
                    "invalid_tool_entry", "Each tool entry must be a mapping.", path
                )
            )
            continue
        _check_unknown_fields(raw_tool, _TOOL_FIELDS, f"tools[{index}]", issues)
        tool_id = _required_string(
            raw_tool.get("id"),
            f"{path}.id",
            issues,
            "tool_id_missing",
            "Each tool must include an id.",
        )
        _validate_action(raw_tool.get("action"), f"{path}.action", issues)
        _validate_labels(raw_tool.get("labels"), f"{path}.labels", issues)
        if tool_id is None:
            continue
        if tool_id in discovered_tool_ids:
            issues.append(
                SpecificationV1Issue(
                    "duplicate_tool_id",
                    f"Tool id {tool_id!r} appears more than once.",
                    f"{path}.id",
                )
            )
            continue
        discovered_tool_ids.add(tool_id)
        _validate_optional_reference(
            raw_tool.get("related_module"),
            discovered_module_ids,
            f"{path}.related_module",
            issues,
        )
        _validate_reference_list(
            raw_tool.get("related_modules"),
            discovered_module_ids,
            f"{path}.related_modules",
            issues,
        )

    return ModuleToolValidationResult(
        discovered_module_ids, discovered_tool_ids, issues
    )


def validate_module_tool_references(
    raw_modules: Sequence[Mapping[str, Any]],
    raw_tools: Sequence[Mapping[str, Any]],
    *,
    module_ids: set[str],
) -> list[SpecificationV1Issue]:
    """Validate module/tool references against the canonical module id set."""
    issues: list[SpecificationV1Issue] = []
    for index, module in enumerate(raw_modules):
        _validate_optional_reference(
            module.get("parent_module"),
            module_ids,
            f"modules[{index}].parent_module",
            issues,
        )
        _validate_reference_list(
            module.get("related_modules"),
            module_ids,
            f"modules[{index}].related_modules",
            issues,
        )
    for index, tool in enumerate(raw_tools):
        _validate_optional_reference(
            tool.get("related_module"),
            module_ids,
            f"tools[{index}].related_module",
            issues,
        )
        _validate_reference_list(
            tool.get("related_modules"),
            module_ids,
            f"tools[{index}].related_modules",
            issues,
        )
    return issues


def _required_string(
    value: object,
    path: str,
    issues: list[SpecificationV1Issue],
    code: str,
    message: str,
) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    issues.append(SpecificationV1Issue(code, message, path))
    return None


def _check_unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    path: str,
    issues: list[SpecificationV1Issue],
) -> None:
    for key in sorted(set(value) - allowed):
        issues.append(
            SpecificationV1Issue(
                "unknown_field",
                f"Unknown field {key!r}.",
                f"{path}.{key}",
            )
        )


def _validate_action(
    value: object, path: str, issues: list[SpecificationV1Issue]
) -> None:
    if not isinstance(value, str) or value not in ENTITY_ACTIONS:
        issues.append(
            SpecificationV1Issue(
                "invalid_entity_action",
                "Each entity action must be one of 'added', 'removed', or 'changed'.",
                path,
            )
        )


def _validate_labels(
    value: object, path: str, issues: list[SpecificationV1Issue]
) -> None:
    if value is None:
        return
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        issues.append(
            SpecificationV1Issue(
                "invalid_tool_labels",
                "Tool labels must be a list of non-empty strings.",
                path,
            )
        )
        return
    for index, label in enumerate(value):
        if not isinstance(label, str) or not label.strip():
            issues.append(
                SpecificationV1Issue(
                    "invalid_tool_label",
                    "Each tool label must be a non-empty string.",
                    f"{path}[{index}]",
                )
            )


def _validate_optional_reference(
    value: object, module_ids: set[str], path: str, issues: list[SpecificationV1Issue]
) -> None:
    if value is None:
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        _validate_reference_list(value, module_ids, path, issues)
        return
    if not isinstance(value, str) or not value.strip():
        issues.append(
            SpecificationV1Issue(
                "invalid_module_reference",
                "Module references must be module ids.",
                path,
            )
        )
    elif value not in module_ids:
        issues.append(
            SpecificationV1Issue(
                "unknown_module_reference",
                f"Module id {value!r} is not listed in modules.",
                path,
            )
        )


def _validate_reference_list(
    value: object, module_ids: set[str], path: str, issues: list[SpecificationV1Issue]
) -> None:
    if value is None:
        return
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        issues.append(
            SpecificationV1Issue(
                "invalid_module_reference_list",
                "related_modules must be a list of module ids.",
                path,
            )
        )
        return
    for index, reference in enumerate(value):
        _validate_optional_reference(reference, module_ids, f"{path}[{index}]", issues)
