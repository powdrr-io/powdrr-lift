from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TextIO

from powdrr_lift.core import (
    AgentRole,
    AssigneeType,
    HumanRole,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
)
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    render_gather_context_report,
)
from powdrr_lift.workflow_chat_agent import (
    ZAI_LLM_MAPPINGS,
    LocalLlamaChatClient,
    _execute_fuzzy_match_tool,
    _execute_shell_tool,
    _LLMExchangeRecordingClient,
    _print_waiting_for_model,
    _resolve_credentials,
    _resolve_llm_mapping,
    _resolve_local_model_path,
    _resolve_project_root,
)


class WorkflowTaskChatClient(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WorkflowTaskAgentConfig:
    workflow_dir: Path
    repo_root: Path = Path(".")
    task_id: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_roundtrips: int = 12
    verbose: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowTaskAction:
    kind: str
    tool: str = "shell"
    output_state: Any = None
    text: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    human_input: dict[str, Any] | None = None


def run_workflow_task(
    config: WorkflowTaskAgentConfig,
    *,
    client: WorkflowTaskChatClient | None = None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    workflow = WorkflowInstance.from_directory(config.workflow_dir)
    task = _select_task(workflow, config.task_id)
    if task is None:
        print("No ready agent task found.", file=stderr)
        return 1
    task = workflow.claim_task(task.task_id)
    print(f"Claimed workflow task: {task.task_id}", file=stdout)
    mapping = _resolve_llm_mapping(
        task.llm_type,
        mappings=tuple(ZAI_LLM_MAPPINGS.items()),
        provider="zai",
    )
    if mapping is None:
        raise RuntimeError(f"Workflow task has no LLM mapping: {task.task_id}")
    model = mapping.model
    if client is None:
        client = _build_zai_client(config, task)
    dump_root = _resolve_project_root(
        config.repo_root.resolve(), config.repo_root.resolve()
    )
    client = _LLMExchangeRecordingClient(client, dump_root)

    events: list[dict[str, Any]] = []
    response_correction: str | None = None
    for _roundtrip in range(max(1, config.max_roundtrips)):
        _print_waiting_for_model(stderr, model)
        messages = _build_task_messages(
            workflow,
            task,
            events,
            response_correction=response_correction,
        )
        try:
            response = client.complete_json(messages)
            action = _parse_task_action(response)
        except RuntimeError as exc:
            if not _is_repairable_task_response_error(exc):
                raise
            response_correction = (
                "The previous response was invalid: "
                f"{exc} Return exactly one complete JSON object matching one of "
                "the documented action shapes. Do not return markdown, prose, "
                "or an empty response."
            )
            print(
                "Workflow task response needs repair; requesting a corrected "
                "JSON response from the LLM.",
                file=stderr,
            )
            events.append(
                {
                    "kind": "llm_response_error",
                    "error": str(exc),
                }
            )
            continue
        response_correction = None
        if action.kind == "invoke_tool":
            repaired_parameters = _repair_workflow_file_command(
                action.parameters,
                workflow.directory,
            )
            if repaired_parameters is not None:
                print(
                    "Corrected malformed workflow filename suffix to the exact "
                    ".json filename.",
                    file=stderr,
                )
                action = WorkflowTaskAction(
                    kind=action.kind,
                    tool=action.tool,
                    parameters=repaired_parameters,
                )
            command_error = _workflow_file_command_error(
                action.parameters,
                workflow.directory,
            )
            if command_error is not None:
                print(command_error, file=stderr)
                result = {
                    "command": action.parameters.get("command"),
                    "cwd": str(config.repo_root),
                    "returncode": 2,
                    "stdout": "",
                    "stderr": command_error,
                }
            else:
                if action.tool == "shell":
                    result = _execute_shell_tool(
                        action.parameters,
                        worktree_root=config.repo_root,
                        stdout=stdout,
                        stderr=stderr,
                        verbose=config.verbose,
                    )
                elif action.tool == "fuzzy-match":
                    result = _execute_fuzzy_match_tool(
                        action.parameters,
                        worktree_root=config.repo_root,
                    )
                elif action.tool == "gather-context":
                    result = _execute_gather_context_tool(
                        action.parameters,
                        worktree_root=config.repo_root,
                    )
                else:
                    raise RuntimeError(
                        f"Unsupported workflow task tool {action.tool!r}; "
                        "supported tools are shell, fuzzy-match, and "
                        "gather-context."
                    )
            events.append(
                {
                    "kind": action.kind,
                    "tool": action.tool,
                    "parameters": action.parameters,
                    "result": result,
                }
            )
            continue
        if action.kind == "complete":
            completed = workflow.complete_task(task.task_id, action.output_state)
            if action.text:
                print(action.text, file=stdout)
            print(f"Completed workflow task: {completed.task_id}", file=stdout)
            return 0
        if action.kind == "get-human-input":
            human_task, follow_up_task = _insert_human_handoff(
                workflow,
                task,
                action.human_input or {},
            )
            print(f"Workflow blocked on human task: {human_task.task_id}", file=stdout)
            if follow_up_task is not None:
                print(
                    f"Inserted follow-up task: {follow_up_task.task_id}",
                    file=stdout,
                )
            return 0
        raise RuntimeError(f"Unsupported workflow task action: {action.kind}")

    print(
        f"Workflow task {task.task_id} blocked after reaching the roundtrip limit.",
        file=stderr,
    )
    return 2


def _select_task(
    workflow: WorkflowInstance,
    task_id: str | None,
) -> WorkflowTask | None:
    ready_tasks = workflow.ready_tasks(assignee_type=AssigneeType.AGENT)
    if task_id is not None:
        selected = next((task for task in ready_tasks if task.task_id == task_id), None)
        if selected is None:
            raise ValueError(f"Task is not a ready agent task: {task_id}")
        return selected
    return ready_tasks[0] if ready_tasks else None


def _build_task_messages(
    workflow: WorkflowInstance,
    task: WorkflowTask,
    events: list[dict[str, Any]],
    *,
    response_correction: str | None = None,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _task_system_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "execution_mode": "process_workflow_task",
                    "task": task.to_data(),
                    "task_context": workflow.task_context(task.task_id),
                    "events": events,
                    "workflow_dir": str(workflow.directory),
                    "workflow_files": _workflow_file_names(workflow.directory),
                    "available_tools": [
                        {
                            "name": "shell",
                            "description": (
                                "Execute a shell command in the current worktree."
                            ),
                        },
                        {
                            "name": "fuzzy-match",
                            "description": (
                                "Search worktree paths with find-like filters and "
                                "fuzzy name matching."
                            ),
                        },
                        {
                            "name": "gather-context",
                            "description": (
                                "Find structured specification context by type and "
                                "optional keywords."
                            ),
                        },
                    ],
                    "response_correction": response_correction,
                },
                indent=2,
                ensure_ascii=False,
            ),
        },
    ]


