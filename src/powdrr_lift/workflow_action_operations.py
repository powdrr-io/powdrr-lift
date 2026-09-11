from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.execution.builtin_tools import invoke_intrinsic_capability
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.intrinsic_git_gh import GIT_TOOL
from powdrr_lift.pr_workflow_record import (
    is_pull_request_create_command,
    pull_request_number,
    record_pull_request_workflow,
)
from powdrr_lift.process.catalog import SkillCatalogEntry
from powdrr_lift.workflow_action_validation import _command_items_for_validation
from powdrr_lift.workflow_llm import (
    PowdrrExecutionError,
)
from powdrr_lift.workflow_llm import (
    WorkflowAction as SkillChatAction,
)
from powdrr_lift.workflow_llm import (
    WorkflowEdit as SkillChatEdit,
)
from powdrr_lift.workflow_llm import (
    WorkflowYamlOperation as SkillChatYamlOperation,
)
from powdrr_lift.workflow_paths import resolve_worktree_file_path

_PRE_STEP_PLACEHOLDER = re.compile(r"<([^<>]+)>")


class WorkflowEditRangeError(PowdrrExecutionError):
    pass


class WorkflowYamlEditError(PowdrrExecutionError):
    pass


_WorkflowEditRangeError = WorkflowEditRangeError
_WorkflowYamlEditError = WorkflowYamlEditError


def _resolve_pre_step_template(
    value: Any,
    context_values: Mapping[str, Any],
) -> Any:
    if isinstance(value, str):
        exact = _PRE_STEP_PLACEHOLDER.fullmatch(value.strip())
        if exact is not None:
            key = re.sub(r"[-\s]+", "_", exact.group(1).strip().lower())
            if key not in context_values:
                raise PowdrrExecutionError(
                    f"Deterministic pre-step placeholder <{exact.group(1)}> "
                    "is not present in the input context."
                )
            return context_values[key]

        def replace_match(match: re.Match[str]) -> str:
            key = re.sub(r"[-\s]+", "_", match.group(1).strip().lower())
            if key not in context_values:
                raise PowdrrExecutionError(
                    f"Deterministic pre-step placeholder <{match.group(1)}> "
                    "is not present in the input context."
                )
            replacement = context_values[key]
            if isinstance(replacement, (Mapping, Sequence)) and not isinstance(
                replacement, (str, bytes, bytearray)
            ):
                raise PowdrrExecutionError(
                    f"Deterministic pre-step placeholder <{match.group(1)}> "
                    "must be the complete template value when its context value "
                    "is structured."
                )
            return str(replacement)

        return _PRE_STEP_PLACEHOLDER.sub(replace_match, value)
    if isinstance(value, Mapping):
        return {
            key: _resolve_pre_step_template(item, context_values)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_resolve_pre_step_template(item, context_values) for item in value]
    return value


def _list_worktree_files(
    directory_value: str,
    pattern: str | None,
    recursive: bool,
    worktree_root: Path,
) -> dict[str, Any]:
    directory = resolve_worktree_file_path(directory_value, worktree_root)
    if not directory.exists() or not directory.is_dir():
        raise PowdrrExecutionError(
            f"Workflow list_files directory does not exist: {directory_value}."
        )
    normalized_pattern = pattern or "*"
    paths = (
        directory.rglob(normalized_pattern)
        if recursive
        else directory.glob(normalized_pattern)
    )
    return {
        "directory": directory_value,
        "pattern": normalized_pattern,
        "recursive": recursive,
        "files": sorted(
            str(path.relative_to(worktree_root)) for path in paths if path.is_file()
        ),
    }


