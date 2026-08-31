import pytest

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