def _is_repairable_task_response_error(exc: RuntimeError) -> bool:
    message = str(exc).casefold()
    return any(
        phrase in message
        for phrase in (
            "was not valid json",
            "must be a json object",
            "workflow task action must include",
            "unknown workflow task action",
            "must include parameters.command",
            "must include output_state",
        )
    )


def _task_system_prompt() -> str:
    return (
        "Task: act as a staff engineer processing one claimed task from a durable "
        "workflow. Use the task "
        "input, completed upstream outputs, task details, and prior events to "
        "make safe progress toward the task's output.\n"
        "The user message includes workflow_files, the exact JSON filenames "
        "available in workflow_dir. When reading or writing workflow files, use "
        "those exact filenames, including the .json suffix. Never infer, shorten, "
        "or replace a filename with a trailing period. If a filename is not in "
        "workflow_files, list the directory before using it.\n"
        "If response_correction is non-null, it describes a validation failure "
        "from your previous response. Correct that failure and return only the "
        "required JSON object.\n"
        "Choose exactly one outcome:\n"
        "- invoke_tool: choose this when a shell, fuzzy-match, or gather-context "
        "command is needed "
        "to inspect the worktree or perform work required to determine the "
        "output.\n"
        "- complete: choose this when the task can be safely finished now; put "
        "the produced state in output_state.\n"
        "- get-human-input: choose this only when a human decision or review is "
        "required for safe completion; describe the human task, its input, the "
        "required role, and the expected output state. Do not use it for ordinary "
        "chat clarification.\n"
        "Response: return exactly one JSON object matching exactly one outcome "
        "shape below. Do not include markdown or combine outcomes.\n"
        '{"kind":"invoke_tool","tool":"shell","parameters":{"command":["..."]}}\n'
        '{"kind":"complete","output_state":{},"text":"..."}\n'
        '{"kind":"get-human-input","human_input":{"human_task":{'
        '"description":"...","role":"decider","input_state":{},'
        '"output_state_type":"decision"},"incorporation_instructions":"...",'
        '"follow_up_task":{"description":"...","role":"coder",'
        '"input_state":{},"output_state_type":"state"}}}\n'
        "Use invoke_tool for work needed to determine the output. Set tool to "
        "shell for repository commands, fuzzy-match for fuzzy path discovery, or "
        "gather-context for structured specification context. "
        "The fuzzy-match command starts with fuzzy-match and supports find-like "
        "options including -name, -path, -type, -maxdepth, -mindepth, "
        "-threshold, and -print. Use complete "
        "when the task can be finished and put the task's produced state in "
        "output_state. Use get-human-input when you cannot safely finish without "
        "a human decision or review. It inserts the human task into this workflow "
        "and may insert a follow-up agent task that depends on it, then blocks "
        "this task. The human_task must specify what the human should do, the "
        "required role (decider or reviewer), the context/input_state to provide, "
        "and the output_state_type expected from the human. "
        "incorporation_instructions must tell the follow-up agent how to use the "
        "human output. Do not ask for ordinary chat clarification.\n"
        "Do not output markdown."
    )


