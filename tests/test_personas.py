from dataclasses import replace

from powdrr_lift.core.delivery_profile import DeliveryProfile, PhaseType
from powdrr_lift.core.execution_state import ExecutionArtifact, initial_execution_state
from powdrr_lift.core.tool_manifest import ToolEffect
from powdrr_lift.execution.personas import build_persona_packet, validate_handoff


def profile() -> DeliveryProfile:
    return DeliveryProfile.from_file("delivery-profiles/default-software-delivery.yaml")


def test_persona_packet_intersects_step_and_persona_actions() -> None:
    packet = build_persona_packet(
        profile(),
        execution_id="execution-1",
        run_id="run-1",
        phase_type=PhaseType.SPECIFY,
        phase_actions=frozenset({"inspect", "write_specification"}),
        persona_actions={"architect": frozenset({"inspect"})},
        allowed_effects=frozenset({ToolEffect.WORKSPACE_READ}),
    )
    assert packet.allowed_actions == frozenset({"inspect"})
    assert packet.prompt_catalog() == (
        packet.responsibility,
        packet.posture,
    )


def test_handoff_requires_owned_accepted_artifact() -> None:
    current = initial_execution_state("execution-1", profile_id=profile().profile_id)
    state = replace(
        current,
        artifacts=(
            ExecutionArtifact(
                "request-1", "request", "request-v1", "architect", "request.yaml", True
            ),
        ),
    )
    result = validate_handoff(
        profile(),
        state,
        source_phase=PhaseType.INTAKE,
        destination_phase=PhaseType.SPECIFY,
        artifact_ids=("request-1",),
    )
    assert result.valid
    wrong_owner = replace(
        state,
        artifacts=(replace(state.artifacts[0], owner_persona_id="engineer"),),
    )
    assert not validate_handoff(
        profile(),
        wrong_owner,
        source_phase=PhaseType.INTAKE,
        destination_phase=PhaseType.SPECIFY,
        artifact_ids=("request-1",),
    ).valid
