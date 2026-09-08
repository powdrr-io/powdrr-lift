"""Execution behavior objects for workflow skill steps.

The workflow engine owns state and delegates step-specific policy to these
objects.  In particular, the engine does not need to know which step type
controls prompting, available actions, or whether a deterministic step should
invoke an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StepBehavior:
    """Policy surface used by the workflow engine for one step."""

    _kind: str
    invokes_llm: bool
    runs_gate: bool
    publishes_predicated_outputs: bool

    @property
    def is_predicated(self) -> bool:
        return self.publishes_predicated_outputs

    def runtime_actions(self, declared_actions: Any) -> frozenset[str]:
        actions = frozenset(declared_actions or ())
        if self.publishes_predicated_outputs:
            return actions | {"emit_outputs"}
        return actions

    def auto_advance_after_pre_step(self, *, completion_satisfied: bool) -> bool:
        return self.publishes_predicated_outputs and completion_satisfied

    def auto_advance_after_action(self, *, completion_satisfied: bool) -> bool:
        return self.publishes_predicated_outputs and completion_satisfied

    def rejects_model_transition(self, action_kind: str) -> bool:
        return self.publishes_predicated_outputs and action_kind == "next_step"


_BEHAVIORS: dict[str, StepBehavior] = {
    "governed": StepBehavior(
        "governed",
        invokes_llm=True,
        runs_gate=False,
        publishes_predicated_outputs=False,
    ),
    "predicated": StepBehavior(
        "predicated",
        invokes_llm=True,
        runs_gate=False,
        publishes_predicated_outputs=True,
    ),
    "invoke_tool": StepBehavior(
        "invoke_tool",
        invokes_llm=False,
        runs_gate=False,
        publishes_predicated_outputs=False,
    ),
    "uses_skill": StepBehavior(
        "uses_skill",
        invokes_llm=False,
        runs_gate=False,
        publishes_predicated_outputs=False,
    ),
    "gate": StepBehavior(
        "gate", invokes_llm=False, runs_gate=True, publishes_predicated_outputs=False
    ),
    "coding_loop": StepBehavior(
        "coding_loop",
        invokes_llm=True,
        runs_gate=False,
        publishes_predicated_outputs=False,
    ),
}


def behavior_for_step(step: Any) -> StepBehavior:
    """Return the execution policy for a parsed step."""
    step_type = getattr(step, "step_type", "governed")
    try:
        return _BEHAVIORS[step_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported workflow step behavior: {step_type!r}") from exc