def _workflow_file_names(workflow_dir: Path) -> list[str]:
    return sorted(path.name for path in workflow_dir.glob("*.json") if path.is_file())


def _execute_gather_context_tool(
    parameters: dict[str, Any],
    *,
    worktree_root: Path,
) -> dict[str, Any]:
    command = parameters.get("command")
    if not isinstance(command, (str, list, tuple)):
        raise RuntimeError(
            "Workflow gather-context tool parameters must include a command array."
        )
    command_items = command.split() if isinstance(command, str) else list(command)
    if not command_items or command_items[0] != "gather-context":
        raise RuntimeError("gather-context command must start with 'gather-context'.")

    types: list[str] = []
    keywords: list[str] = []
    collecting_keywords = False
    for item in command_items[1:]:
        if not isinstance(item, str) or not item:
            continue
        if item in {"--keywords", "-keywords"}:
            collecting_keywords = True
            continue
        if collecting_keywords:
            keywords.append(item)
        else:
            types.append(item)
    report = gather_specification_context(
        worktree_root,
        types=types,
        keywords=keywords,
    )
    return {
        "tool": "gather-context",
        "command": command_items,
        "result": json.loads(render_gather_context_report(report)),
    }


def _workflow_file_command_error(
    parameters: dict[str, Any],
    workflow_dir: Path,
) -> str | None:
    command = parameters.get("command")
    if isinstance(command, str):
        command_text = command
    elif isinstance(command, list) and all(isinstance(item, str) for item in command):
        command_text = " ".join(command)
    else:
        return None

    workflow_path = str(workflow_dir.resolve())
    valid_files = _workflow_file_names(workflow_dir)
    if not valid_files or workflow_path not in command_text:
        return None

    missing_references: list[str] = []
    pattern = re.compile(re.escape(workflow_path) + r"/[^\s'\";&|()]+")
    for match in pattern.finditer(command_text):
        reference = match.group(0).rstrip(",")
        if reference.endswith("."):
            missing_references.append(reference)

    if not missing_references:
        return None
    valid_paths = ", ".join(str(workflow_dir / name) for name in valid_files)
    return (
        "Rejected workflow shell command because it referenced nonexistent "
        f"filename(s): {', '.join(missing_references)}. Use the exact .json "
        f"filenames from workflow_files: {valid_paths}."
    )


def _repair_workflow_file_command(
    parameters: dict[str, Any],
    workflow_dir: Path,
) -> dict[str, Any] | None:
    command = parameters.get("command")
    valid_files = _workflow_file_names(workflow_dir)
    if not valid_files:
        return None

    def repair_text(text: str) -> str:
        repaired = text
        for filename in valid_files:
            exact_stem = str(workflow_dir.resolve() / filename.removesuffix(".json"))
            repaired = re.sub(
                re.escape(exact_stem) + r"\.+(?=[\s'\";&|()]|$)",
                str(workflow_dir.resolve() / filename),
                repaired,
            )
        return repaired

    repaired_command: str | list[str]
    if isinstance(command, str):
        repaired_command = repair_text(command)
    elif isinstance(command, list) and all(isinstance(item, str) for item in command):
        repaired_command = [repair_text(item) for item in command]
    else:
        return None

    if repaired_command == command:
        return None
    repaired_parameters = dict(parameters)
    repaired_parameters["command"] = repaired_command
    return repaired_parameters


def _parse_task_action(payload: dict[str, Any]) -> WorkflowTaskAction:
    kind = payload.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise RuntimeError("Workflow task action must include kind.")
    normalized_kind = kind.strip()
    if normalized_kind == "complete":
        if "output_state" not in payload:
            raise RuntimeError("Complete action must include output_state.")
        text = payload.get("text")
        if text is not None and not isinstance(text, str):
            raise RuntimeError("Complete action text must be a string.")
        return WorkflowTaskAction(
            kind=normalized_kind,
            output_state=payload["output_state"],
            text=text,
        )
    if normalized_kind == "invoke_tool":
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict) or "command" not in parameters:
            raise RuntimeError("Invoke-tool action must include parameters.command.")
        tool = payload.get("tool", "shell")
        if not isinstance(tool, str) or tool not in {
            "shell",
            "fuzzy-match",
            "gather-context",
        }:
            raise RuntimeError(
                "Invoke-tool action tool must be shell, fuzzy-match, or gather-context."
            )
        return WorkflowTaskAction(
            kind=normalized_kind,
            tool=tool,
            parameters=parameters,
        )
    if normalized_kind == "get-human-input":
        return WorkflowTaskAction(
            kind=normalized_kind,
            human_input=_parse_human_input(payload.get("human_input")),
        )
    raise RuntimeError(f"Unknown workflow task action kind: {normalized_kind!r}")


