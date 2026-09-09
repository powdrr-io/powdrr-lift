from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from powdrr_lift.workflow_execution import ProgressDecision
from powdrr_lift.workflow_llm import (
    ProgrammerInvariantError,
    RepairFailure,
    RepairFailureClass,
    RepairPolicy,
    RepairStage,
    WorkflowActionObservation,
    WorkflowActionOutcome,
    WorkflowActionRequest,
    WorkflowExecutionStrategy,
    WorkflowLLMActionEngine,
    WorkflowRepairCoordinator,
    WorkflowStepRunner,
    assert_material_repair_prompt,
    build_clean_room_action_parameters_prompt,
    build_clean_room_action_selection_prompt,
    build_clean_room_repair_prompt,
    build_repair_prompt_manifest,
    complete_json_with_timeout_retry,
    complete_two_pass_action,
    constrain_action_response_schema,
    prompt_size_breakdown,
    prune_execution_events,
    workflow_action_signature,
)


def test_repair_coordinator_is_bounded_and_resets_at_boundaries() -> None:
    coordinator = WorkflowRepairCoordinator(
        RepairPolicy(targeted_attempts=1, clean_room_attempts=1)
    )
    coordinator.begin_boundary("step-1")
    response_failure = RepairFailure(
        RepairFailureClass.RESPONSE, "invalid_json", "response was not JSON"
    )
    assert coordinator.record_failure(response_failure).stage == RepairStage.TARGETED
    assert (
        coordinator.record_failure(
            RepairFailure(
                RepairFailureClass.EXECUTION,
                "action_failed",
                "edit failed",
                action_signature="edit:file-a",
            )
        ).stage
        == RepairStage.CLEAN_ROOM
    )
    exhausted = coordinator.record_failure(
        RepairFailure(
            RepairFailureClass.EXECUTION,
            "action_failed_again",
            "another action failed",
            action_signature="edit:file-b",
        )
    )
    assert exhausted.stage == RepairStage.EXHAUSTED

    coordinator.begin_boundary("step-2")
    assert coordinator.record_failure(response_failure).attempt == 1


def test_repair_coordinator_rejects_duplicate_failure_identity() -> None:
    coordinator = WorkflowRepairCoordinator()
    coordinator.begin_boundary("step-1")
    failure = RepairFailure(
        RepairFailureClass.NO_PROGRESS,
        "no_progress",
        "same action repeated",
        action_signature="edit:file-a",
    )
    coordinator.record_failure(failure)
    try:
        coordinator.record_failure(failure)
    except ProgrammerInvariantError as error:
        assert error.error_code == "duplicate_repair_attempt"
    else:
        raise AssertionError("duplicate repair failure was accepted")


def test_clean_room_repair_prompt_excludes_conversation_and_records_profile() -> None:
    original = [
        {"role": "system", "content": "normal instructions"},
        {
            "role": "user",
            "content": '{"objective":"keep-objective","canary":"old-history"}',
        },
        {"role": "assistant", "content": '{"action":"bad","canary":"old-payload"}'},
    ]
    previous = build_repair_prompt_manifest(
        original,
        profile="normal_full_context",
        source_sections=("conversation",),
        history_policy="full",
        allowed_actions=("edit", "read_document"),
        reasoning_mode="direct_action",
    )
    repaired, current = build_clean_room_repair_prompt(
        context="workflow execution",
        error_message="bad action",
        repair_instructions="Choose a legal action.",
        allowed_actions=("read_document",),
    )

    assert len(repaired) == 2
    assert all("old-history" not in message["content"] for message in repaired)
    assert all("old-payload" not in message["content"] for message in repaired)
    assert current.profile == "clean_room_replan"
    assert current.history_policy == "none"
    assert_material_repair_prompt(previous, current)


