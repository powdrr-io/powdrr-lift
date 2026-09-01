from pathlib import Path

from powdrr_lift.execution.acceptance import (
    REQUIRED_BUILTIN_MANIFESTS,
    audit_capability_surface,
    run_final_acceptance,
)
from powdrr_lift.execution.builtin_tools import builtin_tool_registry


def test_final_acceptance_scenario_passes_without_an_llm(tmp_path: Path) -> None:
    report = run_final_acceptance(Path(__file__).parents[1], tmp_path / "workflow")

    assert report.passed
    assert {check.name for check in report.checks} == {
        "compiled-task-graph",
        "runtime-contract",
        "persona-phase-assignments",
        "review-resolution-order",
        "mutable-row-consequences",
        "durable-lifecycle",
        "adapter-parity",
        "production-task-adapter",
        "production-chat-adapter",
        "stale-evidence-gate",
        "compaction-retrieval",
        "full-phase-walk",
        "interruption-replay",
        "partial-failure-recovery",
        "exception-decision-flow",
        "scope-expansion-blocked",
    }


def test_capability_audit_covers_all_registered_manifests() -> None:
    checks = audit_capability_surface(builtin_tool_registry())

    assert len(checks) == len(REQUIRED_BUILTIN_MANIFESTS) + 2
    assert all(check.passed for check in checks)
