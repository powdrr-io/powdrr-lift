"""Task-local execution settings retained by durable workflow documents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from powdrr_lift.basedpyright_tools import BASEDPYRIGHT_TOOLS

SUPPORTED_TASK_STEP_TYPES = frozenset(
    {"governed", "invoke_tool", "gate", "coding_loop"}
)
SUPPORTED_TASK_INTERACTION_STYLES = frozenset(
    {"engineering", "observational_review", "devils_advocate"}
)
SUPPORTED_TASK_ACTIONS = frozenset(
    {
        "gather_context",
        "prompt_user",
        "edit",
        "yaml_edit",
        "file_management",
        "delete_file",
        "task_run_process",
        "task_complete",
        "task_handoff",
        "invoke_tool",
        "read_document",
        "list_files",
        "respond",
    }
)
_SUPPORTED_TOOLS = (
    frozenset(
        {
            "shell",
            "internal",
            "git",
            "gh",
            "fuzzy-match",
            "enrich",
            "validate_edit",
            "apply_edit",
            "ref",
        }
    )
    | BASEDPYRIGHT_TOOLS
)
_TOOL_PACKAGES: dict[str, tuple[dict[str, Any], ...]] = {
    "git_readonly": tuple(
        {"tool": "git", "operation": operation}
        for operation in (
            "status",
            "remote",
            "branch_current",
            "default_branch",
            "show_ref",
        )
    ),
    "git_additive": tuple(
        {"tool": "git", "operation": operation}
        for operation in (
            "add",
            "commit",
            "push",
            "switch",
            "switch_create",
            "move",
            "rename",
        )
    ),
    "gh_readonly": tuple(
        {"tool": "gh", "operation": operation}
        for operation in ("pr_view", "pr_diff", "pr_checks", "pr_comments")
    ),
    "gh_write": tuple(
        {"tool": "gh", "operation": operation}
        for operation in ("pr_create", "pr_edit", "pr_review_comment")
    ),
}
_TOOL_PACKAGES["git_readonly_and_additive"] = (
    *_TOOL_PACKAGES["git_readonly"],
    *_TOOL_PACKAGES["git_additive"],
)
_TOOL_PACKAGES["gh_readonly_and_write"] = (
    *_TOOL_PACKAGES["gh_readonly"],
    *_TOOL_PACKAGES["gh_write"],
)


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    tool: str
    command: tuple[str, ...] = field(default_factory=tuple)
    operation: str | None = None
    label: str | None = None
    cwd: str | None = None
    env: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"tool": self.tool}
        if self.command:
            data["command"] = list(self.command)
        if self.operation is not None:
            data["operation"] = self.operation
        if self.label is not None:
            data["label"] = self.label
        if self.cwd is not None:
            data["cwd"] = self.cwd
        if self.env:
            data["env"] = dict(self.env)
        return data


@dataclass(frozen=True, slots=True)
class TaskPreStep:
    action: str
    template: Mapping[str, Any]

    def to_data(self) -> dict[str, Any]:
        return {"action": self.action, "template": dict(self.template)}


@dataclass(frozen=True, slots=True)
class TaskGate:
    outcome: Mapping[str, Any]
    goto_step: str
    retry_context: str
    success_goto_step: str | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "outcome": dict(self.outcome),
            "goto_step": self.goto_step,
            "retry_context": self.retry_context,
        }
        if self.success_goto_step is not None:
            data["success_goto_step"] = self.success_goto_step
        return data


@dataclass(frozen=True, slots=True)
class CodingLoopVerification:
    id: str
    command: str | tuple[str, ...]
    cwd: str | None = None
    env: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "command": self.command
            if isinstance(self.command, str)
            else list(self.command),
        }
        if self.cwd is not None:
            data["cwd"] = self.cwd
        if self.env:
            data["env"] = dict(self.env)
        return data


@dataclass(frozen=True, slots=True)
class CodingLoopSpec:
    goal: str
    verification: tuple[CodingLoopVerification, ...] = field(default_factory=tuple)
    stopping_conditions: tuple[str, ...] = field(default_factory=tuple)
    max_iterations: int = 12

    def to_data(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "verification": [item.to_data() for item in self.verification],
            "stopping_conditions": list(self.stopping_conditions),
            "max_iterations": self.max_iterations,
        }


@dataclass(frozen=True, slots=True)
class TaskExecutionSettings:
    details: str | None = None
    llm_type: str | None = None
    interaction_style: str | None = None
    tool_invocations: tuple[ToolInvocation, ...] = field(default_factory=tuple)
    prompt_catalogs: tuple[str, ...] = field(default_factory=tuple)
    actions: tuple[str, ...] = field(default_factory=tuple)
    actions_declared: bool = False
    step_type: str = "governed"
    pre_step: TaskPreStep | None = None
    gate: TaskGate | None = None
    coding_loop: CodingLoopSpec | None = None


def task_execution_settings_from_data(data: Mapping[str, Any]) -> TaskExecutionSettings:
    step_type = _optional_string(data.get("step_type")) or "governed"
    if step_type == "freeform":
        step_type = "governed"
    if step_type not in SUPPORTED_TASK_STEP_TYPES:
        raise ValueError("Workflow task step_type is unsupported.")
    interaction_style = _optional_string(data.get("interaction_style"))
    if (
        interaction_style is not None
        and interaction_style not in SUPPORTED_TASK_INTERACTION_STYLES
    ):
        raise ValueError("Workflow task interaction_style is unsupported.")
    tool_invocations = _parse_tool_invocations(data.get("tool_invocations"))
    packages = _parse_string_sequence(
        data.get("tool_invocation_packages"), "tool_invocation_packages"
    )
    for package in packages:
        if package not in _TOOL_PACKAGES:
            raise ValueError(f"Unsupported tool invocation package {package!r}.")
        for invocation in _TOOL_PACKAGES[package]:
            tool_invocations += (_parse_tool_invocation(invocation),)
    tool_invocations = tuple(dict.fromkeys(tool_invocations))
    prompt_catalogs = _parse_string_sequence(
        data.get("prompt_catalogs"), "prompt_catalogs"
    )
    actions_declared = "actions" in data
    actions = _parse_string_sequence(data.get("actions"), "actions")
    unsupported_actions = set(actions) - SUPPORTED_TASK_ACTIONS
    if unsupported_actions:
        raise ValueError(
            "Workflow task actions are unsupported: "
            + ", ".join(sorted(unsupported_actions))
        )
    pre_step = _parse_pre_step(data.get("pre_step"))
    gate = _parse_gate(data.get("gate"))
    coding_loop = (
        _parse_coding_loop(data.get("coding_loop"))
        if data.get("coding_loop") is not None
        else None
    )
    if step_type in {"invoke_tool", "gate"} and pre_step is None:
        raise ValueError("invoke_tool and gate task steps must declare pre_step.")
    if step_type == "gate" and gate is None:
        raise ValueError("gate task steps must declare gate.")
    if step_type != "gate" and gate is not None:
        raise ValueError("Only gate task steps may declare gate.")
    if step_type == "coding_loop" and coding_loop is None:
        raise ValueError("coding_loop task steps must declare coding_loop.")
    if step_type != "coding_loop" and coding_loop is not None:
        raise ValueError("Only coding_loop task steps may declare coding_loop.")
    return TaskExecutionSettings(
        details=_optional_string(data.get("details")),
        llm_type=_optional_string(data.get("llm_type")),
        interaction_style=interaction_style,
        tool_invocations=tool_invocations,
        prompt_catalogs=prompt_catalogs,
        actions=actions,
        actions_declared=actions_declared,
        step_type=step_type,
        pre_step=pre_step,
        gate=gate,
        coding_loop=coding_loop,
    )


def _parse_pre_step(value: object) -> TaskPreStep | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Workflow task pre_step must be an object.")
    action = _required_string(value, "action")
    template = value.get("template")
    if action not in {"gather_context", "invoke_tool"} or not isinstance(
        template, Mapping
    ):
        raise ValueError(
            "Workflow task pre_step must declare a supported action and template."
        )
    return TaskPreStep(action, dict(template))


def _parse_gate(value: object) -> TaskGate | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not isinstance(value.get("outcome"), Mapping):
        raise ValueError("Workflow task gate must declare an outcome object.")
    outcome = value["outcome"]
    if not _optional_string(outcome.get("path")) or "equals" not in outcome:
        raise ValueError("Workflow task gate outcome must declare path and equals.")
    return TaskGate(
        outcome=dict(outcome),
        goto_step=_required_string(value, "goto_step"),
        retry_context=_required_string(value, "retry_context"),
        success_goto_step=_optional_string(value.get("success_goto_step")),
    )


def _parse_coding_loop(value: object) -> CodingLoopSpec:
    if not isinstance(value, Mapping):
        raise ValueError("Workflow task coding_loop must be an object.")
    raw_verification = value.get("verification", [])
    if not isinstance(raw_verification, Sequence) or isinstance(
        raw_verification, (str, bytes, bytearray)
    ):
        raise ValueError("Workflow task coding_loop verification must be an array.")
    verification: list[CodingLoopVerification] = []
    for item in raw_verification:
        if not isinstance(item, Mapping):
            raise ValueError(
                "Workflow task coding_loop verification entries must be objects."
            )
        command = item.get("command")
        if isinstance(command, str) and command.strip():
            parsed_command: str | tuple[str, ...] = command
        elif isinstance(command, Sequence) and not isinstance(
            command, (str, bytes, bytearray)
        ):
            parsed_command = tuple(
                _required_string({"command": part}, "command") for part in command
            )
        else:
            raise ValueError("Workflow task verification command is required.")
        verification.append(
            CodingLoopVerification(
                id=_required_string(item, "id"),
                command=parsed_command,
                cwd=_optional_string(item.get("cwd")),
                env=tuple(sorted(_string_mapping(item.get("env"), "env").items())),
            )
        )
    if not verification:
        raise ValueError("Workflow task coding_loop requires verification commands.")
    max_iterations = value.get("max_iterations", 12)
    if (
        not isinstance(max_iterations, int)
        or isinstance(max_iterations, bool)
        or not 1 <= max_iterations <= 100
    ):
        raise ValueError(
            "Workflow task coding_loop max_iterations must be from 1 to 100."
        )
    return CodingLoopSpec(
        goal=_required_string(value, "goal"),
        verification=tuple(verification),
        stopping_conditions=_parse_string_sequence(
            value.get("stopping_conditions"), "stopping_conditions"
        ),
        max_iterations=max_iterations,
    )


def _parse_tool_invocations(value: object) -> tuple[ToolInvocation, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Workflow task tool_invocations must be an array.")
    return tuple(_parse_tool_invocation(item) for item in value)


def _parse_tool_invocation(value: object) -> ToolInvocation:
    if not isinstance(value, Mapping):
        raise ValueError("Workflow task tool_invocations entries must be objects.")
    tool = _required_string(value, "tool")
    if tool not in _SUPPORTED_TOOLS:
        raise ValueError(f"Unsupported workflow task tool {tool!r}.")
    command_value = value.get("command")
    operation = _optional_string(value.get("operation"))
    if operation is not None:
        if command_value is not None:
            raise ValueError(
                "Structured task tool invocations must not include command."
            )
        command: tuple[str, ...] = ()
    elif command_value is None:
        command = ()
    elif isinstance(command_value, Sequence) and not isinstance(
        command_value, (str, bytes, bytearray)
    ):
        command = tuple(
            _required_string({"command": part}, "command") for part in command_value
        )
    else:
        raise ValueError("Workflow task tool invocation command must be an array.")
    env = tuple(sorted(_string_mapping(value.get("env"), "env").items()))
    return ToolInvocation(
        tool=tool,
        command=command,
        operation=operation,
        label=_optional_string(value.get("label")),
        cwd=_optional_string(value.get("cwd")),
        env=env,
    )


def _parse_string_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"Workflow task {field_name} must be an array.")
    return tuple(_required_string({field_name: item}, field_name) for item in value)


def _string_mapping(value: object, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"Workflow task {field_name} must be an object.")
    return {
        _required_string({"key": key}, "key"): _required_string(
            {"value": item}, "value"
        )
        for key, item in value.items()
    }


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Workflow task {key} must be a non-empty string.")
    return value.strip()


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
