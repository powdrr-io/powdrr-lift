"""Deterministic quality checks and prompt snapshots for workflow definitions."""

# The analyzer carries human-readable diagnostics whose wording is intentionally
# kept intact; long diagnostic strings are exempt from the repository line limit.
# ruff: noqa: E501

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core import (
    build_skill_validation_report,
    load_skill,
    resolve_repo_root,
)
from powdrr_lift.core.skill_specification import Skill, SkillStep, skill_step_from_data
from powdrr_lift.core.workflow_template_specification import (
    build_workflow_template_validation_report,
)
from powdrr_lift.workflow_liveness import (
    AbstractWorkflowState,
    capability_effect,
    effect_for_pre_step,
    expand_abstract_transitions,
    is_fixed_deterministic,
    is_idempotent,
)

_PLACEHOLDER = re.compile(r"<([A-Za-z0-9_-]+)>")
_ACTION_START = re.compile(r'\{\s*"action"\s*:')


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionIssue:
    code: str
    message: str
    path: str
    severity: str = "error"
    state: Mapping[str, Any] | None = None
    cycle: tuple[str, ...] = ()
    remediation: str | None = None

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
        }
        if self.state is not None:
            data["state"] = dict(self.state)
        if self.cycle:
            data["cycle"] = list(self.cycle)
        if self.remediation is not None:
            data["remediation"] = self.remediation
        return data


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionReport:
    definition: Path
    kind: str
    issues: tuple[WorkflowDefinitionIssue, ...]

    @property
    def validation_successful(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_data(self) -> dict[str, object]:
        return {
            "definition": str(self.definition),
            "kind": self.kind,
            "validation_successful": self.validation_successful,
            "issues": [issue.to_data() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class WorkflowStepIR:
    """The engine-independent, statically compiled view of one step."""

    index: int
    step_id: str
    step: SkillStep
    successors: tuple[int, ...]
    predecessors: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WorkflowIR:
    """Normalized control-flow representation used by static validation."""

    steps: tuple[WorkflowStepIR, ...]
    initial_inputs: frozenset[str]


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionsReport:
    """Repository-wide result for statically compiling definitions."""

    reports: tuple[WorkflowDefinitionReport, ...]

    @property
    def validation_successful(self) -> bool:
        return all(report.validation_successful for report in self.reports)

    def to_data(self) -> dict[str, object]:
        return {
            "validation_successful": self.validation_successful,
            "definition_count": len(self.reports),
            "reports": [report.to_data() for report in self.reports],
        }


def discover_workflow_definitions(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Return supported definition files below files or directories."""
    discovered: set[Path] = set()
    for path in paths:
        if path.is_file():
            if path.suffix.lower() in {".yaml", ".yml", ".json"}:
                discovered.add(path)
            continue
        if path.is_dir():
            discovered.update(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file()
                and candidate.suffix.lower() in {".yaml", ".yml", ".json"}
                and ".git" not in candidate.relative_to(path).parts
            )
    return tuple(sorted(discovered))


def analyze_workflow_definitions(paths: Sequence[Path]) -> WorkflowDefinitionsReport:
    """Statically analyze every supported definition under ``paths``."""
    definitions = discover_workflow_definitions(paths)
    reports = [analyze_workflow_definition(path) for path in definitions]
    skill_paths = {
        path for path in definitions if path.parent.name == "skill-definitions"
    }
    skills: dict[str, tuple[Path, Skill]] = {}
    for skill_path in skill_paths:
        try:
            skill = load_skill(skill_path)
        except (OSError, ValueError):
            continue
        skills[skill.name] = (skill_path, skill)
    if skills:
        updated: list[WorkflowDefinitionReport] = []
        for report in reports:
            extra = _validate_skill_call_graph(report.definition, skills)
            updated.append(
                WorkflowDefinitionReport(
                    report.definition, report.kind, (*report.issues, *extra)
                )
            )
        reports = updated
    return WorkflowDefinitionsReport(tuple(reports))


def analyze_workflow_definition(path: Path) -> WorkflowDefinitionReport:
    """Validate a skill or workflow template and flag deterministic confusion risks."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        return WorkflowDefinitionReport(
            definition=path,
            kind="unknown",
            issues=(
                WorkflowDefinitionIssue(
                    code="definition_parse_error",
                    message=str(exc),
                    path=str(path),
                ),
            ),
        )
    if not isinstance(data, Mapping):
        return WorkflowDefinitionReport(
            definition=path,
            kind="unknown",
            issues=(
                WorkflowDefinitionIssue(
                    code="definition_not_mapping",
                    message="Definition must decode to an object.",
                    path=str(path),
                ),
            ),
        )
    base_issues: Sequence[Any] | None
    if "steps" in data:
        kind = "skill"
        base_issues = build_skill_validation_report(raw, source_path=path).issues
    elif "task_templates" in data:
        kind = "workflow_template"
        base_issues = build_workflow_template_validation_report(
            json.dumps(data), source_path=path
        ).issues
    else:
        kind = "unknown"
        base_issues = None
    issues: list[WorkflowDefinitionIssue] = []
    if base_issues is None:
        issues.append(
            WorkflowDefinitionIssue(
                code="unsupported_definition_kind",
                message="Definition must contain steps or task_templates.",
                path=str(path),
            )
        )
    else:
        issues.extend(
            WorkflowDefinitionIssue(issue.code, issue.message, issue.path or str(path))
            for issue in base_issues
        )
        if kind == "workflow_template":
            issues.extend(_validate_template_liveness(data, path))
    step_key = "steps" if kind == "skill" else "task_templates"
    steps = data.get(step_key)
    if isinstance(steps, Sequence) and not isinstance(steps, (str, bytes)):
        declared = _declared_placeholders(data, steps)
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                continue
            step_path = f"{path}.{step_key}[{index}]"
            issues.extend(_validate_step_examples(step, step_path))
            if declared:
                issues.extend(_validate_step_placeholders(step, step_path, declared))
        if kind == "skill":
            try:
                skill = load_skill(path)
            except (OSError, ValueError):
                skill = None
            if skill is not None:
                ir, compiler_issues = _compile_skill(skill, path)
                issues.extend(compiler_issues)
                if ir is not None:
                    issues.extend(_validate_handoffs(ir, path))
                    issues.extend(_validate_liveness(ir, path))
            else:
                issues.extend(_validate_raw_liveness(data, path))
    return WorkflowDefinitionReport(path, kind, tuple(issues))


def _validate_raw_liveness(
    data: Mapping[str, Any], path: Path
) -> list[WorkflowDefinitionIssue]:
    """Retain liveness diagnostics when schema validation rejects a definition."""
    issues: list[WorkflowDefinitionIssue] = []
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
        return issues
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            continue
        step_path = f"{path}.steps[{index}]"
        if raw_step.get("step_type") == "coding_loop":
            loop = raw_step.get("coding_loop")
            if isinstance(loop, Mapping) and (
                not isinstance(loop.get("max_iterations"), int)
                or loop.get("max_iterations", 0) <= 0
                or not loop.get("stopping_conditions")
            ):
                issues.append(
                    WorkflowDefinitionIssue(
                        "unbounded_coding_loop",
                        "Coding loops require a positive iteration bound and a stopping condition.",
                        f"{step_path}.coding_loop",
                    )
                )
        completion = raw_step.get("completion")
        if isinstance(completion, Mapping) and completion.get("required_outputs"):
            declared = {
                output.get("name")
                for output in raw_step.get("outputs", [])
                if isinstance(output, Mapping)
            }
            missing = set(completion["required_outputs"]) - declared
            if missing:
                issues.append(
                    WorkflowDefinitionIssue(
                        "unobservable_completion",
                        "Completion requires outputs with no declared producer: "
                        + ", ".join(sorted(missing)),
                        f"{step_path}.completion.required_outputs",
                    )
                )
                issues.append(
                    WorkflowDefinitionIssue(
                        "terminal_state_without_completion",
                        "Reachable terminal state cannot satisfy its completion outputs.",
                        step_path,
                        state={"missing_outputs": sorted(missing)},
                    )
                )
    return issues


def _validate_liveness(ir: WorkflowIR, path: Path) -> list[WorkflowDefinitionIssue]:
    """Find high-confidence model-owned actions that can repeat without progress."""
    issues: list[WorkflowDefinitionIssue] = []
    for item in ir.steps:
        step = item.step
        issues.extend(_validate_unknown_effects(item, path))
        issues.extend(_validate_prompt_authority(item, path))
        if step.step_type not in {"governed", "coding_loop"}:
            continue
        for invocation in step.tool_invocations:
            effect = capability_effect(invocation.to_data())
            if not is_fixed_deterministic(effect):
                continue
            step_path = f"{path}.steps[{item.index}]"
            operation = effect.operation if effect is not None else "operation"
            if step.pre_step is None:
                issues.append(
                    WorkflowDefinitionIssue(
                        "model_owned_deterministic_action",
                        f"{operation} is deterministic but is exposed as an LLM-owned "
                        "tool invocation. Move it to a deterministic pre-step or "
                        "declare why LLM judgment is required.",
                        f"{step_path}.tool_invocations",
                        severity="warning",
                    )
                )
            if is_idempotent(effect) and step.completion is None:
                issues.append(
                    WorkflowDefinitionIssue(
                        "idempotent_action_without_auto_advance",
                        f"{operation} can succeed repeatedly without changing "
                        "abstract state while this step remains active. Convert "
                        "it to a runner-owned pre-step or add a machine-owned "
                        "success transition.",
                        f"{step_path}.tool_invocations",
                        severity="warning",
                        remediation="Convert the action to a runner-owned pre-step or add a machine-owned success transition.",
                    )
                )
        issues.extend(_validate_step_contract(ir, item, path))
    issues.extend(_validate_abstract_graph(ir, path))
    issues.extend(_validate_non_progress_cycles(ir, path))
    return issues


def _validate_unknown_effects(
    item: WorkflowStepIR, path: Path
) -> list[WorkflowDefinitionIssue]:
    issues: list[WorkflowDefinitionIssue] = []
    for invocation in item.step.tool_invocations:
        if (
            invocation.tool == "shell"
            and capability_effect(invocation.to_data()) is None
        ):
            issues.append(
                WorkflowDefinitionIssue(
                    "unknown_shell_effect",
                    "Unrestricted shell invocation has no declarative effect summary; progress cannot be proven.",
                    f"{path}.steps[{item.index}].tool_invocations",
                    severity="warning",
                    remediation="Use a bounded capability or declare checked-in effect metadata for this command.",
                )
            )
    return issues


def _validate_prompt_authority(
    item: WorkflowStepIR, path: Path
) -> list[WorkflowDefinitionIssue]:
    details = item.step.details or ""
    if not details:
        return []
    declared = set(item.step.actions)
    declared.update(
        invocation.operation or (invocation.command[0] if invocation.command else "")
        for invocation in item.step.tool_invocations
    )
    if item.step.pre_step is not None:
        declared.add(item.step.pre_step.action)
    issues: list[WorkflowDefinitionIssue] = []
    for action in (
        "edit",
        "yaml_edit",
        "file_management",
        "read_document",
        "git add",
        "git commit",
    ):
        mentioned = action in details.casefold()
        denied = re.search(
            rf"\b(?:do not|don't|never|avoid|without)\b[^.\n]{{0,60}}"
            rf"\b{re.escape(action)}\b",
            details,
            flags=re.IGNORECASE,
        )
        directed = re.search(
            rf"\b(?:use|invoke|run|perform|call|must|should)\b[^.\n]{{0,60}}"
            rf"\b{re.escape(action)}\b",
            details,
            flags=re.IGNORECASE,
        )
        if (
            mentioned
            and directed
            and not denied
            and action.replace(" ", "_") not in declared
            and action not in declared
        ):
            issues.append(
                WorkflowDefinitionIssue(
                    "forbidden_verification_action",
                    f"Prompt mentions {action!r}, but the effective step contract does not declare it.",
                    f"{path}.steps[{item.index}].details",
                    severity="warning",
                    remediation="Declare the action structurally or move it to a deterministic step.",
                )
            )
    return issues


def _validate_template_liveness(
    data: Mapping[str, Any], path: Path
) -> list[WorkflowDefinitionIssue]:
    issues: list[WorkflowDefinitionIssue] = []
    tasks = data.get("task_templates")
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        return issues
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            continue
        task_path = f"{path}.task_templates[{index}]"
        step_type = task.get("step_type")
        loop = task.get("coding_loop")
        if step_type == "coding_loop" and (
            not isinstance(loop, Mapping)
            or not isinstance(loop.get("max_iterations"), int)
            or loop.get("max_iterations", 0) <= 0
            or not loop.get("stopping_conditions")
        ):
            issues.append(
                WorkflowDefinitionIssue(
                    "unbounded_coding_loop",
                    "Coding-loop template requires max_iterations and stopping_conditions.",
                    f"{task_path}.coding_loop",
                )
            )
        details = task.get("details")
        actions = (
            set(task.get("actions", []))
            if isinstance(task.get("actions"), list)
            else set()
        )
        if (
            isinstance(details, str)
            and "git add" in details.casefold()
            and "git_add" not in actions
        ):
            issues.append(
                WorkflowDefinitionIssue(
                    "forbidden_verification_action",
                    "Template prose mentions git add without a structured action contract.",
                    f"{task_path}.details",
                    severity="warning",
                )
            )
    return issues


def _validate_skill_call_graph(
    definition: Path, skills: Mapping[str, tuple[Path, Skill]]
) -> list[WorkflowDefinitionIssue]:
    """Check nested-skill references and recursive call components."""
    try:
        current = load_skill(definition)
    except (OSError, ValueError):
        return []
    issues: list[WorkflowDefinitionIssue] = []
    graph: dict[str, set[str]] = {name: set() for name in skills}
    for name, (_, skill) in skills.items():
        for index, step in enumerate(skill.steps):
            if step.uses_skill is None:
                continue
            target = step.uses_skill.skill
            if target not in skills:
                if name == current.name:
                    issues.append(
                        WorkflowDefinitionIssue(
                            "missing_nested_skill_output",
                            f"Nested skill {target!r} is not available in the checked-in skill set.",
                            f"{definition}.steps[{index}].uses_skill.skill",
                        )
                    )
                continue
            graph[name].add(target)
            target_skill = skills[target][1]
            target_outputs = {
                output.name
                for nested_step in target_skill.steps
                for output in nested_step.outputs
            }
            for binding in step.uses_skill.outputs:
                if binding.ref not in target_outputs:
                    issues.append(
                        WorkflowDefinitionIssue(
                            "missing_nested_skill_output",
                            f"Nested skill {target!r} does not declare output {binding.ref!r}.",
                            f"{definition}.steps[{index}].uses_skill.outputs",
                            remediation="Expose the output from the nested skill or remove the binding.",
                        )
                    )
    reachable = {current.name}
    frontier = [current.name]
    while frontier:
        name = frontier.pop()
        for target in graph.get(name, ()):
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    for name in sorted(reachable):
        if name in graph.get(name, set()):
            issues.append(
                WorkflowDefinitionIssue(
                    "unbounded_nested_skill_recursion",
                    f"Skill {name!r} recursively invokes itself without a decreasing bound.",
                    str(definition),
                    remediation="Add an explicit recursion bound or remove the recursive call.",
                )
            )
    for component in _graph_components(graph):
        if len(component) > 1 and component & reachable:
            issues.append(
                WorkflowDefinitionIssue(
                    "unbounded_nested_skill_recursion",
                    "Nested skill call cycle has no explicit decreasing bound: "
                    + " -> ".join(sorted(component)),
                    str(definition),
                    remediation="Add an explicit recursion bound or remove the recursive call.",
                )
            )
    return issues


def _graph_components(graph: Mapping[str, set[str]]) -> tuple[frozenset[str], ...]:
    """Return strongly connected components for the skill call graph."""
    counter = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[frozenset[str]] = []

    def visit(node: str) -> None:
        nonlocal counter
        indices[node] = counter
        lowlinks[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph.get(node, set()):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] == indices[node]:
            component: set[str] = set()
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.add(member)
                if member == node:
                    break
            components.append(frozenset(component))

    for node in graph:
        if node not in indices:
            visit(node)
    return tuple(components)


def _validate_step_contract(
    ir: WorkflowIR, item: WorkflowStepIR, path: Path
) -> list[WorkflowDefinitionIssue]:
    """Check completion, observability, and bounded-loop contracts."""
    step = item.step
    step_path = f"{path}.steps[{item.index}]"
    issues: list[WorkflowDefinitionIssue] = []
    if step.completion is not None:
        declared = {output.name for output in step.outputs}
        missing = set(step.completion.required_outputs) - declared
        if missing:
            issues.append(
                WorkflowDefinitionIssue(
                    "unobservable_completion",
                    "Completion requires outputs with no declared producer: "
                    + ", ".join(sorted(missing)),
                    f"{step_path}.completion.required_outputs",
                    remediation="Declare the output and produce it through a structured action.",
                )
            )
        if (
            step.completion.required_outputs
            and not step.actions
            and not step.tool_invocations
            and step.pre_step is None
            and step.uses_skill is None
        ):
            issues.append(
                WorkflowDefinitionIssue(
                    "unobservable_completion",
                    "Completion outputs are declared but no action, tool, pre-step, "
                    "or nested skill can produce them.",
                    f"{step_path}.completion",
                    remediation="Add a structured producer or remove the completion guard.",
                )
            )
        for required in step.completion.required_actions:
            declared_actions = set(step.actions)
            declared_actions.update(
                invocation.operation
                or (invocation.command[0] if invocation.command else "")
                for invocation in step.tool_invocations
            )
            if required.action not in declared_actions:
                issues.append(
                    WorkflowDefinitionIssue(
                        "required_action_after_satisfied_postcondition",
                        f"Completion requires {required.action!r}, but the step does not declare a producer.",
                        f"{step_path}.completion.required_actions",
                        severity="warning",
                        remediation="Declare the action or remove the completion requirement.",
                    )
                )
    if step.step_type == "coding_loop":
        loop = step.coding_loop
        if loop is None or loop.max_iterations <= 0 or not loop.stopping_conditions:
            issues.append(
                WorkflowDefinitionIssue(
                    "unbounded_coding_loop",
                    "Coding loops require a positive iteration bound and a stopping condition.",
                    f"{step_path}.coding_loop",
                    remediation="Declare max_iterations and a verification-backed stopping condition.",
                )
            )
    if step.pre_step is not None and step.outputs == ():
        effect = effect_for_pre_step(step.pre_step.to_data())
        if effect is not None and effect.produces:
            issues.append(
                WorkflowDefinitionIssue(
                    "runner_result_not_consumed",
                    f"Runner-owned {effect.operation} produces {sorted(effect.produces)!r}, but the step declares no output or condition consuming it.",
                    f"{step_path}.pre_step",
                    severity="warning",
                    remediation="Publish the result as a structured output or remove the operation.",
                )
            )
    if step.gate is not None:
        retry_target = next(
            (successor for successor in item.successors if successor != item.index + 1),
            None,
        )
        if retry_target == item.index or retry_target is None:
            issues.append(
                WorkflowDefinitionIssue(
                    "retry_without_relevant_effect",
                    "Gate retry returns to the same state without a declared corrective effect.",
                    f"{step_path}.gate.goto_step",
                    severity="warning",
                    remediation="Redirect the retry to a step that writes a domain observed by the gate.",
                )
            )
    return issues


def _validate_abstract_graph(
    ir: WorkflowIR, path: Path
) -> list[WorkflowDefinitionIssue]:
    """Explore the finite abstract graph and report terminal sinks."""
    if not ir.steps:
        return []
    initial = AbstractWorkflowState(0)
    queue = [initial]
    seen: set[AbstractWorkflowState] = set()
    issues: list[WorkflowDefinitionIssue] = []
    while queue and len(seen) < 4096:
        state = queue.pop(0)
        if state in seen:
            continue
        seen.add(state)
        transitions = expand_abstract_transitions(ir, state)
        item = ir.steps[state.step_index]
        missing_completion = (
            set(item.step.completion.required_outputs) - state.available_outputs
            if item.step.completion is not None
            else set()
        )
        if not transitions and missing_completion:
            issues.append(
                WorkflowDefinitionIssue(
                    "terminal_state_without_completion",
                    "Reachable state has no valid outgoing transition and cannot satisfy completion.",
                    f"{path}.steps[{state.step_index}]",
                    state={
                        "step": item.step_id,
                        "available_outputs": sorted(state.available_outputs),
                        "missing_outputs": sorted(missing_completion),
                    },
                    remediation="Add a producer, a terminal transition, or an explicit blocked outcome.",
                )
            )
        for transition in transitions:
            if transition.target not in seen:
                queue.append(transition.target)
    return issues


def _validate_non_progress_cycles(
    ir: WorkflowIR, path: Path
) -> list[WorkflowDefinitionIssue]:
    """Warn on CFG cycles with no declared progress-producing action."""
    issues: list[WorkflowDefinitionIssue] = []
    reachable: set[int] = set()
    frontier = [0] if ir.steps else []
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        frontier.extend(ir.steps[current].successors)
    for component in _strongly_connected_components(ir):
        if not component & reachable:
            continue
        if len(component) == 1:
            index = next(iter(component))
            if index not in ir.steps[index].successors:
                continue
        if any(_step_can_produce_progress(ir.steps[index].step) for index in component):
            continue
        first = min(component)
        ids = ", ".join(ir.steps[index].step_id for index in sorted(component))
        issues.append(
            WorkflowDefinitionIssue(
                "non_progress_cycle",
                f"Reachable cycle ({ids}) has no declared action that can change "
                "workflow state, repository state, validation state, or human "
                "input.",
                f"{path}.steps[{first}]",
                severity="warning",
                cycle=tuple(ir.steps[index].step_id for index in sorted(component)),
                remediation="Add a bounded exit, relevant corrective action, or terminal blocked outcome.",
            )
        )
    return issues


def _step_can_produce_progress(step: SkillStep) -> bool:
    if step.step_type in {"uses_skill", "predicated"}:
        return True
    if any(
        action
        in {"edit", "yaml_edit", "file_management", "prompt_user", "invoke_skill"}
        for action in step.actions
    ):
        return True
    for invocation in step.tool_invocations:
        effect = capability_effect(invocation.to_data())
        if (
            effect is None
            or effect.writes
            or effect.produces
            - {
                "tool_result",
                "context",
                "repository_state",
            }
        ):
            return True
    pre_effect = effect_for_pre_step(step.pre_step.to_data() if step.pre_step else None)
    if pre_effect is None:
        return False
    return bool(
        pre_effect.writes
        or pre_effect.produces - {"tool_result", "context", "repository_state"}
    )


def _strongly_connected_components(ir: WorkflowIR) -> tuple[frozenset[int], ...]:
    """Return CFG strongly connected components using Tarjan's algorithm."""
    successors = {item.index: item.successors for item in ir.steps}
    index = 0
    indices: dict[int, int] = {}
    lowlinks: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    components: list[frozenset[int]] = []

    def visit(node: int) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for successor in successors[node]:
            if successor not in indices:
                visit(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] == indices[node]:
            component: set[int] = set()
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.add(member)
                if member == node:
                    break
            components.append(frozenset(component))

    for item in ir.steps:
        if item.index not in indices:
            visit(item.index)
    return tuple(components)


def render_skill_prompt_snapshots(
    definition_path: Path,
    *,
    output_dir: Path,
    repo_root: Path | None = None,
) -> tuple[Path, ...]:
    """Render normalized prompt contracts for every skill or template step."""
    from powdrr_lift.workflow_chat_agent import (
        SkillCatalogEntry,
        _build_step_execution_messages,
    )

    root = resolve_repo_root(repo_root)
    raw = yaml.safe_load(definition_path.read_text(encoding="utf-8"))
    if isinstance(raw, Mapping) and isinstance(raw.get("task_templates"), list):
        return _render_template_prompt_snapshots(
            definition_path, raw, output_dir=output_dir, repo_root=root
        )
    skill = load_skill(definition_path)
    entry = SkillCatalogEntry(definition_path, skill)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, step in enumerate(skill.steps):
        messages = _build_step_execution_messages(
            selected_skill=entry,
            current_step=step,
            current_step_index=index,
            transcript=[{"role": "user", "content": "<root-intent>"}],
            execution_events=[],
            execution_context=[],
            handoff_records={},
            durable_facts={},
            current_file_path=None,
            worktree_root=root,
            catalog=(entry,),
        )
        snapshot = _normalize_snapshot(
            {
                "schema_version": 1,
                "definition": _portable_path(definition_path, root),
                "skill": skill.name,
                "step_index": index,
                "step_id": step.id,
                "messages": messages,
            },
            root,
        )
        name = f"{index + 1:03d}-{step.id or 'step'}.json"
        output_path = output_dir / name
        output_path.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        paths.append(output_path)
    return tuple(paths)


def _render_template_prompt_snapshots(
    definition_path: Path,
    template: Mapping[str, Any],
    *,
    output_dir: Path,
    repo_root: Path,
) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    tasks = template["task_templates"]
    assert isinstance(tasks, list)
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            continue
        snapshot = _normalize_snapshot(
            {
                "schema_version": 1,
                "definition": _portable_path(definition_path, repo_root),
                "workflow_template": template.get("id"),
                "task_index": index,
                "description": task.get("description"),
                "step_type": task.get("step_type"),
                "input_state": task.get("input_state", {}),
                "pre_step": task.get("pre_step"),
                "details": task.get("details"),
                "output_state_type": task.get("output_state_type"),
            },
            repo_root,
        )
        name = f"{index + 1:03d}-{_snapshot_name(task.get('description'))}.json"
        output_path = output_dir / name
        output_path.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        paths.append(output_path)
    return tuple(paths)


def _snapshot_name(value: Any) -> str:
    text = value if isinstance(value, str) else "task"
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "task"


def _validate_step_examples(
    step: Mapping[str, Any], step_path: str
) -> list[WorkflowDefinitionIssue]:
    details = step.get("details")
    if not isinstance(details, str):
        return []
    issues: list[WorkflowDefinitionIssue] = []
    decoder = json.JSONDecoder()
    for match in _ACTION_START.finditer(details):
        # A step description may embed an action inside another example. Only
        # validate top-level action examples; output schemas cover nested data.
        prefix = details[: match.start()]
        if prefix.count("{") != prefix.count("}"):
            continue
        try:
            action_data, _ = decoder.raw_decode(details[match.start() :])
        except json.JSONDecodeError as exc:
            issues.append(
                WorkflowDefinitionIssue(
                    "invalid_action_example_json",
                    f"Action example is not valid JSON: {exc.msg}.",
                    f"{step_path}.details",
                )
            )
            continue
        if not isinstance(action_data, dict):
            continue
        try:
            from powdrr_lift.workflow_chat_agent import (
                _parse_action_response_with_schema,
                _step_action_response_schema,
            )

            parsed_step = skill_step_from_data(step)
            _parse_action_response_with_schema(
                action_data, schema=_step_action_response_schema(parsed_step)
            )
        except RuntimeError as exc:
            issues.append(
                WorkflowDefinitionIssue(
                    "invalid_action_example",
                    f"Action example does not match the runtime action schema: {exc}",
                    f"{step_path}.details",
                )
            )
    return issues


def _compile_skill(
    skill: Skill, path: Path
) -> tuple[WorkflowIR | None, list[WorkflowDefinitionIssue]]:
    """Compile a skill into explicit CFG edges before execution."""
    issues: list[WorkflowDefinitionIssue] = []
    steps = skill.steps
    ids = [step.id or f"step-{index + 1}" for index, step in enumerate(steps)]
    index_by_id = {step_id: index for index, step_id in enumerate(ids)}
    successors: list[set[int]] = [set() for _ in steps]

    def add_target(source: int, target_id: str, field: str) -> None:
        target = index_by_id.get(target_id)
        if target is None:
            issues.append(
                WorkflowDefinitionIssue(
                    "unknown_control_flow_target",
                    f"Control-flow target {target_id!r} is not a declared step id.",
                    f"{path}.steps[{source}].{field}",
                )
            )
        else:
            successors[source].add(target)

    for index, step in enumerate(steps):
        if index + 1 < len(steps):
            successors[index].add(index + 1)
        if step.next_step_override is not None:
            successors[index].clear()
            add_target(index, step.next_step_override, "next_step_override")
        if step.gate is not None:
            add_target(index, step.gate.goto_step, "gate.goto_step")
            if step.gate.success_goto_step is not None:
                add_target(index, step.gate.success_goto_step, "gate.success_goto_step")

    predecessors: list[set[int]] = [set() for _ in steps]
    for source, targets in enumerate(successors):
        for target in targets:
            predecessors[target].add(source)
    reachable: set[int] = set()
    frontier = [0] if steps else []
    while frontier:
        index = frontier.pop()
        if index in reachable:
            continue
        reachable.add(index)
        frontier.extend(successors[index])
    for index in range(len(steps)):
        if index not in reachable:
            issues.append(
                WorkflowDefinitionIssue(
                    "unreachable_step",
                    "Step cannot be reached from the first step through declared "
                    "control flow.",
                    f"{path}.steps[{index}]",
                )
            )
    compiled = tuple(
        WorkflowStepIR(
            index=index,
            step_id=ids[index],
            step=steps[index],
            successors=tuple(sorted(successors[index])),
            predecessors=tuple(sorted(predecessors[index])),
        )
        for index in range(len(steps))
    )
    initial_inputs = frozenset(
        name
        for item in (*skill.inputs, *(steps[0].inputs if steps else ()))
        for name in _input_names(item.name)
    )
    return WorkflowIR(compiled, initial_inputs), issues


def _validate_handoffs(ir: WorkflowIR, path: Path) -> list[WorkflowDefinitionIssue]:
    """Run definite-assignment and schema checks over every reachable path."""
    issues: list[WorkflowDefinitionIssue] = []
    available_in: list[set[str] | None] = [None] * len(ir.steps)
    available_out: list[set[str]] = [set() for _ in ir.steps]
    if ir.steps:
        available_in[0] = set(ir.initial_inputs)
    changed = True
    while changed:
        changed = False
        for item in ir.steps:
            if item.index and item.predecessors:
                incoming = [
                    available_out[index]
                    for index in item.predecessors
                    if index < item.index
                ]
                if not incoming:
                    continue
                candidate = set.intersection(*(set(values) for values in incoming))
                if available_in[item.index] != candidate:
                    available_in[item.index] = candidate
                    changed = True
            current = set(available_in[item.index] or ())
            current.update(_guaranteed_outputs(item.step))
            if current != available_out[item.index]:
                available_out[item.index] = current
                changed = True

    for item in ir.steps:
        step_path = f"{path}.steps[{item.index}]"
        available = available_in[item.index] or set()
        for input_item in item.step.inputs:
            if (
                item.index
                and input_item.required
                and input_item.source == "previous_step"
            ):
                if not any(name in available for name in _input_names(input_item.name)):
                    issues.append(
                        WorkflowDefinitionIssue(
                            "missing_handoff_input",
                            f"Required input {input_item.name!r} is not definitely "
                            "produced by every incoming path.",
                            f"{step_path}.inputs",
                        )
                    )
        required_outputs = {
            output.name for output in item.step.outputs if output.required_for_next_step
        }
        if item.step.completion is not None:
            required_outputs.update(item.step.completion.required_outputs)
        declarations = {output.name: output for output in item.step.outputs}
        for output_name in sorted(required_outputs):
            output = declarations.get(output_name)
            if output is not None and output.schema is None:
                issues.append(
                    WorkflowDefinitionIssue(
                        "missing_output_schema",
                        "Required handoff outputs must declare a JSON schema.",
                        f"{step_path}.outputs[{output_name}]",
                    )
                )
    return issues


def _guaranteed_outputs(step: SkillStep) -> set[str]:
    # Handoff records persist in workflow state after publication.  A later
    # step may therefore depend on any declared output, not only outputs marked
    # as required for the immediate next step.
    outputs = {output.name for output in step.outputs}
    if step.completion is not None:
        outputs.update(step.completion.required_outputs)
    return outputs


def _input_names(name: str) -> tuple[str, ...]:
    return (name, name.replace("_", "-"), name.replace("-", "_"))


def _declared_placeholders(
    definition: Mapping[str, Any], steps: Sequence[Any]
) -> set[str]:
    declared: set[str] = set()
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        for item in step.get("inputs", []):
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                declared.add(item["name"].replace("_", "-"))
                declared.add(item["name"])
            elif isinstance(item, str):
                declared.update(_PLACEHOLDER.findall(item))
        for item in step.get("outputs", []):
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                declared.add(item["name"])
                declared.add(item["name"].replace("_", "-"))
        input_state = step.get("input_state")
        if isinstance(input_state, Mapping):
            for name, value in input_state.items():
                if isinstance(name, str):
                    declared.add(name)
                    declared.add(name.replace("_", "-"))
                if isinstance(value, str):
                    declared.update(_PLACEHOLDER.findall(value))
    for item in definition.get("inputs", []):
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            declared.add(item["name"])
            declared.add(item["name"].replace("_", "-"))
        elif isinstance(item, str):
            declared.update(_PLACEHOLDER.findall(item))
    return declared


def _validate_step_placeholders(
    step: Mapping[str, Any], step_path: str, declared: set[str]
) -> list[WorkflowDefinitionIssue]:
    issues: list[WorkflowDefinitionIssue] = []
    for field, text in _walk_strings(step):
        for name in _PLACEHOLDER.findall(text):
            if name in declared or re.fullmatch(r"upstream-task-\d+", name):
                continue
            issues.append(
                WorkflowDefinitionIssue(
                    "unbound_placeholder",
                    f"Placeholder <{name}> is not declared by this definition "
                    "input contract.",
                    f"{step_path}.{field}",
                )
            )
    return issues


def _walk_strings(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(prefix or "value", value)]
    if isinstance(value, Mapping):
        return [
            item
            for key, child in value.items()
            for item in _walk_strings(child, f"{prefix}.{key}" if prefix else str(key))
        ]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            item
            for index, child in enumerate(value)
            for item in _walk_strings(child, f"{prefix}[{index}]")
        ]
    return []


def _normalize_snapshot(value: Any, repo_root: Path) -> Any:
    if isinstance(value, str):
        return value.replace(str(repo_root.resolve()), "<repo-root>")
    if isinstance(value, Mapping):
        return {
            key: _normalize_snapshot(item, repo_root) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_snapshot(item, repo_root) for item in value]
    return value


def _portable_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)
