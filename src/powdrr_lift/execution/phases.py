"""Pure phase transition rules for shadow and enforced execution."""

from __future__ import annotations

from dataclasses import dataclass

from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.execution_state import ExecutionState

DEFAULT_PHASE_TRANSITIONS: dict[PhaseType, frozenset[PhaseType]] = {
    PhaseType.INTAKE: frozenset({PhaseType.SPECIFY}),
    PhaseType.SPECIFY: frozenset({PhaseType.REVIEW_SPECIFICATIONS}),
    PhaseType.REVIEW_SPECIFICATIONS: frozenset({PhaseType.DECOMPOSE}),
    PhaseType.DECOMPOSE: frozenset({PhaseType.REVIEW_PROPOSED_PRS}),
    PhaseType.REVIEW_PROPOSED_PRS: frozenset({PhaseType.PLAN_PR}),
    PhaseType.PLAN_PR: frozenset({PhaseType.AWAIT_PLAN_DECISION, PhaseType.BUILD}),
    PhaseType.AWAIT_PLAN_DECISION: frozenset({PhaseType.BUILD}),
    PhaseType.BUILD: frozenset({PhaseType.VALIDATE}),
    PhaseType.VALIDATE: frozenset({PhaseType.REVIEW_PR}),
    PhaseType.REVIEW_PR: frozenset(
        {PhaseType.RESOLVE_FINDINGS, PhaseType.CONFIRM_READINESS}
    ),
    PhaseType.RESOLVE_FINDINGS: frozenset({PhaseType.VALIDATE, PhaseType.REVIEW_PR}),
    PhaseType.CONFIRM_READINESS: frozenset({PhaseType.PUBLISH_PR}),
    PhaseType.PUBLISH_PR: frozenset({PhaseType.COMPLETE_FEATURE}),
    PhaseType.COMPLETE_FEATURE: frozenset(),
}


@dataclass(frozen=True, slots=True)
class PhaseTransitionDecision:
    allowed: bool
    current_phase: PhaseType
    target_phase: PhaseType
    guards: tuple[str, ...] = ()


class PhaseController:
    """Evaluate the closed phase topology without consulting an LLM."""

    def __init__(
        self,
        transitions: dict[PhaseType, frozenset[PhaseType]] | None = None,
    ) -> None:
        self._transitions = transitions or DEFAULT_PHASE_TRANSITIONS

    def evaluate(
        self,
        state: ExecutionState,
        target_phase: PhaseType,
    ) -> PhaseTransitionDecision:
        allowed_targets = self._transitions.get(state.current_phase, frozenset())
        if target_phase not in allowed_targets:
            return PhaseTransitionDecision(
                allowed=False,
                current_phase=state.current_phase,
                target_phase=target_phase,
                guards=(
                    f"Transition from {state.current_phase.value} to "
                    f"{target_phase.value} is not allowed.",
                ),
            )
        return PhaseTransitionDecision(
            allowed=True,
            current_phase=state.current_phase,
            target_phase=target_phase,
        )
