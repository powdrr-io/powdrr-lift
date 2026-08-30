from pathlib import Path
from typing import Any

from powdrr_lift.core.execution_plan import (
    ExecutionPlan,
    ExecutionUnit,
    FileExecutionPlanStore,
    evaluate_execution_plan,
)


def unit(**kwargs: Any) -> ExecutionUnit:
    values = {
        "unit_id": "unit-1",
        "objective": "implement the change",
        "paths": ("src/app.py",),
        "validation_profiles": ("tests",),
        "acceptance_criteria": ("tests pass",),
    }
    values.update(kwargs)
    return ExecutionUnit(**values)  # type: ignore[arg-type]


def plan(*units: ExecutionUnit, decisions: tuple[str, ...] = ()) -> ExecutionPlan:
    return ExecutionPlan("plan-1", "pr-1", units, ("src",), decisions)


def test_complete_plan_is_build_eligible() -> None:
    result = evaluate_execution_plan(
        plan(unit()),
        proposed_pr_fingerprint="pr-1",
        proposed_pr_paths=("src/app.py",),
        known_validation_profiles=frozenset({"tests"}),
    )
    assert result.valid
    assert result.build_eligible


def test_plan_rejects_cycles_scope_and_unknown_validation() -> None:
    result = evaluate_execution_plan(
        plan(
            unit(dependencies=("unit-2",), paths=("../escape.py",)),
            unit(
                unit_id="unit-2",
                dependencies=("unit-1",),
                validation_profiles=("missing",),
            ),
        ),
        proposed_pr_fingerprint="changed",
        proposed_pr_paths=("tests/test.py",),
        known_validation_profiles=frozenset({"tests"}),
    )
    assert not result.valid
    assert any("cycle" in issue for issue in result.issues)
    assert any("fingerprint" in issue for issue in result.issues)


def test_material_decision_blocks_build_without_invalidating_plan() -> None:
    result = evaluate_execution_plan(
        plan(unit(), decisions=("choose migration strategy",)),
        proposed_pr_fingerprint="pr-1",
        proposed_pr_paths=("src/app.py",),
        known_validation_profiles=frozenset({"tests"}),
    )
    assert result.valid
    assert not result.build_eligible
    assert result.required_decisions == ("choose migration strategy",)


def test_plan_store_round_trips(tmp_path: Path) -> None:
    store = FileExecutionPlanStore(tmp_path)
    original = plan(unit())
    store.save(original)
    assert store.load("plan-1") == original
