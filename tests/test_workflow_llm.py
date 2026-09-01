from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from powdrr_lift.workflow_execution import ProgressDecision
from powdrr_lift.workflow_llm import (
    WorkflowActionObservation,
    WorkflowActionOutcome,
    WorkflowActionRequest,
    WorkflowExecutionStrategy,
    WorkflowLLMActionEngine,
    WorkflowStepRunner,
    prompt_size_breakdown,
    prune_execution_events,
    workflow_action_signature,
)


def test_prompt_size_breakdown_reports_execution_mode_and_top_level_fields() -> None:
    messages = [
        {"role": "system", "content": "rules"},
        {
            "role": "user",
            "content": '{"execution_mode":"execute_selected_skill",'
            '"current_step":{"description":"Inspect"},"events":[1,2,3]}',
        },
    ]

    breakdown = prompt_size_breakdown(messages)

    assert breakdown["execution_mode"] == "execute_selected_skill"
    assert breakdown["estimated_input_tokens"] > 0
    fields = breakdown["fields"]
    assert fields["system_prompt"] == 2
    assert fields["message_1.current_step"] > 0
    assert fields["message_1.events"] > 0


@dataclass(frozen=True)
class _Action:
    kind: str
    value: str = ""


class _ProgressStrategy:
    def __init__(self, state: str) -> None:
        self.state = state
        self.observations: list[WorkflowActionObservation] = []

    def material_state(self, action: _Action) -> str:
        _ = action
        return self.state

    def record_no_progress(
        self,
        action: _Action,
        observation: WorkflowActionObservation,
    ) -> None:
        _ = action
        self.observations.append(observation)


class _Client:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        _ = messages
        return self.payloads.pop(0)


class _ExecutionStrategy(WorkflowExecutionStrategy):
    def __init__(self) -> None:
        self.client = _Client([{"kind": "next_step"}, {"kind": "complete"}])
        self.executed: list[str] = []
        self.observations: list[WorkflowActionObservation] = []

    def next_request(self) -> WorkflowActionRequest:
        return WorkflowActionRequest(
            client=self.client,
            messages=[{"role": "user", "content": "run"}],
            parser=lambda payload: _Action(kind=str(payload["kind"])),
            model="test",
            stderr=None,
            max_timeout_retries=0,
            timeout_backoff_seconds=0,
        )

    def material_state(self, action: _Action) -> object:
        _ = action
        return None

    def record_no_progress(
        self,
        action: _Action,
        observation: WorkflowActionObservation,
    ) -> None:
        _ = action
        self.observations.append(observation)

    def record_response_error(
        self, error: RuntimeError, payload: dict[str, Any] | None
    ) -> None:
        raise error

    def execute_action(self, action: _Action) -> WorkflowActionOutcome:
        self.executed.append(action.kind)
        return WorkflowActionOutcome(continue_running=action.kind != "complete")

    def record_action_error(self, action: _Action, error: Exception) -> None:
        raise error

    def action_failure_exit_code(self, action: _Action) -> int:
        _ = action
        return 1

    def observe_outcome(
        self,
        action: _Action,
        observation: WorkflowActionObservation,
        outcome: WorkflowActionOutcome,
    ) -> WorkflowActionOutcome:
        _ = action
        self.observations.append(observation)
        return outcome

    def exhausted_roundtrips_exit_code(self) -> int:
        return 2


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


def test_action_failure_guard_ignores_narrative_changes() -> None:
    engine = WorkflowLLMActionEngine(max_stalled_roundtrips=2)

    def signature(action: dict[str, object]) -> str:
        return json.dumps(action)

    first_action: dict[str, object] = {
        "action": "invoke_tool",
        "parameters": {"command": ["powdrr-lift", "yaml-edit"]},
        "decisions_and_context": "first explanation",
    }
    second_action: dict[str, object] = {
        "action": "invoke_tool",
        "parameters": {"command": ["powdrr-lift", "yaml-edit"]},
        "decisions_and_context": "different explanation",
    }
    first = engine.record_action_failure(
        first_action,
        signature=signature,
    )
    threshold = engine.record_action_failure(
        second_action,
        signature=signature,
    )

    assert first is ProgressDecision.CONTINUE
    assert threshold is ProgressDecision.THRESHOLD


