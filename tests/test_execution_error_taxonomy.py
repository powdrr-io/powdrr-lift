from pathlib import Path

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
