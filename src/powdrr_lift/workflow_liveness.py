"""Static capability effects used by workflow-definition liveness checks."""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class CapabilityEffect:
    """Conservative, execution-free summary of one bounded capability."""

    operation: str
    determinism: str
    idempotence: str
    reads: frozenset[str]
    writes: frozenset[str]
    produces: frozenset[str] = frozenset()
    invalidates: frozenset[str] = frozenset()
    success_postconditions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StepControlContract:
    """Normalized, execution-free control contract for one workflow step."""

    index: int
    step_id: str
    owner: Literal["runner", "llm"]
    actions: tuple[str, ...]
    entry_requirements: tuple[str, ...]
    completion_requirements: tuple[str, ...]
    success_transition: tuple[int, ...]
    failure_transitions: tuple[int, ...]
    successors: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AbstractWorkflowState:
    """Finite abstract state used to reason about repeated transitions."""

    step_index: int
    successful_actions: frozenset[str] = frozenset()
    satisfied_conditions: frozenset[str] = frozenset()
    available_outputs: frozenset[str] = frozenset()
    changed_domains: frozenset[str] = frozenset()
    validation_epoch: int = 0
    bounded_iterations: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class AbstractTransition:
    source: AbstractWorkflowState
    target: AbstractWorkflowState
    outcome: Literal[
        "success_with_progress",
        "success_without_progress",
        "correctable_failure",
        "terminal_failure",
        "transition",
    ]
    progress: bool
    description: str


def effect_registry() -> Mapping[tuple[str, str], CapabilityEffect]:
    """Return the immutable-by-convention registry used by static analysis."""
    return _EFFECTS


_EFFECTS: dict[tuple[str, str], CapabilityEffect] = {
    ("git", "status"): CapabilityEffect(
        "status",
        "deterministic",
        "idempotent",
        frozenset({"repository"}),
        frozenset(),
        frozenset({"tool_result"}),
        success_postconditions=("repository_observed",),
    ),
    ("git", "add"): CapabilityEffect(
        "add",
        "deterministic",
        "idempotent",
        frozenset({"files"}),
        frozenset({"repository_index"}),
        success_postconditions=("paths_staged",),
    ),
    ("git", "move"): CapabilityEffect(
        "move",
        "deterministic",
        "conditional",
        frozenset({"files", "repository_index"}),
        frozenset({"files", "repository_index"}),
        success_postconditions=("paths_moved",),
    ),
    ("git", "commit"): CapabilityEffect(
        "commit",
        "conditional",
        "non_idempotent",
        frozenset({"repository_index"}),
        frozenset({"repository_history"}),
        success_postconditions=("commit_created",),
    ),
    ("git", "push"): CapabilityEffect(
        "push",
        "conditional",
        "conditional",
        frozenset({"repository_history"}),
        frozenset({"remote_repository"}),
        success_postconditions=("remote_updated",),
    ),
    ("internal", "repository-state"): CapabilityEffect(
        "repository-state",
        "deterministic",
        "idempotent",
        frozenset({"repository"}),
        frozenset(),
        frozenset({"repository_state"}),
        success_postconditions=("repository_observed",),
    ),
    ("internal", "read_document"): CapabilityEffect(
        "read_document",
        "deterministic",
        "idempotent",
        frozenset({"files"}),
        frozenset(),
        frozenset({"context"}),
        success_postconditions=("document_read",),
    ),
    ("internal", "gather_context"): CapabilityEffect(
        "gather_context",
        "deterministic",
        "idempotent",
        frozenset({"specifications"}),
        frozenset(),
        frozenset({"context"}),
        success_postconditions=("context_gathered",),
    ),
    ("internal", "validation"): CapabilityEffect(
        "validation",
        "conditional",
        "idempotent",
        frozenset({"files"}),
        frozenset({"validation"}),
        frozenset({"validation_result"}),
        success_postconditions=("validation_recorded",),
    ),
    ("internal", "prompt_user"): CapabilityEffect(
        "prompt_user",
        "conditional",
        "non_idempotent",
        frozenset(),
        frozenset({"human_input"}),
        frozenset({"human_input"}),
        success_postconditions=("human_input_received",),
    ),
    ("gh", "pr_view"): CapabilityEffect(
        "pr_view",
        "conditional",
        "idempotent",
        frozenset({"remote_repository"}),
        frozenset(),
        frozenset({"context"}),
        success_postconditions=("pr_observed",),
    ),
    ("gh", "pr_diff"): CapabilityEffect(
        "pr_diff",
        "conditional",
        "idempotent",
        frozenset({"remote_repository"}),
        frozenset(),
        frozenset({"context"}),
        success_postconditions=("diff_observed",),
    ),
    ("gh", "pr_create"): CapabilityEffect(
        "pr_create",
        "conditional",
        "non_idempotent",
        frozenset({"files"}),
        frozenset({"remote_repository"}),
        frozenset({"pull_request"}),
        success_postconditions=("pr_created",),
    ),
    ("gh", "pr_edit"): CapabilityEffect(
        "pr_edit",
        "conditional",
        "conditional",
        frozenset({"remote_repository"}),
        frozenset({"remote_repository"}),
        frozenset({"pull_request"}),
        success_postconditions=("pr_updated",),
    ),
    ("fuzzy-match", "fuzzy-match"): CapabilityEffect(
        "fuzzy-match",
        "conditional",
        "idempotent",
        frozenset({"files"}),
        frozenset(),
        frozenset({"context"}),
        success_postconditions=("matches_found",),
    ),
    ("enrich", "enrich"): CapabilityEffect(
        "enrich",
        "deterministic",
        "idempotent",
        frozenset({"outputs"}),
        frozenset(),
        frozenset({"context"}),
        success_postconditions=("output_enriched",),
    ),
}


