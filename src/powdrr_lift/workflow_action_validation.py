from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.builtin_tool_help import BUILTIN_TOOL_NAMES
from powdrr_lift.core.execution_state import ExecutionArtifact
from powdrr_lift.core.pr_specification import compile_split_pr_specification
from powdrr_lift.core.spec_context import normalize_context_type
from powdrr_lift.core.validation_messages import ValidationError
from powdrr_lift.execution.builtin_tools import invoke_file_mutation
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.intrinsic_git_gh import GH_TOOL, GIT_TOOL, intrinsic_command
from powdrr_lift.process.action_catalog import (
    declared_action_names as _declared_action_names,
)
from powdrr_lift.process.action_catalog import (
    recovery_tool_invocations as _recovery_tool_invocations,
)
from powdrr_lift.process.catalog import SkillCatalogEntry
from powdrr_lift.process.step_behavior import behavior_for_step
from powdrr_lift.workflow_action_protocol import _parse_action_response
from powdrr_lift.workflow_execution_state import (
    _ensure_execution_runtime,
    _ValidationGateState,
    _ValidationObligation,
    _WorkflowExecutionState,
)
from powdrr_lift.workflow_llm import (
    PowdrrExecutionError,
)
from powdrr_lift.workflow_llm import (
    WorkflowAction as SkillChatAction,
)
from powdrr_lift.workflow_observer import (
    ObserverActionRecommendation,
    observer_action_matches,
)
from powdrr_lift.workrr.context import WorkflowContext

_INTERNAL_TOOL = "internal"
_INTERNAL_BINARY = "powdrr-lift"


