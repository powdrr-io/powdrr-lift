"""Parsing and normalization for workflow actions proposed by LLMs."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from powdrr_lift.agent.actions import (
    WorkflowAction as SkillChatAction,
)
from powdrr_lift.agent.actions import (
    WorkflowEdit as SkillChatEdit,
)
from powdrr_lift.agent.actions import (
    WorkflowFileEdits as SkillChatFileEdits,
)
from powdrr_lift.agent.actions import (
    WorkflowYamlOperation as SkillChatYamlOperation,
)
from powdrr_lift.basedpyright_tools import is_basedpyright_tool
from powdrr_lift.core.spec_context import normalize_context_type
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.intrinsic_edit import APPLY_EDIT_TOOL, VALIDATE_EDIT_TOOL
from powdrr_lift.intrinsic_enrich import ENRICH_TOOL
from powdrr_lift.intrinsic_git_gh import GH_TOOL, GIT_TOOL, intrinsic_command

WorkflowActionParser = Callable[
    [dict[str, Any], str | None, str | None], SkillChatAction
]

_REQUIRED_SHELL_COMMAND_ERROR = (
    "Workflow invoke_tool action command items must be non-empty strings."
)


def _required_shell_command_item(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(_REQUIRED_SHELL_COMMAND_ERROR)
    return value.strip()


def _parse_action_response(payload: dict[str, Any]) -> SkillChatAction:
    action = payload.get("action")
    if action is None:
        raise PowdrrExecutionError(
            "Workflow action response must include action. Return one JSON object "
            'with a top-level "action" field.'
        )
    if not isinstance(action, str):
        raise PowdrrExecutionError("Workflow action response action must be a string.")
    normalized_kind = action.strip()
    if not normalized_kind:
        raise PowdrrExecutionError("Workflow action response action must not be empty.")
    decisions_and_context = _optional_string(payload.get("decisions_and_context"))
    llm_type = _optional_llm_type(payload.get("llm_type"))
    parser = _workflow_action_parsers().get(normalized_kind)
    if parser is None:
        raise PowdrrExecutionError(f"Unknown workflow action: {normalized_kind!r}")
    raw_outputs = payload.get("outputs", {})
    if not isinstance(raw_outputs, Mapping) or any(
        not isinstance(name, str) or not name.strip() for name in raw_outputs
    ):
        raise PowdrrExecutionError(
            "Workflow action outputs must be an object with named values."
        )
    raw_outputs = dict(raw_outputs)
    nested_decisions = raw_outputs.pop("decisions_and_context", None)
    if decisions_and_context is None and nested_decisions is not None:
        decisions_and_context = _optional_string(nested_decisions)
    parser_payload = payload
    if normalized_kind in {"edit", "gather_context", "read_document"} and isinstance(
        payload.get("parameters"), Mapping
    ):
        parser_payload = dict(payload)
        for name, value in payload["parameters"].items():
            parser_payload.setdefault(name, value)
    if normalized_kind == "edit":
        raw_edits = parser_payload.get("edits")
        if isinstance(raw_edits, Mapping):
            if parser_payload is payload:
                parser_payload = dict(payload)
            parser_payload["edits"] = [dict(raw_edits)]
    if normalized_kind == "gather_context" and isinstance(
        parser_payload.get("types"), str
    ):
        if parser_payload is payload:
            parser_payload = dict(payload)
        parser_payload["types"] = [parser_payload["types"]]
    action = parser(parser_payload, decisions_and_context, llm_type)
    return replace(
        action,
        outputs=dict(raw_outputs),
    )


def _workflow_action_parsers() -> dict[str, WorkflowActionParser]:
    return {
        "complete": _parse_workflow_action_complete,
        "get-human-input": _parse_workflow_action_human_input,
        "edit": _parse_workflow_action_edit,
        "yaml_edit": _parse_workflow_action_yaml_edit,
        "file_management": _parse_workflow_action_file_management,
        "delete_file": _parse_workflow_action_delete_file,
        "gather_context": _parse_workflow_action_gather_context,
        "invoke_tool": _parse_workflow_action_invoke_tool,
        "invoke_skill": _parse_workflow_action_invoke_skill,
        "goto_step": _parse_workflow_action_goto_step,
        "read_document": _parse_workflow_action_read_document,
        "list_files": _parse_workflow_action_list_files,
        "next_step": _parse_workflow_action_next_step,
        "emit_outputs": _parse_workflow_action_emit_outputs,
        "prompt_user": _parse_workflow_action_prompt_user,
    }


def _parse_workflow_action_emit_outputs(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    return SkillChatAction(
        kind="emit_outputs",
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_invoke_skill(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    skill_name = payload.get("skill")
    if not isinstance(skill_name, str) or not skill_name.strip():
        raise PowdrrExecutionError("Workflow invoke_skill action must include skill.")
    provider_role = payload.get("provider_role")
    if provider_role is not None and provider_role not in {"normal", "adversarial"}:
        raise PowdrrExecutionError(
            'provider_role must be "normal" or "adversarial" when provided.'
        )
    clean = payload.get("clean", False)
    if not isinstance(clean, bool):
        raise PowdrrExecutionError("clean must be a boolean when provided.")
    raw_context = payload.get("context", [])
    if not isinstance(raw_context, list) or not all(
        isinstance(value, str) and value.strip() for value in raw_context
    ):
        raise PowdrrExecutionError("context must be a list of non-empty strings.")
    return SkillChatAction(
        kind="invoke_skill",
        skill_name=skill_name.strip(),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
        provider_role=provider_role,
        clean=clean,
        context=tuple(value.strip() for value in raw_context),
    )


def _parse_workflow_action_goto_step(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    step_id = payload.get("step_id")
    if not isinstance(step_id, str) or not step_id.strip():
        raise PowdrrExecutionError("Workflow goto_step action must include step_id.")
    return SkillChatAction(
        kind="goto_step",
        step_id=step_id.strip(),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_complete(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    text = payload.get("text")
    if text is not None and not isinstance(text, str):
        raise PowdrrExecutionError("Workflow complete action text must be a string.")
    return SkillChatAction(
        kind="complete",
        text=(text.strip() if text else None),
        output_state=payload.get("output_state"),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_human_input(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    value = payload.get("human_input")
    if not isinstance(value, Mapping):
        raise PowdrrExecutionError("get-human-input must include human_input.")
    human_task = _parse_human_input_task(value.get("human_task"), "human_task")
    instructions = value.get("incorporation_instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise PowdrrExecutionError(
            "get-human-input must include incorporation_instructions."
        )
    follow_up_value = value.get("follow_up_task")
    follow_up = (
        _parse_human_input_task(follow_up_value, "follow_up_task")
        if follow_up_value is not None
        else None
    )
    return SkillChatAction(
        kind="get-human-input",
        human_input={
            "human_task": human_task,
            "incorporation_instructions": instructions.strip(),
            "follow_up_task": follow_up,
        },
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_human_input_task(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PowdrrExecutionError(f"{field_name} must be an object.")
    description = value.get("description")
    role = value.get("role")
    output_state_type = value.get("output_state_type")
    if not isinstance(description, str) or not description.strip():
        raise PowdrrExecutionError(f"{field_name}.description must be non-empty.")
    if not isinstance(role, str) or not role.strip():
        raise PowdrrExecutionError(f"{field_name}.role must be provided.")
    if not isinstance(output_state_type, str) or not output_state_type.strip():
        raise PowdrrExecutionError(f"{field_name}.output_state_type must be non-empty.")
    if "input_state" not in value:
        raise PowdrrExecutionError(f"{field_name}.input_state must be provided.")
    return {
        "description": description.strip(),
        "role": role.strip(),
        "input_state": value["input_state"],
        "output_state_type": output_state_type.strip(),
    }


def _parse_workflow_action_edit(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_edits_value = payload.get("file_edits")
    if file_edits_value is not None:
        parsed_file_edits = _required_file_edits(file_edits_value)
        return SkillChatAction(
            kind="edit",
            file_edits=parsed_file_edits,
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PowdrrExecutionError(
            "Workflow edit action must include file_path.", action_kind="edit"
        )
    edits = _required_edit_operations(payload.get("edits"))
    return SkillChatAction(
        kind="edit",
        file_path=file_path.strip(),
        edits=edits,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_yaml_edit(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PowdrrExecutionError(
            "yaml_edit requires file_path. Use a repository-relative .yaml or "
            ".yml path."
        )
    if not file_path.strip().lower().endswith((".yaml", ".yml")):
        raise PowdrrExecutionError("yaml_edit file_path must end in .yaml or .yml.")
    raw_operations = payload.get("operations")
    if not isinstance(raw_operations, Sequence) or isinstance(
        raw_operations,
        (str, bytes, bytearray),
    ):
        raise PowdrrExecutionError(
            "yaml_edit requires a non-empty operations array. Supported "
            "operations are upsert_item, remove_item, remove_key, and set_value. "
            "Include "
            "all independent edits for this YAML file in one operations array."
        )
    operations = tuple(_parse_yaml_operation(item) for item in raw_operations)
    if not operations:
        raise PowdrrExecutionError(
            "yaml_edit operations must not be empty. Use upsert_item, "
            "remove_item, remove_key, or set_value. Try to include multiple "
            "independent "
            "edits in this one operations array when possible."
        )
    return SkillChatAction(
        kind="yaml_edit",
        file_path=file_path.strip(),
        yaml_operations=operations,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_yaml_operation(value: object) -> SkillChatYamlOperation:
    if not isinstance(value, Mapping):
        raise PowdrrExecutionError(
            'yaml_edit operations must be objects. Example: {"op": '
            '"upsert_item", "section": "features", "id": '
            '"feature-id", "value": {...}}.'
        )
    operation = value.get("op")
    if operation not in {"upsert_item", "remove_item", "remove_key", "set_value"}:
        raise PowdrrExecutionError(
            f"yaml_edit operation op must be upsert_item, remove_item, remove_key, or "
            f"set_value; received {operation!r}."
        )
    if operation in {"set_value", "remove_key"}:
        raw_path = value.get("path")
        if (
            not isinstance(raw_path, Sequence)
            or isinstance(
                raw_path,
                (str, bytes, bytearray),
            )
            or not raw_path
            or not all(
                (isinstance(item, str) and item.strip())
                or (isinstance(item, int) and not isinstance(item, bool) and item >= 0)
                for item in raw_path
            )
        ):
            raise PowdrrExecutionError(
                "yaml_edit set_value requires a non-empty path array of mapping "
                'keys or non-negative list indexes, for example ["title"].'
            )
        if operation == "remove_key":
            return SkillChatYamlOperation(
                operation=operation,
                path=tuple(str(item).strip() for item in raw_path),
            )
        if "value" not in value:
            raise PowdrrExecutionError("yaml_edit set_value requires value.")
        return SkillChatYamlOperation(
            operation=operation,
            path=tuple(str(item).strip() for item in raw_path),
            value=value["value"],
        )

    section = value.get("section")
    item_id = value.get("id")
    item_index = value.get("index")
    if not isinstance(section, str) or not section.strip():
        raise PowdrrExecutionError(
            f"yaml_edit {operation} requires a non-empty section, such as "
            "features or decisions."
        )
    if (
        operation == "remove_item"
        and isinstance(item_index, int)
        and not isinstance(item_index, bool)
    ):
        if item_index < 0:
            raise PowdrrExecutionError(
                "yaml_edit remove_item index must be non-negative. Corrective "
                "action: use the zero-based list index reported by the validator, "
                'for example {"op":"remove_item","section":"requirements",'
                '"index":0}; do not use a negative index.'
            )
        if item_id is not None:
            raise PowdrrExecutionError(
                "yaml_edit remove_item must use exactly one of id or index. "
                "Corrective action: remove id when using the validator-reported "
                'index, for example {"op":"remove_item",'
                '"section":"requirements","index":0}; do not include both.'
            )
        return SkillChatYamlOperation(
            operation=operation,
            section=section.strip(),
            item_index=item_index,
        )
    if not isinstance(item_id, str) or not item_id.strip():
        raise PowdrrExecutionError(
            f"yaml_edit {operation} requires a non-empty id. Use read_document "
            "to discover the existing item id."
        )
    if operation == "upsert_item" and not isinstance(value.get("value"), Mapping):
        raise PowdrrExecutionError(
            "yaml_edit upsert_item requires value to be a mapping containing the "
            "complete item fields."
        )
    return SkillChatYamlOperation(
        operation=operation,
        section=section.strip(),
        item_id=item_id.strip(),
        value=value.get("value"),
    )


def _parse_workflow_action_gather_context(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    types = _required_action_string_sequence(
        payload.get("types"),
        field_name="types",
    )
    keywords = _optional_action_string_sequence(
        payload.get("keywords"),
        field_name="keywords",
    )
    filters = _optional_action_filters(payload.get("filters"))
    feature_id = payload.get("feature_id")
    if feature_id is not None and (
        not isinstance(feature_id, str) or not feature_id.strip()
    ):
        raise PowdrrExecutionError(
            "Workflow gather_context action feature_id must be a non-empty string."
        )
    try:
        normalized_types = tuple(
            normalize_context_type(context_type) for context_type in types
        )
    except ValueError as exc:
        raise PowdrrExecutionError(str(exc)) from exc
    return SkillChatAction(
        kind="gather_context",
        types=normalized_types,
        keywords=keywords,
        filters=filters,
        feature_id=feature_id.strip() if isinstance(feature_id, str) else None,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_invoke_tool(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    tool = payload.get("tool", "shell")
    if not isinstance(tool, str) or not tool.strip():
        raise PowdrrExecutionError("Workflow invoke_tool action must include tool.")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise PowdrrExecutionError(
            "Workflow invoke_tool action must include parameters."
        )
    normalized_tool = tool.strip()
    if normalized_tool in {GIT_TOOL, GH_TOOL}:
        if normalized_tool == GH_TOOL and parameters.get("operation") in {
            "pr_create",
            "pr_edit",
        }:
            forbidden = {"pr_reference", "head", "base"}.intersection(parameters)
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise PowdrrExecutionError(
                    f"gh {parameters['operation']} does not accept model-owned "
                    f"identity fields: {names}. Provide only title and body."
                )
            if parameters.get("operation") == "pr_edit":
                validation_parameters = {
                    **parameters,
                    "pr_reference": "__current_branch__",
                }
            else:
                validation_parameters = parameters
        else:
            validation_parameters = parameters
        intrinsic_command(validation_parameters, tool=normalized_tool)
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=dict(parameters),
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    if normalized_tool in {
        ENRICH_TOOL,
        VALIDATE_EDIT_TOOL,
        APPLY_EDIT_TOOL,
    }:
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=dict(parameters),
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    if is_basedpyright_tool(normalized_tool):
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=dict(parameters),
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    command = parameters.get("command")
    first_class_action = _first_class_action_from_command(command)
    if normalized_tool in {"internal", "shell"} and first_class_action is not None:
        raise PowdrrExecutionError(
            f"Use the first-class action `{first_class_action}`, not "
            f"`invoke_tool` with a {normalized_tool} command. Return this "
            "kind of action directly: "
            f"{_first_class_action_example(first_class_action)}"
        )
    if isinstance(command, str):
        normalized_parameters = dict(parameters)
        normalized_command = command.strip()
        if not normalized_command:
            raise PowdrrExecutionError(
                "Workflow invoke_tool action command must be non-empty."
            )
        normalized_parameters["command"] = normalized_command
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=normalized_parameters,
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    if isinstance(command, Sequence) and not isinstance(
        command,
        (str, bytes, bytearray),
    ):
        normalized_command_list = [
            _required_shell_command_item(item) for item in command
        ]
        if not normalized_command_list:
            raise PowdrrExecutionError(
                "Workflow invoke_tool action command must not be empty."
            )
        normalized_parameters = dict(parameters)
        normalized_parameters["command"] = normalized_command_list
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=normalized_parameters,
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    raise PowdrrExecutionError(
        "Workflow invoke_tool action command must be a string or array."
    )


def _first_class_action_from_command(command: object) -> str | None:
    if isinstance(command, str):
        try:
            command_items = shlex.split(command)
        except ValueError:
            return None
    elif isinstance(command, Sequence) and not isinstance(
        command, (str, bytes, bytearray)
    ):
        command_items = list(command)
    else:
        return None
    if (
        len(command_items) < 2
        or not isinstance(command_items[0], str)
        or not isinstance(command_items[1], str)
        or command_items[0] != "powdrr-lift"
    ):
        return None
    action_name = command_items[1].replace("-", "_")
    if action_name in _workflow_action_parsers() and action_name != "invoke_tool":
        return action_name
    return None


def _first_class_action_example(action_name: str) -> str:
    examples = {
        "gather_context": '{"action":"gather_context","types":["requirements"]}',
        "prompt_user": '{"action":"prompt_user","text":"What is the decision?"}',
        "edit": '{"action":"edit","file_path":"src/example.py","edits":[...]}',
        "yaml_edit": (
            '{"action":"yaml_edit","file_path":"docs/example.yaml","operations":[...]}'
        ),
        "file_management": (
            '{"action":"file_management","operation":"rename",'
            '"file_path":"old.txt","destination_path":"new.txt"}'
        ),
        "delete_file": '{"action":"delete_file","file_path":"old.txt"}',
        "invoke_skill": '{"action":"invoke_skill","skill":"skill-name"}',
        "goto_step": '{"action":"goto_step","step_id":"step-id"}',
        "read_document": (
            '{"action":"read_document","file_path":"docs/example.md",'
            '"start_line":0,"end_line":20}'
        ),
        "list_files": (
            '{"action":"list_files","directory":"src",'
            '"pattern":"*.py","recursive":true}'
        ),
        "next_step": '{"action":"next_step"}',
        "complete": '{"action":"complete"}',
    }
    return examples[action_name]


def _parse_workflow_action_file_management(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    operation = payload.get("operation")
    if operation not in {"delete", "move", "rename"}:
        raise PowdrrExecutionError(
            "file_management operation must be delete, move, or rename."
        )
    file_path = payload.get("file_path")
    # Some models use the move/rename field name for the target of a delete.
    # Treat it as the source path for delete, while keeping the internal action
    # canonical so execution and event recording remain unchanged.
    if operation == "delete" and (
        not isinstance(file_path, str) or not file_path.strip()
    ):
        file_path = payload.get("destination_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PowdrrExecutionError(
            "file_management action requires file_path or destination_path for delete."
            if operation == "delete"
            else "file_management action requires file_path."
        )
    destination_path = payload.get("destination_path")
    if operation == "delete":
        destination_path = None
    if operation in {"move", "rename"} and (
        not isinstance(destination_path, str) or not destination_path.strip()
    ):
        raise PowdrrExecutionError(
            f"file_management {operation} requires destination_path."
        )
    if destination_path is not None and not isinstance(destination_path, str):
        raise PowdrrExecutionError("file_management destination_path must be a string.")
    return SkillChatAction(
        kind="file_management",
        file_operation=operation,
        file_path=file_path.strip(),
        destination_path=(
            destination_path.strip() if isinstance(destination_path, str) else None
        ),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_delete_file(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PowdrrExecutionError("delete_file action requires file_path.")
    return SkillChatAction(
        kind="delete_file",
        file_operation="delete",
        file_path=file_path.strip(),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_read_document(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PowdrrExecutionError(
            "Workflow read_document action must include file_path."
        )
    start_line = _required_document_line_number(
        payload.get("start_line"),
        field_name="start_line",
    )
    end_line = _required_document_line_number(
        payload.get("end_line"),
        field_name="end_line",
    )
    effective_start_line = max(1, start_line)
    if end_line < effective_start_line:
        raise PowdrrExecutionError(
            "Workflow read_document action end_line must be >= start_line."
        )
    return SkillChatAction(
        kind="read_document",
        file_path=file_path.strip(),
        start_line=start_line,
        end_line=end_line,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_list_files(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    directory = payload.get("directory", ".")
    if not isinstance(directory, str) or not directory.strip():
        raise PowdrrExecutionError(
            "Workflow list_files directory must be a non-empty string."
        )
    pattern = payload.get("pattern")
    if pattern is not None and (not isinstance(pattern, str) or not pattern.strip()):
        raise PowdrrExecutionError(
            "Workflow list_files pattern must be a non-empty string."
        )
    recursive = payload.get("recursive", False)
    if not isinstance(recursive, bool):
        raise PowdrrExecutionError("Workflow list_files recursive must be a boolean.")
    return SkillChatAction(
        kind="list_files",
        directory=directory.strip(),
        pattern=pattern.strip() if isinstance(pattern, str) else None,
        recursive=recursive,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_next_step(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    _ = payload
    return SkillChatAction(
        kind="next_step",
        output_state=payload.get("output_state"),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_prompt_user(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    text = payload.get("text")
    if not isinstance(text, str):
        raise PowdrrExecutionError(
            'Workflow prompt_user action requires a string text field; use "text".'
        )
    text = _validate_user_question(
        text,
        field_name="Workflow prompt_user action text",
    )
    capture_as = payload.get("capture_as")
    if capture_as is not None and (
        not isinstance(capture_as, str) or not capture_as.strip()
    ):
        raise PowdrrExecutionError(
            "Workflow prompt_user capture_as must be a non-empty string."
        )
    return SkillChatAction(
        kind="prompt_user",
        text=(text.strip() if text else None),
        capture_as=(capture_as.strip() if isinstance(capture_as, str) else None),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _validate_user_question(value: str, *, field_name: str) -> str:
    normalized_value = value.strip()
    if (
        not normalized_value
        or not re.search(r"[A-Za-z]", normalized_value)
        or not normalized_value.endswith("?")
    ):
        raise PowdrrExecutionError(
            f"{field_name} must be a non-empty, properly formed English question."
        )
    return normalized_value


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
