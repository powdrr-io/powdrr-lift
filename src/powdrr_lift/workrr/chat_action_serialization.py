"""Small serialization helpers shared by Procedrr operation adapters."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.core import (
    architecture_specification_default_output_path,
    codebase_state_default_output_path,
    current_state_specification_default_output_path,
    feature_pr_specification_default_output_path,
    implementation_specification_default_output_path,
    pr_specification_default_output_path,
    system_map_specification_default_output_path,
    system_specification_default_output_path,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.llm import (
    WorkflowEdit,
    WorkflowFileEdits,
    WorkflowYamlOperation,
)
from powdrr_lift.workrr.paths import resolve_worktree_file_path


def normalize_structured_document_text(path: Path, text: str) -> str:
    """Remove a single Markdown fence when an LLM wraps a structured file."""
    if path.suffix.lower() not in {".json", ".yaml", ".yml"}:
        return text
    match = re.fullmatch(
        r"\s*```(?:json|yaml|yml)?\s*\n(.*?)\n?```\s*",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) + "\n" if match is not None else text


def yaml_operation_to_data(operation: WorkflowYamlOperation) -> dict[str, Any]:
    data: dict[str, Any] = {"op": operation.operation}
    if operation.section is not None:
        data["section"] = operation.section
    if operation.item_id is not None:
        data["id"] = operation.item_id
    if operation.item_index is not None:
        data["index"] = operation.item_index
    if operation.path:
        data["path"] = list(operation.path)
    if operation.value is not None:
        data["value"] = operation.value
    return data


def _edit_to_data(edit: WorkflowEdit) -> dict[str, Any]:
    data: dict[str, Any] = {"kind": edit.kind, "start_line": edit.start_line}
    if edit.end_line is not None:
        data["end_line"] = edit.end_line
    if edit.text is not None:
        data["text"] = edit.text
    return data


def file_edits_to_data(file_edits: WorkflowFileEdits) -> dict[str, Any]:
    return {
        "file_path": file_edits.file_path,
        "edits": [_edit_to_data(edit) for edit in file_edits.edits],
    }


def resolve_generated_file_path_from_command(
    command: object,
    *,
    worktree_root: Path,
) -> Path | None:
    command_items = _command_items(command)
    if not command_items or command_items[0] != "powdrr-lift" or len(command_items) < 2:
        return None
    output_path_value = _extract_command_option(command_items, "--output")
    if output_path_value is not None:
        return resolve_worktree_file_path(output_path_value, worktree_root)
    work_item_name = _extract_command_option(command_items, "--work-item-name")
    if work_item_name is None:
        return None
    subcommand = command_items[1]
    if subcommand == "system-specification":
        return system_specification_default_output_path(work_item_name, worktree_root)
    if subcommand == "architecture-specification":
        return architecture_specification_default_output_path(
            work_item_name, worktree_root
        )
    if subcommand == "implementation-specification":
        return implementation_specification_default_output_path(
            work_item_name, worktree_root
        )
    if subcommand == "pr-specification":
        return pr_specification_default_output_path(work_item_name, worktree_root)
    if subcommand == "feature-pr-specification":
        return feature_pr_specification_default_output_path(
            work_item_name, worktree_root
        )
    if subcommand == "system-map-specification":
        return system_map_specification_default_output_path(
            work_item_name, worktree_root
        )
    if subcommand == "current-state":
        return current_state_specification_default_output_path(worktree_root)
    if subcommand == "codebase-state":
        return codebase_state_default_output_path(worktree_root)
    return None


def _command_items(command: object) -> list[str]:
    if isinstance(command, str):
        return [item for item in shlex.split(command) if item]
    if isinstance(command, Sequence) and not isinstance(
        command, (str, bytes, bytearray)
    ):
        items: list[str] = []
        for item in command:
            if not isinstance(item, str):
                raise PowdrrExecutionError("Command items must be strings.")
            if item.strip():
                items.append(item.strip())
        return items
    return []


def _extract_command_option(
    command_items: Sequence[str], option_name: str
) -> str | None:
    for index, item in enumerate(command_items):
        if item == option_name and index + 1 < len(command_items):
            return command_items[index + 1]
    return None
