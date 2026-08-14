from __future__ import annotations

from powdrr_lift.workflow_execution import (
    ProgressDecision,
    WorkflowExecutionController,
)


def test_repeated_actions_use_the_same_no_progress_threshold() -> None:
    controllers = [WorkflowExecutionController(3), WorkflowExecutionController(3)]

    for controller in controllers:
        assert controller.observe("same-action", made_progress=True) == (
            ProgressDecision.PROGRESS
        )
        assert controller.observe("same-action", made_progress=False) == (
            ProgressDecision.CONTINUE
        )
        assert controller.observe("same-action", made_progress=False) == (
            ProgressDecision.CONTINUE
        )
        assert controller.observe("same-action", made_progress=False) == (
            ProgressDecision.THRESHOLD
        )


def test_corrective_failures_use_the_same_threshold_and_reset_on_progress() -> None:
    controller = WorkflowExecutionController(2)

    assert controller.record_failure("bad-action") == ProgressDecision.CONTINUE
    assert controller.record_failure("bad-action") == ProgressDecision.THRESHOLD
    assert controller.observe("fixed-action", made_progress=True) == (
        ProgressDecision.PROGRESS
    )
    assert controller.record_failure("new-bad-action") == ProgressDecision.CONTINUE
