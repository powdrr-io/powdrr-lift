from __future__ import annotations

import json
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
from powdrr_lift.workflow_chat_agent import (
    ZAI_LLM_MAPPINGS,
    LocalLlamaChatClient,
    _execute_shell_tool,
    _print_waiting_for_model,
    _resolve_credentials,
    _resolve_llm_mapping,
    _resolve_local_model_path,
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

    events: list[dict[str, Any]] = []
    for _roundtrip in range(max(1, config.max_roundtrips)):
        _print_waiting_for_model(stderr, model)
        response = client.complete_json(_build_task_messages(workflow, task, events))
        action = _parse_task_action(response)
        if action.kind == "invoke_tool":
            result = _execute_shell_tool(
                action.parameters,
                worktree_root=config.repo_root,
                stdout=stdout,
                stderr=stderr,
                verbose=config.verbose,
            )
            events.append(
                {
                    "kind": action.kind,
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
                },
                indent=2,
                ensure_ascii=False,
            ),
        },
    ]


def _task_system_prompt() -> str:
    return (
        "Task: act as a staff engineer processing one claimed task from a durable "
        "workflow. Use the task "
        "input, completed upstream outputs, task details, and prior events to "
        "make safe progress toward the task's output.\n"
        "Choose exactly one outcome:\n"
        "- invoke_tool: choose this when a command is needed to inspect the "
        "worktree or perform work required to determine the output.\n"
        "- complete: choose this when the task can be safely finished now; put "
        "the produced state in output_state.\n"
        "- get-human-input: choose this only when a human decision or review is "
        "required for safe completion; describe the human task, its input, the "
        "required role, and the expected output state. Do not use it for ordinary "
        "chat clarification.\n"
        "Response: return exactly one JSON object matching exactly one outcome "
        "shape below. Do not include markdown or combine outcomes.\n"
        '{"kind":"invoke_tool","parameters":{"command":["..."]}}\n'
        '{"kind":"complete","output_state":{},"text":"..."}\n'
        '{"kind":"get-human-input","human_input":{"human_task":{'
        '"description":"...","role":"decider","input_state":{},'
        '"output_state_type":"decision"},"incorporation_instructions":"...",'
        '"follow_up_task":{"description":"...","role":"coder",'
        '"input_state":{},"output_state_type":"state"}}}\n'
        "Use invoke_tool for work needed to determine the output. Use complete "
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
        return WorkflowTaskAction(kind=normalized_kind, parameters=parameters)
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
