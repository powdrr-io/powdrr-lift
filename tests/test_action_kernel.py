import pytest

from powdrr_lift.core.execution_state import (
    ExecutionEventType,
    initial_execution_state,
    reduce_execution_events,
)
from powdrr_lift.execution.kernel import ActionKernel, ActionLifecyclePhase
from powdrr_lift.intrinsic_enrich import execute_enrich_tool
from powdrr_lift.workflow_llm import PowdrrExecutionError


def test_action_kernel_records_one_ordered_lifecycle() -> None:
    kernel = ActionKernel()
    kernel.propose({"kind": "edit"})
    kernel.start({"kind": "edit"})
    kernel.complete({"kind": "edit"})
    assert [event.phase for event in kernel.events] == [
        ActionLifecyclePhase.PROPOSED,
        ActionLifecyclePhase.STARTED,
        ActionLifecyclePhase.COMPLETED,
    ]
    assert [event.sequence for event in kernel.events] == [0, 1, 2]


def test_action_kernel_requires_typed_correction_failure() -> None:
    kernel = ActionKernel()
    error = PowdrrExecutionError(
        "bad action",
        error_code="invalid_action",
        action_kind="edit",
        remediation="retry",
    )
    event = kernel.fail({"kind": "edit"}, error)
    assert event.error is error
    assert error.error_code == "invalid_action"
    assert error.remediation == "retry"


def test_enrich_input_errors_are_agent_correctable() -> None:
    with pytest.raises(PowdrrExecutionError) as raised:
        execute_enrich_tool({"format": "json"})
    assert raised.value.error_code == "invalid_enrich_format"
    assert raised.value.action_kind == "enrich"


def test_action_kernel_expands_and_closes_instance_bound_obligations() -> None:
    kernel = ActionKernel()
    event = kernel.propose(
        {"kind": "change"},
        semantic_action="change_mutable_row",
    )
    assert len(event.obligations) == 2
    assert len(kernel.open_obligations) == 2
    obligation = event.obligations[0]
    assert obligation.required_action is not None
    assert kernel.satisfy("action-0", obligation.required_action)
    assert len(kernel.open_obligations) == 1
    assert not kernel.satisfy("other-action", obligation.required_action)


def test_action_kernel_blocks_unrelated_actions_until_obligations_close() -> None:
    kernel = ActionKernel()
    kernel.propose({"kind": "change"}, semantic_action="change_mutable_row")

    assert kernel.validate_proposal({"kind": "complete"})
    assert not kernel.validate_proposal(
        {"kind": "test"}, semantic_action="run_concurrency_test"
    )
    kernel.complete({"kind": "test", "semantic_action": "run_concurrency_test"})
    assert kernel.validate_proposal({"kind": "test"})


def test_action_kernel_records_obligation_open_and_satisfied_events() -> None:
    kernel = ActionKernel()
    proposed = kernel.propose(
        {"kind": "review-edit"},
        semantic_action="edit_for_review_comment",
    )
    assert proposed.obligations
    assert sum(
        event.phase is ActionLifecyclePhase.OBLIGATION_OPENED for event in kernel.events
    ) == len(proposed.obligations)

    kernel.complete({"kind": "validation", "semantic_action": "run_validation"})
    assert (
        sum(
            event.phase is ActionLifecyclePhase.OBLIGATION_SATISFIED
            for event in kernel.events
        )
        == 1
    )


def test_action_kernel_preserves_validation_before_thread_resolution() -> None:
    kernel = ActionKernel()
    kernel.propose(
        {"kind": "review-edit"},
        semantic_action="edit_for_review_comment",
    )
    assert kernel.validate_proposal(
        {"kind": "resolve"}, semantic_action="resolve_review_thread"
    )


def test_action_kernel_serializes_obligation_provenance_and_closes_one_source() -> None:
    kernel = ActionKernel()
    first = kernel.propose({"kind": "change-1"}, semantic_action="change_mutable_row")
    second = kernel.propose({"kind": "change-2"}, semantic_action="change_mutable_row")
    assert (
        first.obligations[0].source_action_instance_id
        != second.obligations[0].source_action_instance_id
    )

    kernel.complete(
        {
            "kind": "lock",
            "semantic_action": "add_optimistic_lock",
            "source_action_instance_id": first.obligations[0].source_action_instance_id,
        }
    )
    remaining = [
        item
        for item in kernel.open_obligations
        if item.required_action == "add_optimistic_lock"
    ]
    assert len(remaining) == 1
    satisfied_event = next(
        event
        for event in kernel.events
        if event.phase is ActionLifecyclePhase.OBLIGATION_SATISFIED
    )
    assert satisfied_event.to_data()["obligations"][0]["relationship_id"]


def test_action_kernel_preserves_declared_safety_attributes() -> None:
    kernel = ActionKernel()
    event = kernel.propose(
        {
            "kind": "change_mutable_row",
            "attributes": ["optimistic_locking", "concurrency_evidence"],
        }
    )
    assert event.obligations == ()


def test_action_kernel_snapshot_is_json_compatible_and_replayable() -> None:
    kernel = ActionKernel()
    kernel.propose({"kind": "change"}, semantic_action="change_mutable_row")

    snapshot = kernel.snapshot()

    assert snapshot["events"][0]["phase"] == "proposed"
    assert snapshot["events"][1]["phase"] == "obligation_opened"
    assert all("relationship_id" in item for item in snapshot["open_obligations"])


def test_action_kernel_projects_lifecycle_to_durable_state_events() -> None:
    kernel = ActionKernel()
    kernel.propose({"kind": "change"}, semantic_action="change_mutable_row")
    kernel.complete({"kind": "test", "semantic_action": "run_concurrency_test"})
    initial = initial_execution_state("run-1", profile_id="default")

    events = kernel.to_execution_events(
        "run-1", phase_type="build", actor_id="engineer", starting_state=initial
    )
    rebuilt = reduce_execution_events(initial, events)

    assert events[0].event_type is ExecutionEventType.ACTION_PROPOSED
    assert any(
        event.event_type is ExecutionEventType.OBLIGATION_OPENED for event in events
    )
    assert any(
        event.event_type is ExecutionEventType.OBLIGATION_SATISFIED for event in events
    )
    assert len(rebuilt.obligations) == 2
    assert sum(item.status.value == "open" for item in rebuilt.obligations) == 1
