"""One deterministic entry point for tuning workflow definitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.workflow_definition_analysis import (
    analyze_workflow_definition,
    render_skill_prompt_snapshots,
)
from powdrr_lift.workflow_definition_comparison import (
    WorkflowComparisonError,
    compare_workflow_definitions,
)

WORKFLOW_TUNING_REPORT_SCHEMA_VERSION = 1


class WorkflowTuningError(ValueError):
    """Raised when a deterministic workflow-tuning run cannot be completed."""


def tune_workflow(
    *,
    definition: Path,
    repo_root: Path,
    baseline_ref: str,
    replay_paths: Sequence[Path] = (),
    scenario_paths: Sequence[Path] = (),
    thresholds: Mapping[str, int] | None = None,
    snapshot_output_dir: Path | None = None,
) -> dict[str, Any]:
    """Run deterministic validation and comparison, returning a portable report."""
    root = repo_root.resolve()
    definition_path = definition if definition.is_absolute() else root / definition
    if not definition_path.is_file():
        raise WorkflowTuningError(f"Definition does not exist: {definition_path}")
    static = analyze_workflow_definition(definition_path)
    snapshots: list[str] = []
    if snapshot_output_dir is not None:
        output_dir = (
            snapshot_output_dir
            if snapshot_output_dir.is_absolute()
            else root / snapshot_output_dir
        )
        snapshots = [
            str(path.relative_to(root))
            for path in render_skill_prompt_snapshots(
                definition_path, output_dir=output_dir, repo_root=root
            )
        ]
    try:
        comparison = compare_workflow_definitions(
            repo_root=root,
            baseline_ref=baseline_ref,
            replay_paths=replay_paths,
            scenario_paths=scenario_paths,
            thresholds=thresholds,
        )
    except WorkflowComparisonError as exc:
        raise WorkflowTuningError(str(exc)) from exc
    static_data = static.to_data()
    report = {
        "schema_version": WORKFLOW_TUNING_REPORT_SCHEMA_VERSION,
        "definition": _portable_path(definition_path, root),
        "definition_hash": hashlib.sha256(definition_path.read_bytes()).hexdigest(),
        "status": (
            "passed" if static.validation_successful and comparison.passed else "failed"
        ),
        "static_validation": static_data,
        "prompt_snapshots": snapshots,
        "replays": [
            case.to_data()
            for case in comparison.candidate_cases
            if case.kind == "replay"
        ],
        "scenarios": [
            case.to_data()
            for case in comparison.candidate_cases
            if case.kind == "scenario"
        ],
        "comparison": comparison.to_data(),
        "failure_clusters": [],
        "summary": {
            "cases": comparison.candidate_metrics.total_cases,
            "passed": comparison.candidate_metrics.passed_cases,
            "failed": comparison.candidate_metrics.failed_cases,
            "valid_first_action_rate": _valid_rate(comparison.to_data()),
            "repair_count": 0,
        },
    }
    return report


def save_workflow_tuning_report(path: Path, report: Mapping[str, Any]) -> Path:
    """Save a completed tuning report; reports are never staged automatically."""
    if report.get("schema_version") != WORKFLOW_TUNING_REPORT_SCHEMA_VERSION:
        raise WorkflowTuningError("Invalid workflow tuning report schema version.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def _portable_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def _valid_rate(comparison: Mapping[str, Any]) -> float:
    metrics = comparison["candidate_metrics"]
    assert isinstance(metrics, Mapping)
    total = int(metrics["total_cases"])
    return int(metrics["valid_replays"]) / total if total else 1.0
