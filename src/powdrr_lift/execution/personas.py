"""Persona-owned phase runs and typed artifact handoffs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from powdrr_lift.core.behavior_rule import BehaviorRule
from powdrr_lift.core.delivery_profile import (
    DeliveryProfile,
    PersonaDefinition,
    PhaseType,
)
from powdrr_lift.core.execution_state import ExecutionState
from powdrr_lift.core.tool_manifest import ToolEffect


class PersonaRunStatus(StrEnum):
    ACTIVE = "active"
    HANDED_OFF = "handed_off"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PersonaPacket:
    """The complete prompt/runtime assignment for one active persona run.

    ``responsibility`` and ``posture`` are the only prompt text supplied by
    the kernel. Step-specific instructions remain in the active step packet;
    no prior persona or phase catalog is carried forward.
    """

    run_id: str
    execution_id: str
    phase_type: PhaseType
    persona: PersonaDefinition
    responsibility: str
    posture: str
    allowed_actions: frozenset[str]
    allowed_effects: frozenset[ToolEffect]
    input_artifact_ids: tuple[str, ...] = ()
    guidance_rules: tuple[BehaviorRule, ...] = ()

    def prompt_catalog(self) -> tuple[str, ...]:
        return (
            self.responsibility,
            self.posture,
            *(rule.text for rule in self.guidance_rules),
        )


@dataclass(frozen=True, slots=True)
class PersonaRun:
    run_id: str
    execution_id: str
    phase_type: PhaseType
    persona_id: str
    model_profile: str
    status: PersonaRunStatus = PersonaRunStatus.ACTIVE
    handoff_artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HandoffValidation:
    valid: bool
    errors: tuple[str, ...] = ()


def build_persona_packet(
    profile: DeliveryProfile,
    *,
    execution_id: str,
    run_id: str,
    phase_type: PhaseType,
    phase_actions: frozenset[str],
    persona_actions: Mapping[str, frozenset[str]],
    allowed_effects: frozenset[ToolEffect],
    input_artifact_ids: tuple[str, ...] = (),
    guidance_rules: tuple[BehaviorRule, ...] = (),
) -> PersonaPacket:
    phase = next(
        (item for item in profile.phases if item.phase_type is phase_type), None
    )
    if phase is None:
        raise ValueError(f"No assignment exists for phase {phase_type.value!r}.")
    persona = next(
        (item for item in profile.personas if item.persona_id == phase.persona_id), None
    )
    if persona is None:
        raise ValueError(f"No persona exists for {phase.persona_id!r}.")
    actions = phase_actions & persona_actions.get(persona.persona_id, frozenset())
    return PersonaPacket(
        run_id,
        execution_id,
        phase_type,
        persona,
        (
            f"Own the {phase_type.value} responsibilities assigned to "
            f"{persona.persona_id}."
        ),
        "Use only the active phase envelope and produce the assigned typed artifacts.",
        actions,
        allowed_effects,
        input_artifact_ids,
        guidance_rules,
    )


def validate_handoff(
    profile: DeliveryProfile,
    state: ExecutionState,
    *,
    source_phase: PhaseType,
    destination_phase: PhaseType,
    artifact_ids: tuple[str, ...],
) -> HandoffValidation:
    errors: list[str] = []
    contracts = tuple(
        handoff
        for handoff in profile.artifact_handoffs
        if handoff.source_phase is source_phase
        and handoff.destination_phase is destination_phase
    )
    artifacts = {artifact.artifact_id: artifact for artifact in state.artifacts}
    required_types = {
        contract.artifact_type for contract in contracts if contract.required
    }
    supplied = [artifacts.get(artifact_id) for artifact_id in artifact_ids]
    for artifact_type in required_types:
        if not any(
            item is not None and item.artifact_type == artifact_type and item.accepted
            for item in supplied
        ):
            errors.append(f"missing accepted {artifact_type} handoff artifact")
    for artifact in supplied:
        if artifact is None:
            errors.append("handoff references an unknown artifact")
        elif not artifact.accepted:
            errors.append(f"artifact {artifact.artifact_id!r} has not been accepted")
    for contract in contracts:
        for artifact in supplied:
            if (
                artifact is not None
                and artifact.artifact_type == contract.artifact_type
                and artifact.owner_persona_id != contract.owner_persona_id
            ):
                errors.append(f"artifact {artifact.artifact_id!r} has the wrong owner")
    return HandoffValidation(not errors, tuple(errors))