def test_material_repair_prompt_rejects_cosmetic_changes() -> None:
    first = build_repair_prompt_manifest(
        [{"role": "user", "content": "one"}],
        profile="targeted_schema_correction",
        history_policy="full",
        reasoning_mode="direct_action",
    )
    second = build_repair_prompt_manifest(
        [{"role": "user", "content": "two"}],
        profile="targeted_schema_correction",
        history_policy="full",
        reasoning_mode="direct_action",
    )

    try:
        assert_material_repair_prompt(first, second)
    except ProgrammerInvariantError as error:
        assert error.error_code == "repair_prompt_not_materially_different"
    else:
        raise AssertionError("cosmetic repair prompt change was accepted")


def test_two_pass_repair_selects_then_constrains_action_parameters() -> None:
    class _Client:
        def __init__(self) -> None:
            self.messages: list[list[dict[str, str]]] = []

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            self.messages.append(messages)
            if len(self.messages) == 1:
                return {"action": "edit"}
            return {"action": "edit", "file_path": "README.md"}

    client = _Client()
    selection_messages, selection_schema, _ = build_clean_room_action_selection_prompt(
        context="change the README",
        error_message="the previous action stalled",
        allowed_actions=("edit", "complete"),
    )
    parameter_schema = constrain_action_response_schema(
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["edit", "complete"]},
                "file_path": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["action"],
        },
        "edit",
    )
    parameter_messages, _ = build_clean_room_action_parameters_prompt(
        context="change the README",
        error_message="the previous action stalled",
        selected_action="edit",
        response_schema=parameter_schema,
    )

    action = complete_two_pass_action(
        client,
        selection_messages=selection_messages,
        selection_schema=selection_schema,
        parameter_messages_for=lambda _action: parameter_messages,
        parameter_schema_for=lambda _action: parameter_schema,
        parser=lambda payload: payload,
        allowed_actions=("edit", "complete"),
        model="test-model",
        stderr=None,
        max_timeout_retries=0,
        timeout_backoff_seconds=0,
    )

    assert action["action"] == "edit"
    assert len(client.messages) == 2
    assert '"repair_stage":"action_selection"' in client.messages[0][1]["content"]
    assert '"repair_stage":"action_parameters"' in client.messages[1][1]["content"]
    assert parameter_schema["properties"]["action"]["enum"] == ["edit"]


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


def test_timeout_retry_preserves_optional_response_schema(
    monkeypatch: Any,
) -> None:
    schema = {"type": "object"}

    class _SchemaClient:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            _ = messages
            self.calls.append(response_schema)
            if len(self.calls) == 1:
                raise RuntimeError("request timed out")
            return {"action": "next_step"}

    monkeypatch.setattr("powdrr_lift.workflow_llm.time.sleep", lambda _: None)
    client = _SchemaClient()

    assert complete_json_with_timeout_retry(
        client,
        [],
        model="test",
        stderr=None,
        max_timeout_retries=1,
        timeout_backoff_seconds=0,
        response_schema=schema,
    ) == {"action": "next_step"}
    assert client.calls == [schema, schema]


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


def test_execution_driver_bounds_coding_loop_iterations() -> None:
    class _CodingLoopStrategy(_ExecutionStrategy):
        current_step_index = 0

        def __init__(self) -> None:
            super().__init__()
            self.client = _Client(
                [{"kind": "next_step"}, {"kind": "next_step"}, {"kind": "next_step"}]
            )
            self.current_step = type(
                "Step",
                (),
                {
                    "id": "implement",
                    "coding_loop": type("Loop", (), {"max_iterations": 2})(),
                },
            )()
            self.exhausted_limit: int | None = None

        def coding_loop_exhausted(self, limit: int) -> int:
            self.exhausted_limit = limit
            return 9

    strategy = _CodingLoopStrategy()
    assert (
        WorkflowStepRunner(max_stalled_roundtrips=1, legacy_compatibility=True).run(
            strategy, max_roundtrips=None, signature=workflow_action_signature
        )
        == 9
    )
    assert strategy.exhausted_limit == 2
    assert strategy.executed == ["next_step", "next_step"]


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
