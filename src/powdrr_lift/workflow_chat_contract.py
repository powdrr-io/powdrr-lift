"""Action parsing, contracts, and repair guidance for workflow chat execution."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping, Sequence
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
from powdrr_lift.process.catalog import SkillCatalogEntry
from powdrr_lift.workflow_action_catalog import (
    DEFAULT_ACTION_INSTRUCTIONS as _DEFAULT_ACTION_INSTRUCTIONS,
)
from powdrr_lift.workflow_action_catalog import (
    recovery_tool_invocations as _recovery_tool_invocations,
)
from powdrr_lift.workflow_action_catalog import (
    step_actions as _step_actions,
)
from powdrr_lift.workflow_action_operations import (
    WorkflowEditRangeError as _WorkflowEditRangeError,
)
from powdrr_lift.workflow_action_operations import (
    WorkflowYamlEditError as _WorkflowYamlEditError,
)
from powdrr_lift.workflow_action_validation import (
    _validation_gate_enabled,
    _WorkflowStructuredDocumentError,
)
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
    WorkflowFileEdits as SkillChatFileEdits,
)
from powdrr_lift.workflow_llm import (
    WorkflowYamlOperation as SkillChatYamlOperation,
)
from powdrr_lift.workflow_llm import (
    workflow_action_signature as _shared_workflow_action_signature,
)
from powdrr_lift.workflow_paths import resolve_worktree_file_path
from powdrr_lift.workflow_prompting import (
    _successful_document_reads_for_prompt,
    _tool_invocation_to_data,
    interaction_style_prompt,
)
from powdrr_lift.workflow_step_behavior import behavior_for_step


def _workflow_action_signature(action: SkillChatAction) -> str:
    return _shared_workflow_action_signature(action)


def _required_action_string_item(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(
            "Workflow gather_context action "
            f"{field_name} must contain non-empty strings."
        )
    return value.strip()


def _required_action_string_sequence(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise PowdrrExecutionError(
            f"Workflow gather_context action {field_name} must be an array. "
            f"Return a JSON array in the {field_name!r} field, for example "
            f'"{field_name}": ["requirements"].'
        )

    normalized_values = tuple(
        _required_action_string_item(item, field_name=field_name) for item in value
    )
    if not normalized_values:
        raise PowdrrExecutionError(
            f"Workflow gather_context action {field_name} must not be empty."
        )
    return normalized_values


def _optional_action_string_sequence(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise PowdrrExecutionError(
            f"Workflow gather_context action {field_name} must be an array."
        )
    return tuple(
        _required_action_string_item(item, field_name=field_name) for item in value
    )


def _optional_action_filters(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PowdrrExecutionError(
            "Workflow gather_context action filters must be an object."
        )
    filters: dict[str, object] = {}
    for raw_field, raw_values in value.items():
        field = _required_action_string_item(raw_field, field_name="filters")
        if isinstance(raw_values, (str, bytes, bytearray)):
            values: object = (raw_values,)
        elif isinstance(raw_values, Sequence):
            values = tuple(
                _required_action_string_item(item, field_name=f"filters.{field}")
                for item in raw_values
            )
        else:
            raise PowdrrExecutionError(
                f"Workflow gather_context action filters.{field} must be an array."
            )
        if not values:
            raise PowdrrExecutionError(
                f"Workflow gather_context action filters.{field} must not be empty."
            )
        filters[field] = values
    return filters


def _required_edit_operations(value: object) -> tuple[SkillChatEdit, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise PowdrrExecutionError(
            "Workflow edit action edits must be an array.", action_kind="edit"
        )

    edits = tuple(_required_edit_operation(item) for item in value)
    if not edits:
        raise PowdrrExecutionError(
            "Workflow edit action edits must not be empty.", action_kind="edit"
        )
    return edits


def _required_file_edits(value: object) -> tuple[SkillChatFileEdits, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise PowdrrExecutionError(
            "Workflow edit action file_edits must be an array.", action_kind="edit"
        )

    file_edits: list[SkillChatFileEdits] = []
    for item in value:
        if not isinstance(item, dict):
            raise PowdrrExecutionError(
                "Workflow edit action file_edits must contain objects.",
                action_kind="edit",
            )
        file_path = item.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise PowdrrExecutionError(
                "Workflow edit action file_edits entries must include file_path.",
                action_kind="edit",
            )
        file_edits.append(
            SkillChatFileEdits(
                file_path=file_path.strip(),
                edits=_required_edit_operations(item.get("edits")),
            )
        )
    if not file_edits:
        raise PowdrrExecutionError(
            "Workflow edit action file_edits must not be empty.", action_kind="edit"
        )
    return tuple(file_edits)


def _required_edit_operation(value: object) -> SkillChatEdit:
    if not isinstance(value, dict):
        raise PowdrrExecutionError(
            "Workflow edit action edits must be objects.", action_kind="edit"
        )

    kind = value.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise PowdrrExecutionError(
            "Workflow edit action edit kind must be a string.", action_kind="edit"
        )
    normalized_kind = kind.strip()
    if normalized_kind not in {"add", "remove", "replace"}:
        raise PowdrrExecutionError(
            "Workflow edit action edit kind must be add, remove, or replace.",
            action_kind="edit",
        )

    start_line = _required_edit_line_number(
        value.get("start_line"),
        field_name="start_line",
    )
    end_line_value = value.get("end_line")
    end_line = None
    if end_line_value is not None:
        end_line = _required_edit_line_number(end_line_value, field_name="end_line")
        if end_line < start_line:
            raise PowdrrExecutionError(
                "Workflow edit action end_line must be >= start_line.",
                action_kind="edit",
            )

    text_value = value.get("text")
    if normalized_kind in {"add", "replace"}:
        if not isinstance(text_value, str):
            raise PowdrrExecutionError(
                "Workflow edit action add/replace edits must include text.",
                action_kind="edit",
            )
        text = text_value
    else:
        if text_value is not None:
            raise PowdrrExecutionError(
                "Workflow edit action remove edits must not include text.",
                action_kind="edit",
            )
        text = None
        if end_line is None:
            end_line = start_line

    return SkillChatEdit(
        kind=normalized_kind,
        start_line=start_line,
        end_line=end_line,
        text=text,
    )


def _required_edit_line_number(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PowdrrExecutionError(
            f"Workflow edit action {field_name} must be a positive integer.",
            action_kind="edit",
        )
    return value


def _required_document_line_number(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PowdrrExecutionError(
            f"Workflow read_document action {field_name} must be a "
            "non-negative integer."
        )
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PowdrrExecutionError(
            "Workflow action decisions_and_context must be a string."
        )
    normalized_value = value.strip()
    return normalized_value or None


def _optional_llm_type(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError("Workflow llm_type must be a non-empty string.")
    return value.strip().lower().replace("-", "_")


def _normalize_structured_document_text(path: Path, text: str) -> str:
    """Remove a single Markdown fence when an LLM wraps a structured file."""
    if path.suffix.lower() not in {".json", ".yaml", ".yml"}:
        return text
    match = re.fullmatch(
        r"\s*```(?:json|yaml|yml)?\s*\n(.*?)\n?```\s*",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) + "\n" if match is not None else text


def _edit_to_data(edit: SkillChatEdit) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": edit.kind,
        "start_line": edit.start_line,
    }
    if edit.end_line is not None:
        data["end_line"] = edit.end_line
    if edit.text is not None:
        data["text"] = edit.text
    return data


def _yaml_operation_to_data(operation: SkillChatYamlOperation) -> dict[str, Any]:
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


def _file_edits_to_data(file_edits: SkillChatFileEdits) -> dict[str, Any]:
    return {
        "file_path": file_edits.file_path,
        "edits": [_edit_to_data(edit) for edit in file_edits.edits],
    }


def _workflow_edit_failure_feedback(
    action: SkillChatAction,
    error: Exception,
    current_file_context: dict[str, Any] | None,
) -> str:
    error = _underlying_execution_error(error)
    feedback = (
        f"Workflow {action.kind} action failed: {error}. "
        "Re-read the current file context and return a corrected action."
    )
    if isinstance(error, PowdrrExecutionError):
        feedback += f" Error code: {error.error_code}."
        if error.remediation:
            feedback += f" Remediation: {error.remediation}"
        if error.details:
            feedback += (
                " Diagnostic details: "
                + json.dumps(error.details, sort_keys=True)
                + "."
            )
        if (
            error.error_code == "capability_not_executable"
            and error.details.get("capability") == "basedpyright-structure"
        ):
            feedback += (
                " This is a capability argument error, not a reason to reread the "
                "same document. Choose a materially different exact .py path from "
                "candidate_python_files, or use an available discovery action."
            )
    if isinstance(error, _WorkflowEditRangeError):
        if current_file_context and current_file_context.get("exists"):
            feedback += (
                " The current file has "
                f"{current_file_context['line_count']} lines; every edit range "
                "must stay within that line count."
            )
    elif isinstance(error, _WorkflowStructuredDocumentError):
        feedback += (
            " The edit range was within the file, but the resulting structured "
            "document is invalid. Preserve surrounding mapping keys and YAML "
            "indentation, such as section headers like `entities:`. If a prose "
            "value contains embedded double quotes, colons, or YAML punctuation, "
            "use a single-quoted scalar or a `>-` block scalar; do not use "
            "unescaped double quotes inside a double-quoted value. Correct the "
            "document before retrying."
        )
    elif isinstance(error, _WorkflowYamlEditError):
        feedback += (
            " Prefer yaml_edit for .yaml or .yml files. Its operations are "
            "structural: upsert_item replaces or appends a list item by section "
            "and id, remove_item deletes one by section and id (or by an exact "
            "validator-reported list index for boilerplate), and set_value "
            "updates a mapping value by path; remove_key deletes an exact mapping "
            "key path. Include multiple independent "
            "operations in one yaml_edit action when correcting the same file. "
            "If the structural operations cannot express the repair, use a normal "
            "edit with exact line ranges, preserve YAML indentation and section "
            "headers, and rerun the validator."
        )
    if action.kind == "yaml_edit" and "does not exist" in str(error):
        feedback += (
            " This target is absent and no change was applied. yaml_edit cannot "
            "create a document: do not retry this file_path or invent a name such "
            "as requirements.yaml. First use the declared read/list or generator "
            "action to identify or create the exact repository-relative YAML path, "
            "then apply yaml_edit to that existing path."
        )
    return feedback


def _underlying_execution_error(error: Exception) -> Exception:
    """Recover the concrete Powdrr action error for specialized feedback."""
    if isinstance(error, PowdrrExecutionError) and error.cause_error is not None:
        return error.cause_error
    return error


def _rejected_edit_guidance(action: SkillChatAction) -> str:
    if action.kind not in {"edit", "yaml_edit"}:
        return ""
    return (
        "\n\nLast proposed edit (NOT APPLIED):\n"
        f"{_workflow_action_signature(action)}\n"
        "Do not repeat it unchanged. Re-read the current file and return a "
        "corrected action using the structural yaml_edit contract when editing "
        "YAML."
    )


def _resolve_generated_file_path_from_command(
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
            work_item_name,
            worktree_root,
        )
    if subcommand == "implementation-specification":
        return implementation_specification_default_output_path(
            work_item_name,
            worktree_root,
        )
    if subcommand == "pr-specification":
        return pr_specification_default_output_path(work_item_name, worktree_root)
    if subcommand == "feature-pr-specification":
        return feature_pr_specification_default_output_path(
            work_item_name,
            worktree_root,
        )
    if subcommand == "system-map-specification":
        return system_map_specification_default_output_path(
            work_item_name,
            worktree_root,
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
        command,
        (str, bytes, bytearray),
    ):
        items: list[str] = []
        for item in command:
            if not isinstance(item, str):
                raise PowdrrExecutionError(
                    "Workflow invoke_tool action command items must be strings."
                )
            normalized_item = item.strip()
            if normalized_item:
                items.append(normalized_item)
        return items
    return []


def _extract_command_option(
    command_items: Sequence[str],
    option_name: str,
) -> str | None:
    for index, item in enumerate(command_items):
        if item != option_name:
            continue
        if index + 1 >= len(command_items):
            return None
        return command_items[index + 1]
    return None


def _current_step_contract(
    step: Any | None,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
) -> dict[str, Any]:
    """Describe the current step's authoritative action and tool contract."""
    if step is None:
        return {}
    invocations = tuple(getattr(step, "tool_invocations", ()) or ())
    recovery_invocations = _recovery_tool_invocations(
        step, execution_events, step_index
    )
    nested_skill = getattr(step, "uses_skill", None)
    actions = _step_actions(
        step, execution_events=execution_events, step_index=step_index
    )
    return {
        "step_type": getattr(step, "step_type", "governed"),
        "coding_loop": (
            step.coding_loop.to_data()
            if getattr(step, "coding_loop", None) is not None
            else None
        ),
        "completion": (
            step.completion.to_data()
            if getattr(step, "completion", None) is not None
            else None
        ),
        "outputs": [
            output.to_data() for output in (getattr(step, "outputs", ()) or ())
        ],
        "actions": [name for name, _instructions in actions],
        "declared_tool_invocations": [
            _tool_invocation_to_data(invocation) for invocation in invocations
        ],
        "tool_invocation_packages": list(
            getattr(step, "tool_invocation_packages", ()) or ()
        ),
        "recovery_tool_invocations": [
            _tool_invocation_to_data(invocation) for invocation in recovery_invocations
        ],
        "declared_nested_skill": (
            nested_skill.to_data() if nested_skill is not None else None
        ),
        "validation_gate": getattr(step, "validation_gate", None),
        "requires_successful_declared_tool_before_next_step": any(
            invocation.tool == "shell" for invocation in invocations
        ),
    }


