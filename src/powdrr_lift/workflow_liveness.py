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


@dataclass(frozen=True, slots=True)
class SkillEffectSummary:
    """Interprocedural summary used for nested-skill liveness checks."""

    name: str
    required_inputs: frozenset[str]
    produced_outputs: frozenset[str]
    reads: frozenset[str]
    writes: frozenset[str]
    may_prompt: bool
    may_return: bool
    may_fail: bool


def summarize_skill(skill: Any) -> SkillEffectSummary:
    required = frozenset(item.name for item in skill.inputs if item.required)
    outputs = frozenset(output.name for step in skill.steps for output in step.outputs)
    reads: set[str] = set()
    writes: set[str] = set()
    may_prompt = False
    may_fail = False
    for step in skill.steps:
        may_prompt |= "prompt_user" in step.actions
        may_fail |= step.gate is not None
        for action in step.actions:
            if action in {"read_document", "list_files", "gather_context"}:
                reads.add("files" if action != "gather_context" else "context")
            if action in {"edit", "yaml_edit", "file_management"}:
                writes.add("files")
        for invocation in step.tool_invocations:
            effect = capability_effect(invocation.to_data())
            if effect is not None:
                reads.update(effect.reads)
                writes.update(effect.writes)
        pre_effect = effect_for_pre_step(
            step.pre_step.to_data() if step.pre_step else None
        )
        if pre_effect is not None:
            reads.update(pre_effect.reads)
            writes.update(pre_effect.writes)
    return SkillEffectSummary(
        skill.name,
        required,
        outputs,
        frozenset(reads),
        frozenset(writes),
        may_prompt,
        True,
        may_fail,
    )


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

_DECLARED_UNKNOWN_EFFECT_TOOLS = frozenset(
    {"internal", "gh", "ref", "basedpyright-structure", "basedpyright-symbol"}
)


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
        if tool in _DECLARED_UNKNOWN_EFFECT_TOOLS:
            return CapabilityEffect(
                str(operation or "reference"),
                "unknown",
                "unknown",
                frozenset(),
                frozenset(),
                frozenset({"tool_result"}),
            )
        return None
    if not command or not all(isinstance(item, str) for item in command):
        return None
    operation = command[0]
    if tool == "internal" and command[0] == "powdrr-lift" and len(command) >= 2:
        operation = command[1]
    if tool == "shell":
        return _shell_capability_effect(command)
    if tool == "gh" and len(command) >= 2 and command[0] == "pr":
        operation = f"pr_{command[1]}"
    if tool == "fuzzy-match":
        operation = "fuzzy-match"
    if tool == "enrich":
        operation = "enrich"
    if tool == "internal" and operation == "repository-state":
        return CapabilityEffect(
            operation,
            "unknown",
            "unknown",
            frozenset({"repository"}),
            frozenset(),
            frozenset({"tool_result"}),
        )
    effect = _EFFECTS.get((tool, operation))
    if effect is not None:
        return effect
    if tool in _DECLARED_UNKNOWN_EFFECT_TOOLS:
        reads = frozenset({"remote_repository"}) if tool == "gh" else frozenset()
        writes = (
            frozenset({"validation"})
            if tool.startswith("basedpyright")
            or "evaluate" in operation
            or "validate" in operation
            else frozenset()
        )
        return CapabilityEffect(
            operation,
            "unknown",
            "unknown",
            reads,
            writes,
            frozenset({"tool_result"}),
        )
    return None