def capability_effect(invocation: Mapping[str, Any]) -> CapabilityEffect | None:
    """Return a bounded effect summary without executing the invocation."""
    tool = invocation.get("tool")
    if not isinstance(tool, str):
        return None
    operation = invocation.get("operation")
    if isinstance(operation, str):
        return _EFFECTS.get((tool, operation))
    command = invocation.get("command")
    if not isinstance(command, Sequence) or isinstance(
        command, (str, bytes, bytearray)
    ):
        return None
    if not command or not all(isinstance(item, str) for item in command):
        return None
    operation = command[0]
    if tool == "gh" and len(command) >= 2 and command[0] == "pr":
        operation = f"pr_{command[1]}"
    if tool == "fuzzy-match":
        operation = "fuzzy-match"
    if tool == "enrich":
        operation = "enrich"
    return _EFFECTS.get((tool, operation))


def is_fixed_deterministic(effect: CapabilityEffect | None) -> bool:
    return effect is not None and effect.determinism == "deterministic"


def is_idempotent(effect: CapabilityEffect | None) -> bool:
    return effect is not None and effect.idempotence == "idempotent"


def effect_for_pre_step(pre_step: Mapping[str, Any] | None) -> CapabilityEffect | None:
    """Resolve a parsed pre-step as a bounded capability when possible."""
    if not isinstance(pre_step, Mapping):
        return None
    template = pre_step.get("template")
    if not isinstance(template, Mapping):
        return None
    action = pre_step.get("action")
    if action == "invoke_tool":
        return capability_effect(template)
    if action == "gather_context":
        return _EFFECTS.get(("internal", "gather_context"))
    if action == "prompt_user":
        return _EFFECTS.get(("internal", "prompt_user"))
    if action == "read_document":
        return _EFFECTS.get(("internal", "read_document"))
    return None


def step_control_contracts(ir: Any) -> tuple[StepControlContract, ...]:
    """Compile the existing IR into the normalized ownership contract."""
    contracts: list[StepControlContract] = []
    for item in ir.steps:
        step = item.step
        owner: Literal["runner", "llm"] = (
            "runner"
            if step.step_type in {"invoke_tool", "gate", "uses_skill"}
            else "llm"
        )
        actions = tuple(step.actions)
        if step.step_type == "predicated":
            actions = tuple((*actions, "emit_outputs"))
        completion = ()
        if step.completion is not None:
            completion = tuple(step.completion.required_outputs)
        failure: tuple[int, ...] = ()
        if step.gate is not None:
            target = next(
                (
                    successor
                    for successor in item.successors
                    if successor != item.index + 1
                ),
                None,
            )
            failure = () if target is None else (target,)
        contracts.append(
            StepControlContract(
                item.index,
                item.step_id,
                owner,
                actions,
                tuple(input_item.name for input_item in step.inputs),
                completion,
                item.successors,
                failure,
                item.successors,
            )
        )
    return tuple(contracts)


def expand_abstract_transitions(
    ir: Any, state: AbstractWorkflowState
) -> tuple[AbstractTransition, ...]:
    """Expand one state without executing tools or consulting the environment."""
    if state.step_index < 0 or state.step_index >= len(ir.steps):
        return ()
    item = ir.steps[state.step_index]
    step = item.step
    raw_effects = [
        capability_effect(invocation.to_data()) for invocation in step.tool_invocations
    ]
    pre_effect = effect_for_pre_step(step.pre_step.to_data() if step.pre_step else None)
    effects: list[CapabilityEffect] = [
        effect for effect in (*raw_effects, pre_effect) if effect is not None
    ]
    writes = frozenset(domain for effect in effects for domain in effect.writes)
    produces = frozenset(value for effect in effects for value in effect.produces)
    outputs = frozenset(output.name for output in step.outputs)
    new_domains = writes - state.changed_domains
    material_produces = produces - {"tool_result", "context", "repository_state"}
    progress_actions = set(step.actions) - {
        "read_document",
        "list_files",
        "invoke_tool",
        "next_step",
        "goto_step",
    }
    progress = bool(new_domains or material_produces or outputs or progress_actions)
    action_ids = frozenset(
        f"{state.step_index}:{effect.operation}" for effect in effects
    )
    next_outputs = state.available_outputs | outputs
    next_domains = state.changed_domains | writes
    if not item.successors:
        return ()
    target_states = item.successors
    transitions: list[AbstractTransition] = []
    for successor in target_states:
        target = AbstractWorkflowState(
            successor,
            state.successful_actions | action_ids,
            state.satisfied_conditions,
            next_outputs,
            next_domains,
            state.validation_epoch + (1 if "validation" in writes else 0),
            state.bounded_iterations,
        )
        transitions.append(
            AbstractTransition(
                state,
                target,
                "success_with_progress" if progress else "success_without_progress",
                progress,
                f"{step.id or state.step_index} -> {successor}",
            )
        )
    return tuple(transitions)
