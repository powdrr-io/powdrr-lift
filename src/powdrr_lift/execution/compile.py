"""Compile typed delivery artifacts into durable workflow tasks."""

from __future__ import annotations

from collections.abc import Mapping

from powdrr_lift.core.delivery_profile import DeliveryProfile, PersonaType, PhaseType
from powdrr_lift.core.execution_plan import ExecutionPlan
from powdrr_lift.core.skill_specification import SUPPORTED_STEP_ACTIONS
from powdrr_lift.core.workflow_task_specification import (
    AgentRole,
    AssigneeType,
    TaskComplexity,
    TaskStatus,
    WorkflowTask,
)
from powdrr_lift.errors import PowdrrExecutionError


def compile_execution_plan(
    profile: DeliveryProfile,
    plan: ExecutionPlan,
    *,
    actions_by_phase: Mapping[PhaseType, tuple[str, ...]],
    skills_by_phase: Mapping[PhaseType, tuple[str, ...]] | None = None,
    intent_ids_by_phase: Mapping[PhaseType, tuple[str, ...]] | None = None,
    clause_ids_by_phase: Mapping[PhaseType, tuple[str, ...]] | None = None,
) -> tuple[WorkflowTask, ...]:
    """Produce deterministic tasks while preserving step actions verbatim."""

    skills = skills_by_phase or {}
    intent_ids = intent_ids_by_phase or {}
    clause_ids = clause_ids_by_phase or {}
    assignments = {item.phase_type: item for item in profile.phases}
    personas = {item.persona_id: item for item in profile.personas}
    phases = tuple(assignments)
    unknown_phases = set(actions_by_phase) - set(phases)
    if unknown_phases:
        names = ", ".join(sorted(phase.value for phase in unknown_phases))
        raise PowdrrExecutionError(
            f"Action contract contains unknown delivery phase(s): {names}.",
            error_code="compiled_workflow_phase_unknown",
            remediation="Provide contracts only for phases in the delivery profile.",
        )
    tasks: list[WorkflowTask] = []
    for unit in plan.units:
        previous_task_id: str | None = None
        for phase in phases:
            assignment = assignments[phase]
            persona = personas[assignment.persona_id]
            actions = actions_by_phase.get(phase)
            if actions is None:
                raise PowdrrExecutionError(
                    f"No action contract was supplied for phase {phase.value!r}.",
                    error_code="compiled_workflow_actions_missing",
                    action_kind=phase.value,
                    remediation="Provide the phase's complete allowed action set.",
                )
            if any(not action.strip() for action in actions):
                raise PowdrrExecutionError(
                    "Action contracts may not contain blank actions for phase "
                    f"{phase.value!r}.",
                    error_code="compiled_workflow_action_invalid",
                    action_kind=phase.value,
                    remediation="Remove blank action names from the phase contract.",
                )
            if len(set(actions)) != len(actions):
                raise PowdrrExecutionError(
                    "Action contracts may not contain duplicate actions for phase "
                    f"{phase.value!r}.",
                    error_code="compiled_workflow_action_duplicate",
                    action_kind=phase.value,
                    remediation="List each allowed action exactly once.",
                )
            unsupported = set(actions) - SUPPORTED_STEP_ACTIONS
            if unsupported:
                names = ", ".join(sorted(unsupported))
                raise PowdrrExecutionError(
                    f"Action contract contains unsupported action(s): {names}.",
                    error_code="compiled_workflow_action_unsupported",
                    action_kind=phase.value,
                    remediation=(
                        "Use only actions supported by the workflow action schema."
                    ),
                )
            task_id = f"{unit.unit_id}-{phase.value}"
            upstream: list[str] = []
            if previous_task_id is not None:
                upstream.append(previous_task_id)
            upstream.extend(
                f"{dependency}-{phase.value}" for dependency in unit.dependencies
            )
            tasks.append(
                WorkflowTask(
                    task_id=task_id,
                    status=TaskStatus.OPEN,
                    description=f"{phase.value} for {unit.unit_id}: {unit.objective}",
                    complexity=TaskComplexity.MEDIUM,
                    input_state={
                        "artifacts": list(assignment.input_artifacts),
                        "execution_plan_id": plan.plan_id,
                        "proposed_pr_fingerprint": plan.proposed_pr_fingerprint,
                        "execution_unit_id": unit.unit_id,
                        "phase_type": phase.value,
                        "persona_id": persona.persona_id,
                        "intent_ids": list(intent_ids.get(phase, ())),
                        "clause_ids": list(clause_ids.get(phase, ())),
                    },
                    output_state={
                        "artifacts": list(assignment.output_artifacts),
                        "acceptance_criteria": list(unit.acceptance_criteria),
                        "validation_profiles": list(unit.validation_profiles),
                    },
                    assignee_type=AssigneeType.AGENT,
                    assignee_role=_agent_role(persona.persona_type),
                    llm_type=persona.model_profile,
                    uses_skills=skills.get(phase, persona.skill_references),
                    prompt_catalogs=persona.prompt_catalogs,
                    actions=actions,
                    actions_declared=True,
                    upstream_task_ids=tuple(upstream),
                    phase_type=phase,
                    persona_id=persona.persona_id,
                )
            )
            previous_task_id = task_id
    return tuple(tasks)


def _agent_role(persona_type: PersonaType) -> AgentRole:
    if persona_type is PersonaType.ARCHITECT:
        return AgentRole.ARCHITECT
    if persona_type in {PersonaType.SPECIFICATION_REVIEWER, PersonaType.CODE_REVIEWER}:
        return AgentRole.REVIEWER
    return AgentRole.CODER