def _record_skill_pull_request(
    action: SkillChatAction,
    repo_root: Path,
    skill: SkillCatalogEntry,
    events: Sequence[Mapping[str, Any]],
    tool_result: Mapping[str, Any],
    runtime: ExecutionRuntime,
    root_skill: SkillCatalogEntry | None = None,
    step_index: int | None = None,
) -> None:
    command_value = action.parameters.get("command", tool_result.get("command"))
    command = _command_items_for_validation(command_value)
    if not is_pull_request_create_command(command):
        return
    if tool_result.get("returncode") != 0:
        return
    output = tool_result.get("stdout")
    if not isinstance(output, str):
        return
    number = pull_request_number(output)
    if number is None:
        raise PowdrrExecutionError(
            "GitHub did not return a pull-request URL, so the workflow record "
            "could not be named under docs/prs/<pr-number>.yaml."
        )
    event = {
        "kind": action.kind,
        "tool": action.tool,
        "parameters": action.parameters,
        "result": tool_result,
        "decisions_and_context": action.decisions_and_context,
        "step_index": step_index,
    }
    record_skill = root_skill or skill
    with runtime.without_action_contract():
        branch_result = invoke_intrinsic_capability(
            GIT_TOOL,
            {"operation": "branch_current"},
            worktree_root=repo_root,
            runtime=runtime,
        )
    branch = str(branch_result.get("stdout", "")).strip()
    if not branch:
        raise PowdrrExecutionError(
            "Could not determine the branch for the workflow record."
        )
    record_pull_request_workflow(
        repo_root,
        number,
        branch=branch,
        base_branch="main",
        title="Agent-created pull request",
        workflow_name=record_skill.skill.name,
        workflow_path=str(record_skill.path),
        steps=[step.to_data() for step in record_skill.skill.steps],
        events=[*events, event],
        explanation=(
            "This record documents the selected skill steps and the tool calls "
            "observed before the agent created the pull request."
        ),
    )


