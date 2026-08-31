from pathlib import Path

from powdrr_lift.core.delivery_profile import DeliveryProfile, PhaseType
from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
from powdrr_lift.execution.compaction import (
    FileContextRetrievalStore,
    compact_execution_context,
    compatibility_diagnostic,
)
from powdrr_lift.execution.compile import compile_execution_plan


def test_compiler_preserves_actions_personas_and_dependencies() -> None:
    profile = DeliveryProfile.from_file(
        "delivery-profiles/default-software-delivery.yaml"
    )
    plan = ExecutionPlan(
        "plan-1",
        "pr-1",
        (
            ExecutionUnit("one", "first unit", ("src",), acceptance_criteria=("done",)),
            ExecutionUnit(
                "two",
                "second unit",
                ("src",),
                dependencies=("one",),
                acceptance_criteria=("done",),
            ),
        ),
        ("src",),
    )
    tasks = compile_execution_plan(
        profile,
        plan,
        actions_by_phase={PhaseType.BUILD: ("edit",)},
    )
    build_tasks = [task for task in tasks if task.phase_type is PhaseType.BUILD]
    assert build_tasks[0].actions == ("edit",)
    assert build_tasks[0].persona_id == "engineer"
    assert "one-build" in build_tasks[1].upstream_task_ids


def test_compaction_retains_typed_references_and_bounds_previews() -> None:
    compacted = compact_execution_context(
        {"prose": "x" * 20, "plan_id": "plan-1", "finding_ids": ["f-1"]},
        max_preview_chars=8,
    )
    assert compacted["prose"] == "x" * 7 + "…"
    assert compacted["plan_id"] == "plan-1"
    assert compacted["finding_ids"] == ["f-1"]
    assert compatibility_diagnostic({"schema_version": "old-v0"}) is not None
    assert compatibility_diagnostic({"schema_version": "execution-state-v1"}) is None


def test_retrieval_store_bounds_ephemeral_context_history(tmp_path: Path) -> None:
    store = FileContextRetrievalStore(tmp_path, max_entries=2)
    references = [store.save({"sequence": index}) for index in range(3)]

    assert len(tuple(store.root.glob("*.json"))) == 2
    assert store.load(references[-1])["sequence"] == 2
