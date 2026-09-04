from pathlib import Path
from typing import Any, cast

import pytest

from powdrr_lift.errors import (
    AgentCorrectableError,
    ExecutionCancelled,
    PersistenceCorruptionError,
    PowdrrExecutionError,
    ProgrammerInvariantError,
    ProviderExecutionError,
)
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.workflow_llm import (
    WorkflowActionRequest,
    WorkflowExecutionStrategy,
    WorkflowStepRunner,
)


def test_execution_error_categories_are_distinct() -> None:
    categories = (
        AgentCorrectableError,
        ProviderExecutionError,
        ExecutionCancelled,
        PersistenceCorruptionError,
        ProgrammerInvariantError,
    )

    assert all(issubclass(category, PowdrrExecutionError) for category in categories)
    assert len({category.__name__ for category in categories}) == len(categories)


def test_execution_error_provides_typed_correction_notes() -> None:
    edit_error = PowdrrExecutionError("invalid edit", action_kind="edit")
    other_error = PowdrrExecutionError("invalid action", action_kind="next_step")

    assert "action=edit" in edit_error.correction_notes()
    assert other_error.correction_notes() == ""


def test_runtime_surfaces_corrupt_state_as_non_correctable_error(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "taxonomy-corrupt",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    state_path = (
        tmp_path / "workflow" / "execution" / runtime.execution_id / "state.json"
    )
    state_path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(PersistenceCorruptionError):
        runtime.verify()


def test_provider_failure_bypasses_model_correction_path() -> None:
    class Strategy:
        def next_request(self) -> WorkflowActionRequest:
            def fail() -> None:
                raise ProviderExecutionError("provider unavailable")

            return WorkflowActionRequest(
                client=cast(Any, object()),
                messages=[],
                parser=lambda payload: payload,
                model="test",
                stderr=None,
                max_timeout_retries=0,
                timeout_backoff_seconds=0,
                request_action=fail,
            )

        def record_response_error(self, error: RuntimeError, payload: object) -> None:
            raise AssertionError("provider failures must not enter correction")

    with pytest.raises(ProviderExecutionError):
        WorkflowStepRunner(max_stalled_roundtrips=1, legacy_compatibility=True).run(
            cast(WorkflowExecutionStrategy, Strategy()),
            max_roundtrips=1,
            signature=lambda _: "failure",
        )
