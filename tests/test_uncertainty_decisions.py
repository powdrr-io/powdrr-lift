from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.uncertainty_decisions import (
    records_for_scenario,
    update_decision_artifact,
)


def test_records_for_scenario_preserves_exact_source_and_decision_basis() -> None:
    source = "Add the feature and preserve existing behavior."
    start = source.index("preserve")
    end = len(source)
    clause = {
        "clause_id": "instruction-002",
        "fingerprint": "clause-fingerprint",
        "source_span": {"start": start, "end": end},
    }
    scenario = {
        "scenario_id": "scenario:instruction-002",
        "subject": "existing behavior",
        "given": "the feature is enabled",
        "when": "the new behavior runs",
        "then": "the existing behavior remains available",
        "assumptions": [
            {
                "dimension": "compatibility",
                "resolution": "Keep the existing behavior available.",
                "rationale": "The request requires preservation.",
                "basis": "conservative_default",
                "basis_reference": "The request is explicit about preservation.",
                "confidence": "high",
            }
        ],
    }

    records = records_for_scenario(
        clause,
        source,
        scenario,
        phase="design_interview",
        repository_location={"path": "src/example.py", "symbol": "run"},
    )

    assert records == [
        {
            "id": "uncertainty:instruction-002:compatibility",
            "originating_phase": "design_interview",
            "last_updated_phase": "design_interview",
            "source_ref": "instruction-002",
            "source_fingerprint": "clause-fingerprint",
            "source_quote": "preserve existing behavior.",
            "source_location": {
                "kind": "request_text",
                "start_offset": start,
                "end_offset": end,
            },
            "uncertainty": (
                "The source leaves compatibility undefined for this behavior."
            ),
            "dimension": "compatibility",
            "selected_default": "Keep the existing behavior available.",
            "rationale": "The request requires preservation.",
            "basis": "conservative_default",
            "basis_reference": "The request is explicit about preservation.",
            "confidence": "high",
            "scenario": {
                "scenario_id": "scenario:instruction-002",
                "subject": "existing behavior",
                "given": "the feature is enabled",
                "when": "the new behavior runs",
                "then": "the existing behavior remains available",
            },
            "revision_history": [],
            "repository_location": {"path": "src/example.py", "symbol": "run"},
        }
    ]


def test_decision_artifact_is_incremental_and_retains_revisions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "uncertainty-decisions.json"
    source = "Process all items, even on failure."
    clause = {
        "clause_id": "instruction-001",
        "source_span": {"start": 0, "end": len(source)},
    }
    initial_assumption: dict[str, str] = {
        "dimension": "continuation",
        "resolution": "Stop after the first failure.",
        "rationale": "Use the simplest failure boundary.",
        "basis": "conservative_default",
        "basis_reference": "No specific normative source identified.",
        "confidence": "low",
    }
    initial: dict[str, Any] = {
        "subject": "batch operation",
        "given": "several items are supplied",
        "when": "one item fails",
        "then": "processing continues",
        "assumptions": [initial_assumption],
    }
    record = records_for_scenario(clause, source, initial, phase="design_interview")

    update_decision_artifact(
        path,
        record,
        uncertainty_policy="normative_default",
        instruction_ledger_fingerprint="ledger-fingerprint",
    )
    persisted_before_later_work = json.loads(path.read_text(encoding="utf-8"))
    revised = {
        **initial,
        "assumptions": [
            {
                **initial_assumption,
                "resolution": "Continue processing remaining items.",
                "rationale": "Keep independent items useful after an isolated failure.",
            }
        ],
    }
    revised_record = records_for_scenario(
        clause,
        source,
        revised,
        phase="scenario_consistency_review",
    )
    update_decision_artifact(
        path,
        revised_record,
        uncertainty_policy="normative_default",
        instruction_ledger_fingerprint="ledger-fingerprint",
    )
    persisted_after_revision = json.loads(path.read_text(encoding="utf-8"))
    decision = persisted_after_revision["decisions"][0]

    assert persisted_before_later_work["decisions"][0]["selected_default"] == (
        "Stop after the first failure."
    )
    assert decision["id"] == "uncertainty:instruction-001:continuation"
    assert decision["originating_phase"] == "design_interview"
    assert decision["last_updated_phase"] == "scenario_consistency_review"
    assert decision["selected_default"] == "Continue processing remaining items."
    assert decision["revision_history"] == [
        {
            "selected_default": "Stop after the first failure.",
            "rationale": "Use the simplest failure boundary.",
            "basis": "conservative_default",
            "basis_reference": "No specific normative source identified.",
            "confidence": "low",
            "last_updated_phase": "design_interview",
            "superseded_in_phase": "scenario_consistency_review",
        }
    ]
