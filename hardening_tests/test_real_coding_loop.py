from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_support_path = Path(__file__).parents[1] / "tests" / "test_workflow_task_scenario.py"
_support_spec = importlib.util.spec_from_file_location(
    "workflow_task_scenario_support", _support_path
)
assert _support_spec is not None and _support_spec.loader is not None
_support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(_support)


@pytest.mark.real_coding_loop
def test_coding_task_agent_reacts_to_failure_and_verifies_the_fix(
    tmp_path: Path,
) -> None:
    _support._coding_task_agent_reacts_to_failure_and_verifies_the_fix(tmp_path)
