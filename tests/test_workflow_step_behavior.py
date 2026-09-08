from dataclasses import dataclass

from powdrr_lift.workflow_step_behavior import behavior_for_step


@dataclass
class Step:
    step_type: str


def test_governed_behavior_owns_ordinary_step_policy() -> None:
    behavior = behavior_for_step(Step("governed"))

    assert behavior.invokes_llm
    assert not behavior.runs_gate
    assert behavior.runtime_actions(("edit",)) == frozenset({"edit"})
    assert not behavior.rejects_model_transition("next_step")


def test_predicated_behavior_owns_automatic_completion_policy() -> None:
    behavior = behavior_for_step(Step("predicated"))

    assert behavior.invokes_llm
    assert behavior.runtime_actions(("edit",)) == frozenset({"edit", "emit_outputs"})
    assert behavior.auto_advance_after_action(completion_satisfied=True)
    assert behavior.rejects_model_transition("next_step")


def test_deterministic_behaviors_do_not_invoke_llm() -> None:
    assert not behavior_for_step(Step("invoke_tool")).invokes_llm
    assert behavior_for_step(Step("gate")).runs_gate