def _shell_capability_effect(command: Sequence[str]) -> CapabilityEffect | None:
    """Resolve the checked-in effect metadata for known shell commands.

    Shell remains an escape hatch: arbitrary commands intentionally return
    ``None`` and continue to produce an advisory diagnostic.  Repository
    definitions use a small, explicit set of read, validation, and GitHub
    operations; recording those effects here lets the analyzer reason about
    them without executing them.
    """
    if not command:
        return None
    executable = command[0]
    operation = executable
    writes: frozenset[str] = frozenset()
    reads: frozenset[str] = frozenset({"files"})
    # Shell commands are bounded by this registry, but their environment and
    # output are still not fixed enough to treat them as model-owned
    # deterministic actions.
    determinism = "unknown"
    idempotence = "idempotent"
    produces = frozenset({"tool_result"})

    if executable in {"rg", "ruff", "mypy", "pytest", "pyright", "basedpyright"}:
        operation = executable
        writes = frozenset({"validation"}) if executable != "rg" else frozenset()
        return CapabilityEffect(
            operation,
            "conditional" if writes else determinism,
            idempotence,
            reads,
            writes,
            produces,
            success_postconditions=("validation_recorded",) if writes else (),
        )
    if executable == "curl":
        return CapabilityEffect(
            "curl",
            "conditional",
            "idempotent",
            frozenset({"remote_repository"}),
            frozenset(),
            frozenset({"context", "tool_result"}),
        )
    if executable == "gh":
        operation = command[1] if len(command) > 1 else "gh"
        writes = frozenset({"remote_repository"}) if "POST" in command else frozenset()
        return CapabilityEffect(
            operation,
            "conditional",
            "non_idempotent" if writes else "idempotent",
            frozenset({"remote_repository"}),
            writes,
            frozenset({"context", "tool_result"}),
        )
    if executable == "git" and len(command) > 1:
        operation = command[1]
        if operation in {"status", "diff", "log", "blame"}:
            return CapabilityEffect(
                operation,
                determinism,
                idempotence,
                frozenset({"repository"}),
                frozenset(),
                frozenset({"context", "tool_result"}),
            )
        if operation == "add":
            return CapabilityEffect(
                operation,
                determinism,
                idempotence,
                frozenset({"files"}),
                frozenset({"repository_index"}),
                produces,
                success_postconditions=("paths_staged",),
            )
        if operation in {"fetch", "pull"}:
            return CapabilityEffect(
                operation,
                "conditional",
                "idempotent",
                frozenset({"remote_repository"}),
                frozenset({"repository"}),
                produces,
            )
        if operation == "merge":
            return CapabilityEffect(
                operation,
                "conditional",
                "conditional",
                frozenset({"repository"}),
                frozenset({"repository", "files"}),
                produces,
            )
        if operation == "commit":
            return CapabilityEffect(
                operation,
                "conditional",
                "non_idempotent",
                frozenset({"repository_index"}),
                frozenset({"repository_history"}),
                produces,
                success_postconditions=("commit_created",),
            )
        if operation == "push":
            return CapabilityEffect(
                operation,
                "conditional",
                "conditional",
                frozenset({"repository_history"}),
                frozenset({"remote_repository"}),
                produces,
            )
    return None


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


def runtime_static_conformance(ir: Any) -> tuple[str, ...]:
    """Compare static ownership with the production step behavior policy."""
    from powdrr_lift.workflow_step_behavior import behavior_for_step

    mismatches: list[str] = []
    for contract, item in zip(step_control_contracts(ir), ir.steps, strict=True):
        behavior = behavior_for_step(item.step)
        expected_owner = "runner" if not behavior.invokes_llm else "llm"
        if contract.owner != expected_owner:
            mismatches.append(
                f"{contract.step_id}: static owner {contract.owner} != runtime {expected_owner}"
            )
        if behavior.runs_gate != (item.step.step_type == "gate"):
            mismatches.append(f"{contract.step_id}: gate policy mismatch")
    return tuple(mismatches)


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
    if step.gate is not None and step.gate.goto_step:
        retry_target = next(
            (successor for successor in item.successors if successor != item.index + 1),
            None,
        )
        if retry_target is not None:
            retry_state = AbstractWorkflowState(
                retry_target,
                state.successful_actions,
                state.satisfied_conditions,
                state.available_outputs,
                state.changed_domains,
                state.validation_epoch,
                state.bounded_iterations,
            )
            transitions.append(
                AbstractTransition(
                    state,
                    retry_state,
                    "correctable_failure",
                    False,
                    f"{step.id or state.step_index} retry -> {retry_target}",
                )
            )
    if any(
        is_idempotent(effect)
        and f"{state.step_index}:{effect.operation}" in state.successful_actions
        for effect in effects
    ):
        transitions = [
            AbstractTransition(
                transition.source,
                transition.target,
                "success_without_progress",
                False,
                transition.description,
            )
            for transition in transitions
            if transition.outcome == "success_with_progress"
        ]
    return tuple(transitions)


def build_abstract_execution_graph(
    ir: Any, *, max_states: int = 4096
) -> Mapping[AbstractWorkflowState, tuple[AbstractTransition, ...]]:
    """Reach the fixed point of the finite abstract transition system."""
    initial = AbstractWorkflowState(0)
    graph: dict[AbstractWorkflowState, tuple[AbstractTransition, ...]] = {}
    queue = [initial]
    while queue and len(graph) < max_states:
        state = queue.pop(0)
        if state in graph:
            continue
        transitions = expand_abstract_transitions(ir, state)
        graph[state] = transitions
        queue.extend(
            transition.target
            for transition in transitions
            if transition.target not in graph
        )
    return graph