def test_action_engine_treats_repeated_gather_context_as_no_progress() -> None:
    engine = WorkflowLLMActionEngine(max_stalled_roundtrips=2)
    action = _Action(kind="gather_context", value="requirements")

    first = engine.observe_action(
        action,
        signature=workflow_action_signature,
        before_state="unchanged",
        after_state="unchanged",
    )
    repeated = engine.observe_action(
        action,
        signature=workflow_action_signature,
        before_state="unchanged",
        after_state="unchanged",
    )

    assert first.made_progress is True
    assert repeated.made_progress is False
    assert repeated.correction is not None


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


def test_action_engine_reports_stalls_through_the_runner_strategy() -> None:
    engine = WorkflowLLMActionEngine(max_stalled_roundtrips=1)
    strategy = _ProgressStrategy("unchanged")
    action = _Action(kind="invoke_tool", value="git status --short")

    first_state = engine.begin_action(action, strategy=strategy)
    engine.complete_action(
        action,
        before_state=first_state,
        signature=workflow_action_signature,
        strategy=strategy,
    )
    repeated_state = engine.begin_action(action, strategy=strategy)
    observation = engine.complete_action(
        action,
        before_state=repeated_state,
        signature=workflow_action_signature,
        strategy=strategy,
    )

    assert observation.decision is ProgressDecision.THRESHOLD
    assert strategy.observations == [observation]


def test_execution_driver_owns_roundtrips_and_terminal_action_outcomes() -> None:
    strategy = _ExecutionStrategy()

    exit_code = WorkflowStepRunner(
        max_stalled_roundtrips=1, legacy_compatibility=True
    ).run(
        strategy,
        max_roundtrips=3,
        signature=workflow_action_signature,
    )

    assert exit_code == 0
    assert strategy.executed == ["next_step", "complete"]
    assert [observation.signature for observation in strategy.observations] == [
        '{"kind": "next_step", "value": ""}',
        '{"kind": "complete", "value": ""}',
    ]


def test_execution_driver_can_stop_a_strategy_after_no_progress_threshold() -> None:
    class _StalledStrategy(_ExecutionStrategy):
        def __init__(self) -> None:
            super().__init__()
            self.client = _Client(
                [
                    {"kind": "next_step"},
                    {"kind": "next_step"},
                    {"kind": "next_step"},
                ]
            )

        def no_progress_threshold_exit_code(
            self,
            action: _Action,
            observation: WorkflowActionObservation,
        ) -> int:
            _ = action, observation
            return 7

    strategy = _StalledStrategy()
    assert (
        WorkflowStepRunner(max_stalled_roundtrips=1, legacy_compatibility=True).run(
            strategy,
            max_roundtrips=None,
            signature=workflow_action_signature,
        )
        == 7
    )


def test_execution_driver_never_crashes_when_observer_fails() -> None:
    class _FailingObserver:
        def response_failed(self, error: Exception) -> None:
            raise RuntimeError("observer failed") from error

        def action_failed(self, action: Any, error: Exception) -> None:
            raise RuntimeError("observer failed") from error

        def action_proposed(self, action: Any) -> None:
            _ = action
            raise RuntimeError("observer failed")

        def action_completed(
            self,
            action: Any,
            observation: WorkflowActionObservation,
        ) -> None:
            _ = action, observation
            raise RuntimeError("observer failed")

    strategy = _ExecutionStrategy()
    driver = WorkflowStepRunner(
        max_stalled_roundtrips=1,
        observer=_FailingObserver(),
        legacy_compatibility=True,
    )

    assert (
        driver.run(
            strategy,
            max_roundtrips=3,
            signature=workflow_action_signature,
        )
        == 0
    )


def test_execution_driver_supports_a_shared_model_fallback_request() -> None:
    strategy = _ExecutionStrategy()
    action = _Action(kind="complete")
    original_next_request = strategy.next_request

    def next_request() -> WorkflowActionRequest:
        request = original_next_request()
        return WorkflowActionRequest(
            client=request.client,
            messages=request.messages,
            parser=request.parser,
            model=request.model,
            stderr=request.stderr,
            max_timeout_retries=request.max_timeout_retries,
            timeout_backoff_seconds=request.timeout_backoff_seconds,
            request_action=lambda: action,
        )

    strategy.next_request = next_request  # type: ignore[method-assign]

    exit_code = WorkflowStepRunner(
        max_stalled_roundtrips=1, legacy_compatibility=True
    ).run(
        strategy,
        max_roundtrips=1,
        signature=workflow_action_signature,
    )

    assert exit_code == 0
    assert strategy.executed == ["complete"]


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