def _apply_file_edits(current_text: str, edits: Sequence[SkillChatEdit]) -> str:
    lines = current_text.splitlines()
    for edit in sorted(edits, key=_edit_sort_key, reverse=True):
        start_index = edit.start_line - 1
        if edit.kind == "add":
            if start_index > len(lines):
                raise _WorkflowEditRangeError(
                    "Workflow edit action add start_line "
                    f"{edit.start_line} is beyond the end of the file, which has "
                    f"{len(lines)} lines."
                )
            insert_lines = edit.text.splitlines() if edit.text is not None else []
            lines[start_index:start_index] = insert_lines
            continue

        end_line = edit.end_line if edit.end_line is not None else edit.start_line
        end_index = end_line
        if end_index > len(lines):
            raise _WorkflowEditRangeError(
                "Workflow edit action range ends at line "
                f"{end_line}, but the file has {len(lines)} lines."
            )

        if edit.kind == "remove":
            del lines[start_index:end_index]
            continue

        replacement_lines = edit.text.splitlines() if edit.text is not None else []
        lines[start_index:end_index] = replacement_lines

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _apply_yaml_operations(
    path: Path,
    current_text: str,
    operations: Sequence[SkillChatYamlOperation],
) -> str:
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise _WorkflowYamlEditError(
            "yaml_edit only supports .yaml and .yml files. Use edit for other "
            "file types."
        )
    try:
        document = yaml.safe_load(current_text)
    except yaml.YAMLError as exc:
        raise _WorkflowYamlEditError(
            f"Cannot apply yaml_edit because {path} is already invalid YAML: "
            f"{exc}. Repair the YAML with edit first, then retry yaml_edit."
        ) from exc
    if document is None:
        document = {}
    if not isinstance(document, Mapping):
        raise _WorkflowYamlEditError(
            "yaml_edit requires the document root to be a YAML mapping."
        )
    updated: dict[str, Any] = dict(document)

    for operation in operations:
        if operation.operation == "set_value":
            if not operation.path:
                raise _WorkflowYamlEditError(
                    "set_value requires a non-empty path, for example "
                    '["title"] or ["features", "0", "description"].'
                )
            target: Any = updated
            for key in operation.path[:-1]:
                if isinstance(target, dict) and key in target:
                    target = target[key]
                    continue
                if isinstance(target, list) and key.isdigit():
                    index = int(key)
                    if 0 <= index < len(target):
                        target = target[index]
                        continue
                raise _WorkflowYamlEditError(
                    f"set_value path {list(operation.path)!r} cannot find "
                    f"mapping key or list index {key!r}. Re-read the YAML and "
                    "use the exact path from the current document."
                )
            final_key = operation.path[-1]
            if isinstance(target, dict):
                target[final_key] = operation.value
            elif isinstance(target, list) and final_key.isdigit():
                index = int(final_key)
                if not 0 <= index < len(target):
                    raise _WorkflowYamlEditError(
                        f"set_value list index {final_key!r} is outside the "
                        f"current list of length {len(target)}."
                    )
                target[index] = operation.value
            else:
                raise _WorkflowYamlEditError(
                    f"set_value path {list(operation.path)!r} does not resolve "
                    "to a YAML mapping or list index."
                )
            continue

        if operation.operation == "remove_key":
            if not operation.path:
                raise _WorkflowYamlEditError(
                    "remove_key requires a non-empty mapping-key path."
                )
            remove_target: Any = updated
            for key in operation.path[:-1]:
                if isinstance(remove_target, dict) and key in remove_target:
                    remove_target = remove_target[key]
                    continue
                if isinstance(remove_target, list) and key.isdigit():
                    index = int(key)
                    if 0 <= index < len(remove_target):
                        remove_target = remove_target[index]
                        continue
                raise _WorkflowYamlEditError(
                    f"remove_key path {list(operation.path)!r} cannot find "
                    f"mapping key {key!r}. Re-read the YAML and use the exact "
                    "path from the current document."
                )
            final_key = operation.path[-1]
            if not isinstance(remove_target, dict):
                raise _WorkflowYamlEditError(
                    f"remove_key path {list(operation.path)!r} does not resolve "
                    "to a YAML mapping."
                )
            if final_key not in remove_target:
                raise _WorkflowYamlEditError(
                    f"No mapping key {final_key!r} exists at path "
                    f"{list(operation.path)!r}. Re-read the YAML before retrying."
                )
            del remove_target[final_key]
            continue

        if operation.section is None or (
            operation.item_id is None and operation.item_index is None
        ):
            raise _WorkflowYamlEditError(
                f"{operation.operation} requires section plus id or index. "
                "Use an operation "
                'such as {"op": "upsert_item", "section": "features", '
                '"id": "feature-id", "value": {...}} or {"op": '
                '"remove_item", "section": "requirements", "index": 0}.'
            )
        raw_items = updated.get(operation.section)
        if raw_items is None and operation.operation == "upsert_item":
            raw_items = []
            updated[operation.section] = raw_items
        if not isinstance(raw_items, list):
            raise _WorkflowYamlEditError(
                f"YAML section {operation.section!r} must be a list for "
                f"{operation.operation}. Re-read the document and use the exact "
                "section name."
            )

        matching_indexes = [
            index
            for index, item in enumerate(raw_items)
            if isinstance(item, Mapping) and item.get("id") == operation.item_id
        ]
        if operation.operation == "remove_item" and operation.item_index is not None:
            if not 0 <= operation.item_index < len(raw_items):
                raise _WorkflowYamlEditError(
                    f"No item at index {operation.item_index} exists in section "
                    f"{operation.section!r}. Re-read the YAML before retrying."
                )
            del raw_items[operation.item_index]
            continue
        if len(matching_indexes) > 1:
            raise _WorkflowYamlEditError(
                f"YAML section {operation.section!r} contains duplicate id "
                f"{operation.item_id!r}; repair the duplicates before yaml_edit."
            )
        if operation.operation == "remove_item":
            if not matching_indexes:
                raise _WorkflowYamlEditError(
                    f"No item with id {operation.item_id!r} exists in section "
                    f"{operation.section!r}. Use read_document to inspect ids "
                    "before retrying."
                )
            del raw_items[matching_indexes[0]]
            continue

        if operation.operation == "upsert_item":
            if not isinstance(operation.value, Mapping):
                raise _WorkflowYamlEditError(
                    "upsert_item requires value to be a mapping containing the "
                    "complete item fields."
                )
            item = dict(operation.value)
            item["id"] = operation.item_id
            if matching_indexes:
                raw_items[matching_indexes[0]] = item
            else:
                raw_items[:] = [
                    existing
                    for existing in raw_items
                    if not (
                        isinstance(existing, Mapping) and existing.get("id") is None
                    )
                ]
                raw_items.append(item)
            continue

        raise _WorkflowYamlEditError(
            f"Unsupported yaml_edit operation {operation.operation!r}. Use "
            "upsert_item, remove_item, remove_key, or set_value."
        )

    return yaml.safe_dump(
        updated,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


def _edit_sort_key(edit: SkillChatEdit) -> tuple[int, int]:
    end_line = edit.end_line if edit.end_line is not None else edit.start_line
    return edit.start_line, end_line
