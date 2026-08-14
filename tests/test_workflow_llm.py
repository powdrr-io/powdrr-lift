from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from powdrr_lift.workflow_execution import ProgressDecision
from powdrr_lift.workflow_llm import (
    WorkflowLLMActionEngine,
    prune_execution_events,
    workflow_action_signature,
)


@dataclass(frozen=True)
class _Action:
    kind: str
    value: str = ""


def test_action_engine_uses_the_same_state_aware_no_progress_rule() -> None:
    engine = WorkflowLLMActionEngine(max_stalled_roundtrips=2)
    action = _Action(kind="invoke_tool", value="git status --short")

    first = engine.observe_action(
        action,
        signature=workflow_action_signature,
        before_state="clean",
        after_state="clean",
    )
    repeated = engine.observe_action(
        action,
        signature=workflow_action_signature,
        before_state="clean",
        after_state="clean",
    )
    threshold = engine.observe_action(
        action,
        signature=workflow_action_signature,
        before_state="clean",
        after_state="clean",
    )

    assert first.decision is ProgressDecision.PROGRESS
    assert repeated.decision is ProgressDecision.CONTINUE
    assert repeated.correction is not None
    assert threshold.decision is ProgressDecision.THRESHOLD
    assert "Do not invoke this action unchanged again" in (threshold.correction or "")


def test_action_engine_accepts_a_material_state_change_for_a_repeat() -> None:
    engine = WorkflowLLMActionEngine(max_stalled_roundtrips=1)
    action = _Action(kind="edit", value="README.md")

    engine.observe_action(
        action,
        signature=workflow_action_signature,
        before_state="before",
        after_state="after",
    )
    observation = engine.observe_action(
        action,
        signature=workflow_action_signature,
        before_state="after",
        after_state="after-again",
    )

    assert observation.made_progress is True
    assert observation.decision is ProgressDecision.PROGRESS


def test_prompt_event_pruning_preserves_task_results_and_bounds_large_values() -> None:
    events: list[dict[str, Any]] = [
        {"kind": "invoke_tool", "result": {"stdout": "x" * 9_000}},
        {"kind": "edit", "result": {"path": "README.md"}},
    ]

    chat_events = prune_execution_events(events, include_results=False)
    task_events = prune_execution_events(events, include_results=True)

    assert all("result" not in event for event in chat_events)
    assert task_events[0]["result"] == {
        "truncated": True,
        "preview": '{"stdout": "' + "x" * 7_988,
    }
    assert task_events[1]["result"] == {"path": "README.md"}