def _step_action_response_schema(
    step: Any,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
) -> dict[str, Any]:
    """Derive a strict provider envelope from the active step contract."""
    behavior = behavior_for_step(step)
    outputs = tuple(getattr(step, "outputs", ()) or ())
    output_properties = {
        output.name: (
            dict(output.schema)
            if output.schema is not None
            else _json_schema_for_declared_type(output.type)
        )
        for output in outputs
    }
    action_names = [
        name
        for name, _instructions in _step_actions(
            step, execution_events=execution_events, step_index=step_index
        )
    ]
    if not getattr(step, "actions_declared", False):
        for legacy_action in (
            "gather_context",
            "prompt_user",
            "edit",
            "yaml_edit",
            "file_management",
            "delete_file",
            "invoke_skill",
            "invoke_tool",
            "read_document",
            "list_files",
            "goto_step",
            "next_step",
            "complete",
        ):
            if legacy_action == "next_step" and behavior.is_predicated:
                continue
            if legacy_action not in action_names:
                action_names.append(legacy_action)
    properties: dict[str, Any] = {
        "action": {
            "type": "string",
            "enum": action_names,
        },
        "decisions_and_context": {"type": "string"},
        "llm_type": {"type": "string"},
    }
    action_properties: dict[str, dict[str, Any]] = {
        "gather_context": {
            "types": {"type": "array", "items": {"type": "string"}},
            "feature_id": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "filters": {"type": "object"},
        },
        "prompt_user": {"text": {"type": "string"}},
        "edit": {
            "file_path": {"type": "string"},
            "edits": {"type": "array", "items": {"type": "object"}},
            "file_edits": {"type": "array", "items": {"type": "object"}},
        },
        "yaml_edit": {
            "file_path": {"type": "string"},
            "operations": {"type": "array", "items": {"type": "object"}},
        },
        "file_management": {
            "operation": {"type": "string"},
            "file_path": {"type": "string"},
            "destination_path": {"type": "string"},
        },
        "delete_file": {"file_path": {"type": "string"}},
        "invoke_skill": {
            "skill": {"type": "string"},
            "provider_role": {
                "type": "string",
                "enum": ["normal", "adversarial"],
            },
            "clean": {"type": "boolean"},
            "context": {"type": "array", "items": {"type": "string"}},
        },
        "invoke_tool": {
            "tool": {"type": "string"},
            "parameters": {"type": "object"},
        },
        "read_document": {
            "file_path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        },
        "list_files": {
            "directory": {"type": "string"},
            "pattern": {"type": "string"},
            "recursive": {"type": "boolean"},
        },
        "goto_step": {"step_id": {"type": "string"}},
        "next_step": {"output_state": {}},
        "complete": {"text": {"type": "string"}},
        "emit_outputs": {},
    }
    for action_name in action_names:
        properties.update(action_properties.get(action_name, {}))
    if outputs:
        properties["outputs"] = {
            "type": "object",
            "properties": output_properties,
            "required": [
                output.name
                for output in outputs
                if output.required_for_next_step
                or (
                    behavior.is_predicated
                    and getattr(step, "completion", None) is not None
                    and output.name in step.completion.required_outputs
                )
            ],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["action"],
        "additionalProperties": False,
    }


def _json_schema_for_declared_type(type_name: str) -> dict[str, Any]:
    normalized = type_name.strip().casefold()
    if normalized in {"string", "array", "object", "integer", "number", "boolean"}:
        return {"type": normalized}
    return {}


def _is_infrastructure_tool_failure(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.casefold()
    return any(
        marker in lowered
        for marker in (
            "operation not permitted",
            "permission denied",
            "index.lock",
            "read-only file system",
            "no such file or directory",
            "timed out",
            "connection refused",
        )
    )


def _action_repair_prompt(
    selected_skill: SkillCatalogEntry,
    *,
    current_step: Any | None = None,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
    failed_action: SkillChatAction | None = None,
    validation_error: str | None = None,
) -> str:
    predicated = (
        current_step is not None and behavior_for_step(current_step).is_predicated
    )
    empty_response_guidance = (
        "If the original action response was empty, return a valid action that "
        "can produce the missing completion output."
        if predicated
        else (
            "If the original action response was empty, choose next_step when the "
            "current step is complete instead of returning an empty response. If "
            "this corrective response is also empty, the system will interpret it "
            "as next_step."
        )
    )
    prompt = (
        "Generate a JSON document selecting the best action based on this "
        "context. The current step's action declarations below are the only "
        "actions available. Do not use action names or instructions from a "
        "previous step.\n" + empty_response_guidance + "\n"
        'Return exactly one JSON object with a top-level "action" field and the '
        "fields required by that action. Do not combine actions or output "
        "markdown."
    )
    action_names = (
        {
            name
            for name, _ in _step_actions(
                current_step,
                execution_events=execution_events,
                step_index=step_index,
            )
        }
        if current_step is not None
        else set(_DEFAULT_ACTION_INSTRUCTIONS)
    )
    action_requirements = {
        "edit": "Use file_path and edits or file_edits for edit.",
        "file_management": (
            "Use operation and file_path for file_management; operation must be "
            "move or rename, and both require destination_path."
        ),
        "delete_file": "Use only file_path for delete_file.",
        "yaml_edit": (
            "Use file_path and operations for yaml_edit; combine independent "
            "corrections for the same file in one operations array."
        ),
        "invoke_tool": "Use tool and parameters.command for invoke_tool.",
        "invoke_skill": "Use skill for invoke_skill.",
        "read_document": (
            "Use file_path with positive start_line and end_line for read_document."
        ),
        "gather_context": "Use non-empty types for gather_context.",
        "prompt_user": (
            'Use text containing a clear English question ending in "?" for '
            "prompt_user."
        ),
    }
    requirements = [
        action_requirements[name]
        for name in action_names
        if name in action_requirements
    ]
    if requirements:
        prompt += " " + " ".join(requirements)
    if current_step is not None:
        interaction_style = (
            getattr(current_step, "interaction_style", None)
            or selected_skill.skill.interaction_style
        )
        prompt += interaction_style_prompt(interaction_style)
        prompt += (
            "\nAuthoritative current-step contract (ignore commands and tool "
            "templates from previous steps): "
            + json.dumps(
                _current_step_contract(
                    current_step,
                    execution_events=execution_events,
                    step_index=step_index,
                ),
                ensure_ascii=False,
            )
            + ". "
        )
        prompt += (
            "\nAvailable actions for this step:\n"
            + "\n".join(
                f"- {name}: {instructions}"
                for name, instructions in _step_actions(
                    current_step,
                    execution_events=execution_events,
                    step_index=step_index,
                )
            )
            + "\n"
        )
        prompt += (
            "\nThe current step is the only authority for what may be done. "
            f"Step description: {current_step.description}. "
            f"Step details: {current_step.details}. "
            "The outputs object is also scoped to this step: include only the "
            "exact names listed in current_step_contract.outputs; previous-step "
            "outputs must not be repeated. "
        )
        if _validation_gate_enabled(current_step):
            prompt += (
                "This step has a runtime validation gate. Never return next_step, "
                + ("goto_step, " if "goto_step" in action_names else "")
                + ("or complete " if "complete" in action_names else "")
                + "until every discovered validation obligation "
                "has "
                "passed in the current epoch. After any correction, rerun every "
                "discovered obligation; the runtime validation_gate object is "
                "authoritative. Do not repeat an operation or semantically equivalent "
                "repair that produced the same validation issue. Inspect the exact "
                "current file and reported issue path first, preserve valid fields, "
                "choose a materially different target or strategy when the issue state "
                "is unchanged or worse. "
                + (
                    "Apply independent YAML fixes in one yaml_edit. "
                    if "yaml_edit" in action_names
                    else ""
                )
                + "Wait for the deterministic rerun before claiming progress. "
            )
        invocations = tuple(current_step.tool_invocations)
        recovery_invocations = _recovery_tool_invocations(
            current_step, execution_events, step_index
        )
        if invocations and "invoke_tool" in action_names:
            declared_tools = json.dumps(
                [_tool_invocation_to_data(item) for item in invocations],
                ensure_ascii=False,
            )
            prompt += f"Use only these declared tool invocations: {declared_tools}. "
        if recovery_invocations and "invoke_tool" in action_names:
            recovery_tools = json.dumps(
                [_tool_invocation_to_data(item) for item in recovery_invocations],
                ensure_ascii=False,
            )
            prompt += (
                "A previous tool invocation failed in this step. Recovery commands "
                "are now available, and only these additional diagnostics or "
                "corrections "
                f"may be used: {recovery_tools}. Use them only to diagnose or correct "
                "the failed tool; do not invent another command. "
            )
        if failed_action is not None and failed_action.tool == "basedpyright-structure":
            prompt += (
                "BasedPyright structure repair rule: parameters.path must be one "
                "exact existing repository-relative Python file path ending in "
                ".py. A directory, missing path, or non-Python path is invalid. "
                "Do not reread the same specification document to repair this "
                "error; select a concrete implementation file from the diagnostic "
                "candidate_python_files list or use an available discovery action. "
            )
        shapes: list[str] = []
        if "prompt_user" in action_names:
            shapes.append(
                '{"action":"prompt_user","text":"One clear English question?",'
                '"decisions_and_context":"More information is required."}'
            )
        if "next_step" in action_names:
            shapes.append(
                '{"action":"next_step","decisions_and_context":"The current '
                'step is complete."}'
            )
        if shapes:
            prompt += (
                "For this repair, use one of these exact action shapes allowed for "
                "the current step: " + " or ".join(shapes) + ". "
            )
        if "yaml_edit" in action_names:
            prompt += (
                'For a YAML correction, use exactly {"action":"yaml_edit",'
                '"file_path":"relative/file.yaml","operations":[{"op":"set_value",'
                '"path":["id"],"value":"feature-id"}]}; include all required '
                'fields in the operation. For list items, use {"op":"upsert_item",'
                '"section":"requirements","id":"req-1","value":{"description":'
                '"...","state":"added"}}. '
            )
        if invocations and "invoke_tool" in action_names:
            prompt += (
                "invoke_tool is also allowed only with one of the declared tool "
                "templates shown above; do not invent another tool or parameter "
                "shape. "
            )
    if validation_error is not None:
        prompt += (
            "\nThe previous action returned a validation_error and was not "
            "executed. Do not repeat it unchanged. Read the validation_error "
            "message and return an action that matches the current step's "
            "declared tool template exactly."
        )
        if "prompt_user action" in validation_error.lower() and "text" in (
            validation_error.lower()
        ):
            prompt += (
                " The previous response used the wrong prompt_user field. "
                'prompt_user requires a string in "text"; rename "prompt" to '
                '"text" and return the complete corrected action. Do not use '
                '"text".'
            )
    if failed_action is not None:
        prompt += (
            f"\nRecovery is mandatory: the previous {failed_action.kind} action "
            "failed and was not applied. Do not repeat that action or an equivalent "
            "action with only different prose; choose a materially different action "
            "from the current step's allowed actions. The failure reason was: "
            + (validation_error or "the action violated the active workflow contract")
            + ". The rejected action was:\n"
            + _workflow_action_signature(failed_action)
            + ". "
            + (
                "For YAML, prefer yaml_edit with upsert_item, remove_item, "
                "remove_key, or set_value; use a normal edit with exact line "
                "ranges when those operations cannot express the repair. "
                if {"edit", "yaml_edit"} & action_names
                else ""
            )
        )
        successful_reads = _successful_document_reads_for_prompt(
            execution_events, step_index
        )
        if successful_reads:
            prompt += (
                " Documents already read successfully in this step (do not reread "
                "them as a substitute for correcting the failed action): "
                + ", ".join(str(item["path"]) for item in successful_reads)
                + "."
            )
    if _is_infrastructure_tool_failure(validation_error):
        prompt += (
            " The failure appears to be an environment or permission problem. "
            "Do not switch to unrelated diagnostic commands. Retry the exact "
            "required action at most once; if it fails again, return prompt_user "
            "to request human intervention."
        )
    return prompt
