"""Compile typed delivery artifacts into durable workflow tasks."""

from __future__ import annotations

from collections.abc import Mapping

from powdrr_lift.core.delivery_profile import DeliveryProfile, PersonaType, PhaseType
from powdrr_lift.core.execution_plan import ExecutionPlan
from powdrr_lift.core.workflow_task_specification import (
    AgentRole,
    AssigneeType,
    TaskComplexity,
    TaskStatus,
    WorkflowTask,
)


def compile_execution_plan(
    profile: DeliveryProfile,
    plan: ExecutionPlan,
    *,
    actions_by_phase: Mapping[PhaseType, tuple[str, ...]],
    skills_by_phase: Mapping[PhaseType, tuple[str, ...]] | None = None,
) -> tuple[WorkflowTask, ...]:
    """Produce deterministic tasks while preserving step actions verbatim."""

    skills = skills_by_phase or {}
    assignments = {item.phase_type: item for item in profile.phases}
    personas = {item.persona_id: item for item in profile.personas}
    phases = tuple(assignments)
    tasks: list[WorkflowTask] = []
    for unit in plan.units:
        previous_task_id: str | None = None
        for phase in phases:
            assignment = assignments[phase]
            persona = personas[assignment.persona_id]
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
                    input_state={"artifacts": list(assignment.input_artifacts)},
                    output_state={"artifacts": list(assignment.output_artifacts)},
                    assignee_type=AssigneeType.AGENT,
                    assignee_role=_agent_role(persona.persona_type),
                    llm_type=persona.model_profile,
                    uses_skills=skills.get(phase, persona.skill_references),
                    prompt_catalogs=persona.prompt_catalogs,
                    actions=actions_by_phase.get(phase, ()),
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