class _WorkflowToolValidationError(PowdrrExecutionError):
    def __init__(self, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(validation_error.message)


class _WorkflowStructuredDocumentError(PowdrrExecutionError):
    pass


def _invalidate_deterministic_pre_step(
    execution_events: list[dict[str, Any]],
    *,
    skill_name: str,
    step_index: int,
) -> None:
    execution_events[:] = [
        event
        for event in execution_events
        if not (
            event.get("kind") == "deterministic_pre_step"
            and event.get("skill_name") == skill_name
            and event.get("step_index") == step_index
        )
    ]


def _step_index_by_id(skill: SkillCatalogEntry, step_id: str | None) -> int:
    if step_id is None:
        raise PowdrrExecutionError("Workflow goto_step action must include step_id.")
    for index, step in enumerate(skill.skill.steps):
        if step.id == step_id:
            return index
    raise PowdrrExecutionError(
        f"Workflow goto_step target {step_id!r} is not declared in skill "
        f"{skill.skill.name!r}."
    )


def _worktree_relative_path(path: Path, worktree_root: Path) -> str:
    try:
        return path.resolve().relative_to(worktree_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _validate_internal_command(command: object) -> None:
    command_items = _command_items_for_validation(command)
    if command_items[0] != _INTERNAL_BINARY:
        raise PowdrrExecutionError(
            "The internal tool may invoke only the powdrr-lift binary."
        )


def _validate_structured_document_text(path: Path, text: str) -> None:
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise _WorkflowStructuredDocumentError(
                f"Edited JSON file {path} is invalid at line {exc.lineno}, "
                f"column {exc.colno}: {exc.msg}. Correct the JSON before continuing."
            ) from exc
    elif suffix in {".yaml", ".yml"}:
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            problem_mark = getattr(exc, "problem_mark", None)
            location = (
                f"line {problem_mark.line + 1}, column {problem_mark.column + 1}"
                if problem_mark is not None
                else "an unknown location"
            )
            problem = getattr(exc, "problem", None) or str(exc)
            raise _WorkflowStructuredDocumentError(
                f"Edited YAML file {path} is invalid at {location}: {problem}. "
                "Correct the YAML before continuing."
            ) from exc


def _validate_workflow_action_for_step(
    action: SkillChatAction,
    step: Any,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
    observer_allowed_action: ObserverActionRecommendation | None = None,
) -> None:
    """Reject tool actions that do not match a current-step invocation."""
    allowed_actions = _declared_action_names(
        step, execution_events=execution_events, step_index=step_index
    )
    if (
        allowed_actions is not None
        and action.kind not in allowed_actions
        and action.kind not in {"prompt_user", "next_step"}
        and not observer_action_matches(action, observer_allowed_action)
        and not (
            action.kind == "complete" and not getattr(step, "actions_declared", False)
        )
        and not (
            action.kind == "invoke_tool"
            and action.tool == "internal"
            and not getattr(step, "actions_declared", False)
        )
    ):
        raise _WorkflowToolValidationError(
            ValidationError(
                code="workflow_action_not_allowed",
                message=(
                    f"The {action.kind} action is not allowed in this step. "
                    + (
                        "This step explicitly supports: none; next_step is implicit."
                        if (
                            allowed_actions == ("next_step",)
                            or (
                                action.kind == "invoke_tool"
                                and action.tool == "shell"
                                and not getattr(step, "tool_invocations", ())
                            )
                        )
                        else "Use one of: " + ", ".join(allowed_actions) + "."
                    )
                ),
                path="kind",
            )
        )
    if action.kind != "invoke_tool":
        return
    try:
        _validate_workflow_action_for_step_unwrapped(
            action,
            step,
            execution_events=execution_events,
            step_index=step_index,
            observer_allowed_action=observer_allowed_action,
        )
    except RuntimeError as exc:
        if isinstance(exc, _WorkflowToolValidationError):
            raise
        raise _WorkflowToolValidationError(
            ValidationError(
                code="workflow_tool_action_invalid",
                message=str(exc),
                path="parameters.command",
            )
        ) from exc


def _validation_gate_config(step: Any) -> Mapping[str, Any] | None:
    value = getattr(step, "validation_gate", None)
    return value if isinstance(value, Mapping) else None


def _validation_gate_enabled(step: Any) -> bool:
    return _validation_gate_config(step) is not None


def _validation_gate_id(step: Any) -> str:
    config = _validation_gate_config(step)
    gate_id = config.get("id") if config is not None else None
    if not isinstance(gate_id, str) or not gate_id.strip():
        raise PowdrrExecutionError("Every validation gate must have a non-empty id.")
    return gate_id.strip()


def _validation_gate_state(
    state: _WorkflowExecutionState,
    step: Any,
) -> _ValidationGateState:
    gate_id = _validation_gate_id(step)
    gate_state = state.validation_gates.get(gate_id)
    if gate_state is None:
        gate_state = _ValidationGateState(step_index=state.step_index)
        state.validation_gates[gate_id] = gate_state
    return gate_state


def _nested_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _workflow_action_data(action: SkillChatAction) -> dict[str, Any]:
    return {
        "kind": action.kind,
        "tool": action.tool,
        "file_operation": action.file_operation,
        "file_path": action.file_path,
        "destination_path": action.destination_path,
        "parameters": dict(action.parameters),
        "types": list(action.types),
        "keywords": list(action.keywords),
        "filters": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in action.filters.items()
        },
        "feature_id": action.feature_id,
    }


def _action_template_matches(
    template: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> bool:
    for key, expected in template.items():
        value = actual.get(key)
        if isinstance(expected, Mapping):
            if not isinstance(value, Mapping) or not _action_template_matches(
                expected, value
            ):
                return False
        elif value != expected:
            return False
    return True


def _validation_actions_match(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> bool:
    """Match equivalent shell commands regardless of JSON string/list shape."""
    expected_parameters = expected.get("parameters")
    actual_parameters = actual.get("parameters")
    if not isinstance(expected_parameters, Mapping) or not isinstance(
        actual_parameters, Mapping
    ):
        return expected == actual

    def normalize_command(command: Any) -> Any:
        if isinstance(command, str):
            try:
                return tuple(shlex.split(command))
            except ValueError:
                return None
        if isinstance(command, Sequence) and not isinstance(
            command, (bytes, bytearray)
        ):
            return tuple(command)
        return command

    expected_command = normalize_command(expected_parameters.get("command"))
    actual_command = normalize_command(actual_parameters.get("command"))

    normalized_expected = dict(expected)
    normalized_actual = dict(actual)
    normalized_expected["parameters"] = {
        **expected_parameters,
        "command": expected_command,
    }
    normalized_actual["parameters"] = {
        **actual_parameters,
        "command": actual_command,
    }
    return normalized_expected == normalized_actual


def _discover_validation_obligations(
    result: Mapping[str, Any],
    *,
    state: _WorkflowExecutionState,
    gate: Any,
    gate_step_index: int,
    discovery_action: Mapping[str, Any] | None = None,
) -> None:
    config = _validation_gate_config(gate)
    assert config is not None
    discovery = config.get("discovery")
    if not isinstance(discovery, Mapping):
        raise PowdrrExecutionError("Validation gate discovery must be an object.")
    configured_discovery_action = discovery.get("action")
    if configured_discovery_action is not None and not isinstance(
        configured_discovery_action, Mapping
    ):
        raise PowdrrExecutionError(
            "Validation gate discovery.action must be an action object."
        )
    if configured_discovery_action is None and not isinstance(
        discovery.get("input_ref"), str
    ):
        raise PowdrrExecutionError(
            "Validation gate discovery must declare an action or input_ref."
        )
    obligation_config = config.get("obligations")
    if not isinstance(obligation_config, Mapping):
        raise PowdrrExecutionError("Validation gate obligations must be an object.")
    matches = _nested_value(
        result,
        str(obligation_config.get("source", discovery.get("result_path", "matches"))),
    )
    if not isinstance(matches, Sequence) or isinstance(
        matches, (str, bytes, bytearray)
    ):
        raise PowdrrExecutionError("Validation tool discovery did not return matches.")
    obligations: dict[str, _ValidationObligation] = {}
    filter_values = obligation_config.get("filter", {})
    if not isinstance(filter_values, Mapping):
        raise PowdrrExecutionError(
            "Validation gate obligations.filter must be an object."
        )
    id_path = obligation_config.get("id")
    action_path = obligation_config.get("action")
    if not isinstance(id_path, str) or not isinstance(action_path, str):
        raise PowdrrExecutionError(
            "Validation gate obligations must declare id and action projections."
        )
    for match in matches:
        if not isinstance(match, Mapping):
            continue
        if any(match.get(key) != expected for key, expected in filter_values.items()):
            continue
        raw_id = _nested_value(match, id_path)
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise PowdrrExecutionError(
                "Every discovered validation item must have an id."
            )
        normalized_id = raw_id.strip()
        if normalized_id in obligations:
            raise PowdrrExecutionError(
                "Validation discovery returned duplicate obligation id "
                f"{normalized_id!r}."
            )
        expected_action = _nested_value(match, action_path)
        if not isinstance(expected_action, Mapping):
            raise PowdrrExecutionError(
                f"Validation item {normalized_id!r} must provide action at "
                f"{action_path}."
            )
        obligations[normalized_id] = _ValidationObligation(
            obligation_id=normalized_id,
            expected_action=dict(expected_action),
            source=dict(match),
        )
    gate_state = _validation_gate_state(state, gate)
    gate_state.step_index = gate_step_index
    gate_state.discovered = True
    gate_state.epoch = 1
    gate_state.obligations = obligations
    gate_state.correction_required = False
    gate_state.discovery_action = (
        dict(discovery_action) if discovery_action is not None else None
    )
    output_name = discovery.get("output_ref")
    if isinstance(output_name, str) and output_name.strip():
        state.handoff_records[output_name.strip()] = {
            "name": output_name.strip(),
            "type": "any",
            "value": [
                {
                    "obligation_id": obligation.obligation_id,
                    "action": dict(obligation.expected_action),
                }
                for obligation in obligations.values()
            ],
            "produced_by": {
                "step_index": state.step_index,
                "action": "gather_context",
            },
            "scope": "skill",
        }


def _auto_register_validation_handoff(
    state: _WorkflowExecutionState,
    *,
    gate: Any,
    gate_step_index: int,
) -> bool:
    """Register a valid discovered-obligation handoff when resuming a workflow."""
    config = _validation_gate_config(gate)
    if config is None:
        return False
    discovery = config.get("discovery")
    if not isinstance(discovery, Mapping):
        return False
    input_ref = discovery.get("input_ref")
    if not isinstance(input_ref, str) or not input_ref.strip():
        return False
    record = state.handoff_records.get(input_ref.strip())
    if not isinstance(record, Mapping):
        return False
    raw_obligations = record.get("value")
    if not isinstance(raw_obligations, Sequence) or isinstance(
        raw_obligations, (str, bytes, bytearray)
    ):
        return False
    matches: list[dict[str, Any]] = []
    for item in raw_obligations:
        if not isinstance(item, Mapping):
            return False
        obligation_id = item.get("id")
        validation_action = item.get("validation_action")
        if not isinstance(obligation_id, str) or not obligation_id.strip():
            return False
        if not isinstance(validation_action, Mapping) or not validation_action:
            return False
        matches.append(
            {
                "section": "tools",
                "item": {
                    "id": obligation_id.strip(),
                    "validation_action": dict(validation_action),
                },
            }
        )
    _discover_validation_obligations(
        {"matches": matches},
        state=state,
        gate=gate,
        gate_step_index=gate_step_index,
        discovery_action={"kind": "handoff", "name": input_ref.strip()},
    )
    return True


def _register_validation_gate_discovery(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
) -> None:
    for gate_step_index, gate in enumerate(state.selected_skill.skill.steps):
        config = _validation_gate_config(gate)
        if gate_step_index <= state.step_index or config is None:
            continue
        discovery = config.get("discovery")
        if not isinstance(discovery, Mapping):
            continue
        discovery_action = discovery.get("action")
        input_ref = discovery.get("input_ref")
        matches_discovery = (
            isinstance(discovery_action, Mapping)
            and _action_template_matches(
                discovery_action, _workflow_action_data(action)
            )
        ) or (
            isinstance(input_ref, str)
            and input_ref.strip()
            and action.kind == "gather_context"
        )
        if not matches_discovery:
            continue
        gate_state = _validation_gate_state(state, gate)
        if not gate_state.discovered:
            event = state.execution_events[-1] if state.execution_events else None
            if not isinstance(event, Mapping) or not isinstance(
                event.get("result"), Mapping
            ):
                raise PowdrrExecutionError(
                    "Validation discovery action did not produce a result."
                )
            _discover_validation_obligations(
                event["result"],
                state=state,
                gate=gate,
                gate_step_index=gate_step_index,
                discovery_action=_workflow_action_data(action),
            )


def _validation_result_passed(
    result: Mapping[str, Any],
    gate: Any,
) -> bool:
    config = _validation_gate_config(gate) or {}
    success = config.get("success")
    if isinstance(success, Mapping):
        field = success.get("result_field")
        accepted = success.get("accepted_values")
        value = result.get(field) if isinstance(field, str) else None
        if isinstance(accepted, Sequence) and not isinstance(
            accepted, (str, bytes, bytearray)
        ):
            return value in accepted
    if result.get("returncode") not in (None, 0):
        return False
    if result.get("validation_successful") is False:
        return False
    return result.get("status") not in {"failed", "failure", "error"}


def _validation_issue_fingerprint(result: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a stable semantic fingerprint for a validation result.

    Validators commonly return YAML in ``stdout`` rather than structured JSON.
    Fingerprinting the parsed issue records lets us distinguish a real repair
    from formatting churn or an edit that merely changes the error wording.
    """
    payload: Any = result
    stdout = result.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        try:
            parsed = yaml.safe_load(stdout)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, Mapping):
            payload = parsed
    issues = payload.get("issues") if isinstance(payload, Mapping) else None
    if isinstance(issues, Sequence) and not isinstance(issues, (str, bytes, bytearray)):
        return tuple(
            sorted(
                json.dumps(issue, sort_keys=True, ensure_ascii=False, default=str)
                for issue in issues
            )
        )
    if _validation_result_passed(result, None):
        return ()
    return (
        json.dumps(
            {
                "returncode": result.get("returncode"),
                "stderr": result.get("stderr", ""),
                "stdout": result.get("stdout", ""),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ),
    )


def _validation_gate_incomplete(
    state: _WorkflowExecutionState,
    gate: Any,
) -> list[_ValidationObligation]:
    gate_state = _validation_gate_state(state, gate)
    return [
        obligation
        for obligation in gate_state.obligations.values()
        if obligation.epoch != gate_state.epoch or obligation.status != "passed"
    ]


def _validate_dynamic_validation_gate_action(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    step: Any,
) -> None:
    if not _validation_gate_enabled(step):
        return
    gate_state = _validation_gate_state(state, step)
    if not gate_state.discovered:
        if _auto_register_validation_handoff(
            state, gate=step, gate_step_index=state.step_index
        ):
            gate_state = _validation_gate_state(state, step)
        else:
            raise _WorkflowToolValidationError(
                ValidationError(
                    code="validation_discovery_required",
                    message=(
                        "Validation obligations have not been discovered in the "
                        "current "
                        "gate epoch. Do not invoke a validation command or invent a "
                        "discovery CLI command. Return to the configured discovery "
                        "step and run its exact gather_context action before invoking "
                        "any obligation."
                    ),
                    path="action",
                )
            )
    if state.step_index != gate_state.step_index:
        return
    if gate_state.correction_required and action.kind != "gather_context":
        raise _WorkflowToolValidationError(
            ValidationError(
                code="validation_correction_required",
                message=(
                    "A validation tool failed. Apply its corrective action before "
                    "running validation tools again."
                ),
                path="action",
            )
        )
    actual = {
        "kind": action.kind,
        "tool": action.tool,
        "parameters": dict(action.parameters),
    }
    if action.kind == "gather_context":
        return
    config = _validation_gate_config(step) or {}
    correction_actions = config.get(
        "correction_actions",
        ["edit", "yaml_edit", "file_management", "gather_context"],
    )
    if isinstance(correction_actions, Sequence) and action.kind in correction_actions:
        return
    if action.kind in {"next_step", "goto_step", "complete"}:
        return
    obligations = gate_state.obligations.values()
    if not any(
        _validation_actions_match(obligation.expected_action, actual)
        for obligation in obligations
    ):
        declared = "; ".join(
            json.dumps(obligation.expected_action, sort_keys=True)
            for obligation in gate_state.obligations.values()
        )
        raise _WorkflowToolValidationError(
            ValidationError(
                code="validation_action_not_discovered",
                message=(
                    "The action is not one of the discovered obligations. "
                    f"Use one of: {declared or 'none'}."
                ),
                path="action",
            )
        )


def _record_dynamic_validation_result(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
) -> None:
    if state.step_index >= len(state.selected_skill.skill.steps):
        return
    step = state.selected_skill.skill.steps[state.step_index]
    if not _validation_gate_enabled(step):
        return
    event = state.execution_events[-1] if state.execution_events else None
    if not isinstance(event, Mapping) or not isinstance(event.get("result"), Mapping):
        return
    actual = {
        "kind": action.kind,
        "tool": action.tool,
        "parameters": dict(action.parameters),
    }
    gate_state = _validation_gate_state(state, step)
    obligation = next(
        (
            item
            for item in gate_state.obligations.values()
            if _validation_actions_match(item.expected_action, actual)
        ),
        None,
    )
    if obligation is None:
        return
    result = dict(event["result"])
    obligation.attempts += 1
    obligation.last_result = result
    if _validation_result_passed(result, step):
        obligation.status = "passed"
        obligation.last_issue_fingerprint = ()
    else:
        obligation.status = "failed"
        fingerprint = _validation_issue_fingerprint(result)
        previous_fingerprint = obligation.last_issue_fingerprint
        previous_issues = set(previous_fingerprint or ())
        current_issues = set(fingerprint)
        if (
            previous_fingerprint == fingerprint
            or fingerprint in obligation.issue_history
            or (previous_issues and not current_issues < previous_issues)
        ):
            obligation.semantic_stalls += 1
        else:
            obligation.semantic_stalls = 0
        obligation.last_issue_fingerprint = fingerprint
        obligation.issue_history.add(fingerprint)
        gate_state.correction_required = True
        progress_note = (
            " The repair made no semantic improvement or repeated a prior "
            "validation state; choose a materially different correction."
            if obligation.semantic_stalls
            else ""
        )
        state.execution_context.append(
            "Validation failed for "
            f"{obligation.obligation_id}. Exact result: "
            f"{json.dumps(result, ensure_ascii=False, default=str)}. "
            "Apply the corrective action indicated by that result, then rerun every "
            "discovered validation obligation." + progress_note
        )


def _reset_validation_gate_after_correction(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
) -> None:
    if state.step_index >= len(state.selected_skill.skill.steps):
        return
    step = state.selected_skill.skill.steps[state.step_index]
    if not _validation_gate_enabled(step):
        return
    if action.kind not in {"edit", "yaml_edit"}:
        return
    gate_state = _validation_gate_state(state, step)
    if not gate_state.correction_required:
        return
    gate_state.epoch += 1
    gate_state.correction_required = False
    for obligation in gate_state.obligations.values():
        obligation.epoch = gate_state.epoch
        obligation.status = "pending"
        obligation.attempts = 0
        obligation.last_result = None
    state.execution_events.append(
        {
            "kind": "validation_gate_epoch",
            "gate_id": _validation_gate_id(step),
            "epoch": gate_state.epoch,
            "step_index": state.step_index,
            "reason": "correction_applied",
        }
    )


def _validation_gate_prompt_data(
    state: _WorkflowExecutionState,
) -> dict[str, Any] | None:
    if state.step_index >= len(state.selected_skill.skill.steps):
        return None
    step = state.selected_skill.skill.steps[state.step_index]
    if not _validation_gate_enabled(step):
        return None
    gate_state = _validation_gate_state(state, step)
    if not gate_state.discovered:
        return {"gate_id": _validation_gate_id(step), "discovered": False}
    incomplete = _validation_gate_incomplete(state, step)
    return {
        "gate_id": _validation_gate_id(step),
        "discovered": True,
        "epoch": gate_state.epoch,
        "correction_required": gate_state.correction_required,
        "can_advance": not gate_state.correction_required and not incomplete,
        "discovery_action": gate_state.discovery_action,
        "discovered_tools": [
            {
                "obligation_id": obligation.obligation_id,
                "action": dict(obligation.expected_action),
            }
            for obligation in gate_state.obligations.values()
        ],
        "obligations": [
            obligation.to_data() for obligation in gate_state.obligations.values()
        ],
        "repair_guidance": (
            "For every failed obligation, inspect the exact current file and validator "
            "result before editing. Use the reported issue path and a structural YAML "
            "operation, preserve valid fields, and combine independent fixes in one "
            "yaml_edit. Never repeat an operation or semantically equivalent operation "
            "that produced the same issue fingerprint. If the issue fingerprint is "
            "unchanged or worse, choose a different target or repair strategy; do not "
            "retry the same action. Rerun every obligation after any correction."
        ),
    }


def _validate_workflow_action_outputs(action: SkillChatAction, step: Any) -> None:
    if not action.outputs or not step.outputs:
        return
    declared_names = {output.name for output in step.outputs}
    unexpected = sorted(set(action.outputs) - declared_names)
    if unexpected:
        raise PowdrrExecutionError(
            "Workflow action outputs are not declared by the current step: "
            + ", ".join(unexpected)
        )
    declarations = {output.name: output for output in step.outputs}
    for name, value in action.outputs.items():
        declaration = declarations[name]
        if declaration.schema is None:
            continue
        schema_error = _json_schema_error(
            value,
            declaration.schema,
            path=f"outputs.{name}",
        )
        if schema_error is not None:
            raise PowdrrExecutionError(
                f"Workflow action output does not match its declared schema: "
                f"{schema_error}"
            )


def _json_schema_error(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str,
) -> str | None:
    """Validate the strict JSON-schema subset used by workflow handoffs."""
    expected_type = schema.get("type")
    type_matches = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray)),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected_type, str) and not type_matches.get(expected_type, False):
        return f"{path} must be {expected_type}."
    enum = schema.get("enum")
    if isinstance(enum, Sequence) and value not in enum:
        return f"{path} must be one of {list(enum)!r}."
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            return f"{path} schema properties must be an object."
        required = schema.get("required", ())
        required_names = (
            tuple(required)
            if isinstance(required, Sequence)
            and not isinstance(required, (str, bytes, bytearray))
            else ()
        )
        missing = sorted(str(name) for name in required_names if name not in value)
        if missing:
            return f"{path} is missing required properties: {', '.join(missing)}."
        if schema.get("additionalProperties") is False:
            unknown = sorted(str(name) for name in set(value) - set(properties))
            if unknown:
                return f"{path} has unknown properties: {', '.join(unknown)}."
        for name, item in value.items():
            item_schema = properties.get(name)
            if not isinstance(item_schema, Mapping):
                continue
            error = _json_schema_error(item, item_schema, path=f"{path}.{name}")
            if error is not None:
                return error
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            return f"{path} must contain at least {min_items} items."
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                error = _json_schema_error(item, item_schema, path=f"{path}[{index}]")
                if error is not None:
                    return error
    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            return f"{path} must contain at least {min_length} characters."
    return None


def _parse_action_response_with_schema(
    payload: dict[str, Any],
    *,
    schema: Mapping[str, Any],
) -> SkillChatAction:
    schema_error = _json_schema_error(payload, schema, path="response")
    if schema_error is not None:
        raise PowdrrExecutionError(
            f"Workflow action response does not match the active schema: {schema_error}"
        )
    return _parse_action_response(payload)


def _record_workflow_action_outputs(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    step: Any,
) -> None:
    if not action.outputs:
        return
    declarations = {output.name: output for output in step.outputs}
    for name, value in action.outputs.items():
        declaration = declarations.get(name)
        state.handoff_records[name] = {
            "name": name,
            "type": declaration.type if declaration is not None else "any",
            "value": value,
            "produced_by": {
                "step_index": state.step_index,
                "action": action.kind,
            },
            "scope": declaration.scope if declaration is not None else "skill",
        }


def _record_runtime_readiness_artifact(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    step: Any,
) -> None:
    """Replace model-asserted readiness with a runtime-evaluated artifact."""
    if "readiness_report" not in action.outputs or state.runtime is None:
        return
    report_data = _runtime_readiness_report(state.runtime)
    record = state.handoff_records.get("readiness_report")
    if record is not None:
        record["value"] = report_data
        record["produced_by"] = {
            "step_index": state.step_index,
            "action": "runtime_readiness_evaluation",
        }
    if not report_data["ready"]:
        return
    content_ref = (
        "readiness:"
        + hashlib.sha256(
            json.dumps(report_data, sort_keys=True).encode("utf-8")
        ).hexdigest()
    )
    artifact = ExecutionArtifact(
        artifact_id="readiness-report-" + content_ref[-16:],
        artifact_type="readiness_report",
        schema_version="readiness-report-v1",
        owner_persona_id="code_reviewer",
        content_ref=content_ref,
        accepted=True,
    )
    state.runtime.record_artifact(artifact)


def _materialize_split_pr_specification(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    step: Any,
) -> None:
    """Compile validated split decisions into the proposed-PR document."""
    allocation = action.outputs.get("effect_allocation")
    if allocation is None or step.id != "allocate-proposed-pr-effects":
        return
    if not isinstance(allocation, Mapping):
        raise PowdrrExecutionError("effect_allocation must be a JSON object.")
    plan_record = state.handoff_records.get("proposed_pr_plan")
    effects_record = state.handoff_records.get("authoritative_effects")
    plan = plan_record.get("value") if isinstance(plan_record, Mapping) else None
    effects = (
        effects_record.get("value") if isinstance(effects_record, Mapping) else None
    )
    if not isinstance(plan, Mapping) or not isinstance(effects, Mapping):
        raise PowdrrExecutionError(
            "Cannot compile effect_allocation without validated proposed_pr_plan "
            "and authoritative_effects handoffs."
        )
    target = state.current_file_path
    if target is None or target.name != "proposed-pr-specification.yaml":
        candidates = tuple(state.worktree_root.rglob("proposed-pr-specification.yaml"))
        target = candidates[0] if len(candidates) == 1 else None
    if target is None:
        raise PowdrrExecutionError(
            "Cannot materialize effect_allocation without the proposed PR "
            "specification path in current file context."
        )
    try:
        compiled = compile_split_pr_specification(
            plan,
            allocation,
            effects,
            work_item_name=target.parent.name,
            repo_root=state.worktree_root,
            file_path=target,
        )
    except ValueError as exc:
        raise PowdrrExecutionError(str(exc)) from exc
    updated_text = (
        "# This file is read-only and should never be edited by a tool or agent.\n"
        + yaml.safe_dump(compiled, sort_keys=False)
    )
    _validate_structured_document_text(target, updated_text)
    runtime = _ensure_execution_runtime(state)
    with runtime.without_action_contract():
        invoke_file_mutation(
            (_worktree_relative_path(target, state.worktree_root),),
            worktree_root=state.worktree_root,
            executor=lambda: target.write_text(updated_text, encoding="utf-8"),
            runtime=runtime,
        )
    state.current_file_path = target


def _reset_split_pr_handoffs_for_repair(
    state: _WorkflowExecutionState,
    *,
    skill_name: str,
    target_step_id: str | None,
) -> None:
    if skill_name != "start-implementing-feature":
        return
    if target_step_id == "plan-proposed-pr-specification":
        for name in (
            "proposed_pr_plan",
            "authoritative_effects",
            "effect_allocation",
        ):
            state.handoff_records.pop(name, None)
        effect_step_index = _step_index_by_id(
            state.selected_skill, "load-authoritative-pr-effects"
        )
        _invalidate_deterministic_pre_step(
            state.execution_events,
            skill_name=skill_name,
            step_index=effect_step_index,
        )
    elif target_step_id == "allocate-proposed-pr-effects":
        state.handoff_records.pop("effect_allocation", None)


def _runtime_readiness_report(runtime: ExecutionRuntime) -> dict[str, Any]:
    report = runtime.publish_readiness(required_artifact_types=())
    return {
        "ready": report.ready,
        "reasons": list(report.reasons),
        "satisfied_requirements": list(getattr(report, "satisfied_requirements", ())),
    }


def _record_runtime_readiness_from_pre_step(
    step: Any,
    runtime: ExecutionRuntime,
) -> None:
    if not any(output.name == "readiness_report" for output in step.outputs):
        return
    report_data = _runtime_readiness_report(runtime)
    if not report_data["ready"]:
        return
    content_ref = (
        "readiness:"
        + hashlib.sha256(
            json.dumps(report_data, sort_keys=True).encode("utf-8")
        ).hexdigest()
    )
    runtime.record_artifact(
        ExecutionArtifact(
            artifact_id="readiness-report-" + content_ref[-16:],
            artifact_type="readiness_report",
            schema_version="readiness-report-v1",
            owner_persona_id="code_reviewer",
            content_ref=content_ref,
            accepted=True,
        )
    )


def _workflow_context_handoff_records(
    workflow_context: WorkflowContext | None,
) -> dict[str, dict[str, Any]]:
    if workflow_context is None:
        return {}
    records: dict[str, dict[str, Any]] = {}
    for name, value in workflow_context.to_data().items():
        if value is None:
            continue
        value_type = (
            "path"
            if name == "worktree_root"
            else "integer"
            if isinstance(value, int) and not isinstance(value, bool)
            else "string"
            if isinstance(value, str)
            else "any"
        )
        records[name] = {
            "name": name,
            "type": value_type,
            "value": value,
            "produced_by": {"source": "workflow_context"},
            "source": "workflow_context",
            "scope": "skill",
        }
    return records


def _validate_workflow_handoff(
    current_step: Any,
    next_step: Any,
    records: Mapping[str, Mapping[str, Any]],
    *,
    current_step_index: int,
) -> None:
    def is_current_step_record(record: Mapping[str, Any]) -> bool:
        producer = record.get("produced_by")
        return (
            isinstance(producer, Mapping)
            and producer.get("step_index") == current_step_index
        )

    def is_prior_step_record(record: Mapping[str, Any]) -> bool:
        producer = record.get("produced_by")
        return (
            isinstance(producer, Mapping)
            and isinstance(producer.get("step_index"), int)
            and producer["step_index"] < current_step_index
        )

    required_outputs = {
        output.name for output in current_step.outputs if output.required_for_next_step
    }
    missing_outputs = sorted(
        name
        for name in required_outputs
        if name not in records or not is_current_step_record(records[name])
    )
    if missing_outputs:
        raise PowdrrExecutionError(
            "Cannot advance: the current step has not produced required outputs: "
            + ", ".join(missing_outputs)
        )
    if next_step is None:
        return
    missing_inputs = []
    for input_spec in next_step.inputs:
        record = records.get(input_spec.name)
        if not input_spec.required and record is None:
            continue
        if (
            record is None
            or (
                input_spec.source == "previous_step"
                and not (is_current_step_record(record) or is_prior_step_record(record))
            )
            or (
                input_spec.source == "workflow_context"
                and record.get("source") != "workflow_context"
                and not (is_current_step_record(record) or is_prior_step_record(record))
            )
        ):
            missing_inputs.append(input_spec.name)
    if missing_inputs:
        raise PowdrrExecutionError(
            "Cannot advance: the next step is missing required inputs: "
            + ", ".join(missing_inputs)
            + "; available handoffs: "
            + ", ".join(sorted(str(name) for name in records))
        )
    mismatched = []
    for input_spec in next_step.inputs:
        record = records.get(input_spec.name)
        if record is None or input_spec.type == "any":
            continue
        if record.get("type") != input_spec.type:
            mismatched.append(
                f"{input_spec.name} (expected {input_spec.type}, "
                f"got {record.get('type')})"
            )
    if mismatched:
        raise PowdrrExecutionError(
            "Cannot advance: handoff input types do not match: " + ", ".join(mismatched)
        )


def _predicated_step_complete(step: Any, state: _WorkflowExecutionState) -> bool:
    completion = getattr(step, "completion", None)
    if completion is None:
        return False
    for output_name in completion.required_outputs:
        record = state.handoff_records.get(output_name)
        producer = record.get("produced_by") if isinstance(record, Mapping) else None
        if (
            not isinstance(producer, Mapping)
            or producer.get("step_index") != state.step_index
        ):
            return False
    for requirement in getattr(completion, "required_actions", ()):
        events = _predicated_action_evidence(requirement, state)
        if requirement.targets_from is not None:
            targets = _resolve_predicated_targets(requirement.targets_from, state)
            if any(
                not any(
                    event.get(requirement.match_field) == target for event in events
                )
                for target in targets
            ):
                return False
        elif not events:
            return False
        if requirement.exactly is not None and len(events) != requirement.exactly:
            return False
    return True


def _predicated_action_evidence(
    requirement: Any, state: _WorkflowExecutionState
) -> list[Mapping[str, Any]]:
    parameters = requirement.parameters or {}
    return [
        event
        for event in state.execution_events
        if event.get("step_index") == state.step_index
        and event.get("kind") == requirement.action
        and all(
            _predicated_parameter_matches(event.get(name), value, name)
            for name, value in parameters.items()
        )
    ]


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


def _resolve_predicated_targets(path: str, state: _WorkflowExecutionState) -> list[Any]:
    """Resolve a small deterministic handoff path such as ``x.items[*].file``."""
    segments = path.split(".")
    if not segments or segments[0] not in state.handoff_records:
        return []
    values: list[Any] = [state.handoff_records[segments[0]].get("value")]
    for segment in segments[1:]:
        wildcard = segment.endswith("[*]")
        key = segment[:-3] if wildcard else segment
        next_values: list[Any] = []
        for value in values:
            if key and isinstance(value, Mapping) and key in value:
                nested = value[key]
                if (
                    wildcard
                    and isinstance(nested, Sequence)
                    and not isinstance(nested, (str, bytes, bytearray))
                ):
                    next_values.extend(nested)
                else:
                    next_values.append(nested)
            elif (
                wildcard
                and isinstance(value, Sequence)
                and not isinstance(value, (str, bytes, bytearray))
            ):
                next_values.extend(value)
        values = next_values
    flattened: list[Any] = []
    for value in values:
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            flattened.extend(value)
        else:
            flattened.append(value)
    return flattened


def _advance_predicated_step(
    state: _WorkflowExecutionState,
    current_step: Any,
) -> None:
    next_index = state.step_index + 1
    next_step = (
        state.selected_skill.skill.steps[next_index]
        if next_index < len(state.selected_skill.skill.steps)
        else None
    )
    _validate_workflow_handoff(
        current_step,
        next_step,
        state.handoff_records,
        current_step_index=state.step_index,
    )
    state.execution_events.append(
        {
            "kind": "predicated_advance",
            "step_index": state.step_index,
            "required_outputs": list(current_step.completion.required_outputs),
        }
    )
    state.step_index = next_index


def _validate_workflow_step_transition(
    action: SkillChatAction,
    step: Any,
    execution_events: Sequence[Mapping[str, Any]],
    current_step_index: int,
    state: _WorkflowExecutionState | None = None,
) -> None:
    """Prevent the LLM from skipping a step's required tool invocation."""
    behavior = behavior_for_step(step)
    if state is not None and behavior.is_predicated:
        for requirement in getattr(
            getattr(step, "completion", None), "required_actions", ()
        ):
            if (
                action.kind == requirement.action
                and requirement.exactly is not None
                and len(_predicated_action_evidence(requirement, state))
                >= requirement.exactly
            ):
                raise PowdrrExecutionError(
                    f"Predicated step permits at most {requirement.exactly} "
                    f"{requirement.action} action(s) matching its required parameters."
                )
    if action.kind == "emit_outputs":
        if not behavior.is_predicated:
            raise PowdrrExecutionError(
                "emit_outputs is valid only for predicated steps."
            )
        if not action.outputs:
            raise PowdrrExecutionError(
                "emit_outputs must include at least one declared output."
            )
        if (
            state is not None
            and not _predicated_step_complete(step, state)
            and getattr(step, "completion", None) is not None
        ):
            missing = [
                requirement.targets_from or requirement.action
                for requirement in getattr(step.completion, "required_actions", ())
                if (
                    not _predicated_action_evidence(requirement, state)
                    or (
                        requirement.exactly is not None
                        and len(_predicated_action_evidence(requirement, state))
                        > requirement.exactly
                    )
                )
                or (
                    requirement.targets_from is not None
                    and not all(
                        any(
                            event.get(requirement.match_field) == target
                            for event in _predicated_action_evidence(requirement, state)
                        )
                        for target in _resolve_predicated_targets(
                            requirement.targets_from, state
                        )
                    )
                )
            ]
            if missing:
                raise PowdrrExecutionError(
                    "Cannot emit_outputs until required action evidence is recorded: "
                    + ", ".join(missing)
                )
    if behavior.rejects_model_transition(action.kind):
        raise PowdrrExecutionError(
            "Predicated steps advance automatically when their completion "
            "predicate is satisfied; next_step is not a valid model action."
        )
    if action.kind not in {"next_step", "goto_step", "complete"}:
        return
    if action.kind == "next_step":
        next_step_override = getattr(step, "next_step_override", None)
        if next_step_override:
            if state is None:
                raise PowdrrExecutionError(
                    "Workflow next_step_override requires the current skill state."
                )
            target_index = _step_index_by_id(state.selected_skill, next_step_override)
            if target_index >= current_step_index:
                raise PowdrrExecutionError(
                    "Workflow next_step_override must target a prior step; "
                    f"{next_step_override!r} is not before step {current_step_index}."
                )
    if action.kind == "goto_step":
        if state is None:
            raise PowdrrExecutionError(
                "Workflow goto_step validation requires the current skill state."
            )
        target_index = _step_index_by_id(state.selected_skill, action.step_id)
        if target_index >= current_step_index:
            raise PowdrrExecutionError(
                "Workflow goto_step may target only a prior step in the current "
                f"skill; {action.step_id!r} is not before step "
                f"{current_step_index}."
            )
    if action.kind == "complete":
        if state is None:
            raise PowdrrExecutionError(
                "Workflow complete validation requires the current skill state."
            )
        later_gates = tuple(
            step.id or f"index {index}"
            for index, step in enumerate(state.selected_skill.skill.steps)
            if index > current_step_index and behavior_for_step(step).runs_gate
        )
        if later_gates:
            raise PowdrrExecutionError(
                "Cannot complete the skill while later gate steps remain: "
                + ", ".join(later_gates)
            )
    if _validation_gate_enabled(step):
        gate_state = _validation_gate_state(state, step) if state is not None else None
        if gate_state is None or not gate_state.discovered:
            raise _WorkflowToolValidationError(
                ValidationError(
                    code="validation_obligations_not_discovered",
                    message=(
                        "Validation obligations have not been discovered; the "
                        "validation gate cannot be skipped."
                    ),
                    path="kind",
                )
            )
        if gate_state.correction_required:
            raise _WorkflowToolValidationError(
                ValidationError(
                    code="validation_correction_required",
                    message=(
                        "A validation obligation failed. Apply corrective steps "
                        "and rerun every discovered obligation before advancing."
                    ),
                    path="kind",
                )
            )
        assert state is not None
        incomplete = _validation_gate_incomplete(state, step)
        if incomplete:
            raise _WorkflowToolValidationError(
                ValidationError(
                    code="validation_tool_obligations_incomplete",
                    message=(
                        "Every discovered validation obligation must pass before "
                        "advancing. "
                        "Still pending: "
                        + ", ".join(item.obligation_id for item in incomplete)
                    ),
                    path="kind",
                )
            )
    invocations = tuple(
        invocation for invocation in step.tool_invocations if invocation.tool != "ref"
    )
    if not invocations:
        return
    # Shell invocations are the externally visible commands that can mutate
    # the branch. Internal inspection and validator actions retain their
    # existing corrective-action behavior; a shell command in the same step
    # remains the transition gate.
    required_invocations = tuple(
        invocation for invocation in invocations if invocation.tool == "shell"
    )
    if not required_invocations:
        return

    def successful_event_matches(invocation: Any, event: Mapping[str, Any]) -> bool:
        if event.get("kind") != "invoke_tool":
            return False
        event_tool = event.get("tool")
        if event_tool not in {None, invocation.tool} and not (
            event_tool == _INTERNAL_TOOL and invocation.tool == "shell"
        ):
            return False
        if event.get("step_index") != current_step_index:
            return False
        result = event.get("result")
        if isinstance(result, Mapping) and result.get("returncode") not in (None, 0):
            return False
        parameters = event.get("parameters")
        if isinstance(parameters, Mapping) and parameters.get("help") is True:
            return False
        if not isinstance(parameters, Mapping) or parameters.get("command") is None:
            return True
        try:
            command_items = _command_items_for_validation(parameters["command"])
        except RuntimeError:
            return False
        return _command_matches_invocation(command_items, invocation.command)

    if any(
        any(successful_event_matches(invocation, event) for event in execution_events)
        for invocation in required_invocations
    ):
        return
    expected = ", ".join(invocation.tool for invocation in required_invocations)
    raise _WorkflowToolValidationError(
        ValidationError(
            code="workflow_step_tool_required",
            message=(
                f"The current step requires a successful tool invocation before "
                f"{action.kind}: {expected}. Current step index: "
                f"{current_step_index}. Invoke the declared tool and wait for its "
                "result before advancing."
            ),
            path="kind",
        )
    )


def _validate_workflow_action_for_step_unwrapped(
    action: SkillChatAction,
    step: Any,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
    observer_allowed_action: ObserverActionRecommendation | None = None,
) -> None:
    """Validate a tool action while preserving the original error wording."""
    allowed_actions = _declared_action_names(
        step, execution_events=execution_events, step_index=step_index
    )
    if (
        allowed_actions is not None
        and action.kind not in allowed_actions
        and action.kind not in {"prompt_user", "next_step"}
        and not observer_action_matches(action, observer_allowed_action)
        and not (
            action.kind == "complete" and not getattr(step, "actions_declared", False)
        )
        and not (
            action.kind == "invoke_tool"
            and action.tool == "internal"
            and not getattr(step, "actions_declared", False)
        )
    ):
        raise _WorkflowToolValidationError(
            ValidationError(
                code="workflow_action_not_allowed",
                message=(
                    f"The {action.kind} action is not allowed in this step. "
                    + (
                        "This step explicitly supports: none; next_step is implicit."
                        if (
                            allowed_actions == ("next_step",)
                            or (
                                action.kind == "invoke_tool"
                                and action.tool == "shell"
                                and not getattr(step, "tool_invocations", ())
                            )
                        )
                        else "Use one of: " + ", ".join(allowed_actions) + "."
                    )
                ),
                path="kind",
            )
        )
    if (
        action.kind == "invoke_tool"
        and action.parameters.get("help") is True
        and action.tool in BUILTIN_TOOL_NAMES
    ):
        return
    supported_invocations = tuple(
        invocation
        for invocation in (
            tuple(step.tool_invocations)
            + _recovery_tool_invocations(step, execution_events, step_index)
        )
        if invocation.tool != "ref"
    )
    if action.tool == _INTERNAL_TOOL:
        internal_invocations = tuple(
            invocation
            for invocation in supported_invocations
            if invocation.tool in {_INTERNAL_TOOL, "shell"}
        )
        if not internal_invocations:
            supported_tools = sorted(
                {invocation.tool for invocation in supported_invocations}
            )
            supported_tools_text = ", ".join(supported_tools) or "none"
            raise PowdrrExecutionError(
                "The internal tool is not declared by the current workflow step. "
                f"The step explicitly supports: {supported_tools_text}."
            )
        _validate_internal_command(action.parameters.get("command"))
        command_items = _command_items_for_validation(action.parameters.get("command"))
        if not any(
            _command_matches_invocation(command_items, invocation.command)
            for invocation in internal_invocations
        ):
            declared = "; ".join(
                " ".join(invocation.command) for invocation in internal_invocations
            )
            raise PowdrrExecutionError(
                "The internal command is not declared by the current workflow "
                f"step. Use one of: {declared}."
            )
        return
    if action.tool == "shell":
        command_items = _command_items_for_validation(action.parameters.get("command"))
        if command_items == ["git", "diff", "--cached", "--name-only"] and any(
            invocation.tool == GIT_TOOL and invocation.operation == "add"
            for invocation in supported_invocations
        ):
            return
    matching_invocations = tuple(
        invocation
        for invocation in supported_invocations
        if invocation.tool == action.tool
    )
    if action.tool in {GIT_TOOL, GH_TOOL}:
        operation = action.parameters.get("operation")
        if any(
            invocation.operation == operation
            for invocation in matching_invocations
            if invocation.operation is not None
        ):
            return
        intrinsic_items = intrinsic_command(action.parameters, tool=action.tool)
        if not matching_invocations or any(
            _intrinsic_command_matches(intrinsic_items, invocation.command)
            for invocation in matching_invocations
        ):
            return
    if action.tool in {"basedpyright-symbol", "basedpyright-structure"}:
        # BasedPyright is a structured builtin. New declarations use an
        # operation discriminator so these calls cannot be confused with shell
        # command templates; legacy command declarations remain compatible.
        declared_operations = {
            invocation.operation
            for invocation in matching_invocations
            if invocation.operation is not None
        }
        if not matching_invocations:
            supported_tools = sorted(
                {invocation.tool for invocation in supported_invocations}
            )
            supported_tools_text = ", ".join(supported_tools) or "none"
            raise PowdrrExecutionError(
                f"Tool {action.tool!r} is not supported by the current workflow "
                f"step. The step explicitly supports: {supported_tools_text}."
            )
        if declared_operations:
            operation = action.parameters.get("operation")
            if operation not in declared_operations:
                expected = ", ".join(sorted(declared_operations))
                raise PowdrrExecutionError(
                    f"Tool {action.tool!r} requires one of the declared intrinsic "
                    f"operations: {expected}."
                )
        return
    if not matching_invocations:
        supported_tools = sorted(
            {invocation.tool for invocation in supported_invocations}
        )
        supported_tools_text = ", ".join(supported_tools) or "none"
        raise PowdrrExecutionError(
            f"Tool {action.tool!r} is not supported by the current workflow step. "
            f"The step explicitly supports: {supported_tools_text}."
        )
    command = (
        intrinsic_command(action.parameters, tool=action.tool)
        if action.tool in {GIT_TOOL, GH_TOOL}
        else action.parameters.get("command")
    )
    command_items = _command_items_for_validation(command)
    if any(
        _command_matches_invocation(command_items, invocation.command)
        for invocation in matching_invocations
    ):
        return
    expected_commands = "; ".join(
        " ".join(invocation.command) for invocation in matching_invocations
    )
    raise PowdrrExecutionError(
        f"Tool {action.tool!r} command {' '.join(command_items)!r} does not match "
        f"the command shape explicitly supported by the current workflow step: "
        f"{expected_commands}."
    )


def _command_items_for_validation(command: object) -> list[str]:
    if isinstance(command, str):
        try:
            command_items = shlex.split(command)
        except ValueError as exc:
            raise PowdrrExecutionError(
                "Workflow tool command is not valid shell syntax."
            ) from exc
    elif isinstance(command, Sequence) and not isinstance(
        command, (str, bytes, bytearray)
    ):
        command_items = list(command)
    else:
        raise PowdrrExecutionError("Workflow tool action must include a command.")
    if not command_items or any(
        not isinstance(item, str) or not item for item in command_items
    ):
        raise PowdrrExecutionError(
            "Workflow tool action command must contain non-empty strings."
        )
    return command_items


def _command_matches_invocation(
    command: Sequence[str],
    expected_command: Sequence[str],
) -> bool:
    actual_index = 0
    for expected_index, expected in enumerate(expected_command):
        if expected == "<files-to-publish>":
            return expected_index == len(expected_command) - 1 and actual_index < len(
                command
            )
        if actual_index >= len(command):
            return False
        if not _command_token_matches(command[actual_index], expected):
            return False
        actual_index += 1
    return actual_index == len(command)


def _intrinsic_command_matches(
    command: Sequence[str], expected_command: Sequence[str]
) -> bool:
    """Allow structured intrinsic operations to omit optional CLI flags."""
    if len(command) > len(expected_command):
        return False
    return all(
        _command_token_matches(actual, expected)
        for actual, expected in zip(command, expected_command, strict=True)
    )


def _command_token_matches(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    placeholder_matches = list(re.finditer(r"<[^<>]+>", expected))
    if not placeholder_matches:
        return False
    if len(placeholder_matches) == 1 and placeholder_matches[0].span() == (
        0,
        len(expected),
    ):
        # A command-template argument such as <populated-pr-description> can
        # intentionally contain spaces and newlines.  The command parser keeps
        # that value as one argv item when the LLM returns an array (or quotes
        # it in a shell command), so validating it as a non-whitespace token
        # incorrectly rejects otherwise valid commands.
        return bool(actual)
    pattern_parts: list[str] = []
    previous_end = 0
    for placeholder_match in placeholder_matches:
        pattern_parts.append(
            re.escape(expected[previous_end : placeholder_match.start()])
        )
        # One argv item may contain spaces when a placeholder is embedded in a
        # token (for example ``verification-command=<command>``).
        pattern_parts.append(r".+?")
        previous_end = placeholder_match.end()
    pattern_parts.append(re.escape(expected[previous_end:]))
    return re.fullmatch("".join(pattern_parts), actual) is not None