def _parse_human_input(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("get-human-input must include human_input.")
    human_task = _parse_handoff_task(value.get("human_task"), HumanRole, "human_task")
    instructions = value.get("incorporation_instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise RuntimeError("get-human-input must include incorporation_instructions.")
    follow_up = value.get("follow_up_task")
    return {
        "human_task": human_task,
        "incorporation_instructions": instructions.strip(),
        "follow_up_task": (
            _parse_handoff_task(follow_up, AgentRole, "follow_up_task")
            if follow_up is not None
            else None
        ),
    }


def _parse_handoff_task(
    value: object,
    role_type: type[HumanRole] | type[AgentRole],
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{field_name} must be an object.")
    description = value.get("description")
    role = value.get("role")
    output_state_type = value.get("output_state_type")
    if not isinstance(description, str) or not description.strip():
        raise RuntimeError(f"{field_name}.description must be non-empty.")
    if not isinstance(role, str):
        raise RuntimeError(f"{field_name}.role must be provided.")
    try:
        normalized_role = role_type(role).value
    except ValueError as exc:
        raise RuntimeError(f"Invalid {field_name}.role: {role}") from exc
    if not isinstance(output_state_type, str) or not output_state_type.strip():
        raise RuntimeError(f"{field_name}.output_state_type must be non-empty.")
    if "input_state" not in value:
        raise RuntimeError(f"{field_name}.input_state must be provided.")
    return {
        "description": description.strip(),
        "role": normalized_role,
        "input_state": value["input_state"],
        "output_state_type": output_state_type.strip(),
    }


def _insert_human_handoff(
    workflow: WorkflowInstance,
    blocked_task: WorkflowTask,
    human_input: dict[str, Any],
) -> tuple[WorkflowTask, WorkflowTask | None]:
    human_spec = human_input["human_task"]
    human_id = _next_handoff_id(workflow)
    human_task = WorkflowTask(
        task_id=human_id,
        status=TaskStatus.OPEN,
        description=human_spec["description"],
        complexity=TaskComplexity.LOW,
        input_state=human_spec["input_state"],
        assignee_type=AssigneeType.HUMAN,
        assignee_role=HumanRole(human_spec["role"]),
        output_state_type=human_spec["output_state_type"],
        upstream_task_ids=blocked_task.upstream_task_ids,
    )
    workflow.add_task(human_task)
    follow_up_spec = human_input.get("follow_up_task")
    if follow_up_spec is None:
        return human_task, None
    follow_up_task = WorkflowTask(
        task_id=f"{human_id}-follow-up",
        status=TaskStatus.OPEN,
        description=follow_up_spec["description"],
        complexity=TaskComplexity.MEDIUM,
        input_state=follow_up_spec["input_state"],
        assignee_type=AssigneeType.AGENT,
        assignee_role=AgentRole(follow_up_spec["role"]),
        details=human_input["incorporation_instructions"],
        output_state_type=follow_up_spec["output_state_type"],
        upstream_task_ids=(human_id,),
    )
    workflow.add_task(follow_up_task)
    return human_task, follow_up_task


def _next_handoff_id(workflow: WorkflowInstance) -> str:
    existing_ids = {task.task_id for task in workflow.tasks}
    index = 1
    while f"human-input-{index}" in existing_ids:
        index += 1
    return f"human-input-{index}"


def _build_zai_client(
    config: WorkflowTaskAgentConfig,
    task: WorkflowTask,
) -> WorkflowTaskChatClient:
    from powdrr_lift.workflow_chat_agent import OpenAIChatClient

    mapping = _resolve_llm_mapping(
        task.llm_type,
        mappings=tuple(ZAI_LLM_MAPPINGS.items()),
        provider="zai",
    )
    if mapping is None:
        raise RuntimeError(f"Workflow task has no llm_type mapping: {task.task_id}")
    model = mapping.model
    if mapping.provider == "local":
        return LocalLlamaChatClient(
            model_path=_resolve_local_model_path(
                config.repo_root / ".powdrr" / "models"
            )
        )
    credentials = _resolve_credentials("zai", config.api_key, config.base_url)
    return OpenAIChatClient(
        model=model,
        api_key=credentials.api_key,
        base_url=credentials.base_url,
    )
