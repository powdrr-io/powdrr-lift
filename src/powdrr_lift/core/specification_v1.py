"""Shared validation rules for specification-v1 module and tool sections."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core.specification_actions import ENTITY_ACTIONS
from powdrr_lift.core.validation_messages import ValidationError

_MODULE_FIELDS = {
    "id",
    "action",
    "relative_location",
    "related_modules",
    "purpose",
}
_TOOL_FIELDS = {
    "id",
    "action",
    "related_modules",
    "labels",
    "when_to_use",
    "template",
    "how_to_use",
    "validation_action",
    "evidence",
}


@dataclass(frozen=True, slots=True)
class SpecificationV1Issue(ValidationError):
    pass


@dataclass(frozen=True, slots=True)
class ModuleToolValidationResult:
    module_ids: set[str] = field(default_factory=set)
    tool_ids: set[str] = field(default_factory=set)
    issues: list[SpecificationV1Issue] = field(default_factory=list)


_MAPPING_EMPTY_VALUE_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[^#:\n][^:\n]*):\s*(?P<value>null|~|\[\s*\])"
    r"\s*(?:#.*)?(?:\r?\n)?$"
)
_MAPPING_EMPTY_BLOCK_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[^#:\n][^:\n]*):\s*(?:#.*)?(?:\r?\n)?$"
)
_SEQUENCE_NULL_LINE = re.compile(r"^\s*-\s*(?:null|~)?\s*(?:#.*)?(?:\r?\n)?$")


def normalize_specification_v1_file(path: str | Path) -> bool:
    """Remove safe explicit empty values from a specification-v1 YAML file.

    The normalizer only removes complete YAML lines that represent a null or an
    empty list. It preserves comments and all remaining source text. Returning
    whether the file changed lets callers validate the rewritten content in a
    separate pass, keeping validation locations aligned with the file on disk.
    """
    resolved_path = Path(path)
    original = resolved_path.read_text(encoding="utf-8")
    normalized = normalize_specification_v1_yaml(original)
    if normalized == original:
        return False
    resolved_path.write_text(normalized, encoding="utf-8")
    return True


def normalize_specification_v1_yaml(content: str) -> str:
    """Return content with explicit null and empty-list values omitted.

    Parsing each intermediate result means that removing the only item in a
    block list also removes its now-null parent on the next iteration. If the
    input is malformed, leave it untouched so the normal validator can report
    the original YAML parse error.
    """
    normalized = content
    while True:
        try:
            node = yaml.compose(normalized)
        except yaml.YAMLError:
            return content
        if node is None:
            return normalized

        lines = normalized.splitlines(keepends=True)
        lines_to_remove: set[int] = set()

        def mark_node(
            node_to_remove: yaml.Node,
            source_lines: list[str] = lines,
            removed_lines: set[int] = lines_to_remove,
        ) -> None:
            line_number = node_to_remove.start_mark.line
            if line_number >= len(source_lines):
                return
            line = source_lines[line_number]
            if (
                _MAPPING_EMPTY_VALUE_LINE.match(line)
                or _MAPPING_EMPTY_BLOCK_LINE.match(line)
                or _SEQUENCE_NULL_LINE.match(line)
            ):
                removed_lines.add(line_number)

        def visit(current: yaml.Node) -> None:
            if isinstance(current, yaml.ScalarNode):
                if current.tag == "tag:yaml.org,2002:null":
                    mark_node(current)
                return
            if isinstance(current, yaml.SequenceNode):
                if not current.value:
                    mark_node(current)
                for child in current.value:
                    visit(child)
                return
            if isinstance(current, yaml.MappingNode):
                for key_node, value_node in current.value:
                    if isinstance(value_node, yaml.ScalarNode) and value_node.tag == (
                        "tag:yaml.org,2002:null"
                    ):
                        mark_node(key_node)
                    elif (
                        isinstance(value_node, yaml.SequenceNode)
                        and not value_node.value
                    ):
                        mark_node(key_node)
                    visit(value_node)

        visit(node)
        if not lines_to_remove:
            return normalized
        normalized = "".join(
            line for index, line in enumerate(lines) if index not in lines_to_remove
        )


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
        issues.extend(validate_no_explicit_empty_values(raw_module, path=path))
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
        issues.extend(validate_no_explicit_empty_values(raw_tool, path=path))
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
        _validate_reference_list(
            module.get("related_modules"),
            module_ids,
            f"modules[{index}].related_modules",
            issues,
        )
    for index, tool in enumerate(raw_tools):
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


def validate_no_explicit_empty_values(
    value: object,
    *,
    path: str = "",
) -> list[SpecificationV1Issue]:
    """Reject explicit nulls and empty lists; omission represents defaults."""
    issues: list[SpecificationV1Issue] = []

    def visit(current: object, current_path: str) -> None:
        if current is None:
            issues.append(
                SpecificationV1Issue(
                    "explicit_empty_value",
                    "Remove this null value; omit the field when it is not needed.",
                    current_path,
                )
            )
            return
        if isinstance(current, Mapping):
            for key, child in current.items():
                child_path = f"{current_path}.{key}" if current_path else str(key)
                visit(child, child_path)
            return
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            if not current:
                issues.append(
                    SpecificationV1Issue(
                        "explicit_empty_value",
                        "Remove this empty list; omit the field when it is not needed.",
                        current_path,
                    )
                )
                return
            for index, child in enumerate(current):
                visit(child, f"{current_path}[{index}]")

    visit(value, path)
    return issues
