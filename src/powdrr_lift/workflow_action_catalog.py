"""Process-language action availability and recovery policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_lift.core import SkillToolInvocation
from powdrr_lift.core.spec_context import normalize_context_type
from powdrr_lift.workflow_step_behavior import behavior_for_step

DEFAULT_ACTION_INSTRUCTIONS: dict[str, str] = {
    "gather_context": "Discover checked-in specifications relevant to this step.",
    "prompt_user": "Ask one necessary human question.",
    "edit": "Apply a known line-based change to a file.",
    "yaml_edit": "Apply a structural change to a YAML file.",
    "file_management": "Move or rename one relative file.",
    "delete_file": "Delete one relative file using file_path.",
    "invoke_skill": "Run one listed nested skill.",
    "invoke_tool": "Run one command declared by the step.",
    "read_document": "Read a bounded range from a known document.",
    "list_files": "Discover exact file paths.",
    "goto_step": "Repeat one declared prior step when another pass is needed.",
    "next_step": "Advance after this step is complete.",
    "emit_outputs": "Publish the completed outputs for a predicated step.",
    "complete": "End the skill after all work is finished.",
}


def _predicated_parameter_matches(actual: Any, expected: Any, name: str) -> bool:
    if (
        name == "types"
        and isinstance(actual, Sequence)
        and isinstance(expected, Sequence)
    ):
        try:
            return [normalize_context_type(str(item)) for item in actual] == [
                normalize_context_type(str(item)) for item in expected
            ]
        except ValueError:
            return list(actual) == list(expected)
    return actual == expected


def predicated_context_complete(
    step: Any,
    execution_events: Sequence[Mapping[str, Any]],
    step_index: int,
) -> bool:
    completion = getattr(step, "completion", None)
    if completion is None or not completion.required_actions:
        return False
    for requirement in completion.required_actions:
        matching = [
            event
            for event in execution_events
            if event.get("step_index") == step_index
            and event.get("kind") == requirement.action
            and all(
                _predicated_parameter_matches(event.get(name), value, name)
                for name, value in (requirement.parameters or {}).items()
            )
        ]
        if requirement.exactly is not None and len(matching) != requirement.exactly:
            return False
        if not matching:
            return False
    return True


def recovery_tool_invocations(
    step: Any,
    execution_events: Sequence[Mapping[str, Any]],
    step_index: int | None,
) -> tuple[SkillToolInvocation, ...]:
    """Return bounded diagnostics/corrections after a tool failure."""
    if step_index is None or not any(
        event.get("step_index") == step_index
        and event.get("kind") in {"action_error", "tool_error"}
        for event in execution_events
    ):
        return ()
    if not any(
        invocation.tool in {"shell", "git"} for invocation in step.tool_invocations
    ):
        return ()
    commands = (
        ("git", "status", "--short"),
        ("git", "diff", "--cached", "--stat"),
        ("git", "diff", "--cached", "--name-only"),
        ("git", "add", "<files-to-stage>"),
        ("git", "commit", "-m", "<commit-message>"),
    )
    declared = {invocation.command for invocation in step.tool_invocations}
    return tuple(
        SkillToolInvocation(tool="shell", command=command, label="recovery")
        for command in commands
        if command not in declared
    )


def step_actions(
    step: Any,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
    validation_gate_enabled: bool | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return the process contract's available actions for one step."""
    behavior = behavior_for_step(step)
    if validation_gate_enabled is None:
        validation_gate_enabled = getattr(step, "validation_gate", None) is not None
    completion = None
    context_complete = False
    recovery_invocations = recovery_tool_invocations(step, execution_events, step_index)
    declared = tuple(getattr(step, "actions", ()) or ())
    if declared or getattr(step, "actions_declared", False):
        actions = [(name, DEFAULT_ACTION_INSTRUCTIONS[name]) for name in declared]
    else:
        names: list[str] = []
        if (
            getattr(step, "details", None)
            or getattr(step, "tool_invocations", ())
            or getattr(step, "uses_skill", None)
        ):
            names.extend(
                [
                    "gather_context",
                    "edit",
                    "yaml_edit",
                    "file_management",
                    "delete_file",
                    "read_document",
                    "prompt_user",
                ]
            )
        else:
            names.append("invoke_skill")
        if getattr(step, "tool_invocations", ()):
            names.insert(0, "invoke_tool")
        if getattr(step, "uses_skill", None):
            names.insert(0, "invoke_skill")
        if validation_gate_enabled:
            names = [
                "invoke_tool",
                "edit",
                "yaml_edit",
                "file_management",
                "delete_file",
                "prompt_user",
            ]
        if not behavior.invokes_llm:
            names = []
        actions = [(name, DEFAULT_ACTION_INSTRUCTIONS[name]) for name in names]
    if recovery_invocations and not any(name == "invoke_tool" for name, _ in actions):
        actions.insert(0, ("invoke_tool", DEFAULT_ACTION_INSTRUCTIONS["invoke_tool"]))
    if behavior.is_predicated:
        completion = getattr(step, "completion", None)
        context_complete = (
            completion is not None
            and step_index is not None
            and predicated_context_complete(step, execution_events, step_index)
        )
        if completion is not None and completion.required_actions:
            if context_complete:
                actions = [
                    ("emit_outputs", DEFAULT_ACTION_INSTRUCTIONS["emit_outputs"])
                ]
        else:
            actions.append(
                ("emit_outputs", DEFAULT_ACTION_INSTRUCTIONS["emit_outputs"])
            )
        action_names = {name for name, _ in actions}
    else:
        action_names = {name for name, _ in actions}
    if "prompt_user" not in action_names and not (
        behavior.is_predicated
        and completion is not None
        and completion.required_actions
        and context_complete
    ):
        actions.append(("prompt_user", DEFAULT_ACTION_INSTRUCTIONS["prompt_user"]))
    if "next_step" not in action_names and not behavior.is_predicated:
        actions.append(("next_step", "Advance only after this step is complete."))
    outputs = tuple(
        output
        for output in (getattr(step, "outputs", ()) or ())
        if getattr(output, "required_for_next_step", False)
    )
    if outputs:
        suffix = (
            " Include outputs: " + ", ".join(output.name for output in outputs) + "."
        )
        actions = [
            (name, instructions + suffix if name == "next_step" else instructions)
            for name, instructions in actions
        ]
    return tuple(actions)


def declared_action_names(
    step: Any,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
    validation_gate_enabled: bool | None = None,
) -> tuple[str, ...]:
    """Return the closed action names, including implicit next_step."""
    behavior = behavior_for_step(step)
    names = [
        name
        for name, _ in step_actions(
            step,
            execution_events=execution_events,
            step_index=step_index,
            validation_gate_enabled=validation_gate_enabled,
        )
    ]
    if "next_step" not in names and not behavior.is_predicated:
        names.append("next_step")
    return tuple(names)
